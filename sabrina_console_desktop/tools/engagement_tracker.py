"""Engagement intake queue for autonomous bounty work.

Sabrina's interface should be: PROMPT her with a program -> her agents run it. This is the durable
intake that makes that real, so we stop hand-passing --goal/--scope-file. It mirrors
tools/improvement_tracker.py: engagements are the existing config/engagements/*.json scope files, and a
JSON ledger (data/engagement_ledger.json) records each engagement's phase/status so the standing runner
works them oldest-first and never repeats a finished phase.

An engagement is "runnable" only if it has a non-empty in_scope and is not a template / platform-meta
file. Every phase stays inside that engagement's EngagementGate scope -- this queue never widens scope.
"""

import os
import json
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENG_DIR = REPO_ROOT / "config" / "engagements"
LEDGER_FILE = REPO_ROOT / "data" / "engagement_ledger.json"

# Never treat these as runnable engagements: the template and platform-meta/read-only configs.
_SKIP_STEMS = {"TEMPLATE"}


def _relpath(p) -> str:
    """Repo-relative forward-slash path, falling back to the raw path if it's outside the repo
    (e.g. under test isolation)."""
    p = Path(p)
    try:
        return str(p.relative_to(REPO_ROOT)).replace(os.sep, "/")
    except ValueError:
        return str(p).replace(os.sep, "/")


def _is_meta(stem: str) -> bool:
    return stem in _SKIP_STEMS or stem.startswith("_")


def _slug_of(path: Path, data: dict) -> str:
    prog = (data.get("program") or data.get("engagement") or "").strip()
    return prog if prog and prog.upper() != "REPLACE_WITH_PROGRAM_SLUG" else path.stem


def list_engagements(include_meta: bool = False):
    """Every engagement config with its scope summary. Runnable ones have non-empty in_scope."""
    out = []
    if not ENG_DIR.exists():
        return out
    for f in sorted(ENG_DIR.glob("*.json")):
        if not include_meta and _is_meta(f.stem):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        in_scope = [s for s in (d.get("in_scope") or []) if isinstance(s, str) and "REPLACE" not in s]
        out.append({
            "slug": _slug_of(f, d),
            "file": _relpath(f),
            "platform": d.get("platform", ""),
            "in_scope": in_scope,
            "in_scope_count": len(in_scope),
            "automated_testing_allowed": bool(d.get("automated_testing_allowed", False)),
            "rate_limit_rps": d.get("rate_limit_rps", 1),
            "runnable": len(in_scope) > 0 and not _is_meta(f.stem),
        })
    return out


def _load_ledger():
    if LEDGER_FILE.exists():
        try:
            return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_ledger(data):
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, LEDGER_FILE)


def get_next_engagement(phase: str = "recon"):
    """The least-recently-worked runnable engagement for `phase` (never-worked first). Returns the
    engagement dict (from list_engagements) or None if the queue is empty."""
    ledger = _load_ledger()
    runnable = [e for e in list_engagements() if e["runnable"]]
    if not runnable:
        return None

    def last_ts(e):
        return (ledger.get(e["slug"], {}).get(phase, {}) or {}).get("ts", 0)

    runnable.sort(key=last_ts)
    return runnable[0]


def mark_engagement(slug: str, phase: str, status: str, note: str = ""):
    """Record that `phase` of `slug` finished with `status` (e.g. done / auth_expired / error)."""
    ledger = _load_ledger()
    rec = ledger.setdefault(slug, {})
    rec[phase] = {"ts": time.time(), "status": status, "note": note, "when": time.ctime()}
    _save_ledger(ledger)
    return {"slug": slug, "phase": phase, "status": status}


def engagement_status(slug: str):
    return _load_ledger().get(slug, {})


def add_engagement(slug, in_scope, platform="manual", program_url="", out_of_scope=None,
                   automated_testing_allowed=False, rate_limit_rps=1, focus=None, notes=""):
    """Enqueue a program by writing config/engagements/<slug>.json. This is 'prompt Sabrina with a
    program' in code form. automated_testing_allowed defaults False (recon only) -- the EngagementGate
    hard-blocks active api_request testing until it is deliberately set True."""
    if isinstance(in_scope, str):
        in_scope = [s.strip() for s in in_scope.split(",") if s.strip()]
    cfg = {
        "platform": platform,
        "program": slug,
        "program_url": program_url,
        "in_scope": list(in_scope),
        "out_of_scope": list(out_of_scope or []),
        "automated_testing_allowed": bool(automated_testing_allowed),
        "rate_limit_rps": rate_limit_rps,
        "focus": list(focus or []),
        "notes": notes,
    }
    ENG_DIR.mkdir(parents=True, exist_ok=True)
    path = ENG_DIR / f"{slug}.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return {"status": "added", "slug": slug, "file": _relpath(path),
            "in_scope_count": len(in_scope)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Engagement intake queue for autonomous bounty work.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a_ = sub.add_parser("add")
    a_.add_argument("--slug", required=True)
    a_.add_argument("--scope", required=True, help="Comma-separated in-scope hosts/URLs.")
    a_.add_argument("--platform", default="manual")
    a_.add_argument("--url", default="")
    a_.add_argument("--out-of-scope", default="")
    a_.add_argument("--allow-automated", action="store_true", help="Permit active testing (default: recon only).")
    a_.add_argument("--rps", type=float, default=1.0)
    a_.add_argument("--notes", default="")
    n_ = sub.add_parser("next"); n_.add_argument("--phase", default="recon")
    a = ap.parse_args()
    if a.cmd == "list":
        for e in list_engagements(include_meta=True):
            flag = "RUNNABLE" if e["runnable"] else "meta/empty"
            auto = "AUTOMATED-OK" if e["automated_testing_allowed"] else "recon-only"
            print(f"  [{flag}] {e['slug']:30s} {e['in_scope_count']:>3} in-scope  {auto}  ({e['file']})")
    elif a.cmd == "add":
        import pprint
        pprint.pprint(add_engagement(a.slug, a.scope, platform=a.platform, program_url=a.url,
                                     out_of_scope=[s.strip() for s in a.out_of_scope.split(",") if s.strip()],
                                     automated_testing_allowed=a.allow_automated, rate_limit_rps=a.rps, notes=a.notes))
    elif a.cmd == "next":
        import pprint
        pprint.pprint(get_next_engagement(a.phase))
