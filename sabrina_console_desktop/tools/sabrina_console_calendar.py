"""Granular Time-Block Calendar (Month / Week / Day / Hour) for Sabrina Operator Console & Web Dashboard.

Provides:
1. Granular time-block allocation across:
   - Month View (6x7 day grid with scheduled/executed event badges and daily hour load)
   - Week View (7-day x 24-hour matrix showing hourly blocks)
   - Day View (24-hour vertical timeline with 15-minute / 30-minute sub-slots)
   - Hour View (60-minute granular breakdown of the selected hour + real-time execution history)
2. Unified Calendar Event Aggregator (`get_granular_calendar_data`):
   - Reads upcoming & recurring jobs from `Sabrina/data/social/scheduler.sqlite3` (`scheduled_jobs`)
   - Projects recurring jobs across the target Month/Week/Day window so Sabrina's future time blocks are visible
   - Reads historical and active Swarm / Sabrina sessions (`assistant_sessions`), workflow objectives (`workflow_objectives`),
     and capability handoffs (`handoffs`) so past and current hours show what Sabrina actually spent time on
   - Reads Windows Workshop schedules (`artifacts/desktop_workshop_jobs`)
3. Direct Time-Block Scheduling & Granularization (`schedule_calendar_block`, `seed_default_daily_blocks`):
   - Lets the operator click any Month day, Week cell, Day hour, or Hour minute-slot to schedule a capability
     or Swarm mission at an exact Date + Hour + Minute with duration and recurrence.
"""
from __future__ import annotations

import calendar as pycal
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk
    _BaseFrame = ttk.Frame
except ImportError:
    tk = None  # type: ignore[assignment]
    messagebox = None  # type: ignore[assignment]
    simpledialog = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _BaseFrame = object  # type: ignore[assignment,misc]

ROOT = Path(__file__).resolve().parents[1]
ZONE = ZoneInfo("America/Chicago")
WEEKDAY_LABELS = ("Sun", "Mon", "Tue", "Wed", "Thur", "Fri", "Sat")


def _central_now() -> dt.datetime:
    return dt.datetime.now(ZONE).replace(tzinfo=None)


def _central_timestamp(value: dt.datetime) -> float:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=ZONE)
    return aware.timestamp()


def _central_fromtimestamp(value: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(value, ZONE).replace(tzinfo=None)

from sabrina_social.scheduler import JobStore, Scheduler
from tools.capability_registry import CapabilityRegistry
from tools.sabrina_workflow import DEFAULT_DB


def _scheduler_db_path() -> Path:
    return ROOT / "data" / "social" / "scheduler.sqlite3"


def _assistant_db_path() -> Path:
    return Path(os.environ.get("SABRINA_ASSISTANT_DB", str(DEFAULT_DB)))


def _capabilities_db_path() -> Path:
    return ROOT / "data" / "control" / "capabilities.sqlite3"


def _remote_social_api_url() -> str | None:
    """When running as a Windows operator client without a local scheduler daemon, route to ego's live :8087 API."""
    if os.name != "nt" or os.environ.get("SABRINA_LOCAL_SCHEDULER_ONLY") == "1":
        return None
    try:
        hb = JobStore(_scheduler_db_path()).heartbeat_state()
        if hb.get("active"):
            return None
    except Exception:
        pass
    return os.environ.get("SABRINA_SOCIAL_API_URL", "http://127.0.0.1:8087").rstrip("/")


CATEGORY_COLORS = {
    "frontier": "#ec4899",    # Rose/Magenta: Frontier Research Guild (OpenAI/Gemini/Claude/SpaceX/WSP/RSI)
    "swarm": "#3b82f6",       # Blue: Swarm / Codex / Antigravity / Claude
    "social": "#10b981",      # Emerald: LinkedIn / Social engagement
    "rsi": "#a855f7",         # Purple: Skill draft / RSI / Adaptation
    "system": "#f59e0b",      # Amber: System health / Kraken / Diagnostics
    "executed": "#6366f1",    # Indigo: Completed historical run
}


def _classify_category(capability_or_kind: str) -> str:
    s = (capability_or_kind or "").lower()
    if any(k in s for k in ("frontier.research", "wsp_thinking", "rsi_augment", "hard_challenge")):
        return "frontier"
    if any(k in s for k in ("codex", "antigravity", "claude", "dual_orchestration", "swarm", "workflow")):
        return "swarm"
    if any(k in s for k in ("linkedin", "social", "reply", "engage", "x.")):
        return "social"
    if any(k in s for k in ("skill", "rsi", "adapt", "inventory", "dream")):
        return "rsi"
    return "system"


def schedule_calendar_block(
    capability: str,
    start_iso: str,
    duration_minutes: int = 30,
    repeat_every_minutes: int | None = None,
    objective: str = "",
    mode: str = "codex_thought_antigravity_implement",
    payload: dict[str, Any] | None = None,
    approved_at_schedule: bool = False,
) -> dict[str, Any]:
    """Schedule a granular time block for Sabrina at a specific date/hour/minute."""
    cap = (capability or "").strip()
    if not cap:
        raise ValueError("Capability is required")

    try:
        dt_start = dt.datetime.fromisoformat(start_iso)
    except Exception:
        dt_start = _central_now()

    next_run_ts = _central_timestamp(dt_start)
    dur = max(5, min(720, int(duration_minutes or 30)))
    interval_sec = int(repeat_every_minutes) * 60 if repeat_every_minutes and int(repeat_every_minutes) >= 1 else None
    obj_str = (objective or "").strip() or f"Scheduled {cap} block ({dur}m)"

    if cap == "kraken.inspect":
        payload: dict[str, Any] = {
            "kind": "service_status",
            "title": obj_str[:155],
            "notes": f"duration_minutes={dur}; slot={dt_start.strftime('%Y-%m-%d %H:%M')}",
            "timezone": "America/Chicago",
        }
    else:
        job_payload = {
            "duration_minutes": dur,
            "scheduled_slot": dt_start.strftime("%Y-%m-%d %H:%M"),
            "objective": obj_str,
            "mode": mode,
        }
        job_payload.update(payload or {})
        if cap == "linkedin.replies":
            job_payload.setdefault("max_threads", 5)
        elif cap in ("linkedin.browser", "social.semantic_review"):
            job_payload.setdefault("max_posts", 3)
        elif cap == "x.feed":
            job_payload.setdefault("max_posts", 5)
        elif cap == "linkedin.performance_report":
            job_payload.setdefault("days", 30)
        payload = job_payload

    remote_api = _remote_social_api_url()
    if remote_api:
        try:
            import urllib.request
            req_body = json.dumps({
                "capability": cap,
                "start_iso": dt_start.isoformat(),
                "duration_minutes": dur,
                "repeat_every_minutes": repeat_every_minutes,
                "objective": obj_str,
                "mode": mode,
                "payload": payload,
                "approved_at_schedule": approved_at_schedule,
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{remote_api}/api/calendar/allocate",
                data=req_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=4.0) as resp:
                remote_res = json.load(resp)
                if isinstance(remote_res, dict) and remote_res.get("ok"):
                    remote_res["routed_to"] = remote_api
                    return remote_res
        except Exception:
            pass

    store = JobStore(_scheduler_db_path())
    reg = CapabilityRegistry(_capabilities_db_path())
    sched = Scheduler(store=store, registry=reg)
    if cap in ("linkedin.publish_post", "linkedin.reshare", "x.publish"):
        from sabrina_social.runtime import SocialRuntime
        runtime = SocialRuntime(data_dir=ROOT / "data" / "social")
        job = sched.schedule_approved_social_action(
            runtime, cap, payload, next_run=next_run_ts,
            interval_min=interval_sec, interval_max=interval_sec,
            approved_at_schedule=approved_at_schedule,
        )
        return {"ok": True, "job": job, "scheduled_for": dt_start.isoformat(),
                "duration_minutes": dur, "approval_id": job["payload"].get("scheduled_approval_id")}
    job = sched.schedule(
        capability=cap,
        payload=payload,
        next_run=next_run_ts,
        interval_min=interval_sec,
        interval_max=interval_sec,
        max_attempts=3,
    )
    return {"ok": True, "job": job, "scheduled_for": dt_start.isoformat(), "duration_minutes": dur}


def seed_default_daily_blocks(anchor_date: str | None = None) -> dict[str, Any]:
    """Populate a structured daily hourly cadence for Sabrina if the schedule is empty or requested."""
    base_day = dt.date.fromisoformat(anchor_date) if anchor_date else dt.date.today()
    template = [
        (8, 0, 30, 1440, "system.health_report", "Morning Lab, Bridge & GPU Health Audit"),
        (9, 0, 45, 360, "linkedin.replies", "Inbox & Comment Reply Triage Block"),
        (10, 0, 60, 720, "capabilities.dual_orchestration", "Swarm Deep-Work Block: Codex Thought -> Antigravity Implement"),
        (11, 0, 30, 1440, "x.feed", "X.com (Twitter) Morning Timeline & Technical Trend Scan"),
        (12, 0, 30, 360, "social.semantic_review", "Midday Feed Semantic Scan & Draft Generation"),
        (14, 0, 60, 720, "antigravity.thought", "Afternoon Architecture & RSI Skill Review Block"),
        (15, 0, 30, 1440, "x.feed", "X.com (Twitter) Afternoon Engagement & Bandit Candidate Review"),
        (16, 0, 45, 360, "linkedin.browser", "Afternoon Social Engagement & Verification Pass"),
        (17, 0, 20, 1440, "x.analytics", "X.com (Twitter) Daily Metrics & Reward Reinforcement Snapshot"),
        (18, 0, 30, 1440, "kraken.inspect", "Evening Kraken & Trading Service Inspection"),
    ]
    created = []
    for hr, mn, dur, rep_min, cap, obj in template:
        slot_dt = dt.datetime.combine(base_day, dt.time(hr, mn))
        if _central_timestamp(slot_dt) < _central_timestamp(_central_now()) - 3600:
            slot_dt = slot_dt + dt.timedelta(days=1)
        res = schedule_calendar_block(
            capability=cap,
            start_iso=slot_dt.isoformat(),
            duration_minutes=dur,
            repeat_every_minutes=rep_min,
            objective=obj,
        )
        created.append(res["job"])
    return {"ok": True, "created_count": len(created), "jobs": created}


def get_granular_calendar_data(
    anchor_iso: str | None = None,
    view_mode: str = "week",
    selected_hour: int | None = None,
) -> dict[str, Any]:
    """Return Month, Week, Day, and Hour granular calendar structures around `anchor_iso`."""
    now_dt = _central_now()
    if anchor_iso:
        try:
            anchor_dt = dt.datetime.fromisoformat(anchor_iso)
        except Exception:
            anchor_dt = now_dt
    else:
        anchor_dt = now_dt

    focus_hour = selected_hour if selected_hour is not None else anchor_dt.hour
    focus_hour = max(0, min(23, int(focus_hour)))

    # Compute window bounds: full month of anchor_dt (padded to whole weeks)
    cal = pycal.Calendar(firstweekday=6)  # Sunday first
    month_weeks_dates = cal.monthdatescalendar(anchor_dt.year, anchor_dt.month)
    win_start_dt = dt.datetime.combine(month_weeks_dates[0][0], dt.time(0, 0, 0))
    win_end_dt = dt.datetime.combine(month_weeks_dates[-1][-1], dt.time(23, 59, 59))
    win_start_ts = _central_timestamp(win_start_dt)
    win_end_ts = _central_timestamp(win_end_dt)

    # Week bounds (Sunday..Saturday containing anchor_dt.date())
    anchor_date = anchor_dt.date()
    days_since_sun = (anchor_date.weekday() + 1) % 7
    week_start_date = anchor_date - dt.timedelta(days=days_since_sun)
    week_dates = [week_start_date + dt.timedelta(days=i) for i in range(7)]

    events: list[dict[str, Any]] = []

    # 1. Load scheduled jobs from live remote scheduler (:8087) if client-only, else local scheduler.sqlite3
    sched_path = _scheduler_db_path()
    raw_jobs: list[dict[str, Any]] = []
    remote_api = _remote_social_api_url()
    if remote_api:
        try:
            import urllib.request
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                f"{remote_api}/api/schedule?limit=250", timeout=2.5
            ) as resp:
                rjson = json.load(resp)
                if isinstance(rjson, dict) and isinstance(rjson.get("jobs"), list):
                    raw_jobs = rjson["jobs"]
        except Exception:
            raw_jobs = []
    if not raw_jobs and sched_path.exists():
        try:
            store = JobStore(sched_path)
            raw_jobs = store.list(limit=250)
        except Exception:
            raw_jobs = []

    for job in raw_jobs:
        status = str(job.get("status") or "queued")
        if status == "cancelled":
            continue
        cap = str(job.get("capability") or "operation")
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        dur_min = int(payload.get("duration_minutes") or 30)
        obj_text = str(payload.get("objective") or payload.get("title") or cap)
        owner = "/".join(str(payload.get(key) or "").strip() for key in ("guild", "agent") if payload.get(key))
        if owner:
            obj_text = f"[{owner}] {obj_text}"
        next_run = float(job.get("next_run") or _central_timestamp(now_dt))
        int_min = job.get("interval_min")
        int_max = job.get("interval_max") or int_min
        cat = _classify_category(cap)

        # Base occurrence
        occurrences = [next_run]
        # Project recurring occurrences across the active month/week window (up to 60 projections per job)
        if int_min and int(int_min) >= 60 and status in ("queued", "running"):
            step = max(900, int((int(int_min) + int(int_max or int_min)) // 2))
            cur = next_run + step
            count = 0
            while cur <= win_end_ts and count < 60:
                if cur >= win_start_ts:
                    occurrences.append(cur)
                cur += step
                count += 1

        for idx, occ_ts in enumerate(occurrences):
            if not (win_start_ts <= occ_ts <= win_end_ts):
                continue
            st_dt = _central_fromtimestamp(occ_ts)
            en_dt = st_dt + dt.timedelta(minutes=dur_min)
            events.append(
                {
                    "id": f"{job['id']}#{idx}",
                    "job_id": job["id"],
                    "source": "scheduled" if idx == 0 else "recurring_projection",
                    "capability": cap,
                    "title": f"{cap}",
                    "subtitle": obj_text,
                    "category": cat,
                    "color": CATEGORY_COLORS.get(cat, "#3b82f6"),
                    "status": status.upper() if idx == 0 else "PROJECTED",
                    "start_ts": occ_ts,
                    "end_ts": _central_timestamp(en_dt),
                    "date": st_dt.strftime("%Y-%m-%d"),
                    "hour": st_dt.hour,
                    "minute": st_dt.minute,
                    "duration_min": dur_min,
                    "time_label": f"{st_dt.strftime('%I:%M %p')} ({dur_min}m)",
                    "repeat_label": f"Every {int(int_min)//60}m" if int_min else "One-shot",
                }
            )

    # 2. Load actual executed / active Sabrina runs & Swarm handoffs so past/current hours are granularized
    ass_path = _assistant_db_path()
    if ass_path.exists():
        try:
            with sqlite3.connect(str(ass_path), timeout=4) as db:
                db.row_factory = sqlite3.Row
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "assistant_sessions" in tables:
                    rows = db.execute(
                        """SELECT id, task, state, model, created, updated
                           FROM assistant_sessions
                           WHERE created >= ? AND created <= ?
                           ORDER BY created DESC LIMIT 120""",
                        (win_start_ts, win_end_ts),
                    ).fetchall()
                    for r in rows:
                        c_ts = float(r["created"] or r["updated"] or 0)
                        u_ts = float(r["updated"] or c_ts)
                        dur_m = max(5, min(120, int(round((u_ts - c_ts) / 60.0)) or 10))
                        st_dt = _central_fromtimestamp(c_ts)
                        task_str = str(r["task"] or "Sabrina session")[:90]
                        events.append(
                            {
                                "id": f"run_{r['id']}",
                                "job_id": "",
                                "run_id": str(r["id"]),
                                "source": "executed_session",
                                "capability": str(r["model"] or "sabrina.session"),
                                "title": f"Run: {str(r['model'] or 'Sabrina')}",
                                "subtitle": task_str,
                                "category": "executed",
                                "color": CATEGORY_COLORS["executed"],
                                "status": str(r["state"] or "done").upper(),
                                "start_ts": c_ts,
                                "end_ts": c_ts + dur_m * 60,
                                "date": st_dt.strftime("%Y-%m-%d"),
                                "hour": st_dt.hour,
                                "minute": st_dt.minute,
                                "duration_min": dur_m,
                                "time_label": f"{st_dt.strftime('%I:%M %p')} ({dur_m}m)",
                                "repeat_label": "Logged Run",
                            }
                        )
        except Exception:
            pass

    events.sort(key=lambda e: e["start_ts"])

    # Build Month structure (6x7 grid of days)
    events_by_date: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        events_by_date.setdefault(ev["date"], []).append(ev)

    month_grid: list[list[dict[str, Any]]] = []
    for wk in month_weeks_dates:
        row_days = []
        for d in wk:
            d_iso = d.strftime("%Y-%m-%d")
            d_evs = events_by_date.get(d_iso, [])
            sched_cnt = sum(1 for e in d_evs if e["source"] != "executed_session")
            exec_cnt = sum(1 for e in d_evs if e["source"] == "executed_session")
            total_mins = sum(int(e.get("duration_min", 15)) for e in d_evs if e["source"] != "executed_session")
            row_days.append(
                {
                    "date": d_iso,
                    "day": d.day,
                    "in_month": d.month == anchor_dt.month,
                    "is_today": d == now_dt.date(),
                    "is_selected": d == anchor_date,
                    "scheduled_count": sched_cnt,
                    "executed_count": exec_cnt,
                    "scheduled_hours": round(total_mins / 60.0, 1),
                    "preview": [f"{e['hour']:02d}:{e['minute']:02d} {e['title'][:18]}" for e in d_evs[:3]],
                }
            )
        month_grid.append(row_days)

    # Build Week structure (7 days x 24 hours)
    week_columns: list[dict[str, Any]] = []
    for d in week_dates:
        d_iso = d.strftime("%Y-%m-%d")
        d_evs = events_by_date.get(d_iso, [])
        hours_map: dict[int, list[dict[str, Any]]] = {h: [] for h in range(24)}
        for ev in d_evs:
            hours_map[int(ev["hour"])].append(ev)
        week_columns.append(
            {
                "date": d_iso,
                "label": f"{WEEKDAY_LABELS[(d.weekday() + 1) % 7]} {d:%m/%d}",
                "is_today": d == now_dt.date(),
                "is_selected": d == anchor_date,
                "hours": {str(h): hours_map[h] for h in range(24)},
            }
        )

    # Build Day structure (24 hours for anchor_date with 15-min quarter slots)
    selected_date_iso = anchor_date.strftime("%Y-%m-%d")
    day_events = events_by_date.get(selected_date_iso, [])
    day_hours: list[dict[str, Any]] = []
    for h in range(24):
        h_evs = [e for e in day_events if int(e["hour"]) == h]
        quarters = {0: [], 15: [], 30: [], 45: []}
        for e in h_evs:
            q_slot = (int(e["minute"]) // 15) * 15
            quarters[q_slot].append(e)
        day_hours.append(
            {
                "hour": h,
                "label": dt.time(h, 0).strftime("%I:00 %p"),
                "is_current_hour": (anchor_date == now_dt.date() and h == now_dt.hour),
                "is_selected_hour": h == focus_hour,
                "events": h_evs,
                "quarters": {str(k): v for k, v in quarters.items()},
            }
        )

    # Build Hour structure (60-minute granular breakdown of `focus_hour` on `anchor_date`)
    hour_events = [e for e in day_events if int(e["hour"]) == focus_hour]
    minute_blocks: list[dict[str, Any]] = []
    for m_start in range(0, 60, 5):
        m_end = m_start + 5
        overlapping = []
        for e in hour_events:
            ev_s = int(e["minute"])
            ev_e = ev_s + int(e["duration_min"])
            if ev_s < m_end and ev_e > m_start:
                overlapping.append(e)
        minute_blocks.append(
            {
                "minute_start": m_start,
                "minute_end": m_end,
                "label": f"{focus_hour:02d}:{m_start:02d} – {focus_hour:02d}:{m_end:02d}",
                "status": "ALLOCATED" if overlapping else "FREE",
                "events": overlapping,
            }
        )

    return {
        "ok": True,
        "anchor_iso": anchor_dt.isoformat(),
        "selected_date": selected_date_iso,
        "selected_month_label": anchor_dt.strftime("%B %Y"),
        "selected_week_label": f"Week of {week_dates[0].strftime('%b %d')} – {week_dates[-1].strftime('%b %d, %Y')}",
        "selected_day_label": anchor_date.strftime("%A, %B %d, %Y"),
        "selected_hour": focus_hour,
        "selected_hour_label": f"{anchor_date.strftime('%b %d, %Y')} · {dt.time(focus_hour, 0).strftime('%I:00 %p')} – {dt.time(focus_hour, 59).strftime('%I:59 %p')}",
        "view_mode": view_mode,
        "weekday_labels": list(WEEKDAY_LABELS),
        "month_grid": month_grid,
        "week_columns": week_columns,
        "day_hours": day_hours,
        "hour_breakdown": {
            "hour": focus_hour,
            "events": hour_events,
            "five_minute_blocks": minute_blocks,
        },
        "raw_jobs": raw_jobs,
    }


class GranularCalendarView(_BaseFrame):
    """Multi-scale Calendar View (Month / Week / Day / Hour) for `sabrina_operator_console.py`."""

    def __init__(self, parent, console=None):
        super().__init__(parent)
        self.console = console
        self.view_mode = tk.StringVar(value="week")  # month | week | day | hour
        self.anchor_dt = _central_now().replace(second=0, microsecond=0)
        self.selected_hour = self.anchor_dt.hour
        self._data: dict[str, Any] = {}
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        # Top Navigation & Granularity Mode Bar
        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Label(nav, text="Sabrina Time Granularizer:", font=("Segoe UI", 11, "bold")).pack(side="left", padx=(0, 8))
        for mode_id, label in (("month", "Month"), ("week", "Week"), ("day", "Day (24h)"), ("hour", "Hour (60m)")):
            ttk.Radiobutton(
                nav,
                text=label,
                value=mode_id,
                variable=self.view_mode,
                command=self.refresh,
            ).pack(side="left", padx=4)

        ttk.Separator(nav, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(nav, text="◀ Prev", command=lambda: self._step_period(-1)).pack(side="left", padx=2)
        ttk.Button(nav, text="Today / Now", command=self._jump_now).pack(side="left", padx=2)
        ttk.Button(nav, text="Next ▶", command=lambda: self._step_period(1)).pack(side="left", padx=2)

        self.period_label_var = tk.StringVar(value="")
        ttk.Label(nav, textvariable=self.period_label_var, font=("Segoe UI", 10, "bold")).pack(side="left", padx=12)

        ttk.Button(nav, text="Seed Daily Cadence", command=self._on_seed_daily).pack(side="right", padx=3)

        # Quick Time-Block Allocation Bar
        sched_bar = ttk.Frame(self, style="Card.TFrame", padding=8)
        sched_bar.pack(fill="x", padx=8, pady=4)

        ttk.Label(sched_bar, text="Date:", style="Card.TLabel").pack(side="left", padx=(0, 3))
        self.date_var = tk.StringVar(value=self.anchor_dt.strftime("%Y-%m-%d"))
        ttk.Entry(sched_bar, textvariable=self.date_var, width=11).pack(side="left", padx=(0, 6))

        ttk.Label(sched_bar, text="Hour:", style="Card.TLabel").pack(side="left", padx=(0, 3))
        self.hour_var = tk.StringVar(value=f"{self.selected_hour:02d}")
        self.hour_spin = ttk.Spinbox(sched_bar, from_=0, to=23, textvariable=self.hour_var, width=4, format="%02.0f")
        self.hour_spin.pack(side="left", padx=(0, 4))

        ttk.Label(sched_bar, text="Min:", style="Card.TLabel").pack(side="left", padx=(0, 3))
        self.min_var = tk.StringVar(value="00")
        self.min_combo = ttk.Combobox(
            sched_bar, textvariable=self.min_var, values=("00", "15", "30", "45"), width=4, state="readonly"
        )
        self.min_combo.pack(side="left", padx=(0, 6))

        ttk.Label(sched_bar, text="Dur(m):", style="Card.TLabel").pack(side="left", padx=(0, 3))
        self.dur_var = tk.StringVar(value="30")
        ttk.Combobox(
            sched_bar, textvariable=self.dur_var, values=("15", "30", "45", "60", "90", "120"), width=4, state="readonly"
        ).pack(side="left", padx=(0, 6))

        ttk.Label(sched_bar, text="Capability:", style="Card.TLabel").pack(side="left", padx=(0, 3))
        self.cap_var = tk.StringVar(value="capabilities.dual_orchestration")
        self.cap_combo = ttk.Combobox(
            sched_bar,
            textvariable=self.cap_var,
            state="readonly",
            width=26,
            values=(
                "capabilities.dual_orchestration",
                "codex.astra.thought",
                "antigravity.implement",
                "linkedin.browser",
                "linkedin.replies",
                "linkedin.performance_report",
                "linkedin.publish_post",
                "linkedin.reshare",
                "x.feed",
                "x.status",
                "x.analytics",
                "x.publish",
                "social.semantic_review",
                "system.health_report",
                "kraken.inspect",
            ),
        )
        self.cap_combo.pack(side="left", padx=(0, 6))

        ttk.Label(sched_bar, text="Repeat(m):", style="Card.TLabel").pack(side="left", padx=(0, 3))
        self.rep_var = tk.StringVar(value="")
        ttk.Entry(sched_bar, textvariable=self.rep_var, width=6).pack(side="left", padx=(0, 6))

        ttk.Label(sched_bar, text="Objective:", style="Card.TLabel").pack(side="left", padx=(0, 3))
        self.obj_var = tk.StringVar(value="Scheduled Swarm / Sabrina time block")
        ttk.Entry(sched_bar, textvariable=self.obj_var, width=24).pack(side="left", fill="x", expand=True, padx=(0, 6))

        ttk.Button(sched_bar, text="+ Allocate Block", command=self._on_allocate_block).pack(side="right")

        # Main Split: Left = Visual Calendar Canvas (Month/Week/Day/Hour), Right = Selected Slot Inspector & Job Controls
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=6)

        left_wrap = ttk.Frame(paned)
        paned.add(left_wrap, weight=3)

        self.canvas = tk.Canvas(left_wrap, bg="#111827", highlightthickness=0)
        vscroll = ttk.Scrollbar(left_wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vscroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda e: self._draw_calendar())

        right_wrap = ttk.Frame(paned, style="Card.TFrame", padding=8)
        paned.add(right_wrap, weight=2)

        ttk.Label(
            right_wrap,
            text="Selected Window / Time-Slot Schedule",
            style="Card.TLabel",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w", pady=(0, 4))

        self.slot_tree = ttk.Treeview(
            right_wrap,
            columns=("time", "cap", "status", "repeat"),
            show="headings",
            height=12,
        )
        for col, lbl, w in (
            ("time", "Time & Duration", 125),
            ("cap", "Capability / Run", 145),
            ("status", "State", 80),
            ("repeat", "Cadence", 85),
        ):
            self.slot_tree.heading(col, text=lbl)
            self.slot_tree.column(col, width=w)
        self.slot_tree.pack(fill="both", expand=True, pady=(0, 6))

        btn_row = ttk.Frame(right_wrap, style="Card.TFrame")
        btn_row.pack(fill="x")
        for lbl, act in (("Run Now", "run-now"), ("Pause", "pause"), ("Resume", "resume"), ("Cancel", "cancel")):
            ttk.Button(btn_row, text=lbl, command=lambda a=act: self._on_job_action(a)).pack(side="left", padx=2)

    def _step_period(self, direction: int):
        mode = self.view_mode.get()
        if mode == "month":
            month = self.anchor_dt.month - 1 + direction
            year = self.anchor_dt.year + month // 12
            month = month % 12 + 1
            day = min(self.anchor_dt.day, pycal.monthrange(year, month)[1])
            self.anchor_dt = self.anchor_dt.replace(year=year, month=month, day=day)
        elif mode == "week":
            self.anchor_dt = self.anchor_dt + dt.timedelta(days=7 * direction)
        elif mode == "day":
            self.anchor_dt = self.anchor_dt + dt.timedelta(days=direction)
        elif mode == "hour":
            self.anchor_dt = self.anchor_dt + dt.timedelta(hours=direction)
            self.selected_hour = self.anchor_dt.hour
            self.hour_var.set(f"{self.selected_hour:02d}")
        self.date_var.set(self.anchor_dt.strftime("%Y-%m-%d"))
        self.refresh()

    def _jump_now(self):
        self.anchor_dt = _central_now().replace(second=0, microsecond=0)
        self.selected_hour = self.anchor_dt.hour
        self.date_var.set(self.anchor_dt.strftime("%Y-%m-%d"))
        self.hour_var.set(f"{self.selected_hour:02d}")
        self.refresh()

    def _ask_multiline(self, title: str, prompt: str) -> str | None:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self.winfo_toplevel())
        dialog.geometry("620x360")
        dialog.grab_set()
        ttk.Label(dialog, text=prompt, wraplength=580).pack(anchor="w", padx=12, pady=(12, 6))
        text = tk.Text(dialog, wrap="word", height=12)
        text.pack(fill="both", expand=True, padx=12, pady=6)
        result: dict[str, str | None] = {"value": None}

        def accept():
            result["value"] = text.get("1.0", "end-1c")
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ttk.Button(buttons, text="Use this text", command=accept).pack(side="right", padx=4)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="right", padx=4)
        self.wait_window(dialog)
        return result["value"]

    def _on_allocate_block(self):
        try:
            d_str = self.date_var.get().strip()
            hr = int(self.hour_var.get() or 0)
            mn = int(self.min_var.get() or 0)
            dur = int(self.dur_var.get() or 30)
            rep_raw = self.rep_var.get().strip()
            rep = int(rep_raw) if rep_raw else None
            start_iso = f"{d_str}T{hr:02d}:{mn:02d}:00"
            capability = self.cap_var.get()
            payload: dict[str, Any] = {}
            approved_at_schedule = False
            if capability in {"linkedin.publish_post", "linkedin.reshare"} and rep is not None:
                raise ValueError("LinkedIn publish and reshare jobs are one-time only")
            if capability == "linkedin.publish_post":
                content = self._ask_multiline("Exact LinkedIn post", "Enter the exact text Sabrina may publish at the scheduled time.")
                if content is None or not content.strip():
                    return
                payload["content"] = content.strip()
                approved_at_schedule = messagebox.askyesno(
                    "Approve one-time scheduled post",
                    "Approve this exact post for one publication at " + start_iso.replace("T", " ") + "?\n\n" + content.strip(),
                    parent=self,
                )
                if not approved_at_schedule:
                    return
            elif capability == "linkedin.reshare":
                source_url = simpledialog.askstring(
                    "LinkedIn post URL", "HTTPS URL of the LinkedIn post to reshare:", parent=self)
                if not source_url or not source_url.strip():
                    return
                payload["source_url"] = source_url.strip()
                if messagebox.askyesno("Add commentary?", "Would you like to attach exact commentary to this reshare?", parent=self):
                    commentary = self._ask_multiline("Reshare commentary", "Enter the exact commentary Sabrina may publish.")
                    if commentary is None:
                        return
                    payload["commentary"] = commentary.strip()
                approved_at_schedule = messagebox.askyesno(
                    "Approve one-time reshare",
                    "Approve reshare of this exact LinkedIn post at " + start_iso.replace("T", " ") + "?\n\n" + source_url.strip(),
                    parent=self,
                )
                if not approved_at_schedule:
                    return
            elif capability == "linkedin.performance_report":
                payload["days"] = 30
            schedule_calendar_block(
                capability=capability,
                start_iso=start_iso,
                duration_minutes=dur,
                repeat_every_minutes=rep,
                objective=self.obj_var.get().strip(),
                payload=payload,
                approved_at_schedule=approved_at_schedule,
            )
            self.refresh()
            if self.console:
                self.console.refresh()
        except Exception as exc:
            messagebox.showerror("Schedule Block Failed", str(exc))

    def _on_seed_daily(self):
        try:
            seed_default_daily_blocks(self.date_var.get().strip())
            self.refresh()
            if self.console:
                self.console.refresh()
        except Exception as exc:
            messagebox.showerror("Seed Daily Cadence Failed", str(exc))

    def _on_job_action(self, action: str):
        sel = self.slot_tree.selection()
        if not sel:
            return
        job_id = sel[0].split("#")[0]
        if not job_id.startswith("job_"):
            messagebox.showinfo("Historical Run", "Selected item is a logged execution, not a mutable scheduled job.")
            return
        try:
            handled_remotely = False
            remote_api = _remote_social_api_url()
            if remote_api and action in ("run-now", "pause", "resume", "cancel"):
                try:
                    import urllib.request
                    req = urllib.request.Request(
                        f"{remote_api}/api/schedule/{job_id}/{action}",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=4.0) as resp:
                        json.load(resp)
                        handled_remotely = True
                except Exception:
                    handled_remotely = False

            if not handled_remotely:
                store = JobStore(_scheduler_db_path())
                sched = Scheduler(store=store, registry=CapabilityRegistry(_capabilities_db_path()))
                if action == "run-now":
                    sched.run_now(job_id)
                elif action == "pause":
                    sched.pause(job_id)
                elif action == "resume":
                    sched.resume(job_id)
                elif action == "cancel":
                    sched.cancel(job_id)
            self.refresh()
            if self.console:
                self.console.refresh()
        except Exception as exc:
            messagebox.showerror("Schedule Action Failed", str(exc))

    def refresh(self):
        try:
            self.selected_hour = max(0, min(23, int(self.hour_var.get() or self.selected_hour)))
        except Exception:
            pass
        self._data = get_granular_calendar_data(
            anchor_iso=self.anchor_dt.isoformat(),
            view_mode=self.view_mode.get(),
            selected_hour=self.selected_hour,
        )
        mode = self.view_mode.get()
        if mode == "month":
            self.period_label_var.set(self._data.get("selected_month_label", ""))
        elif mode == "week":
            self.period_label_var.set(self._data.get("selected_week_label", ""))
        elif mode == "day":
            self.period_label_var.set(self._data.get("selected_day_label", ""))
        else:
            self.period_label_var.set(self._data.get("selected_hour_label", ""))

        self._populate_slot_tree()
        self._draw_calendar()

    def _populate_slot_tree(self):
        for item in self.slot_tree.get_children():
            self.slot_tree.delete(item)
        if not self._data:
            return
        mode = self.view_mode.get()
        rows: list[dict[str, Any]] = []
        if mode == "hour":
            rows = (self._data.get("hour_breakdown") or {}).get("events") or []
        elif mode == "day":
            for h_obj in self._data.get("day_hours") or []:
                rows.extend(h_obj.get("events") or [])
        else:
            for col in self._data.get("week_columns") or []:
                for h_evs in (col.get("hours") or {}).values():
                    rows.extend(h_evs)

        seen = set()
        for ev in rows:
            eid = ev["id"]
            if eid in seen:
                continue
            seen.add(eid)
            t_lbl = f"{ev['date'][5:]} {ev['hour']:02d}:{ev['minute']:02d} ({ev['duration_min']}m)"
            self.slot_tree.insert(
                "",
                "end",
                iid=eid,
                values=(t_lbl, ev["title"], ev["status"], ev["repeat_label"]),
            )

    def _select_day_and_hour(self, date_iso: str, hour: int | None = None, switch_to: str | None = None):
        try:
            d = dt.date.fromisoformat(date_iso)
            hr = self.selected_hour if hour is None else max(0, min(23, int(hour)))
            self.anchor_dt = dt.datetime.combine(d, dt.time(hr, 0))
            self.selected_hour = hr
            self.date_var.set(date_iso)
            self.hour_var.set(f"{hr:02d}")
            if switch_to:
                self.view_mode.set(switch_to)
            self.refresh()
        except Exception:
            pass

    def _draw_calendar(self):
        self.canvas.delete("all")
        if not self._data:
            return
        w = max(520, self.canvas.winfo_width())
        mode = self.view_mode.get()
        if mode == "month":
            self._draw_month(w)
        elif mode == "week":
            self._draw_week(w)
        elif mode == "day":
            self._draw_day(w)
        else:
            self._draw_hour(w)

    def _draw_month(self, w: int):
        grid = self._data.get("month_grid") or []
        col_w = w / 7.0
        hdr_h = 26
        row_h = 82
        days_hdr = self._data.get("weekday_labels") or WEEKDAY_LABELS
        for c, d_name in enumerate(days_hdr):
            x0 = c * col_w
            self.canvas.create_rectangle(x0, 0, x0 + col_w, hdr_h, fill="#1f2937", outline="#374151")
            self.canvas.create_text(x0 + col_w / 2, hdr_h / 2, text=d_name, fill="#9ca3af", font=("Segoe UI", 9, "bold"))

        for r, week in enumerate(grid):
            y0 = hdr_h + r * row_h
            for c, cell in enumerate(week):
                x0 = c * col_w
                bg = "#1e293b" if cell["is_selected"] else ("#172554" if cell["is_today"] else ("#111827" if cell["in_month"] else "#0b0f19"))
                border = "#3b82f6" if cell["is_selected"] else "#374151"
                tag = f"mday_{cell['date']}"
                self.canvas.create_rectangle(x0, y0, x0 + col_w, y0 + row_h, fill=bg, outline=border, tags=(tag,))
                self.canvas.create_text(
                    x0 + 6,
                    y0 + 6,
                    anchor="nw",
                    text=f"{cell['day']}",
                    fill="#f9fafb" if cell["in_month"] else "#4b5563",
                    font=("Segoe UI", 9, "bold"),
                    tags=(tag,),
                )
                if cell["scheduled_count"] or cell["executed_count"]:
                    badge = f"{cell['scheduled_count']} sched · {cell['scheduled_hours']}h"
                    self.canvas.create_text(
                        x0 + col_w - 5,
                        y0 + 6,
                        anchor="ne",
                        text=badge,
                        fill="#34d399",
                        font=("Segoe UI", 7),
                        tags=(tag,),
                    )
                for idx, line in enumerate(cell.get("preview") or []):
                    self.canvas.create_text(
                        x0 + 6,
                        y0 + 26 + idx * 16,
                        anchor="nw",
                        text=line[:20],
                        fill="#93c5fd",
                        font=("Consolas", 8),
                        tags=(tag,),
                    )
                self.canvas.tag_bind(
                    tag,
                    "<Button-1>",
                    lambda e, d_iso=cell["date"]: self._select_day_and_hour(d_iso, switch_to="day"),
                )
        self.canvas.configure(scrollregion=(0, 0, w, hdr_h + len(grid) * row_h + 10))

    def _draw_week(self, w: int):
        cols = self._data.get("week_columns") or []
        gutter = 58
        col_w = (w - gutter) / max(1, len(cols))
        hdr_h = 28
        row_h = 30
        self.canvas.create_rectangle(0, 0, gutter, hdr_h, fill="#1f2937", outline="#374151")
        self.canvas.create_text(gutter / 2, hdr_h / 2, text="Hour", fill="#9ca3af", font=("Segoe UI", 8, "bold"))

        for c, col in enumerate(cols):
            x0 = gutter + c * col_w
            bg = "#1d4ed8" if col["is_selected"] else ("#1e3a8a" if col["is_today"] else "#1f2937")
            self.canvas.create_rectangle(x0, 0, x0 + col_w, hdr_h, fill=bg, outline="#374151")
            self.canvas.create_text(x0 + col_w / 2, hdr_h / 2, text=col["label"], fill="#f9fafb", font=("Segoe UI", 8, "bold"))

        for h in range(24):
            y0 = hdr_h + h * row_h
            self.canvas.create_rectangle(0, y0, gutter, y0 + row_h, fill="#111827", outline="#1f2937")
            self.canvas.create_text(gutter - 6, y0 + row_h / 2, anchor="e", text=f"{h:02d}:00", fill="#9ca3af", font=("Consolas", 8))
            for c, col in enumerate(cols):
                x0 = gutter + c * col_w
                evs = (col.get("hours") or {}).get(str(h)) or []
                is_sel = col["is_selected"] and h == self.selected_hour
                bg = "#1e293b" if is_sel else "#111827"
                tag = f"wk_{col['date']}_{h}"
                self.canvas.create_rectangle(x0, y0, x0 + col_w, y0 + row_h, fill=bg, outline="#1f2937", tags=(tag,))
                if evs:
                    ev = evs[0]
                    self.canvas.create_rectangle(
                        x0 + 2,
                        y0 + 3,
                        x0 + col_w - 2,
                        y0 + row_h - 3,
                        fill=ev.get("color", "#3b82f6"),
                        outline="",
                        tags=(tag,),
                    )
                    lbl = f"{ev['title'][:13]}" + (f" (+{len(evs)-1})" if len(evs) > 1 else "")
                    self.canvas.create_text(
                        x0 + 5,
                        y0 + row_h / 2,
                        anchor="w",
                        text=lbl,
                        fill="#ffffff",
                        font=("Segoe UI", 7, "bold"),
                        tags=(tag,),
                    )
                self.canvas.tag_bind(
                    tag,
                    "<Button-1>",
                    lambda e, d_iso=col["date"], hr=h: self._select_day_and_hour(d_iso, hr, switch_to="hour"),
                )
        self.canvas.configure(scrollregion=(0, 0, w, hdr_h + 24 * row_h + 10))

    def _draw_day(self, w: int):
        hours = self._data.get("day_hours") or []
        d_iso = self._data.get("selected_date", "")
        gutter = 78
        row_h = 38
        q_w = (w - gutter) / 4.0
        for q_idx, q_lbl in enumerate((":00 – :15", ":15 – :30", ":30 – :45", ":45 – :60")):
            x0 = gutter + q_idx * q_w
            self.canvas.create_rectangle(x0, 0, x0 + q_w, 24, fill="#1f2937", outline="#374151")
            self.canvas.create_text(x0 + q_w / 2, 12, text=q_lbl, fill="#9ca3af", font=("Consolas", 8, "bold"))

        for h_obj in hours:
            h = int(h_obj["hour"])
            y0 = 24 + h * row_h
            bg_lbl = "#1e3a8a" if h_obj["is_selected_hour"] else "#1f2937"
            self.canvas.create_rectangle(0, y0, gutter, y0 + row_h, fill=bg_lbl, outline="#374151")
            self.canvas.create_text(gutter - 6, y0 + row_h / 2, anchor="e", text=h_obj["label"], fill="#e5e7eb", font=("Segoe UI", 8, "bold"))

            quarters = h_obj.get("quarters") or {}
            for q_idx, q_min in enumerate((0, 15, 30, 45)):
                x0 = gutter + q_idx * q_w
                q_evs = quarters.get(str(q_min)) or []
                tag = f"day_{h}_{q_min}"
                self.canvas.create_rectangle(x0, y0, x0 + q_w, y0 + row_h, fill="#111827", outline="#1f2937", tags=(tag,))
                if q_evs:
                    ev = q_evs[0]
                    self.canvas.create_rectangle(
                        x0 + 3,
                        y0 + 4,
                        x0 + q_w - 3,
                        y0 + row_h - 4,
                        fill=ev.get("color", "#3b82f6"),
                        outline="",
                        tags=(tag,),
                    )
                    self.canvas.create_text(
                        x0 + 6,
                        y0 + row_h / 2,
                        anchor="w",
                        text=f"{ev['title'][:18]} ({ev['duration_min']}m)",
                        fill="#ffffff",
                        font=("Segoe UI", 8, "bold"),
                        tags=(tag,),
                    )
                self.canvas.tag_bind(
                    tag,
                    "<Button-1>",
                    lambda e, hr=h, mn=q_min: (
                        self.min_var.set(f"{mn:02d}"),
                        self._select_day_and_hour(d_iso, hr, switch_to="hour"),
                    ),
                )
        self.canvas.configure(scrollregion=(0, 0, w, 24 + 24 * row_h + 10))

    def _draw_hour(self, w: int):
        hb = self._data.get("hour_breakdown") or {}
        blocks = hb.get("five_minute_blocks") or []
        row_h = 36
        gutter = 115
        for idx, blk in enumerate(blocks):
            y0 = idx * row_h
            self.canvas.create_rectangle(0, y0, gutter, y0 + row_h, fill="#1f2937", outline="#374151")
            self.canvas.create_text(gutter - 8, y0 + row_h / 2, anchor="e", text=blk["label"], fill="#e5e7eb", font=("Consolas", 8, "bold"))
            evs = blk.get("events") or []
            self.canvas.create_rectangle(gutter, y0, w, y0 + row_h, fill="#111827", outline="#1f2937")
            if evs:
                ev = evs[0]
                self.canvas.create_rectangle(
                    gutter + 4,
                    y0 + 4,
                    w - 6,
                    y0 + row_h - 4,
                    fill=ev.get("color", "#10b981"),
                    outline="",
                )
                self.canvas.create_text(
                    gutter + 10,
                    y0 + row_h / 2,
                    anchor="w",
                    text=f"[{ev['status']}] {ev['title']} — {ev['subtitle'][:60]} ({ev['time_label']})",
                    fill="#ffffff",
                    font=("Segoe UI", 8, "bold"),
                )
            else:
                self.canvas.create_text(
                    gutter + 10,
                    y0 + row_h / 2,
                    anchor="w",
                    text="FREE 5-MIN GRANULAR SLOT — Click '+ Allocate Block' above to assign Sabrina",
                    fill="#4b5563",
                    font=("Segoe UI", 8),
                )
        self.canvas.configure(scrollregion=(0, 0, w, len(blocks) * row_h + 10))
