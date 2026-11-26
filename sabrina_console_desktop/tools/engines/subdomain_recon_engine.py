import requests
import socket
import json
import time
import re

TOOL_NAME = "subdomain_recon_plugin"
TOOL_PROMPT = """
"subdomain_recon_plugin": Performs passive subdomain enumeration using crt.sh.
Format: {"tool": "subdomain_recon_plugin", "root_domain": "example.com"}
"""

def execute(action_dict, agent_instance):
    """
    Performs passive subdomain reconnaissance using crt.sh.
    Does NOT perform active HTTP requests to the target.
    """
    root_domain = action_dict.get("root_domain")
    if not root_domain:
        return "Error: 'root_domain' is required."

    url = f"https://crt.sh/?q=%.{root_domain}&output=json"
    headers = {"User-Agent": "Mozilla/5.0 (Security-Recon-Tool/1.0)"}

    subdomains = set()

    # 1. Query crt.sh with retry logic
    try:
        response = None
        for attempt in range(2):
            try:
                response = requests.get(url, headers=headers, timeout=15)
                if response.status_code == 200:
                    break
            except requests.exceptions.RequestException:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return "Error: Failed to connect to crt.sh after retries."

        if not response or response.status_code != 200:
            return f"Error: crt.sh returned status {response.status_code if response else 'None'}"

        data = response.json()

        # 2. Parse and dedupe
        # name_value can contain multiple domains separated by newlines
        for entry in data:
            name_value = entry.get("name_value", "")
            # Split by newline and clean up
            parts = name_value.split('\n')
            for part in parts:
                domain = part.strip().lower()
                # Ensure it's a subdomain of the root
                if domain.endswith(root_domain.lower()):
                    subdomains.add(domain)

    except Exception as e:
        return f"Error parsing response: {str(e)}"

    # 3. Passive DNS Resolution check
    resolved_count = 0
    results_list = []

    for domain in sorted(list(subdomains)):
        resolves = False
        try:
            # Passive DNS lookup (socket.gethostbyname)
            socket.gethostbyname(domain)
            resolves = True
            resolved_count += 1
        except socket.gaierror:
            pass

        results_list.append(f"{domain} (Resolves: {resolves})")

    # 4. Format Output
    output = [
        "RECON ONLY -- not yet authorized for active testing. Cross-reference against the program's real published scope before sending any live HTTP request to any of these.",
        f"Root Domain: {root_domain}",
        f"Total Unique Subdomains Found: {len(subdomains)}",
        f"Total Resolving Subdomains: {resolved_count}",
        "---",
        "\n".join(results_list)
    ]

    return "\n".join(output)
