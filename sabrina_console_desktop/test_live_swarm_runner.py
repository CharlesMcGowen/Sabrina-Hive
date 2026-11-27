"""Unit tests for LiveSwarmRunner dynamic worker lifecycle and bounded mission execution."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from swarm_command.models import AgentRole
from swarm_command.role_binding import RoleBindingConfig


class TestLiveSwarmRunner(unittest.TestCase):
    def test_unexpected_pipeline_failure_is_available_after_reopening(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = {"workspace_root": self._make_sample_workspace(root), "artifacts_dir": root / "missions",
                       "step_delay_sec": 0, "auto_spindown": False}
            runner = LiveSwarmRunner(**options)
            with patch.object(runner, "_build_scoped_registry", side_effect=RuntimeError("injected pipeline fault")):
                mission_id = runner.launch_mission("Audit a sample file")["mission_id"]
                self.assertEqual(runner.wait_for_mission(mission_id, timeout=10)["state"], "FAILED")

            reopened = LiveSwarmRunner(**options)
            output = reopened.get_mission_output(mission_id)["output"]
            self.assertEqual(output["mission_id"], mission_id)
            self.assertEqual(output["state"], "FAILED")
            self.assertIn("injected pipeline fault", output["summary"])
            self.assertEqual(reopened.list_run_attention(), [output])

    def test_process_exit_is_recovered_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._make_sample_workspace(root)
            artifacts = root / "missions"
            child = """
import os
import sys
from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner

runner = LiveSwarmRunner(workspace_root=sys.argv[1], artifacts_dir=sys.argv[2], step_delay_sec=60)
mission = runner.launch_mission('Audit a sample file')
print(mission['mission_id'], flush=True)
os._exit(71)
"""
            env = {**os.environ, "SABRINA_ASSISTANT_DB": str(root / "assistant.sqlite3"),
                   "SABRINA_DND_BLACKBOARD_DB": str(root / "telemetry.sqlite3")}
            result = subprocess.run([sys.executable, "-c", child, str(workspace), str(artifacts)],
                                    cwd=Path(__file__).resolve().parents[1], env=env, timeout=25,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 71, result.stderr)
            mission_id = result.stdout.strip().splitlines()[-1]
            reopened = LiveSwarmRunner(workspace_root=workspace, artifacts_dir=artifacts)
            output = reopened.get_mission_output(mission_id)["output"]
            self.assertEqual(output["mission_id"], mission_id)
            self.assertEqual(output["state"], "INTERRUPTED")
            self.assertEqual(reopened.list_run_attention(), [output])

    def test_overlapping_missions_block_stale_write_and_allow_a_fresh_read(self) -> None:
        contender_ready = threading.Event()
        winner_done = threading.Event()

        def caller(role: str, model: str, system: str, prompt: str) -> dict[str, object]:
            output = "Use the existing workspace file."
            if role == AgentRole.EXECUTION_SPECIALIST.value:
                if "Contender beta" in prompt:
                    contender_ready.set()
                    if not winner_done.wait(10):
                        raise RuntimeError("Winner did not finish")
                    output = "```write_file:shared.txt\nstale beta edit\n```"
                elif "Winner alpha" in prompt:
                    if not contender_ready.wait(10):
                        raise RuntimeError("Contender did not read")
                    output = "```write_file:shared.txt\nwinning alpha edit\n```"
                else:
                    output = "```write_file:shared.txt\nfresh gamma edit\n```"
            elif role == AgentRole.CRITIC_VERIFIER.value:
                output = "PASS: Review complete."
            return {"ok": True, "text": output, "model": model, "provider": "gemini"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._make_sample_workspace(root)
            shared = workspace / "shared.txt"
            shared.write_text("original", encoding="utf-8")
            runner = LiveSwarmRunner(
                workspace_root=workspace, artifacts_dir=root / "missions", llm_caller=caller,
                step_delay_sec=0.01, max_refinement_rounds=0, auto_spindown=False,
            )
            try:
                launch_options = {"workers_per_coordinator": 3, "allow_workspace_writes": True}
                first = runner.launch_mission("Winner alpha edit shared.txt", **launch_options)["mission_id"]
                second = runner.launch_mission("Contender beta edit shared.txt", **launch_options)["mission_id"]
                self.assertTrue(contender_ready.wait(8))
                self.assertIsNotNone(runner.wait_for_mission(first, timeout=8))
                self.assertEqual(shared.read_text(encoding="utf-8").strip(), "winning alpha edit")
                winner_done.set()
                result = runner.wait_for_mission(second, timeout=8)
                self.assertEqual(result["written_files"], [])
                self.assertFalse(result["verdict"]["passed"])
                self.assertEqual(shared.read_text(encoding="utf-8").strip(), "winning alpha edit")
                conflict = runner.get_state()["file_conflicts"][0]
                self.assertEqual((conflict["path"], conflict["mission_id"], conflict["current_mission_id"]),
                                 ("shared.txt", second, first))
                draft = runner.get_next_step_draft(second)
                self.assertTrue(draft["ok"])
                self.assertIn("Failed checks:", draft["prompt"])
                self.assertIn(f"Current version was saved by {first}", draft["prompt"])
                self.assertIn("Read the current file before editing", draft["prompt"])
                self.assertEqual(runner.get_next_step_draft(first)["evidence"]["written_files"], ["shared.txt"])
                edited = "Fresh gamma edit shared.txt after a new read."
                launch = runner.launch_mission(edited, parent_mission_id=second, **launch_options)
                third = runner.wait_for_mission(launch["mission_id"], timeout=8)
                self.assertEqual(third["written_files"], ["shared.txt"])
                self.assertEqual(third["parent_mission_id"], second)
                self.assertEqual(third["goal"], edited)
                self.assertEqual(shared.read_text(encoding="utf-8").strip(), "fresh gamma edit")
            finally:
                winner_done.set()
                runner.shutdown()

    def _make_sample_workspace(self, root: Path) -> Path:
        ws = root / "workspace"
        (ws / "src").mkdir(parents=True)
        (ws / "src" / "alpha_module.py").write_text(
            '"""Alpha module sample."""\ndef run_alpha() -> int:\n    return 42\n',
            encoding="utf-8",
        )
        (ws / "README.md").write_text("# Sample Project\nAlpha module documentation.\n", encoding="utf-8")
        # Create an excluded holdout directory to verify fail-closed guard_safe_path filtering.
        (ws / "outputs" / "kraken-holdout").mkdir(parents=True)
        (ws / "outputs" / "kraken-holdout" / "alpha_secret.txt").write_text("do not scan", encoding="utf-8")
        return ws

    def test_idle_starts_with_zero_workers_and_safety_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            runner = LiveSwarmRunner(
                workspace_root=tmp_dir,
                artifacts_dir=tmp_dir / "artifacts",
                spindown_delay_sec=0.2,
                step_delay_sec=0.02,
            )
            try:
                state = runner.get_state()
                self.assertFalse(state["execution_authorized"])
                self.assertEqual(state["status"], "IDLE")
                self.assertEqual(state["safety_lock"], "LOCKED")
                self.assertEqual(state["workers"], [])
                self.assertEqual(state["active_worker_count"], 0)
                self.assertEqual(state["recent_spindowns"], [])
                self.assertEqual(state["active_missions"], [])
                self.assertEqual(state["objectives"], [])
                self.assertEqual(state["mission_outputs"], [])
                self.assertEqual(
                    set(state["nodes"].keys()),
                    {"sabrina", "codex", "antigravity", "claude", "desktop"},
                )
                self.assertIn("READY", state["nodes"]["sabrina"]["status"])
                self.assertIn("execution_authorized: false", state["nodes"]["desktop"]["detail"])
            finally:
                runner.shutdown()

    def test_launch_mission_spawns_runs_writes_artifact_and_auto_spindown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            artifacts = tmp_dir / "artifacts" / "missions"
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=artifacts,
                spindown_delay_sec=0.25,
                step_delay_sec=0.03,
            )
            try:
                res = runner.launch_mission(
                    goal="Inspect alpha_module structure and verify safety boundaries",
                    mode="codex_thought_antigravity_implement",
                    physical_input=False,
                )
                self.assertTrue(res["ok"])
                self.assertFalse(res["execution_authorized"])
                mission_id = res["mission_id"]
                self.assertEqual(len(res["workers_spawned"]), 4)

                # Immediately after launch, dynamic workers are live and council is active.
                live_state = runner.get_state()
                self.assertEqual(live_state["status"], "ACTIVE")
                self.assertEqual(live_state["active_worker_count"], 4)
                self.assertEqual(len(live_state["workers"]), 4)
                self.assertIn("ORCHESTRATING", live_state["nodes"]["sabrina"]["status"])
                self.assertEqual(len(live_state["active_missions"]), 1)

                # Wait for mission completion
                output = runner.wait_for_mission(mission_id, timeout=8.0)
                self.assertIsNotNone(output)
                assert output is not None
                self.assertEqual(output["state"], "PASS")
                self.assertFalse(output["execution_authorized"])
                self.assertTrue(output["verdict"]["passed"])
                self.assertEqual(len(output["subtasks"]), 4)

                # Bounded mission report artifact written under artifacts_dir
                artifact_path = Path(output["artifact_path"])
                self.assertTrue(artifact_path.is_file())
                self.assertEqual(artifact_path.parent.resolve(), artifacts.resolve())
                report_text = artifact_path.read_text(encoding="utf-8")
                self.assertIn(mission_id, report_text)
                self.assertIn("alpha_module.py", report_text)
                self.assertIn("`False`", report_text)
                # Holdout file must never be scanned or leaked into report
                self.assertNotIn("alpha_secret.txt", report_text)
                self.assertNotIn("kraken-holdout", report_text)

                # get_mission_output works by explicit ID and default (latest)
                by_id = runner.get_mission_output(mission_id)
                self.assertTrue(by_id["ok"])
                self.assertEqual(by_id["artifact_path"], str(artifact_path))
                self.assertIn("alpha_module.py", by_id["report_markdown"])
                latest = runner.get_mission_output()
                self.assertTrue(latest["ok"])
                self.assertEqual(latest["mission_id"], mission_id)

                # Wait for automatic spin-down back to 0 active workers
                self.assertTrue(runner.wait_until_idle(timeout=6.0))
                after = runner.get_state()
                self.assertEqual(after["workers"], [])
                self.assertEqual(after["active_worker_count"], 0)
                self.assertEqual(after["status"], "IDLE")
                self.assertEqual(len(after["recent_spindowns"]), 4)
                self.assertTrue(all(s["reason"] == "auto_spindown" for s in after["recent_spindowns"]))
                self.assertTrue(len(after["activity_feed"]) >= 5)
                self.assertEqual(len(after["research_episodes"]), 1)
                self.assertEqual(after["research_episodes"][0]["status"], "PASS")
            finally:
                runner.shutdown()

    def test_manual_spawn_worker_and_spindown_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts",
                spindown_delay_sec=0.1,
                step_delay_sec=0.02,
            )
            try:
                sp1 = runner.spawn_worker(
                    name="Alpha Auditor",
                    role="critic_verifier",
                    task="Review alpha_module",
                )
                self.assertTrue(sp1["ok"])
                self.assertFalse(sp1["execution_authorized"])
                w1_id = sp1["worker_id"]

                sp2 = runner.spawn_worker(
                    name="Custom Scout",
                    role="research_scout",
                    model="openai/gpt-5-mini",
                )
                self.assertTrue(sp2["ok"])
                self.assertEqual(sp2["worker"]["model"], "openai/gpt-5-mini")
                self.assertEqual(sp2["active_worker_count"], 2)

                # Wait briefly for manual task to complete; manual workers stay mounted until explicit spindown
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    st = runner.get_state()
                    w1 = next((w for w in st["workers"] if w["id"] == w1_id), None)
                    if w1 and w1["status"] == "DONE":
                        break
                    time.sleep(0.02)

                mid_state = runner.get_state()
                self.assertEqual(mid_state["active_worker_count"], 2)
                w1_live = next(w for w in mid_state["workers"] if w["id"] == w1_id)
                self.assertEqual(w1_live["status"], "DONE")
                self.assertIn("inspected", w1_live["detail"])

                # Spin down worker 1 by ID
                sd1 = runner.spindown_worker(w1_id)
                self.assertTrue(sd1["ok"])
                self.assertEqual(sd1["active_worker_count"], 1)

                # Spin down worker 2 by name
                sd2 = runner.spindown_worker("Custom Scout")
                self.assertTrue(sd2["ok"])
                self.assertEqual(sd2["active_worker_count"], 0)

                final_state = runner.get_state()
                self.assertEqual(final_state["workers"], [])
                self.assertEqual(final_state["status"], "IDLE")
                self.assertEqual(len(final_state["recent_spindowns"]), 2)
                self.assertTrue(all(s["reason"] == "manual_spindown" for s in final_state["recent_spindowns"]))

                # Unknown worker returns ok=False cleanly
                missing = runner.spindown_worker("w-does-not-exist")
                self.assertFalse(missing["ok"])
            finally:
                runner.shutdown()

    def test_stop_mission_cancels_and_immediately_spins_down(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts",
                spindown_delay_sec=10.0,
                step_delay_sec=0.5,
            )
            try:
                m1 = runner.launch_mission("Long running audit of alpha_module")
                self.assertTrue(m1["ok"])
                mid1 = m1["mission_id"]

                manual = runner.spawn_worker("Pinned Worker", "fast_worker")
                self.assertTrue(manual["ok"])
                self.assertEqual(runner.get_state()["active_worker_count"], 5)

                # Stop only mission 1: its 4 workers immediately spin down, manual worker stays
                stop1 = runner.stop_mission(mid1)
                self.assertTrue(stop1["ok"])
                self.assertEqual(stop1["stopped_missions"], [mid1])
                self.assertEqual(len(stop1["spun_down_workers"]), 4)
                self.assertEqual(stop1["active_worker_count"], 1)
                st_after_one = runner.get_state()
                obj1 = next(o for o in st_after_one["objectives"] if o["id"] == mid1)
                self.assertEqual(obj1["state"], "STOPPED")

                # Launch a second mission and stop ALL (mission_id=None)
                m2 = runner.launch_mission("Second audit of alpha_module")
                self.assertTrue(m2["ok"])
                self.assertEqual(runner.get_state()["active_worker_count"], 5)

                stop_all = runner.stop_mission()
                self.assertTrue(stop_all["ok"])
                self.assertEqual(stop_all["active_worker_count"], 0)
                self.assertEqual(len(stop_all["spun_down_workers"]), 5)

                # Ensure background threads do not resurrect workers after stop
                time.sleep(0.15)
                final_st = runner.get_state()
                self.assertEqual(final_st["workers"], [])
                self.assertEqual(final_st["active_worker_count"], 0)
                self.assertEqual(final_st["status"], "IDLE")
            finally:
                runner.shutdown()

    def test_clear_completed_preserves_active_and_clears_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts",
                spindown_delay_sec=0.05,
                step_delay_sec=0.01,
            )
            try:
                res = runner.launch_mission("Inspect alpha_module")
                runner.wait_for_mission(res["mission_id"], timeout=6.0)
                self.assertTrue(runner.wait_until_idle(timeout=4.0))

                man = runner.spawn_worker("Live Helper", "fast_worker")
                self.assertTrue(man["ok"])

                cleared = runner.clear_completed()
                self.assertTrue(cleared["ok"])
                self.assertEqual(cleared["cleared_missions"], 1)
                self.assertEqual(cleared["cleared_spindowns"], 4)

                st = runner.get_state()
                self.assertEqual(st["objectives"], [])
                self.assertEqual(st["mission_outputs"], [])
                self.assertEqual(st["recent_spindowns"], [])
                self.assertEqual(st["active_worker_count"], 1)
                self.assertEqual(st["workers"][0]["id"], man["worker_id"])
            finally:
                runner.shutdown()

    def test_preflight_role_bindings_modes_and_physical_input_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            rb = RoleBindingConfig(config_path=tmp_dir / "role_bindings.json")
            rb.set_binding("planner_architect", "openai/gpt-5", ["gemini-4-carbon"])
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts",
                role_bindings=rb,
                unlocked_providers_fn=lambda: {"openai"},
                spindown_delay_sec=0.05,
                step_delay_sec=0.01,
            )
            try:
                # Preflight rejects empty and null-byte goals without spawning workers
                bad_empty = runner.launch_mission("   ")
                self.assertFalse(bad_empty["ok"])
                self.assertFalse(bad_empty["execution_authorized"])
                bad_null = runner.launch_mission("bad\x00goal")
                self.assertFalse(bad_null["ok"])
                self.assertEqual(runner.get_state()["active_worker_count"], 0)

                # Mode swap still assigns planner/executor under sabrina coordinator; physical_input=True keeps execution_authorized=False
                res = runner.launch_mission(
                    goal="Audit alpha_module with swapped roles",
                    mode="antigravity_thought_codex_implement",
                    physical_input=True,
                )
                self.assertTrue(res["ok"])
                self.assertFalse(res["execution_authorized"])
                st = runner.get_state()
                by_role = {w["role"]: w for w in st["workers"]}
                self.assertEqual(by_role["planner_architect"]["model"], "openai/gpt-5")
                self.assertEqual(by_role["planner_architect"]["parent_node"], "sabrina")
                self.assertEqual(by_role["execution_specialist"]["parent_node"], "sabrina")
                self.assertFalse(st["execution_authorized"])

                out = runner.wait_for_mission(res["mission_id"], timeout=6.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out["execution_authorized"])
                self.assertIn("`False`", out["report_markdown"])
                self.assertTrue(runner.wait_until_idle(timeout=4.0))
            finally:
                runner.shutdown()

    def test_mission_heartbeats_deliberations_and_active_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts",
                spindown_delay_sec=0.2,
                step_delay_sec=0.02,
            )
            try:
                res = runner.launch_mission("Audit alpha_module pipeline handoffs", mode="hive_council")
                self.assertTrue(res["ok"])
                mid = res["mission_id"]

                # Active state exposes workers with progress and heartbeats, and active handoff edges
                st_active = runner.get_state()
                self.assertIn("active_handoffs", st_active)
                self.assertIn("deliberations", st_active)
                self.assertGreaterEqual(len(st_active["active_handoffs"]), 3)
                for h in st_active["active_handoffs"]:
                    self.assertIn("from_agent", h)
                    self.assertIn("to_agent", h)
                    self.assertIn("from_role", h)
                    self.assertIn("to_role", h)
                    self.assertIn("edge_class", h)
                    self.assertTrue(h["edge_class"].startswith("edge-"))

                for w in st_active["workers"]:
                    self.assertIn("last_heartbeat_at", w)
                    self.assertIn("current_step", w)
                    self.assertIn("progress_pct", w)

                out = runner.wait_for_mission(mid, timeout=8.0)
                self.assertIsNotNone(out)
                self.assertTrue(runner.wait_until_idle(timeout=5.0))

                st_done = runner.get_state()
                self.assertGreaterEqual(len(st_done["deliberations"]), 3)
                stages = [d["stage"] for d in st_done["deliberations"]]
                self.assertIn("PLAN_DISPATCH", stages)
                self.assertIn("HANDOFF_TO_EXECUTOR", stages)
                self.assertIn("VERDICT_FINAL", stages)

                # Idle state has 0 active workers and 0 active handoffs
                self.assertEqual(len(st_done["active_handoffs"]), 0)
                self.assertEqual(st_done["active_worker_count"], 0)

                # clear_completed cleans up deliberations
                runner.clear_completed()
                st_cleared = runner.get_state()
                self.assertEqual(st_cleared["deliberations"], [])
                self.assertEqual(st_cleared["active_handoffs"], [])
            finally:
                runner.shutdown()

    def test_live_llm_execution_runs_all_four_stages_with_file_excerpts(self) -> None:
        calls: list[dict[str, str]] = []

        def fake_llm_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            calls.append(
                {
                    "role": role,
                    "model_id": model_id,
                    "system_prompt": system_prompt,
                    "user_prompt": user_prompt,
                }
            )
            if role == "critic_verifier":
                text = "PASS: Deliverable verified against alpha_module.py with execution_authorized=False."
            elif role == "execution_specialist":
                text = "REAL_DELIVERABLE: run_alpha() returns 42 cleanly; no unsafe side effects."
            else:
                text = f"Stage {role} completed with real model reasoning."
            return {
                "ok": True,
                "text": text,
                "model": "gemini-3.5-flash-lite",
                "provider": "gemini",
                "latency_ms": 12.5,
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=fake_llm_caller,
            )
            try:
                res = runner.launch_mission("Audit alpha_module return value and safety")
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["execution_mode"], "LIVE_LLM")
                self.assertEqual(out["live_llm_calls"], 4)
                self.assertEqual(out["state"], "PASS")
                self.assertTrue(out["verdict"]["passed"])
                self.assertFalse(out["execution_authorized"])
                self.assertIn("## Mission Deliverable (Execution Specialist Output)", out["report_markdown"])
                self.assertIn("REAL_DELIVERABLE: run_alpha() returns 42 cleanly", out["report_markdown"])

                called_roles = [c["role"] for c in calls]
                self.assertEqual(
                    called_roles,
                    ["planner_architect", "research_scout", "execution_specialist", "critic_verifier"],
                )
                scout_call = next(c for c in calls if c["role"] == "research_scout")
                self.assertIn("def run_alpha() -> int:", scout_call["user_prompt"])
                self.assertNotIn("do not scan", scout_call["user_prompt"])
                self.assertNotIn("alpha_secret.txt", scout_call["user_prompt"])
                self.assertTrue(runner.wait_until_idle(timeout=5.0))
            finally:
                runner.shutdown()

    def test_live_llm_critic_review_blocks_pass_verdict(self) -> None:
        def review_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            text = (
                "REVIEW: Missing negative boundary check on run_alpha."
                if role == "critic_verifier"
                else f"Output for {role}"
            )
            return {
                "ok": True,
                "text": text,
                "model": "openai/gpt-5-mini",
                "provider": "openai",
                "latency_ms": 18.0,
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=review_caller,
            )
            try:
                res = runner.launch_mission("Review alpha_module edge cases")
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["execution_mode"], "LIVE_LLM")
                self.assertEqual(out["live_llm_calls"], 4)
                self.assertFalse(out["verdict"]["passed"])
                self.assertEqual(out["state"], "DONE")
                self.assertTrue(any("REVIEW:" in issue for issue in out["verdict"]["issues"]))
                self.assertTrue(runner.wait_until_idle(timeout=5.0))
            finally:
                runner.shutdown()

    def test_dry_run_no_llm_never_fakes_pass(self) -> None:
        def failing_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            return {
                "ok": False,
                "error": "No unlocked provider API key in vault",
                "provider": "none",
                "model": model_id,
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=failing_caller,
            )
            try:
                res = runner.launch_mission("Explain alpha_module without any unlocked key")
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["execution_mode"], "DRY_RUN_NO_LLM")
                self.assertEqual(out["live_llm_calls"], 0)
                self.assertEqual(out["state"], "FAILED")
                self.assertFalse(out["verdict"]["passed"])
                self.assertFalse(out["execution_authorized"])
                self.assertIn("## Notice: No Live LLM Executed", out["report_markdown"])
                self.assertIn("`FAILED`", out["report_markdown"])
                self.assertTrue(runner.wait_until_idle(timeout=5.0))
            finally:
                runner.shutdown()

    def test_llm_caller_failure_marks_workers_and_mission_failed(self) -> None:
        def http_400_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            return {
                "ok": False,
                "error": "HTTP 400: Invalid model or API key",
                "provider": "gemini",
                "model": model_id,
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=http_400_caller,
                auto_spindown=False,
            )
            try:
                res = runner.launch_mission("Audit alpha_module with failing LLM")
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["state"], "FAILED")
                self.assertEqual(out["live_llm_calls"], 0)
                self.assertFalse(out["verdict"]["passed"])
                self.assertIn("HTTP 400", out["error"])

                st = runner.get_state()
                self.assertEqual(st["status"], "IDLE")
                self.assertEqual(st["active_worker_count"], 0)
                self.assertEqual(st["verifier_verdict"], "FAILED (LLM Error)")
                primary = [w for w in st["workers"] if not w.get("recovery_for")]
                self.assertEqual(len(primary), 4)
                self.assertEqual(len(out["recovery_attempts"]), 3)
                self.assertTrue(all(attempt["status"] == "FAILED" for attempt in out["recovery_attempts"]))
                for w in primary:
                    self.assertEqual(w["status"], "FAILED")
                    self.assertFalse(w["llm_used"])
                    self.assertIn("HTTP 400", w["llm_error"])
                    self.assertTrue(w["output_preview"].startswith("LLM Error:"))
                    self.assertTrue(w["detail"].startswith("LLM failed:"))

                self.assertEqual(st["objectives"][0]["state"], "FAILED")
                self.assertIn("HTTP 400", st["objectives"][0]["last_event"])
                self.assertEqual(st["research_episodes"][0]["status"], "FAILED")
                self.assertTrue(any(item["badge"] == "LLM_ERROR" for item in st["activity_feed"]))

                cleared = runner.clear_completed()
                self.assertTrue(cleared["ok"])
                st_cleared = runner.get_state()
                self.assertEqual(st_cleared["workers"], [])
                self.assertEqual(len(st_cleared["recent_spindowns"]), len(st["workers"]))
                for sd in st_cleared["recent_spindowns"]:
                    self.assertEqual(sd["reason"], "cleared")
                    self.assertEqual(sd["final_status"], "FAILED")
                    self.assertFalse(sd["llm_used"])
                    self.assertIn("HTTP 400", sd["llm_error"])
                    self.assertTrue(sd["output_preview"].startswith("LLM Error:"))
            finally:
                runner.shutdown()

    def test_llm_caller_success_populates_worker_outputs_and_pass(self) -> None:
        def ok_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            text = (
                "PASS: Verified alpha_module cleanly."
                if role == "critic_verifier"
                else f"Live output from {role}"
            )
            return {
                "ok": True,
                "text": text,
                "model": "gemini-2.5-flash",
                "provider": "gemini",
                "latency_ms": 23.4,
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=ok_caller,
                auto_spindown=False,
            )
            try:
                res = runner.launch_mission("Verify alpha_module with live LLM")
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["execution_mode"], "LIVE_LLM")
                self.assertEqual(out["state"], "PASS")
                self.assertTrue(out["verdict"]["passed"])

                st = runner.get_state()
                self.assertEqual(st["status"], "IDLE")
                self.assertEqual(st["active_worker_count"], 0)
                self.assertEqual(len(st["workers"]), 4)
                by_role = {w["role"]: w for w in st["workers"]}
                self.assertEqual(by_role["critic_verifier"]["status"], "PASS")
                for role_name in ("planner_architect", "research_scout", "execution_specialist"):
                    self.assertEqual(by_role[role_name]["status"], "DONE")
                for w in st["workers"]:
                    self.assertTrue(w["llm_used"])
                    self.assertEqual(w["provider"], "gemini")
                    self.assertEqual(w["model"], "gemini-2.5-flash")
                    self.assertEqual(w["latency_ms"], 23.4)
                    self.assertTrue(w["llm_output"])
                    self.assertEqual(w["output_preview"], w["llm_output"][:500])
            finally:
                runner.shutdown()

    def test_auto_spindown_false_retains_terminal_workers_until_cleared_or_superseded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.05,
                step_delay_sec=0.01,
                auto_spindown=False,
            )
            try:
                m1 = runner.launch_mission("First offline mission on alpha_module")
                out1 = runner.wait_for_mission(m1["mission_id"], timeout=8.0)
                self.assertIsNotNone(out1)
                assert out1 is not None
                self.assertEqual(out1["state"], "PASS")

                # Wait longer than spindown_delay_sec; workers must still be retained
                time.sleep(0.15)
                st1 = runner.get_state()
                self.assertEqual(st1["status"], "IDLE")
                self.assertEqual(st1["active_worker_count"], 0)
                self.assertEqual(len(st1["workers"]), 4)
                self.assertEqual(st1["recent_spindowns"], [])

                # Launching a second mission sweeps previous terminal workers as superseded_by_new_mission
                m2 = runner.launch_mission("Second offline mission on alpha_module")
                self.assertTrue(m2["ok"])
                st_mid = runner.get_state()
                self.assertEqual(len(st_mid["recent_spindowns"]), 4)
                self.assertTrue(
                    all(sd["reason"] == "superseded_by_new_mission" for sd in st_mid["recent_spindowns"])
                )

                out2 = runner.wait_for_mission(m2["mission_id"], timeout=8.0)
                self.assertIsNotNone(out2)
                st2 = runner.get_state()
                self.assertEqual(st2["status"], "IDLE")
                self.assertEqual(len(st2["workers"]), 4)

                # clear_completed sweeps retained terminal workers with reason="cleared"
                cleared = runner.clear_completed()
                self.assertTrue(cleared["ok"])
                st3 = runner.get_state()
                self.assertEqual(st3["workers"], [])
                self.assertEqual(len(st3["recent_spindowns"]), 4)
                self.assertTrue(all(sd["reason"] == "cleared" for sd in st3["recent_spindowns"]))
            finally:
                runner.shutdown()

    def test_large_multipart_mission_spawns_dynamic_parallel_subagents(self) -> None:
        directive = (
            "# COLLECTIVE IMPLEMENTATION DIRECTIVE: Live Telemetry\n"
            "## Objective\n"
            "Build live command and state telemetry.\n"
            "## 1. Telemetry Bus & Schema\n"
            "Emit structured step progress events.\n"
            "## 2. Lifecycle State Machine\n"
            "Track worker transitions cleanly.\n"
            "## 3. UI Event Stream\n"
            "Render live worker trees in Swarm Monitor.\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                auto_spindown=False,
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
            )
            try:
                res = runner.launch_mission(directive)
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                # 4 base pipeline workers + 3 dynamically spawned workstream sub-agents = 7
                self.assertEqual(len(out["subtasks"]), 7)
                st = runner.get_state()
                sub_workers = [w for w in st["workers"] if "subagent_" in str(w.get("id") or "")]
                self.assertEqual(len(sub_workers), 3)
                self.assertTrue(all(w.get("progress_pct") == 100 for w in sub_workers))
                self.assertTrue(all(w.get("completed_steps") == 3 and w.get("total_steps") == 3 for w in sub_workers))
            finally:
                runner.shutdown()

    def test_swarm_board_mentions_wakeup_and_jsonl_persistence(self) -> None:
        from sabrina_console_desktop.swarm_board import SwarmBoard

        with tempfile.TemporaryDirectory() as tmp:
            board_path = Path(tmp) / "m-test_board.jsonl"
            board = SwarmBoard("m-test", board_path)
            board.register_worker("coordinator")
            board.register_worker("worker-a", aliases=["parser"])
            board.register_worker("verifier")

            # Post addressed to @parser -> wakes worker-a, NOT verifier
            p1 = board.post(author="coordinator", text="@parser check src/parser.py", kind="brief")
            self.assertEqual(p1.seq, 1)
            self.assertEqual(p1.mentions, ("parser",))
            got_a = board.wait_for_mentions("worker-a", timeout=0.5)
            self.assertEqual([p.seq for p in got_a], [1])
            got_v = board.wait_for_mentions("verifier", timeout=0.05)
            self.assertEqual(got_v, [])

            # Peer-to-peer handoff @verifier check #1 -> wakes verifier
            p2 = board.post(author="worker-a", text="@verifier check #1", kind="handoff")
            self.assertEqual(p2.seq, 2)
            got_v2 = board.wait_for_mentions("verifier", timeout=0.5)
            self.assertEqual([p.seq for p in got_v2], [2])

            # Reload from JSONL preserves monotonic seq & posts
            board.close()
            board2 = SwarmBoard("m-test", board_path)
            self.assertEqual(board2.latest_seq, 2)
            self.assertEqual(len(board2.snapshot()), 2)

    def test_fallback_does_not_block_error_detection_and_surfaces_warning(self) -> None:
        def fallback_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            # Simulate gemini-4-carbon failing with 429 and falling back to gemini-2.5-flash
            return {
                "ok": True,
                "text": f"Deliverable produced by fallback model for {role}.",
                "model": "gemini-2.5-flash",
                "provider": "gemini",
                "used_fallback": True,
                "primary_error": "gemini/gemini-4-carbon: 429 Rate limit exceeded (limit: 0 input tokens per minute)",
                "fallback_errors": [
                    "gemini/gemini-4-carbon: 429 Rate limit exceeded (limit: 0 input tokens per minute)"
                ],
                "requested_model": "gemini-4-carbon",
                "attempts": 2,
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=fallback_caller,
                auto_spindown=False,
            )
            try:
                res = runner.launch_mission("Audit with fallback LLM")
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None

                # Mission succeeds, but detected fallback errors are NOT swallowed
                self.assertEqual(out["live_llm_calls"], 4)
                self.assertTrue(len(out["llm_errors"]) > 0)
                self.assertIn("429 Rate limit exceeded", out["llm_errors"][0])

                # Subtask records must retain fallback metadata and primary error
                p_task = next(st for st in out["subtasks"] if st.get("role") == AgentRole.PLANNER_ARCHITECT.value)
                self.assertTrue(p_task["fallback_used"])
                self.assertIn("429 Rate limit exceeded", p_task["primary_error"])
                self.assertEqual(p_task["requested_model"], "gemini-4-carbon")
                self.assertEqual(p_task["model"], "gemini-2.5-flash")

                # Activity feed must surface LLM_WARN badge
                st = runner.get_state()
                warn_events = [ev for ev in st["activity_feed"] if ev.get("badge") == "LLM_WARN"]
                self.assertTrue(len(warn_events) > 0)
                self.assertIn("Fallback model", warn_events[0]["summary"])
                self.assertIn("429 Rate limit exceeded", warn_events[0]["summary"])

                # Mission report markdown must include fallback section
                self.assertIn("## Model Fallbacks & Detected Errors", out["report_markdown"])
                self.assertIn("gemini-4-carbon", out["report_markdown"])
                self.assertIn("429 Rate limit exceeded", out["report_markdown"])
            finally:
                runner.shutdown()

    def test_subtask_failure_blocks_mission_pass_verdict_and_reports_issues(self) -> None:
        def partial_failure_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            if role == AgentRole.PLANNER_ARCHITECT.value:
                return {
                    "ok": False,
                    "error": "gemini-4-carbon 429 quota exhaustion",
                    "model": model_id,
                    "provider": "gemini",
                }
            return {
                "ok": True,
                "text": f"Output for {role}",
                "model": "gemini-2.5-flash",
                "provider": "gemini",
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=partial_failure_caller,
                auto_spindown=False,
            )
            try:
                res = runner.launch_mission("Audit with failing planner")
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None

                # Subtask failure must prevent mission from reporting PASS
                self.assertEqual(out["state"], "FAILED")
                self.assertFalse(out["verdict"]["passed"])
                self.assertTrue(any("Planner Architect failed" in issue or "Subtask failure" in issue for issue in out["verdict"]["issues"]))
                self.assertIn("429 quota exhaustion", out["error"])
            finally:
                runner.shutdown()

    def test_launch_mission_respects_custom_coordinator_worker_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                auto_spindown=False,
            )
            try:
                # 1. Lean mode (1:2) -> 2 unique workers, all 4 pipeline stages still complete
                res_lean = runner.launch_mission("Lean 1:2 check", coordinator_count=1, workers_per_coordinator=2)
                self.assertTrue(res_lean["ok"])
                self.assertEqual(res_lean["ratio"], "1:2")
                self.assertEqual(len(res_lean["workers_spawned"]), 2)
                out_lean = runner.wait_for_mission(res_lean["mission_id"], timeout=8.0)
                self.assertIsNotNone(out_lean)

                # 2. Dual-coordinator sharding (2:3) -> 2 sub-coordinators + 4 core + 2 fan-out shards = 6 workers
                res_dual = runner.launch_mission("Dual 2:3 check", coordinator_count=2, workers_per_coordinator=3)
                self.assertTrue(res_dual["ok"])
                self.assertEqual(res_dual["ratio"], "2:3")
                self.assertEqual(res_dual["coordinator_count"], 2)
                self.assertEqual(res_dual["workers_per_coordinator"], 3)
                st_dual = runner.get_state()
                coords = [w for w in st_dual["workers"] if w.get("role") == "coordinator"]
                self.assertEqual(len(coords), 2)
                out_dual = runner.wait_for_mission(res_dual["mission_id"], timeout=8.0)
                self.assertIsNotNone(out_dual)

                # 3. Elastic quorum budding (1:10 with quorum=6, prune=2) -> ELASTIC_BUD then ELASTIC_PRUNE
                res_bud = runner.launch_mission(
                    "Large 1:10 elastic quorum test",
                    coordinator_count=1,
                    workers_per_coordinator=10,
                    quorum_split_threshold=6,
                    enable_work_stealing=True,
                )
                self.assertTrue(res_bud["ok"])
                out_bud = runner.wait_for_mission(res_bud["mission_id"], timeout=10.0)
                self.assertIsNotNone(out_bud)
                st_bud = runner.get_state()
                stages = [d.get("stage") for d in st_bud["deliberations"] if d.get("mission_id") == res_bud["mission_id"]]
                self.assertIn("ELASTIC_BUD", stages)
                self.assertIn("ELASTIC_PRUNE", stages)

                # 4. Strict INV-03 cap: max_total_agents=10 on 1:10 must trigger ELASTIC_BUD_CAPPED (never breach 10)
                #    and max_concurrency=2 must exercise real work-stealing across queued tasks
                runner.configure_topology(max_total_agents=10, max_concurrency=2)
                res_cap = runner.launch_mission(
                    "Capped 1:10 work-steal test",
                    coordinator_count=1,
                    workers_per_coordinator=10,
                    quorum_split_threshold=6,
                    enable_work_stealing=True,
                )
                out_cap = runner.wait_for_mission(res_cap["mission_id"], timeout=10.0)
                self.assertIsNotNone(out_cap)
                assert out_cap is not None
                st_cap = runner.get_state()
                cap_stages = [d.get("stage") for d in st_cap["deliberations"] if d.get("mission_id") == res_cap["mission_id"]]
                self.assertIn("ELASTIC_BUD_CAPPED", cap_stages)
                self.assertIn("WORK_STEAL", cap_stages)
                self.assertTrue(any(st.get("stolen") for st in out_cap["subtasks"]))
            finally:
                runner.shutdown()

    def test_executor_closed_loop_harness_diagnostics_and_repair(self) -> None:
        exec_calls = 0

        def repair_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            nonlocal exec_calls
            if role == AgentRole.EXECUTION_SPECIALIST.value:
                exec_calls += 1
                if exec_calls == 1:
                    # First attempt writes invalid Python syntax
                    return {
                        "ok": True,
                        "text": "```write_file:generated_mod.py\ndef broken_fn(:\n    return 1\n```",
                        "model": "gemini-2.5-flash",
                        "provider": "gemini",
                    }
                # Bounded repair turn fixes the syntax error
                return {
                    "ok": True,
                    "text": "```write_file:generated_mod.py\ndef fixed_fn():\n    return 1\n```",
                    "model": "gemini-2.5-flash",
                    "provider": "gemini",
                }
            return {
                "ok": True,
                "text": f"OK for {role}",
                "model": "gemini-2.5-flash",
                "provider": "gemini",
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=repair_caller,
                auto_spindown=False,
            )
            try:
                res = runner.launch_mission("Create generated_mod.py", allow_workspace_writes=True)
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                # Repair loop ran twice for Execution Specialist and produced valid Python
                self.assertEqual(exec_calls, 2)
                self.assertIn("def fixed_fn():", (ws / "generated_mod.py").read_text(encoding="utf-8"))
                st = runner.get_state()
                self.assertTrue(len(st["harness_events"]) > 0)
            finally:
                runner.shutdown()

    def test_peer_assisted_self_healing_recovers_failed_executor(self) -> None:
        def timeout_then_peer_recover(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            if role == AgentRole.EXECUTION_SPECIALIST.value:
                return {
                    "ok": False,
                    "error": "gemini/gemini-3.5-flash: The read operation timed out; ollama/ollama/gemma3:12b: timed out",
                    "model": model_id,
                    "provider": "gemini",
                }
            if role == AgentRole.CRITIC_VERIFIER.value:
                return {
                    "ok": True,
                    "text": '{"passed": true, "reason": "Peer-recovered deliverable verified.", "issues": []}',
                    "model": "gemini-3.5-flash-lite",
                    "provider": "gemini",
                }
            return {
                "ok": True,
                "text": f"Concrete workspace-grounded output from {role}.",
                "model": "gemini-3.5-flash-lite",
                "provider": "gemini",
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=timeout_then_peer_recover,
                auto_spindown=False,
            )
            try:
                res = runner.launch_mission("Recover from Execution Specialist timeout")
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertNotEqual(out["state"], "FAILED")
                self.assertTrue(out["verdict"]["passed"])
                st = runner.get_state()
                badges = [ev.get("badge") for ev in st["activity_feed"]]
                self.assertIn("PEER_ASSIST", badges)
                self.assertIn("SELF_HEALED", badges)
                attempt, = out["recovery_attempts"]
                workers = {worker["id"]: worker for worker in st["workers"]}
                parent = workers[attempt["failed_worker_id"]]
                child = workers[attempt["worker_id"]]
                self.assertEqual(parent["status"], "FAILED")
                self.assertIn("timed out", parent["llm_error"])
                self.assertEqual(parent["recovered_by"], child["id"])
                self.assertEqual(child["parent_id"], parent["id"])
                self.assertEqual(child["status"], "DONE")
                self.assertIn(child["id"], out["report_markdown"])
            finally:
                runner.shutdown()

    def test_carbon_pushes_distribution_algorithm_and_inner_subagent_comms(self) -> None:
        observed_sub2_prompts: list[str] = []
        observed_coordinator_prompts: list[str] = []

        def carbon_algo_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            if role == "coordinator" and any(m in model_id for m in ("gemini-4-carbon", "gemini-4-neon", "gemini-neo")):
                observed_coordinator_prompts.append(user_prompt)
                return {
                    "ok": True,
                    "text": (
                        '{"algorithm":"stigmergic_blackboard","theta":0.42,'
                        '"communication_protocol":"blackboard_handoff",'
                        '"workstreams":['
                        '{"key":"subagent_2","title":"Synthesize auth fix","role":"execution_specialist","stimulus":1.1,"depends_on":["subagent_1"],"peer_targets":["subagent_1"]},'
                        '{"key":"subagent_1","title":"Inspect auth module","role":"research_scout","stimulus":1.5,"depends_on":[],"peer_targets":["subagent_2"]}'
                        ']}'
                    ),
                    "model": model_id,
                    "provider": "gemini",
                }
            if user_prompt.startswith("Assigned Workstream:\nInspect auth module"):
                time.sleep(1.6)
                return {"ok": True, "text": "Verified prerequisite result.", "model": model_id, "provider": "gemini"}
            if user_prompt.startswith("Assigned Workstream:\nSynthesize auth fix"):
                observed_sub2_prompts.append(user_prompt)
            if role == AgentRole.RESEARCH_SCOUT.value:
                text = "Unsupported Scout claim."
                if "External Grounding Output:" not in user_prompt:
                    text += "\n```tool:fetch_url\nhttps://example.com/verified\n```\n```tool:fetch_url\nhttps://example.com/unverified\n```"
                return {"ok": True, "text": text, "model": model_id, "provider": "gemini"}
            if role == AgentRole.CRITIC_VERIFIER.value:
                return {
                    "ok": True,
                    "text": '{"passed": true, "reason": "Carbon algo and inner comms verified.", "issues": []}',
                    "model": "gemini-3.5-flash-lite",
                    "provider": "gemini",
                }
            return {
                "ok": True,
                "text": f"Finding from {role}: auth token verified.",
                "model": "gemini-3.5-flash-lite",
                "provider": "gemini",
            }

        def fetch(kind: str, url: str) -> dict[str, object]:
            if url.endswith("/verified"):
                return {"ok": True, "verified": True, "source_url": url, "text": "Source-backed finding."}
            return {"ok": True, "source_url": url, "text": "Provider did not read the page."}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=carbon_algo_caller,
                web_grounding_fn=fetch,
                auto_spindown=False,
            )
            try:
                runner.max_concurrency = 1
                res = runner.launch_mission(
                    "Audit auth with Carbon-pushed distribution and public docs",
                    coordinator_count=1,
                    workers_per_coordinator=6,
                )
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                st = runner.get_state()
                stages = [d.get("stage") for d in st["deliberations"] if d.get("mission_id") == res["mission_id"]]
                self.assertIn("CARBON_ALGO", stages)
                self.assertIn("INNER_COMM", stages)
                self.assertTrue(observed_coordinator_prompts)
                self.assertIn("Source-backed finding.", observed_coordinator_prompts[0])
                self.assertNotIn("Unsupported Scout claim.", observed_coordinator_prompts[0])
                self.assertNotIn("Provider did not read the page.", observed_coordinator_prompts[0])
                self.assertTrue(any("stigmergic_blackboard" in p and "INNER SUB-AGENT COMMUNICATIONS" in p for p in observed_sub2_prompts))
                self.assertIn("Completed prerequisite subagent_1:\nVerified prerequisite result.", observed_sub2_prompts[0])
            finally:
                runner.shutdown()

    def test_offline_checkout_rejects_external_script_even_when_critic_passes(self) -> None:
        def caller(role: str, model: str, system: str, prompt: str) -> dict[str, object]:
            output = "Build local storefront."
            if role == AgentRole.EXECUTION_SPECIALIST.value:
                output = '```write_file:index.html\n<script src="https://example.com/checkout.js"></script>\n```'
            elif role == AgentRole.CRITIC_VERIFIER.value:
                output = "PASS: Looks complete."
            return {"ok": True, "text": output, "model": model, "provider": "gemini"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = self._make_sample_workspace(root)
            runner = LiveSwarmRunner(
                workspace_root=workspace, artifacts_dir=root / "missions",
                step_delay_sec=0.01, llm_caller=caller, auto_spindown=False,
            )
            try:
                launch = runner.launch_mission(
                    "Build a florist with offline simulated checkout",
                    coordinator_count=1, workers_per_coordinator=3,
                    allow_workspace_writes=True, max_refinement_rounds=0,
                )
                result = runner.wait_for_mission(launch["mission_id"], timeout=8.0)
                self.assertIsNotNone(result)
                self.assertFalse((workspace / "index.html").exists())
                self.assertFalse(result["verdict"]["passed"])
                self.assertTrue(any("Offline checkout" in issue for issue in result["verdict"]["issues"]))
            finally:
                runner.shutdown()

    def test_workstreams_with_unmet_prerequisites_never_invoke_the_model(self) -> None:
        invoked: list[str] = []

        def caller(role: str, model: str, system: str, prompt: str) -> dict[str, object]:
            if role == "coordinator":
                output = (
                    '{"workstreams":['
                    '{"key":"research","title":"Obtain source evidence","role":"research_scout"},'
                    '{"key":"build","title":"Build from evidence","depends_on":["research"]},'
                    '{"key":"missing","title":"Missing prerequisite","depends_on":["absent"]},'
                    '{"key":"cycle","title":"Cyclic prerequisite","depends_on":["cycle"]}'
                    ']}'
                )
            elif prompt.startswith("Assigned Workstream:\n"):
                invoked.append(prompt.splitlines()[1])
                output = "INSUFFICIENT_CONTEXT"
            elif prompt.startswith("Original stage failed"):
                output = "INSUFFICIENT_CONTEXT"
            else:
                output = "No files yet."
            return {"ok": True, "text": output, "model": model, "provider": "gemini"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = LiveSwarmRunner(
                workspace_root=self._make_sample_workspace(root),
                artifacts_dir=root / "missions",
                step_delay_sec=0.01,
                llm_caller=caller,
                auto_spindown=False,
            )
            try:
                launch = runner.launch_mission(
                    "Research and build from evidence",
                    coordinator_count=1,
                    workers_per_coordinator=8,
                )
                result = runner.wait_for_mission(launch["mission_id"], timeout=8.0)
                self.assertIsNotNone(result)
                self.assertEqual(result["state"], "FAILED")
                self.assertFalse(result["verdict"]["passed"])
                self.assertTrue(Path(result["artifact_path"]).is_file())
                self.assertIn("Blocked by failed, missing, or cyclic prerequisite", result["report_markdown"])
                self.assertIn("- **Passed:** `False`", result["report_markdown"])
                self.assertEqual(invoked, ["Obtain source evidence"])
                blocked = {
                    w["task"] for w in runner.get_state()["workers"]
                    if w.get("current_step") == "dependency_blocked"
                }
                self.assertEqual(blocked, {"Build from evidence", "Missing prerequisite", "Cyclic prerequisite"})
            finally:
                runner.shutdown()

    def test_verifier_blocks_hallucinated_pass_on_insufficient_context_or_unapplied_writes(self) -> None:
        def hallucinating_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
            if role == AgentRole.CRITIC_VERIFIER.value:
                # Critic naively says PASS even though deliverable is INSUFFICIENT_CONTEXT / unapplied write
                return {"ok": True, "text": '{"passed": true, "reason": "Looks good!"}', "model": "gemini-2.5-flash", "provider": "gemini"}
            if role in (AgentRole.EXECUTION_SPECIALIST.value, AgentRole.RESEARCH_SCOUT.value, "coordinator"):
                return {"ok": True, "text": "INSUFFICIENT_CONTEXT", "model": "gemini-2.5-flash", "provider": "gemini"}
            return {"ok": True, "text": f"OK from {role}", "model": "gemini-2.5-flash", "provider": "gemini"}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws = self._make_sample_workspace(tmp_dir)
            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=tmp_dir / "artifacts" / "missions",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=hallucinating_caller,
                auto_spindown=False,
            )
            try:
                res = runner.launch_mission("Attempt task with INSUFFICIENT_CONTEXT")
                out = runner.wait_for_mission(res["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertFalse(out["verdict"]["passed"])
                self.assertIn(out["state"], ("DONE", "FAILED"))
                self.assertTrue(any("INSUFFICIENT_CONTEXT" in i for i in out["verdict"]["issues"]))
            finally:
                runner.shutdown()


if __name__ == "__main__":
    unittest.main()
