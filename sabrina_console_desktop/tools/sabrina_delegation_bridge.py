"""Codex delegation bridge for Sabrina.

Provides a reliable bridge for delegating bounded coding tasks to an installed
AI product (OpenAI Codex CLI) and collecting real, attributable results.

Capabilities verified against installed Codex CLI (v0.166.0-alpha.6) help and
official schemas:
- Non-interactive execution via `codex exec`
- Sandboxed execution via `--sandbox read-only` and `--skip-git-repo-check`
- JSONL event streaming via `--json`
- Session/thread continuity via `codex exec resume <thread_id>`
- Live authentication verified via `codex doctor` (chatgpt auth mode)

This module owns only this narrow bridge interface:
- submit(task_contract, operation_id)
- status(assignment_id)
- collect(assignment_id)
- revise(assignment_id, correction, operation_id)
- cancel(assignment_id)
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional
import uuid

from tools.sabrina_workflow_verifier import digest, public_contract, suite_fingerprint, LabVerifier


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data/sabrina_assistant.sqlite3"
DEFAULT_CODEX_PATH = Path(r"C:\Users\bcmcg\AppData\Local\OpenAI\Codex\bin\a159911ff70742e9\codex.exe")

SCHEMA = """
CREATE TABLE IF NOT EXISTS delegation_assignments (
    id TEXT PRIMARY KEY,
    operation_id TEXT UNIQUE NOT NULL,
    task_id TEXT,
    objective_id TEXT,
    contract TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    interface TEXT NOT NULL,
    conversation_key TEXT,
    marker TEXT UNIQUE NOT NULL,
    expected_prompt TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    delivery TEXT NOT NULL DEFAULT 'not_submitted',
    state TEXT NOT NULL DEFAULT 'assigned',
    failure_kind TEXT,
    error_detail TEXT,
    raw_response TEXT,
    extracted_artifact TEXT,
    artifact_hash TEXT,
    metrics TEXT NOT NULL DEFAULT '{}',
    created REAL NOT NULL,
    updated REAL NOT NULL,
    deadline REAL NOT NULL,
    parent_assignment_id TEXT
);
CREATE TABLE IF NOT EXISTS delegation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_delegation_assignment_op ON delegation_assignments(operation_id);
CREATE INDEX IF NOT EXISTS idx_delegation_events_assign ON delegation_events(assignment_id, id);
"""


def find_codex_binary() -> Optional[str]:
    """Locate the installed Codex CLI executable."""
    env_bin = os.environ.get("CODEX_BIN")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    if DEFAULT_CODEX_PATH.is_file():
        return str(DEFAULT_CODEX_PATH)
    which = shutil.which("codex")
    if which:
        return which
    return None


def extract_visible_artifact(raw_text: str, marker: str, expected_files: Optional[List[str]] = None) -> Dict[str, str]:
    """Extract complete, marker-matched JSON artifact from raw provider response.

    Rejects responses missing the required task_marker, incomplete artifacts,
    or files not conforming to the specified contract manifest.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("Empty or non-string provider response")

    text = raw_text.strip()
    decoder = json.JSONDecoder()
    candidates: Dict[str, Dict[str, str]] = {}

    # Extract all candidate JSON objects from the text
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue

        if not isinstance(obj, dict):
            continue
        if obj.get("task_marker") != marker:
            continue
        if obj.get("complete") is not True:
            continue
        files = obj.get("files")
        if not isinstance(files, dict):
            continue

        # Validate file specifications
        if expected_files is not None:
            if set(files.keys()) != set(expected_files):
                continue
        for fname, fcontent in files.items():
            if not isinstance(fname, str) or not isinstance(fcontent, str):
                break
            if len(fcontent.encode("utf-8")) > 65536:
                break
        else:
            candidates[digest(files)] = files

    if len(candidates) != 1:
        raise ValueError(
            f"Expected exactly 1 complete marker-matched artifact for marker {marker}, found {len(candidates)}"
        )
    return next(iter(candidates.values()))


class CodexDelegationBridge:
    """Manages delegation lifecycle to the local Codex CLI."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        codex_bin: Optional[Path | str] = None,
        runner: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.db_path = Path(db_path or os.environ.get("SABRINA_ASSISTANT_DB", str(DEFAULT_DB))).resolve()
        self.codex_bin = str(codex_bin) if codex_bin else find_codex_binary()
        self.runner = runner
        self.clock = clock

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(self, db: sqlite3.Connection, aid: str, op_id: str, kind: str, detail: Any) -> None:
        db.execute(
            "INSERT INTO delegation_events(assignment_id, operation_id, kind, detail, created) VALUES(?,?,?,?,?)",
            (aid, op_id, kind, json.dumps(detail), self.clock()),
        )

    def _execute_process(self, cmd: List[str], timeout: int = 120) -> tuple[int, str, str]:
        """Execute command either through injected runner or isolated subprocess."""
        if self.runner is not None:
            return self.runner(cmd, timeout=timeout)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout, stderr
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=5)
            raise TimeoutError(f"Codex command timed out after {timeout} seconds")
        except Exception as exc:
            proc.kill()
            raise

    def submit(self, task_contract: Dict[str, Any], operation_id: str) -> Dict[str, Any]:
        """Submit a bounded coding task to the Codex provider."""
        if not operation_id or not isinstance(operation_id, str):
            raise ValueError("operation_id must be a non-empty string")
        if not isinstance(task_contract, dict):
            raise ValueError("task_contract must be a dictionary")

        contract = dict(task_contract)
        suite_id = contract.get("suite_id")
        if suite_id and "requirement" not in contract:
            canonical = public_contract(suite_id)
            contract.update(canonical)

        if "requirement" not in contract and "prompt" not in contract:
            raise ValueError("task_contract must contain 'requirement' or 'prompt'")
        requirement = contract.get("requirement", contract.get("prompt", ""))

        files = contract.get("files", ["solution.py"])
        contract_hash = digest(contract)

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM delegation_assignments WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                if existing["contract_hash"] != contract_hash:
                    raise ValueError("Duplicate operation_id with conflicting task contract")
                return self.status(existing["id"])

            aid = uuid.uuid4().hex
            marker = uuid.uuid4().hex
            now = self.clock()
            timeout = int(contract.get("timeout", 120))
            deadline = now + timeout

            artifact_format = contract.get("artifact_format", "python")
            if artifact_format not in {"python", "text"}:
                raise ValueError("artifact_format must be python or text")
            description = "complete plain-text research note as a JSON string" if artifact_format == "text" else "complete Python source as a JSON string"
            files_spec = {f: description for f in files}
            prompt = (
                f"Task marker: {marker}. {requirement} "
                f"Return only JSON with task_marker equal to that marker, complete:true, "
                f"and files:{json.dumps(files_spec)}. "
                "Do not run commands, edit files, use tools, or request approvals. "
                "This is a proposed code artifact; an independent verifier will test it."
            )
            prompt_hash = digest(prompt)

            # Persist assignment before submission
            db.execute(
                """INSERT INTO delegation_assignments(
                    id, operation_id, task_id, objective_id, contract, contract_hash,
                    provider, model, interface, conversation_key, marker,
                    expected_prompt, prompt_hash, delivery, state,
                    metrics, created, updated, deadline, parent_assignment_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    operation_id,
                    contract.get("task_id"),
                    contract.get("objective_id"),
                    json.dumps(contract),
                    contract_hash,
                    "codex",
                    contract.get("model", "chatgpt-tokens"),
                    "cli_exec",
                    None,
                    marker,
                    prompt,
                    prompt_hash,
                    "uncertain",
                    "submitting",
                    "{}",
                    now,
                    now,
                    deadline,
                    None,
                ),
            )
            self._event(db, aid, operation_id, "submission_intent", {"prompt_hash": prompt_hash, "marker": marker})

        # Check binary availability
        if self.codex_bin is None or (self.runner is None and not os.path.isfile(self.codex_bin)):
            with self.connect() as db:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='blocked', failure_kind='provider_unavailable',
                           error_detail='Codex CLI binary not found', delivery='not_submitted', updated=?
                       WHERE id=?""",
                    (self.clock(), aid),
                )
                self._event(db, aid, operation_id, "provider_unavailable", {"detail": "codex.exe not found"})
            return self.status(aid)

        cmd = [self.codex_bin, "exec", "--skip-git-repo-check", "--sandbox", "read-only", "--json", prompt]

        # Execute submission
        try:
            retcode, stdout, stderr = self._execute_process(cmd, timeout=timeout)
        except Exception as exc:
            with self.connect() as db:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='blocked', failure_kind='environment',
                           error_detail=?, delivery='uncertain', updated=?
                       WHERE id=?""",
                    (f"{type(exc).__name__}: {exc}", self.clock(), aid),
                )
                self._event(db, aid, operation_id, "submission_error", {"error": str(exc)})
            return self.status(aid)

        # Parse stream events
        thread_id: Optional[str] = None
        agent_message: Optional[str] = None
        usage: Dict[str, Any] = {}

        for line in stdout.splitlines():
            line_str = line.strip()
            if not line_str.startswith("{"):
                continue
            try:
                ev = json.loads(line_str)
                ev_type = ev.get("type")
                if ev_type == "thread.started":
                    thread_id = ev.get("thread_id")
                elif ev_type == "item.completed":
                    item = ev.get("item", {})
                    if item.get("type") == "agent_message":
                        agent_message = item.get("text")
                elif ev_type == "turn.completed":
                    usage = ev.get("usage", {})
            except Exception:
                continue

        metrics = {
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
            "elapsed_seconds": round(self.clock() - now, 3),
        }

        with self.connect() as db:
            if retcode != 0:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='failed', failure_kind='interface',
                           error_detail=?, delivery='uncertain', metrics=?, updated=?
                       WHERE id=?""",
                    (stderr[:1000] or f"Exit code {retcode}", json.dumps(metrics), self.clock(), aid),
                )
                self._event(db, aid, operation_id, "process_failed", {"returncode": retcode, "stderr": stderr[:500]})
            elif agent_message is None:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='failed', failure_kind='incomplete_output',
                           error_detail='No agent message returned by provider',
                           conversation_key=?, delivery='uncertain', metrics=?, updated=?
                       WHERE id=?""",
                    (thread_id, json.dumps(metrics), self.clock(), aid),
                )
                self._event(db, aid, operation_id, "incomplete_output", {"thread_id": thread_id})
            else:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='submitted', delivery='observed',
                           conversation_key=?, raw_response=?, metrics=?, updated=?
                       WHERE id=?""",
                    (thread_id, agent_message, json.dumps(metrics), self.clock(), aid),
                )
                self._event(db, aid, operation_id, "provider_output_recorded", {
                    "conversation_key": thread_id,
                    "metrics": metrics,
                })

        return self.status(aid)

    def status(self, assignment_id: str) -> Dict[str, Any]:
        """Retrieve current assignment record and event trail."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM delegation_assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown assignment_id: {assignment_id}")
            events = [
                dict(r) for r in db.execute(
                    "SELECT id, kind, detail, created FROM delegation_events WHERE assignment_id=? ORDER BY id",
                    (assignment_id,),
                )
            ]
            record = dict(row)
            record["metrics"] = json.loads(record["metrics"])
            record["contract"] = json.loads(record["contract"])
            record["events"] = events
            return record

    def collect(self, assignment_id: str) -> Dict[str, Any]:
        """Extract and validate the produced artifact from recorded provider response."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM delegation_assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown assignment_id: {assignment_id}")
            if row["state"] in {"blocked", "failed"}:
                raise ValueError(f"Cannot collect from {row['state']} assignment: {row['error_detail']}")
            if row["delivery"] != "observed":
                raise ValueError(f"Cannot collect artifact with unconfirmed delivery state: {row['delivery']}")

            contract = json.loads(row["contract"])
            expected_files = contract.get("files", ["solution.py"])
            raw = row["raw_response"] or ""

            try:
                files = extract_visible_artifact(raw, row["marker"], expected_files)
            except ValueError as exc:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='needs_revision', failure_kind='malformed_response',
                           error_detail=?, updated=?
                       WHERE id=?""",
                    (str(exc), self.clock(), assignment_id),
                )
                self._event(db, assignment_id, row["operation_id"], "artifact_extraction_failed", {"error": str(exc)})
                db.commit()
                raise

            artifact_hash = digest(files)
            db.execute(
                """UPDATE delegation_assignments
                   SET state='collected', extracted_artifact=?, artifact_hash=?, updated=?
                   WHERE id=?""",
                (json.dumps(files), artifact_hash, self.clock(), assignment_id),
            )
            self._event(db, assignment_id, row["operation_id"], "artifact_collected", {
                "artifact_hash": artifact_hash,
                "files": list(files.keys()),
            })

            return {
                "assignment_id": assignment_id,
                "operation_id": row["operation_id"],
                "status": "collected",
                "artifact": files,
                "artifact_hash": artifact_hash,
                "conversation_key": row["conversation_key"],
                "provider": row["provider"],
                "model": row["model"],
                "metrics": json.loads(row["metrics"]),
            }

    def revise(self, assignment_id: str, correction: Dict[str, Any], operation_id: str) -> Dict[str, Any]:
        """Send a corrective follow-up reusing the existing conversation/thread session."""
        if not operation_id or not isinstance(operation_id, str):
            raise ValueError("operation_id must be a non-empty string")
        if not isinstance(correction, dict):
            raise ValueError("correction must be a dictionary")

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM delegation_assignments WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing:
                return self.status(existing["id"])

            parent = db.execute("SELECT * FROM delegation_assignments WHERE id=?", (assignment_id,)).fetchone()
            if not parent:
                raise ValueError(f"Parent assignment not found: {assignment_id}")
            if not parent["conversation_key"]:
                raise ValueError(
                    f"Parent assignment {assignment_id} has no conversation_key to resume"
                )

            contract = json.loads(parent["contract"])
            contract_hash = parent["contract_hash"]
            conversation_key = parent["conversation_key"]

            aid = uuid.uuid4().hex
            marker = uuid.uuid4().hex
            now = self.clock()
            timeout = int(contract.get("timeout", 120))
            deadline = now + timeout

            files = contract.get("files", ["solution.py"])
            files_spec = {f: "complete Python source as a JSON string" for f in files}
            requirement = contract.get("requirement", contract.get("prompt", ""))

            prompt = (
                f"Task marker: {marker}. "
                f"Your previous attempt required correction: {json.dumps(correction)[:1100]}. "
                f"Preserve the original requirement: {requirement} "
                f"Return only JSON with task_marker equal to that marker, complete:true, "
                f"and files:{json.dumps(files_spec)}. "
                "Do not run commands, edit files, use tools, or request approvals. "
                "This is a proposed code artifact; an independent verifier will test it."
            )
            prompt_hash = digest(prompt)

            # Persist child revision assignment before sending
            db.execute(
                """INSERT INTO delegation_assignments(
                    id, operation_id, task_id, objective_id, contract, contract_hash,
                    provider, model, interface, conversation_key, marker,
                    expected_prompt, prompt_hash, delivery, state,
                    metrics, created, updated, deadline, parent_assignment_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    aid,
                    operation_id,
                    parent["task_id"],
                    parent["objective_id"],
                    parent["contract"],
                    contract_hash,
                    parent["provider"],
                    parent["model"],
                    "cli_exec_resume",
                    conversation_key,
                    marker,
                    prompt,
                    prompt_hash,
                    "uncertain",
                    "submitting",
                    "{}",
                    now,
                    now,
                    deadline,
                    assignment_id,
                ),
            )
            self._event(db, aid, operation_id, "revision_intent", {
                "parent_id": assignment_id,
                "conversation_key": conversation_key,
                "prompt_hash": prompt_hash,
            })

        if self.codex_bin is None or (self.runner is None and not os.path.isfile(self.codex_bin)):
            with self.connect() as db:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='blocked', failure_kind='provider_unavailable',
                           error_detail='Codex CLI binary not found', delivery='not_submitted', updated=?
                       WHERE id=?""",
                    (self.clock(), aid),
                )
                self._event(db, aid, operation_id, "provider_unavailable", {"detail": "codex.exe not found"})
            return self.status(aid)

        cmd = [
            self.codex_bin,
            "exec",
            "resume",
            "--skip-git-repo-check",
            "--json",
            conversation_key,
            prompt,
        ]

        try:
            retcode, stdout, stderr = self._execute_process(cmd, timeout=timeout)
        except Exception as exc:
            with self.connect() as db:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='blocked', failure_kind='environment',
                           error_detail=?, delivery='uncertain', updated=?
                       WHERE id=?""",
                    (f"{type(exc).__name__}: {exc}", self.clock(), aid),
                )
                self._event(db, aid, operation_id, "revision_error", {"error": str(exc)})
            return self.status(aid)

        agent_message: Optional[str] = None
        usage: Dict[str, Any] = {}

        for line in stdout.splitlines():
            line_str = line.strip()
            if not line_str.startswith("{"):
                continue
            try:
                ev = json.loads(line_str)
                ev_type = ev.get("type")
                if ev_type == "item.completed":
                    item = ev.get("item", {})
                    if item.get("type") == "agent_message":
                        agent_message = item.get("text")
                elif ev_type == "turn.completed":
                    usage = ev.get("usage", {})
            except Exception:
                continue

        metrics = {
            "input_tokens": usage.get("input_tokens"),
            "cached_input_tokens": usage.get("cached_input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
            "elapsed_seconds": round(self.clock() - now, 3),
        }

        with self.connect() as db:
            if retcode != 0:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='failed', failure_kind='interface',
                           error_detail=?, delivery='uncertain', metrics=?, updated=?
                       WHERE id=?""",
                    (stderr[:1000] or f"Exit code {retcode}", json.dumps(metrics), self.clock(), aid),
                )
                self._event(db, aid, operation_id, "revision_process_failed", {"returncode": retcode, "stderr": stderr[:500]})
            elif agent_message is None:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='failed', failure_kind='incomplete_output',
                           error_detail='No agent message returned in revision',
                           delivery='uncertain', metrics=?, updated=?
                       WHERE id=?""",
                    (json.dumps(metrics), self.clock(), aid),
                )
                self._event(db, aid, operation_id, "revision_incomplete_output", {})
            else:
                db.execute(
                    """UPDATE delegation_assignments
                       SET state='submitted', delivery='observed',
                           raw_response=?, metrics=?, updated=?
                       WHERE id=?""",
                    (agent_message, json.dumps(metrics), self.clock(), aid),
                )
                self._event(db, aid, operation_id, "revision_output_recorded", {"metrics": metrics})

        return self.status(aid)

    def cancel(self, assignment_id: str) -> Dict[str, Any]:
        """Cancel an assignment, returning unsupported when appropriate."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM delegation_assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown assignment_id: {assignment_id}")

            state = row["state"]
            if state in {"collected", "completed", "failed", "blocked"}:
                return {
                    "assignment_id": assignment_id,
                    "status": "unsupported",
                    "reason": f"Assignment is already in finished state: {state}",
                }

            if state == "submitted":
                return {
                    "assignment_id": assignment_id,
                    "status": "unsupported",
                    "reason": "Codex CLI non-interactive execution turn is already finished; external provider cancellation is unsupported",
                }

            # If assigned or submitting but not completed
            db.execute(
                "UPDATE delegation_assignments SET state='cancelled', updated=? WHERE id=?",
                (self.clock(), assignment_id),
            )
            self._event(db, assignment_id, row["operation_id"], "cancelled", {"previous_state": state})
            return {"assignment_id": assignment_id, "status": "cancelled"}

    def reconcile(self, assignment_id: str) -> Dict[str, Any]:
        """Reconcile an uncertain submission without blind duplicate dispatch."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM delegation_assignments WHERE id=?", (assignment_id,)).fetchone()
            if not row:
                raise ValueError(f"Unknown assignment_id: {assignment_id}")

            if row["delivery"] != "uncertain":
                return {"assignment_id": assignment_id, "reconciled": False, "delivery": row["delivery"]}

            raw = row["raw_response"]
            if raw:
                try:
                    extract_visible_artifact(raw, row["marker"])
                    db.execute(
                        "UPDATE delegation_assignments SET delivery='observed', updated=? WHERE id=?",
                        (self.clock(), assignment_id),
                    )
                    self._event(db, assignment_id, row["operation_id"], "reconciled_via_recorded_output", {})
                    return {"assignment_id": assignment_id, "reconciled": True, "delivery": "observed"}
                except ValueError:
                    pass

            return {
                "assignment_id": assignment_id,
                "reconciled": False,
                "delivery": "uncertain",
                "diagnosis": "No matching verifiable output recovered; manual diagnosis required",
            }

    def verify_with_lab(self, assignment_id: str, verifier: Optional[LabVerifier] = None) -> Dict[str, Any]:
        """Submit the collected artifact to the isolated LabVerifier."""
        if verifier is None:
            verifier = LabVerifier()

        status_record = self.status(assignment_id)
        if status_record["state"] != "collected":
            collection = self.collect(assignment_id)
            artifact = collection["artifact"]
        else:
            artifact = json.loads(status_record["extracted_artifact"])

        contract = status_record["contract"]
        payload = {
            "suite_id": contract["suite_id"],
            "suite_sha256": contract["suite_sha256"],
            "files": artifact,
        }

        verdict = verifier.check(payload)
        passed = verdict.get("status") == "passed"

        with self.connect() as db:
            new_state = "verified" if passed else "needs_revision"
            db.execute(
                "UPDATE delegation_assignments SET state=?, updated=? WHERE id=?",
                (new_state, self.clock(), assignment_id),
            )
            self._event(db, assignment_id, status_record["operation_id"], "verification_result", verdict)

        return {
            "assignment_id": assignment_id,
            "passed": passed,
            "verdict": verdict,
            "artifact_hash": status_record["artifact_hash"],
        }
