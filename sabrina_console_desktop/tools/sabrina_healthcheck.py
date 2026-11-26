#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Container Healthcheck Probe (`tools/sabrina_healthcheck.py`)
===================================================================
Lightweight, zero-dependency health probe executed by Docker engine
inside running containers or directly on host.

Exits with:
  0: Healthy (service is operational and meeting heartbeat thresholds)
  1: Unhealthy (endpoint failed, heartbeat stale, or process dead)
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
import urllib.request


def check_http_endpoint(url: str, timeout: float = 4.0, expected_key: str | None = None) -> bool:
    """Verifies that an internal HTTP endpoint returns HTTP 200 within timeout."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "sabrina-healthcheck/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                sys.stderr.write(f"HTTP probe {url} returned status {resp.status}\n")
                return False
            if expected_key:
                body = resp.read(512).decode("utf-8", errors="ignore")
                if expected_key not in body:
                    sys.stderr.write(f"HTTP probe {url} missing expected key '{expected_key}'\n")
                    return False
            return True
    except Exception as exc:
        sys.stderr.write(f"HTTP probe failed for {url}: {exc}\n")
        return False


def check_file_freshness(path_str: str, max_age_seconds: float) -> bool:
    """Verifies that a state or heartbeat file exists and has been modified recently."""
    p = Path(path_str)
    if not p.exists():
        sys.stderr.write(f"Heartbeat file does not exist: {path_str}\n")
        return False
    try:
        mtime = p.stat().st_mtime
        age = time.time() - mtime
        if age > max_age_seconds:
            sys.stderr.write(f"Heartbeat file stale: {path_str} age={age:.1f}s > {max_age_seconds}s\n")
            return False
        return True
    except Exception as exc:
        sys.stderr.write(f"Failed to check freshness of {path_str}: {exc}\n")
        return False


def check_process_liveness(pid: int = 1) -> bool:
    """Verifies that the target PID inside the container namespace is alive."""
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError as exc:
        sys.stderr.write(f"Process {pid} liveness check failed: {exc}\n")
        return False


def probe_service(service_name: str) -> bool:
    name = service_name.lower().strip()
    if name.endswith(".service"):
        name = name[:-8]

    # 1. HTTP API microservices
    if name in ("sabrina-lab-coordinator", "lab-coordinator"):
        return check_http_endpoint("http://127.0.0.1:8090/health", timeout=4.0, expected_key="sabrina-lab-coordinator")

    if name in ("sabrina-social-api", "social-api"):
        return check_http_endpoint("http://127.0.0.1:8087/health", timeout=4.0)

    if name in ("sabrina-content-research", "content-research"):
        return check_http_endpoint("http://127.0.0.1:8088/health", timeout=4.0)

    # 2. Live websocket and high-frequency market stream recorder
    if name in ("sabrina-kraken-ws-recorder", "kraken-ws-recorder"):
        return check_file_freshness("/dev/shm/sabrina_kraken_ws_live.json", max_age_seconds=15.0)

    # 3. Trading simulation & wallet observer
    if name in ("sabrina-kraken-paper-1000", "kraken-paper-1000"):
        return check_file_freshness("/home/ego/.local/state/sabrina-kraken/paper-1000-v1", max_age_seconds=120.0)

    if name in ("sabrina-kraken-wallet-observer", "kraken-wallet-observer"):
        return check_file_freshness("/home/ego/.local/state/sabrina-kraken/wallet-observer-v1", max_age_seconds=120.0)

    # 4. Production live execution engines
    if name in ("sabrina-kraken-execution-v1-acct2", "kraken-execution-v1-acct2"):
        state_file = "/home/ego/.local/state/sabrina-kraken/execution-v1-acct2/last-cycle.json"
        if Path(state_file).exists():
            return check_file_freshness(state_file, max_age_seconds=360.0)
        return check_process_liveness(1)

    if name in ("sabrina-kraken-execution-v1", "kraken-execution-v1"):
        state_file = "/home/ego/.local/state/sabrina-kraken/execution-v1/last-cycle.json"
        if Path(state_file).exists():
            return check_file_freshness(state_file, max_age_seconds=360.0)
        return check_process_liveness(1)

    # 5. Default check for research workers, tripwires, schedulers
    return check_process_liveness(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Container Healthcheck Probe")
    parser.add_argument("service", help="Name of the service or container (e.g. sabrina-lab-coordinator)")
    args = parser.parse_args()

    healthy = probe_service(args.service)
    if healthy:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
