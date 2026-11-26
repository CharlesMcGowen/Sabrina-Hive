"""Shared browser tool inputs for the coordinator and the container."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit, urlunsplit

BROWSER_TOOL = "browser_session"
BACKENDS = ("agent-browser", "playwright-cli")
SNAPSHOT_LIMIT = 5_000
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")
_REF = re.compile(r"@?(?:f[0-9]+)?e[0-9]+")
_KEYS = {"Enter", "Tab", "Shift+Tab", "Escape", "Space", "ArrowUp", "ArrowDown",
         "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"}


def browser_reply(text: str) -> dict | None:
    """Read one model command. Later text cannot enqueue more browser actions."""
    text = text.strip().replace("\r\n", "\n")
    for fence in ("```tool:browser_session", "```tool:desktop_session", "```json", "```"):
        if text.startswith(fence + "\n"):
            text = text[len(fence):].lstrip()
            break
    if not text.startswith("{"):
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    return value if isinstance(value.get("action"), str) else None


def snapshot_page(snapshot: str, requested: int) -> tuple[str, int]:
    """Keep every complete line of a large snapshot accessible to the model."""
    pages, lines, count = [], [], 0
    for line in snapshot.splitlines():
        line = line[:SNAPSHOT_LIMIT - 100]
        if lines and count + len(line) + 1 > SNAPSHOT_LIMIT - 100:
            pages.append("\n".join(lines))
            lines, count = [], 0
        lines.append(line)
        count += len(line) + 1
    if lines:
        pages.append("\n".join(lines))
    pages = pages or [""]
    actual = min(requested, len(pages) - 1)
    result = pages[actual]
    if len(pages) > 1:
        result = f"[Snapshot page {actual + 1} of {len(pages)}; use snapshot with page=0, 1, ... to view other parts]\n" + result
    return result, actual


def scoped_url(raw: str) -> tuple[str, str]:
    """Return a canonical HTTPS URL and its origin. The worker checks public DNS."""
    value = raw.strip() if isinstance(raw, str) else ""
    if not value or len(value) > 4_096 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("Enter a valid HTTPS URL for the browser.")
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = 443 if parts.port is None else parts.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Enter a valid HTTPS URL for the browser.") from exc
    if parts.scheme != "https" or not _HOST.fullmatch(host) or parts.username is not None or port == 0:
        raise ValueError("Use an HTTPS URL with a public hostname and no embedded credentials.")
    netloc = host if port == 443 else f"{host}:{port}"
    url = urlunsplit(("https", netloc, parts.path or "/", parts.query, parts.fragment))
    return url, f"https://{netloc}"


def browser_action(raw: object, start_url: str, origin: str) -> dict[str, str]:
    if not isinstance(raw, dict) or not isinstance(raw.get("action"), str):
        raise ValueError("Provide a JSON object with an action.")
    action = raw["action"]
    fields = {
        "open": {"url"}, "snapshot": {"page"}, "back": set(), "click": {"ref"},
        "fill": {"ref", "value"}, "select": {"ref", "value"}, "press": {"key"},
    }
    if action not in fields or set(raw) - fields[action] - {"action"}:
        raise ValueError("Use open, snapshot, back, click, fill, select, or press with the documented fields.")
    if action == "open":
        url, url_origin = scoped_url(raw.get("url", start_url))
        if url_origin != origin:
            raise ValueError(f"Browser navigation is limited to {origin}.")
        return {"action": action, "url": url}
    if action == "press":
        key = raw.get("key")
        if not isinstance(key, str) or key not in _KEYS:
            raise ValueError("Use a browser navigation key such as Enter, Tab, Escape, or ArrowDown.")
        return {"action": action, "key": key}
    if action == "snapshot":
        page = raw.get("page", 0)
        if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 99:
            raise ValueError("Snapshot page must be a number between 0 and 99.")
        return {"action": action, "page": page}
    if action == "back":
        return {"action": action}
    ref = raw.get("ref")
    if isinstance(ref, str) and not _REF.fullmatch(ref):
        embedded = re.findall(r"\[ref=(@?(?:f[0-9]+)?e[0-9]+)\]", ref)
        if len(embedded) == 1:
            ref = embedded[0]
    if not isinstance(ref, str) or not _REF.fullmatch(ref):
        raise ValueError("Use an element ref from the last snapshot, such as @e1 or e12.")
    result = {"action": action, "ref": ref.lstrip("@")}
    if action in {"fill", "select"}:
        value = raw.get("value")
        if not isinstance(value, str) or len(value) > 2_000 or "\x00" in value or value.startswith("--"):
            raise ValueError("Use a text value of up to 2,000 characters that does not start with --.")
        result["value"] = value
    return result
