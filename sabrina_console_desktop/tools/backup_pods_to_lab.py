"""Sabrina's pod backup system -- pull each RUNNING RunPod pod's work down to the lab.

Runs ON the lab (192.168.2.69). Self-discovering: lists running pods via the RunPod API
(tools.pod_control.running_pods_ssh), finds each pod's direct SSH endpoint, and rsyncs its
/workspace (models + work, e.g. /workspace/ollama) to  <DEST_ROOT>/<name>-<id>/.

Why PULL (lab <- pod), not push: the lab sits behind NAT, so a pod can't reach it; the lab reaches
out to the pod's public SSH. Incremental + resumable (rsync --partial --append-verify), so a nightly
run only transfers the delta -- a 45GB model backs up once, then costs seconds a night until it
changes. Bounded to a window by the caller (cron `timeout 5h`, run 00:00 so it finishes by ~06:00).

REQUIREMENT: the lab's SSH key (SABRINA_POD_BACKUP_KEY, default ~/.ssh/id_ed25519_egollama) must be
authorized on each pod (its .pub in the pod's /root/.ssh/authorized_keys). The pods only trust the
account's key by default, so this is a one-time bootstrap done from a machine that can already SSH
the pod (see tools/authorize_lab_key_on_pods.sh / the setup notes). A pod we can't reach is logged
and skipped, never fatal -- one unreachable pod must not block backing up the others.

Env overrides:
  SABRINA_POD_BACKUP_DIR    destination root on the lab   (default /mnt/14tb/pod_backups)
  SABRINA_POD_BACKUP_KEY    ssh private key the lab uses  (default ~/.ssh/id_ed25519_egollama)
  SABRINA_POD_BACKUP_PATHS  comma-separated remote paths  (default /workspace)
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import pod_control

DEST_ROOT = os.environ.get("SABRINA_POD_BACKUP_DIR", "/mnt/14tb/pod_backups")
SSH_KEY = os.environ.get("SABRINA_POD_BACKUP_KEY", os.path.expanduser("~/.ssh/id_ed25519_egollama"))
REMOTE_PATHS = [p.strip() for p in os.environ.get("SABRINA_POD_BACKUP_PATHS", "/workspace").split(",") if p.strip()]


def _log(msg, logf=None):
    line = f"[backup {time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    if logf:
        try:
            logf.write(line + "\n")
            logf.flush()
        except Exception:
            pass


def _ssh_base(pod):
    return ["ssh", "-i", SSH_KEY, "-p", str(pod["port"]), "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", f"root@{pod['ip']}"]


def _ensure_remote_rsync(pod, logf=None):
    """rsync-over-ssh needs rsync on BOTH ends; RunPod base images often lack it, and pods are
    ephemeral so it must be (re)installed each time it's missing. Returns True if rsync is available.
    """
    try:
        chk = subprocess.run(_ssh_base(pod) + ["command -v rsync >/dev/null && echo HAVE || echo MISSING"],
                             capture_output=True, text=True, timeout=30)
        if "HAVE" in (chk.stdout or ""):
            return True
        if "MISSING" not in (chk.stdout or ""):
            _log(f"  {pod['id']}: cannot reach to check rsync :: {(chk.stderr or '').strip()[:100]}", logf)
            return False
        _log(f"  {pod['id']}: rsync missing -- installing (apt-get) ...", logf)
        inst = subprocess.run(_ssh_base(pod) + ["export DEBIAN_FRONTEND=noninteractive; "
                              "(apt-get update -qq && apt-get install -y -qq rsync) >/tmp/rsync_install.log 2>&1 && "
                              "command -v rsync >/dev/null && echo INSTALLED || echo INSTALL_FAILED"],
                              capture_output=True, text=True, timeout=300)
        ok = "INSTALLED" in (inst.stdout or "")
        _log(f"  {pod['id']}: rsync install {'ok' if ok else 'FAILED'}", logf)
        return ok
    except Exception as e:
        _log(f"  {pod['id']}: rsync ensure error {type(e).__name__}: {e}", logf)
        return False


def _rsync(pod, remote_path, dest, dry_run=False, logf=None):
    # No -z: model blobs (gguf/safetensors) are already compressed, so rsync -z just pins a CPU for
    # nothing. A fast AEAD cipher + ssh-level compression off keeps the transfer bound by the link,
    # not the crypto. --inplace + --partial resume a huge blob in place across nights.
    ssh_cmd = (f"ssh -i {SSH_KEY} -p {pod['port']} -o StrictHostKeyChecking=no "
               f"-o BatchMode=yes -o ConnectTimeout=20 -o Compression=no "
               f"-c {os.environ.get('SABRINA_POD_BACKUP_CIPHER', 'aes128-gcm@openssh.com')}")
    cmd = ["rsync", "-a", "--partial", "--inplace", "--info=stats1,progress2"]
    if dry_run:
        cmd.append("--dry-run")
    cmd += ["-e", ssh_cmd, f"root@{pod['ip']}:{remote_path.rstrip('/')}/", str(dest) + "/"]
    _log(f"rsync {pod['name']} ({pod['id']}) {remote_path} -> {dest}", logf)
    t0 = time.time()
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=int(os.environ.get("SABRINA_POD_BACKUP_TIMEOUT", "18000")))
        tail = (res.stdout or "").strip().splitlines()[-4:] + (res.stderr or "").strip().splitlines()[-3:]
        _log(f"  rc={res.returncode} in {time.time()-t0:.0f}s :: " + " | ".join(tail), logf)
        return res.returncode == 0
    except subprocess.TimeoutExpired:
        _log(f"  TIMEOUT after {time.time()-t0:.0f}s (partial transfer kept -- resumes next run)", logf)
        return False
    except Exception as e:
        _log(f"  ERROR {type(e).__name__}: {e}", logf)
        return False


def run(dry_run=False):
    Path(DEST_ROOT).mkdir(parents=True, exist_ok=True)
    logf = None
    try:
        logf = open(Path(DEST_ROOT) / "backup.log", "a", encoding="utf-8")
    except Exception:
        pass

    pods = pod_control.running_pods_ssh()
    _log(f"discovered {len(pods)} running pod(s): {[p['id'] for p in pods]}", logf)
    if not pods:
        _log("no running pods with SSH -- nothing to back up", logf)

    manifest = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "dry_run": dry_run, "pods": []}
    for pod in pods:
        entry = {"id": pod["id"], "name": pod["name"], "paths": {}}
        if not _ensure_remote_rsync(pod, logf):
            entry["paths"] = {rp: "unreachable_or_no_rsync" for rp in REMOTE_PATHS}
            _log(f"skipping {pod['id']} (unreachable or rsync unavailable)", logf)
            manifest["pods"].append(entry)
            continue
        for rp in REMOTE_PATHS:
            safe = rp.strip("/").replace("/", "_") or "root"
            dest = Path(DEST_ROOT) / f"{pod['name']}-{pod['id']}" / safe
            dest.mkdir(parents=True, exist_ok=True)
            ok = _rsync(pod, rp, dest, dry_run=dry_run, logf=logf)
            entry["paths"][rp] = "ok" if ok else "failed"
        manifest["pods"].append(entry)

    try:
        (Path(DEST_ROOT) / "last_run.json").write_text(json.dumps(manifest, indent=2))
    except Exception as e:
        _log(f"could not write manifest: {e}", logf)
    ok_all = all(v == "ok" for p in manifest["pods"] for v in p["paths"].values()) if manifest["pods"] else True
    _log(f"done. all_ok={ok_all}", logf)
    if logf:
        logf.close()
    return manifest, ok_all


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Back up running RunPod pods' /workspace to the lab.")
    ap.add_argument("--dry-run", action="store_true", help="list transfers without copying")
    a = ap.parse_args()
    _, ok = run(dry_run=a.dry_run)
    raise SystemExit(0 if ok else 1)
