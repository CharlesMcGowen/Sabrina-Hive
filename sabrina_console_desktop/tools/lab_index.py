"""Lab and Project Discovery Index for Sabrina.

Maintains a queryable index of:
- Lab host projects and repositories (/home/ego/...)
- Running services and Docker containers
- Component files, databases, and network ports
- Local desktop integration points and artifacts

Prevents prompt bloat by allowing Sabrina to dynamically discover assets
instead of requiring them to be embedded in her initial prompt.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from tools.tool_registry import TOOL_REGISTRY

logger = logging.getLogger("sabrina.lab_index")

# Curated ground-truth index of known lab infrastructure
LAB_INDEX: Dict[str, Dict[str, Any]] = {
    "kraken-explorer": {
        "name": "kraken-explorer",
        "category": "project",
        "host": "ego.local",
        "user": "ego",
        "root_path": "/home/ego/kraken-explorer",
        "description": "Cryptocurrency trading, live market data feed, candle recorder, and forecasting platform.",
        "containers": [
            {
                "name": "candle-recorder",
                "image": "kraken-explorer:latest",
                "role": "Continuous candle recorder daemon querying Kraken WebSocket/REST feeds",
                "internal_ports": [8000, 8010],
                "expected_state": "running",
            },
            {
                "name": "kraken-explorer",
                "image": "kraken-explorer:latest",
                "role": "FastAPI/Uvicorn web dashboard and UI server",
                "internal_ports": [8000, 8010],
                "expected_state": "dormant",
            },
        ],
        "key_components": [
            "candle_recorder.py: Main entrypoint for active candle-recorder container",
            "kraken_client.py: Unified Kraken REST API client",
            "kraken_ws_client.py: Real-time WebSocket streaming feed client",
            "kraken_spot_client.py: Spot market order execution and balance manager",
            "kraken_futures_client.py: Futures derivatives client",
            "kraken_mcp_server.py: MCP server interface for model tool calls",
            "kraken_gan_forecaster.py: Time-series forecasting model",
            "static/krakencast.js: Web front-end dashboard interface",
        ],
        "databases": [
            "/home/ego/kraken-explorer/kraken_trades.db: Historical trade and fill records",
            "/home/ego/kraken-explorer/kraken_bot.db: Strategy states and orders",
            "/home/ego/kraken-explorer/kraken_data.db: Candle and tick data cache",
        ],
        "logs": [
            "/home/ego/kraken-explorer/kraken_engine.log",
            "/home/ego/kraken-explorer/kraken_bot_8012.log",
            "/home/ego/kraken-explorer/kraken_bot_err.log",
        ],
        "local_workspace_references": [
            "C:\\Users\\bcmcg\\Desktop\\KE\\APIexplorerkey.txt: Kraken API credentials reference",
        ],
    },
    "recon-stack": {
        "name": "recon-stack",
        "category": "project",
        "host": "ego.local",
        "user": "ego",
        "root_path": "/home/ego",
        "description": "Autonomous security reconnaissance and data streaming pipeline.",
        "containers": [
            {"name": "recon-django", "role": "Central reconnaissance API and web dashboard", "port": 9000},
            {"name": "recon-kafka", "role": "Event stream broker", "ports": [9092, 9093]},
            {"name": "recon-zookeeper", "role": "Kafka coordinator", "port": 2181},
            {"name": "recon-qdrant", "role": "Vector database for target embeddings", "ports": [6333, 6334]},
            {"name": "recon-redis", "role": "In-memory cache and task queue", "port": 6379},
        ],
    },
    "juiceshop": {
        "name": "juiceshop",
        "category": "service",
        "host": "ego.local",
        "user": "ego",
        "description": "OWASP Juice Shop target application for security testing and agent evaluation.",
        "containers": [
            {"name": "juiceshop", "image": "bkimminich/juice-shop", "port": 3000, "status": "active"},
        ],
    },
    "sql-playground": {
        "name": "sql-playground",
        "category": "service",
        "host": "ego.local",
        "user": "ego",
        "description": "SQL injection testing sandbox.",
        "containers": [
            {"name": "sql-playground", "image": "sqlplayground", "port": 8020, "status": "active"},
        ],
    },
    "sabrina-social": {
        "name": "sabrina-social",
        "category": "service",
        "host": "ego.local",
        "user": "ego",
        "description": "Sabrina social media automation and API supervisor.",
        "services": [
            {"name": "sabrina_social.api", "port": 8087},
            {"name": "sabrina_social.research_api", "port": 8088},
            {"name": "tools.lab_control", "port": 8090},
        ],
    },
}


def search_lab_index(query: str = "", category: str = "all") -> List[Dict[str, Any]]:
    """Search known lab index by keyword and category."""
    query_terms = [t.lower() for t in query.split() if len(t) > 1]
    matches = []

    for key, item in LAB_INDEX.items():
        if category != "all" and item.get("category") != category:
            continue

        item_str = json.dumps(item).lower()
        if not query_terms:
            matches.append(item)
            continue

        score = sum(1 for term in query_terms if term in item_str)
        if score > 0:
            matches.append(item)

    return matches


def discover_lab(query: str = "", category: str = "all") -> List[Dict[str, Any]]:
    """Discover lab assets, projects, containers, and services."""
    return search_lab_index(query, category=category)


def _handle_discover_lab(action: Dict[str, Any], loop: Any = None) -> str:
    query = str(action.get("query") or action.get("search") or "").strip()
    category = str(action.get("category") or "all").strip().lower()

    results = search_lab_index(query, category)
    if not results:
        available_keys = list(LAB_INDEX.keys())
        return f"No lab projects or services matched query '{query}'. Known lab assets: {', '.join(available_keys)}"
    return json.dumps(results, indent=2, ensure_ascii=False)


TOOL_REGISTRY.register(
    name="discover_lab",
    handler=_handle_discover_lab,
    description="Discover lab projects, services, Docker containers, databases, and code paths across the compute mesh.",
    parameters={
        "query": "search query (e.g. 'kraken', 'crypto', 'recon', 'juiceshop')",
        "category": "optional category ('all', 'project', 'service')",
    },
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)
