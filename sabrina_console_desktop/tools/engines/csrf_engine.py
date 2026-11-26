"""
tools/engines/csrf_engine.py
==============================
Cross-Site Request Forgery (CSRF) detection engine.

Usage: python tools/engines/csrf_engine.py '{"target_url": "http://host:port", "endpoint": "/api/state-change", "method": "POST", "headers": {"Cookie": "session=..."}, "body": {"field": "value"}, "scope_file": "juiceshop_scope.json"}'

Why this exists: one of several real, verified gaps found by cross-referencing
Sabrina's 9 existing engines against a corpus of real HackerOne top-report bug
categories -- CSRF had zero coverage despite being a common, well-established
real-world finding class.

Methodology (the two standard, generic checks that don't require knowing a
site's specific CSRF token field name in advance):
  1. FORGED ORIGIN: resend the exact same authenticated, state-changing request
     with an attacker-controlled Origin/Referer header. If the server doesn't
     validate Origin/Referer, the response looks the same as the legitimate
     baseline -- that's the actual cross-site attack vector (a real attacker
     page's cross-origin request would also carry a foreign Origin).
  2. TOKEN REMOVAL: if the request body contains a field that looks like a CSRF
     token (csrf, xsrf, authenticity_token, _token, csrfmiddlewaretoken, etc.,
     case-insensitive), resend the request with that field stripped out. If the
     server still processes it as if authenticated/valid, the token isn't
     actually being enforced server-side.

Both tests compare against a BASELINE request (the exact request as given,
unmodified) to establish what "success" looks like for this specific endpoint,
rather than assuming any particular status code means success.
"""

import sys
import os
import re
import json
import requests

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.engagement_scope import EngagementGate
from tools.gate_guarded_request import is_url_allowed

TOKEN_FIELD_PATTERN = re.compile(r"csrf|xsrf|authenticity_token|_token|csrfmiddlewaretoken", re.IGNORECASE)


def _similar_response(baseline: requests.Response, candidate: requests.Response) -> bool:
    if baseline.status_code != candidate.status_code:
        return False
    b_len, c_len = len(baseline.content), len(candidate.content)
    if b_len == 0:
        return c_len == 0
    return abs(b_len - c_len) / b_len < 0.10  # within 10% -- allows for timestamps/nonces in the body


def run(data: dict) -> dict:
    target_url = data.get("target_url", "").rstrip("/")
    endpoint = data.get("endpoint", "")
    method = data.get("method", "POST").upper()
    headers = dict(data.get("headers") or {})
    body = data.get("body") or {}
    scope_file = data.get("scope_file")
    full_url = target_url + endpoint

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

    # TRUSTED-CHANNEL gate (authoritative): the caller-supplied scope_file above can only NARROW, never
    # widen -- the request must ALSO be allowed by the session's authorized scope (SABRINA_ACTIVE_SCOPE_FILE),
    # so a model can't broaden scope via a permissive scope_file arg. Fail-closed if no trusted scope.
    trusted_ok, trusted_reason = is_url_allowed(full_url)
    if not trusted_ok:
        return {"confirmed": False, "blocked_reason": f"trusted-scope: {trusted_reason}"}

    results = {"confirmed": False, "target": full_url, "checks": {}}

    gate.throttle()
    try:
        baseline = requests.request(method, full_url, headers=headers, json=body if body else None, timeout=15)
    except requests.exceptions.RequestException as e:
        return {"confirmed": False, "target": full_url, "error": f"Baseline request failed: {e}"}
    results["baseline_status"] = baseline.status_code

    # Verified via live testing against Juice Shop with no real auth cookie: if the baseline
    # itself fails (e.g. 401 for lack of auth), EVERY subsequent tampered request also fails
    # for that same unrelated reason, and "looks the same as baseline" becomes trivially true
    # -- producing a false "confirmed" CSRF finding that has nothing to do with CSRF at all.
    # The comparison only means anything if the baseline represents a real, successful,
    # authenticated state change.
    if not (200 <= baseline.status_code < 300):
        return {
            "confirmed": False,
            "target": full_url,
            "baseline_status": baseline.status_code,
            "note": (
                f"Baseline request did not succeed (status {baseline.status_code}) -- cannot "
                f"meaningfully test CSRF without a working authenticated baseline that actually "
                f"performs the state change. Provide valid session cookies/headers and retry."
            ),
        }

    # Check 1: forged Origin/Referer
    gate.throttle()
    forged_headers = dict(headers)
    forged_headers["Origin"] = "https://attacker-controlled-example.com"
    forged_headers["Referer"] = "https://attacker-controlled-example.com/"
    try:
        forged_resp = requests.request(method, full_url, headers=forged_headers, json=body if body else None, timeout=15)
        origin_vulnerable = _similar_response(baseline, forged_resp)
        results["checks"]["forged_origin"] = {
            "vulnerable": origin_vulnerable,
            "status_code": forged_resp.status_code,
            "note": "Server accepted the request with a foreign Origin/Referer, same as the legitimate baseline." if origin_vulnerable else "Server's response differed with a foreign Origin/Referer -- likely validates it.",
        }
    except requests.exceptions.RequestException as e:
        results["checks"]["forged_origin"] = {"vulnerable": False, "error": str(e)}

    # Check 2: token removal (only if a token-shaped field exists in the body)
    token_fields = [k for k in body.keys() if TOKEN_FIELD_PATTERN.search(k)] if isinstance(body, dict) else []
    if token_fields:
        gate.throttle()
        stripped_body = {k: v for k, v in body.items() if k not in token_fields}
        try:
            stripped_resp = requests.request(method, full_url, headers=headers, json=stripped_body, timeout=15)
            token_vulnerable = _similar_response(baseline, stripped_resp)
            results["checks"]["token_removed"] = {
                "vulnerable": token_vulnerable,
                "removed_fields": token_fields,
                "status_code": stripped_resp.status_code,
                "note": "Server accepted the request without the token field, same as baseline." if token_vulnerable else "Server rejected/changed behavior without the token field -- likely enforces it.",
            }
        except requests.exceptions.RequestException as e:
            results["checks"]["token_removed"] = {"vulnerable": False, "error": str(e)}
    else:
        results["checks"]["token_removed"] = {"vulnerable": None, "note": "No token-shaped field found in body to test removal of."}

    any_vulnerable = any(c.get("vulnerable") for c in results["checks"].values())
    results["confirmed"] = any_vulnerable
    if any_vulnerable:
        vulnerable_checks = [name for name, c in results["checks"].items() if c.get("vulnerable")]
        results["summary"] = (
            f"Possible CSRF on {full_url}: {', '.join(vulnerable_checks)} check(s) suggest the server doesn't "
            f"validate Origin/Referer and/or doesn't enforce a token. Verify manually with a real cross-origin "
            f"HTML PoC before reporting -- this heuristic can false-positive on endpoints that are read-only or "
            f"idempotent regardless of caller."
        )
    else:
        results["summary"] = f"No CSRF signal on {full_url} -- Origin/Referer and/or token appear to be enforced."

    return results


def main():
    if len(sys.argv) != 2:
        raise ValueError("Please provide a JSON string as the first argument.")
    data = json.loads(sys.argv[1])
    output = run(data)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
