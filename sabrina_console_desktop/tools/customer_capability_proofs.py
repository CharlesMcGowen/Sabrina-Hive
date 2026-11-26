"""Create and verify snapshot-bound offline proof receipts for local test engines.

The runner accepts only the selectors declared in ``PROOF_SPECS``. A receipt is
evidence about an isolated local fixture, not authority to contact a customer or
to enable an engagement test.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools.messaging_store import ROOT


PROOF_ROOT = ROOT / "data/customer_capability_proofs"
MAX_PROOF_AGE_DAYS = 30

PROOF_SPECS: dict[str, dict[str, tuple[str, ...]]] = {
    "ssti_engine": {
        "selectors": (
            "tests/unit/test_engines_gated.py::test_ssti_fails_closed",
            "tests/unit/test_engines_gated.py::test_ssti_in_scope_uses_gate",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "lfi_engine": {
        "selectors": (
            "tests/unit/test_engines_gated.py::test_lfi_fails_closed",
            "tests/unit/test_engines_gated.py::test_lfi_controlled_fixture_detects_file_signature",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "idor_engine": {
        "selectors": (
            "tests/unit/test_engines_gated.py::test_idor_fails_closed",
            "tests/unit/test_engines_gated.py::test_idor_controlled_fixture_flags_matching_unauthenticated_body",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "xss_engine": {
        "selectors": (
            "tests/unit/test_engines_gated.py::test_xss_main_fails_closed",
            "tests/unit/test_engines_gated.py::test_xss_controlled_fixture_detects_reflection",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "open_redirect_engine": {
        "selectors": (
            "tests/unit/test_engines_gated.py::test_open_redirect_main_fails_closed",
            "tests/unit/test_engines_gated.py::test_open_redirect_controlled_fixture_detects_external_location",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "file_upload_engine": {
        "selectors": (
            "tests/unit/test_engines_gated.py::test_file_upload_main_fails_closed",
            "tests/unit/test_engines_gated.py::test_file_upload_controlled_fixture_detects_reflected_marker",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "ssrf_engine": {
        "selectors": ("tests/unit/test_engine_subprocess_gating.py",),
        "dimensions": ("safety_gate", "functional_behavior", "subprocess_inheritance"),
    },
    "csrf_engine": {
        "selectors": (
            "tests/unit/test_engines_trusted_scope.py::test_csrf_fails_closed_without_trusted_scope",
            "tests/unit/test_engines_trusted_scope.py::test_csrf_passes_with_trusted_scope",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "xxe_engine": {
        "selectors": (
            "tests/unit/test_engines_trusted_scope.py::test_xxe_fails_closed_without_trusted_scope",
            "tests/unit/test_engines_trusted_scope.py::test_xxe_controlled_fixture_detects_reflected_file_signature",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "clickjacking_engine": {
        "selectors": (
            "tests/unit/test_engines_trusted_scope.py::test_clickjacking_fails_closed_without_trusted_scope",
            "tests/unit/test_engines_trusted_scope.py::test_clickjacking_controlled_fixture_classifies_missing_protection",
            "tests/unit/test_engines_trusted_scope.py::test_clickjacking_controlled_fixture_accepts_frame_ancestors",
        ),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
    "sqli_engine": {
        "selectors": (
            "tests/unit/test_engines_trusted_scope.py::test_sqli_fails_closed_without_trusted_scope",
            "tests/unit/test_engines_trusted_scope.py::test_sqli_controlled_fixture_detects_positive_signal",
            "tests/unit/test_gate_guarded_request.py::test_sqli_engine_actually_imports",
        ),
        "dimensions": ("safety_gate", "functional_behavior", "import_integrity"),
    },
    "subdomain_recon_engine": {
        "selectors": ("tests/unit/test_subdomain_recon_engine.py",),
        "dimensions": ("safety_gate", "functional_behavior"),
    },
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "receipt_sha256"}
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _test_files(selectors: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({selector.split("::", 1)[0].replace("\\", "/") for selector in selectors}))


def build_proof_receipt(
    candidate_id: str,
    *,
    root: str | Path = ROOT,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Build a receipt only for a statically declared candidate and test plan."""
    if candidate_id not in PROOF_SPECS:
        raise ValueError("Candidate has no allowlisted offline proof specification")
    root = Path(root)
    spec = PROOF_SPECS[candidate_id]
    engine_relative = f"tools/engines/{candidate_id}.py"
    engine_path = root / engine_relative
    if not engine_path.is_file():
        raise FileNotFoundError(engine_relative)
    test_hashes = {}
    for relative in _test_files(spec["selectors"]):
        path = root / relative
        if not path.is_file():
            raise FileNotFoundError(relative)
        test_hashes[relative] = _sha(path)
    passed = int(exit_code) == 0
    receipt: dict[str, Any] = {
        "version": 1,
        "candidate_id": candidate_id,
        "engine_path": engine_relative,
        "engine_sha256": _sha(engine_path),
        "test_command_kind": "allowlisted_pytest_selectors",
        "test_selectors": list(spec["selectors"]),
        "test_file_sha256": test_hashes,
        "declared_dimensions": list(spec["dimensions"]),
        "verified_dimensions": list(spec["dimensions"]) if passed else [],
        "isolated_fixture": True,
        "customer_data_used": False,
        "customer_targets_used": False,
        "external_target_contact_allowed": False,
        "customer_execution_authorized": False,
        "operator_execution_approval": False,
        "exit_code": int(exit_code),
        "passed": passed,
        "completed_at": completed_at or datetime.now(timezone.utc).isoformat(),
        "max_age_days": MAX_PROOF_AGE_DAYS,
        "stdout_tail": str(stdout)[-4000:],
        "stderr_tail": str(stderr)[-4000:],
    }
    receipt["receipt_sha256"] = _canonical_hash(receipt)
    return receipt


def verify_proof_receipt(
    candidate_id: str,
    receipt: dict[str, Any],
    *,
    root: str | Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    failures = []
    root = Path(root)
    spec = PROOF_SPECS.get(candidate_id)
    if spec is None:
        return {"verified": False, "failures": ["candidate is not allowlisted"]}
    engine_relative = f"tools/engines/{candidate_id}.py"
    engine_path = root / engine_relative
    if receipt.get("version") != 1 or receipt.get("candidate_id") != candidate_id:
        failures.append("receipt identity mismatch")
    if receipt.get("engine_path") != engine_relative or not engine_path.is_file():
        failures.append("engine path is missing or mismatched")
    elif receipt.get("engine_sha256") != _sha(engine_path):
        failures.append("engine source changed after proof")
    if receipt.get("test_command_kind") != "allowlisted_pytest_selectors":
        failures.append("proof command is not allowlisted")
    if receipt.get("test_selectors") != list(spec["selectors"]):
        failures.append("test selectors differ from the allowlisted proof plan")
    if receipt.get("declared_dimensions") != list(spec["dimensions"]):
        failures.append("proof dimensions differ from the allowlisted proof plan")
    expected_hashes = receipt.get("test_file_sha256")
    if not isinstance(expected_hashes, dict):
        failures.append("test hashes are missing")
    else:
        for relative in _test_files(spec["selectors"]):
            path = root / relative
            if not path.is_file() or expected_hashes.get(relative) != _sha(path):
                failures.append(f"test fixture changed after proof: {relative}")
    if receipt.get("passed") is not True or receipt.get("exit_code") != 0:
        failures.append("offline proof did not pass")
    if receipt.get("verified_dimensions") != list(spec["dimensions"]):
        failures.append("verified proof dimensions are incomplete")
    required_false = (
        "customer_data_used",
        "customer_targets_used",
        "external_target_contact_allowed",
        "customer_execution_authorized",
        "operator_execution_approval",
    )
    if receipt.get("isolated_fixture") is not True or any(receipt.get(field) is not False for field in required_false):
        failures.append("receipt is not an isolated target-free planning proof")
    try:
        observed = datetime.fromisoformat(str(receipt.get("completed_at")))
        if observed.tzinfo is None:
            raise ValueError("timezone missing")
        current = now or datetime.now(timezone.utc)
        if observed > current + timedelta(minutes=5):
            failures.append("proof timestamp is in the future")
        if current - observed > timedelta(days=MAX_PROOF_AGE_DAYS):
            failures.append("proof receipt expired")
    except (TypeError, ValueError):
        failures.append("proof timestamp is invalid")
    if receipt.get("max_age_days") != MAX_PROOF_AGE_DAYS:
        failures.append("proof expiry policy mismatch")
    if receipt.get("receipt_sha256") != _canonical_hash(receipt):
        failures.append("proof receipt integrity mismatch")
    return {
        "verified": not failures,
        "failures": failures,
        "verified_dimensions": receipt.get("verified_dimensions", []),
        "customer_execution_authorized": False,
    }


def candidate_proof_summary(
    candidate_id: str,
    *,
    root: str | Path = ROOT,
    proof_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(root)
    store = Path(proof_root) if proof_root is not None else root / "data/customer_capability_proofs"
    path = store / f"{candidate_id}.json"
    if not path.is_file():
        return {
            "receipt_present": False,
            "receipt_verified": False,
            "failures": ["no offline proof receipt exists"],
            "verified_dimensions": [],
            "customer_execution_authorized": False,
        }
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return {
            "receipt_present": True,
            "receipt_verified": False,
            "failures": ["proof receipt cannot be read: " + str(error)],
            "verified_dimensions": [],
            "customer_execution_authorized": False,
        }
    verification = verify_proof_receipt(candidate_id, receipt, root=root)
    return {
        "receipt_present": True,
        "receipt_verified": verification["verified"],
        "failures": verification["failures"],
        "verified_dimensions": verification.get("verified_dimensions", []),
        "completed_at": receipt.get("completed_at"),
        "receipt_sha256": receipt.get("receipt_sha256"),
        "customer_execution_authorized": False,
    }


def run_candidate_proof(
    candidate_id: str,
    *,
    root: str | Path = ROOT,
    proof_root: str | Path | None = None,
    timeout_seconds: int = 300,
) -> dict[str, Any]:
    """Run one exact offline proof plan and persist its target-free receipt."""
    if candidate_id not in PROOF_SPECS:
        raise ValueError("Candidate has no allowlisted offline proof specification")
    root = Path(root)
    selectors = PROOF_SPECS[candidate_id]["selectors"]
    env = dict(os.environ)
    env.pop("SABRINA_ACTIVE_SCOPE_JSON", None)
    env.pop("SABRINA_ACTIVE_SCOPE_FILE", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *selectors],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(30, min(int(timeout_seconds), 900)),
            check=False,
        )
        exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as error:
        exit_code = 124
        stdout = error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes) else (error.stdout or "")
        stderr = error.stderr.decode(errors="replace") if isinstance(error.stderr, bytes) else (error.stderr or "")
        stderr += "\noffline proof timed out"
    receipt = build_proof_receipt(
        candidate_id,
        root=root,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )
    store = Path(proof_root) if proof_root is not None else root / "data/customer_capability_proofs"
    store.mkdir(parents=True, exist_ok=True)
    destination = store / f"{candidate_id}.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(destination)
    return {"path": str(destination.resolve()), "receipt": receipt, "verification": verify_proof_receipt(candidate_id, receipt, root=root)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded offline proof fixtures for local Sabrina engines")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--candidate", choices=sorted(PROOF_SPECS))
    group.add_argument("--all", action="store_true")
    args = parser.parse_args()
    candidates = sorted(PROOF_SPECS) if args.all else [args.candidate]
    results = []
    for candidate in candidates:
        result = run_candidate_proof(candidate)
        results.append({
            "candidate_id": candidate,
            "passed": result["receipt"]["passed"],
            "verified": result["verification"]["verified"],
            "verified_dimensions": result["verification"].get("verified_dimensions", []),
            "path": result["path"],
        })
    print(json.dumps({"results": results, "customer_execution_authorized": False}, indent=2))
    if not all(row["verified"] for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
