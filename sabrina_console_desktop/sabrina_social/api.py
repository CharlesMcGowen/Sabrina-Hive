"""Small standalone API/dashboard for Sabrina Social (default port 8087)."""
from __future__ import annotations

import argparse
import os
import time
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field

from .runtime import SocialRuntime
from .scheduler import Scheduler, build_social_scheduler
from .mission import assemble_mission
from tools.capability_registry import CapabilityRegistry

app = FastAPI(title="Sabrina Social", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "null",
        "http://127.0.0.1:8087",
        "http://localhost:8087",
        "http://127.0.0.1:8096",
        "http://localhost:8096",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)
runtime = SocialRuntime()
scheduler = build_social_scheduler(runtime)
capability_registry = CapabilityRegistry()
_SERVICE_STARTED_AT = time.time()


def _service_status_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "sabrina-social",
        "pid": os.getpid(),
        "uptime_sec": round(max(0.0, time.time() - _SERVICE_STARTED_AT), 3),
        "registry_mtime": getattr(scheduler, "registry_mtime", None),
        "lane_limits": {
            "code": getattr(scheduler, "max_code_workers", 4),
            "agent": getattr(scheduler, "max_agent_workers", 2),
            "browser": getattr(scheduler, "max_browser_workers", 2),
        },
    }


@app.get("/api/services/status")
def get_services_status():
    return _service_status_payload()


@app.post("/api/system/reload")
def reload_system_registry():
    global capability_registry
    reloaded = scheduler.reload_registry_if_changed(force=True)
    capability_registry = scheduler.registry
    return {**_service_status_payload(), "reloaded": bool(reloaded)}


@app.get('/livez')
def livez():
    return {'service': 'sabrina-social', 'status': 'alive'}


@app.get('/readyz')
def readyz():
    from .availability import status_snapshot
    value = status_snapshot(runtime.data_dir)
    return JSONResponse(value, status_code=200 if value.get('ready') else 503)


@app.get('/api/recovery/status')
def recovery_status():
    from .availability import status_snapshot
    return status_snapshot(runtime.data_dir)


@app.get('/api/recruiters')
def recruiter_status():
    import json
    from pathlib import Path
    path=Path(os.environ.get('SABRINA_RECRUITER_SNAPSHOT',str(runtime.data_dir/'recruiter-tracker.json')))
    if not path.exists():
        return {'contacts': [], 'counts': {}, 'profile': {}, 'worker': {'state': 'awaiting_first_sync', 'detail': 'Recruiter tracker has not synced from the Windows desktop yet'}}
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        value['sync_age_seconds']=max(0,time.time()-path.stat().st_mtime)
        return value
    except (OSError,ValueError) as exc:
        raise HTTPException(503,'Recruiter tracker snapshot is unavailable') from exc


@app.get('/api/recruiter-templates')
def recruiter_templates():
    from tools.recruiter_outreach_templates import list_templates
    try:
        return {'templates': list_templates(), 'sending': 'review_only'}
    except (OSError, ValueError) as exc:
        raise HTTPException(503, 'Recruiter outreach templates are unavailable') from exc


@app.get('/recruiters',response_class=HTMLResponse)
def recruiter_dashboard():
    from tools.recruiter_tracker import TRACKER_HTML
    return TRACKER_HTML


@app.get('/assets/recruiter-tracker.js')
def recruiter_tracker_script():
    from pathlib import Path
    return FileResponse(Path(__file__).parent/'static/recruiter_tracker.js',media_type='text/javascript',headers={'Cache-Control':'no-store'})


class ActionBody(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class DraftBody(BaseModel):
    topic: str | None = None
    content: str | None = None


class ApprovalBody(BaseModel):
    action: str
    payload: dict[str, Any]


class MissionBody(BaseModel):
    objective: str


@app.post("/api/mission/plan")
def mission_plan(body: MissionBody):
    if not body.objective.strip():
        raise HTTPException(400, "objective is required")
    return assemble_mission(body.objective, capability_registry, scheduler)


@app.get("/health")
def health():
    value = runtime.health()
    status_file = os.environ.get("SABRINA_SOCIAL_STATUS_FILE", "data/social/supervisor-status.json")
    try:
        import json
        supervisor = json.loads(open(status_file, encoding="utf-8").read())
        supervisor["age_seconds"] = max(0, time.time() - float(supervisor.get("updated", 0)))
        supervisor["heartbeat_fresh"] = supervisor["age_seconds"] < 120
        value["supervisor"] = supervisor
    except Exception as exc:
        value["supervisor"] = {"status": "unavailable", "error": str(exc)}
    value["scheduler"] = {"queued": len(scheduler.store.list(status="queued")),
                          "running": len(scheduler.store.list(status="running")),
                          **scheduler.store.heartbeat_state()}
    bridge_url = os.environ.get("SABRINA_BRIDGE_RECOVERY_URL", "http://127.0.0.1:19225")
    try:
        with urllib.request.urlopen(bridge_url.rstrip("/") + "/health", timeout=3) as response:
            value["bridge"] = json.load(response)
    except Exception as exc:
        value["bridge"] = {"status": "unreachable", "error": str(exc)}
    from .availability import status_snapshot
    value['availability'] = status_snapshot(runtime.data_dir)
    return value


class SwarmLaunchBody(BaseModel):
    objective: str
    mode: str = "codex_thought_antigravity_implement"
    physical_input: bool = False
    launch_worker: bool = True


class SwarmSwapBody(BaseModel):
    current_mode: str = "codex_thought_antigravity_implement"
    objective: str = ""


class SwarmDecideBody(BaseModel):
    kind: str
    item_id: str
    approve: bool
    run_id: str = ""
    expected_version: str = ""
    expected_sha256: str = ""


class SwarmChatBody(BaseModel):
    message: str
    mode: str = "hive_council"
    target: str = "local:qwen3:14b"


class SwarmForkBody(BaseModel):
    turn_id: str
    title: str | None = None


class SwarmThreadBody(BaseModel):
    thread_id: str = "main"


class SwarmApiTiersBody(BaseModel):
    enabled: bool = False


@app.get("/api/swarm/chat/roster")
def swarm_chat_roster():
    from tools.swarm_chat_portal import get_swarm_roster
    return get_swarm_roster()


@app.get("/api/swarm/chat/history")
def swarm_chat_history(thread_id: str | None = None):
    from tools.swarm_chat_portal import get_chat_history, list_chat_threads
    info = list_chat_threads()
    return {
        "history": get_chat_history(thread_id=thread_id),
        "active_thread_id": info["active_thread_id"],
        "threads": info["threads"],
    }


@app.post("/api/swarm/chat/clear")
def swarm_chat_clear():
    from tools.swarm_chat_portal import clear_chat_history
    return clear_chat_history()


@app.post("/api/swarm/chat/fork")
def swarm_chat_fork(body: SwarmForkBody):
    from tools.swarm_chat_portal import fork_chat_history
    try:
        return fork_chat_history(body.turn_id, title=body.title)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/swarm/chat/thread")
def swarm_chat_thread(body: SwarmThreadBody):
    from tools.swarm_chat_portal import set_active_chat_thread
    return set_active_chat_thread(body.thread_id)


@app.post("/api/swarm/chat/api-tiers")
def swarm_chat_api_tiers(body: SwarmApiTiersBody):
    from tools.swarm_chat_portal import set_swarm_api_tiers
    return set_swarm_api_tiers(body.enabled)


@app.post("/api/swarm/chat/send")
def swarm_chat_send(body: SwarmChatBody):
    from tools.swarm_chat_portal import send_swarm_chat
    try:
        return send_swarm_chat(body.message, mode=body.mode, target=body.target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/swarm/state")
def swarm_state():
    from tools.sabrina_console_swarm import get_live_swarm_state
    return get_live_swarm_state()


@app.post("/api/swarm/launch")
def swarm_launch(body: SwarmLaunchBody):
    from tools.sabrina_console_swarm import launch_swarm_mission
    try:
        return launch_swarm_mission(
            objective=body.objective,
            mode=body.mode,
            physical_input=body.physical_input,
            launch_worker=body.launch_worker,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/swarm/swap-roles")
def swarm_swap_roles(body: SwarmSwapBody):
    from tools.sabrina_console_swarm import swap_swarm_pipeline_roles
    try:
        return swap_swarm_pipeline_roles(body.current_mode, body.objective)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/swarm/release-lease")
def swarm_release_lease():
    from tools.sabrina_console_swarm import force_release_desktop_lease
    return force_release_desktop_lease()


@app.post("/api/swarm/decide")
def swarm_decide(body: SwarmDecideBody):
    from tools.sabrina_console_swarm import decide_pending_item
    try:
        return decide_pending_item(
            kind=body.kind,
            item_id=body.item_id,
            approve=body.approve,
            run_id=body.run_id,
            expected_version=body.expected_version,
            expected_sha256=body.expected_sha256,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class CalendarBlockBody(BaseModel):
    capability: str = "capabilities.dual_orchestration"
    start_iso: str
    duration_minutes: int = 30
    repeat_every_minutes: int | None = None
    objective: str = ""
    mode: str = "codex_thought_antigravity_implement"
    payload: dict[str, Any] = Field(default_factory=dict)
    approved_at_schedule: bool = False


@app.get("/api/calendar/granular")
def calendar_granular(anchor_iso: str | None = None, view_mode: str = "week", selected_hour: int | None = None):
    from tools.sabrina_console_calendar import get_granular_calendar_data
    return get_granular_calendar_data(anchor_iso=anchor_iso, view_mode=view_mode, selected_hour=selected_hour)


@app.post("/api/calendar/allocate")
def calendar_allocate(body: CalendarBlockBody):
    from tools.sabrina_console_calendar import schedule_calendar_block
    try:
        return schedule_calendar_block(
            capability=body.capability,
            start_iso=body.start_iso,
            duration_minutes=body.duration_minutes,
            repeat_every_minutes=body.repeat_every_minutes,
            objective=body.objective,
            mode=body.mode,
            payload=body.payload,
            approved_at_schedule=body.approved_at_schedule,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/calendar/seed-daily")
def calendar_seed_daily(anchor_date: str | None = None):
    from tools.sabrina_console_calendar import seed_default_daily_blocks
    return seed_default_daily_blocks(anchor_date=anchor_date)


@app.get("/api/frontier-research/state")
def frontier_research_state(limit: int = 50):
    from tools.frontier_research_guild import list_frontier_research_queue
    return list_frontier_research_queue(limit=limit)


@app.post("/api/frontier-research/budget")
def frontier_research_budget(payload: dict[str, Any]):
    from tools.frontier_research_guild import update_weekly_budget
    try:
        return update_weekly_budget(
            provider_id=str(payload.get("provider_id", "")),
            weekly_budget_tokens=payload.get("weekly_budget_tokens"),
            enabled=payload.get("enabled"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/frontier-research/enqueue")
def frontier_research_enqueue(payload: dict[str, Any]):
    from tools.frontier_research_guild import enqueue_frontier_research_task
    try:
        return enqueue_frontier_research_task(
            capability=str(payload.get("capability", "frontier.research.rsi_augment")),
            title=str(payload.get("title", "")),
            prompt=str(payload.get("prompt", "")),
            provider_id=str(payload.get("provider_id", "auto")),
            model_id=str(payload.get("model_id", "auto")),
            thinking_budget_tokens=int(payload.get("thinking_budget_tokens", 12000)),
            schedule_iso=payload.get("schedule_iso") or None,
            duration_minutes=int(payload.get("duration_minutes", 45)),
            repeat_every_minutes=int(payload["repeat_every_minutes"]) if payload.get("repeat_every_minutes") else None,
            run_immediately=bool(payload.get("run_immediately", False)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/frontier-research/run/{task_id}")
def frontier_research_run(task_id: str):
    from tools.frontier_research_guild import execute_frontier_research_task
    try:
        return execute_frontier_research_task(task_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/frontier-research/seed-weekly")
def frontier_research_seed_weekly(payload: dict[str, Any] | None = None):
    from tools.frontier_research_guild import seed_weekly_frontier_research_schedule
    body = payload or {}
    return seed_weekly_frontier_research_schedule(anchor_date=body.get("anchor_date"))


@app.get("/api/capabilities")
def capabilities():
    """Expose the same registered capability catalog used by the bridge watcher."""
    from tools.capability_registry import CapabilityRegistry
    return {"capabilities": CapabilityRegistry().discover()}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """<!doctype html><html><head><meta charset='utf-8'><title>Sabrina Swarm & Social Command Center</title>
<style>body{font:15px system-ui;max-width:1220px;margin:28px auto;padding:0 20px;background:#111827;color:#e5e7eb}
button,input,select,textarea{font:inherit;margin:4px;padding:8px 12px;border-radius:6px;border:1px solid #374151;background:#1f2937;color:#e5e7eb}
button{background:#2563eb;border-color:#1d4ed8;cursor:pointer;font-weight:600}button.secondary{background:#374151;border-color:#4b5563}
textarea{width:95%;height:80px}pre{white-space:pre-wrap;background:#1f2937;padding:14px;border-radius:8px;border:1px solid #374151}.warn{color:#fbbf24}
.card{background:#1f2937;padding:12px;border-radius:8px;border:1px solid #374151}</style></head>
<body><h1>Sabrina Swarm &amp; Social Command Center</h1><p>Live Multi-Agent Swarm Topology (Sabrina &bull; Codex Astra &bull; Antigravity &bull; Claude &bull; Desktop Cursor Broker) + Social Runtime.</p>
<p><a href="/recruiters" style="color:#93c5fd;font-weight:700">Recruiter conversations: outreach, replies, and next actions</a></p>
<section id='meters' style='display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:14px 0'></section>

<h2>Swarm Live Topology (Who is doing what right now)</h2>
<section id='swarmNodes' style='display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:12px 0'></section>

<div class='card' style='margin:14px 0'>
  <h3 style='margin-top:0'>Direct Swarm Mission Control (Effects the Swarm Immediately)</h3>
  <input id='swarmGoal' style='width:65%' value='Design and verify a bounded desktop or code improvement task across the Swarm.' placeholder='Enter objective or prompt for the Swarm...'>
  <select id='swarmMode'>
    <option value='codex_thought_antigravity_implement'>Codex Astra (Thought) &rarr; Antigravity (Implement)</option>
    <option value='antigravity_thought_codex_implement'>Antigravity (Thought) &rarr; Codex Astra (Implement)</option>
    <option value='workflow_objective'>Sabrina Durable Workflow + Independent Verifier</option>
  </select><br>
  <label><input type='checkbox' id='swarmPhys'> Unlock Physical Cursor (physical_input)</label>
  <button onclick='launchSwarm()'>Launch Swarm Mission</button>
  <button class='secondary' onclick='swapSwarmRoles()'>Swap Roles (Codex &harr; Antigravity)</button>
  <button class='secondary' onclick='releaseLease()'>Release Cursor Lease</button>
  <button class='secondary' onclick='refreshSwarm()'>Refresh Swarm</button>
</div>

<div class='card' style='margin:14px 0;border-color:#2563eb'>
  <div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px'>
    <div>
      <h3 style='margin:0;color:#93c5fd'>Swarm Collective Chat Portal (Multi-Family Hive &amp; WSP hf-gradio Guilds)</h3>
      <small style='color:#9ca3af'>Chat with Meta Llama, IBM Granite, Mistral, Qwen3, Constitutional Jury, and External Zero-Auth WSP hf-gradio Guilds with 100% verbatim attribution.</small>
    </div>
    <div>
      <small id='scPill' style='padding:4px 10px;border-radius:999px;background:#064e3b;color:#6ee7b7;font-family:monospace'>0 Cloud Tokens &bull; Ready</small>
      <a href='http://127.0.0.1:8095' target='_blank' rel='noopener' style='margin-left:6px;padding:6px 10px;border-radius:6px;background:#1d4ed8;color:#fff;text-decoration:none;font-size:13px;font-weight:600'>Fullscreen (:8095)</a>
    </div>
  </div>
  <div style='margin:8px 0;display:flex;flex-wrap:wrap;gap:6px;align-items:center'>
    <label>Thread:
      <select id='scThread' onchange='scSwitchThread(this.value)'></select>
    </label>
    <label>Mode:
      <select id='scMode' onchange='scToggle()'>
        <option value='hive_council'>Multi-Family Hive Council (Parallel Guilds + Synthesis)</option>
        <option value='external_council'>HF Guilds (external only)</option>
        <option value='direct_node'>Direct Node / WSP Guild Picker (1-on-1)</option>
      </select>
    </label>
    <label id='scTargetWrap' style='display:none'>Member:
      <select id='scTarget'></select>
    </label>
    <button id='scApiTiersBtn' class='secondary' style='background:#064e3b;border-color:#059669;color:#6ee7b7' onclick='scToggleApiTiers()'>API Tiers: OFF (Open Web + Local)</button>
    <button class='secondary' onclick='scRefresh()'>Refresh WSP &amp; Fleet</button>
    <button class='secondary' onclick='scClear()'>Clear Branch</button>
  </div>
  <div id='scStream' style='max-height:360px;overflow-y:auto;background:#0b1120;padding:12px;border-radius:8px;border:1px solid #374151;margin:8px 0;font-size:13px'>Loading Swarm Collective roster...</div>
  <div style='display:flex;gap:8px;align-items:center'>
    <textarea id='scMsg' placeholder='Ask the Multi-Family Swarm Collective or any WSP hf-gradio Guild member (Enter to send)...' style='flex:1;height:52px;margin:0' onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();scSend();}"></textarea>
    <button id='scSendBtn' onclick='scSend()' style='height:52px;padding:0 18px'>Send to Swarm</button>
  </div>
</div>

<div style='display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0'>
  <div class='card'>
    <h3 style='margin-top:0' class='warn'>Waiting on Operator Approval (Skills &amp; Desktop Actions)</h3>
    <div id='swarmPending'>Loading pending gates...</div>
  </div>
  <div class='card'>
    <h3 style='margin-top:0'>Plain-English Live Swarm Activity Stream</h3>
    <pre id='swarmFeed' style='max-height:220px;overflow-y:auto;margin:0;font-size:13px'>Loading activity feed...</pre>
  </div>
</div>

<div class='card' style='margin:16px 0'>
  <div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px'>
    <h2 style='margin:0'>Sabrina Time Granularizer Calendar (Month &bull; Week &bull; Day &bull; Hour)</h2>
    <div>
      <button onclick="setCalView('month')">Month</button>
      <button onclick="setCalView('week')">Week</button>
      <button onclick="setCalView('day')">Day (24h)</button>
      <button onclick="setCalView('hour')">Hour (60m)</button>
      <button class='secondary' onclick="seedCalDaily()">Seed Daily Cadence</button>
    </div>
  </div>
  <div style='margin:10px 0;padding:8px;background:#111827;border-radius:6px;border:1px solid #374151'>
    <input id='calDate' type='date' style='width:145px'>
    <input id='calHour' type='number' min='0' max='23' value='10' style='width:65px' title='Hour (0-23)'> :
    <select id='calMin'><option value='00'>00</option><option value='15'>15</option><option value='30'>30</option><option value='45'>45</option></select>
    <select id='calDur'><option value='15'>15m</option><option value='30' selected>30m</option><option value='45'>45m</option><option value='60'>60m</option><option value='90'>90m</option></select>
    <select id='calCap'>
      <option value='capabilities.dual_orchestration'>capabilities.dual_orchestration</option>
      <option value='codex.astra.thought'>codex.astra.thought</option>
      <option value='antigravity.implement'>antigravity.implement</option>
      <option value='linkedin.browser'>linkedin.browser</option>
      <option value='linkedin.replies'>linkedin.replies</option>
      <option value='linkedin.performance_report'>linkedin.performance_report</option>
      <option value='x.feed'>x.feed (X.com Timeline &amp; Trend Scan)</option>
      <option value='x.status'>x.status (X.com Auth &amp; Quota Check)</option>
      <option value='x.analytics'>x.analytics (X.com Metrics &amp; Bandit Reward)</option>
      <option value='social.semantic_review'>social.semantic_review</option>
      <option value='system.health_report'>system.health_report</option>
      <option value='kraken.inspect'>kraken.inspect</option>
    </select>
    <input id='calRep' type='number' placeholder='Repeat (min)' style='width:110px'>
    <input id='calObj' placeholder='Block objective...' value='Scheduled Swarm / Sabrina time block' style='width:220px'>
    <button onclick='allocateCalBlock()'>+ Allocate Time Block</button>
  </div>
  <div id='calHeaderLabel' style='font-weight:700;color:#93c5fd;margin-bottom:8px'>Loading granular calendar...</div>
  <div id='calGridWrap' style='max-height:420px;overflow-y:auto;background:#111827;padding:10px;border-radius:6px;border:1px solid #374151'></div>
</div>

<hr style='border-color:#374151;margin:24px 0'>
<div class='card' style='margin:16px 0;border:1px solid #059669'>
  <div style='display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px'>
    <h2 style='margin:0;color:#6ee7b7'>Job Application Funnel, Round-Robin Pulse &amp; Live Visibility</h2>
    <div>
      <button onclick='refreshVisibility()'>Refresh Visibility</button>
      <button onclick='discoverJobsNow()'>Discover LinkedIn Jobs Now</button>
      <button onclick='runRsiLoopNow()' style='background:#7c3aed;border-color:#8b5cf6'>Run RSI Loop (3 Gens)</button>
      <button class='secondary' onclick='healAiServices()'>Self-Heal AI Services</button>
      <button id='boostBtn' class='secondary' onclick='toggleBoostMode()'>24/7 Boost Mode: Checking...</button>
    </div>
  </div>
  <div id='visSummary' style='display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:12px 0'>Loading visibility metrics...</div>
  <div style='display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:10px'>
    <div style='background:#111827;padding:10px;border-radius:6px;border:1px solid #374151'>
      <h3 style='margin:0 0 8px 0;font-size:14px;color:#93c5fd'>Staged &amp; Discovered LinkedIn Jobs</h3>
      <div id='visJobsList' style='max-height:240px;overflow-y:auto;font-size:12px'>No jobs scanned yet.</div>
    </div>
    <div style='background:#111827;padding:10px;border-radius:6px;border:1px solid #374151'>
      <h3 style='margin:0 0 8px 0;font-size:14px;color:#fcd34d'>Recent LinkedIn Activity &amp; Skip/Block Reasons</h3>
      <div id='visActivityList' style='max-height:240px;overflow-y:auto;font-size:12px'>Loading operation details...</div>
    </div>
  </div>
</div>

<h2>LinkedIn &amp; Social Operations</h2>
<button onclick="call('/api/read/status',{})">Check LinkedIn</button>
<button onclick="call('/api/read/inbox',{max_threads:5})">Triage inbox</button>
<button onclick="call('/api/read/feed_suggestions',{max_posts:3})">Review feed</button>
<button onclick="call('/api/read/cybersecurity_scout',{max_posts:4,auto_approve:true})">Run Cybersecurity Scout</button>
<button onclick="runReport('/api/reports/health')">Run health report</button>
<button onclick="runReport('/api/reports/linkedin?days=30')">Run LinkedIn performance report</button>
<h2>Draft a post</h2><input id='topic' placeholder='Topic'><br><textarea id='content' placeholder='Exact content (optional)'></textarea><br>
<button onclick="draft()">Draft for review</button><h2 class='warn'>Pending approvals</h2>
<button onclick="approvals()">Refresh approvals</button><div id='pending'></div>
<h2>Schedule reports and LinkedIn shares</h2>
<p>Health and performance reports can repeat. Posts and reposts are one-time only and require approval of the exact content here.</p>
<select id='jobCapability'><option value='system.health_report'>System health report</option><option value='linkedin.performance_report'>LinkedIn performance report</option><option value='linkedin.publish_post'>Publish exact post</option><option value='linkedin.reshare'>Reshare a LinkedIn post</option></select>
<input id='jobWhen' type='datetime-local'><input id='jobRepeat' type='number' min='1' placeholder='Repeat every N minutes (reports only)'>
<input id='jobDays' type='number' min='1' max='90' value='30' title='Performance report lookback in days'>
<textarea id='jobPostContent' placeholder='Exact post text to publish'></textarea><input id='jobSourceUrl' type='url' placeholder='HTTPS LinkedIn post URL to reshare'><textarea id='jobCommentary' placeholder='Optional exact reshare commentary'></textarea>
<label><input id='jobApproved' type='checkbox'> I approve this exact one-time post or reshare for the scheduled time</label><br>
<button onclick='scheduleOperation()'>Schedule operation</button>
<h2>Scheduled operations</h2><button onclick="refreshJobs()">Refresh schedule</button><button onclick="scheduleFeed()">Schedule feed review</button><div id='jobList'></div><pre id='out'>Ready.</pre>
<script>async function call(url,body,method='POST'){let o={method,headers:{'Content-Type':'application/json'}};if(body!==null)o.body=JSON.stringify({payload:body});let r=await fetch(url,o);out.textContent=JSON.stringify(await r.json(),null,2)}
async function runReport(url){let r=await fetch(url);out.textContent=JSON.stringify(await r.json(),null,2)}
async function draft(){let body={topic:topic.value||null,content:content.value||null};let r=await fetch('/api/draft/post',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});out.textContent=JSON.stringify(await r.json(),null,2);await approvals()}
async function decide(id,verb){let r=await fetch('/api/approvals/'+id+'/'+verb,{method:'POST'});out.textContent=JSON.stringify(await r.json(),null,2);await approvals()}
async function approvals(){let r=await fetch('/api/approvals?status=pending'),j=await r.json();pending.innerHTML='';for(let a of j.approvals){let d=document.createElement('div');d.innerHTML='<p><b>'+a.action+'</b> '+JSON.stringify(a.payload)+' <button>Approve</button> <button>Reject</button></p>';let b=d.querySelectorAll('button');b[0].onclick=()=>decide(a.id,'approve');b[1].onclick=()=>decide(a.id,'reject');pending.appendChild(d)}}
async function refreshJobs(){let r=await fetch('/api/schedule'),j=await r.json();jobList.replaceChildren();for(let x of j.jobs){let card=document.createElement('div');card.className='card';card.style.margin='8px 0';let title=document.createElement('b');title.textContent=x.capability+' · '+x.status+' · '+new Date(x.next_run*1000).toLocaleString();card.append(title);let summary=document.createElement('p');summary.textContent=(x.last_error||((x.result||{}).summary)||'No result recorded yet');card.append(summary);if(x.payload?.scheduled_approval_id){let approved=document.createElement('small');approved.textContent='Exact one-time action approved at schedule creation · approval '+x.payload.scheduled_approval_id;card.append(approved)}let actions=document.createElement('div');for(let [label,action] of [['Run now','run-now'],['Pause','pause'],['Resume','resume'],['Cancel','cancel']]){let button=document.createElement('button');button.textContent=label;button.onclick=()=>jobAction(x.id,action);actions.append(button)}card.append(actions);if(x.result||x.events?.length){let details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='Result and event timeline';pre.textContent=JSON.stringify({result:x.result,events:x.events},null,2);details.append(summary,pre);card.append(details)}jobList.append(card)}}
async function jobAction(id,a){let r=await fetch('/api/schedule/'+id+'/'+a,{method:'POST'});let j=await r.json();out.textContent=JSON.stringify(j,null,2);if(!r.ok)throw new Error(j.detail||'schedule action failed');await refreshJobs()}
async function scheduleFeed(){let r=await fetch('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capability:'linkedin.browser',payload:{max_posts:4},interval_min:2700,interval_max:4500,max_attempts:3})});let j=await r.json();out.textContent=JSON.stringify(j,null,2);if(!r.ok)throw new Error(j.detail||'schedule create failed');await refreshJobs()}
async function scheduleOperation(){let capability=jobCapability.value,now=jobWhen.value?Date.parse(jobWhen.value)/1000:Date.now()/1000,repeat=jobRepeat.value?parseInt(jobRepeat.value,10)*60:null,payload={},approved=jobApproved.checked;if(capability==='linkedin.performance_report')payload.days=parseInt(jobDays.value||'30',10);if(capability==='linkedin.publish_post')payload.content=jobPostContent.value;if(capability==='linkedin.reshare'){payload.source_url=jobSourceUrl.value;payload.commentary=jobCommentary.value}let body={capability,payload,next_run:now,max_attempts:1,approved_at_schedule:approved};if(repeat!==null){body.interval_min=repeat;body.interval_max=repeat}let r=await fetch('/api/schedule',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let j=await r.json();out.textContent=JSON.stringify(j,null,2);if(!r.ok)throw new Error(j.detail||'schedule create failed');await refreshJobs()}
function meter(label,value,good){return '<div style="background:#1f2937;padding:12px;border-radius:8px;border:1px solid #374151"><small>'+label+'</small><div style="font-size:20px;color:'+(good?'#86efac':'#fbbf24')+'">'+value+'</div></div>'}
async function refreshHealth(){try{let r=await fetch('/health');let h=await r.json();let s=h.supervisor||{},q=h.scheduler||{},b=h.bridge||{};meters.innerHTML=meter('Supervisor',s.heartbeat_fresh?'ACTIVE':'STALE',s.heartbeat_fresh)+meter('Scheduler',q.active?'ACTIVE':'DOWN',q.active)+meter('Bridge',b.status||'unknown',b.status==='bridge_ready')+meter('Jobs',(q.running||0)+' running / '+(q.queued||0)+' queued',true);out.dataset.health=JSON.stringify(h)}catch(e){meters.innerHTML=meter('Dashboard','UNREACHABLE',false)}}
async function refreshSwarm(){try{let r=await fetch('/api/swarm/state'),s=await r.json();let n=s.nodes||{};swarmNodes.innerHTML=Object.values(n).map(x=>'<div class="card"><b>'+x.name+'</b><div style="color:#86efac;font-weight:700;margin:4px 0">'+x.status+'</div><small style="color:#9ca3af">'+x.detail+'</small></div>').join('');let p=s.pending_actions||[];swarmPending.innerHTML=p.length?p.map(x=>'<div style="border-bottom:1px solid #374151;padding:6px 0"><b>'+x.title+'</b><br><small>'+x.detail+'</small><br><button onclick="decideSwarm(\\''+x.kind+'\\',\\''+x.id+'\\',true,\\''+(x.run_id||'')+'\\',\\''+(x.version||'')+'\\',\\''+(x.sha256||'')+'\\')">Approve</button> <button class="secondary" onclick="decideSwarm(\\''+x.kind+'\\',\\''+x.id+'\\',false,\\''+(x.run_id||'')+'\\',\\''+(x.version||'')+'\\',\\''+(x.sha256||'')+'\\')">Reject</button></div>').join(''):'<small>No pending skill or desktop actions waiting on operator.</small>';swarmFeed.textContent=(s.activity_feed||[]).map(e=>'['+e.time+' · '+e.ago+'] ['+e.badge+'] '+e.actor+': '+e.summary).join('\\n')}catch(e){swarmFeed.textContent='Swarm state error: '+e}}
async function launchSwarm(){let r=await fetch('/api/swarm/launch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({objective:swarmGoal.value,mode:swarmMode.value,physical_input:swarmPhys.checked,launch_worker:true})});out.textContent=JSON.stringify(await r.json(),null,2);await refreshSwarm()}
async function swapSwarmRoles(){let r=await fetch('/api/swarm/swap-roles',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_mode:swarmMode.value,objective:swarmGoal.value})});let j=await r.json();if(j.new_mode)swarmMode.value=j.new_mode;out.textContent=JSON.stringify(j,null,2);await refreshSwarm()}
async function releaseLease(){let r=await fetch('/api/swarm/release-lease',{method:'POST'});out.textContent=JSON.stringify(await r.json(),null,2);await refreshSwarm()}
async function decideSwarm(kind,id,approve,run_id,ver,sha){let r=await fetch('/api/swarm/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({kind,item_id:id,approve,run_id,expected_version:ver,expected_sha256:sha})});out.textContent=JSON.stringify(await r.json(),null,2);await refreshSwarm()}
let calViewMode='week';calDate.value=new Date().toISOString().slice(0,10);calHour.value=new Date().getHours();
function setCalView(m){calViewMode=m;refreshCal()}
function drillCal(d,h,m){if(d)calDate.value=d;if(h!==null&&h!==undefined)calHour.value=h;calViewMode=m;refreshCal()}
async function allocateCalBlock(){let iso=calDate.value+'T'+String(calHour.value).padStart(2,'0')+':'+calMin.value+':00';let rep=calRep.value?parseInt(calRep.value,10):null;let payload=calCap.value==='linkedin.performance_report'?{days:30}:{};let r=await fetch('/api/calendar/allocate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({capability:calCap.value,start_iso:iso,duration_minutes:parseInt(calDur.value,10),repeat_every_minutes:rep,objective:calObj.value,payload})});out.textContent=JSON.stringify(await r.json(),null,2);await refreshCal();await refreshJobs()}
async function seedCalDaily(){let r=await fetch('/api/calendar/seed-daily?anchor_date='+calDate.value,{method:'POST'});out.textContent=JSON.stringify(await r.json(),null,2);await refreshCal();await refreshJobs()}
async function refreshCal(){try{let iso=calDate.value+'T'+String(calHour.value||0).padStart(2,'0')+':00:00';let r=await fetch('/api/calendar/granular?anchor_iso='+encodeURIComponent(iso)+'&view_mode='+calViewMode+'&selected_hour='+encodeURIComponent(calHour.value||0));let c=await r.json();if(calViewMode==='month'){calHeaderLabel.textContent='Month View: '+c.selected_month_label+' (click any day to drill into Day 24h view)';calGridWrap.innerHTML='<div style="display:grid;grid-template-columns:repeat(7,1fr);gap:6px">'+c.weekday_labels.map(day=>'<div style="text-align:center;padding:6px;font-weight:700;color:#9ca3af">'+day+'</div>').join('')+(c.month_grid||[]).flat().map(d=>'<div onclick="drillCal(\\''+d.date+'\\',null,\\'day\\')" style="cursor:pointer;padding:8px;border-radius:6px;border:1px solid '+(d.is_selected?'#3b82f6':'#374151')+';background:'+(d.in_month?'#1f2937':'#0b0f19')+'"><b>'+d.day+'</b> <small style="float:right;color:#34d399">'+d.scheduled_count+' ('+d.scheduled_hours+'h)</small><div style="font-size:11px;color:#93c5fd;margin-top:4px">'+(d.preview||[]).join('<br>')+'</div></div>').join('')+'</div>'}else if(calViewMode==='week'){calHeaderLabel.textContent='Week View: '+c.selected_week_label+' (click any hour cell to drill into Hour 60m view)';let cols=c.week_columns||[];let h='<table style="width:100%;border-collapse:collapse;font-size:12px"><tr><th style="border:1px solid #374151;padding:4px">Hour</th>'+cols.map(x=>'<th style="border:1px solid #374151;padding:4px">'+x.label+'</th>').join('')+'</tr>';for(let hr=0;hr<24;hr++){h+='<tr><td style="border:1px solid #374151;padding:4px;font-family:monospace">'+String(hr).padStart(2,'0')+':00</td>'+cols.map(col=>{let evs=(col.hours||{})[String(hr)]||[];let badge=evs.length?'<div style="background:'+(evs[0].color||'#2563eb')+';padding:2px 4px;border-radius:4px;color:#fff">'+evs[0].title.slice(0,14)+(evs.length>1?' (+'+(evs.length-1)+')':'')+'</div>':'';return '<td onclick="drillCal(\\''+col.date+'\\','+hr+',\\'hour\\')" style="cursor:pointer;border:1px solid #374151;padding:3px">'+badge+'</td>'}).join('')+'</tr>'}calGridWrap.innerHTML=h+'</table>'}else if(calViewMode==='day'){calHeaderLabel.textContent='Day 24-Hour View: '+c.selected_day_label+' (15-min quarter slots — click to drill into Hour 60m view)';calGridWrap.innerHTML=(c.day_hours||[]).map(hr=>'<div onclick="drillCal(\\''+c.selected_date+'\\','+hr.hour+',\\'hour\\')" style="cursor:pointer;display:grid;grid-template-columns:90px repeat(4,1fr);gap:6px;padding:4px;border-bottom:1px solid #1f2937;align-items:center"><b style="font-family:monospace">'+hr.label+'</b>'+[0,15,30,45].map(q=>{let qevs=(hr.quarters||{})[String(q)]||[];return '<div style="background:#1f2937;padding:4px;border-radius:4px;min-height:22px;font-size:12px">'+(qevs.length?'<span style="color:#86efac;font-weight:600">'+qevs[0].title+' ('+qevs[0].duration_min+'m)</span>':'<span style="color:#4b5563">:'+String(q).padStart(2,'0')+' free</span>')+'</div>'}).join('')+'</div>').join('')}else{calHeaderLabel.textContent='Hour 60-Minute Granular View: '+c.selected_hour_label+' (5-minute time slices)';let blks=((c.hour_breakdown||{}).five_minute_blocks)||[];calGridWrap.innerHTML=blks.map(b=>'<div style="display:flex;justify-content:space-between;padding:5px 8px;border-bottom:1px solid #1f2937;font-family:monospace;font-size:12px"><span>'+b.label+'</span><span>'+(b.events&&b.events.length?'<b style="color:#34d399">['+b.events[0].status+'] '+b.events[0].title+' — '+b.events[0].subtitle+' ('+b.events[0].time_label+')</b>':'<span style="color:#6b7280">FREE 5-MIN SLOT</span>')+'</span></div>').join('')}}catch(e){calGridWrap.textContent='Calendar error: '+e}}
const SC_BASES=['http://127.0.0.1:8095/api/swarm-chat','/api/swarm/chat'];let scBase=SC_BASES[0];
function scEsc(s){let d=document.createElement('div');d.textContent=s||'';return d.innerHTML}
function scJsonToMd(v,d=0){let p='  '.repeat(d);if(Array.isArray(v))return v.length?v.map((x,i)=>x&&typeof x==='object'?p+(i+1)+'.\\n'+scJsonToMd(x,d+1):p+'- '+String(x)).join('\\n'):p+'- *(none)*';if(v&&typeof v==='object'){let ks=Object.keys(v);return ks.length?ks.map(k=>{let l=k.replace(/_/g,' '),x=v[k];if(Array.isArray(x))return !x.length?p+'- **'+l+'**: *(none)*':x.every(e=>!e||typeof e!=='object')?p+'- **'+l+'**: '+x.map(e=>'`'+String(e)+'`').join(', '):p+'- **'+l+'**:\\n'+scJsonToMd(x,d+1);if(x&&typeof x==='object')return p+'- **'+l+'**:\\n'+scJsonToMd(x,d+1);if(typeof x==='boolean'||x===null)return p+'- **'+l+'**: `'+String(x)+'`';return p+'- **'+l+'**: '+String(x)}).join('\\n'):p+'- *(empty)*'}return p+String(v)}
function scInlineMd(t){return scEsc(t).replace(/`([^`]+)`/g,'<code style="background:#1e293b;color:#67e8f9;padding:1px 4px;border-radius:3px">$1</code>').replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>').replace(/(^|[^*])\\*([^*\\n]+)\\*/g,'$1<em>$2</em>')}
function scRenderMd(raw){let s=String(raw||'').trim();if(!s)return '';let c=s,rawJ=null,fm=s.match(/^```(?:json)?\\s*([\\s\\S]*?)\\s*```$/i);if(fm)c=fm[1].trim();if((c.startsWith('{')&&c.endsWith('}'))||(c.startsWith('[')&&c.endsWith(']'))){try{let j=JSON.parse(c);if(j&&typeof j==='object'){s=scJsonToMd(j);rawJ=c}}catch(_){}}else{let bi=s.indexOf('{');if(bi>0&&s.endsWith('}')){try{let j=JSON.parse(s.slice(bi).trim());if(j&&typeof j==='object'){rawJ=s.slice(bi).trim();s=s.slice(0,bi).trim()+'\\n\\n### Structured Context\\n'+scJsonToMd(j)}}catch(_){}}}let ls=s.split(/\\r?\\n/),h='',inC=false,cb=[],inU=false,inO=false,cl=()=>{if(inU){h+='</ul>';inU=false}if(inO){h+='</ol>';inO=false}};for(let ln of ls){let tr=ln.trim();if(tr.startsWith('```')){cl();if(!inC){inC=true;cb=[]}else{h+='<pre style="background:#020617;border:1px solid #1e293b;border-radius:5px;padding:6px 8px;overflow-x:auto"><code>'+scEsc(cb.join('\\n'))+'</code></pre>';inC=false}continue}if(inC){cb.push(ln);continue}if(!tr){cl();continue}let hm=tr.match(/^(#{1,4})\\s+(.*)$/);if(hm){cl();h+='<div style="font-weight:700;color:#93c5fd;margin:6px 0 3px">'+scInlineMd(hm[2])+'</div>';continue}let um=ln.match(/^\\s*[-*]\\s+(.*)$/);if(um){if(inO){h+='</ol>';inO=false}if(!inU){h+='<ul style="margin:3px 0 6px 18px;padding:0">';inU=true}h+='<li>'+scInlineMd(um[1])+'</li>';continue}let om=ln.match(/^\\s*\\d+\\.\\s+(.*)$/);if(om){if(inU){h+='</ul>';inU=false}if(!inO){h+='<ol style="margin:3px 0 6px 18px;padding:0">';inO=true}h+='<li>'+scInlineMd(om[1])+'</li>';continue}cl();h+='<p style="margin:0 0 6px">'+scInlineMd(ln)+'</p>'}cl();if(inC)h+='<pre><code>'+scEsc(cb.join('\\n'))+'</code></pre>';if(rawJ)h+='<details style="margin-top:4px;color:#94a3b8;font-size:11px"><summary>View verbatim JSON payload</summary><pre style="white-space:pre-wrap">'+scEsc(rawJ)+'</pre></details>';return h}
function scToggle(){scTargetWrap.style.display=scMode.value==='direct_node'?'inline-block':'none'}
async function scApi(p,o){let err=null;for(let b of SC_BASES){try{let r=await fetch(b+p,o),j=await r.json();if(!r.ok)throw new Error(j.detail||('HTTP '+r.status));scBase=b;return j}catch(e){err=e}}throw err||new Error('Swarm chat API unreachable')}
let scApiTiersOn=false;
function scUpdateApiBtn(on,cnt){scApiTiersOn=!!on;if(!window.scApiTiersBtn)return;scApiTiersBtn.style.background=scApiTiersOn?'#7c2d12':'#064e3b';scApiTiersBtn.style.borderColor=scApiTiersOn?'#ea580c':'#059669';scApiTiersBtn.style.color=scApiTiersOn?'#fdba74':'#6ee7b7';scApiTiersBtn.textContent=scApiTiersOn?'API Tiers: ON (Cloud Keys)':'API Tiers: OFF ('+(cnt||76)+' Open Web + Local)'}
function scRenderThreads(ths,act){if(!window.scThread)return;scThread.innerHTML='';for(let th of (ths||[{thread_id:'main',title:'Main Thread',turn_count:0}])){let o=document.createElement('option');o.value=th.thread_id;o.textContent=th.title+' ('+(th.turn_count||0)+')';if(th.thread_id===act)o.selected=true;scThread.appendChild(o)}}
async function scSwitchThread(tid){await scApi('/thread',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({thread_id:tid})});await scRefresh()}
async function scForkTurn(tid){await scApi('/fork',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({turn_id:tid})});await scRefresh();scMsg.focus()}
async function scToggleApiTiers(){let d=await scApi('/api-tiers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!scApiTiersOn})});scUpdateApiBtn(d.api_tiers_enabled,d.roster&&d.roster.open_web_portals_count);await scRefresh()}
async function scRefresh(){try{let [ro,hi]=await Promise.all([scApi('/roster'),scApi('/history')]),prev=scTarget.value;scUpdateApiBtn(ro.api_tiers_enabled,ro.open_web_portals_count);scRenderThreads(hi.threads||ro.threads,hi.active_thread_id||ro.active_thread_id);scTarget.innerHTML='';let g1=document.createElement('optgroup');g1.label='Local ego Multi-Family Fleet';for(let n of (ro.local_nodes||[])){let o=document.createElement('option');o.value=n.id;o.textContent=n.label;g1.appendChild(o)}let g2=document.createElement('optgroup');g2.label='External Zero-Auth WSP & Arena hf-gradio Guilds';for(let p of (ro.external_nodes||[])){let o=document.createElement('option');o.value=p.id;o.textContent='['+p.status_badge+'] '+p.label;g2.appendChild(o)}scTarget.append(g1,g2);if(prev)scTarget.value=prev;let turns=hi.history||[];scStream.innerHTML=turns.length?turns.map(t=>{let cards=(t.responses||[]).map(r=>'<div style="background:#111827;border:1px solid #1f2937;border-radius:6px;padding:8px"><div style="display:flex;justify-content:space-between;color:#fcd34d;font-weight:700;font-size:12px"><span>'+scEsc(r.family)+'</span><span>'+scEsc(r.latency_sec+'s · '+r.tokens+' tok')+'</span></div><div style="font-family:monospace;font-size:11px;color:#64748b">'+scEsc(r.speaker)+'</div>'+(r.ok?'<div style="margin-top:4px;color:#e2e8f0">'+scRenderMd(r.display_markdown||r.reply)+'</div>':'<div style="color:#94a3b8;font-style:italic;margin-top:4px">'+scEsc(r.error)+'</div>')+(r.thinking?'<details style="margin-top:4px;color:#94a3b8"><summary>View &lt;think&gt; trace</summary><div>'+scRenderMd(r.thinking)+'</div></details>':'')+'</div>').join('');let syn=t.synthesis&&t.synthesis.reply?'<div style="background:#064e3b44;border:1px solid #05966966;border-radius:6px;padding:10px;margin-top:8px"><div style="color:#6ee7b7;font-weight:700;font-size:12px;margin-bottom:4px">Cross-Family Synthesis ('+scEsc(t.synthesis.speaker)+' · '+scEsc(t.synthesis.latency_sec+'s')+')</div><div style="color:#f8fafc">'+scRenderMd(t.synthesis.display_markdown||t.synthesis.reply)+'</div></div>':'';let fbtn=t.id?'<button class="secondary" style="padding:2px 8px;font-size:11px;color:#7dd3fc;border-color:#38bdf866" onclick="scForkTurn(\\''+scEsc(t.id)+'\\')">⑂ Fork from here</button>':'';return '<div style="border-bottom:1px solid #1f2937;padding:10px 0"><div style="color:#93c5fd;font-weight:700;margin-bottom:8px"><div style="display:flex;justify-content:space-between;align-items:center"><span>You ('+scEsc(t.mode)+'):</span>'+fbtn+'</div>'+scRenderMd(t.user_message_markdown||t.user_message)+'</div><div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px">'+cards+'</div>'+syn+'</div>'}).join(''):'<span style="color:#9ca3af">No messages yet in this branch. Send a prompt below to convene Meta Llama, IBM Granite, Mistral, WSP hf-gradio Guilds, and Qwen3.</span>';scStream.scrollTop=scStream.scrollHeight;scPill.textContent=(scBase.includes(':8095')?'Connected (:8095 Live WSP + Fleet)':'Connected (:8087 Local Fleet)')+' · 0 Cloud Tokens'}catch(e){scPill.textContent='Chat API error: '+e.message}}
async function scSend(){let m=scMsg.value.trim();if(!m)return;let mode=scMode.value,target=scTarget.value||'local:qwen3:14b';scMsg.value='';scSendBtn.disabled=true;scPill.textContent='Swarm deliberating ('+mode+')...';try{let t=await scApi('/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:m,mode,target})});await scRefresh();scPill.textContent='Completed in '+t.total_elapsed_sec+'s · 0 Cloud Tokens'}catch(e){scPill.textContent='Error: '+e.message}finally{scSendBtn.disabled=false}}
async function scClear(){await scApi('/clear',{method:'POST'});await scRefresh()}
let boostActive=false;
async function refreshVisibility(){try{let r=await fetch('/api/reports/visibility'),v=await r.json();let rr=v.round_robin||{},jf=v.job_funnel||{},q=v.quotas||{},ai=v.ai_services||{},rsi=v.rsi||{};boostActive=Boolean(v.boost_24_7);if(window.boostBtn){boostBtn.textContent=boostActive?'24/7 Boost Mode: ON (06:00-23:59 / 24h)':'24/7 Boost Mode: Standard (08:00-21:00)';boostBtn.style.borderColor=boostActive?'#10b981':'#4b5563'}let dc=q.daily_counts||{},dl=q.daily_limits||{};visSummary.innerHTML=meter('Round-Robin Next',rr.next_task||'engage_feed',true)+meter('Last Pulse Task',(rr.last_task||'none')+' ('+(rr.last_status||'idle')+')',rr.last_status==='success'||rr.last_status==='completed')+meter('Jobs Funnel',(jf.applied||0)+' applied / '+(jf.staged||0)+' staged / '+(jf.total||0)+' found',true)+meter('RSI Self-Improvement','Gen '+(rsi.generation||0)+' · V='+(rsi.current_fitness||0)+' (Best '+(rsi.best_fitness||0)+')',true)+meter('Daily Quotas','Apply '+(dc.job_apply||0)+'/'+(dl.job_apply||4)+' · Search '+(dc.job_search||0)+'/'+(dl.job_search||25),!q.circuit_breaker_active)+meter('AI Services',(ai.ollama&&ai.ollama.reachable?'Ollama OK':'Ollama DOWN')+' · Healed '+(ai.coordinator&&ai.coordinator.stale_requests_healed||0),Boolean(ai.ollama&&ai.ollama.reachable));let jobs=v.recent_jobs||[];visJobsList.innerHTML=jobs.length?jobs.map(j=>'<div style="border-bottom:1px solid #1f2937;padding:6px 0"><b>['+(j.score||j.match_score||0)+'%] '+scEsc(j.title)+'</b> — '+scEsc(j.company)+' <span style="color:#34d399">['+scEsc(j.status)+']</span>'+(j.easy_apply?' <span style="color:#60a5fa">[Easy Apply]</span>':'')+'<br><a href="'+scEsc(j.job_url)+'" target="_blank" style="color:#93c5fd">Open Listing</a> · <button style="padding:2px 6px;font-size:11px" onclick="inspectJob(\\''+scEsc(j.job_id)+'\\')">Inspect &amp; Stage</button></div>').join(''):'<span style="color:#9ca3af">No jobs discovered yet. Click "Discover LinkedIn Jobs Now" or wait for the next jobs_discover round-robin slot.</span>';let acts=v.recent_activity||[];visActivityList.innerHTML=acts.length?acts.map(a=>'<div style="border-bottom:1px solid #1f2937;padding:5px 0"><span style="color:#9ca3af;font-family:monospace">'+scEsc(a.time)+'</span> <b>'+scEsc(a.kind)+'</b> ['+scEsc(a.status)+'] — <span style="color:'+(a.verified?'#86efac':'#fcd34d')+'">'+scEsc(a.summary)+'</span></div>').join(''):'No operations recorded.'}catch(e){visSummary.textContent='Visibility error: '+e}}
async function discoverJobsNow(){out.textContent='Running LinkedIn job discovery...';let r=await fetch('/api/jobs/discover',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_jobs:6,easy_apply_only:true})});out.textContent=JSON.stringify(await r.json(),null,2);await refreshVisibility()}
async function runRsiLoopNow(){out.textContent='Running 3-Generation Closed-Loop RSI Cycle...';let r=await fetch('/api/rsi/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rounds:3})});out.textContent=JSON.stringify(await r.json(),null,2);await refreshVisibility()}
async function inspectJob(jid){out.textContent='Inspecting job '+jid+'...';let r=await fetch('/api/jobs/'+encodeURIComponent(jid)+'/inspect',{method:'POST'});out.textContent=JSON.stringify(await r.json(),null,2);await refreshVisibility()}
async function healAiServices(){let r=await fetch('/api/ai-services/heal',{method:'POST'});out.textContent=JSON.stringify(await r.json(),null,2);await refreshVisibility()}
async function toggleBoostMode(){let r=await fetch('/api/boost-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!boostActive})});out.textContent=JSON.stringify(await r.json(),null,2);await refreshVisibility()}
approvals();refreshJobs();refreshHealth();refreshSwarm();refreshCal();scRefresh();refreshVisibility();setInterval(refreshHealth,10000);setInterval(refreshJobs,15000);setInterval(refreshSwarm,4000);setInterval(refreshVisibility,12000)</script></body></html>"""


@app.post("/api/read/{action}")
def read_action(action: str, body: ActionBody):
    try:
        return runtime.run_read(action, body.payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/draft/post")
def draft_post(body: DraftBody):
    if not body.topic and not body.content:
        raise HTTPException(400, "topic or content is required")
    try:
        result = runtime.draft_post(body.topic, body.content)
        response = {"draft": result, "approval": None}
        content = result.get("post_content")
        if result.get("status") == "drafted_for_review" and content:
            response["approval"] = runtime.request_live_action("publish_post", {"content": content})
        return response
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/approvals")
def request_approval(body: ApprovalBody):
    try:
        return runtime.request_live_action(body.action, body.payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/approvals")
def approvals(status: str | None = None):
    return {"approvals": runtime.store.list(status=status)}


@app.get("/api/operations")
def operations(status: str | None = None, limit: int = 100):
    return {"operations": runtime.store.operations(status=status, limit=limit)}


@app.get("/api/operations/{operation_id}")
def operation(operation_id: str):
    value = runtime.store.operation(operation_id)
    if not value:
        raise HTTPException(404, "Operation not found")
    return value


@app.get("/api/reports/health")
def run_health_report():
    from .health_report import health_report
    return health_report(scheduler.store)


@app.get("/api/reports/linkedin")
def run_linkedin_performance_report(days: int = 30):
    from .performance_report import linkedin_performance_report
    try:
        return linkedin_performance_report(runtime, {"days": days})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _summarize_operation_row(row: dict) -> dict:
    import datetime as dt
    res = row.get("result") if isinstance(row.get("result"), dict) else {}
    feed = res.get("feed") if isinstance(res.get("feed"), dict) else res
    rr = res.get("round_robin") if isinstance(res.get("round_robin"), dict) else {}
    kind = str(row.get("kind") or "unknown")
    if rr.get("executed_task"):
        kind = f"{kind}->{rr['executed_task']}"
    status = str(row.get("status") or "unknown")
    err = row.get("error") or feed.get("error") or feed.get("reason")
    scanned = int(feed.get("posts_scanned") or 0)
    attempted = int(feed.get("actions_attempted") or 0)
    jobs_disc = feed.get("jobs_discovered")
    decisions = feed.get("decisions") if isinstance(feed.get("decisions"), list) else []
    verified = bool(
        (row.get("linkedin_activity_id") or row.get("linkedin_url") or feed.get("linkedin_activity_id") or feed.get("linkedin_url"))
        and (row.get("verified_text_hash") or feed.get("verified_text_hash"))
    )
    if err:
        summary = f"Blocked/Error: {str(err)[:140]}"
    elif jobs_disc is not None:
        summary = f"Jobs discovered={jobs_disc}, staged={feed.get('jobs_staged', 0)} (query: {feed.get('query', '')})"
    elif feed.get("task") == "cybersecurity_scout" or "cybersecurity" in kind:
        summary = f"Cyber scout: scanned={feed.get('posts_scanned', 0)}, staged={len(feed.get('staged_approvals', []))}"
    elif attempted > 0:
        summary = f"{'VERIFIED' if verified else 'Attempted'} action ({attempted} attempted, {scanned} posts scanned)"
    elif scanned > 0:
        skip_reasons = [str(d.get("reason") or d.get("action") or "skip") for d in decisions[:3] if isinstance(d, dict)]
        reason_str = ", ".join(skip_reasons) if skip_reasons else "no qualifying posts above threshold"
        summary = f"Scanned {scanned} posts · 0 actions ({reason_str[:110]})"
    else:
        summary = f"Completed ({status}) · no external write"
    stamp = dt.datetime.fromtimestamp(float(row.get("created") or 0)).strftime("%m-%d %H:%M:%S")
    return {
        "id": row.get("id"),
        "time": stamp,
        "kind": kind,
        "status": status,
        "verified": verified,
        "summary": summary,
    }


@app.get("/api/reports/visibility")
def run_visibility_report():
    from tools.linkedin_rate_limiter import LinkedInRateLimiter
    from tools.ai_service_manager import AIServiceManager
    from tools.universal_rsi_reflector import UniversalRsiReflector
    ops = runtime.store.operations(limit=30)
    rot = runtime.store.get_rotation_state() if hasattr(runtime.store, "get_rotation_state") else {}
    funnel = runtime.store.job_funnel_counts() if hasattr(runtime.store, "job_funnel_counts") else {}
    jobs = runtime.store.list_job_applications(limit=25) if hasattr(runtime.store, "list_job_applications") else []
    quotas = LinkedInRateLimiter.get_instance().get_status_summary()
    ai_health = AIServiceManager().health_and_recover(heal=False)
    rsi_status = UniversalRsiReflector(social_db_path=runtime.store.path).status_summary()
    x_creator_status = {}
    try:
        from tools.x_creator_monetization_engine import XCreatorMonetizationEngine
        x_creator_status = XCreatorMonetizationEngine(db_path=runtime.store.path).status_summary()
    except Exception:
        x_creator_status = {}
    boost_on = os.environ.get("SABRINA_LINKEDIN_24_7", "").strip().lower() in ("1", "true", "yes")
    return {
        "status": "ok",
        "boost_24_7": boost_on,
        "round_robin": rot,
        "job_funnel": funnel,
        "recent_jobs": jobs,
        "quotas": quotas,
        "ai_services": ai_health,
        "rsi": rsi_status,
        "x_creator": x_creator_status,
        "recent_activity": [_summarize_operation_row(o) for o in ops[:25]],
    }


@app.get("/api/rsi/status")
def get_rsi_status():
    from tools.universal_rsi_reflector import UniversalRsiReflector
    return UniversalRsiReflector(social_db_path=runtime.store.path).status_summary()


@app.post("/api/rsi/run")
def run_rsi_cycle(payload: dict[str, Any] | None = None):
    rounds = int((payload or {}).get("rounds", 3))
    return runtime.run_read("rsi_reflect", {"rounds": rounds})


@app.get("/api/jobs")
def list_jobs(status: str | None = None, limit: int = 100):
    return {
        "funnel": runtime.store.job_funnel_counts(),
        "jobs": runtime.store.list_job_applications(status=status, limit=limit),
    }


@app.post("/api/jobs/discover")
def trigger_jobs_discover(payload: dict[str, Any] | None = None):
    try:
        return runtime.run_read("jobs_discover", payload or {"max_jobs": 6, "easy_apply_only": True})
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/jobs/{job_id}/inspect")
def trigger_job_inspect(job_id: str):
    try:
        return runtime.run_read("jobs_inspect", {"job_id": job_id})
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/jobs/apply")
def trigger_job_apply(payload: dict[str, Any] | None = None):
    try:
        return runtime.run_read("jobs_apply", payload or {})
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/ai-services/heal")
def trigger_ai_services_heal():
    return runtime.run_read("ai_services_health", {"heal": True})


@app.post("/api/boost-mode")
def toggle_boost_mode(payload: dict[str, Any]):
    enabled = bool((payload or {}).get("enabled", True))
    os.environ["SABRINA_LINKEDIN_24_7"] = "1" if enabled else "0"
    os.environ["SABRINA_LINKEDIN_START_HOUR"] = "6" if enabled else "8"
    os.environ["SABRINA_LINKEDIN_END_HOUR"] = "23" if enabled else "21"
    return {
        "status": "updated",
        "boost_24_7": enabled,
        "SABRINA_LINKEDIN_24_7": os.environ["SABRINA_LINKEDIN_24_7"],
        "operating_window": "24/7 split-duty (06:00-23:59 browser + 24h offline/Tor)" if enabled else "08:00-21:00 standard",
    }


class ScheduleBody(BaseModel):
    capability: str
    payload: dict[str, Any] = Field(default_factory=dict)
    next_run: float | None = None
    interval_min: int | None = None
    interval_max: int | None = None
    max_attempts: int = 3
    draft: bool = False
    approved_at_schedule: bool = False


@app.get("/api/schedule")
def schedule_list(status: str | None = None, limit: int = 100):
    return {"jobs": scheduler.store.list(status=status, limit=limit)}


@app.get('/api/kraken/research')
def kraken_research_status():
    from tools.kraken_director_store import ResearchStore
    return {'programs': ResearchStore(scheduler.store).status()}


@app.post('/api/kraken/research/{program_id}/{action}')
def kraken_research_control(program_id: str, action: str):
    from tools.kraken_director_store import ResearchStore
    try:
        return ResearchStore(scheduler.store).control(program_id, action)
    except KeyError as exc:
        raise HTTPException(404, 'research program not found') from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/schedule")
def schedule_create(body: ScheduleBody):
    try:
        if body.capability in {"linkedin.publish_post", "linkedin.reshare"}:
            if body.draft:
                raise ValueError("Live LinkedIn shares cannot be scheduled as drafts")
            return scheduler.schedule_approved_social_action(
                runtime, body.capability, body.payload, body.next_run,
                body.interval_min, body.interval_max, body.max_attempts,
                approved_at_schedule=body.approved_at_schedule,
            )
        return scheduler.schedule(body.capability, body.payload, body.next_run,
                                  body.interval_min, body.interval_max, body.max_attempts, body.draft)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/schedule/dispatch")
def schedule_dispatch():
    return {"job": scheduler.dispatch_once()}


@app.post("/api/schedule/{job_id}/{action}")
def schedule_action(job_id: str, action: str):
    try:
        if action == "cancel": return scheduler.cancel(job_id)
        if action == "pause": return scheduler.pause(job_id)
        if action == "resume": return scheduler.resume(job_id)
        if action == "run-now":
            return scheduler.run_now(job_id)
        raise ValueError("unsupported schedule action")
    except KeyError as exc:
        raise HTTPException(404, "job not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/approvals/{request_id}/approve")
def approve(request_id: str):
    try:
        return runtime.approve(request_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/approvals/{request_id}/reject")
def reject(request_id: str):
    if not runtime.store.reject(request_id):
        raise HTTPException(409, "Approval does not exist or is no longer pending")
    return runtime.store.get(request_id)


class ResourceSweepBody(BaseModel):
    channels: list[str] | None = None
    arms_per_sweep: int = 4
    max_probes_per_arm: int = 6
    reverify_limit: int = 4


class ResourceUseBody(BaseModel):
    resource_id: str
    max_chars: int = 4000


@app.get("/api/resources/index")
def get_internet_resource_index(query: str = "", kind: str = "all", limit: int = 20):
    from tools.internet_resource_indexer import InternetResourceIndexer
    indexer = InternetResourceIndexer(social_db_path=runtime.store.path)
    return {
        "ok": True,
        "summary": indexer.status_summary(),
        "resources": indexer.search(query=query, resource_kind=kind, limit=limit),
    }


@app.post("/api/resources/sweep")
def trigger_internet_resource_sweep(body: ResourceSweepBody):
    return runtime.run_read(
        "internet_resource_sweep",
        {
            "channels": body.channels,
            "arms_per_sweep": body.arms_per_sweep,
            "max_probes_per_arm": body.max_probes_per_arm,
            "reverify_limit": body.reverify_limit,
        },
    )


@app.post("/api/resources/use")
def use_internet_resource(body: ResourceUseBody):
    from tools.internet_resource_indexer import InternetResourceIndexer
    indexer = InternetResourceIndexer(social_db_path=runtime.store.path)
    res = indexer.use_resource(body.resource_id, max_chars=max(200, min(16000, int(body.max_chars))))
    if not res.get("ok"):
        raise HTTPException(404 if res.get("error") == "resource_not_found" else 502, res.get("error", "use_resource_failed"))
    return res


class CompressionRunBody(BaseModel):
    corpus_name: str = "enwik8_100k"
    allow_download: bool = True
    force_cpu: bool = False


@app.get("/api/compression/leaderboard")
def get_compression_leaderboard(limit: int = 15):
    from tools.neural_compressor_lab import NeuralCompressorLab
    lab = NeuralCompressorLab()
    return {"ok": True, **lab.get_leaderboard(limit=limit)}


@app.post("/api/compression/run")
def trigger_compression_experiment(body: CompressionRunBody):
    return runtime.run_read(
        "compression_experiment",
        {
            "corpus_name": body.corpus_name,
            "allow_download": body.allow_download,
            "force_cpu": body.force_cpu,
        },
    )


class ExploitGymRsiBody(BaseModel):
    model: str = "qwen2.5-coder:14b"
    max_turns: int = 16
    timeout_seconds: int = 240
    run_replay: bool = True
    recurring_hours: int | None = None


class SabrinaPromptBody(BaseModel):
    prompt: str
    execute: bool = True


@app.get("/api/exploitgym/status")
def get_exploitgym_status():
    from tools.coder_agent.exploitgym_rsi_controller import ExploitGymRSIController
    ctrl = ExploitGymRSIController()
    with scheduler.store.db() as db:
        rows = db.execute(
            "SELECT * FROM scheduled_jobs WHERE capability = 'exploitgym.rsi_cycle' ORDER BY updated DESC LIMIT 8"
        ).fetchall()
    jobs = []
    for raw in rows:
        j = scheduler.store._decode(raw)
        jobs.append({
            "id": j["id"],
            "status": j["status"],
            "next_run": j["next_run"],
            "updated": j["updated"],
            "last_error": j.get("last_error"),
            "result": j.get("result"),
        })
    return {"ok": True, **ctrl.coverage_summary(), "scheduler_jobs": jobs}


@app.post("/api/exploitgym/rsi-cycle")
def queue_exploitgym_rsi_cycle(body: ExploitGymRsiBody):
    payload = {
        "model": body.model,
        "max_turns": max(4, min(48, int(body.max_turns))),
        "timeout_seconds": max(60, min(900, int(body.timeout_seconds))),
        "run_replay": bool(body.run_replay),
    }
    iv_min = int(body.recurring_hours) * 3600 if body.recurring_hours else None
    iv_max = (iv_min + 3600) if iv_min else None
    job = scheduler.schedule(
        "exploitgym.rsi_cycle",
        payload,
        next_run=time.time(),
        interval_min=iv_min,
        interval_max=iv_max,
        max_attempts=1,
    )
    return {
        "ok": True,
        "dispatched": True,
        "capability": "exploitgym.rsi_cycle",
        "job_id": job["id"],
        "status": job["status"],
        "payload": payload,
    }


@app.post("/api/sabrina/prompt")
def sabrina_autonomous_prompt(body: SabrinaPromptBody):
    """Natural-language autonomous prompt router: Sabrina plans and executes her own capabilities."""
    text = (body.prompt or "").strip()
    if not text:
        raise HTTPException(400, "prompt is required")
    low = text.lower()
    mission = assemble_mission(text, capability_registry, scheduler)
    is_status_only = any(k in low for k in ("status", "how many", "progress", "leaderboard", "what resources", "list ")) and not any(
        k in low for k in ("run ", "start", "solve", "improve", "train", "sweep", "execute", "launch", "cycle", "build", "schedule", "beat")
    )
    is_recurring = any(k in low for k in ("every ", "continuously", "schedule", "recurring", "keep running"))

    actions: list[dict[str, Any]] = []
    caps = [row["name"] for row in mission.get("registered", [])]

    if "exploitgym.rsi_cycle" in caps:
        if is_status_only or not body.execute:
            actions.append({"capability": "exploitgym.rsi_cycle", "mode": "status", "data": get_exploitgym_status()})
        else:
            run_replay = "no replay" not in low and "single pass" not in low
            job_res = queue_exploitgym_rsi_cycle(
                ExploitGymRsiBody(
                    model="qwen2.5-coder:14b",
                    max_turns=16,
                    timeout_seconds=240,
                    run_replay=run_replay,
                    recurring_hours=6 if is_recurring else None,
                )
            )
            actions.append({"capability": "exploitgym.rsi_cycle", "mode": "queued_autonomous_job", "data": job_res})

    if "compression.autonomous_experiment" in caps:
        if is_status_only or not body.execute:
            actions.append({"capability": "compression.autonomous_experiment", "mode": "status", "data": get_compression_leaderboard(limit=10)})
        else:
            job = scheduler.schedule(
                "compression.autonomous_experiment",
                {"corpus_name": "enwik8_100k", "allow_download": True},
                next_run=time.time(),
                interval_min=21600 if is_recurring else None,
                interval_max=25200 if is_recurring else None,
                max_attempts=1,
            )
            actions.append({"capability": "compression.autonomous_experiment", "mode": "queued_autonomous_job", "data": {"job_id": job["id"], "status": job["status"]}})

    if "resources.internet_index_sweep" in caps:
        if is_status_only or not body.execute:
            actions.append({"capability": "resources.internet_index_sweep", "mode": "status", "data": get_internet_resource_index(limit=15)})
        else:
            job = scheduler.schedule(
                "resources.internet_index_sweep",
                {"arms_per_sweep": 8, "max_probes_per_arm": 12, "reverify_limit": 4},
                next_run=time.time(),
                interval_min=2700 if is_recurring else None,
                interval_max=3600 if is_recurring else None,
                max_attempts=1,
            )
            actions.append({"capability": "resources.internet_index_sweep", "mode": "queued_autonomous_job", "data": {"job_id": job["id"], "status": job["status"]}})

    if "system.self_repair_workshop" in caps:
        if is_status_only or not body.execute:
            actions.append({"capability": "system.self_repair_workshop", "mode": "status", "data": get_self_repair_status()})
        else:
            auto_app = not any(k in low for k in ("stage only", "stage-only", "dry run", "dry-run"))
            job = scheduler.schedule(
                "system.self_repair_workshop",
                {"max_tickets": 1, "auto_apply": auto_app},
                next_run=time.time(),
                interval_min=1800 if is_recurring else None,
                interval_max=2700 if is_recurring else None,
                max_attempts=1,
            )
            actions.append({"capability": "system.self_repair_workshop", "mode": "queued_autonomous_job", "data": {"job_id": job["id"], "status": job["status"], "auto_apply": auto_app}})

    if "system.compute_federation" in caps:
        from tools.sabrina_compute_federation import SabrinaComputeFederation
        fed = SabrinaComputeFederation()
        wants_mesh_rec = any(k in low for k in ("nanoquant", "littlebit", "bitnet", "recommend", "preference", "ultra_low_bit", "8gb", "16gb"))
        wants_mesh_cli = any(k in low for k in ("rpc-spec", "resolve-gguf", "quants", "sabrina-mesh cli"))
        if wants_mesh_cli:
            if "rpc-spec" in low:
                cli_res = fed.run_sabrina_mesh_cli(["rpc-spec", "--model", "qwen3-30b-a3b"])
            elif "resolve-gguf" in low:
                cli_res = fed.run_sabrina_mesh_cli(["resolve-gguf", "--model", "qwen2.5-coder:14b"])
            else:
                cli_res = fed.run_sabrina_mesh_cli(["quants"])
            actions.append({"capability": "system.compute_federation", "mode": "executed_live", "operation": "sabrina_mesh_cli", "data": cli_res})
        elif wants_mesh_rec:
            prof = "8gb" if "8gb" in low or "8 gb" in low else ("16gb" if "16gb" in low or "16 gb" in low else ("cpu" if "cpu" in low else "live"))
            pref = (
                "ultra_low_bit_frontier" if any(k in low for k in ("nanoquant", "littlebit", "bitnet", "low-bit", "low bit", "frontier"))
                else ("max_quality" if "quality" in low else ("max_speed" if "speed" in low else "all_profiles"))
            )
            rec_res = fed.mesh_recommend(preference=pref, profile=prof, ctx_len=4096, top_k=3)
            actions.append({"capability": "system.compute_federation", "mode": "executed_live", "operation": f"mesh_recommend({prof},{pref})", "data": rec_res})
        elif is_status_only or not body.execute:
            actions.append({"capability": "system.compute_federation", "mode": "status", "data": get_federation_status(probe=False)})
        else:
            job = scheduler.schedule(
                "system.compute_federation",
                {"probe": True},
                next_run=time.time(),
                interval_min=1800 if is_recurring else None,
                interval_max=2700 if is_recurring else None,
                max_attempts=1,
            )
            actions.append({"capability": "system.compute_federation", "mode": "queued_autonomous_job", "data": {"job_id": job["id"], "status": job["status"]}})

    if "github.autonomous_repo_forge" in caps:
        import re as _re
        from tools.sabrina_upstream_forge import SabrinaUpstreamForge
        forge = SabrinaUpstreamForge()
        wants_token = any(k in low for k in ("generate token", "new token", "github token", "personal access token", "pat", "upgrade token"))
        if wants_token and body.execute and not is_status_only:
            tok_res = forge.browser_generate_github_token()
            actions.append({"capability": "github.autonomous_repo_forge", "mode": "executed_live", "operation": "generate_token", "data": tok_res})
        if (not wants_token or any(k in low for k in ("repo", "sabrina-mesh", "publish", "push", "create"))) and body.execute and not is_status_only:
            m_repo = _re.search(r"(?:CharlesMcGowen/)?([A-Za-z0-9_.-]*sabrina-mesh[A-Za-z0-9_.-]*)", text, _re.I)
            repo_name = m_repo.group(1) if m_repo else "sabrina-mesh"
            repo_res = forge.browser_create_github_repo(
                repo_name=repo_name,
                description="Sabrina-Mesh: Collective-Built Distributed Layer & MoE Sharding Planner + OpenAI Mesh Gateway",
                private=False,
                auto_push=True,
            )
            actions.append({"capability": "github.autonomous_repo_forge", "mode": "executed_live", "operation": "create_and_publish_repo", "data": repo_res})
        elif is_status_only or not body.execute:
            actions.append({"capability": "github.autonomous_repo_forge", "mode": "status", "data": {"dossiers": len(forge.list_dossiers())}})

    if "forge.daily_paper_to_code" in caps:
        from tools.sabrina_tool_forge import catalog, cycle, invoke
        if is_status_only or not body.execute:
            actions.append({"capability": "forge.daily_paper_to_code", "mode": "status", "data": {"ok": True, **catalog()}})
        elif "invoke" in low and "paper_" in low:
            import re as _re
            m_t = _re.search(r"(paper_[0-9]{4}_[0-9]{4,5}(?:v[1-9][0-9]*)?)", text)
            tname = m_t.group(1) if m_t else "paper_2611_26627"
            inv_res = invoke(tname, [[1, 1], [1, 0], [0, 0], [1, 1]])
            actions.append({"capability": "forge.daily_paper_to_code", "mode": "executed_live", "operation": f"invoke({tname})", "data": {"ok": True, **inv_res}})
        else:
            job = scheduler.schedule(
                "forge.daily_paper_to_code",
                {"operation": "cycle"},
                next_run=time.time(),
                interval_min=3600 if is_recurring else None,
                interval_max=5400 if is_recurring else None,
                max_attempts=1,
            )
            actions.append({"capability": "forge.daily_paper_to_code", "mode": "queued_autonomous_job", "data": {"job_id": job["id"], "status": job["status"]}})

    summary_parts = []
    for a in actions:
        if a["mode"] == "queued_autonomous_job":
            jid = a["data"].get("job_id")
            summary_parts.append(f"Dispatched `{a['capability']}` autonomously on ego.local scheduler (job `{jid}`).")
        elif a["mode"] == "executed_live":
            summary_parts.append(f"Executed `{a['capability']}` ({a.get('operation', 'live')}) via CDP/API: ok={a['data'].get('ok')}.")
        else:
            summary_parts.append(f"Retrieved live status for `{a['capability']}`.")
    if not summary_parts:
        summary_parts.append("No matching autonomous execution capability triggered; inspect mission plan or use Swarm Collective Chat.")

    return {
        "ok": True,
        "prompt": text,
        "mission": mission,
        "actions": actions,
        "summary": " ".join(summary_parts),
    }


class SelfRepairRunBody(BaseModel):
    max_tickets: int = 1
    auto_apply: bool = True


@app.get("/api/self-repair/status")
def get_self_repair_status(limit: int = 12):
    from tools.sabrina_self_repair_workshop import SabrinaSelfRepairWorkshop
    ws = SabrinaSelfRepairWorkshop()
    return {"ok": True, **ws.status_summary(limit=limit)}


@app.post("/api/self-repair/run")
def queue_self_repair_cycle(body: SelfRepairRunBody):
    job = scheduler.schedule(
        "system.self_repair_workshop",
        {"max_tickets": max(1, min(3, int(body.max_tickets))), "auto_apply": bool(body.auto_apply)},
        next_run=time.time(),
        max_attempts=1,
    )
    return {"ok": True, "job_id": job["id"], "status": job["status"], "auto_apply": bool(body.auto_apply)}


@app.post("/api/self-repair/{patch_id}/approve")
def approve_self_repair_patch(patch_id: str):
    from tools.sabrina_self_repair_workshop import SabrinaSelfRepairWorkshop
    res = SabrinaSelfRepairWorkshop().apply_patch(patch_id, approver="modern-console-operator")
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or res.get("status") or "apply_failed")
    return res


@app.post("/api/self-repair/{patch_id}/rollback")
def rollback_self_repair_patch(patch_id: str):
    from tools.sabrina_self_repair_workshop import SabrinaSelfRepairWorkshop
    res = SabrinaSelfRepairWorkshop().rollback_patch(patch_id)
    if not res.get("ok"):
        raise HTTPException(409, res.get("error") or "rollback_failed")
    return res


class FederationDispatchBody(BaseModel):
    code: str = ""
    snippets: list[str] = []
    target: str = "auto"
    timeout: int = 25


@app.get("/api/federation/status")
def get_federation_status(probe: bool = False):
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    return SabrinaComputeFederation().status_summary(probe=bool(probe))


@app.post("/api/federation/probe")
def probe_federation_nodes():
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    return SabrinaComputeFederation().status_summary(probe=True)


@app.post("/api/federation/dispatch")
def dispatch_federation_workload(body: FederationDispatchBody):
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    fed = SabrinaComputeFederation()
    if body.snippets:
        return fed.spread_parallel_python(body.snippets, timeout=max(5, min(60, int(body.timeout))))
    if body.code.strip():
        return fed.execute_python(body.code, target=body.target or "auto", timeout=max(5, min(60, int(body.timeout))))
    raise HTTPException(400, "Provide `code` or `snippets`")


class SabrinaMeshCliBody(BaseModel):
    argv: list[str] = Field(default_factory=lambda: ["recommend", "--profile", "24gb-mesh", "--preference", "ultra_low_bit_frontier"])


@app.get("/api/federation/mesh-recommend")
def get_federation_mesh_recommend(
    preference: str = "ultra_low_bit_frontier",
    profile: str = "live",
    ctx_len: int = 4096,
    top_k: int = 3,
):
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    return SabrinaComputeFederation().mesh_recommend(
        preference=preference,
        profile=profile,
        ctx_len=max(512, min(65536, int(ctx_len))),
        top_k=max(1, min(6, int(top_k))),
    )


@app.post("/api/federation/mesh-cli")
def run_federation_mesh_cli(body: SabrinaMeshCliBody):
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    return SabrinaComputeFederation().run_sabrina_mesh_cli(body.argv)


@app.get("/api/federation/rsi-impact")
def get_federation_rsi_impact():
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    return SabrinaComputeFederation().rsi_causal_impact_ledger()


class FederationEnrollNodeBody(BaseModel):
    node_id: str
    endpoint_url: str
    ollama_url: str = ""
    gpu_label: str = "Remote VPS / Mesh Node"
    kind: str = "remote_vps_tunnel"


@app.get("/api/federation/join-script")
def get_federation_join_script(
    node_id: str = "vps-node-01",
    target_os: str = "linux",
    mode: str = "ssh_reverse_tunnel",
    worker_port: int = 18097,
    ollama_tunnel_port: int = 11436,
    gpu_label: str = "Remote VPS / Mesh Node",
):
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    return SabrinaComputeFederation().generate_node_join_bundle(
        node_id=node_id,
        target_os=target_os,
        mode=mode,
        worker_port=worker_port,
        ollama_tunnel_port=ollama_tunnel_port,
        gpu_label=gpu_label,
    )


@app.post("/api/federation/enroll-node")
def enroll_federation_node(body: FederationEnrollNodeBody):
    from tools.sabrina_compute_federation import SabrinaComputeFederation
    res = SabrinaComputeFederation().enroll_owned_node(
        node_id=body.node_id,
        endpoint_url=body.endpoint_url,
        ollama_url=body.ollama_url,
        gpu_label=body.gpu_label,
        kind=body.kind,
    )
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "enroll_failed")
    return res


class PaperForgeCycleBody(BaseModel):
    retry_id: str | None = None


class PaperForgeInvokeBody(BaseModel):
    name: str
    rows: list[list[float]]


@app.get("/api/paper-forge/status")
def get_paper_forge_status():
    from tools.sabrina_tool_forge import catalog
    return {"ok": True, **catalog()}


@app.post("/api/paper-forge/cycle")
def run_paper_forge_cycle(body: PaperForgeCycleBody):
    from tools.sabrina_tool_forge import catalog, cycle
    res = cycle(retry_id=(body.retry_id or "").strip() or None)
    return {"ok": True, "cycle": res, "catalog": catalog()}


@app.post("/api/paper-forge/invoke")
def invoke_paper_forge_tool(body: PaperForgeInvokeBody):
    from tools.sabrina_tool_forge import invoke
    return {"ok": True, **invoke(body.name, body.rows)}


class DistributedWSPSynthesizeBody(BaseModel):
    model_alias: str = "mri-sabrina-ablated-3b"
    source_experiment: str = "mri-causal-20261115-fresh"
    write_manifest: bool = True


@app.get("/api/wsp/distributed-status")
def get_distributed_wsp_status(limit: int = 8):
    from tools.distributed_wsp_weight_generator import DistributedWSPWeightGenerator
    return DistributedWSPWeightGenerator().status_summary(limit=limit)


@app.post("/api/wsp/distributed-synthesize")
def run_distributed_wsp_synthesize(body: DistributedWSPSynthesizeBody):
    from tools.distributed_wsp_weight_generator import DistributedWSPWeightGenerator
    return DistributedWSPWeightGenerator().run_distributed_wsp_synthesis(
        model_alias=body.model_alias,
        source_experiment=body.source_experiment,
        write_manifest=bool(body.write_manifest),
    )


class BountyStageDossierBody(BaseModel):
    program: str = "hostgator-latam-bb"
    target_url: str = "https://financeiro.hostgator.com.br/api/v1/ai-agent/session"
    vuln_category: str = "idor"
    title: str = "IDOR in AI Agent Session Endpoint Allows Cross-Tenant Session Metadata Read"
    steps_to_reproduce: str = ""
    http_poc: str = ""
    impact: str = ""
    severity: str = ""
    use_llm_triager: bool = False


class BountyCdpPrefillBody(BaseModel):
    dossier_id: str
    cdp_url: str = "http://127.0.0.1:9222"


@app.get("/api/bounty/status")
def get_bounty_submitter_status(limit: int = 10):
    from tools.sabrina_bounty_submitter import SabrinaBountySubmitter
    return SabrinaBountySubmitter().status_summary(limit=limit)


@app.post("/api/bounty/stage-dossier")
def stage_bounty_submission_dossier(body: BountyStageDossierBody):
    from tools.sabrina_bounty_submitter import SabrinaBountySubmitter
    return SabrinaBountySubmitter().stage_submission_dossier(
        program=body.program,
        target_url=body.target_url,
        vuln_category=body.vuln_category,
        title=body.title,
        steps_to_reproduce=body.steps_to_reproduce,
        http_poc=body.http_poc,
        impact=body.impact,
        severity=body.severity,
        use_llm_triager=bool(body.use_llm_triager),
    )


@app.post("/api/bounty/prefill-cdp")
def prefill_bounty_submission_cdp(body: BountyCdpPrefillBody):
    from tools.sabrina_bounty_submitter import SabrinaBountySubmitter
    res = SabrinaBountySubmitter().prefill_platform_submission_via_cdp(
        dossier_id=body.dossier_id,
        cdp_url=body.cdp_url,
    )
    if not res.get("ok"):
        raise HTTPException(400, res.get("error") or "cdp_prefill_failed")
    return res


@app.get("/api/bounty/cve-hunter/status")
def get_cve_variant_hunter_status():
    from tools.sabrina_cve_variant_hunter import SabrinaCVEVariantHunter
    return SabrinaCVEVariantHunter().status()


@app.post("/api/bounty/cve-hunter/scan")
def run_cve_variant_hunter_scan():
    from tools.sabrina_cve_variant_hunter import SabrinaCVEVariantHunter
    return SabrinaCVEVariantHunter().run_full_scan(stage_to_bounty_submitter=True)


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Sabrina Social service")
    parser.add_argument("--host", default=os.environ.get("SABRINA_SOCIAL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SABRINA_SOCIAL_PORT", "8087")))
    args = parser.parse_args()
    uvicorn.run("sabrina_social.api:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
