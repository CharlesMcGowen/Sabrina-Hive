import os
import sys

import dns.resolver
import requests
from requests.exceptions import RequestException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import is_url_allowed  # noqa: E402

# Fingerprint table: CNAME suffix -> Unclaimed signature
VULNERABLE_SERVICES = {
    "github.io": "There isn't a GitHub Pages site here",
    "herokuapp.com": "No such app",
    "s3.amazonaws.com": "NoSuchBucket",
    "azurewebsites.net": "Web App - Unavailable",
    "readthedocs.io": "Read the Docs could not find the project",
    "myshopify.com": "Sorry, this shop is currently unavailable"
}

def check_subdomain_takeover(subdomain):
    """
    Performs a takeover check on a subdomain.
    Returns: (status, message)
    """
    try:
        # 1. Resolve CNAME
        answers = dns.resolver.resolve(subdomain, 'CNAME')
        cname_target = str(answers[0].target).rstrip('.')
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return "NXDOMAIN", f"{subdomain} does not resolve (Potential dangling DNS record)"
    except Exception as e:
        return "ERROR", f"DNS lookup failed for {subdomain}: {str(e)}"

    # 2. Check against fingerprint table
    matched_service = None
    for service_suffix, signature in VULNERABLE_SERVICES.items():
        if cname_target.endswith(service_suffix):
            matched_service = (service_suffix, signature)
            break
    
    if not matched_service:
        return "SAFE", f"CNAME {cname_target} not in vulnerable fingerprint list."

    # 3. Verify via HTTP GET
    suffix, signature = matched_service
    for protocol in ['http', 'https']:
        url = f"{protocol}://{subdomain}"
        # SCOPE GATE (fail-closed): only HTTP-probe an authorized in-scope subdomain. (DNS resolution
        # above is passive; the active web request is what must be gated.)
        allowed, _reason = is_url_allowed(url)
        if not allowed:
            continue
        try:
            response = requests.get(url, timeout=5, allow_redirects=True)
            if signature in response.text:
                return "TAKEOVER_CONFIRMED", f"Confirmed: {subdomain} points to {cname_target} and shows '{signature}'"
        except RequestException:
            continue
            
    return "SAFE", f"CNAME {cname_target} points to a known service but appears claimed."

# Example usage integration:
# for sub in discovered_subdomains:
#     status, msg = check_subdomain_takeover(sub)
#     print(f"[{status}] {msg}")