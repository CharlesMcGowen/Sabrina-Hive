import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import gate_guarded_request as ggr, is_url_allowed  # noqa: E402

def main():
    # Parse the JSON input from command line argument
    data = json.loads(sys.argv[1])

    target_url = data["target_url"]
    endpoint = data["endpoint"]
    method = data["method"]
    headers = data["headers"] if "headers" in data else {}
    body = data["body"]

    # SCOPE GATE (fail-closed): never attempt a webshell upload to an out-of-scope host.
    allowed, reason = is_url_allowed(target_url + endpoint)
    if not allowed:
        print(json.dumps({"confirmed": False, "scanned": False, "blocked_reason": reason}))
        sys.exit(0)

    # Define payloads for file upload vulnerability
    payloads = [
        ("shell.php", "<?php echo 'Hello, World!'; ?>"),
        ("shell.jsp", "<% out.println('Hello, World!'); %>")
    ]

    # Iterate over parameters that might be files
    for param_name in body:
        if isinstance(body[param_name], str) and any(ext in body[param_name] for ext in [".php", ".jsp"]):
            for file_ext, payload_content in payloads:
                full_payload = {param_name: (file_ext, payload_content.encode(), "application/x-php")}
                response = ggr(method, target_url + endpoint, files=full_payload, headers=headers)
                if response is None:
                    continue  # gate blocked -> no traffic
                if re.search(r"Hello, World!", response.text):
                    print(json.dumps({
                        "confirmed": True,
                        "successful_payload": full_payload[param_name][0],
                        "evidence": f"{response.status_code} {response.reason} | {response.text}"
                    }))
                    sys.exit(0)

    print(json.dumps({"confirmed": False}))
    sys.exit(0)

if __name__ == "__main__":
    main()