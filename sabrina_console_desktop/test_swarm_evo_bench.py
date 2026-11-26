"""Unit and integration tests for Swarm Self-Analytical Evolution Benchmark (Evo-Bench)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any

from sabrina_console_desktop.recovery_policy import RecoveryPolicy
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from sabrina_console_desktop.swarm_evo_bench import (
    BenchmarkTask,
    EvoBenchDatabase,
    PerturbationInjector,
    SwarmEvoBenchmarkRunner,
    SwarmScaffold,
    build_default_benchmark_suite,
)
from swarm_console.vault import ProviderKeyVault


def _deterministic_bench_llm(
    role: str,
    model_id: str,
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    """Deterministic local test LLM implementing Planner, Scout, Executor, Verifier, and Mutator."""
    prov = "ollama" if str(model_id).startswith("ollama/") else "gemini"
    tokens = max(25, (len(system_prompt) + len(user_prompt)) // 6)

    # Mutator call in _propose_mutation (stand-off benchmark)
    if "unassisted stand-off benchmark" in system_prompt or "Evo-Bench Scaffold Evolver" in system_prompt:
        if "Generation: 1" in user_prompt:
            proposed = {
                "coordinator_count": 1,
                "workers_per_coordinator": 2,
                "quorum_split_threshold": 6,
                "quorum_prune_threshold": 2,
                "enable_work_stealing": True,
                "max_concurrency": 4,
                "subagent_excerpt_chars": 3600,
                "executor_excerpt_chars": 3600,
                "recovery": {
                    "model": "ollama/gemma3:12b",
                    "timeout_seconds": 60,
                    "max_context_chars": 6500,
                    "max_reply_tokens": 768,
                    "max_workers_per_mission": 3,
                    "allow_worker_hotfixes": True,
                },
            }
            return {
                "ok": True,
                "text": json.dumps(proposed),
                "model": model_id,
                "provider": prov,
                "tokens_used": tokens,
            }
        # Generation 2: propose no-gain bloated excerpt to trigger AnytimeVal hold rollback
        bloated = {
            "coordinator_count": 1,
            "workers_per_coordinator": 4,
            "quorum_split_threshold": 6,
            "quorum_prune_threshold": 2,
            "enable_work_stealing": True,
            "max_concurrency": 4,
            "subagent_excerpt_chars": 5000,
            "executor_excerpt_chars": 5000,
            "recovery": {
                "model": "ollama/gemma3:12b",
                "timeout_seconds": 60,
                "max_context_chars": 7000,
                "max_reply_tokens": 768,
                "max_workers_per_mission": 3,
                "allow_worker_hotfixes": True,
            },
        }
        return {
            "ok": True,
            "text": json.dumps(bloated),
            "model": model_id,
            "provider": prov,
            "tokens_used": tokens,
        }

    if role == "coordinator" and "Swarm Algorithm Architect" in system_prompt:
        plan = {
            "algorithm": "quorum_work_steal",
            "theta": 0.55,
            "communication_protocol": "blackboard_handoff",
            "workstreams": [
                {"key": "subagent_1", "title": "Inspect primary module", "role": "research_scout", "stimulus": 1.1},
                {"key": "subagent_2", "title": "Verify secondary invariants", "role": "execution_specialist", "stimulus": 1.2},
            ],
        }
        return {"ok": True, "text": json.dumps(plan), "model": model_id, "provider": prov, "tokens_used": tokens}

    if role == "planner_architect":
        return {
            "ok": True,
            "text": "1. Scan workspace files.\n2. Synthesize grounded evidence.\n3. Verify safety lock.",
            "model": model_id,
            "provider": prov,
            "tokens_used": tokens,
        }

    if role == "critic_verifier":
        if "INSUFFICIENT_CONTEXT" in user_prompt:
            return {
                "ok": True,
                "text": "REVIEW: Deliverable lacks required context.",
                "model": model_id,
                "provider": prov,
                "tokens_used": tokens,
            }
        return {
            "ok": True,
            "text": "PASS: Grounded citations verified and execution_authorized=False intact.",
            "model": model_id,
            "provider": prov,
            "tokens_used": tokens,
        }

    # Repair turn or patch creation
    if "helper_check.py" in user_prompt and "```write_file:" in user_prompt:
        return {
            "ok": True,
            "text": "Repaired syntax error.\n```write_file:helper_check.py\ndef check_ready() -> bool:\n    return True\n```",
            "model": model_id,
            "provider": prov,
            "tokens_used": tokens,
        }
    if "gate_probe.py" in user_prompt and "```write_file:" in user_prompt:
        return {
            "ok": True,
            "text": "Repaired probe module.\n```write_file:gate_probe.py\ndef probe_status() -> bool:\n    return True\n```",
            "model": model_id,
            "provider": prov,
            "tokens_used": tokens,
        }

    # Standard grounded scout / executor / recovery response
    findings = ["Verified EXECUTION_AUTHORIZED = False in safety_gate.py (line 1); RETRY_CEILING = 3."]
    if "TAIL_ANCHOR_VALUE = 42099" in user_prompt:
        findings.append("Confirmed TAIL_ANCHOR_VALUE = 42099 in deep_config.py.")
    if "LEDGER_ZERO_SUM_SEAL = 'SEAL_OK_991'" in user_prompt:
        findings.append("Confirmed LEDGER_ZERO_SUM_SEAL = 'SEAL_OK_991' in ledger_reconcile.py.")
    return {
        "ok": True,
        "text": " ".join(findings),
        "model": model_id,
        "provider": prov,
        "tokens_used": tokens,
    }


class TestSwarmEvoBench(unittest.TestCase):
    def test_three_way_split_isolation_and_canary_never_leaks_held_out_to_mutator(self) -> None:
        suite = build_default_benchmark_suite()
        search_ids = {t.task_id for t in suite["search"]}
        gate_ids = {t.task_id for t in suite["regression_gate"]}
        held_ids = {t.task_id for t in suite["held_out"]}

        self.assertEqual(len(search_ids & gate_ids), 0)
        self.assertEqual(len(search_ids & held_ids), 0)
        self.assertEqual(len(gate_ids & held_ids), 0)
        self.assertIn("heldout_canary_ledger_domain_shift", held_ids)
        self.assertTrue(any(t.domain_canary for t in suite["held_out"]))

        mutator_prompts: list[str] = []

        def spy_caller(role: str, model_id: str, sys_p: str, usr_p: str) -> dict[str, Any]:
            if "unassisted stand-off benchmark" in sys_p:
                mutator_prompts.append(usr_p)
            return _deterministic_bench_llm(role, model_id, sys_p, usr_p)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            runner = SwarmEvoBenchmarkRunner(
                llm_caller=spy_caller,
                db_path=tmp_dir / "eval.sqlite3",
                artifacts_dir=tmp_dir / "evo_bench",
            )
            runner.run_evolution_round(generations=1)

        self.assertEqual(len(mutator_prompts), 1)
        prompt_text = mutator_prompts[0]
        for s_id in search_ids:
            self.assertIn(s_id, prompt_text)
        for h_id in held_ids:
            self.assertNotIn(h_id, prompt_text)
        for g_id in gate_ids:
            self.assertNotIn(g_id, prompt_text)
        self.assertNotIn("ledger_reconcile.py", prompt_text)
        self.assertNotIn("LEDGER_ZERO_SUM_SEAL", prompt_text)

    def test_regression_gate_rejects_broken_task_and_rolls_back_to_peak_snapshot(self) -> None:
        mutator_parent_hashes: list[str] = []

        def regression_then_valid_caller(role: str, model_id: str, sys_p: str, usr_p: str) -> dict[str, Any]:
            if "unassisted stand-off benchmark" in sys_p:
                # Record which scaffold hash this generation branched from
                for line in usr_p.splitlines():
                    if "Current Active Peak Scaffold (hash=" in line:
                        mutator_parent_hashes.append(line.split("hash=")[1].split(")")[0])
                if "Generation: 1" in usr_p:
                    # Break gate_timeout_resilience by setting max_workers_per_mission = 0!
                    broken = SwarmScaffold(
                        subagent_excerpt_chars=3600,
                        executor_excerpt_chars=3600,
                        recovery=RecoveryPolicy(max_workers_per_mission=0),
                    ).to_dict()
                    return {"ok": True, "text": json.dumps(broken), "model": model_id, "provider": "gemini", "tokens_used": 60}
                # Generation 2: fix excerpt window while keeping recovery intact
                good = SwarmScaffold(
                    workers_per_coordinator=2,
                    subagent_excerpt_chars=3600,
                    executor_excerpt_chars=3600,
                    recovery=RecoveryPolicy(max_workers_per_mission=3),
                ).to_dict()
                return {"ok": True, "text": json.dumps(good), "model": model_id, "provider": "gemini", "tokens_used": 60}
            return _deterministic_bench_llm(role, model_id, sys_p, usr_p)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            seed = SwarmScaffold()
            runner = SwarmEvoBenchmarkRunner(
                llm_caller=regression_then_valid_caller,
                db_path=tmp_dir / "eval.sqlite3",
                artifacts_dir=tmp_dir / "evo_bench",
            )
            summary = runner.run_evolution_round(seed_scaffold=seed, generations=2)

        gens = summary["generations"]
        self.assertEqual(gens[0]["status"], "SEED_BASELINE")
        self.assertEqual(gens[1]["status"], "REJECTED_ROLLBACK")
        self.assertGreaterEqual(gens[1]["gate_regressions"], 1)
        self.assertGreater(gens[1]["regression_rate"], 0.0)
        self.assertIn("gate_timeout_resilience", gens[1]["reason"])

        # Generation 2 MUST have branched from seed (peak snapshot rollback), NOT from rejected Gen 1!
        self.assertEqual(mutator_parent_hashes, [seed.scaffold_hash, seed.scaffold_hash])
        self.assertEqual(gens[2]["status"], "PROMOTED")
        self.assertEqual(gens[2]["gate_regressions"], 0)
        self.assertEqual(summary["promoted_hash"], gens[2]["scaffold_hash"])

    def test_max_llm_calls_per_task_confound_guard_blocks_call_inflation(self) -> None:
        multi_task = BenchmarkTask(
            task_id="search_call_inflation",
            split="search",
            goal="## 1. Check safety_gate.py\n## 2. Check core_service.py\n## 3. Check deep_config.py",
            perturbation="none",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            runner = SwarmEvoBenchmarkRunner(
                llm_caller=_deterministic_bench_llm,
                db_path=tmp_dir / "eval.sqlite3",
                artifacts_dir=tmp_dir / "evo_bench",
                max_llm_calls_per_task=4,  # Tight cap: 4 base stages + sub-agents + carbon > 4 calls
            )
            eval_res = runner._evaluate_split(SwarmScaffold(workers_per_coordinator=6), [multi_task])

        self.assertEqual(eval_res["pass_rate"], 0.0)
        outcome = eval_res["outcomes"][0]
        self.assertFalse(outcome["passed"])
        self.assertGreater(outcome["llm_calls"], 4)
        self.assertTrue(any("Exceeded max_llm_calls_per_task" in iss for iss in outcome["issues"]))

    def test_self_analytical_round_improves_seed_scaffold_and_persists_wal_sqlite(self) -> None:
        # Also verify PerturbationInjector determinism across resets
        inj = PerturbationInjector(_deterministic_bench_llm)
        t_timeout = BenchmarkTask(task_id="t1", split="search", goal="test", perturbation="executor_timeout_once")
        inj.set_task(t_timeout)
        r1_step1 = inj("execution_specialist", "gemini-4-argon", "sys", "usr")
        r1_step2 = inj("execution_specialist", "gemini-4-argon", "sys", "usr")
        inj.set_task(t_timeout)
        r2_step1 = inj("execution_specialist", "gemini-4-argon", "sys", "usr")
        self.assertFalse(r1_step1["ok"])
        self.assertTrue(r1_step2["ok"])
        self.assertEqual(r1_step1["error"], r2_step1["error"])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_path = tmp_dir / "sabrina_evaluations.sqlite3"
            art_dir = tmp_dir / "evo_bench"
            progress_events: list[dict[str, Any]] = []
            runner = SwarmEvoBenchmarkRunner(
                llm_caller=_deterministic_bench_llm,
                db_path=db_path,
                artifacts_dir=art_dir,
                progress_cb=progress_events.append,
            )
            summary = runner.run_evolution_round(generations=2, transfer_model="ollama/gemma3:12b")

            self.assertTrue(summary["ok"])
            self.assertFalse(summary["execution_authorized"])
            self.assertEqual(summary["final_regression_rate"], 0.0)
            self.assertEqual(summary["held_out_seed_pass_rate"], 0.0)
            self.assertEqual(summary["held_out_promoted_pass_rate"], 1.0)
            self.assertEqual(summary["net_capability_gain"], 1.0)
            self.assertGreater(len(progress_events), 2)

            # Generation 1 promoted, Generation 2 (bloated no-gain) rejected via AnytimeVal hold
            self.assertEqual(summary["generations"][1]["status"], "PROMOTED")
            self.assertEqual(summary["generations"][2]["status"], "REJECTED_ROLLBACK")
            self.assertIn("AnytimeVal", summary["generations"][2]["reason"])

            # Verify Markdown scorecard written
            report_file = Path(summary["report_path"])
            self.assertTrue(report_file.is_file())
            md = report_file.read_text(encoding="utf-8")
            self.assertIn("Sabrina Evo-Bench Scorecard", md)
            self.assertIn("`execution_authorized`):** `False`", md)

            # Verify SQLite WAL mode & persisted tables
            with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
                mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
                self.assertEqual(str(mode).lower(), "wal")
                runs_cnt = conn.execute("SELECT COUNT(*) FROM evo_bench_runs").fetchone()[0]
                gens_cnt = conn.execute("SELECT COUNT(*) FROM evo_bench_generations").fetchone()[0]
                tasks_cnt = conn.execute("SELECT COUNT(*) FROM evo_bench_task_outcomes").fetchone()[0]
            self.assertEqual(runs_cnt, 1)
            self.assertEqual(gens_cnt, 3)  # gen-000, gen-001, gen-002
            self.assertEqual(tasks_cnt, 18)  # 3 gens * (3 search + 3 gate)

    def test_cross_model_transfer_and_bridge_apply_promoted_scaffold(self) -> None:
        recorded_models: list[str] = []

        def tracking_caller(role: str, model_id: str, sys_p: str, usr_p: str) -> dict[str, Any]:
            recorded_models.append(model_id)
            return _deterministic_bench_llm(role, model_id, sys_p, usr_p)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            db_path = tmp_dir / "sabrina_evaluations.sqlite3"
            pol_path = tmp_dir / "recovery_policy.json"
            runner = SwarmEvoBenchmarkRunner(
                llm_caller=tracking_caller,
                db_path=db_path,
                artifacts_dir=tmp_dir / "evo_bench",
            )
            summary = runner.run_evolution_round(generations=1, transfer_model="ollama/gemma3:12b")

            self.assertEqual(summary["transfer_model"], "ollama/gemma3:12b")
            self.assertEqual(summary["transfer_gain"], 1.0)
            self.assertIn("ollama/gemma3:12b", recorded_models)
            self.assertFalse(any(m.startswith("ollama/ollama/") for m in recorded_models))

            # Verify SabrinaDesktopBridge endpoints read WAL DB and apply promoted scaffold safely
            vault = ProviderKeyVault(vault_path=tmp_dir / "vault.json")
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "roles.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            bridge._evo_bench_db_path = db_path  # type: ignore[attr-defined]
            bridge._evo_bench_policy_path = pol_path  # type: ignore[attr-defined]

            state = bridge.get_evo_bench_state()
            self.assertTrue(state["ok"])
            self.assertFalse(state["execution_authorized"])
            self.assertEqual(state["latest_summary"]["run_id"], summary["run_id"])

            applied = bridge.apply_promoted_scaffold(summary["run_id"])
            self.assertTrue(applied["ok"])
            self.assertFalse(applied["execution_authorized"])
            self.assertEqual(applied["promoted_hash"], summary["promoted_hash"])
            self.assertEqual(bridge.swarm_runner.subagent_excerpt_chars, 3600)
            self.assertEqual(bridge.swarm_runner.executor_excerpt_chars, 3600)
            self.assertEqual(bridge.swarm_runner.workers_per_coordinator, 2)
            self.assertTrue(pol_path.is_file())
            loaded_pol = RecoveryPolicy.load(pol_path)
            self.assertEqual(loaded_pol.max_context_chars, 6500)

    def test_unassisted_standoff_honestly_grades_zero_gain_when_model_fails_limit_and_passes_when_model_learns(self) -> None:
        # 1. Honest 0% grade when a model fails to understand the limit (no Python cheat rescues it!)
        def stubborn_weak_model(role: str, model_id: str, sys_p: str, usr_p: str) -> dict[str, Any]:
            if "unassisted stand-off benchmark" in sys_p:
                timid = SwarmScaffold(
                    workers_per_coordinator=4,
                    subagent_excerpt_chars=1400,
                    executor_excerpt_chars=1600,
                ).to_dict()
                return {"ok": True, "text": json.dumps(timid), "model": model_id, "provider": "gemini", "tokens_used": 55}
            return _deterministic_bench_llm(role, model_id, sys_p, usr_p)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            seed = SwarmScaffold()
            runner_weak = SwarmEvoBenchmarkRunner(
                llm_caller=stubborn_weak_model,
                db_path=tmp_dir / "weak.sqlite3",
                artifacts_dir=tmp_dir / "weak_art",
            )
            weak_summary = runner_weak.run_evolution_round(seed_scaffold=seed, generations=2)

        self.assertEqual(weak_summary["generations"][1]["status"], "REJECTED_ROLLBACK")
        self.assertEqual(weak_summary["generations"][2]["status"], "REJECTED_ROLLBACK")
        self.assertEqual(weak_summary["promoted_hash"], seed.scaffold_hash)
        self.assertEqual(weak_summary["net_capability_gain"], 0.0)
        self.assertEqual(weak_summary["held_out_promoted_pass_rate"], 0.0)

        # 2. Honest +100% grade when a model reads PAST ATTEMPT LOG and adjusts on its own reasoning
        gen2_prompts: list[str] = []

        def learning_model(role: str, model_id: str, sys_p: str, usr_p: str) -> dict[str, Any]:
            if "unassisted stand-off benchmark" in sys_p:
                if "Generation: 1" in usr_p:
                    timid = SwarmScaffold(executor_excerpt_chars=1600, subagent_excerpt_chars=1400).to_dict()
                    return {"ok": True, "text": json.dumps(timid), "model": model_id, "provider": "gemini", "tokens_used": 55}
                gen2_prompts.append(usr_p)
                smart = SwarmScaffold(
                    workers_per_coordinator=2,
                    subagent_excerpt_chars=3600,
                    executor_excerpt_chars=3600,
                ).to_dict()
                return {"ok": True, "text": json.dumps(smart), "model": model_id, "provider": "gemini", "tokens_used": 55}
            return _deterministic_bench_llm(role, model_id, sys_p, usr_p)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            runner_smart = SwarmEvoBenchmarkRunner(
                llm_caller=learning_model,
                db_path=tmp_dir / "smart.sqlite3",
                artifacts_dir=tmp_dir / "smart_art",
            )
            smart_summary = runner_smart.run_evolution_round(generations=2)

        self.assertEqual(len(gen2_prompts), 1)
        self.assertIn("PAST ATTEMPT LOG", gen2_prompts[0])
        self.assertEqual(smart_summary["generations"][1]["status"], "REJECTED_ROLLBACK")
        self.assertEqual(smart_summary["generations"][2]["status"], "PROMOTED")
        self.assertEqual(smart_summary["net_capability_gain"], 1.0)

    def test_basename_excerpt_lookup_and_no_dangling_tool_block_deliverable(self) -> None:
        from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            nested = tmp_dir / "ws" / "sabrina_console_desktop"
            nested.mkdir(parents=True)
            (nested / "live_swarm_runner.py").write_text(
                "def terminate_at_depth_four() -> str:\n    return 'STOPPED_AT_4'\n",
                encoding="utf-8",
            )

            exec_calls = 0

            def tool_heavy_llm(role: str, model_id: str, sys_p: str, usr_p: str) -> dict[str, Any]:
                nonlocal exec_calls
                if role == "execution_specialist":
                    exec_calls += 1
                    # Emit tool:search until final synthesis prompt forces prose (no raw tool block left in deliverable or sub-agent!)
                    if "NO ```tool:...``` blocks" not in usr_p and exec_calls <= 6:
                        return {"ok": True, "text": "```tool:search\nterminate_at_depth_four\n```", "model": model_id, "provider": "gemini"}
                    return {
                        "ok": True,
                        "text": "Found terminate_at_depth_four in sabrina_console_desktop/live_swarm_runner.py:1.",
                        "model": model_id,
                        "provider": "gemini",
                    }
                return _deterministic_bench_llm(role, model_id, sys_p, usr_p)

            runner = LiveSwarmRunner(
                workspace_root=tmp_dir / "ws",
                artifacts_dir=tmp_dir / "missions",
                spindown_delay_sec=0.05,
                step_delay_sec=0.0,
                llm_caller=tool_heavy_llm,
            )
            try:
                # Verify bare basename 'live_swarm_runner.py' resolves to nested sabrina_console_desktop/live_swarm_runner.py
                ex = runner._read_targeted_file_excerpts("Inspect live_swarm_runner.py for terminate_at_depth_four")
                self.assertIn("sabrina_console_desktop/live_swarm_runner.py", ex)
                self.assertIn("terminate_at_depth_four", ex)

                res = runner.launch_mission("Trace live_swarm_runner.py termination line.")
                out = runner.wait_for_mission(res["mission_id"], timeout=10.0) or {}
                self.assertEqual(out.get("state"), "PASS")
                self.assertNotIn("```tool:", out.get("report_markdown") or "")
                self.assertIn("terminate_at_depth_four", out.get("report_markdown") or "")
            finally:
                runner.shutdown()


if __name__ == "__main__":
    unittest.main()
