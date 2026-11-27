"""Run an operator-approved, bounded Finale chain independently of the desktop."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

MODE = "codex_thought_antigravity_implement"
IN_PROGRESS = {"QUEUED", "RUNNING"}
PASSED = {"PASS", "DONE", "COMPLETED"}
IN_FLIGHT = {"SPAWNING", "QUEUED", "WORKING", "RUNNING", "ACTIVE", "VERIFYING", "VALIDATING"}
STALE_SECONDS = 45


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path, data):
    temp = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _record(job, record, **changes):
    record.update(changes, updated_at=time.time())
    _save(job / "state.json", record)


class FollowupCoordinator:
    def __init__(self, root):
        self.root = Path(root)

    def submit(self, goal, workspace, cycles, allow_writes=False):
        if type(cycles) is not int or not 1 <= cycles <= 3 or not str(goal).strip():
            return {"ok": False, "error": "Choose one to three follow-ups and enter a mission goal.",
                    "execution_authorized": False}
        job = self.root / f"chain-{uuid.uuid4().hex}"
        try:
            workspace = Path(workspace).resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("Attach a workspace directory before launching.")
            job.mkdir(parents=True, exist_ok=False)
            request = {"chain_id": job.name, "goal": str(goal).strip(), "workspace": str(workspace),
                       "cycles": cycles, "allow_initial_writes": bool(allow_writes)}
            _save(job / "request.json", request)
            state = {"chain_id": job.name, "workspace": str(workspace), "goal": request["goal"],
                     "state": "QUEUED", "remaining": cycles, "runs": [], "workers": [],
                     "current_mission_id": "", "detail": "Starting the background coordinator."}
            _record(job, state)
            command = ([sys.executable, "--followup-coordinator", str(job)] if getattr(sys, "frozen", False)
                       else [sys.executable, str(Path(__file__).resolve()), str(job)])
            detached = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
                        if os.name == "nt" else {"start_new_session": True})
            subprocess.Popen(command, cwd=Path(__file__).resolve().parent, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached)
        except (OSError, ValueError) as exc:
            if (job / "state.json").is_file():
                _record(job, state, state="FAILED", detail=str(exc))
            return {"ok": False, "error": f"Could not start the background coordinator: {exc}",
                    "execution_authorized": False}
        return {"ok": True, "chain_id": job.name, "state": "QUEUED",
                "message": f"Background mission queued with up to {cycles} follow-up(s).",
                "execution_authorized": False}

    def snapshot(self):
        rows = []
        for path in self.root.glob("chain-*/state.json"):
            row = _read(path)
            if not isinstance(row, dict) or not row.get("chain_id"):
                continue
            if row.get("state") in IN_PROGRESS and time.time() - row.get("updated_at", 0) > STALE_SECONDS:
                row.update(state="INTERRUPTED", workers=[], detail="Coordinator heartbeat expired. Review this run before retrying.")
            rows.append(row)
        return sorted(rows, key=lambda row: row.get("updated_at", 0), reverse=True)

    def stop(self, mission_id=None):
        stopped = []
        for row in self.snapshot():
            chain_id = row["chain_id"]
            if (not re.fullmatch(r"chain-[a-f0-9]{32}", chain_id)
                    or row["state"] not in IN_PROGRESS | {"INTERRUPTED"}):
                continue
            if mission_id and mission_id not in {chain_id, row.get("current_mission_id")}:
                continue
            (self.root / chain_id / "stop").touch(exist_ok=True)
            stopped.append(chain_id)
        return stopped


def _run_summary(output):
    return {key: output.get(key) for key in (
        "mission_id", "parent_mission_id", "goal", "state", "summary", "error",
        "artifact_path", "completed_at", "updated_str",
    )}


def _wait_for_run(job, runner, state, mission_id):
    while True:
        if (job / "stop").exists():
            runner.stop_mission(mission_id)
            return None
        output = runner.wait_for_mission(mission_id, timeout=0.7) or {}
        if output.get("state") and output["state"] != "ACTIVE (RUNNING)":
            return output
        snapshot = runner.get_state()
        workers = [w for w in snapshot.get("workers", [])
                   if w.get("mission_id") == mission_id and str(w.get("status", "")).upper() in IN_FLIGHT]
        _record(job, state, workers=workers)


def run_chain(job, create_runner):
    job = Path(job).resolve(strict=True)
    request, state = _read(job / "request.json"), _read(job / "state.json")
    if request.get("chain_id") != job.name or state.get("state") != "QUEUED":
        return
    try:
        with (job / "claimed").open("x"):
            pass
    except FileExistsError:
        return
    try:
        cycles = request["cycles"]
        if type(cycles) is not int or not 1 <= cycles <= 3:
            raise ValueError("Invalid follow-up limit.")
        runner = create_runner(Path(request["workspace"]))
        goal, parent = request["goal"], ""
        for step in range(cycles + 1):
            if (job / "stop").exists():
                _record(job, state, state="STOPPED", workers=[], detail="Stopped by the operator.")
                return
            launched = runner.launch_mission(
                goal, mode=MODE, parent_mission_id=parent,
                allow_workspace_writes=bool(request.get("allow_initial_writes")) if step == 0 else False,
            )
            if not launched.get("ok") or not launched.get("mission_id"):
                raise ValueError(launched.get("error") or "Coordinator could not launch the mission.")
            mid = launched["mission_id"]
            _record(job, state, state="RUNNING", current_mission_id=mid, current_goal=goal,
                    remaining=cycles - step, detail="Running in the background.")
            output = _wait_for_run(job, runner, state, mid)
            if output is None or (job / "stop").exists():
                _record(job, state, state="STOPPED", workers=[], detail="Stopped by the operator.")
                return
            state["runs"].append(_run_summary(output))
            if output.get("state") not in PASSED or (output.get("audit_decision") or {}).get("needs_more_work"):
                _record(job, state, state="ATTENTION", workers=[],
                        detail=output.get("error") or f"{mid} ended {output.get('state') or 'without a verdict'}.")
                return
            if step == cycles:
                _record(job, state, state="COMPLETED", workers=[], detail="Requested follow-ups completed.")
                return
            draft = runner.get_next_step_draft(mid)
            evidence = draft.get("evidence") or {}
            if (not draft.get("ok") or not evidence.get("recommendation") or evidence.get("conflicts")
                    or not draft.get("prompt")):
                _record(job, state, state="ATTENTION", workers=[], detail="Review the parent report before continuing.")
                return
            parent = mid
            goal = draft["prompt"] + "\nStay within the original mission scope. This automatic run has read-only workspace access."
            _record(job, state, state="QUEUED", workers=[], detail="Verified follow-up queued.")
    except Exception as exc:
        _record(job, state, state="FAILED", workers=[], detail=str(exc)[:600])


def run_worker(job):
    def create_runner(workspace):
        from sabrina_desktop_ui import SabrinaDesktopBridge

        bridge = SabrinaDesktopBridge(coordinator_only=True)
        bridge.swarm_runner.set_workspace_root(workspace)
        return bridge.swarm_runner

    run_chain(job, create_runner)


if __name__ == "__main__":
    run_worker(sys.argv[1])

