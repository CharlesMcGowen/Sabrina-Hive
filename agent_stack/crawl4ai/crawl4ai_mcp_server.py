#!/usr/bin/env python3
"""Zero-dependency MCP (Model Context Protocol) stdio server for self-hosted Crawl4AI.

Exposes local, headless-browser web crawling to clean Markdown via Crawl4AI
for Living Archive agents, Antigravity, Claude Desktop, or any MCP-compatible client.

Environment variables:
    CRAWL4AI_URL: Base URL of your Crawl4AI instance (default: http://127.0.0.1:11235)
    CRAWL4AI_API_TOKEN: Optional bearer token (auto-loaded from sibling .env if unset)
    CRAWL4AI_TIMEOUT_S: HTTP request timeout in seconds (default: 60)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

SERVER_INFO = {
    "name": "livingarchive-crawl4ai-mcp",
    "version": "1.0.0",
}
PROTOCOL_VERSION = "2024-11-05"

DEFAULT_CRAWL4AI_URL = os.environ.get("CRAWL4AI_URL", "http://127.0.0.1:11235").rstrip("/")
DEFAULT_TIMEOUT_S = float(os.environ.get("CRAWL4AI_TIMEOUT_S", "60"))


def _load_api_token() -> str:
    token = os.environ.get("CRAWL4AI_API_TOKEN", "").strip()
    if token:
        return token
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.is_file():
        try:
            for raw_line in env_file.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip() == "CRAWL4AI_API_TOKEN":
                    return val.strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


DEFAULT_API_TOKEN = _load_api_token()

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "crawl4ai_markdown",
        "description": (
            "Crawl a public http/https URL using self-hosted Crawl4AI (headless browser) "
            "and return clean LLM-ready Markdown."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Public http:// or https:// URL to crawl",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum Markdown characters to return (500-100000, default 15000)",
                    "default": 15000,
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "crawl4ai_health",
        "description": "Check reachability and health status of the self-hosted Crawl4AI instance.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _extract_markdown(result_item: Dict[str, Any]) -> str:
    md = result_item.get("markdown")
    if isinstance(md, str):
        return md
    if isinstance(md, dict):
        for key in ("fit_markdown", "raw_markdown", "markdown_with_citations"):
            val = md.get(key)
            if isinstance(val, str) and val.strip():
                return val
    for fallback in ("markdown_v2", "fit_markdown", "raw_markdown", "cleaned_html"):
        val = result_item.get(fallback)
        if isinstance(val, str) and val.strip():
            return val
        if isinstance(val, dict):
            for key in ("fit_markdown", "raw_markdown"):
                inner = val.get(key)
                if isinstance(inner, str) and inner.strip():
                    return inner
    return ""


def crawl4ai_markdown(
    url: str,
    *,
    max_chars: int = 15000,
    base_url: str = DEFAULT_CRAWL4AI_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http:// and https:// URLs are allowed, got: {parsed.scheme!r}")

    endpoint = f"{base_url.rstrip('/')}/crawl"
    body = json.dumps({"urls": [url]}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "livingarchive-crawl4ai-mcp/1.0",
    }
    if DEFAULT_API_TOKEN:
        headers["Authorization"] = f"Bearer {DEFAULT_API_TOKEN}"
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    results = payload.get("results")
    if isinstance(results, list) and results:
        item = results[0]
    elif isinstance(payload, dict) and ("markdown" in payload or "success" in payload):
        item = payload
    else:
        raise RuntimeError(f"Unexpected Crawl4AI response shape: {list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}")

    if item.get("success") is False:
        raise RuntimeError(f"Crawl4AI failed to crawl {url}: {item.get('error_message') or item.get('error') or 'unknown error'}")

    markdown = _extract_markdown(item).strip()
    limit = max(500, min(int(max_chars), 100000))
    truncated = len(markdown) > limit

    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "url": item.get("url") or url,
        "title": metadata.get("title", ""),
        "status_code": item.get("status_code"),
        "truncated": truncated,
        "chars": min(len(markdown), limit),
        "markdown": markdown[:limit],
    }


def crawl4ai_health(
    *,
    base_url: str = DEFAULT_CRAWL4AI_URL,
    timeout_s: float = 10.0,
) -> Dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/health"
    req = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "User-Agent": "livingarchive-crawl4ai-mcp/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        status_code = resp.getcode()
        raw = resp.read().decode("utf-8", errors="replace").strip()
    try:
        details: Any = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        details = {"raw": raw}

    return {
        "status": "ok" if status_code == 200 else "degraded",
        "http_status": status_code,
        "base_url": base_url,
        "details": details,
    }


def _handle_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "crawl4ai_markdown":
        data = crawl4ai_markdown(
            str(args["url"]),
            max_chars=int(args.get("max_chars", 15000)),
        )
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}

    if name == "crawl4ai_health":
        data = crawl4ai_health()
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}

    raise ValueError(f"Unknown tool: {name}")


def _write_message(msg: Dict[str, Any]) -> None:
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
