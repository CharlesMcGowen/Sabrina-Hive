#!/usr/bin/env python3
"""Zero-dependency MCP (Model Context Protocol) stdio server for self-hosted SearXNG.

Exposes local, free ($0.00/query) web, developer docs, arXiv, and page-reading tools
to Living Archive agents, Antigravity, Claude Desktop, or any MCP-compatible client.

Environment variables:
    SEARXNG_URL: Base URL of your SearXNG instance (default: http://127.0.0.1:8888)
    SEARXNG_TIMEOUT_S: HTTP request timeout in seconds (default: 15)
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List

SERVER_INFO = {
    "name": "livingarchive-searxng-mcp",
    "version": "1.1.0",
}
PROTOCOL_VERSION = "2024-11-05"

DEFAULT_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
DEFAULT_TIMEOUT_S = float(os.environ.get("SEARXNG_TIMEOUT_S", "15"))

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "searxng_search",
        "description": (
            "Search the web locally via self-hosted SearXNG aggregating Google, Brave, "
            "DuckDuckGo, Mojeek, Qwant, Wikipedia, and arXiv at $0.00/query."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "categories": {
                    "type": "string",
                    "description": "Comma-separated SearXNG categories (e.g. general, science, it, news)",
                    "default": "general",
                },
                "engines": {
                    "type": "string",
                    "description": "Optional comma-separated engines (e.g. google,brave,duckduckgo,arxiv,github,stackoverflow)",
                    "default": "",
                },
                "time_range": {
                    "type": "string",
                    "description": "Optional time filter: day, week, month, year, or empty",
                    "default": "",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (1-30, default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "searxng_dev_search",
        "description": (
            "Search developer & code sources via self-hosted SearXNG (GitHub, StackOverflow, "
            "MDN, PyPI, Crates.io, Docker Hub, Hugging Face, Hacker News, Arch Wiki)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Developer, library, API, or error query"},
                "engines": {
                    "type": "string",
                    "description": "Optional subset of IT engines (default: all enabled IT engines)",
                    "default": "",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (1-30, default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "searxng_arxiv_search",
        "description": "Search scientific papers via self-hosted SearXNG (arXiv, Semantic Scholar, Crossref).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Research topic or paper query"},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum papers to return (1-30, default 10)",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "searxng_fetch_page",
        "description": (
            "Fetch a public http/https URL and extract clean readable text (stripping scripts/styles) "
            "so research agents can read full documentation or papers returned by SearXNG."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Public http:// or https:// URL to fetch"},
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (500-40000, default 12000)",
                    "default": 12000,
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "searxng_health",
        "description": "Check reachability and JSON API status of the self-hosted SearXNG instance.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


class _TextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "iframe"}
    _BLOCK_TAGS = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "li", "tr", "br", "pre", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: List[str] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
        elif t == "title":
            self._in_title = True
        elif t in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif t == "title":
            self._in_title = False
        elif t in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title += data
        self._chunks.append(data)

    def get_text(self) -> str:
        raw = html.unescape("".join(self._chunks))
        lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in raw.splitlines()]
        cleaned = "\n".join(ln for ln in lines if ln)
        return re.sub(r"\n{3,}", "\n\n", cleaned)


def query_searxng(
    query: str,
    *,
    categories: str = "general",
    engines: str = "",
    time_range: str = "",
    max_results: int = 10,
    base_url: str = DEFAULT_SEARXNG_URL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    params: Dict[str, str] = {
        "q": query,
        "format": "json",
        "language": "en",
    }
    if categories:
        params["categories"] = categories
    if engines:
        params["engines"] = engines
    if time_range in ("day", "week", "month", "year"):
        params["time_range"] = time_range

    url = f"{base_url.rstrip('/')}/search?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "livingarchive-searxng-mcp/1.1",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    limit = max(1, min(int(max_results), 30))
    results = []
    for item in payload.get("results", [])[:limit]:
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
                "engines": item.get("engines", []),
                "published_date": item.get("publishedDate"),
                "score": item.get("score"),
            }
        )

    return {
        "query": payload.get("query", query),
        "number_of_results": len(results),
        "unresponsive_engines": payload.get("unresponsive_engines", []),
        "results": results,
    }


def fetch_page_text(url: str, *, max_chars: int = 12000, timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Only http:// and https:// URLs are allowed, got: {parsed.scheme!r}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "livingarchive-searxng-mcp/1.1 (+https://local.livingarchive)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        content_type = resp.headers.get("Content-Type", "")
        raw_bytes = resp.read(2 * 1024 * 1024)  # 2 MB cap
    text_raw = raw_bytes.decode("utf-8", errors="replace")

    if "html" in content_type.lower() or "<html" in text_raw[:500].lower():
        parser = _TextExtractor()
        parser.feed(text_raw)
        title = parser.title.strip()
        text = parser.get_text()
    else:
        title = ""
        text = text_raw

    limit = max(500, min(int(max_chars), 40000))
    truncated = len(text) > limit
    return {
        "url": url,
        "title": title,
        "content_type": content_type,
        "truncated": truncated,
        "chars": min(len(text), limit),
        "text": text[:limit],
    }


def _handle_tool_call(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    if name == "searxng_search":
        data = query_searxng(
            str(args["query"]),
            categories=str(args.get("categories", "general")),
            engines=str(args.get("engines", "")),
            time_range=str(args.get("time_range", "")),
            max_results=int(args.get("max_results", 10)),
        )
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}

    if name == "searxng_dev_search":
        data = query_searxng(
            str(args["query"]),
            categories="it",
            engines=str(args.get("engines", "")),
            max_results=int(args.get("max_results", 10)),
        )
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}

    if name == "searxng_arxiv_search":
        data = query_searxng(
            str(args["query"]),
            categories="science",
            engines="arxiv,semantic scholar,crossref",
            max_results=int(args.get("max_results", 10)),
        )
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}

    if name == "searxng_fetch_page":
        data = fetch_page_text(
            str(args["url"]),
            max_chars=int(args.get("max_chars", 12000)),
        )
        return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}

    if name == "searxng_health":
        data = query_searxng("python", categories="it", max_results=2)
        health = {
            "status": "ok",
            "base_url": DEFAULT_SEARXNG_URL,
            "sample_results": data["number_of_results"],
            "unresponsive_engines": data["unresponsive_engines"],
        }
        return {"content": [{"type": "text", "text": json.dumps(health, indent=2)}]}

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
