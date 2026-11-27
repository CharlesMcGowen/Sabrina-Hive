"""Concurrency and invariant tests for join_cut_swarm, plus the live runner's claim and settle hooks."""

from __future__ import annotations

import random
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sabrina_console_desktop.join_cut_swarm import (
    M,
    ROOT,
    DeduplicationRegistry,
    SpatialRegion,
    task_hash,
)


class JoinCutSwarmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = DeduplicationRegistry(Path(self.tmp.name) / "swarm.sqlite3")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _assert_valid(self) -> None:
        errors = self.reg.verify_invariants()
        self.assertEqual(errors, [], f"invariant violations: {errors}")

    def test_100_plus_identical_submissions_yield_exactly_one_active_node(self) -> None:
        payload = {"task": "research mean reversion", "universe": "sp500"}
        n = 120

        def submit(_: int):
            return self.reg.cut_branch(ROOT, [payload])

        with ThreadPoolExecutor(max_workers=24) as pool:
            outcomes = list(pool.map(submit, range(n)))

        created = [o for o in outcomes if o.reason == "ok" and o.results[0].created]
        duplicates = [o for o in outcomes if o.reason == "ok" and not o.results[0].created]
        self.assertEqual(len(created), 1, "exactly one submission must perform the creation")
        self.assertEqual(len(created) + len(duplicates), n)

        h = task_hash(payload)
        active_for_hash = [p for p in self.reg.active_paths() if self.reg.get_node(p).task_hash == h]
        self.assertEqual(len(active_for_hash), 1)
        self._assert_valid()

    def test_deep_multi_level_cuts_have_zero_spatial_overlap(self) -> None:
        reg = self.reg
        frontier = [ROOT]
        counter = 0
        for _ in range(3):  # root -> depth 1 -> depth 2 -> depth 3, every node cut to M children
            next_frontier = []
            for parent in frontier:
                payloads = [{"n": counter + i} for i in range(M)]
                counter += M
                result = reg.cut_branch(parent, payloads, max_depth=3)
                self.assertTrue(result.ok, result.reason)
                next_frontier.extend(r.cantor_path for r in result.results)
            frontier = next_frontier
        self._assert_valid()

        nodes = {n.cantor_path: n for n in reg.all_nodes()}
        siblings = [n for n in nodes.values() if n.parent_path == ROOT]
        for i, a in enumerate(siblings):
            for b in siblings[i + 1:]:
                self.assertTrue(a.region.disjoint(b.region), f"siblings {a.cantor_path} and {b.cantor_path} overlap")
            self.assertTrue(SpatialRegion(0.0, 0.0, 0.0, 1.0).contains(a.region))

    def test_join_prunes_subtree_and_tombstoned_tasks_cannot_respawn(self) -> None:
        reg = self.reg
        child = reg.cut_branch(ROOT, [{"branch": "A"}, {"branch": "B"}])
        path_a = child.results[0].cantor_path
        grand = reg.cut_branch(path_a, [{"leaf": 1}, {"leaf": 2}])
        self.assertTrue(grand.ok)

        self.assertTrue(reg.join_branch(path_a, reason="redundant").ok)
        self.assertEqual(reg.get_node(path_a).state, "JOINED")
        self.assertEqual(reg.get_node(grand.results[0].cantor_path).state, "JOINED")
        self._assert_valid()

        again = reg.cut_branch(ROOT, [{"leaf": 1}])
        self.assertFalse(again.results[0].created)
        self.assertEqual(again.results[0].reason, "duplicate_tombstoned")
        self._assert_valid()

    def test_cut_refuses_more_than_m_children(self) -> None:
        result = self.reg.cut_branch(ROOT, [{"k": i} for i in range(M + 1)])
        self.assertFalse(result.ok)
        self.assertEqual(self.reg.active_paths(), [ROOT], "refused cut must leave no nodes behind")

    def test_joined_slots_admit_new_work_without_replaying_against_it(self) -> None:
        first_tasks = [{"first": i} for i in range(M)]
        first = self.reg.cut_branch(ROOT, first_tasks, request_id="first-mission")
        self.assertTrue(first.ok)
        for result in first.results:
            self.assertTrue(self.reg.join_branch(result.cantor_path, output="first").ok)

        reg = DeduplicationRegistry(self.reg.db_path)
        next_tasks = [{"next": i} for i in range(M)]
        second = reg.cut_branch(ROOT, next_tasks, auto_route=True, request_id="next-mission")
        self.assertTrue(second.ok, second.reason)
        self.assertEqual(len(second.results), M)
        self.assertNotIn(ROOT, reg.open_parent_paths())
        self.assertEqual(reg.cut_branch(ROOT, first_tasks, request_id="first-mission").results, first.results)
        for task, old, new in zip(first_tasks, first.results, second.results):
            self.assertNotEqual(old.cantor_path, new.cantor_path)
            self.assertEqual(reg.get_node(old.cantor_path).region, reg.get_node(new.cantor_path).region)
            self.assertEqual(reg.cut_branch(ROOT, [task]).results[0].reason, "duplicate_tombstoned")
            self.assertEqual(reg.join_branch(old.cantor_path).reason, "replayed")
            self.assertEqual(reg.get_node(new.cantor_path).state, "ACTIVE")
        self.assertEqual(len(reg.read_branch_outputs(ROOT)), M)
        self.assertEqual(reg.verify_invariants(), [])

        # A finalized worker may still have an active child inside its region.
        parent = second.results[0].cantor_path
        leaf = reg.cut_branch(parent, [{"leaf": 1}]).results[0].cantor_path
        self.assertTrue(reg.finalize_node(parent).ok)
        self.assertFalse(reg.cut_branch(ROOT, [{"third": 0}]).ok)
        self.assertTrue(reg.join_branch(leaf).ok)
        self.assertIn(ROOT, reg.open_parent_paths())
        self.assertTrue(reg.cut_branch(ROOT, [{"third": 0}]).ok)
        self.assertEqual(reg.verify_invariants(), [])

    def test_cut_and_join_are_idempotent_and_replay_safe(self) -> None:
        payloads = [{"x": 1}, {"x": 2}]
        first = self.reg.cut_branch(ROOT, payloads, request_id="req-1")
        second = self.reg.cut_branch(ROOT, payloads, request_id="req-1")
        self.assertEqual(second.reason, "replayed")
        self.assertEqual(first.results, second.results)
        self.assertEqual(len([p for p in self.reg.active_paths() if p != ROOT]), 2)

        path = first.results[0].cantor_path
        self.assertTrue(self.reg.join_branch(path).ok)
        self.assertEqual(self.reg.join_branch(path).reason, "replayed")
        self._assert_valid()

    def test_release_frees_volume_and_task_for_retry(self) -> None:
        leaf = self.reg.cut_branch(ROOT, [{"t": 1}]).results[0].cantor_path
        self.assertTrue(self.reg.release_leaf(leaf).ok)
        self.assertIsNone(self.reg.get_node(leaf))
        retry = self.reg.cut_branch(ROOT, [{"t": 1}])
        self.assertTrue(retry.results[0].created, "a released task must be spawnable again")
        self._assert_valid()

    def test_release_refuses_nodes_with_children(self) -> None:
        parent = self.reg.cut_branch(ROOT, [{"p": 1}]).results[0].cantor_path
        self.reg.cut_branch(parent, [{"c": 1}])
        self.assertFalse(self.reg.release_leaf(parent).ok)
        self._assert_valid()

    def test_depth_and_active_budgets_are_enforced(self) -> None:
        shallow = DeduplicationRegistry(Path(self.tmp.name) / "shallow.sqlite3")
        child = shallow.cut_branch(ROOT, [{"d": 1}]).results[0].cantor_path
        deep = shallow.cut_branch(child, [{"d": 2}], max_depth=1)
        self.assertFalse(deep.ok)
        self.assertIn("max_depth", deep.reason)

        capped = shallow.cut_branch(ROOT, [{"c": i} for i in range(3)], max_active_nodes=3)
        self.assertFalse(capped.ok)
        self.assertIn("max_active_nodes", capped.reason)

    def test_chaotic_concurrent_join_cut_keeps_topology_valid(self) -> None:
        reg = self.reg

        def worker(seed: int) -> None:
            rng = random.Random(seed)
            for step in range(15):
                roll = rng.random()
                if roll < 0.55:
                    open_parents = reg.open_parent_paths(max_depth=6)
                    if open_parents:
                        reg.cut_branch(rng.choice(open_parents),
                                       [{"s": seed, "i": step, "k": k} for k in range(rng.randint(1, 3))])
                elif roll < 0.85:
                    joinable = [p for p in reg.active_paths() if p != ROOT]
                    if joinable:
                        reg.join_branch(rng.choice(joinable), reason=rng.choice(["complete", "fail", "redundant"]))
                else:
                    reg.claim_node(rng.choice(reg.active_paths()), f"w{seed}")

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(worker, range(48)))
        self._assert_valid()

        nodes = {n.cantor_path: n for n in reg.all_nodes()}
        for p in nodes:
            depth, cur = 0, p
            while nodes[cur].parent_path is not None:
                cur = nodes[cur].parent_path
                depth += 1
                self.assertLessEqual(depth, len(nodes), f"cycle through {p}")
            self.assertEqual(cur, ROOT)


class JoinCutRunnerWiringTests(unittest.TestCase):
    """The live runner's claim and settle hooks drive the registry the same way the spawn path does."""

    def setUp(self) -> None:
        from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner

        self.tmp = tempfile.TemporaryDirectory()
        self.runner = LiveSwarmRunner(artifacts_dir=Path(self.tmp.name), workspace_root=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _ws(self, i: int, title: str | None = None) -> dict[str, str]:
        return {"key": f"ws_{i}", "role": "research_scout", "title": title or f"task number {i}"}

    def test_duplicate_subagent_is_refused_and_completion_joins_with_folded_output(self) -> None:
        from sabrina_console_desktop.live_swarm_runner import _settle_outcome  # noqa: F401

        ws = self._ws(1, "scan order books")
        allowed, _ = self.runner._join_cut_claim("m-1", ws, "m-1-ws_1")
        self.assertTrue(allowed)
        path = self.runner._join_cut_nodes["m-1-ws_1"]

        again_allowed, again_reason = self.runner._join_cut_claim("m-1", ws, "m-1-ws_1b")
        self.assertFalse(again_allowed)
        self.assertEqual(again_reason, "duplicate_active")

        self.runner._join_cut_settle(
            "m-1",
            "m-1-ws_1",
            outcome="completed",
            output={"summary": "spread 12 bps", "citations": ["book:BTCUSD"]},
            spent=2.5,
            earned=1.0,
        )
        reg = self.runner._join_cut_registry("m-1")
        self.assertEqual(reg.get_node(path).state, "JOINED")
        root_outputs = reg.read_branch_outputs(ROOT)
        self.assertTrue(any(item.get("output", {}).get("summary") == "spread 12 bps" for item in root_outputs))
        self.assertFalse(self.runner._join_cut_claim("m-1", ws, "m-1-ws_1c")[0])

    def test_recursive_parent_node_id_and_multi_level_output_folding(self) -> None:
        ws_parent = self._ws(10, "parent research")
        self.assertTrue(self.runner._join_cut_claim("m-rec", ws_parent, "m-rec-parent")[0])
        parent_path = self.runner._join_cut_nodes["m-rec-parent"]

        # Child explicitly passes parent's worker ID as parent_node_id
        ws_child = self._ws(11, "child deep dive")
        self.assertTrue(
            self.runner._join_cut_claim("m-rec", ws_child, "m-rec-child", parent_node_id="m-rec-parent")[0]
        )
        child_path = self.runner._join_cut_nodes["m-rec-child"]
        self.assertTrue(child_path.startswith(parent_path + "."), f"{child_path} must sit under {parent_path}")

        # Join child -> folds into parent; join parent -> folds both up to ROOT
        self.runner._join_cut_settle("m-rec", "m-rec-child", outcome="completed", output={"finding": "leaf-ok"})
        reg = self.runner._join_cut_registry("m-rec")
        self.assertEqual(len(reg.read_branch_outputs(parent_path)), 1)

        self.runner._join_cut_settle("m-rec", "m-rec-parent", outcome="completed", output={"finding": "parent-ok"})
        root_folded = reg.read_branch_outputs(ROOT)
        self.assertTrue(any(e.get("child_path") == parent_path and e.get("children") for e in root_folded))
        self.assertEqual(reg.verify_invariants(), [])

    def test_branch_budgets_split_track_spend_and_return_on_join_and_release(self) -> None:
        reg = DeduplicationRegistry(Path(self.tmp.name) / "budget_blackboard.sqlite3", initial_budget=100.0)
        cut = reg.cut_branch(ROOT, [{"b": 1}], budget_share=40.0, request_id="b-1")
        self.assertTrue(cut.ok)
        child_path = cut.results[0].cantor_path
        self.assertAlmostEqual(reg.get_node(ROOT).budget_allocated, 60.0)
        self.assertAlmostEqual(reg.get_node(child_path).budget_allocated, 40.0)

        # Excessive budget request is refused cleanly
        over = reg.cut_branch(ROOT, [{"b": 2}], budget_share=999.0, request_id="b-over")
        self.assertFalse(over.ok)
        self.assertIn("insufficient budget", over.reason)

        # Spend 15, earn 5 -> remaining 25 + earned 5 = 30 returned to ROOT (60 + 30 = 90)
        self.assertTrue(reg.record_spend(child_path, 10.0).ok)
        self.assertTrue(reg.join_branch(child_path, reason="completed", output={"v": 1}, spent=5.0, earned=5.0).ok)
        self.assertAlmostEqual(reg.get_node(ROOT).budget_allocated, 90.0)

        # Transient release returns full unspent budget to parent
        cut2 = reg.cut_branch(ROOT, [{"b": 3}], budget_share=20.0, request_id="b-3")
        self.assertTrue(cut2.ok)
        p2 = cut2.results[0].cantor_path
        self.assertAlmostEqual(reg.get_node(ROOT).budget_allocated, 70.0)
        self.assertTrue(reg.release_leaf(p2, reason="transient_failure", spent=2.0).ok)
        self.assertAlmostEqual(reg.get_node(ROOT).budget_allocated, 88.0)
        self.assertEqual(reg.verify_invariants(), [])

    def test_shared_blackboard_db_across_missions_and_no_per_mission_files(self) -> None:
        from sabrina_console_desktop.tools.telemetry_blackboard import TelemetryBlackboard

        bb_path = Path(self.tmp.name) / "dnd_rsi_blackboard.sqlite3"
        # Ensure TelemetryBlackboard and DeduplicationRegistry coexist in the exact same SQLite DB
        bb = TelemetryBlackboard(bb_path)
        bb.record_transition_trace("m-shared-1", "coordinator", "research_scout")

        reg_a = self.runner._join_cut_registry("m-shared-1")
        reg_b = self.runner._join_cut_registry("m-shared-2")
        self.assertIs(reg_a, reg_b)
        self.assertEqual(reg_a.db_path.name, "dnd_rsi_blackboard.sqlite3")
        self.assertFalse((Path(self.tmp.name) / "join_cut").exists(), "Must not create per-mission SQLite files")

        # 10 distinct tasks across 2 missions route recursively under ROOT without flat-6 refusal
        for i in range(10):
            mid = "m-shared-1" if i < 5 else "m-shared-2"
            ok, reason = self.runner._join_cut_claim(mid, self._ws(100 + i), f"{mid}-w{i}")
            self.assertTrue(ok, f"task {i} failed: {reason}")
        self.assertFalse((bb_path.parent / "join_cut_registry").exists())
        self.assertIsNotNone(reg_a.lookup_dedup({"mission": "m-shared-1", "role": "research_scout", "task": "task number 100"}))
        self.assertEqual(reg_a.verify_invariants(), [])

    def test_transient_failure_releases_so_the_task_can_retry(self) -> None:
        ws = self._ws(2, "fetch universe list")
        self.assertTrue(self.runner._join_cut_claim("m-2", ws, "m-2-ws_2")[0])
        self.runner._join_cut_settle("m-2", "m-2-ws_2", outcome="transient_failure")
        retry_allowed, retry_reason = self.runner._join_cut_claim("m-2", ws, "m-2-ws_2b")
        self.assertTrue(retry_allowed, retry_reason)

    def test_semantic_failure_tombstones_so_the_task_never_repeats(self) -> None:
        ws = self._ws(3, "build report")
        self.assertTrue(self.runner._join_cut_claim("m-3", ws, "m-3-ws_3")[0])
        self.runner._join_cut_settle("m-3", "m-3-ws_3", outcome="semantic_failure")
        allowed, reason = self.runner._join_cut_claim("m-3", ws, "m-3-ws_3b")
        self.assertFalse(allowed)
        self.assertEqual(reason, "duplicate_tombstoned")

    def test_seven_distinct_tasks_all_spawn_and_duplicates_are_skipped(self) -> None:
        results = [self.runner._join_cut_claim("m-7", self._ws(i), f"m-7-ws_{i}") for i in range(7)]
        self.assertEqual([r[0] for r in results], [True] * 7, results)
        dup_allowed, dup_reason = self.runner._join_cut_claim("m-7", self._ws(0), "m-7-dup")
        self.assertFalse(dup_allowed)
        self.assertEqual(dup_reason, "duplicate_active")
        self.assertEqual(self.runner._join_cut_registry("m-7").verify_invariants(), [])

    def test_settle_for_an_unknown_worker_fails_open(self) -> None:
        self.runner._join_cut_settle("m-none", "never-claimed", outcome="completed")
        self.runner._join_cut_settle("m-none", "never-claimed", outcome="semantic_failure")

    def test_outcome_mapping(self) -> None:
        from sabrina_console_desktop.live_swarm_runner import _settle_outcome

        self.assertEqual(_settle_outcome({"status": "DONE"}, cancelled=False), "completed")
        self.assertEqual(_settle_outcome({"status": "DONE"}, cancelled=True), "cancelled")
        self.assertEqual(_settle_outcome({"status": "FAILED", "llm_error": "HTTP 429 rate limit"}, cancelled=False),
                         "transient_failure")
        self.assertEqual(_settle_outcome({"status": "FAILED", "llm_error": "timed out after 25s"}, cancelled=False),
                         "transient_failure")
        self.assertEqual(_settle_outcome({"status": "FAILED", "llm_error": ""}, cancelled=False),
                         "transient_failure")
        self.assertEqual(_settle_outcome({"status": "FAILED", "llm_error": "invalid JSON schema for deliverable"},
                                         cancelled=False), "semantic_failure")


if __name__ == "__main__":
    unittest.main()
