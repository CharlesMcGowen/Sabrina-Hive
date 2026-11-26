from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import patch
import tempfile

from sabrina_console_desktop import sabrina_desktop_ui as desktop
from swarm_console.workspace_agent import WorkspaceAgent


class WorkspacePluginTests(TestCase):
    def test_native_chat_requests_and_receives_coordinator_public_quote(self):
        calls = []
        def reply(**request):
            calls.append(request)
            if len(calls) == 1:
                return SimpleNamespace(id="first", steps=[{
                    "type": "function_call", "name": "request_plugin",
                    "arguments": {"plugin": "kraken_console", "action_json": '{"operation":"quote","pair":"BTCUSD"}'},
                }])
            return SimpleNamespace(id="second", steps=[], output_text="Quote received")
        client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root / "project"
            workspace.mkdir()
            with (patch.object(desktop, "ROOT", root),
                  patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                  patch.object(WorkspaceAgent, "_get_client", lambda agent: client),
                  patch("tools.coordinator_plugin_access.invoke", return_value={"ok": True, "bid": "1", "ask": "2"}) as invoke):
                bridge = desktop.SabrinaDesktopBridge(role_bindings_path=root/"roles.json", vault_path=root/"vault.json")
                try:
                    bridge.workspace_policy_profile = "full_harness"
                    bridge._bind_workspace(workspace, "sandbox_auto")
                    result = bridge.send_chat("Check [plugins:kraken_console.quote]", "workspace_agent", "")
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["tool_events"][0]["status"], "executed", result["tool_events"][0])
                    self.assertEqual(result["tool_events"][0]["result"]["result"]["bid"], "1")
                    self.assertTrue(any(t["name"] == "request_plugin" for t in calls[0]["tools"]))
                    self.assertEqual(invoke.call_count, 1)
                    self.assertIsNone(bridge.workspace_agent._plugin_access)
                finally:
                    bridge.swarm_runner.shutdown()

    def test_approve_writes_waits_for_operator_before_plugin(self):
        calls = []
        def reply(**request):
            calls.append(request)
            if isinstance(request["input"], str) and request["input"].startswith("Check "):
                return SimpleNamespace(id="first", steps=[{
                    "type": "function_call", "name": "request_plugin",
                    "arguments": {"plugin": "kraken_console", "action_json": '{"operation":"quote","pair":"BTCUSD"}'},
                }])
            return SimpleNamespace(id="second", steps=[], output_text="Finished")
        client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root/"project"
            workspace.mkdir()
            with (patch.object(desktop, "ROOT", root),
                  patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                  patch.object(WorkspaceAgent, "_get_client", lambda agent: client),
                  patch("tools.coordinator_plugin_access.invoke", return_value={"ok": True, "bid": "1"}) as invoke):
                bridge = desktop.SabrinaDesktopBridge(role_bindings_path=root/"roles.json", vault_path=root/"vault.json")
                try:
                    bridge.workspace_policy_profile = "full_harness"
                    bridge._bind_workspace(workspace, "approve_writes")
                    result = bridge.send_chat("Check [plugins:kraken_console.quote]", "workspace_agent", "")
                    pending_id = result["pending_approvals"][0]["id"]
                    invoke.assert_not_called()
                    self.assertTrue(bridge.set_active_harness("mcp_harness")["ok"])
                    self.assertEqual(bridge.get_workspace_status()["pending_approvals"][0]["id"], pending_id)
                    approved = bridge.decide_workspace_action(pending_id, True)
                    self.assertTrue(approved["ok"], approved)
                    self.assertEqual(approved["tool_events"][0]["status"], "approved", approved["tool_events"][0])
                    self.assertEqual(invoke.call_count, 1)
                finally:
                    bridge.swarm_runner.shutdown()

    def test_policy_change_before_staging_blocks_plugin(self):
        def reply(**request):
            if isinstance(request["input"], str):
                return SimpleNamespace(id="first", steps=[{
                    "type": "function_call", "name": "request_plugin",
                    "arguments": {"plugin": "kraken_console", "action_json": '{"operation":"quote","pair":"BTCUSD"}'},
                }])
            return SimpleNamespace(id="second", steps=[], output_text="Blocked")
        client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root/"project"
            workspace.mkdir()
            with (patch.object(desktop, "ROOT", root),
                  patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                  patch.object(WorkspaceAgent, "_get_client", lambda agent: client),
                  patch("tools.coordinator_plugin_access.CoordinatorPluginAccess._current_policy", return_value={}),
                  patch("tools.coordinator_plugin_access.invoke") as invoke):
                bridge = desktop.SabrinaDesktopBridge(role_bindings_path=root/"roles.json", vault_path=root/"vault.json")
                try:
                    bridge.workspace_policy_profile = "full_harness"
                    bridge._bind_workspace(workspace, "approve_writes")
                    result = bridge.send_chat("Check [plugins:kraken_console.quote]", "workspace_agent", "")
                    self.assertEqual(result["tool_events"][0]["status"], "blocked")
                    self.assertEqual(result["pending_approvals"], [])
                    invoke.assert_not_called()
                finally:
                    bridge.swarm_runner.shutdown()

    def test_changed_pending_arguments_fail_at_approval(self):
        def reply(**request):
            if isinstance(request["input"], str):
                return SimpleNamespace(id="first", steps=[{
                    "type": "function_call", "name": "request_plugin",
                    "arguments": {"plugin": "kraken_console", "action_json": '{"operation":"quote","pair":"BTCUSD"}'},
                }])
            return SimpleNamespace(id="second", steps=[], output_text="Finished")
        client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root/"project"
            workspace.mkdir()
            with (patch.object(desktop, "ROOT", root),
                  patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                  patch.object(WorkspaceAgent, "_get_client", lambda agent: client),
                  patch("tools.coordinator_plugin_access.invoke") as invoke):
                bridge = desktop.SabrinaDesktopBridge(role_bindings_path=root/"roles.json", vault_path=root/"vault.json")
                try:
                    bridge.workspace_policy_profile = "full_harness"
                    bridge._bind_workspace(workspace, "approve_writes")
                    result = bridge.send_chat("Check [plugins:kraken_console.quote]", "workspace_agent", "")
                    pending = result["pending_approvals"][0]["id"]
                    bridge.workspace_agent.pending_actions[pending]["arguments"]["action_json"] = '{"operation":"quote","pair":"ETHUSD"}'
                    resolved = bridge.decide_workspace_action(pending, True)
                    self.assertEqual(resolved["tool_events"][0]["status"], "error")
                    invoke.assert_not_called()
                finally:
                    bridge.swarm_runner.shutdown()

    def test_unrequested_capability_is_denied(self):
        def reply(**request):
            if isinstance(request["input"], str):
                return SimpleNamespace(id="first", steps=[{
                    "type": "function_call", "name": "request_plugin",
                    "arguments": {"plugin": "kraken_console", "action_json": '{"operation":"balances"}'},
                }])
            return SimpleNamespace(id="second", steps=[], output_text="Blocked")
        client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root/"project"
            workspace.mkdir()
            with (patch.object(desktop, "ROOT", root),
                  patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                  patch.object(WorkspaceAgent, "_get_client", lambda agent: client),
                  patch("tools.coordinator_plugin_access.invoke") as invoke):
                bridge = desktop.SabrinaDesktopBridge(role_bindings_path=root/"roles.json", vault_path=root/"vault.json")
                try:
                    bridge.workspace_policy_profile = "full_harness"
                    bridge._bind_workspace(workspace, "sandbox_auto")
                    result = bridge.send_chat("Check [plugins:kraken_console.quote]", "workspace_agent", "")
                    self.assertEqual(result["tool_events"][0]["status"], "blocked")
                    invoke.assert_not_called()
                finally:
                    bridge.swarm_runner.shutdown()

    def test_sandboxed_edit_does_not_run_plugins_as_host_processes(self):
        def reply(**request):
            if isinstance(request["input"], str):
                return SimpleNamespace(id="first", steps=[{
                    "type": "function_call", "name": "request_plugin",
                    "arguments": {"plugin": "kraken_console", "action_json": '{"operation":"quote","pair":"BTCUSD"}'},
                }])
            return SimpleNamespace(id="second", steps=[], output_text="Blocked")
        client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root/"project"
            workspace.mkdir()
            with (patch.object(desktop, "ROOT", root),
                  patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                  patch.object(WorkspaceAgent, "_get_client", lambda agent: client),
                  patch("tools.coordinator_plugin_access.invoke") as invoke):
                bridge = desktop.SabrinaDesktopBridge(role_bindings_path=root/"roles.json", vault_path=root/"vault.json")
                try:
                    bridge.workspace_policy_profile = "sandboxed_edit"
                    bridge._bind_workspace(workspace, "approve_writes")
                    result = bridge.send_chat("Check [plugins:kraken_console.quote]", "workspace_agent", "")
                    self.assertEqual(result["tool_events"][0]["status"], "blocked")
                    invoke.assert_not_called()
                finally:
                    bridge.swarm_runner.shutdown()

    def test_read_only_refuses_even_a_model_invented_request(self):
        def reply(**request):
            if isinstance(request["input"], str):
                return SimpleNamespace(id="first", steps=[{
                    "type": "function_call", "name": "request_plugin",
                    "arguments": {"plugin": "kraken_console", "action_json": '{"operation":"quote","pair":"BTCUSD"}'},
                }])
            return SimpleNamespace(id="second", steps=[], output_text="Blocked")
        client = SimpleNamespace(interactions=SimpleNamespace(create=reply))
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = root/"project"
            workspace.mkdir()
            with (patch.object(desktop, "ROOT", root),
                  patch.object(desktop, "SABRINA_WORKSPACE", workspace),
                  patch.object(WorkspaceAgent, "_get_client", lambda agent: client),
                  patch("tools.coordinator_plugin_access.invoke") as invoke):
                bridge = desktop.SabrinaDesktopBridge(role_bindings_path=root/"roles.json", vault_path=root/"vault.json")
                try:
                    bridge.workspace_policy_profile = "full_harness"
                    bridge._bind_workspace(workspace, "read_only")
                    result = bridge.send_chat("Check [plugins:kraken_console.quote]", "workspace_agent", "")
                    self.assertEqual(result["tool_events"][0]["status"], "blocked")
                    invoke.assert_not_called()
                finally:
                    bridge.swarm_runner.shutdown()


if __name__ == "__main__":
    main()

