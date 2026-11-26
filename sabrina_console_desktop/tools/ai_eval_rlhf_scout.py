# -*- coding: utf-8 -*-
"""
tools/ai_eval_rlhf_scout.py
===========================
Autonomous AI Evaluation, RLHF, Red-Teaming, & Coding Specialist Scout for Sabrina.

Targets high-paying ($40-$150+/hr) fast-cash technical evaluation & RLHF tracks
tailored to Charles McGowen's verified profile (`config/operator_profile.json`):
- 7+ yrs Offensive Security, Application Security, Penetration Testing ($25k+ bug bounty)
- 7+ yrs Python (FastAPI, Django, automation, multi-agent harnesses), 5+ yrs Go, SQL, Linux

Channels:
1. Direct AI Evaluation & RLHF Job Boards (`work.mercor.com/explore`, `app.outlier.ai/en/expert/jobs`,
   Greenhouse boards for `snorkelai` / `xai` / `scaleai`, plus direct specialist portals
   `dataannotation.tech/coders`, `alignerr.com`, `auraone.ai`, `developers.turing.com`).
2. LinkedIn Jobs AI Eval / RLHF / AI Red-Team / Coding Trainer contract search (both Easy Apply
   and External ATS).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from tools.operator_profile import load_operator_profile

logger = logging.getLogger("sabrina.ai_eval_scout")

AI_EVAL_LINKEDIN_QUERIES: List[str] = [
    "Agentic Security Engineer",
    "Offensive AI Security Engineer",
    "AI Red Team Engineer",
    "AI Threat Emulation Developer",
    "Agentic Cybersecurity Developer",
    "LLM Red Team Engineer",
    "Adversarial AI Python",
    "Cybersecurity AI Trainer",
]

DIRECT_EVAL_PORTALS: List[Dict[str, Any]] = [
    {
        "platform": "Mercor",
        "mode": "board",
        "url": "https://work.mercor.com/explore",
        "fallback_title": "Cybersecurity & Python SWE LLM Evaluation Specialist",
        "fallback_apply_url": "https://work.mercor.com/explore",
        "est_rate": "$60-$150/hr",
    },
    {
        "platform": "Outlier AI",
        "mode": "board",
        "url": "https://app.outlier.ai/en/expert/jobs",
        "fallback_title": "AI Coding & Cybersecurity Expert Trainer (Python / Red Team)",
        "fallback_apply_url": "https://app.outlier.ai/en/expert/jobs",
        "est_rate": "$45-$65/hr",
    },
    {
        "platform": "DataAnnotation Tech",
        "mode": "direct",
        "url": "https://www.dataannotation.tech/coders",
        "title": "AI Coding Evaluator — Python, Go & Software Engineering",
        "apply_url": "https://app.dataannotation.tech/workers/starter_assessment",
        "est_rate": "$40+/hr",
    },
    {
        "platform": "Alignerr (Labelbox)",
        "mode": "board",
        "url": "https://www.alignerr.com",
        "fallback_title": "AI Trainer — Software Engineering, Python & Cybersecurity",
        "fallback_apply_url": "https://app.alignerr.com/signin",
        "est_rate": "$50-$150/hr",
    },
    {
        "platform": "AuraOne (Invisible)",
        "mode": "direct",
        "url": "https://www.auraone.ai",
        "title": "AI Safety, Red Teaming & SWE Agent Evaluation Specialist",
        "apply_url": "https://www.auraone.ai",
        "est_rate": "$50-$120/hr",
    },
    {
        "platform": "Snorkel AI",
        "mode": "board",
        "url": "https://job-boards.greenhouse.io/snorkelai",
        "fallback_title": "Open Benchmarks Red Team & Coding Evaluation Specialist",
        "fallback_apply_url": "https://job-boards.greenhouse.io/snorkelai",
        "est_rate": "Contract / Competitive",
    },
    {
        "platform": "Turing",
        "mode": "direct",
        "url": "https://developers.turing.com",
        "title": "LLM Python & Go Coding / Agent RLHF Trainer",
        "apply_url": "https://developers.turing.com",
        "est_rate": "$50-$100/hr",
    },
]

TECH_POSITIVE_SIGNALS: Dict[str, int] = {
    "cybersecurity": 26,
    "security": 24,
    "red team": 28,
    "red-team": 28,
    "penetration": 25,
    "appsec": 26,
    "vulnerability": 24,
    "exploit": 22,
    "python": 24,
    "golang": 20,
    "go ": 14,
    "coding": 20,
    "software engineer": 20,
    "swe": 18,
    "backend": 18,
    "sql": 14,
    "linux": 15,
    "agent": 18,
    "rlhf": 20,
    "evaluation": 18,
    "evaluator": 20,
    "trainer": 18,
    "tutor": 16,
    "benchmark": 18,
    "remote": 10,
}

NON_FIT_NEGATIVE_SIGNALS: Dict[str, int] = {
    "biology": -65,
    "chemistry": -65,
    "physics phd": -60,
    "psychiatrist": -75,
    "medical": -65,
    "physician": -75,
    "nursing": -75,
    "legal": -55,
    "attorney": -65,
    "japanese": -70,
    "korean": -70,
    "mandarin": -70,
    "arabic": -70,
    "irish": -70,
    "hindi": -70,
    "french": -65,
    "german": -65,
    "spanish": -65,
    "linguist": -50,
    "phd required": -45,
    "must hold a phd": -50,
    "program director": -55,
    "product manager": -50,
    "project manager": -55,
    "grants": -45,
    "grc": -55,
    "faculty": -65,
    "unpaid": -80,
}


def score_eval_role(title: str, company: str, snippet: str = "") -> int:
    """Score an AI evaluation / RLHF role for fit with Charles's Python + AppSec/Red-Team profile."""
    text = f"{title} {company} {snippet}".lower()
    score = 52
    for kw, pts in TECH_POSITIVE_SIGNALS.items():
        if kw in text:
            score += pts
    for kw, pen in NON_FIT_NEGATIVE_SIGNALS.items():
        if kw in text:
            score += pen
    # Extract hourly pay boost if listed (e.g. $75/hr, $100/hr)
    rates = [int(m) for m in re.findall(r"\$(\d{2,3})\s*(?:/\s*hr|/\s*hour|per\s+hour)", text)]
    if rates and max(rates) >= 50:
        score += 10
    return max(0, min(100, score))


def build_eval_pitch_note(title: str, platform: str) -> str:
    profile = load_operator_profile()
    headline = (profile.get("identity") or {}).get("headline", "Senior Security Engineer | Python & Go Specialist")
    return (
        f"Applying for {title} on {platform} ({headline}). "
        f"7+ years offensive/application security & Python engineering ($25k+ bug bounty rewards, "
        f"multi-agent LLM evaluation harnesses, FastAPI/Go/SQL/Linux). Available immediately for technical RLHF & red-team queues."
    )


BOARD_LINK_EXTRACTOR = r"""(maxItems) => {
    const out = [];
    const seen = new Set();
    const anchors = Array.from(document.querySelectorAll('a[href]'));
    for (const a of anchors) {
        const href = a.href || '';
        if (!href || seen.has(href)) continue;
        if (!/\/jobs\/|\/explore\/|\/positions\/|\/role\/|\/expert\/jobs|greenhouse\.io\/.+\/jobs\/\d+|ashbyhq\.com\/|lever\.co\//i.test(href)) {
            continue;
        }
        const card = a.closest('li, article, tr, [class*="card" i], [class*="job" i], div') || a;
        const rawText = ((a.innerText || '') + '\n' + (card.innerText || '')).replace(/\s+/g, ' ').trim();
        if (rawText.length < 10) continue;
        seen.add(href);
        const titleLine = ((a.innerText || '').split('\n').map(s => s.trim()).find(Boolean) || rawText.slice(0, 120));
        const rateMatch = rawText.match(/\$\d{2,3}(?:\s*[-–]\s*\$\d{2,3})?\s*(?:\/\s*hr|\/\s*hour|per hour|hr)/i);
        out.push({
            title: titleLine.slice(0, 180),
            url: href,
            rate: rateMatch ? rateMatch[0] : '',
            snippet: rawText.slice(0, 420)
        });
        if (out.length >= maxItems) break;
    }
    return out;
}"""


class AiEvalRlhfScout:
    """Discovers and stages high-paying AI Evaluation, RLHF, & Red-Teaming roles across direct portals + LinkedIn."""

    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.store = runtime.store

    @staticmethod
    def _make_job_id(platform: str, url: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "", platform.lower())[:12]
        digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:10]
        return f"eval_{slug}_{digest}"

    def discover_direct_platforms(
        self, max_per_board: int = 12, min_stage_score: int = 68
    ) -> Dict[str, Any]:
        """Visit curated AI eval / RLHF portals in an isolated tab, scrape matching Python/Security/Coding tracks, and stage in SQLite."""
        base_page = self.runtime.manager._ensure_browser_page()
        page = base_page.context.new_page()
        existing_by_id = {
            str(r.get("job_id")): r for r in self.store.list_job_applications(limit=500)
        }
        existing_urls = {
            str(r.get("job_url") or "").rstrip("/").lower(): r
            for r in existing_by_id.values()
        }

        discovered: List[Dict[str, Any]] = []
        staged: List[Dict[str, Any]] = []
        platform_summaries: List[Dict[str, Any]] = []

        try:
            for entry in DIRECT_EVAL_PORTALS:
                platform = entry["platform"]
                mode = entry["mode"]
                portal_url = entry["url"]
                found_for_platform = 0
                staged_for_platform = 0

                try:
                    if mode == "direct":
                        apply_url = entry["apply_url"]
                        jid = self._make_job_id(platform, apply_url)
                        prev = existing_by_id.get(jid) or existing_urls.get(apply_url.rstrip("/").lower())
                        prev_status = (prev or {}).get("status")
                        score = score_eval_role(entry["title"], platform, entry.get("est_rate", ""))
                        status = (
                            prev_status
                            if prev_status in ("submitted", "skipped", "awaiting_human_captcha", "external_in_progress")
                            else ("staged" if score >= min_stage_score else "discovered")
                        )
                        saved = self.store.upsert_job_application({
                            "job_id": (prev or {}).get("job_id") or jid,
                            "title": entry["title"],
                            "company": platform,
                            "location": f"Remote ({entry.get('est_rate', 'Hourly/Contract')})",
                            "job_url": apply_url,
                            "easy_apply": False,
                            "match_score": score,
                            "status": status,
                            "draft_note": build_eval_pitch_note(entry["title"], platform),
                            "receipt": (prev or {}).get("receipt") or {
                                "track": "ai_eval_rlhf",
                                "platform": platform,
                                "est_rate": entry.get("est_rate", ""),
                                "observed_at": time.time(),
                            },
                        })
                        discovered.append(saved)
                        found_for_platform += 1
                        if status == "staged":
                            staged.append(saved)
                            staged_for_platform += 1
                        platform_summaries.append({"platform": platform, "found": found_for_platform, "staged": staged_for_platform})
                        continue

                    # Board mode: navigate & scrape live listings
                    page.goto(portal_url, wait_until="domcontentloaded", timeout=25000)
                    time.sleep(2.5)
                    try:
                        page.mouse.wheel(0, 500)
                        time.sleep(1.0)
                    except Exception:
                        pass

                    cards = page.evaluate(BOARD_LINK_EXTRACTOR, max_per_board * 3) or []
                    matched_any_card = False
                    for card in cards:
                        c_url = str(card.get("url") or "").strip()
                        c_title = str(card.get("title") or "").strip()
                        c_snip = str(card.get("snippet") or "")
                        if not c_url or not c_title or c_url.rstrip("/") == portal_url.rstrip("/"):
                            continue
                        score = score_eval_role(c_title, platform, c_snip)
                        if score < min_stage_score:
                            continue
                        matched_any_card = True
                        jid = self._make_job_id(platform, c_url)
                        prev = existing_by_id.get(jid) or existing_urls.get(c_url.rstrip("/").lower())
                        prev_status = (prev or {}).get("status")
                        status = (
                            prev_status
                            if prev_status in ("submitted", "skipped", "awaiting_human_captcha", "external_in_progress")
                            else "staged"
                        )
                        rate_lbl = card.get("rate") or entry.get("est_rate", "Remote Contract")
                        saved = self.store.upsert_job_application({
                            "job_id": (prev or {}).get("job_id") or jid,
                            "title": c_title[:180],
                            "company": platform,
                            "location": f"Remote ({rate_lbl})",
                            "job_url": c_url,
                            "easy_apply": False,
                            "match_score": score,
                            "status": status,
                            "draft_note": build_eval_pitch_note(c_title[:120], platform),
                            "receipt": (prev or {}).get("receipt") or {
                                "track": "ai_eval_rlhf",
                                "platform": platform,
                                "est_rate": rate_lbl,
                                "observed_at": time.time(),
                            },
                        })
                        discovered.append(saved)
                        found_for_platform += 1
                        if status == "staged":
                            staged.append(saved)
                            staged_for_platform += 1
                        if found_for_platform >= max_per_board:
                            break

                    # Fallback: if SPA board gated listing links behind sign-in, stage the portal's specialist onboarding entry
                    if not matched_any_card and entry.get("fallback_apply_url"):
                        fb_url = entry["fallback_apply_url"]
                        fb_title = entry["fallback_title"]
                        jid = self._make_job_id(platform, fb_url)
                        prev = existing_by_id.get(jid) or existing_urls.get(fb_url.rstrip("/").lower())
                        prev_status = (prev or {}).get("status")
                        score = score_eval_role(fb_title, platform, entry.get("est_rate", ""))
                        status = (
                            prev_status
                            if prev_status in ("submitted", "skipped", "awaiting_human_captcha", "external_in_progress")
                            else "staged"
                        )
                        saved = self.store.upsert_job_application({
                            "job_id": (prev or {}).get("job_id") or jid,
                            "title": fb_title,
                            "company": platform,
                            "location": f"Remote ({entry.get('est_rate', 'Hourly')})",
                            "job_url": fb_url,
                            "easy_apply": False,
                            "match_score": score,
                            "status": status,
                            "draft_note": build_eval_pitch_note(fb_title, platform),
                            "receipt": (prev or {}).get("receipt") or {
                                "track": "ai_eval_rlhf",
                                "platform": platform,
                                "est_rate": entry.get("est_rate", ""),
                                "observed_at": time.time(),
                            },
                        })
                        discovered.append(saved)
                        found_for_platform += 1
                        if status == "staged":
                            staged.append(saved)
                            staged_for_platform += 1

                    platform_summaries.append({"platform": platform, "found": found_for_platform, "staged": staged_for_platform})
                except Exception as exc:
                    logger.warning("AI eval scout error on %s (%s): %s", platform, portal_url, exc)
                    platform_summaries.append({"platform": platform, "error": str(exc)})
        finally:
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass

        return {
            "status": "success",
            "track": "ai_eval_rlhf",
            "platforms_scanned": len(DIRECT_EVAL_PORTALS),
            "roles_discovered": len(discovered),
            "roles_staged": len(staged),
            "platform_summaries": platform_summaries,
            "sample": staged[:8],
        }

    def run_full_scout_cycle(self, include_linkedin: bool = True) -> Dict[str, Any]:
        """Run both Direct Platform AI-Eval discovery AND LinkedIn AI-Eval/RLHF search."""
        direct_res = self.discover_direct_platforms()
        li_res: Dict[str, Any] = {"status": "skipped"}
        if include_linkedin:
            try:
                from tools.linkedin_jobs_worker import LinkedInJobsWorker
                worker = LinkedInJobsWorker(self.runtime)
                li_res = worker.discover_and_stage(
                    queries=AI_EVAL_LINKEDIN_QUERIES,
                    location="United States",
                    max_jobs=8,
                    easy_apply_only=False,
                    auto_stage_min_score=62,
                )
            except Exception as exc:
                li_res = {"status": "failed", "error": str(exc)}
        return {
            "status": "success",
            "direct_platforms": direct_res,
            "linkedin_eval_jobs": li_res,
            "funnel": self.store.job_funnel_counts(),
        }
