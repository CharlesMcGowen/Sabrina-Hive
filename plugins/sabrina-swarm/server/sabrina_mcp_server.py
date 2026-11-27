#!/usr/bin/env python3
"""Zero-dependency MCP stdio server bridging Antigravity to Sabrina Swarm & Desktop Console.

Interfaces with:
- Sabrina Swarm API (:8095) for blackboard, worker locks, A2A links, and swarm command turns.
- Sabrina Control API (:8087) for desktop health, scheduler state, and subsystem status.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List

SERVER_INFO = {
    "name": "sabrina-swarm-bridge",
    "version": "1.0.0",
}
PROTOCOL_VERSION = "2024-11-05"

SWARM_API_BASE = os.environ.get("SABRINA_SWARM_API_BASE", "http://127.0.0.1:8095").rstrip("/")
CONTROL_API_BASE = os.environ.get("SABRINA_SWARM_CONTROL_API_BASE", "http://127.0.0.1:8087").rstrip("/")
REQUEST_TIMEOUT_S = float(os.environ.get("SABRINA_REQUEST_TIMEOUT_S", "5.0"))


def _http_request(method: str, url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    req_body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {
        "User-Agent": "Antigravity-Sabrina-Bridge/1.0",
        "Accept": "application/json",
    }
    if req_body is not None:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=req_body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {"ok": True, "status": resp.status}
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
            return {"ok": False, "http_status": exc.code, "error": parsed}
        except Exception:
            return {"ok": False, "http_status": exc.code, "error": err_body}
    except urllib.error.URLError as exc:
        return {
            "ok": False,
            "error": "connection_error",
            "message": f"Could not reach {url}: {exc.reason}. Ensure Sabrina Console/Swarm is running.",
        }
    except Exception as exc:
        return {"ok": False, "error": "unknown_error", "message": str(exc)}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "sabrina_get_swarm_state",
        "description": "Fetch live state of Sabrina Swarm (:8095), including active workers, lock table, and mission topology.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "sabrina_read_blackboard",
        "description": "Read coordination messages, agent @mentions, and artifacts posted on the Sabrina Swarm blackboard (:8095).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {
                    "type": "integer",
                    "description": "Sequence number to read messages after (default 0 for all).",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of messages to return (default 50).",
                    "default": 50,
                },
            },
        },
    },
    {
        "name": "sabrina_post_board_message",
        "description": "Post a status update, decision, artifact note, or @mention assignment to the Sabrina Swarm blackboard (:8095).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Sender agent identifier (e.g. 'antigravity', 'execution_specialist', 'coordinator').",
                    "default": "antigravity",
                },
                "text": {
                    "type": "string",
                    "description": "Message text to broadcast to the swarm. Can include @mentions like @coordinator or @architect.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of tags (e.g. ['task', 'implementation', 'status']).",
                },
            },
            "required": ["text"],
        },
    },
    {
        "name": "sabrina_update_worker",
        "description": "Update worker state, heartbeat note, role, or claim/release file locks in Sabrina Swarm (:8095).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Worker identifier (e.g. 'antigravity').",
                    "default": "antigravity",
                },
                "state": {
                    "type": "string",
                    "description": "Worker state: 'IDLE', 'WORKING', 'WAITING', 'FAILED', 'COMPLETED'.",
                },
                "role": {
                    "type": "string",
                    "description": "Worker role: 'coordinator', 'architect', 'worker', 'verifier', 'execution_specialist'.",
                },
                "locks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of resource or file paths to acquire exclusively.",
                },
                "note": {
                    "type": "string",
                    "description": "Brief current status note.",
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "sabrina_get_a2a_links",
        "description": "List all configured agent-to-agent (A2A) communication channels and loop routing rules in Sabrina Swarm (:8095).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "sabrina_run_swarm_turn",
        "description": "Dispatch a request to the Sabrina Swarm Command router (:8095) across multi-agent subtasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "High-level goal or instruction for the swarm.",
                },
                "subtasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of decomposed subtasks.",
                },
                "complexity_hint": {
                    "type": "string",
                    "description": "Optional complexity hint: 'trivial', 'simple', 'moderate', 'complex'.",
                },
            },
            "required": ["request"],
        },
    },
    {
        "name": "sabrina_check_desktop_health",
        "description": "Check Sabrina Desktop Console health, scheduled jobs, and control plane status (:8087).",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _handle_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "sabrina_get_swarm_state":
        res = _http_request("GET", f"{SWARM_API_BASE}/api/state")
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}

    if name == "sabrina_read_blackboard":
        since = int(args.get("since", 0))
        limit = int(args.get("limit", 50))
        res = _http_request("GET", f"{SWARM_API_BASE}/api/board?since={since}&limit={limit}")
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}

    if name == "sabrina_post_board_message":
        agent = str(args.get("agent") or "antigravity").strip()
        text = str(args.get("text", "")).strip()
        tags = args.get("tags")
        payload: dict[str, Any] = {"agent": agent, "text": text}
        if tags is not None and isinstance(tags, list):
            payload["tags"] = [str(t) for t in tags]
        res = _http_request("POST", f"{SWARM_API_BASE}/api/board", payload)
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}

    if name == "sabrina_update_worker":
        payload = {"name": str(args.get("name", "antigravity")).strip()}
        for key in ("state", "role", "note", "locks"):
            if key in args and args[key] is not None:
                payload[key] = args[key]
        res = _http_request("POST", f"{SWARM_API_BASE}/api/worker", payload)
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}

    if name == "sabrina_get_a2a_links":
        res = _http_request("GET", f"{SWARM_API_BASE}/api/a2a-links")
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}

    if name == "sabrina_run_swarm_turn":
        payload = {
            "request": str(args.get("request", "")).strip(),
        }
        if "subtasks" in args and isinstance(args["subtasks"], list):
            payload["subtasks"] = [str(s) for s in args["subtasks"]]
        if "complexity_hint" in args and args["complexity_hint"]:
            payload["complexity_hint"] = str(args["complexity_hint"])
        res = _http_request("POST", f"{SWARM_API_BASE}/api/swarm-command/turn", payload)
        return {"content": [{"type": "text", "text": json.dumps(res, indent=2, ensure_ascii=False)}]}

    if name == "sabrina_check_desktop_health":
        health = _http_request("GET", f"{CONTROL_API_BASE}/health")
        schedules = _http_request("GET", f"{CONTROL_API_BASE}/api/schedule?limit=10")
        combined = {
            "control_api": CONTROL_API_BASE,
            "swarm_api": SWARM_API_BASE,
            "health": health,
            "schedules": schedules,
        }
        return {"content": [{"type": "text", "text": json.dumps(combined, indent=2, ensure_ascii=False)}]}

    raise ValueError(f"Unknown tool: {name}")


def _write_message(msg: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        if req_id is None:
            # Notifications require no response
            continue

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = _handle_tool_call(str(params.get("name", "")), dict(params.get("arguments") or {}))
            elif method == "ping":
                result = {}
            else:
                _write_message(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }
                )
                continue

            _write_message({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception as exc:
            _write_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": f"Error: {exc}"}],
                    },
                }
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
