#!/usr/bin/env python3
"""
LivingArchive HITL Approval Gate (HumanLayer-style self-hosted approval server).
Pure Python 3 stdlib: http.server, sqlite3, json, urllib.request.
"""

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

HOST = os.environ.get("HITL_HOST", "0.0.0.0")
PORT = int(os.environ.get("HITL_PORT", "8890"))
DB_PATH = os.environ.get("HITL_DB_PATH", "/data/approvals.db")
WEBHOOK_URL = os.environ.get("HITL_WEBHOOK_URL", "").strip()
PUBLIC_URL = os.environ.get("HITL_PUBLIC_URL", "").strip()

VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}
VALID_DECISIONS = {"approved", "rejected"}

DB_LOCK = threading.Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db() -> None:
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    action TEXT NOT NULL,
                    risk_level TEXT NOT NULL DEFAULT 'medium',
                    payload TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    decided_at TEXT,
                    reviewer TEXT,
                    note TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_status_created ON approval_requests(status, created_at DESC)"
            )
            conn.commit()
        finally:
            conn.close()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _expire_stale_locked(conn: sqlite3.Connection) -> None:
    now_str = iso_utc(utc_now())
    conn.execute(
        """
        UPDATE approval_requests
        SET status = 'expired', decided_at = COALESCE(decided_at, ?)
        WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at <= ?
        """,
        (now_str, now_str),
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    try:
        payload_parsed = json.loads(row["payload"]) if row["payload"] else {}
    except json.JSONDecodeError:
        payload_parsed = row["payload"]
    return {
        "id": row["id"],
        "agent": row["agent"],
        "action": row["action"],
        "risk_level": row["risk_level"],
        "payload": payload_parsed,
        "status": row["status"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
        "reviewer": row["reviewer"],
        "note": row["note"],
    }


def create_request(
    agent: str,
    action: str,
    risk_level: str = "medium",
    payload=None,
    ttl_seconds=3600,
) -> dict:
    req_id = f"req_{uuid.uuid4().hex[:12]}"
    now = utc_now()
    created_at = iso_utc(now)
    expires_at = None
    if ttl_seconds is not None:
        ttl_int = int(ttl_seconds)
        if ttl_int > 0:
            expires_at = iso_utc(now + timedelta(seconds=ttl_int))

    risk = (risk_level or "medium").lower().strip()
    if risk not in VALID_RISK_LEVELS:
        risk = "medium"

    payload_json = json.dumps(payload if payload is not None else {}, ensure_ascii=False)

    with DB_LOCK:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT INTO approval_requests
                (id, agent, action, risk_level, payload, status, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (req_id, agent, action, risk, payload_json, created_at, expires_at),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (req_id,)
            ).fetchone()
            record = _row_to_dict(row)
        finally:
            conn.close()

    if WEBHOOK_URL:
        t = threading.Thread(target=send_webhook, args=(record,), daemon=True)
        t.start()

    return record


def list_requests(status_filter: str = "", limit: int = 100) -> list:
    limit = max(1, min(int(limit), 500))
    with DB_LOCK:
        conn = _get_conn()
        try:
            _expire_stale_locked(conn)
            conn.commit()
            if status_filter:
                rows = conn.execute(
                    """
                    SELECT * FROM approval_requests
                    WHERE status = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status_filter, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM approval_requests
                    ORDER BY
                        CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                        created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [_row_to_dict(r) for r in rows]
        finally:
            conn.close()


def get_request(req_id: str):
    with DB_LOCK:
        conn = _get_conn()
        try:
            _expire_stale_locked(conn)
            conn.commit()
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (req_id,)
            ).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def decide_request(req_id: str, decision: str, reviewer: str = "human", note: str = ""):
    decision = decision.lower().strip()
    if decision not in VALID_DECISIONS:
        return None, ("invalid_decision", "decision must be 'approved' or 'rejected'")

    now_str = iso_utc(utc_now())
    with DB_LOCK:
        conn = _get_conn()
        try:
            _expire_stale_locked(conn)
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (req_id,)
            ).fetchone()
            if not row:
                conn.commit()
                return None, ("not_found", f"Request {req_id} not found")
            if row["status"] != "pending":
                conn.commit()
                return _row_to_dict(row), (
                    "conflict",
                    f"Request {req_id} is already '{row['status']}'",
                )

            conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, decided_at = ?, reviewer = ?, note = ?
                WHERE id = ?
                """,
                (decision, now_str, reviewer or "human", note or "", req_id),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT * FROM approval_requests WHERE id = ?", (req_id,)
            ).fetchone()
            return _row_to_dict(updated), None
        finally:
            conn.close()


def pending_count() -> int:
    with DB_LOCK:
        conn = _get_conn()
        try:
            _expire_stale_locked(conn)
            conn.commit()
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM approval_requests WHERE status = 'pending'"
            ).fetchone()
            return int(row["cnt"])
        finally:
            conn.close()


def send_webhook(record: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        summary = (
            f"[HITL Approval Needed] ({record['risk_level'].upper()}) "
            f"agent={record['agent']} action={record['action']} id={record['id']}"
        )
        if PUBLIC_URL:
            summary += f" | Review: {PUBLIC_URL}"

        body = {
            "content": summary,
            "text": summary,
            "title": f"HITL Approval: {record['action']}",
            "message": summary,
            "request": record,
        }
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "livingarchive-hitl-gate/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as exc:
        print(f"[webhook] failed to deliver to {WEBHOOK_URL}: {exc}", file=sys.stderr, flush=True)


UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LivingArchive HITL Approval Gate</title>
<style>
  :root {
    --bg: #0b0f17;
    --surface: #131a26;
    --surface-2: #1a2333;
    --border: #263248;
    --text: #e6edf7;
    --muted: #8fa3bf;
    --accent: #3b82f6;
    --approve: #16a34a;
    --approve-hover: #15803d;
    --reject: #dc2626;
    --reject-hover: #b91c1c;
    --low: #22c55e;
    --medium: #eab308;
    --high: #f97316;
    --critical: #ef4444;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 16px 24px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .brand h1 {
    margin: 0;
    font-size: 1.15rem;
    font-weight: 600;
    letter-spacing: 0.01em;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 0.75rem;
    font-weight: 600;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .pill.pending-count {
    background: rgba(59, 130, 246, 0.16);
    border-color: rgba(59, 130, 246, 0.4);
    color: #93c5fd;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  input[type="text"] {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 10px;
    border-radius: 6px;
    font-size: 0.85rem;
  }
  input[type="text"]:focus {
    outline: none;
    border-color: var(--accent);
  }
  button {
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--surface-2);
    color: var(--text);
    padding: 7px 12px;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 500;
  }
  button:hover { filter: brightness(1.12); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.approve { background: var(--approve); border-color: var(--approve); color: #fff; }
  button.approve:hover { background: var(--approve-hover); }
  button.reject { background: var(--reject); border-color: var(--reject); color: #fff; }
  button.reject:hover { background: var(--reject-hover); }
  main {
    max-width: 1040px;
    margin: 0 auto;
    padding: 20px 24px 48px;
  }
  .tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 18px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 10px;
  }
  .tab {
    background: transparent;
    border: 1px solid transparent;
    color: var(--muted);
    padding: 6px 12px;
  }
  .tab.active {
    background: var(--surface-2);
    border-color: var(--border);
    color: var(--text);
  }
  .list {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .card.status-pending { border-left: 4px solid var(--accent); }
  .card.status-approved { border-left: 4px solid var(--approve); opacity: 0.88; }
  .card.status-rejected { border-left: 4px solid var(--reject); opacity: 0.88; }
  .card.status-expired { border-left: 4px solid var(--muted); opacity: 0.72; }
  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 10px;
  }
  .action-title {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text);
    word-break: break-word;
  }
  .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    font-size: 0.8rem;
    color: var(--muted);
    margin-top: 4px;
  }
  .badge {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .risk-low { background: rgba(34, 197, 94, 0.15); color: var(--low); }
  .risk-medium { background: rgba(234, 179, 8, 0.15); color: var(--medium); }
  .risk-high { background: rgba(249, 115, 22, 0.18); color: var(--high); }
  .risk-critical { background: rgba(239, 68, 68, 0.2); color: var(--critical); }
  .st-pending { background: rgba(59, 130, 246, 0.16); color: #93c5fd; }
  .st-approved { background: rgba(22, 163, 74, 0.2); color: #86efac; }
  .st-rejected { background: rgba(220, 38, 38, 0.2); color: #fca5a5; }
  .st-expired { background: rgba(143, 163, 191, 0.18); color: var(--muted); }
  pre.payload {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    margin: 10px 0 12px;
    font-size: 0.8rem;
    overflow-x: auto;
    color: #cbd5e1;
    max-height: 260px;
  }
  .decision-bar {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
  }
  .decision-bar input[type="text"] {
    flex: 1;
    min-width: 200px;
  }
  .decision-info {
    font-size: 0.82rem;
    color: var(--muted);
    margin-top: 6px;
  }
  .empty {
    text-align: center;
    padding: 48px 16px;
    color: var(--muted);
    background: var(--surface);
    border: 1px dashed var(--border);
    border-radius: 10px;
  }
  .toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 10px 16px;
    border-radius: 8px;
    font-size: 0.85rem;
    display: none;
    z-index: 100;
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <h1>LivingArchive HITL Approval Gate</h1>
    <span id="pendingPill" class="pill pending-count">0 pending</span>
  </div>
  <div class="controls">
    <label for="reviewerInput" style="font-size:0.82rem;color:var(--muted);">Reviewer:</label>
    <input id="reviewerInput" type="text" placeholder="charles" style="width:130px;" />
    <button onclick="loadRequests()">Refresh</button>
  </div>
</header>
<main>
  <div class="tabs">
    <button class="tab active" data-filter="pending" onclick="setFilter('pending')">Pending</button>
    <button class="tab" data-filter="decided" onclick="setFilter('decided')">Decided</button>
    <button class="tab" data-filter="all" onclick="setFilter('all')">All</button>
  </div>
  <div id="requestList" class="list"></div>
</main>
<div id="toast" class="toast"></div>
<script>
  let currentFilter = 'pending';
  let allRequests = [];

  const reviewerEl = document.getElementById('reviewerInput');
  reviewerEl.value = localStorage.getItem('hitl_reviewer') || 'charles';
  reviewerEl.addEventListener('input', () => {
    localStorage.setItem('hitl_reviewer', reviewerEl.value.trim() || 'human');
  });

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 2800);
  }

  function setFilter(f) {
    currentFilter = f;
    document.querySelectorAll('.tab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.filter === f);
    });
    render();
  }

  function escapeHtml(str) {
    return String(str ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  async function loadRequests() {
    try {
      const res = await fetch('/api/requests?limit=200');
      const data = await res.json();
      allRequests = data.requests || [];
      const pendingCnt = allRequests.filter(r => r.status === 'pending').length;
      document.getElementById('pendingPill').textContent = `${pendingCnt} pending`;
      render();
    } catch (e) {
      showToast('Failed to load requests');
    }
  }

  async function decide(id, decision) {
    const noteEl = document.getElementById('note-' + id);
    const note = noteEl ? noteEl.value.trim() : '';
    const reviewer = reviewerEl.value.trim() || 'human';
    try {
      const res = await fetch(`/api/requests/${encodeURIComponent(id)}/decide`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decision, reviewer, note })
      });
      const data = await res.json();
      if (!res.ok) {
        showToast(data.error || 'Decision failed');
      } else {
        showToast(`${id} marked ${decision}`);
      }
      await loadRequests();
    } catch (e) {
      showToast('Network error submitting decision');
    }
  }

  function render() {
    const listEl = document.getElementById('requestList');
    let items = allRequests;
    if (currentFilter === 'pending') {
      items = allRequests.filter(r => r.status === 'pending');
    } else if (currentFilter === 'decided') {
      items = allRequests.filter(r => r.status !== 'pending');
    }

    if (!items.length) {
      listEl.innerHTML = `<div class="empty">No ${currentFilter} approval requests.</div>`;
      return;
    }

    listEl.innerHTML = items.map(r => {
      const payloadStr = typeof r.payload === 'string'
        ? r.payload
        : JSON.stringify(r.payload, null, 2);
      const actionBar = r.status === 'pending'
        ? `<div class="decision-bar">
             <input id="note-${escapeHtml(r.id)}" type="text" placeholder="Optional reviewer note..." />
             <button class="approve" onclick="decide('${escapeHtml(r.id)}', 'approved')">Approve</button>
             <button class="reject" onclick="decide('${escapeHtml(r.id)}', 'rejected')">Reject</button>
           </div>`
        : `<div class="decision-info">
             <strong>${escapeHtml(r.status.toUpperCase())}</strong>
             ${r.reviewer ? ` by <strong>${escapeHtml(r.reviewer)}</strong>` : ''}
             ${r.decided_at ? ` at ${escapeHtml(r.decided_at)}` : ''}
             ${r.note ? ` &mdash; Note: <em>${escapeHtml(r.note)}</em>` : ''}
           </div>`;

      return `
        <div class="card status-${escapeHtml(r.status)}">
          <div class="card-header">
            <div>
              <div class="action-title">${escapeHtml(r.action)}</div>
              <div class="meta">
                <span>Agent: <strong>${escapeHtml(r.agent)}</strong></span>
                <span>&middot;</span>
                <span>ID: <code>${escapeHtml(r.id)}</code></span>
                <span>&middot;</span>
                <span>Created: ${escapeHtml(r.created_at)}</span>
                ${r.expires_at ? `<span>&middot;</span><span>Expires: ${escapeHtml(r.expires_at)}</span>` : ''}
              </div>
            </div>
            <div style="display:flex;gap:6px;align-items:center;">
              <span class="badge risk-${escapeHtml(r.risk_level)}">${escapeHtml(r.risk_level)}</span>
              <span class="badge st-${escapeHtml(r.status)}">${escapeHtml(r.status)}</span>
            </div>
          </div>
          <pre class="payload">${escapeHtml(payloadStr)}</pre>
          ${actionBar}
        </div>
      `;
    }).join('');
  }

  loadRequests();
  setInterval(loadRequests, 5000);
</script>
</body>
</html>
"""


class HitlRequestHandler(BaseHTTPRequestHandler):
    server_version = "LivingArchiveHITL/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"[{iso_utc(utc_now())}] {self.address_string()} - {fmt % args}\n"
        )
        sys.stderr.flush()

    def _send_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self, status: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        if length > 1024 * 1024:
            raise ValueError("Request body exceeds 1MB limit")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._send_html(200, UI_HTML)
            return

        if path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": "livingarchive-hitl-gate",
                    "pending_count": pending_count(),
                    "timestamp": iso_utc(utc_now()),
                },
            )
            return

        if path == "/api/requests":
            status_filter = qs.get("status", [""])[0].strip().lower()
            try:
                limit = int(qs.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            items = list_requests(status_filter=status_filter, limit=limit)
            self._send_json(200, {"requests": items, "count": len(items)})
            return

        if path.startswith("/api/requests/"):
            req_id = path[len("/api/requests/") :]
            if "/" not in req_id and req_id:
                record = get_request(req_id)
                if not record:
                    self._send_json(404, {"error": f"Request '{req_id}' not found"})
                    return
                self._send_json(200, record)
                return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/requests":
            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json(400, {"error": f"Invalid JSON body: {exc}"})
                return

            agent = str(body.get("agent", "")).strip()
            action = str(body.get("action", "")).strip()
            if not agent or not action:
                self._send_json(400, {"error": "'agent' and 'action' are required"})
                return

            risk_level = str(body.get("risk_level", "medium")).strip().lower()
            payload = body.get("payload", {})
            ttl_seconds = body.get("ttl_seconds", 3600)
            try:
                if ttl_seconds is not None:
                    ttl_seconds = int(ttl_seconds)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "'ttl_seconds' must be an integer or null"})
                return

            record = create_request(
                agent=agent,
                action=action,
                risk_level=risk_level,
                payload=payload,
                ttl_seconds=ttl_seconds,
            )
            self._send_json(201, record)
            return

        if path.startswith("/api/requests/") and path.endswith("/decide"):
            req_id = path[len("/api/requests/") : -len("/decide")].strip("/")
            if not req_id or "/" in req_id:
                self._send_json(404, {"error": "Invalid request ID"})
                return

            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json(400, {"error": f"Invalid JSON body: {exc}"})
                return

            decision = str(body.get("decision", "")).strip().lower()
            reviewer = str(body.get("reviewer", "human")).strip() or "human"
            note = str(body.get("note", "")).strip()

            record, err = decide_request(req_id, decision, reviewer=reviewer, note=note)
            if err:
                code, msg = err
                if code == "not_found":
                    self._send_json(404, {"error": msg})
                elif code == "conflict":
                    self._send_json(409, {"error": msg, "request": record})
                else:
                    self._send_json(400, {"error": msg})
                return

            self._send_json(200, record)
            return

        self._send_json(404, {"error": "Not found"})


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), HitlRequestHandler)
    print(
        f"livingarchive-hitl-gate listening on http://{HOST}:{PORT} (db={DB_PATH})",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
