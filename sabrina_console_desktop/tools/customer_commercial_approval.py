"""Queue and deliver exact operator approved commercial replies without model rewriting."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from tools.customer_commercial_handoff import COMMERCIAL_CATEGORIES
from tools.customer_engagement import load_customer_policy, validate_natural_style
from tools.customer_engagement_loop import _is_transient_browser_error
from tools.cyber_sales import validate_commercial_move
from tools.gmail_customer_reader import GmailCustomerReader
from tools.gmail_message_sender import GmailMessageSender
from tools.messaging_loop import browser_lease
from tools.messaging_outbox import MessageOutbox
from tools.messaging_store import ConversationStore, ROOT, digest, now


NONNEGOTIABLE_CONTROLS = ("written_authorization", "explicit_scope", "data_handling", "evidence_quality")
FORBIDDEN_REPLY_TERMS = re.compile(
    r"\b(?:GPU|Podman|RunPod|Astra|Terra|Luna|model cost|token cost|internal cost|margin|markup)\b",
    re.IGNORECASE,
)
UNAUTHORIZED_TESTING_CLAIM = re.compile(
    r"\b(?:we|I|Sabrina)\s+(?:have\s+)?(?:started|begun|authorized|approved)\s+(?:the\s+)?(?:test|testing|assessment)",
    re.IGNORECASE,
)


class CommercialApprovalQueue:
    def __init__(self, store: ConversationStore):
        self.store = store
        self.db = store.db
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS commercial_reply_approvals(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              action_key TEXT NOT NULL UNIQUE,
              account TEXT NOT NULL,
              thread TEXT NOT NULL,
              context_version INTEGER NOT NULL,
              category TEXT NOT NULL,
              reply_hash TEXT NOT NULL,
              terms_hash TEXT NOT NULL,
              reply_text TEXT NOT NULL,
              terms TEXT NOT NULL,
              operator_approval_id TEXT NOT NULL,
              operator_instruction TEXT NOT NULL,
              approved_by TEXT NOT NULL,
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
        row = self.db.execute("SELECT * FROM commercial_reply_approvals WHERE id=?", (int(approval_id),)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        value = dict(row)
        value["terms"] = json.loads(value["terms"])
        value["receipt"] = json.loads(value["receipt"]) if value.get("receipt") else None
        return value

    def queue(
        self,
        *,
        account: str,
        thread: str,
        category: str,
        reply_text: str,
        terms: dict[str, Any],
        operator_approval_id: str,
        operator_instruction: str,
        approved_by: str,
        policy: dict[str, Any] | None = None,
        commercial_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if category not in COMMERCIAL_CATEGORIES:
            raise ValueError("Only commercial categories can use a commercial reply approval")
        if not all(isinstance(value, str) and value.strip() for value in (operator_approval_id, operator_instruction, approved_by)):
            raise ValueError("Explicit operator approval identity and instruction are required")
        policy = policy or load_customer_policy()
        snapshot = self.store.snapshot(account, thread)
        if not snapshot.get("complete") or snapshot["messages"][-1].get("ownership") != "external":
            raise ValueError("Commercial approval requires a complete unanswered customer message")
        contacts = [row for row in policy["contacts"] if row.get("id") == snapshot.get("contact_id")]
        if (
            account.lower() != policy["account_email"].lower()
            or len(contacts) != 1
            or snapshot.get("peer_id", "").lower() != contacts[0]["email"].lower()
        ):
            raise ValueError("Commercial approval is outside the configured account or contact allowlist")
        reply_text = validate_natural_style(reply_text, policy)
        if FORBIDDEN_REPLY_TERMS.search(reply_text):
            raise ValueError("Commercial reply exposes internal implementation or economics")
        if UNAUTHORIZED_TESTING_CLAIM.search(reply_text):
            raise ValueError("Commercial reply claims testing authority or execution")
        if not isinstance(terms, dict):
            raise ValueError("Approved commercial terms must be an object")
        approved_terms = {**terms, "operator_approval_id": operator_approval_id.strip()}
        if any(approved_terms.get(control) is not True for control in NONNEGOTIABLE_CONTROLS):
            raise ValueError("Every nonnegotiable commercial control must be preserved")
        approved_terms["testing_authorized"] = False
        validate_commercial_move(approved_terms, policy=commercial_policy)
        reply_hash = digest(reply_text)
        terms_hash = digest(approved_terms)
        action_key = digest([account, thread, snapshot["version"], category, reply_hash, terms_hash])
        existing = self.db.execute(
            "SELECT id FROM commercial_reply_approvals WHERE action_key=?", (action_key,)
        ).fetchone()
        if existing:
            return self.get(existing["id"])

        profile_hash = "commercial-approval-v1:" + digest([action_key, operator_approval_id.strip()])
        decision = {
            "action": "draft_reply",
            "category": category,
            "draft": reply_text,
            "reason": "Exact operator approved commercial reply",
            "next_action": "send_exact_approved_reply",
            "missing_information": [],
            "commitments": ["operator approved commercial terms"],
            "testing_authorized": False,
            "commercial_action_key": action_key,
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
            raise ValueError("Approved commercial decision is not sendable")
        outbox = MessageOutbox(self.store)
        outbox_id = outbox.authorize(
            decision_row["id"],
            "Exact commercial reply approved by operator: " + operator_instruction.strip(),
        )
        timestamp = now()
        with self.db:
            self.db.execute(
                """INSERT INTO commercial_reply_approvals(
                  action_key,account,thread,context_version,category,reply_hash,terms_hash,reply_text,terms,
                  operator_approval_id,operator_instruction,approved_by,decision_id,outbox_id,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'queued',?,?)""",
                (
                    action_key,
                    account,
                    thread,
                    snapshot["version"],
                    category,
                    reply_hash,
                    terms_hash,
                    reply_text,
                    json.dumps(approved_terms, ensure_ascii=False),
                    operator_approval_id.strip(),
                    operator_instruction.strip(),
                    approved_by.strip(),
                    int(decision_row["id"]),
                    int(outbox_id),
                    timestamp,
                    timestamp,
                ),
            )
        return self.get(self.db.execute(
            "SELECT id FROM commercial_reply_approvals WHERE action_key=?", (action_key,)
        ).fetchone()["id"])

    def queued_for_contact(self, contact_id: str) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT id,account,thread FROM commercial_reply_approvals WHERE status='queued' ORDER BY id"
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
            raise RuntimeError("Multiple approved commercial replies are queued for one contact")
        return matches[0] if matches else None

    def finish(self, approval_id: int, receipt: dict[str, Any]) -> dict[str, Any]:
        approval = self.get(approval_id)
        if approval["status"] != "queued":
            raise ValueError("Commercial approval is not queued")
        status = receipt.get("status")
        if status not in {"sent", "uncertain"}:
            raise ValueError("Commercial delivery receipt is invalid")
        outbox = MessageOutbox(self.store).get(approval["outbox_id"])
        if outbox["status"] != status:
            raise ValueError("Commercial receipt does not match the outbox state")
        with self.db:
            self.db.execute(
                "UPDATE commercial_reply_approvals SET status=?,receipt=?,updated_at=? WHERE id=? AND status='queued'",
                (status, json.dumps(receipt, ensure_ascii=False), now(), int(approval_id)),
            )
        return self.get(approval_id)


def deliver_queued_commercial_reply(
    contact_id: str,
    *,
    cdp_url: str | None = None,
    messaging_db: str | Path | None = None,
) -> dict[str, Any] | None:
    store = ConversationStore(messaging_db)
    queue = CommercialApprovalQueue(store)
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
        "decision": {"category": approval["category"]},
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
