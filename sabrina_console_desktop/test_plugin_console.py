import tempfile
import unittest
from pathlib import Path
from tools import plugin_console


class PluginConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.custom = self.root / "tools" / "custom"
        self.custom.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, filename, source):
        (self.custom / filename).write_text(source, encoding="utf-8")

    def test_discovery_does_not_execute_and_explicit_run_does(self):
        marker = self.root / "imported"
        self.write("sample_plugin.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\nTOOL_NAME='sample'\nTOOL_PROMPT='A sample'\ndef execute(action, loop=None):\n return {{'received': action}}\n")
        found = plugin_console.execute_command("/plugin list", root=self.root)
        self.assertTrue(found["ok"])
        self.assertFalse(marker.exists())
        result = plugin_console.execute_command('/plugin run sample {"operation":"status"}', root=self.root)
        self.assertTrue(result["ok"])
        self.assertTrue(marker.exists())
        self.assertEqual(result["data"]["result"]["received"]["operation"], "status")

    def test_duplicate_plugin_name_is_unavailable(self):
        for f in ("a_plugin.py", "b_plugin.py"):
            self.write(f, "TOOL_NAME='same'\ndef execute(action): return {}\n")
        result = plugin_console.execute_command('/plugin run same {}', root=self.root)
        self.assertFalse(result["ok"])

    def test_json_string_failure_propagates(self):
        self.write("sample_plugin.py", """TOOL_NAME='sample'
def execute(action):
 return '{"ok": false, "error": "unsupported"}'
""")
        result = plugin_console.execute_command('/plugin run sample {}', root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["result"]["error"], "unsupported")

    def test_requires_json_object_and_no_implicit_action(self):
        self.write("sample_plugin.py", "TOOL_NAME='sample'\ndef execute(action): return action\n")
        self.assertFalse(plugin_console.execute_command('/plugin run sample []', root=self.root)["ok"])
        self.assertFalse(plugin_console.execute_command('/plugin run sample', root=self.root)["ok"])

    def test_tab_must_declare_read_access(self):
        self.write("sample_plugin.py", "TOOL_NAME='sample'\nTOOL_ACCESS='write'\nTOOL_TAB={'title':'S','operation':'status','display':'json'}\ndef execute(action): return {}\n")
        result = plugin_console.list_tabs(root=self.root)
        self.assertEqual(result["tabs"], [])
        self.assertTrue(any("TOOL_ACCESS" in e["error"] for e in result["discovery_errors"]))

    def test_tab_rejects_unknown_keys_and_bad_refresh(self):
        self.write("sample_plugin.py", "TOOL_NAME='sample'\nTOOL_ACCESS='read'\nTOOL_TAB={'title':'S','operation':'status','display':'json','onclick':'x'}\ndef execute(action): return {}\n")
        self.assertEqual(plugin_console.list_tabs(root=self.root)["tabs"], [])
        self.write("sample_plugin.py", "TOOL_NAME='sample'\nTOOL_ACCESS='read'\nTOOL_TAB={'title':'S','operation':'status','display':'json','refresh_seconds':1}\ndef execute(action): return {}\n")
        self.assertEqual(plugin_console.list_tabs(root=self.root)["tabs"], [])

    def test_valid_tab_is_listed_without_running_plugin(self):
        marker = self.root / "imported"
        self.write("sample_plugin.py", f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\nTOOL_NAME='sample'\nTOOL_ACCESS='read'\nTOOL_TAB={{'title':'Sample','operation':'status','display':'text','refresh_seconds':0}}\ndef execute(action): return {{}}\n")
        result = plugin_console.list_tabs(root=self.root)
        self.assertEqual(result["tabs"], [{"plugin": "sample", "title": "Sample", "icon": "🧩", "operation": "status", "display": "text", "refresh_seconds": 0}])
        self.assertFalse(marker.exists())

    def test_shipped_kraken_monitor_declares_read_only_tab(self):
        tabs = {t["plugin"]: t for t in plugin_console.list_tabs()["tabs"]}
        self.assertIn("kraken_monitor", tabs)
        self.assertEqual(tabs["kraken_monitor"]["operation"], "snapshot")
        self.assertEqual(tabs["kraken_monitor"]["display"], "html")

    def test_html_and_dashboard_tabs_sanitize_scripts_and_inline_handlers(self):
        self.write(
            "ui_plugin.py",
            "TOOL_NAME='ui'\n"
            "TOOL_ACCESS='read'\n"
            "TOOL_TAB={'title':'Custom UI','operation':'view','display':'html','refresh_seconds':10}\n"
            "def execute(action):\n"
            "    return {\n"
            "        'ok': True,\n"
            "        'html': (\n"
            "            '<style>.card{color:#fff;} @import url(evil);</style>'\n"
            "            '<div class=\"card\" onclick=\"steal()\">'\n"
            "            '<script>window.pwned=1;</script>'\n"
            "            '<iframe src=\"https://evil.example\"></iframe>'\n"
            "            '<svg viewBox=\"0 0 100 20\"><linearGradient id=\"g\"></linearGradient><polyline points=\"0,10 100,2\" stroke=\"#34d399\" /></svg>'\n"
            "            '<canvas data-plugin-chart=\"kraken_gainers_24h\"></canvas>'\n"
            "            '<button data-plugin-op=\"view\" type=\"submit\">Reload</button>'\n"
            "            '<a href=\"javascript:alert(1)\">Bad</a><a href=\"#top\">Good</a>'\n"
            "            '</div>'\n"
            "        ),\n"
            "    }\n",
        )
        tabs = plugin_console.list_tabs(root=self.root)
        self.assertEqual(len(tabs["tabs"]), 1)
        self.assertEqual(tabs["tabs"][0]["display"], "html")

        res = plugin_console.execute_command('/plugin run ui {"operation":"view"}', root=self.root)
        self.assertTrue(res["ok"])
        cleaned = res["data"]["result"]["html"]
        self.assertNotIn("<script", cleaned.lower())
        self.assertNotIn("window.pwned", cleaned)
        self.assertNotIn("<iframe", cleaned.lower())
        self.assertNotIn("onclick", cleaned.lower())
        self.assertNotIn("javascript:", cleaned.lower())
        self.assertNotIn("@import", cleaned.lower())
        self.assertIn('viewBox="0 0 100 20"', cleaned)
        self.assertIn("<linearGradient", cleaned)
        self.assertIn('data-plugin-chart="kraken_gainers_24h"', cleaned)
        self.assertIn('data-plugin-op="view"', cleaned)
        self.assertIn('type="button"', cleaned)
        self.assertIn('href="#top"', cleaned)


if __name__ == "__main__":
    unittest.main()
