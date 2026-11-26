"""
tools/response_classifier.py
=============================
Deterministic, rules-based vulnerability response classifier.

Extracted from sabrina_coven_orchestrator_LAB.py's ResponseClassifier (found during
review — a real, already-built OWASP/PortSwigger-style detection scheme that
sabrina_gemini_loop.py never used). Pulled out into its own tools/ module rather
than importing the 260KB orchestrator file directly, to avoid any import-time side
effects from unrelated machinery in that file.

Uses config/detection_rules.json (two schemas: "confirm_any" -- id/endpoint_pattern/
require_code/confirm_any/exclude; and "patterns"/"fingerprints" -- broader regex/
substring corpora) plus a handful of hard-coded guards to decide whether a given
HTTP response actually confirms a vulnerability, without ever calling an LLM. This
is the deterministic counterpart to reflect()'s LLM-based judgment: reflect() can
still be wrong about whether a 200 response really confirms an exploit (that's
exactly the class of bug fixed earlier today with the HTTP-status grounding rule);
a rules-based classifier gives a second, non-LLM opinion for the same question.
"""

import os
import re
import json
from typing import Any, Dict, List, Optional, Tuple


class ResponseClassifier:
    """Single-call, deterministic vulnerability classifier.

    Returns one of:
        "CONFIRMED"      -- matches confirm patterns, status in require_code
        "FALSE_POSITIVE" -- same as baseline, is an error page, or excluded
        "NEEDS_REVIEW"   -- anomalous size/content but no pattern match

    Never loops. Never calls an LLM. One call -> one verdict.
    """

    ERROR_PAGE_SIGNALS = [
        "Unexpected path:",
        "Error: Unexpected",
        "Cannot GET",
        "Cannot POST",
        "ENOTFOUND",
        "ECONNREFUSED",
        "404 Not Found",
    ]

    DATABASE_ERROR_SIGNALS = [
        "sequelizedatabaseerror",
        "syntax error in sql",
        "unclosed quotation mark",
        "sqlite3.operationalerror",
        "ora-00933",
        "pg::syntaxerror",
        "sql syntax",
    ]

    SPA_SIGNALS = [
        "<title>owasp juice shop</title>",
        "<app-root>",
        "<!doctype html",
        "<html",
        "<body",
        "<style",
        "background: #ece9e9",
        "-webkit-gradient",
        "runtime.js",
        "main.js",
        "polyfills.js",
    ]

    FALSE_POSITIVE_STATUSES = {500, 502, 503, 504}

    def __init__(self, rules_path: str = None, include_generic_corpus: bool = False):
        """
        include_generic_corpus=False (default) excludes rules with no "id" field --
        verified by testing (a real captured 401 error page was misclassified as
        CONFIRMED) that the unlabeled "Schema B" generic corpus in
        detection_rules.json has overly-broad patterns (bare [A-Z], [a-z], (\\d+),
        etc. -- these look borrowed from a different tool's token-extraction use
        case, not tuned for vulnerability confirmation) that produce false
        positives on almost any text. The curated, id-labeled rules (the
        "juiceshop_*" ones, hand-written for this engagement's specific endpoints)
        tested clean. Pass include_generic_corpus=True only after that corpus has
        actually been reviewed/tightened.
        """
        self._rules: List[Dict[str, Any]] = []
        if rules_path is None:
            # tools/response_classifier.py -> tools/ -> project root -> config/detection_rules.json
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            rules_path = os.path.join(project_root, "config", "detection_rules.json")
        if os.path.exists(rules_path):
            try:
                with open(rules_path) as f:
                    all_rules = json.load(f).get("rules", [])
                self._rules = all_rules if include_generic_corpus else [r for r in all_rules if "id" in r]
            except Exception:
                pass

    def classify(
        self,
        finding: Dict[str, Any],
        resp_body: str,
        resp_status: int,
        baseline: Optional[Dict[str, Any]] = None,
    ) -> str:
        verdict, _ = self.classify_detailed(finding, resp_body, resp_status, baseline)
        return verdict

    def classify_detailed(
        self,
        finding: Dict[str, Any],
        resp_body: str,
        resp_status: int,
        baseline: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[str]]:
        vuln_type = (finding.get("vuln_type") or "").lower()
        resp_size = len(resp_body)
        matched_patterns: List[str] = []

        body_lower = resp_body.lower()
        url = (finding.get("url") or "").lower()

        for sig in self.ERROR_PAGE_SIGNALS:
            if sig.lower() in body_lower:
                return "FALSE_POSITIVE", []

        # SPA fallback check: requesting an API/REST endpoint but getting HTML SPA homepage shell
        if any(sig in body_lower for sig in self.SPA_SIGNALS) and ("/api/" in url or "/rest/" in url):
            return "FALSE_POSITIVE", ["spa_route_fallback"]

        # Database error reflection in injection testing: evidence of SQL error disclosure, not false positive
        if any(sig in body_lower for sig in self.DATABASE_ERROR_SIGNALS):
            return "NEEDS_REVIEW", ["database_error_reflection"]

        if baseline:
            if resp_status == baseline.get("status") and resp_status != 200:
                return "FALSE_POSITIVE", []
            b_size = baseline.get("resp_size", 0)
            if b_size > 0 and abs(resp_size - b_size) / b_size < 0.20:
                if resp_status == baseline.get("status"):
                    return "FALSE_POSITIVE", []
            if baseline.get("status") in (401, 403) and resp_status == 200:
                return "CONFIRMED", ["auth_barrier_broken"]

        if resp_status in self.FALSE_POSITIVE_STATUSES:
            return "FALSE_POSITIVE", []

        for rule in self._rules:
            rule_vtype = (rule.get("vuln_type") or rule.get("id") or "").lower()
            if vuln_type not in rule_vtype and rule_vtype not in vuln_type:
                continue

            if "confirm_any" in rule:
                if resp_status not in rule.get("require_code", [resp_status]):
                    continue
                excludes = rule.get("exclude", [])
                if any(ex in resp_body for ex in excludes):
                    continue
                for pat in rule.get("confirm_any", []):
                    try:
                        if re.search(pat, resp_body):
                            matched_patterns.append(pat)
                    except re.error:
                        if pat in resp_body:
                            matched_patterns.append(pat)

            if "patterns" in rule:
                for pat in rule.get("patterns", []):
                    try:
                        if re.search(pat, resp_body):
                            matched_patterns.append(pat)
                    except re.error:
                        if pat in resp_body:
                            matched_patterns.append(pat)
                for fp in rule.get("fingerprints", []):
                    if fp in resp_body:
                        matched_patterns.append(fp)

        if matched_patterns:
            return "CONFIRMED", matched_patterns

        if baseline:
            b_size = baseline.get("resp_size", 0)
            if b_size > 0 and resp_size > b_size * 2.5 and resp_status == 200:
                return "NEEDS_REVIEW", []

        if resp_status == 200 and baseline and baseline.get("status") in (401, 403):
            return "CONFIRMED", ["auth_barrier_broken_200"]

        return "FALSE_POSITIVE", []
