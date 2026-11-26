"""Autonomous Collective Swarm Daemon for Sabrina.

Continuously discovers, recruits, benchmarks, health-checks, and tournaments
across all global and Chinese AI chat portals and inference endpoints,
maintaining the collective as a unified swarm intelligence for Sabrina.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.external_mind_scout import ExternalMindScout
from tools.capability_registry import CapabilityRegistry
from tools.oracle_providers import UnifiedOracleHub
from tools.sabrina_workflow import DEFAULT_DB

LOGGER = logging.getLogger("sabrina.collective_swarm_daemon")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

EXPLORATION_MISSIONS = [
    ("deepseek-chat-china", "emerging Chinese MoE and coding reasoning portals"),
    ("kimi-moonshot-china", "East Asian and pan-Pacific long-context AI chat services"),
    ("mistral-le-chat", "European sovereign open models and sovereign inference endpoints"),
    ("perplexity-ai", "new zero-login global AI search and chat interfaces 2026"),
    ("groq-fast-inference", "high-throughput serverless LPU and GPU open model gateways"),
    ("tongyi-qwen-china", "Chinese enterprise code generation and automated testing agents"),
]


class CollectiveSwarmDaemon:
    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = db_path
        self.scout = ExternalMindScout(self.db_path)
        self.hub = UnifiedOracleHub()
        self.running = True

    def run_scouting_cycle(self) -> dict[str, Any]:
        """Run one full cycle of distributed recruitment across external recruiters."""
        new_recruits = 0
        cycle_details = []

        for recruiter_id, topic in EXPLORATION_MISSIONS:
            try:
                res = self.scout.recruit_portals_via_external_minds(
                    recruiter_portal_id=recruiter_id,
                    query_topic=topic,
                )
                if res.get("ok"):
                    recruited = res.get("newly_qualified_recruits", [])
                    new_recruits += len(recruited)
                    cycle_details.append({
                        "recruiter": recruiter_id,
                        "topic": topic,
                        "recruited_count": len(recruited),
                        "recruits": [r["portal_name"] for r in recruited],
                    })
            except Exception as e:
                LOGGER.warning(f"Recruitment mission via {recruiter_id} failed: {e}")

        return {
            "cycle_timestamp": time.time(),
            "new_recruits_count": new_recruits,
            "missions": cycle_details,
        }

    def run_tournament_cycle(self, stage_ladder: list[tuple[int, int]] | None = None) -> dict[str, Any]:
        """Run an automated scaling tournament across the collective on an engineering challenge."""
        ladder = stage_ladder or [(1, 1), (4, 4), (8, 8), (16, 16)]
        challenge = (
            "Verify thread-safe TokenBucket rate limiter, monotonic clock progression, "
            "and IEEE-754 NaN parameter rejection invariants in Python."
        )
        return self.scout.run_scaling_cross_test_matrix(
            stages=ladder,
            challenge_prompt=challenge,
        )

    def execute_loop(self, max_cycles: int = 3, interval_seconds: float = 2.0) -> dict[str, Any]:
        """Execute autonomous collective expansion cycles."""
        LOGGER.info(f"Starting Collective Swarm Daemon (Max Cycles: {max_cycles})...")
        history = []

        for cycle in range(1, max_cycles + 1):
            LOGGER.info(f"--- [COLLECTIVE CYCLE {cycle}/{max_cycles}] ---")
            
            # 1. Scouting & Recruitment
            scout_report = self.run_scouting_cycle()
            LOGGER.info(f"Scouting finished: {scout_report['new_recruits_count']} new portals qualified.")

            # 2. Tournament Cross-Testing
            tourn_report = self.run_tournament_cycle()
            LOGGER.info(f"Tournament finished: Verdict = {tourn_report['total_verdict']} across {tourn_report['stages_executed']} stages.")

            total_portals = len(self.scout.list_portals())
            LOGGER.info(f"Current Collective Size: {total_portals} registered AI models.")

            history.append({
                "cycle": cycle,
                "scout_report": scout_report,
                "tourn_report": tourn_report,
                "total_portals": total_portals,
            })

            if cycle < max_cycles:
                time.sleep(interval_seconds)

        return {
            "ok": True,
            "cycles_completed": len(history),
            "final_collective_size": len(self.scout.list_portals()),
            "history": history,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Collective Swarm Daemon")
    parser.add_argument("--cycles", type=int, default=1, help="Number of continuous cycles to execute")
    parser.add_argument("--interval", type=float, default=1.0, help="Interval between cycles in seconds")
    args = parser.parse_args()

    daemon = CollectiveSwarmDaemon()
    res = daemon.execute_loop(max_cycles=args.cycles, interval_seconds=args.interval)
    print(json.dumps({
        "ok": res["ok"],
        "cycles_completed": res["cycles_completed"],
        "final_collective_size": res["final_collective_size"],
    }, indent=2))


if __name__ == "__main__":
    main()
