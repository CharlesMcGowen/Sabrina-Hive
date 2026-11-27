"""Regression tests for mission m-a1414ff3, which reported PASS on a stub that failed 13 of 13 tests.

Covers: goal-named files reach the executor whole, goal-named tests decide PASS,
and a fallback model's PASS is not accepted without those tests.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sabrina_console_desktop import live_swarm_runner as lsr
from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from swarm_command.models import AgentRole

TEST_BODY = (
    "import unittest\nimport gate_mod\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_value(self):\n        self.assertEqual(gate_mod.value(), 2)\n"
)
GOOD = "```write_file:gate_mod.py\ndef value():\n    return 2\n```"
STUB = "```write_file:gate_mod.py\ndef value():\n    return 1\n```"
GOAL = "Implement gate_mod.py so that the tests in test_gate_mod.py pass."


def make_runner(tmp: Path, caller) -> tuple[LiveSwarmRunner, Path]:
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "test_gate_mod.py").write_text(TEST_BODY, encoding="utf-8")
    runner = LiveSwarmRunner(
        workspace_root=ws, artifacts_dir=tmp / "artifacts" / "missions",
        spindown_delay_sec=0.1, step_delay_sec=0.01, llm_caller=caller, auto_spindown=False,
    )
    return runner, ws


def reply(text: str, **extra) -> dict[str, object]:
    return {"ok": True, "text": text, "model": "m", "provider": "p", **extra}


class GoalNamedFileTests(unittest.TestCase):
    def test_full_budget_returns_whole_files_and_default_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner, ws = make_runner(Path(tmp), None)
            try:
                (ws / "spec.md").write_text("\n".join(f"rule {n}" for n in range(1, 201)), encoding="utf-8")
                goal = "Follow spec.md and pass test_gate_mod.py"
                self.assertNotIn("rule 200", runner._read_targeted_file_excerpts(goal))
                full = runner._read_targeted_file_excerpts(goal, full_budget=lsr.GOAL_NAMED_FILE_CHARS)
                self.assertIn("200: rule 200", full)
                self.assertIn("self.assertEqual(gate_mod.value(), 2)", full)
                self.assertNotIn("TRUNCATED", full)
                cut = runner._read_targeted_file_excerpts(goal, full_budget=600)
                self.assertIn("TRUNCATED", cut)
                self.assertIn("self.assertEqual(gate_mod.value(), 2)", cut)  # small file kept whole
            finally:
                runner.shutdown()


class GoalNamedTestRunnerTests(unittest.TestCase):
    def test_pass_fail_and_nothing_executed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "test_gate_mod.py").write_text(TEST_BODY, encoding="utf-8")
            (ws / "gate_mod.py").write_text("def value():\n    return 2\n", encoding="utf-8")
            self.assertEqual(lsr._run_goal_named_tests(ws, GOAL), (1, []))
            (ws / "gate_mod.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            ran, errs = lsr._run_goal_named_tests(ws, GOAL)
            self.assertEqual(ran, 1)
            self.assertIn("FAILED", errs[0])
            self.assertIn("AssertionError", errs[0])
            (ws / "test_gate_mod.py").write_text(
                "import unittest\n\n@unittest.skip('not built')\nclass T(unittest.TestCase):\n"
                "    def test_value(self):\n        pass\n", encoding="utf-8")
            self.assertIn("no test executed", lsr._run_goal_named_tests(ws, GOAL)[1][0])
            self.assertEqual(lsr._run_goal_named_tests(ws, "No tests named. See ../test_outside.py"), (0, []))


class MissionGateTests(unittest.TestCase):
    def run_mission(self, caller) -> tuple[dict, Path, str]:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        runner, ws = make_runner(Path(self._tmp.name), caller)
        self.addCleanup(runner.shutdown)
        res = runner.launch_mission(GOAL, allow_workspace_writes=True)
        self.assertTrue(res["ok"])
        out = runner.wait_for_mission(res["mission_id"], timeout=60.0)
        self.assertIsNotNone(out)
        report = (Path(self._tmp.name) / "artifacts" / "missions" / f"{res['mission_id']}.md").read_text(encoding="utf-8")
        return out, ws, report

    def test_stub_that_fails_named_tests_is_not_pass_despite_critic(self) -> None:
        prompts: list[str] = []

        def caller(role, model_id, system_prompt, user_prompt):
            if role == AgentRole.EXECUTION_SPECIALIST.value:
                prompts.append(user_prompt)
                return reply(STUB)
            if role == AgentRole.CRITIC_VERIFIER.value:
                return reply('{"passed": true, "reason": "Looks complete.", "issues": []}')
            return reply(f"Notes from {role}.")

        out, _ws, report = self.run_mission(caller)
        self.assertNotEqual(out.get("state"), "PASS")
        self.assertIn("Goal-named tests test_gate_mod.py FAILED", report)
        self.assertIn("self.assertEqual(gate_mod.value(), 2)", prompts[0])  # executor saw the test file
        self.assertTrue(any("Goal-named tests test_gate_mod.py FAILED" in p for p in prompts[1:]))  # and the failure

    def test_repair_that_passes_named_tests_is_pass(self) -> None:
        calls = 0

        def caller(role, model_id, system_prompt, user_prompt):
            nonlocal calls
            if role == AgentRole.EXECUTION_SPECIALIST.value:
                calls += 1
                return reply(STUB if calls == 1 else GOOD)
            if role == AgentRole.CRITIC_VERIFIER.value:
                return reply('{"passed": true, "reason": "Tests pass.", "issues": []}')
            return reply(f"Notes from {role}.")

        out, ws, _report = self.run_mission(caller)
        self.assertEqual(calls, 2)
        self.assertIn("return 2", (ws / "gate_mod.py").read_text(encoding="utf-8"))
        self.assertEqual(out.get("state"), "PASS")

    def test_fallback_pass_without_named_tests_is_not_accepted(self) -> None:
        def caller(role, model_id, system_prompt, user_prompt):
            if role == AgentRole.EXECUTION_SPECIALIST.value:
                return reply("```write_file:notes_mod.py\nX = 1\n```")
            if role == AgentRole.CRITIC_VERIFIER.value:
                return reply('{"passed": true, "reason": "Fine.", "issues": []}', model="gemma3:12b", provider="ollama",
                             used_fallback=True, requested_model="claude-sonnet-4-5",
                             primary_error="provider unavailable", fallback_errors=["provider unavailable"])
            return reply(f"Notes from {role}.")

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        runner, _ws = make_runner(Path(self._tmp.name), caller)
        self.addCleanup(runner.shutdown)
        res = runner.launch_mission("Create notes_mod.py with X = 1.", allow_workspace_writes=True)
        out = runner.wait_for_mission(res["mission_id"], timeout=60.0)
        report = (Path(self._tmp.name) / "artifacts" / "missions" / f"{res['mission_id']}.md").read_text(encoding="utf-8")
        self.assertNotEqual(out.get("state"), "PASS")
        self.assertIn("Model fallback: Critic Verifier wanted claude-sonnet-4-5", report)


if __name__ == "__main__":
    unittest.main()
