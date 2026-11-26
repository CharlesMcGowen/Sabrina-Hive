"""rsi_distill.py -- the Dream-RSI loop closed around Sabrina's OWN distillation data.

This is the distillation-domain analog of tools/rsi_improve.py. Where rsi_improve dreams a target-
SELECTION policy over engagement discovery trees, this dreams a GENERATION-ALLOCATION policy over the
recorded OUTCOMES of a trace-generation run, then forges the next round's per-class plan + RL
curriculum weights. It is the piece that makes Sabrina adapt her own process from her own data instead
of a human hand-tuning each round.

Faithful to the paper's guarantees (same as rsi_improve):
  * RECORD every outcome: each generation attempt -> (class, kept?, quality) becomes a pool node.
  * REPLAY, don't re-run: candidate allocation policies are scored over the FROZEN pool statistics
    (zero new teacher calls), by the objective V below.
  * NEVER-WORSE: pi0 (uniform allocation -- the current behavior) is always in the candidate set and
    the deployed policy must satisfy V(best) >= V(pi0), so a dream can never regress the process.
  * POLICY-AS-CODE: each candidate is an allocate(budget, stats) function; the winner is serialized as
    a concrete per-class plan the next distill_traces round consumes (--plan) and the RL curriculum reads.

Objective (offline replay over recorded per-class hit-rate h_c and mean kept-quality q_c):
    useful(pi)   = sum_c  b_c * h_c * q_c              # expected quality-weighted kept traces
    coverage(pi) = sum_c  min(b_c*h_c, floor)/floor    # saturating: rewards NOT starving hard classes
    V(pi)        = useful(pi) + COV_WEIGHT * coverage(pi)       (budget fixed, so cost is constant)
The tension is real: pure `useful` favors easy high-yield classes; `coverage` protects the hard ones
(e.g. python_exploit, whose PoCs sometimes don't compile). The dream picks the balance that beats
uniform -- typically shifting budget toward the hard class so it still reaches target, without
starving the rest.

CLI:
  python tools/rsi_distill.py forge --traces data/distill_sft_full.jsonl --observed data/rsi_distill/observed_round1.json --budget 720
  python tools/rsi_distill.py show
"""
import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "data" / "rsi_distill"
CURRENT = STATE_DIR / "current_plan.json"
LOG = STATE_DIR / "dream_log.json"

COV_WEIGHT = 1.0        # how much we value not-starving any class vs raw yield
COV_FLOOR = 40          # per-class expected-kept target that saturates the coverage term


# ── pool: recorded outcomes ────────────────────────────────────────────────────────────────────
def _trace_quality(rec):
    """Per kept-trace quality in [0,1]: reasoning richness + a non-trivial action. Kept traces already
    passed validation (well-formed think+action, code compiles), so this grades DEPTH, not validity."""
    a = rec["messages"][2]["content"]
    think = re.search(r"<think>(.*?)</think>", a, re.DOTALL)
    action = re.search(r"<action>(.*?)</action>", a, re.DOTALL)
    tlen = len(think.group(1).strip()) if think else 0
    alen = len(action.group(1).strip()) if action else 0
    richness = min(tlen / 300.0, 1.0)
    substance = min(alen / 80.0, 1.0)
    return round(0.6 * richness + 0.4 * substance, 4)


def build_pool(traces_path, observed_path):
    """Reconstruct the recorded outcome pool. Kept traces (with quality) come from the corpus jsonl;
    per-class ATTEMPT counts (kept + rejected) come from the observed-outcomes file the run wrote.
    Returns per-class stats: attempts, kept, hit_rate, mean_quality, action_value."""
    kept = [json.loads(l) for l in open(traces_path, encoding="utf-8") if l.strip()]
    observed = json.loads(Path(observed_path).read_text(encoding="utf-8"))  # {class: {"attempts":N,"kept":K}}

    by_class = {}
    for r in kept:
        c = r["vuln_class"]
        by_class.setdefault(c, []).append(_trace_quality(r))

    stats = {}
    for c, obs in observed.items():
        quals = by_class.get(c, [])
        attempts = max(obs["attempts"], 1)
        k = obs.get("kept", len(quals))
        mean_q = round(sum(quals) / len(quals), 4) if quals else 0.0
        hit = round(k / attempts, 4)
        stats[c] = {"attempts": attempts, "kept": k, "hit_rate": hit,
                    "mean_quality": mean_q, "action_value": round(hit * mean_q, 4)}
    return stats


# ── candidate allocation policies (policy-as-code) ─────────────────────────────────────────────
def _normalize_alloc(weights, budget):
    tot = sum(weights.values()) or 1.0
    raw = {c: budget * w / tot for c, w in weights.items()}
    # round to ints, then fix rounding drift against budget
    alloc = {c: max(1, int(round(v))) for c, v in raw.items()}
    drift = budget - sum(alloc.values())
    if drift:
        order = sorted(alloc, key=lambda c: raw[c] - alloc[c], reverse=drift > 0)
        for i in range(abs(drift)):
            alloc[order[i % len(order)]] += 1 if drift > 0 else -1
    return alloc


def pol_uniform(stats, budget):
    return _normalize_alloc({c: 1.0 for c in stats}, budget)


def pol_yield_weighted(stats, budget):
    return _normalize_alloc({c: s["hit_rate"] for c, s in stats.items()}, budget)


def pol_value_weighted(stats, budget):
    return _normalize_alloc({c: max(s["action_value"], 1e-3) for c, s in stats.items()}, budget)


def pol_difficulty_adjusted(stats, budget):
    # spend MORE attempts where the hit-rate is low, so hard classes still reach target counts
    return _normalize_alloc({c: 1.0 / max(s["hit_rate"], 0.05) for c, s in stats.items()}, budget)


def pol_coverage_deficit(stats, budget):
    # shore up classes whose realized action-value is weakest
    return _normalize_alloc({c: 1.0 - s["action_value"] + 0.1 for c, s in stats.items()}, budget)


POLICIES = {
    "uniform (pi0)": pol_uniform,
    "yield_weighted": pol_yield_weighted,
    "value_weighted": pol_value_weighted,
    "difficulty_adjusted": pol_difficulty_adjusted,
    "coverage_deficit": pol_coverage_deficit,
}


# ── dream: replay each policy over the frozen pool, deploy best-if >= pi0 ────────────────────────
def _V(alloc, stats):
    useful, coverage = 0.0, 0.0
    for c, b in alloc.items():
        s = stats[c]
        exp_kept = b * s["hit_rate"]
        useful += exp_kept * s["mean_quality"]
        coverage += min(exp_kept, COV_FLOOR) / COV_FLOOR
    return round(useful + COV_WEIGHT * coverage, 4), round(useful, 4), round(coverage, 4)


def dream(stats, budget):
    results = []
    for name, fn in POLICIES.items():
        alloc = fn(stats, budget)
        v, useful, cov = _V(alloc, stats)
        results.append({"policy": name, "V": v, "useful": useful, "coverage": cov, "alloc": alloc})
    v0 = next(r["V"] for r in results if r["policy"].startswith("uniform"))
    # never-worse: pi0 is in the set; require best strictly beat it to redeploy, else keep pi0
    best = max(results, key=lambda r: r["V"])
    deployed = best if best["V"] >= v0 else next(r for r in results if r["policy"].startswith("uniform"))
    return {"deployed": deployed, "pi0_V": v0, "candidates": results}


# ── forge: winning policy -> next-round plan + RL curriculum weights ─────────────────────────────
def forge(traces_path, observed_path, budget):
    stats = build_pool(traces_path, observed_path)
    d = dream(stats, budget)
    dep = d["deployed"]
    # RL curriculum: give harder / lower-value classes MORE weight so training focuses where the model
    # is weak (inverse action-value, normalized) -- the same signal, applied to the RL side.
    inv = {c: 1.0 / max(s["action_value"], 0.05) for c, s in stats.items()}
    tot = sum(inv.values())
    curriculum = {c: round(w / tot, 4) for c, w in inv.items()}
    plan = {
        "budget": budget,
        "deployed_policy": dep["policy"],
        "improved_over_uniform": bool(dep["V"] > d["pi0_V"]),
        "V": dep["V"], "pi0_V": d["pi0_V"],
        "next_round_counts": dep["alloc"],
        "rl_curriculum_weights": curriculum,
        "per_class_stats": stats,
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    hist = json.loads(LOG.read_text(encoding="utf-8")) if LOG.exists() else []
    hist.append({"budget": budget, "deployed": dep["policy"], "V": dep["V"], "pi0_V": d["pi0_V"]})
    LOG.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    return plan, d


def _fmt_plan(plan, dream_res):
    lines = [f"# RSI dream over distillation outcomes (budget {plan['budget']})", ""]
    lines.append(f"deployed policy: {plan['deployed_policy']}  "
                 f"(V={plan['V']} vs uniform pi0 V={plan['pi0_V']}, "
                 f"{'IMPROVED' if plan['improved_over_uniform'] else 'kept pi0 -- no candidate beat it'})")
    lines.append("\ncandidates scored by replay (never-worse: pi0 always in set):")
    for r in sorted(dream_res["candidates"], key=lambda x: x["V"], reverse=True):
        mark = "  <-- deployed" if r["policy"] == plan["deployed_policy"] else ""
        lines.append(f"  {r['policy']:22s} V={r['V']:7.3f}  useful={r['useful']:7.2f} cov={r['coverage']:.3f}{mark}")
    lines.append("\nper-class action-value (hit_rate x mean_quality) and next-round allocation:")
    stats = plan["per_class_stats"]
    for c in sorted(stats, key=lambda c: stats[c]["action_value"]):
        s = stats[c]
        lines.append(f"  {c:16s} hit={s['hit_rate']:.3f} q={s['mean_quality']:.3f} "
                     f"value={s['action_value']:.3f}  -> next={plan['next_round_counts'][c]}")
    lines.append("\nRL curriculum weights (inverse action-value -- train more where the model is weak):")
    cw = plan["rl_curriculum_weights"]
    for c in sorted(cw, key=lambda c: cw[c], reverse=True):
        lines.append(f"  {c:16s} {cw[c]:.4f}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("forge", help="dream over recorded outcomes, forge next-round plan")
    f.add_argument("--traces", default=str(REPO_ROOT / "data" / "distill_sft_full.jsonl"))
    f.add_argument("--observed", default=str(STATE_DIR / "observed_round1.json"))
    f.add_argument("--budget", type=int, default=720)
    sub.add_parser("show", help="print the current forged plan")
    a = ap.parse_args()

    if a.cmd == "forge":
        plan, d = forge(a.traces, a.observed, a.budget)
        print(_fmt_plan(plan, d))
        print(f"\n[rsi_distill] plan -> {CURRENT}")
    elif a.cmd == "show":
        if not CURRENT.exists():
            print("no forged plan yet -- run `forge`")
            return
        plan = json.loads(CURRENT.read_text(encoding="utf-8"))
        print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
