"""Trusted artifact verifier. Generated code runs ONLY in a restricted container.

Run on the lab with --stdio. This interface is not a Sabrina model tool. The
controller supplies artifacts, never a command, test program, or claimed verdict.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import threading
import uuid


SUITES = {
    "duration-v1": {
        "requirement": (
            "Return solution.py defining parse_duration(text). Parse one or more "
            "non-negative integer amounts followed by h, m, or s, in any order; "
            "whitespace between amounts and units is allowed. Return total integer "
            "seconds. Reject empty input, unknown units, negatives, decimals, "
            "repeated units, non-string values, and unconsumed characters with ValueError. "
            "Use only the Python standard library. No I/O, commands, dependencies or file edits."
        ),
        "file": "solution.py", "function": "parse_duration",
        "cases": [
            ["1h 30m", 5400], ["15s", 15], ["0s", 0], ["2m1h", 3720],
            [" 3 h 2 m 1 s ", 10921], ["100h", 360000], ["", "ValueError"],
            ["1m 2m", "ValueError"], ["-1s", "ValueError"], ["1.5h", "ValueError"],
            ["1d", "ValueError"], ["2s garbage", "ValueError"], [None, "ValueError"],
        ],
    },
    "bytes-v1": {
        "requirement": (
            "Return solution.py defining parse_bytes(text). Parse non-negative integer "
            "or decimal byte amounts with units: B, KB, MB, GB (case-insensitive, optional "
            "whitespace between number and unit). 1 KB = 1024 B, 1 MB = 1048576 B, 1 GB = 1073741824 B. "
            "Return total integer bytes (truncated with int()). Reject empty input, non-strings, "
            "unknown units, negatives, missing amounts, repeated units, or unconsumed characters "
            "with ValueError. Use only the Python standard library. No I/O, commands, dependencies or file edits."
        ),
        "file": "solution.py", "function": "parse_bytes",
        "cases": [
            ["512B", 512], ["1KB", 1024], ["1.5MB", 1572864], [" 2 GB ", 2147483648],
            ["0 B", 0], ["256 b", 256], ["", "ValueError"], ["-10MB", "ValueError"],
            ["1TB", "ValueError"], ["1KB 2KB", "ValueError"], ["1.2.3MB", "ValueError"],
            ["500B extra", "ValueError"], [None, "ValueError"],
        ],
    },
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def suite_fingerprint(suite_id):
    return digest(SUITES[suite_id])


def public_contract(suite_id):
    suite = SUITES[suite_id]
    return {"suite_id": suite_id, "suite_sha256": suite_fingerprint(suite_id),
            "requirement": suite["requirement"], "files": [suite["file"]],
            "criteria": ["manifest", "syntax", "functional"]}


def verify(payload, *, image="python:3.11-slim"):
    """Verdicts are computed here, outside the generated program's process."""
    started = time.time()
    suite_id = payload["suite_id"]
    suite = SUITES[suite_id]
    files = payload["files"]
    base = {"suite_sha256": suite_fingerprint(suite_id), "artifact_sha256": digest(files),
            "checks": [], "execution_mode": "live", "isolation": "docker"}
    if payload.get("suite_sha256") != base["suite_sha256"]:
        return {**base, "status": "blocked", "reason": "suite_fingerprint_changed"}
    if (not isinstance(files, dict) or set(files) != {suite["file"]}
            or any(not isinstance(v, str) or len(v.encode()) > 65536 for v in files.values())):
        return {**base, "status": "failed", "checks": [{"id": "manifest", "passed": False}]}
    base["checks"].append({"id": "manifest", "passed": True})
    try:
        ast.parse(files[suite["file"]])
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {**base, "status": "failed", "checks": base["checks"] + [
            {"id": "syntax", "passed": False, "detail": str(exc)[:400]}]}
    base["checks"].append({"id": "syntax", "passed": True})
    # Pin the locally installed image ID. Never download software during a trial.
    try:
        inspected = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                   capture_output=True, text=True, timeout=10, check=True)
        image_id = inspected.stdout.strip()
        if not image_id.startswith("sha256:"):
            raise ValueError("invalid image identity")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {**base, "status": "blocked", "reason": "isolation_unavailable"}
    base["image_id"] = image_id
    # Expected values stay outside the candidate container. Output is data, not a verdict.
    fn_name = suite.get("function", "parse_duration")
    driver = f"""import json,sys
sys.path.insert(0, '/candidate')
import solution
func = getattr(solution, {fn_name!r})
inputs=json.loads(sys.stdin.read())
results=[]
for item in inputs:
    try:
        value=func(item)
        results.append({{'value':value,'type':type(value).__name__}})
    except Exception as exc:
        results.append({{'error':type(exc).__name__}})
print(json.dumps(results))
"""
    name = "sabrina-verify-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="sabrina-verify-") as directory:
        root = Path(directory)
        candidate = root / "candidate"
        grader = root / "grader"
        candidate.mkdir(mode=0o755)
        grader.mkdir(mode=0o755)
        root.chmod(0o755)
        (candidate / "solution.py").write_text(files["solution.py"], encoding="utf-8")
        (grader / "driver.py").write_text(driver, encoding="utf-8")
        for path in (candidate / "solution.py", grader / "driver.py"):
            path.chmod(0o444)
        command = ["docker", "run", "--rm", "-i", "--name", name, "--network", "none",
                   "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                   "--pids-limit=32", "--memory=128m", "--memory-swap=128m", "--cpus=1",
                   "--user=65534:65534", "--ulimit", "fsize=1048576:1048576",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m",
                   "--mount", f"type=bind,src={candidate},dst=/candidate,readonly",
                   "--mount", f"type=bind,src={grader},dst=/grader,readonly",
                   image_id, "python", "-I", "-B", "/grader/driver.py"]
        try:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            captured = bytearray()
            def drain():
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    captured.extend(chunk[:max(0,65537-len(captured))])
                    if len(captured) > 65536:
                        proc.kill()
                        break
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            try:
                proc.stdin.write(json.dumps([case[0] for case in suite['cases']]).encode())
                proc.stdin.close()
                proc.wait(timeout=20)
            except (subprocess.TimeoutExpired, BrokenPipeError):
                proc.kill()
                proc.wait(timeout=5)
                raise
            finally:
                reader.join(timeout=5)
                proc.stdout.close()
            raw = bytes(captured)
            if proc.returncode == 125:
                return {**base, "status": "blocked", "reason": "container_start_failed"}
            values = json.loads(raw) if proc.returncode == 0 and len(raw) <= 65536 else None
            expected = [{"error": "ValueError"} if result == "ValueError" else
                        {"value": result, "type": type(result).__name__} for _, result in suite["cases"]]
            good = isinstance(values, list) and values == expected
            failed_cases = [i for i, wanted in enumerate(expected)
                            if not isinstance(values, list) or i >= len(values) or values[i] != wanted]
            base["checks"].append({"id": "functional", "passed": good,
                                   "failed_cases": failed_cases,
                                   "case_count": len(expected)})
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            base["checks"].append({"id": "functional", "passed": False,
                                   "detail": type(exc).__name__})
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
    return {**base, "status": "passed" if all(c["passed"] for c in base["checks"]) else "failed",
            "elapsed_seconds": round(time.time() - started, 3)}


class LabVerifier:
    """Transport to the trusted verifier, with no model-supplied executable/path."""
    def __init__(self, host="ego@ego.local", remote_root="/home/ego/bot/Sabrina-windows-sync"):
        self.host, self.remote_root = host, remote_root

    def check(self, payload):
        try:
            result = subprocess.run([
                "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.host,
                f"python3 {self.remote_root}/tools/sabrina_workflow_verifier.py --stdio"],
                input=json.dumps(payload), capture_output=True, text=True, timeout=50)
            if result.returncode:
                return {"status": "blocked", "reason": "verifier_unavailable"}
            verdict = json.loads(result.stdout)
            if (verdict.get("artifact_sha256") != digest(payload["files"])
                    or verdict.get("suite_sha256") != payload["suite_sha256"]):
                return {"status": "blocked", "reason": "verifier_fingerprint_mismatch"}
            return verdict
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"status": "blocked", "reason": "verifier_unavailable"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stdio", action="store_true", required=True)
    parser.parse_args()
    import sys
    raw = sys.stdin.buffer.read(131073)
    if len(raw) > 131072:
        raise ValueError("verification input too large")
    print(json.dumps(verify(json.loads(raw))))


if __name__ == "__main__":
    main()
