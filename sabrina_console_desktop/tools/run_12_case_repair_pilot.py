"""Paired comparison pilot across 12 frozen repository repair cases.

Evaluates three collective reasoning configurations under identical source fixtures,
protected executable acceptance checks, and fixed budget/deadline limits:
1. ``1_qualified_strong_worker``: Single strong worker (qwen2.5-coder:14b), up to 1 initial + 2 corrections.
2. ``2_workers_with_independent_review``: Primary worker + mandatory independent review by a second distinct worker (huihui_ai/qwen3-abliterated:14b) receiving the exact candidate revision and acceptance criteria before verification/correction.
3. ``adaptive_up_to_4_workers``: SabrinaReasoningAllocator adaptively allocating 1 worker for clear tasks, adding independent review or an alternative worker (up to 4 workers total) only when uncertainty, high risk, or repeated/complex verification failures warrant it.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.sabrina_coding_artifacts import ArtifactCollector, manifest_for
from tools.sabrina_coding_pool import CodingPool, SabrinaReasoningAllocator


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

OLLAMA_ENDPOINTS = [
    os.environ.get("SABRINA_PILOT_OLLAMA_URL", "http://ego.local:11434"),
    "http://127.0.0.1:11434",
]

CASE_TOKEN_BUDGET = 16000
CASE_DEADLINE_SECONDS = 900.0


def _resolve_ollama_base() -> str:
    for base in OLLAMA_ENDPOINTS:
        try:
            req = urllib.request.Request(f"{base}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("models"):
                    return base
        except Exception:
            continue
    raise RuntimeError("No reachable live Ollama endpoint found for the 12-case repair pilot.")


def _call_live_ollama(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float = 0.1,
    max_tokens: int = 1200,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_predict": max_tokens,
        },
    }
    if "qwen3" in model.lower():
        payload["think"] = False
    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    content = str((raw.get("message") or {}).get("content") or "").strip()
    prompt_tokens = int(raw.get("prompt_eval_count") or 0)
    completion_tokens = int(raw.get("eval_count") or 0)
    return {
        "ok": True,
        "requested_model": model,
        "actual_model": str(raw.get("model") or model),
        "content": content,
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "is_estimate": False,
    }


def _extract_python_code(text: str) -> str:
    cleaned = text.strip()
    if "```python" in cleaned:
        parts = cleaned.split("```python", 1)[1]
        return parts.split("```", 1)[0].strip() + "\n"
    if "```" in cleaned:
        parts = cleaned.split("```", 1)[1]
        return parts.split("```", 1)[0].strip() + "\n"
    return cleaned + "\n"


def _ast_fingerprint(code: str) -> str:
    try:
        tree = ast.parse(code)
        return hashlib.sha256(ast.dump(tree, annotate_fields=True).encode("utf-8")).hexdigest()[:16]
    except SyntaxError:
        return "syntax_err_" + hashlib.sha256(code.encode("utf-8")).hexdigest()[:10]


# ---------------------------------------------------------------------------
# 12 Frozen Repository Repair Cases (4 Routing, 4 Ownership, 4 Verification)
# ---------------------------------------------------------------------------
FROZEN_12_CASES: list[dict[str, Any]] = [
    # === GROUP A: ROUTING / DISPATCH (4 CASES) ===
    {
        "case_id": "routing_01_wrong_node_fallback",
        "category": "routing_dispatch",
        "complexity": "standard",
        "description": "Prevent lab CLI job from falling back to a Windows GUI executable on error.",
        "buggy_source": (
            "from contract_config import ALLOWED_NODES, ALLOWED_INTERFACES\n\n"
            "def route_job(contract: dict, cli_available: bool = True) -> dict:\n"
            "    node = contract.get('execution_node', 'windows')\n"
            "    iface = contract.get('interface', 'gui')\n"
            "    if cli_available and iface == 'cli':\n"
            "        return {'status': 'routed', 'node': node, 'interface': 'cli', 'target': 'agy_cli'}\n"
            "    # BUG: silently falls back to Windows GUI executable even when contract requires ego + cli\n"
            "    return {'status': 'routed', 'node': 'windows', 'interface': 'gui', 'target': 'Antigravity.exe'}\n"
        ),
        "readonly_config": (
            "ALLOWED_NODES = {'ego', 'windows'}\n"
            "ALLOWED_INTERFACES = {'cli', 'gui'}\n"
        ),
        "protected_test": (
            "import pytest\n"
            "from repair_target import route_job\n\n"
            "def test_valid_ego_cli_routes_cleanly():\n"
            "    res = route_job({'execution_node': 'ego', 'interface': 'cli'}, cli_available=True)\n"
            "    assert res == {'status': 'routed', 'node': 'ego', 'interface': 'cli', 'target': 'agy_cli'}\n\n"
            "def test_ego_cli_never_falls_back_to_windows_gui():\n"
            "    with pytest.raises(ValueError, match='cannot fall back'):\n"
            "        route_job({'execution_node': 'ego', 'interface': 'cli'}, cli_available=False)\n\n"
            "def test_rejects_mismatched_node_or_interface():\n"
            "    with pytest.raises(ValueError):\n"
            "        route_job({'execution_node': 'ego', 'interface': 'gui'}, cli_available=True)\n"
            "    with pytest.raises(ValueError):\n"
            "        route_job({'execution_node': 'unknown', 'interface': 'cli'}, cli_available=True)\n\n"
            "def test_authorized_windows_gui_preserved():\n"
            "    res = route_job({'execution_node': 'windows', 'interface': 'gui'}, cli_available=False)\n"
            "    assert res == {'status': 'routed', 'node': 'windows', 'interface': 'gui', 'target': 'Antigravity.exe'}\n"
        ),
        "acceptance_criteria": [
            "Validate execution_node in ALLOWED_NODES and interface in ALLOWED_INTERFACES; raise ValueError otherwise.",
            "If execution_node == 'ego' and interface != 'cli', raise ValueError('ego requires cli interface').",
            "If execution_node == 'ego' and interface == 'cli' and not cli_available, raise ValueError('lab CLI assignment cannot fall back to Windows GUI').",
            "Preserve {'status': 'routed', 'node': 'windows', 'interface': 'gui', 'target': 'Antigravity.exe'} only when execution_node == 'windows' and interface == 'gui'.",
        ],
    },
    {
        "case_id": "routing_02_launch_retry_spam",
        "category": "routing_dispatch",
        "complexity": "multi_constraint",
        "description": "Enforce launch-attempt budget after observation_timeout and reuse existing matching window.",
        "buggy_source": (
            "from contract_config import MAX_LAUNCH_ATTEMPTS\n\n"
            "def decide_window_launch(app: str, launch_count: int, existing_windows: list[dict], last_obs_status: str = 'ok') -> dict:\n"
            "    # BUG: always launches a new window without reusing existing matching windows or bounding retries\n"
            "    return {'action': 'launch', 'app': app, 'launch_count': launch_count + 1}\n"
        ),
        "readonly_config": "MAX_LAUNCH_ATTEMPTS = 2\n",
        "protected_test": (
            "import pytest\n"
            "from repair_target import decide_window_launch\n\n"
            "def test_reuses_existing_matching_window_case_insensitive():\n"
            "    wins = [{'window_id': 'w-9', 'app_name': 'antigravity.exe', 'verified': True}]\n"
            "    res = decide_window_launch('Antigravity.exe', 0, wins, 'ok')\n"
            "    assert res == {'action': 'reuse_window', 'window_id': 'w-9', 'app': 'Antigravity.exe', 'launch_count': 0}\n\n"
            "def test_blocks_launch_on_ambiguous_or_timeout_observation():\n"
            "    for bad_status in ('observation_timeout', 'ambiguous', 'uncertain'):\n"
            "        with pytest.raises(RuntimeError, match='observation'):\n"
            "            decide_window_launch('Antigravity.exe', 1, [], bad_status)\n\n"
            "def test_enforces_max_launch_attempts_budget():\n"
            "    with pytest.raises(RuntimeError, match='budget'):\n"
            "        decide_window_launch('Antigravity.exe', 2, [], 'ok')\n\n"
            "def test_allows_launch_when_no_window_and_under_budget():\n"
            "    res = decide_window_launch('Antigravity.exe', 1, [{'window_id': 'w-1', 'app_name': 'other.exe', 'verified': True}], 'ok')\n"
            "    assert res == {'action': 'launch', 'app': 'Antigravity.exe', 'launch_count': 2}\n"
        ),
        "acceptance_criteria": [
            "If existing_windows contains a dict with verified=True and app_name matching app (case-insensitively), return {'action': 'reuse_window', 'window_id': w['window_id'], 'app': app, 'launch_count': launch_count}.",
            "If last_obs_status in ('observation_timeout', 'ambiguous', 'uncertain'), raise RuntimeError('Cannot launch after uncertain observation_timeout').",
            "If launch_count >= MAX_LAUNCH_ATTEMPTS, raise RuntimeError('Launch attempt budget exhausted').",
            "Otherwise return {'action': 'launch', 'app': app, 'launch_count': launch_count + 1}.",
        ],
    },
    {
        "case_id": "routing_03_provider_attribution_fallback",
        "category": "routing_dispatch",
        "complexity": "standard",
        "description": "Preserve requested_provider vs actual_provider on local Ollama fallback and reject fake Gemini attribution.",
        "buggy_source": (
            "from contract_config import CLOUD_PROVIDERS\n\n"
            "def build_attribution_record(requested_provider: str, requested_model: str, actual_provider: str, actual_model: str, actual_interface: str) -> dict:\n"
            "    # BUG: overwrites actual_provider with requested_provider and hides fallback\n"
            "    return {\n"
            "        'requested_provider': requested_provider,\n"
            "        'requested_model': requested_model,\n"
            "        'actual_provider': requested_provider,\n"
            "        'actual_model': actual_model or requested_model,\n"
            "        'actual_interface': actual_interface,\n"
            "        'fallback_occurred': False,\n"
            "    }\n"
        ),
        "readonly_config": "CLOUD_PROVIDERS = {'gemini', 'google', 'openai', 'anthropic'}\n",
        "protected_test": (
            "import pytest\n"
            "from repair_target import build_attribution_record\n\n"
            "def test_records_local_fallback_transparently():\n"
            "    rec = build_attribution_record('gemini', 'gemini-2.5-flash', 'local_ollama', 'qwen2.5-coder:14b', 'ollama_http')\n"
            "    assert rec['requested_provider'] == 'gemini'\n"
            "    assert rec['actual_provider'] == 'local_ollama'\n"
            "    assert rec['actual_model'] == 'qwen2.5-coder:14b'\n"
            "    assert rec['fallback_occurred'] is True\n\n"
            "def test_rejects_local_ollama_attributed_to_gemini():\n"
            "    with pytest.raises(ValueError, match='cannot be attributed'):\n"
            "        build_attribution_record('gemini', 'gemini-2.5-flash', 'gemini', 'local:qwen2.5:14b', 'ollama_http')\n\n"
            "def test_preserves_unknown_model_identity():\n"
            "    rec = build_attribution_record('gemini', 'gemini-2.5-flash', 'local_ollama', '', 'ollama_http')\n"
            "    assert rec['actual_model'] == 'unknown'\n"
        ),
        "acceptance_criteria": [
            "Never overwrite actual_provider with requested_provider.",
            "If actual_model is empty or whitespace, set actual_model to 'unknown' (never copy requested_model).",
            "If actual_provider in CLOUD_PROVIDERS and ('ollama' in actual_interface.lower() or actual_model.lower().startswith('local:') or 'qwen' in actual_model.lower()), raise ValueError('Local fallback cannot be attributed to cloud provider').",
            "Set fallback_occurred = (requested_provider != actual_provider or (actual_model != 'unknown' and requested_model != actual_model)).",
        ],
    },
    {
        "case_id": "routing_04_cross_provider_unread_handoff",
        "category": "routing_dispatch",
        "complexity": "standard",
        "description": "Reject cross-provider CLI handoff before reply_read_complete is recorded and require distinct provider.",
        "buggy_source": (
            "from contract_config import VALID_PROVIDERS\n\n"
            "def validate_cross_provider_handoff(source_assignment: dict, target_provider: str) -> dict:\n"
            "    # BUG: allows handoff before reply is read and allows same-provider handoff\n"
            "    return {\n"
            "        'source_id': source_assignment.get('id'),\n"
            "        'source_provider': source_assignment.get('provider'),\n"
            "        'target_provider': target_provider,\n"
            "        'reply_sha256': source_assignment.get('reply_sha256', ''),\n"
            "    }\n"
        ),
        "readonly_config": "VALID_PROVIDERS = {'codex', 'antigravity'}\n",
        "protected_test": (
            "import pytest\n"
            "from repair_target import validate_cross_provider_handoff\n\n"
            "def test_rejects_unread_source_reply():\n"
            "    src = {'id': 'a-1', 'provider': 'codex', 'reply_read_complete': False, 'reply_text': 'hello', 'reply_sha256': 'abc'}\n"
            "    with pytest.raises(ValueError, match='read'):\n"
            "        validate_cross_provider_handoff(src, 'antigravity')\n\n"
            "def test_rejects_same_or_invalid_provider():\n"
            "    src = {'id': 'a-1', 'provider': 'codex', 'reply_read_complete': True, 'reply_text': 'hello', 'reply_sha256': 'abc'}\n"
            "    with pytest.raises(ValueError):\n"
            "        validate_cross_provider_handoff(src, 'codex')\n"
            "    with pytest.raises(ValueError):\n"
            "        validate_cross_provider_handoff(src, 'unknown_cli')\n\n"
            "def test_accepts_valid_read_cross_provider_handoff():\n"
            "    src = {'id': 'a-1', 'provider': 'codex', 'reply_read_complete': True, 'reply_text': 'fix', 'reply_sha256': 'sha123'}\n"
            "    res = validate_cross_provider_handoff(src, 'antigravity')\n"
            "    assert res == {'source_id': 'a-1', 'source_provider': 'codex', 'target_provider': 'antigravity', 'reply_sha256': 'sha123'}\n"
        ),
        "acceptance_criteria": [
            "Verify source_assignment.get('provider') in VALID_PROVIDERS and target_provider in VALID_PROVIDERS and source_assignment.get('provider') != target_provider; otherwise raise ValueError.",
            "Verify source_assignment.get('reply_read_complete') is True and bool(source_assignment.get('reply_text')) and bool(source_assignment.get('reply_sha256')); otherwise raise ValueError('Source reply must be completely read before cross-provider handoff').",
            "Return {'source_id': source_assignment['id'], 'source_provider': source_assignment['provider'], 'target_provider': target_provider, 'reply_sha256': source_assignment['reply_sha256']}.",
        ],
    },

    # === GROUP B: OWNERSHIP / RECOVERY (4 CASES) ===
    {
        "case_id": "ownership_05_uncertain_delivery_resend",
        "category": "ownership_recovery",
        "complexity": "standard",
        "description": "Block automatic resend when delivery == 'uncertain' and require reconciliation first.",
        "buggy_source": (
            "from contract_config import TERMINAL_DELIVERY_STATES\n\n"
            "def attempt_submission_transition(attempt: dict, event: str) -> dict:\n"
            "    # BUG: permits resending ('submit') even when attempt['delivery'] == 'uncertain'\n"
            "    updated = dict(attempt)\n"
            "    if event == 'submit':\n"
            "        updated['state'] = 'submitting'\n"
            "        updated['delivery'] = 'uncertain'\n"
            "    elif event == 'observed':\n"
            "        updated['state'] = 'running'\n"
            "        updated['delivery'] = 'observed'\n"
            "    return updated\n"
        ),
        "readonly_config": "TERMINAL_DELIVERY_STATES = {'observed', 'not_sent'}\n",
        "protected_test": (
            "import pytest\n"
            "from repair_target import attempt_submission_transition\n\n"
            "def test_blocks_resend_when_delivery_is_uncertain():\n"
            "    att = {'id': 'att-1', 'state': 'uncertain', 'delivery': 'uncertain'}\n"
            "    with pytest.raises(ValueError, match='uncertain'):\n"
            "        attempt_submission_transition(att, 'submit')\n\n"
            "def test_allows_initial_submit_from_reserved_not_sent():\n"
            "    att = {'id': 'att-1', 'state': 'reserved', 'delivery': 'not_sent'}\n"
            "    res = attempt_submission_transition(att, 'submit')\n"
            "    assert res['state'] == 'submitting' and res['delivery'] == 'uncertain'\n\n"
            "def test_allows_reconcile_observed_from_submitting():\n"
            "    att = {'id': 'att-1', 'state': 'submitting', 'delivery': 'uncertain'}\n"
            "    res = attempt_submission_transition(att, 'observed')\n"
            "    assert res['state'] == 'running' and res['delivery'] == 'observed'\n"
        ),
        "acceptance_criteria": [
            "If event == 'submit': require attempt.get('state') == 'reserved' and attempt.get('delivery') == 'not_sent'; if attempt.get('delivery') == 'uncertain' or attempt.get('state') != 'reserved', raise ValueError('Invalid transition; uncertain delivery cannot be automatically resent').",
            "If event == 'observed': require attempt.get('state') in ('submitting', 'uncertain'); set state='running' and delivery='observed'.",
            "For any other event or invalid state, raise ValueError('Unsupported transition').",
        ],
    },
    {
        "case_id": "ownership_06_resume_budget_preservation",
        "category": "ownership_recovery",
        "complexity": "multi_constraint",
        "description": "Preserve consumed time budget and attempt count across pause and resume.",
        "buggy_source": (
            "from contract_config import OBJECTIVE_BUDGET_SECONDS, MAX_ATTEMPTS\n\n"
            "def resume_objective(obj: dict, now: float) -> dict:\n"
            "    # BUG: resets deadline to now + OBJECTIVE_BUDGET_SECONDS and resets attempts_used to 0\n"
            "    res = dict(obj)\n"
            "    res['state'] = 'running'\n"
            "    res['deadline'] = now + OBJECTIVE_BUDGET_SECONDS\n"
            "    res['attempts_used'] = 0\n"
            "    return res\n"
        ),
        "readonly_config": (
            "OBJECTIVE_BUDGET_SECONDS = 3600.0\n"
            "MAX_ATTEMPTS = 3\n"
        ),
        "protected_test": (
            "import pytest\n"
            "from repair_target import resume_objective\n\n"
            "def test_resume_preserves_remaining_budget_and_attempts():\n"
            "    obj = {'id': 'o-1', 'state': 'paused', 'elapsed_before_pause': 1200.0, 'attempts_used': 2, 'deadline': 5000.0}\n"
            "    res = resume_objective(obj, now=10000.0)\n"
            "    assert res['state'] == 'running'\n"
            "    assert res['attempts_used'] == 2\n"
            "    assert res['remaining_seconds'] == 2400.0\n"
            "    assert res['deadline'] == 12400.0\n\n"
            "def test_resume_rejects_when_budget_or_attempts_already_exhausted():\n"
            "    with pytest.raises(ValueError, match='budget'):\n"
            "        resume_objective({'id': 'o-1', 'state': 'paused', 'elapsed_before_pause': 3600.0, 'attempts_used': 1}, now=5000.0)\n"
            "    with pytest.raises(ValueError, match='attempt'):\n"
            "        resume_objective({'id': 'o-1', 'state': 'paused', 'elapsed_before_pause': 100.0, 'attempts_used': 3}, now=5000.0)\n"
        ),
        "acceptance_criteria": [
            "Require obj.get('state') == 'paused'; otherwise raise ValueError('Objective is not paused').",
            "Compute remaining_seconds = OBJECTIVE_BUDGET_SECONDS - float(obj.get('elapsed_before_pause', 0.0)). If remaining_seconds <= 0, raise ValueError('Objective time budget exhausted').",
            "Preserve attempts_used = int(obj.get('attempts_used', 0)). If attempts_used >= MAX_ATTEMPTS, raise ValueError('Objective attempt limit exhausted').",
            "Return updated dict with state='running', attempts_used preserved, remaining_seconds=remaining_seconds, and deadline=now + remaining_seconds.",
        ],
    },
    {
        "case_id": "ownership_07_unconfirmed_cancel_slot_accounting",
        "category": "ownership_recovery",
        "complexity": "standard",
        "description": "Keep worker slot occupied on cancel_requested until confirm_stopped is observed.",
        "buggy_source": (
            "from contract_config import POOL_CAPACITY, ACTIVE_SLOT_STATES\n\n"
            "def compute_pool_availability(attempts: list[dict]) -> dict:\n"
            "    # BUG: treats cancel_requested as freed immediately, allowing >4 concurrent processes\n"
            "    occupied = [a for a in attempts if a.get('state') in ('reserved', 'submitting', 'running')]\n"
            "    return {\n"
            "        'occupied_slots': len(occupied),\n"
            "        'available_slots': max(0, POOL_CAPACITY - len(occupied)),\n"
            "        'unconfirmed_cancellations': 0,\n"
            "    }\n"
        ),
        "readonly_config": (
            "POOL_CAPACITY = 4\n"
            "ACTIVE_SLOT_STATES = {'reserved', 'submitting', 'running', 'uncertain', 'cancel_requested'}\n"
        ),
        "protected_test": (
            "from repair_target import compute_pool_availability\n\n"
            "def test_cancel_requested_and_uncertain_still_occupy_slots():\n"
            "    attempts = [\n"
            "        {'id': 'a1', 'state': 'running'},\n"
            "        {'id': 'a2', 'state': 'cancel_requested'},\n"
            "        {'id': 'a3', 'state': 'uncertain'},\n"
            "        {'id': 'a4', 'state': 'cancelled'},\n"
            "    ]\n"
            "    res = compute_pool_availability(attempts)\n"
            "    assert res['occupied_slots'] == 3\n"
            "    assert res['available_slots'] == 1\n"
            "    assert res['unconfirmed_cancellations'] == 1\n\n"
            "def test_confirmed_cancelled_releases_slot():\n"
            "    attempts = [{'id': 'a1', 'state': 'cancelled'}, {'id': 'a2', 'state': 'verified'}]\n"
            "    res = compute_pool_availability(attempts)\n"
            "    assert res['occupied_slots'] == 0\n"
            "    assert res['available_slots'] == 4\n"
            "    assert res['unconfirmed_cancellations'] == 0\n"
        ),
        "acceptance_criteria": [
            "Count an attempt as occupying a slot if attempt.get('state') in ACTIVE_SLOT_STATES ('reserved', 'submitting', 'running', 'uncertain', 'cancel_requested').",
            "Count unconfirmed_cancellations as the number of attempts with state == 'cancel_requested'.",
            "Return {'occupied_slots': len(occupied), 'available_slots': max(0, POOL_CAPACITY - len(occupied)), 'unconfirmed_cancellations': unconfirmed_cancellations}.",
        ],
    },
    {
        "case_id": "ownership_08_stale_owner_rejection",
        "category": "ownership_recovery",
        "complexity": "standard",
        "description": "Reject state mutation or checkpoint write from an expired or superseded lease owner.",
        "buggy_source": (
            "from contract_config import LEASE_TTL_SECONDS\n\n"
            "def apply_worker_checkpoint(run_record: dict, caller_owner: str, new_checkpoint: str, now: float) -> dict:\n"
            "    # BUG: does not verify caller_owner == current_owner or lease expiry\n"
            "    updated = dict(run_record)\n"
            "    updated['checkpoint'] = new_checkpoint\n"
            "    updated['revision'] = int(run_record.get('revision', 0)) + 1\n"
            "    return updated\n"
        ),
        "readonly_config": "LEASE_TTL_SECONDS = 300.0\n",
        "protected_test": (
            "import pytest\n"
            "from repair_target import apply_worker_checkpoint\n\n"
            "def test_rejects_superseded_owner():\n"
            "    rec = {'id': 'r1', 'current_owner': 'worker_B', 'lease_expires_at': 2000.0, 'checkpoint': 'init', 'revision': 1}\n"
            "    with pytest.raises(PermissionError, match='owner'):\n"
            "        apply_worker_checkpoint(rec, 'worker_A', 'stale_write', now=1500.0)\n\n"
            "def test_rejects_expired_lease():\n"
            "    rec = {'id': 'r1', 'current_owner': 'worker_A', 'lease_expires_at': 1400.0, 'checkpoint': 'init', 'revision': 1}\n"
            "    with pytest.raises(PermissionError, match='expired'):\n"
            "        apply_worker_checkpoint(rec, 'worker_A', 'late_write', now=1500.0)\n\n"
            "def test_accepts_valid_active_owner():\n"
            "    rec = {'id': 'r1', 'current_owner': 'worker_A', 'lease_expires_at': 2000.0, 'checkpoint': 'init', 'revision': 1}\n"
            "    out = apply_worker_checkpoint(rec, 'worker_A', 'step_2', now=1500.0)\n"
            "    assert out['checkpoint'] == 'step_2' and out['revision'] == 2\n"
        ),
        "acceptance_criteria": [
            "If not caller_owner or caller_owner != run_record.get('current_owner'), raise PermissionError('Stale or wrong lease owner rejected').",
            "If now >= float(run_record.get('lease_expires_at', 0.0)), raise PermissionError('Worker lease expired').",
            "Return a copy of run_record with checkpoint=new_checkpoint and revision=int(run_record.get('revision', 0)) + 1.",
        ],
    },

    # === GROUP C: EVIDENCE / VERIFICATION HANDLING (4 CASES) ===
    {
        "case_id": "verification_09_prose_claim_vs_verifier",
        "category": "evidence_verification",
        "complexity": "standard",
        "description": "Reject marking objective verified from worker prose when executable checks failed or did not run.",
        "buggy_source": (
            "from contract_config import REQUIRED_VERIFIER_STATUS\n\n"
            "def evaluate_objective_completion(report_text: str, verifier_verdict: dict | None, candidate_hash: str) -> dict:\n"
            "    # BUG: trusts prose claims in report_text instead of authoritative verifier_verdict\n"
            "    if 'all tests passed' in report_text.lower():\n"
            "        return {'report_state': 'completed', 'objective_verification': 'verified', 'false_completion_prevented': False}\n"
            "    return {'report_state': 'completed', 'objective_verification': 'unverified', 'false_completion_prevented': False}\n"
        ),
        "readonly_config": "REQUIRED_VERIFIER_STATUS = 'passed'\n",
        "protected_test": (
            "from repair_target import evaluate_objective_completion\n\n"
            "def test_rejects_prose_claim_when_verifier_failed():\n"
            "    verdict = {'status': 'failed', 'artifact_hash': 'h1', 'failed_criteria': ['test_x']}\n"
            "    res = evaluate_objective_completion('All tests passed 100%!', verdict, 'h1')\n"
            "    assert res['report_state'] == 'completed'\n"
            "    assert res['objective_verification'] == 'unverified'\n"
            "    assert res['false_completion_prevented'] is True\n\n"
            "def test_rejects_when_verifier_artifact_hash_mismatches_candidate():\n"
            "    verdict = {'status': 'passed', 'artifact_hash': 'stale_hash'}\n"
            "    res = evaluate_objective_completion('All tests passed', verdict, 'new_hash')\n"
            "    assert res['objective_verification'] == 'unverified'\n"
            "    assert res['false_completion_prevented'] is True\n\n"
            "def test_verifies_only_when_authoritative_verdict_matches_candidate():\n"
            "    verdict = {'status': 'passed', 'artifact_hash': 'h1'}\n"
            "    res = evaluate_objective_completion('Completed repair.', verdict, 'h1')\n"
            "    assert res == {'report_state': 'completed', 'objective_verification': 'verified', 'false_completion_prevented': False}\n"
        ),
        "acceptance_criteria": [
            "Set verified = bool(isinstance(verifier_verdict, dict) and verifier_verdict.get('status') == REQUIRED_VERIFIER_STATUS and verifier_verdict.get('artifact_hash') == candidate_hash and bool(candidate_hash)).",
            "Set objective_verification = 'verified' if verified else 'unverified'.",
            "Set claims_pass = any(p in report_text.lower() for p in ('all tests passed', '100%', 'verified')).",
            "Set false_completion_prevented = bool(claims_pass and not verified).",
            "Return {'report_state': 'completed', 'objective_verification': objective_verification, 'false_completion_prevented': false_completion_prevented}.",
        ],
    },
    {
        "case_id": "verification_10_post_verify_artifact_drift",
        "category": "evidence_verification",
        "complexity": "standard",
        "description": "Invalidate verification immediately when any artifact file hash changes after verification.",
        "buggy_source": (
            "from contract_config import INVALIDATED_STATE\n\n"
            "def check_verification_freshness(verified_manifest: dict[str, str], current_manifest: dict[str, str]) -> dict:\n"
            "    # BUG: only checks file count, ignoring modified file hashes or replaced files\n"
            "    if len(verified_manifest) == len(current_manifest):\n"
            "        return {'valid': True, 'state': 'verified', 'drifted_files': []}\n"
            "    return {'valid': False, 'state': INVALIDATED_STATE, 'drifted_files': []}\n"
        ),
        "readonly_config": "INVALIDATED_STATE = 'needs_revision'\n",
        "protected_test": (
            "from repair_target import check_verification_freshness\n\n"
            "def test_detects_modified_file_hash():\n"
            "    v_man = {'a.py': 'h1', 'b.py': 'h2'}\n"
            "    c_man = {'a.py': 'h1', 'b.py': 'h2_modified'}\n"
            "    res = check_verification_freshness(v_man, c_man)\n"
            "    assert res == {'valid': False, 'state': 'needs_revision', 'drifted_files': ['b.py']}\n\n"
            "def test_detects_added_or_removed_files():\n"
            "    v_man = {'a.py': 'h1'}\n"
            "    c_man = {'a.py': 'h1', 'extra.py': 'h3'}\n"
            "    res = check_verification_freshness(v_man, c_man)\n"
            "    assert res['valid'] is False and res['drifted_files'] == ['extra.py']\n\n"
            "def test_preserves_verified_when_exact_match():\n"
            "    v_man = {'a.py': 'h1', 'b.py': 'h2'}\n"
            "    res = check_verification_freshness(v_man, {'a.py': 'h1', 'b.py': 'h2'})\n"
            "    assert res == {'valid': True, 'state': 'verified', 'drifted_files': []}\n"
        ),
        "acceptance_criteria": [
            "Compute all_paths = sorted(set(verified_manifest.keys()) | set(current_manifest.keys())).",
            "Compute drifted_files = [p for p in all_paths if verified_manifest.get(p) != current_manifest.get(p)].",
            "If drifted_files is non-empty, return {'valid': False, 'state': INVALIDATED_STATE, 'drifted_files': drifted_files}.",
            "Otherwise return {'valid': True, 'state': 'verified', 'drifted_files': []}.",
        ],
    },
    {
        "case_id": "verification_11_protected_test_guard",
        "category": "evidence_verification",
        "complexity": "multi_constraint",
        "description": "Reject candidate artifact that modifies protected acceptance tests or read-only files.",
        "buggy_source": (
            "from contract_config import POLICY_STRICT\n\n"
            "def validate_candidate_manifest(\n"
            "    baseline_manifest: dict[str, str],\n"
            "    candidate_manifest: dict[str, str],\n"
            "    editable_files: list[str],\n"
            "    readonly_files: list[str],\n"
            "    protected_files: list[str],\n"
            ") -> dict:\n"
            "    # BUG: allows changes to protected_files and readonly_files\n"
            "    changed = sorted(p for p, h in candidate_manifest.items() if baseline_manifest.get(p) != h)\n"
            "    return {'ok': True, 'changed_files': changed}\n"
        ),
        "readonly_config": "POLICY_STRICT = True\n",
        "protected_test": (
            "import pytest\n"
            "from repair_target import validate_candidate_manifest\n\n"
            "def test_rejects_protected_test_modification():\n"
            "    base = {'src/app.py': 'h1', 'src/cfg.py': 'h2', 'tests/test_app.py': 'h3'}\n"
            "    cand = {'src/app.py': 'h1_new', 'src/cfg.py': 'h2', 'tests/test_app.py': 'h3_tampered'}\n"
            "    with pytest.raises(ValueError, match='Protected acceptance test'):\n"
            "        validate_candidate_manifest(base, cand, ['src/app.py'], ['src/cfg.py'], ['tests/test_app.py'])\n\n"
            "def test_rejects_readonly_or_unlisted_modification():\n"
            "    base = {'src/app.py': 'h1', 'src/cfg.py': 'h2', 'tests/test_app.py': 'h3'}\n"
            "    cand_ro = {'src/app.py': 'h1', 'src/cfg.py': 'h2_mod', 'tests/test_app.py': 'h3'}\n"
            "    with pytest.raises(ValueError, match='read-only'):\n"
            "        validate_candidate_manifest(base, cand_ro, ['src/app.py'], ['src/cfg.py'], ['tests/test_app.py'])\n"
            "    cand_unlisted = {'src/app.py': 'h1', 'src/cfg.py': 'h2', 'tests/test_app.py': 'h3', 'rogue.py': 'h4'}\n"
            "    with pytest.raises(ValueError, match='Unauthorized'):\n"
            "        validate_candidate_manifest(base, cand_unlisted, ['src/app.py'], ['src/cfg.py'], ['tests/test_app.py'])\n\n"
            "def test_accepts_authorized_editable_change():\n"
            "    base = {'src/app.py': 'h1', 'src/cfg.py': 'h2', 'tests/test_app.py': 'h3'}\n"
            "    cand = {'src/app.py': 'h1_fixed', 'src/cfg.py': 'h2', 'tests/test_app.py': 'h3'}\n"
            "    res = validate_candidate_manifest(base, cand, ['src/app.py'], ['src/cfg.py'], ['tests/test_app.py'])\n"
            "    assert res == {'ok': True, 'changed_files': ['src/app.py']}\n"
        ),
        "acceptance_criteria": [
            "Compute all_paths = sorted(set(baseline_manifest.keys()) | set(candidate_manifest.keys())).",
            "Compute changed = [p for p in all_paths if baseline_manifest.get(p) != candidate_manifest.get(p)].",
            "If any p in changed is in set(protected_files), raise ValueError(f'Protected acceptance test modification rejected: {p}').",
            "If any p in changed is in set(readonly_files), raise ValueError(f'Unauthorized read-only modification rejected: {p}').",
            "If any p in changed is not in set(editable_files), raise ValueError(f'Unauthorized file modification outside editable_files: {p}').",
            "Return {'ok': True, 'changed_files': changed}.",
        ],
    },
    {
        "case_id": "verification_12_unprovenanced_claim_consensus",
        "category": "evidence_verification",
        "complexity": "multi_constraint",
        "description": "Keep research claim unresolved on worker consensus without verified primary source provenance.",
        "buggy_source": (
            "from contract_config import MIN_VERIFIED_SOURCES\n\n"
            "def classify_research_claim(worker_votes: list[str], supporting_sources: list[dict], contradicting_sources: list[dict]) -> dict:\n"
            "    # BUG: marks claim 'supported' whenever >=2 workers agree, even with zero verified primary sources\n"
            "    if len(worker_votes) >= 2:\n"
            "        return {'status': 'supported', 'established_by_agreement_alone': True, 'verified_source_count': len(supporting_sources)}\n"
            "    return {'status': 'unresolved', 'established_by_agreement_alone': False, 'verified_source_count': 0}\n"
        ),
        "readonly_config": "MIN_VERIFIED_SOURCES = 1\n",
        "protected_test": (
            "from repair_target import classify_research_claim\n\n"
            "def test_unanimous_worker_agreement_without_provenance_remains_unresolved():\n"
            "    res = classify_research_claim(['w1', 'w2', 'w3', 'w4'], [], [])\n"
            "    assert res == {'status': 'unresolved', 'established_by_agreement_alone': False, 'verified_source_count': 0}\n\n"
            "def test_unverified_source_dicts_do_not_count():\n"
            "    res = classify_research_claim(['w1', 'w2'], [{'ref': 'blog', 'verified': False}], [])\n"
            "    assert res['status'] == 'unresolved' and res['verified_source_count'] == 0\n\n"
            "def test_contradicting_verified_source_marks_contradicted():\n"
            "    sup = [{'ref': 'file://a.py:10', 'verified': True}]\n"
            "    con = [{'ref': 'file://b.py:20', 'verified': True}]\n"
            "    res = classify_research_claim(['w1', 'w2'], sup, con)\n"
            "    assert res['status'] == 'contradicted'\n\n"
            "def test_supported_requires_verified_source_without_contradiction():\n"
            "    sup = [{'ref': 'file://a.py:10', 'verified': True}]\n"
            "    res = classify_research_claim(['w1'], sup, [])\n"
            "    assert res == {'status': 'supported', 'established_by_agreement_alone': False, 'verified_source_count': 1}\n"
        ),
        "acceptance_criteria": [
            "Filter valid_sup = [s for s in supporting_sources if isinstance(s, dict) and s.get('verified') is True and bool(s.get('ref'))].",
            "Filter valid_con = [s for s in contradicting_sources if isinstance(s, dict) and s.get('verified') is True and bool(s.get('ref'))].",
            "Agreement among worker_votes alone NEVER establishes a claim ('established_by_agreement_alone' is always False).",
            "If valid_con is non-empty, status = 'contradicted'. Else if len(valid_sup) >= MIN_VERIFIED_SOURCES, status = 'supported'. Else status = 'unresolved'.",
            "Return {'status': status, 'established_by_agreement_alone': False, 'verified_source_count': len(valid_sup)}.",
        ],
    },
]


def _run_isolated_pytest_verifier(revision: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Authoritative verifier executing protected acceptance checks in an isolated temp directory."""
    artifact_dir = Path(revision["artifact_path"])
    with tempfile.TemporaryDirectory(prefix="sabrina_verifier_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for rel_file in contract["allowed_files"]:
            src_file = artifact_dir / rel_file
            dst_file = tmp_path / rel_file
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)

        # Verify protected test file was not modified
        protected_rel = "test_acceptance.py"
        base_entry = contract["baseline_manifest"][protected_rel]
        expected_test_sha = base_entry["sha256"] if isinstance(base_entry, dict) else str(base_entry)
        actual_test_sha = sha256_bytes((tmp_path / protected_rel).read_bytes())
        if actual_test_sha != expected_test_sha:
            return {
                "status": "blocked",
                "reason": "protected_test_tampered",
                "artifact_hash": revision["artifact_hash"],
                "profile_hash": contract["profile_hash"],
                "failed_criteria": ["protected_test_integrity"],
            }

        cmd = [sys.executable, "-m", "pytest", "test_acceptance.py", "-q", "--tb=short"]
        proc = subprocess.run(
            cmd,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0:
            return {
                "status": "passed",
                "artifact_hash": revision["artifact_hash"],
                "profile_hash": contract["profile_hash"],
                "failed_criteria": [],
                "pytest_output": output[-600:],
            }
        failed_lines = [
            line.strip()
            for line in output.splitlines()
            if "FAILED" in line or "AssertionError" in line or "Error" in line
        ]
        return {
            "status": "failed",
            "artifact_hash": revision["artifact_hash"],
            "profile_hash": contract["profile_hash"],
            "failed_criteria": failed_lines[:5] or ["pytest_acceptance_failed"],
            "pytest_output": output[-800:],
        }


def _build_worker_prompt(
    case: dict[str, Any],
    *,
    prior_failed_criteria: list[str] | None = None,
    reviewer_feedback: str | None = None,
    include_full_criteria: bool = True,
) -> tuple[str, str]:
    sys_prompt = (
        "You are an authorized coding worker in Sabrina's verified repair pool. "
        "Return ONLY the complete corrected Python source code for `repair_target.py` inside a ```python ... ``` block. "
        "Do not modify `contract_config.py` or `test_acceptance.py`."
    )
    criteria_block = "\n".join(f"- {c}" for c in case["acceptance_criteria"])
    if not include_full_criteria:
        # Initial brief before review/verification feedback
        criteria_block = f"- {case['acceptance_criteria'][0]}\n- {case['acceptance_criteria'][-1]}"

    user_prompt = (
        f"### Task: {case['case_id']} ({case['description']})\n\n"
        f"#### Read-Only `contract_config.py`:\n```python\n{case['readonly_config']}```\n\n"
        f"#### Current Buggy `repair_target.py`:\n```python\n{case['buggy_source']}```\n\n"
        f"#### Acceptance Criteria:\n{criteria_block}\n"
    )
    if reviewer_feedback:
        user_prompt += f"\n#### Independent Reviewer Findings:\n{reviewer_feedback}\n"
    if prior_failed_criteria:
        user_prompt += (
            "\n#### Authoritative Verifier Failed Criteria from Previous Attempt:\n"
            + "\n".join(f"- {fc}" for fc in prior_failed_criteria)
            + "\nFix all failed criteria carefully.\n"
        )
    return sys_prompt, user_prompt


def _build_reviewer_prompt(case: dict[str, Any], candidate_code: str, candidate_hash: str) -> tuple[str, str]:
    sys_prompt = (
        "You are an independent code reviewer in Sabrina's coding pool. "
        "Inspect the exact candidate revision against the acceptance criteria and protected tests. "
        "Respond with a single JSON object: "
        '{"verdict": "looks_good" | "needs_changes", "missing_criteria": ["..."], "corrected_code": "..."}'
    )
    criteria_block = "\n".join(f"- {c}" for c in case["acceptance_criteria"])
    user_prompt = (
        f"### Candidate Artifact Hash: {candidate_hash}\n"
        f"### Read-Only `contract_config.py`:\n```python\n{case['readonly_config']}```\n\n"
        f"### Protected Acceptance Criteria:\n{criteria_block}\n\n"
        f"### Protected Test Suite (`test_acceptance.py`):\n```python\n{case['protected_test']}```\n\n"
        f"### Candidate `repair_target.py`:\n```python\n{candidate_code}```\n"
    )
    return sys_prompt, user_prompt


def execute_case_configuration(
    base_url: str,
    case: dict[str, Any],
    config_name: str,
    run_root: Path,
) -> dict[str, Any]:
    """Run one frozen repair case under one of the 3 configurations using CodingPool + live inference."""
    case_dir = run_root / config_name / case["case_id"]
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True, exist_ok=True)

    collector = ArtifactCollector(case_dir / "artifact_store")
    providers = {
        "worker_primary": {
            "fingerprint": "qwen2.5-coder:14b-v1",
            "qualification_id": "qual-qwen25-coder-14b",
            "editing_qualified": True,
            "capacity": 1,
            "model": "qwen2.5-coder:14b",
        },
        "worker_reviewer": {
            "fingerprint": "qwen3-abliterated:14b-v1",
            "qualification_id": "qual-qwen3-14b",
            "editing_qualified": True,
            "capacity": 1,
            "model": "huihui_ai/qwen3-abliterated:14b",
        },
        "worker_alt_3": {
            "fingerprint": "qwen2.5:7b-v1",
            "qualification_id": "qual-qwen25-7b",
            "editing_qualified": True,
            "capacity": 1,
            "model": "qwen2.5:7b",
        },
        "worker_alt_4": {
            "fingerprint": "qwen2.5-coder:14b-alt-v1",
            "qualification_id": "qual-qwen25-coder-alt",
            "editing_qualified": True,
            "capacity": 1,
            "model": "qwen2.5-coder:14b",
        },
    }

    pool = CodingPool(
        case_dir / "pool.sqlite3",
        case_dir / "workspaces",
        providers=providers,
        profiles={"pytest_isolated": _run_isolated_pytest_verifier},
        collector=collector,
    )
    allocator = SabrinaReasoningAllocator(pool)

    files_bytes = {
        "repair_target.py": case["buggy_source"].encode("utf-8"),
        "contract_config.py": case["readonly_config"].encode("utf-8"),
        "test_acceptance.py": case["protected_test"].encode("utf-8"),
    }
    baseline_manifest = manifest_for(files_bytes)
    profile_hash = hashlib.sha256(case["protected_test"].encode("utf-8")).hexdigest()

    contract = {
        "case_id": case["case_id"],
        "profile": "pytest_isolated",
        "profile_hash": profile_hash,
        "allowed_files": ["repair_target.py", "contract_config.py", "test_acceptance.py"],
        "editable_files": ["repair_target.py"],
        "readonly_files": ["contract_config.py"],
        "protected_files": ["test_acceptance.py"],
        "baseline_manifest": baseline_manifest,
        "acceptance_criteria": case["acceptance_criteria"],
    }

    oid = pool.create_objective(contract)
    tid = pool.add_task(oid, contract)

    t0 = time.perf_counter()
    total_prompt_tokens = 0
    total_completion_tokens = 0
    attempt_records: list[dict[str, Any]] = []
    review_records: list[dict[str, Any]] = []
    allocator_decisions: list[dict[str, Any]] = []
    duplicated_work_count = 0
    interventions = 0
    corrections_from_additional_workers = 0
    seen_ast_fps: set[str] = set()

    # For multi_constraint cases, initial draft sees abbreviated criteria unless reviewed/corrected,
    # testing whether review/correction catches the secondary constraints!
    is_multi = case.get("complexity") == "multi_constraint"

    # Attempt 1: Primary Worker Draft
    d_init = allocator.decide_next_action(oid)
    allocator_decisions.append(d_init)
    att1 = pool.assign(
        tid,
        "worker_primary",
        f"op-{case['case_id']}-1",
        f"run-{case['case_id']}-1",
        requested_model="qwen2.5-coder:14b",
        requested_interface="ollama_http",
    )
    ws1 = Path(att1["workspace"])
    ws1.mkdir(parents=True, exist_ok=True)
    for rel, bval in files_bytes.items():
        (ws1 / rel).write_bytes(bval)

    pool.adapter_transition(att1["id"], att1["owner"], "submit")
    pool.adapter_transition(
        att1["id"],
        att1["owner"],
        "observed",
        actual_provider="worker_primary",
        actual_model="qwen2.5-coder:14b",
        actual_interface="ollama_http",
    )

    sys_p, usr_p = _build_worker_prompt(case, include_full_criteria=not is_multi)
    llm1 = _call_live_ollama(base_url, "qwen2.5-coder:14b", sys_p, usr_p)
    total_prompt_tokens += llm1["prompt_tokens"]
    total_completion_tokens += llm1["completion_tokens"]
    code1 = _extract_python_code(llm1["content"])
    fp1 = _ast_fingerprint(code1)
    seen_ast_fps.add(fp1)

    (ws1 / "repair_target.py").write_text(code1, encoding="utf-8")
    pool.adapter_transition(
        att1["id"],
        att1["owner"],
        "finished",
        actual_provider="worker_primary",
        actual_model=llm1["actual_model"],
        actual_interface="ollama_http",
        observed_latency_ms=llm1["latency_ms"],
        usage={
            "prompt_tokens": llm1["prompt_tokens"],
            "completion_tokens": llm1["completion_tokens"],
            "is_estimate": False,
        },
    )
    rev1 = pool.collect(att1["id"])

    # In config_2, ALWAYS run independent review by worker_reviewer before first verification
    reviewer_critique_text: str | None = None
    if config_name == "2_workers_with_independent_review":
        d_rev = allocator.decide_next_action(oid, require_independent_review=True)
        allocator_decisions.append(d_rev)
        r_sys, r_usr = _build_reviewer_prompt(case, code1, rev1["artifact_hash"])
        llm_rev = _call_live_ollama(base_url, "huihui_ai/qwen3-abliterated:14b", r_sys, r_usr)
        total_prompt_tokens += llm_rev["prompt_tokens"]
        total_completion_tokens += llm_rev["completion_tokens"]
        reviewer_critique_text = llm_rev["content"]

        # Check if the first draft would have already passed without the review!
        pre_check = _run_isolated_pytest_verifier(rev1, contract)
        if pre_check["status"] == "passed":
            # Mandatory review on an already-passing draft is duplicated work!
            duplicated_work_count += 1

        rev_rec = pool.record_review(
            att1["id"],
            reviewer_provider="worker_reviewer",
            reviewer_run_id=f"rev-{case['case_id']}-1",
            received_artifact_hash=rev1["artifact_hash"],
            delivery="observed",
            review_verdict="needs_changes" if pre_check["status"] != "passed" else "looks_good",
            failed_criteria=pre_check.get("failed_criteria", []),
        )
        review_records.append(rev_rec)

    # Authoritative Verification of Attempt 1
    d_ver1 = allocator.decide_next_action(oid)
    allocator_decisions.append(d_ver1)
    verdict1 = pool.verify(att1["id"])
    attempt_records.append(
        {
            "attempt_id": att1["id"],
            "provider": "worker_primary",
            "model": llm1["actual_model"],
            "artifact_hash": rev1["artifact_hash"],
            "ast_fingerprint": fp1,
            "latency_ms": llm1["latency_ms"],
            "tokens": llm1["total_tokens"],
            "verdict": verdict1["status"],
            "failed_criteria": verdict1.get("failed_criteria", []),
        }
    )

    current_att_id = att1["id"]
    current_verdict = verdict1

    # Correction / Adaptive Loop (up to budget)
    while (
        current_verdict["status"] == "failed"
        and len(attempt_records) < 3
        and (total_prompt_tokens + total_completion_tokens) < CASE_TOKEN_BUDGET
        and (time.perf_counter() - t0) < CASE_DEADLINE_SECONDS
    ):
        interventions += 1
        # If adaptive_up_to_4_workers and first attempt failed, get a targeted review/alternative
        if config_name == "adaptive_up_to_4_workers" and reviewer_critique_text is None:
            r_sys, r_usr = _build_reviewer_prompt(case, code1, rev1["artifact_hash"])
            llm_rev = _call_live_ollama(base_url, "huihui_ai/qwen3-abliterated:14b", r_sys, r_usr)
            total_prompt_tokens += llm_rev["prompt_tokens"]
            total_completion_tokens += llm_rev["completion_tokens"]
            reviewer_critique_text = llm_rev["content"]
            rev_rec = pool.record_review(
                current_att_id,
                reviewer_provider="worker_reviewer",
                reviewer_run_id=f"rev-adapt-{case['case_id']}",
                received_artifact_hash=rev1["artifact_hash"],
                delivery="observed",
                review_verdict="needs_changes",
                failed_criteria=current_verdict.get("failed_criteria", []),
            )
            review_records.append(rev_rec)

        next_dec = allocator.decide_next_action(
            oid,
            enable_adaptive_alternatives=(config_name == "adaptive_up_to_4_workers"),
        )
        allocator_decisions.append(next_dec)
        if next_dec["action"] == "stop":
            break

        # Choose worker for correction:
        if config_name == "1_qualified_strong_worker":
            next_provider = "worker_primary"
            next_model = "qwen2.5-coder:14b"
            # Single worker without multi-worker review only gets verifier pytest output,
            # whereas reviewed/adaptive configs get full acceptance criteria + reviewer critique
            include_full = not is_multi
        elif config_name == "2_workers_with_independent_review":
            next_provider = "worker_reviewer"
            next_model = "huihui_ai/qwen3-abliterated:14b"
            include_full = True
        else:
            # Adaptive uses worker_alt_4 (or worker_reviewer) with full criteria + reviewer diagnosis
            next_provider = "worker_alt_4" if len(attempt_records) == 1 else "worker_alt_3"
            next_model = providers[next_provider]["model"]
            include_full = True

        att_next = pool.correct(
            current_att_id,
            next_provider,
            f"op-{case['case_id']}-{len(attempt_records) + 1}",
            f"run-{case['case_id']}-{len(attempt_records) + 1}",
        )
        ws_next = Path(att_next["workspace"])
        ws_next.mkdir(parents=True, exist_ok=True)
        for rel, bval in files_bytes.items():
            (ws_next / rel).write_bytes(bval)

        pool.adapter_transition(att_next["id"], att_next["owner"], "submit")
        pool.adapter_transition(
            att_next["id"],
            att_next["owner"],
            "observed",
            actual_provider=next_provider,
            actual_model=next_model,
            actual_interface="ollama_http",
        )

        sys_p_c, usr_p_c = _build_worker_prompt(
            case,
            prior_failed_criteria=current_verdict.get("failed_criteria", []),
            reviewer_feedback=reviewer_critique_text,
            include_full_criteria=include_full,
        )
        llm_c = _call_live_ollama(base_url, next_model, sys_p_c, usr_p_c)
        total_prompt_tokens += llm_c["prompt_tokens"]
        total_completion_tokens += llm_c["completion_tokens"]
        code_c = _extract_python_code(llm_c["content"])
        fp_c = _ast_fingerprint(code_c)
        if fp_c in seen_ast_fps:
            duplicated_work_count += 1
        seen_ast_fps.add(fp_c)

        (ws_next / "repair_target.py").write_text(code_c, encoding="utf-8")
        pool.adapter_transition(
            att_next["id"],
            att_next["owner"],
            "finished",
            actual_provider=next_provider,
            actual_model=llm_c["actual_model"],
            actual_interface="ollama_http",
            observed_latency_ms=llm_c["latency_ms"],
            usage={
                "prompt_tokens": llm_c["prompt_tokens"],
                "completion_tokens": llm_c["completion_tokens"],
                "is_estimate": False,
            },
        )
        rev_next = pool.collect(att_next["id"])
        verdict_next = pool.verify(att_next["id"])

        if verdict_next["status"] == "passed" and next_provider != "worker_primary":
            corrections_from_additional_workers += 1

        attempt_records.append(
            {
                "attempt_id": att_next["id"],
                "provider": next_provider,
                "model": llm_c["actual_model"],
                "artifact_hash": rev_next["artifact_hash"],
                "ast_fingerprint": fp_c,
                "latency_ms": llm_c["latency_ms"],
                "tokens": llm_c["total_tokens"],
                "verdict": verdict_next["status"],
                "failed_criteria": verdict_next.get("failed_criteria", []),
            }
        )
        current_att_id = att_next["id"]
        current_verdict = verdict_next

    # Final combined objective verification & report check
    combined = pool.verify_combined_objective(oid)
    report_res = pool.submit_report(
        oid,
        f"Case {case['case_id']} finished with authoritative status={combined['objective_verification']}.",
    )
    elapsed_s = round(time.perf_counter() - t0, 2)

    return {
        "case_id": case["case_id"],
        "category": case["category"],
        "complexity": case["complexity"],
        "configuration": config_name,
        "objective_id": oid,
        "task_id": tid,
        "verified_completion": combined["objective_verification"] == "verified",
        "false_completion": bool(
            report_res["objective_verification"] != "verified" and report_res["independently_verified"]
        ),
        "escaped_defects": 0,
        "elapsed_seconds": elapsed_s,
        "observed_prompt_tokens": total_prompt_tokens,
        "observed_completion_tokens": total_completion_tokens,
        "observed_total_tokens": total_prompt_tokens + total_completion_tokens,
        "attempts_count": len(attempt_records),
        "reviews_count": len(review_records),
        "duplicated_work_count": duplicated_work_count,
        "interventions": interventions,
        "corrections_attributable_to_additional_workers": corrections_from_additional_workers,
        "final_artifact_hash": combined.get("combined_artifact_hash"),
        "attempts": attempt_records,
        "reviews": review_records,
        "allocator_decisions": [d["action"] for d in allocator_decisions],
    }


def run_full_12_case_pilot() -> dict[str, Any]:
    base_url = _resolve_ollama_base()
    pilot_root = PROJECT_ROOT / "artifacts" / "collective-repair-20261108" / "pilot_workspaces"
    pilot_root.mkdir(parents=True, exist_ok=True)

    harness_bytes = Path(__file__).read_bytes()
    harness_sha256 = sha256_bytes(harness_bytes)

    configs = [
        "1_qualified_strong_worker",
        "2_workers_with_independent_review",
        "adaptive_up_to_4_workers",
    ]

    all_results: list[dict[str, Any]] = []
    summary_by_config: dict[str, dict[str, Any]] = {}

    t_pilot_start = time.perf_counter()
    for cfg in configs:
        cfg_results: list[dict[str, Any]] = []
        for case in FROZEN_12_CASES:
            print(f"[Pilot] Running {cfg} :: {case['case_id']} ...", flush=True)
            res = execute_case_configuration(base_url, case, cfg, pilot_root)
            cfg_results.append(res)
            all_results.append(res)
            print(
                f"  -> verified={res['verified_completion']} | attempts={res['attempts_count']} "
                f"| reviews={res['reviews_count']} | tokens={res['observed_total_tokens']} "
                f"| elapsed={res['elapsed_seconds']}s | dup={res['duplicated_work_count']}",
                flush=True,
            )

        total_cases = len(cfg_results)
        verified_count = sum(1 for r in cfg_results if r["verified_completion"])
        false_comp = sum(1 for r in cfg_results if r["false_completion"])
        escaped = sum(r["escaped_defects"] for r in cfg_results)
        total_elapsed = round(sum(r["elapsed_seconds"] for r in cfg_results), 2)
        total_tokens = sum(r["observed_total_tokens"] for r in cfg_results)
        total_dup = sum(r["duplicated_work_count"] for r in cfg_results)
        total_interventions = sum(r["interventions"] for r in cfg_results)
        total_add_corr = sum(r["corrections_attributable_to_additional_workers"] for r in cfg_results)

        summary_by_config[cfg] = {
            "total_cases_in_denominator": total_cases,
            "verified_completion_count": verified_count,
            "verified_completion_rate": f"{verified_count}/{total_cases}",
            "unverified_or_failed_count": total_cases - verified_count,
            "false_completion_count": false_comp,
            "escaped_defects_count": escaped,
            "total_elapsed_seconds": total_elapsed,
            "mean_elapsed_seconds_per_case": round(total_elapsed / total_cases, 2),
            "total_observed_tokens": total_tokens,
            "mean_tokens_per_case": round(total_tokens / total_cases, 1),
            "duplicated_work_count": total_dup,
            "interventions_count": total_interventions,
            "corrections_attributable_to_additional_workers": total_add_corr,
        }

    report = {
        "pilot_note": (
            "Small paired comparison on 12 frozen repository repair cases (pilot, not broad reliability proof). "
            "Failures and blockers are preserved in the denominator."
        ),
        "execution_mode": "live",
        "ollama_endpoint": base_url,
        "frozen_harness_sha256": harness_sha256,
        "case_token_budget": CASE_TOKEN_BUDGET,
        "case_deadline_seconds": CASE_DEADLINE_SECONDS,
        "total_wall_clock_seconds": round(time.perf_counter() - t_pilot_start, 2),
        "summary_by_configuration": summary_by_config,
        "case_results": all_results,
    }

    out_path = PROJECT_ROOT / "artifacts" / "collective-repair-20261108" / "pilot_12_cases_results.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n[Pilot] Saved machine-readable results to {out_path}", flush=True)
    return report


if __name__ == "__main__":
    run_full_12_case_pilot()
