#!/usr/bin/env python3
"""
eval.py
=======
Grade a trained coder model on HumanEval and MBPP-20 by REUSING Sabrina's HumanEval-style runner:
`tools.agentic_solver.grade(code, verify_fn, exec_timeout=8) -> (score, feedback)`.

Design decisions:
  - Load HumanEval + MBPP via `datasets` (public, MIT/CC).
  - Convert each into the (name, prompt, verify_fn) shape `tools.capability_bench.run_benchmark`
    expects, so we get its solver-agnostic scoring path for free.
  - The solver is a closure over a loaded (base + full-FT weights) model that emits Python from
    a HumanEval-style prompt. Greedy decoding by default.
  - `pass@1` metric: fraction of tasks where `grade` returns score == 1.0.

Verifier construction (HumanEval): each task ships a `test` string whose `check(candidate)`
function asserts correctness. We exec the test into a namespace, take `check`, and call it with
the model's produced entry-point function -- exactly HumanEval's own evaluation pattern.

Verifier construction (MBPP): each task has a `test_list` of assert statements. We exec each
against a namespace containing the produced code and return True iff all pass.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_TOOLS = _HERE.parent
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from agentic_solver import grade, _extract_code  # noqa: E402


# ── HumanEval → capability_bench tuple ─────────────────────────────────────────

def _humaneval_verify_fn(test_src: str, entry_point: str) -> Callable[[Dict[str, Any]], bool]:
    """Build a verify_fn(ns) that runs HumanEval's own `check(candidate)` against ns[entry_point]."""
    def verify(ns: Dict[str, Any]) -> bool:
        if entry_point not in ns:
            return False
        local: Dict[str, Any] = {}
        try:
            exec(test_src, local)
        except Exception:
            return False
        check = local.get("check")
        if check is None:
            return False
        try:
            check(ns[entry_point])
            return True
        except Exception:
            return False
    return verify


def humaneval_task_to_entry(task: Dict[str, Any]) -> Tuple[str, str, Callable]:
    """(name, prompt, verify_fn) tuple in capability_bench's shape."""
    name = task["task_id"]
    prompt = (task["prompt"] + "\n\n"
              "Respond with ONLY a ```python code block that completes this function.")
    verify = _humaneval_verify_fn(task["test"], task["entry_point"])
    return name, prompt, verify


def load_humaneval(limit: Optional[int] = None) -> List[Tuple[str, str, Callable]]:
    from datasets import load_dataset
    # Newer datasets library (>= ~3.0) rejects bare "openai_humaneval" with
    # HfUriError: Repository id must be 'namespace/name'. The dataset lives at
    # 'openai/openai_humaneval' with the same schema; keep a fallback to the
    # bare name for older environments that still resolve it.
    try:
        ds = load_dataset("openai/openai_humaneval", split="test")
    except Exception:
        ds = load_dataset("openai_humaneval", split="test")
    tasks = list(ds)
    if limit:
        tasks = tasks[:limit]
    return [humaneval_task_to_entry(t) for t in tasks]


# ── MBPP → capability_bench tuple ──────────────────────────────────────────────

def _mbpp_verify_fn(test_list: List[str]) -> Callable[[Dict[str, Any]], bool]:
    """Build verify_fn(ns) that runs each MBPP assert statement against the produced namespace."""
    def verify(ns: Dict[str, Any]) -> bool:
        for stmt in test_list:
            try:
                exec(stmt, ns)
            except Exception:
                return False
        return True
    return verify


def mbpp_task_to_entry(task: Dict[str, Any]) -> Tuple[str, str, Callable]:
    name = f"mbpp/{task.get('task_id')}"
    text = task.get("text") or task.get("prompt") or ""
    # MBPP tasks name the target function via test_list assertions. Show one example test so the
    # model knows the expected signature.
    example_test = (task.get("test_list") or [""])[0]
    prompt = (
        f"{text}\n\nExample test that must pass:\n  {example_test}\n\n"
        "Respond with ONLY a ```python code block defining the required function(s)."
    )
    verify = _mbpp_verify_fn(task.get("test_list") or [])
    return name, prompt, verify


def load_mbpp(limit: int = 20) -> List[Tuple[str, str, Callable]]:
    from datasets import load_dataset
    # Same namespaced-id story as HumanEval above.
    try:
        ds = load_dataset("google-research-datasets/mbpp", split="test")
    except Exception:
        ds = load_dataset("mbpp", split="test")
    tasks = list(ds)[:limit]
    return [mbpp_task_to_entry(t) for t in tasks]


# ── Solver over a loaded HF model ─────────────────────────────────────────────

def build_model_solver(base: str, checkpoint: str,
                       max_new_tokens: int = 512,
                       temperature: float = 0.0) -> Callable[[str], str]:
    """Return solver(prompt)->str that loads a full-FT checkpoint (or the base if checkpoint==base)."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(base)
    model = AutoModelForCausalLM.from_pretrained(
        checkpoint if checkpoint else base,
        torch_dtype=torch.bfloat16, device_map={"": 0})
    model.eval()

    def solver(prompt: str) -> str:
        msgs = [{"role": "system",
                 "content": "You are a careful Python coding assistant. Respond with a ```python "
                            "code block only when code is requested."},
                {"role": "user", "content": prompt}]
        result = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt")
        # transformers 5.14: apply_chat_template may return a BatchEncoding; handle both shapes.
        if hasattr(result, "get") and "input_ids" in getattr(result, "keys", lambda: [])():
            input_ids = result["input_ids"]
        else:
            input_ids = result if hasattr(result, "shape") else result[0]
        input_ids = input_ids.to(model.device)
        with torch.no_grad():
            out = model.generate(
                input_ids,
                max_new_tokens=max_new_tokens,
                do_sample=temperature > 0,
                temperature=temperature if temperature > 0 else None,
                pad_token_id=tok.eos_token_id,
            )
        gen = tok.decode(out[0][input_ids.shape[1]:], skip_special_tokens=True)
        return gen
    return solver


# ── run_benchmark adapter ─────────────────────────────────────────────────────

# HumanEval prompts include typing imports + function signature at the top; the
# model's completion is usually just the function body, so exec-ing ONLY the
# extracted code loses those imports (verified live: HumanEval/0-9 all failed
# with NameError: name 'List' is not defined on the first eval run). Prepending
# these common imports to every candidate is the standard HumanEval harness
# convention -- the official evaluator concatenates prompt+completion for the
# same reason. Both harmless if the model already imported them.
_TYPING_PRELUDE = (
    "from typing import List, Dict, Set, Tuple, Optional, Any, Callable, "
    "Iterable, Iterator, Union\n"
    "import math\n"
    "import re\n"
    "import collections\n"
    "import itertools\n"
    "import functools\n"
    "import heapq\n"
)


def run_benchmark(tasks: List[Tuple[str, str, Callable]],
                  solver: Callable[[str], str],
                  verbose: bool = True) -> Dict[str, Any]:
    """Score `solver` over `tasks`. Uses grade() from tools.agentic_solver.
    Returns {n, solves, pass_at_1, per_task}."""
    per_task: List[Dict[str, Any]] = []
    solves = 0
    for name, prompt, verify in tasks:
        try:
            gen = solver(prompt)
        except Exception as e:
            per_task.append({"name": name, "score": 0.0,
                             "feedback": f"solver raised: {type(e).__name__}: {e!r}"})
            if verbose:
                print(f"  {name}: 0.0 (solver raised)", flush=True)
            continue
        code = _TYPING_PRELUDE + _extract_code(gen)
        score, fb = grade(code, verify)
        per_task.append({"name": name, "score": score, "feedback": fb[:200]})
        if score == 1.0:
            solves += 1
        if verbose:
            print(f"  {name}: {score:.2f}  {fb[:80]}", flush=True)
    return {
        "n": len(tasks), "solves": solves,
        "pass_at_1": solves / len(tasks) if tasks else 0.0,
        "per_task": per_task,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="base model id (Qwen/Qwen2.5-Coder-1.5B-Instruct)")
    ap.add_argument("--checkpoint", default="",
                    help="path to a full-FT checkpoint dir; empty means evaluate the base model")
    ap.add_argument("--humaneval-limit", type=int, default=None, help="cap HumanEval tasks (all if None)")
    ap.add_argument("--mbpp-limit", type=int, default=20)
    ap.add_argument("--out", default="coder_eval.json", help="where to write per-task results JSON")
    a = ap.parse_args()

    print(f"[coder-eval] base={a.base}   checkpoint={a.checkpoint or '(none, using base)'}", flush=True)
    print("[coder-eval] loading HumanEval + MBPP-20 tasks ...", flush=True)
    he = load_humaneval(limit=a.humaneval_limit)
    mp = load_mbpp(limit=a.mbpp_limit)
    print(f"[coder-eval] humaneval={len(he)}  mbpp={len(mp)}", flush=True)

    solver = build_model_solver(a.base, a.checkpoint)

    print("\n[coder-eval] === HumanEval ===", flush=True)
    he_r = run_benchmark(he, solver)
    print(f"[coder-eval] HumanEval pass@1 = {he_r['solves']}/{he_r['n']} = {he_r['pass_at_1']:.3f}",
          flush=True)

    print("\n[coder-eval] === MBPP-20 ===", flush=True)
    mp_r = run_benchmark(mp, solver)
    print(f"[coder-eval] MBPP-20 pass@1 = {mp_r['solves']}/{mp_r['n']} = {mp_r['pass_at_1']:.3f}",
          flush=True)

    result = {"base": a.base, "checkpoint": a.checkpoint,
              "humaneval": he_r, "mbpp": mp_r}
    with open(a.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n[coder-eval] per-task results -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
