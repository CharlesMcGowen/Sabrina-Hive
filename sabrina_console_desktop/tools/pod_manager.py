"""Queue-driven RunPod lifecycle manager -- Sabrina brings the GPU up when work is queued (grabbing a
slot the instant one frees), and stops it when nothing's pending, so the pod is up exactly when needed
and never bills idle.

Builds on tools/pod_control (REST start/stop + ollama boot). Single responsibility: pod UP-when-demanded
/ DOWN-when-idle. It does NOT run the work -- it pairs with run_engagement.serve (which does the actual
recon/engagement work while the pod is up). Sabrina runs this on the lab as a standing manager.

PREREQUISITE: RUNPOD_API_KEY must be in the lab secret_store:
    python tools/secret_store.py set RUNPOD_API_KEY <key>
Without it pod_control can't start/stop, so the manager only OBSERVES (reports it can't act) -- it never
silently no-ops.

COST GUARDS (Charles: don't waste money on idle): resumes ONLY when demand>0; idle-stops after
idle_minutes of no demand; a hard --max-hours cap stops the pod and exits no matter what. Auto-resume
retries every tick, so a host-full "can't resume yet" just waits and grabs the GPU when a slot opens.
"""

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools import pod_control  # noqa: E402


def pending_engagements() -> int:
    """Runnable engagements whose recon phase isn't done yet -- the GPU demand signal. (The standing
    improvement backlog is deliberately NOT counted; it's not on-demand GPU work, and counting it would
    pin the pod on forever.)"""
    try:
        from tools.engagement_tracker import list_engagements, engagement_status
        n = 0
        for e in list_engagements():
            if not e.get("runnable"):
                continue
            st = (engagement_status(e["slug"]).get("recon", {}) or {}).get("status")
            if st != "done":
                n += 1
        return n
    except Exception:
        return 0


def demand() -> int:
    """Total pending GPU work. Pluggable seam -- extend with a gpu-job queue etc. as needed."""
    return pending_engagements()


def manage_once(pod_id, ollama_url, state, idle_minutes=15):
    """One decision tick. `state` is a persistent dict carrying {'idle_since': ts|None} across ticks."""
    d = demand()
    up = pod_control._ollama_ready(ollama_url)
    now = time.time()

    if not pod_control._api_key():
        return {"demand": d, "ollama_up": up,
                "action": "OBSERVE-ONLY: no RUNPOD_API_KEY in secret_store -- cannot start/stop the pod"}

    if d > 0 and not up:
        ok, detail = pod_control.ensure_running(pod_id, ollama_url)
        state["idle_since"] = None
        action = f"resume ({'READY' if ok else 'waiting for a free GPU'}): {detail}"
    elif d > 0 and up:
        state["idle_since"] = None
        action = f"working: {d} engagement(s) pending, pod up"
    elif d == 0 and up:
        if state.get("idle_since") is None:
            state["idle_since"] = now
            action = f"no demand -> idle timer started ({idle_minutes}m to stop)"
        elif now - state["idle_since"] >= idle_minutes * 60:
            ok, detail = pod_control.stop(pod_id)
            state["idle_since"] = None
            action = f"idle-stop ({'ok' if ok else 'FAILED'}): {detail}"
        else:
            action = f"no demand -> idle {int((now - state['idle_since']) / 60)}/{idle_minutes}m"
    else:  # d == 0 and not up
        state["idle_since"] = None
        action = "quiet: no demand, pod down"
    return {"demand": d, "ollama_up": up, "action": action}


def manage_loop(pod_id, ollama_url, interval=300, idle_minutes=15, max_hours=None, verbose=True):
    """Standing manager: tick every `interval`s. Hard-stops the pod and exits at `max_hours` (a spend
    ceiling), so an autonomous manager can never run the GPU unbounded."""
    state = {"idle_since": None}
    started = time.time()
    while True:
        if max_hours and (time.time() - started) >= max_hours * 3600:
            if pod_control._api_key() and (pod_control._ollama_ready(ollama_url)
                                           or pod_control.get_status(pod_id) == "RUNNING"):
                pod_control.stop(pod_id)
            if verbose:
                print(f"[pod_manager] max_hours={max_hours} cap reached -> pod stopped, exiting.", flush=True)
            return
        r = manage_once(pod_id, ollama_url, state, idle_minutes)
        if verbose:
            print(f"[pod_manager] demand={r['demand']} up={r['ollama_up']} -> {r['action']}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Queue-driven RunPod lifecycle manager.")
    ap.add_argument("action", choices=["status", "once", "loop"])
    ap.add_argument("--pod", default=os.environ.get("RUNPOD_POD_ID", "45m90jfro2yzjn"))
    ap.add_argument("--ollama", default=(os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",")[0]
                                         or "https://45m90jfro2yzjn-11434.proxy.runpod.net"))
    ap.add_argument("--interval", type=int, default=300)
    ap.add_argument("--idle-minutes", type=int, default=15)
    ap.add_argument("--max-hours", type=float, default=None)
    a = ap.parse_args()
    if a.action == "status":
        print({"demand": demand(), "pending_engagements": pending_engagements(),
               "pod_status": pod_control.get_status(a.pod),
               "ollama_up": pod_control._ollama_ready(a.ollama),
               "can_act (api_key present)": bool(pod_control._api_key())})
    elif a.action == "once":
        print(manage_once(a.pod, a.ollama, {"idle_since": None}, a.idle_minutes))
    else:
        manage_loop(a.pod, a.ollama, a.interval, a.idle_minutes, a.max_hours)
