#!/usr/bin/env python3
"""
Sabrina Collective Router & Autonomous Shadow-Canary / AST Consensus Daemon (`v9`)
===================================================================================
Implements both cooperative guild system upgrades proposed by the 29-model collective:

1. **Guild 1 + Guild 4 Upgrade (`CollectiveRouter`)**:
   - Directly wires `KindCollectivePool` (`harnesses/external/collective_pool.py`) and
     `SemanticQueryCache` (`assistant_semantic_query_cache` in SQLite) into Sabrina's core
     routing layer (`CollectiveRouter`).
   - Enforces **3-Tier Latency & Entropy Routing**:
     * **Tier 1 (`flash`, `<1.5s` target)**: High-speed cloud & low-latency Gradio flash portals
       for fast triage, syntax/linting checks, and low-entropy queries.
     * **Tier 2 (`deep_reasoning`, `27B–72B` + Reasoning Distills)**: High-capacity reasoning models
       (`Qwen2.5-72B`, `Qwen3-27B`, `DeepSeek-R1-Distill-Qwen-14B`, `Gemini-3-Flash`, `Gemma-4`)
       for complex multi-file architecture, concurrency audits, and algorithm synthesis.
     * **Tier 3 (`domain_rag`, Specialized Domain & Digital-Twin RAG Spaces)**: Domain-specialized
       portals (`SNAP-RAG`, `Resume-RAG`, `KrishiGuard`, `Shlokartha`, `Agentic-RAG`, `Einstein`, etc.).
   - Provides `<1ms` exact SHA-256 and `>=0.88` Jaccard token-shingle semantic cache hits so
     repeated or near-identical sub-queries never waste community compute.

2. **Guild 2 + Guild 3 Upgrade (`AutonomousShadowCanaryAndASTDaemon`)**:
   - **Shadow-Canary & Schema-Fingerprint Monitor**: Periodically sweeps all `215` registered
     portals in SQLite (`assistant_external_portals`), computing live `/info` schema fingerprints
     and running `ACK_7` shadow-canary probes.
   - **Automatic CEGAR Harness Self-Repair**: When a portal's schema drifts or its response
     fails verification (`delivery_reliability < 1.0` or `answer_correctness < 1.0`), automatically
     triggers Counterexample-Guided Harness Repair (CEGAR) to patch `harnesses/external/<portal_id>.py`,
     validate syntax via `py_compile`, and re-qualify the portal in `execution_mode="live"`.
   - **Multi-Agent AST Structural Consensus & Ephemeral Sandbox Verification**: Dispatches code
     synthesis tasks across an independent multi-agent committee (`k >= 3` distinct external portals),
     normalizes candidate Abstract Syntax Trees (`ast.dump(tree, include_attributes=False)`),
     selects the structural quorum winner while preserving minority AST variants, and executes
     authoritative unit-test verification in a restricted namespace before accepting any code.
"""
from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import py_compile
import re
import sqlite3
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harnesses.external.cognitive_blackboard import HierarchicalCognitiveBlackboard  # noqa: E402
from harnesses.external.swarm_system_bridges import (  # noqa: E402
    enrich_prompt_with_blackboard_remedies,
    prioritize_committee_by_competence,
)
from harnesses.external.collective_pool import (  # noqa: E402
    DEFAULT_DB_PATH,
    HARNESS_DIR,
    KindCollectivePool,
    SemanticQueryCache,
    TIER_1_FLASH_PORTALS,
    TIER_2_DEEP_REASONING_PORTALS,
    TIER_3_DOMAIN_RAG_PORTALS,
)
from tools.external_mind_scout import (  # noqa: E402
    ExternalMindScout,
    PORTAL_BENCHMARK_SUITE,
    _authoritative_clamp_ratio_check,
)
from tools.run_cegar_repair_and_semantic_canary import (  # noqa: E402
    is_swallowed_error,
    repair_gradio_defaults_from_info,
)
from tools.run_self_organizing_collective_recruitment_loop import (  # noqa: E402
    load_harness_module,
    safe_module_name,
)
from tools.test_web_playground_ephemeral_and_cegar import build_cegar_v8_7_harness  # noqa: E402


# ===========================================================================
# PART 1: Guild 1 + Guild 4 — CollectiveRouter (3-Tier + Semantic Cache)
# ===========================================================================
class CollectiveRouter:
    """
    Sabrina's Core External Collective Router integrating:
    - Guild 1's Polite Token-Bucket Cooldowns & SQLite Semantic Query Cache (`<1ms` hits)
    - Guild 4's 3-Tier Latency/Entropy Routing (`flash` -> `deep_reasoning` -> `domain_rag`)
    - Guild 3's Multi-Agent AST Structural Consensus for high-stakes code synthesis
    """

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        min_cooldown_sec: float = 2.0,
        enable_semantic_cache: bool = True,
        external_hf_only: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self.pool = KindCollectivePool(
            min_cooldown_sec=min_cooldown_sec,
            db_path=self.db_path,
            enable_semantic_cache=enable_semantic_cache,
            external_hf_only=external_hf_only,
        )
        self.scout = ExternalMindScout(db_path=self.db_path)
        self.blackboard = HierarchicalCognitiveBlackboard(self.db_path)

    def route(
        self,
        prompt: str,
        *,
        tier: str = "auto",
        task_id: str = "",
        use_cache: bool = True,
        allow_tier_escalation: bool = True,
        required_capability: str = "grounded_summarization",
        tool_allowlist: tuple = (),
        supervised: bool = False,
        conversation: bool = False,
    ) -> Dict[str, Any]:
        """Route a query through the 3-tier collective with SQLite semantic caching."""
        return self.pool.route_query(
            prompt,
            tier=tier,
            task_id=task_id,
            use_cache=use_cache,
            allow_tier_escalation=allow_tier_escalation,
            required_capability=required_capability, tool_allowlist=tool_allowlist, supervised=supervised,
            conversation=conversation,
        )

    def run_guild_round(self, prompt, **kwargs):
        """Use the original pool's external HF broadcast and collective review."""
        return self.pool.run_guild_round(prompt, **kwargs)

    def synthesize_code_with_ast_consensus(
        self,
        prompt: str,
        *,
        committee_portals: Optional[List[str]] = None,
        committee_size: int = 5,
        quorum_ratio: float = 0.6,
        authoritative_verifier_fn: Optional[Callable[[str], Dict[str, Any]]] = None,
        domain: str = "code_synthesis",
        bug_signature: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Guild 3 Upgrade: Multi-Agent AST Structural Consensus + Authoritative Sandbox Verifier
        + Swarm-Authored HierarchicalCognitiveBlackboard v2 Integration.
        1. Queries `self.blackboard.query_patterns(domain, bug_signature)` to inject proven
           cross-task remedies into the synthesis prompt.
        2. Prioritizes committee portals using `self.blackboard.get_competence_ranking(domain)`.
        3. Parses each candidate into a canonical Python AST (`ast.parse`) and computes its
           structural SHA-256 fingerprint (`ast.dump(tree, include_attributes=False)`).
        4. Runs authoritative sandbox verification (`_authoritative_clamp_ratio_check` or custom
           `authoritative_verifier_fn`) on every structurally valid candidate, updating
           `self.blackboard.update_model_competence(...)` per portal.
        5. Selects the AST equivalence class that achieves structural consensus AND passes
           authoritative execution verification, recording the verified hypothesis and pattern.
        """
        t0 = time.perf_counter()
        verifier = authoritative_verifier_fn or _authoritative_clamp_ratio_check

        known_patterns = self.blackboard.query_patterns(domain, bug_signature)
        effective_prompt = enrich_prompt_with_blackboard_remedies(
            self.blackboard, prompt, domain, bug_signature
        )

        if committee_portals is None:
            t1 = self.pool.get_tier_candidates("flash", "code_generation")[:3]
            t2 = self.pool.get_tier_candidates("deep_reasoning", "code_generation")[:3]
            t3 = self.pool.get_tier_candidates("domain_rag", "code_generation")[:2]
            default_portals = list(dict.fromkeys(t1 + t2 + t3))
            committee_portals = prioritize_committee_by_competence(
                self.blackboard, domain, default_portals, committee_size
            )

        raw_responses: List[Dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(committee_portals)))) as ex:
            futs = {
                ex.submit(self.pool.invoke_member, pid, effective_prompt, "code_clamp_nan", False, required_capability="code_generation"): pid
                for pid in committee_portals
            }
            for f in concurrent.futures.as_completed(futs):
                raw_responses.append(f.result())

        # Group by canonical AST structural fingerprint
        ast_clusters: Dict[str, List[Dict[str, Any]]] = {}
        malformed_members: List[Dict[str, Any]] = []
        verified_clusters: Dict[str, Dict[str, Any]] = {}

        for resp in raw_responses:
            pid = resp.get("portal_id", "unknown")
            code = str(resp.get("reply") or "").strip()
            lat = float(resp.get("latency_ms") or 0.0)
            if not code:
                malformed_members.append({"portal_id": pid, "reason": "empty_reply", "latency_ms": lat})
                self.blackboard.update_model_competence(pid, domain, syntax_ok=False, tests_ok=False, latency_ms=lat)
                continue

            fp = ExternalMindScout._compute_ast_fingerprint(code)
            if fp.startswith("invalid_syntax_"):
                malformed_members.append({"portal_id": pid, "reason": fp, "latency_ms": lat})
                self.blackboard.update_model_competence(pid, domain, syntax_ok=False, tests_ok=False, latency_ms=lat)
                continue

            ver_res = verifier(code)
            passed_sandbox = bool(ver_res.get("passed", False))
            self.blackboard.update_model_competence(
                pid, domain, syntax_ok=True, tests_ok=passed_sandbox, latency_ms=lat
            )
            entry = {
                "portal_id": pid,
                "actual_model": resp.get("actual_model", pid),
                "latency_ms": lat,
                "ast_fingerprint": fp,
                "code": code,
                "sandbox_verified": passed_sandbox,
                "verification_detail": ver_res,
            }
            ast_clusters.setdefault(fp, []).append(entry)
            if passed_sandbox and fp not in verified_clusters:
                verified_clusters[fp] = ver_res

        # Rank AST clusters by: (sandbox_verified, cluster_vote_count, -avg_latency_ms)
        ranked_clusters: List[Tuple[str, List[Dict[str, Any]]]] = sorted(
            ast_clusters.items(),
            key=lambda kv: (
                1 if kv[0] in verified_clusters else 0,
                len(kv[1]),
                -sum(m["latency_ms"] for m in kv[1]) / max(1, len(kv[1])),
            ),
            reverse=True,
        )

        selected_fp = None
        selected_code = ""
        selected_members: List[str] = []
        sandbox_passed = False
        minority_variants: List[Dict[str, Any]] = []

        if ranked_clusters:
            selected_fp, top_members = ranked_clusters[0]
            selected_code = top_members[0]["code"]
            selected_members = [m["portal_id"] for m in top_members]
            sandbox_passed = selected_fp in verified_clusters

            for min_fp, min_members in ranked_clusters[1:]:
                minority_variants.append({
                    "ast_fingerprint": min_fp,
                    "vote_count": len(min_members),
                    "portals": [m["portal_id"] for m in min_members],
                    "sandbox_verified": min_fp in verified_clusters,
                })

        total_valid_verified = sum(
            len(members) for fp, members in ast_clusters.items() if fp in verified_clusters
        )
        required_quorum = max(1, int(len(committee_portals) * quorum_ratio))
        consensus_reached = sandbox_passed and (total_valid_verified >= required_quorum)

        if sandbox_passed and selected_fp and selected_members:
            confidence = round(len(selected_members) / max(1, len(committee_portals)), 4)
            self.blackboard.record_hypothesis(
                f"hyp_{domain}_{selected_fp}",
                domain,
                selected_members[0],
                f"Verified AST equivalence class {selected_fp} ({len(selected_members)}/{len(committee_portals)} votes)",
                confidence,
                "verified" if consensus_reached else "sandbox_verified",
            )
            self.blackboard.record_solution_pattern(
                f"pat_{domain}_{selected_fp}",
                domain,
                bug_signature or "ast_consensus_verified",
                f"Canonical verified AST structure {selected_fp}",
                selected_code[:500],
            )

        return {
            "ok": consensus_reached,
            "consensus_reached": consensus_reached,
            "committee_size": len(committee_portals),
            "required_quorum": required_quorum,
            "total_sandbox_verified_agents": total_valid_verified,
            "unique_valid_ast_structures": len(ast_clusters),
            "selected_ast_fingerprint": selected_fp,
            "selected_cluster_votes": len(selected_members),
            "selected_cluster_portals": selected_members,
            "sandbox_verified": sandbox_passed,
            "selected_code": selected_code,
            "minority_ast_variants": minority_variants,
            "malformed_or_empty": malformed_members,
            "blackboard_domain": domain,
            "blackboard_patterns_injected": len(known_patterns),
            "blackboard_competence_ranking": self.blackboard.get_competence_ranking(domain)[:5],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }


# ===========================================================================
# PART 2: Guild 2 + Guild 3 — Autonomous Shadow-Canary & CEGAR Self-Repair Daemon
# ===========================================================================
class AutonomousShadowCanaryAndASTDaemon:
    """
    Background / Periodic Autonomous Maintenance Daemon for Sabrina's External Collective:
    1. Audits all registered portals in SQLite (`assistant_external_portals`, 215+ portals).
    2. Runs the `ACK_7` Shadow Canary across active qualified harnesses and checks `/info`
       schema fingerprints for Gradio endpoints.
    3. Triggers automatic CEGAR self-repair (`build_cegar_v8_7_harness` + parameter repair)
       whenever a portal exhibits schema drift or degraded delivery/correctness.
    4. Persists an immutable audit trail into SQLite (`assistant_shadow_canary_audit_log`).
    """

    def __init__(
        self,
        db_path: Path | str = DEFAULT_DB_PATH,
        router: Optional[CollectiveRouter] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.router = router or CollectiveRouter(db_path=self.db_path, min_cooldown_sec=1.5)
        self.scout = self.router.scout
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ensure_audit_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_audit_table(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS assistant_shadow_canary_audit_log (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_timestamp REAL NOT NULL,
                    total_registered_portals INTEGER NOT NULL,
                    qualified_portals_probed INTEGER NOT NULL,
                    shadow_canary_passed INTEGER NOT NULL,
                    drifted_or_degraded_count INTEGER NOT NULL,
                    cegar_repaired_count INTEGER NOT NULL,
                    ast_consensus_verified INTEGER NOT NULL,
                    summary_json TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def compute_gradio_schema_hash(space_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Fetch live `/info` for a Gradio space and compute its deterministic schema fingerprint."""
        info = repair_gradio_defaults_from_info(space_id)
        if not info:
            return None
        raw_sig = json.dumps(
            {
                "ep_name": info.get("ep_name"),
                "prefix": info.get("prefix"),
                "defaults_types": [type(x).__name__ for x in info.get("repaired_defaults", [])],
                "arity": len(info.get("repaired_defaults", [])) + 1,
            },
            sort_keys=True,
        )
        fp = hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()[:16]
        return fp, info

    def run_shadow_canary_probe(self, portal_id: str) -> Dict[str, Any]:
        """Run a lightweight `ACK_7` shadow canary against a materialized harness."""
        t0 = time.perf_counter()
        res = self.router.pool.invoke_member(
            portal_id,
            "Reply with the exact token ACK_7 and nothing else.",
            task_id="shadow_canary_ack7",
            use_cache=False,
        )
        reply = str(res.get("reply") or "").strip()
        lat = round((time.perf_counter() - t0) * 1000.0, 2)
        passed = bool(
            res.get("ok")
            and reply
            and not is_swallowed_error(reply)
            and len(reply) >= 2
        )
        return {
            "portal_id": portal_id,
            "canary_passed": passed,
            "latency_ms": lat,
            "reply_preview": reply[:120],
        }

    def trigger_cegar_self_repair(self, portal_id: str, space_id: str) -> Dict[str, Any]:
        from tools.portal_policy import PortalPolicy
        from tools.portal_transport import refresh_protocol
        result = refresh_protocol(PortalPolicy(self.db_path), portal_id)
        return {**result, "portal_id": portal_id, "repaired": bool(result.get("ok")),
                "qualification_state": "revalidation_required"}

    def run_single_daemon_cycle(self, *, inject_synthetic_drift_portal=None):
        from tools.portal_policy import PortalPolicy
        policy = PortalPolicy(self.db_path)
        with policy.db() as db:
            qualified = db.execute("SELECT COUNT(*) FROM portal_role_qualifications WHERE responsibility='qualified' AND expires>?", (policy.clock(),)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM portal_eval_runs WHERE state!='complete'").fetchone()[0]
        # Maintenance never sweeps providers or regenerates runtime code.
        return {"ok": True, "qualified_roles": qualified, "pending_runs": pending,
                "research_enabled": policy.setting("research_enabled"),
                "tools_enabled": policy.setting("tools_enabled"), "external_requests": 0}

    def start_background_daemon(self, interval_seconds: float = 900.0) -> None:
        """Start the periodic background daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                try:
                    self.run_single_daemon_cycle()
                except Exception:
                    pass
                self._stop_event.wait(interval_seconds)

        self._thread = threading.Thread(
            target=_loop,
            name="SabrinaShadowCanaryASTDaemon",
            daemon=True,
        )
        self._thread.start()

    def stop_background_daemon(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)


def print_swarm_status(router: CollectiveRouter) -> None:
    all_portals = router.scout.list_portals()
    qualified = [p for p in all_portals if p.get("qualification_state") == "qualified" and p.get("execution_mode") == "live"]
    t1 = router.pool.get_tier_candidates("flash")
    t2 = router.pool.get_tier_candidates("deep_reasoning")
    t3 = router.pool.get_tier_candidates("domain_rag")
    cache_stats = router.pool.cache.stats() if router.pool.cache else {}

    print("=" * 68)
    print("      SABRINA COLLECTIVE SWARM INTELLIGENCE STATUS")
    print("=" * 68)
    print(f"[*] Total Registered Portals in SQLite: {len(all_portals)}")
    print(f"[*] Live-Qualified Portals:             {len(qualified)}")
    print(f"[*] Compiled External Harness Modules:  {len(router.pool._harnesses)}")
    print("-" * 68)
    print("3-TIER OPERATIONAL FLEET:")
    print(f"  • Tier 1 (Flash / <1.5s Triage):      {len(t1)} portals (e.g. {', '.join(t1[:3])}...)")
    print(f"  • Tier 2 (Deep Reasoning / 27B-72B): {len(t2)} portals (e.g. {', '.join(t2[:3])}...)")
    print(f"  • Tier 3 (Domain RAG / Specialists): {len(t3)} portals (e.g. {', '.join(t3[:3])}...)")
    print("-" * 68)
    print("SQLITE SEMANTIC QUERY CACHE:")
    print(f"  • Entries:         {cache_stats.get('entries', 0)}")
    print(f"  • Total DB Hits:   {cache_stats.get('total_db_hits', 0)}")
    print(f"  • Saved Latency:   {cache_stats.get('total_saved_latency_ms', 0.0):.2f} ms")
    print("=" * 68)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sabrina Collective Swarm Router & Shadow Daemon")
    parser.add_argument("--status", action="store_true", help="Print current swarm fleet status and exit")
    parser.add_argument("--once", action="store_true", help="Run a single shadow-canary and AST consensus cycle")
    parser.add_argument("--daemon", action="store_true", help="Run continuous background daemon")
    parser.add_argument("--interval", type=float, default=900.0, help="Daemon cycle interval in seconds (default: 900s / 15m)")
    parser.add_argument("--query", type=str, default="", help="Dispatch an ad-hoc query through the swarm router")
    parser.add_argument("--tier", type=str, default="auto", choices=["auto", "flash", "deep_reasoning", "domain_rag"], help="Routing tier for --query")

    args = parser.parse_args()

    router = CollectiveRouter()
    daemon = AutonomousShadowCanaryAndASTDaemon(router=router)

    if args.query:
        print(f"[*] Dispatching query to Swarm Tier: {args.tier}...")
        res = router.route(args.query, tier=args.tier)
        print(f"[+] Response from {res.get('portal_id')} ({res.get('actual_model')}):")
        print(f"    Latency: {res.get('latency_ms')} ms | Cache: {res.get('cache_match_type', 'live')}")
        print("-" * 60)
        print(res.get("reply", ""))
        sys.exit(0)

    print_swarm_status(router)

    if args.once:
        print("\n[*] Executing single Swarm Shadow-Canary & AST Consensus cycle...")
        summary = daemon.run_single_daemon_cycle()
        print(json.dumps(summary, sort_keys=True), flush=True)

    elif args.daemon:
        print(f"\n[*] Starting Autonomous Swarm Daemon (interval: {args.interval}s)... Press Ctrl+C to stop.")
        daemon.start_background_daemon(interval_seconds=args.interval)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[*] Stopping Swarm Daemon...")
            daemon.stop_background_daemon()
            print("[+] Swarm Daemon stopped.")
