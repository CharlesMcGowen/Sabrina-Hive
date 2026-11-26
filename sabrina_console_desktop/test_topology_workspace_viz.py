"""Tests for Live Multi-Agent Topology workspace visualization, CREATED/MODIFIED tracking, and Machine Catalog indexing."""

from __future__ import annotations

import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from sabrina_console_desktop.workspace_topology_index import (
    WorkspaceActivityTracker,
    get_machine_and_catalog_snapshot,
    refresh_and_search_machine_catalog,
)


class TestTopologyWorkspaceVisualization(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name).resolve()
        self.ws_dir = self.root / "project_ws"
        self.ws_dir.mkdir(parents=True, exist_ok=True)
        # Pre-existing baseline file
        (self.ws_dir / "app.py").write_text(
            '"""Baseline app module."""\n\ndef greet(name: str) -> str:\n    return f"Hello, {name}"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_tracker_distinguishes_created_and_modified_with_unified_diffs(self) -> None:
        tracker = WorkspaceActivityTracker(self.ws_dir, scan_ttl_sec=0.0)
        initial = tracker.scan_delta(force=True)
        self.assertFalse(initial["execution_authorized"])
        self.assertEqual(initial["scanned_files"], 1)
        self.assertEqual(initial["created_count"], 0)
        self.assertEqual(initial["modified_count"], 0)

        # 1. Agent modifies existing app.py
        target_app = self.ws_dir / "app.py"
        before_mod = tracker.snapshot_before_write(target_app)
        target_app.write_text(
            '"""Baseline app module."""\nimport math\n\ndef greet(name: str) -> str:\n    return f"Hi, {name}!"\n\ndef area(r: float) -> float:\n    return math.pi * r * r\n',
            encoding="utf-8",
        )
        rec_mod = tracker.record_after_write(
            target_app,
            before_mod,
            worker_id="m-01-executor",
            worker_name="Execution Specialist",
            role="execution_specialist",
            mission_id="m-01",
        )
        self.assertIsNotNone(rec_mod)
        assert rec_mod is not None
        self.assertEqual(rec_mod["status"], "MODIFIED")
        self.assertEqual(rec_mod["last_op"], "MODIFIED")
        self.assertGreater(rec_mod["lines_added"], 0)
        self.assertIn("@@", rec_mod["diff"])

        # 2. Sub-agent creates brand-new SVG asset
        svg_path = self.ws_dir / "images" / "badge.svg"
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        before_new = tracker.snapshot_before_write(svg_path)
        svg_path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 40"><rect width="80" height="40" fill="#10b981"/></svg>',
            encoding="utf-8",
        )
        rec_new = tracker.record_after_write(
            svg_path,
            before_new,
            worker_id="m-01-subagent_1",
            worker_name="Sub-Agent 1 (Brand SVG)",
            role="execution_specialist",
            mission_id="m-01",
        )
        self.assertIsNotNone(rec_new)
        assert rec_new is not None
        self.assertEqual(rec_new["status"], "CREATED")
        self.assertEqual(rec_new["last_op"], "CREATED")

        duplicate = tracker.record_after_write(
            svg_path,
            tracker.snapshot_before_write(svg_path),
            worker_id="retry-worker",
            worker_name="Retry Worker",
            mission_id="retry-mission",
        )
        self.assertEqual(duplicate["last_op"], "TOUCHED")
        after_duplicate = tracker.inspect_file("images/badge.svg")
        self.assertEqual(after_duplicate["worker_id"], rec_new["worker_id"])
        self.assertEqual(after_duplicate["mission_id"], rec_new["mission_id"])
        self.assertEqual(after_duplicate["revisions"], rec_new["revisions"])
        self.assertEqual(after_duplicate["diff"], rec_new["diff"])


        # 3. External/disk file created directly on disk is picked up by scan_delta
        (self.ws_dir / "notes.md").write_text("# Live Notes\nDetected by delta scan.\n", encoding="utf-8")
        delta = tracker.scan_delta(force=True)
        self.assertEqual(delta["created_count"], 2)
        self.assertEqual(delta["modified_count"], 1)

        # 4. Inspect modified Python file -> AST symbols, imports, and diff returned
        inspected_py = tracker.inspect_file("app.py")
        self.assertTrue(inspected_py["ok"])
        self.assertFalse(inspected_py["execution_authorized"])
        self.assertEqual(inspected_py["status"], "MODIFIED")
        sym_names = {s["name"] for s in inspected_py["symbols"]}
        self.assertIn("greet", sym_names)
        self.assertIn("area", sym_names)
        self.assertIn("math", inspected_py["imports"])
        self.assertIn("+def area", inspected_py["diff"])

        # 5. Inspect created SVG -> visual base64 data URI returned
        inspected_svg = tracker.inspect_file("images/badge.svg")
        self.assertTrue(inspected_svg["ok"])
        self.assertTrue(inspected_svg["is_image"])
        self.assertTrue(inspected_svg["image_data_uri"].startswith("data:image/svg+xml;base64,"))

        # 6. Path traversal & secret files are strictly blocked
        blocked_escape = tracker.inspect_file("../outside.txt")
        self.assertFalse(blocked_escape["ok"])
        (self.ws_dir / ".env").write_text("SECRET=123\n", encoding="utf-8")
        blocked_env = tracker.inspect_file(".env")
        self.assertFalse(blocked_env["ok"])

    def test_live_swarm_runner_attributes_created_and_modified_files_to_workers(self) -> None:
        def fake_llm(role: str, model: str, system: str, prompt: str) -> dict:
            if role == "planner_architect":
                return {
                    "ok": True,
                    "text": (
                        "MISSION_MODE: hybrid\n"
                        "1. Sub-agent creates stylesheet.\n"
                        "2. Execution Specialist updates app.py and creates index.html.\n"
                        "```swarm_roles\n"
                        '[{"role": "execution_specialist", "title": "Create CSS stylesheet", "allow_writes": true, "target_files": ["css/style.css"], "needs_coordinator": false}]\n'
                        "```"
                    ),
                    "model": model,
                    "provider": "gemini",
                    "latency_ms": 12.0,
                }
            if "Assigned Workstream:" in prompt:
                return {
                    "ok": True,
                    "text": "Created stylesheet.\n```write_file:css/style.css\nbody { background: #0f172a; color: #f8fafc; }\n```",
                    "model": model,
                    "provider": "gemini",
                    "latency_ms": 15.0,
                }
            if role == "execution_specialist":
                return {
                    "ok": True,
                    "text": (
                        "Updated app.py and created index.html.\n"
                        "```write_file:app.py\n"
                        '"""Updated app module."""\n\ndef greet(name: str) -> str:\n    return f"Welcome, {name}!"\n'
                        "```\n"
                        "```write_file:index.html\n"
                        '<!DOCTYPE html><html><head><link rel="stylesheet" href="css/style.css"></head><body><h1>Studio</h1></body></html>\n'
                        "```"
                    ),
                    "model": model,
                    "provider": "gemini",
                    "latency_ms": 20.0,
                }
            if role == "critic_verifier":
                return {
                    "ok": True,
                    "text": "PASS: Both created and modified deliverables verified.\nRECOMMENDED_NEXT_GOAL: Add unit tests.",
                    "model": model,
                    "provider": "gemini",
                    "latency_ms": 10.0,
                }
            return {"ok": True, "text": "Scouted workspace.", "model": model, "provider": "gemini", "latency_ms": 8.0}

        runner = LiveSwarmRunner(
            workspace_root=self.ws_dir,
            artifacts_dir=self.root / "artifacts",
            llm_caller=fake_llm,
            step_delay_sec=0.01,
            auto_spindown=False,
        )
        try:
            res = runner.launch_mission(
                "Update app.py greeting and build index.html with css/style.css",
                allow_workspace_writes=True,
            )
            self.assertTrue(res["ok"])
            mid = res["mission_id"]
            done_ev = runner._mission_done_events[mid]
            self.assertTrue(done_ev.wait(timeout=15.0))

            state = runner.get_state()
            self.assertFalse(state["execution_authorized"])
            ws_act = state["workspace_activity"]
            self.assertEqual(ws_act["created_count"], 2)
            self.assertEqual(ws_act["modified_count"], 1)

            by_path = {c["path"]: c for c in ws_act["changes"]}
            self.assertEqual(by_path["css/style.css"]["status"], "CREATED")
            self.assertIn("Sub-Agent", by_path["css/style.css"]["worker_name"])
            self.assertEqual(by_path["index.html"]["status"], "CREATED")
            self.assertEqual(by_path["index.html"]["worker_name"], "Execution Specialist")
            self.assertEqual(by_path["app.py"]["status"], "MODIFIED")
            self.assertEqual(by_path["app.py"]["worker_name"], "Execution Specialist")

            badges = {ev["badge"] for ev in state["activity_feed"]}
            self.assertIn("FILE_CREATED", badges)
            self.assertIn("FILE_MODIFIED", badges)

            exec_id = by_path["index.html"]["worker_id"]
            self.assertEqual(by_path["app.py"]["worker_id"], exec_id)
            exec_worker = next(w for w in state["workers"] if w["id"] == exec_id)
            touched_paths = {t["path"] for t in exec_worker["files_touched"]}
            self.assertIn("app.py", touched_paths)
            self.assertIn("index.html", touched_paths)
        finally:
            runner.shutdown()

    def test_browser_preview_opens_only_workspace_deliverables(self) -> None:
        page = self.ws_dir / "index.html"
        page.write_text("<h1>Preview</h1>", encoding="utf-8")
        (self.root / "outside.html").write_text("<h1>Outside</h1>", encoding="utf-8")
        bridge = SimpleNamespace(
            swarm_runner=SimpleNamespace(workspace_root=self.ws_dir),
            _default_workspace_root=self.ws_dir,
        )

        with patch("webbrowser.open", return_value=True) as browser:
            result = SabrinaDesktopBridge.open_topology_file_in_browser(bridge, "index.html")
            self.assertTrue(result["ok"])
            self.assertFalse(result["execution_authorized"])
            browser.assert_called_once_with(page.as_uri())
            for path in ("../outside.html", "app.py"):
                rejected = SabrinaDesktopBridge.open_topology_file_in_browser(bridge, path)
                self.assertFalse(rejected["ok"])
            self.assertEqual(browser.call_count, 1)

        with patch("webbrowser.open", return_value=False):
            result = SabrinaDesktopBridge.open_topology_file_in_browser(bridge, "index.html")
            self.assertFalse(result["ok"])


    def test_bridge_machine_catalog_indexing_and_symbol_search(self) -> None:
        cat_db = self.root / "test_catalog.sqlite3"
        rag_db = self.root / "test_rag.db"
        bridge = SabrinaDesktopBridge(
            role_bindings_path=self.root / "role_bindings.json",
            vault_path=self.root / "provider_vault.json",
            custom_models_path=self.root / "custom_models.json",
            swarm_artifacts_dir=self.root / "artifacts",
            ui_html_path=self.root / "ui.html",
        )
        bridge._catalog_db_path = cat_db
        try:
            set_res = bridge.set_workspace_directory(str(self.ws_dir), mode="sandbox_auto", create_if_missing=False)
            self.assertTrue(set_res["ok"])

            # Refresh machine & workspace SQLite code catalog
            idx_res = refresh_and_search_machine_catalog(
                [self.ws_dir],
                include_rag_fts=True,
                catalog_db_path=cat_db,
                rag_db_path=rag_db,
            )
            self.assertTrue(idx_res["ok"])
            self.assertFalse(idx_res["execution_authorized"])
            self.assertGreaterEqual(idx_res["snapshot"]["catalog"]["indexed_files"], 1)
            node_ids = {m["node_id"] for m in idx_res["snapshot"]["machines"]}
            self.assertIn("windows-workstation", node_ids)
            self.assertIn("ego-lab", node_ids)

            # Search indexed AST symbol `greet` via bridge
            search_res = bridge.index_workspace_and_machines(query="greet")
            self.assertTrue(search_res["ok"])
            self.assertGreaterEqual(len(search_res["search_hits"]), 1)
            self.assertTrue(any("app.py" in h["path"] for h in search_res["search_hits"]))

            # Verify snapshot includes workspace activity & machine_index, and reset_baseline clears changes
            (self.ws_dir / "extra.txt").write_text("new file\n", encoding="utf-8")
            rescan = bridge.rescan_topology_workspace(reset_baseline=False)
            self.assertEqual(rescan["activity"]["created_count"], 1)
            reset = bridge.rescan_topology_workspace(reset_baseline=True)
            self.assertEqual(reset["activity"]["created_count"], 0)
            self.assertEqual(reset["activity"]["scanned_files"], 2)
        finally:
            bridge.swarm_runner.shutdown()

    def test_ui_html_parity_and_zero_missing_dom_ids(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        desktop_html = repo_root / "sabrina_console_desktop" / "sabrina_desktop_ui.html"
        root_html = repo_root / "sabrina_desktop_ui.html"
        text = desktop_html.read_text(encoding="utf-8")
        self.assertEqual(text, root_html.read_text(encoding="utf-8"), "Root and desktop UI HTML files must stay synced")

        # Verify all static `el("literal-id")` calls have matching `id="literal-id"` in HTML
        defined_ids = set(re.findall(r'\bid="([^"]+)"', text))
        referenced_ids = set(re.findall(r'\bel\(\s*"([A-Za-z0-9_-]+)"\s*\)', text))
        missing = sorted(referenced_ids - defined_ids)
        self.assertEqual(missing, [], f"Missing DOM IDs referenced via el(): {missing}")

        for required_id in (
            "topo-workspace-panel",
            "topo-toggle-file-nodes",
            "topo-file-delta-pill",
            "topo-inspect-files-wrap",
            "topo-ws-list",
            "topo-preview-code",
            "btn-topo-ws-index",
            "btn-topo-ws-baseline",
        ):
            self.assertIn(required_id, defined_ids)

    def test_engine_self_protection_guard_blocks_core_overwrites(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        runner = LiveSwarmRunner(
            workspace_root=repo_root,
            artifacts_dir=self.root / "missions_guard",
            auto_spindown=False,
        )
        try:
            rejected: list[str] = []
            written = runner._apply_workspace_writes(
                "```write_file:sabrina_console_desktop/live_swarm_runner.py\n# rogue overwrite\n```\n"
                "```write_file:sabrina_desktop_ui.html\n<!-- rogue -->\n```\n",
                rejected_out=rejected,
            )
            self.assertEqual(written, [])
            self.assertEqual(len(rejected), 2)
            self.assertTrue(all("Engine Self-Protection Guard" in r for r in rejected))
        finally:
            runner.shutdown()


if __name__ == "__main__":
    unittest.main()
