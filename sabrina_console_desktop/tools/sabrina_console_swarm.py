"""Swarm Command Center view and live control engine for Sabrina Operator Console & Web Dashboard.

Provides:
1. `get_live_swarm_state()`: Aggregates real-time state across Sabrina's local model sessions,
   cross-app swarm handoffs (`Claude.exe`, `Codex.exe`, `Antigravity.exe`), capability pipelines,
   hardware cursor lease (`primary_desktop`), pending operator approvals, and a plain-English
   chronological activity feed explaining exactly what is happening.
2. Direct Swarm control actions (`launch_swarm_mission`, `swap_swarm_pipeline_roles`,
   `force_release_desktop_lease`, `decide_pending_item`) that immediately effect the Swarm.
3. `SwarmCommandView`: Native Tkinter tab for `sabrina_operator_console.py`.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    _SwarmBaseFrame = ttk.Frame
except ImportError:
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _SwarmBaseFrame = object  # type: ignore[assignment,misc]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.console_remote import call_console_backend
from tools.capability_registry import CapabilityRegistry
from tools.sabrina_skill_hydration import SabrinaSkillHydration
from tools.sabrina_workflow import DEFAULT_DB, WorkflowStore


def _assistant_db_path() -> Path:
    fresh = ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
    fallback = fresh if fresh.exists() else DEFAULT_DB
    return Path(os.environ.get("SABRINA_ASSISTANT_DB", str(fallback)))


def _capabilities_db_path() -> Path:
    return ROOT / "data" / "control" / "capabilities.sqlite3"


def _agent_runtime_db_path() -> Path:
    return Path(os.environ.get("SABRINA_AGENT_RUNTIME_DB", str(ROOT / "data" / "agent_runtime.sqlite3")))


def _dnd_blackboard_db_path() -> Path:
    return Path(os.environ.get("SABRINA_DND_BLACKBOARD_DB", str(ROOT / "data" / "dnd_rsi_blackboard.sqlite3")))


def _fmt_time(ts: float | int | None) -> str:
    if not ts:
        return "—"
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%I:%M:%S %p")
    except Exception:
        return "—"


def _fmt_ago(ts: float | int | None, now: float | None = None) -> str:
    if not ts:
        return ""
    now = now or time.time()
    diff = max(0, int(now - float(ts)))
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def get_live_swarm_state() -> dict[str, Any]:
    """Build a unified, human-readable snapshot of what the Swarm is doing right now."""
    now = time.time()
    db_path = _assistant_db_path()
    cap_path = _capabilities_db_path()

    nodes: dict[str, dict[str, Any]] = {
        "sabrina": {
            "name": "Sabrina Controller",
            "role": "Orchestrator & Local Reasoning (Ollama)",
            "status": "IDLE",
            "detail": "No active reasoning turn running.",
            "run_id": "",
            "updated": 0.0,
        },
        "codex": {
            "name": "Codex.exe (Astra)",
            "role": "Thought / Architecture & Code Implementation",
            "status": "IDLE",
            "detail": "Ready for capability handoff.",
            "conversation": "",
            "updated": 0.0,
        },
        "antigravity": {
            "name": "Antigravity.exe",
            "role": "Implementation, GenUI & Pair Programming",
            "status": "IDLE",
            "detail": "Connected via MCP (sabrina-assistant & agent-desktop).",
            "conversation": "",
            "updated": 0.0,
        },
        "claude": {
            "name": "Claude.exe",
            "role": "Cross-App Critique & Spec Review",
            "status": "IDLE",
            "detail": "No active desktop handoff.",
            "conversation": "",
            "updated": 0.0,
        },
        "desktop": {
            "name": "Desktop & Cursor Agent",
            "role": "DPI-Gated Screen & Pointer Broker (1.2.0)",
            "status": "READ-ONLY OBSERVE",
            "detail": "Lease unlocked · Physical cursor gated off by default.",
            "lease_owner": None,
            "lease_expires_in": 0,
            "physical_input": False,
        },
    }

    feed: list[dict[str, Any]] = []
    pending_actions: list[dict[str, Any]] = []
    objectives_summary: list[dict[str, Any]] = []
    capability_pipelines: list[dict[str, Any]] = []

    # 1. Read CapabilityRegistry handoffs (Codex <-> Antigravity pipelines)
    try:
        reg = CapabilityRegistry(cap_path)
        handoffs = reg.list_handoffs(limit=20)
        for h in handoffs:
            payload = h.get("payload") or {}
            cap_name = str(h.get("capability") or "")
            state = str(h.get("state") or "queued").upper()
            app = str(payload.get("assigned_app") or "")
            stage = str(payload.get("stage") or "task")
            created = float(h.get("created") or 0)
            updated = float(h.get("updated") or created)
            capability_pipelines.append(
                {
                    "id": h.get("id"),
                    "pipeline_id": payload.get("pipeline_id", ""),
                    "stage": stage,
                    "capability": cap_name,
                    "app": app,
                    "state": state,
                    "objective": h.get("objective", ""),
                    "updated": updated,
                    "updated_str": _fmt_time(updated),
                }
            )
            feed.append(
                {
                    "ts": updated,
                    "time": _fmt_time(updated),
                    "ago": _fmt_ago(updated, now),
                    "badge": "PIPELINE",
                    "actor": app or cap_name,
                    "summary": f"[{state}] Stage '{stage.upper()}' assigned to {app or cap_name}: {str(h.get('objective') or '')[:110]}",
                }
            )
            target_key = "codex" if "codex" in (app + cap_name).lower() else "antigravity" if "antigravity" in (app + cap_name).lower() else "claude" if "claude" in (app + cap_name).lower() else None
            if target_key and updated >= nodes[target_key]["updated"]:
                nodes[target_key]["updated"] = updated
                nodes[target_key]["status"] = "QUEUED STAGE" if state == "QUEUED" else state
                nodes[target_key]["detail"] = f"{cap_name} ({stage}): {str(h.get('objective') or '')[:80]}"
    except Exception:
        pass

    # 2. Read Sabrina Assistant SQLite tables
    if db_path.exists():
        try:
            with contextlib.closing(sqlite3.connect(str(db_path), timeout=5)) as db:
                db.row_factory = sqlite3.Row
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

                # Desktop lease status
                if "assistant_desktop_lease" in tables:
                    lease = db.execute(
                        "SELECT owner_run_id, acquired_at, expires_at, purpose FROM assistant_desktop_lease WHERE lease_key='primary_desktop'"
                    ).fetchone()
                    if lease and float(lease["expires_at"] or 0) > now:
                        rem = int(float(lease["expires_at"]) - now)
                        nodes["desktop"]["lease_owner"] = lease["owner_run_id"]
                        nodes["desktop"]["lease_expires_in"] = rem
                        nodes["desktop"]["status"] = "LEASE ACTIVE"
                        nodes["desktop"]["detail"] = (
                            f"Held by run {str(lease['owner_run_id'])[:12]} for {lease['purpose'] or 'desktop op'} ({rem}s left)"
                        )

                # Latest sessions
                if "assistant_sessions" in tables:
                    sessions = db.execute(
                        "SELECT id, task, state, model, activity, progress, updated FROM assistant_sessions ORDER BY updated DESC LIMIT 8"
                    ).fetchall()
                    if sessions:
                        latest = sessions[0]
                        s_state = str(latest["state"] or "unknown").upper()
                        nodes["sabrina"]["run_id"] = str(latest["id"])
                        nodes["sabrina"]["updated"] = float(latest["updated"] or 0)
                        nodes["sabrina"]["status"] = s_state
                        act = str(latest["activity"] or latest["progress"] or latest["task"] or "")
                        nodes["sabrina"]["detail"] = f"[{latest['model'] or 'local-llm'}] {act[:95]}"

                        # Check desktop runtime permissions for the latest session
                        if "assistant_desktop_runtime" in tables:
                            rt = db.execute(
                                "SELECT permissions, scope FROM assistant_desktop_runtime WHERE run_id=?",
                                (latest["id"],),
                            ).fetchone()
                            if rt:
                                perms = json.loads(rt["permissions"] or "{}")
                                scope = json.loads(rt["scope"] or "{}")
                                phys = bool(perms.get("physical_input"))
                                nodes["desktop"]["physical_input"] = phys
                                mode_str = "PHYSICAL CURSOR ENABLED" if phys else "READ-ONLY OBSERVE"
                                if not nodes["desktop"]["lease_owner"]:
                                    nodes["desktop"]["status"] = mode_str
                                apps_str = ", ".join(scope.get("apps") or []) or "none"
                                nodes["desktop"]["detail"] = (
                                    f"Access: {scope.get('access', 'observe')} | Apps: {apps_str} | Physical Input: {'ON' if phys else 'OFF'}"
                                )

                    for s in sessions[:5]:
                        ts = float(s["updated"] or 0)
                        feed.append(
                            {
                                "ts": ts,
                                "time": _fmt_time(ts),
                                "ago": _fmt_ago(ts, now),
                                "badge": "SABRINA",
                                "actor": s["model"] or "Sabrina",
                                "summary": f"Run {str(s['id'])[:10]} [{str(s['state']).upper()}]: {str(s['activity'] or s['task'] or '')[:110]}",
                            }
                        )

                # Cross-app handoffs (Claude.exe, Antigravity.exe, Codex.exe)
                if "assistant_cross_app_handoffs" in tables:
                    ca_rows = db.execute(
                        """SELECT handoff_id, run_id, app, status, conversation_key, prompt,
                                  summary, error_detail, started, completed, desktop_call_count
                           FROM assistant_cross_app_handoffs
                           ORDER BY started DESC LIMIT 8"""
                    ).fetchall()
                    for ca in ca_rows:
                        app_name = str(ca["app"] or "")
                        st = str(ca["status"] or "").upper()
                        ts = float(ca["completed"] or ca["started"] or 0)
                        conv = str(ca["conversation_key"] or "window")
                        err = str(ca["error_detail"] or "")
                        calls = int(ca["desktop_call_count"] or 0)
                        desc = f"Handoff to {app_name} ({conv}) [{st}] · {calls} desktop calls"
                        if err:
                            desc += f" · {err[:80]}"
                        elif ca["summary"]:
                            desc += f" · {str(ca['summary'])[:80]}"
                        feed.append(
                            {
                                "ts": ts,
                                "time": _fmt_time(ts),
                                "ago": _fmt_ago(ts, now),
                                "badge": "SWARM APP",
                                "actor": app_name,
                                "summary": desc,
                            }
                        )
                        app_key = (
                            "claude"
                            if "claude" in app_name.lower()
                            else "antigravity"
                            if "antigravity" in app_name.lower()
                            else "codex"
                            if "codex" in app_name.lower()
                            else None
                        )
                        if app_key and ts >= nodes[app_key]["updated"]:
                            nodes[app_key]["updated"] = ts
                            nodes[app_key]["status"] = f"HANDOFF: {st}"
                            nodes[app_key]["conversation"] = conv
                            nodes[app_key]["detail"] = (
                                f"Conv: {conv} ({calls} UI ops) · {err[:65] if err else (str(ca['summary'] or ca['prompt'] or '')[:65])}"
                            )

                # Recent desktop audit events
                if "assistant_desktop_audit_events" in tables:
                    d_events = db.execute(
                        """SELECT id, run_id, created, app, tool, arguments, result
                           FROM assistant_desktop_audit_events
                           ORDER BY id DESC LIMIT 10"""
                    ).fetchall()
                    for dev in d_events:
                        ts = float(dev["created"] or 0)
                        tool_name = str(dev["tool"] or "")
                        app_name = str(dev["app"] or "desktop")
                        ok_flag = True
                        err_msg = ""
                        try:
                            res_obj = json.loads(dev["result"] or "{}")
                            if isinstance(res_obj, dict) and res_obj.get("ok") is False:
                                ok_flag = False
                                err_obj = res_obj.get("error")
                                err_msg = (
                                    err_obj.get("code") or err_obj.get("message") or str(err_obj)
                                    if isinstance(err_obj, dict)
                                    else str(err_obj or "")
                                )
                        except Exception:
                            pass
                        outcome_str = "OK" if ok_flag else f"BLOCKED/ERR ({err_msg[:45]})"
                        feed.append(
                            {
                                "ts": ts,
                                "time": _fmt_time(ts),
                                "ago": _fmt_ago(ts, now),
                                "badge": "CURSOR/UI",
                                "actor": app_name,
                                "summary": f"{tool_name} in {app_name} -> {outcome_str} (run {str(dev['run_id'])[:8]})",
                            }
                        )

                # Workflow objectives & attempts
                if "workflow_objectives" in tables:
                    w_objs = db.execute(
                        "SELECT id, goal, state, external_stop, created FROM workflow_objectives ORDER BY created DESC LIMIT 8"
                    ).fetchall()
                    for wo in w_objs:
                        oid = str(wo["id"])
                        attempts = (
                            db.execute(
                                """SELECT id, run_id, state, app, conversation_key, failure_kind, tool_calls, created
                                   FROM workflow_attempts WHERE task_id IN (
                                       SELECT id FROM workflow_tasks WHERE objective_id=?
                                   ) ORDER BY created DESC LIMIT 3""",
                                (oid,),
                            ).fetchall()
                            if "workflow_attempts" in tables
                            else []
                        )
                        latest_att = dict(attempts[0]) if attempts else {}
                        last_ev_detail = ""
                        if "workflow_events" in tables:
                            ev_row = db.execute(
                                "SELECT kind, detail, created FROM workflow_events WHERE objective_id=? ORDER BY id DESC LIMIT 1",
                                (oid,),
                            ).fetchone()
                            if ev_row:
                                last_ev_detail = f"{ev_row['kind']}: {str(ev_row['detail'] or '')[:90]}"
                        objectives_summary.append(
                            {
                                "id": oid,
                                "goal": str(wo["goal"]),
                                "state": str(wo["state"]).upper(),
                                "app": latest_att.get("app") or "—",
                                "conversation": latest_att.get("conversation_key") or "—",
                                "tool_calls": latest_att.get("tool_calls", 0),
                                "failure_kind": latest_att.get("failure_kind") or "",
                                "last_event": last_ev_detail,
                                "run_id": latest_att.get("run_id") or "",
                                "created": float(wo["created"] or 0),
                                "created_str": _fmt_time(wo["created"]),
                            }
                        )

                # Pending desktop proposals
                if "assistant_desktop_proposals" in tables:
                    d_props = db.execute(
                        "SELECT id, run_id, tool, arguments, reason, state, created FROM assistant_desktop_proposals WHERE state='pending' ORDER BY created DESC LIMIT 10"
                    ).fetchall()
                    for dp in d_props:
                        pending_actions.append(
                            {
                                "kind": "desktop_action",
                                "id": str(dp["id"]),
                                "run_id": str(dp["run_id"]),
                                "title": f"Desktop Action: {dp['tool']} (run {str(dp['run_id'])[:8]})",
                                "detail": f"Reason: {dp['reason']} | Args: {str(dp['arguments'])[:100]}",
                                "version": "",
                                "sha256": "",
                                "created": float(dp["created"] or 0),
                            }
                        )

                # Pending skill hydration proposals
                if "assistant_skill_proposals" in tables:
                    s_props = db.execute(
                        "SELECT proposal_id, skill_name, trigger_reason, skill_content, status, created FROM assistant_skill_proposals WHERE status='pending_approval' ORDER BY created DESC LIMIT 10"
                    ).fetchall()
                    for sp in s_props:
                        content = str(sp["skill_content"] or "")
                        sha = hashlib.sha256(content.encode("utf-8")).hexdigest().lower()
                        m_ver = re.search(r"(?m)^version:\s*['\"]?([0-9A-Za-z_.-]+)['\"]?", content)
                        ver = m_ver.group(1).strip() if m_ver else "1.0.0"
                        pending_actions.append(
                            {
                                "kind": "skill_proposal",
                                "id": str(sp["proposal_id"]),
                                "run_id": "",
                                "title": f"RSI Skill Proposal: {sp['skill_name']} (v{ver})",
                                "detail": f"Trigger: {sp['trigger_reason']} | SHA-256: {sha[:16]}…",
                                "version": ver,
                                "sha256": sha,
                                "created": float(sp["created"] or 0),
                            }
                        )
        except Exception as exc:
            feed.append(
                {
                    "ts": now,
                    "time": _fmt_time(now),
                    "ago": "now",
                    "badge": "WARN",
                    "actor": "Telemetry",
                    "summary": f"Partial DB read warning: {exc}",
                }
            )

    # 3. Project-Agnostic Long-Horizon Runtime Projects & 24h Shadow A/B Arena (`agent_runtime.sqlite3`)
    long_horizon_projects: list[dict[str, Any]] = []
    shadow_ab_trials: list[dict[str, Any]] = []
    epistemic_triggers: list[dict[str, Any]] = []
    system_gates: list[dict[str, Any]] = []
    runtime_db_path = _agent_runtime_db_path()
    if runtime_db_path.exists():
        try:
            with contextlib.closing(sqlite3.connect(str(runtime_db_path), timeout=3.0)) as rdb:
                rdb.row_factory = sqlite3.Row
                rtables = {r[0] for r in rdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "agent_tasks" in rtables:
                    roots = rdb.execute(
                        """SELECT t.*, r.deadline AS run_deadline
                           FROM agent_tasks t
                           LEFT JOIN agent_runs r ON t.run_id = r.id
                           WHERE t.parent IS NULL
                           ORDER BY t.created DESC, t.rowid DESC LIMIT 12"""
                    ).fetchall()
                    for rt in roots:
                        tid = str(rt["id"])
                        rid = str(rt["run_id"])
                        children = rdb.execute(
                            "SELECT id, goal, state, tier, device, result, rounds FROM agent_tasks WHERE run_id=? AND parent=?",
                            (rid, tid),
                        ).fetchall()
                        epochs = []
                        if "agent_epochs" in rtables:
                            ep_rows = rdb.execute(
                                "SELECT epoch, verified_lemmas, falsified_dead_ends, open_equations, next_frontier, created FROM agent_epochs WHERE task_id=? ORDER BY epoch ASC",
                                (tid,),
                            ).fetchall()
                            for ep in ep_rows:
                                epochs.append(
                                    {
                                        "epoch": int(ep["epoch"]),
                                        "verified_lemmas": json.loads(ep["verified_lemmas"] or "[]"),
                                        "falsified_dead_ends": json.loads(ep["falsified_dead_ends"] or "[]"),
                                        "open_equations": json.loads(ep["open_equations"] or "[]"),
                                        "next_frontier": str(ep["next_frontier"] or ""),
                                        "created": float(ep["created"] or 0),
                                        "created_str": _fmt_time(ep["created"]),
                                    }
                                )
                        guild_children = []
                        for ch in children:
                            c_goal = str(ch["goal"] or "")
                            m_fam = re.match(r"^\s*\[FAMILY:([A-Za-z0-9_.-]+)\]\s*", c_goal, re.IGNORECASE)
                            fam = m_fam.group(1).lower() if m_fam else "qwen"
                            guild_children.append(
                                {
                                    "id": str(ch["id"]),
                                    "family": fam,
                                    "state": str(ch["state"] or "").upper(),
                                    "goal": c_goal[:180],
                                    "result": str(ch["result"] or "")[:240],
                                    "rounds": int(ch["rounds"] or 0),
                                }
                            )
                        latest_ep = epochs[-1] if epochs else {}
                        created_ts = float(rt["created"] or now)
                        proj_entry = {
                            "task_id": tid,
                            "run_id": rid,
                            "goal": str(rt["goal"] or ""),
                            "state": str(rt["state"] or "").upper(),
                            "tier": str(rt["tier"] or "smart"),
                            "rounds": int(rt["rounds"] or 0),
                            "epoch_count": len(epochs),
                            "latest_epoch": latest_ep.get("epoch", 0),
                            "verified_lemmas": latest_ep.get("verified_lemmas", []),
                            "falsified_dead_ends": latest_ep.get("falsified_dead_ends", []),
                            "open_equations": latest_ep.get("open_equations", []),
                            "next_frontier": latest_ep.get("next_frontier", ""),
                            "result": str(rt["result"] or "")[:360],
                            "guild_children": guild_children,
                            "created": created_ts,
                            "created_str": _fmt_time(created_ts),
                        }
                        long_horizon_projects.append(proj_entry)
                        feed.append(
                            {
                                "ts": latest_ep.get("created") or created_ts,
                                "time": _fmt_time(latest_ep.get("created") or created_ts),
                                "ago": _fmt_ago(latest_ep.get("created") or created_ts, now),
                                "badge": f"EPOCH {len(epochs)}" if epochs else "RUNTIME",
                                "actor": f"Task {tid[:8]}",
                                "summary": f"[{proj_entry['state']}] {proj_entry['goal'][:80]} (Guild workers: {len(guild_children)}, Lemmas: {len(proj_entry['verified_lemmas'])})",
                            }
                        )
                if "shadow_ab_trials" in rtables:
                    t_rows = rdb.execute(
                        "SELECT * FROM shadow_ab_trials ORDER BY created_at DESC LIMIT 8"
                    ).fetchall()
                    for tr in t_rows:
                        champ_s = json.loads(tr["champion_shadow_json"] or "{}")
                        chal_s = json.loads(tr["challenger_shadow_json"] or "{}")
                        rem_h = max(0.0, (float(tr["expires_at"] or 0) - now) / 3600.0)
                        shadow_ab_trials.append({
                            "trial_id": str(tr["trial_id"]),
                            "label": str(tr["label"]),
                            "trigger_kind": str(tr["trigger_kind"]),
                            "trigger_summary": str(tr["trigger_summary"]),
                            "status": str(tr["status"]).upper(),
                            "remaining_hours": round(rem_h, 1),
                            "champion_net_bps": float(champ_s.get("total_net_bps", 0.0)),
                            "challenger_net_bps": float(chal_s.get("total_net_bps", 0.0)),
                            "frames_observed": int(chal_s.get("frames_observed", 0)),
                            "proposal_id": str(tr["proposal_id"] or ""),
                            "proposal_sha256": str(tr["proposal_sha256"] or ""),
                            "created_at": float(tr["created_at"] or 0),
                        })
                if "epistemic_trigger_log" in rtables:
                    e_rows = rdb.execute(
                        "SELECT * FROM epistemic_trigger_log ORDER BY created DESC LIMIT 8"
                    ).fetchall()
                    for er in e_rows:
                        ets = float(er["created"] or 0)
                        epistemic_triggers.append({
                            "dedup_key": str(er["dedup_key"]),
                            "kind": str(er["kind"]),
                            "pair": str(er["pair"]),
                            "summary": str(er["summary"]),
                            "resolution": str(er["resolution"]),
                            "trial_id": str(er["trial_id"]),
                            "created": ets,
                            "ago": _fmt_ago(ets, now),
                        })
                        feed.append({
                            "ts": ets,
                            "time": _fmt_time(ets),
                            "ago": _fmt_ago(ets, now),
                            "badge": "EPISTEMIC",
                            "actor": str(er["kind"]),
                            "summary": f"{er['summary']} -> {er['resolution']}",
                        })
                if "system_gate_registry" in rtables:
                    g_rows = rdb.execute(
                        "SELECT * FROM system_gate_registry ORDER BY anomaly_active DESC, domain ASC"
                    ).fetchall()
                    for gr in g_rows:
                        system_gates.append({
                            "domain": str(gr["domain"]),
                            "gate_name": str(gr["gate_name"]),
                            "predicate": str(gr["deterministic_predicate"]),
                            "state_sha256": str(gr["last_state_sha256"]),
                            "anomaly_active": bool(gr["anomaly_active"]),
                            "severity": str(gr["severity"]),
                            "headline": str(gr["headline"]),
                            "ping_count": int(gr["ping_count"] or 0),
                            "updated_at": float(gr["updated_at"] or 0),
                        })
        except Exception:
            pass

    # 4. Dynamic D&D / RSI Research Episodes (`dnd_rsi_blackboard.sqlite3`)
    research_episodes: list[dict[str, Any]] = []
    dnd_db_path = _dnd_blackboard_db_path()
    if dnd_db_path.exists():
        try:
            with contextlib.closing(sqlite3.connect(str(dnd_db_path), timeout=3.0)) as ddb:
                ddb.row_factory = sqlite3.Row
                dtables = {r[0] for r in ddb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "episodes" in dtables:
                    eps = ddb.execute(
                        "SELECT id, created, updated, domain, challenge, goal, dm_model, worker_model, status, score FROM episodes ORDER BY updated DESC LIMIT 10"
                    ).fetchall()
                    for ep in eps:
                        upd = float(ep["updated"] or ep["created"] or 0)
                        research_episodes.append(
                            {
                                "id": str(ep["id"]),
                                "domain": str(ep["domain"] or ""),
                                "challenge": str(ep["challenge"] or ""),
                                "goal": str(ep["goal"] or "")[:200],
                                "dm_model": str(ep["dm_model"] or ""),
                                "worker_model": str(ep["worker_model"] or ""),
                                "status": str(ep["status"] or "").upper(),
                                "score": float(ep["score"] or 0.0),
                                "updated": upd,
                                "updated_str": _fmt_time(upd),
                            }
                        )
        except Exception:
            pass

    # 5. Live External Portal Guild Telemetry (`sabrina_assistant_fresh.sqlite3`)
    external_guild_telemetry: dict[str, Any] = {
        "registered_portals": 0,
        "qualified_active_portals": 0,
        "full_delivery_portals": 0,
        "recent_dispatches": [],
    }
    if db_path.exists():
        try:
            with contextlib.closing(sqlite3.connect(str(db_path), timeout=3.0)) as db:
                db.row_factory = sqlite3.Row
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "assistant_external_portals" in tables:
                    ext_rows = db.execute(
                        "SELECT qualification_state, benchmark_json FROM assistant_external_portals"
                    ).fetchall()
                    external_guild_telemetry["registered_portals"] = len(ext_rows)
                    qual_count = 0
                    full_deliv = 0
                    for er in ext_rows:
                        if str(er["qualification_state"] or "") == "qualified":
                            qual_count += 1
                        try:
                            b = json.loads(er["benchmark_json"] or "{}")
                            if b.get("delivery_reliability") == 1:
                                full_deliv += 1
                        except Exception:
                            pass
                    external_guild_telemetry["qualified_active_portals"] = qual_count
                    external_guild_telemetry["full_delivery_portals"] = full_deliv
                if "portal_audit" in tables:
                    p_rows = db.execute(
                        "SELECT id, created, kind, detail FROM portal_audit ORDER BY id DESC LIMIT 12"
                    ).fetchall()
                    for pr in p_rows:
                        pts = float(pr["created"] or 0)
                        det_raw = str(pr["detail"] or "{}")
                        det_obj: dict[str, Any] = {}
                        try:
                            parsed_det = json.loads(det_raw)
                            if isinstance(parsed_det, dict):
                                det_obj = parsed_det
                        except Exception:
                            pass
                        pid = str(det_obj.get("portal_id") or det_obj.get("portal") or pr["kind"] or "")
                        st = str(det_obj.get("status") or det_obj.get("result") or ("ok" if "response" in str(pr["kind"]) else pr["kind"]))
                        lat = int(det_obj.get("latency_ms") or float(det_obj.get("latency_sec") or 0) * 1000)
                        external_guild_telemetry["recent_dispatches"].append(
                            {
                                "id": int(pr["id"]),
                                "portal_id": pid,
                                "purpose": str(pr["kind"] or ""),
                                "status": st,
                                "latency_ms": lat,
                                "created": pts,
                                "created_str": _fmt_time(pts),
                                "ago": _fmt_ago(pts, now),
                            }
                        )
        except Exception:
            pass

    feed.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return {
        "ok": True,
        "timestamp": now,
        "time_str": _fmt_time(now),
        "nodes": nodes,
        "pending_actions": pending_actions,
        "objectives": objectives_summary,
        "pipelines": capability_pipelines,
        "long_horizon_projects": long_horizon_projects,
        "shadow_ab_trials": shadow_ab_trials,
        "epistemic_triggers": epistemic_triggers,
        "system_gates": system_gates,
        "research_episodes": research_episodes,
        "external_guild_telemetry": external_guild_telemetry,
        "activity_feed": feed[:25],
    }


def launch_swarm_mission(
    objective: str,
    mode: str = "codex_thought_antigravity_implement",
    physical_input: bool = False,
    launch_worker: bool = True,
) -> dict[str, Any]:
    """Dispatch a new Swarm mission that immediately effects the Swarm and registers in SQLite."""
    goal = (objective or "").strip()
    if not goal:
        raise ValueError("Enter a non-empty mission objective or prompt for the Swarm.")

    result: dict[str, Any] = {
        "ok": True,
        "objective": goal,
        "mode": mode,
        "physical_input": bool(physical_input),
    }

    # 1. If a dual-role capability mode is selected, register the 2-stage pipeline in CapabilityRegistry
    if mode in {"codex_thought_antigravity_implement", "antigravity_thought_codex_implement"}:
        reg = CapabilityRegistry(_capabilities_db_path())
        pipe = reg.plan_thought_and_implement(
            objective=goal,
            mode=mode,
            payload={"physical_input": bool(physical_input), "initiated_by": "swarm_command_center"},
        )
        result["pipeline"] = pipe

    # 2. Create durable workflow objective and configure desktop run policy
    from tools.sabrina_lab_assistant_mcp import create_objective_and_start
    from tools.mcp_agent_desktop import desktop_configure_run

    apps = ["Claude.exe", "Antigravity.exe", "Codex.exe"]
    wf = create_objective_and_start(
        task=f"[{mode}] {goal}",
        apps=apps,
        ops={"desktop": True, "host": False, "file": True},
        retry=3,
        timeout=300,
        launch=bool(launch_worker),
    )
    oid = wf["objective_id"]
    result["objective_id"] = oid

    # Pre-configure run-scoped desktop policy with explicit physical_input setting
    desktop_cfg = desktop_configure_run(
        run_id=oid,
        apps=apps,
        access="autonomous" if physical_input else "observe",
        physical_input=bool(physical_input),
        visual_observation=True,
    )
    result["desktop_policy"] = desktop_cfg
    return result


def swap_swarm_pipeline_roles(
    current_mode: str = "codex_thought_antigravity_implement",
    objective: str = "",
) -> dict[str, Any]:
    """Invert Thought/Implement assignment between Codex.exe and Antigravity.exe."""
    reg = CapabilityRegistry(_capabilities_db_path())
    swapped = reg.swap_roles(current_mode)
    pipe = None
    if objective.strip():
        pipe = reg.plan_thought_and_implement(
            objective=objective.strip(),
            mode=swapped["mode"],
            payload={"swapped_from": current_mode, "initiated_by": "swarm_command_center"},
        )
    return {"ok": True, "new_mode": swapped["mode"], "config": swapped, "pipeline": pipe}


def force_release_desktop_lease() -> dict[str, Any]:
    """Force release the shared primary_desktop lease if a run stalled."""
    db_path = _assistant_db_path()
    with sqlite3.connect(str(db_path), timeout=5) as db:
        changed = db.execute("DELETE FROM assistant_desktop_lease WHERE lease_key='primary_desktop'").rowcount
        db.commit()
    return {"ok": True, "released": bool(changed)}


def decide_pending_item(
    kind: str,
    item_id: str,
    approve: bool,
    run_id: str = "",
    expected_version: str = "",
    expected_sha256: str = "",
    reason: str = "Operator decision from Swarm Command Center",
) -> dict[str, Any]:
    """Approve or reject a pending skill proposal or desktop action directly from the UI."""
    if kind == "skill_proposal":
        hydration = SabrinaSkillHydration(db_path=_assistant_db_path())
        if approve:
            return hydration.approve_proposal(
                item_id,
                expected_version=expected_version,
                expected_sha256=expected_sha256,
                reviewer="swarm_command_center",
                feedback=reason,
            )
        return hydration.reject_proposal(item_id, reason=reason, reviewer="swarm_command_center")

    if kind == "desktop_action":
        from tools.sabrina_lab_assistant_mcp import sabrina_desktop_decide
        return sabrina_desktop_decide(
            run_id=run_id,
            proposal_id=item_id,
            approve=bool(approve),
            decision_reason=reason,
        )

    raise ValueError(f"Unknown pending item kind: {kind!r}")


class SwarmCommandView(_SwarmBaseFrame):
    """Intuitive Swarm Command Center tab for the native Tkinter Sabrina Operator Console."""

    def __init__(self, parent, console=None, on_select_run=None):
        super().__init__(parent)
        self.console = console
        self.on_select_run = on_select_run
        self._busy = False
        self._state: dict[str, Any] = {}
        self._pending_map: dict[str, dict[str, Any]] = {}
        self._obj_map: dict[str, dict[str, Any]] = {}
        self._node_vars: dict[str, tuple[tk.StringVar, tk.StringVar]] = {}
        self._topo_images: dict[str, Any] = {}
        self._topo_phase: int = 0
        self._topo_canvas: Any = None
        self._build_ui()
        self._load_topology_avatars()
        self.refresh()
        self._animate_topology()

    def _build_ui(self):
        # 1. Top Banner: Swarm Topology & Node Status Cards
        header_bar = ttk.Frame(self)
        header_bar.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(
            header_bar,
            text="Swarm Command Center — Live Multi-Agent Topology & Control",
            font=("Segoe UI", 12, "bold"),
        ).pack(side="left")
        self.last_sync_var = tk.StringVar(value="Syncing swarm state…")
        ttk.Label(header_bar, textvariable=self.last_sync_var).pack(side="right")

        self._topo_canvas = tk.Canvas(
            self,
            height=135,
            bg="#0f172a",
            highlightthickness=1,
            highlightbackground="#263044",
        )
        self._topo_canvas.pack(fill="x", padx=10, pady=(2, 4))
        self._topo_canvas.bind("<Configure>", lambda _e: self._draw_topology())

        nodes_frame = ttk.Frame(self)
        nodes_frame.pack(fill="x", padx=8, pady=4)
        for key, title in (
            ("sabrina", "1. Sabrina Controller"),
            ("codex", "2. Codex Astra"),
            ("antigravity", "3. Antigravity IDE"),
            ("claude", "4. Claude Desktop"),
            ("desktop", "5. Cursor & Screen Gate"),
        ):
            card = ttk.Frame(nodes_frame, style="Card.TFrame", padding=8)
            card.pack(side="left", fill="both", expand=True, padx=3)
            ttk.Label(card, text=title, style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
            st_var = tk.StringVar(value="IDLE")
            dt_var = tk.StringVar(value="—")
            ttk.Label(card, textvariable=st_var, style="Card.TLabel", font=("Segoe UI", 11, "bold")).pack(
                anchor="w", pady=(2, 2)
            )
            ttk.Label(card, textvariable=dt_var, style="Card.TLabel", wraplength=165, font=("Segoe UI", 8)).pack(
                anchor="w"
            )
            self._node_vars[key] = (st_var, dt_var)

        # 2. Direct Swarm Mission Control Bar (Effects the Swarm immediately)
        ctrl_card = ttk.Frame(self, style="Card.TFrame", padding=10)
        ctrl_card.pack(fill="x", padx=10, pady=6)

        row1 = ttk.Frame(ctrl_card, style="Card.TFrame")
        row1.pack(fill="x", pady=(0, 6))
        ttk.Label(row1, text="Mission / Prompt:", style="Card.TLabel", font=("Segoe UI", 9, "bold")).pack(
            side="left", padx=(0, 6)
        )
        self.goal_var = tk.StringVar(
            value="Design and verify a bounded desktop or code improvement task across the Swarm."
        )
        ttk.Entry(row1, textvariable=self.goal_var, width=68).pack(side="left", fill="x", expand=True, padx=(0, 8))

        row2 = ttk.Frame(ctrl_card, style="Card.TFrame")
        row2.pack(fill="x")
        ttk.Label(row2, text="Swarm Routing:", style="Card.TLabel").pack(side="left", padx=(0, 6))
        self.mode_var = tk.StringVar(value="codex_thought_antigravity_implement")
        self.mode_combo = ttk.Combobox(
            row2,
            textvariable=self.mode_var,
            state="readonly",
            width=36,
            values=(
                "codex_thought_antigravity_implement",
                "antigravity_thought_codex_implement",
                "workflow_objective",
            ),
        )
        self.mode_combo.pack(side="left", padx=(0, 10))

        self.phys_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            row2,
            text="Unlock Physical Cursor (physical_input)",
            variable=self.phys_var,
        ).pack(side="left", padx=(0, 10))

        ttk.Button(row2, text="Launch Swarm Mission", command=self._on_launch).pack(side="left", padx=3)
        ttk.Button(row2, text="Swap Roles (Codex <-> AGY)", command=self._on_swap_roles).pack(side="left", padx=3)
        ttk.Button(row2, text="Release Cursor Lease", command=self._on_release_lease).pack(side="left", padx=3)

        self.feedback_var = tk.StringVar(
            value="Ready. Launch a mission above, approve pending gates below, or inspect the live plain-English feed."
        )
        ttk.Label(ctrl_card, textvariable=self.feedback_var, style="Card.TLabel", font=("Segoe UI", 9)).pack(
            anchor="w", pady=(6, 0)
        )

        # 3. Split Panes: Left = Pending Approvals + Active Objectives | Right = Plain-English Live Swarm Feed
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=(2, 8))

        left_col = ttk.Frame(panes)
        panes.add(left_col, weight=1)

        right_col = ttk.Frame(panes)
        panes.add(right_col, weight=1)

        # Left Upper: Pending Operator Approvals (Skills + Desktop Actions)
        ttk.Label(
            left_col,
            text="Waiting on Operator Approval (Skills & Desktop Actions)",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 3))
        self.pending_tree = ttk.Treeview(
            left_col, columns=("kind", "title", "detail"), show="headings", height=5
        )
        self.pending_tree.heading("kind", text="Gate")
        self.pending_tree.column("kind", width=95)
        self.pending_tree.heading("title", text="Item")
        self.pending_tree.column("title", width=185)
        self.pending_tree.heading("detail", text="Verification / Reason")
        self.pending_tree.column("detail", width=220)
        self.pending_tree.pack(fill="x", pady=(0, 4))

        p_btns = ttk.Frame(left_col)
        p_btns.pack(fill="x", pady=(0, 8))
        ttk.Button(p_btns, text="Approve Selected (Verify SHA-256 / Execute)", command=lambda: self._on_decide(True)).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(p_btns, text="Reject Selected", command=lambda: self._on_decide(False)).pack(side="left")

        # Left Lower: Active Swarm Objectives & Handoffs
        ttk.Label(
            left_col,
            text="Active & Recent Swarm Objectives (Select to Resume / Inspect)",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(2, 3))
        self.obj_tree = ttk.Treeview(
            left_col, columns=("state", "app", "goal", "event"), show="headings", height=6
        )
        self.obj_tree.heading("state", text="State")
        self.obj_tree.column("state", width=80)
        self.obj_tree.heading("app", text="Active App")
        self.obj_tree.column("app", width=105)
        self.obj_tree.heading("goal", text="Objective")
        self.obj_tree.column("goal", width=190)
        self.obj_tree.heading("event", text="Latest Event / Blocker")
        self.obj_tree.column("event", width=160)
        self.obj_tree.pack(fill="both", expand=True, pady=(0, 4))

        o_btns = ttk.Frame(left_col)
        o_btns.pack(fill="x")
        ttk.Button(o_btns, text="Resume / Retry Objective", command=lambda: self._on_obj_control("resume")).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(o_btns, text="Pause", command=lambda: self._on_obj_control("pause")).pack(side="left", padx=(0, 4))
        ttk.Button(o_btns, text="Cancel", command=lambda: self._on_obj_control("cancel")).pack(side="left", padx=(0, 4))
        ttk.Button(o_btns, text="Jump to Decision Trace", command=self._on_jump_trace).pack(side="left")

        # Right: Plain-English Chronological Activity Feed
        ttk.Label(
            right_col,
            text="Plain-English Live Swarm Activity Stream (What is happening & why)",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 3))
        self.feed_text = tk.Text(
            right_col,
            bg="#1f2937",
            fg="#e5e7eb",
            wrap="word",
            font=("Consolas", 9),
            relief="flat",
            state="disabled",
        )
        self.feed_text.pack(fill="both", expand=True)

    def refresh(self):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                state = call_console_backend(
                    "SABRINA_SWARM_CONTROL_API_BASE", "/api/swarm/state", get_live_swarm_state,
                )
                self.after(0, lambda s=state: self._render(s))
            except Exception as exc:
                self.after(0, lambda err=str(exc): self.last_sync_var.set(f"Swarm sync error: {err}"))
            finally:
                self.after(0, lambda: setattr(self, "_busy", False))

        threading.Thread(target=work, daemon=True).start()

    def _render(self, state: dict[str, Any]):
        self._state = state
        self.last_sync_var.set(f"Live Swarm synced at {state.get('time_str', '—')}")

        nodes = state.get("nodes") or {}
        for key, (st_var, dt_var) in self._node_vars.items():
            info = nodes.get(key) or {}
            st_var.set(info.get("status", "IDLE"))
            dt_var.set(info.get("detail", "—"))

        # Render Pending Approvals
        sel_p = self.pending_tree.selection()
        for item in self.pending_tree.get_children():
            self.pending_tree.delete(item)
        self._pending_map.clear()
        for p in state.get("pending_actions") or []:
            iid = f"{p['kind']}:{p['id']}"
            self._pending_map[iid] = p
            self.pending_tree.insert("", "end", iid=iid, values=(p["kind"], p["title"], p["detail"]))
        if sel_p and sel_p[0] in self._pending_map:
            self.pending_tree.selection_set(sel_p[0])

        # Render Objectives
        sel_o = self.obj_tree.selection()
        for item in self.obj_tree.get_children():
            self.obj_tree.delete(item)
        self._obj_map.clear()
        for o in state.get("objectives") or []:
            oid = o["id"]
            self._obj_map[oid] = o
            self.obj_tree.insert(
                "",
                "end",
                iid=oid,
                values=(o["state"], o["app"], o["goal"][:60], o["last_event"][:60]),
            )
        if sel_o and sel_o[0] in self._obj_map:
            self.obj_tree.selection_set(sel_o[0])

        # Render Plain-English Feed
        lines: list[str] = []
        pipes = state.get("pipelines") or []
        if pipes:
            lines.append("=== ACTIVE CAPABILITY PIPELINES (Codex Astra <-> Antigravity) ===")
            for cp in pipes[:4]:
                lines.append(
                    f"  • [{cp['state']}] {cp['stage'].upper()} -> {cp['app']} ({cp['capability']}) | {cp['objective'][:85]}"
                )
            lines.append("")

        lines.append("=== CHRONOLOGICAL SWARM & DESKTOP EVENT STREAM ===")
        for ev in state.get("activity_feed") or []:
            lines.append(f"[{ev['time']} · {ev['ago']}] [{ev['badge']}] {ev['actor']}: {ev['summary']}")

        self.feed_text.configure(state="normal")
        self.feed_text.delete("1.0", "end")
        self.feed_text.insert("end", "\n".join(lines))
        self.feed_text.configure(state="disabled")
        self._draw_topology()

    def _load_topology_avatars(self):
        assets_dir = Path(r"c:\Users\charl\OneDrive\Documents\Sabrina\swarm_console\assets")
        mapping = {
            "sabrina": assets_dir / "coordinator.jpg",
            "codex": assets_dir / "architect.jpg",
            "antigravity": assets_dir / "worker.jpg",
            "claude": assets_dir / "verifier.jpg",
            "desktop": assets_dir / "worker.jpg",
        }
        try:
            from PIL import Image, ImageDraw, ImageTk  # type: ignore
            for key, img_path in mapping.items():
                if not img_path.is_file():
                    continue
                im = Image.open(img_path).convert("RGBA").resize((42, 42), Image.Resampling.LANCZOS)
                mask = Image.new("L", (42, 42), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, 41, 41), fill=255)
                im.putalpha(mask)
                self._topo_images[key] = ImageTk.PhotoImage(im)
        except Exception:
            self._topo_images = {}

    def _animate_topology(self):
        self._topo_phase = (self._topo_phase + 1) % 24
        self._draw_topology()
        self.after(120, self._animate_topology)

    def _draw_topology(self):
        canvas = getattr(self, "_topo_canvas", None)
        if canvas is None:
            return
        w = max(680, canvas.winfo_width())
        h = max(135, canvas.winfo_height())
        canvas.delete("all")

        nodes_state = (self._state or {}).get("nodes") or {}
        specs = [
            ("sabrina", "Sabrina", "#38bdf8"),
            ("codex", "Codex Astra", "#34d399"),
            ("antigravity", "Antigravity", "#a78bfa"),
            ("claude", "Claude", "#fb7185"),
            ("desktop", "Desktop Gate", "#fbbf24"),
        ]
        n = len(specs)
        xs = [int(w * (i + 0.65) / (n + 0.3)) for i in range(n)]
        cy = int(h * 0.44)

        # Animated connecting links between adjacent swarm stages
        for i in range(n - 1):
            st_a = str((nodes_state.get(specs[i][0]) or {}).get("status", "IDLE")).upper()
            st_b = str((nodes_state.get(specs[i + 1][0]) or {}).get("status", "IDLE")).upper()
            active = st_a not in ("IDLE", "OFFLINE", "") or st_b not in ("IDLE", "OFFLINE", "")
            color = "#38bdf8" if active else "#334155"
            canvas.create_line(
                xs[i] + 26,
                cy,
                xs[i + 1] - 26,
                cy,
                fill=color,
                width=2.2 if active else 1.4,
                dash=(6, 4),
                dashoff=self._topo_phase if active else 0,
            )

        # Nodes with generated avatars + pulsing active rings
        pulse_r = 25 + (self._topo_phase % 6) * 0.6
        for (key, label, accent), x in zip(specs, xs):
            info = nodes_state.get(key) or {}
            st = str(info.get("status", "IDLE")).upper()
            is_active = st not in ("IDLE", "OFFLINE", "")
            ring_color = accent if is_active else "#334155"
            if is_active:
                canvas.create_oval(
                    x - pulse_r, cy - pulse_r, x + pulse_r, cy + pulse_r,
                    outline=accent, width=1.2, dash=(3, 3),
                )
            canvas.create_oval(
                x - 23, cy - 23, x + 23, cy + 23,
                fill="#111827", outline=ring_color, width=2.4 if is_active else 1.5,
            )
            if key in self._topo_images:
                canvas.create_image(x, cy, image=self._topo_images[key])
            else:
                canvas.create_oval(x - 10, cy - 10, x + 10, cy + 10, fill=accent, outline="")
            canvas.create_text(x, cy + 34, text=label, fill="#e5e7eb", font=("Segoe UI", 9, "bold"))
            canvas.create_text(
                x, cy + 49, text=st, fill=accent if is_active else "#94a3b8", font=("Segoe UI", 8)
            )

    def _on_launch(self):
        goal = self.goal_var.get().strip()
        mode = self.mode_var.get().strip()
        phys = bool(self.phys_var.get())
        try:
            res = call_console_backend(
                "SABRINA_SWARM_CONTROL_API_BASE", "/api/swarm/launch",
                lambda: launch_swarm_mission(objective=goal, mode=mode, physical_input=phys, launch_worker=True),
                {"objective": goal, "mode": mode, "physical_input": phys, "launch_worker": True},
            )
            oid = res.get("objective_id", "")[:12]
            pipe_id = (res.get("pipeline") or {}).get("pipeline_id", "none")
            self.feedback_var.set(
                f"Launched Swarm Mission! Objective={oid}… | Pipeline={pipe_id} | Physical Cursor={'ON' if phys else 'READ-ONLY'}"
            )
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Launch Swarm Mission Failed", str(exc), parent=self)

    def _on_swap_roles(self):
        cur = self.mode_var.get().strip()
        if cur not in {"codex_thought_antigravity_implement", "antigravity_thought_codex_implement"}:
            cur = "codex_thought_antigravity_implement"
        try:
            objective = self.goal_var.get().strip()
            res = call_console_backend(
                "SABRINA_SWARM_CONTROL_API_BASE", "/api/swarm/swap-roles",
                lambda: swap_swarm_pipeline_roles(current_mode=cur, objective=objective),
                {"current_mode": cur, "objective": objective},
            )
            new_mode = res["new_mode"]
            self.mode_var.set(new_mode)
            self.feedback_var.set(f"Swapped Swarm roles to: {new_mode} ({res['config']['description']})")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Swap Roles Failed", str(exc), parent=self)

    def _on_release_lease(self):
        try:
            res = call_console_backend(
                "SABRINA_SWARM_CONTROL_API_BASE", "/api/swarm/release-lease",
                force_release_desktop_lease, {},
            )
            self.feedback_var.set(
                "Desktop lease released." if res.get("released") else "Desktop lease was already unlocked."
            )
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Release Lease Failed", str(exc), parent=self)

    def _on_decide(self, approve: bool):
        sel = self.pending_tree.selection()
        if not sel:
            messagebox.showinfo("Select Item", "Select a pending skill proposal or desktop action first.", parent=self)
            return
        item = self._pending_map.get(sel[0])
        if not item:
            return
        try:
            payload = {
                "kind": item["kind"],
                "item_id": item["id"],
                "approve": approve,
                "run_id": item.get("run_id", ""),
                "expected_version": item.get("version", ""),
                "expected_sha256": item.get("sha256", ""),
            }
            res = call_console_backend(
                "SABRINA_SWARM_CONTROL_API_BASE", "/api/swarm/decide",
                lambda: decide_pending_item(**payload), payload,
            )
            verb = "Approved" if approve else "Rejected"
            self.feedback_var.set(f"{verb} {item['kind']} ({item['id']}) -> {json.dumps(res)[:100]}")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Decision Failed", str(exc), parent=self)

    def _on_obj_control(self, action: str):
        sel = self.obj_tree.selection()
        if not sel:
            messagebox.showinfo("Select Objective", "Select an objective from the table first.", parent=self)
            return
        oid = sel[0]
        try:
            if os.environ.get("SABRINA_SWARM_API_BASE", "").strip():
                call_console_backend(
                    "SABRINA_SWARM_API_BASE", "/api/active-prompt/control",
                    lambda: None, {"objective_id": oid, "action": action},
                )
                self.feedback_var.set(f"Objective {oid[:12]}… -> {action.upper()} applied on Ego Lab.")
                self.refresh()
                return
            store = WorkflowStore(_assistant_db_path())
            store.control(oid, action)
            if action == "resume":
                directory = ROOT / "artifacts/workflows" / oid
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / "worker.log").open("a", encoding="utf-8") as output:
                    subprocess.Popen(
                        [sys.executable, "-m", "tools.sabrina_workflow", "work", "--id", oid, "--db", str(_assistant_db_path())],
                        cwd=ROOT,
                        stdout=output,
                        stderr=output,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
            self.feedback_var.set(f"Objective {oid[:12]}… -> {action.upper()} applied.")
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Objective Control Failed", str(exc), parent=self)

    def _on_jump_trace(self):
        sel = self.obj_tree.selection()
        if not sel:
            return
        obj = self._obj_map.get(sel[0])
        if obj and obj.get("run_id") and self.on_select_run:
            self.on_select_run(obj["run_id"])
