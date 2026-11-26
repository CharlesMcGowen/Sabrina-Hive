"""Validate pure numeric paper tools and qualify them against a blind reference."""
import ast
import hashlib
import json
import math
import random
from pathlib import Path

from tools.tool_sandbox import execute_candidate

DOMAINS = {"binary": (0, 1), "unit": (0, 1), "signed": (-10, 10), "positive": (1, 10)}
BUILTINS = set("abs min max sum len range enumerate zip sorted round float int sqrt log exp".split())
IMAGE = "python:3.11-slim"
DRIVER = """
import json, sys
from math import sqrt, log, exp
inputs = json.load(sys.stdin)
print(json.dumps([compute(rows) for rows in inputs], allow_nan=False))
"""

class ProposalError(ValueError):
    """An authored proposal failed a qualification gate."""

class ReferenceExecutionError(ProposalError):
    """The blind reviewer's implementation failed before returning its comparison."""

def fingerprint(data):
    return hashlib.sha256(data).hexdigest()

def validated_source(source):
    if not isinstance(source, str) or not 0 < len(source) <= 12000:
        raise ProposalError("Expected Python source under 12,000 characters")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ProposalError(str(exc)) from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ProposalError("Only one compute(rows) function is supported")
    fn = tree.body[0]
    if (fn.name != "compute" or ast.unparse(fn.args) != "rows" or fn.decorator_list or fn.returns
            or getattr(fn, "type_params", None)):
        raise ProposalError("Expected undecorated compute(rows)")
    forbidden = (ast.Import, ast.ImportFrom, ast.Attribute, ast.Lambda, ast.ClassDef, ast.Global,
                 ast.Nonlocal, ast.With, ast.AsyncWith, ast.Yield, ast.YieldFrom, ast.AsyncFor,
                 ast.Raise, ast.Try, ast.While)
    for node in ast.walk(fn):
        if isinstance(node, forbidden) or (isinstance(node, ast.FunctionDef) and node is not fn):
            raise ProposalError("Unsupported expression: " + type(node).__name__)
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ProposalError("Private names are outside the numeric interface")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in BUILTINS):
            raise ProposalError("Only numeric builtins and sqrt/log/exp may be called")
    return source

def valid_number(x):
    return type(x) in (float, int) and abs(x) < 1e100 and math.isfinite(x)

def validate_rows(rows, width, domain):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 512:
        raise ProposalError("Expected between 1 and 512 numeric rows")
    low, high = DOMAINS[domain]
    for row in rows:
        if not isinstance(row, list) or len(row) != width:
            raise ProposalError("Row width differs from the tool contract")
        for value in row:
            if not valid_number(value) or not low <= value <= high or (domain == "binary" and value not in (0, 1)):
                raise ProposalError("Input outside the registered numeric domain")

def run_source(source, samples, receipts):
    run = execute_candidate(validated_source(source) + "\n" + DRIVER, timeout=12, image=IMAGE,
                            receipt_root=receipts, stdin_text=json.dumps(samples, allow_nan=False))
    if not run["isolated"] or run["status"] in {"unavailable", "sandbox_error"}:
        raise RuntimeError("Isolated verifier unavailable: " + run["stderr"][:240])
    if run["status"] != "script_completed" or run["exit_code"] != 0:
        raise ProposalError("Isolated execution failed: " + run["status"] + " " + run["stderr"][-350:])
    try:
        values = json.loads(run["stdout"])
    except ValueError as exc:
        raise ProposalError("Expected one numeric result per input") from exc
    if not isinstance(values, list) or len(values) != len(samples) or not all(map(valid_number, values)):
        raise ProposalError("Expected one finite scalar per input")
    return values, {"receipt":run["run_id"], "image":run["image_digest"], "source":fingerprint(source.encode())}

def self_examples(author):
    width, domain = author["width"], author["domain"]
    own = author["self_tests"]
    if not isinstance(own, list) or not 2 <= len(own) <= 8:
        raise ProposalError("At least two self-tests are required")
    for case in own:
        if not isinstance(case, dict) or not valid_number(case.get("expected")):
            raise ProposalError("Invalid expected result")
        validate_rows(case.get("rows"), width, domain)
    examples = [x["rows"] for x in own]
    if len({json.dumps(x) for x in examples}) < 2:
        raise ProposalError("Identical self-tests")
    return examples

def compare(author, reviewer, receipts, seed, *, reference=None):
    width, domain = author["width"], author["domain"]
    own = author["self_tests"]
    examples = self_examples(author)
    rng = random.Random(seed)
    low, high = DOMAINS[domain]
    fixtures = [[[low]*width], [[high]*width]*4,
                [[(low, high)[(i+j)%2] for j in range(width)] for i in range(7)]]
    for n in (1, 2, 3, 5, 8, 17, 31):
        for _ in range(3):
            draw = (lambda: rng.randint(low, high)) if domain == "binary" else (lambda: round(rng.uniform(low, high), 4))
            fixtures.append([[draw() for _ in range(width)] for _ in range(n)])
    samples = examples + fixtures
    actual, run = run_source(author["source"], samples, receipts)
    def close(a, b):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-8)
    if any(not close(a, b["expected"]) for a, b in zip(actual, own)):
        raise ProposalError("A worker-authored self-test failed")
    if reference and any(not close(a, reference(rows)) for a, rows in zip(actual,samples)):
        raise ProposalError("The reference statistic disagreed")
    if all(close(actual[-1], v) for v in actual[len(own):]):
        raise ProposalError("The held-out set could not distinguish this tool from a constant")
    try:
        expected, ref = run_source(reviewer["source"], samples, receipts)
    except ProposalError as exc:
        raise ReferenceExecutionError(str(exc)) from exc
    if any(not close(a, b) for a, b in zip(actual, expected)):
        raise ProposalError("The independent implementation disagreed")
    return {"self_tests_passed":len(own), "held_out_passed":len(fixtures), "seed":seed,
            "candidate":run, "reference":ref, "formula_checked":reference is not None,
            "inputs_sha256":fingerprint(json.dumps(samples).encode())}
