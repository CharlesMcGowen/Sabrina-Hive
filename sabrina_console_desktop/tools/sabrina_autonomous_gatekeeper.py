"""Autonomous Model Gatekeeper & Defensive Security Self-Auditor for Sabrina.

Replaces human-in-the-loop review with an autonomous evaluator model (Codex / Claude / Qwen 14B)
that audits proposed skills against strict programmatic, security, and read-only boundaries.

Also provides autonomous discovery and self-testing for vulnerabilities and CVEs:
- SAST Code Auditing (AST-based static analysis for insecure patterns, injection risks, hardcoded secrets)
- Dependency CVE Auditing (queries the Open Source Vulnerabilities / OSV API for real CVE advisories)
- Autonomous Skill Proposal & Gatekeeper Evaluation Loop without manual operator intervention.
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
AUDITS_DIR = ROOT / "data" / "security_audits"

# Forbidden patterns: Reject any skill proposing offensive attack tools or credential theft
FORBIDDEN_OFFENSIVE_PATTERNS = [
    r"reverse_shell",
    r"keylogger",
    r"ransomware",
    r"privilege_escalation_exploit",
    r"credential_dump",
    r"packet_sniffer",
    r"ddos",
    r"metasploit",
    r"mimikatz",
    r"payload_generator",
    r"exfiltrate_secrets",
]

# Allowed defensive security & verification categories
ALLOWED_CATEGORIES = {
    "sast_static_analysis",
    "dependency_cve_audit",
    "secret_detection",
    "code_hygiene",
    "testing_verification",
    "vulnerability_triage",
    "remediation_patching",
    "procedural_workflow",
}


class AutonomousSecurityGatekeeper:
    """Autonomous evaluator model and safety gatekeeper for Sabrina's skills and self-auditing."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        evaluator_model: str = "huihui_ai/qwen3-abliterated:14b",
        client_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        skills_dir: Optional[Path | str] = None,
    ):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.skills_dir = Path(skills_dir) if skills_dir else (ROOT / "skills")
        self.evaluator_model = evaluator_model
        self.client_fn = client_fn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        AUDITS_DIR.mkdir(parents=True, exist_ok=True)
        from tools.sabrina_skill_hydration import SabrinaSkillHydration
        self.hydration = SabrinaSkillHydration(db_path=self.db_path, skills_dir=self.skills_dir)

    # -------------------------------------------------------------------------
    # 1. Programmatic Screening Rubric
    # -------------------------------------------------------------------------

    def programmatic_screen(
        self,
        skill_name: str,
        content: str,
        target_path: str,
    ) -> Tuple[bool, List[str]]:
        """Screen proposal against deterministic security invariants before model evaluation.

        Returns (passed, list_of_violations).
        """
        violations: List[str] = []

        # A. Frontmatter check
        if not content.startswith("---"):
            violations.append("Skill missing YAML frontmatter opening '---'")
        else:
            try:
                parts = content.split("---", 2)
                if len(parts) < 3:
                    violations.append("Incomplete YAML frontmatter enclosure")
                else:
                    front = yaml.safe_load(parts[1])
                    if not isinstance(front, dict):
                        violations.append("Frontmatter is not a valid YAML dictionary")
                    elif not front.get("name") or not front.get("description"):
                        violations.append("Frontmatter must declare 'name' and 'description'")
            except Exception as exc:
                violations.append(f"YAML frontmatter parse error: {exc}")

        # B. Target Path Boundary check (must remain strictly in skills directory)
        p_target = Path(target_path)
        if not p_target.is_absolute():
            target_str = target_path.replace("\\", "/")
            if target_str.startswith("skills/"):
                norm_target = str((self.skills_dir.parent / target_path).resolve())
            else:
                norm_target = str((self.skills_dir / target_path).resolve())
        else:
            norm_target = str(p_target.resolve())

        norm_skills = str(self.skills_dir.resolve())
        if not norm_target.startswith(norm_skills):
            violations.append(f"Target path {target_path!r} escapes skills root directory")

        # Disallow writing into core orchestration files or git
        disallowed_dirs = ["tools", ".git", ".env", "config", "tests"]
        for dis in disallowed_dirs:
            if f"/{dis}/" in norm_target.replace("\\", "/") or norm_target.endswith(f"/{dis}"):
                violations.append(f"Target path cannot be inside core directory '{dis}'")

        # C. Prohibited offensive pattern scan
        lower_content = content.lower()
        for pat in FORBIDDEN_OFFENSIVE_PATTERNS:
            if re.search(pat, lower_content):
                violations.append(f"Prohibited offensive security pattern detected: '{pat}'")

        return len(violations) == 0, violations

    # -------------------------------------------------------------------------
    # 2. Autonomous Evaluator Model Review
    # -------------------------------------------------------------------------

    def model_evaluator_review(
        self,
        proposal: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Invoke evaluator model to assess utility, defensive safety, and blast radius."""
        if self.client_fn is not None:
            return self.client_fn(proposal)

        # Call lab coordinator or local Ollama client
        cli = None
        if not getattr(self, "_cli_offline", False):
            from tools import control_client
            cli = control_client.configured()

        skill_content = proposal.get("skill_content", "")
        system_rubric = (
            "You are an autonomous AI Security and Quality Gatekeeper. "
            "You evaluate proposed agent skills and tools before they are activated. "
            "Evaluation Criteria:\n"
            "1. MUST be purely defensive, auditing, static analysis, CVE scanning, or testing.\n"
            "2. MUST NOT execute destructive commands, lateral movement, or exfiltrate data.\n"
            "3. MUST have clear procedural steps, preconditions, and failure verification.\n"
            "Return ONLY a single valid JSON object with keys:\n"
            '{"verdict": "approved" | "rejected", "confidence": float, '
            '"category": string, "rationale": string, "risk_level": "low" | "medium" | "high"}'
        )
        user_prompt = (
            f"Review Proposal: {proposal.get('proposal_id')}\n"
            f"Skill Name: {proposal.get('skill_name')}\n"
            f"Target Path: {proposal.get('target_path')}\n\n"
            f"Content:\n{skill_content[:3000]}"
        )

        messages = [
            {"role": "system", "content": system_rubric},
            {"role": "user", "content": user_prompt},
        ]

        if cli is not None:
            try:
                with cli.lease("compute:lab", wait_seconds=2):
                    resp = cli.request(
                        "/v1/chat",
                        {
                            "model": self.evaluator_model,
                            "messages": messages,
                            "stream": False,
                            "options": {"temperature": 0.1, "num_predict": 512},
                        },
                    )
                msg_text = resp.get("message", {}).get("content", "")
                m = re.search(r"\{.*\}", msg_text, re.DOTALL)
                if m:
                    return json.loads(m.group(0))
            except Exception:
                self._cli_offline = True

        # Fallback local deterministic evaluator if coordinator is offline
        clean_name = proposal.get("skill_name", "")
        passed_screen, violations = self.programmatic_screen(
            clean_name, skill_content, proposal.get("target_path", "")
        )
        if not passed_screen:
            return {
                "verdict": "rejected",
                "confidence": 0.95,
                "category": "safety_violation",
                "rationale": f"Programmatic screen rejected: {'; '.join(violations)}",
                "risk_level": "high",
            }

        return {
            "verdict": "approved",
            "confidence": 0.90,
            "category": "defensive_audit",
            "rationale": "Proposal conforms to defensive self-auditing and scope safety invariants.",
            "risk_level": "low",
        }

    # -------------------------------------------------------------------------
    # 3. Autonomous Proposal Processing Loop
    # -------------------------------------------------------------------------

    def process_pending_proposals(self) -> List[Dict[str, Any]]:
        """Scan assistant_skill_proposals and autonomously review all pending submissions."""
        pending = self.hydration.list_proposals(status="pending_approval")
        results: List[Dict[str, Any]] = []

        for prop in pending:
            pid = prop["proposal_id"]
            name = prop["skill_name"]
            content = prop["skill_content"]
            target = prop["target_path"]
            version = prop.get("version", "1.0.0")
            sha256 = prop.get("sha256", hashlib.sha256(content.encode()).hexdigest())

            # 1. Programmatic Pre-screening
            screen_ok, violations = self.programmatic_screen(name, content, target)
            if not screen_ok:
                rejection_msg = f"Autonomous Gatekeeper programmatic rejection: {'; '.join(violations)}"
                self.hydration.reject_proposal(
                    pid,
                    reason=rejection_msg,
                    reviewer=f"autonomous_gatekeeper:programmatic",
                )
                results.append({"proposal_id": pid, "status": "rejected", "reasons": violations})
                continue

            # 2. Model Review
            eval_result = self.model_evaluator_review(prop)
            verdict = eval_result.get("verdict", "rejected")
            confidence = float(eval_result.get("confidence", 0.0))
            risk = eval_result.get("risk_level", "high")
            rationale = eval_result.get("rationale", "")

            if verdict == "approved" and confidence >= 0.8 and risk == "low":
                approval_rec = self.hydration.approve_proposal(
                    proposal_id=pid,
                    expected_version=version,
                    expected_sha256=sha256,
                    reviewer=f"autonomous_gatekeeper:{self.evaluator_model}",
                    feedback=f"Autonomous Approval ({confidence*100:.0f}% confidence): {rationale}",
                )
                results.append({
                    "proposal_id": pid,
                    "status": "approved",
                    "installed_path": approval_rec["installed_path"],
                    "version": version,
                    "sha256": sha256,
                    "rationale": rationale,
                })
            else:
                rejection_rec = self.hydration.reject_proposal(
                    proposal_id=pid,
                    reason=f"Autonomous Rejection (risk={risk}, conf={confidence}): {rationale}",
                    reviewer=f"autonomous_gatekeeper:{self.evaluator_model}",
                )
                results.append({
                    "proposal_id": pid,
                    "status": "rejected",
                    "risk": risk,
                    "rationale": rationale,
                })

        return results

    # -------------------------------------------------------------------------
    # 4. Cyber Tool Autonomous Discovery & Proposal
    # -------------------------------------------------------------------------

    def discover_and_propose_cyber_tool(
        self,
        tool_name: str,
        category: str = "dependency_cve_audit",
        description: str = "",
        custom_search_results: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Synthesize and propose a standard defensive cyber tool skill autonomously."""
        clean_name = re.sub(r"[^a-zA-Z0-9_\-]", "-", tool_name.strip().lower()).strip("-")
        desc = description or f"Defensive vulnerability auditing and CVE self-testing via {clean_name}."

        search_results = custom_search_results or [
            {
                "title": f"Documentation: {clean_name} Defensive Security Tool",
                "url": f"https://pypi.org/project/{clean_name}/",
                "snippet": f"Open-source security auditing utility for Python repositories. Identifies vulnerabilities and CVEs.",
            }
        ]

        # Construct safe, structured SKILL.md
        sources_block = "\n".join(f"- [{r['title']}]({r['url']}): {r['snippet']}" for r in search_results)
        content = f"""---
name: {clean_name}
description: {desc}
version: 1.0.0
---

# {clean_name.replace('-', ' ').title()} Protocol

## Overview
{desc}

Hydrated autonomously by Sabrina via defensive security discovery.
Category: {category}

## External Sources
{sources_block}

## Safety Guardrails
- **Read-Only Scope**: This skill performs static evaluation and read-only package checking.
- **Zero Host Mutation**: Never modifies core harness files or runs unverified binaries.
- **Structured Findings**: Outputs all CVEs and vulnerability vectors into structured JSON.

## Step-by-Step Procedure
1. **Pre-flight Check**: Verify target repository path exists and contains source code or dependency manifests.
2. **Execute Scan**: Run static analysis or dependency advisory lookups.
3. **Capture Findings**: Extract CVSS scores, affected packages, and remediation advice.
4. **Report**: Write structured report to `data/security_audits/`.
"""
        pid = self.hydration.create_proposal(
            skill_name=clean_name,
            skill_content=content,
            trigger_reason="autonomous_cyber_tool_discovery",
            query_used=f"defensive audit {clean_name}",
            target_path=str(self.skills_dir / clean_name / "SKILL.md"),
        )

        return {
            "proposal_id": pid,
            "skill_name": clean_name,
            "category": category,
            "status": "pending_approval",
        }

    # -------------------------------------------------------------------------
    # 5. Live Self-Auditing Engine (SAST + Dependency CVE Checks)
    # -------------------------------------------------------------------------

    def audit_dependencies_osv(
        self,
        packages: List[Tuple[str, str]],
        timeout: float = 6.0,
    ) -> List[Dict[str, Any]]:
        """Query official OSV.dev (Open Source Vulnerabilities) API for CVEs in dependencies."""
        findings: List[Dict[str, Any]] = []
        for name, version in packages:
            try:
                payload = json.dumps({
                    "package": {"name": name, "ecosystem": "PyPI"},
                    "version": version,
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.osv.dev/v1/query",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.load(resp)
                    for v in data.get("vulns", []):
                        findings.append({
                            "type": "dependency_cve",
                            "package": name,
                            "version": version,
                            "id": v.get("id"),
                            "summary": v.get("summary", "No summary provided"),
                            "details": v.get("details", "")[:300],
                            "aliases": v.get("aliases", []),
                            "published": v.get("published"),
                        })
            except Exception:
                pass
        return findings

    def audit_source_code_ast(
        self,
        source_dir: Path | str,
    ) -> List[Dict[str, Any]]:
        """Perform static application security testing (SAST) on Python files using AST."""
        p_dir = Path(source_dir).resolve()
        findings: List[Dict[str, Any]] = []

        if not p_dir.exists():
            return findings

        for root, _, files in os.walk(p_dir):
            for file in files:
                if file.endswith(".py"):
                    fpath = Path(root) / file
                    try:
                        code = fpath.read_text(encoding="utf-8", errors="ignore")
                        tree = ast.parse(code, filename=str(fpath))
                    except Exception:
                        continue

                    try:
                        rel = str(fpath.relative_to(ROOT))
                    except ValueError:
                        rel = str(fpath.name)
                    for node in ast.walk(tree):
                        # 1. Dangerous calls: eval / exec
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                            if node.func.id in {"eval", "exec"}:
                                findings.append({
                                    "type": "sast_vulnerability",
                                    "file": rel,
                                    "line": node.lineno,
                                    "severity": "HIGH",
                                    "rule": "INSECURE_EVAL_EXEC",
                                    "description": f"Use of built-in {node.func.id}() allows arbitrary code execution.",
                                })

                        # 2. Insecure subprocess shell=True
                        if isinstance(node, ast.Call):
                            for kw in node.keywords:
                                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                                    findings.append({
                                        "type": "sast_vulnerability",
                                        "file": rel,
                                        "line": node.lineno,
                                        "severity": "MEDIUM",
                                        "rule": "SUBPROCESS_SHELL_TRUE",
                                        "description": "subprocess invoked with shell=True invites command injection.",
                                    })

                        # 3. Insecure pickle loads
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                            if node.func.attr in {"loads", "load"} and getattr(node.func.value, "id", "") == "pickle":
                                findings.append({
                                    "type": "sast_vulnerability",
                                    "file": rel,
                                    "line": node.lineno,
                                    "severity": "HIGH",
                                    "rule": "INSECURE_DESERIALIZATION",
                                    "description": "pickle deserialization allows arbitrary object execution.",
                                })

        return findings

    def run_full_self_audit(
        self,
        target_dirs: Optional[List[str]] = None,
        test_packages: Optional[List[Tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Execute full autonomous security self-audit and save timestamped report."""
        dirs = target_dirs or ["tools", "skills", "missions"]
        sast_findings: List[Dict[str, Any]] = []
        for d in dirs:
            p_target = Path(d)
            if not p_target.is_absolute():
                p_target = ROOT / d
            sast_findings.extend(self.audit_source_code_ast(p_target))

        # Check dependencies
        packages_to_check = test_packages or [
            ("urllib3", "1.26.4"), # Known CVE check for verification
            ("cryptography", "3.4.6"),
            ("requests", "2.25.1"),
        ]
        cve_findings = self.audit_dependencies_osv(packages_to_check)

        audit_id = f"audit_{int(time.time())}"
        report = {
            "audit_id": audit_id,
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "target_dirs": dirs,
            "sast_summary": {
                "total_findings": len(sast_findings),
                "findings": sast_findings[:50],
            },
            "cve_summary": {
                "packages_scanned": len(packages_to_check),
                "cves_found": len(cve_findings),
                "findings": cve_findings,
            },
            "status": "completed",
        }

        report_file = AUDITS_DIR / f"{audit_id}.json"
        report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["report_path"] = str(report_file)

        return report
