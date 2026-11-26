#!/usr/bin/env python3
"""
prompt_sabrina_recon_build.py
-----------------------------
Prompts Sabrina (qwen2.5:7b via lab Ollama) to write:
  1. A ReconAnt that fetches a target's JS bundle and extracts endpoints into structured JSON
  2. A Blackboard state manager that stores and deduplicates the attack surface catalog

We collect the output, validate it, and save it as the implementation files.
"""
import paramiko
import time
import json
import sys

LAB = "192.168.2.69"
USER = "ego"
PASS = "ego"

# ── helpers ──────────────────────────────────────────────────────────────────

def ssh_connect():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(LAB, username=USER, password=PASS, timeout=10,
                   look_for_keys=False, allow_agent=False)
    return client


def ask_ollama(client, model: str, system: str, user: str, timeout: int = 120) -> str:
    """Send a single chat turn to the lab's Ollama instance and return the text reply."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user}
        ],
        "stream": False,
        "options": {"temperature": 0.15, "num_predict": 2048}
    })
    # Write payload to a temp file then curl it to avoid shell-quoting issues
    cmd = (
        f"echo '{payload}' | "
        f"curl -s -X POST http://127.0.0.1:11434/api/chat "
        f"-H 'Content-Type: application/json' -d @-"
    )
    chan = client.get_transport().open_session()
    chan.set_combine_stderr(True)
    chan.exec_command(cmd)
    out = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chan.recv_ready():
            out.append(chan.recv(16384).decode("utf-8", errors="replace"))
        if chan.exit_status_ready():
            while chan.recv_ready():
                out.append(chan.recv(16384).decode("utf-8", errors="replace"))
            break
        time.sleep(0.3)
    chan.close()
    raw = "".join(out).strip()
    try:
        return json.loads(raw)["message"]["content"]
    except Exception:
        return raw  # return whatever we got


# ── prompts ──────────────────────────────────────────────────────────────────

SYSTEM_CODE_GEN = (
    "You are Sabrina, a senior Python security engineer embedded in an autonomous "
    "penetration testing framework. When asked to write code, you produce ONLY "
    "complete, runnable Python 3 code with no markdown fences, no explanation text "
    "before or after the code block. The code must be self-contained and importable."
)

PROMPT_RECON_ANT = """\
Write a Python 3 module called `recon_ant.py`.

The module must:
1. Define a dataclass `DiscoveredEndpoint` with fields:
   - path: str
   - method: str  (default "GET")
   - params: list  (field names or path vars, default empty list)
   - param_type: str  (one of: "query", "body_json", "path_variable", "unknown", default "unknown")
   - source: str  (where we found it, default "unknown")

2. Define a function `extract_endpoints_from_text(text: str) -> list[DiscoveredEndpoint]`
   that sends `text` as a user message to qwen2.5:7b running on http://127.0.0.1:11434/api/chat
   with a tightly-scoped system prompt instructing the model to output ONLY a JSON array
   of objects with keys: path, method, params, param_type.
   Parse the JSON reply and return a list of DiscoveredEndpoint objects.
   On any JSON parse error, return an empty list and print a warning.

3. Define a function `crawl_js_bundles(base_url: str) -> list[DiscoveredEndpoint]`
   that:
   a. Fetches `base_url` HTML via urllib.request
   b. Extracts all <script src="..."> paths
   c. Fetches each JS bundle (skip cross-origin)
   d. Calls extract_endpoints_from_text() on each bundle's text
   e. Also checks /api-docs, /swagger.json, /openapi.json for a JSON spec
      and calls extract_endpoints_from_text() on any found spec
   f. Returns a deduplicated list of DiscoveredEndpoint (deduplicate on path+method)

4. A `if __name__ == "__main__"` block that calls crawl_js_bundles("http://127.0.0.1:3000")
   and pretty-prints each discovered endpoint.

Use only stdlib (urllib, json, re, dataclasses). No third-party deps.
"""

PROMPT_BLACKBOARD = """\
Write a Python 3 module called `blackboard.py`.

The module must:
1. Import DiscoveredEndpoint from recon_ant (assume it is in the same directory)

2. Define a thread-safe class `Blackboard` with:
   - An internal dict `attack_surface: dict[str, dict]` keyed by "METHOD:path"
   - A threading.Lock for all mutations
   - Method `register(endpoint: DiscoveredEndpoint) -> bool`
     Adds endpoint if not already present. Returns True if new, False if duplicate.
   - Method `get_untested(vuln_type: str) -> list[dict]`
     Returns endpoints that have NOT been tested for the given vuln_type yet.
   - Method `mark_tested(path: str, method: str, vuln_type: str)`
     Records that a given endpoint has been tested for a vuln_type.
   - Method `to_json() -> str`
     Returns the full attack_surface as pretty-printed JSON.
   - Method `load_json(data: str)`
     Loads a JSON string (from to_json()) back into attack_surface, merging with existing.

3. A module-level singleton `BLACKBOARD = Blackboard()` so workers can import and use it directly.

4. A `if __name__ == "__main__"` self-test that:
   - Creates a few DiscoveredEndpoint objects manually
   - Registers them
   - Calls get_untested("sqli") and prints results
   - Marks one tested and calls get_untested("sqli") again
   - Prints the JSON dump

Use only stdlib (json, threading, dataclasses). No third-party deps.
"""

PROMPT_GROUNDED_TASK = """\
Write a Python 3 function `build_grounded_probe_prompt(endpoint: dict, vuln_type: str) -> str`.

`endpoint` is a dict with keys: path, method, params, param_type.
`vuln_type` is a string like "sqli", "xss", "idor", "auth_bypass".

The function builds and returns a detailed LLM prompt string that:
1. States the exact target URL (base_url = "http://127.0.0.1:3000" + endpoint["path"])
2. States the HTTP method
3. States each parameter and its type (query, body_json, or path_variable)
4. States the specific vulnerability to test
5. Instructs the model to write a self-contained Python 3 script using only urllib
   that sends the actual probe request with appropriate payloads for that vuln_type
   and prints the HTTP status code and first 500 chars of the response body.

Include a `VULN_PAYLOADS` dict mapping each vuln_type to a list of 3 representative payloads.

Put everything in a standalone file called `grounded_task_builder.py`.
Add a `if __name__ == "__main__"` demo that prints a sample prompt for sqli on
{"path": "/rest/user/login", "method": "POST", "params": ["email","password"], "param_type": "body_json"}.
"""


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    model = "qwen2.5:7b"
    print(f"Connecting to lab ({LAB})...")
    client = ssh_connect()
    print("Connected.\n")

    tasks = [
        ("recon_ant.py",            PROMPT_RECON_ANT,   "ReconAnt module"),
        ("blackboard.py",           PROMPT_BLACKBOARD,  "Blackboard state manager"),
        ("grounded_task_builder.py",PROMPT_GROUNDED_TASK,"Grounded task prompt builder"),
    ]

    results = {}
    for filename, prompt, label in tasks:
        print(f"{'='*60}")
        print(f"Prompting Sabrina to write: {label}")
        print(f"{'='*60}")
        code = ask_ollama(client, model, SYSTEM_CODE_GEN, prompt, timeout=180)
        results[filename] = code
        # Show first 40 lines of what she produced
        lines = code.splitlines()
        preview = "\n".join(lines[:40])
        print(f"[{label} — first 40 lines]\n{preview}")
        if len(lines) > 40:
            print(f"... ({len(lines)} total lines)")
        print()

    client.close()

    # Save all generated files locally for review
    base_dir = r"c:\Users\bcmcg\Desktop\bot\Sabrina\tools"
    import os
    for filename, code in results.items():
        path = os.path.join(base_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        print(f"Saved: {path}")

    print("\nDone. Review generated files in tools/ before deploying.")

if __name__ == "__main__":
    main()
