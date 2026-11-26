"""Live control for the 'keep herself busy' loop -- flip what Sabrina works on WITHOUT relaunching.

The busy scheduler (run_engagement.py busy) reads data/busy_control.json at the top of every cycle:

    {"mode": "improve" | "all", "paused": false}

  mode=improve : self-improvement only (no third-party target)
  mode=all     : work the authorized engagement recon queue first, else self-improve
  paused=true  : do nothing this cycle (pod stays up; no work spawned)

Flip it however you like -- all of these write the same file:
  * the web panel (tools/busy_panel.py) -- buttons in your browser
  * CLI:  python tools/busy_control.py mode all      (or: mode improve / pause / resume / status)
  * just edit data/busy_control.json

The change takes effect on the busy loop's NEXT cycle (no restart).
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTROL_PATH = REPO_ROOT / "data" / "busy_control.json"
_DEFAULT = {"mode": "improve", "paused": False}
_VALID_MODES = ("improve", "all")


def read_control() -> dict:
    try:
        data = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
        mode = data.get("mode", "improve")
        return {"mode": mode if mode in _VALID_MODES else "improve",
                "paused": bool(data.get("paused", False))}
    except Exception:
        return dict(_DEFAULT)


def write_control(mode=None, paused=None) -> dict:
    cur = read_control()
    if mode is not None:
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES}, got {mode!r}")
        cur["mode"] = mode
    if paused is not None:
        cur["paused"] = bool(paused)
    CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTROL_PATH.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def _main(argv):
    if not argv or argv[0] == "status":
        print(json.dumps(read_control(), indent=2))
        return 0
    cmd = argv[0]
    if cmd == "mode":
        if len(argv) < 2:
            print("usage: busy_control.py mode improve|all")
            return 2
        print(json.dumps(write_control(mode=argv[1]), indent=2))
    elif cmd == "pause":
        print(json.dumps(write_control(paused=True), indent=2))
    elif cmd == "resume":
        print(json.dumps(write_control(paused=False), indent=2))
    else:
        print(f"unknown command {cmd!r}; use: mode <improve|all> | pause | resume | status")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
