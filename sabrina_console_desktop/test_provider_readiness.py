import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge


class ProviderReadinessTest(unittest.TestCase):
    def test_power_shell_failure_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = SabrinaDesktopBridge(role_bindings_path=root / "roles.json", vault_path=root / "vault.json",
                                          swarm_artifacts_dir=root / "reports", step_delay_sec=0)
            runner = bridge.swarm_runner
            runner.set_workspace_root(root)
            runner.llm_caller = bridge._call_swarm_llm
            runner.model_readiness = bridge._ready_stage_model
            bridge._unlocked_providers = lambda: {"openai", "gemini"}
            bridge.vault.get_key = lambda *args, **kwargs: "fake"
            good = "gemini/gemini-3.5-flash-lite"
            roles = {
                "planner_architect": "openai/gpt-5",
                "research_scout": "openai/gpt-5-mini",
                "execution_specialist": "gemini/gemini-4-hydrogen",
                "critic_verifier": "gemini/gemini-4-neon",
            }
            for role, model in roles.items():
                bridge.role_bindings.set_binding(role, model, [good])
            errors = {
                roles["planner_architect"]: "HTTP 429 no credits remaining",
                roles["execution_specialist"]: "HTTP 503 high demand",
                roles["critic_verifier"]: "HTTP 429 limit: 0 input tokens per minute",
            }
            calls = []

            def fake_provider(provider, model, system, prompt, **kwargs):
                calls.append(model)
                if model in errors:
                    return {"ok": False, "error": errors[model]}
                reply = "Inspect the requested PowerShell facts."
                if "approved tools below" in system:
                    reply = "PowerShell inspection complete." if "tool feedback" in prompt else "\n".join(
                        f"```tool:{name}\n{args}\n```" for name, args in (
                            ("powershell_info", "{}"),
                            ("powershell_help", '{"cmdlet":"Get-Item"}'),
                            ("powershell_item", '{"path":"fixture.txt"}'),
                        )
                    )
                if "independent Critic Verifier" in system:
                    reply = "PASS: Verified PowerShell receipts."
                return {"ok": True, "model": model, "provider": provider, "text": reply}

            bridge._invoke_provider_text = fake_provider
            (root / "fixture.txt").write_bytes(b"receipt")
            try:
                with patch("sabrina_console_desktop.sabrina_desktop_ui.time.monotonic", return_value=100):
                    for role in ("planner_architect", "execution_specialist", "critic_verifier"):
                        result = bridge._call_swarm_llm(role, roles[role], "", "")
                        self.assertEqual(result["model"], good)
                    self.assertEqual(bridge._ready_stage_model("research_scout", roles["research_scout"]), good)
                with patch("sabrina_console_desktop.sabrina_desktop_ui.time.monotonic", return_value=161):
                    self.assertEqual(bridge._ready_stage_model("execution_specialist", roles["execution_specialist"]),
                                     roles["execution_specialist"])
                    self.assertEqual(bridge._ready_stage_model("critic_verifier", roles["critic_verifier"]), good)
                if sys.platform == "win32":
                    calls.clear()
                    with patch("sabrina_console_desktop.sabrina_desktop_ui.time.monotonic", return_value=150):
                        launch = bridge.launch_powershell_mission("Inspect the Windows host and fixture.txt")
                        self.assertTrue(launch["ok"], launch)
                        output = runner.wait_for_mission(launch["mission_id"], timeout=20)
                    self.assertEqual(output["state"], "PASS", output)
                    self.assertEqual(set(calls), {good})
                    self.assertEqual({row["tool"] for row in output["powershell_tool_receipts"]},
                                     {"powershell_info", "powershell_help", "powershell_item"})
                    self.assertFalse(output["execution_authorized"])
                    self.assertIn("account credits exhausted", output["report_markdown"])
                    self.assertIn("temporary cooldown after HTTP 503", output["report_markdown"])
                    self.assertIn("model quota is zero", output["report_markdown"])
                with patch.object(bridge, "_unlocked_providers", return_value={"openai"}):
                    calls.clear()
                    blocked = bridge._call_swarm_llm("planner_architect", roles["planner_architect"], "", "")
                    self.assertIn("Blocked: no eligible model route", blocked["error"])
                    self.assertEqual(calls, [])
                with patch.object(bridge.vault, "probe_provider", return_value={"ok": True}), \
                        patch.object(bridge.registry, "refresh_models"):
                    for provider in ("openai", "gemini"):
                        self.assertTrue(bridge.probe_vault_provider(provider)["ok"])
                for role in ("planner_architect", "critic_verifier"):
                    self.assertEqual(bridge._ready_stage_model(role, roles[role]), roles[role])
            finally:
                runner.shutdown()
