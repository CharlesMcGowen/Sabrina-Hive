"""Queue exact operator approved meeting proposals while calendar actions remain unperformed."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.customer_engagement import load_customer_policy, validate_natural_style
from tools.customer_engagement_loop import _is_transient_browser_error
from tools.gmail_customer_reader import GmailCustomerReader
from tools.gmail_message_sender import GmailMessageSender
from tools.messaging_loop import browser_lease
from tools.messaging_outbox import MessageOutbox
from tools.messaging_store import ConversationStore, ROOT, digest, now


FALSE_BOOKING_CLAIM = re.compile(
    r"\b(?:is|has been|was)\s+(?:booked|scheduled|confirmed)\b|\bcalendar invite (?:was|has been) sent\b",
    re.IGNORECASE,
)


def _aware_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(field + " must be an ISO datetime") from error
    if parsed.tzinfo is None:
        raise ValueError(field + " must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_meeting_slots(slots: list[dict[str, str]], not_before_utc: str) -> list[dict[str, str]]:
    if not isinstance(slots, list) or not 1 <= len(slots) <= 3:
        raise ValueError("One to three meeting slots are required")
    boundary = _aware_utc(not_before_utc, "not_before_utc")
    normalized = []
    seen = set()
    for slot in slots:
        if not isinstance(slot, dict):
            raise ValueError("Each meeting slot must be an object")
        start = _aware_utc(slot.get("start"), "slot.start")
        end = _aware_utc(slot.get("end"), "slot.end")
        duration = (end - start).total_seconds() / 60
        if start < boundary:
            raise ValueError("Meeting slot violates the minimum notice boundary")
        if not 15 <= duration <= 120:
            raise ValueError("Meeting duration must be between 15 and 120 minutes")
        identity = (start.isoformat(), end.isoformat())
        if identity in seen:
            raise ValueError("Meeting slots must be unique")
        seen.add(identity)
        normalized.append({"start_utc": identity[0], "end_utc": identity[1], "duration_minutes": int(duration)})
    normalized.sort(key=lambda row: row["start_utc"])
    for previous, current in zip(normalized, normalized[1:]):
        if datetime.fromisoformat(current["start_utc"]) < datetime.fromisoformat(previous["end_utc"]):
            raise ValueError("Meeting slots must not overlap")
    return normalized


class MeetingApprovalQueue:
    def __init__(self, store: ConversationStore):
        self.store = store
        self.db = store.db
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS meeting_reply_approvals(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              action_key TEXT NOT NULL UNIQUE,
              account TEXT NOT NULL,
              thread TEXT NOT NULL,
              context_version INTEGER NOT NULL,
              reply_hash TEXT NOT NULL,
              reply_text TEXT NOT NULL,
              slots TEXT NOT NULL,
              not_before_utc TEXT NOT NULL,
              operator_approval_id TEXT NOT NULL,
              operator_instruction TEXT NOT NULL,
              availability_verified_by TEXT NOT NULL,
              availability_verification_basis TEXT NOT NULL,
              decision_id INTEGER NOT NULL,
              outbox_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              receipt TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )"""
        )
        self.db.commit()

    def get(self, approval_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM meeting_reply_approvals WHERE id=?", (int(approval_id),)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        value = dict(row)
        value["slots"] = json.loads(value["slots"])
        value["receipt"] = json.loads(value["receipt"]) if value.get("receipt") else None
        return value

    def queue_proposal(
        self,
        *,
        account: str,
        thread: str,
        reply_text: str,
        slots: list[dict[str, str]],
        not_before_utc: str,
        operator_approval_id: str,
        operator_instruction: str,
        availability_verified_by: str,
        availability_verification_basis: str,
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        required = (
            operator_approval_id,
            operator_instruction,
            availability_verified_by,
            availability_verification_basis,
        )
        if not all(isinstance(value, str) and value.strip() for value in required):
            raise ValueError("Operator approval and availability verification evidence are required")
        policy = policy or load_customer_policy()
        snapshot = self.store.snapshot(account, thread)
        if not snapshot.get("complete") or snapshot["messages"][-1].get("ownership") != "external":
            raise ValueError("Meeting approval requires a complete unanswered customer message")
        contacts = [row for row in policy["contacts"] if row.get("id") == snapshot.get("contact_id")]
        if (
            account.lower() != policy["account_email"].lower()
            or len(contacts) != 1
            or snapshot.get("peer_id", "").lower() != contacts[0]["email"].lower()
        ):
            raise ValueError("Meeting approval is outside the configured account or contact allowlist")
        normalized_slots = validate_meeting_slots(slots, not_before_utc)
        reply_text = validate_natural_style(reply_text, policy)
        if FALSE_BOOKING_CLAIM.search(reply_text):
            raise ValueError("Meeting proposal falsely claims a booking or calendar action")
        reply_hash = digest(reply_text)
        action_key = digest([
            account,
            thread,
            snapshot["version"],
            reply_hash,
            normalized_slots,
            operator_approval_id.strip(),
        ])
        existing = self.db.execute(
            "SELECT id FROM meeting_reply_approvals WHERE action_key=?", (action_key,)
        ).fetchone()
        if existing:
            return self.get(existing["id"])
        profile_hash = "meeting-approval-v1:" + action_key
        decision = {
            "action": "draft_reply",
            "category": "call_or_meeting",
            "draft": reply_text,
            "reason": "Exact operator approved meeting proposal",
            "next_action": "send_exact_approved_meeting_options",
            "missing_information": [],
            "commitments": ["operator verified proposed meeting availability"],
            "testing_authorized": False,
            "meeting_action_key": action_key,
        }
        decision_row = self.db.execute(
            """SELECT id,status FROM decisions
               WHERE account=? AND thread=? AND context_version=? AND profile_hash=?""",
            (account, thread, snapshot["version"], profile_hash),
        ).fetchone()
        if decision_row is None:
            self.store.save_decision(account, thread, snapshot["version"], profile_hash, decision)
            decision_row = self.db.execute(
                """SELECT id,status FROM decisions
                   WHERE account=? AND thread=? AND context_version=? AND profile_hash=?""",
                (account, thread, snapshot["version"], profile_hash),
            ).fetchone()
        if decision_row["status"] != "draft":
            raise ValueError("Approved meeting proposal is not sendable")
        outbox_id = MessageOutbox(self.store).authorize(
            decision_row["id"],
            "Exact meeting proposal approved by operator: " + operator_instruction.strip(),
        )
        timestamp = now()
        with self.db:
            self.db.execute(
                """INSERT INTO meeting_reply_approvals(
                  action_key,account,thread,context_version,reply_hash,reply_text,slots,not_before_utc,
                  operator_approval_id,operator_instruction,availability_verified_by,
                  availability_verification_basis,decision_id,outbox_id,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    action_key,
                    account,
                    thread,
                    snapshot["version"],
                    reply_hash,
                    reply_text,
                    json.dumps(normalized_slots, ensure_ascii=False),
                    _aware_utc(not_before_utc, "not_before_utc").isoformat(),
                    operator_approval_id.strip(),
                    operator_instruction.strip(),
                    availability_verified_by.strip(),
                    availability_verification_basis.strip(),
                    int(decision_row["id"]),
                    int(outbox_id),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(self.db.execute(
            "SELECT id FROM meeting_reply_approvals WHERE action_key=?", (action_key,)
        ).fetchone()["id"])

    def queued_for_contact(self, contact_id: str) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT id,account,thread FROM meeting_reply_approvals WHERE status='queued' ORDER BY id"
        ).fetchall()
        matches = []
        for row in rows:
            try:
                snapshot = self.store.snapshot(row["account"], row["thread"])
            except KeyError:
                continue
            if snapshot.get("contact_id") == contact_id:
                matches.append(self.get(row["id"]))
        if len(matches) > 1:
            raise RuntimeError("Multiple meeting proposals are queued for one contact")
        return matches[0] if matches else None

    def finish(self, approval_id: int, receipt: dict[str, Any]) -> dict[str, Any]:
        approval = self.get(approval_id)
        if approval["status"] != "queued":
            raise ValueError("Meeting approval is not queued")
        status = receipt.get("status")
        if status not in {"sent", "uncertain"}:
            raise ValueError("Meeting delivery receipt is invalid")
        outbox = MessageOutbox(self.store).get(approval["outbox_id"])
        if outbox["status"] != status:
            raise ValueError("Meeting receipt does not match the outbox state")
        with self.db:
            self.db.execute(
                "UPDATE meeting_reply_approvals SET status=?,receipt=?,updated_at=? WHERE id=? AND status='queued'",
                (status, json.dumps(receipt, ensure_ascii=False), now(), int(approval_id)),
            )
        return self.get(approval_id)


def deliver_queued_meeting_reply(
    contact_id: str,
    *,
    cdp_url: str | None = None,
    messaging_db: str | Path | None = None,
) -> dict[str, Any] | None:
    store = ConversationStore(messaging_db)
    queue = MeetingApprovalQueue(store)
    reader = GmailCustomerReader(cdp_url)
    approval = queue.queued_for_contact(contact_id)
    if approval is None:
        store.close()
        return None
    result = {
        "status": "starting",
        "contact_id": contact_id,
        "account_id": approval["account"],
        "thread_id": approval["thread"],
        "approval_id": approval["id"],
        "decision": {"category": "call_or_meeting"},
        "calendar_event_created": False,
        "operator_notification_sent": False,
        "sent": False,
    }
    try:
        with browser_lease(ROOT / "data/customer_email_browser.lock"):
            reader.connect()
            receipt = GmailMessageSender(reader, store, MessageOutbox(store)).send(approval["outbox_id"])
            queue.finish(approval["id"], receipt)
            result["receipt"] = receipt
            result["sent"] = receipt["status"] == "sent"
            result["status"] = receipt["status"]
            return result
    except Exception as error:
        result["error"] = str(error)
        result["status"] = "retry" if _is_transient_browser_error(error) else "blocked"
        if result["status"] == "retry":
            result["retry_after_seconds"] = 60
        return result
    finally:
        reader.disconnect()
        store.close()
