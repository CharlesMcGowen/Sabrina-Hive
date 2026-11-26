"""Capability benchmark -- the measurement layer for provable RSI + "is the 72b a thinking model?".

A fixed, held-out set of coding/reasoning tasks. Each has a spec (prompt) and a HIDDEN verifier that
passes only if the produced code is actually correct. A solver (by default the 72b on the pod) writes
the code; we exec it in an isolated namespace and run the verifier. Score = % of tasks solved.

Why this exists: Sabrina could already edit code and check it *runs*, but never whether she got
*better*. Run this to get a baseline score; run it again after a self-improvement; a RISING score under
her own changes is the proof of recursive self-improvement AND evidence the 72b genuinely reasons.
Scores are logged to data/capability_scores.json with a timestamp + note (e.g. the checkpoint id), so
improvement over time is auditable.

    python tools/capability_bench.py run                    # score the pod 72b, append to the ledger
    python tools/capability_bench.py run --note "baseline"
    python tools/capability_bench.py history
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Make `from tools.X import ...` resolve even when this file is run as a SCRIPT (python
# tools/capability_bench.py), where sys.path[0] is tools/ and the `tools` package is otherwise
# invisible. Harmless when imported as a module (repo root already on the path).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
SCORES_FILE = REPO_ROOT / "data" / "capability_scores.json"
POD_OLLAMA = (os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",")[0]
              or "https://45m90jfro2yzjn-11434.proxy.runpod.net")
MODEL = os.environ.get("SABRINA_FORCE_LOCAL_MODEL", "qwen2.5:72b")

# --- The benchmark: (name, spec prompt, verifier(ns)->None-or-raises) -----------------------------
# Each spec asks for ONE named function; the verifier calls it and asserts real behavior. Tasks span
# string/logic/data-structure/parsing/reasoning so the score reflects genuine coding+reasoning, not
# memorization. Verifiers raise AssertionError on wrong output -> that task scores 0.

TASKS = [
    ("reverse_words",
     "Write a Python function `reverse_words(s: str) -> str` that reverses the order of words in a "
     "string, collapsing runs of whitespace to a single space and stripping ends. "
     "Respond with ONLY a ```python code block defining the function.",
     lambda ns: (ns["reverse_words"]("  the  quick brown fox ") == "fox brown quick the"
                 and ns["reverse_words"]("hello") == "hello")),

    ("merge_intervals",
     "Write `merge_intervals(intervals: list[list[int]]) -> list[list[int]]` that merges all "
     "overlapping intervals and returns them sorted by start. E.g. [[1,3],[2,6],[8,10],[15,18]] -> "
     "[[1,6],[8,10],[15,18]]. Respond with ONLY a ```python code block.",
     lambda ns: (ns["merge_intervals"]([[1,3],[2,6],[8,10],[15,18]]) == [[1,6],[8,10],[15,18]]
                 and ns["merge_intervals"]([[1,4],[4,5]]) == [[1,5]]
                 and ns["merge_intervals"]([]) == [])),

    ("roman_to_int",
     "Write `roman_to_int(s: str) -> int` converting a Roman numeral (I,V,X,L,C,D,M, with subtractive "
     "notation like IV=4, IX=9, XL=40) to an integer. Respond with ONLY a ```python code block.",
     lambda ns: (ns["roman_to_int"]("MCMXCIV") == 1994 and ns["roman_to_int"]("LVIII") == 58
                 and ns["roman_to_int"]("IV") == 4)),

    ("balanced_brackets",
     "Write `balanced(s: str) -> bool` returning True iff the brackets (), [], {} in s are correctly "
     "matched and nested (ignore other chars). Respond with ONLY a ```python code block.",
     lambda ns: (ns["balanced"]("a(b[c]{d}e)") is True and ns["balanced"]("([)]") is False
                 and ns["balanced"]("(((") is False and ns["balanced"]("") is True)),

    ("word_freq_top",
     "Write `top_word(text: str) -> str` returning the most frequent word (case-insensitive, words are "
     "maximal [a-z0-9]+ runs); on a tie return the one that appears first. Respond with ONLY a "
     "```python code block.",
     lambda ns: (ns["top_word"]("The cat the CAT dog") == "the"
                 and ns["top_word"]("a b c a b") == "a")),

    ("dedupe_stable",
     "Write `dedupe(xs: list) -> list` returning xs with duplicates removed but original order kept "
     "(first occurrence wins). Respond with ONLY a ```python code block.",
     lambda ns: (ns["dedupe"]([3,1,3,2,1,4]) == [3,1,2,4] and ns["dedupe"]([]) == [])),
]


# Task DEFINITIONS live in config/ (synced to the lab); scores/history stay in data/ (per-machine runtime).
EXTERNAL_TASKS_FILE = REPO_ROOT / "config" / "capability_tasks.json"


def _load_external_tasks():
    """Load harder tasks generated + adversarially validated by the generate-hard-coding-bench
    workflow. Each entry is {name, prompt, verify_src}; verify_src defines `def verify(ns)`."""
    out = []
    if not EXTERNAL_TASKS_FILE.exists():
        return out
    try:
        entries = json.loads(EXTERNAL_TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return out
    for t in entries:
        try:
            g = {}
            exec(t["verify_src"], g)
            vfn = g["verify"]
            out.append((t["name"], t["prompt"], vfn))
        except Exception:
            pass  # a malformed verifier just doesn't join the benchmark
    return out


def all_tasks():
    """Built-in easy tasks + the validated hard tasks (headroom for measuring improvement)."""
    return list(TASKS) + _load_external_tasks()


# --- Probes: turn a wrong answer into a CONCRETE counterexample for the repair loop -----------------
# A probe(ns) returns a short string naming a FAILING input and the candidate's OWN output -- never the
# expected value -- so the agentic repair step can reason about the specific defect instead of blindly
# resampling. Integrity: the probe author knows the answers, the model never sees them.

def _probe_from_cases(fn_name, cases):
    """Build a probe from (args_tuple, expected) pairs. Reveals the first failing input + the
    candidate's returned value; withholds `expected`."""
    def probe(ns):
        f = ns.get(fn_name)
        if not callable(f):
            return f"your code does not define a callable {fn_name}(...)"
        for args, expected in cases:
            try:
                got = f(*args)
            except Exception as e:
                return f"on input {args!r} your {fn_name} raised {type(e).__name__}: {str(e)[:80]}"
            if got != expected:
                return (f"on input {args!r} your {fn_name} returned {got!r}, which is incorrect -- "
                        f"work out the right answer for that specific case and fix the root cause")
        return None
    return probe


BUILTIN_PROBES = {
    "reverse_words": _probe_from_cases("reverse_words", [
        (("  the  quick brown fox ",), "fox brown quick the"), (("hello",), "hello"), ((" a  b ",), "b a")]),
    "merge_intervals": _probe_from_cases("merge_intervals", [
        (([[1, 3], [2, 6], [8, 10], [15, 18]],), [[1, 6], [8, 10], [15, 18]]),
        (([[1, 4], [4, 5]],), [[1, 5]]), (([],), [])]),
    "roman_to_int": _probe_from_cases("roman_to_int", [
        (("MCMXCIV",), 1994), (("LVIII",), 58), (("IV",), 4)]),
    "balanced": _probe_from_cases("balanced", [
        (("a(b[c]{d}e)",), True), (("([)]",), False), (("(((",), False), (("",), True)]),
    "top_word": _probe_from_cases("top_word", [
        (("The cat the CAT dog",), "the"), (("a b c a b",), "a")]),
    "dedupe": _probe_from_cases("dedupe", [
        (([3, 1, 3, 2, 1, 4],), [3, 1, 2, 4]), (([],), [])]),
}


def _external_task_entries():
    if not EXTERNAL_TASKS_FILE.exists():
        return []
    try:
        return json.loads(EXTERNAL_TASKS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _generic_probe_from_verify(verify_src):
    """Auto-build a counterexample probe from a task's verify_src -- no per-task authoring.

    How: the external verifiers all fetch the function(s) under test as ns['NAME'] and loop
    `for x in cases: if f(x) != reference(x): return False` (bailing at the FIRST failure). So we wrap
    exactly those target functions with a recorder, re-run verify, and if it fails, the LAST recorded
    call is the failing input and the candidate's own output. We reveal only (input, got) -- never the
    reference's expected value -- so the model still cannot hardcode against the hidden check."""
    import re as _re
    names = list(dict.fromkeys(_re.findall(r"ns\[['\"]([^'\"]+)['\"]\]", verify_src or "")))
    if not names:
        return None
    g = {}
    try:
        exec(verify_src, g)
        verify = g["verify"]
    except Exception:
        return None

    def probe(ns):
        calls = []
        wrapped = dict(ns)
        for nm in names:
            fn = ns.get(nm)
            if callable(fn):
                def mk(f, name):
                    def rec(*a, **kw):
                        try:
                            r = f(*a, **kw)
                        except Exception as e:          # record the FAILING input, then re-raise so
                            calls.append((name, a, kw, f"<raised {type(e).__name__}: {str(e)[:60]}>"))
                            raise                        # verify's own try/except still sees it
                        calls.append((name, a, kw, r))
                        return r
                    return rec
                wrapped[nm] = mk(fn, nm)
        try:
            ok = bool(verify(wrapped))
        except Exception:
            ok = False
        if ok or not calls:
            return None
        nm, a, kw, r = calls[-1]                          # the call that led to the first failure
        args = ", ".join([repr(x) for x in a] + [f"{k}={v!r}" for k, v in kw.items()])
        return (f"on input {nm}({args}) your code returned {r!r}, which is incorrect -- work out the "
                f"correct result for that specific case and fix the root cause")
    return probe


def _load_external_probes():
    """External tasks may ship an explicit `probe_src` (def probe(ns)); otherwise auto-generate one
    from verify_src. Explicit wins."""
    out = {}
    for t in _external_task_entries():
        name = t.get("name")
        if not name:
            continue
        src = t.get("probe_src")
        if src:
            try:
                g = {}
                exec(src, g)
                if callable(g.get("probe")):
                    out[name] = g["probe"]
                    continue
            except Exception:
                pass
        p = _generic_probe_from_verify(t.get("verify_src", ""))
        if p:
            out[name] = p
    return out


def all_probes():
    d = dict(BUILTIN_PROBES)
    d.update(_load_external_probes())   # external explicit/auto probes
    return d


def _extract_code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def solve_with_pod(prompt: str, timeout=180) -> str:
    """Default solver: ask the pod 72b to write the code. Returns raw model text."""
    import urllib.request
    payload = json.dumps({
        "model": MODEL, "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0.2, "num_ctx": 8192},
    }).encode()
    req = urllib.request.Request(POD_OLLAMA.rstrip("/") + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json", "User-Agent": "Sabrina"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return (data.get("message") or {}).get("content", "")


def run_benchmark(solver=solve_with_pod, verbose=True):
    # Score through the SAME capped grader as the agentic path (tools.agentic_solver.grade) so one-shot
    # and agentic can never disagree on the same candidate, and a runaway (infinite-loop) solution can
    # never hang the benchmark on an un-capped exec.
    from tools.agentic_solver import grade
    results = []
    for name, prompt, verify in all_tasks():
        ok, detail = False, ""
        try:
            code = _extract_code(solver(prompt))
            sv, fb = grade(code, verify)
            ok = sv >= 1.0
            detail = "passed" if ok else fb
        except Exception as e:
            detail = f"{type(e).__name__}: {e}".splitlines()[0][:120]
        results.append({"task": name, "passed": ok, "detail": detail})
        if verbose:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    score = sum(1 for r in results if r["passed"]) / len(results)
    return score, results


def run_benchmark_agentic(breadth=None, max_iters=3, verbose=True, save_tree=True, note_prefix="", chat=None, close_loop=False, strategy=None, use_policy=False):
    """Score the pod 72b in AGENTIC mode: each task is solved by a draft->test->repair search
    (tools/agentic_solver), not a single completion. This is the MOVABLE score -- lift over one-shot
    is lift from reasoning under feedback on fixed weights. Every draft/repair is logged as a graded
    node in ONE deep discovery tree (data/rsi_history_solve/) for Dream-RSI to navigate.

    breadth defaults to the dreamed SOLVE policy's worker_count (data/rsi_policy/solve_current.json --
    dreamed by rsi_improve.dream_solve over the solve pool) so the solve loop tunes its own search
    width; falls back to 2. With close_loop=True, dream_solve() runs after the session so the trees just
    logged update the solve policy for next time. Returns (score, results, tree_path)."""
    from tools.agentic_solver import solve_agentic, SOLVE_HISTORY_DIR, grade
    from tools.rsi_history import SessionTree
    # breadth/strategy come from the dreamed SOLVE policy ONLY when not passed explicitly. The audit
    # passes both explicitly so its measurement stays pinned (never reads the mutating solve policy).
    _pol = None
    try:
        from tools.rsi_improve import CandidatePolicy, SOLVE_CURRENT
        _pol = CandidatePolicy.load(SOLVE_CURRENT)   # the SOLVE policy, not the improvement policy
        if breadth is None:
            breadth = max(1, _pol.worker_count)
        if strategy is None:
            strategy = _pol.strategy
    except Exception:
        _pol = None
    if breadth is None:
        breadth = 2
    if strategy is None:
        strategy = "score"

    st = SessionTree(root_label=f"solve-benchmark:breadth{breadth}") if save_tree else None
    probes = all_probes()
    results, total_calls = [], 0
    for name, prompt, verify in all_tasks():
        try:
            _kw = {} if chat is None else {"chat": chat}  # inject a mock model for offline regression tests
            # Round 7: opt-in -- let the dreamed/authored SOLVE policy govern the live rollout. Guarded so
            # a failed policy load degrades to policy=None (legacy search). run_audit never sets use_policy,
            # so the pinned audit stays policy-free.
            _polkw = {"policy": _pol} if (use_policy and _pol is not None) else {}
            r = solve_agentic(prompt, verify, breadth=breadth, max_iters=max_iters, strategy=strategy,
                              tree=st, parent=None, task_name=name, probe_fn=probes.get(name),
                              log=(lambda m: print(m, flush=True)) if verbose else None, **_kw, **_polkw)
            total_calls += r["calls"]
            # Authoritative re-check through the SAME capped grader (no separate un-capped exec that
            # could hang on a runaway solution; grade() and the solver now agree by construction).
            _sv, _ = grade(_extract_code(r["best_code"]), verify)
            ok = _sv >= 1.0
            results.append({"task": name, "passed": ok, "calls": r["calls"],
                            "detail": f"agentic s_v={r['best_score']:.2f} iters={r['iters']} calls={r['calls']}"})
        except Exception as e:
            results.append({"task": name, "passed": False, "calls": 0, "detail": f"solver error: {type(e).__name__}: {e}"})
        if verbose:
            print(f"  [{'PASS' if results[-1]['passed'] else 'FAIL'}] {name}: {results[-1]['detail']}", flush=True)
    score = sum(1 for r in results if r["passed"]) / len(results)
    tree_path = None
    if st is not None:
        try:
            tree_path = st.save(label="benchmark", history_dir=SOLVE_HISTORY_DIR)
        except TypeError:
            tree_path = st.save(label="benchmark")  # rsi_history without the history_dir param
    if verbose:
        print(f"\n[agentic] {sum(r['passed'] for r in results)}/{len(results)} solved with "
              f"breadth={breadth} iters<= {max_iters}; {total_calls} pod calls; tree -> {tree_path}", flush=True)
    # Close the solve loop: dream a (never-worse) solve policy over the accumulated solve trees so the
    # NEXT run's breadth/strategy reflect what actually worked. Opt-in (real runs), off for offline tests.
    if close_loop and save_tree:
        try:
            from tools.rsi_improve import dream_solve
            dr = dream_solve()
            if verbose:
                print(f"[dream_solve] {dr.get('status')}: "
                      f"selected={dr.get('selected')} kept_current={dr.get('kept_current')}", flush=True)
        except Exception as e:
            if verbose:
                print(f"[dream_solve] error: {e}", flush=True)
    return score, results, tree_path


def run_benchmark_bestofN(n, verbose=True, chat=None):
    """Equal-budget NO-FEEDBACK control: n independent drafts per task (temperature spread), NO repair,
    report the max. This is the honest control for the agentic score -- best-of-N sampling on a fixed
    model beats best-of-1 on borderline tasks with ZERO feedback, so `agentic - bestofN@equal-budget`
    is the part attributable to reasoning-under-feedback. Implemented by reusing solve_agentic with
    max_iters=0 (no repair loop) and probe_fn=None (no counterexample). Returns (score, results)."""
    from tools.agentic_solver import solve_agentic, grade
    results = []
    for name, prompt, verify in all_tasks():
        try:
            _kw = {} if chat is None else {"chat": chat}
            r = solve_agentic(prompt, verify, breadth=max(1, n), max_iters=0, strategy="score",
                              tree=None, task_name=name, probe_fn=None,
                              log=(lambda m: print(m, flush=True)) if verbose else None, **_kw)
            _sv, _ = grade(_extract_code(r["best_code"]), verify)
            ok = _sv >= 1.0
            results.append({"task": name, "passed": ok, "calls": r["calls"],
                            "detail": f"best-of-{n} s_v={r['best_score']:.2f} calls={r['calls']}"})
        except Exception as e:
            results.append({"task": name, "passed": False, "calls": 0, "detail": f"error: {type(e).__name__}: {e}"})
        if verbose:
            print(f"  [{'PASS' if results[-1]['passed'] else 'FAIL'}] {name}: {results[-1]['detail']}", flush=True)
    score = sum(1 for r in results if r["passed"]) / len(results)
    return score, results


# --- The pinned honest audit: fixed tasks + fixed budget so runs are comparable over time -----------
AUDIT_TASKS = ["crt_solve", "largest_island", "count_palindromic_subsequences",
               "strict_roman_to_int", "rle_codec_roundtrip", "eval_rational"]
AUDIT_BREADTH = 2
AUDIT_ITERS = 3
AUDIT_FILE = REPO_ROOT / "data" / "capability_audits.json"


def _solver_from_chat(chat):
    """Adapt a chat(pod,model,messages,temp,timeout) into a one-shot solver(prompt)->text, so run_audit
    can drive one-shot, best-of-N and agentic from a SINGLE injected mock in offline tests."""
    def solver(prompt, timeout=180):
        return chat(POD_OLLAMA, MODEL, [{"role": "user", "content": prompt}], 0.2, timeout)
    return solver


def _ensure_model_reachable(chat=None):
    """Preflight before running (and LOGGING) an audit/sweep: confirm MODEL actually resolves on
    POD_OLLAMA. Skipped when `chat` is injected (offline/mock tests never hit real ollama).

    Without this, a bad or missing model -- e.g. SABRINA_FORCE_LOCAL_MODEL misconfigured to a stray
    value like "1" -- makes every solve call error inside solve_with_pod/solve_agentic; the broad
    exception handlers there turn that into passed=False for every task, which reads exactly like a
    genuine 0% capability score. run_audit/run_sweep would then log that as real data (2026-09-21:
    a junk {"model": "1", ...} all-zero record polluted data/capability_audits.json this way). Fail
    loudly here instead so an infra error can never be mistaken for a real result.
    """
    if chat is not None:
        return
    import urllib.request
    try:
        req = urllib.request.Request(POD_OLLAMA.rstrip("/") + "/api/tags", headers={"User-Agent": "Sabrina"})
        with urllib.request.urlopen(req, timeout=15) as r:
            names = {m.get("name") for m in json.load(r).get("models", [])}
    except Exception as e:
        raise RuntimeError(f"cannot reach {POD_OLLAMA} to verify model '{MODEL}' is available: {e}") from e
    if MODEL not in names:
        raise RuntimeError(
            f"model '{MODEL}' is not present on {POD_OLLAMA} (available: {sorted(names)}) -- refusing "
            f"to run: every task would silently score as a failure, logging a misleading 0% result."
        )


def run_audit(verbose=True, chat=None):
    """Run the honest decomposition over AUDIT_TASKS at the pinned budget:
        one_shot      = best-of-1 (run_benchmark)
        bestofN       = best-of-(breadth*(1+iters)) drafts, NO feedback (equal budget)
        agentic       = breadth/iters with counterexample repair
        sampling_gain = bestofN - one_shot     (pure sampling)
        feedback_gain = agentic - bestofN      (reasoning under feedback -- the headline)
    Logs one record to data/capability_audits.json. Returns the record."""
    _ensure_model_reachable(chat)
    global all_tasks
    _orig_all = all_tasks
    subset = _filter_tasks(",".join(AUDIT_TASKS))
    all_tasks = (lambda _s=subset: list(_s))  # noqa: E731  (both solvers read this module global)
    n = AUDIT_BREADTH * (1 + AUDIT_ITERS)
    try:
        if verbose:
            print(f"[audit] tasks={[t[0] for t in subset]} breadth={AUDIT_BREADTH} iters={AUDIT_ITERS} "
                  f"bestofN_n={n}", flush=True)
            print("[audit] --- one-shot (best-of-1) ---", flush=True)
        solver = _solver_from_chat(chat) if chat is not None else solve_with_pod
        one_shot, _ = run_benchmark(solver=solver, verbose=verbose)
        if verbose:
            print(f"[audit] --- best-of-{n} (no feedback, equal budget) ---", flush=True)
        bestofN, _ = run_benchmark_bestofN(n, verbose=verbose, chat=chat)
        if verbose:
            print(f"[audit] --- agentic (breadth={AUDIT_BREADTH} iters={AUDIT_ITERS}, counterexample repair) ---", flush=True)
        agentic, _, _ = run_benchmark_agentic(breadth=AUDIT_BREADTH, max_iters=AUDIT_ITERS, strategy="score",
                                              verbose=verbose, chat=chat, close_loop=(chat is None))
    finally:
        all_tasks = _orig_all
    rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "model": MODEL,
           "tasks": AUDIT_TASKS, "breadth": AUDIT_BREADTH, "iters": AUDIT_ITERS, "bestofN_n": n,
           "one_shot": round(one_shot, 3), "bestofN": round(bestofN, 3), "agentic": round(agentic, 3),
           "sampling_gain": round(bestofN - one_shot, 3), "feedback_gain": round(agentic - bestofN, 3)}
    _log_audit(rec)
    if verbose:
        print(f"\n[audit RESULT] one-shot {one_shot:.0%} -> best-of-{n} {bestofN:.0%} -> agentic {agentic:.0%}"
              f"  |  sampling_gain={rec['sampling_gain']:+.2f}  feedback_gain={rec['feedback_gain']:+.2f}", flush=True)
    return rec


SWEEP_FILE = REPO_ROOT / "data" / "capability_sweeps.json"


def run_sweep(ns=(4, 8, 16, 32), verbose=True, chat=None):
    """Best-of-N SAMPLING CURVE over the pinned audit tasks: how high does pure diverse sampling from
    the CURRENT model climb as N grows, and where does it plateau? This is the cheap lever (compute, not
    a bigger GPU): if best-of-32 hits target, a stronger base model may be unnecessary; if it plateaus
    low, a bigger model is justified. Each N is a no-feedback run (run_benchmark_bestofN). Logs a row per
    N to data/capability_sweeps.json. Returns the rows."""
    _ensure_model_reachable(chat)
    global all_tasks
    _orig_all = all_tasks
    subset = _filter_tasks(",".join(AUDIT_TASKS))
    all_tasks = (lambda _s=subset: list(_s))  # noqa: E731
    rows = []
    try:
        for n in ns:
            if verbose:
                print(f"[sweep] --- best-of-{n} (no feedback) ---", flush=True)
            score, results = run_benchmark_bestofN(n, verbose=verbose, chat=chat)
            row = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "model": MODEL, "mode": "sweep",
                   "tasks": AUDIT_TASKS, "n": n, "score": round(score, 3),
                   "passed": sum(r["passed"] for r in results), "total": len(results),
                   "calls": sum(r.get("calls", 0) for r in results)}
            rows.append(row)
            if verbose:
                print(f"[sweep] best-of-{n}: {row['passed']}/{row['total']} = {score:.0%} ({row['calls']} calls)", flush=True)
        _log_sweep(rows)
    finally:
        all_tasks = _orig_all
    if verbose and rows:
        print("\n[sweep CURVE] " + "  ".join(f"N={r['n']}:{r['score']:.0%}" for r in rows), flush=True)
    return rows


def _log_sweep(rows):
    SWEEP_FILE.parent.mkdir(parents=True, exist_ok=True)
    hist = []
    if SWEEP_FILE.exists():
        try:
            hist = json.loads(SWEEP_FILE.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist.append({"ts": _dt.datetime.now().isoformat(timespec="seconds"), "curve": rows})
    SWEEP_FILE.write_text(json.dumps(hist, indent=2), encoding="utf-8")


def _log_audit(rec):
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    hist = []
    if AUDIT_FILE.exists():
        try:
            hist = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist.append(rec)
    AUDIT_FILE.write_text(json.dumps(hist, indent=2), encoding="utf-8")


def _log(score, results, note):
    SCORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    hist = []
    if SCORES_FILE.exists():
        try:
            hist = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist.append({"ts": _dt.datetime.now().isoformat(timespec="seconds"), "model": MODEL,
                 "score": round(score, 3), "passed": sum(r["passed"] for r in results),
                 "total": len(results), "note": note,
                 "tasks": {r["task"]: r["passed"] for r in results}})
    SCORES_FILE.write_text(json.dumps(hist, indent=2), encoding="utf-8")


def guidance_line():
    """One-liner for the loop's context. Prefers the pinned audit ledger (honest decomposition) and is
    BUDGET-AWARE: it only reports a trend across audits at the SAME (tasks, breadth, iters), so a wider
    search can't fake a '+delta'. Falls back to the legacy scalar score ledger."""
    if AUDIT_FILE.exists():
        try:
            h = json.loads(AUDIT_FILE.read_text(encoding="utf-8"))
            if h:
                last = h[-1]
                trend = ""
                prev = next((r for r in reversed(h[:-1])
                             if r.get("tasks") == last.get("tasks") and r.get("breadth") == last.get("breadth")
                             and r.get("iters") == last.get("iters")), None)
                if prev:
                    d = last["feedback_gain"] - prev["feedback_gain"]
                    trend = f" ({'+' if d >= 0 else ''}{d:.2f} feedback_gain vs prev audit)"
                return (f"[capability audit] one-shot {last['one_shot']:.0%} -> best-of-{last['bestofN_n']} "
                        f"{last['bestofN']:.0%} -> agentic {last['agentic']:.0%}; "
                        f"feedback_gain={last['feedback_gain']:+.2f}{trend}")
        except Exception:
            pass
    if not SCORES_FILE.exists():
        return "[capability] no benchmark score yet -- run tools/capability_bench.py audit to establish a baseline."
    try:
        hist = json.loads(SCORES_FILE.read_text(encoding="utf-8"))
        if not hist:
            return "[capability] no scores yet."
        last = hist[-1]
        return f"[capability] latest 72b coding-benchmark score: {last['passed']}/{last['total']} = {last['score']:.0%}"
    except Exception as e:
        return f"[capability] score unavailable: {e}"


def _filter_tasks(names):
    """Restrict all_tasks() to a comma-separated name subset (for cheap targeted measurement)."""
    want = {n.strip() for n in names.split(",") if n.strip()}
    return [(n, p, v) for (n, p, v) in all_tasks() if n in want]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Capability benchmark for provable RSI.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--note", default="")
    r.add_argument("--agentic", action="store_true",
                   help="Solve each task via draft->test->repair search (the MOVABLE score), not one-shot.")
    r.add_argument("--breadth", type=int, default=None, help="Candidates per round (default: dreamed policy worker_count).")
    r.add_argument("--iters", type=int, default=3, help="Repair rounds per task (search depth).")
    r.add_argument("--tasks", default=None, help="Comma-separated task-name subset to score (default: all).")
    r.add_argument("--use-policy", action="store_true",
                   help="Agentic only: let the dreamed/authored SOLVE policy govern the live rollout (Dream-RSI section 4).")
    r.add_argument("--close-loop", action="store_true",
                   help="Agentic only: run dream_solve() after the session so the trees just logged update the solve policy.")
    au = sub.add_parser("audit", help="Pinned honest decomposition: one-shot vs equal-budget best-of-N vs agentic over a fixed subset.")
    au.add_argument("--quiet", action="store_true")
    sw = sub.add_parser("sweep", help="Best-of-N sampling curve over the audit tasks (finds the sampling ceiling of the current model).")
    sw.add_argument("--ns", default="4,8,16,32", help="Comma-separated N values (default 4,8,16,32).")
    sw.add_argument("--quiet", action="store_true")
    sub.add_parser("history")
    a = ap.parse_args()
    if a.cmd == "audit":
        print(f"Auditing {MODEL} via {POD_OLLAMA} ...", flush=True)
        rec = run_audit(verbose=not a.quiet)
        print(f"\nAUDIT: one-shot {rec['one_shot']:.0%} | best-of-{rec['bestofN_n']} {rec['bestofN']:.0%} | "
              f"agentic {rec['agentic']:.0%}  ==>  sampling_gain {rec['sampling_gain']:+.2f}, "
              f"feedback_gain {rec['feedback_gain']:+.2f}", flush=True)
        raise SystemExit(0)
    if a.cmd == "sweep":
        ns = tuple(int(x) for x in a.ns.split(",") if x.strip())
        print(f"Sweeping best-of-N {ns} for {MODEL} via {POD_OLLAMA} ...", flush=True)
        rows = run_sweep(ns=ns, verbose=not a.quiet)
        print("\nSWEEP: " + "  ".join(f"N={r['n']}:{r['score']:.0%}" for r in rows), flush=True)
        raise SystemExit(0)
    if a.cmd == "run":
        # Optional task subset: monkeypatch all_tasks so both solvers see the same filtered set.
        if a.tasks:
            _subset = _filter_tasks(a.tasks)
            all_tasks = (lambda _s=_subset: list(_s))  # noqa: E731
            print(f"(subset: {[n for n,_,_ in _subset]})", flush=True)
        mode = "AGENTIC (draft->test->repair)" if a.agentic else "one-shot"
        print(f"Scoring {MODEL} [{mode}] on {len(all_tasks())} tasks via {POD_OLLAMA} ...", flush=True)
        if a.agentic:
            score, results, _tree = run_benchmark_agentic(breadth=a.breadth, max_iters=a.iters,
                                                          use_policy=a.use_policy, close_loop=a.close_loop)
            note = (a.note or "") + f" [agentic breadth={a.breadth or 'policy'} iters<= {a.iters}" \
                                    f"{' policy' if a.use_policy else ''}]"
        else:
            score, results = run_benchmark()
            note = a.note
        _log(score, results, note)
        print(f"\nCAPABILITY SCORE: {sum(r['passed'] for r in results)}/{len(results)} = {score:.0%}", flush=True)
    elif a.cmd == "history":
        if SCORES_FILE.exists():
            for e in json.loads(SCORES_FILE.read_text(encoding="utf-8")):
                print(f"  {e['ts']}  {e['score']:.0%} ({e['passed']}/{e['total']})  {e.get('note','')}")
        else:
            print("no scores yet")
