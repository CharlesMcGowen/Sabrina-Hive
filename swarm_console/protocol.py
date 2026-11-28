"""File-backed `.swarm/` protocol, atomic persistence, lock matrix, and path isolation."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VALID_WORKER_STATES = frozenset({"working", "waiting", "done", "blocked"})
VALID_BOARD_TAGS = frozenset({"PASS", "BLOCK", "done"})
VALID_A2A_COMM_TYPES = frozenset(
    {"pass_off", "loop", "critique_gate", "delegate_spawn", "blackboard_sync", "escalate_hitl"}
)
VALID_A2A_CONDITIONS = frozenset(
    {"always", "on_needs_work", "on_pass", "on_low_confidence", "on_error", "custom"}
)

_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9_-]{0,63})")
_TAG_BRACKET_RE = re.compile(r"\[(PASS|BLOCK|done)\]")

# Fail-closed path-isolation patterns covering all 5 excluded path classes:
# 1. outputs/holdout, holdout
# 2. Nov 24-30 (2026-11-24..30 / 20261124..30)
# 3. 2d8acaed
# 4. 14-day / 14day / active 14-day study
# 5. sampled-book
_EXCLUDED_PATH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "holdout",
        re.compile(r"(?:outputs[\\/]holdout|holdout)", re.IGNORECASE),
    ),
    (
        "nov_24_30",
        re.compile(r"2026-?11-?(?:2[4-9]|30)", re.IGNORECASE),
    ),
    (
        "2d8acaed",
        re.compile(r"2d8acaed", re.IGNORECASE),
    ),
    (
        "14_day_study",
        re.compile(r"(?:active[-_\s]+14[-_\s]*day[-_\s]*study|14[-_\s]*day)", re.IGNORECASE),
    ),
    (
        "sampled_book",
        re.compile(r"sampled[-_\s]*book", re.IGNORECASE),
    ),
)


class SwarmProtocolError(Exception):
    """Base error for swarm console protocol violations."""


class PathIsolationError(SwarmProtocolError):
    """Raised when a path touches an excluded or unsafe location."""


class LockConflictError(SwarmProtocolError):
    """Raised when a worker requests a file lock overlapping another active worker."""

    def __init__(self, message: str, conflicts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.conflicts: list[dict[str, Any]] = conflicts or []


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def guard_safe_path(path: str | Path) -> Path:
    """Fail-closed guard blocking excluded holdout, study-window, or artifact paths.

    Checks both the raw input string and the resolved path so symlinks, relative
    traversals (`..`), and mixed slashes cannot bypass the boundary.
    """
    if path is None:
        raise PathIsolationError("Path must not be None.")

    raw = str(path).strip()
    if not raw or "\x00" in raw:
        raise PathIsolationError("Path is empty or contains null bytes.")

    try:
        resolved = Path(raw).resolve()
    except OSError as exc:
        raise PathIsolationError(f"Failed to resolve path {raw!r}: {exc}") from exc

    candidates = (raw.replace("\\", "/"), str(resolved).replace("\\", "/"))
    for candidate in candidates:
        for label, pattern in _EXCLUDED_PATH_PATTERNS:
            if pattern.search(candidate):
                raise PathIsolationError(
                    f"Path blocked by fail-closed isolation guard ({label}): {raw!r}"
                )

    return resolved


def normalize_lock_path(lock_path: str) -> str:
    """Normalize a file or glob lock path for consistent comparison."""
    cleaned = lock_path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    if not cleaned:
        raise SwarmProtocolError("Lock path must not be empty.")
    guard_safe_path(cleaned)
    return cleaned


def _strip_glob_prefix(pattern: str) -> str:
    """Return the concrete directory prefix before any glob metacharacter."""
    parts = pattern.split("/")
    prefix_parts: list[str] = []
    for part in parts:
        if any(ch in part for ch in "*?["):
            break
        prefix_parts.append(part)
    return "/".join(prefix_parts)


def locks_overlap(lock_a: str, lock_b: str) -> bool:
    """Return True if two normalized file/glob locks can touch the same path."""
    a = lock_a.rstrip("/")
    b = lock_b.rstrip("/")
    if not a or not b:
        return False
    if a == b:
        return True
    if fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    if a.startswith(b + "/") or b.startswith(a + "/"):
        return True

    # Handle directory globs such as `pkg/**` vs `pkg/sub/file.py` or `pkg/*`.
    prefix_a = _strip_glob_prefix(a)
    prefix_b = _strip_glob_prefix(b)
    if prefix_a and (b == prefix_a or b.startswith(prefix_a + "/")) and "**" in a:
        return True
    if prefix_b and (a == prefix_b or a.startswith(prefix_b + "/")) and "**" in b:
        return True
    return False


def find_lock_conflicts(
    roster: dict[str, Any],
    worker_name: str,
    candidate_locks: list[str],
) -> list[dict[str, str]]:
    """Return lock conflict records against active workers other than `worker_name`."""
    conflicts: list[dict[str, str]] = []
    workers = roster.get("workers", {})
    for other_name, other_info in workers.items():
        if other_name == worker_name:
            continue
        if other_info.get("state") == "done":
            continue
        for other_lock in other_info.get("locks", []):
            for cand_lock in candidate_locks:
                if locks_overlap(cand_lock, other_lock):
                    conflicts.append(
                        {
                            "worker": worker_name,
                            "requested_lock": cand_lock,
                            "held_by": other_name,
                            "existing_lock": other_lock,
                            "holder_state": str(other_info.get("state", "unknown")),
                        }
                    )
    return conflicts


def compute_lock_matrix(roster: dict[str, Any]) -> dict[str, Any]:
    """Build a summary lock matrix and detect any existing conflicts in `roster`."""
    workers = roster.get("workers", {})
    entries: list[dict[str, Any]] = []
    active_holders: list[tuple[str, str, str]] = []

    for name, info in sorted(workers.items()):
        state = str(info.get("state", "waiting"))
        locks = list(info.get("locks", []))
        entries.append(
            {
                "worker": name,
                "role": info.get("role", "worker"),
                "model": info.get("model", "default"),
                "state": state,
                "locks": locks,
            }
        )
        if state != "done":
            for lock in locks:
                active_holders.append((name, state, lock))

    conflicts: list[dict[str, str]] = []
    for idx, (name_a, state_a, lock_a) in enumerate(active_holders):
        for name_b, state_b, lock_b in active_holders[idx + 1 :]:
            if name_a == name_b:
                continue
            if locks_overlap(lock_a, lock_b):
                conflicts.append(
                    {
                        "worker": name_a,
                        "requested_lock": lock_a,
                        "held_by": name_b,
                        "existing_lock": lock_b,
                        "holder_state": state_b,
                        "worker_state": state_a,
                    }
                )

    return {
        "entries": entries,
        "conflicts": conflicts,
        "has_conflicts": bool(conflicts),
    }


def _atomic_write_text(target: Path, content: str) -> None:
    """Atomically write UTF-8 text to `target` using a same-directory temp file."""
    safe_target = guard_safe_path(target)
    safe_target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = safe_target.parent / f".{safe_target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, safe_target)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def _atomic_write_json(target: Path, payload: Any) -> None:
    """Atomically serialize `payload` as formatted JSON."""
    _atomic_write_text(target, json.dumps(payload, indent=2, sort_keys=False) + "\n")


def _read_json(target: Path, default: Any) -> Any:
    """Read JSON from `target`, returning a deep-copied `default` if absent."""
    safe_target = guard_safe_path(target)
    if not safe_target.exists():
        return json.loads(json.dumps(default))
    with open(safe_target, "r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_mentions(text: str) -> list[str]:
    """Extract unique `@mention` targets in order of appearance."""
    seen: set[str] = set()
    mentions: list[str] = []
    for match in _MENTION_RE.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            mentions.append(name)
    return mentions


def parse_tags(text: str, explicit_tags: list[str] | None = None) -> list[str]:
    """Combine explicit tags and bracketed tags (`[PASS]`, `[BLOCK]`, `[done]`)."""
    seen: set[str] = set()
    tags: list[str] = []
    for raw_tag in explicit_tags or []:
        cleaned = str(raw_tag).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            tags.append(cleaned)
    for match in _TAG_BRACKET_RE.finditer(text):
        tag = match.group(1)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


class SwarmStore:
    """Thread-safe, file-backed manager for a `.swarm/` directory."""

    def __init__(self, swarm_dir: str | Path) -> None:
        self.swarm_dir: Path = guard_safe_path(swarm_dir)
        self._lock = threading.RLock()

    @property
    def plan_path(self) -> Path:
        return self.swarm_dir / "plan.json"

    @property
    def roster_path(self) -> Path:
        return self.swarm_dir / "roster.json"

    @property
    def board_path(self) -> Path:
        return self.swarm_dir / "board.jsonl"

    @property
    def safety_path(self) -> Path:
        return self.swarm_dir / "safety.json"

    def init(
        self,
        title: str = "Swarm Research Console",
        goal: str = "Coordinate swarm workers safely.",
        phase: str = "planning",
        execution_authorized: bool = False,
    ) -> dict[str, Any]:
        """Initialize `.swarm/` files if missing, enforcing safe defaults."""
        with self._lock:
            self.swarm_dir = guard_safe_path(self.swarm_dir)
            self.swarm_dir.mkdir(parents=True, exist_ok=True)

            if not self.plan_path.exists():
                _atomic_write_json(
                    self.plan_path,
                    {
                        "swarm_id": uuid.uuid4().hex[:12],
                        "title": title,
                        "goal": goal,
                        "phase": phase,
                        "threads": [],
                        "updated_at": utc_now_iso(),
                    },
                )
            if not self.roster_path.exists():
                _atomic_write_json(
                    self.roster_path,
                    {
                        "workers": {},
                        "updated_at": utc_now_iso(),
                    },
                )
            if not self.safety_path.exists():
                _atomic_write_json(
                    self.safety_path,
                    {
                        "execution_authorized": bool(execution_authorized),
                        "mode": "read_only_observe" if not execution_authorized else "authorized",
                        "path_isolation": "fail_closed",
                        "updated_at": utc_now_iso(),
                    },
                )
            if not self.board_path.exists():
                _atomic_write_text(self.board_path, "")

            return self.get_state()

    def read_plan(self) -> dict[str, Any]:
        with self._lock:
            return _read_json(
                self.plan_path,
                {
                    "swarm_id": "uninitialized",
                    "title": "Swarm Console",
                    "goal": "",
                    "phase": "planning",
                    "threads": [],
                    "updated_at": utc_now_iso(),
                },
            )

    def write_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = dict(plan)
            updated["updated_at"] = utc_now_iso()
            _atomic_write_json(self.plan_path, updated)
            return updated

    def read_roster(self) -> dict[str, Any]:
        with self._lock:
            return _read_json(
                self.roster_path,
                {"workers": {}, "updated_at": utc_now_iso()},
            )

    def write_roster(self, roster: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = dict(roster)
            updated["updated_at"] = utc_now_iso()
            _atomic_write_json(self.roster_path, updated)
            return updated

    def read_safety(self) -> dict[str, Any]:
        with self._lock:
            data = _read_json(
                self.safety_path,
                {
                    "execution_authorized": False,
                    "mode": "read_only_observe",
                    "path_isolation": "fail_closed",
                    "updated_at": utc_now_iso(),
                },
            )
            if "execution_authorized" not in data:
                data["execution_authorized"] = False
            return data

    def write_safety(self, safety: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            updated = dict(safety)
            updated["execution_authorized"] = bool(updated.get("execution_authorized", False))
            updated["path_isolation"] = "fail_closed"
            updated["updated_at"] = utc_now_iso()
            _atomic_write_json(self.safety_path, updated)
            return updated

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
        """Register or update a worker after validating disjoint file locks."""
        clean_name = name.strip()
        if not clean_name:
            raise SwarmProtocolError("Worker name must not be empty.")
        if state not in VALID_WORKER_STATES:
            raise SwarmProtocolError(
                f"Invalid worker state {state!r}; expected one of {sorted(VALID_WORKER_STATES)}."
            )

        normalized_locks = [normalize_lock_path(item) for item in (locks or [])]

        with self._lock:
            roster = self.read_roster()
            if state != "done" and normalized_locks:
                conflicts = find_lock_conflicts(roster, clean_name, normalized_locks)
                if conflicts:
                    first = conflicts[0]
                    raise LockConflictError(
                        f"Lock conflict for {clean_name!r}: {first['requested_lock']!r} "
                        f"overlaps {first['existing_lock']!r} held by {first['held_by']!r}.",
                        conflicts=conflicts,
                    )

            workers = roster.setdefault("workers", {})
            worker_record = {
                "name": clean_name,
                "role": role.strip() or "worker",
                "model": model.strip() or "default",
                "state": state,
                "locks": normalized_locks,
                "thread_id": thread_id,
                "note": note,
                "updated_at": utc_now_iso(),
            }
            workers[clean_name] = worker_record
            self.write_roster(roster)
            return worker_record

    def update_worker(
        self,
        name: str,
        state: str | None = None,
        note: str | None = None,
        locks: list[str] | None = None,
        role: str | None = None,
        model: str | None = None,
        thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing worker's state, note, or exclusive locks."""
        clean_name = name.strip()
        if not clean_name:
            raise SwarmProtocolError("Worker name must not be empty.")

        with self._lock:
            roster = self.read_roster()
            workers = roster.setdefault("workers", {})
            if clean_name not in workers:
                return self.register_worker(
                    name=clean_name,
                    role=role or "worker",
                    model=model or "default",
                    locks=locks or [],
                    state=state or "waiting",
                    thread_id=thread_id,
                    note=note or "",
                )

            existing = dict(workers[clean_name])
            next_state = state if state is not None else existing.get("state", "waiting")
            if next_state not in VALID_WORKER_STATES:
                raise SwarmProtocolError(
                    f"Invalid worker state {next_state!r}; expected one of {sorted(VALID_WORKER_STATES)}."
                )

            next_locks = (
                [normalize_lock_path(item) for item in locks]
                if locks is not None
                else list(existing.get("locks", []))
            )
            if next_state != "done" and next_locks:
                conflicts = find_lock_conflicts(roster, clean_name, next_locks)
                if conflicts:
                    first = conflicts[0]
                    raise LockConflictError(
                        f"Lock conflict for {clean_name!r}: {first['requested_lock']!r} "
                        f"overlaps {first['existing_lock']!r} held by {first['held_by']!r}.",
                        conflicts=conflicts,
                    )

            existing["state"] = next_state
            existing["locks"] = next_locks
            if note is not None:
                existing["note"] = note
            if role is not None:
                existing["role"] = role.strip() or existing.get("role", "worker")
            if model is not None:
                existing["model"] = model.strip() or existing.get("model", "default")
            if thread_id is not None:
                existing["thread_id"] = thread_id
            existing["updated_at"] = utc_now_iso()

            workers[clean_name] = existing
            self.write_roster(roster)
            return existing

    def read_board(self, since: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
        """Read board messages with `seq > since`, oldest first."""
        with self._lock:
            safe_board = guard_safe_path(self.board_path)
            if not safe_board.exists():
                return []
            messages: list[dict[str, Any]] = []
            with open(safe_board, "r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    msg = json.loads(stripped)
                    if int(msg.get("seq", 0)) > since:
                        messages.append(msg)
            if limit is not None and limit >= 0:
                return messages[-limit:] if limit > 0 else []
            return messages

    def append_board(
        self,
        agent: str,
        text: str,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Append a message atomically to `board.jsonl` with a monotonic `seq`."""
        clean_agent = agent.strip()
        clean_text = text.strip()
        if not clean_agent:
            raise SwarmProtocolError("Board message `agent` must not be empty.")
        if not clean_text:
            raise SwarmProtocolError("Board message `text` must not be empty.")

        with self._lock:
            safe_board = guard_safe_path(self.board_path)
            existing = self.read_board(since=0)
            next_seq = (int(existing[-1]["seq"]) + 1) if existing else 1
            entry = {
                "seq": next_seq,
                "agent": clean_agent,
                "text": clean_text,
                "mentions": parse_mentions(clean_text),
                "tags": parse_tags(clean_text, tags),
                "time": utc_now_iso(),
            }
            lines = [json.dumps(item, separators=(",", ":")) for item in existing]
            lines.append(json.dumps(entry, separators=(",", ":")))
            _atomic_write_text(safe_board, "\n".join(lines) + "\n")
            return entry

    def list_a2a_links(self) -> list[dict[str, Any]]:
        with self._lock:
            plan = self.read_plan()
            raw = plan.get("a2a_links")
            return list(raw) if isinstance(raw, list) else []

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
        clean_src = str(source or "").strip()
        clean_dst = str(target or "").strip()
        if not clean_src or not clean_dst:
            raise SwarmProtocolError("A2A link `from` and `to` nodes must not be empty.")
        clean_comm = str(comm_type or "pass_off").strip().lower()
        if clean_comm not in VALID_A2A_COMM_TYPES:
            raise SwarmProtocolError(
                f"Invalid comm_type {comm_type!r}; expected one of {sorted(VALID_A2A_COMM_TYPES)}."
            )
        default_cond = "on_needs_work" if clean_comm == "loop" else "always"
        clean_cond = str(condition or default_cond).strip().lower()
        if clean_cond not in VALID_A2A_CONDITIONS:
            clean_cond = default_cond
        bounded_loops = max(1, min(25, int(max_loops or 3)))

        with self._lock:
            plan = self.read_plan()
            links: list[dict[str, Any]] = list(plan.get("a2a_links") or [])
            matched_idx = -1
            for idx, existing in enumerate(links):
                if (link_id and existing.get("id") == link_id) or (
                    not link_id
                    and existing.get("from") == clean_src
                    and existing.get("to") == clean_dst
                    and existing.get("comm_type") == clean_comm
                ):
                    matched_idx = idx
                    break
            resolved_id = (
                str(links[matched_idx]["id"])
                if matched_idx >= 0
                else (str(link_id).strip() if link_id else f"a2a_{uuid.uuid4().hex[:10]}")
            )
            record = {
                "id": resolved_id,
                "from": clean_src,
                "to": clean_dst,
                "comm_type": clean_comm,
                "condition": clean_cond,
                "max_loops": bounded_loops,
                "payload_mode": str(payload_mode or "full_envelope").strip(),
                "message_template": str(message_template or "").strip(),
                "updated_at": utc_now_iso(),
            }
            if matched_idx >= 0:
                links[matched_idx] = record
            else:
                links.append(record)
            plan["a2a_links"] = links
            self.write_plan(plan)
            return record

    def delete_a2a_link(self, link_id: str) -> bool:
        clean_id = str(link_id or "").strip()
        if not clean_id:
            return False
        with self._lock:
            plan = self.read_plan()
            links: list[dict[str, Any]] = list(plan.get("a2a_links") or [])
            filtered = [x for x in links if x.get("id") != clean_id]
            if len(filtered) == len(links):
                return False
            plan["a2a_links"] = filtered
            self.write_plan(plan)
            return True

    def get_state(self, board_limit: int = 100) -> dict[str, Any]:
        """Return combined swarm state for the HTTP API and SSE stream."""
        with self._lock:
            plan = self.read_plan()
            roster = self.read_roster()
            safety = self.read_safety()
            all_messages = self.read_board(since=0)
            head_seq = int(all_messages[-1]["seq"]) if all_messages else 0
            recent = all_messages[-board_limit:] if board_limit > 0 else []
            a2a_links = list(plan.get("a2a_links") or []) if isinstance(plan.get("a2a_links"), list) else []
            return {
                "swarm_dir": str(self.swarm_dir),
                "plan": plan,
                "roster": roster,
                "a2a_links": a2a_links,
                "lock_matrix": compute_lock_matrix(roster),
                "safety": safety,
                "board": recent,
                "head_seq": head_seq,
            }

    def state_fingerprint(self) -> tuple[int, int, int, int, int]:
        """Fast mtime-based fingerprint for SSE change detection."""
        def _mtime(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return 0

        board_msgs = self.read_board(since=0)
        head_seq = int(board_msgs[-1]["seq"]) if board_msgs else 0
        return (
            _mtime(self.plan_path),
            _mtime(self.roster_path),
            _mtime(self.safety_path),
            _mtime(self.board_path),
            head_seq,
        )
