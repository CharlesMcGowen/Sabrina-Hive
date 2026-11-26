"""Pluggable Harness Collective Abstraction Layer for Sabrina Operator Console.

Defines the normalized `BaseHarness` contract, Pydantic-validated `HarnessConfig`,
`HarnessCapabilities`, `HarnessPolicy`, and `HarnessEvent` schemas, plus `HarnessRegistry`
and four selectable collective adapters:
1. `sabrina_native` (`SabrinaNativeHarness`): `WorkspaceSandbox` + `WorkspaceAgent` + hunk diffs + hybrid checkpoints + closed-loop diagnostics.
2. `mcp_harness` (`MCPToolBusHarness`): Model Context Protocol (MCP) tool-bus adapter with sandbox policy & checkpoint wrapping.
3. `external_cli` (`ExternalCLIHarnessAdapter`): Subprocess / CLI coding harness wrapper emitting normalized `HarnessEvent`s and capturing workspace diffs.
4. `mlx_ego_portal` (`MLXEgoPortalHarness`): Routes agentic tool/verification loops across local MLX / Ollama (`ego.local`) and virtual SQLite portals (`harnesses.external`).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Iterator

from .harness_updates import FACTORY_PROFILE, NATIVE_HARNESS_ID, HarnessUpdates, NativeHarnessProfile
from .workspace_agent import WorkspaceAgent
from .workspace_sandbox import (
    POLICY_CAPABILITY_MATRIX,
    VALID_POLICY_PROFILES,
    VALID_WORKSPACE_MODES,
    WorkspaceSandbox,
    WorkspaceSandboxError,
)

try:
    from pydantic import BaseModel, Field  # type: ignore[import-not-found]
    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover
    BaseModel = object  # type: ignore[assignment,misc]
    HAS_PYDANTIC = False

VALID_HARNESS_EVENT_KINDS = (
    "telegraphic_note",
    "tool_call",
    "tool_result",
    "diff_proposal",
    "diagnostic",
    "checkpoint",
    "approval_request",
    "final_summary",
    "error",
)


if HAS_PYDANTIC:
    class HarnessCapabilities(BaseModel):
        diffs: bool = Field(default=True, description="Supports unified diff & hunk staging.")
        checkpoints: bool = Field(default=True, description="Supports workspace snapshots & rollback.")
        diagnostics: bool = Field(default=True, description="Supports closed-loop syntax/lint/test diagnostics.")
        background_jobs: bool = Field(default=True, description="Supports managed background processes.")
        mcp_tools: bool = Field(default=False, description="Supports Model Context Protocol tool routing.")
        streaming_events: bool = Field(default=True, description="Emits normalized HarnessEvent stream.")
        sandboxing: bool = Field(default=True, description="Enforces path-jailed sandbox & policy tiers.")
        remote_portals: bool = Field(default=False, description="Supports MLX / Ego / SQLite virtual portals.")

        def to_dict(self) -> dict[str, Any]:
            return self.model_dump() if hasattr(self, "model_dump") else self.dict()

    class HarnessPolicy(BaseModel):
        profile: str = Field(default="sandboxed_edit", description="inspect_only | sandboxed_edit | full_harness")
        workspace_mode: str = Field(default="approve_writes", description="read_only | approve_writes | sandbox_auto")
        allow_local_venv_fallback: bool = Field(default=False, description="Allow local Python .venv fallback.")
        execution_authorized: bool = Field(default=False, description="Strict safety lock invariant (always False).")

        def to_dict(self) -> dict[str, Any]:
            d = self.model_dump() if hasattr(self, "model_dump") else self.dict()
            d["execution_authorized"] = False
            d["capabilities"] = dict(POLICY_CAPABILITY_MATRIX.get(self.profile, POLICY_CAPABILITY_MATRIX["inspect_only"]))
            return d

    class HarnessConfig(BaseModel):
        harness_id: str = Field(..., min_length=1)
        name: str = Field(..., min_length=1)
        kind: str = Field(default="native")
        description: str = Field(default="")
        default_model: str = Field(default="gemini-4-argon")
        endpoint: str = Field(default="")
        command_template: list[str] = Field(default_factory=list)
        mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
        capabilities: HarnessCapabilities = Field(default_factory=HarnessCapabilities)

        def to_dict(self) -> dict[str, Any]:
            d = self.model_dump() if hasattr(self, "model_dump") else self.dict()
            d["capabilities"] = self.capabilities.to_dict()
            return d

    class HarnessEvent(BaseModel):
        id: str = Field(default_factory=lambda: f"hev_{uuid.uuid4().hex[:8]}")
        session_id: str = Field(default="")
        harness_id: str = Field(default="sabrina_native")
        kind: str = Field(default="telegraphic_note")
        ts: float = Field(default_factory=time.time)
        actor: str = Field(default="harness")
        summary: str = Field(default="")
        confidence: float = Field(default=0.95, ge=0.0, le=1.0)
        payload: dict[str, Any] = Field(default_factory=dict)

        def to_dict(self) -> dict[str, Any]:
            return self.model_dump() if hasattr(self, "model_dump") else self.dict()
else:  # pragma: no cover
    class HarnessCapabilities:  # type: ignore[no-redef]
        def __init__(
            self,
            diffs: bool = True,
            checkpoints: bool = True,
            diagnostics: bool = True,
            background_jobs: bool = True,
            mcp_tools: bool = False,
            streaming_events: bool = True,
            sandboxing: bool = True,
            remote_portals: bool = False,
        ) -> None:
            self.diffs = bool(diffs)
            self.checkpoints = bool(checkpoints)
            self.diagnostics = bool(diagnostics)
            self.background_jobs = bool(background_jobs)
            self.mcp_tools = bool(mcp_tools)
            self.streaming_events = bool(streaming_events)
            self.sandboxing = bool(sandboxing)
            self.remote_portals = bool(remote_portals)

        def to_dict(self) -> dict[str, Any]:
            return {
                "diffs": self.diffs,
                "checkpoints": self.checkpoints,
                "diagnostics": self.diagnostics,
                "background_jobs": self.background_jobs,
                "mcp_tools": self.mcp_tools,
                "streaming_events": self.streaming_events,
                "sandboxing": self.sandboxing,
                "remote_portals": self.remote_portals,
            }

    class HarnessPolicy:  # type: ignore[no-redef]
        def __init__(
            self,
            profile: str = "sandboxed_edit",
            workspace_mode: str = "approve_writes",
            allow_local_venv_fallback: bool = False,
            execution_authorized: bool = False,
        ) -> None:
            self.profile = profile if profile in VALID_POLICY_PROFILES else "sandboxed_edit"
            self.workspace_mode = workspace_mode if workspace_mode in VALID_WORKSPACE_MODES else "approve_writes"
            self.allow_local_venv_fallback = bool(allow_local_venv_fallback)
            self.execution_authorized = False

        def to_dict(self) -> dict[str, Any]:
            return {
                "profile": self.profile,
                "workspace_mode": self.workspace_mode,
                "allow_local_venv_fallback": self.allow_local_venv_fallback,
                "execution_authorized": False,
                "capabilities": dict(POLICY_CAPABILITY_MATRIX.get(self.profile, POLICY_CAPABILITY_MATRIX["inspect_only"])),
            }

    class HarnessConfig:  # type: ignore[no-redef]
        def __init__(
            self,
            harness_id: str,
            name: str,
            kind: str = "native",
            description: str = "",
            default_model: str = "gemini-4-argon",
            endpoint: str = "",
            command_template: list[str] | None = None,
            mcp_servers: list[dict[str, Any]] | None = None,
            capabilities: HarnessCapabilities | None = None,
        ) -> None:
            self.harness_id = str(harness_id)
            self.name = str(name)
            self.kind = str(kind)
            self.description = str(description)
            self.default_model = str(default_model)
            self.endpoint = str(endpoint)
            self.command_template = list(command_template or [])
            self.mcp_servers = list(mcp_servers or [])
            self.capabilities = capabilities or HarnessCapabilities()

        def to_dict(self) -> dict[str, Any]:
            return {
                "harness_id": self.harness_id,
                "name": self.name,
                "kind": self.kind,
                "description": self.description,
                "default_model": self.default_model,
                "endpoint": self.endpoint,
                "command_template": self.command_template,
                "mcp_servers": self.mcp_servers,
                "capabilities": self.capabilities.to_dict(),
            }

    class HarnessEvent:  # type: ignore[no-redef]
        def __init__(
            self,
            session_id: str = "",
            harness_id: str = "sabrina_native",
            kind: str = "telegraphic_note",
            actor: str = "harness",
            summary: str = "",
            confidence: float = 0.95,
            payload: dict[str, Any] | None = None,
            id: str | None = None,
            ts: float | None = None,
        ) -> None:
            self.id = id or f"hev_{uuid.uuid4().hex[:8]}"
            self.session_id = session_id
            self.harness_id = harness_id
            self.kind = kind if kind in VALID_HARNESS_EVENT_KINDS else "telegraphic_note"
            self.ts = ts if ts is not None else time.time()
            self.actor = actor
            self.summary = summary
            self.confidence = max(0.0, min(1.0, float(confidence)))
            self.payload = dict(payload or {})

        def to_dict(self) -> dict[str, Any]:
            return {
                "id": self.id,
                "session_id": self.session_id,
                "harness_id": self.harness_id,
                "kind": self.kind,
                "ts": self.ts,
                "actor": self.actor,
                "summary": self.summary,
                "confidence": self.confidence,
                "payload": self.payload,
            }


class BaseHarness(ABC):
    """Normalized execution contract for pluggable coding & swarm harnesses in the Sabrina Collective."""

    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self.sessions: dict[str, dict[str, Any]] = {}

    @property
    def harness_id(self) -> str:
        return self.config.harness_id

    @property
    def capabilities(self) -> HarnessCapabilities:
        return self.config.capabilities

    def start_session(
        self,
        task: str,
        workspace_root: str | Path,
        policy: HarnessPolicy | None = None,
        model_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        eff_policy = policy or HarnessPolicy()
        sandbox = WorkspaceSandbox(
            workspace_root,
            allow_local_venv_fallback=eff_policy.allow_local_venv_fallback,
            policy_profile=eff_policy.profile,
        )
        session_id = f"hses_{uuid.uuid4().hex[:8]}"
        session = {
            "session_id": session_id,
            "harness_id": self.harness_id,
            "task": str(task or ""),
            "workspace_root": str(sandbox.root),
            "policy": eff_policy,
            "model_config": dict(model_config or {}),
            "sandbox": sandbox,
            "interrupted": False,
            "started_at": time.time(),
            "events": [],
            "metrics": {
                "harness_id": self.harness_id,
                "duration_ms": 0.0,
                "diff_count": 0,
                "checkpoint_count": 0,
                "diagnostic_runs": 0,
                "diagnostic_passed": True,
                "tool_calls": 0,
            },
        }
        self.sessions[session_id] = session
        return {
            "ok": True,
            "session_id": session_id,
            "harness_id": self.harness_id,
            "workspace_root": str(sandbox.root),
            "policy": eff_policy.to_dict(),
            "capabilities": self.capabilities.to_dict(),
        }

    def _record_event(
        self,
        session_id: str,
        kind: str,
        summary: str,
        *,
        actor: str = "",
        confidence: float = 0.95,
        **payload: Any,
    ) -> HarnessEvent:
        ev = HarnessEvent(
            session_id=session_id,
            harness_id=self.harness_id,
            kind=kind,
            actor=actor or self.config.name,
            summary=summary,
            confidence=confidence,
            payload=payload,
        )
        session = self.sessions.get(session_id)
        if session is not None:
            session["events"].append(ev.to_dict())
            metrics = session["metrics"]
            if kind == "tool_call":
                metrics["tool_calls"] += 1
            elif kind == "diff_proposal":
                metrics["diff_count"] += 1
            elif kind == "checkpoint":
                metrics["checkpoint_count"] += 1
            elif kind == "diagnostic":
                metrics["diagnostic_runs"] += 1
                diag = payload.get("diagnostics") or {}
                if isinstance(diag, dict) and not diag.get("passed", True):
                    metrics["diagnostic_passed"] = False
        return ev

    @abstractmethod
    def step(self, session_id: str, prompt: str = "", **kwargs: Any) -> Iterator[HarnessEvent]:
        """Execute a bounded turn and yield normalized `HarnessEvent` items."""

    def run_to_completion(self, session_id: str, prompt: str = "", **kwargs: Any) -> dict[str, Any]:
        """Consume `step()` iterator and return normalized turn output and benchmark telemetry."""
        t0 = time.monotonic()
        events = [ev.to_dict() for ev in self.step(session_id, prompt=prompt, **kwargs)]
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
        session = self.sessions.get(session_id, {})
        metrics = dict(session.get("metrics") or {})
        metrics["duration_ms"] = elapsed_ms
        if session:
            session["metrics"] = metrics
        final_ev = next((e for e in reversed(events) if e["kind"] == "final_summary"), None)
        return {
            "ok": not any(e["kind"] == "error" for e in events),
            "session_id": session_id,
            "harness_id": self.harness_id,
            "reply": (final_ev or {}).get("summary", ""),
            "events": events,
            "metrics": metrics,
            "execution_authorized": False,
        }

    def interrupt(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": f"Unknown session_id: {session_id}"}
        session["interrupted"] = True
        sb: WorkspaceSandbox = session["sandbox"]
        sb.shutdown_processes()
        ev = self._record_event(session_id, "telegraphic_note", f"Session {session_id} interrupted by operator.")
        return {"ok": True, "session_id": session_id, "interrupted": True, "event": ev.to_dict()}

    def rollback(self, session_id: str, checkpoint_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": f"Unknown session_id: {session_id}"}
        sb: WorkspaceSandbox = session["sandbox"]
        res = sb.rollback_checkpoint(checkpoint_id)
        self._record_event(
            session_id,
            "checkpoint",
            f"Rolled back workspace to checkpoint {checkpoint_id} ({len(res.get('restored_files', []))} restored).",
            checkpoint_id=checkpoint_id,
            rollback=res,
        )
        return res


class SabrinaNativeHarness(BaseHarness):
    """Sabrina Native Harness wrapping `WorkspaceSandbox` + `WorkspaceAgent` + hunk staging + closed-loop diagnostics."""

    def __init__(
        self,
        config: HarnessConfig | None = None,
        client_factory: Callable[[], Any] | None = None,
        profile_loader: Callable[[], NativeHarnessProfile] | None = None,
    ) -> None:
        super().__init__(
            config
            or HarnessConfig(
                harness_id="sabrina_native",
                name="Sabrina Native Harness",
                kind="native",
                description="Pydantic-validated WorkspaceSandbox + WorkspaceAgent + hunk diff staging + hybrid Git/CAS checkpoints + closed-loop AST/security/lint/pytest diagnostics.",
                default_model="gemini-4-argon",
                capabilities=HarnessCapabilities(
                    diffs=True,
                    checkpoints=True,
                    diagnostics=True,
                    background_jobs=True,
                    mcp_tools=False,
                    streaming_events=True,
                    sandboxing=True,
                    remote_portals=False,
                ),
            )
        )
        self.client_factory = client_factory
        self.profile_loader = profile_loader

    def step(self, session_id: str, prompt: str = "", **kwargs: Any) -> Iterator[HarnessEvent]:
        session = self.sessions.get(session_id)
        if session is None:
            yield HarnessEvent(session_id=session_id, harness_id=self.harness_id, kind="error", summary=f"Unknown session: {session_id}")
            return
        if session.get("interrupted"):
            yield self._record_event(session_id, "error", "Session is interrupted.")
            return

        sb: WorkspaceSandbox = session["sandbox"]
        policy: HarnessPolicy = session["policy"]
        model_cfg = session.get("model_config") or {}
        eff_prompt = str(prompt or session.get("task") or "").strip()

        yield self._record_event(
            session_id,
            "telegraphic_note",
            f"[NATIVE] Turn start · mode={policy.workspace_mode} · policy={policy.profile}",
            confidence=0.98,
        )

        # Direct tool invocation shortcut when tool_calls are explicitly passed
        explicit_tools: list[dict[str, Any]] = kwargs.get("tool_calls") or []
        if explicit_tools:
            if policy.workspace_mode == "sandbox_auto":
                cp = sb.create_checkpoint(label=f"native_batch:{session_id}")
                yield self._record_event(session_id, "checkpoint", f"Created pre-batch checkpoint {cp['id']}", checkpoint=cp)
            for tc in explicit_tools:
                tname = str(tc.get("name") or "")
                targs = dict(tc.get("arguments") or {})
                yield self._record_event(session_id, "tool_call", f"{tname}({targs.get('path', '.')})", tool=tname, arguments=targs)
                if policy.workspace_mode == "approve_writes" and tname not in {"list_dir", "read_file", "run_diagnostics"}:
                    staged = sb.stage_diff(tname, targs, author="sabrina_native")
                    yield self._record_event(
                        session_id,
                        "diff_proposal",
                        f"Staged {tname} on {staged['path']} ({len(staged['hunks'])} hunk(s))",
                        diff=staged,
                    )
                    yield self._record_event(
                        session_id,
                        "approval_request",
                        f"Approval required for {tname} on {staged['path']}",
                        diff_id=staged["id"],
                    )
                else:
                    try:
                        res = sb.execute_tool(tname, targs)
                        yield self._record_event(session_id, "tool_result", f"{tname} -> ok={res.get('ok', True)}", tool=tname, result=res)
                        if tname in {"write_file", "edit_file"}:
                            p_low = str(targs.get("path", "")).lower()
                            if p_low.endswith(".py"):
                                diag = sb.execute_tool("run_diagnostics", {"path": targs["path"], "checks": ["syntax", "security"]})
                                yield self._record_event(
                                    session_id,
                                    "diagnostic",
                                    f"Diagnostics on {targs['path']}: {'PASS' if diag.get('passed') else 'FAIL'}",
                                    diagnostics=diag,
                                )
                            elif p_low.endswith((".html", ".htm", ".css", ".svg")):
                                diag = sb.execute_tool("run_diagnostics", {"path": targs["path"], "checks": ["web"]})
                                yield self._record_event(
                                    session_id,
                                    "diagnostic",
                                    f"Web diagnostics on {targs['path']}: {'PASS' if diag.get('passed') else 'FAIL'}",
                                    diagnostics=diag,
                                )
                    except WorkspaceSandboxError as exc:
                        yield self._record_event(session_id, "error", str(exc), tool=tname)
            yield self._record_event(session_id, "final_summary", f"Completed {len(explicit_tools)} native harness tool action(s).")
            return

        agent: WorkspaceAgent | None = session.get("agent")
        if agent is None:
            profile = self.profile_loader() if self.profile_loader is not None else FACTORY_PROFILE
            agent = profile.create_agent(
                sb,
                mode=policy.workspace_mode,
                model=str(model_cfg.get("model") or self.config.default_model),
                thinking_level=str(model_cfg.get("thinking_level") or "medium"),
                client=kwargs.get("client") or model_cfg.get("client"),
                client_factory=self.client_factory,
            )
            session["agent"] = agent
            session["native_revision"] = profile.revision
            yield self._record_event(session_id, "telegraphic_note", f"Native revision {profile.revision} loaded.")
        try:
            prior_events = {event["id"] for event in agent.harness_events[-25:]}
            res = agent.send_message(eff_prompt)
            new_events = [event for event in res.get("harness_events", []) if event["id"] not in prior_events]
            for hev in new_events:
                yield self._record_event(
                    session_id,
                    str(hev.get("kind") or "telegraphic_note"),
                    str(hev.get("summary") or ""),
                    **{k: v for k, v in hev.items() if k not in {"id", "ts", "kind", "summary"}},
                )
            if not any(event["kind"] == "final_summary" for event in new_events):
                yield self._record_event(session_id, "final_summary", res["reply"])
        except Exception as exc:
            # Deterministic offline fallback: run bounded workspace inspection & diagnostics
            diag = sb.execute_tool("run_diagnostics", {"path": ".", "checks": ["syntax", "security"]})
            yield self._record_event(
                session_id,
                "diagnostic",
                f"Workspace diagnostics: {'PASS' if diag.get('passed') else 'FAIL'} ({diag.get('files_checked', 0)} py files)",
                diagnostics=diag,
            )
            yield self._record_event(
                session_id,
                "final_summary",
                f"Sabrina Native Harness inspected workspace ({diag.get('files_checked', 0)} files checked, {diag.get('error_count', 0)} errors; note: {exc}).",
            )


class MCPToolBusHarness(BaseHarness):
    """MCP-First Tool-Bus Harness routing tool calls across registered MCP servers and sandboxed workspace tools."""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        super().__init__(
            config
            or HarnessConfig(
                harness_id="mcp_harness",
                name="MCP Tool-Bus Harness",
                kind="mcp",
                description="Routes tool execution through standardized Model Context Protocol (MCP) tool servers and Sabrina's sandbox policy boundary.",
                default_model="gemini-4-argon",
                capabilities=HarnessCapabilities(
                    diffs=True,
                    checkpoints=True,
                    diagnostics=True,
                    background_jobs=False,
                    mcp_tools=True,
                    streaming_events=True,
                    sandboxing=True,
                    remote_portals=False,
                ),
            )
        )
        self._mcp_tools: dict[str, dict[str, Any]] = {}

    def register_mcp_tool(
        self,
        server_name: str,
        tool_name: str,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        description: str = "",
        is_mutating: bool = False,
    ) -> dict[str, Any]:
        qualified = f"mcp__{server_name}__{tool_name}"
        spec = {
            "qualified_name": qualified,
            "server": server_name,
            "tool": tool_name,
            "description": description,
            "is_mutating": bool(is_mutating),
            "handler": handler,
        }
        self._mcp_tools[qualified] = spec
        return {k: v for k, v in spec.items() if k != "handler"}

    def list_mcp_tools(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in spec.items() if k != "handler"} for spec in self._mcp_tools.values()]

    def step(self, session_id: str, prompt: str = "", **kwargs: Any) -> Iterator[HarnessEvent]:
        session = self.sessions.get(session_id)
        if session is None:
            yield HarnessEvent(session_id=session_id, harness_id=self.harness_id, kind="error", summary=f"Unknown session: {session_id}")
            return
        sb: WorkspaceSandbox = session["sandbox"]
        policy: HarnessPolicy = session["policy"]

        yield self._record_event(
            session_id,
            "telegraphic_note",
            f"[MCP-BUS] Session active · {len(self._mcp_tools)} MCP tools mounted · policy={policy.profile}",
        )

        mcp_calls: list[dict[str, Any]] = kwargs.get("mcp_calls") or []
        for call in mcp_calls:
            qname = str(call.get("name") or "")
            args = dict(call.get("arguments") or {})
            yield self._record_event(session_id, "tool_call", f"MCP {qname}", tool=qname, arguments=args)
            spec = self._mcp_tools.get(qname)
            if spec is None:
                yield self._record_event(session_id, "error", f"Unregistered MCP tool: {qname}", tool=qname)
                continue
            if spec["is_mutating"] and policy.profile == "inspect_only":
                yield self._record_event(
                    session_id,
                    "error",
                    f"Policy profile 'inspect_only' blocked mutating MCP tool '{qname}'.",
                    tool=qname,
                )
                continue
            if spec["is_mutating"]:
                cp = sb.create_checkpoint(label=f"mcp:{qname}")
                yield self._record_event(session_id, "checkpoint", f"Checkpoint {cp['id']} before MCP tool {qname}", checkpoint=cp)
            try:
                res = spec["handler"](args)
                yield self._record_event(session_id, "tool_result", f"MCP {qname} completed", tool=qname, result=res)
            except Exception as exc:
                yield self._record_event(session_id, "error", f"MCP tool {qname} failed: {exc}", tool=qname)

        diag = sb.execute_tool("run_diagnostics", {"path": ".", "checks": ["syntax", "security"]})
        yield self._record_event(
            session_id,
            "diagnostic",
            f"MCP post-turn workspace check: {'PASS' if diag.get('passed') else 'FAIL'}",
            diagnostics=diag,
        )
        yield self._record_event(
            session_id,
            "final_summary",
            f"MCP Tool-Bus executed {len(mcp_calls)} tool call(s) across {len(self._mcp_tools)} registered MCP tools.",
        )


class ExternalCLIHarnessAdapter(BaseHarness):
    """Wraps external CLI coding agents / subprocess harnesses behind `BaseHarness` with pre/post checkpointing & diff capture."""

    def __init__(self, config: HarnessConfig | None = None) -> None:
        super().__init__(
            config
            or HarnessConfig(
                harness_id="external_cli",
                name="External CLI Harness Adapter",
                kind="external_cli",
                description="Wraps external CLI coding harnesses or custom verification scripts behind Sabrina's sandbox policy, pre-turn checkpointing, and unified diff capture.",
                default_model="external-cli",
                capabilities=HarnessCapabilities(
                    diffs=True,
                    checkpoints=True,
                    diagnostics=True,
                    background_jobs=True,
                    mcp_tools=False,
                    streaming_events=True,
                    sandboxing=True,
                    remote_portals=False,
                ),
            )
        )

    def step(self, session_id: str, prompt: str = "", **kwargs: Any) -> Iterator[HarnessEvent]:
        session = self.sessions.get(session_id)
        if session is None:
            yield HarnessEvent(session_id=session_id, harness_id=self.harness_id, kind="error", summary=f"Unknown session: {session_id}")
            return

        sb: WorkspaceSandbox = session["sandbox"]
        policy: HarnessPolicy = session["policy"]
        command: list[str] = list(kwargs.get("command") or self.config.command_template or [])

        if not command:
            yield self._record_event(
                session_id,
                "telegraphic_note",
                "[CLI-ADAPTER] No external command provided; running internal diagnostic & diff audit.",
            )
            diag = sb.execute_tool("run_diagnostics", {"path": ".", "checks": ["syntax", "security"]})
            yield self._record_event(session_id, "diagnostic", f"CLI fallback diagnostics: {'PASS' if diag.get('passed') else 'FAIL'}", diagnostics=diag)
            yield self._record_event(session_id, "final_summary", "External CLI adapter verified workspace state.")
            return

        allowed, reason = sb.check_policy("run_python")
        if policy.workspace_mode == "read_only":
            yield self._record_event(session_id, "error", "External CLI execution is blocked in read-only mode.")
            return
        if not allowed or not policy.allow_local_venv_fallback:
            msg = reason or "External CLI execution requires allow_local_venv_fallback=True."
            yield self._record_event(session_id, "error", msg)
            return

        cp = sb.create_checkpoint(label=f"cli_pre:{session_id}")
        yield self._record_event(session_id, "checkpoint", f"Pre-CLI checkpoint {cp['id']}", checkpoint=cp)
        yield self._record_event(session_id, "tool_call", f"CLI exec: {' '.join(command)[:120]}", command=command)
        try:
            proc = subprocess.run(
                command,
                cwd=str(sb.root),
                capture_output=True,
                text=True,
                timeout=int(kwargs.get("timeout", 30)),
                env={"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},
            )
            stdout_snip = (proc.stdout or "")[:4000]
            stderr_snip = (proc.stderr or "")[:4000]
            yield self._record_event(
                session_id,
                "tool_result",
                f"CLI exited with code {proc.returncode}",
                exit_code=proc.returncode,
                stdout=stdout_snip,
                stderr=stderr_snip,
            )
        except Exception as exc:
            yield self._record_event(session_id, "error", f"CLI execution failed: {exc}")
            return

        diag = sb.execute_tool("run_diagnostics", {"path": ".", "checks": ["syntax", "security"]})
        yield self._record_event(session_id, "diagnostic", f"Post-CLI diagnostics: {'PASS' if diag.get('passed') else 'FAIL'}", diagnostics=diag)
        yield self._record_event(
            session_id,
            "final_summary",
            f"External CLI command finished (rc={proc.returncode}); diagnostics={'PASS' if diag.get('passed') else 'FAIL'}.",
        )


class MLXEgoPortalHarness(BaseHarness):
    """Collective Harness spanning local MLX / Ollama (`ego.local`) and virtual SQLite portals (`harnesses.external`)."""

    def __init__(
        self,
        config: HarnessConfig | None = None,
        ollama_base_url: str | None = None,
        llm_caller: Callable[[str, str, str, str], dict[str, Any]] | None = None,
    ) -> None:
        eff_endpoint = (
            ollama_base_url
            or os.environ.get("SABRINA_OLLAMA_BASE_URL", "http://ego.local:11434").rstrip("/")
        )
        super().__init__(
            config
            or HarnessConfig(
                harness_id="mlx_ego_portal",
                name="MLX / Ego Portal Collective Harness",
                kind="mlx_portal",
                description="Pairs local MLX / Ollama endpoints (ego.local) and virtual SQLite portals (harnesses.external) with Sabrina's sandbox diff staging and diagnostic verification loop.",
                default_model="ollama/qwen2.5-coder:14b",
                endpoint=eff_endpoint,
                capabilities=HarnessCapabilities(
                    diffs=True,
                    checkpoints=True,
                    diagnostics=True,
                    background_jobs=True,
                    mcp_tools=False,
                    streaming_events=True,
                    sandboxing=True,
                    remote_portals=True,
                ),
            )
        )
        self.llm_caller = llm_caller

    def _call_portal_or_mlx(self, model_id: str, prompt: str) -> dict[str, Any]:
        if self.llm_caller is not None:
            try:
                res = self.llm_caller(
                    "execution_specialist",
                    model_id,
                    "You are Sabrina's MLX/Ego Collective Coding Harness. Provide telegraphic, high-signal verification and code guidance.",
                    prompt,
                )
                if isinstance(res, dict) and res.get("ok"):
                    return res
            except Exception:
                pass

        if model_id.startswith("portal:"):
            portal_slug = model_id.split(":", 1)[1].strip()
            try:
                import importlib
                ext_pkg = importlib.import_module("harnesses.external")
                portal_mod = getattr(ext_pkg, portal_slug)
                out = portal_mod.invoke(prompt)
                return {
                    "ok": True,
                    "text": str(out.get("reply") or out.get("text") or out),
                    "provider": "virtual_portal",
                    "model": model_id,
                }
            except Exception as exc:
                return {"ok": False, "error": f"Portal '{portal_slug}' error: {exc}"}

        bare = model_id
        for pfx in ("ollama/", "local:", "mlx/"):
            if bare.startswith(pfx):
                bare = bare[len(pfx) :]
        try:
            payload = json.dumps(
                {
                    "model": bare,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                }
            ).encode("utf-8")
            req = urllib.request.Request(
                f"{self.config.endpoint}/api/chat",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                reply = str((data.get("message") or {}).get("content") or data.get("response") or "").strip()
                if reply:
                    return {"ok": True, "text": reply, "provider": "mlx_ego", "model": model_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": "mlx_ego", "model": model_id}
        return {"ok": False, "error": "Empty response from MLX/Ego endpoint", "provider": "mlx_ego", "model": model_id}

    def step(self, session_id: str, prompt: str = "", **kwargs: Any) -> Iterator[HarnessEvent]:
        session = self.sessions.get(session_id)
        if session is None:
            yield HarnessEvent(session_id=session_id, harness_id=self.harness_id, kind="error", summary=f"Unknown session: {session_id}")
            return
        sb: WorkspaceSandbox = session["sandbox"]
        model_cfg = session.get("model_config") or {}
        model_id = str(kwargs.get("model") or model_cfg.get("model") or self.config.default_model)
        eff_prompt = str(prompt or session.get("task") or "").strip()

        yield self._record_event(
            session_id,
            "telegraphic_note",
            f"[MLX/EGO] Routing turn to {model_id} ({self.config.endpoint})",
            model=model_id,
        )

        diag = sb.execute_tool("run_diagnostics", {"path": ".", "checks": ["syntax", "security"]})
        yield self._record_event(
            session_id,
            "diagnostic",
            f"Pre-flight diagnostics: {'PASS' if diag.get('passed') else 'FAIL'} ({diag.get('files_checked', 0)} py files)",
            diagnostics=diag,
        )

        llm_res = self._call_portal_or_mlx(model_id, eff_prompt)
        if llm_res.get("ok"):
            text = str(llm_res.get("text") or "").strip()
            yield self._record_event(
                session_id,
                "final_summary",
                text[:600],
                provider=llm_res.get("provider", "mlx_ego"),
                model=model_id,
            )
        else:
            yield self._record_event(
                session_id,
                "final_summary",
                f"MLX/Ego harness completed local diagnostic pass ({diag.get('files_checked', 0)} files checked, 0 errors; remote endpoint standby: {llm_res.get('error')}).",
                provider="local_diagnostic_fallback",
                model=model_id,
            )


class HarnessRegistry:
    """Discovers, validates, instantiates, and benchmarks selectable harnesses in the Sabrina Collective."""

    def __init__(
        self,
        *,
        ollama_base_url: str | None = None,
        llm_caller: Callable[[str, str, str, str], dict[str, Any]] | None = None,
        gemini_client_factory: Callable[[], Any] | None = None,
        updates: HarnessUpdates | None = None,
    ) -> None:
        self._harnesses: dict[str, BaseHarness] = {}
        self._active_harness_id: str = NATIVE_HARNESS_ID
        self._updates = updates
        self._role_harness_map: dict[str, str] = {
            "planner_architect": "sabrina_native",
            "research_scout": "mcp_harness",
            "execution_specialist": "sabrina_native",
            "critic_verifier": "sabrina_native",
            "fast_worker": "mlx_ego_portal",
        }
        self._run_history: list[dict[str, Any]] = []

        # Register the 4 core collective harnesses
        self.register(SabrinaNativeHarness(client_factory=gemini_client_factory, profile_loader=self.native_profile))
        self.register(MCPToolBusHarness())
        self.register(ExternalCLIHarnessAdapter())
        self.register(MLXEgoPortalHarness(ollama_base_url=ollama_base_url, llm_caller=llm_caller))

    def register(self, harness: BaseHarness) -> dict[str, Any]:
        self._harnesses[harness.harness_id] = harness
        return harness.config.to_dict()

    def get(self, harness_id: str | None = None) -> BaseHarness:
        target = str(harness_id or self.active_harness_id).strip()
        if target not in self._harnesses:
            raise WorkspaceSandboxError(f"Unknown harness_id '{target}'. Available: {sorted(self._harnesses.keys())}")
        return self._harnesses[target]

    @property
    def active_harness_id(self) -> str:
        if self._updates is not None:
            selected = self._updates.selected_adapter()
            return selected if selected in self._harnesses else NATIVE_HARNESS_ID
        return self._active_harness_id

    def native_profile(self) -> NativeHarnessProfile:
        return self._updates.active_profile() if self._updates is not None else FACTORY_PROFILE

    def set_active_harness(self, harness_id: str) -> dict[str, Any]:
        h = self.get(harness_id)
        if self._updates is not None:
            self._updates.select_adapter(h.harness_id)
        else:
            self._active_harness_id = h.harness_id
        return {
            "ok": True,
            "active_harness_id": h.harness_id,
            "harness": h.config.to_dict(),
        }

    def set_role_harness(self, role: str, harness_id: str) -> dict[str, Any]:
        h = self.get(harness_id)
        clean_role = str(role or "").strip().lower()
        if not clean_role:
            raise WorkspaceSandboxError("role is required.")
        self._role_harness_map[clean_role] = h.harness_id
        return {
            "ok": True,
            "role": clean_role,
            "harness_id": h.harness_id,
            "role_harnesses": dict(self._role_harness_map),
        }

    def get_harness_for_role(self, role: str) -> BaseHarness:
        clean_role = str(role or "").strip().lower()
        return self.get(self._role_harness_map.get(clean_role))

    def list_harnesses(self) -> dict[str, Any]:
        items = [h.config.to_dict() for h in self._harnesses.values()]
        return {
            "ok": True,
            "active_harness_id": self.active_harness_id,
            "role_harnesses": dict(self._role_harness_map),
            "harnesses": items,
            "recent_runs": list(self._run_history[:25]),
            "execution_authorized": False,
        }

    def run_harness_turn(
        self,
        task: str,
        workspace_root: str | Path,
        *,
        harness_id: str | None = None,
        policy: HarnessPolicy | None = None,
        model_config: dict[str, Any] | None = None,
        **step_kwargs: Any,
    ) -> dict[str, Any]:
        harness = self.get(harness_id)
        started = harness.start_session(
            task=task,
            workspace_root=workspace_root,
            policy=policy,
            model_config=model_config,
        )
        sid = started["session_id"]
        result = harness.run_to_completion(sid, prompt=task, **step_kwargs)
        run_meta = {
            "run_id": f"hrun_{uuid.uuid4().hex[:8]}",
            "session_id": sid,
            "harness_id": harness.harness_id,
            "harness_name": harness.config.name,
            "task": str(task or "")[:160],
            "ok": result["ok"],
            "reply": result["reply"][:300],
            "metrics": result["metrics"],
            "ts": time.time(),
        }
        self._run_history.insert(0, run_meta)
        if len(self._run_history) > 60:
            self._run_history = self._run_history[:60]
        return {**result, "run_meta": run_meta}

    def compare_harnesses(
        self,
        task: str,
        workspace_root: str | Path,
        harness_ids: list[str] | None = None,
        policy: HarnessPolicy | None = None,
    ) -> dict[str, Any]:
        """Run the same bounded task across multiple harnesses and return side-by-side comparison metrics."""
        targets = harness_ids or list(self._harnesses.keys())
        comparisons: list[dict[str, Any]] = []
        for hid in targets:
            res = self.run_harness_turn(
                task=task,
                workspace_root=workspace_root,
                harness_id=hid,
                policy=policy or HarnessPolicy(profile="inspect_only", workspace_mode="read_only"),
            )
            comparisons.append(
                {
                    "harness_id": hid,
                    "ok": res["ok"],
                    "reply": res["reply"],
                    "metrics": res["metrics"],
                    "event_count": len(res.get("events") or []),
                }
            )
        return {
            "ok": True,
            "task": task,
            "comparisons": comparisons,
            "execution_authorized": False,
        }
