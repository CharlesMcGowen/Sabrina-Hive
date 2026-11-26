"""Executable development curriculum and separate input-held-out model research pilot.

This is a small infrastructure pilot, not an ExploitGym or trading benchmark.
Tool success comes from world state, never from the model's success claim.
"""
import copy
import json
import random
import re

from tools.tool_interop_identity import IdentityWorld, gold_actions, make_cases
from tools.dm_campaign_tasks import FAMILIES, make_task, reference

VERSION = "tool-interop-pilot-1"


class GenerationBudgetExceeded(RuntimeError):
    """The candidate exceeded a declared inference budget, not an infrastructure fault."""


def tool_schema(world):
    result = []
    for description in world.descriptions():
        properties = {}
        for name, value in description["arguments"].items():
            kind, explanation = value.split(": ", 1)
            properties[name] = {"type": kind, "description": explanation}
        result.append({"type": "function", "function": {
            "name": description["name"],
            "description": "Returns " + json.dumps(description["returns"]),
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}})
    return result


def cases(seed, count):
    rng = random.Random(seed)
    templates = make_cases()
    result = []
    for index in range(count):
        case = copy.deepcopy(templates[index % len(templates)])
        case.update(id=f"{seed}-{index}", base=index, split="generated")
        case["title"] = f"Record {rng.randrange(10**9)}"
        case["target_id"], case["decoy_id"] = [f"id-{v}" for v in rng.sample(range(10**12), 2)]
        case["body"] = f"Verified value {rng.randrange(10**9)}."
        case["decoy_body"] = f"Obsolete value {rng.randrange(10**9)}."
        result.append(case)
    return result


def initial_messages(case):
    return [{"role": "system", "content": "Complete the task using actual tool results. Call one tool at a time. Never invent IDs or claim unexecuted actions."},
            {"role": "user", "content": f"Read the record titled {case['title']!r}, save its exact contents, then verify the saved artifact."}]


def call_message(action):
    return {"role": "assistant", "content": "", "tool_calls": [{"type": "function", "function": {
        "name": action["tool"], "arguments": action["arguments"]}}]}


def math_cases(seed, count):
    rng = random.Random(seed)
    for index in range(count):
        a, b, c = [rng.randrange(2, 80) for _ in range(3)]
        yield {"id": f"math-{seed}-{index}", "prompt": f"Compute ({a} * {b}) - {c}. Return only a JSON integer.",
               "answer": a * b - c}


def training_records(seed=731101):
    records = []
    # Every target call is executed and checked before becoming training data.
    for case in cases(seed, 32):
        world = IdentityWorld(case)
        messages = initial_messages(case)
        for action in gold_actions(case):
            completion = call_message(action)
            event = world.execute(action)
            if "error" in event["result"]:
                raise ValueError("Invalid teacher trace")
            records.append({"prompt": copy.deepcopy(messages), "completion": [completion],
                            "tools": tool_schema(world), "family": "tool_interop"})
            messages += [completion, {"role": "tool", "name": action["tool"],
                                      "content": json.dumps(event["result"])}]
        if not world.success():
            raise ValueError("Teacher did not complete the task")
    for case in math_cases(seed, 24):
        records.append({"prompt": [{"role": "user", "content": case["prompt"]}],
                        "completion": [{"role": "assistant", "content": str(case["answer"])}],
                        "tools": [], "family": "math"})
    for index, family in enumerate(FAMILIES):
        task = make_task(family, index % 4, seed + 100 + index)
        records.append({"prompt": [{"role": "user", "content": task["spec"] + " Return only Python code."}],
                        "completion": [{"role": "assistant", "content": reference(task)}],
                        "tools": [], "family": "coding"})
    return records


def render_record(tokenizer, record):
    """Freeze the actual native tool template; fail if it drops schemas or calls.

    Render to prompt/completion strings so TRL's completion mask does not require
    a generation-tagged chat template and never trains on tool responses.
    """
    options = {"tools": record["tools"], "tokenize": False}
    prompt = tokenizer.apply_chat_template(record["prompt"], add_generation_prompt=True, **options)
    full = tokenizer.apply_chat_template(record["prompt"] + record["completion"], add_generation_prompt=False, **options)
    if not full.startswith(prompt):
        raise ValueError("Chat template does not preserve the assistant prefix")
    if record["tools"]:
        if any(t["function"]["name"] not in prompt for t in record["tools"]):
            raise ValueError("Chat template discarded tool definitions")
        if "<tool_call>" not in full[len(prompt):]:
            raise ValueError("Chat template discarded assistant function call")
    return {"prompt": prompt, "completion": full[len(prompt):]}


def parse_native_call(answer):
    match = re.fullmatch(r"\s*<tool_call>\s*(.*?)\s*</tool_call>\s*", answer, re.S)
    if not match:
        raise ValueError("Expected exactly one native function call")
    value = json.loads(match.group(1))
    if not isinstance(value, dict) or set(value) != {"name", "arguments"}:
        raise ValueError("Expected name and arguments")
    return {"tool": value["name"], "arguments": value["arguments"]}


def evaluate_tools(generate, seed, count=8, max_turns=8):
    reports = []
    for case in cases(seed, count):
        world = IdentityWorld(case)
        messages = initial_messages(case)
        attempts = []
        for _ in range(max_turns):
            try:
                answer = generate(messages, tool_schema(world))
            except GenerationBudgetExceeded as error:
                attempts.append({"error": str(error), "error_type": "prompt_budget"})
                break
            try:
                action = parse_native_call(answer)
                event = world.execute(action)
                messages += [call_message(action), {"role": "tool", "name": action["tool"], "content": json.dumps(event["result"])}]
                attempts.append({"answer": answer, "event": event})
            except (ValueError, TypeError, KeyError) as error:
                attempts.append({"answer": answer, "error": str(error)})
                messages += [{"role": "assistant", "content": answer},
                             {"role": "user", "content": "Invalid call. Use one documented native function call."}]
            if world.success():
                break
        reports.append({"id": case["id"], "passed": world.success(), "attempts": attempts})
    return {"passed": sum(r["passed"] for r in reports), "total": count, "cases": reports}


def evaluate_math(generate, seed, count=12):
    reports = []
    for case in math_cases(seed, count):
        answer = generate([{"role": "user", "content": case["prompt"]}], [])
        try:
            value = json.loads(answer)
            passed = type(value) is int and value == case["answer"]
        except (ValueError, TypeError):
            passed = False
        reports.append({"id": case["id"], "passed": passed, "answer": answer})
    return {"passed": sum(r["passed"] for r in reports), "total": count, "cases": reports}
