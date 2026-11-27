#!/usr/bin/env python3
"""
Zero-dependency Python stdlib MCP stdio server (JSON-RPC 2.0, protocol 2024-11-05)
for LivingArchive HITL Approval Gate.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {
    "name": "livingarchive-hitl-gate",
    "version": "1.0.0",
}
BASE_URL = os.environ.get("HITL_GATE_URL", "http://127.0.0.1:8890").rstrip("/")

TOOLS = [
    {
        "name": "request_human_approval",
        "description": (
            "Submit an action to the LivingArchive HITL Approval Gate for human review. "
            "Optionally wait up to wait_seconds for a decision."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Name or ID of the requesting agent.",
                },
                "action": {
                    "type": "string",
                    "description": "Short description of the action requiring approval.",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Risk level of the requested action.",
                    "default": "medium",
                },
                "payload": {
                    "type": "object",
                    "description": "Structured context/arguments for the human reviewer.",
                    "default": {},
                },
                "wait_seconds": {
                    "type": "integer",
                    "description": "Seconds to poll for a decision before returning (0 = return immediately).",
                    "default": 0,
                    "minimum": 0,
                    "maximum": 300,
                },
            },
            "required": ["agent", "action", "risk_level", "payload"],
        },
    },
    {
        "name": "check_approval_status",
        "description": "Check the current status (pending, approved, rejected, expired) of an approval request by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {
                    "type": "string",
                    "description": "The approval request ID (e.g. req_...).",
                }
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "list_pending_approvals",
        "description": "List all currently pending human approval requests.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _http_json(method: str, path: str, body=None, timeout: int = 15) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Connection failed to {url}: {exc.reason}") from exc


def handle_tool_call(name: str, arguments: dict) -> dict:
    if name == "request_human_approval":
        agent = str(arguments.get("agent", "")).strip()
        action = str(arguments.get("action", "")).strip()
        risk_level = str(arguments.get("risk_level", "medium")).strip().lower()
        payload = arguments.get("payload", {})
        wait_seconds = int(arguments.get("wait_seconds", 0) or 0)

        if not agent or not action:
            raise ValueError("'agent' and 'action' are required")

        created = _http_json(
            "POST",
            "/api/requests",
            {
                "agent": agent,
                "action": action,
                "risk_level": risk_level,
                "payload": payload,
            },
        )

        if wait_seconds > 0:
            req_id = created["id"]
            deadline = time.monotonic() + min(wait_seconds, 300)
            latest = created
            while time.monotonic() < deadline:
                time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
                latest = _http_json(
                    "GET", f"/api/requests/{urllib.parse.quote(req_id)}"
                )
                if latest.get("status") != "pending":
                    return latest
            return latest
        return created

    if name == "check_approval_status":
        req_id = str(arguments.get("request_id", "")).strip()
        if not req_id:
            raise ValueError("'request_id' is required")
        return _http_json("GET", f"/api/requests/{urllib.parse.quote(req_id)}")

    if name == "list_pending_approvals":
        return _http_json("GET", "/api/requests?status=pending")

    raise ValueError(f"Unknown tool: {name}")


def send_response(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def process_message(msg: dict) -> None:
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # Notifications have no id and must not receive a response.
    if req_id is None:
        return

    if method == "initialize":
        send_response(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            }
        )
        return

    if method == "ping":
        send_response({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return

    if method == "tools/list":
        send_response(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            }
        )
        return

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            result = handle_tool_call(tool_name, arguments)
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(result, indent=2, ensure_ascii=False),
                            }
                        ],
                        "isError": False,
                    },
                }
            )
        except Exception as exc:
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {exc}"}],
                        "isError": True,
                    },
                }
            )
        return

    send_response(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    )


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            send_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
            )
            continue
        process_message(msg)


if __name__ == "__main__":
    main()
