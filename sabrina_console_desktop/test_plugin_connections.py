import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from connector_registry import ConnectorRegistry
from tools import plugin_console
from tools.custom import kraken_console_plugin


class PluginConnectionTests(unittest.TestCase):
    def test_vault_round_trip_masks_and_preserves_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vault.json"
            vault = ConnectorRegistry(vault_path=path)
            vault.save_plugin_config("kraken_console", {"api_key": "fake-key-123", "api_secret": "fake-secret-456"})
            self.assertNotIn("fake-key-123", path.read_text())
            self.assertNotIn("fake-secret-456", path.read_text())
            listed = vault.list_plugin_connections()
            item = next(item for item in listed if item["id"] == "kraken_console")
            self.assertTrue(item["configured"])
            self.assertNotIn("fake-secret-456", json.dumps(item))
            vault.save_plugin_config("kraken_console", {"api_key": "", "api_secret": "replacement-secret"})
            self.assertEqual(vault.get_plugin_config("kraken_console"), {"api_key": "fake-key-123", "api_secret": "replacement-secret"})
            with self.assertRaises(ValueError):
                vault.save_plugin_config("kraken_console", {"unexpected": "value"})

    def test_literal_declaration_validation(self):
        with self.assertRaises(ValueError):
            plugin_console.validate_connection({"title": "Bad", "fields": [{"key": "api_key", "label": "Key"}]})
        valid = plugin_console.validate_connection({"title": "Hook", "fields": [{"key": "endpoint", "label": "URL", "kind": "url"}]})
        self.assertEqual(valid["fields"][0]["kind"], "url")

    def test_kraken_secrets_only_in_stdin(self):
        fake = Mock()
        fake.get_plugin_config.return_value = {"api_key": "fake-key", "api_secret": "fake-secret"}
        response = Mock(returncode=0, stdout='{"data": {}}\n')
        with patch("connector_registry.ConnectorRegistry", return_value=fake), patch.object(kraken_console_plugin.subprocess, "run", return_value=response) as run:
            result = kraken_console_plugin.execute({"operation": "balances"})
        self.assertTrue(result["ok"])
        self.assertNotIn("fake-secret", str(run.call_args.args[0]))
        self.assertNotIn("fake-key", str(run.call_args.args[0]))
        self.assertNotIn("fake-secret", json.dumps(result))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["api_secret"], "fake-secret")


if __name__ == "__main__":
    unittest.main()
