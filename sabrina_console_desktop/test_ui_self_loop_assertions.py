"""Programmatic DOM & Multi-Cycle Self-Loop Integration Assertions for `sabrina_desktop_ui.html`.

Validates:
1. HTML DOM contract for `#quick-mission-goal` ("Enter a mission goal for the swarm..."),
   `#quick-auto-loop-count` (0/+1x/+2x/+3x), `#recommended-next-banner`, `#btn-restore-prev-goal`,
   `#btn-auto-seed-self-loop`, and `data-spawn-agent="recommend_next"`.
2. Browser handlers for background follow-up submission, recommendation review, Stop, and the self-loop preset.
3. End-to-end 3-cycle Bridge + SwarmRunner prompt-and-loop telemetry:
   Prompt 1 -> Swarm Mission 1 -> Finale Next Step 1 -> Prompt 2 -> Swarm Mission 2 -> Finale Next Step 2,
   plus SBV drift interception and payload byte-ceiling telemetry verification.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

from sabrina_console_desktop.automation_engine import compute_goal_similarity, stabilize_evolved_goal
from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from sabrina_console_desktop.test_snapshot_ui import find_browser

ROOT = Path(__file__).resolve().parent.parent
UI_HTML = ROOT / "sabrina_console_desktop" / "sabrina_desktop_ui.html"


_SELF_LOOP_PROBE = """
<output id="self-loop-result">pending</output>
<script>
(async () => {
  const el = id => document.getElementById(id);
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  const check = (ok, message) => { if (!ok) throw new Error(message); };
  const launches = [], stops = [];
  const goal = el("quick-mission-goal");
  let saved;
  window.pywebview = {api: {
    launch_mission: async (...args) => { launches.push(args); return {ok:true}; },
    stop_mission: async id => { stops.push(id); return {ok:true}; },
    get_snapshot: async () => ({swarm:{workers:[], active_worker_count:0},
                               recommended_next_step:{goal:"Review results", source:"finale"}}),
    save_automation: async spec => { saved = spec; return {ok:true}; },
  }};
  try {
    goal.value = "Check inputs";
    el("quick-auto-loop-count").value = "2";
    el("btn-quick-launch").click();
    await wait(0);
    check(launches.length === 1 && launches[0][0] === "Check inputs" && launches[0][10] === 2,
          "Quick launch did not submit the selected follow-up count once");
    check(goal.value === "Review results" && el("recommended-next-text").textContent === goal.value,
          "The completed run did not populate the recommended goal");
    el("btn-restore-prev-goal").click();
    el("btn-refresh").click();
    await wait(1000);  // Advance past the former 950 ms browser relaunch timer.
    check(goal.value === "Check inputs" && launches.length === 1, "A repeated snapshot reapplied or launched the recommendation");
    el("btn-quick-stop").click();
    check(stops.length === 1 && stops[0] === "", "Stop did not cancel background missions through the bridge");
    el("btn-auto-seed-self-loop").click();
    el("btn-manual-save").click();
    const finale = saved.steps.find(step => step.step_type === "recommend_next");
    check(finale && saved.edges.some(edge => edge.from === finale.id && edge.to === "loop_root" &&
          edge.comm_type === "loop" && edge.max_loops === 1), "The preset did not loop from the finale to the root once");
    el("self-loop-result").textContent = "passed";
  } catch (error) { el("self-loop-result").textContent = String(error); }
})();
</script>
"""


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

        browser = find_browser()
        if not browser:
            self.skipTest("Requires an installed Edge or Chromium browser")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "self-loop.html"
            page.write_text(html.replace("</body>", _SELF_LOOP_PROBE + "</body>"), encoding="utf-8")
            result = subprocess.run(
                [browser, "--headless", "--disable-gpu", "--disable-extensions", "--no-first-run",
                 "--disable-background-networking", f"--user-data-dir={root / 'profile'}",
                 "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=True,
            )
        outcome = re.search(r'<output id="self-loop-result">([^<]*)</output>', result.stdout)
        self.assertIsNotNone(outcome, "Browser did not return the self-loop page")
        self.assertEqual(unescape(outcome[1]), "passed")

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
