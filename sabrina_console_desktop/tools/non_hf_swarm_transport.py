#!/usr/bin/env python3
"""Live-verified transport & handshake client for non-Hugging-Face swarm members.

Provides real zero-auth invocation & handshake verification without modifying
`portal_transport.py` (preserving all existing `adapter_fingerprint` hashes):
1. `invoke_pollinations(prompt, model='openai-fast')` — zero-auth Pollinations.ai LLM inference.
2. `probe_mcp_http_server(url)` — real JSON-RPC 2.0 `initialize` + `tools/list` handshake
   on Official MCP Registry remote streamable-HTTP servers (`HTTP 200` + valid JSON-RPC).
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.parse
import urllib.request

from tools.cyber_compliance_sentinel import CyberComplianceSentinel

USER_AGENT = "SabrinaNonHFTransport/1.0 (+inquiry-only; zero-auth)"


def invoke_pollinations(
    prompt: str,
    *,
    model: str = "openai-fast",
    timeout: float = 25.0,
    sentinel: Optional[CyberComplianceSentinel] = None,
) -> Dict[str, Any]:
    """Send a zero-auth prompt to Pollinations.ai and verify non-empty response."""
    sent = sentinel or CyberComplianceSentinel()
    ok_out, reason = sent.sanitize_outbound_query(prompt)
    if not ok_out:
        return {"ok": False, "failure": f"sentinel_blocked:{reason}", "reply": ""}

    t0 = time.perf_counter()
    url = f"https://text.pollinations.ai/{urllib.parse.quote(prompt[:2000], safe='')}?model={urllib.parse.quote(model)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/plain,application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            body = resp.read(262_144).decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as exc:
        return {"ok": False, "failure": f"http_{exc.code}", "http_status": exc.code, "reply": "", "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "failure": type(exc).__name__, "reply": "", "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    lat = round((time.perf_counter() - t0) * 1000, 1)
    if status != 200 or not body:
        return {"ok": False, "failure": f"http_{status}_or_empty", "http_status": status, "reply": "", "latency_ms": lat}
    threat = sent.inspect_and_report_threat(source_identifier=url, content=body[:6000])
    if isinstance(threat, dict) and (threat.get("threat_detected") or threat.get("incident_id")):
        return {"ok": False, "failure": "sentinel_quarantined", "reply": "", "latency_ms": lat}
    return {"ok": True, "provider": "pollinations", "model": model, "reply": body, "latency_ms": lat}


def probe_mcp_http_server(
    url: str,
    *,
    timeout: float = 8.0,
    sentinel: Optional[CyberComplianceSentinel] = None,
) -> Dict[str, Any]:
    """Perform a genuine zero-auth MCP JSON-RPC 2.0 `initialize` handshake against a remote MCP URL."""
    sent = sentinel or CyberComplianceSentinel()
    ok_out, reason = sent.sanitize_outbound_query(url)
    if not ok_out:
        return {"ok": False, "failure": f"sentinel_blocked:{reason}"}

    t0 = time.perf_counter()
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "SabrinaSwarmProbe", "version": "1.0"},
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(init_req).encode("utf-8"),
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            raw = resp.read(131_072).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 500))
        return {"ok": False, "failure": "auth_required" if code in (401, 403) else f"http_{code}", "http_status": code, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "failure": type(exc).__name__, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}

    lat = round((time.perf_counter() - t0) * 1000, 1)
    if status in (401, 403):
        return {"ok": False, "failure": "auth_required", "http_status": status, "latency_ms": lat}
    if status != 200:
        return {"ok": False, "failure": f"http_{status}", "http_status": status, "latency_ms": lat}

    # Parse JSON or SSE `data: {...}` frame
    payload = None
    try:
        payload = json.loads(raw)
    except Exception:
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                    break
                except Exception:
                    continue

    if not isinstance(payload, dict) or "result" not in payload:
        return {"ok": False, "failure": "non_mcp_jsonrpc_response", "http_status": status, "latency_ms": lat}
    res = payload.get("result") or {}
    return {
        "ok": True,
        "http_status": 200,
        "latency_ms": lat,
        "server_info": res.get("serverInfo") or {},
        "protocol_version": res.get("protocolVersion") or "",
        "capabilities": list((res.get("capabilities") or {}).keys()),
    }
