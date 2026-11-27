"""Integration tests for SabrinaDesktopBridge role bindings and vault wiring."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from swarm_console.vault import ProviderKeyVault


def _mock_fetch_json(url: str, headers: dict[str, str] | None = None, timeout: float = 6.0) -> Any:
    if "openai.com" in url:
        return {"data": [{"id": "gpt-5"}, {"id": "gpt-5-mini"}]}
    if "generativelanguage.googleapis.com" in url:
        return {"models": [{"name": "models/gemini-4-carbon"}]}
    if "127.0.0.1:11434" in url:
        return {"models": [{"name": "qwen2.5:7b"}]}
    return {"data": []}


class TestConsoleBridgeWiring(unittest.TestCase):
    def test_health_check_uses_current_workers_missions_and_workspace(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = SabrinaDesktopBridge(role_bindings_path=root / "roles.json", vault_path=root / "vault.json")
            snapshot = {
                "api_ok": True, "api_8095_ok": True,
                "schema_audit": {"valid": True},
                "workspace": {"swarm_workspace_root": str(root)},
                "swarm": {
                    "workers": [{"status": "DONE"}, {"status": "WORKING"}],
                    "active_worker_count": 1, "active_missions": [{"id": "current"}],
                },
            }
            with patch.object(bridge, "get_snapshot", return_value=snapshot):
                health = bridge.run_health_check()
                self.assertEqual(health["status"], "HEALTHY")
                self.assertEqual(health["checks"]["swarm"], {"active_workers": 1, "active_missions": 1})
                snapshot["api_8095_ok"] = False
                self.assertEqual(bridge.run_health_check()["status"], "DEGRADED")
                snapshot["workspace"]["root"] = str(root / "missing")
                self.assertEqual(bridge.run_health_check()["status"], "UNHEALTHY")

    def test_bridge_vault_role_bindings_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(
                vault_path=tmp_dir / "provider_vault.json",
                fetch_json=_mock_fetch_json,
            )
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
            )

            status = bridge.get_vault_status()
            self.assertTrue(status["ok"])
            self.assertIn("ollama", status["unlocked_providers"])

            # Unlock openai with probe=True
            set_res = bridge.set_vault_key("openai", "sk-test-openai-1234", probe=True)
            self.assertTrue(set_res["ok"])
            self.assertIn("openai", set_res["vault"]["unlocked_providers"])

            # Role bindings reflect unlocked providers and allow comma-separated fallback updates.
            rb_list = bridge.get_role_bindings()
            self.assertTrue(rb_list["ok"])
            self.assertIn("openai", rb_list["unlocked_providers"])

            upd = bridge.set_role_binding(
                "coordinator",
                "gemini-4-carbon",
                "openai/gpt-5, anthropic/claude-sonnet-4-5",
            )
            self.assertTrue(upd["ok"])
            self.assertEqual(
                upd["binding"]["fallback_models"],
                ["openai/gpt-5", "anthropic/claude-sonnet-4-5"],
            )

            # Because gemini is locked and openai is unlocked, resolve_role_model picks openai/gpt-5.
            resolved = bridge.resolve_role_model("coordinator")
            self.assertTrue(resolved["ok"])
            self.assertEqual(resolved["selected_model"], "openai/gpt-5")
            self.assertTrue(resolved["used_fallback"])

            # If openai/gpt-5 is unhealthy, falls back to first healthy candidate.
            resolved_unhealthy = bridge.resolve_role_model(
                "coordinator", unhealthy_models=["openai/gpt-5"]
            )
            self.assertTrue(resolved_unhealthy["ok"])
            self.assertEqual(resolved_unhealthy["selected_model"], "gemini-4-carbon")
            self.assertFalse(resolved_unhealthy["unlocked"])

            # search_models(..., unlocked_only=True) excludes locked providers.
            bridge.registry.enable_remote = False
            search_res = bridge.search_models(query="gpt-5", unlocked_only=True)
            self.assertTrue(search_res["ok"])
            self.assertTrue(any(item["id"] == "openai/gpt-5" or item["id"] == "gpt-5" for item in search_res["items"]))

            # Delete key locks openai again.
            del_res = bridge.delete_vault_key("openai")
            self.assertTrue(del_res["ok"])
            self.assertNotIn("openai", del_res["vault"]["unlocked_providers"])

            # Snapshot includes vault and role_bindings.
            snap = bridge.get_snapshot()
            self.assertIn("vault", snap)
            self.assertIn("role_bindings", snap)
            self.assertTrue(snap["vault"]["ok"])
            self.assertGreaterEqual(len(snap["role_bindings"]), 7)

    def test_unified_fleet_add_remove_models_and_separate_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            skill_dir = tmp_dir / "skills" / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: Demo Skill\ndescription: Separate skill registry check.\n---\n# Demo Skill Body\n",
                encoding="utf-8",
            )

            vault = ProviderKeyVault(
                vault_path=tmp_dir / "provider_vault.json",
                fetch_json=_mock_fetch_json,
            )
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            bridge.registry.enable_remote = False
            bridge.registry.skill_roots = [tmp_dir / "skills"]

            # Add external, collective, and local Ollama models
            ext = bridge.add_custom_model(
                "openrouter/deepseek/deepseek-r1",
                name="DeepSeek R1 External",
                provider="openrouter",
                tier="external",
                role="External Reasoner",
            )
            self.assertTrue(ext["ok"])
            self.assertEqual(ext["model"]["tier"], "external")
            self.assertTrue(ext["model"]["custom"])

            loc = bridge.add_custom_model(
                "qwen2.5-coder:14b",
                name="Qwen 2.5 Coder 14B",
                provider="ollama",
                tier="local",
                role="Local Verifier",
            )
            self.assertTrue(loc["ok"])
            self.assertEqual(loc["model"]["id"], "ollama/qwen2.5-coder:14b")
            self.assertEqual(loc["model"]["tier"], "local")

            # Verify tier filtering in unified fleet
            ext_only = bridge.search_models(tier="external")
            self.assertTrue(ext_only["ok"])
            self.assertTrue(any(m["id"] == "openrouter/deepseek/deepseek-r1" for m in ext_only["items"]))
            self.assertTrue(all(m["tier"] == "external" for m in ext_only["items"]))

            loc_only = bridge.search_models(tier="local")
            self.assertTrue(loc_only["ok"])
            self.assertTrue(any(m["id"] == "ollama/qwen2.5-coder:14b" for m in loc_only["items"]))

            # Verify persistence across a fresh bridge instance and removal
            bridge2 = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            bridge2.registry.enable_remote = False
            reloaded = bridge2.search_models(query="deepseek-r1")
            self.assertEqual(reloaded["total"], 1)

            rem = bridge2.remove_custom_model("openrouter/deepseek/deepseek-r1")
            self.assertTrue(rem["ok"])
            self.assertTrue(rem["removed"])
            after_rem = bridge2.search_models(query="deepseek-r1")
            self.assertEqual(after_rem["total"], 0)

            # Separate skills registry search + SKILL.md inspection
            skills_res = bridge.search_skills(query="Separate skill")
            self.assertTrue(skills_res["ok"])
            self.assertEqual(skills_res["total"], 1)
            detail_res = bridge.get_skill_detail("demo-skill")
            self.assertTrue(detail_res["ok"])
            self.assertIn("# Demo Skill Body", detail_res["skill"]["content"])

    def test_ui_html_panes_and_dom_ids_have_no_missing_elements(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        ui_files = [
            repo_root / "sabrina_console_desktop" / "sabrina_desktop_ui.html",
            repo_root / "sabrina_desktop_ui.html",
            repo_root / "swarm_console" / "ui.html",
        ]
        for html_path in ui_files:
            with self.subTest(html_path=str(html_path)):
                html = html_path.read_text(encoding="utf-8")
                self.assertNotIn("Gemini Swarm Copilot", html)
                self.assertNotIn("700–800+", html)
                self.assertNotIn("Search 700+", html)
                self.assertIn("Collective &amp; External Model Fleet", html)
                self.assertIn("Add Model to Fleet", html)
                self.assertIn("Skill Registry", html)

                declared_ids = set(re.findall(r'\bid="([^"]+)"', html))
                referenced_ids = set(re.findall(r'\bel\("([a-zA-Z0-9_-]+)"\)', html))
                missing = sorted(referenced_ids - declared_ids)
                self.assertEqual(missing, [], f"Missing DOM IDs in {html_path.name}: {missing}")

                if "sabrina_desktop_ui" in html_path.name:
                    self.assertIn("chat-toggle-deliberation", declared_ids)
                    self.assertIn("deliberation-stream", declared_ids)
                    self.assertIn("deliberation-count-badge", declared_ids)
                    self.assertIn("jobs-status-filter", declared_ids)
                    self.assertIn("btn-refresh-jobs", declared_ids)
                    self.assertIn("jobs-feedback", declared_ids)
                    self.assertIn("jobs-count-badge", declared_ids)
                    # Top Operator Console Navigation tabs
                    self.assertNotIn('data-subtab="lab"', html)
                    self.assertIn("Model Fleet &amp; Vault", html)
                    self.assertIn('data-subtab="schema"', html)
                    self.assertNotIn('data-subtab="monitor"', html)
                    self.assertNotIn("advanceSimTick", html)
                    self.assertIn("api-8095-status", declared_ids)
                    # Swarm Schema View & Interactive Builder (.JSON / .MD) elements
                    for schema_id in (
                        "subview-schema",
                        "schema-dropzone",
                        "schema-file-input",
                        "schema-json-editor",
                        "btn-schema-load-sample",
                        "btn-schema-prettify",
                        "btn-schema-validate",
                        "btn-schema-apply",
                        "btn-schema-export",
                        "btn-schema-export-md",
                        "builder-max-depth",
                        "builder-max-concurrency",
                        "builder-conf-gate",
                        "builder-agent-id",
                        "builder-agent-role",
                        "builder-agent-model",
                        "builder-model-options",
                        "builder-agent-max-children",
                        "btn-builder-add-agent",
                        "builder-rule-id",
                        "builder-rule-parent",
                        "builder-rule-child",
                        "builder-rule-logprob",
                        "btn-builder-add-rule",
                        "schema-stat-depth",
                        "schema-stat-concurrency",
                        "schema-stat-confidence",
                        "schema-rules-list",
                        "schema-vault-body",
                    ):
                        self.assertIn(schema_id, declared_ids)
                    self.assertIn(".md", html)
                    self.assertIn("parseSwarmMarkdown", html)
                    self.assertIn("serializeSwarmToMarkdown", html)
                    self.assertIn("buildSwarmDag", html)
                    self.assertIn("computeLineage", html)
                    self.assertIn("parentIds", html)
                    self.assertIn("VAULT_LOCKED", html)
                    self.assertIn("GATE_BLOCKED", html)
                    # Live Swarm Monitor Topology elements (simulator removed)
                    for monitor_id in (
                        "topo-logprob-badge",
                        "topo-node-detail-bar",
                        "topo-inspect-vault",
                        "topo-event-log",
                    ):
                        self.assertIn(monitor_id, declared_ids)
                    for removed_sim_id in (
                        "subview-monitor",
                        "btn-sys-sim-spawn",
                        "btn-sys-sim-pause",
                        "btn-sys-sim-reset",
                        "sys-topo-svg",
                    ):
                        self.assertNotIn(removed_sim_id, declared_ids)
                    swarm_match = re.search(r'<section id="view-swarm"[^>]*>(.*?)</section>', html, re.S)
                    chat_match = re.search(r'<section id="view-chat"[^>]*>(.*?)</section>', html, re.S)
                    self.assertIsNotNone(swarm_match)
                    self.assertIsNotNone(chat_match)
                    assert swarm_match is not None and chat_match is not None
                    self.assertNotIn("Launch &amp; Control Swarm", swarm_match.group(1))
                    self.assertIn("Launch &amp; Control Swarm", chat_match.group(1))
                    self.assertIn('id="btn-launch"', chat_match.group(1))
                    self.assertNotIn("btn-sim-spawn", swarm_match.group(1))
                    self.assertNotIn("sim-vault-mode", swarm_match.group(1))
                    self.assertNotIn("▶ Simulate Swarm Spawn", swarm_match.group(1))

    def test_chat_history_isolated_from_board_and_deduplicated_roster(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(
                vault_path=tmp_dir / "provider_vault.json",
                fetch_json=_mock_fetch_json,
            )
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            bridge.registry.enable_remote = False

            # Unlock openai so gpt-5 is in unlocked vault models.
            bridge.set_vault_key("openai", "sk-test-openai-1234", probe=True)

            # Default chat model resolves to unlocked fallback openai/gpt-5.
            self.assertEqual(bridge._get_default_chat_model(), "openai/gpt-5")

            # Add local model with same canonical key as an ollama model and check _get_local_models dedup.
            runtime_models = [
                {"id": "ollama/qwen2.5:7b", "name": "qwen2.5:7b", "provider": "Ollama (Local)", "tier": "local"},
                {"id": "local:qwen2.5:7b", "name": "qwen2.5:7b", "provider": "local", "tier": "local"},
            ]
            local_models = bridge._get_local_models(runtime_models=runtime_models)
            qwen_entries = [m for m in local_models if "qwen2.5:7b" in m["id"]]
            self.assertEqual(len(qwen_entries), 1)

            # Add collective summary display strings and openrouter:openai while openrouter is locked;
            # neither should leak into _get_unlocked_vault_models() / chat_roster.
            bridge.registry.set_runtime_fleet(
                [
                    {
                        "id": "gpt-5 / o3 / o4-mini (low / medium / high)",
                        "name": "Codex Collective",
                        "provider": "openai",
                        "tier": "collective",
                    },
                    {
                        "id": "openrouter/openai/gpt-5",
                        "name": "OpenRouter GPT-5",
                        "provider": "openrouter:openai",
                        "tier": "external",
                        "capabilities": ["chat"],
                    },
                ]
            )
            unlocked_models = bridge._get_unlocked_vault_models()
            unlocked_ids = [m["id"] for m in unlocked_models]
            self.assertTrue(any("gpt-5" in mid for mid in unlocked_ids))
            self.assertNotIn("gpt-5 / o3 / o4-mini (low / medium / high)", unlocked_ids)
            self.assertNotIn("openrouter/openai/gpt-5", unlocked_ids)

            # Build roster with overlapping local + vault canonical key; must deduplicate and have 0 worker IDs.
            roster = bridge._build_chat_roster(runtime_models=runtime_models)
            roster_ids = [r["id"] for r in roster]
            self.assertIn("hive_council", roster_ids)
            for worker_id in ("hw-c99-replay", "lead-architect", "indep-verifier", "hw-rust-ffi"):
                self.assertNotIn(worker_id, roster_ids)
            canon_keys = [bridge._canonical_model_key(r["id"]) for r in roster if r["id"] != "hive_council"]
            self.assertEqual(len(canon_keys), len(set(canon_keys)))

            # Verify get_snapshot() isolates swarm_board_messages from chat_history and exposes telemetry & deliberations.
            snap = bridge.get_snapshot()
            self.assertIn("swarm_board_messages", snap)
            self.assertIn("board_messages", snap["swarm"])
            self.assertEqual(snap["chat_history"], [])
            self.assertIn("api_8095_ok", snap)
            self.assertIn("active_prompt_state", snap)
            self.assertIn("frontier_research_state", snap)
            self.assertIn("swarm_deliberations", snap)
            self.assertIn("swarm_active_handoffs", snap)

            # Custom local:* target not in LOCAL_FLEET must NOT be hijacked by _is_portal_target;
            # it should route to _dispatch_ollama_chat.
            self.assertFalse(bridge._is_portal_target("local:custom-not-in-portal"))
            with patch(
                "sabrina_console_desktop.sabrina_desktop_ui._http_json",
                return_value={"message": {"content": "Hello from Ollama"}},
            ):
                local_res = bridge.send_chat(
                    "Hi local", mode="direct_node", target="local:custom-not-in-portal"
                )
                self.assertTrue(local_res["ok"])
                self.assertEqual(local_res["provider"], "ollama")
                self.assertEqual(local_res["reply"], "Hello from Ollama")

            # Direct node chat dispatch to unlocked vault model works and appends to chat_history.
            with patch(
                "sabrina_console_desktop.sabrina_desktop_ui._http_json",
                return_value={"choices": [{"message": {"content": "Hello from GPT-5"}}]},
            ):
                res = bridge.send_chat("Hi GPT-5", mode="direct_node", target="openai/gpt-5")
                self.assertTrue(res["ok"])
                self.assertEqual(res["reply"], "Hello from GPT-5")

            snap_after = bridge.get_snapshot()
            self.assertEqual(len(snap_after["chat_history"]), 4)
            self.assertEqual(snap_after["chat_history"][2]["sender"], "operator")
            self.assertEqual(snap_after["chat_history"][3]["text"], "Hello from GPT-5")
            for msg in snap_after["chat_history"]:
                self.assertNotIn("[BRIEF]", msg["text"])
                self.assertNotIn("[PASS]", msg["text"])

    def test_live_swarm_idle_zero_workers_and_mission_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(
                vault_path=tmp_dir / "provider_vault.json",
                fetch_json=_mock_fetch_json,
            )
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
                spindown_delay_sec=0.15,
                step_delay_sec=0.02,
            )
            bridge.registry.enable_remote = False
            try:
                # Idle state: 0 child workers, no static Track B workers leaked, execution locked.
                snap_idle = bridge.get_snapshot()
                swarm_idle = snap_idle["swarm"]
                self.assertEqual(swarm_idle["workers"], [])
                self.assertEqual(swarm_idle["active_worker_count"], 0)
                self.assertEqual(swarm_idle["status"], "IDLE")
                self.assertFalse(swarm_idle["execution_authorized"])
                self.assertFalse(snap_idle["kraken"]["execution_authorized"])

                # Empty goal fails cleanly without spawning workers.
                bad = bridge.launch_mission("   ", mode="codex_thought_antigravity_implement", physical_input=False)
                self.assertFalse(bad["ok"])
                self.assertFalse(bad["execution_authorized"])

                # Launch a real mission: child workers dynamically spawn and execute bounded work.
                launch_res = bridge.launch_mission(
                    "Audit swarm_console protocol and role bindings",
                    mode="codex_thought_antigravity_implement",
                    physical_input=False,
                )
                self.assertTrue(launch_res["ok"])
                self.assertFalse(launch_res["execution_authorized"])
                mission_id = launch_res["mission_id"]
                self.assertGreaterEqual(len(launch_res["workers_spawned"]), 3)

                snap_active = bridge.get_snapshot()
                swarm_active = snap_active["swarm"]
                self.assertGreaterEqual(len(swarm_active["workers"]), 1)
                self.assertFalse(swarm_active["execution_authorized"])
                self.assertTrue(any(obj.get("id") == mission_id for obj in swarm_active["objectives"]))

                # Wait for mission completion and automatic worker spin-down back to 0.
                self.assertTrue(bridge.swarm_runner.wait_for_mission(mission_id, timeout=10.0))
                self.assertTrue(bridge.swarm_runner.wait_until_idle(timeout=10.0))

                snap_done = bridge.get_snapshot()
                swarm_done = snap_done["swarm"]
                self.assertEqual(swarm_done["workers"], [])
                self.assertEqual(swarm_done["active_worker_count"], 0)
                self.assertEqual(swarm_done["status"], "IDLE")
                self.assertGreaterEqual(len(swarm_done["recent_spindowns"]), 3)
                self.assertGreaterEqual(len(swarm_done["mission_outputs"]), 1)
                self.assertGreaterEqual(len(swarm_done["activity_feed"]), 1)
                self.assertGreaterEqual(len(swarm_done["research_episodes"]), 1)
                self.assertFalse(swarm_done["execution_authorized"])

                # Verify real mission report artifact written in isolated temp missions dir.
                out_res = bridge.get_mission_output(mission_id)
                self.assertTrue(out_res["ok"])
                self.assertEqual(out_res["mission_id"], mission_id)
                self.assertFalse(out_res["execution_authorized"])
                artifact_path = Path(out_res["artifact_path"])
                self.assertTrue(artifact_path.is_file())
                self.assertEqual(artifact_path.parent.resolve(), (tmp_dir / "missions").resolve())
                self.assertGreater(len(out_res["report_markdown"]), 50)

                # Clear completed state cleanly.
                cleared = bridge.clear_completed()
                self.assertTrue(cleared["ok"])
                self.assertFalse(cleared["execution_authorized"])
                snap_cleared = bridge.get_snapshot()
                self.assertEqual(snap_cleared["swarm"]["mission_outputs"], [])
                self.assertEqual(snap_cleared["swarm"]["recent_spindowns"], [])
            finally:
                bridge.swarm_runner.shutdown()

    def test_manual_spawn_spindown_and_stop_mission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(
                vault_path=tmp_dir / "provider_vault.json",
                fetch_json=_mock_fetch_json,
            )
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
                spindown_delay_sec=5.0,
                step_delay_sec=0.15,
            )
            bridge.registry.enable_remote = False
            try:
                self.assertEqual(bridge.get_snapshot()["swarm"]["workers"], [])

                # Manual spawn_worker mounts a live child worker.
                sp = bridge.spawn_worker(
                    "Custom Scout",
                    "research_scout",
                    model="ollama/qwen2.5:7b",
                    task="Inspect swarm_console files",
                )
                self.assertTrue(sp["ok"])
                self.assertFalse(sp["execution_authorized"])
                wid = sp["worker_id"]

                snap_spawned = bridge.get_snapshot()
                self.assertEqual(snap_spawned["swarm"]["active_worker_count"], 1)
                self.assertTrue(any(w["id"] == wid for w in snap_spawned["swarm"]["workers"]))

                # Manual spindown_worker immediately removes the worker.
                sd = bridge.spindown_worker(wid)
                self.assertTrue(sd["ok"])
                self.assertEqual(sd["active_worker_count"], 0)
                self.assertFalse(sd["execution_authorized"])
                snap_after_sd = bridge.get_snapshot()
                self.assertEqual(snap_after_sd["swarm"]["workers"], [])
                self.assertTrue(any(r["id"] == wid for r in snap_after_sd["swarm"]["recent_spindowns"]))

                # Launch mission and stop_mission immediately spins down all active workers to 0.
                launch_res = bridge.launch_mission(
                    "Inspect swarm_command router invariants",
                    mode="antigravity_thought_codex_implement",
                    physical_input=False,
                )
                self.assertTrue(launch_res["ok"])
                self.assertGreaterEqual(bridge.get_snapshot()["swarm"]["active_worker_count"], 1)

                stop_res = bridge.stop_mission()
                self.assertTrue(stop_res["ok"])
                self.assertEqual(stop_res["active_worker_count"], 0)
                self.assertFalse(stop_res["execution_authorized"])
                snap_stopped = bridge.get_snapshot()
                self.assertEqual(snap_stopped["swarm"]["workers"], [])
                self.assertEqual(snap_stopped["swarm"]["active_worker_count"], 0)
            finally:
                bridge.swarm_runner.shutdown()

    def test_schema_audit_deterministic_and_gated_flash(self) -> None:
        import json
        from sabrina_console_desktop.swarm_schema_audit import (
            audit_swarm_file,
            audit_swarm_payload,
            build_compact_sample,
            validate_swarm_payload,
        )

        # Clean payload -> 0 errors, 0 warnings, Flash NOT invoked
        calls: list[str] = []

        def fake_flash(prompt: str) -> str:
            calls.append(prompt)
            return "mock-flash-diagnosis"

        clean_payload = {
            "workers": [{"id": "w1", "status": "WORKING"}],
            "objectives": [{"id": "o1"}],
            "messages": [{"seq": 1}],
        }
        clean_res = audit_swarm_payload(clean_payload, flash_auditor=fake_flash)
        self.assertTrue(clean_res.valid)
        self.assertEqual(clean_res.errors, [])
        self.assertEqual(clean_res.warnings, [])
        self.assertIsNone(clean_res.flash_diagnosis)
        self.assertEqual(len(calls), 0)

        # Drifted file -> deterministic error + gated Flash invoked with compact sample
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bad_file = tmp_dir / "drifted.json"
            bad_file.write_text(
                json.dumps({"workers": [{"name": "no-id-1"}, {"name": "no-id-2"}, {"name": "no-id-3"}]}),
                encoding="utf-8",
            )
            sample = build_compact_sample({"workers": [1, 2, 3, 4], "extra": {"a": 1, "b": 2, "c": 3}}, max_items=2)
            self.assertEqual(len(sample["workers"]), 2)
            self.assertEqual(len(sample["extra"]), 2)

            file_res = audit_swarm_file(bad_file, flash_auditor=fake_flash)
            self.assertFalse(file_res.valid)
            self.assertTrue(any("missing string 'id'" in e for e in file_res.errors))
            self.assertEqual(file_res.flash_diagnosis, "mock-flash-diagnosis")
            self.assertEqual(len(calls), 1)

            # Bridge snapshot includes microsecond deterministic schema_audit and on-demand audit_swarm_schema
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
            bridge.registry.enable_remote = False
            try:
                snap = bridge.get_snapshot()
                self.assertIn("schema_audit", snap)
                self.assertTrue(snap["schema_audit"]["valid"])
                manual = bridge.audit_swarm_schema(use_flash=True, target="live", flash_auditor=fake_flash)
                self.assertTrue(manual["ok"])
                self.assertEqual(manual["flash_diagnosis"], "mock-flash-diagnosis")
                self.assertEqual(len(calls), 2)

                health_check = bridge.run_health_check()
                self.assertTrue(health_check["ok"])
                self.assertIn("status", health_check)
                self.assertIn("checks", health_check)
                self.assertIn("backend_8087", health_check["checks"])
                self.assertIn("vault", health_check["checks"])
                self.assertIn("schema", health_check["checks"])
                self.assertIn("workspace", health_check["checks"])
                self.assertTrue(health_check["checks"]["schema"]["ok"])

                log_tail = bridge.get_launch_log_tail(lines=20)
                self.assertTrue(log_tail["ok"])
                self.assertIsInstance(log_tail["lines"], list)

                # Verify save_swarm_schema persists schema, enforces execution_authorized=False, and syncs role bindings
                schema_payload = {
                    "schema_version": "2.1.0-collective",
                    "execution_authorized": True,
                    "agents": [
                        {"id": "sabrina", "role": "coordinator", "model": "openai/gpt-5", "status": "ACTIVE"},
                        {"id": "agy-exec", "role": "execution_specialist", "model": "gemini-4-argon", "status": "ACTIVE"},
                    ],
                    "objectives": [{"id": "o1"}],
                }
                save_res = bridge.save_swarm_schema(json.dumps(schema_payload), sync_role_bindings=True)
                self.assertTrue(save_res["ok"])
                self.assertFalse(save_res["execution_authorized"])
                self.assertIn("coordinator", save_res["updated_roles"])
                loaded_schema = bridge.get_swarm_schema()
                self.assertTrue(loaded_schema["ok"])
                self.assertTrue(loaded_schema["exists"])
                self.assertFalse(loaded_schema["schema"]["execution_authorized"])
                self.assertEqual(bridge.role_bindings.get_binding("coordinator").primary_model, "openai/gpt-5")

                # Verify INV-03 checks coordinator_count * workers_per_coordinator against max_concurrency
                ratio_schema = {
                    "schema_version": "2.1.0-collective",
                    "topology": {
                        "coordinator_count": 2,
                        "workers_per_coordinator": 6,
                        "max_concurrency": 8,
                    },
                    "agents": [
                        {"id": "sabrina", "role": "coordinator", "model": "openai/gpt-5", "status": "ACTIVE"},
                    ],
                }
                ratio_audit = bridge.audit_swarm_schema(use_flash=False, raw_schema=json.dumps(ratio_schema))
                self.assertTrue(ratio_audit["ok"])
                self.assertTrue(any("2:6 (12 workers) exceeds max_concurrency (8)" in w for w in ratio_audit["warnings"]))
            finally:
                bridge.swarm_runner.shutdown()

    def test_bridge_swarm_llm_wiring_and_honest_status(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(
                vault_path=tmp_dir / "provider_vault.json",
                fetch_json=_mock_fetch_json,
            )
            # 1. With no unlocked cloud key and no cached Ollama models, _call_swarm_llm fails fast & cleanly
            bridge_no_key = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
            )
            bridge_no_key.registry.enable_remote = False
            try:
                no_key_res = bridge_no_key._call_swarm_llm(
                    "execution_specialist", "gemini-3.5-flash-lite", "sys", "hello"
                )
                self.assertFalse(no_key_res["ok"])
                self.assertIn("No unlocked provider API key", no_key_res["error"])
                self.assertEqual(bridge_no_key.chat_log, [])

                # 2. Unlocking OpenAI routes _call_swarm_llm to OpenAI without writing to operator chat_log
                bridge_no_key.set_vault_key("openai", "sk-test-openai-swarm", probe=True)
                with patch(
                    "sabrina_console_desktop.sabrina_desktop_ui._http_json",
                    return_value={"choices": [{"message": {"content": "OpenAI swarm answer"}}]},
                ):
                    ok_res = bridge_no_key._call_swarm_llm(
                        "execution_specialist", "openai/gpt-5", "sys", "analyze"
                    )
                self.assertTrue(ok_res["ok"])
                self.assertEqual(ok_res["provider"], "openai")
                self.assertEqual(ok_res["text"], "OpenAI swarm answer")
                self.assertEqual(bridge_no_key.chat_log, [])
            finally:
                bridge_no_key.swarm_runner.shutdown()

            # 3. Bridge with injected llm_caller runs end-to-end LIVE_LLM mission
            def fake_live_caller(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                return {
                    "ok": True,
                    "text": f"PASS: Live response for {role}",
                    "model": "gemini-3.5-flash-lite",
                    "provider": "gemini",
                    "latency_ms": 9.0,
                }

            bridge_live = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings_live.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models_live.json",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
                llm_caller=fake_live_caller,
            )
            bridge_live.registry.enable_remote = False
            try:
                launch = bridge_live.launch_mission("Audit swarm_console live LLM mode")
                self.assertTrue(launch["ok"])
                out = bridge_live.swarm_runner.wait_for_mission(launch["mission_id"], timeout=8.0)
                self.assertIsNotNone(out)
                assert out is not None
                self.assertEqual(out["execution_mode"], "LIVE_LLM")
                self.assertEqual(out["live_llm_calls"], 4)
                self.assertEqual(out["state"], "PASS")
                self.assertFalse(out["execution_authorized"])
                self.assertTrue(bridge_live.swarm_runner.wait_until_idle(timeout=5.0))
            finally:
                bridge_live.swarm_runner.shutdown()

    def test_job_action_optimistic_update_and_normalization(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
            bridge.registry.enable_remote = False
            try:
                # Seed cache with sample job
                with bridge._remote_lock:
                    bridge._remote_cache["jobs"] = [
                        {"id": "job-123", "capability": "kraken.objective", "status": "draft", "state": "draft"}
                    ]
                with patch("sabrina_console_desktop.sabrina_desktop_ui._http_json", return_value={"ok": True}):
                    res = bridge.job_action("job-123", "run-now")
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["state"], "queued")
                    snap = bridge.get_snapshot()
                    matched = [j for j in snap.get("jobs", []) if j["id"] == "job-123"]
                    self.assertEqual(len(matched), 1)
                    self.assertEqual(matched[0]["state"], "queued")
                    self.assertEqual(matched[0]["status"], "queued")
            finally:
                bridge.swarm_runner.shutdown()

    def test_http_json_error_extraction_and_gemini_interactions_api(self) -> None:
        import io
        import json as _json
        import urllib.error
        from types import SimpleNamespace
        from unittest.mock import patch

        from sabrina_console_desktop.sabrina_desktop_ui import (
            _call_gemini_interactions,
            _extract_gemini_interactions_text,
            _http_json,
        )

        # 1. _http_json extracts JSON error.message and redacts secret headers
        err_body = _json.dumps(
            {"error": {"message": "Model gemini-old not found for key AIzaSecretKey123"}}
        ).encode("utf-8")
        http_err = urllib.error.HTTPError(
            url="https://generativelanguage.googleapis.com/v1beta/interactions",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(err_body),
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            with self.assertRaises(RuntimeError) as ctx:
                _http_json(
                    "https://generativelanguage.googleapis.com/v1beta/interactions",
                    method="POST",
                    payload={"model": "gemini-old", "input": "hi"},
                    headers={"x-goog-api-key": "AIzaSecretKey123"},
                )
        self.assertIn("HTTP 400: Model gemini-old not found", str(ctx.exception))
        self.assertNotIn("AIzaSecretKey123", str(ctx.exception))
        self.assertIn("[REDACTED]", str(ctx.exception))

        # 2. _extract_gemini_interactions_text handles dict output_text, dict steps, and SDK objects
        self.assertEqual(
            _extract_gemini_interactions_text({"output_text": "Direct text"}),
            "Direct text",
        )
        self.assertEqual(
            _extract_gemini_interactions_text(
                {
                    "steps": [
                        {"type": "thinking", "content": [{"text": "skip"}]},
                        {"type": "model_output", "content": [{"text": "Hello "}, {"text": "World"}]},
                    ]
                }
            ),
            "Hello World",
        )
        sdk_obj = SimpleNamespace(
            output_text="",
            steps=[SimpleNamespace(type="model_output", content=[SimpleNamespace(text="SDK reply")])],
        )
        self.assertEqual(_extract_gemini_interactions_text(sdk_obj), "SDK reply")

        # 3. _call_gemini_interactions hits /v1beta/interactions via HTTP when SDK unavailable,
        # and falls back to generateContent on HTTP 400/404.
        calls: list[str] = []

        def _fake_http_json(url: str, method: str = "GET", payload: Any = None, timeout: float = 3.0, headers: Any = None) -> Any:
            calls.append(url)
            if url.endswith("/v1beta/interactions"):
                if payload and payload.get("model") == "gemma-3-27b":
                    raise RuntimeError("HTTP 400: Interactions not supported for gemma")
                return {"output_text": "Interactions OK"}
            if ":generateContent" in url:
                return {"candidates": [{"content": {"parts": [{"text": "GenerateContent Fallback OK"}]}}]}
            return {}

        with patch.dict("sys.modules", {"google": None, "google.genai": None}):
            with patch("sabrina_console_desktop.sabrina_desktop_ui._http_json", side_effect=_fake_http_json):
                res1 = _call_gemini_interactions("gemini-4-carbon", "hi", " 'AIzaKey' ", system_prompt="sys")
                self.assertEqual(res1, "Interactions OK")
                res2 = _call_gemini_interactions("gemma-3-27b", "hi", "AIzaKey")
                self.assertEqual(res2, "GenerateContent Fallback OK")
        self.assertIn("https://generativelanguage.googleapis.com/v1beta/interactions", calls)
        self.assertIn("https://generativelanguage.googleapis.com/v1beta/models/gemma-3-27b:generateContent", calls)

    def test_swarm_llm_probed_priority_auth_failfast_and_job_action_offline(self) -> None:
        from unittest.mock import patch

        def _mock_fetch_ga(url: str, headers: dict[str, str] | None = None, timeout: float = 6.0) -> Any:
            if "generativelanguage.googleapis.com" in url:
                return {"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/gemini-2.5-flash-lite"}]}
            if "openai.com" in url:
                return {"data": [{"id": "gpt-4.1-mini"}]}
            return {"data": []}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_ga)
            bridge = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
            bridge.registry.enable_remote = False
            try:
                # 1. job_action fails fast with clear offline message when backend :8087 is known down
                with bridge._remote_lock:
                    bridge._remote_cache["api_ok"] = False
                    bridge._remote_cache["last_refresh"] = 12345.0
                with patch("sabrina_console_desktop.sabrina_desktop_ui._http_json") as mock_http:
                    off_res = bridge.job_action("job-999", "run-now")
                self.assertFalse(off_res["ok"])
                self.assertIn("Backend :8087", off_res["message"])
                mock_http.assert_not_called()

                # 2. Unlock Gemini (probing gemini-2.5-flash) and OpenAI (probing gpt-4.1-mini).
                # Default binding has unprobed codename gemini-4-carbon; _call_swarm_llm must try
                # verified probed GA gemini-2.5-flash FIRST, and on API_KEY_INVALID skip all remaining
                # Gemini models and immediately fail over to OpenAI.
                bridge.set_vault_key("gemini", "AIzaBadKeyPlaceholder", probe=True)
                bridge.set_vault_key("openai", "sk-test-openai-valid", probe=True)
                attempted: list[tuple[str, str]] = []

                def _fake_invoke(
                    prov: str,
                    model_id: str,
                    system_prompt: str,
                    user_prompt: str,
                    api_key: str | None = None,
                ) -> dict[str, Any]:
                    attempted.append((prov, model_id))
                    if prov == "gemini":
                        return {
                            "ok": False,
                            "error": "HTTP 400: API key not valid. Please pass a valid API key.",
                            "provider": "gemini",
                            "model": model_id,
                        }
                    return {
                        "ok": True,
                        "text": "Failover OpenAI response",
                        "provider": prov,
                        "model": model_id,
                        "latency_ms": 11.0,
                    }

                with patch.object(bridge, "_invoke_provider_text", side_effect=_fake_invoke):
                    res = bridge._call_swarm_llm("planner_architect", "gemini-4-carbon", "sys", "goal")

                self.assertTrue(res["ok"])
                self.assertEqual(res["provider"], "openai")
                # First attempt used verified probed gemini-2.5-flash, NOT unprobed gemini-4-carbon
                self.assertEqual(attempted[0][0], "gemini")
                self.assertIn("gemini-2.5-flash", attempted[0][1])
                # Only ONE gemini call attempted before auth fail-fast switched to openai
                self.assertEqual(sum(1 for p, _ in attempted if p == "gemini"), 1)
                self.assertEqual(attempted[1][0], "openai")
            finally:
                bridge.swarm_runner.shutdown()

    def test_delete_vault_key_disconnects_env_provider_and_set_reenables(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaEnvTestKey1234567890"}):
                vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
                bridge = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
                bridge.registry.enable_remote = False
                try:
                    # Initially unlocked via env
                    st1 = bridge.get_vault_status()
                    self.assertIn("gemini", st1["unlocked_providers"])
                    gem1 = next(p for p in st1["providers"] if p["provider"] == "gemini")
                    self.assertEqual(gem1["source"], "env")

                    # Removing disconnects the env key cleanly so provider becomes LOCKED
                    del_res = bridge.delete_vault_key("gemini")
                    self.assertTrue(del_res["ok"])
                    self.assertNotIn("gemini", del_res["vault"]["unlocked_providers"])
                    gem2 = next(p for p in del_res["vault"]["providers"] if p["provider"] == "gemini")
                    self.assertEqual(gem2["source"], "none")
                    self.assertFalse(gem2["unlocked"])

                    # Saving a real vault key re-enables gemini via DPAPI vault
                    set_res = bridge.set_vault_key("gemini", "AIzaNewVaultKey9876543210", probe=True)
                    self.assertTrue(set_res["ok"])
                    self.assertIn("gemini", set_res["vault"]["unlocked_providers"])
                    gem3 = next(p for p in set_res["vault"]["providers"] if p["provider"] == "gemini")
                    self.assertEqual(gem3["source"], "vault")
                finally:
                    bridge.swarm_runner.shutdown()

    def test_harness_collective_checkpoints_diagnostics_and_chat_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
            )
            bridge.registry.enable_remote = False
            try:
                coll = bridge.get_harness_collective()
                self.assertTrue(coll["ok"])
                self.assertEqual(coll["active_harness_id"], "sabrina_native")
                self.assertEqual(
                    {h["harness_id"] for h in coll["harnesses"]},
                    {"sabrina_native", "mcp_harness", "external_cli", "mlx_ego_portal"},
                )

                # Bind a temporary workspace, create a file, checkpoint, edit, and rollback
                ws_dir = tmp_dir / "proj_ws"
                ws_dir.mkdir(parents=True)
                bridge._bind_workspace(ws_dir, mode="sandbox_auto")
                bridge.workspace_create_item("file", "calc.py", "def val() -> int:\n    return 10\n")

                cp_res = bridge.create_workspace_checkpoint("before_edit")
                self.assertTrue(cp_res["ok"])
                cp_id = cp_res["checkpoint"]["id"]

                assert bridge.workspace_sandbox is not None
                bridge.workspace_sandbox.execute_tool(
                    "write_file",
                    {"path": "calc.py", "content": "def val() -> int:\n    return 99\n", "overwrite": True},
                )
                self.assertIn("99", (ws_dir / "calc.py").read_text(encoding="utf-8"))

                diag_res = bridge.run_workspace_diagnostics("calc.py", ["syntax", "security"])
                self.assertTrue(diag_res["ok"])
                self.assertTrue(diag_res["passed"])

                rb_res = bridge.rollback_workspace_checkpoint(cp_id)
                self.assertTrue(rb_res["ok"])
                self.assertIn("10", (ws_dir / "calc.py").read_text(encoding="utf-8"))

                # Switch harness and run comparison turn; verify harness events populate swarm telemetry, NOT chat_history
                sw_res = bridge.set_active_harness("mlx_ego_portal")
                self.assertTrue(sw_res["ok"])
                self.assertEqual(sw_res["active_harness_id"], "mlx_ego_portal")

                cmp_res = bridge.compare_harnesses("Audit calc.py", harness_ids=["sabrina_native", "mcp_harness"])
                self.assertTrue(cmp_res["ok"])
                self.assertEqual(len(cmp_res["comparisons"]), 2)

                snap = bridge.get_snapshot()
                self.assertEqual(snap["chat_history"], [])
                self.assertGreaterEqual(len(snap["swarm"]["harness_events"]), 3)
                self.assertEqual(snap["swarm"]["active_harness_id"], "mlx_ego_portal")
            finally:
                bridge.swarm_runner.shutdown()

    def test_stigmergic_invariants_inv01_through_inv05(self) -> None:
        from sabrina_console_desktop.swarm_schema_audit import (
            audit_swarm_payload,
            evaluate_collective_invariants,
        )

        # 1. Clean 6-agent fan-out + multi-parent fan-in collective with SPINDOWN
        clean_collective = {
            "schema_version": "2.1.0-collective",
            "swarm_name": "Clean Collective",
            "execution_authorized": False,
            "topology": {
                "max_spawn_depth": 3,
                "max_concurrency": 12,
                "confidence_gate_threshold": -0.35,
                "blackboard_bus": "dnd_rsi_blackboard.sqlite3",
            },
            "deterministic_spawning_rules": [
                {"rule_id": "R1", "parent_role": "coordinator", "child_role": "planner_architect", "min_logprob": -0.35, "transition": "SPAWNING -> ACTIVE -> VALIDATING"},
                {"rule_id": "R2", "parent_role": "coordinator", "child_role": "rag_scout", "min_logprob": -0.35, "transition": "SPAWNING -> ACTIVE -> VALIDATING"},
                {"rule_id": "R3", "parent_role": "planner_architect", "child_role": "execution_specialist", "min_logprob": -0.35, "transition": "SPAWNING -> ACTIVE -> VALIDATING"},
                {"rule_id": "R4", "parent_role": "rag_scout", "child_role": "critic_verifier", "min_logprob": -0.35, "transition": "VALIDATING -> PASS -> SPINDOWN"},
                {"rule_id": "R5_STRICT", "parent_role": "execution_specialist", "child_role": "critic_verifier", "min_logprob": -0.25, "transition": "VALIDATING -> PASS -> SPINDOWN"},
            ],
            "agents": [
                {"id": "sabrina", "role": "coordinator", "model": "gemini-4-carbon", "provider": "gemini", "max_children": 3, "enabled": True, "status": "ACTIVE"},
                {"id": "codex-arch", "role": "planner_architect", "model": "openai/gpt-5", "provider": "openai", "max_children": 2, "enabled": True, "status": "ACTIVE"},
                {"id": "rag-scout", "role": "rag_scout", "model": "gemini-4-neon", "provider": "gemini", "max_children": 2, "enabled": True, "status": "ACTIVE"},
                {"id": "agy-exec", "role": "execution_specialist", "model": "gemini-4-argon", "provider": "gemini", "max_children": 2, "enabled": True, "status": "ACTIVE"},
                {"id": "claude-critic", "role": "critic_verifier", "model": "anthropic/claude-sonnet-4-5", "provider": "anthropic", "max_children": 1, "enabled": True, "status": "VALIDATING"},
            ],
        }
        res_all_unlocked = audit_swarm_payload(
            clean_collective,
            unlocked_providers={"gemini", "openai", "anthropic", "ollama"},
        )
        self.assertTrue(res_all_unlocked.valid)
        self.assertEqual(res_all_unlocked.errors, [])
        self.assertEqual(len(res_all_unlocked.invariants), 5)
        inv_map = {item["id"]: item for item in res_all_unlocked.invariants}
        for inv_id in ("INV-01", "INV-02", "INV-03", "INV-04", "INV-05"):
            self.assertEqual(inv_map[inv_id]["status"], "PASS", f"{inv_id} failed: {inv_map[inv_id]}")
        self.assertEqual(res_all_unlocked.counts["max_spawn_depth"], 3)
        self.assertEqual(res_all_unlocked.counts["max_concurrency"], 12)
        self.assertEqual(res_all_unlocked.counts["aggregate_max_children"], 10)
        # INV-04 highlights stricter rule R5_STRICT (-0.25 > -0.35)
        stricter_ids = [s["rule_id"] for s in inv_map["INV-04"]["stricter_rules"]]
        self.assertEqual(stricter_ids, ["R5_STRICT"])

        # INV-05 flags KEY_MISSING when cloud providers are locked
        res_locked = audit_swarm_payload(clean_collective, unlocked_providers={"ollama"})
        self.assertTrue(res_locked.valid)
        inv5_locked = next(i for i in res_locked.invariants if i["id"] == "INV-05")
        self.assertEqual(inv5_locked["status"], "WARN")
        self.assertEqual(inv5_locked["missing_key_count"], 5)
        self.assertEqual(set(inv5_locked["missing_providers"]), {"anthropic", "gemini", "openai"})

        # 2. Broken collective: missing root, orphan rule, cycle, fan-out > concurrency, positive logprob, missing bus
        broken = {
            "schema_version": "2.1.0-collective",
            "topology": {
                "max_spawn_depth": 1,
                "max_concurrency": 2,
                "max_total_agents": 2,
                "confidence_gate_threshold": 0.25,
                "blackboard_bus": "",
            },
            "deterministic_spawning_rules": [
                {"rule_id": "CYC1", "parent_role": "planner_architect", "child_role": "execution_specialist", "min_logprob": -0.3},
                {"rule_id": "CYC2", "parent_role": "execution_specialist", "child_role": "planner_architect", "min_logprob": 0.5},
                {"rule_id": "ORPH1", "parent_role": "planner_architect", "child_role": "ghost_role", "min_logprob": -0.3},
            ],
            "agents": [
                {"id": "arch-1", "role": "planner_architect", "model": "openai/gpt-5", "provider": "openai", "max_children": 4, "status": "ACTIVE"},
                {"id": "exec-1", "role": "execution_specialist", "model": "gemini-4-argon", "provider": "gemini", "max_children": 4, "status": "ACTIVE"},
                {"id": "iso-1", "role": "fast_worker", "model": "ollama/qwen2.5:7b", "provider": "ollama", "max_children": 1, "status": "ACTIVE"},
            ],
        }
        invs, errs, warns, counts = evaluate_collective_invariants(broken, unlocked_providers={"ollama"})
        b_map = {item["id"]: item for item in invs}
        self.assertEqual(b_map["INV-01"]["status"], "FAIL")
        self.assertTrue(any("Missing root coordinator" in e for e in errs))
        self.assertTrue(any("Orphan rule" in e and "ORPH1" in e for e in errs))
        self.assertTrue(any("Unreachable" in w and "iso-1" in w for w in warns))
        self.assertEqual(b_map["INV-02"]["status"], "FAIL")
        self.assertTrue(any("Role cycle detected" in e for e in errs))
        self.assertTrue(any("SPINDOWN" in w for w in warns))
        self.assertEqual(b_map["INV-03"]["status"], "WARN")
        self.assertEqual(counts["aggregate_max_children"], 9)
        self.assertEqual(counts["max_total_agents"], 2)
        self.assertTrue(any("exceed max_total_agents (2)" in w for w in warns))
        self.assertEqual(b_map["INV-04"]["status"], "FAIL")
        self.assertTrue(any("confidence_gate_threshold" in e for e in errs))
        self.assertTrue(any("CYC2" in e for e in errs))
        self.assertEqual(b_map["INV-05"]["status"], "WARN")
        self.assertTrue(any("blackboard_bus" in w for w in warns))

        # 3. Invalid max_total_agents (< 1) fails INV-03 deterministically
        bad_limit = {
            **clean_collective,
            "topology": {**clean_collective["topology"], "max_total_agents": 0},
        }
        invs_bad, errs_bad, _, _ = evaluate_collective_invariants(
            bad_limit, unlocked_providers={"gemini", "openai", "anthropic", "ollama"}
        )
        inv3_bad = next(i for i in invs_bad if i["id"] == "INV-03")
        self.assertEqual(inv3_bad["status"], "FAIL")
        self.assertTrue(any("Invalid max_total_agents" in e for e in errs_bad))

        # 4. Unbounded algorithmic zero-predef seed (1 root coordinator, all caps null/∞) passes ALL 5 invariants
        unbounded_seed = {
            "schema_version": "2.1.0-collective",
            "swarm_name": "Autonomous Unbounded Seed",
            "execution_authorized": False,
            "topology": {
                "spawn_mode": "algorithmic",
                "task_complexity": 3.6,
                "max_spawn_depth": None,
                "max_concurrency": None,
                "max_total_agents": None,
                "confidence_gate_threshold": -0.35,
                "blackboard_bus": "dnd_rsi_blackboard.sqlite3",
            },
            "deterministic_spawning_rules": [],
            "agents": [
                {"id": "sabrina", "role": "coordinator", "model": "ollama/qwen2.5-coder:14b", "provider": "ollama", "status": "ACTIVE", "max_children": None, "enabled": True},
            ],
        }
        res_unbounded = audit_swarm_payload(unbounded_seed, unlocked_providers={"ollama"})
        self.assertTrue(res_unbounded.valid)
        self.assertEqual(res_unbounded.errors, [])
        self.assertEqual(res_unbounded.warnings, [])
        u_map = {item["id"]: item for item in res_unbounded.invariants}
        for inv_id in ("INV-01", "INV-02", "INV-03", "INV-04", "INV-05"):
            self.assertEqual(u_map[inv_id]["status"], "PASS", f"Unbounded {inv_id}: {u_map[inv_id]}")
        self.assertIsNone(res_unbounded.counts["max_total_agents"])
        self.assertIsNone(res_unbounded.counts["max_spawn_depth"])
        self.assertIsNone(res_unbounded.counts["max_concurrency"])

        # 5. Boolean switch toggle verification: if not enabled, agents cap goes to infinity (∞)
        unlimited_multi_agents = {
            **clean_collective,
            "topology": {**clean_collective["topology"], "max_total_agents": None},
        }
        invs_unlim, _, warns_unlim, counts_unlim = evaluate_collective_invariants(
            unlimited_multi_agents, unlocked_providers={"gemini", "openai", "anthropic", "ollama"}
        )
        inv3_unlim = next(i for i in invs_unlim if i["id"] == "INV-03")
        self.assertEqual(inv3_unlim["status"], "PASS")
        self.assertIn("agents=∞", inv3_unlim["summary"])
        self.assertIsNone(counts_unlim["max_total_agents"])
        self.assertFalse(any("exceed max_total_agents" in w for w in warns_unlim))

        # When agent limit switch is ON with limit = 3 and 5 enabled agents, cap is enforced
        limited_multi_agents = {
            **clean_collective,
            "topology": {**clean_collective["topology"], "max_total_agents": 3},
        }
        invs_lim, _, warns_lim, counts_lim = evaluate_collective_invariants(
            limited_multi_agents, unlocked_providers={"gemini", "openai", "anthropic", "ollama"}
        )
        inv3_lim = next(i for i in invs_lim if i["id"] == "INV-03")
        self.assertEqual(inv3_lim["status"], "WARN")
        self.assertIn("agents≤3", inv3_lim["summary"])
        self.assertEqual(counts_lim["max_total_agents"], 3)
        self.assertTrue(any("exceed max_total_agents (3)" in w for w in warns_lim))

    def test_swarm_markdown_json_roundtrip_and_bridge_save_audit(self) -> None:
        from sabrina_console_desktop.swarm_schema_audit import (
            audit_swarm_file,
            parse_swarm_markdown,
            serialize_swarm_markdown,
        )

        original = {
            "schema_version": "2.1.0-collective",
            "swarm_name": "RoundTrip Fan-In Collective",
            "execution_authorized": False,
            "topology": {
                "max_spawn_depth": 3,
                "max_concurrency": 6,
                "max_total_agents": 18,
                "confidence_gate_threshold": -0.35,
                "confidence_gate_probability": 0.705,
                "blackboard_bus": "custom_blackboard.sqlite3",
            },
            "deterministic_spawning_rules": [
                {
                    "rule_id": "R1_PLAN",
                    "parent_role": "coordinator",
                    "child_role": "planner_architect",
                    "min_logprob": -0.30,
                    "trigger": "mission_dispatch && depth < 3",
                    "transition": "SPAWNING -> ACTIVE -> VALIDATING",
                },
                {
                    "rule_id": "R2_VERIFY",
                    "parent_role": "planner_architect",
                    "child_role": "critic_verifier",
                    "min_logprob": -0.25,
                    "trigger": "plan_ready",
                    "transition": "VALIDATING -> PASS -> SPINDOWN",
                },
            ],
            "agents": [
                {
                    "id": "sabrina",
                    "role": "coordinator",
                    "model": "openai/gpt-5",
                    "provider": "openai",
                    "max_children": 3,
                    "enabled": True,
                    "status": "ACTIVE",
                    "task": "Coordinate swarm",
                    "prompt": "Coordinate bounded decomposition safely.",
                },
                {
                    "id": "arch",
                    "role": "planner_architect",
                    "model": "ollama/qwen2.5-coder:14b",
                    "provider": "ollama",
                    "max_children": 2,
                    "enabled": True,
                    "status": "ACTIVE",
                    "task": "Plan DAG tasks",
                },
                {
                    "id": "critic",
                    "role": "critic_verifier",
                    "model": "ollama/qwen2.5:7b",
                    "provider": "ollama",
                    "max_children": 1,
                    "enabled": True,
                    "status": "VALIDATING",
                    "task": "Verify invariants",
                    "prompt": "Block if any invariant fails.",
                },
                {
                    "id": "disabled-extra",
                    "role": "fast_worker",
                    "model": "anthropic/claude-sonnet-4-5",
                    "provider": "anthropic",
                    "max_children": 1,
                    "enabled": False,
                    "status": "STANDBY",
                    "task": "Disabled reserve worker",
                },
            ],
        }

        md_text = serialize_swarm_markdown(original)
        self.assertIn("## Agents", md_text)
        self.assertIn("## Spawning Rules", md_text)
        self.assertIn("## Prompts", md_text)
        self.assertIn("custom_blackboard.sqlite3", md_text)
        self.assertIn("VALIDATING -> PASS -> SPINDOWN", md_text)

        parsed = parse_swarm_markdown(md_text)
        self.assertEqual(parsed["schema_version"], "2.1.0-collective")
        self.assertEqual(parsed["swarm_name"], "RoundTrip Fan-In Collective")
        self.assertFalse(parsed["execution_authorized"])
        self.assertEqual(parsed["topology"]["max_spawn_depth"], 3)
        self.assertEqual(parsed["topology"]["max_concurrency"], 6)
        self.assertEqual(parsed["topology"]["max_total_agents"], 18)
        self.assertAlmostEqual(parsed["topology"]["confidence_gate_threshold"], -0.35)
        self.assertEqual(parsed["topology"]["blackboard_bus"], "custom_blackboard.sqlite3")
        self.assertEqual(len(parsed["agents"]), 4)
        self.assertEqual(parsed["agents"][0]["prompt"], "Coordinate bounded decomposition safely.")
        self.assertEqual(parsed["agents"][2]["prompt"], "Block if any invariant fails.")
        self.assertFalse(parsed["agents"][3]["enabled"])
        self.assertEqual(len(parsed["deterministic_spawning_rules"]), 2)
        self.assertEqual(parsed["deterministic_spawning_rules"][1]["transition"], "VALIDATING -> PASS -> SPINDOWN")
        self.assertAlmostEqual(parsed["deterministic_spawning_rules"][1]["min_logprob"], -0.25)

        # Also test bullet-list spawning rules fallback in Markdown
        bullet_md = (
            "---\nschema_version: 2.1.0-collective\nswarm_name: Bullet Test\nmax_spawn_depth: 2\n---\n"
            "## Agents\n| id | role | model |\n| --- | --- | --- |\n"
            "| sabrina | coordinator | ollama/qwen2.5:7b |\n"
            "| critic | critic_verifier | ollama/qwen2.5:7b |\n\n"
            "## Spawning Rules\n- R1_B: coordinator -> critic_verifier (min_logprob: -0.20, transition: VALIDATING -> PASS -> SPINDOWN)\n"
        )
        bullet_parsed = parse_swarm_markdown(bullet_md)
        self.assertEqual(len(bullet_parsed["deterministic_spawning_rules"]), 1)
        self.assertEqual(bullet_parsed["deterministic_spawning_rules"][0]["rule_id"], "R1_B")
        self.assertAlmostEqual(bullet_parsed["deterministic_spawning_rules"][0]["min_logprob"], -0.20)
        self.assertIn("SPINDOWN", bullet_parsed["deterministic_spawning_rules"][0]["transition"])

        # Also test unbounded algorithmic Markdown round-trip (unlimited caps -> None)
        unbounded_md = serialize_swarm_markdown(
            {
                "schema_version": "2.1.0-collective",
                "swarm_name": "Unbounded Algo Seed",
                "execution_authorized": False,
                "topology": {
                    "spawn_mode": "algorithmic",
                    "task_complexity": 4.8,
                    "max_spawn_depth": None,
                    "max_concurrency": None,
                    "max_total_agents": None,
                    "confidence_gate_threshold": -0.35,
                    "blackboard_bus": "dnd_rsi_blackboard.sqlite3",
                },
                "agents": [{"id": "sabrina", "role": "coordinator", "model": "ollama/qwen2.5:7b", "max_children": None}],
                "deterministic_spawning_rules": [],
            }
        )
        self.assertIn("max_total_agents: unlimited", unbounded_md)
        self.assertIn("spawn_mode: \"algorithmic\"", unbounded_md)
        unbounded_parsed = parse_swarm_markdown(unbounded_md)
        self.assertEqual(unbounded_parsed["topology"]["spawn_mode"], "algorithmic")
        self.assertAlmostEqual(unbounded_parsed["topology"]["task_complexity"], 4.8)
        self.assertIsNone(unbounded_parsed["topology"]["max_total_agents"])
        self.assertIsNone(unbounded_parsed["topology"]["max_spawn_depth"])
        self.assertIsNone(unbounded_parsed["topology"]["max_concurrency"])
        self.assertIsNone(unbounded_parsed["agents"][0]["max_children"])

        # Test audit_swarm_file(.md) and SabrinaDesktopBridge save/audit with Markdown
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            md_file = tmp_dir / "collective.md"
            md_file.write_text(md_text, encoding="utf-8")
            file_audit = audit_swarm_file(md_file, unlocked_providers={"openai", "ollama"})
            self.assertTrue(file_audit.valid)
            self.assertEqual(len(file_audit.invariants), 5)

            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
            bridge.registry.enable_remote = False
            try:
                bridge.set_vault_key("openai", "sk-test-openai-md", probe=True)
                save_res = bridge.save_swarm_schema(md_text, sync_role_bindings=True)
                self.assertTrue(save_res["ok"])
                self.assertEqual(save_res["format"], "markdown")
                self.assertFalse(save_res["execution_authorized"])
                self.assertEqual(len(save_res["invariants"]), 5)
                # Disabled agent role 'fast_worker' must not be synced
                self.assertNotIn("fast_worker", save_res["updated_roles"])
                self.assertIn("coordinator", save_res["updated_roles"])

                audit_res = bridge.audit_swarm_schema(target="schema", raw_schema=md_text)
                self.assertTrue(audit_res["ok"])
                self.assertTrue(audit_res["valid"])
                inv5 = next(i for i in audit_res["invariants"] if i["id"] == "INV-05")
                self.assertEqual(inv5["status"], "PASS")
            finally:
                bridge.swarm_runner.shutdown()

    def test_workspace_directory_setting_persistence_and_runner_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            ws_dir = tmp_dir / "my_target_project"
            ws_dir.mkdir(parents=True)
            (ws_dir / "main.py").write_text("print('ok')\n", encoding="utf-8")

            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
            bridge.registry.enable_remote = False
            try:
                # 1. Rejects non-existent path and protected system directory cleanly
                bad_missing = bridge.set_workspace_directory(str(tmp_dir / "does_not_exist"))
                self.assertFalse(bad_missing["ok"])
                self.assertIn("does not exist", bad_missing["error"])
                bad_sys = bridge.set_workspace_directory(r"C:\Windows")
                self.assertFalse(bad_sys["ok"])
                self.assertIn("protected system directory", bad_sys["error"])

                # 2. Accepts quoted path, binds WorkspaceSandbox + syncs LiveSwarmRunner.workspace_root + persists state
                set_res = bridge.set_workspace_directory(f'"{ws_dir}"', mode="approve_writes")
                self.assertTrue(set_res["ok"])
                self.assertTrue(set_res["active"])
                self.assertEqual(Path(set_res["root"]).resolve(), ws_dir.resolve())
                self.assertEqual(bridge.swarm_runner.workspace_root.resolve(), ws_dir.resolve())
                self.assertEqual(Path(set_res["swarm_workspace_root"]).resolve(), ws_dir.resolve())
                self.assertIn(str(ws_dir.resolve()), set_res["recent_workspaces"])

                snap = bridge.get_snapshot()
                self.assertTrue(snap["workspace"]["active"])
                self.assertEqual(snap["workspace"]["name"], "my_target_project")
                self.assertEqual(Path(snap["workspace"]["swarm_workspace_root"]).resolve(), ws_dir.resolve())
            finally:
                bridge.swarm_runner.shutdown()

            # 3. Fresh bridge automatically restores workspace_state.json across restart
            bridge2 = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
            bridge2.registry.enable_remote = False
            try:
                st2 = bridge2.get_workspace_status()
                self.assertTrue(st2["active"])
                self.assertEqual(Path(st2["root"]).resolve(), ws_dir.resolve())
                self.assertEqual(bridge2.swarm_runner.workspace_root.resolve(), ws_dir.resolve())

                # 4. clear_workspace detaches sandbox & retains recent_workspaces
                clr = bridge2.clear_workspace()
                self.assertTrue(clr["ok"])
                self.assertFalse(clr["active"])
                self.assertIn(str(ws_dir.resolve()), clr["recent_workspaces"])
            finally:
                bridge2.swarm_runner.shutdown()

    def test_async_chat_dispatch_and_cancellation(self) -> None:
        import threading
        import time as _time
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge = SabrinaDesktopBridge(role_bindings_path=tmp_dir / "role_bindings.json", vault=vault)
            bridge.registry.enable_remote = False
            started_ev = threading.Event()
            release_ev = threading.Event()

            def _slow_turn(
                clean: str,
                mode: str,
                target: str,
                *,
                progress: Any = None,
                cancel_event: threading.Event | None = None,
            ) -> dict[str, Any]:
                if progress:
                    progress("Querying llama3.1:8b (1/3)…")
                started_ev.set()
                while not release_ev.is_set() and (cancel_event is None or not cancel_event.is_set()):
                    _time.sleep(0.02)
                if cancel_event is not None and cancel_event.is_set():
                    return {"ok": False, "cancelled": True, "error": "Chat cancelled by operator."}
                return {"ok": True, "reply": "Done"}

            try:
                with patch.object(bridge, "_execute_chat_turn", side_effect=_slow_turn):
                    r1 = bridge.send_chat_async("Run collective plan review", "hive_council", "hive_council")
                    self.assertTrue(r1["ok"])
                    self.assertTrue(r1["async"])
                    self.assertTrue(started_ev.wait(timeout=3.0))

                    # Duplicate send while running returns busy immediately instead of stacking
                    r_dup = bridge.send_chat_async("Second prompt", "hive_council", "hive_council")
                    self.assertFalse(r_dup["ok"])
                    self.assertTrue(r_dup["busy"])

                    job_st = bridge.get_snapshot()["chat_job"]
                    self.assertTrue(job_st["running"])
                    self.assertIn("llama3.1:8b", job_st["stage"])

                    # Cancel immediately unlocks UI and signals cancel_event
                    c_res = bridge.cancel_chat_job()
                    self.assertTrue(c_res["ok"])
                    self.assertTrue(c_res["cancelled"])
                    self.assertFalse(bridge.get_chat_job_status()["running"])
            finally:
                release_ev.set()
                bridge.swarm_runner.shutdown()

    def test_synced_desktop_html_copies_and_collective_web_markers(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        desktop_html = (repo_root / "sabrina_console_desktop" / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        root_html = (repo_root / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        self.assertEqual(desktop_html, root_html, "Root sabrina_desktop_ui.html must stay byte-for-byte synced")

        declared_ids = set(re.findall(r'\bid="([^"]+)"', desktop_html))
        for required_id in (
            "btn-top-workspace",
            "ws-path-input",
            "btn-ws-set-path",
            "ws-recent-select",
            "btn-ws-detach",
            "ws-feedback",
            "btn-cancel-chat",
            "schema-audit-banner",
            "schema-preset-select",
            "builder-spawn-mode",
            "builder-complexity",
            "btn-builder-unbound-all",
            "builder-agent-limit-toggle",
            "builder-max-agents",
            "schema-stat-max-agents",
            "builder-blackboard-bus",
            "builder-rule-transition",
            "schema-stat-fanout",
            "topo-web-legend",
            "topo-web-metrics",
            "api-8095-dot",
            "api-8095-status",
            "nav-pending",
            "nav-models",
            "nav-jobs",
            "nav-debug",
            "top-tunnel-badge",
            "ui-live-badge",
            "ui-theme-select",
            "btn-cycle-ui-theme",
            "btn-debug-pill",
            "blackboard-chat-panel",
            "blackboard-chat-stream",
            "bb-chat-filter-agent",
            "bb-chat-filter-stage",
            "bb-chat-autoscroll",
            "bb-chat-count-badge",
        ):
            self.assertIn(required_id, declared_ids)

        for marker in (
            "function buildCollectiveWeb(",
            "function buildLiveRunWeb(",
            "function evaluateCollectiveInvariants(",
            "function renderSchemaAuditBanner(",
            "function renderBlackboardChat(",
            "__builderEditAgentModel",
            "__builderToggleRuleSpindown",
            "AGENT_CAP",
            "max_total_agents",
            "toggleAgentLimitSwitch",
            "switch-toggle",
            "INV-01",
            "INV-02",
            "INV-03",
            "INV-04",
            "INV-05",
            "STRICTER GATE",
            "animateMotion",
            "node-reading-beacon",
            "blackboard-motion-pkt",
            "READING BLACKBOARD",
            "execution_authorized: false",
            "queen_bee",
            "applyUiTheme",
        ):
            self.assertIn(marker, desktop_html)

        # Swarm Monitor (#view-swarm) must stay strictly real-runs-only (zero simulation controls/copy)
        swarm_view_match = re.search(r'<section id="view-swarm"[^>]*>(.*?)</section>', desktop_html, re.S)
        self.assertIsNotNone(swarm_view_match)
        assert swarm_view_match is not None
        swarm_view_html = swarm_view_match.group(1)
        for forbidden_sim in (
            "btn-sim-spawn",
            "sim-vault-mode",
            "quick-spawn-mode",
            "quick-complexity",
            "simulate the web",
            "▶ Simulate Swarm Spawn",
        ):
            self.assertNotIn(forbidden_sim, swarm_view_html)

    def test_hive_ui_theme_and_swarm_display_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = SabrinaDesktopBridge(
                role_bindings_path=root / "role_bindings.json",
                vault_path=root / "provider_vault.json",
                custom_models_path=root / "custom_models.json",
                swarm_artifacts_dir=root / "artifacts",
                coordinator_only=True,
            )
            try:
                initial = bridge.get_ui_theme()
                self.assertTrue(initial["ok"])
                self.assertEqual(initial["ui_theme"], "obsidian")
                self.assertIn("hive", initial["themes"])

                res_ui = bridge.save_ui_theme("hive")
                self.assertTrue(res_ui["ok"])
                self.assertEqual(bridge.get_ui_theme()["ui_theme"], "hive")

                res_graph = bridge.save_swarm_display("hive", "on", "nectar", "fast", "honeycomb", "hive")
                self.assertTrue(res_graph["ok"])
                prefs = bridge.get_swarm_display()
                self.assertEqual(prefs["theme"], "hive")
                self.assertEqual(prefs["backdrop"], "honeycomb")
                self.assertEqual(prefs["link_style"], "nectar")
                self.assertEqual(prefs["ui_theme"], "hive")

                bad_ui = bridge.save_ui_theme("neon_pink")
                self.assertFalse(bad_ui["ok"])
            finally:
                bridge.swarm_runner.shutdown()

    def test_operator_chat_log_persists_and_clears_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            bindings_path = tmp_dir / "role_bindings.json"
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge1 = SabrinaDesktopBridge(
                role_bindings_path=bindings_path,
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            bridge1._append_chat("operator", "Resume my last session")
            bridge1._append_chat("gemini-2.5-pro", "Session restored cleanly.")
            self.assertTrue((tmp_dir / "operator_chat_log.json").is_file())

            # Simulate restarting SabrinaDesktopBridge against the same control directory
            bridge2 = SabrinaDesktopBridge(
                role_bindings_path=bindings_path,
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            self.assertEqual(len(bridge2.chat_log), 2)
            self.assertEqual(bridge2.chat_log[0]["sender"], "operator")
            self.assertEqual(bridge2.chat_log[0]["text"], "Resume my last session")
            self.assertEqual(bridge2.chat_log[1]["text"], "Session restored cleanly.")

            # Clearing chat clears persisted file as well
            bridge2.clear_chat()
            bridge3 = SabrinaDesktopBridge(
                role_bindings_path=bindings_path,
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            self.assertEqual(bridge3.chat_log, [])

    def test_rate_limit_429_fails_over_to_next_provider_with_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json", fetch_json=_mock_fetch_json)
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
            )
            bridge.registry.enable_remote = False
            bridge.set_vault_key("gemini", "AIza-test-gemini-key", probe=False)
            bridge.set_vault_key("openai", "sk-test-openai-key", probe=False)

            calls: list[tuple[str, str]] = []

            def fake_invoke(prov: str, model_id: str, sys_p: str, user_p: str, api_key: str | None = None):
                calls.append((prov, model_id))
                if prov == "gemini":
                    return {"ok": False, "error": "HTTP 429 Too Many Requests (RESOURCE_EXHAUSTED)", "provider": "gemini", "model": model_id}
                return {"ok": True, "text": "OpenAI failover succeeded", "provider": prov, "model": model_id, "latency_ms": 12.5}

            bridge._invoke_provider_text = fake_invoke  # type: ignore[method-assign]

            # First call hits gemini 429 and immediately fails over to openai (without burning 3 attempts on gemini!)
            r1 = bridge._call_swarm_llm("planner_architect", "gemini-2.5-flash", "sys", "hello")
            self.assertTrue(r1["ok"])
            self.assertEqual(r1["provider"], "openai")
            self.assertEqual([p for p, _ in calls], ["gemini", "openai"])

            # Second call immediately skips cooled-down gemini and goes straight to openai
            calls.clear()
            r2 = bridge._call_swarm_llm("research_scout", "gemini-2.5-flash", "sys", "hello again")
            self.assertTrue(r2["ok"])
            self.assertEqual(r2["provider"], "openai")
            self.assertEqual([p for p, _ in calls], ["openai"])

    def test_free_tier_zero_quota_falls_back_to_sibling_gemini_without_cooling_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_d:
            tmp_dir = Path(tmp_d)
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json")
            vault.set_key("gemini", "AIzaTestKeyValid")
            vault._probed_models = [
                {"id": "gemini-4-carbon", "name": "Gemini 4 Carbon", "provider": "gemini"},
                {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "provider": "gemini"},
            ]
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
            )
            calls: list[tuple[str, str]] = []

            def fake_invoke(prov: str, model_id: str, sys_p: str, user_p: str, api_key: str | None = None):
                calls.append((prov, model_id))
                if model_id == "gemini-4-carbon" or model_id == "gemini-4-hydrogen":
                    return {
                        "ok": False,
                        "error": f"Error code: 429 - Rate limit exceeded for model {model_id} (limit: 0 input tokens per minute on Free Tier). Please upgrade your tier.",
                        "provider": "gemini",
                        "model": model_id,
                    }
                return {
                    "ok": True,
                    "text": f"Success with {model_id}",
                    "provider": prov,
                    "model": model_id,
                    "latency_ms": 10.0,
                }

            bridge._invoke_provider_text = fake_invoke  # type: ignore[method-assign]

            # 1. First call: gemini-4-carbon hits limit: 0 on Free Tier, but Gemini is NOT cooled down,
            # so it seamlessly tries gemini-2.5-flash and succeeds!
            r1 = bridge._call_swarm_llm("planner_architect", "gemini-4-carbon", "sys", "hello")
            self.assertTrue(r1["ok"])
            self.assertEqual(r1["model"], "gemini-2.5-flash")
            self.assertEqual(r1["provider"], "gemini")
            self.assertTrue(r1.get("used_fallback"))
            self.assertIn("limit: 0", r1.get("primary_error", ""))
            self.assertEqual(calls, [("gemini", "gemini-4-carbon"), ("gemini", "gemini-2.5-flash")])

            # 2. Second call: all gemini-4-* are remembered as zero-quota on Free Tier,
            # so it goes straight to gemini-2.5-flash!
            calls.clear()
            r2 = bridge._call_swarm_llm("research_scout", "gemini-4-hydrogen", "sys", "hello again")
            self.assertTrue(r2["ok"])
            self.assertEqual(r2["model"], "gemini-2.5-flash")
            self.assertTrue(r2.get("used_fallback"))
            self.assertIn("gemini-4-hydrogen", r2.get("primary_error", ""))
            self.assertEqual(calls, [("gemini", "gemini-2.5-flash")])

    def test_ollama_crash_and_prefix_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_d:
            tmp_dir = Path(tmp_d)
            vault = ProviderKeyVault(vault_path=tmp_dir / "provider_vault.json")
            vault.set_key("gemini", "AIzaTestKeyValid")
            vault._probed_models = [
                {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "provider": "gemini"},
            ]
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_dir / "role_bindings.json",
                vault=vault,
                custom_models_path=tmp_dir / "custom_models.json",
                spindown_delay_sec=0.1,
                step_delay_sec=0.01,
            )
            # Simulate Ollama having a broken model with existing prefix
            bridge._remote_cache["ollama_models"] = [
                {"name": "ollama/mahade315/mamba-codestral:7b-q4_K_M"},
            ]
            calls: list[tuple[str, str]] = []

            def fake_invoke(prov: str, model_id: str, sys_p: str, user_p: str, api_key: str | None = None):
                calls.append((prov, model_id))
                if prov == "ollama":
                    return {
                        "ok": False,
                        "error": 'HTTP 500: {"error":"llama-server process has terminated: exit status 1: error loading model: check_tensor_dims"}',
                        "provider": "ollama",
                        "model": model_id,
                    }
                if model_id == "gemini-4-hydrogen":
                    return {
                        "ok": False,
                        "error": "Error code: 429 - Rate limit exceeded for model gemini-4-hydrogen (limit: 0 input tokens per minute on Free Tier).",
                        "provider": "gemini",
                        "model": model_id,
                    }
                return {
                    "ok": True,
                    "text": "Fallback gemini success",
                    "provider": prov,
                    "model": model_id,
                    "latency_ms": 15.0,
                }

            bridge._invoke_provider_text = fake_invoke  # type: ignore[method-assign]

            # Invocations should not have double ollama/ prefix and should fall back cleanly to gemini
            res = bridge._call_swarm_llm("research_scout", "ollama/mahade315/mamba-codestral:7b-q4_K_M", "sys", "scan")
            self.assertTrue(res["ok"])
            self.assertEqual(res["provider"], "gemini")
            self.assertTrue(res.get("used_fallback"))
            self.assertTrue("check_tensor_dims" in res.get("primary_error", "") or any("check_tensor_dims" in str(e) for e in res.get("fallback_errors", [])))
            self.assertFalse(any(m.startswith("ollama/ollama/") for _, m in calls))
            self.assertIn(("ollama", "ollama/mahade315/mamba-codestral:7b-q4_K_M"), calls)
            self.assertEqual(calls[-1], ("gemini", "gemini-2.5-flash"))


if __name__ == "__main__":
    unittest.main()
