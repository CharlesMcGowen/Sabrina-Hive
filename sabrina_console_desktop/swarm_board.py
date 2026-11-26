from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

PostKind = Literal["brief", "handoff", "done", "stall", "tool"]

_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.-])@([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class BoardPost:
    seq: int
    ts: float
    author: str
    channel: str
    mentions: tuple[str, ...]
    kind: PostKind
    text: str
    artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "author": self.author,
            "channel": self.channel,
            "mentions": list(self.mentions),
            "kind": self.kind,
            "text": self.text,
            "artifact_refs": list(self.artifact_refs),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BoardPost:
        return cls(
            seq=int(data["seq"]),
            ts=float(data["ts"]),
            author=str(data["author"]),
            channel=str(data.get("channel", "swarm")),
            mentions=tuple(str(m).lower() for m in data.get("mentions", ())),
            kind=data["kind"],  # type: ignore[arg-type]
            text=str(data["text"]),
            artifact_refs=tuple(str(r) for r in data.get("artifact_refs", ())),
        )


@dataclass
class _WorkerCursor:
    worker_id: str
    aliases: set[str]
    last_seen_seq: int = 0
    state: Literal["waiting", "working", "done", "killed"] = "waiting"


class SwarmBoard:
    """Thread-safe shared message board with predicate-based @mention wakeups."""

    def __init__(
        self,
        mission_id: str,
        jsonl_path: Path,
        *,
        on_post: Callable[[BoardPost], None] | None = None,
    ) -> None:
        self.mission_id = mission_id
        self.jsonl_path = Path(jsonl_path)
        self._on_post = on_post

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._posts: list[BoardPost] = []
        self._cursors: dict[str, _WorkerCursor] = {}
        self._closed = False

        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_existing_jsonl()

    def _load_existing_jsonl(self) -> None:
        if not self.jsonl_path.exists():
            return
        with self.jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                post = BoardPost.from_dict(json.loads(raw))
                if self._posts and post.seq <= self._posts[-1].seq:
                    raise ValueError(
                        f"Non-monotonic seq {post.seq} after {self._posts[-1].seq} in {self.jsonl_path}"
                    )
                self._posts.append(post)

    @staticmethod
    def extract_mentions(text: str, known_targets: set[str] | None = None) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for match in _MENTION_RE.finditer(text):
            token = match.group(1).lower()
            if known_targets is not None and token != "all" and token not in known_targets:
                continue
            if token not in seen:
                seen.add(token)
                found.append(token)
        return found

    def register_worker(self, worker_id: str, aliases: list[str] | None = None) -> int:
        wid = worker_id.lower()
        alias_set = {wid}
        if aliases:
            alias_set.update(a.lower() for a in aliases if a)
        with self._lock:
            cursor = self._cursors.get(wid)
            if cursor is None:
                start_seq = self._posts[-1].seq if self._posts else 0
                cursor = _WorkerCursor(
                    worker_id=wid,
                    aliases=alias_set,
                    last_seen_seq=start_seq,
                    state="waiting",
                )
                self._cursors[wid] = cursor
            else:
                cursor.aliases.update(alias_set)
                if cursor.state in ("done", "killed"):
                    cursor.state = "waiting"
            self._cond.notify_all()
            return cursor.last_seen_seq

    def set_worker_state(
        self,
        worker_id: str,
        state: Literal["waiting", "working", "done", "killed"],
    ) -> None:
        wid = worker_id.lower()
        with self._lock:
            cursor = self._cursors.get(wid)
            if cursor is not None:
                cursor.state = state
            self._cond.notify_all()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cond.notify_all()

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._posts[-1].seq if self._posts else 0

    def _known_targets_locked(self) -> set[str]:
        targets: set[str] = set()
        for cursor in self._cursors.values():
            targets.update(cursor.aliases)
        return targets

    def _is_addressed_to_worker_locked(self, post: BoardPost, cursor: _WorkerCursor) -> bool:
        if post.author.lower() == cursor.worker_id:
            return False
        if not post.mentions:
            return cursor.worker_id == "coordinator"
        if "all" in post.mentions:
            return True
        return bool(cursor.aliases.intersection(post.mentions))

    def _unread_for_locked(self, cursor: _WorkerCursor) -> list[BoardPost]:
        if cursor.last_seen_seq >= (self._posts[-1].seq if self._posts else 0):
            return []
        return [
            p
            for p in self._posts
            if p.seq > cursor.last_seen_seq and self._is_addressed_to_worker_locked(p, cursor)
        ]

    def post(
        self,
        *,
        author: str,
        text: str,
        kind: PostKind = "handoff",
        channel: str = "swarm",
        mentions: list[str] | None = None,
        artifact_refs: list[str] | None = None,
    ) -> BoardPost:
        callback: Callable[[BoardPost], None] | None = None
        with self._lock:
            next_seq = (self._posts[-1].seq + 1) if self._posts else 1
            resolved_mentions = (
                [m.lower() for m in mentions]
                if mentions is not None
                else self.extract_mentions(text, self._known_targets_locked())
            )
            board_post = BoardPost(
                seq=next_seq,
                ts=time.time(),
                author=author.lower(),
                channel=channel,
                mentions=tuple(resolved_mentions),
                kind=kind,
                text=text,
                artifact_refs=tuple(artifact_refs or ()),
            )
            self._posts.append(board_post)
            self._append_jsonl_locked(board_post)
            callback = self._on_post
            self._cond.notify_all()

        if callback is not None:
            callback(board_post)
        return board_post

    def _append_jsonl_locked(self, board_post: BoardPost) -> None:
        line = json.dumps(board_post.to_dict(), ensure_ascii=False) + "\n"
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()

    def wait_for_mentions(
        self,
        worker_id: str,
        *,
        timeout: float | None = None,
        mark_working: bool = True,
    ) -> list[BoardPost]:
        wid = worker_id.lower()
        deadline = (time.monotonic() + timeout) if timeout is not None else None

        with self._lock:
            cursor = self._cursors.get(wid)
            if cursor is None:
                raise KeyError(f"Unregistered worker: {worker_id}")
            if cursor.state in ("done", "killed") or self._closed:
                return []

            cursor.state = "waiting"
            self._cond.notify_all()

            def _ready() -> bool:
                if self._closed or cursor.state in ("done", "killed"):
                    return True
                return len(self._unread_for_locked(cursor)) > 0

            while not _ready():
                remaining = None if deadline is None else (deadline - time.monotonic())
                if remaining is not None and remaining <= 0:
                    return []
                self._cond.wait(timeout=remaining)

            if self._closed or cursor.state in ("done", "killed"):
                return []

            unread = self._unread_for_locked(cursor)
            if unread:
                cursor.last_seen_seq = unread[-1].seq
                if mark_working:
                    cursor.state = "working"
            return unread

    def snapshot(self, since_seq: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [p.to_dict() for p in self._posts if p.seq > since_seq]

    def worker_states(self) -> dict[str, str]:
        with self._lock:
            return {wid: c.state for wid, c in self._cursors.items()}

    def all_non_coordinator_waiting(self) -> bool:
        with self._lock:
            workers = [
                c
                for wid, c in self._cursors.items()
                if wid != "coordinator" and c.state not in ("done", "killed")
            ]
            if not workers:
                return False
            return all(c.state == "waiting" for c in workers)
