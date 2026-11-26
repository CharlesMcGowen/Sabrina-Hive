"""A mission tab in the operator's existing Chrome or Edge session."""

from __future__ import annotations

import importlib.util
import os
import re
import threading
import time
from pathlib import Path
from . import MAX_TOOL_ROUNDS
from .protocol import browser_action, scoped_url, snapshot_page

_BROWSERS = {
    "visible-chrome": ("chrome", "Google/Chrome", "Chrome"),
    "visible-edge": ("msedge", "Microsoft/Edge", "Microsoft Edge"),
}
_CHALLENGE = re.compile(
    r"performing security verification|additional security check|verify (?:you are|you're) (?:a )?human|"
    r"confirm (?:you are|you're) human|checking your browser|complete (?:the )?(?:captcha|security check)",
    re.IGNORECASE,
)


class VisibleBrowserSession:
    """Connect to an existing browser after the operator enables its native prompt."""

    is_visible = True

    def __init__(self, backend: str, start_url: str, *, manual_start: bool = False) -> None:
        if backend not in _BROWSERS:
            raise ValueError("Choose an existing Chrome or Microsoft Edge browser.")
        self.backend = backend
        self.start_url, self.origin = scoped_url(start_url)
        self.events: list[dict] = []
        self.startup_ms: int | None = None
        self.cleanup_error = ""
        self.is_open = False
        self.manual_start = bool(manual_start)
        self.max_actions = 40
        self.initial_handoff_done = False
        self._attempts = 0
        self._playwright = None
        self._browser = None
        self._page = None
        self._last_status = None
        self._closed = False
        self._snapshot_page = 0
        self._latest_snapshot = ""

    def manifest(self) -> dict[str, str]:
        return {"backend": self.backend, "start_url": self.start_url, "origin": self.origin}

    def check_ready(self) -> None:
        if importlib.util.find_spec("playwright") is None:
            raise RuntimeError("Visible browser support requires the Playwright Python package. See browser setup.")
        _, profile, name = _BROWSERS[self.backend]
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data or not (Path(local_app_data) / profile / "User Data" / "DevToolsActivePort").is_file():
            address = "chrome://inspect/#remote-debugging" if self.backend == "visible-chrome" else "edge://inspect/#remote-debugging"
            raise RuntimeError(
                f"In {name}, open {address} and enable 'Allow remote debugging for this browser instance'. "
                "Then launch the mission again."
            )

    def _on_response(self, response) -> None:
        try:
            if response.request.is_navigation_request() and response.request.frame == self._page.main_frame:
                self._last_status = response.status
        except Exception:
            pass

    def _start(self) -> None:
        from playwright.sync_api import sync_playwright

        started = time.monotonic()
        self._playwright = sync_playwright().start()
        try:
            channel = _BROWSERS[self.backend][0]
            self._browser = self._playwright.chromium.connect_over_cdp(channel, timeout=90_000, no_defaults=True)
            if not self._browser.contexts:
                raise RuntimeError("The browser did not provide a profile for this session.")
            self._page = self._browser.contexts[0].new_page()
            self._page.set_default_timeout(8_000)
            self._page.on("response", self._on_response)
            self._page.bring_to_front()
            self.is_open = True
        except Exception:
            self._playwright.stop()
            self._playwright = None
            self._browser = None
            self._page = None
            raise
        self.startup_ms = round((time.monotonic() - started) * 1_000)

    def _observe(self) -> dict:
        from playwright.sync_api import Error

        page = self._page
        url = page.evaluate("() => location.href")
        try:
            same_origin = scoped_url(url)[1] == self.origin
        except ValueError:
            same_origin = False
        if not same_origin:
            return {
                "ok": False, "url": url, "http_status": self._last_status,
                "needs_human": True,
                "error": f"The tab is outside {self.origin}. Return to the requested site before continuing.",
            }
        snapshot = ""
        for _ in range(8):
            try:
                snapshot = page.aria_snapshot(mode="ai", timeout=10_000)
            except Error:
                pass
            if snapshot.strip():
                break
            page.wait_for_timeout(300)
        if not snapshot.strip():
            raise RuntimeError("The browser page is still loading. Take another snapshot before acting.")
        page_text, self._snapshot_page = snapshot_page(snapshot, self._snapshot_page)
        self._latest_snapshot = page_text
        result = {
            "ok": not self._last_status or self._last_status < 400,
            "url": url,
            "http_status": self._last_status,
            "snapshot": page_text,
        }
        if _CHALLENGE.search(snapshot) or self._last_status == 403:
            result["ok"] = False
            result["needs_human"] = True
            result["error"] = "The site needs your attention. Review the page and complete any verification."
        elif not result["ok"]:
            result["error"] = f"The page returned HTTP {self._last_status}."
        return result

    def run(self, raw: object, cancel_event: threading.Event) -> dict:
        from playwright.sync_api import Error

        started = time.monotonic()
        action = ""
        dispatched = False
        try:
            request = browser_action(raw, self.start_url, self.origin)
            action = request["action"]
            if self._closed:
                raise ValueError("The browser session is closed.")
            if self._attempts >= self.max_actions or cancel_event.is_set():
                raise ValueError("The browser action budget is exhausted or the mission was cancelled.")
            self._attempts += 1
            if self._page is None:
                if action != "open":
                    raise ValueError("Use the open action first.")
                self._start()
            page = self._page
            if action in {"click", "fill", "select"} and f'[ref={request["ref"]}]' not in self._latest_snapshot:
                raise ValueError("That ref is not in the latest snapshot. Choose a fresh ref from the current page.")
            dispatched = True
            if action == "open":
                self._last_status = None
                page.goto(request["url"], wait_until="domcontentloaded", timeout=30_000)
            elif action == "back":
                self._last_status = None
                page.go_back(wait_until="domcontentloaded", timeout=30_000)
            elif action == "press":
                page.keyboard.press(request["key"])
            elif action == "snapshot":
                self._snapshot_page = request["page"]
            elif action != "snapshot":
                locator = page.get_by_ref(request["ref"])
                if action == "click":
                    locator.click()
                elif action == "fill":
                    locator.fill(request["value"])
                elif action == "select":
                    locator.select_option(request["value"])
            result = self._observe()
        except (ValueError, RuntimeError, Error) as exc:
            if self._page is not None and self._last_status == 403:
                result = {
                    "ok": False, "http_status": 403, "needs_human": True,
                    "error": "The site returned HTTP 403. Review the page and complete any verification.",
                }
            else:
                result = {"ok": False, "error": str(exc)[:800]}
                if self._page is not None and dispatched:
                    try:
                        current = self._observe()
                        if current.get("needs_human"):
                            result = current
                        else:
                            result.update({key: current[key] for key in ("snapshot", "url", "http_status") if key in current})
                    except Exception:
                        pass
                elif self._latest_snapshot:
                    result["snapshot"] = self._latest_snapshot
        event = {
            "backend": self.backend, "action": action,
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
            "dispatched": dispatched, **result,
        }
        self.events.append(event)
        return event

    def observe_after_user(self, cancel_event: threading.Event) -> dict:
        started = time.monotonic()
        try:
            if self._closed or cancel_event.is_set():
                raise ValueError("The mission was stopped.")
            result = self._observe()
        except Exception as exc:
            result = {"ok": False, "error": str(exc)[:800]}
        event = {
            "backend": self.backend, "action": "snapshot", "dispatched": True,
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
            "user_completed_handoff": not cancel_event.is_set(),
            **result,
        }
        self.events.append(event)
        return event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                self.cleanup_error = str(exc)[:300]

    def telemetry(self) -> dict:
        return {
            **self.manifest(),
            "startup_ms": self.startup_ms,
            "actions": len(self.events),
            "action_ms": sum(event["elapsed_ms"] for event in self.events),
            "snapshot_chars": sum(len(event.get("snapshot", "")) for event in self.events),
            "cleanup_error": self.cleanup_error,
            "manual_start": self.manual_start,
        }
