"""m-Eulerian RSI Join-Cut Swarm: 3D Cantor-Path Embedding & Atomic SQLite WAL Registry.

Standalone sidecar implementing the formal specification:
  1. Deterministic base-m Cantor-path to 3D bounding-sphere embedding with alternating
     orthogonal frames (Alexander horned sphere construction), Gram-Schmidt
     re-orthonormalization, and IEEE 754 float64 precision floor (min_radius / max_depth).
  2. Atomic deduplication protocol over SQLite WAL (isolation_level=None, explicit
     BEGIN IMMEDIATE / COMMIT / ROLLBACK) with RFC 8785 canonical SHA-256 digests,
     idempotent request_id retries, monotonic tombstones, and wait_edges cycle detection.
  3. Genus-0 Join-Cut operators (cut, join, prune, release_transient) preserving
     |V| - |E| = 1, strict parent containment, pairwise active-leaf disjointness,
     and safe delimiter-anchored descendant prefix matching.
  4. Thin lifecycle hooks (try_cut, settle_or_join, release_transient) for non-breaking
     integration into LiveSwarmRunner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

logger = logging.getLogger(__name__)

ROOT_PATH: str = ""
DEFAULT_M: int = 6
DEFAULT_R0: float = 1.0
DEFAULT_GAMMA: float = 0.90
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BUSY_TIMEOUT_MS: int = 30000

_VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "timestamp",
        "created_at",
        "updated_at",
        "started_at",
        "worker_id",
        "owner_id",
        "retry_count",
        "attempt",
        "trace_id",
        "request_id",
        "sub_wid",
    }
)

Vec3 = tuple[float, float, float]


# ---------------------------------------------------------------------------
# 1. Cantor-Path Formatting & 3D Bounding-Sphere Embedding
# ---------------------------------------------------------------------------


def format_child_path(parent_path: str, branch_idx: int) -> str:
    """Format child Cantor path without leading dots (root '' -> '0', '0' -> '0.1')."""
    if branch_idx < 0:
        raise ValueError(f"branch_idx must be >= 0, got {branch_idx}")
    if parent_path == ROOT_PATH:
        return str(branch_idx)
    return f"{parent_path}.{branch_idx}"


def parent_of(path: str) -> str | None:
    """Return parent Cantor path, or None for root ''."""
    if path == ROOT_PATH:
        return None
    if "." not in path:
        return ROOT_PATH
    return path.rsplit(".", 1)[0]


def path_depth(path: str) -> int:
    """Return depth of a Cantor path (root '' has depth 0)."""
    if path == ROOT_PATH:
        return 0
    return path.count(".") + 1


def parse_path(path: str) -> tuple[int, ...]:
    """Parse a Cantor path string into a tuple of branch indices."""
    if path == ROOT_PATH:
        return ()
    if path.startswith(".") or path.endswith(".") or ".." in path:
        raise ValueError(f"malformed Cantor path: {path!r}")
    return tuple(int(seg) for seg in path.split("."))


def is_prefix_or_equal(prefix: str, path: str) -> bool:
    """Strict delimiter-safe prefix test: True iff prefix == path or prefix is an ancestor of path."""
    if prefix == ROOT_PATH:
        return True
    return path == prefix or path.startswith(prefix + ".")


@dataclass(frozen=True)
class EmbeddingParams:
    m: int
    r0: float
    gamma: float
    alpha: float
    beta_star: float
    beta: float
    min_radius: float
    max_depth: int


def compute_embedding_params(
    m: int = DEFAULT_M,
    r0: float = DEFAULT_R0,
    gamma: float = DEFAULT_GAMMA,
) -> EmbeddingParams:
    """Compute exact geometric scaling constants and float64 precision floor."""
    if m < 2:
        raise ValueError(f"branching factor m must be >= 2, got {m}")
    if r0 <= 0.0:
        raise ValueError(f"r0 must be > 0, got {r0}")
    if not (0.0 < gamma < 1.0):
        raise ValueError(f"gamma must be in (0, 1), got {gamma}")

    sin_pi_m = math.sin(math.pi / m)
    alpha = 1.0 / (1.0 + sin_pi_m)
    beta_star = sin_pi_m / (1.0 + sin_pi_m)
    beta = gamma * beta_star
    min_radius = max(
        r0 * (2.0**-40),
        ((2.0**12) * gamma / (2.0 * (1.0 - gamma))) * r0 * (2.0**-52),
    )
    if beta * r0 < min_radius:
        raise ValueError("first-level child radius is below min_radius")
    max_depth = max(1, math.floor(math.log(min_radius / r0) / math.log(beta)))
    return EmbeddingParams(
        m=m,
        r0=r0,
        gamma=gamma,
        alpha=alpha,
        beta_star=beta_star,
        beta=beta,
        min_radius=min_radius,
        max_depth=max_depth,
    )


def _dot(a: Vec3, b: Vec3) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec3, b: Vec3) -> Vec3:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _norm(a: Vec3) -> float:
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def _scale(s: float, a: Vec3) -> Vec3:
    return (s * a[0], s * a[1], s * a[2])


def _add(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _sub(a: Vec3, b: Vec3) -> Vec3:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _normalize(a: Vec3) -> Vec3:
    n = _norm(a)
    if n == 0.0:
        raise ValueError("cannot normalize zero vector")
    return (a[0] / n, a[1] / n, a[2] / n)


@dataclass(frozen=True)
class OrthonormalFrame:
    u: Vec3
    v: Vec3
    w: Vec3

    def reorthonormalize(self) -> "OrthonormalFrame":
        """Gram-Schmidt re-orthonormalization guarding against deep-spine frame drift."""
        u_hat = _normalize(self.u)
        v_proj = _scale(_dot(self.v, u_hat), u_hat)
        v_hat = _normalize(_sub(self.v, v_proj))
        w_hat = _normalize(_cross(u_hat, v_hat))
        return OrthonormalFrame(u=u_hat, v=v_hat, w=w_hat)


ROOT_FRAME = OrthonormalFrame(
    u=(1.0, 0.0, 0.0),
    v=(0.0, 1.0, 0.0),
    w=(0.0, 0.0, 1.0),
)


@dataclass(frozen=True)
class BoundingSphere:
    cx: float
    cy: float
    cz: float
    radius: float

    def center(self) -> Vec3:
        return (self.cx, self.cy, self.cz)

    def strictly_contains(self, child: "BoundingSphere") -> bool:
        """Strict containment: ||c_child - c_parent||_2 + r_child < r_parent."""
        return math.dist(self.center(), child.center()) + child.radius < self.radius

    def strictly_disjoint(self, other: "BoundingSphere") -> bool:
        """Strict disjointness: ||c_i - c_j||_2 > r_i + r_j."""
        return math.dist(self.center(), other.center()) > self.radius + other.radius


def embed_child(
    parent_sphere: BoundingSphere,
    parent_frame: OrthonormalFrame,
    branch_idx: int,
    params: EmbeddingParams,
) -> tuple[BoundingSphere, OrthonormalFrame]:
    """Compute child bounding sphere and alternating orthogonal frame."""
    if not (0 <= branch_idx < params.m):
        raise ValueError(f"branch_idx {branch_idx} out of range [0, {params.m})")
    theta_j = (2.0 * math.pi * branch_idx) / params.m
    e_j = _add(_scale(math.cos(theta_j), parent_frame.u), _scale(math.sin(theta_j), parent_frame.v))
    c_child = _add(parent_sphere.center(), _scale(params.alpha * parent_sphere.radius, e_j))
    r_child = params.beta * parent_sphere.radius
    child_frame = OrthonormalFrame(
        u=e_j,
        v=parent_frame.w,
        w=_cross(e_j, parent_frame.w),
    ).reorthonormalize()
    child_sphere = BoundingSphere(cx=c_child[0], cy=c_child[1], cz=c_child[2], radius=r_child)
    return child_sphere, child_frame


def embed_cantor_path(
    path: str,
    params: EmbeddingParams,
) -> tuple[BoundingSphere, OrthonormalFrame]:
    """Deterministically embed any Cantor path into its 3D bounding sphere and frame."""
    sphere = BoundingSphere(cx=0.0, cy=0.0, cz=0.0, radius=params.r0)
    frame = ROOT_FRAME
    for idx in parse_path(path):
        sphere, frame = embed_child(sphere, frame, idx, params)
    return sphere, frame


# ---------------------------------------------------------------------------
# 2. RFC 8785 Canonical JSON & SHA-256 Task Digest
# ---------------------------------------------------------------------------


def _strip_volatile(value: Any, serialize: Callable[[Any], str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(k): _strip_volatile(v, serialize)
            for k, v in value.items()
            if str(k) not in _VOLATILE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_strip_volatile(v, serialize) for v in value]
    if isinstance(value, set):
        return sorted((_strip_volatile(v, serialize) for v in value), key=serialize)
    return value


def _rfc8785_key_sort(key: str) -> bytes:
    """RFC 8785 Section 3.2.3: sort object keys by UTF-16 code units."""
    return key.encode("utf-16-be")


def _legacy_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    return json.dumps(value, allow_nan=False)


def _jcs_number(value: int | float) -> str:
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("RFC 8785 number exceeds binary64 range") from exc
    if not math.isfinite(number):
        raise ValueError(f"RFC 8785 forbids non-finite float: {number}")
    if isinstance(value, int) and value != number:
        raise ValueError("RFC 8785 integer cannot be represented exactly as binary64")
    if number == 0.0:
        return "0"
    shortest = repr(number)
    coefficient, sep, power = shortest.partition("e")
    if not sep:
        return coefficient.removesuffix(".0")
    exponent = int(power)
    if -6 <= exponent < 21:
        return format(Decimal(shortest), "f")
    return f"{coefficient}e{exponent:+d}"


def _canonical_json(value: Any, number_text: Callable[[int | float], str]) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return number_text(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (bytes, bytearray)):
        return json.dumps(bytes(value).hex())
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json(item, number_text) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = sorted((str(k) for k in value.keys()), key=_rfc8785_key_sort)
        parts = [
            f"{_canonical_json(k, number_text)}:{_canonical_json(value[k], number_text)}" for k in keys
        ]
        return "{" + ",".join(parts) + "}"
    return _canonical_json(str(value), number_text)


def canonical_json_str(value: Any) -> str:
    """Serialize JCS values, rejecting integers that lose precision in binary64."""
    return _canonical_json(value, _jcs_number)


class _TaskHash(bytes):
    """Canonical digest with older numeric spellings available to local registry lookups."""

    legacy: tuple[bytes, ...]


def _stored_task_hash(conn: sqlite3.Connection, task_hash: bytes) -> bytes:
    if not isinstance(task_hash, _TaskHash):
        return task_hash
    canonical = bytes(task_hash)
    candidates = (canonical, *task_hash.legacy)
    values = ", ".join(f"(?, {i})" for i in range(len(candidates)))
    row = conn.execute(
        f"WITH inputs(hash, ordinal) AS (VALUES {values}) "
        "SELECT d.task_hash FROM inputs i JOIN task_dedup d ON d.task_hash = i.hash "
        "LEFT JOIN swarm_nodes n ON n.task_hash = d.task_hash "
        "ORDER BY CASE WHEN d.status IN ('JOINED', 'PRUNED') THEN 0 "
        "WHEN n.owner_id IS NOT NULL AND (n.lease_until IS NULL OR n.lease_until > ?) THEN 1 "
        "WHEN n.state = 'SPLIT' THEN 2 WHEN n.cantor_path IS NOT NULL THEN 3 "
        "ELSE 4 END, i.ordinal LIMIT 1",
        (*candidates, int(time.time())),
    ).fetchone()
    return canonical if row is None else bytes(row[0])


def canonical_task_hash(
    op_type: str,
    normalized_inputs: Any,
    constraints: Iterable[Any] = (),
    schema_version: str = "1",
) -> bytes:
    """Compute 32-byte SHA-256 digest over RFC 8785 canonical task tuple."""
    constraints = tuple(constraints)
    legacy_differs = False
    int_float_differ = False

    def canonical_number(value: int | float) -> str:
        nonlocal legacy_differs, int_float_differ
        text = _jcs_number(value)
        legacy_differs |= text != _legacy_number(value)
        int_float_differ |= abs(value) >= 1e21
        return text

    def digest(number_text: Callable[[int | float], str]) -> bytes:
        def serialize(item: Any) -> str:
            return _canonical_json(item, number_text)

        clean_inputs = _strip_volatile(normalized_inputs, serialize)
        sorted_constraints = sorted((_strip_volatile(c, serialize) for c in constraints), key=serialize)
        payload = [str(op_type), clean_inputs, sorted_constraints, str(schema_version)]
        return hashlib.sha256(serialize(payload).encode("utf-8")).digest()

    canonical = digest(canonical_number)
    if not legacy_differs and not int_float_differ:
        return canonical
    # Old rows store only the SHA-256 digest. Check the exact old input spelling and
    # uniform int/float alternatives without enumerating combinations across a payload.
    candidates = [digest(_legacy_number)]
    if int_float_differ:
        def legacy_integral(value: int | float) -> str:
            number = float(value)
            return str(int(number)) if number.is_integer() else _legacy_number(value)

        candidates.append(digest(lambda v: _legacy_number(float(v))))
        candidates.append(digest(legacy_integral))
    result = _TaskHash(canonical)
    result.legacy = tuple(dict.fromkeys(h for h in candidates if h != canonical))
    return result


# ---------------------------------------------------------------------------
# 3. Typed Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildSpec:
    branch_idx: int
    task_hash: bytes
    request_id: str | None = None
    max_retries: int | None = None


@dataclass(frozen=True)
class SpawnOutcome:
    status: str  # SPAWNED | COLLAPSED_LIVE | TOMBSTONED | ERR_*
    cantor_path: str | None
    tombstone_status: str | None = None
    result_digest: bytes | None = None
    sphere: BoundingSphere | None = None
    error: str | None = None


@dataclass(frozen=True)
class CutOutcome:
    ok: bool
    error: str | None = None
    parent_version: int | None = None
    children: tuple[SpawnOutcome, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class JoinOutcome:
    ok: bool
    error: str | None = None
    parent_state: str | None = None
    joined_paths: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PruneOutcome:
    ok: bool
    error: str | None = None
    pruned_paths: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ReleaseOutcome:
    ok: bool
    status: str  # RELEASED_FOR_RETRY | TOMBSTONED_PRUNED | ERR_*
    retry_count: int = 0
    error: str | None = None


@dataclass(frozen=True)
class NodeRecord:
    cantor_path: str
    parent_path: str | None
    branch_idx: int | None
    depth: int
    task_hash: bytes
    state: str
    sphere: BoundingSphere
    version: int
    owner_id: str | None
    lease_epoch: int
    lease_until: int | None
    started_at: int | None


@dataclass(frozen=True)
class TryCutResult:
    """Thin runner-facing result returned by JoinCutRegistry.try_cut()."""

    allowed: bool
    status: str  # SPAWNED | COLLAPSED_LIVE | TOMBSTONED | ERR_*
    cantor_path: str | None
    lease_epoch: int
    task_hash: bytes
    reason: str
    is_dedup: bool = False
    result_digest: bytes | None = None


# ---------------------------------------------------------------------------
# 4. SQLite WAL Registry Implementation
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS swarm_meta (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    m                   INTEGER NOT NULL CHECK (m >= 2),
    r0                  REAL    NOT NULL CHECK (r0 > 0),
    gamma               REAL    NOT NULL CHECK (gamma > 0 AND gamma < 1),
    min_radius          REAL    NOT NULL CHECK (min_radius > 0),
    max_depth           INTEGER NOT NULL CHECK (max_depth >= 1),
    default_max_retries INTEGER NOT NULL DEFAULT 3 CHECK (default_max_retries >= 0),
    legacy_generation_floor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_dedup (
    task_hash      BLOB    PRIMARY KEY,
    cantor_path    TEXT,
    request_id     TEXT,
    status         TEXT    NOT NULL CHECK (status IN ('LIVE', 'JOINED', 'PRUNED')),
    retry_count    INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    max_retries    INTEGER NOT NULL CHECK (max_retries >= 0),
    result_digest  BLOB,
    created_at     INTEGER NOT NULL,
    tombstoned_at  INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_dedup_live_path
ON task_dedup(cantor_path)
WHERE status = 'LIVE' AND cantor_path IS NOT NULL;

CREATE TABLE IF NOT EXISTS swarm_nodes (
    cantor_path  TEXT    PRIMARY KEY,
    parent_path  TEXT    REFERENCES swarm_nodes(cantor_path) ON DELETE RESTRICT,
    branch_idx   INTEGER CHECK (branch_idx IS NULL OR branch_idx >= 0),
    depth        INTEGER NOT NULL CHECK (depth >= 0),
    task_hash    BLOB    NOT NULL UNIQUE REFERENCES task_dedup(task_hash),
    state        TEXT    NOT NULL CHECK (state IN ('ACTIVE', 'SPLIT')),
    cx           REAL    NOT NULL,
    cy           REAL    NOT NULL,
    cz           REAL    NOT NULL,
    radius       REAL    NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    owner_id     TEXT,
    lease_epoch  INTEGER NOT NULL DEFAULT 0,
    lease_until  INTEGER,
    started_at   INTEGER,
    UNIQUE (parent_path, branch_idx),
    CHECK ((cantor_path = '' AND parent_path IS NULL AND branch_idx IS NULL AND depth = 0)
        OR (cantor_path <> '' AND parent_path IS NOT NULL AND branch_idx IS NOT NULL AND depth > 0))
);

CREATE TABLE IF NOT EXISTS wait_edges (
    waiter_path  TEXT NOT NULL REFERENCES swarm_nodes(cantor_path) ON DELETE CASCADE,
    target_hash  BLOB NOT NULL REFERENCES task_dedup(task_hash),
    PRIMARY KEY (waiter_path, target_hash)
);

CREATE TABLE IF NOT EXISTS runner_branches (
    cantor_path TEXT PRIMARY KEY REFERENCES swarm_nodes(cantor_path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS retired_paths (
    cantor_path TEXT PRIMARY KEY,
    generation  INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_nodes_retire_generation
AFTER DELETE ON swarm_nodes
BEGIN
    INSERT INTO retired_paths (cantor_path, generation)
    VALUES (OLD.cantor_path, max(OLD.version, OLD.lease_epoch))
    ON CONFLICT(cantor_path) DO UPDATE SET generation = max(generation, excluded.generation);
END;

CREATE TRIGGER IF NOT EXISTS trg_nodes_fence_reuse
AFTER INSERT ON swarm_nodes
WHEN NEW.cantor_path <> '' AND (
    (SELECT legacy_generation_floor FROM swarm_meta WHERE id = 1) > 0
    OR EXISTS (SELECT 1 FROM retired_paths WHERE cantor_path = NEW.cantor_path)
)
BEGIN
    UPDATE swarm_nodes SET (version, lease_epoch) = (
        SELECT high_water + 1, high_water + 1 FROM (
            SELECT max(legacy_generation_floor, coalesce((
                SELECT generation FROM retired_paths WHERE cantor_path = NEW.cantor_path
            ), 0)) AS high_water FROM swarm_meta WHERE id = 1
        )
    ) WHERE cantor_path = NEW.cantor_path;
END;

CREATE TRIGGER IF NOT EXISTS trg_dedup_no_delete
BEFORE DELETE ON task_dedup
BEGIN
    SELECT RAISE(ABORT, 'TOMBSTONE_DELETE_FORBIDDEN');
END;

CREATE TRIGGER IF NOT EXISTS trg_dedup_monotonic
BEFORE UPDATE OF status ON task_dedup
WHEN OLD.status IN ('JOINED', 'PRUNED')
BEGIN
    SELECT RAISE(ABORT, 'TOMBSTONE_RESURRECTION_FORBIDDEN');
END;

CREATE TRIGGER IF NOT EXISTS trg_nodes_arity_guard
BEFORE INSERT ON swarm_nodes
WHEN NEW.branch_idx IS NOT NULL
 AND (NEW.branch_idx < 0 OR NEW.branch_idx >= (SELECT m FROM swarm_meta WHERE id = 1))
BEGIN
    SELECT RAISE(ABORT, 'BRANCH_IDX_OUT_OF_RANGE');
END;
"""

_CYCLE_CHECK_SQL = """
WITH RECURSIVE reach(node) AS (
    SELECT ?
    UNION
    SELECT n.cantor_path
    FROM swarm_nodes n
    JOIN reach r ON n.parent_path = r.node
    UNION
    SELECT td.cantor_path
    FROM wait_edges w
    JOIN task_dedup td ON td.task_hash = w.target_hash
    JOIN reach r ON w.waiter_path = r.node
    WHERE td.status = 'LIVE' AND td.cantor_path IS NOT NULL
)
SELECT 1 FROM reach WHERE node = ? LIMIT 1
"""


class JoinCutRegistry:
    """Atomic m-Eulerian RSI Join-Cut Swarm registry backed by SQLite WAL."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        m: int = DEFAULT_M,
        r0: float = DEFAULT_R0,
        gamma: float = DEFAULT_GAMMA,
        default_max_retries: int = DEFAULT_MAX_RETRIES,
        synchronous: str = "NORMAL",
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        sync_mode = str(synchronous).upper()
        if sync_mode not in ("NORMAL", "FULL"):
            raise ValueError(f"synchronous must be 'NORMAL' or 'FULL', got {synchronous!r}")
        if default_max_retries < 0:
            raise ValueError(f"default_max_retries must be >= 0, got {default_max_retries}")

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.synchronous = sync_mode
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        self.default_max_retries = int(default_max_retries)
        self.params = compute_embedding_params(m=m, r0=r0, gamma=gamma)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,  # Explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute(f"PRAGMA synchronous = {self.synchronous};")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms};")
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _immediate_tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            if conn.in_transaction:
                conn.execute("COMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_SQL)
            meta_columns = {row["name"] for row in conn.execute("PRAGMA table_info(swarm_meta)")}
            if "legacy_generation_floor" not in meta_columns:
                # Old releases erased the path, so even a previously occupied slot can
                # have no surviving row. Fence new incarnations above legacy counters.
                conn.execute(
                    "ALTER TABLE swarm_meta ADD COLUMN legacy_generation_floor "
                    f"INTEGER NOT NULL DEFAULT {1 << 62}"
                )
            meta = conn.execute("SELECT * FROM swarm_meta WHERE id = 1").fetchone()
            if meta is None:
                conn.execute(
                    "INSERT INTO swarm_meta (id, m, r0, gamma, min_radius, max_depth, default_max_retries) "
                    "VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (
                        self.params.m,
                        self.params.r0,
                        self.params.gamma,
                        self.params.min_radius,
                        self.params.max_depth,
                        self.default_max_retries,
                    ),
                )
            else:
                self.params = compute_embedding_params(
                    m=int(meta["m"]),
                    r0=float(meta["r0"]),
                    gamma=float(meta["gamma"]),
                )
                self.default_max_retries = int(meta["default_max_retries"])

            root_row = conn.execute(
                "SELECT 1 FROM swarm_nodes WHERE cantor_path = ?", (ROOT_PATH,)
            ).fetchone()
            if root_row is None:
                now = int(time.time())
                root_hash = canonical_task_hash("SWARM_ROOT", {"root": True})
                conn.execute(
                    "INSERT INTO task_dedup "
                    "(task_hash, cantor_path, request_id, status, retry_count, max_retries, created_at) "
                    "VALUES (?, ?, NULL, 'LIVE', 0, ?, ?) ON CONFLICT(task_hash) DO NOTHING",
                    (root_hash, ROOT_PATH, self.default_max_retries, now),
                )
                conn.execute(
                    "INSERT INTO swarm_nodes "
                    "(cantor_path, parent_path, branch_idx, depth, task_hash, state, cx, cy, cz, radius, version, lease_epoch) "
                    "VALUES (?, NULL, NULL, 0, ?, 'ACTIVE', 0.0, 0.0, 0.0, ?, 1, 0) "
                    "ON CONFLICT(cantor_path) DO NOTHING",
                    (ROOT_PATH, root_hash, self.params.r0),
                )
            conn.execute("COMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> NodeRecord:
        return NodeRecord(
            cantor_path=str(row["cantor_path"]),
            parent_path=None if row["parent_path"] is None else str(row["parent_path"]),
            branch_idx=None if row["branch_idx"] is None else int(row["branch_idx"]),
            depth=int(row["depth"]),
            task_hash=bytes(row["task_hash"]),
            state=str(row["state"]),
            sphere=BoundingSphere(
                cx=float(row["cx"]),
                cy=float(row["cy"]),
                cz=float(row["cz"]),
                radius=float(row["radius"]),
            ),
            version=int(row["version"]),
            owner_id=None if row["owner_id"] is None else str(row["owner_id"]),
            lease_epoch=int(row["lease_epoch"]),
            lease_until=None if row["lease_until"] is None else int(row["lease_until"]),
            started_at=None if row["started_at"] is None else int(row["started_at"]),
        )

    def get_node(self, cantor_path: str) -> NodeRecord | None:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM swarm_nodes WHERE cantor_path = ?", (cantor_path,)
            ).fetchone()
        return None if row is None else self._row_to_node(row)

    def get_dedup(self, task_hash: bytes) -> dict[str, Any] | None:
        with self._read() as c:
            row = c.execute(
                "SELECT * FROM task_dedup WHERE task_hash = ?", (_stored_task_hash(c, task_hash),)
            ).fetchone()
        return None if row is None else dict(row)

    def all_nodes(self) -> list[NodeRecord]:
        with self._read() as c:
            rows = c.execute("SELECT * FROM swarm_nodes ORDER BY depth ASC, cantor_path ASC").fetchall()
        return [self._row_to_node(r) for r in rows]

    def children_of(self, parent_path: str) -> list[NodeRecord]:
        with self._read() as c:
            rows = c.execute(
                "SELECT * FROM swarm_nodes WHERE parent_path = ? ORDER BY branch_idx ASC",
                (parent_path,),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # ------------------------------------------------------------------
    # Lease & execution lifecycle helpers
    # ------------------------------------------------------------------

    def acquire_lease(
        self,
        cantor_path: str,
        owner_id: str,
        ttl_sec: int = 300,
        *,
        mark_started: bool = False,
    ) -> tuple[bool, int]:
        """Claim or renew a lease on an ACTIVE node, monotonically bumping lease_epoch."""
        with self._immediate_tx() as c:
            now = int(time.time())
            lease_until = now + max(1, int(ttl_sec))
            row = c.execute(
                "SELECT state, owner_id, lease_epoch, lease_until, started_at "
                "FROM swarm_nodes WHERE cantor_path = ?",
                (cantor_path,),
            ).fetchone()
            if row is None or row["state"] != "ACTIVE":
                return False, 0
            current_owner = row["owner_id"]
            current_until = row["lease_until"]
            if current_owner is not None and current_owner != owner_id and current_until is not None and current_until > now:
                return False, int(row["lease_epoch"])
            new_epoch = int(row["lease_epoch"]) + 1
            started_val = now if (mark_started and row["started_at"] is None) else row["started_at"]
            c.execute(
                "UPDATE swarm_nodes "
                "SET owner_id = ?, lease_epoch = ?, lease_until = ?, started_at = ?, version = version + 1 "
                "WHERE cantor_path = ?",
                (owner_id, new_epoch, lease_until, started_val, cantor_path),
            )
            return True, new_epoch

    def mark_started(self, cantor_path: str, owner_id: str, lease_epoch: int) -> bool:
        """Mark a leased ACTIVE node as started so transient failures release lease rather than spatial volume."""
        now = int(time.time())
        with self._immediate_tx() as c:
            cur = c.execute(
                "UPDATE swarm_nodes SET started_at = COALESCE(started_at, ?) "
                "WHERE cantor_path = ? AND state = 'ACTIVE' AND owner_id = ? AND lease_epoch = ?",
                (now, cantor_path, owner_id, lease_epoch),
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------
    # Internal helpers (must run inside an active BEGIN IMMEDIATE tx)
    # ------------------------------------------------------------------

    @staticmethod
    def _has_wait_cycle(c: sqlite3.Connection, waiter_path: str, target_path: str) -> bool:
        if waiter_path == target_path:
            return True
        row = c.execute(_CYCLE_CHECK_SQL, (target_path, waiter_path)).fetchone()
        return row is not None

    @staticmethod
    def _has_open_waits(c: sqlite3.Connection, path: str) -> bool:
        return c.execute(
            "SELECT 1 FROM wait_edges w JOIN task_dedup td ON td.task_hash = w.target_hash "
            "WHERE td.status = 'LIVE' AND (w.waiter_path = ? OR w.waiter_path IN "
            "(SELECT cantor_path FROM swarm_nodes WHERE parent_path = ?)) LIMIT 1",
            (path, path),
        ).fetchone() is not None

    @classmethod
    def _recover_empty_split_parent(cls, c: sqlite3.Connection, parent_path: str | None) -> None:
        """Empty SPLIT Parent Check: transition SPLIT parent with 0 remaining children back to ACTIVE."""
        if parent_path is None:
            return
        parent = c.execute(
            "SELECT n.state, n.task_hash, n.parent_path, b.cantor_path AS branch "
            "FROM swarm_nodes n LEFT JOIN runner_branches b USING (cantor_path) WHERE n.cantor_path = ?",
            (parent_path,),
        ).fetchone()
        if parent is None:
            return
        remaining = int(
            c.execute(
                "SELECT COUNT(*) FROM swarm_nodes WHERE parent_path = ?", (parent_path,)
            ).fetchone()[0]
        )
        if parent["state"] == "SPLIT" and remaining == 0:
            if parent["branch"] is not None and not cls._has_open_waits(c, parent_path):
                c.execute(
                    "UPDATE task_dedup SET status = 'JOINED', tombstoned_at = ? WHERE task_hash = ?",
                    (int(time.time()), parent["task_hash"]),
                )
                c.execute("DELETE FROM swarm_nodes WHERE cantor_path = ?", (parent_path,))
                cls._recover_empty_split_parent(c, parent["parent_path"])
                return
            c.execute(
                "UPDATE swarm_nodes SET state = 'ACTIVE', version = version + 1 WHERE cantor_path = ?",
                (parent_path,),
            )
        else:
            c.execute(
                "UPDATE swarm_nodes SET version = version + 1 WHERE cantor_path = ?",
                (parent_path,),
            )

    def _prevalidate_child_geometry(
        self,
        parent_path: str,
        branch_idx: int,
    ) -> tuple[str | None, str, BoundingSphere | None]:
        """Compute & validate child path + 3D sphere outside the write transaction."""
        if not (0 <= branch_idx < self.params.m):
            return "ERR_BRANCH_ARITY", "", None
        child_path = format_child_path(parent_path, branch_idx)
        depth = path_depth(child_path)
        if depth > self.params.max_depth:
            return "ERR_DEPTH_FLOOR", child_path, None
        parent_sphere, parent_frame = embed_cantor_path(parent_path, self.params)
        child_sphere, _ = embed_child(parent_sphere, parent_frame, branch_idx, self.params)
        if child_sphere.radius < self.params.min_radius:
            return "ERR_DEPTH_FLOOR", child_path, None
        if not parent_sphere.strictly_contains(child_sphere):
            return "ERR_DEPTH_FLOOR", child_path, None
        return None, child_path, child_sphere

    def _claim_child_in_tx(
        self,
        c: sqlite3.Connection,
        parent_path: str,
        parent_depth: int,
        spec: ChildSpec,
        child_path: str,
        child_sphere: BoundingSphere,
        now: int,
    ) -> tuple[SpawnOutcome, bool]:
        """Execute Section 2.4 atomic dedup claim inside an open BEGIN IMMEDIATE transaction.

        Returns (SpawnOutcome, mutated_nodes_or_wait_edges).
        """
        max_retries = self.default_max_retries if spec.max_retries is None else int(spec.max_retries)
        task_hash = _stored_task_hash(c, spec.task_hash)
        cur = c.execute(
            "INSERT INTO task_dedup "
            "(task_hash, cantor_path, request_id, status, retry_count, max_retries, created_at) "
            "VALUES (?, ?, ?, 'LIVE', 0, ?, ?) "
            "ON CONFLICT(task_hash) DO NOTHING",
            (task_hash, child_path, spec.request_id, max_retries, now),
        )
        if cur.rowcount == 1:
            c.execute(
                "INSERT INTO swarm_nodes "
                "(cantor_path, parent_path, branch_idx, depth, task_hash, state, cx, cy, cz, radius, version, lease_epoch) "
                "VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, 1, 0)",
                (
                    child_path,
                    parent_path,
                    spec.branch_idx,
                    parent_depth + 1,
                    task_hash,
                    child_sphere.cx,
                    child_sphere.cy,
                    child_sphere.cz,
                    child_sphere.radius,
                ),
            )
            return SpawnOutcome(status="SPAWNED", cantor_path=child_path, sphere=child_sphere), True

        existing = c.execute(
            "SELECT cantor_path, request_id, status, result_digest "
            "FROM task_dedup WHERE task_hash = ?",
            (task_hash,),
        ).fetchone()
        assert existing is not None
        ex_path = None if existing["cantor_path"] is None else str(existing["cantor_path"])
        ex_req = None if existing["request_id"] is None else str(existing["request_id"])
        ex_status = str(existing["status"])
        ex_digest = None if existing["result_digest"] is None else bytes(existing["result_digest"])

        # Case A — Terminal Tombstone
        if ex_status in ("JOINED", "PRUNED"):
            return (
                SpawnOutcome(
                    status="TOMBSTONED",
                    cantor_path=ex_path,
                    tombstone_status=ex_status,
                    result_digest=ex_digest,
                ),
                False,
            )

        # Case B — Idempotent Retry (same request_id, LIVE, bound path)
        if spec.request_id is not None and ex_req == spec.request_id and ex_path is not None:
            ex_node = c.execute(
                "SELECT cx, cy, cz, radius FROM swarm_nodes WHERE cantor_path = ?", (ex_path,)
            ).fetchone()
            ex_sphere = (
                BoundingSphere(ex_node["cx"], ex_node["cy"], ex_node["cz"], ex_node["radius"])
                if ex_node is not None
                else None
            )
            return SpawnOutcome(status="SPAWNED", cantor_path=ex_path, sphere=ex_sphere), False

        # Case C — Re-bind Released Spatial Volume (LIVE, cantor_path IS NULL)
        if ex_path is None:
            c.execute(
                "UPDATE task_dedup SET cantor_path = ?, request_id = COALESCE(?, request_id) "
                "WHERE task_hash = ?",
                (child_path, spec.request_id, task_hash),
            )
            c.execute(
                "INSERT INTO swarm_nodes "
                "(cantor_path, parent_path, branch_idx, depth, task_hash, state, cx, cy, cz, radius, version, lease_epoch) "
                "VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, 1, 0)",
                (
                    child_path,
                    parent_path,
                    spec.branch_idx,
                    parent_depth + 1,
                    task_hash,
                    child_sphere.cx,
                    child_sphere.cy,
                    child_sphere.cz,
                    child_sphere.radius,
                ),
            )
            return SpawnOutcome(status="SPAWNED", cantor_path=child_path, sphere=child_sphere), True

        # Case D — Distinct Concurrent Caller (LIVE, bound path): cycle check + wait_edges
        if self._has_wait_cycle(c, parent_path, ex_path):
            return (
                SpawnOutcome(
                    status="ERR_DEDUP_CYCLE",
                    cantor_path=ex_path,
                    error=f"wait_edges cycle between {parent_path!r} and {ex_path!r}",
                ),
                False,
            )
        edge = c.execute(
            "INSERT INTO wait_edges (waiter_path, target_hash) VALUES (?, ?) "
            "ON CONFLICT(waiter_path, target_hash) DO NOTHING",
            (parent_path, task_hash),
        )
        return SpawnOutcome(status="COLLAPSED_LIVE", cantor_path=ex_path), edge.rowcount == 1

    # ------------------------------------------------------------------
    # Section 2.4 & Section 3: Core Formal Operators
    # ------------------------------------------------------------------

    def spawn_child(
        self,
        parent_path: str,
        branch_idx: int,
        task_hash: bytes,
        *,
        expected_version: int | None = None,
        request_id: str | None = None,
        max_retries: int | None = None,
    ) -> SpawnOutcome:
        """Atomically spawn or deduplicate a single child under an ACTIVE or SPLIT parent (Section 2.4)."""
        err, child_path, child_sphere = self._prevalidate_child_geometry(parent_path, branch_idx)
        if err is not None or child_sphere is None:
            return SpawnOutcome(status=err or "ERR_DEPTH_FLOOR", cantor_path=None, error=err)

        now = int(time.time())
        spec = ChildSpec(
            branch_idx=branch_idx,
            task_hash=task_hash,
            request_id=request_id,
            max_retries=max_retries,
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            parent = conn.execute(
                "SELECT depth, state, version FROM swarm_nodes WHERE cantor_path = ?",
                (parent_path,),
            ).fetchone()
            if parent is None:
                conn.execute("ROLLBACK;")
                return SpawnOutcome(status="ERR_STALE_PARENT", cantor_path=None, error="parent not found")
            if expected_version is not None and int(parent["version"]) != expected_version:
                replay = conn.execute(
                    "SELECT n.* FROM task_dedup d JOIN swarm_nodes n "
                    "ON n.task_hash = d.task_hash AND n.cantor_path = d.cantor_path "
                    "WHERE d.task_hash = ? AND d.request_id = ? AND d.status = 'LIVE'",
                    (_stored_task_hash(conn, task_hash), request_id),
                ).fetchone() if request_id is not None else None
                conn.execute("ROLLBACK;")
                if replay is not None:
                    node = self._row_to_node(replay)
                    return SpawnOutcome(status="SPAWNED", cantor_path=node.cantor_path, sphere=node.sphere)
                return SpawnOutcome(status="ERR_STALE_PARENT", cantor_path=None, error="parent version mismatch")
            if parent["state"] not in ("ACTIVE", "SPLIT"):
                conn.execute("ROLLBACK;")
                return SpawnOutcome(status="ERR_STALE_PARENT", cantor_path=None, error=f"invalid parent state {parent['state']}")

            outcome, mutated = self._claim_child_in_tx(
                conn, parent_path, int(parent["depth"]), spec, child_path, child_sphere, now
            )
            if outcome.status in ("TOMBSTONED", "ERR_DEDUP_CYCLE") or not mutated:
                conn.execute("ROLLBACK;")
                return outcome

            if outcome.status == "SPAWNED":
                conn.execute(
                    "UPDATE swarm_nodes SET state = 'SPLIT', version = version + 1 WHERE cantor_path = ?",
                    (parent_path,),
                )
            elif outcome.status == "COLLAPSED_LIVE":
                conn.execute(
                    "UPDATE swarm_nodes SET version = version + 1 WHERE cantor_path = ?",
                    (parent_path,),
                )
            conn.execute("COMMIT;")
            return outcome
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def cut(
        self,
        parent_path: str,
        expected_version: int,
        lease_epoch: int,
        child_specs: Sequence[ChildSpec],
    ) -> CutOutcome:
        """Section 3 CUT: atomically split an ACTIVE node into 1..m children."""
        if not child_specs or len(child_specs) > self.params.m:
            return CutOutcome(ok=False, error="ERR_BRANCH_ARITY")
        seen_indices: set[int] = set()
        prepared: list[tuple[ChildSpec, str, BoundingSphere]] = []
        for spec in child_specs:
            if spec.branch_idx in seen_indices:
                return CutOutcome(ok=False, error="ERR_BRANCH_ARITY")
            seen_indices.add(spec.branch_idx)
            err, child_path, child_sphere = self._prevalidate_child_geometry(parent_path, spec.branch_idx)
            if err is not None or child_sphere is None:
                return CutOutcome(ok=False, error=err or "ERR_DEPTH_FLOOR")
            prepared.append((spec, child_path, child_sphere))

        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            parent = conn.execute(
                "SELECT depth, state, version, lease_epoch FROM swarm_nodes WHERE cantor_path = ?",
                (parent_path,),
            ).fetchone()
            if parent is None:
                conn.execute("ROLLBACK;")
                return CutOutcome(ok=False, error="ERR_STALE_PARENT")
            if (
                parent["state"] != "ACTIVE"
                or int(parent["version"]) != expected_version
                or int(parent["lease_epoch"]) != lease_epoch
            ):
                conn.execute("ROLLBACK;")
                return CutOutcome(ok=False, error="ERR_STALE_PARENT")

            child_cnt = int(
                conn.execute(
                    "SELECT COUNT(*) FROM swarm_nodes WHERE parent_path = ?", (parent_path,)
                ).fetchone()[0]
            )
            if child_cnt != 0:
                conn.execute("ROLLBACK;")
                return CutOutcome(ok=False, error="ERR_NON_EMPTY_PARENT")

            conn.execute(
                "UPDATE swarm_nodes SET state = 'SPLIT', version = version + 1 WHERE cantor_path = ?",
                (parent_path,),
            )

            outcomes: list[SpawnOutcome] = []
            spawned_new_nodes = 0
            added_wait_edges = 0
            for spec, child_path, child_sphere in prepared:
                out, mutated = self._claim_child_in_tx(
                    conn, parent_path, int(parent["depth"]), spec, child_path, child_sphere, now
                )
                if out.status == "ERR_DEDUP_CYCLE":
                    conn.execute("ROLLBACK;")
                    return CutOutcome(ok=False, error="ERR_DEDUP_CYCLE", children=(out,))
                if out.status == "SPAWNED" and mutated:
                    spawned_new_nodes += 1
                elif out.status == "COLLAPSED_LIVE" and mutated:
                    added_wait_edges += 1
                outcomes.append(out)

            # Abort cleanly if no children were spawned and no wait_edges were added
            # (prevents leaving an empty SPLIT parent when all children collapsed to tombstones).
            if spawned_new_nodes == 0 and added_wait_edges == 0:
                conn.execute("ROLLBACK;")
                return CutOutcome(
                    ok=False,
                    error="ALL_CHILDREN_COLLAPSED",
                    parent_version=expected_version,
                    children=tuple(outcomes),
                )

            conn.execute("COMMIT;")
            return CutOutcome(
                ok=True,
                parent_version=expected_version + 1,
                children=tuple(outcomes),
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def join(
        self,
        parent_path: str,
        expected_version: int,
        child_versions: Mapping[str, int],
        merge_result_digest: bytes | None = None,
        *,
        finalize: bool = False,
    ) -> JoinOutcome:
        """Section 3 JOIN: merge completed ACTIVE children back into SPLIT parent."""
        if finalize and parent_path == ROOT_PATH:
            return JoinOutcome(ok=False, error="ERR_CANNOT_FINALIZE_ROOT")

        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            parent = conn.execute(
                "SELECT parent_path, task_hash, state, version FROM swarm_nodes WHERE cantor_path = ?",
                (parent_path,),
            ).fetchone()
            if parent is None or parent["state"] != "SPLIT" or int(parent["version"]) != expected_version:
                conn.execute("ROLLBACK;")
                return JoinOutcome(ok=False, error="ERR_STALE_PARENT")

            children = conn.execute(
                "SELECT cantor_path, task_hash, state, version FROM swarm_nodes WHERE parent_path = ?",
                (parent_path,),
            ).fetchall()
            if len(children) != len(child_versions):
                conn.execute("ROLLBACK;")
                return JoinOutcome(ok=False, error="ERR_CHILD_SET_MISMATCH")
            for ch in children:
                cp = str(ch["cantor_path"])
                if ch["state"] != "ACTIVE":
                    conn.execute("ROLLBACK;")
                    return JoinOutcome(ok=False, error=f"ERR_CHILD_NOT_ACTIVE:{cp}")
                if child_versions.get(cp) != int(ch["version"]):
                    conn.execute("ROLLBACK;")
                    return JoinOutcome(ok=False, error=f"ERR_STALE_CHILD:{cp}")

            if self._has_open_waits(conn, parent_path):
                conn.execute("ROLLBACK;")
                return JoinOutcome(ok=False, error="ERR_OPEN_WAIT_EDGES")

            joined_paths: list[str] = []
            for ch in children:
                cp = str(ch["cantor_path"])
                conn.execute(
                    "UPDATE task_dedup SET status = 'JOINED', tombstoned_at = ? WHERE task_hash = ?",
                    (now, ch["task_hash"]),
                )
                conn.execute("DELETE FROM swarm_nodes WHERE cantor_path = ?", (cp,))
                joined_paths.append(cp)

            conn.execute("DELETE FROM wait_edges WHERE waiter_path = ?", (parent_path,))

            if finalize:
                grandparent = None if parent["parent_path"] is None else str(parent["parent_path"])
                conn.execute(
                    "UPDATE task_dedup SET status = 'JOINED', result_digest = ?, tombstoned_at = ? "
                    "WHERE task_hash = ?",
                    (merge_result_digest, now, parent["task_hash"]),
                )
                conn.execute("DELETE FROM swarm_nodes WHERE cantor_path = ?", (parent_path,))
                self._recover_empty_split_parent(conn, grandparent)
                conn.execute("COMMIT;")
                return JoinOutcome(ok=True, parent_state="JOINED", joined_paths=tuple(joined_paths))

            conn.execute(
                "UPDATE swarm_nodes SET state = 'ACTIVE', version = version + 1 WHERE cantor_path = ?",
                (parent_path,),
            )
            if merge_result_digest is not None:
                conn.execute(
                    "UPDATE task_dedup SET result_digest = ? WHERE task_hash = ?",
                    (merge_result_digest, parent["task_hash"]),
                )
            conn.execute("COMMIT;")
            return JoinOutcome(ok=True, parent_state="ACTIVE", joined_paths=tuple(joined_paths))
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def prune(self, cantor_path: str, expected_version: int) -> PruneOutcome:
        """Section 3 PRUNE: atomically eliminate a closed subtree using strict delimiter-safe prefix matching."""
        if cantor_path == ROOT_PATH:
            return PruneOutcome(ok=False, error="ERR_CANNOT_PRUNE_ROOT")

        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            target = conn.execute(
                "SELECT parent_path, version FROM swarm_nodes WHERE cantor_path = ?",
                (cantor_path,),
            ).fetchone()
            if target is None or int(target["version"]) != expected_version:
                conn.execute("ROLLBACK;")
                return PruneOutcome(ok=False, error="ERR_STALE_NODE")

            parent_path = None if target["parent_path"] is None else str(target["parent_path"])
            rows = conn.execute(
                "SELECT cantor_path, task_hash "
                "FROM swarm_nodes "
                "WHERE cantor_path = ? OR cantor_path LIKE ? || '.%' "
                "ORDER BY depth DESC",
                (cantor_path, cantor_path),
            ).fetchall()

            pruned_paths: list[str] = []
            for r in rows:
                cp = str(r["cantor_path"])
                conn.execute(
                    "UPDATE task_dedup SET status = 'PRUNED', tombstoned_at = ? WHERE task_hash = ?",
                    (now, r["task_hash"]),
                )
                conn.execute("DELETE FROM swarm_nodes WHERE cantor_path = ?", (cp,))
                pruned_paths.append(cp)

            self._recover_empty_split_parent(conn, parent_path)
            conn.execute("COMMIT;")
            return PruneOutcome(ok=True, pruned_paths=tuple(pruned_paths))
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def release_transient(
        self,
        cantor_path: str,
        owner_id: str | None,
        lease_epoch: int,
        *,
        permanent_abort: bool = False,
    ) -> ReleaseOutcome:
        """Section 2.5 RELEASE_TRANSIENT: distinguish transient retryable failure from permanent tombstone."""
        if cantor_path == ROOT_PATH:
            return ReleaseOutcome(ok=False, status="ERR_CANNOT_RELEASE_ROOT", error="cannot release root")

        now = int(time.time())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            node = conn.execute(
                "SELECT parent_path, task_hash, state, owner_id, lease_epoch, started_at "
                "FROM swarm_nodes WHERE cantor_path = ?",
                (cantor_path,),
            ).fetchone()
            if node is None:
                conn.execute("ROLLBACK;")
                return ReleaseOutcome(ok=False, status="ERR_NOT_FOUND", error="node not found")
            if node["state"] != "ACTIVE":
                conn.execute("ROLLBACK;")
                return ReleaseOutcome(ok=False, status="ERR_INVALID_STATE", error=f"state={node['state']}")
            if node["owner_id"] != owner_id or int(node["lease_epoch"]) != lease_epoch:
                conn.execute("ROLLBACK;")
                return ReleaseOutcome(ok=False, status="ERR_LEASE_MISMATCH", error="owner or lease_epoch mismatch")

            parent_path = None if node["parent_path"] is None else str(node["parent_path"])
            t_hash = bytes(node["task_hash"])

            conn.execute(
                "UPDATE task_dedup SET retry_count = retry_count + 1 WHERE task_hash = ?",
                (t_hash,),
            )
            dedup = conn.execute(
                "SELECT retry_count, max_retries FROM task_dedup WHERE task_hash = ?",
                (t_hash,),
            ).fetchone()
            assert dedup is not None
            retry_count = int(dedup["retry_count"])
            max_retries = int(dedup["max_retries"])

            if permanent_abort or retry_count >= max_retries:
                conn.execute(
                    "UPDATE task_dedup SET status = 'PRUNED', tombstoned_at = ? WHERE task_hash = ?",
                    (now, t_hash),
                )
                conn.execute("DELETE FROM swarm_nodes WHERE cantor_path = ?", (cantor_path,))
                self._recover_empty_split_parent(conn, parent_path)
                conn.execute("COMMIT;")
                return ReleaseOutcome(ok=True, status="TOMBSTONED_PRUNED", retry_count=retry_count)

            # Transient failure with retries remaining: never tombstone
            if node["started_at"] is None:
                # Unstarted: release spatial volume and unbind cantor_path while keeping status='LIVE'
                conn.execute("DELETE FROM swarm_nodes WHERE cantor_path = ?", (cantor_path,))
                conn.execute(
                    "UPDATE task_dedup SET cantor_path = NULL WHERE task_hash = ?",
                    (t_hash,),
                )
                self._recover_empty_split_parent(conn, parent_path)
            else:
                # Already started: keep node in swarm_nodes and release lease
                conn.execute(
                    "UPDATE swarm_nodes SET owner_id = NULL, lease_until = NULL, version = version + 1 "
                    "WHERE cantor_path = ?",
                    (cantor_path,),
                )
            conn.execute("COMMIT;")
            return ReleaseOutcome(ok=True, status="RELEASED_FOR_RETRY", retry_count=retry_count)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Thin Runner Lifecycle Hooks (Part A Rule 2)
    # ------------------------------------------------------------------

    def try_cut(
        self,
        op_type: str,
        normalized_inputs: Any,
        *,
        owner_id: str,
        constraints: Iterable[Any] = (),
        schema_version: str = "1",
        request_id: str | None = None,
        max_retries: int | None = None,
        ttl_sec: int = 300,
        preferred_parent: str | None = None,
    ) -> TryCutResult:
        """Thin runner hook: atomically reserve a geometric slot & lease for a task.

        Automatically allocates under open structural parents up to depth max_depth so
        callers never embed geometry or SQL in the runner loop.
        """
        t_hash = canonical_task_hash(
            op_type, normalized_inputs, constraints=constraints, schema_version=schema_version
        )
        eff_max_retries = self.default_max_retries if max_retries is None else int(max_retries)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            now = int(time.time())
            lease_until = now + max(1, int(ttl_sec))

            # 1. Fast-path check if task_hash already exists in task_dedup
            t_hash = _stored_task_hash(conn, t_hash)
            existing = conn.execute(
                "SELECT cantor_path, request_id, status, result_digest "
                "FROM task_dedup WHERE task_hash = ?",
                (t_hash,),
            ).fetchone()
            if existing is not None:
                ex_path = None if existing["cantor_path"] is None else str(existing["cantor_path"])
                ex_req = None if existing["request_id"] is None else str(existing["request_id"])
                ex_status = str(existing["status"])
                ex_digest = None if existing["result_digest"] is None else bytes(existing["result_digest"])

                if ex_status in ("JOINED", "PRUNED"):
                    conn.execute("ROLLBACK;")
                    return TryCutResult(
                        allowed=False,
                        status="TOMBSTONED",
                        cantor_path=ex_path,
                        lease_epoch=0,
                        task_hash=t_hash,
                        reason=f"duplicate_tombstoned:{ex_status.lower()}",
                        is_dedup=True,
                        result_digest=ex_digest,
                    )

                if ex_path is not None:
                    # A replay may launch work only after acquiring a released or expired lease.
                    node = conn.execute(
                        "SELECT n.state, n.owner_id, n.lease_epoch, n.lease_until, "
                        "(n.cantor_path = '' OR b.cantor_path IS NOT NULL) AS structural "
                        "FROM swarm_nodes n LEFT JOIN runner_branches b USING (cantor_path) "
                        "WHERE n.cantor_path = ? AND n.task_hash = ?",
                        (ex_path, t_hash),
                    ).fetchone()
                    if (
                        node is not None
                        and not node["structural"]
                        and node["state"] == "ACTIVE"
                        and (node["owner_id"] is None or (node["lease_until"] is not None and int(node["lease_until"]) <= now))
                    ):
                        new_epoch = int(node["lease_epoch"]) + 1
                        conn.execute(
                            "UPDATE swarm_nodes SET owner_id = ?, lease_epoch = ?, lease_until = ?, version = version + 1 "
                            "WHERE cantor_path = ?",
                            (owner_id, new_epoch, lease_until, ex_path),
                        )
                        if request_id is not None and request_id != ex_req:
                            conn.execute(
                                "UPDATE task_dedup SET request_id = ? WHERE task_hash = ?",
                                (request_id, t_hash),
                            )
                        conn.execute("COMMIT;")
                        return TryCutResult(
                            allowed=True,
                            status="SPAWNED",
                            cantor_path=ex_path,
                            lease_epoch=new_epoch,
                            task_hash=t_hash,
                            reason="reclaimed_lease",
                            is_dedup=False,
                        )
                    conn.execute("ROLLBACK;")
                    replay = request_id is not None and ex_req == request_id
                    return TryCutResult(
                        allowed=False,
                        status="COLLAPSED_LIVE",
                        cantor_path=ex_path,
                        lease_epoch=int(node["lease_epoch"]) if replay and node is not None else 0,
                        task_hash=t_hash,
                        reason="idempotent_retry" if replay else "duplicate_active",
                        is_dedup=True,
                    )

            # 2. Find an open (parent_path, branch_idx) slot (BFS by depth)
            slot = self._find_open_slot_in_tx(conn, now, preferred_parent=preferred_parent)
            if slot is None:
                conn.execute("ROLLBACK;")
                return TryCutResult(
                    allowed=False,
                    status="ERR_DEPTH_FLOOR",
                    cantor_path=None,
                    lease_epoch=0,
                    task_hash=t_hash,
                    reason="capacity_or_depth_floor",
                    is_dedup=False,
                )
            parent_path, parent_depth, branch_idx = slot
            err, child_path, child_sphere = self._prevalidate_child_geometry(parent_path, branch_idx)
            if err is not None or child_sphere is None:
                conn.execute("ROLLBACK;")
                return TryCutResult(
                    allowed=False,
                    status=err or "ERR_DEPTH_FLOOR",
                    cantor_path=None,
                    lease_epoch=0,
                    task_hash=t_hash,
                    reason=err or "ERR_DEPTH_FLOOR",
                    is_dedup=False,
                )

            if existing is None:
                conn.execute(
                    "INSERT INTO task_dedup "
                    "(task_hash, cantor_path, request_id, status, retry_count, max_retries, created_at) "
                    "VALUES (?, ?, ?, 'LIVE', 0, ?, ?)",
                    (t_hash, child_path, request_id, eff_max_retries, now),
                )
            else:
                # Re-bind previously released unstarted spatial volume
                conn.execute(
                    "UPDATE task_dedup SET cantor_path = ?, request_id = COALESCE(?, request_id) "
                    "WHERE task_hash = ?",
                    (child_path, request_id, t_hash),
                )

            conn.execute(
                "INSERT INTO swarm_nodes "
                "(cantor_path, parent_path, branch_idx, depth, task_hash, state, cx, cy, cz, radius, version, owner_id, lease_epoch, lease_until) "
                "VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, 1, ?, 1, ?)",
                (
                    child_path,
                    parent_path,
                    branch_idx,
                    parent_depth + 1,
                    t_hash,
                    child_sphere.cx,
                    child_sphere.cy,
                    child_sphere.cz,
                    child_sphere.radius,
                    owner_id,
                    lease_until,
                ),
            )
            conn.execute(
                "UPDATE swarm_nodes SET state = 'SPLIT', version = version + 1 WHERE cantor_path = ?",
                (parent_path,),
            )
            epoch = int(conn.execute(
                "SELECT lease_epoch FROM swarm_nodes WHERE cantor_path = ?", (child_path,)
            ).fetchone()[0])
            conn.execute("COMMIT;")
            return TryCutResult(
                allowed=True,
                status="SPAWNED",
                cantor_path=child_path,
                lease_epoch=epoch,
                task_hash=t_hash,
                reason="created",
                is_dedup=False,
            )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def _search_slots_in_tx(
        self, c: sqlite3.Connection, now: int, initial_parents: list[tuple[str, int]]
    ) -> tuple[str, int, int] | None:
        parents = list(initial_parents)
        while True:
            expansion = None
            for path, depth in parents:
                used = {row[0] for row in c.execute(
                    "SELECT branch_idx FROM swarm_nodes WHERE parent_path = ?", (path,),
                )}
                for idx in range(self.params.m):
                    if idx in used:
                        continue
                    # Reserve the last slot for expansion. Alternating branch levels let
                    # even a binary registry grow a tree of independent worker leaves.
                    if depth + 1 == self.params.max_depth or (depth % 2 == 0 and idx < self.params.m - 1):
                        return path, depth, idx
                    if expansion is None:
                        expansion = (path, depth, idx)
            if expansion is None:
                return None
            path, depth, idx = expansion
            error, child_path, sphere = self._prevalidate_child_geometry(path, idx)
            if error or sphere is None:
                return None
            previous = c.execute(
                "SELECT generation FROM retired_paths WHERE cantor_path = ?", (child_path,),
            ).fetchone()
            generation = previous[0] + 1 if previous else 1
            branch_hash = hashlib.sha256(f"runner-branch:{child_path}:{generation}".encode("ascii")).digest()
            outcome, created = self._claim_child_in_tx(
                c, path, depth, ChildSpec(idx, branch_hash), child_path, sphere, now,
            )
            if not created or outcome.status != "SPAWNED":
                return None
            c.execute("INSERT INTO runner_branches (cantor_path) VALUES (?)", (child_path,))
            c.execute(
                "UPDATE swarm_nodes SET state = 'SPLIT', version = version + 1 WHERE cantor_path = ?", (path,),
            )
            parents = [(child_path, depth + 1)]

    def _find_open_slot_in_tx(
        self, c: sqlite3.Connection, now: int, preferred_parent: str | None = None
    ) -> tuple[str, int, int] | None:
        """Allocate below the root or a structural branch; executable leaves never become parents."""
        if preferred_parent is not None and preferred_parent != "":
            pref_row = c.execute(
                "SELECT cantor_path, depth FROM swarm_nodes WHERE cantor_path = ? AND depth < ?",
                (preferred_parent, self.params.max_depth),
            ).fetchone()
            if pref_row is not None:
                c.execute("INSERT OR IGNORE INTO runner_branches (cantor_path) VALUES (?)", (preferred_parent,))
                slot = self._search_slots_in_tx(c, now, [(pref_row[0], pref_row[1])])
                if slot is not None:
                    return slot
        parents = c.execute(
            "SELECT cantor_path, depth FROM swarm_nodes WHERE depth < ? AND "
            "(cantor_path = '' OR cantor_path IN (SELECT cantor_path FROM runner_branches)) "
            "ORDER BY depth, cantor_path", (self.params.max_depth,),
        ).fetchall()
        return self._search_slots_in_tx(c, now, [(r[0], r[1]) for r in parents])

    def settle_or_join(
        self,
        cantor_path: str,
        *,
        owner_id: str | None,
        lease_epoch: int,
        outcome: str,
        result_digest: bytes | None = None,
    ) -> ReleaseOutcome | JoinOutcome:
        """Thin runner hook: settle a leaf on completion, semantic failure, transient failure, or cancellation."""
        if outcome == "completed":
            if cantor_path == ROOT_PATH:
                return JoinOutcome(ok=False, error="ERR_CANNOT_FINALIZE_ROOT")
            now = int(time.time())
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE;")
                node = conn.execute(
                    "SELECT parent_path, task_hash, state, owner_id, lease_epoch "
                    "FROM swarm_nodes WHERE cantor_path = ?",
                    (cantor_path,),
                ).fetchone()
                if node is None:
                    conn.execute("ROLLBACK;")
                    return JoinOutcome(ok=True, parent_state=None, joined_paths=())
                if node["owner_id"] != owner_id or int(node["lease_epoch"]) != lease_epoch:
                    conn.execute("ROLLBACK;")
                    return JoinOutcome(ok=False, error="ERR_LEASE_MISMATCH")
                if node["state"] != "ACTIVE":
                    conn.execute("ROLLBACK;")
                    return JoinOutcome(ok=False, error=f"ERR_INVALID_STATE:{node['state']}")
                if self._has_open_waits(conn, cantor_path):
                    conn.execute("ROLLBACK;")
                    return JoinOutcome(ok=False, error="ERR_OPEN_WAIT_EDGES")

                parent_path = None if node["parent_path"] is None else str(node["parent_path"])
                conn.execute(
                    "UPDATE task_dedup SET status = 'JOINED', result_digest = ?, tombstoned_at = ? "
                    "WHERE task_hash = ?",
                    (result_digest, now, node["task_hash"]),
                )
                conn.execute("DELETE FROM swarm_nodes WHERE cantor_path = ?", (cantor_path,))
                self._recover_empty_split_parent(conn, parent_path)
                conn.execute("COMMIT;")
                return JoinOutcome(ok=True, parent_state="JOINED", joined_paths=(cantor_path,))
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK;")
                raise
            finally:
                conn.close()

        if outcome == "semantic_failure":
            return self.release_transient(
                cantor_path, owner_id, lease_epoch, permanent_abort=True
            )

        return self.release_transient(
            cantor_path, owner_id, lease_epoch, permanent_abort=False
        )

    # ------------------------------------------------------------------
    # Formal Invariant Verifier (Part C Deliverable 1)
    # ------------------------------------------------------------------

    def verify_invariants(self) -> list[str]:
        """Verify all mathematical, geometric, and topological invariants. Empty list == valid."""
        errors: list[str] = []
        with self._read() as c:
            c.execute("BEGIN;")
            nodes_rows = c.execute("SELECT * FROM swarm_nodes").fetchall()
            dedup_rows = c.execute("SELECT * FROM task_dedup").fetchall()
            wait_rows = c.execute("SELECT waiter_path, target_hash FROM wait_edges").fetchall()

        nodes: dict[str, NodeRecord] = {
            str(r["cantor_path"]): self._row_to_node(r) for r in nodes_rows
        }
        dedup_by_hash: dict[bytes, dict[str, Any]] = {
            bytes(r["task_hash"]): dict(r) for r in dedup_rows
        }

        # 1. Root existence & |V| - |E| = 1 (genus 0)
        if ROOT_PATH not in nodes:
            errors.append("missing root node ''")
            return errors

        edges: list[tuple[str, str]] = []
        children_map: dict[str, list[NodeRecord]] = {p: [] for p in nodes}
        for path, node in nodes.items():
            if path == ROOT_PATH:
                if node.parent_path is not None or node.depth != 0:
                    errors.append(f"root invariants violated: parent={node.parent_path}, depth={node.depth}")
            else:
                if node.parent_path is None or node.parent_path not in nodes:
                    errors.append(f"orphan node {path!r} (parent={node.parent_path!r})")
                else:
                    edges.append((node.parent_path, path))
                    children_map[node.parent_path].append(node)
                    parent = nodes[node.parent_path]
                    if node.depth != parent.depth + 1:
                        errors.append(f"depth monotonicity violated for {path!r}: {node.depth} != {parent.depth + 1}")
                    expected_path = format_child_path(node.parent_path, node.branch_idx or 0)
                    if node.branch_idx is None or not (0 <= node.branch_idx < self.params.m):
                        errors.append(f"branch_idx out of range for {path!r}: {node.branch_idx}")
                    elif path != expected_path:
                        errors.append(f"path formatting mismatch: {path!r} != {expected_path!r}")
                    if not parent.sphere.strictly_contains(node.sphere):
                        errors.append(f"strict containment violated: {path!r} not strictly inside {node.parent_path!r}")

            if node.depth > self.params.max_depth:
                errors.append(f"depth exceeds max_depth for {path!r}: {node.depth} > {self.params.max_depth}")
            if node.sphere.radius < self.params.min_radius:
                errors.append(f"radius below min_radius for {path!r}: {node.sphere.radius} < {self.params.min_radius}")

            # Cross-check task_dedup correspondence
            dd = dedup_by_hash.get(node.task_hash)
            if dd is None or dd["status"] != "LIVE" or dd["cantor_path"] != path:
                errors.append(f"task_dedup mismatch for active node {path!r}")

        if len(nodes) - len(edges) != 1:
            errors.append(f"genus-0 Euler invariant violated: |V|={len(nodes)}, |E|={len(edges)}")

        # 2. Bounded arity & connectivity / acyclicity
        for path, chs in children_map.items():
            if len(chs) > self.params.m:
                errors.append(f"arity exceeded at {path!r}: {len(chs)} > {self.params.m}")

        visited: set[str] = set()
        stack = [ROOT_PATH]
        while stack:
            cur = stack.pop()
            if cur in visited:
                errors.append(f"cycle detected in topology at {cur!r}")
                break
            visited.add(cur)
            for ch in children_map.get(cur, ()):
                stack.append(ch.cantor_path)
        if len(visited) != len(nodes):
            errors.append(f"disconnected topology: reachable={len(visited)}, total={len(nodes)}")

        # 3. Active leaf frontier: prefix-free & pairwise strictly disjoint 3D spheres
        active_leaves = [n for n in nodes.values() if n.state == "ACTIVE"]
        for i, a in enumerate(active_leaves):
            for b in active_leaves[i + 1 :]:
                if is_prefix_or_equal(a.cantor_path, b.cantor_path) or is_prefix_or_equal(b.cantor_path, a.cantor_path):
                    errors.append(f"active frontier not prefix-free: {a.cantor_path!r} vs {b.cantor_path!r}")
                if not a.sphere.strictly_disjoint(b.sphere):
                    errors.append(
                        f"sphere overlap between active leaves {a.cantor_path!r} and {b.cantor_path!r}"
                    )

        # 4. Zero cycles in E union wait_edges
        adj: dict[str, list[str]] = {p: [ch.cantor_path for ch in chs] for p, chs in children_map.items()}
        for wr in wait_rows:
            waiter = str(wr["waiter_path"])
            dd = dedup_by_hash.get(bytes(wr["target_hash"]))
            if dd is not None and dd["status"] == "LIVE" and dd["cantor_path"] is not None:
                adj.setdefault(waiter, []).append(str(dd["cantor_path"]))

        color: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=done
        for start in list(adj.keys()):
            if color.get(start, 0) != 0:
                continue
            dfs_stack: list[tuple[str, int]] = [(start, 0)]
            color[start] = 1
            while dfs_stack:
                u, idx = dfs_stack[-1]
                nbrs = adj.get(u, ())
                if idx < len(nbrs):
                    dfs_stack[-1] = (u, idx + 1)
                    v = nbrs[idx]
                    c_v = color.get(v, 0)
                    if c_v == 1:
                        errors.append(f"cycle detected in E U wait_edges involving {u!r} -> {v!r}")
                        break
                    if c_v == 0:
                        color[v] = 1
                        dfs_stack.append((v, 0))
                else:
                    color[u] = 2
                    dfs_stack.pop()

        return errors
