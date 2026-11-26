"""Catalog and construct the interactive tools a mission can explicitly select."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sabrina_console_desktop.agent_browser_service import BrowserSession
from sabrina_console_desktop.agent_browser_service.fleet import FleetBrowserSession
from sabrina_console_desktop.agent_browser_service.visible import VisibleBrowserSession
from sabrina_console_desktop.agent_desktop_service import DesktopSession


@dataclass(frozen=True)
class InteractiveBackend:
    id: str
    label: str
    kind: str
    session: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label, "kind": self.kind, "session": self.session}


BACKENDS: tuple[InteractiveBackend, ...] = (
    InteractiveBackend("agent-desktop", "Vercel desktop", "desktop", "selected window"),
    InteractiveBackend("visible-edge", "Your Microsoft Edge", "visible", "your existing browser profile"),
    InteractiveBackend("visible-chrome", "Your Google Chrome", "visible", "your existing browser profile"),
    InteractiveBackend("ego-fleet", "Ego browser fleet (temporary)", "fleet", "temporary isolated fleet context"),
    InteractiveBackend("agent-browser", "Vercel agent-browser", "container", "temporary container"),
    InteractiveBackend("playwright-cli", "Microsoft Playwright CLI", "container", "temporary container"),
)
_BY_ID = {backend.id: backend for backend in BACKENDS}
_SESSION_TYPES = {"desktop": DesktopSession, "visible": VisibleBrowserSession,
                  "fleet": FleetBrowserSession, "container": BrowserSession}


def list_interactive_backends() -> list[dict[str, str]]:
    """Describe selectable backends; readiness is checked when one is selected."""
    return [backend.to_dict() for backend in BACKENDS]


def create_interactive_session(
    backend: str, start_url: str = "", *, desktop_window_id: str = "",
    headed: bool = False, manual_start: bool = False,
    session_types: dict[str, Any] | None = None,
) -> Any:
    spec = _BY_ID.get(backend)
    if spec is None:
        raise ValueError("Choose a supported browser or desktop backend.")
    types = {**_SESSION_TYPES, **(session_types or {})}
    session_type = types[spec.kind]
    if spec.kind == "desktop":
        return session_type(desktop_window_id, headed=bool(headed), manual_start=bool(manual_start))
    if spec.kind == "visible":
        return session_type(backend, start_url, manual_start=bool(manual_start))
    if spec.kind == "fleet":
        return session_type(start_url)
    return session_type(backend, start_url)
