"""Resolve explicit browser-opening requests and provide bounded local fallbacks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


_OPEN_REQUEST = re.compile(
    r"^(?:please )?(?:(?:could|can|would) (?:you|yo|u) (?:please )?)?"
    r"(?:(?:open|launch|go to|browse to) (?:the )?(?P<target>my linkedin profile|https?://[^\s<>'\"`]+)"
    r"|(?:get |have )?(?P<profile>my linkedin profile) opened)(?=$|[\s.?!])",
    re.I,
)
_BROWSER = re.compile(r"^\s+(?:in|using|with) (?:the )?"
                      r"(internet explorer|microsoft edge|edge|google chrome|chrome|default browser)\b", re.I)
_BROWSER_NAMES = {"edge": "Microsoft Edge", "chrome": "Google Chrome", "microsoft edge": "Microsoft Edge",
                  "google chrome": "Google Chrome", "internet explorer": "Internet Explorer",
                  "default browser": "Default Browser"}
_GREETING = re.compile(r"(?:and |then )?say\s+(?:['\"](hi|hello|hey)['\"]|(hi|hello|hey))", re.I)
_DEFAULT = "Default Browser"


@dataclass(frozen=True)
class BrowserOpenRequest:
    url: str
    browser: str
    greeting: str = ""
    follow_up: bool = False

    def routes(self) -> tuple[str, ...]:
        if self.browser == "Internet Explorer":
            return self.browser, "Microsoft Edge", _DEFAULT
        if self.browser == _DEFAULT:
            return (_DEFAULT,)
        return self.browser, _DEFAULT


def _validated_url(url: str) -> str:
    if len(url) > 2048 or re.search(r"[\x00-\x20\\\x7f]", url):
        raise ValueError("Provide a valid browser URL.")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
            or parts.password is not None):
            raise ValueError
    except ValueError:
        raise ValueError("Provide an HTTP or HTTPS URL without embedded credentials.") from None
    return url


def parse_browser_request(goal: str, account_url: str = "") -> BrowserOpenRequest | None:
    normalized = " ".join(goal.split())
    intent = _OPEN_REQUEST.match(normalized)
    if not intent:
        return None
    target = intent["target"] or intent["profile"]
    if target.casefold() == "my linkedin profile":
        url = _validated_url(account_url or "https://www.linkedin.com/in/me/")
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in {"www.linkedin.com", "linkedin.com"} or not re.fullmatch(
            r"/in/[A-Za-z0-9_%-]+/?", parts.path
        ) or parts.query or parts.fragment:
            raise ValueError("The configured LinkedIn account URL must be an HTTPS LinkedIn profile link.")
    else:
        url = _validated_url(target.rstrip(".,;!?"))
    remaining = normalized[intent.end():]
    named_browser = _BROWSER.match(remaining)
    browser = _BROWSER_NAMES[named_browser[1].casefold()] if named_browser else _DEFAULT
    tail = (remaining[named_browser.end():] if named_browser else remaining).strip(" ,.!?")
    greeting = _GREETING.fullmatch(tail)
    return BrowserOpenRequest(url, browser, (greeting[1] or greeting[2]) if greeting else "", bool(tail and not greeting))


def dispatch_browser_open(browser: str, url: str) -> str:
    """Dispatch one browser navigation, returning the evidence available from the launcher."""
    url = _validated_url(url)
    if browser == "Internet Explorer":
        raise OSError("The Internet Explorer desktop app is unavailable. Microsoft Edge is its supported replacement.")
    if browser == _DEFAULT:
        if not webbrowser.open(url, new=2):
            raise OSError("Windows did not accept the default-browser launch request.")
        return "Default browser accepted the address."
    install = {
        "Microsoft Edge": ("Microsoft", "Edge", "Application", "msedge.exe"),
        "Google Chrome": ("Google", "Chrome", "Application", "chrome.exe"),
    }.get(browser)
    if install is None or sys.platform != "win32":
        raise OSError(f"{browser} launch is unavailable on this host.")
    roots = (os.environ.get(name) for name in ("ProgramFiles(x86)", "ProgramW6432", "ProgramFiles", "LOCALAPPDATA"))
    candidates = (Path(root).joinpath(*install) for root in roots if root)
    executable = next((path for path in candidates if path.is_absolute() and path.is_file()), None)
    if executable is None:
        raise OSError(f"{browser} executable was not found.")
    subprocess.Popen([str(executable), url], creationflags=subprocess.CREATE_NO_WINDOW)
    return f"Windows accepted the request to open this address in {browser}."
