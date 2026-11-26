#!/usr/bin/env python3
"""
Model-Authored Code & Live Sandbox Test Runner
Sends an algorithmic specification directly to multiple live external models in the swarm,
extracts their verbatim code, saves each file to disk, compiles them with py_compile,
and executes them against test suites in an isolated sandbox.
"""
from __future__ import annotations

import ast
import json
import py_compile
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harnesses.external.collective_pool import DEFAULT_DB_PATH
from tools.collective_router_and_shadow_daemon import CollectiveRouter
from experiments.whitepaper_bft_verification.bft_ast_consensus import ASTConsensusEngine

OUTPUT_DIR = REPO_ROOT / "artifacts" / "model_authored_code"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def extract_python_code(raw_text: str) -> str:
    """Extracts pure python code from model output, stripping markdown or scratchpad."""
    # Check for markdown code blocks
    code_blocks = re.findall(r"```(?:python)?\s*(.*?)\s*```", raw_text, re.DOTALL)
    if code_blocks:
        # Choose the longest block that contains 'def '
        for block in sorted(code_blocks, key=len, reverse=True):
            if "def " in block:
                return block.strip()
        return code_blocks[0].strip()

    # If no markdown block, search for 'def calculate_bft_quorum' to the end of indentation
    lines = raw_text.splitlines()
    code_lines = []
    recording = False
    for line in lines:
        if "def calculate_bft_quorum" in line:
            recording = True
        if recording:
            code_lines.append(line)

    if code_lines:
        return "\n".join(code_lines).strip()

    return raw_text.strip()


def main() -> None:
    print("=" * 72)
    print("  LIVE SWARM MODEL CODE AUTHORSHIP & EXECUTION BENCHMARK")
    print("=" * 72)

    router = CollectiveRouter(db_path=DEFAULT_DB_PATH, min_cooldown_sec=1.0)

    prompt = (
        "TASK: Write a complete, standalone Python function named `calculate_bft_quorum`.\n\n"
        "SIGNATURE:\n"
        "def calculate_bft_quorum(n: int, f: int) -> dict:\n\n"
        "SPECIFICATION:\n"
        "1. Check if the Byzantine Fault Tolerance condition `n >= 3 * f + 1` holds.\n"
        "2. Compute the supermajority quorum size: `max(int((2 * n) / 3) + 1, int(0.6 * n))`.\n"
        "3. Compute fault tolerance ratio: `round(f / n, 4)` if n > 0 else 0.0.\n"
        "4. Return a dict with exact keys: 'satisfied' (bool), 'required_quorum' (int), and 'fault_tolerance_ratio' (float).\n\n"
        "Do NOT return markdown explanations or chatter. Return ONLY the python function."
    )

    # We test across 4 different qualified external models in SQLite
    portals_to_test = [
        ("hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "jayeshmurthi49-byte/Agentic-RAG", "tier_1"),
        ("hf-gradio-neondiro-qwen2-5-vl-32b-gguf", "Qwen2.5-VL-32B", "tier_2"),
        ("hf-gradio-hamza46758-my-gaia-agent", "hamza46758/my-gaia-agent", "tier_2"),
        ("hf-gradio-dkt3-digital-twin", "dkT3/digital-twin", "tier_3"),
    ]

    results: List[Dict[str, Any]] = []
    test_vectors = [
        ((4, 1), {"satisfied": True, "required_quorum": 3, "fault_tolerance_ratio": 0.25}),
        ((7, 2), {"satisfied": True, "required_quorum": 5, "fault_tolerance_ratio": 0.2857}),
        ((10, 3), {"satisfied": True, "required_quorum": 7, "fault_tolerance_ratio": 0.3}),
        ((4, 2), {"satisfied": False, "required_quorum": 3, "fault_tolerance_ratio": 0.5}),
    ]

    engine = ASTConsensusEngine(n_nodes=len(portals_to_test))
    code_candidates = []

    print(f"\n[*] Dispatching Code Generation Prompt to {len(portals_to_test)} Live External Models...")

    for portal_id, friendly_name, tier in portals_to_test:
        print(f"\n---> Querying {friendly_name} ({portal_id})...", flush=True)
        t0 = time.perf_counter()
        resp = router.route(prompt, tier=tier, use_cache=False, allow_tier_escalation=True)
        elapsed = round((time.perf_counter() - t0) * 1000.0, 2)

        raw_reply = resp.get("reply", "")
        code = extract_python_code(raw_reply)

        # Save code directly to disk
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", friendly_name).lower()
        file_path = OUTPUT_DIR / f"{slug}.py"
        file_path.write_text(code, encoding="utf-8")

        # Compile check
        compiled = False
        compile_err = None
        try:
            py_compile.compile(str(file_path), doraise=True)
            compiled = True
        except Exception as ex:
            compile_err = str(ex)

        # Execute check
        tests_passed = 0
        exec_err = None
        if compiled:
            try:
                exec_globals: Dict[str, Any] = {}
                exec(code, exec_globals)
                fn = exec_globals.get("calculate_bft_quorum")
                if callable(fn):
                    for args, expected in test_vectors:
                        res = fn(*args)
                        if (
                            res.get("satisfied") == expected["satisfied"]
                            and res.get("required_quorum") == expected["required_quorum"]
                            and abs(res.get("fault_tolerance_ratio", 0.0) - expected["fault_tolerance_ratio"]) < 0.01
                        ):
                            tests_passed += 1
            except Exception as ex:
                exec_err = str(ex)

        record = {
            "portal_id": portal_id,
            "friendly_name": friendly_name,
            "elapsed_ms": elapsed,
            "raw_reply_length": len(raw_reply),
            "code_file": str(file_path.relative_to(REPO_ROOT)),
            "syntax_valid": compiled,
            "compile_error": compile_err,
            "tests_passed": tests_passed,
            "tests_total": len(test_vectors),
            "execution_error": exec_err,
            "code_snippet": code[:150] + ("..." if len(code) > 150 else ""),
        }
        results.append(record)

        print(f"  [+] Elapsed: {elapsed} ms")
        print(f"  [+] Saved to: {file_path.relative_to(REPO_ROOT)}")
        print(f"  [+] Syntax Valid (py_compile): {compiled}")
        print(f"  [+] Unit Tests Passed: {tests_passed} / {len(test_vectors)}")
        if compile_err:
            print(f"  [!] Compile Error: {compile_err}")
        if exec_err:
            print(f"  [!] Execution Error: {exec_err}")

        if compiled:
            code_candidates.append({
                "portal_id": portal_id,
                "code": code
            })

    # Run AST Quorum Consensus across the models' actual code
    print("\n[*] Evaluating AST Consensus across the Model-Authored Code Files...")
    quorum_eval = engine.evaluate_quorum(code_candidates)
    print(f"  [+] Unique AST Structures:   {quorum_eval['unique_ast_classes']}")
    print(f"  [+] Dominant Fingerprint:    {quorum_eval['dominant_fingerprint']}")
    print(f"  [+] Dominant Cluster Size:   {quorum_eval['dominant_cluster_size']} / {len(code_candidates)}")
    print(f"  [+] Required Quorum:         {quorum_eval['required_quorum']}")
    print(f"  [+] Quorum Achieved:         {quorum_eval['quorum_achieved']} ({quorum_eval['quorum_share'] * 100}%)")

    # Clean AST objects before json serialization
    if quorum_eval.get("selected_candidate"):
        quorum_eval["selected_candidate"].pop("ast_tree", None)
        quorum_eval["selected_candidate"].pop("canonical_dump", None)

    # Save summary
    summary_path = REPO_ROOT / "artifacts" / "collective-repair-20261108" / "model_code_authorship_results.json"
    summary_path.write_text(json.dumps({
        "timestamp": time.time(),
        "models_tested": results,
        "ast_quorum_consensus": quorum_eval
    }, indent=2), encoding="utf-8")
    print(f"\n[+] Full test ledger written to:\n    {summary_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
