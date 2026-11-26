"""Bounded, exactly-once Gmail follow-ups for an unanswered first-customer thread."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from tools.customer_engagement import load_customer_policy, validate_natural_style
from tools.gmail_message_sender import GmailMessageSender
from tools.messaging_store import ConversationStore, digest, now


def _as_utc(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        raise ValueError("Follow-up timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def add_business_days(value: str | datetime, days: int, timezone_name: str) -> datetime:
    if int(days) <= 0:
        raise ValueError("Follow-up business-day delay must be positive")
    local = _as_utc(value).astimezone(ZoneInfo(timezone_name))
    added = 0
    while added < int(days):
        local += timedelta(days=1)
        if local.weekday() < 5:
            added += 1
    return local.astimezone(timezone.utc)


def _gmail_message_time(
    message: dict[str, Any],
    timezone_name: str,
    reference_utc: datetime,
) -> datetime | None:
    raw = str(message.get("timestamp_text") or "").replace("\u202f", " ").replace("\xa0", " ").strip()
    raw = re.sub(r"\s+", " ", raw)
    zone = ZoneInfo(timezone_name)
    formats = (
        "%b %d, %Y, %I:%M %p",
        "%B %d, %Y, %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
    )
    for pattern in formats:
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=zone).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        parsed_time = datetime.strptime(raw, "%I:%M %p").time()
    except ValueError:
        return None
    local_reference = reference_utc.astimezone(zone)
    candidate = datetime.combine(local_reference.date(), parsed_time, tzinfo=zone)
    if candidate > local_reference + timedelta(minutes=5):
        candidate -= timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class CustomerFollowupOutbox:
    def __init__(
        self,
        store: ConversationStore,
        policy: dict[str, Any] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.db = store.db
        self.policy = policy or load_customer_policy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS customer_followups(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              action_key TEXT NOT NULL UNIQUE,
              contact_id TEXT NOT NULL,
              account TEXT NOT NULL,
              thread TEXT NOT NULL,
              step INTEGER NOT NULL,
              context_version INTEGER NOT NULL,
              baseline_last_message_id TEXT NOT NULL,
              not_before TEXT NOT NULL,
              text TEXT NOT NULL,
              authorization TEXT NOT NULL,
              status TEXT NOT NULL,
              receipt TEXT,
              last_error TEXT,
              created TEXT NOT NULL,
              updated TEXT NOT NULL,
              UNIQUE(account,thread,step)
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS customer_followup_campaigns(
              account TEXT NOT NULL,
              thread TEXT NOT NULL,
              contact_id TEXT NOT NULL,
              initial_message_id TEXT NOT NULL,
              anchor_time_utc TEXT NOT NULL,
              anchor_source TEXT NOT NULL,
              created TEXT NOT NULL,
              PRIMARY KEY(account,thread)
            )"""
        )
        self.db.commit()

    def _settings(self) -> dict[str, Any]:
        value = self.policy.get("followup_policy") or {}
        waits = value.get("wait_business_days")
        templates = value.get("templates")
        maximum = int(value.get("max_followups", 0))
        if not isinstance(waits, list) or not isinstance(templates, list):
            raise ValueError("Follow-up waits and templates must be lists")
        if maximum < 0 or maximum > len(waits) or maximum > len(templates):
            raise ValueError("Follow-up maximum exceeds the configured waits or templates")
        if maximum and any(int(days) <= 0 for days in waits[:maximum]):
            raise ValueError("Follow-up waits must be positive business-day counts")
        return {
            **value,
            "max_followups": maximum,
            "wait_business_days": [int(days) for days in waits],
            "templates": templates,
            "business_timezone": value.get("business_timezone", "America/Chicago"),
        }

    def get(self, followup_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM customer_followups WHERE id=?", (int(followup_id),)).fetchone()
        if row is None:
            raise KeyError(followup_id)
        return dict(row)

    def _message_observed(self, snapshot: dict[str, Any], message_id: str) -> datetime:
        row = self.db.execute(
            "SELECT observed FROM messages WHERE account=? AND thread=? AND id=?",
            (snapshot["account_id"], snapshot["thread_id"], message_id),
        ).fetchone()
        if row is None:
            raise ValueError("Follow-up anchor message is not in the durable conversation store")
        return _as_utc(row["observed"])

    def _campaign_anchor(self, snapshot: dict[str, Any], current: datetime) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM customer_followup_campaigns WHERE account=? AND thread=?",
            (snapshot["account_id"], snapshot["thread_id"]),
        ).fetchone()
        if row is not None:
            return dict(row)
        settings = self._settings()
        initial = snapshot["messages"][0]
        parsed = _gmail_message_time(initial, settings["business_timezone"], current)
        if parsed is None:
            parsed = self._message_observed(snapshot, initial["id"])
            source = "first_durable_observation"
        else:
            source = "gmail_timestamp_text"
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO customer_followup_campaigns(
                  account,thread,contact_id,initial_message_id,anchor_time_utc,anchor_source,created
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    snapshot["account_id"],
                    snapshot["thread_id"],
                    snapshot["contact_id"],
                    initial["id"],
                    parsed.isoformat(),
                    source,
                    now(),
                ),
            )
        return dict(self.db.execute(
            "SELECT * FROM customer_followup_campaigns WHERE account=? AND thread=?",
            (snapshot["account_id"], snapshot["thread_id"]),
        ).fetchone())

    def plan(self, snapshot: dict[str, Any], *, now_utc: datetime | None = None) -> dict[str, Any]:
        settings = self._settings()
        current = _as_utc(now_utc or self.clock())
        base = {
            "internal_only": True,
            "enabled": bool(settings.get("enabled")),
            "max_followups": settings["max_followups"],
            "customer_channel": "gmail",
            "linkedin_allowed": False,
        }
        if not settings.get("enabled") or settings["max_followups"] == 0:
            return {**base, "status": "disabled", "send_allowed": False}
        if not snapshot.get("complete") or not snapshot.get("messages"):
            return {**base, "status": "incomplete_thread", "send_allowed": False}
        if any(message.get("ownership") == "external" for message in snapshot["messages"]):
            return {**base, "status": "customer_engaged", "send_allowed": False}
        if snapshot["messages"][-1].get("ownership") != "self":
            return {**base, "status": "customer_engaged", "send_allowed": False}
        pending = self.db.execute(
            """SELECT * FROM customer_followups
               WHERE account=? AND thread=? AND status IN ('authorized','sending','uncertain')
               ORDER BY id DESC LIMIT 1""",
            (snapshot["account_id"], snapshot["thread_id"]),
        ).fetchone()
        if pending is not None:
            value = dict(pending)
            if value["status"] in {"sending", "uncertain"}:
                return {
                    **base,
                    "status": "attention_required",
                    "send_allowed": False,
                    "step": int(value["step"]),
                    "outbox_id": int(value["id"]),
                }
            due = current >= _as_utc(value["not_before"])
            return {
                **base,
                "status": "due" if due else "not_due",
                "send_allowed": due,
                "step": int(value["step"]),
                "outbox_id": int(value["id"]),
                "not_before_utc": _as_utc(value["not_before"]).isoformat(),
            }
        sent_count = int(self.db.execute(
            "SELECT COUNT(*) AS count FROM customer_followups WHERE account=? AND thread=? AND status='sent'",
            (snapshot["account_id"], snapshot["thread_id"]),
        ).fetchone()["count"])
        if sent_count >= settings["max_followups"]:
            return {**base, "status": "exhausted", "send_allowed": False, "sent_count": sent_count}
        if sent_count == 0 and len(snapshot["messages"]) > 1:
            return {
                **base,
                "status": "manual_outbound_detected",
                "send_allowed": False,
                "sent_count": sent_count,
            }
        step = sent_count + 1
        latest = snapshot["messages"][-1]
        if sent_count == 0:
            campaign = self._campaign_anchor(snapshot, current)
            anchor = _as_utc(campaign["anchor_time_utc"])
            anchor_source = campaign["anchor_source"]
        else:
            prior = self.db.execute(
                """SELECT updated FROM customer_followups
                   WHERE account=? AND thread=? AND status='sent' ORDER BY step DESC LIMIT 1""",
                (snapshot["account_id"], snapshot["thread_id"]),
            ).fetchone()
            if prior is None:
                raise ValueError("Sent follow-up count has no delivery record")
            anchor = _as_utc(prior["updated"])
            anchor_source = "verified_followup_delivery"
        not_before = add_business_days(
            anchor,
            settings["wait_business_days"][step - 1],
            settings["business_timezone"],
        )
        due = current >= not_before
        return {
            **base,
            "status": "due" if due else "not_due",
            "send_allowed": due,
            "step": step,
            "sent_count": sent_count,
            "anchor_message_id": latest["id"],
            "anchor_source": anchor_source,
            "not_before_utc": not_before.isoformat(),
            "business_timezone": settings["business_timezone"],
        }

    def queue_due(self, snapshot: dict[str, Any], *, now_utc: datetime | None = None) -> dict[str, Any]:
        plan = self.plan(snapshot, now_utc=now_utc)
        if plan.get("status") != "due" or not plan.get("send_allowed"):
            raise ValueError("Customer follow-up is not due")
        if plan.get("outbox_id"):
            return self.get(plan["outbox_id"])
        settings = self._settings()
        step = int(plan["step"])
        text = validate_natural_style(settings["templates"][step - 1], self.policy)
        action_key = digest([
            snapshot["account_id"],
            snapshot["thread_id"],
            snapshot["version"],
            snapshot["messages"][-1]["id"],
            step,
            text,
        ])
        timestamp = now()
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO customer_followups(
                  action_key,contact_id,account,thread,step,context_version,baseline_last_message_id,
                  not_before,text,authorization,status,created,updated
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'authorized',?,?)""",
                (
                    action_key,
                    snapshot["contact_id"],
                    snapshot["account_id"],
                    snapshot["thread_id"],
                    step,
                    int(snapshot["version"]),
                    snapshot["messages"][-1]["id"],
                    plan["not_before_utc"],
                    text,
                    "Standing follow-up authorization: " + str(settings.get("source", "")).strip(),
                    timestamp,
                    timestamp,
                ),
            )
        row = self.db.execute(
            "SELECT id FROM customer_followups WHERE action_key=?", (action_key,)
        ).fetchone()
        return self.get(row["id"])

    def claim(self, followup_id: int, expected: dict[str, Any]) -> dict[str, Any]:
        with self.db:
            row = self.get(followup_id)
            if row["status"] != "authorized":
                raise ValueError("Follow-up was already claimed, sent, or made uncertain")
            if row["account"] != expected["account_id"] or row["thread"] != expected["thread_id"]:
                raise ValueError("Follow-up account or thread changed")
            current = self.store.snapshot(row["account"], row["thread"])
            if current["version"] != row["context_version"] or expected["version"] != row["context_version"]:
                raise ValueError("Follow-up context changed")
            if not current.get("complete") or any(
                message.get("ownership") == "external" for message in current["messages"]
            ):
                raise ValueError("Customer engaged before the follow-up was sent")
            if current["messages"][-1]["id"] != row["baseline_last_message_id"]:
                raise ValueError("A newer owner message superseded the follow-up")
            if _as_utc(self.clock()) < _as_utc(row["not_before"]):
                raise ValueError("Follow-up minimum wait has not elapsed")
            pending = self.db.execute(
                """SELECT 1 FROM customer_followups
                   WHERE account=? AND thread=? AND id!=? AND status IN ('sending','uncertain')""",
                (row["account"], row["thread"], int(followup_id)),
            ).fetchone()
            if pending:
                raise ValueError("A prior follow-up send is unresolved")
            changed = self.db.execute(
                "UPDATE customer_followups SET status='sending',updated=? WHERE id=? AND status='authorized'",
                (now(), int(followup_id)),
            ).rowcount
            if changed != 1:
                raise ValueError("Follow-up send claim was lost")
        return row

    def finish(self, followup_id: int, receipt: dict[str, Any]) -> None:
        if receipt.get("status") not in {"sent", "uncertain"}:
            raise ValueError("Invalid follow-up delivery receipt")
        row = self.get(followup_id)
        if row["status"] != "sending":
            raise ValueError("Follow-up has no active send claim")
        if receipt["status"] == "sent" and (
            not receipt.get("message_id")
            or receipt.get("account_id") != row["account"]
            or receipt.get("thread_id") != row["thread"]
            or receipt.get("text") != row["text"]
        ):
            raise ValueError("Follow-up delivery evidence does not match the authorized message")
        with self.db:
            self.db.execute(
                "UPDATE customer_followups SET status=?,receipt=?,updated=? WHERE id=? AND status='sending'",
                (receipt["status"], json.dumps(receipt, ensure_ascii=False), now(), int(followup_id)),
            )

    def release_before_send(self, followup_id: int, error: str) -> None:
        with self.db:
            changed = self.db.execute(
                """UPDATE customer_followups SET status='authorized',last_error=?,updated=?
                   WHERE id=? AND status='sending'""",
                (str(error), now(), int(followup_id)),
            ).rowcount
            if changed != 1:
                raise ValueError("Follow-up pre-send claim could not be released")


def maybe_send_due_followup(
    reader: Any,
    store: ConversationStore,
    snapshot: dict[str, Any],
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outbox = CustomerFollowupOutbox(store, policy)
    plan = outbox.plan(snapshot)
    if plan["status"] == "attention_required":
        return {"status": "uncertain", "sent": False, "followup": plan}
    if plan["status"] != "due":
        return {"status": plan["status"], "sent": False, "followup": plan}
    queued = outbox.queue_due(snapshot)
    try:
        receipt = GmailMessageSender(reader, store, outbox).send(queued["id"])
    except ValueError as error:
        if any(marker in str(error) for marker in (
            "Follow-up context changed",
            "Customer engaged before the follow-up was sent",
            "A newer owner message superseded the follow-up",
        )):
            return {
                "status": "superseded",
                "sent": False,
                "followup": {**plan, "outbox_id": queued["id"], "reason": str(error)},
            }
        raise
    return {
        "status": receipt["status"],
        "sent": receipt["status"] == "sent",
        "receipt": receipt,
        "followup": {**plan, "outbox_id": queued["id"]},
    }
