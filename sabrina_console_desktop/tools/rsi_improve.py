"""Dream-RSI phase ❸: the "dream" / policy-improvement step, wired to REAL history.

Loads the replay-simulator pool (recorded discovery trees from tools/rsi_history), scores the current
selection policy π⁰ plus a set of candidate policies by REPLAYING them over that history
(tools/dream_rsi/replay.OfflineReplayEvaluation -- zero new execution), and deploys the best. Because
π⁰ is always in the candidate set and we require V(m*) >= V(0), the deployed policy can NEVER be worse
than the current one -- the paper's core safety guarantee.

The policy here is Sabrina's TARGET/ACTION SELECTION policy (which node to expand next, how wide) --
the direct analog of the paper's Batch-Selection policy. Candidates are a parameterized family for
now (worker width x ranking strategy); a later step can have the 72b write open-ended candidates.

    python tools/rsi_improve.py dream        # score candidates over real history, deploy best
    python tools/rsi_improve.py show         # print current deployed policy + last dream result
"""

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = REPO_ROOT / "data" / "rsi_policy"
CURRENT = POLICY_DIR / "current.json"
LOG = POLICY_DIR / "dream_log.json"

sys.path.insert(0, str(REPO_ROOT))
# The dream_rsi modules cross-import each other with bare names (`from tree import ...`) so they run as
# scripts; put their dir on sys.path so those imports resolve when we import them as a library here.
sys.path.insert(0, str(REPO_ROOT / "tools" / "dream_rsi"))
from tools.rsi_history import load_pool  # noqa: E402
# NOTE: `from replay import OfflineReplayEvaluation` is imported LAZILY inside dream() only. That keeps
# `import tools.rsi_improve` cheap and dependency-light for the ONLINE path (improvement_tracker loads
# CandidatePolicy every round to rank the queue); it must not drag in the whole replay stack.


def _walk_nodes(node):
    """Yield a node and all its descendants (dream_rsi tree.Node has .children)."""
    yield node
    for child in getattr(node, "children", None) or []:
        yield from _walk_nodes(child)


def action_values_from_pool(pool=None):
    """Mean realized s_v per action-type over EVERY recorded attempt in the shared discovery-tree pool.
    This is the ONLINE value estimator: an un-attempted candidate has no realized score yet, so we
    predict its value from how that action-type has historically paid off. Same quantity the offline
    replay reads directly from a revealed node's .score -- see CandidatePolicy.node_value."""
    if pool is None:
        pool = load_pool()
    sums, counts = {}, {}
    for tree in pool:
        for node in _walk_nodes(tree.root):
            snap = getattr(node, "workspace_snapshot", None) or {}
            if snap.get("kind") != "attempt":
                continue
            act = snap.get("action")
            if not act:
                continue
            try:
                s = float(getattr(node, "score", 0.0) or 0.0)
            except (TypeError, ValueError):
                s = 0.0
            sums[act] = sums.get(act, 0.0) + s
            counts[act] = counts.get(act, 0) + 1
    return {a: sums[a] / counts[a] for a in sums if counts[a]}


class _OnlineCandidate:
    """Adapter so an un-attempted queue entry (a dict) looks like a dream_rsi tree Node to node_value:
    it has NO realized score (score=None) and carries the entry's action. This lets the SAME value
    function rank the online queue and the offline replay tree -- the genuine online<->offline unification."""
    __slots__ = ("score", "workspace_snapshot")

    def __init__(self, entry):
        self.score = None  # not yet attempted -> no realized s_v
        self.workspace_snapshot = {"action": entry.get("action")}


# Restricted builtins for LLM-authored select() bodies: pure list/number helpers only, NO __import__,
# open, eval, exec, etc. An authored policy is a ranking function over already-revealed nodes -- it
# needs nothing more. This is a research harness running the user's own model on the lab, not an
# untrusted-input sandbox; the guard keeps an authored policy from doing I/O or imports, and any error
# (including a name it can't resolve) falls back to the built-in strategy.
_SAFE_BUILTINS = {n: getattr(__builtins__, n, None) if not isinstance(__builtins__, dict)
                  else __builtins__.get(n)
                  for n in ("sorted", "list", "len", "max", "min", "abs", "range", "enumerate",
                            "sum", "float", "int", "bool", "reversed", "filter", "map", "dict",
                            "set", "tuple", "zip", "any", "all", "round", "getattr")}


def _run_authored_select(code, feasible, worker_count):
    """Execute an LLM-authored `def select(feasible, worker_count): ...` in a restricted namespace and
    return a validated batch: a de-duplicated list of nodes drawn from `feasible`, length <= worker_count.
    Returns None on ANY failure (no select defined, exception, bad output) so select_batch falls back to
    the built-in strategy -- so a malformed authored policy is scored exactly as baseline, never better,
    and can never crash the dream. An empty list is a valid 'stop early' decision and is returned as-is."""
    try:
        ns = {"__builtins__": _SAFE_BUILTINS}
        exec(code, ns)                       # noqa: S102 -- restricted builtins, lab-local model
        fn = ns.get("select")
        if not callable(fn):
            return None
        out = fn(list(feasible), int(worker_count))
        if out is None:
            return None
        fids = {id(n) for n in feasible}
        picked, seen = [], set()
        for n in out:
            if id(n) in fids and id(n) not in seen:
                seen.add(id(n))
                picked.append(n)
                if len(picked) >= worker_count:
                    break
        return picked
    except Exception:
        return None


class CandidatePolicy:
    """The ONE selection policy, used identically online and offline (Round-5 bridge):

      * offline (dream)  -- select_batch(feasible_nodes) picks up to `worker_count` nodes to expand,
        exactly what tools/dream_rsi/replay.OfflineReplayEvaluation expects.
      * online (swarm)   -- rank_targets(queue_entries) orders improvement_tracker's candidate queue.

    Both consult the SAME value function node_value() and the SAME `strategy`/`worker_count`, so the
    policy dream ships in data/rsi_policy/current.json is literally the policy the swarm then uses.

    `strategy`: 'score' (exploit highest-value first) or 'fifo' (breadth-first / oldest-first).
    `worker_count`: how many nodes to expand per round / suggested swarm width.
    `action_values`: {action -> mean s_v} learned from the pool (the online value estimator)."""

    def __init__(self, worker_count=2, strategy="score", action_values=None,
                 select_code=None, name=None):
        self.worker_count = max(1, int(worker_count))
        self.strategy = strategy
        self.action_values = dict(action_values or {})
        # select_code: an OPTIONAL LLM-authored `select(feasible, worker_count)` body (paper §8's
        # "policy-development step revises the policy code"). When present it drives select_batch; when
        # absent the policy is the built-in score/fifo family. name: a short label for logging which
        # authored candidate won.
        self.select_code = select_code or None
        self.name = name or (f"{strategy}-w{self.worker_count}" if not select_code else "authored")

    def node_value(self, node):
        """Value estimate for a node/candidate. Uses the realized score s_v when the node has one
        (offline replay: revealed recorded attempts always carry a float .score), else falls back to
        the learned per-action mean (online: candidates aren't attempted yet). The single shared value
        function behind both select_batch and rank_targets."""
        s = getattr(node, "score", None)
        if s is not None:
            try:
                return float(s)
            except (TypeError, ValueError):
                pass
        snap = getattr(node, "workspace_snapshot", None) or {}
        return float(self.action_values.get(snap.get("action"), 0.0))

    def select_batch(self, feasible):
        """OFFLINE (replay): pick up to worker_count feasible nodes to expand this round.

        If this policy carries LLM-authored select_code (paper §8), that governs selection -- an
        open-ended node-prioritization strategy the 72b wrote, scored against the built-in family by
        replay so dream can only ship it if it's not worse. On any authoring error we fall back to the
        built-in strategy, so a broken authored policy is scored as (not better than) baseline and can
        never crash the dream or win. Built-in: 'score' expands highest-value first (by realized s_v);
        'fifo' keeps discovery order."""
        if self.select_code:
            picked = _run_authored_select(self.select_code, feasible, self.worker_count)
            if picked is not None:
                return picked
        nodes = list(feasible)
        if self.strategy == "score":
            nodes.sort(key=self.node_value, reverse=True)
        return nodes[: self.worker_count]

    def rank_targets(self, entries):
        """ONLINE (swarm): order improvement_tracker's candidate queue by this policy BEFORE it is
        worker-sliced. 'score' -> by learned per-action value (desc), tie-broken least-recently-improved
        first; 'fifo' -> least-recently-improved first (the pre-Round-5 default). Deterministic (stable
        sort over a total key) so every worker computes the identical order and their disjoint slices
        still partition the queue exactly once."""
        items = list(entries)
        if self.strategy != "score":
            return sorted(items, key=lambda e: e.get("last_improved_ts", 0))
        return sorted(items, key=lambda e: (-self.node_value(_OnlineCandidate(e)),
                                            e.get("last_improved_ts", 0)))

    def as_dict(self):
        d = {"worker_count": self.worker_count, "strategy": self.strategy,
             "action_values": self.action_values, "name": self.name}
        if self.select_code:
            d["select_code"] = self.select_code
        return d

    @classmethod
    def load(cls, path=None):
        """Load a deployed policy from `path` (default data/rsi_policy/current.json = the improvement
        policy). Pass SOLVE_CURRENT to load the solve policy. Returns the default π⁰ when the file is
        absent/unreadable, so callers always get a usable policy -- callers degrade gracefully, never crash."""
        p = Path(path) if path else CURRENT
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                return cls(d.get("worker_count", 2), d.get("strategy", "score"), d.get("action_values"),
                           select_code=d.get("select_code"), name=d.get("name"))
            except Exception:
                pass
        return cls(2, "score", {})   # default π⁰


def _current_policy():
    return CandidatePolicy.load()


def _candidate_set(pi0, action_values):
    """π⁰ FIRST (so we can never ship worse), then the parameterized family. All candidates share the
    pool-derived action_values (they are a property of the observed history, not a per-candidate choice)."""
    cands = [pi0]
    seen = {(pi0.worker_count, pi0.strategy)}
    for w in (1, 2, 3):
        for s in ("score", "fifo"):
            if (w, s) not in seen:
                seen.add((w, s))
                cands.append(CandidatePolicy(w, s, action_values))
    return cands


_AUTHOR_PROMPT = """You are improving the BATCH-SELECTION POLICY of a Dream-RSI recursive-self-improvement loop.

A discovery tree records attempts to solve a task. Replay walks a FROZEN tree, revealing recorded
children one round at a time (zero execution cost). Each round your policy chooses which of the
currently-expandable nodes to expand next.

Your job: write a Python function

    def select(feasible, worker_count):
        # feasible: list of already-revealed nodes, each with:
        #     n.score   -> float, realized quality of that recorded attempt (higher is better)
        #     n.children-> list of child nodes
        #     n.parent  -> parent node or None
        # return: a list of AT MOST worker_count nodes chosen from `feasible` to expand this round.
        ...

The replay objective you are being optimized for is:

    V = max_score_reached  -  0.1 * total_reveals  +  0.1 * (total_reveals / rounds_used)

So a GOOD policy reaches high-scoring nodes with FEWER total reveals (cheap) and expands several nodes
per round when worthwhile (parallelism). Think about explore-vs-exploit: expanding children of an
already-high-scoring node vs. spreading reveals across the frontier.

Constraints: pure Python only, NO imports, no I/O. Use only builtins (sorted, max, min, len, etc.).
Return a list (may be empty to stop early). Keep each function self-contained.

Pool context (mean realized score per action-type observed so far): {action_values}
Average revealed nodes per tree in the pool: {avg_nodes}

Write {n} DISTINCT strategies. Output each as its own ```python code block containing a complete
`def select(feasible, worker_count):`. Make them genuinely different approaches. No prose needed."""


def _parse_select_bodies(text):
    """Pull each complete `def select(...)` from the author's reply -- from fenced ```python blocks when
    present, else by scanning for top-level `def select`. Returns a list of source strings."""
    import re
    bodies = []
    blocks = re.findall(r"```(?:python)?\s*(.*?)```", text or "", re.DOTALL)
    candidates = blocks if blocks else ([text] if text else [])
    for block in candidates:
        # A block may hold >1 def select; split on top-level 'def select' boundaries.
        parts = re.split(r"(?m)^(?=def select\b)", block)
        for part in parts:
            if re.search(r"(?m)^def select\b", part):
                bodies.append(part.strip("\n"))
    return bodies


def _authored_candidates(pi0, pool, author, n=6, max_rounds=8):
    """Paper section 8, done for real: ask the author callable (the 72b) to WRITE n candidate select()
    policies, wrap each valid one as a CandidatePolicy carrying select_code. Returns [] on any failure
    -- dream then just scores the built-in grid, so this is strictly additive and never breaks dream."""
    if author is None:
        return []
    try:
        avg_nodes = 0.0
        if pool:
            tot = sum(sum(1 for _ in _walk_nodes(t.root)) for t in pool)
            avg_nodes = round(tot / len(pool), 1)
        prompt = _AUTHOR_PROMPT.format(action_values={k: round(v, 3) for k, v in (pi0.action_values or {}).items()},
                                       avg_nodes=avg_nodes, n=n)
        reply = author(prompt)
    except Exception:
        return []
    cands = []
    for i, body in enumerate(_parse_select_bodies(reply)):
        # Validate it defines a callable select() before trusting it; a body that won't even define
        # select is dropped here rather than silently becoming a fallback-to-baseline candidate.
        if _run_authored_select(body, [], 1) is None and "def select" not in body:
            continue
        cands.append(CandidatePolicy(pi0.worker_count, pi0.strategy, pi0.action_values,
                                     select_code=body, name=f"authored-{i + 1}"))
    return cands


def dream(max_rounds=8, history_dir=None, current_file=None, log_file=None, author=None, n_authored=6):
    """Dream a better selection policy over a discovery-tree pool and ship it, never worse than π⁰.

    history_dir : which pool to dream over (default data/rsi_history = the improvement pool; pass
                  SOLVE_HISTORY_DIR for the solve pool).
    current_file: where to read π⁰ from and write the shipped policy (default CURRENT = current.json).
    log_file    : where to append the dream record (default LOG).
    Parameterized so the SOLVE loop (dream_solve) can dream over its own pool into its own policy file
    without touching the improvement policy."""
    from replay import OfflineReplayEvaluation  # lazy (resolves via the dream_rsi dir on sys.path)
    target_current = Path(current_file) if current_file else CURRENT
    target_log = Path(log_file) if log_file else LOG
    pool = load_pool(history_dir)
    if not pool:
        where = str(history_dir) if history_dir else "data/rsi_history"
        return {"status": "no_history",
                "detail": f"{where} is empty -- run a session first so there are real discovery trees "
                          f"to dream over."}
    # Per-action value table over the WHOLE pool (all workers' trees). Shared by every candidate and
    # baked into the shipped policy so the online queue ranker can read it without touching the pool.
    action_values = action_values_from_pool(pool)
    cur = CandidatePolicy.load(target_current)
    pi0 = CandidatePolicy(cur.worker_count, cur.strategy, action_values,
                          select_code=cur.select_code, name=cur.name)  # π⁰ with fresh pool stats
    cands = _candidate_set(pi0, action_values)
    # Paper §8: if an author (the 72b) is supplied, add LLM-WRITTEN select() policies to the candidate
    # set. Never-worse selection below still guarantees we only ship one if it beats π⁰.
    authored = _authored_candidates(pi0, pool, author, n_authored, max_rounds)
    cands += authored
    scored = []
    for i, pol in enumerate(cands):
        try:
            v = OfflineReplayEvaluation(pol, pool, max_rounds).evaluate()
        except Exception as e:
            v = float("-inf"); pol._err = str(e)[:80]
        scored.append((v, i, pol))
    v0 = scored[0][0]                                   # π⁰ is index 0
    best_v, best_i, best_pol = max(scored, key=lambda t: t[0])
    # Guarantee: only switch if strictly-not-worse than π⁰ (ties keep π⁰).
    if best_v <= v0:
        best_v, best_i, best_pol = v0, 0, pi0
    target_current.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (temp + os.replace) so a worker can never read a half-written policy file -- a torn
    # read would parse-fail, silently fall back to default-empty action_values, and desync that worker's
    # queue ordering from its siblings.
    _tmp = target_current.with_suffix(".json.tmp")
    _tmp.write_text(json.dumps(best_pol.as_dict(), indent=2), encoding="utf-8")
    os.replace(_tmp, target_current)
    # Efficiency comparison (the paper's actual win): reach the same quality with fewer reveals.
    # Detailed replay of π⁰ vs the shipped policy makes it visible in the log.
    base_eff = OfflineReplayEvaluation(pi0, pool, max_rounds).evaluate_detailed()
    ship_eff = OfflineReplayEvaluation(best_pol, pool, max_rounds).evaluate_detailed()
    record = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "pool_trees": len(pool),
        "action_values": {a: round(v, 4) for a, v in action_values.items()},
        "V0": round(v0, 4), "V_selected": round(best_v, 4),
        "selected": {"worker_count": best_pol.worker_count, "strategy": best_pol.strategy,
                     "name": best_pol.name, "authored": bool(best_pol.select_code)},
        "kept_current": best_i == 0,
        "n_authored_candidates": len(authored),
        # Efficiency: mean best-score reached, mean reveals (cost), mean rounds -- baseline vs shipped.
        # A real Dream-RSI win shows the SAME max_score at FEWER reveals.
        "efficiency": {
            "baseline": {k: round(base_eff[k], 3) for k in ("max_score", "reveals", "rounds")},
            "shipped": {k: round(ship_eff[k], 3) for k in ("max_score", "reveals", "rounds")},
            "reveals_saved": round(base_eff["reveals"] - ship_eff["reveals"], 3),
        },
        # action_values are identical across candidates (pool stats), so trim them from the per-candidate
        # log rows -- they're recorded once above.
        "candidates": [{"name": p.name, "authored": bool(p.select_code),
                        "policy": {"worker_count": p.worker_count, "strategy": p.strategy},
                        "V": (None if v == float("-inf") else round(v, 4))}
                       for v, _i, p in scored],
    }
    hist = []
    if target_log.exists():
        try:
            hist = json.loads(target_log.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    hist.append(record)
    target_log.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    return {"status": "ok", **record}


# --- Solve-side policy: a SEPARATE pool + policy file so the task-solving loop and the codebase-
# improvement loop never muddle each other's dreamed policy. -----------------------------------------
SOLVE_CURRENT = POLICY_DIR / "solve_current.json"
SOLVE_LOG = POLICY_DIR / "solve_dream_log.json"


def dream_solve(max_rounds=8, author=None, n_authored=6):
    """Dream a SOLVE policy over the agentic solver's discovery-tree pool (data/rsi_history_solve) and
    ship it to solve_current.json. This is the READ half that was missing: the deep draft->repair trees
    the solver logs now actually feed a policy (worker_count = solve breadth, strategy) that the next
    agentic run consumes -- closing the solve loop, read-only w.r.t. the 72b's weights.

    Pass author (a callable(prompt)->str, e.g. the 72b) to have the model WRITE candidate select()
    policies (paper §8); omit it to score only the built-in grid."""
    from tools.agentic_solver import SOLVE_HISTORY_DIR
    return dream(max_rounds=max_rounds, history_dir=SOLVE_HISTORY_DIR,
                 current_file=SOLVE_CURRENT, log_file=SOLVE_LOG,
                 author=author, n_authored=n_authored)


def pod_author(prompt, timeout=240):
    """A minimal author callable: send `prompt` to the pod 72b (ollama /api/chat) and return its text.
    Reads SABRINA_RUNPOD_OLLAMA_URLS (first url) + the model name, same as capability_bench. Used so
    `dream --author` can have the 72b WRITE candidate select() policies (paper §8)."""
    import urllib.request
    url = (os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",")[0]
           or "https://oc28k8qtvpojnf-11434.proxy.runpod.net").rstrip("/")
    model = os.environ.get("SABRINA_RSI_MODEL", "qwen2.5:72b")
    payload = json.dumps({"model": model, "stream": False,
                          "messages": [{"role": "user", "content": prompt}],
                          "options": {"temperature": 0.7, "num_ctx": 8192}}).encode()
    req = urllib.request.Request(url + "/api/chat", data=payload,
                                 headers={"Content-Type": "application/json", "User-Agent": "Sabrina-RSI"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (json.load(r).get("message") or {}).get("content", "")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Dream-RSI policy-improvement step over real history.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dream")
    d.add_argument("--max-rounds", type=int, default=8)
    d.add_argument("--solve", action="store_true", help="Dream the SOLVE policy over data/rsi_history_solve -> solve_current.json.")
    d.add_argument("--author", action="store_true", help="Have the 72b WRITE candidate select() policies (paper §8), not just the built-in grid.")
    d.add_argument("--n-authored", type=int, default=6, help="How many authored candidates to request.")
    sub.add_parser("show")
    a = ap.parse_args()
    if a.cmd == "dream":
        import pprint
        author = pod_author if a.author else None
        if a.solve:
            pprint.pprint(dream_solve(a.max_rounds, author=author, n_authored=a.n_authored))
        else:
            pprint.pprint(dream(a.max_rounds, author=author, n_authored=a.n_authored))
    else:
        print("improvement policy:", CandidatePolicy.load().as_dict())
        print("solve policy:", CandidatePolicy.load(SOLVE_CURRENT).as_dict())
        if LOG.exists():
            last = json.loads(LOG.read_text(encoding="utf-8"))[-1]
            print("last improvement dream:", {k: last[k] for k in ("ts", "V0", "V_selected", "selected", "kept_current")})
