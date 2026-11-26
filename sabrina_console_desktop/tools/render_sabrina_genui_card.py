"""Render Antigravity Generative UI cards for Sabrina Swarm Command Center,
RSI proposal review, desktop cursor bounding-box preview, and segregated competence telemetry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from tools.sabrina_console_swarm import get_live_swarm_state
from tools.sabrina_console_calendar import get_granular_calendar_data
from tools.sabrina_lab_assistant_mcp import (
    sabrina_get_competence_summary,
    sabrina_list_skill_proposals,
)


def collect_genui_payload() -> dict[str, Any]:
    """Gather live Swarm topology, proposals, and segregated competence data."""
    proposals = sabrina_list_skill_proposals()
    competence = sabrina_get_competence_summary()
    swarm = get_live_swarm_state()
    cal = get_granular_calendar_data()
    slim_proposals = []
    for p in proposals[:6]:
        slim_proposals.append(
            {
                "proposal_id": p.get("proposal_id", ""),
                "skill_name": p.get("skill_name", ""),
                "version": p.get("version", "1.0.0"),
                "sha256": p.get("sha256", ""),
                "installed_version": p.get("installed_version"),
                "installed_sha256": p.get("installed_sha256"),
                "status": p.get("status", "pending_approval"),
                "trigger_reason": p.get("trigger_reason", ""),
                "target_path": p.get("target_path", ""),
                "skill_content": str(p.get("skill_content", ""))[:800],
            }
        )
    return {
        "swarm": {
            "time_str": swarm.get("time_str", ""),
            "nodes": swarm.get("nodes", {}),
            "pending_actions": (swarm.get("pending_actions") or [])[:6],
            "objectives": (swarm.get("objectives") or [])[:5],
            "pipelines": (swarm.get("pipelines") or [])[:4],
            "long_horizon_projects": (swarm.get("long_horizon_projects") or [])[:8],
            "shadow_ab_trials": (swarm.get("shadow_ab_trials") or [])[:6],
            "epistemic_triggers": (swarm.get("epistemic_triggers") or [])[:6],
            "research_episodes": (swarm.get("research_episodes") or [])[:8],
            "external_guild_telemetry": swarm.get("external_guild_telemetry") or {},
            "activity_feed": (swarm.get("activity_feed") or [])[:12],
        },
        "calendar": {
            "selected_month_label": cal.get("selected_month_label", ""),
            "selected_week_label": cal.get("selected_week_label", ""),
            "selected_day_label": cal.get("selected_day_label", ""),
            "selected_hour_label": cal.get("selected_hour_label", ""),
            "month_grid": cal.get("month_grid", []),
            "week_columns": cal.get("week_columns", []),
            "day_hours": cal.get("day_hours", []),
            "hour_breakdown": cal.get("hour_breakdown", {}),
        },
        "proposals": slim_proposals,
        "competence": {
            "total_evaluated_runs": competence.get("total_evaluated_runs", 0),
            "skills": competence.get("skills", {}),
            "capabilities": competence.get("capabilities", {}),
            "category_counts": competence.get("category_counts", {}),
        },
    }


def render_inline_card_html(payload: dict[str, Any] | None = None) -> str:
    """Render a compact (<460px height) inline Generative UI widget for Antigravity chat."""
    data = payload or collect_genui_payload()
    data_json = json.dumps(data, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-3">
  <div class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-xl p-4 shadow-sm space-y-3">
    <div class="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] pb-2.5">
      <div>
        <div class="flex items-center gap-2">
          <span class="inline-block w-2 h-2 rounded-full bg-emerald-500"></span>
          <h2 class="text-[var(--foreground)] font-semibold text-base">Sabrina Swarm Command Center &amp; RSI Gate</h2>
        </div>
        <p class="text-[var(--muted-foreground)] text-xs">Live 5-Node Swarm Topology &bull; Plain-English Activity Feed &bull; SHA-256 Skill &amp; Cursor Gates</p>
      </div>
      <div class="flex items-center gap-1.5">
        <button id="tabBtnSwarm" onclick="switchTab('swarm')" class="px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--primary)] text-[var(--primary-foreground)]">Swarm Live</button>
        <button id="tabBtnProjects" onclick="switchTab('projects')" class="px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Projects &amp; Guilds</button>
        <button id="tabBtnCal" onclick="switchTab('calendar')" class="px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Calendar (M/W/D/H)</button>
        <button id="tabBtnProp" onclick="switchTab('proposal')" class="px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Proposal Gate</button>
        <button id="tabBtnCursor" onclick="switchTab('cursor')" class="px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Cursor Preview</button>
        <button id="tabBtnComp" onclick="switchTab('competence')" class="px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Competence</button>
      </div>
    </div>

    <!-- Tab 0: Swarm Live Topology & Plain-English Stream -->
    <div id="tabSwarm" class="space-y-2.5">
      <div id="swarmNodesGrid" class="grid grid-cols-5 gap-2"></div>
      <div class="grid grid-cols-2 gap-2">
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5">
          <div class="flex items-center justify-between mb-1">
            <span class="text-xs font-semibold text-[var(--foreground)]">Active Swarm Pipelines &amp; Objectives</span>
            <span class="text-xs text-[var(--muted-foreground)]">Codex &harr; Antigravity &harr; Claude</span>
          </div>
          <div id="swarmPipesList" class="text-xs font-mono space-y-1 max-h-36 overflow-y-auto"></div>
        </div>
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5">
          <div class="flex items-center justify-between mb-1">
            <span class="text-xs font-semibold text-[var(--foreground)]">Plain-English Live Activity Stream</span>
            <span id="swarmSyncTime" class="text-xs text-[var(--muted-foreground)]"></span>
          </div>
          <div id="swarmFeedList" class="text-xs font-mono space-y-1 max-h-36 overflow-y-auto"></div>
        </div>
      </div>
    </div>

    <!-- Tab 0.25: Project-Agnostic Long-Horizon Projects & External Portal Guilds -->
    <div id="tabProjects" class="hidden space-y-2.5">
      <div class="grid grid-cols-3 gap-2">
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2">
          <div class="text-xs text-[var(--muted-foreground)]">Active / Recent Long-Horizon Projects</div>
          <div id="projCountBadge" class="text-sm font-semibold text-emerald-400 mt-0.5">0 Discovered from DB</div>
        </div>
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2">
          <div class="text-xs text-[var(--muted-foreground)]">External Zero-Auth Portal Guilds</div>
          <div id="portalFleetBadge" class="text-sm font-semibold text-sky-400 mt-0.5">0 Registered / 0 Active</div>
        </div>
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2">
          <div class="text-xs text-[var(--muted-foreground)]">D&amp;D / RSI Research Episodes</div>
          <div id="episodeCountBadge" class="text-sm font-semibold text-violet-400 mt-0.5">0 Episodes in Blackboard</div>
        </div>
      </div>
      <div class="grid grid-cols-2 gap-2">
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5">
          <div class="text-xs font-semibold text-[var(--foreground)] mb-1">Auto-Discovered Long-Horizon Projects &amp; Epoch Ledgers</div>
          <div id="projectsList" class="text-xs font-mono space-y-1.5 max-h-40 overflow-y-auto"></div>
        </div>
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5">
          <div class="text-xs font-semibold text-[var(--foreground)] mb-1">Recent External Portal Guild Dispatches &amp; Episodes</div>
          <div id="externalDispatchesList" class="text-xs font-mono space-y-1 max-h-40 overflow-y-auto"></div>
        </div>
      </div>
    </div>

    <!-- Tab 0.5: Granular Time-Block Calendar (Month / Week / Day / Hour) -->
    <div id="tabCalendar" class="hidden space-y-2.5">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-1.5">
          <button id="calModeMonth" onclick="renderCalMode('month')" class="px-2 py-0.5 rounded text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Month</button>
          <button id="calModeWeek" onclick="renderCalMode('week')" class="px-2 py-0.5 rounded text-xs font-medium bg-[var(--primary)] text-[var(--primary-foreground)]">Week</button>
          <button id="calModeDay" onclick="renderCalMode('day')" class="px-2 py-0.5 rounded text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Day (24h)</button>
          <button id="calModeHour" onclick="renderCalMode('hour')" class="px-2 py-0.5 rounded text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]">Hour (60m)</button>
        </div>
        <span id="calPeriodTitle" class="text-xs font-mono text-emerald-400 font-semibold"></span>
      </div>
      <div id="calViewport" class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5 max-h-48 overflow-y-auto text-xs font-mono"></div>
    </div>

    <!-- Tab 1: Proposal Approval Gate -->
    <div id="tabProposal" class="hidden space-y-2.5">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-2">
          <label class="text-xs text-[var(--muted-foreground)]">Proposal:</label>
          <select id="propSelect" onchange="renderProposal()" class="bg-[var(--background)] text-[var(--foreground)] border border-[var(--border)] rounded px-2 py-1 text-xs"></select>
        </div>
        <div class="flex items-center gap-2 text-xs">
          <span id="propVersionBadge" class="px-2 py-0.5 rounded bg-[var(--secondary)] text-[var(--secondary-foreground)] font-mono"></span>
          <span id="propStatusBadge" class="px-2 py-0.5 rounded font-medium"></span>
        </div>
      </div>

      <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5 space-y-1.5">
        <div class="flex items-center justify-between text-xs">
          <span class="text-[var(--muted-foreground)] font-medium">Expected 64-char SHA-256 (Mandatory Gate):</span>
          <span id="hashMatchStatus" class="font-mono text-xs text-emerald-500">EXACT MATCH VERIFIED</span>
        </div>
        <input id="shaInput" oninput="verifyHashInput()" type="text" class="w-full bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded px-2 py-1 font-mono text-xs" />
        <pre id="propDiffPreview" class="text-xs font-mono text-[var(--muted-foreground)] overflow-x-auto max-h-24 p-1.5 bg-[var(--card)] rounded border border-[var(--border)]"></pre>
      </div>

      <div class="flex flex-wrap items-center justify-between gap-2 pt-1">
        <div class="text-xs text-[var(--muted-foreground)]">
          Activation: <strong class="text-[var(--foreground)]">Approve in Console &lsquo;Swarm Control&rsquo; tab or http://127.0.0.1:8087</strong>
        </div>
        <div class="flex items-center gap-2">
          <button id="approveBtn" onclick="triggerApprove()" class="px-3 py-1.5 rounded-md text-xs font-semibold bg-[var(--primary)] text-[var(--primary-foreground)] hover:opacity-90">Approve &amp; Promote (SHA-256)</button>
        </div>
      </div>
      <div id="mcpCallOutput" class="hidden text-xs font-mono bg-[var(--background)] border border-[var(--border)] rounded p-2 text-[var(--foreground)] break-all"></div>
    </div>

    <!-- Tab 2: Desktop Cursor Preview -->
    <div id="tabCursor" class="hidden space-y-2.5">
      <div class="flex flex-wrap items-center justify-between gap-2 text-xs">
        <div class="flex items-center gap-2">
          <span class="text-[var(--muted-foreground)]">Target App:</span>
          <span class="font-mono px-2 py-0.5 rounded bg-[var(--secondary)] text-[var(--secondary-foreground)]">Antigravity.exe (w-1042)</span>
          <span class="text-[var(--muted-foreground)]">DPI:</span>
          <span class="font-mono">96 (1.0x Physical)</span>
        </div>
        <label class="flex items-center gap-1.5 cursor-pointer">
          <input id="physPermToggle" type="checkbox" onchange="updateCursorPreview()" class="rounded" />
          <span class="font-mono text-xs">--desktop-permission physical_input</span>
        </label>
      </div>

      <div id="cursorCanvasWrap" onclick="moveCursorTarget(event)" class="relative h-36 bg-[var(--background)] border border-[var(--border)] rounded-lg overflow-hidden cursor-crosshair select-none p-3">
        <div class="text-xs text-[var(--muted-foreground)]">Simulated Window Frame (Click anywhere to preview DPI-gated pointer target)</div>
        <div class="mt-3 inline-block px-2.5 py-1 rounded border border-[var(--border)] bg-[var(--card)] text-xs">Safe UI Button (x:140, y:72)</div>
        <div class="mt-3 ml-4 inline-block px-2.5 py-1 rounded border border-rose-500/50 bg-rose-500/10 text-xs text-rose-400">Always Allow (Protected Control)</div>
        <div id="cursorBox" style="left: 128px; top: 60px; width: 24px; height: 24px;" class="absolute border-2 border-emerald-500 bg-emerald-500/20 rounded pointer-events-none transition-all duration-150 flex items-center justify-center">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span>
        </div>
      </div>

      <div id="cursorGateStatus" class="text-xs font-mono bg-[var(--background)] border border-[var(--border)] rounded p-2"></div>
    </div>

    <!-- Tab 3: Segregated Competence Telemetry -->
    <div id="tabCompetence" class="hidden space-y-2.5">
      <div class="grid grid-cols-3 gap-2">
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5">
          <div class="text-xs text-[var(--muted-foreground)]">Live Verified Competence</div>
          <div id="liveCompValue" class="text-base font-semibold text-[var(--foreground)] mt-0.5">0/0 (No live passes)</div>
          <div class="text-xs text-[var(--muted-foreground)]">Excludes all simulations</div>
        </div>
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5">
          <div class="text-xs text-[var(--muted-foreground)]">Segregated Simulations</div>
          <div id="simCompValue" class="text-base font-semibold text-[var(--foreground)] mt-0.5">0/1 simulations</div>
          <div class="text-xs text-[var(--muted-foreground)]">is_simulation = 1</div>
        </div>
        <div class="bg-[var(--background)] border border-[var(--border)] rounded-lg p-2.5">
          <div class="text-xs text-[var(--muted-foreground)]">Env / Harness Blocks</div>
          <div id="blockCompValue" class="text-base font-semibold text-[var(--foreground)] mt-0.5">Tracked Separately</div>
          <div class="text-xs text-[var(--muted-foreground)]">Never counted as pass</div>
        </div>
      </div>
      <div id="skillTableWrap" class="max-h-32 overflow-y-auto border border-[var(--border)] rounded-lg bg-[var(--background)] p-2 text-xs font-mono space-y-1"></div>
    </div>
  </div>

  <script>
    const PAYLOAD = {data_json};
    let cursorPos = {{ x: 140, y: 72 }};

    function switchTab(tab) {{
      const tabs = ['swarm', 'projects', 'calendar', 'proposal', 'cursor', 'competence'];
      const map = {{
        swarm: ['tabSwarm', 'tabBtnSwarm'],
        projects: ['tabProjects', 'tabBtnProjects'],
        calendar: ['tabCalendar', 'tabBtnCal'],
        proposal: ['tabProposal', 'tabBtnProp'],
        cursor: ['tabCursor', 'tabBtnCursor'],
        competence: ['tabCompetence', 'tabBtnComp']
      }};
      tabs.forEach(t => {{
        const [panelId, btnId] = map[t];
        const panel = document.getElementById(panelId);
        const btn = document.getElementById(btnId);
        if (t === tab) {{
          panel.classList.remove('hidden');
          btn.className = 'px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--primary)] text-[var(--primary-foreground)]';
        }} else {{
          panel.classList.add('hidden');
          btn.className = 'px-2.5 py-1 rounded-md text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]';
        }}
      }});
    }}

    function initProjectsAndGuilds() {{
      const s = PAYLOAD.swarm || {{}};
      const projs = s.long_horizon_projects || [];
      const eps = s.research_episodes || [];
      const ext = s.external_guild_telemetry || {{}};
      document.getElementById('projCountBadge').textContent = `${{projs.length}} Active / Recent in agent_runtime.sqlite3`;
      document.getElementById('portalFleetBadge').textContent = `${{ext.registered_portals || 0}} Reg · ${{ext.qualified_active_portals || 0}} Active · ${{ext.full_delivery_portals || 0}} Delivering`;
      document.getElementById('episodeCountBadge').textContent = `${{eps.length}} Episodes in dnd_rsi_blackboard.sqlite3`;

      const pList = document.getElementById('projectsList');
      pList.innerHTML = '';
      if (!projs.length) {{
        pList.innerHTML = '<div class="text-[var(--muted-foreground)]">No long-horizon tasks recorded yet in agent_runtime.sqlite3. Any new task or epoch automatically renders here.</div>';
      }} else {{
        projs.forEach(p => {{
          const card = document.createElement('div');
          card.className = 'border border-[var(--border)] rounded p-1.5 bg-[var(--card)]';
          const lemmas = (p.verified_lemmas || []).length;
          const deads = (p.falsified_dead_ends || []).length;
          const gCount = (p.guild_children || []).length;
          const fams = (p.guild_children || []).map(c => c.family).join(', ');
          card.innerHTML = `
            <div class="flex justify-between items-center">
              <span class="font-semibold text-emerald-400">[${{p.state}}] Epoch ${{p.epoch_count}} &bull; Task ${{String(p.task_id).slice(0, 8)}}</span>
              <span class="text-[10px] text-[var(--muted-foreground)]">${{p.created_str || ''}}</span>
            </div>
            <div class="text-[var(--foreground)] mt-0.5">${{String(p.goal || '').slice(0, 110)}}</div>
            <div class="text-[10px] text-sky-400 mt-0.5">Verified Lemmas: ${{lemmas}} | Dead-Ends Falsified: ${{deads}} | Guild Workers: ${{gCount}} ${{fams ? '(' + fams + ')' : ''}}</div>
            ${{p.next_frontier ? `<div class="text-[10px] text-amber-400 truncate mt-0.5">Next Frontier: ${{p.next_frontier}}</div>` : ''}}
          `;
          pList.appendChild(card);
        }});
      }}

      const eList = document.getElementById('externalDispatchesList');
      eList.innerHTML = '';
      (s.shadow_ab_trials || []).forEach(tr => {{
        const row = document.createElement('div');
        row.className = 'border border-emerald-500/40 rounded p-1.5 bg-emerald-500/10 mb-1';
        row.innerHTML = `<div class="font-semibold text-emerald-300">[24H SHADOW A/B &bull; ${{tr.status}}] ${{tr.label}} (${{tr.remaining_hours}}h left)</div><div class="text-[10px] text-[var(--foreground)]">Challenger: ${{tr.challenger_net_bps >= 0 ? '+' : ''}}${{tr.challenger_net_bps}} bps vs Champion: ${{tr.champion_net_bps >= 0 ? '+' : ''}}${{tr.champion_net_bps}} bps (${{tr.frames_observed}} live frames)</div>`;
        eList.appendChild(row);
      }});
      (s.system_gates || []).forEach(gt => {{
        const row = document.createElement('div');
        const clr = gt.severity === 'critical' ? 'text-rose-400 border-rose-500/40 bg-rose-500/10' : (gt.anomaly_active ? 'text-amber-300 border-amber-500/30 bg-amber-500/10' : 'text-emerald-300 border-emerald-500/30 bg-emerald-500/5');
        row.className = `border rounded p-1 mb-1 ${{clr}}`;
        row.innerHTML = `<div class="font-semibold">[GATE: ${{gt.domain}} &bull; ${{gt.severity.toUpperCase()}}] ${{gt.gate_name}} (pings: ${{gt.ping_count}})</div><div class="text-[10px] text-[var(--foreground)]">${{gt.headline.slice(0, 105)}}</div>`;
        eList.appendChild(row);
      }});
      (s.epistemic_triggers || []).forEach(et => {{
        const row = document.createElement('div');
        row.className = 'border-b border-[var(--border)] pb-1 text-amber-300';
        row.textContent = `[SURPRISE: ${{et.kind}}] ${{et.summary.slice(0, 68)}} -> ${{et.resolution}}`;
        eList.appendChild(row);
      }});
      (ext.recent_dispatches || []).forEach(d => {{
        const row = document.createElement('div');
        row.className = 'border-b border-[var(--border)] pb-1';
        row.innerHTML = `<span class="${{d.status === 'ok' ? 'text-emerald-400' : 'text-amber-400'}}">[EXT ${{d.status.toUpperCase()}}]</span> ${{d.portal_id.slice(0, 34)}} &bull; ${{d.purpose}} (${{d.latency_ms}}ms · ${{d.ago}})`;
        eList.appendChild(row);
      }});
      eps.forEach(ep => {{
        const row = document.createElement('div');
        row.className = 'border-b border-[var(--border)] pb-1 text-violet-300';
        row.textContent = `[EPISODE ${{ep.status}}] ${{ep.challenge || ep.domain}}: ${{ep.goal.slice(0, 65)}} (score: ${{ep.score}})`;
        eList.appendChild(row);
      }});
    }}

    function renderCalMode(mode) {{
      const c = PAYLOAD.calendar || {{}};
      const btnIds = {{ month: 'calModeMonth', week: 'calModeWeek', day: 'calModeDay', hour: 'calModeHour' }};
      Object.entries(btnIds).forEach(([m, bid]) => {{
        const el = document.getElementById(bid);
        if (el) {{
          el.className = m === mode
            ? 'px-2 py-0.5 rounded text-xs font-medium bg-[var(--primary)] text-[var(--primary-foreground)]'
            : 'px-2 py-0.5 rounded text-xs font-medium bg-[var(--secondary)] text-[var(--secondary-foreground)]';
        }}
      }});
      const vp = document.getElementById('calViewport');
      const title = document.getElementById('calPeriodTitle');
      if (mode === 'month') {{
        title.textContent = c.selected_month_label || 'Month View';
        const cells = (c.month_grid || []).flat();
        vp.innerHTML = `<div class="grid grid-cols-7 gap-1">${{cells.map(d => `
          <div onclick="renderCalMode('day')" class="cursor-pointer p-1.5 rounded border ${{d.is_selected ? 'border-emerald-500 bg-[var(--card)]' : 'border-[var(--border)]'}} ${{d.in_month ? '' : 'opacity-40'}}">
            <div class="flex justify-between"><b>${{d.day}}</b><span class="text-emerald-400">${{d.scheduled_count}} (${{d.scheduled_hours}}h)</span></div>
            <div class="text-[10px] text-[var(--muted-foreground)] truncate">${{(d.preview || [])[0] || '—'}}</div>
          </div>`).join('')}}</div>`;
      }} else if (mode === 'week') {{
        title.textContent = c.selected_week_label || 'Week View';
        const cols = c.week_columns || [];
        let rowsHtml = '';
        [8, 9, 10, 12, 14, 15, 16, 18].forEach(hr => {{
          rowsHtml += `<div class="grid grid-cols-8 gap-1 border-b border-[var(--border)] py-1 items-center">
            <span class="text-[var(--muted-foreground)]">${{String(hr).padStart(2, '0')}}:00</span>
            ${{cols.map(col => {{
              const evs = (col.hours || {{}})[String(hr)] || [];
              return `<div onclick="renderCalMode('hour')" class="cursor-pointer truncate px-1 py-0.5 rounded ${{evs.length ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40' : 'text-[var(--muted-foreground)] opacity-40'}}">${{evs.length ? evs[0].title.slice(0, 12) : 'free'}}</div>`;
            }}).join('')}}
          </div>`;
        }});
        vp.innerHTML = `<div class="grid grid-cols-8 gap-1 font-bold border-b border-[var(--border)] pb-1 mb-1"><span>Hour</span>${{cols.map(x => `<span>${{x.label.slice(0, 6)}}</span>`).join('')}}</div>` + rowsHtml;
      }} else if (mode === 'day') {{
        title.textContent = c.selected_day_label || 'Day (24h) View';
        vp.innerHTML = (c.day_hours || []).map(h => {{
          const q = h.quarters || {{}};
          return `<div onclick="renderCalMode('hour')" class="cursor-pointer grid grid-cols-5 gap-1 border-b border-[var(--border)] py-1 items-center">
            <span class="font-bold">${{h.label}}</span>
            ${{[0, 15, 30, 45].map(m => {{
              const evs = q[String(m)] || [];
              return `<div class="truncate px-1 py-0.5 rounded ${{evs.length ? 'bg-blue-500/20 text-blue-300' : 'text-[var(--muted-foreground)] opacity-50'}}">${{evs.length ? `${{evs[0].title.slice(0, 14)}} (${{evs[0].duration_min}}m)` : `:${{String(m).padStart(2, '0')}} free`}}</div>`;
            }}).join('')}}
          </div>`;
        }}).join('');
      }} else {{
        title.textContent = c.selected_hour_label || 'Hour (60m) View';
        const blks = ((c.hour_breakdown || {{}}).five_minute_blocks) || [];
        vp.innerHTML = blks.map(b => `<div class="flex justify-between border-b border-[var(--border)] py-1">
          <span>${{b.label}}</span>
          <span class="${{b.events && b.events.length ? 'text-emerald-400 font-semibold' : 'text-[var(--muted-foreground)]'}}">${{b.events && b.events.length ? `[${{b.events[0].status}}] ${{b.events[0].title}} — ${{b.events[0].subtitle.slice(0, 42)}}` : 'FREE 5-MIN SLOT'}}</span>
        </div>`).join('');
      }}
    }}

    function initSwarm() {{
      const s = PAYLOAD.swarm || {{}};
      document.getElementById('swarmSyncTime').textContent = s.time_str || '';
      const grid = document.getElementById('swarmNodesGrid');
      grid.innerHTML = '';
      Object.values(s.nodes || {{}}).forEach(node => {{
        const card = document.createElement('div');
        card.className = 'bg-[var(--background)] border border-[var(--border)] rounded-lg p-2';
        card.innerHTML = `
          <div class="text-xs font-semibold truncate">${{node.name}}</div>
          <div class="text-xs font-mono text-emerald-500 font-semibold mt-0.5 truncate">${{node.status}}</div>
          <div class="text-xs text-[var(--muted-foreground)] mt-0.5 line-clamp-2">${{node.detail}}</div>
        `;
        grid.appendChild(card);
      }});

      const pipesList = document.getElementById('swarmPipesList');
      pipesList.innerHTML = '';
      (s.objectives || []).forEach(o => {{
        const d = document.createElement('div');
        d.className = 'border-b border-[var(--border)] pb-1';
        d.textContent = `[OBJ ${{o.state}}] ${{o.app}} | ${{o.goal.slice(0, 52)}}... (${{o.last_event.slice(0, 42)}})`;
        pipesList.appendChild(d);
      }});
      (s.pipelines || []).forEach(p => {{
        const d = document.createElement('div');
        d.className = 'border-b border-[var(--border)] pb-1 text-[var(--muted-foreground)]';
        d.textContent = `[PIPE ${{p.state}}] ${{p.stage.toUpperCase()}} -> ${{p.app}}: ${{p.objective.slice(0, 55)}}`;
        pipesList.appendChild(d);
      }});

      const feedList = document.getElementById('swarmFeedList');
      feedList.innerHTML = '';
      (s.activity_feed || []).forEach(ev => {{
        const d = document.createElement('div');
        d.className = 'border-b border-[var(--border)] pb-1';
        d.textContent = `[${{ev.time}} · ${{ev.ago}}] [${{ev.badge}}] ${{ev.actor}}: ${{ev.summary}}`;
        feedList.appendChild(d);
      }});
    }}

    function initProposals() {{
      const sel = document.getElementById('propSelect');
      sel.innerHTML = '';
      (PAYLOAD.proposals || []).forEach((p, idx) => {{
        const opt = document.createElement('option');
        opt.value = idx;
        opt.textContent = `${{p.skill_name}} (${{p.proposal_id.slice(0, 24)}}...)`;
        sel.appendChild(opt);
      }});
      renderProposal();
    }}

    function renderProposal() {{
      const idx = parseInt(document.getElementById('propSelect').value || '0', 10);
      const p = (PAYLOAD.proposals || [])[idx];
      if (!p) return;
      document.getElementById('propVersionBadge').textContent = `v${{p.version}} (prior: ${{p.installed_version || 'none'}})`;
      document.getElementById('propStatusBadge').textContent = p.status;
      document.getElementById('shaInput').value = p.sha256;
      const lines = (p.skill_content || '').split('\\n').slice(0, 8).map(l => '+ ' + l).join('\\n');
      document.getElementById('propDiffPreview').textContent =
        `--- skills/${{p.skill_name}}/SKILL.md (archived to skills/.archive/${{p.skill_name}}/)\\n+++ proposed/${{p.skill_name}}/SKILL.md (v${{p.version}})\\n` + lines;
      verifyHashInput();
    }}

    function verifyHashInput() {{
      const idx = parseInt(document.getElementById('propSelect').value || '0', 10);
      const p = (PAYLOAD.proposals || [])[idx];
      if (!p) return;
      const val = (document.getElementById('shaInput').value || '').trim().toLowerCase();
      const status = document.getElementById('hashMatchStatus');
      const btn = document.getElementById('approveBtn');
      const isHex64 = /^[0-9a-f]{{64}}$/.test(val);
      if (isHex64 && val === p.sha256) {{
        status.textContent = '64-CHAR SHA-256 MATCH VERIFIED';
        status.className = 'font-mono text-xs text-emerald-500';
        btn.disabled = false;
        btn.classList.remove('opacity-50', 'cursor-not-allowed');
      }} else {{
        status.textContent = !isHex64 ? 'REJECTED: MUST BE 64 HEX CHARS' : 'REJECTED: INTEGRITY HASH MISMATCH';
        status.className = 'font-mono text-xs text-rose-500';
        btn.disabled = true;
        btn.classList.add('opacity-50', 'cursor-not-allowed');
      }}
    }}

    function triggerApprove() {{
      const idx = parseInt(document.getElementById('propSelect').value || '0', 10);
      const p = (PAYLOAD.proposals || [])[idx];
      if (!p) return;
      const val = (document.getElementById('shaInput').value || '').trim().toLowerCase();
      const out = document.getElementById('mcpCallOutput');
      out.classList.remove('hidden');
      out.textContent = `MCP Call Ready: sabrina_approve_proposal(proposal_id="${{p.proposal_id}}", expected_version="${{p.version}}", expected_sha256="${{val}}")`;
    }}

    function moveCursorTarget(ev) {{
      const rect = ev.currentTarget.getBoundingClientRect();
      cursorPos.x = Math.max(12, Math.min(Math.round(ev.clientX - rect.left), Math.round(rect.width - 12)));
      cursorPos.y = Math.max(12, Math.min(Math.round(ev.clientY - rect.top), Math.round(rect.height - 12)));
      const box = document.getElementById('cursorBox');
      box.style.left = (cursorPos.x - 12) + 'px';
      box.style.top = (cursorPos.y - 12) + 'px';
      updateCursorPreview();
    }}

    function updateCursorPreview() {{
      const phys = document.getElementById('physPermToggle').checked;
      const protectedZone = cursorPos.x > 195 && cursorPos.x < 410 && cursorPos.y > 42 && cursorPos.y < 88;
      const box = document.getElementById('cursorBox');
      const gate = document.getElementById('cursorGateStatus');
      if (protectedZone) {{
        box.className = 'absolute border-2 border-rose-500 bg-rose-500/20 rounded pointer-events-none transition-all duration-150 flex items-center justify-center';
        gate.textContent = `BLOCKED at (${{cursorPos.x}}, ${{cursorPos.y}}): Protected control ('Always Allow') detected by _safe_points gate.`;
      }} else if (!phys) {{
        box.className = 'absolute border-2 border-amber-500 bg-amber-500/20 rounded pointer-events-none transition-all duration-150 flex items-center justify-center';
        gate.textContent = `READ-ONLY OBSERVE MODE at (${{cursorPos.x}}, ${{cursorPos.y}}): Bounding box verified; enable physical_input to execute click.`;
      }} else {{
        box.className = 'absolute border-2 border-emerald-500 bg-emerald-500/20 rounded pointer-events-none transition-all duration-150 flex items-center justify-center';
        gate.textContent = `READY FOR EXECUTION at (${{cursorPos.x}}, ${{cursorPos.y}}): Fresh screenshot (<60s TTL), foreground HWND verified, physical_input=True.`;
      }}
    }}

    function initCompetence() {{
      const skills = (PAYLOAD.competence || {{}}).skills || {{}};
      const wrap = document.getElementById('skillTableWrap');
      wrap.innerHTML = '';
      Object.values(skills).forEach(s => {{
        const row = document.createElement('div');
        row.className = 'flex items-center justify-between border-b border-[var(--border)] pb-1';
        row.innerHTML = `<span>${{s.skill_id}}</span><span>Live: ${{s.verified_passes}}/${{s.valid_attempts}} | Sim: ${{s.simulation_passes}}/${{s.simulation_runs}} | Label: ${{s.competence_label}}</span>`;
        wrap.appendChild(row);
      }});
    }}

    initSwarm();
    initProjectsAndGuilds();
    renderCalMode('week');
    initProposals();
    updateCursorPreview();
    initCompetence();
  </script>
</body>
</html>
"""


def write_genui_artifacts(artifact_dir: Path) -> dict[str, Any]:
    """Write the inline and standalone Generative UI HTML files to `artifact_dir`."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = collect_genui_payload()
    inline_html = render_inline_card_html(payload)
    inline_path = artifact_dir / "sabrina_rsi_inline_card.html"
    inline_path.write_text(inline_html, encoding="utf-8")
    return {
        "inline_card_path": str(inline_path),
        "proposal_count": len(payload["proposals"]),
        "evaluated_runs": payload["competence"]["total_evaluated_runs"],
        "long_horizon_projects": len(payload["swarm"].get("long_horizon_projects", [])),
        "registered_external_portals": payload["swarm"].get("external_guild_telemetry", {}).get("registered_portals", 0),
        "swarm_feed_events": len(payload["swarm"]["activity_feed"]),
    }


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else WORKSPACE_ROOT / "artifacts" / "genui"
    info = write_genui_artifacts(out_dir)
    print(json.dumps(info, indent=2))
