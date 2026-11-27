"""Verify desktop routing, fallback graph, and honest browser evidence."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sabrina_console_desktop.desktop_browser_action import parse_browser_request
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge


LINKEDIN_GOAL = "could yo my linkedin profile opened in internet explorer and say 'hi'"


class TestDesktopBrowserAction(unittest.TestCase):
    def test_original_prompt_uses_local_browser_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bridge = SabrinaDesktopBridge(role_bindings_path=root / "roles.json", vault_path=root / "vault.json",
                                          swarm_artifacts_dir=root / "reports", step_delay_sec=0)
            bridge.swarm_runner.auto_spindown = False
            self.addCleanup(bridge.swarm_runner.shutdown)
            with patch.dict(os.environ, {"SABRINA_LINKEDIN_ACCOUNT_URL": "https://www.linkedin.com/in/test-profile/"}), \
                    patch("sabrina_console_desktop.live_swarm_runner.dispatch_browser_open",
                          side_effect=[OSError("IE desktop is unavailable"), "Edge accepted the address."]) as dispatch, \
                    patch.object(bridge, "_try_local_desktop_action", return_value=None) as generic_action:
                started = bridge.launch_mission(LINKEDIN_GOAL)
                self.assertTrue(started["ok"])
                output = bridge.swarm_runner.wait_for_mission(started["mission_id"])
            self.assertEqual([call.args for call in dispatch.call_args_list], [
                ("Internet Explorer", "https://www.linkedin.com/in/test-profile/"),
                ("Microsoft Edge", "https://www.linkedin.com/in/test-profile/"),
            ])
            generic_action.assert_not_called()
            self.assertEqual(output["state"], "DONE")
            self.assertEqual(output["execution_mode"], "LOCAL_DESKTOP")
            self.assertFalse(output["execution_authorized"])
            self.assertTrue(output["browser_dispatch_accepted"])
            self.assertFalse(output["browser_page_verified"])
            self.assertFalse(output["verdict"]["passed"])
            self.assertEqual(output["console_greeting"], "hi")
            first, second = output["browser_attempts"]
            branch, = output["recovery_attempts"]
            self.assertEqual((branch["failed_worker_id"], branch["worker_id"]), (first["worker_id"], second["worker_id"]))
            workers = {item["id"]: item for item in bridge.swarm_runner.get_state()["workers"]}
            self.assertEqual(workers[second["worker_id"]]["parent_id"], first["worker_id"])

    def test_second_fallback_has_new_parent_and_stops_when_routes_are_exhausted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bridge = SabrinaDesktopBridge(role_bindings_path=root / "roles.json", vault_path=root / "vault.json",
                                          swarm_artifacts_dir=root / "reports", step_delay_sec=0)
            self.addCleanup(bridge.swarm_runner.shutdown)
            with patch("sabrina_console_desktop.live_swarm_runner.dispatch_browser_open",
                       side_effect=OSError("Browser unavailable")) as dispatch:
                started = bridge.launch_mission("Open https://example.test in Internet Explorer and post 'hi'")
                output = bridge.swarm_runner.wait_for_mission(started["mission_id"])
            self.assertEqual([call.args[0] for call in dispatch.call_args_list],
                             ["Internet Explorer", "Microsoft Edge", "Default Browser"])
            self.assertEqual(output["state"], "FAILED")
            self.assertFalse(output["browser_dispatch_accepted"])
            self.assertEqual(output["console_greeting"], "")
            first, second = output["recovery_attempts"]
            self.assertEqual(first["worker_id"], second["failed_worker_id"])

    def test_only_explicit_open_requests_are_routed(self):
        self.assertIsNone(parse_browser_request("Explain how to open https://example.test"))
        self.assertIsNone(parse_browser_request("Draft a post that links to https://example.test"))
        browser = parse_browser_request("Open my LinkedIn profile in Microsoft Edge and message 'hi'")
        self.assertTrue(browser.follow_up)
        self.assertEqual(browser.greeting, "")
        with self.assertRaises(ValueError):
            parse_browser_request(LINKEDIN_GOAL, "https://linkedin.com.example.test/in/test/")
        with self.assertRaises(ValueError):
            parse_browser_request("Open https://token@example.test")


if __name__ == "__main__":
    unittest.main()
