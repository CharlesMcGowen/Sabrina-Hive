"""On-demand browser containers for Context-First missions."""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from .protocol import BACKENDS, BROWSER_TOOL, browser_action, scoped_url

IMAGE = "sabrina-agent-browser:1"
MAX_TOOL_ROUNDS = 12


def docker_command(*args: str, target_override: str | None = None) -> list[str]:
    target = target_override if target_override is not None else os.environ.get("SABRINA_BROWSER_DOCKER_SSH")
    if target is None:
        target = "ego" if os.name == "nt" and shutil.which("docker") is None else "local"
    if target == "local":
        return ["docker", *args]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(?:@[A-Za-z0-9_][A-Za-z0-9_.-]*)?", target):
        raise ValueError("The Docker target must be local or a configured OpenSSH destination.")
    return [
        "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
        "-o", "StrictHostKeyChecking=yes", target, shlex.join(["docker", *args]),
    ]


class BrowserSession:
    """A single mission's CLI, container, site scope, and observed results."""

    def __init__(self, backend: str, start_url: str) -> None:
        if backend not in BACKENDS:
            raise ValueError("Choose agent-browser or playwright-cli.")
        self.backend = backend
        self.start_url, self.origin = scoped_url(start_url)
        self.name = f"sabrina-browser-{uuid.uuid4().hex}"
        self.events: list[dict] = []
        self.startup_ms: int | None = None
        self.cleanup_error = ""
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._replies: queue.Queue[str | None] = queue.Queue()
        self._closed = False
        self._stderr = None
        self.max_actions = MAX_TOOL_ROUNDS

    def manifest(self) -> dict[str, str]:
        return {"backend": self.backend, "start_url": self.start_url, "origin": self.origin}

    @staticmethod
    def check_ready() -> None:
        try:
            probe = subprocess.run(
                docker_command("image", "inspect", "--format", "{{.Id}}", IMAGE),
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not reach the browser Docker engine: {exc}") from exc
        if probe.returncode:
            detail = probe.stderr.strip()[:350]
            raise RuntimeError(f"Browser image unavailable. Build it on the configured Docker engine. {detail}")

    def _read_replies(self) -> None:
        try:
            for line in self._process.stdout:
                self._replies.put(line)
        finally:
            self._replies.put(None)

    def _request(self, request: dict, cancel_event: threading.Event, timeout: float) -> dict:
        if cancel_event.is_set():
            raise InterruptedError("Mission was cancelled.")
        self._process.stdin.write(json.dumps(request) + "\n")
        self._process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise InterruptedError("Mission was cancelled.")
            try:
                line = self._replies.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                self._stderr.seek(0)
                detail = self._stderr.read(800).strip()
                raise RuntimeError(f"Browser container exited. {detail}")
            reply = json.loads(line)
            if not isinstance(reply, dict) or "ok" not in reply:
                raise RuntimeError("Browser container returned an invalid reply.")
            return reply
        raise TimeoutError("Browser container did not respond in time.")

    def _start(self, cancel_event: threading.Event) -> None:
        start = time.monotonic()
        self._stderr = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
        self._process = subprocess.Popen(
            docker_command(
                "run", "--rm", "--init", "-i", "--pull=never", "--name", self.name,
                "--label", "com.sabrina.service=agent-browser",
                "--log-driver=none",
                "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--memory=1024m", "--cpus=1.5", "--pids-limit=256", "--shm-size=256m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,exec,mode=1777,size=256m", IMAGE,
            ),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._read_replies, daemon=True)
        self._reader.start()
        reply = self._request({"backend": self.backend, "start_url": self.start_url}, cancel_event, 18)
        self.startup_ms = round((time.monotonic() - start) * 1_000)
        if not reply["ok"]:
            raise RuntimeError(reply.get("error") or "The browser container could not start.")

    def run(self, raw: object, cancel_event: threading.Event) -> dict:
        start = time.monotonic()
        action = ""
        dispatched = False
        try:
            request = browser_action(raw, self.start_url, self.origin)
            action = request["action"]
            if self._closed:
                raise ValueError("The browser session is closed.")
            if len(self.events) >= self.max_actions or cancel_event.is_set():
                raise ValueError("The browser action budget is exhausted or the mission was cancelled.")
            if self._process is None:
                if action != "open":
                    raise ValueError("Use the open action first.")
                self._start(cancel_event)
            dispatched = True
            result = self._request(request, cancel_event, 42)
        except (OSError, RuntimeError) as exc:
            result = {"ok": False, "error": str(exc)[:800]}
            self.close()
        except ValueError as exc:
            result = {"ok": False, "error": str(exc)[:800]}
        event = {
            "backend": self.backend, "action": action,
            "elapsed_ms": round((time.monotonic() - start) * 1_000),
            "dispatched": dispatched,
            **result,
        }
        if len(self.events) < self.max_actions:
            self.events.append(event)
        return event

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
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    cleanup = subprocess.run(
                        docker_command("rm", "-f", self.name),
                        capture_output=True, text=True, timeout=8,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if cleanup.returncode and "No such container" not in cleanup.stderr:
                        self.cleanup_error = cleanup.stderr.strip()[:300]
                except (OSError, subprocess.TimeoutExpired) as exc:
                    self.cleanup_error = str(exc)[:300]
                finally:
                    process.kill()
                    process.wait()
            if self._reader is not None:
                self._reader.join(timeout=1)
            process.stdout.close()
        if self._stderr is not None:
            self._stderr.close()

    def telemetry(self) -> dict:
        return {
            **self.manifest(),
            "startup_ms": self.startup_ms,
            "actions": len(self.events),
            "action_ms": sum(event["elapsed_ms"] for event in self.events),
            "snapshot_chars": sum(len(event.get("snapshot", "")) for event in self.events),
            "cleanup_error": self.cleanup_error,
        }
