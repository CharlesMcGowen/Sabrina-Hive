#!/usr/bin/env python3
"""Autonomous Internet Resource Discovery, Indexing & Recursive Self-Improvement Engine.

Strictly scoped to zero-authentication public internet resources across three channels:
1. `public_api`     — Open REST / OpenAPI / JSON data endpoints (APIs.guru OpenAPI specs + public feeds).
2. `dataset`        — Open unauthenticated datasets & benchmarks (Hugging Face Hub `gated=False` + Dataset Viewer).
3. `doc_skill_repo` — Documentation, Agent `SKILL.md`, and MCP/SDK repositories (GitHub + npm/PyPI fallback).

Designed for scheduled periodic sweeps (e.g., every 4 hours via `sabrina_social.scheduler`) rather than
unbounded background loops. Self-improves recursively on every sweep via:
- UCB1 multi-armed bandit arm selection across `(channel, query)` pairs,
- Automatic `cursor_page` pagination advancement so repeat pulls explore deeper pages instead of re-scraping Page 1,
- Exhaustion retirement (`zero_yield_streak >= 2`) so dry queries stop wasting probe budget,
- Gap-driven + local LLM (`dolphin-mistral:7b` / `granite3.1-dense:8b` when idle, deterministic fallback when busy)
  child query synthesis (`generation + 1`),
- Closed-loop usage reward feedback (`use_resource`) and periodic staleness re-verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.cyber_compliance_sentinel import CyberComplianceSentinel

DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
DEFAULT_SOCIAL_DB_PATH = ROOT / "data" / "social" / "social.sqlite3"
DEFAULT_RSI_STATE_PATH = ROOT / "data" / "rsi_policy" / "universal_rsi_state.json"

VALID_CHANNELS: Tuple[str, ...] = ("public_api", "dataset", "doc_skill_repo", "free_compute_sandbox")
USER_AGENT = "SabrinaResourceIndexer/1.0 (+inquiry-only; zero-auth public catalog)"
MAX_PROBE_BYTES = 524_288        # 512 KB cap per live resource verification fetch
MAX_MANIFEST_BYTES = 24_000_000  # 24 MB cap for 24h-cached OpenAPI directory manifest (APIs.guru is ~9 MB)

# Malicious instruction override patterns rejected from untrusted documentation
UNTRUSTED_DOC_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(?:your\s+)?system\s+prompt", re.IGNORECASE),
    re.compile(r"curl\s+[^\n]*\|\s*(?:ba)?sh", re.IGNORECASE),
    re.compile(r"wget\s+[^\n]*\|\s*(?:ba)?sh", re.IGNORECASE),
]

# Seed zero-auth public JSON/XML APIs verified live before admission (never trusted without HTTP 200 probe)
CURATED_PUBLIC_API_SEEDS: List[Dict[str, Any]] = [
    {
        "title": "CISA Known Exploited Vulnerabilities (KEV) Catalog JSON",
        "url": "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
        "endpoint_url": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        "tags": ["cybersecurity", "cve", "vulnerability", "cisa", "threat-intel"],
        "description": "Official CISA authoritative JSON feed of actively exploited CVEs, remediation due dates, and vendor/product metadata.",
    },
    {
        "title": "OSV.dev Open Source Vulnerability Query API",
        "url": "https://osv.dev/",
        "endpoint_url": "https://api.osv.dev/v1/vulns/GHSA-jfh8-c2jp-5v3q",
        "tags": ["cybersecurity", "vulnerability", "osv", "dependencies", "sbom"],
        "description": "Zero-auth REST JSON API for querying open-source package vulnerabilities across PyPI, npm, crates.io, Go, and Linux distributions.",
    },
    {
        "title": "Hugging Face Daily AI Research Papers JSON API",
        "url": "https://huggingface.co/papers",
        "endpoint_url": "https://huggingface.co/api/daily_papers?limit=3",
        "tags": ["ai", "research", "papers", "llm", "machine-learning"],
        "description": "Unauthenticated JSON feed of daily peer-curated ML/AI research papers with abstracts, arXiv IDs, and GitHub links.",
    },
    {
        "title": "arXiv Scientific Preprint Search API",
        "url": "https://info.arxiv.org/help/api/index.html",
        "endpoint_url": "https://export.arxiv.org/api/query?search_query=all:agentic+security&start=0&max_results=2",
        "tags": ["research", "arxiv", "science", "math", "cs", "papers"],
        "description": "Open Atom/XML query interface over arXiv preprints across computer science, mathematics, quantitative finance, and physics.",
    },
    {
        "title": "PyPI Package Release & Dependency Metadata JSON API",
        "url": "https://warehouse.pypa.io/api-reference/json.html",
        "endpoint_url": "https://pypi.org/pypi/pytest/json",
        "tags": ["python", "packaging", "dependencies", "releases", "sdk"],
        "description": "Zero-auth JSON endpoint returning Python package versions, digests, requires_dist dependencies, and project URLs.",
    },
    {
        "title": "Crossref Scholarly Works & DOI Metadata REST API",
        "url": "https://api.crossref.org/swagger-ui/index.html",
        "endpoint_url": "https://api.crossref.org/works?query=program+synthesis&rows=2",
        "tags": ["citations", "doi", "research", "metadata", "academic"],
        "description": "Public scholarly metadata API providing structured JSON bibliographic records, citations, licenses, and abstracts.",
    },
    {
        "title": "Open-Meteo Global Weather & Forecast JSON API",
        "url": "https://open-meteo.com/en/docs",
        "endpoint_url": "https://api.open-meteo.com/v1/forecast?latitude=30.27&longitude=-97.74&current_weather=true",
        "tags": ["weather", "timeseries", "forecast", "geospatial", "climate"],
        "description": "Zero-API-key high-resolution weather and historical climate time-series JSON API.",
    },
    {
        "title": "USGS Real-Time Earthquake GeoJSON Feed",
        "url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php",
        "endpoint_url": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson",
        "tags": ["geospatial", "seismic", "geojson", "timeseries", "science"],
        "description": "Real-time unauthenticated GeoJSON feed of global seismic events and sensor telemetry.",
    },
    {
        "title": "DeFiLlama Protocol TVL & Market Metrics JSON API",
        "url": "https://defillama.com/docs/api",
        "endpoint_url": "https://api.llama.fi/v2/chains",
        "tags": ["crypto", "market-data", "finance", "timeseries", "defi"],
        "description": "Open zero-auth quantitative market and chain TVL metrics API.",
    },
]

CURATED_FREE_COMPUTE_SEEDS: List[Dict[str, Any]] = [
    {
        "title": "Godbolt Compiler Explorer Public Compilation & Execution API",
        "url": "https://godbolt.org/",
        "endpoint_url": "https://godbolt.org/api/languages",
        "tags": ["sandbox", "compiler", "execution", "cpp", "rust", "python", "free-compute"],
        "description": "Zero-auth public compilation and execution sandbox API supporting C++, Rust, Python, Go, and 60+ languages.",
    },
    {
        "title": "Wandbox Public Multi-Language Code Execution Sandbox API",
        "url": "https://wandbox.org/",
        "endpoint_url": "https://wandbox.org/api/list.json",
        "tags": ["sandbox", "execution", "compiler", "runtime", "free-compute"],
        "description": "Zero-auth REST JSON code execution sandbox supporting 30+ compilers and interpreters.",
    },
    {
        "title": "Piston Public Sandboxed Code Execution Runtimes API",
        "url": "https://emkc.org/api/v2/piston/runtimes",
        "endpoint_url": "https://emkc.org/api/v2/piston/runtimes",
        "tags": ["sandbox", "execution", "python", "node", "runtime", "free-compute"],
        "description": "Unauthenticated multi-language isolated code execution runtime catalog and REST execution engine.",
    },
]

SEED_BANDIT_ARMS: List[Tuple[str, str]] = [
    ("public_api", "security vulnerability cve"),
    ("public_api", "science data research open"),
    ("public_api", "finance market crypto metrics"),
    ("public_api", "developer tools code analysis"),
    ("dataset", "cybersecurity vulnerability code"),
    ("dataset", "python code repair benchmark"),
    ("dataset", "math reasoning theorem"),
    ("dataset", "financial timeseries market"),
    ("doc_skill_repo", "SKILL.md agent workflow"),
    ("doc_skill_repo", "mcp-server model context protocol"),
    ("doc_skill_repo", "python security static analysis"),
    ("doc_skill_repo", "openapi client generator cli"),
    ("free_compute_sandbox", "code execution sandbox compiler"),
    ("free_compute_sandbox", "zero-gpu inference gradio space"),
    ("free_compute_sandbox", "python notebook wasm runtime"),
    ("free_compute_sandbox", "open llm playground compute"),
    ("dataset", "enwik8 enwik9 compression"),
    ("doc_skill_repo", "nncp cmix lossless compression"),
    ("public_api", "arxiv neural compression bpb"),
    ("free_compute_sandbox", "pollinations anonymous llm docker"),
    ("free_compute_sandbox", "free tier cloud gpu serverless"),
    ("dataset", "zenodo openml science benchmark"),
    ("doc_skill_repo", "mcp server registry remote"),
    ("doc_skill_repo", "twitter x-skills SKILL.md growth"),
    ("doc_skill_repo", "social media creator agent skill"),
]

MUTATION_MODIFIERS: Dict[str, List[str]] = {
    "public_api": ["openapi", "metrics", "feed", "telemetry", "registry", "analytics", "gov", "directory"],
    "dataset": ["instruction", "evaluation", "traces", "parquet", "synthetic", "classification", "corpus", "qa"],
    "doc_skill_repo": ["automation", "sdk", "cli", "audit", "sandbox", "guide", "reference", "cookbook"],
    "free_compute_sandbox": ["sandbox", "runtime", "gpu", "wasm", "inference", "notebook", "compiler", "serverless"],
}


HttpResponseTuple = Tuple[int, Dict[str, str], bytes, str]
HttpGetFn = Callable[[str, float, int], HttpResponseTuple]


def _default_http_get(url: str, timeout: float = 8.0, max_bytes: int = MAX_PROBE_BYTES) -> HttpResponseTuple:
    """Perform a clean, unauthenticated HTTP GET with strict byte cap and login-redirect guard."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/markdown, text/plain, */*;q=0.8",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = int(getattr(resp, "status", 200))
        headers = {k.lower(): v for k, v in resp.headers.items()}
        final_url = str(resp.geturl() or url)
        body = resp.read(max_bytes + 1)
        if len(body) > max_bytes:
            body = body[:max_bytes]
        return status, headers, body, final_url


def _slug_id(kind: str, url: str) -> str:
    digest = hashlib.sha256(url.strip().lower().encode("utf-8")).hexdigest()[:14]
    parsed = urllib.parse.urlsplit(url)
    host_slug = re.sub(r"[^a-z0-9]+", "-", (parsed.netloc or "web").lower()).strip("-")[:24]
    return f"res_{kind[:4]}_{host_slug}_{digest}"


class InternetResourceIndexer:
    """Scheduled, recursively self-improving indexer for Public APIs, Datasets & Doc/Skill Repos."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        social_db_path: Optional[Path | str] = None,
        rsi_state_path: Optional[Path | str] = None,
        sentinel: Optional[CyberComplianceSentinel] = None,
        http_get_fn: Optional[HttpGetFn] = None,
        llm_mutator_fn: Optional[Callable[[str], Optional[List[Dict[str, str]]]]] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.social_db_path = Path(social_db_path) if social_db_path else DEFAULT_SOCIAL_DB_PATH
        self.rsi_state_path = Path(rsi_state_path) if rsi_state_path else DEFAULT_RSI_STATE_PATH
        self.sentinel = sentinel or CyberComplianceSentinel()
        self.http_get: HttpGetFn = http_get_fn or _default_http_get
        self.llm_mutator_fn = llm_mutator_fn
        self._last_host_call: Dict[str, float] = {}
        self._fts_enabled = False
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS internet_resource_index (
                    resource_id TEXT PRIMARY KEY,
                    resource_kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL UNIQUE,
                    endpoint_or_raw_url TEXT NOT NULL,
                    domain_tags_json TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    auth_verified_zero INTEGER NOT NULL DEFAULT 1,
                    http_status INTEGER NOT NULL DEFAULT 200,
                    latency_ms REAL NOT NULL DEFAULT 0.0,
                    content_type TEXT NOT NULL DEFAULT '',
                    schema_summary_json TEXT NOT NULL DEFAULT '{}',
                    usage_snippet TEXT NOT NULL DEFAULT '',
                    quality_score REAL NOT NULL DEFAULT 50.0,
                    usage_count INTEGER NOT NULL DEFAULT 0,
                    usage_successes INTEGER NOT NULL DEFAULT 0,
                    discovered_by_arm TEXT NOT NULL DEFAULT '',
                    discovered_by_query TEXT NOT NULL DEFAULT '',
                    content_sha256 TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    first_indexed REAL NOT NULL,
                    last_verified REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_iri_kind_status
                    ON internet_resource_index(resource_kind, status, quality_score DESC);
                CREATE INDEX IF NOT EXISTS idx_iri_verified
                    ON internet_resource_index(status, last_verified ASC);

                CREATE TABLE IF NOT EXISTS internet_discovery_bandit_arms (
                    arm_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    query TEXT NOT NULL,
                    cursor_page INTEGER NOT NULL DEFAULT 1,
                    pulls INTEGER NOT NULL DEFAULT 0,
                    candidates_probed INTEGER NOT NULL DEFAULT 0,
                    novel_indexed INTEGER NOT NULL DEFAULT 0,
                    cumulative_reward REAL NOT NULL DEFAULT 0.0,
                    mean_reward REAL NOT NULL DEFAULT 0.5,
                    zero_yield_streak INTEGER NOT NULL DEFAULT 0,
                    generation INTEGER NOT NULL DEFAULT 0,
                    parent_query TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(channel, query)
                );

                CREATE TABLE IF NOT EXISTS internet_sweep_runs (
                    sweep_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    started_at REAL NOT NULL,
                    duration_sec REAL NOT NULL,
                    arms_pulled_json TEXT NOT NULL,
                    probed_count INTEGER NOT NULL,
                    newly_indexed_count INTEGER NOT NULL,
                    updated_existing_count INTEGER NOT NULL,
                    reverified_count INTEGER NOT NULL,
                    stale_demoted_count INTEGER NOT NULL,
                    evolved_queries_json TEXT NOT NULL,
                    total_active_index_size INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS internet_catalog_cache (
                    cache_key TEXT PRIMARY KEY,
                    fetched_at REAL NOT NULL,
                    payload_json TEXT NOT NULL
                );
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS internet_resource_fts USING fts5(
                        resource_id UNINDEXED,
                        resource_kind,
                        title,
                        description,
                        domain_tags,
                        usage_snippet
                    )
                    """
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

            now = time.time()
            for ch, q in SEED_BANDIT_ARMS:
                arm_id = f"arm_{ch[:4]}_{hashlib.sha256(f'{ch}:{q}'.encode()).hexdigest()[:10]}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO internet_discovery_bandit_arms (
                        arm_id, channel, query, cursor_page, pulls, candidates_probed,
                        novel_indexed, cumulative_reward, mean_reward, zero_yield_streak,
                        generation, parent_query, active, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 0, 0, 0, 0.0, 0.5, 0, 0, '', 1, ?, ?)
                    """,
                    (arm_id, ch, q, now, now),
                )

    # ------------------------------------------------------------------
    # Polite Zero-Auth Transport + CyberComplianceSentinel Verification
    # ------------------------------------------------------------------
    def _polite_get(
        self,
        url: str,
        timeout: float = 8.0,
        max_bytes: int = MAX_PROBE_BYTES,
    ) -> Tuple[bool, Dict[str, Any]]:
        """Execute zero-auth GET with host pacing, login/auth rejection, and threat sentinel scan."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return False, {"reason": "invalid_scheme_or_host"}

        # Never index interactive Gradio Space chat portals here (strictly APIs, datasets, docs)
        host = parsed.netloc.lower()
        if host.endswith(".hf.space"):
            return False, {"reason": "excluded_interactive_chat_portal"}

        ok_out, out_msg = self.sentinel.sanitize_outbound_query(url)
        if not ok_out:
            return False, {"reason": f"sentinel_outbound_blocked:{out_msg}"}

        min_gap = 1.2 if "api.github.com" in host else 0.15
        prev = self._last_host_call.get(host, 0.0)
        wait_s = min_gap - (time.monotonic() - prev)
        if wait_s > 0 and self.http_get is _default_http_get:
            time.sleep(wait_s)
        self._last_host_call[host] = time.monotonic()

        t0 = time.perf_counter()
        try:
            status, headers, raw_bytes, final_url = self.http_get(url, timeout, max_bytes)
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 500))
            if code in (401, 403):
                return False, {"reason": f"http_{code}_auth_or_rate_limited", "http_status": code}
            return False, {"reason": f"http_{code}", "http_status": code}
        except Exception as exc:
            return False, {"reason": f"network_error:{type(exc).__name__}"}

        latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        if status in (401, 403) or "www-authenticate" in headers:
            return False, {"reason": "auth_required", "http_status": status}
        if status < 200 or status >= 300:
            return False, {"reason": f"http_{status}", "http_status": status}

        final_lower = final_url.lower()
        if any(tok in final_lower for tok in ("/login", "/signin", "oauth/authorize", "/sso/")):
            return False, {"reason": "redirected_to_login", "http_status": status}

        text = raw_bytes.decode("utf-8", errors="replace")
        head_lower = text[:1600].lower()
        if "please log in to continue" in head_lower or "authentication required" in head_lower:
            return False, {"reason": "body_demands_auth", "http_status": status}

        threat = self.sentinel.inspect_and_report_threat(
            source_identifier=url,
            content=text[:8000],
            context={"component": "internet_resource_indexer"},
        )
        if threat is not None:
            return False, {"reason": "quarantined_by_cyber_sentinel", "http_status": status, "quarantined": True}

        return True, {
            "http_status": status,
            "latency_ms": latency_ms,
            "content_type": headers.get("content-type", "text/plain").split(";")[0].strip(),
            "headers": headers,
            "text": text,
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "final_url": final_url,
        }

    # ------------------------------------------------------------------
    # Channel 1: Zero-Auth Public APIs (APIs.guru OpenAPI + Curated Feeds)
    # ------------------------------------------------------------------
    def _load_apis_guru_catalog(self) -> Dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fetched_at, payload_json FROM internet_catalog_cache WHERE cache_key='apis_guru_v2'"
            ).fetchone()
            if row and (now - float(row["fetched_at"])) < 86400:
                try:
                    return json.loads(row["payload_json"])
                except Exception:
                    pass

        ok, res = self._polite_get("https://api.apis.guru/v2/list.json", timeout=12.0, max_bytes=MAX_MANIFEST_BYTES)
        if not ok:
            return {}
        try:
            full_map = json.loads(res["text"])
            if not isinstance(full_map, dict):
                return {}
            # Store compact projection so cache stays lightweight (<500 KB)
            compact: Dict[str, Any] = {}
            for api_key, entry in list(full_map.items())[:1500]:
                if not isinstance(entry, dict):
                    continue
                pref = entry.get("preferred")
                ver_obj = (entry.get("versions") or {}).get(pref) or {}
                info = ver_obj.get("info") or {}
                swagger_url = ver_obj.get("swaggerUrl") or ""
                if not swagger_url:
                    continue
                compact[api_key] = {
                    "title": str(info.get("title") or api_key)[:140],
                    "description": re.sub(r"\s+", " ", str(info.get("description") or ""))[:280],
                    "categories": list(info.get("x-apisguru-categories") or []),
                    "swaggerUrl": swagger_url,
                    "externalDocs": (ver_obj.get("externalDocs") or {}).get("url") or swagger_url,
                }
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO internet_catalog_cache(cache_key, fetched_at, payload_json) VALUES (?, ?, ?)",
                    ("apis_guru_v2", now, json.dumps(compact)),
                )
            return compact
        except Exception:
            return {}

    @staticmethod
    def _openapi_has_public_get(spec: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """Check whether an OpenAPI/Swagger spec exposes unauthenticated GET operations."""
        paths = spec.get("paths")
        if not isinstance(paths, dict) or not paths:
            return False, []
        global_sec = spec.get("security")
        has_global_lock = isinstance(global_sec, list) and len(global_sec) > 0 and any(bool(s) for s in global_sec)
        public_gets: List[str] = []
        for path_name, ops in paths.items():
            if not isinstance(ops, dict) or "get" not in ops:
                continue
            get_op = ops.get("get") or {}
            op_sec = get_op.get("security") if isinstance(get_op, dict) else None
            if op_sec == [] or (op_sec is None and not has_global_lock):
                public_gets.append(str(path_name))
        return len(public_gets) > 0, public_gets[:8]

    def _discover_public_apis(
        self, query: str, cursor_page: int, max_probes: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        terms = [t.lower() for t in re.findall(r"[a-z0-9_-]{2,}", query.lower())]
        candidates: List[Dict[str, Any]] = []

        # 1. Match curated zero-auth seed feeds on page 1
        if cursor_page == 1:
            for seed in CURATED_PUBLIC_API_SEEDS:
                hay = f"{seed['title']} {seed['description']} {' '.join(seed['tags'])}".lower()
                if not terms or any(t in hay for t in terms):
                    candidates.append({
                        "kind": "seed_json_api",
                        "title": seed["title"],
                        "url": seed["url"],
                        "probe_url": seed["endpoint_url"],
                        "tags": seed["tags"],
                        "description": seed["description"],
                    })

        # 2. Rank APIs.guru OpenAPI entries by term overlap and paginate via cursor_page
        catalog = self._load_apis_guru_catalog()
        scored_guru: List[Tuple[int, str, Dict[str, Any]]] = []
        for api_key, meta in catalog.items():
            hay = f"{api_key} {meta.get('title','')} {meta.get('description','')} {' '.join(meta.get('categories',[]))}".lower()
            hits = sum(1 for t in terms if t in hay) if terms else 1
            if hits > 0:
                scored_guru.append((hits, api_key, meta))
        scored_guru.sort(key=lambda x: (-x[0], x[1]))

        start_idx = max(0, (cursor_page - 1) * max_probes)
        for _, api_key, meta in scored_guru[start_idx : start_idx + max_probes * 2]:
            candidates.append({
                "kind": "openapi_spec",
                "title": f"{meta['title']} ({api_key})",
                "url": meta["swaggerUrl"],
                "probe_url": meta["swaggerUrl"],
                "tags": ["openapi", "public-api"] + [str(c) for c in meta.get("categories", [])[:4]],
                "description": meta.get("description") or f"OpenAPI specification for {api_key}",
            })

        # 2b. Also discover health-scored zero-auth APIs from FreePublicAPIs.com
        if len(candidates) < max_probes * 2:
            ok_fpa, fpa_res = self._polite_get("https://www.freepublicapis.com/api/apis?limit=50", timeout=8.0)
            if ok_fpa:
                try:
                    fpa_rows = json.loads(fpa_res["text"])
                except Exception:
                    fpa_rows = []
                if isinstance(fpa_rows, list):
                    fpa_matches = []
                    for row in fpa_rows:
                        if not isinstance(row, dict) or int(row.get("health") or 0) < 70:
                            continue
                        doc_u = str(row.get("documentation") or "").strip()
                        if not doc_u.startswith("http"):
                            continue
                        hay = f"{row.get('title', '')} {row.get('description', '')}".lower()
                        hits = sum(1 for t in terms if t in hay) if terms else 1
                        fpa_matches.append((hits, int(row.get("health") or 0), row))
                    fpa_matches.sort(key=lambda x: (-x[0], -x[1]))
                    for _, _, row in fpa_matches[start_idx : start_idx + max(3, max_probes // 2)]:
                        doc_u = str(row["documentation"])
                        candidates.append({
                            "kind": "openapi_spec" if "openapi" in doc_u.lower() or "swagger" in doc_u.lower() else "seed_json_api",
                            "title": f"FreePublicAPI: {row.get('title', 'API')}",
                            "url": str(row.get("source") or doc_u),
                            "probe_url": doc_u,
                            "tags": ["freepublicapis", "public-api"],
                            "description": re.sub(r"\s+", " ", str(row.get("description") or ""))[:300],
                        })

        probed = 0
        verified_items: List[Dict[str, Any]] = []
        for cand in candidates:
            if probed >= max_probes:
                break
            probed += 1
            ok, res = self._polite_get(cand["probe_url"], timeout=8.0)
            if not ok:
                continue

            schema_summary: Dict[str, Any] = {}
            if cand["kind"] == "openapi_spec":
                try:
                    spec = json.loads(res["text"])
                except Exception:
                    continue
                if not isinstance(spec, dict):
                    continue
                is_open, pub_paths = self._openapi_has_public_get(spec)
                if not is_open:
                    continue
                servers = spec.get("servers") or []
                base_server = (
                    servers[0].get("url")
                    if servers and isinstance(servers[0], dict)
                    else (f"https://{spec.get('host', '')}{spec.get('basePath', '')}" if spec.get("host") else "")
                )
                schema_summary = {
                    "format": "openapi_json",
                    "openapi_version": spec.get("openapi") or spec.get("swagger") or "3.0",
                    "base_url": base_server,
                    "public_get_paths": pub_paths,
                }
                snippet = (
                    f"import json, urllib.request\n"
                    f"spec = json.loads(urllib.request.urlopen({cand['probe_url']!r}, timeout=8).read())\n"
                    f"# Base URL: {base_server} | Public GET paths: {pub_paths[:3]}"
                )
            else:
                # Seed JSON / Atom feed verification
                ctype = res["content_type"].lower()
                txt = res["text"].strip()
                if "json" in ctype or txt.startswith(("{", "[")):
                    try:
                        parsed_json = json.loads(txt)
                        top_keys = list(parsed_json.keys())[:8] if isinstance(parsed_json, dict) else [f"list[{len(parsed_json)}]"]
                    except Exception:
                        continue
                    schema_summary = {"format": "rest_json", "top_keys": top_keys}
                elif "xml" in ctype or "atom" in ctype or txt.startswith("<?xml"):
                    schema_summary = {"format": "atom_xml", "root_preview": txt[:120]}
                else:
                    continue
                snippet = (
                    f"import urllib.request\n"
                    f"req = urllib.request.Request({cand['probe_url']!r}, headers={{'User-Agent': {USER_AGENT!r}}})\n"
                    f"data = urllib.request.urlopen(req, timeout=8).read().decode('utf-8')"
                )

            quality = round(
                min(
                    98.0,
                    68.0
                    + (12.0 if schema_summary.get("format") in ("rest_json", "openapi_json") else 6.0)
                    + max(0.0, 12.0 - res["latency_ms"] / 250.0)
                    + min(6.0, len(cand.get("description", "")) / 40.0),
                ),
                2,
            )
            verified_items.append({
                "resource_kind": "public_api",
                "title": cand["title"][:160],
                "url": cand["probe_url"] if cand["kind"] == "openapi_spec" else cand["url"],
                "endpoint_or_raw_url": cand["probe_url"],
                "domain_tags": cand["tags"],
                "description": cand["description"][:400],
                "http_status": res["http_status"],
                "latency_ms": res["latency_ms"],
                "content_type": res["content_type"],
                "schema_summary": schema_summary,
                "usage_snippet": snippet,
                "quality_score": quality,
                "content_sha256": res["sha256"],
            })
        return probed, verified_items

    # ------------------------------------------------------------------
    # Channel 2: Zero-Auth Public Datasets (Hugging Face Hub + Viewer)
    # ------------------------------------------------------------------
    def _discover_datasets(
        self, query: str, cursor_page: int, max_probes: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        limit_fetch = min(60, max_probes * max(2, cursor_page + 1))
        tokens = [t.lower() for t in re.findall(r"[a-z0-9_-]{3,}", query.lower())]
        search_terms = [query] + [t for t in tokens if t != query.lower()]

        items: List[Any] = []
        for s_term in search_terms[:3]:
            q_enc = urllib.parse.quote_plus(s_term)
            search_url = f"https://huggingface.co/api/datasets?search={q_enc}&sort=downloads&direction=-1&limit={limit_fetch}"
            ok, res = self._polite_get(search_url, timeout=10.0)
            if not ok:
                continue
            try:
                parsed_list = json.loads(res["text"])
            except Exception:
                continue
            if isinstance(parsed_list, list) and len(parsed_list) > 0:
                items = parsed_list
                break

        if not items:
            return 1, []

        ungated = [
            it for it in items
            if isinstance(it, dict)
            and it.get("id")
            and not it.get("private")
            and it.get("gated") in (False, None)
        ]
        if len(tokens) > 1:
            ungated.sort(
                key=lambda d: (
                    -sum(
                        1
                        for t in tokens
                        if t in f"{d.get('id','')} {' '.join(str(x) for x in (d.get('tags') or []))} {d.get('description','')}".lower()
                    ),
                    -int(d.get("downloads") or 0),
                )
            )
        start_idx = max(0, (cursor_page - 1) * max_probes)
        window = ungated[start_idx : start_idx + max_probes]

        probed = 0
        verified: List[Dict[str, Any]] = []
        for ds in window:
            ds_id = str(ds["id"])
            probed += 1
            parquet_endpoint = f"https://huggingface.co/api/datasets/{ds_id}/parquet"
            ok_v, res_v = self._polite_get(parquet_endpoint, timeout=8.0)
            parquet_manifest: Dict[str, Any] = {}
            if ok_v:
                try:
                    parquet_manifest = json.loads(res_v["text"])
                except Exception:
                    parquet_manifest = {}
            if not ok_v or not isinstance(parquet_manifest, dict) or len(parquet_manifest) == 0:
                continue

            configs = list(parquet_manifest.keys())[:6]
            first_cfg = parquet_manifest.get(configs[0]) if configs else {}
            splits = list(first_cfg.keys())[:6] if isinstance(first_cfg, dict) else []
            tags = [str(t) for t in (ds.get("tags") or []) if isinstance(t, str) and len(t) < 42][:8]
            downloads = int(ds.get("downloads") or 0)
            likes = int(ds.get("likes") or 0)
            desc = re.sub(r"\s+", " ", str(ds.get("description") or f"Zero-auth Hugging Face Parquet dataset {ds_id}")).strip()[:400]
            snippet = (
                f"import json, urllib.request\n"
                f"# Zero-auth Parquet manifest for {ds_id} (configs={configs}, splits={splits})\n"
                f"parquet_urls = json.loads(urllib.request.urlopen({parquet_endpoint!r}, timeout=10).read())"
            )
            quality = round(
                min(
                    98.0,
                    64.0
                    + min(18.0, math.log10(max(1, downloads) + 1) * 3.5)
                    + min(8.0, math.log10(max(1, likes) + 1) * 3.0)
                    + (6.0 if splits else 2.0),
                ),
                2,
            )
            verified.append({
                "resource_kind": "dataset",
                "title": f"Dataset: {ds_id}",
                "url": f"https://huggingface.co/datasets/{ds_id}",
                "endpoint_or_raw_url": parquet_endpoint,
                "domain_tags": ["dataset", "parquet"] + tags[:6],
                "description": desc,
                "http_status": res_v["http_status"],
                "latency_ms": res_v["latency_ms"],
                "content_type": "application/json",
                "schema_summary": {
                    "dataset_id": ds_id,
                    "downloads": downloads,
                    "likes": likes,
                    "configs": configs,
                    "splits": splits,
                },
                "usage_snippet": snippet,
                "quality_score": quality,
                "content_sha256": res_v["sha256"],
            })

        # 2. Non-HF Open Research Datasets: Zenodo + OpenML
        if len(verified) < max_probes:
            zen_url = "https://zenodo.org/api/records?" + urllib.parse.urlencode(
                {"type": "dataset", "q": query.strip() or "machine learning", "size": max(3, max_probes // 2), "page": max(1, cursor_page)}
            )
            ok_z, z_res = self._polite_get(zen_url, timeout=8.0)
            probed += 1
            if ok_z:
                try:
                    hits = ((json.loads(z_res["text"]) or {}).get("hits") or {}).get("hits") or []
                except Exception:
                    hits = []
                for h in hits[: max(2, max_probes - len(verified))]:
                    if not isinstance(h, dict):
                        continue
                    meta = h.get("metadata") or {}
                    rec_id = str(h.get("id") or "")
                    title = str(meta.get("title") or f"Zenodo Dataset {rec_id}")[:180]
                    doi_u = str(h.get("doi_url") or f"https://zenodo.org/records/{rec_id}")
                    api_u = f"https://zenodo.org/api/records/{rec_id}"
                    desc = re.sub(r"<[^>]+>", " ", str(meta.get("description") or ""))
                    desc = re.sub(r"\s+", " ", desc).strip()[:360] or f"Zenodo open research dataset {rec_id}"
                    verified.append({
                        "resource_kind": "dataset",
                        "title": f"Zenodo Dataset: {title}",
                        "url": doi_u,
                        "endpoint_or_raw_url": api_u,
                        "domain_tags": ["zenodo", "dataset", "open-research"] + [str(k) for k in (meta.get("keywords") or [])[:4]],
                        "description": desc,
                        "http_status": z_res["http_status"],
                        "latency_ms": z_res["latency_ms"],
                        "content_type": "application/json",
                        "schema_summary": {"source": "zenodo", "record_id": rec_id, "doi": h.get("doi")},
                        "usage_snippet": f"import json, urllib.request\nrec = json.loads(urllib.request.urlopen({api_u!r}, timeout=10).read())",
                        "quality_score": 81.5,
                        "content_sha256": hashlib.sha256(f"zenodo:{rec_id}".encode()).hexdigest(),
                    })

        if len(verified) < max_probes:
            oml_off = max(0, (cursor_page - 1) * 10)
            oml_url = f"https://www.openml.org/api/v1/json/data/list/limit/10/offset/{oml_off}/status/active"
            ok_o, o_res = self._polite_get(oml_url, timeout=8.0)
            probed += 1
            if ok_o:
                try:
                    ds_rows = ((json.loads(o_res["text"]) or {}).get("data") or {}).get("dataset") or []
                except Exception:
                    ds_rows = []
                for row in ds_rows[: max(2, max_probes - len(verified))]:
                    if not isinstance(row, dict):
                        continue
                    did = str(row.get("did") or "")
                    dname = str(row.get("name") or f"openml_{did}")
                    fmt = str(row.get("format") or "ARFF")
                    d_url = f"https://www.openml.org/d/{did}"
                    api_u = f"https://www.openml.org/api/v1/json/data/{did}"
                    verified.append({
                        "resource_kind": "dataset",
                        "title": f"OpenML Dataset: {dname} (did={did})",
                        "url": d_url,
                        "endpoint_or_raw_url": api_u,
                        "domain_tags": ["openml", "dataset", fmt.lower()],
                        "description": f"OpenML public active dataset `{dname}` (format={fmt}, version={row.get('version', 1)}).",
                        "http_status": o_res["http_status"],
                        "latency_ms": o_res["latency_ms"],
                        "content_type": "application/json",
                        "schema_summary": {"source": "openml", "did": did, "format": fmt},
                        "usage_snippet": f"import json, urllib.request\nmeta = json.loads(urllib.request.urlopen({api_u!r}, timeout=10).read())",
                        "quality_score": 78.0,
                        "content_sha256": hashlib.sha256(f"openml:{did}".encode()).hexdigest(),
                    })

        return max(1, probed), verified

    # ------------------------------------------------------------------
    # Channel 3: Documentation, Agent SKILL.md & MCP Repositories
    # ------------------------------------------------------------------
    @staticmethod
    def _doc_is_safe(text: str) -> bool:
        return not any(pat.search(text) for pat in UNTRUSTED_DOC_PATTERNS)

    def _discover_doc_skill_repos(
        self, query: str, cursor_page: int, max_probes: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        q_enc = urllib.parse.quote_plus(query)
        gh_url = (
            f"https://api.github.com/search/repositories?q={q_enc}"
            f"&sort=stars&order=desc&per_page={max_probes}&page={max(1, cursor_page)}"
        )
        ok, res = self._polite_get(gh_url, timeout=9.0)
        probed = 0
        verified: List[Dict[str, Any]] = []

        if ok:
            try:
                payload = json.loads(res["text"])
                repos = payload.get("items") if isinstance(payload, dict) else []
            except Exception:
                repos = []
            for repo in (repos or [])[:max_probes]:
                if not isinstance(repo, dict) or repo.get("private"):
                    continue
                full_name = str(repo.get("full_name") or "")
                branch = str(repo.get("default_branch") or "main")
                html_url = str(repo.get("html_url") or "")
                if not full_name or not html_url:
                    continue
                probed += 1
                doc_res = None
                raw_url = ""
                for candidate_doc in ("SKILL.md", "llms.txt", "README.md"):
                    try_url = f"https://raw.githubusercontent.com/{full_name}/{branch}/{candidate_doc}"
                    ok_d, r_d = self._polite_get(try_url, timeout=7.0, max_bytes=98_304)
                    if ok_d and len(r_d["text"].strip()) >= 80 and self._doc_is_safe(r_d["text"]):
                        doc_res = r_d
                        raw_url = try_url
                        break
                if doc_res is None:
                    continue

                headings = re.findall(r"(?m)^#{1,3}\s+(.+)$", doc_res["text"])[:8]
                stars = int(repo.get("stargazers_count") or 0)
                topics = [str(t) for t in (repo.get("topics") or [])[:6]]
                license_id = str(((repo.get("license") or {}).get("spdx_id")) or "unknown")
                desc = re.sub(r"\s+", " ", str(repo.get("description") or f"Documentation & code repository {full_name}")).strip()[:400]
                quality = round(
                    min(
                        98.0,
                        62.0
                        + min(20.0, math.log10(max(1, stars) + 1) * 4.5)
                        + (8.0 if raw_url.endswith(("SKILL.md", "llms.txt")) else 4.0)
                        + min(6.0, len(headings) * 0.8),
                    ),
                    2,
                )
                verified.append({
                    "resource_kind": "doc_skill_repo",
                    "title": f"Repo: {full_name}",
                    "url": html_url,
                    "endpoint_or_raw_url": raw_url,
                    "domain_tags": ["github", "documentation"] + topics,
                    "description": desc,
                    "http_status": doc_res["http_status"],
                    "latency_ms": doc_res["latency_ms"],
                    "content_type": "text/markdown",
                    "schema_summary": {
                        "full_name": full_name,
                        "stars": stars,
                        "license": license_id,
                        "doc_file": raw_url.rsplit("/", 1)[-1],
                        "headings": [h.strip()[:80] for h in headings],
                    },
                    "usage_snippet": (
                        f"import urllib.request\n"
                        f"doc_md = urllib.request.urlopen({raw_url!r}, timeout=8).read().decode('utf-8')"
                    ),
                    "quality_score": quality,
                    "content_sha256": doc_res["sha256"],
                })
            return max(1, probed), verified

        # Fallback when GitHub unauthenticated search hits 10 req/min rate limit: npm public registry search
        offset = max(0, (cursor_page - 1) * max_probes)
        npm_url = f"https://registry.npmjs.org/-/v1/search?text={q_enc}&size={max_probes}&from={offset}"
        ok_n, res_n = self._polite_get(npm_url, timeout=8.0)
        if not ok_n:
            return 1, []
        try:
            objs = (json.loads(res_n["text"]).get("objects")) or []
        except Exception:
            objs = []
        for obj in objs[:max_probes]:
            pkg = (obj or {}).get("package") or {}
            name = str(pkg.get("name") or "")
            if not name:
                continue
            probed += 1
            pkg_meta_url = f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@/')}"
            ok_p, res_p = self._polite_get(pkg_meta_url, timeout=7.0, max_bytes=131_072)
            if not ok_p:
                continue
            try:
                p_json = json.loads(res_p["text"])
            except Exception:
                continue
            readme = str(p_json.get("readme") or "")
            if len(readme.strip()) < 60 or not self._doc_is_safe(readme):
                continue
            headings = re.findall(r"(?m)^#{1,3}\s+(.+)$", readme)[:8]
            desc = re.sub(r"\s+", " ", str(pkg.get("description") or f"Package documentation for {name}")).strip()[:400]
            keywords = [str(k) for k in (pkg.get("keywords") or [])[:6]]
            verified.append({
                "resource_kind": "doc_skill_repo",
                "title": f"Package Docs: {name}",
                "url": str((pkg.get("links") or {}).get("npm") or pkg_meta_url),
                "endpoint_or_raw_url": pkg_meta_url,
                "domain_tags": ["npm", "documentation"] + keywords,
                "description": desc,
                "http_status": res_p["http_status"],
                "latency_ms": res_p["latency_ms"],
                "content_type": "application/json",
                "schema_summary": {
                    "package": name,
                    "version": pkg.get("version"),
                    "headings": [h.strip()[:80] for h in headings],
                },
                "usage_snippet": (
                    f"import json, urllib.request\n"
                    f"meta = json.loads(urllib.request.urlopen({pkg_meta_url!r}, timeout=8).read())\n"
                    f"readme_md = meta.get('readme', '')"
                ),
                "quality_score": 74.0,
                "content_sha256": res_p["sha256"],
            })

        # 3. Official Model Context Protocol (MCP) Server Registry (public remote MCP tools/servers)
        if len(verified) < max_probes:
            mcp_url = "https://registry.modelcontextprotocol.io/v0/servers?" + urllib.parse.urlencode({"limit": 25})
            ok_m, m_res = self._polite_get(mcp_url, timeout=8.0)
            probed += 1
            if ok_m:
                try:
                    srvs = (json.loads(m_res["text"]) or {}).get("servers") or []
                except Exception:
                    srvs = []
                q_terms = [t.lower() for t in re.findall(r"[a-z0-9_-]{3,}", query.lower())]
                matched_srvs = []
                for entry in srvs:
                    srv = (entry.get("server") if isinstance(entry, dict) else None) or {}
                    sname = str(srv.get("name") or "")
                    sdesc = str(srv.get("description") or "")
                    if not sname:
                        continue
                    hay = f"{sname} {sdesc}".lower()
                    hits = sum(1 for t in q_terms if t in hay)
                    matched_srvs.append((hits, srv))
                matched_srvs.sort(key=lambda x: -x[0])
                off = max(0, (cursor_page - 1) * 3)
                for _, srv in matched_srvs[off : off + max(3, max_probes - len(verified))]:
                    sname = str(srv.get("name") or "")
                    title = str(srv.get("title") or sname)
                    sdesc = re.sub(r"\s+", " ", str(srv.get("description") or f"Public MCP server {sname}")).strip()[:360]
                    remotes = [r.get("url") for r in (srv.get("remotes") or []) if isinstance(r, dict) and r.get("url")]
                    repo_u = str(((srv.get("repository") or {}).get("url")) or "")
                    canon = remotes[0] if remotes else (repo_u or f"https://registry.modelcontextprotocol.io/v0/servers?search={urllib.parse.quote(sname)}")
                    verified.append({
                        "resource_kind": "doc_skill_repo",
                        "title": f"MCP Server: {title} ({sname})",
                        "url": canon,
                        "endpoint_or_raw_url": mcp_url,
                        "domain_tags": ["mcp-registry", "mcp-server", "remote-tools"],
                        "description": sdesc,
                        "http_status": m_res["http_status"],
                        "latency_ms": m_res["latency_ms"],
                        "content_type": "application/json",
                        "schema_summary": {"mcp_name": sname, "version": srv.get("version"), "remotes": remotes[:3]},
                        "usage_snippet": f"# Official MCP Registry server: {sname} | remotes={remotes[:2]}",
                        "quality_score": 85.0 if remotes else 79.0,
                        "content_sha256": hashlib.sha256(f"mcp:{sname}".encode()).hexdigest(),
                    })

        return max(1, probed), verified

    def _discover_free_compute_sandboxes(
        self, query: str, cursor_page: int, max_probes: int
    ) -> Tuple[int, List[Dict[str, Any]]]:
        """Discover intentionally offered, zero-auth free compute endpoints & execution sandboxes."""
        terms = [t.lower() for t in re.findall(r"[a-z0-9_-]{2,}", query.lower())]
        verified: List[Dict[str, Any]] = []
        probed = 0

        # 1. Verify curated zero-auth code execution sandboxes on page 1
        if cursor_page == 1:
            matched_seeds: List[Dict[str, Any]] = []
            for seed in CURATED_FREE_COMPUTE_SEEDS:
                hay = f"{seed['title']} {seed['description']} {' '.join(seed['tags'])}".lower()
                if any(t in hay for t in terms):
                    matched_seeds.append(seed)
            for seed in (matched_seeds or CURATED_FREE_COMPUTE_SEEDS)[:max_probes]:
                if probed >= max_probes:
                    break
                probed += 1
                ok, res = self._polite_get(seed["endpoint_url"])
                if not ok:
                    continue
                score = round(min(99.0, 82.0 - min(res["latency_ms"] / 250.0, 8.0)), 2)
                verified.append({
                    "resource_kind": "free_compute_sandbox",
                    "title": seed["title"],
                    "url": seed["url"],
                    "endpoint_or_raw_url": seed["endpoint_url"],
                    "domain_tags": list(seed["tags"]),
                    "description": seed["description"],
                    "http_status": res["http_status"],
                    "latency_ms": res["latency_ms"],
                    "content_type": res["content_type"],
                    "schema_summary": {"kind": "zero_auth_execution_sandbox", "preview_bytes": len(res["text"])},
                    "usage_snippet": (
                        f"import urllib.request\n"
                        f"data = urllib.request.urlopen({seed['endpoint_url']!r}, timeout=10).read().decode('utf-8')"
                    ),
                    "quality_score": score,
                    "content_sha256": res["sha256"],
                })

        # 1b. Discover live zero-cost (`:free`) inference models from OpenRouter public catalog
        remaining = max_probes - probed
        if remaining > 0 and any(t in ("free", "open", "llm", "gpu", "inference", "compute", "playground", "model") for t in terms):
            ok_or, or_res = self._polite_get("https://openrouter.ai/api/v1/models", timeout=10.0, max_bytes=2_000_000)
            probed += 1
            if ok_or:
                try:
                    models = (json.loads(or_res["text"]) or {}).get("data") or []
                except Exception:
                    models = []
                free_models = [
                    m for m in models
                    if isinstance(m, dict)
                    and (
                        str(m.get("id") or "").endswith(":free")
                        or (
                            str((m.get("pricing") or {}).get("prompt", "1")) == "0"
                            and str((m.get("pricing") or {}).get("completion", "1")) == "0"
                        )
                    )
                ]
                or_off = max(0, (cursor_page - 1) * max(1, remaining // 2))
                for m in free_models[or_off : or_off + max(2, remaining // 2)]:
                    mid = str(m.get("id") or "")
                    if not mid:
                        continue
                    ctx_len = int(m.get("context_length") or 0)
                    name = str(m.get("name") or mid)
                    score = round(min(95.0, 82.0 + min(ctx_len / 32768.0, 10.0)), 2)
                    verified.append({
                        "resource_kind": "free_compute_sandbox",
                        "title": f"Free Compute Model: {name} ({mid})",
                        "url": f"https://openrouter.ai/{mid.replace(':free', '')}",
                        "endpoint_or_raw_url": "https://openrouter.ai/api/v1/models",
                        "domain_tags": ["free-compute", "openrouter-free", "llm-inference", "zero-cost"],
                        "description": f"Zero-cost free-tier inference model {mid} (context={ctx_len} tokens).",
                        "http_status": or_res["http_status"],
                        "latency_ms": or_res["latency_ms"],
                        "content_type": or_res["content_type"],
                        "schema_summary": {"model_id": mid, "context_length": ctx_len, "pricing": m.get("pricing")},
                        "usage_snippet": f"# OpenRouter free-tier model: {mid} (context_length={ctx_len})",
                        "quality_score": score,
                        "content_sha256": hashlib.sha256(mid.encode("utf-8")).hexdigest(),
                    })

        # 1c. Pollinations.ai Anonymous Zero-Auth LLM Inference Endpoints (zero API key required!)
        if len(verified) < max_probes:
            ok_pol, pol_res = self._polite_get("https://text.pollinations.ai/models", timeout=8.0)
            probed += 1
            if ok_pol:
                try:
                    pol_models = json.loads(pol_res["text"])
                except Exception:
                    pol_models = []
                if isinstance(pol_models, list):
                    for pm in pol_models[:4]:
                        if not isinstance(pm, dict) or str(pm.get("tier") or "") != "anonymous":
                            continue
                        pname = str(pm.get("name") or "")
                        if not pname:
                            continue
                        pdesc = str(pm.get("description") or pname)
                        verified.append({
                            "resource_kind": "free_compute_sandbox",
                            "title": f"Pollinations Zero-Auth LLM: {pname} ({pdesc})",
                            "url": f"https://text.pollinations.ai/models#{pname}",
                            "endpoint_or_raw_url": "https://text.pollinations.ai/models",
                            "domain_tags": ["pollinations", "zero-auth-llm", "free-compute", "openai-compatible"],
                            "description": f"Anonymous zero-auth inference model `{pname}`: {pdesc} (reasoning={pm.get('reasoning')}, tools={pm.get('tools')}).",
                            "http_status": pol_res["http_status"],
                            "latency_ms": pol_res["latency_ms"],
                            "content_type": "application/json",
                            "schema_summary": {"provider": "pollinations", "model": pname, "tier": "anonymous", "tools": pm.get("tools")},
                            "usage_snippet": f"import urllib.request, urllib.parse\ntxt = urllib.request.urlopen('https://text.pollinations.ai/' + urllib.parse.quote('Hello') + '?model={pname}', timeout=15).read().decode()",
                            "quality_score": 91.0,
                            "content_sha256": hashlib.sha256(f"pollinations:{pname}".encode()).hexdigest(),
                        })

        # 1d. Docker Hub Public Sandbox / AI Compute Container Images
        if len(verified) < max_probes:
            dh_term = next((t for t in terms if len(t) >= 3 and t not in ("free", "open", "zero", "space")), "sandbox")
            dh_url = "https://hub.docker.com/v2/search/repositories/?" + urllib.parse.urlencode(
                {"query": dh_term, "page": max(1, cursor_page), "page_size": 8}
            )
            ok_dh, dh_res = self._polite_get(dh_url, timeout=8.0)
            probed += 1
            if ok_dh:
                try:
                    dh_rows = (json.loads(dh_res["text"]) or {}).get("results") or []
                except Exception:
                    dh_rows = []
                for row in dh_rows[: max(2, (max_probes - len(verified)) // 2)]:
                    if not isinstance(row, dict):
                        continue
                    rname = str(row.get("repo_name") or "")
                    if not rname:
                        continue
                    stars = int(row.get("star_count") or 0)
                    rdesc = re.sub(r"\s+", " ", str(row.get("short_description") or f"Public Docker container image {rname}")).strip()[:300]
                    verified.append({
                        "resource_kind": "free_compute_sandbox",
                        "title": f"Docker Compute Image: {rname} ({stars}★)",
                        "url": f"https://hub.docker.com/r/{rname}" if "/" in rname else f"https://hub.docker.com/_/{rname}",
                        "endpoint_or_raw_url": dh_url,
                        "domain_tags": ["dockerhub", "container-sandbox", "free-compute", dh_term],
                        "description": rdesc,
                        "http_status": dh_res["http_status"],
                        "latency_ms": dh_res["latency_ms"],
                        "content_type": "application/json",
                        "schema_summary": {"source": "dockerhub", "repo_name": rname, "star_count": stars, "is_official": row.get("is_official")},
                        "usage_snippet": f"docker pull {rname}",
                        "quality_score": round(min(90.0, 72.0 + min(stars / 200.0, 16.0)), 2),
                        "content_sha256": hashlib.sha256(f"dockerhub:{rname}".encode()).hexdigest(),
                    })

        # 1e. free-for.dev Open-Source Free-Tier Cloud & Compute Directory (page 1)
        if cursor_page == 1 and len(verified) < max_probes and any(t in ("free", "cloud", "gpu", "serverless", "tier", "compute") for t in terms):
            ffd_url = "https://raw.githubusercontent.com/ripienaar/free-for-dev/master/README.md"
            ok_ffd, ffd_res = self._polite_get(ffd_url, timeout=8.0, max_bytes=262_144)
            probed += 1
            if ok_ffd:
                links = re.findall(r"^\s*\*\s*\[([^\]]{3,60})\]\((https://[^)\s]+)\)\s*[—-]\s*([^\n]{15,180})", ffd_res["text"], flags=re.M)
                for svc_name, svc_url, svc_desc in links[:4]:
                    verified.append({
                        "resource_kind": "free_compute_sandbox",
                        "title": f"Free-Tier Cloud/Compute: {svc_name}",
                        "url": svc_url,
                        "endpoint_or_raw_url": ffd_url,
                        "domain_tags": ["free-for-dev", "free-tier", "cloud-compute"],
                        "description": re.sub(r"\s+", " ", svc_desc).strip()[:300],
                        "http_status": ffd_res["http_status"],
                        "latency_ms": ffd_res["latency_ms"],
                        "content_type": "text/markdown",
                        "schema_summary": {"source": "free-for-dev", "service": svc_name},
                        "usage_snippet": f"# Free-tier service: {svc_name} -> {svc_url}",
                        "quality_score": 80.5,
                        "content_sha256": hashlib.sha256(f"ffd:{svc_url}".encode()).hexdigest(),
                    })

        # 2. Search Hugging Face public running Spaces (hardware tiers, SDKs, zero-GPU/CPU compute)
        remaining = max_probes - probed
        if remaining <= 0:
            return max(1, probed), verified

        search_queries: List[str] = [query.strip()] if query.strip() else ["sandbox"]
        for t in terms:
            if len(t) >= 3 and t not in ("free", "open", "zero", "space", "compute"):
                search_queries.append(t)
        seen_sq = set()
        ordered_sq = [sq for sq in search_queries if not (sq in seen_sq or seen_sq.add(sq))]

        raw_spaces: Dict[str, Dict[str, Any]] = {}
        for sq in ordered_sq[:3]:
            hub_url = (
                "https://huggingface.co/api/spaces?"
                + urllib.parse.urlencode({"search": sq, "sort": "likes", "direction": "-1", "limit": 25})
            )
            ok_hub, hub_res = self._polite_get(hub_url)
            probed += 1
            if not ok_hub:
                continue
            try:
                rows = json.loads(hub_res["text"])
            except Exception:
                continue
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sp_id = str(row.get("id") or "")
                    if not sp_id or row.get("private") or row.get("gated"):
                        continue
                    raw_spaces.setdefault(sp_id, row)
            if len(raw_spaces) >= 12:
                break

        def _space_rank(row: Dict[str, Any]) -> Tuple[int, int]:
            hay = f"{row.get('id', '')} {' '.join(row.get('tags') or [])}".lower()
            hits = sum(1 for t in terms if t in hay)
            return hits, int(row.get("likes") or 0)

        ranked_spaces = sorted(raw_spaces.values(), key=_space_rank, reverse=True)
        offset = max(0, (cursor_page - 1) * remaining)
        for sp in ranked_spaces[offset : offset + remaining]:
            sp_id = str(sp.get("id") or "")
            if not sp_id:
                continue
            meta_url = f"https://huggingface.co/api/spaces/{urllib.parse.quote(sp_id, safe='/')}"
            probed += 1
            ok_sp, sp_res = self._polite_get(meta_url)
            if not ok_sp:
                continue
            try:
                meta = json.loads(sp_res["text"])
            except Exception:
                continue
            if meta.get("private") or meta.get("gated"):
                continue
            runtime = meta.get("runtime") or {}
            stage = str(runtime.get("stage") or "").upper()
            if stage and stage != "RUNNING":
                continue
            hw = ((runtime.get("hardware") or {}).get("current") or "cpu-basic")
            sdk = str(meta.get("sdk") or sp.get("sdk") or "gradio")
            likes = int(meta.get("likes") or sp.get("likes") or 0)
            score = round(min(96.0, 68.0 + min(likes / 80.0, 18.0) + (6.0 if "gpu" in str(hw).lower() else 2.0)), 2)
            space_url = f"https://huggingface.co/spaces/{sp_id}"
            verified.append({
                "resource_kind": "free_compute_sandbox",
                "title": f"Compute Space: {sp_id} ({hw}/{sdk})",
                "url": space_url,
                "endpoint_or_raw_url": meta_url,
                "domain_tags": ["huggingface-space", sdk, str(hw), *([t for t in (meta.get("tags") or [])[:5]])],
                "description": f"Public running {sdk} compute Space on {hw} hardware ({likes} likes).",
                "http_status": sp_res["http_status"],
                "latency_ms": sp_res["latency_ms"],
                "content_type": sp_res["content_type"],
                "schema_summary": {"sdk": sdk, "hardware": hw, "stage": stage or "RUNNING", "likes": likes},
                "usage_snippet": (
                    f"import json, urllib.request\n"
                    f"meta = json.loads(urllib.request.urlopen({meta_url!r}, timeout=10).read().decode())"
                ),
                "quality_score": score,
                "content_sha256": sp_res["sha256"],
            })

        return max(1, probed), verified

    # ------------------------------------------------------------------
    # Index Upsert, Search, Closed-Loop Usage & Staleness Re-Verification
    # ------------------------------------------------------------------
    def _upsert_resource(self, item: Dict[str, Any], arm_id: str, query: str) -> bool:
        """Insert or refresh an indexed resource. Returns True if newly added."""
        now = time.time()
        rid = _slug_id(item["resource_kind"], item["url"])
        tags_str = json.dumps(item.get("domain_tags") or [])
        schema_str = json.dumps(item.get("schema_summary") or {})

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT resource_id FROM internet_resource_index WHERE url = ? OR resource_id = ?",
                (item["url"], rid),
            ).fetchone()
            is_new = existing is None
            effective_id = str(existing["resource_id"]) if existing else rid

            if is_new:
                conn.execute(
                    """
                    INSERT INTO internet_resource_index (
                        resource_id, resource_kind, title, url, endpoint_or_raw_url,
                        domain_tags_json, description, auth_verified_zero, http_status,
                        latency_ms, content_type, schema_summary_json, usage_snippet,
                        quality_score, usage_count, usage_successes, discovered_by_arm,
                        discovered_by_query, content_sha256, status, first_indexed, last_verified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        effective_id,
                        item["resource_kind"],
                        item["title"],
                        item["url"],
                        item["endpoint_or_raw_url"],
                        tags_str,
                        item["description"],
                        int(item["http_status"]),
                        float(item["latency_ms"]),
                        item["content_type"],
                        schema_str,
                        item["usage_snippet"],
                        float(item["quality_score"]),
                        arm_id,
                        query,
                        item["content_sha256"],
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE internet_resource_index
                    SET http_status = ?, latency_ms = ?, schema_summary_json = ?,
                        content_sha256 = ?, status = 'active', last_verified = ?
                    WHERE resource_id = ?
                    """,
                    (
                        int(item["http_status"]),
                        float(item["latency_ms"]),
                        schema_str,
                        item["content_sha256"],
                        now,
                        effective_id,
                    ),
                )

            if self._fts_enabled:
                conn.execute("DELETE FROM internet_resource_fts WHERE resource_id = ?", (effective_id,))
                conn.execute(
                    """
                    INSERT INTO internet_resource_fts (
                        resource_id, resource_kind, title, description, domain_tags, usage_snippet
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        effective_id,
                        item["resource_kind"],
                        item["title"],
                        item["description"],
                        " ".join(item.get("domain_tags") or []),
                        item["usage_snippet"],
                    ),
                )
        return is_new

    def search(
        self,
        query: str = "",
        resource_kind: str = "all",
        limit: int = 10,
        active_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Search indexed zero-auth internet resources via FTS5 BM25 + quality ranking."""
        limit = max(1, min(int(limit), 50))
        kind_filter = resource_kind if resource_kind in VALID_CHANNELS else None
        tokens = re.findall(r"[a-zA-Z0-9_]{2,}", query or "")

        with self._connect() as conn:
            rows = []
            if tokens and self._fts_enabled:
                fts_q = " OR ".join(f'"{t}"' for t in tokens[:8])
                sql = (
                    "SELECT idx.* FROM internet_resource_fts fts "
                    "JOIN internet_resource_index idx ON idx.resource_id = fts.resource_id "
                    "WHERE internet_resource_fts MATCH ?"
                )
                params: List[Any] = [fts_q]
                if active_only:
                    sql += " AND idx.status = 'active'"
                if kind_filter:
                    sql += " AND idx.resource_kind = ?"
                    params.append(kind_filter)
                sql += " ORDER BY (bm25(internet_resource_fts) * -5.0 + idx.quality_score) DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()

            if not rows:
                sql = "SELECT * FROM internet_resource_index WHERE 1=1"
                params = []
                if active_only:
                    sql += " AND status = 'active'"
                if kind_filter:
                    sql += " AND resource_kind = ?"
                    params.append(kind_filter)
                if tokens:
                    like_clauses = []
                    for t in tokens[:5]:
                        like_clauses.append("(LOWER(title) LIKE ? OR LOWER(description) LIKE ? OR LOWER(domain_tags_json) LIKE ?)")
                        pat = f"%{t.lower()}%"
                        params.extend([pat, pat, pat])
                    sql += " AND (" + " OR ".join(like_clauses) + ")"
                sql += " ORDER BY quality_score DESC, usage_successes DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()

        results: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["domain_tags"] = json.loads(d.pop("domain_tags_json", "[]"))
            d["schema_summary"] = json.loads(d.pop("schema_summary_json", "{}"))
            results.append(d)
        return results

    def use_resource(self, resource_id: str, max_chars: int = 8000) -> Dict[str, Any]:
        """Fetch live data/docs from an indexed resource and feed empirical success back into Bandit RSI."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM internet_resource_index WHERE resource_id = ?", (resource_id,)
            ).fetchone()
        if not row:
            return {"ok": False, "error": f"Resource '{resource_id}' not found in internet_resource_index."}
        if row["status"] == "quarantined":
            return {"ok": False, "error": f"Resource '{resource_id}' is quarantined by CyberComplianceSentinel."}

        target_url = str(row["endpoint_or_raw_url"] or row["url"])
        ok, res = self._polite_get(target_url, timeout=10.0)
        now = time.time()
        with self._connect() as conn:
            if ok:
                new_q = min(99.5, float(row["quality_score"]) + 0.75)
                conn.execute(
                    """
                    UPDATE internet_resource_index
                    SET usage_count = usage_count + 1,
                        usage_successes = usage_successes + 1,
                        quality_score = ?,
                        latency_ms = ?,
                        status = 'active',
                        last_verified = ?
                    WHERE resource_id = ?
                    """,
                    (new_q, float(res["latency_ms"]), now, resource_id),
                )
                # Closed-loop credit assignment back to the discovering bandit arm!
                arm_id = str(row["discovered_by_arm"] or "")
                if arm_id:
                    conn.execute(
                        """
                        UPDATE internet_discovery_bandit_arms
                        SET cumulative_reward = cumulative_reward + 0.08,
                            mean_reward = MIN(1.0, (cumulative_reward + 0.08) / MAX(1, pulls)),
                            updated_at = ?
                        WHERE arm_id = ?
                        """,
                        (now, arm_id),
                    )
                return {
                    "ok": True,
                    "resource_id": resource_id,
                    "resource_kind": row["resource_kind"],
                    "title": row["title"],
                    "fetched_url": target_url,
                    "content_type": res["content_type"],
                    "latency_ms": res["latency_ms"],
                    "content_preview": res["text"][: max(200, min(max_chars, 24000))],
                }
            else:
                new_status = "quarantined" if res.get("quarantined") else (
                    "auth_blocked" if "auth" in str(res.get("reason", "")) else "stale"
                )
                conn.execute(
                    """
                    UPDATE internet_resource_index
                    SET usage_count = usage_count + 1,
                        quality_score = MAX(10.0, quality_score - 5.0),
                        status = ?,
                        last_verified = ?
                    WHERE resource_id = ?
                    """,
                    (new_status, now, resource_id),
                )
                return {
                    "ok": False,
                    "resource_id": resource_id,
                    "status": new_status,
                    "error": res.get("reason", "fetch_failed"),
                }

    def reverify_stale_batch(self, max_Age_sec: float = 6 * 86400, limit: int = 4) -> Tuple[int, int]:
        """Re-probe oldest active resources so dead endpoints are pruned automatically."""
        cutoff = time.time() - max_Age_sec
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT resource_id, endpoint_or_raw_url FROM internet_resource_index
                WHERE status = 'active' AND last_verified < ?
                ORDER BY last_verified ASC LIMIT ?
                """,
                (cutoff, max(0, int(limit))),
            ).fetchall()

        checked = 0
        demoted = 0
        now = time.time()
        for r in rows:
            checked += 1
            ok, res = self._polite_get(str(r["endpoint_or_raw_url"]), timeout=7.0)
            with self._connect() as conn:
                if ok:
                    conn.execute(
                        "UPDATE internet_resource_index SET latency_ms=?, last_verified=? WHERE resource_id=?",
                        (float(res["latency_ms"]), now, r["resource_id"]),
                    )
                else:
                    demoted += 1
                    st = "auth_blocked" if "auth" in str(res.get("reason", "")) else "stale"
                    conn.execute(
                        "UPDATE internet_resource_index SET status=?, last_verified=? WHERE resource_id=?",
                        (st, now, r["resource_id"]),
                    )
        return checked, demoted

    # ------------------------------------------------------------------
    # UCB1 Multi-Armed Bandit & Recursive Query Self-Improvement
    # ------------------------------------------------------------------
    def _select_bandit_arms(
        self, channels: Sequence[str], arms_per_sweep: int = 3
    ) -> List[Dict[str, Any]]:
        valid_ch = [c for c in channels if c in VALID_CHANNELS] or list(VALID_CHANNELS)
        with self._connect() as conn:
            placeholders = ",".join("?" for _ in valid_ch)
            arms = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM internet_discovery_bandit_arms WHERE active = 1 AND channel IN ({placeholders}) ORDER BY rowid ASC",
                    valid_ch,
                ).fetchall()
            ]
            if not arms:
                # Re-activate best historical arms incremented to next cursor page if all exhausted
                conn.execute(
                    f"UPDATE internet_discovery_bandit_arms SET active = 1, zero_yield_streak = 0 WHERE channel IN ({placeholders})",
                    valid_ch,
                )
                arms = [
                    dict(r)
                    for r in conn.execute(
                        f"SELECT * FROM internet_discovery_bandit_arms WHERE active = 1 AND channel IN ({placeholders}) ORDER BY rowid ASC",
                        valid_ch,
                    ).fetchall()
                ]

        total_pulls = sum(int(a["pulls"]) for a in arms) + 1
        for a in arms:
            n_i = int(a["pulls"])
            mean_r = float(a["mean_reward"])
            explore = 0.85 * math.sqrt(2.0 * math.log(total_pulls + 1.0) / (n_i + 1.0))
            unpulled_bonus = 0.20 if n_i == 0 else 0.0
            a["ucb_score"] = round(mean_r + explore + unpulled_bonus, 4)

        # Ensure channel diversity first (top UCB arm per requested channel), then overall UCB
        selected: List[Dict[str, Any]] = []
        used_ids = set()
        for ch in valid_ch:
            ch_arms = sorted([a for a in arms if a["channel"] == ch], key=lambda x: x["ucb_score"], reverse=True)
            if ch_arms and len(selected) < arms_per_sweep:
                selected.append(ch_arms[0])
                used_ids.add(ch_arms[0]["arm_id"])

        for a in sorted(arms, key=lambda x: x["ucb_score"], reverse=True):
            if len(selected) >= arms_per_sweep:
                break
            if a["arm_id"] not in used_ids:
                selected.append(a)
                used_ids.add(a["arm_id"])
        return selected

    def _collect_operational_gap_terms(self) -> List[str]:
        """Extract live domain terms from Sabrina's recent operational failures, jobs, and RSI state."""
        terms: List[str] = []
        if self.rsi_state_path.exists():
            try:
                st = json.loads(self.rsi_state_path.read_text(encoding="utf-8"))
                for q in (st.get("evolved_job_queries") or [])[:4]:
                    terms.extend(re.findall(r"[a-z]{4,}", str(q).lower()))
                for sig in (st.get("synthesized_failure_signatures") or [])[-4:]:
                    terms.extend(re.findall(r"[a-z]{4,}", str(sig).lower()))
            except Exception:
                pass
        if self.social_db_path.exists():
            try:
                with sqlite3.connect(str(self.social_db_path)) as conn:
                    rows = conn.execute(
                        "SELECT title FROM job_applications ORDER BY updated DESC LIMIT 8"
                    ).fetchall()
                    for (t,) in rows:
                        terms.extend(re.findall(r"[a-z]{4,}", str(t or "").lower()))
            except Exception:
                pass
        stop = {"engineer", "senior", "staff", "lead", "principal", "trace", "remediation", "with", "from"}
        dedup: List[str] = []
        for t in terms:
            if t not in stop and t not in dedup:
                dedup.append(t)
        return dedup[:10]

    def _query_idle_local_mutator(self, prompt: str) -> Optional[List[Dict[str, str]]]:
        """Ask an idle divergent/disjoint local model (`dolphin-mistral:7b` / `granite3.1-dense:8b`) for child queries."""
        if self.llm_mutator_fn is not None:
            return self.llm_mutator_fn(prompt)
        try:
            from tools.control_client import configured, inference_wait_budget
            client = configured()
            if client is None:
                return None
            st = client.request("/v1/status")
            if any(l.get("resource") == "compute:lab" and l.get("state") in ("held", "uncertain") for l in st.get("leases", [])):
                return None
            if any(r.get("state") in ("queued", "running") for r in st.get("requests", [])):
                return None
            client.client, client.project = "internet-resource-indexer", "bandit-query-rsi"
            for model_id in ("dolphin-mistral:7b", "granite3.1-dense:8b"):
                try:
                    with inference_wait_budget(15):
                        resp = client.infer(
                            "/api/chat",
                            {
                                "model": model_id,
                                "stream": False,
                                "think": False,
                                "options": {"temperature": 0.45, "num_predict": 350},
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": (
                                            "Generate 4 concise 2-to-4 word search queries as a JSON array of objects: "
                                            '[{"channel":"public_api","query":"..."},{"channel":"dataset","query":"..."},'
                                            '{"channel":"doc_skill_repo","query":"..."},{"channel":"free_compute_sandbox","query":"..."}]. Return JSON only.'
                                        ),
                                    },
                                    {"role": "user", "content": prompt},
                                ],
                            },
                            timeout=20,
                            device="auto",
                            task_label=f"internet_query_evolution:{model_id}",
                        )
                    txt = ((resp.get("message") or {}).get("content") or "").strip()
                    m = re.search(r"\[.*\]", txt, re.DOTALL)
                    if m:
                        parsed = json.loads(m.group(0))
                        if isinstance(parsed, list):
                            return parsed
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _evolve_bandit_queries(self, next_gen: int) -> List[Dict[str, Any]]:
        """Synthesize `generation + 1` search arms from highest-yield parents + live operational gaps."""
        with self._connect() as conn:
            top_parents = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM internet_discovery_bandit_arms
                    WHERE pulls > 0
                    ORDER BY mean_reward DESC, novel_indexed DESC LIMIT 6
                    """
                ).fetchall()
            ]
            existing_pairs = {
                (str(r["channel"]), str(r["query"]).strip().lower())
                for r in conn.execute("SELECT channel, query FROM internet_discovery_bandit_arms").fetchall()
            }
            dead_queries = [
                str(r["query"])
                for r in conn.execute(
                    "SELECT query FROM internet_discovery_bandit_arms WHERE active = 0 LIMIT 8"
                ).fetchall()
            ]

        if not top_parents:
            return []

        gap_terms = self._collect_operational_gap_terms()
        prompt = (
            f"Top high-yield parent search queries: {[p['channel'] + ':' + p['query'] for p in top_parents[:4]]}\n"
            f"Exhausted queries to avoid: {dead_queries}\n"
            f"Sabrina operational focus terms: {gap_terms or ['cybersecurity', 'python', 'verification', 'timeseries']}\n"
            f"Propose 1 novel search query for each channel: public_api, dataset, doc_skill_repo, free_compute_sandbox."
        )

        proposals = self._query_idle_local_mutator(prompt) or []
        evolved: List[Dict[str, Any]] = []
        now = time.time()

        # Validate any LLM-proposed arms first
        for prop in proposals:
            if not isinstance(prop, dict):
                continue
            ch = str(prop.get("channel") or "").strip()
            q_raw = re.sub(r"[^a-zA-Z0-9._\-\s]", " ", str(prop.get("query") or "")).strip()
            q_norm = re.sub(r"\s+", " ", q_raw).lower()
            if ch not in VALID_CHANNELS or len(q_norm) < 4 or len(q_norm) > 60:
                continue
            ok_q, _ = self.sentinel.sanitize_outbound_query(q_norm)
            if not ok_q or (ch, q_norm) in existing_pairs:
                continue
            parent_q = next((p["query"] for p in top_parents if p["channel"] == ch), top_parents[0]["query"])
            evolved.append({"channel": ch, "query": q_norm, "parent_query": parent_q, "source": "llm_bandit_mutator"})
            existing_pairs.add((ch, q_norm))

        # Deterministic combinatoric backfill so recursive evolution NEVER stalls when GPU is busy
        covered_channels = {e["channel"] for e in evolved}
        for ch in VALID_CHANNELS:
            if ch in covered_channels:
                continue
            ch_parents = [p for p in top_parents if p["channel"] == ch] or top_parents
            parent = ch_parents[0]
            parent_words = [w for w in re.findall(r"[a-z0-9._-]+", parent["query"].lower()) if len(w) >= 3]
            anchor = parent_words[0] if parent_words else "security"
            modifiers = list(gap_terms) + MUTATION_MODIFIERS[ch]
            for mod in modifiers:
                if mod == anchor:
                    continue
                cand_q = f"{anchor} {mod}".strip().lower()
                ok_q, _ = self.sentinel.sanitize_outbound_query(cand_q)
                if ok_q and (ch, cand_q) not in existing_pairs:
                    evolved.append({
                        "channel": ch,
                        "query": cand_q,
                        "parent_query": parent["query"],
                        "source": "deterministic_gap_crossover",
                    })
                    existing_pairs.add((ch, cand_q))
                    break

        with self._connect() as conn:
            for item in evolved:
                raw_key = f"{item['channel']}:{item['query']}".encode("utf-8")
                arm_id = f"arm_{item['channel'][:4]}_{hashlib.sha256(raw_key).hexdigest()[:10]}"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO internet_discovery_bandit_arms (
                        arm_id, channel, query, cursor_page, pulls, candidates_probed,
                        novel_indexed, cumulative_reward, mean_reward, zero_yield_streak,
                        generation, parent_query, active, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 0, 0, 0, 0.0, 0.55, 0, ?, ?, 1, ?, ?)
                    """,
                    (arm_id, item["channel"], item["query"], next_gen, item["parent_query"], now, now),
                )
        return evolved

    # ------------------------------------------------------------------
    # Scheduled Periodic Sweep Entrypoint
    # ------------------------------------------------------------------
    def run_scheduled_sweep(
        self,
        channels: Optional[Sequence[str]] = None,
        arms_per_sweep: int = 3,
        max_probes_per_arm: int = 6,
        reverify_limit: int = 4,
    ) -> Dict[str, Any]:
        """Execute one bounded periodic discovery, indexing, re-verification & bandit-RSI sweep."""
        started = time.time()
        arms_per_sweep = max(1, min(int(arms_per_sweep), 12))
        max_probes_per_arm = max(1, min(int(max_probes_per_arm), 25))

        with self._connect() as conn:
            prev_gen_row = conn.execute("SELECT COALESCE(MAX(generation), 0) FROM internet_sweep_runs").fetchone()
            generation = int(prev_gen_row[0]) + 1

        selected_arms = self._select_bandit_arms(channels or VALID_CHANNELS, arms_per_sweep=arms_per_sweep)
        total_probed = 0
        newly_indexed = 0
        updated_existing = 0
        arm_reports: List[Dict[str, Any]] = []

        for arm in selected_arms:
            ch = str(arm["channel"])
            q = str(arm["query"])
            page = int(arm["cursor_page"])

            if ch == "public_api":
                probed, items = self._discover_public_apis(q, page, max_probes_per_arm)
            elif ch == "dataset":
                probed, items = self._discover_datasets(q, page, max_probes_per_arm)
            elif ch == "free_compute_sandbox":
                probed, items = self._discover_free_compute_sandboxes(q, page, max_probes_per_arm)
            else:
                probed, items = self._discover_doc_skill_repos(q, page, max_probes_per_arm)

            arm_new = 0
            arm_upd = 0
            qualities: List[float] = []
            for item in items:
                qualities.append(float(item["quality_score"]))
                if self._upsert_resource(item, arm["arm_id"], q):
                    arm_new += 1
                else:
                    arm_upd += 1

            total_probed += probed
            newly_indexed += arm_new
            updated_existing += arm_upd

            avg_q = (sum(qualities) / len(qualities)) if qualities else 0.0
            yield_ratio = arm_new / max(1, probed)
            reward = round(min(1.0, 0.65 * yield_ratio + 0.35 * (avg_q / 100.0)), 4)

            new_pulls = int(arm["pulls"]) + 1
            new_cum = float(arm["cumulative_reward"]) + reward
            new_mean = round(new_cum / new_pulls, 4)
            new_streak = 0 if arm_new > 0 else (int(arm["zero_yield_streak"]) + 1)
            still_active = 0 if new_streak >= 2 else 1

            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE internet_discovery_bandit_arms
                    SET cursor_page = cursor_page + 1,
                        pulls = ?,
                        candidates_probed = candidates_probed + ?,
                        novel_indexed = novel_indexed + ?,
                        cumulative_reward = ?,
                        mean_reward = ?,
                        zero_yield_streak = ?,
                        active = ?,
                        updated_at = ?
                    WHERE arm_id = ?
                    """,
                    (
                        new_pulls,
                        probed,
                        arm_new,
                        new_cum,
                        new_mean,
                        new_streak,
                        still_active,
                        time.time(),
                        arm["arm_id"],
                    ),
                )

            arm_reports.append({
                "arm_id": arm["arm_id"],
                "channel": ch,
                "query": q,
                "page_probed": page,
                "probed": probed,
                "newly_indexed": arm_new,
                "updated_existing": arm_upd,
                "reward": reward,
                "retired_exhausted": still_active == 0,
            })

        reverified_count, stale_demoted = self.reverify_stale_batch(limit=reverify_limit)
        evolved = self._evolve_bandit_queries(next_gen=generation)

        duration = round(time.time() - started, 3)
        sweep_id = f"isweep_gen{generation}_{int(started)}"

        with self._connect() as conn:
            active_total = int(
                conn.execute("SELECT COUNT(*) FROM internet_resource_index WHERE status = 'active'").fetchone()[0]
            )
            by_kind = {
                str(r[0]): int(r[1])
                for r in conn.execute(
                    "SELECT resource_kind, COUNT(*) FROM internet_resource_index WHERE status = 'active' GROUP BY resource_kind"
                ).fetchall()
            }
            conn.execute(
                """
                INSERT INTO internet_sweep_runs (
                    sweep_id, generation, started_at, duration_sec, arms_pulled_json,
                    probed_count, newly_indexed_count, updated_existing_count,
                    reverified_count, stale_demoted_count, evolved_queries_json,
                    total_active_index_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sweep_id,
                    generation,
                    started,
                    duration,
                    json.dumps(arm_reports),
                    total_probed,
                    newly_indexed,
                    updated_existing,
                    reverified_count,
                    stale_demoted,
                    json.dumps(evolved),
                    active_total,
                ),
            )

        return {
            "status": "completed",
            "sweep_id": sweep_id,
            "generation": generation,
            "duration_sec": duration,
            "probed_count": total_probed,
            "newly_indexed_count": newly_indexed,
            "updated_existing_count": updated_existing,
            "reverified_count": reverified_count,
            "stale_demoted_count": stale_demoted,
            "arms_pulled": arm_reports,
            "evolved_queries": evolved,
            "active_index_counts": by_kind,
            "total_active_index_size": active_total,
        }

    def status_summary(self) -> Dict[str, Any]:
        """Return live catalog counts, top bandit arms, and recent sweep history."""
        with self._connect() as conn:
            by_kind = {
                str(r[0]): int(r[1])
                for r in conn.execute(
                    "SELECT resource_kind, COUNT(*) FROM internet_resource_index WHERE status = 'active' GROUP BY resource_kind"
                ).fetchall()
            }
            by_status = {
                str(r[0]): int(r[1])
                for r in conn.execute(
                    "SELECT status, COUNT(*) FROM internet_resource_index GROUP BY status"
                ).fetchall()
            }
            top_arms = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT channel, query, cursor_page, pulls, novel_indexed, mean_reward, generation, active
                    FROM internet_discovery_bandit_arms
                    ORDER BY mean_reward DESC, novel_indexed DESC LIMIT 10
                    """
                ).fetchall()
            ]
            recent_sweeps = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT sweep_id, generation, started_at, duration_sec, probed_count,
                           newly_indexed_count, stale_demoted_count, total_active_index_size
                    FROM internet_sweep_runs ORDER BY generation DESC LIMIT 5
                    """
                ).fetchall()
            ]
        return {
            "active_by_kind": by_kind,
            "total_active": sum(by_kind.values()),
            "by_status": by_status,
            "top_bandit_arms": top_arms,
            "recent_sweeps": recent_sweeps,
        }


# ------------------------------------------------------------------
# ToolRegistry Handlers for Sabrina Agents
# ------------------------------------------------------------------
def search_internet_resources_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    indexer = InternetResourceIndexer()
    query = str(action.get("query") or "")
    kind = str(action.get("resource_kind") or "all")
    limit = int(action.get("limit") or 8)
    matches = indexer.search(query=query, resource_kind=kind, limit=limit)
    return {"ok": True, "count": len(matches), "resources": matches}


def use_indexed_resource_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    resource_id = str(action.get("resource_id") or "").strip()
    if not resource_id:
        return {"ok": False, "error": "Missing required parameter 'resource_id'."}
    max_chars = int(action.get("max_chars") or 8000)
    return InternetResourceIndexer().use_resource(resource_id=resource_id, max_chars=max_chars)


def run_internet_resource_sweep_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    channels = action.get("channels")
    if isinstance(channels, str):
        channels = [c.strip() for c in channels.split(",") if c.strip()]
    arms = int(action.get("arms_per_sweep") or 3)
    probes = int(action.get("max_probes_per_arm") or 5)
    return InternetResourceIndexer().run_scheduled_sweep(
        channels=channels, arms_per_sweep=arms, max_probes_per_arm=probes
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Internet Resource Indexer (Public APIs, Datasets, Doc/Skill Repos)")
    sub = parser.add_subparsers(dest="cmd")

    p_sweep = sub.add_parser("sweep", help="Run one bounded discovery, indexing & recursive query-evolution sweep")
    p_sweep.add_argument("--arms", type=int, default=3)
    p_sweep.add_argument("--max-probes", type=int, default=6)
    p_sweep.add_argument("--channel", action="append", choices=list(VALID_CHANNELS))

    p_search = sub.add_parser("search", help="Search indexed resources")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--kind", default="all", choices=["all"] + list(VALID_CHANNELS))
    p_search.add_argument("--limit", type=int, default=10)

    p_use = sub.add_parser("use", help="Fetch live content from an indexed resource_id")
    p_use.add_argument("resource_id")

    sub.add_parser("status", help="Show index summary, top bandit arms, and sweep history")

    args = parser.parse_args()
    indexer = InternetResourceIndexer()
    if args.cmd == "sweep":
        print(json.dumps(indexer.run_scheduled_sweep(channels=args.channel, arms_per_sweep=args.arms, max_probes_per_arm=args.max_probes), indent=2))
    elif args.cmd == "search":
        print(json.dumps(indexer.search(query=args.query, resource_kind=args.kind, limit=args.limit), indent=2))
    elif args.cmd == "use":
        print(json.dumps(indexer.use_resource(args.resource_id), indent=2))
    else:
        print(json.dumps(indexer.status_summary(), indent=2))


if __name__ == "__main__":
    main()
