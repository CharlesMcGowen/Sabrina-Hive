"""Exercise the detached Finale handoff with isolated, deterministic mission results."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from followup_coordinator import FollowupCoordinator, run_chain


class FixtureRunner:
    def __init__(self, workspace, *, first_delay=0, passed=True):
        self.workspace_root = workspace
        self.first_delay = first_delay
        self.passed = passed
        self.launches = []
        self.current = {}

    def launch_mission(self, goal, **options):
        self.launches.append(options)
        mid = f"m-{len(self.launches):08x}"
        self.current = {
            "mission_id": mid, "parent_mission_id": options["parent_mission_id"],
            "goal": goal, "state": "ACTIVE (RUNNING)",
        }
        self.until = time.monotonic() + (self.first_delay if len(self.launches) == 1 else 0)
        return {"ok": True, "mission_id": mid}

    def wait_for_mission(self, mission_id, timeout):
        time.sleep(min(timeout, max(0, self.until - time.monotonic())))
        if time.monotonic() >= self.until:
            self.current.update(state="PASS" if self.passed else "FAILED", completed_at=time.time(),
                                summary="Fixture completed.", execution_authorized=False)
        return self.current

    def get_state(self):
        return {"workers": [{
            "id": "fixture-worker", "mission_id": self.current["mission_id"], "status": "WORKING",
            "name": "Fixture worker", "role": "research_scout",
        }]}

    def get_next_step_draft(self, mid):
        return {"ok": True, "prompt": f"Review the report from {mid}.",
                "evidence": {"recommendation": "Review the report.", "conflicts": []}}

    def stop_mission(self, mid):
        self.current["state"] = "STOPPED"


def _detached_fixture_command(command):
    return [command[0], str(Path(__file__).resolve()), "--fixture-worker", command[-1]]


class FollowupCoordinatorTests(unittest.TestCase):
    def test_persisted_chain_stops_on_failed_gate(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            coordinator = FollowupCoordinator(path / "followups")
            with patch("followup_coordinator.subprocess.Popen"):
                result = coordinator.submit("Review a fixture", path, 2)
            runner = FixtureRunner(path, passed=False)
            job = coordinator.root / result["chain_id"]
            run_chain(job, lambda workspace: runner)
            run_chain(job, lambda workspace: self.fail("Completed chain must not launch twice"))
            self.assertEqual(coordinator.snapshot()[0]["state"], "ATTENTION")
            self.assertEqual(len(runner.launches), 1)

    def test_followups_keep_parentage_and_drop_write_permission(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            coordinator = FollowupCoordinator(path / "followups")
            with patch("followup_coordinator.subprocess.Popen"):
                result = coordinator.submit("Review a fixture", path, 2, allow_writes=True)
            runner = FixtureRunner(path)
            run_chain(coordinator.root / result["chain_id"], lambda workspace: runner)
            row = coordinator.snapshot()[0]
            self.assertEqual(row["state"], "COMPLETED")
            self.assertEqual([step["parent_mission_id"] for step in row["runs"]], ["", "m-00000001", "m-00000002"])
            self.assertEqual([opts["allow_workspace_writes"] for opts in runner.launches], [True, False, False])
            self.assertTrue(all("plugin_capabilities" not in opts for opts in runner.launches))

    def test_stop_before_launch(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            coordinator = FollowupCoordinator(path / "followups")
            with patch("followup_coordinator.subprocess.Popen"):
                result = coordinator.submit("Review a fixture", path, 1)
            job = coordinator.root / result["chain_id"]
            self.assertEqual(coordinator.stop(result["chain_id"]), [result["chain_id"]])
            runner = FixtureRunner(path)
            run_chain(job, lambda workspace: runner)
            self.assertEqual(coordinator.snapshot()[0]["state"], "STOPPED")
            self.assertEqual(runner.launches, [])

    def test_detached_chain_finishes_after_submitter_exits(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            submitter = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--fixture-submitter", str(path)],
                check=True, capture_output=True, text=True, timeout=15,
            )
            submitted = json.loads(submitter.stdout)
            self.assertTrue(submitted["ok"])
            coordinator = FollowupCoordinator(path / "followups")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and coordinator.snapshot()[0]["state"] in {"QUEUED", "RUNNING"}:
                time.sleep(0.05)
            row = coordinator.snapshot()[0]
            self.assertEqual(row["state"], "COMPLETED", row)
            self.assertEqual(len(row["runs"]), 2)
            self.assertEqual(row["runs"][1]["parent_mission_id"], row["runs"][0]["mission_id"])


if __name__ == "__main__":
    if sys.argv[1:2] == ["--fixture-worker"]:
        delay = float(os.environ.get("SABRINA_FIXTURE_MISSION_DELAY", "1.5"))
        run_chain(sys.argv[2], lambda workspace: FixtureRunner(workspace, first_delay=delay))
    elif sys.argv[1:2] == ["--fixture-submitter"]:
        root = Path(sys.argv[2])
        original = subprocess.Popen
        with patch("followup_coordinator.subprocess.Popen",
                   side_effect=lambda command, **kwargs: original(_detached_fixture_command(command), **kwargs)):
            print(json.dumps(FollowupCoordinator(root / "followups").submit("Review a fixture", root, 1)))
    else:
        unittest.main()

