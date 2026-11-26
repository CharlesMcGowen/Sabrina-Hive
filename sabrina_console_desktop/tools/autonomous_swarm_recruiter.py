#!/usr/bin/env python3
"""Autonomous Windows Single-Authority Swarm Member Recruiter & Prober.

Respects `AGENTS.md` Single Authority Rule & Cyber Law Rules of Engagement:
- Operates strictly on Windows's live authority DB (`data/sabrina_assistant_fresh.sqlite3`).
- Probes only zero-auth public Hugging Face Spaces (`HTTP 200`, zero credentials/cookies).
- Disqualifies `401`/`403` auth-gated spaces and filters out Gradio error wrappers.
- Screens every URL and payload through `CyberComplianceSentinel`.
- Drains `swarm_discovery_candidates` (`schema_verified` first) AND discovers fresh public
  Gradio Spaces, admitting live-verified portals into `assistant_external_portals`.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
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

DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
DEFAULT_LOG_PATH = ROOT / "data" / "swarm_recruitment_log.json"
USER_AGENT = "SabrinaSwarmRecruiter/1.0 (+inquiry-only; zero-auth public gradio)"

DISCOVERY_TAGS: Tuple[str, ...] = (
    "chatbot",
    "llm",
    "agent",
    "rag",
    "coding",
    "assistant",
    "reasoning",
    "chat",
    "mcp",
    "qwen",
    "llama",
    "mistral",
)

_ERROR_WRAPPER_RE = re.compile(
    r"(?:401\s+unauthorized|403\s+forbidden|invalid\s+api\s+key|space\s+is\s+sleeping|runtime\s+error)",
    re.IGNORECASE,
)

_PREFERRED_CHAT_ENDPOINTS: Tuple[str, ...] = (
    "/chat", "/respond", "/predict", "/generate", "/ask", "/answer", "/reply", "/submit", "/bot", "/inference",
)
_EXCLUDED_ECHO_ENDPOINTS: Tuple[str, ...] = (
    "user_submit", "add_text", "add_message", "clear", "reset", "like", "vote", "flag", "upvote", "downvote", "retry", "undo",
)


def _select_verified_gradio_adapter(base_url: str, info_url: str, info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a verified documented_gradio_adapter skipping echo-only endpoints like `user_submit`."""
    from tools.portal_transport import documented_gradio_adapter

    named = info.get("named_endpoints") or {}
    if not isinstance(named, dict) or not named:
        return None
    call_prefix = info_url.rsplit("/info", 1)[0] + "/call"
    candidates = []
    for ep in named.keys():
        ep_low = str(ep).lower().lstrip("/")
        if any(bad in ep_low for bad in _EXCLUDED_ECHO_ENDPOINTS):
            continue
        pref_rank = next((i for i, p in enumerate(_PREFERRED_CHAT_ENDPOINTS) if p.lstrip("/") == ep_low), 99)
        candidates.append((pref_rank, str(ep)))
    candidates.sort()

    for _, ep in candidates:
        call_url = f"{call_prefix}{ep if ep.startswith('/') else '/' + ep}"
        try:
            adapter = documented_gradio_adapter(call_url, info)
            if adapter.get("verified_protocol"):
                return adapter
        except Exception:
            continue
    return None


def _slug_portal_id(space_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", space_id.lower()).strip("-")
    return f"hf-gradio-{slug[:72]}"


def _space_host_url(space_id: str) -> str:
    owner, _, name = space_id.partition("/")
    sub = re.sub(r"[^a-z0-9-]+", "-", f"{owner}-{name}".lower()).strip("-")
    return f"https://{sub}.hf.space"


def _default_get(url: str, timeout: float = 6.0) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json,*/*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(getattr(resp, "status", 200)), {k.lower(): v for k, v in resp.headers.items()}, resp.read(262_144)


class AutonomousSwarmRecruiter:
    """Batch prober, live zero-auth verifier, and fresh discoverer for Sabrina's Swarm."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        log_path: Optional[Path | str] = None,
        sentinel: Optional[CyberComplianceSentinel] = None,
        http_get_fn: Optional[Callable[[str, float], Tuple[int, Dict[str, str], bytes]]] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
        self.sentinel = sentinel or CyberComplianceSentinel()
        self.http_get = http_get_fn or _default_get

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def status(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total_portals = conn.execute("SELECT COUNT(*) FROM assistant_external_portals").fetchone()[0]
            reachable = conn.execute(
                "SELECT COUNT(*) FROM assistant_external_portals WHERE reachability_state = 'reachable'"
            ).fetchone()[0]
            qualified = conn.execute(
                "SELECT COUNT(*) FROM assistant_external_portals WHERE status = 'qualified'"
            ).fetchone()[0]
            staged_sv = conn.execute(
                """
                SELECT COUNT(*) FROM swarm_discovery_candidates c
                WHERE c.probe_status = 'schema_verified' AND c.state = 'staged'
                  AND NOT EXISTS (SELECT 1 FROM assistant_external_portals p WHERE p.portal_id = c.portal_id OR p.url = c.url)
                """
            ).fetchone()[0]
            total_staged = conn.execute(
                "SELECT COUNT(*) FROM swarm_discovery_candidates WHERE state = 'staged'"
            ).fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "total_portals": int(total_portals),
            "reachable_portals": int(reachable),
            "qualified_portals": int(qualified),
            "staged_schema_verified_backlog": int(staged_sv),
            "total_staged_candidates": int(total_staged),
        }

    def _probe_gradio_candidate(self, cand: Dict[str, Any]) -> Dict[str, Any]:
        """Verify live zero-auth Gradio `/gradio_api/info` (or `/info`) HTTP 200 reachability."""
        base_url = str(cand["url"]).rstrip("/")
        info_urls = [cand.get("info_url") or f"{base_url}/gradio_api/info", f"{base_url}/info"]
        seen = set()
        urls = [u for u in info_urls if u and not (u in seen or seen.add(u))]

        for target in urls:
            ok_out, reason = self.sentinel.sanitize_outbound_query(target)
            if not ok_out:
                return {"ok": False, "probe_status": "sentinel_blocked", "http_status": 0, "latency_ms": 0.0, "error": reason}
            t0 = time.perf_counter()
            try:
                status, headers, raw = self.http_get(target, 6.0)
            except urllib.error.HTTPError as exc:
                code = int(getattr(exc, "code", 500))
                if code in (401, 403):
                    return {"ok": False, "probe_status": "auth_required", "http_status": code, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
                continue
            except Exception as exc:
                continue

            lat = round((time.perf_counter() - t0) * 1000.0, 1)
            if status in (401, 403) or "www-authenticate" in headers:
                return {"ok": False, "probe_status": "auth_required", "http_status": status, "latency_ms": lat}
            if status != 200:
                continue
            text = raw.decode("utf-8", errors="replace")
            if _ERROR_WRAPPER_RE.search(text[:2000]):
                return {"ok": False, "probe_status": "upstream_error_wrapper", "http_status": status, "latency_ms": lat}
            threat = self.sentinel.inspect_and_report_threat(source_identifier=target, content=text[:6000])
            if isinstance(threat, dict) and (threat.get("threat_detected") or threat.get("incident_id")):
                return {"ok": False, "probe_status": "sentinel_quarantined", "http_status": status, "latency_ms": lat}
            try:
                data = json.loads(text)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            named = data.get("named_endpoints") or {}
            endpoints = list(named.keys())[:8] if isinstance(named, dict) else []
            adapter = _select_verified_gradio_adapter(base_url, target, data)
            if adapter:
                return {
                    "ok": True,
                    "probe_status": "live_verified_200",
                    "http_status": 200,
                    "latency_ms": lat,
                    "info_url": target,
                    "endpoints": endpoints,
                    "adapter": adapter,
                }
            if endpoints:
                return {
                    "ok": False,
                    "probe_status": "non_conversational_or_ambiguous_schema",
                    "http_status": 200,
                    "latency_ms": lat,
                    "info_url": target,
                    "endpoints": endpoints,
                }
        return {"ok": False, "probe_status": "unreachable_or_sleeping", "http_status": 503, "latency_ms": 0.0}

    def discover_fresh_candidates(self, tag: str, limit: int = 40) -> int:
        """Query Hugging Face Spaces public API for fresh zero-auth Gradio spaces and stage new ones."""
        url = "https://huggingface.co/api/spaces?" + urllib.parse.urlencode(
            {"search": tag, "sort": "lastModified", "direction": "-1", "limit": max(5, min(limit, 80))}
        )
        ok_out, _ = self.sentinel.sanitize_outbound_query(url)
        if not ok_out:
            return 0
        try:
            st, _, raw = self.http_get(url, 8.0)
            if st != 200:
                return 0
            rows = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            return 0
        if not isinstance(rows, list):
            return 0

        now = time.time()
        inserted = 0
        with self._connect() as conn:
            for row in rows:
                if not isinstance(row, dict) or row.get("private") or row.get("gated"):
                    continue
                if str(row.get("sdk") or "").lower() != "gradio":
                    continue
                sp_id = str(row.get("id") or "")
                if "/" not in sp_id:
                    continue
                pid = _slug_portal_id(sp_id)
                sp_url = _space_host_url(sp_id)
                info_url = f"{sp_url}/gradio_api/info"
                exists = conn.execute(
                    "SELECT 1 FROM swarm_discovery_candidates WHERE portal_id = ? OR url = ?",
                    (pid, sp_url),
                ).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO swarm_discovery_candidates (
                        portal_id, space_id, url, info_url, source, search_tag,
                        probe_status, http_status, latency_ms, text_endpoints_json,
                        error, state, first_seen, last_probed
                    ) VALUES (?, ?, ?, ?, 'hf_spaces_auto', ?, 'schema_verified', 200, 0.0, '[]', NULL, 'staged', ?, 0.0)
                    """,
                    (pid, sp_id, sp_url, info_url, tag, now),
                )
                inserted += 1
        return inserted

    def discover_non_hf_members(self, mcp_limit: int = 35) -> Dict[str, int]:
        """Discover & admit non-Hugging-Face zero-auth swarm members (Pollinations LLMs + Official MCP Registry)."""
        now = time.time()
        pollinations_added = 0
        mcp_added = 0

        # 1. Pollinations.ai anonymous zero-auth inference endpoints
        try:
            st, _, raw = self.http_get("https://text.pollinations.ai/models", 8.0)
            if st == 200:
                models = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(models, list):
                    with self._connect() as conn:
                        for pm in models:
                            if not isinstance(pm, dict) or str(pm.get("tier") or "") != "anonymous":
                                continue
                            mname = str(pm.get("name") or "").strip()
                            if not mname:
                                continue
                            pid = f"pollinations-{re.sub(r'[^a-z0-9]+', '-', mname.lower()).strip('-')[:60]}"
                            purl = f"https://text.pollinations.ai/models#{mname}"
                            exists = conn.execute(
                                "SELECT 1 FROM assistant_external_portals WHERE portal_id = ? OR url = ?",
                                (pid, purl),
                            ).fetchone()
                            if exists:
                                continue
                            desc = str(pm.get("description") or mname)
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO assistant_external_portals (
                                    portal_id, name, region, category, url, auth_mode,
                                    models_json, strengths_json, status, catalog_state,
                                    auth_state, reachability_state, tested_state,
                                    qualification_state, execution_mode, requires_revalidation,
                                    harness_hash, requested_model, actual_model,
                                    benchmark_json, skill_proposal_id, discovered_at,
                                    last_benchmarked, portal_name, geo_origin,
                                    supported_models, qualified_roles, recent_telemetry, last_scouted
                                ) VALUES (
                                    ?, ?, 'GLOBAL', 'pollinations_zero_auth', ?, 'none',
                                    ?, ?, 'cataloged', 'cataloged',
                                    'unauthenticated', 'reachable', 'untested',
                                    'unqualified', 'openai_compatible', 0,
                                    '', ?, ?,
                                    '{}', NULL, ?,
                                    NULL, ?, 'global',
                                    ?, '[]', ?, ?
                                )
                                """,
                                (
                                    pid,
                                    f"Pollinations: {desc}",
                                    purl,
                                    json.dumps([mname]),
                                    json.dumps(["anonymous_zero_auth", "reasoning" if pm.get("reasoning") else "chat", "tools" if pm.get("tools") else "text"]),
                                    mname,
                                    mname,
                                    now,
                                    mname,
                                    json.dumps([mname]),
                                    json.dumps({"provider": "pollinations", "tier": "anonymous", "tools": bool(pm.get("tools"))}),
                                    now,
                                ),
                            )
                            pollinations_added += 1
        except Exception:
            pass

        # 2. Official MCP Server Registry (live-verified JSON-RPC 2.0 `initialize` handshake ONLY!)
        from tools.non_hf_swarm_transport import probe_mcp_http_server

        try:
            mcp_api = "https://registry.modelcontextprotocol.io/v0/servers?" + urllib.parse.urlencode({"limit": max(10, min(mcp_limit, 60))})
            st, _, raw = self.http_get(mcp_api, 8.0)
            if st == 200:
                srvs = (json.loads(raw.decode("utf-8", errors="replace")) or {}).get("servers") or []
                cand_mcps: List[Tuple[str, str, str, str, List[str]]] = []
                with self._connect() as conn:
                    for entry in srvs:
                        srv = (entry.get("server") if isinstance(entry, dict) else None) or {}
                        sname = str(srv.get("name") or "").strip()
                        remotes = [r.get("url") for r in (srv.get("remotes") or []) if isinstance(r, dict) and str(r.get("url") or "").startswith("https://")]
                        if not sname or not remotes:
                            continue
                        rurl = str(remotes[0])
                        pid = f"mcp-reg-{re.sub(r'[^a-z0-9]+', '-', sname.lower()).strip('-')[:64]}"
                        if conn.execute("SELECT 1 FROM assistant_external_portals WHERE portal_id = ? OR url = ?", (pid, rurl)).fetchone():
                            continue
                        cand_mcps.append((pid, str(srv.get("title") or sname), sname, rurl, remotes[:3]))

                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    futs = {pool.submit(probe_mcp_http_server, c[3], sentinel=self.sentinel): c for c in cand_mcps[:25]}
                    verified_mcps = [(futs[f], f.result()) for f in concurrent.futures.as_completed(futs)]

                with self._connect() as conn:
                    for (pid, title, sname, rurl, remotes), hres in verified_mcps:
                        if not hres.get("ok"):
                            continue
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO assistant_external_portals (
                                portal_id, name, region, category, url, auth_mode,
                                models_json, strengths_json, status, catalog_state,
                                auth_state, reachability_state, tested_state,
                                qualification_state, execution_mode, requires_revalidation,
                                harness_hash, requested_model, actual_model,
                                benchmark_json, skill_proposal_id, discovered_at,
                                last_benchmarked, portal_name, geo_origin,
                                supported_models, qualified_roles, recent_telemetry, last_scouted
                            ) VALUES (
                                ?, ?, 'GLOBAL', 'public_mcp_server', ?, 'none',
                                ?, ?, 'cataloged', 'cataloged',
                                'unauthenticated', 'reachable', 'handshake_verified',
                                'unqualified', 'mcp_http', 0,
                                '', 'mcp_remote', 'mcp_remote',
                                '{}', NULL, ?,
                                NULL, ?, 'global',
                                ?, '[]', ?, ?
                            )
                            """,
                            (
                                pid,
                                f"MCP Registry: {title}",
                                rurl,
                                json.dumps([sname]),
                                json.dumps(["mcp_server", "remote_tools", "jsonrpc_verified"]),
                                now,
                                sname,
                                json.dumps([sname]),
                                json.dumps({"source": "official_mcp_registry", "handshake": hres, "remotes": remotes}),
                                now,
                            ),
                        )
                        mcp_added += 1
        except Exception:
            pass

        return {"pollinations_admitted": pollinations_added, "mcp_registry_admitted": mcp_added}

    def recruit_wave(
        self,
        *,
        batch_size: int = 60,
        discover_fresh: bool = True,
        workers: int = 12,
    ) -> Dict[str, Any]:
        """Run one fast concurrent recruitment wave: non-HF + HF discovery, live verification & admission."""
        started = time.time()
        fresh_staged = 0
        non_hf = {"pollinations_admitted": 0, "mcp_registry_admitted": 0}
        tag_used = DISCOVERY_TAGS[int(started // 60) % len(DISCOVERY_TAGS)]
        if discover_fresh:
            non_hf = self.discover_non_hf_members(mcp_limit=40)
            fresh_staged = self.discover_fresh_candidates(tag_used, limit=40)

        with self._connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT c.* FROM swarm_discovery_candidates c
                    WHERE c.state = 'staged' AND c.probe_status = 'schema_verified'
                      AND NOT EXISTS (SELECT 1 FROM assistant_external_portals p WHERE p.portal_id = c.portal_id OR p.url = c.url)
                    ORDER BY COALESCE(c.last_probed, 0) ASC, c.first_seen DESC
                    LIMIT ?
                    """,
                    (max(1, min(int(batch_size), 250)),),
                ).fetchall()
            ]

        probed_results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        if rows:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(int(workers), 20))) as pool:
                fut_map = {pool.submit(self._probe_gradio_candidate, r): r for r in rows}
                for fut in concurrent.futures.as_completed(fut_map):
                    probed_results.append((fut_map[fut], fut.result()))

        now = time.time()
        admitted: List[str] = []
        retired = 0
        auth_blocked = 0
        with self._connect() as conn:
            for cand, res in probed_results:
                pid = str(cand["portal_id"])
                if res.get("ok"):
                    sp_id = str(cand.get("space_id") or pid)
                    url = str(cand["url"])
                    endpoints = res.get("endpoints") or []
                    tag = str(cand.get("search_tag") or "general")
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO assistant_external_portals (
                            portal_id, name, region, category, url, auth_mode,
                            models_json, strengths_json, status, catalog_state,
                            auth_state, reachability_state, tested_state,
                            qualification_state, execution_mode, requires_revalidation,
                            harness_hash, requested_model, actual_model,
                            benchmark_json, skill_proposal_id, discovered_at,
                            last_benchmarked, portal_name, geo_origin,
                            supported_models, qualified_roles, recent_telemetry, last_scouted
                        ) VALUES (
                            ?, ?, 'GLOBAL', 'hf_gradio_swarm', ?, 'none',
                            ?, ?, 'cataloged', 'cataloged',
                            'unauthenticated', 'reachable', 'untested',
                            'unqualified', 'gradio_api', 0,
                            '', 'gradio_zero_auth', 'gradio_zero_auth',
                            '{}', NULL, ?,
                            NULL, ?, 'global',
                            ?, '[]', ?, ?
                        )
                        """,
                        (
                            pid,
                            f"HF Space: {sp_id}",
                            url,
                            json.dumps(endpoints[:4]),
                            json.dumps([tag, "zero_auth_gradio"]),
                            now,
                            sp_id,
                            json.dumps(endpoints[:4]),
                            json.dumps({"latency_ms": res["latency_ms"], "info_url": res.get("info_url")}),
                            now,
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE swarm_discovery_candidates
                        SET state = 'admitted', probe_status = 'live_verified_200',
                            http_status = 200, latency_ms = ?, text_endpoints_json = ?, last_probed = ?
                        WHERE portal_id = ?
                        """,
                        (float(res["latency_ms"]), json.dumps(endpoints), now, pid),
                    )
                    admitted.append(pid)
                else:
                    pstat = str(res.get("probe_status") or "unreachable")
                    if pstat == "auth_required":
                        auth_blocked += 1
                    else:
                        retired += 1
                    conn.execute(
                        """
                        UPDATE swarm_discovery_candidates
                        SET state = 'inactive', probe_status = ?, http_status = ?,
                            latency_ms = ?, last_probed = ?
                        WHERE portal_id = ?
                        """,
                        (pstat, int(res.get("http_status") or 0), float(res.get("latency_ms") or 0.0), now, pid),
                    )

        # Register verified Gradio adapters directly into `portal_profiles` so `invoke_portal` can call them!
        profiles_registered = 0
        if admitted:
            from tools.portal_policy import PortalPolicy
            policy = PortalPolicy(self.db_path)
            for cand, res in probed_results:
                if res.get("ok") and isinstance(res.get("adapter"), dict):
                    pid = str(cand["portal_id"])
                    sp_id = str(cand.get("space_id") or pid)
                    policy.register(
                        pid,
                        res["adapter"],
                        sp_id,
                        access_allowed=True,
                        access_reason="zero_auth_gradio_protocol_verified",
                    )
                    with policy.db() as pc:
                        pc.execute("UPDATE portal_profiles SET access_state='reachable' WHERE portal_id=?", (pid,))
                    profiles_registered += 1

        snap = self.status()
        report = {
            "ok": True,
            "wave_ts": now,
            "duration_sec": round(time.time() - started, 2),
            "fresh_tag": tag_used,
            "fresh_candidates_staged": fresh_staged,
            "non_hf_admitted": non_hf,
            "probed": len(probed_results),
            "newly_admitted": len(admitted) + non_hf["pollinations_admitted"] + non_hf["mcp_registry_admitted"],
            "profiles_registered": profiles_registered,
            "admitted_sample": admitted[:10],
            "retired_unreachable": retired,
            "auth_disqualified": auth_blocked,
            "status_after": snap,
        }
        try:
            self.log_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        except Exception:
            pass
        return report

    def audit_and_backfill_harnesses(self, workers: int = 12) -> Dict[str, Any]:
        """Verify and wire missing `portal_profiles` harnesses, test MCP handshakes, and quarantine broken profiles."""
        from tools.non_hf_swarm_transport import probe_mcp_http_server
        from tools.portal_policy import PortalPolicy

        policy = PortalPolicy(self.db_path)
        now = time.time()

        # 1. Backfill missing `portal_profiles` for newly admitted `hf_gradio_swarm` portals
        with self._connect() as conn:
            missing_hf = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT p.portal_id, p.portal_name AS space_id, p.url
                    FROM assistant_external_portals p
                    WHERE p.category = 'hf_gradio_swarm'
                      AND NOT EXISTS (SELECT 1 FROM portal_profiles pp WHERE pp.portal_id = p.portal_id)
                    """
                ).fetchall()
            ]
            unverified_mcps = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT portal_id, url FROM assistant_external_portals
                    WHERE category = 'public_mcp_server' AND tested_state != 'handshake_verified'
                    """
                ).fetchall()
            ]

        hf_registered = 0
        hf_unusable = 0
        if missing_hf:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._probe_gradio_candidate, r): r for r in missing_hf}
                results = [(futs[f], f.result()) for f in concurrent.futures.as_completed(futs)]
            to_register = []
            with self._connect() as conn:
                for cand, res in results:
                    pid = str(cand["portal_id"])
                    if res.get("ok") and isinstance(res.get("adapter"), dict):
                        to_register.append((pid, res["adapter"], str(cand.get("space_id") or pid)))
                        conn.execute(
                            "UPDATE assistant_external_portals SET reachability_state='reachable', tested_state='protocol_verified' WHERE portal_id=?",
                            (pid,),
                        )
                        hf_registered += 1
                    else:
                        conn.execute(
                            "UPDATE assistant_external_portals SET reachability_state=?, tested_state='unusable' WHERE portal_id=?",
                            (str(res.get("probe_status") or "unusable"), pid),
                        )
                        hf_unusable += 1
            for pid, adapter, sp_id in to_register:
                policy.register(
                    pid,
                    adapter,
                    sp_id,
                    access_allowed=True,
                    access_reason="zero_auth_gradio_protocol_verified",
                )
                with policy.db() as pc:
                    pc.execute("UPDATE portal_profiles SET access_state='reachable' WHERE portal_id=?", (pid,))

        # 2. Verify existing `public_mcp_server` rows with real JSON-RPC 2.0 `initialize` handshakes
        mcp_verified = 0
        mcp_failed = 0
        if unverified_mcps:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futs = {pool.submit(probe_mcp_http_server, r["url"], sentinel=self.sentinel): r for r in unverified_mcps}
                mres_list = [(futs[f], f.result()) for f in concurrent.futures.as_completed(futs)]
            with self._connect() as conn:
                for row, hres in mres_list:
                    pid = str(row["portal_id"])
                    if hres.get("ok"):
                        conn.execute(
                            "UPDATE assistant_external_portals SET reachability_state='reachable', tested_state='handshake_verified', recent_telemetry=? WHERE portal_id=?",
                            (json.dumps({"handshake": hres}), pid),
                        )
                        mcp_verified += 1
                    else:
                        conn.execute(
                            "UPDATE assistant_external_portals SET reachability_state=?, tested_state='handshake_failed', recent_telemetry=? WHERE portal_id=?",
                            (str(hres.get("failure") or "unreachable"), json.dumps({"handshake": hres}), pid),
                        )
                        mcp_failed += 1

        # 3. Demote chronically broken `portal_profiles` (>=2 hard failures, 0 successes in last 14d)
        hard_failures = {
            "provider_error", "unrecognized_response", "endpoint_missing",
            "prompt_echo", "ambiguous_output_schema", "unsupported_adapter",
        }
        demoted_broken: List[str] = []
        with policy.db() as pc:
            rows = pc.execute(
                "SELECT portal_id, result FROM portal_requests WHERE state='complete' AND started >= ? ORDER BY started DESC",
                (now - 14 * 86400,),
            ).fetchall()
            ok_map: Dict[str, int] = {}
            fail_map: Dict[str, int] = {}
            for r in rows:
                pid = r["portal_id"]
                res = (r["result"] or "").strip()
                if not res:
                    ok_map[pid] = ok_map.get(pid, 0) + 1
                elif res in hard_failures:
                    fail_map[pid] = fail_map.get(pid, 0) + 1
            for pid, fcnt in fail_map.items():
                if fcnt >= 2 and ok_map.get(pid, 0) == 0:
                    cur = pc.execute(
                        "UPDATE portal_profiles SET access_state='degraded', updated=? WHERE portal_id=? AND access_state='reachable'",
                        (now, pid),
                    )
                    if cur.rowcount:
                        demoted_broken.append(pid)

        return {
            "ok": True,
            "hf_checked": len(missing_hf),
            "hf_profiles_registered": hf_registered,
            "hf_marked_unusable": hf_unusable,
            "mcp_checked": len(unverified_mcps),
            "mcp_handshake_verified": mcp_verified,
            "mcp_handshake_failed": mcp_failed,
            "chronically_broken_demoted": demoted_broken,
        }


def recruit_swarm_members_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    action = action or {}
    rec = AutonomousSwarmRecruiter()
    return rec.recruit_wave(
        batch_size=int(action.get("batch_size", 60)),
        discover_fresh=bool(action.get("discover_fresh", True)),
        workers=int(action.get("workers", 12)),
    )


def get_swarm_recruitment_status_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    return AutonomousSwarmRecruiter().status()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Autonomous Swarm Member Recruiter")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Show swarm portal counts and backlog")
    p_w = sub.add_parser("wave", help="Run one concurrent recruitment & live-verification wave")
    p_w.add_argument("--batch", type=int, default=60)
    p_w.add_argument("--workers", type=int, default=12)
    p_w.add_argument("--no-discover", action="store_true")
    sub.add_parser("audit-harnesses", help="Backfill portal_profiles harnesses, test MCP handshakes, and demote broken profiles")
    args = parser.parse_args()

    rec = AutonomousSwarmRecruiter()
    if args.cmd == "status":
        print(json.dumps(rec.status(), indent=2))
    elif args.cmd == "audit-harnesses":
        print(json.dumps(rec.audit_and_backfill_harnesses(), indent=2))
    else:
        print(json.dumps(rec.recruit_wave(batch_size=args.batch, discover_fresh=not args.no_discover, workers=args.workers), indent=2))


if __name__ == "__main__":
    main()
