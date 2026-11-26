"""End-to-end verification script for Live Telemetry Graph Node binding.

Verifies:
1. Triggering dynamic worker enters ACTIVE or VALIDATING with recorded command.
2. Telemetry Blackboard (dnd_rsi_blackboard.sqlite3) indexes record by agent_id with dyn-<role>-<seq>.
3. Operational fields: state, mode (DYNAMIC), logprob, gate_status, active_command, parent_id, depth, action_trail.
4. Operator chat history is completely separate and unmixed.
5. DOM / HTML bindings for node pill, compact command snippet, and inspector view.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Add project root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from sabrina_console_desktop.tools.telemetry_blackboard import TelemetryBlackboard, get_blackboard_db_path


def verify_telemetry_flow() -> None:
    print("[1/5] Setting up isolated verification environment...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        db_path = tmp_root / "dnd_rsi_blackboard.sqlite3"
        ws_path = tmp_root / "workspace"
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "auth.py").write_text("def authenticate(): pass\n", encoding="utf-8")
        
        # Override env var so all telemetry uses our test blackboard
        os.environ["SABRINA_DND_BLACKBOARD_DB"] = str(db_path)
        
        bridge = SabrinaDesktopBridge(
            role_bindings_path=tmp_root / "role_bindings.json",
            vault_path=tmp_root / "provider_vault.json",
            custom_models_path=tmp_root / "custom_models.json",
            swarm_artifacts_dir=tmp_root / "artifacts",
            ui_html_path=tmp_root / "ui.html",
        )
        bridge.swarm_runner.workspace_root = ws_path

        # Simulate operator chatting beforehand
        bridge.chat_log.append({
            "sender": "operator",
            "text": "OPERATOR_SECRET_PROMPT: Please inspect auth subsystem now.",
            "time": "14:30:00",
        })

        print("[2/5] Triggering dynamic sub-agent worker spawn...")
        spawn_res = bridge.swarm_runner.spawn_worker(
            name="Security Scout",
            role="research_scout",
            task='rg "auth" src/',
            model="gemini-2.5-pro",
        )
        assert spawn_res["ok"], f"Spawn failed: {spawn_res}"
        worker_id = spawn_res["worker_id"]
        print(f"      Spawned dynamic worker: {worker_id}")
        assert worker_id.startswith("dyn-scout-"), f"Worker ID format mismatch: {worker_id}"

        # Allow worker thread a brief tick
        time.sleep(0.1)

        print("[3/5] Inspecting Telemetry Blackboard in SQLite...")
        bb = TelemetryBlackboard(db_path)
        all_tel = bb.get_latest_telemetry_by_agent()
        assert worker_id in all_tel, f"{worker_id} not found in blackboard: {list(all_tel.keys())}"
        rec = all_tel[worker_id]
        print(f"      Blackboard record for {worker_id}:")
        print(f"        State: {rec['state']}")
        print(f"        Mode: {rec['mode']}")
        print(f"        Command: {rec['command']}")
        print(f"        Gate Status: {rec['gate_status']}")
        print(f"        Logprob: {rec['logprob']}")
        print(f"        Action trail count: {len(rec['action_trail'])}")

        assert rec["mode"] == "DYNAMIC", f"Mode must be DYNAMIC, got {rec['mode']}"
        assert rec["state"] in ("SPAWNING", "ACTIVE", "VALIDATING", "PASS"), f"Unexpected state {rec['state']}"
        assert rec["command"] == 'rg "auth" src/'
        assert rec["parent_id"] == "sabrina"

        print("[4/5] Inspecting Bridge Snapshot binding...")
        snap = bridge.get_snapshot()
        assert "agent_telemetry" in snap
        assert worker_id in snap["agent_telemetry"]
        w_snap = snap["agent_telemetry"][worker_id]
        
        # Verify operational fields on worker in snapshot
        assert w_snap["agent_id"] == worker_id
        assert w_snap["mode"] == "DYNAMIC"
        assert w_snap["state"] in ("SPAWNING", "ACTIVE", "VALIDATING", "PASS")
        assert w_snap["command"] == 'rg "auth" src/'
        assert w_snap["active_command"] == 'rg "auth" src/'
        assert w_snap["parent_id"] == "sabrina"
        assert w_snap["depth"] == 1
        assert len(w_snap["action_trail"]) >= 1

        # Check council seeded nodes
        council_nodes = snap["swarm"]["nodes"]
        for c_id in ("sabrina", "codex", "antigravity", "claude"):
            assert c_id in council_nodes, f"Council node {c_id} missing"
            cn = council_nodes[c_id]
            assert cn["mode"] == "SEEDED", f"{c_id} mode should be SEEDED, got {cn['mode']}"
            assert "active_command" in cn
            assert "gate_status" in cn

        # Verify strict separation from operator chat
        print("[5/5] Verifying strict chat isolation and UI bindings...")
        for trail_item in w_snap["action_trail"]:
            trail_str = json.dumps(trail_item)
            assert "OPERATOR_SECRET_PROMPT" not in trail_str, "Operator chat leaked into action trail!"
        
        for n in council_nodes.values():
            assert "OPERATOR_SECRET_PROMPT" not in str(n.get("command") or "")
            assert "OPERATOR_SECRET_PROMPT" not in str(n.get("task") or "")

        # Check operator chat remained in chat_history
        assert len(bridge.chat_log) == 1
        assert bridge.chat_log[0]["text"] == "OPERATOR_SECRET_PROMPT: Please inspect auth subsystem now."

        # Verify HTML has both copies synced and contains inspector IDs
        html_desktop = (REPO_ROOT / "sabrina_console_desktop" / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        html_root = (REPO_ROOT / "sabrina_desktop_ui.html").read_text(encoding="utf-8")
        assert html_desktop == html_root, "sabrina_desktop_ui.html copies are out of sync!"
        
        for required_token in (
            "topo-inspect-state",
            "topo-inspect-mode",
            "topo-inspect-command",
            "topo-inspect-lineage",
            "topo-inspect-trail",
            "cmdSnippet",
            "modeBadgeText",
        ):
            assert required_token in html_desktop, f"HTML missing {required_token}"

        bridge.swarm_runner.shutdown()
        print("\nAll verification checks PASSED successfully!")


if __name__ == "__main__":
    verify_telemetry_flow()
