"""External Mind Scout & Capability Adapter (Tools & Collective Scaffolding).

Discovers, catalogs, and benchmarks external free/regional LLM chat portals
with strict execution identity and provenance enforcement:

1. Explicit ``execution_mode`` required (``"live"``, ``"simulation"``, or ``"replay"``).
   Production entrypoints (``execution_mode="live"``) fail clearly when a real
   authenticated/reachable adapter is unavailable and NEVER silently invoke
   synthetic callbacks.
2. Separates 5 distinct lifecycle states:
   - ``catalog_state``: ``"cataloged"`` | ``"uncataloged"``
   - ``auth_state``: ``"authenticated"`` | ``"unauthenticated"`` | ``"expired"`` | ``"unknown"``
   - ``reachability_state``: ``"reachable"`` | ``"unreachable"`` | ``"unprobed"``
   - ``tested_state``: ``"tested"`` | ``"untested"`` | ``"failed"``
   - ``qualification_state``: ``"qualified"`` | ``"unqualified"`` | ``"revalidation_required"`` | ``"invalidated"``
3. Marks any qualification derived from synthetic or provenance-free results as
   ``revalidation_required`` while preserving all underlying historical records.
4. Corrects the hierarchical simulation methodology so injected fault labels remain
   strictly private to the evaluator, aggregation operates only on observable outputs,
   quorum/timeout/tie/remainder/all-failing/cancellation states are enforced, and
   information loss (including discarded correct minority findings) is measured.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
from tools.portal_policy import DEFAULT_DB
PROPOSED_SKILLS_DIR = PROJECT_ROOT / "skills" / "proposed"
ACTIVE_SKILLS_DIR = PROJECT_ROOT / "skills"

VALID_EXECUTION_MODES = {"live", "simulation", "replay"}
PRIVATE_FAULT_LABEL_KEYS = {
    "variant",
    "valid_invariants",
    "fault_label",
    "is_correct",
    "ground_truth",
    "answer_key",
    "injected_fault",
}


def compute_scout_harness_hash() -> str:
    """Compute a deterministic SHA-256 digest of the scout and verifier files."""
    h = hashlib.sha256()
    for rel in (
        "tools/external_mind_scout.py",
        "tools/capability_registry.py",
        "tools/sabrina_workflow_verifier.py",
    ):
        p = PROJECT_ROOT / rel
        if p.exists():
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


# Curated catalog of external LLM portals (cataloged products, NOT automatically qualified)
EXTERNAL_PORTAL_CATALOG: list[dict[str, Any]] = [
    {
        "portal_id": "deepseek-chat-china",
        "name": "DeepSeek Chat (V3 / R1)",
        "region": "CN",
        "category": "chinese_frontier",
        "url": "https://chat.deepseek.com/",
        "auth_mode": "free_account_or_oauth",
        "models": ["DeepSeek-V3", "DeepSeek-R1"],
        "strengths": ["algorithmic_coding", "mathematical_reasoning", "root_cause_debugging"],
        "Free_Tier_Type": "unlimited_web_chat",
        "notes": "Strong open-weights reasoning and coding portal.",
    },
    {
        "portal_id": "qwen-chat-china",
        "name": "Qwen Chat (Alibaba Cloud)",
        "region": "CN",
        "category": "chinese_frontier",
        "url": "https://chat.qwen.ai/",
        "auth_mode": "guest_or_free_account",
        "models": ["Qwen2.5-Max", "Qwen2.5-Coder-32B-Instruct", "QwQ-32B"],
        "strengths": ["polyglot_coding", "structured_json", "tool_calling", "long_context"],
        "Free_Tier_Type": "free_web_portal",
        "notes": "Direct web portal for Qwen2.5-Max and QwQ-32B.",
    },
    {
        "portal_id": "kimi-moonshot-china",
        "name": "Kimi.ai (Moonshot AI)",
        "region": "CN",
        "category": "chinese_frontier",
        "url": "https://kimi.moonshot.cn/",
        "auth_mode": "free_account",
        "models": ["Kimi-k1.5", "Moonshot-v1-128k"],
        "strengths": ["long_context_research", "multi_file_synthesis", "web_grounding"],
        "Free_Tier_Type": "free_web_portal",
        "notes": "200k-2M token context window research portal.",
    },
    {
        "portal_id": "chatglm-zhipu-china",
        "name": "ChatGLM / Qingyan (Zhipu AI)",
        "region": "CN",
        "category": "chinese_frontier",
        "url": "https://chatglm.cn/",
        "auth_mode": "guest_or_free_account",
        "models": ["GLM-4-Plus", "GLM-Zero-Preview", "CodeGeeX-4"],
        "strengths": ["code_generation", "agentic_browsing", "bilingual_analysis"],
        "Free_Tier_Type": "free_web_portal",
        "notes": "Includes CodeGeeX-4 and GLM-4 reasoning.",
    },
    {
        "portal_id": "doubao-bytedance-china",
        "name": "Doubao (ByteDance)",
        "region": "CN",
        "category": "chinese_frontier",
        "url": "https://www.doubao.com/chat/",
        "auth_mode": "guest_or_free_account",
        "models": ["Doubao-1.5-Pro"],
        "strengths": ["low_latency_drafting", "summarization", "fast_triage"],
        "Free_Tier_Type": "free_web_portal",
        "notes": "High-speed drafting and triage portal.",
    },
    {
        "portal_id": "yi-lingyiwanwu-china",
        "name": "Yi Chat / Wanzhi (01.AI)",
        "region": "CN",
        "category": "chinese_frontier",
        "url": "https://www.wanzhi.com/",
        "auth_mode": "free_account",
        "models": ["Yi-Lightning"],
        "strengths": ["fast_reasoning", "code_review", "concise_synthesis"],
        "Free_Tier_Type": "free_web_portal",
        "notes": "Yi-Lightning competitive reasoning model.",
    },
    {
        "portal_id": "duckduckgo-ai-chat",
        "name": "DuckDuckGo AI Chat (Anonymous)",
        "region": "US/Global",
        "category": "anonymous_free_portal",
        "url": "https://duckduckgo.com/?q=DuckDuckGo+AI+Chat&ia=chat&duckai=1",
        "auth_mode": "no_login_anonymous",
        "models": ["GPT-4o-mini", "Claude-3-Haiku", "Llama-3.3-70B", "Mistral-Small-3"],
        "strengths": ["zero_auth_triage", "fast_critique", "privacy_preserving_queries"],
        "Free_Tier_Type": "no_login_session",
        "notes": "Zero-login anonymous access to multiple fast models.",
    },
    {
        "portal_id": "huggingface-chat-open",
        "name": "HuggingChat (Open Weights Portal)",
        "region": "US/EU",
        "category": "open_weights_portal",
        "url": "https://huggingface.co/chat/",
        "auth_mode": "guest_or_hf_account",
        "models": ["Qwen2.5-72B-Instruct", "Llama-3.3-70B-Instruct", "DeepSeek-R1-Distill-Qwen-32B"],
        "strengths": ["open_weights_verification", "code_synthesis", "web_search"],
        "Free_Tier_Type": "guest_or_free_account",
        "notes": "Hosts 70B+ open-weight instruct and reasoning models.",
    },
    {
        "portal_id": "lmarena-chatbot-arena",
        "name": "LMSYS Chatbot Arena (Direct Chat)",
        "region": "Global",
        "category": "benchmark_arena_portal",
        "url": "https://lmarena.ai/",
        "auth_mode": "no_login_session",
        "models": ["Frontier-Arena-Pool"],
        "strengths": ["blind_ab_comparison", "frontier_model_probing"],
        "Free_Tier_Type": "no_login_rate_limited",
        "notes": "Useful for side-by-side blind comparison.",
    },
    {
        "portal_id": "mistral-le-chat-eu",
        "name": "Le Chat (Mistral AI)",
        "region": "EU",
        "category": "european_frontier",
        "url": "https://chat.mistral.ai/chat",
        "auth_mode": "free_account",
        "models": ["Mistral-Large-2", "Codestral"],
        "strengths": ["fast_code_completion", "structured_extraction", "multilingual"],
        "Free_Tier_Type": "free_web_portal",
        "notes": "High throughput (~1000 tok/s Flash) and Codestral coding.",
    },
]

# Standard evaluation suite used to benchmark an external portal
PORTAL_BENCHMARK_SUITE: list[dict[str, Any]] = [
    {
        "task_id": "code_clamp_nan",
        "dimension": "coding_correctness",
        "prompt": (
            "Write a Python function `clamp_ratio(val: float, lo: float = 0.0, hi: float = 1.0) -> float` "
            "that raises ValueError on NaN or when lo > hi, and clamps val into [lo, hi]. Return ONLY a python code block."
        ),
        "required_tokens": ["def clamp_ratio", "isnan", "ValueError", "return"],
    },
    {
        "task_id": "audit_critique_race",
        "dimension": "adversarial_critique",
        "prompt": (
            "Critique this snippet for concurrency bugs: "
            "`if not lock_file.exists(): lock_file.write_text(pid)` "
            "Identify the exact vulnerability class and the atomic fix in 2 sentences."
        ),
        "required_tokens": ["TOCTOU", "race", "atomic"],
    },
]


def _validate_execution_mode(execution_mode: str) -> str:
    mode = str(execution_mode or "").strip().lower()
    if mode not in VALID_EXECUTION_MODES:
        raise ValueError(
            f"execution_mode must be explicitly one of {sorted(VALID_EXECUTION_MODES)}; got {execution_mode!r}"
        )
    return mode


def _authoritative_clamp_ratio_check(code_text: str) -> dict[str, Any]:
    from tools.portal_sandbox import clamp_check
    return clamp_check(code_text)


class ExternalMindScout:
    """Discovers, benchmarks, and manages external AI chat portals with strict provenance."""

    def __init__(self, db_path: Path | str = DEFAULT_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS assistant_external_portals (
                    portal_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    region TEXT NOT NULL,
                    category TEXT NOT NULL,
                    url TEXT NOT NULL,
                    auth_mode TEXT NOT NULL,
                    models_json TEXT NOT NULL DEFAULT '[]',
                    strengths_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'cataloged',
                    catalog_state TEXT NOT NULL DEFAULT 'cataloged',
                    auth_state TEXT NOT NULL DEFAULT 'unauthenticated',
                    reachability_state TEXT NOT NULL DEFAULT 'unprobed',
                    tested_state TEXT NOT NULL DEFAULT 'untested',
                    qualification_state TEXT NOT NULL DEFAULT 'unqualified',
                    execution_mode TEXT NOT NULL DEFAULT 'unknown',
                    requires_revalidation INTEGER NOT NULL DEFAULT 0,
                    harness_hash TEXT NOT NULL DEFAULT '',
                    requested_model TEXT NOT NULL DEFAULT '',
                    actual_model TEXT NOT NULL DEFAULT 'unknown',
                    benchmark_json TEXT NOT NULL DEFAULT '{}',
                    skill_proposal_id TEXT,
                    discovered_at REAL NOT NULL,
                    last_benchmarked REAL
                );
                """
            )
            # Ensure new and legacy columns exist on older databases
            existing_cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(assistant_external_portals)").fetchall()
            }
            additions = [
                ("name", "TEXT NOT NULL DEFAULT ''"),
                ("portal_name", "TEXT NOT NULL DEFAULT ''"),
                ("region", "TEXT NOT NULL DEFAULT 'global'"),
                ("geo_origin", "TEXT NOT NULL DEFAULT 'global'"),
                ("models_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("supported_models", "TEXT NOT NULL DEFAULT '[]'"),
                ("strengths_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("qualified_roles", "TEXT NOT NULL DEFAULT '[]'"),
                ("benchmark_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("recent_telemetry", "TEXT NOT NULL DEFAULT '{}'"),
                ("discovered_at", "REAL NOT NULL DEFAULT 0.0"),
                ("last_benchmarked", "REAL"),
                ("last_scouted", "REAL"),
                ("catalog_state", "TEXT NOT NULL DEFAULT 'cataloged'"),
                ("auth_state", "TEXT NOT NULL DEFAULT 'unauthenticated'"),
                ("reachability_state", "TEXT NOT NULL DEFAULT 'unprobed'"),
                ("tested_state", "TEXT NOT NULL DEFAULT 'untested'"),
                ("qualification_state", "TEXT NOT NULL DEFAULT 'unqualified'"),
                ("execution_mode", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("requires_revalidation", "INTEGER NOT NULL DEFAULT 0"),
                ("harness_hash", "TEXT NOT NULL DEFAULT ''"),
                ("requested_model", "TEXT NOT NULL DEFAULT ''"),
                ("actual_model", "TEXT NOT NULL DEFAULT 'unknown'"),
            ]
            for col_name, col_def in additions:
                if col_name not in existing_cols:
                    conn.execute(
                        f"ALTER TABLE assistant_external_portals ADD COLUMN {col_name} {col_def}"
                    )

    def revalidate_synthetic_records(self) -> dict[str, Any]:
        """Mark all synthetic or provenance-free portal qualifications and skills as requiring revalidation.

        Preserves all underlying benchmark, recent_telemetry, and proposal records in SQLite.
        """
        current_hash = compute_scout_harness_hash()
        marked_portals: list[str] = []
        marked_proposals: list[str] = []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM assistant_external_portals").fetchall()
            for r in rows:
                d = dict(r)
                pid = d["portal_id"]
                bjson = {}
                for field_name in ("benchmark_json", "recent_telemetry"):
                    raw_val = d.get(field_name)
                    if raw_val and raw_val != "{}":
                        try:
                            bjson = json.loads(raw_val)
                            break
                        except Exception:
                            bjson = {}
                exec_mode = str(d.get("execution_mode") or bjson.get("execution_mode") or "unknown")
                has_live_provenance = (
                    exec_mode == "live"
                    and bool(bjson.get("provenance_ref"))
                    and not bjson.get("is_simulated")
                )
                hash_mismatch = bool(d.get("harness_hash")) and d.get("harness_hash") != current_hash
                if not has_live_provenance or hash_mismatch:
                    had_prior_benchmark = bool(bjson) or d.get("status") in {
                        "qualified",
                        "benchmarked",
                        "ingested",
                        "skill_proposed",
                        "revalidation_required",
                    }
                    new_qual = "revalidation_required" if had_prior_benchmark else "unqualified"
                    new_status = "revalidation_required" if had_prior_benchmark else "cataloged"
                    bjson["revalidation_required"] = bool(had_prior_benchmark)
                    bjson["revalidation_reason"] = (
                        "harness_changed"
                        if hash_mismatch
                        else "synthetic_or_unprovenanced_history_preserved"
                    )
                    conn.execute(
                        """
                        UPDATE assistant_external_portals
                        SET status = ?,
                            name = CASE WHEN name = '' THEN COALESCE(portal_name, portal_id) ELSE name END,
                            region = CASE WHEN region = '' THEN COALESCE(geo_origin, 'global') ELSE region END,
                            catalog_state = 'cataloged',
                            auth_state = CASE WHEN auth_state = 'authenticated' AND ? THEN 'authenticated' ELSE 'unauthenticated' END,
                            reachability_state = CASE WHEN reachability_state = 'reachable' AND ? THEN 'reachable' ELSE 'unprobed' END,
                            tested_state = CASE WHEN ? THEN 'tested' ELSE 'untested' END,
                            qualification_state = ?,
                            requires_revalidation = ?,
                            benchmark_json = ?
                        WHERE portal_id = ?
                        """,
                        (
                            new_status,
                            1 if has_live_provenance else 0,
                            1 if has_live_provenance else 0,
                            1 if has_live_provenance else 0,
                            new_qual,
                            1 if had_prior_benchmark else 0,
                            json.dumps(bjson, ensure_ascii=False),
                            pid,
                        ),
                    )
                    if had_prior_benchmark:
                        marked_portals.append(pid)

            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "assistant_skill_proposals" in tables:
                prop_rows = conn.execute(
                    "SELECT * FROM assistant_skill_proposals"
                ).fetchall()
                for pr in prop_rows:
                    pd = dict(pr)
                    sname = str(pd.get("skill_name") or pd.get("name") or pd.get("slug") or "")
                    pid_col = str(pd.get("id") or pd.get("proposal_id") or "")
                    if sname.startswith("portal-") and pd.get("status") in ("approved", "pending", "active"):
                        id_field = "id" if "id" in pd else "proposal_id"
                        conn.execute(
                            f"UPDATE assistant_skill_proposals SET status = 'revalidation_required' WHERE {id_field} = ?",
                            (pid_col,),
                        )
                        marked_proposals.append(pid_col)

        return {
            "ok": True,
            "harness_hash": current_hash,
            "portals_marked_revalidation_required": marked_portals,
            "proposals_marked_revalidation_required": marked_proposals,
        }

    def sync_catalog(self) -> dict[str, Any]:
        """Seed or update the external portal catalog without overwriting live qualification state."""
        now = time.time()
        inserted = 0
        updated = 0
        with self._connect() as conn:
            for item in EXTERNAL_PORTAL_CATALOG:
                row = conn.execute(
                    "SELECT portal_id FROM assistant_external_portals WHERE portal_id = ?",
                    (item["portal_id"],),
                ).fetchone()
                if not row:
                    conn.execute(
                        """
                        INSERT INTO assistant_external_portals
                        (portal_id, name, portal_name, region, geo_origin, category, url, auth_mode,
                         models_json, supported_models, strengths_json, status, catalog_state,
                         auth_state, reachability_state, tested_state, qualification_state,
                         execution_mode, requires_revalidation, discovered_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cataloged', 'cataloged', 'unauthenticated',
                                'unprobed', 'untested', 'unqualified', 'unknown', 0, ?)
                        """,
                        (
                            item["portal_id"],
                            item["name"],
                            item["name"],
                            item["region"],
                            item["region"],
                            item["category"],
                            item["url"],
                            item["auth_mode"],
                            json.dumps(item["models"]),
                            json.dumps(item["models"]),
                            json.dumps(item["strengths"]),
                            now,
                        ),
                    )
                    inserted += 1
                else:
                    conn.execute(
                        """
                        UPDATE assistant_external_portals
                        SET name = ?, portal_name = ?, region = ?, geo_origin = ?,
                            category = ?, url = ?, auth_mode = ?,
                            models_json = ?, supported_models = ?, strengths_json = ?,
                            catalog_state = 'cataloged'
                        WHERE portal_id = ?
                        """,
                        (
                            item["name"],
                            item["name"],
                            item["region"],
                            item["region"],
                            item["category"],
                            item["url"],
                            item["auth_mode"],
                            json.dumps(item["models"]),
                            json.dumps(item["models"]),
                            json.dumps(item["strengths"]),
                            item["portal_id"],
                        ),
                    )
                    updated += 1
        return {
            "ok": True,
            "catalog_size": len(EXTERNAL_PORTAL_CATALOG),
            "inserted": inserted,
            "updated": updated,
        }

    def list_portals(
        self,
        category: str = "",
        status: str = "",
        qualification_state: str = "",
    ) -> list[dict[str, Any]]:
        self.sync_catalog()
        query = "SELECT * FROM assistant_external_portals WHERE 1=1"
        params: list[Any] = []
        if category:
            query += " AND category = ?"
            params.append(category)
        if status:
            query += " AND status = ?"
            params.append(status)
        if qualification_state:
            query += " AND qualification_state = ?"
            params.append(qualification_state)
        query += " ORDER BY region ASC, portal_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["models"] = json.loads(d.pop("models_json", "[]") or "[]")
            d["strengths"] = json.loads(d.pop("strengths_json", "[]") or "[]")
            d["benchmark"] = json.loads(d.pop("benchmark_json", "{}") or "{}")
            d["requires_revalidation"] = bool(d.get("requires_revalidation"))
            out.append(d)
        return out

    def invalidate_on_change(
        self,
        portal_id: str,
        *,
        new_model: str | None = None,
        new_url: str | None = None,
        new_harness_hash: str | None = None,
    ) -> dict[str, Any]:
        """Invalidate applicable qualification when model, endpoint, adapter, or harness changes."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_external_portals WHERE portal_id = ?",
                (portal_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown portal_id: {portal_id}")
            d = dict(row)
            reasons: list[str] = []
            if new_model is not None and d.get("actual_model") not in ("", "unknown", new_model):
                reasons.append(f"model_changed:{d.get('actual_model')}->{new_model}")
            if new_url is not None and d.get("url") != new_url:
                reasons.append(f"endpoint_changed:{d.get('url')}->{new_url}")
            expected_hash = new_harness_hash or compute_scout_harness_hash()
            if d.get("harness_hash") and d.get("harness_hash") != expected_hash:
                reasons.append("harness_hash_changed")
            if reasons:
                conn.execute(
                    """
                    UPDATE assistant_external_portals
                    SET qualification_state = 'invalidated',
                        requires_revalidation = 1,
                        status = 'revalidation_required'
                    WHERE portal_id = ?
                    """,
                    (portal_id,),
                )
            return {
                "portal_id": portal_id,
                "invalidated": bool(reasons),
                "reasons": reasons,
                "qualification_state": "invalidated" if reasons else d.get("qualification_state"),
            }

    def benchmark_portal(
        self,
        portal_id: str,
        *,
        execution_mode: str = "live",
        live_probe_fn: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Benchmark a portal under an explicit execution_mode ('live', 'simulation', 'replay').

        In 'live' mode, requires a real ``live_probe_fn`` adapter and rejects any simulated
        response. In 'simulation' mode, never marks the portal as qualified.
        """
        mode = _validate_execution_mode(execution_mode)
        if mode == "live":
            raise RuntimeError("Legacy two-task qualification retired. Use tools.portal_cli evaluate with a required capability; callbacks cannot grant live qualifications.")
        self.sync_catalog()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_external_portals WHERE portal_id = ?",
                (portal_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": f"Unknown portal_id: {portal_id}"}

        portal = dict(row)
        models = json.loads(portal.get("models_json") or "[]")
        requested_model = models[0] if models else "default"

        if mode == "live" and live_probe_fn is None:
            raise RuntimeError(
                f"Live adapter unavailable for portal '{portal_id}'; "
                "production entrypoints must not silently invoke synthetic callbacks."
            )

        task_results: list[dict[str, Any]] = []
        passed_tasks = 0
        observed_deliveries = 0
        actual_model_observed = "unknown"

        for task in PORTAL_BENCHMARK_SUITE:
            t0 = time.perf_counter()
            if mode == "live":
                raw_res = live_probe_fn(portal, task)  # type: ignore[misc]
                if not isinstance(raw_res, dict):
                    raise ValueError("Live adapter returned malformed non-dict response")
                if raw_res.get("is_simulated") or raw_res.get("execution_mode") == "simulation":
                    raise ValueError(
                        f"Simulated response rejected in live execution_mode for portal '{portal_id}'"
                    )
                reply = str(raw_res.get("reply") or "")
                latency_ms = float(
                    raw_res.get("latency_ms") or round((time.perf_counter() - t0) * 1000.0, 2)
                )
                delivery = str(raw_res.get("delivery") or ("observed" if reply.strip() else "missing"))
                returned_model = str(raw_res.get("actual_model") or "unknown").strip() or "unknown"
                if returned_model != "unknown":
                    actual_model_observed = returned_model
            else:
                # Explicit simulation/replay mode: never used for qualification
                reply = self._generate_portal_response(portal_id, task["task_id"])
                latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                delivery = "simulated"
                actual_model_observed = "unknown"

            if delivery == "observed":
                observed_deliveries += 1

            if not reply.strip():
                verdict_status = "missing"
                task_passed = False
                check_detail: dict[str, Any] = {"status": "missing"}
            elif task["task_id"] == "code_clamp_nan":
                check_detail = _authoritative_clamp_ratio_check(reply)
                task_passed = bool(check_detail["passed"])
                verdict_status = str(check_detail["status"])
            else:
                matched = [
                    tok for tok in task["required_tokens"] if tok.lower() in reply.lower()
                ]
                coverage = len(matched) / max(1, len(task["required_tokens"]))
                task_passed = coverage >= 1.0
                verdict_status = "verified" if task_passed else "failed"
                check_detail = {"matched_tokens": matched, "token_coverage": round(coverage, 4)}

            if task_passed:
                passed_tasks += 1

            task_results.append(
                {
                    "task_id": task["task_id"],
                    "dimension": task["dimension"],
                    "delivery": delivery,
                    "status": verdict_status,
                    "passed": task_passed,
                    "latency_ms": latency_ms,
                    "requested_model": requested_model,
                    "actual_model": actual_model_observed,
                    "check_detail": check_detail,
                }
            )

        total_tasks = max(1, len(task_results))
        delivery_reliability = round(observed_deliveries / total_tasks, 4) if mode == "live" else 0.0
        answer_correctness = round(passed_tasks / total_tasks, 4)
        harness_hash = compute_scout_harness_hash()

        # Qualification requires live execution, 100% observed delivery, and 100% verified checks
        is_qualified = (
            mode == "live"
            and observed_deliveries == total_tasks
            and passed_tasks == total_tasks
        )
        auth_state = "authenticated" if (mode == "live" and observed_deliveries > 0) else "unauthenticated"
        reachability_state = "reachable" if (mode == "live" and observed_deliveries > 0) else "unprobed"
        tested_state = "tested" if mode == "live" else "untested"
        qualification_state = (
            "qualified"
            if is_qualified
            else ("unqualified" if mode == "live" else "revalidation_required")
        )
        requires_revalidation = mode != "live"

        benchmark_summary = {
            "portal_id": portal_id,
            "name": portal["name"],
            "region": portal["region"],
            "execution_mode": mode,
            "is_simulated": mode != "live",
            "requested_provider": portal_id,
            "actual_provider": portal_id if mode == "live" else "simulation_scaffold",
            "requested_model": requested_model,
            "actual_model": actual_model_observed,
            "delivery_reliability": delivery_reliability,
            "answer_correctness": answer_correctness,
            "catalog_state": "cataloged",
            "auth_state": auth_state,
            "reachability_state": reachability_state,
            "tested_state": tested_state,
            "qualification_state": qualification_state,
            "requires_revalidation": requires_revalidation,
            "harness_hash": harness_hash,
            "provenance_ref": f"live_bench:{portal_id}:{int(time.time())}" if mode == "live" else None,
            "tasks": task_results,
            "timestamp": time.time(),
        }

        new_status = (
            "qualified"
            if is_qualified
            else ("tested_unqualified" if mode == "live" else "revalidation_required")
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE assistant_external_portals
                SET status = ?,
                    catalog_state = 'cataloged',
                    auth_state = ?,
                    reachability_state = ?,
                    tested_state = ?,
                    qualification_state = ?,
                    execution_mode = ?,
                    requires_revalidation = ?,
                    harness_hash = ?,
                    requested_model = ?,
                    actual_model = ?,
                    benchmark_json = ?,
                    last_benchmarked = ?
                WHERE portal_id = ?
                """,
                (
                    new_status,
                    auth_state,
                    reachability_state,
                    tested_state,
                    qualification_state,
                    mode,
                    1 if requires_revalidation else 0,
                    harness_hash,
                    requested_model,
                    actual_model_observed,
                    json.dumps(benchmark_summary, ensure_ascii=False),
                    benchmark_summary["timestamp"],
                    portal_id,
                ),
            )
        return benchmark_summary

    @staticmethod
    def _generate_portal_response(portal_id: str, task_id: str) -> str:
        if task_id == "code_clamp_nan":
            return (
                "```python\n"
                "import math\n\n"
                "def clamp_ratio(val: float, lo: float = 0.0, hi: float = 1.0) -> float:\n"
                "    if math.isnan(val) or math.isnan(lo) or math.isnan(hi) or lo > hi:\n"
                "        raise ValueError('Invalid bounds or NaN input')\n"
                "    return max(lo, min(hi, float(val)))\n"
                "```"
            )
        return (
            "This code contains a classic TOCTOU (Time-of-Check to Time-of-Use) race condition "
            "because another process can create `lock_file` between `exists()` and `write_text()`. "
            "Fix it using an atomic exclusive file creation (`open(lock_file, 'x')`)."
        )

    def adapt_and_propose_skill(
        self,
        portal_id: str,
        *,
        execution_mode: str = "live",
    ) -> dict[str, Any]:
        """Stage a skill proposal ONLY in skills/proposed/ and only if qualified in live mode."""
        mode = _validate_execution_mode(execution_mode)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_external_portals WHERE portal_id = ?",
                (portal_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": f"Unknown portal_id: {portal_id}"}
        portal = dict(row)
        if mode == "live" and (
            portal.get("qualification_state") != "qualified"
            or bool(portal.get("requires_revalidation"))
        ):
            return {
                "ok": False,
                "status": "blocked_unqualified_portal",
                "error": (
                    f"Portal '{portal_id}' has qualification_state={portal.get('qualification_state')!r} "
                    f"(requires_revalidation={bool(portal.get('requires_revalidation'))}). "
                    "Only live-qualified portals may propose skills."
                ),
            }
        proposal_id = f"prop_portal_{portal_id.replace('-', '_')}_{uuid.uuid4().hex[:6]}"
        skill_slug = f"portal-{portal_id}-specialist"
        PROPOSED_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        skill_dir = PROPOSED_SKILLS_DIR / skill_slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        skill_md = (
            f"---\nname: {skill_slug}\n"
            f"description: Proposed adapter for {portal['name']} ({portal['url']}). Requires operator review.\n---\n"
        )
        skill_path.write_text(skill_md, encoding="utf-8")
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "portal_id": portal_id,
            "skill_name": skill_slug,
            "skill_path": str(skill_path),
            "promoted_to_active": False,
            "requires_operator_review": True,
        }

    def ingest_into_swarm_registry(
        self,
        portal_id: str,
        *,
        execution_mode: str = "live",
        cap_db_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Register a portal into CapabilityRegistry ONLY when live-qualified."""
        mode = _validate_execution_mode(execution_mode)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_external_portals WHERE portal_id = ?",
                (portal_id,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": f"Unknown portal_id: {portal_id}"}
        portal = dict(row)
        if (
            mode != "live"
            or portal.get("qualification_state") != "qualified"
            or bool(portal.get("requires_revalidation"))
        ):
            return {
                "ok": False,
                "status": "rejected_unqualified",
                "portal_id": portal_id,
                "qualification_state": portal.get("qualification_state"),
                "requires_revalidation": bool(portal.get("requires_revalidation")),
                "error": "Synthetic or unverified portals cannot be ingested into production worker selection.",
            }
        return {
            "ok": True,
            "portal_id": portal_id,
            "qualification_state": "qualified",
            "execution_mode": "live",
        }

    def swarm_external_portals(
        self,
        prompts: list[str],
        portal_ids: list[str] | None = None,
        *,
        execution_mode: str = "live",
        live_dispatch_fn: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Dispatch subtasks across external portals with strict execution_mode enforcement."""
        mode = _validate_execution_mode(execution_mode)
        if mode == "live" and live_dispatch_fn is None:
            raise RuntimeError(
                "Live portal dispatch adapter is unavailable; refusing to invoke synthetic portal callbacks in live mode."
            )
        clean_prompts = [str(p).strip() for p in (prompts or []) if str(p).strip()][:4]
        if not clean_prompts:
            return {"ok": False, "error": "At least one non-empty prompt is required"}
        targets = (portal_ids or [p["portal_id"] for p in EXTERNAL_PORTAL_CATALOG[:4]])[:4]
        results: list[dict[str, Any]] = []
        for idx, prompt in enumerate(clean_prompts):
            pid = targets[idx % len(targets)]
            t0 = time.perf_counter()
            if mode == "live":
                res = live_dispatch_fn(pid, prompt)  # type: ignore[misc]
                if res.get("is_simulated") or res.get("execution_mode") == "simulation":
                    raise ValueError(f"Simulated response rejected in live mode for portal {pid}")
                reply = str(res.get("reply") or "")
                status = str(res.get("status") or ("unverified" if reply else "missing"))
                delivery = str(res.get("delivery") or ("observed" if reply else "missing"))
                actual_model = str(res.get("actual_model") or "unknown")
            else:
                reply = f"[SIMULATION ONLY - {pid}] {prompt[:120]}"
                status = "simulated_unverified"
                delivery = "simulated"
                actual_model = "unknown"
            results.append(
                {
                    "index": idx,
                    "portal_id": pid,
                    "execution_mode": mode,
                    "delivery": delivery,
                    "status": status,
                    "actual_model": actual_model,
                    "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
                    "reply": reply,
                }
            )
        return {
            "ok": True,
            "execution_mode": mode,
            "worker_count": len(results),
            "results": results,
        }

    def recruit_portals_via_external_minds(
        self,
        recruiter_portal_id: str = "kimi-moonshot-china",
        query_topic: str = "open inference portals",
        *,
        execution_mode: str = "live",
        live_recruit_fn: Callable[[str, str], list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Discover candidate portals into cataloged state ONLY (never auto-qualified)."""
        mode = _validate_execution_mode(execution_mode)
        if mode == "live" and live_recruit_fn is None:
            raise RuntimeError(
                f"Live recruitment adapter unavailable for '{recruiter_portal_id}'; "
                "refusing to fabricate recruited portals in live mode."
            )
        candidates = live_recruit_fn(recruiter_portal_id, query_topic) if mode == "live" else []
        return {
            "ok": True,
            "execution_mode": mode,
            "recruiter_portal_id": recruiter_portal_id,
            "query_topic": query_topic,
            "newly_cataloged_count": len(candidates),
            "qualification_state": "unqualified",
        }

    def run_scaling_cross_test_matrix(
        self,
        challenge_prompt: str = "Implement clamp_ratio with NaN and bounds check.",
        *,
        execution_mode: str = "simulation",
        live_solver_fn: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run a scaling matrix with explicit execution_mode and independent check verification."""
        mode = _validate_execution_mode(execution_mode)
        if mode == "live" and live_solver_fn is None:
            raise RuntimeError(
                "Live solver adapter is required when execution_mode='live' in run_scaling_cross_test_matrix."
            )
        return {
            "ok": True,
            "execution_mode": mode,
            "is_simulation": mode != "live",
            "qualifies_capabilities": False,
            "challenge_prompt": challenge_prompt,
        }

    # ------------------------------------------------------------------
    # Corrected Hierarchical Simulation Methodology (Section 3)
    # ------------------------------------------------------------------
    @staticmethod
    def _compute_ast_fingerprint(code_str: str) -> str:
        """Compute AST structural hash strictly as a deduplication aid (never proof of correctness)."""
        try:
            clean = code_str.strip()
            if "```" in clean:
                for part in clean.split("```"):
                    s = part.strip()
                    if s.startswith("python"):
                        s = s[len("python") :].strip()
                    if "def " in s:
                        clean = s
                        break
            tree = ast.parse(clean)
            dump_str = ast.dump(tree, annotate_fields=True, include_attributes=False)
            return hashlib.sha256(dump_str.encode("utf-8")).hexdigest()[:16]
        except Exception:
            return "invalid_syntax_" + hashlib.sha256(code_str.encode("utf-8", errors="ignore")).hexdigest()[:8]

    @classmethod
    def _verify_no_fault_label_leakage(cls, record: dict[str, Any]) -> None:
        """Reject any worker or group record that leaks private evaluator fault labels into selection."""
        leaked = PRIVATE_FAULT_LABEL_KEYS.intersection(record.keys())
        if leaked:
            raise ValueError(
                f"Fault-label leakage detected in observable worker payload: {sorted(leaked)}"
            )

    @classmethod
    def _reduce_group_observable(
        cls,
        group_id: str,
        records: list[dict[str, Any]],
        *,
        quorum_ratio: float = 0.67,
        timeout_ms: float = 1500.0,
        enable_hedged_cancellation: bool = True,
        observable_check_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Aggregate a squad or platoon using ONLY observable outputs.

        - Rejects fault-label leakage.
        - Enforces quorum thresholds and timeout behavior.
        - Distinguishes cancellation_requested from cancellation_confirmed.
        - Handles empty groups, remainder groups, ties, and all-failing populations.
        - Preserves disagreements and minority findings.
        """
        if not (0.0 < quorum_ratio <= 1.0):
            raise ValueError(f"quorum_ratio must be in (0.0, 1.0]; got {quorum_ratio}")
        if timeout_ms <= 0:
            raise ValueError(f"timeout_ms must be > 0; got {timeout_ms}")

        actual_size = len(records)
        if actual_size == 0:
            return {
                "group_id": group_id,
                "actual_size": 0,
                "required_quorum": 0,
                "status": "empty_group",
                "selected_candidate": None,
                "selected_ast_fingerprint": None,
                "disagreements": [],
                "minority_findings": [],
                "cancellation_requested_count": 0,
                "cancellation_confirmed_count": 0,
                "cancellation_ineffective_count": 0,
            }

        for rec in records:
            cls._verify_no_fault_label_leakage(rec)

        required_quorum = math.ceil(actual_size * quorum_ratio)
        ordered = sorted(records, key=lambda r: float(r.get("latency_ms", 0.0)))

        valid_responses: list[dict[str, Any]] = []
        failed_count = 0
        timed_out_count = 0
        missing_count = 0
        malformed_count = 0
        cancel_requested = 0
        cancel_confirmed = 0
        cancel_ineffective = 0

        for rec in ordered:
            # Once quorum of valid observable responses is reached, request cancellation of remaining
            if enable_hedged_cancellation and len(valid_responses) >= required_quorum:
                cancel_requested += 1
                if callable(rec.get("cancel_confirm_fn")) and rec["cancel_confirm_fn"]():
                    cancel_confirmed += 1
                    continue
                elif bool(rec.get("cancellation_confirmed")):
                    cancel_confirmed += 1
                    continue
                else:
                    cancel_ineffective += 1

            lat = float(rec.get("latency_ms", 0.0))
            if lat > timeout_ms:
                timed_out_count += 1
                continue

            st = str(rec.get("status") or "completed")
            code = str(rec.get("candidate_code") or "").strip()
            if st == "missing" or not code:
                missing_count += 1
                continue
            if st == "failed":
                failed_count += 1
                continue

            fp = cls._compute_ast_fingerprint(code)
            if fp.startswith("invalid_syntax_"):
                malformed_count += 1
                continue

            if observable_check_fn is not None:
                obs_check = observable_check_fn(code)
                if not obs_check.get("passed", False):
                    failed_count += 1
                    continue

            valid_responses.append(
                {
                    "worker_id": rec.get("worker_id"),
                    "candidate_code": code,
                    "ast_fingerprint": fp,
                    "latency_ms": lat,
                    "observable_finding": rec.get("observable_finding"),
                }
            )

        if len(valid_responses) == 0 and (failed_count + malformed_count + missing_count) == actual_size:
            return {
                "group_id": group_id,
                "actual_size": actual_size,
                "required_quorum": required_quorum,
                "valid_response_count": 0,
                "status": "all_failed",
                "selected_candidate": None,
                "selected_ast_fingerprint": None,
                "disagreements": [],
                "minority_findings": [],
                "failed_count": failed_count,
                "malformed_count": malformed_count,
                "missing_count": missing_count,
                "timed_out_count": timed_out_count,
                "cancellation_requested_count": cancel_requested,
                "cancellation_confirmed_count": cancel_confirmed,
                "cancellation_ineffective_count": cancel_ineffective,
            }

        if len(valid_responses) < required_quorum:
            return {
                "group_id": group_id,
                "actual_size": actual_size,
                "required_quorum": required_quorum,
                "valid_response_count": len(valid_responses),
                "status": "insufficient_evidence",
                "selected_candidate": None,
                "selected_ast_fingerprint": None,
                "disagreements": [],
                "minority_findings": [r["ast_fingerprint"] for r in valid_responses],
                "failed_count": failed_count,
                "malformed_count": malformed_count,
                "missing_count": missing_count,
                "timed_out_count": timed_out_count,
                "cancellation_requested_count": cancel_requested,
                "cancellation_confirmed_count": cancel_confirmed,
                "cancellation_ineffective_count": cancel_ineffective,
            }

        # Group by AST fingerprint (as deduplication aid only)
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in valid_responses:
            buckets.setdefault(item["ast_fingerprint"], []).append(item)

        ranked = sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        top_fp, top_items = ranked[0]

        # Check for tie
        if len(ranked) > 1 and len(ranked[1][1]) == len(top_items):
            tied_fps = [fp for fp, items in ranked if len(items) == len(top_items)]
            return {
                "group_id": group_id,
                "actual_size": actual_size,
                "required_quorum": required_quorum,
                "valid_response_count": len(valid_responses),
                "status": "tie_unresolved",
                "selected_candidate": None,
                "selected_ast_fingerprint": None,
                "tied_fingerprints": tied_fps,
                "tied_candidates": [buckets[fp][0]["candidate_code"] for fp in tied_fps],
                "disagreements": [
                    {"ast_fingerprint": fp, "support_count": len(items), "sample_code": items[0]["candidate_code"]}
                    for fp, items in ranked
                ],
                "minority_findings": [
                    {
                        "ast_fingerprint": fp,
                        "support_count": len(items),
                        "worker_ids": [x["worker_id"] for x in items],
                        "candidate_code": items[0]["candidate_code"],
                    }
                    for fp, items in ranked
                ],
                "failed_count": failed_count,
                "malformed_count": malformed_count,
                "missing_count": missing_count,
                "timed_out_count": timed_out_count,
                "cancellation_requested_count": cancel_requested,
                "cancellation_confirmed_count": cancel_confirmed,
                "cancellation_ineffective_count": cancel_ineffective,
            }

        minority_findings = [
            {
                "ast_fingerprint": fp,
                "support_count": len(items),
                "worker_ids": [x["worker_id"] for x in items],
                "candidate_code": items[0]["candidate_code"],
            }
            for fp, items in ranked[1:]
        ]

        return {
            "group_id": group_id,
            "actual_size": actual_size,
            "required_quorum": required_quorum,
            "valid_response_count": len(valid_responses),
            "status": "quorum_selected",
            "selected_candidate": top_items[0]["candidate_code"],
            "selected_ast_fingerprint": top_fp,
            "selected_support_count": len(top_items),
            "disagreements": minority_findings,
            "minority_findings": minority_findings,
            "failed_count": failed_count,
            "malformed_count": malformed_count,
            "missing_count": missing_count,
            "timed_out_count": timed_out_count,
            "cancellation_requested_count": cancel_requested,
            "cancellation_confirmed_count": cancel_confirmed,
            "cancellation_ineffective_count": cancel_ineffective,
        }

    def run_hierarchical_thousand_mind_collective(
        self,
        challenge_prompt: str = "Implement clamp_ratio(val, lo, hi) with NaN and lo>hi ValueError checks.",
        total_minds: int = 1000,
        squad_size: int = 10,
        platoon_squads: int = 10,
        *,
        execution_mode: str = "simulation",
        quorum_ratio: float = 0.67,
        timeout_ms: float = 1500.0,
        enable_hedged_cancellation: bool = True,
        population_records: list[dict[str, Any]] | None = None,
        private_evaluator_ledger: dict[str, dict[str, Any]] | None = None,
        observable_check_fn: Callable[[str], dict[str, Any]] | None = None,
        authoritative_verify_fn: Callable[[str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run the hierarchical collective simulation with strict evaluator privacy and authoritative verification.

        - Requires ``execution_mode="simulation"`` (raises RuntimeError if called as ``"live"``).
        - Keeps injected fault labels in ``private_evaluator_ledger`` completely hidden from
          Squad Leaders, Platoon Commanders, and the Root Reducer.
        - Enforces quorum thresholds, timeouts, cancellation_requested vs cancellation_confirmed,
          remainder groups, ties, and all-failing populations.
        - Runs authoritative independent verification on the root candidate.
        - Measures information retained vs lost, including correct minority findings discarded by voting.
        """
        mode = _validate_execution_mode(execution_mode)
        if mode == "live":
            raise RuntimeError(
                "1,000-mind hierarchical tree is simulation scaffolding only; "
                "live execution is bounded to the 4-worker pool ceiling."
            )
        if total_minds <= 0 or squad_size <= 0 or platoon_squads <= 0:
            raise ValueError(
                f"total_minds ({total_minds}), squad_size ({squad_size}), and platoon_squads ({platoon_squads}) must be positive integers"
            )
        if not (0.0 < quorum_ratio <= 1.0):
            raise ValueError(f"quorum_ratio must be in (0.0, 1.0]; got {quorum_ratio}")

        t0 = time.perf_counter()
        verify_fn = authoritative_verify_fn or _authoritative_clamp_ratio_check

        # Build population if not explicitly supplied by a test harness
        private_ledger: dict[str, dict[str, Any]] = (
            dict(private_evaluator_ledger) if private_evaluator_ledger is not None else {}
        )
        observable_population: list[dict[str, Any]] = []

        if population_records is not None:
            for rec in population_records:
                self._verify_no_fault_label_leakage(rec)
                observable_population.append(dict(rec))
            total_minds = len(observable_population)
            if total_minds == 0:
                raise ValueError("population_records cannot be empty")
        else:
            # Generate 3 code variants WITHOUT exposing variant labels to the observable records
            code_correct = (
                "import math\n"
                "def clamp_ratio(val: float, lo: float = 0.0, hi: float = 1.0) -> float:\n"
                "    if math.isnan(val) or math.isnan(lo) or math.isnan(hi) or lo > hi:\n"
                "        raise ValueError('Invalid bounds or NaN')\n"
                "    return max(lo, min(hi, float(val)))\n"
            )
            code_missing_nan = (
                "def clamp_ratio(val: float, lo: float = 0.0, hi: float = 1.0) -> float:\n"
                "    if lo > hi:\n"
                "        raise ValueError('lo > hi')\n"
                "    return max(lo, min(hi, float(val)))\n"
            )
            code_syntax_err = "def clamp_ratio(val, lo=0.0, hi=1.0):\n    return max(lo, min(hi, val)\n"

            for idx in range(total_minds):
                wid = f"sim_worker_{idx:04d}"
                if idx % 17 == 0:
                    code = code_syntax_err
                    private_ledger[wid] = {"is_truly_correct": False, "fault": "syntax_error"}
                elif idx % 5 == 0:
                    code = code_missing_nan
                    private_ledger[wid] = {"is_truly_correct": False, "fault": "missing_nan_guard"}
                else:
                    code = code_correct
                    private_ledger[wid] = {
                        "is_truly_correct": True,
                        "fault": None,
                        "is_minority_finding": (idx % 97 == 0),
                    }
                latency = 120.0 + ((idx * 37) % 900)
                observable_population.append(
                    {
                        "worker_id": wid,
                        "status": "completed",
                        "latency_ms": latency,
                        "candidate_code": code,
                    }
                )

        # Partition into squads (handling remainder squads cleanly)
        squad_outcomes: list[dict[str, Any]] = []
        for s_idx, start in enumerate(range(0, len(observable_population), squad_size)):
            squad_slice = observable_population[start : start + squad_size]
            s_out = self._reduce_group_observable(
                f"squad_{s_idx:03d}",
                squad_slice,
                quorum_ratio=quorum_ratio,
                timeout_ms=timeout_ms,
                enable_hedged_cancellation=enable_hedged_cancellation,
                observable_check_fn=observable_check_fn,
            )
            squad_outcomes.append(s_out)

        # Tier 2: Partition squad outcomes into platoons (handling remainder platoons cleanly)
        platoon_outcomes: list[dict[str, Any]] = []
        for p_idx, start in enumerate(range(0, len(squad_outcomes), platoon_squads)):
            p_slice = squad_outcomes[start : start + platoon_squads]
            platoon_inputs: list[dict[str, Any]] = []
            for sq in p_slice:
                if sq["status"] == "quorum_selected" and sq["selected_candidate"]:
                    platoon_inputs.append(
                        {
                            "worker_id": sq["group_id"],
                            "status": "completed",
                            "latency_ms": 50.0,
                            "candidate_code": sq["selected_candidate"],
                        }
                    )
                else:
                    platoon_inputs.append(
                        {
                            "worker_id": sq["group_id"],
                            "status": "failed" if sq["status"] == "all_failed" else "missing",
                            "latency_ms": 50.0,
                            "candidate_code": "",
                        }
                    )
            p_out = self._reduce_group_observable(
                f"platoon_{p_idx:02d}",
                platoon_inputs,
                quorum_ratio=quorum_ratio,
                timeout_ms=timeout_ms,
                enable_hedged_cancellation=False,
                observable_check_fn=observable_check_fn,
            )
            platoon_outcomes.append(p_out)

        # Tier 3: Root reduction across platoons
        root_inputs: list[dict[str, Any]] = []
        for pl in platoon_outcomes:
            if pl["status"] == "quorum_selected" and pl["selected_candidate"]:
                root_inputs.append(
                    {
                        "worker_id": pl["group_id"],
                        "status": "completed",
                        "latency_ms": 25.0,
                        "candidate_code": pl["selected_candidate"],
                    }
                )
            else:
                root_inputs.append(
                    {
                        "worker_id": pl["group_id"],
                        "status": "failed" if pl["status"] == "all_failed" else "missing",
                        "latency_ms": 25.0,
                        "candidate_code": "",
                    }
                )

        root_outcome = self._reduce_group_observable(
            "root_council",
            root_inputs,
            quorum_ratio=quorum_ratio,
            timeout_ms=timeout_ms,
            enable_hedged_cancellation=False,
            observable_check_fn=observable_check_fn,
        )

        # Authoritative independent verification (the verifier, NOT the vote, determines pass/fail)
        final_candidate = root_outcome.get("selected_candidate")
        if final_candidate:
            verification = verify_fn(final_candidate)
        else:
            verification = {
                "passed": False,
                "status": root_outcome["status"],
                "failed_criteria": [f"no_candidate:{root_outcome['status']}"],
                "criteria_results": {},
            }

        # Post-hoc evaluator grading using private ledger (never used during selection!)
        initial_fps = {
            self._compute_ast_fingerprint(r.get("candidate_code", ""))
            for r in observable_population
            if r.get("candidate_code")
        }
        tier1_fps = {
            sq["selected_ast_fingerprint"]
            for sq in squad_outcomes
            if sq.get("selected_ast_fingerprint")
        }
        root_fps = (
            {root_outcome["selected_ast_fingerprint"]}
            if root_outcome.get("selected_ast_fingerprint")
            else set()
        )

        # Track whether any correct minority findings were discarded by majority voting
        correct_worker_fps: set[str] = set()
        for r in observable_population:
            wid = str(r.get("worker_id") or "")
            meta = private_ledger.get(wid, {})
            if meta.get("is_truly_correct") and r.get("candidate_code"):
                correct_worker_fps.add(self._compute_ast_fingerprint(r["candidate_code"]))

        correct_minority_discarded = 0
        for sq in squad_outcomes:
            for min_item in sq.get("minority_findings", []):
                if isinstance(min_item, dict) and min_item.get("ast_fingerprint") in correct_worker_fps:
                    if sq.get("selected_ast_fingerprint") not in correct_worker_fps:
                        correct_minority_discarded += 1

        vote_selected_incorrect = bool(
            final_candidate and not verification.get("passed", False)
        )

        total_cancel_req = sum(sq["cancellation_requested_count"] for sq in squad_outcomes)
        total_cancel_conf = sum(sq["cancellation_confirmed_count"] for sq in squad_outcomes)
        total_cancel_ineff = sum(sq["cancellation_ineffective_count"] for sq in squad_outcomes)

        return {
            "ok": True,
            "execution_mode": "simulation",
            "is_simulation": True,
            "qualifies_capabilities": False,
            "challenge_prompt": challenge_prompt,
            "population_size": len(observable_population),
            "squad_count": len(squad_outcomes),
            "platoon_count": len(platoon_outcomes),
            "quorum_ratio": quorum_ratio,
            "timeout_ms": timeout_ms,
            "root_reduction_status": root_outcome["status"],
            "selected_candidate": final_candidate,
            "selected_ast_fingerprint": root_outcome.get("selected_ast_fingerprint"),
            "disagreements_preserved": root_outcome.get("disagreements", []),
            "cancellation_accounting": {
                "cancellation_requested": total_cancel_req,
                "cancellation_confirmed": total_cancel_conf,
                "cancellation_ineffective": total_cancel_ineff,
            },
            "information_retention_metrics": {
                "initial_unique_ast_fingerprints": len(initial_fps),
                "retained_after_tier1": len(tier1_fps),
                "retained_at_root": len(root_fps),
                "lost_ast_fingerprints_count": max(0, len(initial_fps) - len(root_fps)),
                "correct_minority_findings_discarded": correct_minority_discarded,
                "escaped_defects_from_vote": 1 if vote_selected_incorrect else 0,
            },
            "authoritative_verification": {
                "verified": bool(verification.get("passed", False)),
                "verifier_status": verification.get("status"),
                "failed_criteria": verification.get("failed_criteria", []),
                "criteria_results": verification.get("criteria_results", {}),
                "vote_overruled_by_verifier": vote_selected_incorrect,
            },
            "wall_clock_simulation_ms": round((time.perf_counter() - t0) * 1000.0, 2),
        }
