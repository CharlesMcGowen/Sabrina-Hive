#!/usr/bin/env python3
"""
prompt_sabrina_build_architecture.py
------------------------------------
Orchestrates direct collaborative building with Sabrina (running on the lab host via Ollama).
Prompts Sabrina to architect, write, review, and self-test each module:
  1. verification_module.py (Completed & Verified)
  2. telemetry_prober.py   (Targeted HTTP prober with path deduplication & raw telemetry)
  3. modular_orchestrator.py (Clean, resident multi-model task dispatcher & runner)
"""
import paramiko
import json
import time
import sys
import os
import subprocess
import py_compile

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

LAB = "192.168.2.69"
USER = "ego"
PASS = "ego"

def chat_with_sabrina(messages: list, model: str = "qwen2.5:7b", timeout: int = 150) -> str:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(LAB, username=USER, password=PASS, timeout=10)
    
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0.15, "num_predict": 3000}
    }
    
    payload_raw = json.dumps(json.dumps(payload))
    remote_bridge = f'''import urllib.request, json, sys
payload = json.loads({payload_raw})
req = urllib.request.Request(
    "http://127.0.0.1:11434/api/chat",
    data=json.dumps(payload).encode("utf-8"),
    headers={{"Content-Type": "application/json"}}
)
try:
    with urllib.request.urlopen(req, timeout={timeout}) as r:
        res = json.loads(r.read().decode("utf-8"))
        print(res["message"]["content"])
except Exception as e:
    print(f"ERROR: {{e}}", file=sys.stderr)
'''
    sftp = client.open_sftp()
    with sftp.open('/tmp/sabrina_ask_runner.py', 'w') as f:
        f.write(remote_bridge)
    sftp.close()
    
    stdin, stdout, stderr = client.exec_command('python3 /tmp/sabrina_ask_runner.py')
    output = stdout.read().decode('utf-8', errors='replace').strip()
    client.close()
    
    # Extract Python code block cleanly
    if "```python" in output:
        output = output.split("```python", 1)[1]
        if "```" in output:
            output = output.rsplit("```", 1)[0]
    elif "```" in output:
        output = output.split("```", 1)[1]
        if "```" in output:
            output = output.rsplit("```", 1)[0]
    else:
        lines = output.splitlines()
        code_lines = []
        for line in lines:
            if line.startswith("This code defines") or line.startswith("Note:") or line.startswith("Here is"):
                continue
            code_lines.append(line)
        output = "\n".join(code_lines)

    return output.strip()

def collaborative_build(module_name: str, initial_prompt: str, target_local_path: str, max_rounds: int = 3):
    system_msg = (
        "You are Sabrina, an elite autonomous AI security engineer and system architect. "
        "You are collaborating with Antigravity to build a clean, modular multi-agent testing and verification framework. "
        "When writing code, produce ONLY complete, runnable Python 3 inside ```python fences. "
        "Put all self-test execution in the `if __name__ == '__main__':` block. "
        "Do not write any conversational text outside of python comments or docstrings."
    )
    
    conversation = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": initial_prompt}
    ]
    
    for round_num in range(1, max_rounds + 1):
        print(f"\n{'='*75}")
        print(f"[*] ROUND {round_num}/{max_rounds}: Prompting Sabrina for {module_name}...")
        print(f"{'='*75}")
        
        code = chat_with_sabrina(conversation)
        if not code or len(code) < 100:
            print("[-] Empty or invalid response from Sabrina.")
            return False
            
        print(f"[+] Sabrina replied with {len(code)} bytes of Python code.")
        
        # Save to local file
        with open(target_local_path, "w", encoding="utf-8") as f:
            f.write(code)
            
        # Review 1: Syntax compilation
        try:
            py_compile.compile(target_local_path, doraise=True)
            print(f"[+] Syntax check: PASSED.")
        except Exception as err:
            print(f"[-] Syntax check: FAILED with: {err}")
            feedback = (
                f"Antigravity Reviewer Feedback:\n"
                f"Your code failed Python syntax compilation with error:\n{err}\n\n"
                f"Please fix this syntax error and output the complete, corrected Python 3 file inside ```python fences."
            )
            conversation.append({"role": "assistant", "content": code})
            conversation.append({"role": "user", "content": feedback})
            continue
            
        # Review 2: Self-test execution
        print(f"[*] Running Sabrina's self-test ({target_local_path})...")
        run_res = subprocess.run([sys.executable, target_local_path], capture_output=True, text=True, timeout=25)
        stdout = run_res.stdout.strip()
        stderr = run_res.stderr.strip()
        
        print(f"[+] Self-test output:\n{stdout}")
        if stderr:
            print(f"[-] Self-test stderr:\n{stderr}")
            
        if run_res.returncode == 0 and "ALL TESTS PASSED" in stdout:
            print(f"\n[★] SUCCESS: Sabrina successfully built and verified {module_name}!")
            return True
        else:
            diag = ""
            if "name 'sys' is not defined" in stderr:
                diag = (
                    "\nCRITICAL DIAGNOSIS:\n"
                    "Your code raised NameError: name 'sys' is not defined.\n"
                    "You MUST include 'import sys' at the top of the file!\n"
                )
            elif "EndpointNormalizer" in stderr or "AssertionError" in stderr:
                diag = (
                    "\nCRITICAL DIAGNOSIS for EndpointNormalizer.normalize_target:\n"
                    "Your normalize_target returned a URL with '//' double slashes (e.g. 'http://localhost:3000//rest/admin').\n"
                    "This happened because ep_clean already starts with '/'. When you write:\n"
                    "    final_path = f'{base_path}/{ep_clean}'\n"
                    "it creates two slashes! Change it to:\n"
                    "    final_path = base_path + ep_clean\n"
                    "Do NOT put a slash between base_path and ep_clean!\n"
                )
            feedback = (
                f"Antigravity Reviewer Feedback:\n"
                f"Syntax compiled cleanly, but the self-test did not pass:\n"
                f"Return Code: {run_res.returncode}\n"
                f"STDOUT: {stdout}\n"
                f"STDERR: {stderr}\n"
                f"{diag}\n"
                f"Please address the test failure and output the complete, corrected Python 3 file inside ```python fences."
            )
            conversation.append({"role": "assistant", "content": code})
            conversation.append({"role": "user", "content": feedback})

    print(f"[-] Reached max rounds ({max_rounds}) without full pass.")
    return False

PROMPT_VERIFICATION_MODULE = """\
Sabrina, please build `verification_module.py`.

Requirements:
1. Define `class JSONSanitizer:` with method:
   @staticmethod
   def sanitize_and_load(raw_text: str) -> dict:
       # Attempts json.loads(raw_text)
       # If JSONDecodeError, strips markdown code blocks, normalizes quotes, and tries ast.literal_eval
       # Raises ValueError only if completely unparseable

2. Define `class VulnerabilityVerifier:`
   - Class variables:
     ERROR_SIGNALS = ["cannot get", "cannot post", "500 internal server error", "500 error:", "sequelizedatabaseerror", "unexpected path:"]
     SPA_SIGNALS = ["<title>owasp juice shop</title>", "<app-root>", "<!doctype html><html", "runtime.js", "main.js"]

   - @staticmethod
     def deterministic_check(finding: dict, resp_body: str, resp_status: int, baseline: dict = None) -> tuple:
       # Returns (verdict: str, reasons: list)
       # 1. If resp_status >= 500 or any error signal in resp_body.lower(): return ("FALSE_POSITIVE", ["error_signal_detected"])
       # 2. If any SPA signal in resp_body.lower() and ("/api/" in finding.get("url", "") or "/rest/" in finding.get("url", "")): return ("FALSE_POSITIVE", ["spa_route_fallback"])
       # 3. If baseline and baseline.get("status") in (401, 403) and resp_status == 200: return ("CONFIRMED", ["auth_barrier_broken"])
       # 4. If baseline and abs(len(resp_body) - baseline.get("resp_size", 0)) > 2000 and resp_status == 200: return ("NEEDS_REVIEW", ["size_delta_anomaly"])
       # 5. Otherwise return ("NEEDS_REVIEW", ["inconclusive"])

   - @staticmethod
     def cot_verify(finding: dict, resp_body: str, resp_status: int, baseline: dict = None, ollama_url: str = "http://127.0.0.1:11434", model: str = "qwen2.5:7b") -> dict:
       # First call deterministic_check. If verdict == "FALSE_POSITIVE":
       #   return {"verdict": "FALSE_POSITIVE", "confidence": 1.0, "reason": "; ".join(reasons)}
       # Otherwise build JSON payload for Ollama /api/chat:
       #   payload = {
       #       "model": model,
       #       "messages": [
       #           {"role": "system", "content": "You are an elite application security verification arbiter. Analyze candidate security findings against baseline and raw telemetry using chain-of-thought reasoning. Return ONLY valid JSON with keys: 'verdict' ('CONFIRMED' or 'FALSE_POSITIVE'), 'confidence' (float 0.0-1.0), 'reason' (string)."},
       #           {"role": "user", "content": f"Candidate: {finding}\\nStatus: {resp_status}\\nBody snippet: {resp_body[:1000]}\\nBaseline: {baseline}"}
       #       ],
       #       "stream": False,
       #       "options": {"temperature": 0.1}
       #   }
       # Send via urllib.request.Request(f"{ollama_url}/api/chat", data=json.dumps(payload).encode('utf-8'), headers={"Content-Type": "application/json"})
       # Read response, extract message content: json.loads(response.read().decode())["message"]["content"]
       # Parse message content with JSONSanitizer.sanitize_and_load.
       # On any exception, fallback to: {"verdict": det_verdict, "confidence": 0.6, "reason": "offline_fallback: " + "; ".join(reasons)}

3. `if __name__ == '__main__':` self-test block (must pass completely offline):
   - Test 1: assert JSONSanitizer.sanitize_and_load("{'vuln_type': 'auth_bypass', 'severity': 'medium'}") == {'vuln_type': 'auth_bypass', 'severity': 'medium'}
   - Test 2: finding = {'url': 'http://localhost:3000/rest/country-mapping'}
             v, r = VulnerabilityVerifier.deterministic_check(finding, "500 Internal Server Error", 500)
             assert v == "FALSE_POSITIVE"
   - Test 3: finding = {'url': 'http://localhost:3000/api/test'}
             v, r = VulnerabilityVerifier.deterministic_check(finding, "<title>OWASP Juice Shop</title>", 200)
             assert v == "FALSE_POSITIVE"
   - Test 4: cot_res = VulnerabilityVerifier.cot_verify({'url': 'http://test/fail'}, "500 Error", 500, ollama_url="http://127.0.0.1:9999")
             assert cot_res.get("verdict") == "FALSE_POSITIVE"
   - Prints "[SELF-TEST] ALL TESTS PASSED" only if all assertions pass.

Use only Python stdlib (json, ast, re, urllib.request, urllib.error, typing). Output ONLY complete Python code inside ```python fences.
"""

PROMPT_TELEMETRY_PROBER = """\
Sabrina, please build `telemetry_prober.py`.

Requirements:
1. Define `class EndpointNormalizer:` with method:
   @staticmethod
   def normalize_target(base_url: str, endpoint_path: str) -> str:
       from urllib.parse import urljoin
       return urljoin(base_url, endpoint_path)

2. Define dataclass `TelemetryProbeResult` with fields:
   - `url: str`
   - `method: str`
   - `status_code: int`
   - `headers: dict`
   - `body_snippet: str` (first 2000 chars of decoded response)
   - `body_size: int`
   - `elapsed_ms: float`
   - `error: str = ""`

3. Define `class TelemetryProber:` with method:
   @staticmethod
   def send_probe(url: str, method: str = "GET", headers: dict = None, body: str = None, timeout: float = 10.0) -> TelemetryProbeResult:
       # Ensure method is string (e.g. 'GET', 'POST', 'PUT', 'DELETE')
       # headers = headers if headers is not None else {}
       # If method != 'GET' and body is not None:
       #     data = body.encode('utf-8') if isinstance(body, str) else body
       # else:
       #     data = None
       # Construct req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
       # Open with urllib.request.urlopen(req, timeout=timeout) as response:
       #   extract status_code, dict(response.headers), body snippet, elapsed_ms
       # Catches urllib.error.HTTPError as e: extracts e.code, dict(e.headers), e.read().decode('utf-8', errors='replace')[:2000], body_size=0, elapsed_ms, error=str(e)
       # Catches http.client.IncompleteRead as e: extracts status_code=200, headers={}, body_snippet=e.partial.decode('utf-8', errors='replace')[:2000], body_size=len(e.partial), elapsed_ms, error=str(e)
       # Catches urllib.error.URLError as e: status_code=0, headers={}, body_snippet="", body_size=0, elapsed_ms, error=str(e)
       # Catches Exception as e: status_code=0, headers={}, body_snippet="", body_size=0, elapsed_ms, error=str(e)
       # Measures elapsed_ms with time.time() and returns TelemetryProbeResult

4. `if __name__ == '__main__':` self-test block:
   - Test 1: assert EndpointNormalizer.normalize_target("http://localhost:3000/rest/country-mapping", "/rest/country-mapping") == "http://localhost:3000/rest/country-mapping"
   - Test 2: assert EndpointNormalizer.normalize_target("http://localhost:3000", "/rest/admin") == "http://localhost:3000/rest/admin"
   - Test 3: assert EndpointNormalizer.normalize_target("http://localhost:3000/", "api/BasketItems") == "http://localhost:3000/api/BasketItems"
   - Test 4: sample = TelemetryProbeResult("http://test", "GET", 200, {}, "ok", 2, 10.5)
             assert sample.status_code == 200 and sample.body_snippet == "ok"
   - Prints "[SELF-TEST] ALL TESTS PASSED" only if all assertions pass.

Use only Python stdlib (dataclasses, urllib.request, urllib.error, urllib.parse, http.client, time, typing). Output ONLY complete Python code inside ```python fences.
"""

PROMPT_MODULAR_ORCHESTRATOR = """\
Sabrina, please build `modular_orchestrator.py`.

This module unifies our modular architecture into a master autonomous pentesting engine running our resident model swarm.

Requirements:
1. Imports: You MUST include ALL these imports at the very top of the file:
   ```python
   import sys
   import os
   import time
   import json
   import argparse
   from typing import Dict, List, Optional, Any
   from verification_module import JSONSanitizer, VulnerabilityVerifier
   from telemetry_prober import EndpointNormalizer, TelemetryProber, TelemetryProbeResult
   ```

2. Define `class ModularOrchestrator:`
   - `__init__(self, target_base_url: str, ollama_url: str = "http://127.0.0.1:11434"):`
     - Sets target_base_url, ollama_url.
     - Initializes `self.baselines: Dict[str, dict] = {}`
     - Initializes `self.confirmed_findings: List[dict] = []`
     - Initializes `self.filtered_false_positives: List[dict] = []`

   - `collect_baseline(self, path: str) -> dict`:
     - Calls `url = EndpointNormalizer.normalize_target(self.target_base_url, path)`
     - Probes GET with `res = TelemetryProber.send_probe(url, method="GET")`
     - Stores baseline dict: `{"status": res.status_code, "resp_size": res.body_size, "snippet": res.body_snippet, "is_error": res.status_code >= 400}`
     - `self.baselines[path] = baseline`
     - Returns baseline

   - `run_probe_task(self, endpoint_path: str, vuln_type: str, test_payload: str = None, method: str = "GET", headers: dict = None) -> dict`:
     - If method.upper() == "GET" and test_payload and (test_payload.startswith("?") or test_payload.startswith("/")):
         target_path = endpoint_path.rstrip("/") + test_payload if test_payload.startswith("?") else endpoint_path.rstrip("/") + "/" + test_payload.lstrip("/")
         body_data = None
       else:
         target_path = endpoint_path
         body_data = test_payload
     - Calls `url = EndpointNormalizer.normalize_target(self.target_base_url, target_path)`
     - Sends probe with `res = TelemetryProber.send_probe(url, method=method, headers=headers, body=body_data)`
     - Candidate finding: `{"url": url, "path": target_path, "vuln_type": vuln_type, "status": res.status_code, "payload": test_payload or "", "timestamp": time.time()}`
     - Baseline: `bl = self.baselines.get(endpoint_path, {})`
     - Stage 1: `verdict, reasons = VulnerabilityVerifier.deterministic_check(candidate, res.body_snippet, res.status_code, bl)`
     - If `verdict == "FALSE_POSITIVE"`:
         candidate["verdict"] = "FALSE_POSITIVE"
         candidate["reasons"] = reasons
         self.filtered_false_positives.append(candidate)
         return {}
     - Stage 2: `cot_res = VulnerabilityVerifier.cot_verify(candidate, res.body_snippet, res.status_code, bl, ollama_url=self.ollama_url)`
     - If str(cot_res.get("verdict", "")).upper() == "CONFIRMED":
         candidate["verified"] = True
         candidate["confidence"] = cot_res.get("confidence", 0.9)
         candidate["reason"] = cot_res.get("reason", "")
         self.confirmed_findings.append(candidate)
         return candidate
     - Else:
         candidate["verdict"] = "FALSE_POSITIVE"
         candidate["reasons"] = [cot_res.get("reason", "cot_rejected")]
         self.filtered_false_positives.append(candidate)
         return {}

   - `run_autonomous_assessment(self, endpoints: List[str] = None) -> dict`:
     - If endpoints is None or empty, default to high-value test endpoints:
       ["/rest/user/login", "/rest/products/search", "/rest/country-mapping", "/api/Feedbacks", "/ftp"]
     - For each path in endpoints:
       print(f"[*] Baselining {path}...")
       self.collect_baseline(path)
     - Execute targeted test probes:
       1. For "/rest/user/login": test SQLi Auth Bypass (POST JSON `{"email": "' or 1=1--", "password": "x"}` with header `{"Content-Type": "application/json"}`)
       2. For "/rest/products/search": test SQLi probe (GET with `?q=apple'))--`)
       3. For "/rest/country-mapping": test candidate (GET) to verify 500 error is caught as FALSE_POSITIVE
       4. For "/ftp": test directory/sensitive file probe (GET with `/package.json.bak%2500.md`)
     - Return `self.generate_report()`

   - `generate_report(self) -> dict`:
     - Returns:
       {
         "target": self.target_base_url,
         "total_baselined": len(self.baselines),
         "confirmed_findings": self.confirmed_findings,
         "filtered_false_positives": self.filtered_false_positives
       }

3. CLI execution & `if __name__ == '__main__':` self-test block:
   - If sys.argv has arguments (e.g. `--target`):
     - Parse args: `--target` (default "http://localhost:3000"), `--ollama-url` (default "http://127.0.0.1:11434")
     - Instantiate `ModularOrchestrator(args.target, args.ollama_url)`
     - Call `report = orch.run_autonomous_assessment()`
     - Print report formatted as JSON.
   - Else (self-test mode without arguments):
     - Instantiate `orch = ModularOrchestrator("http://localhost:3000")`
     - Test deterministic 500 error filtering:
       `candidate_500 = {"url": "http://localhost:3000/rest/country-mapping", "vuln_type": "info_leak"}`
       `v500, r500 = VulnerabilityVerifier.deterministic_check(candidate_500, "500 Internal Server Error", 500)`
       `assert v500 == "FALSE_POSITIVE"`
     - Test SPA fallback filtering:
       `candidate_spa = {"url": "http://localhost:3000/api/nonexistent", "vuln_type": "bypass"}`
       `v_spa, _ = VulnerabilityVerifier.deterministic_check(candidate_spa, "<title>OWASP Juice Shop</title>", 200)`
       `assert v_spa == "FALSE_POSITIVE"`
     - Test report generation:
       `rep = orch.generate_report()`
       `assert rep["target"] == "http://localhost:3000"`
     - Prints "[SELF-TEST] ALL TESTS PASSED" only if all assertions pass.

Use only Python stdlib. Output ONLY complete Python code inside ```python fences.
"""

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    
    # Module 1: verification_module.py
    target_verifier = os.path.join(here, "verification_module.py")
    if os.path.exists(target_verifier) and subprocess.run([sys.executable, target_verifier], capture_output=True, text=True).returncode == 0:
        print("[+] Module 1 (verification_module.py) already verified.")
        success_verifier = True
    else:
        success_verifier = collaborative_build("verification_module.py", PROMPT_VERIFICATION_MODULE, target_verifier)
    
    if success_verifier:
        # Module 2: telemetry_prober.py
        target_prober = os.path.join(here, "telemetry_prober.py")
        if os.path.exists(target_prober) and subprocess.run([sys.executable, target_prober], capture_output=True, text=True).returncode == 0:
            print("[+] Module 2 (telemetry_prober.py) already verified.")
            success_prober = True
        else:
            success_prober = collaborative_build("telemetry_prober.py", PROMPT_TELEMETRY_PROBER, target_prober)
        
        if success_prober:
            # Module 3: modular_orchestrator.py
            target_orch = os.path.join(here, "modular_orchestrator.py")
            success_orch = collaborative_build("modular_orchestrator.py", PROMPT_MODULAR_ORCHESTRATOR, target_orch)
            if success_orch:
                print("\n" + "="*75)
                print("[★] COMPLETE: Sabrina has built and verified all modular components!")
                print("="*75)

