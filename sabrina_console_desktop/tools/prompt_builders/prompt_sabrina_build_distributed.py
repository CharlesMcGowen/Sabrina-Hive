#!/usr/bin/env python3
"""
prompt_sabrina_build_distributed.py
-----------------------------------
Orchestrates direct collaborative building with Sabrina (running on the lab host via Ollama).
Prompts Sabrina to architect, write, review, and self-test:
  1. distributed_node_orchestrator.py
     (Detects GPU/CPU/RAM, configures model execution strategies for CPU vs GPU.)
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
        "options": {"temperature": 0.15, "num_predict": 4000}
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

def collaborative_build(module_name: str, initial_prompt: str, target_local_path: str, max_rounds: int = 4):
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
        
        with open(target_local_path, "w", encoding="utf-8") as f:
            f.write(code)
            
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
            feedback = (
                f"Antigravity Reviewer Feedback:\n"
                f"Syntax compiled cleanly, but the self-test did not pass:\n"
                f"Return Code: {run_res.returncode}\n"
                f"STDOUT: {stdout}\n"
                f"STDERR: {stderr}\n"
                f"Please address the test failure and output the complete, corrected Python 3 file inside ```python fences."
            )
            conversation.append({"role": "assistant", "content": code})
            conversation.append({"role": "user", "content": feedback})

    print(f"[-] Reached max rounds ({max_rounds}) without full pass.")
    return False

PROMPT_DISTRIBUTED_NODE = """\
Sabrina, please build `distributed_node_orchestrator.py`.

Requirements:
1. Imports: 
   ```python
   import os
   import sys
   import platform
   import json
   import subprocess
   from dataclasses import dataclass, asdict
   from typing import Dict, Any, Optional
   ```

2. Define `@dataclass` `NodeCapabilities`:
   - `architecture: str`
   - `os_type: str`
   - `total_ram_gb: float`
   - `cpu_cores: int`
   - `has_gpu: bool`
   - `vram_gb: float`

3. Define `class DistributedNodeOrchestrator:`
   - `@staticmethod`
     `def detect_capabilities() -> NodeCapabilities:`
       # Uses platform.machine(), platform.system(), os.cpu_count()
       # For total_ram_gb, use safe subprocess calls (e.g. 'free -m' on linux, 'wmic memorychip get capacity' on windows). If fails, assume 16.0.
       # For GPU/VRAM, use 'nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits'. If fails, assume has_gpu=False, vram_gb=0.0.
       # Returns NodeCapabilities instance.
   
   - `@staticmethod`
     `def determine_provisioning_strategy(caps: NodeCapabilities, model_size_gb: float) -> Dict[str, Any]:`
       # Given the capabilities and the target model size (e.g., 35.0 for a 35B model, 4.5 for an 8B model).
       # Rules:
       # 1. If caps.has_gpu and caps.vram_gb > (model_size_gb + 2.0):
       #    return {"strategy": "GPU_RESIDENT", "ollama_env": {"OLLAMA_NUM_PARALLEL": "4", "OLLAMA_MAX_VRAM": str(int(caps.vram_gb * 1024))}}
       # 2. If not enough VRAM but caps.total_ram_gb > (model_size_gb + 16.0):
       #    return {"strategy": "CPU_RAM_OFFLOAD", "ollama_env": {"OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_VRAM": "0", "CUDA_VISIBLE_DEVICES": ""}, "threads": caps.cpu_cores // 2}
       # 3. Otherwise:
       #    return {"strategy": "INSUFFICIENT_RESOURCES", "reason": "Model size exceeds available GPU VRAM and System RAM thresholds."}

   - `@staticmethod`
     `def provision_ollama_instance(model_name: str, model_size_gb: float) -> Dict[str, Any]:`
       # Combines detect_capabilities and determine_provisioning_strategy.
       # Returns a dict with capabilities and the proposed strategy.

4. `if __name__ == '__main__':` self-test block:
   - Call `caps = DistributedNodeOrchestrator.detect_capabilities()`
   - Assert `caps.cpu_cores is not None and caps.cpu_cores > 0`
   
   - Create a mock `NodeCapabilities` for a Lab host (architecture='x86_64', os_type='Linux', has_gpu=True, vram_gb=16.0, total_ram_gb=128.0, cpu_cores=24).
   - Test GPU Strategy (8B model -> 4.5 GB):
     `strat = DistributedNodeOrchestrator.determine_provisioning_strategy(mock_lab, 4.5)`
     `assert strat["strategy"] == "GPU_RESIDENT"`
     
   - Test CPU Strategy (35B model -> 18.0 GB):
     `strat2 = DistributedNodeOrchestrator.determine_provisioning_strategy(mock_lab, 18.0)`
     `assert strat2["strategy"] == "CPU_RAM_OFFLOAD"`
     `assert strat2["ollama_env"]["OLLAMA_MAX_VRAM"] == "0"`
     
   - Prints "[SELF-TEST] ALL TESTS PASSED" only if all assertions pass.

Use only Python stdlib. Output ONLY complete Python code inside ```python fences.
"""

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    target_path = os.path.join(here, "distributed_node_orchestrator.py")
    
    success = collaborative_build("distributed_node_orchestrator.py", PROMPT_DISTRIBUTED_NODE, target_path)
    if success:
        print("\n" + "="*75)
        print("[★] COMPLETE: Sabrina has built and verified the distributed node orchestrator!")
        print("="*75)
