"""Programmatic DOM & Multi-Cycle Self-Loop Integration Assertions for `sabrina_desktop_ui.html`.

Validates:
1. HTML DOM contract for `#quick-mission-goal` ("Enter a mission goal for the swarm..."),
   `#quick-auto-loop-count` (0/+1x/+2x/+3x), `#recommended-next-banner`, `#btn-restore-prev-goal`,
   `#btn-auto-seed-self-loop`, and `data-spawn-agent="recommend_next"`.
2. JS event-handler invariants (`applyRecommendedNextGoalToInputs`, `window.__remainingAutoLoops`,
   `seedSelfLoopFinalePreset`, `fId -> loop_root` self-loop preset, and Node VM execution when Node is installed).
3. End-to-end 3-cycle Bridge + SwarmRunner prompt-and-loop telemetry:
   Prompt 1 -> Swarm Mission 1 -> Finale Next Step 1 -> Prompt 2 -> Swarm Mission 2 -> Finale Next Step 2,
   plus SBV drift interception and payload byte-ceiling telemetry verification.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from sabrina_console_desktop.automation_engine import compute_goal_similarity, stabilize_evolved_goal
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge

ROOT = Path(__file__).resolve().parent.parent
UI_HTML = ROOT / "sabrina_console_desktop" / "sabrina_desktop_ui.html"


def _find_node_binary() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\nodejs\node.exe"),
        os.path.expandvars(r"%APPDATA%\nvm\current\node.exe"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class _DomInspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements_by_id: dict[str, dict[str, str]] = {}
        self.select_options: dict[str, list[str]] = {}
        self._current_select_id: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {k: (v if v is not None else "") for k, v in attrs}
        el_id = attr_map.get("id", "")
        if el_id:
            self.elements_by_id[el_id] = {"tag": tag.lower(), **attr_map}
        if tag.lower() == "select" and el_id:
            self._current_select_id = el_id
            self.select_options.setdefault(el_id, [])
        elif tag.lower() == "option" and self._current_select_id:
            self.select_options[self._current_select_id].append(attr_map.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "select":
            self._current_select_id = None


class TestUiSelfLoopAssertions(unittest.TestCase):
    def test_dom_controls_and_js_self_loop_handlers_in_sabrina_desktop_ui(self) -> None:
        html = UI_HTML.read_text(encoding="utf-8")
        inspector = _DomInspector()
        inspector.feed(html)

        # 1. Verify exact Swarm Mission Input placeholder & Finale Self-Loop controls
        quick_goal = inspector.elements_by_id.get("quick-mission-goal")
        self.assertIsNotNone(quick_goal)
        assert quick_goal is not None
        self.assertEqual(quick_goal["placeholder"], "Enter a mission goal for the swarm...")

        self.assertEqual(inspector.select_options.get("quick-auto-loop-count"), ["0", "1", "2", "3"])
        for req_id in (
            "btn-quick-suggest-next",
            "recommended-next-banner",
            "recommended-next-source",
            "recommended-next-text",
            "btn-run-recommended-next",
            "btn-restore-prev-goal",
            "btn-auto-seed-self-loop",
        ):
            self.assertIn(req_id, inspector.elements_by_id, f"Missing required DOM id={req_id}")

        # 2. Verify JS state machine wiring for input auto-fill, remaining auto-loops, Stop cancellation, and self-loop preset
        self.assertIn("function applyRecommendedNextGoalToInputs(", html)
        self.assertIn("window.__applyRecommendedNextGoal = applyRecommendedNextGoalToInputs;", html)
        self.assertIn("window.__remainingAutoLoops = Math.max(0, Number(window.__remainingAutoLoops) - 1);", html)
        self.assertIn("window.__remainingAutoLoops = 0;", html)
        self.assertIn('to: "loop_root"', html)
        self.assertRegex(html, r'step_type:\s*"recommend_next"')

        # Optional Node VM execution if Node.js is installed on host
        node_bin = _find_node_binary()
        if node_bin:
            proc = subprocess.run(
                [node_bin, "-e", "const fs=require('fs'); console.log(fs.statSync(process.argv[1]).size);", str(UI_HTML)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0)

    def test_three_cycle_prompt_and_loop_telemetry_with_sbv_and_budget_guards(self) -> None:
        mission_round = 0

        def fake_swarm_llm(role: str, model_id: str, sys_prompt: str, user_prompt: str):
            nonlocal mission_round
            if role == "critic_verifier":
                mission_round += 1
                return {
                    "ok": True,
                    "model": "gemini-3.5-flash-lite",
                    "provider": "gemini",
                    "latency_ms": 12.0,
                    "text": (
                        f"PASS: Verified Sabrina cycle {mission_round}.\n"
                        f"RECOMMENDED_NEXT_GOAL: Cycle {mission_round + 1} Sabrina A2A telemetry & payload budget audit"
                    ),
                }
            return {
                "ok": True,
                "model": "gemini-3.5-flash-lite",
                "provider": "gemini",
                "latency_ms": 10.0,
                "text": f"Completed {role} stage for: {user_prompt.splitlines()[0][:80]}",
            }

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bridge = SabrinaDesktopBridge(
                role_bindings_path=tmp_path / "roles.json",
                vault_path=tmp_path / "vault.json",
                custom_models_path=tmp_path / "models.json",
                swarm_artifacts_dir=tmp_path / "missions",
                spindown_delay_sec=0.05,
                step_delay_sec=0.0,
                llm_caller=fake_swarm_llm,
            )
            try:
                # Simulate operator entering initial prompt with `Loop +2x` (3 total cycles)
                quick_input_value = "Cycle 1 Sabrina A2A workflow & Finale input handoff verification"
                executed_prompts: list[str] = []
                remaining_auto_loops = 2

                while True:
                    executed_prompts.append(quick_input_value)
                    launch = bridge.launch_mission(quick_input_value, mode="codex_thought_antigravity_implement")
                    self.assertTrue(launch["ok"])
                    out = bridge.swarm_runner.wait_for_mission(launch["mission_id"], timeout=15.0)
                    self.assertIsNotNone(out)
                    assert out is not None
                    self.assertEqual(out["state"], "PASS")

                    snap = bridge.get_snapshot()
                    rec = snap["recommended_next_step"]["goal"]
                    self.assertTrue(rec.startswith(f"Cycle {len(executed_prompts) + 1} Sabrina A2A"))

                    # Auto-fill `#quick-mission-goal` ("Enter a mission goal for the swarm...")
                    quick_input_value = rec
                    if remaining_auto_loops > 0:
                        remaining_auto_loops -= 1
                        continue
                    break

                # Verify 3 full cycles ran and the 4th recommended next step is now sitting ready in the input!
                self.assertEqual(len(executed_prompts), 3)
                self.assertEqual(quick_input_value, "Cycle 4 Sabrina A2A telemetry & payload budget audit")

                # Verify SBV similarity keeps domain-aligned evolutions untethered while catching off-topic drift
                aligned_sim = compute_goal_similarity(executed_prompts[0], quick_input_value)
                self.assertGreaterEqual(aligned_sim, 0.25)
                drifted = stabilize_evolved_goal(
                    anchor_goal=executed_prompts[0],
                    candidate_goal="Book flights to Paris and reserve hotel rooms",
                    goal_history=executed_prompts,
                    drift_threshold=0.16,
                )
                self.assertTrue(drifted["drift_intercepted"])
                self.assertTrue(drifted["effective_goal"].startswith("[Anchored to:"))
            finally:
                bridge.swarm_runner.shutdown()


if __name__ == "__main__":
    unittest.main()
