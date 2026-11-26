"""Target-free public-CVE case-study selection for Sabrina's isolated lab."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools.cve_feed import public_cve_review


def select_case_study(cves: list[dict[str, Any]], fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the first public fixed CVE with an operator-declared local fixture.

    ``fixtures`` is local metadata, never a target list. A fixture must state its
    local ID, supported CVE IDs, and that it is isolated. No network action or
    exploit instruction is accepted here.
    """
    if not isinstance(cves, list) or not isinstance(fixtures, list):
        raise ValueError("CVE candidates and fixtures must be lists")
    by_cve: dict[str, dict[str, Any]] = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict) or fixture.get("isolated") is not True:
            continue
        fixture_id = fixture.get("fixture_id")
        supported = fixture.get("cve_ids")
        if not isinstance(fixture_id, str) or not fixture_id or not isinstance(supported, list):
            continue
        for cve_id in supported:
            if isinstance(cve_id, str):
                by_cve[cve_id.upper()] = fixture
    rejected = []
    for cve in cves:
        if not isinstance(cve, dict):
            continue
        cve_id = str(cve.get("id", "")).upper()
        advisory = cve.get("vendor_advisory")
        fixed = cve.get("fixed_version")
        if not isinstance(advisory, str) or not advisory.startswith("https://"):
            rejected.append({"id":cve_id,"reason":"vendor_advisory_missing"}); continue
        if not isinstance(fixed, str) or not fixed.strip():
            rejected.append({"id":cve_id,"reason":"fixed_version_missing"}); continue
        fixture = by_cve.get(cve_id)
        if not fixture:
            rejected.append({"id":cve_id,"reason":"isolated_fixture_missing"}); continue
        review = public_cve_review(cve, "Use the vendor-published fixed release: " + fixed.strip())
        return {"status":"selected","case_study":review,"advisory_url":advisory,
                "fixed_version":fixed.strip(),"fixture_id":fixture["fixture_id"],
                "fixture_mapping":fixture.get("mapping", "operator-declared isolated fixture"),
                "benchmark_score":None,"live_target_authorization":False,
                "limitations":["This is not an ExploitGym score.","No live target was contacted or authorized.","The fixture does not establish real-world exploitability."]}
    return {"status":"blocked","reason":"no_fixed_public_cve_with_compatible_isolated_fixture",
            "rejected":rejected,"benchmark_score":None,"live_target_authorization":False}


def write_case_study(record: dict[str, Any], artifact_root: str | Path, name: str) -> Path:
    """Write an immutable case-study record outside the source checkout."""
    root = Path(artifact_root).resolve()
    source = Path(__file__).resolve().parents[1]
    if root == source or root.is_relative_to(source):
        raise ValueError("case-study artifacts must be outside Sabrina source")
    if not name or Path(name).name != name:
        raise ValueError("case-study artifact name must be a simple filename")
    root.mkdir(parents=True, exist_ok=True)
    output = root / name
    if output.exists():
        raise FileExistsError("case-study artifact already exists")
    output.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return output
