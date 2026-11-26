"""Cyber-services sales, negotiation preparation and internal reinvestment math.

All commercial outputs are internal preparation until the operator approves an
exact customer-facing proposal. Models may organize evidence, not set authority.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tools.lab_capability_catalog import (
    build_capability_catalog,
    build_sow_code_review,
    build_testing_framework,
    customer_safe_view,
    review_sow,
)
from tools.messaging_store import ROOT, digest


def load_sales_skills(path: str | Path | None = None) -> dict[str, Any]:
    value = json.loads(Path(path or ROOT / "config/cyber_sales_skills.json").read_text(encoding="utf-8-sig"))
    if value.get("version") != 1 or not value.get("stages"):
        raise ValueError("Unsupported or empty cyber-sales skill registry")
    ids = [stage.get("id") for stage in value["stages"]]
    if any(not stage_id for stage_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Cyber-sales stages need unique IDs")
    return value


def load_commercial_policy(path: str | Path | None = None) -> dict[str, Any]:
    value = json.loads(Path(path or ROOT / "config/commercial_policy.json").read_text(encoding="utf-8-sig"))
    if value.get("version") != 1 or value.get("operator_approval_required") is not True:
        raise ValueError("Commercial policy must reserve approval for the operator")
    return value


DISCOVERY_FIELDS = {
    "business_goal": r"\b(soc ?2|compliance|assurance|risk|renewal|launch|customer requirement|objective|goal)\b",
    "risk_owner": r"\b(risk owner|ciso|cto|security lead|engineering lead|owner)\b",
    "success_criteria": r"\b(success|acceptance criteria|pass|assurance|validate|outcome)\b",
    "target_types": r"\b(domain|web application|website|api|cloud|network|mobile|source code|container)\b",
    "environment": r"\b(production|staging|development|test environment|sandbox)\b",
    "constraints": r"\b(exclusion|constraint|out.of.scope|do not|prohibited|rate limit|window)\b",
    "buyer_authority": r"\b(procurement|purchase order|budget owner|signatory|authorized representative)\b",
    "timing": r"\b(deadline|due|renewal|date|window|schedule|quarter)\b",
    "reporting": r"\b(report|readout|retest|cvss|executive summary|evidence)\b",
}


def build_discovery_brief(customer_text: str) -> dict[str, Any]:
    observed = {field: bool(re.search(pattern, customer_text or "", re.I)) for field, pattern in DISCOVERY_FIELDS.items()}
    missing = [field for field, present in observed.items() if not present]
    questions = {
        "business_goal": "What decision or assurance requirement should this assessment support?",
        "risk_owner": "Who owns the risk and should receive urgent findings?",
        "success_criteria": "What would make this assessment successful for your team?",
        "target_types": "Which domains, applications, APIs, cloud services, or repositories should be considered?",
        "environment": "Should planning assume staging, production, or both?",
        "constraints": "Are there critical exclusions, fragile components, rate limits, or prohibited techniques?",
        "buyer_authority": "Who will approve the scope, rules of engagement, and commercial terms?",
        "timing": "What business deadline or compliance milestone drives the schedule?",
        "reporting": "Who is the report for, and do you need a retest or executive readout?",
    }
    return {
        "brief_version": 1,
        "customer_text_sha256": digest(customer_text or ""),
        "observed_fields": observed,
        "missing_fields": missing,
        "suggested_questions": [questions[field] for field in missing],
        "questions_are_nonbinding": True,
    }


def build_negotiation_brief(customer_request: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or load_commercial_policy()
    unknowns = [
        field for field in (
            "reservation_price", "target_price", "maximum_discount_percent", "tax_reserve_percent",
            "contingency_percent", "gpu_reinvestment_percent", "gpu_target_cost", "podman_revenue_per_reserved_hour"
        ) if policy.get(field) is None
    ]
    customer_interests = []
    for label, pattern in (
        ("compliance_timing", r"\b(soc ?2|audit|compliance|renewal|deadline)\b"),
        ("risk_reduction", r"\b(risk|security|vulnerab|assurance)\b"),
        ("budget_control", r"\b(price|cost|budget|discount)\b"),
        ("delivery_speed", r"\b(urgent|soon|deadline|quick|turnaround)\b"),
        ("report_utility", r"\b(report|evidence|executive|retest)\b"),
    ):
        if re.search(pattern, customer_request or "", re.I):
            customer_interests.append(label)
    options = [
        "Adjust in-scope assets while preserving explicit exclusions and authorization.",
        "Trade delivery timing against resource intensity.",
        "Offer a focused retest or readout as a separately scoped option.",
        "Separate must-have assurance goals from optional coverage.",
        "Use an agreed change-control process for newly discovered assets."
    ]
    return {
        "brief_version": 1,
        "internal_only": True,
        "customer_request_sha256": digest(customer_request or ""),
        "interests": {"customer_observed": customer_interests, "provider": ["authorized work", "credible evidence", "sustainable delivery", "long-term relationship"]},
        "options": options,
        "objective_criteria": ["authorized asset count", "application roles and workflows", "environment criticality", "test types", "resource days", "reporting and retest requirements"],
        "batna": "Do not accept terms that fall below the operator-approved reservation point or weaken safety and authorization controls.",
        "relationship": "Prefer transparent scope trades and documented change control over surprise fees or unsupported promises.",
        "commitments": "No commitment is valid until the operator approves the exact price, scope, schedule and legal terms.",
        "communication": "Ask concise questions, summarize shared understanding, and label assumptions before presenting options.",
        "commercial_inputs_complete": not unknowns,
        "missing_commercial_inputs": unknowns,
        "operator_approval_required": True,
    }


def validate_commercial_move(move: dict[str, Any], policy: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = policy or load_commercial_policy()
    if not isinstance(move, dict):
        raise ValueError("Commercial move must be an object")
    for field in ("price", "discount_percent", "delivery_date", "payment_terms", "legal_terms"):
        if move.get(field) is not None and not move.get("operator_approval_id"):
            raise ValueError(field + " requires operator approval")
    if move.get("discount_percent") is not None:
        maximum = policy.get("maximum_discount_percent")
        if maximum is None:
            raise ValueError("No discount authority is configured")
        if float(move["discount_percent"]) > float(maximum):
            raise ValueError("Discount exceeds operator policy")
    if any(move.get(key) is False for key in ("written_authorization", "explicit_scope", "data_handling", "evidence_quality")):
        raise ValueError("Safety, authorization and evidence controls are nonnegotiable")
    text = json.dumps(move, ensure_ascii=False)
    if re.search(r"\b(podman|gpu|model cost|token cost|margin|markup|internal cost)\b", text, re.I):
        raise ValueError("Commercial move exposes internal economics")
    return move


@dataclass(frozen=True)
class EngagementEconomics:
    customer_price: float
    direct_internal_cost: float
    tax_reserve_percent: float
    contingency_percent: float
    gpu_reinvestment_percent: float
    gpu_target_cost: float
    gpu_fund_current_balance: float = 0.0


def calculate_gpu_reinvestment(value: EngagementEconomics) -> dict[str, Any]:
    numbers = asdict(value)
    if any(float(number) < 0 for number in numbers.values()):
        raise ValueError("Commercial inputs cannot be negative")
    for field in ("tax_reserve_percent", "contingency_percent", "gpu_reinvestment_percent"):
        if not 0 <= float(numbers[field]) <= 100:
            raise ValueError(field + " must be between 0 and 100")
    gross_profit = max(0.0, value.customer_price - value.direct_internal_cost)
    tax_reserve = gross_profit * value.tax_reserve_percent / 100
    contingency = gross_profit * value.contingency_percent / 100
    distributable = max(0.0, gross_profit - tax_reserve - contingency)
    remaining_gpu_target = max(0.0, value.gpu_target_cost - value.gpu_fund_current_balance)
    gpu_reinvestment = min(remaining_gpu_target, distributable * value.gpu_reinvestment_percent / 100)
    retained = distributable - gpu_reinvestment
    return {
        "internal_only": True,
        "complete": True,
        "customer_price": value.customer_price,
        "direct_internal_cost": value.direct_internal_cost,
        "gross_profit": gross_profit,
        "tax_reserve": tax_reserve,
        "contingency": contingency,
        "distributable": distributable,
        "gpu_reinvestment": gpu_reinvestment,
        "gpu_fund_after": value.gpu_fund_current_balance + gpu_reinvestment,
        "retained_after_reinvestment": retained,
    }


def map_expectations_to_lab(customer_text: str, root: str | Path = ROOT) -> dict[str, Any]:
    catalog = build_capability_catalog(root)
    sow = review_sow(customer_text, catalog)
    code_review = build_sow_code_review(customer_text, root=root, catalog=catalog)
    framework = build_testing_framework(customer_text, catalog)
    return {
        "mapping_version": 1,
        "discovery": build_discovery_brief(customer_text),
        "sow_review": sow,
        "code_review": code_review,
        "testing_framework": framework,
        "customer_view": customer_safe_view({
            "requested_test_categories": sow["requested_test_categories"],
            "missing_scope_information": sow["missing_fields"],
            "planned_coverage": framework["coverage"],
            "assumptions": ["Final coverage depends on signed scope and rules of engagement."],
        }),
    }
