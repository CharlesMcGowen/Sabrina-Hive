"""Read-only LinkedIn performance and action report."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path


METRIC_NAMES = ("impressions", "reactions", "comments", "reposts", "saves",
                "sends", "profile_viewers", "followers")


def _read_verified_snapshots(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            """SELECT s.experiment_id, s.window, s.evidence, s.reward,
                      p.platform, p.account, p.post_id
                 FROM snapshots AS s JOIN publications AS p
                   ON p.experiment_id=s.experiment_id
                WHERE p.platform='linkedin'
                ORDER BY s.experiment_id, s.window"""
        ).fetchall()
    result = []
    for row in rows:
        evidence = json.loads(row["evidence"] or "{}")
        if evidence.get("verified") is not True:
            continue
        result.append({
            "experiment_id": row["experiment_id"],
            "window": row["window"],
            "account": row["account"],
            "post_id": row["post_id"],
            "observed_at": evidence.get("observed_at"),
            "metrics": evidence.get("metrics") or {},
            "reward": row["reward"],
        })
    return result


def linkedin_performance_report(runtime, payload: dict | None = None, now: float | None = None) -> dict:
    """Summarize imported verified metrics and the local operation journal; never scrapes LinkedIn."""
    payload = payload or {}
    try:
        days = max(1, min(90, int(payload.get("days", 30))))
    except (TypeError, ValueError):
        raise ValueError("days must be an integer from 1 to 90")
    now = time.time() if now is None else now
    since = now - days * 86400
    research_path = Path(os.environ.get(
        "SABRINA_SOCIAL_EXPERIMENT_DB", runtime.data_dir / "experiments.sqlite3"))
    snapshots = _read_verified_snapshots(research_path)
    snapshots = [s for s in snapshots if isinstance(s.get("observed_at"), (int, float))
                 and since <= s["observed_at"] <= now + 300]

    buckets: dict[str, list[dict]] = defaultdict(list)
    for snapshot in snapshots:
        buckets[snapshot["window"]].append(snapshot)
    by_window = {}
    for window, rows in sorted(buckets.items()):
        means = {}
        for name in METRIC_NAMES:
            values = [r["metrics"].get(name) for r in rows
                      if type(r["metrics"].get(name)) is int and r["metrics"].get(name) >= 0]
            means[name] = round(sum(values) / len(values), 2) if values else None
        by_window[window] = {"verified_snapshot_count": len(rows), "mean_metrics": means}

    operations = [op for op in runtime.store.operations(limit=500)
                  if op.get("created", 0) >= since and op.get("created", 0) <= now + 300]
    counts = Counter(op.get("kind", "unknown") + ":" + op.get("status", "unknown")
                     for op in operations)
    verified_actions = sum(
        1 for op in operations
        if isinstance(op.get("result"), dict) and op["result"].get("verified") is True
    )
    latest_observation = max((s["observed_at"] for s in snapshots), default=None)
    age = max(0, now - latest_observation) if latest_observation is not None else None
    freshness = "no_imported_metrics" if age is None else "current" if age <= 8 * 86400 else "stale"
    return {
        "status": "report_ready",
        "report": "linkedin_performance",
        "observed_at": now,
        "window_days": days,
        "source": "verified_imported_snapshots_and_local_operation_journal",
        "metrics_freshness": freshness,
        "latest_metrics_observed_at": latest_observation,
        "latest_metrics_age_seconds": age,
        "verified_snapshot_count": len(snapshots),
        "verified_posts_with_metrics": len({s["experiment_id"] for s in snapshots}),
        "metrics_by_window": by_window,
        "recent_operations": {
            "count": len(operations),
            "verified_action_count": verified_actions,
            "counts_by_kind_and_status": dict(sorted(counts.items())),
            "latest": [{"operation_id": op["id"], "kind": op["kind"],
                        "status": op["status"], "created": op["created"],
                        "verified": bool((op.get("result") or {}).get("verified"))}
                       for op in sorted(operations, key=lambda item: item.get("created", 0), reverse=True)[:20]],
        },
        "read_only": True,
        "collection_note": "LinkedIn metrics come from verified imported snapshots. This report does not open LinkedIn or collect fresh analytics.",
        "summary": (f"LinkedIn report: {len(snapshots)} verified metric snapshots across "
                    f"{len({s['experiment_id'] for s in snapshots})} posts; "
                    f"{len(operations)} recorded operations in {days} days. Metrics: {freshness}."),
    }
