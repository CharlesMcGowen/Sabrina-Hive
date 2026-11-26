"""Unit tests for sabrina_console_desktop.tools.service_supervisor and launch_console supervised mode."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

DESKTOP_ROOT = Path(__file__).resolve().parent.parent
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

from tools.service_supervisor import MicroserviceSupervisor, ServiceSpec  # noqa: E402
import launch_console  # noqa: E402


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.terminated = False
        self.killed = False
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode


class ServiceSupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.next_pid = 1000
        self.spawned: list[_FakeProcess] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_popen(self, cmd, cwd=None, env=None):
        self.next_pid += 1
        proc = _FakeProcess(self.next_pid)
        self.spawned.append(proc)
        return proc

    def test_file_change_restarts_only_affected_service(self) -> None:
        api_py = self.root / "api.py"
        sched_py = self.root / "scheduler.py"
        ui_html = self.root / "ui.html"
        api_py.write_text("# v1\n", encoding="utf-8")
        sched_py.write_text("# v1\n", encoding="utf-8")
        ui_html.write_text("<html>v1</html>\n", encoding="utf-8")

        sup = MicroserviceSupervisor(
            [
                ServiceSpec("api_8087", ["py", "api.py"], watch_paths=[api_py], cwd=self.root),
                ServiceSpec("scheduler", ["py", "scheduler.py"], watch_paths=[sched_py], cwd=self.root),
                ServiceSpec("desktop_ui", ["py", "ui.py"], watch_paths=[ui_html], cwd=self.root),
            ],
            poll_interval=0.05,
            popen_factory=self._fake_popen,
        )
        initial = sup.start_all()
        self.assertEqual(initial["api_8087"]["pid"], 1001)
        self.assertEqual(initial["scheduler"]["pid"], 1002)
        self.assertEqual(initial["desktop_ui"]["pid"], 1003)

        # No file changes -> no restarts
        self.assertEqual(sup.poll_once(), [])

        # Modify only api.py with a deterministic mtime bump
        old_mtime = api_py.stat().st_mtime
        api_py.write_text("# v2\n", encoding="utf-8")
        os.utime(api_py, (old_mtime + 5, old_mtime + 5))

        events = sup.poll_once()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["service"], "api_8087")
        self.assertEqual(events[0]["event"], "reloaded")
        self.assertEqual(events[0]["restarts"], 1)

        after = sup.status()
        # Only api_8087 got a new PID and restart count; siblings untouched
        self.assertEqual(after["api_8087"]["pid"], 1004)
        self.assertEqual(after["api_8087"]["restarts"], 1)
        self.assertIsNotNone(after["api_8087"]["last_reload_ts"])
        self.assertTrue(self.spawned[0].terminated)

        self.assertEqual(after["scheduler"]["pid"], 1002)
        self.assertEqual(after["scheduler"]["restarts"], 0)
        self.assertFalse(self.spawned[1].terminated)

        self.assertEqual(after["desktop_ui"]["pid"], 1003)
        self.assertEqual(after["desktop_ui"]["restarts"], 0)
        self.assertFalse(self.spawned[2].terminated)

        sup.stop_all()
        self.assertEqual(sup.service_status("scheduler")["status"], "stopped")

    def test_unexpected_exit_auto_restart_and_crash(self) -> None:
        svc_a = self.root / "a.py"
        svc_b = self.root / "b.py"
        svc_a.write_text("pass\n", encoding="utf-8")
        svc_b.write_text("pass\n", encoding="utf-8")

        sup = MicroserviceSupervisor(
            [
                ServiceSpec("svc_auto", ["py", "a.py"], watch_paths=[svc_a], auto_restart=True),
                ServiceSpec("svc_no_auto", ["py", "b.py"], watch_paths=[svc_b], auto_restart=False),
            ],
            popen_factory=self._fake_popen,
        )
        sup.start_all()
        # Simulate both processes exiting
        self.spawned[0].returncode = 1
        self.spawned[1].returncode = 2

        events = sup.poll_once()
        by_svc = {e["service"]: e for e in events}
        self.assertEqual(by_svc["svc_auto"]["event"], "restarted_after_exit")
        self.assertEqual(sup.service_status("svc_auto")["restarts"], 1)
        self.assertEqual(sup.service_status("svc_auto")["status"], "running")

        self.assertEqual(by_svc["svc_no_auto"]["event"], "crashed")
        self.assertEqual(sup.service_status("svc_no_auto")["status"], "crashed")
        self.assertIsNone(sup.service_status("svc_no_auto")["pid"])

    def test_launch_console_supervised_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SABRINA_LOCAL_MICROSERVICES", None)
            self.assertFalse(launch_console.is_supervised_mode([]))
            self.assertTrue(launch_console.is_supervised_mode(["--supervised"]))

        with patch.dict(os.environ, {"SABRINA_LOCAL_MICROSERVICES": "1"}, clear=False):
            self.assertTrue(launch_console.is_supervised_mode([]))


if __name__ == "__main__":
    unittest.main()
