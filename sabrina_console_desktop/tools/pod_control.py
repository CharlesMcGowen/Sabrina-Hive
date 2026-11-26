"""Start/stop the RunPod GPU pod around an improvement session, so it never bills while idle.

Reuses the same RunPod REST v2 surface + secret-store API key as tools/gpu_idle_watchdog.py. The
LOCAL launcher calls this: ensure_running() before a session, stop() after -- so a session is
start -> work -> stop, and there is zero idle billing between sessions.

Honest limitation: ollama does NOT auto-start when the pod boots (no /workspace boot hook is wired),
so ensure_running() will, after starting a stopped pod, best-effort SSH in and run start_ollama.sh.
If SSH/keys aren't available it starts the pod and reports that ollama may need a manual start.
All failures are non-fatal and reported; the caller decides whether to proceed.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

REST_BASE = os.environ.get("RUNPOD_REST_BASE", "https://rest.runpod.io/v1")


def _api_key():
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tools.secret_store import get_secret
        k = get_secret("RUNPOD_API_KEY")
        if k:
            return k.strip()
    except Exception:
        pass
    return (os.environ.get("RUNPOD_API_KEY", "") or "").strip() or None


def _req(method, path, key, timeout=30):
    req = urllib.request.Request(
        f"{REST_BASE}{path}", data=b"{}" if method == "POST" else None, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        try:
            return resp.status, json.loads(body)
        except Exception:
            return resp.status, body


def get_status(pod_id, key=None):
    """Return the pod's status string (RUNNING / EXITED / ...) or None on error."""
    key = key or _api_key()
    if not key:
        return None
    try:
        _, data = _req("GET", f"/pods/{pod_id}", key)
        return (data or {}).get("desiredStatus") or (data or {}).get("status")
    except Exception:
        return None


def start(pod_id, key=None):
    key = key or _api_key()
    if not key:
        return False, "no RUNPOD_API_KEY"
    try:
        st, _ = _req("POST", f"/pods/{pod_id}/start", key)
        return True, f"HTTP {st}"
    except Exception as e:
        return False, str(e)


def stop(pod_id, key=None):
    key = key or _api_key()
    if not key:
        return False, "no RUNPOD_API_KEY (set it in secret_store or env) -- cannot stop"
    try:
        st, _ = _req("POST", f"/pods/{pod_id}/stop", key)
        return True, f"HTTP {st}"
    except Exception as e:
        return False, str(e)


def _ollama_ready(ollama_url, timeout=8):
    try:
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/tags", headers={"User-Agent": "Sabrina"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return bool(json.load(r).get("models") is not None)
    except Exception:
        return False


def wait_ollama(ollama_url, timeout=300, poll=10):
    start_t = time.time()
    while time.time() - start_t < timeout:
        if _ollama_ready(ollama_url):
            return True
        time.sleep(poll)
    return False


def model_present(ollama_url, model, timeout=8):
    """True if `model` is already pulled on the pod's ollama."""
    try:
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/tags", headers={"User-Agent": "Sabrina"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return any(m.get("name") == model for m in (json.load(r).get("models") or []))
    except Exception:
        return False


def ensure_model(ollama_url, model, timeout=2400, verbose=True):
    """Ensure `model` is pulled on the pod's ollama, pulling it if absent. Returns (ok, detail).

    Streams /api/pull: a fresh pod's ephemeral disk has no cached model, and a non-streaming pull
    holds an idle connection that RunPod's Cloudflare proxy drops at ~100s, aborting the download.
    The streamed progress bytes keep the connection alive through the whole ~47GB pull.
    """
    if model_present(ollama_url, model):
        return True, f"{model} already present"
    if verbose:
        print(f"[pod_control] pulling {model} (streaming) ...", flush=True)
    url = ollama_url.rstrip("/") + "/api/pull"
    body = json.dumps({"name": model, "stream": True}).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"User-Agent": "Sabrina", "Content-Type": "application/json"})
    last = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:  # streamed JSON lines; iterating keeps the connection alive
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    last = json.loads(line).get("status", last)
                except Exception:
                    pass
    except Exception as e:
        # The stream can drop after the server-side pull has actually finished -- verify by tags.
        if model_present(ollama_url, model):
            return True, f"pulled {model} (stream dropped but model present)"
        return False, f"pull failed: {e} (last status: {last})"
    if last == "success" or model_present(ollama_url, model):
        return True, f"pulled {model}"
    return False, f"pull ended without success (last status: {last})"


def _ssh_start_ollama(pod_id, key):
    """Best-effort: read the pod's SSH info from the REST API and run the persistent start script."""
    try:
        _, data = _req("GET", f"/pods/{pod_id}", key)
        rt = (data or {}).get("runtime") or {}
        ip = port = None
        for p in rt.get("ports", []):
            if p.get("privatePort") == 22 or p.get("private") == 22:
                ip, port = p.get("ip"), p.get("publicPort") or p.get("public")
        if not ip or not port:
            return False, "no SSH port yet"
        ssh_key = os.path.expanduser(os.environ.get("RUNPOD_SSH_KEY", "~/.ssh/id_ed25519"))
        cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
               "-o", "ConnectTimeout=15", "-i", ssh_key, "-p", str(port), f"root@{ip}",
               "bash /workspace/start_ollama.sh >/tmp/ollama_boot.log 2>&1 &"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        return True, "start_ollama.sh invoked"
    except Exception as e:
        return False, str(e)


def ensure_running(pod_id, ollama_url, key=None, boot_timeout=360):
    """Make sure the pod is up and ollama answers. Returns (ok, detail). Non-fatal on failure."""
    key = key or _api_key()
    if not key:
        return False, "no RUNPOD_API_KEY -- cannot auto-start (start the pod manually)"
    if _ollama_ready(ollama_url):
        return True, "ollama already reachable"
    st = get_status(pod_id, key)
    if st != "RUNNING":
        ok, detail = start(pod_id, key)
        if not ok:
            return False, f"start failed: {detail}"
        # give the pod a moment to get an SSH port, then kick ollama
        time.sleep(20)
    _ssh_start_ollama(pod_id, key)
    if wait_ollama(ollama_url, timeout=boot_timeout):
        return True, "pod started + ollama ready"
    return False, ("pod started but ollama did not come up in time -- ollama has no boot hook; "
                   "SSH in and run /workspace/start_ollama.sh")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Start/stop the RunPod pod for improvement sessions.")
    ap.add_argument("action", choices=["status", "start", "stop", "ensure", "ensure-model"])
    ap.add_argument("--pod", default=os.environ.get("RUNPOD_POD_ID", "45m90jfro2yzjn"))
    ap.add_argument("--ollama", default=os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",")[0]
                    or "https://45m90jfro2yzjn-11434.proxy.runpod.net")
    ap.add_argument("--model", default=os.environ.get("SABRINA_FORCE_LOCAL_MODEL", "qwen2.5:72b-instruct"))
    a = ap.parse_args()
    if a.action == "status":
        print(get_status(a.pod))
    elif a.action == "start":
        print(start(a.pod))
    elif a.action == "stop":
        print(stop(a.pod))
    elif a.action == "ensure-model":
        print(ensure_model(a.ollama, a.model))
    else:
        print(ensure_running(a.pod, a.ollama))
