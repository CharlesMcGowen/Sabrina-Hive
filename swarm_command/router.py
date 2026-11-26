"""Swarm Command deterministic control loop, router decision table, fan-out, and handoffs."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from .blackboard import SessionBlackboard
from .console_bridge import SwarmConsoleBridge
from .models import (
    DEFAULT_ROLE_SPECS,
    AgentRole,
    BudgetSpec,
    ComplexityClass,
    ExecutionMode,
    HandoffEnvelope,
    HandoffMode,
    SchemaValidationError,
    new_id,
    validate_return_payload,
)
from .role_binding import RoleBindingConfig
from .runtime import ApprovalGate, ScopedToolManifest, ToolRegistry

MAX_HOPS_CIRCUIT_BREAKER = 6
MAX_SAME_AGENT_LOOPS = 2
RESEARCH_SCOUT_CONCURRENCY_CAP = 4
MAX_CRITIC_RETRIES = 1


class PreflightError(ValueError):
    """Raised when preflight guardrails reject a request."""


@dataclass
class TurnResult:
    """Structured outcome of a single Swarm Command turn."""

    mode: ExecutionMode
    active_owner: str
    response: str
    artifacts_written: list[str] = field(default_factory=list)
    verdict: dict[str, Any] | None = None
    circuit_breaker_triggered: bool = False
    circuit_breaker_reason: str | None = None
    envelopes: list[dict[str, Any]] = field(default_factory=list)


AgentRunner = Callable[
    [HandoffEnvelope, SessionBlackboard, ScopedToolManifest],
    dict[str, Any],
]
ClassifierFn = Callable[[str], ComplexityClass]
DirectAnswerFn = Callable[[str, SessionBlackboard], str]


def _default_classifier(request_text: str) -> ComplexityClass:
    lowered = request_text.lower()
    write_keywords = (
        "refactor",
        "patch",
        "edit",
        "modify",
        "deploy",
        "execute",
        "run test",
        "plan",
        "roadmap",
        "milestone",
        "debug",
        "fix",
    )
    research_keywords = (
        "compare",
        "research",
        "audit",
        "investigate",
        "gather",
        "search",
        "analyze",
        "survey",
    )
    if any(kw in lowered for kw in write_keywords):
        return ComplexityClass.STATEFUL_WORKFLOW
    if any(kw in lowered for kw in research_keywords):
        return ComplexityClass.MULTI_SOURCE_INDEPENDENT
    return ComplexityClass.SINGLE_STEP


def _default_direct_answer(request_text: str, blackboard: SessionBlackboard) -> str:
    return f"[Direct Answer] Processed single-step request: {request_text}"


def _default_research_scout(
    envelope: HandoffEnvelope,
    blackboard: SessionBlackboard,
    manifest: ScopedToolManifest,
) -> dict[str, Any]:
    return {
        "summary": f"Scout findings for: {envelope.objective}",
        "citations": [f"tool_manifest:{','.join(manifest.tool_names) or 'none'}"],
        "evidence_blocks": [{"objective": envelope.objective, "namespace": envelope.target_namespace}],
    }


def _default_execution_specialist(
    envelope: HandoffEnvelope,
    blackboard: SessionBlackboard,
    manifest: ScopedToolManifest,
) -> dict[str, Any]:
    return {
        "status": "complete",
        "actions_taken": [f"Executed objective: {envelope.objective}"],
        "idempotency_key": envelope.idempotency_key,
    }


def _default_planner_architect(
    envelope: HandoffEnvelope,
    blackboard: SessionBlackboard,
    manifest: ScopedToolManifest,
) -> dict[str, Any]:
    return {
        "status": "complete",
        "root_goal": envelope.objective,
        "tasks": [
            {
                "id": "t1",
                "owner": AgentRole.RESEARCH_SCOUT.value,
                "status": "pending",
                "depends_on": [],
            },
            {
                "id": "t2",
                "owner": AgentRole.EXECUTION_SPECIALIST.value,
                "status": "pending",
                "depends_on": ["t1"],
            },
        ],
    }


def _default_critic_verifier(
    envelope: HandoffEnvelope,
    blackboard: SessionBlackboard,
    manifest: ScopedToolManifest,
) -> dict[str, Any]:
    refs = blackboard.resolve_context_refs(envelope.context_refs)
    all_have_citations = bool(refs) and all(
        bool(item.get("citations")) for item in refs.values() if isinstance(item, dict)
    )
    return {
        "passed": all_have_citations,
        "citations_verified": all_have_citations,
        "schema_valid": True,
        "policy_compliant": True,
        "issues": [] if all_have_citations else ["Missing citations in one or more artifacts."],
    }


class SwarmCommand:
    """Central router and deterministic control loop for Sabrina's multi-agent swarm."""

    def __init__(
        self,
        blackboard: SessionBlackboard,
        registry: ToolRegistry | None = None,
        approval_gate: ApprovalGate | None = None,
        console_bridge: SwarmConsoleBridge | None = None,
        classifier_fn: ClassifierFn | None = None,
        direct_answer_fn: DirectAnswerFn | None = None,
        agent_runners: dict[AgentRole, AgentRunner] | None = None,
        role_bindings: RoleBindingConfig | None = None,
        unlocked_providers_fn: Callable[[], set[str]] | None = None,
    ) -> None:
        self.blackboard = blackboard
        self.registry = registry or ToolRegistry()
        self.approval_gate = approval_gate
        self.console_bridge = console_bridge
        self.classifier_fn = classifier_fn or _default_classifier
        self.direct_answer_fn = direct_answer_fn or _default_direct_answer
        self.role_bindings = role_bindings or RoleBindingConfig()
        self.unlocked_providers_fn = unlocked_providers_fn
        if self.console_bridge is not None:
            if self.console_bridge.role_bindings is None:
                self.console_bridge.role_bindings = self.role_bindings
            if self.console_bridge.unlocked_providers_fn is None and unlocked_providers_fn is not None:
                self.console_bridge.unlocked_providers_fn = unlocked_providers_fn
        self.agent_runners: dict[AgentRole, AgentRunner] = {
            AgentRole.RESEARCH_SCOUT: _default_research_scout,
            AgentRole.EXECUTION_SPECIALIST: _default_execution_specialist,
            AgentRole.PLANNER_ARCHITECT: _default_planner_architect,
            AgentRole.CRITIC_VERIFIER: _default_critic_verifier,
        }
        if agent_runners:
            self.agent_runners.update(agent_runners)

    def preflight_guardrails(
        self,
        request_text: str,
        user_permissions: set[str] | None = None,
    ) -> dict[str, Any]:
        """Validate input safety, remaining session budget, and user permissions."""
        if not isinstance(request_text, str) or not request_text.strip():
            raise PreflightError("Request text must be a non-empty string.")
        if "\x00" in request_text:
            raise PreflightError("Request text contains invalid null bytes.")
        if len(request_text) > 32000:
            raise PreflightError("Request text exceeds maximum length (32000 chars).")

        metrics = self.blackboard.get_turn_metrics()
        return {
            "permissions": set(user_permissions or {"read", "write", "execute"}),
            "tokens_remaining": max(
                0, int(metrics["budget_limit"]) - int(metrics["tokens_used"])
            ),
            "hop_count": int(metrics["hop_count"]),
        }

    def check_circuit_breakers(self, target_agent: str | None = None) -> str | None:
        """Return a reason string if any circuit breaker is tripped, else None."""
        metrics = self.blackboard.get_turn_metrics()
        hop_count = int(metrics.get("hop_count", 0))
        tokens_used = int(metrics.get("tokens_used", 0))
        budget_limit = int(metrics.get("budget_limit", 40000))
        loop_counts: dict[str, int] = dict(metrics.get("loop_counts", {}))

        if hop_count >= MAX_HOPS_CIRCUIT_BREAKER:
            return f"hop_count ({hop_count}) >= {MAX_HOPS_CIRCUIT_BREAKER}"
        if tokens_used >= budget_limit:
            return f"tokens_used ({tokens_used}) >= budget_limit ({budget_limit})"
        if target_agent and int(loop_counts.get(target_agent, 0)) >= MAX_SAME_AGENT_LOOPS:
            return f"loop_count on {target_agent} ({loop_counts[target_agent]}) >= {MAX_SAME_AGENT_LOOPS}"
        return None

    def _synthesize_partial_artifacts(self, reason: str, request_text: str) -> TurnResult:
        snap = self.blackboard.snapshot()
        artifacts = snap.get("artifacts", {})
        summaries = [
            f"- {k}: {v.get('summary', '')}"
            for k, v in sorted(artifacts.items())
            if isinstance(v, dict)
        ]
        body = "\n".join(summaries) if summaries else "No partial artifacts recorded."
        return TurnResult(
            mode=ExecutionMode.CIRCUIT_BREAKER_SYNTHESIS,
            active_owner=self.blackboard.active_owner,
            response=(
                f"[Circuit Breaker Fallback: {reason}]\n"
                f"Request: {request_text}\n"
                f"Partial Artifacts:\n{body}"
            ),
            artifacts_written=sorted(artifacts.keys()),
            circuit_breaker_triggered=True,
            circuit_breaker_reason=reason,
        )

    def _select_handoff_role(self, request_text: str) -> AgentRole:
        lowered = request_text.lower()
        if any(kw in lowered for kw in ("plan", "roadmap", "milestone", "checklist", "goal tree")):
            return AgentRole.PLANNER_ARCHITECT
        return AgentRole.EXECUTION_SPECIALIST

    def _build_envelope(
        self,
        role: AgentRole,
        mode: HandoffMode,
        objective: str,
        trace_id: str,
        constraints: list[str] | None = None,
        context_refs: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        target_namespace: str | None = None,
        idempotency_key: str | None = None,
    ) -> HandoffEnvelope:
        spec = DEFAULT_ROLE_SPECS[role]
        schema_map = {
            AgentRole.RESEARCH_SCOUT: "ResearchBriefV1",
            AgentRole.EXECUTION_SPECIALIST: "ExecutionReportV1",
            AgentRole.PLANNER_ARCHITECT: "PlanSpecV1",
            AgentRole.CRITIC_VERIFIER: "CriticVerdictV1",
        }
        unlocked = self.unlocked_providers_fn() if self.unlocked_providers_fn else None
        resolved = self.role_bindings.resolve_model(role, unlocked_providers=unlocked)
        return HandoffEnvelope(
            from_agent=AgentRole.SWARM_COMMAND.value,
            to_agent=role.value,
            mode=mode.value,
            objective=objective,
            constraints=list(constraints or []),
            context_refs=list(context_refs or []),
            allowed_tools=list(allowed_tools if allowed_tools is not None else spec.default_allowed_tools),
            idempotency_key=idempotency_key or new_id("idem"),
            budget=BudgetSpec(),
            return_schema=schema_map[role],
            trace_id=trace_id,
            target_namespace=target_namespace,
            assigned_model=str(resolved["selected_model"]),
            fallback_models=list(resolved["fallback_models"]),
        )

    def _execute_role_runner(
        self,
        role: AgentRole,
        env: HandoffEnvelope,
        manifest: ScopedToolManifest,
    ) -> dict[str, Any]:
        runner = self.agent_runners[role]
        unlocked = self.unlocked_providers_fn() if self.unlocked_providers_fn else None

        def _invoke(candidate_model: str) -> dict[str, Any]:
            env.assigned_model = candidate_model
            return runner(env, self.blackboard, manifest)

        payload, telemetry = self.role_bindings.execute_with_fallback(
            role,
            _invoke,
            unlocked_providers=unlocked,
        )
        env.assigned_model = str(telemetry["selected_model"])
        return payload

    def _run_parallel_fan_out(
        self,
        request_text: str,
        subtasks: list[str],
        trace_id: str,
        constraints: list[str] | None = None,
    ) -> TurnResult:
        if not (2 <= len(subtasks) <= 5):
            raise ValueError("Parallel fan-out requires between 2 and 5 subtasks.")

        if self.console_bridge:
            self.console_bridge.sync_goal(goal=request_text, phase="executing")

        envelopes: list[HandoffEnvelope] = []
        for idx, subtask in enumerate(subtasks, start=1):
            ns = f"findings.t{idx}"
            env = self._build_envelope(
                role=AgentRole.RESEARCH_SCOUT,
                mode=HandoffMode.WORKER_CALL,
                objective=subtask,
                trace_id=trace_id,
                constraints=constraints,
                target_namespace=ns,
            )
            envelopes.append(env)

        written_refs: list[str] = []

        def _exec_worker(env: HandoffEnvelope) -> tuple[str, dict[str, Any]]:
            self.blackboard.record_hop(
                from_agent=env.from_agent,
                to_agent=env.to_agent,
                reason="parallel_fan_out_worker",
                tokens_used=400,
                trace_id=env.trace_id,
                handoff_id=env.handoff_id,
            )
            if self.console_bridge:
                self.console_bridge.on_worker_start(env.to_agent, env)

            manifest = ScopedToolManifest(self.registry, env.allowed_tools)
            payload = self._execute_role_runner(AgentRole.RESEARCH_SCOUT, env, manifest)
            validate_return_payload(env.return_schema, payload)

            ns = env.target_namespace or f"findings.{env.handoff_id}"
            self.blackboard.write_artifact(
                namespace=ns,
                author=env.to_agent,
                summary=str(payload.get("summary", "")),
                citations=list(payload.get("citations", [])),
                data=payload,
                assigned_namespace=ns,
            )
            if self.console_bridge:
                self.console_bridge.on_worker_complete(
                    env.to_agent,
                    AgentRole.SWARM_COMMAND.value,
                    str(payload.get("summary", ""))[:120],
                    passed=True,
                )
            return f"state.artifacts.{ns}", payload

        max_workers = min(RESEARCH_SCOUT_CONCURRENCY_CAP, len(envelopes))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_exec_worker, env) for env in envelopes]
            for fut in as_completed(futures):
                ref, _ = fut.result()
                written_refs.append(ref)

        written_refs.sort()

        # Pre-exit Critic / Verifier gate (max 1 retry).
        verdict: dict[str, Any] = {}
        critic_envelopes: list[HandoffEnvelope] = []
        for attempt in range(MAX_CRITIC_RETRIES + 1):
            critic_env = self._build_envelope(
                role=AgentRole.CRITIC_VERIFIER,
                mode=HandoffMode.WORKER_CALL,
                objective=f"Verify citations, schema, and policy for: {request_text}",
                trace_id=trace_id,
                context_refs=written_refs,
            )
            critic_envelopes.append(critic_env)
            self.blackboard.record_hop(
                from_agent=AgentRole.SWARM_COMMAND.value,
                to_agent=AgentRole.CRITIC_VERIFIER.value,
                reason=f"critic_verification_attempt_{attempt + 1}",
                tokens_used=250,
                trace_id=trace_id,
                handoff_id=critic_env.handoff_id,
            )
            manifest = ScopedToolManifest(self.registry, critic_env.allowed_tools)
            verdict = self._execute_role_runner(
                AgentRole.CRITIC_VERIFIER, critic_env, manifest
            )
            validate_return_payload(critic_env.return_schema, verdict)
            if verdict.get("passed"):
                break

        resolved = self.blackboard.resolve_context_refs(written_refs)
        summary_lines = [
            f"- {ref}: {item.get('summary', '')} (citations: {item.get('citations', [])})"
            for ref, item in sorted(resolved.items())
        ]
        return TurnResult(
            mode=ExecutionMode.PARALLEL_FAN_OUT,
            active_owner=self.blackboard.active_owner,
            response=(
                f"[Parallel Fan-Out Synthesis | Critic passed={verdict.get('passed')}]\n"
                + "\n".join(summary_lines)
            ),
            artifacts_written=[ref.removeprefix("state.artifacts.") for ref in written_refs],
            verdict=verdict,
            envelopes=[e.to_dict() for e in (*envelopes, *critic_envelopes)],
        )

    def _run_ownership_handoff(
        self,
        role: AgentRole,
        request_text: str,
        trace_id: str,
        constraints: list[str] | None = None,
        context_refs: list[str] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> TurnResult:
        breaker = self.check_circuit_breakers(target_agent=role.value)
        if breaker:
            return self._synthesize_partial_artifacts(breaker, request_text)

        env = self._build_envelope(
            role=role,
            mode=HandoffMode.HANDOFF,
            objective=request_text,
            trace_id=trace_id,
            constraints=constraints,
            context_refs=context_refs,
            allowed_tools=allowed_tools,
        )

        # CAS ownership transfer to specialist if Swarm Command currently owns the turn.
        if self.blackboard.active_owner != role.value:
            self.blackboard.compare_and_swap_owner(
                expected_version=self.blackboard.state_version,
                new_owner=role.value,
                reason=f"handoff_to_{role.value}",
                trace_id=trace_id,
                handoff_id=env.handoff_id,
            )

        self.blackboard.record_hop(
            from_agent=AgentRole.SWARM_COMMAND.value,
            to_agent=role.value,
            reason="ownership_handoff",
            tokens_used=600,
            trace_id=trace_id,
            handoff_id=env.handoff_id,
        )

        if self.console_bridge:
            self.console_bridge.sync_goal(goal=request_text, phase="executing")
            self.console_bridge.on_worker_start(role.value, env)

        manifest = ScopedToolManifest(self.registry, env.allowed_tools)
        payload = self._execute_role_runner(role, env, manifest)
        validate_return_payload(env.return_schema, payload)

        ns = f"handoff.{role.value}.{env.handoff_id}"
        self.blackboard.write_artifact(
            namespace=ns,
            author=role.value,
            summary=str(payload.get("summary") or payload.get("root_goal") or env.objective),
            citations=[],
            data=payload,
            assigned_namespace=ns,
        )

        if role == AgentRole.PLANNER_ARCHITECT and "tasks" in payload:
            self.blackboard.update_goal_tree(
                expected_version=self.blackboard.state_version,
                root_goal=str(payload.get("root_goal", request_text)),
                tasks=list(payload["tasks"]),
            )

        status = str(payload.get("status", "complete")).lower()
        escalated = bool(payload.get("escalation_reason"))
        if status == "complete" or escalated:
            self.blackboard.compare_and_swap_owner(
                expected_version=self.blackboard.state_version,
                new_owner=AgentRole.SWARM_COMMAND.value,
                reason="return_to_command_complete" if status == "complete" else "escalation",
                trace_id=trace_id,
                handoff_id=env.handoff_id,
            )

        if self.console_bridge:
            self.console_bridge.on_worker_complete(
                role.value,
                AgentRole.SWARM_COMMAND.value,
                f"status={status}",
                passed=not escalated,
            )

        return TurnResult(
            mode=ExecutionMode.OWNERSHIP_HANDOFF,
            active_owner=self.blackboard.active_owner,
            response=f"[Ownership Handoff -> {role.value} | status={status}] {payload}",
            artifacts_written=[ns],
            envelopes=[env.to_dict()],
        )

    def run_turn(
        self,
        request_text: str,
        subtasks: list[str] | None = None,
        user_permissions: set[str] | None = None,
        complexity_hint: ComplexityClass | None = None,
        constraints: list[str] | None = None,
        context_refs: list[str] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> TurnResult:
        """Execute one turn of Sabrina's Swarm Command control loop."""
        self.preflight_guardrails(request_text, user_permissions=user_permissions)
        trace_id = new_id("tr")

        breaker = self.check_circuit_breakers()
        if breaker:
            return self._synthesize_partial_artifacts(breaker, request_text)

        # If a specialist currently owns the turn, route directly to that owner.
        current_owner = self.blackboard.active_owner
        if current_owner != AgentRole.SWARM_COMMAND.value:
            owner_role = AgentRole(current_owner)
            return self._run_ownership_handoff(
                role=owner_role,
                request_text=request_text,
                trace_id=trace_id,
                constraints=constraints,
                context_refs=context_refs,
                allowed_tools=allowed_tools,
            )

        complexity = complexity_hint or self.classifier_fn(request_text)

        if complexity == ComplexityClass.SINGLE_STEP:
            answer = self.direct_answer_fn(request_text, self.blackboard)
            return TurnResult(
                mode=ExecutionMode.DIRECT_ANSWER,
                active_owner=self.blackboard.active_owner,
                response=answer,
            )

        if complexity == ComplexityClass.MULTI_SOURCE_INDEPENDENT:
            resolved_subtasks = subtasks or [
                f"{request_text} (aspect 1)",
                f"{request_text} (aspect 2)",
            ]
            return self._run_parallel_fan_out(
                request_text=request_text,
                subtasks=resolved_subtasks,
                trace_id=trace_id,
                constraints=constraints,
            )

        target_role = self._select_handoff_role(request_text)
        return self._run_ownership_handoff(
            role=target_role,
            request_text=request_text,
            trace_id=trace_id,
            constraints=constraints,
            context_refs=context_refs,
            allowed_tools=allowed_tools,
        )
