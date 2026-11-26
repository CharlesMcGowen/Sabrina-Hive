"""
tools/security_engineering_tool.py
─────────────────────────────────────────────────────────────────────────────
Autonomous Security Engineering Tool for Sabrina.
Enables Sabrina to autonomously select, synthesize, execute, and iterate on
live Python security validators for vulnerability hypotheses without human
script intervention.
"""

import json
import logging
import os
import re
import textwrap
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger("sabrina.security_engineering_tool")


def list_detection_rules() -> str:
    """
    List all detection rules and vulnerability hypotheses available in config/detection_rules.json.
    Returns a formatted summary that Sabrina can review to pick target rules for validation.
    """
    try:
        import sabrina_security_engineer as sse
        rules = sse.load_rules()
        if not rules:
            return "No detection rules found in config/detection_rules.json."
        
        summary_list = []
        for r in rules:
            rid = r.get("id", r.get("rule_id", "unknown"))
            if rid == "unknown":
                vtype_clean = re.sub(r"[^a-z0-9]+", "_", r.get("vuln_type", "unknown")[:30].lower()).strip("_")
                rid = f"rule_{vtype_clean}"
            summary_list.append({
                "rule_id": rid,
                "vuln_type": r.get("vuln_type", "unknown"),
                "severity": r.get("severity", "medium"),
                "endpoint_pattern": r.get("endpoint_pattern", r.get("endpoint", "/")),
                "method": r.get("method", "GET"),
                "description": r.get("notes", r.get("description", ""))
            })
        
        return json.dumps({
            "total_rules": len(summary_list),
            "rules": summary_list
        }, indent=2)
    except Exception as e:
        return f"Error listing detection rules: {e}"


def engineer_security_validator(
    target_url: str,
    rule_id: Optional[str] = None,
    hypothesis: Optional[str] = None,
    max_rounds: int = 4,
    engagement_gate = None
) -> str:
    """
    Autonomously engineer, compile, execute, and iterate on a security validator
    against target_url. Generates a formal finding report and injects verified
    findings into RAG.
    """
    if not target_url:
        return "Error: target_url is required."

    # A direct call must never turn a public-CVE or code-review task into live
    # traffic. The runtime supplies its one authoritative engagement gate.
    if engagement_gate is None:
        return "BLOCKED by Scope Gate: live validation requires an explicit authorized engagement gate."
    ok, reason = engagement_gate.check(target_url)
    if not ok:
        return f"BLOCKED by Scope Gate: {reason}"
    engagement_gate.throttle()

    import sabrina_security_engineer as sse
    
    selected_rule = None
    target_hypothesis = ""
    resolved_id = ""

    if rule_id:
        selected_rule = sse.get_rule(rule_id)
        if not selected_rule:
            # Try fuzzy search on rule ID or vuln_type
            for r in sse.load_rules():
                rid = r.get("id", r.get("rule_id", ""))
                if rid == rule_id or rule_id.lower() in rid.lower():
                    selected_rule = r
                    break
        
        if selected_rule:
            resolved_id = selected_rule.get("id", selected_rule.get("rule_id", rule_id))
            target_hypothesis = sse.rule_to_hypothesis(selected_rule, target_url)
        else:
            return f"Error: Rule '{rule_id}' not found in detection_rules.json. Use 'list_detection_rules' to see valid rule IDs."
    elif hypothesis:
        target_hypothesis = hypothesis.strip()
        slug = re.sub(r"[^a-z0-9]+", "_", target_hypothesis[:40].lower()).strip("_")
        resolved_id = f"hypo_{slug}"
    else:
        return "Error: You must provide either 'rule_id' or 'hypothesis' to engineer a security validator."

    out_name = f"{resolved_id}.py"
    
    # Save original max rounds and override if specified
    old_max = sse.MAX_ROUNDS
    sse.MAX_ROUNDS = min(max(1, max_rounds), 6)

    try:
        confirmed, code, output = sse.engineer_validator(
            hypothesis=target_hypothesis,
            target_url=target_url,
            out_name=out_name
        )

        report_path = sse.generate_report(
            hypothesis=target_hypothesis,
            code=code,
            result_output=output,
            confirmed=confirmed,
            target_url=target_url,
            rule_id=resolved_id
        )

        # If confirmed, persist into RAG and Blackboard
        rag_status = ""
        if confirmed:
            try:
                from tools.rag_knowledge import add_knowledge
                vuln_type = selected_rule.get("vuln_type", "Vulnerability") if selected_rule else "Vulnerability"
                add_knowledge(
                    topic=f"Confirmed Finding: {resolved_id} ({vuln_type})",
                    content=f"Target: {target_url}\nHypothesis: {target_hypothesis}\n\nValidator Code:\n```python\n{code[:1500]}\n```\n\nEvidence/Output:\n{output[:1000]}",
                    source_type="verified_worked"
                )
                rag_status = " Finding automatically committed to RAG knowledge base as verified_worked."
            except Exception as re:
                rag_status = f" (RAG update note: {re})"

            try:
                from urllib.parse import urlparse
                from tools.blackboard import BLACKBOARD, DiscoveredEndpoint
                parsed = urlparse(target_url)
                ep_path = parsed.path or "/"
                BLACKBOARD.mark_tested(ep_path, selected_rule.get("method", "GET") if selected_rule else "GET", resolved_id)
            except Exception:
                pass

        result_summary = {
            "rule_id": resolved_id,
            "target_url": target_url,
            "confirmed": confirmed,
            "status": "CONFIRMED_VULNERABILITY" if confirmed else "NOT_CONFIRMED",
            "validator_code_path": str(sse.TESTS_DIR / out_name),
            "report_path": str(report_path),
            "execution_preview": output[:1000] if output else "No output",
            "notes": f"Hypothesis validation finished with confirmed={confirmed}.{rag_status}"
        }

        return json.dumps(result_summary, indent=2)

    except Exception as e:
        return f"Error executing security validator loop: {e}"
    finally:
        sse.MAX_ROUNDS = old_max
