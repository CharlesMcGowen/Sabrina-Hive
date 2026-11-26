"""Durable, versioned Session Blackboard with CAS locking, partitioned writes, and idempotency ledger."""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from swarm_console.protocol import guard_safe_path


class BlackboardError(Exception):
    """Base error for SessionBlackboard operations."""


class VersionConflictError(BlackboardError):
    """Raised when an optimistic-locking CAS update sees a stale state_version."""

    def __init__(self, expected_version: int, actual_version: int) -> None:
        super().__init__(
            f"Blackboard state_version conflict: expected {expected_version}, actual {actual_version}"
        )
        self.expected_version = expected_version
        self.actual_version = actual_version


class PartitionWriteError(BlackboardError):
    """Raised when a worker attempts to write outside its assigned artifact namespace."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


class SessionBlackboard:
    """Thread-safe, durable Session Blackboard persisted to a JSON checkpoint file."""

    def __init__(
        self,
        session_id: str,
        checkpoint_path: str | Path | None = None,
        budget_limit: int = 40000,
    ) -> None:
        self._lock = threading.RLock()
        self.checkpoint_path: Path | None = (
            guard_safe_path(checkpoint_path) if checkpoint_path is not None else None
        )
        if self.checkpoint_path is not None and self.checkpoint_path.exists():
            self._state = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        else:
            self._state = {
                "session_id": session_id,
                "state_version": 1,
                "active_owner": "swarm_command",
                "turn_metrics": {
                    "hop_count": 0,
                    "tokens_used": 0,
                    "budget_limit": int(budget_limit),
                    "loop_counts": {},
                },
                "goal_tree": {
                    "root_goal": "",
                    "tasks": [],
                },
                "artifacts": {},
                "side_effect_ledger": [],
                "audit_trail": [],
            }
            self._persist()

    def _persist(self) -> None:
        if self.checkpoint_path is not None:
            _atomic_write_json(self.checkpoint_path, self._state)

    @property
    def state_version(self) -> int:
        with self._lock:
            return int(self._state["state_version"])

    @property
    def active_owner(self) -> str:
        with self._lock:
            return str(self._state["active_owner"])

    def snapshot(self) -> dict[str, Any]:
        """Return a deep copy of the current blackboard state."""
        with self._lock:
            return copy.deepcopy(self._state)

    def get_turn_metrics(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state["turn_metrics"])

    def record_hop(
        self,
        from_agent: str,
        to_agent: str,
        reason: str,
        tokens_used: int = 0,
        trace_id: str = "",
        handoff_id: str = "",
    ) -> dict[str, Any]:
        """Increment hop_count, loop_counts, and tokens_used, and append to audit_trail."""
        with self._lock:
            metrics = self._state["turn_metrics"]
            metrics["hop_count"] = int(metrics.get("hop_count", 0)) + 1
            metrics["tokens_used"] = int(metrics.get("tokens_used", 0)) + max(0, int(tokens_used))
            loop_counts = dict(metrics.get("loop_counts", {}))
            loop_counts[to_agent] = int(loop_counts.get(to_agent, 0)) + 1
            metrics["loop_counts"] = loop_counts

            entry = {
                "hop": metrics["hop_count"],
                "from": from_agent,
                "to": to_agent,
                "reason": reason,
                "trace_id": trace_id,
                "handoff_id": handoff_id,
                "timestamp": _utc_now_iso(),
            }
            self._state["audit_trail"].append(entry)
            self._state["state_version"] += 1
            self._persist()
            return copy.deepcopy(entry)

    def add_tokens(self, tokens: int) -> int:
        """Add token spend to turn_metrics and return the updated total."""
        with self._lock:
            metrics = self._state["turn_metrics"]
            metrics["tokens_used"] = int(metrics.get("tokens_used", 0)) + max(0, int(tokens))
            self._persist()
            return int(metrics["tokens_used"])

    def compare_and_swap_owner(
        self,
        expected_version: int,
        new_owner: str,
        reason: str = "ownership_transfer",
        trace_id: str = "",
        handoff_id: str = "",
    ) -> int:
        """Atomically transfer active_owner if state_version matches expected_version."""
        with self._lock:
            current_version = int(self._state["state_version"])
            if current_version != expected_version:
                raise VersionConflictError(expected_version, current_version)
            previous_owner = self._state["active_owner"]
            self._state["active_owner"] = new_owner
            self._state["state_version"] = current_version + 1
            self._state["audit_trail"].append(
                {
                    "hop": int(self._state["turn_metrics"].get("hop_count", 0)),
                    "from": previous_owner,
                    "to": new_owner,
                    "reason": reason,
                    "trace_id": trace_id,
                    "handoff_id": handoff_id,
                    "timestamp": _utc_now_iso(),
                }
            )
            self._persist()
            return int(self._state["state_version"])

    def update_goal_tree(
        self,
        expected_version: int,
        root_goal: str | None = None,
        tasks: list[dict[str, Any]] | None = None,
    ) -> int:
        """Atomically update goal_tree using optimistic locking on state_version."""
        with self._lock:
            current_version = int(self._state["state_version"])
            if current_version != expected_version:
                raise VersionConflictError(expected_version, current_version)
            goal_tree = self._state["goal_tree"]
            if root_goal is not None:
                goal_tree["root_goal"] = root_goal
            if tasks is not None:
                goal_tree["tasks"] = copy.deepcopy(tasks)
            self._state["state_version"] = current_version + 1
            self._persist()
            return int(self._state["state_version"])

    def write_artifact(
        self,
        namespace: str,
        author: str,
        summary: str,
        citations: list[str] | None = None,
        data: dict[str, Any] | None = None,
        assigned_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Write a versioned artifact into a partitioned namespace.

        If `assigned_namespace` is provided, enforces that `namespace == assigned_namespace`
        so parallel workers cannot overwrite each other's outputs.
        """
        cleaned = namespace.strip()
        if not cleaned:
            raise PartitionWriteError("Artifact namespace must not be empty.")
        if assigned_namespace is not None and cleaned != assigned_namespace:
            raise PartitionWriteError(
                f"Worker {author!r} attempted write to {cleaned!r}, "
                f"but is partitioned to {assigned_namespace!r}."
            )

        with self._lock:
            artifacts = self._state["artifacts"]
            existing = artifacts.get(cleaned)
            next_version = int(existing.get("version", 0)) + 1 if existing else 1
            record: dict[str, Any] = {
                "version": next_version,
                "author": author,
                "summary": summary,
                "citations": list(citations or []),
                "updated_at": _utc_now_iso(),
            }
            if data is not None:
                record["data"] = copy.deepcopy(data)
            artifacts[cleaned] = record
            self._state["state_version"] += 1
            self._persist()
            return copy.deepcopy(record)

    def resolve_context_refs(self, context_refs: list[str]) -> dict[str, Any]:
        """Resolve `state.*` pointers into shared state without duplicating full chat history."""
        resolved: dict[str, Any] = {}
        with self._lock:
            for ref in context_refs:
                if not ref.startswith("state."):
                    continue
                path = ref[len("state.") :]
                if path == "goal_tree":
                    resolved[ref] = copy.deepcopy(self._state["goal_tree"])
                elif path.startswith("artifacts."):
                    key = path[len("artifacts.") :]
                    if key in self._state["artifacts"]:
                        resolved[ref] = copy.deepcopy(self._state["artifacts"][key])
                elif path.startswith("findings."):
                    # Support both `findings.<id>` keys in artifacts and `state.findings.<id>` aliases.
                    if path in self._state["artifacts"]:
                        resolved[ref] = copy.deepcopy(self._state["artifacts"][path])
        return resolved

    def check_side_effect(self, idempotency_key: str) -> dict[str, Any] | None:
        """Return the committed ledger entry for idempotency_key if it already exists."""
        with self._lock:
            for entry in self._state["side_effect_ledger"]:
                if (
                    entry.get("idempotency_key") == idempotency_key
                    and entry.get("status") == "committed"
                ):
                    return copy.deepcopy(entry)
            return None

    def record_side_effect(
        self,
        idempotency_key: str,
        action: str,
        status: str = "committed",
        result: Any = None,
    ) -> dict[str, Any]:
        """Append a side-effect entry to side_effect_ledger."""
        with self._lock:
            entry: dict[str, Any] = {
                "idempotency_key": idempotency_key,
                "action": action,
                "status": status,
                "timestamp": _utc_now_iso(),
            }
            if result is not None:
                entry["result"] = copy.deepcopy(result)
            self._state["side_effect_ledger"].append(entry)
            self._state["state_version"] += 1
            self._persist()
            return copy.deepcopy(entry)
