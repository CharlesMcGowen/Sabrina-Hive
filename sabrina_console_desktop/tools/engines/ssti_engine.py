import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import gate_guarded_request as ggr, is_url_allowed  # noqa: E402

def ssti_engine(json_input):
    data = json.loads(json_input)
    base_url = data['target_url']
    endpoint = data['endpoint']
    method = data['method']
    headers = data.get('headers', {})
    body = data.get('body', {})

    # SCOPE GATE (fail-closed): SSTI probes can trigger server-side code execution -- never fire at a
    # host outside the session's authorized scope. is_url_allowed reads the trusted env scope only.
    target = base_url + endpoint
    allowed, reason = is_url_allowed(target)
    if not allowed:
        return {"confirmed": False, "scanned": False, "blocked_reason": reason}

    payload_patterns = [
        "{{7*7}}",
        "${7*7}",
        "{% 7 * 7 %}"
    ]

    for param, value in body.items():
        for pattern in payload_patterns:
            test_payload = {param: re.sub(r"(?<!\\)\b" + re.escape(param) + r"\b", pattern, json.dumps(value))}
            response = ggr(method, target, headers=headers, json=test_payload)
            if response is None:
                continue  # gate blocked this request -> no traffic sent
            if "49" in response.text or "49" in response.json():
                return {
                    "confirmed": True,
                    "successful_payload": test_payload[param],
                    "evidence": f"{response.status_code} {response.reason} | {response.text}"
                }
    return {"confirmed": False, "successful_payload": None, "evidence": ""}

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python ssti_engine.py <json_input>")
        sys.exit(1)

    result = ssti_engine(sys.argv[1])
    print(json.dumps(result))