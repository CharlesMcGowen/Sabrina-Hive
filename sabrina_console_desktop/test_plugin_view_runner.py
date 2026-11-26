import tempfile
import unittest
from pathlib import Path

from tools import plugin_console

GOOD_VIEW_PLUGIN = """TOOL_NAME = 'sample'
TOOL_VIEW = {'operations': ['show', 'echo']}
def execute(action, loop=None):
    if action['operation'] == 'show':
        return {'ok': True, 'view': {'version': 1, 'blocks': [{'type': 'text', 'text': 'hi'}]}}
    return {'ok': True, 'received': action.get('inputs')}
"""

BAD_VIEW_PLUGIN = """TOOL_NAME = 'sample'
TOOL_VIEW = {'operations': ['show']}
def execute(action, loop=None):
    return {'ok': True, 'view': {'version': 1, 'blocks': [{'type': 'script', 'code': 'x'}]}}
"""


class ViewRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.custom = self.root / "tools" / "custom"
        self.custom.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, source):
        (self.custom / "sample_plugin.py").write_text(source, encoding="utf-8")

    def test_declared_operation_runs_and_view_is_validated(self):
        self.write(GOOD_VIEW_PLUGIN)
        result = plugin_console.run_view("sample", "show", {}, root=self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["result"]["view"]["blocks"][0]["text"], "hi")

    def test_undeclared_operation_is_refused_before_running(self):
        marker = self.root / "ran"
        self.write(GOOD_VIEW_PLUGIN.replace("TOOL_NAME", f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\nTOOL_NAME"))
        result = plugin_console.run_view("sample", "delete_everything", {}, root=self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "operation_not_declared")
        self.assertFalse(marker.exists())

    def test_inputs_must_be_plain_scalars(self):
        self.write(GOOD_VIEW_PLUGIN)
        self.assertFalse(plugin_console.run_view("sample", "echo", {"x": {"nested": 1}}, root=self.root)["ok"])
        self.assertFalse(plugin_console.run_view("sample", "echo", {"Bad Name": 1}, root=self.root)["ok"])
        ok = plugin_console.run_view("sample", "echo", {"size": 2, "dry": True}, root=self.root)
        self.assertEqual(ok["data"]["result"]["received"], {"size": 2, "dry": True})

    def test_invalid_view_from_plugin_is_rejected(self):
        self.write(BAD_VIEW_PLUGIN)
        result = plugin_console.run_view("sample", "show", {}, root=self.root)
        self.assertFalse(result["ok"])
        self.assertIn("Plugin view rejected", result["error"])

    def test_bad_declaration_is_a_discovery_error(self):
        self.write("TOOL_NAME = 'sample'\nTOOL_VIEW = {'operations': ['Bad Op']}\ndef execute(action): return {}\n")
        plugins, errors = plugin_console.discover(self.root)
        self.assertNotIn("sample", plugins)
        self.assertTrue(any("TOOL_VIEW" in e["error"] or "operations" in e["error"] for e in errors))


if __name__ == "__main__":
    unittest.main()
