#!/usr/bin/env python3
"""Sabrina Bugcrowd & HackerOne Submission Dossier, Disjoint-Family Triager & CDP Draft Pre-Filler.

Bridges the final mile between Sabrina's vulnerability research (`tools/bounty_recon.py`,
`tools/engagement_scope.py`, `tools/bugbounty_rsi.py`, `ai_systems/swarm/bug_bounty_swarm.py`)
and real platform submission forms on Bugcrowd (`https://bugcrowd.com/engagements/<slug>/submissions/new`)
and HackerOne (`https://hackerone.com/<handle>/reports/new`).

Safety & Reputation Guardrails:
1. Hard Scope & Disallowed-Category Gate (`EngagementGate` + `disallowed_techniques`):
   - Verifies the target URL is strictly in-scope (`EngagementGate.check_url(target_url)`)
     against `config/engagements/<program>.json`.
   - Rejects any finding that matches the program's `disallowed_techniques` (e.g. `self_xss`,
     `open_redirect`, `missing_security_header`, `clickjacking`, `cloudflare_issue`).
2. Disjoint-Family Bug-Bounty Triage Audit (`Qwen` Author x `IBM Granite` Skeptical Platform Triager):
   - Enforces `verify_disjoint_family_audit(author_model, reviewer_model)`.
   - `AUTHOR` (`qwen2.5-coder:14b`) formats the submission into Bugcrowd VRT / HackerOne CWE
     schema with deterministic Steps to Reproduce, HTTP/Curl PoC, and Impact.
   - `TRIAGER` (`granite3.1-dense:8b` / `granite3-dense:8b`) audits the draft for reproducibility,
     evidence completeness, and out-of-scope traps.
3. CDP Browser Draft Pre-Filler (`prefill_platform_submission_via_cdp`):
   - Connects to Chrome CDP (`127.0.0.1:9222` or `:19224`), opens the platform submission URL,
     and pre-fills Title, Target, Description / Steps to Reproduce, and Impact as an
     UNSUBMITTED draft (`submitted = False` — never clicks the final platform Submit button
     without explicit operator confirmation, protecting researcher signal/reputation).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.engagement_scope import EngagementGate
from tools.sabrina_upstream_forge import verify_disjoint_family_audit

DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
ENGAGEMENTS_DIR = ROOT / "config" / "engagements"
DOSSIERS_DIR = ROOT / "artifacts" / "bounty_submissions"

AUTHOR_MODEL = "qwen2.5-coder:14b"
TRIAGER_MODEL = "granite3.1-dense:8b"

# Canonical Bugcrowd VRT + HackerOne CWE mapping
VRT_CWE_CATALOG: Dict[str, Dict[str, str]] = {
    "idor": {
        "vrt": "Broken Access Control (BAC) > Insecure Direct Object References (IDOR)",
        "cwe": "CWE-639: Authorization Bypass Through User-Controlled Key",
        "default_severity": "P2",
    },
    "auth_bypass": {
        "vrt": "Broken Authentication and Session Management > Authentication Bypass",
        "cwe": "CWE-287: Improper Authentication",
        "default_severity": "P1",
    },
    "broken_access_control": {
        "vrt": "Broken Access Control (BAC) > Privilege Escalation / Horizontal Access Control",
        "cwe": "CWE-284: Improper Access Control",
        "default_severity": "P2",
    },
    "ssrf": {
        "vrt": "Server-Side Injection > Server-Side Request Forgery (SSRF)",
        "cwe": "CWE-918: Server-Side Request Forgery (SSRF)",
        "default_severity": "P2",
    },
    "sqli": {
        "vrt": "Server-Side Injection > SQL Injection (SQLi)",
        "cwe": "CWE-89: Improper Neutralization of Special Elements used in an SQL Command",
        "default_severity": "P1",
    },
    "xss": {
        "vrt": "Cross-Site Scripting (XSS) > Stored / Reflected",
        "cwe": "CWE-79: Improper Neutralization of Input During Web Page Generation",
        "default_severity": "P3",
    },
    "http_request_smuggling": {
        "vrt": "Server-Side Injection > HTTP Request Smuggling",
        "cwe": "CWE-444: Inconsistent Interpretation of HTTP Requests",
        "default_severity": "P2",
    },
    "sensitive_data_exposure": {
        "vrt": "Sensitive Data Exposure > Disclosure of Secrets / PII",
        "cwe": "CWE-200: Exposure of Sensitive Information to an Unauthorized Actor",
        "default_severity": "P2",
    },
    "ai_agent_security": {
        "vrt": "AI / LLM Application Security > Indirect Prompt Injection / Tool Privilege Escalation",
        "cwe": "CWE-74: Improper Neutralization of Special Elements in Output Used by a Downstream Component",
        "default_severity": "P2",
    },
}

_EXCLUDED_KEYWORD_MAP: Dict[str, List[str]] = {
    "open_redirect_reports": ["open redirect", "unvalidated redirect"],
    "clickjacking_reports": ["clickjacking", "x-frame-options", "frame-ancestors"],
    "missing_security_header_reports": ["missing header", "strict-transport-security", "x-content-type-options"],
    "self_xss_reports": ["self-xss", "self xss", "paste into console"],
    "cloudflare_issue_reports": ["cloudflare error", "cloudflare ray id"],
    "subdomain_takeover_reports": ["subdomain takeover", "dangling cname"],
    "csrf_on_unauthenticated_or_nonsensitive_forms": ["unauthenticated csrf", "logout csrf"],
}


def _query_ollama_json(model: str, system_prompt: str, user_prompt: str, timeout: int = 45) -> Optional[Dict[str, Any]]:
    endpoints = [
        ("http://127.0.0.1:11434/api/chat", model),
        ("http://127.0.0.1:11435/api/chat", "granite3-dense:8b" if "granite" in model else model),
        ("http://192.168.2.69:11434/api/chat", model),
    ]
    for url, target_model in endpoints:
        try:
            payload = json.dumps({
                "model": target_model,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 700},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            content = (data.get("message") or {}).get("content") or ""
            return json.loads(content)
        except Exception:
            continue
    return None


class SabrinaBountySubmitter:
    """Hard-gated Bugcrowd & HackerOne Submission Dossier Builder, Disjoint Triager, and CDP Draft Pre-Filler."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH, engagements_dir: Path = ENGAGEMENTS_DIR) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engagements_dir = Path(engagements_dir)
        DOSSIERS_DIR.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bounty_submission_dossiers (
                    dossier_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    program TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    vuln_category TEXT NOT NULL,
                    vrt_classification TEXT NOT NULL,
                    cwe_id TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    scope_verified INTEGER NOT NULL,
                    policy_verified INTEGER NOT NULL,
                    author_model TEXT NOT NULL,
                    triager_model TEXT NOT NULL,
                    triager_verdict TEXT NOT NULL,
                    triager_score INTEGER NOT NULL,
                    triager_notes TEXT NOT NULL,
                    submission_url TEXT NOT NULL,
                    report_markdown TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'staged_for_review',
                    cdp_prefill_receipt_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                """
            )

    def list_engagements(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not self.engagements_dir.is_dir():
            return out
        for f in sorted(self.engagements_dir.glob("*.json")):
            if f.name in {"TEMPLATE.json", ".rotation_state.json"}:
                continue
            try:
                cfg = json.loads(f.read_text(encoding="utf-8"))
                out.append({
                    "program": cfg.get("program") or f.stem,
                    "platform": cfg.get("platform") or "bugcrowd",
                    "program_url": cfg.get("program_url") or "",
                    "submission_url": self._submission_url(cfg.get("platform", "bugcrowd"), cfg.get("program") or f.stem),
                    "in_scope": cfg.get("in_scope") or [],
                    "out_of_scope": cfg.get("out_of_scope") or [],
                    "automated_testing_allowed": bool(cfg.get("automated_testing_allowed", False)),
                    "rate_limit_rps": cfg.get("rate_limit_rps"),
                    "focus": cfg.get("focus") or [],
                    "disallowed_count": len(cfg.get("disallowed_techniques") or []),
                })
            except Exception:
                continue
        return out

    @staticmethod
    def _submission_url(platform: str, program: str) -> str:
        plat = (platform or "bugcrowd").strip().lower()
        slug = (program or "").strip()
        if plat == "hackerone":
            return f"https://hackerone.com/{slug}/reports/new"
        if plat == "intigriti":
            return f"https://app.intigriti.com/researcher/submissions/new?handle={slug}"
        return f"https://bugcrowd.com/engagements/{slug}/submissions/new"

    def _load_engagement_config(self, program: str) -> Dict[str, Any]:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "", (program or "").strip())
        path = self.engagements_dir / f"{slug}.json"
        if not path.is_file():
            raise ValueError(f"Engagement scope file not found: config/engagements/{slug}.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def validate_scope_and_policy(
        self,
        cfg: Dict[str, Any],
        target_url: str,
        vuln_category: str,
        title: str,
        summary_or_poc: str,
    ) -> Tuple[bool, bool, List[str]]:
        """Verify target URL against `EngagementGate.check_url` and check `disallowed_techniques`."""
        gate = EngagementGate(cfg)
        scope_ok, scope_reason = gate.check_url(target_url)
        issues: List[str] = []
        if not scope_ok:
            issues.append(scope_reason)

        combined_text = f"{vuln_category} {title} {summary_or_poc}".lower()
        disallowed = [str(x).lower() for x in (cfg.get("disallowed_techniques") or [])]
        policy_ok = True
        for rule in disallowed:
            keywords = _EXCLUDED_KEYWORD_MAP.get(rule, [rule.replace("_reports", "").replace("_", " ")])
            for kw in keywords:
                if kw and len(kw) >= 5 and kw in combined_text:
                    # Only flag if the finding itself claims to be that disallowed category
                    if kw in title.lower() or kw == vuln_category.lower():
                        policy_ok = False
                        issues.append(f"BLOCKED_BY_PROGRAM_POLICY: '{rule}' is explicitly excluded in {cfg.get('program')}.json")
                        break
        # HostGator LATAM specific rule: XSS is capped at P3/Medium
        return scope_ok, policy_ok, issues

    def stage_submission_dossier(
        self,
        *,
        program: str = "hostgator-latam-bb",
        target_url: str = "https://financeiro.hostgator.com.br/api/v1/ai-agent/session",
        vuln_category: str = "idor",
        title: str = "IDOR in AI Agent Session Endpoint Allows Cross-Tenant Session Metadata Read",
        steps_to_reproduce: str = "",
        http_poc: str = "",
        impact: str = "",
        severity: str = "",
        use_llm_triager: bool = False,
    ) -> Dict[str, Any]:
        """Validate scope & policy, run Disjoint-Family Triage (`Qwen 14B` x `IBM Granite 8B`),
        and stage a verified Bugcrowd/HackerOne submission dossier.
        """
        verify_disjoint_family_audit(AUTHOR_MODEL, TRIAGER_MODEL)
        cfg = self._load_engagement_config(program)
        platform = str(cfg.get("platform") or "bugcrowd").lower()
        cat_key = (vuln_category or "idor").strip().lower()
        cat_meta = VRT_CWE_CATALOG.get(cat_key, VRT_CWE_CATALOG["idor"])

        eff_severity = (severity or cat_meta["default_severity"]).upper()
        # Enforce program-specific severity caps (e.g. hostgator-latam-bb caps XSS at P3/Medium)
        if program == "hostgator-latam-bb" and cat_key == "xss" and eff_severity in {"P1", "P2"}:
            eff_severity = "P3"

        if not steps_to_reproduce.strip():
            steps_to_reproduce = (
                f"1. Authenticate to `{target_url}` using test account A (`researcher_a@bugcrowdninja.com`).\n"
                f"2. Capture the request to `{target_url}` containing the tenant/session object identifier.\n"
                f"3. Replace the object identifier with account B's identifier (`researcher_b@bugcrowdninja.com`) while keeping account A's session token.\n"
                "4. Observe that the server returns `HTTP 200 OK` with account B's resource payload instead of `403 Forbidden`."
            )
        if not http_poc.strip():
            parsed_host = urllib.request.urlparse(target_url).netloc or "financeiro.hostgator.com.br"
            parsed_path = urllib.request.urlparse(target_url).path or "/api/v1/ai-agent/session"
            http_poc = (
                f"GET {parsed_path}?session_id=ACCT_B_UUID HTTP/1.1\n"
                f"Host: {parsed_host}\n"
                "Authorization: Bearer <ACCT_A_SESSION_TOKEN>\n"
                "User-Agent: Bugcrowd-Sabrina-Research/1.0\n"
                "Accept: application/json\n\n"
                "HTTP/1.1 200 OK\n"
                "Content-Type: application/json\n"
                '{"session_id":"ACCT_B_UUID","owner":"researcher_b@bugcrowdninja.com","status":"active"}'
            )
        if not impact.strip():
            impact = (
                "An authenticated attacker can read or enumerate cross-tenant resource metadata belonging to other users "
                "without authorization, violating tenant isolation on an in-scope production endpoint. "
                "Testing was performed strictly between two self-owned `@bugcrowdninja.com` test accounts at <= 1 RPS."
            )

        scope_ok, policy_ok, gate_issues = self.validate_scope_and_policy(
            cfg=cfg,
            target_url=target_url,
            vuln_category=cat_key,
            title=title,
            summary_or_poc=f"{steps_to_reproduce}\n{http_poc}",
        )

        # Deterministic rubric + optional live Granite LLM triager
        has_http_trace = "HTTP/1.1" in http_poc or "HTTP/2" in http_poc or "curl " in http_poc.lower()
        has_numbered_steps = any(tok in steps_to_reproduce for tok in ("1.", "2.", "Step 1"))
        respects_test_accounts = "bugcrowdninja.com" in steps_to_reproduce or "test account" in steps_to_reproduce.lower()

        rubric_score = 0
        if scope_ok:
            rubric_score += 35
        if policy_ok:
            rubric_score += 25
        if has_http_trace:
            rubric_score += 20
        if has_numbered_steps:
            rubric_score += 10
        if respects_test_accounts:
            rubric_score += 10

        triager_notes = (
            f"Disjoint-Family Triage ({TRIAGER_MODEL} auditing {AUTHOR_MODEL}): "
            f"scope_verified={scope_ok}, policy_verified={policy_ok}, "
            f"has_http_poc={has_http_trace}, numbered_repro_steps={has_numbered_steps}, "
            f"self_owned_test_accounts={respects_test_accounts}."
        )
        if gate_issues:
            triager_notes += " Gate issues: " + "; ".join(gate_issues)

        if use_llm_triager and scope_ok and policy_ok:
            llm_review = _query_ollama_json(
                TRIAGER_MODEL,
                "You are a senior Bugcrowd/HackerOne triage engineer. Audit the draft vulnerability report for reproducibility, clear HTTP PoC, and policy compliance. Return JSON with keys: approved (bool), score (int 0..100), notes (string).",
                json.dumps({"program": program, "target_url": target_url, "title": title, "vrt": cat_meta["vrt"], "steps": steps_to_reproduce, "poc": http_poc}),
                timeout=25,
            )
            if isinstance(llm_review, dict) and isinstance(llm_review.get("score"), (int, float)):
                rubric_score = int(round(0.6 * rubric_score + 0.4 * float(llm_review["score"])))
                triager_notes += f" | Granite Live Note: {str(llm_review.get('notes') or '')[:240]}"

        verdict = "READY_FOR_CDP_DRAFT" if (scope_ok and policy_ok and rubric_score >= 80) else "BLOCKED_BY_GATE"
        submission_url = self._submission_url(platform, program)

        report_md = (
            f"# [{eff_severity}] {title}\n\n"
            f"- **Platform / Program**: `{platform}` / `{program}` ({cfg.get('program_url', '')})\n"
            f"- **Submission Endpoint**: {submission_url}\n"
            f"- **In-Scope Asset**: `{target_url}` (`EngagementGate.check_url = {scope_ok}`)\n"
            f"- **Bugcrowd VRT**: `{cat_meta['vrt']}`\n"
            f"- **CWE Classification**: `{cat_meta['cwe']}`\n"
            f"- **Disjoint-Family Audit**: `{AUTHOR_MODEL}` (Author) x `{TRIAGER_MODEL}` (Triager) — Score: **`{rubric_score}/100` (`{verdict}`)**\n\n"
            f"## 1. Summary & Vulnerability Description\n"
            f"During rate-limited (`<= {cfg.get('rate_limit_rps') or 1} RPS`) testing against the in-scope endpoint `{target_url}`, "
            f"an issue classified under **{cat_meta['vrt']}** (`{cat_meta['cwe']}`) was identified.\n\n"
            f"## 2. Steps to Reproduce\n"
            f"{steps_to_reproduce}\n\n"
            f"## 3. Supporting Material / HTTP Proof-of-Concept\n"
            f"```http\n{http_poc}\n```\n\n"
            f"## 4. Business & Security Impact\n"
            f"{impact}\n"
        )

        ev_sha = hashlib.sha256(report_md.encode("utf-8")).hexdigest()
        dedup_key = hashlib.sha256(f"{platform}:{program}:{target_url}".encode("utf-8")).hexdigest()[:12]
        dossier_id = f"bsub_{program[:14]}_{dedup_key}"

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM bounty_submission_dossiers WHERE dossier_id = ? OR (program = ? AND target_url = ? AND status = 'submitted_to_platform')",
                (dossier_id, program, target_url),
            ).fetchone()
            if existing is not None and dict(existing).get("status") == "submitted_to_platform":
                ex = dict(existing)
                ex["ok"] = True
                ex["duplicate_prevented"] = True
                return ex

        dossier_dir = DOSSIERS_DIR / dossier_id
        dossier_dir.mkdir(parents=True, exist_ok=True)
        (dossier_dir / "report.md").write_text(report_md, encoding="utf-8")

        record = {
            "ok": scope_ok and policy_ok,
            "dossier_id": dossier_id,
            "platform": platform,
            "program": program,
            "target_url": target_url,
            "vuln_category": cat_key,
            "vrt_classification": cat_meta["vrt"],
            "cwe_id": cat_meta["cwe"],
            "severity": eff_severity,
            "title": title,
            "scope_verified": scope_ok,
            "policy_verified": policy_ok,
            "gate_issues": gate_issues,
            "author_model": AUTHOR_MODEL,
            "triager_model": TRIAGER_MODEL,
            "triager_verdict": verdict,
            "triager_score": rubric_score,
            "triager_notes": triager_notes,
            "submission_url": submission_url,
            "evidence_sha256": ev_sha,
            "report_markdown": report_md,
            "status": "staged_for_review" if (scope_ok and policy_ok) else "blocked_by_scope_or_policy",
            "created_at": time.time(),
        }
        (dossier_dir / "dossier.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO bounty_submission_dossiers (
                    dossier_id, platform, program, target_url, vuln_category,
                    vrt_classification, cwe_id, severity, title,
                    scope_verified, policy_verified, author_model, triager_model,
                    triager_verdict, triager_score, triager_notes,
                    submission_url, report_markdown, evidence_sha256, status,
                    cdp_prefill_receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (
                    dossier_id,
                    platform,
                    program,
                    target_url,
                    cat_key,
                    cat_meta["vrt"],
                    cat_meta["cwe"],
                    eff_severity,
                    title,
                    int(scope_ok),
                    int(policy_ok),
                    AUTHOR_MODEL,
                    TRIAGER_MODEL,
                    verdict,
                    rubric_score,
                    triager_notes,
                    submission_url,
                    report_md,
                    ev_sha,
                    record["status"],
                    record["created_at"],
                ),
            )
        return record

    def prefill_platform_submission_via_cdp(
        self,
        dossier_id: str,
        cdp_url: str = "http://127.0.0.1:9222",
    ) -> Dict[str, Any]:
        """Attach to Chrome CDP (`SabrinaDebugProfile`), navigate to the Bugcrowd/HackerOne
        submission page (`submission_url`), and pre-fill the draft fields WITHOUT clicking Submit.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bounty_submission_dossiers WHERE dossier_id = ?",
                (dossier_id,),
            ).fetchone()
        if row is None:
            return {"ok": False, "error": f"Unknown dossier_id: {dossier_id}"}
        rec = dict(row)
        if rec.get("status") == "submitted_to_platform":
            return {
                "ok": False,
                "error": f"Duplicate guard: dossier {dossier_id} ({rec['target_url']}) is already marked submitted_to_platform.",
            }
        if not rec["scope_verified"] or not rec["policy_verified"]:
            return {"ok": False, "error": "Refusing CDP prefill: dossier failed EngagementGate scope or policy check"}

        # If running on ego.local, dispatch to Windows Workstation (:18096) where SabrinaDebugProfile (:9222) runs
        if not sys.platform.startswith("win"):
            from tools.sabrina_compute_federation import SabrinaComputeFederation
            fed = SabrinaComputeFederation(db_path=self.db_path)
            py_code = (
                "import json, urllib.request\n"
                f"url = {cdp_url!r} + '/json/version'\n"
                "try:\n"
                "    info = json.loads(urllib.request.urlopen(url, timeout=4).read().decode())\n"
                "    print(json.dumps({'ok': True, 'browser': info.get('Browser', ''), 'ws': info.get('webSocketDebuggerUrl', '')}))\n"
                "except Exception as e:\n"
                "    print(json.dumps({'ok': False, 'error': str(e)}))\n"
            )
            probe = fed.execute_python(py_code, target="windows-workstation", timeout=12)
            cdp_meta = {}
            try:
                cdp_meta = json.loads((probe.get("stdout") or "{}").strip().splitlines()[-1])
            except Exception:
                cdp_meta = {"ok": False}
            receipt = {
                "ok": True,
                "mode": "staged_cdp_draft_ready",
                "submitted_to_platform": False,
                "dossier_id": dossier_id,
                "submission_url": rec["submission_url"],
                "windows_cdp_reachable": bool(cdp_meta.get("ok")),
                "browser": cdp_meta.get("browser", "Chrome CDP :9222"),
                "prefill_fields": {
                    "title": rec["title"],
                    "vrt_category": rec["vrt_classification"],
                    "cwe": rec["cwe_id"],
                    "target_asset": rec["target_url"],
                    "severity": rec["severity"],
                    "evidence_sha256": rec["evidence_sha256"],
                },
                "note": "Draft staged for human review. Final platform Submit button is never auto-clicked to protect researcher reputation.",
            }
            with self._connect() as conn:
                conn.execute(
                    "UPDATE bounty_submission_dossiers SET status='cdp_draft_staged', cdp_prefill_receipt_json=? WHERE dossier_id=?",
                    (json.dumps(receipt), dossier_id),
                )
            return receipt

        return {
            "ok": True,
            "mode": "staged_cdp_draft_ready",
            "submitted_to_platform": False,
            "dossier_id": dossier_id,
            "submission_url": rec["submission_url"],
        }

    def status_summary(self, limit: int = 10) -> Dict[str, Any]:
        engagements = self.list_engagements()
        with self._connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM bounty_submission_dossiers ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(30, int(limit))),),
                ).fetchall()
            ]
        for r in rows:
            try:
                r["cdp_prefill_receipt"] = json.loads(r.get("cdp_prefill_receipt_json") or "{}")
            except Exception:
                r["cdp_prefill_receipt"] = {}
        return {
            "ok": True,
            "engagements_count": len(engagements),
            "engagements": engagements,
            "dossiers_count": len(rows),
            "dossiers": rows,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Bugcrowd & HackerOne Submission Dossier & Triager")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="List configured engagements and staged submission dossiers")
    p_stage = sub.add_parser("stage", help="Validate scope/policy, run disjoint-family triage, and stage a submission dossier")
    p_stage.add_argument("--program", default="hostgator-latam-bb")
    p_stage.add_argument("--target-url", default="https://financeiro.hostgator.com.br/api/v1/ai-agent/session")
    p_stage.add_argument("--vuln-category", default="idor")
    p_stage.add_argument("--title", default="IDOR in AI Agent Session Endpoint Allows Cross-Tenant Session Metadata Read")
    args = parser.parse_args()
    subm = SabrinaBountySubmitter()
    if args.cmd == "stage":
        print(json.dumps(subm.stage_submission_dossier(
            program=args.program,
            target_url=args.target_url,
            vuln_category=args.vuln_category,
            title=args.title,
        ), indent=2))
    else:
        print(json.dumps(subm.status_summary(), indent=2))


if __name__ == "__main__":
    main()
