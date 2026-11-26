#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Host Service Containerizer (`tools/containerize_sabrina_host_services.py`)
==================================================================================
Migrates Sabrina's bare-host `systemd --user` Python services on `ego` into isolated,
named Docker containers (`sabrina-host-runtime:latest`) while preserving:
  1. `systemctl --user is-active <service>` / `restart` compatibility for all existing
     health supervisors (`kraken_desktop_widget.py`, `kraken_paper_supervision.py`,
     `sabrina_epistemic_sentinel.py`, `ai_service_manager.py`, `sabrina_social.availability`).
  2. Real-time `/dev/shm/sabrina_kraken_ws_live.json` 0.5s RAM-disk sharing (`--ipc host -v /dev/shm:/dev/shm`).
  3. Exact file ownership (`--user 1000:1000`) and SHA-256 release verification paths
     (`-v /home/ego:/home/ego -v /mnt/14tb:/mnt/14tb`).
  4. First-class container visibility (`docker ps`, `docker stats`, `docker logs <service>`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List

IMAGE_TAG = "sabrina-host-runtime:latest"
SYNC_ROOT = "/home/ego/bot/Sabrina-windows-sync"
EXEC_V9_ROOT = "/home/ego/.local/share/sabrina-kraken/execution-v1-plugins-20261121-v9"
WALLET_OBS_ROOT = "/home/ego/.local/share/sabrina-kraken/wallet-observer-v1"

# 19 Continuous Daemon Services + 5 Timer-Triggered Oneshot Services = 24 Containerized Services
CONTAINERIZED_SERVICES: List[Dict[str, Any]] = [
    {
        "unit": "sabrina-kraken-ws-recorder.service",
        "container": "sabrina-kraken-ws-recorder",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -m tools.kraken_ws_2s_recorder --out-dir /mnt/14tb/sabrina-model-data/kraken-ws-2s --interval-sec 0.5 --flush-sec 15.0 --refresh-sec 900.0 --book-depth 10",
    },
    {
        "unit": "sabrina-kraken-paper-1000.service",
        "container": "sabrina-kraken-paper-1000",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -m tools.kraken_paper_1000_forward --interval 15 --start-cash 1000.0",
    },
    {
        "unit": "sabrina-kraken-execution-v1.service",
        "container": "sabrina-kraken-execution-v1",
        "daemon": True,
        "workdir": EXEC_V9_ROOT,
        "env": {"PYTHONPATH": EXEC_V9_ROOT},
        "healthcheck_script": f"{SYNC_ROOT}/tools/sabrina_healthcheck.py",
        "extra_docker_args": "--memory 512m",
        "cmd": "python3 -B -m tools.kraken_production --config /home/ego/.local/state/sabrina-kraken/execution-v1/config.json run --live",
    },
    {
        "unit": "sabrina-kraken-execution-v1-acct2.service",
        "container": "sabrina-kraken-execution-v1-acct2",
        "daemon": True,
        "workdir": EXEC_V9_ROOT,
        "env": {"PYTHONPATH": EXEC_V9_ROOT},
        "env_file": "/home/ego/kraken-explorer/.env.account2",
        "extra_docker_args": "--memory 512m",
        "cmd": "python3 -B -m tools.kraken_production --config /home/ego/.local/state/sabrina-kraken/execution-v1-acct2/config.json run --live",
    },
    {
        "unit": "sabrina-kraken-strategy-lab.service",
        "container": "sabrina-kraken-strategy-lab",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -m tools.kraken_strategy_lab worker",
    },
    {
        "unit": "sabrina-kraken-tripwires.service",
        "container": "sabrina-kraken-tripwires",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -m tools.kraken_tripwires",
    },
    {
        "unit": "sabrina-kraken-wallet-observer.service",
        "container": "sabrina-kraken-wallet-observer",
        "daemon": True,
        "workdir": WALLET_OBS_ROOT,
        "env": {"PYTHONPATH": WALLET_OBS_ROOT},
        "cmd": "python3 -B -m tools.kraken_wallet_observer --state-dir /home/ego/.local/state/sabrina-kraken/wallet-observer-v1 --duration 0 --decision-file /home/ego/.local/state/sabrina-kraken/execution-v1/last-cycle.json",
    },
    {
        "unit": "sabrina-lab-coordinator.service",
        "container": "sabrina-lab-coordinator",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -m tools.lab_control --host 0.0.0.0 --port 8090",
    },
    {
        "unit": "sabrina-customer-scheduler.service",
        "container": "sabrina-customer-scheduler",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -u -m tools.customer_job_scheduler run",
    },
    {
        "unit": "sabrina-social-api.service",
        "container": "sabrina-social-api",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {
            "PYTHONPATH": SYNC_ROOT,
            "TZ": "America/Chicago",
            "SABRINA_LINKEDIN_24_7": "0",
            "SABRINA_LINKEDIN_START_HOUR": "8",
            "SABRINA_LINKEDIN_END_HOUR": "21",
        },
        "cmd": "python3 -m sabrina_social.api --host 127.0.0.1 --port 8087",
    },
    {
        "unit": "sabrina-social-pulse.service",
        "container": "sabrina-social-pulse",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {
            "PYTHONPATH": SYNC_ROOT,
            "TZ": "America/Chicago",
            "SABRINA_LINKEDIN_24_7": "0",
            "SABRINA_LINKEDIN_START_HOUR": "8",
            "SABRINA_LINKEDIN_END_HOUR": "21",
            "SABRINA_LINKEDIN_MODEL": "llama3.1:8b",
        },
        "cmd": "python3 -m sabrina_social.supervisor --continuous --cdp-url http://127.0.0.1:19224 --interval-seconds 45 --wait-seconds 45",
    },
    {
        "unit": "sabrina-social-scheduler.service",
        "container": "sabrina-social-scheduler",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -m sabrina_social.scheduler --loop --interval-seconds 15",
    },
    {
        "unit": "sabrina-content-research.service",
        "container": "sabrina-content-research",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -m sabrina_social.research_api --host 0.0.0.0 --port 8088",
    },
    {
        "unit": "sabrina-architecture-research.service",
        "container": "sabrina-architecture-research",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -u -m tools.architecture_research --loop",
    },
    {
        "unit": "sabrina-layer-research.service",
        "container": "sabrina-layer-research",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -u -m tools.selective_layer_research --loop",
    },
    {
        "unit": "sabrina-model-research.service",
        "container": "sabrina-model-research",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -u -m tools.model_research_cycle --loop",
    },
    {
        "unit": "sabrina-printed-math-proof-v1.service",
        "container": "sabrina-printed-math-proof-v1",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": f"python3 -B -m tools.math_proof worker --run {SYNC_ROOT}/data/architecture_research/math-proof-v1",
    },
    {
        "unit": "sabrina-recruited-ssm.service",
        "container": "sabrina-recruited-ssm",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -u -m tools.recruited_ssm_eval worker",
    },
    {
        "unit": "sabrina-recruited-systems.service",
        "container": "sabrina-recruited-systems",
        "daemon": True,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -u -m tools.recruited_ssm_eval systems-worker",
    },
    # Timer-Triggered Oneshot Services
    {
        "unit": "sabrina-kraken-algo-rd-forge.service",
        "container": "sabrina-kraken-algo-rd-forge",
        "daemon": False,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -m tools.kraken_autonomous_algo_forge --once",
    },
    {
        "unit": "sabrina-kraken-release-stager.service",
        "container": "sabrina-kraken-release-stager",
        "daemon": False,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -m tools.kraken_paper_to_live_stager",
    },
    {
        "unit": "sabrina-cve-variant-hunter.service",
        "container": "sabrina-cve-variant-hunter",
        "daemon": True,
        "service_type": "simple",
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": f"python3 -u {SYNC_ROOT}/tools/sabrina_cve_variant_hunter.py --loop --interval-sec 900 --batch-size 6",
    },
    {
        "unit": "sabrina-epistemic-sentinel.service",
        "container": "sabrina-epistemic-sentinel",
        "daemon": False,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -B -m tools.sabrina_epistemic_sentinel pulse --autonomous-model-approval",
    },
    {
        "unit": "sabrina-recruiter-conversations.service",
        "container": "sabrina-recruiter-conversations",
        "daemon": False,
        "workdir": SYNC_ROOT,
        "env": {"PYTHONPATH": SYNC_ROOT},
        "cmd": "python3 -m tools.autonomous_dm_responder tick --cdp-url http://127.0.0.1:19224",
    },
]


def build_dropin_content(spec: Dict[str, Any]) -> str:
    cname = spec["container"]
    wdir = spec["workdir"]
    env_flags: List[str] = [
        "-e HOME=/home/ego",
        "-e PYTHONUNBUFFERED=1",
        "-e PYTHONDONTWRITEBYTECODE=1",
    ]
    for k, v in (spec.get("env") or {}).items():
        env_flags.append(f"-e {k}={v}")
    if spec.get("env_file"):
        env_flags.append(f"--env-file {spec['env_file']}")
    extra = f" {spec['extra_docker_args']}" if spec.get("extra_docker_args") else ""

    health_flags = ""
    if spec.get("daemon"):
        h_interval = spec.get("health_interval", "30s")
        h_timeout = spec.get("health_timeout", "5s")
        h_retries = spec.get("health_retries", 3)
        h_start = spec.get("health_start_period", "20s")
        script = spec.get("healthcheck_script")
        probe = f"python3 -B {script}" if script else "python3 -m tools.sabrina_healthcheck"
        health_flags = (
            f" --health-cmd '{probe} {cname}'"
            f" --health-interval {h_interval}"
            f" --health-timeout {h_timeout}"
            f" --health-retries {h_retries}"
            f" --health-start-period {h_start}"
        )

    env_str = " ".join(env_flags)

    docker_run = (
        f"/usr/bin/docker run --rm --init --name {cname} "
        f"--user 1000:1000 --network host --pid host --ipc host{extra}{health_flags} "
        f"-v /home/ego:/home/ego -v /mnt/14tb:/mnt/14tb -v /dev/shm:/dev/shm "
        f"-w {wdir} {env_str} {IMAGE_TAG} {spec['cmd']}"
    )
    lines = [
        "[Service]",
        f"ExecStartPre=-/usr/bin/docker rm -f {cname}",
        "ExecStart=",
        f"ExecStart={docker_run}",
    ]
    if spec.get("service_type"):
        lines.append(f"Type={spec['service_type']}")
    if spec.get("daemon"):
        lines.append(f"ExecStop=-/usr/bin/docker stop -t 15 {cname}")
        lines.append("Restart=always")
        lines.append("RestartSec=5s")
    return "\n".join(lines) + "\n"


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Sabrina Host Service Containerizer")
    parser.add_argument("--dropins-only", action="store_true", help="Regenerate systemd dropins and daemon-reload without rebuilding Docker image")
    args = parser.parse_args()

    if not args.dropins_only:
        dockerfile_path = Path(SYNC_ROOT) / "docker" / "Dockerfile.sabrina-host"
        if not dockerfile_path.is_file():
            raise FileNotFoundError(f"Missing Dockerfile: {dockerfile_path}")

        print(f"[1/4] Building Docker image {IMAGE_TAG}...")
        subprocess.check_call([
            "docker", "build",
            "-t", IMAGE_TAG,
            "-f", str(dockerfile_path),
            str(dockerfile_path.parent),
        ])

        print(f"[2/4] Smoke-testing {IMAGE_TAG}...")
        subprocess.check_call([
            "docker", "run", "--rm", "--user", "1000:1000",
            "-v", "/home/ego:/home/ego",
            IMAGE_TAG,
            "python3", "-c",
            "import pyarrow, pytest, defusedxml, pypdf, torch, requests, websocket, websockets, numpy, pandas; print('RUNTIME_SMOKE_TEST_OK')",
        ])
    else:
        print("[INFO] --dropins-only specified: skipping docker build and smoke tests.")

    print("[3/4] Writing systemd --user Docker container drop-ins...")
    systemd_user_dir = Path("/home/ego/.config/systemd/user")
    daemon_units: List[str] = []
    for spec in CONTAINERIZED_SERVICES:
        unit = spec["unit"]
        unit_file = systemd_user_dir / unit
        if not unit_file.is_file():
            continue
        dropin_dir = systemd_user_dir / f"{unit}.d"
        dropin_dir.mkdir(parents=True, exist_ok=True)
        old_dropin = dropin_dir / "90-docker-container.conf"
        if old_dropin.is_file():
            old_dropin.unlink()
        dropin_file = dropin_dir / "zzz-docker-container.conf"
        dropin_file.write_text(build_dropin_content(spec), encoding="utf-8")
        if spec.get("daemon"):
            daemon_units.append(unit)

    subprocess.check_call(["systemctl", "--user", "daemon-reload"])

    existing_ps = subprocess.run(
        ["docker", "ps", "--filter", "name=sabrina-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    already_running = set(existing_ps.stdout.strip().splitlines())
    to_restart = [
        spec["unit"]
        for spec in CONTAINERIZED_SERVICES
        if spec.get("daemon") and spec["unit"] in daemon_units and spec["container"] not in already_running
    ]
    print(f"[4/4] Starting/restarting {len(to_restart)} non-running continuous Sabrina container services...")
    for unit in to_restart:
        subprocess.run(["systemctl", "--user", "restart", unit], check=False)

    time.sleep(4.0)
    ps_proc = subprocess.run(
        ["docker", "ps", "--filter", "name=sabrina-", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"],
        capture_output=True,
        text=True,
         check=True,
    )
    containers = [line.split("\t") for line in ps_proc.stdout.strip().splitlines() if line.strip()]
    print(json.dumps({
        "image": IMAGE_TAG,
        "configured_services": len(CONTAINERIZED_SERVICES),
        "running_sabrina_containers_count": len(containers),
        "running_containers": [
            {"name": c[0], "status": c[1], "image": c[2] if len(c) > 2 else IMAGE_TAG}
            for c in containers
        ],
    }, indent=2))


if __name__ == "__main__":
    main()
