"""Build a bounded, target free research plan for SOW capability gaps."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from tools.messaging_store import ROOT


REGISTRY_PATH = ROOT / "config/customer_resource_registry.json"


def load_resource_registry(path: str | Path | None = None) -> dict[str, Any]:
    value = json.loads(Path(path or REGISTRY_PATH).read_text(encoding="utf-8-sig"))
    if value.get("version") != 1:
        raise ValueError("Unsupported customer resource registry")
    allowed_hosts = set(value.get("allowed_hosts", []))
    sources = value.get("sources", [])
    ids = [source.get("id") for source in sources]
    if not ids or any(not source_id for source_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Resource source IDs must be unique and nonempty")
    for source in sources:
        parsed = urlparse(source.get("url", ""))
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
            raise ValueError("Resource source is outside the official host allowlist")
        kind = source.get("kind", "guidance")
        if kind not in {"guidance", "tool", "advisory"}:
            raise ValueError("Resource source kind is unsupported")
        if kind == "tool":
            if not source.get("candidate_id") or source.get("candidate_only") is not True:
                raise ValueError("Tool resource must remain a named candidate")
            if source.get("automatic_install_allowed") is not False:
                raise ValueError("Tool resource cannot allow automatic installation")
            controls = source.get("required_controls")
            if not isinstance(controls, list) or not controls or len(controls) != len(set(controls)):
                raise ValueError("Tool resource requires unique adoption controls")
    references = {source_id for values in value.get("category_sources", {}).values() for source_id in values}
    if not references.issubset(set(ids)):
        raise ValueError("Category mapping references an unknown resource source")
    gates = value.get("adoption_gates", [])
    if not gates or len(gates) != len(set(gates)):
        raise ValueError("Adoption gates must be unique and nonempty")
    return value


def _gap_for_category(category: str, code_review: dict[str, Any], test_matrix: dict[str, Any]) -> dict[str, Any] | None:
    cases = [case for case in test_matrix.get("cases", []) if case.get("category") == category]
    candidate_names = sorted({name for case in cases for name in case.get("local_candidates", [])})
    missing_candidates = sorted({name for case in cases for name in case.get("missing_candidates", [])})
    proofs = test_matrix.get("candidate_proofs", {})
    unproven = [
        name for name in candidate_names
        if (proofs.get(name) or {}).get("proof_status") != "verified_isolated_fixture"
    ]
    reasons = []
    if category in code_review.get("coverage_gaps", []):
        reasons.append("no relevant source evidence found in the snapshot")
    if missing_candidates:
        reasons.append("planned local candidate is absent")
    if unproven:
        reasons.append("local candidate lacks a current safety and functional isolated-fixture receipt")
    if not reasons:
        return None
    return {
        "category": category,
        "reasons": reasons,
        "local_candidates_considered": candidate_names,
        "missing_candidates": missing_candidates,
        "unproven_candidates": unproven,
        "case_ids": [case.get("id") for case in cases],
    }


def verify_resource_research_plan(plan: dict[str, Any], registry: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = registry or load_resource_registry()
    failures = []
    policy = registry["research_policy"]
    source_by_id = {source["id"]: source for source in registry["sources"]}
    allowed_hosts = set(registry["allowed_hosts"])
    if plan.get("customer_text_included") is not False or plan.get("customer_targets_included") is not False:
        failures.append("customer data is present")
    if plan.get("external_fetch_performed") is not False:
        failures.append("the planning step performed an external fetch")
    if plan.get("candidate_download_performed") is not False:
        failures.append("the planning step downloaded a candidate")
    if plan.get("customer_scheduler_web_access") is not False:
        failures.append("customer scheduler has web research access")
    for task in plan.get("tasks", []):
        if len(task.get("source_ids", [])) > int(policy["max_sources_per_gap"]):
            failures.append("research task exceeds the source cap")
        for source_id in task.get("source_ids", []):
            source = source_by_id.get(source_id)
            if source is None:
                failures.append("research task references an unknown source")
                continue
            parsed = urlparse(source["url"])
            if parsed.scheme != "https" or parsed.hostname not in allowed_hosts:
                failures.append("research task references a nonallowlisted source")
        for candidate in task.get("candidate_tools", []):
            source = source_by_id.get(candidate.get("source_id"))
            if (
                source is None
                or source.get("kind") != "tool"
                or source.get("candidate_only") is not True
                or source.get("automatic_install_allowed") is not False
            ):
                failures.append("research task includes an unbounded tool candidate")
        gates = task.get("adoption_gate", {})
        if set(gates) != set(registry["adoption_gates"]) or any(value is not False for value in gates.values()):
            failures.append("candidate adoption gate is not fail closed")
    return {"verified": not failures, "failures": failures}


def build_resource_research_plan(
    framework: dict[str, Any],
    code_review: dict[str, Any],
    test_matrix: dict[str, Any],
    catalog: dict[str, Any],
    *,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    registry = registry or load_resource_registry()
    policy = registry["research_policy"]
    source_by_id = {source["id"]: source for source in registry["sources"]}
    tasks = []
    for category in framework["review"].get("requested_test_categories", []):
        gap = _gap_for_category(category, code_review, test_matrix)
        if gap is None:
            continue
        source_ids = registry.get("category_sources", {}).get(category, ["nist_sp_800_115"])
        source_ids = source_ids[: int(policy["max_sources_per_gap"])]
        selected_sources = [source_by_id[source_id] for source_id in source_ids]
        tasks.append({
            "id": f"research_{category}",
            **gap,
            "research_question": (
                f"Identify maintained methods or tools for the {category.replace('_', ' ')} gap, "
                "then define the smallest isolated fixture that can disprove or validate each candidate."
            ),
            "source_ids": source_ids,
            "sources": [
                {
                    key: source[key]
                    for key in ("id", "kind", "candidate_id", "publisher", "title", "url", "selection_note")
                    if key in source
                }
                for source in selected_sources
            ],
            "candidate_tools": [
                {
                    "source_id": source["id"],
                    "candidate_id": source["candidate_id"],
                    "title": source["title"],
                    "url": source["url"],
                    "required_controls": source["required_controls"],
                    "candidate_only": True,
                    "automatic_install_allowed": False,
                }
                for source in selected_sources if source.get("kind") == "tool"
            ],
            "security_advisories": [
                {
                    "source_id": source["id"],
                    "candidate_id": source.get("candidate_id"),
                    "title": source["title"],
                    "url": source["url"],
                    "selection_note": source["selection_note"],
                }
                for source in selected_sources if source.get("kind") == "advisory"
            ],
            "model_route": {
                "first": "local_smart",
                "cloud_review": "analysis_only_after_repeated_failure",
                "astra_call_cap": int(policy["max_astra_reviews_per_engagement"]),
            },
            "adoption_gate": {gate: False for gate in registry["adoption_gates"]},
            "promotion_allowed": False,
        })
    plan = {
        "version": 1,
        "registry_last_verified": registry["last_verified"],
        "catalog_snapshot_sha256": catalog.get("source_snapshot_sha256"),
        "local_inventory": {
            "engine_count": len(catalog.get("engines", [])),
            "skill_count": len(catalog.get("skills", [])),
            "registered_tool_count": len(catalog.get("registered_tools", [])),
        },
        "status": "research_required" if tasks else "no_capability_gap_detected",
        "tasks": tasks,
        "local_inventory_checked_first": True,
        "customer_scheduler_web_access": False,
        "separate_research_worker_required": bool(tasks),
        "customer_text_included": False,
        "customer_targets_included": False,
        "external_fetch_performed": False,
        "candidate_download_performed": False,
        "candidate_install_performed": False,
        "customer_execution_allowed": False,
    }
    verification = verify_resource_research_plan(plan, registry)
    if not verification["verified"]:
        raise ValueError("Resource research plan failed verification: " + "; ".join(verification["failures"]))
    plan["verification"] = verification
    return plan
