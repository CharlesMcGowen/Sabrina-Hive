"""Translate an SOW and lab snapshot into separate internal and customer safe views.

The customer view is planning language only. It never exposes source paths, hashes, engine names,
models, compute economics, or customer targets, and it never authorizes execution or sending.
"""
from __future__ import annotations

import json
from typing import Any


CATEGORY_LANGUAGE = {
    "web_application": {
        "label": "web application assessment",
        "approach": "Map application roles, workflows, inputs, trust boundaries, configuration, and business logic using bounded nondestructive checks.",
        "acceptance": [
            "Each in scope application and user role is represented in the coverage matrix.",
            "Each reported issue includes reproducible evidence, impact, limitations, and remediation guidance.",
        ],
    },
    "api": {
        "label": "API assessment",
        "approach": "Build an authorized endpoint inventory and assess schema handling, object access, function access, and rate controls without load testing.",
        "acceptance": [
            "The final coverage record identifies the assessed endpoints, roles, and excluded operations.",
            "Authorization observations distinguish expected denial from unintended cross account or cross role access.",
        ],
    },
    "authentication": {
        "label": "authentication review",
        "approach": "Review enrollment, login, recovery, multifactor controls, session creation, rotation, and invalidation within supplied test identities.",
        "acceptance": [
            "The assessment covers the agreed identity lifecycle and session states.",
            "No account lockout, recovery, or identity change is attempted outside the approved test identities.",
        ],
    },
    "authorization": {
        "label": "authorization review",
        "approach": "Assess horizontal and vertical access boundaries with approved test identities and controlled object ownership changes.",
        "acceptance": [
            "The test matrix identifies the roles, object relationships, and expected access decisions.",
            "Any access control finding includes the minimum evidence needed to reproduce the boundary failure safely.",
        ],
    },
    "cloud_or_container": {
        "label": "cloud and container configuration review",
        "approach": "Review supplied configuration, images, dependencies, secrets handling, and network boundaries without changing customer infrastructure.",
        "acceptance": [
            "Reviewed artifacts and excluded environments are recorded in the final coverage statement.",
            "Configuration observations are separated from findings that were directly validated.",
        ],
    },
    "source_review": {
        "label": "source code review",
        "approach": "Review supplied source for trust boundaries, authorization decisions, unsafe data flows, dependency exposure, and embedded secrets.",
        "acceptance": [
            "The agreed repositories, revisions, and excluded components are recorded before review begins.",
            "Findings identify the affected component, evidence, risk, and recommended correction without reproducing customer secrets.",
        ],
    },
    "network": {
        "label": "network security assessment",
        "approach": "Build a bounded inventory of approved hosts, ports, services, TLS configuration, and supplied network boundaries without denial of service or out of scope discovery.",
        "acceptance": [
            "The final coverage record identifies the approved address ranges, hosts, ports, exclusions, and scan-rate restrictions.",
            "Each observation distinguishes directly validated service evidence from customer-supplied architecture or segmentation evidence.",
        ],
    },
}

MISSING_FIELD_QUESTIONS = {
    "in_scope_assets": "Which domains, applications, APIs, repositories, and environments are explicitly in scope?",
    "out_of_scope_assets": "Which assets, tenants, paths, data, and supporting services must not be tested?",
    "written_authorization": "Who will provide written authorization for the named assets and assessment window?",
    "test_window": "What dates, hours, time zone, and change restrictions apply to the assessment?",
    "allowed_and_prohibited_methods": "Which methods are allowed, and which methods such as load testing or social engineering are prohibited?",
    "customer_contacts": "Who are the primary and emergency contacts during the assessment?",
    "data_handling": "What evidence retention, confidentiality, storage, and deletion requirements apply?",
    "deliverables": "Which report, readout, remediation support, and retest deliverables are expected?",
}

FORBIDDEN_CUSTOMER_TERMS = (
    "sha256",
    "tools/",
    "tests/",
    ".py",
    "gpt",
    "model",
    "token",
    "gpu",
    "podman",
    "runpod",
    "margin",
    "internal cost",
)


def _category_cases(test_matrix: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [case for case in test_matrix.get("cases", []) if case.get("category") == category]


def _category_internal_status(test_matrix: dict[str, Any], category: str) -> dict[str, Any]:
    cases = _category_cases(test_matrix, category)
    names = sorted({name for case in cases for name in case.get("local_candidates", [])})
    proofs = test_matrix.get("candidate_proofs", {})
    unproven = [name for name in names if (proofs.get(name) or {}).get("proof_status") != "verified_isolated_fixture"]
    return {
        "case_ids": [case.get("id") for case in cases],
        "candidate_names": names,
        "candidate_proofs": {name: proofs.get(name) for name in names},
        "unproven_candidates": unproven,
        "resource_discovery_required": bool(unproven),
    }


def verify_customer_expectation_packet(packet: dict[str, Any]) -> dict[str, Any]:
    failures = []
    serialized = json.dumps(packet, ensure_ascii=False).lower()
    for term in FORBIDDEN_CUSTOMER_TERMS:
        if term in serialized:
            failures.append(f"customer packet contains forbidden internal term: {term}")
    if packet.get("approved_for_send") is not False:
        failures.append("customer packet is approved for send")
    if packet.get("testing_authorized") is not False:
        failures.append("customer packet authorizes testing")
    if packet.get("contains_customer_targets") is not False:
        failures.append("customer packet contains customer targets")
    if "\u2013" in serialized or "\u2014" in serialized:
        failures.append("customer packet contains a prohibited dash character")
    return {"verified": not failures, "failures": failures}


def build_expectation_bridge(
    framework: dict[str, Any],
    code_review: dict[str, Any],
    test_matrix: dict[str, Any],
) -> dict[str, Any]:
    review = framework["review"]
    categories = review.get("requested_test_categories", [])
    customer_coverage = []
    internal_coverage = {}
    for category in categories:
        language = CATEGORY_LANGUAGE.get(category)
        if language is None:
            continue
        internal_status = _category_internal_status(test_matrix, category)
        internal_coverage[category] = internal_status
        has_candidates = bool(internal_status["candidate_names"])
        has_unproven = bool(internal_status["unproven_candidates"])
        customer_coverage.append({
            "category": category,
            "label": language["label"],
            "approach": language["approach"],
            "planning_status": (
                "capability gap requires resolution"
                if has_unproven else
                "candidate methods identified and isolated validation required"
                if has_candidates else
                "manual or review led method requires validation"
            ),
            "acceptance_criteria": language["acceptance"],
        })

    questions = [
        MISSING_FIELD_QUESTIONS[field]
        for field in review.get("missing_fields", [])
        if field in MISSING_FIELD_QUESTIONS
    ]
    customer_packet = {
        "version": 1,
        "purpose": "customer expectation alignment draft",
        "coverage": customer_coverage,
        "open_questions": questions,
        "summary_paragraphs": [
            "Thank you for sharing the statement of work. We mapped the requested coverage to our current assessment methods and prepared a planning framework.",
            "Each method must pass isolated validation before it can be included in a customer test plan. Customer testing will begin only after the final scope, rules of engagement, test window, emergency contacts, and written authorization are verified and separately approved.",
            "The final proposal will state the agreed coverage, evidence standard, limitations, deliverables, and retest expectations without making assumptions about missing scope details.",
        ],
        "limitations": [
            "Planning evidence does not prove a result against a customer system.",
            "Coverage may change when the final asset list, identities, environment constraints, and prohibited methods are confirmed.",
            "No price, schedule, legal term, or delivery commitment is included in this draft.",
        ],
        "contains_customer_targets": False,
        "testing_authorized": False,
        "approved_for_send": False,
        "next_gate": "operator reviews the exact customer language and resolves open scope questions",
    }
    verification = verify_customer_expectation_packet(customer_packet)
    if not verification["verified"]:
        raise ValueError("Customer expectation packet failed safety verification: " + "; ".join(verification["failures"]))
    customer_packet["verification"] = verification

    internal_packet = {
        "version": 1,
        "catalog_snapshot_sha256": framework.get("catalog_snapshot_sha256"),
        "source_files_reviewed": code_review.get("reviewed_source_file_count"),
        "snapshot_consistent": code_review.get("catalog_snapshot_consistent", False),
        "category_coverage": internal_coverage,
        "coverage_gaps": code_review.get("coverage_gaps", []),
        "method_references": code_review.get("method_references", []),
        "resource_plan": {
            "reuse_local_candidates_first": True,
            "external_tool_search_required": any(
                value["resource_discovery_required"] for value in internal_coverage.values()
            ),
            "external_tools_must_be_evaluated_in_isolation": True,
            "customer_execution_allowed": False,
        },
        "customer_packet_verified": True,
        "customer_reply_authorized": False,
        "next_gate": "run isolated capability proofs and obtain operator approval before using customer language",
    }
    return {"customer": customer_packet, "internal": internal_packet}
