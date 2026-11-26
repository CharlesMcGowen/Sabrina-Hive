"""Unified Active Prompt & Objective Director for Sabrina Modern Console.

Bridges operator natural-language prompts (and promoted Guild Chat syntheses) directly
into Sabrina's live execution engines:
1. `autonomous_objective`: `ObjectiveHarness` (`create_objective_and_start`) durable active prompt loop.
2. `swarm_pipeline`: 2-stage `CapabilityRegistry` (`Codex Thought -> Antigravity Implement`) + workflow objective.
3. `lab_task`: Shared `ego` RTX 5060 Ti Lab Coordinator (`sabrina_task_submit` / `agent_runtime.sqlite3`).
4. `steer_active`: Mid-run operator guidance (`sabrina_objective_correct` / `operator_guidance` event) on active objective.
5. `daemon_capability`: `:8087` background scheduler (`ExploitGym`, `Compression`, `Resource Sweep`, `Self-Repair`), with
   automatic promotion to `autonomous_objective` if no narrow daemon keyword matched.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sqlite3
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
ASSISTANT_DB = ROOT_DIR / "data" / "sabrina_assistant.sqlite3"
FRESH_DB = ROOT_DIR / "data" / "sabrina_assistant_fresh.sqlite3"
RUNTIME_DB = ROOT_DIR / "data" / "agent_runtime.sqlite3"
SOCIAL_API_BASE = os.environ.get("SABRINA_SOCIAL_API_URL", "http://127.0.0.1:8087")


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


def _collect_objectives_from_db(db_path: Path, now: float) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not db_path.exists():
        return items
    try:
        with contextlib.closing(sqlite3.connect(str(db_path), timeout=3.0)) as db:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "workflow_objectives" in tables:
                rows = db.execute(
                    "SELECT id, goal, state, contract, created, deadline FROM workflow_objectives ORDER BY created DESC LIMIT 8"
                ).fetchall()
                for row in rows:
                    oid = str(row["id"])
                    state = str(row["state"] or "unknown").lower()
                    created = float(row["created"] or 0.0)
                    round_num = 0
                    budget_rounds = 10
                    try:
                        c_obj = json.loads(row["contract"] or "{}")
                        if isinstance(c_obj, dict):
                            budget_rounds = int(c_obj.get("budget_rounds") or 10)
                    except Exception:
                        pass
                    last_detail = ""
                    updated = created
                    if "workflow_events" in tables:
                        ev_rows = db.execute(
                            "SELECT kind, detail, created FROM workflow_events WHERE objective_id=? ORDER BY id DESC LIMIT 6",
                            (oid,),
                        ).fetchall()
                        if ev_rows:
                            updated = max(updated, float(ev_rows[0]["created"] or 0.0))
                            raw_d = str(ev_rows[0]["detail"] or "")
                            try:
                                parsed_d = json.loads(raw_d)
                                if isinstance(parsed_d, dict) and "tool" in parsed_d:
                                    round_num = int(parsed_d.get("round") or 0)
                                    last_detail = f"Round {round_num}: {parsed_d.get('tool')} — {str(parsed_d.get('result_preview') or '')[:110]}"
                                else:
                                    last_detail = f"{ev_rows[0]['kind']}: {raw_d[:110]}"
                            except Exception:
                                last_detail = f"{ev_rows[0]['kind']}: {raw_d[:110]}"
                            for ev in ev_rows:
                                if ev["kind"] == "harness_step":
                                    try:
                                        p = json.loads(ev["detail"] or "{}")
                                        round_num = max(round_num, int(p.get("round") or 0))
                                    except Exception:
                                        pass
                    items.append(
                        {
                            "id": oid,
                            "kind": "workflow_objective",
                            "db_name": db_path.name,
                            "prompt": str(row["goal"] or ""),
                            "state": state.upper(),
                            "is_active": state in {"running", "ready", "queued", "leased"},
                            "is_controllable": True,
                            "round": round_num,
                            "budget_rounds": budget_rounds,
                            "last_detail": last_detail or "Initialized in Sabrina Objective Harness.",
                            "updated": updated,
                            "updated_str": _fmt_time(updated),
                            "ago": _fmt_ago(updated, now),
                        }
                    )
            if "assistant_sessions" in tables:
                s_rows = db.execute(
                    "SELECT id, task, state, model, activity, progress, updated FROM assistant_sessions ORDER BY updated DESC LIMIT 4"
                ).fetchall()
                for s in s_rows:
                    st = str(s["state"] or "unknown").lower()
                    upd = float(s["updated"] or 0.0)
                    items.append(
                        {
                            "id": str(s["id"]),
                            "kind": "assistant_session",
                            "db_name": db_path.name,
                            "prompt": str(s["task"] or ""),
                            "state": st.upper(),
                            "is_active": st in {"running", "ready", "queued"},
                            "is_controllable": False,
                            "round": 0,
                            "budget_rounds": 0,
                            "last_detail": f"[{s['model'] or 'local-llm'}] {str(s['activity'] or s['progress'] or '')[:110]}",
                            "updated": upd,
                            "updated_str": _fmt_time(upd),
                            "ago": _fmt_ago(upd, now),
                        }
                    )
    except Exception:
        pass
    return items


def _collect_lab_runtime_tasks(now: float) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not RUNTIME_DB.exists():
        return items
    try:
        with contextlib.closing(sqlite3.connect(str(RUNTIME_DB), timeout=3.0)) as rdb:
            rdb.row_factory = sqlite3.Row
            tables = {r[0] for r in rdb.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "agent_tasks" in tables:
                rows = rdb.execute(
                    "SELECT id, goal, state, tier, rounds, result, created FROM agent_tasks WHERE parent IS NULL ORDER BY created DESC LIMIT 5"
                ).fetchall()
                for r in rows:
                    st = str(r["state"] or "unknown").lower()
                    ts = float(r["created"] or 0.0)
                    items.append(
                        {
                            "id": str(r["id"]),
                            "kind": "lab_task",
                            "db_name": RUNTIME_DB.name,
                            "prompt": str(r["goal"] or ""),
                            "state": st.upper(),
                            "is_active": st in {"running", "queued", "ready", "leased"},
                            "is_controllable": True,
                            "round": int(r["rounds"] or 0),
                            "budget_rounds": 0,
                            "last_detail": (str(r["result"] or "")[:120] or f"Tier: {r['tier'] or 'smart'}"),
                            "updated": ts,
                            "updated_str": _fmt_time(ts),
                            "ago": _fmt_ago(ts, now),
                        }
                    )
    except Exception:
        pass
    return items


def get_active_prompt_state() -> dict[str, Any]:
    """Return Sabrina's live active prompt, controllable objectives, and node statuses."""
    now = time.time()
    seen_ids: set[str] = set()
    directives: list[dict[str, Any]] = []

    for db_candidate in (ASSISTANT_DB, FRESH_DB):
        for item in _collect_objectives_from_db(db_candidate, now):
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                directives.append(item)

    for item in _collect_lab_runtime_tasks(now):
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            directives.append(item)

    directives.sort(key=lambda x: (0 if x["is_active"] else (1 if x["state"] == "PAUSED" else 2), -float(x["updated"] or 0.0)))

    active_prompt = directives[0] if directives else None
    return {
        "ok": True,
        "observed_at": now,
        "active_prompt": active_prompt,
        "recent_directives": directives[:8],
    }


def _try_trigger_social_daemon(prompt_text: str) -> dict[str, Any] | None:
    """Opportunistically trigger :8087 background scheduler if prompt matches a daemon capability."""
    try:
        payload = json.dumps({"prompt": prompt_text, "execute": True}).encode("utf-8")
        req = urllib.request.Request(
            SOCIAL_API_BASE.rstrip("/") + "/api/sabrina/prompt",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("actions"):
                return data
    except Exception:
        return None
    return None


def direct_sabrina_prompt(
    prompt: str,
    execution_mode: str = "autonomous_objective",
    physical_input: bool = False,
    target_objective_id: str | None = None,
) -> dict[str, Any]:
    """Dispatch an operator prompt into Sabrina's live Active Prompt & Objective system."""
    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Enter a non-empty prompt or directive for Sabrina.")

    mode = (execution_mode or "autonomous_objective").strip().lower()
    receipts: list[str] = []
    created_id = ""

    if mode == "swarm_pipeline":
        from tools.sabrina_console_swarm import launch_swarm_mission
        res = launch_swarm_mission(
            objective=clean_prompt,
            mode="codex_thought_antigravity_implement",
            physical_input=bool(physical_input),
            launch_worker=True,
        )
        created_id = str(res.get("objective_id") or "")
        pipe_id = str((res.get("pipeline") or {}).get("pipeline_id") or "")
        receipts.append(
            f"Launched 2-Stage Swarm Pipeline ({pipe_id or 'Codex Thought -> Antigravity Implement'}) "
            f"and active objective `{created_id}`."
        )
        return {
            "ok": True,
            "execution_mode": mode,
            "objective_id": created_id,
            "summary": " ".join(receipts),
            "detail": res,
            "state": get_active_prompt_state(),
        }

    if mode == "lab_task":
        from tools.sabrina_lab_assistant_mcp import sabrina_task_submit
        op_id = uuid.uuid4().hex
        res = sabrina_task_submit(
            goal=clean_prompt,
            operation_id=op_id,
            tier="smart",
            device="auto",
            client_id="modern-console-operator",
            project="active-prompt",
        )
        created_id = str(res.get("task_id") or res.get("id") or op_id)
        receipts.append(f"Submitted durable task `{created_id}` to Sabrina Shared Lab Coordinator (:8090 / ego GPU).")
        return {
            "ok": True,
            "execution_mode": mode,
            "objective_id": created_id,
            "summary": " ".join(receipts),
            "detail": res,
            "state": get_active_prompt_state(),
        }

    if mode == "steer_active":
        snap = get_active_prompt_state()
        target_id = (target_objective_id or "").strip()
        if not target_id:
            candidates = [
                d for d in snap.get("recent_directives", [])
                if d.get("kind") == "workflow_objective" and d.get("state") in {"RUNNING", "READY", "PAUSED", "BLOCKED"}
            ]
            if candidates:
                target_id = candidates[0]["id"]

        if target_id:
            steered = False
            for db_path in (ASSISTANT_DB, FRESH_DB):
                if not db_path.exists():
                    continue
                with contextlib.closing(sqlite3.connect(str(db_path), timeout=5.0)) as con:
                    row = con.execute("SELECT id, goal, state FROM workflow_objectives WHERE id=?", (target_id,)).fetchone()
                    if row:
                        now = time.time()
                        updated_goal = f"{row[1]}\n\n[Operator Live Steering]: {clean_prompt}"
                        con.execute("UPDATE workflow_objectives SET goal=? WHERE id=?", (updated_goal, target_id))
                        con.execute(
                            "INSERT INTO workflow_events(objective_id, kind, detail, created) VALUES(?, 'operator_guidance', ?, ?)",
                            (target_id, clean_prompt, now),
                        )
                        con.commit()
                        steered = True
                        break
            if not steered:
                from tools.sabrina_lab_assistant_mcp import sabrina_objective_correct
                sabrina_objective_correct(target_id, clean_prompt)
            receipts.append(f"Attached live operator steering guidance to active objective `{target_id}`.")
            return {
                "ok": True,
                "execution_mode": mode,
                "objective_id": target_id,
                "summary": " ".join(receipts),
                "state": get_active_prompt_state(),
            }
        # Fall through to starting a new autonomous objective if none was active
        receipts.append("No active objective was running to steer — starting a new Autonomous Active Objective:")

    if mode == "daemon_capability":
        daemon_res = _try_trigger_social_daemon(clean_prompt)
        if daemon_res and daemon_res.get("actions"):
            return {
                "ok": True,
                "execution_mode": mode,
                "objective_id": "",
                "summary": str(daemon_res.get("summary") or "Triggered :8087 background daemon job."),
                "detail": daemon_res,
                "state": get_active_prompt_state(),
            }
        receipts.append("Prompt goes beyond narrow :8087 daemon keywords — promoted directly to Sabrina Autonomous Objective Harness:")

    # Default (`autonomous_objective`) + fallback promotion:
    from tools.sabrina_lab_assistant_mcp import create_objective_and_start
    wf = create_objective_and_start(
        task=clean_prompt,
        apps=["Antigravity.exe", "Codex.exe", "Claude.exe"],
        ops={"desktop": True, "host": False, "file": True},
        retry=3,
        timeout=300,
        launch=True,
        autonomous=True,
    )
    created_id = str(wf.get("objective_id") or "")
    receipts.append(f"Active Objective `{created_id}` started in Sabrina Autonomous Harness.")

    daemon_bonus = _try_trigger_social_daemon(clean_prompt)
    if daemon_bonus and daemon_bonus.get("actions"):
        receipts.append(str(daemon_bonus.get("summary") or ""))

    return {
        "ok": True,
        "execution_mode": "autonomous_objective",
        "objective_id": created_id,
        "summary": " ".join(r for r in receipts if r),
        "detail": wf,
        "state": get_active_prompt_state(),
    }


def control_active_prompt(objective_id: str, action: str) -> dict[str, Any]:
    """Pause, resume, cancel, or single-step an active Sabrina objective or lab task."""
    oid = (objective_id or "").strip()
    act = (action or "").strip().lower()
    if not oid:
        raise ValueError("objective_id is required.")
    if act not in {"pause", "resume", "cancel", "step"}:
        raise ValueError(f"Unsupported control action: {act}")

    # Check if this ID is a workflow_objective first
    is_wf = False
    target_db = ASSISTANT_DB
    for db_cand in (ASSISTANT_DB, FRESH_DB):
        if db_cand.exists():
            with contextlib.closing(sqlite3.connect(str(db_cand), timeout=3.0)) as con:
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "workflow_objectives" in tables and con.execute("SELECT 1 FROM workflow_objectives WHERE id=?", (oid,)).fetchone():
                    is_wf = True
                    target_db = db_cand
                    break

    prev_env = os.environ.get("SABRINA_ASSISTANT_DB")
    try:
        if is_wf:
            os.environ["SABRINA_ASSISTANT_DB"] = str(target_db)
            if act == "step":
                from tools.sabrina_lab_assistant_mcp import sabrina_objective_step
                res = sabrina_objective_step(oid)
            else:
                from tools.sabrina_lab_assistant_mcp import sabrina_objective_control
                res = sabrina_objective_control(oid, act)
        else:
            if act == "cancel":
                from tools.sabrina_lab_assistant_mcp import sabrina_task_cancel
                res = sabrina_task_cancel(oid)
            elif act == "step":
                from tools.sabrina_lab_assistant_mcp import sabrina_task_step
                res = sabrina_task_step(oid)
            else:
                raise ValueError(f"Lab task `{oid}` only supports 'step' or 'cancel' (received '{act}').")
    finally:
        if prev_env is None:
            os.environ.pop("SABRINA_ASSISTANT_DB", None)
        else:
            os.environ["SABRINA_ASSISTANT_DB"] = prev_env

    return {
        "ok": True,
        "objective_id": oid,
        "action": act,
        "detail": res,
        "state": get_active_prompt_state(),
    }
