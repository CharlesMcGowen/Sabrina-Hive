"""Typed models, role specifications, handoff envelopes, and return contracts for Sabrina Swarm Command."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    """Top-level execution modes resolved by Swarm Command."""

    DIRECT_ANSWER = "direct_answer"
    PARALLEL_FAN_OUT = "parallel_fan_out"
    OWNERSHIP_HANDOFF = "ownership_handoff"
    CIRCUIT_BREAKER_SYNTHESIS = "circuit_breaker_synthesis"


class AgentRole(str, Enum):
    """Canonical specialist roles in Sabrina's swarm architecture."""

    SWARM_COMMAND = "swarm_command"
    RESEARCH_SCOUT = "research_scout"
    EXECUTION_SPECIALIST = "execution_specialist"
    PLANNER_ARCHITECT = "planner_architect"
    CRITIC_VERIFIER = "critic_verifier"


class HandoffMode(str, Enum):
    """Delegation mode inside a HandoffEnvelope."""

    HANDOFF = "handoff"
    WORKER_CALL = "worker_call"


class ComplexityClass(str, Enum):
    """Request complexity classification used by the router decision table."""

    SINGLE_STEP = "single_step"
    MULTI_SOURCE_INDEPENDENT = "multi_source_independent"
    STATEFUL_WORKFLOW = "stateful_workflow"


@dataclass(frozen=True)
class AgentRoleSpec:
    """Configuration and budget constraints for a specialist agent role."""

    role: AgentRole
    execution_pattern: str
    model_tier: str
    max_routing_hops: int | None = None
    concurrency_cap: int | None = None
    requires_approval_gate: bool = False
    max_plan_revisions: int | None = None
    max_retries: int = 0
    default_allowed_tools: tuple[str, ...] = ()
    system_prompt: str = ""


DEFAULT_ROLE_SPECS: dict[AgentRole, AgentRoleSpec] = {
    AgentRole.SWARM_COMMAND: AgentRoleSpec(
        role=AgentRole.SWARM_COMMAND,
        execution_pattern="hub",
        model_tier="fast_router",
        max_routing_hops=2,
        default_allowed_tools=("read_state", "spawn_worker", "handoff"),
        system_prompt=(
            "You are Sabrina Swarm Command. Classify intent, enforce budget and policy "
            "guardrails, select direct answer, parallel fan-out, or ownership handoff, "
            "and synthesize verified outputs."
        ),
    ),
    AgentRole.RESEARCH_SCOUT: AgentRoleSpec(
        role=AgentRole.RESEARCH_SCOUT,
        execution_pattern="as_tool",
        model_tier="mid_tier_fast",
        concurrency_cap=4,
        default_allowed_tools=("web_search", "doc_read", "vector_query", "repo_read"),
        system_prompt=(
            "You are a Research & Retrieval Scout. Query sources in an isolated context "
            "window and return cited evidence blocks conforming to ResearchBriefV1."
        ),
    ),
    AgentRole.EXECUTION_SPECIALIST: AgentRoleSpec(
        role=AgentRole.EXECUTION_SPECIALIST,
        execution_pattern="handoff",
        model_tier="reasoning",
        requires_approval_gate=True,
        default_allowed_tools=("repo_read", "repo_patch", "test_runner", "api_call"),
        system_prompt=(
            "You are the Execution & Ops Specialist. Execute code, modify files, and "
            "run workflows with idempotency keys. Respect approval gates on writes and "
            "return to swarm_command upon completion."
        ),
    ),
    AgentRole.PLANNER_ARCHITECT: AgentRoleSpec(
        role=AgentRole.PLANNER_ARCHITECT,
        execution_pattern="handoff",
        model_tier="reasoning",
        max_plan_revisions=1,
        default_allowed_tools=("repo_read", "doc_read", "write_goal_tree"),
        system_prompt=(
            "You are the Planner & State Architect. Decompose goals into dependency "
            "graphs, milestones, and structured checklists in shared state."
        ),
    ),
    AgentRole.CRITIC_VERIFIER: AgentRoleSpec(
        role=AgentRole.CRITIC_VERIFIER,
        execution_pattern="as_tool",
        model_tier="fast_eval",
        max_retries=1,
        default_allowed_tools=("read_state", "schema_validate", "citation_check"),
        system_prompt=(
            "You are the Critic / Verifier. Verify citations, schema validity, tool "
            "output grounding, and policy compliance before final delivery."
        ),
    ),
}


@dataclass(frozen=True)
class BudgetSpec:
    """Per-handoff execution budget."""

    max_steps: int = 5
    max_tokens: int = 16000
    timeout_ms: int = 45000

    def __post_init__(self) -> None:
        if self.max_steps <= 0:
            raise ValueError("BudgetSpec.max_steps must be > 0")
        if self.max_tokens <= 0:
            raise ValueError("BudgetSpec.max_tokens must be > 0")
        if self.timeout_ms <= 0:
            raise ValueError("BudgetSpec.timeout_ms must be > 0")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
            "timeout_ms": self.timeout_ms,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetSpec:
        return cls(
            max_steps=int(data.get("max_steps", 5)),
            max_tokens=int(data.get("max_tokens", 16000)),
            timeout_ms=int(data.get("timeout_ms", 45000)),
        )


def new_id(prefix: str) -> str:
    """Generate a short unique identifier with the given prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class HandoffEnvelope:
    """Typed delegation or ownership-transfer contract between agents."""

    from_agent: str
    to_agent: str
    objective: str
    mode: str = HandoffMode.WORKER_CALL.value
    constraints: list[str] = field(default_factory=list)
    context_refs: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    idempotency_key: str = field(default_factory=lambda: new_id("idem"))
    budget: BudgetSpec = field(default_factory=BudgetSpec)
    return_schema: str = "ResearchBriefV1"
    handoff_id: str = field(default_factory=lambda: new_id("hf"))
    trace_id: str = field(default_factory=lambda: new_id("tr"))
    target_namespace: str | None = None
    assigned_model: str | None = None
    fallback_models: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in {HandoffMode.HANDOFF.value, HandoffMode.WORKER_CALL.value}:
            raise ValueError(f"Invalid handoff mode: {self.mode!r}")
        if not self.from_agent or not self.to_agent:
            raise ValueError("from_agent and to_agent must be non-empty")
        if not self.objective.strip():
            raise ValueError("objective must be non-empty")
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        if self.return_schema not in RETURN_SCHEMAS:
            raise ValueError(f"Unsupported return_schema: {self.return_schema!r}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "handoff_id": self.handoff_id,
            "trace_id": self.trace_id,
            "mode": self.mode,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "objective": self.objective,
            "constraints": list(self.constraints),
            "context_refs": list(self.context_refs),
            "allowed_tools": list(self.allowed_tools),
            "idempotency_key": self.idempotency_key,
            "budget": self.budget.to_dict(),
            "return_schema": self.return_schema,
        }
        if self.target_namespace is not None:
            payload["target_namespace"] = self.target_namespace
        if self.assigned_model is not None:
            payload["assigned_model"] = self.assigned_model
        if self.fallback_models:
            payload["fallback_models"] = list(self.fallback_models)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoffEnvelope:
        return cls(
            handoff_id=str(data.get("handoff_id") or new_id("hf")),
            trace_id=str(data.get("trace_id") or new_id("tr")),
            mode=str(data["mode"]),
            from_agent=str(data["from_agent"]),
            to_agent=str(data["to_agent"]),
            objective=str(data["objective"]),
            constraints=list(data.get("constraints", [])),
            context_refs=list(data.get("context_refs", [])),
            allowed_tools=list(data.get("allowed_tools", [])),
            idempotency_key=str(data.get("idempotency_key") or new_id("idem")),
            budget=BudgetSpec.from_dict(data.get("budget", {})),
            return_schema=str(data.get("return_schema", "ResearchBriefV1")),
            target_namespace=data.get("target_namespace"),
            assigned_model=data.get("assigned_model"),
            fallback_models=list(data.get("fallback_models") or []),
        )


RETURN_SCHEMAS: dict[str, tuple[str, ...]] = {
    "ResearchBriefV1": ("summary", "citations"),
    "ExecutionReportV1": ("status", "actions_taken", "idempotency_key"),
    "PlanSpecV1": ("root_goal", "tasks"),
    "CriticVerdictV1": ("passed", "citations_verified", "schema_valid", "policy_compliant"),
}


class SchemaValidationError(ValueError):
    """Raised when a worker or specialist return payload violates its return_schema."""


def validate_return_payload(schema_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate that a return payload satisfies the declared schema or explicit escalation."""
    if not isinstance(payload, dict):
        raise SchemaValidationError(f"Return payload for {schema_name} must be a dict.")
    if schema_name not in RETURN_SCHEMAS:
        raise SchemaValidationError(f"Unknown return schema: {schema_name!r}")

    # Explicit escalation is always a valid contract exit back to Swarm Command.
    if payload.get("escalation_reason"):
        return payload

    missing = [key for key in RETURN_SCHEMAS[schema_name] if key not in payload]
    if missing:
        raise SchemaValidationError(
            f"Payload for {schema_name} missing required keys: {missing}"
        )

    if schema_name == "ResearchBriefV1" and not isinstance(payload["citations"], list):
        raise SchemaValidationError("ResearchBriefV1.citations must be a list.")
    if schema_name == "ExecutionReportV1" and not isinstance(payload["actions_taken"], list):
        raise SchemaValidationError("ExecutionReportV1.actions_taken must be a list.")
    if schema_name == "PlanSpecV1" and not isinstance(payload["tasks"], list):
        raise SchemaValidationError("PlanSpecV1.tasks must be a list.")
    if schema_name == "CriticVerdictV1" and not isinstance(payload["passed"], bool):
        raise SchemaValidationError("CriticVerdictV1.passed must be a boolean.")

    return payload
