"""Safety and action behavior for the selected-window desktop integration."""
import threading
import unittest
from unittest.mock import Mock, patch

from sabrina_console_desktop.agent_desktop_service import DesktopSession, _snapshot_pages, _snapshot_text


class DesktopServiceTests(unittest.TestCase):
    def setUp(self):
        self.cancel = threading.Event()
        self.session = DesktopSession("w-42", headed=True)
        self.session.is_open = True
        self.session.window = {"app": "example.exe", "title": "Example", "process_instance": "p1"}
        self.session._refs = {"d1e1": "@sABC:e1"}
        self.session._ref_data = {"d1e1": {"bounds": {"x": 20, "y": 30, "width": 20, "height": 10}, "states": []}}
        self.session._latest_snapshot = 'button "Add" [ref=d1e1]'
        self.session._snapshot = Mock(side_effect=lambda: {"ok": True, "snapshot": self.session._latest_snapshot})

    def test_snapshot_redacts_password_values(self):
        tree = {"role": "window", "children": [{"role": "textbox", "name": "Password", "value": "secret", "ref_id": "@sABC:e1"}]}
        text, refs, _ = _snapshot_text(tree, prefix="d1")
        self.assertNotIn("secret", text)
        self.assertIn("d1e1", refs)

    def test_long_snapshots_remain_available_without_splitting_refs(self):
        lines = [f'link "Product {n}" [ref=d1e{n}] ' + "x" * 90 for n in range(300)]
        pages = _snapshot_pages("\n".join(lines))
        self.assertGreater(len(pages), 1)
        self.assertEqual("\n".join(pages), "\n".join(lines))
        self.assertTrue(all(len(page) <= 7900 for page in pages))

    def test_stale_ref_and_unapproved_coordinate_click_are_rejected(self):
        self.session._call = Mock()
        self.assertFalse(self.session.run({"action": "click", "ref": "d0e1"}, self.cancel)["ok"])
        self.assertFalse(self.session.run({"action": "click_at", "ref": "d1e1"}, self.cancel)["ok"])
        self.session._call.assert_not_called()

    def test_ambiguous_click_is_not_repeated_and_can_use_checked_point(self):
        ambiguous = {"ok": False, "error": {"code": "AMBIGUOUS_TARGET", "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
        self.session._call = Mock(side_effect=[ambiguous, {"ok": True}, {"ok": True}])
        first = self.session.run({"action": "click", "ref": "d1e1"}, self.cancel)
        self.assertIn("AMBIGUOUS_TARGET", first["error"])
        again = self.session.run({"action": "click", "ref": "d1e1"}, self.cancel)
        self.assertFalse(again["dispatched"])
        self.assertEqual(self.session._call.call_count, 1)
        with patch("sabrina_console_desktop.agent_desktop_service.available_windows", return_value=[{"id": "w-42", "bounds": {"x": 0, "y": 0, "width": 100, "height": 100}}]), patch("sabrina_console_desktop.agent_desktop_service._point_is_in_selected_window", return_value=True):
            result = self.session.run({"action": "click_at", "ref": "d1e1"}, self.cancel)
        self.assertTrue(result["ok"], result)
        self.session._call.assert_any_call("mouse-click", "--xy=30,35", text_input=None)

    def test_coordinate_click_is_blocked_if_point_is_obscured(self):
        self.session._last_ambiguous = ('button "Add"', (20., 30., 20., 10.))
        self.session._call = Mock(return_value={"ok": True})
        with patch("sabrina_console_desktop.agent_desktop_service.available_windows", return_value=[{"id": "w-42", "bounds": {"x": 0, "y": 0, "width": 100, "height": 100}}]), patch("sabrina_console_desktop.agent_desktop_service._point_is_in_selected_window", return_value=False):
            result = self.session.run({"action": "click_at", "ref": "d1e1"}, self.cancel)
        self.assertFalse(result["ok"])
        self.assertIn("unobscured", result["error"])
        self.assertEqual(self.session._call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
