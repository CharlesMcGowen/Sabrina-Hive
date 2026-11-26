"""Scoped tool manifest filtering, idempotent execution, and HITL approval gates."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .blackboard import SessionBlackboard
from .models import HandoffEnvelope


class ToolRuntimeError(Exception):
    """Base error for scoped tool execution."""


class ToolPermissionError(ToolRuntimeError):
    """Raised when an agent attempts to call a tool outside its HandoffEnvelope.allowed_tools."""


class ApprovalDeniedError(ToolRuntimeError):
    """Raised when a human approval gate rejects or blocks a mutating action."""


@dataclass(frozen=True)
class ToolSpec:
    """Registered tool definition."""

    name: str
    handler: Callable[[dict[str, Any]], Any]
    description: str = ""
    is_mutating: bool = False
    requires_approval: bool = False


class ToolRegistry:
    """Central registry of tools available to Sabrina agents."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        handler: Callable[[dict[str, Any]], Any],
        description: str = "",
        is_mutating: bool = False,
        requires_approval: bool = False,
    ) -> None:
        self._tools[name] = ToolSpec(
            name=name,
            handler=handler,
            description=description,
            is_mutating=is_mutating,
            requires_approval=requires_approval,
        )

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._tools.keys())


class ScopedToolManifest:
    """Filtered tool manifest containing only tools permitted by HandoffEnvelope.allowed_tools."""

    def __init__(self, registry: ToolRegistry, allowed_tools: list[str]) -> None:
        allowed_set = set(allowed_tools)
        self._tools: dict[str, ToolSpec] = {}
        for name in allowed_set:
            spec = registry.get(name)
            if spec is not None:
                self._tools[name] = spec

    @property
    def tool_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolPermissionError(
                f"Tool {name!r} is not in the receiving agent's allowed_tools manifest."
            )
        return spec


class ApprovalGate(Protocol):
    """Protocol for human-in-the-loop approval gates."""

    def request_approval(
        self,
        agent: str,
        action: str,
        payload: dict[str, Any],
        risk_level: str = "high",
    ) -> bool:
        """Return True if approved, False if rejected/pending."""
        ...


class CallableApprovalGate:
    """In-process approval gate backed by a Python callable (ideal for tests/local hooks)."""

    def __init__(self, decision_fn: Callable[[str, str, dict[str, Any], str], bool]) -> None:
        self._decision_fn = decision_fn

    def request_approval(
        self,
        agent: str,
        action: str,
        payload: dict[str, Any],
        risk_level: str = "high",
    ) -> bool:
        return bool(self._decision_fn(agent, action, payload, risk_level))


class HttpApprovalGate:
    """HTTP client for `agent_stack/hitl_gate` (`HITL_GATE_URL`, default localhost/LAN)."""

    def __init__(self, base_url: str | None = None, timeout_s: float = 10.0) -> None:
        self.base_url = (
            base_url or os.environ.get("HITL_GATE_URL", "http://127.0.0.1:8890")
        ).rstrip("/")
        self.timeout_s = timeout_s

    def request_approval(
        self,
        agent: str,
        action: str,
        payload: dict[str, Any],
        risk_level: str = "high",
    ) -> bool:
        body = json.dumps(
            {
                "agent": agent,
                "action": action,
                "risk_level": risk_level,
                "payload": payload,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/approvals",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return str(data.get("status", "")).lower() == "approved"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            # Fail-closed: if HITL gate is unreachable, do not auto-approve writes.
            return False


def execute_scoped_tool(
    tool_name: str,
    args: dict[str, Any],
    envelope: HandoffEnvelope,
    blackboard: SessionBlackboard,
    registry: ToolRegistry,
    approval_gate: ApprovalGate | None = None,
    call_idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Execute a tool strictly within the HandoffEnvelope's allowed_tools, idempotency, and HITL rules."""
    manifest = ScopedToolManifest(registry, envelope.allowed_tools)
    spec = manifest.get(tool_name)

    idem_key = call_idempotency_key or f"{envelope.idempotency_key}:{tool_name}"

    if spec.is_mutating:
        existing = blackboard.check_side_effect(idem_key)
        if existing is not None:
            return {
                "status": "replayed_from_ledger",
                "idempotency_key": idem_key,
                "result": existing.get("result"),
            }

    if spec.requires_approval:
        if approval_gate is None:
            raise ApprovalDeniedError(
                f"Tool {tool_name!r} requires a human approval gate, but none is configured."
            )
        approved = approval_gate.request_approval(
            agent=envelope.to_agent,
            action=tool_name,
            payload={"args": args, "handoff_id": envelope.handoff_id, "objective": envelope.objective},
            risk_level="high" if spec.is_mutating else "medium",
        )
        if not approved:
            if spec.is_mutating:
                blackboard.record_side_effect(
                    idempotency_key=idem_key,
                    action=tool_name,
                    status="rejected",
                    result=None,
                )
            raise ApprovalDeniedError(f"Approval gate denied execution of {tool_name!r}.")

    result = spec.handler(args)

    if spec.is_mutating:
        blackboard.record_side_effect(
            idempotency_key=idem_key,
            action=tool_name,
            status="committed",
            result=result,
        )

    return {
        "status": "executed",
        "idempotency_key": idem_key,
        "result": result,
    }
