"""Unit tests for WorkspaceSandbox, WorkspaceAgent, and Pluggable Harness Collective (zero network calls)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from swarm_console.harness_collective import (
    HarnessPolicy,
    HarnessRegistry,
    MCPToolBusHarness,
)
from swarm_console.workspace_agent import WorkspaceAgent
from swarm_console.workspace_sandbox import (
    WorkspaceSandbox,
    WorkspaceSandboxError,
    apply_selected_hunks,
    create_sandbox_project,
    parse_unified_diff_hunks,
)


class _FakeStep:
    def __init__(self, step_type: str, **kwargs: Any) -> None:
        self.type = step_type
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeInteraction:
    def __init__(self, interaction_id: str, steps: list[Any], output_text: str = "") -> None:
        self.id = interaction_id
        self.steps = steps
        self.output_text = output_text


class _ScriptedInteractions:
    def __init__(self, responses: list[_FakeInteraction]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeInteraction:
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeGenAIClient:
    def __init__(self, responses: list[_FakeInteraction]) -> None:
        self.interactions = _ScriptedInteractions(responses)


class TestWorkspaceSandbox(unittest.TestCase):
    def test_path_jail_and_holdout_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "proj")
            with self.assertRaises(WorkspaceSandboxError):
                sb.resolve_path("../outside.txt")
            with self.assertRaises(WorkspaceSandboxError):
                sb.resolve_path("C:/Windows/win.ini")
            with self.assertRaises(WorkspaceSandboxError):
                sb.resolve_path("outputs/kraken-holdout/secret.txt")

    def test_crud_tools_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = create_sandbox_project("Demo App!", base_root=Path(tmp))
            sb = WorkspaceSandbox(proj)

            mk = sb.execute_tool("create_dir", {"path": "src"})
            self.assertTrue(mk["ok"])

            wr = sb.execute_tool("write_file", {"path": "src/hello.py", "content": "def hi():\n    return 'hello'\n"})
            self.assertTrue(wr["ok"])
            self.assertIn("+def hi():", wr["diff"])
            self.assertEqual(len(wr["hunks"]), 1)

            # overwrite=False protects existing files
            with self.assertRaises(WorkspaceSandboxError):
                sb.execute_tool("write_file", {"path": "src/hello.py", "content": "nope"})

            ed = sb.execute_tool("edit_file", {"path": "src/hello.py", "target": "'hello'", "replacement": "'sabrina'"})
            self.assertTrue(ed["ok"])
            self.assertIn("'sabrina'", ed["diff"])

            rd = sb.execute_tool("read_file", {"path": "src/hello.py"})
            self.assertIn("sabrina", rd["content"])

            ls = sb.execute_tool("list_dir", {"path": "."})
            paths = [e["path"] for e in ls["entries"]]
            self.assertIn("src", paths)
            self.assertIn("src/hello.py", paths)

            # Invalid schema args rejected
            with self.assertRaises(WorkspaceSandboxError):
                sb.execute_tool("write_file", {"path": "", "content": "x"})

    def test_run_python_fail_closed_and_local_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb_closed = WorkspaceSandbox(Path(tmp) / "closed", allow_local_venv_fallback=False)
            res_closed = sb_closed.execute_tool("run_python", {"code": "print(2 + 2)"})
            if res_closed.get("backend") != "docker":
                self.assertFalse(res_closed["ok"])
                self.assertIn("fail-closed", res_closed["error"])

            sb_local = WorkspaceSandbox(Path(tmp) / "local", allow_local_venv_fallback=True)
            res_local = sb_local.execute_tool("run_python", {"code": "print(2 + 2)"})
            self.assertTrue(res_local["ok"])
            self.assertEqual(res_local["stdout"].strip(), "4")

    def test_hunk_parsing_staging_and_selective_hunk_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "hunks")
            old_lines = [f"line_{i} = {i}\n" for i in range(1, 21)]
            sb.execute_tool("write_file", {"path": "mod.py", "content": "".join(old_lines)})

            new_lines = list(old_lines)
            new_lines[1] = "line_2 = 222\n"
            new_lines[18] = "line_19 = 1999\n"
            staged = sb.stage_diff("write_file", {"path": "mod.py", "content": "".join(new_lines), "overwrite": True})
            self.assertEqual(len(staged["hunks"]), 2)
            self.assertEqual(staged["hunks"][0]["id"], "hunk_1")
            self.assertEqual(staged["hunks"][1]["id"], "hunk_2")

            # Accept only hunk_2 (keep line_2 original, apply line_19 change)
            applied = sb.apply_staged_diff(staged["id"], accepted_hunk_ids=["hunk_2"])
            self.assertTrue(applied["ok"])
            text_after = (sb.root / "mod.py").read_text(encoding="utf-8")
            self.assertIn("line_2 = 2\n", text_after)
            self.assertIn("line_19 = 1999\n", text_after)

    def test_hybrid_checkpoints_and_instant_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "cp")
            sb.execute_tool("write_file", {"path": "a.py", "content": "v = 1\n"})
            cp = sb.create_checkpoint(label="v1")
            self.assertEqual(cp["file_count"], 1)

            sb.execute_tool("write_file", {"path": "a.py", "content": "v = 2\n", "overwrite": True})
            sb.execute_tool("write_file", {"path": "extra.py", "content": "extra = True\n"})
            self.assertTrue((sb.root / "extra.py").exists())

            rb = sb.rollback_checkpoint(cp["id"])
            self.assertTrue(rb["ok"])
            self.assertIn("a.py", rb["restored_files"])
            self.assertIn("extra.py", rb["removed_files"])
            self.assertEqual((sb.root / "a.py").read_text(encoding="utf-8"), "v = 1\n")
            self.assertFalse((sb.root / "extra.py").exists())

    def test_closed_loop_diagnostics_and_policy_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "diag")
            sb.execute_tool("write_file", {"path": "broken.py", "content": "def oops(:\n    pass\n"})
            diag = sb.execute_tool("run_diagnostics", {"path": "broken.py", "checks": ["syntax", "security"]})
            self.assertTrue(diag["ok"])
            self.assertFalse(diag["passed"])
            self.assertEqual(diag["error_count"], 1)
            self.assertEqual(diag["diagnostics"][0]["source"], "syntax")

            # Policy gate: inspect_only blocks write_file
            sb.set_policy_profile("inspect_only")
            with self.assertRaises(WorkspaceSandboxError):
                sb.execute_tool("write_file", {"path": "blocked.py", "content": "x = 1\n"})

    def test_managed_background_process_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(
                Path(tmp) / "proc",
                allow_local_venv_fallback=True,
                policy_profile="full_harness",
            )
            started = sb.execute_tool(
                "manage_process",
                {
                    "action": "start",
                    "command": [sys.executable, "-c", "import time; print('sabrina_bg_ok', flush=True); time.sleep(0.2)"],
                    "label": "bg_test",
                },
            )
            self.assertTrue(started["ok"])
            pid = started["process"]["id"]
            time.sleep(0.1)
            logs = sb.execute_tool("manage_process", {"action": "logs", "process_id": pid})
            self.assertTrue(logs["ok"])
            stopped = sb.execute_tool("manage_process", {"action": "stop", "process_id": pid})
            self.assertTrue(stopped["ok"])
            self.assertTrue(stopped["stopped"])


class TestWorkspaceAgent(unittest.TestCase):
    def test_json_handoff_uses_observed_source_and_reserves_a_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp))
            (sandbox.root / "note.json").write_text('{"note":"observed"}', encoding="utf-8")
            requests = []

            def create(**request):
                requests.append(request)
                if not request.get("response_format"):
                    return _FakeInteraction("read", [
                        _FakeStep("function_call", id="read-1", name="read_file", arguments={"path": "note.json"}),
                    ])
                evidence = json.loads(request["input"])["observations"][0]["result"]["content"]
                return _FakeInteraction("handoff", [], output_text=json.dumps(evidence))

            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            agent = WorkspaceAgent(sandbox, mode="read_only", json_handoff=True, max_turns=2, client=client)
            result = agent.send_message("Read note.json. Return one JSON object.")
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(result["reply"]), {"note": "observed"})
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[-1]["tools"], [])
            self.assertFalse(requests[-1]["store"])
            self.assertEqual(requests[-1]["response_format"]["mime_type"], "application/json")

    def test_json_handoff_rejects_malformed_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp))
            for output in ('[]', 'Here is {"ok":true}', '{"x":1,"x":2}', '{"x":NaN}'):
                with self.subTest(output=output):
                    client = _FakeGenAIClient([_FakeInteraction("handoff", [], output_text=output)])
                    agent = WorkspaceAgent(sandbox, mode="read_only", json_handoff=True, max_turns=1, client=client)
                    result = agent.send_message("Return one JSON object.")
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "JSON handoff failed validation.")
                    self.assertEqual(result["harness_events"][-1]["kind"], "error")

    def test_json_handoff_uses_the_operator_decision_after_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = WorkspaceSandbox(Path(tmp))

            def create(**request):
                if request.get("response_format"):
                    evidence = json.loads(request["input"])["observations"]
                    approved = evidence[0]["result"]["ok"]
                    return _FakeInteraction("handoff", [], output_text=json.dumps({"approved": approved}))
                if isinstance(request["input"], str):
                    return _FakeInteraction("edit", [
                        _FakeStep("function_call", id="edit-1", name="write_file", arguments={"path": "note.txt", "content": "hello"}),
                    ])
                return _FakeInteraction("draft", [], output_text="Operator decision recorded.")

            client = SimpleNamespace(interactions=SimpleNamespace(create=create))
            agent = WorkspaceAgent(sandbox, json_handoff=True, client=client)
            first = agent.send_message("Write note.txt. Return one JSON object with an approved field.")
            action = first["pending_approvals"][0]["id"]
            result = agent.decide_action(action, approve=False)
            self.assertTrue(result["ok"])
            self.assertEqual(json.loads(result["reply"]), {"approved": False})
            self.assertFalse((sandbox.root / "note.txt").exists())

    def test_sandbox_auto_executes_multi_turn_tools_with_checkpoint_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "auto")
            fake_client = _FakeGenAIClient([
                _FakeInteraction(
                    "int-1",
                    [_FakeStep("function_call", id="fc-1", name="write_file", arguments={"path": "note.py", "content": "x = 42\n"})],
                ),
                _FakeInteraction("int-2", [], output_text="Created note.py."),
            ])
            agent = WorkspaceAgent(sb, mode="sandbox_auto", client=fake_client)
            out = agent.send_message("Create note.py")
            self.assertTrue(out["ok"])
            self.assertEqual(out["reply"], "Created note.py.")
            self.assertTrue((sb.root / "note.py").is_file())
            self.assertEqual(len(out["tool_events"]), 1)
            self.assertEqual(len(out["checkpoints"]), 1)
            self.assertEqual(len(out["diagnostics"]), 1)
            self.assertTrue(out["diagnostics"][0]["passed"])

    def test_approve_writes_queues_hunks_and_resolves_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "approve")
            fake_client = _FakeGenAIClient([
                _FakeInteraction(
                    "int-1",
                    [_FakeStep("function_call", id="fc-1", name="write_file", arguments={"path": "a.py", "content": "x = 1\n"})],
                ),
                _FakeInteraction("int-2", [], output_text="Done writing a.py."),
            ])
            agent = WorkspaceAgent(sb, mode="approve_writes", client=fake_client)
            first = agent.send_message("Write a.py")
            self.assertEqual(len(first["pending_approvals"]), 1)
            self.assertEqual(len(first["pending_approvals"][0]["hunks"]), 1)
            self.assertFalse((sb.root / "a.py").exists())

            action_id = first["pending_approvals"][0]["id"]
            second = agent.decide_action(action_id, approve=True, accepted_hunk_ids=["hunk_1"])
            self.assertTrue(second["ok"])
            self.assertTrue((sb.root / "a.py").is_file())
            self.assertEqual(second["reply"], "Done writing a.py.")

    def test_read_only_blocks_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "ro")
            fake_client = _FakeGenAIClient([
                _FakeInteraction(
                    "int-1",
                    [_FakeStep("function_call", id="fc-1", name="create_dir", arguments={"path": "nope"})],
                ),
                _FakeInteraction("int-2", [], output_text="Cannot write in read-only mode."),
            ])
            agent = WorkspaceAgent(sb, mode="read_only", client=fake_client)
            out = agent.send_message("Create nope dir")
            self.assertFalse((sb.root / "nope").exists())
            self.assertEqual(out["tool_events"][0]["status"], "blocked")

    def test_manage_dependencies_cache_info_and_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "deps", allow_local_venv_fallback=True)
            info = sb.execute_tool("manage_dependencies", {"action": "cache_info"})
            self.assertTrue(info["ok"])
            self.assertTrue(info["uv_available"])
            self.assertIn("wheelhouse_dir", info)

            listed = sb.execute_tool("manage_dependencies", {"action": "list"})
            self.assertTrue(listed["ok"])
            self.assertIn("output", listed)

    def test_manage_dependencies_validation_and_offline_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sb = WorkspaceSandbox(Path(tmp) / "deps_val", allow_local_venv_fallback=True)
            # Rejects unsafe package strings
            res_bad = sb.execute_tool("manage_dependencies", {"action": "install", "packages": ["bad;rm -rf /"]})
            self.assertFalse(res_bad["ok"])
            self.assertIn("Invalid or unsafe", res_bad["error"])

            # Offline wheelhouse install check (dry-run style with wheelhouse_only)
            res_offline = sb.execute_tool(
                "manage_dependencies",
                {"action": "install", "packages": ["pydantic"], "wheelhouse_only": True},
            )
            # Should succeed using local cached wheels or find-links
            self.assertTrue(res_offline["ok"])
            self.assertEqual(res_offline["exit_code"], 0)


class TestHarnessCollective(unittest.TestCase):
    def test_registry_discovery_and_comparison_across_four_harnesses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "collective_ws"
            ws.mkdir(parents=True)
            (ws / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

            reg = HarnessRegistry(
                llm_caller=lambda role, model, sys_p, usr_p: {
                    "ok": True,
                    "text": "PASS: verified app.py via MLX/Ego collective caller.",
                    "provider": "mlx_ego",
                    "model": model,
                }
            )
            listing = reg.list_harnesses()
            ids = {h["harness_id"] for h in listing["harnesses"]}
            self.assertEqual(ids, {"sabrina_native", "mcp_harness", "external_cli", "mlx_ego_portal"})

            # Register a custom MCP tool and run a turn on mcp_harness
            mcp_h = reg.get("mcp_harness")
            assert isinstance(mcp_h, MCPToolBusHarness)
            mcp_h.register_mcp_tool(
                "code_index",
                "lookup_symbol",
                lambda args: {"symbol": args.get("name"), "file": "app.py", "line": 1},
                description="Lookup symbol in repo index",
            )
            mcp_turn = reg.run_harness_turn(
                task="Lookup symbol add",
                workspace_root=ws,
                harness_id="mcp_harness",
                mcp_calls=[{"name": "mcp__code_index__lookup_symbol", "arguments": {"name": "add"}}],
            )
            self.assertTrue(mcp_turn["ok"])
            self.assertEqual(mcp_turn["metrics"]["tool_calls"], 1)

            # Side-by-side comparison mode across all 4 harnesses
            cmp_res = reg.compare_harnesses("Verify app.py diagnostics", workspace_root=ws)
            self.assertTrue(cmp_res["ok"])
            self.assertEqual(len(cmp_res["comparisons"]), 4)
            self.assertTrue(all(c["ok"] for c in cmp_res["comparisons"]))


if __name__ == "__main__":
    unittest.main()
