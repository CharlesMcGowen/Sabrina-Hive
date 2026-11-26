#!/usr/bin/env python3
"""
tools/detection_engine.py
==========================
Two-tier detection engine for Sabrina.

Tier 1 — Regex fast-path:
    Sabrina owns config/detection_rules.json.
    Each rule defines endpoint_pattern, confirm_any/exclude regex lists.
    If any confirm_any pattern matches (and no exclude pattern matches) → confirmed.

Tier 2 — Semantic fallback:
    If regex produces no result AND rule.fallback_to_model is True,
    send the raw HTTP response to the local LLM for semantic analysis.
    Model must return JSON or "NO_FINDING".

Usage:
    from tools.detection_engine import DetectionEngine, ProbeResult

    engine = DetectionEngine()
    result = engine.analyze(
        url="http://target/api/Users/3",
        method="GET",
        status_code=200,
        body='{"email":"admin@juice-sh.op","role":"admin"}',
        headers={}
    )
    if result:
        print(result.to_dict())
"""

import re
import json
import urllib.request
import urllib.error
import pathlib
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)

# Default rules file — relative to this file's parent (project root)
_DEFAULT_RULES = pathlib.Path(__file__).parent.parent / "config" / "detection_rules.json"

# Ollama chat endpoint — uses same model infrastructure as the rest of Sabrina
_OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """Raw HTTP probe result passed into the detection engine."""
    url: str
    method: str
    status_code: int
    body: str
    headers: dict = field(default_factory=dict)


@dataclass
class FindingResult:
    """
    A confirmed finding.
    detection_method: 'regex' | 'model' | 'regex+model'
    """
    vuln_type: str
    url: str
    endpoint_pattern: str
    severity: str
    evidence: str
    description: str
    detection_method: str          # how it was confirmed
    matched_rule_id: str = ""
    chained_from: str = ""         # set by caller when this finding pivots from another

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Detection engine
# ---------------------------------------------------------------------------

class DetectionEngine:
    """
    Loads Sabrina's detection_rules.json once.
    Call analyze() per HTTP probe response.
    """

    def __init__(self, rules_path: Optional[pathlib.Path] = None,
                 model: Optional[str] = None):
        self.rules_path = pathlib.Path(rules_path) if rules_path else _DEFAULT_RULES
        self._rules: list[dict] = []
        self._fallback_model: str = model or "llama3.3:latest"
        self._load_rules()

    # ------------------------------------------------------------------
    # Rule loading
    # ------------------------------------------------------------------

    def _load_rules(self):
        if not self.rules_path.exists():
            logger.warning(f"[DetectionEngine] Rules file not found: {self.rules_path}")
            return
        with open(self.rules_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        meta = data.get("_meta", {})
        if meta.get("fallback_model"):
            self._fallback_model = meta["fallback_model"]
        self._rules = data.get("rules", [])
        logger.info(f"[DetectionEngine] Loaded {len(self._rules)} rules from {self.rules_path}")

    def reload_rules(self):
        """Hot-reload rules without restarting — Sabrina can update the file mid-run."""
        self._rules = []
        self._load_rules()

    def learn_detection_rule(self, vuln_type: str, new_pattern: str = None, new_fingerprint: str = None) -> str:
        """ 
        Called dynamically by Sabrina when she discovers a new way to fingerprint a vulnerability.
        Persists the new signature into detection_rules.json and reloads the engine.
        """
        if not self.rules_path.exists():
            return f"Error: Rules file {self.rules_path} not found."
            
        try:
            with open(self.rules_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            rules = data.get("rules", [])
            updated = False
            
            for rule in rules:
                if rule.get("vuln_type") == vuln_type:
                    if "patterns" not in rule: rule["patterns"] = []
                    if "fingerprints" not in rule: rule["fingerprints"] = []
                    
                    if new_pattern and new_pattern not in rule["patterns"]:
                        rule["patterns"].append(new_pattern)
                        updated = True
                        
                    if new_fingerprint and new_fingerprint not in rule["fingerprints"]:
                        rule["fingerprints"].append(new_fingerprint)
                        updated = True
                    break
            else:
                # Vuln type doesn't exist, create a new rule entry
                new_rule = {
                    "vuln_type": vuln_type,
                    "severity": "High",
                    "patterns": [new_pattern] if new_pattern else [],
                    "fingerprints": [new_fingerprint] if new_fingerprint else []
                }
                rules.append(new_rule)
                data["rules"] = rules
                updated = True
                
            if updated:
                with open(self.rules_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
                self.reload_rules()
                logger.info(f"Learned new detection rule for {vuln_type}")
                return f"Rule learned successfully for {vuln_type}."
            else:
                return f"Pattern/fingerprint already known for {vuln_type}."
                
        except Exception as e:
            logger.error(f"Failed to learn rule: {e}")
            return f"Error learning rule: {str(e)}"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def analyze(self, url: str, method: str, status_code: int,
                body: str, headers: Optional[dict] = None) -> Optional[FindingResult]:
        """
        Run Tier 1 (regex) then Tier 2 (model) against a probe response.
        Returns FindingResult if confirmed, None otherwise.
        """
        probe = ProbeResult(
            url=url,
            method=method.upper(),
            status_code=status_code,
            body=body,
            headers=headers or {}
        )
        for rule in self._rules:
            result = self._apply_rule(rule, probe)
            if result is not None:
                return result
        return None

    # ------------------------------------------------------------------
    # Tier 1: Regex fast-path
    # ------------------------------------------------------------------

    def _apply_rule(self, rule: dict, probe: ProbeResult) -> Optional[FindingResult]:
        """Try one rule against a probe. Returns FindingResult or None."""

        # --- endpoint_pattern match ---
        ep_pat = rule.get("endpoint_pattern", "")
        if ep_pat and not re.search(ep_pat, probe.url):
            return None

        # --- method match ---
        rule_method = rule.get("method", "ANY").upper()
        if rule_method != "ANY" and rule_method != probe.method:
            return None

        # --- require_code check ---
        req_codes = rule.get("require_code", [])
        if req_codes and probe.status_code not in req_codes:
            return None

        # --- exclude check (FP suppression) ---
        for excl_pat in rule.get("exclude", []):
            if re.search(excl_pat, probe.body, re.IGNORECASE | re.DOTALL):
                logger.debug(f"[{rule['id']}] Excluded by pattern: {excl_pat!r}")
                return None

        # --- confirm_any regex ---
        matched_evidence = None
        for pat in rule.get("confirm_any", []):
            m = re.search(pat, probe.body, re.IGNORECASE | re.DOTALL)
            if m:
                matched_evidence = m.group(0)
                break

        if matched_evidence:
            logger.info(f"[{rule['id']}] REGEX CONFIRMED — {probe.url}")
            return FindingResult(
                vuln_type=rule["vuln_type"],
                url=probe.url,
                endpoint_pattern=rule.get("endpoint_pattern", ""),
                severity=rule.get("severity", "medium"),
                evidence=f"Regex match: {matched_evidence[:300]}",
                description=rule.get("notes", ""),
                detection_method="regex",
                matched_rule_id=rule.get("id", ""),
            )

        # --- No regex hit — check fallback ---
        if rule.get("fallback_to_model", False):
            return self._model_fallback(rule, probe)

        return None

    # ------------------------------------------------------------------
    # Tier 2: Semantic model fallback
    # ------------------------------------------------------------------

    def _model_fallback(self, rule: dict, probe: ProbeResult) -> Optional[FindingResult]:
        """
        Send the raw HTTP response to the LLM for semantic analysis.
        The model must return either:
          - A JSON object: {vuln_type, url, severity, evidence, description}
          - The string "NO_FINDING"
        """
        logger.info(f"[{rule['id']}] Regex produced no result — falling back to model review")

        system_prompt = (
            "You are Sabrina, an elite autonomous penetration tester. "
            "Your job is to determine whether an HTTP response confirms a successful exploit. "
            "Reply with ONLY a JSON object or the exact string NO_FINDING. "
            "No explanation. No markdown. No extra text."
        )

        user_prompt = (
            f"Vulnerability type being probed: {rule['vuln_type']}\n"
            f"Rule intent: {rule.get('notes', 'N/A')}\n\n"
            f"HTTP Request:\n"
            f"  URL: {probe.url}\n"
            f"  Method: {probe.method}\n\n"
            f"HTTP Response:\n"
            f"  Status: {probe.status_code}\n"
            f"  Body (first 1500 chars):\n{probe.body[:1500]}\n\n"
            f"If the vulnerability was successfully exploited, respond with a JSON object using "
            f"these exact keys: vuln_type, url, severity, evidence, description.\n"
            f"If it was NOT exploited, respond with exactly: NO_FINDING"
        )

        raw = self._call_model(system_prompt, user_prompt)
        if not raw or raw.strip() == "NO_FINDING":
            logger.info(f"[{rule['id']}] Model returned NO_FINDING")
            return None

        try:
            # Strip any accidental markdown fencing
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            data = json.loads(clean)
            logger.info(f"[{rule['id']}] MODEL CONFIRMED — {probe.url}")
            return FindingResult(
                vuln_type=data.get("vuln_type", rule["vuln_type"]),
                url=data.get("url", probe.url),
                endpoint_pattern=rule.get("endpoint_pattern", ""),
                severity=data.get("severity", rule.get("severity", "medium")),
                evidence=data.get("evidence", raw[:300]),
                description=data.get("description", rule.get("notes", "")),
                detection_method="model",
                matched_rule_id=rule.get("id", ""),
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"[{rule['id']}] Model returned unparseable output: {raw[:200]!r} — {e}")
            return None

    def _call_model(self, system: str, user: str) -> Optional[str]:
        """Non-streaming Ollama /api/chat call. Returns full response content string."""
        payload = {
            "model": self._fallback_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user}
            ],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 512}
        }
        try:
            req = urllib.request.Request(
                _OLLAMA_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
                return data.get("message", {}).get("content", "").strip()
        except urllib.error.URLError as e:
            logger.error(f"[DetectionEngine] Ollama unavailable: {e}")
            return None
        except Exception as e:
            logger.error(f"[DetectionEngine] Model call failed: {e}")
            return None


# ---------------------------------------------------------------------------
# Quick self-test (python -m tools.detection_engine)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    engine = DetectionEngine()
    print(f"Loaded {len(engine._rules)} rules\n")

    # --- Test 1: SQLi regex hit ---
    r1 = engine.analyze(
        url="http://192.168.2.69:3000/rest/user/login",
        method="POST",
        status_code=200,
        body='{"authentication":{"token":"eyJhbGci...","bid":1,"umail":"admin@juice-sh.op"}}'
    )
    print("Test 1 (SQLi regex):", r1.to_dict() if r1 else "None")

    # --- Test 2: IDOR regex hit ---
    r2 = engine.analyze(
        url="http://192.168.2.69:3000/api/Users/3",
        method="GET",
        status_code=200,
        body='{"id":3,"email":"bender@juice-sh.op","role":"customer","password":"0c36e517..."}'
    )
    print("\nTest 2 (IDOR regex):", r2.to_dict() if r2 else "None")

    # --- Test 3: Excluded FP ---
    r3 = engine.analyze(
        url="http://192.168.2.69:3000/rest/user/login",
        method="POST",
        status_code=401,
        body='{"error":"Wrong email or password"}'
    )
    print("\nTest 3 (excluded FP):", r3.to_dict() if r3 else "None (correct — should be None)")
