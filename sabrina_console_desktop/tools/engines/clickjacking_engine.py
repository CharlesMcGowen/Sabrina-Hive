"""
tools/engines/clickjacking_engine.py
======================================
Clickjacking (missing frame-busting protection) detection engine.

Usage: python tools/engines/clickjacking_engine.py '{"target_url": "http://host:port", "path": "/", "scope_file": "juiceshop_scope.json"}'

Why this exists: one of several real, verified gaps found by cross-referencing
Sabrina's 9 existing engines against a corpus of real HackerOne top-report bug
categories (27 categories total) -- clickjacking had zero coverage despite
being a common, easy-to-check real-world finding.

This is a pure header-inspection check: no payload, no exploitation, nothing
sent beyond a normal GET request. A page is potentially frame-able (clickjackable)
if it lacks BOTH:
  - X-Frame-Options: DENY or SAMEORIGIN
  - Content-Security-Policy with a frame-ancestors directive
A CONFIRMED result here is a real, low-effort finding worth reporting as-is
(unlike the smuggling/XXE engines, there's no further "manual verification"
step needed -- either the header is present or it isn't).
"""

import sys
import os
import json
import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.engagement_scope import EngagementGate
from tools.gate_guarded_request import is_url_allowed


def _has_frame_protection(headers: dict) -> tuple[bool, str]:
    # Header names arrive with whatever casing the server sent; requests'
    # CaseInsensitiveDict handles lookup, but we still normalize for the reason string.
    xfo = headers.get("X-Frame-Options", "")
    if xfo.strip().upper() in ("DENY", "SAMEORIGIN"):
        return True, f"X-Frame-Options: {xfo}"

    csp = headers.get("Content-Security-Policy", "")
    if "frame-ancestors" in csp.lower():
        return True, f"CSP frame-ancestors directive present: {csp[:150]}"

    return False, ""


def run(data: dict) -> dict:
    target_url = data.get("target_url", "").rstrip("/")
    path = data.get("path", "/")
    scope_file = data.get("scope_file")
    full_url = target_url + path

    if not scope_file:
        return {"confirmed": False, "blocked_reason": "No 'scope_file' provided -- refusing to probe without an explicit engagement scope to check against."}
    if not os.path.isfile(scope_file):
        return {"confirmed": False, "blocked_reason": f"scope_file '{scope_file}' not found."}

    with open(scope_file, "r", encoding="utf-8") as f:
        scope = json.load(f)
    gate = EngagementGate(scope)

    gate_ok, gate_reason = gate.check(full_url)
    if not gate_ok:
        return {"confirmed": False, "blocked_reason": gate_reason}

    # TRUSTED-CHANNEL gate (authoritative): caller scope_file can only NARROW; the request must also be
    # allowed by SABRINA_ACTIVE_SCOPE_FILE, so a permissive caller scope can't widen it. Fail-closed.
    trusted_ok, trusted_reason = is_url_allowed(full_url)
    if not trusted_ok:
        return {"confirmed": False, "blocked_reason": f"trusted-scope: {trusted_reason}"}

    gate.throttle()
    try:
        resp = requests.get(full_url, timeout=15, allow_redirects=True)
    except requests.exceptions.RequestException as e:
        return {"confirmed": False, "target": full_url, "error": str(e)}

    protected, evidence = _has_frame_protection(resp.headers)
    result = {
        "confirmed": not protected,
        "target": full_url,
        "final_url": resp.url,
        "status_code": resp.status_code,
        "protected": protected,
    }
    if protected:
        result["evidence"] = evidence
        result["summary"] = f"{full_url} is protected against clickjacking ({evidence})."
    else:
        result["summary"] = (
            f"CONFIRMED: {full_url} sets neither X-Frame-Options (DENY/SAMEORIGIN) nor a CSP "
            f"frame-ancestors directive -- the page can likely be embedded in an attacker-controlled "
            f"<iframe>, which is a real clickjacking finding as-is (no further payload/exploitation "
            f"needed to confirm this class of issue)."
        )
    return result


def main():
    if len(sys.argv) != 2:
        raise ValueError("Please provide a JSON string as the first argument.")
    data = json.loads(sys.argv[1])
    output = run(data)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
