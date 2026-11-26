#!/usr/bin/env python3
"""
Swarm Autonomous Code Generation: Hierarchical Cognitive Blackboard
Prompts live external models to write the complete Python implementation of the
HierarchicalCognitiveBlackboard class, verifies syntax, and runs live benchmarks.
"""
from __future__ import annotations

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

OUTPUT_DIR = REPO_ROOT / "artifacts" / "model_authored_code"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def extract_python_code(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("FULL REPLY:"):
        cleaned = cleaned[len("FULL REPLY:"):].strip()
    blocks = re.findall(r"```(?:python)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if blocks:
        for b in sorted(blocks, key=len, reverse=True):
            if "class HierarchicalCognitiveBlackboard" in b:
                return b.strip()
        return blocks[0].strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def main():
    print("=" * 72)
    print("  SWARM AUTONOMOUS IMPLEMENTATION: COGNITIVE BLACKBOARD V2 (THREAD-SAFE)")
    print("=" * 72)

    router = CollectiveRouter(db_path=DEFAULT_DB_PATH, min_cooldown_sec=1.0)

    prompt = (
        "TASK: Write the hardened v2 standalone Python class `HierarchicalCognitiveBlackboard` fixing 3 counterexamples found in v1.\n\n"
        "COUNTEREXAMPLES TO FIX:\n"
        "1. Multi-thread safety: Initialize `self.db_path = str(db_path)` and `self._lock = threading.Lock()`. "
        "Provide a `_connect(self)` helper that opens `sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)`, sets `conn.row_factory = sqlite3.Row`, "
        "and executes `PRAGMA journal_mode=WAL`. Also set `self.conn = self._connect()` in `__init__` for backwards compatibility, and wrap every method operation in `with self._lock:` using `self.conn` (or `self._connect()`).\n"
        "2. Pattern counter increment: In `record_solution_pattern(self, pat_id, domain, bug_sig, remedy, ast_patch)`, insert with `success_count=1` and `last_applied=now`, "
        "and use `ON CONFLICT(pattern_id) DO UPDATE SET domain=excluded.domain, bug_signature=excluded.bug_signature, remedy=excluded.remedy, ast_patch=excluded.ast_patch, success_count=semantic_solution_patterns.success_count + 1, last_applied=excluded.last_applied`.\n"
        "3. Python 3.12 compatibility: Use `datetime.datetime.now(datetime.timezone.utc).isoformat()` instead of deprecated `utcnow()`.\n\n"
        "TABLES & METHODS REQUIRED:\n"
        "- `working_hypotheses` (hypothesis_id TEXT PRIMARY KEY, domain TEXT, model_id TEXT, statement TEXT, confidence REAL, status TEXT, created_at TEXT)\n"
        "- `semantic_solution_patterns` (pattern_id TEXT PRIMARY KEY, domain TEXT, bug_signature TEXT, remedy TEXT, ast_patch TEXT, success_count INTEGER DEFAULT 0, last_applied TEXT)\n"
        "- `model_competence_matrix` (model_id TEXT, domain TEXT, attempts INTEGER DEFAULT 0, syntax_passed INTEGER DEFAULT 0, tests_passed INTEGER DEFAULT 0, avg_latency REAL DEFAULT 0, reliability REAL DEFAULT 0, PRIMARY KEY(model_id, domain))\n"
        "- Methods:\n"
        "  * `__init__(self, db_path)`\n"
        "  * `record_hypothesis(self, hyp_id, domain, model_id, stmt, conf, status)`\n"
        "  * `record_solution_pattern(self, pat_id, domain, bug_sig, remedy, ast_patch)`\n"
        "  * `query_patterns(self, domain, bug_sig=None) -> list[dict]` (ordered by `success_count DESC`)\n"
        "  * `update_model_competence(self, model_id, domain, syntax_ok, tests_ok, latency_ms)`\n"
        "  * `get_competence_ranking(self, domain) -> list[dict]` (ordered by `reliability DESC, avg_latency ASC`)\n"
        "  * `close(self)` (safely closes `self.conn` under `self._lock`)\n\n"
        "Return ONLY valid, executable Python code with no markdown preamble."
    )

    models_to_build = [
        ("hf-gradio-hamza46758-my-gaia-agent", "My-GAIA-Agent", "tier_3"),
        ("hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "Agentic-RAG", "tier_1"),
        ("pollinations-openai-fast", "OpenAI-Fast", "tier_2"),
    ]

    for portal_id, name, tier in models_to_build:
        print(f"\n[*] Requesting v2 code implementation from {name} ({portal_id})...", flush=True)
        t0 = time.perf_counter()
        res = router.pool.invoke_member(portal_id, prompt, "cognitive_blackboard_v2", False)
        if not res.get("reply"):
            res = router.route(prompt, tier=tier, use_cache=False, allow_tier_escalation=True)
        elapsed = round((time.perf_counter() - t0) * 1000.0, 2)

        code = extract_python_code(res.get("reply", ""))
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", name).lower()
        file_out = OUTPUT_DIR / f"{slug}_cognitive_blackboard_v2.py"
        file_out.write_text(code, encoding="utf-8")

        # Compile check
        compiled = False
        err = None
        try:
            py_compile.compile(str(file_out), doraise=True)
            compiled = True
        except Exception as ex:
            err = str(ex)

        print(f"  [+] Elapsed: {elapsed} ms | Bytes: {len(code)}")
        print(f"  [+] Saved to: {file_out.relative_to(REPO_ROOT)}")
        print(f"  [+] Syntax Valid (`py_compile`): {compiled}")
        if err:
            print(f"  [!] Compile Error: {err}")


if __name__ == "__main__":
    main()
