"""Run a workshop candidate via Sabrina's existing offline Docker sandbox."""
import hashlib
import json
import re
import shlex
import subprocess

LAB_SOURCE = "/home/ego/bot/Sabrina-windows-sync/tools/tool_sandbox.py"
LAB_SANDBOX_SHA256 = "1c972e7f6c01bd7319b4738c659c6cdfd3712a74b4918a73d0ae59e1ba224f38"
LAB_EVIDENCE = "/mnt/webapps-nvme/sabrina-research/workshop-runs"
IMAGE = "sha256:245f9d32fabaec22e749aa5cfef79996088135e9d4410f4e7ebe133c2be98132"

# Trusted bridge runs on ego. The generated candidate only runs in Docker.
REMOTE = r'''
import fcntl
import hashlib
import json
from pathlib import Path

stage = Path(request["evidence_root"]) / request["run_id"] / request["stage"]
stage.mkdir(parents=True, exist_ok=True)
with (stage / "owner.lock").open("ab") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    receipt = stage / "result.json"
    request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    if receipt.exists():
        prior = json.loads(receipt.read_text())
        if prior["request_sha256"] != request_hash:
            raise ValueError("Different sandbox request already recorded")
        print(json.dumps(prior))
    else:
        intent = stage / "intent.txt"
        installed = Path(request["sandbox_source"])
        source_path = stage.parent / "sandbox-source.py"
        source = (source_path if source_path.exists() else installed).read_bytes()
        if hashlib.sha256(source).hexdigest() != request["sandbox_sha256"]:
            raise ValueError("Installed lab sandbox source changed from the frozen fingerprint")
        if not source_path.exists():
            saved_source = stage / "sandbox-source.tmp"
            saved_source.write_bytes(source)
            saved_source.replace(source_path)
        namespace = {"__file__": str(source_path), "__name__": "workshop_sandbox"}
        exec(compile(source, str(source_path), "exec"), namespace)
        with intent.open("x") as stream:
            stream.write(request_hash)
        result = namespace["execute_candidate"](
            request["script"], timeout=15, image=request["image"], receipt_root=stage)
        result["request_sha256"] = request_hash
        result["sandbox_sha256"] = request["sandbox_sha256"]
        scratch = receipt.with_suffix(".tmp")
        scratch.write_text(json.dumps(result))
        scratch.replace(receipt)
        print(json.dumps(result))
'''

DRIVER = """
import contextlib
import copy
import json

scope = {"__name__": "workshop_candidate"}
data = json.loads(INPUTS)
results = []
with open("/dev/null", "w") as sink:
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        exec(compile(CANDIDATE, "tool.py", "exec"), scope)
    for args in data:
        original = copy.deepcopy(args)
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                value = scope["solve"](*args)
            results.append({"value": value, "mutated": args != original})
        except Exception as exc:
            results.append({"error": type(exc).__name__ + ": " + str(exc)[:240]})
print(json.dumps(results, allow_nan=False))
"""

PROBE = """
import json
import os
from pathlib import Path
import platform

try:
    with open("/input/run_script.py", "r+b"):
        pass
except OSError:
    script_write_denied = True
else:
    script_write_denied = False
scratch = Path("/tmp/workshop-isolation-probe")
scratch.write_text("ok")
print(json.dumps({"uid": os.getuid(), "links": sorted(os.listdir("/sys/class/net")),
                  "script_write_denied": script_write_denied,
                  "socket_visible": Path("/var/run/docker.sock").exists(),
                  "scratch_works": scratch.read_text() == "ok",
                  "python": platform.python_version()}))
"""


class SandboxBlocked(RuntimeError):
    """A transport or sandbox gate failed before a valid execution receipt."""


class WorkshopSandbox:
    def __init__(self, *, run_id, sandbox_sha256, host="ego@ego.local",
                 hostname="192.168.2.69", image=IMAGE):
        self.run_id = run_id
        self.sandbox_sha256 = sandbox_sha256
        self.host = host
        self.hostname = hostname
        self.image = image

    def execute(self, stage, script):
        if not re.fullmatch(r"[a-z0-9-]+", stage):
            raise ValueError("Invalid sandbox stage")
        call = {
            "run_id": self.run_id, "stage": stage, "image": self.image,
            "evidence_root": LAB_EVIDENCE, "sandbox_source": LAB_SOURCE,
            "sandbox_sha256": self.sandbox_sha256, "script": script,
        }
        driver = "import sys,json; request=json.load(sys.stdin); exec(request.pop('driver'))"
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ForwardAgent=no"]
        if self.hostname:
            cmd += ["-o", f"HostName={self.hostname}"]
        cmd += [self.host, "python3 -c " + shlex.quote(driver)]
        expected_hash = hashlib.sha256(json.dumps(call, sort_keys=True).encode()).hexdigest()
        try:
            result = subprocess.run(
                cmd, input=json.dumps({**call, "driver": REMOTE}), capture_output=True,
                text=True, encoding="utf-8", timeout=48)
            if result.returncode:
                raise SandboxBlocked("Lab sandbox could not return a receipt: " + result.stderr[-450:])
            receipt = json.loads(result.stdout)
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SandboxBlocked(f"Lab sandbox receipt unavailable: {exc}") from exc
        if (receipt.get("request_sha256") != expected_hash
                or receipt.get("sandbox_sha256") != self.sandbox_sha256
                or receipt.get("code_sha256") != hashlib.sha256(script.encode()).hexdigest()):
            raise SandboxBlocked("Lab sandbox receipt fingerprint mismatch")
        return receipt

    def probe(self):
        receipt = self.execute("isolation-probe", PROBE)
        actual = self.output(receipt)
        expected = {"uid": 65534, "links": ["lo"], "script_write_denied": True,
                    "socket_visible": False, "scratch_works": True}
        if not isinstance(actual, dict) or any(actual.get(k) != v for k, v in expected.items()):
            raise SandboxBlocked("Live Docker boundary checks did not pass")
        return {"receipt": receipt, "observed": actual}

    def output(self, receipt):
        if (not receipt.get("isolated") or receipt.get("host_fallback") is not False
                or receipt.get("image_digest") != self.image or receipt.get("network") != "none"
                or receipt.get("exit_code") in {125, 126, 127}
                or receipt.get("status") in {"unavailable", "sandbox_error"}):
            raise SandboxBlocked("Offline Docker execution unavailable: " + receipt.get("stderr", "")[-450:])
        if receipt["status"] != "script_completed":
            raise ValueError(f"{receipt['status']} (exit {receipt['exit_code']}): {receipt['stderr'][-350:]}")
        return json.loads(receipt["stdout"])

    def evaluate(self, stage, code, cases):
        inputs = json.dumps([case["args"] for case in cases], allow_nan=False)
        script = f"CANDIDATE = {code!r}\nINPUTS = {inputs!r}\n" + DRIVER
        receipt = self.execute(stage, script)
        error = ""
        try:
            results = self.output(receipt)
            if not isinstance(results, list) or len(results) != len(cases):
                raise ValueError("Invalid result count from candidate")
        except ValueError as exc:
            error = str(exc)
            results = [{"error": error}] * len(cases)
        # The candidate only receives inputs. This controller owns the expected results.
        findings = []
        for case, actual in zip(cases, results, strict=True):
            wanted = case["expected"]
            passed = (isinstance(actual, dict) and type(actual.get("value")) is type(wanted)
                      and actual["value"] == wanted and actual.get("mutated") is False
                      and "error" not in actual)
            findings.append({"id": case["id"], "passed": passed, "args": case["args"],
                             "expected": wanted, "actual": actual})
        return {"code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                "passed": sum(row["passed"] for row in findings), "total": len(findings),
                "findings": findings, "sandbox_receipt": receipt, "error": error}

