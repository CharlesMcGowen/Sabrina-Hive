"""Bounty recon capabilities -- the reusable logic behind Sabrina's discovery/enrich/enum TOOLS.

This module is the single source of truth for three mechanical (no-LLM) capabilities that Sabrina
calls as tools from her loop (see config/tools.yaml + sabrina_gemini_loop dispatch):

  discover_engagements(manager)   -> list every Bugcrowd program on the authenticated marketplace
  enrich_scope(manager, slug)     -> pull a program's in-scope targets from its brief page
  passive_enum(gate)              -> crt.sh subdomain enum filtered through an engagement's scope gate

Design note (2026-09-21): these started life as standalone run-off scripts. Charles' architecture is
"prompt Sabrina -> her agents deploy" -- so the mechanics live HERE as importable functions and are
exposed to her as named tools, instead of as scripts a human runs. Nothing here needs a model or a pod.
"""

import json
import re
from typing import Any, Dict, List

BUGCROWD_ENGAGEMENTS_URL = "https://bugcrowd.com/engagements-us"

# Hosts that appear on every brief as chrome / support / docs -- never a program target.
_NOISE_HOSTS = re.compile(
    r"(^|\.)(bugcrowd\.com|bugcrowdusercontent\.com|freshdesk\.com|"
    r"cloudfront\.net|googleapis\.com|gstatic\.com|google\.com|gravatar\.com|"
    r"w3\.org|schema\.org|youtube\.com|twitter\.com|linkedin\.com)$", re.I)
_URL_RE = re.compile(r"https?://[^\s)\"'<>]+", re.I)
_WILD_RE = re.compile(r"\*\.[a-z0-9.-]+", re.I)
_SLUG_RE = re.compile(r"/engagements/([a-z0-9][a-z0-9-]*)$", re.I)


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url, re.I)
    return (m.group(1) if m else url).lower().strip().rstrip(".")


def discover_engagements(manager, max_pages: int = 20, settle_ms: int = 4000) -> List[Dict[str, Any]]:
    """Open the authenticated Bugcrowd marketplace and return every program listed.

    Pure DOM extraction: paginate ?page=1..N collecting /engagements/<slug> anchors, parse them
    with the existing link parser. `manager` is a live PlaywrightBrowserManager. The cards hydrate
    client-side (one mounts fast, the rest lag ~3-4s) -- settle_ms waits for the full grid.
    """
    from tools.bounty_discovery import discover_bugcrowd_programs_from_links

    manager.open_authenticated_site(BUGCROWD_ENGAGEMENTS_URL)
    page = manager._page
    url = page.url
    if "/login" in url or "/sign-in" in url:
        raise RuntimeError(f"BUGCROWD AUTH EXPIRED (landed on {url}). Refresh browser_auth_state.json.")

    all_links: List[Dict[str, str]] = []
    seen = set()
    for page_num in range(1, max_pages + 1):
        if page_num > 1:
            target = (f"https://bugcrowd.com/engagements-us?category=bug_bounty&page={page_num}"
                      f"&sort_by=promoted&sort_direction=desc")
            page.goto(target, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector('a[href^="/engagements/"]', timeout=15000)
            page.wait_for_timeout(settle_ms)
        except Exception:
            break
        page_links = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('a[href^="/engagements/"]').forEach(a => {
                out.push({text: (a.innerText || '').trim(), href: a.getAttribute('href')});
            });
            return out;
        }""")
        fresh = [lk for lk in page_links if lk["href"] not in seen]
        for lk in page_links:
            seen.add(lk["href"])
        all_links.extend(page_links)
        if not fresh:
            break
    programs = discover_bugcrowd_programs_from_links(all_links)
    # The parser returns scope-file skeletons keyed 'program'/'display_name'; add stable
    # 'slug'/'name' aliases so every downstream consumer (tools, shims, promotion) agrees.
    for p in programs:
        p.setdefault("slug", p.get("program", ""))
        p.setdefault("name", p.get("display_name", p.get("program", "")))
    return programs


def extract_scope_from_page(page) -> List[str]:
    """Regex the in-scope targets (URLs + *.wildcards) out of a rendered brief page's text."""
    text = page.evaluate("() => document.body.innerText || ''")
    hosts, wilds = [], []
    for u in _URL_RE.findall(text):
        h = _host_of(u)
        if h and not _NOISE_HOSTS.search(h) and h not in hosts:
            hosts.append(h)
    for w in _WILD_RE.findall(text):
        w = w.lower()
        if not _NOISE_HOSTS.search(w[2:]) and w not in wilds:
            wilds.append(w)
    # wildcards first; drop specific hosts a wildcard already covers
    return wilds + [h for h in hosts if not any(h.endswith(w[1:]) for w in wilds)]


def enrich_scope(manager, slug: str, settle_ms: int = 4500) -> Dict[str, Any]:
    """Open a program's brief and return {slug, name, url, in_scope} with real targets."""
    slug = slug.strip().strip("/").split("/")[-1]
    url = f"https://bugcrowd.com/engagements/{slug}"
    manager.open_authenticated_site(url)
    page = manager._page
    page.wait_for_timeout(settle_ms)
    in_scope = extract_scope_from_page(page)
    name = page.evaluate("() => (document.querySelector('h1')||{}).innerText || ''").strip() or slug
    return {"slug": slug, "name": name, "url": url, "in_scope": in_scope, "target_count": len(in_scope)}


def passive_enum(gate) -> List[str]:
    """crt.sh subdomain enumeration filtered through an EngagementGate. Sends ZERO target traffic
    (crt.sh is a public certificate-transparency log). `gate` is a tools.engagement_scope.EngagementGate.
    """
    from tools.smuggle_sweep import enumerate_in_scope
    return enumerate_in_scope(gate)


# Where promote_engagement STAGES scope files. Deliberately NOT config/engagements/ -- that dir is
# write-protected from the loop so Sabrina cannot self-authorize a target (see sabrina_gemini_loop
# .is_protected_path). Staging lets her do all the prep; a human moves an approved file into
# config/engagements/ to actually authorize the engagement.
PENDING_DIR_REL = "data/pending_engagements"


def stage_engagement(slug: str, in_scope, program_url: str = "", out_of_scope=None,
                     focus=None, notes: str = "", platform: str = "bugcrowd",
                     rate_limit_rps: float = 0.5, repo_root=None) -> Dict[str, Any]:
    """Write a review-ready scope file to data/pending_engagements/<slug>.json.

    automated_testing_allowed is FORCED False -- promotion prepares a recon-only engagement; only a
    human flipping the flag (after moving the file into config/engagements/) authorizes live traffic.
    `in_scope` must be a non-empty list/CSV of real targets (e.g. from enrich_scope) -- promotion of
    empty/invented scope is refused so scope always traces back to a real brief.
    """
    import json as _json
    from pathlib import Path as _Path

    slug = (slug or "").strip().strip("/").split("/")[-1]
    if not slug:
        raise ValueError("stage_engagement requires a non-empty slug.")
    if isinstance(in_scope, str):
        in_scope = [s.strip() for s in in_scope.split(",") if s.strip()]
    in_scope = [s for s in (in_scope or []) if s]
    if not in_scope:
        raise ValueError("stage_engagement requires a non-empty in_scope (run enrich_scope first).")

    cfg = {
        "_readme": [
            f"STAGED by Sabrina's promote_engagement tool. Scope auto-extracted for '{slug}'.",
            "automated_testing_allowed is FALSE (recon-only). To AUTHORIZE this engagement, a human",
            "reviews this file against the live brief and MOVES it into config/engagements/ (that dir",
            "is write-protected from Sabrina on purpose -- she cannot self-authorize a target).",
        ],
        "platform": platform,
        "program": slug,
        "program_url": program_url or f"https://bugcrowd.com/engagements/{slug}",
        "in_scope": list(in_scope),
        "out_of_scope": list(out_of_scope or ["Any asset not covered by the in_scope entries above"]),
        "automated_testing_allowed": False,
        "rate_limit_rps": rate_limit_rps,
        "focus": list(focus or []),
        "notes": notes or f"Staged from Bugcrowd brief for {slug}. Human must confirm scope before authorizing.",
    }
    root = _Path(repo_root) if repo_root else _Path(__file__).resolve().parent.parent
    pending = root / PENDING_DIR_REL
    pending.mkdir(parents=True, exist_ok=True)
    path = pending / f"{slug}.json"
    path.write_text(_json.dumps(cfg, indent=2), encoding="utf-8")
    return {
        "status": "staged",
        "slug": slug,
        "file": f"{PENDING_DIR_REL}/{slug}.json",
        "in_scope_count": len(in_scope),
        "automated_testing_allowed": False,
        "next_step": (f"Human review: confirm scope, then move data/pending_engagements/{slug}.json "
                      f"into config/engagements/ to authorize recon; flip automated_testing_allowed "
                      f"only after reading the program's policy."),
    }
