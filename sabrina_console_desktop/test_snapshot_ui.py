"""Exercise snapshot rendering and recovery in a browser with a local test bridge."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from html import unescape
from pathlib import Path


def find_browser():
    for name in ("msedge", "chromium", "chromium-browser", "google-chrome"):
        if executable := shutil.which(name):
            return executable
    for folder in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(folder)
        if base:
            edge = Path(base) / "Microsoft/Edge/Application/msedge.exe"
            if edge.is_file():
                return str(edge)
    return None


PROBE = """
<output id="snapshot-test-result">pending</output>
<script>
(async () => {
  const element = id => {
    const node = document.getElementById(id);
    if (!node) throw new Error(`Missing test element: ${id}`);
    return node;
  };
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const nextTurn = () => new Promise(resolve => setTimeout(resolve, 0));
  const refresh = async () => { element("btn-refresh").click(); await nextTurn(); };
  const worker = {id: "test-worker", name: "Test worker", status: "DONE", role: "reviewer"};
  const good = {
    api_ok: true, api_8095_ok: true,
    swarm: {workers: [worker], active_worker_count: 0, time_str: "test time",
            mission_outputs: [{mission_id: "test-mission", state: "FAILED"}]},
  };
  let snapshot = good;
  let reloads = 0;
  const api = {
    get_snapshot: async () => snapshot,
    reload_ui: async () => { reloads++; return {ok: true, reloaded: false}; },
  };
  window.pywebview = {api};
  try {
    element("swarm-monitor-detail").open = true;
    await refresh();
    check(element("sync-time").textContent.includes("test time"), "Snapshot did not finish rendering");
    check(element("monitor-running-card").open, "Running should be visible by default");
    check(!element("monitor-attention-card").open && !element("monitor-recent-card").open,
          "Inactive monitor sections should start compact");
    const loops = Array.from({length: 5}, (_, index) => ({
      id: `loop-${index}`, name: `Automation ${index}`, enabled: true, status: "idle", next_run: null,
    }));
    snapshot = {...good, swarm: {...good.swarm, mission_outputs: []},
                automations: {loops, pending_approvals: [], connectors: []}};
    await refresh();
    check(element("monitor-running-count").textContent === "0 active", "Idle automations appeared to be running");
    check(element("monitor-automation-summary").textContent.replace(/\u00a0/g, " ") === "5 automations enabled · 0 scheduled",
          "Enabled and scheduled automation counts were not distinguished");
    loops[0].status = "running";
    loops[1].status = "waiting_approval";
    loops[2].next_run = Date.now() / 1000 + 3600;
    snapshot.automations.pending_approvals = [{loop_id: "loop-1", pending_approval: {reason: "Approval required"}}];
    element("monitor-attention-card").open = true;
    await refresh();
    check(element("monitor-running-count").textContent === "1 active" &&
          element("monitor-running").textContent.includes("Automation 0"), "A running automation was not shown");
    check(element("monitor-automation-summary").textContent.replace(/\u00a0/g, " ") === "5 automations enabled · 1 scheduled",
          "The next scheduled automation was not counted");
    check(element("monitor-attention-count").textContent === "1" &&
          element("monitor-attention").textContent.includes("Automation 1"), "Automation approval was not shown");
    check(element("monitor-attention-card").open, "Refreshing closed a section opened by the operator");
    element("monitor-attention").querySelector('[data-monitor-automation="attention"]').click();
    check(element("view-automations").classList.contains("active"), "Automation approval did not open Automations");
    window.__openView("swarm");
    snapshot = good;
    await refresh();
    check(element("monitor-automation-summary").textContent === "Automation status unavailable",
          "Missing automation status was reported as an empty list");
    const card = element("topo-svg").querySelector(".svg-node-clickable");
    check(card, "The worker graph did not render");
    card.dispatchEvent(new MouseEvent("click", {bubbles: true}));
    check(element("topo-svg").querySelector('.dag-card[stroke-width="2.4"]'), "Node selection did not render");

    const graph = element("topo-svg");
    element("btn-swarm-a2a-edit-mode").click();
    const theme = element("topo-sprite-theme");
    const backdrop = element("topo-backdrop");
    const motion = element("topo-motion");
    const select = (control, value) => {
      control.value = value;
      control.dispatchEvent(new Event("change"));
    };
    select(theme, "christmas");
    select(motion, "paused");
    check(graph.dataset.spriteTheme === "christmas", "The graph theme did not switch");
    check(graph.animationsPaused(), "The graph motion did not pause");
    const workerAnimation = () => graph.querySelector('[onclick*="test-worker"] .topo-sprite-body animateTransform');
    check(workerAnimation()?.ownerSVGElement === graph, "The worker sprite must use the live graph's clock");
    graph.setCurrentTime(4.2);
    const graphTime = graph.getCurrentTime();
    await refresh();
    check(graph.getCurrentTime() === graphTime, "Snapshot refresh advanced the paused clock");
    check(workerAnimation()?.ownerSVGElement === graph, "Snapshot refresh detached the sprite from the graph's clock");

    const links = element("topo-link-style");
    const speed = element("topo-link-speed");
    window.__inspectTopoNode("", "main");
    window.__inspectTopoNode("sabrina", "main");
    window.__inspectTopoNode(worker.id, "main");
    const linkPacket = () => graph.querySelector("#topo-a2a-overlay [data-packet-style]");
    check(linkPacket()?.dataset.packetStyle === "snow_bridge", "Christmas did not use the snow bridge");
    check(graph.querySelector("#topo-a2a-overlay .snow-bridge") && linkPacket().querySelector("image"),
          "Snow bridge or walking sprite did not render");
    select(backdrop, "workshop");
    check(graph.dataset.backdrop === "workshop" && graph.dataset.spriteTheme === "christmas" &&
          getComputedStyle(graph).backgroundImage.includes("data:image/jpeg;base64,"),
          "Workshop backdrop did not apply");
    check(linkPacket()?.querySelector(".workshop-parcel"), "A2A preview did not display a parcel");
    check(!graph.querySelector(".workshop-bench"), "Standby agents appeared to be working");
    const linkDuration = () => parseFloat(linkPacket().querySelector("animateMotion").getAttribute("dur"));
    const normalDuration = linkDuration();
    select(speed, "fast");
    check(linkDuration() < normalDuration, "Link speed did not affect the A2A preview");
    select(links, "still");
    check(!linkPacket() && graph.querySelector("#topo-a2a-overlay path"), "Still removed the link or left its packet animated");
    check(speed.disabled && workerAnimation(), "Still disabled the character animation or left the speed control enabled");
    select(links, "pulse");
    check(!speed.disabled && linkPacket()?.dataset.packetStyle === "pulse", "Pulses did not override the theme");
    check(graph.animationsPaused(), "Updating link appearance resumed global motion");
    element("btn-swarm-a2a-close").click();

    const packing = {id:"packing", role:"executor", status:"WORKING", current_step:"wrapping deliverable"};
    const latestFile = {path:"gift.txt", worker_id:packing.id, status:"CREATED", ts:Date.now() / 1000};
    snapshot = {...good, swarm:{...good.swarm, workers:[packing]},
                workspace:{activity:{changes:[latestFile]}}};
    select(links, "theme");
    await refresh();
    check(graph.querySelector('[onclick*="packing"] [data-workshop-task="pack"]'),
          "The working agent did not animate at the packing bench");
    check(graph.querySelector('[onclick*="gift.txt"] .workshop-parcel'),
          "The worker's deliverable did not appear as a parcel");
    const deliveryAnimation = () => graph.querySelector(".edge-file-created + .snow-bridge + [data-packet-style] .workshop-parcel");
    check(deliveryAnimation(), "The worker's recent file did not travel as a parcel");
    packing.status = "DONE";
    await refresh();
    check(!graph.querySelector(".workshop-bench") && deliveryAnimation(),
          "A completed agent kept working or its recent delivery disappeared");
    latestFile.ts -= 60;
    await refresh();
    check(!deliveryAnimation(), "An older file still appeared to be in delivery");

    select(backdrop, "standard");
    check(graph.dataset.backdrop === "standard" && !getComputedStyle(graph).backgroundImage.includes("data:image/jpeg;base64,"),
          "Standard did not restore the usual background");
    select(theme, "classic");
    check(!graph.querySelector(".topo-agent-sprite") && graph.querySelector(".dag-card"), "Classic did not restore plain cards");
    select(motion, "on");
    check(!graph.animationsPaused(), "The graph motion did not resume");
    snapshot = good;

    const broken = {get nodes() { throw new ReferenceError("test graph error"); }};
    snapshot = {...good, swarm: broken};
    await refresh();
    check(element("debug-feed").textContent.includes("test graph error"), "Render error was lost");
    const renderKeptConnection = element("api-status").textContent.includes("Connected");
    snapshot = {...snapshot, ui_dirty: true};
    await refresh();
    check(reloads === 0, "A changed file reloaded the UI without opt-in");
    snapshot = {...snapshot, ui_auto_reload: true};
    await refresh();
    await nextTurn();
    check(reloads === 1, "The opt-in reload did not recover a render error");
    check(renderKeptConnection, "A render error was reported as a lost API connection");
    snapshot = good;
    await refresh();
    check(element("sync-time").textContent.includes("test time"), "Polling did not recover");
    check(element("swarm-fleet-badge").textContent.startsWith("0 Active"), "Finished workers counted as active");
    check(element("topo-web-metrics").textContent.includes("0 active workers"), "Graph still counted finished workers");
    check(element("live-mission-banner").style.display === "none", "Finished mission appeared to be running");
    check(element("debug-mission-status").textContent.includes("FAILED"), "Last mission failure was hidden");

    const failedParent = {id: "executor-1", name: "Execution Specialist", role: "execution_specialist",
                          mission_id: "recovery-mission", status: "FAILED", state: "FAILED"};
    const recovery = {id: "recovery-1", name: "Recovery 1", role: "recovery", mission_id: "recovery-mission",
                      status: "WORKING", current_step: "recovering", recovery_for: failedParent.id,
                      recovery_role: failedParent.role, initial_error: "Executor timed out", parent_id: failedParent.id};
    snapshot = {...good, swarm: {workers: [failedParent, recovery], active_worker_count: 1,
                                 active_missions: [{mission_id: "recovery-mission", state: "ACTIVE (RUNNING)", goal: "Sample"}]}};
    await refresh();
    const branch = window.__sabrinaCollectiveApi.getLiveDag().edges.find(edge => edge.rule_id === "RECOVERY");
    check(branch?.from === failedParent.id && branch?.to === recovery.id, "Recovery is detached from the failed worker");
    check(element("topo-svg").textContent.includes("RECOVERY · recovering"), "Recovery edge wasn't labeled");
    check(element("lmb-stepper").textContent.includes("RECOVERING"), "Executor recovery wasn't visible");
    recovery.status = "DONE";
    await refresh();
    check(element("lmb-stepper").textContent.includes("RECOVERED"), "Recovery didn't update the stage");

    const interrupted = {mission_id:"m-12345678", state:"INTERRUPTED", goal:"Saved mission",
                         summary:"The runner exited before recording a final status."};
    snapshot = {...good, swarm:{...good.swarm, saved_run_attention:[interrupted]}};
    api.get_mission_output = async id => ({ok:id === interrupted.mission_id, output:interrupted});
    await refresh();
    const savedRunButton = element("monitor-attention").querySelector('[data-monitor-run="m-12345678"]');
    check(savedRunButton, "Interrupted mission was missing from Needs Attention");
    savedRunButton.click();
    await nextTurn();
    check(element("mission-output-body").textContent.includes(interrupted.summary), "Saved status did not open");

    api.get_snapshot = async () => { throw new Error("test bridge error"); };
    await refresh();
    check(element("api-status").textContent.includes("test bridge error"), "Bridge error was lost");
    element("btn-debug-check").click();
    await nextTurn();
    check(JSON.parse(element("debug-schema-output").textContent).status === "UNAVAILABLE",
          "Unavailable health check reported success");
    element("snapshot-test-result").textContent = "passed";
  } catch (error) {
    element("snapshot-test-result").textContent = String(error);
  }
})();
</script>
"""


class TestSnapshotUI(unittest.TestCase):
    def test_render_selection_and_poll_recovery(self):
        browser = find_browser()
        if not browser:
            self.skipTest("Requires an installed Edge or Chromium browser")
        template = Path(__file__).with_name("sabrina_desktop_ui.html").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = root / "console-test.html"
            page.write_text(template.replace("</body>", PROBE + "</body>"), encoding="utf-8")
            result = subprocess.run(
                [browser, "--headless", "--disable-gpu", "--disable-extensions", "--no-first-run",
                 "--disable-background-networking", f"--user-data-dir={root / 'profile'}",
                 "--virtual-time-budget=1500", "--dump-dom", page.as_uri()],
                capture_output=True, encoding="utf-8", errors="replace", timeout=30, check=True,
            )
            match = re.search(r'<output id="snapshot-test-result">([^<]*)</output>', result.stdout)
            self.assertIsNotNone(match, "Browser did not return the test page")
            self.assertEqual(unescape(match.group(1)), "passed")


if __name__ == "__main__":
    unittest.main()
