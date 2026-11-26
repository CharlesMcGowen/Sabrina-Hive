"""Inventory Sabrina's lab and build bounded, SOW-derived test plans."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tools.messaging_store import ROOT

IGNORED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "data", "artifacts", "work",
}
CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".ps1",
    ".bat", ".cmd", ".sql", ".html", ".css", ".vue", ".svelte",
}
CODE_FILENAMES = {"Dockerfile", "Containerfile", "Makefile", "Rakefile"}
SUPPORT_SUFFIXES = {".yaml", ".yml", ".json", ".jsonc", ".toml", ".ini", ".cfg", ".md"}
SOURCE_SUFFIXES = CODE_SUFFIXES | SUPPORT_SUFFIXES


def _is_code_path(path: Path) -> bool:
    return path.suffix.lower() in CODE_SUFFIXES or path.name in CODE_FILENAMES


def _source_files(root: Path) -> list[Path]:
    return sorted(
        path for path in root.rglob("*")
        if path.is_file()
        and (path.suffix.lower() in SOURCE_SUFFIXES or path.name in CODE_FILENAMES)
        and not (set(path.parts) & IGNORED_PARTS)
    )


def build_capability_catalog(root: str | Path = ROOT) -> dict[str, Any]:
    root = Path(root)
    engines = []
    engine_support_modules = []
    engine_dir = root / "tools/engines"
    if engine_dir.exists():
        engine_modules = [path for path in sorted(engine_dir.glob("*.py")) if path.name != "__init__.py"]
        engines = [
            path.stem for path in engine_modules
            if path.stem.endswith("_engine") or path.stem in {"http_smuggling_gated"}
        ]
        engine_support_modules = [
            {
                "module": path.stem,
                "reason": (
                    "shared engine helper"
                    if path.stem == "gate_guarded_request" else
                    "generated research or provenance artifact, not a registered assessment engine"
                ),
            }
            for path in engine_modules if path.stem not in engines
        ]
    skills = []
    skill_dir = root / "skills"
    if skill_dir.exists():
        skills = [str(path.parent.relative_to(root)).replace("\\", "/") for path in sorted(skill_dir.glob("*/SKILL.md"))]
    registered_tools = []
    tool_config = root / "config/tools.yaml"
    if tool_config.exists():
        for line in tool_config.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            match = re.match(r"^  ([a-zA-Z0-9_]+):\s*$", line)
            if match:
                registered_tools.append(match.group(1))
    digest = hashlib.sha256()
    files = _source_files(root)
    source_manifest = []
    for path in files:
        relative = str(path.relative_to(root)).replace("\\", "/")
        content = path.read_bytes()
        file_hash = hashlib.sha256(content).hexdigest()
        digest.update(relative.encode())
        digest.update(content)
        source_manifest.append({
            "path": relative,
            "sha256": file_hash,
            "size_bytes": len(content),
            "file_kind": "code" if _is_code_path(path) else "support",
        })
    return {
        "catalog_version": 1,
        "root": str(root),
        "source_snapshot_sha256": digest.hexdigest(),
        "source_file_count": len(files),
        "source_manifest": source_manifest,
        "engines": engines,
        "engine_support_modules": engine_support_modules,
        "skills": skills,
        "registered_tools": registered_tools,
        "limitations": [
            "Catalog presence does not prove a capability works against a customer target.",
            "No customer testing is authorized by this inventory.",
            "A signed SOW and rules of engagement remain mandatory."
        ],
    }


REFERENCE_METHODS = {
    "web_application": [
        {"name": "OWASP Web Security Testing Guide 4.2", "url": "https://wstg.owasp.org/v4.2/"},
        {"name": "NCSC penetration testing guidance", "url": "https://www.ncsc.gov.uk/guidance/penetration-testing"},
    ],
    "api": [
        {"name": "OWASP API Security Top 10 2023", "url": "https://api-security.owasp.org/editions/2023/en/0x11-t10/"},
        {"name": "OWASP Web Security Testing Guide 4.2", "url": "https://wstg.owasp.org/v4.2/"},
    ],
    "authentication": [
        {"name": "OWASP authentication testing 4.2", "url": "https://wstg.owasp.org/v4.2/4-Web_Application_Security_Testing/04-Authentication_Testing/"},
    ],
    "authorization": [
        {"name": "OWASP authorization testing 4.2", "url": "https://wstg.owasp.org/v4.2/4-Web_Application_Security_Testing/05-Authorization_Testing/"},
    ],
    "cloud_or_container": [
        {"name": "NIST SP 800-190 Application Container Security Guide", "url": "https://csrc.nist.gov/pubs/sp/800/190/final"},
    ],
    "source_review": [
        {"name": "OWASP Code Review Guide", "url": "https://owasp.org/projects/code-review-guide"},
        {"name": "OWASP testing framework 4.2", "url": "https://wstg.owasp.org/v4.2/3-The_OWASP_Testing_Framework/"},
    ],
    "network": [
        {"name": "NIST SP 800-115 Technical Guide to Information Security Testing and Assessment", "url": "https://csrc.nist.gov/pubs/sp/800/115/final"},
        {"name": "NCSC penetration testing guidance", "url": "https://www.ncsc.gov.uk/guidance/penetration-testing"},
    ],
}


def _sow_terms(text: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", (text or "").lower())
    stop = {"the", "and", "with", "from", "that", "this", "will", "shall", "for", "are", "our", "your", "test", "testing", "scope"}
    return sorted({word for word in words if word not in stop})[:80]


def build_sow_code_review(sow_text: str, *, root: str | Path = ROOT, catalog: dict[str, Any] | None = None, max_files: int = 200) -> dict[str, Any]:
    """Create a traceable, targeted semantic review queue over the full snapshot.

    Every source file contributes to the catalog hash and relevance review.
    Every relevant code file is queued with evidence. ``max_files`` caps only
    additional support documents so executable source is never truncated.
    """
    root = Path(root)
    catalog = catalog or build_capability_catalog(root)
    review = review_sow(sow_text, catalog)
    terms = _sow_terms(sow_text)
    category_terms = {
        "web_application": {"http", "browser", "web", "xss", "sqli", "csrf", "ssrf"},
        "api": {"api", "graphql", "rest", "http", "endpoint"},
        "authentication": {"auth", "login", "session", "oauth", "sso", "mfa"},
        "authorization": {"authorization", "access", "idor", "privilege", "rbac"},
        "cloud_or_container": {"cloud", "container", "docker", "podman", "kubernetes", "aws", "azure", "gcp"},
        "source_review": {"sast", "source", "code", "dependency", "secret"},
        "network": {"network", "host", "port", "service", "tcp", "udp", "tls", "dns", "firewall"},
    }
    search_terms = set(terms)
    for category in review["requested_test_categories"]:
        search_terms.update(category_terms.get(category, set()))
    ranked = []
    file_review_index = []
    manifest = catalog.get("source_manifest")
    if not isinstance(manifest, list):
        manifest = [
            {
                "path": str(path.relative_to(root)).replace("\\", "/"),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
                "file_kind": "code" if _is_code_path(path) else "support",
            }
            for path in _source_files(root)
        ]
    manifest_paths = {entry["path"] for entry in manifest}
    current_paths = {str(path.relative_to(root)).replace("\\", "/") for path in _source_files(root)}
    snapshot_drift = {
        "missing_files": sorted(manifest_paths - current_paths),
        "new_files": sorted(current_paths - manifest_paths),
        "changed_files": [],
    }
    for entry in manifest:
        relative = entry["path"]
        path = root / relative
        if not path.is_file():
            continue
        try:
            content = path.read_bytes()
            text = content.decode("utf-8-sig", errors="replace")
        except OSError:
            continue
        current_hash = hashlib.sha256(content).hexdigest()
        if current_hash != entry["sha256"]:
            snapshot_drift["changed_files"].append(relative)
        lower = (relative + "\n" + text).lower()
        matched = sorted(term for term in search_terms if term in lower)
        score = sum(min(lower.count(term), 5) for term in matched)
        evidence = []
        for number, line in enumerate(text.splitlines(), 1):
            line_lower = line.lower()
            hits = [term for term in matched if term in line_lower]
            if hits:
                evidence.append({"line": number, "terms": hits[:5], "text": line.strip()[:240]})
            if len(evidence) >= 3:
                break
        file_hash = entry["sha256"]
        file_kind = entry.get("file_kind") or ("code" if _is_code_path(path) else "support")
        file_review_index.append({
            "path": relative,
            "sha256": file_hash,
            "file_kind": file_kind,
            "line_count": len(text.splitlines()),
            "matched_terms": matched,
            "relevance_score": score,
            "disposition": "deep_review" if matched else "no_sow_relevance_signal",
        })
        if matched:
            ranked.append((score, relative, file_hash, file_kind, matched, evidence))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    relevant_code = [item for item in ranked if item[3] == "code"]
    relevant_support = [item for item in ranked if item[3] != "code"]
    selected_items = relevant_code + relevant_support[:max(1, min(int(max_files), 500))]
    selected_items.sort(key=lambda item: (-item[0], item[1]))
    selected = [
        {
            "path": relative,
            "sha256": file_hash,
            "file_kind": file_kind,
            "score": score,
            "matched_terms": matched[:20],
            "evidence": evidence,
        }
        for score, relative, file_hash, file_kind, matched, evidence in selected_items
    ]
    references = []
    for category in review["requested_test_categories"]:
        references.extend(REFERENCE_METHODS.get(category, []))
    references = list({item["url"]: item for item in references}.values())
    snapshot_consistent = not any(snapshot_drift.values())
    code_index = [item for item in file_review_index if item["file_kind"] == "code"]
    selected_code_count = sum(item["file_kind"] == "code" for item in selected)
    omitted_relevant_support_count = max(0, len(relevant_support) - sum(item["file_kind"] == "support" for item in selected))
    category_coverage = {}
    for category in review["requested_test_categories"]:
        terms_for_category = category_terms.get(category, set())
        paths = [
            item["path"] for item in file_review_index
            if terms_for_category.intersection(item["matched_terms"])
        ]
        category_coverage[category] = {"relevant_files": paths, "covered": bool(paths)}
    return {
        "code_review_version": 1,
        "catalog_snapshot_sha256": catalog["source_snapshot_sha256"],
        "all_source_files_hashed": catalog["source_file_count"],
        "all_files_reviewed_for_relevance": len(file_review_index) == catalog["source_file_count"],
        "catalog_snapshot_consistent": snapshot_consistent,
        "snapshot_drift": snapshot_drift,
        "reviewed_source_file_count": len(file_review_index),
        "code_file_count": len(code_index),
        "reviewed_code_file_count": len(code_index),
        "all_code_files_reviewed_for_relevance": len(code_index) == sum(
            (entry.get("file_kind") or ("code" if _is_code_path(root / entry["path"]) else "support")) == "code"
            for entry in manifest
        ),
        "relevant_code_file_count": len(relevant_code),
        "deep_review_code_file_count": selected_code_count,
        "all_relevant_code_queued": selected_code_count == len(relevant_code),
        "omitted_relevant_code_file_count": max(0, len(relevant_code) - selected_code_count),
        "omitted_relevant_support_file_count": omitted_relevant_support_count,
        "support_review_queue_capped": omitted_relevant_support_count > 0,
        "unmatched_source_file_count": sum(item["disposition"] == "no_sow_relevance_signal" for item in file_review_index),
        "file_review_index": file_review_index,
        "sow_terms": terms,
        "deep_review_queue": selected,
        "deep_review_file_count": len(selected),
        "method_references": references,
        "category_coverage": category_coverage,
        "coverage_gaps": [category for category, value in category_coverage.items() if not value["covered"]],
        "execution_allowed": False,
        "next_gate": "review queued files, prove tools in isolated fixtures, then obtain operator execution approval",
    }


REQUIRED_ROE = {
    "in_scope_assets": (r"\b(in.scope|target|domain|host|application|api)\b",),
    "out_of_scope_assets": (r"\b(out.of.scope|exclusion|excluded|do not test)\b",),
    "written_authorization": (r"\bauthori[sz](?:e|ed|es|ing|ation)\b|\b(?:permission|authority)\b",),
    "test_window": (r"\b(test window|schedule|start date|end date|hours)\b",),
    "allowed_and_prohibited_methods": (r"\b(allowed|permitted|prohibited|forbidden|denial of service|social engineering)\b",),
    "customer_contacts": (r"\b(contact|escalation|incident response|emergency)\b",),
    "data_handling": (r"\b(data handling|retention|deletion|evidence|confidential)\b",),
    "deliverables": (r"\b(report|deliverable|retest|readout)\b",),
}


def review_sow(text: str, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    text = text or ""
    catalog = catalog or build_capability_catalog()
    present = {field: any(re.search(pattern, text, re.I) for pattern in patterns) for field, patterns in REQUIRED_ROE.items()}
    missing = [field for field, found in present.items() if not found]
    categories = []
    for label, pattern in (
        ("web_application", r"\b(web|website|browser|domain|url)\b"),
        ("api", r"\bapi|graphql|rest\b"),
        ("authentication", r"\bauthentication|login|session|mfa|oauth|sso\b"),
        ("authorization", r"\bauthorization|access control|idor|privilege\b"),
        ("cloud_or_container", r"\bcloud|aws|azure|gcp|container|kubernetes|docker|podman\b"),
        ("source_review", r"\bsource code|code review|sast|repository\b"),
        ("network", r"\bnetwork|network security|port scan|service discovery|host discovery|tcp|udp|firewall|network segmentation\b"),
    ):
        if re.search(pattern, text, re.I):
            categories.append(label)
    return {
        "review_version": 1,
        "sow_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "authorization_ready": not missing,
        "present_fields": present,
        "missing_fields": missing,
        "requested_test_categories": categories,
        "catalog_snapshot_sha256": catalog["source_snapshot_sha256"],
        "planning_only": True,
        "next_action": "operator_and_customer_resolve_missing_rules_of_engagement" if missing else "operator_approves_test_plan_before_execution",
    }


def build_testing_framework(sow_text: str, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog = catalog or build_capability_catalog()
    review = review_sow(sow_text, catalog)
    coverage = {
        "web_application": ["OWASP WSTG information gathering", "configuration", "input validation", "business logic"],
        "api": ["API inventory", "schema and input validation", "rate limits", "object-level authorization"],
        "authentication": ["identity lifecycle", "authentication", "session management", "recovery controls"],
        "authorization": ["horizontal access", "vertical access", "forced browsing", "object ownership"],
        "cloud_or_container": ["configuration review", "image and dependency scan", "secrets review", "network boundaries"],
        "source_review": ["static analysis", "dependency analysis", "secret scanning", "manual trust-boundary review"],
        "network": ["authorized host discovery", "bounded port and service inventory", "TLS configuration review", "segmentation evidence review"],
    }
    phases = [
        {"name": "scope_validation", "execution_allowed": False, "exit_gate": "signed authorization and complete rules of engagement"},
        {"name": "capability_validation", "execution_allowed": False, "exit_gate": "tools proven in an isolated fixture"},
        {"name": "customer_test", "execution_allowed": False, "exit_gate": "separate operator approval for the authorized window"},
        {"name": "evidence_and_report", "execution_allowed": False, "exit_gate": "finding quality and data-handling review"},
    ]
    return {
        "framework_version": 1,
        "review": review,
        "coverage": {name: coverage[name] for name in review["requested_test_categories"]},
        "available_engines": catalog["engines"],
        "catalog_snapshot_sha256": catalog["source_snapshot_sha256"],
        "phases": phases,
        "all_execution_disabled": True,
    }


@dataclass(frozen=True)
class InternalResourceUsage:
    local_gpu_hours: float = 0.0
    podman_reserved_hours: float = 0.0
    human_hours: float = 0.0
    cloud_input_tokens: int = 0
    cloud_output_tokens: int = 0
    cloud_model: str = ""


def calculate_internal_cost(usage: InternalResourceUsage, defaults: dict[str, Any]) -> dict[str, Any]:
    required = ("local_gpu_hourly_cost", "podman_reserved_hourly_opportunity_cost", "human_hourly_cost")
    if any(defaults.get(name) is None for name in required):
        return {"internal_only": True, "complete": False, "missing_rates": [name for name in required if defaults.get(name) is None]}
    model = usage.cloud_model
    in_rate = defaults.get("cloud_input_cost_per_million_tokens", {}).get(model)
    out_rate = defaults.get("cloud_output_cost_per_million_tokens", {}).get(model)
    if (usage.cloud_input_tokens or usage.cloud_output_tokens) and (in_rate is None or out_rate is None):
        return {"internal_only": True, "complete": False, "missing_rates": ["cloud token rates for " + model]}
    components = {
        "local_gpu": usage.local_gpu_hours * float(defaults["local_gpu_hourly_cost"]),
        "podman_opportunity": usage.podman_reserved_hours * float(defaults["podman_reserved_hourly_opportunity_cost"]),
        "human": usage.human_hours * float(defaults["human_hourly_cost"]),
        "cloud": usage.cloud_input_tokens / 1_000_000 * float(in_rate or 0) + usage.cloud_output_tokens / 1_000_000 * float(out_rate or 0),
    }
    return {"internal_only": True, "complete": True, "currency": defaults.get("currency", "USD"), "components": components, "total": sum(components.values()), "usage": asdict(usage)}


def customer_safe_view(value: Any) -> Any:
    """Remove internal economics and implementation details recursively."""
    hidden = {"internal_cost", "internal_only", "components", "usage", "model", "cloud_model", "podman", "gpu", "tokens", "margin", "markup"}
    if isinstance(value, dict):
        return {key: customer_safe_view(item) for key, item in value.items() if key.lower() not in hidden and not any(word in key.lower() for word in ("podman", "gpu", "token", "margin", "internal_cost"))}
    if isinstance(value, list):
        return [customer_safe_view(item) for item in value]
    return value


def route_model(task: str, *, risk: str = "low", novelty: str = "known", volume: str = "small") -> str:
    if risk == "high" and novelty == "novel":
        return "gpt-6-astra"
    if risk in {"medium", "high"} or novelty == "novel":
        return "gpt-6.1-sol"
    if task in {"sow_extraction", "scope_mapping"} or volume == "large":
        return "local_smart"
    return "local_fast"
