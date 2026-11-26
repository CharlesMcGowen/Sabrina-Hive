"""Ensure local Modern Console services (:8096 telemetry and :8095 Windows Swarm Chat & Active Prompt portal) are running."""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pythonw_or_current() -> str:
    cur = Path(sys.executable)
    pyw = cur.with_name("pythonw.exe")
    return str(pyw) if pyw.is_file() else str(cur)


def _is_up(url: str, expected_service: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:
            if resp.status == 200:
                payload = json.load(resp)
                return payload.get("service") == expected_service
    except Exception:
        return False
    return False


def main() -> None:
    exe = _pythonw_or_current()
    services = (
        ("http://127.0.0.1:8096/health", "model_telemetry", [exe, "-m", "tools.model_telemetry_server", "--port", "8096"], "model_telemetry_8096.log"),
        ("http://127.0.0.1:8095/health", "swarm_chat_portal", [exe, "-m", "tools.swarm_chat_portal", "--port", "8095"], "swarm_chat_portal.log"),
    )
    log_dir = ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    pending_urls: list[tuple[str, str]] = []

    for url, svc, cmd, log_name in services:
        if _is_up(url, svc):
            continue
        log_fp = (log_dir / log_name).open("a", encoding="utf-8", buffering=1)
        subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            creationflags=flags,
        )
        pending_urls.append((url, svc))

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and pending_urls:
        pending_urls = [(u, s) for (u, s) in pending_urls if not _is_up(u, s)]
        if pending_urls:
            time.sleep(0.2)


if __name__ == "__main__":
    main()
