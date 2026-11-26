"""m-Eulerian join-cut swarm registry: deduplicated spawns, disjoint regions, bounded recursion.

Invariant map (checked by DeduplicationRegistry.verify_invariants):
  I1  Cantor path uniqueness and disjoint regions. Paths are primary keys. Each child region is
      derived from its parent, contained in it, and disjoint from every non-ancestor active region.
  I2  Task dedup. A task's canonical sha256 is inserted into `dedup` with INSERT OR IGNORE before any
      node is created. A hit refuses the spawn and returns the existing path.
  I3  Join-cut integrity. A parent has at most M children, every node has exactly one parent that
      exists, paths form a tree (no cycles, no orphans), and join prunes whole subtrees while
      tombstoning their hashes.
  I4  Safeguards. max_depth, min_radius and max_active_nodes bound recursion. Every mutation runs in
      one BEGIN IMMEDIATE transaction (SQLite WAL, no Python-level lock). Events are idempotent by id.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

ROOT = "r"
M = 6  # child index 0..M-1, mapped to the six axis directions of an octahedron
DIRECTIONS: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, -1.0),
)
CENTER_OFFSET = 0.5  # child centre sits at 0.5 * parent radius from the parent centre (I1)
CHILD_SCALE = 0.25   # child radius is 0.25 * parent radius, so children fit in the parent (I1)
DEFAULT_MAX_DEPTH = 6
DEFAULT_MIN_RADIUS = 1e-4
DEFAULT_MAX_ACTIVE = 1000
_EPS = 1e-12


class Refused(Exception):
    """A mutation was refused. Its transaction is rolled back."""


@dataclass(frozen=True)
class SpatialRegion:
    x: float
    y: float
    z: float
    r: float

    def centre(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def child(self, index: int) -> "SpatialRegion":
        dx, dy, dz = DIRECTIONS[index]
        return SpatialRegion(
            self.x + CENTER_OFFSET * self.r * dx,
            self.y + CENTER_OFFSET * self.r * dy,
            self.z + CENTER_OFFSET * self.r * dz,
            CHILD_SCALE * self.r,
        )

    def contains(self, other: "SpatialRegion") -> bool:
        return math.dist(self.centre(), other.centre()) + other.r <= self.r + _EPS

    def disjoint(self, other: "SpatialRegion") -> bool:
        # Strict separation: d > r_i + r_j (I1).
        return math.dist(self.centre(), other.centre()) > self.r + other.r + _EPS


@dataclass(frozen=True)
class SwarmNode:
    cantor_path: str
    parent_path: str | None
    task_hash: str
    depth: int
    region: SpatialRegion
    state: str  # ACTIVE | FINALIZED | JOINED
    claimed_by: str | None


@dataclass(frozen=True)
class JoinCutEvent:
    event_id: str
    op: str  # CUT | JOIN | FINALIZE
    path: str
    result: str  # JSON, returned verbatim on replay (idempotency)
    ts: float


@dataclass(frozen=True)
class SpawnResult:
    created: bool
    cantor_path: str | None
    reason: str  # created | duplicate_active | duplicate_tombstoned


@dataclass(frozen=True)
class CutResult:
    ok: bool
    reason: str
    results: tuple[SpawnResult, ...] = field(default_factory=tuple)


def task_hash(payload: Any) -> str:
    """Canonical sha256 of a task payload (I2): key order and whitespace do not change the hash."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _ancestors(path: str) -> list[str]:
    segments = path.split(".")
    return [".".join(segments[:i]) for i in range(1, len(segments))]


class DeduplicationRegistry:
    """SQLite-backed registry. Each mutation is one BEGIN IMMEDIATE transaction; WAL allows parallel readers."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._read() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute(
                "CREATE TABLE IF NOT EXISTS nodes(cantor_path TEXT PRIMARY KEY, parent_path TEXT, "
                "task_hash TEXT UNIQUE NOT NULL, depth INTEGER NOT NULL, x REAL NOT NULL, y REAL NOT NULL, "
                "z REAL NOT NULL, r REAL NOT NULL, state TEXT NOT NULL, claimed_by TEXT)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS dedup(task_hash TEXT PRIMARY KEY, cantor_path TEXT NOT NULL, "
                "state TEXT NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, op TEXT NOT NULL, "
                "path TEXT NOT NULL, result TEXT NOT NULL, ts REAL NOT NULL)"
            )
            exists = c.execute("SELECT 1 FROM nodes WHERE cantor_path = ?", (ROOT,)).fetchone()
            if exists is None:
                root = SpatialRegion(0.0, 0.0, 0.0, 1.0)
                h = task_hash({"root": True})
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT OR IGNORE INTO dedup VALUES(?,?,?)", (h, ROOT, "ACTIVE"))
                c.execute(
                    "INSERT OR IGNORE INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ROOT, None, h, 0, root.x, root.y, root.z, root.r, "ACTIVE", None),
                )
                c.execute("COMMIT")

    # ------------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        """Short-lived connection that is always closed (sqlite3's own context manager does not close)."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @staticmethod
    def _event(c: sqlite3.Connection, event_id: str) -> str | None:
        row = c.execute("SELECT result FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return None if row is None else row["result"]

    @staticmethod
    def _record(c: sqlite3.Connection, event_id: str, op: str, path: str, result: Any) -> None:
        c.execute(
            "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)",
            (event_id, op, path, json.dumps(result, sort_keys=True), time.time()),
        )

    @staticmethod
    def _node(row: sqlite3.Row) -> SwarmNode:
        return SwarmNode(
            cantor_path=row["cantor_path"],
            parent_path=row["parent_path"],
            task_hash=row["task_hash"],
            depth=row["depth"],
            region=SpatialRegion(row["x"], row["y"], row["z"], row["r"]),
            state=row["state"],
            claimed_by=row["claimed_by"],
        )

    # ------------------------------------------------------------------ queries

    def get_node(self, path: str) -> SwarmNode | None:
        with self._read() as c:
            row = c.execute("SELECT * FROM nodes WHERE cantor_path = ?", (path,)).fetchone()
        return None if row is None else self._node(row)

    def active_paths(self) -> list[str]:
        with self._read() as c:
            return [r["cantor_path"] for r in c.execute("SELECT cantor_path FROM nodes WHERE state = 'ACTIVE' ORDER BY cantor_path")]

    def open_parent_paths(self, max_depth: int = DEFAULT_MAX_DEPTH) -> list[str]:
        """Active nodes that can still take a child: below max_depth and with fewer than M children."""
        with self._read() as c:
            rows = c.execute(
                "SELECT p.cantor_path FROM nodes p WHERE p.state = 'ACTIVE' AND p.depth < ? "
                "AND (SELECT COUNT(*) FROM nodes c WHERE c.parent_path = p.cantor_path) < ? "
                "ORDER BY p.cantor_path",
                (max_depth, M),
            ).fetchall()
        return [r["cantor_path"] for r in rows]

    def all_nodes(self) -> list[SwarmNode]:
        with self._read() as c:
            return [self._node(r) for r in c.execute("SELECT * FROM nodes ORDER BY cantor_path")]

    # ------------------------------------------------------------------ mutations

    def cut_branch(
        self,
        parent_path: str,
        payloads: list[Any],
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_active_nodes: int = DEFAULT_MAX_ACTIVE,
        request_id: str | None = None,
    ) -> CutResult:
        """Split an active node into up to M children, one per new task. Atomic: all or nothing (I3, I4).

        Duplicate tasks (I2) are not counted against M. They return a reference to the existing path.
        request_id is the caller's idempotency key: repeating a cut with the same request_id replays the
        stored outcome. Without one, every call is a new request and dedup is by task hash alone.
        """
        hashes = [task_hash(p) for p in payloads]
        event_id = _event_id("CUT", parent_path, request_id) if request_id is not None else None
        try:
            with self._tx() as c:
                prior = self._event(c, event_id) if event_id is not None else None
                if prior is not None:  # replay: return the original outcome without new work (I4)
                    stored = json.loads(prior)
                    return CutResult(True, "replayed", tuple(SpawnResult(**r) for r in stored))
                parent = c.execute("SELECT * FROM nodes WHERE cantor_path = ?", (parent_path,)).fetchone()
                if parent is None:
                    raise Refused(f"no parent {parent_path}")
                if parent["state"] != "ACTIVE":
                    raise Refused(f"parent {parent_path} is {parent['state']}")
                if parent["depth"] + 1 > max_depth:
                    raise Refused(f"max_depth {max_depth} reached")
                active = c.execute("SELECT COUNT(*) FROM nodes WHERE state = 'ACTIVE'").fetchone()[0]
                if active + len(hashes) > max_active_nodes:
                    raise Refused(f"max_active_nodes {max_active_nodes} reached")
                existing = c.execute("SELECT COUNT(*) FROM nodes WHERE parent_path = ?", (parent_path,)).fetchone()[0]
                parent_region = SpatialRegion(parent["x"], parent["y"], parent["z"], parent["r"])
                results: list[SpawnResult] = []
                created = 0
                for h in hashes:
                    index = existing + created
                    candidate = f"{parent_path}.{index}"
                    cur = c.execute("INSERT OR IGNORE INTO dedup VALUES(?,?,?)", (h, candidate, "ACTIVE"))
                    if cur.rowcount == 0:  # CAS lost: the task already exists somewhere (I2)
                        row = c.execute("SELECT cantor_path, state FROM dedup WHERE task_hash = ?", (h,)).fetchone()
                        reason = "duplicate_active" if row["state"] == "ACTIVE" else "duplicate_tombstoned"
                        results.append(SpawnResult(False, row["cantor_path"], reason))
                        continue
                    if index >= M:
                        raise Refused(f"cut exceeds m={M} children under {parent_path}")
                    region = parent_region.child(index)
                    c.execute(
                        "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (candidate, parent_path, h, parent["depth"] + 1, region.x, region.y, region.z,
                         region.r, "ACTIVE", None),
                    )
                    results.append(SpawnResult(True, candidate, "created"))
                    created += 1
                if event_id is not None:
                    self._record(c, event_id, "CUT", parent_path, [r.__dict__ for r in results])
                return CutResult(True, "ok", tuple(results))
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def join_branch(self, path: str, reason: str = "complete") -> CutResult:
        """Prune a subtree back into its parent. Tombstones every hash in it so it is never re-spawned (I3)."""
        event_id = _event_id("JOIN", path)
        try:
            with self._tx() as c:
                prior = self._event(c, event_id)
                if prior is not None:
                    return CutResult(True, "replayed", ())
                if path == ROOT:
                    raise Refused("the root cannot be joined")
                if c.execute("SELECT 1 FROM nodes WHERE cantor_path = ?", (path,)).fetchone() is None:
                    raise Refused(f"no node {path}")
                prefix = path + "."
                subtree = c.execute(
                    "SELECT cantor_path, task_hash, state FROM nodes "
                    "WHERE cantor_path = ? OR substr(cantor_path, 1, ?) = ?",
                    (path, len(prefix), prefix),
                ).fetchall()
                joined = 0
                for row in subtree:
                    if row["state"] == "ACTIVE":
                        c.execute("UPDATE nodes SET state = 'JOINED' WHERE cantor_path = ?", (row["cantor_path"],))
                        joined += 1
                    c.execute("UPDATE dedup SET state = 'TOMBSTONED' WHERE task_hash = ?", (row["task_hash"],))
                self._record(c, event_id, "JOIN", path, {"joined": joined, "reason": reason})
                return CutResult(True, "ok", ())
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def release_leaf(self, path: str, reason: str = "transient") -> CutResult:
        """Free a leaf's spatial volume and its task hash so the task can be spawned again.

        Used for transient infrastructure failures, where tombstoning would kill retries. Only leaves
        can be released, so no other work is pruned. Releasing an absent node is a no-op (idempotent).
        """
        try:
            with self._tx() as c:
                row = c.execute("SELECT state, task_hash FROM nodes WHERE cantor_path = ?", (path,)).fetchone()
                if row is None:
                    return CutResult(True, "absent", ())
                if path == ROOT:
                    raise Refused("the root cannot be released")
                if c.execute("SELECT 1 FROM nodes WHERE parent_path = ?", (path,)).fetchone() is not None:
                    raise Refused(f"{path} has children and cannot be released")
                if row["state"] != "ACTIVE":
                    raise Refused(f"node {path} is {row['state']}")
                c.execute("DELETE FROM dedup WHERE task_hash = ?", (row["task_hash"],))
                c.execute("DELETE FROM nodes WHERE cantor_path = ?", (path,))
                return CutResult(True, reason, ())
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def child_count(self, path: str) -> int:
        with self._read() as c:
            return int(c.execute("SELECT COUNT(*) FROM nodes WHERE parent_path = ?", (path,)).fetchone()[0])

    def claim_node(self, path: str, worker_id: str) -> CutResult:
        """Assign an active node to one worker. Re-claiming by the same worker is a no-op (idempotent)."""
        try:
            with self._tx() as c:
                row = c.execute("SELECT state, claimed_by FROM nodes WHERE cantor_path = ?", (path,)).fetchone()
                if row is None:
                    raise Refused(f"no node {path}")
                if row["state"] != "ACTIVE":
                    raise Refused(f"node {path} is {row['state']}")
                if row["claimed_by"] is None:
                    c.execute("UPDATE nodes SET claimed_by = ? WHERE cantor_path = ?", (worker_id, path))
                elif row["claimed_by"] != worker_id:
                    raise Refused(f"node {path} already claimed by {row['claimed_by']}")
                return CutResult(True, "ok", ())
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def finalize_node(self, path: str) -> CutResult:
        """Mark an active node complete and tombstone its hash. Finalized nodes keep their record (I3, I4)."""
        event_id = _event_id("FINALIZE", path)
        try:
            with self._tx() as c:
                row = c.execute("SELECT state, task_hash FROM nodes WHERE cantor_path = ?", (path,)).fetchone()
                if row is None:
                    raise Refused(f"no node {path}")
                if row["state"] == "FINALIZED":
                    return CutResult(True, "replayed", ())
                if row["state"] != "ACTIVE":
                    raise Refused(f"node {path} is {row['state']}")
                c.execute("UPDATE nodes SET state = 'FINALIZED' WHERE cantor_path = ?", (path,))
                c.execute("UPDATE dedup SET state = 'TOMBSTONED' WHERE task_hash = ?", (row["task_hash"],))
                self._record(c, event_id, "FINALIZE", path, {})
                return CutResult(True, "ok", ())
        except Refused as exc:
            return CutResult(False, str(exc), ())

    # ------------------------------------------------------------------ invariants

    def verify_invariants(
        self,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        min_radius: float = DEFAULT_MIN_RADIUS,
        max_active_nodes: int = DEFAULT_MAX_ACTIVE,
    ) -> list[str]:
        """Return every violated invariant. An empty list means the topology is valid."""
        errors: list[str] = []
        nodes = {n.cantor_path: n for n in self.all_nodes()}
        with self._read() as c:
            dedup = {r["task_hash"]: (r["cantor_path"], r["state"]) for r in c.execute("SELECT * FROM dedup")}

        roots = [p for p, n in nodes.items() if n.parent_path is None]
        if roots != [ROOT]:
            errors.append(f"I3 expected exactly one root {ROOT!r}, found {roots}")

        child_count: dict[str, int] = {}
        for p, n in nodes.items():
            if n.parent_path is None:
                continue
            parent = nodes.get(n.parent_path)
            if parent is None:
                errors.append(f"I3 orphan {p}: parent {n.parent_path} missing")
                continue
            if p.rsplit(".", 1)[0] != n.parent_path:
                errors.append(f"I3 path {p} does not extend parent {n.parent_path}")
            if n.depth != parent.depth + 1:
                errors.append(f"I3 depth of {p} is {n.depth}, expected {parent.depth + 1}")
            index = int(p.rsplit(".", 1)[1])
            if not 0 <= index < M:
                errors.append(f"I3 child index {index} at {p} outside 0..{M - 1}")
            child_count[n.parent_path] = child_count.get(n.parent_path, 0) + 1
            if not parent.region.contains(n.region):
                errors.append(f"I1 {p} is not contained in its parent {n.parent_path}")
            if n.depth > max_depth:
                errors.append(f"budget: {p} depth {n.depth} exceeds max_depth {max_depth}")
            if n.region.r < min_radius:
                errors.append(f"budget: {p} radius {n.region.r} below min_radius {min_radius}")
        for parent_path, count in child_count.items():
            if count > M:
                errors.append(f"I3 {parent_path} has {count} children, more than m={M}")

        for p in nodes:  # no cycles: walk parent links, which must reach the root in bounded steps
            seen: set[str] = set()
            cur: str | None = p
            while cur is not None:
                if cur in seen or len(seen) > len(nodes):
                    errors.append(f"I3 cycle through {p}")
                    break
                seen.add(cur)
                cur = nodes[cur].parent_path if cur in nodes else None

        active = [p for p, n in nodes.items() if n.state == "ACTIVE"]
        for p in active:
            for anc in _ancestors(p):
                if anc in nodes and nodes[anc].state == "JOINED":
                    errors.append(f"I3 active {p} sits under joined ancestor {anc}")
        for i, p in enumerate(active):
            for q in active[i + 1:]:
                if p in _ancestors(q) or q in _ancestors(p):
                    continue  # nested in the same lineage is containment, checked above
                if not nodes[p].region.disjoint(nodes[q].region):
                    errors.append(f"I1 active regions overlap: {p} and {q}")

        for p in active:
            h = nodes[p].task_hash
            if dedup.get(h) != (p, "ACTIVE"):
                errors.append(f"I2 active {p} is not registered as ACTIVE in dedup")
        for h, (path, state) in dedup.items():
            if state == "ACTIVE" and (path not in nodes or nodes[path].state != "ACTIVE"):
                errors.append(f"I2 dedup entry {path} is ACTIVE but its node is not")

        if len(active) > max_active_nodes:
            errors.append(f"budget: {len(active)} active nodes exceeds max_active_nodes {max_active_nodes}")
        return errors
