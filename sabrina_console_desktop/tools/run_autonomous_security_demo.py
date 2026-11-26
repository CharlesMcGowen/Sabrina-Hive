"""Live Demonstration of Sabrina Autonomous Security Gatekeeper & Self-Auditor.

1. Autonomously discovers and proposes defensive security tools (e.g. CVE and vulnerability scanners).
2. Autonomous Security Gatekeeper evaluates proposals without human intervention, enforcing
   strict programmatic safety invariants and read-only boundaries.
3. Live Self-Auditing Engine scans local codebase (SAST AST) and checks dependencies against
   the official OSV (Open Source Vulnerabilities) CVE database.
4. Produces immutable JSON audit reports in Sabrina/data/security_audits/.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

# Ensure Sabrina root is in python path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sabrina_autonomous_gatekeeper import AutonomousSecurityGatekeeper


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    print("=" * 70, flush=True)
    print("SABRINA AUTONOMOUS SECURITY GATEKEEPER & DEFENSIVE AUDIT DEMONSTRATION", flush=True)
    print("=" * 70, flush=True)

    gatekeeper = AutonomousSecurityGatekeeper()

    # Step 1: Autonomous Cyber Tool Discovery & Proposal
    tool_name = "python-dependency-cve-checker"
    print(f"\n[1] Sabrina discovering cyber tool: '{tool_name}'...")
    proposal_info = gatekeeper.discover_and_propose_cyber_tool(
        tool_name=tool_name,
        category="dependency_cve_audit",
        description="Autonomous verification of Python packages against the Open Source Vulnerabilities database.",
    )
    print(f"    -> Proposal created: ID={proposal_info['proposal_id']}")
    print(f"    -> Category: {proposal_info['category']}")
    print(f"    -> Status: {proposal_info['status']}")

    # Step 2: Autonomous Model Gatekeeper Review Loop
    print("\n[2] Executing Autonomous Gatekeeper evaluation (no human in the loop)...")
    results = gatekeeper.process_pending_proposals()
    print(f"    -> Evaluated {len(results)} pending proposal(s):")
    for res in results:
        print(f"       * Proposal: {res.get('proposal_id')}")
        print(f"         Verdict: {res.get('status').upper()}")
        if res.get('status') == 'approved':
            print(f"         Installed Path: {res.get('installed_path')}")
            print(f"         Rationale: {res.get('rationale')}")
        else:
            print(f"         Risk: {res.get('risk')}")
            print(f"         Rationale: {res.get('rationale')}")

    # Step 3: Run Full Self-Audit
    print("\n[3] Sabrina executing full defensive self-audit (SAST AST + Live OSV CVE query)...")
    audit_report = gatekeeper.run_full_self_audit(
        target_dirs=["tools"],
        test_packages=[
            ("urllib3", "1.26.4"),
            ("jinja2", "2.11.2"),
            ("requests", "2.25.1"),
        ],
    )

    print(f"    -> Audit ID: {audit_report['audit_id']}")
    print(f"    -> Timestamp: {audit_report['timestamp']}")
    print(f"    -> Report Path: {audit_report['report_path']}")
    print(f"    -> SAST Total Findings: {audit_report['sast_summary']['total_findings']}")
    print(f"    -> CVE Packages Scanned: {audit_report['cve_summary']['packages_scanned']}")
    print(f"    -> CVEs Detected: {audit_report['cve_summary']['cves_found']}")

    if audit_report['cve_summary']['findings']:
        print("\n[4] Sample Detected CVE / Advisory Findings:")
        for vuln in audit_report['cve_summary']['findings'][:5]:
            cve_id = vuln.get("id")
            pkg = vuln.get("package")
            ver = vuln.get("version")
            summary = vuln.get("summary", "")[:80]
            print(f"       * [{cve_id}] {pkg}=={ver}: {summary}...")

    print("\n" + "=" * 70)
    print("DEMONSTRATION COMPLETED SUCCESSFULLY")
    print("=" * 70)


if __name__ == "__main__":
    main()
