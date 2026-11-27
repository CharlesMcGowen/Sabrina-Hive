"""Unit and integration tests for m-Eulerian Join-Cut topology, planar closure, and LOG_fin gates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from swarm_console.eulerian_topology import (
    EulerianInvariantError,
    EulerianSwarmTopology,
    LogFinPromotionGate,
    SharingPolicy,
)


class EulerianTopologyTests(unittest.TestCase):
    def test_riemann_hurwitz_and_log_fin_across_orders(self) -> None:
        for m in (2, 3, 4, 5):
            topo = EulerianSwarmTopology(m=m)
            h1 = topo.create_sphere(label="root")
            topo.expand_hub(h1, label="arbiter-1")
            ports = sorted(topo.log_fin_ports())
            self.assertEqual(len(ports), m)

            h2, b1 = topo.graft_new_hub(ports[0], hub_label="child-1", bridge_label="seam-1")
            topo.expand_hub(h2, label="arbiter-2")
            ports2 = sorted(topo.log_fin_ports())
            self.assertEqual(len(ports2), m)

            h3, b2 = topo.graft_new_hub(ports2[0], hub_label="child-2", bridge_label="seam-2")
            topo.assert_invariants()

            closures = topo.close_planar_map()
            self.assertEqual(len(closures), 1)
            cl = closures[0]
            self.assertEqual(cl.alpha, (2, 2, 1))
            self.assertEqual(cl.n_sum, 5)
            self.assertEqual(cl.black_leaf_count, (m - 1) * 5 - 3 + 2)
            self.assertEqual(cl.black_leaf_count - cl.white_leaf_count, m)
            self.assertEqual(len(cl.log_fin_ports), m)
            self.assertEqual(len(cl.log_da_pairs), cl.white_leaf_count)

            # Cut bridge cleanly into 2 isolated genus-0 spheres, each with m LOG_fin ports
            leaf_a, leaf_b = topo.cut_bridge(b1)
            split_closures = topo.close_planar_map()
            self.assertEqual(len(split_closures), 2)
            for sc in split_closures:
                self.assertEqual(len(sc.log_fin_ports), m)
                self.assertEqual(sc.black_leaf_count - sc.white_leaf_count, m)

            # Re-joining restores a single valid genus-0 sphere
            topo.join_spheres(leaf_a, leaf_b)
            self.assertEqual(len(topo.close_planar_map()), 1)
            self.assertEqual(len(topo.log_fin_ports()), m)
            self.assertIn(h3, topo.nodes)
            self.assertIn(b2, topo.nodes)

    def test_same_sphere_join_refused_as_genus_violation(self) -> None:
        topo = EulerianSwarmTopology(m=3)
        topo.create_sphere()
        ports = sorted(topo.log_fin_ports())
        with self.assertRaises(EulerianInvariantError):
            topo.join_spheres(ports[0], ports[1])

    def test_sharing_policy_seam_and_cut_isolation(self) -> None:
        topo = EulerianSwarmTopology(m=3)
        h1 = topo.create_sphere(owner_worker_id="coord_root")
        port = sorted(topo.log_fin_ports())[0]
        h2, bridge_id = topo.graft_new_hub(
            port,
            policy=SharingPolicy(
                allowed_kinds=frozenset({"summary", "verified_artifact"}),
                max_excerpt_chars=120,
                allow_raw_log_da=False,
            ),
            hub_worker_id="coord_bud",
        )
        self.assertNotEqual(h1, h2)

        ok_sum, _ = topo.can_cross_seam("coord_root", "coord_bud", "summary", payload_chars=80)
        self.assertTrue(ok_sum)

        ok_raw, reason_raw = topo.can_cross_seam("coord_root", "coord_bud", "log_da", payload_chars=40)
        self.assertFalse(ok_raw)
        self.assertIn("blocked_by_bridge", reason_raw)

        ok_big, reason_big = topo.can_cross_seam("coord_root", "coord_bud", "summary", payload_chars=500)
        self.assertFalse(ok_big)
        self.assertIn("blocked_by_bridge", reason_big)

        # After cutting the bridge, even summaries cannot leak across isolated spheres
        topo.cut_bridge(bridge_id)
        ok_after, reason_after = topo.can_cross_seam("coord_root", "coord_bud", "summary", payload_chars=20)
        self.assertFalse(ok_after)
        self.assertEqual(reason_after, "isolated_spheres")

    def test_fractal_compaction_restores_single_hub(self) -> None:
        topo = EulerianSwarmTopology(m=3)
        root = topo.create_sphere()
        port = sorted(topo.log_fin_ports())[0]
        child_hub, bridge_id = topo.graft_new_hub(port)
        topo.expand_hub(child_hub)
        self.assertEqual(len(topo.close_planar_map()[0].hub_ids), 2)

        restored_leaf = topo.compact_branch(bridge_id, keep_hub_id=root)
        self.assertEqual(topo.nodes[restored_leaf].kind, "b_leaf")
        self.assertEqual(topo.close_planar_map()[0].hub_ids, (root,))
        self.assertEqual(len(topo.log_fin_ports()), 3)

    def test_log_fin_promotion_gate_enforces_ports_and_checkers(self) -> None:
        topo = EulerianSwarmTopology(m=3)
        root = topo.create_sphere()
        arbiter = topo.expand_hub(root)
        w_leaf = [nb for nb in topo.nodes[arbiter].neighbors if topo.nodes[nb].kind == "w_leaf"][0]
        cl = topo.close_planar_map()[0]
        internal_paired_b_leaf = cl.log_da_pairs[0][1]
        outer_port = cl.log_fin_ports[0]

        gate = LogFinPromotionGate(
            topo,
            checkers={
                "patch": lambda p: (bool(p.get("tests_passed")), "tests passed" if p.get("tests_passed") else "failed")
            },
        )
        aid_ok = gate.append_log_da("patch", {"tests_passed": True}, author="w1", slot=w_leaf)
        aid_bad = gate.append_log_da("patch", {"tests_passed": False}, author="w2", slot=w_leaf)

        # Cannot promote via an internal paired LOG_da black leaf
        res_internal = gate.promote_via_log_fin(aid_ok, port_id=internal_paired_b_leaf)
        self.assertFalse(res_internal["ok"])

        # Checker failure blocks promotion even on an authorized LOG_fin port
        res_fail = gate.promote_via_log_fin(aid_bad, port_id=outer_port)
        self.assertFalse(res_fail["ok"])

        # Passing artifact promotes cleanly through an outer-face LOG_fin port
        res_ok = gate.promote_via_log_fin(aid_ok, port_id=outer_port)
        self.assertTrue(res_ok["ok"])
        self.assertIn(aid_ok, gate.finalized)


class LiveSwarmRunnerEulerianIntegrationTests(unittest.TestCase):
    def test_configure_and_launch_m_eulerian_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            runner = LiveSwarmRunner(
                workspace_root=Path(td),
                artifacts_dir=Path(td) / "artifacts",
                spindown_delay_sec=0.05,
                step_delay_sec=0.01,
                auto_spindown=True,
                join_cut_enabled=False,
            )
            try:
                cfg = runner.configure_topology(topology_mode="m_eulerian_join_cut", branching_order=4)
                self.assertEqual(cfg["topology_mode"], "m_eulerian_join_cut")
                self.assertEqual(cfg["branching_order"], 4)

                launched = runner.launch_mission("Inspect topology invariants", mode="codex_thought_antigravity_implement")
                self.assertTrue(launched["ok"])
                mid = launched["mission_id"]
                runner.wait_for_mission(mid, timeout=10.0)

                topo_snap = runner.get_mission_topology_snapshot(mid)
                self.assertIsNotNone(topo_snap)
                self.assertEqual(topo_snap["m"], 4)
                self.assertEqual(topo_snap["sphere_count"], 1)
                self.assertEqual(len(topo_snap["spheres"][0]["log_fin_ports"]), 4)
                self.assertEqual(topo_snap["spheres"][0]["genus"], 0)

                snap = runner.get_state()
                self.assertEqual(snap["topology_mode"], "m_eulerian_join_cut")
                self.assertEqual(snap["branching_order"], 4)
            finally:
                runner.shutdown()

    def test_multi_bud_grafts_multiple_sub_hubs_across_log_fin_ports(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            def _stub_llm(role: str, model: str, system: str, user: str) -> dict:
                if role == "coordinator":
                    return {
                        "ok": True,
                        "provider": "stub",
                        "model": model,
                        "text": (
                            '{"algorithm":"quorum_work_steal","theta":0.55,'
                            '"communication_protocol":"blackboard_handoff",'
                            '"research_decision":"fan out 6 independent slices",'
                            '"workstreams":['
                            '{"key":"ws_1","title":"Slice 1","role":"research_scout"},'
                            '{"key":"ws_2","title":"Slice 2","role":"execution_specialist"},'
                            '{"key":"ws_3","title":"Slice 3","role":"research_scout"},'
                            '{"key":"ws_4","title":"Slice 4","role":"execution_specialist"},'
                            '{"key":"ws_5","title":"Slice 5","role":"research_scout"},'
                            '{"key":"ws_6","title":"Slice 6","role":"execution_specialist"}'
                            ']}'
                        ),
                    }
                return {"ok": True, "provider": "stub", "model": model, "text": f"VERDICT: PASS\nCompleted {role}."}

            runner = LiveSwarmRunner(
                workspace_root=Path(td),
                artifacts_dir=Path(td) / "artifacts",
                spindown_delay_sec=0.05,
                step_delay_sec=0.0,
                auto_spindown=False,
                llm_caller=_stub_llm,
                join_cut_enabled=False,
            )
            try:
                runner.configure_topology(
                    coordinator_count=1,
                    workers_per_coordinator=10,
                    topology_mode="m_eulerian_join_cut",
                    branching_order=3,
                    quorum_split_threshold=2,
                    quorum_prune_threshold=1,
                )
                goal = (
                    "1. Audit module alpha\n2. Audit module beta\n3. Audit module gamma\n"
                    "4. Audit module delta\n5. Audit module epsilon\n6. Audit module zeta"
                )
                launched = runner.launch_mission(goal, mode="codex_thought_antigravity_implement")
                self.assertTrue(launched["ok"])
                mid = launched["mission_id"]
                runner.wait_for_mission(mid, timeout=15.0)

                delibs = runner.get_state()["deliberations"]
                bud_events = [d for d in delibs if d.get("stage") == "ELASTIC_BUD" and d.get("mission_id") == mid]
                prune_events = [d for d in delibs if d.get("stage") == "ELASTIC_PRUNE" and d.get("mission_id") == mid]
                # 6 workstreams / eff_quorum=2 => 3 sub-hubs budded across LOG_fin ports and compacted back cleanly
                self.assertGreaterEqual(len(bud_events), 3)
                self.assertEqual(len(prune_events), len(bud_events))

                topo_snap = runner.get_mission_topology_snapshot(mid)
                self.assertIsNotNone(topo_snap)
                self.assertEqual(topo_snap["sphere_count"], 1)
                self.assertEqual(len(topo_snap["spheres"][0]["log_fin_ports"]), 3)
                self.assertEqual(topo_snap["spheres"][0]["genus"], 0)
            finally:
                runner.shutdown()

    def test_swarm_can_write_proposed_skill_but_not_core_engine(self) -> None:
        from sabrina_console_desktop.live_swarm_runner import REPO_ROOT

        runner = LiveSwarmRunner(
            workspace_root=REPO_ROOT,
            spindown_delay_sec=0.05,
            step_delay_sec=0.0,
            auto_spindown=True,
            join_cut_enabled=False,
        )
        proposed_file = REPO_ROOT / "sabrina_console_desktop" / "skills" / "proposed" / "_test_probe_skill" / "SKILL.md"
        try:
            rejected: list[str] = []
            text = (
                "```write_file:sabrina_console_desktop/skills/proposed/_test_probe_skill/SKILL.md\n"
                "---\nname: test-probe-skill\ndescription: probe\n---\n# Probe\n```\n"
                "```write_file:sabrina_console_desktop/live_swarm_runner.py\n# blocked\n```\n"
            )
            written = runner._apply_workspace_writes(text, rejected_out=rejected)
            self.assertEqual(written, ["sabrina_console_desktop/skills/proposed/_test_probe_skill/SKILL.md"])
            self.assertTrue(proposed_file.is_file())
            self.assertTrue(any("live_swarm_runner.py" in r for r in rejected))
        finally:
            if proposed_file.exists():
                proposed_file.unlink()
            if proposed_file.parent.exists():
                proposed_file.parent.rmdir()
            runner.shutdown()


if __name__ == "__main__":
    unittest.main()
