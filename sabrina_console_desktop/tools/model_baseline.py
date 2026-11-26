"""Fixed diagnostic baseline against one Ollama model; not an ExploitGym score.

No generated tool call or code is executed. Expected answers are graded outside
the prompt. Save the suite, model digest, settings, full responses and outcomes.
"""
from __future__ import annotations
import argparse
import collections
import datetime
import hashlib
import json
from pathlib import Path
import time
import urllib.request

MODEL = "huihui_ai/qwen3-abliterated:14b"
SYSTEM = "Follow the requested output contract exactly. Use only supplied evidence. Do not claim actions or results that were not observed."
OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 512}
SUITE = [
    {"id": "json_action", "category": "json", "prompt": 'Return only this JSON object: {"tool":"inspect","model_alias":"sample"}', "expected": {"tool": "inspect", "model_alias": "sample"}},
    {"id": "json_types", "category": "json", "prompt": 'Return JSON only: enabled must be the boolean false; count the integer 0; error null; labels an empty array. Use those four keys.', "expected": {"enabled": False, "count": 0, "error": None, "labels": []}},
    {"id": "json_nested", "category": "json", "prompt": 'Return JSON only with headers containing Authorization equal to "Bearer demo-token", and status equal to the integer 200.', "expected": {"headers": {"Authorization": "Bearer demo-token"}, "status": 200}},
    {"id": "json_escape", "category": "json", "prompt": 'Return JSON only with key text whose value is exactly: He said "hello". Key path must contain exactly C:\\temp\\model.', "expected": {"text": 'He said "hello".', "path": 'C:\\temp\\model'}},
    {"id": "evidence_401", "category": "evidence", "prompt": 'A request returned HTTP 401 and {"error":"invalid token"}. No protected data was returned. Did it demonstrate successful authentication? Return JSON only: {"confirmed": boolean}.', "expected": {"confirmed": False}},
    {"id": "evidence_timeout", "category": "evidence", "prompt": 'A benchmark timed out before any test ran. Classify the outcome as pass, fail, or infrastructure_error. Return JSON only with key outcome.', "expected": {"outcome": "infrastructure_error"}},
    {"id": "evidence_partial", "category": "evidence", "prompt": 'An inventory reports 200 entries and truncated=true. Can you conclude that the archive has exactly 200 models? Return JSON only with key complete and a boolean value.', "expected": {"complete": False}},
    {"id": "evidence_holdout", "category": "evidence", "prompt": 'Candidate A passes 10/10 training examples and 2/10 held-out examples. Baseline passes 6/10 held-out examples. Both used the same evaluation settings. Did A improve held-out accuracy? Return JSON only with key improved and a boolean value.', "expected": {"improved": False}},
    {"id": "code_bool", "category": "code_reasoning", "prompt": 'In Python, x=True. Give the results of isinstance(x,int) and type(x) is int in that order. Return only a JSON array of two booleans.', "expected": [True, False]},
    {"id": "code_slice", "category": "code_reasoning", "prompt": 'What is list(range(8))[1:7:2] in Python? Return only the resulting JSON array.', "expected": [1, 3, 5]},
    {"id": "code_alias", "category": "code_reasoning", "prompt": 'Python executes: a=[[0]]*3; a[0].append(1). What is a? Return only the resulting JSON array.', "expected": [[0, 1], [0, 1], [0, 1]]},
    {"id": "code_edge", "category": "code_reasoning", "prompt": 'Python executes: def f(xs): return all(x > 0 for x in xs). What is f([])? Return only the JSON boolean.', "expected": True},
]
TOOLS = [
    {"type": "function", "function": {"name": "inspect_checkpoint", "description": "Read checkpoint metadata by alias.", "parameters": {"type": "object", "properties": {"model_alias": {"type": "string"}}, "required": ["model_alias"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "record_result", "description": "Record supplied test evidence.", "parameters": {"type": "object", "properties": {"test_id": {"type": "string"}, "passed": {"type": "boolean"}}, "required": ["test_id", "passed"], "additionalProperties": False}}},
]
SUITE += [
    {"id": "tool_inspect", "category": "native_tools", "prompt": "Call inspect_checkpoint for model_alias archive-granite-8b-sft. Make exactly one tool call.", "expected": {"name": "inspect_checkpoint", "arguments": {"model_alias": "archive-granite-8b-sft"}}},
    {"id": "tool_failure", "category": "native_tools", "prompt": "Observed test schema-7 failed. Call record_result with that test_id and passed=false. Make exactly one tool call.", "expected": {"name": "record_result", "arguments": {"test_id": "schema-7", "passed": False}}},
    {"id": "tool_success", "category": "native_tools", "prompt": "Observed test parse-2 passed. Call record_result with that test_id and passed=true. Make exactly one tool call.", "expected": {"name": "record_result", "arguments": {"test_id": "parse-2", "passed": True}}},
    {"id": "tool_missing_alias", "category": "native_tools", "prompt": "I want a checkpoint inspected but have not specified its alias. Do not invent an alias or call a tool. Respond only with JSON {\"needs_alias\":true}.", "expected": {"needs_alias": True}, "no_call": True},
]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def grade(case, response):
    if response.get("done_reason") == "length":
        return False, "output_budget_exhausted"
    message = response.get("message", {})
    calls = message.get("tool_calls", [])
    if case["category"] == "native_tools" and not case.get("no_call"):
        if len(calls) != 1:
            return False, "tool_call_count"
        function = calls[0].get("function", {})
        actual = {"name": function.get("name"), "arguments": function.get("arguments")}
    else:
        if calls:
            return False, "unexpected_tool_call"
        try:
            actual = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            return False, "invalid_json"
    matched = canonical(actual) == canonical(case["expected"])
    return matched, "correct" if matched else "wrong_answer_or_schema"


def request(base, endpoint, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + "/api/" + endpoint, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as result:
        return json.load(result)


def summarize(rows):
    groups = {}
    for category in sorted({r["category"] for r in rows}):
        selected = [r for r in rows if r["category"] == category]
        groups[category] = {"passed": sum(r.get("passed") is True for r in selected), "attempted": len(selected),
                            "infrastructure_errors": sum(r["outcome"] == "infrastructure_error" for r in selected)}
    return groups


def run(base, model, output, repeats=2, think=False, max_tokens=512):
    if not 1 <= repeats <= 3:
        raise ValueError("repeats must be 1..3")
    if not 128 <= max_tokens <= 4096:
        raise ValueError("max_tokens must be 128..4096")
    options = dict(OPTIONS, num_predict=max_tokens)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    tags = request(base, "tags")
    entry = next((m for m in tags["models"] if m["name"] == model), None)
    if entry is None:
        raise ValueError("Requested model is not installed; no fallback or download permitted")
    manifest = {"benchmark": "sabrina-diagnostic-v1", "exploitgym_score": None,
                "model": entry, "ollama_version": request(base, "version"),
                "system": SYSTEM, "options": options, "think": think, "repeats": repeats,
                "suite": SUITE, "tools": TOOLS, "suite_sha256": hashlib.sha256(canonical(SUITE).encode()).hexdigest(),
                "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "limitations": ["Small custom diagnostic, not an ExploitGym or coding benchmark score", "No generated code or tool call is executed", "Same-seed repeats measure repeatability, not independent samples", "No training or candidate comparison performed"]}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2))
    rows = []
    with (output / "results.jsonl").open("w") as stream:
        for repeat in range(repeats):
            for case in SUITE:
                payload = {"model": model, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": case["prompt"]}],
                           "stream": False, "think": think, "options": options}
                if case["category"] == "native_tools":
                    payload["tools"] = TOOLS
                start = time.monotonic()
                row = {"id": case["id"], "repeat": repeat, "category": case["category"]}
                try:
                    response = request(base, "chat", payload)
                    if response.get("model") != model or not response.get("done"):
                        raise RuntimeError("Unexpected serving model or incomplete response")
                    passed, reason = grade(case, response)
                    row.update(passed=passed, outcome=reason, response=response)
                except Exception as exc:
                    row.update(passed=None, outcome="infrastructure_error", error=f"{type(exc).__name__}: {exc}")
                row["wall_seconds"] = round(time.monotonic() - start, 3)
                rows.append(row)
                stream.write(json.dumps(row) + "\n")
                stream.flush()
                print(json.dumps({k: v for k, v in row.items() if k != "response"}), flush=True)
                if row["outcome"] == "infrastructure_error":
                    break
            if rows[-1]["outcome"] == "infrastructure_error":
                break
    after = next((m for m in request(base, "tags")["models"] if m["name"] == model), {})
    summary = {"benchmark": manifest["benchmark"], "exploitgym_score": None, "model_digest_unchanged": after.get("digest") == entry["digest"],
               "completed": len(rows) == len(SUITE) * repeats and all(r["outcome"] != "infrastructure_error" for r in rows),
               "by_category": summarize(rows), "outcomes": dict(collections.Counter(r["outcome"] for r in rows)), "run_directory": str(output)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--think", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=512)
    args = parser.parse_args()
    run(args.base, args.model, args.output, args.repeats, args.think, args.max_tokens)
