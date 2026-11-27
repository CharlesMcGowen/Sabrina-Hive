"""Integration, concurrency, geometry, and fail-open pytest suite for JoinCutRegistry & LiveSwarmRunner."""

from __future__ import annotations

import logging
import math
import random
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sabrina_console_desktop.join_cut_registry import (
    ROOT_PATH,
    ChildSpec,
    JoinCutRegistry,
    OrthonormalFrame,
    canonical_task_hash,
    compute_embedding_params,
    embed_cantor_path,
    embed_child,
    format_child_path,
)
from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner


# ---------------------------------------------------------------------------
# 1. Geometry, Formatting, Precision Floor & Arity Guard
# ---------------------------------------------------------------------------


def test_path_formatting_no_leading_dot() -> None:
    assert format_child_path(ROOT_PATH, 0) == "0"
    assert format_child_path(ROOT_PATH, 11) == "11"
    assert format_child_path("0", 1) == "0.1"
    assert format_child_path("0.1", 2) == "0.1.2"
    assert not format_child_path(ROOT_PATH, 0).startswith(".")


def test_multi_level_cuts_zero_overlap_and_frame_orthonormality(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "geom.sqlite3", m=4, r0=1.0, gamma=0.90)
    # Build 3 full levels (root -> 4 -> 16 -> 64 leaves = 85 nodes)
    frontier = [ROOT_PATH]
    seq = 0
    for _ in range(3):
        next_frontier: list[str] = []
        for parent in frontier:
            node = reg.get_node(parent)
            assert node is not None
            specs = [
                ChildSpec(branch_idx=j, task_hash=canonical_task_hash("work", {"seq": seq + j}))
                for j in range(4)
            ]
            seq += 4
            out = reg.cut(parent, node.version, node.lease_epoch, specs)
            assert out.ok, out.error
            next_frontier.extend(ch.cantor_path for ch in out.children if ch.cantor_path is not None)
        frontier = next_frontier

    assert reg.verify_invariants() == []

    # Check Gram-Schmidt frame orthonormality along a deep spine up to max_depth
    params = compute_embedding_params(m=7)
    deep_path = ".".join(str(1 + level % 6) for level in range(params.max_depth))
    sphere, frame = embed_cantor_path(deep_path, params)
    assert sphere.radius >= params.min_radius
    for vec in (frame.u, frame.v, frame.w):
        assert math.isclose(math.sqrt(sum(c * c for c in vec)), 1.0, abs_tol=1e-12)
    assert abs(sum(a * b for a, b in zip(frame.u, frame.v))) < 1e-12
    assert abs(sum(a * b for a, b in zip(frame.u, frame.w))) < 1e-12
    assert abs(sum(a * b for a, b in zip(frame.v, frame.w))) < 1e-12


def test_embedding_corrects_perturbed_parent_frame() -> None:
    params = compute_embedding_params(m=7)
    sphere, _ = embed_cantor_path(ROOT_PATH, params)
    perturbed = OrthonormalFrame(u=(1.0000001, 0, 0), v=(0, 1, 0), w=(1e-7, 0, 1))
    _, frame = embed_child(sphere, perturbed, 1, params)
    for vector in (frame.u, frame.v, frame.w):
        assert abs(math.hypot(*vector) - 1) < 1e-12
    for a, b in ((frame.u, frame.v), (frame.u, frame.w), (frame.v, frame.w)):
        assert abs(sum(x * y for x, y in zip(a, b))) < 1e-12


def test_depth_floor_and_arity_guard(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "floor.sqlite3", m=4)
    root = reg.get_node(ROOT_PATH)
    assert root is not None

    # Branch arity guard (Python + SQLite trigger)
    bad = reg.cut(
        ROOT_PATH,
        root.version,
        root.lease_epoch,
        [ChildSpec(branch_idx=4, task_hash=canonical_task_hash("op", {"i": 1}))],
    )
    assert not bad.ok
    assert bad.error == "ERR_BRANCH_ARITY"

    # Verify SQLite trigger directly blocks out-of-range branch_idx
    conn = sqlite3.connect(reg.db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        h = canonical_task_hash("raw", {"x": 99})
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            "INSERT INTO task_dedup (task_hash, cantor_path, status, retry_count, max_retries, created_at) "
            "VALUES (?, '99', 'LIVE', 0, 3, 1)",
            (h,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="BRANCH_IDX_OUT_OF_RANGE"):
            conn.execute(
                "INSERT INTO swarm_nodes "
                "(cantor_path, parent_path, branch_idx, depth, task_hash, state, cx, cy, cz, radius, version, lease_epoch) "
                "VALUES ('99', '', 99, 1, ?, 'ACTIVE', 0.0, 0.0, 0.0, 0.1, 1, 0)",
                (h,),
            )
        conn.execute("ROLLBACK;")
    finally:
        conn.close()

    # Walk down to max_depth and verify depth+1 returns ERR_DEPTH_FLOOR
    cur = ROOT_PATH
    for step in range(reg.params.max_depth):
        n = reg.get_node(cur)
        assert n is not None
        out = reg.cut(
            cur,
            n.version,
            n.lease_epoch,
            [ChildSpec(branch_idx=0, task_hash=canonical_task_hash("spine", {"d": step}))],
        )
        assert out.ok, out.error
        cur = out.children[0].cantor_path or ""

    leaf = reg.get_node(cur)
    assert leaf is not None
    too_deep = reg.cut(
        cur,
        leaf.version,
        leaf.lease_epoch,
        [ChildSpec(branch_idx=0, task_hash=canonical_task_hash("spine", {"d": 999}))],
    )
    assert not too_deep.ok
    assert too_deep.error == "ERR_DEPTH_FLOOR"
    assert reg.verify_invariants() == []


# ---------------------------------------------------------------------------
# 2. Concurrency, Atomic Dedup, Idempotent Retries & Cycle Detection
# ---------------------------------------------------------------------------


def test_120_concurrent_identical_spawns_collapse_to_one_and_emit_dedup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reg = JoinCutRegistry(tmp_path / "dedup.sqlite3", m=6)
    runner = LiveSwarmRunner(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        join_cut_enabled=True,
        join_cut_registry=reg,
    )
    ws = {"key": "ws_alpha", "role": "research_scout", "title": "scan order books"}
    start = threading.Barrier(120, timeout=30)

    with caplog.at_level(logging.INFO, logger="sabrina_console_desktop.live_swarm_runner"):
        def submit(i: int) -> tuple[bool, str]:
            start.wait()
            return runner._join_cut_claim("m-concurrent", ws, f"m-concurrent-w{i}")

        with ThreadPoolExecutor(max_workers=120) as pool:
            outcomes = list(pool.map(submit, range(120)))

    allowed = [o for o in outcomes if o[0]]
    denied = [o for o in outcomes if not o[0]]
    assert len(allowed) == 1, f"expected exactly 1 winner, got {len(allowed)}"
    assert len(denied) == 119
    assert all(r == "duplicate_active" for _, r in denied)
    dedup_logs = [rec for rec in caplog.records if "JOIN_CUT_DEDUP" in rec.getMessage()]
    assert len(dedup_logs) == 119
    assert reg.verify_invariants() == []


def test_idempotent_retry_with_matching_request_id(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "idem.sqlite3", m=4)
    h = canonical_task_hash("compute", {"x": 42})

    first = reg.spawn_child(ROOT_PATH, 0, h, request_id="req-abc-123")
    assert first.status == "SPAWNED"
    assert first.cantor_path == "0"

    # Same request_id returns (SPAWNED, existing_path) idempotently without creating wait_edges
    retry = reg.spawn_child(ROOT_PATH, 1, h, request_id="req-abc-123")
    assert retry.status == "SPAWNED"
    assert retry.cantor_path == "0"

    # Distinct request_id from a sibling branch collapses to COLLAPSED_LIVE
    other_h = canonical_task_hash("compute", {"x": 99})
    sib = reg.spawn_child(ROOT_PATH, 1, other_h)
    assert sib.status == "SPAWNED"
    assert sib.cantor_path == "1"

    dup = reg.spawn_child("1", 0, h, request_id="req-different")
    assert dup.status == "COLLAPSED_LIVE"
    assert dup.cantor_path == "0"

    # Reverse dependency from "0" -> "1" must be rejected as ERR_DEDUP_CYCLE
    cyc = reg.spawn_child("0", 0, other_h, request_id="req-cycle")
    assert cyc.status == "ERR_DEDUP_CYCLE"
    assert reg.verify_invariants() == []


def test_tombstone_immutability_triggers(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "tomb.sqlite3", m=4)
    h = canonical_task_hash("job", {"id": 7})
    res = reg.spawn_child(ROOT_PATH, 0, h)
    assert res.status == "SPAWNED"

    n = reg.get_node("0")
    assert n is not None
    pruned = reg.prune("0", n.version)
    assert pruned.ok

    # Re-spawning tombstoned hash is rejected
    again = reg.spawn_child(ROOT_PATH, 1, h)
    assert again.status == "TOMBSTONED"
    assert again.tombstone_status == "PRUNED"

    # Direct SQLite DELETE or status resurrection is blocked by triggers
    conn = sqlite3.connect(reg.db_path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="TOMBSTONE_DELETE_FORBIDDEN"):
            conn.execute("DELETE FROM task_dedup WHERE task_hash = ?", (h,))
        with pytest.raises(sqlite3.IntegrityError, match="TOMBSTONE_RESURRECTION_FORBIDDEN"):
            conn.execute("UPDATE task_dedup SET status = 'LIVE' WHERE task_hash = ?", (h,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. Transient Release, Empty SPLIT Recovery & Safe Prefix Matching
# ---------------------------------------------------------------------------


def test_unstarted_transient_failure_releases_volume_and_recovers_empty_split_parent(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "transient.sqlite3", m=4, default_max_retries=3)
    h = canonical_task_hash("fetch", {"ticker": "BTC"})

    root = reg.get_node(ROOT_PATH)
    assert root is not None
    cut_res = reg.cut(ROOT_PATH, root.version, root.lease_epoch, [ChildSpec(branch_idx=0, task_hash=h)])
    assert cut_res.ok
    assert reg.get_node(ROOT_PATH).state == "SPLIT"

    ok_lease, epoch = reg.acquire_lease("0", "worker-1", mark_started=False)
    assert ok_lease

    # Unstarted transient failure: deletes node, unbinds cantor_path, keeps status='LIVE',
    # and transitions empty SPLIT parent back to ACTIVE.
    rel = reg.release_transient("0", "worker-1", epoch, permanent_abort=False)
    assert rel.ok
    assert rel.status == "RELEASED_FOR_RETRY"
    assert rel.retry_count == 1
    assert reg.get_node("0") is None
    assert reg.get_node(ROOT_PATH).state == "ACTIVE"

    dd = reg.get_dedup(h)
    assert dd is not None
    assert dd["status"] == "LIVE"
    assert dd["cantor_path"] is None

    # Task can now be re-spawned and re-bound to a fresh slot
    root2 = reg.get_node(ROOT_PATH)
    assert root2 is not None
    cut_res2 = reg.cut(ROOT_PATH, root2.version, root2.lease_epoch, [ChildSpec(branch_idx=1, task_hash=h)])
    assert cut_res2.ok
    assert cut_res2.children[0].status == "SPAWNED"
    assert cut_res2.children[0].cantor_path == "1"

    # Started transient failure releases lease without deleting node; hitting max_retries tombstones
    ok_l2, ep2 = reg.acquire_lease("1", "worker-2", mark_started=True)
    assert ok_l2
    rel2 = reg.release_transient("1", "worker-2", ep2, permanent_abort=False)
    assert rel2.ok and rel2.status == "RELEASED_FOR_RETRY" and rel2.retry_count == 2
    n1 = reg.get_node("1")
    assert n1 is not None and n1.owner_id is None and n1.lease_until is None

    ok_l3, ep3 = reg.acquire_lease("1", "worker-3", mark_started=True)
    assert ok_l3
    rel3 = reg.release_transient("1", "worker-3", ep3, permanent_abort=False)
    assert rel3.ok and rel3.status == "TOMBSTONED_PRUNED" and rel3.retry_count == 3
    assert reg.get_node("1") is None
    assert reg.get_node(ROOT_PATH).state == "ACTIVE"
    assert reg.get_dedup(h)["status"] == "PRUNED"
    assert reg.verify_invariants() == []


def test_prune_delimiter_safe_prefix_does_not_touch_sibling_0_10(tmp_path: Path) -> None:
    """PRUNE('0.1') with m=12 must NOT touch '0.10' or '0.11'."""
    reg = JoinCutRegistry(tmp_path / "prefix.sqlite3", m=12)
    root = reg.get_node(ROOT_PATH)
    assert root is not None
    assert reg.cut(
        ROOT_PATH,
        root.version,
        root.lease_epoch,
        [ChildSpec(branch_idx=0, task_hash=canonical_task_hash("lvl1", {"b": 0}))],
    ).ok

    n0 = reg.get_node("0")
    assert n0 is not None
    specs = [
        ChildSpec(branch_idx=1, task_hash=canonical_task_hash("lvl2", {"b": 1})),
        ChildSpec(branch_idx=10, task_hash=canonical_task_hash("lvl2", {"b": 10})),
        ChildSpec(branch_idx=11, task_hash=canonical_task_hash("lvl2", {"b": 11})),
    ]
    assert reg.cut("0", n0.version, n0.lease_epoch, specs).ok

    # Add a child under 0.1 ("0.1.0") to verify descendants of 0.1 ARE pruned
    n01 = reg.get_node("0.1")
    assert n01 is not None
    assert reg.cut(
        "0.1",
        n01.version,
        n01.lease_epoch,
        [ChildSpec(branch_idx=0, task_hash=canonical_task_hash("lvl3", {"b": 0}))],
    ).ok

    n01_split = reg.get_node("0.1")
    assert n01_split is not None
    pruned = reg.prune("0.1", n01_split.version)
    assert pruned.ok
    assert set(pruned.pruned_paths) == {"0.1.0", "0.1"}

    # Critical assertion: "0.10" and "0.11" must remain completely intact!
    assert reg.get_node("0.1") is None
    assert reg.get_node("0.1.0") is None
    assert reg.get_node("0.10") is not None
    assert reg.get_node("0.11") is not None
    assert reg.verify_invariants() == []


# ---------------------------------------------------------------------------
# 4. Randomized Concurrent CUT / JOIN / PRUNE Stress Run
# ---------------------------------------------------------------------------


def test_randomized_concurrent_cut_join_prune_preserves_invariants(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "stress.sqlite3", m=4)

    def worker(seed: int) -> set[str]:
        rng = random.Random(seed)
        succeeded: set[str] = set()
        for step in range(25):
            roll = rng.random()
            nodes = reg.all_nodes()
            if roll < 0.50:
                actives = [n for n in nodes if n.state == "ACTIVE" and n.depth < 4]
                if actives:
                    target = rng.choice(actives)
                    k = rng.randint(1, 3)
                    specs = [
                        ChildSpec(
                            branch_idx=j,
                            task_hash=canonical_task_hash("stress", {"s": seed, "t": step, "j": j}),
                        )
                        for j in range(k)
                    ]
                    if reg.cut(target.cantor_path, target.version, target.lease_epoch, specs).ok:
                        succeeded.add("cut")
            elif roll < 0.78:
                splits = [n for n in nodes if n.state == "SPLIT"]
                if splits:
                    target = rng.choice(splits)
                    chs = reg.children_of(target.cantor_path)
                    if chs and all(c.state == "ACTIVE" for c in chs):
                        outcome = reg.join(
                            target.cantor_path,
                            target.version,
                            {c.cantor_path: c.version for c in chs},
                            finalize=(target.cantor_path != ROOT_PATH and rng.random() < 0.3),
                        )
                        if outcome.ok:
                            succeeded.add("join")
            else:
                non_root = [n for n in nodes if n.cantor_path != ROOT_PATH]
                if non_root:
                    target = rng.choice(non_root)
                    if reg.prune(target.cantor_path, target.version).ok:
                        succeeded.add("prune")
        return succeeded

    with ThreadPoolExecutor(max_workers=12) as pool:
        succeeded = set().union(*pool.map(worker, range(32)))

    assert succeeded == {"cut", "join", "prune"}
    assert reg.verify_invariants() == []


# ---------------------------------------------------------------------------
# 5. Runner Regression (join_cut_enabled=False) & Fail-Open Under DB Lock
# ---------------------------------------------------------------------------


def test_runner_regression_disabled_bypasses_registry_completely(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "disabled.sqlite3", m=4)
    runner = LiveSwarmRunner(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        join_cut_enabled=False,
        join_cut_registry=reg,
    )
    ws = {"key": "ws_dup", "role": "research_scout", "title": "same task twice"}
    assert runner._join_cut_claim("m-off", ws, "m-off-1") == (True, "disabled")
    assert runner._join_cut_claim("m-off", ws, "m-off-2") == (True, "disabled")
    runner._join_cut_settle("m-off", "m-off-1", outcome="completed")
    # Registry remains completely untouched (only root node present)
    assert [n.cantor_path for n in reg.all_nodes()] == [ROOT_PATH]


def test_runner_fails_open_with_real_sqlite_writer_lock(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reg = JoinCutRegistry(tmp_path / "locked.sqlite3", busy_timeout_ms=100)
    runner = LiveSwarmRunner(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        join_cut_enabled=True,
        join_cut_registry=reg,
    )
    ws = {"key": "ws_lock", "role": "research_scout", "title": "resilient task"}

    writer = sqlite3.connect(reg.db_path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING, logger="sabrina_console_desktop.live_swarm_runner"):
            allowed, reason = runner._join_cut_claim("m-lock", ws, "m-lock-w1")
        assert allowed is True
        assert "database is locked" in reason
    finally:
        writer.close()

    assert any("fail-open" in rec.getMessage() for rec in caplog.records)
    assert [node.cantor_path for node in reg.all_nodes()] == [ROOT_PATH]
    assert runner._join_cut_claim("m-lock", ws, "m-lock-w2")[0]


def test_desktop_bridge_deployment_health_exposes_all_wired_modules(tmp_path: Path) -> None:
    from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge

    bridge = SabrinaDesktopBridge(
        role_bindings_path=tmp_path / "role_bindings.json",
    )
    try:
        health = bridge.get_deployment_health()
        assert health["all_ok"] is True, health
        assert health["active_count"] == health["total_count"] == 5
        ids = {m["id"]: m["active"] for m in health["modules"]}
        assert ids == {
            "join_cut_registry": True,
            "promotion_ledger": True,
            "signal_promotion_gate": True,
            "gpu_providers": True,
            "m_eulerian_rsi_schema": True,
        }
        pl = bridge.get_promotion_ledger_status()
        assert pl["ok"] is True
        assert pl["active"] is True
    finally:
        bridge.swarm_runner.shutdown()


def test_request_replay_requires_an_owned_lease_before_dispatch(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "replay-lease.sqlite3")
    first = reg.try_cut("job", {"id": 1}, owner_id="a", request_id="req")
    assert first.allowed and first.cantor_path is not None
    for owner in ("a", "b"):
        replay = reg.try_cut("job", {"id": 1}, owner_id=owner, request_id="req")
        assert not replay.allowed and replay.is_dedup
        assert (replay.cantor_path, replay.lease_epoch) == (first.cantor_path, first.lease_epoch)
    assert reg.mark_started(first.cantor_path, "a", first.lease_epoch)
    assert reg.release_transient(first.cantor_path, "a", first.lease_epoch).ok
    retry = reg.try_cut("job", {"id": 1}, owner_id="b", request_id="req")
    assert retry.allowed and retry.cantor_path == first.cantor_path
    assert retry.lease_epoch > first.lease_epoch
    assert reg.mark_started(first.cantor_path, "b", retry.lease_epoch)


def test_runner_overflow_preserves_running_and_released_leaves(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "overflow.sqlite3", m=2)
    first = reg.try_cut("job", 0, owner_id="worker-0")
    assert first.allowed and first.cantor_path == "0"
    assert reg.mark_started(first.cantor_path, "worker-0", first.lease_epoch)
    assert reg.release_transient(first.cantor_path, "worker-0", first.lease_epoch).ok

    others = [reg.try_cut("job", i, owner_id=f"worker-{i}") for i in range(1, 5)]
    assert all(claim.allowed for claim in others)
    assert reg.get_node(first.cantor_path).state == "ACTIVE"
    for i, claim in enumerate(others, 1):
        assert claim.cantor_path is not None
        assert reg.settle_or_join(
            claim.cantor_path, owner_id=f"worker-{i}", lease_epoch=claim.lease_epoch, outcome="completed",
        ).ok
    assert [node.cantor_path for node in reg.all_nodes()] == [ROOT_PATH, first.cantor_path]
    retry = reg.try_cut("job", 0, owner_id="retry")
    assert retry.allowed and retry.cantor_path == first.cantor_path
    assert reg.settle_or_join(retry.cantor_path, owner_id="retry", lease_epoch=retry.lease_epoch, outcome="completed").ok
    assert [node.cantor_path for node in reg.all_nodes()] == [ROOT_PATH]
    root_replay = reg.try_cut("SWARM_ROOT", {"root": True}, owner_id="worker")
    assert not root_replay.allowed and root_replay.is_dedup
    assert reg.verify_invariants() == []


@pytest.mark.parametrize("pre_upgrade", [False, True], ids=["current-schema", "legacy-schema"])
def test_reused_path_fences_stale_prune_and_settlement(tmp_path: Path, pre_upgrade: bool) -> None:
    db = tmp_path / "generation.sqlite3"
    reg = JoinCutRegistry(db)
    first = reg.try_cut("old", {}, owner_id="worker")
    assert first.allowed and first.cantor_path is not None
    resident = reg.try_cut("resident", {}, owner_id="resident")
    assert resident.allowed and resident.cantor_path is not None
    old = reg.get_node(first.cantor_path)
    assert old is not None
    assert reg.release_transient(old.cantor_path, "worker", old.lease_epoch).ok
    if pre_upgrade:
        # Old releases erased the binding and kept no slot history. Preserve all task
        # and node rows while presenting exactly that schema to the next registry.
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TRIGGER IF EXISTS trg_nodes_retire_generation")
            conn.execute("DROP TRIGGER IF EXISTS trg_nodes_fence_reuse")
            conn.execute("DROP TABLE IF EXISTS retired_paths")
            conn.execute("DROP TABLE IF EXISTS runner_branches")
            if any(row[1] == "legacy_generation_floor" for row in conn.execute("PRAGMA table_info(swarm_meta)")):
                conn.execute("ALTER TABLE swarm_meta DROP COLUMN legacy_generation_floor")

    reg = JoinCutRegistry(db)
    replacement = reg.try_cut("new", {}, owner_id="worker")
    assert replacement.allowed and replacement.cantor_path == old.cantor_path
    assert reg.prune(old.cantor_path, old.version).error == "ERR_STALE_NODE"
    stale = reg.settle_or_join(old.cantor_path, owner_id="worker", lease_epoch=old.lease_epoch, outcome="completed")
    assert not stale.ok and stale.error == "ERR_LEASE_MISMATCH"
    assert reg.mark_started(old.cantor_path, "worker", replacement.lease_epoch)
    assert reg.settle_or_join(
        resident.cantor_path, owner_id="resident", lease_epoch=resident.lease_epoch, outcome="completed",
    ).ok


def test_leaf_completion_cannot_finalize_root(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "keep-root.sqlite3")
    root = reg.get_node(ROOT_PATH)
    assert root is not None
    outcome = reg.settle_or_join(ROOT_PATH, owner_id=root.owner_id, lease_epoch=root.lease_epoch, outcome="completed")
    assert not outcome.ok and outcome.error == "ERR_CANNOT_FINALIZE_ROOT"
    assert reg.get_node(ROOT_PATH) == root
    assert reg.get_dedup(root.task_hash)["status"] == "LIVE"


def test_unresolved_wait_blocks_leaf_and_parent_completion(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "unresolved.sqlite3")
    target_hash = canonical_task_hash("target", {})
    assert reg.spawn_child(ROOT_PATH, 0, target_hash).status == "SPAWNED"
    assert reg.spawn_child(ROOT_PATH, 1, canonical_task_hash("parent", {})).status == "SPAWNED"
    assert reg.spawn_child("1", 0, canonical_task_hash("waiter", {})).status == "SPAWNED"
    assert reg.spawn_child("1.0", 0, target_hash).status == "COLLAPSED_LIVE"

    waiter = reg.get_node("1.0")
    parent = reg.get_node("1")
    assert waiter is not None and parent is not None
    leaf = reg.settle_or_join("1.0", owner_id=waiter.owner_id, lease_epoch=waiter.lease_epoch, outcome="completed")
    assert not leaf.ok and leaf.error == "ERR_OPEN_WAIT_EDGES"
    blocked = reg.join("1", parent.version, {"1.0": waiter.version})
    assert not blocked.ok and blocked.error == "ERR_OPEN_WAIT_EDGES"
    target = reg.get_node("0")
    assert target is not None
    assert reg.settle_or_join("0", owner_id=target.owner_id, lease_epoch=target.lease_epoch, outcome="completed").ok
    assert reg.join("1", parent.version, {"1.0": waiter.version}).ok


def test_spawn_replay_accepts_the_original_parent_version(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "replay-version.sqlite3")
    root = reg.get_node(ROOT_PATH)
    assert root is not None
    h = canonical_task_hash("work", {})
    first = reg.spawn_child(ROOT_PATH, 0, h, expected_version=root.version, request_id="original")
    assert first.status == "SPAWNED"
    replay = reg.spawn_child(ROOT_PATH, 0, h, expected_version=root.version, request_id="original")
    assert replay == first
    different = reg.spawn_child(ROOT_PATH, 1, h, expected_version=root.version, request_id="different")
    assert different.status == "ERR_STALE_PARENT"
    assert reg.get_node(ROOT_PATH).version == root.version + 1


def test_duplicate_wait_edge_preserves_waiter_version(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "repeat-edge.sqlite3")
    target_hash = canonical_task_hash("target", {})
    assert reg.spawn_child(ROOT_PATH, 0, target_hash).status == "SPAWNED"
    assert reg.spawn_child(ROOT_PATH, 1, canonical_task_hash("waiter", {})).status == "SPAWNED"
    assert reg.spawn_child("1", 0, target_hash, request_id="wait").status == "COLLAPSED_LIVE"
    before = reg.get_node("1")
    assert before is not None
    assert reg.spawn_child("1", 0, target_hash, request_id="wait").status == "COLLAPSED_LIVE"
    assert reg.get_node("1").version == before.version


@pytest.mark.parametrize("entry_point", ["try_cut", "acquire_lease"])
def test_lease_clock_starts_after_acquiring_sqlite_writer(tmp_path: Path, entry_point: str) -> None:
    import threading
    import time

    attempting_write = threading.Event()

    class TracedRegistry(JoinCutRegistry):
        def _connect(self) -> sqlite3.Connection:
            conn = super()._connect()
            conn.set_trace_callback(lambda sql: attempting_write.set() if sql.startswith("BEGIN IMMEDIATE") else None)
            return conn

    reg = TracedRegistry(tmp_path / "lock-clock.sqlite3")
    if entry_point == "acquire_lease":
        assert reg.spawn_child(ROOT_PATH, 0, canonical_task_hash("a", {})).status == "SPAWNED"

    def obtain() -> str:
        if entry_point == "acquire_lease":
            assert reg.acquire_lease("0", "worker", ttl_sec=1)[0]
            return "0"
        claim = reg.try_cut("a", {}, owner_id="worker", ttl_sec=1)
        assert claim.allowed and claim.cantor_path is not None
        return claim.cantor_path

    with ThreadPoolExecutor(max_workers=1) as pool:
        blocker = sqlite3.connect(reg.db_path, isolation_level=None)
        try:
            blocker.execute("BEGIN IMMEDIATE")
            attempting_write.clear()
            future = pool.submit(obtain)
            assert attempting_write.wait(3)
            time.sleep(1.1)
            unlock_second = int(time.time())
        finally:
            blocker.close()
        path = future.result(timeout=5)
    assert reg.get_node(path).lease_until >= unlock_second + 1


def test_bootstrap_rolls_back_all_ddl_when_root_seed_violates_a_constraint(tmp_path: Path) -> None:
    db = tmp_path / "bootstrap.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE swarm_nodes (cantor_path TEXT PRIMARY KEY, parent_path TEXT, "
            "branch_idx INTEGER, depth INTEGER, task_hash BLOB, state TEXT, cx REAL, cy REAL, cz REAL, "
            "radius REAL CONSTRAINT existing_radius_floor CHECK (radius > 1), version INTEGER, "
            "owner_id TEXT, lease_epoch INTEGER, lease_until INTEGER, started_at INTEGER)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="existing_radius_floor"):
        JoinCutRegistry(db, r0=1.0)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT type, name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall() == [("table", "swarm_nodes")]


def test_verify_invariants_reads_one_snapshot_across_a_committed_prune(tmp_path: Path) -> None:
    db = tmp_path / "snapshot.sqlite3"
    writer = JoinCutRegistry(db)
    task_hash = canonical_task_hash("snapshot", {})
    child = writer.spawn_child(ROOT_PATH, 0, task_hash)
    assert child.status == "SPAWNED"
    version = writer.get_node("0").version
    committed = []

    def prune_before_dedup_read(statement: str) -> None:
        if statement == "SELECT * FROM task_dedup":
            committed.append(writer.prune("0", version).ok)

    class ObservedReader(JoinCutRegistry):
        def _connect(self) -> sqlite3.Connection:
            conn = super()._connect()
            conn.set_trace_callback(prune_before_dedup_read)
            return conn

    errors = ObservedReader(db).verify_invariants()
    assert committed == [True]
    assert writer.get_dedup(task_hash)["status"] == "PRUNED"
    assert errors == []


def test_first_level_below_precision_floor_is_rejected_before_seeding(tmp_path: Path) -> None:
    db = tmp_path / "precision.sqlite3"
    with pytest.raises(ValueError, match="min_radius"):
        JoinCutRegistry(db, m=2, r0=1.0, gamma=1.0 - 2.0**-42)
    assert not db.exists()


def test_rfc8785_numbers_deduplicate_against_persisted_legacy_hashes(tmp_path: Path) -> None:
    import hashlib

    db = tmp_path / "numeric.sqlite3"
    old_key = hashlib.sha256(b'["measure",{"large":1000000000000000000000,"small":1e-06},[],"1"]').digest()
    reg = JoinCutRegistry(db)
    assert reg.spawn_child(ROOT_PATH, 0, old_key).status == "SPAWNED"
    assert reg.prune("0", reg.get_node("0").version).ok

    amounts = {"small": 1e-6, "large": 1e21}
    key = canonical_task_hash("measure", amounts)
    assert key == hashlib.sha256(b'["measure",{"large":1e+21,"small":0.000001},[],"1"]').digest()
    assert key == canonical_task_hash("measure", {**amounts, "large": 10**21})

    reopened = JoinCutRegistry(db)
    assert reopened.get_dedup(old_key)["status"] == "PRUNED"
    assert reopened.get_dedup(key)["task_hash"] == old_key
    assert reopened.spawn_child(ROOT_PATH, 0, key).status == "TOMBSTONED"
    assert reopened.try_cut("measure", amounts, owner_id="retry").status == "TOMBSTONED"

    # Older processes could persist two encodings with different lifecycle states.
    def seed_released_canonical(op: str) -> None:
        raw = bytes(canonical_task_hash(op, amounts))
        assert reopened.spawn_child(ROOT_PATH, 0, raw).status == "SPAWNED"
        node = reopened.get_node("0")
        assert reopened.release_transient("0", node.owner_id, node.lease_epoch).ok
        assert reopened.get_dedup(raw)["cantor_path"] is None

    seed_released_canonical("measure")
    assert reopened.try_cut("measure", amounts, owner_id="retry").status == "TOMBSTONED"

    leased_old = hashlib.sha256(b'["held",{"large":1000000000000000000000,"small":1e-06},[],"1"]').digest()
    assert reopened.spawn_child(ROOT_PATH, 1, leased_old).status == "SPAWNED"
    assert reopened.acquire_lease("1", "existing-worker")[0]
    seed_released_canonical("held")
    duplicate = reopened.try_cut("held", amounts, owner_id="second-worker")
    assert not duplicate.allowed and duplicate.status == "COLLAPSED_LIVE"
    assert duplicate.task_hash == leased_old

    epoch = reopened.get_node("1").lease_epoch
    assert reopened.mark_started("1", "existing-worker", epoch)
    assert reopened.release_transient("1", "existing-worker", epoch).ok
    paths = [node.cantor_path for node in reopened.all_nodes()]
    retry = reopened.try_cut("held", amounts, owner_id="replacement")
    assert retry.allowed and retry.cantor_path == "1" and retry.task_hash == leased_old
    assert [node.cantor_path for node in reopened.all_nodes()] == paths

    # Historical opaque keys remain usable even if their input is outside the JCS domain.
    old_inexact = hashlib.sha256(b'["measure",{"n":9007199254740993},[],"1"]').digest()
    assert reopened.spawn_child(ROOT_PATH, 0, old_inexact).status == "SPAWNED"
    assert reopened.prune("0", reopened.get_node("0").version).ok
    exact = reopened.try_cut("measure", {"n": 2**53}, owner_id="exact")
    assert exact.allowed
    before = reopened.all_nodes()
    with pytest.raises(ValueError, match="represented exactly as binary64"):
        reopened.try_cut("measure", {"n": 2**53 + 1}, owner_id="inexact")
    assert reopened.all_nodes() == before
    assert reopened.get_dedup(old_inexact)["status"] == "PRUNED"


def _run_join_cut_plan(tmp_path: Path, workstreams, respond=None, recover=None):
    """Run the real scheduler and registry with a local scripted provider."""
    import json

    prompts = []
    reg = JoinCutRegistry(tmp_path / "plan.sqlite3")

    def caller(role, model, system, prompt):
        if "Swarm Algorithm Architect" in system:
            output = json.dumps({"workstreams": workstreams})
        elif prompt.startswith("Assigned Workstream:\n"):
            prompts.append(prompt)
            title = prompt.splitlines()[1]
            if respond:
                return respond(title)
            output = f"Result for {title}"
        elif role == "critic_verifier":
            output = '{"passed":true,"reason":"Workstreams verified","issues":[]}'
        else:
            output = "Findings for the local workstreams."
        return {"ok": True, "text": output, "model": model, "provider": "local"}

    runner = LiveSwarmRunner(
        workspace_root=tmp_path, artifacts_dir=tmp_path / "missions",
        join_cut_enabled=True, join_cut_registry=reg, step_delay_sec=0,
        llm_caller=caller, recovery_llm_caller=recover, auto_spindown=False,
        max_refinement_rounds=0,
    )
    runner.max_concurrency = 1
    try:
        launched = runner.launch_mission(
            "Research and build from local evidence",
            coordinator_count=1, workers_per_coordinator=4 + len(workstreams),
        )
        assert launched["ok"]
        mission_id = launched["mission_id"]
        assert runner._mission_done_events[mission_id].wait(15), "mission did not finish"
        return reg, mission_id, prompts, runner.get_state()["deliberations"]
    finally:
        runner.shutdown()


def _plan_dedup(reg, mission_id, title, *, depends_on=()):
    inputs = {"mission": mission_id, "role": "execution_specialist", "task": title}
    if depends_on:
        inputs["depends_on"] = sorted(depends_on)
    task_hash = canonical_task_hash("dynamic_subagent", inputs)
    row = reg.get_dedup(task_hash)
    assert row is not None, title
    return row


def test_provider_cancellation_releases_the_started_workstream(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import threading

    cancelled = threading.Event()

    def capture_cancel(args):
        if args.exc_type is asyncio.CancelledError:
            cancelled.set()
        else:
            threading.__excepthook__(args)

    def cancel(_title):
        raise asyncio.CancelledError()

    monkeypatch.setattr(threading, "excepthook", capture_cancel)
    reg, mission_id, _, _ = _run_join_cut_plan(tmp_path, [{"key": "a", "title": "Task A"}], cancel)
    assert cancelled.wait(2), "provider cancellation did not propagate out of the mission"
    row = _plan_dedup(reg, mission_id, "Task A")
    assert row["status"] == "LIVE" and row["retry_count"] == 1
    assert reg.get_node(row["cantor_path"]).owner_id is None


@pytest.mark.parametrize("recover_first", [False, True], ids=["successful-first", "recovered-first"])
def test_stolen_work_settles_its_own_claim_and_final_result(tmp_path: Path, recover_first: bool) -> None:
    plan = [{"key": "a", "title": "Task A"}, {"key": "b", "title": "Task B"}]

    def respond(title):
        if title == "Task A" and not recover_first:
            return {"ok": True, "text": "Finished A"}
        return {"ok": False, "error": "HTTP 429"}

    def recover(_policy, _system, prompt):
        if "Assigned Workstream:\nTask A" in prompt:
            return {"ok": True, "text": "Recovered A"}
        return {"ok": False, "error": "HTTP 429"}

    reg, mission_id, prompts, events = _run_join_cut_plan(tmp_path, plan, respond, recover)
    assert [p.splitlines()[1] for p in prompts] == ["Task A", "Task B"]
    assert any(e["stage"] == "WORK_STEAL" for e in events)
    assert _plan_dedup(reg, mission_id, "Task A")["status"] == "JOINED"
    failed = _plan_dedup(reg, mission_id, "Task B")
    assert failed["status"] == "LIVE" and failed["retry_count"] == 1


def test_dependency_scheduler_settles_completed_and_releases_unstarted_claims(tmp_path: Path) -> None:
    plan = [
        {"key": "a", "title": "Task A"},
        {"key": "b", "title": "Task B", "depends_on": ["a"]},
        {"key": "c", "title": "Task C", "depends_on": ["absent"]},
    ]
    reg, mission_id, prompts, _ = _run_join_cut_plan(tmp_path, plan)
    assert [p.splitlines()[1] for p in prompts] == ["Task A", "Task B"]
    assert _plan_dedup(reg, mission_id, "Task A")["status"] == "JOINED"
    assert _plan_dedup(reg, mission_id, "Task B", depends_on=["a"])["status"] == "JOINED"
    blocked = _plan_dedup(reg, mission_id, "Task C", depends_on=["absent"])
    assert blocked["status"] == "LIVE" and blocked["cantor_path"] is None
    assert blocked["retry_count"] == 1


def test_runner_claim_distinguishes_normalized_targets_and_prerequisites(tmp_path: Path) -> None:
    reg = JoinCutRegistry(tmp_path / "targets.sqlite3")
    runner = LiveSwarmRunner(workspace_root=tmp_path, artifacts_dir=tmp_path, join_cut_registry=reg)
    ws = {"role": "builder", "title": "Update tests", "target_files": ["a", "b"]}
    assert runner._join_cut_claim("m", ws, "first")[0]
    assert not runner._join_cut_claim("m", {**ws, "target_files": ["b", "a"]}, "duplicate")[0]
    assert runner._join_cut_claim("m", {**ws, "target_files": ["c"]}, "other-target")[0]
    assert runner._join_cut_claim("m", {**ws, "depends_on": ["prep"]}, "other-input")[0]


def test_collapsed_prerequisite_uses_the_canonical_workstream_result(tmp_path: Path) -> None:
    plan = [
        {"key": "original", "title": "Task A"},
        {"key": "alias", "title": "Task A"},
        {"key": "consumer", "title": "Task B", "depends_on": ["alias"]},
    ]
    reg, mission_id, prompts, _ = _run_join_cut_plan(tmp_path, plan)
    assert [p.splitlines()[1] for p in prompts] == ["Task A", "Task B"]
    assert "Completed prerequisite alias:\nResult for Task A" in prompts[1]
    assert _plan_dedup(reg, mission_id, "Task B", depends_on=["alias"])["status"] == "JOINED"


def test_locked_settlement_retries_after_real_writer_releases(tmp_path: Path) -> None:
    import time

    reg = JoinCutRegistry(tmp_path / "settlement.sqlite3", busy_timeout_ms=100)
    runner = LiveSwarmRunner(workspace_root=tmp_path, artifacts_dir=tmp_path, join_cut_registry=reg)
    ws = {"role": "builder", "title": "Task A"}
    task_hash = canonical_task_hash("dynamic_subagent", {"mission": "m", "role": "builder", "task": "Task A"})
    assert runner._join_cut_claim("m", ws, "first")[0]
    writer = sqlite3.connect(reg.db_path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        runner._join_cut_settle("m", "first", outcome="completed")
        assert reg.get_dedup(task_hash)["status"] == "LIVE"
        writer.rollback()
        deadline = time.monotonic() + 5
        while reg.get_dedup(task_hash)["status"] == "LIVE" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert reg.get_dedup(task_hash)["status"] == "JOINED"
        assert not runner._join_cut_claim("m", ws, "retry")[0]
    finally:
        writer.close()
        runner.shutdown()


def test_explicit_enable_opens_a_separate_sidecar_for_each_mission(tmp_path: Path) -> None:
    artifacts = tmp_path / "missions"
    runner = LiveSwarmRunner(workspace_root=tmp_path, artifacts_dir=artifacts, join_cut_enabled=True)
    ws = {"role": "builder", "title": "Task A"}
    assert not list(artifacts.rglob("*.sqlite3"))
    for mission in ("one", "two"):
        assert runner._join_cut_claim(mission, ws, mission)[0]
        runner._join_cut_settle(mission, mission, outcome="completed")
    databases = list(artifacts.rglob("*.sqlite3"))
    assert len(databases) == 2
    for db_path in databases:
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT count(*) FROM task_dedup WHERE status='JOINED'").fetchone()[0] == 1


def test_recursive_multilevel_budding_and_portal_round_robin(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "sample.py").write_text("val = 42\n")
    runner = LiveSwarmRunner(
        workspace_root=ws,
        artifacts_dir=tmp_path / "artifacts",
        spindown_delay_sec=0.1,
        step_delay_sec=0.01,
        auto_spindown=False,
    )
    try:
        runner.configure_topology(
            coordinator_count=1,
            workers_per_coordinator=18,
            topology_mode="m_eulerian_join_cut",
            branching_order=3,
            quorum_split_threshold=4,
            max_concurrency=16,
        )
        portals = [f"portal:cluster-worker-{i:03d}" for i in range(24)]
        res = runner.launch_mission(
            "Recursive multi-level budding across high-volume portals",
            coordinator_count=1,
            workers_per_coordinator=18,
            quorum_split_threshold=4,
            enable_work_stealing=True,
            candidate_models=portals,
        )
        assert res["ok"]
        out = runner.wait_for_mission(res["mission_id"], timeout=20.0)
        assert out is not None
        state = runner.get_state()
        workers = state["workers"]
        coords = [w for w in workers if w.get("role") == "coordinator"]
        subagents = [w for w in workers if "Sub-Agent" in w.get("name", "")]
        assert len(workers) > 10, f"Expected scaling past 10, got {len(workers)}"
        assert len(coords) >= 3, f"Expected multi-level budded coordinators, got {len(coords)}"
        assert len(subagents) >= 10, f"Expected >= 10 subagents, got {len(subagents)}"
        assigned_models = {w["id"]: w.get("model") for w in subagents}
        assert len(set(assigned_models.values())) > 1, "Expected portal round-robin across subagents"
        depths = set(w.get("depth") for w in workers)
        assert {1, 2, 3}.issubset(depths), f"Expected depths 1, 2, 3, got {depths}"
        stages = [d.get("stage") for d in state["deliberations"] if d.get("mission_id") == res["mission_id"]]
        assert stages.count("ELASTIC_BUD") >= 2
        assert stages.count("ELASTIC_PRUNE") >= 2
        assert stages.count("BRANCH_VERIFIED") >= 2
    finally:
        runner.shutdown()


def test_per_path_rwlock_allows_concurrent_reads_and_disjoint_writes(tmp_path: Path) -> None:
    import threading
    from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner

    runner = LiveSwarmRunner(workspace_root=tmp_path, artifacts_dir=tmp_path / "artifacts", auto_spindown=False)
    try:
        file_a = tmp_path / "a.txt"
        file_b = tmp_path / "b.txt"
        file_a.write_text("alpha", encoding="utf-8")
        file_b.write_text("beta", encoding="utf-8")

        # Two readers on the SAME file must overlap concurrently (barrier proves non-serialization)
        read_barrier = threading.Barrier(2, timeout=2.0)

        def reader() -> None:
            with runner._rwlock_for_path(file_a).read_lock():
                read_barrier.wait()

        t1 = threading.Thread(target=reader)
        t2 = threading.Thread(target=reader)
        t1.start()
        t2.start()
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)
        assert not t1.is_alive() and not t2.is_alive()

        # Two writers on DISJOINT files must overlap concurrently
        write_barrier = threading.Barrier(2, timeout=2.0)

        def writer(p: Path) -> None:
            with runner._rwlock_for_path(p).write_lock():
                write_barrier.wait()

        w1 = threading.Thread(target=writer, args=(file_a,))
        w2 = threading.Thread(target=writer, args=(file_b,))
        w1.start()
        w2.start()
        w1.join(timeout=3.0)
        w2.join(timeout=3.0)
        assert not w1.is_alive() and not w2.is_alive()
    finally:
        runner.shutdown()


def test_readonly_analysis_refinement_and_scaled_recovery_policy(tmp_path: Path) -> None:
    from sabrina_console_desktop.recovery_policy import RecoveryPolicy

    # Verify RecoveryPolicy accepts scaled worker counts and context budgets
    pol_path = tmp_path / "scaled_policy.json"
    pol_path.write_text('{"max_workers_per_mission": 64, "max_context_chars": 32000}', encoding="utf-8")
    loaded = RecoveryPolicy.load(pol_path)
    assert loaded.max_workers_per_mission == 64
    assert loaded.max_context_chars == 32000

    # Verify read-only mission performs Critic refinement loop when Critic returns REVIEW first
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "mod.py").write_text("def compute():\n    return 7\n", encoding="utf-8")
    critic_calls = 0

    def caller(role: str, model: str, system: str, prompt: str) -> dict[str, object]:
        nonlocal critic_calls
        if role == "critic_verifier":
            critic_calls += 1
            if critic_calls == 1:
                return {"ok": True, "text": "REVIEW: Missing citation to mod.py return value.", "model": model}
            return {"ok": True, "text": "PASS: Citation to mod.py verified.", "model": model}
        return {"ok": True, "text": "Analysis of mod.py: compute() returns 7.", "model": model}

    runner = LiveSwarmRunner(
        workspace_root=ws,
        artifacts_dir=tmp_path / "artifacts",
        llm_caller=caller,
        step_delay_sec=0.0,
        max_refinement_rounds=2,
        auto_spindown=False,
    )
    try:
        runner.configure_topology(allow_readonly_refinement=True)
        res = runner.launch_mission("Audit mod.py return value", allow_workspace_writes=False)
        assert res["ok"]
        out = runner.wait_for_mission(res["mission_id"], timeout=10.0)
        assert out is not None
        assert out["state"] == "PASS"
        assert critic_calls == 2
        stages = [d.get("stage") for d in runner.get_state()["deliberations"] if d.get("mission_id") == res["mission_id"]]
        assert "CRITIC_REFINEMENT_LOOP" in stages
    finally:
        runner.shutdown()


def test_default_runner_uses_join_cut_registry_and_preserves_deep_geometry(tmp_path: Path) -> None:
    runner = LiveSwarmRunner(workspace_root=tmp_path, artifacts_dir=tmp_path / "artifacts")
    try:
        reg = runner._join_cut_registry("m-deep")
        assert isinstance(reg, JoinCutRegistry)
        assert reg.params.max_depth > 6
        assert runner._resolve_join_cut_parent("sabrina", {}) == ROOT_PATH
        assert runner._resolve_join_cut_parent("coordinator", {}) == ROOT_PATH

        # Claim down to depth 12 and confirm sphere radius is not truncated to 0.0
        parent_path: str | None = None
        last_wid = ""
        for depth in range(1, 13):
            wid = f"w-d{depth}"
            ok, reason = runner._join_cut_claim(
                "m-deep",
                {"role": "execution_specialist", "title": f"Deep Task {depth}"},
                wid,
                parent_node_id=parent_path,
            )
            assert ok, f"failed at depth {depth}: {reason}"
            parent_path = runner._join_cut_nodes[wid]
            last_wid = wid
        sphere = runner._join_cut_spheres[last_wid]
        assert sphere["r"] > 0.0
        assert sphere["r"] < 1e-5
    finally:
        runner.shutdown()
