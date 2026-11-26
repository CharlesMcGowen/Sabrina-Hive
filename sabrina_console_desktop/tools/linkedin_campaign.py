"""Start/stop/status controller for supervised LinkedIn campaigns.

This is deliberately a thin layer over Sabrina's existing coordinator:
objective cancellation remains authoritative, and stopping also reconciles the
LinkedIn browser lease so child agents cannot continue using the desktop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.control_client import configured

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = Path(os.getenv("SABRINA_LINKEDIN_STATE", ROOT / "data/linkedin_campaign.json"))
RESOURCE = os.getenv("SABRINA_LINKEDIN_RESOURCE", "browser:linkedin-primary")


def _read() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"state": "stopped", "task_id": None, "campaign_id": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _write(value: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def start(goal: str, *, context: str = "", client: str = "operator", project: str = "linkedin", tier: str = "fast", action_policy: dict[str, Any] | None = None) -> dict[str, Any]:
    current = _read()
    if current.get("state") == "running":
        return {"ok": False, "error": "already_running", "campaign": current}
    control = configured()
    if control is None:
        return {"ok": False, "error": "coordinator_unconfigured", "message": "configure SABRINA_CONTROL_URL/token first"}
    campaign_id = uuid.uuid4().hex
    policy = action_policy or {
        "observe": True,
        "draft": True,
        "send": False,
        "connect": False,
        "like": False,
        "comment": False,
    }
    envelope = {
        "campaign_id": campaign_id,
        "platform": "linkedin",
        "resource": RESOURCE,
        "action_policy": policy,
        "stop_control": "operator_cancel_or_lease_loss",
    }
    receipt = control.submit(goal=goal, context=json.dumps({"campaign": envelope, "context": context}), client=client, project=project, tier=tier)
    task_id = receipt.get("id") or receipt.get("task_id")
    if not task_id:
        return {"ok": False, "error": "missing_task_id", "receipt": receipt}
    state = {
        "state": "running",
        "campaign_id": campaign_id,
        "task_id": task_id,
        "client": client,
        "project": project,
        "goal": goal,
        "action_policy": policy,
        "resource": RESOURCE,
        "started_at": time.time(),
        "coordinator_receipt": receipt,
    }
    _write(state)
    return {"ok": True, "campaign": state}


def stop(*, reason: str = "operator_stop") -> dict[str, Any]:
    current = _read()
    control = configured()
    if control is None:
        return {"ok": False, "error": "coordinator_unconfigured"}
    results: dict[str, Any] = {}
    task_id = current.get("task_id")
    if task_id:
        try:
            results["cancel"] = control.cancel(task_id)
        except Exception as exc:
            results["cancel_error"] = type(exc).__name__
    try:
        results["lease_reconcile"] = control.request("/v1/leases/reconcile", {"resource": current.get("resource", RESOURCE), "confirmed_stopped": True})
    except Exception as exc:
        results["lease_reconcile_error"] = type(exc).__name__
    current.update({"state": "stopped", "stopped_at": time.time(), "stop_reason": reason, "stop_results": results})
    _write(current)
    return {"ok": True, "campaign": current}


def status() -> dict[str, Any]:
    current = _read()
    control = configured()
    if control and current.get("task_id"):
        try:
            current["coordinator"] = control.status(current["task_id"])
        except Exception as exc:
            current["coordinator_error"] = type(exc).__name__
    return {"ok": True, "campaign": current}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start")
    p_start.add_argument("goal")
    p_start.add_argument("--context", default="")
    p_start.add_argument("--client", default="operator")
    p_start.add_argument("--project", default="linkedin")
    p_start.add_argument("--tier", default="fast")
    p_stop = sub.add_parser("stop")
    p_stop.add_argument("--reason", default="operator_stop")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "start":
        result = start(args.goal, context=args.context, client=args.client, project=args.project, tier=args.tier)
    elif args.command == "stop":
        result = stop(reason=args.reason)
    else:
        result = status()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
