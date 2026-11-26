"""Revocable standing authority for bounded customer price negotiations.

The operator approves the complete envelope before it can be used. Customer
replies are deterministic and remain conditional on a mutually signed SOW and
rules of engagement. This module cannot approve legal terms or testing.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from tools.customer_commercial_approval import CommercialApprovalQueue
from tools.customer_engagement import detect_escalations, load_customer_policy, validate_natural_style
from tools.cyber_sales import load_commercial_policy
from tools.messaging_store import ConversationStore, digest, now


AMOUNT_PATTERN = re.compile(r"(?:\$|\bUSD\s*)(\d[\d,]*(?:\.\d{1,2})?)", re.IGNORECASE)
PERCENT_PATTERN = re.compile(r"\b(\d{1,2}(?:\.\d+)?)\s*(?:%|percent)\b", re.IGNORECASE)
FORBIDDEN_ENVELOPE_TEXT = re.compile(
    r"\b(?:GPU|Podman|RunPod|Astra|Terra|Luna|token cost|model cost|internal cost|margin|markup)\b",
    re.IGNORECASE,
)
NONNEGOTIABLE_CONTROLS = {
    "written_authorization": True,
    "explicit_scope": True,
    "data_handling": True,
    "evidence_quality": True,
}


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Negotiation authority expiry must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise ValueError("Negotiation authority expiry must include a timezone")
    return parsed.astimezone(timezone.utc)


def _money(value: float) -> str:
    amount = f"${value:,.2f}"
    return amount[:-3] if amount.endswith(".00") else amount


def _configured_envelope(commercial: dict[str, Any]) -> tuple[float, float, float, str]:
    missing = [
        key for key in ("target_price", "reservation_price", "maximum_discount_percent")
        if commercial.get(key) is None
    ]
    if missing:
        raise ValueError("Commercial policy is missing: " + ", ".join(missing))
    target = float(commercial["target_price"])
    reservation = float(commercial["reservation_price"])
    maximum_discount = float(commercial["maximum_discount_percent"])
    currency = str(commercial.get("currency", "")).strip().upper()
    if target <= 0 or reservation <= 0 or reservation > target:
        raise ValueError("Commercial target and reservation prices are invalid")
    if not 0 <= maximum_discount <= 100:
        raise ValueError("Maximum discount must be between zero and 100")
    discount_floor = target * (1 - maximum_discount / 100)
    if reservation + 0.005 < discount_floor:
        raise ValueError("Reservation price is below the configured discount floor")
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("Commercial currency must be a three letter code")
    return target, reservation, maximum_discount, currency


class NegotiationAuthorityStore:
    def __init__(
        self,
        store: ConversationStore,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.db = store.db
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS commercial_negotiation_authorities(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              account TEXT NOT NULL,
              thread TEXT NOT NULL,
              contact_id TEXT NOT NULL,
              authority_hash TEXT NOT NULL UNIQUE,
              envelope TEXT NOT NULL,
              operator_approval_id TEXT NOT NULL,
              operator_instruction TEXT NOT NULL,
              approved_by TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              max_turns INTEGER NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              revoked_at TEXT
            );
            CREATE TABLE IF NOT EXISTS commercial_negotiation_moves(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              authority_id INTEGER NOT NULL,
              account TEXT NOT NULL,
              thread TEXT NOT NULL,
              context_version INTEGER NOT NULL,
              customer_message_id TEXT NOT NULL,
              proposed_price REAL NOT NULL,
              discount_percent REAL NOT NULL,
              scope_trade_used INTEGER NOT NULL,
              commercial_approval_id INTEGER NOT NULL,
              outbox_id INTEGER NOT NULL,
              move_hash TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              UNIQUE(authority_id,context_version)
            );
            """
        )
        self.db.commit()

    def get(self, authority_id: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM commercial_negotiation_authorities WHERE id=?",
            (int(authority_id),),
        ).fetchone()
        if row is None:
            raise KeyError(authority_id)
        value = dict(row)
        value["envelope"] = json.loads(value["envelope"])
        return value

    def grant(
        self,
        *,
        account: str,
        thread: str,
        service_label: str,
        payment_terms: str,
        scope_trade: str,
        expires_at: str,
        max_turns: int,
        operator_approval_id: str,
        operator_instruction: str,
        approved_by: str,
        commercial: dict[str, Any] | None = None,
        customer_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        commercial = commercial or load_commercial_policy()
        customer_policy = customer_policy or load_customer_policy()
        if commercial.get("operator_approval_required") is not True:
            raise ValueError("Commercial policy must retain operator approval")
        target, reservation, maximum_discount, currency = _configured_envelope(commercial)
        if not all(
            isinstance(value, str) and value.strip()
            for value in (operator_approval_id, operator_instruction, approved_by)
        ):
            raise ValueError("Explicit operator approval identity and instruction are required")
        if not isinstance(max_turns, int) or not 1 <= max_turns <= 5:
            raise ValueError("Negotiation authority must allow between one and five turns")
        expiry = _parse_utc(expires_at)
        current = self.now_fn().astimezone(timezone.utc)
        if not current < expiry <= current + timedelta(days=366):
            raise ValueError("Negotiation authority must expire within one year")
        snapshot = self.store.snapshot(account, thread)
        if not snapshot.get("complete") or snapshot["messages"][-1].get("ownership") != "external":
            raise ValueError("Negotiation authority requires a complete unanswered customer thread")
        contacts = [
            row for row in customer_policy["contacts"]
            if row.get("id") == snapshot.get("contact_id")
        ]
        if (
            account.lower() != customer_policy["account_email"].lower()
            or len(contacts) != 1
            or snapshot.get("peer_id", "").lower() != contacts[0]["email"].lower()
        ):
            raise ValueError("Negotiation authority is outside the customer allowlist")
        service_label = str(service_label).strip()
        payment_terms = str(payment_terms).strip()
        scope_trade = str(scope_trade).strip()
        if not service_label or not payment_terms or not scope_trade:
            raise ValueError("Service, payment and scope-trade terms are required")
        envelope_text = " ".join((service_label, payment_terms, scope_trade))
        if FORBIDDEN_ENVELOPE_TEXT.search(envelope_text):
            raise ValueError("Negotiation authority exposes internal implementation or economics")
        validate_natural_style(
            f"The service is {service_label}. Payment terms are {payment_terms}. {scope_trade}",
            customer_policy,
        )
        envelope = {
            "currency": currency,
            "target_price": target,
            "reservation_price": reservation,
            "maximum_discount_percent": maximum_discount,
            "service_label": service_label,
            "payment_terms": payment_terms,
            "scope_trade": scope_trade,
            "legal_condition": "Subject to a mutually signed statement of work and rules of engagement.",
            "testing_authorized": False,
            "nonnegotiable_controls": NONNEGOTIABLE_CONTROLS,
        }
        authority_hash = digest([
            account,
            thread,
            snapshot["contact_id"],
            envelope,
            expiry.isoformat(),
            max_turns,
            operator_approval_id.strip(),
        ])
        existing = self.db.execute(
            "SELECT id FROM commercial_negotiation_authorities WHERE authority_hash=?",
            (authority_hash,),
        ).fetchone()
        if existing:
            previous = self.get(existing["id"])
            if previous["status"] == "active":
                return previous
            raise ValueError("This negotiation authority was previously revoked or expired")
        timestamp = now()
        with self.db:
            self.db.execute(
                """UPDATE commercial_negotiation_authorities
                   SET status='revoked',revoked_at=?
                   WHERE account=? AND thread=? AND status='active'""",
                (timestamp, account, thread),
            )
            cursor = self.db.execute(
                """INSERT INTO commercial_negotiation_authorities(
                  account,thread,contact_id,authority_hash,envelope,operator_approval_id,
                  operator_instruction,approved_by,expires_at,max_turns,status,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'active',?)""",
                (
                    account,
                    thread,
                    snapshot["contact_id"],
                    authority_hash,
                    json.dumps(envelope, ensure_ascii=False),
                    operator_approval_id.strip(),
                    operator_instruction.strip(),
                    approved_by.strip(),
                    expiry.isoformat(),
                    max_turns,
                    timestamp,
                ),
            )
        return self.get(int(cursor.lastrowid))

    def revoke(self, authority_id: int) -> dict[str, Any]:
        with self.db:
            changed = self.db.execute(
                """UPDATE commercial_negotiation_authorities
                   SET status='revoked',revoked_at=? WHERE id=? AND status='active'""",
                (now(), int(authority_id)),
            ).rowcount
        if changed != 1:
            raise ValueError("Negotiation authority is not active")
        return self.get(authority_id)

    def active(self, account: str, thread: str) -> dict[str, Any] | None:
        rows = self.db.execute(
            """SELECT id FROM commercial_negotiation_authorities
               WHERE account=? AND thread=? AND status='active' ORDER BY id DESC""",
            (account, thread),
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise RuntimeError("Multiple active negotiation authorities exist for one thread")
        authority = self.get(rows[0]["id"])
        if _parse_utc(authority["expires_at"]) <= self.now_fn().astimezone(timezone.utc):
            with self.db:
                self.db.execute(
                    "UPDATE commercial_negotiation_authorities SET status='expired' WHERE id=?",
                    (authority["id"],),
                )
            return None
        return authority

    def move_count(self, authority_id: int) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM commercial_negotiation_moves WHERE authority_id=?",
            (int(authority_id),),
        ).fetchone()[0])


def grant_negotiation_authority(
    *,
    messaging_db: str | Path | None = None,
    **values: Any,
) -> dict[str, Any]:
    store = ConversationStore(messaging_db)
    try:
        return NegotiationAuthorityStore(store).grant(**values)
    finally:
        store.close()


def queue_authorized_negotiation_move(
    outcome: dict[str, Any],
    *,
    messaging_db: str | Path | None = None,
    commercial: dict[str, Any] | None = None,
    customer_policy: dict[str, Any] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    decision = outcome.get("decision") or {}
    if decision.get("category") != "price_or_discount":
        return {"status": "not_applicable", "customer_reply_authorized": False}
    account = outcome.get("account_id")
    thread = outcome.get("thread_id")
    if not account or not thread:
        return {
            "status": "not_authorized",
            "reason": "Commercial outcome is missing conversation identity",
            "customer_reply_authorized": False,
        }
    commercial = commercial or load_commercial_policy()
    customer_policy = customer_policy or load_customer_policy()
    store = ConversationStore(messaging_db)
    try:
        authorities = NegotiationAuthorityStore(store, now_fn=now_fn)
        authority = authorities.active(account, thread)
        if authority is None:
            return {
                "status": "not_authorized",
                "reason": "No active operator-approved negotiation authority",
                "customer_reply_authorized": False,
            }
        snapshot = store.snapshot(account, thread)
        latest = snapshot["messages"][-1]
        if latest.get("ownership") != "external" or not snapshot.get("complete"):
            return {
                "status": "requires_operator",
                "reason": "The customer thread is incomplete or already answered",
                "customer_reply_authorized": False,
            }
        escalations = detect_escalations(latest.get("text", ""))
        additional = [category for category in escalations if category != "price_or_discount"]
        if additional:
            return {
                "status": "requires_operator",
                "reason": "The message also requires review for: " + ", ".join(additional),
                "customer_reply_authorized": False,
            }
        if authorities.move_count(authority["id"]) >= int(authority["max_turns"]):
            return {
                "status": "requires_operator",
                "reason": "The approved negotiation turn limit is exhausted",
                "customer_reply_authorized": False,
            }
        existing = store.db.execute(
            """SELECT * FROM commercial_negotiation_moves
               WHERE authority_id=? AND context_version=?""",
            (authority["id"], snapshot["version"]),
        ).fetchone()
        if existing:
            return {
                "status": "queued",
                "authority_id": authority["id"],
                "commercial_approval_id": int(existing["commercial_approval_id"]),
                "outbox_id": int(existing["outbox_id"]),
                "customer_reply_authorized": True,
                "idempotent": True,
            }
        envelope = authority["envelope"]
        target = float(envelope["target_price"])
        reservation = float(envelope["reservation_price"])
        maximum_discount = float(envelope["maximum_discount_percent"])
        discount_floor = target * (1 - maximum_discount / 100)
        floor = max(reservation, discount_floor)
        text = latest.get("text", "")
        amount_match = AMOUNT_PATTERN.search(text)
        percent_match = PERCENT_PATTERN.search(text)
        requested_price = None
        if amount_match:
            requested_price = float(amount_match.group(1).replace(",", ""))
        elif percent_match:
            requested_price = target * (1 - float(percent_match.group(1)) / 100)
        proposed_price = min(target, requested_price) if requested_price is not None else target
        scope_trade_used = proposed_price + 0.005 < floor
        if scope_trade_used:
            proposed_price = floor
        proposed_price = round(proposed_price, 2)
        discount_percent = round(max(0.0, (target - proposed_price) / target * 100), 4)
        first_name = str(snapshot.get("peer_name", "")).strip().split(" ")[0]
        greeting = f"Thanks, {first_name}." if first_name else "Thanks."
        reply_parts = [
            greeting,
            (
                f"I can propose {envelope['service_label']} at {_money(proposed_price)} "
                f"{envelope['currency']}, subject to a mutually signed statement of work and rules of engagement."
            ),
        ]
        if scope_trade_used:
            reply_parts.append(envelope["scope_trade"])
        reply_parts.append(f"The payment terms are {envelope['payment_terms']}.")
        reply_text = validate_natural_style(" ".join(reply_parts), customer_policy)
        terms = {
            "price": proposed_price,
            "discount_percent": discount_percent,
            "delivery_date": None,
            "payment_terms": envelope["payment_terms"],
            "legal_terms": envelope["legal_condition"],
            **NONNEGOTIABLE_CONTROLS,
        }
        approval = CommercialApprovalQueue(store).queue(
            account=account,
            thread=thread,
            category="price_or_discount",
            reply_text=reply_text,
            terms=terms,
            operator_approval_id=authority["operator_approval_id"],
            operator_instruction=(
                f"Apply standing negotiation authority {authority['id']}: "
                + authority["operator_instruction"]
            ),
            approved_by=authority["approved_by"],
            policy=customer_policy,
            commercial_policy=commercial,
        )
        move_hash = digest([
            authority["id"], snapshot["version"], latest["id"], proposed_price,
            discount_percent, scope_trade_used, approval["reply_hash"],
        ])
        with store.db:
            store.db.execute(
                """INSERT INTO commercial_negotiation_moves(
                  authority_id,account,thread,context_version,customer_message_id,
                  proposed_price,discount_percent,scope_trade_used,commercial_approval_id,
                  outbox_id,move_hash,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    authority["id"], account, thread, snapshot["version"], latest["id"],
                    proposed_price, discount_percent, int(scope_trade_used), approval["id"],
                    approval["outbox_id"], move_hash, now(),
                ),
            )
        return {
            "status": "queued",
            "authority_id": authority["id"],
            "commercial_approval_id": approval["id"],
            "outbox_id": approval["outbox_id"],
            "proposed_price": proposed_price,
            "currency": envelope["currency"],
            "discount_percent": discount_percent,
            "scope_trade_used": scope_trade_used,
            "turns_used": authorities.move_count(authority["id"]),
            "turns_allowed": authority["max_turns"],
            "customer_reply_authorized": True,
            "model_generated": False,
            "testing_authorized": False,
        }
    finally:
        store.close()
