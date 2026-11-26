"""Regression coverage for the Console harness update and routing entry points."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sabrina_console_desktop import sabrina_desktop_ui as desktop
from swarm_console.harness_collective import HarnessRegistry
from swarm_console.harness_updates import FACTORY_PROFILE, HarnessUpdates
from swarm_console.workspace_agent import WorkspaceAgent


class TestHarnessUpdates(unittest.TestCase):
    def test_json_handoff_can_extend_a_legacy_saved_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updates.sqlite3"
            updates = HarnessUpdates(path)
            old = updates.propose(FACTORY_PROFILE.revision, "Check evidence.", 4, "Saved profile")
            updates.probe(old["revision"])
            updates.activate(old["revision"])
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("DROP TABLE native_json_handoffs")
            reopened = HarnessUpdates(path)
            self.assertFalse(reopened.active_profile().json_handoff)
            new = reopened.propose(old["revision"], old["instructions"], old["max_turns"], "JSON handoff", True)
            result = reopened.probe(new["revision"])["probe"]
            self.assertTrue(result["passed"])
            self.assertTrue(result["candidate"]["checks"]["json_handoff_completed"])
            reopened.activate(new["revision"])
            self.assertTrue(HarnessUpdates(path).active_profile().json_handoff)
            self.assertFalse(reopened.get(old["revision"])["json_handoff"])
            self.assertEqual(reopened.rollback(new["revision"])["revision"], old["revision"])

    def test_probe_and_activation_require_a_current_base_and_restore_previous_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "updates.sqlite3"
            updates = HarnessUpdates(path)
            base = FACTORY_PROFILE.revision
            one_turn = updates.propose(base, "", 1, "Try a smaller budget.")["revision"]
            with self.assertRaisesRegex(ValueError, "fixture probe"):
                updates.activate(one_turn)
            failure = updates.probe(one_turn)["probe"]
            self.assertFalse(failure["passed"])
            self.assertFalse(failure["candidate"]["checks"]["read_completed"])
            with self.assertRaisesRegex(ValueError, "fixture probe"):
                updates.activate(one_turn)

            first = updates.propose(base, "Check existing files first.", 4, "Bound fixture turns.")["revision"]
            other = updates.propose(base, "Start with diagnostics.", 5, "Try another instruction.")["revision"]
            self.assertTrue(updates.probe(first)["probe"]["passed"])
            self.assertTrue(updates.probe(other)["probe"]["passed"])
            self.assertEqual(updates.active_profile().revision, base)
            updates.activate(first)

            reopened = HarnessUpdates(path)
            self.assertEqual(reopened.active_profile().revision, first)
            self.assertEqual(reopened.active_profile().max_turns, 4)
            with self.assertRaisesRegex(ValueError, "different revision"):
                reopened.activate(other)
            with self.assertRaisesRegex(ValueError, "Refresh"):
                reopened.rollback(base)
            reopened.rollback(first)
            self.assertEqual(updates.active_profile(), FACTORY_PROFILE)
            self.assertTrue(updates.get(other)["probe"]["passed"])

    def test_console_dispatches_selected_adapter_and_keeps_pending_native_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "project"
            workspace.mkdir()
            requests = []

            def reply(**request):
                requests.append(request)
                if request["input"] == "Draft an edit":
                    return SimpleNamespace(id="draft", steps=[{
                        "type": "function_call", "name": "write_file",
                        "arguments": {"path": "draft.txt", "content": "draft"},
                    }])
                return SimpleNamespace(id="response", steps=[], output_text="Workspace reply")

            client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
            with (
                patch.object(desktop, "ROOT", root),
                patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                patch.object(WorkspaceAgent, "_get_client", lambda agent: agent._client or client),
            ):
                bridge = desktop.SabrinaDesktopBridge(
                    role_bindings_path=root / "roles.json", vault_path=root / "vault.json",
                    llm_caller=lambda *_: {"ok": True, "text": "Portal reply", "provider": "fixture"},
                )
                try:
                    bridge._bind_workspace(workspace, "approve_writes")
                    original = bridge.send_chat("Draft an edit", "workspace_agent", "")
                    self.assertTrue(original["ok"], original.get("error"))
                    pending = original["pending_approvals"][0]["id"]
                    base = original["harness_revision"]
                    instructions = "Report diagnostics in a short checklist."
                    staged = bridge.propose_harness_update(base, instructions, 4, "Check prompt delivery.")
                    self.assertTrue(staged["ok"], staged.get("error"))
                    revision = staged["revision"]
                    self.assertTrue(bridge.probe_harness_update(revision)["probe"]["passed"])
                    self.assertTrue(bridge.activate_harness_update(revision)["ok"])

                    self.assertEqual(bridge.send_chat("Continue", "workspace_agent", "")["harness_revision"], base)
                    self.assertNotIn(instructions, requests[-1]["system_instruction"])
                    for adapter, message in (
                        ("mcp_harness", "MCP Tool-Bus executed"),
                        ("external_cli", "External CLI adapter verified"),
                        ("mlx_ego_portal", "Portal reply"),
                    ):
                        self.assertTrue(bridge.set_active_harness(adapter)["ok"])
                        result = bridge.send_chat("Inspect the workspace", "workspace_agent", "")
                        self.assertTrue(result["ok"], result.get("error"))
                        self.assertEqual(result["harness_id"], adapter)
                        self.assertIn(message, result["reply"])
                        self.assertEqual(bridge.get_workspace_status()["pending_approvals"][0]["id"], pending)

                    reopened = HarnessRegistry(updates=HarnessUpdates(root / "harness_updates.sqlite3"))
                    self.assertEqual(reopened.active_harness_id, "mlx_ego_portal")
                    self.assertTrue(bridge.decide_workspace_action(pending, False)["ok"])
                    self.assertFalse((workspace / "draft.txt").exists())
                    self.assertTrue(bridge.set_active_harness("sabrina_native")["ok"])
                    updated = bridge.send_chat("Continue", "workspace_agent", "")
                    self.assertEqual(updated["harness_revision"], revision)
                    self.assertIn(instructions, requests[-1]["system_instruction"])
                    self.assertNotIn("previous_interaction_id", requests[-1])
                    self.assertEqual(bridge.workspace_agent.max_turns, 4)

                    native_turn = bridge.run_harness_turn("Verify profile", "sabrina_native")
                    self.assertEqual(native_turn["reply"], "Workspace reply")
                    self.assertIn(instructions, requests[-1]["system_instruction"])
                    self.assertTrue(bridge.rollback_harness_update(revision)["ok"])
                    self.assertEqual(bridge.send_chat("Continue", "workspace_agent", "")["harness_revision"], base)
                    self.assertNotIn(instructions, requests[-1]["system_instruction"])
                finally:
                    bridge.swarm_runner.shutdown()
