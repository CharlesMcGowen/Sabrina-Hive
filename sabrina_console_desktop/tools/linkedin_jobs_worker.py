# -*- coding: utf-8 -*-
"""
tools/linkedin_jobs_worker.py
=============================
Anti-bot-safe LinkedIn Job Discovery, Scoring, and Staged Application Worker.

Implements:
1. Job Search & Deduplication against `social.sqlite3` (`job_applications` table).
2. Career Profile & Keyword Scoring (AI Engineering, Cybersecurity, AppSec, Software Architecture).
3. Anti-Bot Telemetry Safeguards:
   - Natural viewport scroll over job description before interacting with Easy Apply.
   - Triangular/Poisson human pacing via `LinkedInRateLimiter.wait_jitter`.
   - Strict daily caps (`job_search`: 25/day, `job_apply`: 15/day) and circuit breaker checks.
4. Human-in-the-Loop Safe Staging + Verified Receipt Recording.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

from tools.linkedin_rate_limiter import LinkedInRateLimiter
from tools.operator_profile import (
    load_operator_profile,
    resolve_fields_with_profile_skill,
    to_screening_qa_bank,
)

logger = logging.getLogger(__name__)

DEFAULT_JOB_QUERIES = [
    "Agentic Security Engineer",
    "Offensive AI Security Engineer",
    "AI Red Team Engineer",
    "Agentic Cybersecurity Developer",
    "AI Threat Emulation Developer",
    "Autonomous Offensive Security",
    "LLM Red Team Engineer",
    "Adversarial AI Security Engineer",
    "Security Automation AI Agent",
    "AI Security Evaluator",
]

POSITIVE_KEYWORDS = {
    "agentic": 28,
    "ai red team": 30,
    "llm red team": 28,
    "threat emulation": 28,
    "adversarial": 24,
    "autonomous": 22,
    "security automation": 24,
    "offensive ai": 28,
    "offensive security": 18,
    "exploit": 20,
    "ai security": 26,
    "llm security": 26,
    "red team": 20,
    "red-team": 20,
    "rlhf": 20,
    "ai trainer": 18,
    "evaluator": 18,
    "evaluation": 14,
    "benchmark": 14,
    "mercor": 20,
    "outlier": 18,
    "alignerr": 18,
    "dataannotation": 18,
    "agent": 20,
    "llm": 18,
    "ai": 14,
    "python": 14,
    "security": 12,
    "cybersecurity": 12,
    "appsec": 12,
    "automation": 14,
    "remote": 10,
}

NEGATIVE_KEYWORDS = {
    "unpaid": -60,
    "intern ": -35,
    "clearance required: ts/sci": -35,
    "grc": -55,
    "governance, risk": -55,
    "ai risk": -45,
    "it audit": -55,
    "compliance analyst": -55,
    "soc analyst": -55,
    "faculty": -65,
    "instructor": -55,
    "program director": -55,
    "product manager": -50,
    "project manager": -55,
    "pki ": -45,
    "help desk": -60,
}

# Canonical Screening Q&A Bank loaded from single cross-platform config/operator_profile.json
APPROVED_SCREENING_QA_BANK: Dict[str, Any] = to_screening_qa_bank()


def match_screening_answer(
    question_label: str,
    input_type: str = "text",
    options: Optional[List[str]] = None,
    qa_bank: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Deterministically resolve an Easy Apply screening question from config/operator_profile.json."""
    bank = qa_bank or to_screening_qa_bank()
    q = (question_label or "").strip().lower()
    if not q:
        return None

    # 0. Phone Country Code & Phone / Mobile number fields from config/operator_profile.json
    if "country code" in q:
        return str((bank.get("contact") or {}).get("phone_country_code", "United States (+1)"))
    if input_type == "tel" or any(k in q for k in ("phone", "mobile", "telephone", "cell")):
        return str((bank.get("contact") or {}).get("phone", "4794039960"))

    # 1. Yes/No Radio or Select questions
    if any(k in q for k in ("sponsorship", "visa", "h-1b", "h1b", "require future")):
        ans = bank["work_authorization"]["require_sponsorship"]
        return ans
    if any(k in q for k in ("previously been employed", "previously employed", "former employee", "ever worked for", "currently employed by")):
        if "polito" in q:
            return "Yes"
        return "No"
    if any(k in q for k in ("bachelor", "master's", "masters degree", "phd", "doctorate")):
        if options:
            for o in options:
                if any(h in o.lower() for h in ("high school", "ged", "no degree")):
                    return o.strip()
        return str((bank.get("education_and_links") or {}).get("bachelor_degree", "No"))
    if any(k in q for k in ("highest level of education", "education level", "level of education")):
        if options:
            for o in options:
                if any(h in o.lower() for h in ("high school", "secondary", "ged")):
                    return o.strip()
        return str((bank.get("education_and_links") or {}).get("degree", "High School"))
    if any(k in q for k in (
        "authorized to work", "legally authorized", "eligible to work", "work authorization",
        "u.s. citizen", "us citizen", "citizenship",
        "commut", "relocat", "hybrid", "remote", "onsite", "on-site", "in-office", "in office", "work from office",
        "high school", "ged", "background check", "drug screen", "18 years",
    )):
        return "Yes"

    # 2. Numeric / Years of experience questions
    is_numeric = input_type in ("number", "tel") or any(
        k in q for k in ("how many years", "years of experience", "years of work", "years do you have", "number of years")
    )
    if is_numeric:
        if any(k in q for k in ("salary", "compensation", "annual", "base pay", "desired pay", "expectation")):
            return bank["compensation_and_notice"]["salary_annual"]
        if any(k in q for k in ("hourly", "rate per hour")):
            return bank["compensation_and_notice"]["hourly_rate"]
        if any(k in q for k in ("notice", "weeks")):
            return bank["compensation_and_notice"]["notice_period_weeks"]
        exp_map = bank["years_experience"]
        for skill, yrs in exp_map.items():
            if skill != "default_years" and skill in q:
                return str(yrs)
        return str(exp_map["default_years"])

    # 3. Text / URL / Location questions
    if any(k in q for k in ("salary", "compensation", "desired pay", "pay expectation")):
        return bank["compensation_and_notice"]["salary_annual"]
    if "linkedin" in q:
        return bank["education_and_links"]["linkedin_url"]
    if "github" in q or "portfolio" in q or "website" in q:
        return bank["education_and_links"]["github_url"]
    if any(k in q for k in ("current or most recent title", "current title", "most recent title", "current job title")):
        return bank["education_and_links"].get("current_title", "Senior Security Engineer")
    if any(k in q for k in ("current or most recent company", "current company", "most recent company", "current employer")):
        return bank["education_and_links"].get("current_company", "")
    if any(k in q for k in ("city", "current location", "where are you located")):
        return bank["location_and_workplace"]["city"]
    if "postal" in q or "zip" in q:
        return bank["location_and_workplace"]["zip"]
    if any(k in q for k in ("start date", "when can you start", "notice period", "availability")):
        return bank["compensation_and_notice"]["start_date"]
    if any(k in q for k in ("hear about", "heard about", "channel/event/medium", "how did you find")):
        if options:
            for o in options:
                ol = o.strip().lower()
                if "linkedin" in ol or "job board" in ol:
                    return o.strip()
            non_empty = [o.strip() for o in options if o and not re.match(r"^(?:select|choose|--|please select)", o.strip(), re.I)]
            if non_empty:
                return non_empty[0]
        return "LinkedIn"

    # 3b. Canonical EEO / Voluntary Self-Identification questions from config/operator_profile.json
    eeo = bank.get("eeo_demographics") or {}
    if any(k in q for k in ("gender", "sex", "pronoun")):
        target = str(eeo.get("gender", "Decline To Self Identify"))
        if options:
            for o in options:
                if any(p in o.lower() for p in ("decline", "prefer not", "don't wish", "do not wish", "choose not")):
                    return o.strip()
        return target
    if "hispanic" in q or "latino" in q:
        target = str(eeo.get("hispanic_latino", "No"))
        if options:
            for o in options:
                ol = o.strip().lower()
                if ol == "no" or ol.startswith("no,") or "not hispanic" in ol:
                    return o.strip()
        return target
    if any(k in q for k in ("race", "ethnicity", "racial")):
        target = str(eeo.get("race", "White"))
        if options:
            for o in options:
                if "white" in o.lower():
                    return o.strip()
        return target
    if "veteran" in q:
        target = str(eeo.get("veteran_status", "I am not a protected veteran"))
        if options:
            for o in options:
                ol = o.lower()
                if "not a protected veteran" in ol or "not a veteran" in ol or ol.strip() == "no":
                    return o.strip()
        return target
    if "disability" in q or "handicap" in q:
        target = str(eeo.get("disability_status", "No, I do not have a disability"))
        if options:
            for o in options:
                ol = o.lower()
                if "do not have a disability" in ol or "don't have a disability" in ol or ol.strip() == "no":
                    return o.strip()
        return target

    # 4. Dropdown with Yes/No options fallback
    if options:
        opt_lower = {o.strip().lower(): o.strip() for o in options if o and o.strip()}
        if "yes" in opt_lower and any(k in q for k in ("experience", "familiar", "proficient", "comfortable", "able to", "willing", "open to", "commut", "relocat")):
            return opt_lower["yes"]

    # 5. Dynamic Universal RSI Learned Screening Q&A Patterns
    try:
        from tools.universal_rsi_reflector import load_universal_rsi_state
        learned_patterns = load_universal_rsi_state().get("learned_qa_patterns") or {}
        for pattern, ans in learned_patterns.items():
            if pattern and pattern.lower() in q:
                return str(ans)
    except Exception:
        pass
    return None


def score_job_posting(title: str, company: str, location: str, snippet: str = "", easy_apply: bool = False) -> int:
    text = f"{title} {company} {location} {snippet}".lower()
    score = 38
    if easy_apply:
        score += 10
    kw_map = dict(POSITIVE_KEYWORDS)
    try:
        from tools.universal_rsi_reflector import load_universal_rsi_state
        evolved_kw = load_universal_rsi_state().get("evolved_keyword_weights") or {}
        for k, v in evolved_kw.items():
            kw_map[str(k).lower()] = int(v)
    except Exception:
        pass
    for kw, weight in kw_map.items():
        if kw in text:
            score += weight
    for kw, penalty in NEGATIVE_KEYWORDS.items():
        if kw in text:
            score += penalty

    # Demote traditional manual pentesting / generic cybersecurity listings that lack AI / Agentic / LLM engineering signals
    has_ai_agentic_signal = bool(
        re.search(r"\b(ai|llm|agent|agentic|autonomous|ml|machine learning|rlhf|gen\s*ai|genai|adversarial)\b", text)
    )
    if not has_ai_agentic_signal:
        score -= 28
    return max(0, min(100, score))


def build_tailored_note(title: str, company: str) -> str:
    try:
        from tools.universal_rsi_reflector import load_universal_rsi_state
        tpl = load_universal_rsi_state().get("evolved_tailored_note_template")
        if tpl and "{company}" in tpl and "{title}" in tpl:
            return tpl.format(company=company, title=title)
    except Exception:
        pass
    return (
        f"Hi {company} team — I'm applying for the {title} role. My background spans "
        f"production AI/LLM systems, autonomous agent orchestration, and application/cybersecurity "
        f"engineering in Python and TypeScript. Happy to share code samples or discuss architecture fit."
    )


class LinkedInJobsWorker:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.store = runtime.store
        self.rate_limiter = LinkedInRateLimiter.get_instance()

    def discover_and_stage(
        self,
        queries: Optional[List[str]] = None,
        location: str = "United States",
        max_jobs: int = 6,
        easy_apply_only: bool = True,
        auto_stage_min_score: int = 65,
    ) -> Dict[str, Any]:
        """Search LinkedIn Jobs, score listings, scroll naturally, and stage top matches."""
        allowed, quota_msg = self.rate_limiter.check_quota("job_search")
        if not allowed:
            return {"status": "skipped", "reason": quota_msg, "jobs_discovered": 0, "jobs_staged": 0}

        evolved_queries = None
        if not queries:
            try:
                from tools.universal_rsi_reflector import load_universal_rsi_state
                evolved_queries = load_universal_rsi_state().get("evolved_job_queries")
            except Exception:
                evolved_queries = None
        search_queries = queries or evolved_queries or DEFAULT_JOB_QUERIES
        chosen_query = random.choice(search_queries)
        max_jobs = max(1, min(int(max_jobs), 10))

        try:
            page = self.runtime.manager._ensure_browser_page()
            ok, auth_detail = self.runtime.manager.stealth.is_logged_in(page)
            if not ok:
                return {"status": "blocked", "reason": f"LinkedIn not authenticated: {auth_detail}"}

            params = {"keywords": chosen_query, "location": location}
            if easy_apply_only:
                params["f_AL"] = "true"
            search_url = "https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params)

            self.rate_limiter.wait_jitter("job_search")
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2.2, 4.0))

            # Check for security challenge / checkpoint
            cur_url = (page.url or "").lower()
            if "checkpoint" in cur_url or "authwall" in cur_url:
                self.rate_limiter.trip_circuit_breaker("LinkedIn checkpoint detected during job search")
                return {"status": "blocked", "reason": "LinkedIn checkpoint detected; circuit breaker tripped"}

            # Natural human scroll on job search list to avoid zero-scroll bot signature
            for _ in range(random.randint(2, 4)):
                page.mouse.wheel(0, random.randint(260, 520))
                time.sleep(random.uniform(0.7, 1.6))

            raw_cards = page.evaluate(
                """(limit) => {
                    const seen = new Set();
                    const results = [];
                    // 1. Modern LinkedIn SDUI job cards: [componentkey^="job-card-component-ref-"]
                    const sduiCards = Array.from(document.querySelectorAll('[componentkey^="job-card-component-ref-"]'));
                    for (const card of sduiCards) {
                        const ck = card.getAttribute('componentkey') || '';
                        const m = ck.match(/job-card-component-ref-(\\d+)/);
                        const jobId = m && m[1];
                        if (!jobId || seen.has(jobId)) continue;
                        const cardText = (card.innerText || '').trim();
                        const lines = cardText.split('\\n').map(s => s.trim()).filter(Boolean);
                        if (lines.length < 2) continue;
                        seen.add(jobId);
                        // First line is often screen-reader duplicate or 'Selected, ...'; second line is clean title
                        let title = lines[0].replace(/^Selected,\\s*/i, '').replace(/\\s*\\(Verified job\\)$/i, '').trim();
                        let company = lines[1] || 'LinkedIn Listing';
                        let loc = lines[2] || '';
                        if (lines.length >= 4 && lines[1] && title.toLowerCase().startsWith(lines[1].toLowerCase().slice(0, 12))) {
                            title = lines[1];
                            company = lines[2] || 'LinkedIn Listing';
                            loc = lines[3] || '';
                        }
                        const easy = /easy apply/i.test(cardText);
                        const alreadyApplied = /\bapplied\b|application submitted/i.test(cardText);
                        results.push({
                            job_id: String(jobId),
                            title: title.slice(0, 200),
                            company: company.slice(0, 160),
                            location: loc.slice(0, 160),
                            job_url: `https://www.linkedin.com/jobs/view/${jobId}/`,
                            easy_apply: easy,
                            already_applied: alreadyApplied,
                            snippet: cardText.slice(0, 500)
                        });
                        if (results.length >= limit) return results;
                    }
                    // 2. Fallback: classic /jobs/view/ or currentJobId= links
                    const links = Array.from(document.querySelectorAll('a[href*="/jobs/view/"], a[href*="currentJobId="]'));
                    for (const a of links) {
                        const href = a.href || a.getAttribute('href') || '';
                        const m = href.match(/\\/jobs\\/view\\/(\\d+)/) || href.match(/currentJobId=(\\d+)/);
                        const card = a.closest('li, div[data-job-id], .job-card-container') || a.parentElement;
                        const dataJobId = card && card.getAttribute ? card.getAttribute('data-job-id') : null;
                        const jobId = (m && m[1]) || dataJobId;
                        if (!jobId || seen.has(jobId)) continue;
                        seen.add(jobId);
                        const cardText = (card ? card.innerText : a.innerText) || '';
                        const lines = cardText.split('\\n').map(s => s.trim()).filter(Boolean);
                        const title = (a.innerText || lines[0] || 'Untitled Role').split('\\n')[0].trim();
                        const company = lines[1] || 'LinkedIn Listing';
                        const loc = lines[2] || '';
                        const easy = /easy apply/i.test(cardText);
                        const alreadyApplied = /\bapplied\b|application submitted/i.test(cardText);
                        results.push({
                            job_id: String(jobId),
                            title: title.slice(0, 200),
                            company: company.slice(0, 160),
                            location: loc.slice(0, 160),
                            job_url: `https://www.linkedin.com/jobs/view/${jobId}/`,
                            easy_apply: easy,
                            already_applied: alreadyApplied,
                            snippet: cardText.slice(0, 500)
                        });
                        if (results.length >= limit) break;
                    }
                    return results;
                }""",
                max_jobs,
            )

            self.rate_limiter.record_action("job_search", {"query": chosen_query, "found": len(raw_cards or [])})
            discovered = []
            staged = []
            existing_by_id = {
                str(r.get("job_id")): r for r in self.store.list_job_applications(limit=500)
            }

            for item in raw_cards or []:
                jid = str(item["job_id"])
                existing = existing_by_id.get(jid)
                existing_status = (existing or {}).get("status")
                score = score_job_posting(
                    item.get("title", ""),
                    item.get("company", ""),
                    item.get("location", ""),
                    item.get("snippet", ""),
                    bool(item.get("easy_apply")),
                )
                if existing_status in (
                    "submitted",
                    "skipped",
                    "external_ats",
                    "external_in_progress",
                    "awaiting_human_captcha",
                    "awaiting_screening_input",
                ):
                    # Never overwrite an already-submitted or terminal job back to staged
                    status = existing_status
                elif item.get("already_applied"):
                    status = "submitted"
                else:
                    status = "staged" if score >= auto_stage_min_score else "discovered"
                draft_note = (existing or {}).get("draft_note") or build_tailored_note(item.get("title", "Engineer"), item.get("company", "Hiring"))
                saved = self.store.upsert_job_application({
                    "job_id": jid,
                    "title": item.get("title"),
                    "company": item.get("company"),
                    "location": item.get("location"),
                    "job_url": item.get("job_url"),
                    "easy_apply": bool(item.get("easy_apply")),
                    "match_score": score,
                    "status": status,
                    "draft_note": draft_note,
                    "receipt": (existing or {}).get("receipt") or {"query": chosen_query, "observed_at": time.time()},
                })
                discovered.append(saved)
                if status == "staged":
                    staged.append(saved)

            return {
                "status": "success",
                "query": chosen_query,
                "jobs_discovered": len(discovered),
                "jobs_staged": len(staged),
                "funnel": self.store.job_funnel_counts(),
                "sample": discovered[:5],
            }
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "query": chosen_query}

    def inspect_and_prepare_application(self, job_id: str) -> Dict[str, Any]:
        """Open a staged job listing, scroll description naturally, verify Easy Apply button, and stage ready-to-submit state."""
        allowed, quota_msg = self.rate_limiter.check_quota("job_apply")
        if not allowed:
            return {"status": "skipped", "reason": quota_msg, "job_id": job_id}

        rows = [r for r in self.store.list_job_applications(limit=200) if str(r.get("job_id")) == str(job_id)]
        if not rows:
            return {"status": "failed", "error": f"Job {job_id} not found in job_applications"}
        job = rows[0]

        try:
            page = self.runtime.manager._ensure_browser_page()
            self.rate_limiter.wait_jitter("profile_view")
            page.goto(job["job_url"], wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(2.5, 4.5))

            # Anti-bot: natural scroll through job description before inspecting Easy Apply button
            for _ in range(random.randint(2, 4)):
                page.mouse.wheel(0, random.randint(220, 460))
                time.sleep(random.uniform(0.8, 1.7))

            inspect_info = page.evaluate(
                """() => {
                    const bodyText = (document.body && document.body.innerText) || '';
                    const alreadyApplied = /applied\\s+\\d+|application submitted/i.test(bodyText);
                    const easyBtn = Array.from(document.querySelectorAll('button, a')).find(
                        el => /easy apply/i.test(el.innerText || el.getAttribute('aria-label') || '')
                    );
                    return {
                        already_applied: alreadyApplied,
                        has_easy_apply_button: Boolean(easyBtn),
                        page_title: document.title || '',
                        url: location.href
                    };
                }"""
            )

            new_status = "submitted" if inspect_info.get("already_applied") else "staged_ready" if inspect_info.get("has_easy_apply_button") else "external_ats"
            updated = self.store.upsert_job_application({
                **job,
                "easy_apply": bool(inspect_info.get("has_easy_apply_button") or job.get("easy_apply")),
                "status": new_status,
                "receipt": {
                    **(job.get("receipt") if isinstance(job.get("receipt"), dict) else {}),
                    "inspected_at": time.time(),
                    "inspect_info": inspect_info,
                },
            })
            return {"status": "success", "job": updated, "inspect_info": inspect_info}
        except Exception as exc:
            self.store.upsert_job_application({**job, "error": str(exc)})
            return {"status": "failed", "job_id": job_id, "error": str(exc)}

    def sweep_open_external_tabs(self, base_page: Any) -> Optional[Dict[str, Any]]:
        """Autonomously finish and close any open external career/ATS tabs in Chrome (deduplicating duplicates first)."""
        try:
            ctx = base_page.context
            pages = list(ctx.pages)
        except Exception:
            return None

        protected_domains = (
            "linkedin.com", "youtube.com", "google.com", "gmail.com", "github.com",
            "x.com", "twitter.com", "claude.ai", "chatgpt.com", "127.0.0.1", "localhost",
        )
        career_signal = re.compile(
            r"(greenhouse\.io|ashbyhq\.com|lever\.co|rippling\.com|myworkdayjobs\.com|eightfold\.ai|"
            r"icims\.com|smartrecruiters\.com|appcast\.io|myjobs\.adp\.com|apply\.deloitte\.com|"
            r"kpmguscareers\.com|huntington-careers\.com|elpho\.io|accenture\.com/.+job|"
            r"://careers\.|/careers/|/jobdetails|/jobs/|/job/)",
            re.I,
        )

        seen_urls: set[str] = set()
        career_tabs: List[Any] = []
        for pg in pages:
            if pg is base_page:
                continue
            u = (pg.url or "").strip()
            ul = u.lower()
            if not u or ul.startswith(("chrome://", "devtools://", "edge://", "about:")):
                continue
            if any(d in ul for d in protected_domains):
                continue
            if not career_signal.search(ul):
                continue
            norm = ul.split("#")[0].rstrip("/")
            if norm in seen_urls:
                try:
                    if not pg.is_closed():
                        pg.close()
                except Exception:
                    pass
                continue
            seen_urls.add(norm)
            career_tabs.append(pg)

        if not career_tabs:
            return None

        def _tab_priority(pg: Any) -> int:
            ul = (pg.url or "").lower()
            if any(k in ul for k in ("ashbyhq.com", "greenhouse.io", "lever.co", "rippling.com")):
                return 0
            if any(k in ul for k in ("myworkdayjobs.com", "eightfold.ai", "icims.com", "smartrecruiters.com")):
                return 1
            return 2

        career_tabs.sort(key=_tab_priority)
        target_tab = None
        tab_url = ""
        tab_title = "External ATS Role"
        tab_score = 80
        for cand_tab in career_tabs:
            cu = (cand_tab.url or "").strip()
            try:
                ct = (cand_tab.title() or "External ATS Role").strip()[:180]
            except Exception:
                ct = "External ATS Role"
            sc = score_job_posting(ct, "", cu)
            if sc < 60:
                # Off-target leftover tab (e.g. GRC, Human Risk, non-AI pentest) — close cleanly without applying
                try:
                    if not cand_tab.is_closed():
                        cand_tab.close()
                except Exception:
                    pass
                continue
            target_tab = cand_tab
            tab_url = cu
            tab_title = ct
            tab_score = sc
            break

        if target_tab is None:
            return None

        existing = self.store.list_job_applications(limit=300)
        matched_job: Optional[Dict[str, Any]] = None
        for r in existing:
            ru = str(r.get("job_url") or "").rstrip("/").lower()
            if ru and (ru in tab_url.lower() or tab_url.rstrip("/").lower() in ru):
                matched_job = r
                break
        if matched_job is None:
            host = urllib.parse.urlsplit(tab_url).hostname or "External Career Portal"
            company_guess = host.replace("www.", "").replace("careers.", "").replace("jobs.", "").replace("apply.", "").split(".")[0].title()
            jid = "exttab_" + hashlib.sha256(tab_url.encode("utf-8")).hexdigest()[:10]
            matched_job = self.store.upsert_job_application({
                "job_id": jid,
                "title": tab_title,
                "company": company_guess,
                "location": "United States",
                "job_url": tab_url,
                "easy_apply": False,
                "match_score": tab_score,
                "status": "external_ats",
                "draft_note": build_tailored_note(tab_title, company_guess),
                "receipt": {"source": "open_tab_sweep", "observed_at": time.time()},
            })

        ext_status = "external_in_progress"
        try:
            from tools.external_ats_worker import ExternalAtsWorker
            try:
                target_tab.bring_to_front()
            except Exception:
                pass
            ats_worker = ExternalAtsWorker(self.runtime)
            ext_res = ats_worker.apply_on_external_page(target_tab, matched_job)
            ext_status = ext_res.get("status", "external_in_progress")
            updated_ext = self.store.upsert_job_application({
                **matched_job,
                "status": ext_status,
                "receipt": {
                    **(matched_job.get("receipt") if isinstance(matched_job.get("receipt"), dict) else {}),
                    "external_ats": ext_res,
                    "swept_from_open_tab": True,
                    "submitted_at": time.time() if ext_status == "submitted" else None,
                },
                "error": ext_res.get("reason"),
            })
            if ext_status == "submitted":
                self.rate_limiter.record_action("job_apply", {
                    "job_id": matched_job["job_id"],
                    "title": matched_job.get("title"),
                    "company": matched_job.get("company"),
                    "external_url": ext_res.get("external_url") or tab_url,
                })
            return {
                "status": "success" if ext_status == "submitted" else "partial",
                "application_status": ext_status,
                "job_id": matched_job["job_id"],
                "title": matched_job.get("title"),
                "company": matched_job.get("company"),
                "job_url": tab_url,
                "external_ats": ext_res,
                "remaining_open_career_tabs": max(0, len(career_tabs) - 1),
                "job": updated_ext,
            }
        except Exception as sweep_exc:
            logger.warning("Open external tab sweep failed on %s: %s", tab_url, sweep_exc)
            return None
        finally:
            if ext_status != "awaiting_human_captcha":
                try:
                    if not target_tab.is_closed():
                        target_tab.close()
                except Exception:
                    pass

    def apply_to_job_end_to_end(self, job_id: Optional[str] = None, max_steps: int = 8) -> Dict[str, Any]:
        """Execute an end-to-end Easy Apply or External ATS job application (uncapped) + hiring team outreach."""
        allowed, quota_msg = self.rate_limiter.check_quota("job_apply")
        if not allowed:
            return {"status": "skipped", "reason": quota_msg}

        page = self.runtime.manager._ensure_browser_page()
        if not job_id:
            swept = self.sweep_open_external_tabs(page)
            if swept is not None:
                return swept

        all_jobs = self.store.list_job_applications(limit=200)
        if job_id:
            candidates = [r for r in all_jobs if str(r.get("job_id")) == str(job_id)]
        else:
            # Include both Easy Apply AND External ATS portal jobs ('staged', 'staged_ready', 'external_ats'),
            # never getting stuck on a single portal because every attempted job transitions out of staged.
            candidates = [
                r for r in all_jobs
                if r.get("status") in ("staged", "staged_ready", "external_ats")
            ]
            candidates.sort(key=lambda r: int(r.get("match_score") or 0), reverse=True)

        if not candidates:
            return {"status": "skipped", "reason": "No staged Easy Apply or External ATS jobs available in queue"}

        try:
            page.bring_to_front()
        except Exception:
            pass

        job = None
        target_id = ""
        found_apply_target = False
        hiring_info: Dict[str, Any] = {"has_hiring_contact": False}
        steps_trace: List[str] = []

        try:
            for cand in candidates:
                job = cand
                target_id = str(cand["job_id"])
                steps_trace = []
                cand_url = str(cand.get("job_url") or "").strip()

                # 0. Direct External Portal / AI-Eval / RLHF URL (Mercor, Outlier, DataAnnotation, Alignerr, Greenhouse, etc.)
                if cand_url and "linkedin.com" not in cand_url.lower():
                    ext_page = None
                    ext_status = "external_in_progress"
                    try:
                        from tools.external_ats_worker import ExternalAtsWorker
                        ext_page = page.context.new_page()
                        try:
                            ext_page.bring_to_front()
                        except Exception:
                            pass
                        ext_page.goto(cand_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(random.uniform(2.5, 3.6))
                        ats_worker = ExternalAtsWorker(self.runtime)
                        ext_res = ats_worker.apply_on_external_page(ext_page, cand)
                        ext_status = ext_res.get("status", "external_in_progress")
                        updated_ext = self.store.upsert_job_application({
                            **cand,
                            "status": ext_status,
                            "receipt": {
                                **(cand.get("receipt") if isinstance(cand.get("receipt"), dict) else {}),
                                "external_ats": ext_res,
                                "submitted_at": time.time() if ext_status == "submitted" else None,
                            },
                            "error": ext_res.get("reason"),
                        })
                        if ext_status == "submitted":
                            self.rate_limiter.record_action("job_apply", {
                                "job_id": target_id,
                                "title": cand.get("title"),
                                "company": cand.get("company"),
                                "external_url": ext_res.get("external_url") or cand_url,
                            })
                        return {
                            "status": "success" if ext_status == "submitted" else "partial",
                            "application_status": ext_status,
                            "job_id": target_id,
                            "title": cand.get("title"),
                            "company": cand.get("company"),
                            "job_url": cand_url,
                            "external_ats": ext_res,
                            "job": updated_ext,
                        }
                    except Exception as direct_exc:
                        logger.warning("Direct external portal apply failed on %s (%s): %s", target_id, cand_url, direct_exc)
                        self.store.upsert_job_application({
                            **cand,
                            "status": "external_in_progress",
                            "error": str(direct_exc),
                        })
                        if job_id:
                            return {"status": "failed", "job_id": target_id, "error": str(direct_exc)}
                        continue
                    finally:
                        if ext_page is not None and ext_status != "awaiting_human_captcha":
                            try:
                                if not ext_page.is_closed():
                                    ext_page.close()
                            except Exception:
                                pass

                # Prefer a tab already viewing this job if present
                try:
                    ctx = page.context
                    for pg in ctx.pages:
                        if target_id in (pg.url or ""):
                            page = pg
                            break
                except Exception:
                    pass

                if target_id not in (page.url or ""):
                    page.goto(cand["job_url"], wait_until="domcontentloaded", timeout=30000)
                    time.sleep(random.uniform(2.5, 3.8))
                    for _ in range(2):
                        page.mouse.wheel(0, random.randint(220, 420))
                        time.sleep(random.uniform(0.8, 1.4))

                # 1. Inspect Hiring Team / Recruiter on the job page
                hiring_info = page.evaluate(
                    """() => {
                        const msgLink = document.querySelector('a[href*="JOB_DETAILS_HIRING_TEAM"]');
                        if (!msgLink) return {has_hiring_contact: false};
                        let p = msgLink;
                        for (let i = 0; i < 4 && p.parentElement; i++) p = p.parentElement;
                        const lines = ((p.innerText || '').trim()).split('\\n').map(s => s.trim()).filter(Boolean);
                        return {
                            has_hiring_contact: true,
                            compose_url: msgLink.href || msgLink.getAttribute('href'),
                            lines: lines.slice(0, 8)
                        };
                    }"""
                )

                # 2. Check if already applied or if Submit / Review / Next / Easy Apply is visible
                submit_loc = page.locator('button:has-text("Submit application")')
                next_or_review = page.locator('button:has-text("Next"), button:has-text("Review")')
                if submit_loc.count() == 0 and next_or_review.count() == 0:
                    easy_btn = page.locator('button[aria-label*="Easy Apply" i], button:has-text("Easy Apply")').first
                    if easy_btn.count() == 0:
                        already_on_page = False
                        try:
                            already_on_page = bool(
                                page.evaluate(
                                    "() => /applied\\s+\\d+|application submitted|your application was sent/i.test((document.body && document.body.innerText) || '')"
                                )
                            )
                        except Exception:
                            already_on_page = False
                        if not already_on_page:
                            ext_btn = page.locator(
                                'button[aria-label^="Apply" i]:not([aria-label*="Easy Apply" i]), '
                                'a[aria-label^="Apply" i]:not([aria-label*="Easy Apply" i]), '
                                '.jobs-apply-button--top-card button:not([aria-label*="Easy Apply" i]), '
                                '.jobs-s-apply button:not([aria-label*="Easy Apply" i]), '
                                'button:has-text("Apply"):not(:has-text("Easy Apply")), '
                                'a:has-text("Apply"):not(:has-text("Easy Apply"))'
                            ).first
                            if ext_btn.count() > 0 and ext_btn.is_visible():
                                ext_page = None
                                ext_status = "external_in_progress"
                                before_pages_set = set(page.context.pages)
                                try:
                                    from tools.external_ats_worker import ExternalAtsWorker
                                    ext_btn.click(timeout=5000)
                                    # Handle optional LinkedIn 'Share your profile? -> Continue to apply' interstitial modal
                                    time.sleep(1.2)
                                    cont_modal = page.locator('button:has-text("Continue to apply"), a:has-text("Continue to apply")').first
                                    if cont_modal.count() > 0 and cont_modal.is_visible():
                                        try:
                                            cont_modal.click(timeout=3000)
                                        except Exception:
                                            pass

                                    # Poll up to 12s for the newly opened popup tab (regardless of tab bar position!) to finish redirecting off linkedin.com
                                    for _ in range(24):
                                        spawned = [p for p in page.context.pages if p not in before_pages_set]
                                        if spawned:
                                            ext_page = spawned[-1]
                                            eu = (ext_page.url or "").strip().lower()
                                            if eu and not eu.startswith("about:") and "linkedin.com" not in eu:
                                                break
                                        elif "linkedin.com" not in (page.url or "").lower():
                                            ext_page = page
                                            break
                                        time.sleep(0.5)

                                    if ext_page is not None and "linkedin.com" not in (ext_page.url or "").lower():
                                        time.sleep(1.8)
                                        ats_worker = ExternalAtsWorker(self.runtime)
                                        ext_res = ats_worker.apply_on_external_page(ext_page, cand)
                                        ext_status = ext_res.get("status", "external_in_progress")
                                        updated_ext = self.store.upsert_job_application({
                                            **cand,
                                            "status": ext_status,
                                            "receipt": {
                                                **(cand.get("receipt") if isinstance(cand.get("receipt"), dict) else {}),
                                                "external_ats": ext_res,
                                                "submitted_at": time.time() if ext_status == "submitted" else None,
                                            },
                                            "error": ext_res.get("reason"),
                                        })
                                        if ext_status == "submitted":
                                            self.rate_limiter.record_action("job_apply", {
                                                "job_id": target_id,
                                                "title": cand.get("title"),
                                                "company": cand.get("company"),
                                                "external_url": ext_res.get("external_url"),
                                            })
                                        return {
                                            "status": "success" if ext_status == "submitted" else "partial",
                                            "application_status": ext_status,
                                            "job_id": target_id,
                                            "title": cand.get("title"),
                                            "company": cand.get("company"),
                                            "job_url": cand.get("job_url"),
                                            "external_ats": ext_res,
                                            "job": updated_ext,
                                        }
                                except Exception as ext_exc:
                                    logger.warning("External ATS apply fallback failed on %s: %s", target_id, ext_exc)
                                finally:
                                    if ext_status != "awaiting_human_captcha":
                                        for sp in list(page.context.pages):
                                            if sp not in before_pages_set and sp is not page:
                                                try:
                                                    if not sp.is_closed():
                                                        sp.close()
                                                except Exception:
                                                    pass
                        terminal_status = "submitted" if already_on_page else "skipped"
                        self.store.upsert_job_application({
                            **cand,
                            "status": terminal_status,
                            "error": None if already_on_page else "Easy Apply button not present on listing",
                        })
                        if job_id:
                            return {
                                "status": "skipped",
                                "job_id": target_id,
                                "reason": "Already applied on LinkedIn" if already_on_page else "Easy Apply button not present on listing",
                            }
                        # Continue immediately to the next staged candidate in the queue!
                        continue
                    easy_btn.click(timeout=6000)
                    steps_trace.append("clicked_easy_apply")
                    time.sleep(random.uniform(2.0, 3.0))

                found_apply_target = True
                break

            if not found_apply_target or job is None:
                return {"status": "skipped", "reason": "All candidate listings were already applied to or lacked Easy Apply"}

            # 3. Step through Next / Review -> Submit application (single-pass per field + model profile skill)
            submitted = False
            resume_handled = False
            attempted_keys: set[str] = set()
            skill_attempted_keys: set[str] = set()
            for step_idx in range(max_steps):
                if not resume_handled:
                    res_step = self.ensure_updated_resume_on_easy_apply(page)
                    if res_step:
                        steps_trace.append(res_step)
                        resume_handled = True
                        time.sleep(random.uniform(1.2, 2.0))

                filled = self.autofill_screening_questions(
                    page,
                    attempted_keys=attempted_keys,
                    skill_attempted_keys=skill_attempted_keys,
                )
                if filled:
                    steps_trace.extend(filled)
                    time.sleep(random.uniform(0.8, 1.4))

                if page.locator('button:has-text("Submit application")').count() > 0:
                    steps_trace.append("clicking_submit_application")
                    page.locator('button:has-text("Submit application")').first.click(timeout=6000)
                    time.sleep(random.uniform(3.0, 4.2))
                    submitted = True
                    break

                rev_btn = page.locator('button:has-text("Review")')
                if rev_btn.count() > 0 and rev_btn.first.is_visible():
                    steps_trace.append(f"step_{step_idx+1}_clicked_review")
                    rev_btn.first.click(timeout=5000)
                    time.sleep(random.uniform(2.0, 3.0))
                    err_banner = page.evaluate(
                        r"""() => {
                            const roots = [document];
                            for (const el of document.querySelectorAll('*')) { if (el.shadowRoot) roots.push(el.shadowRoot); }
                            let modal = null;
                            for (const r of roots) {
                                modal = r.querySelector('.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"], dialog[open], [data-sdui-screen*="EasyApply"], .artdeco-modal:not(.msg-overlay-conversation-bubble)');
                                if (modal) break;
                            }
                            const scope = modal || document.body;
                            if (Array.from(scope.querySelectorAll('button')).some(b => /submit\s+application/i.test(b.innerText || ''))) return '';
                            const errs = Array.from(scope.querySelectorAll('.artdeco-inline-feedback--error, [role="alert"], [aria-invalid="true"]'))
                                .map(e => (e.innerText || e.getAttribute('aria-label') || 'invalid_field').trim())
                                .filter(t => t && !/successfully|resume\s+uploaded|saved/i.test(t));
                            if (errs.length) return errs.join(' | ');
                            const txt = (scope && scope.innerText) || '';
                            return /enter a valid|this field is required|please make a selection/i.test(txt) ? txt.slice(0, 300) : '';
                        }"""
                    )
                    if err_banner:
                        retry_filled = self.autofill_screening_questions(
                            page,
                            attempted_keys=attempted_keys,
                            skill_attempted_keys=skill_attempted_keys,
                            inline_error_text=err_banner,
                            use_model_skill=True,
                        )
                        if retry_filled:
                            steps_trace.extend(retry_filled)
                            time.sleep(random.uniform(0.8, 1.4))
                            continue
                        steps_trace.append("paused_on_required_custom_question")
                        break
                    continue

                nxt_btn = page.locator('button:has-text("Next")')
                if nxt_btn.count() > 0 and nxt_btn.first.is_visible():
                    steps_trace.append(f"step_{step_idx+1}_clicked_next")
                    nxt_btn.first.click(timeout=5000)
                    time.sleep(random.uniform(2.0, 3.0))
                    err_banner = page.evaluate(
                        r"""() => {
                            const roots = [document];
                            for (const el of document.querySelectorAll('*')) { if (el.shadowRoot) roots.push(el.shadowRoot); }
                            let modal = null;
                            for (const r of roots) {
                                modal = r.querySelector('.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"], dialog[open], [data-sdui-screen*="EasyApply"], .artdeco-modal:not(.msg-overlay-conversation-bubble)');
                                if (modal) break;
                            }
                            const scope = modal || document.body;
                            if (Array.from(scope.querySelectorAll('button')).some(b => /submit\s+application/i.test(b.innerText || ''))) return '';
                            const errs = Array.from(scope.querySelectorAll('.artdeco-inline-feedback--error, [role="alert"], [aria-invalid="true"]'))
                                .map(e => (e.innerText || e.getAttribute('aria-label') || 'invalid_field').trim())
                                .filter(t => t && !/successfully|resume\s+uploaded|saved/i.test(t));
                            if (errs.length) return errs.join(' | ');
                            const txt = (scope && scope.innerText) || '';
                            return /enter a valid|this field is required|please make a selection/i.test(txt) ? txt.slice(0, 300) : '';
                        }"""
                    )
                    if err_banner:
                        retry_filled = self.autofill_screening_questions(
                            page,
                            attempted_keys=attempted_keys,
                            skill_attempted_keys=skill_attempted_keys,
                            inline_error_text=err_banner,
                            use_model_skill=True,
                        )
                        if retry_filled:
                            steps_trace.extend(retry_filled)
                            time.sleep(random.uniform(0.8, 1.4))
                            continue
                        steps_trace.append("paused_on_required_custom_question")
                        break
                    continue
                break

            post_text = page.evaluate(
                """() => {
                    const chunks = [(document.body && document.body.innerText) || ''];
                    for (const el of document.querySelectorAll('*')) {
                        if (el.shadowRoot) chunks.push(el.shadowRoot.textContent || '');
                    }
                    return chunks.join(' ').slice(0, 3000);
                }"""
            )
            confirmed = submitted and bool(re.search(r"application sent|application submitted|applied|your application was sent", post_text, re.I))

            # Dismiss post-submission modal if present so page is clean
            if submitted:
                try:
                    done_btn = page.locator('button:has-text("Done"), button[aria-label="Dismiss"]').first
                    if done_btn.count() > 0 and done_btn.is_visible():
                        done_btn.click(timeout=3000)
                        time.sleep(1.0)
                except Exception:
                    pass

            final_status = "submitted" if (submitted or confirmed) else "awaiting_screening_input"
            if final_status == "submitted":
                self.rate_limiter.record_action("job_apply", {
                    "job_id": target_id,
                    "title": job.get("title"),
                    "company": job.get("company"),
                })

            receipt = {
                **(job.get("receipt") if isinstance(job.get("receipt"), dict) else {}),
                "submitted_at": time.time() if final_status == "submitted" else None,
                "confirmed_banner": confirmed,
                "steps_trace": steps_trace,
                "hiring_team": hiring_info,
            }
            updated = self.store.upsert_job_application({
                **job,
                "status": final_status,
                "receipt": receipt,
                "error": None if final_status == "submitted" else "Paused at custom screening question for human review",
            })
            return {
                "status": "success" if final_status == "submitted" else "partial",
                "application_status": final_status,
                "job_id": target_id,
                "title": job.get("title"),
                "company": job.get("company"),
                "job_url": job.get("job_url"),
                "steps_trace": steps_trace,
                "hiring_team": hiring_info,
                "job": updated,
                "quota_after": self.rate_limiter.check_quota("job_apply")[1],
            }
        except Exception as exc:
            if job:
                self.store.upsert_job_application({**job, "error": str(exc)})
            return {"status": "failed", "job_id": target_id, "error": str(exc), "steps_trace": steps_trace}

    def ensure_updated_resume_on_easy_apply(self, page: Any) -> Optional[str]:
        """On LinkedIn Easy Apply's Resume step, select or upload `Charles_McGowen_Resume_2026.pdf` so old resumes are never used."""
        from pathlib import Path
        root = Path(getattr(self.runtime, "data_dir", None) or os.environ.get("SABRINA_SOCIAL_DATA_DIR", "data/social"))
        candidates = [
            root / "Charles_McGowen_Resume_2026.pdf",
            Path("/home/ego/bot/Sabrina-windows-sync/data/social/Charles_McGowen_Resume_2026.pdf"),
            Path("c:/Users/bcmcg/Desktop/bot/Sabrina/data/social/Charles_McGowen_Resume_2026.pdf"),
            root / "charles_resume.pdf",
        ]
        resume_pdf = next((p for p in candidates if p.exists()), None)
        if not resume_pdf:
            return None
        target_name = resume_pdf.name
        pdf_payload = {
            "name": target_name,
            "mimeType": "application/pdf",
            "buffer": resume_pdf.read_bytes(),
        }
        try:
            state = page.evaluate(
                r"""(targetName) => {
                    const roots = [document];
                    for (const el of document.querySelectorAll('*')) { if (el.shadowRoot) roots.push(el.shadowRoot); }
                    let modal = null;
                    for (const r of roots) {
                        modal = r.querySelector('.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"], dialog[open], [data-sdui-screen*="EasyApply"], .artdeco-modal:not(.msg-overlay-conversation-bubble)');
                        if (modal) break;
                    }
                    if (!modal) return {in_modal: false};
                    const txt = (modal.innerText || '');
                    const fileInputs = Array.from(modal.querySelectorAll('input[type="file"]')).filter((el, i) => {
                        const meta = ((el.id || '') + ' ' + (el.name || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase();
                        if (meta.includes('cover') || meta.includes('attachment-input-ember')) return false;
                        el.setAttribute('data-sabrina-resume-input', String(i));
                        return true;
                    });
                    const uploadBtn = Array.from(modal.querySelectorAll('button, label[role="button"]')).find(b => {
                        const bt = ((b.innerText || '') + ' ' + (b.getAttribute('aria-label') || '')).replace(/\s+/g, ' ').trim();
                        return /^upload\s+resume$/i.test(bt) || /upload\s+resume/i.test(bt);
                    });
                    if (uploadBtn) uploadBtn.setAttribute('data-sabrina-upload-resume-btn', '1');
                    const resumeRadios = Array.from(modal.querySelectorAll('input[type="radio"]')).filter(r =>
                        /\.(pdf|docx?)\b/i.test(r.getAttribute('aria-label') || '')
                    );
                    const isResumeStep = Boolean(fileInputs.length || uploadBtn || resumeRadios.length);
                    if (!isResumeStep) return {in_modal: true, has_resume_step: false};

                    // Tag target resume radio/button if already listed on the page
                    const targetRadio = resumeRadios.find(r => (r.getAttribute('aria-label') || '').includes(targetName));
                    if (targetRadio) {
                        if (window.getComputedStyle(targetRadio).opacity === '0') targetRadio.style.pointerEvents = 'none';
                        const clickTarget = targetRadio.closest('label') || (targetRadio.parentElement && targetRadio.parentElement.parentElement) || targetRadio;
                        clickTarget.setAttribute('data-sabrina-target-resume', '1');
                        return {
                            in_modal: true,
                            has_resume_step: true,
                            already_present: true,
                            already_checked: Boolean(targetRadio.checked),
                        };
                    }
                    if (txt.includes(targetName)) {
                        const chooseBtn = Array.from(modal.querySelectorAll('button, label, [role="radio"]')).find(el => {
                            const a = (el.getAttribute('aria-label') || el.innerText || '');
                            return a.includes(targetName);
                        });
                        if (chooseBtn) {
                            chooseBtn.setAttribute('data-sabrina-target-resume', '1');
                            chooseBtn.click();
                        }
                        return {in_modal: true, has_resume_step: true, already_present: true, already_checked: false};
                    }
                    return {
                        in_modal: true,
                        has_resume_step: true,
                        already_present: false,
                        has_file_input: Boolean(fileInputs.length),
                        has_upload_button: Boolean(uploadBtn),
                    };
                }""",
                target_name,
            )
            if not state.get("has_resume_step"):
                return None
            if state.get("already_present"):
                if not state.get("already_checked"):
                    t_loc = page.locator('[data-sabrina-target-resume="1"]').first
                    if t_loc.count() > 0:
                        try:
                            t_loc.click(timeout=4000)
                            time.sleep(0.8)
                        except Exception:
                            pass
                return f"resume_selected:{target_name}"

            f_loc = page.locator('input[data-sabrina-resume-input="0"]').first
            if state.get("has_file_input") and f_loc.count() > 0:
                f_loc.set_input_files(pdf_payload, timeout=8000)
                time.sleep(2.2)
                return f"resume_uploaded:{target_name}"

            up_btn = page.locator('[data-sabrina-upload-resume-btn="1"]').first
            if state.get("has_upload_button") and up_btn.count() > 0:
                with page.expect_file_chooser(timeout=7000) as fc_info:
                    up_btn.click(timeout=4000)
                fc_info.value.set_files(pdf_payload)
                time.sleep(2.8)
                # Ensure newly uploaded resume radio is selected
                page.evaluate(
                    r"""(targetName) => {
                        const r = Array.from(document.querySelectorAll('input[type="radio"]')).find(x =>
                            (x.getAttribute('aria-label') || '').includes(targetName)
                        );
                        if (r && !r.checked) {
                            const wrap = r.closest('label') || (r.parentElement && r.parentElement.parentElement) || r;
                            wrap.click();
                        }
                    }""",
                    target_name,
                )
                return f"resume_uploaded:{target_name}"
        except Exception as exc:
            logger.warning("Resume selection/upload warning: %s", exc)
        return None

    def autofill_screening_questions(
        self,
        page: Any,
        attempted_keys: Optional[set[str]] = None,
        skill_attempted_keys: Optional[set[str]] = None,
        inline_error_text: str = "",
        use_model_skill: bool = False,
    ) -> List[str]:
        """Inspect visible Easy Apply controls and fill from `config/operator_profile.json` +
        the `operator-profile-identity` model skill, touching each control at most ONCE per run (never looping).
        """
        answered: List[str] = []
        seen_det = attempted_keys if attempted_keys is not None else set()
        seen_skill = skill_attempted_keys if skill_attempted_keys is not None else set()
        op_profile = load_operator_profile()
        qa_bank = to_screening_qa_bank(op_profile)
        target_phone_digits = re.sub(r"\D", "", str((op_profile.get("contact") or {}).get("phone_digits", "4794039960")))[-10:]
        try:
            fields = page.evaluate(
                r"""(targetPhoneDigits) => {
                    const out = [];
                    // Pierce open Shadow DOMs (LinkedIn mounts .jobs-easy-apply-modal inside #interop-outlet.shadowRoot)
                    // and explicitly exclude floating messaging chat bubbles (.msg-overlay-conversation-bubble[role="dialog"])!
                    const allRoots = [document];
                    const walkShadows = (node) => {
                        for (const el of node.querySelectorAll('*')) {
                            if (el.shadowRoot) {
                                allRoots.push(el.shadowRoot);
                                walkShadows(el.shadowRoot);
                            }
                        }
                    };
                    walkShadows(document);

                    let root = null;
                    for (const r of allRoots) {
                        const m = r.querySelector(
                            '.jobs-easy-apply-modal, [data-test-modal-id="easy-apply-modal"], ' +
                            'dialog[open], [data-sdui-screen*="EasyApply"], ' +
                            '.artdeco-modal:not(.msg-overlay-conversation-bubble), ' +
                            '[role="dialog"]:not(.msg-overlay-conversation-bubble):not([id^="ember"][aria-label*="conversation"])'
                        );
                        if (m && !((m.className || '').toString().includes('msg-overlay')) && !/close your conversation with/i.test((m.innerText || '').slice(0, 140))) {
                            root = m;
                            break;
                        }
                    }
                    if (!root) root = document;

                    const ownerRoot = (el) => (el && el.getRootNode ? el.getRootNode() : document);
                    const findForLabel = (el) => {
                        if (!el || !el.id) return null;
                        const rn = ownerRoot(el);
                        return (rn.querySelector && rn.querySelector(`label[for="${CSS.escape(el.id)}"]`)) || document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                    };
                    const cleanVisibleText = (el) => {
                        if (!el) return '';
                        // Prefer aria-hidden='true' span if LinkedIn duplicated screen-reader text inside visually-hidden
                        const ariaSpan = el.querySelector && el.querySelector('[aria-hidden="true"]');
                        const raw = (ariaSpan && ariaSpan.innerText && ariaSpan.innerText.trim().length > 1)
                            ? ariaSpan.innerText
                            : (el.innerText || el.textContent || '');
                        return raw.replace(/\s+/g, ' ').trim();
                    };
                    const isBoxVisible = (el) => {
                        if (!el) return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const extractLabel = (el) => {
                        if (el.labels && el.labels[0] && cleanVisibleText(el.labels[0])) {
                            return cleanVisibleText(el.labels[0]);
                        }
                        const explicit = findForLabel(el);
                        if (explicit && cleanVisibleText(explicit)) {
                            return cleanVisibleText(explicit);
                        }
                        if (el.getAttribute('aria-label') && el.getAttribute('aria-label').trim()) {
                            let baseLbl = el.getAttribute('aria-label').replace(/\s+/g, ' ').trim();
                            const descId = el.getAttribute('aria-describedby');
                            if (el.tagName === 'TEXTAREA' && descId) {
                                const descEl = document.getElementById(descId);
                                if (descEl && descEl.innerText) {
                                    baseLbl += ' — ' + descEl.innerText.replace(/\s+/g, ' ').trim();
                                }
                            }
                            return baseLbl;
                        }
                        const box = el.closest('.fb-dash-form-element, [data-test-form-element], [data-test-text-entity-list-form-component], [data-test-single-line-text-form-component], fieldset');
                        if (box) {
                            const qEl = box.querySelector('legend, .fb-dash-form-element__label, [data-test-form-element-label], label, span[aria-hidden="true"]');
                            if (qEl && cleanVisibleText(qEl)) {
                                return cleanVisibleText(qEl);
                            }
                            if (box.innerText && box.innerText.trim()) {
                                return box.innerText.replace(/\s+/g, ' ').trim();
                            }
                        }
                        if (el.previousElementSibling && el.previousElementSibling.innerText) {
                            return el.previousElementSibling.innerText.replace(/\s+/g, ' ').trim();
                        }
                        if (el.parentElement && el.parentElement.innerText) {
                            return el.parentElement.innerText.replace(/\s+/g, ' ').trim();
                        }
                        return '';
                    };
                    const hasFieldError = (el) => {
                        if (el && el.getAttribute && el.getAttribute('aria-invalid') === 'true') return true;
                        const box = el.closest('.fb-dash-form-element, [data-test-form-element], [data-test-form-builder-radio-button-form-component], fieldset') || (el.parentElement && el.parentElement.parentElement);
                        if (!box) return false;
                        const alertEls = box.querySelectorAll ? Array.from(box.querySelectorAll('.artdeco-inline-feedback--error, [role="alert"]')).filter(a => !/successfully|resume\s+uploaded|saved/i.test(a.innerText || '')) : [];
                        return Boolean(alertEls.length) || /this field is required|enter a valid|please make a selection/i.test(box.innerText || '');
                    };

                    // 1. Text / Number / Tel inputs
                    const inputs = Array.from(root.querySelectorAll('input[type="text"], input[type="number"], input[type="tel"], input:not([type]), textarea'));
                    inputs.forEach((el, idx) => {
                        if (el.disabled || el.readOnly || !isBoxVisible(el)) return;
                        const lbl = extractLabel(el);
                        if (!lbl || /search/i.test(lbl)) return;
                        const curVal = (el.value || '').trim();
                        const isPhone = el.type === 'tel' || (/^(?:mobile\s+)?phone(?:\s+number)?\*?$|^mobile(?:\s+number)?\*?$|^telephone/i.test(lbl));
                        const fieldErr = hasFieldError(el);
                        if (curVal !== '' && !fieldErr && !(isPhone && targetPhoneDigits && curVal.replace(/\D/g, '').slice(-10) !== targetPhoneDigits)) return;
                        el.setAttribute('data-sabrina-qa-idx', String(idx));
                        out.push({kind: 'input', idx: String(idx), type: el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text'), label: lbl.slice(0, 480), current_value: curVal, has_error: fieldErr, options: []});
                    });

                    // 2. Select dropdowns
                    const selects = Array.from(root.querySelectorAll('select'));
                    selects.forEach((sel, idx) => {
                        if (sel.disabled || !isBoxVisible(sel)) return;
                        const val = (sel.value || '').trim();
                        const selOptText = (sel.selectedIndex >= 0 && sel.options[sel.selectedIndex] ? (sel.options[sel.selectedIndex].text || '') : '').trim();
                        const isPlaceholder = !val || /^(?:select|choose|--|please select)/i.test(val) || /^(?:select|choose|--|please select|$)/i.test(selOptText);
                        if (!isPlaceholder && !hasFieldError(sel)) return;
                        const lbl = extractLabel(sel);
                        const opts = Array.from(sel.options || []).map(o => (o.text || o.value || '').trim()).filter(Boolean);
                        sel.setAttribute('data-sabrina-qa-sel', String(idx));
                        out.push({kind: 'select', idx: String(idx), type: 'select', label: lbl.slice(0, 240), current_value: selOptText, has_error: hasFieldError(sel), options: opts});
                    });

                    // 3. Radio Groups (Shadow-DOM + React 19 SDUI safe)
                    const groups = Array.from(root.querySelectorAll('fieldset, [role="radiogroup"]'));
                    groups.forEach((grp, idx) => {
                        if (!isBoxVisible(grp)) return;
                        const radios = Array.from(grp.querySelectorAll('input[type="radio"]'));
                        if (!radios.length) return;
                        if (radios.some(r => /\.(pdf|docx?)\b/i.test(r.getAttribute('aria-label') || ''))) return;
                        if (/press enter to send|choose a send option/i.test(grp.innerText || '')) return;
                        const legend = grp.querySelector('legend, [data-test-form-builder-radio-button-form-component__title], .fb-dash-form-element__label');
                        const radioAriaLbl = (radios[0].getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
                        let lbl = (radioAriaLbl.length > 4 ? radioAriaLbl : '') || cleanVisibleText(legend) || (grp.innerText || '').replace(/\s+/g, ' ').trim();
                        if (!lbl || /^(?:yes\s*no|no\s*yes)$/i.test(lbl)) {
                            const outer = grp.closest('.fb-dash-form-element, [data-test-form-element], .jobs-easy-apply-form-section__grouping') || (grp.parentElement && grp.parentElement.parentElement) || grp.parentElement;
                            const prev = grp.previousElementSibling;
                            const outerTxt = (prev && prev.innerText ? prev.innerText : (outer ? outer.innerText : '')).replace(/\b(?:Yes|No)\b/gi, '').replace(/\s+/g, ' ').trim();
                            if (outerTxt) lbl = outerTxt;
                        }
                        const opts = radios.map((r, rIdx) => {
                            if (window.getComputedStyle(r).opacity === '0') r.style.pointerEvents = 'none';
                            const explicitFor = findForLabel(r);
                            const gp = r.parentElement && r.parentElement.parentElement;
                            const forLbl = (explicitFor && cleanVisibleText(explicitFor)) ? explicitFor : (r.closest('label') || r.nextElementSibling || gp || r.parentElement);
                            const optAttr = (forLbl && forLbl.getAttribute && forLbl.getAttribute('data-test-text-selectable-option__label')) || r.getAttribute('data-test-text-selectable-option__input') || '';
                            let rl = optAttr || cleanVisibleText(forLbl) || (r.labels && r.labels[0] && cleanVisibleText(r.labels[0])) || '';
                            if (!rl || rl.toLowerCase() === 'on') {
                                rl = (forLbl && (forLbl.innerText || forLbl.textContent) || '').replace(/\s+/g, ' ').trim();
                            }
                            if (!rl || rl.toLowerCase() === 'on') {
                                rl = rIdx === 0 ? 'Yes' : (rIdx === 1 ? 'No' : (r.value || ''));
                            }
                            const tagTarget = (explicitFor && isBoxVisible(explicitFor)) ? explicitFor : (r.closest('label') || (gp && isBoxVisible(gp) ? gp : null) || r.nextElementSibling || r);
                            tagTarget.setAttribute('data-sabrina-qa-radio-opt', `${idx}:${rIdx}`);
                            r.setAttribute('data-sabrina-qa-radio-inp', `${idx}:${rIdx}`);
                            return rl.replace(/\s+/g, ' ').trim();
                        });
                        const checkedIdx = radios.findIndex(r => r.checked);
                        const checkedOpt = checkedIdx >= 0 ? (opts[checkedIdx] || '').toLowerCase() : '';
                        const shouldBeNo = /previously\s+(?:been\s+)?employed|former\s+employee|sponsor|visa\s+status/i.test(lbl) && !/polito/i.test(lbl);
                        if (checkedIdx >= 0 && !(shouldBeNo && checkedOpt === 'yes')) return;
                        grp.setAttribute('data-sabrina-qa-grp', String(idx));
                        out.push({kind: 'radio', idx: String(idx), type: 'radio', label: lbl.slice(0, 280), current_value: checkedOpt, has_error: hasFieldError(grp), options: opts});
                    });

                    // 4. Unchecked Confirmation Checkboxes (Shadow-DOM safe)
                    const checkboxes = Array.from(root.querySelectorAll('input[type="checkbox"]'));
                    checkboxes.forEach((cb, idx) => {
                        if (cb.disabled || cb.checked) return;
                        const forLbl = findForLabel(cb);
                        const box = cb.closest('fieldset, .fb-dash-form-element, [data-test-form-element], .jobs-easy-apply-form-section__grouping') || cb.parentElement;
                        if (!isBoxVisible(forLbl) && !isBoxVisible(box) && !isBoxVisible(cb)) return;
                        const legendTxt = box ? cleanVisibleText(box.querySelector('legend, .fb-dash-form-element__label')) : '';
                        const optTxt = (cleanVisibleText(forLbl) || (cb.labels && cb.labels[0] && cleanVisibleText(cb.labels[0])) || (box && box.innerText) || '').trim();
                        const lbl = (legendTxt + ' ' + optTxt).replace(/\s+/g, ' ').trim();
                        if (!lbl || /follow\s+.+\s+to\s+stay\s+up\s+to\s+date/i.test(lbl)) return;
                        const clickEl = forLbl || cb;
                        clickEl.setAttribute('data-sabrina-qa-cb-click', String(idx));
                        cb.setAttribute('data-sabrina-qa-cb', String(idx));
                        out.push({kind: 'checkbox', idx: String(idx), type: 'checkbox', label: lbl.slice(0, 240), current_value: '', has_error: hasFieldError(cb), options: ['Yes']});
                    });

                    return out;
                }""",
                target_phone_digits,
            )

            unresolved_for_skill: List[Dict[str, Any]] = []
            field_by_key: Dict[str, Dict[str, Any]] = {}

            for f in fields or []:
                kind = f.get("kind")
                idx_str = str(f.get("idx", "0"))
                short_lbl = re.sub(r"\s+", " ", f.get("label", ""))[:45]
                fkey = f"{kind}:{idx_str}:{short_lbl.lower()}"
                f["field_key"] = fkey
                field_by_key[fkey] = f

                # If this field has an inline error OR was not deterministically resolved yet, let model skill handle error repair
                if f.get("has_error") and fkey in seen_det:
                    if fkey not in seen_skill:
                        unresolved_for_skill.append(f)
                    continue

                if fkey in seen_det:
                    continue

                ans = match_screening_answer(f.get("label", ""), f.get("type", "text"), f.get("options") or [], qa_bank=qa_bank)
                if ans is None and kind == "radio":
                    opts_low = [str(o).strip().lower() for o in (f.get("options") or [])]
                    if "yes" in opts_low and "no" in opts_low:
                        lbl_low = (f.get("label") or "").lower()
                        ans = "No" if any(w in lbl_low for w in ("sponsor", "visa", "relative", "previously employed", "former employee", "government official", "conflict")) else "Yes"
                if ans is None:
                    if fkey not in seen_skill:
                        unresolved_for_skill.append(f)
                    continue

                seen_det.add(fkey)
                if self._apply_single_field_value(page, f, str(ans)):
                    answered.append(f"qa_{kind}:{short_lbl}={ans}")

            # Invoke the LLM `operator-profile-identity` skill at most ONCE for any unresolved or inline-error controls
            if unresolved_for_skill and (use_model_skill or any(not x.get("has_error") for x in unresolved_for_skill)):
                for uf in unresolved_for_skill:
                    seen_skill.add(uf["field_key"])
                try:
                    skill_res = resolve_fields_with_profile_skill(
                        [
                            {
                                "field_key": uf["field_key"],
                                "kind": uf["kind"],
                                "type": uf["type"],
                                "label": uf["label"],
                                "current_value": uf.get("current_value", ""),
                                "has_error": bool(uf.get("has_error")),
                                "options": (uf.get("options") or [])[:12],
                            }
                            for uf in unresolved_for_skill
                        ],
                        inline_error_text=inline_error_text,
                        profile=op_profile,
                    )
                    for item in skill_res:
                        fk = item.get("field_key", "")
                        act = item.get("action", "skip")
                        val = str(item.get("value") or "").strip()
                        target_f = field_by_key.get(fk)
                        if not target_f or act == "skip" or (act != "check" and not val):
                            continue
                        seen_det.add(fk)
                        short_lbl = re.sub(r"\s+", " ", target_f.get("label", ""))[:45]
                        if self._apply_single_field_value(page, target_f, "Yes" if act == "check" else val):
                            answered.append(f"skill_{target_f['kind']}:{short_lbl}={val or 'Yes'}")
                except Exception:
                    pass
        except Exception:
            pass
        return answered

    def _apply_single_field_value(self, page: Any, f: Dict[str, Any], ans: str) -> bool:
        """Apply a resolved value to one Easy Apply DOM control cleanly with focus/input/change/blur."""
        kind = f.get("kind")
        idx = f.get("idx")
        if kind == "input":
            loc = page.locator(f'[data-sabrina-qa-idx="{idx}"]').first
            if loc.count() > 0:
                loc.click(timeout=3000)
                loc.fill(str(ans), timeout=3000)
                loc.dispatch_event("input")
                loc.dispatch_event("change")
                try:
                    loc.evaluate("e => e.blur()")
                except Exception:
                    pass
                return True
        elif kind == "select":
            loc = page.locator(f'select[data-sabrina-qa-sel="{idx}"]').first
            if loc.count() > 0:
                target_ans = str(ans).strip()
                opts_list = [o for o in (f.get("options") or []) if not re.match(r"^(?:select|choose|--|please select)", o.strip(), re.I)]
                matched_opt = next((o for o in opts_list if o.strip().lower() == target_ans.lower()), None)
                if matched_opt is None:
                    matched_opt = next((o for o in opts_list if o.strip().lower().startswith(target_ans.lower())), None)
                selected_ok = False
                try:
                    loc.select_option(label=matched_opt or target_ans, timeout=3000)
                    selected_ok = True
                except Exception:
                    try:
                        loc.select_option(value=target_ans, timeout=3000)
                        selected_ok = True
                    except Exception:
                        selected_ok = bool(
                            loc.evaluate(
                                """(sel, tAns) => {
                                    if (!sel) return false;
                                    const low = tAns.toLowerCase();
                                    for (let i = 0; i < sel.options.length; i++) {
                                        const txt = (sel.options[i].text || '').trim().toLowerCase();
                                        const val = (sel.options[i].value || '').trim().toLowerCase();
                                        if (txt === low || val === low || txt.startsWith(low)) {
                                            sel.selectedIndex = i;
                                            sel.dispatchEvent(new Event('input', {bubbles: true}));
                                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                                            return true;
                                        }
                                    }
                                    return false;
                                }""",
                                target_ans,
                            )
                        )
                if selected_ok:
                    loc.dispatch_event("change")
                    return True
        elif kind == "radio":
            low = str(ans).strip().lower()
            opts = f.get("options") or []
            matched_r_idx = None
            for r_i, opt_txt in enumerate(opts):
                ot = (opt_txt or "").strip().lower()
                if ot == low or ot == low + low or ot.startswith(low + " ") or ot.startswith(low + ","):
                    matched_r_idx = r_i
                    break
            if matched_r_idx is not None:
                # Playwright's CSS locator natively pierces #interop-outlet.shadowRoot and fires a trusted click!
                opt_loc = page.locator(f'[data-sabrina-qa-radio-opt="{idx}:{matched_r_idx}"]').first
                inp_loc = page.locator(f'input[data-sabrina-qa-radio-inp="{idx}:{matched_r_idx}"]').first
                if opt_loc.count() > 0:
                    try:
                        opt_loc.scroll_into_view_if_needed(timeout=2000)
                        opt_loc.click(timeout=2000)
                    except Exception:
                        try:
                            opt_loc.evaluate("el => el.click()")
                        except Exception:
                            pass
                if inp_loc.count() > 0:
                    inp_loc.evaluate(
                        """(r) => {
                            const rn = r.getRootNode ? r.getRootNode() : document;
                            const lbl = r.id && rn.querySelector ? rn.querySelector(`label[for="${CSS.escape(r.id)}"]`) : null;
                            if (!r.checked && lbl) lbl.click();
                            if (!r.checked) {
                                r.click();
                                r.checked = true;
                            }
                            r.dispatchEvent(new Event('input', {bubbles: true}));
                            r.dispatchEvent(new Event('change', {bubbles: true}));
                        }"""
                    )
                    return bool(inp_loc.evaluate("r => Boolean(r.checked)"))
                return opt_loc.count() > 0
        elif kind == "checkbox" and str(ans).strip().lower() in ("yes", "true", "1"):
            click_loc = page.locator(f'[data-sabrina-qa-cb-click="{idx}"]').first
            cb_loc = page.locator(f'input[data-sabrina-qa-cb="{idx}"]').first
            if click_loc.count() > 0:
                try:
                    click_loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                click_loc.click(timeout=3000, force=True)
            if cb_loc.count() > 0:
                return bool(
                    cb_loc.evaluate(
                        """(cb) => {
                            if (!cb.checked) {
                                cb.click();
                                cb.checked = true;
                                cb.dispatchEvent(new Event('input', {bubbles: true}));
                                cb.dispatchEvent(new Event('change', {bubbles: true}));
                            }
                            return Boolean(cb.checked);
                        }"""
                    )
                )
        return False


