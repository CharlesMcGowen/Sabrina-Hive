#!/usr/bin/env python3
"""Bounded, local Ollama supervisor/worker experiment with deterministic test tools.

No generated code is executed. Test subjects are deliberately buggy toy functions.
All model requests stay on lab-local Ollama; this does not start the Sabrina swarm.
"""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import time
import urllib.request

SUPERVISOR = "huihui_ai/qwen3-abliterated:14b"
WORKER = "qwen2.5:3b"
SUBJECTS = [
    {"id": "normalize_email", "spec": "Trim leading/trailing whitespace, then lowercase an email string.",
     "schema": 'cases: list of objects, each {"text": string}'},
    {"id": "chunks", "spec": "Split an input list into chunks of size k, preserving all items including a final partial chunk. k is a positive integer.",
     "schema": 'cases: list of objects, each {"items": list of integers, "k": integer 1..8}'},
    {"id": "retry_delay", "spec": "For a nonnegative integer attempt, delay is 2 raised to attempt, capped at 8 seconds.",
     "schema": 'cases: list of objects, each {"attempt": integer 0..12}'},
]


def post(path, payload):
    request = urllib.request.Request("http://127.0.0.1:11434" + path,
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.load(response)


def memory():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=5) as response:
            loaded = json.load(response)["models"]
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
        return {"gpu_mib_used_total": gpu.stdout.strip(), "models": [
            {"name": m["name"], "size": m.get("size"), "size_vram": m.get("size_vram"),
             "context_length": m.get("context_length")} for m in loaded]}
    except Exception as exc:
        return {"error": str(exc)}


def evaluate(subject, cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 6:
        raise ValueError("Need 1–6 test cases")
    results = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Each case must be an object")
        if subject == "normalize_email":
            text = case.get("text")
            if not isinstance(text, str) or len(text) > 200:
                raise ValueError("text must be a string of at most 200 characters")
            expected, actual = text.strip().lower(), text.lower()
        elif subject == "chunks":
            items, k = case.get("items"), case.get("k")
            if not isinstance(items, list) or len(items) > 16 or any(type(i) is not int for i in items):
                raise ValueError("items must be a list of at most 16 integers")
            if type(k) is not int or not 1 <= k <= 8:
                raise ValueError("k must be 1–8")
            expected = [items[i:i+k] for i in range(0, len(items), k)]
            actual = [items[i:i+k] for i in range(0, len(items)-k+1, k)]
        else:
            attempt = case.get("attempt")
            if type(attempt) is not int or not 0 <= attempt <= 12:
                raise ValueError("attempt must be 0–12")
            expected, actual = min(2 ** attempt, 8), 2 ** attempt
        results.append({"input": case, "expected": expected, "actual": actual, "passed": expected == actual})
    return results


def run(args):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = Path(args.output_root).expanduser() / stamp
    root.mkdir(parents=True)
    report = {"supervisor": args.supervisor, "worker": args.worker, "context": 2048,
              "status": "running", "calls": [], "delegated": [], "baseline": [], "memory_before": memory()}

    def save():
        (root / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    def call(model, stage, system, prompt, limit=350):
        print(f"START {stage} [{model}]", flush=True)
        started = time.monotonic()
        response = post("/api/chat", {"model": model, "stream": False, "think": False,
                        "format": "json", "keep_alive": "5m", "messages": [
                            {"role": "system", "content": system}, {"role": "user", "content": prompt}],
                        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": limit}})
        content = response.get("message", {}).get("content", "")
        entry = {"stage": stage, "model": model, "system": system, "prompt": prompt,
                 "response": content, "elapsed_s": round(time.monotonic()-started, 3),
                 "prompt_tokens": response.get("prompt_eval_count", 0), "output_tokens": response.get("eval_count", 0),
                 "load_s": round(response.get("load_duration", 0)/1e9, 3),
                 "generation_s": round(response.get("eval_duration", 0)/1e9, 3),
                 "done_reason": response.get("done_reason"), "memory_after": memory()}
        report["calls"].append(entry)
        save()
        print(f"DONE {stage}: {entry['elapsed_s']}s, {entry['output_tokens']} output tokens", flush=True)
        return json.loads(content)

    def test_task(subject, model, instructions, phase):
        item = {"id": subject["id"], "status": "inconclusive"}
        report[phase].append(item)
        try:
            proposal = call(model, f"{phase}/{subject['id']}/tests",
                            'You test a black-box function using a fixed evaluate_cases tool. Return JSON only: {"cases": [...]}. Choose at most 6 valid inputs to expose specification violations. You cannot see the implementation.',
                            json.dumps({"subject": subject, "assignment": instructions}), 300)
            evidence = evaluate(subject["id"], proposal.get("cases"))
            item.update(cases=proposal["cases"], evidence=evidence,
                        detected_bug=any(not row["passed"] for row in evidence))
            if phase == "delegated":
                conclusion = call(model, f"{phase}/{subject['id']}/report",
                                  'Report only evidence provided by evaluate_cases. Return JSON: {"status":"bug_found|no_bug_observed|inconclusive", "summary":"brief", "failed_case_indices":[zero-based indices]}. Do not invent additional tests.',
                                  json.dumps({"spec": subject["spec"], "tool_results": evidence}), 220)
                item["worker_report"] = conclusion
                expected_indices = [i for i, e in enumerate(evidence) if not e["passed"]]
                item["report_grounded"] = (conclusion.get("failed_case_indices") == expected_indices and
                                           conclusion.get("status") == ("bug_found" if expected_indices else "no_bug_observed"))
            item["status"] = "complete"
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"
        save()

    try:
        save()
        plan = call(args.supervisor, "supervisor/assign",
                    'You supervise a smaller testing agent. For each supplied subject give one precise testing assignment. The agent can call evaluate_cases once with at most 6 cases, then report evidence. Do not do the tests yourself. Return JSON {"tasks":[{"id":"exact subject id", "instruction":"concise task"}]}. Include every subject exactly once.',
                    json.dumps(SUBJECTS), 450)
        assignments = plan.get("tasks")
        if not isinstance(assignments, list) or len(assignments) != len(SUBJECTS):
            raise ValueError("Supervisor did not return three assignments")
        mapping = {task["id"]: task["instruction"] for task in assignments}
        if set(mapping) != {s["id"] for s in SUBJECTS} or any(not isinstance(v, str) for v in mapping.values()):
            raise ValueError("Supervisor returned invalid task IDs/instructions")
        report["assignments"] = assignments
        for subject in SUBJECTS:
            test_task(subject, args.worker, mapping[subject["id"]], "delegated")
        report["supervisor_review"] = call(args.supervisor, "supervisor/review",
            'Review smaller-agent reports against supplied evidence. Return JSON {"tasks":[{"id":"subject id", "accept":true, "reason":"short"}]}. Reject unsupported claims or inconclusive results. Do not invent test evidence.',
            json.dumps(report["delegated"]), 400)
        for subject in SUBJECTS:
            test_task(subject, args.supervisor, "Choose test cases for this specification.", "baseline")
        for phase in ("delegated", "baseline"):
            report[phase + "_bugs_detected"] = sum(bool(row.get("detected_bug")) for row in report[phase])
        report["status"] = "complete"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    report["memory_after"] = memory()
    save()
    print(f"RESULTS {root / 'results.json'}", flush=True)
    print(json.dumps({k: v for k, v in report.items() if k in ("status", "error", "delegated_bugs_detected", "baseline_bugs_detected")}), flush=True)
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supervisor", default=SUPERVISOR)
    parser.add_argument("--worker", default=WORKER)
    parser.add_argument("--output-root", default="~/.local/share/sabrina/delegation_experiments")
    raise SystemExit(run(parser.parse_args()))
