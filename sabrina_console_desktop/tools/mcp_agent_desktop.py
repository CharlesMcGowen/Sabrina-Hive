"""Dedicated Antigravity MCP Server wrapping Sabrina's AgentDesktopBroker (1.2.0 catalog).

Enforces:
1. Read-only observation by default (`access='observe'`, `physical_input=False`).
2. Explicit per-run `physical_input=True` grant required before any hardware cursor or
   keyboard interaction (`desktop_mouse_move`, `desktop_mouse_click`, `desktop_drag`,
   `desktop_type`, `desktop_press`, etc.).
3. DPI-aware coordinate & occlusion gating with 60s screenshot TTL (`observation_id`)
   and interactive bounding-box preview (`desktop_cursor_preview`).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools import agent_desktop_registry as catalog
from tools.agent_desktop_broker import AgentDesktopBroker
from tools.sabrina_coordination import Coordination

mcp = FastMCP("sabrina-agent-desktop")

_lock = threading.RLock()
_coordination: Coordination | None = None
_broker: AgentDesktopBroker | None = None


def _get_runtime(db_path: Path | None = None) -> tuple[Coordination, AgentDesktopBroker]:
    """Return shared Coordination and AgentDesktopBroker instances."""
    global _coordination, _broker
    with _lock:
        resolved_db = db_path or Path(
            os.environ.get("SABRINA_ASSISTANT_DB", str(WORKSPACE_ROOT / "data/sabrina_assistant.sqlite3"))
        )
        if _coordination is None or Path(_coordination.store.path) != resolved_db:
            _coordination = Coordination(resolved_db)
            _broker = AgentDesktopBroker(_coordination.store)
        assert _broker is not None
        return _coordination, _broker


def _ensure_session_row(board: Coordination, run_id: str, goal: str = "Desktop Agent MCP Session") -> str:
    """Ensure `run_id` exists in `assistant_sessions` with active desktop audit retention."""
    with board.store.connect(True) as db:
        row = db.execute("SELECT id, desktop_audit_expires FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
        now = time.time()
        if row is None:
            expires = now + 7 * 86400
            db.execute(
                """INSERT INTO assistant_sessions(
                       id, task, context, state, owner, lease_until, updated,
                       created, desktop_audit_retention_days, desktop_audit_expires
                   ) VALUES (?, ?, '', 'running', 'desktop-agent', ?, ?, ?, 7, ?)""",
                (run_id, goal, now + 3600, now, now, expires),
            )
        elif not row["desktop_audit_expires"]:
            db.execute(
                "UPDATE assistant_sessions SET desktop_audit_expires=? WHERE id=?",
                (now + 7 * 86400, run_id),
            )
    return run_id


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def desktop_configure_run(
    run_id: str,
    apps: list[str] | None = None,
    access: str = "observe",
    physical_input: bool = False,
    visual_observation: bool = True,
    clipboard: bool = False,
    notifications: bool = False,
    app_closure: bool = False,
) -> dict[str, Any]:
    """Configure or inspect a run-scoped desktop session.

    Defaults to read-only observation (`access='observe'`, `physical_input=False`).
    Moving the hardware cursor or sending physical keystrokes requires an explicit
    `physical_input=True` grant and `access='autonomous'` (or `'propose'`).
    Once configured for a `run_id`, permissions and app scope are frozen for that run.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    run_id = run_id.strip()
    target_apps = apps or ["Antigravity.exe"]
    board, broker = _get_runtime()
    _ensure_session_row(board, run_id, f"Desktop session for {', '.join(target_apps)}")

    current_policy = broker.policy(run_id)
    is_new = current_policy.get("access") == "none"
    policy = broker.configure(run_id, access=access, apps=target_apps, new_run=is_new)

    permissions = {
        "physical_input": bool(physical_input),
        "visual_observation": bool(visual_observation),
        "clipboard": bool(clipboard),
        "notifications": bool(notifications),
        "app_closure": bool(app_closure),
    }
    ext_row = broker.configure_full_desktop(run_id, permissions=permissions)
    stored_perms = json.loads(ext_row["permissions"]) if ext_row and ext_row.get("permissions") else permissions

    return {
        "ok": True,
        "run_id": run_id,
        "access": policy["access"],
        "apps": policy["apps"],
        "permissions": stored_perms,
        "read_only_mode": not stored_perms.get("physical_input", False) and policy["access"] == "observe",
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False))
def desktop_observe(
    run_id: str,
    tool: str = "desktop_status",
    arguments: dict[str, Any] | None = None,
    apps: list[str] | None = None,
) -> dict[str, Any]:
    """Execute a read-only desktop observation tool (`desktop_status`, `desktop_list_apps`,
    `desktop_list_windows`, `desktop_snapshot`, `desktop_find`, `desktop_get`,
    `desktop_wait`, `desktop_screenshot`, `desktop_capabilities`).

    Automatically initializes the run in read-only observation mode (`physical_input=False`)
    if `run_id` has not been configured yet.
    """
    if tool not in catalog.READS:
        raise ValueError(f"Tool {tool!r} is not a read-only observation tool. Use desktop_act or desktop_propose.")
    board, broker = _get_runtime()
    _ensure_session_row(board, run_id)
    if broker.policy(run_id).get("access") == "none":
        inferred_app = (arguments or {}).get("app")
        default_apps = apps or ([inferred_app] if inferred_app else ["Antigravity.exe"])
        desktop_configure_run(
            run_id=run_id,
            apps=default_apps,
            access="observe",
            physical_input=False,
            visual_observation=True,
        )
    raw = broker.dispatch(run_id, tool, dict(arguments or {}))
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return {
        "ok": bool(parsed.get("ok")) if isinstance(parsed, dict) else True,
        "run_id": run_id,
        "tool": tool,
        "result": parsed,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def desktop_cursor_preview(
    run_id: str,
    observation_id: str,
    window_id: str,
    x: int,
    y: int,
    to_x: int | None = None,
    to_y: int | None = None,
) -> dict[str, Any]:
    """Validate DPI-scaled coordinates, 60s screenshot TTL, and occlusion safety without moving the cursor.

    Returns a structured bounding-box preview for Antigravity's Generative UI overlay
    showing the exact physical screen target `(screen_x, screen_y)` and whether physical
    execution is permitted on this run.
    """
    if int(x) < 0 or int(y) < 0:
        raise ValueError("Relative window coordinates (x, y) must be non-negative integers")
    _, broker = _get_runtime()
    ext = broker._extended_runtime()
    row = ext.check(run_id)
    perms = json.loads(row["permissions"])
    saved = ext.load_artifact(run_id, observation_id, "screenshot")
    if saved["window_id"] != window_id:
        raise ValueError(f"Screenshot {observation_id!r} belongs to window {saved['window_id']!r}, not {window_id!r}")

    metadata = json.loads(saved["metadata"])
    geom = metadata.get("geometry") or {"x": 0, "y": 0, "width": 1920, "height": 1080, "dpi": 96}
    width = int(geom.get("width", 1920))
    height = int(geom.get("height", 1080))
    if not (0 <= int(x) < width and 0 <= int(y) < height):
        raise ValueError(f"Point ({x}, {y}) is outside target window dimensions ({width}x{height})")

    screen_x = int(geom.get("x", 0)) + int(x)
    screen_y = int(geom.get("y", 0)) + int(y)
    points = [(screen_x, screen_y)]
    drag_target = None
    if to_x is not None and to_y is not None:
        if not (0 <= int(to_x) < width and 0 <= int(to_y) < height):
            raise ValueError(f"Drag target ({to_x}, {to_y}) is outside target window dimensions ({width}x{height})")
        drag_screen_x = int(geom.get("x", 0)) + int(to_x)
        drag_screen_y = int(geom.get("y", 0)) + int(to_y)
        points.append((drag_screen_x, drag_screen_y))
        drag_target = {
            "window_x": int(to_x),
            "window_y": int(to_y),
            "screen_x": drag_screen_x,
            "screen_y": drag_screen_y,
        }

    return {
        "ok": True,
        "run_id": run_id,
        "observation_id": observation_id,
        "window_id": window_id,
        "app": saved["app"],
        "window_geometry": geom,
        "target": {
            "window_x": int(x),
            "window_y": int(y),
            "screen_x": screen_x,
            "screen_y": screen_y,
            "bounding_box": {
                "left": max(0, int(x) - 12),
                "top": max(0, int(y) - 12),
                "width": 24,
                "height": 24,
            },
        },
        "drag_target": drag_target,
        "physical_input_granted": bool(perms.get("physical_input")),
        "ready_for_execution": bool(perms.get("physical_input")) and broker.policy(run_id).get("access") == "autonomous",
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
def desktop_act(
    run_id: str,
    tool: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute a bounded desktop or cursor mutation (`desktop_click`, `desktop_set_value`,
    `desktop_type`, `desktop_press`, `desktop_mouse_move`, `desktop_mouse_click`,
    `desktop_drag`, `desktop_focus_window`, `desktop_restore_window`).

    Fails closed if:
    - The run is in read-only `observe` mode.
    - The tool requires `physical_input` and the run was not explicitly granted `physical_input=True`.
    - Pointer coordinates lack a fresh (<60s) `observation_id` screenshot or target an occluded/protected control.
    """
    _, broker = _get_runtime()
    entry = catalog.validate(tool, dict(arguments or {}))
    policy = broker.policy(run_id)
    if policy.get("access") != "autonomous":
        raise ValueError(
            f"Run {run_id!r} has access={policy.get('access')!r}. "
            "Desktop mutations require access='autonomous' (or use desktop_propose in 'propose' mode)."
        )
    ext = broker._extended_runtime()
    row = ext.row(run_id)
    if entry.get("physical") or tool in catalog.PHYSICAL:
        perms = json.loads(row["permissions"]) if row and row.get("permissions") else {}
        if not perms.get("physical_input"):
            raise ValueError(
                f"Physical input gate blocked {tool!r}: run {run_id!r} is in read-only observation mode "
                "(physical_input=False). Re-configure a fresh run with physical_input=True to move the cursor."
            )

    raw = broker.dispatch(run_id, tool, dict(arguments or {}))
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return {
        "ok": bool(parsed.get("ok")) if isinstance(parsed, dict) else False,
        "run_id": run_id,
        "tool": tool,
        "result": parsed,
    }


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
def desktop_propose(
    run_id: str,
    tool: str,
    arguments: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Propose a desktop action or catalog command in `propose` mode for operator review."""
    _, broker = _get_runtime()
    if tool in catalog.LEGACY_ACTIONS or tool == "desktop_notify_user":
        proposal_args = dict(arguments or {})
        proposal_args["tool"] = tool
        proposal_args["reason"] = reason
        res = broker.propose(run_id, proposal_args)
        return {"ok": True, "run_id": run_id, "proposal": res}
    raw = broker.dispatch(
        run_id,
        "desktop_propose_command",
        {"tool": tool, "arguments": dict(arguments or {}), "reason": reason},
    )
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return {"ok": bool(parsed.get("ok")), "run_id": run_id, "proposal": parsed}


if __name__ == "__main__":
    mcp.run(transport="stdio")
