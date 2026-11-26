"""Tests for Crypto Market Research Swarm Topology and Telemetry Transition Traces."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from sabrina_console_desktop.swarm_schema_audit import audit_swarm_payload
from sabrina_console_desktop.tools.telemetry_blackboard import TelemetryBlackboard, get_telemetry_blackboard


class TestCryptoMarketResearchTopology(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.schema_path = (
            Path(__file__).resolve().parent / "data" / "control" / "crypto_market_research_schema.json"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_crypto_market_research_schema_invariants_pass(self) -> None:
        """Confirm that the Crypto Market Research topology satisfies INV-01 through INV-05."""
        self.assertTrue(self.schema_path.is_file(), f"Missing schema file: {self.schema_path}")
        raw = json.loads(self.schema_path.read_text(encoding="utf-8"))
        res = audit_swarm_payload(raw)
        self.assertTrue(res.valid, f"Schema audit failed: {res.errors}")
        self.assertEqual(res.errors, [])

        inv_map = {inv["id"]: inv["status"] for inv in res.invariants}
        self.assertEqual(inv_map["INV-01"], "PASS", "INV-01 Root Reachability must pass")
        self.assertEqual(inv_map["INV-02"], "PASS", "INV-02 Bounded Termination must pass")
        self.assertEqual(inv_map["INV-04"], "PASS", "INV-04 Confidence Gate must pass")

    def test_telemetry_transition_traces_logged_in_blackboard(self) -> None:
        """Verify that A2A edge hops and critic rubrics are persisted in dnd_rsi_blackboard SQLite."""
        db_path = self.tmp_path / "test_blackboard.sqlite3"
        tb = TelemetryBlackboard(db_path)

        runner = LiveSwarmRunner(artifacts_dir=self.tmp_path, workspace_root=Path("."))
        runner._telemetry_bb = tb

        def fake_llm(role: str, model: str, system: str, prompt: str) -> dict[str, object]:
            return {
                "ok": True,
                "text": f"Deliverable from role={role} model={model}",
                "tokens_used": 120,
                "latency_ms": 45.0,
            }

        runner.llm_caller = fake_llm
        res = runner.launch_context_first_mission(
            "Run crypto quantitative market strategy research pipeline",
            blueprint={
                "playbook": "parallel_research_swarm",
                "role_bindings": {
                    "planner_architect": "gemini-4-neon",
                    "research_scout": "gemini-4-hydrogen",
                    "execution_specialist": "gemini-4-argon",
                    "critic_verifier": "gemini-4-neon",
                },
                "stop_condition": "All tests pass",
            },
        )
        self.assertTrue(res.get("ok"), f"Launch mission failed: {res.get('error')}")
        mid = str(res.get("mission_id"))
        runner.wait_for_mission(mid, timeout=10.0)

        traces = tb.get_mission_transition_traces(mid)
        self.assertEqual(len(traces), 4, f"Expected 4 transition traces for 4-stage run, got {len(traces)}")

        rule_ids = [t["rule_id"] for t in traces]
        self.assertIn("DR1_ORCHESTRATOR_DISPATCH", rule_ids)
        self.assertIn("DR2_DATA_RECON_FANOUT", rule_ids)
        self.assertIn("DR3_EXEC_SYNTHESIS_STAGE", rule_ids)
        self.assertIn("DR4_CRITIC_INVARIANT_GATE", rule_ids)

        critic_trace = next(t for t in traces if t["rule_id"] == "DR4_CRITIC_INVARIANT_GATE")
        self.assertIsNotNone(critic_trace.get("critic_rubric"))
        rubric = critic_trace["critic_rubric"]
        self.assertIn("goal_completeness", rubric)
        self.assertIn("ast_syntax_valid", rubric)
        self.assertIn("citations_verified", rubric)
        self.assertIn("execution_authorized_locked", rubric)
        self.assertEqual(rubric["execution_authorized_locked"], 1.0)

    def test_bridge_loads_preset_and_queries_traces(self) -> None:
        """Verify bridge method can activate the crypto research preset and query traces."""
        bridge = SabrinaDesktopBridge(role_bindings_path=self.tmp_path / "role_bindings.json")
        res = bridge.load_preset_swarm_schema("crypto_market_research")
        self.assertTrue(res.get("ok"), f"Preset load failed: {res.get('error')}")

        schema = bridge.get_swarm_schema()
        self.assertTrue(schema.get("ok"))
        self.assertIn("Crypto Market Strategy", schema["schema"]["swarm_name"])

        trace_query = bridge.get_transition_traces("non_existent_mission")
        self.assertTrue(trace_query.get("ok"))
        self.assertEqual(trace_query.get("traces"), [])


if __name__ == "__main__":
    unittest.main()
