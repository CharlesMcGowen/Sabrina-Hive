"""Internal sales and negotiation packet for an allowlisted customer thread."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.customer_contract_review import build_contract_issue_review
from tools.cyber_sales import build_discovery_brief, build_negotiation_brief, load_sales_skills
from tools.engagement_economics import economics_readiness
from tools.messaging_store import ConversationStore, ROOT


DEFAULT_OUTPUT_ROOT = ROOT / "artifacts/customer_engagement/commercial_reviews"
COMMERCIAL_CATEGORIES = {"price_or_discount", "contract_or_legal", "delivery_commitment"}


def _latest_external_text(outcome: dict[str, Any], messaging_db: str | Path | None = None) -> str:
    if isinstance(outcome.get("customer_text"), str):
        return outcome["customer_text"]
    account = outcome.get("account_id")
    thread = outcome.get("thread_id")
    if not account or not thread:
        raise ValueError("Commercial handoff is missing conversation identity")
    store = ConversationStore(messaging_db)
    try:
        snapshot = store.snapshot(account, thread)
    finally:
        store.close()
    for message in reversed(snapshot["messages"]):
        if message.get("ownership") == "external":
            return message.get("text", "")
    raise ValueError("No external customer request was found")


def build_commercial_packet(
    outcome: dict[str, Any],
    *,
    messaging_db: str | Path | None = None,
) -> dict[str, Any]:
    decision = outcome.get("decision") or {}
    category = decision.get("category") or "uncertain_intent"
    if category not in COMMERCIAL_CATEGORIES:
        raise ValueError(f"No commercial packet is defined for {category}")
    customer_text = _latest_external_text(outcome, messaging_db)
    skills = load_sales_skills()
    stage_ids = {
        "price_or_discount": ["discovery", "qualification", "proposal", "negotiation"],
        "contract_or_legal": ["qualification", "proposal", "contracting"],
        "delivery_commitment": ["discovery", "qualification", "proposal"],
    }[category]
    stages = [stage for stage in skills["stages"] if stage["id"] in stage_ids]
    negotiation = build_negotiation_brief(customer_text)
    contract_review = build_contract_issue_review(customer_text) if category == "contract_or_legal" else None
    return {
        "version": 1,
        "id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "contact_id": outcome.get("contact_id"),
        "account_id": outcome.get("account_id"),
        "thread_id": outcome.get("thread_id"),
        "category": category,
        "internal_only": True,
        "discovery": build_discovery_brief(customer_text),
        "negotiation": negotiation,
        "contract_review": contract_review,
        "economics_readiness": economics_readiness(),
        "applicable_stages": stages,
        "operator_approval_template": {
            "operator_approval_id": None,
            "approved_scope": None,
            "approved_price": None,
            "approved_discount_percent": None,
            "approved_delivery_date": None,
            "approved_payment_terms": None,
            "approved_legal_terms": None,
        },
        "legal_review_required": category == "contract_or_legal",
        "customer_reply_authorized": False,
        "next_gate": (
            "qualified legal review and operator approval of the exact customer-facing move"
            if category == "contract_or_legal"
            else "operator supplies missing commercial inputs and approves the exact customer-facing move"
        ),
    }


def write_commercial_packet(
    outcome: dict[str, Any],
    *,
    messaging_db: str | Path | None = None,
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    packet = build_commercial_packet(outcome, messaging_db=messaging_db)
    root = Path(output_root or DEFAULT_OUTPUT_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    stamp = packet["created_at"].replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    path = root / f"{stamp}_{packet['id']}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return {**packet, "path": str(path.resolve())}
