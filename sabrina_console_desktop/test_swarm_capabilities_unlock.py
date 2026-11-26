"""Unit and integration tests for the 6-Tier Sabrina Swarm Capabilities Unlock."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from sabrina_console_desktop.context_first_coordinator import (
    BlueprintValidationError,
    build_preflight_manifest,
    parse_swarm_blueprint,
)
from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.sabrina_desktop_ui import (
    SabrinaDesktopBridge,
    _extract_gemini_interactions_media,
    _wrap_pcm16_as_wav,
)
from swarm_console.workspace_sandbox import WorkspaceSandbox

# Valid 1x1 transparent PNG bytes
VALID_1X1_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _UnlockFakeVault:
    def __init__(self, unlocked: list[str], keys: dict[str, str] | None = None, probed: list[dict[str, str]] | None = None) -> None:
        self._unlocked = list(unlocked)
        self._keys = dict(keys or {p: f"key-{p}" for p in unlocked})
        self._probed = list(probed or [])

    def list_providers(self) -> dict[str, object]:
        return {"unlocked_providers": list(self._unlocked)}

    def is_provider_unlocked(self, provider: str) -> bool:
        return provider in self._unlocked

    def get_key(self, provider: str, include_env: bool = True) -> str | None:
        return self._keys.get(provider)

    def get_probed_models(self) -> list[dict[str, str]]:
        return list(self._probed)


class TestSwarmCapabilitiesUnlock(unittest.TestCase):
    # ------------------------------------------------------------------
    # Tier 1: Multimodal Capability Manifest, Blueprint & Media Helpers
    # ------------------------------------------------------------------

    def test_tier1_manifest_exposes_media_models_and_validates_coordinator_media_bindings(self) -> None:
        vault = _UnlockFakeVault(unlocked=["gemini"])
        manifest = build_preflight_manifest(
            vault=vault,
            ollama_models=[{"name": "gemma3:12b"}],
            ollama_ready=True,
            allow_workspace_writes=True,
            allow_web_grounding=True,
        )
        self.assertIn("generate_image", manifest.available_tool_shed)
        self.assertIn("generate_audio", manifest.available_tool_shed)
        self.assertIn("web_search", manifest.available_tool_shed)
        self.assertIn("gemini/gemini-nano-banana-2.1", manifest.healthy_media_models.get("image", ()))
        self.assertIn("gemini/gemini-3.8-flash-lite-tts", manifest.healthy_media_models.get("tts", ()))
        self.assertIn("ollama/gemma3:12b", manifest.healthy_media_models.get("vision", ()))

        bp = parse_swarm_blueprint(
            {
                "playbook": "direct_single_agent",
                "role_bindings": {
                    "execution_specialist": "gemini/gemini-3.5-flash-lite",
                    "critic_verifier": "ollama/gemma3:12b",
                },
                "media_bindings": {
                    "image": "gemini-nano-banana-2.1",
                    "audio": "gemini-3.8-flash-lite-tts",
                    "vision": "ollama/gemma3:12b",
                },
                "stop_condition": "Webpage and generated hero image verified on disk.",
            },
            manifest,
        )
        self.assertEqual(bp.media_bindings["image"], "gemini/gemini-nano-banana-2.1")
        self.assertEqual(bp.media_bindings["tts"], "gemini/gemini-3.8-flash-lite-tts")
        self.assertEqual(bp.media_bindings["vision"], "ollama/gemma3:12b")

        with self.assertRaisesRegex(BlueprintValidationError, "not in healthy_media_models"):
            parse_swarm_blueprint(
                {
                    "playbook": "direct_single_agent",
                    "role_bindings": {
                        "execution_specialist": "gemini/gemini-3.5-flash-lite",
                        "critic_verifier": "ollama/gemma3:12b",
                    },
                    "media_bindings": {"image": "openai/dall-e-dead"},
                    "stop_condition": "Done",
                },
                manifest,
            )

    def test_tier1_extract_interactions_media_and_wav_pcm_wrapper(self) -> None:
        fake_resp = {
            "output_image": {
                "data": base64.b64encode(VALID_1X1_PNG).decode("ascii"),
                "mime_type": "image/png",
            }
        }
        img_bytes, mime = _extract_gemini_interactions_media(fake_resp, "image")
        self.assertEqual(img_bytes, VALID_1X1_PNG)
        self.assertEqual(mime, "image/png")

        pcm_sample = b"\x00\x01" * 400
        wav_bytes = _wrap_pcm16_as_wav(pcm_sample, sample_rate=24000)
        self.assertTrue(wav_bytes.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav_bytes[:16])

    # ------------------------------------------------------------------
    # Tier 2: ACI Path Normalization, Binary Write Guard & Greenfield Builders
    # ------------------------------------------------------------------

    def test_tier2_apply_workspace_writes_normalizes_abs_paths_and_blocks_binary_text_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "site_out"
            ws.mkdir(parents=True)
            runner = LiveSwarmRunner(workspace_root=ws, artifacts_dir=Path(tmp) / "reports", auto_spindown=False)
            try:
                abs_html = str((ws / "index.html").resolve())
                outside_path = str((Path(tmp) / "escaped.html").resolve())
                payload = (
                    f"```write_file:{abs_html}\n<!doctype html><html><body>Hello</body></html>\n```\n"
                    f"```write_file:{outside_path}\nhacked\n```\n"
                    "```write_file:images/placeholder_hero.jpg\n\n```\n"
                    "```write_file:images/fake.png\nnot_binary_png_bytes\n```\n"
                )
                rejected: list[str] = []
                written = runner._apply_workspace_writes(payload, rejected_out=rejected)

                self.assertEqual(written, ["index.html"])
                self.assertTrue((ws / "index.html").is_file())
                self.assertFalse((Path(tmp) / "escaped.html").exists())
                self.assertFalse((ws / "images" / "placeholder_hero.jpg").exists())
                self.assertFalse((ws / "images" / "fake.png").exists())
                self.assertTrue(any("placeholder_hero.jpg" in r and "binary media extension" in r for r in rejected))
                self.assertTrue(any("escaped.html" in r and "escapes workspace_root" in r for r in rejected))
            finally:
                runner.shutdown()

    def test_tier1_and_tier2_greenfield_parallel_subagents_and_generate_image_with_vision_critic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "empty_business_site"
            ws.mkdir(parents=True)
            critic_received_attachments: list[Path] = []
            media_calls: list[dict[str, Any]] = []

            def fake_media_caller(
                kind: str,
                prompt: str,
                output_path: Path | str,
                *,
                model: str = "",
                aspect_ratio: str = "16:9",
                voice: str = "Puck",
                workspace_root: Path | None = None,
            ) -> dict[str, Any]:
                media_calls.append({"kind": kind, "model": model, "path": str(output_path), "prompt": prompt})
                dest = (Path(workspace_root or ws) / str(output_path)).resolve()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(VALID_1X1_PNG)
                return {
                    "ok": True,
                    "path": dest.relative_to((workspace_root or ws).resolve()).as_posix(),
                    "bytes": len(VALID_1X1_PNG),
                    "mime_type": "image/png",
                    "model": model or "gemini/gemini-nano-banana-2.1",
                    "provider": "gemini",
                }

            exec_round = 0

            def fake_llm(
                role: str,
                model_id: str,
                system_prompt: str,
                user_prompt: str,
                *,
                attachments: list[Path] | None = None,
            ) -> dict[str, Any]:
                nonlocal exec_round
                if role == "planner_architect":
                    return {
                        "ok": True,
                        "text": (
                            "MISSION_MODE: build\n"
                            "IMAGE_MODEL: gemini/gemini-nano-banana-2.1\n"
                            "1. Author CSS stylesheet\n"
                            "2. Generate hero image\n"
                            "```swarm_roles\n"
                            "[\n"
                            '  {"role": "execution_specialist", "title": "Author CSS", "allow_writes": true, "target_files": ["css/style.css"]},\n'
                            '  {"role": "execution_specialist", "title": "Create Hero Asset", "allow_writes": true, "target_files": ["images/hero.png"]}\n'
                            "]\n"
                            "```"
                        ),
                        "model": model_id,
                        "provider": "gemini",
                    }
                if role == "coordinator":
                    return {
                        "ok": True,
                        "text": json.dumps({
                            "algorithm": "hierarchical_shard",
                            "theta": 0.55,
                            "communication_protocol": "blackboard_handoff",
                            "workstreams": [
                                {"key": "subagent_1", "title": "Author CSS", "role": "execution_specialist", "allow_writes": True, "target_files": ["css/style.css"]},
                                {"key": "subagent_2", "title": "Create Hero Asset", "role": "execution_specialist", "allow_writes": True, "target_files": ["images/hero.png"]},
                            ],
                        }),
                        "model": model_id,
                        "provider": "gemini",
                    }
                if role == "research_scout":
                    return {"ok": True, "text": "Workspace is empty (0 files); ready for greenfield build.", "model": model_id, "provider": "gemini"}
                if "Assigned Workstream:\nAuthor CSS" in user_prompt:
                    return {
                        "ok": True,
                        "text": "Created stylesheet.\n```write_file:css/style.css\nbody { font-family: sans-serif; color: #111; }\n```",
                        "model": model_id,
                        "provider": "gemini",
                    }
                if "Assigned Workstream:\nCreate Hero Asset" in user_prompt:
                    if "Tool Output:" not in user_prompt:
                        return {
                            "ok": True,
                            "text": '```tool:generate_image\n{"path": "images/hero.png", "prompt": "Artisan bakery storefront at sunrise"}\n```',
                            "model": model_id,
                            "provider": "gemini",
                        }
                    return {"ok": True, "text": "Generated images/hero.png via Gemini image tool.", "model": model_id, "provider": "gemini"}
                if role == "execution_specialist":
                    exec_round += 1
                    return {
                        "ok": True,
                        "text": (
                            "Integrated HTML landing page with CSS and generated hero image.\n"
                            "```write_file:index.html\n"
                            "<!doctype html><html><head><link rel='stylesheet' href='css/style.css'></head>"
                            "<body><h1>Sunrise Bakery</h1><img src='images/hero.png' alt='Hero'></body></html>\n"
                            "```"
                        ),
                        "model": model_id,
                        "provider": "gemini",
                    }
                if role == "critic_verifier":
                    if attachments:
                        critic_received_attachments.extend(attachments)
                    return {
                        "ok": True,
                        "text": "PASS: Complete business webpage with valid CSS and non-empty PNG image.\nRECOMMENDED_NEXT_GOAL: Add menu pricing.",
                        "model": model_id,
                        "provider": "gemini",
                    }
                return {"ok": True, "text": "Done", "model": model_id, "provider": "gemini"}

            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=Path(tmp) / "reports",
                step_delay_sec=0.0,
                auto_spindown=False,
                llm_caller=fake_llm,
                media_caller=fake_media_caller,
                capability_manifest_fn=lambda _w: {"healthy_media_models": {"image": ["gemini/gemini-nano-banana-2.1"]}},
            )
            runner.configure_topology(coordinator_count=1, workers_per_coordinator=6)
            try:
                res = runner.launch_mission(
                    "make a web page for me for my business with images",
                    allow_workspace_writes=True,
                )
                self.assertTrue(res["ok"])
                out = runner.wait_for_mission(res["mission_id"], timeout=10.0)
                assert out is not None
                self.assertEqual(out["state"], "PASS")
                self.assertTrue(out["verdict"]["passed"])
                self.assertTrue((ws / "css" / "style.css").is_file())
                self.assertTrue((ws / "images" / "hero.png").is_file())
                self.assertEqual((ws / "images" / "hero.png").read_bytes(), VALID_1X1_PNG)
                self.assertTrue((ws / "index.html").is_file())
                self.assertEqual(len(media_calls), 1)
                self.assertEqual(media_calls[0]["model"], "gemini/gemini-nano-banana-2.1")
                self.assertEqual(len(critic_received_attachments), 1)
                self.assertEqual(critic_received_attachments[0].name, "hero.png")
            finally:
                runner.shutdown()

    # ------------------------------------------------------------------
    # Tier 3 & Tier 4: Web Asset Diagnostics & Critic <-> Executor Repair Loop
    # ------------------------------------------------------------------

    def test_tier4_workspace_sandbox_web_diagnostics_catches_broken_assets_and_corrupt_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "web_diag"
            (ws / "images").mkdir(parents=True)
            (ws / "css").mkdir(parents=True)
            (ws / "images" / "empty.png").write_bytes(b"")
            (ws / "images" / "fake.jpg").write_text("plain text pretending to be jpeg", encoding="utf-8")
            (ws / "images" / "bad.svg").write_text("<svg><unclosed>", encoding="utf-8")
            (ws / "css" / "broken.css").write_text("body { color: red; ", encoding="utf-8")
            (ws / "index.html").write_text(
                "<!doctype html><html><head><link rel='stylesheet' href='css/broken.css'></head>"
                "<body>"
                "<img src='images/missing.png'>"
                "<img src='images/empty.png'>"
                "<img src='images/fake.jpg'>"
                "<img src='images/bad.svg'>"
                "</body></html>",
                encoding="utf-8",
            )

            sb = WorkspaceSandbox(ws)
            diag = sb.execute_tool("run_diagnostics", {"path": ".", "checks": ["web"]})
            self.assertFalse(diag["passed"])
            codes = {item["code"] for item in diag["diagnostics"]}
            self.assertIn("WEB002", codes)  # missing.png
            self.assertIn("WEB003", codes)  # empty.png
            self.assertIn("WEB004", codes)  # fake.jpg corrupt magic header
            self.assertIn("WEB005", codes)  # bad.svg invalid XML
            self.assertIn("WEB006", codes)  # broken.css unbalanced braces

            # Now fix all assets and re-run web diagnostics
            (ws / "css" / "broken.css").write_text("body { color: red; }", encoding="utf-8")
            (ws / "images" / "good.svg").write_text(
                "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='40' fill='#38bdf8'/></svg>",
                encoding="utf-8",
            )
            (ws / "images" / "good.png").write_bytes(VALID_1X1_PNG)
            for stale in ("empty.png", "fake.jpg", "bad.svg"):
                (ws / "images" / stale).unlink()
            (ws / "index.html").write_text(
                "<!doctype html><html><head><link rel='stylesheet' href='css/broken.css'></head>"
                "<body><img src='images/good.png'><img src='images/good.svg'></body></html>",
                encoding="utf-8",
            )
            diag_ok = sb.execute_tool("run_diagnostics", {"path": ".", "checks": ["web"]})
            self.assertTrue(diag_ok["passed"], msg=str(diag_ok["diagnostics"]))

    def test_tier3_autonomous_critic_executor_refinement_loop_fixes_review_defects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "loop_site"
            ws.mkdir(parents=True)
            exec_calls = 0
            critic_calls = 0

            def loop_llm(role: str, model_id: str, system_prompt: str, user_prompt: str) -> dict[str, Any]:
                nonlocal exec_calls, critic_calls
                if role == "planner_architect":
                    return {"ok": True, "text": "MISSION_MODE: build\n1. Build index.html and hero SVG.", "model": model_id, "provider": "ollama"}
                if role == "research_scout":
                    return {"ok": True, "text": "Empty workspace.", "model": model_id, "provider": "ollama"}
                if role == "execution_specialist":
                    exec_calls += 1
                    if exec_calls == 1:
                        # Round 1: valid HTML + SVG, but missing contact section that Critic asks for
                        return {
                            "ok": True,
                            "text": (
                                "```write_file:images/hero.svg\n<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 80 40'><rect width='80' height='40' fill='#0ea5e9'/></svg>\n```\n"
                                "```write_file:index.html\n<!doctype html><html><body><h1>Apex Studio</h1><img src='images/hero.svg'></body></html>\n```"
                            ),
                            "model": model_id,
                            "provider": "ollama",
                        }
                    # Round 2: receives Critic REVIEW feedback and adds Contact section
                    assert "Critic Verifier Feedback" in user_prompt
                    return {
                        "ok": True,
                        "text": (
                            "Added contact section per Critic feedback.\n"
                            "```write_file:index.html\n<!doctype html><html><body><h1>Apex Studio</h1><img src='images/hero.svg'><section id='contact'>hello@apex.example</section></body></html>\n```"
                        ),
                        "model": model_id,
                        "provider": "ollama",
                    }
                if role == "critic_verifier":
                    critic_calls += 1
                    if critic_calls == 1:
                        return {
                            "ok": True,
                            "text": "REVIEW: Missing business contact section in index.html.\nRECOMMENDED_NEXT_GOAL: Add contact section.",
                            "model": model_id,
                            "provider": "ollama",
                        }
                    return {
                        "ok": True,
                        "text": "PASS: Hero SVG and contact section verified.\nRECOMMENDED_NEXT_GOAL: Add testimonials.",
                        "model": model_id,
                        "provider": "ollama",
                    }
                return {"ok": True, "text": "OK", "model": model_id, "provider": "ollama"}

            runner = LiveSwarmRunner(
                workspace_root=ws,
                artifacts_dir=Path(tmp) / "reports",
                step_delay_sec=0.0,
                auto_spindown=False,
                llm_caller=loop_llm,
                max_refinement_rounds=2,
            )
            try:
                res = runner.launch_mission("Create business landing page with contact section", allow_workspace_writes=True)
                out = runner.wait_for_mission(res["mission_id"], timeout=10.0)
                assert out is not None
                self.assertEqual(out["state"], "PASS")
                self.assertTrue(out["verdict"]["passed"])
                self.assertEqual(exec_calls, 2)
                self.assertEqual(critic_calls, 2)
                self.assertEqual(out["refinement_rounds_used"], 1)
                self.assertIn("hello@apex.example", (ws / "index.html").read_text(encoding="utf-8"))
            finally:
                runner.shutdown()

    # ------------------------------------------------------------------
    # Tier 5 & Tier 6: Skill Injection, SSRF Guard & Router Dead-Provider Short-Circuit
    # ------------------------------------------------------------------

    def test_tier5_skill_matching_and_ssrf_guard_and_tier6_router_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = _UnlockFakeVault(
                unlocked=["openai", "gemini"],
                probed=[
                    {"id": "openai/gpt-5-mini", "provider": "openai"},
                    {"id": "openai/gpt-5", "provider": "openai"},
                    {"id": "openai/omni-moderation-latest", "provider": "openai"},
                    {"id": "openai/gpt-realtime-mini", "provider": "openai"},
                    {"id": "gemini-3.5-flash-lite", "provider": "gemini"},
                ],
            )
            bridge = SabrinaDesktopBridge(
                role_bindings_path=root / "roles.json",
                vault=vault,
                swarm_artifacts_dir=root / "reports",
                step_delay_sec=0.0,
            )
            self.addCleanup(bridge.swarm_runner.shutdown)

            # Tier 5: SSRF protection blocks localhost/private URLs in ground_swarm_web
            ssrf_res = bridge.ground_swarm_web("fetch_url", "http://127.0.0.1:8087/secret")
            self.assertFalse(ssrf_res["ok"])
            self.assertIn("Blocked", ssrf_res["error"])

            # Tier 6: When OpenAI returns billing quota exhaustion on model #1,
            # _call_swarm_llm must NOT try gpt-5, omni-moderation, or gpt-realtime — it must jump straight to gemini!
            attempted_models: list[tuple[str, str]] = []

            def fake_invoke(
                provider: str,
                model_id: str,
                system_prompt: str,
                user_prompt: str,
                api_key: str | None = None,
                **kwargs: Any,
            ) -> dict[str, Any]:
                attempted_models.append((provider, model_id))
                if provider == "openai":
                    return {
                        "ok": False,
                        "error": "HTTP 429: insufficient_quota: You exceeded your current quota, please check your plan and billing details.",
                        "provider": "openai",
                        "model": model_id,
                    }
                return {
                    "ok": True,
                    "text": "Routed cleanly to healthy Gemini model.",
                    "provider": provider,
                    "model": model_id,
                    "tokens_used": 24,
                    "latency_ms": 12.0,
                }

            bridge._invoke_provider_text = fake_invoke  # type: ignore[method-assign]
            res = bridge._call_swarm_llm("execution_specialist", "openai/gpt-5-mini", "sys", "hello")
            self.assertTrue(res["ok"])
            self.assertEqual(res["provider"], "gemini")
            openai_attempts = [m for p, m in attempted_models if p == "openai"]
            self.assertEqual(len(openai_attempts), 1, f"Expected exactly 1 OpenAI attempt before inf cooldown, got {attempted_models}")
            self.assertEqual(bridge._provider_cooldowns.get("openai"), float("inf"))

            # Verify permanent LLM_WARN self-healing: execution_specialist primary_model was gemini-4-hydrogen (unprobed/deferred)
            # or if bound to openai/gpt-5-mini (zero credits), _call_swarm_llm auto-promotes the working model in role_bindings
            # so the next stage call has used_fallback=False (no perpetual LLM_WARN)!
            bridge.role_bindings.set_binding("planner_architect", "openai/gpt-5-mini", ["gemini-3.5-flash-lite"])
            r_warn = bridge._call_swarm_llm("planner_architect", "gemini-3.5-flash-lite", "sys", "plan 1")
            self.assertTrue(r_warn["ok"])
            self.assertTrue(r_warn.get("used_fallback"))
            self.assertEqual(bridge.role_bindings.get_binding("planner_architect").primary_model, "gemini-3.5-flash-lite")

            # Subsequent call using resolved role binding no longer triggers LLM_WARN!
            next_model = bridge.swarm_runner._resolve_model_for_role("planner_architect")
            r_clean = bridge._call_swarm_llm("planner_architect", next_model, "sys", "plan 2")
            self.assertTrue(r_clean["ok"])
            self.assertFalse(r_clean.get("used_fallback", False))

    def test_multifile_targeted_excerpts_shares_budget_across_all_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "repo"
            (ws / "pkg").mkdir(parents=True)
            for name in ("alpha_big.py", "beta_mid.py", "recovery_policy.py", "swarm_evo_bench.py"):
                body = "\n".join(f"def fn_{name[:4]}_{i}(): return {i}  # padding line for budget check" for i in range(180))
                (ws / "pkg" / name).write_text(body, encoding="utf-8")
            runner = LiveSwarmRunner(workspace_root=ws, artifacts_dir=Path(tmp) / "reports", auto_spindown=False)
            try:
                excerpts = runner._read_targeted_file_excerpts(
                    "Topology Mutation and Recovery Policy\nAudit pkg/alpha_big.py, beta_mid.py, recovery_policy.py, and swarm_evo_bench.py"
                )
                clipped = excerpts[: runner.subagent_excerpt_chars]
                # Workstream-matched recovery_policy.py comes FIRST and all 4 files fit inside 3200 chars!
                self.assertLess(clipped.find("recovery_policy.py"), clipped.find("alpha_big.py"))
                for fname in ("recovery_policy.py", "alpha_big.py", "beta_mid.py", "swarm_evo_bench.py"):
                    self.assertIn(fname, clipped)
            finally:
                runner.shutdown()

    def test_expanded_ai_systems_vault_and_routing(self) -> None:
        from swarm_command.role_binding import infer_model_provider
        from swarm_console.vault import SUPPORTED_PROVIDERS

        for expected_prov in (
            "huggingface",
            "cerebras",
            "sambanova",
            "fireworks",
            "nvidia",
            "github",
            "cohere",
            "perplexity",
            "pollinations",
            "lmstudio",
        ):
            self.assertIn(expected_prov, SUPPORTED_PROVIDERS)
            self.assertEqual(infer_model_provider(f"{expected_prov}/test-model"), expected_prov)
        self.assertEqual(infer_model_provider("portal:hf-gradio-htchu-coderhelper"), "portal")

        manifest = build_preflight_manifest(
            unlocked_providers=["huggingface", "cerebras", "pollinations"],
            ollama_ready=False,
            allow_workspace_writes=True,
        )
        self.assertIn("huggingface/Qwen/Qwen2.5-72B-Instruct", manifest.healthy_model_ids)
        self.assertIn("cerebras/llama-3.3-70b", manifest.healthy_model_ids)
        self.assertIn("pollinations/openai-fast", manifest.healthy_model_ids)
        self.assertIn("huggingface/black-forest-labs/FLUX.1-schnell", manifest.healthy_media_models.get("image", ()))
        self.assertIn("pollinations/flux", manifest.healthy_media_models.get("image", ()))


if __name__ == "__main__":
    unittest.main()
