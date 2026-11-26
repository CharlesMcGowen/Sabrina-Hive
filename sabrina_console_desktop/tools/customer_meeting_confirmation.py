"""Confirm one previously approved meeting slot with receipt-backed adapters."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from tools.customer_engagement import load_customer_policy
from tools.messaging_store import ConversationStore, digest, now


AFFIRMATIVE = re.compile(r"\b(?:yes|works|good|fine|confirm|book|schedule|take|choose|prefer)\b", re.I)
NEGATIVE_OR_CHANGE = re.compile(
    r"\b(?:can(?:no|')t|cannot|doesn(?:'t| not)|do not|won't|will not|neither|instead|different|other time|reschedule|change)\b",
    re.I,
)


class UnconfiguredCalendarGateway:
    configured = False
    provider = None


class UnconfiguredOperatorNotifier:
    configured = False
    channel = None


def _aware(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Meeting slot is not an ISO datetime") from error
    if parsed.tzinfo is None:
        raise ValueError("Meeting slot lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _time_tokens(value: datetime) -> set[str]:
    hour = value.strftime("%I").lstrip("0") or "0"
    minute = value.strftime("%M")
    meridiem = value.strftime("%p")
    tokens = {f"{hour}:{minute} {meridiem}", f"{hour}:{minute}{meridiem}"}
    if minute == "00":
        tokens.update({f"{hour} {meridiem}", f"{hour}{meridiem}"})
    return {token.lower() for token in tokens}


def _slot_match_evidence(text: str, slot: dict[str, Any], timezone_name: str) -> dict[str, Any]:
    local = _aware(slot["start_utc"]).astimezone(ZoneInfo(timezone_name))
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    weekday_tokens = {local.strftime("%A").lower(), local.strftime("%a").lower()}
    date_tokens = {
        local.strftime("%B %d").replace(" 0", " ").lower(),
        local.strftime("%b %d").replace(" 0", " ").lower(),
        f"{local.month}/{local.day}",
    }
    time_tokens = _time_tokens(local)
    return {
        "weekday": any(re.search(rf"\b{re.escape(token)}\b", normalized) for token in weekday_tokens),
        "date": any(token in normalized for token in date_tokens),
        "time": any(re.search(rf"\b{re.escape(token)}\b", normalized) for token in time_tokens),
        "local_start": local.isoformat(),
    }


def match_confirmed_slot(
    customer_text: str,
    slots: list[dict[str, Any]],
    *,
    timezone_name: str,
) -> dict[str, Any]:
    """Return one unambiguous slot or a reason requiring operator review."""
    text = str(customer_text or "").strip()
    if not text or not AFFIRMATIVE.search(text) or NEGATIVE_OR_CHANGE.search(text):
        return {"status": "requires_operator", "reason": "Customer confirmation is absent, negative, or requests a change"}
    ordinal = None
    if re.search(r"\b(?:first|1st)\b", text, re.I):
        ordinal = 0
    elif re.search(r"\b(?:second|2nd)\b", text, re.I):
        ordinal = 1
    elif re.search(r"\b(?:third|3rd)\b", text, re.I):
        ordinal = 2
    if ordinal is not None and ordinal < len(slots):
        return {"status": "matched", "slot": slots[ordinal], "match_basis": "ordinal"}
    evidence = [_slot_match_evidence(text, slot, timezone_name) for slot in slots]
    candidates = [
        index for index, row in enumerate(evidence)
        if row["time"] and (row["weekday"] or row["date"])
    ]
    if not candidates:
        day_candidates = [index for index, row in enumerate(evidence) if row["weekday"] or row["date"]]
        time_candidates = [index for index, row in enumerate(evidence) if row["time"]]
        candidates = day_candidates if len(day_candidates) == 1 else time_candidates if len(time_candidates) == 1 else []
    if len(candidates) != 1:
        return {
            "status": "requires_operator",
            "reason": "Customer selection does not identify exactly one offered slot",
            "candidate_count": len(candidates),
        }
    index = candidates[0]
    return {
        "status": "matched",
        "slot": slots[index],
        "match_basis": "local_day_and_time" if evidence[index]["time"] else "unique_local_day",
        "local_start": evidence[index]["local_start"],
    }


def _validate_availability_receipt(receipt: dict[str, Any], slot: dict[str, Any]) -> None:
    if not isinstance(receipt, dict) or receipt.get("status") not in {"available", "busy"}:
        raise ValueError("Calendar availability receipt is invalid")
    if not receipt.get("provider") or not receipt.get("checked_at"):
        raise ValueError("Calendar availability receipt lacks provider evidence")
    if receipt.get("start_utc") != slot["start_utc"] or receipt.get("end_utc") != slot["end_utc"]:
        raise ValueError("Calendar availability receipt does not match the selected slot")


def _validate_event_receipt(receipt: dict[str, Any], request: dict[str, Any]) -> None:
    required = ("provider", "event_id", "created_at", "start_utc", "end_utc", "attendee_email")
    if not isinstance(receipt, dict) or receipt.get("status") != "created":
        raise ValueError("Calendar event receipt is invalid")
    if any(not receipt.get(field) for field in required):
        raise ValueError("Calendar event receipt is incomplete")
    for field in ("start_utc", "end_utc", "attendee_email"):
        if receipt[field] != request[field]:
            raise ValueError("Calendar event receipt does not match the approved request")
    if receipt.get("attendee_notification_sent") is not True:
        raise ValueError("Calendar provider did not prove the attendee invitation was sent")


def process_customer_meeting_confirmation(
    outcome: dict[str, Any],
    *,
    messaging_db: str | Path | None = None,
    calendar_gateway: Any | None = None,
    notifier: Any | None = None,
    policy: dict[str, Any] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    decision = outcome.get("decision") or {}
    if decision.get("category") != "call_or_meeting":
        return {"status": "not_applicable", "calendar_event_created": False}
    account = outcome.get("account_id")
    thread = outcome.get("thread_id")
    if not account or not thread:
        return {"status": "not_authorized", "reason": "Meeting outcome lacks conversation identity", "calendar_event_created": False}
    policy = policy or load_customer_policy()
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    calendar_gateway = calendar_gateway or UnconfiguredCalendarGateway()
    notifier = notifier or UnconfiguredOperatorNotifier()
    store = ConversationStore(messaging_db)
    try:
        store.db.execute(
            """CREATE TABLE IF NOT EXISTS meeting_confirmations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              proposal_id INTEGER NOT NULL,
              account TEXT NOT NULL,
              thread TEXT NOT NULL,
              context_version INTEGER NOT NULL,
              customer_message_id TEXT NOT NULL,
              selected_slot TEXT NOT NULL,
              match_basis TEXT NOT NULL,
              availability_receipt TEXT NOT NULL,
              event_receipt TEXT NOT NULL,
              notification_receipt TEXT,
              created_at TEXT NOT NULL,
              UNIQUE(proposal_id,customer_message_id)
            )"""
        )
        store.db.commit()
        snapshot = store.snapshot(account, thread)
        latest = snapshot["messages"][-1]
        if not snapshot.get("complete") or latest.get("ownership") != "external":
            return {"status": "requires_operator", "reason": "Meeting thread is incomplete or already answered", "calendar_event_created": False}
        proposal_table = store.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meeting_reply_approvals'"
        ).fetchone()
        if proposal_table is None:
            return {"status": "not_authorized", "reason": "No meeting proposal history exists", "calendar_event_created": False}
        proposal_row = store.db.execute(
            """SELECT * FROM meeting_reply_approvals
               WHERE account=? AND thread=? AND status='sent'
               ORDER BY id DESC LIMIT 1""",
            (account, thread),
        ).fetchone()
        if proposal_row is None:
            return {"status": "not_authorized", "reason": "No delivered operator-approved meeting proposal exists", "calendar_event_created": False}
        if int(proposal_row["context_version"]) >= int(snapshot["version"]):
            return {"status": "requires_operator", "reason": "No newer customer confirmation exists", "calendar_event_created": False}
        existing = store.db.execute(
            "SELECT * FROM meeting_confirmations WHERE proposal_id=? AND customer_message_id=?",
            (int(proposal_row["id"]), latest["id"]),
        ).fetchone()
        if existing:
            event_receipt = json.loads(existing["event_receipt"])
            notification_receipt = json.loads(existing["notification_receipt"]) if existing["notification_receipt"] else None
            return {
                "status": "booked",
                "confirmation_id": int(existing["id"]),
                "calendar_event_created": True,
                "event_receipt": event_receipt,
                "operator_notification_sent": bool(notification_receipt and notification_receipt.get("status") == "sent"),
                "notification_receipt": notification_receipt,
                "idempotent": True,
            }
        slots = json.loads(proposal_row["slots"])
        timezone_name = policy.get("operator_channels", {}).get("timezone", "America/Chicago")
        match = match_confirmed_slot(latest.get("text", ""), slots, timezone_name=timezone_name)
        if match["status"] != "matched":
            return {**match, "calendar_event_created": False, "offered_slots": slots}
        slot = match["slot"]
        if _aware(slot["start_utc"]) <= now_fn().astimezone(timezone.utc):
            return {"status": "requires_operator", "reason": "The selected meeting slot is no longer in the future", "calendar_event_created": False}
        if not getattr(calendar_gateway, "configured", False):
            return {
                "status": "connection_required",
                "reason": "A calendar connection is required before availability can be rechecked or an event created",
                "selected_slot": slot,
                "calendar_event_created": False,
                "operator_notification_sent": False,
            }
        availability = calendar_gateway.check_availability(slot["start_utc"], slot["end_utc"])
        _validate_availability_receipt(availability, slot)
        if availability["status"] != "available":
            return {"status": "requires_operator", "reason": "The selected slot is no longer available", "availability_receipt": availability, "calendar_event_created": False}
        request = {
            "title": "Authorized security assessment discussion",
            "start_utc": slot["start_utc"],
            "end_utc": slot["end_utc"],
            "organizer_email": account,
            "attendee_email": snapshot["peer_id"],
            "description": "Scope discussion only. This meeting does not authorize testing.",
            "send_attendee_notification": True,
            "idempotency_key": digest([
                int(proposal_row["id"]), latest["id"], slot["start_utc"], slot["end_utc"]
            ]),
        }
        event_receipt = calendar_gateway.create_event(request)
        _validate_event_receipt(event_receipt, request)
        notification_receipt = None
        if getattr(notifier, "configured", False):
            notification_receipt = notifier.notify({
                "kind": "customer_meeting_booked",
                "event_id": event_receipt["event_id"],
                "start_utc": slot["start_utc"],
                "customer_email": snapshot["peer_id"],
            })
            if not isinstance(notification_receipt, dict) or notification_receipt.get("status") != "sent" or not notification_receipt.get("message_id"):
                notification_receipt = {"status": "failed", "reason": "Notification adapter did not return a delivery receipt"}
        with store.db:
            cursor = store.db.execute(
                """INSERT INTO meeting_confirmations(
                  proposal_id,account,thread,context_version,customer_message_id,selected_slot,
                  match_basis,availability_receipt,event_receipt,notification_receipt,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(proposal_row["id"]), account, thread, snapshot["version"], latest["id"],
                    json.dumps(slot, ensure_ascii=False), match["match_basis"],
                    json.dumps(availability, ensure_ascii=False), json.dumps(event_receipt, ensure_ascii=False),
                    json.dumps(notification_receipt, ensure_ascii=False) if notification_receipt else None,
                    now(),
                ),
            )
        return {
            "status": "booked",
            "confirmation_id": int(cursor.lastrowid),
            "calendar_event_created": True,
            "event_receipt": event_receipt,
            "operator_notification_sent": bool(notification_receipt and notification_receipt.get("status") == "sent"),
            "notification_receipt": notification_receipt or {
                "status": "not_configured",
                "reason": "No SMS, call, or Signal notification channel is configured",
            },
            "selected_slot": slot,
            "testing_authorized": False,
        }
    finally:
        store.close()
