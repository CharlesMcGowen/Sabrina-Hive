"""Codebase-improvement target queue + ledger for the standing self-improvement loop.

Mirrors tools/qa_tracker.py: a durable JSON ledger (data/improvement_ledger.json) of when each
target was last worked, so the loop pulls the least-recently-improved target first and never
repeats work across sessions/restarts.

CRITICAL GUARDRAIL: this repo is 6.7 GB and full of duplicate/generated/vendored trees
(.sandbox_clone_*, Sabrina-dev/, scratch/, security_items/, data/, models/, logs/, ...). Targets
are drawn ONLY from an allow-list of real source dirs MINUS a deny-list of those trees and the
protected orchestration core -- otherwise the loop would waste cycles "improving" a stale fork or a
vendored library (a bug the repo's own mcp_code_improvement.py has: it rglob's *.py with no
exclusions). `is_in_scope()` is the single most important safety check here.
"""

import os
import json
import time
import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
LEDGER_FILE = DATA_DIR / "improvement_ledger.json"

# Real source roots the loop may work in (top-level segment, relative to repo root).
ALLOW_ROOTS = ("tools", "ai_systems", "mcp_server", "api", "tests")

# Any path whose FIRST segment is one of these, or that contains a ".sandbox_clone_" segment, is out
# of scope (duplicate / generated / vendored / noise). Kept as first-segment matches so a legit file
# is never excluded for coincidentally containing a word.
DENY_FIRST_SEGMENTS = {
    "sabrina-dev", "github_variant", "archives", "scratch", "dev",
    "security_items", "models", "data", "logs", "docs",
    "__pycache__", ".venv", ".git", "node_modules",
}

# Orchestration core + finished modules the loop must NOT modify. Mirror of the loop's own
# PROTECTED_PATHS + _FINISHED_MODULE_PATHS (duplicated here to avoid importing the loop, which would
# be circular). The loop's write_file/edit_file enforce the real block; this just keeps the queue
# from ever SUGGESTING one.
# Recovery layer -- NEVER a target, even in core mode (mirrors the loop's RECOVERY_LAYER).
RECOVERY_RELATIVE = {
    "tools/sabrina_supervisor.py", "tools/system_checkpoint.py", "tools/pod_control.py",
    "tools/run_improvement_session.py", "tools/improvement_tracker.py",
}
# Sabrina's brain -- a target ONLY in core mode (SABRINA_CORE_MODE=1); mirrors the loop's CORE_PATHS.
CORE_RELATIVE = {
    "sabrina_gemini_loop.py",
    "tools/oracle_providers.py", "tools/token_utils.py", "tools/agent_skills.py",
    "tools/blackboard.py", "tools/rag_knowledge.py", "tools/lab_knowledge.py",
    "tools/model_assignment_api.py",
    "ai_systems/browser_utils.py", "ai_systems/training/probe_api.py",
}
# Finished dream_rsi deliverables -- never a target.
FINISHED_RELATIVE = {
    "tools/dream_rsi/tree.py", "tools/dream_rsi/replay.py", "tools/dream_rsi/policy.py",
    "tools/dream_rsi/simulator.py", "tools/dream_rsi/rollout.py", "tools/dream_rsi/improve.py",
}
PROTECTED_RELATIVE = RECOVERY_RELATIVE | CORE_RELATIVE | FINISHED_RELATIVE  # protected in a normal run

# Curated seed backlog: safe, high-value first-session targets (per the approved plan). Each 'target'
# is the WRITABLE in-scope file the loop creates/edits; 'source' is the (possibly read-only) module
# it characterizes. Adding tests never edits the source, so "add tests for a protected module" is
# safe -- the target is a new file under tests/.
SEED_TARGETS = [
    {"target": "tests/unit/test_replay_simulator.py", "action": "add_tests",
     "source": "tools/replay_simulator.py",
     "why": "replay_simulator is wired into the loop's read_environment() every round but has no tests"},
    {"target": "tests/unit/test_oracle_providers.py", "action": "add_tests",
     "source": "tools/oracle_providers.py",
     "why": "core routing hub, untested; characterization tests de-risk any future change"},
    {"target": "tests/unit/test_sabrina_supervisor.py", "action": "add_tests",
     "source": "tools/sabrina_supervisor.py",
     "why": "process supervisor lifecycle logic, untested"},
    {"target": "tests/unit/test_improvement_tracker.py", "action": "add_tests",
     "source": "tools/improvement_tracker.py",
     "why": "the new improvement queue itself should have tests (scope filter especially)"},
    {"target": "tools/engines/sqli_engine.py", "action": "retrofit_engagement_gate",
     "source": "tools/engines/sqli_engine.py",
     "why": "AGENTS.md: the original engines predate EngagementGate and need the scope-gate retrofit"},
    # More add_tests targets (untested, mostly-pure modules) so a swarm has runway. The 'source' may be
    # a protected module -- that's fine, add_tests only WRITES the new test file under tests/unit/.
    {"target": "tests/unit/test_engagement_scope.py", "action": "add_tests", "source": "tools/engagement_scope.py",
     "why": "EngagementGate is the security scope gate -- high value to characterize; pure logic, no network"},
    {"target": "tests/unit/test_response_classifier.py", "action": "add_tests", "source": "tools/response_classifier.py",
     "why": "deterministic rules-based classifier, untested, no network"},
    {"target": "tests/unit/test_qa_tracker.py", "action": "add_tests", "source": "tools/qa_tracker.py",
     "why": "queue/ledger logic, pure JSON, untested"},
    {"target": "tests/unit/test_ast_pruner.py", "action": "add_tests", "source": "tools/ast_pruner.py",
     "why": "AST utility, pure, untested"},
    {"target": "tests/unit/test_token_utils.py", "action": "add_tests", "source": "tools/token_utils.py",
     "why": "token counting/truncation helpers, pure, untested"},
    {"target": "tests/unit/test_code_indexer.py", "action": "add_tests", "source": "tools/code_indexer.py",
     "why": "codebase indexer, filesystem-only, untested"},
    {"target": "tests/unit/test_detection_engine.py", "action": "add_tests", "source": "tools/detection_engine.py",
     "why": "detection logic, untested"},
    {"target": "tests/unit/test_blackboard.py", "action": "add_tests", "source": "tools/blackboard.py",
     "why": "attack-surface tracker (untested/tested set + JSON persistence), pure, untested"},
    # Engine EngagementGate retrofits (AGENTS.md: the original engines predate the scope-gate). sqli is
    # done; these are the rest of the original set. Behavior-preserving guard, one per engine.
    {"target": "tools/engines/xss_engine.py", "action": "retrofit_engagement_gate", "source": "tools/engines/xss_engine.py",
     "why": "AGENTS.md engine-gap: xss_engine predates EngagementGate"},
    {"target": "tools/engines/idor_engine.py", "action": "retrofit_engagement_gate", "source": "tools/engines/idor_engine.py",
     "why": "AGENTS.md engine-gap: idor_engine predates EngagementGate"},
    {"target": "tools/engines/lfi_engine.py", "action": "retrofit_engagement_gate", "source": "tools/engines/lfi_engine.py",
     "why": "AGENTS.md engine-gap: lfi_engine predates EngagementGate"},
    {"target": "tools/engines/ssrf_engine.py", "action": "retrofit_engagement_gate", "source": "tools/engines/ssrf_engine.py",
     "why": "AGENTS.md engine-gap: ssrf_engine predates EngagementGate"},
    {"target": "tools/engines/ssti_engine.py", "action": "retrofit_engagement_gate", "source": "tools/engines/ssti_engine.py",
     "why": "AGENTS.md engine-gap: ssti_engine predates EngagementGate"},
    {"target": "tools/engines/open_redirect_engine.py", "action": "retrofit_engagement_gate", "source": "tools/engines/open_redirect_engine.py",
     "why": "AGENTS.md engine-gap: open_redirect_engine predates EngagementGate"},
    {"target": "tools/engines/file_upload_engine.py", "action": "retrofit_engagement_gate", "source": "tools/engines/file_upload_engine.py",
     "why": "AGENTS.md engine-gap: file_upload_engine predates EngagementGate"},
]


def discover_untested_targets():
    """Self-refilling runway: synthesize add_tests targets for in-scope source modules under tools/
    that have no tests/unit/test_<name>.py yet. Keeps the swarm supplied with real work without a human
    curating every target. Restricted to tools/ (peripheral, live, high-value) -- ai_systems/ has large
    dormant subtrees that aren't worth auto-testing."""
    existing = set()
    tdir = REPO_ROOT / "tests" / "unit"
    if tdir.exists():
        for f in tdir.iterdir():
            if f.name.startswith("test_") and f.name.endswith(".py"):
                existing.add(f.name[len("test_"):-3])
    out = []
    for rel in list_in_scope_files(exts=(".py",)):
        parts = rel.split("/")
        if parts[0] != "tools":
            continue
        base = parts[-1][:-3]
        if base == "__init__" or base.startswith("test_") or base in existing:
            continue
        target = f"tests/unit/test_{base}.py"
        if is_in_scope(target):
            out.append({"target": target, "action": "add_tests", "source": rel,
                        "why": f"{rel} has no unit test (auto-discovered)"})
            existing.add(base)  # dedup if two modules share a basename
    return out


def discover_core_targets():
    """CORE-RSI only: surface Sabrina's own brain modules as improvement targets. Empty outside core
    mode. action='improve' -> the goal decides docs/type-hints/refactor. Each is gated per-target by
    the loop's self-check + checkpoint auto-revert, so a brain-breaking edit is undone automatically."""
    if not os.environ.get("SABRINA_CORE_MODE"):
        return []
    return [{"target": rel, "action": "improve", "source": rel,
             "why": "core module (core-RSI session): improve clarity/docs/types/logic without breaking it"}
            for rel in sorted(CORE_RELATIVE) if is_in_scope(rel)]


def _rel(path):
    """Normalize any path to a forward-slash string relative to the repo root."""
    try:
        p = Path(path)
        if not p.is_absolute():
            p = REPO_ROOT / p
        return str(p.resolve().relative_to(REPO_ROOT)).replace(os.sep, "/")
    except Exception:
        return str(path).replace(os.sep, "/")


def is_in_scope(path):
    """True only for real, writable source files the loop is allowed to improve."""
    rel = _rel(path)
    # Explicit membership decided FIRST, so root-level core files (e.g. sabrina_gemini_loop.py) are
    # handled correctly even though they sit outside ALLOW_ROOTS.
    if rel in RECOVERY_RELATIVE or rel in FINISHED_RELATIVE:
        return False  # never a target, even in core mode
    if rel in CORE_RELATIVE:
        return bool(os.environ.get("SABRINA_CORE_MODE"))  # Sabrina's brain: only in a core-RSI session
    low = rel.lower()
    parts = low.split("/")
    if not parts:
        return False
    if parts[0] not in ALLOW_ROOTS:
        return False
    if parts[0] in DENY_FIRST_SEGMENTS:
        return False
    if any(seg.startswith(".sandbox_clone_") for seg in parts):
        return False
    if any(seg in DENY_FIRST_SEGMENTS for seg in parts):
        return False
    if "__pycache__" in parts:
        return False
    return True


def list_in_scope_files(exts=(".py",)):
    """Yield repo-relative paths of all in-scope source files (for the session snapshotter). Walks
    only the allow-roots and applies is_in_scope, so it never descends into the 6.7 GB of
    duplicate/vendored/generated trees."""
    out = []
    for root in ALLOW_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # prune obviously-excluded dirs early for speed
            dirnames[:] = [d for d in dirnames
                           if d != "__pycache__" and not d.startswith(".sandbox_clone_")
                           and d not in DENY_FIRST_SEGMENTS]
            for fn in filenames:
                if exts and not fn.endswith(tuple(exts)):
                    continue
                rel = _rel(os.path.join(dirpath, fn))
                if is_in_scope(rel):
                    out.append(rel)
    return sorted(out)


def _load_ledger():
    if LEDGER_FILE.exists():
        try:
            with open(LEDGER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_ledger(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _with_ledger_lock(fn, timeout=10.0):
    """Run fn() while holding a simple cross-process lock, so swarm workers don't clobber the ledger
    during concurrent read-modify-write. Breaks a stale lock after `timeout`."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = LEDGER_FILE.with_suffix(".lock")
    start = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            if time.time() - start > timeout:
                try:
                    os.remove(lock)  # stale lock -- break it
                except OSError:
                    pass
                continue
            time.sleep(0.05)
    try:
        return fn()
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def mark_improved(target, action="", outcome="", note=""):
    """Stamp a target as worked right now, with the action taken and its outcome. Lock-guarded so
    parallel swarm workers can't lose each other's stamps. REFUSES to stamp a target whose file does
    not actually exist on disk -- otherwise a loop that over-claims ("tests green") would record
    PHANTOM work and permanently skip a real target (this happened with test_oracle_providers.py)."""
    rel = _rel(target)
    if not (REPO_ROOT / rel).is_file():
        return {"status": "refused", "target": rel,
                "reason": ("target file does not exist on disk -- the change did not persist, so this "
                           "is NOT done. Write the file (and verify it) before calling mark_improved.")}

    def _do():
        data = _load_ledger()
        data[rel] = {
            "last_improved_ts": time.time(),
            "action": action,
            "outcome": outcome,
            "note": note,
        }
        _save_ledger(data)
        return {"status": "success", "target": rel}

    return _with_ledger_lock(_do)


def _worker_for(target, wc):
    """Assign a target to a worker by a PURE function of its (immutable) path -- NOT its position in a
    ranked list. This is what keeps the swarm's partition a true disjoint cover: every worker computes
    the same owner for a given file no matter when it read the mutating policy/ledger, so two workers
    can never both own a file and no file is ever ownerless. (sha1 is used, not builtin hash(), because
    hash() is per-process salted via PYTHONHASHSEED and would differ across the worker processes.)"""
    h = int(hashlib.sha1(str(target).encode("utf-8")).hexdigest(), 16)
    return h % max(1, int(wc))


def _rank_by_policy(queue):
    """Round-5 bridge: order the candidate queue by the DREAMED selection policy
    (data/rsi_policy/current.json) so the whole swarm works what the policy values -- not merely
    oldest-first. 'score' ranks by the per-action value dream learned over the shared discovery-tree
    pool (action-types that historically paid off come first), tie-broken least-recently-improved
    first; 'fifo' is the oldest-first default. The SAME tools.rsi_improve.CandidatePolicy object is
    used offline by dream()'s replay scoring, which is exactly what closes the online<->offline loop
    across the swarm.

    DEFENSIVE: this runs on every worker's critical path, so ANY failure (no policy yet, import error,
    malformed file) falls back to plain oldest-first -- the queue must never crash. The result is
    validated to be a true permutation of the same entry objects before it is trusted."""
    try:
        from tools.rsi_improve import CandidatePolicy  # canonical policy object (the one dream ships)
        ranked = CandidatePolicy.load().rank_targets(queue)
        if (isinstance(ranked, list) and len(ranked) == len(queue)
                and {id(x) for x in ranked} == {id(x) for x in queue}):
            return ranked
    except Exception:
        pass
    return sorted(queue, key=lambda x: x.get("last_improved_ts", 0))


def get_improvement_queue(limit=5, worker_id=0, worker_count=1):
    """Return in-scope improvement targets ordered by the dreamed selection policy (Round-5 bridge),
    falling back to least-recently-improved first when no policy is deployed yet. Only targets whose
    writable file is in scope are surfaced.

    Swarm support: with worker_count>1 each worker OWNS a disjoint subset of targets, assigned by a
    pure hash of the target PATH (_worker_for) BEFORE ranking. This makes ownership independent of the
    (mutating) policy and ledger, so the partition stays a perfect disjoint cover even though each
    worker reads that state at a different instant -- two workers can never grab the same file and no
    file is ever skipped. Ranking then only orders a worker's OWN targets, so the per-worker order may
    safely differ. worker_id/worker_count default to a single worker (whole queue).

    (This replaced an earlier positional slice queue[wi::wc] taken AFTER ranking, which broke whenever
    the ledger's last_improved_ts or the dreamed action_values changed between two workers' reads:
    the workers then sliced differently-ordered lists, overlapping on some files and skipping others.)"""
    ledger = _load_ledger()
    # Curated seeds first, then auto-discovered untested modules (deduped by target). Seeds win on
    # collision so their richer 'why' is kept.
    combined, seen = [], set()
    # In a core-RSI session, brain modules come FIRST (that's the point); then seeds, then discovered.
    for entry in discover_core_targets() + list(SEED_TARGETS) + discover_untested_targets():
        if entry["target"] in seen:
            continue
        seen.add(entry["target"])
        combined.append(entry)
    queue = []
    for entry in combined:
        target = entry["target"]
        if not is_in_scope(target):
            continue  # guardrail: never surface an out-of-scope / protected target
        rec = ledger.get(_rel(target), {})
        last = rec.get("last_improved_ts", 0)
        queue.append({
            "target": target,
            "action": entry["action"],
            "source": entry.get("source"),
            "why": entry.get("why", ""),
            "last_improved_ts": last,
            "last_improved_str": time.ctime(last) if last else "Never",
            "last_outcome": rec.get("outcome", ""),
        })
    try:
        wc = max(1, int(worker_count))
        wi = max(0, int(worker_id)) % wc
    except Exception:
        wc, wi = 1, 0
    if wc > 1:
        # Own by target-path hash BEFORE ranking -> a stable disjoint cover regardless of when each
        # worker read the mutating policy/ledger (no overlaps, no gaps).
        queue = [e for e in queue if _worker_for(e["target"], wc) == wi]
    queue = _rank_by_policy(queue)  # Round-5 bridge: rank THIS worker's own targets by the dreamed policy
    if limit:
        queue = queue[:limit]
    return {"status": "success", "worker_id": wi, "worker_count": wc, "queue": queue}


if __name__ == "__main__":
    import pprint
    print("=== is_in_scope checks (expect True only for real writable source) ===")
    for p, expect in [
        ("tools/replay_simulator.py", True),
        ("tests/unit/test_x.py", True),
        ("tools/engines/sqli_engine.py", True),
        (".sandbox_clone_9cdbb5/tools/x.py", False),
        ("Sabrina-dev/tools/x.py", False),
        ("scratch/check_x.py", False),
        ("security_items/PayloadsAllTheThings/x.py", False),
        ("data/probe_runs.db", False),
        ("sabrina_gemini_loop.py", False),          # protected core
        ("tools/oracle_providers.py", False),        # protected core
        ("tools/dream_rsi/tree.py", False),          # finished module
    ]:
        got = is_in_scope(p)
        flag = "OK" if got == expect else "!! MISMATCH"
        print(f"  [{flag}] {p}: in_scope={got} (expected {expect})")
    print("\n=== queue ===")
    pprint.pprint(get_improvement_queue())
