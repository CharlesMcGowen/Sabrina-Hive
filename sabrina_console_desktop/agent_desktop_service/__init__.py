"""Vercel agent-desktop integration for user-selected application windows."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

DESKTOP_TOOL = "desktop_session"
MAX_ACTIONS = 40
OUTPUT_LIMIT = 8_000
_DEFAULT_BINARY = Path(__file__).parent / "bin" / "agent-desktop.exe"
_WINDOW_ID = re.compile(r"w-[0-9]+")
_REF = re.compile(r"@s[a-zA-Z0-9]+:e[0-9]+")
_KEY = re.compile(r"(?:ctrl\+|alt\+|shift\+)*(?:[a-z0-9]|enter|return|tab|escape|space|up|down|left|right|home|end|pageup|pagedown|backspace|delete|f(?:[1-9]|1[0-2]))", re.I)
_CHALLENGE = re.compile(
    r"performing security verification|additional security check|verify (?:you are|you're) (?:a )?human|"
    r"confirm (?:you are|you're) human|checking your browser|complete (?:the )?(?:captcha|security check)|"
    r"access denied|you don.t have permission to access",
    re.IGNORECASE,
)


def _browser_document(tree: object, title: str) -> dict[str, Any] | None:
    documents: list[dict[str, Any]] = []

    def collect(node: object) -> None:
        if not isinstance(node, dict):
            return
        if node.get("role") == "document" and str(node.get("name") or "").strip():
            documents.append(node)
        for child in node.get("children") or []:
            collect(child)

    collect(tree)
    normalized_title = title.replace("\u200b", "").casefold()
    matching = [
        node for node in documents
        if str(node["name"]).replace("\u200b", "").casefold() in normalized_title
    ]
    if matching:
        return max(matching, key=lambda node: len(str(node["name"])))
    return next((node for node in documents if "focused" in (node.get("states") or [])), None)


def _binary() -> Path:
    configured = os.environ.get("SABRINA_AGENT_DESKTOP_PATH", "").strip()
    return Path(configured) if configured else _DEFAULT_BINARY


def _command(args: list[str], *, state_root: Path, text_input: str | None = None) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["AGENT_DESKTOP_HOME"] = str(state_root)
    try:
        result = subprocess.run(
            [str(_binary()), *args], input=text_input, capture_output=True,
            text=True, encoding="utf-8", errors="replace", env=environment, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Desktop command timed out. Observe the window before trying another action.") from exc
    except OSError as exc:
        raise RuntimeError(f"Could not start agent-desktop: {exc}") from exc
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError((result.stderr or "agent-desktop returned invalid JSON.")[:600]) from exc
    if not isinstance(data, dict) or "ok" not in data:
        raise RuntimeError("agent-desktop returned an invalid result.")
    return data


def available_windows() -> list[dict[str, Any]]:
    if not _binary().is_file():
        raise RuntimeError("agent-desktop is unavailable. Install the desktop runtime.")
    with tempfile.TemporaryDirectory(prefix="sabrina-desktop-list-") as folder:
        result = _command(["list-windows"], state_root=Path(folder) / "state")
    if not result["ok"]:
        raise RuntimeError(str(result.get("error") or "Could not list desktop windows.")[:600])
    windows = result.get("data")
    if not isinstance(windows, list):
        raise RuntimeError("agent-desktop returned an invalid window list.")
    return [
        {
            "id": str(window.get("id") or ""),
            "app": str(window.get("app_name") or ""),
            "title": str(window.get("title") or ""),
            "process_instance": str(window.get("process_instance") or ""),
            "bounds": window.get("bounds") or {},
            "is_focused": bool(window.get("is_focused")),
        }
        for window in windows
        if isinstance(window, dict) and window.get("accessible")
        and not window.get("minimized") and _WINDOW_ID.fullmatch(str(window.get("id") or ""))
    ]


def _snapshot_text(
    tree: dict[str, Any], *, prefix: str, browser: bool = False, document: dict[str, Any] | None = None,
) -> tuple[str, dict[str, str], dict[str, dict[str, Any]]]:
    lines: list[str] = []
    refs: dict[str, str] = {}
    ref_data: dict[str, dict[str, Any]] = {}

    def find(node: Any, predicate) -> dict[str, Any] | None:
        if not isinstance(node, dict):
            return None
        if predicate(node):
            return node
        for child in node.get("children") or []:
            match = find(child, predicate)
            if match is not None:
                return match
        return None

    if browser:
        address = find(tree, lambda node: "address and search bar" in str(node.get("name") or "").lower())
        if document:
            tree = {"role": "window", "name": tree.get("name", ""), "children": [
                item for item in (document, address) if item is not None
            ]}

    def visit(node: Any, depth: int, parents: tuple[str, ...] = ()) -> None:
        if not isinstance(node, dict):
            return
        ref = str(node.get("ref_id") or "")
        role = str(node.get("role") or "")
        name = str(node.get("name") or "")
        states = [str(state) for state in (node.get("states") or [])]
        if browser and role in {"tab", "tabitem"} and "selected" not in states:
            return
        protected = "password" in role.lower() or "protected" in states or "password" in name.lower()
        value = "" if protected or (role == "link" and name) else str(node.get("value") or "")
        include = bool(name or value or (ref and role not in {"group", "menubutton", "statictext", "separator"}))
        if role in {"statictext", "image"} and name in parents[-3:]:
            include = False
        if include:
            alias = f"{prefix}e{len(refs) + 1}" if ref and _REF.fullmatch(ref) else ""
            details = " ".join(part for part in (
                role, json.dumps(name, ensure_ascii=False) if name else "",
                f"[ref={alias}]" if alias else "",
            ) if part)
            if value:
                details += " value=" + json.dumps(value[:300], ensure_ascii=False)
            if states:
                details += " [" + ", ".join(states) + "]"
            lines.append("  " * min(depth, 12) + details)
            if alias:
                refs[alias] = ref
                ref_data[alias] = {"bounds": node.get("bounds"), "states": states}
        for child in node.get("children") or []:
            visit(child, depth + 1, (*parents[-3:], name) if name else parents)

    visit(tree, 0)
    text = "\n".join(lines)
    return text, refs, ref_data


def _snapshot_pages(snapshot: str) -> list[str]:
    """Divide large accessibility trees at line boundaries without dropping controls."""
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for line in snapshot.splitlines():
        line = line[:OUTPUT_LIMIT - 100]
        if current and size + len(line) + 1 > OUTPUT_LIMIT - 100:
            pages.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        pages.append("\n".join(current))
    return pages or [""]


def _rectangle(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        rect = tuple(float(value[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in rect) or rect[2] <= 1 or rect[3] <= 1:
        return None
    return rect


def _point_is_in_selected_window(window_id: str, x: int, y: int) -> bool:
    """Check real foreground and hit-test the point using physical-pixel coordinates."""
    if os.name != "nt" or not _WINDOW_ID.fullmatch(window_id):
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    old_context = user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        window = int(window_id[2:])
        hit = user32.WindowFromPoint(wintypes.POINT(x, y))
        root = user32.GetAncestor(hit, 2) if hit else None
        return bool(old_context and root and int(root) == window and int(user32.GetForegroundWindow() or 0) == window)
    finally:
        if old_context:
            user32.SetThreadDpiAwarenessContext(old_context)


def _stable_snapshot(snapshot: str) -> str:
    text = re.sub(r"\[ref=d[0-9]+e[0-9]+\]", "", snapshot)
    text = re.sub(r"\s*\[focused\]", "", text)
    text = text.replace("focused, ", "").replace(", focused", "")
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _ref_identity(snapshot: str, ref: str) -> str:
    marker = f"[ref={ref}]"
    line = next((line for line in snapshot.splitlines() if marker in line), "")
    return _stable_snapshot(line)


class DesktopSession:
    """A session whose actions target one window explicitly selected by the user."""

    is_desktop = True
    is_visible = True
    tool_name = DESKTOP_TOOL
    backend = "agent-desktop"
    max_actions = MAX_ACTIONS
    def __init__(self, window_id: str, *, headed: bool = False, manual_start: bool = False) -> None:
        if not isinstance(window_id, str) or not _WINDOW_ID.fullmatch(window_id):
            raise ValueError("Choose a window from the desktop window list.")
        self.window_id = window_id
        self.headed = bool(headed)
        self.manual_start = bool(manual_start)
        self.initial_handoff_done = False
        self.window: dict[str, Any] = {}
        self.events: list[dict[str, Any]] = []
        self.is_open = False
        self.startup_ms: int | None = None
        self.cleanup_error = ""
        self._temp: tempfile.TemporaryDirectory | None = None
        self._session_id = ""
        self._refs: dict[str, str] = {}
        self._ref_data: dict[str, dict[str, Any]] = {}
        self._last_ambiguous: tuple[str, tuple[float, float, float, float]] | None = None
        self._snapshot_number = 0
        self._snapshot_page = 0
        self._latest_snapshot = ""
        self._last_click_identity = ""
        self._last_click_result = ""
        self._attempts = 0
        self._closed = False
        self.start_url = ""
        self.origin = ""

    def check_ready(self) -> None:
        self.window = next((window for window in available_windows() if window["id"] == self.window_id), {})
        if not self.window:
            raise RuntimeError("The selected window is no longer available. Refresh the window list.")
        self.origin = self.window["title"] or self.window["app"]

    def manifest(self) -> dict[str, str]:
        return {
            "backend": self.backend,
            "window": self.window.get("title", ""),
            "app": self.window.get("app", ""),
            "input_mode": "accessibility with mouse and keyboard when needed" if self.headed else "accessibility",
        }

    def _call(self, *args: str, text_input: str | None = None) -> dict[str, Any]:
        prefix = ["--session", self._session_id] if self._session_id else []
        if self.headed and args and args[0] in {"focus-window", "mouse-click", "press"}:
            prefix.append("--headed")
        return _command([*prefix, *args], state_root=Path(self._temp.name) / "state", text_input=text_input)

    def _start(self) -> None:
        started = time.monotonic()
        self._temp = tempfile.TemporaryDirectory(prefix="sabrina-desktop-mission-")
        result = self._call("session", "start", "--no-trace", "--name", "Sabrina mission")
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "Could not create an agent-desktop session.")[:600])
        self._session_id = str(result.get("data", {}).get("session_id") or "")
        if not self._session_id:
            raise RuntimeError("agent-desktop did not return a session ID.")
        self.is_open = True
        self.startup_ms = round((time.monotonic() - started) * 1_000)

    def _snapshot(self, page: int | None = None) -> dict[str, Any]:
        current = next((window for window in available_windows() if window["id"] == self.window_id), {})
        if not current or current["process_instance"] != self.window.get("process_instance"):
            raise RuntimeError("The selected application window has closed or changed.")
        browser = self.window["app"].lower() in {"msedge.exe", "chrome.exe", "brave.exe"}
        started = time.monotonic()
        deadline = started + 25
        document = None
        while True:
            args = ["snapshot", "--app", self.window["app"], "--window-id", self.window_id,
                    "--compact", "--max-depth", "25", "--timeout-ms", "15000"]
            if self.headed:
                args.append("--include-bounds")
            result = self._call(*args)
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "Desktop snapshot failed.")[:600])
            data = result.get("data") or {}
            document = _browser_document(data.get("tree"), current["title"]) if browser else None
            if browser and document:
                doc_name = str(document.get("name") or "").replace("\u200b", "").casefold()
                title = current["title"].replace("\u200b", "").casefold()
                if doc_name not in title and time.monotonic() - started < 3:
                    current = next((window for window in available_windows() if window["id"] == self.window_id), current)
                    time.sleep(.3)
                    continue
            if not browser or document is not None:
                break
            if time.monotonic() >= deadline:
                self._refs = {}
                self._ref_data = {}
                return {"ok": False, "error": "The browser page is not yet available to accessibility tools. Try another snapshot or ask the user."}
            time.sleep(0.5)
        self._snapshot_number += 1
        snapshot, refs, ref_data = _snapshot_text(
            data.get("tree") or {}, prefix=f"d{self._snapshot_number}", browser=browser, document=document,
        )
        challenge = browser and bool(_CHALLENGE.search(snapshot))
        pages = _snapshot_pages(snapshot)
        requested = self._snapshot_page if page is None else page
        self._snapshot_page = min(requested, len(pages) - 1)
        snapshot = pages[self._snapshot_page]
        if len(pages) > 1:
            snapshot = f"[Snapshot page {self._snapshot_page + 1} of {len(pages)}; use snapshot with page=0, 1, ... to view other parts]\n" + snapshot
        self._refs = {alias: ref for alias, ref in refs.items() if f"[ref={alias}]" in snapshot}
        self._ref_data = {alias: ref_data[alias] for alias in self._refs}
        self._latest_snapshot = snapshot
        result = {"ok": bool(snapshot), "snapshot": snapshot, "complete": bool(data.get("complete"))}
        if challenge:
            result.update(
                ok=False, needs_human=True,
                error="The site needs your attention. Review the page and complete any verification.",
            )
        return result

    def run(self, raw: object, cancel_event: threading.Event) -> dict[str, Any]:
        started = time.monotonic()
        action = raw.get("action", "") if isinstance(raw, dict) else ""
        dispatched = False
        try:
            if self._closed or cancel_event.is_set() or self._attempts >= self.max_actions:
                raise ValueError("The desktop session is closed, stopped, or out of actions.")
            self._attempts += 1
            fields = {
                "snapshot": {"page"}, "click": {"ref", "repeat"}, "click_at": {"ref", "repeat"},
                "scroll_to": {"ref"}, "fill": {"ref", "value"},
                "select": {"ref", "value"}, "scroll": {"ref", "direction"},
                "press": {"key"},
            }
            if not isinstance(raw, dict) or action not in fields or set(raw) - fields[action] - {"action"}:
                raise ValueError("Use a documented desktop action and its allowed fields.")
            if not self.is_open:
                if action != "snapshot":
                    raise ValueError("Take a snapshot of the selected window first.")
                self._start()
            if action == "snapshot":
                page = raw.get("page", 0)
                if isinstance(page, bool) or not isinstance(page, int) or not 0 <= page <= 99:
                    raise ValueError("Snapshot page must be a number between 0 and 99.")
                dispatched = True
                result = self._snapshot(page)
            else:
                ref = raw.get("ref")
                if action in {"click", "click_at", "scroll_to", "fill", "select", "scroll"} and (not isinstance(ref, str) or ref not in self._refs):
                    raise ValueError("Use a ref from the latest snapshot of the selected window.")
                click_identity = _ref_identity(self._latest_snapshot, ref) if action in {"click", "click_at"} else ""
                target_rect = _rectangle(self._ref_data.get(ref, {}).get("bounds"))
                x = y = 0
                if action in {"click", "click_at"} and "repeat" in raw and not isinstance(raw["repeat"], bool):
                    raise ValueError("The repeat field must be true or false.")
                if action == "click" and target_rect and self._last_ambiguous == (click_identity, target_rect):
                    result = {
                        "ok": False, "snapshot": self._latest_snapshot,
                        "error": "This click previously failed as AMBIGUOUS_TARGET and was not delivered. "
                                 + ("Use click_at on the same visible target or choose another control." if self.headed
                                    else "Choose another control or ask the user for help."),
                    }
                    event = {"backend": self.backend, "action": action, "dispatched": False,
                             "elapsed_ms": round((time.monotonic() - started) * 1_000), **result}
                    self.events.append(event)
                    return event
                if (
                    action in {"click", "click_at"} and not raw.get("repeat")
                    and click_identity == self._last_click_identity
                    and _stable_snapshot(self._latest_snapshot) == self._last_click_result
                ):
                    result = {
                        "ok": False, "snapshot": self._latest_snapshot,
                        "error": "This click was already delivered and the screen is unchanged. "
                                 "Review the result before acting again. Use repeat=true only if the task requires another click.",
                    }
                    event = {
                        "backend": self.backend, "action": action, "dispatched": False,
                        "elapsed_ms": round((time.monotonic() - started) * 1_000), **result,
                    }
                    self.events.append(event)
                    return event
                if action == "click_at":
                    if not self.headed or not target_rect or self._last_ambiguous != (click_identity, target_rect):
                        raise ValueError("A coordinate click requires mouse mode and a preceding undelivered ambiguous click on this same visible target.")
                    states = self._ref_data[ref].get("states") or []
                    if any(str(s).lower() in {"offscreen", "invisible", "disabled"} for s in states):
                        raise ValueError("The target is offscreen or unavailable. Scroll it into view and retry the semantic click first.")
                    focus = self._call("focus-window", "--window-id", self.window_id)
                    if not focus.get("ok"):
                        raise RuntimeError("Could not focus the selected window. Take a fresh snapshot before continuing.")
                    refreshed = self._snapshot()
                    if not refreshed.get("ok"):
                        raise RuntimeError("The selected page changed or needs attention. Take a fresh snapshot before continuing.")
                    matching = [alias for alias, data in self._ref_data.items()
                                if _ref_identity(self._latest_snapshot, alias) == click_identity
                                and _rectangle(data.get("bounds")) == target_rect]
                    if len(matching) != 1:
                        raise RuntimeError("The target moved or is no longer unique. Take a fresh snapshot before continuing.")
                    x, y = round(target_rect[0] + target_rect[2] / 2), round(target_rect[1] + target_rect[3] / 2)
                    window = next((w for w in available_windows() if w["id"] == self.window_id), {})
                    rect = _rectangle(window.get("bounds"))
                    if not rect or not (rect[0] < x < rect[0] + rect[2] and rect[1] < y < rect[1] + rect[3]):
                        raise RuntimeError("The target point is outside the selected window.")
                    if not _point_is_in_selected_window(self.window_id, x, y):
                        raise RuntimeError("The selected window is not unobscured and focused at the target point.")
                if isinstance(ref, str):
                    ref = self._refs.get(ref)
                if action in {"fill", "select"}:
                    value = raw.get("value")
                    if not isinstance(value, str) or len(value) > 2_000 or "\x00" in value:
                        raise ValueError("Use a text value of at most 2,000 characters.")
                if action == "press" and (not isinstance(raw.get("key"), str) or not _KEY.fullmatch(raw["key"])):
                    raise ValueError("Use a supported key or key combination.")
                if action == "scroll" and raw.get("direction") not in {"up", "down", "left", "right"}:
                    raise ValueError("Choose up, down, left, or right.")
                args = {
                    "click": ["click", ref],
                    "click_at": ["mouse-click", f"--xy={x},{y}"],
                    "scroll_to": ["scroll-to", ref],
                    "fill": ["set-value", ref, "--stdin"],
                    "select": ["select", ref, raw.get("value", "")],
                    "scroll": ["scroll", ref, "--direction", raw.get("direction", "")],
                    "press": ["press", raw.get("key", ""), "--app", self.window["app"], "--window-id", self.window_id],
                }[action]
                dispatched = True
                response = self._call(*args, text_input=raw.get("value") if action == "fill" else None)
                result = self._snapshot()
                error = response.get("error")
                disposition = error.get("disposition") or {} if isinstance(error, dict) else {}
                if action in {"click", "click_at"} and result.get("ok") and (response.get("ok") or disposition.get("retry") != "safe"):
                    self._last_click_identity = click_identity
                    self._last_click_result = _stable_snapshot(result["snapshot"])
                self._last_ambiguous = (
                    (click_identity, target_rect)
                    if action == "click" and target_rect and isinstance(error, dict)
                    and error.get("code") == "AMBIGUOUS_TARGET" and disposition.get("delivery") == "not_delivered"
                    else None
                )
                if not response.get("ok"):
                    result["ok"] = False
                    result["error"] = json.dumps(response.get("error") or {}, ensure_ascii=False)[:1000]
                    result["error"] += " Read the new snapshot before deciding what to do next."
        except (ValueError, RuntimeError) as exc:
            result = {"ok": False, "error": str(exc)[:1000]}
            if dispatched:
                self._refs = {}
        event = {
            "backend": self.backend, "action": action, "dispatched": dispatched,
            "elapsed_ms": round((time.monotonic() - started) * 1_000), **result,
        }
        self.events.append(event)
        return event

    def observe_after_user(self, cancel_event: threading.Event) -> dict[str, Any]:
        started = time.monotonic()
        try:
            if self._closed or cancel_event.is_set():
                raise ValueError("The mission was stopped.")
            result = self._snapshot()
        except (ValueError, RuntimeError) as exc:
            result = {"ok": False, "error": str(exc)[:1000]}
        event = {
            "backend": self.backend, "action": "snapshot", "dispatched": True,
            "elapsed_ms": round((time.monotonic() - started) * 1_000),
            "user_completed_handoff": not cancel_event.is_set(), **result,
        }
        self.events.append(event)
        return event

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session_id:
            try:
                self._call("session", "end", self._session_id)
            except RuntimeError as exc:
                self.cleanup_error = str(exc)[:300]
        if self._temp is not None:
            try:
                self._temp.cleanup()
            except OSError as exc:
                self.cleanup_error = str(exc)[:300]

    def telemetry(self) -> dict[str, Any]:
        return {
            **self.manifest(), "startup_ms": self.startup_ms, "actions": len(self.events),
            "action_ms": sum(event["elapsed_ms"] for event in self.events),
            "snapshot_chars": sum(len(event.get("snapshot", "")) for event in self.events),
            "cleanup_error": self.cleanup_error,
        }
