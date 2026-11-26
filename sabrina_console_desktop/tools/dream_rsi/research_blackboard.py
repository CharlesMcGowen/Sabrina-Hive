"""SQLite episode ledger for Sabrina's D&D / weight-space research loop.

This is deliberately separate from the pentest attack-surface Blackboard and the
Coven live-task queue. It records metadata and artifact references; model weights
remain in immutable checkpoint files outside the database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "dnd_rsi_blackboard.sqlite3"
STATUSES = {"draft", "ready", "claimed", "attempted", "accepted", "rejected", "candidate", "promoted"}


class ResearchBlackboard:
    """Append-only episode history with atomic claims and explicit promotion gates."""

    def __init__(self, db_path: str | os.PathLike[str] | None = None):
        self.db_path = Path(db_path or os.environ.get("SABRINA_DND_RSI_DB", DEFAULT_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._session() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    episode_id TEXT PRIMARY KEY,
                    parent_episode_id TEXT,
                    status TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    challenge TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    rubric_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    dm_model TEXT NOT NULL,
                    worker_model TEXT NOT NULL,
                    base_revision TEXT NOT NULL,
                    claimed_by TEXT,
                    lease_until REAL,
                    attempt_ref TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT,
                    evaluator TEXT,
                    evaluator_version TEXT,
                    score REAL,
                    training_eligible INTEGER NOT NULL DEFAULT 0,
                    candidate_revision TEXT,
                    candidate_ref TEXT,
                    manifest_ref TEXT,
                    heldout_report_ref TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY(parent_episode_id) REFERENCES episodes(episode_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    episode_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY(episode_id) REFERENCES episodes(episode_id)
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_status_created
                    ON episodes(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_episode
                    ON events(episode_id, event_id);
            """)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _event(self, db: sqlite3.Connection, episode_id: str, event_type: str,
               actor: str, payload: dict[str, Any]) -> None:
        db.execute("INSERT INTO events(episode_id,event_type,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
                   (episode_id, event_type, actor, self._json(payload), time.time()))

    def create_episode(self, *, goal: str, challenge: str, scope: dict[str, Any],
                       rubric: dict[str, Any], budget: dict[str, Any], dm_model: str,
                       worker_model: str, base_revision: str,
                       parent_episode_id: str | None = None, actor: str = "operator",
                       episode_id: str | None = None) -> str:
        fields = (goal, challenge, dm_model, worker_model, base_revision)
        if any(not isinstance(v, str) or not v.strip() for v in fields):
            raise ValueError("goal, challenge, model names, and base_revision must be non-empty strings")
        episode_id = episode_id or str(uuid.uuid4())
        now = time.time()
        with self._session() as db:
            db.execute("""INSERT INTO episodes(
                episode_id,parent_episode_id,status,goal,challenge,scope_json,rubric_json,budget_json,
                dm_model,worker_model,base_revision,created_at,updated_at)
                VALUES(?,?,'draft',?,?,?,?,?,?,?,?,?,?)""",
                (episode_id, parent_episode_id, goal, challenge, self._json(scope), self._json(rubric),
                 self._json(budget), dm_model, worker_model, base_revision, now, now))
            self._event(db, episode_id, "created", actor, {"status": "draft", "base_revision": base_revision})
        return episode_id

    def publish(self, episode_id: str, actor: str = "dm") -> None:
        self._transition(episode_id, "draft", "ready", actor, "published")

    def record_job_event(self, episode_id: str, *, job_id: str, state: str,
                         evidence: dict[str, Any] | None = None) -> None:
        """Link queue execution to research evidence without granting acceptance."""
        with self._session() as db:
            if db.execute("SELECT 1 FROM episodes WHERE episode_id=?", (episode_id,)).fetchone() is None:
                raise ValueError("Unknown research episode")
            self._event(db, episode_id, "gpu_job_" + state, "gpu_job_queue",
                        {"job_id": job_id, "state": state, "evidence": evidence or {}})

    def claim_next(self, worker_id: str, lease_seconds: int = 900) -> dict[str, Any] | None:
        if not worker_id.strip() or lease_seconds < 1:
            raise ValueError("worker_id and positive lease_seconds are required")
        now = time.time()
        with self._session() as db:
            # Serialize the read/claim pair so two workers cannot take one episode.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM episodes WHERE status='ready'
                OR (status='claimed' AND lease_until < ?) ORDER BY created_at LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE episodes SET status='claimed',claimed_by=?,lease_until=?,updated_at=? WHERE episode_id=?",
                       (worker_id, now + lease_seconds, now, row["episode_id"]))
            self._event(db, row["episode_id"], "claimed", worker_id, {"lease_until": now + lease_seconds})
            return self.get_episode(row["episode_id"], db=db)

    def submit_attempt(self, episode_id: str, *, worker_id: str, summary: str,
                       attempt_ref: str, evidence_refs: list[str], model_revision: str) -> None:
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,claimed_by,lease_until FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
            if row is None or row["status"] != "claimed" or row["claimed_by"] != worker_id:
                raise ValueError("episode is not claimed by this worker")
            if row["lease_until"] is not None and row["lease_until"] < time.time():
                raise ValueError("worker claim lease expired")
            now = time.time()
            changed = db.execute("""UPDATE episodes SET status='attempted',attempt_ref=?,evidence_json=?,summary=?,
                updated_at=? WHERE episode_id=? AND status='claimed' AND claimed_by=?""",
                (attempt_ref, self._json(evidence_refs), summary, now, episode_id, worker_id)).rowcount
            if changed != 1:
                raise ValueError("episode claim changed before attempt was recorded")
            self._event(db, episode_id, "attempted", worker_id,
                        {"attempt_ref": attempt_ref, "evidence_refs": evidence_refs, "model_revision": model_revision})

    def evaluate(self, episode_id: str, *, evaluator: str, evaluator_version: str,
                 score: float, accepted: bool, rationale: str) -> None:
        if not 0.0 <= score <= 1.0:
            raise ValueError("score must be between 0 and 1")
        next_status = "accepted" if accepted else "rejected"
        self._transition(episode_id, "attempted", next_status, evaluator, "evaluated", {
            "score": score, "evaluator_version": evaluator_version, "rationale": rationale,
        }, extra={"evaluator": evaluator, "evaluator_version": evaluator_version,
                  "score": score, "training_eligible": int(accepted)})

    def attach_candidate(self, episode_id: str, *, candidate_revision: str,
                         candidate_ref: str, manifest_ref: str, actor: str = "trainer") -> None:
        self._transition(episode_id, "accepted", "candidate", actor, "candidate_attached", {
            "candidate_revision": candidate_revision, "candidate_ref": candidate_ref,
            "manifest_ref": manifest_ref,
        }, extra={"candidate_revision": candidate_revision, "candidate_ref": candidate_ref,
                  "manifest_ref": manifest_ref})

    def promote(self, episode_id: str, *, heldout_report_ref: str, passed: bool,
                actor: str = "promoter") -> None:
        if not heldout_report_ref:
            raise ValueError("held-out report reference is required")
        if not passed:
            self._transition(episode_id, "candidate", "rejected", actor, "promotion_rejected",
                             {"heldout_report_ref": heldout_report_ref},
                             extra={"heldout_report_ref": heldout_report_ref, "training_eligible": 0})
            return
        self._transition(episode_id, "candidate", "promoted", actor, "promoted",
                         {"heldout_report_ref": heldout_report_ref},
                         extra={"heldout_report_ref": heldout_report_ref})

    def _transition(self, episode_id: str, expected: str, new: str, actor: str,
                    event_type: str, payload: dict[str, Any] | None = None,
                    extra: dict[str, Any] | None = None) -> None:
        if new not in STATUSES:
            raise ValueError(f"invalid status: {new}")
        now = time.time()
        updates = {"status": new, "updated_at": now, **(extra or {})}
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
            if row is None or row["status"] != expected:
                raise ValueError(f"expected {expected} episode")
            assignments = ",".join(f"{key}=?" for key in updates)
            changed = db.execute(f"UPDATE episodes SET {assignments} WHERE episode_id=? AND status=?",
                                 (*updates.values(), episode_id, expected)).rowcount
            if changed != 1:
                raise ValueError(f"episode transition lost race: expected {expected}")
            self._event(db, episode_id, event_type, actor, {"from": expected, "to": new, **(payload or {})})

    def get_episode(self, episode_id: str, *, db: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        own = db is None
        conn = db or self._connect()
        try:
            row = conn.execute("SELECT * FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            for key in ("scope_json", "rubric_json", "budget_json", "evidence_json"):
                result[key.removesuffix("_json")] = json.loads(result.pop(key))
            result["training_eligible"] = bool(result["training_eligible"])
            return result
        finally:
            if own:
                conn.close()

    def events(self, episode_id: str) -> list[dict[str, Any]]:
        with self._session() as db:
            rows = db.execute("SELECT * FROM events WHERE episode_id=? ORDER BY event_id", (episode_id,)).fetchall()
            return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]
