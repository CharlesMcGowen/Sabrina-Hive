"""Start the local read-only infrastructure endpoint for the Modern Console if absent."""
from __future__ import annotations

import subprocess
import sys
import json
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8096/health"


def main() -> None:
    try:
        with urllib.request.urlopen(URL, timeout=1) as response:
            if response.status == 200 and json.load(response).get("service") == "model_telemetry":
                return
    except Exception:
        pass
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([sys.executable, "-m", "tools.model_telemetry_server", "--port", "8096"],
                     cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)


if __name__ == "__main__":
    main()
