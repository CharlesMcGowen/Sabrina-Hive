"""Safety and action behavior for the selected-window desktop integration."""
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sabrina_console_desktop.agent_desktop_service import DesktopSession, _snapshot_pages, _snapshot_text
from sabrina_console_desktop.tools.swarm_search_hydrator import SwarmSearchHydrator
from sabrina_console_desktop.windows_powershell import register_windows_tools
from sabrina_console_desktop.windows_skill_runtime import powershell_executable
from swarm_command.runtime import ToolRegistry


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

    def test_windows11_semantic_actions_find_get_is_wait_and_headed_guards(self):
        # Multi-page find locates controls and reports their exact page index
        self.session._snapshot_pages_cache = [
            'window "Settings"\n  button "System" [ref=d1e1]',
            '  switch "Bluetooth" [ref=d1e2]\n  treeitem "Advanced" [ref=d1e3]',
        ]
        found = self.session.run({"action": "find", "query": "bluetooth", "role": "switch"}, self.cancel)
        self.assertTrue(found["ok"])
        self.assertEqual(found["total_matches"], 1)
        self.assertEqual(found["matches"][0]["page"], 1)
        self.assertIn("[ref=d1e2]", found["matches"][0]["line"])

        # Semantic WinUI 3 actions (toggle, expand, clear) work without coordinate clicks
        self.session._call = Mock(return_value={"ok": True, "data": {"checked": True}})
        for act, cli_verb in (("toggle", "toggle"), ("expand", "expand"), ("clear", "clear")):
            res = self.session.run({"action": act, "ref": "d1e1"}, self.cancel)
            self.assertTrue(res["ok"], res)
            self.session._call.assert_any_call(cli_verb, "@sABC:e1", text_input=None)

        # Protected password ref blocks 'get' value while allowing safe refs
        self.session._refs["d1e9"] = "@sABC:e9"
        self.session._ref_data["d1e9"] = {"bounds": None, "states": ["protected"], "protected": True}
        blocked_get = self.session.run({"action": "get", "ref": "d1e9", "property": "value"}, self.cancel)
        self.assertFalse(blocked_get["ok"])
        self.assertIn("protected", blocked_get["error"])

        ok_is = self.session.run({"action": "is", "ref": "d1e1", "state": "enabled"}, self.cancel)
        self.assertTrue(ok_is["ok"])

        # Wait predicate dispatches bounded wait scoped to selected app
        waited = self.session.run({"action": "wait", "text": "Saved", "timeout_ms": 1500}, self.cancel)
        self.assertTrue(waited["ok"])
        self.session._call.assert_any_call("wait", "--text", "Saved", "--app", "example.exe", "--timeout-ms", "1500")

        # double_click and right_click require headed=True
        headless = DesktopSession("w-42", headed=False)
        headless.is_open = True
        headless.window = {"app": "explorer.exe", "title": "File Explorer", "process_instance": "p2"}
        headless._refs = {"d1e1": "@sABC:e1"}
        headless._ref_data = {"d1e1": {"bounds": None, "states": []}}
        headless._latest_snapshot = 'listitem "Documents" [ref=d1e1]'
        denied = headless.run({"action": "double_click", "ref": "d1e1"}, self.cancel)
        self.assertFalse(denied["ok"])
        self.assertIn("headed", denied["error"])

    @unittest.skipUnless(sys.platform == "win32", "Requires Windows PowerShell")
    def test_windows11_powershell_info_list_and_hash_respect_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hello.txt").write_bytes(b"windows11-sabrina")
            (root / ".env").write_text("SECRET=1", encoding="utf-8")
            (root / "vault.sqlite3").write_bytes(b"sqlite")
            reg = ToolRegistry()
            register_windows_tools(reg, str(root), str(powershell_executable()))

            info = reg.get("powershell_info").handler({})
            self.assertIn("os_build", info)
            self.assertIn("is_windows_11", info)
            self.assertIn("Get-FileHash", info["cmdlets"])

            listed = reg.get("powershell_list").handler({"path": "."})
            names = [entry["name"] for entry in listed["entries"]]
            self.assertIn("hello.txt", names)
            self.assertNotIn(".env", names)
            self.assertNotIn("vault.sqlite3", names)

            hashed = reg.get("powershell_hash").handler({"path": "hello.txt"})
            self.assertEqual(hashed["sha256"], hashlib.sha256(b"windows11-sabrina").hexdigest())

            with self.assertRaises(ValueError):
                reg.get("powershell_hash").handler({"path": ".env"})

    def test_local_search_hydrator_indexes_vendor_agent_desktop_and_windows11_docs(self):
        repo_root = Path(__file__).resolve().parents[1]
        vendor_dir = repo_root / "sabrina_console_desktop" / "docs" / "vendor" / "agent-desktop"
        self.assertTrue((vendor_dir / "manifest.json").is_file(), "Synced agent-desktop manifest.json must exist locally")
        self.assertTrue((vendor_dir / "windows.md").is_file(), "Synced agent-desktop windows.md must exist locally")
        self.assertTrue((vendor_dir / "windows-shell-and-overlay.md").is_file())
        self.assertTrue((vendor_dir / "mcp-tools.json").is_file())

        hydrator = SwarmSearchHydrator(workspace_root=str(repo_root))
        res = hydrator.search_and_hydrate(
            "windows 11 vercel agent-desktop snapshot toggle powershell uia",
            allow_web=False,
        )
        urls = [r["url"] for r in res["results"]]
        titles = [r["title"] for r in res["results"]]
        self.assertTrue(any("docs/vendor/agent-desktop/" in u for u in urls), urls)
        self.assertTrue(any("windows11-agent-desktop/SKILL.md" in u for u in urls), urls)
        self.assertTrue(any("Vercel agent-desktop v1.3.1" in t for t in titles), titles)


if __name__ == "__main__":
    unittest.main()
