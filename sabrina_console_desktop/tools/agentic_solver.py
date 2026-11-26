"""Agentic solver -- route a coding task through Sabrina's scaffolding (draft -> test -> repair) instead
of a single one-shot completion.

WHY THIS IS THE MISSING PIECE
-----------------------------
Every RSI round so far optimized plumbing around a FIXED-weight 72b that got exactly ONE try per
benchmark task (tools/capability_bench.solve_with_pod). So the 62% score could never move, and the
discovery trees were flat (root -> a few marked attempts). This module gives the 72b what a human
engineer has: write code, RUN the tests, read the failure, fix it, repeat. Two consequences:

  1. The capability score becomes MOVABLE on fixed weights -- lift from *reasoning under feedback*,
     which is exactly the "prove her weights are a thinking model" claim (agentic-coding, SWE-bench
     style). We can now measure raw one-shot vs scaffolded and log the delta.
  2. The search produces DEEP discovery trees: every draft and every repair is a node with a GRADED
     score s_v in [0,1]. Dream-RSI finally has real structure to navigate, and the policy's
     `worker_count` becomes the solve BREADTH (candidates per round) -- a knob that actually matters.

HONESTY / BENCHMARK INTEGRITY
-----------------------------
The solver sees only PASS/FAIL and its OWN runtime tracebacks -- never the verifier's source or the
expected outputs -- so it cannot hardcode against the hidden checks (it doesn't know them). The
verifier plays the role of the task's own test suite. Caveat inherited from the benchmark: some prompts
embed a worked example, and thin verifiers test few cases, so a determined model could in principle
overfit the disclosed example; this affects one-shot and agentic equally, so the *delta* stays a fair
measure of the scaffolding's contribution.
"""

import json
import os
import re
import signal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POD_OLLAMA = (os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",")[0]
              or "https://45m90jfro2yzjn-11434.proxy.runpod.net")
MODEL = os.environ.get("SABRINA_FORCE_LOCAL_MODEL", "qwen2.5:72b")

# Solver discovery trees live in their OWN pool, separate from improvement-session trees, so the two
# kinds of history (self-improvement vs task-solving) don't muddle each other's dreamed policy.
SOLVE_HISTORY_DIR = REPO_ROOT / "data" / "rsi_history_solve"


def _extract_code(text: str) -> str:
    """Pull the first ```python fenced block (or the whole text if unfenced)."""
    m = re.search(r"```(?:python)?\s*(.*?)```", text or "", re.DOTALL)
    return (m.group(1) if m else (text or "")).strip()


def _chat(pod, model, messages, temperature, timeout, num_ctx=8192):
    """One ollama /api/chat call. Isolated + injectable so the solve loop is testable without a pod."""
    import urllib.request
    payload = json.dumps({
        "model": model, "stream": False, "messages": messages,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }).encode()
    req = urllib.request.Request(pod.rstrip("/") + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json", "User-Agent": "Sabrina"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return (data.get("message") or {}).get("content", "")


class _Timeout(Exception):
    pass


def _run_time_limited(fn, seconds):
    """Best-effort wall-clock cap around fn() so a runaway candidate (infinite loop) can't stall the
    whole benchmark. Uses SIGALRM on Unix (the lab); a no-op elsewhere (Windows) -- there the caller
    accepts the same in-process risk capability_bench already runs with."""
    if seconds and hasattr(signal, "SIGALRM"):
        try:
            def _handler(signum, frame):
                raise _Timeout()
            old = signal.signal(signal.SIGALRM, _handler)  # raises ValueError off the main thread
        except (ValueError, OSError):
            return fn()  # can't install the alarm here (non-main thread) -> run without the cap
        signal.alarm(int(seconds))
        try:
            return fn()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old)
    return fn()


def grade(code: str, verify_fn, probe_fn=None, exec_timeout=8):
    """Grade one candidate solution against the task's hidden verifier. Returns (score, feedback).

    The score is GRADED (not just pass/fail) so the repair loop and the discovery tree get real signal
    about how far a candidate got:

        1.00  verifier passes                          -> solved
        0.60  defines fn, verifier runs but is falsy   -> wrong output, but the code runs cleanly
        0.30  defines fn, verifier RAISED when called  -> runtime error in the produced function
        0.00  code doesn't load / exec raised          -> syntax or import error

    Semantics match capability_bench.run_benchmark's authoritative `bool(verify(ns))` (a verifier that
    returns None is treated as falsy = 0.60, exactly as the real scorer would score it), so a candidate
    the solver believes it "solved" will also pass the benchmark's own check."""
    def _load():
        ns = {}
        exec(code, ns)
        return ns
    try:
        ns = _run_time_limited(_load, exec_timeout)
    except _Timeout:
        return 0.0, "code did not finish loading within the time limit (possible infinite loop at import)"
    except Exception as e:
        return 0.0, f"code failed to load: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
    try:
        ok = _run_time_limited(lambda: bool(verify_fn(ns)), exec_timeout)
    except _Timeout:
        return 0.30, "the function ran too long on a hidden check (possible infinite loop / bad complexity)"
    except Exception as e:
        return 0.30, f"the function raised on a hidden check: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
    if ok:
        return 1.0, "solved"
    # Wrong output. If the task supplies a probe, surface a CONCRETE counterexample (a failing input and
    # the candidate's OWN output) so the repair step can reason about the specific defect instead of
    # resampling blindly. The probe never reveals the EXPECTED answer, so it can't be hardcoded against.
    feedback = "the function runs but returns the wrong result on a hidden check"
    if probe_fn is not None:
        try:
            hint = _run_time_limited(lambda: probe_fn(ns), exec_timeout)
            if hint:
                feedback = str(hint)[:400]
        except Exception:
            pass
    return 0.60, feedback


def _repair_prompt(spec: str, code: str, feedback: str) -> str:
    return (
        "Your Python solution is not yet correct. Debug it methodically.\n\n"
        f"TASK:\n{spec}\n\n"
        f"YOUR CURRENT CODE:\n```python\n{code}\n```\n\n"
        f"WHAT WENT WRONG WHEN IT WAS TESTED:\n{feedback}\n\n"
        "Work through this before rewriting:\n"
        "1. Re-read the TASK and list EVERY requirement and edge case (empty input, ties, boundaries, "
        "duplicates, negative/large values, ordering, off-by-one).\n"
        "2. If a specific failing input is given above, trace your code on THAT input line by line and "
        "find where its behavior diverges from the requirement. Otherwise trace the trickiest edge case.\n"
        "3. Fix the root cause (do not merely special-case the example).\n"
        "Respond with ONLY the corrected ```python code block defining the same function. No prose."
    )


class _Shim:
    """A minimal selection-node used ONLY on the policy path when tree=None (no live SessionTree, e.g.
    unit tests / best-of-N). Carries the .score / .children / .parent / .workspace_snapshot a
    CandidatePolicy.select_batch (and an authored select() walking .children/.parent) reads, so the
    policy runs identically with or without a live discovery tree."""
    __slots__ = ("score", "children", "parent", "workspace_snapshot")

    def __init__(self, score, action, parent=None):
        self.score = float(score)
        self.children = []
        self.parent = parent
        self.workspace_snapshot = {"action": action, "kind": "attempt"}
        if parent is not None:
            parent.children.append(self)


def solve_agentic(spec, verify_fn, *, breadth=1, max_iters=4, strategy="score",
                  model=MODEL, pod=POD_OLLAMA, temperature=0.2, timeout=180,
                  tree=None, parent=None, task_name="task", chat=_chat, log=print, probe_fn=None,
                  policy=None):
    """Draft -> test -> repair search for ONE task.

    breadth   : candidates generated per round (Dream-RSI worker_count -- search width).
    max_iters : repair rounds after the initial drafts (search depth).
    strategy  : 'score' repairs the best partial candidate(s) (exploit); anything else spreads across
                the frontier (explore). Mirrors CandidatePolicy so the DREAMED policy governs the search.
    policy    : OPT-IN. A CandidatePolicy (rsi_improve). When set, its select_batch() -- including any
                LLM-authored select_code -- chooses which nodes to expand each round over a WIDENED
                feasible set (all un-expanded, unsolved draft/repair leaves), so the DREAMED/AUTHORED
                policy actually governs the live rollout (Dream-RSI paper section 4). `strategy` is then
                ignored (the policy carries its own). When None, the search is byte-for-byte the legacy
                fixed-frontier behavior -- so the pinned audit is unchanged. Efficiency bound with a
                policy: calls <= breadth + max_iters*worker_count.
    tree/parent: optional rsi_history.SessionTree -- every draft/repair is logged as a graded node so
                the search becomes replay history for dream().

    Returns {solved, best_code, best_score, best_feedback, iters, calls}."""
    best_code, best_score, best_fb = "", -1.0, ""
    calls = 0
    frontier = []  # list of dicts: {score, code, fb, node}

    # --- Round 0: `breadth` independent drafts (temperature spread => diverse candidates) ---
    for b in range(max(1, breadth)):
        temp = min(1.0, temperature + 0.15 * b)
        raw = chat(pod, model, [{"role": "user", "content": spec}], temp, timeout)
        calls += 1
        code = _extract_code(raw)
        score, fb = grade(code, verify_fn, probe_fn)
        node = tree.add_attempt(parent, task_name, "draft", code, score) if tree is not None else None
        frontier.append({"score": score, "code": code, "fb": fb, "node": node})
        if score > best_score:
            best_code, best_score, best_fb = code, score, fb
        if log:
            log(f"    [solve:{task_name}] draft {b+1}/{breadth} -> s_v={score:.2f} ({fb[:60]})")
        if best_score >= 1.0:
            break

    # --- Repair iterations: chain fixes under the partial candidates (grows tree DEPTH) ---
    if policy is None:
        # LEGACY path (policy=None): byte-for-byte the pre-Round-7 fixed-frontier search, so the pinned
        # audit and every existing caller are unchanged.
        it = 0
        while best_score < 1.0 and it < max_iters:
            it += 1
            frontier.sort(key=lambda d: d["score"], reverse=True)
            seeds = frontier[:max(1, breadth)] if strategy == "score" else frontier[-max(1, breadth):]
            new_frontier = []
            for si, seed in enumerate(seeds):
                # Repair hotter than drafting (and spread across seeds) so a repair explores a genuinely
                # different fix instead of resampling the same wrong code.
                rtemp = min(1.0, temperature + 0.3 + 0.1 * si)
                raw = chat(pod, model,
                           [{"role": "user", "content": _repair_prompt(spec, seed["code"], seed["fb"])}],
                           rtemp, timeout)
                calls += 1
                ncode = _extract_code(raw)
                nscore, nfb = grade(ncode, verify_fn, probe_fn)
                nnode = (tree.add_attempt(seed["node"], task_name, "repair", ncode, nscore)
                         if tree is not None else None)
                new_frontier.append({"score": nscore, "code": ncode, "fb": nfb, "node": nnode})
                if nscore > best_score:
                    best_code, best_score, best_fb = ncode, nscore, nfb
                if log:
                    log(f"    [solve:{task_name}] repair it{it} -> s_v={nscore:.2f} ({nfb[:60]})")
                if nscore >= 1.0:
                    break
            # Keep the better of {this round, previous frontier} so a bad repair round can't lose progress.
            frontier = sorted(new_frontier + frontier, key=lambda d: d["score"], reverse=True)[:max(1, breadth)]
    else:
        # POLICY-DRIVEN path (Dream-RSI section 4): the policy's select_batch() -- possibly LLM-authored --
        # chooses which of ALL un-expanded, unsolved leaves to expand each round. The widened feasible set
        # (not a pruned top-breadth) is what gives the policy a real explore/exploit lever, so a better
        # policy reaches s_v=1.0 in fewer calls (the efficiency win the paper's objective rewards).
        wc = max(1, getattr(policy, "worker_count", breadth))
        info = {}       # id(sel_node) -> {"code","fb","node"} -- repair payload (nodes are lossy: never
                        # recover code/fb from a node's truncated workspace_snapshot)
        feasible = []   # selection-nodes (live Node or _Shim) with score < 1.0 not yet expanded
        for d in frontier:
            sel = d["node"] if d["node"] is not None else _Shim(d["score"], "draft")
            info[id(sel)] = {"code": d["code"], "fb": d["fb"], "node": d["node"]}
            if d["score"] < 1.0:
                feasible.append(sel)
        it = 0
        while best_score < 1.0 and it < max_iters and feasible:
            it += 1
            batch = policy.select_batch(feasible)          # <= worker_count nodes
            if not batch:
                break
            for sel in batch:
                rec = info.get(id(sel))
                if rec is None or sel not in feasible:     # foreign / already-expanded / solved -> skip
                    continue
                feasible.remove(sel)                       # expand-once
                rtemp = min(1.0, temperature + 0.3)
                raw = chat(pod, model,
                           [{"role": "user", "content": _repair_prompt(spec, rec["code"], rec["fb"])}],
                           rtemp, timeout)
                calls += 1
                ncode = _extract_code(raw)
                nscore, nfb = grade(ncode, verify_fn, probe_fn)
                nnode = (tree.add_attempt(rec["node"], task_name, "repair", ncode, nscore)
                         if tree is not None else None)
                child = nnode if nnode is not None else _Shim(nscore, "repair", parent=sel)
                info[id(child)] = {"code": ncode, "fb": nfb, "node": nnode}
                if nscore < 1.0:
                    feasible.append(child)                 # child becomes newly-feasible -> depth grows
                if nscore > best_score:
                    best_code, best_score, best_fb = ncode, nscore, nfb
                if log:
                    log(f"    [solve:{task_name}] repair it{it} (policy) -> s_v={nscore:.2f} ({nfb[:60]})")
                if nscore >= 1.0:
                    break

    return {"solved": best_score >= 1.0, "best_code": best_code, "best_score": best_score,
            "best_feedback": best_fb, "iters": it, "calls": calls}


if __name__ == "__main__":
    # Self-test the SEARCH MACHINERY with a mock model (no pod): first draft is wrong, the repair fixes
    # it. Proves grading, the repair loop, and tree depth without spending a single pod call.
    import sys
    sys.path.insert(0, str(REPO_ROOT))
    from tools.rsi_history import SessionTree

    spec = "Write add(a,b)."
    verify = lambda ns: ns["add"](2, 3) == 5

    scripted = iter([
        "```python\ndef add(a, b):\n    return a - b\n```",   # wrong -> s_v 0.60
        "```python\ndef add(a, b):\n    return a + b\n```",   # correct -> s_v 1.0
    ])
    def mock_chat(pod, model, messages, temperature, timeout, num_ctx=8192):
        return next(scripted)

    st = SessionTree(root_label="solve-selftest")
    res = solve_agentic(spec, verify, breadth=1, max_iters=2, tree=st, chat=mock_chat, log=print)
    print("result:", {k: res[k] for k in ("solved", "best_score", "iters", "calls")})
    # tree depth: root -> draft -> repair  == 2 edges below root
    def depth(n):
        return 0 if not n.children else 1 + max(depth(c) for c in n.children)
    d = depth(st.tree.root)
    print("tree depth below root:", d)
    assert res["solved"] and res["best_score"] == 1.0, "repair loop failed to reach a correct solution"
    assert d == 2, f"expected a deep tree (draft->repair), got depth {d}"
    print("SELFTEST OK: agentic search machinery works (draft wrong -> repair correct, deep tree logged)")
