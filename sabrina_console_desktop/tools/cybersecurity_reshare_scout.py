"""Autonomous Cybersecurity Feed Scout & Safe Proxy Reshare Engine for Sabrina.

Scouts LinkedIn feed for high-value cybersecurity intelligence (vulnerabilities,
CVEs, threat actor reports, malware analysis, AppSec tooling), extracts embedded
links, and safely explores external URLs using isolated web proxy / Tor egress
(strictly unauthenticated, zero credential/session inheritance, SSRF-protected).

Evaluates whether the post and linked resource offer genuine technical substance
(POCs, IOCs, architectural teardowns, novel research vs marketing fluff/paywalls).
Stages approved reshares within Sabrina's scheduling, rate limits (08:00-21:00,
max 2/day), and single-use operator approval gates.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

# Ensure Sabrina root is on sys.path
SABRINA_ROOT = Path(__file__).resolve().parent.parent
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.cyber_compliance_sentinel import CyberComplianceSentinel
from tools.proxy_tor_egress_manager import SafePrivacySession, ProxyPoolManager, ProxyNode
from tools.linkedin_thread_browser import post_identity

LOGGER = logging.getLogger("sabrina.cybersecurity_scout")
DEFAULT_SOCIAL_DB = SABRINA_ROOT / "data" / "social" / "social.sqlite3"


# -----------------------------------------------------------------------------
# 1. Cybersecurity Topic Taxonomy & Filtering
# -----------------------------------------------------------------------------

CYBERSECURITY_KEYWORDS = {
    # Vulnerability & Exploit Research
    "cve", "0-day", "zero-day", "exploit", "poc", "proof-of-concept", "rce",
    "remote code execution", "privilege escalation", "privesc", "buffer overflow",
    "heap overflow", "use-after-free", "uaf", "memory safety", "type confusion",
    "ssrf", "sqli", "sql injection", "xss", "cross-site scripting", "deserialization",
    "auth bypass", "authentication bypass", "arbitrary file read", "path traversal",
    "directory traversal", "sandbox escape", "vm escape", "side-channel", "rowhammer",
    # Threat Intelligence & Malware Teardowns
    "threat intelligence", "threat actor", "apt", "advanced persistent threat",
    "infostealer", "ransomware", "trojan", "loader", "wiper", "botnet", "c2",
    "command and control", "beacon", "cobalt strike", "brute ratel",
    "reverse engineering", "disassembly", "decompilation", "ghidra", "ida pro",
    "binary analysis", "yara", "sigma rule", "iocs", "indicators of compromise",
    # Cloud, Container & Infra Security
    "kubernetes security", "k8s security", "container breakout", "cloud security",
    "aws security", "iam privilege escalation", "misconfiguration", "active directory",
    "kerberoasting", "ntlm relay", "bloodhound", "lateral movement",
    # AppSec, AI Security & Defensive Engineering
    "appsec", "application security", "supply chain attack", "dependency confusion",
    "typosquatting", "prompt injection", "model inversion", "adversarial attack",
    "cryptography", "post-quantum", "zero trust", "ebpf security", "edr bypass",
    "detection engineering", "dfir", "incident response"
}

NON_CYBER_NOISE_PATTERNS = [
    r"\bhiring\b", r"\bjoin our team\b", r"\bwe are hiring\b",
    r"\bcongratulations on\b", r"\bhappy to share that i['’]ve started\b",
    r"\bcertified in\b", r"\bpassed my .* exam\b", r"\bpromoted to\b",
    r"\bthrilled to announce my new role\b", r"\bdiscount code\b",
    r"\bbook a demo\b", r"\bschedule a demo\b", r"\bwebinar registration\b"
]


def classify_cybersecurity_text(text: str) -> Tuple[bool, List[str], float]:
    """
    Classifies whether text discusses cybersecurity topics.
    Returns (is_cyber, matched_signals, confidence_score).
    """
    if not text or not text.strip():
        return False, [], 0.0

    lower = text.lower()

    # Reject obvious career/announcement noise
    for pat in NON_CYBER_NOISE_PATTERNS:
        if re.search(pat, lower):
            # Still allow if heavy CVE or PoC discussion
            if not re.search(r"\bcve-\d{4}-\d{4,7}\b", lower):
                return False, ["noise_rejected"], 0.0

    matched = []
    for kw in CYBERSECURITY_KEYWORDS:
        if " " in kw:
            if kw in lower:
                matched.append(kw)
        else:
            if re.search(r"\b" + re.escape(kw) + r"\b", lower):
                matched.append(kw)

    # Check for CVE patterns
    cves = re.findall(r"\bcve-\d{4}-\d{4,7}\b", lower)
    for cve in cves:
        matched.append(cve.upper())

    matched = list(dict.fromkeys(matched))
    is_cyber = len(matched) >= 1
    # Confidence score scaled by signal density
    confidence = min(1.0, len(matched) * 0.25)
    return is_cyber, matched, confidence


# -----------------------------------------------------------------------------
# 2. Safe URL Extraction & SSRF Leak-Prevention Guard
# -----------------------------------------------------------------------------

URL_REGEX = re.compile(
    r'https?://(?:[a-zA-Z0-9_\-]+\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s<>"\'{}|\\^`]*)*',
    re.IGNORECASE
)

BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0"
}

BLOCKED_DOMAIN_SUFFIXES = (
    ".local", ".internal", ".lan", ".home", ".corp", ".test", ".example", ".invalid", ".onion"
)

PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local / Cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),     # Private RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),      # IETF Protocol Assignments
    ipaddress.ip_network("192.0.2.0/24"),      # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),    # Private RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),     # Benchmarking
    ipaddress.ip_network("198.51.100.0/24"),   # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),    # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),       # Multicast
    ipaddress.ip_network("240.0.0.0/4"),       # Reserved
    ipaddress.ip_network("255.255.255.255/32"), # Broadcast
    ipaddress.ip_network("::1/128"),           # IPv6 Loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 Unique Local
    ipaddress.ip_network("fe80::/10"),         # IPv6 Link-local
]


class URLSafetyGuard:
    """Validates external URLs to strictly prevent SSRF, DNS rebinding, and credential leakage."""

    @staticmethod
    def extract_urls(text: str) -> List[str]:
        """Extracts valid URLs from free text and strips trailing punctuation."""
        if not text:
            return []
        raw_matches = URL_REGEX.findall(text)
        cleaned = []
        for url in raw_matches:
            # Strip trailing punctuation often caught by regex
            url = re.sub(r'[\.,;:\)\]!]+$', '', url)
            if url.startswith(("http://", "https://")):
                cleaned.append(url)
        return list(dict.fromkeys(cleaned))

    @staticmethod
    def is_safe_public_url(url: str, resolve_dns: bool = True) -> Tuple[bool, str]:
        """
        Validates that a URL is a legitimate public web resource.
        Disallows private IP ranges, cloud metadata (169.254.169.254), and local networks.
        """
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False, f"Unsupported scheme: {parsed.scheme}"

            hostname = parsed.hostname
            if not hostname:
                return False, "Missing hostname"

            hostname_lower = hostname.lower()

            if hostname_lower in BLOCKED_HOSTNAMES:
                return False, f"Blocked internal hostname: {hostname}"

            if any(hostname_lower.endswith(suffix) for suffix in BLOCKED_DOMAIN_SUFFIXES):
                return False, f"Blocked internal domain suffix: {hostname}"

            # Check if host is a raw IP literal
            try:
                ip_obj = ipaddress.ip_address(hostname_lower)
                for net in PRIVATE_NETWORKS:
                    if ip_obj in net:
                        return False, f"Blocked private IP literal: {hostname} in {net}"
            except ValueError:
                # Host is a domain name
                pass

            if resolve_dns:
                try:
                    # Resolve domain name to verify underlying IP addresses
                    addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                    for item in addr_info:
                        resolved_ip = item[4][0]
                        ip_obj = ipaddress.ip_address(resolved_ip)
                        for net in PRIVATE_NETWORKS:
                            if ip_obj in net:
                                return False, f"Domain {hostname} resolves to private IP {resolved_ip} in {net}"
                except socket.gaierror as e:
                    # DNS resolution failure
                    return False, f"DNS resolution failed for {hostname}: {e}"

            return True, ""
        except Exception as exc:
            return False, f"URL validation error: {exc}"


# -----------------------------------------------------------------------------
# 3. Isolated Web Proxy Content Explorer
# -----------------------------------------------------------------------------

@dataclass
class ExploredResource:
    original_url: str
    final_url: str
    status_code: int
    proxy_used: str
    egress_ip: str
    title: str = ""
    meta_description: str = ""
    author_or_site: str = ""
    clean_text: str = ""
    is_safe: bool = True
    threat_flag: Optional[str] = None
    latency_ms: float = 0.0


class ProxyContentExplorer:
    """
    Safely explores third-party web links through unauthenticated proxy/Tor egress.
    Zero cookie/credential inheritance. Strips active elements (scripts/iframes).
    """

    def __init__(self, pool_manager: Optional[ProxyPoolManager] = None):
        self.privacy_session = SafePrivacySession(pool_manager=pool_manager)
        self.sentinel = CyberComplianceSentinel()

    def explore_url(self, target_url: str, max_redirects: int = 5, timeout: float = 12.0) -> ExploredResource:
        """
        Fetches an external URL through the privacy proxy pool, follows redirects safely,
        extracts clean readable text, and audits for threats.
        """
        current_url = target_url
        redirect_count = 0
        final_status = 0
        proxy_used = "unknown"
        egress_ip = "unknown"
        latency_total = 0.0
        response_text = ""

        while redirect_count <= max_redirects:
            # 1. Enforce strict SSRF check on every hop
            safe, reason = URLSafetyGuard.is_safe_public_url(current_url)
            if not safe:
                LOGGER.warning("Blocked unsafe URL hop: %s (%s)", current_url, reason)
                return ExploredResource(
                    original_url=target_url,
                    final_url=current_url,
                    status_code=403,
                    proxy_used=proxy_used,
                    egress_ip=egress_ip,
                    clean_text=f"Exploration aborted: {reason}",
                    is_safe=False,
                    threat_flag="SSRF_PREVENTION"
                )

            # 2. Dispatch request via privacy proxy
            try:
                res = self.privacy_session.get(current_url, timeout=timeout)
                final_status = res.get("status_code", 0)
                proxy_used = res.get("proxy_used", "tor_daemon_primary")
                egress_ip = res.get("egress_ip", "masked")
                latency_total += res.get("latency_ms", 0.0)
                response_text = res.get("response_text", "")

                # Check for HTTP redirect
                if final_status in (301, 302, 303, 307, 308):
                    # In requests, allow_redirects=True by default.
                    # If manually returned redirect:
                    redirect_count += 1
                    continue
                break
            except Exception as exc:
                LOGGER.warning("Proxy exploration failed for %s: %s", current_url, exc)
                return ExploredResource(
                    original_url=target_url,
                    final_url=current_url,
                    status_code=502,
                    proxy_used=proxy_used,
                    egress_ip=egress_ip,
                    clean_text=f"Connection failed: {exc}",
                    is_safe=False
                )

        # 3. Whistleblower threat check on raw content
        threat_reported = False
        threat_desc = None
        if response_text and len(response_text) > 50:
            report = self.sentinel.inspect_and_report_threat(
                source_identifier=f"proxy_scout:{current_url}",
                content=response_text[:4000]
            )
            if report:
                threat_reported = True
                threat_desc = f"Hostile payload detected (Incident {report.get('incident_id')})"
                LOGGER.warning("Whistleblower alert triggered on explored URL %s: %s", current_url, threat_desc)

        # 4. Extract sanitized content
        title, desc, site, clean_text = self._sanitize_html(response_text)

        return ExploredResource(
            original_url=target_url,
            final_url=current_url,
            status_code=final_status,
            proxy_used=proxy_used,
            egress_ip=egress_ip,
            title=title,
            meta_description=desc,
            author_or_site=site,
            clean_text=clean_text[:6000],
            is_safe=not threat_reported,
            threat_flag=threat_desc,
            latency_ms=latency_total
        )

    @staticmethod
    def _sanitize_html(html_content: str) -> Tuple[str, str, str, str]:
        """Strips scripts, styles, iframes, forms, and extracts title, description, and clean body."""
        if not html_content or not html_content.strip():
            return "", "", "", ""

        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, "html.parser")

            # Remove hazardous tags
            for elem in soup(["script", "style", "svg", "iframe", "frame", "object", "embed", "applet", "form", "button", "nav", "header", "footer"]):
                elem.decompose()

            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()

            desc = ""
            meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
            if meta_desc and meta_desc.get("content"):
                desc = meta_desc["content"].strip()

            site = ""
            og_site = soup.find("meta", attrs={"property": "og:site_name"}) or soup.find("meta", attrs={"name": "author"})
            if og_site and og_site.get("content"):
                site = og_site["content"].strip()

            # Extract substantive body text
            text = soup.get_text(separator="\n")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            clean_body = "\n".join(lines)
            return title, desc, site, clean_body

        except Exception:
            # Fallback simple regex sanitization
            no_tags = re.sub(r'<[^>]+>', ' ', html_content)
            clean_body = re.sub(r'\s+', ' ', no_tags).strip()
            return "", "", "", clean_body


# -----------------------------------------------------------------------------
# 4. Substance Evaluation & Grounded Reshare Synthesis
# -----------------------------------------------------------------------------

@dataclass
class SubstanceEvaluation:
    is_cybersecurity: bool
    technical_depth_score: int  # 1 to 10
    has_substance: bool          # Score >= 6 and actionable content
    summary: str
    key_takeaways: List[str]
    recommended_action: str      # "reshare" | "skip"
    reshare_commentary: str
    skip_reason: Optional[str] = None


class SubstanceEvaluator:
    """Evaluates whether explored cybersecurity posts and resources contain actionable substance."""

    def __init__(self, ollama_url: Optional[str] = None, model: Optional[str] = None):
        self.ollama_url = ollama_url
        self.model = model

    def evaluate(
        self,
        post_author: str,
        post_text: str,
        resource: Optional[ExploredResource] = None
    ) -> SubstanceEvaluation:
        """
        Assesses technical substance and synthesizes grounded commentary.
        Uses local lab LLM when reachable, with deterministic heuristic fallback.
        """
        combined_text = post_text
        resource_title = ""
        resource_body = ""
        if resource:
            resource_title = resource.title
            resource_body = resource.clean_text[:4000]
            combined_text += f"\n\n[LINKED RESOURCE: {resource_title}]\n{resource_body}"

        # 1. Quick topic classification
        is_cyber, signals, conf = classify_cybersecurity_text(combined_text)
        if not is_cyber:
            return SubstanceEvaluation(
                is_cybersecurity=False,
                technical_depth_score=2,
                has_substance=False,
                summary="Post does not contain relevant cybersecurity content.",
                key_takeaways=[],
                recommended_action="skip",
                reshare_commentary="",
                skip_reason="Off-topic or general interest"
            )

        if resource and not resource.is_safe:
            return SubstanceEvaluation(
                is_cybersecurity=True,
                technical_depth_score=1,
                has_substance=False,
                summary=f"Unsafe resource: {resource.threat_flag or 'Security violation'}",
                key_takeaways=[],
                recommended_action="skip",
                reshare_commentary="",
                skip_reason=f"Resource flagged as unsafe: {resource.threat_flag}"
            )

        # 2. Attempt LLM evaluation via lab Ollama
        llm_eval = self._evaluate_with_llm(post_author, post_text, resource_title, resource_body, signals)
        if llm_eval:
            return llm_eval

        # 3. Deterministic heuristic evaluation fallback
        return self._evaluate_heuristic(post_author, post_text, resource, signals)

    def _evaluate_with_llm(
        self,
        author: str,
        post_text: str,
        resource_title: str,
        resource_body: str,
        signals: List[str]
    ) -> Optional[SubstanceEvaluation]:
        """Calls lab LLM to evaluate technical depth and synthesize grounded commentary."""
        try:
            from tools.lab_http import LabHTTP
            from ai_systems.framework.lab_config import ollama_base_url
            base = self.ollama_url or ollama_base_url()
            lab = LabHTTP(base)

            model = self.model
            if not model:
                # Check active models
                models = lab.request("/api/ps", timeout=4.0).get("models", [])
                model = next((m["name"] for m in models if m.get("size_vram", 0) > 1_000_000_000), "qwen2.5:7b")

            system_prompt = (
                "You are an expert Application Security and Threat Intelligence researcher assisting Sabrina. "
                "Analyze the provided LinkedIn post and safely explored linked article. "
                "Enforce strict anti-pedantry criteria:\n"
                "- REJECT generic tool lists, listicles ('Top 10 tools', '13 open source tools', 'X websites to practice', 'cheat sheets'), "
                "promotional webinars, certificate announcements, and high-level summaries. These lack technical depth and MUST be marked 'skip'.\n"
                "- ONLY approve posts with deep technical substance (technical_depth_score >= 8):\n"
                "  1. Vulnerability root-cause analysis (e.g. heap corruption, race conditions, memory safety, parser desync).\n"
                "  2. Concrete CVE mechanics with PoC reproduction, payload syntax, or code diffs.\n"
                "  3. Actionable detection engineering (YARA rules, Sigma rules, threat actor C2 IOCs).\n"
                "  4. Novel offensive tradecraft, reverse engineering, or kernel/compiler-level defensive engineering.\n"
                "Return ONLY valid JSON matching this schema:\n"
                "{\n"
                '  "is_cybersecurity": true,\n'
                '  "technical_depth_score": 8,\n'
                '  "has_substance": true,\n'
                '  "summary": "Concise 1-2 sentence technical summary",\n'
                '  "key_takeaways": ["point 1", "point 2"],\n'
                '  "recommended_action": "reshare",\n'
                '  "reshare_commentary": "Concise professional commentary (200-400 chars) highlighting the technical takeaway, citing author/source.",\n'
                '  "skip_reason": ""\n'
                "}\n"
                "If score < 8 or if it is a listicle, promotional, or pedantic roundup, set recommended_action to 'skip' and explain skip_reason."
            )

            user_content = json.dumps({
                "author": author,
                "post_text": post_text[:2000],
                "linked_title": resource_title,
                "linked_content": resource_body[:3000],
                "detected_signals": signals
            })

            payload = {
                "model": model,
                "stream": False,
                "think": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                "format": "json",
                "options": {"temperature": 0.2, "num_predict": 700}
            }

            resp = lab.request("/api/chat", payload, timeout=60.0)
            raw = resp.get("message", {}).get("content", "").strip()
            if not raw:
                return None

            data = json.loads(raw)
            return SubstanceEvaluation(
                is_cybersecurity=bool(data.get("is_cybersecurity", True)),
                technical_depth_score=int(data.get("technical_depth_score", 5)),
                has_substance=bool(data.get("has_substance", False)),
                summary=str(data.get("summary", "")),
                key_takeaways=list(data.get("key_takeaways", [])),
                recommended_action=str(data.get("recommended_action", "skip")),
                reshare_commentary=str(data.get("reshare_commentary", "")).strip(),
                skip_reason=data.get("skip_reason")
            )
        except Exception as exc:
            LOGGER.debug("LLM substance evaluation unavailable, falling back to heuristics: %s", exc)
            return None

    def _evaluate_heuristic(
        self,
        author: str,
        post_text: str,
        resource: Optional[ExploredResource],
        signals: List[str]
    ) -> SubstanceEvaluation:
        """Deterministic heuristic fallback based on technical keyword density and indicators."""
        corpus = post_text.lower()
        if resource:
            corpus += " " + resource.clean_text.lower()

        # Indicators of technical substance
        indicators = {
            "cve": bool(re.search(r"cve-\d{4}-\d{4,7}", corpus)),
            "code_poc": any(t in corpus for t in ("poc", "proof-of-concept", "exploit", "github.com", "curl", "payload")),
            "technical_terms": any(t in corpus for t in ("rce", "privilege escalation", "heap overflow", "uaf", "ssrf", "sqli", "deserialization")),
            "iocs_or_rules": any(t in corpus for t in ("yara", "sigma", "hashes", "sha256", "mitre att&ck", "c2")),
            "defense_guidance": any(t in corpus for t in ("mitigation", "patch", "remediation", "workaround", "detection"))
        }

        matched_indicators = [k for k, v in indicators.items() if v]
        score = min(10, 3 + len(matched_indicators) * 2)

        # Detect generic tool lists, listicles, or roundups
        is_pedantic_listicle = any(p in corpus for p in (
            "top 10", "top 5", "free websites", "tools to practice", "open-source tools",
            "cheat sheet", "cheat sheets", "awesome list", "13 open", "15 free", "webinar",
            "join us", "certificat"
        ))
        if is_pedantic_listicle:
            score = min(score, 4)

        has_substance = score >= 8
        action = "reshare" if has_substance else "skip"
        skip_reason = None if has_substance else (
            "Pedantic tool listicle or promotional roundup lacking deep vulnerability root-cause analysis"
            if is_pedantic_listicle else "Insufficient technical depth (< 8/10)"
        )

        # Generate grounded commentary
        commentary = ""
        summary = ""
        if has_substance:
            signal_str = ", ".join(signals[:3])
            summary = f"Technical disclosure focusing on {signal_str}."
            if indicators["cve"]:
                cve_match = re.search(r"cve-\d{4}-\d{4,7}", corpus)
                cve_name = cve_match.group(0).upper() if cve_match else "CVE disclosure"
                commentary = f"Valuable technical analysis on {cve_name} via {author or 'researcher'}. Highlights actionable attack mechanics and remediation guidance."
            elif indicators["code_poc"]:
                commentary = f"High-signal technical breakdown shared by {author or 'researcher'}, detailing reproducible proof-of-concept vectors and security boundaries."
            else:
                commentary = f"Insightful defensive analysis on {signal_str} from {author or 'researcher'}, emphasizing practical detection and risk mitigation."

        return SubstanceEvaluation(
            is_cybersecurity=True,
            technical_depth_score=score,
            has_substance=has_substance,
            summary=summary,
            key_takeaways=[f"Discusses {sig}" for sig in signals[:3]],
            recommended_action=action,
            reshare_commentary=commentary,
            skip_reason=skip_reason
        )


# -----------------------------------------------------------------------------
# 5. Durable SQLite Persistence & Scout State
# -----------------------------------------------------------------------------

def ensure_scout_schema(db_path: Path) -> None:
    """Ensures cybersecurity_scout_records table exists in SQLite."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cybersecurity_scout_records (
                id TEXT PRIMARY KEY,
                post_urn TEXT,
                post_author TEXT,
                post_url TEXT,
                external_url TEXT,
                final_url TEXT,
                proxy_used TEXT,
                egress_ip TEXT,
                is_cybersecurity INTEGER NOT NULL,
                technical_depth_score INTEGER NOT NULL,
                has_substance INTEGER NOT NULL,
                summary TEXT,
                reshare_commentary TEXT,
                decision TEXT NOT NULL,
                skip_reason TEXT,
                staged_job_id TEXT,
                created REAL NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cyber_scout_created ON cybersecurity_scout_records(created)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cyber_scout_urn ON cybersecurity_scout_records(post_urn)")
        conn.commit()


# -----------------------------------------------------------------------------
# 6. Cybersecurity Reshare Scout Orchestrator
# -----------------------------------------------------------------------------

class CybersecurityReshareScout:
    """
    Orchestrates the entire scout lifecycle:
    1. Scan LinkedIn feed posts for cybersecurity criteria.
    2. Extract and validate embedded links.
    3. Safely explore external links via isolated web proxy / Tor.
    4. Evaluate substance and synthesize grounded commentary.
    5. Stage reshares within scheduler & rate limits with operator approval gate.
    """

    def __init__(
        self,
        runtime: Optional[Any] = None,
        db_path: Path = DEFAULT_SOCIAL_DB,
        pool_manager: Optional[ProxyPoolManager] = None,
        cdp_url: Optional[str] = None
    ):
        self.cdp_url = cdp_url
        self.runtime = runtime
        self.db_path = db_path
        ensure_scout_schema(self.db_path)
        self.explorer = ProxyContentExplorer(pool_manager=pool_manager)
        self.evaluator = SubstanceEvaluator()

    def scout_feed_candidates(self, max_posts: int = 5) -> List[Dict[str, Any]]:
        """Extracts visible feed posts and classifies candidates."""
        if not self.runtime:
            from sabrina_social.runtime import SocialRuntime
            cdp_url = self.cdp_url or os.environ.get("SABRINA_SOCIAL_CDP_URL") or "http://127.0.0.1:9222"
            self.runtime = SocialRuntime(cdp_url=cdp_url)

        # Fetch visible feed posts
        if hasattr(self.runtime.manager, "fetch_feed_posts"):
            posts = self.runtime.manager.fetch_feed_posts(max_posts=max_posts)
        else:
            # Fallback to feed_suggestions read
            res = self.runtime.run_read("feed_suggestions", {"max_posts": max_posts})
            posts = res.get("decisions", [])

        candidates = []
        for post in posts:
            text = post.get("text", "")
            author = post.get("author", "Unknown")
            urn = post.get("urn", "")

            # Classify topic
            is_cyber, signals, conf = classify_cybersecurity_text(text)
            if not is_cyber:
                continue

            # Extract URLs
            extracted_urls = URLSafetyGuard.extract_urls(text)
            # Combine with any links from DOM card
            dom_links = post.get("links", [])
            for link in dom_links:
                if link not in extracted_urls:
                    extracted_urls.append(link)

            # Determine canonical post url
            _, canonical_post_url = post_identity(urn)
            if not canonical_post_url and urn:
                canonical_post_url = f"https://www.linkedin.com/feed/update/{urn}/"

            candidates.append({
                "urn": urn,
                "author": author,
                "text": text,
                "post_url": canonical_post_url or "",
                "signals": signals,
                "confidence": conf,
                "external_urls": extracted_urls
            })

        return candidates

    def process_candidate(
        self,
        candidate: Dict[str, Any],
        dry_run: bool = True,
        auto_approve: bool = False
    ) -> Dict[str, Any]:
        """Explores links, evaluates substance, and stages reshare if qualified."""
        external_urls = candidate.get("external_urls", [])
        explored_resource = None

        if external_urls:
            # Explore the first valid external link
            target_link = external_urls[0]
            explored_resource = self.explorer.explore_url(target_link)

        # Evaluate substance
        evaluation = self.evaluator.evaluate(
            candidate.get("author", ""),
            candidate.get("text", ""),
            explored_resource
        )

        urn_val = candidate.get("urn", "")
        raw_key = f"{urn_val}:{time.time()}".encode("utf-8")
        record_id = f"scout_{hashlib.sha256(raw_key).hexdigest()[:12]}"
        decision = "skipped"
        staged_job_id = None

        if evaluation.has_substance and evaluation.recommended_action == "reshare":
            if dry_run:
                decision = "dry_run"
            else:
                decision = self._stage_reshare(candidate, evaluation, auto_approve=auto_approve)
                if isinstance(decision, dict):
                    staged_job_id = decision.get("job_id")
                    decision = decision.get("status", "staged")

        # Record in database
        self._record_scout_event(
            record_id=record_id,
            candidate=candidate,
            resource=explored_resource,
            evaluation=evaluation,
            decision=decision,
            staged_job_id=staged_job_id
        )

        return {
            "record_id": record_id,
            "post_urn": candidate.get("urn"),
            "author": candidate.get("author"),
            "post_url": candidate.get("post_url"),
            "is_cybersecurity": evaluation.is_cybersecurity,
            "technical_depth_score": evaluation.technical_depth_score,
            "has_substance": evaluation.has_substance,
            "summary": evaluation.summary,
            "key_takeaways": evaluation.key_takeaways,
            "reshare_commentary": evaluation.reshare_commentary,
            "decision": decision,
            "skip_reason": evaluation.skip_reason,
            "staged_job_id": staged_job_id,
            "explored_url": explored_resource.final_url if explored_resource else None,
            "proxy_used": explored_resource.proxy_used if explored_resource else None,
            "egress_ip": explored_resource.egress_ip if explored_resource else None,
        }

    def _stage_reshare(
        self,
        candidate: Dict[str, Any],
        evaluation: SubstanceEvaluation,
        auto_approve: bool = False
    ) -> Dict[str, Any]:
        """Stages the approved or pending reshare in scheduler / SocialStore."""
        source_url = candidate.get("post_url")
        if not source_url and candidate.get("urn"):
            source_url = f"https://www.linkedin.com/feed/update/{candidate['urn']}/"
        if not source_url and candidate.get("external_urls"):
            source_url = candidate["external_urls"][0]

        commentary = evaluation.reshare_commentary

        if not source_url:
            return {"status": "skipped", "reason": "No canonical post URL or external reference link"}

        from sabrina_social.scheduler import JobStore, build_social_scheduler
        store = JobStore()
        payload = {
            "source_url": source_url,
            "commentary": commentary,
            "objective": "cybersecurity_intel_reshare"
        }

        if auto_approve:
            scheduler = build_social_scheduler(self.runtime)
            try:
                job = scheduler.schedule_approved_social_action(
                    self.runtime,
                    "linkedin.reshare",
                    payload,
                    approved_at_schedule=True
                )
                return {"status": "reshare_approved", "job_id": job.get("id")}
            except Exception as exc:
                LOGGER.error("Failed to schedule approved reshare: %s", exc)
                return {"status": "failed", "reason": str(exc)}
        else:
            # Stage as draft for operator single-use approval
            job = store.create(
                capability="linkedin.reshare",
                payload=payload,
                resources=["browser:linkedin-primary"],
                next_run=time.time(),
                draft=True
            )
            return {"status": "reshare_staged", "job_id": job.get("id")}

    def _record_scout_event(
        self,
        record_id: str,
        candidate: Dict[str, Any],
        resource: Optional[ExploredResource],
        evaluation: SubstanceEvaluation,
        decision: str,
        staged_job_id: Optional[str]
    ) -> None:
        """Persists the audit record into SQLite."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute(
                    """
                    INSERT INTO cybersecurity_scout_records (
                        id, post_urn, post_author, post_url, external_url,
                        final_url, proxy_used, egress_ip, is_cybersecurity,
                        technical_depth_score, has_substance, summary,
                        reshare_commentary, decision, skip_reason, staged_job_id,
                        created
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id,
                        candidate.get("urn"),
                        candidate.get("author"),
                        candidate.get("post_url"),
                        resource.original_url if resource else None,
                        resource.final_url if resource else None,
                        resource.proxy_used if resource else None,
                        resource.egress_ip if resource else None,
                        1 if evaluation.is_cybersecurity else 0,
                        evaluation.technical_depth_score,
                        1 if evaluation.has_substance else 0,
                        evaluation.summary,
                        evaluation.reshare_commentary,
                        decision,
                        evaluation.skip_reason,
                        staged_job_id,
                        time.time()
                    )
                )
                conn.commit()
        except Exception as exc:
            LOGGER.warning("Could not persist scout record to SQLite: %s", exc)

    def run_scout_cycle(
        self,
        max_posts: int = 5,
        dry_run: bool = True,
        auto_approve: bool = False
    ) -> Dict[str, Any]:
        """Runs an end-to-end scouting, proxy exploration, and staging cycle."""
        LOGGER.info("Starting cybersecurity scout cycle (max_posts=%d, dry_run=%s)", max_posts, dry_run)
        candidates = self.scout_feed_candidates(max_posts=max_posts)
        results = []

        for candidate in candidates:
            res = self.process_candidate(candidate, dry_run=dry_run, auto_approve=auto_approve)
            results.append(res)

        substantive = [r for r in results if r.get("has_substance")]
        staged = [r for r in results if "reshare" in r.get("decision", "")]

        return {
            "status": "completed",
            "dry_run": dry_run,
            "posts_scanned": max_posts,
            "cyber_candidates_found": len(candidates),
            "substantive_findings": len(substantive),
            "reshares_staged": len(staged),
            "results": results
        }


# -----------------------------------------------------------------------------
# 7. CLI Entrypoint
# -----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sabrina Cybersecurity Feed Scout & Safe Reshare Engine")
    parser.add_argument("--max-posts", type=int, default=5, help="Maximum feed posts to scan")
    parser.add_argument("--cdp-url", type=str, default=None, help="Chrome CDP URL (default: auto)")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Explore and evaluate without staging jobs")
    parser.add_argument("--auto-approve", action="store_true", default=False, help="Queue reshares with immediate approval")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    runtime = None
    if args.cdp_url:
        from sabrina_social.runtime import SocialRuntime
        runtime = SocialRuntime(cdp_url=args.cdp_url)

    scout = CybersecurityReshareScout(runtime=runtime)
    summary = scout.run_scout_cycle(
        max_posts=args.max_posts,
        dry_run=args.dry_run,
        auto_approve=args.auto_approve
    )

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print("\n=== Cybersecurity Scout Summary ===")
        print(f"Candidates Found: {summary['cyber_candidates_found']}")
        print(f"Substantive:      {summary['substantive_findings']}")
        print(f"Reshares Staged:  {summary['reshares_staged']}")
        for r in summary["results"]:
            print(f"- [{r['decision'].upper()}] {r['author']}: score={r['technical_depth_score']}/10")
            if r['reshare_commentary']:
                print(f"  Commentary: {r['reshare_commentary']}")


if __name__ == "__main__":
    main()
