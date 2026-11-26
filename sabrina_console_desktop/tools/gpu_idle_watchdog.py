#!/usr/bin/env python3
"""
gpu_idle_watchdog.py -- stop the RunPod GPU pod when Sabrina goes idle, so you stop paying
GPU-hour cost (the pod GPU is ~97% of the RunPod bill; disk is negligible) while nothing runs.

Idle is detected from the pod's OWN GPU utilization (nvidia-smi over SSH -- the same number the
RunPod API's runtime.gpus[].util reports), NOT from local process state, so it works even if the
supervisor is on a different machine. Utilization must stay below IDLE_UTIL_PCT for a *sustained*
IDLE_MINUTES before the pod is stopped -- IDLE_MINUTES must exceed the 72b's ~2 min between-call
gaps so an active run (which spikes the GPU every couple minutes) never trips it.

It also meters spend: prints accrued cost this session and the cost it has saved by stopping.

STOP requires a RunPod API key (console -> Settings -> API Keys) in env RUNPOD_API_KEY or in .env.
DETECTION needs only SSH to the pod (SSH_KEY below).

  python tools/gpu_idle_watchdog.py                 # run until Ctrl+C
  python tools/gpu_idle_watchdog.py --dry-run       # detect + meter, but never actually stop
  python tools/gpu_idle_watchdog.py --once          # one detection cycle, print state, exit
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

# --- config (env overrides) --------------------------------------------------------------------
POD_ID        = os.environ.get("RUNPOD_POD_ID", "kgn0uwgzfk2zhf")
POD_IP        = os.environ.get("RUNPOD_POD_IP", "154.54.102.30")
POD_SSH_PORT  = os.environ.get("RUNPOD_POD_SSH_PORT", "14857")  # changes on each pod restart
SSH_KEY       = os.path.expanduser(os.environ.get("RUNPOD_SSH_KEY", "~/.ssh/id_ed25519"))
IDLE_UTIL_PCT = float(os.environ.get("IDLE_UTIL_PCT", "5"))     # GPU util below this = idle
IDLE_MINUTES  = float(os.environ.get("IDLE_MINUTES", "15"))     # sustained idle before stopping
POLL_SECONDS  = int(os.environ.get("POLL_SECONDS", "60"))
GPU_HOURLY    = float(os.environ.get("GPU_HOURLY_USD", "1.59"))
REST_BASE     = os.environ.get("RUNPOD_REST_BASE", "https://rest.runpod.io/v1")


def _now():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[idle-watchdog {_now()}] {msg}", flush=True)


def _api_key():
    # Use the project's secret_store: OS credential store -> os.environ -> .env. So it resolves
    # the key however Charles stored it (`python tools/secret_store.py set RUNPOD_API_KEY`, an env
    # var, or a .env line) -- same path the loop uses for GEMINI/ANTHROPIC keys.
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tools.secret_store import get_secret
        k = get_secret("RUNPOD_API_KEY")
        if k:
            return k.strip()
    except Exception:
        pass
    return os.environ.get("RUNPOD_API_KEY", "").strip() or None


def gpu_util():
    """Return current GPU utilization % via SSH nvidia-smi, or None if the pod is unreachable
    (unreachable usually means it's already stopped/exited -- treat as 'do not act')."""
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15", "-i", SSH_KEY, "-p", POD_SSH_PORT, f"root@{POD_IP}",
        "nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        vals = [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        return max(vals) if vals else None
    except Exception as e:
        log(f"gpu_util check failed ({e}) -- pod may be down; not acting")
        return None


def stop_pod():
    """Stop the pod via RunPod REST v2. Returns (ok, detail)."""
    key = _api_key()
    if not key:
        return False, "no RUNPOD_API_KEY (set it in env or .env) -- cannot stop"
    req = urllib.request.Request(
        f"{REST_BASE}/pods/{POD_ID}/stop",
        data=b"{}",
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="detect + meter but never stop")
    ap.add_argument("--once", action="store_true", help="one cycle, print state, exit")
    args = ap.parse_args()

    have_key = bool(_api_key())
    log(f"watching pod {POD_ID} (ssh {POD_IP}:{POD_SSH_PORT}); idle = GPU<{IDLE_UTIL_PCT}% for "
        f"{IDLE_MINUTES} min; poll {POLL_SECONDS}s; GPU ${GPU_HOURLY}/hr; "
        f"stop-key={'present' if have_key else 'MISSING (detect-only)'}; "
        f"dry_run={args.dry_run}")

    session_start = time.time()
    idle_since = None
    while True:
        util = gpu_util()
        elapsed_hr = (time.time() - session_start) / 3600.0
        accrued = elapsed_hr * GPU_HOURLY
        if util is None:
            state = "unreachable (already stopped?)"
            idle_since = None
        elif util < IDLE_UTIL_PCT:
            if idle_since is None:
                idle_since = time.time()
            idle_min = (time.time() - idle_since) / 60.0
            state = f"IDLE {idle_min:.1f}/{IDLE_MINUTES} min (util {util}%)"
            if idle_min >= IDLE_MINUTES:
                log(f"idle threshold reached (util {util}% for {idle_min:.1f} min) -- "
                    f"{'DRY-RUN, not stopping' if args.dry_run else 'STOPPING POD'}")
                if not args.dry_run:
                    ok, detail = stop_pod()
                    log(f"stop_pod -> {ok} ({detail})")
                    if ok:
                        log(f"pod stopped. GPU billing halted. session cost so far ~${accrued:.2f}; "
                            f"further GPU spend now ~$0/hr until you start it again.")
                        return
                idle_since = None  # dry-run: reset and keep watching
        else:
            idle_since = None
            state = f"active (util {util}%)"
        log(f"{state} | session ~${accrued:.2f}")
        if args.once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
