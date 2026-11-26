"""Exercise operator contract revisions through the normal mission runner."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.mission_contract import parse_contract, parse_response


def contract(field: str = "answer", constraint: str = "Use the blue value.", kind: str = "string") -> str:
    return json.dumps({
        "objective": "Report the selected value from fixture.txt.",
        "constraints": [constraint],
        "response_schema": {
            "type": "object", "properties": {field: {"type": kind}},
            "required": [field], "additionalProperties": False,
        },
    })


class TestMissionContract(unittest.TestCase):
    def make_runner(self, primary, recovery=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        workspace = directory / "workspace"
        workspace.mkdir()
        (workspace / "fixture.txt").write_text("blue = cobalt\ngreen = emerald\n", encoding="utf-8")
        runner = LiveSwarmRunner(workspace_root=workspace, artifacts_dir=directory / "reports",
                                 step_delay_sec=0, auto_spindown=False, llm_caller=primary,
                                 recovery_llm_caller=recovery)
        self.addCleanup(runner.shutdown)
        return runner

    def test_schema_update_discards_inflight_executor_before_critic(self):
        entered, resume = threading.Event(), threading.Event()
        critic_inputs = []

        def primary(role, model, system, prompt):
            if role == "execution_specialist":
                entered.set()
                resume.wait(8)
                return {"ok": True, "text": '{"answer":"cobalt"}'}
            if role == "critic_verifier":
                critic_inputs.append((system, prompt))
                return {"ok": True, "text": "PASS: Matches fixture."}
            return {"ok": True, "text": "The fixture gives the source values."}

        def recovery(policy, system, prompt):
            if 'revision 2:' in system and '"result"' in system and "cobalt" in prompt:
                return {"ok": True, "text": '```json\n{"result":"cobalt"}\n```'}
            return {"ok": False, "error": "Recovery did not receive the updated schema and evidence."}

        runner = self.make_runner(primary, recovery)
        started = runner.launch_mission("", live_contract_json=contract())
        self.assertTrue(started["ok"])
        mid = started["mission_id"]
        try:
            self.assertTrue(entered.wait(6))
            self.assertFalse(runner.revise_mission_contract(mid, 2, contract("result"))["ok"])
            self.assertFalse(runner.revise_mission_contract(mid, 1, contract().replace("selected", "other"))["ok"])
            revised = runner.revise_mission_contract(mid, 1, contract("result"))
            self.assertTrue(revised["ok"])
            self.assertEqual(revised["mission_contract"]["revision"], 2)
            self.assertFalse(runner.revise_mission_contract(mid, 1, contract())["ok"])
        finally:
            resume.set()

        output = runner.wait_for_mission(mid)
        self.assertEqual(output["state"], "PASS", output.get("error"))
        self.assertEqual(output["contract_result"], {"result": "cobalt"})
        self.assertEqual(output["contract_discards"], [{"revision": 1, "replacement": 2, "stage": "executor"}])
        self.assertEqual(len(critic_inputs), 1)
        self.assertIn('Deliverable:\n{"result": "cobalt"}', critic_inputs[0][1])
        self.assertNotIn('{"answer":"cobalt"}', output["report_markdown"])
        branch, = output["recovery_attempts"]
        self.assertEqual((branch["status"], branch["contract_revision"]), ("DONE", 2))
        worker = next(w for w in runner.get_state()["workers"] if w["id"] == branch["worker_id"])
        self.assertEqual(worker["parent_id"], branch["failed_worker_id"])
        self.assertFalse(output["execution_authorized"])
        self.assertFalse(runner.revise_mission_contract(mid, 2, contract())["ok"])

    def test_constraint_update_discards_verdict_inflight(self):
        entered, resume = threading.Event(), threading.Event()
        reviews = []

        def primary(role, model, system, prompt):
            if role == "execution_specialist":
                return {"ok": True, "text": '{"answer":"cobalt"}'}
            if role == "critic_verifier":
                reviews.append((system, prompt))
                if "revision 1:" in system:
                    entered.set()
                    resume.wait(8)
                    return {"ok": True, "text": "PASS: Old answer: cobalt."}
                if 'Use the green value.' in system and 'Deliverable:\n{"answer": "emerald"}' in prompt:
                    return {"ok": True, "text": "PASS: New answer matches green."}
                return {"ok": True, "text": "REVIEW: The result doesn't match the revised evidence."}
            return {"ok": True, "text": "Blue and green values are in the fixture."}

        def recovery(policy, system, prompt):
            if "Use the green value." in system and "emerald" in prompt:
                return {"ok": True, "text": '{"answer":"emerald"}'}
            return {"ok": False, "error": "Current contract or evidence missing."}

        runner = self.make_runner(primary, recovery)
        mid = runner.launch_mission("", live_contract_json=contract())["mission_id"]
        try:
            self.assertTrue(entered.wait(6))
            self.assertTrue(runner.revise_mission_contract(mid, 1, contract(constraint="Use the green value."))["ok"])
        finally:
            resume.set()
        output = runner.wait_for_mission(mid)
        self.assertEqual(output["state"], "PASS", output.get("error"))
        self.assertEqual(output["contract_result"], {"answer": "emerald"})
        self.assertEqual(output["contract_discards"], [{"revision": 1, "replacement": 2, "stage": "verifier"}])
        self.assertEqual(len(reviews), 2)
        self.assertNotIn("Old answer", output["report_markdown"])
        self.assertIn("New answer matches green", output["report_markdown"])

    def test_invalid_recovery_is_not_published_or_executed(self):
        def primary(role, model, system, prompt):
            if role == "execution_specialist":
                return {"ok": True, "text": "```tool:run_tests\n.\n```"}
            return {"ok": True, "text": "Workspace evidence."}

        def recovery(policy, system, prompt):
            return {"ok": True, "text": '{"answer":true}'}

        runner = self.make_runner(primary, recovery)
        with patch.object(runner, "_execute_ide_tool_blocks") as ide, patch.object(runner, "_apply_workspace_writes") as write:
            mid = runner.launch_mission("", live_contract_json=contract(kind="integer"))["mission_id"]
            output = runner.wait_for_mission(mid)
        ide.assert_not_called()
        write.assert_not_called()
        self.assertEqual(output["state"], "FAILED")
        self.assertFalse(output["verdict"]["schema_valid"])
        self.assertIsNone(output["contract_result"])
        branch, = output["recovery_attempts"]
        self.assertEqual(branch["status"], "FAILED")
        self.assertIn("must be integer", branch["error"])

    def test_cancellation_and_revision_limit(self):
        entered, resume = threading.Event(), threading.Event()

        def primary(role, model, system, prompt):
            if role == "execution_specialist":
                entered.set()
                resume.wait(8)
            return {"ok": True, "text": '{"answer":"cobalt"}'}

        runner = self.make_runner(primary)
        self.assertFalse(runner.launch_mission("", live_contract_json=contract(), physical_input=True)["ok"])
        self.assertFalse(runner.launch_mission("", live_contract_json=contract(), allow_workspace_writes=True)["ok"])
        mid = runner.launch_mission("", live_contract_json=contract())["mission_id"]
        try:
            self.assertTrue(entered.wait(6))
            for revision in range(1, 4):
                self.assertTrue(runner.revise_mission_contract(mid, revision, contract(f"answer_{revision}"))["ok"])
            self.assertFalse(runner.revise_mission_contract(mid, 4, contract())["ok"])
            runner.stop_mission(mid)
            self.assertFalse(runner.revise_mission_contract(mid, 4, contract())["ok"])
        finally:
            resume.set()
        output = runner.wait_for_mission(mid)
        self.assertEqual(output["state"], "STOPPED")
        self.assertFalse(output["artifact_path"])

    def test_schema_parser_rejects_unsafe_or_ambiguous_inputs(self):
        spec = parse_contract(contract(kind="integer"))
        schema = spec["response_schema"]
        self.assertEqual(parse_response(schema, '{"answer":12}'), {"answer": 12})
        for payload in ('{"answer":12,"answer":13}', '{"answer":true}', '{"answer":1e1000}',
                        '{"answer":4,"extra":2}', '{}', 'prefix {"answer":5}'):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_response(schema, payload)
        schema["properties"]["answer"]["$ref"] = "https://example.invalid/schema"
        with self.assertRaises(ValueError):
            parse_contract(json.dumps(spec))


if __name__ == "__main__":
    unittest.main()
