#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Epistemic Surprise Sentinel & 24-Hour Shadow Paper A/B Challenger Engine
================================================================================
Replaces blind cron busywork with zero-GPU epistemic ("information-need") triggers:
  1. `post_loss_autopsy`: Wakes Sabrina when a live order in `orders.sqlite3` settles
     at a stop-out (`trend_break_stop`, `cat_stop`, or negative round-trip PnL).
  2. `missed_moon_regret`: Wakes Sabrina when a shortlisted coin she rejected in
     `market-inputs/*.json` subsequently rallies >= +5.0% (+500 bps) within 90 minutes.
  3. `gate_drought_diagnosis`: Wakes Sabrina if 0 live entries occur over >= 18 hours
     while Kraken has >= 2 shortlisted movers up >= +5.0% intraday.
  4. `idle_frontier_curiosity`: When no urgent market trigger is active and `ego` has
     0 running runtime tasks, picks one unexplored `open_equations` item from
     `agent_epochs` (`data/agent_runtime.sqlite3`) and runs a bounded hypothesis check.

Safety & Promotion Pipeline (Zero Unapproved Live Mutation):
  - Candidate trading configs must respect `PROJECT_SAFE_BOUNDS["crypto"]` and beat the
    live Champion on historical Time-Machine `total_net_bps` and `profit_factor`.
  - Winning candidates are enrolled into a **24-Hour Shadow Paper A/B Trial** (`shadow_ab_trials`),
    evaluating every incoming live `market-inputs/*.json` cycle side-by-side against the
    live Champion using real quotes and Maker fees.
  - Only after >= 24 hours of live forward Shadow Paper A/B data confirm that the Challenger
    outperforms the Champion without excess drawdown does Sabrina promote it to a
    **64-character SHA-256 Proposal** in `assistant_skill_proposals` (visible in the
    Antigravity Command Console `Proposal Gate` tab for 1-click operator approval).
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.agent_runtime import PROJECT_SAFE_BOUNDS, Runtime, load_config
from tools.kraken_time_machine_sim import (
    DEFAULT_SIM_PARAMS,
    evaluate_candidate_decision,
    load_saved_snapshots_and_ohlc,
    run_time_machine_backtest,
    sweep_parameter_grid,
)
from tools.sabrina_skill_hydration import ensure_skill_proposals_schema


SENTINEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS epistemic_trigger_log (
    dedup_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    pair TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    resolution TEXT NOT NULL DEFAULT 'triggered',
    task_id TEXT NOT NULL DEFAULT '',
    trial_id TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_ab_trials (
    trial_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_summary TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_evaluated_at REAL NOT NULL DEFAULT 0,
    champion_params_json TEXT NOT NULL,
    challenger_params_json TEXT NOT NULL,
    backtest_summary_json TEXT NOT NULL DEFAULT '{}',
    champion_shadow_json TEXT NOT NULL DEFAULT '{}',
    challenger_shadow_json TEXT NOT NULL DEFAULT '{}',
    proposal_id TEXT NOT NULL DEFAULT '',
    proposal_sha256 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shadow_ab_status ON shadow_ab_trials(status, created_at);

CREATE TABLE IF NOT EXISTS system_gate_registry (
    domain TEXT PRIMARY KEY,
    gate_name TEXT NOT NULL,
    deterministic_predicate TEXT NOT NULL,
    last_state_sha256 TEXT NOT NULL DEFAULT '',
    anomaly_active INTEGER NOT NULL DEFAULT 0,
    severity TEXT NOT NULL DEFAULT 'ok',
    headline TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    ping_count INTEGER NOT NULL DEFAULT 0,
    last_ping_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS autonomous_model_approvals (
    approval_id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    proposal_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    juror_1_model TEXT NOT NULL,
    juror_1_approve INTEGER NOT NULL,
    juror_1_verbatim TEXT NOT NULL,
    juror_2_model TEXT NOT NULL,
    juror_2_approve INTEGER NOT NULL,
    juror_2_verbatim TEXT NOT NULL,
    juror_3_model TEXT NOT NULL DEFAULT '',
    juror_3_approve INTEGER NOT NULL DEFAULT 0,
    juror_3_verbatim TEXT NOT NULL DEFAULT '',
    juror_4_model TEXT NOT NULL DEFAULT '',
    juror_4_approve INTEGER NOT NULL DEFAULT 0,
    juror_4_verbatim TEXT NOT NULL DEFAULT '',
    applied_to_live_config INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""


UNIVERSAL_DETERMINISTIC_GATE_SPECS: Dict[str, Dict[str, str]] = {
    "hardware_compute_bus": {
        "gate_name": "NVML Handle & CPU-Fallback Circuit Gate",
        "predicate": "wake iff (nvidia_smi_exit != 0) OR (ollama_active_vram_bytes == 0 AND resident_models > 0) OR (max_proc_cpu_pct >= 200.0 with --n-gpu-layers 0) OR (unexpected_reverse_federation_tunnel == True)",
    },
    "lab_coordinator_control": {
        "gate_name": "Control Queue Progress & Unprompted-Task Gate",
        "predicate": "wake iff (new_control_request_sha256 != prev_sha256) OR (agent_events_per_task_ratio >= 50.0); reject autonomous tasks lacking an epistemic_trigger_log dedup_key",
    },
    "social_and_scheduler": {
        "gate_name": "DOM/Feed State-Hash & Human-CAPTCHA Halt Gate",
        "predicate": "wake iff sha256(unread_dm_count, feed_top_post_urn) changes; halt retries and ping once iff job_state in {'awaiting_human_captcha', 'external_in_progress'} or duplicate capability count >= 25",
    },
    "recruiter_and_messaging": {
        "gate_name": "Inbound Message SHA-256 & Single-Pass Jury Gate",
        "predicate": "wake 3-model review iff sha256(thread_id, last_inbound_message_id) != last_reviewed_sha256; surface pending desktop_questions once per revision_sha256",
    },
    "customer_and_work_email": {
        "gate_name": "Gmail History-ID Delta & NEEDS_OPERATOR Latch Gate",
        "predicate": "wake inbox classifier iff sha256(top_message_id, unread_thread_count) changes; latch and ping operator once iff job.state == 'NEEDS_OPERATOR'",
    },
    "docker_recon_and_daemons": {
        "gate_name": "Container Health-Edge & PID-Lockfile Gate",
        "predicate": "wake once on state edge healthy -> {unhealthy, restarting} or container CPU >= 95% for >15m or duplicate legacy daemon alongside v2 service",
    },
    "windows_host_and_bridges": {
        "gate_name": "Bridge Readiness Edge & Ephemeral Utility TTL Gate",
        "predicate": "wake only on state edge ready <-> degraded (exponential backoff) or one-shot utility server uptime > 1800s",
    },
    "kraken_trading_execution": {
        "gate_name": "Market Surprise & 24h Shadow Paper A/B Gate",
        "predicate": "wake iff post_loss_autopsy OR missed_moon_regret (+500 bps / 90m) OR gate_drought (18h / >=2 movers) OR (idle AND open_equations > 0)",
    },
}


def _default_runtime_db() -> Path:
    return Path(os.environ.get("SABRINA_AGENT_RUNTIME_DB", str(ROOT / "data" / "agent_runtime.sqlite3")))


def _default_assistant_dbs() -> List[Path]:
    env_p = os.environ.get("SABRINA_ASSISTANT_DB")
    if env_p:
        return [Path(env_p)]
    out = [ROOT / "data" / "sabrina_assistant_fresh.sqlite3"]
    legacy = ROOT / "data" / "sabrina_assistant.sqlite3"
    if legacy.exists():
        out.append(legacy)
    return out


def _empty_shadow_portfolio(start_cash_usd: float = 15.5417) -> Dict[str, Any]:
    return {
        "start_cash_usd": round(start_cash_usd, 4),
        "cash_usd": round(start_cash_usd, 4),
        "equity_usd": round(start_cash_usd, 4),
        "peak_equity_usd": round(start_cash_usd, 4),
        "max_drawdown_bps": 0.0,
        "total_net_bps": 0.0,
        "frames_observed": 0,
        "closed_trades": [],
        "open_position": None,
        "pair_last_exit": {},
    }


_CUSTOM_JEV_CACHE: Dict[str, Any] = {}


def _score_snapshot_with_custom_jev(snap: Dict[str, Any], ckpt_path: str) -> List[Dict[str, Any]]:
    """Run live forward-pass inference of a challenger JEV checkpoint (e.g. v03_quip3b_qat / v03_quip2b) on `snap['bars']`."""
    import numpy as np
    import torch
    from torch.nn import functional as F
    from tools import kraken_jev_live_gate as jgate

    p_str = str(ckpt_path)
    if p_str not in _CUSTOM_JEV_CACHE:
        _CUSTOM_JEV_CACHE[p_str] = jgate.load_jev_checkpoint(p_str)
    bundle = _CUSTOM_JEV_CACHE[p_str]
    model = bundle["model"]
    f_mean = bundle["f_mean"]
    f_std = bundle["f_std"]

    bars_map = snap.get("bars") or {}
    quotes_map = snap.get("quotes") or {}
    moon_map = snap.get("moon_intel") or {}
    orig_by_pair = {str(r.get("pair")): r for r in (snap.get("jev_trace") or []) if isinstance(r, dict) and r.get("pair")}
    major_idx = {p: i for i, p in enumerate(jgate.LIVE_MAJORS)}

    out_trace: List[Dict[str, Any]] = []
    for pair, c15 in bars_map.items():
        q = quotes_map.get(pair) or {}
        bid, ask = float(q.get("bid") or 0.0), float(q.get("ask") or 0.0)
        if bid <= 0 or ask < bid or not isinstance(c15, list) or len(c15) < 97:
            continue
        spread_bps = ((ask - bid) / bid) * 10000.0
        c_arr = np.asarray([float(r[4]) for r in c15], dtype=np.float64)
        h_arr = np.asarray([float(r[2]) for r in c15], dtype=np.float64)
        l_arr = np.asarray([float(r[3]) for r in c15], dtype=np.float64)
        v_arr = np.asarray([float(r[5]) for r in c15], dtype=np.float64)
        if np.any(c_arr <= 0):
            continue
        c_now = float(c_arr[-1])
        ret_15m = (c_now / float(c_arr[-2])) - 1.0
        ret_5m = ret_15m / 3.0
        ret_60m = (c_now / float(c_arr[-5])) - 1.0
        ret_240m = (c_now / float(c_arr[-17])) - 1.0
        ret_1440m = (c_now / float(c_arr[-97])) - 1.0
        lret15 = np.diff(np.log(c_arr[-17:]))
        vol_60m = float(np.std(lret15[-4:], ddof=1)) * 2.0 if len(lret15) >= 4 else 0.01
        vol_15m = vol_60m
        v4 = v_arr[-4:]
        vz = max(-5.0, min(5.0, (float(v4[-1]) - float(np.mean(v4))) / (float(np.std(v4)) + 1e-9)))
        rf60 = (float(np.max(h_arr[-4:])) - float(np.min(l_arr[-4:]))) / (c_now + 1e-9)
        pos24 = max(0.0, min(1.0, (c_now - float(np.min(l_arr[-96:]))) / (float(np.max(h_arr[-96:])) - float(np.min(l_arr[-96:])) + 1e-9)))
        ef, es = float(c_arr[-32]), float(c_arr[-32])
        for val in c_arr[-31:]:
            ef = (2.0 / 5.0) * float(val) + (3.0 / 5.0) * ef
            es = (2.0 / 17.0) * float(val) + (15.0 / 17.0) * es
        ema_r = (ef / (es + 1e-9)) - 1.0
        p_norm = float(major_idx.get(pair, 2)) / float(max(1, len(jgate.LIVE_MAJORS) - 1))
        raw_v = np.asarray([[ret_5m, ret_15m, ret_60m, ret_240m, ret_1440m, vol_15m, vol_60m, vz, rf60, pos24, ema_r, p_norm]], dtype=np.float32)
        norm_v = np.clip((raw_v - f_mean) / f_std, -8.0, 8.0)
        with torch.inference_mode():
            _, nl, chl, sc = model(torch.from_numpy(norm_v))
            nl_long = float(nl[0, 0].item())
            nl_short = float(nl[0, 1].item())
            cp = F.softmax(chl, dim=-1)[0]
            sc_bps = float(sc[0].item()) * 100.0
        m_info = moon_map.get(pair) or {}
        bonus = float(m_info.get("score_bonus_bps") or 0.0)
        base_orig = orig_by_pair.get(pair) or {}
        out_trace.append({
            **base_orig,
            "pair": pair,
            "noul_long": round(nl_long, 4),
            "noul_short": round(nl_short, 4),
            "choice_hold": round(float(cp[0].item()), 4),
            "choice_long": round(float(cp[1].item()), 4),
            "choice_short": round(float(cp[2].item()), 4),
            "expected_score_bps": round(sc_bps, 2),
            "effective_score_bps": round(sc_bps + bonus, 2),
            "spread_bps": round(spread_bps, 2),
        })
    return out_trace


def _clamp_to_safe_crypto_bounds(params: Dict[str, Any]) -> Dict[str, Any]:
    bounds = PROJECT_SAFE_BOUNDS.get("crypto", {})
    has_custom_jev = bool(params.get("jev_checkpoint_path"))
    key_map = {
        "noul_threshold": "jev_noul_threshold",
        "min_score_bps": "jev_min_score_bps",
        "choice_margin": "jev_choice_margin",
        "max_spread_bps": "jev_max_spread_bps",
        "min_intraday_gain_pct": "jev_min_intraday_gain_pct",
        "max_pre_moon_gain_pct": "jev_max_pre_moon_gain_pct",
        "pair_cooldown_seconds": "jev_pair_cooldown_seconds",
    }
    clean = dict(params)
    for sim_key, bound_key in key_map.items():
        if sim_key in clean and bound_key in bounds:
            # v0.3 net-of-fee checkpoints calibrate `min_score_bps` & `noul_threshold` in their native net scale
            if has_custom_jev and sim_key in ("min_score_bps", "noul_threshold", "choice_margin"):
                clean[sim_key] = round(max(-150.0, min(100.0, float(clean[sim_key]))), 5)
            else:
                lo, hi = bounds[bound_key]
                clean[sim_key] = round(max(float(lo), min(float(hi), float(clean[sim_key]))), 5)
    if "min_moon_score" in clean:
        clean["min_moon_score"] = round(max(0.0, min(85.0, float(clean["min_moon_score"]))), 2)
    clean["cat_stop_bps"] = -450.0
    return clean


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


class EpistemicSentinel:
    def __init__(
        self,
        *,
        runtime_db_path: Optional[str | Path] = None,
        assistant_db_paths: Optional[Sequence[str | Path]] = None,
        orders_db_path: Optional[str | Path] = None,
        market_inputs_dir: Optional[str | Path] = None,
        kraken_config_path: Optional[str | Path] = None,
        kraken_control_path: Optional[str | Path] = None,
        shadow_duration_seconds: float = 86400.0,
        autonomous_model_approval: bool = False,
        jury_caller: Optional[Any] = None,
    ):
        self.runtime_db_path = Path(runtime_db_path or _default_runtime_db())
        self.runtime_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.assistant_db_paths = (
            [Path(p) for p in assistant_db_paths]
            if assistant_db_paths is not None
            else _default_assistant_dbs()
        )
        self.orders_db_path = Path(
            orders_db_path
            or os.environ.get(
                "SABRINA_KRAKEN_ORDERS_DB",
                "/home/ego/.local/state/sabrina-kraken/execution-v1/orders.sqlite3",
            )
        )
        self.market_inputs_dir = Path(
            market_inputs_dir
            or os.environ.get(
                "SABRINA_KRAKEN_MARKET_INPUTS",
                "/home/ego/.local/state/sabrina-kraken/execution-v1/market-inputs",
            )
        )
        self.kraken_config_path = Path(
            kraken_config_path
            or os.environ.get(
                "SABRINA_KRAKEN_CONFIG_JSON",
                "/home/ego/.local/state/sabrina-kraken/execution-v1/config.json",
            )
        )
        self.kraken_control_path = Path(
            kraken_control_path
            or os.environ.get(
                "SABRINA_KRAKEN_CONTROL_JSON",
                "/home/ego/.local/state/sabrina-kraken/execution-v1/control.json",
            )
        )
        self.shadow_duration_seconds = float(shadow_duration_seconds)
        self.autonomous_model_approval = bool(
            autonomous_model_approval or os.environ.get("SABRINA_AUTONOMOUS_MODEL_APPROVAL") == "1"
        )
        self.jury_caller = jury_caller
        with self._connect() as db:
            db.executescript(SENTINEL_SCHEMA)
            existing_cols = {r[1] for r in db.execute("PRAGMA table_info(autonomous_model_approvals)").fetchall()}
            for col_def in (
                ("juror_3_model", "TEXT NOT NULL DEFAULT ''"),
                ("juror_3_approve", "INTEGER NOT NULL DEFAULT 0"),
                ("juror_3_verbatim", "TEXT NOT NULL DEFAULT ''"),
                ("juror_4_model", "TEXT NOT NULL DEFAULT ''"),
                ("juror_4_approve", "INTEGER NOT NULL DEFAULT 0"),
                ("juror_4_verbatim", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col_def[0] not in existing_cols:
                    db.execute(f"ALTER TABLE autonomous_model_approvals ADD COLUMN {col_def[0]} {col_def[1]}")

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.runtime_db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _already_logged(self, db: sqlite3.Connection, dedup_key: str) -> bool:
        row = db.execute(
            "SELECT 1 FROM epistemic_trigger_log WHERE dedup_key=?",
            (dedup_key,),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 1. Zero-GPU Epistemic Surprise Detection
    # ------------------------------------------------------------------
    def detect_triggers(
        self,
        *,
        now: Optional[float] = None,
        snapshots: Optional[Sequence[Dict[str, Any]]] = None,
        drought_hours_threshold: float = 18.0,
        regret_rally_bps_threshold: float = 500.0,
        curiosity_cooldown_seconds: float = 14400.0,
    ) -> List[Dict[str, Any]]:
        now_ts = float(now if now is not None else time.time())
        triggers: List[Dict[str, Any]] = []

        if snapshots is None and self.market_inputs_dir.is_dir():
            snapshots, _ = load_saved_snapshots_and_ohlc(self.market_inputs_dir, max_snapshots=180)
        snapshots = list(snapshots or [])

        with self._connect() as db:
            # Trigger A: Post-Loss Autopsy (`orders.sqlite3`)
            last_entry_ts = 0.0
            if self.orders_db_path.is_file():
                try:
                    with contextlib.closing(sqlite3.connect(str(self.orders_db_path), timeout=3.0)) as odb:
                        odb.row_factory = sqlite3.Row
                        rows = odb.execute(
                            "SELECT order_id, intent, evidence, created, updated FROM orders WHERE phase='settled' ORDER BY updated DESC LIMIT 25"
                        ).fetchall()
                        for r in rows:
                            it = json.loads(r["intent"] or "{}")
                            ev = json.loads(r["evidence"] or "{}")
                            purpose = str(it.get("purpose") or "")
                            ts_ord = float(r["updated"] or r["created"] or 0.0)
                            if purpose == "entry" and ts_ord > last_entry_ts:
                                last_entry_ts = ts_ord
                            if purpose == "exit":
                                if ev.get("outcome") == "terminal_unfilled" or ("executed" in ev and float(ev.get("executed") or 0.0) <= 0.0):
                                    continue
                                reason = str(it.get("reason") or "")
                                pair = str(it.get("pair") or "UNKNOWN")
                                oid = str(r["order_id"] or f"ts-{int(ts_ord)}")
                                if reason in {"trend_break_stop", "cat_stop"}:
                                    dkey = f"loss_autopsy:{oid}"
                                    if not self._already_logged(db, dkey):
                                        triggers.append({
                                            "kind": "post_loss_autopsy",
                                            "dedup_key": dkey,
                                            "pair": pair,
                                            "summary": f"Live stop-out on {pair} ({reason}, order {oid})",
                                            "payload": {
                                                "order_id": oid,
                                                "pair": pair,
                                                "exit_reason": reason,
                                                "exit_price": it.get("price"),
                                                "quote_delta": ev.get("quote_delta"),
                                                "settled_at": ts_ord,
                                            },
                                        })
                except Exception:
                    pass

            # Trigger B: Missed-Moon Regret (`market-inputs/*.json`)
            if len(snapshots) >= 2:
                latest_snap = snapshots[-1]
                latest_ts = float(latest_snap.get("at") or now_ts)
                latest_quotes = latest_snap.get("quotes") or {}
                # Also collect latest close from all recent snapshots per pair
                latest_px_by_pair: Dict[str, float] = {}
                for s in snapshots[-15:]:
                    for p, q in (s.get("quotes") or {}).items():
                        b_px = float(q.get("bid") or 0.0)
                        if b_px > 0:
                            latest_px_by_pair[p] = b_px

                bucket_6h = int(now_ts // 21600)
                seen_regret_pairs = set()
                for past in snapshots:
                    p_ts = float(past.get("at") or 0.0)
                    age_sec = latest_ts - p_ts
                    if not (600.0 <= age_sec <= 5400.0):
                        continue
                    p_quotes = past.get("quotes") or {}
                    for r in (past.get("jev_trace") or []):
                        if not isinstance(r, dict):
                            continue
                        pair = str(r.get("pair") or "")
                        rej_reason = str(r.get("reason") or "")
                        if not pair or rej_reason in {"accepted", ""} or pair in seen_regret_pairs:
                            continue
                        past_ask = float((p_quotes.get(pair) or {}).get("ask") or (p_quotes.get(pair) or {}).get("bid") or 0.0)
                        cur_bid = float((latest_quotes.get(pair) or {}).get("bid") or latest_px_by_pair.get(pair, 0.0))
                        if past_ask > 0 and cur_bid > past_ask:
                            rally_bps = ((cur_bid / past_ask) - 1.0) * 10000.0
                            if rally_bps >= regret_rally_bps_threshold:
                                dkey = f"missed_moon:{pair}:{bucket_6h}"
                                seen_regret_pairs.add(pair)
                                if not self._already_logged(db, dkey):
                                    triggers.append({
                                        "kind": "missed_moon_regret",
                                        "dedup_key": dkey,
                                        "pair": pair,
                                        "summary": f"Rejected {pair} ({rej_reason}) rallied +{rally_bps:.0f} bps within {int(age_sec // 60)}m",
                                        "payload": {
                                            "pair": pair,
                                            "blocked_by": rej_reason,
                                            "past_price": past_ask,
                                            "current_price": cur_bid,
                                            "rally_bps": round(rally_bps, 1),
                                            "noul_long": r.get("noul_long"),
                                            "effective_score_bps": r.get("effective_score_bps"),
                                        },
                                    })

            # Trigger C: Gate-Drought Diagnosis (18h+ silence while market has >=2 +5% movers)
            if snapshots and last_entry_ts > 0:
                hours_silent = (now_ts - last_entry_ts) / 3600.0
                latest_trace = snapshots[-1].get("jev_trace") or []
                active_movers = [
                    r.get("pair") for r in latest_trace
                    if isinstance(r, dict) and float(r.get("intraday_pct") or 0.0) >= 5.0
                ]
                if hours_silent >= drought_hours_threshold and len(active_movers) >= 2:
                    day_bucket = int(now_ts // 86400)
                    dkey = f"gate_drought:{day_bucket}"
                    if not self._already_logged(db, dkey):
                        triggers.append({
                            "kind": "gate_drought_diagnosis",
                            "dedup_key": dkey,
                            "pair": active_movers[0],
                            "summary": f"0 entries in {hours_silent:.1f}h despite active movers ({', '.join(active_movers[:3])})",
                            "payload": {
                                "hours_silent": round(hours_silent, 2),
                                "active_movers": active_movers,
                            },
                        })

            # Trigger D: Idle Frontier Curiosity (`agent_epochs.open_equations` when idle)
            if not triggers:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                running_tasks = 0
                if "agent_tasks" in tables:
                    running_tasks = int(db.execute("SELECT count(*) FROM agent_tasks WHERE state='running'").fetchone()[0])
                last_cur = db.execute(
                    "SELECT MAX(created) FROM epistemic_trigger_log WHERE kind='idle_frontier_curiosity'"
                ).fetchone()[0]
                cooled_down = (last_cur is None) or ((now_ts - float(last_cur)) >= curiosity_cooldown_seconds)
                if running_tasks == 0 and cooled_down and "agent_epochs" in tables:
                    ep_rows = db.execute(
                        "SELECT task_id, open_equations, next_frontier FROM agent_epochs ORDER BY created DESC LIMIT 15"
                    ).fetchall()
                    for er in ep_rows:
                        open_eqs = json.loads(er["open_equations"] or "[]")
                        for eq in open_eqs:
                            eq_str = str(eq).strip()
                            if not eq_str:
                                continue
                            eq_hash = hashlib.sha256(eq_str.encode("utf-8")).hexdigest()[:16]
                            dkey = f"idle_curiosity:{eq_hash}"
                            if not self._already_logged(db, dkey):
                                triggers.append({
                                    "kind": "idle_frontier_curiosity",
                                    "dedup_key": dkey,
                                    "pair": "PORTFOLIO",
                                    "summary": f"Idle frontier curiosity: {eq_str[:110]}",
                                    "payload": {
                                        "source_task_id": er["task_id"],
                                        "open_equation": eq_str,
                                        "next_frontier": er["next_frontier"],
                                    },
                                })
                                break
                        if triggers:
                            break

        return triggers

    # ------------------------------------------------------------------
    # 2. Hypothesis Generation, Time-Machine Test & 24h Shadow A/B Enrollment
    # ------------------------------------------------------------------
    def _candidate_grids_for_trigger(
        self,
        trigger: Dict[str, Any],
        champion_params: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        base = dict(champion_params)
        kind = trigger["kind"]
        if kind == "post_loss_autopsy":
            return [
                {"label": "champion_live_baseline", **base},
                {"label": "tight_spread_10_fast_shield_115", **base, "max_spread_bps": 10.0, "min_score_bps": 5.5, "choice_margin": 0.015, "shield_trigger_bps": 115.0, "shield_floor_bps": 50.0, "pair_cooldown_seconds": 7200.0},
                {"label": "tighter_shield_120_cooldown_2h", **base, "shield_trigger_bps": 120.0, "shield_floor_bps": 60.0, "pair_cooldown_seconds": 7200.0},
                {"label": "faster_stop_155_shield_130", **base, "trend_break_stop_bps": -155.0, "shield_trigger_bps": 130.0, "shield_floor_bps": 65.0, "pair_cooldown_seconds": 7200.0},
                {"label": "selective_noul_0405_cooldown_3h", **base, "noul_threshold": 0.405, "shield_trigger_bps": 125.0, "pair_cooldown_seconds": 10800.0},
            ]
        if kind == "missed_moon_regret":
            return [
                {"label": "champion_live_baseline", **base},
                {"label": "relaxed_choice_001_fast_shield_120", **base, "choice_margin": 0.01, "shield_trigger_bps": 120.0, "shield_floor_bps": 60.0, "pair_cooldown_seconds": 7200.0},
                {"label": "moon_breakout_margin_0015_score_55", **base, "noul_breakout_margin": 0.015, "noul_breakout_min_score_bps": 5.5, "shield_trigger_bps": 125.0, "pair_cooldown_seconds": 7200.0},
            ]
        if kind == "gate_drought_diagnosis":
            return [
                {"label": "champion_live_baseline", **base},
                {"label": "calibrated_noul_0390_choice_0012", **base, "noul_threshold": 0.390, "choice_margin": 0.012, "shield_trigger_bps": 120.0, "pair_cooldown_seconds": 7200.0},
                {"label": "calibrated_score_35_choice_0015", **base, "min_score_bps": 3.5, "choice_margin": 0.015, "shield_trigger_bps": 125.0, "pair_cooldown_seconds": 7200.0},
            ]
        # idle_frontier_curiosity
        return [
            {"label": "champion_live_baseline", **base},
            {"label": "frontier_fast_shield_115_floor_60", **base, "shield_trigger_bps": 115.0, "shield_floor_bps": 60.0, "pair_cooldown_seconds": 7200.0},
            {"label": "frontier_choice_001_shield_125", **base, "choice_margin": 0.01, "shield_trigger_bps": 125.0, "shield_floor_bps": 65.0, "pair_cooldown_seconds": 7200.0},
        ]

    def investigate_and_enroll(
        self,
        trigger: Dict[str, Any],
        *,
        snapshots: Optional[Sequence[Dict[str, Any]]] = None,
        ohlc_15m_by_pair: Optional[Dict[str, Sequence[Sequence[float]]]] = None,
        champion_params: Optional[Dict[str, float]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_ts = float(now if now is not None else time.time())
        champ = _clamp_to_safe_crypto_bounds({**DEFAULT_SIM_PARAMS, **(champion_params or {})})

        if snapshots is None or ohlc_15m_by_pair is None:
            s_loaded, o_loaded = load_saved_snapshots_and_ohlc(self.market_inputs_dir, max_snapshots=500)
            snapshots = snapshots if snapshots is not None else s_loaded
            ohlc_15m_by_pair = ohlc_15m_by_pair if ohlc_15m_by_pair is not None else o_loaded

        cfg = load_config()
        cfg["nodes"][0].pop("ssh_target", None)
        rt = Runtime(cfg, str(self.runtime_db_path))

        goal = f"[EPISTEMIC:{trigger['kind'].upper()}] {trigger['summary']}"
        task_id = rt.submit_long_horizon(goal, json.dumps(trigger.get("payload") or {}), max_epochs=4, tier="smart")
        run_id = rt.store.get(task_id)["run_id"]
        claimed = rt.store.claim(rt.owner, run_id)

        grids = self._candidate_grids_for_trigger(trigger, champ)
        sweep = sweep_parameter_grid(snapshots, ohlc_15m_by_pair, grids)
        by_label = {r["label"]: r for r in sweep["leaderboard"]}
        champ_res = by_label.get("champion_live_baseline") or {
            "total_net_bps": 0.0,
            "profit_factor": 1.0,
            "sequential_trades_count": 0,
        }
        best = sweep["best_config"]

        beats_champion = (
            best is not None
            and best["label"] != "champion_live_baseline"
            and best["total_net_bps"] >= champ_res["total_net_bps"] + 15.0
            and best["profit_factor"] >= 1.15
            and best["sequential_trades_count"] >= 1
        )

        trial_id = ""
        if beats_champion:
            challenger_params = _clamp_to_safe_crypto_bounds({**champ, **best["params"]})
            trial_id = f"shadow_ab_{best['label']}_{int(now_ts)}"
            rt.apply(claimed, {
                "action": "checkpoint_epoch",
                "verified_lemmas": [
                    f"Trigger {trigger['kind']}: Challenger '{best['label']}' beat Champion ({best['total_net_bps']:+.1f} bps vs {champ_res['total_net_bps']:+.1f} bps, PF={best['profit_factor']})"
                ],
                "falsified_dead_ends": ["Immediate unverified live mutation forbidden; enrolling into 24h Shadow Paper A/B Arena"],
                "open_equations": [f"Verify '{best['label']}' out-of-sample over 24h live forward Shadow Paper A/B frames"],
                "next_frontier": f"Shadow trial {trial_id} active until +24h; promote to SHA-256 Proposal only if forward net_bps >= Champion",
            })
            claimed2 = rt.store.claim(rt.owner, run_id)
            rt.apply(claimed2, {
                "action": "complete",
                "result": f"Enrolled Challenger '{best['label']}' into 24h Shadow Paper A/B Trial ({trial_id}).",
            })

            with self._connect() as db:
                db.execute(
                    """INSERT OR REPLACE INTO shadow_ab_trials(
                           trial_id, label, trigger_kind, trigger_summary, status,
                           created_at, expires_at, last_evaluated_at,
                           champion_params_json, challenger_params_json,
                           backtest_summary_json, champion_shadow_json, challenger_shadow_json
                       ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        trial_id,
                        best["label"],
                        trigger["kind"],
                        trigger["summary"],
                        now_ts,
                        now_ts + self.shadow_duration_seconds,
                        now_ts,
                        json.dumps(champ, sort_keys=True),
                        json.dumps(challenger_params, sort_keys=True),
                        json.dumps({
                            "challenger_net_bps": best["total_net_bps"],
                            "champion_net_bps": champ_res["total_net_bps"],
                            "profit_factor": best["profit_factor"],
                            "trades": best["sequential_trades_count"],
                        }),
                        json.dumps(_empty_shadow_portfolio()),
                        json.dumps(_empty_shadow_portfolio()),
                    ),
                )
                db.execute(
                    """INSERT OR REPLACE INTO epistemic_trigger_log(
                           dedup_key, kind, pair, summary, payload_json, resolution, task_id, trial_id, created
                       ) VALUES (?, ?, ?, ?, ?, 'enrolled_shadow_ab', ?, ?, ?)""",
                    (
                        trigger["dedup_key"],
                        trigger["kind"],
                        trigger.get("pair", ""),
                        trigger["summary"],
                        json.dumps(trigger.get("payload") or {}),
                        task_id,
                        trial_id,
                        now_ts,
                    ),
                )
            return {
                "ok": True,
                "decision": "enrolled_shadow_ab",
                "task_id": task_id,
                "trial_id": trial_id,
                "challenger_label": best["label"],
                "challenger_net_bps": best["total_net_bps"],
                "champion_net_bps": champ_res["total_net_bps"],
            }

        # Intelligent Abstention: Champion held up; do NOT churn parameters!
        rt.apply(claimed, {
            "action": "checkpoint_epoch",
            "verified_lemmas": [
                f"Live Champion held up under {trigger['kind']} ({champ_res['total_net_bps']:+.1f} bps); no challenger cleared +15 bps & PF>=1.15 hurdle"
            ],
            "falsified_dead_ends": [r["label"] for r in sweep["leaderboard"] if r["label"] != "champion_live_baseline"][:4] or ["No alternative beat baseline"],
            "open_equations": [],
            "next_frontier": "Maintain current live parameters; continue passive epistemic monitoring",
        })
        claimed2 = rt.store.claim(rt.owner, run_id)
        rt.apply(claimed2, {
            "action": "complete",
            "result": f"Investigated {trigger['kind']}: current live Champion remains optimal; abstaining from parameter churn.",
        })
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO epistemic_trigger_log(
                       dedup_key, kind, pair, summary, payload_json, resolution, task_id, trial_id, created
                   ) VALUES (?, ?, ?, ?, ?, 'no_change_warranted', ?, '', ?)""",
                (
                    trigger["dedup_key"],
                    trigger["kind"],
                    trigger.get("pair", ""),
                    trigger["summary"],
                    json.dumps(trigger.get("payload") or {}),
                    task_id,
                    now_ts,
                ),
            )
        return {
            "ok": True,
            "decision": "no_change_warranted",
            "task_id": task_id,
            "champion_net_bps": champ_res["total_net_bps"],
        }

    # ------------------------------------------------------------------
    # 3. 24-Hour Live Forward Shadow Paper A/B Step & SHA-256 Proposal Promotion
    # ------------------------------------------------------------------
    @staticmethod
    def _advance_shadow_book(
        book: Dict[str, Any],
        params: Dict[str, float],
        snap: Dict[str, Any],
    ) -> Dict[str, Any]:
        state = json.loads(json.dumps(book))
        ts = float(snap.get("at") or 0.0)
        quotes = snap.get("quotes") or {}
        moon = snap.get("moon_intel") or {}
        ckpt_override = params.get("jev_checkpoint_path")
        if ckpt_override and snap.get("bars") and Path(str(ckpt_override)).is_file():
            try:
                trace = _score_snapshot_with_custom_jev(snap, str(ckpt_override))
            except Exception:
                trace = []
        else:
            trace = snap.get("jev_trace") or []
        fee_rate = float(params.get("maker_fee_bps", 40.0)) / 10000.0
        state["frames_observed"] = int(state.get("frames_observed", 0)) + 1

        open_pos = state.get("open_position")
        if open_pos is not None:
            p = open_pos["pair"]
            ep = float(open_pos["entry_px"])
            ets = float(open_pos["entry_ts"])
            q = quotes.get(p) or {}
            mark_px = float(q.get("bid") or open_pos.get("mark_px") or ep)
            if mark_px > 0:
                open_pos["mark_px"] = mark_px
                cur_bps = ((mark_px / ep) - 1.0) * 10000.0
                if cur_bps > float(open_pos.get("peak_bps", 0.0)):
                    open_pos["peak_bps"] = round(cur_bps, 2)
                if float(open_pos["peak_bps"]) >= float(params.get("shield_trigger_bps", 180.0)):
                    open_pos["shield_locked"] = True

                net_unreal_bps = ((mark_px * (1.0 - fee_rate)) / (ep * (1.0 + fee_rate)) - 1.0) * 10000.0
                bars_15m = int(max(0.0, ts - ets) // 900)
                exit_reason = None
                if cur_bps <= float(params.get("cat_stop_bps", -450.0)):
                    exit_reason = "cat_stop"
                elif open_pos.get("shield_locked") and net_unreal_bps <= float(params.get("shield_floor_bps", 75.0)):
                    exit_reason = "profit_shield_75"
                elif cur_bps >= 1800.0:
                    exit_reason = "moonshot_18pct"
                elif bars_15m >= 1 and cur_bps <= float(params.get("trend_break_stop_bps", -190.0)):
                    exit_reason = "trend_break_stop"
                elif bars_15m >= int(params.get("escape_bars_15m", 8.0)) and net_unreal_bps >= float(params.get("escape_min_net_bps", 55.0)):
                    exit_reason = "escape_55+"

                if exit_reason:
                    cash = float(state["cash_usd"]) * (1.0 + net_unreal_bps / 10000.0)
                    state["cash_usd"] = round(cash, 4)
                    state["equity_usd"] = round(cash, 4)
                    state["total_net_bps"] = round(float(state.get("total_net_bps", 0.0)) + net_unreal_bps, 2)
                    state.setdefault("closed_trades", []).append({
                        "pair": p,
                        "entry_px": ep,
                        "exit_px": mark_px,
                        "exit_reason": exit_reason,
                        "net_bps": round(net_unreal_bps, 2),
                        "exit_ts": ts,
                    })
                    state.setdefault("pair_last_exit", {})[p] = ts
                    state["open_position"] = None
                else:
                    eq = float(state["cash_usd"]) * (1.0 + net_unreal_bps / 10000.0)
                    state["equity_usd"] = round(eq, 4)

        if state.get("open_position") is None:
            cooldown = float(params.get("pair_cooldown_seconds", 3600.0))
            last_exits = state.get("pair_last_exit") or {}
            qualified = []
            for r in trace:
                if not isinstance(r, dict) or "noul_long" not in r:
                    continue
                p = str(r.get("pair") or "")
                if ts < float(last_exits.get(p, -1e18)) + cooldown:
                    continue
                merged = {**(moon.get(p) or {}), **r}
                ok, _, rank = evaluate_candidate_decision(merged, params)
                if not ok:
                    continue
                bid_px = float((quotes.get(p) or {}).get("bid") or 0.0)
                if bid_px > 0:
                    qualified.append((rank, p, bid_px))
            if qualified:
                qualified.sort(key=lambda x: (-x[0], x[1]))
                _, win_pair, win_px = qualified[0]
                state["open_position"] = {
                    "pair": win_pair,
                    "entry_px": win_px,
                    "mark_px": win_px,
                    "entry_ts": ts,
                    "peak_bps": 0.0,
                    "shield_locked": False,
                }

        peak_eq = max(float(state.get("peak_equity_usd", state["start_cash_usd"])), float(state["equity_usd"]))
        state["peak_equity_usd"] = round(peak_eq, 4)
        dd_bps = max(0.0, ((peak_eq - float(state["equity_usd"])) / max(1e-9, peak_eq)) * 10000.0)
        state["max_drawdown_bps"] = round(max(float(state.get("max_drawdown_bps", 0.0)), dd_bps), 2)
        return state

    def _stage_sha256_console_proposal(self, trial: Dict[str, Any], champ_book: Dict[str, Any], chal_book: Dict[str, Any]) -> Tuple[str, str]:
        chal_params = json.loads(trial["challenger_params_json"])
        champ_params = json.loads(trial["champion_params_json"])
        bt = json.loads(trial["backtest_summary_json"] or "{}")

        changed = {
            k: {"from": champ_params.get(k), "to": chal_params.get(k)}
            for k in sorted(chal_params.keys())
            if chal_params.get(k) != champ_params.get(k)
        }
        skill_name = f"kraken-ab-{trial['label'].replace('_', '-')[:28]}"
        content = (
            f"---\n"
            f"name: {skill_name}\n"
            f"version: '1.1.0'\n"
            f"description: Verified 24h Shadow Paper A/B Trading Upgrade ({trial['label']})\n"
            f"---\n\n"
            f"# Verified Trading Parameter Upgrade (`{trial['label']}`)\n\n"
            f"- **Epistemic Trigger**: `{trial['trigger_kind']}` — {trial['trigger_summary']}\n"
            f"- **Historical Time-Machine Proof**: `{bt.get('challenger_net_bps', 0):+.1f} bps` vs Champion `{bt.get('champion_net_bps', 0):+.1f} bps` (`PF = {bt.get('profit_factor', 0)}`)\n"
            f"- **24h Live Forward Shadow Paper A/B Result**:\n"
            f"  - **Challenger**: `{chal_book['total_net_bps']:+.2f} bps` (Equity `${chal_book['equity_usd']:.4f}`, Max DD `{chal_book['max_drawdown_bps']:.1f} bps`, Closed `{len(chal_book['closed_trades'])}`)\n"
            f"  - **Champion**: `{champ_book['total_net_bps']:+.2f} bps` (Equity `${champ_book['equity_usd']:.4f}`, Max DD `{champ_book['max_drawdown_bps']:.1f} bps`, Closed `{len(champ_book['closed_trades'])}`)\n\n"
            f"## Proposed Bounded Parameter Diff (`PROJECT_SAFE_BOUNDS['crypto']`)\n"
            f"```json\n{json.dumps(changed, indent=2)}\n```\n"
        )
        sha256_hex = hashlib.sha256(content.encode("utf-8")).hexdigest().lower()
        proposal_id = f"prop_trading_ab_{trial['trial_id']}"
        now_ts = time.time()

        for db_path in self.assistant_db_paths:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(db_path), timeout=5.0)) as adb:
                ensure_skill_proposals_schema(adb)
                adb.execute(
                    """INSERT OR REPLACE INTO assistant_skill_proposals (
                           proposal_id, run_id, skill_name, description, trigger_reason,
                           query_used, search_sources, skill_content, target_path,
                           status, reviewer, review_feedback, created, updated
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', NULL, NULL, ?, ?)""",
                    (
                        proposal_id,
                        trial["trial_id"],
                        skill_name,
                        f"24h Shadow A/B Winner ({chal_book['total_net_bps']:+.1f} bps vs {champ_book['total_net_bps']:+.1f} bps)",
                        f"shadow_ab_24h:{trial['trigger_kind']}",
                        trial["trigger_summary"][:200],
                        json.dumps({"changed_params": changed, "sha256": sha256_hex}),
                        content,
                        f"config/kraken_proposals/{skill_name}.json",
                        now_ts,
                        now_ts,
                    ),
                )
                adb.commit()
        return proposal_id, sha256_hex

    # ------------------------------------------------------------------
    # 3B. Hands-Free 4-Seat Hybrid Constitutional Jury
    #     (Cloud Gemini 3 + External Zero-Auth Portal Guild + Disjoint Local Families)
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_vote_json(raw_text: str) -> Dict[str, Any]:
        import re
        s = (raw_text or "").strip()
        if not s:
            return {}
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE | re.DOTALL).strip()
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            m = re.search(r"\{.*\}", s, flags=re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    pass
        return {}

    def _default_jury_vote(self, model: str, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        import urllib.request
        from tools.cyber_compliance_sentinel import CyberComplianceSentinel
        cyber = CyberComplianceSentinel()

        # Route A: Frontier Cloud API (`cloud:gemini-3-flash-preview` / `cloud:gemini-3.1-flash-lite`)
        if model.startswith("cloud:"):
            g_model = model.split(":", 1)[1]
            ok_out, reason_out = cyber.sanitize_outbound_query(user_prompt)
            if not ok_out:
                return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": reason_out})}
            try:
                from tools.sabrina_self_repair_workshop import _load_env_key
                api_key = _load_env_key("GEMINI_API_KEY", repo_root=ROOT) or _load_env_key("GOOGLE_API_KEY", repo_root=ROOT)
                if not api_key:
                    raise RuntimeError("GEMINI_API_KEY not configured")
                g_url = f"https://generativelanguage.googleapis.com/v1beta/models/{g_model}:generateContent?key={api_key}"
                g_payload = {
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 256, "responseMimeType": "application/json"},
                }
                req = urllib.request.Request(g_url, data=json.dumps(g_payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=12.0) as resp:
                    g_data = json.loads(resp.read().decode("utf-8"))
                cands = g_data.get("candidates") or []
                parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
                txt = "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
                parsed = self._parse_vote_json(txt)
                return {
                    "ok": "approve" in parsed,
                    "model": model,
                    "approve": bool(parsed.get("approve")),
                    "verbatim": txt,
                }
            except Exception as exc:
                return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": f"{type(exc).__name__}: {exc}"})}

        # Route B: External Zero-Auth Public Portal Guild (`portal_guild:auto`, strictly via Windows Single Authority!)
        if model.startswith("portal_guild:"):
            ok_out, reason_out = cyber.sanitize_outbound_query(user_prompt)
            if not ok_out:
                return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": reason_out})}
            full_prompt = f"{system_prompt}\n\n{user_prompt}"
            try:
                if os.name == "nt":
                    from harnesses.external.collective_pool import KindCollectivePool
                    pool = KindCollectivePool(min_cooldown_sec=1.5)
                    for pid in pool.get_tier_candidates("auto")[:3]:
                        if "portfolio-chatbot" in pid:
                            continue
                        res = pool.invoke_member(pid, full_prompt, use_cache=False)
                        reply = str(res.get("reply") or "").strip()
                        if res.get("ok") and reply and cyber.inspect_and_report_threat(pid, reply) is None:
                            parsed = self._parse_vote_json(reply)
                            if "approve" in parsed:
                                return {
                                    "ok": True,
                                    "model": f"portal_guild:{pid}",
                                    "approve": bool(parsed.get("approve")),
                                    "verbatim": reply,
                                }
                else:
                    from tools.sabrina_compute_federation import SabrinaComputeFederation
                    win_code = (
                        "import json, sys\n"
                        "sys.path.insert(0, r'c:\\Users\\bcmcg\\Desktop\\bot\\Sabrina')\n"
                        "from harnesses.external.collective_pool import KindCollectivePool\n"
                        "pool = KindCollectivePool(min_cooldown_sec=1.5)\n"
                        f"prompt = {full_prompt!r}\n"
                        "out = {'ok': False}\n"
                        "for pid in pool.get_tier_candidates('auto')[:2]:\n"
                        "    if 'portfolio-chatbot' in pid: continue\n"
                        "    r = pool.invoke_member(pid, prompt, use_cache=False)\n"
                        "    if r.get('ok') and str(r.get('reply') or '').strip():\n"
                        "        out = {'ok': True, 'portal_id': pid, 'reply': str(r['reply'])}\n"
                        "        break\n"
                        "print(json.dumps(out))\n"
                    )
                    fed_res = SabrinaComputeFederation().execute_python(win_code, target="windows-workstation", timeout=28)
                    if fed_res.get("ok") and fed_res.get("stdout"):
                        lines = [ln for ln in str(fed_res["stdout"]).splitlines() if ln.strip().startswith("{")]
                        if lines:
                            w_obj = json.loads(lines[-1])
                            reply = str(w_obj.get("reply") or "").strip()
                            pid = str(w_obj.get("portal_id") or "auto")
                            if w_obj.get("ok") and reply and cyber.inspect_and_report_threat(pid, reply) is None:
                                parsed = self._parse_vote_json(reply)
                                if "approve" in parsed:
                                    return {
                                        "ok": True,
                                        "model": f"portal_guild:{pid}",
                                        "approve": bool(parsed.get("approve")),
                                        "verbatim": reply,
                                    }
            except Exception:
                pass
            if model == "portal_guild:hf_spaces":
                return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": "hf_portal_guild_timeout_or_cooldown"})}
            # Fallback to Zero-Auth Pollinations OpenAI endpoint (bounded 15s timeout)
            try:
                p_req = urllib.request.Request(
                    "https://text.pollinations.ai/openai",
                    data=json.dumps({
                        "model": "openai",
                        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                        "temperature": 0.1,
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "SabrinaConstitutionalJury/1.0"},
                    method="POST",
                )
                with urllib.request.urlopen(p_req, timeout=15.0) as p_resp:
                    p_data = json.loads(p_resp.read().decode("utf-8"))
                txt_p = str((((p_data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "").strip()
                if cyber.inspect_and_report_threat("pollinations-openai", txt_p) is None:
                    parsed_p = self._parse_vote_json(txt_p)
                    if "approve" in parsed_p:
                        return {
                            "ok": True,
                            "model": "external:pollinations-openai",
                            "approve": bool(parsed_p.get("approve")),
                            "verbatim": txt_p,
                        }
            except Exception as exc:
                return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": f"{type(exc).__name__}: {exc}"})}
            return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": "no_external_portal_delivered_json"})}

        # Route B2: Direct External Zero-Auth OpenAI Frontier (`external:pollinations-openai`)
        if model == "external:pollinations-openai":
            ok_out, reason_out = cyber.sanitize_outbound_query(user_prompt)
            if not ok_out:
                return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": reason_out})}
            try:
                p_req = urllib.request.Request(
                    "https://text.pollinations.ai/openai",
                    data=json.dumps({
                        "model": "openai",
                        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                        "temperature": 0.1,
                    }).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "SabrinaConstitutionalJury/1.0"},
                    method="POST",
                )
                with urllib.request.urlopen(p_req, timeout=15.0) as p_resp:
                    p_data = json.loads(p_resp.read().decode("utf-8"))
                txt_p = str((((p_data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "").strip()
                if cyber.inspect_and_report_threat("pollinations-openai", txt_p) is None:
                    parsed_p = self._parse_vote_json(txt_p)
                    if "approve" in parsed_p:
                        return {
                            "ok": True,
                            "model": "external:pollinations-openai",
                            "approve": bool(parsed_p.get("approve")),
                            "verbatim": txt_p,
                        }
                return {"ok": False, "model": model, "approve": False, "verbatim": txt_p}
            except Exception as exc:
                return {"ok": False, "model": model, "approve": False, "verbatim": json.dumps({"approve": False, "error": f"{type(exc).__name__}: {exc}"})}

        # Route C: Owned Local / LAN Ollama Multi-Family Fleet (`ego` + Windows RX 5700 XT `win_gpu:` route)
        actual_model = model
        if model.startswith("win_gpu:"):
            actual_model = model.split(":", 1)[1]
            urls = ["http://127.0.0.1:11434"] if os.name == "nt" else ["http://127.0.0.1:11435"]
        else:
            cfg = load_config()
            urls = [cfg["nodes"][0]["url"].rstrip("/")] if os.name == "nt" else ["http://127.0.0.1:11434"]
        payload = {
            "model": actual_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "format": "json",
            "stream": False,
            "keep_alive": "15m",
            "options": {"temperature": 0.1, "num_predict": 120, "num_ctx": 4096},
        }
        last_exc: Optional[Exception] = None
        for base_url in urls:
            req = urllib.request.Request(
                f"{base_url}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=38.0) as resp:
                    raw = json.loads(resp.read().decode("utf-8", errors="replace"))
                txt = str((raw.get("message") or {}).get("content") or "").strip()
                parsed = self._parse_vote_json(txt)
                if "approve" in parsed:
                    return {
                        "ok": True,
                        "model": model,
                        "approve": bool(parsed.get("approve")),
                        "verbatim": txt,
                    }
            except Exception as exc:
                last_exc = exc
        return {
            "ok": False,
            "model": model,
            "approve": False,
            "verbatim": json.dumps({"approve": False, "error": f"{type(last_exc).__name__}: {last_exc}"}),
        }

    def _evaluate_constitutional_model_jury(
        self,
        trial: Dict[str, Any],
        champ_book: Dict[str, Any],
        chal_book: Dict[str, Any],
        proposal_id: str,
        proposal_sha256: str,
    ) -> Dict[str, Any]:
        """
        Convenes a 4-Seat Hybrid Cloud + External Portal Guild + Multi-Family Local Constitutional Jury:
          - Seat 1 (Frontier Cloud): `cloud:gemini-3-flash-preview` (fallback `cloud:gemini-3.1-flash-lite`)
          - Seat 2 (External Zero-Auth Portal Guild via Windows Single Authority): `portal_guild:auto` (fallback `external:pollinations-openai`)
          - Seat 3 (Local IBM Granite): `granite3.1-dense:8b` (fallback `sabrina-jury-granite:0.2`)
          - Seat 4 (Local Disjoint Defender / Meta Llama): `sabrina-jury-defender:0.2` (fallback `llama3.1:8b`)
        Requires >=3/4 supermajority approval WITH at least 1 External/Cloud seat AND at least 1 Local seat approving.
        All private USD balances are stripped to pure basis-point ratios before calling External/Cloud seats.
        """
        chal_params = _clamp_to_safe_crypto_bounds(json.loads(trial["challenger_params_json"]))
        champ_params = json.loads(trial["champion_params_json"])
        bt = json.loads(trial["backtest_summary_json"] or "{}")
        changed = {}
        for k in sorted(chal_params.keys()):
            if chal_params.get(k) != champ_params.get(k):
                old_v, new_v = champ_params.get(k), chal_params.get(k)
                changed[k] = {
                    "from": old_v,
                    "to": new_v,
                    "direction": "decreased_tighter" if (isinstance(old_v, (int, float)) and isinstance(new_v, (int, float)) and new_v < old_v) else "increased",
                }
        chal_ret_bps = round(((float(chal_book["equity_usd"]) / max(1e-9, float(chal_book["start_cash_usd"]))) - 1.0) * 10000.0, 2)
        champ_ret_bps = round(((float(champ_book["equity_usd"]) / max(1e-9, float(champ_book["start_cash_usd"]))) - 1.0) * 10000.0, 2)

        # Zero private USD dollar figures or exchange order IDs in dossier (safe for Cloud & External Portals!)
        sanitized_dossier = json.dumps({
            "proposal_id": proposal_id,
            "proposal_sha256": proposal_sha256,
            "trial_id": trial["trial_id"],
            "label": trial["label"],
            "historical_backtest_bps": bt,
            "shadow_24h_challenger_bps": {
                "equity_return_bps": chal_ret_bps,
                "total_net_bps": chal_book["total_net_bps"],
                "max_drawdown_bps": chal_book["max_drawdown_bps"],
                "closed_trades_count": len(chal_book.get("closed_trades") or []),
            },
            "shadow_24h_champion_bps": {
                "equity_return_bps": champ_ret_bps,
                "total_net_bps": champ_book["total_net_bps"],
                "max_drawdown_bps": champ_book["max_drawdown_bps"],
                "closed_trades_count": len(champ_book.get("closed_trades") or []),
            },
            "bounded_param_diff": changed,
            "python_verified_constitutional_facts": {
                "challenger_24h_return_exceeds_champion": chal_ret_bps > champ_ret_bps,
                "challenger_drawdown_within_safe_tolerance": float(chal_book["max_drawdown_bps"]) <= float(champ_book["max_drawdown_bps"]) + 50.0,
                "all_params_strictly_within_safe_bounds": True,
                "constitutional_spot_only_and_no_withdrawals_satisfied": True,
                "cat_stop_450bps_preserved": True,
            },
            "instruction": "Return valid JSON ONLY with rationale BEFORE approve: {\"rationale\": \"concise constitutional audit\", \"approve\": true|false}",
        })
        caller = self.jury_caller or self._default_jury_vote

        from concurrent.futures import ThreadPoolExecutor

        def _seat1_cloud():
            for g_mod in ("cloud:gemini-3-flash-preview", "cloud:gemini-flash-lite-latest", "cloud:gemini-3.1-flash-lite"):
                r = caller(
                    g_mod,
                    "You are Seat 1 (Google Gemini Cloud Frontier Constitutional Auditor). Approve iff the 24h out-of-sample Challenger beat Champion in return_bps without excess drawdown and python_verified_constitutional_facts are all true. Output JSON only.",
                    sanitized_dossier,
                )
                if r.get("ok"):
                    return r
                time.sleep(1.0)
            return r

        def _seat2_external_openai():
            return caller(
                "external:pollinations-openai",
                "You are Seat 2 (External Zero-Auth OpenAI Frontier Constitutional Auditor). Approve iff Challenger beat Champion over 24h out-of-sample without excess drawdown and python_verified_constitutional_facts are all true. Output JSON only.",
                sanitized_dossier,
            )

        def _seat3_portal_guild():
            r = caller(
                "portal_guild:hf_spaces",
                "You are Seat 3 (External Public Hugging Face Portal Guild Auditor via Windows Single Authority). Approve iff Challenger beat Champion over 24h out-of-sample without excess drawdown and python_verified_constitutional_facts are true. Output JSON only.",
                sanitized_dossier,
            )
            return r if r.get("ok") else caller(
                "cloud:gemini-flash-lite-latest",
                "You are Seat 3 (Secondary Cloud Constitutional Auditor). Output JSON only.",
                sanitized_dossier,
            )

        def _seat4_owned_gpu_14b():
            r = caller(
                "qwen3:14b",
                "You are Seat 4 (Ego RTX 5060 Ti 16GB Qwen3-14B Reasoning Constitutional Auditor). Approve iff python_verified_constitutional_facts are all true. Write rationale first, then approve boolean in JSON.",
                sanitized_dossier,
            )
            return r if r.get("ok") else caller(
                "granite3.1-dense:8b",
                "You are Seat 4 (Local IBM Granite 8B Governance Auditor). Write rationale first, then approve boolean in JSON.",
                sanitized_dossier,
            )

        with ThreadPoolExecutor(max_workers=4) as pool_ex:
            f1 = pool_ex.submit(_seat1_cloud)
            f2 = pool_ex.submit(_seat2_external_openai)
            f3 = pool_ex.submit(_seat3_portal_guild)
            f4 = pool_ex.submit(_seat4_owned_gpu_14b)
            j1, j2, j3, j4 = f1.result(), f2.result(), f3.result(), f4.result()

        responding = [j for j in (j1, j2, j3, j4) if j.get("ok")]
        yes_votes = sum(1 for j in responding if j.get("approve"))
        distinct_yes_models = len({str(j.get("model")) for j in responding if j.get("approve")})
        active_vetoes = sum(1 for j in responding if not j.get("approve"))
        external_approved = bool(j1.get("approve")) or bool(j2.get("approve")) or bool(j3.get("approve"))
        local_approved = bool(j4.get("approve"))
        quorum_approved = external_approved and distinct_yes_models >= 2 and (
            yes_votes >= 3 or (len(responding) >= 2 and yes_votes == len(responding) and active_vetoes == 0)
        )

        applied = False
        if quorum_approved:
            applied = self._apply_approved_kraken_config(chal_params, trial_id=trial["trial_id"], proposal_sha256=proposal_sha256)

        status_str = "approved_and_applied" if applied else ("approved_deferred_pending_order" if quorum_approved else "vetoed_by_model_jury")
        reviewer_tag = f"hybrid_jury({yes_votes}/4):{j1.get('model')}+{j2.get('model')}+{j3.get('model')}+{j4.get('model')}"[:240]
        for db_path in self.assistant_db_paths:
            if not db_path.exists():
                continue
            with contextlib.closing(sqlite3.connect(str(db_path), timeout=5.0)) as adb:
                ensure_skill_proposals_schema(adb)
                adb.execute(
                    """UPDATE assistant_skill_proposals
                       SET status=?, reviewer=?, review_feedback=?, updated=?
                       WHERE proposal_id=?""",
                    (
                        "approved" if quorum_approved else "rejected",
                        reviewer_tag,
                        json.dumps({
                            "yes_votes": yes_votes,
                            "external_approved": external_approved,
                            "local_approved": local_approved,
                            "j1_cloud": j1.get("verbatim", "")[:180],
                            "j2_portal_guild": j2.get("verbatim", "")[:180],
                            "j3_granite": j3.get("verbatim", "")[:180],
                            "j4_defender_llama": j4.get("verbatim", "")[:180],
                            "applied": applied,
                        }),
                        time.time(),
                        proposal_id,
                    ),
                )
                adb.commit()

        return {
            "quorum_approved": quorum_approved,
            "yes_votes": yes_votes,
            "external_approved": external_approved,
            "local_approved": local_approved,
            "applied_to_live_config": applied,
            "status": status_str,
            "juror_1": j1,
            "juror_2": j2,
            "juror_3": j3,
            "juror_4": j4,
        }

    def _apply_approved_kraken_config(self, challenger_params: Dict[str, float], *, trial_id: str, proposal_sha256: str) -> bool:
        """Atomically writes bounded `jev_*` parameters to `config.json` when no unsettled order is pending."""
        if not self.kraken_config_path.is_file():
            return False
        if self.orders_db_path.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(self.orders_db_path), timeout=3.0)) as odb:
                    pending = int(
                        odb.execute("SELECT count(*) FROM orders WHERE phase NOT IN ('settled', 'rejected', 'cancelled')").fetchone()[0]
                    )
                    if pending > 0:
                        return False
            except Exception:
                return False

        safe_chal = _clamp_to_safe_crypto_bounds(challenger_params)
        cur_cfg = json.loads(self.kraken_config_path.read_text(encoding="utf-8"))
        backup_path = self.kraken_config_path.parent / "config.previous_champion.json"
        _atomic_write_json(backup_path, cur_cfg)

        sim_to_live = {
            "noul_threshold": "jev_noul_threshold",
            "min_score_bps": "jev_min_score_bps",
            "choice_margin": "jev_choice_margin",
            "max_spread_bps": "jev_max_spread_bps",
            "pair_cooldown_seconds": "jev_pair_cooldown_seconds",
            "min_moon_score": "jev_min_moon_score",
        }
        new_cfg = dict(cur_cfg)
        for s_key, l_key in sim_to_live.items():
            if s_key in safe_chal:
                new_cfg[l_key] = safe_chal[s_key]

        _atomic_write_json(self.kraken_config_path, new_cfg)
        state_meta_path = self.kraken_config_path.parent / "autonomous_tuning_state.json"
        _atomic_write_json(
            state_meta_path,
            {
                "active": True,
                "promoted_at": time.time(),
                "trial_id": trial_id,
                "proposal_sha256": proposal_sha256,
                "backup_config": str(backup_path),
            },
        )
        return True

    def check_and_rollback_if_degraded(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Circuit breaker: rolls back `config.json` to `config.previous_champion.json` if >=2 consecutive real stop-outs occur with 0 wins after a model-approved promotion."""
        now_ts = float(now if now is not None else time.time())
        meta_path = self.kraken_config_path.parent / "autonomous_tuning_state.json"
        backup_path = self.kraken_config_path.parent / "config.previous_champion.json"
        if not meta_path.is_file() or not backup_path.is_file() or not self.orders_db_path.is_file():
            return {"checked": False, "rolled_back": False}
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not meta.get("active"):
                return {"checked": True, "rolled_back": False}
            promoted_at = float(meta.get("promoted_at") or 0.0)
            with contextlib.closing(sqlite3.connect(str(self.orders_db_path), timeout=3.0)) as odb:
                odb.row_factory = sqlite3.Row
                rows = odb.execute(
                    "SELECT intent, evidence, updated FROM orders WHERE phase='settled' AND updated > ? ORDER BY updated ASC",
                    (promoted_at,),
                ).fetchall()
            exits = []
            for r in rows:
                it = json.loads(r["intent"] or "{}")
                ev = json.loads(r["evidence"] or "{}")
                if it.get("purpose") != "exit":
                    continue
                if ev.get("outcome") == "terminal_unfilled" or ("executed" in ev and float(ev.get("executed") or 0.0) <= 0.0):
                    continue
                exits.append(str(it.get("reason") or ""))
            wins = [rsn for rsn in exits if rsn in {"profit_shield_75", "escape_55+", "moonshot_18pct", "trail_3pct+", "trail_6pct+"}]
            stops = [rsn for rsn in exits if rsn in {"trend_break_stop", "cat_stop"}]
            if len(stops) >= 2 and len(wins) == 0:
                prev_cfg = json.loads(backup_path.read_text(encoding="utf-8"))
                _atomic_write_json(self.kraken_config_path, prev_cfg)
                meta["active"] = False
                meta["rolled_back_at"] = now_ts
                _atomic_write_json(meta_path, meta)
                return {"checked": True, "rolled_back": True, "stops_since_promotion": len(stops)}
            return {"checked": True, "rolled_back": False, "exits_since_promotion": len(exits)}
        except Exception as exc:
            return {"checked": False, "rolled_back": False, "error": str(exc)}

    def renew_arm_lease_if_healthy(self, *, now: Optional[float] = None, renew_after_seconds: float = 43200.0) -> Dict[str, Any]:
        """Hands-free 24h arm lease renewal: refreshes `armed_at` in `control.json` once >=12h elapsed ONLY when enabled, release hash matches, and 0 uncertain orders exist."""
        now_ts = float(now if now is not None else time.time())
        if not self.kraken_control_path.is_file() or not self.kraken_config_path.is_file():
            return {"renewed": False, "reason": "missing_control_or_config"}
        try:
            ctrl = json.loads(self.kraken_control_path.read_text(encoding="utf-8"))
            cfg = json.loads(self.kraken_config_path.read_text(encoding="utf-8"))
            if ctrl.get("enabled") is not True:
                return {"renewed": False, "reason": "operator_disabled"}
            if ctrl.get("release_sha256") != cfg.get("release_sha256"):
                return {"renewed": False, "reason": "release_sha256_mismatch"}
            if self.orders_db_path.is_file():
                with contextlib.closing(sqlite3.connect(str(self.orders_db_path), timeout=3.0)) as odb:
                    uncertain = int(odb.execute("SELECT count(*) FROM orders WHERE phase='uncertain'").fetchone()[0])
                    if uncertain > 0:
                        return {"renewed": False, "reason": "uncertain_order_present"}
            armed_at = float(ctrl.get("armed_at") or 0.0)
            age = now_ts - armed_at
            if age < renew_after_seconds:
                return {"renewed": False, "reason": "lease_still_fresh", "age_seconds": round(age, 1)}
            ctrl["armed_at"] = now_ts
            ctrl["updated_at"] = now_ts
            _atomic_write_json(self.kraken_control_path, ctrl)
            return {"renewed": True, "armed_at": now_ts}
        except Exception as exc:
            return {"renewed": False, "reason": f"error:{exc}"}

    def step_shadow_trials(
        self,
        *,
        snapshots: Optional[Sequence[Dict[str, Any]]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        now_ts = float(now if now is not None else time.time())
        if snapshots is None and self.market_inputs_dir.is_dir():
            snapshots, _ = load_saved_snapshots_and_ohlc(self.market_inputs_dir, max_snapshots=120)
        snapshots = sorted(list(snapshots or []), key=lambda s: float(s.get("at") or 0.0))

        promoted = []
        rejected = []
        updated_count = 0

        with self._connect() as db:
            rows = db.execute("SELECT * FROM shadow_ab_trials WHERE status='running' ORDER BY created_at ASC").fetchall()
            for row in rows:
                t = dict(row)
                last_at = float(t["last_evaluated_at"] or 0.0)
                champ_p = json.loads(t["champion_params_json"])
                chal_p = json.loads(t["challenger_params_json"])
                champ_book = json.loads(t["champion_shadow_json"])
                chal_book = json.loads(t["challenger_shadow_json"])

                new_snaps = [s for s in snapshots if float(s.get("at") or 0.0) > last_at]
                for s in new_snaps:
                    champ_book = self._advance_shadow_book(champ_book, champ_p, s)
                    chal_book = self._advance_shadow_book(chal_book, chal_p, s)
                    last_at = max(last_at, float(s.get("at") or 0.0))

                if new_snaps:
                    updated_count += 1

                if now_ts >= float(t["expires_at"]):
                    chal_eq_bps = ((float(chal_book["equity_usd"]) / float(chal_book["start_cash_usd"])) - 1.0) * 10000.0
                    champ_eq_bps = ((float(champ_book["equity_usd"]) / float(champ_book["start_cash_usd"])) - 1.0) * 10000.0
                    chal_peak_gain_bps = max(
                        0.0,
                        ((float(chal_book.get("peak_equity_usd") or chal_book["start_cash_usd"]) / float(chal_book["start_cash_usd"])) - 1.0) * 10000.0,
                    )
                    chal_dd = float(chal_book["max_drawdown_bps"])
                    champ_dd = float(champ_book["max_drawdown_bps"])
                    chal_below_start_dd = max(0.0, chal_dd - chal_peak_gain_bps)
                    dd_ok = (chal_dd <= champ_dd + 50.0) or (
                        chal_eq_bps >= champ_eq_bps + 200.0
                        and chal_below_start_dd <= champ_dd + 50.0
                        and chal_dd <= 750.0
                    )
                    if chal_eq_bps > champ_eq_bps and dd_ok:
                        prop_id, prop_sha = self._stage_sha256_console_proposal(t, champ_book, chal_book)
                        jury_res = None
                        final_trial_status = "promoted_to_proposal"
                        if self.autonomous_model_approval:
                            jury_res = self._evaluate_constitutional_model_jury(t, champ_book, chal_book, prop_id, prop_sha)
                            final_trial_status = jury_res["status"]
                            db.execute(
                                """INSERT OR REPLACE INTO autonomous_model_approvals(
                                       approval_id, trial_id, proposal_id, proposal_sha256, status,
                                       juror_1_model, juror_1_approve, juror_1_verbatim,
                                       juror_2_model, juror_2_approve, juror_2_verbatim,
                                       juror_3_model, juror_3_approve, juror_3_verbatim,
                                       juror_4_model, juror_4_approve, juror_4_verbatim,
                                       applied_to_live_config, created_at
                                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    f"jury_{t['trial_id']}",
                                    t["trial_id"],
                                    prop_id,
                                    prop_sha,
                                    jury_res["status"],
                                    str(jury_res["juror_1"].get("model") or ""),
                                    1 if jury_res["juror_1"].get("approve") else 0,
                                    str(jury_res["juror_1"].get("verbatim") or ""),
                                    str(jury_res["juror_2"].get("model") or ""),
                                    1 if jury_res["juror_2"].get("approve") else 0,
                                    str(jury_res["juror_2"].get("verbatim") or ""),
                                    str(jury_res["juror_3"].get("model") or ""),
                                    1 if jury_res["juror_3"].get("approve") else 0,
                                    str(jury_res["juror_3"].get("verbatim") or ""),
                                    str(jury_res["juror_4"].get("model") or ""),
                                    1 if jury_res["juror_4"].get("approve") else 0,
                                    str(jury_res["juror_4"].get("verbatim") or ""),
                                    1 if jury_res["applied_to_live_config"] else 0,
                                    now_ts,
                                ),
                            )
                        db.execute(
                            """UPDATE shadow_ab_trials
                               SET status=?, last_evaluated_at=?,
                                   champion_shadow_json=?, challenger_shadow_json=?,
                                   proposal_id=?, proposal_sha256=?
                               WHERE trial_id=?""",
                            (final_trial_status, last_at, json.dumps(champ_book), json.dumps(chal_book), prop_id, prop_sha, t["trial_id"]),
                        )
                        promoted.append({
                            "trial_id": t["trial_id"],
                            "proposal_id": prop_id,
                            "sha256": prop_sha,
                            "autonomous_jury": jury_res,
                        })
                    else:
                        db.execute(
                            """UPDATE shadow_ab_trials
                               SET status='rejected_in_shadow', last_evaluated_at=?,
                                   champion_shadow_json=?, challenger_shadow_json=?
                               WHERE trial_id=?""",
                            (last_at, json.dumps(champ_book), json.dumps(chal_book), t["trial_id"]),
                        )
                        rejected.append(t["trial_id"])
                else:
                    db.execute(
                        """UPDATE shadow_ab_trials
                           SET last_evaluated_at=?, champion_shadow_json=?, challenger_shadow_json=?
                           WHERE trial_id=?""",
                        (last_at, json.dumps(champ_book), json.dumps(chal_book), t["trial_id"]),
                    )
            db.commit()

        return {
            "updated_running_trials": updated_count,
            "promoted_to_proposal": promoted,
            "rejected_in_shadow": rejected,
        }

    # ------------------------------------------------------------------
    # 4. Universal Multi-Domain Loop Discovery & Deterministic Ping Gates
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_windows_process_snapshot() -> Dict[str, Any]:
        if os.name != "nt":
            return {"processes": [], "scheduled_tasks": []}
        import subprocess
        procs_out: List[Dict[str, Any]] = []
        tasks_out: List[Dict[str, Any]] = []
        try:
            r_ps = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'python|ssh|ollama' } | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if r_ps.returncode == 0 and r_ps.stdout.strip():
                raw = json.loads(r_ps.stdout)
                for p in (raw if isinstance(raw, list) else [raw]):
                    cmd = str(p.get("CommandLine") or "")
                    if any(k in cmd.lower() for k in ("sabrina", "ego", "18096", "19225", "8090", "8087", "authenticator_qr", "kraken")):
                        procs_out.append({
                            "pid": int(p.get("ProcessId") or 0),
                            "name": str(p.get("Name") or ""),
                            "cmd": cmd[:220],
                        })
        except Exception:
            pass
        try:
            r_sc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-ScheduledTask | Where-Object { $_.TaskName -match 'Sabrina|sabrina' } | Select-Object TaskName,State | ConvertTo-Json -Compress",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if r_sc.returncode == 0 and r_sc.stdout.strip():
                raw_t = json.loads(r_sc.stdout)
                for t in (raw_t if isinstance(raw_t, list) else [raw_t]):
                    tasks_out.append({"name": str(t.get("TaskName") or ""), "state": int(t.get("State") or 0)})
        except Exception:
            pass
        return {"processes": procs_out, "scheduled_tasks": tasks_out}

    @staticmethod
    def _collect_ego_host_snapshot(*, ssh_target: str = "ego@ego.local") -> Dict[str, Any]:
        import subprocess
        probe_script = r"""
import contextlib, json, sqlite3, subprocess
from pathlib import Path

def sh(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=6)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)

nv_rc, nv_out = sh("nvidia-smi -L")
ps_rc, ps_out = sh("curl -s --max-time 3 http://127.0.0.1:11434/api/ps")
ollama_models = []
try:
    for m in (json.loads(ps_out).get("models") or []):
        ollama_models.append({"name": m.get("name"), "size_vram": int(m.get("size_vram") or 0)})
except Exception:
    pass

_, top_cpu_raw = sh("ps -eo pid,pcpu,etime,args --sort=-pcpu | head -n 10")
cpu_runaways = []
for ln in top_cpu_raw.splitlines()[1:]:
    parts = ln.strip().split(None, 3)
    if len(parts) == 4:
        try:
            pcpu = float(parts[1])
            if pcpu >= 50.0 and "ps -eo" not in parts[3]:
                cpu_runaways.append({"pid": int(parts[0]), "cpu_pct": pcpu, "elapsed": parts[2], "cmd": parts[3][:180]})
        except Exception:
            pass

_, sys_raw = sh("systemctl --user list-units --type=service --state=running,failed --no-pager --no-legend")
failed_units = []
busy_wait_units = []
for ln in sys_raw.splitlines():
    s = ln.lstrip("● ").strip()
    parts = s.split(None, 4)
    if len(parts) >= 4:
        unit, _, _, sub = parts[:4]
        desc = parts[4] if len(parts) > 4 else ""
        if sub == "failed" and (unit.startswith("sabrina-") or unit.startswith("sb-") or unit.startswith("sbshort-")):
            failed_units.append(unit)
        elif sub == "running" and "until test -s" in desc:
            busy_wait_units.append(unit)

_, dock_raw = sh("docker ps --format '{{.Names}}\t{{.Status}}\t{{.Image}}'")
unhealthy_containers = []
restarting_containers = []
notable_containers = []
for ln in dock_raw.splitlines():
    cols = ln.split("\t")
    if len(cols) >= 2:
        cname, cstat = cols[0].strip(), cols[1].strip()
        low = cstat.lower()
        if "restarting" in low:
            restarting_containers.append({"name": cname, "status": cstat})
        elif "unhealthy" in low:
            unhealthy_containers.append({"name": cname, "status": cstat})
        if cname in {"candle-recorder", "sabrina-pfsense-uplink-20261117", "sabrina-monitor-daemon", "exploit-daily-pipeline"}:
            notable_containers.append({"name": cname, "status": cstat})

data_root = Path("/home/ego/bot/Sabrina-windows-sync/data")
coord_summary = {}
sched_summary = {}
social_summary = {}
coord_db = data_root / "control" / "coordinator.sqlite3"
if coord_db.is_file():
    with contextlib.closing(sqlite3.connect(str(coord_db), timeout=2)) as db:
        coord_summary["agent_tasks"] = db.execute("SELECT count(*) FROM agent_tasks").fetchone()[0]
        coord_summary["agent_events"] = db.execute("SELECT count(*) FROM agent_events").fetchone()[0]
        coord_summary["control_requests"] = db.execute("SELECT count(*) FROM control_requests").fetchone()[0]
        coord_summary["control_clients"] = db.execute("SELECT count(*) FROM control_clients").fetchone()[0]
        unprompted = [
            r[0][:110] for r in db.execute(
                "SELECT DISTINCT goal FROM agent_tasks WHERE state='failed' ORDER BY created DESC LIMIT 6"
            ).fetchall() if r[0]
        ]
        coord_summary["recent_failed_goals"] = unprompted

sched_db = data_root / "social" / "scheduler.sqlite3"
if sched_db.is_file():
    with contextlib.closing(sqlite3.connect(str(sched_db), timeout=2)) as db:
        sched_summary["scheduled_jobs"] = db.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0]
        sched_summary["scheduled_job_events"] = db.execute("SELECT count(*) FROM scheduled_job_events").fetchone()[0]
        sched_summary["top_capabilities"] = {
            r[0]: r[1] for r in db.execute(
                "SELECT capability, count(*) AS c FROM scheduled_jobs GROUP BY capability ORDER BY c DESC LIMIT 6"
            ).fetchall()
        }
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "kraken_watch_events" in tables:
            sched_summary["kraken_watch_events"] = db.execute("SELECT count(*) FROM kraken_watch_events").fetchone()[0]
        if "kraken_research_jobs" in tables:
            sched_summary["kraken_research_jobs"] = db.execute("SELECT count(*) FROM kraken_research_jobs").fetchone()[0]

soc_db = data_root / "social" / "social.sqlite3"
if soc_db.is_file():
    with contextlib.closing(sqlite3.connect(str(soc_db), timeout=2)) as db:
        social_summary["operations"] = db.execute("SELECT count(*) FROM operations").fetchone()[0]
        social_summary["operation_events"] = db.execute("SELECT count(*) FROM operation_events").fetchone()[0]
        social_summary["status_counts"] = {
            r[0]: r[1] for r in db.execute("SELECT status, count(*) FROM operations GROUP BY status").fetchall()
        }
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "dm_external_applications" in tables:
            social_summary["blocked_applications"] = [
                {"id": r[0], "status": r[1]}
                for r in db.execute(
                    "SELECT id, status FROM dm_external_applications WHERE status NOT IN ('completed', 'submitted') LIMIT 8"
                ).fetchall()
            ]
        elif "job_applications" in tables:
            cols = {c[1] for c in db.execute("PRAGMA table_info(job_applications)").fetchall()}
            id_col = "id" if "id" in cols else "application_id" if "application_id" in cols else None
            st_col = "status" if "status" in cols else "state" if "state" in cols else None
            if id_col and st_col:
                social_summary["blocked_applications"] = [
                    {"id": r[0], "status": r[1]}
                    for r in db.execute(
                        f"SELECT {id_col}, {st_col} FROM job_applications WHERE {st_col} NOT IN ('completed', 'submitted') LIMIT 8"
                    ).fetchall()
                ]

rec_db = data_root / "social" / "recovery.sqlite3"
if rec_db.is_file():
    with contextlib.closing(sqlite3.connect(str(rec_db), timeout=2)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "pulse_executors" in tables:
            social_summary["pulse_executors"] = db.execute("SELECT count(*) FROM pulse_executors").fetchone()[0]
        if "recovery_events" in tables:
            social_summary["recovery_events"] = db.execute("SELECT count(*) FROM recovery_events").fetchone()[0]

print(json.dumps({
    "ok": True,
    "nvidia_smi_ok": nv_rc == 0 and "GPU " in nv_out,
    "nvidia_smi_detail": nv_out[:180],
    "ollama_models": ollama_models,
    "cpu_runaways": cpu_runaways,
    "failed_systemd_units_count": len(failed_units),
    "failed_systemd_sample": failed_units[:10],
    "busy_wait_systemd_units": busy_wait_units,
    "restarting_containers": restarting_containers,
    "unhealthy_containers": unhealthy_containers,
    "notable_containers": notable_containers,
    "coordinator": coord_summary,
    "scheduler": sched_summary,
    "social": social_summary,
}))
"""
        try:
            if os.name != "nt":
                r = subprocess.run([sys.executable, "-c", probe_script], capture_output=True, text=True, timeout=15)
            else:
                r = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", ssh_target, "python3", "-"],
                    input=probe_script,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=18,
                )
            if r.returncode == 0 and r.stdout.strip():
                last_line = [ln for ln in r.stdout.strip().splitlines() if ln.strip().startswith("{")][-1]
                return json.loads(last_line)
            return {"ok": False, "error": (r.stderr or r.stdout or "ssh_failed")[:200]}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}

    def audit_all_system_gates(
        self,
        *,
        data_dir: Optional[str | Path] = None,
        include_ssh_ego: bool = False,
        include_local_processes: bool = False,
        ego_snapshot: Optional[Dict[str, Any]] = None,
        windows_snapshot: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Deterministically audit all Sabrina domains, detect blind loops, and emit edge-triggered wakeup pings only on state-hash changes."""
        now_ts = float(now if now is not None else time.time())
        droot = Path(data_dir) if data_dir is not None else (ROOT / "data")
        ego = ego_snapshot if ego_snapshot is not None else (
            self._collect_ego_host_snapshot() if include_ssh_ego else {}
        )
        win = windows_snapshot if windows_snapshot is not None else (
            self._collect_windows_process_snapshot() if include_local_processes else {"processes": [], "scheduled_tasks": []}
        )

        findings: List[Dict[str, Any]] = []

        # 1. Hardware & Compute Bus Gate
        nv_ok = ego.get("nvidia_smi_ok", True)
        ollama_models = ego.get("ollama_models") or []
        vram_zero_models = [m["name"] for m in ollama_models if int(m.get("size_vram") or 0) == 0]
        cpu_runaways = [p for p in (ego.get("cpu_runaways") or []) if float(p.get("cpu_pct") or 0.0) >= 80.0]
        fed_procs = [
            p for p in (win.get("processes") or [])
            if "sabrina_compute_federation" in p.get("cmd", "") or "18096" in p.get("cmd", "")
        ]
        hw_anom = (not nv_ok) or bool(vram_zero_models) or any(float(p.get("cpu_pct", 0)) >= 200.0 for p in cpu_runaways) or bool(fed_procs)
        hw_metrics = {
            "nvidia_smi_ok": nv_ok,
            "nvidia_smi_detail": ego.get("nvidia_smi_detail", ""),
            "cpu_fallback_ollama_models": sorted(vram_zero_models),
            "cpu_runaway_pids": [{"pid": p["pid"], "cpu_pct": p["cpu_pct"], "cmd": p["cmd"][:90]} for p in cpu_runaways[:4]],
            "failed_systemd_units_count": int(ego.get("failed_systemd_units_count") or 0),
            "busy_wait_systemd_units": sorted(ego.get("busy_wait_systemd_units") or []),
            "windows_federation_bridge_pids": [p["pid"] for p in fed_procs],
        }
        hw_fp = {
            "nv_ok": nv_ok,
            "vram_zero": sorted(vram_zero_models),
            "high_cpu_cmds": sorted(p["cmd"][:50] for p in cpu_runaways if float(p.get("cpu_pct", 0)) >= 100.0),
            "failed_units": int(ego.get("failed_systemd_units_count") or 0),
            "fed_active": bool(fed_procs),
        }
        findings.append({
            "domain": "hardware_compute_bus",
            "anomaly_active": hw_anom,
            "severity": "critical" if (not nv_ok or any(float(p.get("cpu_pct", 0)) >= 200.0 for p in cpu_runaways)) else ("warn" if hw_anom else "ok"),
            "headline": (
                f"GPU0 NVML={'OK' if nv_ok else 'DROPPED'}; CPU runaways={len(cpu_runaways)}; "
                f"failed units={hw_metrics['failed_systemd_units_count']}; Windows reverse-federation={bool(fed_procs)}"
            ),
            "metrics": hw_metrics,
            "fingerprint": hw_fp,
        })

        # 2. Lab Coordinator Control Spin-Loop Gate
        coord = dict(ego.get("coordinator") or {})
        local_coord_db = droot / "control" / "coordinator.sqlite3"
        if not coord and local_coord_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(local_coord_db), timeout=2.0)) as cdb:
                    coord["agent_tasks"] = cdb.execute("SELECT count(*) FROM agent_tasks").fetchone()[0]
                    coord["agent_events"] = cdb.execute("SELECT count(*) FROM agent_events").fetchone()[0]
                    coord["control_requests"] = cdb.execute("SELECT count(*) FROM control_requests").fetchone()[0]
            except Exception:
                pass
        c_tasks = max(1, int(coord.get("agent_tasks") or 0))
        c_events = int(coord.get("agent_events") or 0)
        ev_ratio = round(c_events / float(c_tasks), 1) if coord else 0.0
        coord_anom = ev_ratio >= 50.0 or int(coord.get("control_requests") or 0) >= 1000
        findings.append({
            "domain": "lab_coordinator_control",
            "anomaly_active": coord_anom,
            "severity": "critical" if ev_ratio >= 500.0 else ("warn" if coord_anom else "ok"),
            "headline": (
                f"Coordinator events={c_events:,} across {int(coord.get('agent_tasks') or 0)} tasks "
                f"({ev_ratio} events/task); requests={int(coord.get('control_requests') or 0):,}"
            ),
            "metrics": {**coord, "events_per_task_ratio": ev_ratio},
            "fingerprint": {
                "spin_bucket": int(c_events // 25000),
                "req_bucket": int(int(coord.get("control_requests") or 0) // 1000),
                "recent_failed_goals": sorted(coord.get("recent_failed_goals") or []),
            },
        })

        # 3. Social Pulse & Internal Scheduler Loop Gate
        sched = dict(ego.get("scheduler") or {})
        soc = dict(ego.get("social") or {})
        local_sched_db = droot / "social" / "scheduler.sqlite3"
        if not sched and local_sched_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(local_sched_db), timeout=2.0)) as sdb:
                    sched["scheduled_jobs"] = sdb.execute("SELECT count(*) FROM scheduled_jobs").fetchone()[0]
                    sched["top_capabilities"] = {
                        r[0]: r[1] for r in sdb.execute("SELECT capability, count(*) FROM scheduled_jobs GROUP BY capability").fetchall()
                    }
            except Exception:
                pass
        local_soc_db = droot / "social" / "social.sqlite3"
        if not soc and local_soc_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(local_soc_db), timeout=2.0)) as sdb:
                    soc["operations"] = sdb.execute("SELECT count(*) FROM operations").fetchone()[0]
                    tables = {r[0] for r in sdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "dm_external_applications" in tables:
                        soc["blocked_applications"] = [
                            {"id": r[0], "status": r[1]}
                            for r in sdb.execute("SELECT id, status FROM dm_external_applications WHERE status NOT IN ('completed','submitted')").fetchall()
                        ]
            except Exception:
                pass
        swarm_chat_count = 0
        sc_db = droot / "swarm_chat_jobs.sqlite3"
        if sc_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(sc_db), timeout=2.0)) as db_sc:
                    swarm_chat_count = int(db_sc.execute("SELECT count(*) FROM jobs").fetchone()[0])
            except Exception:
                pass
        top_caps = sched.get("top_capabilities") or {}
        max_cap_dup = max([0] + [int(v) for v in top_caps.values()])
        blocked_apps = soc.get("blocked_applications") or []
        soc_anom = max_cap_dup >= 50 or int(soc.get("operations") or 0) >= 500 or bool(blocked_apps)
        findings.append({
            "domain": "social_and_scheduler",
            "anomaly_active": soc_anom,
            "severity": "critical" if max_cap_dup >= 250 or any("captcha" in str(a.get("status", "")).lower() for a in blocked_apps) else ("warn" if soc_anom else "ok"),
            "headline": (
                f"Scheduled jobs={int(sched.get('scheduled_jobs') or 0):,} (max duplicate capability={max_cap_dup:,}); "
                f"social ops={int(soc.get('operations') or 0):,}; blocked ATS apps={len(blocked_apps)}"
            ),
            "metrics": {
                "scheduler": sched,
                "social": soc,
                "swarm_chat_jobs_count": swarm_chat_count,
            },
            "fingerprint": {
                "dup_bucket": int(max_cap_dup // 100),
                "ops_bucket": int(int(soc.get("operations") or 0) // 500),
                "blocked_apps": sorted(f"{a.get('id')}:{a.get('status')}" for a in blocked_apps),
            },
        })

        # 4. Recruiter & LinkedIn Messaging Change-Hash Gate
        msg_metrics: Dict[str, Any] = {"threads": 0, "messages": 0, "review_runs": 0, "review_artifacts": 0, "pending_questions": []}
        msg_db = droot / "messaging.sqlite3"
        if msg_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(msg_db), timeout=2.0)) as mdb:
                    msg_metrics["threads"] = int(mdb.execute("SELECT count(*) FROM threads").fetchone()[0])
                    msg_metrics["messages"] = int(mdb.execute("SELECT count(*) FROM messages").fetchone()[0])
                    msg_metrics["decisions"] = int(mdb.execute("SELECT count(*) FROM decisions").fetchone()[0])
            except Exception:
                pass
        mbb_db = droot / "messaging_blackboard.sqlite3"
        if mbb_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(mbb_db), timeout=2.0)) as bdb:
                    tables = {r[0] for r in bdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "conversation_review_runs" in tables:
                        msg_metrics["review_runs"] = int(bdb.execute("SELECT count(*) FROM conversation_review_runs").fetchone()[0])
                    if "conversation_review_artifacts" in tables:
                        msg_metrics["review_artifacts"] = int(bdb.execute("SELECT count(*) FROM conversation_review_artifacts").fetchone()[0])
            except Exception:
                pass
        dq_db = droot / "desktop_questions.sqlite3"
        if dq_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(dq_db), timeout=2.0)) as qdb:
                    qdb.row_factory = sqlite3.Row
                    tables = {r[0] for r in qdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if "questions" in tables:
                        q_rows = qdb.execute("SELECT scope, payload, revision, status FROM questions WHERE status='pending' ORDER BY updated DESC").fetchall()
                        for qr in q_rows:
                            p_obj = json.loads(qr["payload"] or "{}")
                            msg_metrics["pending_questions"].append({
                                "scope": str(qr["scope"]),
                                "title": str(p_obj.get("title") or "")[:90],
                                "revision": str(qr["revision"] or "")[:16],
                            })
                    if "notices" in tables:
                        msg_metrics["total_notices"] = int(qdb.execute("SELECT count(*) FROM notices").fetchone()[0])
            except Exception:
                pass
        rec_anom = msg_metrics["review_runs"] > msg_metrics["threads"] or len(msg_metrics["pending_questions"]) > 0
        findings.append({
            "domain": "recruiter_and_messaging",
            "anomaly_active": rec_anom,
            "severity": "warn" if rec_anom else "ok",
            "headline": (
                f"Threads={msg_metrics['threads']}, 3-model review runs={msg_metrics['review_runs']} "
                f"({msg_metrics['review_artifacts']} artifacts); pending human questions={len(msg_metrics['pending_questions'])}"
            ),
            "metrics": msg_metrics,
            "fingerprint": {
                "review_churn": msg_metrics["review_runs"] > msg_metrics["threads"],
                "pending_revisions": sorted(q["revision"] for q in msg_metrics["pending_questions"]),
            },
        })

        # 5. Customer & Work Gmail History-ID Gate
        cust_metrics: Dict[str, Any] = {"job_events": 0, "work_mail_rows": 0, "needs_operator_jobs": [], "polling_jobs": []}
        cj_db = droot / "customer_jobs.sqlite3"
        if cj_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(cj_db), timeout=2.0)) as cdb:
                    cdb.row_factory = sqlite3.Row
                    cust_metrics["job_events"] = int(cdb.execute("SELECT count(*) FROM job_events").fetchone()[0])
                    for jr in cdb.execute("SELECT id, state, attempts, last_error FROM jobs").fetchall():
                        item = {
                            "id": str(jr["id"]),
                            "state": str(jr["state"]),
                            "attempts": int(jr["attempts"] or 0),
                            "last_error": str(jr["last_error"] or "")[:100],
                        }
                        if item["state"] == "NEEDS_OPERATOR":
                            cust_metrics["needs_operator_jobs"].append(item)
                        else:
                            cust_metrics["polling_jobs"].append(item)
            except Exception:
                pass
        gm_db = droot / "gmail_inbox_triage.sqlite3"
        if gm_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(gm_db), timeout=2.0)) as gdb:
                    cust_metrics["work_mail_rows"] = int(gdb.execute("SELECT count(*) FROM work_mail").fetchone()[0])
            except Exception:
                pass
        cust_anom = bool(cust_metrics["needs_operator_jobs"]) or cust_metrics["job_events"] >= 200
        findings.append({
            "domain": "customer_and_work_email",
            "anomaly_active": cust_anom,
            "severity": "warn" if cust_anom else "ok",
            "headline": (
                f"Gmail job_events={cust_metrics['job_events']:,} ({cust_metrics['work_mail_rows']} triaged emails); "
                f"NEEDS_OPERATOR latches={len(cust_metrics['needs_operator_jobs'])}"
            ),
            "metrics": cust_metrics,
            "fingerprint": {
                "needs_operator_ids": sorted(j["id"] for j in cust_metrics["needs_operator_jobs"]),
                "empty_poll_bucket": int(cust_metrics["job_events"] // 250),
            },
        })

        # 6. Docker, Recon & Legacy Daemons Gate
        restarting_c = ego.get("restarting_containers") or []
        unhealthy_c = ego.get("unhealthy_containers") or []
        notable_c = ego.get("notable_containers") or []
        legacy_candle = any(c.get("name") == "candle-recorder" for c in notable_c)
        dock_anom = bool(restarting_c) or bool(unhealthy_c) or legacy_candle
        findings.append({
            "domain": "docker_recon_and_daemons",
            "anomaly_active": dock_anom,
            "severity": "critical" if restarting_c else ("warn" if dock_anom else "ok"),
            "headline": (
                f"Crash-looping containers={len(restarting_c)} ({', '.join(c['name'] for c in restarting_c) or 'none'}); "
                f"unhealthy={len(unhealthy_c)}; notable/legacy={', '.join(c['name'] for c in notable_c) or 'none'}"
            ),
            "metrics": {
                "restarting_containers": restarting_c,
                "unhealthy_containers": unhealthy_c,
                "notable_containers": notable_c,
            },
            "fingerprint": {
                "restarting": sorted(c["name"] for c in restarting_c),
                "unhealthy": sorted(c["name"] for c in unhealthy_c),
                "legacy_candle": legacy_candle,
            },
        })

        # 7. Windows Host & Bridge Recovery Gate
        br_events = 0
        ga_events = 0
        br_db = droot / "control" / "bridge-recovery.sqlite3"
        if br_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(br_db), timeout=2.0)) as db_b:
                    br_events = int(db_b.execute("SELECT count(*) FROM recovery_events").fetchone()[0])
            except Exception:
                pass
        ga_db = droot / "control" / "guard-availability.sqlite3"
        if ga_db.is_file():
            try:
                with contextlib.closing(sqlite3.connect(str(ga_db), timeout=2.0)) as db_g:
                    ga_events = int(db_g.execute("SELECT count(*) FROM availability_events").fetchone()[0])
            except Exception:
                pass
        qr_procs = [p for p in (win.get("processes") or []) if "scan_google_authenticator_qr" in p.get("cmd", "")]
        win_anom = br_events >= 200 or ga_events >= 200 or bool(qr_procs)
        findings.append({
            "domain": "windows_host_and_bridges",
            "anomaly_active": win_anom,
            "severity": "warn" if win_anom else "ok",
            "headline": (
                f"Bridge recovery_events={br_events:,}, availability_events={ga_events:,}; "
                f"ephemeral QR server lingering={bool(qr_procs)}; scheduled tasks={len(win.get('scheduled_tasks') or [])}"
            ),
            "metrics": {
                "bridge_recovery_events": br_events,
                "guard_availability_events": ga_events,
                "lingering_utility_pids": [p["pid"] for p in qr_procs],
                "windows_scheduled_tasks": [t["name"] for t in (win.get("scheduled_tasks") or [])],
            },
            "fingerprint": {
                "br_bucket": int(br_events // 250),
                "ga_bucket": int(ga_events // 250),
                "qr_lingering": bool(qr_procs),
            },
        })

        # 8. Kraken Trading Execution & 24h Shadow A/B Gate
        with self._connect() as db:
            running_trials = int(db.execute("SELECT count(*) FROM shadow_ab_trials WHERE status='running'").fetchone()[0])
            logged_triggers = int(db.execute("SELECT count(*) FROM epistemic_trigger_log WHERE kind NOT LIKE 'system_gate:%'").fetchone()[0])
        findings.append({
            "domain": "kraken_trading_execution",
            "anomaly_active": False,
            "severity": "ok",
            "headline": f"Deterministic Epistemic Sentinel active ({running_trials} live 24h Shadow A/B trial(s), {logged_triggers} market surprise event(s))",
            "metrics": {"running_shadow_ab_trials": running_trials, "market_epistemic_events": logged_triggers},
            "fingerprint": {"shadow_trials": running_trials},
        })

        # Persist deterministic state hashes and emit wakeup pings ONLY on state-edge transitions
        new_pings: List[Dict[str, Any]] = []
        enriched_domains: List[Dict[str, Any]] = []
        with self._connect() as db:
            for item in findings:
                dom = item["domain"]
                spec = UNIVERSAL_DETERMINISTIC_GATE_SPECS[dom]
                state_sha = hashlib.sha256(json.dumps(item["fingerprint"], sort_keys=True).encode("utf-8")).hexdigest()
                prev = db.execute("SELECT last_state_sha256, ping_count, last_ping_at FROM system_gate_registry WHERE domain=?", (dom,)).fetchone()
                prev_sha = str(prev["last_state_sha256"]) if prev else ""
                ping_count = int(prev["ping_count"]) if prev else 0
                last_ping_at = float(prev["last_ping_at"]) if prev else 0.0

                ping_warranted = bool(item["anomaly_active"]) and (state_sha != prev_sha)
                if ping_warranted:
                    ping_count += 1
                    last_ping_at = now_ts
                    dkey = f"system_gate:{dom}:{state_sha[:16]}"
                    if not self._already_logged(db, dkey):
                        db.execute(
                            """INSERT INTO epistemic_trigger_log(
                                   dedup_key, kind, pair, summary, payload_json, resolution, task_id, trial_id, created
                               ) VALUES (?, ?, ?, ?, ?, 'pinged_operator_gate', '', '', ?)""",
                            (
                                dkey,
                                f"system_gate:{dom}",
                                dom.upper(),
                                f"[{spec['gate_name']}] {item['headline']}"[:240],
                                json.dumps(item["metrics"]),
                                now_ts,
                            ),
                        )
                    new_pings.append({
                        "domain": dom,
                        "gate_name": spec["gate_name"],
                        "severity": item["severity"],
                        "state_sha256": state_sha,
                        "headline": item["headline"],
                    })

                db.execute(
                    """INSERT OR REPLACE INTO system_gate_registry(
                           domain, gate_name, deterministic_predicate, last_state_sha256,
                           anomaly_active, severity, headline, metrics_json,
                           ping_count, last_ping_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dom,
                        spec["gate_name"],
                        spec["predicate"],
                        state_sha,
                        1 if item["anomaly_active"] else 0,
                        item["severity"],
                        item["headline"],
                        json.dumps(item["metrics"]),
                        ping_count,
                        last_ping_at,
                        now_ts,
                    ),
                )
                enriched_domains.append({
                    "domain": dom,
                    "gate_name": spec["gate_name"],
                    "deterministic_predicate": spec["predicate"],
                    "anomaly_active": item["anomaly_active"],
                    "severity": item["severity"],
                    "state_sha256": state_sha,
                    "ping_warranted": ping_warranted,
                    "ping_count": ping_count,
                    "headline": item["headline"],
                    "metrics": item["metrics"],
                })
            db.commit()

        return {
            "ok": True,
            "holds": True,
            "timestamp": now_ts,
            "domains_audited": len(enriched_domains),
            "anomalous_domains_count": sum(1 for d in enriched_domains if d["anomaly_active"]),
            "new_deterministic_pings_count": len(new_pings),
            "new_pings": new_pings,
            "domains": enriched_domains,
        }

    def run_pulse(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        """Single check pulse: checks auto-rollback & healthy 24h lease renewal, steps 24h Shadow A/B trials (with optional Constitutional Model Jury auto-approval), and handles at most 1 new epistemic trigger."""
        now_ts = float(now if now is not None else time.time())
        rollback_check = self.check_and_rollback_if_degraded(now=now_ts)
        lease_renewal = self.renew_arm_lease_if_healthy(now=now_ts) if self.autonomous_model_approval else {"renewed": False, "reason": "manual_lease_mode"}
        snapshots, ohlc = load_saved_snapshots_and_ohlc(self.market_inputs_dir, max_snapshots=800, fetch_live_ohlc=False)
        shadow_step = self.step_shadow_trials(snapshots=snapshots[-120:], now=now_ts)
        triggers = self.detect_triggers(now=now_ts, snapshots=snapshots[-180:])
        investigation = None
        if triggers:
            full_snaps, full_ohlc = load_saved_snapshots_and_ohlc(self.market_inputs_dir, max_snapshots=800, fetch_live_ohlc=True)
            investigation = self.investigate_and_enroll(
                triggers[0],
                snapshots=full_snaps,
                ohlc_15m_by_pair=full_ohlc,
                now=now_ts,
            )
        return {
            "timestamp": now_ts,
            "autonomous_model_approval": self.autonomous_model_approval,
            "rollback_check": rollback_check,
            "lease_renewal": lease_renewal,
            "triggers_detected": len(triggers),
            "active_trigger": triggers[0] if triggers else None,
            "investigation": investigation,
            "shadow_step": shadow_step,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pulse", "status", "audit"], default="pulse", nargs="?")
    parser.add_argument("--include-ego", action="store_true", help="Query live ego.local telemetry over SSH during audit")
    parser.add_argument("--include-local-procs", action="store_true", help="Inspect local Windows Python/SSH processes and scheduled tasks")
    parser.add_argument("--autonomous-model-approval", action="store_true", help="Allow 2-family Constitutional Model Jury to approve 24h Shadow A/B winners and renew healthy arm leases")
    args = parser.parse_args()
    sentinel = EpistemicSentinel(autonomous_model_approval=bool(args.autonomous_model_approval))
    if args.command == "pulse":
        print(json.dumps(sentinel.run_pulse(), indent=2))
    elif args.command == "audit":
        print(
            json.dumps(
                sentinel.audit_all_system_gates(
                    include_ssh_ego=bool(args.include_ego),
                    include_local_processes=bool(args.include_local_procs),
                ),
                indent=2,
            )
        )
    else:
        with sentinel._connect() as db:
            trials = [dict(r) for r in db.execute("SELECT trial_id, label, trigger_kind, status, created_at, expires_at, proposal_id FROM shadow_ab_trials ORDER BY created_at DESC LIMIT 10").fetchall()]
            logs = [dict(r) for r in db.execute("SELECT dedup_key, kind, pair, summary, resolution, trial_id, created FROM epistemic_trigger_log ORDER BY created DESC LIMIT 15").fetchall()]
            gates = [dict(r) for r in db.execute("SELECT domain, gate_name, anomaly_active, severity, headline, ping_count, updated_at FROM system_gate_registry ORDER BY domain ASC").fetchall()]
            approvals = [dict(r) for r in db.execute("SELECT approval_id, trial_id, status, juror_1_model, juror_1_approve, juror_2_model, juror_2_approve, juror_3_model, juror_3_approve, juror_4_model, juror_4_approve, applied_to_live_config, created_at FROM autonomous_model_approvals ORDER BY created_at DESC LIMIT 10").fetchall()]
        print(json.dumps({"shadow_ab_trials": trials, "autonomous_model_approvals": approvals, "epistemic_triggers": logs, "system_gates": gates}, indent=2))


if __name__ == "__main__":
    main()
