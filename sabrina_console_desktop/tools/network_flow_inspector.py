"""API Traffic Inspector and Diagnostic Telemetry Engine for Sabrina.

Provides production-grade, read-only network traffic capture, credential redaction,
latency auditing, and durable SQLite flow logging across:
- Lab Coordinator (:8090)
- Local Ollama Daemons (:11434)
- Frontier Model APIs (OpenAI, Claude, Gemini, Groq, DeepSeek)

INVARIANTS:
1. Strict Read-Only: Never replays captured flows or mutates in-flight requests.
2. Deterministic Redaction: All authorization tokens, API keys, and session cookies
   are scrubbed before persistence or telemetry display.
3. Strict TLS: Never bypasses certificate validation.
4. Epistemological Scope: Network capture verifies transport activity, not semantic correctness.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional
import urllib.parse
import uuid

LOGGER = logging.getLogger("sabrina.traffic_inspector")
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "sabrina_assistant.sqlite3"

# Common sensitive header and key names
SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "x-goog-api-key",
    "anthropic-api-key",
    "cookie",
    "set-cookie",
}

SENSITIVE_BODY_KEYS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "secret",
    "password",
    "private_key",
    "session_token",
}

# Regex to catch API key patterns inside text
API_KEY_PATTERN = re.compile(r"\b(sk-[a-zA-Z0-9_\-]{16,}|gsk_[a-zA-Z0-9_\-]{16,}|ya29\.[a-zA-Z0-9_\-]+)\b")
SESSION_COOKIE_PATTERN = re.compile(r"\b(li_at|session|jwt|token|auth)=[A-Za-z0-9_\-\.]{6,}\b", re.IGNORECASE)


def ensure_network_flows_schema(conn: sqlite3.Connection) -> None:
    """Ensure assistant_network_flows table exists with necessary indexes."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_network_flows (
            flow_id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            worker_id TEXT DEFAULT '',
            run_id TEXT DEFAULT '',
            operation_id TEXT DEFAULT '',
            target_host TEXT NOT NULL,
            target_port INTEGER NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            status_code INTEGER DEFAULT 0,
            latency_ms REAL DEFAULT 0.0,
            queue_wait_ms REAL DEFAULT 0.0,
            ttft_ms REAL DEFAULT 0.0,
            model_requested TEXT DEFAULT '',
            think_param INTEGER DEFAULT 0,
            stream_param INTEGER DEFAULT 0,
            num_ctx INTEGER DEFAULT 0,
            request_headers_json TEXT DEFAULT '{}',
            request_body_json TEXT DEFAULT '{}',
            response_headers_json TEXT DEFAULT '{}',
            response_summary TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            created REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_worker ON assistant_network_flows(worker_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_host_path ON assistant_network_flows(target_host, path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flows_timestamp ON assistant_network_flows(timestamp)"
    )
    conn.commit()


def redact_headers(headers: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Deterministically scrub credentials and session identifiers from headers."""
    if not headers:
        return {}

    redacted = {}
    for key, value in headers.items():
        k_lower = str(key).strip().lower()
        v_str = str(value)

        if k_lower in SENSITIVE_HEADERS:
            if k_lower in ("authorization", "proxy-authorization"):
                if "bearer" in v_str.lower():
                    redacted[key] = "Bearer [REDACTED_SECRET]"
                else:
                    redacted[key] = "[REDACTED_SECRET]"
            elif k_lower in ("cookie", "set-cookie"):
                # Scrub session tokens like li_at, session, jwt
                parts = v_str.split(";")
                scrubbed_parts = []
                for p in parts:
                    if "=" in p:
                        ck, _ = p.strip().split("=", 1)
                        if any(s in ck.lower() for s in ("li_at", "session", "token", "jwt", "auth")):
                            scrubbed_parts.append(f"{ck}=[REDACTED]")
                        else:
                            scrubbed_parts.append(f"{ck}=[REDACTED_COOKIE]")
                    else:
                        scrubbed_parts.append("[REDACTED]")
                redacted[key] = "; ".join(scrubbed_parts)
            else:
                redacted[key] = "[REDACTED_SECRET]"
        else:
            redacted[key] = v_str

    return redacted


def redact_payload(payload: Any) -> Any:
    """Recursively scrub sensitive keys and token patterns from request/response payloads."""
    if isinstance(payload, dict):
        cleaned = {}
        for k, v in payload.items():
            if str(k).lower() in SENSITIVE_BODY_KEYS:
                cleaned[k] = "[REDACTED_SECRET]"
            else:
                cleaned[k] = redact_payload(v)
        return cleaned
    elif isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    elif isinstance(payload, str):
        # Scrub inline API key matches and session tokens
        text = API_KEY_PATTERN.sub("[REDACTED_KEY]", payload)
        return SESSION_COOKIE_PATTERN.sub(r"\1=[REDACTED]", text)
    return payload


class NetworkFlowInspector:
    """Read-only API and Network Flow Inspector backed by persistent SQLite."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            ensure_network_flows_schema(conn)

    def record_flow(
        self,
        target_url: str,
        method: str,
        status_code: int,
        latency_ms: float,
        worker_id: str = "",
        run_id: str = "",
        operation_id: str = "",
        queue_wait_ms: float = 0.0,
        ttft_ms: float = 0.0,
        request_headers: Optional[Dict[str, Any]] = None,
        request_body: Optional[Any] = None,
        response_headers: Optional[Dict[str, Any]] = None,
        response_body: Optional[Any] = None,
        error_message: str = "",
    ) -> str:
        """Record an inspected HTTP/API flow with strict credential redaction.

        Returns flow_id.
        """
        now = time.time()
        flow_id = f"flow_{uuid.uuid4().hex[:12]}"

        parsed = urllib.parse.urlsplit(target_url)
        target_host = parsed.hostname or "unknown"
        target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        # Extract LLM telemetry from request_body
        model_requested = ""
        think_param = 0
        stream_param = 0
        num_ctx = 0

        if isinstance(request_body, dict):
            model_body = request_body.get('payload') if isinstance(request_body.get('payload'), dict) else request_body
            model_requested = str(model_body.get("model") or "")
            stream_param = 1 if model_body.get("stream") is True else 0
            if "think" in model_body:
                think_param = 1 if model_body.get("think") in (True, 1, "true", "True") else 0
            options = model_body.get("options") or {}
            if isinstance(options, dict):
                num_ctx = int(options.get("num_ctx") or 0)
                if not think_param and "think" in options:
                    think_param = 1 if options.get("think") in (True, 1, "true", "True") else 0

        # Scrub sensitive headers and bodies
        clean_req_headers = redact_headers(request_headers)
        clean_req_body = redact_payload(request_body)
        clean_resp_headers = redact_headers(response_headers)
        clean_resp_body = redact_payload(response_body)

        # Summarize response body if long
        response_summary = ""
        if clean_resp_body:
            if isinstance(clean_resp_body, dict):
                # Extract informative telemetry keys
                eval_count = clean_resp_body.get("eval_count")
                eval_dur = clean_resp_body.get("eval_duration")
                prompt_dur = clean_resp_body.get("prompt_eval_duration")
                telemetry_info = []
                if eval_count:
                    telemetry_info.append(f"eval_tokens={eval_count}")
                if eval_dur:
                    telemetry_info.append(f"eval_ms={eval_dur / 1e6:.1f}")
                if prompt_dur:
                    telemetry_info.append(f"prompt_eval_ms={prompt_dur / 1e6:.1f}")
                
                content_preview = ""
                msg = clean_resp_body.get("message") or clean_resp_body.get("response") or ""
                if isinstance(msg, dict):
                    content_preview = str(msg.get("content", ""))[:200]
                elif isinstance(msg, str):
                    content_preview = msg[:200]

                response_summary = f"{' '.join(telemetry_info)} | Preview: {content_preview}".strip(" |")
            else:
                response_summary = str(clean_resp_body)[:300]

        with sqlite3.connect(self.db_path) as conn:
            ensure_network_flows_schema(conn)
            conn.execute(
                """
                INSERT INTO assistant_network_flows (
                    flow_id, timestamp, worker_id, run_id, operation_id,
                    target_host, target_port, method, path, status_code,
                    latency_ms, queue_wait_ms, ttft_ms, model_requested,
                    think_param, stream_param, num_ctx,
                    request_headers_json, request_body_json,
                    response_headers_json, response_summary, error_message, created
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flow_id,
                    now,
                    worker_id,
                    run_id,
                    operation_id,
                    target_host,
                    target_port,
                    method.upper(),
                    path,
                    status_code,
                    round(latency_ms, 2),
                    round(queue_wait_ms, 2),
                    round(ttft_ms, 2),
                    model_requested,
                    think_param,
                    stream_param,
                    num_ctx,
                    json.dumps(clean_req_headers),
                    json.dumps(clean_req_body),
                    json.dumps(clean_resp_headers),
                    response_summary,
                    error_message,
                    now,
                ),
            )
            conn.commit()

        return flow_id

    def inspect_flows(
        self,
        worker_id: str = "",
        model: str = "",
        target_host: str = "",
        path: str = "",
        status_code: Optional[int] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """Query captured flows with flexible diagnostic filtering."""
        query = "SELECT * FROM assistant_network_flows WHERE 1=1"
        params: List[Any] = []

        if worker_id:
            query += " AND worker_id = ?"
            params.append(worker_id)
        if model:
            query += " AND model_requested LIKE ?"
            params.append(f"%{model}%")
        if target_host:
            query += " AND target_host = ?"
            params.append(target_host)
        if path:
            query += " AND path LIKE ?"
            params.append(f"%{path}%")
        if status_code is not None:
            query += " AND status_code = ?"
            params.append(status_code)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_network_flows_schema(conn)
            rows = conn.execute(query, params).fetchall()

        results = []
        for r in rows:
            results.append({
                "flow_id": r["flow_id"],
                "timestamp": r["timestamp"],
                "worker_id": r["worker_id"],
                "run_id": r["run_id"],
                "operation_id": r["operation_id"],
                "target": f"{r['target_host']}:{r['target_port']}{r['path']}",
                "method": r["method"],
                "status_code": r["status_code"],
                "latency_ms": r["latency_ms"],
                "queue_wait_ms": r["queue_wait_ms"],
                "ttft_ms": r["ttft_ms"],
                "model_requested": r["model_requested"],
                "think_param": bool(r["think_param"]),
                "stream_param": bool(r["stream_param"]),
                "num_ctx": r["num_ctx"],
                "response_summary": r["response_summary"],
                "error": r["error_message"],
            })
        return results

    def get_flow_telemetry(self, flow_id: str) -> Dict[str, Any]:
        """Fetch detailed telemetry for a single flow with full redacted payload inspector."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_network_flows_schema(conn)
            r = conn.execute(
                "SELECT * FROM assistant_network_flows WHERE flow_id = ?", (flow_id,)
            ).fetchone()

        if not r:
            raise ValueError(f"Flow ID '{flow_id}' not found.")

        return {
            "flow_id": r["flow_id"],
            "timestamp": r["timestamp"],
            "worker_id": r["worker_id"],
            "run_id": r["run_id"],
            "operation_id": r["operation_id"],
            "target": {
                "host": r["target_host"],
                "port": r["target_port"],
                "path": r["path"],
                "method": r["method"],
            },
            "status": {
                "code": r["status_code"],
                "error": r["error_message"],
            },
            "timings_ms": {
                "total_latency": r["latency_ms"],
                "queue_wait": r["queue_wait_ms"],
                "time_to_first_token": r["ttft_ms"],
            },
            "model_parameters": {
                "model": r["model_requested"],
                "think": bool(r["think_param"]),
                "stream": bool(r["stream_param"]),
                "num_ctx": r["num_ctx"],
            },
            "sanitized_request_headers": json.loads(r["request_headers_json"]),
            "sanitized_request_body": json.loads(r["request_body_json"]),
            "sanitized_response_headers": json.loads(r["response_headers_json"]),
            "response_summary": r["response_summary"],
        }

    def audit_worker_latency(self, target_host: str = "", hours: int = 24) -> Dict[str, Any]:
        """Aggregate connection, queue wait, and generation latency statistics."""
        since = time.time() - (hours * 3600)
        query = """
            SELECT target_host, model_requested,
                   COUNT(*) as total_requests,
                   SUM(CASE WHEN status_code >= 200 AND status_code < 300 THEN 1 ELSE 0 END) as success_count,
                   SUM(CASE WHEN status_code >= 400 OR status_code = 0 THEN 1 ELSE 0 END) as error_count,
                   AVG(latency_ms) as avg_latency_ms,
                   MAX(latency_ms) as max_latency_ms,
                   AVG(queue_wait_ms) as avg_queue_ms,
                   AVG(ttft_ms) as avg_ttft_ms
            FROM assistant_network_flows
            WHERE timestamp >= ?
        """
        params: List[Any] = [since]
        if target_host:
            query += " AND target_host = ?"
            params.append(target_host)

        query += " GROUP BY target_host, model_requested ORDER BY total_requests DESC"

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_network_flows_schema(conn)
            rows = conn.execute(query, params).fetchall()

        stats = []
        for r in rows:
            total = r["total_requests"]
            success = r["success_count"]
            stats.append({
                "target_host": r["target_host"],
                "model_requested": r["model_requested"] or "non_model_endpoint",
                "total_requests": total,
                "success_rate": round((success / total * 100), 1) if total > 0 else 0.0,
                "error_count": r["error_count"],
                "avg_latency_ms": round(r["avg_latency_ms"] or 0, 1),
                "max_latency_ms": round(r["max_latency_ms"] or 0, 1),
                "avg_queue_wait_ms": round(r["avg_queue_ms"] or 0, 1),
                "avg_ttft_ms": round(r["avg_ttft_ms"] or 0, 1),
            })

        return {
            "time_window_hours": hours,
            "filter_host": target_host or "all_hosts",
            "endpoints_audited": len(stats),
            "summary": stats,
        }


# Global Singleton
FLOW_INSPECTOR = NetworkFlowInspector()


# Tool wrapper functions for tool_registry
def inspect_network_flows_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: inspect_network_flows."""
    worker_id = action.get("worker_id") or ""
    model = action.get("model") or ""
    target_host = action.get("target_host") or ""
    path = action.get("path") or ""
    status_code = action.get("status_code")
    if status_code is not None:
        try:
            status_code = int(status_code)
        except (ValueError, TypeError):
            status_code = None
    limit = int(action.get("limit") or 25)

    flows = FLOW_INSPECTOR.inspect_flows(
        worker_id=worker_id,
        model=model,
        target_host=target_host,
        path=path,
        status_code=status_code,
        limit=limit,
    )
    return json.dumps({"count": len(flows), "flows": flows}, indent=2)


def get_flow_telemetry_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: get_flow_telemetry."""
    flow_id = action.get("flow_id", "")
    if not flow_id:
        return json.dumps({"status": "error", "error": "flow_id is required."})
    try:
        telemetry = FLOW_INSPECTOR.get_flow_telemetry(flow_id)
        return json.dumps(telemetry, indent=2)
    except ValueError as e:
        return json.dumps({"status": "error", "error": str(e)})


def audit_worker_latency_tool(action: Dict[str, Any], loop: Any = None) -> str:
    """Tool wrapper: audit_worker_latency."""
    endpoint = action.get("endpoint") or action.get("target_host") or ""
    hours = int(action.get("hours") or 24)
    audit = FLOW_INSPECTOR.audit_worker_latency(target_host=endpoint, hours=hours)
    return json.dumps(audit, indent=2)
