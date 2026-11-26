"""Adapter for the user's existing Sabrina Playwright browser fleet."""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import BrowserSession, docker_command

FLEET_BACKEND = "ego-fleet"
_NODE = re.compile(r"browser_fleet_node_[1-4]")


class FleetBrowserSession(BrowserSession):
    """Use a fresh context on a running fleet node without altering that node."""

    def __init__(self, start_url: str) -> None:
        super().__init__("playwright-cli", start_url)
        self.backend = FLEET_BACKEND
        self.max_actions = 40
        self.target = os.environ.get("SABRINA_FLEET_DOCKER_SSH") or os.environ.get("SABRINA_BROWSER_DOCKER_SSH") or "ego"
        self.node = os.environ.get("SABRINA_FLEET_NODE", "browser_fleet_node_1").strip()
        if not _NODE.fullmatch(self.node):
            raise ValueError("SABRINA_FLEET_NODE must name browser_fleet_node_1 through browser_fleet_node_4.")

    def manifest(self) -> dict[str, str]:
        return {**super().manifest(), "session": "temporary isolated fleet context"}

    def check_ready(self) -> None:
        try:
            result = subprocess.run(
                docker_command("inspect", "--format", "{{.State.Running}}", self.node, target_override=self.target),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not reach the browser fleet: {exc}") from exc
        if result.returncode or result.stdout.strip().lower() != "true":
            raise RuntimeError(f"The selected fleet node {self.node} is not running.")
        probe = (
            "from playwright.sync_api import sync_playwright;"
            "p=sync_playwright().start();"
            "b=p.chromium.connect('ws://127.0.0.1:3000/',timeout=4000);"
            "print('ready' if b.is_connected() else 'unavailable');"
            "b.close();p.stop()"
        )
        try:
            result = subprocess.run(
                docker_command("exec", self.node, "python3", "-c", probe, target_override=self.target),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not check the fleet's Playwright server: {exc}") from exc
        if result.returncode or result.stdout.strip() != "ready":
            raise RuntimeError(f"The selected fleet node {self.node} is running, but its Playwright server is unavailable.")

    def _start(self, cancel_event: threading.Event) -> None:
        start = time.monotonic()
        folder = Path(__file__).parent
        protocol = base64.b64encode((folder / "protocol.py").read_bytes()).decode("ascii")
        worker = base64.b64encode((folder / "fleet_worker.py").read_bytes()).decode("ascii")
        bootstrap = (
            "import base64,sys,types;"
            "m=types.ModuleType('sabrina_fleet_protocol');"
            f"exec(compile(base64.b64decode('{protocol}'),'protocol.py','exec'),m.__dict__);"
            "sys.modules[m.__name__]=m;"
            f"exec(compile(base64.b64decode('{worker}'),'fleet_worker.py','exec'),{{'__name__':'__main__'}})"
        )
        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
        self._process = subprocess.Popen(
            docker_command("exec", "-i", self.node, "timeout", "--kill-after=5s", "15m", "python3", "-u", "-c", bootstrap, target_override=self.target),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._read_replies, daemon=True)
        self._reader.start()
        reply = self._request({"start_url": self.start_url}, cancel_event, 18)
        self.startup_ms = round((time.monotonic() - start) * 1_000)
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error") or "The fleet browser could not start.")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.cleanup_error = "The fleet worker did not exit promptly; its 15-minute limit remains active."
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if self._reader is not None:
                self._reader.join(timeout=1)
            process.stdout.close()
        if self._stderr is not None:
            self._stderr.close()
