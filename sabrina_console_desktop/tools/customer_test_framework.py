"""Build and verify a disabled-by-default SOW-derived test framework bundle."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tools.customer_capability_proofs import PROOF_SPECS, candidate_proof_summary
from tools.lab_capability_catalog import build_capability_catalog, build_sow_code_review, build_testing_framework


CATEGORY_CASES = {
    "web_application": [
        ("WEB-INFO", "Map application roles, workflows, inputs, and trust boundaries", ["subdomain_recon_engine"]),
        ("WEB-CONFIG", "Review transport, browser security, and deployment configuration", ["clickjacking_engine"]),
        ("WEB-INPUT", "Test input validation with non-destructive payloads", ["sqli_engine", "xss_engine", "ssti_engine", "xxe_engine"]),
        ("WEB-CSRF", "Review cross-site request forgery protections on approved state-changing workflows", ["csrf_engine"]),
        ("WEB-FILES", "Review file upload and path handling with controlled non-executing fixtures", ["lfi_engine", "file_upload_engine"]),
        ("WEB-REDIRECT", "Review redirect destinations and allowlist enforcement", ["open_redirect_engine"]),
        ("WEB-LOGIC", "Review business workflows and state transitions manually", []),
    ],
    "api": [
        ("API-INVENTORY", "Build an authorized API and schema inventory", ["subdomain_recon_engine"]),
        ("API-INPUT", "Test schema, parsing, and input handling", ["sqli_engine", "xxe_engine", "ssrf_engine"]),
        ("API-AUTHZ", "Test object and function authorization", ["idor_engine"]),
        ("API-RESILIENCE", "Review rate controls without load or denial-of-service testing", []),
    ],
    "authentication": [
        ("AUTH-LIFECYCLE", "Review enrollment, recovery, MFA, and identity lifecycle", []),
        ("AUTH-SESSION", "Review session creation, rotation, invalidation, and cookie controls", []),
    ],
    "authorization": [
        ("AUTHZ-HORIZONTAL", "Test horizontal object ownership boundaries", ["idor_engine"]),
        ("AUTHZ-VERTICAL", "Test vertical role and forced-browsing boundaries", ["idor_engine"]),
    ],
    "cloud_or_container": [
        ("CLOUD-CONFIG", "Review cloud and container configuration boundaries", []),
        ("CLOUD-SECRETS", "Review secret placement and image or dependency exposure", []),
    ],
    "source_review": [
        ("CODE-TRUST", "Review trust boundaries and authorization decisions", []),
        ("CODE-DEPS", "Review dependencies, static findings, and embedded secrets", []),
    ],
    "network": [
        ("NET-DISCOVERY", "Map only approved hosts, DNS names, ports, and exposed services", ["subdomain_recon_engine", "nmap_engine"]),
        ("NET-TLS", "Review TLS and exposed service configuration using bounded nondestructive checks", ["nmap_engine"]),
        ("NET-BOUNDARY", "Review supplied segmentation and firewall evidence without bypass attempts", []),
    ],
}

REQUIRED_GATE_FIELDS = (
    "signed_sow_verified",
    "scope_imported_and_hash_verified",
    "test_window_open",
    "emergency_contact_confirmed",
    "operator_execution_approval",
)

ENGINE_TEST_HINTS = {
    "ssti_engine": ["tests/unit/test_engines_gated.py"],
    "lfi_engine": ["tests/unit/test_engines_gated.py"],
    "idor_engine": ["tests/unit/test_engines_gated.py"],
    "subdomain_takeover_engine": ["tests/unit/test_engines_gated.py"],
    "xss_engine": ["tests/unit/test_engines_gated.py"],
    "open_redirect_engine": ["tests/unit/test_engines_gated.py"],
    "file_upload_engine": ["tests/unit/test_engines_gated.py"],
    "ssrf_engine": ["tests/unit/test_engine_subprocess_gating.py"],
    "csrf_engine": ["tests/unit/test_engines_trusted_scope.py"],
    "xxe_engine": ["tests/unit/test_engines_trusted_scope.py"],
    "clickjacking_engine": ["tests/unit/test_engines_trusted_scope.py"],
    "sqli_engine": ["tests/unit/test_engines_trusted_scope.py"],
    "http_smuggling_engine": ["tests/unit/test_http_smuggling_gated.py"],
    "subdomain_recon_engine": ["tests/unit/test_subdomain_recon_engine.py"],
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_test_matrix(framework: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    available = set(catalog.get("engines", []))
    catalog_root = Path(catalog.get("root") or ".")
    source_manifest = catalog.get("source_manifest", [])
    source_by_path = {row["path"]: row for row in source_manifest}
    test_paths = [row["path"] for row in source_manifest if row["path"].startswith("tests/")]
    cases = []
    candidate_proofs = {}
    for category in framework["review"]["requested_test_categories"]:
        for case_id, objective, candidates in CATEGORY_CASES.get(category, []):
            local_candidates = [candidate for candidate in candidates if candidate in available]
            missing_candidates = [candidate for candidate in candidates if candidate not in available]
            cases.append({
                "id": case_id,
                "category": category,
                "objective": objective,
                "local_candidates": local_candidates,
                "missing_candidates": missing_candidates,
                "execution_mode": "disabled",
                "requires_operator_approval": True,
                "requires_explicit_method_approval": case_id in {"WEB-CSRF", "WEB-FILES"},
                "required_evidence": ["request or review step", "observed result", "deterministic verdict", "limitation note"],
                "prohibited_by_default": [
                    "denial of service", "destructive changes", "data exfiltration", "social engineering",
                    "persistent uploads or webshell execution", "unapproved state-changing requests",
                ],
            })
            for candidate in local_candidates:
                if candidate in candidate_proofs:
                    continue
                engine_path = f"tools/engines/{candidate}.py"
                stem = candidate.removesuffix("_engine")
                direct = [path for path in test_paths if stem in Path(path).stem.lower()]
                hinted = [path for path in ENGINE_TEST_HINTS.get(candidate, []) if path in source_by_path]
                declared = [
                    selector.split("::", 1)[0]
                    for selector in PROOF_SPECS.get(candidate, {}).get("selectors", ())
                    if selector.split("::", 1)[0] in source_by_path
                ]
                references = sorted(set(direct + hinted + declared))
                runtime_proof = candidate_proof_summary(candidate, root=catalog_root)
                dimensions = set(runtime_proof.get("verified_dimensions", []))
                if runtime_proof.get("receipt_verified") and {"safety_gate", "functional_behavior"}.issubset(dimensions):
                    proof_status = "verified_isolated_fixture"
                elif runtime_proof.get("receipt_verified") and "safety_gate" in dimensions:
                    proof_status = "safety_fixture_only"
                elif references:
                    proof_status = "static_test_reference_only"
                else:
                    proof_status = "unproven_in_snapshot"
                candidate_proofs[candidate] = {
                    "path": engine_path,
                    "sha256": (source_by_path.get(engine_path) or {}).get("sha256"),
                    "test_references": references,
                    "proof_status": proof_status,
                    "runtime_proof": runtime_proof,
                    "customer_execution_authorized": False,
                }
    mapped_candidates = sorted({
        candidate
        for cases in CATEGORY_CASES.values()
        for _case_id, _objective, candidates in cases
        for candidate in candidates
    })
    unmapped = sorted(available - set(mapped_candidates))
    exclusion_reasons = {
        "custom_engine_engine": "ambiguous placeholder engine with no customer-safe assessment method",
        "http_smuggling_engine": "raw-socket timing probes require separate protocol-method approval and a current isolated proof receipt",
        "http_smuggling_gated": "request-smuggling detection requires separate protocol-method approval and a current isolated proof receipt",
        "subdomain_takeover_engine": "DNS resolution is not yet bound to the trusted engagement scope before lookup",
    }
    return {
        "version": 1,
        "cases": cases,
        "candidate_proofs": candidate_proofs,
        "unmapped_local_engines": [
            {
                "engine": name,
                "eligible_for_customer_plan": False,
                "reason": exclusion_reasons.get(name, "engine has no SOW category mapping or verified customer-safe method"),
            }
            for name in unmapped
        ],
        "case_count": len(cases),
        "all_execution_disabled": all(case["execution_mode"] == "disabled" for case in cases),
        "contains_customer_targets": False,
        "contains_credentials": False,
    }


def build_framework_bundle(
    sow_text: str,
    destination: str | Path,
    *,
    catalog: dict[str, Any] | None = None,
    code_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    catalog = catalog or build_capability_catalog()
    framework = build_testing_framework(sow_text, catalog)
    code_review = code_review or build_sow_code_review(sow_text, catalog=catalog)
    matrix = build_test_matrix(framework, catalog)
    gate = {
        "version": 1,
        "execution_enabled": False,
        "requirements": {field: False for field in REQUIRED_GATE_FIELDS},
        "required_true_fields": list(REQUIRED_GATE_FIELDS),
        "scope_reference": {"sow_sha256": framework["review"]["sow_sha256"], "customer_targets_in_bundle": False},
        "credentials_present": False,
        "next_action": "operator verifies signed scope and creates a separate execution approval",
    }
    method_references = {
        "version": 1,
        "references": code_review.get("method_references", []),
        "network_research_performed": False,
        "note": "References are official-method starting points. Their presence is not proof that a local candidate works.",
    }
    payloads = {
        "framework.json": framework,
        "test_matrix.json": matrix,
        "execution_gate.json": gate,
        "method_references.json": method_references,
    }
    for name, value in payloads.items():
        _json_write(destination / name, value)
    manifest = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sow_sha256": framework["review"]["sow_sha256"],
        "catalog_snapshot_sha256": catalog["source_snapshot_sha256"],
        "code_review_snapshot_consistent": code_review.get("catalog_snapshot_consistent", False),
        "execution_enabled": False,
        "customer_targets_present": False,
        "credentials_present": False,
        "case_count": matrix["case_count"],
        "unproven_candidates": sorted(
            name for name, proof in matrix["candidate_proofs"].items()
            if proof["proof_status"] != "verified_isolated_fixture"
        ),
        "payload_sha256": {name: _sha(destination / name) for name in payloads},
    }
    _json_write(destination / "bundle_manifest.json", manifest)
    verification = verify_framework_bundle(destination)
    _json_write(destination / "verification.json", verification)
    return {**manifest, "path": str(destination.resolve()), "verification": verification}


def verify_framework_bundle(destination: str | Path) -> dict[str, Any]:
    destination = Path(destination)
    manifest_path = destination / "bundle_manifest.json"
    failures = []
    if not manifest_path.is_file():
        return {"verified": False, "failures": ["bundle_manifest.json is missing"], "execution_enabled": False}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in manifest.get("payload_sha256", {}).items():
        path = destination / name
        if not path.is_file():
            failures.append(f"missing payload: {name}")
        elif _sha(path) != expected:
            failures.append(f"payload hash mismatch: {name}")
    try:
        gate = json.loads((destination / "execution_gate.json").read_text(encoding="utf-8"))
        matrix = json.loads((destination / "test_matrix.json").read_text(encoding="utf-8"))
    except Exception as error:
        failures.append("payload parse error: " + str(error))
        gate, matrix = {}, {}
    if gate.get("execution_enabled") is not False:
        failures.append("execution gate is enabled")
    if any(gate.get("requirements", {}).get(field) is not False for field in REQUIRED_GATE_FIELDS):
        failures.append("one or more execution requirements are not false")
    if matrix.get("all_execution_disabled") is not True:
        failures.append("test matrix contains an enabled case")
    if manifest.get("customer_targets_present") is not False or matrix.get("contains_customer_targets") is not False:
        failures.append("bundle contains customer targets")
    if manifest.get("credentials_present") is not False or matrix.get("contains_credentials") is not False:
        failures.append("bundle contains credentials")
    return {
        "version": 1,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "verified": not failures,
        "failures": failures,
        "execution_enabled": False,
        "payload_count": len(manifest.get("payload_sha256", {})),
    }
