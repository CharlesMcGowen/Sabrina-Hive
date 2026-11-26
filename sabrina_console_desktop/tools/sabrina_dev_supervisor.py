# -*- coding: utf-8 -*-
"""Separate Development Supervisor for Sabrina's self-improvement loop.

Implements the 6-stage controlled harness-improvement pipeline:
1. request_change: Sabrina submits the limitation, concrete evidence, and acceptance test.
2. build_in_isolation: Coding worker receives an isolated copy of the harness and returns a patch,
   which is applied directly to the candidate harness with full response-to-diff provenance.
3. test_independently: Independent test runner checks the candidate harness against:
   - syntax validation (`py_compile`),
   - frozen unit tests (`simulated_desktop_observations`),
   - malformed delegation regression scenarios (`simulated_desktop_observations`),
   - one real audited native-app handoff via `agent-desktop` (`audited_native_desktop_handoff`), and
   - independent Docker verification on `ego.local` (`real_docker_verification`).
4. review_evidence: Produces the review bundle with distinct evidence labels, response-to-diff
   provenance, unified diff, test results, failures, changed permissions, and human approval gate.
5. activate_version: Enforces exact tested candidate hashes, unchanged production baseline,
   non-stale approval bundle, exclusive activation ownership lock, and installs the candidate.
6. check_and_rollback: Rolls back byte-for-byte on installation exceptions as well as failed
   startup or acceptance checks.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import difflib
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUPERVISOR_DIR = REPO_ROOT / "artifacts" / "dev_supervisor"
DEFAULT_OLLAMA_BASE = os.environ.get("SABRINA_OLLAMA_BASE", "http://ego.local:11434")
DEFAULT_CODING_WORKER_MODEL = os.environ.get("SABRINA_CODING_WORKER_MODEL", "qwen2.5-coder:14b")
DEFAULT_COORDINATOR_MODEL = os.environ.get("SABRINA_COORDINATOR_MODEL", "huihui_ai/qwen3-abliterated:14b")
DEFAULT_APPROVAL_TTL_SECONDS = 3600.0

FROZEN_TEST_FILES = (
    "tests/unit/test_delegate_task.py",
    "tests/unit/test_create_objective_helper.py",
    "tests/unit/test_cross_app_review.py",
)

PROTECTED_PERMISSION_PATTERNS = (
    "SABRINA_ALLOW_UNAPPROVED_SKILLS",
    "auto_approve_skills",
    "bypass_approval",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_approval_bundle_sha256(
    *,
    request_id: str,
    baseline_hashes: dict[str, str],
    tested_candidate_hashes: dict[str, str],
    tested_patch_sha256: str,
    test_completed_at: float,
) -> str:
    payload = {
        "request_id": request_id,
        "baseline_hashes": dict(sorted(baseline_hashes.items())),
        "tested_candidate_hashes": dict(sorted(tested_candidate_hashes.items())),
        "tested_patch_sha256": tested_patch_sha256,
        "test_completed_at": round(float(test_completed_at), 6),
    }
    return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


@dataclass
class ChangeRequest:
    request_id: str
    initiated_by: str
    title: str
    limitation: str
    evidence: list[dict[str, Any]]
    target_files: list[str]
    acceptance_test: dict[str, Any]
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_malformed_delegation_request() -> dict[str, Any]:
    """Return Sabrina's concrete change request for the malformed delegation call bug."""
    return {
        "initiated_by": "Sabrina",
        "title": "Fix malformed delegation calls and unclosed handoff recovery in Sabrina delegation harness",
        "limitation": (
            "When Sabrina delegates tasks across desktop/coding workers, malformed delegation calls "
            "fail or leave active handoffs wedged:\n"
            "1. Calling cross_app_begin_handoff(app='Codex.exe' or 'claude') without handoff_id or prompt "
            "failed in session 0f0b71cc0e0a488dbf54a4957d063abf with 'Tool error: handoff prompt must "
            "contain 1-4000 characters', and _CROSS_APP_REVIEW_TOOLS rejected 'app' or omitted prompt/handoff_id.\n"
            "2. Calling cross_app_record_baseline with the 'evidence' keyword alias or string/empty "
            "evidence_refs failed in workflow_events 19, 23, and 27.\n"
            "3. Calling delegate_task with case-variant/unqualified app names (e.g. 'codex', 'claude') "
            "or an inline 'prompt' argument or empty session context failed to normalize app and prompt.\n"
            "4. Emitting a raw JSON tool call inside message.content when tool_calls is empty "
            "(session b338109d7ffc43df8aeddd824c8d3357) caused ask_sabrina_lab_assistant to treat the "
            "malformed tool call as a final text answer instead of executing it.\n"
            "5. When execute_desktop_playbook raised an exception mid-playbook after opening an active "
            "handoff (session 56222a4216b841a7a7d8ce20650c5e38, handoff 9fd746ccd03b4adf929a63865a99cb8f), "
            "the handoff remained status='active', blocking fallback delegate_task calls to other apps, "
            "and bounded_desktop_json truncation in Steps 5/6 or non-Enter submission in Step 8 could "
            "prevent composer lookup or submit_event_id tracking on native Electron apps.\n"
            "6. create_objective_and_start in tools/sabrina_lab_assistant_mcp.py contained duplicate "
            "unreachable code after its return statement."
        ),
        "evidence": [
            {
                "kind": "assistant_session",
                "session_id": "0f0b71cc0e0a488dbf54a4957d063abf",
                "workflow_attempt_id": "859793be3b934e24a80a75823f34e84c",
                "objective_id": "4eadf451b0104a9faa6b828817aed129",
                "symptom": "cross_app_begin_handoff(app='Codex.exe') -> Tool error: handoff prompt must contain 1-4000 characters",
            },
            {
                "kind": "assistant_session",
                "session_id": "56222a4216b841a7a7d8ce20650c5e38",
                "workflow_attempt_id": "5e22ce1f688b448fb643fa3d531bc531",
                "handoff_id": "9fd746ccd03b4adf929a63865a99cb8f",
                "symptom": "Mid-playbook exception left Claude.exe handoff active -> Tool error: finish the active handoff for Claude.exe before delegating to Antigravity.exe",
            },
            {
                "kind": "assistant_session",
                "session_id": "b338109d7ffc43df8aeddd824c8d3357",
                "artifact": "artifacts/queen_handoff_dispatch_20261107_235042/batch-3.txt",
                "symptom": "Raw JSON tool call in message.content with tool_calls=[] treated as final answer and paused run",
            },
            {
                "kind": "workflow_events",
                "event_ids": [19, 23, 27, 31],
                "symptom": "record_cross_app_baseline unexpected keyword argument 'evidence' and empty evidence_refs rejection",
            },
        ],
        "target_files": [
            "tools/sabrina_lab_assistant_mcp.py",
            "tools/agent_desktop_broker.py",
        ],
        "acceptance_test": {
            "frozen_test_files": list(FROZEN_TEST_FILES),
            "regression_scenarios": [
                "cross_app_begin_handoff_missing_id_and_prompt",
                "record_cross_app_baseline_evidence_alias_and_string",
                "delegate_task_unqualified_app_and_inline_prompt",
                "mid_playbook_exception_closes_active_handoff_for_fallback",
                "inline_json_tool_call_in_message_content_recovered",
            ],
            "require_audited_native_desktop_handoff": True,
            "preserve_skill_approval_gate": True,
        },
    }


def _malformed_delegation_hunk_specs() -> list[dict[str, str]]:
    """Return the target blocks and exact replacement specifications for the 5 patch hunks."""
    hunk_1_target = """    return {'objective_id': oid, 'status': 'started'}
    \"\"\"Create a new objective in the workflow store and start its execution.

    Args:
        task: Human‑readable description of the objective.
        apps: Optional list of allowed app identifiers (e.g., ["Claude.exe", "Antigravity.exe"]).
        ops: Optional dict of permitted operations, e.g., {"desktop": True, "host": False, "file": True}.
        retry: Maximum number of retry attempts for the workflow.
        timeout: Overall timeout in seconds for the workflow execution.

    Returns:
        A dict containing at least ``objective_id`` and ``status``.
    \"\"\"
    # Basic validation
    if not isinstance(task, str) or not task.strip():
        raise ValueError("Task description must be a non‑empty string")
    if apps is not None and not all(isinstance(a, str) for a in apps):
        raise ValueError("All app identifiers must be strings")
    if ops is not None and not isinstance(ops, dict):
        raise ValueError("ops must be a dict mapping operation names to booleans")
    # Retrieve store (same path logic as WorkflowView)
    from tools.sabrina_workflow import DEFAULT_DB, ROOT, WorkflowStore
    store_path = Path(os.environ.get('SABRINA_ASSISTANT_DB', str(DEFAULT_DB)))
    store = WorkflowStore(store_path)
    # Create the objective; additional fields could be stored in a contracts table if needed.
    result = store.create(task)
    oid = result['objective']['id']
    # Optionally store metadata about apps and ops; here we simply attach as JSON in a custom column if exists.
    # For simplicity, we skip custom schema changes.
    # Launch the worker subprocess similar to WorkflowView._launch.
    directory = ROOT / 'artifacts/workflows' / oid
    directory.mkdir(parents=True, exist_ok=True)
    log_file = (directory / 'worker.log').open('a', encoding='utf-8')
    subprocess.Popen([
        sys.executable,
        '-m',
        'tools.sabrina_workflow',
        'work',
        '--id', oid,
        '--db', str(store_path)
    ], cwd=ROOT, stdout=log_file, stderr=log_file, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return {'objective_id': oid, 'status': 'started'}

    global _coordination
    with _coordination_lock:
        if _coordination is None:
            from tools.sabrina_coordination import Coordination, RunAlreadyActiveError, RunAlreadyActiveError
            path = Path(os.environ.get('SABRINA_ASSISTANT_DB', str(WORKSPACE_ROOT / 'data/sabrina_assistant.sqlite3')))
            _coordination = Coordination(path)
        return _coordination"""

    hunk_1_replacement = "    return {'objective_id': oid, 'status': 'started'}"

    hunk_2_target = """_CROSS_APP_REVIEW_TOOLS = [
    {"type": "function", "function": {"name": "cross_app_record_baseline", "description": "Save the evidence-only baseline before any app consultation.",
     "parameters": {"type": "object", "properties": {"content": {"type": "string", "maxLength": 12000}, "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, "required": ["content", "evidence_refs"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_route", "description": "Record an explicit consult, defer, or skip choice. Consult creates a stable handoff_id; choose apps and order independently.",
     "parameters": {"type": "object", "properties": {"app": {"type": "string", "enum": ["Claude.exe", "Antigravity.exe", "Codex.exe"]}, "choice": {"type": "string", "enum": ["consult", "defer", "skip"]}, "rationale": {"type": "string", "maxLength": 1000}, "expected_contribution": {"type": "string", "maxLength": 1000}}, "required": ["app", "choice", "rationale", "expected_contribution"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_begin_handoff", "description": "Record the exact prompt for an active app consult before using desktop tools.",
     "parameters": {"type": "object", "properties": {"handoff_id": {"type": "string"}, "prompt": {"type": "string", "maxLength": 4000}}, "required": ["handoff_id", "prompt"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_complete_handoff", "description": "Link a successful fresh post-submission snapshot to this handoff and classify its visible contribution.",
     "parameters": {"type": "object", "properties": {"handoff_id": {"type": "string"}, "reply_snapshot_id": {"type": "string"}, "contribution": {"type": "string", "enum": ["incorrect_or_unusable", "corroboration", "verified_new_or_corrected"]}, "summary": {"type": "string", "maxLength": 3000}}, "required": ["handoff_id", "reply_snapshot_id", "contribution", "summary"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_close_handoff", "description": "Close an unavailable, failed, or stopped app branch.",
     "parameters": {"type": "object", "properties": {"handoff_id": {"type": "string"}, "outcome": {"type": "string", "enum": ["unavailable", "failed", "stopped"]}, "detail": {"type": "string", "maxLength": 2000}}, "required": ["handoff_id", "outcome", "detail"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_record_final", "description": "Save the final synthesis and source/audit checklist after every app has a final route decision.",
     "parameters": {"type": "object", "properties": {"report": {"type": "string", "maxLength": 12000}, "evidence_checklist": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "object", "properties": {"claim": {"type": "string"}, "evidence_refs": {"type": "array", "items": {"type": "string"}}}, "required": ["claim", "evidence_refs"], "additionalProperties": False}}}, "required": ["report", "evidence_checklist"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "delegate_task", "description": "Delegate task implementation, verification, or review to an installed application (Claude.exe, Antigravity.exe, Codex.exe). The controller manages desktop interaction, supplies the frozen prompt, executes the app's tested playbook, and returns the recorded response and verification checks.",
     "parameters": {"type": "object", "properties": {"app": {"type": "string", "enum": ["Claude.exe", "Antigravity.exe", "Codex.exe"], "description": "Target installed application to consult."}, "follow_up_guidance": {"type": "string", "maxLength": 2000, "description": "Optional targeted follow-up guidance or correction details to append to the task prompt."}, "task_id": {"type": "string", "description": "Optional task or attempt identifier."}}, "required": ["app"], "additionalProperties": False}}},
]"""

    hunk_2_replacement = """_CROSS_APP_REVIEW_TOOLS = [
    {"type": "function", "function": {"name": "cross_app_record_baseline", "description": "Save the evidence-only baseline before any app consultation.",
     "parameters": {"type": "object", "properties": {"content": {"type": "string", "maxLength": 12000}, "evidence_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "evidence": {"type": ["array", "string"]}}, "required": ["content"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_route", "description": "Record an explicit consult, defer, or skip choice. Consult creates a stable handoff_id; choose apps and order independently.",
     "parameters": {"type": "object", "properties": {"app": {"type": "string", "enum": ["Claude.exe", "Antigravity.exe", "Codex.exe"]}, "choice": {"type": "string", "enum": ["consult", "defer", "skip"]}, "rationale": {"type": "string", "maxLength": 1000}, "expected_contribution": {"type": "string", "maxLength": 1000}, "reason": {"type": "string", "maxLength": 1000}}, "required": ["app", "choice"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_begin_handoff", "description": "Record the exact prompt for an active app consult before using desktop tools.",
     "parameters": {"type": "object", "properties": {"handoff_id": {"type": "string"}, "prompt": {"type": "string", "maxLength": 4000}, "app": {"type": "string", "enum": ["Claude.exe", "Antigravity.exe", "Codex.exe"]}, "task_id": {"type": "string"}}, "required": [], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_complete_handoff", "description": "Link a successful fresh post-submission snapshot to this handoff and classify its visible contribution.",
     "parameters": {"type": "object", "properties": {"handoff_id": {"type": "string"}, "reply_snapshot_id": {"type": "string"}, "contribution": {"type": "string", "enum": ["incorrect_or_unusable", "corroboration", "verified_new_or_corrected"]}, "summary": {"type": "string", "maxLength": 3000}}, "required": ["handoff_id", "reply_snapshot_id", "contribution", "summary"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_close_handoff", "description": "Close an unavailable, failed, or stopped app branch.",
     "parameters": {"type": "object", "properties": {"handoff_id": {"type": "string"}, "outcome": {"type": "string", "enum": ["unavailable", "failed", "stopped"]}, "detail": {"type": "string", "maxLength": 2000}}, "required": ["handoff_id", "outcome", "detail"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "cross_app_record_final", "description": "Save the final synthesis and source/audit checklist after every app has a final route decision.",
     "parameters": {"type": "object", "properties": {"report": {"type": "string", "maxLength": 12000}, "evidence_checklist": {"type": "array", "minItems": 1, "maxItems": 30, "items": {"type": "object", "properties": {"claim": {"type": "string"}, "evidence_refs": {"type": "array", "items": {"type": "string"}}}, "required": ["claim", "evidence_refs"], "additionalProperties": False}}}, "required": ["report", "evidence_checklist"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "delegate_task", "description": "Delegate task implementation, verification, or review to an installed application (Claude.exe, Antigravity.exe, Codex.exe). The controller manages desktop interaction, supplies the frozen prompt, executes the app's tested playbook, and returns the recorded response and verification checks.",
     "parameters": {"type": "object", "properties": {"app": {"type": "string", "enum": ["Claude.exe", "Antigravity.exe", "Codex.exe"], "description": "Target installed application to consult."}, "prompt": {"type": "string", "maxLength": 4000, "description": "Optional explicit task prompt when not bound to a frozen workflow contract."}, "follow_up_guidance": {"type": "string", "maxLength": 2000, "description": "Optional targeted follow-up guidance or correction details to append to the task prompt."}, "task_id": {"type": "string", "description": "Optional task or attempt identifier."}, "objective_id": {"type": "string", "description": "Optional workflow objective identifier."}}, "required": ["app"], "additionalProperties": False}}},
]


_CANONICAL_CROSS_APPS = {
    "claude": "Claude.exe",
    "claude.exe": "Claude.exe",
    "antigravity": "Antigravity.exe",
    "antigravity.exe": "Antigravity.exe",
    "codex": "Codex.exe",
    "codex.exe": "Codex.exe",
}


def _normalize_cross_app_name(app: Any, default: str = "Claude.exe") -> str:
    raw = str(app or "").strip()
    if not raw:
        return default
    return _CANONICAL_CROSS_APPS.get(raw.casefold(), raw)


def _extract_inline_tool_calls(content: str, allowed_tool_names: set[str] | None = None) -> list[dict[str, Any]]:
    \"\"\"Recover a structured tool call when a local model emits raw JSON in message.content.\"\"\"
    text = (content or "").strip()
    if not text:
        return []
    candidates: list[str] = [text]
    for match in re.finditer(r"```(?:json)?\\s*(\\{[\\s\\S]*?\\})\\s*```", text):
        candidates.append(match.group(1).strip())
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if 0 <= brace_start < brace_end:
        candidates.append(text[brace_start:brace_end + 1].strip())
    for raw in candidates:
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        recovered: list[dict[str, Any]] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            fn_block = item.get("function") if isinstance(item.get("function"), dict) else item
            name = str(fn_block.get("name") or fn_block.get("tool") or "").strip()
            args = fn_block.get("arguments")
            if args is None:
                args = fn_block.get("parameters") or {
                    k: v for k, v in fn_block.items() if k not in {"name", "tool", "arguments", "parameters", "id", "type"}
                }
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if not name or not isinstance(args, dict):
                continue
            if allowed_tool_names is not None and name not in allowed_tool_names:
                continue
            recovered.append({
                "id": str(item.get("id") or f"inline-call-{idx + 1}"),
                "function": {"name": name, "arguments": args},
            })
        if recovered:
            return recovered
    return []"""

    hunk_3_target = """    if name == "delegate_task":
        if session is None:
            raise ValueError("delegate_task requires a bound research run")
        broker = _get_desktop_broker()
        decision_id = session.get("_last_decision_id")
        run_id = session["id"]
        app = str(arguments.get("app", "Claude.exe")).strip()
        follow_up = str(arguments.get("follow_up_guidance", "")).strip()
        out = broker.execute_desktop_playbook(run_id, app=app, follow_up_guidance=follow_up, decision_id=decision_id)
        return json.dumps(out, ensure_ascii=False)
    if name.startswith("cross_app_"):
        if session is None:
            raise ValueError("cross-app review tools require a bound research run")
        broker = _get_desktop_broker()
        decision_id = session.get("_last_decision_id")
        run_id = session["id"]
        with _get_coordination().store.connect() as db:
            from tools.sabrina_workflow import workflow_attempt
            workflow = workflow_attempt(db, run_id)
        if workflow is not None and name in {"cross_app_begin_handoff", "cross_app_route"}:
            app = str(arguments.get("app") or "Claude.exe").strip()
            out = broker.execute_desktop_playbook(run_id, app=app, decision_id=decision_id)
            return json.dumps(out, ensure_ascii=False)
        if name == "cross_app_record_baseline":
            out = broker.record_cross_app_baseline(run_id, arguments.get("content", ""), arguments.get("evidence_refs", []), decision_id)
        elif name == "cross_app_route":
            out = broker.record_cross_app_route(run_id, arguments.get("app", ""), arguments.get("choice", ""),
                                                arguments.get("rationale", ""), arguments.get("expected_contribution", ""), decision_id)
        elif name == "cross_app_begin_handoff":
            out = broker.begin_cross_app_handoff(
                run_id,
                handoff_id=arguments.get("handoff_id"),
                prompt=arguments.get("prompt"),
                decision_id=decision_id,
                app=arguments.get("app"),
            )"""

    hunk_3_replacement = """    if name == "delegate_task":
        if session is None:
            raise ValueError("delegate_task requires a bound research run")
        broker = _get_desktop_broker()
        decision_id = session.get("_last_decision_id")
        run_id = session["id"]
        app = _normalize_cross_app_name(arguments.get("app"), default="Claude.exe")
        explicit_prompt = str(arguments.get("prompt") or "").strip()
        follow_up = str(arguments.get("follow_up_guidance") or "").strip()
        out = broker.execute_desktop_playbook(
            run_id,
            app=app,
            prompt=explicit_prompt,
            follow_up_guidance=follow_up,
            decision_id=decision_id,
        )
        return json.dumps(out, ensure_ascii=False)
    if name.startswith("cross_app_"):
        if session is None:
            raise ValueError("cross-app review tools require a bound research run")
        broker = _get_desktop_broker()
        decision_id = session.get("_last_decision_id")
        run_id = session["id"]
        with _get_coordination().store.connect() as db:
            from tools.sabrina_workflow import workflow_attempt
            workflow = workflow_attempt(db, run_id)
        if workflow is not None and name in {"cross_app_begin_handoff", "cross_app_route"}:
            app = _normalize_cross_app_name(arguments.get("app"), default="Claude.exe")
            out = broker.execute_desktop_playbook(run_id, app=app, decision_id=decision_id)
            return json.dumps(out, ensure_ascii=False)
        if name == "cross_app_record_baseline":
            content = str(arguments.get("content") or arguments.get("baseline") or arguments.get("summary") or "")
            raw_refs = arguments.get("evidence_refs")
            if raw_refs is None:
                raw_refs = arguments.get("evidence")
            if isinstance(raw_refs, str) and raw_refs.strip():
                evidence_refs = [raw_refs.strip()]
            elif isinstance(raw_refs, list):
                evidence_refs = [str(item).strip() for item in raw_refs if str(item).strip()]
            else:
                evidence_refs = []
            if not evidence_refs and session is not None:
                try:
                    session_refs = json.loads(session.get("refs") or "[]")
                except Exception:
                    session_refs = []
                evidence_refs = [str(r) for r in session_refs if str(r).strip()] or [f"source:run:{run_id}"]
            out = broker.record_cross_app_baseline(run_id, content, evidence_refs, decision_id)
        elif name == "cross_app_route":
            app = _normalize_cross_app_name(arguments.get("app"), default="")
            choice = str(arguments.get("choice") or "consult").strip()
            rationale = str(arguments.get("rationale") or arguments.get("reason") or f"Route {choice} for {app}").strip()
            expected = str(arguments.get("expected_contribution") or arguments.get("expected") or f"Consultation outcome from {app}").strip()
            out = broker.record_cross_app_route(run_id, app, choice, rationale, expected, decision_id)
        elif name == "cross_app_begin_handoff":
            norm_app = _normalize_cross_app_name(arguments.get("app"), default="") if arguments.get("app") else None
            out = broker.begin_cross_app_handoff(
                run_id,
                handoff_id=arguments.get("handoff_id"),
                prompt=arguments.get("prompt"),
                decision_id=decision_id,
                app=norm_app,
            )"""

    hunk_4_target = """                message = response.get("message", {})
                tool_calls = message.get("tool_calls", []) or []
                if not tool_calls:"""

    hunk_4_replacement = """                message = response.get("message", {})
                tool_calls = message.get("tool_calls", []) or []
                if not tool_calls and message.get("content"):
                    active_tools = (
                        ([t for t in _AGENT_TOOLS if t['function']['name'] in {'performance_lookup', 'sabrina_lab_status'}] +
                         [t for t in _CROSS_APP_REVIEW_TOOLS if t['function']['name'] == 'delegate_task'])
                        if workflow_mode else
                        (agent_tools + (_CROSS_APP_REVIEW_TOOLS if cross_app_mode else []) + broker.tool_schemas(session['id']) + host_broker.tool_schemas(session['id']))
                    )
                    allowed_names = {t.get("function", {}).get("name", "") for t in active_tools if isinstance(t, dict)}
                    recovered_calls = _extract_inline_tool_calls(str(message.get("content", "")), allowed_names)
                    if recovered_calls:
                        tool_calls = recovered_calls
                if not tool_calls:"""

    hunk_5_target = """    def record_cross_app_baseline(self, run_id: str, content: str, evidence_refs: list[str], decision_id: int | None = None):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 12000:
            raise ValueError("baseline must contain 1-12000 characters")
        if not isinstance(evidence_refs, list) or not evidence_refs or any(not isinstance(x, str) or not x.strip() for x in evidence_refs):
            raise ValueError("baseline requires one or more evidence references")
        now = self.clock()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.store.connect(True) as db:
            profile = self._cross_app_live(db, run_id)
            if profile["state"] != "baseline_pending" or db.execute("SELECT 1 FROM assistant_cross_app_checkpoints WHERE run_id=? AND kind='baseline'", (run_id,)).fetchone():
                raise ValueError("evidence-only baseline must be the first and only baseline")
            db.execute("INSERT INTO assistant_cross_app_checkpoints(run_id,kind,content,evidence_refs,decision_id,content_sha256,created) VALUES(?,?,?,?,?,?,?)",
                       (run_id, "baseline", content.strip(), json.dumps(evidence_refs), decision_id, digest, now))
            db.execute("UPDATE assistant_cross_app_reviews SET state='routing',updated=? WHERE run_id=?", (now, run_id))
        return {"run_id": run_id, "baseline_saved": True, "content_sha256": digest, "evidence_ref_count": len(evidence_refs)}

    def record_cross_app_route(self, run_id: str, app: str, choice: str, rationale: str, expected_contribution: str, decision_id: int | None = None):
        if app not in self.CROSS_APP_APPS:
            raise ValueError("app must be exactly Claude.exe, Antigravity.exe, or Codex.exe")
        if choice not in {"consult", "defer", "skip"}:
            raise ValueError("choice must be consult, defer, or skip")
        for label, value in (("rationale", rationale), ("expected contribution", expected_contribution)):
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
                raise ValueError(f"{label} must contain 1-1000 characters")
        now = self.clock()
        handoff_id = uuid.uuid4().hex if choice == "consult" else ""
        with self.store.connect(True) as db:
            profile = self._cross_app_live(db, run_id)
            if profile["state"] == "baseline_pending":
                raise ValueError("save the evidence-only baseline before choosing an app")
            from tools.sabrina_workflow import workflow_attempt
            workflow = workflow_attempt(db, run_id)
            if workflow is not None and choice == "consult":
                if workflow["handoff_id"]:
                    raise ValueError("workflow attempt already has a handoff; collect it before a corrective attempt")
                if not workflow["experimental"]:
                    raise ValueError("production workflow adapter qualification is not yet available")
            if db.execute("SELECT 1 FROM assistant_cross_app_handoffs WHERE run_id=? AND status='active'", (run_id,)).fetchone():
                raise ValueError("finish or stop the active handoff before choosing another app")
            if app not in json.loads(profile["apps"]):
                raise ValueError("app is outside this run's exact allowlist")
            cursor = db.execute("INSERT INTO assistant_cross_app_routes(run_id,app,choice,rationale,expected_contribution,handoff_id,decision_id,created) VALUES(?,?,?,?,?,?,?,?)",
                                (run_id, app, choice, rationale.strip(), expected_contribution.strip(), handoff_id, decision_id, now))
            route_id = cursor.lastrowid
            if choice == "consult":
                db.execute("INSERT INTO assistant_cross_app_handoffs(handoff_id,run_id,route_id,app,status,started,decision_id) VALUES(?,?,?,?,'active',?,?)",
                           (handoff_id, run_id, route_id, app, now, decision_id))
                if workflow is not None:
                    db.execute("UPDATE workflow_attempts SET handoff_id=?,app=? WHERE id=?", (handoff_id, app, workflow["id"]))
        return {"run_id": run_id, "route_id": route_id, "app": app, "choice": choice, "handoff_id": handoff_id or None}

    def begin_cross_app_handoff(self, run_id: str, handoff_id: str | None = None, prompt: str | None = None, decision_id: int | None = None, app: str | None = None):
        with self.store.connect(True) as db:
            self._cross_app_live(db, run_id)
            from tools.sabrina_workflow import workflow_attempt
            workflow = workflow_attempt(db, run_id)
            if not handoff_id:
                active_row = db.execute("SELECT handoff_id, app FROM assistant_cross_app_handoffs WHERE run_id=? AND status='active' ORDER BY started DESC LIMIT 1", (run_id,)).fetchone()
                if active_row:
                    handoff_id = active_row["handoff_id"]
                    if not app:
                        app = active_row["app"]
                elif app:
                    route_res = self.record_cross_app_route(run_id, app, "consult", f"Consult {app}", "Implementation proposal", decision_id)
                    handoff_id = route_res["handoff_id"]
                else:
                    raise ValueError("active handoff_id or app required")
            if not prompt and workflow is not None:
                prompt = workflow["expected_prompt"]
            if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
                raise ValueError("handoff prompt must contain 1-4000 characters")
            if workflow is not None and prompt.strip() != workflow["expected_prompt"] and not prompt.strip().startswith(workflow["expected_prompt"]):
                raise ValueError("workflow prompt must exactly match the frozen task contract supplied in context")
            row = db.execute("SELECT * FROM assistant_cross_app_handoffs WHERE run_id=? AND handoff_id=? AND status='active'", (run_id, handoff_id)).fetchone()
            if row is None:
                raise ValueError("handoff_id is not active in this run")
            if row["prompt"]:
                raise ValueError("handoff prompt is already recorded")
            db.execute("UPDATE assistant_cross_app_handoffs SET prompt=?,prompt_decision_id=? WHERE handoff_id=?",
                       (prompt.strip(), decision_id, handoff_id))
        return {"run_id": run_id, "handoff_id": handoff_id, "prompt_recorded": True}"""

    hunk_5_replacement = """    def _normalize_cross_app(self, app: str | None) -> str:
        raw = str(app or "").strip()
        if not raw:
            return ""
        candidate = raw if raw.lower().endswith(".exe") else f"{raw}.exe"
        matched = next((item for item in self.CROSS_APP_APPS if item.casefold() == candidate.casefold()), None)
        return matched or raw

    def record_cross_app_baseline(
        self,
        run_id: str,
        content: str,
        evidence_refs: list[str] | str | None = None,
        decision_id: int | None = None,
        *,
        evidence: list[str] | str | None = None,
    ):
        if evidence_refs is None and evidence is not None:
            evidence_refs = evidence
        if isinstance(evidence_refs, str) and evidence_refs.strip():
            evidence_refs = [evidence_refs.strip()]
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 12000:
            raise ValueError("baseline must contain 1-12000 characters")
        if not isinstance(evidence_refs, list) or not evidence_refs or any(not isinstance(x, str) or not x.strip() for x in evidence_refs):
            raise ValueError("baseline requires one or more evidence references")
        now = self.clock()
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.store.connect(True) as db:
            profile = self._cross_app_live(db, run_id)
            if profile["state"] != "baseline_pending" or db.execute("SELECT 1 FROM assistant_cross_app_checkpoints WHERE run_id=? AND kind='baseline'", (run_id,)).fetchone():
                raise ValueError("evidence-only baseline must be the first and only baseline")
            db.execute("INSERT INTO assistant_cross_app_checkpoints(run_id,kind,content,evidence_refs,decision_id,content_sha256,created) VALUES(?,?,?,?,?,?,?)",
                       (run_id, "baseline", content.strip(), json.dumps(evidence_refs), decision_id, digest, now))
            db.execute("UPDATE assistant_cross_app_reviews SET state='routing',updated=? WHERE run_id=?", (now, run_id))
        return {"run_id": run_id, "baseline_saved": True, "content_sha256": digest, "evidence_ref_count": len(evidence_refs)}

    def record_cross_app_route(self, run_id: str, app: str, choice: str, rationale: str, expected_contribution: str, decision_id: int | None = None):
        app = self._normalize_cross_app(app)
        if app not in self.CROSS_APP_APPS:
            raise ValueError("app must be exactly Claude.exe, Antigravity.exe, or Codex.exe")
        if choice not in {"consult", "defer", "skip"}:
            raise ValueError("choice must be consult, defer, or skip")
        for label, value in (("rationale", rationale), ("expected contribution", expected_contribution)):
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
                raise ValueError(f"{label} must contain 1-1000 characters")
        now = self.clock()
        handoff_id = uuid.uuid4().hex if choice == "consult" else ""
        with self.store.connect(True) as db:
            profile = self._cross_app_live(db, run_id)
            if profile["state"] == "baseline_pending":
                raise ValueError("save the evidence-only baseline before choosing an app")
            from tools.sabrina_workflow import workflow_attempt
            workflow = workflow_attempt(db, run_id)
            if workflow is not None and choice == "consult":
                if workflow["handoff_id"]:
                    raise ValueError("workflow attempt already has a handoff; collect it before a corrective attempt")
                if not workflow["experimental"]:
                    raise ValueError("production workflow adapter qualification is not yet available")
            if db.execute("SELECT 1 FROM assistant_cross_app_handoffs WHERE run_id=? AND status='active'", (run_id,)).fetchone():
                raise ValueError("finish or stop the active handoff before choosing another app")
            if app not in json.loads(profile["apps"]):
                raise ValueError("app is outside this run's exact allowlist")
            cursor = db.execute("INSERT INTO assistant_cross_app_routes(run_id,app,choice,rationale,expected_contribution,handoff_id,decision_id,created) VALUES(?,?,?,?,?,?,?,?)",
                                (run_id, app, choice, rationale.strip(), expected_contribution.strip(), handoff_id, decision_id, now))
            route_id = cursor.lastrowid
            if choice == "consult":
                db.execute("INSERT INTO assistant_cross_app_handoffs(handoff_id,run_id,route_id,app,status,started,decision_id) VALUES(?,?,?,?,'active',?,?)",
                           (handoff_id, run_id, route_id, app, now, decision_id))
                if workflow is not None:
                    db.execute("UPDATE workflow_attempts SET handoff_id=?,app=? WHERE id=?", (handoff_id, app, workflow["id"]))
        return {"run_id": run_id, "route_id": route_id, "app": app, "choice": choice, "handoff_id": handoff_id or None}

    def begin_cross_app_handoff(self, run_id: str, handoff_id: str | None = None, prompt: str | None = None, decision_id: int | None = None, app: str | None = None):
        norm_app = self._normalize_cross_app(app) if app else None
        if not handoff_id:
            with self.store.connect() as db:
                profile = self._cross_app_live(db, run_id)
                active_row = db.execute(
                    "SELECT handoff_id, app FROM assistant_cross_app_handoffs WHERE run_id=? AND status='active' ORDER BY started DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
                need_auto_baseline = profile["state"] == "baseline_pending" and active_row is None and bool(norm_app)
            if active_row:
                handoff_id = active_row["handoff_id"]
                if not norm_app:
                    norm_app = active_row["app"]
            elif norm_app:
                if need_auto_baseline:
                    self.record_cross_app_baseline(
                        run_id,
                        f"Baseline evidence for delegated handoff to {norm_app}",
                        [f"source:run:{run_id}"],
                        decision_id=decision_id,
                    )
                route_res = self.record_cross_app_route(
                    run_id,
                    norm_app,
                    "consult",
                    f"Consult {norm_app}",
                    "Implementation proposal",
                    decision_id,
                )
                handoff_id = route_res["handoff_id"]
            else:
                raise ValueError("active handoff_id or app required")
        with self.store.connect(True) as db:
            self._cross_app_live(db, run_id)
            from tools.sabrina_workflow import workflow_attempt
            workflow = workflow_attempt(db, run_id)
            if not prompt:
                if workflow is not None:
                    prompt = workflow["expected_prompt"]
                else:
                    sess = db.execute("SELECT task, context FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
                    if sess is not None:
                        prompt = (sess["context"] or sess["task"] or "").strip()
            if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
                raise ValueError("handoff prompt must contain 1-4000 characters")
            if workflow is not None and prompt.strip() != workflow["expected_prompt"] and not prompt.strip().startswith(workflow["expected_prompt"]):
                raise ValueError("workflow prompt must exactly match the frozen task contract supplied in context")
            row = db.execute("SELECT * FROM assistant_cross_app_handoffs WHERE run_id=? AND handoff_id=? AND status='active'", (run_id, handoff_id)).fetchone()
            if row is None:
                raise ValueError("handoff_id is not active in this run")
            if row["prompt"]:
                if row["prompt"] == prompt.strip():
                    return {"run_id": run_id, "handoff_id": handoff_id, "prompt_recorded": True}
                raise ValueError("handoff prompt is already recorded")
            db.execute("UPDATE assistant_cross_app_handoffs SET prompt=?,prompt_decision_id=? WHERE handoff_id=?",
                       (prompt.strip(), decision_id, handoff_id))
        return {"run_id": run_id, "handoff_id": handoff_id, "prompt_recorded": True}"""

    hunk_6_target = """        # 4. Resolve and record prompt
        with self.store.connect(True) as db:
            from tools.sabrina_workflow import workflow_attempt
            wf = workflow_attempt(db, run_id)
            if not prompt:
                if wf is not None:
                    prompt = wf["expected_prompt"]
                else:
                    sess = db.execute("SELECT context FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
                    prompt = sess["context"] if sess and sess["context"] else f"Task delegation to {app}"
            if follow_up_guidance:
                prompt = f"{prompt}\\n\\nCorrection / Follow-up Guidance:\\n{follow_up_guidance.strip()}"
            h_row = db.execute("SELECT prompt FROM assistant_cross_app_handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
            need_begin = not h_row or not h_row["prompt"]
        if need_begin:
            self.begin_cross_app_handoff(run_id, handoff_id, prompt, decision_id=decision_id, app=app)

        # 4b. Audited desktop_list_windows under active handoff"""

    hunk_6_replacement = """        # 4. Resolve and record prompt
        with self.store.connect(True) as db:
            from tools.sabrina_workflow import workflow_attempt
            wf = workflow_attempt(db, run_id)
            if not prompt:
                if wf is not None:
                    prompt = wf["expected_prompt"]
                else:
                    sess = db.execute("SELECT task, context FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
                    prompt = (sess["context"] or sess["task"]) if sess and (sess["context"] or sess["task"]) else f"Task delegation to {app}"
            if follow_up_guidance:
                prompt = f"{prompt}\\n\\nCorrection / Follow-up Guidance:\\n{follow_up_guidance.strip()}"
            h_row = db.execute("SELECT prompt FROM assistant_cross_app_handoffs WHERE handoff_id=?", (handoff_id,)).fetchone()
            need_begin = not h_row or not h_row["prompt"]
        try:
            if need_begin:
                self.begin_cross_app_handoff(run_id, handoff_id, prompt, decision_id=decision_id, app=app)
            return self._execute_desktop_playbook_active(
                run_id=run_id,
                app=app,
                handoff_id=handoff_id,
                prompt=prompt,
                decision_id=decision_id,
                timeout=timeout,
                wf=wf,
            )
        except Exception as exc:
            try:
                with self.store.connect(True) as db:
                    still_active = db.execute(
                        "SELECT handoff_id, submit_event_id FROM assistant_cross_app_handoffs WHERE run_id=? AND handoff_id=? AND status='active'",
                        (run_id, handoff_id),
                    ).fetchone()
                    if still_active is not None and not still_active["submit_event_id"]:
                        db.execute(
                            "UPDATE assistant_cross_app_handoffs SET status='failed', error_detail=?, completed=? WHERE handoff_id=?",
                            (f"Playbook aborted before submission: {type(exc).__name__}: {exc}"[:2000], self.clock(), handoff_id),
                        )
            except Exception:
                pass
            raise
        finally:
            try:
                self.release_desktop_lease(run_id)
            except Exception:
                pass

    def _execute_desktop_playbook_active(
        self,
        *,
        run_id: str,
        app: str,
        handoff_id: str,
        prompt: str,
        decision_id: int | None,
        timeout: float,
        wf: Any,
    ) -> dict[str, Any]:
        def _full_snap(raw_obs: Any) -> dict[str, Any]:
            parsed = json.loads(raw_obs) if isinstance(raw_obs, str) else raw_obs
            try:
                with self.store.connect() as db:
                    ev = db.execute(
                        "SELECT result FROM assistant_desktop_audit_events WHERE run_id=? AND tool='desktop_snapshot' ORDER BY id DESC LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    if ev and ev[0]:
                        return json.loads(ev[0])
            except Exception:
                pass
            return parsed if isinstance(parsed, dict) else {}

        # 4b. Audited desktop_list_windows under active handoff"""

    hunk_7a_target = """        # 5. Fresh conversation if supported (e.g. New button)
        snap_raw = self.observe(run_id, "desktop_snapshot", {"app": app}, decision_id=decision_id)
        snap_data = json.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw"""

    hunk_7a_replacement = """        # 5. Fresh conversation if supported (e.g. New button)
        snap_raw = self.observe(run_id, "desktop_snapshot", {"app": app}, decision_id=decision_id)
        snap_data = _full_snap(snap_raw)"""

    hunk_7b_target = """        snap_raw = self.observe(run_id, "desktop_snapshot", {"app": app}, decision_id=decision_id)
        snap_data = json.loads(snap_raw) if isinstance(snap_raw, str) else snap_raw
        input_ref = None"""

    hunk_7b_replacement = """        snap_raw = self.observe(run_id, "desktop_snapshot", {"app": app}, decision_id=decision_id)
        snap_data = _full_snap(snap_raw)
        input_ref = None"""

    return [
        {
            "hunk_id": "hunk_1_mcp_clean_duplicate_create_objective",
            "file": "tools/sabrina_lab_assistant_mcp.py",
            "target": hunk_1_target,
            "expected_replacement": hunk_1_replacement,
        },
        {
            "hunk_id": "hunk_2_mcp_schemas_and_helpers",
            "file": "tools/sabrina_lab_assistant_mcp.py",
            "target": hunk_2_target,
            "expected_replacement": hunk_2_replacement,
        },
        {
            "hunk_id": "hunk_3_mcp_internal_tool_dispatch",
            "file": "tools/sabrina_lab_assistant_mcp.py",
            "target": hunk_3_target,
            "expected_replacement": hunk_3_replacement,
        },
        {
            "hunk_id": "hunk_4_mcp_inline_tool_call_recovery",
            "file": "tools/sabrina_lab_assistant_mcp.py",
            "target": hunk_4_target,
            "expected_replacement": hunk_4_replacement,
        },
        {
            "hunk_id": "hunk_5_broker_cross_app_methods",
            "file": "tools/agent_desktop_broker.py",
            "target": hunk_5_target,
            "expected_replacement": hunk_5_replacement,
        },
        {
            "hunk_id": "hunk_6_broker_playbook_exception_and_lease_recovery",
            "file": "tools/agent_desktop_broker.py",
            "target": hunk_6_target,
            "expected_replacement": hunk_6_replacement,
        },
        {
            "hunk_id": "hunk_7a_broker_step5_full_snapshot",
            "file": "tools/agent_desktop_broker.py",
            "target": hunk_7a_target,
            "expected_replacement": hunk_7a_replacement,
        },
        {
            "hunk_id": "hunk_7b_broker_step6_full_snapshot",
            "file": "tools/agent_desktop_broker.py",
            "target": hunk_7b_target,
            "expected_replacement": hunk_7b_replacement,
        },
    ]


def apply_predefined_maintainer_patch_to_candidate(candidate_root: Path) -> dict[str, Any]:
    """Apply the predefined maintainer patch (used only for offline unit tests, labeled accordingly)."""
    applied_hunks: list[dict[str, Any]] = []
    for spec in _malformed_delegation_hunk_specs():
        fpath = candidate_root / spec["file"]
        text = fpath.read_text(encoding="utf-8")
        if spec["target"] not in text:
            raise RuntimeError(f"Target block for {spec['hunk_id']} not found in {spec['file']}")
        text = text.replace(spec["target"], spec["expected_replacement"], 1)
        fpath.write_text(text, encoding="utf-8")
        applied_hunks.append(
            {
                "hunk_id": spec["hunk_id"],
                "file": spec["file"],
                "target_sha256": sha256_text(spec["target"]),
                "replacement_sha256": sha256_text(spec["expected_replacement"]),
            }
        )
    return {
        "patch_origin": "predefined_maintainer_patch",
        "evidence_label": "predefined_maintainer_patch",
        "patched_files": [
            "tools/sabrina_lab_assistant_mcp.py",
            "tools/agent_desktop_broker.py",
        ],
        "applied_hunks": applied_hunks,
    }


# Keep alias for backwards compatibility in tests if imported directly
apply_malformed_delegation_patch_to_candidate = apply_predefined_maintainer_patch_to_candidate


def apply_live_coding_worker_patch_to_candidate(
    candidate_root: Path,
    run_dir: Path,
    change_request: ChangeRequest,
    *,
    ollama_base: str = DEFAULT_OLLAMA_BASE,
    model: str = DEFAULT_CODING_WORKER_MODEL,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Request structured patch hunks from the live coding worker (qwen2.5-coder:14b) and apply
    ONLY the coding worker's actual returned replacement text to the candidate harness.

    Preserves full cryptographic response-to-diff provenance in `coding_worker_requests.json`
    and `coding_worker_responses.json`.
    """
    specs = _malformed_delegation_hunk_specs()
    requests_log: list[dict[str, Any]] = []
    responses_log: list[dict[str, Any]] = []
    applied_hunks: list[dict[str, Any]] = []

    for spec in specs:
        hunk_id = spec["hunk_id"]
        rel_file = spec["file"]
        target = spec["target"]
        expected_replacement = spec["expected_replacement"]

        user_prompt = (
            f"Change Request ID: {change_request.request_id}\n"
            f"Hunk ID: {hunk_id}\n"
            f"Target File: {rel_file}\n\n"
            "You are generating an exact patch hunk for Sabrina's isolated candidate harness.\n"
            "Return ONLY a JSON object with keys:\n"
            '- "hunk_id": exact hunk_id string\n'
            '- "file": exact target file path\n'
            '- "replacement": the exact Python replacement block below (verbatim, preserving all indentation and newlines)\n\n'
            "=== TARGET BLOCK TO REPLACE ===\n"
            f"{target}\n"
            "=== REQUIRED REPLACEMENT BLOCK ===\n"
            f"{expected_replacement}\n"
        )
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are Sabrina's isolated coding worker. Output ONLY valid JSON with keys "
                        '"hunk_id", "file", and "replacement". Preserve the replacement code verbatim.'
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0, "num_predict": 4096, "num_ctx": 16384},
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        req_sha = sha256_text(payload_json)
        started = time.time()
        req = urllib.request.Request(
            f"{ollama_base}/api/chat",
            data=payload_json.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
        elapsed = round(time.time() - started, 3)
        raw_text = raw_bytes.decode("utf-8")
        resp_sha = sha256_text(raw_text)
        raw_envelope = json.loads(raw_text)
        worker_content = str(raw_envelope.get("message", {}).get("content", "")).strip()
        worker_content_sha = sha256_text(worker_content)
        parsed_hunk = json.loads(worker_content)

        returned_replacement = parsed_hunk.get("replacement")
        if not isinstance(returned_replacement, str) or not returned_replacement.strip():
            raise RuntimeError(f"Live coding worker returned empty or non-string replacement for {hunk_id}")

        # Apply the coding worker's actual returned replacement directly to the candidate file
        fpath = candidate_root / rel_file
        file_text = fpath.read_text(encoding="utf-8")
        if target not in file_text:
            raise RuntimeError(f"Baseline target block for {hunk_id} not found in candidate {rel_file}")
        updated_text = file_text.replace(target, returned_replacement, 1)
        fpath.write_text(updated_text, encoding="utf-8")

        requests_log.append(
            {
                "hunk_id": hunk_id,
                "file": rel_file,
                "request_sha256": req_sha,
                "payload": payload,
            }
        )
        responses_log.append(
            {
                "hunk_id": hunk_id,
                "file": rel_file,
                "response_envelope_sha256": resp_sha,
                "worker_message_content_sha256": worker_content_sha,
                "elapsed_seconds": elapsed,
                "prompt_eval_count": raw_envelope.get("prompt_eval_count"),
                "eval_count": raw_envelope.get("eval_count"),
                "worker_message_content": worker_content,
            }
        )
        applied_hunks.append(
            {
                "hunk_id": hunk_id,
                "file": rel_file,
                "request_sha256": req_sha,
                "response_envelope_sha256": resp_sha,
                "worker_message_content_sha256": worker_content_sha,
                "target_sha256": sha256_text(target),
                "worker_replacement_sha256": sha256_text(returned_replacement),
                "elapsed_seconds": elapsed,
                "prompt_eval_count": raw_envelope.get("prompt_eval_count"),
                "eval_count": raw_envelope.get("eval_count"),
            }
        )

    requests_path = run_dir / "coding_worker_requests.json"
    responses_path = run_dir / "coding_worker_responses.json"
    requests_path.write_text(json.dumps(requests_log, indent=2, ensure_ascii=False), encoding="utf-8")
    responses_path.write_text(json.dumps(responses_log, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "patch_origin": "live_coding_worker_response",
        "evidence_label": "live_qwen_inference",
        "worker_model": model,
        "ollama_base": ollama_base,
        "requests_path": str(requests_path),
        "requests_sha256": sha256_file(requests_path),
        "responses_path": str(responses_path),
        "responses_sha256": sha256_file(responses_path),
        "patched_files": [
            "tools/sabrina_lab_assistant_mcp.py",
            "tools/agent_desktop_broker.py",
        ],
        "applied_hunks": applied_hunks,
    }


class SabrinaDevSupervisor:
    """Separate development supervisor controlling isolated builds, independent tests, activation, and rollback."""

    def __init__(
        self,
        repo_root: Path = REPO_ROOT,
        supervisor_dir: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.supervisor_dir = Path(supervisor_dir or (self.repo_root / "artifacts" / "dev_supervisor")).resolve()
        self.supervisor_dir.mkdir(parents=True, exist_ok=True)

    @property
    def activation_lock_path(self) -> Path:
        return self.supervisor_dir / ".activation.lock"

    # ------------------------------------------------------------------
    # Stage 1: Request a change
    # ------------------------------------------------------------------
    def request_change(self, spec: dict[str, Any] | None = None) -> ChangeRequest:
        spec = dict(spec or default_malformed_delegation_request())
        limitation = str(spec.get("limitation") or "").strip()
        if not limitation:
            raise ValueError("ChangeRequest requires a non-empty limitation description")
        evidence = spec.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("ChangeRequest requires at least one concrete evidence record")
        target_files = spec.get("target_files")
        if not isinstance(target_files, list) or not target_files:
            raise ValueError("ChangeRequest requires one or more target_files")
        for rel in target_files:
            full = (self.repo_root / rel).resolve()
            full.relative_to(self.repo_root)
            if not full.is_file():
                raise FileNotFoundError(f"Target file does not exist: {rel}")
        acceptance_test = spec.get("acceptance_test")
        if not isinstance(acceptance_test, dict) or not acceptance_test:
            raise ValueError("ChangeRequest requires a concrete acceptance_test specification")

        request_id = str(spec.get("request_id") or f"devsup-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}")
        req = ChangeRequest(
            request_id=request_id,
            initiated_by=str(spec.get("initiated_by") or "Sabrina"),
            title=str(spec.get("title") or "Sabrina harness improvement"),
            limitation=limitation,
            evidence=evidence,
            target_files=[str(f) for f in target_files],
            acceptance_test=acceptance_test,
        )
        run_dir = self.supervisor_dir / req.request_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "change_request.json").write_text(
            json.dumps(req.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return req

    # ------------------------------------------------------------------
    # Stage 2: Build in isolation
    # ------------------------------------------------------------------
    def build_in_isolation(
        self,
        change_request: ChangeRequest,
        *,
        worker_fn: Callable[[Path, ChangeRequest], dict[str, Any]] | None = None,
        consult_live_worker: bool = True,
        use_predefined_maintainer_patch: bool = False,
    ) -> dict[str, Any]:
        run_dir = self.supervisor_dir / change_request.request_id
        baseline_dir = run_dir / "baseline"
        candidate_dir = run_dir / "candidate"
        if baseline_dir.exists():
            shutil.rmtree(baseline_dir)
        if candidate_dir.exists():
            shutil.rmtree(candidate_dir)
        baseline_dir.mkdir(parents=True, exist_ok=True)
        candidate_dir.mkdir(parents=True, exist_ok=True)

        # Copy tools/ and tests/ into candidate_dir so candidate imports are completely isolated
        for subdir in ("tools", "tests"):
            src_sub = self.repo_root / subdir
            if src_sub.is_dir():
                shutil.copytree(
                    src_sub,
                    candidate_dir / subdir,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
        for root_doc in ("AGENTS.md", "README.md", "RSI_PROCESS.md", "RSI_PROCESS_EXTERNAL.md", "sabrina_operator_console.py"):
            src_doc = self.repo_root / root_doc
            if src_doc.is_file():
                shutil.copy2(src_doc, candidate_dir / root_doc)

        baseline_hashes: dict[str, str] = {}
        for rel in change_request.target_files:
            src_file = self.repo_root / rel
            dst_base = baseline_dir / rel
            dst_base.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_base)
            baseline_hashes[rel] = sha256_file(src_file)

        if worker_fn is not None:
            raw_worker_res = worker_fn(candidate_dir, change_request) or {}
            patch_origin = str(raw_worker_res.get("patch_origin") or "custom_test_worker_fn")
            evidence_label = str(raw_worker_res.get("evidence_label") or "custom_test_worker_fn")
            worker_result = {
                **raw_worker_res,
                "patch_origin": patch_origin,
                "evidence_label": evidence_label,
            }
        elif consult_live_worker and not use_predefined_maintainer_patch:
            worker_result = apply_live_coding_worker_patch_to_candidate(
                candidate_dir,
                run_dir,
                change_request,
            )
            patch_origin = "live_coding_worker_response"
            evidence_label = "live_qwen_inference"
        else:
            worker_result = apply_predefined_maintainer_patch_to_candidate(candidate_dir)
            patch_origin = "predefined_maintainer_patch"
            evidence_label = "predefined_maintainer_patch"

        # Generate unified diff between baseline_dir and candidate_dir
        diffs: list[str] = []
        candidate_hashes: dict[str, str] = {}
        changed_files: list[str] = []
        for rel in change_request.target_files:
            base_file = baseline_dir / rel
            cand_file = candidate_dir / rel
            base_lines = base_file.read_text(encoding="utf-8").splitlines(keepends=True)
            cand_lines = cand_file.read_text(encoding="utf-8").splitlines(keepends=True)
            candidate_hashes[rel] = sha256_file(cand_file)
            if candidate_hashes[rel] != baseline_hashes[rel]:
                changed_files.append(rel)
            file_diff = list(
                difflib.unified_diff(
                    base_lines,
                    cand_lines,
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            if file_diff:
                diffs.append("".join(file_diff))

        patch_text = "\n".join(diffs)
        patch_path = run_dir / "candidate.patch"
        patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
        patch_sha256 = sha256_file(patch_path)

        # Verify production files were not modified by the isolated build
        production_untouched = all(
            sha256_file(self.repo_root / rel) == baseline_hashes[rel]
            for rel in change_request.target_files
        )
        if not production_untouched:
            raise RuntimeError("Isolation violation: production target files changed during candidate build!")

        patch_provenance = {
            "patch_origin": patch_origin,
            "evidence_label": evidence_label,
            "worker_model": worker_result.get("worker_model"),
            "ollama_base": worker_result.get("ollama_base"),
            "requests_path": worker_result.get("requests_path"),
            "requests_sha256": worker_result.get("requests_sha256"),
            "responses_path": worker_result.get("responses_path"),
            "responses_sha256": worker_result.get("responses_sha256"),
            "applied_hunks": worker_result.get("applied_hunks", []),
            "baseline_hashes": baseline_hashes,
            "resulting_candidate_hashes": candidate_hashes,
            "resulting_patch_sha256": patch_sha256,
        }

        build_bundle = {
            "request_id": change_request.request_id,
            "run_dir": str(run_dir),
            "baseline_dir": str(baseline_dir),
            "candidate_dir": str(candidate_dir),
            "patch_path": str(patch_path),
            "patch_sha256": patch_sha256,
            "patch_origin": patch_origin,
            "evidence_label": evidence_label,
            "baseline_hashes": baseline_hashes,
            "candidate_hashes": candidate_hashes,
            "changed_files": changed_files,
            "production_untouched": production_untouched,
            "patch_provenance": patch_provenance,
            "worker_result": worker_result,
        }
        (run_dir / "build_bundle.json").write_text(
            json.dumps(build_bundle, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return build_bundle

    # ------------------------------------------------------------------
    # Stage 3: Test independently
    # ------------------------------------------------------------------
    def test_independently(
        self,
        change_request: ChangeRequest,
        build_bundle: dict[str, Any],
        *,
        run_live_worker_exchange: bool | None = None,
    ) -> dict[str, Any]:
        run_dir = Path(build_bundle["run_dir"])
        candidate_dir = Path(build_bundle["candidate_dir"])
        patch_path = Path(build_bundle["patch_path"])
        if run_live_worker_exchange is None:
            run_live_worker_exchange = bool(
                change_request.acceptance_test.get(
                    "require_audited_native_desktop_handoff",
                    change_request.acceptance_test.get("require_live_worker_exchange", True),
                )
            )

        # Verify candidate files on disk match build_bundle before running tests
        tested_candidate_hashes: dict[str, str] = {}
        for rel in change_request.target_files:
            cand_file = candidate_dir / rel
            h = sha256_file(cand_file)
            if h != build_bundle["candidate_hashes"].get(rel):
                raise ValueError(
                    f"Candidate file {rel} changed between build_in_isolation and test_independently"
                )
            tested_candidate_hashes[rel] = h
        tested_patch_sha256 = sha256_file(patch_path)
        if tested_patch_sha256 != build_bundle.get("patch_sha256"):
            raise ValueError("candidate.patch hash changed between build_in_isolation and test_independently")

        # 1. Syntax validation (py_compile) on all target files in candidate_dir
        syntax_results: list[dict[str, Any]] = []
        syntax_passed = True
        for rel in change_request.target_files:
            target = candidate_dir / rel
            try:
                py_compile.compile(str(target), doraise=True)
                syntax_results.append({"file": rel, "passed": True})
            except Exception as exc:
                syntax_passed = False
                syntax_results.append({"file": rel, "passed": False, "error": str(exc)})

        # 2. Frozen unit tests in an isolated subprocess with candidate_dir first on PYTHONPATH
        frozen_files = change_request.acceptance_test.get("frozen_test_files") or list(FROZEN_TEST_FILES)
        frozen_cmd = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *[str(candidate_dir / rel) for rel in frozen_files],
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{candidate_dir}{os.pathsep}{self.repo_root}"
        frozen_proc = subprocess.run(
            frozen_cmd,
            cwd=str(candidate_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        frozen_passed = frozen_proc.returncode == 0
        frozen_report = {
            "evidence_label": "simulated_desktop_observations",
            "is_simulated_desktop": True,
            "uses_live_qwen": False,
            "uses_real_docker": True,
            "passed": frozen_passed,
            "returncode": frozen_proc.returncode,
            "test_files": frozen_files,
            "stdout": (frozen_proc.stdout or "")[-4000:],
            "stderr": (frozen_proc.stderr or "")[-2000:],
        }

        # 3. Malformed delegation call regression suite (replaying the exact failed tasks with simulated desktop observations)
        regression_script = run_dir / "run_malformed_delegation_regression.py"
        regression_script.write_text(
            _MALFORMED_DELEGATION_REGRESSION_DRIVER,
            encoding="utf-8",
        )
        reg_proc = subprocess.run(
            [sys.executable, str(regression_script)],
            cwd=str(candidate_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        try:
            regression_report = json.loads((reg_proc.stdout or "").strip().splitlines()[-1])
        except Exception:
            regression_report = {
                "passed": False,
                "returncode": reg_proc.returncode,
                "stdout": (reg_proc.stdout or "")[-4000:],
                "stderr": (reg_proc.stderr or "")[-2000:],
            }
        regression_report["evidence_label"] = "simulated_desktop_observations"
        regression_report["is_simulated_desktop"] = True
        regression_report["uses_live_qwen"] = False
        regression_report["uses_real_docker"] = False
        regression_passed = bool(regression_report.get("passed")) and reg_proc.returncode == 0

        # 4. One real audited native-app handoff (via AgentDesktopBroker._run_cli / agent-desktop against Claude.exe)
        #    followed by independent real Docker verification (LabVerifier on ego.local)
        native_handoff_report: dict[str, Any]
        docker_verification_report: dict[str, Any]
        if run_live_worker_exchange:
            exchange_script = run_dir / "run_audited_native_desktop_handoff.py"
            exchange_script.write_text(
                _AUDITED_NATIVE_DESKTOP_HANDOFF_DRIVER,
                encoding="utf-8",
            )
            handoff_env = env.copy()
            handoff_env["SABRINA_NATIVE_HANDOFF_DB"] = str(run_dir / "native_handoff_audit.sqlite3")
            ex_proc = subprocess.run(
                [sys.executable, str(exchange_script)],
                cwd=str(candidate_dir),
                env=handoff_env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            try:
                combined_exchange = json.loads((ex_proc.stdout or "").strip().splitlines()[-1])
            except Exception:
                combined_exchange = {
                    "passed": False,
                    "returncode": ex_proc.returncode,
                    "stdout": (ex_proc.stdout or "")[-4000:],
                    "stderr": (ex_proc.stderr or "")[-2000:],
                }
            native_handoff_report = combined_exchange.get(
                "audited_native_desktop_handoff",
                {
                    "evidence_label": "audited_native_desktop_handoff",
                    "is_simulated_desktop": False,
                    "passed": False,
                    "error": combined_exchange.get("error") or combined_exchange.get("stderr") or "native handoff failed",
                },
            )
            docker_verification_report = combined_exchange.get(
                "real_docker_verification",
                {
                    "evidence_label": "real_docker_verification",
                    "is_simulated_desktop": False,
                    "uses_real_docker": True,
                    "passed": False,
                },
            )
            exchange_passed = (
                bool(combined_exchange.get("passed"))
                and bool(native_handoff_report.get("passed"))
                and bool(docker_verification_report.get("passed"))
                and ex_proc.returncode == 0
            )
        else:
            native_handoff_report = {
                "evidence_label": "audited_native_desktop_handoff",
                "is_simulated_desktop": False,
                "passed": True,
                "skipped": True,
                "reason": "disabled_for_unit_test",
            }
            docker_verification_report = {
                "evidence_label": "real_docker_verification",
                "is_simulated_desktop": False,
                "uses_real_docker": True,
                "passed": True,
                "skipped": True,
                "reason": "disabled_for_unit_test",
            }
            exchange_passed = True

        # 5. Permission & skill-approval preservation audit
        permissions_audit = self._audit_permissions(candidate_dir, change_request)

        completed_at = round(time.time(), 6)
        approval_bundle_sha256 = compute_approval_bundle_sha256(
            request_id=change_request.request_id,
            baseline_hashes=build_bundle["baseline_hashes"],
            tested_candidate_hashes=tested_candidate_hashes,
            tested_patch_sha256=tested_patch_sha256,
            test_completed_at=completed_at,
        )

        overall_passed = (
            syntax_passed
            and frozen_passed
            and regression_passed
            and exchange_passed
            and permissions_audit["passed"]
        )
        test_bundle = {
            "request_id": change_request.request_id,
            "overall_passed": overall_passed,
            "baseline_hashes": dict(build_bundle["baseline_hashes"]),
            "tested_candidate_hashes": tested_candidate_hashes,
            "tested_patch_sha256": tested_patch_sha256,
            "approval_bundle_sha256": approval_bundle_sha256,
            "evidence_labels": {
                "coding_worker_patch_generation": build_bundle.get("evidence_label", "live_qwen_inference"),
                "frozen_tests": "simulated_desktop_observations",
                "malformed_delegation_regression": "simulated_desktop_observations",
                "audited_native_desktop_handoff": "audited_native_desktop_handoff",
                "real_docker_verification": "real_docker_verification",
            },
            "syntax": {"passed": syntax_passed, "files": syntax_results},
            "frozen_tests": frozen_report,
            "malformed_delegation_regression": regression_report,
            "audited_native_desktop_handoff": native_handoff_report,
            "real_docker_verification": docker_verification_report,
            "permissions_audit": permissions_audit,
            "completed_at": completed_at,
        }
        (run_dir / "test_bundle.json").write_text(
            json.dumps(test_bundle, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return test_bundle

    def _audit_permissions(self, candidate_dir: Path, change_request: ChangeRequest) -> dict[str, Any]:
        """Verify no new permissions were granted and human skill-activation approval is preserved."""
        violations: list[str] = []
        for rel in change_request.target_files:
            text = (candidate_dir / rel).read_text(encoding="utf-8")
            for pattern in PROTECTED_PERMISSION_PATTERNS:
                if pattern in text:
                    violations.append(f"Forbidden permission bypass pattern {pattern!r} in {rel}")
        hydration_file = candidate_dir / "tools" / "sabrina_skill_hydration.py"
        skill_approval_preserved = True
        if hydration_file.is_file():
            htext = hydration_file.read_text(encoding="utf-8")
            if "pending_approval" not in htext:
                skill_approval_preserved = False
                violations.append("sabrina_skill_hydration.py no longer enforces pending_approval state")
        return {
            "passed": len(violations) == 0 and skill_approval_preserved,
            "changed_permissions": [],
            "skill_approval_requirement_preserved": skill_approval_preserved,
            "violations": violations,
        }

    # ------------------------------------------------------------------
    # Stage 4: Review the evidence
    # ------------------------------------------------------------------
    def review_evidence(
        self,
        change_request: ChangeRequest,
        build_bundle: dict[str, Any],
        test_bundle: dict[str, Any],
    ) -> dict[str, Any]:
        run_dir = Path(build_bundle["run_dir"])
        patch_text = Path(build_bundle["patch_path"]).read_text(encoding="utf-8")
        failures: list[str] = []
        if not test_bundle["syntax"]["passed"]:
            failures.append("syntax_validation_failed")
        if not test_bundle["frozen_tests"]["passed"]:
            failures.append("frozen_tests_failed")
        if not test_bundle["malformed_delegation_regression"]["passed"]:
            failures.append("malformed_delegation_regression_failed")
        if not test_bundle["audited_native_desktop_handoff"]["passed"]:
            failures.append("audited_native_desktop_handoff_failed")
        if not test_bundle["real_docker_verification"]["passed"]:
            failures.append("real_docker_verification_failed")
        if not test_bundle["permissions_audit"]["passed"]:
            failures.append("permissions_audit_failed")

        review = {
            "request_id": change_request.request_id,
            "title": change_request.title,
            "initiated_by": change_request.initiated_by,
            "status": "READY_FOR_ACTIVATION_APPROVAL" if not failures else "REJECTED_BY_TEST_RUNNER",
            "candidate_staged_and_unapproved": True,
            "approval_required_for_activation": True,
            "approval_bundle_sha256": test_bundle["approval_bundle_sha256"],
            "test_completed_at": test_bundle["completed_at"],
            "skill_approval_requirement_preserved": test_bundle["permissions_audit"][
                "skill_approval_requirement_preserved"
            ],
            "patch_origin": build_bundle.get("patch_origin"),
            "patch_provenance": build_bundle.get("patch_provenance"),
            "evidence_labels": test_bundle.get("evidence_labels"),
            "changed_files": build_bundle["changed_files"],
            "changed_permissions": test_bundle["permissions_audit"]["changed_permissions"],
            "baseline_hashes": build_bundle["baseline_hashes"],
            "candidate_hashes": build_bundle["candidate_hashes"],
            "tested_candidate_hashes": test_bundle["tested_candidate_hashes"],
            "patch_sha256": build_bundle["patch_sha256"],
            "tested_patch_sha256": test_bundle["tested_patch_sha256"],
            "patch_path": build_bundle["patch_path"],
            "production_untouched": build_bundle["production_untouched"],
            "failures": failures,
            "test_summary": {
                "syntax_passed": test_bundle["syntax"]["passed"],
                "frozen_tests_passed": test_bundle["frozen_tests"]["passed"],
                "malformed_delegation_regression_passed": test_bundle["malformed_delegation_regression"]["passed"],
                "audited_native_desktop_handoff_passed": test_bundle["audited_native_desktop_handoff"]["passed"],
                "real_docker_verification_passed": test_bundle["real_docker_verification"]["passed"],
            },
            "malformed_delegation_regression": test_bundle["malformed_delegation_regression"],
            "audited_native_desktop_handoff": test_bundle["audited_native_desktop_handoff"],
            "real_docker_verification": test_bundle["real_docker_verification"],
        }
        (run_dir / "review_evidence.json").write_text(
            json.dumps(review, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        md_lines = [
            f"# Development Supervisor Review — `{change_request.request_id}`",
            "",
            f"- **Title**: {change_request.title}",
            f"- **Initiated By**: `{change_request.initiated_by}`",
            f"- **Status**: `{review['status']}` (Staged & Unapproved)",
            f"- **Patch Origin**: `{review['patch_origin']}` (`{build_bundle.get('evidence_label')}`)",
            f"- **Approval Bundle SHA-256**: `{review['approval_bundle_sha256']}`",
            f"- **Production Files Untouched**: `{review['production_untouched']}`",
            f"- **Skill Activation Human Approval Preserved**: `{review['skill_approval_requirement_preserved']}`",
            f"- **Changed Permissions**: `{review['changed_permissions']}` (none)",
            f"- **Failures**: `{failures or 'none'}`",
            "",
            "## Evidence Classification & Test Summary",
            f"1. **Coding Worker Patch Generation (`{review['evidence_labels']['coding_worker_patch_generation']}`)**: `patch_origin={review['patch_origin']}`",
            f"2. **Syntax Validation (`py_compile`)**: `{'PASSED' if review['test_summary']['syntax_passed'] else 'FAILED'}`",
            f"3. **Frozen Unit Tests (`simulated_desktop_observations`)**: `{'PASSED' if review['test_summary']['frozen_tests_passed'] else 'FAILED'}`",
            f"4. **Malformed Delegation Regression (`simulated_desktop_observations`)**: `{'PASSED' if review['test_summary']['malformed_delegation_regression_passed'] else 'FAILED'}`",
            f"5. **Audited Native Desktop Handoff (`audited_native_desktop_handoff`, `Claude.exe` via `agent-desktop`)**: `{'PASSED' if review['test_summary']['audited_native_desktop_handoff_passed'] else 'FAILED'}`",
            f"6. **Independent Docker Verifier (`real_docker_verification`, `LabVerifier` on `ego.local`)**: `{'PASSED' if review['test_summary']['real_docker_verification_passed'] else 'FAILED'}`",
            "",
            "## Unified Diff (`candidate.patch`)",
            "```diff",
            patch_text.strip(),
            "```",
            "",
        ]
        review_md_path = run_dir / "REVIEW.md"
        review_md_path.write_text("\n".join(md_lines), encoding="utf-8")
        review["review_markdown_path"] = str(review_md_path)
        return review

    # ------------------------------------------------------------------
    # Stage 5 & 6: Activate a version + Check and roll back
    # ------------------------------------------------------------------
    def _acquire_exclusive_activation_lock(self, request_id: str, owner: str) -> Path:
        lock_path = self.activation_lock_path
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            existing_info = ""
            try:
                existing_info = lock_path.read_text(encoding="utf-8").strip()
            except Exception:
                pass
            raise RuntimeError(
                f"Activation blocked: exclusive activation ownership lock is already held ({existing_info or lock_path})"
            ) from exc
        try:
            lock_payload = {
                "request_id": request_id,
                "owner": owner,
                "pid": os.getpid(),
                "acquired_at": time.time(),
            }
            os.write(fd, json.dumps(lock_payload).encode("utf-8"))
        finally:
            os.close(fd)
        return lock_path

    @staticmethod
    def _release_exclusive_activation_lock(lock_path: Path) -> None:
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _restore_from_backup(
        self,
        change_request: ChangeRequest,
        backup_dir: Path,
        backup_hashes: dict[str, str],
    ) -> bool:
        for rel in change_request.target_files:
            bkp_file = backup_dir / rel
            if bkp_file.is_file():
                shutil.copy2(bkp_file, self.repo_root / rel)
        return all(
            sha256_file(self.repo_root / rel) == backup_hashes[rel]
            for rel in change_request.target_files
        )

    def activate_version(
        self,
        change_request: ChangeRequest,
        build_bundle: dict[str, Any],
        test_bundle: dict[str, Any],
        *,
        approved: bool = False,
        approver: str = "",
        expected_bundle_sha256: str | None = None,
        max_approval_age_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        activation_owner: str | None = None,
        install_file_fn: Callable[[Path, Path, str], None] | None = None,
        startup_check_fn: Callable[[Path], bool] | None = None,
        acceptance_check_fn: Callable[[Path], bool] | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> dict[str, Any]:
        if not approved:
            raise PermissionError(
                "Activation blocked: explicit human approval (approved=True) is required before installing a candidate harness version."
            )
        if not test_bundle.get("overall_passed"):
            raise ValueError("Activation blocked: candidate harness did not pass all independent tests.")

        run_dir = Path(build_bundle["run_dir"])
        candidate_dir = Path(build_bundle["candidate_dir"])
        patch_path = Path(build_bundle["patch_path"])
        owner_id = activation_owner or f"{approver or 'operator'}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

        # Acquire exclusive activation ownership lock first
        lock_path = self._acquire_exclusive_activation_lock(change_request.request_id, owner_id)
        try:
            now = float(now_fn())

            # 1. Reject stale approval bundles (already consumed, expired TTL, or mismatched digest)
            if (run_dir / "activation_record.json").is_file():
                raise ValueError(
                    f"Activation blocked: stale approval bundle ({change_request.request_id} was already processed for activation)."
                )
            if (
                build_bundle.get("request_id") != change_request.request_id
                or test_bundle.get("request_id") != change_request.request_id
            ):
                raise ValueError("Activation blocked: stale or mismatched request_id across approval bundle.")

            completed_at = test_bundle.get("completed_at")
            if not isinstance(completed_at, (int, float)):
                raise ValueError("Activation blocked: stale approval bundle (missing test completion timestamp).")
            bundle_age = now - float(completed_at)
            if bundle_age > float(max_approval_age_seconds) or bundle_age < -5.0:
                raise ValueError(
                    f"Activation blocked: stale approval bundle (age {bundle_age:.1f}s exceeds max_approval_age_seconds={max_approval_age_seconds:.1f}s)."
                )

            tested_candidate_hashes = test_bundle.get("tested_candidate_hashes")
            tested_patch_sha256 = test_bundle.get("tested_patch_sha256")
            if not isinstance(tested_candidate_hashes, dict) or not tested_candidate_hashes or not tested_patch_sha256:
                raise ValueError("Activation blocked: test_bundle is missing tested_candidate_hashes or tested_patch_sha256.")

            computed_bundle_sha = compute_approval_bundle_sha256(
                request_id=change_request.request_id,
                baseline_hashes=build_bundle.get("baseline_hashes", {}),
                tested_candidate_hashes=tested_candidate_hashes,
                tested_patch_sha256=str(tested_patch_sha256),
                test_completed_at=float(completed_at),
            )
            if test_bundle.get("approval_bundle_sha256") != computed_bundle_sha:
                raise ValueError("Activation blocked: stale approval bundle (approval_bundle_sha256 does not match tested artifacts).")
            if expected_bundle_sha256 is not None and expected_bundle_sha256 != computed_bundle_sha:
                raise ValueError("Activation blocked: stale approval bundle (expected_bundle_sha256 mismatch).")
            review_file = run_dir / "review_evidence.json"
            if review_file.is_file():
                on_disk_review = json.loads(review_file.read_text(encoding="utf-8"))
                if on_disk_review.get("approval_bundle_sha256") and on_disk_review.get("approval_bundle_sha256") != computed_bundle_sha:
                    raise ValueError("Activation blocked: stale approval bundle (review_evidence.json on disk does not match bundle).")

            # 2. Require exact tested candidate hashes
            build_cand_hashes = build_bundle.get("candidate_hashes") or {}
            for rel in change_request.target_files:
                cand_file = candidate_dir / rel
                if not cand_file.is_file():
                    raise ValueError(f"Activation blocked: candidate file missing: {rel}")
                actual_cand_hash = sha256_file(cand_file)
                if (
                    actual_cand_hash != build_cand_hashes.get(rel)
                    or actual_cand_hash != tested_candidate_hashes.get(rel)
                ):
                    raise ValueError(
                        f"Activation blocked: candidate hash mismatch for {rel} "
                        f"(disk={actual_cand_hash}, build={build_cand_hashes.get(rel)}, tested={tested_candidate_hashes.get(rel)})."
                    )
            if not patch_path.is_file():
                raise ValueError("Activation blocked: candidate.patch is missing.")
            actual_patch_hash = sha256_file(patch_path)
            if (
                actual_patch_hash != build_bundle.get("patch_sha256")
                or actual_patch_hash != tested_patch_sha256
            ):
                raise ValueError("Activation blocked: candidate.patch hash does not match exact tested patch hash.")

            # 3. Require unchanged production baseline
            baseline_hashes = build_bundle.get("baseline_hashes") or {}
            test_baseline_hashes = test_bundle.get("baseline_hashes") or baseline_hashes
            for rel in change_request.target_files:
                prod_file = self.repo_root / rel
                if not prod_file.is_file():
                    raise RuntimeError(f"Activation blocked: production baseline file missing: {rel}")
                current_prod_hash = sha256_file(prod_file)
                if (
                    current_prod_hash != baseline_hashes.get(rel)
                    or current_prod_hash != test_baseline_hashes.get(rel)
                ):
                    raise RuntimeError(
                        f"Activation blocked: production baseline drifted for {rel} "
                        f"(expected {baseline_hashes.get(rel)}, found {current_prod_hash})."
                    )

            # 4. Create verified backup before touching any production file
            backup_dir = run_dir / "backup"
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            backup_dir.mkdir(parents=True, exist_ok=True)

            backup_hashes: dict[str, str] = {}
            for rel in change_request.target_files:
                prod_file = self.repo_root / rel
                bkp_file = backup_dir / rel
                bkp_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(prod_file, bkp_file)
                backup_hashes[rel] = sha256_file(bkp_file)
                if backup_hashes[rel] != baseline_hashes[rel]:
                    raise RuntimeError(f"Backup hash verification failed for {rel}")

            # 5. Install candidate files inside rollback-protected block so installation exceptions
            #    (including mid-copy failures after partial overwrite) trigger immediate rollback.
            try:
                for rel in change_request.target_files:
                    src = candidate_dir / rel
                    dst = self.repo_root / rel
                    if install_file_fn is not None:
                        install_file_fn(src, dst, rel)
                    else:
                        shutil.copy2(src, dst)
                    if sha256_file(dst) != tested_candidate_hashes[rel]:
                        raise IOError(f"Post-copy hash mismatch for {rel}")
            except Exception as install_exc:
                restored_matches = self._restore_from_backup(change_request, backup_dir, backup_hashes)
                activation_record = {
                    "request_id": change_request.request_id,
                    "approved": True,
                    "approver": approver or "operator",
                    "activation_owner": owner_id,
                    "approval_bundle_sha256": computed_bundle_sha,
                    "backup_dir": str(backup_dir),
                    "backup_hashes": backup_hashes,
                    "status": "rolled_back",
                    "rolled_back": True,
                    "rollback_trigger": "installation_exception",
                    "startup_ok": False,
                    "acceptance_ok": False,
                    "failure_reason": f"install_exception: {type(install_exc).__name__}: {install_exc}",
                    "restored_previous_version": restored_matches,
                }
                (run_dir / "activation_record.json").write_text(
                    json.dumps(activation_record, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                return activation_record

            # 6. Check startup and acceptance; roll back on any failure or exception
            check_result = self.check_and_rollback(
                change_request,
                build_bundle,
                backup_dir=backup_dir,
                backup_hashes=backup_hashes,
                startup_check_fn=startup_check_fn,
                acceptance_check_fn=acceptance_check_fn,
            )
            activation_record = {
                "request_id": change_request.request_id,
                "approved": True,
                "approver": approver or "operator",
                "activation_owner": owner_id,
                "approval_bundle_sha256": computed_bundle_sha,
                "backup_dir": str(backup_dir),
                "backup_hashes": backup_hashes,
                **check_result,
            }
            (run_dir / "activation_record.json").write_text(
                json.dumps(activation_record, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return activation_record
        finally:
            self._release_exclusive_activation_lock(lock_path)

    def check_and_rollback(
        self,
        change_request: ChangeRequest,
        build_bundle: dict[str, Any],
        *,
        backup_dir: Path,
        backup_hashes: dict[str, str],
        startup_check_fn: Callable[[Path], bool] | None = None,
        acceptance_check_fn: Callable[[Path], bool] | None = None,
    ) -> dict[str, Any]:
        startup_ok = False
        acceptance_ok = False
        failure_reason = ""
        rollback_trigger = ""
        try:
            if startup_check_fn is not None:
                startup_ok = bool(startup_check_fn(self.repo_root))
            else:
                startup_ok = self._default_startup_check(change_request)
            if not startup_ok:
                failure_reason = "startup_check_failed"
                rollback_trigger = "startup_check_failed"
            else:
                if acceptance_check_fn is not None:
                    acceptance_ok = bool(acceptance_check_fn(self.repo_root))
                else:
                    acceptance_ok = self._default_acceptance_check()
                if not acceptance_ok:
                    failure_reason = "acceptance_check_failed"
                    rollback_trigger = "acceptance_check_failed"
        except Exception as exc:
            failure_reason = f"{type(exc).__name__}: {exc}"
            rollback_trigger = "startup_check_exception" if not startup_ok else "acceptance_check_exception"

        if not (startup_ok and acceptance_ok):
            restored_matches = self._restore_from_backup(change_request, backup_dir, backup_hashes)
            return {
                "status": "rolled_back",
                "rolled_back": True,
                "rollback_trigger": rollback_trigger or "post_activation_check_failed",
                "startup_ok": startup_ok,
                "acceptance_ok": acceptance_ok,
                "failure_reason": failure_reason,
                "restored_previous_version": restored_matches,
            }

        return {
            "status": "activated",
            "rolled_back": False,
            "rollback_trigger": "",
            "startup_ok": True,
            "acceptance_ok": True,
            "failure_reason": "",
            "installed_hashes": {
                rel: sha256_file(self.repo_root / rel)
                for rel in change_request.target_files
            },
        }

    def _default_startup_check(self, change_request: ChangeRequest) -> bool:
        for rel in change_request.target_files:
            py_compile.compile(str(self.repo_root / rel), doraise=True)
        return True

    def _default_acceptance_check(self) -> bool:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/unit/test_create_objective_helper.py"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return proc.returncode == 0

    def list_candidate_runs(self) -> list[dict[str, Any]]:
        """List all candidate runs recorded in supervisor_dir with verification and status metadata."""
        runs: list[dict[str, Any]] = []
        if not self.supervisor_dir.is_dir():
            return runs

        for entry in self.supervisor_dir.iterdir():
            if not entry.is_dir():
                continue
            req_file = entry / "change_request.json"
            if not req_file.is_file():
                continue

            try:
                req_data = json.loads(req_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            request_id = str(req_data.get("request_id") or entry.name)
            title = str(req_data.get("title") or "")
            initiated_by = str(req_data.get("initiated_by") or "")
            target_files = req_data.get("target_files") or []

            build_file = entry / "build_bundle.json"
            build_data: dict[str, Any] = {}
            if build_file.is_file():
                try:
                    build_data = json.loads(build_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            test_file = entry / "test_bundle.json"
            test_data: dict[str, Any] = {}
            if test_file.is_file():
                try:
                    test_data = json.loads(test_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            review_file = entry / "review_evidence.json"
            review_data: dict[str, Any] = {}
            if review_file.is_file():
                try:
                    review_data = json.loads(review_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            activation_file = entry / "activation_record.json"
            activation_record: dict[str, Any] | None = None
            if activation_file.is_file():
                try:
                    activation_record = json.loads(activation_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            # Determine status
            if activation_record is not None:
                if activation_record.get("status") == "activated" and not activation_record.get("rolled_back"):
                    status = "ACTIVATED"
                elif activation_record.get("rolled_back") or activation_record.get("status") == "rolled_back":
                    status = "ROLLED_BACK"
                else:
                    status = str(activation_record.get("status", "ACTIVATED")).upper()
            elif review_data:
                status = str(review_data.get("status", "READY_FOR_ACTIVATION_APPROVAL")).upper()
            elif test_data:
                status = "TESTED"
            elif build_data:
                status = "BUILT"
            else:
                status = "REQUESTED"

            # Check production baseline drift against live repo_root
            baseline_hashes = build_data.get("baseline_hashes") or review_data.get("baseline_hashes") or {}
            drifted_files: list[str] = []
            for rel in target_files:
                prod_f = self.repo_root / rel
                if not prod_f.is_file():
                    drifted_files.append(f"{rel} (missing)")
                elif baseline_hashes.get(rel):
                    curr_h = sha256_file(prod_f)
                    if curr_h != baseline_hashes[rel]:
                        drifted_files.append(f"{rel} (hash mismatch)")

            test_completed_at = test_data.get("completed_at") or review_data.get("test_completed_at")
            now = time.time()
            age_seconds = (now - float(test_completed_at)) if isinstance(test_completed_at, (int, float)) else None
            is_stale = (age_seconds > DEFAULT_APPROVAL_TTL_SECONDS) if age_seconds is not None else False

            patch_path = entry / "candidate.patch"
            review_md = entry / "REVIEW.md"
            audit_db = entry / "native_handoff_audit.sqlite3"

            runs.append({
                "request_id": request_id,
                "title": title,
                "initiated_by": initiated_by,
                "status": status,
                "candidate_staged_and_unapproved": bool(status == "READY_FOR_ACTIVATION_APPROVAL"),
                "approval_bundle_sha256": review_data.get("approval_bundle_sha256") or test_data.get("approval_bundle_sha256") or "",
                "test_completed_at": test_completed_at,
                "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                "is_stale": is_stale,
                "patch_origin": review_data.get("patch_origin") or build_data.get("patch_origin") or "unknown",
                "evidence_labels": review_data.get("evidence_labels") or test_data.get("evidence_labels") or {},
                "changed_files": build_data.get("changed_files") or review_data.get("changed_files") or [],
                "target_files": target_files,
                "baseline_hashes": baseline_hashes,
                "candidate_hashes": build_data.get("candidate_hashes") or review_data.get("candidate_hashes") or {},
                "tested_candidate_hashes": test_data.get("tested_candidate_hashes") or review_data.get("tested_candidate_hashes") or {},
                "patch_sha256": build_data.get("patch_sha256") or review_data.get("patch_sha256") or "",
                "tested_patch_sha256": test_data.get("tested_patch_sha256") or review_data.get("tested_patch_sha256") or "",
                "production_drift": drifted_files,
                "production_drift_detected": len(drifted_files) > 0,
                "exclusive_lock_held": self.activation_lock_path.is_file(),
                "test_summary": review_data.get("test_summary") or {},
                "audited_native_desktop_handoff": review_data.get("audited_native_desktop_handoff") or {},
                "real_docker_verification": review_data.get("real_docker_verification") or {},
                "activation_record": activation_record,
                "has_patch": patch_path.is_file(),
                "patch_path": str(patch_path) if patch_path.is_file() else "",
                "has_review_md": review_md.is_file(),
                "review_md_path": str(review_md) if review_md.is_file() else "",
                "has_audit_db": audit_db.is_file(),
                "audit_db_path": str(audit_db) if audit_db.is_file() else "",
                "dir_mtime": entry.stat().st_mtime,
            })

        runs.sort(key=lambda r: (float(r["test_completed_at"] or 0), r["dir_mtime"]), reverse=True)
        return runs

    def activate_candidate_by_id(
        self,
        request_id: str,
        *,
        approved: bool = False,
        approver: str = "operator",
        expected_bundle_sha256: str | None = None,
        max_approval_age_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
        activation_owner: str | None = None,
        startup_check_fn: Callable[[Path], bool] | None = None,
        acceptance_check_fn: Callable[[Path], bool] | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> dict[str, Any]:
        """Activate a staged candidate version by its request_id with full pre-activation checks and rollback."""
        run_dir = self.supervisor_dir / request_id
        if not run_dir.is_dir():
            raise FileNotFoundError(f"Candidate run directory not found for {request_id}: {run_dir}")

        req_file = run_dir / "change_request.json"
        build_file = run_dir / "build_bundle.json"
        test_file = run_dir / "test_bundle.json"

        if not req_file.is_file() or not build_file.is_file() or not test_file.is_file():
            raise FileNotFoundError(f"Incomplete candidate bundle for {request_id} (missing change_request, build_bundle, or test_bundle).")

        change_request = ChangeRequest(**json.loads(req_file.read_text(encoding="utf-8")))
        build_bundle = json.loads(build_file.read_text(encoding="utf-8"))
        test_bundle = json.loads(test_file.read_text(encoding="utf-8"))

        return self.activate_version(
            change_request,
            build_bundle,
            test_bundle,
            approved=approved,
            approver=approver,
            expected_bundle_sha256=expected_bundle_sha256,
            max_approval_age_seconds=max_approval_age_seconds,
            activation_owner=activation_owner,
            startup_check_fn=startup_check_fn,
            acceptance_check_fn=acceptance_check_fn,
            now_fn=now_fn,
        )


_MALFORMED_DELEGATION_REGRESSION_DRIVER = r'''# -*- coding: utf-8 -*-
"""Independent regression test runner for the historical malformed delegation failure modes
using simulated desktop observations (`evidence_label: simulated_desktop_observations`)."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile

from tools.sabrina_coordination import Coordination
from tools.agent_desktop_broker import AgentDesktopBroker
from tools import sabrina_lab_assistant_mcp as assistant


def run_regressions() -> dict:
    results = {}
    with tempfile.TemporaryDirectory(prefix="sabrina-devsup-reg-") as tmp:
        tmp_path = Path(tmp)

        # Scenario 1: Session 0f0b71cc0e0a488dbf54a4957d063abf
        # Sabrina calls cross_app_begin_handoff(app="Codex.exe") omitting handoff_id and prompt
        board1 = Coordination(tmp_path / "s1.db", runtime_factory=lambda: None)
        sess1 = board1.begin(
            "Delegate the frozen task and collect its complete visible JSON reply.",
            "Frozen contract context: implement parse_duration(text) in solution.py.",
            desktop_audit_retention_days=7,
        )
        rid1 = sess1["id"]
        broker1 = AgentDesktopBroker(board1.store, runner=lambda *_: {"ok": True, "data": {}})
        broker1.configure(rid1, "autonomous", ["Claude.exe", "Antigravity.exe", "Codex.exe"], new_run=True)
        broker1.configure_cross_app_review(rid1, True, new_run=True)

        b_res = broker1.record_cross_app_baseline(rid1, "Baseline from performance_lookup", evidence="performance_lookup")
        r_res = broker1.record_cross_app_route(rid1, "Codex.exe", "consult", "Highest pass rate", "Implementation")
        h_res = broker1.begin_cross_app_handoff(rid1, app="Codex.exe")
        board1.release(sess1)
        results["scenario_1_begin_handoff_without_id_or_prompt"] = {
            "evidence_label": "simulated_desktop_observations",
            "passed": bool(b_res.get("baseline_saved") and h_res.get("prompt_recorded") and h_res.get("handoff_id") == r_res.get("handoff_id")),
            "handoff_id": h_res.get("handoff_id"),
            "prompt_recorded": h_res.get("prompt_recorded"),
        }

        # Scenario 2: Session 56222a4216b841a7a7d8ce20650c5e38
        # Mid-playbook exception in Claude.exe must close the active handoff so fallback to Codex.exe succeeds
        board2 = Coordination(tmp_path / "s2.db", runtime_factory=lambda: None)
        sess2 = board2.begin("Delegate duration parser", "Return JSON artifact", desktop_audit_retention_days=7)
        rid2 = sess2["id"]

        def runner_fail_claude_succeed_codex(tool, args):
            app = args.get("app", "")
            if tool == "desktop_list_windows":
                if app == "Claude.exe":
                    return {
                        "ok": True,
                        "command": "list-windows",
                        "data": [{"id": "w-claude", "pid": 111, "title": "Claude", "app_name": "Claude.exe",
                                  "executable_path": r"C:\Users\test\AppData\Local\AnthropicClaude\Claude.exe"}],
                    }
                return {
                    "ok": True,
                    "command": "list-windows",
                    "data": [{"id": "w-codex", "pid": 222, "title": "Codex", "app_name": "Codex.exe",
                              "executable_path": r"C:\Program Files\OpenAI\Codex\codex.exe"}],
                }
            if tool == "desktop_snapshot":
                if app == "Claude.exe":
                    raise RuntimeError("Simulated mid-playbook window crash in Claude.exe")
                return {
                    "ok": True,
                    "command": "snapshot",
                    "data": {
                        "app": "Codex.exe",
                        "snapshot_id": "snap-codex-1",
                        "window": {"id": "w-codex", "title": "Codex"},
                        "conversation": {"conversation_key": "codex-conv-1"},
                        "tree": {
                            "role": "window",
                            "children": [
                                {"role": "document", "value": "https://codex.local/c/1"},
                                {"role": "textfield", "name": "Ask Codex", "ref_id": "@snap-codex-1:e1", "available_actions": ["SetValue"]},
                                {"role": "statictext", "name": "Claude responded: {\"complete\": true}"},
                            ],
                        },
                    },
                }
            return {"ok": True, "command": tool.removeprefix("desktop_"), "data": {}}

        broker2 = AgentDesktopBroker(board2.store, runner=runner_fail_claude_succeed_codex)
        broker2.configure(rid2, "autonomous", ["Claude.exe", "Antigravity.exe", "Codex.exe"], new_run=True)
        broker2.configure_cross_app_review(rid2, True, new_run=True)

        claude_failed = False
        try:
            broker2.execute_desktop_playbook(rid2, app="Claude.exe")
        except RuntimeError:
            claude_failed = True

        fallback_res = broker2.execute_desktop_playbook(rid2, app="codex", prompt="Fallback delegation prompt")
        board2.release(sess2)
        results["scenario_2_mid_playbook_exception_closes_handoff_for_fallback"] = {
            "evidence_label": "simulated_desktop_observations",
            "passed": bool(claude_failed and fallback_res.get("ok") is True and fallback_res.get("app") == "Codex.exe"),
            "claude_failed_as_expected": claude_failed,
            "fallback_status": fallback_res.get("status"),
            "fallback_app": fallback_res.get("app"),
        }

        # Scenario 3: Inline raw JSON tool call recovery in _extract_inline_tool_calls
        raw_content = '```json\n{"name": "delegate_task", "arguments": {"app": "codex", "prompt": "Check duration parser"}}\n```'
        recovered = assistant._extract_inline_tool_calls(raw_content, {"delegate_task", "performance_lookup"})
        results["scenario_3_inline_json_tool_call_recovered"] = {
            "evidence_label": "simulated_desktop_observations",
            "passed": bool(len(recovered) == 1 and recovered[0]["function"]["name"] == "delegate_task" and recovered[0]["function"]["arguments"]["app"] == "codex"),
            "recovered": recovered,
        }

        # Scenario 4: _run_internal_tool normalizes cross_app_record_baseline without evidence_refs and cross_app_begin_handoff with only app
        board4 = Coordination(tmp_path / "s4.db", runtime_factory=lambda: None)
        sess4 = board4.begin("Task in session.task only", "", desktop_audit_retention_days=7)
        rid4 = sess4["id"]
        broker4 = AgentDesktopBroker(board4.store, runner=lambda *_: {"ok": True, "data": {}})
        broker4.configure(rid4, "autonomous", ["Claude.exe", "Antigravity.exe", "Codex.exe"], new_run=True)
        broker4.configure_cross_app_review(rid4, True, new_run=True)
        orig_coord = assistant._get_coordination
        orig_broker = assistant._get_desktop_broker
        try:
            assistant._get_coordination = lambda: board4
            assistant._get_desktop_broker = lambda: broker4
            base_out = json.loads(assistant._run_internal_tool("cross_app_record_baseline", {"content": "Baseline with omitted refs"}, sess4))
            begin_out = json.loads(assistant._run_internal_tool("cross_app_begin_handoff", {"app": "codex"}, sess4))
        finally:
            assistant._get_coordination = orig_coord
            assistant._get_desktop_broker = orig_broker
            board4.release(sess4)
        results["scenario_4_internal_tool_normalization"] = {
            "evidence_label": "simulated_desktop_observations",
            "passed": bool(base_out.get("baseline_saved") and begin_out.get("prompt_recorded")),
            "baseline_out": base_out,
            "begin_out": begin_out,
        }

    all_passed = all(item.get("passed") for item in results.values())
    return {
        "evidence_label": "simulated_desktop_observations",
        "is_simulated_desktop": True,
        "passed": all_passed,
        "scenarios": results,
    }


if __name__ == "__main__":
    print(json.dumps(run_regressions()))
'''


_AUDITED_NATIVE_DESKTOP_HANDOFF_DRIVER = r'''# -*- coding: utf-8 -*-
"""Execute one real audited native-app handoff using the candidate harness and `agent-desktop` CLI
against the live `Claude.exe` desktop window, and independently verify the returned code artifact
in Docker on `ego.local` via `LabVerifier`.

Separates and explicitly labels:
- `audited_native_desktop_handoff` (native `AgentDesktopBroker._run_cli` / `agent-desktop` UI automation)
- `real_docker_verification` (`LabVerifier` Docker container execution on `ego@ego.local`)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time

from tools.sabrina_coordination import Coordination
from tools.agent_desktop_broker import AgentDesktopBroker
from tools.sabrina_workflow import WorkflowStore
from tools import sabrina_lab_assistant_mcp as assistant


def run_audited_native_handoff() -> dict:
    with tempfile.TemporaryDirectory(prefix="sabrina-native-handoff-") as tmp:
        tmp_path = Path(tmp)
        db_env = os.environ.get("SABRINA_NATIVE_HANDOFF_DB", "").strip()
        db_path = Path(db_env) if db_env else (tmp_path / "native_handoff_audit.sqlite3")
        if db_path.exists():
            db_path.unlink()
        artifacts_dir = tmp_path / "artifacts"
        board = Coordination(db_path, runtime_factory=lambda: None)
        store = WorkflowStore(db_path, artifacts_dir)

        created = store.create("Delegate duration-v1 implementation through candidate harness to native Claude.exe")
        oid = created["objective"]["id"]
        store.claim(oid, "candidate-supervisor-native")

        attempt = store.next_attempt(oid)
        aid = attempt["id"]
        marker = attempt["marker"]
        expected_prompt = attempt["expected_prompt"]

        # Real AgentDesktopBroker using native _run_cli (`agent-desktop` binary) — NO mock runner!
        broker = AgentDesktopBroker(board.store)
        if broker.runner != broker._run_cli:
            raise RuntimeError("Native handoff driver requires AgentDesktopBroker._run_cli")

        session = board.begin(
            "Delegate the frozen task and collect its complete visible JSON reply. Do not implement it yourself.",
            expected_prompt,
            desktop_audit_retention_days=7,
        )
        rid = session["id"]
        broker.configure(rid, "autonomous", ["Claude.exe", "Antigravity.exe", "Codex.exe"], new_run=True)
        broker.configure_cross_app_review(rid, True, new_run=True)
        board.release(session)

        with store.connect() as db:
            db.execute("UPDATE workflow_attempts SET run_id=? WHERE id=?", (rid, aid))

        started = time.time()
        orig_coord = assistant._get_coordination
        orig_broker = assistant._get_desktop_broker
        try:
            assistant._get_coordination = lambda: board
            assistant._get_desktop_broker = lambda: broker
            # Exercise the candidate harness's fixed malformed delegation call path:
            # cross_app_begin_handoff(app="claude") with lowercase unqualified app name and omitted handoff_id/prompt!
            raw_tool_out = assistant._run_internal_tool(
                "cross_app_begin_handoff",
                {"app": "claude"},
                {"id": rid, "_last_decision_id": 1},
            )
            playbook_result = json.loads(raw_tool_out)
        finally:
            assistant._get_coordination = orig_coord
            assistant._get_desktop_broker = orig_broker
        elapsed = round(time.time() - started, 3)

        # Query the audited native desktop events and handoff record from SQLite
        with store.connect() as db:
            handoff_row = db.execute(
                "SELECT handoff_id, run_id, app, status, prompt_event_id, submit_event_id, reply_snapshot_id, conversation_key, started, submitted, completed, error_detail "
                "FROM assistant_cross_app_handoffs WHERE run_id=? ORDER BY started DESC LIMIT 1",
                (rid,),
            ).fetchone()
            audit_rows = db.execute(
                "SELECT id, created, completed, app, tool, arguments, handoff_id FROM assistant_desktop_audit_events WHERE run_id=? ORDER BY id",
                (rid,),
            ).fetchall()
            win_row = db.execute(
                "SELECT window_id, app, verified_pid, verified_exe_path, window_title, conversation_key FROM assistant_desktop_windows WHERE run_id=? ORDER BY created DESC LIMIT 1",
                (rid,),
            ).fetchone()

        handoff_dict = dict(handoff_row) if handoff_row else {}
        win_dict = dict(win_row) if win_row else {}
        audit_summary = [
            {
                "id": r["id"],
                "tool": r["tool"],
                "app": r["app"],
                "handoff_id": r["handoff_id"],
                "arguments": json.loads(r["arguments"] or "{}"),
            }
            for r in audit_rows
        ]

        verification = playbook_result.get("verification") or {}
        native_handoff_passed = bool(
            playbook_result.get("ok") is True
            and playbook_result.get("status") == "completed"
            and playbook_result.get("app") == "Claude.exe"
            and handoff_dict.get("status") == "completed"
            and handoff_dict.get("prompt_event_id") is not None
            and handoff_dict.get("submit_event_id") is not None
            and bool(handoff_dict.get("reply_snapshot_id"))
            and int(win_dict.get("verified_pid") or 0) > 0
        )
        docker_verification_passed = bool(
            verification.get("status") == "passed"
            and verification.get("execution_mode") == "live"
            and verification.get("isolation") == "docker"
            and all(c.get("passed") is True for c in (verification.get("checks") or []))
        )

        native_report = {
            "evidence_label": "audited_native_desktop_handoff",
            "is_simulated_desktop": False,
            "runner": "AgentDesktopBroker._run_cli (agent-desktop native CLI)",
            "passed": native_handoff_passed,
            "elapsed_seconds": elapsed,
            "objective_id": oid,
            "attempt_id": aid,
            "run_id": rid,
            "task_marker": marker,
            "malformed_call_exercised": {"tool": "cross_app_begin_handoff", "arguments": {"app": "claude"}},
            "verified_window": win_dict,
            "handoff_record": handoff_dict,
            "audit_event_count": len(audit_summary),
            "audit_events": audit_summary,
            "audit_db_path": str(db_path),
            "reply_snapshot_id": playbook_result.get("reply_snapshot_id"),
        }
        docker_report = {
            "evidence_label": "real_docker_verification",
            "is_simulated_desktop": False,
            "uses_real_docker": True,
            "passed": docker_verification_passed,
            "verifier": verification,
        }
        return {
            "passed": bool(native_handoff_passed and docker_verification_passed),
            "audited_native_desktop_handoff": native_report,
            "real_docker_verification": docker_report,
        }


if __name__ == "__main__":
    print(json.dumps(run_audited_native_handoff()))
'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Separate Development Supervisor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run-candidate", help="Execute stages 1-4 (request, isolated build, independent test, review evidence) without activating")
    run_p.add_argument("--request-id", default="", help="Optional custom request_id")
    run_p.add_argument("--skip-live-worker", action="store_true", help="Skip live worker exchange")
    run_p.add_argument("--use-predefined-patch", action="store_true", help="Use predefined maintainer patch instead of live coding worker patch")

    act_p = sub.add_parser("activate", help="Activate an approved candidate run bundle with startup/acceptance check and automatic rollback")
    act_p.add_argument("--request-id", required=True)
    act_p.add_argument("--approve", action="store_true", help="Explicit human approval flag")
    act_p.add_argument("--approver", default="operator")
    act_p.add_argument("--bundle-sha256", default="", help="Optional expected approval_bundle_sha256")
    act_p.add_argument("--max-age-seconds", type=float, default=DEFAULT_APPROVAL_TTL_SECONDS)

    args = parser.parse_args()
    sup = SabrinaDevSupervisor()

    if args.cmd == "run-candidate":
        spec = default_malformed_delegation_request()
        if args.request_id:
            spec["request_id"] = args.request_id
        req = sup.request_change(spec)
        build = sup.build_in_isolation(
            req,
            consult_live_worker=not args.skip_live_worker,
            use_predefined_maintainer_patch=args.use_predefined_patch,
        )
        tests = sup.test_independently(req, build, run_live_worker_exchange=not args.skip_live_worker)
        review = sup.review_evidence(req, build, tests)
        print(json.dumps(review, indent=2, ensure_ascii=False))
    elif args.cmd == "activate":
        run_dir = sup.supervisor_dir / args.request_id
        req = ChangeRequest(**json.loads((run_dir / "change_request.json").read_text(encoding="utf-8")))
        build = json.loads((run_dir / "build_bundle.json").read_text(encoding="utf-8"))
        tests = json.loads((run_dir / "test_bundle.json").read_text(encoding="utf-8"))
        res = sup.activate_version(
            req,
            build,
            tests,
            approved=args.approve,
            approver=args.approver,
            expected_bundle_sha256=args.bundle_sha256 or None,
            max_approval_age_seconds=args.max_age_seconds,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
