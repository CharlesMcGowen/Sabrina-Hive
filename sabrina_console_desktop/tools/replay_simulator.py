"""
replay_simulator.py -- offline policy replay/scoring over Sabrina's OWN decision history.

This is the Dream-RSI idea applied to real recorded data in data/probe_runs.db: score
candidate decision-ROUTING policies against what actually happened, with NO new inference.
A candidate policy only deserves to be deployed if it beats the current one on this replay.

Everything here is read-only and offline -- no HTTP, no target_url, no live model calls.
It reads signals that were already logged:

  inference_traces : every past decide/reflect round -- task_type, `preferred` (the routing
                     choice that round), model_used, and the PLAN_STEP_WORKED:<n> progress
                     signal parsed out of the response text. (n == 0 means the round made no
                     plan progress; n > 0 means it advanced to plan step n.)
  oracle_usage     : per-provider success / error / cooldown counts and latency_ms -- the
                     cost model for each routing choice.

Scoring, per policy, over the recorded decision points:

    score = quality  -  COST_WEIGHT * norm_cost  +  PARALLEL_WEIGHT * parallel_bonus

  quality        = fraction of that routing's recorded rounds where PLAN_STEP_WORKED > 0
  norm_cost      = that routing's mean provider latency, normalised 0..1 across policies,
                   plus its wasted-cooldown-attempt rate (calls that hit a cooled-down provider)
  parallel_bonus = small credit for keeping work on the cheap/local tier (parallelisable, no
                   shared cloud rate limit)

Run standalone:   python tools/replay_simulator.py
Import & call  :   from tools.replay_simulator import run_replay; print(run_replay())

Honest limits (stated, not hidden): this scores ROUTING policies, because that is the signal
the recorded data actually supports. It does NOT re-simulate alternative *tool* choices (the
data doesn't record what a different action would have returned). Groups with few PLAN_STEP_WORKED
samples are reported with their n so low-confidence rows are visible rather than trusted blindly.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections import defaultdict

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "probe_runs.db")

COST_WEIGHT = 0.5
PARALLEL_WEIGHT = 0.15


def _provider_of(model_used: str) -> str:
    m = (model_used or "").lower()
    if "gemini" in m or "google" in m:
        return "gemini"
    if "claude" in m and "cli" in m:
        return "claude_cli"
    if "claude" in m:
        return "claude_api"
    if m.startswith("local") or "ollama" in m or "proxy.runpod" in m or "192.168" in m or "127.0.0.1" in m:
        return "local"
    return "other"


_PLAN_RE = re.compile(r"PLAN_STEP_WORKED:\s*(\d+)")


def _connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    # Read-only: never mutate the live DB the loop writes to.
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def load_decisions(conn: sqlite3.Connection) -> list[dict]:
    """Every recorded decision round that carries a PLAN_STEP_WORKED progress signal."""
    rows = conn.execute(
        "SELECT round_num, task_type, preferred, model_used, response "
        "FROM inference_traces WHERE response LIKE '%PLAN_STEP_WORKED%'"
    ).fetchall()
    out = []
    for round_num, task_type, preferred, model_used, response in rows:
        m = _PLAN_RE.search(response or "")
        if not m:
            continue
        progress = int(m.group(1))
        out.append({
            "round_num": round_num,
            "task_type": task_type,
            "preferred": (preferred or "unknown"),
            "model_used": model_used or "unknown",
            "provider": _provider_of(model_used),
            "progress": progress,
            "worked": 1 if progress > 0 else 0,
        })
    return out


def provider_cost_model(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per-provider real cost/reliability from oracle_usage."""
    stats = {}
    rows = conn.execute(
        "SELECT provider, "
        "SUM(outcome='success') AS succ, "
        "SUM(outcome='error') AS err, "
        "SUM(outcome LIKE 'skipped%') AS skip, "
        "AVG(CASE WHEN latency_ms > 0 THEN latency_ms END) AS avg_ms, "
        "COUNT(*) AS total "
        "FROM oracle_usage GROUP BY provider"
    ).fetchall()
    for provider, succ, err, skip, avg_ms, total in rows:
        total = total or 1
        stats[provider] = {
            "success_rate": (succ or 0) / total,
            "avg_latency_ms": (avg_ms if avg_ms is not None else 0.0),
            "cooldown_rate": (skip or 0) / total,  # fraction of attempts wasted on a cooled-down provider
            "n": total,
        }
    # local models aren't in oracle_usage (that table tracks the cloud/CLI tiers); give local a
    # cheap, reliable default so routing that stays local is costed fairly rather than as unknown.
    stats.setdefault("local", {"success_rate": 0.95, "avg_latency_ms": 1500.0, "cooldown_rate": 0.0, "n": 0})
    stats.setdefault("other", {"success_rate": 0.8, "avg_latency_ms": 2000.0, "cooldown_rate": 0.0, "n": 0})
    return stats


def _routing_quality(decisions: list[dict]) -> dict[str, dict]:
    """Recorded progress quality per `preferred` routing choice."""
    agg = defaultdict(lambda: {"n": 0, "worked": 0, "progress_sum": 0, "providers": defaultdict(int)})
    for d in decisions:
        a = agg[d["preferred"]]
        a["n"] += 1
        a["worked"] += d["worked"]
        a["progress_sum"] += d["progress"]
        a["providers"][d["provider"]] += 1
    q = {}
    for pref, a in agg.items():
        n = a["n"]
        q[pref] = {
            "n": n,
            "quality": a["worked"] / n,               # fraction of rounds that made progress
            "mean_progress": a["progress_sum"] / n,   # how far it typically advanced
            "provider_mix": dict(a["providers"]),
        }
    return q


def _routing_cost(provider_mix: dict[str, int], cost_model: dict[str, dict]) -> dict:
    """Weighted cost of a routing, from the providers it actually used."""
    total = sum(provider_mix.values()) or 1
    latency = 0.0
    cooldown = 0.0
    local_share = 0.0
    for prov, cnt in provider_mix.items():
        w = cnt / total
        cm = cost_model.get(prov, cost_model.get("other"))
        latency += w * cm["avg_latency_ms"]
        cooldown += w * cm["cooldown_rate"]
        if prov == "local":
            local_share += w
    return {"avg_latency_ms": latency, "cooldown_rate": cooldown, "local_share": local_share}


def run_replay(db_path: str = DB_PATH) -> dict:
    """Score every recorded routing policy against history. Returns a ranked result dict."""
    conn = _connect(db_path)
    try:
        decisions = load_decisions(conn)
        cost_model = provider_cost_model(conn)
    finally:
        conn.close()

    if not decisions:
        return {"error": "no decisions with a PLAN_STEP_WORKED signal found", "n_decisions": 0}

    quality = _routing_quality(decisions)

    for pref, q in quality.items():
        q["cost"] = _routing_cost(q["provider_mix"], cost_model)
    max_lat = max((q["cost"]["avg_latency_ms"] for q in quality.values()), default=1.0) or 1.0

    scored = []
    for pref, q in quality.items():
        norm_cost = (q["cost"]["avg_latency_ms"] / max_lat) + q["cost"]["cooldown_rate"]
        parallel_bonus = q["cost"]["local_share"]
        score = q["quality"] - COST_WEIGHT * norm_cost + PARALLEL_WEIGHT * parallel_bonus
        scored.append({
            "policy": pref,
            "n": q["n"],
            "quality": round(q["quality"], 3),
            "mean_progress": round(q["mean_progress"], 2),
            "avg_latency_ms": round(q["cost"]["avg_latency_ms"]),
            "cooldown_rate": round(q["cost"]["cooldown_rate"], 3),
            "local_share": round(q["cost"]["local_share"], 3),
            "score": round(score, 3),
            "provider_mix": q["provider_mix"],
        })
    scored.sort(key=lambda r: r["score"], reverse=True)

    current = max(scored, key=lambda r: r["n"])  # the routing the loop uses most == its de-facto default
    best = scored[0]
    return {
        "n_decisions": len(decisions),
        "n_policies": len(scored),
        "ranked": scored,
        "current_policy": current["policy"],
        "best_policy": best["policy"],
        "verdict": (
            f"KEEP current routing '{current['policy']}' -- it already scores best."
            if best["policy"] == current["policy"] else
            f"CANDIDATE '{best['policy']}' beats current '{current['policy']}' on replay "
            f"({best['score']} vs {current['score']}) -- worth trying, but confirm on n={best['n']} samples."
        ),
    }


def guidance_line(db_path: str = DB_PATH) -> str:
    """One compact line for the loop to consult each round (fed into read_environment)."""
    try:
        res = run_replay(db_path)
        if res.get("error"):
            return f"REPLAY-SIMULATOR: unavailable ({res['error']})."
        top = res["ranked"][:3]
        pairs = ", ".join(f"{r['policy']}={r['quality']:.0%}@{r['avg_latency_ms']}ms(n={r['n']})" for r in top)
        return (
            "REPLAY-SIMULATOR GUIDANCE (from your OWN recorded history, offline): routing "
            f"progress-rate by preference -> {pairs}. {res['verdict']}"
        )
    except Exception as e:  # never let consultation break the loop
        return f"REPLAY-SIMULATOR: consult failed ({e})."


def _format_report(res: dict) -> str:
    if res.get("error"):
        return f"replay_simulator: {res['error']}"
    lines = [
        f"=== Replay simulator over {res['n_decisions']} recorded decisions "
        f"({res['n_policies']} routing policies) ===",
        f"{'policy':<12}{'n':>6}{'quality':>9}{'progress':>10}{'latency_ms':>12}{'cooldown':>10}{'score':>8}",
    ]
    for r in res["ranked"]:
        lines.append(
            f"{r['policy']:<12}{r['n']:>6}{r['quality']:>9}{r['mean_progress']:>10}"
            f"{r['avg_latency_ms']:>12}{r['cooldown_rate']:>10}{r['score']:>8}"
        )
    lines.append("")
    lines.append(f"VERDICT: {res['verdict']}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(_format_report(run_replay()))
