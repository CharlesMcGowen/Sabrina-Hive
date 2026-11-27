"""Regression tests for fast operator chat and native tool routing."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sabrina_console_desktop import sabrina_desktop_ui as desktop
from swarm_console.workspace_agent import WorkspaceAgent
from swarm_console.workspace_sandbox import WorkspaceSandbox


class OperatorChatTests(unittest.TestCase):
    def make_bridge(self, root: Path) -> desktop.SabrinaDesktopBridge:
        workspace = root / "project"
        workspace.mkdir()
        bridge = desktop.SabrinaDesktopBridge(
            role_bindings_path=root / "roles.json", vault_path=root / "vault.json",
            llm_caller=lambda *_: {"ok": True, "text": "fixture", "provider": "fixture"},
            coordinator_only=True,
        )
        bridge._bind_workspace(workspace, "read_only")
        self.addCleanup(bridge.swarm_runner.shutdown)
        return bridge

    def test_fast_chat_really_reads_a_file_and_uses_low_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = []

            def create(**request):
                requests.append(request)
                if isinstance(request["input"], str):
                    return SimpleNamespace(id="read", steps=[{
                        "type": "function_call", "id": "fc-read", "name": "read_file",
                        "arguments": {"path": "note.txt"},
                    }])
                observation = json.loads(request["input"][0]["result"])
                return SimpleNamespace(id="done", steps=[], output_text=observation["content"])

            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root), \
                    patch.object(WorkspaceAgent, "_get_client", lambda self: client):
                bridge = self.make_bridge(root)
                (root / "project" / "note.txt").write_text("violet apple 742", encoding="utf-8")
                result = bridge.send_chat("Read note.txt", "sabrina", "hive_council")
                self.assertTrue(result["ok"], result.get("error"))
                self.assertIn("violet apple 742", result["reply"])
                self.assertEqual([(e["tool"], e["status"]) for e in result["tool_events"]], [("read_file", "executed")])
                self.assertEqual(requests[0]["generation_config"]["thinking_level"], "low")
                self.assertIn("get_console_info", [tool["name"] for tool in requests[0]["tools"]])

    def test_self_inspection_reports_actual_mode_and_tool_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observed = []

            def create(**request):
                if isinstance(request["input"], str):
                    return SimpleNamespace(id="inspect", steps=[{
                        "type": "function_call", "id": "fc-info", "name": "get_console_info",
                        "arguments": {"section": "tools"},
                    }])
                observed.append(json.loads(request["input"][0]["result"]))
                return SimpleNamespace(id="done", steps=[], output_text="I can read files; writes are blocked.")

            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root), \
                    patch.object(WorkspaceAgent, "_get_client", lambda self: client):
                bridge = self.make_bridge(root)
                result = bridge.send_chat("What can you do?", "sabrina", "")
                self.assertTrue(result["ok"])
                self.assertEqual(result["tool_events"][0]["tool"], "get_console_info")
                info = observed[0]
                self.assertEqual(info["workspace"]["mode"], "read_only")
                self.assertEqual(info["workspace"]["root"], str(root / "project"))
                states = {tool["name"]: tool["state"] for tool in info["tools"]}
                self.assertIn("available", states["read_file"])
                self.assertIn("blocked", states["write_file"])
                self.assertIn("Plugin discovery is not plugin execution", info["plugin_access"])

    def test_native_chat_can_read_a_registry_skill_and_then_use_workspace_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "coding-implementation"
            skill.mkdir(parents=True)
            guidance = "---\nname: coding-implementation\ndescription: Implement code changes.\n---\nRead note.txt before answering.\n"
            (skill / "SKILL.md").write_text(guidance, encoding="utf-8")
            requests = []

            def create(**request):
                requests.append(request)
                if isinstance(request["input"], str):
                    return SimpleNamespace(id="skill", steps=[{
                        "type": "function_call", "id": "fc-skill", "name": "get_console_info",
                        "arguments": {"section": "skill_detail", "query": "coding-implementation"},
                    }])
                observation = json.loads(request["input"][0]["result"])
                if "skill" in observation:
                    self.assertEqual(observation["skill"]["instructions"], guidance)
                    return SimpleNamespace(id="read", steps=[{
                        "type": "function_call", "id": "fc-read", "name": "read_file",
                        "arguments": {"path": "note.txt"},
                    }])
                return SimpleNamespace(id="done", steps=[], output_text=observation["content"])

            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root), \
                    patch.object(WorkspaceAgent, "_get_client", lambda self: client):
                bridge = self.make_bridge(root)
                (root / "project" / "note.txt").write_text("observed fixture", encoding="utf-8")
                result = bridge.send_chat("Use coding-implementation to inspect note.txt", "sabrina", "")
                self.assertTrue(result["ok"], result.get("error"))
                self.assertIn("observed fixture", result["reply"])
                self.assertEqual([event["tool"] for event in result["tool_events"]], ["get_console_info", "read_file"])
                self.assertEqual(bridge.workspace_mode, "read_only")

    def test_registry_skill_details_are_bounded_paginated_and_catalog_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "long-skill"
            skill.mkdir(parents=True)
            guidance = '---\nname: long-skill\ndescription: Long fixture.\n---\n' + ('"quoted" path\\file\n' * 300)
            (skill / "SKILL.md").write_text(guidance, encoding="utf-8")
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root):
                bridge = self.make_bridge(root)
                chunks, offset = [], 0
                while offset is not None:
                    page = bridge._console_info("skill_detail", "long-skill", offset)
                    self.assertTrue(page["ok"])
                    self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 3000)
                    chunks.append(page["skill"]["instructions"])
                    offset = page["skill"]["next_offset"]
                self.assertEqual("".join(chunks), guidance)
                self.assertFalse(bridge._console_info("skill_detail", "../../other-file")["ok"])
                self.assertFalse(bridge._console_info("skill_detail", "long-skill", -1)["ok"])

    def test_named_skill_is_delivered_even_when_model_does_not_request_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skill = root / "skills" / "debugging-failures"
            skill.mkdir(parents=True)
            guidance = "---\nname: debugging-failures\ndescription: Debug.\n---\n[plugins:fixture.action]\nTrace the failing input.\n"
            (skill / "SKILL.md").write_text(guidance, encoding="utf-8")
            other = root / "skills" / "review-software"
            other.mkdir(parents=True)
            (other / "SKILL.md").write_text("---\nname: review-software\ndescription: Review.\n---\nReview the change.\n", encoding="utf-8")
            requests = []
            plugin_scopes = []
            def create(**request):
                requests.append(request)
                plugin_scopes.append(bridge.workspace_agent._plugin_access)
                return SimpleNamespace(id="done", steps=[], output_text="done")
            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root), \
                    patch.object(WorkspaceAgent, "_get_client", lambda self: client):
                bridge = self.make_bridge(root)
                bridge.set_workspace_options("approve_writes", False)
                bridge.workspace_sandbox.policy_profile = "full_harness"
                result = bridge.send_chat("Use the debugging-failures skill to inspect the issue", "sabrina", "")
                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual([item["id"] for item in result["registry_skills"]], ["debugging-failures"])
                self.assertIn(guidance, requests[0]["input"])
                self.assertFalse(result["registry_skills"][0]["truncated"])
                self.assertEqual(plugin_scopes, [None])

                requests.clear()
                result = bridge.send_chat("Do not use the debugging-failures skill", "sabrina", "")
                self.assertTrue(result["ok"], result.get("error"))
                self.assertEqual(result["registry_skills"], [])
                self.assertNotIn(guidance, requests[0]["input"])
                self.assertEqual([s["id"] for s in bridge._requested_workspace_skills(
                    "Use debugging-failures and the review-software skill")[1]], ["debugging-failures", "review-software"])
                self.assertEqual(bridge._requested_workspace_skills(
                    "Do not use debugging-failures and the review-software skill")[1], [])

    def test_pending_native_approval_survives_switching_harness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def create(**request):
                if request["input"] == "Draft an edit":
                    return SimpleNamespace(id="draft", steps=[{
                        "type": "function_call", "id": "fc-write", "name": "write_file",
                        "arguments": {"path": "draft.txt", "content": "draft"},
                    }])
                return SimpleNamespace(id="done", steps=[], output_text="Decision recorded.")

            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root), \
                    patch.object(WorkspaceAgent, "_get_client", lambda self: client):
                bridge = self.make_bridge(root)
                bridge.set_workspace_options("approve_writes", False)
                result = bridge.send_chat("Draft an edit", "sabrina", "")
                pending = result["pending_approvals"][0]["id"]
                self.assertFalse((root / "project" / "draft.txt").exists())
                self.assertTrue(bridge.set_active_harness("mcp_harness")["ok"])
                self.assertEqual(bridge.get_workspace_status()["pending_approvals"][0]["id"], pending)
                self.assertTrue(bridge.set_active_harness("sabrina_native")["ok"])
                self.assertTrue(bridge.decide_workspace_action(pending, False)["ok"])
                self.assertFalse((root / "project" / "draft.txt").exists())

    def test_cancel_while_model_waits_prevents_a_later_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started, release = threading.Event(), threading.Event()

            def create(**request):
                started.set()
                if not release.wait(10):
                    raise RuntimeError("test did not release model")
                return SimpleNamespace(id="late", steps=[{
                    "type": "function_call", "id": "fc-write", "name": "write_file",
                    "arguments": {"path": "late.txt", "content": "should not exist"},
                }])

            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root), \
                    patch.object(WorkspaceAgent, "_get_client", lambda self: client):
                bridge = self.make_bridge(root)
                bridge.set_workspace_options("sandbox_auto", False)
                self.assertTrue(bridge.send_chat_async("Write late.txt", "sabrina", "")["ok"])
                self.assertTrue(started.wait(5))
                bridge.cancel_chat_job()
                release.set()
                with bridge._workspace_agent_lock:
                    self.assertFalse((root / "project" / "late.txt").exists())
                    self.assertEqual(bridge.workspace_agent.pending_actions, {})
                    self.assertIsNone(bridge.workspace_agent.last_interaction_id)
                view = bridge.get_chat_view()
                self.assertTrue(view["chat_job"]["cancelled"])
                self.assertTrue(any("CANCELLED" in m["text"] for m in view["chat_history"]))

    def test_cancel_discards_a_newly_staged_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = WorkspaceSandbox(Path(directory))
            cancel = threading.Event()
            stage = sandbox.stage_diff

            def stage_then_cancel(*args, **kwargs):
                proposal = stage(*args, **kwargs)
                cancel.set()
                return proposal

            client = SimpleNamespace(interactions=SimpleNamespace(create=lambda **_: SimpleNamespace(
                id="draft", steps=[{"type": "function_call", "id": "fc-write", "name": "write_file",
                                    "arguments": {"path": "draft.txt", "content": "draft"}}])))
            agent = WorkspaceAgent(sandbox, mode="approve_writes", client=client)
            with patch.object(sandbox, "stage_diff", side_effect=stage_then_cancel):
                result = agent.send_message("Write draft.txt", cancel_event=cancel)
            self.assertTrue(result["cancelled"])
            self.assertEqual(agent.pending_actions, {})
            self.assertEqual(sandbox.list_staged_diffs(), [])
            self.assertFalse((sandbox.root / "draft.txt").exists())

    def test_chat_without_workspace_describes_its_real_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(desktop, "ROOT", root), patch.object(desktop, "SABRINA_WORKSPACE", root):
                bridge = self.make_bridge(root)
                bridge.workspace_sandbox = None
                seen = []

                def respond(provider, model, system, message, **kwargs):
                    seen.append(system)
                    return {"ok": True, "text": "Hello. Attach a folder to use workspace tools."}

                with patch.object(bridge, "_workspace_gemini_key", return_value="fixture"), \
                        patch.object(bridge, "_invoke_provider_text", side_effect=respond):
                    result = bridge.send_chat("Hi", "sabrina", "")
                self.assertTrue(result["ok"])
                self.assertFalse(result["workspace_attached"])
                self.assertIn("no callable tools", seen[0])
                self.assertIn('"attached": false', seen[0])


if __name__ == "__main__":
    unittest.main()
