import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sabrina_console_desktop import interface_privacy
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge


class InterfacePrivacyTests(unittest.TestCase):
    def test_fields_are_typed_and_unrecognized_values_fail_closed(self):
        fields, prompt = interface_privacy.build_brief("stale_write", {
            "base_is_current": False, "raw_log": "PRIVATE_LOG", "path": "PRIVATE_PATH",
        })
        self.assertEqual(fields, {"operation": "stale_write", "base_is_current": False})
        self.assertNotIn("PRIVATE", prompt)
        for operation, context in (
            ("unknown", {}), ("stale_write", {"base_is_current": "PRIVATE"}),
            ("cart", {"unit_cents": 4000, "actions": ["PRIVATE"]}),
            ("beta_zip", {"member_kinds": ["PRIVATE"]}),
        ):
            with self.assertRaises(ValueError):
                interface_privacy.build_brief(operation, context)
        self.assertEqual(interface_privacy.private_ollama_url("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertIsNone(interface_privacy.private_ollama_url("https://8.8.8.8"))
        with patch.object(interface_privacy.socket, "getaddrinfo", return_value=[
            (10, 1, 6, "", ("2606:4700::1111", 11434, 0, 0)),
            (2, 1, 6, "", ("192.168.1.50", 11434)),
        ]):
            self.assertEqual(interface_privacy.private_ollama_url("http://ollama.local:11434"),
                             "http://192.168.1.50:11434")

    def test_worker_context_stays_local_and_interface_fallback_is_filtered(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bridge = SabrinaDesktopBridge(role_bindings_path=root / "roles.json", vault_path=root / "vault.json")
            self.addCleanup(bridge.swarm_runner.shutdown)
            bridge._remote_cache["ollama_models"] = [{"name": "gemma3:4b"}]
            calls = []

            def transport(provider, model, system, prompt, **kwargs):
                calls.append((provider, system, prompt, kwargs))
                if provider == "gemini":
                    return {"ok": False, "error": "HTTP 503 temporarily unavailable"}
                return {"ok": True, "text": '{"result":["BLOCK","A","READ_CURRENT"]}', "provider": provider, "model": model}

            with patch.object(bridge, "_unlocked_providers", return_value={"ollama", "gemini", "groq"}), \
                    patch.object(bridge.vault, "get_key", return_value="synthetic-key"), \
                    patch.object(bridge, "_invoke_provider_text", side_effect=transport):
                private = bridge._call_private_swarm_llm("coordinator", "gemini-3.5-flash-lite", "PRIVATE_SYS", "PRIVATE_RAW")
                self.assertTrue(private["ok"])
                self.assertEqual([row[0] for row in calls], ["ollama"])
                self.assertIn("PRIVATE_RAW", calls[0][2])
                calls.clear()
                filtered = bridge.review_interface_brief("stale_write", {
                    "base_is_current": False, "raw_file": "PRIVATE_RAW",
                    "logs": ["PRIVATE_LOG"], "attachment": "PRIVATE_PATH",
                })
            self.assertTrue(filtered["ok"])
            self.assertEqual([row[0] for row in calls], ["gemini", "ollama"])
            self.assertTrue(all("PRIVATE" not in system + prompt and "attachments" not in kwargs
                                for _, system, prompt, kwargs in calls))
            audit = bridge.get_snapshot()["swarm"]["interface_dispatches"]
            self.assertEqual(audit[0]["fields"], {"operation": "stale_write", "base_is_current": False})
