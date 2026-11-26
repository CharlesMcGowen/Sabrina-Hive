"""Multi-turn Gemini Interactions API Workspace Agent for Sabrina Console.

Connects Gemini 4 models (`google-genai` Interactions API) to `WorkspaceSandbox`
with Pydantic-validated function calling, unified diff hunk staging, automatic
pre-turn checkpoints, closed-loop diagnostic verification, context compaction,
and 3 permission modes:
- `read_only`: only `list_dir`, `read_file`, and `run_diagnostics` may execute.
- `approve_writes`: mutating tools stage a hunk-granularity diff/preview for operator Approve/Reject.
- `sandbox_auto`: mutating tools execute immediately inside the jailed sandbox with turn checkpoints.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable

from .gemini_copilot import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_THINKING_LEVEL,
    SUPPORTED_GEMINI_MODELS,
    SUPPORTED_THINKING_LEVELS,
    GeminiCopilotError,
    _load_genai_module,
    get_copilot_status,
)
from .json_handoff import (
    HANDOFF_RESPONSE_FORMAT,
    HANDOFF_SYSTEM_INSTRUCTION,
    handoff_input,
    requests_json_object,
    validated_json_object,
)
from .workspace_sandbox import (
    MUTATING_TOOLS,
    POLICY_CAPABILITY_MATRIX,
    READ_ONLY_TOOLS,
    VALID_WORKSPACE_MODES,
    WorkspaceSandbox,
    WorkspaceSandboxError,
    gemini_tool_declarations,
)

WORKSPACE_AGENT_SYSTEM_INSTRUCTION = (
    "You are Sabrina's Workspace Coding Agent. You have access to sandboxed project "
    "tools (`list_dir`, `read_file`, `create_dir`, `write_file`, `edit_file`, `run_python`, "
    "`run_diagnostics`, `manage_process`, `manage_dependencies`). All paths MUST be relative to the active project root. "
    "Inspect existing files before editing, keep changes clean and minimal, verify edits against "
    "diagnostics, and summarize exact files created or modified. "
    "If code fails with ModuleNotFoundError or ImportError, do not hallucinate workarounds or fake mocks; "
    "use `manage_dependencies` to check or install required packages, or declare inline PEP 723 script metadata."
)

MAX_COMPACTED_TOOL_RESULT_CHARS = 3600
DEFAULT_WORKSPACE_MAX_TURNS = 6


def compact_tool_result_payload(tool_name: str, result: dict[str, Any], max_chars: int = MAX_COMPACTED_TOOL_RESULT_CHARS) -> str:
    """Deterministically compact large tool results so multi-turn prompt budgets stay bounded."""
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) <= max_chars:
        return raw
    compacted = dict(result)
    for field in ("content", "diff", "stdout", "stderr"):
        val = compacted.get(field)
        if isinstance(val, str) and len(val) > max_chars // 2:
            head = val[: max_chars // 3]
            tail = val[-(max_chars // 6) :]
            compacted[field] = f"{head}\n...[compacted {len(val)} chars for {tool_name}]...\n{tail}"
    if isinstance(compacted.get("entries"), list) and len(compacted["entries"]) > 80:
        total = len(compacted["entries"])
        compacted["entries"] = compacted["entries"][:80]
        compacted["compacted_entries_note"] = f"Showing first 80 of {total} entries."
    return json.dumps(compacted, ensure_ascii=False)[:max_chars]


class WorkspaceAgent:
    """Stateful Gemini tool-calling agent bound to a `WorkspaceSandbox`."""

    def __init__(
        self,
        sandbox: WorkspaceSandbox,
        *,
        mode: str = "approve_writes",
        model: str = DEFAULT_GEMINI_MODEL,
        thinking_level: str = DEFAULT_THINKING_LEVEL,
        max_turns: int = DEFAULT_WORKSPACE_MAX_TURNS,
        instructions: str = "",
        json_handoff: bool = False,
        auto_checkpoint: bool = True,
        auto_diagnostics: bool = True,
        client: Any | None = None,
        client_factory: Callable[[], Any] | None = None,
        api_key_provider: Callable[[], str | None] | None = None,
    ) -> None:
        if mode not in VALID_WORKSPACE_MODES:
            raise ValueError(f"Invalid workspace mode '{mode}'. Choose from {VALID_WORKSPACE_MODES}.")
        self.sandbox = sandbox
        self.mode = mode
        self.model = model
        self.thinking_level = thinking_level
        self.max_turns = max(1, min(12, int(max_turns)))
        self.json_handoff = json_handoff
        self._json_handoff_instruction = HANDOFF_SYSTEM_INSTRUCTION
        self._system_instruction = WORKSPACE_AGENT_SYSTEM_INSTRUCTION
        if instructions:
            self._system_instruction += f"\n\nAdditional workspace instructions:\n{instructions}"
            self._json_handoff_instruction += f"\n\nAdditional workspace instructions:\n{instructions}"
        self.auto_checkpoint = bool(auto_checkpoint)
        self.auto_diagnostics = bool(auto_diagnostics)
        self._client = client
        self._client_factory = client_factory
        self._api_key_provider = api_key_provider
        self.last_interaction_id: str | None = None
        self.pending_actions: dict[str, dict[str, Any]] = {}
        self._pending_json_handoffs: dict[str, tuple[str, list[dict[str, Any]]]] = {}
        self.harness_events: list[dict[str, Any]] = []
        self._plugin_access: Any | None = None
        self._pending_plugin_access: dict[str, Any] = {}

    def _emit_event(self, kind: str, summary: str, **payload: Any) -> dict[str, Any]:
        ev = {
            "id": f"hev_{uuid.uuid4().hex[:8]}",
            "ts": time.time(),
            "kind": kind,
            "summary": summary,
            **payload,
        }
        self.harness_events.append(ev)
        if len(self.harness_events) > 200:
            self.harness_events = self.harness_events[-200:]
        return ev

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        status = get_copilot_status()
        if not status["sdk_installed"]:
            raise GeminiCopilotError(status["reason"], code="sdk_missing", status=503)
        api_key = self._api_key_provider() if self._api_key_provider is not None else None
        if not api_key and not status["api_key_set"]:
            raise GeminiCopilotError(status["reason"], code="api_key_missing", status=503)
        genai = _load_genai_module()
        assert genai is not None
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()
        return self._client

    def _extract_function_calls(self, interaction: Any) -> list[dict[str, Any]]:
        steps = getattr(interaction, "steps", None) or []
        calls: list[dict[str, Any]] = []
        for step in steps:
            step_type = getattr(step, "type", None) if not isinstance(step, dict) else step.get("type")
            if step_type == "function_call":
                call_id = getattr(step, "id", None) if not isinstance(step, dict) else step.get("id")
                name = getattr(step, "name", None) if not isinstance(step, dict) else step.get("name")
                args = getattr(step, "arguments", None) if not isinstance(step, dict) else step.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append({"id": call_id or f"fc_{uuid.uuid4().hex[:8]}", "name": name or "", "arguments": args or {}})
        return calls

    def _call_model(
        self,
        input_payload: Any,
        *,
        model: str,
        thinking_level: str,
        previous_interaction_id: str | None,
        json_output: bool = False,
    ) -> Any:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": model,
            "input": input_payload,
            "system_instruction": self._json_handoff_instruction if json_output else self._system_instruction,
            "tools": [] if json_output else gemini_tool_declarations(),
            "generation_config": {"thinking_level": thinking_level},
        }
        if not json_output and self._plugin_access is not None:
            kwargs["tools"].append({
                "type": "function",
                "name": "request_plugin",
                "description": "Ask the coordinator to run an explicitly permitted plugin action.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plugin": {"type": "string", "description": "Plugin name."},
                        "action_json": {"type": "string", "description": "JSON object with the exact plugin operation and parameters."},
                    },
                    "required": ["plugin", "action_json"],
                },
            })
        if json_output:
            kwargs["response_format"] = HANDOFF_RESPONSE_FORMAT
            kwargs["store"] = False
        if previous_interaction_id:
            kwargs["previous_interaction_id"] = previous_interaction_id
        try:
            return client.interactions.create(**kwargs)
        except Exception as exc:
            raise GeminiCopilotError(f"Gemini API request failed: {exc}", code="gemini_api_error", status=502) from exc

    def _maybe_run_post_edit_diagnostics(self, rel_path: str) -> dict[str, Any] | None:
        if not self.auto_diagnostics or not rel_path.endswith(".py"):
            return None
        try:
            diag = self.sandbox.execute_tool(
                "run_diagnostics",
                {"path": rel_path, "checks": ["syntax", "security"]},
            )
            self._emit_event(
                "diagnostic",
                f"Diagnostics on {rel_path}: {'PASS' if diag.get('passed') else 'FAIL'} ({diag.get('error_count', 0)} errors)",
                path=rel_path,
                diagnostics=diag,
            )
            return diag
        except Exception:
            return None

    def _run_loop(
        self,
        initial_input: Any,
        *,
        model: str,
        thinking_level: str,
        previous_interaction_id: str | None,
        handoff_context: tuple[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        tool_events: list[dict[str, Any]] = []
        pending_out: list[dict[str, Any]] = []
        checkpoints_created: list[dict[str, Any]] = []
        diagnostics_out: list[dict[str, Any]] = []
        current_input = initial_input
        prev_id = previous_interaction_id
        turn_checkpointed = False
        draft: str | None = None
        handoff_results = list(handoff_context[1]) if handoff_context else []
        if handoff_context and isinstance(initial_input, list):
            handoff_results.extend(initial_input)

        for _ in range(self.max_turns - int(handoff_context is not None)):
            interaction = self._call_model(
                current_input,
                model=model,
                thinking_level=thinking_level,
                previous_interaction_id=prev_id,
            )
            prev_id = getattr(interaction, "id", None) or prev_id
            self.last_interaction_id = prev_id

            calls = self._extract_function_calls(interaction)
            if not calls:
                draft = (getattr(interaction, "output_text", None) or "").strip()
                break

            function_results: list[dict[str, Any]] = []
            paused_for_approval = False

            for call in calls:
                name = call["name"]
                args = call["arguments"]
                call_id = call["id"]
                self._emit_event("tool_call", f"{name}({args.get('path', '.')})", tool=name, arguments=args)

                if name == "request_plugin":
                    if self.mode == "approve_writes" and self._plugin_access is not None:
                        action_id = f"act_{uuid.uuid4().hex[:8]}"
                        access = self._plugin_access
                        from tools.coordinator_plugin_access import CoordinatorPluginAccess
                        pending_access = CoordinatorPluginAccess(access.mission_id, access.scope, root=access.root)
                        if (not access._current_policy()
                                or pending_access._policy_hash != access._policy_hash):
                            pending_access.cancel()
                            res = {"ok": False, "error": "Coordinator policy changed during the turn."}
                            tool_events.append({"tool": name, "arguments": args, "result": res, "status": "blocked"})
                            function_results.append({
                                "type": "function_result", "name": name, "call_id": call_id,
                                "result": compact_tool_result_payload(name, res),
                            })
                            continue
                        try:
                            if (set(args) != {"plugin", "action_json"}
                                    or not isinstance(args["action_json"], str)
                                    or len(args["action_json"]) > 10000):
                                raise ValueError("Invalid plugin request fields")
                            action_payload = json.loads(args["action_json"])
                            check = pending_access.request(
                                pending_access.mission_id, "workspace-agent", args["plugin"], action_payload)
                        except (KeyError, TypeError, ValueError):
                            check = {"ok": False, "error": "Invalid plugin request."}
                        if not check.get("ok"):
                            pending_access.cancel()
                            tool_events.append({"tool": name, "arguments": args, "result": check, "status": "blocked"})
                            function_results.append({
                                "type": "function_result", "name": name, "call_id": call_id,
                                "result": compact_tool_result_payload(name, check),
                            })
                            continue
                        self._pending_plugin_access[action_id] = (
                            pending_access, json.dumps(args, sort_keys=True, ensure_ascii=False, allow_nan=False))
                        pending_entry = {
                            "id": action_id, "diff_id": None, "call_id": call_id,
                            "tool": name, "arguments": args, "path": "coordinator plugin",
                            "diff": json.dumps(args, ensure_ascii=False), "hunks": [],
                            "interaction_id": prev_id, "model": model, "thinking_level": thinking_level,
                        }
                        self.pending_actions[action_id] = pending_entry
                        pending_out.append(pending_entry)
                        self._emit_event("approval_request", "Awaiting operator approval for plugin execution.",
                                         action_id=action_id)
                        paused_for_approval = True
                        continue
                    res = self._request_coordinator_plugin(args)
                    status_str = "executed" if res.get("ok") else "blocked"
                    tool_events.append({"tool": name, "arguments": args, "result": res, "status": status_str})
                    self._emit_event("tool_result", f"{name} -> {status_str}", tool=name, status=status_str)
                    function_results.append({
                        "type": "function_result", "name": name, "call_id": call_id,
                        "result": compact_tool_result_payload(name, res),
                    })
                    continue

                allowed_by_policy, policy_reason = self.sandbox.check_policy(name)
                if not allowed_by_policy:
                    res = {"ok": False, "error": policy_reason}
                    tool_events.append({"tool": name, "arguments": args, "result": res, "status": "blocked"})
                    self._emit_event("tool_result", policy_reason, tool=name, status="blocked")
                    function_results.append({
                        "type": "function_result",
                        "name": name,
                        "call_id": call_id,
                        "result": compact_tool_result_payload(name, res),
                    })
                    continue

                if name in READ_ONLY_TOOLS or (name in MUTATING_TOOLS and self.mode == "sandbox_auto"):
                    if (
                        self.auto_checkpoint
                        and not turn_checkpointed
                        and name in {"create_dir", "write_file", "edit_file"}
                    ):
                        try:
                            cp = self.sandbox.create_checkpoint(label=f"auto:{name}:{args.get('path', '.')}")
                            checkpoints_created.append(cp)
                            turn_checkpointed = True
                            self._emit_event("checkpoint", f"Created checkpoint {cp['id']}", checkpoint=cp)
                        except Exception:
                            pass

                    try:
                        res = self.sandbox.execute_tool(name, args)
                    except WorkspaceSandboxError as exc:
                        res = {"ok": False, "error": str(exc)}

                    if res.get("ok") and name in {"write_file", "edit_file"}:
                        diag = self._maybe_run_post_edit_diagnostics(str(res.get("path") or args.get("path") or ""))
                        if diag is not None:
                            res["diagnostics"] = diag
                            diagnostics_out.append(diag)

                    status_str = "executed" if res.get("ok") else "error"
                    tool_events.append({"tool": name, "arguments": args, "result": res, "status": status_str})
                    self._emit_event("tool_result", f"{name} -> {status_str}", tool=name, status=status_str)
                    function_results.append({
                        "type": "function_result",
                        "name": name,
                        "call_id": call_id,
                        "result": compact_tool_result_payload(name, res),
                    })
                elif name in MUTATING_TOOLS and self.mode == "read_only":
                    res = {"ok": False, "error": "Blocked: workspace is in read_only mode."}
                    tool_events.append({"tool": name, "arguments": args, "result": res, "status": "blocked"})
                    self._emit_event("tool_result", "Blocked in read_only mode", tool=name, status="blocked")
                    function_results.append({
                        "type": "function_result",
                        "name": name,
                        "call_id": call_id,
                        "result": compact_tool_result_payload(name, res),
                    })
                elif name in MUTATING_TOOLS and self.mode == "approve_writes":
                    try:
                        staged = self.sandbox.stage_diff(name, args, author=model)
                    except WorkspaceSandboxError as exc:
                        res = {"ok": False, "error": str(exc)}
                        tool_events.append({"tool": name, "arguments": args, "result": res, "status": "error"})
                        function_results.append({
                            "type": "function_result",
                            "name": name,
                            "call_id": call_id,
                            "result": compact_tool_result_payload(name, res),
                        })
                        continue

                    action_id = f"act_{uuid.uuid4().hex[:8]}"
                    pending_entry = {
                        "id": action_id,
                        "diff_id": staged["id"],
                        "call_id": call_id,
                        "tool": name,
                        "arguments": args,
                        "path": staged.get("path", "."),
                        "diff": staged.get("diff", ""),
                        "hunks": staged.get("hunks", []),
                        "interaction_id": prev_id,
                        "model": model,
                        "thinking_level": thinking_level,
                    }
                    self.pending_actions[action_id] = pending_entry
                    pending_out.append(pending_entry)
                    self._emit_event(
                        "diff_proposal",
                        f"Staged {name} on {pending_entry['path']} ({len(pending_entry['hunks'])} hunk(s))",
                        action_id=action_id,
                        diff_id=staged["id"],
                        path=pending_entry["path"],
                        hunks=pending_entry["hunks"],
                    )
                    self._emit_event(
                        "approval_request",
                        f"Awaiting operator approval for {name} on {pending_entry['path']}",
                        action_id=action_id,
                    )
                    paused_for_approval = True
                else:
                    res = {"ok": False, "error": f"Unknown tool: {name}"}
                    tool_events.append({"tool": name, "arguments": args, "result": res, "status": "error"})
                    function_results.append({
                        "type": "function_result",
                        "name": name,
                        "call_id": call_id,
                        "result": compact_tool_result_payload(name, res),
                    })

            if handoff_context:
                handoff_results.extend(function_results)
            if paused_for_approval:
                if handoff_context:
                    for pending in pending_out:
                        self._pending_json_handoffs[pending["id"]] = (handoff_context[0], handoff_results)
                return {
                    "ok": True,
                    "reply": f"Queued {len(pending_out)} workspace action(s) for operator approval.",
                    "tool_events": tool_events,
                    "pending_approvals": pending_out,
                    "checkpoints": checkpoints_created,
                    "diagnostics": diagnostics_out,
                    "harness_events": list(self.harness_events[-25:]),
                    "interaction_id": prev_id,
                    "model": model,
                    "thinking_level": thinking_level,
                }

            current_input = function_results

        error = ""
        if handoff_context:
            try:
                completion = self._call_model(
                    handoff_input(handoff_context[0], handoff_results, draft or ""),
                    model=model,
                    thinking_level=thinking_level,
                    previous_interaction_id=None,
                    json_output=True,
                )
                if self._extract_function_calls(completion):
                    raise ValueError("Unexpected tool call in JSON handoff")
                reply = validated_json_object(getattr(completion, "output_text", None) or "")
            except GeminiCopilotError as exc:
                error = str(exc)
            except ValueError:
                error = "JSON handoff failed validation."
            if error:
                reply = error
            self._emit_event("error" if error else "final_summary", reply, model=model, json_handoff=True)
        elif draft is None:
            reply = f"Stopped after reaching max_turns ({self.max_turns})."
        else:
            reply = draft or "(No text reply returned.)"
            self._emit_event("final_summary", reply, model=model)

        return {
            "ok": not error,
            "reply": reply,
            **({"error": error} if error else {}),
            "tool_events": tool_events,
            "pending_approvals": pending_out,
            "checkpoints": checkpoints_created,
            "diagnostics": diagnostics_out,
            "harness_events": list(self.harness_events[-25:]),
            "interaction_id": prev_id,
            "model": model,
            "thinking_level": thinking_level,
        }

    def _request_coordinator_plugin(self, args: dict[str, Any]) -> dict[str, Any]:
        access = self._plugin_access
        capabilities = POLICY_CAPABILITY_MATRIX.get(self.sandbox.policy_profile, {})
        if (access is None or self.mode == "read_only" or not capabilities.get("process")
                or not isinstance(args, dict) or set(args) != {"plugin", "action_json"}):
            return {"ok": False, "error": "Coordinator plugin access is unavailable under this workspace policy."}
        # Plugin modules execute as the console user, outside the jailed Python
        # runner. The process capability is therefore required for every plugin.
        # Network is checked per operation, so future trusted local tools need not
        # declare network access.
        plugin = args["plugin"]
        raw_action = args["action_json"]
        if not isinstance(raw_action, str) or len(raw_action) > 10000:
            return {"ok": False, "error": "Plugin action must be a bounded JSON object."}
        try:
            action = json.loads(raw_action)
        except (ValueError, TypeError):
            return {"ok": False, "error": "Plugin action must be a JSON object."}
        if (not isinstance(plugin, str) or not isinstance(action, dict)
                or not isinstance(action.get("operation"), str)):
            return {"ok": False, "error": "Plugin and action are required."}
        requirements = access.operation_requirements(plugin, action["operation"])
        if (not isinstance(requirements, tuple)
                or any(requirement not in ("network", "read") or not capabilities.get(requirement)
                       for requirement in requirements)):
            return {"ok": False, "error": "Workspace policy does not allow the plugin's required capabilities."}
        mission_id = access.mission_id
        request = access.request(mission_id, "workspace-agent", plugin, action)
        if not request.get("ok"):
            return request
        grant = access.decide(mission_id, request["request_id"])
        self._emit_event("telegraphic_note", "Coordinator policy evaluated a plugin request.",
                         tool="request_plugin", decision=grant.get("status"),
                         capability=request.get("capability"), reason=grant.get("reason"))
        if not grant.get("ok"):
            return grant
        return access.execute(mission_id, "workspace-agent", grant["grant_id"], plugin, action)

    def send_message(
        self,
        message: str,
        *,
        model: str | None = None,
        thinking_level: str | None = None,
    ) -> dict[str, Any]:
        selected_model = (model or self.model or DEFAULT_GEMINI_MODEL).strip()
        if selected_model not in SUPPORTED_GEMINI_MODELS:
            raise GeminiCopilotError(f"Unsupported model '{selected_model}'.", code="invalid_model", status=400)
        selected_effort = (thinking_level or self.thinking_level or DEFAULT_THINKING_LEVEL).strip().lower()
        if selected_effort not in SUPPORTED_THINKING_LEVELS:
            raise GeminiCopilotError(f"Unsupported thinking_level '{selected_effort}'.", code="invalid_thinking_level", status=400)

        # Only the operator's message can set the turn's scope. A model response
        # or a resumed file approval cannot extend or recreate this authority.
        capabilities = POLICY_CAPABILITY_MATRIX.get(self.sandbox.policy_profile, {})
        if "[plugins:" in message and self.mode != "read_only" and capabilities.get("process"):
            from tools.coordinator_plugin_access import CoordinatorPluginAccess, scope_from_goal
            scope = scope_from_goal(message)
            if scope:
                self._plugin_access = CoordinatorPluginAccess("workspace-" + uuid.uuid4().hex, scope)
        try:
            return self._run_loop(
                message,
                model=selected_model,
                thinking_level=selected_effort,
                previous_interaction_id=self.last_interaction_id,
                handoff_context=(message, []) if self.json_handoff and requests_json_object(message) else None,
            )
        finally:
            if self._plugin_access is not None:
                self._plugin_access.cancel()
                self._plugin_access = None

    def decide_action(
        self,
        action_id: str,
        *,
        approve: bool,
        accepted_hunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        action = self.pending_actions.pop(action_id, None)
        if action is None:
            return {"ok": False, "error": f"Unknown or already-resolved action_id: {action_id}"}
        handoff_context = self._pending_json_handoffs.pop(action_id, None)

        diff_id = action.get("diff_id")
        plugin_pending = self._pending_plugin_access.pop(action_id, None)
        plugin_access = plugin_pending[0] if plugin_pending else None
        if approve:
            try:
                if action["tool"] == "request_plugin":
                    if plugin_access is None:
                        res = {"ok": False, "error": "Coordinator plugin request expired."}
                    elif json.dumps(action["arguments"], sort_keys=True, ensure_ascii=False, allow_nan=False) != plugin_pending[1]:
                        plugin_access.cancel()
                        res = {"ok": False, "error": "Plugin request changed after approval was requested."}
                    else:
                        self._plugin_access = plugin_access
                        try:
                            res = self._request_coordinator_plugin(action["arguments"])
                        finally:
                            plugin_access.cancel()
                            self._plugin_access = None
                elif diff_id:
                    applied = self.sandbox.apply_staged_diff(
                        diff_id,
                        accepted_hunk_ids=accepted_hunk_ids,
                        create_checkpoint=self.auto_checkpoint,
                    )
                    res = applied.get("result") or applied
                    if applied.get("checkpoint"):
                        res["checkpoint_id"] = applied["checkpoint"]["id"]
                else:
                    res = self.sandbox.execute_tool(action["tool"], action["arguments"])
                if res.get("ok") and action["tool"] in {"write_file", "edit_file"}:
                    diag = self._maybe_run_post_edit_diagnostics(str(action.get("path") or ""))
                    if diag is not None:
                        res["diagnostics"] = diag
            except WorkspaceSandboxError as exc:
                res = {"ok": False, "error": str(exc)}
        else:
            if plugin_access is not None:
                plugin_access.cancel()
            if diff_id:
                try:
                    self.sandbox.discard_staged_diff(diff_id)
                except Exception:
                    pass
            res = {"ok": False, "rejected_by_operator": True, "message": "Operator rejected this workspace action."}

        fn_result = [
            {
                "type": "function_result",
                "name": action["tool"],
                "call_id": action["call_id"],
                "result": compact_tool_result_payload(action["tool"], res),
            }
        ]
        follow = self._run_loop(
            fn_result,
            model=action["model"],
            thinking_level=action["thinking_level"],
            previous_interaction_id=action["interaction_id"],
            handoff_context=handoff_context,
        )
        follow["tool_events"].insert(
            0,
            {
                "tool": action["tool"],
                "arguments": action["arguments"],
                "result": res,
                "status": "approved" if (approve and res.get("ok")) else ("rejected" if not approve else "error"),
            },
        )
        return follow
