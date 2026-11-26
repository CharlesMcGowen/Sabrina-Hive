"""
tools/coven/waf_bypass.py
=========================
MrMimeWAFBypass — adaptive WAF evasion engine.
HypnoSessionManager — session security test suite.
DeserializationHunter — multi-runtime deserialization probe.

Extracted from sabrina_coven_orchestrator_LAB.py.
"""

import os
import re
import json
import urllib.request
import urllib.parse
import urllib.error
import base64 as _base64
from typing import List, Dict, Any, Optional

from .shared import STORAGE_DIR, C, _http_get


# ─────────────────────────────────────────────────────────────────────────────
#  MrMimeWAFBypass
# ─────────────────────────────────────────────────────────────────────────────

class MrMimeWAFBypass:
    """
    Adaptive WAF bypass engine. Fingerprints WAF vendor from response headers,
    selects encoding techniques with the highest historical bypass rate for that
    vendor, and learns from each probe outcome.
    Persisted to storage/waf_bypass_memory.json.
    """
    BYPASS_FILE = os.path.join(STORAGE_DIR, "waf_bypass_memory.json")

    WAF_SIGS: Dict[str, List[tuple]] = {
        "cloudflare":  [("cf-ray", ""),              ("server", "cloudflare")],
        "akamai":      [("x-akamai-transformed", ""), ("server", "akamaighost")],
        "sucuri":      [("x-sucuri-id", ""),          ("server", "sucuri")],
        "modsecurity": [("server", "mod_security"),   ("x-modsec-score", "")],
        "awswaf":      [("x-amzn-trace-id", ""),      ("x-amz-cf-id", "")],
        "incapsula":   [("x-iinfo", ""),              ("x-cdn", "incapsula")],
        "f5bigip":     [("x-wa-info", ""),            ("server", "bigip")],
    }

    TECHNIQUES = [
        "url_encode", "double_url_encode", "case_variation", "comment_injection",
        "html_entity", "hex_encode", "null_byte", "tab_substitute", "unicode_escape",
    ]

    def __init__(self):
        os.makedirs(STORAGE_DIR, exist_ok=True)
        self._eff: Dict[str, Dict[str, Dict[str, int]]] = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.BYPASS_FILE):
            return
        try:
            with open(self.BYPASS_FILE, "r", encoding="utf-8") as f:
                self._eff = json.load(f)
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.BYPASS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._eff, f, indent=2)
        except Exception:
            pass

    def fingerprint_waf(self, headers: Dict[str, str]) -> str:
        lh = {k.lower(): v.lower() for k, v in headers.items()}
        for vendor, sigs in self.WAF_SIGS.items():
            for hdr, val in sigs:
                if hdr in lh and (not val or val in lh[hdr]):
                    return vendor
        return ""

    def _transform(self, payload: str, technique: str) -> str:
        if technique == "url_encode":
            return urllib.parse.quote(payload, safe="")
        if technique == "double_url_encode":
            return urllib.parse.quote(urllib.parse.quote(payload, safe=""), safe="")
        if technique == "case_variation":
            out, upper = [], True
            for ch in payload:
                if ch.isalpha():
                    out.append(ch.upper() if upper else ch.lower())
                    upper = not upper
                else:
                    out.append(ch)
            return "".join(out)
        if technique == "comment_injection":
            return payload.replace(" ", "/**/")
        if technique == "html_entity":
            return (payload.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;"))
        if technique == "hex_encode":
            return "".join(f"%{ord(c):02x}" for c in payload)
        if technique == "null_byte":
            return payload + "%00"
        if technique == "tab_substitute":
            return payload.replace(" ", "\t")
        if technique == "unicode_escape":
            return "".join(
                f"\\u{ord(c):04x}" if ord(c) > 127 or c in '<>"\'` ' else c
                for c in payload
            )
        return payload

    def get_bypass_variants(self, payload: str, waf_vendor: str = "",
                            limit: int = 4) -> List[str]:
        vendor_stats = self._eff.get(waf_vendor, {})
        ranked = []
        for tech in self.TECHNIQUES:
            ts    = vendor_stats.get(tech, {"success": 0, "failure": 0})
            total = ts["success"] + ts["failure"]
            rate  = ts["success"] / total if total > 0 else 0.5
            ranked.append((rate, tech))
        ranked.sort(key=lambda x: x[0], reverse=True)
        variants: List[str] = [payload]
        for _, tech in ranked:
            if len(variants) >= limit:
                break
            v = self._transform(payload, tech)
            if v != payload and v not in variants:
                variants.append(v)
        return variants

    def update_effectiveness(self, waf_vendor: str, technique: str, success: bool):
        if not waf_vendor:
            return
        te = self._eff.setdefault(waf_vendor, {}).setdefault(
            technique, {"success": 0, "failure": 0}
        )
        te["success" if success else "failure"] += 1
        self._save()


# ─────────────────────────────────────────────────────────────────────────────
#  HypnoSessionManager — 5-category auth/session test suite
# ─────────────────────────────────────────────────────────────────────────────

class HypnoSessionManager:
    """
    Session security test suite.
    Tests: cookie flags, CSRF, auth-bypass headers, open redirect,
           session fixation signal.
    Only runs in live mode; dry-run returns empty.
    """
    REQUIRED_COOKIE_FLAGS = ["httponly", "secure", "samesite"]

    def run_all_tests(self, target: str,
                      session_headers: Optional[Dict[str, str]] = None,
                      dry_run: bool = True,
                      evidence_pack: Optional[Any] = None) -> List[Dict[str, Any]]:
        if dry_run:
            return []
        hdrs = session_headers or {}
        results: List[Dict[str, Any]] = []
        results += self._cookie_flags(target, hdrs)
        results += self._csrf_probe(target, hdrs)
        results += self._auth_bypass_headers(target)
        results += self._open_redirect(target)
        results += self._session_fixation_signal(target, hdrs)
        return results

    def _cookie_flags(self, target: str, hdrs: Dict[str, str]) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        status, body, resp_headers = _http_get(target, headers=hdrs)
        sc = resp_headers.get("Set-Cookie", resp_headers.get("set-cookie", ""))
        if not sc:
            return findings
        missing = [f for f in self.REQUIRED_COOKIE_FLAGS if f not in sc.lower()]
        if missing:
            findings.append({
                "vuln_type":   "cookie_security",
                "url":         target,
                "severity":    "medium",
                "evidence":    f"Missing cookie flags: {missing}",
                "description": f"Insecure cookie: missing {missing}. Set-Cookie: {sc[:120]}",
            })
        return findings

    def _csrf_probe(self, target: str, hdrs: Dict[str, str]) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        evil_hdrs = {**hdrs, "Origin": "https://evil.com",
                     "Referer": "https://evil.com/",
                     "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "Mozilla/5.0"}
        try:
            req = urllib.request.Request(target, data=b"csrf_test=1",
                                         headers=evil_hdrs, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    status = r.status
            except urllib.error.HTTPError as e:
                status = e.code
            if status == 200:
                findings.append({
                    "vuln_type":   "csrf",
                    "url":         target,
                    "severity":    "medium",
                    "evidence":    "HTTP 200 cross-origin POST accepted",
                    "description": "Possible CSRF: cross-origin POST from evil.com not rejected",
                })
        except Exception:
            pass
        return findings

    def _auth_bypass_headers(self, target: str) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        for extra in [
            {"X-Forwarded-For": "127.0.0.1"},
            {"X-Real-IP": "127.0.0.1"},
            {"X-Original-URL": "/admin"},
            {"X-Rewrite-URL": "/admin"},
            {"X-Custom-IP-Authorization": "127.0.0.1"},
        ]:
            hdrs = {**extra, "User-Agent": "Mozilla/5.0"}
            status, body, _ = _http_get(target, headers=hdrs)
            if status == 200 and any(
                kw in body.lower()
                for kw in ["admin", "dashboard", "manage", "superuser", "settings"]
            ):
                findings.append({
                    "vuln_type":   "authentication_bypass",
                    "url":         target,
                    "severity":    "high",
                    "payload":     str(extra),
                    "evidence":    f"HTTP 200 with admin content using {extra}",
                    "description": f"Auth bypass via header manipulation: {extra}",
                })
                return findings
        return findings

    def _open_redirect(self, target: str) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        for param in ["?next=", "?redirect=", "?return_to=", "?url=", "?continue="]:
            probe = target.rstrip("/") + param + "https://evil.com"
            status, body, resp_headers = _http_get(probe)
            loc = resp_headers.get("Location", "")
            if loc and "evil.com" in loc:
                findings.append({
                    "vuln_type":   "open_redirect",
                    "url":         probe,
                    "severity":    "low",
                    "payload":     param + "https://evil.com",
                    "evidence":    f"Location: {loc}",
                    "description": f"Open redirect via {param.strip('?=')} → {loc}",
                })
                return findings
        return findings

    def _session_fixation_signal(self, target: str,
                                 hdrs: Dict[str, str]) -> List[Dict[str, Any]]:
        findings: List[Dict[str, Any]] = []
        _, _, h1 = _http_get(target.rstrip("/") + "/login")
        c1 = h1.get("Set-Cookie", "")
        for post_auth in ["/dashboard", "/home", "/app", "/portal"]:
            _, b2, h2 = _http_get(target.rstrip("/") + post_auth)
            c2 = h2.get("Set-Cookie", "")
            if c1 and c2 and c1 == c2:
                findings.append({
                    "vuln_type":   "session_fixation",
                    "url":         target,
                    "severity":    "high",
                    "evidence":    "Session ID unchanged pre/post authentication",
                    "description": "Session fixation: cookie not regenerated after login",
                })
                return findings
        return findings


# ─────────────────────────────────────────────────────────────────────────────
#  DeserializationHunter
# ─────────────────────────────────────────────────────────────────────────────

class DeserializationHunter:
    """
    Tests for insecure deserialization across Python Pickle, Java serialization,
    YAML unsafe load, PHP unserialize, and .NET BinaryFormatter.
    Real payloads only sent in live mode; dry-run logs intent only.
    """

    _SPECS: Dict[str, Dict[str, Any]] = {
        "python_pickle": {
            "ctype":       "application/octet-stream",
            "payload_b64": "gASVCgAAAAAAAACMA2RlcIWULg==",
            "sigs": ["pickle", "unpickl", "unserializ", "AttributeError",
                     "ModuleNotFoundError", "_pickle"],
            "hints": ["python", "django", "flask", "fastapi", "celery"],
        },
        "java_serialization": {
            "ctype":       "application/x-java-serialized-object",
            "payload_b64": "rO0ABQ==",
            "sigs": ["java.io", "ClassNotFoundException", "InvalidClassException",
                     "ObjectInputStream", "serializ", "deserializ"],
            "hints": ["java", "tomcat", "spring", "jboss", "weblogic", "glassfish"],
        },
        "yaml_deserialization": {
            "ctype":       "application/x-yaml",
            "payload_b64": "ISFweXRob24vb2JqZWN0L2FwcGx5OmJ1aWx0aW5zLmxpc3QKLSBbXQo=",
            "sigs": ["yaml", "YAMLError", "constructor", "could not determine", "unsafe_load"],
            "hints": ["python", "ruby", "rails", "node", "ansible"],
        },
        "php_unserialize": {
            "ctype":       "application/x-www-form-urlencoded",
            "payload_b64": "Tzo4OiJzdGRDbGFzcyI6MDp7fQ==",
            "sigs": ["unserializ", "Fatal error", "__wakeup", "class not found", "Uncaught Error"],
            "hints": ["php", "laravel", "symfony", "wordpress", "drupal", "magento"],
        },
        "dotnet_deserialization": {
            "ctype":       "application/octet-stream",
            "payload_b64": "AAEAAAD/////",
            "sigs": ["SerializationException", "BinaryFormatter",
                     "System.Runtime.Serialization", "TypeLoadException"],
            "hints": [".net", "asp.net", "iis", "c#", "dotnet", "aspx"],
        },
    }

    def probe(self, target: str, tech_stack: Dict[str, str],
              dry_run: bool = True) -> List[Dict[str, Any]]:
        tech_lower = " ".join(v.lower() for v in tech_stack.values())
        ordered: List[str] = []
        for dtype, spec in self._SPECS.items():
            if any(h in tech_lower for h in spec["hints"]):
                ordered.insert(0, dtype)
            else:
                ordered.append(dtype)
        ordered = list(dict.fromkeys(ordered))[:3]

        findings: List[Dict[str, Any]] = []
        for dtype in ordered:
            spec = self._SPECS[dtype]
            if dry_run:
                findings.append({
                    "dry_run":     True,
                    "dtype":       dtype,
                    "target":      target,
                    "description": (
                        f"[DRY-RUN] Would POST {dtype} canary gadget to {target} "
                        f"(Content-Type: {spec['ctype']})"
                    ),
                })
                continue

            raw_payload = _base64.b64decode(spec["payload_b64"])
            try:
                req = urllib.request.Request(
                    target, data=raw_payload,
                    headers={"Content-Type": spec["ctype"],
                             "User-Agent": "Mozilla/5.0 (Security Research)"},
                    method="POST"
                )
                try:
                    with urllib.request.urlopen(req, timeout=12) as r:
                        status = r.status
                        body   = r.read().decode("utf-8", errors="ignore")
                except urllib.error.HTTPError as e:
                    status = e.code
                    body   = e.read().decode("utf-8", errors="ignore")

                matched = next(
                    (s for s in spec["sigs"] if s.lower() in body.lower()), None
                )
                if matched:
                    findings.append({
                        "vuln_type":   "deserialization",
                        "url":         target,
                        "severity":    "high",
                        "payload":     f"{dtype} canary gadget",
                        "evidence":    matched,
                        "description": (
                            f"Insecure deserialization ({dtype}): "
                            f"signature '{matched}' in response to "
                            f"{spec['ctype']} POST"
                        ),
                    })
                    return findings
            except Exception:
                pass
        return findings
