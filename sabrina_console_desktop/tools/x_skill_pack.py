# -*- coding: utf-8 -*-
"""
tools/x_skill_pack.py
=====================
Online X.com (Twitter) Agent Skill Catalog Integrator, V3 X-Humanizer Scrubber,
and 2026 X1-X6 Hook Formula Classifier for Sabrina.

Integrates verified zero-authentication open-source X agent skills & algorithm resources:
- sergebulaev/x-skills (x-humanizer V3 scrub rules, x-hook-extractor, x-reply-drafter,
  x-post-writer, x-profile-optimizer, algorithm-heuristics, hook-formulas)
- alirezarezvani/claude-skills (marketing-skill/skills/x-twitter-growth)
- social-media-skills/skills (skills/hook-writer)
- twitter/the-algorithm HeavyRanker signal hierarchy
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
X_SKILL_CACHE_DIR = ROOT / "data" / "catalog_cache" / "x-skills"

# Curated zero-auth online X agent skills & algorithm reference docs
ONLINE_X_SKILL_RESOURCES: List[Dict[str, Any]] = [
    {
        "slug": "x-skills-algorithm-heuristics",
        "title": "2026 X (Twitter) Posting & HeavyRanker Algorithm Heuristics (sergebulaev/x-skills)",
        "url": "https://github.com/sergebulaev/x-skills/blob/main/references/algorithm-heuristics.md",
        "raw_url": "https://raw.githubusercontent.com/sergebulaev/x-skills/main/references/algorithm-heuristics.md",
        "tags": ["x.com", "twitter", "algorithm", "heavyranker", "agent-skill", "growth"],
        "description": "Synthesized 2026 X recommendation ranker weights, 30-60m velocity rules, external-link suppressors, and bookmark/reply multipliers.",
    },
    {
        "slug": "x-skills-hook-formulas",
        "title": "11 X (Twitter) Hook Formulas 2026 Edition X1-X11 (sergebulaev/x-skills)",
        "url": "https://github.com/sergebulaev/x-skills/blob/main/references/hook-formulas.md",
        "raw_url": "https://raw.githubusercontent.com/sergebulaev/x-skills/main/references/hook-formulas.md",
        "tags": ["x.com", "twitter", "hooks", "copywriting", "agent-skill", "viral"],
        "description": "11 canonical X hook skeletons mapped to engagement goals (replies, reposts, bookmarks, likes).",
    },
    {
        "slug": "x-skills-humanizer-v3",
        "title": "X Humanizer V3 Anti-AI-Slop Agent Skill (sergebulaev/x-skills)",
        "url": "https://github.com/sergebulaev/x-skills/blob/main/skills/x-humanizer/SKILL.md",
        "raw_url": "https://raw.githubusercontent.com/sergebulaev/x-skills/main/skills/x-humanizer/SKILL.md",
        "tags": ["x.com", "twitter", "humanizer", "anti-ai-slop", "SKILL.md", "agent-skill"],
        "description": "Length-controlled corpus (n=445) X humanizer skill scrubbing AI tells, reveal bridges, negative parallelism, and staccato stacks.",
    },
    {
        "slug": "x-skills-humanizer-scrub-rules",
        "title": "X Humanizer V3 Forensic & Strict Scrub Rules (sergebulaev/x-skills)",
        "url": "https://github.com/sergebulaev/x-skills/blob/main/skills/x-humanizer/references/scrub-rules.md",
        "raw_url": "https://raw.githubusercontent.com/sergebulaev/x-skills/main/skills/x-humanizer/references/scrub-rules.md",
        "tags": ["x.com", "twitter", "humanizer", "stylometry", "scrub-rules", "agent-skill"],
        "description": "Executable regex & density rules for forensic model leaks, em-dash caps, 2026 AI vocabulary markers, and dead closers.",
    },
    {
        "slug": "x-skills-hook-extractor",
        "title": "X Hook Extractor Viral Tweet Reverse-Engineering Skill (sergebulaev/x-skills)",
        "url": "https://github.com/sergebulaev/x-skills/blob/main/skills/x-hook-extractor/SKILL.md",
        "raw_url": "https://raw.githubusercontent.com/sergebulaev/x-skills/main/skills/x-hook-extractor/SKILL.md",
        "tags": ["x.com", "twitter", "hook-extractor", "imitation", "SKILL.md", "agent-skill"],
        "description": "Agent SKILL.md for reverse-engineering viral X creator tweets into reusable hook templates.",
    },
    {
        "slug": "x-skills-reply-drafter",
        "title": "X Reply & Value-Add Quote Drafter R1-R5 Templates (sergebulaev/x-skills)",
        "url": "https://github.com/sergebulaev/x-skills/blob/main/skills/x-reply-drafter/references/reply-templates.md",
        "raw_url": "https://raw.githubusercontent.com/sergebulaev/x-skills/main/skills/x-reply-drafter/references/reply-templates.md",
        "tags": ["x.com", "twitter", "replies", "quote-tweet", "SKILL.md", "agent-skill"],
        "description": "Five high-converting X reply & quote-post shapes (R1 Answer, R2 Concede-Sharpen, R3 Extend, R4 Lived-Experience, R5 Value-Add Quote).",
    },
    {
        "slug": "claude-skills-x-twitter-growth",
        "title": "X/Twitter Growth Engine SKILL.md (alirezarezvani/claude-skills)",
        "url": "https://github.com/alirezarezvani/claude-skills/blob/main/marketing-skill/skills/x-twitter-growth/SKILL.md",
        "raw_url": "https://raw.githubusercontent.com/alirezarezvani/claude-skills/main/marketing-skill/skills/x-twitter-growth/SKILL.md",
        "tags": ["x.com", "twitter", "growth", "competitor-analysis", "SKILL.md", "agent-skill"],
        "description": "Production agent SKILL.md covering X profile conversion checklists, >30% reply ratios, competitor hook extraction, and atomic/thread formats.",
    },
    {
        "slug": "social-media-skills-hook-writer",
        "title": "Scroll-Stopping Hook Writer SKILL.md (social-media-skills/skills)",
        "url": "https://github.com/social-media-skills/skills/blob/main/skills/hook-writer/SKILL.md",
        "raw_url": "https://raw.githubusercontent.com/social-media-skills/skills/main/skills/hook-writer/SKILL.md",
        "tags": ["x.com", "social-media", "hooks", "SKILL.md", "agent-skill"],
        "description": "Open-source agent SKILL.md for crafting scroll-stopping first lines grounded in authentic brand voice.",
    },
]

# Forensic & chatty model leaks (deleted on single hit)
_FORENSIC_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:oaicite|contentReference|turn\d+search\d+|attached_file|grok_card)\b(?:\[\d+\])?", re.I), ""),
    (re.compile(r"^(?:(?:okay|sure|certainly|alright|got it)[,!.\s]+)?(?:here(?:'s|\u2019s|\s+is)\s+[^\n:]{0,90}:\s*)+", re.I), ""),
    (re.compile(r"^(?:Tweet|Reply|Draft|Post|Quote|Caption|Sabrina)\s*:\s*", re.I), ""),
    (re.compile(r"\bAs of my (?:last update|knowledge cutoff)[^.\n]*\.?\s*", re.I), ""),
]

# Reveal bridges from sergebulaev/x-skills scrub-rules.md (deleted on single hit)
_REVEAL_BRIDGES: List[re.Pattern[str]] = [
    re.compile(r"(?im)^the\s+(?:result|outcome|answer|lesson|catch|kicker|truth)\?\s*"),
    re.compile(r"(?im)^here(?:'s|\u2019s)\s+(?:what|how|why|the\s+thing)\b[^:.\n]{0,40}[:.]\s*"),
    re.compile(r"(?im)^(?:plot\s+twist|spoiler|the\s+twist)[:?]\s*"),
    re.compile(r"(?i)\b(?:the\s+hard\s+truth\s+is|the\s+uncomfortable\s+reality\s+is)\s*:?\s*"),
    re.compile(r"(?i)\bin\s+today(?:'s|\u2019s)\s+fast[- ]paced\s+\w+[,\s]*"),
    re.compile(r"(?i)\bin\s+the\s+age\s+of\s+ai[,\s]*"),
]

# Dead AI closers & needy question tags (stripped from end of tweet)
_DEAD_CLOSERS: List[re.Pattern[str]] = [
    re.compile(r"[,;\s—–-]*(?:don(?:'t|\u2019t)\s+you\s+think|what\s+do\s+you\s+think|thoughts|agree|am\s+i\s+right)\?\s*[\U0001F300-\U0001FAFF\u2600-\u27BF]*\s*$", re.I),
    re.compile(r"[\s.!]*(?:let\s+that\s+sink\s+in|that(?:'s|\u2019s)\s+the\s+real\s+story|let\s+me\s+know\s+in\s+the\s+replies|tag\s+someone\s+who\s+needs\s+this)[.!]*\s*$", re.I),
]

# Negative parallelism detector ("It's not X, it's Y" / "This isn't about X, it's about Y")
_NEG_PARALLEL_RE = re.compile(
    r"(?i)\b(?:it(?:'s|\u2019s|\s+is)\s+not\s+(?:about\s+)?[^,.;\n]{2,45}[,.]\s*it(?:'s|\u2019s|\s+is)\s+|"
    r"this\s+isn(?:'t|\u2019t)\s+(?:about\s+)?[^,.;\n]{2,45}[,.]\s*(?:it(?:'s|\u2019s)|this\s+is)\s+)"
)

# 2026 durable AI vocabulary map (replaced cleanly when density >= 2, or always for worst offenders)
_ALWAYS_REPLACE_WORDS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdelve(?:s|d)?\s+into\b", re.I), "look at"),
    (re.compile(r"\bgame[- ]changer\b", re.I), "big shift"),
    (re.compile(r"\bparadigm\s+shift\b", re.I), "shift"),
    (re.compile(r"\butilize\b", re.I), "use"),
    (re.compile(r"\bleverage\b", re.I), "use"),
    (re.compile(r"\bfacilitate\b", re.I), "help"),
    (re.compile(r"\bstreamline\b", re.I), "simplify"),
]

_DENSITY_MARKERS: List[Tuple[str, re.Pattern[str], str]] = [
    ("crucial", re.compile(r"\bcrucial(?:ly)?\b\s*", re.I), ""),
    ("notably", re.compile(r"\b(?:notably|particularly|fundamentally|undoubtedly),?\s*", re.I), ""),
    ("comprehensive", re.compile(r"\b(?:comprehensive|holistic)\b", re.I), "full"),
    ("landscape", re.compile(r"\blandscape\b", re.I), "space"),
    ("multifaceted", re.compile(r"\bmultifaceted\b\s*", re.I), ""),
    ("elevate", re.compile(r"\belevate\b", re.I), "improve"),
    ("empower", re.compile(r"\bempower\b", re.I), "let"),
    ("seamless", re.compile(r"\bseamless\b", re.I), "smooth"),
    ("foster", re.compile(r"\bfoster\b", re.I), "build"),
    ("quietly", re.compile(r"\bquietly\b\s*", re.I), ""),
]


def humanize_x_text(raw: str, max_chars: int = 275, allow_urls: bool = False) -> Dict[str, Any]:
    """Apply V3 X-Humanizer forensic & strict scrubs to any candidate post, reply, or quote-tweet."""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:text|markdown)?\s*|\s*```$", "", text).strip()

    markers_hit: List[str] = []
    for pat, repl in _FORENSIC_PATTERNS:
        if pat.search(text):
            markers_hit.append("forensic_preamble")
            text = pat.sub(repl, text).strip()

    # Curly quotes -> straight quotes, strip outer wrapping quotes
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    if len(text) >= 2 and ((text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'")):
        text = text[1:-1].strip()
    text = re.sub(r"^(?:Tweet|Reply|Draft|Post|Quote|Caption|Sabrina)\s*:\s*", "", text, flags=re.I).strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()

    # Strip hashtags completely & strip outbound URLs on organic tweets unless explicitly allowed
    text = re.sub(r"(?:^|\s)#\w+", "", text).strip()
    if not allow_urls:
        text = re.sub(r"https?://\S+", "", text).strip()

    # Strip reveal bridges
    for rb in _REVEAL_BRIDGES:
        if rb.search(text):
            markers_hit.append("reveal_bridge")
            text = rb.sub("", text).strip()

    # Strip dead closers / needy question tags (now at true end of tweet after hashtags/URLs removed)
    for dc in _DEAD_CLOSERS:
        if dc.search(text):
            markers_hit.append("dead_closer")
            new_text = dc.sub("", text).strip()
            if new_text and new_text[-1] not in ".!?\"')":
                new_text += "."
            if new_text:
                text = new_text

    # Always replace worst AI buzzwords
    for pat, repl in _ALWAYS_REPLACE_WORDS:
        if pat.search(text):
            markers_hit.append(f"vocab:{repl}")
            text = pat.sub(repl, text)

    # Density-scored 2026 markers: replace if >= 2 appear in the same tweet
    density_hits = [name for name, pat, _ in _DENSITY_MARKERS if pat.search(text)]
    if len(density_hits) >= 2:
        markers_hit.extend(f"density:{h}" for h in density_hits)
        for _, pat, repl in _DENSITY_MARKERS:
            text = pat.sub(repl, text)

    # Em-dash cap (at most 1 '—' per tweet; '--' -> ', ')
    text = re.sub(r"\s*--\s*", ", ", text)
    if text.count("\u2014") > 1:
        markers_hit.append("em_dash_excess")
        first_idx = text.find("\u2014")
        head = text[: first_idx + 1]
        tail = text[first_idx + 1 :].replace(" \u2014 ", ", ").replace("\u2014", ", ")
        text = head + tail

    # Collapse extra spaces while preserving intentional single/double line breaks
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    has_neg_parallel = bool(_NEG_PARALLEL_RE.search(text))
    if has_neg_parallel:
        markers_hit.append("negative_parallelism")

    # Enforce character cap cleanly
    if len(text) > max_chars:
        text = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:-") + "…"

    return {
        "text": text,
        "markers_hit": markers_hit,
        "needs_rewrite": has_neg_parallel or len(density_hits) >= 3,
    }


def classify_viral_hook_formula(tweet_text: str) -> Dict[str, str]:
    """Classify a viral X post into one of the canonical 2026 X1-X6 hook formulas."""
    t = (tweet_text or "").strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]

    if len(lines) >= 3 and any(ln.startswith(("-", "•", "1.", "1)", ">")) for ln in lines[1:]):
        return {
            "formula_id": "X5_mini_list",
            "formula_name": "Mini-List / Contrast Stack",
            "goal": "bookmarks",
            "skeleton": "{Sharp 1-line setup}\\n- {Concrete item 1}\\n- {Concrete item 2}\\n{Punchy landing}",
        }
    if re.search(r"\b\d+(?:\.\d+)?%|\$\d+|\b\d+\s*(?:hours?|days?|years?|mins?|minutes?|x)\b", t, re.I):
        return {
            "formula_id": "X2_data_point",
            "formula_name": "Concrete Number / Timing Hook",
            "goal": "bookmarks_and_reposts",
            "skeleton": "{Specific number/time contrast} + {unexpected everyday realization}.",
        }
    if re.search(r"\b(?:unpopular opinion|most people|nobody talks about|hot take|everyone acts like|underrated|overrated)\b", t, re.I):
        return {
            "formula_id": "X1_contrarian_one_liner",
            "formula_name": "One-Liner Contrarian",
            "goal": "reposts",
            "skeleton": "{Against-the-grain truth stated flatly with zero hedge}.",
        }
    if re.search(r"\b(?:i just|me telling myself|i swear|my favorite lie|admitting that|confession)\b", t, re.I):
        return {
            "formula_id": "X3_candid_confession",
            "formula_name": "Candid First-Person Moment",
            "goal": "replies",
            "skeleton": "{Self-aware real-life habit or moment everyone secretly shares}.",
        }
    if "?" in t[:120]:
        return {
            "formula_id": "X9_curiosity_opener",
            "formula_name": "Sharp Rhetorical / Curiosity Opener",
            "goal": "replies",
            "skeleton": "{Specific, provocative observation framed as an immediate question or paradox}.",
        }
    return {
        "formula_id": "X6_relatable_cold_open",
        "formula_name": "Relatable Cold-Open Observation",
        "goal": "likes_and_replies",
        "skeleton": "{Drop straight into a shared internet/everyday observation with deadpan timing}.",
    }


def sync_online_x_skill_resources(
    db_path: Optional[Path] = None,
    cache_dir: Path = X_SKILL_CACHE_DIR,
) -> Dict[str, Any]:
    """Fetch, screen (CyberComplianceSentinel + untrusted override check), cache, and index online X skills."""
    from tools.internet_resource_indexer import DEFAULT_DB_PATH, InternetResourceIndexer
    from tools.resource_adapter import inspect_untrusted_content_safety

    cache_dir.mkdir(parents=True, exist_ok=True)
    target_db = db_path or (
        ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
        if (ROOT / "data" / "sabrina_assistant_fresh.sqlite3").exists()
        else DEFAULT_DB_PATH
    )
    indexer = InternetResourceIndexer(db_path=target_db)

    synced: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    for spec in ONLINE_X_SKILL_RESOURCES:
        t0 = time.time()
        ok, res = indexer._polite_get(spec["raw_url"], timeout=10.0)
        latency_ms = round((time.time() - t0) * 1000.0, 1)
        if not ok:
            errors.append({"slug": spec["slug"], "url": spec["raw_url"], "reason": res.get("reason", "fetch_failed")})
            continue

        content = str(res.get("text") or "")
        safe_ok, safe_err = inspect_untrusted_content_safety(content)
        if not safe_ok:
            errors.append({"slug": spec["slug"], "url": spec["raw_url"], "reason": safe_err})
            continue

        sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        cache_file = cache_dir / f"{spec['slug']}.md"
        cache_file.write_text(content, encoding="utf-8")

        item = {
            "resource_kind": "doc_skill_repo",
            "title": spec["title"],
            "url": spec["url"],
            "endpoint_or_raw_url": spec["raw_url"],
            "domain_tags": spec["tags"],
            "description": spec["description"],
            "http_status": int(res.get("status") or 200),
            "latency_ms": latency_ms,
            "content_type": str(res.get("content_type") or "text/markdown"),
            "schema_summary": {"slug": spec["slug"], "bytes": len(content), "cached_file": str(cache_file.name)},
            "usage_snippet": content[:360].strip(),
            "quality_score": 92.0,
            "content_sha256": sha256,
        }
        newly_added = indexer._upsert_resource(
            item=item,
            arm_id="arm_x_skill_pack",
            query="twitter x-skills SKILL.md growth",
        )
        synced.append({
            "slug": spec["slug"],
            "title": spec["title"],
            "bytes": len(content),
            "sha256": sha256[:16],
            "newly_indexed": newly_added,
        })

    return {
        "status": "success" if synced else "error",
        "db_path": str(target_db),
        "cache_dir": str(cache_dir),
        "synced_count": len(synced),
        "error_count": len(errors),
        "resources": synced,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync & verify Sabrina's online X.com agent skills catalog")
    parser.add_argument("action", choices=["sync", "test-scrub"], default="sync", nargs="?")
    parser.add_argument("--text", default="")
    args = parser.parse_args()
    if args.action == "test-scrub":
        print(json.dumps(humanize_x_text(args.text), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(sync_online_x_skill_resources(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
