"""Pydantic-validated Project Workspace & Sandboxed Tool Execution for Sabrina Console.

Provides strict path-jailed filesystem tools (`list_dir`, `read_file`,
`create_dir`, `write_file`, `edit_file`), bounded Python execution
(`run_python`, Docker-first via `tools.tool_sandbox.execute_candidate` with
fail-closed default and explicit opt-in `.venv` fallback), structured hunk diff
staging, hybrid Git + content-addressed file checkpoints with instant rollback,
closed-loop diagnostics (`run_diagnostics`), and managed background process
handles (`manage_process`).
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .protocol import PathIsolationError, guard_safe_path

try:
    from pydantic import BaseModel, Field, ValidationError  # type: ignore[import-not-found]
    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover - stdlib fallback
    BaseModel = object  # type: ignore[assignment,misc]
    ValidationError = ValueError  # type: ignore[assignment,misc]
    HAS_PYDANTIC = False

DEFAULT_SANDBOX_ROOT = Path(r"c:\Users\charl\OneDrive\Documents\Sabrina\workspaces")
MAX_WRITE_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 500
MAX_READ_LINES = 400
MAX_EXEC_OUTPUT = 64 * 1024
MAX_CHECKPOINT_FILES = 600
MAX_CHECKPOINT_FILE_BYTES = 512 * 1024
MAX_PROCESS_LOG_BYTES = 64 * 1024

READ_ONLY_TOOLS = frozenset({"list_dir", "read_file", "run_diagnostics"})
MUTATING_TOOLS = frozenset({"create_dir", "write_file", "edit_file", "run_python", "manage_process", "manage_dependencies"})
VALID_WORKSPACE_MODES = ("read_only", "approve_writes", "sandbox_auto")
VALID_POLICY_PROFILES = ("inspect_only", "sandboxed_edit", "full_harness")

POLICY_CAPABILITY_MATRIX: dict[str, dict[str, bool]] = {
    "inspect_only": {
        "read": True,
        "write": False,
        "exec": False,
        "process": False,
        "network": False,
    },
    "sandboxed_edit": {
        "read": True,
        "write": True,
        "exec": True,
        "process": False,
        "network": False,
    },
    "full_harness": {
        "read": True,
        "write": True,
        "exec": True,
        "process": True,
        "network": True,
    },
}

_SKIP_SNAPSHOT_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".sabrina_harness",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)


class WorkspaceSandboxError(RuntimeError):
    """Raised when a workspace tool violates path jail, schema, policy, or safety bounds."""


if HAS_PYDANTIC:
    class ListDirInput(BaseModel):
        path: str = Field(default=".", description="Relative directory path inside workspace.")
        max_depth: int = Field(default=2, ge=1, le=5, description="Max recursion depth (1..5).")

    class ReadFileInput(BaseModel):
        path: str = Field(..., min_length=1, description="Relative file path inside workspace.")
        start_line: int = Field(default=1, ge=1, description="1-based start line.")
        end_line: int = Field(default=200, ge=1, description="1-based inclusive end line.")

    class CreateDirInput(BaseModel):
        path: str = Field(..., min_length=1, description="Relative directory path to create.")

    class WriteFileInput(BaseModel):
        path: str = Field(..., min_length=1, description="Relative file path to create or overwrite.")
        content: str = Field(..., description="UTF-8 file content (max 256 KiB).")
        overwrite: bool = Field(default=False, description="Allow overwriting an existing file.")

    class EditFileInput(BaseModel):
        path: str = Field(..., min_length=1, description="Relative file path to edit.")
        target: str = Field(..., min_length=1, description="Exact text block to replace.")
        replacement: str = Field(..., description="Replacement text.")

    class RunPythonInput(BaseModel):
        code: str = Field(..., min_length=1, description="Python snippet to execute in sandbox.")
        timeout: int = Field(default=15, ge=1, le=60, description="Execution timeout in seconds (1..60).")

    class RunDiagnosticsInput(BaseModel):
        path: str = Field(default=".", description="Relative file or directory path to check.")
        checks: list[str] = Field(
            default_factory=lambda: ["syntax", "security"],
            description="Checks to run: syntax, security, ruff, pytest.",
        )
        timeout: int = Field(default=20, ge=1, le=60, description="Timeout in seconds (1..60).")

    class ManageProcessInput(BaseModel):
        action: str = Field(..., description="Lifecycle action: start, status, list, logs, stop.")
        process_id: str = Field(default="", description="Process ID for status/logs/stop.")
        command: list[str] = Field(default_factory=list, description="Command argv list for 'start'.")
        label: str = Field(default="", description="Human-readable label for the background process.")
        tail_lines: int = Field(default=60, ge=1, le=500, description="Number of log lines to read.")

    class ManageDependenciesInput(BaseModel):
        action: str = Field(..., description="Action: 'list', 'install', 'cache_info', or 'cache_wheel'.")
        packages: list[str] = Field(default_factory=list, description="Package specifications (e.g. ['requests>=2.28', 'pandas']).")
        wheelhouse_only: bool = Field(default=False, description="Install strictly from local artifact wheelhouse without remote index.")
else:  # pragma: no cover
    class _FallbackModel:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class ListDirInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(self, path: str = ".", max_depth: int = 2) -> None:
            super().__init__(path=str(path), max_depth=max(1, min(5, int(max_depth))))

    class ReadFileInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(self, path: str, start_line: int = 1, end_line: int = 200) -> None:
            if not str(path).strip():
                raise ValueError("path is required")
            super().__init__(path=str(path), start_line=max(1, int(start_line)), end_line=max(1, int(end_line)))

    class CreateDirInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(self, path: str) -> None:
            if not str(path).strip():
                raise ValueError("path is required")
            super().__init__(path=str(path))

    class WriteFileInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(self, path: str, content: str, overwrite: bool = False) -> None:
            if not str(path).strip():
                raise ValueError("path is required")
            super().__init__(path=str(path), content=str(content), overwrite=bool(overwrite))

    class EditFileInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(self, path: str, target: str, replacement: str) -> None:
            if not str(path).strip() or not str(target):
                raise ValueError("path and target are required")
            super().__init__(path=str(path), target=str(target), replacement=str(replacement))

    class RunPythonInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(self, code: str, timeout: int = 15) -> None:
            if not str(code).strip():
                raise ValueError("code is required")
            super().__init__(code=str(code), timeout=max(1, min(60, int(timeout))))

    class RunDiagnosticsInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(self, path: str = ".", checks: list[str] | None = None, timeout: int = 20) -> None:
            super().__init__(
                path=str(path or "."),
                checks=list(checks or ["syntax", "security"]),
                timeout=max(1, min(60, int(timeout))),
            )

    class ManageProcessInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(
            self,
            action: str,
            process_id: str = "",
            command: list[str] | None = None,
            label: str = "",
            tail_lines: int = 60,
        ) -> None:
            if not str(action).strip():
                raise ValueError("action is required")
            super().__init__(
                action=str(action).strip().lower(),
                process_id=str(process_id or ""),
                command=list(command or []),
                label=str(label or ""),
                tail_lines=max(1, min(500, int(tail_lines))),
            )

    class ManageDependenciesInput(_FallbackModel):  # type: ignore[no-redef]
        def __init__(
            self,
            action: str,
            packages: list[str] | None = None,
            wheelhouse_only: bool = False,
        ) -> None:
            if not str(action).strip():
                raise ValueError("action is required")
            super().__init__(
                action=str(action).strip().lower(),
                packages=list(packages or []),
                wheelhouse_only=bool(wheelhouse_only),
            )


TOOL_INPUT_MODELS: dict[str, type[Any]] = {
    "list_dir": ListDirInput,
    "read_file": ReadFileInput,
    "create_dir": CreateDirInput,
    "write_file": WriteFileInput,
    "edit_file": EditFileInput,
    "run_python": RunPythonInput,
    "run_diagnostics": RunDiagnosticsInput,
    "manage_process": ManageProcessInput,
    "manage_dependencies": ManageDependenciesInput,
}

TOOL_CAPABILITY_CATEGORY: dict[str, str] = {
    "list_dir": "read",
    "read_file": "read",
    "run_diagnostics": "read",
    "create_dir": "write",
    "write_file": "write",
    "edit_file": "write",
    "run_python": "exec",
    "manage_process": "process",
    "manage_dependencies": "exec",
}


def sanitize_project_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    if not cleaned:
        raise WorkspaceSandboxError("Project name must contain letters or digits.")
    return cleaned[:64]


def create_sandbox_project(
    name: str,
    base_root: str | Path = DEFAULT_SANDBOX_ROOT,
) -> Path:
    """Create an isolated sandbox project folder under `base_root/<name>`."""
    slug = sanitize_project_name(name)
    safe_base = guard_safe_path(base_root)
    target = guard_safe_path(safe_base / slug)
    target.mkdir(parents=True, exist_ok=True)
    return target


_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def parse_unified_diff_hunks(diff_text: str) -> list[dict[str, Any]]:
    """Parse a unified diff into individually addressable hunks with line coordinates."""
    lines = (diff_text or "").splitlines(keepends=True)
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk_idx = 0

    for line in lines:
        if line.startswith(("--- ", "+++ ")):
            continue
        m = _HUNK_HEADER_RE.match(line.rstrip("\r\n"))
        if m:
            if current is not None:
                current["patch"] = "".join(current.pop("_lines"))
                hunks.append(current)
            hunk_idx += 1
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
            section = (m.group(5) or "").strip()
            current = {
                "id": f"hunk_{hunk_idx}",
                "index": hunk_idx - 1,
                "header": line.rstrip("\r\n"),
                "section": section,
                "old_start": old_start,
                "old_lines": old_count,
                "new_start": new_start,
                "new_lines": new_count,
                "added": 0,
                "removed": 0,
                "_lines": [line],
            }
        elif current is not None:
            current["_lines"].append(line)
            if line.startswith("+") and not line.startswith("+++"):
                current["added"] += 1
            elif line.startswith("-") and not line.startswith("---"):
                current["removed"] += 1

    if current is not None:
        current["patch"] = "".join(current.pop("_lines"))
        hunks.append(current)
    return hunks


def apply_selected_hunks(
    old_text: str,
    new_text: str,
    accepted_hunk_ids: list[str] | set[str] | None = None,
) -> str:
    """Reconstruct file contents applying only the selected hunks (or all if None)."""
    if accepted_hunk_ids is None:
        return new_text
    accepted = {str(h).strip() for h in accepted_hunk_ids if str(h).strip()}
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    grouped = list(matcher.get_grouped_opcodes(n=3))
    if not grouped:
        return old_text

    out: list[str] = []
    cursor = 0
    for idx, group in enumerate(grouped):
        hunk_id = f"hunk_{idx + 1}"
        first_old = group[0][1]
        last_old = group[-1][2]
        if cursor < first_old:
            out.extend(old_lines[cursor:first_old])
        use_new = (hunk_id in accepted) or (str(idx) in accepted)
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out.extend(old_lines[i1:i2])
            elif use_new:
                out.extend(new_lines[j1:j2])
            else:
                out.extend(old_lines[i1:i2])
        cursor = last_old
    if cursor < len(old_lines):
        out.extend(old_lines[cursor:])
    return "".join(out)


def gemini_tool_declarations(include_extended: bool = True) -> list[dict[str, Any]]:
    """Return Gemini Interactions API function declarations for all workspace tools."""
    decls = [
        {
            "type": "function",
            "name": "list_dir",
            "description": "List files and subdirectories inside the active project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (default '.')."},
                    "max_depth": {"type": "integer", "description": "Max depth 1..5 (default 2)."},
                },
            },
        },
        {
            "type": "function",
            "name": "read_file",
            "description": "Read a UTF-8 text file inside the active project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path."},
                    "start_line": {"type": "integer", "description": "1-based start line (default 1)."},
                    "end_line": {"type": "integer", "description": "1-based inclusive end line (default 200)."},
                },
                "required": ["path"],
            },
        },
        {
            "type": "function",
            "name": "create_dir",
            "description": "Create a directory (including parent directories) inside the active project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path to create."},
                },
                "required": ["path"],
            },
        },
        {
            "type": "function",
            "name": "write_file",
            "description": "Create a new UTF-8 file (or overwrite when overwrite=true) inside the active project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path."},
                    "content": {"type": "string", "description": "Full UTF-8 file contents."},
                    "overwrite": {"type": "boolean", "description": "Set true to overwrite existing file."},
                },
                "required": ["path", "content"],
            },
        },
        {
            "type": "function",
            "name": "edit_file",
            "description": "Replace an exact text block inside an existing file in the active project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path."},
                    "target": {"type": "string", "description": "Exact existing text to replace."},
                    "replacement": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "target", "replacement"],
            },
        },
        {
            "type": "function",
            "name": "run_python",
            "description": "Run a bounded Python snippet inside the sandbox (Docker-first; fail-closed unless local venv fallback is enabled).",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (1..60, default 15)."},
                },
                "required": ["code"],
            },
        },
    ]
    if include_extended:
        decls.extend(
            [
                {
                    "type": "function",
                    "name": "run_diagnostics",
                    "description": "Run structured AST syntax, security, ruff lint, or pytest diagnostics on the workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file or directory path (default '.')."},
                            "checks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Diagnostic checks: syntax, security, web, ruff, pytest.",
                            },
                            "timeout": {"type": "integer", "description": "Timeout in seconds (1..60, default 20)."},
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "manage_process",
                    "description": "Manage background workspace subprocesses with isolated log redirection (start, status, list, logs, stop).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "description": "One of: start, status, list, logs, stop."},
                            "process_id": {"type": "string", "description": "Target process ID for status/logs/stop."},
                            "command": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Command argv list for action='start'.",
                            },
                            "label": {"type": "string", "description": "Optional process label."},
                            "tail_lines": {"type": "integer", "description": "Log lines to return (default 60)."},
                        },
                        "required": ["action"],
                    },
                },
                {
                    "type": "function",
                    "name": "manage_dependencies",
                    "description": "Manage workspace Python packages and artifact cache. Use to list installed packages, inspect cached wheels, or install packages to recover from ModuleNotFoundError or ImportError.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "description": "One of: list (list installed packages), install (install packages), cache_info (inspect artifact wheelhouse), cache_wheel (cache wheels locally).",
                            },
                            "packages": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Package specifications (e.g. ['requests>=2.28', 'pandas']).",
                            },
                            "wheelhouse_only": {
                                "type": "boolean",
                                "description": "Install only from local artifact wheelhouse (offline mode, default false).",
                            },
                        },
                        "required": ["action"],
                    },
                },
            ]
        )
    return decls


class WorkspaceSandbox:
    """Strictly path-jailed workspace tool executor with diff staging, hybrid checkpoints, diagnostics, and process handles."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        allow_local_venv_fallback: bool = False,
        python_executable: str | None = None,
        policy_profile: str = "sandboxed_edit",
    ) -> None:
        safe_root = guard_safe_path(root_dir)
        self.root = safe_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.allow_local_venv_fallback = bool(allow_local_venv_fallback)
        self.python_executable = python_executable or sys.executable
        if policy_profile not in VALID_POLICY_PROFILES:
            raise WorkspaceSandboxError(f"Invalid policy_profile '{policy_profile}'. Choose from {VALID_POLICY_PROFILES}.")
        self.policy_profile = policy_profile

        self._harness_dir = self.root / ".sabrina_harness"
        self._blobs_dir = self._harness_dir / "blobs"
        self._checkpoints_dir = self._harness_dir / "checkpoints"
        self._proc_logs_dir = self._harness_dir / "proc_logs"

        self._lock = threading.RLock()
        self._staged_diffs: dict[str, dict[str, Any]] = {}
        self._checkpoints: list[dict[str, Any]] = []
        self._processes: dict[str, dict[str, Any]] = {}
        self._load_existing_checkpoints()

    def _ensure_harness_dirs(self) -> None:
        self._blobs_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self._proc_logs_dir.mkdir(parents=True, exist_ok=True)

    def _load_existing_checkpoints(self) -> None:
        if not self._checkpoints_dir.is_dir():
            return
        loaded: list[dict[str, Any]] = []
        for mf in sorted(self._checkpoints_dir.glob("*.json")):
            try:
                data = json.loads(mf.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("id"):
                    loaded.append(data)
            except Exception:
                continue
        loaded.sort(key=lambda c: float(c.get("created_at") or 0.0), reverse=True)
        self._checkpoints = loaded[:50]

    def set_policy_profile(self, profile: str) -> dict[str, Any]:
        if profile not in VALID_POLICY_PROFILES:
            raise WorkspaceSandboxError(f"Invalid policy_profile '{profile}'. Choose from {VALID_POLICY_PROFILES}.")
        self.policy_profile = profile
        return {
            "ok": True,
            "policy_profile": self.policy_profile,
            "capabilities": dict(POLICY_CAPABILITY_MATRIX[self.policy_profile]),
        }

    def check_policy(self, tool_name: str) -> tuple[bool, str]:
        """Verify whether `tool_name` is permitted under the active `policy_profile`."""
        category = TOOL_CAPABILITY_CATEGORY.get(tool_name, "exec")
        caps = POLICY_CAPABILITY_MATRIX.get(self.policy_profile, POLICY_CAPABILITY_MATRIX["inspect_only"])
        if not caps.get(category, False):
            return (
                False,
                f"Policy gate blocked '{tool_name}' (requires '{category}' capability; active profile is '{self.policy_profile}').",
            )
        return True, ""

    def resolve_path(self, rel_path: str) -> Path:
        """Resolve `rel_path` strictly inside `self.root`, blocking escapes and holdout paths."""
        raw = str(rel_path or ".").strip()
        if not raw:
            raw = "."
        candidate_input = Path(raw)
        if candidate_input.is_absolute():
            raise WorkspaceSandboxError(f"Absolute paths are forbidden; use a path relative to workspace: {raw}")
        resolved = (self.root / candidate_input).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceSandboxError(f"Path escapes active workspace root: {raw}") from exc
        try:
            guard_safe_path(resolved)
        except PathIsolationError as exc:
            raise WorkspaceSandboxError(str(exc)) from exc
        return resolved

    def validate_args(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        model_cls = TOOL_INPUT_MODELS.get(tool_name)
        if model_cls is None:
            raise WorkspaceSandboxError(f"Unknown workspace tool: {tool_name}")
        if not isinstance(arguments, dict):
            raise WorkspaceSandboxError(f"Tool arguments for {tool_name} must be an object.")
        try:
            return model_cls(**arguments)
        except (ValidationError, ValueError, TypeError) as exc:
            raise WorkspaceSandboxError(f"Invalid arguments for {tool_name}: {exc}") from exc

    # ------------------------------------------------------------------
    # Diff-First Patching & Hunk Review
    # ------------------------------------------------------------------

    def preview_action(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Generate a human-readable diff and structured hunk list for a mutating tool before execution."""
        parsed = self.validate_args(tool_name, arguments)
        if tool_name == "create_dir":
            target = self.resolve_path(parsed.path)
            rel = target.relative_to(self.root).as_posix()
            return {
                "tool": tool_name,
                "path": rel,
                "diff": f"+ mkdir {rel}",
                "hunks": [],
                "old_text": "",
                "new_text": "",
                "existed": target.exists(),
            }
        if tool_name == "write_file":
            target = self.resolve_path(parsed.path)
            rel = target.relative_to(self.root).as_posix()
            existed = target.is_file()
            if existed and not parsed.overwrite:
                raise WorkspaceSandboxError(f"File already exists (pass overwrite=true): {parsed.path}")
            old_text = target.read_text(encoding="utf-8", errors="replace") if existed else ""
            new_text = parsed.content
            diff = "".join(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            hunks = parse_unified_diff_hunks(diff)
            return {
                "tool": tool_name,
                "path": rel,
                "diff": diff or "(empty file)",
                "hunks": hunks,
                "old_text": old_text,
                "new_text": new_text,
                "existed": existed,
            }
        if tool_name == "edit_file":
            target = self.resolve_path(parsed.path)
            if not target.is_file():
                raise WorkspaceSandboxError(f"File not found: {parsed.path}")
            rel = target.relative_to(self.root).as_posix()
            old_text = target.read_text(encoding="utf-8")
            if parsed.target not in old_text:
                raise WorkspaceSandboxError(f"Target block not found in {parsed.path}")
            new_text = old_text.replace(parsed.target, parsed.replacement, 1)
            diff = "".join(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            hunks = parse_unified_diff_hunks(diff)
            return {
                "tool": tool_name,
                "path": rel,
                "diff": diff,
                "hunks": hunks,
                "old_text": old_text,
                "new_text": new_text,
                "existed": True,
            }
        if tool_name == "run_python":
            return {"tool": tool_name, "path": ".", "diff": parsed.code[:2000], "hunks": []}
        if tool_name == "manage_process":
            cmd_str = " ".join(parsed.command) if parsed.command else parsed.action
            return {"tool": tool_name, "path": ".", "diff": f"process {parsed.action}: {cmd_str}", "hunks": []}
        if tool_name == "manage_dependencies":
            act = getattr(parsed, "action", "")
            pkgs = getattr(parsed, "packages", [])
            diff = f"Dependency operation: action={act!r}, packages={pkgs!r}"
            return {"tool": tool_name, "path": ".", "diff": diff, "hunks": []}
        return {"tool": tool_name, "path": ".", "diff": "", "hunks": []}

    def stage_diff(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        author: str = "workspace_agent",
        note: str = "",
    ) -> dict[str, Any]:
        """Stage a file mutation or tool proposal without mutating the target file yet."""
        preview = self.preview_action(tool_name, arguments)
        diff_id = f"diff_{uuid.uuid4().hex[:8]}"
        entry = {
            "id": diff_id,
            "tool": tool_name,
            "arguments": dict(arguments),
            "path": preview.get("path", "."),
            "diff": preview.get("diff", ""),
            "hunks": preview.get("hunks", []),
            "old_text": preview.get("old_text", ""),
            "new_text": preview.get("new_text", ""),
            "existed": bool(preview.get("existed", False)),
            "author": author,
            "note": note,
            "status": "staged",
            "created_at": time.time(),
        }
        with self._lock:
            self._staged_diffs[diff_id] = entry
        return self._public_diff_view(entry)

    @staticmethod
    def _public_diff_view(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": entry["id"],
            "tool": entry["tool"],
            "arguments": entry["arguments"],
            "path": entry["path"],
            "diff": entry["diff"],
            "hunks": entry["hunks"],
            "author": entry.get("author", ""),
            "note": entry.get("note", ""),
            "status": entry.get("status", "staged"),
            "created_at": entry.get("created_at", 0.0),
        }

    def list_staged_diffs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._public_diff_view(d)
                for d in sorted(self._staged_diffs.values(), key=lambda item: float(item.get("created_at") or 0.0))
                if d.get("status") == "staged"
            ]

    def apply_staged_diff(
        self,
        diff_id: str,
        *,
        accepted_hunk_ids: list[str] | None = None,
        create_checkpoint: bool = True,
    ) -> dict[str, Any]:
        """Apply a staged diff (optionally filtering by accepted hunk IDs) with an automatic pre-apply checkpoint."""
        with self._lock:
            entry = self._staged_diffs.get(diff_id)
            if entry is None:
                raise WorkspaceSandboxError(f"Staged diff '{diff_id}' not found.")
            if entry.get("status") != "staged":
                raise WorkspaceSandboxError(f"Staged diff '{diff_id}' is already {entry.get('status')}.")

        tool_name = entry["tool"]
        allowed, reason = self.check_policy(tool_name)
        if not allowed:
            raise WorkspaceSandboxError(reason)

        checkpoint_meta: dict[str, Any] | None = None
        if create_checkpoint and tool_name in {"create_dir", "write_file", "edit_file"}:
            checkpoint_meta = self.create_checkpoint(label=f"pre-{tool_name}:{entry['path']}")

        if tool_name in {"write_file", "edit_file"} and accepted_hunk_ids is not None:
            final_text = apply_selected_hunks(
                entry.get("old_text", ""),
                entry.get("new_text", ""),
                accepted_hunk_ids=accepted_hunk_ids,
            )
            encoded = final_text.encode("utf-8")
            if len(encoded) > MAX_WRITE_BYTES:
                raise WorkspaceSandboxError(f"File content exceeds {MAX_WRITE_BYTES} bytes limit.")
            target = self.resolve_path(entry["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(encoded)
            applied_diff = "".join(
                difflib.unified_diff(
                    entry.get("old_text", "").splitlines(keepends=True),
                    final_text.splitlines(keepends=True),
                    fromfile=f"a/{entry['path']}",
                    tofile=f"b/{entry['path']}",
                )
            )
            res = {
                "ok": True,
                "path": entry["path"],
                "bytes": len(encoded),
                "diff": applied_diff,
                "accepted_hunk_ids": list(accepted_hunk_ids),
            }
        else:
            args = dict(entry["arguments"])
            if tool_name == "write_file":
                args["overwrite"] = True
            res = self.execute_tool(tool_name, args)

        with self._lock:
            entry["status"] = "applied"
            entry["applied_at"] = time.time()

        return {
            "ok": bool(res.get("ok", True)),
            "diff_id": diff_id,
            "result": res,
            "checkpoint": checkpoint_meta,
        }

    def discard_staged_diff(self, diff_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._staged_diffs.get(diff_id)
            if entry is None:
                raise WorkspaceSandboxError(f"Staged diff '{diff_id}' not found.")
            entry["status"] = "discarded"
            entry["discarded_at"] = time.time()
        return {"ok": True, "diff_id": diff_id, "status": "discarded"}

    # ------------------------------------------------------------------
    # Hybrid Checkpoints (Git Tree + Content-Addressed File Snapshots)
    # ------------------------------------------------------------------

    def _detect_git_head(self) -> str | None:
        git_dir = self.root / ".git"
        if not git_dir.exists():
            return None
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=3,
            )
            if proc.returncode == 0:
                return (proc.stdout or "").strip() or None
        except Exception:
            pass
        return None

    def create_checkpoint(self, label: str = "turn", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Create a hybrid checkpoint capturing git HEAD (if inside a git repo) plus content-addressed file blobs."""
        self._ensure_harness_dirs()
        cp_id = f"cp_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        git_head = self._detect_git_head()
        manifest_files: dict[str, dict[str, Any]] = {}
        total_bytes = 0

        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in _SKIP_SNAPSHOT_DIRS and not d.startswith(".sabrina_")
            )
            for fname in sorted(filenames):
                if len(manifest_files) >= MAX_CHECKPOINT_FILES:
                    break
                fpath = Path(dirpath) / fname
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                try:
                    guard_safe_path(fpath)
                    rel = fpath.relative_to(self.root).as_posix()
                    st = fpath.stat()
                except Exception:
                    continue
                if st.st_size > MAX_CHECKPOINT_FILE_BYTES:
                    continue
                try:
                    raw = fpath.read_bytes()
                except OSError:
                    continue
                sha = hashlib.sha256(raw).hexdigest()
                blob_path = self._blobs_dir / sha
                if not blob_path.exists():
                    blob_path.write_bytes(raw)
                manifest_files[rel] = {
                    "sha256": sha,
                    "size": len(raw),
                }
                total_bytes += len(raw)
            if len(manifest_files) >= MAX_CHECKPOINT_FILES:
                break

        record = {
            "id": cp_id,
            "label": str(label or "checkpoint")[:120],
            "backend": "hybrid_git_cas" if git_head else "cas_manifest",
            "git_head": git_head,
            "file_count": len(manifest_files),
            "total_bytes": total_bytes,
            "created_at": time.time(),
            "metadata": dict(metadata or {}),
            "files": manifest_files,
        }
        cp_file = self._checkpoints_dir / f"{cp_id}.json"
        cp_file.write_text(json.dumps(record, indent=2), encoding="utf-8")

        summary = {k: v for k, v in record.items() if k != "files"}
        with self._lock:
            self._checkpoints.insert(0, record)
            if len(self._checkpoints) > 50:
                self._checkpoints = self._checkpoints[:50]
        return summary

    def list_checkpoints(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{k: v for k, v in cp.items() if k != "files"} for cp in self._checkpoints]

    def rollback_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        """Restore workspace files to the exact state captured in `checkpoint_id`."""
        target_id = str(checkpoint_id or "").strip()
        if not target_id:
            raise WorkspaceSandboxError("checkpoint_id is required.")

        record: dict[str, Any] | None = None
        with self._lock:
            for cp in self._checkpoints:
                if cp.get("id") == target_id:
                    record = cp
                    break
        if record is None:
            cp_file = self._checkpoints_dir / f"{target_id}.json"
            if cp_file.is_file():
                record = json.loads(cp_file.read_text(encoding="utf-8"))
        if record is None:
            raise WorkspaceSandboxError(f"Checkpoint '{target_id}' not found.")

        files_map: dict[str, dict[str, Any]] = record.get("files") or {}
        restored: list[str] = []
        removed: list[str] = []

        # 1. Remove files created since the checkpoint
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in _SKIP_SNAPSHOT_DIRS and not d.startswith(".sabrina_")
            )
            for fname in sorted(filenames):
                fpath = Path(dirpath) / fname
                if fpath.is_symlink() or not fpath.is_file():
                    continue
                try:
                    guard_safe_path(fpath)
                    rel = fpath.relative_to(self.root).as_posix()
                except Exception:
                    continue
                if rel not in files_map:
                    try:
                        fpath.unlink()
                        removed.append(rel)
                    except OSError:
                        pass

        # 2. Restore all snapshot files from content-addressed blobs
        for rel, meta in files_map.items():
            sha = str(meta.get("sha256") or "")
            blob_path = self._blobs_dir / sha
            if not blob_path.is_file():
                raise WorkspaceSandboxError(f"Missing blob {sha} for {rel} in checkpoint {target_id}.")
            dest = self.resolve_path(rel)
            raw = blob_path.read_bytes()
            current_sha = hashlib.sha256(dest.read_bytes()).hexdigest() if dest.is_file() else ""
            if current_sha != sha:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                restored.append(rel)

        return {
            "ok": True,
            "checkpoint_id": target_id,
            "label": record.get("label", ""),
            "backend": record.get("backend", "cas_manifest"),
            "restored_files": restored,
            "removed_files": removed,
        }

    # ------------------------------------------------------------------
    # Closed-Loop Diagnostics (AST Syntax, Security, Ruff, Pytest)
    # ------------------------------------------------------------------

    def _run_diagnostics(self, args: RunDiagnosticsInput) -> dict[str, Any]:
        target = self.resolve_path(args.path)
        if not target.exists():
            raise WorkspaceSandboxError(f"Diagnostic target not found: {args.path}")

        requested = [str(c).strip().lower() for c in (args.checks or ["syntax", "security"]) if str(c).strip()]
        web_exts = {".html", ".htm", ".css", ".js", ".svg"}
        py_files: list[Path] = []
        web_files: list[Path] = []
        if target.is_file():
            suf = target.suffix.lower()
            if suf == ".py":
                py_files.append(target)
            elif suf in web_exts:
                web_files.append(target)
        else:
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_SNAPSHOT_DIRS)
                for fn in sorted(filenames):
                    fp = Path(dirpath) / fn
                    suf = fp.suffix.lower()
                    if suf == ".py" and len(py_files) < 120:
                        try:
                            guard_safe_path(fp)
                            py_files.append(fp)
                        except PathIsolationError:
                            continue
                    elif "web" in requested and suf in web_exts and len(web_files) < 120:
                        try:
                            guard_safe_path(fp)
                            web_files.append(fp)
                        except PathIsolationError:
                            continue

        items: list[dict[str, Any]] = []
        checks_run: list[str] = []

        if "syntax" in requested:
            checks_run.append("syntax")
            for fp in py_files:
                rel = fp.relative_to(self.root).as_posix()
                try:
                    src = fp.read_text(encoding="utf-8", errors="replace")
                    ast.parse(src, filename=rel)
                except SyntaxError as exc:
                    items.append(
                        {
                            "source": "syntax",
                            "severity": "error",
                            "path": rel,
                            "line": int(exc.lineno or 1),
                            "column": int(exc.offset or 1),
                            "code": "E999",
                            "message": f"SyntaxError: {exc.msg}",
                        }
                    )

        if "security" in requested:
            checks_run.append("security")
            banned_patterns = [
                (re.compile(r"\beval\s*\("), "SEC001", "Avoid raw eval() execution."),
                (re.compile(r"\bsubprocess\.(?:Popen|run|call)\s*\([^)]*shell\s*=\s*True"), "SEC002", "Avoid shell=True in subprocess calls."),
                (re.compile(r"execution_authorized\s*=\s*True"), "SEC003", "Safety invariant violation: execution_authorized must remain False."),
            ]
            for fp in py_files:
                rel = fp.relative_to(self.root).as_posix()
                try:
                    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for idx, line in enumerate(lines, start=1):
                    for pat, code, msg in banned_patterns:
                        if pat.search(line):
                            items.append(
                                {
                                    "source": "security",
                                    "severity": "warning" if code != "SEC003" else "error",
                                    "path": rel,
                                    "line": idx,
                                    "column": 1,
                                    "code": code,
                                    "message": msg,
                                }
                            )

        if "ruff" in requested and shutil.which("ruff"):
            checks_run.append("ruff")
            try:
                proc = subprocess.run(
                    ["ruff", "check", "--output-format=json", str(target)],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=int(args.timeout),
                )
                if proc.stdout:
                    parsed_ruff = json.loads(proc.stdout)
                    if isinstance(parsed_ruff, list):
                        for entry in parsed_ruff[:50]:
                            fname = str(entry.get("filename") or "")
                            try:
                                rel = Path(fname).resolve().relative_to(self.root).as_posix()
                            except Exception:
                                rel = fname
                            loc = entry.get("location") or {}
                            items.append(
                                {
                                    "source": "ruff",
                                    "severity": "warning",
                                    "path": rel,
                                    "line": int(loc.get("row") or 1),
                                    "column": int(loc.get("column") or 1),
                                    "code": str(entry.get("code") or "RUFF"),
                                    "message": str(entry.get("message") or ""),
                                }
                            )
            except Exception:
                pass

        if "pytest" in requested and self.allow_local_venv_fallback:
            checks_run.append("pytest")
            try:
                proc = subprocess.run(
                    [self.python_executable, "-m", "pytest", "-q", "--tb=short", str(target)],
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=int(args.timeout),
                )
                if proc.returncode != 0:
                    out_lines = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()
                    summary_line = out_lines[-1] if out_lines else f"pytest exited with code {proc.returncode}"
                    items.append(
                        {
                            "source": "pytest",
                            "severity": "error",
                            "path": args.path,
                            "line": 1,
                            "column": 1,
                            "code": "PYTEST_FAIL",
                            "message": summary_line[:300],
                            "output": "\n".join(out_lines[-25:])[:2000],
                        }
                    )
            except subprocess.TimeoutExpired:
                items.append(
                    {
                        "source": "pytest",
                        "severity": "error",
                        "path": args.path,
                        "line": 1,
                        "column": 1,
                        "code": "PYTEST_TIMEOUT",
                        "message": f"pytest timed out after {args.timeout}s",
                    }
                )
            except Exception as exc:
                items.append(
                    {
                        "source": "pytest",
                        "severity": "error",
                        "path": args.path,
                        "line": 1,
                        "column": 1,
                        "code": "PYTEST_ERR",
                        "message": str(exc),
                    }
                )

        if "web" in requested:
            import html.parser as _html_parser
            import xml.etree.ElementTree as _ET

            checks_run.append("web")

            def _has_valid_raster_magic(img_path: Path, raw_bytes: bytes) -> bool:
                ext = img_path.suffix.lower()
                if ext == ".png":
                    return raw_bytes.startswith(b"\x89PNG\r\n\x1a\n")
                if ext in {".jpg", ".jpeg"}:
                    return raw_bytes.startswith(b"\xff\xd8\xff")
                if ext == ".gif":
                    return raw_bytes.startswith((b"GIF87a", b"GIF89a"))
                if ext == ".webp":
                    return len(raw_bytes) >= 12 and raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP"
                return True

            class _AssetRefParser(_html_parser.HTMLParser):
                def __init__(self) -> None:
                    super().__init__()
                    self.refs: list[tuple[str, str, str, int, int]] = []

                def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                    line_no, col_no = self.getpos()
                    attr_map = {k.lower(): (v or "") for k, v in attrs if k}
                    t = tag.lower()
                    if t in {"img", "script", "source", "video", "audio", "iframe"}:
                        if "src" in attr_map:
                            self.refs.append((t, "src", attr_map["src"], line_no, col_no + 1))
                        if t == "video" and "poster" in attr_map:
                            self.refs.append((t, "poster", attr_map["poster"], line_no, col_no + 1))
                    elif t == "link":
                        rel_val = attr_map.get("rel", "").lower()
                        if any(r in rel_val for r in ("stylesheet", "icon", "preload")) and "href" in attr_map:
                            self.refs.append((t, "href", attr_map["href"], line_no, col_no + 1))

            skip_prefixes = ("http://", "https://", "data:", "blob:", "mailto:", "tel:", "javascript:", "#", "//")
            for fp in web_files:
                rel = fp.relative_to(self.root).as_posix()
                suf = fp.suffix.lower()
                try:
                    if fp.stat().st_size == 0:
                        items.append(
                            {
                                "source": "web",
                                "severity": "error",
                                "path": rel,
                                "line": 1,
                                "column": 1,
                                "code": "WEB000",
                                "message": f"Empty zero-byte web deliverable file: {rel}",
                            }
                        )
                        continue
                    src_text = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                if suf in {".html", ".htm"}:
                    parser = _AssetRefParser()
                    try:
                        parser.feed(src_text)
                    except Exception as exc:
                        items.append(
                            {
                                "source": "web",
                                "severity": "error",
                                "path": rel,
                                "line": 1,
                                "column": 1,
                                "code": "WEB001",
                                "message": f"HTML parse error: {exc}",
                            }
                        )
                        continue
                    for tag_name, attr_name, raw_ref, ln_no, col_no in parser.refs:
                        ref_clean = raw_ref.strip()
                        if not ref_clean:
                            items.append(
                                {
                                    "source": "web",
                                    "severity": "error",
                                    "path": rel,
                                    "line": ln_no,
                                    "column": col_no,
                                    "code": "WEB002",
                                    "message": f"Empty <{tag_name} {attr_name}=''> attribute in {rel}",
                                }
                            )
                            continue
                        if ref_clean.lower().startswith(skip_prefixes):
                            continue
                        local_part = ref_clean.split("#", 1)[0].split("?", 1)[0].strip()
                        if not local_part:
                            continue
                        base_dir = self.root if local_part.startswith("/") else fp.parent
                        cand_asset = (base_dir / local_part.lstrip("/\\")).resolve()
                        try:
                            cand_asset.relative_to(self.root)
                            guard_safe_path(cand_asset)
                        except Exception:
                            items.append(
                                {
                                    "source": "web",
                                    "severity": "error",
                                    "path": rel,
                                    "line": ln_no,
                                    "column": col_no,
                                    "code": "WEB001",
                                    "message": f"Local asset path escapes workspace in <{tag_name} {attr_name}='{raw_ref}'>",
                                }
                            )
                            continue
                        if not cand_asset.is_file():
                            items.append(
                                {
                                    "source": "web",
                                    "severity": "error",
                                    "path": rel,
                                    "line": ln_no,
                                    "column": col_no,
                                    "code": "WEB002",
                                    "message": f"Missing local asset referenced in <{tag_name} {attr_name}='{raw_ref}'>",
                                }
                            )
                            continue
                        size_b = cand_asset.stat().st_size
                        if size_b == 0:
                            items.append(
                                {
                                    "source": "web",
                                    "severity": "error",
                                    "path": rel,
                                    "line": ln_no,
                                    "column": col_no,
                                    "code": "WEB003",
                                    "message": f"Zero-byte empty asset referenced in <{tag_name} {attr_name}='{raw_ref}'>",
                                }
                            )
                            continue
                        if cand_asset.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                            head_bytes = cand_asset.read_bytes()[:16]
                            if not _has_valid_raster_magic(cand_asset, head_bytes):
                                items.append(
                                    {
                                        "source": "web",
                                        "severity": "error",
                                        "path": rel,
                                        "line": ln_no,
                                        "column": col_no,
                                        "code": "WEB004",
                                        "message": f"Corrupt binary image header for '{raw_ref}' (not valid {cand_asset.suffix.lower()} bytes; use tool:generate_image or .svg)",
                                    }
                                )
                            elif cand_asset.suffix.lower() == ".svg":
                                pass
                        elif cand_asset.suffix.lower() == ".svg":
                            try:
                                _ET.fromstring(cand_asset.read_text(encoding="utf-8", errors="replace"))
                            except _ET.ParseError as exc:
                                items.append(
                                    {
                                        "source": "web",
                                        "severity": "error",
                                        "path": rel,
                                        "line": ln_no,
                                        "column": col_no,
                                        "code": "WEB005",
                                        "message": f"Invalid SVG XML in referenced '{raw_ref}': {exc}",
                                    }
                                )
                elif suf == ".svg":
                    try:
                        _ET.fromstring(src_text)
                    except _ET.ParseError as exc:
                        items.append(
                            {
                                "source": "web",
                                "severity": "error",
                                "path": rel,
                                "line": 1,
                                "column": 1,
                                "code": "WEB005",
                                "message": f"Invalid SVG XML in {rel}: {exc}",
                            }
                        )
                elif suf == ".css":
                    stripped_css = re.sub(r"/\*.*?\*/", "", src_text, flags=re.S)
                    stripped_css = re.sub(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', '""', stripped_css)
                    if stripped_css.count("{") != stripped_css.count("}"):
                        items.append(
                            {
                                "source": "web",
                                "severity": "error",
                                "path": rel,
                                "line": 1,
                                "column": 1,
                                "code": "WEB006",
                                "message": f"Unbalanced CSS '{{' and '}}' braces in {rel}",
                            }
                        )

        error_count = sum(1 for item in items if item.get("severity") == "error")
        warning_count = sum(1 for item in items if item.get("severity") == "warning")
        checked_total = len(py_files) + (len(web_files) if "web" in checks_run else 0)
        return {
            "ok": True,
            "passed": error_count == 0,
            "checks_run": checks_run,
            "files_checked": checked_total,
            "error_count": error_count,
            "warning_count": warning_count,
            "diagnostics": items,
        }

    # ------------------------------------------------------------------
    # Managed Background Process / PTY Lifecycle
    # ------------------------------------------------------------------

    def _manage_process(self, args: ManageProcessInput) -> dict[str, Any]:
        action = args.action.strip().lower()
        if action == "list":
            return {"ok": True, "processes": self.list_processes()}

        if action == "start":
            if not self.allow_local_venv_fallback:
                raise WorkspaceSandboxError(
                    "Background process execution requires 'allow_local_venv_fallback=True' and 'full_harness' policy."
                )
            if not args.command:
                raise WorkspaceSandboxError("command list is required for manage_process action='start'.")
            self._ensure_harness_dirs()
            proc_id = f"proc_{uuid.uuid4().hex[:8]}"
            log_file = self._proc_logs_dir / f"{proc_id}.log"
            fh = log_file.open("w", encoding="utf-8")
            try:
                proc = subprocess.Popen(
                    args.command,
                    cwd=str(self.root),
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception as exc:
                fh.close()
                raise WorkspaceSandboxError(f"Failed to start background process: {exc}") from exc

            record = {
                "id": proc_id,
                "label": args.label or " ".join(args.command)[:60],
                "command": list(args.command),
                "pid": proc.pid,
                "started_at": time.time(),
                "log_path": str(log_file),
                "_proc": proc,
                "_fh": fh,
            }
            with self._lock:
                self._processes[proc_id] = record
            return {"ok": True, "process": self._process_status_dict(record)}

        proc_id = args.process_id.strip()
        if not proc_id:
            raise WorkspaceSandboxError(f"process_id is required for action='{action}'.")
        with self._lock:
            record = self._processes.get(proc_id)
        if record is None:
            raise WorkspaceSandboxError(f"Managed process '{proc_id}' not found.")

        if action == "status":
            return {"ok": True, "process": self._process_status_dict(record)}

        if action == "logs":
            log_path = Path(record["log_path"])
            lines: list[str] = []
            if log_path.is_file():
                raw = log_path.read_text(encoding="utf-8", errors="replace")[-MAX_PROCESS_LOG_BYTES:]
                lines = raw.splitlines()[-args.tail_lines:]
            return {
                "ok": True,
                "process": self._process_status_dict(record),
                "lines": lines,
            }

        if action == "stop":
            proc: subprocess.Popen[str] = record["_proc"]
            fh = record.get("_fh")
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if fh is not None and not fh.closed:
                try:
                    fh.close()
                except Exception:
                    pass
            return {"ok": True, "process": self._process_status_dict(record), "stopped": True}

        raise WorkspaceSandboxError(f"Unsupported manage_process action: {action}")

    def _process_status_dict(self, record: dict[str, Any]) -> dict[str, Any]:
        proc: subprocess.Popen[str] = record["_proc"]
        rc = proc.poll()
        fh = record.get("_fh")
        if rc is not None and fh is not None and not fh.closed:
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass
        return {
            "id": record["id"],
            "label": record["label"],
            "command": record["command"],
            "pid": record["pid"],
            "running": rc is None,
            "exit_code": rc,
            "started_at": record["started_at"],
            "log_path": record["log_path"],
        }

    def list_processes(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._process_status_dict(r) for r in self._processes.values()]

    def shutdown_processes(self) -> None:
        with self._lock:
            ids = list(self._processes.keys())
        for pid in ids:
            try:
                self._manage_process(ManageProcessInput(action="stop", process_id=pid))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Tool Execution Dispatch
    # ------------------------------------------------------------------

    def execute_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate policy & Pydantic schema, then execute the requested workspace tool."""
        parsed = self.validate_args(tool_name, arguments)
        allowed, reason = self.check_policy(tool_name)
        if not allowed:
            raise WorkspaceSandboxError(reason)

        if tool_name == "list_dir":
            return self._list_dir(parsed)
        if tool_name == "read_file":
            return self._read_file(parsed)
        if tool_name == "create_dir":
            return self._create_dir(parsed)
        if tool_name == "write_file":
            return self._write_file(parsed)
        if tool_name == "edit_file":
            return self._edit_file(parsed)
        if tool_name == "run_python":
            return self._run_python(parsed)
        if tool_name == "run_diagnostics":
            return self._run_diagnostics(parsed)
        if tool_name == "manage_process":
            return self._manage_process(parsed)
        if tool_name == "manage_dependencies":
            return self._manage_dependencies(parsed)
        raise WorkspaceSandboxError(f"Unsupported tool: {tool_name}")

    def _list_dir(self, args: ListDirInput) -> dict[str, Any]:
        base = self.resolve_path(args.path)
        if not base.exists() or not base.is_dir():
            raise WorkspaceSandboxError(f"Directory not found: {args.path}")
        entries: list[dict[str, Any]] = []
        skip_dirs = {".git", ".venv", "__pycache__", "node_modules", ".sabrina_harness"}

        def walk(curr: Path, depth: int) -> None:
            if depth > args.max_depth or len(entries) >= MAX_LIST_ENTRIES:
                return
            try:
                children = sorted(curr.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except OSError:
                return
            for child in children:
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                if child.name in skip_dirs:
                    continue
                if child.is_symlink():
                    try:
                        child.resolve().relative_to(self.root)
                    except ValueError:
                        continue
                rel = child.relative_to(self.root).as_posix()
                entries.append({
                    "path": rel,
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                })
                if child.is_dir():
                    walk(child, depth + 1)

        walk(base, 1)
        return {"ok": True, "root": str(self.root), "entries": entries, "truncated": len(entries) >= MAX_LIST_ENTRIES}

    def _read_file(self, args: ReadFileInput) -> dict[str, Any]:
        target = self.resolve_path(args.path)
        if not target.is_file():
            raise WorkspaceSandboxError(f"File not found: {args.path}")
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, args.start_line)
        end = min(len(lines), max(start, min(args.end_line, start + MAX_READ_LINES - 1)))
        snippet = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1)) if lines else ""
        return {
            "ok": True,
            "path": target.relative_to(self.root).as_posix(),
            "total_lines": len(lines),
            "start_line": start,
            "end_line": end,
            "content": snippet,
        }

    def _create_dir(self, args: CreateDirInput) -> dict[str, Any]:
        target = self.resolve_path(args.path)
        target.mkdir(parents=True, exist_ok=True)
        rel = target.relative_to(self.root).as_posix()
        return {"ok": True, "path": rel, "message": f"Created directory {rel}"}

    def _write_file(self, args: WriteFileInput) -> dict[str, Any]:
        encoded = args.content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise WorkspaceSandboxError(f"File content exceeds {MAX_WRITE_BYTES} bytes limit.")
        target = self.resolve_path(args.path)
        if target.exists() and not args.overwrite:
            raise WorkspaceSandboxError(f"File already exists (pass overwrite=true): {args.path}")
        old_lines = target.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True) if target.is_file() else []
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)
        rel = target.relative_to(self.root).as_posix()
        diff = "".join(
            difflib.unified_diff(
                old_lines,
                args.content.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        return {
            "ok": True,
            "path": rel,
            "bytes": len(encoded),
            "diff": diff,
            "hunks": parse_unified_diff_hunks(diff),
        }

    def _edit_file(self, args: EditFileInput) -> dict[str, Any]:
        target = self.resolve_path(args.path)
        if not target.is_file():
            raise WorkspaceSandboxError(f"File not found: {args.path}")
        old_text = target.read_text(encoding="utf-8")
        if args.target not in old_text:
            raise WorkspaceSandboxError(f"Target block not found in {args.path}")
        new_text = old_text.replace(args.target, args.replacement, 1)
        encoded = new_text.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise WorkspaceSandboxError(f"Edited file exceeds {MAX_WRITE_BYTES} bytes limit.")
        target.write_bytes(encoded)
        rel = target.relative_to(self.root).as_posix()
        diff = "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
            )
        )
        return {
            "ok": True,
            "path": rel,
            "bytes": len(encoded),
            "diff": diff,
            "hunks": parse_unified_diff_hunks(diff),
        }

    def _run_python(self, args: RunPythonInput) -> dict[str, Any]:
        # 1. Try Docker-isolated sandbox (`tools.tool_sandbox.execute_candidate`) first
        try:
            from tools.tool_sandbox import execute_candidate  # type: ignore[import-not-found]
            res = execute_candidate(args.code, timeout=int(args.timeout))
            if res.get("status") != "unavailable":
                return {
                    "ok": res.get("exit_code") == 0,
                    "isolated": True,
                    "backend": "docker",
                    "exit_code": res.get("exit_code"),
                    "stdout": res.get("stdout", ""),
                    "stderr": res.get("stderr", ""),
                    "status": res.get("status"),
                }
        except Exception:
            pass

        # 2. Fail closed unless user explicitly opted into bounded local .venv fallback
        if not self.allow_local_venv_fallback:
            return {
                "ok": False,
                "isolated": False,
                "backend": "none",
                "error": "Docker sandbox unavailable and local .venv fallback is disabled (fail-closed). Enable 'Allow Local .venv Fallback' to run Python locally inside this project.",
            }

        try:
            uv_bin = self._find_uv()
            cache_dir = self._get_uv_cache_dir()
            mpl_dir = cache_dir / "matplotlib"
            mpl_dir.mkdir(parents=True, exist_ok=True)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "UV_CACHE_DIR": str(cache_dir),
                "MPLCONFIGDIR": str(mpl_dir),
            }

            has_inline_deps = "# /// script" in args.code and "dependencies" in args.code
            if has_inline_deps and uv_bin:
                wheelhouse = self._get_wheelhouse_dir()
                temp_script = self.root / f".uv_ephemeral_{uuid.uuid4().hex[:8]}.py"
                try:
                    temp_script.write_text(args.code, encoding="utf-8")
                    uv_cmd = [uv_bin, "run", "--no-project", "--python", self.python_executable]
                    if wheelhouse.exists() and any(wheelhouse.glob("*.whl")):
                        uv_cmd.extend(["--find-links", str(wheelhouse)])
                    uv_cmd.append(str(temp_script))
                    proc = subprocess.run(
                        uv_cmd,
                        cwd=str(self.root),
                        capture_output=True,
                        text=True,
                        timeout=int(args.timeout),
                        env=env,
                    )
                    return {
                        "ok": proc.returncode == 0,
                        "isolated": True,
                        "backend": "uv_inline_deps",
                        "exit_code": proc.returncode,
                        "stdout": (proc.stdout or "")[:MAX_EXEC_OUTPUT],
                        "stderr": (proc.stderr or "")[:MAX_EXEC_OUTPUT],
                        "status": "completed" if proc.returncode == 0 else "failed",
                    }
                finally:
                    if temp_script.exists():
                        try:
                            temp_script.unlink()
                        except OSError:
                            pass

            proc = subprocess.run(
                [self.python_executable, "-I", "-B", "-c", args.code],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=int(args.timeout),
                env=env,
            )
            return {
                "ok": proc.returncode == 0,
                "isolated": False,
                "backend": "local_venv",
                "exit_code": proc.returncode,
                "stdout": (proc.stdout or "")[:MAX_EXEC_OUTPUT],
                "stderr": (proc.stderr or "")[:MAX_EXEC_OUTPUT],
                "status": "completed" if proc.returncode == 0 else "failed",
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "isolated": False, "backend": "local_venv", "error": f"Execution timed out after {args.timeout}s."}
        except Exception as exc:
            return {"ok": False, "isolated": False, "backend": "local_venv", "error": str(exc)}

    def _find_uv(self) -> str | None:
        """Locate the uv binary alongside the active python executable, in the workspace venv, or PATH."""
        try:
            py_path = Path(self.python_executable)
            uv_name = "uv.exe" if sys.platform == "win32" else "uv"
            candidate = py_path.parent / uv_name
            if candidate.is_file():
                return str(candidate)
        except Exception:
            pass

        sabrina_root = Path(r"c:\Users\charl\OneDrive\Documents\Sabrina")
        scripts_dir = "Scripts" if sys.platform == "win32" else "bin"
        uv_name = "uv.exe" if sys.platform == "win32" else "uv"
        for venv_name in ("freqtrade_pivot", "sabrina_console_desktop"):
            candidate = sabrina_root / venv_name / ".venv" / scripts_dir / uv_name
            if candidate.is_file():
                return str(candidate)

        return shutil.which("uv")

    def _get_wheelhouse_dir(self) -> Path:
        """Locate or provision the local artifact wheelhouse."""
        local = self.root / "artifacts" / "wheels"
        if local.is_dir():
            return local
        sabrina_wheelhouse = Path(r"c:\Users\charl\OneDrive\Documents\Sabrina\artifacts\wheels")
        sabrina_wheelhouse.mkdir(parents=True, exist_ok=True)
        return sabrina_wheelhouse

    def _get_uv_cache_dir(self) -> Path:
        """Locate or provision the uv disk cache directory inside an accessible workspace path."""
        local = self.root / "artifacts" / "cache"
        if local.is_dir():
            return local
        sabrina_cache = Path(r"c:\Users\charl\OneDrive\Documents\Sabrina\artifacts\cache")
        sabrina_cache.mkdir(parents=True, exist_ok=True)
        return sabrina_cache

    def _manage_dependencies(self, args: ManageDependenciesInput) -> dict[str, Any]:
        """Manage workspace Python packages and artifact cache using uv/pip."""
        action = str(args.action).strip().lower()
        uv_bin = self._find_uv()
        wheelhouse = self._get_wheelhouse_dir()
        cache_dir = self._get_uv_cache_dir()
        mpl_dir = cache_dir / "matplotlib"
        mpl_dir.mkdir(parents=True, exist_ok=True)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            "UV_CACHE_DIR": str(cache_dir),
            "MPLCONFIGDIR": str(mpl_dir),
        }
        try:
            env["VIRTUAL_ENV"] = str(Path(self.python_executable).parent.parent)
        except Exception:
            pass

        if action == "cache_info":
            wheels = [f.name for f in wheelhouse.glob("*.whl")] if wheelhouse.exists() else []
            return {
                "ok": True,
                "backend": "uv" if uv_bin else "pip",
                "uv_available": uv_bin is not None,
                "wheelhouse_dir": str(wheelhouse),
                "cached_wheels_count": len(wheels),
                "cached_wheels": sorted(wheels)[:50],
                "cache_dir": str(cache_dir),
            }

        if action == "list":
            cmd = [uv_bin, "pip", "list", "--python", self.python_executable] if uv_bin else [self.python_executable, "-m", "pip", "list"]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    env=env,
                    cwd=str(self.root),
                )
                return {
                    "ok": proc.returncode == 0,
                    "output": (proc.stdout if proc.returncode == 0 else proc.stderr)[:MAX_EXEC_OUTPUT],
                    "backend": "uv" if uv_bin else "pip",
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if action == "install":
            if not args.packages:
                return {"ok": False, "error": "No packages specified for install."}

            _safe_pkg_re = re.compile(r"^[A-Za-z0-9_.\-<>!=~]+$")
            clean_packages: list[str] = []
            for pkg in args.packages:
                pkg_str = str(pkg).strip()
                if not pkg_str or not _safe_pkg_re.match(pkg_str):
                    return {"ok": False, "error": f"Invalid or unsafe package specification: {pkg!r}"}
                clean_packages.append(pkg_str)

            if uv_bin:
                cmd = [uv_bin, "pip", "install", "--python", self.python_executable]
                if wheelhouse.exists() and any(wheelhouse.glob("*.whl")):
                    cmd.extend(["--find-links", str(wheelhouse)])
                if args.wheelhouse_only:
                    cmd.append("--no-index")
                cmd.extend(clean_packages)
            else:
                cmd = [self.python_executable, "-m", "pip", "install"]
                if wheelhouse.exists() and any(wheelhouse.glob("*.whl")):
                    cmd.extend(["--find-links", str(wheelhouse)])
                if args.wheelhouse_only:
                    cmd.append("--no-index")
                cmd.extend(clean_packages)

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                    cwd=str(self.root),
                )
                return {
                    "ok": proc.returncode == 0,
                    "packages": clean_packages,
                    "stdout": (proc.stdout or "")[:MAX_EXEC_OUTPUT],
                    "stderr": (proc.stderr or "")[:MAX_EXEC_OUTPUT],
                    "exit_code": proc.returncode,
                    "backend": "uv" if uv_bin else "pip",
                }
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "Installation timed out after 60s."}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        if action == "cache_wheel":
            if not args.packages:
                return {"ok": False, "error": "No packages specified to cache."}

            _safe_pkg_re = re.compile(r"^[A-Za-z0-9_.\-<>!=~]+$")
            clean_packages = []
            for pkg in args.packages:
                pkg_str = str(pkg).strip()
                if not pkg_str or not _safe_pkg_re.match(pkg_str):
                    return {"ok": False, "error": f"Invalid or unsafe package specification: {pkg!r}"}
                clean_packages.append(pkg_str)

            wheelhouse.mkdir(parents=True, exist_ok=True)
            cmd = [self.python_executable, "-m", "pip", "download", "-d", str(wheelhouse)] + clean_packages
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    env=env,
                    cwd=str(self.root),
                )
                return {
                    "ok": proc.returncode == 0,
                    "cached": clean_packages,
                    "wheelhouse_dir": str(wheelhouse),
                    "stdout": (proc.stdout or "")[:MAX_EXEC_OUTPUT],
                    "stderr": (proc.stderr or "")[:MAX_EXEC_OUTPUT],
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return {"ok": False, "error": f"Unknown action: '{action}'. Choose from: 'list', 'install', 'cache_info', 'cache_wheel'."}
