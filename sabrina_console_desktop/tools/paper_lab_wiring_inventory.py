"""List Kraken/gainers standalone modules and whether a paper lab or monitor file names them.

This is a name-reference heuristic, not an import graph: a module counts as wired when
one of the paper lab or monitor entry files mentions its stem. Run from sabrina_console_desktop:
  python -m tools.paper_lab_wiring_inventory
Writes artifacts/paper_lab_wiring_inventory.json.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "paper_lab_wiring_inventory.json"
WIRING_ENTRY_FILES = [
    "tools/gainers_paper_trader.py",
    "tools/kraken_paper_1000_forward.py",
    "tools/custom/kraken_monitor_plugin.py",
    "tools/kraken_desktop_widget.py",
    "tools/kraken_live_plugins.py",
]


def build_inventory(root: Path = ROOT) -> dict:
    entry_text = "\n".join((root / p).read_text(encoding="utf-8", errors="ignore")
                           for p in WIRING_ENTRY_FILES if (root / p).exists())
    modules = []
    for path in sorted((root / "tools").rglob("*.py")):
        name = path.stem
        if not re.match(r"^(kraken|gainers)", name) or name.startswith("test"):
            continue
        rel = path.relative_to(root).as_posix()
        wired = re.search(rf"\b{re.escape(name)}\b", entry_text) is not None
        modules.append({"module": rel, "lines": sum(1 for _ in path.open(encoding="utf-8", errors="ignore")),
                        "named_by_paper_lab_or_monitor": wired})
    return {"entry_files": WIRING_ENTRY_FILES,
            "method": "stem name reference in entry files (heuristic, not an import graph)",
            "total": len(modules),
            "unwired": sum(1 for m in modules if not m["named_by_paper_lab_or_monitor"]),
            "modules": modules}


def main() -> None:
    inventory = build_inventory()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(inventory, indent=1), encoding="utf-8")
    print(json.dumps({"total": inventory["total"], "unwired": inventory["unwired"], "written": str(OUT)}))


if __name__ == "__main__":
    main()
