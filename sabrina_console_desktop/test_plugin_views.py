import copy
import unittest

from tools import plugin_views as pv

PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
PNG_SRC = "data:image/png;base64," + PNG_1PX


def view(*blocks):
    return {"version": 1, "blocks": list(blocks)}


class ViewSchemaTests(unittest.TestCase):
    def test_valid_view_normalizes_and_lists_form_operations(self):
        doc = view(
            {"type": "text", "text": "Hello"},
            {"type": "table", "columns": ["a", "b"], "rows": [["x", 1.5], [None, True]]},
            {"type": "chart", "kind": "line", "labels": ["t1", "t2"],
             "series": [{"name": "pnl", "values": [1, 2.5]}]},
            {"type": "image", "src": PNG_SRC, "alt": "pixel"},
            {"type": "animation", "frames": [PNG_SRC, PNG_SRC], "fps": 2},
            {"type": "graph", "nodes": [{"id": "a", "kind": "model"}, {"id": "b", "kind": "agent"}],
             "edges": [{"from": "a", "to": "b", "label": "feeds"}]},
            {"type": "form", "id": "order", "submit": {"label": "Run", "operation": "quote"},
             "fields": [{"name": "pair", "kind": "select", "options": ["BTCUSD", "ETHUSD"]},
                        {"name": "size", "kind": "number", "min": 0, "max": 10, "default": 1},
                        {"name": "dry", "kind": "checkbox", "default": True}]},
        )
        normalized, ops = pv.validate_view(doc)
        self.assertEqual(ops, ["quote"])
        self.assertEqual(len(normalized["blocks"]), 7)

    def test_rejects_unknown_block_and_bad_version(self):
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "script", "code": "alert(1)"}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view({"version": 2, "blocks": [{"type": "text", "text": "x"}]})
        with self.assertRaises(pv.ViewError):
            pv.validate_view({"version": 1, "blocks": [{"type": "text", "text": "x"}], "html": "<b>"})

    def test_table_and_chart_shape_checks(self):
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "table", "columns": ["a", "b"], "rows": [["only one"]]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "chart", "kind": "pie", "labels": ["a"],
                                   "series": [{"name": "x", "values": [1]}, {"name": "y", "values": [2]}]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "chart", "kind": "bar", "labels": ["a", "b"],
                                   "series": [{"name": "x", "values": [1]}]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "chart", "kind": "bar", "labels": ["a"],
                                   "series": [{"name": "x", "values": [float("nan")]}]}))

    def test_image_sources_are_restricted(self):
        for bad in (
            "javascript:alert(1)",
            "http://example.com/x.png",
            "data:image/svg+xml;base64," + PNG_1PX,
            "data:image/png;base64,AAAA",  # bytes do not match the declared type
            "data:image/png;base64,@@@@",
        ):
            with self.subTest(src=bad[:30]):
                with self.assertRaises(pv.ViewError):
                    pv.validate_view(view({"type": "image", "src": bad}))

    def test_animation_needs_two_to_sixty_frames_and_valid_fps(self):
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "animation", "frames": [PNG_SRC]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "animation", "frames": [PNG_SRC, PNG_SRC], "fps": 99}))

    def test_graph_rejects_unknown_nodes_and_bad_kinds(self):
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "graph", "nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "zzz"}]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "graph", "nodes": [{"id": "a", "kind": "shell"}]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "graph", "nodes": [{"id": "a"}, {"id": "a"}]}))

    def test_form_schema_rejects_bad_definitions(self):
        base = {"type": "form", "id": "f", "submit": {"label": "Go", "operation": "run"}}
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({**base, "fields": [{"name": "p", "kind": "select", "options": ["a"], "default": "b"}]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({**base, "fields": [{"name": "p", "kind": "text"}, {"name": "p", "kind": "text"}]}))
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({**base, "fields": [{"name": "p", "kind": "file"}]}))

    def test_form_inputs_are_checked_against_the_schema(self):
        doc, _ = pv.validate_view(view({
            "type": "form", "id": "f", "submit": {"label": "Go", "operation": "run"},
            "fields": [
                {"name": "pair", "kind": "select", "options": ["BTCUSD"], "required": True},
                {"name": "size", "kind": "number", "min": 1, "max": 5},
                {"name": "dry", "kind": "checkbox"},
            ],
        }))
        form = doc["blocks"][0]
        self.assertEqual(pv.validate_form_inputs(form, {"pair": "BTCUSD", "size": 2, "dry": True}),
                         {"pair": "BTCUSD", "size": 2.0, "dry": True})
        for bad in ({}, {"pair": "ETHUSD"}, {"pair": "BTCUSD", "size": 9}, {"pair": "BTCUSD", "extra": 1},
                    {"pair": "BTCUSD", "dry": "yes"}):
            with self.subTest(inputs=bad):
                with self.assertRaises(pv.ViewError):
                    pv.validate_form_inputs(form, bad)

    def test_block_count_limit_and_non_text_values(self):
        too_many = [{"type": "text", "text": "x"} for _ in range(pv.MAX_BLOCKS + 1)]
        with self.assertRaises(pv.ViewError):
            pv.validate_view({"version": 1, "blocks": too_many})
        with self.assertRaises(pv.ViewError):
            pv.validate_view(view({"type": "text", "text": object()}))


if __name__ == "__main__":
    unittest.main()
