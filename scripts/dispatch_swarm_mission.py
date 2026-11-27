import sys
import os
from pathlib import Path

# Add sabrina_console_desktop to path
ROOT = Path("sabrina_console_desktop").resolve()
sys.path.insert(0, str(ROOT))

from live_swarm_runner import LiveSwarmRunner

runner = LiveSwarmRunner(
    workspace_root=Path(".").resolve(),
    artifacts_dir=ROOT / "artifacts",
)

mission_goal = "Evaluate Kraken cheap trading execution: analyze 24h TREAD/USD spread hurdles, enforce post-only maker fills, and synthesize dynamic take-profit and dip re-entry skills"
print(f"Launching mission on LiveSwarmRunner: '{mission_goal}'...")
res = runner.launch_mission(mission_goal, coordinator_count=1, workers_per_coordinator=3)
print("Launch response:", res)

if res.get("ok"):
    mission_id = res["mission_id"]
    print(f"Mission started with ID: {mission_id}. Waiting for completion...")
    out = runner.wait_for_mission(mission_id, timeout=25.0)
    print("Mission outcome state:", out.get("state") if out else "Timeout")
    if out:
        print("Summary:", out.get("summary", "")[:300])
        print("Verdict:", out.get("verdict"))
