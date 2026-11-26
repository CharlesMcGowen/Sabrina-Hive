"""Durable, capability-bounded scheduler for Sabrina customer work.

This is deliberately not an LLM tool-selection loop. Each job kind maps to one
fixed Python handler and one exact capability profile. A customer-email job can
therefore read only its allowlisted Gmail thread, classify it, send at most the
already-authorized safe reply, or build a planning-only SOW artifact. It has no
LinkedIn, general browsing, shell, or testing capability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.customer_engagement_loop import run_customer_pass
from tools.customer_adaptation import handle_customer_adaptation
from tools.customer_commercial_handoff import COMMERCIAL_CATEGORIES, write_commercial_packet
from tools.customer_commercial_approval import CommercialApprovalQueue, deliver_queued_commercial_reply
from tools.customer_negotiation_authority import NegotiationAuthorityStore, queue_authorized_negotiation_move
from tools.customer_meeting_approval import MeetingApprovalQueue, deliver_queued_meeting_reply
from tools.customer_meeting_confirmation import process_customer_meeting_confirmation
from tools.customer_expectation_bridge import build_expectation_bridge
from tools.customer_resource_research import build_resource_research_plan
from tools.customer_operator_handoff import write_operator_handoff
from tools.customer_test_framework import build_framework_bundle
from tools.lab_capability_catalog import build_capability_catalog, build_sow_code_review, build_testing_framework
from tools.messaging_store import ConversationStore, ROOT


CONFIG_PATH = ROOT / "config/job_scheduler.json"
DB_PATH = ROOT / "data/customer_jobs.sqlite3"
STOP_PATH = ROOT / "data/customer_job_scheduler.stop"
SOW_ARTIFACT_ROOT = ROOT / "artifacts/customer_engagement/sow"

AUTO_STATES = {"WAITING_EMAIL", "CLASSIFYING"}
ATTENTION_STATES = {"DRAFT_READY", "NEEDS_OPERATOR", "SOW_REVIEW", "TEST_PLAN_READY", "WAITING_APPROVAL"}
TERMINAL_STATES = {"COMPLETE", "DISABLED"}
ALL_STATES = AUTO_STATES | ATTENTION_STATES | TERMINAL_STATES | {"SENT"}

ALLOWED_TRANSITIONS = {
    "WAITING_EMAIL": {"CLASSIFYING", "COMPLETE", "DISABLED"},
    "CLASSIFYING": {"WAITING_EMAIL", "SENT", "DRAFT_READY", "NEEDS_OPERATOR", "SOW_REVIEW", "COMPLETE", "DISABLED"},
    "SENT": {"WAITING_EMAIL", "NEEDS_OPERATOR"},
    "DRAFT_READY": {"WAITING_EMAIL", "NEEDS_OPERATOR", "COMPLETE"},
    "NEEDS_OPERATOR": {"WAITING_EMAIL", "COMPLETE"},
    "SOW_REVIEW": {"TEST_PLAN_READY", "NEEDS_OPERATOR"},
    "TEST_PLAN_READY": {"WAITING_APPROVAL", "NEEDS_OPERATOR"},
    "WAITING_APPROVAL": {"WAITING_EMAIL", "COMPLETE"},
    "COMPLETE": set(),
    "DISABLED": {"WAITING_EMAIL"},
}

EXPECTED_PROFILES = {
    "inbox_triage": {"gmail.read_recent_inbox", "operator.notify_question"},
    "customer_email": {
        "gmail.read_allowlisted_thread",
        "gmail.send_allowlisted_reply",
        "customer.classify_policy",
        "customer.apply_preapproved_negotiation",
        "customer.confirm_preapproved_meeting",
        "calendar.recheck_and_create_preapproved_event",
        "operator.notify_preapproved_meeting",
        "customer.build_sow_plan",
        "customer.write_operator_artifact",
    }
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def load_scheduler_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path or CONFIG_PATH)
    value = json.loads(target.read_text(encoding="utf-8-sig"))
    if value.get("version") != 1:
        raise ValueError("Unsupported scheduler config version")
    profiles = value.get("tool_profiles")
    if not isinstance(profiles, dict):
        raise ValueError("tool_profiles is required")
    for name, tools in profiles.items():
        expected = EXPECTED_PROFILES.get(name)
        actual = set(tools)
        if actual != expected:
            raise ValueError(f"Tool profile {name!r} must match the fixed capability boundary")
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("At least one job is required")
    seen: set[str] = set()
    for job in jobs:
        job_id = job.get("id")
        if not isinstance(job_id, str) or not job_id or job_id in seen:
            raise ValueError("Job IDs must be unique nonempty strings")
        seen.add(job_id)
        if job.get("kind") not in EXPECTED_PROFILES:
            raise ValueError(f"Unsupported job kind: {job.get('kind')}")
        if job.get("tool_profile") != job.get("kind"):
            raise ValueError("A job must use the fixed profile for its kind")
        if job['tool_profile'] not in profiles:
            raise ValueError('A job requires its exact capability profile')
        if not isinstance(job.get("context"), dict):
            raise ValueError("Job context must be an object")
        if job["kind"] == "customer_email" and not job["context"].get("contact_id"):
            raise ValueError("customer_email requires contact_id")
        poll = int(job.get("poll_seconds", 300))
        if not 30 <= poll <= 3600:
            raise ValueError("poll_seconds must be between 30 and 3600")
    return value


class JobStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              state TEXT NOT NULL,
              enabled INTEGER NOT NULL,
              priority INTEGER NOT NULL,
              next_run_at REAL,
              lease_owner TEXT,
              lease_expires_at REAL,
              allowed_tools TEXT NOT NULL,
              context TEXT NOT NULL,
              config_hash TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0,
              last_outcome TEXT,
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              event TEXT NOT NULL,
              from_state TEXT,
              to_state TEXT,
              owner TEXT,
              details TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(job_id) REFERENCES jobs(id)
            );
            CREATE TABLE IF NOT EXISTS dispatcher_leases (
              name TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              expires_at REAL NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_due_idx
              ON jobs(enabled, state, next_run_at, priority);
            CREATE INDEX IF NOT EXISTS job_events_job_idx
              ON job_events(job_id, sequence);
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _event(
        self,
        job_id: str,
        event: str,
        from_state: str | None,
        to_state: str | None,
        owner: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO job_events(job_id,event,from_state,to_state,owner,details,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, event, from_state, to_state, owner, json.dumps(details or {}, ensure_ascii=False), utc_now()),
        )

    def sync(self, config: dict[str, Any], *, now_epoch: float | None = None) -> None:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        configured: set[str] = set()
        with self.db:
            for spec in config["jobs"]:
                job_id = spec["id"]
                configured.add(job_id)
                tools = config["tool_profiles"][spec["tool_profile"]]
                spec_hash = canonical_hash(spec)
                old = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
                if old is None:
                    self.db.execute(
                        """INSERT INTO jobs(
                          id,kind,state,enabled,priority,next_run_at,allowed_tools,context,config_hash,created_at,updated_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            job_id,
                            spec["kind"],
                            "WAITING_EMAIL",
                            int(bool(spec.get("enabled", True))),
                            int(spec.get("priority", 0)),
                            now_epoch,
                            json.dumps(tools),
                            json.dumps({**spec["context"], "poll_seconds": int(spec.get("poll_seconds", 300))}),
                            spec_hash,
                            utc_now(),
                            utc_now(),
                        ),
                    )
                    self._event(job_id, "CONFIGURED", None, "WAITING_EMAIL", None, {"config_hash": spec_hash})
                else:
                    self.db.execute(
                        """UPDATE jobs SET kind=?,enabled=?,priority=?,allowed_tools=?,context=?,config_hash=?,updated_at=?
                           WHERE id=?""",
                        (
                            spec["kind"],
                            int(bool(spec.get("enabled", True))),
                            int(spec.get("priority", 0)),
                            json.dumps(tools),
                            json.dumps({**spec["context"], "poll_seconds": int(spec.get("poll_seconds", 300))}),
                            spec_hash,
                            utc_now(),
                            job_id,
                        ),
                    )
            rows = self.db.execute("SELECT id,state FROM jobs").fetchall()
            for row in rows:
                if row["id"] not in configured:
                    self.db.execute("UPDATE jobs SET enabled=0,state='DISABLED',updated_at=? WHERE id=?", (utc_now(), row["id"]))
                    self._event(row["id"], "REMOVED_FROM_CONFIG", row["state"], "DISABLED", None)

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["allowed_tools"] = json.loads(value["allowed_tools"])
        value["context"] = json.loads(value["context"])
        if value.get("last_outcome"):
            value["last_outcome"] = json.loads(value["last_outcome"])
        value["enabled"] = bool(value["enabled"])
        return value

    def get(self, job_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode(row)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._decode(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY priority DESC,id")]

    def events(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM job_events WHERE job_id=? ORDER BY sequence", (job_id,)).fetchall()
        values = []
        for row in rows:
            value = dict(row)
            value["details"] = json.loads(value["details"])
            values.append(value)
        return values

    def acquire_dispatcher(self, owner: str, ttl_seconds: int = 30, *, now_epoch: float | None = None) -> bool:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("DELETE FROM dispatcher_leases WHERE name='customer' AND expires_at<=?", (now_epoch,))
            self.db.execute(
                "INSERT OR IGNORE INTO dispatcher_leases(name,owner,expires_at,updated_at) VALUES('customer',?,?,?)",
                (owner, now_epoch + max(10, int(ttl_seconds)), utc_now()),
            )
            row = self.db.execute("SELECT owner FROM dispatcher_leases WHERE name='customer'").fetchone()
            acquired = row is not None and row["owner"] == owner
            self.db.commit()
            return acquired
        except Exception:
            self.db.rollback()
            raise

    def renew_dispatcher(self, owner: str, ttl_seconds: int = 30, *, now_epoch: float | None = None) -> bool:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        with self.db:
            cursor = self.db.execute(
                """UPDATE dispatcher_leases SET expires_at=?,updated_at=?
                   WHERE name='customer' AND owner=? AND expires_at>?""",
                (now_epoch + max(10, int(ttl_seconds)), utc_now(), owner, now_epoch),
            )
        return cursor.rowcount == 1

    def release_dispatcher(self, owner: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM dispatcher_leases WHERE name='customer' AND owner=?", (owner,))

    def dispatcher(self, *, now_epoch: float | None = None) -> dict[str, Any] | None:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        row = self.db.execute(
            "SELECT owner,expires_at,updated_at FROM dispatcher_leases WHERE name='customer' AND expires_at>?",
            (now_epoch,),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim_due(self, owner: str, lease_seconds: int, *, now_epoch: float | None = None) -> dict[str, Any] | None:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                """SELECT * FROM jobs
                   WHERE enabled=1 AND state IN ('WAITING_EMAIL','CLASSIFYING')
                     AND COALESCE(next_run_at,0)<=?
                     AND (lease_owner IS NULL OR COALESCE(lease_expires_at,0)<=?)
                   ORDER BY priority DESC,next_run_at,id LIMIT 1""",
                (now_epoch, now_epoch),
            ).fetchone()
            if row is None:
                self.db.commit()
                return None
            previous = row["state"]
            self.db.execute(
                """UPDATE jobs SET state='CLASSIFYING',lease_owner=?,lease_expires_at=?,attempts=attempts+1,
                   updated_at=? WHERE id=?""",
                (owner, now_epoch + max(30, int(lease_seconds)), utc_now(), row["id"]),
            )
            self._event(
                row["id"],
                "LEASE_RECOVERED" if previous == "CLASSIFYING" else "CLAIMED",
                previous,
                "CLASSIFYING",
                owner,
                {"lease_seconds": int(lease_seconds)},
            )
            self.db.commit()
            return self.get(row["id"])
        except Exception:
            self.db.rollback()
            raise

    def transition_claimed(
        self,
        job_id: str,
        owner: str,
        to_state: str,
        *,
        event: str = "TRANSITION",
        details: dict[str, Any] | None = None,
    ) -> None:
        if to_state not in ALL_STATES:
            raise ValueError(f"Unknown state {to_state}")
        with self.db:
            row = self.db.execute("SELECT state,lease_owner FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["lease_owner"] != owner:
                raise RuntimeError("Job lease is not owned by this dispatcher")
            if to_state not in ALLOWED_TRANSITIONS[row["state"]]:
                raise ValueError(f"Invalid transition {row['state']} -> {to_state}")
            self.db.execute("UPDATE jobs SET state=?,updated_at=? WHERE id=?", (to_state, utc_now(), job_id))
            self._event(job_id, event, row["state"], to_state, owner, details)

    def finish_claimed(
        self,
        job_id: str,
        owner: str,
        to_state: str,
        outcome: dict[str, Any],
        *,
        next_run_at: float | None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if to_state not in ALL_STATES:
            raise ValueError(f"Unknown state {to_state}")
        with self.db:
            row = self.db.execute("SELECT state,lease_owner FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["lease_owner"] != owner:
                raise RuntimeError("Job lease is not owned by this dispatcher")
            if to_state not in ALLOWED_TRANSITIONS[row["state"]]:
                raise ValueError(f"Invalid transition {row['state']} -> {to_state}")
            self.db.execute(
                """UPDATE jobs SET state=?,next_run_at=?,lease_owner=NULL,lease_expires_at=NULL,
                   last_outcome=?,last_error=?,updated_at=? WHERE id=?""",
                (to_state, next_run_at, json.dumps(outcome, ensure_ascii=False), error, utc_now(), job_id),
            )
            self._event(job_id, "OUTCOME", row["state"], to_state, owner, {"status": outcome.get("status")})
        return self.get(job_id)

    def resume(self, job_id: str, *, now_epoch: float | None = None) -> dict[str, Any]:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        with self.db:
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if "WAITING_EMAIL" not in ALLOWED_TRANSITIONS[row["state"]]:
                raise ValueError(f"Job cannot be resumed from {row['state']}")
            self.db.execute(
                """UPDATE jobs SET state='WAITING_EMAIL',enabled=1,next_run_at=?,lease_owner=NULL,
                   lease_expires_at=NULL,last_error=NULL,updated_at=? WHERE id=?""",
                (now_epoch, utc_now(), job_id),
            )
            self._event(job_id, "OPERATOR_RESUMED", row["state"], "WAITING_EMAIL", "operator")
        return self.get(job_id)


def _is_sow_outcome(outcome: dict[str, Any]) -> bool:
    decision = outcome.get("decision") or {}
    text = " ".join((str(decision.get("category", "")), str(decision.get("reason", ""))))
    return "sow_or_rules_of_engagement" in text


def _latest_external_message(outcome: dict[str, Any], messaging_db: str | Path | None = None) -> str:
    if isinstance(outcome.get("sow_text"), str):
        return outcome["sow_text"]
    account = outcome.get("account_id")
    thread = outcome.get("thread_id")
    if not account or not thread:
        raise ValueError("SOW outcome is missing conversation identity")
    store = ConversationStore(messaging_db)
    try:
        snapshot = store.snapshot(account, thread)
    finally:
        store.close()
    for message in reversed(snapshot["messages"]):
        if message.get("ownership") == "external":
            return message.get("text", "")
    raise ValueError("No external SOW message was found")


def build_sow_artifacts(
    job_id: str,
    outcome: dict[str, Any],
    *,
    output_root: str | Path | None = None,
    messaging_db: str | Path | None = None,
) -> dict[str, Any]:
    sow_text = _latest_external_message(outcome, messaging_db)
    catalog = build_capability_catalog(ROOT)
    framework = build_testing_framework(sow_text, catalog)
    code_review = build_sow_code_review(sow_text, root=ROOT, catalog=catalog)
    artifact_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = Path(output_root or SOW_ARTIFACT_ROOT) / artifact_id
    destination.mkdir(parents=True, exist_ok=False)
    payloads = {
        "sow_review.json": framework["review"],
        "testing_framework.json": framework,
        "lab_code_review.json": code_review,
        "capability_catalog.json": catalog,
    }
    for name, value in payloads.items():
        (destination / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    bundle = build_framework_bundle(
        sow_text,
        destination / "test_framework",
        catalog=catalog,
        code_review=code_review,
    )
    test_matrix = json.loads((Path(bundle["path"]) / "test_matrix.json").read_text(encoding="utf-8"))
    expectation_bridge = build_expectation_bridge(framework, code_review, test_matrix)
    resource_research = build_resource_research_plan(framework, code_review, test_matrix, catalog)
    expectation_paths = {}
    for audience, value in expectation_bridge.items():
        name = f"{audience}_expectation_packet.json"
        path = destination / name
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        expectation_paths[audience] = str(path.resolve())
    resource_research_path = destination / "resource_research_plan.json"
    resource_research_path.write_text(json.dumps(resource_research, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "artifact_id": artifact_id,
        "job_id": job_id,
        "created_at": utc_now(),
        "path": str(destination.resolve()),
        "sow_sha256": framework["review"]["sow_sha256"],
        "catalog_snapshot_sha256": catalog["source_snapshot_sha256"],
        "all_source_files_hashed": code_review["all_source_files_hashed"],
        "all_files_reviewed_for_relevance": code_review["all_files_reviewed_for_relevance"],
        "catalog_snapshot_consistent": code_review["catalog_snapshot_consistent"],
        "reviewed_source_file_count": code_review["reviewed_source_file_count"],
        "code_file_count": code_review["code_file_count"],
        "reviewed_code_file_count": code_review["reviewed_code_file_count"],
        "all_code_files_reviewed_for_relevance": code_review["all_code_files_reviewed_for_relevance"],
        "relevant_code_file_count": code_review["relevant_code_file_count"],
        "deep_review_code_file_count": code_review["deep_review_code_file_count"],
        "all_relevant_code_queued": code_review["all_relevant_code_queued"],
        "omitted_relevant_code_file_count": code_review["omitted_relevant_code_file_count"],
        "deep_review_file_count": code_review["deep_review_file_count"],
        "authorization_ready": framework["review"]["authorization_ready"],
        "all_execution_disabled": framework["all_execution_disabled"],
        "next_state": "WAITING_APPROVAL",
        "test_framework_bundle": bundle,
        "expectation_bridge": {
            "customer_packet": expectation_paths["customer"],
            "internal_packet": expectation_paths["internal"],
            "customer_packet_verified": expectation_bridge["customer"]["verification"]["verified"],
            "customer_reply_authorized": False,
        },
        "resource_research": {
            "path": str(resource_research_path.resolve()),
            "status": resource_research["status"],
            "task_count": len(resource_research["tasks"]),
            "verified": resource_research["verification"]["verified"],
            "customer_scheduler_web_access": False,
            "customer_execution_allowed": False,
        },
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def customer_email_handler(job: dict[str, Any]) -> dict[str, Any]:
    if set(job["allowed_tools"]) != EXPECTED_PROFILES["customer_email"]:
        raise RuntimeError("Customer job capability profile changed after configuration validation")
    context = job["context"]
    approved = deliver_queued_commercial_reply(
        context["contact_id"],
        cdp_url=context.get("cdp_url"),
    )
    if approved is not None:
        return approved
    meeting = deliver_queued_meeting_reply(
        context["contact_id"],
        cdp_url=context.get("cdp_url"),
    )
    if meeting is not None:
        return meeting
    return run_customer_pass(
        contact_id=context["contact_id"],
        cdp_url=context.get("cdp_url"),
        send_safe=bool(context.get("send_safe", True)),
    )


def inbox_triage_handler(job: dict[str, Any]) -> dict[str, Any]:
    if set(job['allowed_tools']) != EXPECTED_PROFILES['inbox_triage']:
        raise RuntimeError('Inbox triage capability profile changed')
    from tools.gmail_inbox_triage import run_inbox_triage
    return run_inbox_triage(job)


HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "customer_email": customer_email_handler, "inbox_triage": inbox_triage_handler}
TRANSIENT_RETRY_DELAYS_SECONDS = (60, 180, 300)


def queue_operator_approved_commercial_reply(
    job_id: str,
    *,
    reply_text: str,
    terms: dict[str, Any],
    operator_approval_id: str,
    operator_instruction: str,
    approved_by: str,
    scheduler_db: str | Path | None = None,
    messaging_db: str | Path | None = None,
) -> dict[str, Any]:
    jobs = JobStore(scheduler_db)
    messages = ConversationStore(messaging_db)
    try:
        job = jobs.get(job_id)
        if job["state"] != "NEEDS_OPERATOR":
            raise ValueError("Commercial reply approval requires a job awaiting operator review")
        outcome = job.get("last_outcome") or {}
        decision = outcome.get("decision") or {}
        category = decision.get("category")
        if category not in COMMERCIAL_CATEGORIES:
            raise ValueError("The pending customer request is not a commercial category")
        account = outcome.get("account_id")
        thread = outcome.get("thread_id")
        if not account or not thread:
            raise ValueError("Pending commercial request is missing conversation identity")
        snapshot = messages.snapshot(account, thread)
        if snapshot.get("contact_id") != job["context"].get("contact_id"):
            raise ValueError("Commercial approval does not match the scheduled contact")
        approval = CommercialApprovalQueue(messages).queue(
            account=account,
            thread=thread,
            category=category,
            reply_text=reply_text,
            terms=terms,
            operator_approval_id=operator_approval_id,
            operator_instruction=operator_instruction,
            approved_by=approved_by,
        )
        resumed = jobs.resume(job_id)
        return {
            "status": "queued",
            "approval_id": approval["id"],
            "outbox_id": approval["outbox_id"],
            "reply_hash": approval["reply_hash"],
            "job_state": resumed["state"],
            "model_rewrite_allowed": False,
        }
    finally:
        messages.close()
        jobs.close()


def grant_operator_negotiation_authority(
    job_id: str,
    *,
    service_label: str,
    payment_terms: str,
    scope_trade: str,
    expires_at: str,
    max_turns: int,
    operator_approval_id: str,
    operator_instruction: str,
    approved_by: str,
    scheduler_db: str | Path | None = None,
    messaging_db: str | Path | None = None,
    commercial_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Grant a bounded price-negotiation envelope and resume its customer job."""
    jobs = JobStore(scheduler_db)
    messages = ConversationStore(messaging_db)
    try:
        job = jobs.get(job_id)
        if job["state"] != "NEEDS_OPERATOR":
            raise ValueError("Negotiation authority requires a job awaiting operator review")
        outcome = job.get("last_outcome") or {}
        if (outcome.get("decision") or {}).get("category") != "price_or_discount":
            raise ValueError("The pending customer request is not a price negotiation")
        account = outcome.get("account_id")
        thread = outcome.get("thread_id")
        if not account or not thread:
            raise ValueError("Pending negotiation is missing conversation identity")
        snapshot = messages.snapshot(account, thread)
        if snapshot.get("contact_id") != job["context"].get("contact_id"):
            raise ValueError("Negotiation authority does not match the scheduled contact")
        authority = NegotiationAuthorityStore(messages).grant(
            account=account,
            thread=thread,
            service_label=service_label,
            payment_terms=payment_terms,
            scope_trade=scope_trade,
            expires_at=expires_at,
            max_turns=max_turns,
            operator_approval_id=operator_approval_id,
            operator_instruction=operator_instruction,
            approved_by=approved_by,
            commercial=commercial_policy,
        )
        resumed = jobs.resume(job_id)
        return {
            "status": "active",
            "authority_id": authority["id"],
            "authority_hash": authority["authority_hash"],
            "expires_at": authority["expires_at"],
            "max_turns": authority["max_turns"],
            "job_state": resumed["state"],
            "customer_reply_sent": False,
            "testing_authorized": False,
            "legal_acceptance_authorized": False,
        }
    finally:
        messages.close()
        jobs.close()


def queue_operator_approved_meeting_proposal(
    job_id: str,
    *,
    reply_text: str,
    slots: list[dict[str, str]],
    operator_approval_id: str,
    operator_instruction: str,
    availability_verified_by: str,
    availability_verification_basis: str,
    scheduler_db: str | Path | None = None,
    messaging_db: str | Path | None = None,
) -> dict[str, Any]:
    jobs = JobStore(scheduler_db)
    messages = ConversationStore(messaging_db)
    try:
        job = jobs.get(job_id)
        if job["state"] != "NEEDS_OPERATOR":
            raise ValueError("Meeting proposal approval requires a job awaiting operator review")
        outcome = job.get("last_outcome") or {}
        if (outcome.get("decision") or {}).get("category") != "call_or_meeting":
            raise ValueError("The pending customer request is not a meeting request")
        handoff = outcome.get("operator_handoff") or {}
        meeting = handoff.get("meeting") or {}
        not_before_utc = meeting.get("not_before_utc")
        if not not_before_utc:
            raise ValueError("Meeting handoff is missing the minimum notice boundary")
        account = outcome.get("account_id")
        thread = outcome.get("thread_id")
        if not account or not thread:
            raise ValueError("Pending meeting request is missing conversation identity")
        snapshot = messages.snapshot(account, thread)
        if snapshot.get("contact_id") != job["context"].get("contact_id"):
            raise ValueError("Meeting approval does not match the scheduled contact")
        approval = MeetingApprovalQueue(messages).queue_proposal(
            account=account,
            thread=thread,
            reply_text=reply_text,
            slots=slots,
            not_before_utc=not_before_utc,
            operator_approval_id=operator_approval_id,
            operator_instruction=operator_instruction,
            availability_verified_by=availability_verified_by,
            availability_verification_basis=availability_verification_basis,
        )
        resumed = jobs.resume(job_id)
        return {
            "status": "queued",
            "approval_id": approval["id"],
            "outbox_id": approval["outbox_id"],
            "reply_hash": approval["reply_hash"],
            "job_state": resumed["state"],
            "calendar_event_created": False,
            "operator_notification_sent": False,
            "model_rewrite_allowed": False,
        }
    finally:
        messages.close()
        jobs.close()


class CustomerJobScheduler:
    def __init__(
        self,
        store: JobStore,
        config: dict[str, Any],
        *,
        owner: str | None = None,
        handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.time,
        sow_builder: Callable[..., dict[str, Any]] = build_sow_artifacts,
        handoff_writer: Callable[[dict[str, Any]], dict[str, Any]] = write_operator_handoff,
        commercial_writer: Callable[[dict[str, Any]], dict[str, Any]] = write_commercial_packet,
        negotiation_handler: Callable[[dict[str, Any]], dict[str, Any]] = queue_authorized_negotiation_move,
        meeting_confirmation_handler: Callable[[dict[str, Any]], dict[str, Any]] = process_customer_meeting_confirmation,
        adaptation_handler: Callable[[dict[str, Any]], dict[str, Any]] = handle_customer_adaptation,
    ):
        self.store = store
        self.config = config
        self.owner = owner or f"dispatcher-{os.getpid()}-{uuid.uuid4()}"
        self.handlers = handlers or HANDLERS
        self.clock = clock
        self.sow_builder = sow_builder
        self.handoff_writer = handoff_writer
        self.commercial_writer = commercial_writer
        self.negotiation_handler = negotiation_handler
        self.meeting_confirmation_handler = meeting_confirmation_handler
        self.adaptation_handler = adaptation_handler

    def run_once(self) -> dict[str, Any]:
        lease_seconds = int(self.config["dispatcher"].get("lease_seconds", 180))
        job = self.store.claim_due(self.owner, lease_seconds, now_epoch=self.clock())
        if job is None:
            return {"status": "idle", "owner": self.owner}
        try:
            handler = self.handlers[job["kind"]]
            outcome = handler(job)
            if not isinstance(outcome, dict) or not isinstance(outcome.get("status"), str):
                raise ValueError("Job handler returned an invalid outcome")
            status = outcome["status"]
            if status == "needs_operator" and (outcome.get("decision") or {}).get("category") == "price_or_discount":
                try:
                    negotiation = self.negotiation_handler(outcome)
                except Exception as negotiation_error:
                    negotiation = {
                        "status": "requires_operator",
                        "reason": f"Negotiation authority check failed: {type(negotiation_error).__name__}: {negotiation_error}"[:300],
                        "customer_reply_authorized": False,
                    }
                outcome = {**outcome, "negotiation_authority": negotiation}
                if negotiation.get("status") == "queued":
                    status = "commercial_reply_queued"
                    outcome = {**outcome, "status": status}
            if status == "needs_operator" and (outcome.get("decision") or {}).get("category") == "call_or_meeting":
                try:
                    meeting_confirmation = self.meeting_confirmation_handler(outcome)
                except Exception as meeting_error:
                    meeting_confirmation = {
                        "status": "requires_operator",
                        "reason": f"Meeting confirmation check failed: {type(meeting_error).__name__}: {meeting_error}"[:300],
                        "calendar_event_created": False,
                        "operator_notification_sent": False,
                    }
                outcome = {**outcome, "meeting_confirmation": meeting_confirmation}
                if meeting_confirmation.get("status") == "booked":
                    status = "meeting_booked"
                    outcome = {**outcome, "status": status}
            if status in {"unchanged", "no_reply"}:
                next_run = self.clock() + int(job["context"]["poll_seconds"])
                current = self.store.finish_claimed(job["id"], self.owner, "WAITING_EMAIL", outcome, next_run_at=next_run)
            elif status in {"retry", "model_retry"}:
                previous = job.get("last_outcome") or {}
                if status == "model_retry":
                    adaptation = self.adaptation_handler(outcome)
                    if adaptation.get("status") != "not_applicable":
                        outcome = {**outcome, "adaptation": adaptation}
                retry_count = (
                    int(previous.get("retry_count", 0)) + 1
                    if previous.get("status") == status else 1
                )
                outcome = {**outcome, "retry_count": retry_count}
                if retry_count <= len(TRANSIENT_RETRY_DELAYS_SECONDS) or job['kind']=='inbox_triage':
                    delay = TRANSIENT_RETRY_DELAYS_SECONDS[min(retry_count,len(TRANSIENT_RETRY_DELAYS_SECONDS))-1]
                    outcome["retry_after_seconds"] = delay
                    current = self.store.finish_claimed(
                        job["id"],
                        self.owner,
                        "WAITING_EMAIL",
                        outcome,
                        next_run_at=self.clock() + delay,
                        error=outcome.get("error"),
                    )
                else:
                    outcome = {
                        **outcome,
                        "status": "blocked",
                        "retry_exhausted": True,
                        "operator_handoff": self.handoff_writer(outcome),
                    }
                    current = self.store.finish_claimed(
                        job["id"],
                        self.owner,
                        "NEEDS_OPERATOR",
                        outcome,
                        next_run_at=None,
                        error=outcome.get("error"),
                    )
            elif status == "sent":
                self.store.transition_claimed(job["id"], self.owner, "SENT", event="CUSTOMER_MESSAGE_SENT")
                next_run = self.clock() + int(job["context"]["poll_seconds"])
                current = self.store.finish_claimed(job["id"], self.owner, "WAITING_EMAIL", outcome, next_run_at=next_run)
            elif status == "draft_saved":
                current = self.store.finish_claimed(job["id"], self.owner, "DRAFT_READY", outcome, next_run_at=None)
            elif status == "commercial_reply_queued":
                current = self.store.finish_claimed(
                    job["id"], self.owner, "WAITING_EMAIL", outcome, next_run_at=self.clock() + 1
                )
            elif status == "meeting_booked":
                current = self.store.finish_claimed(
                    job["id"], self.owner, "WAITING_EMAIL", outcome,
                    next_run_at=self.clock() + int(job["context"]["poll_seconds"]),
                )
            elif status == "needs_operator" and _is_sow_outcome(outcome):
                self.store.transition_claimed(job["id"], self.owner, "SOW_REVIEW", event="SOW_RECEIVED")
                manifest = self.sow_builder(job["id"], outcome)
                outcome = {**outcome, "sow_artifact": manifest}
                self.store.transition_claimed(job["id"], self.owner, "TEST_PLAN_READY", event="TEST_PLAN_BUILT", details=manifest)
                current = self.store.finish_claimed(job["id"], self.owner, "WAITING_APPROVAL", outcome, next_run_at=None)
            else:
                if status in {"needs_operator", "blocked", "uncertain"}:
                    decision = outcome.get("decision") or {}
                    if decision.get("category") in COMMERCIAL_CATEGORIES:
                        outcome = {**outcome, "commercial_packet": self.commercial_writer(outcome)}
                    adaptation = self.adaptation_handler(outcome)
                    if adaptation.get("status") != "not_applicable":
                        outcome = {**outcome, "adaptation": adaptation}
                    outcome = {**outcome, "operator_handoff": self.handoff_writer(outcome)}
                current = self.store.finish_claimed(
                    job["id"], self.owner, "NEEDS_OPERATOR", outcome, next_run_at=None,
                    error=outcome.get("error") if status in {"blocked", "uncertain"} else None,
                )
            return {"status": "processed", "job_id": job["id"], "outcome_status": status, "state": current["state"]}
        except Exception as error:
            outcome = {"status": "scheduler_error", "error": str(error), "finished": utc_now()}
            try:
                current = self.store.finish_claimed(
                    job["id"], self.owner, "NEEDS_OPERATOR", outcome, next_run_at=None, error=str(error)
                )
                state = current["state"]
            except Exception:
                state = "LEASE_UNRESOLVED"
            return {"status": "error", "job_id": job["id"], "state": state, "error": str(error)}


def _next_run_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def _job_operational_summary(row: dict[str, Any]) -> dict[str, Any]:
    outcome = row.get("last_outcome") or {}
    decision = outcome.get("decision") or {}
    followup = outcome.get("followup") or {}
    state = row["state"]
    outcome_status = outcome.get("status")
    category = decision.get("category")
    waiting_reason = "configured_not_started"
    next_action = "wait_for_dispatcher"
    if state == "WAITING_EMAIL":
        next_action = "review_recent_work_inbox" if row['kind']=='inbox_triage' else "poll_allowlisted_gmail_thread"
        if outcome_status in {"retry", "model_retry"}:
            waiting_reason = "bounded_retry_backoff"
        elif outcome_status == "sent":
            waiting_reason = "reply_sent_waiting_for_customer"
        elif outcome_status in {"unchanged", "no_reply"}:
            waiting_reason = "no_new_customer_email"
        elif outcome_status == "commercial_reply_queued":
            waiting_reason = "preapproved_commercial_reply_queued"
            next_action = "deliver_preapproved_commercial_reply"
        elif outcome_status == "meeting_booked":
            waiting_reason = "calendar_invite_sent_waiting_for_customer"
            next_action = "poll_allowlisted_gmail_thread"
        else:
            waiting_reason = "waiting_for_customer_email"
    elif state == "CLASSIFYING":
        waiting_reason = "processing_one_allowlisted_thread_snapshot"
        next_action = "finish_bounded_customer_pass"
    elif state == "NEEDS_OPERATOR":
        waiting_reason = "operator_authority_required"
        next_action = decision.get("next_action") or "operator_reviews_customer_request"
    elif state == "DRAFT_READY":
        waiting_reason = "draft_requires_operator_review"
        next_action = "operator_reviews_draft"
    elif state == "WAITING_APPROVAL":
        waiting_reason = "sow_plan_ready_testing_not_authorized"
        next_action = "operator_reviews_sow_and_separately_authorizes_testing"
    elif state == "COMPLETE":
        waiting_reason = "job_complete"
        next_action = "none"
    elif state == "DISABLED":
        waiting_reason = "job_disabled"
        next_action = "operator_enables_job"
    expected = EXPECTED_PROFILES.get(row["kind"], set())
    capability_profile_locked = set(row["allowed_tools"]) == expected
    return {
        "last_outcome_status": outcome_status,
        "last_decision_category": category,
        "customer_message_sent_last_pass": bool(outcome.get("sent")),
        "testing_authorized": bool(decision.get("testing_authorized")),
        "calendar_event_created_last_pass": bool((outcome.get("meeting_confirmation") or {}).get("calendar_event_created")),
        "operator_notification_sent_last_pass": bool((outcome.get("meeting_confirmation") or {}).get("operator_notification_sent")),
        "operator_action_required": state in ATTENTION_STATES,
        "waiting_reason": waiting_reason,
        "next_action": next_action,
        "next_run_at_utc": _next_run_iso(row.get("next_run_at")),
        "capability_profile_locked": capability_profile_locked,
        "allowed_tools": row["allowed_tools"],
        "sales_followup_status": followup.get("status"),
        "sales_followup_step": followup.get("step"),
        "sales_followup_not_before_utc": followup.get("not_before_utc"),
        "sales_followup_linkedin_allowed": bool(followup.get("linkedin_allowed", False)),
        "forbidden_customer_channels_absent": capability_profile_locked and all(
            marker not in tool.lower()
            for tool in row["allowed_tools"]
            for marker in ("linkedin", "general_browser", "shell", "testing.execute")
        ),
    }


def status_payload(store: JobStore, *, owner: str | None = None) -> dict[str, Any]:
    jobs = store.list_jobs()
    dispatcher = store.dispatcher()
    return {
        "status": "attention_required" if any(row["state"] in ATTENTION_STATES for row in jobs) else "running" if dispatcher else "configured",
        "owner": owner or (dispatcher or {}).get("owner"),
        "dispatcher_active": dispatcher is not None,
        "updated_at": utc_now(),
        "jobs": [
            {
                "id": row["id"],
                "kind": row["kind"],
                "state": row["state"],
                "enabled": row["enabled"],
                "priority": row["priority"],
                "next_run_at": row["next_run_at"],
                "lease_owner": row["lease_owner"],
                # A normal no-reply mailbox pass is a successful job run, not a
                # failed attempt. Keep the persisted column for compatibility,
                # but expose a name that cannot be mistaken for repeated errors.
                "job_run_count": row["attempts"],
                "current_retry_count": (
                    int((row.get("last_outcome") or {}).get("retry_count", 0))
                    if (row.get("last_outcome") or {}).get("status") in {"retry", "model_retry"}
                    else 0
                ),
                "poll_interval_seconds": int(row["context"].get("poll_seconds", 300)),
                "last_error": row["last_error"],
                **_job_operational_summary(row),
            }
            for row in jobs
        ],
    }


def write_status(config: dict[str, Any], payload: dict[str, Any]) -> Path:
    path = ROOT / config["dispatcher"].get("status_path", "artifacts/customer_scheduler/status.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def run_forever(store: JobStore, config: dict[str, Any]) -> int:
    scheduler = CustomerJobScheduler(store, config)
    interval = max(1, int(config["dispatcher"].get("check_interval_seconds", 5)))
    # The Gmail pass can include two bounded local-model attempts. Keep the
    # dispatcher lease longer than the per-job lease so a second daemon cannot
    # become active while the first is inside that bounded handler.
    dispatcher_ttl = max(int(config["dispatcher"].get("lease_seconds", 600)) + 60, interval * 4)
    if not store.acquire_dispatcher(scheduler.owner, dispatcher_ttl):
        active = store.dispatcher()
        print(json.dumps({"event": "scheduler_already_running", "active": active}), flush=True)
        return 2
    # Only the process that became the dispatcher may consume an old stop
    # marker. A duplicate launch must never erase a stop request intended for
    # the active daemon.
    STOP_PATH.unlink(missing_ok=True)
    print(json.dumps({"event": "scheduler_started", "owner": scheduler.owner, "jobs": len(store.list_jobs())}), flush=True)
    try:
        while not STOP_PATH.exists():
            if not store.renew_dispatcher(scheduler.owner, dispatcher_ttl):
                print(json.dumps({"event": "dispatcher_lease_lost", "owner": scheduler.owner}), flush=True)
                return 3
            result = scheduler.run_once()
            payload = status_payload(store, owner=scheduler.owner)
            payload["last_dispatch"] = result
            write_status(config, payload)
            if result["status"] != "idle":
                print(json.dumps(result, ensure_ascii=False), flush=True)
            for _ in range(interval):
                if STOP_PATH.exists():
                    break
                time.sleep(1)
        payload = status_payload(store, owner=scheduler.owner)
        payload["status"] = "stopped"
        payload["stopped_at"] = utc_now()
        write_status(config, payload)
        print(json.dumps({"event": "scheduler_stopped", "owner": scheduler.owner}), flush=True)
        return 0
    finally:
        store.release_dispatcher(scheduler.owner)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Sabrina's bounded customer-job scheduler")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    sub.add_parser("once")
    sub.add_parser("status")
    sub.add_parser("stop")
    resume_parser = sub.add_parser("resume")
    resume_parser.add_argument("job_id")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args()

    if args.command == "stop":
        STOP_PATH.parent.mkdir(parents=True, exist_ok=True)
        STOP_PATH.write_text(utc_now(), encoding="utf-8")
        print(json.dumps({"status": "stop_requested", "path": str(STOP_PATH)}))
        return 0

    config = load_scheduler_config(args.config)
    store = JobStore(args.db)
    try:
        store.sync(config)
        if args.command == "run":
            return run_forever(store, config)
        if args.command == "once":
            scheduler = CustomerJobScheduler(store, config)
            result = scheduler.run_once()
            payload = status_payload(store, owner=scheduler.owner)
            payload["last_dispatch"] = result
            write_status(config, payload)
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if result["status"] != "error" else 1
        if args.command == "resume":
            value = store.resume(args.job_id)
            print(json.dumps({"status": "resumed", "job": value}, ensure_ascii=False, indent=2))
            return 0
        payload = status_payload(store)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
