import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import ZipFile

from sabrina_console_desktop import release_review as release
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge


class TestReleaseReview(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "dist").mkdir()
        archive = self.root / "dist" / "sabrina-console-release-kit.zip"
        with ZipFile(archive, "w") as bundle:
            for name in release.INPUTS:
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((release.KIT / name).read_bytes())
                bundle.write(path, name)
        kit_patch = patch.object(release, "KIT", self.root)
        kit_patch.start()
        self.addCleanup(kit_patch.stop)
        self.invoke = Mock()
        self.review = release.ReleaseReview("gemini-test", self.invoke, lambda: "test_key_placeholder")

    def plan(self):
        return self.review.call("planner_architect", "gemini-test", "stage instructions", "stage context")

    def test_failed_cloud_attempt_is_followed_immediately_by_local_inference(self):
        for error in ("HTTP 404", "HTTP 429", TimeoutError("Timed out")):
            with self.subTest(error=error):
                self.invoke.reset_mock()
                failure = error if isinstance(error, Exception) else {"ok": False, "error": error}
                self.invoke.side_effect = [failure, {"ok": True, "text": "Plan", "model": release.LOCAL_MODEL}]
                result = self.plan()
                calls = self.invoke.call_args_list
                self.assertEqual([call.args[:2] for call in calls], [("gemini", "gemini-test"), ("ollama", release.LOCAL_MODEL)])
                self.assertNotIn("api_key", calls[1].kwargs)
                self.assertEqual(result["requested_model"], "gemini-test")
                self.assertEqual(result["model"], release.LOCAL_MODEL)
                self.assertTrue(result["used_fallback"])

    def test_working_cloud_and_failed_local_are_reported_honestly(self):
        self.invoke.return_value = {"ok": True, "text": "Cloud plan", "model": "gemini-test"}
        self.assertEqual(self.plan()["model"], "gemini-test")
        self.invoke.assert_called_once()
        self.invoke.side_effect = [{"ok": True, "text": ""}, {"ok": False, "error": "local offline"}]
        failure = self.plan()
        self.assertFalse(failure["ok"])
        self.assertIn("empty plan", failure["all_errors"][0])
        self.assertIn("local offline", failure["all_errors"][1])

    def test_changed_archive_input_prevents_start(self):
        (self.root / "REPOSITORY.md").write_text("Changed after build", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Rebuild"):
            release.ReleaseReview("gemini-test", self.invoke, lambda: None)
        self.invoke.assert_not_called()

    def test_release_launch_uses_only_the_report_runner(self):
        bridge = Mock(spec=SabrinaDesktopBridge)
        bridge.launch_release_review.side_effect = lambda: {"ok": True, "execution_authorized": False}
        with patch("sabrina_console_desktop.sabrina_desktop_ui.launch_swarm_mission") as legacy:
            result = SabrinaDesktopBridge.launch_mission(bridge, release.GOAL)
            self.assertTrue(result["ok"])
            bridge.launch_release_review.assert_called_once()
            bridge._try_local_desktop_action.assert_not_called()
            legacy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
