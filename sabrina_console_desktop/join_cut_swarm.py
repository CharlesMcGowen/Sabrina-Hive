"""m-Eulerian join-cut swarm registry: recursive branching, output folding, branch budgets, and shared blackboard dedup.

Invariant map (checked by DeduplicationRegistry.verify_invariants):
  I1  Cantor path uniqueness and disjoint regions. Paths are primary keys. Each child region is
      derived from its parent, contained in it, and disjoint from every non-ancestor active region.
  I2  Task dedup. A task's canonical sha256 is inserted into `dedup` with INSERT OR IGNORE before any
      node is created. A hit refuses the spawn and returns the existing path.
  I3  Join-cut integrity. A parent has at most M occupied child regions, every node has one parent that
      exists, paths form a tree (no cycles, no orphans), and join folds completed outputs back into
      the parent while pruning subtrees and tombstoning their hashes.
  I4  Safeguards & budgets. max_depth, min_radius, max_active_nodes, and per-branch budget splits
      bound recursion. Every mutation runs in one BEGIN IMMEDIATE transaction (SQLite WAL, busy_timeout,
      coexisting safely in dnd_rsi_blackboard.sqlite3). Events are idempotent by request_id / event_id.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from collections import deque
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
DEFAULT_ROOT_BUDGET = 1000.0
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
    budget_allocated: float = 0.0
    budget_spent: float = 0.0
    output: Any = None
    folded_outputs: tuple[dict[str, Any], ...] = ()

    @property
    def budget_remaining(self) -> float:
        return max(0.0, self.budget_allocated - self.budget_spent)


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
    budget_allocated: float = 0.0


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


def _json_loads_safe(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


class DeduplicationRegistry:
    """SQLite-backed registry. Runs in WAL mode so it can share dnd_rsi_blackboard.sqlite3 safely."""

    def __init__(self, db_path: Path | str, *, initial_budget: float = DEFAULT_ROOT_BUDGET) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_budget = max(0.0, float(initial_budget))
        with self._read() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=5000")
            c.execute(
                "CREATE TABLE IF NOT EXISTS nodes("
                "cantor_path TEXT PRIMARY KEY, parent_path TEXT, "
                "task_hash TEXT UNIQUE NOT NULL, depth INTEGER NOT NULL, "
                "x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL, r REAL NOT NULL, "
                "state TEXT NOT NULL, claimed_by TEXT, "
                "budget_allocated REAL NOT NULL DEFAULT 0.0, "
                "budget_spent REAL NOT NULL DEFAULT 0.0, "
                "output_json TEXT, "
                "folded_outputs_json TEXT NOT NULL DEFAULT '[]')"
            )
            # Non-destructive migration for existing DBs created before budget/output columns
            cols = {r["name"] for r in c.execute("PRAGMA table_info(nodes)").fetchall()}
            if "budget_allocated" not in cols:
                c.execute("ALTER TABLE nodes ADD COLUMN budget_allocated REAL NOT NULL DEFAULT 0.0")
            if "budget_spent" not in cols:
                c.execute("ALTER TABLE nodes ADD COLUMN budget_spent REAL NOT NULL DEFAULT 0.0")
            if "output_json" not in cols:
                c.execute("ALTER TABLE nodes ADD COLUMN output_json TEXT")
            if "folded_outputs_json" not in cols:
                c.execute("ALTER TABLE nodes ADD COLUMN folded_outputs_json TEXT NOT NULL DEFAULT '[]'")

            c.execute(
                "CREATE TABLE IF NOT EXISTS dedup(task_hash TEXT PRIMARY KEY, cantor_path TEXT NOT NULL, "
                "state TEXT NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, op TEXT NOT NULL, "
                "path TEXT NOT NULL, result TEXT NOT NULL, ts REAL NOT NULL)"
            )
            exists = c.execute("SELECT budget_allocated FROM nodes WHERE cantor_path = ?", (ROOT,)).fetchone()
            if exists is None:
                root = SpatialRegion(0.0, 0.0, 0.0, 1.0)
                h = task_hash({"root": True})
                c.execute("BEGIN IMMEDIATE")
                c.execute("INSERT OR IGNORE INTO dedup VALUES(?,?,?)", (h, ROOT, "ACTIVE"))
                c.execute(
                    "INSERT OR IGNORE INTO nodes("
                    "cantor_path, parent_path, task_hash, depth, x, y, z, r, state, claimed_by, "
                    "budget_allocated, budget_spent, output_json, folded_outputs_json"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ROOT, None, h, 0, root.x, root.y, root.z, root.r, "ACTIVE", None,
                     self.initial_budget, 0.0, None, "[]"),
                )
                c.execute("COMMIT")
            elif float(exists["budget_allocated"] or 0.0) == 0.0 and self.initial_budget > 0.0:
                c.execute("BEGIN IMMEDIATE")
                c.execute("UPDATE nodes SET budget_allocated = ? WHERE cantor_path = ? AND budget_allocated = 0.0",
                          (self.initial_budget, ROOT))
                c.execute("COMMIT")

    # ------------------------------------------------------------------ plumbing

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
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
            (event_id, op, path, json.dumps(result, sort_keys=True, default=str), time.time()),
        )

    @staticmethod
    def _node(row: sqlite3.Row) -> SwarmNode:
        folded = _json_loads_safe(row["folded_outputs_json"], [])
        if not isinstance(folded, list):
            folded = []
        return SwarmNode(
            cantor_path=row["cantor_path"],
            parent_path=row["parent_path"],
            task_hash=row["task_hash"],
            depth=row["depth"],
            region=SpatialRegion(row["x"], row["y"], row["z"], row["r"]),
            state=row["state"],
            claimed_by=row["claimed_by"],
            budget_allocated=float(row["budget_allocated"] or 0.0),
            budget_spent=float(row["budget_spent"] or 0.0),
            output=_json_loads_safe(row["output_json"], None),
            folded_outputs=tuple(item for item in folded if isinstance(item, dict)),
        )

    @staticmethod
    def _free_child_indices(c: sqlite3.Connection, parent_path: str) -> list[int]:
        prefix = parent_path + "."
        # The first numeric segment identifies the geometric slot. Even a finalized
        # child still occupies it while any descendant is active.
        rows = c.execute(
            "SELECT CAST(substr(cantor_path, ?) AS INTEGER) AS slot, MAX(state = 'ACTIVE') AS occupied "
            "FROM nodes WHERE parent_path = ? OR (state = 'ACTIVE' AND instr(cantor_path, ?) = 1) "
            "GROUP BY slot", (len(prefix) + 1, parent_path, prefix),
        ).fetchall()
        allocated = {row["slot"] for row in rows}
        occupied = {row["slot"] for row in rows if row["occupied"]}
        return sorted((i for i in range(M) if i not in occupied), key=allocated.__contains__)

    @staticmethod
    def _find_open_parent_in_tx(
        c: sqlite3.Connection,
        preferred_parent: str,
        *,
        slots_needed: int,
        max_depth: int,
        min_radius: float,
    ) -> sqlite3.Row | None:
        """BFS from preferred_parent to find an ACTIVE node with enough free child slots."""
        queue: deque[str] = deque([preferred_parent])
        visited: set[str] = set()
        while queue:
            cur_path = queue.popleft()
            if cur_path in visited:
                continue
            visited.add(cur_path)
            row = c.execute("SELECT * FROM nodes WHERE cantor_path = ?", (cur_path,)).fetchone()
            if row is None or row["state"] != "ACTIVE":
                continue
            if row["depth"] + 1 <= max_depth and (float(row["r"]) * CHILD_SCALE) >= min_radius:
                free = DeduplicationRegistry._free_child_indices(c, cur_path)
                if len(free) >= slots_needed:
                    return row
            children = c.execute(
                "SELECT cantor_path FROM nodes WHERE parent_path = ? AND state = 'ACTIVE' ORDER BY cantor_path",
                (cur_path,),
            ).fetchall()
            for child in children:
                queue.append(child["cantor_path"])
        return None

    # ------------------------------------------------------------------ queries & shared blackboard reads

    def get_node(self, path: str) -> SwarmNode | None:
        with self._read() as c:
            row = c.execute("SELECT * FROM nodes WHERE cantor_path = ?", (path,)).fetchone()
        return None if row is None else self._node(row)

    def active_paths(self) -> list[str]:
        with self._read() as c:
            return [r["cantor_path"] for r in c.execute("SELECT cantor_path FROM nodes WHERE state = 'ACTIVE' ORDER BY cantor_path")]

    def open_parent_paths(self, max_depth: int = DEFAULT_MAX_DEPTH, root_prefix: str | None = None) -> list[str]:
        """Active nodes that can still take a child: below max_depth and with fewer than M children."""
        with self._read() as c:
            rows = c.execute(
                "SELECT p.cantor_path FROM nodes p WHERE p.state = 'ACTIVE' AND p.depth < ? "
                "AND (SELECT COUNT(DISTINCT CAST(substr(c.cantor_path, length(p.cantor_path) + 2) AS INTEGER)) "
                "FROM nodes c WHERE c.state = 'ACTIVE' AND instr(c.cantor_path, p.cantor_path || '.') = 1) < ? "
                "ORDER BY p.depth ASC, p.cantor_path ASC",
                (max_depth, M),
            ).fetchall()
        paths = [r["cantor_path"] for r in rows]
        if root_prefix:
            pref = root_prefix + "."
            paths = [p for p in paths if p == root_prefix or p.startswith(pref)]
        return paths

    def all_nodes(self) -> list[SwarmNode]:
        with self._read() as c:
            return [self._node(r) for r in c.execute("SELECT * FROM nodes ORDER BY cantor_path")]

    def lookup_dedup(self, payload_or_hash: Any) -> dict[str, str] | None:
        """Cross-branch read against the shared blackboard dedup index."""
        h = payload_or_hash if (isinstance(payload_or_hash, str) and len(payload_or_hash) == 64) else task_hash(payload_or_hash)
        with self._read() as c:
            row = c.execute("SELECT task_hash, cantor_path, state FROM dedup WHERE task_hash = ?", (h,)).fetchone()
        if row is None:
            return None
        return {"task_hash": row["task_hash"], "cantor_path": row["cantor_path"], "state": row["state"]}

    def read_branch_outputs(self, path: str = ROOT) -> list[dict[str, Any]]:
        """Read folded child outputs stored on `path` in the shared blackboard."""
        node = self.get_node(path)
        if node is None:
            return []
        return list(node.folded_outputs)

    # ------------------------------------------------------------------ mutations

    def cut_branch(
        self,
        parent_path: str,
        payloads: list[Any],
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        min_radius: float = DEFAULT_MIN_RADIUS,
        max_active_nodes: int = DEFAULT_MAX_ACTIVE,
        request_id: str | None = None,
        auto_route: bool = False,
        budget_share: float | None = None,
    ) -> CutResult:
        """Split an active node into up to M children, one per new task. Atomic: all or nothing (I3, I4).

        Duplicate tasks (I2) are not counted against M. They return a reference to the existing path.
        When auto_route=True and parent_path has reached M children, routes recursively under the
        first eligible open ACTIVE descendant (BFS) rather than refusing at root.
        request_id is the caller's idempotency key: repeating a cut with the same request_id replays the
        stored outcome.
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

                active = c.execute("SELECT COUNT(*) FROM nodes WHERE state = 'ACTIVE'").fetchone()[0]
                if active + len(hashes) > max_active_nodes:
                    raise Refused(f"max_active_nodes {max_active_nodes} reached")

                # Pre-check duplicates so we know how many new child slots are genuinely needed
                new_count = 0
                for h in hashes:
                    if c.execute("SELECT 1 FROM dedup WHERE task_hash = ?", (h,)).fetchone() is None:
                        new_count += 1

                target_parent = parent
                free_indices = self._free_child_indices(c, target_parent["cantor_path"])
                if new_count > 0:
                    needs_route = (
                        target_parent["depth"] + 1 > max_depth
                        or (float(target_parent["r"]) * CHILD_SCALE) < min_radius
                        or len(free_indices) < new_count
                    )
                    if needs_route:
                        if auto_route:
                            routed = self._find_open_parent_in_tx(
                                c,
                                parent_path,
                                slots_needed=new_count,
                                max_depth=max_depth,
                                min_radius=min_radius,
                            )
                            if routed is None:
                                raise Refused(f"no open descendant under {parent_path} (max_depth={max_depth}, m={M})")
                            target_parent = routed
                            free_indices = self._free_child_indices(c, target_parent["cantor_path"])
                        else:
                            if target_parent["depth"] + 1 > max_depth:
                                raise Refused(f"max_depth {max_depth} reached")
                            if (float(target_parent["r"]) * CHILD_SCALE) < min_radius:
                                raise Refused(f"min_radius {min_radius} reached")
                            raise Refused(f"cut exceeds m={M} children under {parent_path}")

                t_path = target_parent["cantor_path"]
                parent_region = SpatialRegion(
                    target_parent["x"], target_parent["y"], target_parent["z"], target_parent["r"]
                )
                parent_alloc = float(target_parent["budget_allocated"] or 0.0)
                parent_spent = float(target_parent["budget_spent"] or 0.0)
                parent_remaining = max(0.0, parent_alloc - parent_spent)

                per_child_budget = 0.0
                if new_count > 0:
                    if budget_share is not None:
                        per_child_budget = float(budget_share)
                        if per_child_budget < 0.0:
                            raise Refused("budget_share must be >= 0")
                        if per_child_budget * new_count > parent_remaining + 1e-9:
                            raise Refused(
                                f"insufficient budget under {t_path}: need {per_child_budget * new_count:.4f}, "
                                f"remaining {parent_remaining:.4f}"
                            )
                    elif parent_remaining > 0.0:
                        denom = max(new_count, len(free_indices))
                        per_child_budget = parent_remaining / denom

                results: list[SpawnResult] = []
                created = 0
                total_split = 0.0
                for h in hashes:
                    if created < len(free_indices):
                        index = free_indices[created]
                    else:
                        index = M  # will only trigger Refused if h is not a duplicate
                    candidate = f"{t_path}.{index}"
                    if c.execute("SELECT 1 FROM nodes WHERE cantor_path = ?", (candidate,)).fetchone():
                        # Keep the previous occupant's path, tombstone and JOIN replay valid.
                        candidate = f"{candidate}~{h}"
                    cur = c.execute("INSERT OR IGNORE INTO dedup VALUES(?,?,?)", (h, candidate, "ACTIVE"))
                    if cur.rowcount == 0:  # CAS lost: the task already exists somewhere (I2)
                        row = c.execute("SELECT cantor_path, state FROM dedup WHERE task_hash = ?", (h,)).fetchone()
                        reason = "duplicate_active" if row["state"] == "ACTIVE" else "duplicate_tombstoned"
                        results.append(SpawnResult(False, row["cantor_path"], reason, 0.0))
                        continue
                    if index >= M:
                        raise Refused(f"cut exceeds m={M} children under {t_path}")
                    region = parent_region.child(index)
                    c.execute(
                        "INSERT INTO nodes("
                        "cantor_path, parent_path, task_hash, depth, x, y, z, r, state, claimed_by, "
                        "budget_allocated, budget_spent, output_json, folded_outputs_json"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (candidate, t_path, h, target_parent["depth"] + 1, region.x, region.y, region.z,
                         region.r, "ACTIVE", None, per_child_budget, 0.0, None, "[]"),
                    )
                    results.append(SpawnResult(True, candidate, "created", per_child_budget))
                    created += 1
                    total_split += per_child_budget

                if total_split > 0.0:
                    c.execute(
                        "UPDATE nodes SET budget_allocated = MAX(0.0, budget_allocated - ?) WHERE cantor_path = ?",
                        (total_split, t_path),
                    )
                if event_id is not None:
                    self._record(c, event_id, "CUT", parent_path, [r.__dict__ for r in results])
                return CutResult(True, "ok", tuple(results))
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def record_spend(self, path: str, amount: float) -> CutResult:
        """Record budget spend against an active node."""
        spend = max(0.0, float(amount))
        try:
            with self._tx() as c:
                row = c.execute("SELECT state FROM nodes WHERE cantor_path = ?", (path,)).fetchone()
                if row is None:
                    raise Refused(f"no node {path}")
                if row["state"] != "ACTIVE":
                    raise Refused(f"node {path} is {row['state']}")
                c.execute("UPDATE nodes SET budget_spent = budget_spent + ? WHERE cantor_path = ?", (spend, path))
                return CutResult(True, "ok", ())
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def join_branch(
        self,
        path: str,
        reason: str = "complete",
        *,
        output: Any = None,
        spent: float = 0.0,
        earned: float = 0.0,
    ) -> CutResult:
        """Fold a completed (or terminally failed) subtree back into its parent.

        - Marks the node and any active descendants JOINED and tombstones their hashes (I3).
        - If `output` is provided (or the node already holds folded child outputs), folds them into
          the parent node's `folded_outputs_json` so information flows upward toward ROOT.
        - Credits remaining unspent budget + earned budget back to the parent node.
        """
        event_id = _event_id("JOIN", path)
        try:
            with self._tx() as c:
                prior = self._event(c, event_id)
                if prior is not None:
                    return CutResult(True, "replayed", ())
                if path == ROOT:
                    raise Refused("the root cannot be joined")
                node_row = c.execute("SELECT * FROM nodes WHERE cantor_path = ?", (path,)).fetchone()
                if node_row is None:
                    raise Refused(f"no node {path}")
                prefix = path + "."
                subtree = c.execute(
                    "SELECT cantor_path, task_hash, state, budget_allocated, budget_spent "
                    "FROM nodes WHERE cantor_path = ? OR substr(cantor_path, 1, ?) = ?",
                    (path, len(prefix), prefix),
                ).fetchall()

                extra_spend = max(0.0, float(spent))
                extra_earned = max(0.0, float(earned))
                returned_budget = extra_earned
                joined = 0
                for row in subtree:
                    row_alloc = float(row["budget_allocated"] or 0.0)
                    row_spent = float(row["budget_spent"] or 0.0)
                    if row["cantor_path"] == path:
                        row_spent += extra_spend
                    if row["state"] == "ACTIVE":
                        returned_budget += max(0.0, row_alloc - row_spent)
                        c.execute(
                            "UPDATE nodes SET state = 'JOINED', budget_spent = ?, output_json = ? "
                            "WHERE cantor_path = ?",
                            (
                                row_spent,
                                json.dumps(output, default=str) if (row["cantor_path"] == path and output is not None) else None,
                                row["cantor_path"],
                            ),
                        )
                        joined += 1
                    c.execute("UPDATE dedup SET state = 'TOMBSTONED' WHERE task_hash = ?", (row["task_hash"],))

                # Fold output + budget back into the parent node (flowing up toward ROOT)
                parent_path = node_row["parent_path"]
                if parent_path:
                    parent_row = c.execute(
                        "SELECT folded_outputs_json FROM nodes WHERE cantor_path = ?",
                        (parent_path,),
                    ).fetchone()
                    if parent_row is not None:
                        existing_folded = _json_loads_safe(parent_row["folded_outputs_json"], [])
                        if not isinstance(existing_folded, list):
                            existing_folded = []
                        child_folded = _json_loads_safe(node_row["folded_outputs_json"], [])
                        if not isinstance(child_folded, list):
                            child_folded = []
                        if output is not None or child_folded:
                            fold_entry: dict[str, Any] = {
                                "child_path": path,
                                "reason": reason,
                                "ts": time.time(),
                            }
                            if output is not None:
                                fold_entry["output"] = output
                            if child_folded:
                                fold_entry["children"] = child_folded
                            existing_folded.append(fold_entry)
                        c.execute(
                            "UPDATE nodes SET budget_allocated = budget_allocated + ?, folded_outputs_json = ? "
                            "WHERE cantor_path = ?",
                            (returned_budget, json.dumps(existing_folded, default=str), parent_path),
                        )

                self._record(
                    c,
                    event_id,
                    "JOIN",
                    path,
                    {"joined": joined, "reason": reason, "returned_budget": round(returned_budget, 6)},
                )
                return CutResult(True, "ok", ())
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def release_leaf(self, path: str, reason: str = "transient", *, spent: float = 0.0) -> CutResult:
        """Free a leaf's spatial volume and its task hash so the task can be spawned again.

        Used for transient infrastructure failures (429s, timeouts, local 500s) and cancellation,
        where tombstoning would kill retries. Returns unspent budget to the parent.
        """
        try:
            with self._tx() as c:
                row = c.execute(
                    "SELECT state, task_hash, parent_path, budget_allocated, budget_spent "
                    "FROM nodes WHERE cantor_path = ?",
                    (path,),
                ).fetchone()
                if row is None:
                    return CutResult(True, "absent", ())
                if path == ROOT:
                    raise Refused("the root cannot be released")
                if c.execute("SELECT 1 FROM nodes WHERE parent_path = ?", (path,)).fetchone() is not None:
                    raise Refused(f"{path} has children and cannot be released")
                if row["state"] != "ACTIVE":
                    raise Refused(f"node {path} is {row['state']}")
                returned = max(0.0, float(row["budget_allocated"] or 0.0) - (float(row["budget_spent"] or 0.0) + max(0.0, float(spent))))
                if row["parent_path"] and returned > 0.0:
                    c.execute(
                        "UPDATE nodes SET budget_allocated = budget_allocated + ? WHERE cantor_path = ?",
                        (returned, row["parent_path"]),
                    )
                c.execute("DELETE FROM dedup WHERE task_hash = ?", (row["task_hash"],))
                c.execute("DELETE FROM nodes WHERE cantor_path = ?", (path,))
                return CutResult(True, reason, ())
        except Refused as exc:
            return CutResult(False, str(exc), ())

    def child_count(self, path: str) -> int:
        with self._read() as c:
            return int(c.execute(
                "SELECT COUNT(DISTINCT CAST(substr(cantor_path, ?) AS INTEGER)) FROM nodes WHERE parent_path = ?",
                (len(path) + 2, path),
            ).fetchone()[0])

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

    def finalize_node(self, path: str, *, output: Any = None, spent: float = 0.0, earned: float = 0.0) -> CutResult:
        """Mark an active node complete, tombstone its hash, and optionally fold output/budget to parent."""
        event_id = _event_id("FINALIZE", path)
        try:
            with self._tx() as c:
                row = c.execute(
                    "SELECT state, task_hash, parent_path, budget_allocated, budget_spent, folded_outputs_json "
                    "FROM nodes WHERE cantor_path = ?",
                    (path,),
                ).fetchone()
                if row is None:
                    raise Refused(f"no node {path}")
                if row["state"] == "FINALIZED":
                    return CutResult(True, "replayed", ())
                if row["state"] != "ACTIVE":
                    raise Refused(f"node {path} is {row['state']}")
                new_spent = float(row["budget_spent"] or 0.0) + max(0.0, float(spent))
                returned = max(0.0, float(row["budget_allocated"] or 0.0) - new_spent) + max(0.0, float(earned))
                c.execute(
                    "UPDATE nodes SET state = 'FINALIZED', budget_spent = ?, output_json = ? WHERE cantor_path = ?",
                    (new_spent, json.dumps(output, default=str) if output is not None else None, path),
                )
                c.execute("UPDATE dedup SET state = 'TOMBSTONED' WHERE task_hash = ?", (row["task_hash"],))
                if row["parent_path"]:
                    p_row = c.execute("SELECT folded_outputs_json FROM nodes WHERE cantor_path = ?", (row["parent_path"],)).fetchone()
                    if p_row is not None:
                        p_folded = _json_loads_safe(p_row["folded_outputs_json"], [])
                        if not isinstance(p_folded, list):
                            p_folded = []
                        if output is not None:
                            p_folded.append({"child_path": path, "reason": "finalized", "output": output, "ts": time.time()})
                        c.execute(
                            "UPDATE nodes SET budget_allocated = budget_allocated + ?, folded_outputs_json = ? "
                            "WHERE cantor_path = ?",
                            (returned, json.dumps(p_folded, default=str), row["parent_path"]),
                        )
                self._record(c, event_id, "FINALIZE", path, {"returned_budget": round(returned, 6)})
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

        live_branches = {branch for p, n in nodes.items() if n.state == "ACTIVE" for branch in (p, *_ancestors(p))}
        occupied_slots: set[tuple[str, int]] = set()
        for p, n in nodes.items():
            if n.budget_allocated < -1e-9:
                errors.append(f"budget: {p} has negative budget_allocated {n.budget_allocated}")
            if n.budget_spent < -1e-9:
                errors.append(f"budget: {p} has negative budget_spent {n.budget_spent}")
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
            index = int(p.rsplit(".", 1)[1].partition("~")[0])
            if not 0 <= index < M:
                errors.append(f"I3 child index {index} at {p} outside 0..{M - 1}")
            if p in live_branches:
                slot = (n.parent_path, index)
                if slot in occupied_slots:
                    errors.append(f"I3 child slot {index} occupied twice under {n.parent_path}")
                occupied_slots.add(slot)
            if not parent.region.contains(n.region):
                errors.append(f"I1 {p} is not contained in its parent {n.parent_path}")
            if n.depth > max_depth:
                errors.append(f"budget: {p} depth {n.depth} exceeds max_depth {max_depth}")
            if n.region.r < min_radius:
                errors.append(f"budget: {p} radius {n.region.r} below min_radius {min_radius}")
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
