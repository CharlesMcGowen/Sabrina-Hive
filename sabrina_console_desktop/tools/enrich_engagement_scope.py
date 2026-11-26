"""DEPRECATED shim -- scope enrichment is now Sabrina's `enrich_scope` TOOL.

The real logic moved to tools/bounty_recon.enrich_scope() and is exposed to Sabrina as a tool
(config/tools.yaml + sabrina_gemini_loop dispatch). This shim remains only for manual/debug batch
enrichment on the lab.

    python tools/enrich_engagement_scope.py --slugs webdotcom,nubank,okta
    python tools/enrich_engagement_scope.py --limit 30
"""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--slugs", default="")
    args = ap.parse_args()

    from ai_systems.browser_utils import PlaywrightBrowserManager
    from tools.bounty_recon import enrich_scope

    src = json.loads((REPO_ROOT / "data" / "discovered_engagements.json").read_text(encoding="utf-8"))
    slugs = [s.strip() for s in args.slugs.split(",") if s.strip()] if args.slugs \
        else [p.get("slug") for p in src if p.get("slug")][:args.limit]

    mgr = PlaywrightBrowserManager.get_instance()
    out = []
    for i, slug in enumerate(slugs, 1):
        try:
            rec = enrich_scope(mgr, slug)
        except Exception as e:
            rec = {"slug": slug, "name": slug, "in_scope": [], "error": str(e)}
        out.append(rec)
        print(f"[{i}/{len(slugs)}] {slug}: {len(rec.get('in_scope', []))} targets", flush=True)

    (REPO_ROOT / "data" / "discovered_engagements_scoped.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    lines = ["# Scoped Bugcrowd engagements", "", f"{len(out)} programs.", "",
             "| Program | Slug | # Targets | In-scope (sample) |", "| --- | --- | --- | --- |"]
    for r in sorted(out, key=lambda x: -len(x.get("in_scope", []))):
        samp = ", ".join(r.get("in_scope", [])[:6]).replace("|", "\\|")
        lines.append(f"| {r.get('name','')} | {r['slug']} | {len(r.get('in_scope', []))} | {samp} |")
    (REPO_ROOT / "data" / "discovered_engagements_scoped.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("[enrich] wrote data/discovered_engagements_scoped.{json,md}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
