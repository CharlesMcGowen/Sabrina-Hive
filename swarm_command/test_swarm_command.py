"""Unit tests for Sabrina Swarm Command runtime, blackboard, handoffs, and console bridge."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from swarm_command import (
    AgentRole,
    ApprovalDeniedError,
    CallableApprovalGate,
    ComplexityClass,
    ExecutionMode,
    HandoffEnvelope,
    HandoffMode,
    PartitionWriteError,
    PreflightError,
    RoleBindingConfig,
    RoleBindingResolutionError,
    RoleModelBinding,
    SchemaValidationError,
    SessionBlackboard,
    SwarmCommand,
    SwarmConsoleBridge,
    ToolPermissionError,
    ToolRegistry,
    VersionConflictError,
    execute_scoped_tool,
    infer_model_provider,
    validate_return_payload,
)
from swarm_console import SwarmClient


class TestSessionBlackboard(unittest.TestCase):
    def test_checkpoint_persistence_and_cas_locking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ckpt = Path(tmp) / "blackboard.json"
            bb = SessionBlackboard("sess_01", checkpoint_path=ckpt, budget_limit=10000)
            v1 = bb.state_version
            v2 = bb.compare_and_swap_owner(v1, AgentRole.EXECUTION_SPECIALIST.value)
            self.assertEqual(v2, v1 + 1)
            self.assertEqual(bb.active_owner, AgentRole.EXECUTION_SPECIALIST.value)

            with self.assertRaises(VersionConflictError):
                bb.compare_and_swap_owner(v1, AgentRole.PLANNER_ARCHITECT.value)

            # Reload from checkpoint and verify durability.
            reloaded = SessionBlackboard("sess_01", checkpoint_path=ckpt)
            self.assertEqual(reloaded.active_owner, AgentRole.EXECUTION_SPECIALIST.value)
            self.assertEqual(reloaded.state_version, v2)

    def test_partitioned_writes_and_context_refs(self) -> None:
        bb = SessionBlackboard("sess_02")
        bb.write_artifact(
            namespace="findings.t1",
            author="research_scout",
            summary="Verified router entrypoints",
            citations=["src/router.ts:14-88"],
            assigned_namespace="findings.t1",
        )
        with self.assertRaises(PartitionWriteError):
            bb.write_artifact(
                namespace="findings.t2",
                author="research_scout",
                summary="Cross-partition write should fail",
                assigned_namespace="findings.t1",
            )

        resolved = bb.resolve_context_refs(["state.artifacts.findings.t1"])
        self.assertIn("state.artifacts.findings.t1", resolved)
        self.assertEqual(resolved["state.artifacts.findings.t1"]["author"], "research_scout")


class TestScopedRuntimeAndIdempotency(unittest.TestCase):
    def test_scoped_tool_filtering_approval_and_idempotency(self) -> None:
        bb = SessionBlackboard("sess_03")
        registry = ToolRegistry()
        calls: list[dict[str, object]] = []

        def patch_handler(args: dict[str, object]) -> dict[str, object]:
            calls.append(args)
            return {"patched": True, "file": args.get("file")}

        registry.register(
            "repo_patch",
            patch_handler,
            is_mutating=True,
            requires_approval=True,
        )
        registry.register("repo_read", lambda args: {"content": "ok"}, is_mutating=False)

        env = HandoffEnvelope(
            from_agent="swarm_command",
            to_agent="execution_specialist",
            mode=HandoffMode.HANDOFF.value,
            objective="Patch auth middleware",
            allowed_tools=["repo_read", "repo_patch"],
            idempotency_key="idem_auth_001",
            return_schema="ExecutionReportV1",
        )

        # Unlisted tool is stripped and blocked.
        with self.assertRaises(ToolPermissionError):
            execute_scoped_tool("api_call", {}, env, bb, registry)

        # Missing or denying approval gate blocks mutating write.
        deny_gate = CallableApprovalGate(lambda agent, action, payload, risk: False)
        with self.assertRaises(ApprovalDeniedError):
            execute_scoped_tool(
                "repo_patch", {"file": "auth.py"}, env, bb, registry, approval_gate=deny_gate
            )
        self.assertEqual(len(calls), 0)

        # Approved execution runs once, then replays from side_effect_ledger on retry.
        allow_gate = CallableApprovalGate(lambda agent, action, payload, risk: True)
        first = execute_scoped_tool(
            "repo_patch", {"file": "auth.py"}, env, bb, registry, approval_gate=allow_gate
        )
        second = execute_scoped_tool(
            "repo_patch", {"file": "auth.py"}, env, bb, registry, approval_gate=allow_gate
        )
        self.assertEqual(first["status"], "executed")
        self.assertEqual(second["status"], "replayed_from_ledger")
        self.assertEqual(len(calls), 1)


class TestSwarmCommandRouter(unittest.TestCase):
    def test_direct_answer_zero_hops(self) -> None:
        bb = SessionBlackboard("sess_04")
        cmd = SwarmCommand(bb)
        res = cmd.run_turn("What is the status?")
        self.assertEqual(res.mode, ExecutionMode.DIRECT_ANSWER)
        self.assertEqual(bb.get_turn_metrics()["hop_count"], 0)
        self.assertEqual(bb.active_owner, AgentRole.SWARM_COMMAND.value)

    def test_parallel_fan_out_and_critic_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = SwarmClient(Path(tmp) / ".swarm", auto_init=True)
            bridge = SwarmConsoleBridge(client, enable_otel=False)
            bb = SessionBlackboard("sess_05")
            cmd = SwarmCommand(bb, console_bridge=bridge)

            res = cmd.run_turn(
                "Audit auth and router modules",
                subtasks=["Audit auth middleware", "Audit router entrypoints", "Audit tests"],
            )
            self.assertEqual(res.mode, ExecutionMode.PARALLEL_FAN_OUT)
            self.assertTrue(res.verdict and res.verdict["passed"])
            self.assertEqual(len(res.artifacts_written), 3)
            self.assertGreater(len(client.read_board()), 0)

    def test_multi_turn_ownership_handoff_and_return(self) -> None:
        bb = SessionBlackboard("sess_06")
        turn_counter = {"n": 0}

        def multi_turn_exec(env, blackboard, manifest):
            turn_counter["n"] += 1
            if turn_counter["n"] == 1:
                return {
                    "status": "in_progress",
                    "actions_taken": ["step_1_prepared"],
                    "idempotency_key": env.idempotency_key,
                }
            return {
                "status": "complete",
                "actions_taken": ["step_2_committed"],
                "idempotency_key": env.idempotency_key,
            }

        cmd = SwarmCommand(
            bb,
            agent_runners={AgentRole.EXECUTION_SPECIALIST: multi_turn_exec},
        )
        r1 = cmd.run_turn("Refactor auth middleware")
        self.assertEqual(r1.mode, ExecutionMode.OWNERSHIP_HANDOFF)
        self.assertEqual(r1.active_owner, AgentRole.EXECUTION_SPECIALIST.value)

        # Next user turn goes straight to Execution Specialist, which completes and returns to command.
        r2 = cmd.run_turn("Continue")
        self.assertEqual(r2.mode, ExecutionMode.OWNERSHIP_HANDOFF)
        self.assertEqual(r2.active_owner, AgentRole.SWARM_COMMAND.value)

    def test_circuit_breakers_and_preflight(self) -> None:
        bb = SessionBlackboard("sess_07", budget_limit=100)
        cmd = SwarmCommand(bb)
        with self.assertRaises(PreflightError):
            cmd.run_turn("   ")

        bb.add_tokens(150)
        res = cmd.run_turn("Compare two modules")
        self.assertTrue(res.circuit_breaker_triggered)
        self.assertEqual(res.mode, ExecutionMode.CIRCUIT_BREAKER_SYNTHESIS)

    def test_return_schema_validation(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_return_payload("ResearchBriefV1", {"summary": "missing citations"})
        ok = validate_return_payload("ResearchBriefV1", {"escalation_reason": "need_human"})
        self.assertEqual(ok["escalation_reason"], "need_human")


class TestRoleBindingConfig(unittest.TestCase):
    def test_infer_model_provider(self) -> None:
        cases = {
            "gemini-4-carbon": "gemini",
            "models/genie-4": "gemini",
            "lyria-2": "gemini",
            "gemma-3-27b": "gemini",
            "openai/gpt-5": "openai",
            "gpt-5-mini": "openai",
            "o1-preview": "openai",
            "o3-mini": "openai",
            "o4-mini": "openai",
            "anthropic/claude-sonnet-4-5": "anthropic",
            "claude-opus-4-1": "anthropic",
            "openrouter/meta-llama/llama-4": "openrouter",
            "xai/grok-4": "xai",
            "grok-3": "xai",
            "deepseek/deepseek-r1": "deepseek",
            "deepseek-chat": "deepseek",
            "groq/llama-3.3-70b": "groq",
            "mistral/mistral-large": "mistral",
            "together/qwen-3": "together",
            "ollama/qwen2.5:7b": "ollama",
            "qwen2.5:7b": "ollama",
        }
        for model_id, expected in cases.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(infer_model_provider(model_id), expected)

    def test_persistence_dedupe_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "role_bindings.json"
            cfg = RoleBindingConfig(config_path=cfg_path)
            default_coordinator = cfg.get_binding("coordinator")
            self.assertEqual(cfg.get_binding("verifier").role, "critic_verifier")
            self.assertEqual(cfg.get_binding(AgentRole.RESEARCH_SCOUT).primary_model, "gemini-4-hydrogen")

            with self.assertRaises(ValueError):
                cfg.set_binding("coordinator", "   ")

            updated = cfg.set_binding(
                "coordinator",
                "openai/gpt-5",
                ["openai/gpt-5", "anthropic/claude-sonnet-4-5", "anthropic/claude-sonnet-4-5", "gemini-4-carbon"],
            )
            self.assertEqual(updated.primary_model, "openai/gpt-5")
            self.assertEqual(updated.fallback_models, ["anthropic/claude-sonnet-4-5", "gemini-4-carbon"])
            self.assertTrue(cfg_path.is_file())

            reloaded = RoleBindingConfig(config_path=cfg_path)
            self.assertEqual(reloaded.get_binding("coordinator").primary_model, "openai/gpt-5")
            self.assertEqual(
                reloaded.get_binding("coordinator").fallback_models,
                ["anthropic/claude-sonnet-4-5", "gemini-4-carbon"],
            )

            reset = reloaded.reset_binding("coordinator")
            self.assertEqual(reset, default_coordinator)
            self.assertEqual(RoleBindingConfig(config_path=cfg_path).get_binding("coordinator"), default_coordinator)

    def test_resolve_model_and_execute_with_fallback(self) -> None:
        cfg = RoleBindingConfig()
        cfg.set_binding("coordinator", "gemini-4-carbon", ["gemini-4-neon", "openai/gpt-5"])
        # Primary gemini-4-carbon is locked when only openai is unlocked -> selects openai/gpt-5.
        resolved = cfg.resolve_model("coordinator", unlocked_providers={"openai"})
        self.assertEqual(resolved["selected_model"], "openai/gpt-5")
        self.assertTrue(resolved["used_fallback"])
        self.assertTrue(resolved["unlocked"])

        # Strict unlock raises when no candidate provider is unlocked.
        with self.assertRaises(RoleBindingResolutionError):
            cfg.resolve_model("coordinator", unlocked_providers={"groq"}, strict_unlock=True)

        # Non-strict falls back to first healthy with unlocked=False.
        nonstrict = cfg.resolve_model("coordinator", unlocked_providers={"groq"}, strict_unlock=False)
        self.assertEqual(nonstrict["selected_model"], "gemini-4-carbon")
        self.assertFalse(nonstrict["unlocked"])

        # execute_with_fallback fails over on RuntimeError / ConnectionError.
        seen: list[str] = []

        def flaky_runner(model: str) -> str:
            seen.append(model)
            if model in {"gemini-4-carbon", "gemini-4-neon"}:
                raise ConnectionError(f"{model} unreachable")
            return f"ok:{model}"

        result, telemetry = cfg.execute_with_fallback("coordinator", flaky_runner)
        self.assertEqual(result, "ok:openai/gpt-5")
        self.assertTrue(telemetry["used_fallback"])
        self.assertEqual(telemetry["selected_model"], "openai/gpt-5")
        self.assertEqual(seen, ["gemini-4-carbon", "gemini-4-neon", "openai/gpt-5"])


class TestSwarmCommandRoleBindingWiring(unittest.TestCase):
    def test_envelope_model_roundtrip_and_runner_failover(self) -> None:
        env = HandoffEnvelope(
            from_agent="swarm_command",
            to_agent="research_scout",
            objective="Check roundtrip",
            assigned_model="gemini-4-hydrogen",
            fallback_models=["gemini-3.5-flash-lite", "openai/gpt-5-mini"],
        )
        restored = HandoffEnvelope.from_dict(env.to_dict())
        self.assertEqual(restored.assigned_model, "gemini-4-hydrogen")
        self.assertEqual(restored.fallback_models, ["gemini-3.5-flash-lite", "openai/gpt-5-mini"])

        with tempfile.TemporaryDirectory() as tmp:
            client = SwarmClient(Path(tmp) / ".swarm", auto_init=True)
            bindings = RoleBindingConfig()
            bridge = SwarmConsoleBridge(client, role_bindings=bindings)
            bb = SessionBlackboard("sess_rb")
            attempted_models: list[str | None] = []

            def failing_primary_exec(env_in, blackboard, manifest):
                attempted_models.append(env_in.assigned_model)
                if env_in.assigned_model == "gemini-4-hydrogen":
                    raise TimeoutError("primary timed out")
                return {
                    "status": "complete",
                    "actions_taken": [f"ran on {env_in.assigned_model}"],
                    "idempotency_key": env_in.idempotency_key,
                }

            cmd = SwarmCommand(
                bb,
                console_bridge=bridge,
                role_bindings=bindings,
                agent_runners={AgentRole.EXECUTION_SPECIALIST: failing_primary_exec},
            )
            res = cmd.run_turn("Patch auth middleware")
            self.assertEqual(attempted_models, ["gemini-4-hydrogen", "gemini-4-argon"])
            self.assertEqual(res.envelopes[0]["assigned_model"], "gemini-4-argon")

            roster_workers = client.get_state()["roster"]["workers"]
            self.assertIsInstance(roster_workers, dict)
            self.assertEqual(
                roster_workers[AgentRole.RESEARCH_SCOUT.value]["model"],
                "gemini-4-hydrogen",
            )


if __name__ == "__main__":
    unittest.main()
