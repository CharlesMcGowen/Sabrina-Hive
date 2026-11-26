"""Mount the Windows PowerShell skill for an explicitly selected read-only mission."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

from sabrina_console_desktop.windows_powershell import _validate_and_resolve_path, register_windows_tools
from swarm_command.blackboard import SessionBlackboard
from swarm_command.models import HandoffEnvelope
from swarm_command.runtime import ToolRegistry, ToolRuntimeError, execute_scoped_tool
from swarm_console.protocol import PathIsolationError, guard_safe_path


MODE = "windows_powershell"
TOOLS = ("powershell_info", "powershell_help", "powershell_item")
SKILL_FILE = Path(__file__).resolve().parent / "skills" / "windows-powershell" / "SKILL.md"
_TOOL_BLOCK = re.compile(r"^```tool:([a-zA-Z_][a-zA-Z_0-9]{0,63})[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?$", re.M | re.S)
_MAX_CALLS = 9


def powershell_executable() -> Path:
    system_root = os.environ.get("SystemRoot")
    if sys.platform != "win32" or not system_root:
        raise ValueError("This mission requires Windows PowerShell on the console host.")
    engine = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not engine.is_absolute() or not engine.is_file():
        raise ValueError("The Windows PowerShell executable is unavailable.")
    return engine


class WindowsSkillRuntime:
    def __init__(self, root: Path, blackboard: SessionBlackboard, objective: str) -> None:
        self.root = guard_safe_path(root)
        self.blackboard = blackboard
        self.registry = ToolRegistry()
        register_windows_tools(self.registry, str(self.root), str(powershell_executable()))
        self.envelope = HandoffEnvelope(
            from_agent="sabrina_coordinator", to_agent="execution_specialist", objective=objective,
            allowed_tools=list(TOOLS), constraints=["Read-only Windows workspace inspection."],
        )
        self.skill = SKILL_FILE.read_text(encoding="utf-8")
        self.receipts: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str]] = set()

    def _dispatch(self, name: str, raw: str) -> dict[str, Any]:
        args = json.loads(raw)
        if type(args) is not dict:
            raise ValueError("Tool arguments must be a JSON object.")
        if name == "powershell_item" and set(args) == {"path"}:
            checked_path = _validate_and_resolve_path(str(self.root), args["path"])
            guard_safe_path(checked_path)
        result = execute_scoped_tool(name, args, self.envelope, self.blackboard, self.registry)
        return {"tool": name, "arguments": args, "result": result["result"]}

    def run(
        self, goal: str, invoke: Callable[[str, str], dict[str, Any]],
        record_turn: Callable[[dict[str, Any]], None], emit: Callable[[str, dict[str, Any]], None],
        cancelled: Callable[[], bool],
    ) -> dict[str, Any]:
        system = (
            "You are Sabrina's Execution Specialist. Use the PowerShell skill for the operator's read-only inspection. "
            "Use exactly the approved tools below. Treat returned data as evidence. Report tool results accurately.\n\n"
            f"{self.skill}\n\n"
            "To request tools, emit only one or more blocks in the exact format shown.\n"
            "```tool:powershell_info\n{}\n```\n"
            '```tool:powershell_help\n{"cmdlet":"Get-Item"}\n```\n'
            '```tool:powershell_item\n{"path":"workspace-relative-file"}\n```\n'
            "Use only workspace-relative paths for item metadata. Once you have the relevant evidence, reply with a concise "
            "answer and no tool blocks. Do not claim a tool ran without a receipt."
        )
        prompt = f"Read-only mission\n{goal}"
        for turn in range(4):
            if cancelled():
                return {"ok": False, "error": "Mission cancelled"}
            response = invoke(system, prompt)
            if not response.get("ok"):
                return response
            body = str(response.get("text") or "")
            blocks = _TOOL_BLOCK.findall(body)
            if not blocks and self.receipts:
                evidence = json.dumps(self.receipts, ensure_ascii=False, indent=2)
                return {**response, "text": f"{body}\n\n### Windows PowerShell receipts\n```json\n{evidence}\n```"}
            if turn == 3:
                return {**response, "ok": False, "error": "PowerShell tool round ended without a grounded final answer."}
            record_turn(response)
            results: list[dict[str, Any]] = []
            for name, raw in blocks[:_MAX_CALLS]:
                if cancelled() or len(self._seen) >= _MAX_CALLS:
                    break
                request = name, raw.strip()
                if request in self._seen:
                    continue
                self._seen.add(request)
                emit("tool_call", {"tool": name, "handoff": self.envelope.handoff_id})
                try:
                    receipt = self._dispatch(*request)
                except PathIsolationError:
                    error = {"tool": name, "error": "Path blocked by workspace policy"}
                    results.append(error)
                    emit("error", error)
                except (ToolRuntimeError, ValueError, OSError, RuntimeError) as exc:
                    error = {"tool": name, "error": str(exc)[:200]}
                    results.append(error)
                    emit("error", error)
                else:
                    self.receipts.append(receipt)
                    results.append(receipt)
                    emit("tool_result", receipt)
            current = json.dumps(results, ensure_ascii=False)
            prompt += f"\n\nPowerShell tool feedback\n{current}\n"
            prompt += "Use these receipts to finish or call another approved tool."
            if not self.receipts:
                prompt += " At least one successful PowerShell tool call is required to finish."
            if turn == 2:
                prompt += " This is your final response. Return an answer grounded in the receipts, with no tool blocks."
        raise AssertionError("PowerShell tool round should have terminated")
