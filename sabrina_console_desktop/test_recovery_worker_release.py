"""Exercise the worker release gate and a live retry in the same mission."""
import tempfile
import subprocess
import sys
import unittest
from pathlib import Path
from threading import Event

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.recovery_worker_release import (
    BUILTIN,
    RecoveryWorkerReleases,
    WorkerReleaseError,
    compact_source,
)


class TestRecoveryWorkerRelease(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.releases = RecoveryWorkerReleases(self.root / "worker-releases")
        self.context = "Keep the task.\n" + "Diagnostic data.\n" * 800 + "Keep the answer format."
        self.candidate = compact_source("timeout").replace("head = room // 2", "head = (room // 2) - 10", 1)

    def test_validation_rejects_invalid_source_and_rolls_back_to_previous_worker(self):
        self.assertEqual(self.releases.active_revision, BUILTIN)
        invalid = "def compact(context, limit, fault):\n    if limit < 1:\n        import os\n    return context\n"
        with self.assertRaisesRegex(WorkerReleaseError, "Import"):
            self.releases.promote(invalid, BUILTIN)
        self.assertEqual(self.releases.active_revision, BUILTIN)

        discarded_evidence = "def compact(context, limit, fault):\n    return context[:limit]\n"
        with self.assertRaisesRegex(WorkerReleaseError, "protected context"):
            self.releases.promote(discarded_evidence, BUILTIN)
        before = self.releases.prepare(self.context, 6000, "timeout")
        revision = self.releases.promote(self.candidate, before.revision, improve_fault="timeout",
                                         live_context=self.context)
        after = self.releases.prepare(self.context, 6000, "timeout")
        self.assertEqual(after.revision, revision)
        self.assertEqual(len(before.context), 6000)
        self.assertEqual(len(after.context), 3000)
        self.assertTrue(after.context.endswith("Keep the answer format."))
        with self.assertRaisesRegex(WorkerReleaseError, "changed"):
            self.releases.promote(self.candidate, BUILTIN)
        self.assertEqual(self.releases.rollback(revision), BUILTIN)
        self.assertEqual(self.releases.prepare(self.context, 6000, "timeout").context, before.context)

    def test_cancelled_promotion_and_damaged_release_leave_a_worker_available(self):
        cancel = Event()
        cancel.set()
        with self.assertRaisesRegex(WorkerReleaseError, "cancelled"):
            self.releases.promote(self.candidate, BUILTIN, cancel=cancel)
        self.assertEqual(self.releases.active_revision, BUILTIN)
        revision = self.releases.promote(self.candidate, BUILTIN)
        (self.releases.root / f"{revision}.py").write_text(self.candidate + "# changed\n", encoding="utf-8")
        prepared = RecoveryWorkerReleases(self.releases.root).prepare(self.context, 6000, "timeout")
        self.assertEqual(prepared.revision, BUILTIN)
        self.assertTrue(prepared.warning)

    def test_agent_source_is_validated_then_used_by_the_same_active_mission(self):
        workspace = self.root / "sample"
        workspace.mkdir()
        (workspace / "sample.py").write_text("def count():\n    return 42\n", encoding="utf-8")
        calls = []

        def primary(role, model, system, prompt):
            if role == "execution_specialist":
                return {"ok": False, "error": "Model context length exceeded"}
            if role == "critic_verifier":
                verdict = "PASS: Found the result." if "Returns 42." in prompt else "REVIEW: Missing result."
                return {"ok": True, "text": verdict, "model": model}
            return {"ok": True, "text": "Inspect the sample evidence.", "model": model}

        def recovery(policy, system, prompt):
            if system.startswith("Author a data-only"):
                calls.append("proposal")
                return {"ok": True, "text": self.candidate}
            calls.append(len(prompt))
            if len(prompt) > 3600:
                return {"ok": False, "error": "Model context length exceeded"}
            return {"ok": True, "text": "Returns 42.", "model": policy.model}

        runner = LiveSwarmRunner(workspace_root=workspace, artifacts_dir=self.root / "reports",
                                 step_delay_sec=0, auto_spindown=False, llm_caller=primary,
                                 recovery_worker_releases=self.releases, recovery_llm_caller=recovery)
        self.addCleanup(runner.shutdown)
        goal = "Report the return value in sample.py. " + "Sample diagnostic input. " * 300
        mission_id = runner.launch_mission(goal)["mission_id"]
        output = runner.wait_for_mission(mission_id, timeout=15)
        self.assertIsNotNone(output)
        self.assertEqual(output["mission_id"], mission_id)
        self.assertEqual(output["state"], "PASS")
        self.assertFalse(output["execution_authorized"])
        self.assertEqual(calls[1], "proposal")
        old, new = output["recovery_attempts"]
        self.assertEqual(old["worker_revision"], BUILTIN)
        self.assertEqual(old["status"], "FAILED")
        self.assertEqual(new["worker_revision"], self.releases.active_revision)
        self.assertEqual(new["failed_worker_id"], old["worker_id"])
        self.assertEqual(new["status"], "DONE")
        self.assertLess(new["context_chars"], old["context_chars"])
        self.assertEqual(output["worker_hotfix"]["origin"], "agent proposal")
        self.assertIn("## Live Worker Update", output["report_markdown"])

    def test_ci_activation_affects_new_work_while_an_older_worker_drains(self):
        workspace = self.root / "sample"
        workspace.mkdir()
        started_old_worker = Event()
        finish_old_worker = Event()

        def primary(role, model, system, prompt):
            if role == "execution_specialist":
                return {"ok": False, "error": "Model timed out"}
            if role == "critic_verifier":
                return {"ok": True, "text": "PASS: The recovered result passed the fixture check.", "model": model}
            return {"ok": True, "text": "Inspect the fixture.", "model": model}

        def recover(policy, system, prompt):
            if "First fixture" in prompt:
                started_old_worker.set()
                if not finish_old_worker.wait(12):
                    raise TimeoutError("Test did not release the older worker")
            return {"ok": True, "text": "Recovered output.", "model": policy.model}

        runner = LiveSwarmRunner(workspace_root=workspace, artifacts_dir=self.root / "reports",
                                 step_delay_sec=0, auto_spindown=False, llm_caller=primary,
                                 recovery_worker_releases=self.releases, recovery_llm_caller=recover)
        self.addCleanup(runner.shutdown)
        self.addCleanup(finish_old_worker.set)
        notes = "Synthetic diagnostic context. " * 300
        first = runner.launch_mission("First fixture. " + notes)["mission_id"]
        self.assertTrue(started_old_worker.wait(5))
        candidate_file = self.root / "candidate.py"
        candidate_file.write_text(self.candidate, encoding="utf-8")
        deploy = subprocess.run(
            [sys.executable, "-B", "-m", "sabrina_console_desktop.recovery_worker_release",
             "--root", str(self.releases.root), "promote", str(candidate_file)],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, timeout=7, check=True,
        )
        active = deploy.stdout.strip()
        second = runner.launch_mission("Second fixture. " + notes)["mission_id"]
        second_result = runner.wait_for_mission(second, timeout=8)
        self.assertIsNotNone(second_result)
        self.assertEqual(second_result["state"], "PASS")
        self.assertEqual(second_result["recovery_attempts"][0]["worker_revision"], active)
        self.assertEqual(second_result["recovery_attempts"][0]["context_chars"], 3000)
        self.assertEqual(runner.get_state()["active_missions"][0]["id"], first)
        finish_old_worker.set()
        first_result = runner.wait_for_mission(first, timeout=8)
        self.assertIsNotNone(first_result)
        self.assertEqual(first_result["state"], "PASS")
        self.assertEqual(first_result["recovery_attempts"][0]["worker_revision"], BUILTIN)
        self.assertEqual(first_result["recovery_attempts"][0]["context_chars"], 6000)
