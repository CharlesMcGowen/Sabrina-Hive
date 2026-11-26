"""Authenticated local RPC bridge for Sabrina -> Windows agent-desktop.

Run this on the logged-in Windows orchestrator. Keep it bound to loopback and
reach it from the lab through an SSH tunnel. Mutations require a second,
explicit approval token in addition to the bridge bearer token.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from tools.desktop_agent import act, observe

HOST = "127.0.0.1"
PORT = int(os.getenv("SABRINA_DESKTOP_BRIDGE_PORT", "8765"))
AUTH_TOKEN = os.getenv("SABRINA_DESKTOP_BRIDGE_TOKEN", "")
APP_ALLOWLIST = {x.strip().lower() for x in os.getenv("SABRINA_DESKTOP_APPS", "notepad.exe,chrome.exe,ChatGPT.exe").split(",") if x.strip()}
OBSERVE_COMMANDS = {"status", "list-apps", "list-windows", "snapshot", "find", "get", "wait"}
ACTION_COMMANDS = {"launch", "click", "set-value", "clear"}


def _json_error(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"ok": False, "error": code, "message": message}


def _constant_time_equal(left: str, right: str) -> bool:
    return bool(left) and bool(right) and hmac.compare_digest(left.encode(), right.encode())


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "SabrinaDesktopBridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Do not log authorization headers or request bodies.
        print(f"[desktop-bridge] {self.address_string()} {fmt % args}", flush=True)

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self, mutation: bool = False) -> bool:
        if not AUTH_TOKEN:
            self._reply(503, _json_error(503, "bridge_unconfigured", "SABRINA_DESKTOP_BRIDGE_TOKEN is not set")[1])
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not _constant_time_equal(auth[7:], AUTH_TOKEN):
            self._reply(401, _json_error(401, "unauthorized", "invalid bridge token")[1])
            return False
        if mutation:
            approval = self.headers.get("X-Sabrina-Approval", "")
            expected = os.getenv("SABRINA_DESKTOP_APPROVAL_TOKEN", "")
            if not expected or not _constant_time_equal(approval, expected):
                self._reply(403, _json_error(403, "approval_required", "mutation requires explicit approval token")[1])
                return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, {"ok": True, "service": "sabrina-desktop-bridge", "port": PORT})
            return
        if not self._authorized():
            return
        if self.path == "/pending":
            self._reply(200, {"ok": True, "pending": []})
            return
        self._reply(404, _json_error(404, "not_found", "unknown endpoint")[1])

    def do_POST(self) -> None:
        mutation = self.path == "/act"
        if not self._authorized(mutation=mutation):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 256_000:
                self._reply(413, _json_error(413, "body_too_large", "request body exceeds limit")[1])
                return
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._reply(400, _json_error(400, "invalid_json", "request body must be JSON")[1])
            return
        if not isinstance(body, dict):
            self._reply(400, _json_error(400, "invalid_request", "request body must be an object")[1])
            return
        command = body.get("command")
        app = str(body.get("app", "")).lower() if body.get("app") else None
        if app and app not in APP_ALLOWLIST:
            self._reply(403, _json_error(403, "app_not_allowed", "app is outside the bridge allowlist")[1])
            return
        if self.path == "/observe":
            if command not in OBSERVE_COMMANDS:
                self._reply(400, _json_error(400, "command_not_allowed", "unsupported observation command")[1])
                return
            result = observe(**{k: v for k, v in body.items() if k != "app"}, app=app)
            self._reply(200, result)
            return
        if self.path == "/act":
            if command not in ACTION_COMMANDS:
                self._reply(400, _json_error(400, "command_not_allowed", "unsupported action command")[1])
                return
            result = act(**{k: v for k, v in body.items() if k != "app"}, app=app)
            self._reply(200, result)
            return
        self._reply(404, _json_error(404, "not_found", "unknown endpoint")[1])


def main() -> None:
    if not AUTH_TOKEN:
        raise SystemExit("Set SABRINA_DESKTOP_BRIDGE_TOKEN before starting the bridge")
    if not APP_ALLOWLIST:
        raise SystemExit("SABRINA_DESKTOP_APPS must contain at least one app")
    print(f"Sabrina desktop bridge listening on http://{HOST}:{PORT}; apps={sorted(APP_ALLOWLIST)}", flush=True)
    ThreadingHTTPServer((HOST, PORT), BridgeHandler).serve_forever()


if __name__ == "__main__":
    main()
