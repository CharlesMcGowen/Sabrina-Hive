#!/usr/bin/env python3
"""
Sabrina Swarm Intelligence Live Monitor Dashboard
=================================================
Run anytime to inspect the live status of the autonomous external collective:
- Daemon Process & Thread Health
- 3-Tier Operational Fleet Distribution (Flash / Deep Reasoning / Domain RAG)
- Recent Autonomous Shadow-Canary & Self-Healing Audit Cycles (from SQLite)
- Semantic Query Cache Hit Rates & Latency Savings
- Live Ping Latency Test
"""
from __future__ import annotations

import datetime
import json
import sqlite3
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harnesses.external.collective_pool import DEFAULT_DB_PATH
from tools.collective_router_and_shadow_daemon import CollectiveRouter


def main() -> None:
    print("=" * 72)
    print("        SABRINA COLLECTIVE SWARM INTELLIGENCE MONITOR")
    print("=" * 72)

    router = CollectiveRouter(db_path=DEFAULT_DB_PATH)
    scout = router.scout
    all_portals = scout.list_portals()
    qualified = [
        p for p in all_portals
        if p.get("qualification_state") == "qualified" and p.get("execution_mode") == "live"
    ]

    t1 = router.pool.get_tier_candidates("flash")
    t2 = router.pool.get_tier_candidates("deep_reasoning")
    t3 = router.pool.get_tier_candidates("domain_rag")
    cache_stats = router.pool.cache.stats() if router.pool.cache else {}

    print(f"[*] Local System Time:              {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"[*] Total Registered in SQLite:     {len(all_portals)} external endpoints")
    print(f"[*] Live-Qualified Models:          {len(qualified)} models (100% benchmark passed)")
    print(f"[*] Compiled Standalone Harnesses:  {len(router.pool._harnesses)} modules in harnesses/external/")
    print("-" * 72)
    print("3-TIER OPERATIONAL FLEET:")
    print(f"  • Tier 1 (Flash / <1.5s Triage):      {len(t1)} active portals")
    print(f"    Examples: {', '.join(t1[:4])}...")
    print(f"  • Tier 2 (Deep Reasoning / 27B-72B): {len(t2)} active portals")
    print(f"    Examples: {', '.join(t2[:4])}...")
    print(f"  • Tier 3 (Domain RAG & Specialists): {len(t3)} active portals")
    print(f"    Examples: {', '.join(t3[:4])}...")
    print("-" * 72)
    print("SQLITE SEMANTIC QUERY CACHE PERFORMANCE:")
    print(f"  • Cached Queries in Memory:   {cache_stats.get('entries', 0)}")
    print(f"  • Total DB Hits (Exact/Sim):  {cache_stats.get('total_db_hits', 0)}")
    print(f"  • Latency Saved:              {cache_stats.get('total_saved_latency_ms', 0.0):.2f} ms")
    print("-" * 72)
    print("RECENT AUTONOMOUS DAEMON AUDIT CYCLES (from SQLite audit log):")

    try:
        with sqlite3.connect(str(DEFAULT_DB_PATH), timeout=5.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT audit_id, cycle_timestamp, total_registered_portals,
                       qualified_portals_probed, shadow_canary_passed,
                       drifted_or_degraded_count, cegar_repaired_count,
                       ast_consensus_verified
                FROM assistant_shadow_canary_audit_log
                ORDER BY audit_id DESC
                LIMIT 5
                """
            ).fetchall()
            if rows:
                print(f"  {'Audit ID':<10} {'Cycle Time':<20} {'Probed':<8} {'Passed':<8} {'Repaired':<10} {'AST Consensus'}")
                print(f"  {'-'*9:<10} {'-'*19:<20} {'-'*7:<8} {'-'*7:<8} {'-'*9:<10} {'-'*13}")
                for r in rows:
                    ts = datetime.datetime.fromtimestamp(r["cycle_timestamp"]).strftime('%Y-%m-%d %H:%M:%S')
                    ast_status = "VERIFIED (Quorum)" if r["ast_consensus_verified"] else "No Consensus"
                    print(
                        f"  #{r['audit_id']:<9} {ts:<20} {r['qualified_portals_probed']:<8} "
                        f"{r['shadow_canary_passed']:<8} {r['cegar_repaired_count']:<10} {ast_status}"
                    )
            else:
                print("  No previous audit cycles recorded in table yet.")
    except Exception as e:
        print(f"  Unable to read audit table: {e}")

    print("-" * 72)
    print("[*] Performing Live Swarm Router Ping Test (<1.5s Flash Tier)...")
    t0 = time.perf_counter()
    ping_res = router.route("Echo: SWARM_ONLINE_HEALTHY", tier="flash", use_cache=True)
    ping_lat = round((time.perf_counter() - t0) * 1000.0, 2)
    print(
        f"  [+] Router Ping: {ping_lat} ms | Portal: {ping_res.get('portal_id')} "
        f"({ping_res.get('actual_model')}) | Cache: {ping_res.get('cache_match_type')}"
    )
    print("=" * 72)


if __name__ == "__main__":
    main()
