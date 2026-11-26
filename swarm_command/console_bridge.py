"""Bridge between Sabrina Swarm Command, `swarm_console`, and `agent_stack/phoenix` OTel."""

from __future__ import annotations

import contextlib
from typing import Any, Callable, Iterator

from swarm_console.client import SwarmClient
from swarm_console.protocol import SwarmProtocolError

from .models import DEFAULT_ROLE_SPECS, AgentRole, HandoffEnvelope
from .role_binding import RoleBindingConfig


class SwarmConsoleBridge:
    """Mirrors Swarm Command routing, handoffs, and artifacts into `.swarm/` for live UI."""

    def __init__(
        self,
        client: SwarmClient,
        enable_otel: bool = False,
        role_bindings: RoleBindingConfig | None = None,
        unlocked_providers_fn: Callable[[], set[str]] | None = None,
    ) -> None:
        self.client = client
        self.enable_otel = enable_otel
        self.role_bindings = role_bindings
        self.unlocked_providers_fn = unlocked_providers_fn
        self._registered_roles: set[str] = set()

    def _resolve_role_model(self, role: str | AgentRole, default_model: str) -> str:
        if self.role_bindings is None:
            return default_model
        try:
            unlocked = self.unlocked_providers_fn() if self.unlocked_providers_fn else None
            resolved = self.role_bindings.resolve_model(role, unlocked_providers=unlocked)
            return str(resolved["selected_model"])
        except Exception:
            return default_model

    def ensure_roster(self) -> None:
        """Ensure all canonical specialist roles are registered in `.swarm/roster.json`."""
        state = self.client.get_state()
        workers_raw = state.get("roster", {}).get("workers", {})
        if isinstance(workers_raw, dict):
            existing = {str(k) for k in workers_raw.keys()}
        elif isinstance(workers_raw, list):
            existing = {
                str(w.get("name"))
                for w in workers_raw
                if isinstance(w, dict) and w.get("name")
            }
        else:
            existing = set()
        for role, spec in DEFAULT_ROLE_SPECS.items():
            name = role.value
            bound_model = self._resolve_role_model(role, spec.model_tier)
            if name not in existing and name not in self._registered_roles:
                try:
                    self.client.register_worker(
                        name=name,
                        role=spec.execution_pattern,
                        model=bound_model,
                        locks=[],
                        state="waiting",
                    )
                except SwarmProtocolError:
                    pass
            self._registered_roles.add(name)

    def sync_goal(self, goal: str, phase: str) -> None:
        self.client.update_goal(goal=goal, phase=phase)

    def on_worker_start(self, agent_name: str, envelope: HandoffEnvelope) -> None:
        self.ensure_roster()
        bound_model = envelope.assigned_model or self._resolve_role_model(agent_name, "mid_tier_fast")
        try:
            self.client.update_worker_state(
                name=agent_name,
                state="working",
                note=f"[{bound_model}] {envelope.objective}"[:120],
            )
        except SwarmProtocolError:
            self.client.register_worker(
                name=agent_name,
                role="worker",
                model=bound_model,
                locks=[],
                state="working",
                note=envelope.objective[:120],
            )
        self.client.post_message(
            agent=envelope.from_agent,
            text=f"@{agent_name} [{envelope.mode}] {envelope.objective}",
        )

    def on_worker_complete(
        self,
        agent_name: str,
        to_agent: str,
        summary: str,
        passed: bool = True,
    ) -> None:
        state = "done" if passed else "blocked"
        tag = "PASS" if passed else "BLOCK"
        try:
            self.client.update_worker_state(name=agent_name, state=state, note=summary[:120])
        except SwarmProtocolError:
            pass
        self.client.post_message(
            agent=agent_name,
            text=f"@{to_agent} [done] [{tag}] {summary}",
            tags=["done", tag],
        )

    @contextlib.contextmanager
    def trace_span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        """Emit an Arize Phoenix OTel span if `enable_otel=True`; never raises on collector failure."""
        if not self.enable_otel:
            yield
            return
        try:
            from agent_stack.phoenix.otel_trace_helper import span as otel_span

            with otel_span(name, attributes=attributes or {}):
                yield
        except Exception:
            yield
