import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import gate_guarded_request as ggr, is_url_allowed  # noqa: E402

def main():
    import requests
    from urllib.parse import urlparse, urlunparse

    # Parse JSON input
    data = json.loads(sys.argv[1])

    target_url = data.get("target_url")
    endpoint = data.get("endpoint")
    method = data.get("method", "GET").upper()
    headers = data.get("headers", {})
    body = data.get("body", {})

    # Extract base URL and endpoint
    parsed_url = urlparse(target_url)
    base_url = urlunparse((parsed_url.scheme, parsed_url.netloc, '', '', '', ''))

    # SCOPE GATE (fail-closed): only probe an authorized host.
    allowed, reason = is_url_allowed(f"{base_url}{endpoint}")
    if not allowed:
        print(json.dumps({"confirmed": False, "scanned": False, "blocked_reason": reason}))
        sys.exit(0)

    # Define payloads for open_redirect
    payloads = ["http://evil.com", "https://evil.com", "evil.com"]
    
    # Iterate over parameters in body to find potential redirect parameter names
    params = {k: v for k, v in body.items() if any(p in k.lower() for p in ['next', 'redirect', 'url'])}
    
    for param_name, value in params.items():
        for payload in payloads:
            # Construct the full URL with injected payload
            modified_body = body.copy()
            modified_body[param_name] = payload
            
            try:
                response = ggr(method, f"{base_url}{endpoint}", headers=headers, json=modified_body)
                if response is None:
                    continue  # gate blocked -> no traffic
                if response.status_code in [301, 302] and payload in response.headers.get("Location", ""):
                    print(json.dumps({
                        "confirmed": True,
                        "successful_payload": {param_name: payload},
                        "evidence": f"{response.status_code} | {response.text}"
                    }))
                    sys.exit(0)
            except requests.RequestException as e:
                print(json.dumps({"error": str(e)}))
                sys.exit(1)

if __name__ == "__main__":
    main()