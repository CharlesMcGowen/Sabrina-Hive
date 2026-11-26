"""Truthful operator handoffs for customer events requiring human authority."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tools.customer_engagement import load_customer_policy
from tools.messaging_store import ROOT


DEFAULT_OUTPUT_ROOT = ROOT / "artifacts/customer_engagement/operator_actions"


def _as_utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("Operator handoff time must be timezone-aware")
    return value.astimezone(timezone.utc)


def build_operator_handoff(
    outcome: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    policy = policy or load_customer_policy()
    created_utc = _as_utc(created_at)
    decision = outcome.get("decision") or {}
    category = decision.get("category") or "uncertain_intent"
    channels = policy.get("operator_channels", {})
    timezone_name = channels.get("timezone", "America/Chicago")
    local_zone = ZoneInfo(timezone_name)
    handoff = {
        "version": 1,
        "id": str(uuid.uuid4()),
        "created_at": created_utc.isoformat(),
        "category": category,
        "reason": decision.get("reason") or outcome.get("error") or "Operator review is required.",
        "next_action": decision.get("next_action") or "operator_reviews_customer_request",
        "contact_id": outcome.get("contact_id"),
        "account_id": outcome.get("account_id"),
        "thread_id": outcome.get("thread_id"),
        "customer_message_sent": bool(outcome.get("sent")),
        "operator_notification": {
            "requested": bool(policy.get("meeting_policy", {}).get("notify_operator", True)),
            "sent": False,
            "channel": None,
            "reason": "No connected operator notification channel is configured.",
        },
        "calendar": {
            "provider": channels.get("calendar_provider"),
            "connected": bool(channels.get("calendar_connected")),
            "event_created": False,
            "event_id": None,
        },
        "sms": {
            "provider": channels.get("sms_provider"),
            "connected": bool(channels.get("sms_connected")),
            "sent": False,
            "message_id": None,
        },
    }
    if category == "call_or_meeting":
        minimum_days = int(policy["meeting_policy"]["minimum_notice_days"])
        not_before_utc = created_utc + timedelta(days=minimum_days)
        handoff["meeting"] = {
            "minimum_notice_days": minimum_days,
            "not_before_utc": not_before_utc.isoformat(),
            "not_before_local": not_before_utc.astimezone(local_zone).isoformat(),
            "timezone": timezone_name,
            "auto_book_allowed": bool(policy["meeting_policy"].get("auto_book", False)),
            "availability_checked": False,
            "customer_times_proposed": False,
        }
        if not handoff["calendar"]["connected"]:
            handoff["calendar"]["reason"] = "Google Calendar is not connected. No availability was checked and no event was created."
        if not handoff["sms"]["connected"]:
            handoff["sms"]["reason"] = "No SMS provider, authentication token, sending number, and operator destination number are configured."
    return handoff


def write_operator_handoff(
    outcome: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    value = build_operator_handoff(outcome, policy=policy, created_at=created_at)
    destination = Path(output_root or DEFAULT_OUTPUT_ROOT)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{value['created_at'].replace(':', '').replace('+0000', 'Z').replace('+00:00', 'Z')}_{value['id']}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return {**value, "path": str(path.resolve())}
