"""Opt-in, per-run audited host execution interface for Sabrina.

Provides a unified execution and recording layer alongside desktop tools for:
- Python scripts
- PowerShell scripts
- Executable/argument calls
- File operations

Enforces:
- Operation ID idempotency (duplicate submissions never re-execute; mismatched
  arguments with the same operation_id are rejected).
- Lost responses / unconfirmed process outcomes become ``uncertain`` rather than
  automatically repeating a command.
- Broad user-authorized workspace scope while keeping credentials, authentication
  controls, and system protections strictly outside the task.
- Separation between complete execution evidence (retained under per-run 7-day
  default expiry) and bounded model context.
- Harness and grader fingerprint freezing during experiments.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Callable


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RETENTION_DAYS = 7
MAX_MODEL_OUTPUT_CHARS = 3_500
MAX_RAW_CAPTURE_CHARS = 250_000

HOST_ACCESS_LEVELS = {"none", "enabled"}
EXEC_KINDS = {"python", "powershell", "command", "file"}
FILE_ACTIONS = {"read", "write", "append", "list", "stat", "exists"}

# Credentials, authentication controls, and system protections remain outside the task.
_PROTECTED_PATH_PARTS = {
    ".env",
    ".env.local",
    ".env.production",
    ".git",
    ".sandbox-secrets",
    "credentials",
    "secret_store.py",
}
_PROTECTED_FILE_PATTERNS = re.compile(
    r"(?:^|[/\\])(?:\.env(?:\.[A-Za-z0-9_.-]+)?|auth\.json|bridge\.token|client\.token|"
    r"browser_auth_state\.json|.*credential.*|.*secret.*|.*private[_-]?key.*|.*password.*)$",
    re.I,
)
_PROTECTED_COMMAND_PATTERNS = re.compile(
    r"\b(?:regedit|bcdedit|Set-MpPreference|Add-MpPreference|netsh\s+advfirewall|"
    r"sc\.exe\s+(?:stop|config|delete)|takeown|icacls\s+.*(?:/grant|/remove)|"
    r"cmdkey|vaultcmd|mimikatz|lsass|sekurlsa| keyring\b|secret_store|"
    r"Get-Credential|ConvertFrom-SecureString| Export-PfxCertificate)\b",
    re.I,
)
_BLOCKED_EXECUTABLES = {
    "regedit.exe",
    "reg.exe",
    "bcdedit.exe",
    "cmdkey.exe",
    "vaultcmd.exe",
    "runas.exe",
    "takeown.exe",
    "icacls.exe",
    "sc.exe",
    "netsh.exe",
}

DEFAULT_ALLOWED_EXECUTABLES = {
    "python",
    "python.exe",
    "py",
    "py.exe",
    "pytest",
    "pytest.exe",
    "node",
    "node.exe",
    "git",
    "git.exe",
    "codex",
    "codex.exe",
    "claude",
    "claude.exe",
    "agent-desktop",
    "agent-desktop.cmd",
    "agent-desktop-win32-x64.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
}

HARNESS_FINGERPRINT_FILES = (
    "tools/sabrina_lab_assistant_mcp.py",
    "tools/agent_desktop_broker.py",
    "tools/sabrina_coordination.py",
    "tools/sabrina_host_execution.py",
    "tools/agent_desktop_campaign.py",
)

HOST_EXEC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS assistant_host_exec_policy (
    run_id TEXT PRIMARY KEY,
    access TEXT NOT NULL DEFAULT 'none',
    working_directory TEXT NOT NULL DEFAULT '',
    allowed_roots TEXT NOT NULL DEFAULT '[]',
    allowed_executables TEXT NOT NULL DEFAULT '[]',
    updated REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_host_exec_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    kind TEXT NOT NULL,
    working_directory TEXT NOT NULL,
    script_or_command TEXT NOT NULL,
    arguments TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL,
    process_identity TEXT NOT NULL DEFAULT '{}',
    stdout TEXT NOT NULL DEFAULT '',
    stderr TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    created REAL NOT NULL,
    started REAL,
    completed REAL,
    elapsed_seconds REAL,
    produced_artifacts TEXT NOT NULL DEFAULT '[]',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    disposition TEXT NOT NULL DEFAULT '{}',
    expires_at REAL NOT NULL DEFAULT 0,
    UNIQUE(run_id, operation_id)
);

CREATE INDEX IF NOT EXISTS idx_host_exec_run_id
    ON assistant_host_exec_audit(run_id, id);

CREATE TABLE IF NOT EXISTS assistant_desktop_lease (
    lease_key TEXT PRIMARY KEY,
    owner_run_id TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    purpose TEXT NOT NULL DEFAULT ''
);
"""


def compute_harness_fingerprints(
    workspace_root: Path = WORKSPACE_ROOT,
    extra_files: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    """Compute SHA-256 fingerprints for active harness and grader files."""
    fingerprints: dict[str, str] = {}
    all_files = list(dict.fromkeys([*HARNESS_FINGERPRINT_FILES, *extra_files]))
    for rel in all_files:
        target = (workspace_root / rel).resolve()
        if target.is_file():
            fingerprints[rel.replace("\\", "/")] = hashlib.sha256(target.read_bytes()).hexdigest()
        else:
            fingerprints[rel.replace("\\", "/")] = "missing"
    raw_sorted = "|".join(f"{k}:{fingerprints[k]}" for k in sorted(fingerprints))
    fingerprints["combined_sha256"] = hashlib.sha256(raw_sorted.encode("utf-8")).hexdigest()
    return fingerprints


class HarnessFreezeGuard:
    """Tracks harness and grader fingerprints during an experiment or trial."""

    def __init__(
        self,
        workspace_root: Path = WORKSPACE_ROOT,
        extra_files: list[str] | tuple[str, ...] = (),
        baseline: dict[str, str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.extra_files = tuple(extra_files)
        self.baseline = dict(
            baseline
            if baseline is not None
            else compute_harness_fingerprints(self.workspace_root, self.extra_files)
        )

    def check(self) -> dict[str, Any]:
        current = compute_harness_fingerprints(self.workspace_root, self.extra_files)
        changed: dict[str, dict[str, str]] = {}
        for path_key, expected_sha in self.baseline.items():
            actual_sha = current.get(path_key, "missing")
            if actual_sha != expected_sha:
                changed[path_key] = {"expected": expected_sha, "actual": actual_sha}
        return {
            "frozen": len(changed) == 0,
            "valid": len(changed) == 0,
            "baseline_fingerprints": self.baseline,
            "current_fingerprints": current,
            "current_sha256": current.get("combined_sha256", ""),
            "changed_files": changed,
        }


def ensure_host_exec_schema(db: Any) -> None:
    db.executescript(HOST_EXEC_SCHEMA_SQL)


def expire_host_exec_rows(db: Any, run_ids: list[str], now: float | None = None) -> None:
    """Erase raw execution scripts and outputs upon run retention expiry."""
    if not run_ids:
        return
    now = time.time() if now is None else float(now)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "assistant_host_exec_audit" in tables:
        for run_id in run_ids:
            db.execute("DELETE FROM assistant_host_exec_audit WHERE run_id=?", (run_id,))
    if "assistant_host_exec_policy" in tables:
        for run_id in run_ids:
            db.execute(
                "UPDATE assistant_host_exec_policy SET access='none', updated=? WHERE run_id=?",
                (now, run_id),
            )
    if "assistant_desktop_lease" in tables:
        for run_id in run_ids:
            db.execute("DELETE FROM assistant_desktop_lease WHERE owner_run_id=?", (run_id,))


def bounded_host_exec_json(record: dict[str, Any], limit: int = MAX_MODEL_OUTPUT_CHARS) -> str:
    """Return a bounded JSON representation for model context while full evidence stays in SQLite."""
    summary = {
        "ok": record.get("state") == "completed" and (record.get("exit_code") in (0, None)),
        "operation_id": record.get("operation_id"),
        "event_id": record.get("id"),
        "run_id": record.get("run_id"),
        "kind": record.get("kind"),
        "state": record.get("state"),
        "working_directory": record.get("working_directory"),
        "process_identity": record.get("process_identity", {}),
        "exit_code": record.get("exit_code"),
        "timing": record.get("timing", {}),
        "produced_artifacts": record.get("produced_artifacts", []),
        "duplicate_submission": bool(record.get("duplicate_submission", False)),
        "reexecuted": bool(record.get("reexecuted", False)),
        "disposition": record.get("disposition", {}),
    }
    if record.get("error_code"):
        summary["error"] = {
            "code": record["error_code"],
            "message": str(record.get("error_message", ""))[:400],
            "disposition": record.get("disposition", {}),
        }
    stdout = str(record.get("stdout") or "")
    stderr = str(record.get("stderr") or "")
    budget = max(200, (limit - len(json.dumps(summary, ensure_ascii=False))) // 2)
    summary["stdout_preview"] = stdout[:budget] + (" [truncated; full output in audit]" if len(stdout) > budget else "")
    summary["stderr_preview"] = stderr[:budget] + (" [truncated; full output in audit]" if len(stderr) > budget else "")
    summary["stdout_total_chars"] = len(stdout)
    summary["stderr_total_chars"] = len(stderr)
    encoded = json.dumps(summary, ensure_ascii=False)
    if len(encoded) <= limit:
        return encoded
    summary["stdout_preview"] = stdout[:250]
    summary["stderr_preview"] = stderr[:250]
    return json.dumps(summary, ensure_ascii=False)


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": description, "parameters": params}}


HOST_EXECUTION_SCHEMAS = [
    _schema(
        "host_exec_start",
        "Start an audited Python script, PowerShell script, or executable command in the run's authorized workspace. Reuse operation_id to check an existing submission; a lost response yields state='uncertain' and must not be blindly repeated.",
        {
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "kind": {"type": "string", "enum": ["python", "powershell", "command"]},
            "script": {"type": "string", "description": "Inline script or relative script path for python/powershell."},
            "executable": {"type": "string", "description": "Executable name or path when kind='command'."},
            "args": {"type": "array", "items": {"type": "string"}, "maxItems": 32},
            "working_directory": {"type": "string", "description": "Optional project-relative working directory."},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 60},
            "declared_artifacts": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
        },
        ["operation_id", "kind"],
    ),
    _schema(
        "host_exec_poll",
        "Poll status and output of a previously started host execution operation without re-running it.",
        {
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 30},
        },
        ["operation_id"],
    ),
    _schema(
        "host_exec_cancel",
        "Cancel a running host execution operation and retain its partial audit evidence.",
        {
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        ["operation_id"],
    ),
    _schema(
        "host_exec_result",
        "Retrieve the bounded result and produced artifact metadata for a host execution operation.",
        {
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        ["operation_id"],
    ),
    _schema(
        "host_file_op",
        "Perform an audited file operation (read, write, append, list, stat, exists) within the run's authorized workspace roots.",
        {
            "operation_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "action": {"type": "string", "enum": sorted(FILE_ACTIONS)},
            "relative_path": {"type": "string"},
            "content": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "line_count": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        ["operation_id", "action", "relative_path"],
    ),
]


class _ActiveProcessHandle:
    def __init__(
        self,
        proc: subprocess.Popen[str],
        stdout_chunks: list[str],
        stderr_chunks: list[str],
        reader_threads: list[threading.Thread],
        started_at: float,
    ) -> None:
        self.proc = proc
        self.stdout_chunks = stdout_chunks
        self.stderr_chunks = stderr_chunks
        self.reader_threads = reader_threads
        self.started_at = started_at


class HostExecutionBroker:
    """Per-run audited execution broker for Python, PowerShell, CLI commands, and file ops."""

    def __init__(
        self,
        store: Any,
        workspace_root: Path = WORKSPACE_ROOT,
        clock: Callable[[], float] = time.time,
        fault_injector: Callable[[str, dict[str, Any]], str | None] | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = Path(workspace_root).resolve()
        self.clock = clock
        self.fault_injector = fault_injector
        self._lock = threading.Lock()
        self._handles: dict[tuple[str, str], _ActiveProcessHandle] = {}
        with self.store.connect(True) as db:
            ensure_host_exec_schema(db)

    def configure(
        self,
        run_id: str,
        access: str | None = None,
        *,
        working_directory: str | None = None,
        allowed_roots: list[str] | None = None,
        allowed_executables: list[str] | None = None,
    ) -> dict[str, Any]:
        with self.store.connect(True) as db:
            ensure_host_exec_schema(db)
            current = db.execute(
                "SELECT * FROM assistant_host_exec_policy WHERE run_id=?",
                (run_id,),
            ).fetchone()
            selected_access = (access or (current["access"] if current else "none")).strip().lower()
            if selected_access not in HOST_ACCESS_LEVELS:
                raise ValueError("host_access must be 'none' or 'enabled'")
            roots = (
                [str(Path(r)) for r in allowed_roots]
                if allowed_roots is not None
                else (json.loads(current["allowed_roots"]) if current else [str(self.workspace_root)])
            )
            resolved_roots: list[str] = []
            for r in roots:
                cand = (self.workspace_root / r).resolve() if not Path(r).is_absolute() else Path(r).resolve()
                if not cand.is_relative_to(self.workspace_root):
                    raise ValueError(f"allowed root {r!r} must be inside workspace root {self.workspace_root}")
                resolved_roots.append(str(cand))
            workdir = working_directory or (current["working_directory"] if current and current["working_directory"] else str(self.workspace_root))
            resolved_workdir = (self.workspace_root / workdir).resolve() if not Path(workdir).is_absolute() else Path(workdir).resolve()
            if not any(resolved_workdir.is_relative_to(Path(root)) for root in resolved_roots):
                raise ValueError("working_directory must be inside an allowed workspace root")
            execs = (
                sorted({e.strip().lower() for e in allowed_executables if e.strip()})
                if allowed_executables is not None
                else (json.loads(current["allowed_executables"]) if current and current["allowed_executables"] else sorted(DEFAULT_ALLOWED_EXECUTABLES))
            )
            now = self.clock()
            db.execute(
                """INSERT INTO assistant_host_exec_policy
                   (run_id, access, working_directory, allowed_roots, allowed_executables, updated)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                   access=excluded.access,
                   working_directory=excluded.working_directory,
                   allowed_roots=excluded.allowed_roots,
                   allowed_executables=excluded.allowed_executables,
                   updated=excluded.updated""",
                (
                    run_id,
                    selected_access,
                    str(resolved_workdir),
                    json.dumps(resolved_roots),
                    json.dumps(execs),
                    now,
                ),
            )
        return self.policy(run_id)

    def policy(self, run_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            ensure_host_exec_schema(db)
            row = db.execute(
                "SELECT * FROM assistant_host_exec_policy WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return {
                "run_id": run_id,
                "access": "none",
                "working_directory": str(self.workspace_root),
                "allowed_roots": [str(self.workspace_root)],
                "allowed_executables": sorted(DEFAULT_ALLOWED_EXECUTABLES),
            }
        return {
            "run_id": run_id,
            "access": row["access"],
            "working_directory": row["working_directory"],
            "allowed_roots": json.loads(row["allowed_roots"]),
            "allowed_executables": json.loads(row["allowed_executables"]),
            "updated": row["updated"],
        }

    def tool_schemas(self, run_id: str) -> list[dict[str, Any]]:
        if self.policy(run_id)["access"] != "enabled":
            return []
        return list(HOST_EXECUTION_SCHEMAS)

    def _require_enabled_and_unexpired(self, run_id: str) -> tuple[dict[str, Any], float]:
        pol = self.policy(run_id)
        if pol["access"] != "enabled":
            raise ValueError("host execution access is not enabled for this research run")
        expires_at = self.clock() + DEFAULT_RETENTION_DAYS * 86400
        with self.store.connect() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "assistant_sessions" in tables:
                session = db.execute(
                    "SELECT state, desktop_audit_expires FROM assistant_sessions WHERE id=?",
                    (run_id,),
                ).fetchone()
                if session is None:
                    raise ValueError("Unknown research run_id")
                if session["state"] == "expired" or (
                    session["desktop_audit_expires"] and float(session["desktop_audit_expires"]) <= self.clock()
                ):
                    raise ValueError("Run audit retention expired; host execution is unavailable")
                if session["desktop_audit_expires"]:
                    expires_at = float(session["desktop_audit_expires"])
        return pol, expires_at

    def _validate_path(self, pol: dict[str, Any], path_str: str, *, allow_missing: bool = True) -> Path:
        raw = (path_str or "").strip()
        if not raw:
            raise ValueError("path is required")
        base = Path(pol["working_directory"])
        candidate = (base / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        allowed_roots = [Path(r).resolve() for r in pol["allowed_roots"]]
        if not any(candidate.is_relative_to(root) for root in allowed_roots):
            raise ValueError(f"path {raw!r} is outside this run's allowed workspace roots")
        rel_str = candidate.relative_to(self.workspace_root).as_posix()
        parts_lower = {p.lower() for p in Path(rel_str).parts}
        if parts_lower & _PROTECTED_PATH_PARTS or _PROTECTED_FILE_PATTERNS.search(rel_str):
            raise ValueError(f"access to credential, secret, or protected path {rel_str!r} is blocked")
        if not allow_missing and not candidate.exists():
            raise FileNotFoundError(f"path not found: {rel_str}")
        return candidate

    @staticmethod
    def _validate_command_safety(text: str, args: list[str] | None = None) -> None:
        combined = " ".join([text or "", *(args or [])])
        if _PROTECTED_COMMAND_PATTERNS.search(combined) or _PROTECTED_FILE_PATTERNS.search(combined):
            raise ValueError(
                "host execution is blocked on credentials, authentication controls, or system protections"
            )

    @staticmethod
    def _validate_operation_id(operation_id: Any) -> str:
        if not isinstance(operation_id, str) or not 1 <= len(operation_id.strip()) <= 128:
            raise ValueError("operation_id must be a string of 1-128 characters")
        op_id = operation_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", op_id):
            raise ValueError("operation_id contains invalid characters")
        return op_id

    def _snapshot_artifacts(self, pol: dict[str, Any], declared: list[str] | None) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for rel in declared or []:
            try:
                target = self._validate_path(pol, rel, allow_missing=True)
            except Exception:
                continue
            if target.is_file():
                data = target.read_bytes()
                artifacts.append({
                    "path": target.relative_to(self.workspace_root).as_posix(),
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                })
        return artifacts

    def start(self, run_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        pol, expires_at = self._require_enabled_and_unexpired(run_id)
        op_id = self._validate_operation_id(arguments.get("operation_id"))
        kind = str(arguments.get("kind", "")).strip().lower()
        if kind not in {"python", "powershell", "command"}:
            raise ValueError("kind must be 'python', 'powershell', or 'command'")

        workdir_arg = str(arguments.get("working_directory") or "").strip()
        workdir = self._validate_path(pol, workdir_arg, allow_missing=False) if workdir_arg else Path(pol["working_directory"])
        if not workdir.is_dir():
            raise ValueError("working_directory must be an existing directory")

        script = str(arguments.get("script") or "").strip()
        executable = str(arguments.get("executable") or "").strip()
        raw_args = arguments.get("args") or []
        if not isinstance(raw_args, list) or not all(isinstance(x, str) for x in raw_args):
            raise ValueError("args must be a list of strings")
        args_list = [str(x) for x in raw_args]
        declared_artifacts = arguments.get("declared_artifacts") or []
        if not isinstance(declared_artifacts, list):
            raise ValueError("declared_artifacts must be a list of relative paths")

        digest_payload = {
            "kind": kind,
            "script": script,
            "executable": executable,
            "args": args_list,
            "working_directory": str(workdir),
        }
        digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode("utf-8")).hexdigest()

        with self.store.connect(True) as db:
            existing = db.execute(
                "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                (run_id, op_id),
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise ValueError("operation_id was already used with different arguments")
                rec = self._reconcile_row(run_id, dict(existing))
                rec["duplicate_submission"] = True
                rec["reexecuted"] = False
                return rec

        # Validate safety before building command
        self._validate_command_safety(script or executable, args_list)
        for arg in args_list:
            if any(part in arg.lower() for part in (".env", "secret_store", "auth.json", "bridge.token", "client.token")):
                raise ValueError("command arguments reference a protected credential or secret file")

        cmd: list[str]
        script_or_command_display: str
        if kind == "python":
            if not script:
                raise ValueError("python execution requires 'script' (inline code or relative .py file)")
            cand_file = (workdir / script).resolve()
            if cand_file.is_file() and cand_file.suffix.lower() == ".py":
                self._validate_path(pol, str(cand_file), allow_missing=False)
                cmd = [sys.executable, str(cand_file), *args_list]
                script_or_command_display = f"{cand_file.relative_to(self.workspace_root).as_posix()}"
            else:
                cmd = [sys.executable, "-c", script, *args_list]
                script_or_command_display = script
        elif kind == "powershell":
            if not script:
                raise ValueError("powershell execution requires 'script'")
            ps_bin = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            exe_str = str(ps_bin) if ps_bin.is_file() else "powershell.exe"
            cand_file = (workdir / script).resolve()
            if cand_file.is_file() and cand_file.suffix.lower() == ".ps1":
                self._validate_path(pol, str(cand_file), allow_missing=False)
                cmd = [exe_str, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(cand_file), *args_list]
                script_or_command_display = f"{cand_file.relative_to(self.workspace_root).as_posix()}"
            else:
                cmd = [exe_str, "-NoProfile", "-NonInteractive", "-Command", script, *args_list]
                script_or_command_display = script
        else:
            if not executable:
                raise ValueError("command execution requires 'executable'")
            exe_name = Path(executable).name.lower()
            if exe_name in _BLOCKED_EXECUTABLES:
                raise ValueError(f"executable {executable!r} is a blocked system/credential utility")
            allowed_execs = {e.lower() for e in pol["allowed_executables"]}
            if exe_name not in allowed_execs and executable.lower() not in allowed_execs:
                raise ValueError(f"executable {executable!r} is not in this run's allowed_executables")
            resolved_exe = executable
            if exe_name in {"python", "python.exe", "py", "py.exe"}:
                resolved_exe = sys.executable
            cmd = [resolved_exe, *args_list]
            script_or_command_display = executable

        created_at = self.clock()
        started_at = self.clock()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workdir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except Exception as exc:
            disposition = {"delivery": "not_delivered", "retry": "safe_after_argument_change"}
            with self.store.connect(True) as db:
                row_id = db.execute(
                    """INSERT INTO assistant_host_exec_audit
                       (operation_id, run_id, digest, kind, working_directory, script_or_command,
                        arguments, state, process_identity, stdout, stderr, exit_code,
                        created, started, completed, elapsed_seconds, produced_artifacts,
                        error_code, error_message, disposition, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', '{}', '', '', NULL,
                               ?, ?, ?, 0.0, '[]', 'SPAWN_FAILED', ?, ?, ?)""",
                    (
                        op_id,
                        run_id,
                        digest,
                        kind,
                        str(workdir),
                        script_or_command_display,
                        json.dumps(args_list),
                        created_at,
                        started_at,
                        started_at,
                        f"{type(exc).__name__}: {exc}",
                        json.dumps(disposition),
                        expires_at,
                    ),
                ).lastrowid
                row = dict(db.execute("SELECT * FROM assistant_host_exec_audit WHERE id=?", (row_id,)).fetchone())
            return self._format_row(row)

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _drain(pipe: Any, sink: list[str]) -> None:
            try:
                if pipe is None:
                    return
                while True:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    sink.append(chunk)
            except Exception:
                pass

        t_out = threading.Thread(target=_drain, args=(proc.stdout, stdout_chunks), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, stderr_chunks), daemon=True)
        t_out.start()
        t_err.start()

        process_identity = {
            "pid": proc.pid,
            "executable": cmd[0],
            "started_at": started_at,
            "declared_artifacts": list(declared_artifacts),
        }

        handle = _ActiveProcessHandle(proc, stdout_chunks, stderr_chunks, [t_out, t_err], started_at)
        with self._lock:
            self._handles[(run_id, op_id)] = handle

        with self.store.connect(True) as db:
            db.execute(
                """INSERT INTO assistant_host_exec_audit
                   (operation_id, run_id, digest, kind, working_directory, script_or_command,
                    arguments, state, process_identity, stdout, stderr, exit_code,
                    created, started, completed, elapsed_seconds, produced_artifacts,
                    error_code, error_message, disposition, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, '', '', NULL,
                           ?, ?, NULL, NULL, '[]', '', '', ?, ?)""",
                (
                    op_id,
                    run_id,
                    digest,
                    kind,
                    str(workdir),
                    script_or_command_display,
                    json.dumps(args_list),
                    json.dumps(process_identity),
                    created_at,
                    started_at,
                    json.dumps({"delivery": "in_progress", "retry": "unsafe"}),
                    expires_at,
                ),
            )

        wait_seconds = float(arguments.get("wait_seconds", 5.0))
        if wait_seconds > 0:
            return self.poll(run_id, {"operation_id": op_id, "wait_seconds": min(60.0, wait_seconds)})
        return self.poll(run_id, {"operation_id": op_id, "wait_seconds": 0.0})

    def poll(self, run_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled_and_unexpired(run_id)
        op_id = self._validate_operation_id(arguments.get("operation_id"))
        wait_seconds = max(0.0, min(30.0, float(arguments.get("wait_seconds", 0.0))))

        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                (run_id, op_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown host execution operation_id: {op_id!r}")

        return self._reconcile_row(run_id, dict(row), wait_seconds=wait_seconds)

    def cancel(self, run_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled_and_unexpired(run_id)
        op_id = self._validate_operation_id(arguments.get("operation_id"))
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                (run_id, op_id),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown host execution operation_id: {op_id!r}")
        row_dict = dict(row)
        if row_dict["state"] in {"completed", "failed", "cancelled", "uncertain"}:
            return self._format_row(row_dict)

        with self._lock:
            handle = self._handles.get((run_id, op_id))

        if handle is not None:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(handle.proc.pid)],
                        capture_output=True,
                        timeout=5,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                handle.proc.kill()
            except Exception:
                pass
            try:
                handle.proc.wait(timeout=3)
            except Exception:
                pass
            for t in handle.reader_threads:
                t.join(timeout=1.0)
            stdout_text = "".join(handle.stdout_chunks)[:MAX_RAW_CAPTURE_CHARS]
            stderr_text = "".join(handle.stderr_chunks)[:MAX_RAW_CAPTURE_CHARS]
            exit_code = handle.proc.poll()
            with self._lock:
                self._handles.pop((run_id, op_id), None)
        else:
            stdout_text = row_dict["stdout"]
            stderr_text = row_dict["stderr"]
            exit_code = row_dict["exit_code"]

        completed_at = self.clock()
        started_at = float(row_dict["started"] or row_dict["created"] or completed_at)
        elapsed = max(0.0, completed_at - started_at)
        disposition = {"delivery": "cancelled", "retry": "unsafe_without_inspection"}

        with self.store.connect(True) as db:
            db.execute(
                """UPDATE assistant_host_exec_audit
                   SET state='cancelled', stdout=?, stderr=?, exit_code=?,
                       completed=?, elapsed_seconds=?, error_code='EXECUTION_CANCELLED',
                       error_message='Operation cancelled by caller', disposition=?
                   WHERE run_id=? AND operation_id=?""",
                (
                    stdout_text,
                    stderr_text,
                    exit_code,
                    completed_at,
                    elapsed,
                    json.dumps(disposition),
                    run_id,
                    op_id,
                ),
            )
            updated = dict(
                db.execute(
                    "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                    (run_id, op_id),
                ).fetchone()
            )
        return self._format_row(updated)

    def result(self, run_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.poll(run_id, {"operation_id": arguments.get("operation_id"), "wait_seconds": 0.0})

    def file_op(self, run_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        pol, expires_at = self._require_enabled_and_unexpired(run_id)
        op_id = self._validate_operation_id(arguments.get("operation_id"))
        action = str(arguments.get("action", "")).strip().lower()
        if action not in FILE_ACTIONS:
            raise ValueError(f"action must be one of {sorted(FILE_ACTIONS)}")
        rel_path = str(arguments.get("relative_path", "")).strip()
        content = arguments.get("content")
        start_line = int(arguments.get("start_line", 1))
        line_count = int(arguments.get("line_count", 50))

        digest_payload = {
            "kind": "file",
            "action": action,
            "relative_path": rel_path,
            "content": content if action in {"write", "append"} else None,
            "start_line": start_line if action == "read" else None,
            "line_count": line_count if action == "read" else None,
        }
        digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True).encode("utf-8")).hexdigest()

        with self.store.connect(True) as db:
            existing = db.execute(
                "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                (run_id, op_id),
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    raise ValueError("operation_id was already used with different arguments")
                rec = self._format_row(dict(existing))
                rec["duplicate_submission"] = True
                rec["reexecuted"] = False
                return rec

        created_at = self.clock()
        target = self._validate_path(pol, rel_path, allow_missing=action in {"write", "append", "exists", "stat"})
        rel_norm = target.relative_to(self.workspace_root).as_posix()

        if self.fault_injector and self.fault_injector("file_op", {"operation_id": op_id, "action": action}) == "lost_response":
            disposition = {"delivery": "unknown", "retry": "unsafe"}
            with self.store.connect(True) as db:
                db.execute(
                    """INSERT INTO assistant_host_exec_audit
                       (operation_id, run_id, digest, kind, working_directory, script_or_command,
                        arguments, state, process_identity, stdout, stderr, exit_code,
                        created, started, completed, elapsed_seconds, produced_artifacts,
                        error_code, error_message, disposition, expires_at)
                       VALUES (?, ?, ?, 'file', ?, ?, ?, 'uncertain', ?, '', '', NULL,
                               ?, ?, ?, 0.0, '[]', 'LOST_EXECUTION_RESPONSE',
                               'File operation response was lost; outcome is uncertain. Inspect before retrying.',
                               ?, ?)""",
                    (
                        op_id,
                        run_id,
                        digest,
                        pol["working_directory"],
                        f"file:{action}:{rel_norm}",
                        json.dumps({"action": action, "relative_path": rel_norm}),
                        json.dumps({"pid": os.getpid(), "in_process": True}),
                        created_at,
                        created_at,
                        created_at,
                        json.dumps(disposition),
                        expires_at,
                    ),
                )
                row = dict(
                    db.execute(
                        "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                        (run_id, op_id),
                    ).fetchone()
                )
            return self._format_row(row)

        stdout_text = ""
        stderr_text = ""
        state = "completed"
        exit_code = 0
        error_code = ""
        error_message = ""
        produced_artifacts: list[dict[str, Any]] = []
        disposition = {"delivery": "delivered_verified", "retry": "safe" if action in {"read", "list", "stat", "exists"} else "unsafe"}

        try:
            if action == "exists":
                stdout_text = json.dumps({"path": rel_norm, "exists": target.exists(), "is_file": target.is_file(), "is_dir": target.is_dir()})
            elif action == "stat":
                if not target.exists():
                    stdout_text = json.dumps({"path": rel_norm, "exists": False})
                else:
                    st = target.stat()
                    info: dict[str, Any] = {
                        "path": rel_norm,
                        "exists": True,
                        "size_bytes": st.st_size,
                        "mtime": st.st_mtime,
                        "is_file": target.is_file(),
                        "is_dir": target.is_dir(),
                    }
                    if target.is_file() and st.st_size <= 5_000_000:
                        info["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                    stdout_text = json.dumps(info)
            elif action == "list":
                if not target.is_dir():
                    raise ValueError(f"not a directory: {rel_norm}")
                entries = []
                for child in sorted(target.iterdir(), key=lambda p: p.name)[:200]:
                    c_rel = child.relative_to(self.workspace_root).as_posix()
                    if _PROTECTED_FILE_PATTERNS.search(c_rel) or child.name.lower() in _PROTECTED_PATH_PARTS:
                        continue
                    entries.append({
                        "name": child.name,
                        "path": c_rel,
                        "is_dir": child.is_dir(),
                        "size_bytes": child.stat().st_size if child.is_file() else 0,
                    })
                stdout_text = json.dumps({"path": rel_norm, "entries": entries})
            elif action == "read":
                if not target.is_file():
                    raise FileNotFoundError(f"file not found: {rel_norm}")
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                s_idx = max(1, start_line)
                cnt = max(1, min(200, line_count))
                selected = lines[s_idx - 1 : s_idx - 1 + cnt]
                stdout_text = "\n".join(f"{s_idx + idx}: {line}" for idx, line in enumerate(selected))
            elif action in {"write", "append"}:
                if not isinstance(content, str):
                    raise ValueError(f"{action} requires string 'content'")
                self._validate_command_safety(content)
                target.parent.mkdir(parents=True, exist_ok=True)
                if action == "write":
                    target.write_text(content, encoding="utf-8", newline="\n")
                else:
                    with target.open("a", encoding="utf-8", newline="\n") as fh:
                        fh.write(content)
                raw_bytes = target.read_bytes()
                artifact_entry = {
                    "path": rel_norm,
                    "size_bytes": len(raw_bytes),
                    "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                }
                produced_artifacts.append(artifact_entry)
                stdout_text = json.dumps({"written": artifact_entry, "action": action})
        except Exception as exc:
            state = "failed"
            exit_code = 1
            error_code = "FILE_OP_FAILED"
            error_message = f"{type(exc).__name__}: {exc}"
            stderr_text = error_message
            disposition = {"delivery": "not_delivered", "retry": "safe_after_argument_change"}

        completed_at = self.clock()
        elapsed = max(0.0, completed_at - created_at)
        with self.store.connect(True) as db:
            db.execute(
                """INSERT INTO assistant_host_exec_audit
                   (operation_id, run_id, digest, kind, working_directory, script_or_command,
                    arguments, state, process_identity, stdout, stderr, exit_code,
                    created, started, completed, elapsed_seconds, produced_artifacts,
                    error_code, error_message, disposition, expires_at)
                   VALUES (?, ?, ?, 'file', ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    op_id,
                    run_id,
                    digest,
                    pol["working_directory"],
                    f"file:{action}:{rel_norm}",
                    json.dumps({"action": action, "relative_path": rel_norm}),
                    state,
                    json.dumps({"pid": os.getpid(), "in_process": True}),
                    stdout_text[:MAX_RAW_CAPTURE_CHARS],
                    stderr_text[:MAX_RAW_CAPTURE_CHARS],
                    exit_code,
                    created_at,
                    created_at,
                    completed_at,
                    elapsed,
                    json.dumps(produced_artifacts),
                    error_code,
                    error_message,
                    json.dumps(disposition),
                    expires_at,
                ),
            )
            row = dict(
                db.execute(
                    "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                    (run_id, op_id),
                ).fetchone()
            )
        return self._format_row(row)

    def _reconcile_row(self, run_id: str, row_dict: dict[str, Any], wait_seconds: float = 0.0) -> dict[str, Any]:
        op_id = row_dict["operation_id"]
        if row_dict["state"] != "running":
            return self._format_row(row_dict)

        if self.fault_injector and self.fault_injector("poll", {"operation_id": op_id, "run_id": run_id}) == "lost_response":
            with self._lock:
                handle = self._handles.pop((run_id, op_id), None)
            if handle is not None:
                try:
                    handle.proc.kill()
                except Exception:
                    pass
            now = self.clock()
            started = float(row_dict["started"] or row_dict["created"] or now)
            disposition = {"delivery": "unknown", "retry": "unsafe"}
            with self.store.connect(True) as db:
                db.execute(
                    """UPDATE assistant_host_exec_audit
                       SET state='uncertain', completed=?, elapsed_seconds=?,
                           error_code='LOST_EXECUTION_RESPONSE',
                           error_message='Process response was lost before completion was confirmed; do not repeat blindly.',
                           disposition=?
                       WHERE run_id=? AND operation_id=?""",
                    (now, max(0.0, now - started), json.dumps(disposition), run_id, op_id),
                )
                updated = dict(
                    db.execute(
                        "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                        (run_id, op_id),
                    ).fetchone()
                )
            return self._format_row(updated)

        with self._lock:
            handle = self._handles.get((run_id, op_id))

        if handle is None:
            # Handle vanished (e.g., broker restarted while child process was in flight) -> uncertain outcome!
            now = self.clock()
            started = float(row_dict["started"] or row_dict["created"] or now)
            disposition = {"delivery": "unknown", "retry": "unsafe"}
            with self.store.connect(True) as db:
                db.execute(
                    """UPDATE assistant_host_exec_audit
                       SET state='uncertain', completed=?, elapsed_seconds=?,
                           error_code='UNCONFIRMED_PROCESS_STATE',
                           error_message='Active process handle was lost; completion is uncertain. Inspect workspace before retrying.',
                           disposition=?
                       WHERE run_id=? AND operation_id=?""",
                    (now, max(0.0, now - started), json.dumps(disposition), run_id, op_id),
                )
                updated = dict(
                    db.execute(
                        "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                        (run_id, op_id),
                    ).fetchone()
                )
            return self._format_row(updated)

        if wait_seconds > 0:
            try:
                handle.proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                pass

        exit_code = handle.proc.poll()
        if exit_code is None:
            stdout_partial = "".join(handle.stdout_chunks)[:MAX_RAW_CAPTURE_CHARS]
            stderr_partial = "".join(handle.stderr_chunks)[:MAX_RAW_CAPTURE_CHARS]
            now = self.clock()
            started = float(row_dict["started"] or row_dict["created"] or now)
            with self.store.connect(True) as db:
                db.execute(
                    """UPDATE assistant_host_exec_audit
                       SET stdout=?, stderr=?, elapsed_seconds=?
                       WHERE run_id=? AND operation_id=?""",
                    (stdout_partial, stderr_partial, max(0.0, now - started), run_id, op_id),
                )
                updated = dict(
                    db.execute(
                        "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                        (run_id, op_id),
                    ).fetchone()
                )
            return self._format_row(updated)

        for t in handle.reader_threads:
            t.join(timeout=1.0)
        stdout_text = "".join(handle.stdout_chunks)[:MAX_RAW_CAPTURE_CHARS]
        stderr_text = "".join(handle.stderr_chunks)[:MAX_RAW_CAPTURE_CHARS]
        with self._lock:
            self._handles.pop((run_id, op_id), None)

        completed_at = self.clock()
        started_at = float(row_dict["started"] or row_dict["created"] or completed_at)
        elapsed = max(0.0, completed_at - started_at)
        proc_meta = json.loads(row_dict["process_identity"] or "{}")
        pol = self.policy(run_id)
        artifacts = self._snapshot_artifacts(pol, proc_meta.get("declared_artifacts", []))
        state = "completed" if exit_code == 0 else "failed"
        error_code = "" if exit_code == 0 else "NONZERO_EXIT"
        error_message = "" if exit_code == 0 else f"Process exited with code {exit_code}"
        disposition = {
            "delivery": "delivered_verified",
            "retry": "unsafe_without_inspection" if exit_code != 0 else "not_needed",
        }

        with self.store.connect(True) as db:
            db.execute(
                """UPDATE assistant_host_exec_audit
                   SET state=?, stdout=?, stderr=?, exit_code=?, completed=?,
                       elapsed_seconds=?, produced_artifacts=?, error_code=?,
                       error_message=?, disposition=?
                   WHERE run_id=? AND operation_id=?""",
                (
                    state,
                    stdout_text,
                    stderr_text,
                    exit_code,
                    completed_at,
                    elapsed,
                    json.dumps(artifacts),
                    error_code,
                    error_message,
                    json.dumps(disposition),
                    run_id,
                    op_id,
                ),
            )
            updated = dict(
                db.execute(
                    "SELECT * FROM assistant_host_exec_audit WHERE run_id=? AND operation_id=?",
                    (run_id, op_id),
                ).fetchone()
            )
        return self._format_row(updated)

    def audit_events(self, run_id: str, after_id: int = 0, limit: int = 20) -> list[dict[str, Any]]:
        self._require_enabled_and_unexpired(run_id)
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT * FROM assistant_host_exec_audit
                   WHERE run_id=? AND id>? ORDER BY id ASC LIMIT ?""",
                (run_id, max(0, int(after_id)), max(1, min(100, int(limit)))),
            ).fetchall()
        return [self._format_row(dict(r)) for r in rows]

    @staticmethod
    def _format_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "operation_id": row["operation_id"],
            "run_id": row["run_id"],
            "digest": row["digest"],
            "kind": row["kind"],
            "working_directory": row["working_directory"],
            "script_or_command": row["script_or_command"],
            "arguments": json.loads(row["arguments"] or "[]"),
            "state": row["state"],
            "process_identity": json.loads(row["process_identity"] or "{}"),
            "stdout": row["stdout"],
            "stderr": row["stderr"],
            "exit_code": row["exit_code"],
            "timing": {
                "created": row["created"],
                "started": row["started"],
                "completed": row["completed"],
                "elapsed_seconds": row["elapsed_seconds"],
            },
            "produced_artifacts": json.loads(row["produced_artifacts"] or "[]"),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "disposition": json.loads(row["disposition"] or "{}"),
            "expires_at": row["expires_at"],
            "duplicate_submission": False,
            "reexecuted": True,
        }
