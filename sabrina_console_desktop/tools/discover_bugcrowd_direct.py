"""DEPRECATED shim -- discovery is now Sabrina's `discover_engagements` TOOL.

The real logic moved to tools/bounty_recon.discover_engagements() and is exposed to Sabrina as a
tool (config/tools.yaml + sabrina_gemini_loop dispatch), per the "design tools, not run-off scripts"
architecture. This shim remains only for manual/debug invocation on the lab and writes the same files.

    python tools/discover_bugcrowd_direct.py
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main():
    from ai_systems.browser_utils import PlaywrightBrowserManager
    from tools.bounty_recon import discover_engagements

    mgr = PlaywrightBrowserManager.get_instance()
    programs = discover_engagements(mgr)
    print(f"[discover] {len(programs)} programs", flush=True)

    out_json = REPO_ROOT / "data" / "discovered_engagements.json"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(programs, indent=2), encoding="utf-8")

    lines = ["# Discovered Bugcrowd engagements", "",
             f"{len(programs)} program(s).", "",
             "| Program | URL | Slug |", "| --- | --- | --- |"]
    for p in programs:
        lines.append(f"| {p.get('name','')} | {p.get('url','')} | {p.get('slug','')} |")
    (REPO_ROOT / "data" / "discovered_engagements.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("[discover] wrote data/discovered_engagements.{json,md}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
