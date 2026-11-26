"""Check real mission routing and the local Home Depot handoff."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from sabrina_console_desktop import home_depot_order
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge


GOAL = "can you create a home depot order to build 3 dog houses?"


class HomeDepotOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.model = Mock(side_effect=AssertionError("A materials draft must not call the model"))
        with patch("sabrina_console_desktop.sabrina_desktop_ui.ROOT", self.root):
            self.bridge = SabrinaDesktopBridge(
                role_bindings_path=self.root / "roles.json",
                vault_path=self.root / "vault.json",
                swarm_artifacts_dir=self.root / "reports",
                llm_caller=self.model,
                step_delay_sec=0,
            )
        self.runner = self.bridge.swarm_runner
        self.runner.auto_spindown = False
        self.runner.set_workspace_root(self.root)
        self.addCleanup(self.runner.shutdown)

    def test_mission_creates_verified_review_artifact(self) -> None:
        with patch("webbrowser.open") as browser:
            started = self.bridge.launch_mission(GOAL)
            self.assertTrue(started["ok"], started)
            self.assertEqual(started["mode"], home_depot_order.MODE)
            self.assertEqual(started["ratio"], "1:2")
            output = self.runner.wait_for_mission(started["mission_id"], timeout=3)
            browser.assert_not_called()

            self.assertEqual(output["state"], "PASS", output)
            self.assertTrue(output["verdict"]["passed"])
            self.assertFalse(output["execution_authorized"])
            self.assertEqual(output["live_llm_calls"], 0)
            self.model.assert_not_called()
            draft = output["order_draft"]
            self.assertEqual(draft["status"], "ready_for_review")
            self.assertEqual(draft["house_count"], 3)
            self.assertFalse(draft["merchant_cart_created"])
            self.assertFalse(draft["order_submitted"])
            self.assertFalse(draft["local_stock_confirmed"])
            self.assertFalse(draft["local_prices_confirmed"])
            purchase_map = {row["internet_number"]: row["quantity"] for row in draft["lines"]}
            self.assertEqual(purchase_map, {
                "100061386": 3, "206935184": 6, "302778103": 33, "203429564": 3, "338307861": 15,
                "100087758": 3, "100067957": 6, "100050433": 2, "100660853": 3, "100095071": 3,
                "202038072": 3, "305418743": 6, "305418725": 5, "342161357": 1, "202308602": 3,
                "206667343": 3, "310948305": 3,
                "100085823": 1,
            })
            self.assertEqual(len(draft["lines"]), len(purchase_map))
            artifact = Path(output["artifact_path"])
            self.assertEqual(artifact.parent, self.root / "reports")
            html = artifact.read_text(encoding="utf-8")
            for url in (home_depot_order.PLAN_URL, home_depot_order.LISTS_URL, home_depot_order.CART_URL,
                        home_depot_order.ROOF_GUIDE_URL, home_depot_order.STORE_LOCATOR_URL):
                self.assertIn(url, html)
            self.assertIn("Copy Pro Desk request", html)
            for row in draft["lines"]:
                self.assertIn(f'href="{row["url"]}"', html)
            self.assertEqual({worker["status"] for worker in self.runner.get_state()["workers"]}, {"DONE"})

            self.assertFalse(self.bridge.open_home_depot_order_draft("../other")["ok"])
            browser.assert_not_called()
            self.assertTrue(self.bridge.open_home_depot_order_draft(started["mission_id"])["ok"])
            browser.assert_called_once_with(artifact.as_uri(), new=2)

    def test_plain_operator_chat_dispatches_the_draft(self) -> None:
        response = self.bridge.send_chat_async(
            "Sabrina, please prepare a Home Depot quote request for two doghouses.", "hive_council", ""
        )
        self.assertTrue(response["ok"], response)
        deadline = time.monotonic() + 3
        job = self.bridge.get_chat_job_status()
        while job["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
            job = self.bridge.get_chat_job_status()
        self.assertTrue(job["ok"], job)
        self.assertFalse(job["running"], job)
        output = self.runner.wait_for_mission(job["mission_id"], timeout=3)
        self.assertEqual(output["order_draft"]["house_count"], 2)
        self.model.assert_not_called()

    def test_unsupported_dimensions_or_count_do_not_publish_a_draft(self) -> None:
        for goal in ("Create a Home Depot order to build 0 dog houses.",
                     "Create a Home Depot order for 3 dog houses under $100.",
                     "Create a Home Depot order to build 3 dog houses, all insulated."):
            result = self.bridge.launch_mission(goal)
            self.assertFalse(result["ok"], result)
        self.assertFalse(self.runner.get_state()["mission_outputs"])
        self.assertFalse(home_depot_order.matches_goal("Explain how to create a Home Depot order for 3 dog houses."))
        self.assertFalse(home_depot_order.matches_goal("Design a dog house plan using wood from Home Depot."))

    def test_quantity_validation_rejects_short_and_duplicate_lines(self) -> None:
        lines = home_depot_order.build_lines(10)
        home_depot_order.verify_lines(10, lines)
        for broken in (
            (replace(lines[0], quantity=9), *lines[1:]),
            (*lines, lines[0]),
            lines[:-1],
        ):
            with self.assertRaises(ValueError):
                home_depot_order.verify_lines(10, broken)
        self.assertEqual(next(line.quantity for line in lines if line.product.internet_number == "100085823"), 2)

    def test_artifact_write_failure_never_reports_draft_ready(self) -> None:
        with patch("sabrina_console_desktop.live_swarm_runner._atomic_write_text", side_effect=OSError("disk full")):
            started = self.bridge.launch_mission(GOAL)
            result = self.runner.wait_for_mission(started["mission_id"], timeout=3)
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("disk full", result["summary"])
        self.assertNotIn("order_draft", result)
        self.assertFalse(self.bridge.open_home_depot_order_draft(started["mission_id"])["ok"])


if __name__ == "__main__":
    unittest.main()
