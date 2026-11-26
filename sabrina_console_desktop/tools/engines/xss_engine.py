import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import gate_guarded_request as ggr, is_url_allowed  # noqa: E402

def main():
    if len(sys.argv) != 2:
        print("Usage: python xss_engine.py <input_json>")
        return

    input_data = json.loads(sys.argv[1])

    target_url = input_data.get("target_url")
    endpoint = input_data.get("endpoint", "")
    method = input_data.get("method", "GET").upper()
    headers = input_data.get("headers", {})
    body = input_data.get("body", {})

    if not all([target_url, method in ["GET", "POST"]]):
        print("Invalid target URL or method provided.")
        return

    # SCOPE GATE (fail-closed): reflected-XSS probes only against an authorized host.
    allowed, reason = is_url_allowed(target_url + endpoint)
    if not allowed:
        print(json.dumps({"confirmed": False, "scanned": False, "blocked_reason": reason}))
        return

    # Define robust XSS payloads
    xss_payloads = [
        '<script>alert(1)</script>',
        "<x onerror=alert(/XSS/);>",
        '"><img/onerror=alert(1)>',
        '"><iframe src="javascript:alert(1)">'
    ]
    
    xss_patterns = [
        re.compile(r"<script>alert\(1\)</script>", re.IGNORECASE),
        re.compile(r"onerror=alert", re.IGNORECASE),
        re.compile(r"javascript:alert", re.IGNORECASE),
        re.compile(r"/XSS/", re.IGNORECASE)
    ]
    
    params_to_test = list(body.keys()) if body else ["q"]
    for param in params_to_test:
        for payload in xss_payloads:
            try:
                if method == "POST":
                    test_body = {**body, param: payload}
                    response = ggr("POST", target_url + endpoint, json=test_body, headers=headers, timeout=10)
                else:
                    response = ggr("GET", target_url + endpoint, params={param: payload}, headers=headers, timeout=10)

                if response is None:
                    continue  # gate blocked -> no traffic
                if payload in response.text or any(p.search(response.text) for p in xss_patterns):
                    print(json.dumps({
                        "confirmed": True,
                        "successful_payload": payload,
                        "evidence": f"{response.status_code} {response.reason} | {response.text[:200]}"
                    }))
                    return
            except Exception:
                continue
    
    print(json.dumps({"confirmed": False, "successful_payload": None, "evidence": ""}))

if __name__ == "__main__":
    main()