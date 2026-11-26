"""Sabrina's self-contained capability-ablation pipeline.

One command she owns end-to-end -- no hand-driven pod / curl / ssh steps:

    python tools/run_ablation.py --pod <id> --model qwen2.5:72b-instruct [audit|sweep] [--no-stop]

It:
  1. brings the RunPod pod up and waits for ollama          (tools.pod_control.ensure_running)
  2. ensures the target model is pulled                     (tools.pod_control.ensure_model, streaming)
  3. runs the honest decomposition against it               (tools.capability_bench.run_audit / run_sweep)
     -- one-shot vs equal-budget best-of-N vs agentic, with sampling_gain / feedback_gain
  4. records the result to data/capability_audits.json
  5. ALWAYS stops the pod afterwards (unless --no-stop) so it never bills idle.

This replaces the manual sequence (create-pod / curl /api/pull / ssh capability_bench / pod stop) with
a repeatable pipeline the loop or the operator can invoke. The pod URL/model default to the routing
config (SABRINA_RUNPOD_OLLAMA_URLS / SABRINA_FORCE_LOCAL_MODEL), so a new pod id is a one-flag change.

Honest limitation: this ensures an EXISTING pod is running -- it does NOT create a fresh pod if the pod
is terminated or its host is full (that stays an operator decision, since it is a billable create). If
ensure_running fails, the pipeline reports and aborts rather than running against nothing.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import pod_control

LEDGER = REPO_ROOT / "data" / "capability_audits.json"


def _default_ollama(pod_id: str) -> str:
    env = os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",")[0].strip()
    return env or f"https://{pod_id}-11434.proxy.runpod.net"


def _record(rec: dict):
    try:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        data = []
        if LEDGER.exists():
            try:
                data = json.loads(LEDGER.read_text())
            except Exception:
                data = []
        data.append(rec)
        LEDGER.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[run_ablation] WARNING: could not write ledger: {e}", flush=True)


def run(pod_id: str, ollama_url: str, model: str, mode: str = "audit",
        ns=(4, 8, 16, 32), stop_after: bool = True, ts: str = "") -> dict:
    # Route capability_bench at this pod+model. These are read at import time, so set them BEFORE
    # importing capability_bench below.
    os.environ["SABRINA_RUNPOD_OLLAMA_URLS"] = ollama_url
    os.environ["SABRINA_FORCE_LOCAL_MODEL"] = model

    result = {"ts": ts, "pod": pod_id, "ollama": ollama_url, "model": model, "mode": mode, "ok": False}
    try:
        print(f"[run_ablation] ensuring pod {pod_id} is running + ollama up ...", flush=True)
        ok, detail = pod_control.ensure_running(pod_id, ollama_url)
        print(f"[run_ablation] ensure_running: {ok} -- {detail}", flush=True)
        if not ok:
            result["error"] = f"ensure_running failed: {detail}"
            return result

        ok, detail = pod_control.ensure_model(ollama_url, model)
        print(f"[run_ablation] ensure_model: {ok} -- {detail}", flush=True)
        if not ok:
            result["error"] = f"ensure_model failed: {detail}"
            return result

        # Import AFTER env is set so POD_OLLAMA / MODEL bind to this pod+model.
        from tools import capability_bench as cb
        if mode == "sweep":
            rows = cb.run_sweep(ns=tuple(ns), verbose=True)
            result.update({"ok": True, "sweep": rows})
            print("\nSWEEP: " + "  ".join(f"N={r['n']}:{r['score']:.0%}" for r in rows), flush=True)
        else:
            rec = cb.run_audit(verbose=True)
            result.update({"ok": True, "audit": rec})
            print(f"\nAUDIT: one-shot {rec['one_shot']:.0%} | best-of-{rec['bestofN_n']} {rec['bestofN']:.0%} | "
                  f"agentic {rec['agentic']:.0%}  ==>  sampling_gain {rec['sampling_gain']:+.2f}, "
                  f"feedback_gain {rec['feedback_gain']:+.2f}", flush=True)
        return result
    finally:
        _record(result)
        if stop_after:
            sok, sdetail = pod_control.stop(pod_id)
            print(f"[run_ablation] pod stop: {sok} -- {sdetail}", flush=True)
        else:
            print("[run_ablation] --no-stop: leaving pod RUNNING (it is billing).", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Sabrina's pod->model->ablation->stop pipeline.")
    ap.add_argument("mode", nargs="?", choices=["audit", "sweep"], default="audit")
    ap.add_argument("--pod", default=os.environ.get("SABRINA_RUNPOD_POD_ID",
                    os.environ.get("RUNPOD_POD_ID", "45m90jfro2yzjn")))
    ap.add_argument("--ollama", default="")
    ap.add_argument("--model", default=os.environ.get("SABRINA_FORCE_LOCAL_MODEL", "qwen2.5:72b-instruct"))
    ap.add_argument("--ns", default="4,8,16,32", help="sweep only: comma-separated N values")
    ap.add_argument("--no-stop", action="store_true", help="leave the pod running after (default: stop it)")
    a = ap.parse_args()
    ollama = a.ollama or _default_ollama(a.pod)
    ns = tuple(int(x) for x in a.ns.split(",") if x.strip())
    # A simple wall-clock stamp for the ledger (no external clock dependency in library code).
    stamp = time.strftime("%Y%m%d_%H%M%S")
    res = run(a.pod, ollama, a.model, mode=a.mode, ns=ns, stop_after=not a.no_stop, ts=stamp)
    raise SystemExit(0 if res.get("ok") else 1)
