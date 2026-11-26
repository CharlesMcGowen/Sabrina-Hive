"""Run deterministic recruited generators with no network, mounts or credentials."""
import ast
import json
import subprocess
import uuid
import tempfile

from .common import encoded
from .goals import ACTIONS, FEATURES
from .policy import SHAPES

SPEC = dict(features=FEATURES, actions=ACTIONS, shapes=SHAPES, history="six decayed failed-action counts",
            architecture="logits=relu(concat(features,history) @ up.T + up_bias) @ down.T + down_bias")


def validate_source(source):
    if not isinstance(source, str) or len(source) > 20000:
        raise ValueError("Generator source exceeds 20,000 characters")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Import) and all(i.name == "math" for i in node.names):
            continue
        if not isinstance(node, ast.FunctionDef) or node.decorator_list:
            raise ValueError("Only ordinary function definitions and import math are allowed")
    if not any(isinstance(x, ast.FunctionDef) and x.name == "build" for x in tree.body):
        raise ValueError("Expected build(spec)")
    forbidden = {"open", "exec", "eval", "compile", "input", "__import__", "globals", "locals", "getattr", "setattr"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.ClassDef, ast.Global, ast.Nonlocal, ast.With, ast.AsyncFunctionDef)):
            raise ValueError("Unsupported generator construct")
        if isinstance(node, ast.Name) and (node.id in forbidden or node.id.startswith("__")):
            raise ValueError("Unsupported generator name")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("Unsupported generator attribute")


def build(source):
    validate_source(source)
    runner = """import json,sys
p=json.load(sys.stdin)
namespace={}
exec(compile(p['source'], '<recruit-generator>', 'exec'),namespace)
print(json.dumps(namespace['build'](p['spec']),allow_nan=False))
"""
    values = []
    for _ in range(2):
        name = "math-print-" + uuid.uuid4().hex
        cmd = ["docker", "run", "--rm", "-i", "--name", name, "--pull", "never",
               "--network", "none", "--read-only", "--user", "65534:65534", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--pids-limit", "16", "--memory", "128m",
               "--memory-swap", "128m", "--cpus", "1", "--log-driver", "none",
               "python:3.11-slim-bookworm", "timeout", "6", "python", "-I", "-B", "-c", runner]
        try:
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                p = subprocess.run(["prlimit", "--fsize=65536:65536", "--", *cmd],
                                   input=encoded(dict(source=source, spec=SPEC)),
                                   stdout=stdout, stderr=stderr, timeout=15)
                stdout.seek(0)
                stderr.seek(0)
                output, diagnostic = stdout.read(65537), stderr.read(1500)
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
            raise ValueError("Generator timed out")
        if p.returncode or len(output) > 65536:
            raise ValueError("Generator failed: " + diagnostic.decode(errors="replace")[-900:])
        values.append(json.loads(output))
    if encoded(values[0]) != encoded(values[1]):
        raise ValueError("Generator is not reproducible")
    return values[0]
