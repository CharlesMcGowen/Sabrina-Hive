"""Regression coverage for the desktop browser capability and verdict gate."""

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sabrina_console_desktop.agent_browser_service import BROWSER_TOOL, BrowserSession
from sabrina_console_desktop.agent_browser_service.protocol import browser_action, browser_reply, scoped_url, snapshot_page
from sabrina_console_desktop.agent_browser_service.worker import public_endpoints
from sabrina_console_desktop.agent_browser_service.visible import VisibleBrowserSession
from sabrina_console_desktop.context_first_coordinator import build_preflight_manifest
from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner


class TestAgentBrowserService(unittest.TestCase):
    def test_browser_mission_keeps_matched_skill_guidance_on_each_turn(self):
        class Session:
            backend = "fixture"
            origin = "https://example.com"
            start_url = "https://example.com/"
            max_actions = 3
            def __init__(self):
                self.events = []
            def manifest(self):
                return {"backend": self.backend, "origin": self.origin, "start_url": self.start_url}
            def run(self, request, _cancel):
                event = {"action": request["action"], "ok": True, "dispatched": True,
                         "snapshot": 'heading "Example Domain"', "elapsed_ms": 1}
                self.events.append(event)
                return event
            def close(self):
                pass
            def telemetry(self):
                return {"backend": self.backend, "actions": len(self.events)}

        prompts = []
        critic_calls = []
        model = "ollama/gemma3:4b"
        def llm(role, model_id, _system, prompt):
            if role == "execution_specialist":
                prompts.append(prompt)
                reply = ('{"action":"open"}' if len(prompts) == 1 else
                         '{"action":"finish","summary":"Example Domain"}')
            else:
                critic_calls.append((_system, prompt))
                reply = '{"passed":true,"reason":"The heading was observed."}'
            return {"ok": True, "model": model_id, "provider": "ollama", "text": reply}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = LiveSwarmRunner(workspace_root=root, artifacts_dir=root / "reports", step_delay_sec=0,
                                     auto_spindown=False, llm_caller=llm,
                                     skill_lookup_fn=lambda goal, limit: [{"id": "browser-tasks", "name": "browser-tasks", "excerpt": "Use observed page evidence."}])
            try:
                manifest = build_preflight_manifest(unlocked_providers=["ollama"], ollama_models=["gemma3:4b"], ollama_ready=True)
                blueprint = {"playbook": "direct_single_agent", "role_bindings": {
                    "execution_specialist": model, "critic_verifier": model}, "stop_condition": "Read the heading."}
                launch = runner.launch_context_first_mission("Use browser-tasks to read the heading", manifest=manifest,
                                                             blueprint=blueprint, browser_session=Session())
                self.assertTrue(launch["ok"], launch.get("error"))
                self.assertIsNotNone(runner.wait_for_mission(launch["mission_id"], timeout=10))
                self.assertEqual(len(prompts), 2)
                self.assertEqual(len(critic_calls), 1)
                self.assertIn("only part of the goal is not a pass", critic_calls[0][0])
                self.assertIn("Stop Condition: Read the heading.", critic_calls[0][1])
                for prompt in prompts:
                    self.assertIn("Use observed page evidence.", prompt)
                    self.assertIn("Authorized origin: https://example.com", prompt)
                    self.assertIn("existing permissions still apply", prompt)
            finally:
                runner.shutdown()

    def test_browser_actions_stay_in_scope(self):
        url, origin = scoped_url("https://Example.com:443/start")
        self.assertEqual(url, "https://example.com/start")
        self.assertEqual(browser_action({"action": "open"}, url, origin)["url"], url)
        self.assertEqual(browser_action({"action": "click", "ref": "@f2e4"}, url, origin)["ref"], "f2e4")
        self.assertEqual(browser_action({"action": "click", "ref": "link [ref=f2e4] [cursor=pointer]"}, url, origin)["ref"], "f2e4")
        violations = [
            {"action": "open", "url": "https://example.com:444/"},
            {"action": "open", "url": "https://example.com.other.test/"},
            {"action": "open", "url": "https://user@example.com/"},
            {"action": "open", "url": "https://example.com:0/"},
            {"action": "open", "url": "file:///tmp/page.html"},
            {"action": "click", "ref": "button"},
            {"action": "fill", "ref": "e2", "value": "--config=/tmp/config"},
            {"action": "eval", "value": "location.href"},
        ]
        for violation in violations:
            with self.subTest(violation=violation), self.assertRaises(ValueError):
                browser_action(violation, url, origin)

    def test_browser_reply_accepts_only_one_command(self):
        self.assertEqual(browser_reply('{"action":"open"} {"action":"click","ref":"e1"}'), {"action": "open"})
        self.assertEqual(browser_reply('```tool:browser_session\n{"action":"snapshot"}\n```'), {"action": "snapshot"})
        self.assertIsNone(browser_reply('Here is a JSON example {"action":"click","ref":"e1"}'))

    def test_browser_snapshot_pages_expose_later_items(self):
        lines = [f'link "Product {n}" [ref=e{n}] ' + "x" * 90 for n in range(100)]
        first, _ = snapshot_page("\n".join(lines), 0)
        second, actual = snapshot_page("\n".join(lines), 1)
        self.assertIn("Snapshot page 1", first)
        self.assertEqual(actual, 1)
        self.assertIn("Product 50", second)
        self.assertEqual(browser_action({"action":"snapshot", "page":1}, "https://example.com/", "https://example.com")["page"], 1)
        with self.assertRaises(ValueError):
            browser_action({"action":"snapshot", "page":True}, "https://example.com/", "https://example.com")

    def test_private_dns_answer_is_rejected(self):
        endpoints = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.15", 443)),
        ]
        with patch.object(socket, "getaddrinfo", return_value=endpoints), self.assertRaises(ValueError):
            public_endpoints("demonstration.test", 443)

    def test_visible_browser_rejects_stale_refs_and_refreshes_after_failure(self):
        from playwright.sync_api import Error
        session = VisibleBrowserSession("visible-edge", "https://example.com/")
        session._page = Mock()
        session.is_open = True
        session._latest_snapshot = '- button "Current" [ref=e2]'
        stale = session.run({"action": "click", "ref": "e1"}, threading.Event())
        self.assertFalse(stale["dispatched"])
        self.assertIn("latest snapshot", stale["error"])
        self.assertIn("Current", stale["snapshot"])
        session._page.get_by_ref.return_value.click.side_effect = Error("frame changed")
        session._observe = Mock(return_value={"ok": True, "snapshot": '- link "Fresh" [ref=e3]', "url": "https://example.com/"})
        failed = session.run({"action": "click", "ref": "e2"}, threading.Event())
        self.assertTrue(failed["dispatched"])
        self.assertFalse(failed["ok"])
        self.assertIn("Fresh", failed["snapshot"])

    def test_coordinator_cannot_pass_a_browser_mission_without_observed_evidence(self):
        model = "ollama/gemma3:4b"
        session = BrowserSession("playwright-cli", "https://example.com/")
        manifest = build_preflight_manifest(
            unlocked_providers=["ollama"], ollama_models=["gemma3:4b"], ollama_ready=True,
            extra_tools=[BROWSER_TOOL],
        )
        self.assertNotIn(BROWSER_TOOL, manifest.available_tool_shed)

        def llm(role, model_id, _system, prompt):
            if role == "coordinator":
                self.assertIn('"browser_session"', prompt)
                self.assertIn('"backend": "playwright-cli"', prompt)
                text = json.dumps({
                    "playbook": "direct_single_agent",
                    "role_bindings": {"execution_specialist": model, "critic_verifier": model},
                    "stop_condition": "The browser shows the requested page heading.",
                })
            elif role == "execution_specialist":
                self.assertIn("actions through browser_session", prompt)
                text = "I verified the heading."
            else:
                self.assertIn("Browser observations (from the runtime):\n[]", prompt)
                text = '{"passed":true,"reason":"The heading was verified."}'
            return {"ok": True, "model": model_id, "provider": "ollama", "text": text}

        with tempfile.TemporaryDirectory() as tmp:
            runner = LiveSwarmRunner(
                workspace_root=Path(tmp), artifacts_dir=Path(tmp) / "reports",
                step_delay_sec=0, auto_spindown=False, llm_caller=llm,
            )
            try:
                launched = runner.launch_context_first_mission(
                    "Navigate to https://example.com/ and verify the heading.",
                    manifest=manifest, browser_session=session,
                )
                result = runner.wait_for_mission(launched["mission_id"], timeout=10)
                self.assertEqual(result["state"], "FAILED")
                self.assertFalse(result["verdict"]["passed"])
                self.assertIn("No browser result was verified.", result["verdict"]["issues"])
                self.assertEqual(result["browser_usage"]["actions"], 0)
                self.assertIsNone(result["browser_usage"]["startup_ms"])
            finally:
                runner.shutdown()

    def test_repeated_stale_refs_cannot_be_passed_by_the_critic(self):
        class Session:
            is_visible = True
            is_open = True
            backend = "visible-edge"
            origin = "https://example.com"
            start_url = "https://example.com/"
            manual_start = False
            initial_handoff_done = False
            max_actions = 10
            def __init__(self):
                self.events = []
            def manifest(self):
                return {"backend": self.backend, "origin": self.origin, "start_url": self.start_url}
            def run(self, request, _cancel):
                if request["action"] == "open":
                    event = {"action": "open", "ok": True, "dispatched": True, "snapshot": 'button "Current" [ref=e2]', "elapsed_ms": 1}
                else:
                    event = {"action": "click", "ok": False, "dispatched": False, "snapshot": 'button "Current" [ref=e2]',
                             "error": "That ref is not in the latest snapshot. Choose a fresh ref from the current page.", "elapsed_ms": 1}
                self.events.append(event)
                return event
            def close(self):
                pass
            def telemetry(self):
                return {"backend": self.backend, "actions": len(self.events)}

        def llm(role, model_id, _system, prompt):
            if role == "coordinator":
                text = json.dumps({"playbook": "direct_single_agent", "role_bindings": {
                    "execution_specialist": model_id, "critic_verifier": model_id}, "stop_condition": "Cart completed."})
            elif role == "execution_specialist":
                text = '{"action":"click","ref":"e1"}' if "Observation after action" in prompt else '{"action":"open"}'
            else:
                text = '{"passed":true,"reason":"Claimed success."}'
            return {"ok": True, "model": model_id, "provider": "ollama", "text": text}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = LiveSwarmRunner(workspace_root=root, artifacts_dir=root / "reports", step_delay_sec=0,
                                     auto_spindown=False, llm_caller=llm)
            try:
                manifest = build_preflight_manifest(unlocked_providers=["ollama"], ollama_models=["gemma3:4b"], ollama_ready=True)
                launched = runner.launch_context_first_mission("Create a cart", manifest=manifest, browser_session=Session())
                result = runner.wait_for_mission(launched["mission_id"], timeout=10)
                self.assertEqual(result["state"], "FAILED")
                self.assertTrue(any("outdated ref" in issue for issue in result["verdict"]["issues"]))
            finally:
                runner.shutdown()
