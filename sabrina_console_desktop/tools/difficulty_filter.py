"""Difficulty filter for the scaled Dream-RSI deep-search run (Round 7b).

The dreamed batch-selection policy only has a lever when discovery trees have DEPTH, and depth only
appears on tasks in a specific difficulty band: the 72b can't one-shot them, but CAN solve them via
multi-step draft->repair. This module runs each task through one-shot AND agentic, tags its band, and
emits the sweet-spot subset that will actually produce deep trees to dream over.

Bands:
  trivial   : one-shot already solves it            -> shallow tree, DROP
  sampling  : one-shot fails, agentic solves at iters==0 (best-of-breadth draft, no repair)
                                                     -> still shallow (pure sampling), DROP for depth
  sweet_spot: one-shot fails, agentic solves at iters>=1 (needed repair depth)
                                                     -> DEEP trees, KEEP
  hopeless  : neither solves within budget          -> no payoff node, DROP

Only 'sweet_spot' tasks are emitted as the corpus for deep search. Reuses the real solvers
(capability_bench.solve_with_pod for one-shot, agentic_solver.solve_agentic for agentic) and the same
grader, so a task's band here is the band it will actually exhibit during the scaled run.

    python tools/difficulty_filter.py --breadth 4 --iters 8              # filter all tasks
    python tools/difficulty_filter.py --tasks crt_solve,eval_rational    # a subset
"""

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
OUT_DEFAULT = REPO_ROOT / "config" / "sweet_spot_tasks.json"

from tools.agentic_solver import solve_agentic, grade, _extract_code  # noqa: E402


def classify_task(name, prompt, verify, *, breadth=4, iters=8, probe_fn=None,
                  one_shot_fn=None, agentic_fn=None, chat=None):
    """Run one task through one-shot then (only if that fails) agentic, and return its difficulty band.

    one_shot_fn(prompt)->raw_text and agentic_fn(prompt, verify, **kw)->result are injectable for
    offline testing; they default to the real pod solvers. `calls` totals model calls spent classifying.
    """
    if one_shot_fn is None:
        from tools.capability_bench import solve_with_pod
        one_shot_fn = solve_with_pod
    if agentic_fn is None:
        agentic_fn = solve_agentic

    calls = 0
    os_sv = 0.0
    try:
        os_code = _extract_code(one_shot_fn(prompt))
        calls += 1
        os_sv, _ = grade(os_code, verify, probe_fn)
    except Exception as e:
        return {"task": name, "band": "error", "one_shot_solved": False, "agentic_solved": False,
                "agentic_iters": None, "calls": calls, "detail": f"one-shot error: {type(e).__name__}: {e}"}

    if os_sv >= 1.0:
        return {"task": name, "band": "trivial", "one_shot_solved": True, "agentic_solved": None,
                "agentic_iters": None, "calls": calls}

    # One-shot failed -> does repair-scaffolding rescue it, and did it need DEPTH (iters>=1)?
    _kw = {} if chat is None else {"chat": chat}
    try:
        r = agentic_fn(prompt, verify, breadth=breadth, max_iters=iters, task_name=name,
                       probe_fn=probe_fn, log=None, **_kw)
    except Exception as e:
        return {"task": name, "band": "error", "one_shot_solved": False, "agentic_solved": False,
                "agentic_iters": None, "calls": calls, "detail": f"agentic error: {type(e).__name__}: {e}"}
    calls += r.get("calls", 0)

    if not r.get("solved"):
        band = "hopeless"
    elif r.get("iters", 0) >= 1:
        band = "sweet_spot"          # needed repair depth -> produces deep trees
    else:
        band = "sampling"            # best-of-breadth draft solved it -> shallow
    return {"task": name, "band": band, "one_shot_solved": False, "agentic_solved": bool(r.get("solved")),
            "agentic_iters": r.get("iters"), "calls": calls}


def run_filter(breadth=4, iters=8, tasks=None, chat=None, verbose=True,
               one_shot_fn=None, agentic_fn=None, out=OUT_DEFAULT):
    """Classify a task set and write the sweet-spot subset. `tasks` is an optional list of
    (name, prompt, verify); defaults to capability_bench.all_tasks(). Returns the report dict."""
    from tools.capability_bench import all_tasks, all_probes
    task_list = tasks if tasks is not None else all_tasks()
    probes = all_probes()

    report, bands = [], {}
    total_calls = 0
    for name, prompt, verify in task_list:
        rec = classify_task(name, prompt, verify, breadth=breadth, iters=iters,
                             probe_fn=probes.get(name), one_shot_fn=one_shot_fn,
                             agentic_fn=agentic_fn, chat=chat)
        report.append(rec)
        bands[rec["band"]] = bands.get(rec["band"], 0) + 1
        total_calls += rec.get("calls", 0)
        if verbose:
            print(f"  [{rec['band']:>9}] {name}  (iters={rec.get('agentic_iters')}, calls={rec.get('calls')})",
                  flush=True)

    sweet = [r["task"] for r in report if r["band"] == "sweet_spot"]
    result = {"breadth": breadth, "iters": iters, "n_tasks": len(task_list), "bands": bands,
              "sweet_spot": sweet, "total_calls": total_calls, "report": report}
    if out is not None:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Tag tasks by difficulty band; emit the deep-search sweet-spot subset.")
    ap.add_argument("--breadth", type=int, default=4)
    ap.add_argument("--iters", type=int, default=8)
    ap.add_argument("--tasks", default=None, help="Comma-separated task-name subset (default: all).")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    a = ap.parse_args()

    task_filter = None
    if a.tasks:
        want = {t.strip() for t in a.tasks.split(",") if t.strip()}
        from tools.capability_bench import all_tasks as _all
        task_filter = [t for t in _all() if t[0] in want]

    res = run_filter(breadth=a.breadth, iters=a.iters, tasks=task_filter, out=a.out)
    print(f"\nBANDS: {res['bands']}", flush=True)
    print(f"SWEET-SPOT ({len(res['sweet_spot'])}/{res['n_tasks']}): {res['sweet_spot']}", flush=True)
    print(f"total classification calls: {res['total_calls']}  ->  wrote {a.out}", flush=True)
