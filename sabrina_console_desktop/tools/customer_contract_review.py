"""Internal contract issue spotting for authorized security-service engagements.

This module does not provide legal approval or legal advice. It creates a
deterministic review checklist so an operator and qualified counsel can inspect
customer-provided MSA, NDA, SOW and rules-of-engagement language.
"""
from __future__ import annotations

import re
from typing import Any

from tools.messaging_store import digest


CLAUSE_PATTERNS = {
    "written_authorization": r"\b(?:written authorization|authorized representative|permission to test|authoriz(?:e|es|ed) testing)\b",
    "scope_and_assets": r"\b(?:in.scope|scope of (?:work|services)|target(?:s| assets?)?|domain(?:s)?|application(?:s)?|api(?:s)?)\b",
    "prohibited_methods": r"\b(?:out.of.scope|excluded|prohibited|must not|denial of service|social engineering|destructive testing)\b",
    "testing_window": r"\b(?:testing window|start date|end date|test period|maintenance window)\b",
    "emergency_contacts": r"\b(?:emergency contact|escalation contact|stop testing|kill switch|incident contact)\b",
    "safe_harbor": r"\b(?:safe harbor|good.faith security research|will not pursue|authorized activity)\b",
    "confidentiality": r"\b(?:confidential(?:ity| information)?|non.disclosure|\bnda\b)\b",
    "data_handling": r"\b(?:data handling|data retention|retain|delet(?:e|ion)|evidence storage|personal data|customer data)\b",
    "payment_terms": r"\b(?:payment terms|net \d+|invoice|purchase order|fees?)\b",
    "liability_limit": r"\b(?:limitation of liability|liability cap|aggregate liability|fees paid)\b",
    "indemnity": r"\b(?:indemnif(?:y|ication)|hold harmless|defend against)\b",
    "insurance": r"\b(?:insurance|errors and omissions|cyber liability|professional liability)\b",
    "ip_and_tools": r"\b(?:intellectual property|work product|background (?:ip|technology)|pre.existing tools|license)\b",
    "termination": r"\b(?:termination|terminate|cancellation|cancel)\b",
    "governing_law": r"\b(?:governing law|jurisdiction|venue|dispute resolution|arbitration)\b",
    "subcontractors": r"\b(?:subcontractor|subprocessor|third.party service provider)\b",
    "publicity": r"\b(?:publicity|customer name|logo|case study|press release)\b",
}


RISK_PATTERNS = (
    (
        "unlimited_liability",
        "high",
        r"\b(?:unlimited liability|liability shall not be limited|without limitation as to amount|no limitation of liability)\b",
        "Have qualified counsel review the liability allocation and any cap before approval.",
    ),
    (
        "broad_indemnity",
        "high",
        r"\bindemnif(?:y|ication).{0,180}\b(?:any and all|all claims|regardless of|arising in any way)\b",
        "Identify the claims covered, fault standard, exclusions, procedure and monetary boundary.",
    ),
    (
        "security_result_guarantee",
        "high",
        r"\b(?:guarantee|warrant).{0,140}\b(?:no vulnerabilities|all vulnerabilities|completely secure|free from vulnerabilities)\b",
        "Replace outcome guarantees with defined professional services, methods, limitations and deliverables.",
    ),
    (
        "open_ended_testing_scope",
        "high",
        r"\b(?:all systems|any systems|all assets|any asset|as requested from time to time|without further authorization)\b",
        "Require an explicit asset list, exclusions, test window and written change control.",
    ),
    (
        "background_ip_assignment",
        "high",
        r"\b(?:work made for hire|assign.{0,100}all.{0,60}(?:intellectual property|tools|technology)|ownership.{0,100}pre.existing)\b",
        "Separate customer deliverables from pre-existing methods, tools, templates and general know-how.",
    ),
    (
        "payment_contingent_on_security_result",
        "medium",
        r"\bpayment.{0,160}\b(?:no vulnerabilities|successful remediation|customer satisfaction|acceptance in sole discretion)\b",
        "Tie payment to objective delivery and acceptance mechanics rather than an impossible security outcome.",
    ),
    (
        "destructive_or_disruptive_authority",
        "high",
        r"\b(?:denial of service|destructive testing|delete production data|exfiltrate production data)\b.{0,120}\b(?:allowed|permitted|authorized)\b",
        "Require explicit technique-level approval, safety controls and a separate execution gate.",
    ),
    (
        "publicity_without_consent",
        "medium",
        r"\b(?:may use|right to use).{0,100}\b(?:name|logo|trademark).{0,100}\b(?:without|no) (?:prior )?(?:written )?consent\b",
        "Require explicit approval before either party uses the other party's name, logo or findings.",
    ),
)


REVIEW_QUESTIONS = {
    "written_authorization": "Where does the customer grant written authority for the exact testing activity?",
    "scope_and_assets": "Which exact assets, accounts, environments and workflows are in scope?",
    "prohibited_methods": "Which techniques, assets and operating conditions are expressly excluded?",
    "testing_window": "What start, stop and blackout windows apply?",
    "emergency_contacts": "Who can pause testing, and how are urgent findings escalated?",
    "safe_harbor": "Does the agreement protect good-faith activity performed within the signed scope?",
    "confidentiality": "How are customer information, findings and disclosures protected?",
    "data_handling": "What evidence may be collected, where may it be stored, and when must it be deleted?",
    "payment_terms": "What objective invoicing, payment and acceptance terms apply?",
    "liability_limit": "What liability cap and exclusions apply to each party?",
    "indemnity": "What indemnity obligations, procedures and boundaries apply?",
    "insurance": "Are insurance limits and certificate requirements defined and achievable?",
    "ip_and_tools": "Who owns the report, and how are pre-existing tools and methods protected?",
    "termination": "What happens to fees, data and work product if the engagement ends early?",
    "governing_law": "Which law, venue and dispute process govern the agreement?",
    "subcontractors": "May approved cloud or specialist providers be used, and under what safeguards?",
    "publicity": "May either party use names, logos, findings or a case study, and only with whose consent?",
}


def _evidence_excerpt(text: str, match: re.Match[str], radius: int = 90) -> str:
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    return " ".join(text[start:end].split())[:300]


def build_contract_issue_review(contract_text: str) -> dict[str, Any]:
    if not isinstance(contract_text, str) or not contract_text.strip():
        raise ValueError("Contract review requires customer-provided text")
    text = contract_text.strip()
    clauses = {
        clause: bool(re.search(pattern, text, re.I | re.S))
        for clause, pattern in CLAUSE_PATTERNS.items()
    }
    risks = []
    for risk_id, severity, pattern, question in RISK_PATTERNS:
        match = re.search(pattern, text, re.I | re.S)
        if match:
            risks.append({
                "id": risk_id,
                "severity": severity,
                "evidence_excerpt": _evidence_excerpt(text, match),
                "review_prompt": question,
            })
    unresolved = [clause for clause, present in clauses.items() if not present]
    questions = [REVIEW_QUESTIONS[clause] for clause in unresolved]
    questions.extend(risk["review_prompt"] for risk in risks)
    return {
        "version": 1,
        "internal_only": True,
        "source_sha256": digest(text),
        "source_characters": len(text),
        "clause_presence": clauses,
        "unresolved_or_delegated_topics": unresolved,
        "risk_flags": risks,
        "review_questions": list(dict.fromkeys(questions)),
        "nonnegotiable_execution_controls": [
            "written_authorization",
            "explicit_asset_scope",
            "prohibited_methods",
            "emergency_stop_contacts",
            "data_handling",
            "separate_operator_execution_approval",
        ],
        "interpretation_limit": (
            "Pattern-based issue spotting only. Absence does not prove a missing term because another document "
            "may control. Qualified counsel and the operator must review the complete agreement set."
        ),
        "legal_review_required": True,
        "contract_approval_authorized": False,
        "customer_reply_authorized": False,
        "testing_authorized": False,
    }
