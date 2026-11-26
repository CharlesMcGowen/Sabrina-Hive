"""Regression tests for pausing and resuming a visible browser mission."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from sabrina_console_desktop.context_first_coordinator import build_preflight_manifest
from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner


class _VisibleBrowser:
    is_visible = True
    is_open = False
    backend = "visible-edge"
    origin = "https://example.com"
    start_url = "https://example.com/"
    manual_start = False
    initial_handoff_done = False

    def __init__(self):
        self.events = []
        self.closed = False

    def manifest(self):
        return {"backend": self.backend, "start_url": self.start_url, "origin": self.origin}

    def run(self, _request, _cancel):
        self.is_open = True
        event = {
            "backend": self.backend, "action": "open", "dispatched": True,
            "ok": False, "http_status": 403, "needs_human": True,
            "snapshot": 'heading "Security verification"', "elapsed_ms": 1,
        }
        self.events.append(event)
        return event

    def observe_after_user(self, _cancel):
        event = {
            "backend": self.backend, "action": "snapshot", "dispatched": True,
            "ok": True, "http_status": 200, "snapshot": 'heading "Completed"', "elapsed_ms": 1,
        }
        self.events.append(event)
        return event

    def close(self):
        self.closed = True

    def telemetry(self):
        return {"backend": self.backend, "actions": len(self.events)}


class TestVisibleBrowserHandoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.session = _VisibleBrowser()
        self.roles = []

        def llm(role, model_id, _system, prompt):
            self.roles.append(role)
            if role == "coordinator":
                self.assertIn('"visible-edge"', prompt)
                text = json.dumps({
                    "playbook": "direct_single_agent",
                    "role_bindings": {
                        "execution_specialist": "ollama/gemma3:4b",
                        "critic_verifier": "ollama/gemma3:4b",
                    },
                    "stop_condition": "The page shows Completed.",
                })
            elif role == "execution_specialist":
                text = (
                    '{"action":"finish","summary":"Completed"}'
                    if "heading \"Completed\"" in prompt
                    else '{"action":"open"}'
                )
            else:
                text = '{"passed":true,"reason":"The page shows Completed."}'
            return {"ok": True, "model": model_id, "provider": "ollama", "text": text}

        self.runner = LiveSwarmRunner(
            workspace_root=root, artifacts_dir=root / "reports", step_delay_sec=0,
            auto_spindown=False, llm_caller=llm,
        )
        manifest = build_preflight_manifest(
            unlocked_providers=["ollama"], ollama_models=["gemma3:4b"], ollama_ready=True,
        )
        launch = self.runner.launch_context_first_mission(
            "Open the page and verify completion.", manifest=manifest, browser_session=self.session,
        )
        self.mission_id = launch["mission_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            missions = self.runner.get_state()["active_missions"]
            if any(m.get("browser_handoff") == "waiting" for m in missions):
                break
            time.sleep(0.05)
        else:
            self.fail("The browser mission did not pause for the user.")

    def tearDown(self):
        self.runner.shutdown()
        self.tmp.cleanup()

    def test_resume_observes_the_result_before_verification(self):
        self.assertFalse(self.runner.continue_browser_mission("another-mission")["ok"])
        self.assertTrue(self.runner.continue_browser_mission(self.mission_id)["ok"])
        result = self.runner.wait_for_mission(self.mission_id, timeout=8)
        self.assertEqual(result["state"], "PASS")
        self.assertEqual([item["action"] for item in result["browser_evidence"]], ["open", "snapshot"])
        self.assertEqual(result["browser_evidence"][-1]["snapshot"], 'heading "Completed"')
        self.assertTrue(self.session.closed)

    def test_cancellation_does_not_call_the_model_again(self):
        self.runner.stop_mission(self.mission_id)
        self.runner.wait_for_mission(self.mission_id, timeout=8)
        self.assertEqual(self.roles, ["coordinator", "execution_specialist"])
        self.assertTrue(self.session.closed)
