"""Unit and integration tests for Sabrina Console Automations & Connectors."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sabrina_console_desktop.automation_engine import AutomationEngine
from sabrina_console_desktop.connector_registry import ConnectorRegistry
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from swarm_console.vault import ProviderKeyVault


class TestAutomationsAndConnectors(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self._tmp.name)
        self.provider_vault = ProviderKeyVault(vault_path=self.tmp_dir / "provider_vault.json")
        self.connectors = ConnectorRegistry(
            vault_path=self.tmp_dir / "connectors.dpapi.json",
            provider_vault=self.provider_vault,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_connector_dpapi_roundtrip_and_masking(self) -> None:
        self.connectors.save_config(
            "plaid",
            {
                "environment": "sandbox",
                "client_id": "cid_123",
                "secret": "sec_super_secret",
                "access_token": "access-sandbox-abc",
            },
        )
        cfg = self.connectors.get_config("plaid")
        self.assertEqual(cfg["client_id"], "cid_123")
        self.assertEqual(cfg["secret"], "sec_super_secret")
        self.assertTrue(self.connectors.is_configured("plaid"))

        listed = {c["id"]: c for c in self.connectors.list_connectors()}
        self.assertTrue(listed["plaid"]["configured"])
        self.assertEqual(listed["plaid"]["masked_config"]["secret"], "••••••••")
        self.assertEqual(listed["plaid"]["masked_config"]["client_id"], "cid_123")
        self.assertIn("mcp", listed)
        self.assertIn("shopify", listed)
        self.assertIn("woocommerce", listed)

        self.connectors.delete_config("plaid")
        self.assertFalse(self.connectors.is_configured("plaid"))

    def test_hitl_pause_edit_and_no_step_replay(self) -> None:
        step0_calls: list[str] = []
        write_calls: list[dict] = []

        def fake_execute(cid: str, action: str, params=None):
            if cid == "rss":
                step0_calls.append("called")
                return {"ok": True, "data": {"items": [{"title": "Order #1"}]}}
            if cid == "webhook":
                write_calls.append(dict(params or {}))
                return {"ok": True, "status": 200}
            return {"ok": True}

        self.connectors.execute_action = fake_execute  # type: ignore[method-assign]
        engine = AutomationEngine(
            db_path=self.tmp_dir / "automations.sqlite3",
            connector_registry=self.connectors,
        )
        saved = engine.save_loop(
            {
                "name": "RSS to Webhook",
                "goal": "Poll RSS and post webhook",
                "loop_type": "manual_trigger",
                "approval_policy": "always_for_writes",
                "steps": [
                    {"name": "Poll RSS", "step_type": "connector", "connector_id": "rss", "action": "feed.poll", "kind": "read"},
                    {"name": "Post Webhook", "step_type": "connector", "connector_id": "webhook", "action": "http.post", "kind": "write", "params": {"amount": 50, "msg": "draft"}},
                ],
            }
        )
        loop_id = saved["loop"]["id"]
        res = engine.run_loop_now(loop_id)
        self.assertEqual(res["status"], "waiting_approval")
        self.assertEqual(len(step0_calls), 1)
        self.assertEqual(len(write_calls), 0)

        run_id = res["run_id"]
        resumed = engine.decide_approval(run_id, approve=True, edited_params={"amount": 50, "msg": "edited_by_user"})
        self.assertEqual(resumed["status"], "completed")
        # Step 0 must NOT replay when resuming from waiting_approval
        self.assertEqual(len(step0_calls), 1)
        self.assertEqual(write_calls, [{"amount": 50, "msg": "edited_by_user"}])

    def test_threshold_policy_and_rejection_feedback(self) -> None:
        self.connectors.execute_action = lambda cid, act, p=None: {"ok": True, "params": p}  # type: ignore[method-assign]
        engine = AutomationEngine(
            db_path=self.tmp_dir / "automations.sqlite3",
            connector_registry=self.connectors,
        )
        saved = engine.save_loop(
            {
                "name": "Refund Guard",
                "goal": "Auto-approve small refunds, pause large ones",
                "loop_type": "manual_trigger",
                "approval_policy": "threshold",
                "approval_threshold": 20.0,
                "steps": [
                    {"name": "Small Refund", "step_type": "connector", "connector_id": "stripe", "action": "refund.create", "kind": "write", "params": {"amount": 10}},
                    {"name": "Large Refund", "step_type": "connector", "connector_id": "stripe", "action": "refund.create", "kind": "write", "params": {"amount": 100}},
                ],
            }
        )
        res = engine.run_loop_now(saved["loop"]["id"])
        self.assertEqual(res["status"], "waiting_approval")
        self.assertEqual(res["pending_approval"]["step_index"], 1)

        rej = engine.decide_approval(res["run_id"], approve=False, feedback="Too expensive")
        self.assertEqual(rej["status"], "rejected")
        runs = engine.get_runs(saved["loop"]["id"])
        self.assertEqual(runs[0]["error"], "Too expensive")

    def test_ai_codesigner_multiple_choice_flow(self) -> None:
        def fake_gemini(prompt: str, schema=None) -> str:
            if '"choice_cadence": "daily"' in prompt:
                return json.dumps(
                    {
                        "status": "ready",
                        "assistant_message": "Blueprint ready!",
                        "blueprint": {
                            "name": "Daily Plaid Spend Digest",
                            "goal": "Sync Plaid transactions daily and summarize",
                            "loop_type": "recurring_interval",
                            "interval_minutes": 1440,
                            "max_iterations": 5,
                            "approval_policy": "always_for_writes",
                            "steps": [
                                {"name": "Sync Plaid", "step_type": "connector", "connector_id": "plaid", "action": "transactions.sync", "kind": "read"},
                                {"name": "Gemini Summary", "step_type": "gemini", "prompt_template": "Summarize spending"},
                            ],
                        },
                    }
                )
            return json.dumps(
                {
                    "status": "needs_input",
                    "assistant_message": "Pick a schedule:",
                    "questions": [
                        {
                            "id": "choice_cadence",
                            "prompt": "How often should this run?",
                            "multi_select": False,
                            "options": [
                                {"id": "daily", "label": "Daily", "description": "Once every 24h"},
                                {"id": "hourly", "label": "Hourly", "description": "Once every 60m"},
                            ],
                        }
                    ],
                }
            )

        engine = AutomationEngine(
            db_path=self.tmp_dir / "automations.sqlite3",
            connector_registry=self.connectors,
            gemini_caller=fake_gemini,
        )
        turn1 = engine.design_workflow_with_gemini("Check my Plaid spending")
        self.assertTrue(turn1["ok"])
        self.assertEqual(turn1["status"], "needs_input")
        self.assertEqual(len(turn1["questions"]), 1)

        turn2 = engine.design_workflow_with_gemini("Check my Plaid spending", answers={"choice_cadence": "daily"})
        self.assertTrue(turn2["ok"])
        self.assertEqual(turn2["status"], "ready")
        self.assertEqual(turn2["blueprint"]["name"], "Daily Plaid Spend Digest")

    def test_bridge_snapshot_and_ui_markup(self) -> None:
        bridge = SabrinaDesktopBridge(
            vault_path=self.tmp_dir / "vault.json",
            role_bindings_path=self.tmp_dir / "roles.json",
            custom_models_path=self.tmp_dir / "models.json",
            swarm_artifacts_dir=self.tmp_dir / "missions",
        )
        snap = bridge.get_snapshot()
        self.assertIn("automations", snap)
        self.assertIn("loops", snap["automations"])
        self.assertIn("connectors", snap["automations"])
        self.assertIn("pending_approvals", snap["automations"])

        html = (ROOT / "sabrina_console_desktop" / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        self.assertIn('data-view="automations"', html)
        self.assertIn('id="view-automations"', html)
        self.assertIn('id="auto-topo-svg"', html)
        self.assertIn('id="btn-swarm-to-loop"', html)
        self.assertIn('id="btn-auto-sim-run"', html)
        self.assertIn('id="quick-allow-writes"', html)
        self.assertIn('id="audit-decision-panel"', html)
        self.assertIn("renderAutomationsSnapshot", html)
        self.assertIn("renderAutomationTopology", html)
        self.assertIn("renderAuditDecisionPanel", html)

        # Verify workspace write helper blocks secrets/traversal and writes safe files
        ws_dir = self.tmp_dir / "ws"
        ws_dir.mkdir(parents=True, exist_ok=True)
        bridge.swarm_runner.set_workspace_root(ws_dir)
        written = bridge.swarm_runner._apply_workspace_writes(
            "```write_file:notes/hello.txt\nhi sabrina\n```\n"
            "```write_file:.env\nSECRET=1\n```\n"
            "```write_file:../escape.txt\nbad\n```\n"
        )
        self.assertEqual(written, ["notes/hello.txt"])
        self.assertTrue((bridge.swarm_runner.workspace_root / "notes" / "hello.txt").is_file())
        self.assertFalse((bridge.swarm_runner.workspace_root / ".env").exists())

        # Verify IDE-style tool:search and tool:read_file blocks
        has_tools, tool_out = bridge.swarm_runner._execute_ide_tool_blocks(
            "```tool:search\nhi sabrina\n```\n"
            "```tool:read_file\nnotes/hello.txt:1-10\n```\n"
            "```tool:read_file\n.env:1-5\n```\n",
            allow_tests=False,
        )
        self.assertTrue(has_tools)
        self.assertIn("notes/hello.txt:1: hi sabrina", tool_out)
        self.assertIn("BLOCKED: secret/database file", tool_out)

        # Verify adaptive model-chosen swarm_roles parsing
        from sabrina_console_desktop.live_swarm_runner import _extract_dynamic_workstreams
        ws = _extract_dynamic_workstreams(
            "Build Plaid + Shopify sync",
            planner_output=(
                "1. Connect Plaid\n"
                "```swarm_roles\n"
                '[{"role": "plaid_specialist", "title": "Wire Plaid Link", "needs_coordinator": true}, '
                '{"role": "retail_integrator", "title": "Sync Shopify orders"}]\n'
                "```"
            ),
            max_subagents=4,
        )
        self.assertEqual(len(ws), 2)
        self.assertEqual(ws[0]["role"], "plaid_specialist")
        self.assertTrue(ws[0]["needs_coordinator"])
        self.assertEqual(ws[1]["role"], "retail_integrator")

    def test_autonomous_a2a_graph_pass_off_and_critic_loop(self) -> None:
        calls: list[tuple[str, str]] = []
        critic_turns = 0

        def fake_agent_caller(role: str, model_id: str, sys_prompt: str, user_prompt: str):
            nonlocal critic_turns
            calls.append((role, user_prompt))
            if role == "planner_architect":
                return {"ok": True, "text": "Plan: validate schema and generate report."}
            if role == "execution_specialist":
                rev = sum(1 for r, _ in calls if r == "execution_specialist")
                return {"ok": True, "text": f"Draft deliverable v{rev}"}
            if role == "critic_verifier":
                critic_turns += 1
                if critic_turns == 1:
                    return {"ok": True, "text": "NEEDS_WORK: add missing edge-case coverage."}
                return {"ok": True, "text": "PASS: all invariants verified."}
            return {"ok": True, "text": "ok"}

        engine = AutomationEngine(
            db_path=self.tmp_dir / "automations.sqlite3",
            connector_registry=self.connectors,
            agent_caller=fake_agent_caller,
        )
        initiated = engine.save_and_initiate_loop(
            {
                "name": "Autonomous Council Loop",
                "goal": "Draft and verify module",
                "mode": "autonomous_a2a",
                "loop_type": "autonomous_a2a",
                "steps": [
                    {"id": "n_plan", "name": "Planner", "step_type": "agent", "agent_role": "planner_architect"},
                    {"id": "n_exec", "name": "Executor", "step_type": "agent", "agent_role": "execution_specialist"},
                    {"id": "n_crit", "name": "Critic", "step_type": "agent", "agent_role": "critic_verifier"},
                ],
                "edges": [
                    {"id": "e1", "from": "loop_root", "to": "n_plan", "comm_type": "delegate_spawn", "condition": "always"},
                    {"id": "e2", "from": "n_plan", "to": "n_exec", "comm_type": "pass_off", "condition": "always", "message_template": "Implement blueprint"},
                    {"id": "e3", "from": "n_exec", "to": "n_crit", "comm_type": "critique_gate", "condition": "always"},
                    {"id": "e4", "from": "n_crit", "to": "n_exec", "comm_type": "loop", "condition": "on_needs_work", "max_loops": 2, "message_template": "Fix critic findings"},
                ],
            }
        )
        self.assertTrue(initiated["ok"])
        run_res = initiated["run"]
        self.assertEqual(run_res["status"], "completed")
        self.assertTrue(run_res["goal_met"])
        # Expected execution sequence: Planner -> Executor(v1) -> Critic(NEEDS_WORK) -> loop -> Executor(v2) -> Critic(PASS)
        executed_roles = [r for r, _ in calls]
        self.assertEqual(
            executed_roles,
            ["planner_architect", "execution_specialist", "critic_verifier", "execution_specialist", "critic_verifier"],
        )
        comm_types = [ev["comm_type"] for ev in run_res["a2a_events"]]
        self.assertIn("pass_off", comm_types)
        self.assertIn("critique_gate", comm_types)
        self.assertIn("loop", comm_types)

    def test_escalate_hitl_a2a_link_and_swarm_monitor_a2a_bridge(self) -> None:
        executed_nodes: list[str] = []

        def fake_agent_caller(role: str, model_id: str, sys_prompt: str, user_prompt: str):
            executed_nodes.append(role)
            return {"ok": True, "text": f"PASS: {role} done"}

        engine = AutomationEngine(
            db_path=self.tmp_dir / "automations_hitl.sqlite3",
            connector_registry=self.connectors,
            agent_caller=fake_agent_caller,
        )
        saved = engine.save_loop(
            {
                "name": "Escalated A2A Workflow",
                "goal": "Test escalate_hitl edge pause and resume",
                "loop_type": "autonomous_a2a",
                "approval_policy": "always_for_writes",
                "steps": [
                    {"id": "s1", "name": "Scout", "step_type": "agent", "agent_role": "research_scout"},
                    {"id": "s2", "name": "Deployer", "step_type": "agent", "agent_role": "execution_specialist"},
                ],
                "edges": [
                    {"id": "esc_1", "from": "s1", "to": "s2", "comm_type": "escalate_hitl", "condition": "always"},
                ],
            }
        )
        r1 = engine.run_loop_now(saved["loop"]["id"])
        self.assertEqual(r1["status"], "waiting_approval")
        self.assertEqual(executed_nodes, ["research_scout"])

        r2 = engine.decide_approval(r1["run_id"], approve=True)
        self.assertEqual(r2["status"], "completed")
        # Upstream research_scout must NOT replay on approval resume
        self.assertEqual(executed_nodes, ["research_scout", "execution_specialist"])

        # Verify SabrinaDesktopBridge Swarm Monitor A2A link persistence & UI Autonomous Tab markers
        bridge = SabrinaDesktopBridge(
            vault_path=self.tmp_dir / "vault2.json",
            role_bindings_path=self.tmp_dir / "roles2.json",
            custom_models_path=self.tmp_dir / "models2.json",
            swarm_artifacts_dir=self.tmp_dir / "missions2",
        )
        up = bridge.upsert_swarm_a2a_link(
            {"from": "claude", "to": "antigravity", "comm_type": "loop", "condition": "on_needs_work", "max_loops": 3}
        )
        self.assertTrue(up["ok"])
        self.assertEqual(len(bridge.get_snapshot()["swarm_a2a_links"]), 1)
        deleted = bridge.delete_swarm_a2a_link(up["link"]["id"])
        self.assertTrue(deleted["ok"])
        self.assertEqual(len(bridge.get_snapshot()["swarm_a2a_links"]), 0)

        html = (ROOT / "sabrina_console_desktop" / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        for marker in (
            'id="btn-manual-tab-autonomous"',
            'id="manual-pane-autonomous"',
            'id="btn-manual-initiate"',
            'id="auto-link-composer"',
            'id="swarm-a2a-composer"',
            'id="schema-topo-svg"',
            'id="schema-a2a-composer"',
            "__sabrinaAutomationGraphApi",
        ):
            self.assertIn(marker, html)

    def test_diamond_fan_in_coalescing_and_untruncated_a2a_envelope(self) -> None:
        prompts_by_role: dict[str, list[str]] = {}
        long_deliverable = "SECTION_1_START\n" + ("A" * 4800) + "\nSECTION_5_UNIT_TESTS_VERIFIED"

        def fake_agent(role: str, model_id: str, sys_prompt: str, user_prompt: str):
            prompts_by_role.setdefault(role, []).append(user_prompt)
            if role == "coordinator":
                return {"ok": True, "text": "Dispatched parallel scout + planner branches."}
            if role == "research_scout":
                return {"ok": True, "text": "SCOUT_EVIDENCE_PAYLOAD"}
            if role == "planner_architect":
                return {"ok": True, "text": "PLANNER_INVARIANT_PAYLOAD"}
            if role == "execution_specialist":
                return {"ok": True, "text": long_deliverable}
            if role == "critic_verifier":
                assert "SECTION_5_UNIT_TESTS_VERIFIED" in user_prompt, "Deliverable was truncated before reaching Critic!"
                return {"ok": True, "text": "PASS: Complete 5-section spec received intact."}
            return {"ok": True, "text": "ok"}

        engine = AutomationEngine(
            db_path=self.tmp_dir / "automations_diamond.sqlite3",
            connector_registry=self.connectors,
            agent_caller=fake_agent,
        )
        res = engine.save_and_initiate_loop(
            {
                "name": "Diamond Fan-In Proof",
                "goal": "Verify single coalesced execution at diamond join and >4.8k payload delivery",
                "loop_type": "autonomous_a2a",
                "steps": [
                    {"id": "c0", "name": "Coord", "step_type": "agent", "agent_role": "coordinator"},
                    {"id": "b1", "name": "Scout", "step_type": "agent", "agent_role": "research_scout"},
                    {"id": "b2", "name": "Planner", "step_type": "agent", "agent_role": "planner_architect"},
                    {"id": "join", "name": "Synthesizer", "step_type": "agent", "agent_role": "execution_specialist"},
                    {"id": "crit", "name": "Critic", "step_type": "agent", "agent_role": "critic_verifier"},
                ],
                "edges": [
                    {"id": "e0", "from": "loop_root", "to": "c0", "comm_type": "delegate_spawn"},
                    {"id": "e1", "from": "c0", "to": "b1", "comm_type": "delegate_spawn"},
                    {"id": "e2", "from": "c0", "to": "b2", "comm_type": "pass_off"},
                    {"id": "e3", "from": "b1", "to": "join", "comm_type": "blackboard_sync", "payload_mode": "raw_output"},
                    {"id": "e4", "from": "b2", "to": "join", "comm_type": "pass_off", "payload_mode": "raw_output"},
                    {"id": "e5", "from": "join", "to": "crit", "comm_type": "critique_gate", "payload_mode": "raw_output"},
                ],
            }
        )
        run = res["run"]
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["goal_met"])
        # Diamond join node (`execution_specialist`) and downstream `critic_verifier` MUST each execute ONCE (not twice!)
        self.assertEqual(len(prompts_by_role["execution_specialist"]), 1)
        self.assertEqual(len(prompts_by_role["critic_verifier"]), 1)
        # Coalesced execution prompt must include BOTH parallel upstream payloads
        exec_prompt = prompts_by_role["execution_specialist"][0]
        self.assertIn("SCOUT_EVIDENCE_PAYLOAD", exec_prompt)
        self.assertIn("PLANNER_INVARIANT_PAYLOAD", exec_prompt)

    def test_self_loop_into_root_and_finale_recommended_next_step(self) -> None:
        planner_goals: list[str] = []
        finale_turn = 0

        def fake_agent(role: str, model_id: str, sys_prompt: str, user_prompt: str):
            nonlocal finale_turn
            if "Finale Next-Step Strategist" in sys_prompt:
                finale_turn += 1
                if finale_turn == 1:
                    return {
                        "ok": True,
                        "text": (
                            "Cycle 1 complete. Next we should add regression assertions for self-loop goal evolution.\n"
                            "RECOMMENDED_NEXT_GOAL: Harden Cycle-2 A2A self-loop telemetry and verify input auto-fill."
                        ),
                    }
                return {
                    "ok": True,
                    "text": (
                        "Cycle 2 complete.\n"
                        "RECOMMENDED_NEXT_GOAL: Benchmark 3-cycle Evo-Bench transfer with live Finale prompt handoff."
                    ),
                }
            if role == "planner_architect":
                planner_goals.append(user_prompt.splitlines()[0])
                return {"ok": True, "text": f"Blueprint for turn {len(planner_goals)}"}
            if role == "execution_specialist":
                return {"ok": True, "text": f"Deliverable for turn {len(planner_goals)}"}
            if role == "critic_verifier":
                return {"ok": True, "text": "PASS: Deliverable verified."}
            return {"ok": True, "text": "ok"}

        bridge = SabrinaDesktopBridge(
            vault_path=self.tmp_dir / "vault_finale.json",
            role_bindings_path=self.tmp_dir / "roles_finale.json",
            custom_models_path=self.tmp_dir / "models_finale.json",
            swarm_artifacts_dir=self.tmp_dir / "missions_finale",
            llm_caller=fake_agent,
        )
        bridge.automation_engine.agent_caller = fake_agent

        res = bridge.save_and_initiate_automation(
            {
                "name": "Self-Looping Finale Council",
                "goal": "Initial Goal: Audit Sabrina workflow loop ergonomics.",
                "loop_type": "autonomous_a2a",
                "steps": [
                    {"id": "p1", "name": "Planner", "step_type": "agent", "agent_role": "planner_architect"},
                    {"id": "e1", "name": "Executor", "step_type": "agent", "agent_role": "execution_specialist"},
                    {"id": "c1", "name": "Critic", "step_type": "agent", "agent_role": "critic_verifier"},
                    {"id": "f1", "name": "Finale Next Step", "step_type": "recommend_next", "agent_role": "coordinator"},
                ],
                "edges": [
                    {"id": "r_p", "from": "loop_root", "to": "p1", "comm_type": "delegate_spawn", "max_loops": 2},
                    {"id": "p_e", "from": "p1", "to": "e1", "comm_type": "pass_off"},
                    {"id": "e_c", "from": "e1", "to": "c1", "comm_type": "critique_gate"},
                    {"id": "c_f", "from": "c1", "to": "f1", "comm_type": "pass_off", "condition": "on_pass"},
                    {"id": "f_root", "from": "f1", "to": "loop_root", "comm_type": "loop", "condition": "always", "max_loops": 1},
                ],
            }
        )
        self.assertTrue(res["ok"])
        run = res["run"]
        self.assertEqual(run["status"], "completed")
        self.assertTrue(run["goal_met"])
        self.assertEqual(len(planner_goals), 2)
        self.assertIn("Initial Goal: Audit Sabrina workflow loop ergonomics.", planner_goals[0])
        self.assertIn("Harden Cycle-2 A2A self-loop telemetry and verify input auto-fill.", planner_goals[1])
        self.assertEqual(
            run["recommended_next_goal"],
            "Benchmark 3-cycle Evo-Bench transfer with live Finale prompt handoff.",
        )
        self.assertEqual(
            run["goal_history"],
            [
                "Initial Goal: Audit Sabrina workflow loop ergonomics.",
                "Harden Cycle-2 A2A self-loop telemetry and verify input auto-fill.",
            ],
        )
        snap = bridge.get_snapshot()
        self.assertEqual(
            snap["recommended_next_step"]["goal"],
            "Benchmark 3-cycle Evo-Bench transfer with live Finale prompt handoff.",
        )

        html = (ROOT / "sabrina_console_desktop" / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        for marker in (
            'id="quick-auto-loop-count"',
            'id="btn-quick-suggest-next"',
            'id="recommended-next-banner"',
            'id="btn-auto-seed-self-loop"',
            'data-spawn-agent="recommend_next"',
        ):
            self.assertIn(marker, html)

    def test_sbv_drift_interception_oscillation_halt_and_payload_budget_ceiling(self) -> None:
        # 1. Test SBV Drift Interception + Payload Budget Hard Ceiling across multi-cycle self-loop
        turn = 0
        observed_goals: list[str] = []

        def drift_and_budget_agent(role: str, model_id: str, sys_prompt: str, user_prompt: str):
            nonlocal turn
            if "Finale Next-Step Strategist" in sys_prompt:
                turn += 1
                if turn == 1:
                    # Deliberately off-topic next-step to trigger SBV drift interception!
                    return {"ok": True, "text": "RECOMMENDED_NEXT_GOAL: Bake sourdough bread and check Honolulu surf forecast."}
                # On turn 2, repeat exact same prompt to trigger cyclic oscillation halt even though max_loops=4!
                return {"ok": True, "text": f"RECOMMENDED_NEXT_GOAL: {observed_goals[-1]}"}
            observed_goals.append(user_prompt.splitlines()[0].replace("Loop Goal: ", "").strip())
            # Emit 16KB output to exercise hard payload byte ceiling (max_payload_bytes=1536)
            return {"ok": True, "text": "PASS: " + ("PAYLOAD_STRESS_BLOCK_" * 800)}

        engine = AutomationEngine(
            db_path=self.tmp_dir / "automations_sbv.sqlite3",
            connector_registry=self.connectors,
            agent_caller=drift_and_budget_agent,
        )
        res = engine.save_and_initiate_loop(
            {
                "name": "SBV Drift & Budget Guard Loop",
                "goal": "Audit Sabrina A2A topological fan-in and SQLite checkpoint invariants.",
                "loop_type": "autonomous_a2a",
                "max_payload_bytes": 1536,
                "sbv_drift_threshold": 0.16,
                "steps": [
                    {"id": "w1", "name": "Worker", "step_type": "agent", "agent_role": "execution_specialist"},
                    {"id": "f1", "name": "Finale", "step_type": "recommend_next", "agent_role": "coordinator"},
                ],
                "edges": [
                    {"id": "e_root", "from": "loop_root", "to": "w1", "comm_type": "delegate_spawn", "max_loops": 5},
                    {"id": "e_wf", "from": "w1", "to": "f1", "comm_type": "pass_off", "payload_mode": "raw_output", "max_payload_bytes": 1536, "max_loops": 5},
                    {"id": "e_self", "from": "f1", "to": "loop_root", "comm_type": "loop", "condition": "always", "max_loops": 4},
                ],
            }
        )
        run = res["run"]
        self.assertEqual(run["status"], "completed")
        # Cycle 1 drifted -> tethered (`[Anchored to: ...]`); Cycle 2 repeated -> oscillation_halted stopped before Cycle 3!
        self.assertEqual(len(observed_goals), 2)
        self.assertTrue(observed_goals[1].startswith("[Anchored to: Audit Sabrina A2A topological fan-in"))
        recycle_events = [ev for ev in run["a2a_events"] if ev["to"] == "loop_root"]
        self.assertEqual(len(recycle_events), 2)
        self.assertTrue(recycle_events[0]["drift_intercepted"])
        self.assertFalse(recycle_events[0]["oscillation_halted"])
        self.assertTrue(recycle_events[1]["oscillation_halted"])

        # Verify hard byte ceiling was strictly enforced on every A2A hop
        wf_events = [ev for ev in run["a2a_events"] if ev["edge_id"] == "e_wf"]
        self.assertTrue(all(ev["budget_capped"] for ev in wf_events))
        self.assertTrue(all(ev["payload_bytes"] <= 1536 for ev in wf_events))


if __name__ == "__main__":
    unittest.main()
