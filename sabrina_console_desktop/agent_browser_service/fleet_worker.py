"""Isolated browser session hosted by an existing Sabrina Playwright fleet node.

This module runs inside the selected fleet container.  Its protocol module is
provided by the desktop launcher, so the fleet need not be modified or rebuilt.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import sys
from urllib.parse import urlsplit

try:
    from sabrina_fleet_protocol import browser_action, scoped_url, snapshot_page
except ImportError:  # local tests; the fleet receives the same protocol over stdio
    from sabrina_console_desktop.agent_browser_service.protocol import browser_action, scoped_url, snapshot_page

_CHALLENGE = re.compile(
    r"performing security verification|additional security check|verify (?:you are|you're) (?:a )?human|"
    r"confirm (?:you are|you're) human|checking your browser|complete (?:the )?(?:captcha|security check)|"
    r"access denied|you don.t have permission to access",
    re.IGNORECASE,
)


class FleetWorker:
    def __init__(self, start_url: str) -> None:
        from playwright.sync_api import sync_playwright

        self.start_url, self.origin = scoped_url(start_url)
        self.page_number = 0
        self.latest_snapshot = ""
        self.status = None
        self.blocked_navigation = ""
        self.blocked_verification = False
        self._dns: dict[tuple[str, int], bool] = {}
        self.playwright = sync_playwright().start()
        self.browser = None
        self.context = None
        try:
            self.browser = self.playwright.chromium.connect("ws://127.0.0.1:3000/", timeout=8_000)
            self.context = self.browser.new_context(accept_downloads=False, service_workers="block")
            self.page = self.context.new_page()
            self.page.set_default_timeout(8_000)
            self.context.route("**/*", self._route)
            self.context.route_web_socket("**/*", lambda websocket: websocket.close())
            self.page.on("response", self._on_response)
        except Exception:
            self.close()
            raise

    def _public_url(self, raw: str) -> bool:
        try:
            parts = urlsplit(raw)
            host, port = parts.hostname, parts.port or 443
            if parts.scheme != "https" or not host or parts.username is not None:
                return False
            key = (host, port)
            if key not in self._dns:
                addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                self._dns[key] = bool(addresses) and all(
                    ipaddress.ip_address(address[4][0]).is_global
                    and not ipaddress.ip_address(address[4][0]).is_multicast
                    for address in addresses
                )
            return self._dns[key]
        except (ValueError, OSError):
            return False

    def _route(self, route) -> None:
        request = route.request
        url = request.url
        allowed = self._public_url(url)
        if allowed and request.is_navigation_request():
            try:
                allowed = scoped_url(url)[1] == self.origin
            except ValueError:
                allowed = False
            if not allowed:
                self.blocked_navigation = "A navigation outside the selected site was blocked."
        if allowed:
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.request.frame == self.page.main_frame:
                self.status = response.status
        except Exception:
            pass

    def _observe(self) -> dict:
        from playwright.sync_api import Error

        url = self.page.url
        if self.blocked_navigation:
            error, self.blocked_navigation = self.blocked_navigation, ""
            self.latest_snapshot = ""
            return {"ok": False, "url": url, "error": error}
        try:
            if scoped_url(url)[1] != self.origin:
                raise ValueError("The page is outside the selected site.")
        except ValueError:
            self.latest_snapshot = ""
            return {"ok": False, "url": url, "error": "The page is outside the selected site."}
        snapshot = ""
        for _ in range(6):
            try:
                snapshot = self.page.aria_snapshot(mode="ai", timeout=10_000)
            except Error:
                pass
            if snapshot.strip():
                break
            self.page.wait_for_timeout(250)
        if not snapshot.strip():
            self.latest_snapshot = ""
            return {"ok": False, "url": url, "error": "The page is still loading. Take another snapshot."}
        text, self.page_number = snapshot_page(snapshot, self.page_number)
        self.latest_snapshot = text
        result = {"ok": not self.status or self.status < 400, "url": url,
                  "http_status": self.status, "snapshot": text}
        if _CHALLENGE.search(snapshot):
            self.blocked_verification = True
            result.update(ok=False, error="The site needs a user verification. Use a visible browser for this task.")
        elif not result["ok"]:
            self.blocked_verification = self.status == 403
            result["error"] = f"The page returned HTTP {self.status}. The temporary fleet browser cannot accept a user verification."
        return result

    def run(self, raw: object) -> dict:
        request = browser_action(raw, self.start_url, self.origin)
        action = request["action"]
        if self.blocked_verification and action != "snapshot":
            return {"ok": False, "error": "The site needs a user verification. Use a visible browser for this task.",
                    "snapshot": self.latest_snapshot, "dispatched": False}
        if action in {"click", "fill", "select"} and f'[ref={request["ref"]}]' not in self.latest_snapshot:
            return {"ok": False, "error": "That ref is not in the latest snapshot. Choose a fresh ref from the current page.",
                    "snapshot": self.latest_snapshot, "dispatched": False}
        try:
            if action == "open":
                self.status = None
                self.page.goto(request["url"], wait_until="domcontentloaded", timeout=30_000)
            elif action == "back":
                self.status = None
                self.page.go_back(wait_until="domcontentloaded", timeout=30_000)
            elif action == "snapshot":
                self.page_number = request["page"]
            elif action == "press":
                self.page.keyboard.press(request["key"])
            else:
                locator = self.page.locator("aria-ref=" + request["ref"])
                if action == "click":
                    locator.click()
                elif action == "fill":
                    locator.fill(request["value"])
                elif action == "select":
                    locator.select_option(request["value"])
            return self._observe()
        except Exception as exc:
            result = {"ok": False, "error": str(exc).splitlines()[0][:600]}
            try:
                observed = self._observe()
                result.update({key: observed[key] for key in ("snapshot", "url", "http_status") if key in observed})
                if observed.get("error"):
                    result["error"] += " " + observed["error"]
            except Exception:
                pass
            return result

    def close(self) -> None:
        try:
            if self.context is not None:
                self.context.close()
        finally:
            try:
                if self.browser is not None:
                    self.browser.close()
            finally:
                self.playwright.stop()


def main() -> None:
    worker = None
    try:
        settings = json.loads(sys.stdin.readline())
        worker = FleetWorker(settings["start_url"])
        print(json.dumps({"ok": True}), flush=True)
        for line in sys.stdin:
            try:
                result = worker.run(json.loads(line))
            except (KeyError, TypeError, ValueError) as exc:
                result = {"ok": False, "error": str(exc)[:600]}
            print(json.dumps(result), flush=True)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:600]}), flush=True)
    finally:
        if worker is not None:
            worker.close()


if __name__ == "__main__":
    main()
