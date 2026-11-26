#!/usr/bin/env python3
"""
tools/juiceshop_agent/reset.py
=============================
Recreate the Juice Shop container to clear its STICKY scoreboard between episode batches.

Why recreate, not restart: Juice Shop's solved flags live in an in-container SQLite DB.
A `docker restart` keeps that DB, so solved challenges stay solved and re-training on them
yields reward 0 forever (and a no-op against an already-solved challenge spuriously looks
correct). Only removing + re-running the container gives a clean, all-unsolved baseline.
The feasibility-run confirmed the failure mode: the probe's solves persisted into the next
generation run and got skipped.

Runs ON the lab host (the container is loopback-bound to 127.0.0.1 since the 2026-09-26
rebind, so this must be local). Lab has docker-compose v1 only and Juice Shop has NO compose
file -- it's a raw `docker run`, so reset is stop/rm/run, NOT `compose down/up`.

Config verified live 2026-09-27: image=bkimminich/juice-shop, restart=unless-stopped,
port 127.0.0.1:3000->3000.

A container POOL (juiceshop_1..N on ports 3001..300N) for parallel rollouts is just N calls
to reset_container with different (name, port) -- concurrent rollouts against ONE container
corrupt the shared scoreboard, so parallelism requires one container each.
"""
import argparse
import subprocess
import sys
import time
from typing import Optional

import requests

DEFAULT_NAME = "juiceshop"
DEFAULT_PORT = 3000
DEFAULT_IMAGE = "bkimminich/juice-shop"


def _run(cmd: list, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def wait_ready(port: int, timeout_s: int = 90) -> bool:
    """Poll the scoreboard until it answers 200 or we give up."""
    url = f"http://localhost:{port}/api/Challenges/?size=1"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def reset_container(name: str = DEFAULT_NAME, port: int = DEFAULT_PORT,
                    image: str = DEFAULT_IMAGE, wait: bool = True,
                    wait_timeout_s: int = 90) -> bool:
    """stop -> rm -> run a fresh container. Returns True if it came back ready (or wait=False)."""
    _run(["docker", "stop", name])          # ok if not running
    _run(["docker", "rm", "-f", name])       # ok if absent
    run = _run([
        "docker", "run", "-d", "--restart=unless-stopped",
        "--name", name, "-p", f"127.0.0.1:{port}:3000", image,
    ])
    if run.returncode != 0:
        print(f"[reset] docker run failed: {run.stderr.strip()}", file=sys.stderr)
        return False
    if not wait:
        return True
    return wait_ready(port, wait_timeout_s)


def solved_count(port: int) -> Optional[int]:
    try:
        r = requests.get(f"http://localhost:{port}/api/Challenges/?size=250", timeout=10)
        if r.status_code != 200:
            return None
        return sum(1 for row in r.json().get("data", []) if row.get("solved"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=DEFAULT_NAME)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--verify-clears", action="store_true",
                    help="empirically confirm recreate resets the solved count to 0")
    a = ap.parse_args()

    if a.verify_clears:
        before = solved_count(a.port)
        print(f"[reset] solved before reset: {before}", flush=True)
    print(f"[reset] recreating {a.name} on 127.0.0.1:{a.port} ...", flush=True)
    ok = reset_container(a.name, a.port, a.image, wait=True)
    if not ok:
        print("[reset] FAILED to come ready", flush=True)
        sys.exit(1)
    after = solved_count(a.port)
    print(f"[reset] ready. solved after reset: {after}", flush=True)
    if a.verify_clears:
        if after == 0:
            print("[reset] VERIFIED: recreate clears the scoreboard (0 solved).", flush=True)
        else:
            print(f"[reset] WARNING: {after} still solved after recreate -- investigate.", flush=True)


if __name__ == "__main__":
    main()
