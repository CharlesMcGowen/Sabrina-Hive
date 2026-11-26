#!/usr/bin/env python3
"""
tools/juiceshop_agent/challenges.py
==================================
The easy, HTTP-solvable Juice Shop challenge band for the STaR MVP.

Each entry is keyed by its real /api/Challenges `key` so the scoreboard reward
(reward.py) can attribute a solved-flag flip to it. `desc` gives the model the challenge
INTENT, never the exploit -- the point is to learn the solving action, not memorize an
answer key.

`multi_step` flags challenges that a single HTTP request cannot solve (they need a prior
step, e.g. IDOR needs a login token first). The feasibility probe confirmed the base 3B
throws genuine attempts at these but is blocked by the single-shot modality; the teacher
runs multi-step so it can actually solve and demonstrate them.

This band is deliberately small and expandable. Widen it (or regenerate from the live
scoreboard filtered to difficulty<=2, HTTP categories) once the MVP flywheel is proven.
"""
from typing import Dict, List

# vuln_class = the ground-truth class per challenge (used for the detector's positive-class
# label and the solver trace metadata). More reliable than parsing the model's <hypothesis>
# tag, which reasoning-model teachers suppress under think:false. Values stay within
# generate.VULN_CLASSES.
CHALLENGES: List[Dict] = [
    {"key": "directoryListingChallenge", "name": "Confidential Document",
     "desc": "Access a confidential document that should not be publicly reachable.",
     "vuln_class": "info_disclosure", "multi_step": False},
    {"key": "errorHandlingChallenge", "name": "Error Handling",
     "desc": "Provoke a server error that is not gracefully handled (send input of an unexpected type to an API).",
     "vuln_class": "misconfig", "multi_step": False},
    {"key": "loginAdminChallenge", "name": "Login Admin",
     "desc": "Log in to the administrator account on the login API, without knowing the password.",
     "vuln_class": "sqli", "multi_step": False},
    {"key": "exposedMetricsChallenge", "name": "Exposed Metrics",
     "desc": "Find the endpoint that serves Prometheus-style usage/metrics data to be scraped.",
     "vuln_class": "info_disclosure", "multi_step": False},
    {"key": "emptyUserRegistration", "name": "Empty User Registration",
     "desc": "Register a new user through the API with an empty email AND empty password.",
     "vuln_class": "misconfig", "multi_step": False},
    {"key": "securityPolicyChallenge", "name": "Security Policy",
     "desc": "Retrieve the standard machine-readable security policy file a white-hat would check first.",
     "vuln_class": "info_disclosure", "multi_step": False},
    {"key": "weirdCryptoChallenge", "name": "Weird Crypto",
     "desc": "Submit customer feedback that names an insecure cryptographic algorithm/library the shop should not use (e.g. MD5).",
     "vuln_class": "misconfig", "multi_step": True},
    {"key": "passwordRepeatChallenge", "name": "Repetitive Registration",
     "desc": "Register a user via the API where the repeated-password confirmation does not actually match the password.",
     "vuln_class": "misconfig", "multi_step": False},
    {"key": "basketAccessChallenge", "name": "View Basket",
     "desc": "Access another user's shopping basket by its id (IDOR on the basket REST endpoint).",
     "vuln_class": "idor", "multi_step": True},
    {"key": "deprecatedInterfaceChallenge", "name": "Deprecated Interface",
     "desc": "Use the file-upload B2B interface to upload a file of a deprecated/forbidden type (e.g. XML).",
     "vuln_class": "file_upload", "multi_step": True},
]

BY_KEY: Dict[str, Dict] = {c["key"]: c for c in CHALLENGES}


def http_only() -> List[Dict]:
    """Challenges solvable with a single HTTP request (probe/best-of-N friendly)."""
    return [c for c in CHALLENGES if not c.get("multi_step")]


def all_challenges() -> List[Dict]:
    return list(CHALLENGES)
