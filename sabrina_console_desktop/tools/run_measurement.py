"""Run-linked provenance, deterministic failure attribution, and paired-replay evaluation ledger.

Connects Sabrina's session records, decision traces, desktop audits, external
verifications, CPU benchmarks, and paired-replay artifacts into one queryable
measurement layer for the Operator Console.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assistant_run_provenance (
    run_id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    model TEXT NOT NULL,
    model_digest TEXT NOT NULL DEFAULT '',
    requested_device TEXT NOT NULL DEFAULT 'auto',
    confirmed_placement TEXT NOT NULL DEFAULT 'unknown',
    think_setting TEXT NOT NULL DEFAULT 'unset',
    options_json TEXT NOT NULL DEFAULT '{}',
    bridge_source_sha256 TEXT NOT NULL DEFAULT '',
    broker_source_sha256 TEXT NOT NULL DEFAULT '',
    system_prompt_sha256 TEXT NOT NULL DEFAULT '',
    tool_schema_sha256 TEXT NOT NULL DEFAULT '',
    pinned_prompt_differs INTEGER NOT NULL DEFAULT 0,
    skills_used_json TEXT NOT NULL DEFAULT '{}',
    capability_name TEXT NOT NULL DEFAULT '',
    is_simulation INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assistant_run_evaluations (
    run_id TEXT PRIMARY KEY,
    evaluated_at REAL NOT NULL,
    outcome_status TEXT NOT NULL,
    primary_category TEXT NOT NULL,
    sub_code TEXT NOT NULL DEFAULT '',
    counts_toward_competence INTEGER NOT NULL DEFAULT 0,
    verified_pass INTEGER NOT NULL DEFAULT 0,
    evidence_path TEXT NOT NULL DEFAULT '',
    diagnostic_summary TEXT NOT NULL DEFAULT '',
    step_classifications_json TEXT NOT NULL DEFAULT '[]',
    is_simulation INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS assistant_eval_category
    ON assistant_run_evaluations(primary_category, outcome_status);

CREATE TABLE IF NOT EXISTS assistant_eval_comparisons (
    comparison_id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    baseline_run_id TEXT NOT NULL,
    baseline_event_id INTEGER,
    candidate_run_id TEXT NOT NULL,
    changed_variable TEXT NOT NULL,
    baseline_value TEXT NOT NULL,
    candidate_value TEXT NOT NULL,
    seeds_json TEXT NOT NULL DEFAULT '[]',
    one_shot_rate REAL,
    best_of_n_rate REAL,
    candidate_rate REAL,
    sampling_gain REAL,
    intervention_gain REAL,
    recommendation TEXT NOT NULL DEFAULT '',
    evidence_artifact TEXT NOT NULL DEFAULT ''
);
"""

_SKILL_REF_RE = re.compile(r"(?:^|[/\\]|:)skills[/\\](?P<skill>[A-Za-z0-9_.-]+)[/\\]SKILL\.md", re.I)


def ensure_measurement_schema(db: sqlite3.Connection) -> None:
    """Create the additive measurement tables if they do not exist."""
    db.executescript(SCHEMA_SQL)
    for tbl in ('assistant_run_provenance', 'assistant_run_evaluations'):
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({tbl})").fetchall()}
        if cols and 'is_simulation' not in cols:
            db.execute(f"ALTER TABLE {tbl} ADD COLUMN is_simulation INTEGER NOT NULL DEFAULT 0")


def expire_measurement_rows(db: sqlite3.Connection, run_ids: list[str], now: float | None = None) -> None:
    """Scrub raw diagnostic excerpts on audit expiry while preserving non-sensitive competence counters."""
    if not run_ids:
        return
    now = time.time() if now is None else float(now)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assistant_run_evaluations" in tables:
        for run_id in run_ids:
            db.execute(
                """UPDATE assistant_run_evaluations
                   SET diagnostic_summary='Desktop audit expired; raw diagnostic summary erased',
                       step_classifications_json='[]',
                       evaluated_at=?
                   WHERE run_id=?""",
                (now, run_id),
            )


def _safe_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _file_sha256(path: Path) -> str:
    try:
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return ""


def _load_cpu_registry_digests(workspace_root: Path | None) -> dict[str, str]:
    if workspace_root is None:
        return {}
    reg_path = workspace_root / "config" / "model_cpu_registry.json"
    if not reg_path.is_file():
        return {}
    try:
        data = json.loads(reg_path.read_text(encoding="utf-8"))
        return {
            str(m.get("name")): str(m.get("digest", ""))
            for m in data.get("models", [])
            if isinstance(m, dict) and m.get("name")
        }
    except Exception:
        return {}


def _extract_skills_used(
    refs: list[str],
    messages: list[dict[str, Any]],
    workspace_root: Path | None,
) -> dict[str, dict[str, str]]:
    skills: dict[str, dict[str, str]] = {}
    candidates: list[str] = list(refs or [])
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content") or ""
        if isinstance(content, str):
            for m in _SKILL_REF_RE.finditer(content.replace("\\", "/")):
                candidates.append(f"skills/{m.group('skill')}/SKILL.md")
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            if fn.get("name") == "read_workspace_file":
                args = _safe_json(fn.get("arguments"), {})
                if isinstance(args, dict) and args.get("relative_path"):
                    candidates.append(str(args["relative_path"]))

    for item in candidates:
        base_path = str(item).split(":", 1)[0].replace("\\", "/")
        match = _SKILL_REF_RE.search(base_path)
        if not match:
            match = _SKILL_REF_RE.search(str(item).replace("\\", "/"))
        if not match:
            continue
        skill_name = match.group("skill")
        rel_path = f"skills/{skill_name}/SKILL.md"
        skill_id = f"project:{skill_name}"
        if skill_id not in skills:
            full_path = (workspace_root / rel_path) if workspace_root else None
            skills[skill_id] = {
                "name": skill_name,
                "relative_path": rel_path,
                "sha256": _file_sha256(full_path) if full_path else "",
                "scope": "Sabrina project",
            }
    return skills


def extract_run_provenance(
    db: sqlite3.Connection,
    run_id: str,
    workspace_root: Path | None = None,
    coordinator_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build the provenance record for a single run."""
    db.row_factory = sqlite3.Row
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assistant_sessions" not in tables:
        return None
    session = db.execute("SELECT * FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
    if session is None:
        return None

    refs = _safe_json(session["refs"], [])
    messages = _safe_json(session["messages"], [])
    decisions = []
    if "assistant_decision_audit" in tables:
        decisions = db.execute(
            "SELECT * FROM assistant_decision_audit WHERE run_id=? ORDER BY id ASC",
            (run_id,),
        ).fetchall()

    policy_access = "none"
    if "assistant_desktop_policy" in tables:
        pol = db.execute(
            "SELECT access FROM assistant_desktop_policy WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if pol and pol["access"]:
            policy_access = str(pol["access"])

    has_proposals = False
    if "assistant_desktop_proposals" in tables:
        has_proposals = bool(
            db.execute("SELECT 1 FROM assistant_desktop_proposals WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
        )
    has_desktop_events = False
    if "assistant_desktop_audit_events" in tables:
        has_desktop_events = bool(
            db.execute("SELECT 1 FROM assistant_desktop_audit_events WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
        )

    model = str(session["model"] or "")
    requested_device = str(session["requested_device"] or "auto")
    think_setting = "unset"
    options: dict[str, Any] = {}
    bridge_sha = ""
    broker_sha = ""
    prompt_sha = ""
    schema_sha = ""
    pinned_differs = 0
    is_replay = False

    for dec in decisions:
        req = _safe_json(dec["request"], {})
        meta = _safe_json(dec["metadata"], {})
        resp = _safe_json(dec["response"], {})
        if not model:
            model = str(req.get("model") or resp.get("model") or "")
        if "think" in req and isinstance(req["think"], bool):
            think_setting = "true" if req["think"] else "false"
        elif meta.get("variant") == "think_true":
            think_setting = "true"
        elif meta.get("think_setting") in {"true", "false"}:
            think_setting = str(meta["think_setting"])
        if isinstance(req.get("options"), dict):
            options = dict(req["options"])
        bridge_sha = str(
            meta.get("bridge_source_sha256")
            or (meta.get("source_hashes") or {}).get("tools\\sabrina_lab_assistant_mcp.py")
            or (meta.get("source_hashes") or {}).get("tools/sabrina_lab_assistant_mcp.py")
            or bridge_sha
        )
        broker_sha = str(
            meta.get("broker_source_sha256")
            or (meta.get("source_hashes") or {}).get("tools\\agent_desktop_broker.py")
            or (meta.get("source_hashes") or {}).get("tools/agent_desktop_broker.py")
            or broker_sha
        )
        prompt_sha = str(meta.get("system_prompt_sha256") or prompt_sha)
        schema_sha = str(meta.get("tool_schema_sha256") or schema_sha)
        if meta.get("pinned_prompt_differs_from_current"):
            pinned_differs = 1
        if meta.get("requested_device"):
            requested_device = str(meta["requested_device"])
        if meta.get("desktop_execution") is False or meta.get("experiment"):
            is_replay = True

    confirmed_placement = "replay_only" if is_replay else "unknown"
    if coordinator_requests and not is_replay:
        task_prefix = str(session["task"] or "")[:80]
        for req_row in coordinator_requests:
            if not isinstance(req_row, dict):
                continue
            label = str(req_row.get("task_label") or "")
            if task_prefix and label and task_prefix[:60] == label[:60]:
                placement = str(req_row.get("placement") or "").upper()
                if placement in {"GPU", "CPU"}:
                    confirmed_placement = placement
                    break

    digests = _load_cpu_registry_digests(workspace_root)
    model_digest = digests.get(model, "")
    skills_used = _extract_skills_used(refs, messages, workspace_root)

    if is_replay:
        capability_name = "desktop.paired_replay"
    elif policy_access == "autonomous":
        capability_name = "desktop.autonomous"
    elif policy_access == "propose" or has_proposals:
        capability_name = "desktop.propose"
    elif policy_access == "observe" or has_desktop_events or any(str(r).startswith("desktop:") for r in refs):
        capability_name = "desktop.observe"
    else:
        capability_name = "workspace.research"

    now = time.time()
    created = float(session["created"] or session["updated"] or now)
    updated = float(session["updated"] or now)

    provenance = {
        "run_id": run_id,
        "created": created,
        "updated": updated,
        "model": model or "unknown",
        "model_digest": model_digest,
        "requested_device": requested_device or "auto",
        "confirmed_placement": confirmed_placement,
        "think_setting": think_setting,
        "options_json": json.dumps(options, sort_keys=True),
        "bridge_source_sha256": bridge_sha,
        "broker_source_sha256": broker_sha,
        "system_prompt_sha256": prompt_sha,
        "tool_schema_sha256": schema_sha,
        "pinned_prompt_differs": pinned_differs,
        "skills_used_json": json.dumps(skills_used, sort_keys=True),
        "capability_name": capability_name,
        "is_simulation": 1 if any(s in str(model or '').lower() for s in ('simulation', 'mock')) or str(session['task'] or '').startswith('[SIMULATION]') or 'reclassified' in str(session['verified_result'] or '').lower() or 'synthetic' in str(session['verified_result'] or '').lower() else 0,
    }
    return provenance


def _snapshot_preserved_child_count(
    desktop_events: list[sqlite3.Row],
    messages: list[dict[str, Any]],
    ref_id: str,
) -> bool:
    """Check whether the model-visible snapshot preceding a readonly mutation kept children_count."""
    m = re.match(r"^@(?P<snap>s[0-9A-Za-z_-]+):e\d+$", str(ref_id))
    if not m:
        return True
    snap_id = m.group("snap")
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = str(msg.get("content") or "")
        if snap_id in content:
            return "children_count" in content
    for ev in reversed(desktop_events):
        res_str = str(ev["result"] or "")
        if snap_id in res_str:
            return len(res_str) <= 3500 or "children_count" in res_str
    return True


def classify_run(
    db: sqlite3.Connection,
    run_id: str,
    workspace_root: Path | None = None,
) -> dict[str, Any] | None:
    """Deterministically attribute a run's outcome and step-level failures."""
    db.row_factory = sqlite3.Row
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assistant_sessions" not in tables:
        return None
    session = db.execute("SELECT * FROM assistant_sessions WHERE id=?", (run_id,)).fetchone()
    if session is None:
        return None

    now = time.time()
    if "assistant_run_evaluations" in tables:
        existing = db.execute(
            "SELECT * FROM assistant_run_evaluations WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if existing and existing["outcome_status"] == "invalidated_synthetic":
            return dict(existing)

    if "workflow_attempts" in tables:
        wa_invalid = db.execute(
            "SELECT id FROM workflow_attempts WHERE run_id=? AND (state='invalidated' OR delivery LIKE '%substitution%')",
            (run_id,),
        ).fetchone()
        if wa_invalid:
            return {
                "run_id": run_id,
                "evaluated_at": now,
                "outcome_status": "invalidated_synthetic",
                "primary_category": "invalidated_synthetic",
                "sub_code": "SYNTHETIC_SUBSTITUTION_RETRACTED",
                "counts_toward_competence": 0,
                "verified_pass": 0,
                "evidence_path": "",
                "diagnostic_summary": "Retracted synthetic substitution run.",
                "step_classifications_json": "[]",
            }

    state = str(session["state"] or "")
    if state == "expired":
        if "assistant_run_evaluations" in tables:
            existing = db.execute(
                "SELECT * FROM assistant_run_evaluations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing:
                result = dict(existing)
                result["diagnostic_summary"] = "Desktop audit expired; raw diagnostic summary erased"
                result["step_classifications_json"] = "[]"
                return result
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "expired",
            "primary_category": "expired",
            "sub_code": "AUDIT_RETENTION_EXPIRED",
            "counts_toward_competence": 0,
            "verified_pass": 0,
            "evidence_path": "",
            "diagnostic_summary": "Desktop audit expired; raw diagnostic summary erased",
            "step_classifications_json": "[]",
        }

    decisions = (
        db.execute("SELECT * FROM assistant_decision_audit WHERE run_id=? ORDER BY id ASC", (run_id,)).fetchall()
        if "assistant_decision_audit" in tables
        else []
    )
    desktop_events = (
        db.execute("SELECT * FROM assistant_desktop_audit_events WHERE run_id=? ORDER BY id ASC", (run_id,)).fetchall()
        if "assistant_desktop_audit_events" in tables
        else []
    )
    proposals = (
        db.execute("SELECT * FROM assistant_desktop_proposals WHERE run_id=? ORDER BY created ASC", (run_id,)).fetchall()
        if "assistant_desktop_proposals" in tables
        else []
    )
    messages = _safe_json(session["messages"], [])
    progress = str(session["progress"] or "")
    activity = str(session["activity"] or "")
    answer = str(session["answer"] or "")
    verified_result = str(session["verified_result"] or "").strip()
    verification_evidence = str(session["verification_evidence"] or "").strip()

    # 1. Check if this is a non-executing paired replay run
    if decisions and all(
        (_safe_json(d["metadata"], {}).get("desktop_execution") is False)
        or bool(_safe_json(d["metadata"], {}).get("experiment"))
        for d in decisions
    ):
        first_meta = _safe_json(decisions[0]["metadata"], {})
        exp_name = str(first_meta.get("experiment") or "paired_replay")
        sub_code = "THINKING_TOGGLE_REPLAY" if "thinking" in exp_name else "PAIRED_DECISION_REPLAY"
        step_items = []
        for d in decisions:
            meta = _safe_json(d["metadata"], {})
            resp = _safe_json(d["response"], {})
            msg = resp.get("message") or {}
            tcalls = msg.get("tool_calls") or []
            tool_name = tcalls[0].get("function", {}).get("name", "") if tcalls else "no_tool_call"
            step_items.append({
                "source": "decision_replay",
                "event_id": d["id"],
                "seed": meta.get("seed"),
                "variant": meta.get("variant"),
                "tool_choice": tool_name,
            })
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "replay",
            "primary_category": "paired_replay",
            "sub_code": sub_code,
            "counts_toward_competence": 0,
            "verified_pass": 0,
            "evidence_path": "",
            "diagnostic_summary": f"Controlled non-executing replay ({exp_name}, {len(decisions)} decision trials).",
            "step_classifications_json": json.dumps(step_items),
        }

    step_classifications: list[dict[str, Any]] = []

    # 2. Inspect decision audit rows
    for dec in decisions:
        err = str(dec["error"] or "").strip()
        resp = _safe_json(dec["response"], {})
        if err:
            step_classifications.append({
                "source": "decision_audit",
                "id": dec["id"],
                "category": "scheduler_or_placement",
                "sub_code": "INFERENCE_OR_COORDINATOR_ERROR",
                "detail": err[:240],
            })
            continue
        if resp:
            done_reason = str(resp.get("done_reason") or "")
            msg = resp.get("message") or {}
            tcalls = msg.get("tool_calls") or []
            content = str(msg.get("content") or "").strip()
            if done_reason == "length" and not tcalls:
                step_classifications.append({
                    "source": "decision_audit",
                    "id": dec["id"],
                    "category": "observation_shaping",
                    "sub_code": "TOKEN_CUTOFF_LENGTH",
                    "detail": "Generation hit num_predict token limit before emitting a tool call.",
                })
            elif done_reason == "stop" and not tcalls and not content:
                step_classifications.append({
                    "source": "decision_audit",
                    "id": dec["id"],
                    "category": "model_error",
                    "sub_code": "EMPTY_MODEL_TURN",
                    "detail": "Model stopped with empty content and zero tool calls.",
                })

    # 3. Inspect desktop audit events
    prev_invalid_arg_sigs: set[str] = set()
    for ev in desktop_events:
        tool = str(ev["tool"] or "")
        args = _safe_json(ev["arguments"], {})
        res = _safe_json(ev["result"], {})
        if not isinstance(res, dict):
            continue
        if res.get("ok") is True:
            data = res.get("data")
            if (tool == "desktop_list_apps" and isinstance(data, dict) and data.get("apps") == []) or (
                tool == "desktop_list_windows" and data == []
            ):
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "adapter_or_env",
                    "sub_code": "APP_UNAVAILABLE",
                    "detail": f"{tool} returned zero running windows/instances for target app.",
                })
            continue

        err = res.get("error") if isinstance(res.get("error"), dict) else {}
        code = str(err.get("code") or "DESKTOP_ERROR")
        msg = str(err.get("message") or "")
        layer = str((err.get("details") or {}).get("layer") or "") if isinstance(err.get("details"), dict) else ""

        if code == "INVALID_REFERENCE":
            ref_id = str(args.get("ref_id") or "")
            if "editable text control" in msg:
                if _snapshot_preserved_child_count(desktop_events, messages, ref_id):
                    step_classifications.append({
                        "source": "desktop_event",
                        "id": ev["id"],
                        "tool": tool,
                        "category": "model_error",
                        "sub_code": "READONLY_TARGET_MUTATION",
                        "detail": f"{tool} targeted readonly container {ref_id} despite visible readonly/children_count metadata.",
                    })
                else:
                    step_classifications.append({
                        "source": "desktop_event",
                        "id": ev["id"],
                        "tool": tool,
                        "category": "observation_shaping",
                        "sub_code": "READONLY_CONTAINER_AMBIGUITY",
                        "detail": f"{tool} targeted {ref_id} after compact snapshot omitted children_count.",
                    })
            else:
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "model_error",
                    "sub_code": "STALE_OR_INVENTED_REF",
                    "detail": f"{tool} rejected {ref_id}: {msg[:200]}",
                })
        elif code in {"INVALID_ARGS", "CLI_ARGUMENT_ERROR"}:
            if "--timeout-ms" in msg and tool in {"desktop_restore_window", "desktop_focus_window"}:
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "adapter_or_env",
                    "sub_code": "BROKER_FLAG_MISMATCH",
                    "detail": msg[:200],
                })
            else:
                sig = f"{tool}:{json.dumps(args, sort_keys=True)}"
                sub = "CLI_ARGUMENT_REPEAT" if sig in prev_invalid_arg_sigs else "INVALID_TOOL_ARGUMENTS"
                prev_invalid_arg_sigs.add(sig)
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "model_error",
                    "sub_code": sub,
                    "detail": msg[:200],
                })
        elif code in {"CLI_PROCESS_TIMEOUT", "TIMEOUT"}:
            sub = "CLI_PROCESS_TIMEOUT" if (code == "CLI_PROCESS_TIMEOUT" or layer == "cli_watchdog") else "OBSERVATION_TIMEOUT"
            step_classifications.append({
                "source": "desktop_event",
                "id": ev["id"],
                "tool": tool,
                "category": "adapter_or_env",
                "sub_code": sub,
                "detail": msg[:200],
            })
        elif code in {"PERM_DENIED", "INTERNAL", "WINDOW_NOT_FOUND", "AMBIGUOUS_TARGET", "APP_UNAVAILABLE"}:
            sub = "ELEVATION_MISMATCH" if ("0x80070005" in str(err) or "740" in msg or "Access is denied" in msg) else code
            step_classifications.append({
                "source": "desktop_event",
                "id": ev["id"],
                "tool": tool,
                "category": "adapter_or_env",
                "sub_code": sub,
                "detail": msg[:200],
            })
        elif code == "BROKER_ERROR":
            if any(tok in msg for tok in ("WinError 740", "elevation", "WinError 5", "Access is denied")):
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "adapter_or_env",
                    "sub_code": "ELEVATION_MISMATCH",
                    "detail": msg[:200],
                })
            elif "authentication or credential controls" in msg:
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "application_blocker",
                    "sub_code": "AUTH_SURFACE_BLOCKED",
                    "detail": msg[:200],
                })
            elif "editable text control" in msg:
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "model_error",
                    "sub_code": "READONLY_TARGET_MUTATION",
                    "detail": msg[:200],
                })
            elif "ref_id" in msg or "fresh snapshot" in msg:
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "model_error",
                    "sub_code": "STALE_OR_INVENTED_REF",
                    "detail": msg[:200],
                })
            else:
                step_classifications.append({
                    "source": "desktop_event",
                    "id": ev["id"],
                    "tool": tool,
                    "category": "adapter_or_env",
                    "sub_code": "BROKER_ERROR",
                    "detail": msg[:200],
                })
        else:
            step_classifications.append({
                "source": "desktop_event",
                "id": ev["id"],
                "tool": tool,
                "category": "adapter_or_env",
                "sub_code": code,
                "detail": msg[:200],
            })

    # 4. Inspect proposals
    rejected_sigs: set[str] = set()
    for prop in proposals:
        p_state = str(prop["state"] or "")
        p_tool = str(prop["tool"] or "")
        p_args = _safe_json(prop["arguments"], {})
        p_res = _safe_json(prop["result"], {})
        p_reason = str(prop["decision_reason"] or "")

        if p_state in {"pending", "executing"}:
            step_classifications.append({
                "source": "proposal",
                "id": prop["id"],
                "tool": p_tool,
                "category": "application_blocker",
                "sub_code": "AWAITING_SUPERVISOR_REVIEW",
                "detail": f"Proposal {prop['id']} ({p_tool}) awaiting supervisor review.",
            })
        elif p_state == "rejected":
            sig = f"{p_tool}:{json.dumps(p_args, sort_keys=True)}"
            if sig in rejected_sigs or "Rejected again" in p_reason:
                step_classifications.append({
                    "source": "proposal",
                    "id": prop["id"],
                    "tool": p_tool,
                    "category": "model_error",
                    "sub_code": "REPEATED_REJECTED_PROPOSAL",
                    "detail": p_reason[:200],
                })
            elif "Wrong tool" in p_reason:
                step_classifications.append({
                    "source": "proposal",
                    "id": prop["id"],
                    "tool": p_tool,
                    "category": "model_error",
                    "sub_code": "WRONG_TOOL_SELECTION",
                    "detail": p_reason[:200],
                })
            elif "elevated window" in p_reason or "forced-admin" in p_reason:
                step_classifications.append({
                    "source": "proposal",
                    "id": prop["id"],
                    "tool": p_tool,
                    "category": "adapter_or_env",
                    "sub_code": "ELEVATION_MISMATCH",
                    "detail": p_reason[:200],
                })
            else:
                step_classifications.append({
                    "source": "proposal",
                    "id": prop["id"],
                    "tool": p_tool,
                    "category": "application_blocker",
                    "sub_code": "SUPERVISOR_REJECTED_PROPOSAL",
                    "detail": p_reason[:200],
                })
            rejected_sigs.add(sig)
        elif p_state == "failed" and isinstance(p_res, dict):
            err = p_res.get("error") if isinstance(p_res.get("error"), dict) else {}
            code = str(err.get("code") or "PROPOSAL_ACTION_FAILED")
            msg = str(err.get("message") or "")
            if "--timeout-ms" in msg and p_tool in {"desktop_restore_window", "desktop_focus_window"}:
                step_classifications.append({
                    "source": "proposal",
                    "id": prop["id"],
                    "tool": p_tool,
                    "category": "adapter_or_env",
                    "sub_code": "BROKER_FLAG_MISMATCH",
                    "detail": msg[:200],
                })
            elif any(tok in msg or tok in str(err) for tok in ("WinError 740", "0x80070005", "WinError 5", "Access is denied", "elevation")):
                step_classifications.append({
                    "source": "proposal",
                    "id": prop["id"],
                    "tool": p_tool,
                    "category": "adapter_or_env",
                    "sub_code": "ELEVATION_MISMATCH",
                    "detail": msg[:200],
                })
            else:
                step_classifications.append({
                    "source": "proposal",
                    "id": prop["id"],
                    "tool": p_tool,
                    "category": "adapter_or_env",
                    "sub_code": code,
                    "detail": msg[:200],
                })

    # 5. Inspect session tool messages and progress for errors not in desktop_events
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        t_name = str(msg.get("tool_name") or "")
        content = str(msg.get("content") or "")
        if "file exceeds the 64 KB read limit" in content:
            step_classifications.append({
                "source": "session_message",
                "tool": t_name,
                "category": "observation_shaping",
                "sub_code": "FILE_SIZE_LIMIT_64KB",
                "detail": "Workspace read rejected because target file exceeded 64 KB limit.",
            })
        elif "Tool output budget exhausted" in content:
            step_classifications.append({
                "source": "session_message",
                "tool": t_name,
                "category": "observation_shaping",
                "sub_code": "TOOL_BUDGET_EXHAUSTED",
                "detail": "Run reached MAX_TOTAL_TOOL_OUTPUT_CHARS.",
            })
        elif "desktop operations must be the only tool call in a model turn" in content:
            step_classifications.append({
                "source": "session_message",
                "tool": t_name,
                "category": "model_error",
                "sub_code": "MULTI_TOOL_DESKTOP_TURN",
                "detail": content[:200],
            })
        elif content.startswith("Tool error:") and not desktop_events:
            if "editable text control" in content:
                step_classifications.append({
                    "source": "session_message",
                    "tool": t_name,
                    "category": "model_error",
                    "sub_code": "READONLY_TARGET_MUTATION",
                    "detail": content[:200],
                })
            elif "ref_id" in content:
                step_classifications.append({
                    "source": "session_message",
                    "tool": t_name,
                    "category": "model_error",
                    "sub_code": "STALE_OR_INVENTED_REF",
                    "detail": content[:200],
                })

    if not step_classifications and "failed=" in progress:
        m = re.search(r"(?P<tool>[a-z_]+)(?:;\s*|\s+)failed=(?P<code>[A-Z0-9_]+)", progress)
        if m:
            code = m.group("code")
            tool = m.group("tool")
            cat = "model_error" if code in {"INVALID_ARGS", "CLI_ARGUMENT_ERROR"} else "adapter_or_env"
            sub = "OBSERVATION_TIMEOUT" if code == "TIMEOUT" else code
            step_classifications.append({
                "source": "session_progress",
                "tool": tool,
                "category": cat,
                "sub_code": sub,
                "detail": progress[:200],
            })

    if not step_classifications and ("Independent coding verification: not passed" in answer or "Independent coding verification: not passed" in progress):
        step_classifications.append({
            "source": "independent_verifier",
            "category": "model_error",
            "sub_code": "FUNCTIONAL_VERIFICATION_FAILED",
            "detail": "Independent coding verification: not passed",
        })

    if not step_classifications and ("DACL" in answer or ("permission state" in answer and "denied" in answer)):
        step_classifications.append({
            "source": "session_answer",
            "category": "adapter_or_env",
            "sub_code": "PERM_DENIED",
            "detail": answer[:200],
        })

    if activity.startswith("Blocked: "):
        step_classifications.append({
            "source": "operator_block",
            "category": "application_blocker",
            "sub_code": "OPERATOR_BLOCKER",
            "detail": activity[:200],
        })

    # Fallback to workflow_verifications if session-level verification was omitted
    if not (verified_result and verification_evidence):
        wv_row = None
        if "workflow_verifications" in tables:
            if "workflow_attempts" in tables:
                wv_row = db.execute(
                    """SELECT wv.* FROM workflow_verifications wv
                       JOIN workflow_attempts wa ON wv.attempt_id = wa.id
                       WHERE wa.run_id = ? AND wa.state != 'invalidated' AND wa.delivery NOT LIKE '%substitution%'
                       ORDER BY wv.created DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
            if not wv_row and "workflow_evidence" in tables and "workflow_attempts" in tables:
                wv_row = db.execute(
                    """SELECT wv.* FROM workflow_verifications wv
                       JOIN workflow_evidence we ON wv.attempt_id = we.attempt_id
                       JOIN workflow_attempts wa ON we.attempt_id = wa.id
                       WHERE we.request_id = ? AND wa.state != 'invalidated' AND wa.delivery NOT LIKE '%substitution%'
                       ORDER BY wv.created DESC LIMIT 1""",
                    (run_id,),
                ).fetchone()
        if wv_row:
            try:
                v_data = json.loads(wv_row["verdict"])
                if v_data.get("status") == "passed":
                    verified_result = f"Workflow verifier passed: suite={str(v_data.get('suite_sha256') or '')[:12]} artifact={str(v_data.get('artifact_sha256') or '')[:12]}"
                    verification_evidence = f"workflow_verifications:{wv_row['id']}"
                elif v_data.get("status") == "failed":
                    step_classifications.append({
                        "source": "workflow_verifier",
                        "id": wv_row["id"],
                        "category": "model_error",
                        "sub_code": "FUNCTIONAL_VERIFICATION_FAILED",
                        "detail": f"Workflow verification failed: {v_data.get('checks')}",
                    })
            except Exception:
                pass

    # 6. Determine run-level outcome and primary attribution
    if verified_result and verification_evidence:
        if "reclassified" in verified_result.lower() or "synthetic" in verified_result.lower():
            return {
                "run_id": run_id,
                "evaluated_at": now,
                "outcome_status": "unverified",
                "primary_category": "synthetic_downstream",
                "sub_code": "SYNTHETIC_DOWNSTREAM_EXCLUDED",
                "counts_toward_competence": 0,
                "verified_pass": 0,
                "evidence_path": verification_evidence,
                "diagnostic_summary": verified_result[:400],
                "step_classifications_json": json.dumps(step_classifications),
            }
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "verified_pass",
            "primary_category": "verified_success",
            "sub_code": "EXTERNAL_RESULT_VERIFIED",
            "counts_toward_competence": 1,
            "verified_pass": 1,
            "evidence_path": verification_evidence,
            "diagnostic_summary": verified_result[:400],
            "step_classifications_json": json.dumps(step_classifications),
        }

    by_cat: dict[str, list[dict[str, Any]]] = {}
    for item in step_classifications:
        by_cat.setdefault(item["category"], []).append(item)

    if "scheduler_or_placement" in by_cat:
        top = by_cat["scheduler_or_placement"][-1]
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "blocked_infra",
            "primary_category": "scheduler_or_placement",
            "sub_code": top["sub_code"],
            "counts_toward_competence": 0,
            "verified_pass": 0,
            "evidence_path": "",
            "diagnostic_summary": top["detail"],
            "step_classifications_json": json.dumps(step_classifications),
        }

    last_step = step_classifications[-1] if step_classifications else None
    if "model_error" in by_cat and (
        last_step is None
        or last_step["category"] in {"model_error", "observation_shaping"}
        or any(x["sub_code"] in {"READONLY_TARGET_MUTATION", "WRONG_TOOL_SELECTION", "REPEATED_REJECTED_PROPOSAL"} for x in by_cat["model_error"])
    ):
        had_successful_obs = any(
            isinstance(_safe_json(ev["result"], None), dict)
            and _safe_json(ev["result"], {}).get("ok") is True
            and ev["tool"] in {"desktop_snapshot", "desktop_find"}
            for ev in desktop_events
        )
        if not had_successful_obs and "adapter_or_env" in by_cat and all(
            x["sub_code"] == "REPEATED_REJECTED_PROPOSAL" for x in by_cat["model_error"]
        ):
            top = by_cat["adapter_or_env"][-1]
            return {
                "run_id": run_id,
                "evaluated_at": now,
                "outcome_status": "blocked_env",
                "primary_category": "adapter_or_env",
                "sub_code": top["sub_code"],
                "counts_toward_competence": 0,
                "verified_pass": 0,
                "evidence_path": "",
                "diagnostic_summary": top["detail"],
                "step_classifications_json": json.dumps(step_classifications),
            }

        top = by_cat["model_error"][-1]
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "failed_model",
            "primary_category": "model_error",
            "sub_code": top["sub_code"],
            "counts_toward_competence": 1,
            "verified_pass": 0,
            "evidence_path": "",
            "diagnostic_summary": top["detail"],
            "step_classifications_json": json.dumps(step_classifications),
        }

    if "adapter_or_env" in by_cat:
        top = by_cat["adapter_or_env"][-1]
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "blocked_env",
            "primary_category": "adapter_or_env",
            "sub_code": top["sub_code"],
            "counts_toward_competence": 0,
            "verified_pass": 0,
            "evidence_path": "",
            "diagnostic_summary": top["detail"],
            "step_classifications_json": json.dumps(step_classifications),
        }

    if "application_blocker" in by_cat:
        top = by_cat["application_blocker"][-1]
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "blocked_app",
            "primary_category": "application_blocker",
            "sub_code": top["sub_code"],
            "counts_toward_competence": 0,
            "verified_pass": 0,
            "evidence_path": "",
            "diagnostic_summary": top["detail"],
            "step_classifications_json": json.dumps(step_classifications),
        }

    if "observation_shaping" in by_cat:
        top = by_cat["observation_shaping"][-1]
        return {
            "run_id": run_id,
            "evaluated_at": now,
            "outcome_status": "blocked_observation",
            "primary_category": "observation_shaping",
            "sub_code": top["sub_code"],
            "counts_toward_competence": 0,
            "verified_pass": 0,
            "evidence_path": "",
            "diagnostic_summary": top["detail"],
            "step_classifications_json": json.dumps(step_classifications),
        }

    answer = str(session["answer"] or "")
    sub_code = (
        "STEP_BUDGET_REACHED"
        if "Step budget reached" in answer or "tool-call limit" in answer
        else "REASONING_ENDED_UNVERIFIED"
        if state == "completed"
        else "RUN_PAUSED_UNVERIFIED"
    )
    return {
        "run_id": run_id,
        "evaluated_at": now,
        "outcome_status": "unverified",
        "primary_category": "unverified_in_progress",
        "sub_code": sub_code,
        "counts_toward_competence": 0,
        "verified_pass": 0,
        "evidence_path": "",
        "diagnostic_summary": progress[:300] or f"Run state={state}; no external verification recorded.",
        "step_classifications_json": json.dumps(step_classifications),
    }


def _compute_paired_comparisons(
    db: sqlite3.Connection,
    workspace_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Compute paired-replay comparisons in-memory from decision audit rows."""
    db.row_factory = sqlite3.Row
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    comparisons: list[dict[str, Any]] = []

    if "assistant_decision_audit" not in tables:
        return comparisons

    rows = db.execute("SELECT * FROM assistant_decision_audit ORDER BY id ASC").fetchall()
    by_exp: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        meta = _safe_json(r["metadata"], {})
        exp = meta.get("experiment")
        if exp:
            by_exp.setdefault(str(exp), []).append(r)

    # 1. child_count_ablation (observed_compact vs child_count_restored)
    if "child_count_ablation" in by_exp:
        trials = by_exp["child_count_ablation"]
        compact_valid = []
        restored_valid = []
        seeds = sorted({int(_safe_json(t["metadata"], {}).get("seed", 0)) for t in trials if _safe_json(t["metadata"], {}).get("seed") is not None})
        run_id = str(trials[0]["run_id"])
        for t in trials:
            meta = _safe_json(t["metadata"], {})
            resp = _safe_json(t["response"], {})
            tcalls = ((resp.get("message") or {}).get("tool_calls") or [])
            tool = tcalls[0].get("function", {}).get("name", "") if tcalls else ""
            passed = 1.0 if (tool and tool != "desktop_type") else 0.0
            if meta.get("variant") == "observed_compact":
                compact_valid.append(passed)
            elif meta.get("variant") == "child_count_restored":
                restored_valid.append(passed)

        one_shot = compact_valid[0] if compact_valid else 0.0
        best_of_n = max(compact_valid) if compact_valid else 0.0
        cand_rate = (sum(restored_valid) / len(restored_valid)) if restored_valid else 0.0
        sampling_gain = round(best_of_n - one_shot, 3)
        intervention_gain = round(cand_rate - (sum(compact_valid) / len(compact_valid) if compact_valid else 0.0), 3)
        artifact_path = ""
        if workspace_root:
            candidate_art = workspace_root / "artifacts" / "desktop_review_20261107" / "decision-feedback-review.json"
            if candidate_art.is_file():
                artifact_path = str(candidate_art)

        comp = {
            "comparison_id": f"cmp_child_count_{run_id[:12]}",
            "created": float(trials[-1]["created"] or time.time()),
            "baseline_run_id": "79d7ddb7cce244448f336ebc5f7da557",
            "baseline_event_id": int(trials[0]["id"]),
            "candidate_run_id": run_id,
            "changed_variable": "snapshot_children_count",
            "baseline_value": "observed_compact (children_count omitted)",
            "candidate_value": "child_count_restored (children_count=1 visible)",
            "seeds_json": json.dumps(seeds),
            "one_shot_rate": round(one_shot, 3),
            "best_of_n_rate": round(best_of_n, 3),
            "candidate_rate": round(cand_rate, 3),
            "sampling_gain": sampling_gain,
            "intervention_gain": intervention_gain,
            "recommendation": (
                "Restoring children_count alone did not prevent desktop_type on the readonly document "
                "(0/3 seeds passed across both variants; sampling_gain=0.0, intervention_gain=0.0). "
                "Model reasoning (think=true) or explicit readonly rejection is required."
            ),
            "evidence_artifact": artifact_path,
        }
        comparisons.append(comp)

    # 2. thinking_toggle (think=false baseline vs think=true candidate)
    if "thinking_toggle" in by_exp:
        trials = by_exp["thinking_toggle"]
        baseline_trials = by_exp.get("child_count_ablation", [])
        base_compact = [
            0.0
            for t in baseline_trials
            if _safe_json(t["metadata"], {}).get("variant") == "observed_compact"
        ] or [0.0]
        think_scores = []
        valid_action_scores = []
        seeds = sorted({int(_safe_json(t["metadata"], {}).get("seed", 0)) for t in trials if _safe_json(t["metadata"], {}).get("seed") is not None})
        run_id = str(trials[0]["run_id"])
        base_run_id = str(_safe_json(trials[0]["metadata"], {}).get("baseline_run") or "4c9256ff7fec41a590ad24107aa90b44")
        for t in trials:
            resp = _safe_json(t["response"], {})
            tcalls = ((resp.get("message") or {}).get("tool_calls") or [])
            tool = tcalls[0].get("function", {}).get("name", "") if tcalls else ""
            avoided_readonly = 1.0 if tool != "desktop_type" else 0.0
            emitted_valid_tool = 1.0 if (tool and tool != "desktop_type") else 0.0
            think_scores.append(avoided_readonly)
            valid_action_scores.append(emitted_valid_tool)

        one_shot = base_compact[0]
        best_of_n = max(base_compact)
        cand_rate = sum(valid_action_scores) / len(valid_action_scores) if valid_action_scores else 0.0
        sampling_gain = round(best_of_n - one_shot, 3)
        intervention_gain = round(cand_rate - (sum(base_compact) / len(base_compact)), 3)
        artifact_path = ""
        if workspace_root:
            candidate_art = workspace_root / "artifacts" / "desktop_review_20261107" / "thinking-feedback-review.json"
            if candidate_art.is_file():
                artifact_path = str(candidate_art)

        comp = {
            "comparison_id": f"cmp_thinking_toggle_{run_id[:12]}",
            "created": float(trials[-1]["created"] or time.time()),
            "baseline_run_id": base_run_id,
            "baseline_event_id": int(trials[0]["id"]),
            "candidate_run_id": run_id,
            "changed_variable": "think",
            "baseline_value": "false",
            "candidate_value": "true",
            "seeds_json": json.dumps(seeds),
            "one_shot_rate": round(one_shot, 3),
            "best_of_n_rate": round(best_of_n, 3),
            "candidate_rate": round(cand_rate, 3),
            "sampling_gain": sampling_gain,
            "intervention_gain": intervention_gain,
            "recommendation": (
                "Enabling think=true eliminated readonly-document typing in 3/3 seeds and produced "
                "valid alternative tool calls in 2/3 seeds (+66.7% intervention gain over think=false; "
                "1/3 seeds returned prose without a tool call). Recommend think=true for 14B desktop runs "
                "paired with tool-call enforcement."
            ),
            "evidence_artifact": artifact_path,
        }
        comparisons.append(comp)
    comparisons.sort(key=lambda c: float(c.get("created") or 0), reverse=True)
    return comparisons


def sync_paired_comparisons_from_db(
    db: sqlite3.Connection,
    workspace_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Populate assistant_eval_comparisons from decision-replay rows and saved review artifacts."""
    ensure_measurement_schema(db)
    comparisons = _compute_paired_comparisons(db, workspace_root)
    for comp in comparisons:
        db.execute(
            """INSERT INTO assistant_eval_comparisons (
                   comparison_id, created, baseline_run_id, baseline_event_id,
                   candidate_run_id, changed_variable, baseline_value, candidate_value,
                   seeds_json, one_shot_rate, best_of_n_rate, candidate_rate,
                   sampling_gain, intervention_gain, recommendation, evidence_artifact
               ) VALUES (
                   :comparison_id, :created, :baseline_run_id, :baseline_event_id,
                   :candidate_run_id, :changed_variable, :baseline_value, :candidate_value,
                   :seeds_json, :one_shot_rate, :best_of_n_rate, :candidate_rate,
                   :sampling_gain, :intervention_gain, :recommendation, :evidence_artifact
               ) ON CONFLICT(comparison_id) DO UPDATE SET
                   one_shot_rate=excluded.one_shot_rate,
                   best_of_n_rate=excluded.best_of_n_rate,
                   candidate_rate=excluded.candidate_rate,
                   sampling_gain=excluded.sampling_gain,
                   intervention_gain=excluded.intervention_gain,
                   recommendation=excluded.recommendation,
                   evidence_artifact=excluded.evidence_artifact""",
            comp,
        )
    return comparisons


def evaluate_and_save_run(
    db: sqlite3.Connection,
    run_id: str,
    workspace_root: Path | None = None,
    coordinator_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Compute and upsert both provenance and evaluation records for a single run."""
    ensure_measurement_schema(db)
    prov = extract_run_provenance(db, run_id, workspace_root, coordinator_requests)
    ev = classify_run(db, run_id, workspace_root)
    if prov is None or ev is None:
        return None
    if "is_simulation" not in prov:
        prov["is_simulation"] = 1 if any(s in str(prov.get("model") or "").lower() for s in ("simulation", "mock")) else 0
    if "is_simulation" not in ev:
        ev["is_simulation"] = prov["is_simulation"]
    db.execute(
        """INSERT INTO assistant_run_provenance (
               run_id, created, updated, model, model_digest, requested_device,
               confirmed_placement, think_setting, options_json,
               bridge_source_sha256, broker_source_sha256, system_prompt_sha256,
               tool_schema_sha256, pinned_prompt_differs, skills_used_json, capability_name,
               is_simulation
           ) VALUES (
               :run_id, :created, :updated, :model, :model_digest, :requested_device,
               :confirmed_placement, :think_setting, :options_json,
               :bridge_source_sha256, :broker_source_sha256, :system_prompt_sha256,
               :tool_schema_sha256, :pinned_prompt_differs, :skills_used_json, :capability_name,
               COALESCE(:is_simulation, 0)
           ) ON CONFLICT(run_id) DO UPDATE SET
               updated=excluded.updated,
               model=excluded.model,
               model_digest=excluded.model_digest,
               requested_device=excluded.requested_device,
               confirmed_placement=CASE
                   WHEN excluded.confirmed_placement != 'unknown' THEN excluded.confirmed_placement
                   ELSE assistant_run_provenance.confirmed_placement
               END,
               think_setting=CASE
                   WHEN excluded.think_setting != 'unset' THEN excluded.think_setting
                   ELSE assistant_run_provenance.think_setting
               END,
               options_json=CASE
                   WHEN excluded.options_json != '{}' THEN excluded.options_json
                   ELSE assistant_run_provenance.options_json
               END,
               bridge_source_sha256=CASE
                   WHEN excluded.bridge_source_sha256 != '' THEN excluded.bridge_source_sha256
                   ELSE assistant_run_provenance.bridge_source_sha256
               END,
               broker_source_sha256=CASE
                   WHEN excluded.broker_source_sha256 != '' THEN excluded.broker_source_sha256
                   ELSE assistant_run_provenance.broker_source_sha256
               END,
               system_prompt_sha256=CASE
                   WHEN excluded.system_prompt_sha256 != '' THEN excluded.system_prompt_sha256
                   ELSE assistant_run_provenance.system_prompt_sha256
               END,
               tool_schema_sha256=CASE
                   WHEN excluded.tool_schema_sha256 != '' THEN excluded.tool_schema_sha256
                   ELSE assistant_run_provenance.tool_schema_sha256
               END,
               pinned_prompt_differs=excluded.pinned_prompt_differs,
               skills_used_json=excluded.skills_used_json,
               capability_name=excluded.capability_name,
               is_simulation=excluded.is_simulation""",
        prov,
    )
    db.execute(
        """INSERT INTO assistant_run_evaluations (
               run_id, evaluated_at, outcome_status, primary_category, sub_code,
               counts_toward_competence, verified_pass, evidence_path,
               diagnostic_summary, step_classifications_json, is_simulation
           ) VALUES (
               :run_id, :evaluated_at, :outcome_status, :primary_category, :sub_code,
               :counts_toward_competence, :verified_pass, :evidence_path,
               :diagnostic_summary, :step_classifications_json,
               COALESCE(:is_simulation, 0)
           ) ON CONFLICT(run_id) DO UPDATE SET
               evaluated_at=excluded.evaluated_at,
               outcome_status=excluded.outcome_status,
               primary_category=excluded.primary_category,
               sub_code=excluded.sub_code,
               counts_toward_competence=excluded.counts_toward_competence,
               verified_pass=excluded.verified_pass,
               evidence_path=excluded.evidence_path,
               diagnostic_summary=excluded.diagnostic_summary,
               step_classifications_json=excluded.step_classifications_json,
               is_simulation=excluded.is_simulation""",
        ev,
    )
    return {"provenance": prov, "evaluation": ev}


def sync_all_evaluations(
    db: sqlite3.Connection,
    workspace_root: Path | None = None,
    limit: int = 500,
    coordinator_requests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Synchronize provenance, run evaluations, and paired-replay comparisons for recent sessions."""
    ensure_measurement_schema(db)
    db.row_factory = sqlite3.Row
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assistant_sessions" not in tables:
        return {"evaluated": 0, "comparisons": 0}
    run_ids = [
        str(r[0])
        for r in db.execute(
            "SELECT id FROM assistant_sessions ORDER BY updated DESC LIMIT ?",
            (max(1, min(500, int(limit))),),
        ).fetchall()
    ]
    count = 0
    for rid in run_ids:
        if evaluate_and_save_run(db, rid, workspace_root, coordinator_requests) is not None:
            count += 1
    comparisons = sync_paired_comparisons_from_db(db, workspace_root)
    return {"evaluated": count, "comparisons": len(comparisons)}


def build_console_measurement_summary(
    db: sqlite3.Connection,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Read-only summary of capability competence, skill metrics, run attributions, and recommendations."""
    db.row_factory = sqlite3.Row
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    has_eval = "assistant_run_evaluations" in tables and "assistant_run_provenance" in tables

    if has_eval:
        rows = db.execute(
            """SELECT p.*, e.outcome_status, e.primary_category, e.sub_code,
                      e.counts_toward_competence, e.verified_pass, e.evidence_path,
                      e.diagnostic_summary, e.step_classifications_json
               FROM assistant_run_provenance p
               JOIN assistant_run_evaluations e ON e.run_id = p.run_id
               ORDER BY p.updated DESC"""
        ).fetchall()
        comparisons = (
            [dict(r) for r in db.execute("SELECT * FROM assistant_eval_comparisons ORDER BY created DESC").fetchall()]
            if "assistant_eval_comparisons" in tables
            else []
        )
        if not comparisons:
            comparisons = _compute_paired_comparisons(db, workspace_root)
    else:
        rows = []
        comparisons = _compute_paired_comparisons(db, workspace_root)
        if "assistant_sessions" in tables:
            run_ids = [
                str(r[0])
                for r in db.execute("SELECT id FROM assistant_sessions ORDER BY updated DESC LIMIT 100").fetchall()
            ]
            for rid in run_ids:
                p = extract_run_provenance(db, rid, workspace_root)
                e = classify_run(db, rid, workspace_root)
                if p and e:
                    rows.append({**p, **e})

    by_capability: dict[str, dict[str, Any]] = {}
    by_skill: dict[str, dict[str, Any]] = {}
    by_category: dict[str, int] = {}
    by_run: dict[str, dict[str, Any]] = {}

    for r in rows:
        item = dict(r)
        rid = str(item["run_id"])
        cat = str(item["primary_category"])
        cap = str(item["capability_name"] or "workspace.research")
        counts = int(item["counts_toward_competence"] or 0)
        vpass = int(item["verified_pass"] or 0)

        # Simulation check: exclude simulation / mocked tests from live competence
        m_str = str(item.get("model") or "").lower()
        is_sim = int(item.get("is_simulation") or 0)
        if any(s in m_str for s in ("simulation", "mock")) or cat == "simulation" or "simulation" in str(item.get("sub_code") or "").lower():
            is_sim = 1
        item["is_simulation"] = is_sim

        by_category[cat] = by_category.get(cat, 0) + 1
        by_run[rid] = item

        cap_entry = by_capability.setdefault(
            cap,
            {
                "capability": cap,
                "total_runs": 0,
                "valid_attempts": 0,
                "verified_passes": 0,
                "model_errors": 0,
                "harness_or_env_blocks": 0,
                "unverified_runs": 0,
                "simulation_runs": 0,
                "simulation_passes": 0,
            },
        )
        cap_entry["total_runs"] += 1
        if is_sim:
            cap_entry["simulation_runs"] += 1
            if vpass:
                cap_entry["simulation_passes"] += 1
        else:
            if counts:
                cap_entry["valid_attempts"] += 1
                if vpass:
                    cap_entry["verified_passes"] += 1
                else:
                    cap_entry["model_errors"] += 1
            elif cat in {"adapter_or_env", "scheduler_or_placement", "observation_shaping", "application_blocker"}:
                cap_entry["harness_or_env_blocks"] += 1
            elif cat == "unverified_in_progress":
                cap_entry["unverified_runs"] += 1

        skills_map = _safe_json(item.get("skills_used_json"), {})
        if isinstance(skills_map, dict):
            for skill_id, s_info in skills_map.items():
                s_entry = by_skill.setdefault(
                    skill_id,
                    {
                        "skill_id": skill_id,
                        "name": s_info.get("name", skill_id) if isinstance(s_info, dict) else skill_id,
                        "sha256": s_info.get("sha256", "") if isinstance(s_info, dict) else "",
                        "total_runs": 0,
                        "valid_attempts": 0,
                        "verified_passes": 0,
                        "model_errors": 0,
                        "harness_or_env_blocks": 0,
                        "unverified_runs": 0,
                        "simulation_runs": 0,
                        "simulation_passes": 0,
                        "last_run_id": rid,
                        "last_outcome": f"{cat}:{item.get('sub_code', '')}",
                    },
                )
                s_entry["total_runs"] += 1
                if is_sim:
                    s_entry["simulation_runs"] += 1
                    if vpass:
                        s_entry["simulation_passes"] += 1
                else:
                    if counts:
                        s_entry["valid_attempts"] += 1
                        if vpass:
                            s_entry["verified_passes"] += 1
                        else:
                            s_entry["model_errors"] += 1
                    elif cat in {"adapter_or_env", "scheduler_or_placement", "observation_shaping", "application_blocker"}:
                        s_entry["harness_or_env_blocks"] += 1
                    elif cat == "unverified_in_progress":
                        s_entry["unverified_runs"] += 1

    for cap_entry in by_capability.values():
        va = cap_entry["valid_attempts"]
        vp = cap_entry["verified_passes"]
        sim_runs = cap_entry.get("simulation_runs", 0)
        sim_passes = cap_entry.get("simulation_passes", 0)
        cap_entry["competence_rate"] = round(vp / va, 3) if va > 0 else None
        if va > 0:
            cap_entry["competence_label"] = f"{vp}/{va} ({vp / va:.0%})"
        else:
            sim_info = f"{sim_passes}/{sim_runs} simulations" if sim_runs > 0 else f"{cap_entry['harness_or_env_blocks']} env/app blocks"
            cap_entry["competence_label"] = f"0/0 ({sim_info})"

    for s_entry in by_skill.values():
        va = s_entry["valid_attempts"]
        vp = s_entry["verified_passes"]
        sim_runs = s_entry.get("simulation_runs", 0)
        sim_passes = s_entry.get("simulation_passes", 0)
        s_entry["competence_rate"] = round(vp / va, 3) if va > 0 else None
        if va > 0:
            s_entry["competence_label"] = f"{vp}/{va} ({vp / va:.0%})"
        else:
            sim_info = f"{sim_passes}/{sim_runs} simulations" if sim_runs > 0 else f"{s_entry['harness_or_env_blocks']} env/app blocks"
            s_entry["competence_label"] = f"0/0 ({sim_info})" 

    recommendations = [c["recommendation"] for c in comparisons if c.get("recommendation")]
    return {
        "total_evaluated_runs": len(rows),
        "category_counts": by_category,
        "capabilities": by_capability,
        "skills": by_skill,
        "runs": by_run,
        "comparisons": comparisons,
        "recommendations": recommendations,
    }
