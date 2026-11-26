"""Frontier Research Guild, Thinking Task Queue, and Weekly Token Budget Ledger.

Isolates the 29+ Cloud/API-Key Frontier Models (OpenAI, Google Gemini/Gemma, Anthropic Claude,
SpaceX/xAI Grok, Hugging Face Router, NVIDIA NIM, Groq LPU, GitHub Models) into a dedicated,
calendar-scheduled Research & Thinking Guild while keeping routine background loops on the
zero-cost Local `ego` Fleet + Open Zero-Auth Web Portals.

Provides:
1. Per-Provider Weekly Token Budget & Pacing Ledger (`frontier_weekly_budgets`, `frontier_token_ledger`):
   - Tracks weekly token limits, used tokens, calendar-reserved tokens, and day-of-week pacing
     so every week's token budget is properly budgeted, protected from overspending, and
     actively utilized on high-value thinking research.
2. Frontier Research Queue (`frontier_research_queue`):
   - Queues, schedules (via `sabrina_console_calendar.py`), and executes deep-thinking tasks:
     * `frontier.research.rsi_augment`: RSI Skill & Architecture Augmentation
     * `frontier.research.wsp_thinking`: Weight-Space Programming (WSP) & Counterfactual Neural Research
     * `frontier.research.hard_challenge`: Frontier Preparedness & SWE-Bench Hard Evaluations
     * `frontier.research.synthesis`: Multi-Provider Frontier Council & Deep Reasoning Synthesis
3. Scoped Frontier Execution (`scoped_frontier_research_grant`):
   - Temporarily unlocks only the targeted provider/model for a budgeted Frontier Research job
     without turning on global `api_tiers_enabled` for background social/chat loops.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get("SABRINA_ASSISTANT_DB", ROOT / "data" / "sabrina_assistant_fresh.sqlite3"))
ZONE = ZoneInfo("America/Chicago")
_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# Catalog of the 8 Frontier Providers & 32 Frontier Models
# ---------------------------------------------------------------------------
FRONTIER_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "provider_id": "openai",
        "label": "OpenAI Frontier (o3 / GPT-4o)",
        "secret_key": "OPENAI_API_KEY",
        "default_weekly_budget": 250_000,
        "default_model": "o3-mini",
        "models": [
            {"id": "o3-mini", "label": "o3-mini (Frontier Reasoning / Thinking)", "tier": "thinking"},
            {"id": "gpt-4o", "label": "gpt-4o (Omni Architecture & Synthesis)", "tier": "frontier"},
            {"id": "gpt-4o-mini", "label": "gpt-4o-mini (Fast Structured Verification)", "tier": "fast_frontier"},
        ],
    },
    "google": {
        "provider_id": "google",
        "label": "Google Gemini & Gemma Frontier",
        "secret_key": "GEMINI_API_KEY",
        "default_weekly_budget": 1_500_000,
        "default_model": "gemini-3.1-pro-preview",
        "models": [
            {"id": "gemini-3.1-pro-preview", "label": "gemini-3.1-pro-preview (1M Context Deep Thinking)", "tier": "thinking"},
            {"id": "gemini-3.6-flash", "label": "gemini-3.6-flash (High-Speed Agentic Synthesis)", "tier": "frontier"},
            {"id": "gemini-3-flash-preview", "label": "gemini-3-flash-preview (Portal Profile)", "tier": "frontier"},
            {"id": "gemini-3.5-flash-lite", "label": "gemini-3.5-flash-lite (Portal Profile)", "tier": "fast_frontier"},
            {"id": "gemini-3.1-flash-lite", "label": "gemini-3.1-flash-lite (Portal Profile)", "tier": "fast_frontier"},
            {"id": "gemini-3.1-flash-lite-preview", "label": "gemini-3.1-flash-lite-preview (Portal Profile)", "tier": "fast_frontier"},
            {"id": "gemini-2.5-flash", "label": "gemini-2.5-flash (Portal Profile)", "tier": "fast_frontier"},
            {"id": "gemini-flash-lite-latest", "label": "gemini-flash-lite-latest (Portal Profile)", "tier": "fast_frontier"},
            {"id": "gemma-4-31b-it", "label": "gemma-4-31b-it (Open-Weights 31B Cloud)", "tier": "frontier"},
            {"id": "gemma-4-26b-a4b-it", "label": "gemma-4-26b-a4b-it (MoE 26B Cloud)", "tier": "frontier"},
        ],
    },
    "anthropic": {
        "provider_id": "anthropic",
        "label": "Anthropic Claude Frontier",
        "secret_key": "ANTHROPIC_API_KEY",
        "default_weekly_budget": 200_000,
        "default_model": "claude-sonnet-5",
        "models": [
            {"id": "claude-sonnet-5", "label": "claude-sonnet-5 (Frontier Code & RSI Architect)", "tier": "thinking"},
            {"id": "claude-3-5-sonnet", "label": "claude-3-5-sonnet ( Formal Verification & Patching)", "tier": "frontier"},
            {"id": "claude-haiku-4-5-20251001", "label": "claude-haiku-4-5-20251001 (Fast Critique)", "tier": "fast_frontier"},
        ],
    },
    "spacex_xai": {
        "provider_id": "spacex_xai",
        "label": "SpaceX / xAI Grok Frontier",
        "secret_key": "XAI_API_KEY",
        "default_weekly_budget": 300_000,
        "default_model": "grok-3-mini-beta",
        "models": [
            {"id": "grok-3-mini-beta", "label": "grok-3-mini-beta (xAI Reasoning & First-Principles Thinking)", "tier": "thinking"},
            {"id": "grok-3", "label": "grok-3 (xAI Frontier Flagship)", "tier": "frontier"},
            {"id": "grok-2-1212", "label": "grok-2-1212 (xAI Systems Synthesis)", "tier": "frontier"},
        ],
    },
    "hf_router": {
        "provider_id": "hf_router",
        "label": "Hugging Face Router Frontier",
        "secret_key": "HF_TOKEN",
        "default_weekly_budget": 500_000,
        "default_model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "models": [
            {"id": "deepseek-ai/DeepSeek-V4.1-Flash-1017", "label": "DeepSeek-V4.1-Flash (HF Router Portal)", "tier": "thinking"},
            {"id": "Qwen/Qwen3.8-27B", "label": "Qwen3.8-27B (HF Router Portal)", "tier": "thinking"},
            {"id": "moonshotai/Kimi-K3.1-Pro", "label": "Kimi-K3.1-Pro (Moonshot Long-Horizon Thinking)", "tier": "thinking"},
            {"id": "zai-org/GLM-5.3-Flash", "label": "GLM-5.3-Flash (HF Router Portal)", "tier": "frontier"},
            {"id": "meta-llama/Llama-3.1-8B-Instruct", "label": "Llama-3.1-8B-Instruct (HF Router Portal)", "tier": "fast_frontier"},
        ],
    },
    "nvidia_nim": {
        "provider_id": "nvidia_nim",
        "label": "NVIDIA NIM Frontier Catalog",
        "secret_key": "NVIDIA_API_KEY",
        "default_weekly_budget": 400_000,
        "default_model": "deepseek-ai/deepseek-r1",
        "models": [
            {"id": "deepseek-ai/deepseek-r1", "label": "deepseek-r1 (671B Reasoning via NVIDIA NIM)", "tier": "thinking"},
            {"id": "qwen/qwen2.5-coder-32b-instruct", "label": "qwen2.5-coder-32b-instruct (NIM Code)", "tier": "frontier"},
            {"id": "meta/llama-3.3-70b-instruct", "label": "llama-3.3-70b-instruct (NIM 70B)", "tier": "frontier"},
        ],
    },
    "groq": {
        "provider_id": "groq",
        "label": "Groq LPU Ultra-Fast Frontier",
        "secret_key": "GROQ_API_KEY",
        "default_weekly_budget": 500_000,
        "default_model": "deepseek-r1-distill-llama-70b",
        "models": [
            {"id": "deepseek-r1-distill-llama-70b", "label": "deepseek-r1-distill-llama-70b (LPU Thinking)", "tier": "thinking"},
            {"id": "llama-3.3-70b-versatile", "label": "llama-3.3-70b-versatile (LPU 70B)", "tier": "frontier"},
            {"id": "gemma2-9b-it", "label": "gemma2-9b-it (LPU Fast Verifier)", "tier": "fast_frontier"},
        ],
    },
    "github_models": {
        "provider_id": "github_models",
        "label": "GitHub Models / Azure AI Frontier",
        "secret_key": "GITHUB_TOKEN",
        "default_weekly_budget": 300_000,
        "default_model": "DeepSeek-R1",
        "models": [
            {"id": "DeepSeek-R1", "label": "DeepSeek-R1 (Azure AI Reasoning)", "tier": "thinking"},
            {"id": "gpt-4o", "label": "gpt-4o (GitHub Models Frontier)", "tier": "frontier"},
            {"id": "Llama-3.3-70B-Instruct", "label": "Llama-3.3-70B-Instruct (GitHub Models)", "tier": "frontier"},
        ],
    },
}

RESEARCH_TASK_TYPES: Dict[str, Dict[str, Any]] = {
    "frontier.research.rsi_augment": {
        "capability": "frontier.research.rsi_augment",
        "label": "RSI Skill & Architecture Augmentation (Thinking)",
        "default_tokens": 12_000,
        "default_duration_min": 45,
        "description": "Deep-thinking evaluation, code synthesis, and cryptographic promotion of Sabrina RSI skills & agent architecture.",
    },
    "frontier.research.wsp_thinking": {
        "capability": "frontier.research.wsp_thinking",
        "label": "WSP Weight-Space & Counterfactual Neural Research",
        "default_tokens": 15_000,
        "default_duration_min": 60,
        "description": "Task-vector arithmetic (DARE-TIES / SLERP), SVD spectrum diagnostics, and counterfactual head isolation with Frontier reasoning.",
    },
    "frontier.research.hard_challenge": {
        "capability": "frontier.research.hard_challenge",
        "label": "Frontier Preparedness & SWE-Bench Hard Evaluation",
        "default_tokens": 14_000,
        "default_duration_min": 45,
        "description": "Multi-step adversarial security, async TOCTOU race, SSTI taint, and cryptographic timing side-channel verification.",
    },
    "frontier.research.synthesis": {
        "capability": "frontier.research.synthesis",
        "label": "Cross-Frontier Council Deep Research Synthesis",
        "default_tokens": 18_000,
        "default_duration_min": 60,
        "description": "Convenes budgeted Frontier thinking models alongside the Local ego Chair to author a provenance-signed research paper.",
    },
    "frontier.research.blt_augmentation": {
        "capability": "frontier.research.blt_augmentation",
        "label": "BLT Byte-Latent Transformer Model Research & Benchmark Augmentation",
        "default_tokens": 16_000,
        "default_duration_min": 50,
        "description": "Dynamic Shannon-entropy byte patching, rolling n-gram hash embeddings, PyTorch BLT cross-attention autoencoding, and local model benchmark augmentation.",
    },
}


def current_week_window(now_ts: Optional[float] = None) -> Dict[str, Any]:
    """Return current ISO week key (`YYYY-Www`), start/end timestamps, and ideal weekly pacing percentage."""
    ts = now_ts if now_ts is not None else time.time()
    dt_now = dt.datetime.fromtimestamp(ts, ZONE)
    iso_year, iso_week, iso_weekday = dt_now.isocalendar()  # weekday: 1=Mon .. 7=Sun
    week_key = f"{iso_year}-W{iso_week:02d}"
    monday = (dt_now - dt.timedelta(days=iso_weekday - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    sunday_end = (monday + dt.timedelta(days=6)).replace(hour=23, minute=59, second=59, microsecond=0)
    elapsed_fraction = min(1.0, max(0.05, (ts - monday.timestamp()) / max(1.0, sunday_end.timestamp() - monday.timestamp())))
    return {
        "week_key": week_key,
        "iso_year": iso_year,
        "iso_week": iso_week,
        "iso_weekday": iso_weekday,
        "week_start_iso": monday.strftime("%Y-%m-%d"),
        "week_end_iso": sunday_end.strftime("%Y-%m-%d"),
        "week_start_ts": monday.timestamp(),
        "week_end_ts": sunday_end.timestamp(),
        "ideal_pacing_pct": round(elapsed_fraction * 100.0, 1),
    }


@contextlib.contextmanager
def _db_conn(db_path: Path = DEFAULT_DB):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS frontier_weekly_budgets (
            provider_id TEXT NOT NULL,
            week_key TEXT NOT NULL,
            weekly_budget_tokens INTEGER NOT NULL,
            used_tokens INTEGER NOT NULL DEFAULT 0,
            reserved_tokens INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at REAL NOT NULL,
            PRIMARY KEY (provider_id, week_key)
        );

        CREATE TABLE IF NOT EXISTS frontier_token_ledger (
            entry_id TEXT PRIMARY KEY,
            week_key TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            task_id TEXT,
            capability TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            thinking_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            latency_sec REAL NOT NULL DEFAULT 0.0,
            outcome TEXT NOT NULL,
            created_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS frontier_research_queue (
            task_id TEXT PRIMARY KEY,
            week_key TEXT NOT NULL,
            capability TEXT NOT NULL,
            title TEXT NOT NULL,
            prompt TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            thinking_budget_tokens INTEGER NOT NULL DEFAULT 12000,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            scheduled_job_id TEXT,
            scheduled_for_iso TEXT,
            duration_minutes INTEGER NOT NULL DEFAULT 45,
            status TEXT NOT NULL DEFAULT 'queued',
            result_markdown TEXT DEFAULT '',
            thinking_trace TEXT DEFAULT '',
            metrics_json TEXT DEFAULT '{}',
            error_message TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );
        """
    )


def _has_provider_secret(provider_id: str) -> bool:
    meta = FRONTIER_PROVIDERS.get(provider_id, {})
    secret_name = meta.get("secret_key", "")
    if not secret_name:
        return False
    try:
        from tools.secret_store import has_secret
        if has_secret(secret_name):
            return True
    except Exception:
        pass
    return bool(os.environ.get(secret_name, "").strip())


def _get_provider_secret(provider_id: str) -> str:
    meta = FRONTIER_PROVIDERS.get(provider_id, {})
    secret_name = meta.get("secret_key", "")
    if not secret_name:
        return ""
    try:
        from tools.secret_store import get_secret
        val = get_secret(secret_name, env_fallback_paths=[".env", str(ROOT / ".env")])
        if val:
            return val.strip()
    except Exception:
        pass
    return os.environ.get(secret_name, "").strip()


def get_weekly_budget_status(db_path: Path = DEFAULT_DB) -> Dict[str, Any]:
    """Return the current ISO week's token budget, usage, reservation, and pacing for all 8 Frontier providers."""
    win = current_week_window()
    wk = win["week_key"]
    ideal_pct = win["ideal_pacing_pct"]
    now = time.time()

    providers_out: List[Dict[str, Any]] = []
    total_budget = 0
    total_used = 0
    total_reserved = 0

    with _db_conn(db_path) as conn:
        for pid, meta in FRONTIER_PROVIDERS.items():
            row = conn.execute(
                "SELECT * FROM frontier_weekly_budgets WHERE provider_id=? AND week_key=?",
                (pid, wk),
            ).fetchone()
            if not row:
                conn.execute(
                    """INSERT INTO frontier_weekly_budgets
                       (provider_id, week_key, weekly_budget_tokens, used_tokens, reserved_tokens, enabled, updated_at)
                       VALUES (?, ?, ?, 0, 0, 1, ?)""",
                    (pid, wk, int(meta["default_weekly_budget"]), now),
                )
                row = conn.execute(
                    "SELECT * FROM frontier_weekly_budgets WHERE provider_id=? AND week_key=?",
                    (pid, wk),
                ).fetchone()

            # Compute live reserved tokens from queued/scheduled tasks in this week
            res_row = conn.execute(
                """SELECT COALESCE(SUM(thinking_budget_tokens), 0)
                   FROM frontier_research_queue
                   WHERE week_key=? AND provider_id=? AND status IN ('queued', 'scheduled', 'running')""",
                (wk, pid),
            ).fetchone()
            reserved = int(res_row[0] or 0)
            if reserved != int(row["reserved_tokens"]):
                conn.execute(
                    "UPDATE frontier_weekly_budgets SET reserved_tokens=?, updated_at=? WHERE provider_id=? AND week_key=?",
                    (reserved, now, pid, wk),
                )

            budget = int(row["weekly_budget_tokens"])
            used = int(row["used_tokens"])
            enabled = bool(row["enabled"])
            remaining = max(0, budget - used)
            committed_pct = round(((used + reserved) / budget) * 100.0, 1) if budget > 0 else 0.0
            used_pct = round((used / budget) * 100.0, 1) if budget > 0 else 0.0

            if not enabled:
                pacing_state = "PAUSED"
            elif used_pct >= 95.0:
                pacing_state = "BUDGET_CAPPED"
            elif committed_pct < max(10.0, ideal_pct - 20.0):
                pacing_state = "UNDER_UTILIZED"
            elif used_pct > ideal_pct + 25.0:
                pacing_state = "AHEAD_OF_PACE"
            else:
                pacing_state = "ON_TRACK"

            key_configured = _has_provider_secret(pid)
            total_budget += budget
            total_used += used
            total_reserved += reserved

            providers_out.append(
                {
                    "provider_id": pid,
                    "label": meta["label"],
                    "secret_key": meta["secret_key"],
                    "key_configured": key_configured,
                    "enabled": enabled,
                    "week_key": wk,
                    "weekly_budget_tokens": budget,
                    "used_tokens": used,
                    "reserved_tokens": reserved,
                    "remaining_tokens": remaining,
                    "used_pct": used_pct,
                    "committed_pct": committed_pct,
                    "ideal_pacing_pct": ideal_pct,
                    "pacing_state": pacing_state,
                    "default_model": meta["default_model"],
                    "models": meta["models"],
                }
            )

        ledger_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM frontier_token_ledger WHERE week_key=? ORDER BY created_at DESC LIMIT 40",
                (wk,),
            ).fetchall()
        ]

    total_Frontier_models = sum(len(m["models"]) for m in FRONTIER_PROVIDERS.values())
    return {
        "week": win,
        "total_frontier_models": total_Frontier_models,
        "summary": {
            "total_weekly_budget_tokens": total_budget,
            "total_used_tokens": total_used,
            "total_reserved_tokens": total_reserved,
            "total_remaining_tokens": max(0, total_budget - total_used),
            "overall_used_pct": round((total_used / total_budget) * 100.0, 1) if total_budget > 0 else 0.0,
            "overall_committed_pct": round(((total_used + total_reserved) / total_budget) * 100.0, 1) if total_budget > 0 else 0.0,
            "ideal_pacing_pct": ideal_pct,
        },
        "providers": providers_out,
        "recent_ledger": ledger_rows,
    }


def update_weekly_budget(
    provider_id: str,
    weekly_budget_tokens: Optional[int] = None,
    enabled: Optional[bool] = None,
    db_path: Path = DEFAULT_DB,
) -> Dict[str, Any]:
    """Update a Frontier provider's weekly token budget or enabled state for the current ISO week."""
    pid = (provider_id or "").strip().lower()
    if pid not in FRONTIER_PROVIDERS:
        raise ValueError(f"Unknown frontier provider '{pid}'. Valid: {sorted(FRONTIER_PROVIDERS)}")
    # Ensure current week row exists first
    get_weekly_budget_status(db_path=db_path)
    wk = current_week_window()["week_key"]
    now = time.time()
    with _db_conn(db_path) as conn:
        if weekly_budget_tokens is not None:
            conn.execute(
                "UPDATE frontier_weekly_budgets SET weekly_budget_tokens=?, updated_at=? WHERE provider_id=? AND week_key=?",
                (max(0, int(weekly_budget_tokens)), now, pid, wk),
            )
        if enabled is not None:
            conn.execute(
                "UPDATE frontier_weekly_budgets SET enabled=?, updated_at=? WHERE provider_id=? AND week_key=?",
                (int(bool(enabled)), now, pid, wk),
            )
    return get_weekly_budget_status(db_path=db_path)


def select_budget_paced_provider(
    preferred_provider: str = "auto",
    preferred_model: str = "auto",
    required_tokens: int = 12_000,
    db_path: Path = DEFAULT_DB,
) -> Dict[str, str]:
    """Pick the best Frontier provider & thinking model that has weekly token budget available.

    When `preferred_provider == 'auto'`, prioritizes enabled providers with configured keys whose
    weekly utilization (`used_pct`) is furthest below `ideal_pacing_pct`, ensuring every provider's
    weekly token allocation is evenly and actively used.
    """
    status = get_weekly_budget_status(db_path=db_path)
    by_id = {p["provider_id"]: p for p in status["providers"]}

    if preferred_provider and preferred_provider != "auto" and preferred_provider in by_id:
        p = by_id[preferred_provider]
        model = (
            preferred_model
            if preferred_model and preferred_model != "auto"
            else p["default_model"]
        )
        return {"provider_id": p["provider_id"], "model_id": model}

    # Filter to enabled providers with remaining weekly budget >= required_tokens
    candidates = [
        p
        for p in status["providers"]
        if p["enabled"] and p["remaining_tokens"] >= required_tokens
    ]
    with_keys = [p for p in candidates if p["key_configured"]]
    pool = with_keys if with_keys else candidates
    if not pool:
        return {"provider_id": "google", "model_id": "gemini-3.1-pro-preview"}

    # Sort by lowest used_pct relative to budget so under-utilized weekly budgets get used first
    pool.sort(key=lambda p: (p["used_pct"], -p["remaining_tokens"]))
    chosen = pool[0]
    model = (
        preferred_model
        if preferred_model and preferred_model != "auto"
        else chosen["default_model"]
    )
    return {"provider_id": chosen["provider_id"], "model_id": model}


def record_frontier_token_usage(
    provider_id: str,
    model_id: str,
    capability: str,
    prompt_tokens: int,
    completion_tokens: int,
    thinking_tokens: int = 0,
    task_id: Optional[str] = None,
    latency_sec: float = 0.0,
    outcome: str = "success",
    db_path: Path = DEFAULT_DB,
) -> Dict[str, Any]:
    """Record token expenditure against the current ISO week's provider budget."""
    get_weekly_budget_status(db_path=db_path)
    wk = current_week_window()["week_key"]
    total = max(0, int(prompt_tokens) + int(completion_tokens) + int(thinking_tokens))
    now = time.time()
    entry_id = f"tok_{uuid.uuid4().hex[:12]}"
    with _db_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO frontier_token_ledger
               (entry_id, week_key, provider_id, model_id, task_id, capability,
                prompt_tokens, completion_tokens, thinking_tokens, total_tokens,
                latency_sec, outcome, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry_id,
                wk,
                provider_id,
                model_id,
                task_id,
                capability,
                int(prompt_tokens),
                int(completion_tokens),
                int(thinking_tokens),
                total,
                round(float(latency_sec), 3),
                outcome,
                now,
            ),
        )
        if outcome == "success" and total > 0 and provider_id in FRONTIER_PROVIDERS:
            conn.execute(
                """UPDATE frontier_weekly_budgets
                   SET used_tokens = used_tokens + ?, updated_at = ?
                   WHERE provider_id = ? AND week_key = ?""",
                (total, now, provider_id, wk),
            )
    return {"entry_id": entry_id, "week_key": wk, "provider_id": provider_id, "total_tokens": total}


@contextlib.contextmanager
def scoped_frontier_research_grant():
    """Temporarily allow API-key Frontier calls inside a budgeted Frontier Research task without
    leaving global `api_tiers_enabled` turned on for routine background loops."""
    with _LOCK:
        prev = os.environ.get("SABRINA_API_TIERS_ENABLED")
        os.environ["SABRINA_API_TIERS_ENABLED"] = "1"
        try:
            yield
        finally:
            if prev is None:
                os.environ["SABRINA_API_TIERS_ENABLED"] = "0"
            else:
                os.environ["SABRINA_API_TIERS_ENABLED"] = prev


def _call_frontier_provider_with_budget(
    provider_id: str,
    model_id: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
    db_path: Path = DEFAULT_DB,
    allow_local_fallback: bool = False,
) -> Dict[str, Any]:
    """Execute a budgeted call to the selected Frontier provider, falling back cleanly to the
    Local `ego` `<think>` Chair (`qwen3:14b`) + Open Web Guilds if the provider's weekly budget
    is exhausted or its API key is unconfigured/quota-limited (and allow_local_fallback is True)."""
    status = get_weekly_budget_status(db_path=db_path)
    prov_info = next((p for p in status["providers"] if p["provider_id"] == provider_id), None)
    t0 = time.monotonic()

    if prov_info and not prov_info["enabled"]:
        return {
            "ok": False,
            "provider_id": provider_id,
            "model_id": model_id,
            "reply": "",
            "thinking": "",
            "error": f"Provider '{provider_id}' is paused in Weekly Token Budget settings.",
        }

    if prov_info and prov_info["remaining_tokens"] <= 0:
        return {
            "ok": False,
            "provider_id": provider_id,
            "model_id": model_id,
            "reply": "",
            "thinking": "",
            "error": f"Weekly token budget exhausted for '{provider_id}' ({prov_info['used_tokens']}/{prov_info['weekly_budget_tokens']} tokens used in {status['week']['week_key']}).",
        }

    reply_text: Optional[str] = None
    thinking_text: str = ""
    actual_provider = provider_id
    actual_model = model_id

    with scoped_frontier_research_grant():
        try:
            if provider_id == "openai":
                from tools.oracle_providers import OpenAIOracle
                oracle = OpenAIOracle()
                reply_text = oracle.generate(system=system_prompt, prompt=user_prompt, model=model_id)
                actual_model = oracle.last_model_used or model_id
            elif provider_id == "google":
                from tools.oracle_providers import GoogleGeminiOracle
                oracle = GoogleGeminiOracle()
                reply_text = oracle.generate(system=system_prompt, prompt=user_prompt, model=model_id)
                actual_model = oracle.last_model_used or model_id
            elif provider_id == "anthropic":
                from tools.oracle_providers import AnthropicClaudeOracle
                oracle = AnthropicClaudeOracle()
                reply_text = oracle.generate(system=system_prompt, prompt=user_prompt)
            elif provider_id == "spacex_xai":
                api_key = _get_provider_secret("spacex_xai")
                if api_key:
                    payload = json.dumps(
                        {
                            "model": model_id or "grok-3-mini-beta",
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "temperature": 0.2,
                            "max_tokens": min(4096, max_tokens),
                        }
                    ).encode("utf-8")
                    req = urllib.request.Request(
                        "https://api.x.ai/v1/chat/completions",
                        data=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {api_key}",
                            "User-Agent": "SabrinaFrontierGuild/1.0",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        res_data = json.loads(resp.read().decode("utf-8"))
                        msg_obj = res_data["choices"][0]["message"]
                        reply_text = (msg_obj.get("content") or "").strip()
                        thinking_text = (msg_obj.get("reasoning_content") or "").strip()
            elif provider_id == "hf_router":
                api_key = _get_provider_secret("hf_router")
                if api_key:
                    payload = json.dumps(
                        {
                            "model": model_id or "deepseek-ai/DeepSeek-V4.1-Flash-1017",
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                            "temperature": 0.2,
                            "max_tokens": min(4096, max_tokens),
                        }
                    ).encode("utf-8")
                    req = urllib.request.Request(
                        "https://router.huggingface.co/v1/chat/completions",
                        data=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {api_key}",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        res_data = json.loads(resp.read().decode("utf-8"))
                        msg_obj = res_data["choices"][0]["message"]
                        reply_text = (msg_obj.get("content") or "").strip()
                        thinking_text = (msg_obj.get("reasoning_content") or "").strip()
            elif provider_id in ("nvidia_nim", "groq", "github_models"):
                from tools.free_compute_providers import FREE_COMPUTE_ROUTER
                gw = getattr(FREE_COMPUTE_ROUTER, provider_id, None)
                if gw and gw.is_available():
                    reply_text = gw.generate(system=system_prompt, prompt=user_prompt, model=model_id)
                    actual_model = gw.last_model_used or model_id
        except Exception:
            reply_text = None

    # Fallback to Local `ego` Deep-Thinking Chair (`qwen3:14b`) so scheduled research tasks always deliver
    used_local_fallback = False
    if not reply_text and allow_local_fallback:
        from tools.swarm_chat_portal import call_local_ollama_node
        local_res = call_local_ollama_node(
            "qwen3:14b",
            "Alibaba Qwen3-14B (Local Frontier Thinking Anchor)",
            system_prompt,
            user_prompt,
            num_predict=1800,
            temperature=0.3,
        )
        if local_res.get("ok"):
            reply_text = local_res.get("reply", "")
            thinking_text = local_res.get("thinking", "")
            actual_provider = f"{provider_id}->local_ego_fallback"
            actual_model = "qwen3:14b"
            used_local_fallback = True

    elapsed = round(time.monotonic() - t0, 2)
    prompt_toks = max(1, (len(system_prompt) + len(user_prompt)) // 4)
    comp_toks = max(1, len(reply_text or "") // 4) if reply_text else 0
    think_toks = max(0, len(thinking_text or "") // 4)

    return {
        "ok": bool(reply_text),
        "provider_id": actual_provider,
        "requested_provider_id": provider_id,
        "model_id": actual_model,
        "reply": reply_text or "",
        "thinking": thinking_text,
        "prompt_tokens": prompt_toks,
        "completion_tokens": comp_toks,
        "thinking_tokens": think_toks,
        "total_tokens": prompt_toks + comp_toks + think_toks,
        "latency_sec": elapsed,
        "used_local_fallback": used_local_fallback,
        "error": "" if reply_text else "Provider and local thinking fallback both failed",
    }


def enqueue_frontier_research_task(
    capability: str = "frontier.research.rsi_augment",
    title: str = "",
    prompt: str = "",
    provider_id: str = "auto",
    model_id: str = "auto",
    thinking_budget_tokens: int = 12_000,
    schedule_iso: Optional[str] = None,
    duration_minutes: int = 45,
    repeat_every_minutes: Optional[int] = None,
    run_immediately: bool = False,
    db_path: Path = DEFAULT_DB,
) -> Dict[str, Any]:
    """Add a thinking-intensive research task to the Frontier Research Queue, optionally scheduling
    it onto the Sabrina Time Granularizer Calendar or executing it immediately."""
    cap = (capability or "frontier.research.rsi_augment").strip()
    if cap not in RESEARCH_TASK_TYPES:
        cap = "frontier.research.rsi_augment"
    spec = RESEARCH_TASK_TYPES[cap]
    budget_toks = max(1_000, int(thinking_budget_tokens or spec["default_tokens"]))
    dur_min = max(10, int(duration_minutes or spec["default_duration_min"]))

    picked = select_budget_paced_provider(
        preferred_provider=provider_id,
        preferred_model=model_id,
        required_tokens=budget_toks,
        db_path=db_path,
    )
    resolved_provider = picked["provider_id"]
    resolved_model = picked["model_id"]

    clean_title = (title or "").strip() or f"{spec['label']} [{resolved_provider}:{resolved_model}]"
    clean_prompt = (prompt or "").strip() or (
        f"Execute {spec['label']} using deep step-by-step reasoning. "
        f"Produce actionable architectural findings, mathematical/code verification, and concrete next steps in Markdown."
    )

    task_id = f"frq_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    wk = current_week_window()["week_key"]
    now = time.time()
    scheduled_job_id: Optional[str] = None
    status = "queued"

    if schedule_iso:
        from tools.sabrina_console_calendar import schedule_calendar_block
        cal_res = schedule_calendar_block(
            capability=cap,
            start_iso=schedule_iso,
            duration_minutes=dur_min,
            repeat_every_minutes=repeat_every_minutes,
            objective=f"[{resolved_provider}:{resolved_model}] {clean_title}",
            payload={
                "frontier_task_id": task_id,
                "provider_id": resolved_provider,
                "model_id": resolved_model,
                "thinking_budget_tokens": budget_toks,
                "prompt": clean_prompt,
                "guild": "frontier-research",
            },
        )
        scheduled_job_id = cal_res["job"]["id"]
        status = "scheduled"

    with _db_conn(db_path) as conn:
        conn.execute(
            """INSERT INTO frontier_research_queue
               (task_id, week_key, capability, title, prompt, provider_id, model_id,
                thinking_budget_tokens, tokens_used, scheduled_job_id, scheduled_for_iso,
                duration_minutes, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                wk,
                cap,
                clean_title,
                clean_prompt,
                resolved_provider,
                resolved_model,
                budget_toks,
                scheduled_job_id,
                schedule_iso or "",
                dur_min,
                status,
                now,
                now,
            ),
        )

    # Refresh reserved token count
    get_weekly_budget_status(db_path=db_path)

    if run_immediately:
        return execute_frontier_research_task(task_id, db_path=db_path)

    return get_frontier_research_task(task_id, db_path=db_path)


def get_frontier_research_task(task_id: str, db_path: Path = DEFAULT_DB) -> Dict[str, Any]:
    with _db_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM frontier_research_queue WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        raise KeyError(f"Frontier research task '{task_id}' not found")
    d = dict(row)
    try:
        d["metrics"] = json.loads(d.get("metrics_json") or "{}")
    except Exception:
        d["metrics"] = {}
    return d


def list_frontier_research_queue(limit: int = 50, db_path: Path = DEFAULT_DB) -> Dict[str, Any]:
    with _db_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM frontier_research_queue ORDER BY created_at DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        try:
            d["metrics"] = json.loads(d.get("metrics_json") or "{}")
        except Exception:
            d["metrics"] = {}
        items.append(d)
    budget_state = get_weekly_budget_status(db_path=db_path)
    return {
        "tasks": items,
        "task_types": list(RESEARCH_TASK_TYPES.values()),
        "budget": budget_state,
    }


def execute_frontier_research_task(task_id: str, db_path: Path = DEFAULT_DB) -> Dict[str, Any]:
    """Execute a queued/scheduled Frontier Research task (RSI Augmentation, WSP Thinking,
    Frontier Hard Challenge, or Cross-Frontier Synthesis) and log token usage to the weekly ledger."""
    task = get_frontier_research_task(task_id, db_path=db_path)
    cap = task["capability"]
    provider_id = task["provider_id"]
    model_id = task["model_id"]
    prompt = task["prompt"]
    budget_toks = int(task["thinking_budget_tokens"] or 12_000)

    with _db_conn(db_path) as conn:
        conn.execute(
            "UPDATE frontier_research_queue SET status='running', updated_at=? WHERE task_id=?",
            (time.time(), task_id),
        )

    metrics: Dict[str, Any] = {"capability": cap, "requested_provider": provider_id, "requested_model": model_id}
    context_preamble = ""

    # Augment prompt with domain-specific live evidence from Sabrina's RSI / WSP / Hard-Challenge subsystems
    try:
        if cap == "frontier.research.rsi_augment":
            from tools.frontier_rsi_overseer import FrontierRsiOverseer
            overseer = FrontierRsiOverseer()
            ledger = overseer.audit_ledger()
            metrics["rsi_skills_installed"] = ledger.get("skills_installed", 0)
            metrics["rsi_pending_proposals"] = ledger.get("pending", 0)
            context_preamble = (
                f"### Live RSI Skill & Competence Context\n"
                f"- **Installed Verified Skills**: `{ledger.get('skills_installed', 0)}`\n"
                f"- **Approved Proposals**: `{ledger.get('approved', 0)}`\n"
                f"- **Pending Proposals**: `{ledger.get('pending', 0)}`\n\n"
            )
        elif cap == "frontier.research.wsp_thinking":
            import torch
            from tools.compute_research_engine import WeightSpaceToolkit
            torch.manual_seed(42)
            base_w = {"attn.q_proj": torch.randn(64, 64), "mlp.down_proj": torch.randn(64, 64)}
            ft_a = {k: v + 0.12 * torch.randn_like(v) for k, v in base_w.items()}
            ft_b = {k: v + 0.15 * torch.randn_like(v) for k, v in base_w.items()}
            tv_a = WeightSpaceToolkit.compute_task_vector(base_w, ft_a)
            tv_b = WeightSpaceToolkit.compute_task_vector(base_w, ft_b)
            merged_ties = WeightSpaceToolkit.ties_merging([tv_a, tv_b], density=0.35)
            diag = WeightSpaceToolkit.representation_diagnostics(merged_ties["attn.q_proj"])
            metrics["wsp_diagnostics"] = diag
            context_preamble = (
                f"### Live Weight-Space Programming (WSP) Telemetry\n"
                f"- **Operator**: `DARE-TIES (density=0.35)` across 2 task vectors\n"
                f"- **Effective Rank**: `{diag.get('effective_rank', 0):.2f}`\n"
                f"- **Spectral Norm**: `{diag.get('spectral_norm', 0):.4f}`\n"
                f"- **Condition Number**: `{diag.get('condition_number', 0):.2f}`\n\n"
            )
        elif cap == "frontier.research.hard_challenge":
            from tools.frontier_hard_challenge_engine import CHALLENGES
            ch = CHALLENGES.get("hard_sec_async_toctou_race") or next(iter(CHALLENGES.values()))
            metrics["challenge_id"] = ch.challenge_id
            context_preamble = (
                f"### Frontier Hard Challenge Target: `{ch.title}` (`{ch.challenge_id}`)\n"
                f"- **Threat Model**: {ch.threat_model}\n"
                f"```python\n{ch.vulnerable_code}\n```\n\n"
            )
        elif cap == "frontier.research.blt_augmentation":
            from tools.compute_research_engine import WeightSpaceToolkit
            blt_diag = WeightSpaceToolkit.run_blt_wsp_experiment()
            metrics["blt_diagnostics"] = blt_diag
            spec_diag = blt_diag.get("latent_spectral_diagnostics", {})
            context_preamble = (
                f"### Live Byte Latent Transformer (BLT) & WSP Telemetry (`tools/compute_research_engine.py`)\n"
                f"- **Architecture**: `{blt_diag.get('architecture')}`\n"
                f"- **Mean Patch Compression Ratio**: `{blt_diag.get('mean_compression_ratio_bytes_per_patch')} bytes/patch`\n"
                f"- **Estimated Compute FLOP Savings vs. Byte Transformer**: `{blt_diag.get('mean_flop_savings_vs_byte_transformer_pct')}%`\n"
                f"- **Bits-Per-Byte (BPB) Convergence**: `{blt_diag.get('mean_bpb_pre_adaptation')} -> {blt_diag.get('mean_bpb_post_adaptation')} (-{blt_diag.get('bpb_reduction_pct')}%)`\n"
                f"- **Latent Effective Rank**: `{spec_diag.get('effective_rank')}` | **Spectral Norm**: `{spec_diag.get('spectral_norm')}`\n\n"
            )
    except Exception as exc:
        metrics["context_warning"] = str(exc)

    system_prompt = (
        f"You are `{model_id}` operating inside Sabrina's Frontier Research & Thinking Guild (`{cap}`). "
        "Perform rigorous, multi-step architectural and mathematical analysis. "
        "Format your report in clean Markdown with sections: "
        "1. Executive Hypothesis, 2. Deep Technical / Mathematical Analysis, "
        "3. Concrete Implementation or Weight-Surgery Specification, and 4. Verification Protocol."
    )
    full_user_prompt = f"{context_preamble}### Research Objective\n{prompt}"

    call_res = _call_frontier_provider_with_budget(
        provider_id=provider_id,
        model_id=model_id,
        system_prompt=system_prompt,
        user_prompt=full_user_prompt,
        max_tokens=min(4096, budget_toks),
        db_path=db_path,
        allow_local_fallback=True,
    )

    used_Provider_for_ledger = provider_id if not str(call_res["provider_id"]).endswith("fallback") else provider_id
    if call_res["ok"] and not str(call_res["provider_id"]).endswith("fallback"):
        record_frontier_token_usage(
            provider_id=used_Provider_for_ledger,
            model_id=call_res["model_id"],
            capability=cap,
            prompt_tokens=call_res["prompt_tokens"],
            completion_tokens=call_res["completion_tokens"],
            thinking_tokens=call_res["thinking_tokens"],
            task_id=task_id,
            latency_sec=call_res["latency_sec"],
            outcome="success",
            db_path=db_path,
        )

    metrics["execution_provider"] = call_res["provider_id"]
    metrics["execution_model"] = call_res["model_id"]
    metrics["latency_sec"] = call_res["latency_sec"]
    metrics["tokens"] = {
        "prompt": call_res["prompt_tokens"],
        "completion": call_res["completion_tokens"],
        "thinking": call_res["thinking_tokens"],
        "total": call_res["total_tokens"],
    }

    now = time.time()
    new_status = "completed" if call_res["ok"] else "failed"
    with _db_conn(db_path) as conn:
        conn.execute(
            """UPDATE frontier_research_queue
               SET status=?, tokens_used=?, result_markdown=?, thinking_trace=?,
                   metrics_json=?, error_message=?, updated_at=?
               WHERE task_id=?""",
            (
                new_status,
                int(call_res["total_tokens"]),
                call_res["reply"],
                call_res["thinking"],
                json.dumps(metrics),
                call_res["error"],
                now,
                task_id,
            ),
        )

    get_weekly_budget_status(db_path=db_path)
    return get_frontier_research_task(task_id, db_path=db_path)


def run_scheduled_frontier_capability(capability: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Adapter entry point invoked by `sabrina_social/scheduler.py` when a scheduled calendar block fires."""
    payload = payload or {}
    existing_task_id = payload.get("frontier_task_id")
    if existing_task_id:
        try:
            res = execute_frontier_research_task(str(existing_task_id))
            return {"status": "completed", "task": res}
        except KeyError:
            pass
    task = enqueue_frontier_research_task(
        capability=capability,
        title=str(payload.get("objective") or f"Scheduled {capability}"),
        prompt=str(payload.get("prompt") or payload.get("objective") or f"Execute scheduled {capability} block"),
        provider_id=str(payload.get("provider_id") or "auto"),
        model_id=str(payload.get("model_id") or "auto"),
        thinking_budget_tokens=int(payload.get("thinking_budget_tokens") or 12_000),
        run_immediately=True,
    )
    return {"status": "completed", "task": task}


def seed_weekly_frontier_research_schedule(
    anchor_date: Optional[str] = None,
    db_path: Path = DEFAULT_DB,
) -> Dict[str, Any]:
    """Schedule a full weekly cadence of Frontier Research, RSI Augmentation, and WSP Thinking blocks
    across the Time Granularizer Calendar so every provider's weekly token budget is systematically used."""
    base_day = dt.date.fromisoformat(anchor_date) if anchor_date else dt.datetime.now(ZONE).date()
    # Align to Monday of the target week (or start from today forward across the next 7 days)
    weekly_plan = [
        (0, 13, 0, 45, "frontier.research.rsi_augment", "openai", "o3-mini", 15_000,
         "Weekly OpenAI o3-mini RSI Skill & Architecture Augmentation Pass"),
        (1, 13, 0, 60, "frontier.research.wsp_thinking", "google", "gemini-3.1-pro-preview", 25_000,
         "Weekly Gemini 3.1 Pro 1M-Context WSP Task-Vector & Counterfactual Head Isolation"),
        (2, 13, 0, 45, "frontier.research.hard_challenge", "anthropic", "claude-sonnet-5", 16_000,
         "Weekly Claude Sonnet 5 Frontier Preparedness & Async TOCTOU / SSTI Audit"),
        (3, 13, 0, 45, "frontier.research.synthesis", "spacex_xai", "grok-3-mini-beta", 18_000,
         "Weekly SpaceX / xAI Grok-3 First-Principles Systems & Telemetry Synthesis"),
        (4, 13, 0, 60, "frontier.research.wsp_thinking", "hf_router", "deepseek-ai/DeepSeek-V4.1-Flash-1017", 20_000,
         "Weekly HF Router DeepSeek-V4.1 / Kimi-K3 Weight-Space Merging & DARE-TIES Sweep"),
        (5, 13, 0, 45, "frontier.research.rsi_augment", "nvidia_nim", "deepseek-ai/deepseek-r1", 18_000,
         "Weekly NVIDIA NIM DeepSeek-R1 Autonomous Skill Proposal Verification"),
        (6, 13, 0, 60, "frontier.research.synthesis", "groq", "deepseek-r1-distill-llama-70b", 20_000,
         "Weekly Cross-Frontier Council Synthesis & Weekly Token Budget Reconciliation"),
    ]
    created_tasks = []
    now_ts = time.time()
    for day_offset, hr, mn, dur, cap, prov, mod, tok_budget, title in weekly_plan:
        slot_date = base_day + dt.timedelta(days=day_offset)
        slot_dt = dt.datetime.combine(slot_date, dt.time(hr, mn))
        slot_ts = slot_dt.replace(tzinfo=ZONE).timestamp()
        if slot_ts < now_ts - 1800:
            slot_dt = slot_dt + dt.timedelta(days=7)
        task = enqueue_frontier_research_task(
            capability=cap,
            title=title,
            prompt=(
                f"{title}. Utilize budgeted thinking tokens ({tok_budget:,} tok allocation) "
                f"on `{prov}:{mod}` to produce a verifiable research artifact."
            ),
            provider_id=prov,
            model_id=mod,
            thinking_budget_tokens=tok_budget,
            schedule_iso=slot_dt.isoformat(),
            duration_minutes=dur,
            repeat_every_minutes=10080,  # Weekly (7 * 1440m)
            run_immediately=False,
            db_path=db_path,
        )
        created_tasks.append(task)
    return {
        "ok": True,
        "scheduled_count": len(created_tasks),
        "tasks": created_tasks,
        "budget": get_weekly_budget_status(db_path=db_path),
    }
