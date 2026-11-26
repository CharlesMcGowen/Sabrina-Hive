"""Container entry point. The stdio protocol exposes browser actions only."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import select
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urldefrag, urlsplit

from .protocol import BACKENDS, browser_action, scoped_url, snapshot_page

_BINARY = "/usr/local/bin/sabrina-chromium"
_PAGE_STATE = '({url: location.href, status: performance.getEntriesByType("navigation")[0]?.responseStatus || 0})'


def public_endpoints(host: str, port: int) -> list[tuple]:
    endpoints = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for _, _, _, _, address in endpoints:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast:
            raise ValueError("The browser site must resolve to public IP addresses.")
    return endpoints


class SiteProxy(BaseHTTPRequestHandler):
    """Tunnel TLS only to the selected origin's verified addresses."""

    def log_message(self, *_args) -> None:
        pass

    def do_CONNECT(self) -> None:
        if self.path.lower() != self.server.target:
            self.send_error(403, "Outside browser scope")
            return
        try:
            with self._connect() as upstream:
                self.send_response(200, "Connection established")
                self.end_headers()
                peers = {self.connection: upstream, upstream: self.connection}
                while readable := select.select(list(peers), [], [], 45)[0]:
                    for reader in readable:
                        chunk = reader.recv(65_536)
                        if not chunk:
                            return
                        peers[reader].sendall(chunk)
        except OSError:
            return

    def _connect(self) -> socket.socket:
        for family, kind, protocol, _, address in self.server.endpoints:
            connection = socket.socket(family, kind, protocol)
            connection.settimeout(8)
            try:
                connection.connect(address)
                return connection
            except OSError:
                connection.close()
        raise OSError("Unable to connect to the selected site.")


class BrowserWorker:
    def __init__(self, backend: str, start_url: str) -> None:
        if backend not in BACKENDS:
            raise ValueError("Unknown browser backend.")
        self.backend = backend
        self.start_url, self.origin = scoped_url(start_url)
        parts = urlsplit(self.origin)
        host, port = parts.hostname, parts.port or 443
        endpoints = public_endpoints(host, port)
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), SiteProxy)
        self.proxy.target = f"{host}:{port}"
        self.proxy.endpoints = endpoints
        Thread(target=self.proxy.serve_forever, daemon=True).start()
        proxy_url = f"http://127.0.0.1:{self.proxy.server_port}"
        browser_args = [
            f"--proxy-server={proxy_url}",
            "--proxy-bypass-list=<-loopback>",
            "--disable-quic",
            "--dns-prefetch-disable",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
        self.opened = False
        self.snapshot_page = 0
        self.environment = dict(os.environ)
        if backend == "agent-browser":
            self.command = ["agent-browser", "--json", "--allowed-domains", host,
                            "--executable-path", _BINARY, "--no-webmcp"]
            self.environment["AGENT_BROWSER_ARGS"] = ",".join(browser_args)
        else:
            self.command = ["playwright-cli", "-s=sabrina", "--raw"]
            self.config = Path("/tmp/sabrina-playwright.json")
            self.config.write_text(json.dumps({
                "browser": {
                    "browserName": "chromium",
                    "launchOptions": {"executablePath": _BINARY, "args": browser_args},
                    "contextOptions": {"acceptDownloads": False, "serviceWorkers": "block"},
                },
                "network": {"allowedOrigins": [self.origin]},
                "timeouts": {"navigation": 12_000, "action": 5_000, "settle": 150, "idle": 180_000},
            }), encoding="utf-8")

    def _run(self, *args: str):
        result = subprocess.run(
            [*self.command, *args], env=self.environment,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        output = result.stdout.strip()
        if self.backend == "agent-browser" and output:
            data = json.loads(output)
            if data.get("success"):
                return data.get("data") or {}
            raise ValueError(str(data.get("error") or result.stderr or "Browser command failed.")[:600])
        if result.returncode or "### Error" in output:
            raise ValueError((result.stderr.strip() or output or "Browser command failed.")[:600])
        return output

    def _capture_page(self) -> tuple[dict, str]:
        if self.backend == "agent-browser":
            snapshot = self._run("snapshot", "-c").get("snapshot", "")
            page = self._run("eval", _PAGE_STATE)["result"]
        else:
            snapshot = self._run("snapshot")
            page = json.loads(self._run("eval", f"() => {_PAGE_STATE}"))
        return page, snapshot

    def _observe(self) -> dict:
        try:
            page, snapshot = self._capture_page()
        except ValueError as exc:
            if "document changed" not in str(exc).lower() and "execution context was destroyed" not in str(exc).lower():
                raise
            time.sleep(0.2)
            page, snapshot = self._capture_page()
        url = page.get("url", "")
        if scoped_url(url)[1] != self.origin:
            raise ValueError("The browser did not reach a page in the selected origin.")
        status = page.get("status") or None
        page_text, self.snapshot_page = snapshot_page(snapshot, self.snapshot_page)
        observed = {
            "ok": not status or status < 400,
            "url": url,
            "http_status": status,
            "snapshot": page_text,
        }
        if status and status >= 400:
            observed["error"] = f"The page returned HTTP {status}."
        return observed

    def _failed_navigation(self, url: str) -> int | None:
        url = urldefrag(url).url
        if self.backend == "agent-browser":
            requests = self._run("network", "requests", "--type", "document").get("requests", [])
            for request in reversed(requests):
                if request.get("url") == url and isinstance(request.get("status"), int):
                    return request["status"]
        else:
            for line in reversed(self._run("requests", "--static").splitlines()):
                if match := re.search(rf"\[GET\] {re.escape(url)} => \[(\d{{3}})\]", line):
                    return int(match.group(1))
        return None

    def run(self, raw: object) -> dict:
        request = browser_action(raw, self.start_url, self.origin)
        action = request["action"]
        if action == "open":
            url = request["url"]
            try:
                if self.backend == "agent-browser":
                    self._run("open", url)
                elif self.opened:
                    self._run("goto", url)
                else:
                    self._run("open", url, f"--config={self.config}")
            except ValueError as exc:
                if "ERR_HTTP_RESPONSE_CODE_FAILURE" not in str(exc):
                    raise
                self.opened = True
                try:
                    status = self._failed_navigation(url)
                except (ValueError, OSError, subprocess.TimeoutExpired):
                    status = None
                return {
                    "ok": False, "url": url, "http_status": status,
                    "error": f"The page returned HTTP {status}." if status else str(exc)[:600],
                }
            self.opened = True
        elif not self.opened:
            raise ValueError("Use the open action first.")
        elif action == "press":
            self._run("press", request["key"])
        elif action == "back":
            self._run("back" if self.backend == "agent-browser" else "go-back")
        elif action == "snapshot":
            self.snapshot_page = request["page"]
        elif action != "snapshot":
            ref = request["ref"]
            if self.backend == "agent-browser":
                ref = "@" + ref
            args = [request["value"]] if "value" in request else []
            self._run(action, ref, *args)
        if self.backend == "agent-browser" and action in {"click", "press", "back"}:
            time.sleep(0.2)
        return self._observe()


def main() -> None:
    try:
        settings = json.loads(sys.stdin.readline())
        worker = BrowserWorker(settings["backend"], settings["start_url"])
    except (KeyError, TypeError, ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:600]}))
        return
    print(json.dumps({"ok": True}))
    for line in sys.stdin:
        try:
            reply = worker.run(json.loads(line))
        except (KeyError, TypeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
            reply = {"ok": False, "error": str(exc)[:600]}
        print(json.dumps(reply))


if __name__ == "__main__":
    main()
