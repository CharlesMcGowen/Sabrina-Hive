"""Tests for live telemetry binding to swarm graph nodes in Sabrina Swarm Command Center."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from sabrina_console_desktop.tools.telemetry_blackboard import TelemetryBlackboard, get_blackboard_db_path


class TestLiveTelemetryGraph(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.ws_dir = self.root / "workspace"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        (self.ws_dir / "test_module.py").write_text("def test_auth(): pass\n", encoding="utf-8")
        self.dnd_db = self.root / "dnd_rsi_blackboard.sqlite3"

    def tearDown(self) -> None:
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_telemetry_blackboard_persistence_and_indexing_by_agent_id(self) -> None:
        bb = TelemetryBlackboard(self.dnd_db)
        rec1 = bb.record_telemetry(
            agent_id="dyn-scout-01",
            state="ACTIVE",
            mode="DYNAMIC",
            model="gemini-2.5-pro",
            logprob=-0.14,
            gate_status="PASS",
            task="Inspect workspace context",
            command="rg \"auth\" src/",
            parent_id="sabrina",
            depth=1,
            action="Started file scan",
        )
        self.assertEqual(rec1["agent_id"], "dyn-scout-01")
        self.assertEqual(rec1["mode"], "DYNAMIC")
        self.assertEqual(rec1["state"], "ACTIVE")
        self.assertEqual(rec1["command"], "rg \"auth\" src/")
        self.assertEqual(rec1["gate_status"], "PASS")

        # Second transition for same agent
        rec2 = bb.record_telemetry(
            agent_id="dyn-scout-01",
            state="VALIDATING",
            mode="DYNAMIC",
            model="gemini-2.5-pro",
            logprob=-0.12,
            gate_status="VALIDATING",
            task="Validating citations",
            command="rg \"auth\" src/",
            parent_id="sabrina",
            depth=1,
            action="Validating citations",
        )
        self.assertEqual(rec2["state"], "VALIDATING")
        self.assertEqual(len(rec2["action_trail"]), 2)

        # Pull latest record per agent_id
        all_tel = bb.get_latest_telemetry_by_agent()
        self.assertIn("dyn-scout-01", all_tel)
        latest = all_tel["dyn-scout-01"]
        self.assertEqual(latest["state"], "VALIDATING")
        self.assertEqual(latest["gate_status"], "VALIDATING")
        self.assertEqual(latest["command"], "rg \"auth\" src/")
        self.assertEqual(len(latest["action_trail"]), 2)

    def test_live_swarm_runner_spawns_with_dyn_id_and_operational_fields(self) -> None:
        runner = LiveSwarmRunner(
            workspace_root=self.ws_dir,
            artifacts_dir=self.root / "artifacts",
            step_delay_sec=0.05,
            auto_spindown=False,
        )
        try:
            # Spawn dynamic child with shell command task
            res = runner.spawn_worker(
                name="Security Auditor",
                role="research_scout",
                task="rg \"auth\" src/",
            )
            self.assertTrue(res["ok"])
            worker_id = res["worker_id"]
            self.assertTrue(worker_id.startswith("dyn-scout-"), f"Worker ID must match dyn-<role>-<seq>: {worker_id}")

            # Check runner state
            state = runner.get_state()
            self.assertEqual(state["active_worker_count"], 1)
            w = state["workers"][0]
            self.assertEqual(w["agent_id"], worker_id)
            self.assertEqual(w["mode"], "DYNAMIC")
            self.assertIn(w["state"], ("SPAWNING", "ACTIVE", "VALIDATING", "PASS"))
            self.assertEqual(w["command"], "rg \"auth\" src/")
            self.assertEqual(w["active_command"], "rg \"auth\" src/")
            self.assertEqual(w["parent_id"], "sabrina")
            self.assertEqual(w["depth"], 1)
            self.assertTrue(len(w["action_trail"]) >= 1)

            # Telemetry by agent must be indexed and available
            self.assertIn(worker_id, state["agent_telemetry"])
            agent_rec = state["agent_telemetry"][worker_id]
            self.assertEqual(agent_rec["mode"], "DYNAMIC")
            self.assertEqual(agent_rec["command"], "rg \"auth\" src/")

            # Council nodes must be present with mode SEEDED
            nodes = state["nodes"]
            for node_name in ("sabrina", "codex", "antigravity", "claude"):
                self.assertIn(node_name, nodes)
                n = nodes[node_name]
                self.assertEqual(n["mode"], "SEEDED")
                self.assertIn("command", n)
                self.assertIn("active_command", n)
                self.assertIn("gate_status", n)
        finally:
            runner.shutdown()

    def test_bridge_snapshot_binds_telemetry_without_mixing_operator_chat(self) -> None:
        bridge = SabrinaDesktopBridge(
            role_bindings_path=self.root / "role_bindings.json",
            vault_path=self.root / "provider_vault.json",
            custom_models_path=self.root / "custom_models.json",
            swarm_artifacts_dir=self.root / "artifacts",
            ui_html_path=self.root / "ui.html",
        )
        # Add operator chat messages
        bridge.chat_log.append({
            "sender": "operator",
            "text": "Operator secret message: please audit the security module",
            "time": time.strftime("%H:%M:%S"),
        })

        # Spawn a worker with recorded command
        spawn_res = bridge.swarm_runner.spawn_worker(
            name="Test Worker",
            role="worker",
            task="pytest -q",
        )
        self.assertTrue(spawn_res["ok"])
        wid = spawn_res["worker_id"]
        self.assertTrue(wid.startswith("dyn-worker-"))

        # Snapshot
        snap = bridge.get_snapshot()
        self.assertIn("agent_telemetry", snap)
        tel = snap["agent_telemetry"]
        self.assertIn(wid, tel)
        w_tel = tel[wid]

        # Verify operational fields
        self.assertEqual(w_tel["agent_id"], wid)
        self.assertEqual(w_tel["mode"], "DYNAMIC")
        self.assertIn(w_tel["state"], ("SPAWNING", "ACTIVE", "VALIDATING", "PASS"))
        self.assertEqual(w_tel["command"], "pytest -q")
        self.assertEqual(w_tel["active_command"], "pytest -q")

        # Crucial: verify operator chat is NOT mixed into the agent's telemetry or action trail!
        for action in w_tel.get("action_trail", []):
            action_text = json.dumps(action)
            self.assertNotIn("Operator secret message", action_text)

        # Operator chat history remains strictly in chat_history, isolated from telemetry
        self.assertEqual(len(snap["chat_history"]), 1)
        self.assertEqual(snap["chat_history"][0]["text"], "Operator secret message: please audit the security module")
        self.assertEqual(len(bridge.chat_log), 1)

        # Confirm no node in swarm["nodes"] or swarm["workers"] or agent_telemetry contains the chat text
        for node in snap["swarm"]["nodes"].values():
            self.assertNotIn("Operator secret message", str(node.get("command") or ""))
            self.assertNotIn("Operator secret message", str(node.get("active_command") or ""))
            self.assertNotIn("Operator secret message", str(node.get("task") or ""))

        for worker in snap["swarm"]["workers"]:
            self.assertNotIn("Operator secret message", str(worker.get("command") or ""))
            self.assertNotIn("Operator secret message", str(worker.get("active_command") or ""))
            self.assertNotIn("Operator secret message", str(worker.get("task") or ""))

        for agent_id, tel_record in snap["agent_telemetry"].items():
            self.assertNotIn("Operator secret message", str(tel_record.get("command") or ""))
            self.assertNotIn("Operator secret message", str(tel_record.get("task") or ""))

        # Cleanup
        bridge.swarm_runner.shutdown()

    def test_html_contains_compact_command_and_inspector_wiring(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        html_content = (repo_root / "sabrina_console_desktop" / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        
        # Verify inspector element IDs
        for expected_id in (
            "topo-inspect-state",
            "topo-inspect-mode",
            "topo-inspect-name",
            "topo-inspect-meta",
            "topo-inspect-logprob",
            "topo-inspect-gate",
            "topo-inspect-vault",
            "topo-inspect-lineage",
            "topo-inspect-command",
            "topo-inspect-provenance",
            "topo-inspect-task",
            "topo-inspect-trail",
        ):
            self.assertIn(f'id="{expected_id}"', html_content)

        # Verify dynamic provenance badge and truncated command snippets in SVG builder
        self.assertIn("cmdSnippet", html_content)
        self.assertIn("modeBadgeText", html_content)
        self.assertIn("TIMESTAMPED ACTION TRAIL", html_content)
        self.assertIn("ACTIVE COMMAND:", html_content)


if __name__ == "__main__":
    unittest.main()
