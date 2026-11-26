"""
Bounty Scope Loader
===================
Builds the list of in-scope GitHub repositories from Sabrina's verified
HackerOne / Bugcrowd scope files, expands org-level assets into repos, and
ranks repos by the number of published GitHub Security Advisories (GHSAs)
available for fix-reversal.

Read-only: only public GitHub REST metadata is queried (no exploit traffic).

IMPORTANT: org-level assets (e.g. github.com/zendesk) often carry per-repo
exclusions in the live program brief. Every target is emitted with
`exclusions_verified=False`; the live brief must be checked before staging.
Structured path exclusions conservatively exclude the containing repository,
because this ranker selects repositories rather than individual files.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

H1_FILE = ROOT / "data" / "hackerone_verified_oss_scopes.json"
H1_FULL_FILE = ROOT / "data" / "hackerone_full_scopes.json"
BC_FILE = ROOT / "data" / "bugcrowd_verified_oss_scopes.json"
OUT_DIR = ROOT / "artifacts" / "cve_variant_hunter"
CACHE_FILE = OUT_DIR / "scope_targets_cache.json"

GH_RE = re.compile(
    r"(?<![A-Za-z0-9_.@/-])(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9_.-]+)(?:/([A-Za-z0-9_.*-]+))?(?=$|[/#?\s])", re.I,
)
CACHE_VERSION = 2


def _headers() -> Dict[str, str]:
    token = ""
    try:
        from tools.secret_store import get_secret
        token = get_secret("GITHUB_TOKEN") or get_secret("GH_TOKEN") or ""
    except Exception:
        pass
    h = {"Accept": "application/vnd.github+json", "User-Agent": "Sabrina-Scope-Loader/1.0"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _parse_gh(identifier: str) -> Optional[Tuple[str, Optional[str]]]:
    m = GH_RE.search(identifier or "")
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo:
        repo = re.sub(r"\.git$", "", repo, flags=re.I)
        if repo in ("*", ""):
            repo = None
    return owner, repo


def _scope_fingerprint(org_repo_cap: int) -> str:
    """Invalidate rankings when scope contents, selection rules, or limits change."""
    digest = hashlib.sha256(f"{CACHE_VERSION}:{org_repo_cap}".encode("utf-8"))
    for path in (H1_FULL_FILE, H1_FILE, BC_FILE):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0present\0" + path.read_bytes() if path.exists() else b"\0missing\0")
    return digest.hexdigest()


def _is_excluded(full_name: str, exclusions: set) -> bool:
    name = full_name.lower()
    return any(fnmatchcase(name, key if "/" in key else f"{key}/*") for key in exclusions)


def load_exclusions() -> Dict[str, set]:
    """Program -> set of lowercase 'owner/repo' or 'owner' marked out-of-scope."""
    excl: Dict[str, set] = {}
    if H1_FULL_FILE.exists():
        for p in json.loads(H1_FULL_FILE.read_text(encoding="utf-8")).get("programs", []):
            for a in p.get("source_out_of_scope") or []:
                parsed = _parse_gh(a.get("asset_identifier", ""))
                if parsed:
                    key = f"{parsed[0]}/{parsed[1]}" if parsed[1] else parsed[0]
                    excl.setdefault(f"hackerone:{p['handle']}", set()).add(key.lower())
    return excl


def load_program_assets() -> List[Dict[str, Any]]:
    """Returns one record per (program, github owner[/repo]) asset."""
    assets: List[Dict[str, Any]] = []
    if H1_FULL_FILE.exists():
        for p in json.loads(H1_FULL_FILE.read_text(encoding="utf-8")).get("programs", []):
            if p.get("submission_state") != "open":
                continue
            for a in p.get("source_in_scope") or []:
                if a.get("eligible_for_submission") is not True:
                    continue
                parsed = _parse_gh(a.get("asset_identifier", ""))
                if not parsed:
                    continue
                assets.append({
                    "platform": "hackerone",
                    "program": p.get("handle"),
                    "program_name": p.get("name"),
                    "program_url": p.get("url"),
                    "owner": parsed[0],
                    "repo": parsed[1],
                    "max_severity": a.get("max_severity"),
                    "eligible_for_bounty": a.get("eligible_for_bounty"),
                    "offers_bounties": p.get("offers_bounties"),
                    "asset_identifier": a.get("asset_identifier"),
                    "instruction": a.get("instruction") or "",
                })
    elif H1_FILE.exists():
        for p in json.loads(H1_FILE.read_text(encoding="utf-8")):
            if p.get("submission_state") not in (None, "open"):
                continue
            for a in p.get("github_or_source_in_scope") or []:
                parsed = _parse_gh(a.get("asset_identifier", ""))
                if not parsed:
                    continue
                assets.append({
                    "platform": "hackerone",
                    "program": p.get("handle"),
                    "program_name": p.get("name"),
                    "program_url": p.get("url"),
                    "owner": parsed[0],
                    "repo": parsed[1],
                    "max_severity": a.get("max_severity"),
                    "asset_identifier": a.get("asset_identifier"),
                    "instruction": a.get("instruction") or "",
                })
    if BC_FILE.exists():
        for p in json.loads(BC_FILE.read_text(encoding="utf-8")):
            slug = (p.get("url") or "").rstrip("/").split("/")[-1]
            for a in p.get("github_in_scope") or []:
                parsed = _parse_gh(a.get("uri") or a.get("target") or a.get("name") or "")
                if not parsed:
                    continue
                assets.append({
                    "platform": "bugcrowd",
                    "program": slug,
                    "program_name": p.get("name"),
                    "program_url": p.get("url"),
                    "owner": parsed[0],
                    "repo": parsed[1],
                    "max_payout": p.get("max_payout"),
                    "asset_identifier": a.get("uri") or a.get("target") or a.get("name"),
                    "instruction": "",
                })
    return assets


def _list_org_repos(owner: str, headers: Dict[str, str], cap: int) -> List[Dict[str, Any]]:
    repos: List[Dict[str, Any]] = []
    for kind in ("orgs", "users"):
        page = 1
        while len(repos) < 300:
            r = requests.get(
                f"https://api.github.com/{kind}/{owner}/repos",
                params={"per_page": 100, "page": page, "type": "public"},
                headers=headers, timeout=20,
            )
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            repos.extend(batch)
            page += 1
        if repos:
            break
    repos = [x for x in repos if not x.get("archived") and not x.get("fork")]
    repos.sort(key=lambda x: x.get("stargazers_count", 0), reverse=True)
    return repos[:cap]


def _advisory_count(full_name: str, headers: Dict[str, str]) -> Tuple[int, int]:
    r = requests.get(
        f"https://api.github.com/repos/{full_name}/security-advisories",
        params={"per_page": 100, "state": "published"},
        headers=headers, timeout=20,
    )
    if r.status_code != 200 or not isinstance(r.json(), list):
        return 0, 0
    advs = r.json()
    high = sum(1 for a in advs if a.get("severity") in ("high", "critical"))
    return len(advs), high


def build_targets(org_repo_cap: int = 25, use_cache_hours: float = 24.0) -> Dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fingerprint = _scope_fingerprint(org_repo_cap)
    if CACHE_FILE.exists() and use_cache_hours > 0:
        cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if (cached.get("scope_fingerprint") == fingerprint
                and time.time() - cached.get("built_at", 0) < use_cache_hours * 3600):
            return cached

    headers = _headers()
    assets = load_program_assets()
    targets: Dict[str, Dict[str, Any]] = {}
    org_cache: Dict[str, List[Dict[str, Any]]] = {}
    exclusions = load_exclusions()
    excluded_count = 0

    for a in assets:
        expanded = not a["repo"] or "*" in a["repo"]
        if not expanded:
            full_names = [f"{a['owner']}/{a['repo']}"]
            meta = {}
        else:
            if a["owner"] not in org_cache:
                org_cache[a["owner"]] = _list_org_repos(a["owner"], headers, org_repo_cap)
            full_names = [x["full_name"] for x in org_cache[a["owner"]]]
            meta = {x["full_name"]: x for x in org_cache[a["owner"]]}
            if a["repo"]:
                pattern = f"{a['owner']}/{a['repo']}".lower()
                full_names = [fn for fn in full_names if fnmatchcase(fn.lower(), pattern)]
        prog_excl = exclusions.get(f"{a['platform']}:{a['program']}", set())
        for fn in full_names:
            if _is_excluded(fn, prog_excl):
                excluded_count += 1
                continue
            t = targets.setdefault(fn.lower(), {
                "repo": fn,
                "programs": [],
                "stars": (meta.get(fn) or {}).get("stargazers_count"),
                "language": (meta.get(fn) or {}).get("language"),
                "via_org_wildcard": expanded,
                "exclusions_verified": False,
            })
            t["via_org_wildcard"] = t["via_org_wildcard"] or expanded
            t["programs"].append({k: a.get(k) for k in (
                "platform", "program", "program_name", "program_url",
                "max_severity", "max_payout", "eligible_for_bounty",
                "offers_bounties", "asset_identifier", "instruction")})

    for t in targets.values():
        total, high = _advisory_count(t["repo"], headers)
        t["ghsa_total"] = total
        t["ghsa_high_critical"] = high

    ranked = sorted(targets.values(),
                    key=lambda t: (t["ghsa_high_critical"], t["ghsa_total"], t.get("stars") or 0),
                    reverse=True)
    out = {
        "built_at": time.time(),
        "scope_fingerprint": fingerprint,
        "programs": len({(a["platform"], a["program"]) for a in assets}),
        "assets": len(assets),
        "repos": len(ranked),
        "repos_with_advisories": sum(1 for t in ranked if t["ghsa_total"]),
        "excluded_out_of_scope": excluded_count,
        "targets": ranked,
    }
    CACHE_FILE.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    res = build_targets(use_cache_hours=0)
    print(json.dumps({k: res[k] for k in ("programs", "assets", "repos", "repos_with_advisories", "excluded_out_of_scope")}, indent=2))
    for t in res["targets"][:40]:
        progs = ", ".join(sorted({f"{p['platform']}:{p['program']}" for p in t["programs"]}))
        print(f"{t['ghsa_high_critical']:3d} hi/crit  {t['ghsa_total']:3d} total  {t['repo']:<45} [{progs}]"
              + ("  (org wildcard - check exclusions)" if t["via_org_wildcard"] else ""))
