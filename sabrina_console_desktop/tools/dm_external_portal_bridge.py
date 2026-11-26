# -*- coding: utf-8 -*-
"""
tools/dm_external_portal_bridge.py
==================================
Governed Bridge from Incoming Recruiter LinkedIn DMs -> External ATS Portals.

Responsibilities:
1. Extract genuine external ATS / company career portal job URLs from incoming DM messages
   (Oracle Recruiting Cloud, Workday, Greenhouse, Lever, Ashby, iCIMS, SmartRecruiters,
   Eightfold, Rippling, Workable, `careers.*`, `/job/`, `/jobs/`, `/jobsearch/`) while
   strictly excluding calendar links (`calendly.com`), webinars (`zoom.us`), LinkedIn
   profile/group links, marketing PDFs, and SSRF/private hosts.
2. Persist durable state in `data/social/social.sqlite3` (`dm_external_applications`) so
   Sabrina never double-submits, automatically retries portals that were temporarily down
   (`portal_unreachable`) on future ticks, and tracks every step for operator audit.
3. Open a temporary Chrome CDP tab, inspect location/onsite requirements against
   `config/operator_profile.json`, run `ExternalAtsWorker` when reachable and compatible,
   and return a verified `portal_outcome` packet so Sabrina's DM Swarm replies with 100%
   honesty (never claiming submission unless verified `"submitted"`).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse, urlsplit, urlunsplit

from tools.external_ats_worker import ExternalAtsWorker
from tools.operator_profile import load_operator_profile

REPO_ROOT = Path(__file__).resolve().parent.parent

_URL_RE = re.compile(r"https?://[^\s<>\"\'\)\]]+", re.I)

_KNOWN_ATS_HOST_RE = re.compile(
    r"(?:"
    r"myworkdayjobs\.com$|workday\.com$|oraclecloud\.com$|careers\.oracle\.com$|"
    r"greenhouse\.io$|lever\.co$|ashbyhq\.com$|icims\.com$|smartrecruiters\.com$|"
    r"eightfold\.ai$|workable\.com$|rippling\.com$|bamboohr\.com$|jobvite\.com$|"
    r"ultipro\.com$|ukg\.net$|dayforcehcm\.com$|brassring\.com$|taleo\.net$|"
    r"^careers\.|^jobs\."
    r")",
    re.I,
)

_JOB_PATH_RE = re.compile(
    r"/(?:jobs?|jobsearch|careers?|positions?|openings?|requisitions?|posting|apply)(?:/|$)",
    re.I,
)

_EXCLUDED_HOST_RE = re.compile(
    r"(?:"
    r"linkedin\.com$|lnkd\.in$|calendly\.com$|cal\.com$|chili-?piper\.com$|"
    r"zoom\.us$|teams\.microsoft\.com$|meet\.google\.com$|webex\.com$|"
    r"github\.com$|github\.io$|huggingface\.co$|youtube\.com$|youtu\.be$|"
    r"hackerhalted\.com$|infosec\.events$|bsides|defcon\.org$"
    r")",
    re.I,
)

_EXCLUDED_EXT_RE = re.compile(r"\.(?:pdf|png|jpe?g|gif|svg|mp4|zip|pptx?|docx?)(?:\?|$)", re.I)

_OUT_OF_AREA_ONSITE_RE = re.compile(
    r"\b(?:100%\s+on-?site|office-?based|in-?office\s+\d+\s+days|relocation\s+to|on-?site\s+in)\b",
    re.I,
)


def _clean_url(raw: str) -> str:
    cleaned = raw.strip().rstrip(".,;:!?)'\"]>")
    parsed = urlsplit(cleaned)
    # Unwrap LinkedIn redirect links if present
    if parsed.hostname and parsed.hostname.lower().endswith("linkedin.com") and "/redir/redirect" in parsed.path:
        qs = parse_qs(parsed.query)
        if qs.get("url"):
            cleaned = unquote(qs["url"][0]).strip()
    return cleaned


def is_safe_external_job_url(url: str) -> bool:
    """Verify HTTPS, non-private host, and genuine external ATS / career job URL signature."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().strip(".")
    if host in {"localhost"} or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        pass
    if _EXCLUDED_HOST_RE.search(host):
        return False
    if _EXCLUDED_EXT_RE.search(parsed.path or ""):
        return False
    if _KNOWN_ATS_HOST_RE.search(host):
        return True
    if _JOB_PATH_RE.search(parsed.path or ""):
        return True
    return False


def canonical_url_key(url: str) -> str:
    """Normalize tracking query params (`utm_*`, `source`) so identical job reqs deduplicate cleanly."""
    p = urlsplit(url)
    kept_pairs = []
    for part in (p.query or "").split("&"):
        if not part:
            continue
        k = part.split("=", 1)[0].lower()
        if k.startswith("utm") or k in {"source", "ref", "gh_jid", "trk"}:
            if k == "gh_jid":
                kept_pairs.append(part)
            continue
        kept_pairs.append(part)
    norm_path = re.sub(r"/+$", "", p.path or "") or "/"
    return urlunsplit((p.scheme.lower(), (p.hostname or "").lower(), norm_path, "&".join(kept_pairs), ""))


def extract_req_id_hint(url: str) -> str:
    m = re.search(r"/(?:job|jobs|requisition|positions)/([A-Za-z0-9_-]{3,24})", url, re.I)
    if m:
        return m.group(1)
    qs = parse_qs(urlsplit(url).query)
    for k in ("gh_jid", "jobId", "reqId", "id"):
        if qs.get(k):
            return str(qs[k][0])[:24]
    return ""


def extract_external_job_urls(thread: Dict[str, Any]) -> List[str]:
    """Return deduplicated external ATS / career job URLs shared by external contacts in `thread`."""
    found: List[str] = []
    seen_keys: set[str] = set()
    for msg in thread.get("messages") or []:
        if msg.get("ownership") != "external":
            continue
        candidates: List[str] = list(_URL_RE.findall(msg.get("text") or ""))
        for href in msg.get("links") or []:
            if isinstance(href, str):
                candidates.append(href)
        for raw in candidates:
            u = _clean_url(raw)
            if not is_safe_external_job_url(u):
                continue
            ukey = canonical_url_key(u)
            if ukey not in seen_keys:
                seen_keys.add(ukey)
                found.append(u)
    return found


def alternate_portal_urls(url: str) -> List[str]:
    """Return ordered list of [primary_url, optional_direct_ats_mirror_url] for known enterprise portals."""
    urls = [url]
    m_ora = re.match(
        r"^https://careers\.oracle\.com/en/sites/jobsearch/job/(\d+)",
        url,
        re.I,
    )
    if m_ora:
        req_id = m_ora.group(1)
        urls.append(
            f"https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/jobsearch/job/{req_id}"
        )
    return urls


class DmExternalPortalStore:
    """SQLite persistence for DM-sourced external portal applications (`social.sqlite3`)."""

    def __init__(self, db_path: Optional[Path] = None):
        default_dir = Path(os.environ.get("SABRINA_SOCIAL_DATA_DIR", str(REPO_ROOT / "data/social")))
        self.db_path = Path(db_path or (default_dir / "social.sqlite3"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dm_external_applications (
                    url_key TEXT PRIMARY KEY,
                    portal_url TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    peer_name TEXT,
                    req_id TEXT,
                    job_title TEXT,
                    company TEXT,
                    location TEXT,
                    status TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    last_attempt_ts REAL DEFAULT 0,
                    details_json TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                )
                """
            )

    def get(self, url_key: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM dm_external_applications WHERE url_key = ?", (url_key,)
            ).fetchone()
            return dict(row) if row else None

    def upsert(
        self,
        *,
        url_key: str,
        portal_url: str,
        thread_id: str,
        peer_name: str,
        req_id: str,
        status: str,
        job_title: str = "",
        company: str = "",
        location: str = "",
        increment_attempt: bool = True,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        existing = self.get(url_key)
        attempts = (existing["attempts"] if existing else 0) + (1 if increment_attempt else 0)
        created = existing["created"] if existing else now
        details_str = json.dumps(details or {}, ensure_ascii=False)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO dm_external_applications (
                    url_key, portal_url, thread_id, peer_name, req_id, job_title, company,
                    location, status, attempts, last_attempt_ts, details_json, created, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url_key) DO UPDATE SET
                    portal_url = excluded.portal_url,
                    thread_id = excluded.thread_id,
                    peer_name = excluded.peer_name,
                    req_id = COALESCE(NULLIF(excluded.req_id, ''), dm_external_applications.req_id),
                    job_title = COALESCE(NULLIF(excluded.job_title, ''), dm_external_applications.job_title),
                    company = COALESCE(NULLIF(excluded.company, ''), dm_external_applications.company),
                    location = COALESCE(NULLIF(excluded.location, ''), dm_external_applications.location),
                    status = excluded.status,
                    attempts = ?,
                    last_attempt_ts = ?,
                    details_json = excluded.details_json,
                    updated = excluded.updated
                """,
                (
                    url_key,
                    portal_url,
                    thread_id,
                    peer_name,
                    req_id,
                    job_title,
                    company,
                    location,
                    status,
                    attempts,
                    now if increment_attempt else (existing["last_attempt_ts"] if existing else 0.0),
                    details_str,
                    created,
                    now,
                    attempts,
                    now if increment_attempt else (existing["last_attempt_ts"] if existing else 0.0),
                ),
            )
        return self.get(url_key) or {}

    def list_retryable(self, cooldown_seconds: float = 900.0, max_attempts: int = 8) -> List[Dict[str, Any]]:
        cutoff = time.time() - cooldown_seconds
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM dm_external_applications
                WHERE status = 'portal_unreachable'
                  AND attempts < ?
                  AND last_attempt_ts <= ?
                ORDER BY updated ASC LIMIT 3
                """,
                (max_attempts, cutoff),
            ).fetchall()
            return [dict(r) for r in rows]


def check_location_compatibility(page_title: str, body_text: str, profile: Optional[Dict[str, Any]] = None) -> tuple[bool, str]:
    """Check if an external listing mandates out-of-area onsite work incompatible with Georgia/Remote."""
    op = profile or load_operator_profile()
    loc = op.get("location") or {}
    home_state = (loc.get("state") or "Georgia").lower()
    home_abbr = (loc.get("state_abbr") or "GA").lower()
    home_metros = ("georgia", "atlanta", "alpharetta", "cumming", "roswell", "remote", "anywhere", "hybrid - ga")
    combined = f"{page_title}\n{body_text[:2500]}".lower()
    if any(m in combined for m in home_metros) or f", {home_abbr}" in combined or home_state in combined:
        return True, "compatible"
    if _OUT_OF_AREA_ONSITE_RE.search(combined) and "remote" not in combined[:600]:
        return False, "Listing appears to require onsite presence outside Georgia/Atlanta with no remote option stated"
    return True, "compatible"


def attempt_external_portal_application(
    portal_url: str,
    *,
    thread_id: str,
    peer_name: str = "",
    cdp_url: str = "http://127.0.0.1:9222",
    store: Optional[DmExternalPortalStore] = None,
    timeout_ms: int = 16000,
) -> Dict[str, Any]:
    """Open `portal_url` in a temporary CDP tab, inspect & run ExternalAtsWorker if reachable, and persist status."""
    from playwright.sync_api import sync_playwright

    portal_store = store or DmExternalPortalStore()
    ukey = canonical_url_key(portal_url)
    req_id = extract_req_id_hint(portal_url)
    existing = portal_store.get(ukey)

    if existing is not None:
        return {
            "portal_url": portal_url,
            "url_key": ukey,
            "req_id": existing.get("req_id") or req_id,
            "status": existing["status"],
            "job_title": existing.get("job_title") or "",
            "company": existing.get("company") or "",
            "reason": "Already attempted once; single-attempt rule prevents re-opening portal in a loop",
            "cached": True,
        }

    candidates = alternate_portal_urls(portal_url)
    host = (urlsplit(portal_url).hostname or "").lower()
    inferred_company = "Oracle" if "oracle" in host else host.split(".")[0].capitalize()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(cdp_url, timeout=8000)
        if not browser.contexts:
            raise RuntimeError("No active browser context on CDP")
        context = browser.contexts[0]
        temp_page = context.new_page()
        try:
            loaded = False
            last_nav_err = ""
            for target_url in candidates:
                try:
                    resp = temp_page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                    if resp is not None and resp.status >= 500:
                        last_nav_err = f"HTTP {resp.status} from {target_url}"
                        continue
                    temp_page.wait_for_timeout(2500)
                    loaded = True
                    break
                except Exception as nav_exc:
                    last_nav_err = str(nav_exc).splitlines()[0][:200]

            if not loaded:
                saved_row = portal_store.upsert(
                    url_key=ukey,
                    portal_url=portal_url,
                    thread_id=thread_id,
                    peer_name=peer_name,
                    req_id=req_id,
                    company=inferred_company,
                    status="portal_unreachable",
                    details={"error": last_nav_err, "tried_urls": candidates},
                )
                return {
                    "portal_url": portal_url,
                    "url_key": ukey,
                    "req_id": req_id,
                    "company": inferred_company,
                    "status": "portal_unreachable",
                    "reason": last_nav_err,
                    "attempts": saved_row.get("attempts", 1),
                }

            page_title = (temp_page.title() or "").strip()[:160]
            body_text = ""
            try:
                body_text = (temp_page.locator("body").inner_text(timeout=4000) or "")[:4000]
            except Exception:
                body_text = ""

            ok_loc, loc_reason = check_location_compatibility(page_title, body_text)
            if not ok_loc:
                portal_store.upsert(
                    url_key=ukey,
                    portal_url=portal_url,
                    thread_id=thread_id,
                    peer_name=peer_name,
                    req_id=req_id,
                    job_title=page_title,
                    company=inferred_company,
                    status="needs_location_confirmation",
                    details={"reason": loc_reason},
                )
                return {
                    "portal_url": portal_url,
                    "url_key": ukey,
                    "req_id": req_id,
                    "job_title": page_title,
                    "company": inferred_company,
                    "status": "needs_location_confirmation",
                    "reason": loc_reason,
                }

            ats_worker = ExternalAtsWorker(runtime=None, data_dir=portal_store.db_path.parent)
            ext_res = ats_worker.apply_on_external_page(
                temp_page,
                {
                    "job_id": req_id or ukey[-16:],
                    "title": page_title,
                    "company": inferred_company,
                    "job_url": portal_url,
                },
                max_steps=7,
            )
            ext_status = str(ext_res.get("status") or "external_in_progress")
            portal_store.upsert(
                url_key=ukey,
                portal_url=portal_url,
                thread_id=thread_id,
                peer_name=peer_name,
                req_id=req_id,
                job_title=page_title,
                company=inferred_company,
                status=ext_status,
                details=ext_res,
            )
            return {
                "portal_url": portal_url,
                "url_key": ukey,
                "req_id": req_id,
                "job_title": page_title,
                "company": inferred_company,
                "status": ext_status,
                "steps_trace": ext_res.get("steps_trace", []),
                "reason": ext_res.get("reason", ""),
            }
        finally:
            try:
                if not temp_page.is_closed():
                    temp_page.close()
            except Exception:
                pass


def process_thread_external_portals(
    thread: Dict[str, Any],
    *,
    cdp_url: str = "http://127.0.0.1:9222",
    store: Optional[DmExternalPortalStore] = None,
) -> Optional[Dict[str, Any]]:
    """Inspect all external ATS URLs in `thread` (at most 1 attempt per unique URL ever) before DM Swarm drafting."""
    urls = extract_external_job_urls(thread)
    if not urls:
        return None
    portal_store = store or DmExternalPortalStore()
    outcomes: List[Dict[str, Any]] = []
    for u in urls[:3]:
        outcomes.append(
            attempt_external_portal_application(
                u,
                thread_id=str(thread.get("thread_id") or ""),
                peer_name=str(thread.get("peer_name") or ""),
                cdp_url=cdp_url,
                store=portal_store,
                timeout_ms=10000,
            )
        )
    # Prefer returning any submitted or active portal outcome; otherwise the latest outcome
    chosen = next((o for o in outcomes if o.get("status") == "submitted"), outcomes[-1])
    if len(outcomes) > 1:
        chosen = {**chosen, "all_portal_outcomes": outcomes}
    return chosen
