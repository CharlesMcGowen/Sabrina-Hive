"""Bounded, read-only model infrastructure snapshot for the local control center."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FLOW_DB = ROOT / "data" / "sabrina_assistant.sqlite3"
RESEARCH_DB = ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
TELEMETRY_CONFIG = ROOT / 'config' / 'model_telemetry.json'


def _ollama_base():
    override=os.environ.get('SABRINA_TELEMETRY_OLLAMA_URL')
    if override:return override.rstrip('/')
    if TELEMETRY_CONFIG.is_file():
        value=json.loads(TELEMETRY_CONFIG.read_text(encoding='utf-8'))
        return value.get('ollama_base','http://ego.local:11434').rstrip('/')
    return 'http://ego.local:11434'


def _ollama(path: str) -> dict[str, Any]:
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(_ollama_base() + path, timeout=3) as response:
        return json.load(response)


def _flows(db_path: Path = FLOW_DB, limit: int = 50) -> dict[str, Any]:
    if not db_path.is_file():
        return {"rows": [], "total": 0, "latest": None}
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_network_flows'").fetchone()
        if not exists:
            return {"rows": [], "total": 0, "latest": None}
        total, latest = db.execute("SELECT COUNT(*), MAX(timestamp) FROM assistant_network_flows").fetchone()
        fields = ("flow_id", "timestamp", "worker_id", "run_id", "operation_id", "target_host",
                  "target_port", "method", "path", "status_code", "latency_ms", "queue_wait_ms",
                  "ttft_ms", "model_requested", "think_param", "stream_param", "num_ctx", "error_message")
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT " + ",".join(fields) + " FROM assistant_network_flows ORDER BY timestamp DESC LIMIT ?",
                          (max(1, min(limit, 100)),)).fetchall()
        return {"rows": [dict(row) for row in rows], "total": total, "latest": latest}


def _external_research(db_path: Path = RESEARCH_DB, limit: int = 50) -> dict[str, Any]:
    if not db_path.is_file():
        return {"enabled": None, "candidate_enabled": None, "recent": [], "total": 0,
                "reachable_candidates": [], "state_counts": {}, "qualified_role_records": 0}
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        required = {"portal_settings", "portal_profiles", "portal_requests", "portal_role_qualifications"}
        found = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= found:
            raise ValueError("External research tables are incomplete")
        settings = {row["key"]: json.loads(row["value"]) for row in db.execute("SELECT key,value FROM portal_settings")}
        profiles = db.execute("""SELECT portal_id,provider,model_claim,verified_model,access_allowed,
 access_state,retry_after,adapter_json FROM portal_profiles""").fetchall()
        candidates = []
        malformed_profiles = 0
        now = time.time()
        for row in profiles:
            if not row["access_allowed"] or row["access_state"] != "reachable" or row["retry_after"] > now:
                continue
            try:
                adapter = json.loads(row["adapter_json"])
            except (TypeError, ValueError):
                malformed_profiles += 1
                continue
            if adapter.get("verified_protocol") is True:
                candidates.append({key: row[key] for key in
                                   ("portal_id", "provider", "model_claim", "verified_model", "access_state")})
        total = db.execute("""SELECT COUNT(*) FROM portal_requests
 WHERE purpose IN ('research','research_reserve','research_candidate')""").fetchone()[0]
        recent = db.execute("""SELECT request_id,portal_id,provider,purpose,state,result,started,completed
 FROM portal_requests WHERE purpose IN ('research','research_reserve','research_candidate')
 ORDER BY started DESC LIMIT ?""", (max(1, min(limit, 100)),)).fetchall()
        qualified = db.execute("""SELECT COUNT(*) FROM portal_role_qualifications
 WHERE responsibility='qualified' AND expires>?""", (now,)).fetchone()[0]
        states = {row[0]: row[1] for row in db.execute(
            "SELECT access_state,COUNT(*) FROM portal_profiles GROUP BY access_state")}
        return {"enabled": settings.get("research_enabled"),
                "candidate_enabled": settings.get("research_candidates_enabled"),
                "recent": [dict(row) for row in recent], "total": total,
                "reachable_candidates": candidates, "state_counts": states,
                "malformed_profiles": malformed_profiles,
                "qualified_role_records": qualified,
                "note": "Reachable protocol candidates are experimental; qualification and actual delivery are separate."}


def _select(items: list[dict], keys: tuple[str, ...], limit: int = 100) -> list[dict]:
    return [{key: item.get(key) for key in keys if key in item} for item in items[:limit]]


def _routing() -> dict[str, Any]:
    from tools.free_compute_providers import FREE_COMPUTE_ROUTER

    providers = (FREE_COMPUTE_ROUTER.groq, FREE_COMPUTE_ROUTER.nvidia_nim,
                 FREE_COMPUTE_ROUTER.github_models)
    return {
        "coordinator": "Local Ollama via managed inference queue",
        "free_compute_router": [{
            "provider": provider.provider_name,
            "endpoint": provider.base_url,
            "configured_rpm": provider.rate_limit_rpm,
            "candidate_models": provider.default_models,
            "credential_loaded_in_telemetry_process": bool(provider.api_key),
        } for provider in providers] + [{"provider": "local_ollama", "configured_rpm": None}],
        "note": "Configuration and telemetry-process credential presence only; provider reachability and use are unverified. Actual use appears in captured flows and coordinator requests.",
    }


def snapshot(*, control=None, ollama_get=None, flow_db: Path = FLOW_DB,
             research_db: Path = RESEARCH_DB) -> dict[str, Any]:
    """Return metadata only. Never return prompts, replies, headers, tokens or full payloads."""
    result: dict[str, Any] = {"observed_at": time.time(), "sources": {}, "coordinator": {},
                              "models": {"installed": [], "resident": []}, "traffic": {},
                              "external_research": {}, "routing": {}}
    try:
        if control is None:
            from tools.control_client import configured
            control = configured()
        if control is None:
            raise RuntimeError("Coordinator client is not configured")
        status = control.request("/v1/status")
        result["coordinator"] = {
            "inference_concurrency": status.get("inference_concurrency"),
            "unmanaged_callers_isolated": status.get("unmanaged_callers_isolated"),
            "requests": _select(status.get("requests") or [], ("id", "client", "project", "state", "error", "created", "attempts", "model", "requested_device", "task_label", "placement")),
            "objectives": _select(status.get("objectives") or [], ("id", "client", "project", "state")),
            "leases": _select(status.get("leases") or [], ("resource", "owner", "state", "expires", "lease_until", "updated")),
        }
        result["sources"]["coordinator"] = "ok"
    except Exception as exc:
        result["sources"]["coordinator"] = f"{type(exc).__name__}: {exc}"[:180]
    getter = ollama_get or _ollama
    for path, key in (("/api/tags", "installed"), ("/api/ps", "resident")):
        try:
            data = getter(path)
            keys = ("name", "model", "size", "size_vram", "expires_at", "details", "context_length")
            result["models"][key] = _select(data.get("models") or [], keys, 200)
            result["sources"]["ollama_" + key] = "ok"
        except Exception as exc:
            result["sources"]["ollama_" + key] = f"{type(exc).__name__}: {exc}"[:180]
    try:
        result["traffic"] = _flows(flow_db)
        result["sources"]["flow_journal"] = "ok"
    except Exception as exc:
        result["sources"]["flow_journal"] = f"{type(exc).__name__}: {exc}"[:180]
    try:
        result["external_research"] = _external_research(research_db)
        result["sources"]["external_research"] = "ok"
    except Exception as exc:
        result["sources"]["external_research"] = f"{type(exc).__name__}: {exc}"[:180]
    try:
        result["routing"] = _routing()
        result["sources"]["routing_configuration"] = "ok"
    except Exception as exc:
        result["sources"]["routing_configuration"] = f"{type(exc).__name__}: {exc}"[:180]
    return result
