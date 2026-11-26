"""Small, fail-closed adapter for the local Vercel agent-desktop CLI.

The CLI is only a computer-control surface; Sabrina still owns planning and
must perform an observe -> one action -> fresh observe loop. This module
keeps command construction typed and never invokes a shell.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

_REF_RE = re.compile(r"^@[A-Za-z0-9_-]+:e[0-9]+$")
_OBSERVE_COMMANDS = {"status", "list-apps", "list-windows", "snapshot", "find", "get", "wait"}
_ACTION_COMMANDS = {"launch", "click", "set-value", "clear"}
_MAX_OUTPUT = 12000


def _error(code: str, message: str, **details: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"ok": False, "error": code, "message": message}
    if details:
        result["details"] = details
    return result


def _validate_timeout(timeout_ms: int) -> Optional[Dict[str, Any]]:
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 100 <= timeout_ms <= 60000:
        return _error("invalid_timeout", "timeout_ms must be an integer from 100 to 60000")
    return None


def _validate_ref(ref: Optional[str]) -> Optional[Dict[str, Any]]:
    if ref is not None and not _REF_RE.fullmatch(ref):
        return _error("invalid_ref", "UI refs must come from a fresh snapshot/find result")
    return None


def _executable() -> Optional[str]:
    if platform.system().lower() != "windows":
        return None
    return shutil.which("agent-desktop")


def _base_args(session: Optional[str], agent_id: Optional[str], headed: bool) -> list[str]:
    args: list[str] = []
    if session:
        args += ["--session", session]
    if agent_id:
        args += ["--agent-id", agent_id]
    if headed:
        args.append("--headed")
    return args


def _run(command: str, args: Sequence[str], *, input_text: Optional[str] = None, timeout_ms: int = 5000) -> Dict[str, Any]:
    exe = _executable()
    if not exe:
        return _error(
            "desktop_unavailable",
            "agent-desktop is available only on the Windows orchestrator; no local desktop action was attempted",
            platform=platform.system(),
        )
    timeout_error = _validate_timeout(timeout_ms)
    if timeout_error:
        return timeout_error
    try:
        proc = subprocess.run(
            [exe, *args],
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_ms / 1000 + 5,
            shell=False,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return _error("timeout", f"agent-desktop {command} timed out", command=command)
    except OSError as exc:
        return _error("exec_failed", f"agent-desktop {command} could not start", errno=getattr(exc, "errno", None))

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    try:
        data: Any = json.loads(stdout) if stdout else None
    except json.JSONDecodeError:
        data = stdout[:_MAX_OUTPUT]
    result: Dict[str, Any] = {"ok": proc.returncode == 0, "command": command, "exit_code": proc.returncode}
    if data is not None:
        result["data"] = data
    if stderr:
        result["stderr"] = stderr[:2000]
    if proc.returncode != 0:
        result["message"] = "agent-desktop returned a non-zero exit code"
    return result


def observe(
    command: str,
    *,
    app: Optional[str] = None,
    ref: Optional[str] = None,
    property: Optional[str] = None,
    role: Optional[str] = None,
    name: Optional[str] = None,
    text: Optional[str] = None,
    value: Optional[str] = None,
    window_id: Optional[str] = None,
    surface: Optional[str] = None,
    timeout_ms: int = 5000,
    session: Optional[str] = None,
    agent_id: Optional[str] = None,
    limit: int = 50,
    first: bool = False,
    exact: bool = False,
    interactive_only: bool = False,
    compact: bool = False,
) -> Dict[str, Any]:
    """Run an observation-only command and return parsed JSON when possible."""
    if command not in _OBSERVE_COMMANDS:
        return _error("invalid_observe_command", f"unsupported observe command: {command}")
    ref_error = _validate_ref(ref)
    if ref_error:
        return ref_error
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        return _error("invalid_limit", "limit must be an integer from 1 to 500")
    args = _base_args(session, agent_id, False) + [command]
    if app:
        args += ["--app", app]
    if window_id:
        args += ["--window-id", window_id]
    if ref:
        args.append(ref)
    if property:
        args += ["--property", property]
    if role:
        args += ["--role", role]
    if name:
        args += ["--name", name]
    if text:
        args += ["--text", text]
    if value:
        args += ["--value", value]
    if surface:
        args += ["--surface", surface]
    if command in {"snapshot", "find"}:
        args += ["--timeout-ms", str(timeout_ms)]
    if command == "snapshot":
        if interactive_only:
            args.append("--interactive-only")
        if compact:
            args.append("--compact")
    if command == "find":
        args += ["--limit", str(limit)]
        if first:
            args.append("--first")
        if exact:
            args.append("--exact")
    if command == "wait":
        args += ["--timeout", str(timeout_ms)]
    return _run(command, args, timeout_ms=timeout_ms)


def act(
    command: str,
    *,
    app: Optional[str] = None,
    ref: Optional[str] = None,
    value: Optional[str] = None,
    timeout_ms: int = 5000,
    session: Optional[str] = None,
    agent_id: Optional[str] = None,
    headed: bool = False,
    activate: bool = False,
    no_attach: bool = False,
    cdp: Optional[str] = None,
    args: Optional[Iterable[str]] = None,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Perform one bounded local desktop action; refs must be fresh."""
    if command not in _ACTION_COMMANDS:
        return _error("invalid_action_command", f"unsupported desktop action: {command}")
    ref_error = _validate_ref(ref)
    if ref_error:
        return ref_error
    if command in {"click", "set-value", "clear"} and not ref:
        return _error("missing_ref", f"{command} requires a ref from a fresh snapshot/find result")
    if command == "set-value" and value is None:
        return _error("missing_value", "set-value requires value")
    argv = _base_args(session, agent_id, headed) + [command]
    if command == "launch":
        if not app:
            return _error("missing_app", "launch requires app")
        argv.append(app)
        if activate:
            argv.append("--activate")
        if no_attach:
            argv.append("--no-attach")
        if cdp:
            argv += ["--cdp", cdp]
        for item in args or ():
            argv += ["--arg", str(item)]
        if cwd:
            argv += ["--cwd", cwd]
        for key, item in (env or {}).items():
            argv += ["--env", f"{key}={item}"]
    else:
        argv.append(ref or "")
    input_text = None
    if command == "set-value":
        argv.append("--stdin")
        input_text = value
    return _run(command, argv, input_text=input_text, timeout_ms=timeout_ms)

