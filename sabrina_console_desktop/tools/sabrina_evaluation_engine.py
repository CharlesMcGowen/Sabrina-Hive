"""Sabrina Model & Tool Path Benchmark, Qualification, and Routing Engine.







Stores campaign, inventory, trial, retrieval, feedback comparison, qualification,







and routing records in ``data/sabrina_evaluations.sqlite3``, linked to existing







run and audit IDs. Numerical results and version fingerprints are preserved







independently of raw audit retention expiry.







"""







from __future__ import annotations







import datetime as dt







import hashlib







import json







import math







import os







import re







import sqlite3







import subprocess







import sys







import tempfile







import time







import urllib.error







import urllib.request







import uuid







from pathlib import Path







from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple







ROOT = Path(__file__).resolve().parents[1]







if str(ROOT) not in sys.path:







    sys.path.insert(0, str(ROOT))







from tools.model_cpu_registry import load_registry, resolve_num_threads







from tools.sabrina_host_execution import HarnessFreezeGuard, compute_harness_fingerprints







DEFAULT_EVAL_DB_PATH = ROOT / "data" / "sabrina_evaluations.sqlite3"







DEFAULT_COORDINATION_DB_PATH = ROOT / "data" / "sabrina_coordination.sqlite3"







DEFAULT_OLLAMA_URL = os.environ.get("SABRINA_OLLAMA_URL", "http://ego.local:11434").rstrip("/")







DEFAULT_ORCHESTRATOR_MODEL = "qwen2.5-coder:14b"







RESULT_CATEGORIES = ("model_alone", "model_using_tools", "sabrina_assisted_by_app")







EXECUTION_MODES = ("live", "replay", "simulation")







TASK_CATEGORIES = (







    "structured_instructions",







    "coding_executable_checks",







    "tool_selection",







    "recovery_incomplete_failed_obs",







)







BLOCKER_STATUSES = (







    "resource_wait",







    "quota_limit",







    "auth_failure",







    "app_failure",







    "fingerprint_invalidated",







    "unsupported",







)







class FrozenTaskIntegrityError(RuntimeError):







    """Raised when an attempt is made to mutate frozen benchmark tasks or self-grade."""







def _now() -> float:







    return time.time()







def _sha256_text(text: str) -> str:







    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()







def _canonical_json(obj: Any) -> str:







    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))







def _wilson_interval(passes: int, total: int, z: float = 1.96) -> Tuple[float, float]:







    """Return 95% Wilson score confidence interval (low, high) for a binomial proportion."""







    if total <= 0:







        return (0.0, 1.0)







    p = passes / total







    denom = 1.0 + (z * z) / total







    center = (p + (z * z) / (2.0 * total)) / denom







    half = (







        z







        * math.sqrt((p * (1.0 - p) / total) + ((z * z) / (4.0 * total * total)))







        / denom







    )







    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))







# ---------------------------------------------------------------------------







# Frozen Benchmark Suite: 12 Benchmark Tasks + 6 Held-Out Qualification Tasks







# + Frozen Retrieval Evaluation Corpus for Embedding Models







# ---------------------------------------------------------------------------







FROZEN_GENERATION_TASKS: Tuple[Dict[str, Any], ...] = (







    # --- Category 1: structured_instructions (3 benchmark) ---







    {







        "task_id": "bench_struct_01",







        "split": "benchmark",







        "category": "structured_instructions",







        "title": "Strict JSON Release Manifest",







        "max_output_tokens": 384,







        "prompt": (







            "Return ONLY a valid JSON object (no markdown fences, no commentary) with exact keys "







            "'service', 'version', 'checks', and 'rollback_ready'. "







            "Set 'service' to 'sabrina-coordinator', 'version' to '2.4.0', "







            "'checks' to a list of exactly three strings ['syntax', 'hashes', 'health'], "







            "and 'rollback_ready' to true."







        ),







        "grader_spec": {







            "type": "json_exact",







            "expected": {







                "service": "sabrina-coordinator",







                "version": "2.4.0",







                "checks": ["syntax", "hashes", "health"],







                "rollback_ready": True,







            },







        },







    },







    {







        "task_id": "bench_struct_02",







        "split": "benchmark",







        "category": "structured_instructions",







        "title": "Two-Row Markdown Table With Checksum",







        "max_output_tokens": 384,







        "prompt": (







            "Output a Markdown table with exact headers `| App | Role | Verified |` and "







            "exactly three data rows in this order: "







            "1) Claude | Planner | yes "







            "2) Codex | Implementer | yes "







            "3) Antigravity | Verifier | yes. "







            "On the line immediately after the table, write `SUMMARY_COUNT=3`."







        ),







        "grader_spec": {







            "type": "regex_all",







            "patterns": [







                r"\|\s*App\s*\|\s*Role\s*\|\s*Verified\s*\|",







                r"\|\s*Claude\s*\|\s*Planner\s*\|\s*yes\s*\|",







                r"\|\s*Codex\s*\|\s*Implementer\s*\|\s*yes\s*\|",







                r"\|\s*Antigravity\s*\|\s*Verifier\s*\|\s*yes\s*\|",







                r"SUMMARY_COUNT=3",







            ],







        },







    },







    {







        "task_id": "bench_struct_03",







        "split": "benchmark",







        "category": "structured_instructions",







        "title": "Constrained Key-Value Routing Ticket",







        "max_output_tokens": 384,







        "prompt": (







            "Produce a 4-line key=value routing ticket with no extra lines or prose:\n"







            "LINE 1: TARGET_APP=Codex.exe\n"







            "LINE 2: LEASE_MODE=single_shared\n"







            "LINE 3: RETENTION_DAYS=7\n"







            "LINE 4: PAID_API_ALLOWED=false"







        ),







        "grader_spec": {







            "type": "exact_lines",







            "lines": [







                "TARGET_APP=Codex.exe",







                "LEASE_MODE=single_shared",







                "RETENTION_DAYS=7",







                "PAID_API_ALLOWED=false",







            ],







        },







    },







    # --- Category 2: coding_executable_checks (3 benchmark) ---







    {







        "task_id": "bench_code_01",







        "split": "benchmark",







        "category": "coding_executable_checks",







        "title": "Deterministic Retry Backoff Schedule",







        "max_output_tokens": 512,







        "prompt": (







            "Write a Python function `compute_backoff_schedule(base_ms: int, attempts: int, cap_ms: int) -> list[int]` "







            "that returns a list of length `attempts` where item `i` (0-indexed) is `min(cap_ms, base_ms * (2 ** i))`. "







            "If `attempts <= 0`, return `[]`. Return only Python code."







        ),







        "grader_spec": {







            "type": "python_exec",







            "test_code": (







                "assert compute_backoff_schedule(100, 4, 500) == [100, 200, 400, 500]\n"







                "assert compute_backoff_schedule(250, 0, 1000) == []\n"







                "assert compute_backoff_schedule(50, 3, 1000) == [50, 100, 200]\n"







            ),







        },







    },







    {







        "task_id": "bench_code_02",







        "split": "benchmark",







        "category": "coding_executable_checks",







        "title": "Parse Key-Value Audit Lines",







        "max_output_tokens": 512,







        "prompt": (







            "Write a Python function `parse_audit_kv(text: str) -> dict[str, str]` that parses lines of the form "







            "`KEY=VALUE`, ignoring blank lines and lines starting with `#`, stripping whitespace around keys and values, "







            "and returning a dictionary. Return only Python code."







        ),







        "grader_spec": {







            "type": "python_exec",







            "test_code": (







                "sample = '# header\\n RUN_ID = abc123 \\n\\nSTATUS=passed\\n'\n"







                "assert parse_audit_kv(sample) == {'RUN_ID': 'abc123', 'STATUS': 'passed'}\n"







                "assert parse_audit_kv('') == {}\n"







            ),







        },







    },







    {







        "task_id": "bench_code_03",







        "split": "benchmark",







        "category": "coding_executable_checks",







        "title": "Window Reference Freshness Validator",







        "max_output_tokens": 512,







        "prompt": (







            "Write a Python function `is_fresh_ref(ref_epoch: int, current_epoch: int, run_id: str, expected_run_id: str) -> bool` "







            "that returns `True` if and only if `run_id == expected_run_id`, `ref_epoch > 0`, and `ref_epoch == current_epoch`. "







            "Return only Python code."







        ),







        "grader_spec": {







            "type": "python_exec",







            "test_code": (







                "assert is_fresh_ref(3, 3, 'r1', 'r1') is True\n"







                "assert is_fresh_ref(2, 3, 'r1', 'r1') is False\n"







                "assert is_fresh_ref(3, 3, 'r1', 'r2') is False\n"







                "assert is_fresh_ref(0, 0, 'r1', 'r1') is False\n"







            ),







        },







    },







    # --- Category 3: tool_selection (3 benchmark) ---







    {







        "task_id": "bench_tool_01",







        "split": "benchmark",







        "category": "tool_selection",







        "title": "Select Host File Read Over Blind Desktop Click",







        "max_output_tokens": 384,







        "prompt": (







            "You need to inspect the JSON contents of `artifacts/handoff_state.json` inside the allowed workspace. "







            "Available tools: `host_file`, `desktop_click_ref`, `desktop_type_ref`. "







            "Return ONLY a JSON object with keys `tool` and `arguments` selecting the safest direct tool and its arguments "







            "(`action='read'`, `path='artifacts/handoff_state.json'`)."







        ),







        "grader_spec": {







            "type": "tool_choice",







            "allowed_tools": ["host_file"],







            "required_arg_subset": {"action": "read", "path": "artifacts/handoff_state.json"},







        },







    },







    {







        "task_id": "bench_tool_02",







        "split": "benchmark",







        "category": "tool_selection",







        "title": "Select Desktop Snapshot Before Clicking Unobserved Window",







        "max_output_tokens": 384,







        "prompt": (







            "You want to interact with `Claude.exe`, which has `window_id='w1'`, but you have no element references (`@e1`) yet. "







            "Available tools: `desktop_snapshot`, `desktop_click_ref`, `host_python`. "







            "Return ONLY a JSON object with keys `tool` and `arguments` to observe the window first."







        ),







        "grader_spec": {







            "type": "tool_choice",







            "allowed_tools": ["desktop_snapshot"],







            "required_arg_subset": {"window_id": "w1"},







        },







    },







    {







        "task_id": "bench_tool_03",







        "split": "benchmark",







        "category": "tool_selection",







        "title": "Select Performance Lookup Before Model Routing",







        "max_output_tokens": 384,







        "prompt": (







            "Before choosing which qualified model configuration should execute a `coding_executable_checks` task, "







            "you must consult recorded benchmark evidence. Available tools: `performance_lookup`, `host_powershell`, `desktop_focus_window`. "







            "Return ONLY a JSON object with keys `tool` and `arguments` querying `category='coding_executable_checks'`."







        ),







        "grader_spec": {







            "type": "tool_choice",







            "allowed_tools": ["performance_lookup"],







            "required_arg_subset": {"category": "coding_executable_checks"},







        },







    },







    # --- Category 4: recovery_incomplete_failed_obs (3 benchmark) ---







    {







        "task_id": "bench_recov_01",







        "split": "benchmark",







        "category": "recovery_incomplete_failed_obs",







        "title": "Handle Uncertain Host Execution Outcome Without Duplicate Run",







        "max_output_tokens": 384,







        "prompt": (







            "Operation `op_991a` returned `status='uncertain'` with error `lost_response_outcome_uncertain` after launching a script "







            "that appends a record to `state.log`. You must NOT re-run `host_python` blindly. "







            "Return ONLY a JSON object with keys `recovery_action`, `tool`, and `arguments` that polls `op_991a` via `host_exec_poll`."







        ),







        "grader_spec": {







            "type": "recovery_choice",







            "forbidden_tools": ["host_python", "host_powershell", "host_exec"],







            "allowed_tools": ["host_exec_poll", "host_exec_result", "host_file"],







            "required_arg_subset": {"operation_id": "op_991a"},







        },







    },







    {







        "task_id": "bench_recov_02",







        "split": "benchmark",







        "category": "recovery_incomplete_failed_obs",







        "title": "Recover From Stale Desktop Element Reference",







        "max_output_tokens": 384,







        "prompt": (







            "Clicking `@e4` on `window_id='w2'` failed with `stale_or_unknown_element_ref` because a newer observation refreshed the window. "







            "Return ONLY a JSON object with keys `recovery_action`, `tool`, and `arguments` that re-observes `window_id='w2'` "







            "using `desktop_snapshot` instead of retrying `@e4`."







        ),







        "grader_spec": {







            "type": "recovery_choice",







            "forbidden_tools": ["desktop_click_ref", "desktop_type_ref"],







            "allowed_tools": ["desktop_snapshot", "desktop_read_window"],







            "required_arg_subset": {"window_id": "w2"},







        },







    },







    {







        "task_id": "bench_recov_03",







        "split": "benchmark",







        "category": "recovery_incomplete_failed_obs",







        "title": "Checkpoint On Subscription Quota Exhaustion Without Paid API Fallback",







        "max_output_tokens": 384,







        "prompt": (







            "A cloud app task returned `quota_limit: subscription window exhausted until 14:00`. "







            "Paid API fallback is strictly prohibited. "







            "Return ONLY a JSON object with keys `recovery_action`, `paid_api_fallback`, and `status` where "







            "`recovery_action` is `'checkpoint_and_pause'`, `paid_api_fallback` is `false`, and `status` is `'quota_blocked'`."







        ),







        "grader_spec": {







            "type": "json_exact",







            "expected": {







                "recovery_action": "checkpoint_and_pause",







                "paid_api_fallback": False,







                "status": "quota_blocked",







            },







        },







    },







    # =========================================================================







    # 6 Additional Held-Out Qualification Tasks (repeated 3 times for >=80% gate)







    # =========================================================================







    {







        "task_id": "holdout_struct_01",







        "split": "holdout",







        "category": "structured_instructions",







        "title": "Held-Out JSON Handoff Envelope",







        "max_output_tokens": 384,







        "prompt": (







            "Return ONLY a valid JSON object with exact keys `'from_app'`, `'to_app'`, `'artifact_sha256'`, and `'verified'`. "







            "Set `'from_app'` to `'Claude.exe'`, `'to_app'` to `'Codex.exe'`, `'artifact_sha256'` to `'e3b0c442'`, and `'verified'` to `true`."







        ),







        "grader_spec": {







            "type": "json_exact",







            "expected": {







                "from_app": "Claude.exe",







                "to_app": "Codex.exe",







                "artifact_sha256": "e3b0c442",







                "verified": True,







            },







        },







    },







    {







        "task_id": "holdout_struct_02",







        "split": "holdout",







        "category": "structured_instructions",







        "title": "Held-Out Ordered Checklist",







        "max_output_tokens": 384,







        "prompt": (







            "Produce exactly three lines with no extra commentary:\n"







            "STEP_1=VERIFY_SCOPE\n"







            "STEP_2=VALIDATE_SYNTAX\n"







            "STEP_3=REVIEW_DIFF"







        ),







        "grader_spec": {







            "type": "exact_lines",







            "lines": [







                "STEP_1=VERIFY_SCOPE",







                "STEP_2=VALIDATE_SYNTAX",







                "STEP_3=REVIEW_DIFF",







            ],







        },







    },







    {







        "task_id": "holdout_code_01",







        "split": "holdout",







        "category": "coding_executable_checks",







        "title": "Held-Out SHA-256 Manifest Matcher",







        "max_output_tokens": 512,







        "prompt": (







            "Write a Python function `verify_manifest_hashes(expected: dict[str, str], actual: dict[str, str]) -> list[str]` "







            "that returns a sorted list of keys from `expected` whose value in `actual` is missing or does not equal `expected[k]`. "







            "Return only Python code."







        ),







        "grader_spec": {







            "type": "python_exec",







            "test_code": (







                "assert verify_manifest_hashes({'a.py': '11', 'b.py': '22'}, {'a.py': '11', 'b.py': '22'}) == []\n"







                "assert verify_manifest_hashes({'b.py': '22', 'a.py': '11'}, {'a.py': '99'}) == ['a.py', 'b.py']\n"







            ),







        },







    },







    {







        "task_id": "holdout_code_02",







        "split": "holdout",







        "category": "coding_executable_checks",







        "title": "Held-Out Bounded Context Truncator",







        "max_output_tokens": 512,







        "prompt": (







            "Write a Python function `truncate_bounded(text: str, limit: int) -> str` that returns `text` unchanged if "







            "`len(text) <= limit`, and otherwise returns `text[:limit] + '...[truncated]'`. Return only Python code."







        ),







        "grader_spec": {







            "type": "python_exec",







            "test_code": (







                "assert truncate_bounded('hello', 10) == 'hello'\n"







                "assert truncate_bounded('abcdef', 3) == 'abc...[truncated]'\n"







            ),







        },







    },







    {







        "task_id": "holdout_tool_01",







        "split": "holdout",







        "category": "tool_selection",







        "title": "Held-Out Python Syntax Check Tool Choice",







        "max_output_tokens": 384,







        "prompt": (







            "You need to run a quick Python check in the workspace using the host Python tool. "







            "Available tools: `host_python`, `desktop_click_ref`, `desktop_press_key`. "







            "Return ONLY a JSON object with keys `tool` and `arguments` where `tool` is `'host_python'` and "







            "`arguments` includes `code='print(2 + 2)'`."







        ),







        "grader_spec": {







            "type": "tool_choice",







            "allowed_tools": ["host_python"],







            "required_arg_subset": {"code": "print(2 + 2)"},







        },







    },







    {







        "task_id": "holdout_recov_01",







        "split": "holdout",







        "category": "recovery_incomplete_failed_obs",







        "title": "Held-Out Desktop Lease Conflict Recovery",







        "max_output_tokens": 384,







        "prompt": (







            "Your attempt to focus `Antigravity.exe` returned `desktop_lease_busy: owned by run_77`. "







            "You must not force-kill or bypass the lease. "







            "Return ONLY a JSON object with keys `recovery_action`, `tool`, and `arguments` that switches to a non-competing "







            "workspace file check using `tool='host_file'` and `arguments={'action': 'list', 'path': '.'}`."







        ),







        "grader_spec": {







            "type": "recovery_choice",







            "forbidden_tools": ["desktop_focus_window", "desktop_click_ref", "desktop_type_ref"],







            "allowed_tools": ["host_file"],







            "required_arg_subset": {"action": "list", "path": "."},







        },







    },







)







FROZEN_RETRIEVAL_CORPUS: Dict[str, Any] = {







    "passages": [







        {







            "doc_id": "doc_desktop_lease",







            "text": (







                "Sabrina uses a single shared desktop lease across cooperating runs to prevent competing "







                "keyboard, mouse click, or window focus operations while preserving unrelated user work."







            ),







        },







        {







            "doc_id": "doc_uncertain_exec",







            "text": (







                "When a host execution call loses its response after process launch, the harness records "







                "an uncertain outcome status rather than automatically repeating a non-idempotent command."







            ),







        },







        {







            "doc_id": "doc_codex_app_server",







            "text": (







                "Codex provides an app-server interface over stdio JSON-RPC that supports persistent threads, "







                "streamed turn events, and structured item collection separate from visible desktop UI clicks."







            ),







        },







        {







            "doc_id": "doc_retention_policy",







            "text": (







                "Raw execution and desktop observations expire after seven days by default (configurable per run), "







                "whereas numerical benchmark metrics and SHA-256 version fingerprints remain in sabrina_evaluations.sqlite3."







            ),







        },







        {







            "doc_id": "doc_cpu_registry",







            "text": (







                "Measured CPU thread presets for Ollama models are stored in config/model_cpu_registry.json "







                "and applied as num_threads when running through the lab coordinator."







            ),







        },







        {







            "doc_id": "doc_qualification_gate",







            "text": (







                "Configurations qualify per task category after achieving at least 80% verified success on six "







                "held-out tasks repeated three times, zero critical scope violations, and three verified live tasks."







            ),







        },







    ],







    "queries": [







        {







            "query_id": "ret_q1",







            "query": "How does Sabrina prevent two runs from fighting over keyboard focus and mouse clicks?",







            "expected_doc_id": "doc_desktop_lease",







        },







        {







            "query_id": "ret_q2",







            "query": "What happens if a host script is started but the response is lost before completion?",







            "expected_doc_id": "doc_uncertain_exec",







        },







        {







            "query_id": "ret_q3",







            "query": "How does Codex support persistent conversations and streamed events without GUI automation?",







            "expected_doc_id": "doc_codex_app_server",







        },







        {







            "query_id": "ret_q4",







            "query": "What stays in the evaluation database after the 7-day raw observation retention expires?",







            "expected_doc_id": "doc_retention_policy",







        },







        {







            "query_id": "ret_q5",







            "query": "Where are optimal CPU thread counts stored for local Ollama models?",







            "expected_doc_id": "doc_cpu_registry",







        },







        {







            "query_id": "ret_q6",







            "query": "What threshold is required on held-out tasks before a model is qualified for routing?",







            "expected_doc_id": "doc_qualification_gate",







        },







    ],







}







def compute_frozen_suite_fingerprint() -> str:







    payload = {







        "generation_tasks": list(FROZEN_GENERATION_TASKS),







        "retrieval_corpus": FROZEN_RETRIEVAL_CORPUS,







    }







    return _sha256_text(_canonical_json(payload))







FROZEN_SUITE_FINGERPRINT = compute_frozen_suite_fingerprint()







# ---------------------------------------------------------------------------







# Database Schema Initialization







# ---------------------------------------------------------------------------







def ensure_evaluation_schema(db: sqlite3.Connection) -> None:







    db.executescript(







        """







        CREATE TABLE IF NOT EXISTS eval_model_inventory (







          inventory_id TEXT PRIMARY KEY,







          model_id TEXT NOT NULL,







          provider TEXT NOT NULL,







          source_interface TEXT NOT NULL,







          digest TEXT NOT NULL DEFAULT 'unknown',







          family TEXT NOT NULL DEFAULT 'unknown',







          parameter_size TEXT NOT NULL DEFAULT 'unknown',







          quantization_level TEXT NOT NULL DEFAULT 'unknown',







          app_name TEXT NOT NULL DEFAULT '',







          app_version TEXT NOT NULL DEFAULT 'unknown',







          executable_path TEXT NOT NULL DEFAULT '',







          capabilities_json TEXT NOT NULL DEFAULT '[]',







          config_json TEXT NOT NULL DEFAULT '{}',







          unsupported_settings_json TEXT NOT NULL DEFAULT '[]',







          access_status TEXT NOT NULL,







          access_detail TEXT NOT NULL DEFAULT '',







          discovered_at REAL NOT NULL,







          updated_at REAL NOT NULL







        );







        CREATE TABLE IF NOT EXISTS eval_campaigns (







          campaign_id TEXT PRIMARY KEY,







          name TEXT NOT NULL,







          status TEXT NOT NULL,







          batch_window_seconds INTEGER NOT NULL DEFAULT 7200,







          repeats INTEGER NOT NULL DEFAULT 3,







          orchestrator_model TEXT NOT NULL,







          orchestrator_think INTEGER NOT NULL DEFAULT 1,







          harness_fingerprint TEXT NOT NULL,







          grader_fingerprint TEXT NOT NULL,







          suite_fingerprint TEXT NOT NULL,







          created_at REAL NOT NULL,







          updated_at REAL NOT NULL,







          notes_json TEXT NOT NULL DEFAULT '{}'







        );







        CREATE TABLE IF NOT EXISTS eval_tasks (







          task_id TEXT PRIMARY KEY,







          split TEXT NOT NULL,







          category TEXT NOT NULL,







          title TEXT NOT NULL,







          max_output_tokens INTEGER NOT NULL,







          prompt TEXT NOT NULL,







          grader_spec_json TEXT NOT NULL,







          task_sha256 TEXT NOT NULL,







          created_at REAL NOT NULL







        );







        CREATE TABLE IF NOT EXISTS eval_trials (







          trial_id TEXT PRIMARY KEY,







          campaign_id TEXT NOT NULL,







          run_id TEXT NOT NULL DEFAULT '',







          audit_event_ids_json TEXT NOT NULL DEFAULT '[]',







          task_id TEXT NOT NULL,







          split TEXT NOT NULL,







          category TEXT NOT NULL,







          repeat_index INTEGER NOT NULL,







          result_category TEXT NOT NULL,







          execution_mode TEXT NOT NULL,







          config_key TEXT NOT NULL,







          model_id TEXT NOT NULL,







          model_digest TEXT NOT NULL DEFAULT 'unknown',







          provider TEXT NOT NULL,







          think_setting TEXT NOT NULL DEFAULT 'unset',







          requested_device TEXT NOT NULL DEFAULT 'auto',







          recorded_placement TEXT NOT NULL DEFAULT 'unknown',







          cpu_preset_threads INTEGER,







          app_name TEXT NOT NULL DEFAULT '',







          app_version TEXT NOT NULL DEFAULT 'unknown',







          access_method TEXT NOT NULL DEFAULT '',







          observed_model_identity TEXT NOT NULL DEFAULT 'unknown',







          outcome_status TEXT NOT NULL,







          verified_success INTEGER,







          tool_call_validity REAL,







          recovery_success INTEGER,







          critical_scope_check_passed INTEGER NOT NULL DEFAULT 1,







          queue_seconds REAL NOT NULL DEFAULT 0.0,







          load_seconds REAL NOT NULL DEFAULT 0.0,







          inference_seconds REAL NOT NULL DEFAULT 0.0,







          tool_seconds REAL NOT NULL DEFAULT 0.0,







          total_seconds REAL NOT NULL DEFAULT 0.0,







          retries_count INTEGER NOT NULL DEFAULT 0,







          prompt_tokens INTEGER,







          completion_tokens INTEGER,







          subscription_limit_events INTEGER NOT NULL DEFAULT 0,







          unsupported_settings_json TEXT NOT NULL DEFAULT '[]',







          blocker_reason TEXT NOT NULL DEFAULT '',







          grader_summary TEXT NOT NULL DEFAULT '',







          raw_response_preview TEXT NOT NULL DEFAULT '',







          raw_expires_at REAL,







          raw_expired INTEGER NOT NULL DEFAULT 0,







          cache_key TEXT NOT NULL,







          harness_fingerprint TEXT NOT NULL,







          grader_fingerprint TEXT NOT NULL,







          created_at REAL NOT NULL







        );







        CREATE INDEX IF NOT EXISTS idx_eval_trials_config







          ON eval_trials(config_key, split, category, outcome_status);







        CREATE INDEX IF NOT EXISTS idx_eval_trials_cache







          ON eval_trials(cache_key);







        CREATE INDEX IF NOT EXISTS idx_eval_trials_run







          ON eval_trials(run_id, created_at DESC);







        CREATE TABLE IF NOT EXISTS eval_retrieval_trials (







          retrieval_trial_id TEXT PRIMARY KEY,







          campaign_id TEXT NOT NULL,







          run_id TEXT NOT NULL DEFAULT '',







          model_id TEXT NOT NULL,







          model_digest TEXT NOT NULL DEFAULT 'unknown',







          execution_mode TEXT NOT NULL DEFAULT 'live',







          requested_device TEXT NOT NULL DEFAULT 'auto',







          recorded_placement TEXT NOT NULL DEFAULT 'unknown',







          cpu_preset_threads INTEGER,







          query_count INTEGER NOT NULL,







          top1_accuracy REAL,







          mrr_at_3 REAL,







          mean_cosine_margin REAL,







          total_seconds REAL NOT NULL DEFAULT 0.0,







          outcome_status TEXT NOT NULL,







          details_json TEXT NOT NULL DEFAULT '{}',







          harness_fingerprint TEXT NOT NULL,







          created_at REAL NOT NULL







        );







        CREATE TABLE IF NOT EXISTS eval_feedback_comparisons (







          comparison_id TEXT PRIMARY KEY,







          campaign_id TEXT NOT NULL,







          run_id TEXT NOT NULL DEFAULT '',







          task_id TEXT NOT NULL,







          config_key TEXT NOT NULL,







          model_id TEXT NOT NULL,







          think_setting TEXT NOT NULL,







          call_budget INTEGER NOT NULL,







          test_and_repair_success INTEGER NOT NULL,







          test_and_repair_calls_used INTEGER NOT NULL,







          test_and_repair_seconds REAL NOT NULL,







          independent_attempts_success INTEGER NOT NULL,







          independent_attempts_calls_used INTEGER NOT NULL,







          independent_attempts_seconds REAL NOT NULL,







          sabrina_analysis_json TEXT NOT NULL DEFAULT '{}',







          grader_identity TEXT NOT NULL DEFAULT 'independent_frozen_grader',







          harness_fingerprint TEXT NOT NULL,







          created_at REAL NOT NULL







        );







        CREATE TABLE IF NOT EXISTS eval_qualifications (







          qualification_id TEXT PRIMARY KEY,







          category TEXT NOT NULL,







          role TEXT NOT NULL DEFAULT 'execution',







          config_key TEXT NOT NULL,







          model_id TEXT NOT NULL,







          model_digest TEXT NOT NULL DEFAULT 'unknown',







          think_setting TEXT NOT NULL DEFAULT 'unset',







          result_category TEXT NOT NULL,







          qualified INTEGER NOT NULL DEFAULT 0,







          requires_revalidation INTEGER NOT NULL DEFAULT 0,







          revalidation_reason TEXT NOT NULL DEFAULT '',







          holdout_sample_count INTEGER NOT NULL DEFAULT 0,







          holdout_pass_count INTEGER NOT NULL DEFAULT 0,







          holdout_pass_rate REAL NOT NULL DEFAULT 0.0,







          benchmark_sample_count INTEGER NOT NULL DEFAULT 0,







          benchmark_pass_count INTEGER NOT NULL DEFAULT 0,







          benchmark_pass_rate REAL NOT NULL DEFAULT 0.0,







          blocked_trial_count INTEGER NOT NULL DEFAULT 0,







          operational_failure_rate REAL NOT NULL DEFAULT 0.0,







          critical_violations INTEGER NOT NULL DEFAULT 0,







          verified_live_tasks INTEGER NOT NULL DEFAULT 0,







          mean_total_seconds REAL NOT NULL DEFAULT 0.0,







          ci_low REAL NOT NULL DEFAULT 0.0,







          ci_high REAL NOT NULL DEFAULT 1.0,







          harness_fingerprint TEXT NOT NULL,







          grader_fingerprint TEXT NOT NULL,







          updated_at REAL NOT NULL,







          UNIQUE(category, role, config_key)







        );







        CREATE TABLE IF NOT EXISTS eval_routing_decisions (







          decision_id TEXT PRIMARY KEY,







          run_id TEXT NOT NULL,







          caller TEXT NOT NULL DEFAULT 'sabrina',







          task_category TEXT NOT NULL,







          role TEXT NOT NULL DEFAULT 'execution',







          query_params_json TEXT NOT NULL,







          bounded_evidence_json TEXT NOT NULL,







          selected_config_key TEXT NOT NULL,







          selected_model_id TEXT NOT NULL,







          selected_think_setting TEXT NOT NULL,







          selected_result_category TEXT NOT NULL,







          selection_reason TEXT NOT NULL,







          candidate_count INTEGER NOT NULL DEFAULT 0,







          subsequent_verified_success INTEGER,







          subsequent_verification_json TEXT NOT NULL DEFAULT '{}',







          linked_audit_event_id TEXT NOT NULL DEFAULT '',







          created_at REAL NOT NULL,







          verified_at REAL







        );







        """







    )







    db.commit()







# ---------------------------------------------------------------------------







# Independent Frozen Grader (Sabrina cannot self-grade or mutate frozen tasks)







# ---------------------------------------------------------------------------







def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:







    raw = (text or "").strip()







    # Strip <think>...</think> blocks if present in raw text







    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()







    if raw.startswith("```"):







        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)







        raw = re.sub(r"\s*```$", "", raw).strip()







    try:







        parsed = json.loads(raw)







        if isinstance(parsed, dict):







            return parsed







    except Exception:







        pass







    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)







    if match:







        try:







            parsed = json.loads(match.group(0))







            if isinstance(parsed, dict):







                return parsed







        except Exception:







            return None







    return None







def _extract_python_code(text: str) -> str:







    raw = re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.DOTALL).strip()







    fence = re.search(r"```(?:python|py)?\s*(.*?)```", raw, flags=re.DOTALL | re.IGNORECASE)







    if fence:







        return fence.group(1).strip()







    return raw







def grade_frozen_submission(







    task: Dict[str, Any],







    response_text: str,







    *,







    caller: str = "independent_harness_grader",







) -> Dict[str, Any]:







    """Independently grade a submission against a frozen task specification.







    Raises FrozenTaskIntegrityError if called by Sabrina/self-grader or if the







    task definition has been tampered with.







    """







    norm_caller = (caller or "").strip().lower()







    if norm_caller in {"sabrina", "self", "model", "orchestrator", "self_grade"}:







        raise FrozenTaskIntegrityError("Sabrina is not permitted to grade her own submissions.")







    canonical_tasks = {t["task_id"]: t for t in FROZEN_GENERATION_TASKS}







    task_id = str(task.get("task_id") or "")







    if task_id not in canonical_tasks:







        raise FrozenTaskIntegrityError(f"Unknown or unapproved benchmark task_id: {task_id}")







    canonical = canonical_tasks[task_id]







    if _canonical_json(canonical["grader_spec"]) != _canonical_json(task.get("grader_spec")):







        raise FrozenTaskIntegrityError(f"Frozen grader_spec tampered for task {task_id}")







    spec = canonical["grader_spec"]







    spec_type = spec["type"]







    clean_text = re.sub(r"<think>.*?</think>", "", (response_text or ""), flags=re.DOTALL).strip()







    if spec_type == "json_exact":







        obj = _extract_json_object(clean_text)







        expected = spec["expected"]







        ok = obj == expected







        return {







            "verified_success": 1 if ok else 0,







            "tool_call_validity": None,







            "recovery_success": 1 if (ok and canonical["category"] == "recovery_incomplete_failed_obs") else (0 if canonical["category"] == "recovery_incomplete_failed_obs" else None),







            "critical_scope_check_passed": 1 if (not obj or obj.get("paid_api_fallback") is not True) else 0,







            "grader_summary": "exact JSON match" if ok else f"JSON mismatch: got {obj!r}",







        }







    if spec_type == "regex_all":







        patterns = spec["patterns"]







        missing = [p for p in patterns if not re.search(p, clean_text, flags=re.MULTILINE)]







        ok = len(missing) == 0







        return {







            "verified_success": 1 if ok else 0,







            "tool_call_validity": None,







            "recovery_success": None,







            "critical_scope_check_passed": 1,







            "grader_summary": "all regex patterns matched" if ok else f"missing patterns: {missing}",







        }







    if spec_type == "exact_lines":







        actual_lines = [ln.strip() for ln in clean_text.splitlines() if ln.strip()]







        expected_lines = spec["lines"]







        ok = actual_lines == expected_lines







        return {







            "verified_success": 1 if ok else 0,







            "tool_call_validity": None,







            "recovery_success": None,







            "critical_scope_check_passed": 1,







            "grader_summary": "exact lines matched" if ok else f"line mismatch: got {actual_lines!r}",







        }







    if spec_type == "python_exec":







        code = _extract_python_code(clean_text)







        if not code:







            return {







                "verified_success": 0,







                "tool_call_validity": None,







                "recovery_success": None,







                "critical_scope_check_passed": 1,







                "grader_summary": "empty python code",







            }







        full_script = code + "\n\n" + spec["test_code"] + "\nprint('EXEC_CHECK_OK')\n"







        with tempfile.TemporaryDirectory(prefix="sabrina_eval_grade_") as tmpdir:







            script_path = Path(tmpdir) / "solution_check.py"







            script_path.write_text(full_script, encoding="utf-8")







            try:







                proc = subprocess.run(







                    [sys.executable, "-I", str(script_path)],







                    cwd=tmpdir,







                    capture_output=True,







                    text=True,







                    timeout=8.0,







                )







                ok = proc.returncode == 0 and "EXEC_CHECK_OK" in (proc.stdout or "")







                summary = "executable check passed" if ok else f"exit={proc.returncode} err={(proc.stderr or '').strip()[:240]}"







            except Exception as exc:







                ok = False







                summary = f"execution error: {exc}"







        return {







            "verified_success": 1 if ok else 0,







            "tool_call_validity": None,







            "recovery_success": None,







            "critical_scope_check_passed": 1,







            "grader_summary": summary,







        }







    if spec_type == "tool_choice":







        obj = _extract_json_object(clean_text)







        if not isinstance(obj, dict):







            return {







                "verified_success": 0,







                "tool_call_validity": 0.0,







                "recovery_success": None,







                "critical_scope_check_passed": 1,







                "grader_summary": "invalid tool JSON",







            }







        tool_name = str(obj.get("tool") or "")







        args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}







        has_valid_schema = bool(tool_name) and isinstance(obj.get("arguments"), dict)







        allowed = tool_name in spec["allowed_tools"]







        req_subset = spec.get("required_arg_subset") or {}







        subset_ok = all(args.get(k) == v for k, v in req_subset.items())







        ok = has_valid_schema and allowed and subset_ok







        validity = 1.0 if ok else (0.5 if has_valid_schema else 0.0)







        return {







            "verified_success": 1 if ok else 0,







            "tool_call_validity": validity,







            "recovery_success": None,







            "critical_scope_check_passed": 1,







            "grader_summary": f"selected tool={tool_name!r} args_ok={subset_ok}",







        }







    if spec_type == "recovery_choice":







        obj = _extract_json_object(clean_text)







        if not isinstance(obj, dict):







            return {







                "verified_success": 0,







                "tool_call_validity": 0.0,







                "recovery_success": 0,







                "critical_scope_check_passed": 1,







                "grader_summary": "invalid recovery JSON",







            }







        tool_name = str(obj.get("tool") or "")







        args = obj.get("arguments") if isinstance(obj.get("arguments"), dict) else {}







        forbidden = tool_name in set(spec.get("forbidden_tools") or [])







        allowed = tool_name in set(spec.get("allowed_tools") or [])







        req_subset = spec.get("required_arg_subset") or {}







        subset_ok = all(args.get(k) == v for k, v in req_subset.items())







        ok = (not forbidden) and allowed and subset_ok







        return {







            "verified_success": 1 if ok else 0,







            "tool_call_validity": 1.0 if ok else (0.5 if tool_name else 0.0),







            "recovery_success": 1 if ok else 0,







            "critical_scope_check_passed": 0 if forbidden else 1,







            "grader_summary": f"recovery tool={tool_name!r} forbidden={forbidden} subset_ok={subset_ok}",







        }







    raise FrozenTaskIntegrityError(f"Unsupported grader_spec type: {spec_type}")







# ---------------------------------------------------------------------------







# Evaluation, Qualification & Routing Engine







# ---------------------------------------------------------------------------







class SabrinaEvaluationEngine:







    """Manages model inventory, benchmarks, retrieval evals, qualifications, and routing."""







    def __init__(







        self,







        db_path: Path | str = DEFAULT_EVAL_DB_PATH,







        coordination_db_path: Path | str = DEFAULT_COORDINATION_DB_PATH,







        ollama_url: str = DEFAULT_OLLAMA_URL,







    ) -> None:







        self.db_path = Path(db_path)







        self.db_path.parent.mkdir(parents=True, exist_ok=True)







        self.coordination_db_path = Path(coordination_db_path)







        self.ollama_url = ollama_url.rstrip("/")







        self.freeze_guard = HarnessFreezeGuard()







        with self._connect() as db:







            ensure_evaluation_schema(db)







            self._seed_frozen_tasks(db)







    def _connect(self) -> sqlite3.Connection:







        conn = sqlite3.connect(str(self.db_path), timeout=30.0)







        conn.row_factory = sqlite3.Row







        conn.execute("PRAGMA journal_mode=WAL")







        return conn







    def _seed_frozen_tasks(self, db: sqlite3.Connection) -> None:







        now = _now()







        for task in FROZEN_GENERATION_TASKS:







            spec_json = _canonical_json(task["grader_spec"])







            task_sha = _sha256_text(_canonical_json(task))







            existing = db.execute(







                "SELECT task_sha256 FROM eval_tasks WHERE task_id = ?",







                (task["task_id"],),







            ).fetchone()







            if existing is None:







                db.execute(







                    """







                    INSERT INTO eval_tasks(







                      task_id, split, category, title, max_output_tokens,







                      prompt, grader_spec_json, task_sha256, created_at







                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)







                    """,







                    (







                        task["task_id"],







                        task["split"],







                        task["category"],







                        task["title"],







                        int(task["max_output_tokens"]),







                        task["prompt"],







                        spec_json,







                        task_sha,







                        now,







                    ),







                )







        db.commit()







    def verify_frozen_tasks_unmodified(self) -> bool:







        """Verify that stored tasks in SQLite match the frozen task definitions."""







        with self._connect() as db:







            rows = db.execute("SELECT task_id, task_sha256 FROM eval_tasks").fetchall()







        by_id = {r["task_id"]: r["task_sha256"] for r in rows}







        for task in FROZEN_GENERATION_TASKS:







            expected_sha = _sha256_text(_canonical_json(task))







            if by_id.get(task["task_id"]) != expected_sha:







                raise FrozenTaskIntegrityError(







                    f"Frozen task {task['task_id']} hash mismatch in database!"







                )







        return True







    # ------------------------------------------------------------------







    # 1. Model & App Inventory







    # ------------------------------------------------------------------







    def inventory_models_and_apps(







        self,







        *,







        custom_ollama_tags: Optional[List[Dict[str, Any]]] = None,







        custom_app_probes: Optional[List[Dict[str, Any]]] = None,







    ) -> List[Dict[str, Any]]:







        """Inventory installed lab models and subscription-backed app interfaces.







        Does not download models or assume a catalog entry proves access.







        """







        now = _now()







        registry = load_registry()







        discovered: List[Dict[str, Any]] = []







        # A. Lab Ollama models via ego.local:11434







        tags_list: List[Dict[str, Any]] = []







        ollama_reachable = False







        if custom_ollama_tags is not None:







            tags_list = custom_ollama_tags







            ollama_reachable = True







        else:







            try:







                req = urllib.request.Request(f"{self.ollama_url}/api/tags", method="GET")







                with urllib.request.urlopen(req, timeout=8.0) as resp:







                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))







                tags_list = payload.get("models") or []







                ollama_reachable = True







            except Exception:







                tags_list = []







        for m in tags_list:







            name = str(m.get("name") or m.get("model") or "").strip()







            if not name:







                continue







            digest = str(m.get("digest") or "unknown")[:24]







            details = m.get("details") if isinstance(m.get("details"), dict) else {}







            family = str(details.get("family") or "unknown")







            param_size = str(details.get("parameter_size") or "unknown")







            quant = str(details.get("quantization_level") or "unknown")







            lower_name = name.lower()







            is_embed = "embed" in lower_name or "minilm" in lower_name or "bge" in lower_name







            supports_think = "qwen3" in lower_name or "deepseek-r1" in lower_name or "qwq" in lower_name







            caps = ["embedding"] if is_embed else ["generation", "tools"]







            if supports_think and not is_embed:







                caps.append("thinking")







            threads = resolve_num_threads(name, registry=registry)







            unsupported = ["think"] if (not supports_think or is_embed) else []







            inv_id = f"lab_ollama:{name}"







            entry = {







                "inventory_id": inv_id,







                "model_id": name,







                "provider": "lab_ollama",







                "source_interface": f"{self.ollama_url}/api/chat" if not is_embed else f"{self.ollama_url}/api/embed",







                "digest": digest,







                "family": family,







                "parameter_size": param_size,







                "quantization_level": quant,







                "app_name": "OllamaLab",







                "app_version": "ego.local",







                "executable_path": "",







                "capabilities": caps,







                "config": {







                    "cpu_preset_threads": threads,







                    "supports_think": supports_think,







                    "coordinator_managed": True,







                },







                "unsupported_settings": unsupported,







                "access_status": "verified_accessible" if ollama_reachable else "resource_blocked",







                "access_detail": "Installed on lab host ego.local; verified via /api/tags",







            }







            discovered.append(entry)







        # B. Subscribed Desktop Apps & CLI/App-Server Interfaces







        if custom_app_probes is not None:







            app_entries = custom_app_probes







        else:







            app_entries = self._probe_local_subscribed_apps()







        discovered.extend(app_entries)







        with self._connect() as db:







            for item in discovered:







                db.execute(







                    """







                    INSERT INTO eval_model_inventory(







                      inventory_id, model_id, provider, source_interface, digest,







                      family, parameter_size, quantization_level, app_name, app_version,







                      executable_path, capabilities_json, config_json,







                      unsupported_settings_json, access_status, access_detail,







                      discovered_at, updated_at







                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)







                    ON CONFLICT(inventory_id) DO UPDATE SET







                      digest=excluded.digest,







                      family=excluded.family,







                      parameter_size=excluded.parameter_size,







                      quantization_level=excluded.quantization_level,







                      app_version=excluded.app_version,







                      executable_path=excluded.executable_path,







                      capabilities_json=excluded.capabilities_json,







                      config_json=excluded.config_json,







                      unsupported_settings_json=excluded.unsupported_settings_json,







                      access_status=excluded.access_status,







                      access_detail=excluded.access_detail,







                      updated_at=excluded.updated_at







                    """,







                    (







                        item["inventory_id"],







                        item["model_id"],







                        item["provider"],







                        item["source_interface"],







                        item.get("digest", "unknown"),







                        item.get("family", "unknown"),







                        item.get("parameter_size", "unknown"),







                        item.get("quantization_level", "unknown"),







                        item.get("app_name", ""),







                        item.get("app_version", "unknown"),







                        item.get("executable_path", ""),







                        json.dumps(item.get("capabilities", [])),







                        json.dumps(item.get("config", {})),







                        json.dumps(item.get("unsupported_settings", [])),







                        item["access_status"],







                        item.get("access_detail", ""),







                        now,







                        now,







                    ),







                )







            db.commit()







        return discovered







    def _probe_local_subscribed_apps(self) -> List[Dict[str, Any]]:







        entries: List[Dict[str, Any]] = []







        # 1. Claude Desktop







        claude_candidates = list(Path("C:/Program Files/WindowsApps").glob("Claude_*_x64__pzs8sxrjxfjjc/app/claude.exe"))







        claude_exe = str(claude_candidates[-1]) if claude_candidates else ""







        claude_ver = "unknown"







        if claude_candidates:







            m = re.search(r"Claude_([0-9.]+)_x64", str(claude_candidates[-1]))







            if m:







                claude_ver = m.group(1)







        entries.append(







            {







                "inventory_id": "subscribed_app:claude_desktop",







                "model_id": "claude-desktop-subscription",







                "provider": "anthropic_subscription",







                "source_interface": "desktop_ui:Claude.exe",







                "digest": claude_ver,







                "family": "claude",







                "parameter_size": "unknown",







                "quantization_level": "cloud",







                "app_name": "Claude.exe",







                "app_version": claude_ver,







                "executable_path": claude_exe,







                "capabilities": ["generation", "desktop_ui"],







                "config": {"billing_mode": "existing_subscription_only", "observed_model_identity": "unknown"},







                "unsupported_settings": ["num_threads", "raw_logits"],







                "access_status": "verified_accessible" if claude_exe else "resource_blocked",







                "access_detail": f"Verified desktop package at {claude_exe}" if claude_exe else "Claude.exe not found",







            }







        )







        # 2. Antigravity Desktop IDE







        agy_path = Path.home() / "AppData/Local/Programs/Antigravity/Antigravity.exe"







        agy_exists = agy_path.exists()







        entries.append(







            {







                "inventory_id": "subscribed_app:antigravity_desktop",







                "model_id": "antigravity-desktop-subscription",







                "provider": "google_antigravity_subscription",







                "source_interface": "desktop_ui:Antigravity.exe",







                "digest": "installed" if agy_exists else "missing",







                "family": "antigravity",







                "parameter_size": "unknown",







                "quantization_level": "cloud",







                "app_name": "Antigravity.exe",







                "app_version": "2.0",







                "executable_path": str(agy_path) if agy_exists else "",







                "capabilities": ["generation", "tools", "desktop_ui"],







                "config": {"billing_mode": "existing_subscription_only", "observed_model_identity": "unknown"},







                "unsupported_settings": ["num_threads", "raw_logits"],







                "access_status": "verified_accessible" if agy_exists else "resource_blocked",







                "access_detail": f"Verified Antigravity IDE binary at {agy_path}" if agy_exists else "Antigravity.exe not found",







            }







        )







        # 3. Codex Desktop UI (OpenAI.Codex package -> ChatGPT.exe) and Codex App-Server (codex.exe)







        codex_ui_candidates = list(Path("C:/Program Files/WindowsApps").glob("OpenAI.Codex_*_x64__2p2nqsd0c76g0/app/ChatGPT.exe"))







        codex_ui_exe = str(codex_ui_candidates[-1]) if codex_ui_candidates else ""







        codex_pkg_ver = "unknown"







        if codex_ui_candidates:







            m = re.search(r"OpenAI\.Codex_([0-9.]+)_x64", str(codex_ui_candidates[-1]))







            if m:







                codex_pkg_ver = m.group(1)







        codex_cli_candidates = list((Path.home() / "AppData/Local/OpenAI/Codex/bin").glob("*/codex.exe"))







        if not codex_cli_candidates and codex_ui_candidates:







            res_cli = codex_ui_candidates[-1].parent / "resources" / "codex.exe"







            if res_cli.exists():







                codex_cli_candidates = [res_cli]







        codex_cli_exe = str(codex_cli_candidates[-1]) if codex_cli_candidates else ""







        codex_cfg_model = "gpt-5.4"







        codex_cfg_path = Path.home() / ".codex" / "config.toml"







        if codex_cfg_path.exists():







            try:







                cfg_txt = codex_cfg_path.read_text(encoding="utf-8", errors="replace")







                m_mod = re.search(r'^\s*model\s*=\s*"([^"]+)"', cfg_txt, flags=re.MULTILINE)







                if m_mod:







                    codex_cfg_model = m_mod.group(1)







            except Exception:







                pass







        entries.append(







            {







                "inventory_id": "subscribed_app:codex_desktop",







                "model_id": codex_cfg_model,







                "provider": "openai_codex_subscription",







                "source_interface": "desktop_ui:Codex.exe",







                "digest": codex_pkg_ver,







                "family": "codex",







                "parameter_size": "unknown",







                "quantization_level": "cloud",







                "app_name": "Codex.exe",







                "app_version": codex_pkg_ver,







                "executable_path": codex_ui_exe,







                "capabilities": ["generation", "tools", "desktop_ui"],







                "config": {







                    "billing_mode": "existing_subscription_only",







                    "configured_model": codex_cfg_model,







                    "package_family": "OpenAI.Codex_2p2nqsd0c76g0",







                },







                "unsupported_settings": ["num_threads", "raw_logits"],







                "access_status": "verified_accessible" if codex_ui_exe else "resource_blocked",







                "access_detail": f"Verified OpenAI.Codex desktop executable at {codex_ui_exe}",







            }







        )







        entries.append(







            {







                "inventory_id": "subscribed_app:codex_app_server",







                "model_id": codex_cfg_model,







                "provider": "openai_codex_subscription",







                "source_interface": "app_server_stdio:codex.exe",







                "digest": codex_pkg_ver,







                "family": "codex",







                "parameter_size": "unknown",







                "quantization_level": "cloud",







                "app_name": "Codex.exe",







                "app_version": codex_pkg_ver,







                "executable_path": codex_cli_exe,







                "capabilities": ["generation", "tools", "app_server_stdio"],







                "config": {







                    "billing_mode": "existing_subscription_only",







                    "configured_model": codex_cfg_model,







                    "protocol": "jsonrpc_stdio_v2",







                },







                "unsupported_settings": ["num_threads", "raw_logits"],







                "access_status": "verified_accessible" if codex_cli_exe else "resource_blocked",







                "access_detail": f"Verified Codex app-server binary at {codex_cli_exe}",







            }







        )







        return entries







    # ------------------------------------------------------------------







    # 2. Campaign & Trial Recording







    # ------------------------------------------------------------------







    def create_or_resume_campaign(







        self,







        *,







        campaign_id: Optional[str] = None,







        name: str = "Sabrina Model & Tool Benchmark Campaign",







        batch_window_seconds: int = 7200,







        repeats: int = 3,







        orchestrator_model: str = DEFAULT_ORCHESTRATOR_MODEL,







        orchestrator_think: bool = True,







    ) -> Dict[str, Any]:







        self.verify_frozen_tasks_unmodified()







        cid = campaign_id or f"camp_{uuid.uuid4().hex[:12]}"







        now = _now()







        harness_fp = self.freeze_guard.baseline["combined_sha256"]







        grader_fp = FROZEN_SUITE_FINGERPRINT







        with self._connect() as db:







            existing = db.execute(







                "SELECT * FROM eval_campaigns WHERE campaign_id = ?",







                (cid,),







            ).fetchone()







            if existing is not None:







                if existing["harness_fingerprint"] != harness_fp or existing["grader_fingerprint"] != grader_fp:







                    db.execute(







                        """







                        UPDATE eval_trials







                        SET outcome_status = 'fingerprint_invalidated',







                            verified_success = NULL,







                            blocker_reason = 'harness_or_grader_fingerprint_changed'







                        WHERE campaign_id = ?







                        """,







                        (cid,),







                    )







                    db.execute(







                        """







                        UPDATE eval_campaigns







                        SET status = 'restarted_after_fingerprint_change',







                            harness_fingerprint = ?,







                            grader_fingerprint = ?,







                            updated_at = ?







                        WHERE campaign_id = ?







                        """,







                        (harness_fp, grader_fp, now, cid),







                    )







                    db.commit()







            else:







                db.execute(







                    """







                    INSERT INTO eval_campaigns(







                      campaign_id, name, status, batch_window_seconds, repeats,







                      orchestrator_model, orchestrator_think, harness_fingerprint,







                      grader_fingerprint, suite_fingerprint, created_at, updated_at, notes_json







                    ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')







                    """,







                    (







                        cid,







                        name,







                        int(batch_window_seconds),







                        int(repeats),







                        orchestrator_model,







                        1 if orchestrator_think else 0,







                        harness_fp,







                        grader_fp,







                        FROZEN_SUITE_FINGERPRINT,







                        now,







                        now,







                    ),







                )







                db.commit()







            row = db.execute("SELECT * FROM eval_campaigns WHERE campaign_id = ?", (cid,)).fetchone()







        return dict(row)







    @staticmethod







    def build_config_key(







        model_id: str,







        think_setting: str,







        result_category: str,







        access_method: str = "",







    ) -> str:







        """Build a canonical configuration key that NEVER mixes think=true and think=false."""







        think_norm = str(think_setting or "unset").lower()







        if think_norm not in {"true", "false", "unset"}:







            think_norm = "unset"







        cat_norm = str(result_category or "model_alone")







        suffix = f"|via:{access_method}" if access_method else ""







        return f"{model_id}|think:{think_norm}|cat:{cat_norm}{suffix}"







    def compute_trial_cache_key(







        self,







        *,







        model_id: str,







        model_digest: str,







        think_setting: str,







        result_category: str,







        access_method: str,







        task_id: str,







        repeat_index: int,







        execution_mode: str,







    ) -> str:







        raw = _canonical_json(







            {







                "model_id": model_id,







                "model_digest": model_digest or "unknown",







                "think_setting": str(think_setting).lower(),







                "result_category": result_category,







                "access_method": access_method or "",







                "task_id": task_id,







                "repeat_index": int(repeat_index),







                "execution_mode": execution_mode,







                "suite_fp": FROZEN_SUITE_FINGERPRINT,







                "harness_fp": self.freeze_guard.baseline["combined_sha256"],







            }







        )







        return f"cache_{task_id}_think_{think_setting}_{_sha256_text(raw)}"







    def record_trial(







        self,







        *,







        campaign_id: str,







        task: Dict[str, Any],







        repeat_index: int,







        result_category: str,







        execution_mode: str,







        model_id: str,







        model_digest: str = "unknown",







        provider: str = "lab_ollama",







        think_setting: str = "unset",







        requested_device: str = "auto",







        recorded_placement: str = "unknown",







        cpu_preset_threads: Optional[int] = None,







        app_name: str = "",







        app_version: str = "unknown",







        access_method: str = "",







        observed_model_identity: str = "unknown",







        response_text: str = "",







        outcome_status: Optional[str] = None,







        blocker_reason: str = "",







        queue_seconds: float = 0.0,







        load_seconds: float = 0.0,







        inference_seconds: float = 0.0,







        tool_seconds: float = 0.0,







        total_seconds: Optional[float] = None,







        retries_count: int = 0,







        prompt_tokens: Optional[int] = None,







        completion_tokens: Optional[int] = None,







        subscription_limit_events: int = 0,







        unsupported_settings: Optional[List[str]] = None,







        run_id: str = "",







        audit_event_ids: Optional[List[Any]] = None,







        retention_days: int = 7,







        use_cache: bool = True,







    ) -> Dict[str, Any]:







        """Grade (if not blocked) and store a single benchmark or holdout trial."""







        if result_category not in RESULT_CATEGORIES:







            raise ValueError(f"Invalid result_category: {result_category}")







        if execution_mode not in EXECUTION_MODES:







            raise ValueError(f"Invalid execution_mode: {execution_mode}")







        self.verify_frozen_tasks_unmodified()







        freeze_check = self.freeze_guard.check()







        harness_fp = freeze_check["current_sha256"]







        grader_fp = FROZEN_SUITE_FINGERPRINT







        config_key = self.build_config_key(model_id, think_setting, result_category, access_method)







        cache_key = self.compute_trial_cache_key(







            model_id=model_id,







            model_digest=model_digest,







            think_setting=think_setting,







            result_category=result_category,







            access_method=access_method,







            task_id=task["task_id"],







            repeat_index=repeat_index,







            execution_mode=execution_mode,







        )







        with self._connect() as db:







            if use_cache and freeze_check["valid"]:







                cached = db.execute(







                    """







                    SELECT * FROM eval_trials







                    WHERE cache_key = ? AND outcome_status IN ('passed', 'failed')







                    ORDER BY created_at DESC LIMIT 1







                    """,







                    (cache_key,),







                ).fetchone()







                if cached is not None:







                    res = dict(cached)







                    res["from_cache"] = True







                    return res







            if not freeze_check["valid"]:







                status = "fingerprint_invalidated"







                verified_success = None







                tool_validity = None







                recov_success = None







                crit_ok = 0







                grader_summary = f"Invalidated: modified files {freeze_check['modified_files']}"







                blocker_reason = grader_summary







            elif outcome_status in BLOCKER_STATUSES:







                status = outcome_status







                verified_success = None







                tool_validity = None







                recov_success = None







                crit_ok = 1







                grader_summary = f"Operational blocker ({status}): {blocker_reason}"







            else:







                grade = grade_frozen_submission(task, response_text, caller="independent_harness_grader")







                verified_success = grade["verified_success"]







                tool_validity = grade["tool_call_validity"]







                recov_success = grade["recovery_success"]







                crit_ok = grade["critical_scope_check_passed"]







                grader_summary = grade["grader_summary"]







                status = "passed" if verified_success == 1 else "failed"







            now = _now()







            tot_sec = (







                float(total_seconds)







                if total_seconds is not None







                else round(queue_seconds + load_seconds + inference_seconds + tool_seconds, 4)







            )







            trial_id = f"tr_{uuid.uuid4().hex[:14]}"







            expires_at = now + max(1, min(365, int(retention_days))) * 86400.0







            db.execute(







                """







                INSERT INTO eval_trials(







                  trial_id, campaign_id, run_id, audit_event_ids_json,







                  task_id, split, category, repeat_index,







                  result_category, execution_mode, config_key,







                  model_id, model_digest, provider, think_setting,







                  requested_device, recorded_placement, cpu_preset_threads,







                  app_name, app_version, access_method, observed_model_identity,







                  outcome_status, verified_success, tool_call_validity,







                  recovery_success, critical_scope_check_passed,







                  queue_seconds, load_seconds, inference_seconds, tool_seconds, total_seconds,







                  retries_count, prompt_tokens, completion_tokens,







                  subscription_limit_events, unsupported_settings_json,







                  blocker_reason, grader_summary, raw_response_preview,







                  raw_expires_at, raw_expired, cache_key,







                  harness_fingerprint, grader_fingerprint, created_at







                ) VALUES (







                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,







                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?







                )







                """,







                (







                    trial_id,







                    campaign_id,







                    run_id or "",







                    json.dumps(audit_event_ids or []),







                    task["task_id"],







                    task["split"],







                    task["category"],







                    int(repeat_index),







                    result_category,







                    execution_mode,







                    config_key,







                    model_id,







                    model_digest or "unknown",







                    provider,







                    str(think_setting).lower(),







                    requested_device or "auto",







                    recorded_placement or "unknown",







                    cpu_preset_threads,







                    app_name if result_category == "sabrina_assisted_by_app" else "",







                    app_version if result_category == "sabrina_assisted_by_app" else "unknown",







                    access_method or "",







                    observed_model_identity if result_category == "sabrina_assisted_by_app" else model_id,







                    status,







                    verified_success,







                    tool_validity,







                    recov_success,







                    crit_ok,







                    round(float(queue_seconds), 4),







                    round(float(load_seconds), 4),







                    round(float(inference_seconds), 4),







                    round(float(tool_seconds), 4),







                    round(tot_sec, 4),







                    int(retries_count),







                    int(prompt_tokens) if prompt_tokens is not None else None,







                    int(completion_tokens) if completion_tokens is not None else None,







                    int(subscription_limit_events),







                    json.dumps(unsupported_settings or []),







                    blocker_reason or "",







                    grader_summary,







                    (response_text or "")[:1200],







                    expires_at,







                    cache_key,







                    harness_fp,







                    grader_fp,







                    now,







                ),







            )







            db.commit()







            row = db.execute("SELECT * FROM eval_trials WHERE trial_id = ?", (trial_id,)).fetchone()







        res = dict(row)







        res["from_cache"] = False







        return res







    # ------------------------------------------------------------------







    # 3. Live Lab Execution Through Coordinator







    # ------------------------------------------------------------------







    def run_lab_ollama_trial(







        self,







        *,







        campaign_id: str,







        task: Dict[str, Any],







        repeat_index: int,







        model_id: str,







        think_setting: str = "true",







        result_category: str = "model_alone",







        requested_device: str = "auto",







        run_id: str = "",







        timeout_seconds: float = 90.0,







    ) -> Dict[str, Any]:







        """Execute a single trial against a lab Ollama model through the coordinator."""







        from tools import control_client







        registry = load_registry()







        num_threads = resolve_num_threads(model_id, registry=registry)







        lease_id: Optional[str] = None







        t_queue_start = time.perf_counter()







        queue_sec = 0.0







        recorded_placement = "unknown"







        model_digest = "unknown"







        # The coordinator owns inference admission; no direct Ollama fallback.
        ctrl = control_client.configured()

        try:







            options: Dict[str, Any] = {







                "temperature": 0.0,







                "num_predict": int(task.get("max_output_tokens") or 384),







            }







            if num_threads is not None:







                options["num_thread"] = int(num_threads)







                options["num_threads"] = int(num_threads)







            if requested_device == "cpu":







                options["num_gpu"] = 0







            body: Dict[str, Any] = {







                "model": model_id,







                "messages": [







                    {







                        "role": "system",







                        "content": "Follow the task instructions exactly. Do not add unrequested prose.",







                    },







                    {"role": "user", "content": task["prompt"]},







                ],







                "stream": False,







                "options": options,







            }







            if think_setting in {"true", "false"}:







                body["think"] = think_setting == "true"







            t_call_start = time.perf_counter()







            if ctrl is None:
                raise RuntimeError("Lab coordinator is not configured")
            raw_payload = ctrl.infer(
                '/api/chat', body, timeout=timeout_seconds,
                operation_id=f'eval:{campaign_id}:{run_id}:{model_id}:{task["task_id"]}:{repeat_index}',
                device=requested_device, task_label=f'evaluation {task["task_id"]}')
            wall_sec = time.perf_counter() - t_call_start







            # Check actual placement via /api/ps







            try:







                ps_req = urllib.request.Request(f"{self.ollama_url}/api/ps", method="GET")







                with urllib.request.urlopen(ps_req, timeout=5.0) as ps_resp:







                    ps_data = json.loads(ps_resp.read().decode("utf-8", errors="replace"))







                for m_ps in ps_data.get("models") or []:







                    if m_ps.get("name") == model_id or m_ps.get("model") == model_id:







                        model_digest = str(m_ps.get("digest") or "unknown")[:24]







                        vram = int(m_ps.get("size_vram") or 0)







                        total_sz = int(m_ps.get("size") or 0)







                        if vram > 0 and vram >= total_sz * 0.9:







                            recorded_placement = "GPU"







                        elif vram > 0:







                            recorded_placement = "HYBRID_GPU_CPU"







                        else:







                            recorded_placement = "CPU"







                        break







            except Exception:







                pass







            load_sec = float(raw_payload.get("load_duration") or 0) / 1e9







            eval_sec = (







                float(raw_payload.get("prompt_eval_duration") or 0)







                + float(raw_payload.get("eval_duration") or 0)







            ) / 1e9







            if eval_sec <= 0.0:







                eval_sec = max(0.0, wall_sec - load_sec)







            msg = raw_payload.get("message") or {}







            content = str(msg.get("content") or "")







            prompt_tokens = raw_payload.get("prompt_eval_count")







            completion_tokens = raw_payload.get("eval_count")







            return self.record_trial(







                campaign_id=campaign_id,







                task=task,







                repeat_index=repeat_index,







                result_category=result_category,







                execution_mode="live",







                model_id=model_id,







                model_digest=model_digest,







                provider="lab_ollama",







                think_setting=think_setting,







                requested_device=requested_device,







                recorded_placement=recorded_placement,







                cpu_preset_threads=num_threads,







                response_text=content,







                queue_seconds=queue_sec,







                load_seconds=load_sec,







                inference_seconds=eval_sec,







                total_seconds=queue_sec + wall_sec,







                prompt_tokens=int(prompt_tokens) if prompt_tokens is not None else None,







                completion_tokens=int(completion_tokens) if completion_tokens is not None else None,







                run_id=run_id,







            )







        except urllib.error.HTTPError as exc:







            err_body = ""







            try:







                err_body = exc.read().decode("utf-8", errors="replace")[:300]







            except Exception:







                pass







            status_code = "unsupported" if exc.code == 400 and "think" in err_body.lower() else "app_failure"







            return self.record_trial(







                campaign_id=campaign_id,







                task=task,







                repeat_index=repeat_index,







                result_category=result_category,







                execution_mode="live",







                model_id=model_id,







                think_setting=think_setting,







                requested_device=requested_device,







                recorded_placement=recorded_placement,







                cpu_preset_threads=num_threads,







                outcome_status=status_code,







                blocker_reason=f"HTTP {exc.code}: {err_body}",







                unsupported_settings=["think"] if status_code == "unsupported" else [],







                queue_seconds=queue_sec,







                run_id=run_id,







            )







        except Exception as exc:







            return self.record_trial(







                campaign_id=campaign_id,







                task=task,







                repeat_index=repeat_index,







                result_category=result_category,







                execution_mode="live",







                model_id=model_id,







                think_setting=think_setting,







                requested_device=requested_device,







                recorded_placement=recorded_placement,







                cpu_preset_threads=num_threads,







                outcome_status="resource_wait",







                blocker_reason=f"ollama_error:{exc}",







                queue_seconds=queue_sec,







                run_id=run_id,







            )







        finally:







            if lease_id:







                try:







                    control_client.release(lease_id)







                except Exception:







                    pass







    # ------------------------------------------------------------------







    # 4. Separate Retrieval Evaluation for Embedding Models







    # ------------------------------------------------------------------







    @staticmethod







    def _cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:







        if not vec_a or not vec_b or len(vec_a) != len(vec_b):







            return 0.0







        dot = sum(a * b for a, b in zip(vec_a, vec_b))







        na = math.sqrt(sum(a * a for a in vec_a))







        nb = math.sqrt(sum(b * b for b in vec_b))







        if na <= 1e-12 or nb <= 1e-12:







            return 0.0







        return dot / (na * nb)







    def evaluate_embedding_model(







        self,







        *,







        campaign_id: str,







        model_id: str,







        run_id: str = "",







        execution_mode: str = "live",







        requested_device: str = "auto",







        custom_embed_fn: Optional[Callable[[List[str]], List[List[float]]]] = None,







    ) -> Dict[str, Any]:







        """Run the frozen retrieval evaluation on an embedding model."""







        registry = load_registry()







        num_threads = resolve_num_threads(model_id, registry=registry)







        passages = FROZEN_RETRIEVAL_CORPUS["passages"]







        queries = FROZEN_RETRIEVAL_CORPUS["queries"]







        t0 = time.perf_counter()







        recorded_placement = "CPU" if requested_device == "cpu" else "unknown"







        model_digest = "unknown"







        try:







            texts = [p["text"] for p in passages] + [q["query"] for q in queries]







            if custom_embed_fn is not None:







                vectors = custom_embed_fn(texts)







            else:







                options: Dict[str, Any] = {}







                if num_threads is not None:







                    options["num_thread"] = int(num_threads)







                    options["num_threads"] = int(num_threads)







                if requested_device == "cpu":







                    options["num_gpu"] = 0







                req = urllib.request.Request(







                    f"{self.ollama_url}/api/embed",







                    data=json.dumps({"model": model_id, "input": texts, "options": options}).encode("utf-8"),







                    headers={"Content-Type": "application/json"},







                    method="POST",







                )







                with urllib.request.urlopen(req, timeout=60.0) as resp:







                    data = json.loads(resp.read().decode("utf-8", errors="replace"))







                vectors = data.get("embeddings") or []







                try:







                    ps_req = urllib.request.Request(f"{self.ollama_url}/api/ps", method="GET")







                    with urllib.request.urlopen(ps_req, timeout=5.0) as ps_resp:







                        ps_data = json.loads(ps_resp.read().decode("utf-8", errors="replace"))







                    for m_ps in ps_data.get("models") or []:







                        if m_ps.get("name") == model_id or m_ps.get("model") == model_id:







                            model_digest = str(m_ps.get("digest") or "unknown")[:24]







                            recorded_placement = "GPU" if int(m_ps.get("size_vram") or 0) > 0 else "CPU"







                            break







                except Exception:







                    pass







            if len(vectors) != len(texts):







                raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(vectors)}")







            doc_vecs = vectors[: len(passages)]







            query_vecs = vectors[len(passages) :]







            top1_hits = 0







            rr_sum = 0.0







            margins: List[float] = []







            per_query: List[Dict[str, Any]] = []







            for q_obj, q_vec in zip(queries, query_vecs):







                scored = []







                for p_obj, d_vec in zip(passages, doc_vecs):







                    sim = self._cosine_similarity(q_vec, d_vec)







                    scored.append((sim, p_obj["doc_id"]))







                scored.sort(key=lambda x: x[0], reverse=True)







                ranked_ids = [doc_id for _, doc_id in scored]







                expected_id = q_obj["expected_doc_id"]







                rank = ranked_ids.index(expected_id) + 1 if expected_id in ranked_ids else 999







                if rank == 1:







                    top1_hits += 1







                if rank <= 3:







                    rr_sum += 1.0 / rank







                best_other = max(s for s, did in scored if did != expected_id)







                target_sim = next(s for s, did in scored if did == expected_id)







                margins.append(target_sim - best_other)







                per_query.append(







                    {







                        "query_id": q_obj["query_id"],







                        "expected_doc_id": expected_id,







                        "top_doc_id": ranked_ids[0],







                        "rank": rank,







                        "margin": round(target_sim - best_other, 4),







                    }







                )







            q_count = len(queries)







            top1_acc = round(top1_hits / q_count, 4)







            mrr3 = round(rr_sum / q_count, 4)







            mean_margin = round(sum(margins) / len(margins), 4)







            outcome_status = "passed" if top1_acc >= 0.8 else "failed"







            details = {"queries": per_query}







        except Exception as exc:







            q_count = len(queries)







            top1_acc = None







            mrr3 = None







            mean_margin = None







            outcome_status = "resource_wait"







            details = {"error": str(exc)}







        elapsed = round(time.perf_counter() - t0, 4)







        ret_id = f"ret_{uuid.uuid4().hex[:12]}"







        harness_fp = self.freeze_guard.baseline["combined_sha256"]







        with self._connect() as db:







            db.execute(







                """







                INSERT INTO eval_retrieval_trials(







                  retrieval_trial_id, campaign_id, run_id, model_id, model_digest,







                  execution_mode, requested_device, recorded_placement, cpu_preset_threads,







                  query_count, top1_accuracy, mrr_at_3, mean_cosine_margin,







                  total_seconds, outcome_status, details_json, harness_fingerprint, created_at







                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)







                """,







                (







                    ret_id,







                    campaign_id,







                    run_id or "",







                    model_id,







                    model_digest,







                    execution_mode,







                    requested_device,







                    recorded_placement,







                    num_threads,







                    q_count,







                    top1_acc,







                    mrr3,







                    mean_margin,







                    elapsed,







                    outcome_status,







                    json.dumps(details),







                    harness_fp,







                    _now(),







                ),







            )







            db.commit()







            row = db.execute(







                "SELECT * FROM eval_retrieval_trials WHERE retrieval_trial_id = ?",







                (ret_id,),







            ).fetchone()







        return dict(row)







    # ------------------------------------------------------------------







    # 5. Feedback Analysis: Test-and-Repair vs Independent Attempts







    # ------------------------------------------------------------------







    def record_feedback_comparison(







        self,







        *,







        campaign_id: str,







        task: Dict[str, Any],







        model_id: str,







        think_setting: str,







        call_budget: int,







        repair_attempt_outputs: Sequence[str],







         repair_seconds: float,







        independent_attempt_outputs: Sequence[str],







        independent_seconds: float,







        sabrina_evidence_notes: Optional[Dict[str, Any]] = None,







        run_id: str = "",







    ) -> Dict[str, Any]:







        """Compare test-and-repair against independent attempts under the same call budget.







        All grading is performed strictly by `grade_frozen_submission` with







        `caller='independent_harness_grader'`. Sabrina may attach analysis notes







        on the resulting evidence but cannot grade submissions or alter tests.







        """







        if call_budget <= 0:







            raise ValueError("call_budget must be >= 1")







        if len(repair_attempt_outputs) > call_budget or len(independent_attempt_outputs) > call_budget:







            raise ValueError("Attempt lists cannot exceed call_budget")







        self.verify_frozen_tasks_unmodified()







        repair_success = 0







        repair_used = 0







        for out in repair_attempt_outputs[:call_budget]:







            repair_used += 1







            g = grade_frozen_submission(task, out, caller="independent_harness_grader")







            if g["verified_success"] == 1:







                repair_success = 1







                break







        ind_success = 0







        ind_used = 0







        for out in independent_attempt_outputs[:call_budget]:







            ind_used += 1







            g = grade_frozen_submission(task, out, caller="independent_harness_grader")







            if g["verified_success"] == 1:







                ind_success = 1







                break







        comp_id = f"fb_{uuid.uuid4().hex[:12]}"







        config_key = self.build_config_key(model_id, think_setting, "model_using_tools")







        harness_fp = self.freeze_guard.baseline["combined_sha256"]







        analysis = {







            "budget": call_budget,







            "repair_advantage": repair_success - ind_success,







            "calls_saved_by_repair": ind_used - repair_used,







            "sabrina_notes": sabrina_evidence_notes or {},







        }







        with self._connect() as db:







            db.execute(







                """







                INSERT INTO eval_feedback_comparisons(







                  comparison_id, campaign_id, run_id, task_id, config_key,







                  model_id, think_setting, call_budget,







                  test_and_repair_success, test_and_repair_calls_used, test_and_repair_seconds,







                  independent_attempts_success, independent_attempts_calls_used, independent_attempts_seconds,







                  sabrina_analysis_json, grader_identity, harness_fingerprint, created_at







                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'independent_frozen_grader', ?, ?)







                """,







                (







                    comp_id,







                    campaign_id,







                    run_id or "",







                    task["task_id"],







                    config_key,







                    model_id,







                    str(think_setting).lower(),







                    int(call_budget),







                    repair_success,







                    repair_used,







                    round(float(repair_seconds), 4),







                    ind_success,







                    ind_used,







                    round(float(independent_seconds), 4),







                    json.dumps(analysis),







                    harness_fp,







                    _now(),







                ),







            )







            db.commit()







            row = db.execute(







                "SELECT * FROM eval_feedback_comparisons WHERE comparison_id = ?",







                (comp_id,),







            ).fetchone()







        return dict(row)







    # ------------------------------------------------------------------







    # 6. Automatic Per-Category Qualification & Revalidation







    # ------------------------------------------------------------------







    def recompute_qualifications(







        self,







        *,







        role: str = "execution",







        min_holdout_trials: int = 18,







        min_holdout_pass_rate: float = 0.80,







        min_verified_live_tasks: int = 3,







    ) -> List[Dict[str, Any]]:







        """Compute qualification status per task category and configuration.







        Qualification criteria:







        - >= 80% verified success on the 6 additional held-out tasks repeated 3 times







          (18 trials across the held-out suite, or >= 80% on held-out trials with







          at least `min_holdout_trials` completed non-blocked trials).







        - Zero critical scope/reference violations (`critical_violations == 0`).







        - At least 3 verified live tasks (`verified_live_tasks >= 3`) for execution roles.







        - Blocked trials are excluded from accuracy scores while operational failure







          rates are computed explicitly (`blocked / total_attempted`).







        - Any mismatch in harness/grader fingerprint marks `requires_revalidation = 1`.







        """







        current_harness_fp = compute_harness_fingerprints()["combined_sha256"]







        current_grader_fp = compute_frozen_suite_fingerprint()







        now = _now()







        results: List[Dict[str, Any]] = []







        with self._connect() as db:







            configs = db.execute(







                """







                SELECT DISTINCT config_key, model_id, model_digest, think_setting, result_category







                FROM eval_trials







                """







            ).fetchall()







            for cfg in configs:







                ckey = cfg["config_key"]







                model_id = cfg["model_id"]







                model_digest = cfg["model_digest"]







                think_setting = cfg["think_setting"]







                res_cat = cfg["result_category"]







                # Compute overall held-out performance (6 held-out tasks x 3 repeats = 18 trials)







                holdout_rows = db.execute(







                    """







                    SELECT outcome_status, verified_success, critical_scope_check_passed,







                           execution_mode, total_seconds, harness_fingerprint, grader_fingerprint







                    FROM eval_trials







                    WHERE config_key = ? AND split IN ('held_out', 'holdout')







                    """,







                    (ckey,),







                ).fetchall()







                ho_valid = [r for r in holdout_rows if r["outcome_status"] in ("passed", "failed")]







                ho_samples = len(ho_valid)







                ho_passes = sum(1 for r in ho_valid if r["verified_success"] == 1)







                ho_rate = round(ho_passes / ho_samples, 4) if ho_samples > 0 else 0.0







                for category in TASK_CATEGORIES:







                    cat_rows = db.execute(







                        """







                        SELECT task_id, category, split, outcome_status, verified_success,







                               critical_scope_check_passed, execution_mode,







                               total_seconds, harness_fingerprint, grader_fingerprint







                        FROM eval_trials







                        WHERE config_key = ? AND (category = ? OR split IN ('held_out', 'holdout'))







                        """,







                        (ckey, category),







                    ).fetchall()







                    bench_rows = [







                        r for r in cat_rows







                        if r["category"] == category and r["split"] == "benchmark"







                    ]







                    # Query category specifically for benchmark + holdout







                    cat_specific = db.execute(







                        """







                        SELECT task_id, category, split, outcome_status, verified_success,







                               critical_scope_check_passed, execution_mode,







                               total_seconds, harness_fingerprint, grader_fingerprint







                        FROM eval_trials







                        WHERE config_key = ? AND category = ?







                        """,







                        (ckey, category),







                    ).fetchall()







                    bench_valid = [







                        r for r in cat_specific







                        if r["split"] == "benchmark" and r["outcome_status"] in ("passed", "failed")







                    ]







                    bench_samples = len(bench_valid)







                    bench_passes = sum(1 for r in bench_valid if r["verified_success"] == 1)







                    bench_rate = round(bench_passes / bench_samples, 4) if bench_samples > 0 else 0.0







                    all_cat_and_ho = list(cat_specific) + list(holdout_rows)







                    blocked_count = sum(







                        1 for r in cat_specific if r["outcome_status"] in BLOCKER_STATUSES







                    )







                    total_cat_attempts = len(cat_specific)







                    op_fail_rate = (







                        round(blocked_count / total_cat_attempts, 4)







                        if total_cat_attempts > 0







                        else 0.0







                    )







                    crit_violations = sum(







                        1 for r in all_cat_and_ho if int(r["critical_scope_check_passed"] or 0) == 0







                    )







                    live_verified_tasks = len(







                        {







                            r["task_id"]







                            for r in cat_specific







                            if r["execution_mode"] == "live" and r["verified_success"] == 1







                        }







                        | {







                            r["task_id"]







                            for r in db.execute(







                                """







                                SELECT task_id FROM eval_trials







                                WHERE config_key = ? AND execution_mode = 'live' AND verified_success = 1







                                """,







                                (ckey,),







                            ).fetchall()







                        }







                    )







                    valid_latencies = [







                        float(r["total_seconds"])







                        for r in (bench_valid + ho_valid)







                        if r["total_seconds"] is not None and float(r["total_seconds"]) > 0







                    ]







                    mean_sec = (







                        round(sum(valid_latencies) / len(valid_latencies), 4)







                        if valid_latencies







                        else 999.0







                    )







                    fp_mismatch = any(







                        r["harness_fingerprint"] != current_harness_fp







                        or r["grader_fingerprint"] != current_grader_fp







                        for r in all_cat_and_ho







                    )







                    reval_reason = "fingerprint_changed" if fp_mismatch else ""







                    live_gate_ok = (







                        live_verified_tasks >= min_verified_live_tasks







                        if role == "execution"







                        else True







                    )







                    is_qualified = (







                        ho_samples >= min_holdout_trials







                        and ho_rate >= min_holdout_pass_rate







                        and (bench_passes / bench_samples >= 0.8 if bench_samples > 0 else True)







                        and crit_violations == 0







                        and live_gate_ok







                        and not fp_mismatch







                    )







                    ci_low, ci_high = _wilson_interval(







                        ho_passes + bench_passes, ho_samples + bench_samples







                    )







                    qid = f"qual_{_sha256_text(f'{category}|{role}|{ckey}')[:14]}"







                    db.execute(







                        """







                        INSERT INTO eval_qualifications(







                          qualification_id, category, role, config_key,







                          model_id, model_digest, think_setting, result_category,







                          qualified, requires_revalidation, revalidation_reason,







                          holdout_sample_count, holdout_pass_count, holdout_pass_rate,







                          benchmark_sample_count, benchmark_pass_count, benchmark_pass_rate,







                          blocked_trial_count, operational_failure_rate,







                          critical_violations, verified_live_tasks, mean_total_seconds,







                          ci_low, ci_high, harness_fingerprint, grader_fingerprint, updated_at







                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)







                        ON CONFLICT(category, role, config_key) DO UPDATE SET







                          model_digest=excluded.model_digest,







                          qualified=excluded.qualified,







                          requires_revalidation=excluded.requires_revalidation,







                          revalidation_reason=excluded.revalidation_reason,







                          holdout_sample_count=excluded.holdout_sample_count,







                          holdout_pass_count=excluded.holdout_pass_count,







                          holdout_pass_rate=excluded.holdout_pass_rate,







                          benchmark_sample_count=excluded.benchmark_sample_count,







                          benchmark_pass_count=excluded.benchmark_pass_count,







                          benchmark_pass_rate=excluded.benchmark_pass_rate,







                          blocked_trial_count=excluded.blocked_trial_count,







                          operational_failure_rate=excluded.operational_failure_rate,







                          critical_violations=excluded.critical_violations,







                          verified_live_tasks=excluded.verified_live_tasks,







                          mean_total_seconds=excluded.mean_total_seconds,







                          ci_low=excluded.ci_low,







                          ci_high=excluded.ci_high,







                          harness_fingerprint=excluded.harness_fingerprint,







                          grader_fingerprint=excluded.grader_fingerprint,







                          updated_at=excluded.updated_at







                        """,







                        (







                            qid,







                            category,







                            role,







                            ckey,







                            model_id,







                            model_digest,







                            think_setting,







                            res_cat,







                            1 if is_qualified else 0,







                            1 if fp_mismatch else 0,







                            reval_reason,







                            ho_samples,







                            ho_passes,







                            ho_rate,







                            bench_samples,







                            bench_passes,







                            bench_rate,







                            blocked_count,







                            op_fail_rate,







                            crit_violations,







                            live_verified_tasks,







                            mean_sec,







                            ci_low,







                            ci_high,







                            current_harness_fp,







                            current_grader_fp,







                            now,







                        ),







                    )







                    row = db.execute(







                        "SELECT * FROM eval_qualifications WHERE qualification_id = ?",







                        (qid,),







                    ).fetchone()







                    results.append(dict(row))







            db.commit()







        return results







    def invalidate_qualifications_for_change(self, reason: str = "change", scope: str = "general") -> Any:







        """Mark all qualifications as requiring revalidation when model/skill/adapter/harness changes."""







        with self._connect() as db:







            cur = db.execute(







                """







                UPDATE eval_qualifications







                SET qualified = 0,







                    requires_revalidation = 1,







                    revalidation_reason = ?,







                    updated_at = ?







                """,







                (f"{scope}:{reason}" if scope != "general" else reason, _now()),







            )







            db.commit()







            count = int(cur.rowcount or 0)







            class _InvalidationResult(int):







                def __getitem__(self, item):







                    if item == "invalidated_count":







                        return int(self)







                    if item == "scope":







                        return scope







                    if item == "reason":







                        return reason







                    raise KeyError(item)







            return _InvalidationResult(count)







    # ------------------------------------------------------------------







    # 7. Bounded Performance Lookup & Routing Decision Recording







    # ------------------------------------------------------------------







    def performance_lookup_and_route(







        self,







        *,







        category: Optional[str] = None,







        task_category: Optional[str] = None,







        role: str = "execution",







        execution_role: Optional[str] = None,







        result_category: Optional[str] = None,







        run_id: str = "",







        caller: str = "sabrina",







        require_qualified: bool = True,







    ) -> Dict[str, Any]:







        """Expose bounded performance evidence to Sabrina and select the optimal configuration.







        Routes among qualified configurations by:







          1. Verified task success rate (`benchmark_pass_rate` then `holdout_pass_rate`) DESC







          2. End-to-end latency (`mean_total_seconds`) ASC







        Preserves sample counts and uncertainty intervals (`[ci_low, ci_high]`).







        Records the bounded evidence Sabrina received and the selected configuration.







        """







        cat = category or task_category or "coding_executable_checks"







        if execution_role in ("model_using_tools", "model_alone", "sabrina_assisted_by_app"):







            result_category = execution_role







            rol = "execution"







        else:







            rol = execution_role or role







        self.recompute_qualifications(role=rol)







        with self._connect() as db:







            sql = """







                SELECT * FROM eval_qualifications







                WHERE category = ? AND role = ?







            """







            params: List[Any] = [cat, rol]







            if result_category:







                sql += " AND result_category = ?"







                params.append(result_category)







            sql += """







                ORDER BY







                  qualified DESC,







                  requires_revalidation ASC,







                  benchmark_pass_rate DESC,







                  holdout_pass_rate DESC,







                  mean_total_seconds ASC







            """







            rows = db.execute(sql, tuple(params)).fetchall()







            candidates = []







            for r in rows:







                candidates.append(







                    {







                        "config_key": r["config_key"],







                        "model_id": r["model_id"],







                        "think_setting": r["think_setting"],







                        "result_category": r["result_category"],







                        "qualified": bool(r["qualified"]),







                        "requires_revalidation": bool(r["requires_revalidation"]),







                        "task_success_rate": r["benchmark_pass_rate"],







                        "holdout_success_rate": r["holdout_pass_rate"],







                        "sample_count": int(r["benchmark_sample_count"]) + int(r["holdout_sample_count"]),







                        "uncertainty_ci95": [r["ci_low"], r["ci_high"]],







                        "operational_failure_rate": r["operational_failure_rate"],







                        "mean_latency_seconds": r["mean_total_seconds"],







                        "verified_live_tasks": r["verified_live_tasks"],







                    }







                )







            qualified_candidates = [







                c for c in candidates if c["qualified"] and not c["requires_revalidation"]







            ]







            if qualified_candidates:







                chosen = qualified_candidates[0]







                reason = (







                    f"Qualified for {category} (success={chosen['task_success_rate']:.1%}, "







                    f"holdout={chosen['holdout_success_rate']:.1%}, "







                    f"latency={chosen['mean_latency_seconds']:.2f}s, "







                    f"n={chosen['sample_count']}, CI95={chosen['uncertainty_ci95']})"







                )







            elif not require_qualified and candidates:







                chosen = candidates[0]







                reason = f"Fallback best available (unqualified): {chosen['config_key']}"







            else:







                chosen = {







                    "config_key": f"{DEFAULT_ORCHESTRATOR_MODEL}|think:true|cat:{result_category or 'model_using_tools'}",







                    "model_id": DEFAULT_ORCHESTRATOR_MODEL,







                    "think_setting": "true",







                    "result_category": result_category or "model_using_tools",







                    "qualified": False,







                }







                reason = "No qualified configuration met gate; defaulting to initial orchestrator pending evaluation."







            bounded_evidence = {







                "category": cat,







                "role": rol,







                "candidate_count": len(candidates),







                "qualified_count": len(qualified_candidates),







                "top_candidates": candidates[:6],







                "selected_config_key": chosen["config_key"],







                "selection_reason": reason,







            }







            bounded_json = json.dumps(bounded_evidence, ensure_ascii=False)[:3200]







            decision_id = f"rt_{uuid.uuid4().hex[:14]}"







            now = _now()







            db.execute(







                """







                INSERT INTO eval_routing_decisions(







                  decision_id, run_id, caller, task_category, role,







                  query_params_json, bounded_evidence_json,







                  selected_config_key, selected_model_id,







                  selected_think_setting, selected_result_category,







                  selection_reason, candidate_count, created_at







                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)







                """,







                (







                    decision_id,







                    run_id or "unscoped",







                    caller,







                    cat,







                    rol,







                    json.dumps(







                        {







                            "category": cat,







                            "role": rol,







                            "result_category": result_category,







                            "require_qualified": require_qualified,







                        }







                    ),







                    bounded_json,







                    chosen["config_key"],







                    chosen["model_id"],







                    chosen["think_setting"],







                    chosen["result_category"],







                    reason,







                    len(candidates),







                    now,







                ),







            )







            db.commit()







        return {







            "ok": True,







            "status": "routed" if chosen else "unrouted",







            "decision_id": decision_id,







            "selected": chosen,







            "selected_model_id": chosen.get("model_id") if chosen else "",







            "selection_reason": reason,







            "rationale": {







                "tie_breaker": "mean_total_seconds",







                "reason": reason,







                "selected_config_key": chosen.get("config_key") if chosen else "",







            },







            "bounded_evidence": bounded_evidence,







        }







    def record_routed_outcome(







        self,







        decision_id: str,







        *,







        verified_success: bool,







        verification_details: Dict[str, Any],







        linked_audit_event_id: str = "",







    ) -> Dict[str, Any]:







        """Record the independently verified outcome of a task routed by `decision_id`."""







        now = _now()







        with self._connect() as db:







            db.execute(







                """







                UPDATE eval_routing_decisions







                SET subsequent_verified_success = ?,







                    subsequent_verification_json = ?,







                    linked_audit_event_id = ?,







                    verified_at = ?







                WHERE decision_id = ?







                """,







                (







                    1 if verified_success else 0,







                    json.dumps(verification_details, ensure_ascii=False),







                    str(linked_audit_event_id or ""),







                    now,







                    decision_id,







                ),







            )







            db.commit()







            row = db.execute(







                "SELECT * FROM eval_routing_decisions WHERE decision_id = ?",







                (decision_id,),







            ).fetchone()







        if row is None:







            raise KeyError(f"Unknown routing decision_id: {decision_id}")







        rec = dict(row)







        rec["verified_outcome"] = rec.get("subsequent_verified_success")







        return rec







    # ------------------------------------------------------------------







    # 8. Retention Expiry (Preserve Numerical Results & Fingerprints)







    # ------------------------------------------------------------------







    def expire_raw_trial_previews(self, *, now: Optional[float] = None) -> int:







        """Clear `raw_response_preview` on expired trials while preserving all numerical metrics and fingerprints."""







        ts = _now() if now is None else float(now)







        with self._connect() as db:







            cur = db.execute(







                """







                UPDATE eval_trials







                SET raw_response_preview = '',







                    raw_expired = 1







                WHERE raw_expired = 0







                  AND raw_expires_at IS NOT NULL







                  AND raw_expires_at <= ?







                """,







                (ts,),







            )







            db.commit()







            return int(cur.rowcount or 0)







    # ------------------------------------------------------------------







    # 9. Console Performance View Reader







    # ------------------------------------------------------------------







    def get_console_performance_snapshot(self, limit_trials: int = 150) -> Dict[str, Any]:







        """Return structured data for the Operator Console Performance view."""







        with self._connect() as db:







            inventory = [







                dict(r)







                for r in db.execute(







                    "SELECT * FROM eval_model_inventory ORDER BY provider, model_id"







                ).fetchall()







            ]







            qualifications = [







                dict(r)







                for r in db.execute(







                    """







                    SELECT * FROM eval_qualifications







                    ORDER BY category, qualified DESC, benchmark_pass_rate DESC, mean_total_seconds ASC







                    """







                ).fetchall()







            ]







            recent_trials = [







                dict(r)







                for r in db.execute(







                    "SELECT * FROM eval_trials ORDER BY created_at DESC LIMIT ?",







                    (int(limit_trials),),







                ).fetchall()







            ]







            retrieval_trials = [







                dict(r)







                for r in db.execute(







                    "SELECT * FROM eval_retrieval_trials ORDER BY created_at DESC LIMIT 30"







                ).fetchall()







            ]







            feedback_comps = [







                dict(r)







                for r in db.execute(







                    "SELECT * FROM eval_feedback_comparisons ORDER BY created_at DESC LIMIT 30"







                ).fetchall()







            ]







            routing_decisions = [







                dict(r)







                for r in db.execute(







                    "SELECT * FROM eval_routing_decisions ORDER BY created_at DESC LIMIT 30"







                ).fetchall()







            ]







            # Aggregate model/configuration comparison rows (never mixing think=true/false)







            config_summary_rows = db.execute(







                """







                SELECT







                  config_key,







                  model_id,







                  model_digest,







                  provider,







                  think_setting,







                  result_category,







                  execution_mode,







                  app_name,







                  app_version,







                  observed_model_identity,







                  COUNT(*) AS total_trials,







                  SUM(CASE WHEN outcome_status IN ('passed', 'failed') THEN 1 ELSE 0 END) AS graded_trials,







                  SUM(CASE WHEN verified_success = 1 THEN 1 ELSE 0 END) AS passed_trials,







                  SUM(CASE WHEN outcome_status NOT IN ('passed', 'failed') THEN 1 ELSE 0 END) AS blocked_trials,







                  AVG(CASE WHEN outcome_status IN ('passed', 'failed') THEN total_seconds ELSE NULL END) AS avg_total_seconds,







                  AVG(tool_call_validity) AS avg_tool_validity,







                  AVG(recovery_success) AS avg_recovery_success,







                  SUM(subscription_limit_events) AS subscription_limits







                FROM eval_trials







                GROUP BY config_key, execution_mode







                ORDER BY execution_mode, result_category, model_id, think_setting







                """







            ).fetchall()







            config_comparisons = []







            for r in config_summary_rows:







                graded = int(r["graded_trials"] or 0)







                passed = int(r["passed_trials"] or 0)







                total = int(r["total_trials"] or 0)







                blocked = int(r["blocked_trials"] or 0)







                acc = round(passed / graded, 4) if graded > 0 else None







                op_fail = round(blocked / total, 4) if total > 0 else 0.0







                ci_low, ci_high = _wilson_interval(passed, graded) if graded > 0 else (0.0, 1.0)







                config_comparisons.append(







                    {







                        "config_key": r["config_key"],







                        "model_id": r["model_id"],







                        "model_digest": r["model_digest"],







                        "provider": r["provider"],







                        "think_setting": r["think_setting"],







                        "result_category": r["result_category"],







                        "execution_mode": str(r["execution_mode"]).upper(),







                        "app_name": r["app_name"] or "—",







                        "app_version": r["app_version"] or "unknown",







                        "observed_model_identity": r["observed_model_identity"] or "unknown",







                        "total_trials": total,







                        "graded_trials": graded,







                        "passed_trials": passed,







                        "blocked_trials": blocked,







                        "verified_accuracy": acc,







                        "operational_failure_rate": op_fail,







                        "ci95": [ci_low, ci_high],







                        "avg_total_seconds": round(float(r["avg_total_seconds"] or 0.0), 3),







                        "avg_tool_validity": (







                            round(float(r["avg_tool_validity"]), 3)







                            if r["avg_tool_validity"] is not None







                            else "unknown"







                        ),







                        "avg_recovery_success": (







                            round(float(r["avg_recovery_success"]), 3)







                            if r["avg_recovery_success"] is not None







                            else "unknown"







                        ),







                        "subscription_limits": int(r["subscription_limits"] or 0),







                    }







                )







            # Task-level failures & blockers







            task_failures = [







                dict(r)







                for r in db.execute(







                    """







                    SELECT trial_id, run_id, audit_event_ids_json, task_id, category,







                           config_key, model_id, think_setting, result_category,







                           execution_mode, outcome_status, blocker_reason,







                           grader_summary, total_seconds, created_at







                    FROM eval_trials







                    WHERE outcome_status != 'passed'







                    ORDER BY created_at DESC LIMIT 80







                    """







                ).fetchall()







            ]







            # Tool-path reliability summary







            tool_path_rows = db.execute(







                """







                SELECT







                  result_category,







                  COALESCE(NULLIF(access_method, ''), provider) AS path_id,







                  execution_mode,







                  COUNT(*) AS total_calls,







                  SUM(CASE WHEN verified_success = 1 THEN 1 ELSE 0 END) AS verified_ok,







                  AVG(tool_call_validity) AS mean_tool_validity,







                  AVG(recovery_success) AS mean_recovery_rate,







                  SUM(CASE WHEN outcome_status NOT IN ('passed', 'failed') THEN 1 ELSE 0 END) AS operational_blocks,







                  AVG(total_seconds) AS mean_seconds







                FROM eval_trials







                GROUP BY result_category, path_id, execution_mode







                ORDER BY result_category, path_id







                """







            ).fetchall()







            tool_reliability = [dict(r) for r in tool_path_rows]







        return {







            "inventory": inventory,







            "config_comparisons": config_comparisons,







            "qualifications": qualifications,







            "task_failures": task_failures,







            "tool_reliability": tool_reliability,







            "retrieval_trials": retrieval_trials,







            "feedback_comparisons": feedback_comps,







            "routing_decisions": routing_decisions,







            "recent_trials": recent_trials,







        }

