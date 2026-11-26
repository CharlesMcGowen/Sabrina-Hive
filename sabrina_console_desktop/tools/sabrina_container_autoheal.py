#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Container Auto-Heal Watchdog (`tools/sabrina_container_autoheal.py`)
===========================================================================
Autonomous watchdog supervisor that monitors Sabrina container healthchecks
and automatically recovers unhealthy or unexpectedly stopped containers
via their native systemd --user service units.

Key Guardrails:
  1. Rate-Limited Recovery: Max 3 restarts within 300s per service to prevent crashloops.
  2. Audit Logging: Records recovery events to data/container_autoheal_events.jsonl.
  3. Clean Systemd Delegation: Restarts via `systemctl --user restart <unit>` ensuring
     graceful stop, resource cleanup, and clean container rebuild.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "data" / "container_autoheal_events.jsonl"

# Container name -> Systemd unit mapping for all Sabrina daemon microservices
CONTAINER_TO_UNIT: Dict[str, str] = {
    "sabrina-kraken-ws-recorder": "sabrina-kraken-ws-recorder.service",
    "sabrina-kraken-paper-1000": "sabrina-kraken-paper-1000.service",
    "sabrina-kraken-execution-v1": "sabrina-kraken-execution-v1.service",
    "sabrina-kraken-execution-v1-acct2": "sabrina-kraken-execution-v1-acct2.service",
    "sabrina-kraken-strategy-lab": "sabrina-kraken-strategy-lab.service",
    "sabrina-kraken-tripwires": "sabrina-kraken-tripwires.service",
    "sabrina-kraken-wallet-observer": "sabrina-kraken-wallet-observer.service",
    "sabrina-lab-coordinator": "sabrina-lab-coordinator.service",
    "sabrina-customer-scheduler": "sabrina-customer-scheduler.service",
    "sabrina-social-api": "sabrina-social-api.service",
    "sabrina-social-pulse": "sabrina-social-pulse.service",
    "sabrina-social-scheduler": "sabrina-social-scheduler.service",
    "sabrina-content-research": "sabrina-content-research.service",
    "sabrina-architecture-research": "sabrina-architecture-research.service",
    "sabrina-layer-research": "sabrina-layer-research.service",
    "sabrina-model-research": "sabrina-model-research.service",
    "sabrina-printed-math-proof-v1": "sabrina-printed-math-proof-v1.service",
    "sabrina-recruited-ssm": "sabrina-recruited-ssm.service",
    "sabrina-recruited-systems": "sabrina-recruited-systems.service",
}

# In-memory restart tracking: unit -> list of timestamps
_RESTART_HISTORY: Dict[str, List[float]] = defaultdict(list)
MAX_RESTARTS_PER_WINDOW = 3
WINDOW_SECONDS = 300.0


def log_heal_event(cname: str, unit: str, reason: str, action: str, details: Optional[Dict[str, Any]] = None) -> None:
    event = {
        "timestamp": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "container": cname,
        "unit": unit,
        "reason": reason,
        "action": action,
        "details": details or {},
    }
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as exc:
        sys.stderr.write(f"Failed to write autoheal log: {exc}\n")


def is_rate_limited(unit: str) -> bool:
    now = time.time()
    history = [t for t in _RESTART_HISTORY[unit] if now - t < WINDOW_SECONDS]
    _RESTART_HISTORY[unit] = history
    return len(history) >= MAX_RESTARTS_PER_WINDOW


def record_restart(unit: str) -> None:
    _RESTART_HISTORY[unit].append(time.time())


def get_docker_health_statuses() -> List[Dict[str, Any]]:
    """Inspects running sabrina containers and extracts health status and log output."""
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=sabrina-", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except Exception as exc:
        sys.stderr.write(f"Failed to query docker ps: {exc}\n")
        return []

    containers = []
    for line in proc.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            containers.append(data)
        except Exception:
            pass

    results = []
    for c in containers:
        cname = c.get("Names", "").strip()
        cid = c.get("ID", "").strip()
        status_str = c.get("Status", "").strip()

        # Detailed inspect for health
        health_state = "none"
        last_log = ""
        try:
            insp = subprocess.run(
                ["docker", "inspect", cid, "--format", "{{json .State}}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            state_data = json.loads(insp.stdout.strip())
            health_info = state_data.get("Health") or {}
            health_state = health_info.get("Status", "none")
            log_entries = health_info.get("Log") or []
            if log_entries:
                last_log = log_entries[-1].get("Output", "").strip()
        except Exception:
            pass

        results.append({
            "id": cid,
            "name": cname,
            "status_str": status_str,
            "health": health_state,
            "last_log": last_log,
        })
    return results


def restart_unit(unit: str, cname: str, reason: str, last_log: str = "") -> bool:
    if is_rate_limited(unit):
        sys.stderr.write(f"[AUTOHEAL] Skipping restart for {unit} (rate-limited: {MAX_RESTARTS_PER_WINDOW} restarts in {WINDOW_SECONDS}s)\n")
        log_heal_event(cname, unit, reason, "skipped_rate_limited", {"last_log": last_log})
        return False

    sys.stdout.write(f"[AUTOHEAL] Restarting {unit} (container: {cname}, reason: {reason})...\n")
    try:
        subprocess.run(["systemctl", "--user", "restart", unit], check=True, timeout=15)
        record_restart(unit)
        log_heal_event(cname, unit, reason, "restarted", {"last_log": last_log})
        return True
    except Exception as exc:
        sys.stderr.write(f"[AUTOHEAL] Failed to restart {unit}: {exc}\n")
        log_heal_event(cname, unit, reason, "restart_failed", {"error": str(exc)})
        return False


def run_autoheal_cycle() -> Dict[str, Any]:
    statuses = get_docker_health_statuses()
    restarted: List[str] = []
    unhealthy: List[str] = []

    for item in statuses:
        cname = item["name"]
        unit = CONTAINER_TO_UNIT.get(cname)
        if not unit:
            continue

        # Check 1: Explicitly unhealthy
        if item["health"] == "unhealthy":
            unhealthy.append(cname)
            if restart_unit(unit, cname, "healthcheck_unhealthy", item["last_log"]):
                restarted.append(unit)

        # Check 2: Exited continuous container
        elif "Exited" in item["status_str"]:
            if restart_unit(unit, cname, f"container_exited ({item['status_str']})", item["last_log"]):
                restarted.append(unit)

    return {
        "audited_containers": len(statuses),
        "unhealthy_containers": unhealthy,
        "restarted_units": restarted,
        "timestamp": time.time(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Container Auto-Heal Watchdog")
    parser.add_argument("--once", action="store_true", help="Run a single audit and heal pass")
    parser.add_argument("--interval", type=int, default=30, help="Interval in seconds for continuous watchdog loop")
    args = parser.parse_args()

    if args.once:
        res = run_autoheal_cycle()
        print(json.dumps(res, indent=2))
        return

    sys.stdout.write(f"[AUTOHEAL] Starting continuous watchdog loop (interval: {args.interval}s)...\n")
    while True:
        try:
            run_autoheal_cycle()
        except Exception as exc:
            sys.stderr.write(f"[AUTOHEAL] Cycle error: {exc}\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
