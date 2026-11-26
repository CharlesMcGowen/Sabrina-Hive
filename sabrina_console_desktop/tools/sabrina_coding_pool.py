"""Durable 4-worker coding pool admission, authoritative verification, research claim tracking,
and Sabrina's 7-action reasoning allocator.

Trusted host code supplies qualified backends, collector and verifier callbacks.
Only dispatch() is a model-facing API. Raw prompts/replies stay in the run audit.
All workers sharing a pool must use the same local SQLite database.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS coding_pool_objectives(
    id TEXT PRIMARY KEY,
    contract TEXT NOT NULL,
    hash TEXT NOT NULL,
    created REAL NOT NULL,
    deadline REAL NOT NULL,
    expires REAL NOT NULL,
    report_state TEXT NOT NULL DEFAULT 'pending',
    objective_verification TEXT NOT NULL DEFAULT 'unverified',
    combined_revision TEXT
);
CREATE TABLE IF NOT EXISTS coding_pool_tasks(
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    contract TEXT NOT NULL,
    hash TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coding_pool_attempts(
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    parent_id TEXT,
    operation_id TEXT UNIQUE NOT NULL,
    request_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    backend_hash TEXT NOT NULL,
    qualification_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    state TEXT NOT NULL,
    delivery TEXT NOT NULL,
    workspace TEXT UNIQUE NOT NULL,
    created REAL NOT NULL,
    deadline REAL NOT NULL,
    owner TEXT NOT NULL,
    stop_confirmed INTEGER NOT NULL DEFAULT 0,
    revision TEXT,
    verdict TEXT,
    requested_provider TEXT NOT NULL DEFAULT '',
    requested_model TEXT NOT NULL DEFAULT '',
    requested_interface TEXT NOT NULL DEFAULT '',
    actual_provider TEXT NOT NULL DEFAULT '',
    actual_model TEXT NOT NULL DEFAULT 'unknown',
    actual_interface TEXT NOT NULL DEFAULT '',
    fallback_occurred INTEGER NOT NULL DEFAULT 0,
    observed_latency_ms REAL,
    usage_json TEXT
);
CREATE TABLE IF NOT EXISTS coding_pool_reviews(
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    objective_id TEXT NOT NULL,
    reviewer_provider TEXT NOT NULL,
    reviewer_run_id TEXT NOT NULL,
    candidate_artifact_hash TEXT NOT NULL,
    delivery TEXT NOT NULL,
    review_verdict TEXT NOT NULL,
    failed_criteria TEXT NOT NULL DEFAULT '[]',
    disagreements TEXT NOT NULL DEFAULT '[]',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS coding_pool_claims(
    id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unresolved',
    supporting_sources TEXT NOT NULL DEFAULT '[]',
    contradicting_sources TEXT NOT NULL DEFAULT '[]',
    worker_agreements TEXT NOT NULL DEFAULT '[]',
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS coding_pool_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created REAL NOT NULL
);
"""
ACTIVE = ("reserved", "submitting", "running", "uncertain", "cancel_requested")

SABRINA_ALLOCATION_ACTIONS = (
    "acquire_fact",
    "delegate_implementation",
    "request_alternative",
    "review_candidate",
    "request_verification",
    "return_for_correction",
    "stop",
)


class CodingPool:
    def __init__(
        self,
        db_path: Path | str,
        workspace_root: Path | str,
        *,
        providers: dict[str, dict[str, Any]] | None = None,
        profiles: dict[str, Any] | None = None,
        collector: Any = None,
        clock: Any = time.time,
    ):
        self.path = Path(db_path)
        self.root = Path(workspace_root).resolve()
        self.providers = providers or {}
        self.profiles = profiles or {}
        self.collector = collector
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            # Ensure new columns exist on older DBs
            obj_cols = {r[1] for r in db.execute("PRAGMA table_info(coding_pool_objectives)").fetchall()}
            for cname, cdef in (
                ("report_state", "TEXT NOT NULL DEFAULT 'pending'"),
                ("objective_verification", "TEXT NOT NULL DEFAULT 'unverified'"),
                ("combined_revision", "TEXT"),
            ):
                if cname not in obj_cols:
                    db.execute(f"ALTER TABLE coding_pool_objectives ADD COLUMN {cname} {cdef}")

            att_cols = {r[1] for r in db.execute("PRAGMA table_info(coding_pool_attempts)").fetchall()}
            for cname, cdef in (
                ("requested_provider", "TEXT NOT NULL DEFAULT ''"),
                ("requested_model", "TEXT NOT NULL DEFAULT ''"),
                ("requested_interface", "TEXT NOT NULL DEFAULT ''"),
                ("actual_provider", "TEXT NOT NULL DEFAULT ''"),
                ("actual_model", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("actual_interface", "TEXT NOT NULL DEFAULT ''"),
                ("fallback_occurred", "INTEGER NOT NULL DEFAULT 0"),
                ("observed_latency_ms", "REAL"),
                ("usage_json", "TEXT"),
            ):
                if cname not in att_cols:
                    db.execute(f"ALTER TABLE coding_pool_attempts ADD COLUMN {cname} {cdef}")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _event(self, db: sqlite3.Connection, aid: str | None, kind: str, detail: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO coding_pool_events(attempt_id,kind,detail,created) VALUES(?,?,?,?)",
            (aid, kind, json.dumps(detail), self.clock()),
        )

    def create_objective(self, contract: dict[str, Any], objective_id: str | None = None) -> str:
        if (
            not isinstance(contract, dict)
            or not contract.get("profile")
            or not contract.get("profile_hash")
            or not contract.get("allowed_files")
        ):
            raise ValueError("Frozen profile and allowed file manifest required")
        if contract["profile"] not in self.profiles:
            raise ValueError("Maintainer verifier profile unavailable")
        oid = objective_id or uuid.uuid4().hex
        now = self.clock()
        with self.connect() as db:
            db.execute(
                """INSERT INTO coding_pool_objectives
                   (id,contract,hash,created,deadline,expires,report_state,objective_verification,combined_revision)
                   VALUES(?,?,?,?,?,?,'pending','unverified',NULL)""",
                (oid, json.dumps(contract), digest(contract), now, now + 3600, now + 7 * 86400),
            )
        return oid

    def _objective(self, db: sqlite3.Connection, oid: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM coding_pool_objectives WHERE id=?", (oid,)).fetchone()
        if not row:
            raise ValueError("Unknown objective")
        if self.clock() >= row["expires"]:
            raise ValueError("Expired evidence; fresh run required")
        if self.clock() >= row["deadline"]:
            raise ValueError("Objective deadline exhausted")
        if digest(json.loads(row["contract"])) != row["hash"]:
            raise ValueError("Frozen objective modified")
        return row

    def add_task(
        self,
        objective_id: str,
        contract: dict[str, Any],
        dependencies: list[str] | tuple[str, ...] = (),
        task_id: str | None = None,
    ) -> str:
        tid = task_id or uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = self._objective(db, objective_id)
            obj_contract = json.loads(obj["contract"])
            # Permit either exact objective contract match OR a bounded subtask decomposition
            # that preserves profile, profile_hash, and allowed_files while scoping editable_files/criteria.
            if digest(contract) != obj["hash"]:
                same_core = (
                    isinstance(contract, dict)
                    and contract.get("profile") == obj_contract.get("profile")
                    and contract.get("profile_hash") == obj_contract.get("profile_hash")
                    and contract.get("allowed_files") == obj_contract.get("allowed_files")
                    and set(contract.get("editable_files", [])) <= set(obj_contract.get("allowed_files", []))
                    and bool(contract.get("subtask_decomposition"))
                )
                if not same_core:
                    raise ValueError("Task contract cannot change objective scope")
            known = {
                r[0]
                for r in db.execute(
                    "SELECT id FROM coding_pool_tasks WHERE objective_id=?", (objective_id,)
                )
            }
            if not set(dependencies) <= known:
                raise ValueError("Dependencies must be earlier tasks in this objective")
            db.execute(
                "INSERT INTO coding_pool_tasks VALUES(?,?,?,?,?,?)",
                (
                    tid,
                    objective_id,
                    json.dumps(contract),
                    digest(contract),
                    json.dumps(list(dependencies)),
                    "ready",
                ),
            )
        return tid

    def _task(self, db: sqlite3.Connection, tid: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        task = db.execute("SELECT * FROM coding_pool_tasks WHERE id=?", (tid,)).fetchone()
        if not task:
            raise ValueError("Unknown task")
        obj = self._objective(db, task["objective_id"])
        if digest(json.loads(task["contract"])) != task["hash"]:
            raise ValueError("Frozen task modified")
        return task, obj

    def _qualified(self, provider: str) -> tuple[dict[str, Any], int]:
        p = self.providers.get(provider, {})
        capacity = p.get("capacity", 1)
        if p.get("is_simulated") or p.get("execution_mode") == "simulation":
            raise ValueError("Simulated backend is not qualified for live coding pool")
        if not p.get("fingerprint") or not p.get("qualification_id") or not p.get("editing_qualified"):
            raise ValueError("Source-aware editing backend is not qualified")
        if capacity not in (1, 2, 4) or (capacity > 1 and p.get("overlap_verified", 0) < capacity):
            raise ValueError("Concurrency promotion lacks overlapping containment qualification")
        return p, capacity

    def assign(
        self,
        task_id: str,
        provider: str,
        operation_id: str,
        run_id: str,
        parent_id: str | None = None,
        *,
        allow_independent_alternative: bool = False,
        requested_model: str = "provider_default",
        requested_interface: str = "cli",
    ) -> dict[str, Any]:
        if not all(isinstance(v, str) and v for v in (operation_id, run_id, provider)):
            raise ValueError("Explicit IDs required")
        req = digest([task_id, provider, run_id, parent_id])
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM coding_pool_attempts WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if old:
                self._task(db, old["task_id"])
                if old["request_hash"] != req:
                    raise ValueError("Operation ID reused with changed inputs")
                return dict(old)
            task, obj = self._task(db, task_id)
            backend, capacity = self._qualified(provider)
            allowed_states = (
                ("ready", "needs_revision", "assigned")
                if allow_independent_alternative
                else ("ready", "needs_revision")
            )
            if task["state"] not in allowed_states:
                raise ValueError("Task not assignable")
            for dep in json.loads(task["dependencies"]):
                if (
                    db.execute("SELECT state FROM coding_pool_tasks WHERE id=?", (dep,)).fetchone()[0]
                    != "verified"
                ):
                    raise ValueError("Dependency not verified")
            prior = db.execute(
                "SELECT * FROM coding_pool_attempts WHERE task_id=? ORDER BY rowid", (task_id,)
            ).fetchall()
            if len(prior) >= 3:
                raise ValueError("Initial plus two corrections exhausted")
            if prior and not allow_independent_alternative:
                if (
                    not parent_id
                    or prior[-1]["id"] != parent_id
                    or prior[-1]["state"] != "needs_revision"
                ):
                    raise ValueError("Correction requires latest failed verified attempt")
            elif parent_id and not allow_independent_alternative:
                raise ValueError("Unknown parent attempt")
            if allow_independent_alternative and any(r["provider"] == provider for r in prior):
                raise ValueError("Independent alternative requires a distinct worker/provider")
            active = list(
                db.execute(
                    "SELECT * FROM coding_pool_attempts WHERE state IN ('reserved','submitting','running','uncertain','cancel_requested')"
                )
            )
            if len(active) >= 4 or sum(r["provider"] == provider for r in active) >= capacity:
                raise ValueError("Worker capacity unavailable")
            aid = uuid.uuid4().hex
            workspace = str(self.root / aid)
            owner = uuid.uuid4().hex
            db.execute(
                """INSERT INTO coding_pool_attempts
                   (id,task_id,parent_id,operation_id,request_hash,provider,backend_hash,qualification_id,
                    run_id,state,delivery,workspace,created,deadline,owner,requested_provider,requested_model,requested_interface)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    task_id,
                    parent_id,
                    operation_id,
                    req,
                    provider,
                    backend["fingerprint"],
                    backend["qualification_id"],
                    run_id,
                    "reserved",
                    "not_submitted",
                    workspace,
                    now,
                    min(now + 900, obj["deadline"]),
                    owner,
                    provider,
                    requested_model,
                    requested_interface,
                ),
            )
            db.execute("UPDATE coding_pool_tasks SET state='assigned' WHERE id=?", (task_id,))
            self._event(
                db,
                aid,
                "admitted",
                {
                    "qualification_id": backend["qualification_id"],
                    "run_id": run_id,
                    "independent_alternative": allow_independent_alternative,
                },
            )
            return dict(
                db.execute("SELECT * FROM coding_pool_attempts WHERE id=?", (aid,)).fetchone()
            )

    def inspect(self, attempt_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM coding_pool_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown attempt")
            value = dict(row)
            expiry = db.execute(
                "SELECT o.expires FROM coding_pool_objectives o JOIN coding_pool_tasks t ON t.objective_id=o.id WHERE t.id=?",
                (row["task_id"],),
            ).fetchone()[0]
            if self.clock() >= expiry:
                value.update(state="expired", revision=None, verdict=None)
            if row["state"] in ACTIVE and self.clock() >= row["deadline"]:
                value["recovery"] = (
                    "deadline elapsed; reconcile external worker, never resend automatically"
                )
            return value

    def adapter_transition(
        self,
        attempt_id: str,
        owner: str,
        event: str,
        *,
        actual_provider: str | None = None,
        actual_model: str = "unknown",
        actual_interface: str | None = None,
        fallback_occurred: bool = False,
        observed_latency_ms: float | None = None,
        usage: dict[str, Any] | None = None,
        is_simulated: bool = False,
    ) -> None:
        """Trusted adapter only; not dispatched as a model operation."""
        if is_simulated:
            raise ValueError("Simulated response presented as live in coding pool adapter")
        transitions = {
            "submit": ("reserved", "submitting", "uncertain"),
            "observed": ("submitting", "running", "observed"),
            "finished": ("running", "returned", "observed"),
            "uncertain": ("submitting", "uncertain", "uncertain"),
        }
        if event not in transitions:
            raise ValueError("Unknown adapter event")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute(
                "SELECT * FROM coding_pool_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not r or r["owner"] != owner:
                raise ValueError("Wrong worker owner")
            before, after, delivery = transitions[event]
            if r["state"] != before:
                raise ValueError("Invalid transition; no uncertain resend")
            if event == "submit":
                self._task(db, r["task_id"])
                if self.clock() >= r["deadline"]:
                    raise ValueError("Attempt deadline exhausted")
                p, _ = self._qualified(r["provider"])
                if p["fingerprint"] != r["backend_hash"]:
                    raise ValueError("Backend fingerprint changed")
            act_prov = actual_provider or r["actual_provider"] or r["provider"]
            act_mod = str(actual_model or r["actual_model"] or "unknown").strip() or "unknown"
            act_iface = actual_interface or r["actual_interface"] or r["requested_interface"]
            if act_prov in ("gemini", "google") and ("ollama" in str(act_iface).lower() or "qwen" in act_mod.lower()):
                raise ValueError("Local fallback cannot be attributed to Gemini")
            db.execute(
                """UPDATE coding_pool_attempts
                   SET state=?, delivery=?, actual_provider=?, actual_model=?, actual_interface=?,
                       fallback_occurred=?, observed_latency_ms=COALESCE(?, observed_latency_ms),
                       usage_json=COALESCE(?, usage_json)
                   WHERE id=?""",
                (
                    after,
                    delivery,
                    act_prov,
                    act_mod,
                    act_iface,
                    1 if fallback_occurred else int(r["fallback_occurred"] or 0),
                    observed_latency_ms,
                    json.dumps(usage) if usage is not None else None,
                    attempt_id,
                ),
            )
            self._event(
                db,
                attempt_id,
                event,
                {
                    "delivery": delivery,
                    "actual_provider": act_prov,
                    "actual_model": act_mod,
                    "fallback_occurred": bool(fallback_occurred),
                },
            )

    def collect(self, attempt_id: str) -> dict[str, Any]:
        r = self.inspect(attempt_id)
        if r["state"] != "returned" or not self.collector:
            raise ValueError("Collection unavailable or worker not finished")
        with self.connect() as db:
            task, _ = self._task(db, r["task_id"])
        contract = json.loads(task["contract"])
        sig = inspect.signature(self.collector.__call__)
        if "editable_files" in sig.parameters:
            revision = self.collector(
                Path(r["workspace"]),
                contract["allowed_files"],
                baseline_manifest=contract.get("baseline_manifest"),
                editable_files=contract.get("editable_files"),
                readonly_files=contract.get("readonly_files"),
                protected_files=contract.get("protected_files"),
            )
        else:
            revision = self.collector(Path(r["workspace"]), contract["allowed_files"])
        if not isinstance(revision, dict) or not all(
            revision.get(k) for k in ("artifact_hash", "manifest", "artifact_path")
        ):
            raise ValueError("Collector provenance missing")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._task(db, r["task_id"])
            if (
                db.execute(
                    "UPDATE coding_pool_attempts SET state='collected',revision=? WHERE id=? AND state='returned'",
                    (json.dumps(revision), attempt_id),
                ).rowcount
                != 1
            ):
                raise ValueError("Collection raced with state change")
            # Invalidate any earlier objective verification when a new artifact is collected
            db.execute(
                "UPDATE coding_pool_objectives SET objective_verification='unverified' WHERE id=(SELECT objective_id FROM coding_pool_tasks WHERE id=?)",
                (r["task_id"],),
            )
            self._event(db, attempt_id, "collected", {"artifact_hash": revision["artifact_hash"]})
        return revision

    def record_review(
        self,
        attempt_id: str,
        *,
        reviewer_provider: str,
        reviewer_run_id: str,
        received_artifact_hash: str,
        delivery: str,
        review_verdict: str,
        failed_criteria: list[str] | None = None,
        disagreements: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record an independent review of a collected candidate revision.

        Rejects any review where the reviewer did not receive the exact candidate revision
        or where delivery was not observed. Note: Review does NOT verify the task; only
        verify() has verification authority.
        """
        if delivery != "observed":
            raise ValueError("Review rejected: candidate delivery to reviewer was not observed")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute(
                "SELECT * FROM coding_pool_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not r or not r["revision"]:
                raise ValueError("Review requires a collected candidate revision")
            task, obj = self._task(db, r["task_id"])
            rev = json.loads(r["revision"])
            if not received_artifact_hash or received_artifact_hash != rev["artifact_hash"]:
                raise ValueError(
                    "Review rejected: reviewer never received the exact candidate artifact revision"
                )
            if reviewer_provider == r["provider"]:
                raise ValueError("Independent review requires a distinct reviewer provider/worker")
            review_id = "rev_" + uuid.uuid4().hex[:12]
            now = self.clock()
            db.execute(
                """INSERT INTO coding_pool_reviews
                   (id,attempt_id,task_id,objective_id,reviewer_provider,reviewer_run_id,
                    candidate_artifact_hash,delivery,review_verdict,failed_criteria,disagreements,created)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    review_id,
                    attempt_id,
                    r["task_id"],
                    obj["id"],
                    reviewer_provider,
                    reviewer_run_id,
                    rev["artifact_hash"],
                    delivery,
                    review_verdict,
                    json.dumps(list(failed_criteria or [])),
                    json.dumps(list(disagreements or [])),
                    now,
                ),
            )
            self._event(
                db,
                attempt_id,
                "candidate_reviewed",
                {
                    "review_id": review_id,
                    "reviewer_provider": reviewer_provider,
                    "candidate_artifact_hash": rev["artifact_hash"],
                    "review_verdict": review_verdict,
                },
            )
            return {
                "review_id": review_id,
                "attempt_id": attempt_id,
                "candidate_artifact_hash": rev["artifact_hash"],
                "reviewer_provider": reviewer_provider,
                "review_verdict": review_verdict,
                "failed_criteria": list(failed_criteria or []),
                "disagreements": list(disagreements or []),
                "authoritative_verification": False,
            }

    def verify(self, attempt_id: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute(
                "SELECT * FROM coding_pool_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not r or r["state"] != "collected":
                raise ValueError("Immutable collected revision required")
            task, _ = self._task(db, r["task_id"])
            contract = json.loads(task["contract"])
            verifier = self.profiles.get(contract["profile"])
            if not callable(verifier):
                raise ValueError("Trusted verifier unavailable")
            db.execute("UPDATE coding_pool_attempts SET state='verifying' WHERE id=?", (attempt_id,))
        revision = json.loads(r["revision"])
        try:
            if not self.collector or not callable(getattr(self.collector, "validate", None)):
                raise ValueError("Trusted revision validation unavailable")
            self.collector.validate(revision)
            verdict = verifier(revision, contract)
            self.collector.validate(revision)
        except Exception:
            verdict = {"status": "blocked", "reason": "verifier_unavailable"}
        if (
            not isinstance(verdict, dict)
            or verdict.get("status") not in ("passed", "failed", "blocked")
            or (
                verdict.get("status") != "blocked"
                and (
                    verdict.get("artifact_hash") != revision["artifact_hash"]
                    or verdict.get("profile_hash") != contract.get("profile_hash")
                )
            )
        ):
            verdict = {"status": "blocked", "reason": "verifier_provenance_mismatch"}
        state = {"passed": "verified", "failed": "needs_revision", "blocked": "blocked"}[
            verdict["status"]
        ]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._task(db, r["task_id"])
            if (
                db.execute(
                    "UPDATE coding_pool_attempts SET state=?,verdict=? WHERE id=? AND state=?",
                    (state, json.dumps(verdict), attempt_id, "verifying"),
                ).rowcount
                != 1
            ):
                raise ValueError("Verification raced with state change")
            db.execute("UPDATE coding_pool_tasks SET state=? WHERE id=?", (state, r["task_id"]))
            self._event(
                db,
                attempt_id,
                "verification",
                {
                    "status": verdict["status"],
                    "artifact_hash": revision["artifact_hash"],
                    "failed_criteria": verdict.get("failed_criteria", []),
                },
            )
        return verdict

    def invalidate_verification_on_change(self, attempt_id: str, reason: str = "post_verification_artifact_modified") -> dict[str, Any]:
        """Invalidate verification immediately after any artifact change or stale revision detection."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute("SELECT * FROM coding_pool_attempts WHERE id=?", (attempt_id,)).fetchone()
            if not r:
                raise ValueError("Unknown attempt")
            task, obj = self._task(db, r["task_id"])
            invalid_verdict = {
                "status": "failed",
                "reason": reason,
                "invalidated": True,
            }
            db.execute(
                "UPDATE coding_pool_attempts SET state='needs_revision', verdict=? WHERE id=?",
                (json.dumps(invalid_verdict), attempt_id),
            )
            db.execute(
                "UPDATE coding_pool_tasks SET state='needs_revision' WHERE id=?",
                (r["task_id"],),
            )
            db.execute(
                "UPDATE coding_pool_objectives SET objective_verification='invalidated' WHERE id=?",
                (obj["id"],),
            )
            self._event(db, attempt_id, "verification_invalidated", {"reason": reason})
            return invalid_verdict

    def submit_report(self, objective_id: str, report_text: str) -> dict[str, Any]:
        """Record Sabrina's final report while keeping report completion strictly separate from verified objective completion."""
        if not isinstance(report_text, str) or not report_text.strip():
            raise ValueError("Non-empty report_text required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = self._objective(db, objective_id)
            tasks = db.execute(
                "SELECT id, state FROM coding_pool_tasks WHERE objective_id=?", (objective_id,)
            ).fetchall()
            all_tasks_verified = bool(tasks) and all(t["state"] == "verified" for t in tasks)
            obj_ver = obj["objective_verification"] if all_tasks_verified else "unverified"
            claims_verified_in_prose = any(
                phrase in report_text.lower()
                for phrase in ("all tests passed", "checklist verified", "verified and complete", "100% passing")
            )
            false_completion_prevented = bool(claims_verified_in_prose and obj_ver != "verified")
            db.execute(
                "UPDATE coding_pool_objectives SET report_state='completed' WHERE id=?",
                (objective_id,),
            )
            self._event(
                db,
                None,
                "report_submitted",
                {
                    "objective_id": objective_id,
                    "report_state": "completed",
                    "objective_verification": obj_ver,
                    "false_completion_prevented": false_completion_prevented,
                },
            )
            return {
                "objective_id": objective_id,
                "report_state": "completed",
                "objective_verification": obj_ver,
                "independently_verified": obj_ver == "verified",
                "false_completion_prevented": false_completion_prevented,
            }

    def verify_combined_objective(
        self,
        objective_id: str,
        baseline_files: dict[str, bytes] | None = None,
    ) -> dict[str, Any]:
        """Integrate verified task revisions and run the authoritative verifier on the combined revision."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            obj = self._objective(db, objective_id)
            contract = json.loads(obj["contract"])
            tasks = db.execute(
                "SELECT * FROM coding_pool_tasks WHERE objective_id=? ORDER BY rowid", (objective_id,)
            ).fetchall()
            if not tasks or any(t["state"] != "verified" for t in tasks):
                db.execute(
                    "UPDATE coding_pool_objectives SET objective_verification='failed' WHERE id=?",
                    (objective_id,),
                )
                return {
                    "objective_id": objective_id,
                    "objective_verification": "failed",
                    "reason": "unverified_tasks_remain",
                }
            revisions: list[dict[str, Any]] = []
            for t in tasks:
                att = db.execute(
                    "SELECT * FROM coding_pool_attempts WHERE task_id=? AND state='verified' ORDER BY rowid DESC LIMIT 1",
                    (t["id"],),
                ).fetchone()
                if not att or not att["revision"]:
                    raise ValueError(f"Task {t['id']} lacks verified revision")
                revisions.append(json.loads(att["revision"]))

        if len(revisions) == 1 and baseline_files is None:
            combined_rev = revisions[0]
        else:
            if not hasattr(self.collector, "integrate_revisions") or baseline_files is None:
                combined_rev = revisions[-1]
            else:
                combined_rev = self.collector.integrate_revisions(
                    baseline_files,
                    revisions,
                    protected_files=contract.get("protected_files"),
                )

        verifier = self.profiles.get(contract["profile"])
        if not callable(verifier):
            raise ValueError("Trusted verifier unavailable")
        self.collector.validate(combined_rev)
        verdict = verifier(combined_rev, contract)
        self.collector.validate(combined_rev)
        passed = (
            isinstance(verdict, dict)
            and verdict.get("status") == "passed"
            and verdict.get("artifact_hash") == combined_rev["artifact_hash"]
            and verdict.get("profile_hash") == contract.get("profile_hash")
        )
        obj_ver = "verified" if passed else "failed"
        with self.connect() as db:
            db.execute(
                "UPDATE coding_pool_objectives SET objective_verification=?, combined_revision=? WHERE id=?",
                (obj_ver, json.dumps(combined_rev), objective_id),
            )
            self._event(
                db,
                None,
                "combined_verification",
                {
                    "objective_id": objective_id,
                    "objective_verification": obj_ver,
                    "artifact_hash": combined_rev["artifact_hash"],
                },
            )
        return {
            "objective_id": objective_id,
            "objective_verification": obj_ver,
            "combined_artifact_hash": combined_rev["artifact_hash"],
            "verdict": verdict,
        }

    def correct(self, parent_id: str, provider: str, operation_id: str, run_id: str) -> dict[str, Any]:
        parent = self.inspect(parent_id)
        return self.assign(parent["task_id"], provider, operation_id, run_id, parent_id)

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = db.execute(
                "SELECT * FROM coding_pool_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not r:
                raise ValueError("Unknown attempt")
            self._task(db, r["task_id"])
            if r["state"] in ("verified", "cancelled"):
                raise ValueError("Attempt is terminal")
            state = (
                "cancel_requested"
                if r["state"] in ACTIVE and r["state"] != "reserved"
                else "cancelled"
            )
            db.execute("UPDATE coding_pool_attempts SET state=? WHERE id=?", (state, attempt_id))
            db.execute("UPDATE coding_pool_tasks SET state='cancelled' WHERE id=?", (r["task_id"],))
            self._event(db, attempt_id, "cancel", {"external_stop_confirmed": False})
        return self.inspect(attempt_id)

    def confirm_stopped(self, attempt_id: str, owner: str) -> None:
        with self.connect() as db:
            if (
                db.execute(
                    "UPDATE coding_pool_attempts SET state='cancelled',stop_confirmed=1 WHERE id=? AND owner=? AND state='cancel_requested'",
                    (attempt_id, owner),
                ).rowcount
                != 1
            ):
                raise ValueError("Stop confirmation does not match owned cancelling attempt")

    def expire(self) -> int:
        """Erase raw contracts/verdicts, retain numerical metadata and output provenance."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM coding_pool_objectives WHERE expires<=?", (self.clock(),)
                )
            ]
            for oid in ids:
                db.execute("UPDATE coding_pool_objectives SET contract='{}' WHERE id=?", (oid,))
                db.execute(
                    "UPDATE coding_pool_tasks SET contract='{}' WHERE objective_id=?", (oid,)
                )
                db.execute(
                    "UPDATE coding_pool_attempts SET verdict=NULL WHERE task_id IN (SELECT id FROM coding_pool_tasks WHERE objective_id=?)",
                    (oid,),
                )
        return len(ids)

    def dispatch(
        self, name: str, arguments: dict[str, Any], *, objective_id: str, run_id: str
    ) -> Any:
        """Scope is injected by the harness, never accepted from tool arguments."""
        methods = {
            "assign": self.assign,
            "inspect": self.inspect,
            "collect": self.collect,
            "verify": self.verify,
            "correct": self.correct,
            "cancel": self.cancel,
        }
        if name not in methods:
            raise ValueError("Operation is not model-facing")
        if not isinstance(arguments, dict) or set(arguments) & {
            "run_id",
            "objective_id",
            "owner",
            "verdict",
        }:
            raise ValueError("Caller cannot supply authority fields")
        args = dict(arguments)
        with self.connect() as db:
            if name == "assign":
                task = db.execute(
                    "SELECT objective_id FROM coding_pool_tasks WHERE id=?",
                    (args.get("task_id"),),
                ).fetchone()
                if not task or task["objective_id"] != objective_id:
                    raise ValueError("Foreign task scope")
                args["run_id"] = run_id
            else:
                aid = args.get("parent_id") if name == "correct" else args.get("attempt_id")
                row = db.execute(
                    "SELECT a.run_id,t.objective_id FROM coding_pool_attempts a JOIN coding_pool_tasks t ON a.task_id=t.id WHERE a.id=?",
                    (aid,),
                ).fetchone()
                if not row or row["run_id"] != run_id or row["objective_id"] != objective_id:
                    raise ValueError("Foreign attempt scope")
                if name == "correct":
                    args["run_id"] = run_id
        value = methods[name](**args)
        if isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != "owner"}
        return value


class ResearchClaimTracker:
    """Tracks research claims with source provenance.

    Enforces: Agreement alone cannot establish a fact. Even unanimous agreement across
    multiple workers leaves a claim 'unresolved' unless backed by verified primary
    source provenance.
    """

    def __init__(self, pool: CodingPool):
        self.pool = pool

    def record_claim(
        self,
        objective_id: str,
        statement: str,
        *,
        claim_id: str | None = None,
        worker_agreements: list[str] | None = None,
        supporting_sources: list[dict[str, Any]] | None = None,
        contradicting_sources: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(statement, str) or not statement.strip():
            raise ValueError("Claim statement must be non-empty")
        cid = claim_id or ("claim_" + uuid.uuid4().hex[:12])
        agreements = list(worker_agreements or [])
        sup = [
            s for s in (supporting_sources or [])
            if isinstance(s, dict) and s.get("source_ref") and s.get("verified_provenance") is True
        ]
        con = [
            s for s in (contradicting_sources or [])
            if isinstance(s, dict) and s.get("source_ref") and s.get("verified_provenance") is True
        ]

        # Agreement alone NEVER establishes a fact
        if con:
            status = "contradicted"
        elif sup:
            status = "supported"
        else:
            status = "unresolved"

        now = self.pool.clock()
        with self.pool.connect() as db:
            db.execute(
                """INSERT INTO coding_pool_claims
                   (id,objective_id,statement,status,supporting_sources,contradicting_sources,worker_agreements,updated)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       status=excluded.status,
                       supporting_sources=excluded.supporting_sources,
                       contradicting_sources=excluded.contradicting_sources,
                       worker_agreements=excluded.worker_agreements,
                       updated=excluded.updated""",
                (
                    cid,
                    objective_id,
                    statement.strip(),
                    status,
                    json.dumps(sup),
                    json.dumps(con),
                    json.dumps(agreements),
                    now,
                ),
            )
        return {
            "claim_id": cid,
            "objective_id": objective_id,
            "statement": statement.strip(),
            "status": status,
            "verified_supporting_sources": len(sup),
            "verified_contradicting_sources": len(con),
            "worker_agreement_count": len(agreements),
            "established_by_agreement_alone": False,
        }


class SabrinaReasoningAllocator:
    """Implements Sabrina's 7 explicit reasoning allocation actions under the 4-worker ceiling.

    Actions:
    1. acquire_fact
    2. delegate_implementation
    3. request_alternative
    4. review_candidate
    5. request_verification
    6. return_for_correction
    7. stop

    Uses transparent deterministic rules based on verified task history, unblocking priority,
    remaining budget, and qualified capabilities (no uncalibrated confidence numbers).
    """

    def __init__(self, pool: CodingPool):
        self.pool = pool

    @staticmethod
    def build_scoped_child_brief(
        task_contract: dict[str, Any],
        *,
        delegation_reason: str,
        relevant_evidence: list[dict[str, Any]] | None = None,
        failed_criteria: list[str] | None = None,
        candidate_for_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a bounded child payload containing only relevant evidence and delegation reason.

        Never copies the full parent conversation history.
        """
        if not isinstance(delegation_reason, str) or not delegation_reason.strip():
            raise ValueError("delegation_reason is required for child task delegation")
        scoped_evidence = []
        for item in (relevant_evidence or []):
            if isinstance(item, dict):
                if "full_conversation" in item or "raw_transcript" in item:
                    raise ValueError("Child delegation must not copy full parent conversation")
                scoped_evidence.append(
                    {
                        "fact_id": item.get("fact_id"),
                        "summary": str(item.get("summary") or "")[:600],
                        "source_ref": item.get("source_ref"),
                    }
                )
        brief = {
            "delegation_reason": delegation_reason.strip(),
            "acceptance_criteria": list(task_contract.get("acceptance_criteria") or []),
            "editable_files": list(
                task_contract.get("editable_files") or task_contract.get("allowed_files") or []
            ),
            "readonly_files": list(task_contract.get("readonly_files") or []),
            "protected_files": list(task_contract.get("protected_files") or []),
            "relevant_evidence": scoped_evidence,
            "failed_criteria_to_fix": list(failed_criteria or []),
        }
        if candidate_for_review is not None:
            if not candidate_for_review.get("artifact_hash") or not candidate_for_review.get("content_or_diff"):
                raise ValueError("Review brief requires exact candidate artifact_hash and content_or_diff")
            brief["candidate_revision"] = {
                "artifact_hash": candidate_for_review["artifact_hash"],
                "content_or_diff": candidate_for_review["content_or_diff"],
            }
        return brief

    def decide_next_action(
        self,
        objective_id: str,
        *,
        require_independent_review: bool = False,
        enable_adaptive_alternatives: bool = False,
    ) -> dict[str, Any]:
        """Choose the next action among the 7 bounded actions using transparent rules."""
        now = self.pool.clock()
        with self.pool.connect() as db:
            obj = db.execute(
                "SELECT * FROM coding_pool_objectives WHERE id=?", (objective_id,)
            ).fetchone()
            if not obj:
                raise ValueError("Unknown objective")
            if now >= obj["deadline"]:
                return {
                    "action": "stop",
                    "outcome": "blocker",
                    "blocker": "objective_budget_exhausted",
                    "reason": "60-minute objective budget exhausted",
                }
            obj_contract = json.loads(obj["contract"])
            missing_facts = [
                f for f in obj_contract.get("required_facts", [])
                if f not in set(obj_contract.get("acquired_facts", []))
            ]
            if missing_facts:
                return {
                    "action": "acquire_fact",
                    "missing_fact": missing_facts[0],
                    "reason": f"Acquire missing prerequisite fact '{missing_facts[0]}' before implementation",
                }

            qualified_providers = [
                name
                for name, p in self.pool.providers.items()
                if p.get("editing_qualified")
                and p.get("fingerprint")
                and p.get("qualification_id")
                and not p.get("is_simulated")
            ]
            if not qualified_providers:
                return {
                    "action": "stop",
                    "outcome": "blocker",
                    "blocker": "missing_capability",
                    "reason": "No live-qualified editing worker capability available",
                }

            tasks = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM coding_pool_tasks WHERE objective_id=? ORDER BY rowid",
                    (objective_id,),
                ).fetchall()
            ]
            if not tasks:
                return {
                    "action": "stop",
                    "outcome": "blocker",
                    "blocker": "no_tasks",
                    "reason": "Objective has no bounded tasks",
                }

            # Compute how many downstream tasks each task unblocks (priority for unblocking tasks)
            unblocks_count: dict[str, int] = {t["id"]: 0 for t in tasks}
            task_by_id = {t["id"]: t for t in tasks}
            for t in tasks:
                for dep_id in json.loads(t["dependencies"]):
                    if dep_id in unblocks_count:
                        unblocks_count[dep_id] += 1

            # Sort tasks by (unblocks_count DESC, original order) to prioritize tasks that unblock objective
            prioritized_tasks = sorted(
                tasks, key=lambda t: (-unblocks_count.get(t["id"], 0), t["id"])
            )

            all_verified = all(t["state"] == "verified" for t in tasks)
            if all_verified:
                return {
                    "action": "stop",
                    "outcome": "verified_completion",
                    "blocker": None,
                    "reason": "All required tasks passed authoritative verification",
                }

            for t in prioritized_tasks:
                tid = t["id"]
                deps = json.loads(t["dependencies"])
                if any(task_by_id[d]["state"] != "verified" for d in deps if d in task_by_id):
                    continue

                attempts = [
                    dict(a)
                    for a in db.execute(
                        "SELECT * FROM coding_pool_attempts WHERE task_id=? ORDER BY rowid",
                        (tid,),
                    ).fetchall()
                ]
                reviews = [
                    dict(rv)
                    for rv in db.execute(
                        "SELECT * FROM coding_pool_reviews WHERE task_id=? ORDER BY rowid",
                        (tid,),
                    ).fetchall()
                ]
                t_contract = json.loads(t["contract"])

                # 1. If there is a collected attempt awaiting review or verification:
                collected = [a for a in attempts if a["state"] == "collected"]
                if collected:
                    latest_col = collected[-1]
                    rev_hash = json.loads(latest_col["revision"])["artifact_hash"]
                    already_reviewed = any(
                        rv["attempt_id"] == latest_col["id"]
                        and rv["candidate_artifact_hash"] == rev_hash
                        for rv in reviews
                    )
                    other_providers = [
                        p for p in qualified_providers if p != latest_col["provider"]
                    ]
                    if require_independent_review and not already_reviewed and other_providers:
                        return {
                            "action": "review_candidate",
                            "task_id": tid,
                            "attempt_id": latest_col["id"],
                            "candidate_artifact_hash": rev_hash,
                            "reviewer_provider": other_providers[0],
                            "acceptance_criteria": t_contract.get("acceptance_criteria", []),
                            "reason": "Independent review of collected candidate revision against acceptance criteria",
                        }
                    return {
                        "action": "request_verification",
                        "task_id": tid,
                        "attempt_id": latest_col["id"],
                        "candidate_artifact_hash": rev_hash,
                        "reason": "Execute maintainer-registered verification checks on immutable candidate",
                    }

                # 2. If task is ready and has no attempts yet -> delegate_implementation (1 worker for clear task)
                if t["state"] == "ready" and not attempts:
                    return {
                        "action": "delegate_implementation",
                        "task_id": tid,
                        "provider": qualified_providers[0],
                        "unblocks_count": unblocks_count.get(tid, 0),
                        "reason": "Delegate clear bounded task to single qualified worker",
                    }

                # 3. If task failed verification (needs_revision):
                if t["state"] == "needs_revision" and attempts:
                    latest = attempts[-1]
                    verdict = json.loads(latest["verdict"] or "{}")
                    failed_crit = verdict.get("failed_criteria", [])

                    # Check for repeated unchanged failures across consecutive attempts
                    unchanged_failure = False
                    if len(attempts) >= 2:
                        prev_verdict = json.loads(attempts[-2]["verdict"] or "{}")
                        prev_failed = prev_verdict.get("failed_criteria", [])
                        prev_rev = json.loads(attempts[-2]["revision"] or "{}").get("artifact_hash")
                        curr_rev = json.loads(latest["revision"] or "{}").get("artifact_hash")
                        if (failed_crit and failed_crit == prev_failed) or (curr_rev and curr_rev == prev_rev):
                            unchanged_failure = True

                    used_providers = {a["provider"] for a in attempts}
                    unused_providers = [p for p in qualified_providers if p not in used_providers]

                    if len(attempts) >= 3:
                        return {
                            "action": "stop",
                            "outcome": "blocker",
                            "task_id": tid,
                            "blocker": "attempt_budget_exhausted",
                            "reason": "Initial attempt plus two corrections exhausted",
                        }

                    if unchanged_failure:
                        if enable_adaptive_alternatives and unused_providers:
                            return {
                                "action": "request_alternative",
                                "task_id": tid,
                                "parent_id": latest["id"],
                                "provider": unused_providers[0],
                                "failed_criteria": failed_crit,
                                "reason": "Repeated unchanged failure on primary worker; requesting independent alternative from second worker",
                            }
                        return {
                            "action": "stop",
                            "outcome": "blocker",
                            "task_id": tid,
                            "blocker": "repeated_unchanged_failure",
                            "failed_criteria": failed_crit,
                            "reason": "Stopping due to repeated unchanged verification failures",
                        }

                    if (
                        enable_adaptive_alternatives
                        and t_contract.get("distinct_uncertainty")
                        and unused_providers
                    ):
                        return {
                            "action": "request_alternative",
                            "task_id": tid,
                            "parent_id": latest["id"],
                            "provider": unused_providers[0],
                            "failed_criteria": failed_crit,
                            "reason": "Task has distinct uncertainty after initial failure; requesting independent alternative",
                        }

                    return {
                        "action": "return_for_correction",
                        "task_id": tid,
                        "parent_id": latest["id"],
                        "provider": latest["provider"],
                        "failed_criteria": failed_crit,
                        "reason": "Return criterion-specific verifier failures to worker for bounded correction",
                    }

                if t["state"] == "blocked":
                    return {
                        "action": "stop",
                        "outcome": "blocker",
                        "task_id": tid,
                        "blocker": "task_verification_blocked",
                        "reason": f"Task {tid} is blocked",
                    }

            return {
                "action": "stop",
                "outcome": "blocker",
                "blocker": "no_assignable_tasks",
                "reason": "No ready or actionable tasks remain",
            }
