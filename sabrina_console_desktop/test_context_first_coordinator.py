"""Unit tests for the Three-Layer Context-First Coordinator Architecture."""

from __future__ import annotations

import json
from dataclasses import replace
import tempfile
import time
import unittest
from pathlib import Path

from sabrina_console_desktop.context_first_coordinator import (
    BlueprintValidationError,
    PLAYBOOK_REQUIRED_ROLES,
    VALID_PLAYBOOKS,
    build_preflight_manifest,
    build_coordinator_setup_prompts,
    decide_swarm_blueprint,
    parse_swarm_blueprint,
)
from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge


class _FakeVault:
    def __init__(self, unlocked: list[str], probed: list[dict[str, str]] | None = None) -> None:
        self._unlocked = list(unlocked)
        self._probed = list(probed or [])

    def list_providers(self) -> dict[str, object]:
        return {"unlocked_providers": list(self._unlocked)}

    def is_provider_unlocked(self, provider: str) -> bool:
        return provider in self._unlocked

    def get_probed_models(self) -> list[dict[str, str]]:
        return list(self._probed)


class TestContextFirstCoordinator(unittest.TestCase):
    def test_coordinator_understands_selected_browser_window(self) -> None:
        manifest = build_preflight_manifest(candidate_models=["ollama/test (local, ready)"], ollama_ready=True)
        manifest = replace(manifest, available_tool_shed=("desktop_session",),
                           desktop={"backend": "agent-desktop", "app": "msedge.exe", "window": "Example"})
        system, user = build_coordinator_setup_prompts("Build a cart", manifest)
        self.assertIn("desktop_session supplies browser access", system)
        self.assertIn("execution_authorized is false", system)
        self.assertIn('"desktop_session"', user)

    def _make_workspace(self, root: Path) -> Path:
        ws = root / "workspace"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "engine.py").write_text(
            '"""Engine module."""\ndef compute_score(x: int) -> int:\n    return x * 3\n',
            encoding="utf-8",
        )
        return ws

    def test_layer1_preflight_manifest_filters_cooldowns_and_restricts_tools(self) -> None:
        now = time.monotonic()
        vault = _FakeVault(
            unlocked=["gemini", "openai"],
            probed=[
                {"id": "gemini-3.5-flash-lite", "provider": "gemini"},
                {"id": "gemini-4-carbon", "provider": "gemini"},
                {"id": "openai/gpt-5", "provider": "openai"},
            ],
        )
        manifest = build_preflight_manifest(
            vault=vault,
            cooldowns={
                ("gemini", "gemini-4-carbon"): now + 3600.0,
                ("openai", "*"): now + 45.0,
            },
            zero_quota_models={"gemini-4-argon"},
            unsupported_models={"dead-model-v1"},
            ollama_models=[{"name": "gemma3:12b"}],
            ollama_ready=True,
            execution_authorized=False,
            allow_workspace_writes=False,
            allow_browser=False,
            extra_tools=["browser_open", "write_file"],
            now_mono=now,
        )

        # Only healthy local Ollama + non-cooled Gemini enter the manifest
        self.assertEqual(
            list(manifest.healthy_models),
            [
                "ollama/gemma3:12b (local, ready)",
                "gemini/gemini-3.5-flash-lite (cloud, 15 RPM)",
                "gemini/gemini-4-hydrogen (cloud, 15 RPM)",
            ],
        )
        self.assertNotIn("gemini/gemini-4-carbon", manifest.healthy_model_ids)
        self.assertNotIn("openai/gpt-5", manifest.healthy_model_ids)

        # Tool shed excludes browser and write tools when execution_authorized=False
        self.assertEqual(
            list(manifest.available_tool_shed),
            ["search_files", "read_file", "run_diagnostics"],
        )
        self.assertFalse(manifest.execution_authorized)
        self.assertEqual([p["id"] for p in manifest.playbook_menu], list(VALID_PLAYBOOKS))

    def test_layer2_blueprint_schema_enforces_playbook_roles_and_healthy_models(self) -> None:
        manifest = build_preflight_manifest(
            unlocked_providers=["gemini", "ollama"],
            candidate_models=[
                "ollama/gemma3:12b (local, ready)",
                "gemini/gemini-3.5-flash-lite (cloud, 15 RPM)",
            ],
            ollama_ready=True,
        )

        # Valid iterative_debug_loop blueprint mapping local gemma3:12b to scout and flash-lite to executor/verifier
        valid_raw = json.dumps(
            {
                "playbook": "iterative_debug_loop",
                "role_bindings": {
                    "research_scout": "ollama/gemma3:12b (local, ready)",
                    "execution_specialist": "gemini/gemini-3.5-flash-lite",
                    "critic_verifier": "gemini/gemini-3.5-flash-lite (cloud, 15 RPM)",
                },
                "stop_condition": "compute_score verified with diagnostics PASS and CriticVerdictV1 PASS.",
            }
        )
        bp = parse_swarm_blueprint(valid_raw, manifest)
        self.assertEqual(bp.playbook, "iterative_debug_loop")
        self.assertEqual(
            bp.role_bindings,
            {
                "research_scout": "ollama/gemma3:12b",
                "execution_specialist": "gemini/gemini-3.5-flash-lite",
                "critic_verifier": "gemini/gemini-3.5-flash-lite",
            },
        )

        # Cooled-down or dead model not in manifest is rejected
        with self.assertRaisesRegex(BlueprintValidationError, "not in the Pre-Flight Manifest"):
            parse_swarm_blueprint(
                {
                    "playbook": "direct_single_agent",
                    "role_bindings": {
                        "execution_specialist": "gemini/gemini-4-carbon",
                        "critic_verifier": "gemini/gemini-3.5-flash-lite",
                    },
                    "stop_condition": "Done",
                },
                manifest,
            )

        # Extra unrequired role for direct_single_agent is rejected
        with self.assertRaisesRegex(BlueprintValidationError, "extra unrequired roles"):
            parse_swarm_blueprint(
                {
                    "playbook": "direct_single_agent",
                    "role_bindings": {
                        "planner_architect": "gemini/gemini-3.5-flash-lite",
                        "execution_specialist": "gemini/gemini-3.5-flash-lite",
                        "critic_verifier": "gemini/gemini-3.5-flash-lite",
                    },
                    "stop_condition": "Done",
                },
                manifest,
            )

        # Missing required role for parallel_research_swarm is rejected
        with self.assertRaisesRegex(BlueprintValidationError, "missing required roles"):
            parse_swarm_blueprint(
                {
                    "playbook": "parallel_research_swarm",
                    "role_bindings": {
                        "execution_specialist": "gemini/gemini-3.5-flash-lite",
                        "critic_verifier": "gemini/gemini-3.5-flash-lite",
                    },
                    "stop_condition": "Done",
                },
                manifest,
            )

    def test_layer2_and_layer3_blocked_missing_capability_halts_immediately(self) -> None:
        calls: list[str] = []

        def coordinator_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            calls.append(role)
            return {
                "ok": True,
                "text": json.dumps(
                    {
                        "playbook": "blocked_missing_capability",
                        "role_bindings": {},
                        "stop_condition": "Blocked: Browser tool is required to open Chrome, but available_tool_shed has no browser capability.",
                    }
                ),
                "model": model_id,
                "provider": "gemini",
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "reports",
                step_delay_sec=0.0,
                auto_spindown=False,
                llm_caller=coordinator_caller,
            )
            try:
                manifest = build_preflight_manifest(
                    unlocked_providers=["gemini"],
                    candidate_models=["gemini/gemini-3.5-flash-lite (cloud, 15 RPM)"],
                    ollama_ready=False,
                )
                res = runner.launch_context_first_mission(
                    "Open in browser https://example.com and click login",
                    manifest=manifest,
                    strict_blueprint=True,
                )
                self.assertTrue(res["ok"])
                self.assertEqual(res["state"], "BLOCKED")
                self.assertEqual(res["playbook"], "blocked_missing_capability")
                self.assertEqual(res["workers_spawned"], [])
                self.assertEqual(calls, ["coordinator"])

                out = runner.get_mission_output(res["mission_id"])["output"]
                self.assertEqual(out["state"], "BLOCKED")
                self.assertFalse(out["verdict"]["passed"])
                self.assertIn("Browser tool", out["stop_condition"])
            finally:
                runner.shutdown()

    def test_layer3_iterative_debug_loop_executes_inner_tools_and_critic_gate(self) -> None:
        observed_calls: list[tuple[str, str]] = []
        exec_turns = 0

        def fake_llm(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            nonlocal exec_turns
            observed_calls.append((role, model_id))
            if role == "coordinator":
                # Verify Turn-0 Coordinator received the Pre-Flight Manifest
                assert "available_tool_shed" in user_prompt
                assert "healthy_models" in user_prompt
                return {
                    "ok": True,
                    "text": json.dumps(
                        {
                            "playbook": "iterative_debug_loop",
                            "role_bindings": {
                                "research_scout": "ollama/gemma3:12b",
                                "execution_specialist": "gemini/gemini-3.5-flash-lite",
                                "critic_verifier": "gemini/gemini-3.5-flash-lite",
                            },
                            "stop_condition": "Verify compute_score multiplier and pass diagnostics.",
                        }
                    ),
                    "model": model_id,
                    "provider": "gemini",
                }
            if role == "research_scout":
                return {
                    "ok": True,
                    "text": "Found src/engine.py defining compute_score(x).",
                    "model": model_id,
                    "provider": "ollama",
                }
            if role == "execution_specialist":
                exec_turns += 1
                if exec_turns == 1:
                    return {
                        "ok": True,
                        "text": (
                            "```tool:search_files\ncompute_score\n```\n"
                            "```tool:read_file\nsrc/engine.py:1-10\n```\n"
                            "```tool:run_diagnostics\nsrc/engine.py\n```"
                        ),
                        "model": model_id,
                        "provider": "gemini",
                    }
                assert "=== TOOL search_files('compute_score') ===" in user_prompt
                assert "=== TOOL read_file(src/engine.py:1-10) ===" in user_prompt
                assert "=== TOOL run_diagnostics(src/engine.py) status=PASS ===" in user_prompt
                return {
                    "ok": True,
                    "text": "Verified: compute_score(x) in src/engine.py returns x * 3 and diagnostics passed.",
                    "model": model_id,
                    "provider": "gemini",
                }
            if role == "critic_verifier":
                return {
                    "ok": True,
                    "text": "PASS: Deliverable accurately cites src/engine.py and satisfies stop_condition.",
                    "model": model_id,
                    "provider": "gemini",
                }
            raise AssertionError(f"Unexpected role called: {role}")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "reports",
                step_delay_sec=0.0,
                auto_spindown=False,
                llm_caller=fake_llm,
            )
            try:
                manifest = build_preflight_manifest(
                    unlocked_providers=["gemini", "ollama"],
                    candidate_models=[
                        "ollama/gemma3:12b (local, ready)",
                        "gemini/gemini-3.5-flash-lite (cloud, 15 RPM)",
                    ],
                    ollama_ready=True,
                )
                res = runner.launch_context_first_mission(
                    "Debug and verify compute_score in src/engine.py",
                    manifest=manifest,
                    strict_blueprint=True,
                )
                self.assertTrue(res["ok"])
                self.assertEqual(res["playbook"], "iterative_debug_loop")
                self.assertEqual(len(res["workers_spawned"]), 3)

                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["state"], "PASS")
                self.assertTrue(out["verdict"]["passed"])
                self.assertEqual(out["playbook"], "iterative_debug_loop")
                self.assertEqual(
                    observed_calls,
                    [
                        ("coordinator", "gemini/gemini-3.5-flash-lite"),
                        ("research_scout", "ollama/gemma3:12b"),
                        ("execution_specialist", "gemini/gemini-3.5-flash-lite"),
                        ("execution_specialist", "gemini/gemini-3.5-flash-lite"),
                        ("critic_verifier", "gemini/gemini-3.5-flash-lite"),
                    ],
                )
                self.assertIn("Context-First Pre-Flight Manifest & SwarmBlueprint", out["report_markdown"])
            finally:
                runner.shutdown()

    def test_bridge_get_preflight_manifest_respects_bridge_cooldown_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _FakeVault(
                unlocked=["gemini"],
                probed=[
                    {"id": "gemini-3.5-flash-lite", "provider": "gemini"},
                    {"id": "gemini-4-carbon", "provider": "gemini"},
                ],
            )
            bridge = SabrinaDesktopBridge(
                role_bindings_path=root / "roles.json",
                vault=vault,
                swarm_artifacts_dir=root / "reports",
                step_delay_sec=0.0,
            )
            self.addCleanup(bridge.swarm_runner.shutdown)
            bridge._provider_cooldowns[("gemini", "gemini-4-carbon")] = time.monotonic() + 600.0
            bridge._remote_cache["ollama_models"] = [{"name": "gemma3:12b"}]

            manifest_dict = bridge.get_preflight_manifest()
            self.assertIn("ollama/gemma3:12b (local, ready)", manifest_dict["healthy_models"])
            self.assertIn("gemini/gemini-3.5-flash-lite (cloud, 15 RPM)", manifest_dict["healthy_models"])
            self.assertNotIn("gemini/gemini-4-carbon", manifest_dict["healthy_model_ids"])
            self.assertEqual(
                manifest_dict["available_tool_shed"],
                ["search_files", "read_file", "run_diagnostics"],
            )


if __name__ == "__main__":
    unittest.main()
