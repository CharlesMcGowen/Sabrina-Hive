"""Python client helper and demo seeder for `.swarm/` research consoles."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .protocol import SwarmStore, guard_safe_path


class SwarmClient:
    """High-level helper for researchers and agents interacting with `.swarm/`."""

    def __init__(self, swarm_dir: str | Path, auto_init: bool = False) -> None:
        self.store = SwarmStore(guard_safe_path(swarm_dir))
        if auto_init:
            self.store.init()

    @property
    def swarm_dir(self) -> Path:
        return self.store.swarm_dir

    def init_swarm(
        self,
        title: str = "Swarm Research Console",
        goal: str = "Coordinate swarm workers safely.",
        phase: str = "planning",
        execution_authorized: bool = False,
    ) -> dict[str, Any]:
        """Initialize `.swarm/` files with `execution_authorized: False` by default."""
        return self.store.init(
            title=title,
            goal=goal,
            phase=phase,
            execution_authorized=execution_authorized,
        )

    def register_worker(
        self,
        name: str,
        role: str = "worker",
        model: str = "default",
        locks: list[str] | None = None,
        state: str = "waiting",
        thread_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Register a worker with exclusive disjoint file locks."""
        return self.store.register_worker(
            name=name,
            role=role,
            model=model,
            locks=locks,
            state=state,
            thread_id=thread_id,
            note=note,
        )

    def update_worker_state(
        self,
        name: str,
        state: str,
        note: str | None = None,
        locks: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update worker state (`working`/`waiting`/`done`/`blocked`) and optional note/locks."""
        return self.store.update_worker(
            name=name,
            state=state,
            note=note,
            locks=locks,
        )

    def post_message(
        self,
        agent: str,
        text: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Post an `@mention` message with optional tags (`PASS`/`BLOCK`/`done`)."""
        return self.store.append_board(agent=agent, text=text, tags=tags)

    def read_board(self, since: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """Read board messages strictly after `since`."""
        return self.store.read_board(since=since, limit=limit)

    def update_goal(
        self,
        goal: str,
        phase: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Update the swarm's top-level goal, phase, or title."""
        plan = self.store.read_plan()
        plan["goal"] = goal
        if phase is not None:
            plan["phase"] = phase
        if title is not None:
            plan["title"] = title
        return self.store.write_plan(plan)

    def upsert_thread(
        self,
        thread_id: str,
        title: str,
        outcome: str,
        status: str = "active",
        workers: list[str] | None = None,
        summary: str = "",
    ) -> dict[str, Any]:
        """Create or update an outcome-grouped thread in `plan.json`."""
        plan = self.store.read_plan()
        threads: list[dict[str, Any]] = list(plan.get("threads", []))
        record = {
            "id": thread_id,
            "title": title,
            "outcome": outcome,
            "status": status,
            "workers": list(workers or []),
            "summary": summary,
        }
        replaced = False
        for idx, existing in enumerate(threads):
            if existing.get("id") == thread_id:
                threads[idx] = record
                replaced = True
                break
        if not replaced:
            threads.append(record)
        plan["threads"] = threads
        return self.store.write_plan(plan)

    def list_a2a_links(self) -> list[dict[str, Any]]:
        """Return configured A2A workflow links from `plan.json`."""
        return self.store.list_a2a_links()

    def upsert_a2a_link(
        self,
        source: str,
        target: str,
        comm_type: str = "pass_off",
        condition: str | None = None,
        max_loops: int = 3,
        payload_mode: str = "full_envelope",
        message_template: str = "",
        link_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or update an agent-to-agent workflow link (`pass_off`, `loop`, `critique_gate`, etc.)."""
        return self.store.upsert_a2a_link(
            source=source,
            target=target,
            comm_type=comm_type,
            condition=condition,
            max_loops=max_loops,
            payload_mode=payload_mode,
            message_template=message_template,
            link_id=link_id,
        )

    def delete_a2a_link(self, link_id: str) -> bool:
        """Delete an A2A workflow link by ID."""
        return self.store.delete_a2a_link(link_id)

    def get_state(self) -> dict[str, Any]:
        """Return combined plan, roster, lock matrix, safety, and board state."""
        return self.store.get_state()


def seed_demo_swarm(swarm_dir: str | Path, reset: bool = True) -> Path:
    """Populate a realistic demo `.swarm/` directory for `--demo` inspection."""
    safe_dir = guard_safe_path(swarm_dir)
    if reset and safe_dir.exists():
        shutil.rmtree(safe_dir)

    client = SwarmClient(safe_dir)
    client.init_swarm(
        title="Hydrogen Patch Swarm — Console Demo",
        goal="Build zero-dependency swarm_console/ with fail-closed path isolation and live SSE UI.",
        phase="executing",
        execution_authorized=False,
    )

    client.upsert_thread(
        thread_id="thread-protocol",
        title="Protocol & Path Isolation",
        outcome="Atomic .swarm/ persistence + fail-closed guard",
        status="done",
        workers=["protocol-agent"],
        summary="protocol.py implements atomic writes, disjoint lock checks, and 5-class path guard.",
    )
    client.upsert_thread(
        thread_id="thread-console",
        title="HTTP + SSE Research Console",
        outcome="Live dark-mode UI & incremental board API",
        status="active",
        workers=["server-agent", "ui-agent"],
        summary="ThreadingHTTPServer serves /api/state, /api/board, /events, and ui.html.",
    )
    client.upsert_thread(
        thread_id="thread-verify",
        title="Verification & Safety Audit",
        outcome="Stdlib unittest suite & read-only safety strip",
        status="blocked",
        workers=["verifier-agent"],
        summary="Waiting on final UI integration check before marking PASS.",
    )

    client.register_worker(
        name="protocol-agent",
        role="Protocol & Safety",
        model="gemini-2.5-pro",
        locks=["swarm_console/protocol.py", "swarm_console/__init__.py"],
        state="done",
        thread_id="thread-protocol",
        note="Atomic store and guard_safe_path verified.",
    )
    client.register_worker(
        name="server-agent",
        role="HTTP & SSE Backend",
        model="gemini-2.5-pro",
        locks=["swarm_console/server.py", "swarm_console/client.py"],
        state="working",
        thread_id="thread-console",
        note="Streaming SSE /events and handling POST /api/worker.",
    )
    client.register_worker(
        name="ui-agent",
        role="Console Frontend",
        model="gemini-2.5-flash",
        locks=["swarm_console/ui.html"],
        state="waiting",
        thread_id="thread-console",
        note="Awaiting SSE payload schema confirmation.",
    )
    client.register_worker(
        name="verifier-agent",
        role="Verification",
        model="gemini-2.5-pro",
        locks=["swarm_console/test_swarm_console.py"],
        state="blocked",
        thread_id="thread-verify",
        note="Holding for server-agent final endpoint check.",
    )

    client.post_message(
        agent="coordinator",
        text="@all Swarm initialized with execution_authorized: false. Keep file locks disjoint.",
    )
    client.post_message(
        agent="protocol-agent",
        text="@coordinator [done] [PASS] protocol.py landed with atomic writes and 5-class path guard.",
        tags=["done", "PASS"],
    )
    client.post_message(
        agent="server-agent",
        text="@ui-agent /api/state and /events SSE payload ready; @verifier-agent please check 409 conflict path.",
    )
    client.post_message(
        agent="verifier-agent",
        text="@server-agent [BLOCK] Waiting on final /api/board?since=<seq> smoke check before PASS.",
        tags=["BLOCK"],
    )
    return safe_dir
