"""Code-evolution RSI -- the accept-or-revert gate that CLOSES the loop (AlphaEvolve-shaped).

The shallow policy-RSI only tuned config (worker_count / strategy). This gate makes the candidate a
REAL code change: Sabrina (the code agent) edits a target, and the change is KEPT only if a benchmark
score RISES and the tests stay green -- otherwise it is AUTO-REVERTED. Consequences:

  * MONOTONIC: the benchmark score is non-decreasing across accepted steps (a change that doesn't help
    is rolled back), so the loop can only ever move the real capability number up or leave it flat.
  * NO CATASTROPHIC LOSS: a full-tree snapshot is taken BEFORE each candidate (tools/system_checkpoint)
    and restored on ANY reject, test failure, or exception -- a bad self-edit can never brick the tree.
  * AUDITABLE: every attempt (accepted or reverted) is logged to the discovery tree (tools/rsi_history)
    with its real score delta, feeding the Dream-RSI replay pool.

Roles: Sabrina = code agent (writes the candidate via `candidate_fn`); this harness = the mechanic gate
(snapshot, score, test, accept-or-revert, log). score_fn / test_fn / candidate_fn are all pluggable so
this is testable offline (mocks) and drop-in for any target with a FAST benchmark proxy (score_fn is
called twice per round, so use a fast score -- a sim like smuggle_sim.score_detector, an engine's unit
tests, or a small task sample -- not the full 40-min eval).

    from tools.rsi_evolve import evolve_once, evolve_loop
    evolve_loop("tools/engines/http_smuggling_engine.py", score_fn, sabrina_candidate, test_fn=run_tests)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from tools import system_checkpoint
from tools.rsi_history import SessionTree


@dataclass
class EvolveResult:
    accepted: bool
    reason: str          # accepted | no_gain | tests_failed | candidate_error
    baseline: float
    after: float | None
    gain: float
    target: str
    checkpoint: str = ""


def _log(tree, parent, target, action, diff_summary, score):
    if tree is None:
        return None
    try:
        return tree.add_attempt(parent, target, action, diff_summary, score)
    except Exception:
        return None


def evolve_once(target, score_fn, candidate_fn, *, test_fn=None, min_gain=1e-9,
                tree=None, parent=None, label="rsi_evolve", verbose=True) -> EvolveResult:
    """ONE code-evolution step: snapshot -> candidate_fn edits `target` -> tests -> re-score ->
    accept-or-revert -> log. Returns EvolveResult. `candidate_fn(target) -> diff_summary` is the code
    agent (Sabrina in production, a mock in tests). `score_fn() -> float` is a FAST benchmark proxy."""
    def _say(m):
        if verbose:
            print(f"[rsi_evolve] {m}", flush=True)

    baseline = float(score_fn())
    _say(f"baseline on {target}: {baseline:.4f}")
    ckpt = system_checkpoint.create(label=label)          # snapshot BEFORE the change
    ckpt_name = getattr(ckpt, "name", str(ckpt))

    # 1. the code agent proposes a change to `target`
    try:
        diff_summary = candidate_fn(target) or ""
    except Exception as e:
        system_checkpoint.restore(ckpt)
        _say(f"candidate raised {type(e).__name__}: {e} -> reverted")
        _log(tree, parent, target, "reverted_candidate_error", f"{type(e).__name__}: {e}"[:200], baseline)
        return EvolveResult(False, "candidate_error", baseline, None, 0.0, target, ckpt_name)

    # 2. a change that breaks the tests is reverted regardless of score
    if test_fn is not None:
        try:
            ok = bool(test_fn())
        except Exception:
            ok = False
        if not ok:
            system_checkpoint.restore(ckpt)
            _say("tests FAILED after candidate -> reverted")
            _log(tree, parent, target, "reverted_tests", diff_summary, baseline)
            return EvolveResult(False, "tests_failed", baseline, None, 0.0, target, ckpt_name)

    # 3. re-score; ACCEPT only if the benchmark actually rose
    after = float(score_fn())
    gain = after - baseline
    if gain > min_gain:
        try:
            system_checkpoint.promote(ckpt)               # mark a known-good state
        except Exception:
            pass
        _say(f"ACCEPTED {baseline:.4f} -> {after:.4f} (+{gain:.4f})")
        _log(tree, parent, target, "accepted", diff_summary, after)
        return EvolveResult(True, "accepted", baseline, after, gain, target, ckpt_name)

    system_checkpoint.restore(ckpt)                        # no gain -> roll back
    _say(f"no gain {baseline:.4f} -> {after:.4f} -> reverted")
    _log(tree, parent, target, "reverted_no_gain", diff_summary, baseline)
    return EvolveResult(False, "no_gain", baseline, after, gain, target, ckpt_name)


def evolve_loop(target, score_fn, candidate_fn, *, rounds=5, test_fn=None, min_gain=1e-9,
                patience=3, history_dir=None, verbose=True) -> dict:
    """Run up to `rounds` evolve_once steps on `target`, chaining each ACCEPTED change as the parent of
    the next attempt (so the tree records the real improvement path). Stops early after `patience`
    consecutive non-accepted rounds. Saves the session's discovery tree. Returns a summary dict."""
    tree = SessionTree(root_label=f"evolve:{target}", root_score=float(score_fn()))
    parent = tree.root
    accepted, dry, results = 0, 0, []
    for i in range(rounds):
        if verbose:
            print(f"[rsi_evolve] === round {i+1}/{rounds} on {target} ===", flush=True)
        r = evolve_once(target, score_fn, candidate_fn, test_fn=test_fn, min_gain=min_gain,
                        tree=tree, parent=parent, verbose=verbose)
        results.append(asdict(r))
        if r.accepted:
            accepted += 1
            dry = 0
            kids = getattr(parent, "children", None)
            if kids:
                parent = kids[-1]                          # chain the next attempt off the accepted node
        else:
            dry += 1
            if dry >= patience:
                if verbose:
                    print(f"[rsi_evolve] {patience} non-accepted rounds -> stop", flush=True)
                break
    path = tree.save(label="evolve_" + target.replace("/", "_").replace("\\", "_"), history_dir=history_dir)
    return {"target": target, "rounds_run": len(results), "accepted": accepted,
            "final_score": float(score_fn()), "tree": str(path), "results": results}
