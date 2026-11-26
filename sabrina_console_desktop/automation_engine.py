"""SQLite-backed Automation Loop Engine & Gemini Multiple-Choice Workflow Co-Designer."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

try:
    from sabrina_console_desktop.connector_registry import ConnectorRegistry
except ImportError:
    from connector_registry import ConnectorRegistry  # type: ignore[no-redef]

DEFAULT_AUTOMATIONS_DB = Path(__file__).resolve().parent / "data" / "automations.sqlite3"

VALID_A2A_COMM_TYPES: tuple[str, ...] = (
    "pass_off",
    "loop",
    "critique_gate",
    "delegate_spawn",
    "blackboard_sync",
    "escalate_hitl",
)

VALID_A2A_CONDITIONS: tuple[str, ...] = (
    "always",
    "on_needs_work",
    "on_pass",
    "on_low_confidence",
    "on_error",
    "custom",
)

CODESIGNER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["needs_input", "ready"]},
        "assistant_message": {"type": "string"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "prompt": {"type": "string"},
                    "multi_select": {"type": "boolean"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["id", "label"],
                        },
                    },
                },
                "required": ["id", "prompt", "multi_select", "options"],
            },
        },
        "blueprint": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "goal": {"type": "string"},
                "loop_type": {
                    "type": "string",
                    "enum": ["recurring_interval", "goal_until_done", "manual_trigger", "autonomous_a2a"],
                },
                "interval_minutes": {"type": "integer"},
                "max_iterations": {"type": "integer"},
                "approval_policy": {
                    "type": "string",
                    "enum": ["always_for_writes", "threshold", "never"],
                },
                "approval_threshold": {"type": "number"},
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "step_type": {
                                "type": "string",
                                "enum": ["connector", "gemini", "swarm", "agent", "qa", "recommend_next"],
                            },
                            "agent_role": {"type": "string"},
                            "model_id": {"type": "string"},
                            "connector_id": {"type": "string"},
                            "action": {"type": "string"},
                            "kind": {"type": "string", "enum": ["read", "write"]},
                            "prompt_template": {"type": "string"},
                            "params": {"type": "object"},
                        },
                        "required": ["name", "step_type"],
                    },
                },
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "comm_type": {"type": "string", "enum": list(VALID_A2A_COMM_TYPES)},
                            "condition": {"type": "string", "enum": list(VALID_A2A_CONDITIONS)},
                            "condition_expr": {"type": "string"},
                            "max_loops": {"type": "integer"},
                            "payload_mode": {"type": "string"},
                            "message_template": {"type": "string"},
                        },
                        "required": ["from", "to", "comm_type"],
                    },
                },
            },
        },
    },
    "required": ["status", "assistant_message"],
}


def extract_recommended_next_goal(raw_text: str, fallback_goal: str = "") -> str:
    """Extract a clean single-line mission goal recommendation suitable for the Swarm goal input field."""
    txt = str(raw_text or "").strip()
    if txt:
        for pattern in (
            r"RECOMMENDED_NEXT_GOAL\s*:\s*(.+)",
            r"RECOMMENDED_NEXT_MISSION\s*:\s*(.+)",
            r"NEXT_MISSION_GOAL\s*:\s*(.+)",
            r"RECOMMENDED\s+NEXT\s+STEP\s*:\s*(.+)",
            r"NEXT_STEP\s*:\s*(.+)",
        ):
            m = re.search(pattern, txt, re.IGNORECASE)
            if m:
                cand = m.group(1).strip().splitlines()[0].strip()
                cand = re.sub(r"^[\-*•#>`\"'\s]+|[\"`'\s]+$", "", cand)
                cand = re.sub(r"\s+", " ", cand).strip()
                if len(cand) >= 10:
                    return cand[:320]

        lines = [
            re.sub(r"^[\-*•\d\.\)\s`\"']+|[\"`'\s]+$", "", ln.strip())
            for ln in txt.splitlines()
            if ln.strip() and not ln.strip().startswith(("```", "---", "===", "{", "}"))
        ]
        substantive = [
            ln for ln in lines
            if len(ln) >= 18 and not ln.upper().startswith(("PASS:", "NEEDS_WORK:"))
        ]
        if substantive:
            cand = re.sub(r"\s+", " ", substantive[-1]).strip()
            if len(cand) >= 12:
                return cand[:320]
    clean_fb = re.sub(r"\s+", " ", str(fallback_goal or "").strip())
    if clean_fb:
        short_fb = clean_fb[:200]
        return f"Next Iteration: Deepen implementation, verify edge-case invariants, and expand test coverage for: {short_fb}"
    return "Audit the latest swarm deliverables, implement the highest-leverage improvement, and verify with unit tests."


_SBV_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "across",
        "under", "must", "should", "will", "can", "are", "was", "were", "all",
        "any", "next", "iteration", "cycle", "step", "mission", "goal", "objective",
        "audit", "verify", "implement", "execute", "run", "using",
    }
)

_SBV_SABRINA_CLUSTER: frozenset[str] = frozenset(
    {
        "sabrina", "swarm", "a2a", "agent", "agents", "node", "nodes", "edge",
        "edges", "loop", "loops", "workflow", "workflows", "sqlite", "hitl",
        "telemetry", "planner", "executor", "critic", "scout", "finale", "evo",
        "bench", "console", "html", "input", "autofill", "self", "scaffold",
    }
)


def _tokenize_sbv(text: str) -> dict[str, float]:
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]{2,}", str(text or "").lower())
    counts: dict[str, float] = {}
    domain_hits = 0
    for tok in tokens:
        if tok in _SBV_STOPWORDS:
            continue
        counts[tok] = counts.get(tok, 0.0) + 1.0
        if tok in _SBV_SABRINA_CLUSTER:
            domain_hits += 1
        if len(tok) >= 6:
            stem = tok[:5]
            counts[f"~{stem}"] = counts.get(f"~{stem}", 0.0) + 0.5
    if domain_hits > 0:
        counts["@domain:sabrina_architecture"] = min(2.0, 0.85 + 0.25 * domain_hits)
    return counts


def compute_goal_similarity(anchor_goal: str, candidate_goal: str) -> float:
    """Compute lexical-stem cosine similarity in [0.0, 1.0] for SBV goal-drift detection."""
    u = _tokenize_sbv(anchor_goal)
    v = _tokenize_sbv(candidate_goal)
    if not u or not v:
        return 0.0
    dot = sum(w * v.get(k, 0.0) for k, w in u.items())
    norm_u = math.sqrt(sum(w * w for w in u.values()))
    norm_v = math.sqrt(sum(w * w for w in v.values()))
    if norm_u <= 1e-12 or norm_v <= 1e-12:
        return 0.0
    return round(max(0.0, min(1.0, dot / (norm_u * norm_v))), 4)


def stabilize_evolved_goal(
    anchor_goal: str,
    candidate_goal: str,
    goal_history: list[str],
    drift_threshold: float = 0.16,
) -> dict[str, Any]:
    """Enforce Semantic Bounding Vector (SBV) drift tether and cyclic oscillation detection."""
    clean_anchor = re.sub(r"\s+", " ", str(anchor_goal or "").strip())
    clean_cand = re.sub(r"\s+", " ", str(candidate_goal or "").strip())
    if not clean_cand:
        clean_cand = extract_recommended_next_goal("", clean_anchor)

    norm_cand = clean_cand.lower()
    oscillation_halted = False
    for prior in goal_history:
        clean_prior = re.sub(r"\s+", " ", str(prior or "").strip())
        if not clean_prior:
            continue
        if clean_prior.lower() == norm_cand or compute_goal_similarity(clean_prior, clean_cand) >= 0.95:
            oscillation_halted = True
            break

    sim = compute_goal_similarity(clean_anchor, clean_cand) if clean_anchor else 1.0
    drift_intercepted = bool(clean_anchor and sim < float(drift_threshold))
    effective_goal = clean_cand
    if drift_intercepted and not oscillation_halted:
        anchor_short = clean_anchor[:120]
        effective_goal = f"[Anchored to: {anchor_short}] {clean_cand}"[:320]

    return {
        "effective_goal": effective_goal,
        "sbv_similarity": sim,
        "drift_threshold": float(drift_threshold),
        "drift_intercepted": drift_intercepted,
        "oscillation_halted": oscillation_halted,
    }


def normalize_workflow_steps(raw_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            continue
        step = dict(item)
        raw_id = str(step.get("id") or f"step_{idx}").strip()
        if not raw_id or raw_id in seen_ids:
            raw_id = f"step_{idx}_{uuid.uuid4().hex[:6]}"
        seen_ids.add(raw_id)
        step["id"] = raw_id
        out.append(step)
    return out


def normalize_workflow_edges(raw_edges: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_edges, list):
        return []
    out: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            continue
        src = str(item.get("from") or "").strip()
        dst = str(item.get("to") or "").strip()
        if not src or not dst:
            continue
        comm = str(item.get("comm_type") or "pass_off").strip().lower()
        if comm not in VALID_A2A_COMM_TYPES:
            comm = "loop" if comm in ("retry", "cycle", "feedback") else "pass_off"
        default_cond = "on_needs_work" if comm == "loop" else "always"
        cond = str(item.get("condition") or default_cond).strip().lower()
        if cond not in VALID_A2A_CONDITIONS:
            cond = default_cond
        try:
            max_loops = max(1, min(25, int(item.get("max_loops") or 3)))
        except (TypeError, ValueError):
            max_loops = 3
        try:
            max_payload_bytes = max(512, int(item.get("max_payload_bytes"))) if item.get("max_payload_bytes") else 0
        except (TypeError, ValueError):
            max_payload_bytes = 0
        eid = str(item.get("id") or f"edge_{idx}_{src}_{dst}").strip()
        if not eid or eid in seen_ids:
            eid = f"edge_{idx}_{uuid.uuid4().hex[:6]}"
        seen_ids.add(eid)
        edge_dict: dict[str, Any] = {
            "id": eid,
            "from": src,
            "to": dst,
            "comm_type": comm,
            "condition": cond,
            "condition_expr": str(item.get("condition_expr") or "").strip(),
            "max_loops": max_loops,
            "payload_mode": str(item.get("payload_mode") or "full_envelope").strip(),
            "message_template": str(item.get("message_template") or item.get("label") or "").strip(),
        }
        if max_payload_bytes > 0:
            edge_dict["max_payload_bytes"] = max_payload_bytes
        out.append(edge_dict)
    return out


class AutomationEngine:
    """Durable SQLite automation loop engine with HITL checkpointing and A2A workflow routing."""

    def __init__(
        self,
        db_path: Path | None = None,
        connector_registry: ConnectorRegistry | None = None,
        gemini_caller: Callable[[str, dict[str, Any] | None], str] | None = None,
        swarm_caller: Callable[[str, str], dict[str, Any]] | None = None,
        agent_caller: Callable[[str, str, str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_AUTOMATIONS_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connectors = connector_registry or ConnectorRegistry()
        self.gemini_caller = gemini_caller
        self.swarm_caller = swarm_caller
        self.agent_caller = agent_caller
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._init_db()

    @contextmanager
    def _db(self):
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                with conn:
                    yield conn
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS automation_loops (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'manual',
                    loop_type TEXT NOT NULL DEFAULT 'recurring_interval',
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    max_iterations INTEGER NOT NULL DEFAULT 5,
                    current_iteration INTEGER NOT NULL DEFAULT 0,
                    agent_role TEXT NOT NULL DEFAULT 'coordinator',
                    model_id TEXT NOT NULL DEFAULT '',
                    steps_json TEXT NOT NULL DEFAULT '[]',
                    edges_json TEXT NOT NULL DEFAULT '[]',
                    approval_policy TEXT NOT NULL DEFAULT 'always_for_writes',
                    approval_threshold REAL NOT NULL DEFAULT 0.0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'idle',
                    next_run REAL,
                    last_run REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS automation_runs (
                    id TEXT PRIMARY KEY,
                    loop_id TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    current_step_index INTEGER NOT NULL DEFAULT 0,
                    execution_queue_json TEXT,
                    edge_counts_json TEXT NOT NULL DEFAULT '{}',
                    a2a_events_json TEXT NOT NULL DEFAULT '[]',
                    pending_approval_json TEXT,
                    step_outputs_json TEXT NOT NULL DEFAULT '[]',
                    goal_met INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    started_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_loop ON automation_runs(loop_id, started_at DESC);
                """
            )
            for migration_sql in (
                "ALTER TABLE automation_loops ADD COLUMN edges_json TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE automation_runs ADD COLUMN execution_queue_json TEXT",
                "ALTER TABLE automation_runs ADD COLUMN edge_counts_json TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE automation_runs ADD COLUMN a2a_events_json TEXT NOT NULL DEFAULT '[]'",
            ):
                try:
                    db.execute(migration_sql)
                except sqlite3.OperationalError:
                    pass

    def _decode_loop(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        d["steps"] = normalize_workflow_steps(json.loads(d.pop("steps_json", "[]") or "[]"))
        d["edges"] = normalize_workflow_edges(json.loads(d.pop("edges_json", "[]") or "[]"))
        d["enabled"] = bool(d.get("enabled", 0))
        return d

    def _decode_run(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        d = dict(row)
        d["step_outputs"] = json.loads(d.pop("step_outputs_json", "[]") or "[]")
        raw_pending = d.pop("pending_approval_json", None)
        d["pending_approval"] = json.loads(raw_pending) if raw_pending else None
        raw_queue = d.pop("execution_queue_json", None)
        d["execution_queue"] = json.loads(raw_queue) if raw_queue else None
        d["edge_counts"] = json.loads(d.pop("edge_counts_json", "{}") or "{}")
        d["a2a_events"] = json.loads(d.pop("a2a_events_json", "[]") or "[]")
        d["goal_met"] = bool(d.get("goal_met", 0))
        return d

    def list_loops(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM automation_loops ORDER BY updated_at DESC").fetchall()
        loops = [self._decode_loop(r) for r in rows]
        return [lp for lp in loops if lp]

    def get_loop(self, loop_id: str) -> dict[str, Any] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM automation_loops WHERE id=?", (loop_id,)).fetchone()
        return self._decode_loop(row)

    def save_loop(self, spec: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        loop_id = str(spec.get("id") or f"loop_{uuid.uuid4().hex[:12]}")
        existing = self.get_loop(loop_id)
        loop_type = str(spec.get("loop_type") or (existing["loop_type"] if existing else "recurring_interval"))
        interval_min = max(1, int(spec.get("interval_minutes") or (existing["interval_minutes"] if existing else 60)))
        max_iter = max(1, int(spec.get("max_iterations") or (existing["max_iterations"] if existing else 5)))
        enabled = 1 if spec.get("enabled", True) else 0
        next_run = spec.get("next_run")
        if next_run is None and enabled and loop_type == "recurring_interval":
            next_run = now + interval_min * 60
        steps = normalize_workflow_steps(
            spec.get("steps") if isinstance(spec.get("steps"), list) else (existing["steps"] if existing else [])
        )
        raw_edges = (
            spec.get("edges")
            if isinstance(spec.get("edges"), list)
            else (existing["edges"] if (existing and "edges" not in spec) else [])
        )
        edges = normalize_workflow_edges(raw_edges)

        with self._db() as db:
            if existing:
                db.execute(
                    """
                    UPDATE automation_loops
                    SET name=?, goal=?, mode=?, loop_type=?, interval_minutes=?, max_iterations=?,
                        agent_role=?, model_id=?, steps_json=?, edges_json=?, approval_policy=?, approval_threshold=?,
                        enabled=?, next_run=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        str(spec.get("name") or existing["name"]),
                        str(spec.get("goal") if "goal" in spec else existing["goal"]),
                        str(spec.get("mode") or existing["mode"]),
                        loop_type,
                        interval_min,
                        max_iter,
                        str(spec.get("agent_role") or existing["agent_role"]),
                        str(spec.get("model_id") if "model_id" in spec else existing["model_id"]),
                        json.dumps(steps),
                        json.dumps(edges),
                        str(spec.get("approval_policy") or existing.get("approval_policy") or "always_for_writes"),
                        float(spec.get("approval_threshold") if "approval_threshold" in spec else (existing.get("approval_threshold") or 0.0)),
                        enabled,
                        next_run,
                        now,
                        loop_id,
                    ),
                )
            else:
                db.execute(
                    """
                    INSERT INTO automation_loops (
                        id, name, goal, mode, loop_type, interval_minutes, max_iterations,
                        current_iteration, agent_role, model_id, steps_json, edges_json, approval_policy,
                        approval_threshold, enabled, status, next_run, last_run, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, NULL, ?, ?)
                    """,
                    (
                        loop_id,
                        str(spec.get("name") or "Untitled Loop"),
                        str(spec.get("goal") or ""),
                        str(spec.get("mode") or "manual"),
                        loop_type,
                        interval_min,
                        max_iter,
                        str(spec.get("agent_role") or "coordinator"),
                        str(spec.get("model_id") or ""),
                        json.dumps(steps),
                        json.dumps(edges),
                        str(spec.get("approval_policy") or "always_for_writes"),
                        float(spec.get("approval_threshold") or 0.0),
                        enabled,
                        next_run,
                        now,
                        now,
                    ),
                )
        saved = self.get_loop(loop_id)
        if saved is not None:
            for extra_key in ("max_payload_bytes", "sbv_drift_threshold"):
                if extra_key in spec:
                    saved[extra_key] = spec[extra_key]
        return {"ok": True, "loop": saved}

    def save_and_initiate_loop(self, spec: dict[str, Any]) -> dict[str, Any]:
        merged = {**spec, "enabled": True}
        saved_res = self.save_loop(merged)
        if not saved_res.get("ok") or not saved_res.get("loop"):
            return saved_res
        loop_obj = saved_res["loop"]
        # Carry transient runtime tunables (`max_payload_bytes`, `sbv_drift_threshold`) if passed on spec
        self._transient_loop_opts = getattr(self, "_transient_loop_opts", {})
        for k in ("max_payload_bytes", "sbv_drift_threshold"):
            if k in spec:
                self._transient_loop_opts[(loop_obj["id"], k)] = spec[k]
        run_res = self.run_loop_now(loop_obj["id"])
        refreshed = self.get_loop(loop_obj["id"]) or loop_obj
        return {"ok": bool(run_res.get("ok", True)), "loop": refreshed, "run": run_res}

    def delete_loop(self, loop_id: str) -> dict[str, Any]:
        with self._db() as db:
            db.execute("DELETE FROM automation_runs WHERE loop_id=?", (loop_id,))
            cur = db.execute("DELETE FROM automation_loops WHERE id=?", (loop_id,))
        return {"ok": True, "deleted": cur.rowcount > 0, "loop_id": loop_id}

    def toggle_loop(self, loop_id: str, enabled: bool) -> dict[str, Any]:
        now = time.time()
        loop = self.get_loop(loop_id)
        if not loop:
            return {"ok": False, "error": "Loop not found"}
        next_run = (now + loop["interval_minutes"] * 60) if (enabled and loop["loop_type"] == "recurring_interval") else None
        status = "idle" if enabled else "paused"
        with self._db() as db:
            db.execute(
                "UPDATE automation_loops SET enabled=?, status=?, next_run=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, status, next_run, now, loop_id),
            )
        return {"ok": True, "loop": self.get_loop(loop_id)}

    def get_runs(self, loop_id: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        with self._db() as db:
            if loop_id:
                rows = db.execute(
                    "SELECT * FROM automation_runs WHERE loop_id=? ORDER BY started_at DESC LIMIT ?",
                    (loop_id, limit),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM automation_runs ORDER BY started_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        runs = [self._decode_run(r) for r in rows]
        return [r for r in runs if r]

    def list_pending_approvals(self) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT * FROM automation_runs WHERE status='waiting_approval' ORDER BY started_at DESC"
            ).fetchall()
        runs = [self._decode_run(r) for r in rows]
        return [r for r in runs if r]

    def _needs_approval(
        self,
        loop: dict[str, Any],
        step: dict[str, Any],
        resolved_params: dict[str, Any],
    ) -> tuple[bool, str]:
        policy = loop.get("approval_policy", "always_for_writes")
        if policy == "never":
            return False, ""
        step_kind = step.get("kind")
        if not step_kind and step.get("step_type") == "connector":
            act = self.connectors.get_action_spec(step.get("connector_id", ""), step.get("action", ""))
            if act:
                step_kind = act.kind
                if act.approval_required:
                    step_kind = "write"
        if step_kind != "write":
            return False, ""
        if policy == "threshold":
            threshold = float(loop.get("approval_threshold") or 0.0)
            amount_raw = resolved_params.get("amount")
            if amount_raw is not None:
                try:
                    if float(amount_raw) <= threshold:
                        return False, ""
                    return True, f"Amount {amount_raw} exceeds threshold {threshold}"
                except (TypeError, ValueError):
                    pass
        return True, f"Write action '{step.get('name', step.get('action', 'step'))}' requires operator approval"

    @staticmethod
    def _compact_text_preserving_ends(text: str, max_chars: int = 14000) -> str:
        s = str(text or "")
        if len(s) <= max_chars:
            return s
        head = int(max_chars * 0.72)
        tail = max(400, max_chars - head - 48)
        return f"{s[:head]}\n...[compacted {len(s) - head - tail} middle chars]...\n{s[-tail:]}"

    @classmethod
    def _compact_json_safe(cls, value: Any, max_str_chars: int = 14000, depth: int = 0) -> Any:
        if depth > 6:
            return cls._compact_text_preserving_ends(str(value), 600)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            return cls._compact_text_preserving_ends(value, max_str_chars)
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for idx, (k, v) in enumerate(value.items()):
                if idx >= 32:
                    out["__truncated_keys__"] = len(value) - 32
                    break
                out[str(k)] = cls._compact_json_safe(v, max_str_chars=max_str_chars, depth=depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            items = [cls._compact_json_safe(x, max_str_chars=max_str_chars, depth=depth + 1) for x in list(value)[:20]]
            if len(value) > 20:
                items.append(f"...[{len(value) - 20} more items]")
            return items
        return cls._compact_text_preserving_ends(str(value), max_str_chars)

    @classmethod
    def _enforce_payload_budget(cls, payload_val: Any, max_bytes: int = 24576) -> tuple[Any, int, bool]:
        """Enforce a deterministic UTF-8 serialized JSON byte ceiling without breaking JSON structure."""
        ceiling = max(512, int(max_bytes or 24576))
        serialized = json.dumps(payload_val, sort_keys=True, ensure_ascii=False, default=str)
        raw_bytes = len(serialized.encode("utf-8"))
        if raw_bytes <= ceiling:
            return payload_val, raw_bytes, False

        capped = True
        budget_chars = max(240, int(ceiling * 0.72))
        compacted = payload_val
        for _ in range(6):
            compacted = cls._compact_json_safe(payload_val, max_str_chars=budget_chars)
            s_try = json.dumps(compacted, sort_keys=True, ensure_ascii=False, default=str)
            b_try = len(s_try.encode("utf-8"))
            if b_try <= ceiling:
                return compacted, b_try, capped
            budget_chars = max(120, int(budget_chars * 0.55))

        fallback_excerpt = cls._compact_text_preserving_ends(serialized, max(160, ceiling - 120))
        wrapped: dict[str, Any] = {"__budget_capped__": True, "excerpt": fallback_excerpt}
        final_s = json.dumps(wrapped, sort_keys=True, ensure_ascii=False, default=str)
        while len(final_s.encode("utf-8")) > ceiling and len(wrapped["excerpt"]) > 40:
            wrapped["excerpt"] = wrapped["excerpt"][: max(40, len(wrapped["excerpt"]) // 2)]
            final_s = json.dumps(wrapped, sort_keys=True, ensure_ascii=False, default=str)
        return wrapped, len(final_s.encode("utf-8")), True

    @classmethod
    def _format_a2a_envelope(
        cls,
        edge: dict[str, Any],
        from_name: str,
        to_name: str,
        prior_outputs: list[dict[str, Any]],
        hop: int,
        max_payload_bytes: int | None = None,
    ) -> dict[str, Any]:
        from_id = str(edge.get("from") or "")
        source_output = next(
            (o for o in reversed(prior_outputs) if o.get("node_id") == from_id),
            prior_outputs[-1] if prior_outputs else None,
        )
        payload_mode = str(edge.get("payload_mode") or "full_envelope")
        raw_res = source_output.get("result") if isinstance(source_output, dict) else None
        if payload_mode == "summary_only":
            if isinstance(raw_res, dict) and raw_res.get("text"):
                payload_val: Any = cls._compact_text_preserving_ends(str(raw_res["text"]), 1800)
            else:
                payload_val = cls._compact_json_safe(raw_res, max_str_chars=1200) if raw_res else ""
        elif payload_mode == "raw_output":
            raw_txt = raw_res.get("text") if (isinstance(raw_res, dict) and "text" in raw_res) else raw_res
            payload_val = cls._compact_json_safe(raw_txt, max_str_chars=14000)
        elif payload_mode == "artifacts_ref":
            payload_val = {
                "mention": f"@{edge.get('to', 'agent')}",
                "source_step": from_name,
                "excerpt": (
                    cls._compact_text_preserving_ends(str(raw_res.get("text", "")), 1600)
                    if isinstance(raw_res, dict)
                    else cls._compact_text_preserving_ends(str(raw_res or ""), 1600)
                ),
            }
        else:
            payload_val = cls._compact_json_safe(
                source_output if source_output else {"trigger": "loop_root"},
                max_str_chars=12000,
            )

        eff_ceiling = int(max_payload_bytes or edge.get("max_payload_bytes") or 24576)
        payload_val, payload_bytes, budget_capped = cls._enforce_payload_budget(payload_val, eff_ceiling)
        return {
            "edge_id": edge.get("id", ""),
            "from": from_id,
            "from_name": from_name,
            "to": edge.get("to", ""),
            "to_name": to_name,
            "comm_type": edge.get("comm_type", "pass_off"),
            "condition": edge.get("condition", "always"),
            "hop": hop,
            "payload_mode": payload_mode,
            "payload_bytes": payload_bytes,
            "budget_capped": budget_capped,
            "instruction": str(edge.get("message_template") or ""),
            "payload": payload_val,
        }

    def _execute_single_step(
        self,
        loop: dict[str, Any],
        step: dict[str, Any],
        prior_outputs: list[dict[str, Any]],
        override_params: dict[str, Any] | None = None,
        incoming_a2a: dict[str, Any] | list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        step_type = step.get("step_type", "connector")
        params = dict(override_params if override_params is not None else (step.get("params") or {}))
        a2a_batch = (
            list(incoming_a2a)
            if isinstance(incoming_a2a, list)
            else ([incoming_a2a] if isinstance(incoming_a2a, dict) else [])
        )
        prior_window = prior_outputs[-2:] if a2a_batch else prior_outputs[-3:]
        prior_char_budget = 1200 if a2a_batch else 3600
        compact_priors = self._compact_json_safe(prior_window, max_str_chars=prior_char_budget) if prior_window else []
        context_blob = json.dumps(compact_priors, default=str)
        a2a_section = ""
        if a2a_batch:
            blocks: list[str] = []
            for env_item in a2a_batch:
                raw_payload = env_item.get("payload")
                payload_str = (
                    raw_payload
                    if isinstance(raw_payload, str)
                    else json.dumps(self._compact_json_safe(raw_payload, max_str_chars=14000), default=str)
                )
                blocks.append(
                    f"Incoming A2A ({env_item.get('comm_type', 'pass_off').upper()} from "
                    f"{env_item.get('from_name', env_item.get('from', 'upstream'))}, hop #{env_item.get('hop', 1)}):\n"
                    + (f"Handoff Instruction: {env_item['instruction']}\n" if env_item.get("instruction") else "")
                    + f"Payload:\n{payload_str}"
                )
            a2a_section = "\n\n" + "\n\n---\n".join(blocks)

        if step_type == "connector":
            cid = str(step.get("connector_id") or "")
            action = str(step.get("action") or "")
            if cid == "gemini":
                step_type = "gemini"
            else:
                res = self.connectors.execute_action(cid, action, params)
                return {"step": step.get("name", f"{cid}.{action}"), "type": "connector", "result": res}

        if step_type == "agent":
            role = str(step.get("agent_role") or loop.get("agent_role") or "execution_specialist")
            model_id = str(step.get("model_id") or loop.get("model_id") or "")
            prompt_tpl = str(step.get("prompt_template") or loop.get("goal") or f"Execute {role} responsibilities.")
            sys_prompt = (
                f"You are an autonomous Sabrina swarm agent acting in role '{role}'. "
                "Follow incoming A2A handoff instructions carefully. If acting as a critic/verifier, "
                "prefix 'PASS:' when verified or 'NEEDS_WORK:' if another revision loop is needed."
            )
            user_prompt = (
                f"Loop Goal: {loop.get('goal', '')}\n\n"
                f"Agent Task ({step.get('name', role)}): {prompt_tpl}{a2a_section}\n\n"
                f"Prior Outputs:\n{context_blob}"
            )
            if self.agent_caller:
                try:
                    llm_res = self.agent_caller(role, model_id, sys_prompt, user_prompt)
                    return {"step": step.get("name", role), "type": "agent", "agent_role": role, "result": llm_res}
                except Exception as exc:
                    return {"step": step.get("name", role), "type": "agent", "agent_role": role, "result": {"ok": False, "error": str(exc)}}
            if self.gemini_caller:
                try:
                    text = self.gemini_caller(f"{sys_prompt}\n\n{user_prompt}", None)
                    return {"step": step.get("name", role), "type": "agent", "agent_role": role, "result": {"ok": True, "text": text, "role": role}}
                except Exception as exc:
                    return {"step": step.get("name", role), "type": "agent", "agent_role": role, "result": {"ok": False, "error": str(exc)}}
            if self.swarm_caller:
                res = self.swarm_caller(user_prompt, role)
                return {"step": step.get("name", role), "type": "agent", "agent_role": role, "result": res}
            return {"step": step.get("name", role), "type": "agent", "agent_role": role, "result": {"ok": False, "error": "No LLM or swarm caller configured"}}

        if step_type == "gemini":
            prompt_tpl = str(step.get("prompt_template") or loop.get("goal") or "Summarize the context.")
            full_prompt = f"Goal: {loop.get('goal', '')}\n\nInstruction: {prompt_tpl}{a2a_section}\n\nPrevious Step Outputs:\n{context_blob}"
            if not self.gemini_caller:
                return {"step": step.get("name", "Gemini Step"), "type": "gemini", "result": {"ok": False, "error": "Gemini caller not configured"}}
            try:
                text = self.gemini_caller(full_prompt, None)
                return {"step": step.get("name", "Gemini Step"), "type": "gemini", "result": {"ok": True, "text": text}}
            except Exception as exc:
                return {"step": step.get("name", "Gemini Step"), "type": "gemini", "result": {"ok": False, "error": str(exc)}}

        if step_type == "swarm":
            goal_text = str(step.get("prompt_template") or loop.get("goal") or "")
            role = str(step.get("agent_role") or loop.get("agent_role") or "coordinator")
            if not self.swarm_caller:
                return {"step": step.get("name", "Swarm Step"), "type": "swarm", "result": {"ok": False, "error": "Swarm caller not configured"}}
            res = self.swarm_caller(f"{goal_text}{a2a_section}\nContext: {context_blob}", role)
            return {"step": step.get("name", "Swarm Step"), "type": "swarm", "result": res}

        if step_type == "qa":
            try:
                from sabrina_console_desktop.product_qa import run_product_qa
            except ImportError:
                from product_qa import run_product_qa  # type: ignore[no-redef]
            ws_root = str(params.get("workspace_root") or getattr(self, "workspace_root", "") or Path(__file__).resolve().parent.parent)
            qa_target = str(params.get("target") or step.get("prompt_template") or loop.get("goal") or ".").strip()
            qa_res = run_product_qa(ws_root, target=qa_target, run_tests=bool(params.get("run_tests", True)))
            verdict_prefix = "PASS: " if qa_res.get("passed") else "NEEDS_WORK: "
            summary_txt = (
                f"{verdict_prefix}Product QA {qa_res.get('status')} (score={qa_res.get('score')}, "
                f"defects={len(qa_res.get('defects') or [])})\n\n{qa_res.get('markdown', '')}"
            )
            return {
                "step": step.get("name", "Product QA Gate"),
                "type": "qa",
                "agent_role": "qa_verifier",
                "result": {
                    "ok": True,
                    "passed": bool(qa_res.get("passed")),
                    "text": summary_txt,
                    "qa_report": qa_res,
                },
            }

        if step_type == "recommend_next":
            role = str(step.get("agent_role") or "coordinator")
            model_id = str(step.get("model_id") or loop.get("model_id") or "")
            prompt_tpl = str(
                step.get("prompt_template")
                or "Review all completed workflow outputs, identify the highest-leverage next iteration or improvement, and output a concise, ready-to-run follow-up mission prompt."
            )
            sys_prompt = (
                "You are Sabrina's Finale Next-Step Strategist. Synthesize what this workflow iteration accomplished, "
                "what gaps or next-level improvements remain, and propose a single crisp, self-contained mission prompt "
                "ready to paste directly into 'Enter a mission goal for the swarm...'. "
                "End your response on its own line with: RECOMMENDED_NEXT_GOAL: <single-line actionable swarm mission prompt>"
            )
            user_prompt = (
                f"Current Loop Goal: {loop.get('goal', '')}\n\n"
                f"Finale Task ({step.get('name', 'Recommend Next Step')}): {prompt_tpl}{a2a_section}\n\n"
                f"Prior Step Outputs:\n{context_blob}"
            )
            if self.agent_caller:
                try:
                    llm_res = dict(self.agent_caller(role, model_id, sys_prompt, user_prompt) or {})
                    next_goal = extract_recommended_next_goal(str(llm_res.get("text") or ""), str(loop.get("goal") or ""))
                    llm_res["recommended_next_goal"] = next_goal
                    return {
                        "step": step.get("name", "Recommend Next Step"),
                        "type": "recommend_next",
                        "agent_role": role,
                        "recommended_next_goal": next_goal,
                        "result": llm_res,
                    }
                except Exception as exc:
                    fb_goal = extract_recommended_next_goal("", str(loop.get("goal") or ""))
                    return {
                        "step": step.get("name", "Recommend Next Step"),
                        "type": "recommend_next",
                        "agent_role": role,
                        "recommended_next_goal": fb_goal,
                        "result": {"ok": False, "error": str(exc), "recommended_next_goal": fb_goal},
                    }
            if self.gemini_caller:
                try:
                    text = self.gemini_caller(f"{sys_prompt}\n\n{user_prompt}", None)
                    next_goal = extract_recommended_next_goal(text, str(loop.get("goal") or ""))
                    return {
                        "step": step.get("name", "Recommend Next Step"),
                        "type": "recommend_next",
                        "agent_role": role,
                        "recommended_next_goal": next_goal,
                        "result": {"ok": True, "text": text, "recommended_next_goal": next_goal},
                    }
                except Exception as exc:
                    fb_goal = extract_recommended_next_goal("", str(loop.get("goal") or ""))
                    return {
                        "step": step.get("name", "Recommend Next Step"),
                        "type": "recommend_next",
                        "agent_role": role,
                        "recommended_next_goal": fb_goal,
                        "result": {"ok": False, "error": str(exc), "recommended_next_goal": fb_goal},
                    }
            fb_goal = extract_recommended_next_goal("", str(loop.get("goal") or ""))
            return {
                "step": step.get("name", "Recommend Next Step"),
                "type": "recommend_next",
                "agent_role": role,
                "recommended_next_goal": fb_goal,
                "result": {"ok": True, "text": f"RECOMMENDED_NEXT_GOAL: {fb_goal}", "recommended_next_goal": fb_goal},
            }

        return {"step": step.get("name", "Unknown"), "type": step_type, "result": {"ok": False, "error": f"Unknown step_type {step_type}"}}

    def run_loop_now(self, loop_id: str) -> dict[str, Any]:
        loop = self.get_loop(loop_id)
        if not loop:
            return {"ok": False, "error": "Loop not found"}
        now = time.time()
        iteration = int(loop.get("current_iteration") or 0) + 1
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        with self._db() as db:
            db.execute(
                """
                INSERT INTO automation_runs (
                    id, loop_id, iteration, status, current_step_index,
                    execution_queue_json, edge_counts_json, a2a_events_json,
                    pending_approval_json, step_outputs_json, goal_met, error, started_at, finished_at
                ) VALUES (?, ?, ?, 'running', 0, NULL, '{}', '[]', NULL, '[]', 0, NULL, ?, NULL)
                """,
                (run_id, loop_id, iteration, now),
            )
            db.execute(
                "UPDATE automation_loops SET status='running', current_iteration=?, last_run=?, updated_at=? WHERE id=?",
                (iteration, now, now, loop_id),
            )
        return self._advance_run(run_id)

    def _finalize_run(
        self,
        run: dict[str, Any],
        loop: dict[str, Any],
        idx: int,
        outputs: list[dict[str, Any]],
        edge_counts: dict[str, int],
        a2a_events: list[dict[str, Any]],
        goal_history: list[str] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        run_id = run["id"]
        goal_met = False
        recommended_next_goal = ""
        for out_item in reversed(outputs):
            if recommended_next_goal:
                break
            direct_rec = str(out_item.get("recommended_next_goal") or "").strip()
            if direct_rec:
                recommended_next_goal = direct_rec
                break
            if isinstance(out_item.get("result"), dict):
                res_rec = str(out_item["result"].get("recommended_next_goal") or "").strip()
                if res_rec:
                    recommended_next_goal = res_rec
                    break
                r_txt = str(out_item["result"].get("text") or "")
                if re.search(r"(?:RECOMMENDED_NEXT_GOAL|RECOMMENDED_NEXT_MISSION|NEXT_MISSION_GOAL|NEXT_STEP)\s*:", r_txt, re.IGNORECASE):
                    recommended_next_goal = extract_recommended_next_goal(r_txt, str(loop.get("goal") or ""))
        if not recommended_next_goal:
            last_txt = ""
            for out_item in reversed(outputs):
                if isinstance(out_item.get("result"), dict) and out_item["result"].get("text"):
                    last_txt = str(out_item["result"]["text"])
                    break
            effective_fb_goal = (goal_history[-1] if goal_history else "") or str(loop.get("goal") or "")
            recommended_next_goal = extract_recommended_next_goal(last_txt, effective_fb_goal)

        if loop["loop_type"] in ("goal_until_done", "autonomous_a2a"):
            for out_item in reversed(outputs):
                res_d = out_item.get("result")
                if isinstance(res_d, dict):
                    txt = str(res_d.get("text", "")).lower()
                    if '"goal_met": true' in txt or "goal_met=true" in txt or txt.lstrip().startswith("pass:"):
                        goal_met = True
                        break
                    if out_item.get("type") == "recommend_next" and res_d.get("ok", True):
                        goal_met = True
                        break
            if not goal_met and any(ev.get("condition") == "on_pass" for ev in a2a_events):
                if outputs and isinstance(outputs[-1].get("result"), dict) and outputs[-1]["result"].get("ok", True):
                    goal_met = True
            if int(run["iteration"]) >= int(loop["max_iterations"]):
                goal_met = True

        next_run = None
        new_loop_status = "completed" if (loop["loop_type"] == "goal_until_done" and goal_met) else "idle"
        new_enabled = 0 if new_loop_status == "completed" else (1 if loop["enabled"] else 0)
        if new_enabled and loop["loop_type"] == "recurring_interval":
            next_run = now + int(loop["interval_minutes"]) * 60

        with self._db() as db:
            db.execute(
                """
                UPDATE automation_runs
                SET status='completed', current_step_index=?, execution_queue_json=NULL,
                    edge_counts_json=?, a2a_events_json=?, pending_approval_json=NULL,
                    step_outputs_json=?, goal_met=?, finished_at=?
                WHERE id=?
                """,
                (
                    idx,
                    json.dumps(edge_counts),
                    json.dumps(a2a_events),
                    json.dumps(outputs),
                    1 if goal_met else 0,
                    now,
                    run_id,
                ),
            )
            db.execute(
                "UPDATE automation_loops SET status=?, enabled=?, next_run=?, updated_at=? WHERE id=?",
                (new_loop_status, new_enabled, next_run, now, loop["id"]),
            )
        return {
            "ok": True,
            "status": "completed",
            "run_id": run_id,
            "goal_met": goal_met,
            "recommended_next_goal": recommended_next_goal,
            "goal_history": list(goal_history) if goal_history else ([str(loop.get("goal") or "")] if loop.get("goal") else []),
            "outputs": outputs,
            "a2a_events": a2a_events,
        }

    def _advance_run_graph(
        self,
        run: dict[str, Any],
        loop: dict[str, Any],
        steps: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        approved_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = run["id"]
        outputs: list[dict[str, Any]] = list(run.get("step_outputs") or [])
        edge_counts: dict[str, int] = {str(k): int(v) for k, v in (run.get("edge_counts") or {}).items()}
        a2a_events: list[dict[str, Any]] = list(run.get("a2a_events") or [])

        id_map: dict[str, tuple[int, dict[str, Any]]] = {}
        for i, st in enumerate(steps):
            sid = str(st.get("id") or f"step_{i}")
            id_map[sid] = (i, st)
            id_map.setdefault(f"step_{i}", (i, st))

        def _canon_id(raw_id: str) -> str:
            if raw_id == "loop_root":
                return "loop_root"
            pair = id_map.get(raw_id)
            return str(pair[1].get("id") or f"step_{pair[0]}") if pair else raw_id

        def _node_name(nid: str) -> str:
            if nid == "loop_root":
                return str(loop.get("name") or "Loop Trigger")
            pair = id_map.get(nid)
            return str(pair[1].get("name") or nid) if pair else nid

        norm_edges: list[dict[str, Any]] = []
        for e in edges:
            src = _canon_id(str(e.get("from") or ""))
            dst = _canon_id(str(e.get("to") or ""))
            if (src == "loop_root" or src in id_map) and (dst == "loop_root" or dst in id_map):
                norm_edges.append({**e, "from": src, "to": dst})

        saved_queue = run.get("execution_queue")
        if isinstance(saved_queue, list):
            queue: list[dict[str, Any]] = list(saved_queue)
        else:
            queue = []
            root_edges = [e for e in norm_edges if e["from"] == "loop_root" and e["to"] != "loop_root"]
            if root_edges:
                for re_item in root_edges:
                    eid = re_item["id"]
                    edge_counts[eid] = edge_counts.get(eid, 0) + 1
                    hop = edge_counts[eid]
                    env = self._format_a2a_envelope(re_item, _node_name("loop_root"), _node_name(re_item["to"]), outputs, hop)
                    a2a_events.append(
                        {
                            k: env[k]
                            for k in ("edge_id", "from", "from_name", "to", "to_name", "comm_type", "condition", "hop", "instruction")
                        }
                    )
                    queue.append({"node_id": re_item["to"], "incoming_edge": re_item, "incoming_a2a": env})
            else:
                non_loop_targets = {
                    e["to"]
                    for e in norm_edges
                    if e["from"] != "loop_root" and e["to"] != "loop_root" and e["comm_type"] != "loop" and e["from"] != e["to"]
                }
                entry_ids = [str(s["id"]) for s in steps if str(s["id"]) not in non_loop_targets]
                if not entry_ids and steps:
                    entry_ids = [str(steps[0]["id"])]
                for eid_node in entry_ids:
                    queue.append({"node_id": eid_node, "incoming_edge": None, "incoming_a2a": None})

        forward_parents: dict[str, set[str]] = {}
        for e in norm_edges:
            if e["from"] != "loop_root" and e["to"] != "loop_root" and e["comm_type"] != "loop" and e["from"] != e["to"]:
                forward_parents.setdefault(e["to"], set()).add(e["from"])

        resumed_first = approved_override is not None
        max_hops = max(32, len(steps) * 8)
        hops = 0
        defer_guard = 0
        last_step_idx = int(run.get("current_step_index") or 0)
        active_goal = str(loop.get("goal") or "")
        goal_history: list[str] = [active_goal] if active_goal else []
        latest_next_goal = ""

        while queue and hops < max_hops:
            item = queue.pop(0)
            if isinstance(item.get("evolved_goal"), str) and item["evolved_goal"].strip():
                active_goal = item["evolved_goal"].strip()
            node_id = _canon_id(str(item.get("node_id") or ""))
            pair = id_map.get(node_id)
            if not pair:
                continue

            parents_needed = forward_parents.get(node_id) or set()
            if len(parents_needed) > 1 and queue and defer_guard <= len(steps) * 2:
                queued_ids = {_canon_id(str(q.get("node_id") or "")) for q in queue}
                if parents_needed & queued_ids:
                    queue.append(item)
                    defer_guard += 1
                    continue
            defer_guard = 0

            step_idx, step = pair
            last_step_idx = step_idx
            inc_edges = list(
                item.get("incoming_edges")
                or ([item["incoming_edge"]] if isinstance(item.get("incoming_edge"), dict) else [])
            )
            inc_a2a_batch = list(
                item.get("incoming_a2a_batch")
                or ([item["incoming_a2a"]] if isinstance(item.get("incoming_a2a"), dict) else [])
            )
            use_override = resumed_first
            resumed_first = False

            params = dict(approved_override if (use_override and approved_override is not None) else (step.get("params") or {}))
            if not use_override:
                needs_hitl, reason = self._needs_approval(loop, step, params)
                esc_edge = next((ed for ed in inc_edges if ed.get("comm_type") == "escalate_hitl"), None)
                if not needs_hitl and esc_edge and loop.get("approval_policy") != "never":
                    needs_hitl = True
                    reason = (
                        f"A2A Escalation from '{_node_name(_canon_id(str(esc_edge.get('from') or '')))}' "
                        f"to '{_node_name(node_id)}' requires operator approval"
                    )
                if needs_hitl:
                    paused_queue = [item] + queue
                    pending = {
                        "step_index": step_idx,
                        "node_id": node_id,
                        "step_name": step.get("name", f"Step {step_idx + 1}"),
                        "connector_id": step.get("connector_id", ""),
                        "action": step.get("action", ""),
                        "resolved_params": params,
                        "incoming_a2a": inc_a2a_batch[0] if len(inc_a2a_batch) == 1 else inc_a2a_batch,
                        "reason": reason,
                    }
                    with self._db() as db:
                        db.execute(
                            """
                            UPDATE automation_runs
                            SET status='waiting_approval', current_step_index=?, execution_queue_json=?,
                                edge_counts_json=?, a2a_events_json=?, pending_approval_json=?, step_outputs_json=?
                            WHERE id=?
                            """,
                            (
                                step_idx,
                                json.dumps(paused_queue),
                                json.dumps(edge_counts),
                                json.dumps(a2a_events),
                                json.dumps(pending),
                                json.dumps(outputs),
                                run_id,
                            ),
                        )
                        db.execute(
                            "UPDATE automation_loops SET status='waiting_approval', updated_at=? WHERE id=?",
                            (time.time(), loop["id"]),
                        )
                    return {
                        "ok": True,
                        "status": "waiting_approval",
                        "run_id": run_id,
                        "pending_approval": pending,
                        "a2a_events": a2a_events,
                    }

            step_loop_view = {**loop, "goal": active_goal}
            step_res = self._execute_single_step(
                step_loop_view,
                step,
                outputs,
                override_params=params,
                incoming_a2a=inc_a2a_batch if len(inc_a2a_batch) > 1 else (inc_a2a_batch[0] if inc_a2a_batch else None),
            )
            step_res["node_id"] = node_id
            step_res["active_goal"] = active_goal
            if inc_a2a_batch:
                step_res["a2a_from"] = {
                    "from": inc_a2a_batch[-1].get("from"),
                    "comm_type": inc_a2a_batch[-1].get("comm_type"),
                    "hop": inc_a2a_batch[-1].get("hop"),
                }
                if len(inc_a2a_batch) > 1:
                    step_res["a2a_coalesced_from"] = [
                        {"from": x.get("from"), "comm_type": x.get("comm_type"), "hop": x.get("hop")}
                        for x in inc_a2a_batch
                    ]
            outputs.append(step_res)
            hops += 1

            res_obj = step_res.get("result") if isinstance(step_res.get("result"), dict) else {}
            ok = bool(res_obj.get("ok", True))
            raw_txt = str(res_obj.get("text") if "text" in res_obj else json.dumps(res_obj, default=str))
            txt_low = raw_txt.lower()
            cand_next = str(
                step_res.get("recommended_next_goal")
                or (res_obj.get("recommended_next_goal") if isinstance(res_obj, dict) else "")
                or ""
            ).strip()
            if not cand_next and re.search(
                r"(?:RECOMMENDED_NEXT_GOAL|RECOMMENDED_NEXT_MISSION|NEXT_MISSION_GOAL|NEXT_STEP)\s*:",
                raw_txt,
                re.IGNORECASE,
            ):
                cand_next = extract_recommended_next_goal(raw_txt, active_goal)
            if cand_next:
                latest_next_goal = cand_next

            explicit_pass = (
                txt_low.lstrip().startswith("pass:")
                or '"goal_met": true' in txt_low
                or "goal_met=true" in txt_low
            )
            needs_work = (not ok) or (
                not explicit_pass
                and any(
                    marker in txt_low
                    for marker in (
                        "needs_more_work",
                        "needs_work",
                        "goal_met=false",
                        '"goal_met": false',
                        "fail:",
                        "revise",
                        "loop_back",
                        "issues_found",
                    )
                )
            )
            low_conf = (
                "low_confidence" in txt_low
                or "uncertain" in txt_low
                or (res_obj.get("logprob") is not None and float(res_obj["logprob"]) < -0.35)
            )

            out_edges = [e for e in norm_edges if e["from"] == node_id]
            if not out_edges:
                continue

            def _matches(edge_spec: dict[str, Any]) -> bool:
                cnt = edge_counts.get(edge_spec["id"], 0)
                comm = edge_spec["comm_type"]
                cond = edge_spec["condition"]
                is_self_recycle = edge_spec["to"] == "loop_root"
                max_lp = int(edge_spec.get("max_loops") or (1 if is_self_recycle else 3))
                if (comm == "loop" or is_self_recycle or cond in ("on_needs_work", "on_error", "on_low_confidence")) and cnt >= max_lp:
                    return False
                if cond == "on_needs_work":
                    return needs_work
                if cond == "on_error":
                    return not ok
                if cond == "on_pass":
                    return ok and not needs_work
                if cond == "on_low_confidence":
                    return low_conf
                if cond == "custom":
                    expr = str(edge_spec.get("condition_expr") or "").strip().lower()
                    if (comm == "loop" or is_self_recycle) and cnt >= max_lp:
                        return False
                    return bool(expr and expr in txt_low)
                return True

            priority_edges = [
                e for e in out_edges
                if e["to"] != "loop_root"
                and (e["comm_type"] == "loop" or e["condition"] in ("on_needs_work", "on_error", "on_low_confidence", "custom"))
            ]
            forward_edges = [e for e in out_edges if e not in priority_edges]
            matched_priority = [e for e in priority_edges if _matches(e)]
            chosen_edges = matched_priority if matched_priority else [e for e in forward_edges if _matches(e)]

            transient_opts = getattr(self, "_transient_loop_opts", {})
            loop_ceiling = int(
                loop.get("max_payload_bytes")
                or transient_opts.get((loop["id"], "max_payload_bytes"))
                or 24576
            )
            raw_dt = (
                loop.get("sbv_drift_threshold")
                if loop.get("sbv_drift_threshold") is not None
                else transient_opts.get((loop["id"], "sbv_drift_threshold"))
            )
            drift_threshold = float(raw_dt if raw_dt is not None else 0.16)
            anchor_goal = str(loop.get("goal") or "")

            for chosen in chosen_edges:
                eid = chosen["id"]
                edge_counts[eid] = edge_counts.get(eid, 0) + 1
                hop = edge_counts[eid]
                target_id = chosen["to"]
                edge_ceiling = int(chosen.get("max_payload_bytes") or loop_ceiling)
                env = self._format_a2a_envelope(
                    chosen,
                    _node_name(node_id),
                    _node_name(target_id),
                    outputs,
                    hop,
                    max_payload_bytes=edge_ceiling,
                )
                a2a_events.append(
                    {
                        k: env[k]
                        for k in (
                            "edge_id", "from", "from_name", "to", "to_name",
                            "comm_type", "condition", "hop", "payload_bytes", "budget_capped", "instruction",
                        )
                    }
                )

                if target_id == "loop_root":
                    raw_evolved = latest_next_goal or extract_recommended_next_goal(raw_txt, active_goal)
                    sbv_check = stabilize_evolved_goal(
                        anchor_goal=anchor_goal,
                        candidate_goal=raw_evolved,
                        goal_history=goal_history,
                        drift_threshold=drift_threshold,
                    )
                    a2a_events[-1]["sbv_similarity"] = sbv_check["sbv_similarity"]
                    a2a_events[-1]["drift_intercepted"] = sbv_check["drift_intercepted"]
                    a2a_events[-1]["oscillation_halted"] = sbv_check["oscillation_halted"]
                    if sbv_check["oscillation_halted"]:
                        a2a_events[-1]["evolved_goal"] = raw_evolved
                        continue
                    active_goal = sbv_check["effective_goal"]
                    if not goal_history or goal_history[-1] != active_goal:
                        goal_history.append(active_goal)
                    a2a_events[-1]["evolved_goal"] = active_goal

                    re_entries = [e for e in norm_edges if e["from"] == "loop_root" and e["to"] != "loop_root"]
                    if re_entries:
                        for re_edge in re_entries:
                            re_eid = re_edge["id"]
                            edge_counts[re_eid] = edge_counts.get(re_eid, 0) + 1
                            re_hop = edge_counts[re_eid]
                            re_env = self._format_a2a_envelope(
                                re_edge,
                                _node_name("loop_root"),
                                _node_name(re_edge["to"]),
                                outputs,
                                re_hop,
                                max_payload_bytes=int(re_edge.get("max_payload_bytes") or loop_ceiling),
                            )
                            re_env["instruction"] = (
                                f"[Self-Loop Cycle #{hop + 1}] Evolved Mission Goal: {active_goal}"
                                + (f" · {re_env['instruction']}" if re_env.get("instruction") else "")
                            )
                            a2a_events.append(
                                {
                                    k: re_env[k]
                                    for k in (
                                        "edge_id", "from", "from_name", "to", "to_name",
                                        "comm_type", "condition", "hop", "payload_bytes", "budget_capped", "instruction",
                                    )
                                }
                            )
                            queue.append(
                                {
                                    "node_id": re_edge["to"],
                                    "incoming_edge": re_edge,
                                    "incoming_edges": [re_edge],
                                    "incoming_a2a": re_env,
                                    "incoming_a2a_batch": [re_env],
                                    "evolved_goal": active_goal,
                                }
                            )
                    elif steps:
                        first_sid = str(steps[0]["id"])
                        queue.append(
                            {
                                "node_id": first_sid,
                                "incoming_edge": chosen,
                                "incoming_edges": [chosen],
                                "incoming_a2a": env,
                                "incoming_a2a_batch": [env],
                                "evolved_goal": active_goal,
                            }
                        )
                    continue

                existing_q = next((q for q in queue if _canon_id(str(q.get("node_id") or "")) == target_id), None)
                if existing_q is not None:
                    q_edges = list(
                        existing_q.get("incoming_edges")
                        or ([existing_q["incoming_edge"]] if isinstance(existing_q.get("incoming_edge"), dict) else [])
                    )
                    q_batch = list(
                        existing_q.get("incoming_a2a_batch")
                        or ([existing_q["incoming_a2a"]] if isinstance(existing_q.get("incoming_a2a"), dict) else [])
                    )
                    q_edges.append(chosen)
                    q_batch.append(env)
                    existing_q["incoming_edges"] = q_edges
                    existing_q["incoming_a2a_batch"] = q_batch
                    existing_q["incoming_edge"] = chosen
                    existing_q["incoming_a2a"] = env
                else:
                    queue.append(
                        {
                            "node_id": target_id,
                            "incoming_edge": chosen,
                            "incoming_edges": [chosen],
                            "incoming_a2a": env,
                            "incoming_a2a_batch": [env],
                        }
                    )

        return self._finalize_run(
            run, loop, last_step_idx + 1, outputs, edge_counts, a2a_events, goal_history=goal_history
        )

    def _advance_run(self, run_id: str, approved_override: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._db() as db:
            run_row = db.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        run = self._decode_run(run_row)
        if not run:
            return {"ok": False, "error": "Run not found"}
        loop = self.get_loop(run["loop_id"])
        if not loop:
            return {"ok": False, "error": "Loop not found"}

        steps = loop.get("steps") or []
        edges = list(loop.get("edges") or [])
        if steps and (edges or run.get("execution_queue")):
            return self._advance_run_graph(run, loop, steps, edges, approved_override=approved_override)

        outputs: list[dict[str, Any]] = list(run.get("step_outputs") or [])
        idx = int(run.get("current_step_index") or 0)
        while idx < len(steps):
            step = steps[idx]
            params = dict(
                approved_override
                if (approved_override is not None and idx == run.get("current_step_index"))
                else (step.get("params") or {})
            )
            if approved_override is None or idx != run.get("current_step_index"):
                needs_hitl, reason = self._needs_approval(loop, step, params)
                if needs_hitl:
                    pending = {
                        "step_index": idx,
                        "step_name": step.get("name", f"Step {idx + 1}"),
                        "connector_id": step.get("connector_id", ""),
                        "action": step.get("action", ""),
                        "resolved_params": params,
                        "reason": reason,
                    }
                    with self._db() as db:
                        db.execute(
                            "UPDATE automation_runs SET status='waiting_approval', current_step_index=?, pending_approval_json=?, step_outputs_json=? WHERE id=?",
                            (idx, json.dumps(pending), json.dumps(outputs), run_id),
                        )
                        db.execute(
                            "UPDATE automation_loops SET status='waiting_approval', updated_at=? WHERE id=?",
                            (time.time(), loop["id"]),
                        )
                    return {"ok": True, "status": "waiting_approval", "run_id": run_id, "pending_approval": pending}

            step_res = self._execute_single_step(loop, step, outputs, override_params=params)
            outputs.append(step_res)
            idx += 1

        return self._finalize_run(run, loop, idx, outputs, {}, [])

    def decide_approval(
        self,
        run_id: str,
        approve: bool,
        edited_params: dict[str, Any] | None = None,
        feedback: str = "",
    ) -> dict[str, Any]:
        with self._db() as db:
            row = db.execute("SELECT * FROM automation_runs WHERE id=?", (run_id,)).fetchone()
        run = self._decode_run(row)
        if not run or run["status"] != "waiting_approval":
            return {"ok": False, "error": "Run is not waiting for approval"}

        if approve:
            pending = run.get("pending_approval") or {}
            params_to_use = edited_params if edited_params is not None else (pending.get("resolved_params") or {})
            return self._advance_run(run_id, approved_override=params_to_use)

        now = time.time()
        outputs = list(run.get("step_outputs") or [])
        outputs.append(
            {
                "step": "HITL Approval",
                "type": "approval",
                "result": {"ok": False, "rejected": True, "feedback": feedback or "Rejected by operator"},
            }
        )
        with self._db() as db:
            db.execute(
                "UPDATE automation_runs SET status='rejected', pending_approval_json=NULL, step_outputs_json=?, error=?, finished_at=? WHERE id=?",
                (json.dumps(outputs), feedback or "Rejected by operator", now, run_id),
            )
            db.execute(
                "UPDATE automation_loops SET status='idle', updated_at=? WHERE id=?",
                (now, run["loop_id"]),
            )
        return {"ok": True, "status": "rejected", "run_id": run_id}

    def design_workflow_with_gemini(
        self,
        goal: str,
        answers: dict[str, Any] | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.gemini_caller:
            return {"ok": False, "error": "Google Gemini API key is not configured in Models & API Keys."}
        connectors_summary = [
            {
                "id": c["id"],
                "name": c["name"],
                "category": c["category"],
                "configured": c["configured"],
                "actions": [a["name"] for a in c["actions"]],
            }
            for c in self.connectors.list_connectors()
        ]
        prompt = (
            "You are Sabrina Console's AI Automation Co-Designer.\n"
            "Help the user design an agent automation loop by asking 1 to 3 concise multiple-choice questions (status='needs_input') "
            "until you have enough detail to produce a concrete executable blueprint (status='ready').\n"
            "Prefer read-only steps unless the user asks for write actions, and set approval_policy='always_for_writes'.\n"
            f"Available Connectors: {json.dumps(connectors_summary)}\n"
            f"User Goal: {goal}\n"
            f"Prior Q&A History: {json.dumps(history or [])}\n"
            f"Latest Selected Answers: {json.dumps(answers or {})}\n"
        )
        try:
            raw = self.gemini_caller(prompt, CODESIGNER_RESPONSE_SCHEMA)
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            return {"ok": True, **parsed}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def start_background_scheduler(self, poll_seconds: float = 15.0) -> None:
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.wait(poll_seconds):
                now = time.time()
                for lp in self.list_loops():
                    if not lp["enabled"] or lp["status"] in ("running", "waiting_approval"):
                        continue
                    nxt = lp.get("next_run")
                    if nxt and float(nxt) <= now:
                        try:
                            self.run_loop_now(lp["id"])
                        except Exception:
                            pass

        self._worker_thread = threading.Thread(target=_loop, name="sabrina-automation-loop", daemon=True)
        self._worker_thread.start()

    def stop_background_scheduler(self) -> None:
        self._stop_event.set()
