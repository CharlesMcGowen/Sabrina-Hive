#!/usr/bin/env python3
"""
tools/frontier_rsi_overseer.py
==============================
Frontier Agentic RSI Overseer for Sabrina.

Solves the Recursive Self-Improvement (RSI) stall by providing an autonomous,
frontier-model-powered evaluator and cryptographic approval gate:
1. Evaluates candidate skill proposals produced by DualAgentLoop / resource_adapt.
2. Validates syntax of any embedded Python code blocks or companion scripts.
3. Scores procedures against safety, idempotency, and operational effectiveness rubrics.
4. Executes cryptographic SHA-256 + version approvals through SabrinaSkillHydration.
5. Emits structured evaluation audits into SQLite and Generative UI telemetry.
"""

import os
import sys
import json
import re
import time
import hashlib
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.sabrina_skill_hydration import SabrinaSkillHydration
from tools.oracle_providers import UnifiedOracleHub


def _review_json(text):
    match = re.search(r'\{.*\}', text, re.DOTALL)
    value = json.loads(match.group(0)) if match else None
    if (not isinstance(value, dict) or value.get('decision') not in {'APPROVE', 'REJECT'}
            or type(value.get('score')) is not int or not 1 <= value['score'] <= 10
            or not isinstance(value.get('rationale'), str) or not value['rationale'].strip()):
        raise ValueError('Incomplete independent-review result')
    return value


class FrontierRsiOverseer:
    """
    Autonomous Frontier Agentic Overseer for Recursive Self-Improvement (RSI).
    Bridges the capability gap where local models cannot objectively grade or
    safely approve their own skill adaptations.
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        skills_dir: Optional[Path | str] = None,
        oracle_hub: Optional[UnifiedOracleHub] = None,
    ):
        self.repo_root = REPO_ROOT
        self.db_path = Path(db_path) if db_path else (self.repo_root / "data" / "sabrina_assistant.sqlite3")
        self.skills_dir = Path(skills_dir) if skills_dir else (self.repo_root / "skills")
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.hydration = SabrinaSkillHydration(db_path=self.db_path, skills_dir=self.skills_dir)
        self.oracle_hub = oracle_hub or UnifiedOracleHub()

    def _query_disjoint_local_auditor(self, eval_prompt: str, authored_by: str = "") -> Tuple[Optional[str], str]:
        """Audit candidate skill using a disjoint local model family (IBM Granite / Meta Llama, never Qwen-on-Qwen)."""
        try:
            from tools.control_client import configured, inference_wait_budget
            client = configured()
            if client is None:
                return None, "coordinator_unavailable"
            st = client.request("/v1/status")
            if any(l.get("resource") == "compute:lab" and l.get("state") in ("held", "uncertain") for l in st.get("leases", [])):
                return None, "compute_lab_busy"
            if any(r.get("state") in ("queued", "running") for r in st.get("requests", [])):
                return None, "compute_lab_busy"
            # Disjoint family selection per AGENTS.md (skip sabrina-jury-prosecutor:0.2)
            auditor_candidates = ["sabrina-jury-granite:0.2", "llama3.1:8b", "granite3.1-dense:8b"]
            author_lower = (authored_by or "").lower()
            if "granite" in author_lower:
                auditor_candidates = ["llama3.1:8b", "sabrina-jury-llama:0.2"]
            elif "llama" in author_lower:
                auditor_candidates = ["sabrina-jury-granite:0.2", "granite3.1-dense:8b"]
            client.client, client.project = "frontier-rsi-overseer", "disjoint-skill-audit"
            for model_id in auditor_candidates:
                try:
                    with inference_wait_budget(20):
                        resp = client.infer(
                            "/api/chat",
                            {
                                "model": model_id,
                                "stream": False,
                                "think": False,
                                "options": {"temperature": 0.1, "num_predict": 600},
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": (
                                            "You are an independent disjoint-family RSI Skill Auditor. "
                                            "Return ONLY valid JSON matching the requested schema."
                                        ),
                                    },
                                    {"role": "user", "content": eval_prompt},
                                ],
                            },
                            timeout=25,
                            device="auto",
                            task_label=f"disjoint_rsi_audit:{model_id}",
                        )
                    txt = (resp.get("message") or {}).get("content") or ""
                    if "{" in txt:
                        return txt, f"local_disjoint:{model_id}"
                except Exception:
                    continue
        except Exception as exc:
            return None, 'disjoint_local_unavailable:' + type(exc).__name__
        return None, "disjoint_local_unavailable"

    def evaluate_proposal(self, proposal: Dict[str, Any]) -> Dict[str, Any]:
        """
        Evaluate a single proposal against syntax, safety, and agentic quality rubrics.
        Returns an evaluation report dict.
        """
        proposal_id = proposal["proposal_id"]
        skill_name = proposal["skill_name"]
        content = proposal.get("skill_content", "")
        version = proposal.get("version", "1.0.0")
        sha256_hash = proposal.get("sha256") or hashlib.sha256(content.encode("utf-8")).hexdigest()
        if len(content.encode('utf-8')) > 20000:
            return {"proposal_id": proposal_id, "skill_name": skill_name, "version": version,
                    "sha256": sha256_hash, "passed": False, "decision": "DEFER", "score": 0,
                    "rationale": "Skill exceeds review context budget; split the review rather than truncate it."}

        # 1. Structural & Frontmatter Check
        frontmatter_match = re.search(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        has_frontmatter = bool(frontmatter_match)
        has_name = bool(re.search(r"(?m)^name:\s*\S+", content))
        has_description = bool(re.search(r"(?m)^description:\s*\S+", content))
        structure_valid = has_frontmatter and has_name and has_description

        # 2. Syntax Validation of Embedded Python Code Blocks
        syntax_errors = []
        py_blocks = re.findall(r"```python\s*(.*?)\s*```", content, re.DOTALL)
        for i, code in enumerate(py_blocks):
            try:
                compile(code, f"<skill_code_block_{i}>", "exec")
            except Exception as e:
                syntax_errors.append(f"Block {i}: {e}")

        # 3. Static Safety & Autonomous Security Gatekeeper Policy
        safety_violations = []
        if re.search(r"(AIzaSy[0-9A-Za-z_-]{33}|sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{20,})", content):
            safety_violations.append("Contains hardcoded API keys or personal access tokens.")
        if re.search(r"\b(rm\s+-rf\s+/(?:\s|$)|format\s+[A-Za-z]:)", content):
            safety_violations.append("Contains dangerous unrestricted filesystem destruction commands.")
        try:
            from tools.sabrina_autonomous_gatekeeper import AutonomousSecurityGatekeeper
            gk = AutonomousSecurityGatekeeper(db_path=self.db_path, skills_dir=self.skills_dir)
            target_path = proposal.get("target_path") or f"skills/{skill_name}/SKILL.md"
            gk_ok, gk_violations = gk.programmatic_screen(skill_name, content, target_path)
            if not gk_ok:
                for gv in gk_violations:
                    if "YAML frontmatter" not in gv:
                        safety_violations.append(gv)
        except Exception as exc:
            return {"proposal_id": proposal_id, "skill_name": skill_name, "version": version,
                    "sha256": sha256_hash, "passed": False, "decision": "DEFER", "score": 0,
                    "rationale": 'Programmatic screening unavailable: ' + type(exc).__name__}

        # Check preliminary blockers
        if not structure_valid:
            return {
                "proposal_id": proposal_id,
                "skill_name": skill_name,
                "version": version,
                "sha256": sha256_hash,
                "passed": False,
                "decision": "REJECT",
                "score": 2,
                "rationale": "Missing valid YAML frontmatter (name and description required).",
                "syntax_valid": False,
                "safety_valid": True,
            }

        if syntax_errors:
            return {
                "proposal_id": proposal_id,
                "skill_name": skill_name,
                "version": version,
                "sha256": sha256_hash,
                "passed": False,
                "decision": "REJECT",
                "score": 3,
                "rationale": f"Python code block syntax errors: {'; '.join(syntax_errors)}",
                "syntax_valid": False,
                "safety_valid": True,
            }

        if safety_violations:
            return {
                "proposal_id": proposal_id,
                "skill_name": skill_name,
                "version": version,
                "sha256": sha256_hash,
                "passed": False,
                "decision": "REJECT",
                "score": 0,
                "rationale": f"Safety policy violations: {'; '.join(safety_violations)}",
                "syntax_valid": True,
                "safety_valid": False,
            }

        # 4. Frontier LLM or Disjoint-Family Local Auditor Quality Evaluation
        eval_prompt = (
            f"You are the Lead Agentic Systems Architect and RSI Evaluator for an autonomous AI assistant.\n"
            f"Review the following proposed skill ({skill_name} v{version}):\n\n"
            f"```markdown\n{content}\n```\n\n"
            f"Evaluate against this rubric:\n"
            f"1. Operational Specificity: Are instructions deterministic, concrete, and directly executable?\n"
            f"2. Failure Recovery: Are error handling, fallback steps, and diagnostics explicit?\n"
            f"3. Idempotency: Can these commands/steps be executed repeatedly without causing corrupt state?\n\n"
            f"Reply with a strictly valid JSON object matching this schema:\n"
            f"{{\n"
            f'  "decision": "APPROVE" | "REJECT",\n'
            f'  "score": <integer from 1 to 10>,\n'
            f'  "rationale": "<concise evaluation summary>",\n'
            f'  "strengths": ["<strength 1>", ...],\n'
            f'  "weaknesses": ["<weakness 1>", ...]\n'
            f"}}"
        )

        score = 8
        decision = "APPROVE"
        rationale = "Candidate procedure meets agentic operational criteria."
        strengths = []
        weaknesses = []
        model_used = "deterministic_gatekeeper"
        llm_parsed = False

        try:
            frontier_response, candidate_model = self.oracle_hub.query(
                prompt=eval_prompt,
                system="You are an expert autonomous agent evaluation and verification engine. Always return valid JSON only.",
                preferred="auto",
                response_format="json",
            )
            if frontier_response and "{" in frontier_response:
                m_json = re.search(r"\{.*\}", frontier_response, re.DOTALL)
                if m_json:
                    parsed = _review_json(frontier_response)
                    score = int(parsed.get("score", 7))
                    decision = str(parsed.get("decision", "APPROVE")).upper()
                    rationale = str(parsed.get("rationale", rationale))
                    strengths = parsed.get("strengths", [])
                    weaknesses = parsed.get("weaknesses", [])
                    model_used = candidate_model
                    llm_parsed = True
        except Exception as e:
            rationale = f"Oracle skipped ({e})"

        if not llm_parsed:
            local_resp, local_model = self._query_disjoint_local_auditor(
                eval_prompt, authored_by=str(proposal.get("reviewer") or proposal.get("trigger_reason") or "")
            )
            if local_resp and "{" in local_resp:
                try:
                    m_json = re.search(r"\{.*\}", local_resp, re.DOTALL)
                    if m_json:
                        parsed = _review_json(local_resp)
                        score = int(parsed.get("score", 7))
                        decision = str(parsed.get("decision", "APPROVE")).upper()
                        rationale = str(parsed.get("rationale", rationale))
                        strengths = parsed.get("strengths", [])
                        weaknesses = parsed.get("weaknesses", [])
                        model_used = local_model
                        llm_parsed = True
                except Exception:
                    pass
            if not llm_parsed:
                score = 0
                decision = "DEFER"
                model_used = f"review_unavailable:{local_model}"
                rationale = "No independent review returned. Structure and syntax alone do not qualify a skill."

        # This evaluator reviews prose/syntax; it does not execute independent tests.
        if decision == 'APPROVE' and (py_blocks or 'Candidate Adapter Patch Bundle' in content):
            decision = 'DEFER'
            rationale = 'Executable candidate requires independently verified behavioral tests before deployment. ' + rationale

        passed = (decision == "APPROVE" and score >= 7)
        return {
            "proposal_id": proposal_id,
            "skill_name": skill_name,
            "version": version,
            "sha256": sha256_hash,
            "passed": passed,
            "decision": decision if passed or decision == 'DEFER' else "REJECT",
            "score": score,
            "rationale": rationale,
            "model_used": model_used,
            "strengths": strengths,
            "weaknesses": weaknesses,
            "syntax_valid": True,
            "safety_valid": True,
            "evidence_level": "procedure_review_only",
            "behavioral_qualification": False,
        }

    def _list_actionable_pending(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        all_props = self.hydration.list_proposals()
        pending = [p for p in all_props if p.get("status") in ("pending_approval", "pending_review")]
        return pending[:limit] if limit is not None else pending

    def review_and_promote_pending(
        self, auto_approve: bool = True, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Scan pending proposals, evaluate them with frontier / disjoint-family oversight,
        and cryptographically promote approved ones (or mark failed ones rejected).
        """
        pending = self._list_actionable_pending(limit=limit)
        results = []
        for prop in pending:
            report = self.evaluate_proposal(prop)
            if auto_approve and report["passed"]:
                try:
                    self.hydration.approve_proposal(
                        proposal_id=report["proposal_id"],
                        expected_version=report["version"],
                        expected_sha256=report["sha256"],
                        reviewer=f"frontier_rsi_overseer:{report.get('model_used', 'auditor')}",
                        feedback=f"Autonomous RSI Approval (score {report['score']}/10, model: {report.get('model_used', 'auditor')}): {report['rationale']}",
                    )
                    report["promoted"] = True
                except Exception as e:
                    report["promoted"] = False
                    report["promotion_error"] = str(e)
            elif auto_approve and not report["passed"] and report.get('decision') != 'DEFER':
                try:
                    self.hydration.reject_proposal(
                        proposal_id=report["proposal_id"],
                        reason=f"Autonomous RSI Rejection (score {report['score']}/10): {report['rationale']}",
                        reviewer=f"frontier_rsi_overseer:{report.get('model_used', 'auditor')}",
                    )
                except Exception:
                    pass
                report["promoted"] = False
            elif auto_approve and report.get('decision') == 'DEFER':
                # Preserve the proposal and explanation without re-reviewing the same
                # unchanged blocker on every recurring reflection cycle.
                with sqlite3.connect(self.db_path) as db:
                    db.execute("UPDATE assistant_skill_proposals SET status='awaiting_validation', reviewer=?, review_feedback=?, updated=? WHERE proposal_id=? AND skill_content=?",
                               ('frontier_rsi_overseer:deferred', report['rationale'], time.time(),
                                prop['proposal_id'], prop.get('skill_content', '')))
                report['promoted'] = False
                report['awaiting_validation'] = True
            else:
                report["promoted"] = False
            results.append(report)
        return results

    def evaluate_and_promote(self, limit: int = 5, auto_approve: bool = True) -> Dict[str, Any]:
        """Structured summary wrapper invoked by UniversalRsiReflector."""
        results = self.review_and_promote_pending(auto_approve=auto_approve, limit=limit)
        approved_count = sum(1 for r in results if r.get("promoted"))
        rejected_count = sum(1 for r in results if not r.get("passed"))
        return {
            "evaluated_count": len(results),
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "results": results,
        }

    def audit_ledger(self) -> Dict[str, Any]:
        """Summarize all active and archived skills and proposals."""
        all_props = self.hydration.list_proposals()
        approved = [p for p in all_props if p.get("status") == "approved"]
        pending = [p for p in all_props if p.get("status") in ("pending_approval", "pending_review")]
        return {
            "total_proposals": len(all_props),
            "approved": len(approved),
            "pending": len(pending),
            "skills_installed": len([d for d in self.skills_dir.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name != "proposed"]),
            "proposals": all_props,
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sabrina Frontier RSI Overseer CLI")
    parser.add_argument("command", choices=["review", "audit", "status"], default="review", nargs="?")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate proposals without approving them")
    args = parser.parse_args()

    overseer = FrontierRsiOverseer()
    if args.command == "review":
        print(f"[FrontierRSI] Scanning for pending skill proposals...")
        results = overseer.review_and_promote_pending(auto_approve=not args.dry_run)
        if not results:
            print("[FrontierRSI] No pending proposals found.")
        for r in results:
            status_sym = "[PASS]" if r["passed"] else "[FAIL]"
            promoted_str = " -> PROMOTED" if r.get("promoted") else ""
            print(f"{status_sym} Proposal: {r['proposal_id']} ({r['skill_name']} v{r['version']}){promoted_str}")
            print(f"       Score: {r['score']}/10 | Model: {r.get('model_used')} | Rationale: {r['rationale']}")
    elif args.command in ("audit", "status"):
        audit = overseer.audit_ledger()
        print(f"[FrontierRSI Audit]")
        print(f"  Total Proposals:   {audit['total_proposals']}")
        print(f"  Approved:          {audit['approved']}")
        print(f"  Pending:           {audit['pending']}")
        print(f"  Installed Skills:  {audit['skills_installed']}")


if __name__ == "__main__":
    main()
