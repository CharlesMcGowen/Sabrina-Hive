"""Recovery routing with a live mission, a valid update, and an invalid candidate."""
import tempfile
import unittest
from pathlib import Path
from threading import Event

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.recovery_policy import RecoveryPolicy, promote


class TestRecoveryPolicy(unittest.TestCase):
    def make_runner(self, primary, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "alpha_module.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        runner = LiveSwarmRunner(
            workspace_root=root, artifacts_dir=root / "reports", step_delay_sec=0,
            llm_caller=primary, recovery_policy_path=root / "active.json", auto_spindown=False, **kwargs,
        )
        self.addCleanup(runner.shutdown)
        return runner, root

    def test_validated_policy_changes_between_workers_in_one_mission(self):
        executor_waiting = Event()
        resume_executor = Event()
        verifier_prompts = []

        def primary(role, model, system, prompt):
            if "Assigned Workstream:" in prompt:
                return {"ok": False, "error": "Workstream timed out"}
            if role == "planner_architect":
                return {"ok": False, "error": "Planner timed out"}
            if role == "execution_specialist":
                executor_waiting.set()
                if not resume_executor.wait(6):
                    raise TimeoutError("Test did not release the executor")
                return {"ok": False, "error": "Executor timed out"}
            if role == "critic_verifier":
                verifier_prompts.append(prompt)
                return {"ok": True, "text": "PASS: The recovered result matches the sample.", "model": model}
            return {"ok": True, "text": "Sample context about alpha_module.py.", "model": model}

        def recover(policy, system, prompt):
            summary = "The sample function returns 42." if "Executor timed out" in prompt else "Inspect alpha_module.py."
            return {"ok": True, "text": summary, "model": policy.model, "provider": "ollama"}

        runner, root = self.make_runner(primary, recovery_llm_caller=recover)
        self.addCleanup(resume_executor.set)
        active, candidate = root / "active.json", root / "candidate.json"
        candidate.write_text('{"model":"ollama/first:latest"}', encoding="utf-8")
        first = promote(candidate, active)
        result = runner.launch_mission("1. Inspect alpha_module\n2. Summarize the sample")
        self.assertTrue(executor_waiting.wait(5))
        mission = result["mission_id"]
        self.assertEqual(runner.get_state()["active_missions"][0]["id"], mission)
        candidate.write_text('{"timeout_seconds":900}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            promote(candidate, active)
        self.assertEqual(RecoveryPolicy.load(active), first)
        candidate.write_text('{"model":"ollama/second:latest","max_context_chars":3000}', encoding="utf-8")
        second = promote(candidate, active)
        resume_executor.set()
        output = runner.wait_for_mission(mission, timeout=8)
        assert output is not None
        self.assertEqual(output["state"], "PASS")
        attempts = output["recovery_attempts"]
        self.assertTrue(any("Workstream timed out" in row["initial_error"] for row in attempts))
        self.assertGreaterEqual(sum(row["policy_revision"] == first.revision for row in attempts), 2)
        self.assertEqual(attempts[-1]["policy_revision"], second.revision)
        self.assertIn("The sample function returns 42.", verifier_prompts[0])
        self.assertIn("The sample function returns 42.", output["report_markdown"].split("## Recovery Branches")[0])
        self.assertFalse(output["execution_authorized"])

    def test_stop_during_recovery_ignores_a_late_result(self):
        recovery_started = Event()
        recovery_release = Event()

        def primary(role, model, system, prompt):
            return {"ok": False, "error": "model timeout"}

        def recover(policy, system, prompt):
            recovery_started.set()
            recovery_release.wait(5)
            return {"ok": True, "text": "Late result.", "model": policy.model}

        runner, root = self.make_runner(primary, recovery_llm_caller=recover)
        self.addCleanup(recovery_release.set)
        launch = runner.launch_mission("Summarize alpha_module")
        self.assertTrue(recovery_started.wait(5))
        mission = launch["mission_id"]
        running_children = [w for w in runner.get_state()["workers"] if w.get("recovery_for")]
        self.assertEqual(len(running_children), 1)
        self.assertIn(running_children[0]["id"], runner.stop_mission(mission)["spun_down_workers"])
        recovery_release.set()
        output = runner.wait_for_mission(mission, timeout=8)
        assert output is not None
        self.assertEqual(output["state"], "STOPPED")
        self.assertFalse((root / "reports" / f"{mission}.md").exists())
        self.assertEqual(runner.get_state()["workers"], [])

    def test_agent_cap_blocks_recovery_and_mission_reports_failure(self):
        def primary(role, model, system, prompt):
            return {"ok": False, "error": "model timeout"}

        runner, _ = self.make_runner(primary)
        runner.configure_topology(max_total_agents=1)
        mission = runner.launch_mission("Summarize alpha_module")["mission_id"]
        output = runner.wait_for_mission(mission, timeout=8)
        assert output is not None
        self.assertEqual(output["state"], "FAILED")
        self.assertEqual(output["recovery_attempts"], [])
        self.assertTrue(any(event["badge"] == "RECOVERY_LIMIT" for event in runner.get_state()["activity_feed"]))
