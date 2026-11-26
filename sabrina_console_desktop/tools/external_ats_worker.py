# -*- coding: utf-8 -*-
"""
tools/external_ats_worker.py
============================
Hybrid LLM + Deterministic External ATS Job Application Worker for Sabrina.

Capabilities:
1. Canonical Candidate Profile & Resume File management (`data/social/candidate_profile.json`
   and `data/social/Charles_McGowen_Resume.html`).
2. Deterministic ATS Credential Vault (`ats_accounts` table in `social.sqlite3`) so any
   external portal account created with `charlesmcgowen@gmail.com` (Workday, iCIMS, Lever,
   Greenhouse, Ashby) has its generated password durably stored before submission.
3. Gmail Verification Code & Confirmation Link Reader using Sabrina's authenticated Gmail
   session on CDP (`mail.google.com/mail/u/0/`).
4. Hybrid DOM Control Extractor + LLM Field Mapper (`qwen2.5-coder:14b` / `qwen3:14b`)
   that adapts to arbitrary external ATS form layouts while failing closed on CAPTCHAs.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import string
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from tools.linkedin_jobs_worker import APPROVED_SCREENING_QA_BANK, match_screening_answer
from tools.operator_profile import load_operator_profile, to_ats_candidate_profile

logger = logging.getLogger("sabrina.external_ats")

DEFAULT_CANDIDATE_PROFILE: Dict[str, Any] = to_ats_candidate_profile()


def ensure_candidate_profile_and_resume(data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load canonical operator profile from `config/operator_profile.json` and sync `candidate_profile.json`."""
    root = Path(data_dir or os.environ.get("SABRINA_SOCIAL_DATA_DIR", "data/social"))
    root.mkdir(parents=True, exist_ok=True)
    profile_path = root / "candidate_profile.json"
    profile = to_ats_candidate_profile()
    profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")

    pdf_candidates = [
        root / "Charles_McGowen_Resume_2026.pdf",
        Path("C:/Users/bcmcg/Downloads/Charles_McGowen_Resume_2026.pdf"),
        Path("/home/ego/bot/Sabrina-windows-sync/data/social/Charles_McGowen_Resume_2026.pdf"),
        root / "charles_resume.pdf",
        Path("C:/Users/bcmcg/Downloads/charles_resume.pdf"),
        Path("/home/ego/bot/Sabrina-windows-sync/data/social/charles_resume.pdf"),
    ]
    preferred_2026 = next((p for p in pdf_candidates[:3] if p.exists()), None)
    default_resume = preferred_2026 or next((p for p in pdf_candidates if p.exists()), root / "Charles_McGowen_Resume.html")
    resume_path = preferred_2026 or Path(profile.get("resume_path") or default_resume)
    if not resume_path.exists():
        resume_path = default_resume
    if not resume_path.exists():
        skills_str = ", ".join(profile.get("skills") or DEFAULT_CANDIDATE_PROFILE["skills"])
        html_resume = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{profile['full_name']} - Resume</title></head>
<body>
  <h1>{profile['full_name']}</h1>
  <p>{profile['location']} | {profile['email']} | {profile['phone']} | {profile['linkedin_url']} | {profile['github_url']}</p>
  <h2>Summary</h2>
  <p>{profile['summary']}</p>
  <h2>Core Technical Skills</h2>
  <p>{skills_str}</p>
  <h2>Professional Experience</h2>
  <h3>{profile['current_title']} — {profile['current_company']} (2020 – Present)</h3>
  <ul>
    <li>Architected and deployed fail-closed multi-agent AI orchestration and browser/GPU resource coordination services in Python, FastAPI, and SQLite.</li>
    <li>Engineered automated application security verification harnesses, SSRF/checkpoint guards, and CVE/threat-intelligence triage pipelines.</li>
    <li>Implemented high-throughput CUDA 13 / nvCOMP / OpenZL GPU compression and multi-family local LLM inference scheduling.</li>
  </ul>
</body>
</html>
"""
        resume_path.write_text(html_resume, encoding="utf-8")
    profile["resume_path"] = str(resume_path.resolve())
    return profile


class AtsAccountVault:
    """Durable SQLite vault storing credentials for external ATS portals before signup."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
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
                CREATE TABLE IF NOT EXISTS ats_accounts (
                    domain TEXT PRIMARY KEY,
                    portal_url TEXT,
                    email TEXT NOT NULL,
                    password TEXT NOT NULL,
                    verification_status TEXT DEFAULT 'created',
                    last_job_id TEXT,
                    created REAL NOT NULL,
                    updated REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _generate_strong_password() -> str:
        """Generate a 16-char password satisfying Workday/iCIMS complexity rules."""
        alphabet = string.ascii_letters + string.digits
        token = "".join(secrets.choice(alphabet) for _ in range(10))
        return f"Sabrina!9{token}#"

    def get_or_create_credentials(
        self, url: str, email: str = "charlesmcgowen@gmail.com", job_id: str = ""
    ) -> Dict[str, Any]:
        parsed = urlsplit(url)
        domain = (parsed.hostname or "unknown-ats.local").lower()
        now = time.time()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM ats_accounts WHERE domain = ?", (domain,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE ats_accounts SET updated = ?, last_job_id = COALESCE(?, last_job_id) WHERE domain = ?",
                    (now, job_id or None, domain),
                )
                return dict(row)
            password = self._generate_strong_password()
            conn.execute(
                """
                INSERT INTO ats_accounts (domain, portal_url, email, password, verification_status, last_job_id, created, updated)
                VALUES (?, ?, ?, ?, 'created', ?, ?, ?)
                """,
                (domain, url, email, password, job_id, now, now),
            )
            return {
                "domain": domain,
                "portal_url": url,
                "email": email,
                "password": password,
                "verification_status": "created",
                "last_job_id": job_id,
            }

    def mark_verified(self, url: str) -> None:
        domain = (urlsplit(url).hostname or "").lower()
        if not domain:
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE ats_accounts SET verification_status = 'verified', updated = ? WHERE domain = ?",
                (time.time(), domain),
            )


ATS_DOM_EXTRACTOR = r"""() => {
    const isVisible = el => {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    const labelFor = el => {
        let lbl = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '';
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            for (const refId of labelledBy.split(/\s+/)) {
                if (!refId) continue;
                const refEl = document.getElementById(refId);
                if (refEl && refEl.innerText) lbl = refEl.innerText + ' ' + lbl;
            }
        }
        if (el.id) {
            const explicit = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (explicit && explicit.innerText) lbl = explicit.innerText + ' ' + lbl;
            lbl = lbl + ' ' + el.id.replace(/[_-]+/g, ' ');
        }
        const wrapped = el.closest('label, [ data-automation-id*="formField" ], .field, .form-group, [class*="field" i]');
        if (wrapped && wrapped.innerText) {
            lbl = wrapped.innerText.split('\n')[0] + ' ' + lbl;
        }
        return lbl.replace(/\s+/g, ' ').trim().slice(0, 180);
    };

    const comboValueFor = el => {
        const box = el.closest('.select__control, .select-shell, [class*="select" i], .field, .form-group');
        if (!box) return '';
        const sv = box.querySelector('.select__single-value, [class*="singleValue"], [class*="single-value"]');
        if (sv && sv.innerText) {
            const t = sv.innerText.trim();
            if (t && !/^select\.{0,3}$/i.test(t)) return t.slice(0, 120);
        }
        return '';
    };

    const controls = [];
    const nodes = document.querySelectorAll('input, textarea, select, button, a[role="button"], [role="button"], [role="combobox"], [role="checkbox"], [role="radio"]');
    nodes.forEach((el) => {
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();
        const elId = (el.id || '');
        if (elId.startsWith('iti-') || elId.includes('g-recaptcha-response')) return;
        if (el.getAttribute('data-testid') === 'clear-selection') return;
        const rawAria = (el.getAttribute('aria-label') || '').trim();
        if (/^(clear selections?|toggle flyout|locate me)$/i.test(rawAria)) return;
        if (type === 'hidden') return;
        const role = (el.getAttribute('role') || '').toLowerCase();
        let targetEl = el;
        if (type !== 'file' && !isVisible(el)) {
            if (type === 'radio' || type === 'checkbox') {
                const visLbl = el.closest('label') || (elId ? document.querySelector(`label[for="${CSS.escape(elId)}"]`) : null);
                if (visLbl && isVisible(visLbl)) {
                    targetEl = visLbl;
                } else {
                    return;
                }
            } else if (role === 'combobox') {
                const ctrlBox = el.closest('.select__control, [class*="control" i]');
                if (!ctrlBox || !isVisible(ctrlBox)) return;
            } else {
                return;
            }
        }
        if (el.disabled || el.readOnly) return;
        const idx = controls.length;
        targetEl.setAttribute('data-sabrina-ats-idx', String(idx));
        const options = tag === 'select'
            ? Array.from(el.options).map(o => (o.text || '').trim()).filter(Boolean).slice(0, 15)
            : [];
        let val = type === 'password' ? '' : (el.value || '').slice(0, 120);
        if (type === 'radio' || type === 'checkbox') {
            val = el.checked ? 'checked' : '';
        } else if (!val && role === 'combobox') {
            val = comboValueFor(el);
        }
        let lbl = labelFor(el);
        if (type === 'radio') {
            const grp = el.closest('fieldset, [role="radiogroup"], [class*="question" i], [class*="fieldEntry" i]');
            const qHead = grp ? (grp.innerText || '').split('\n')[0].trim() : '';
            const optTxt = (targetEl.innerText || el.value || '').replace(/\s+/g, ' ').trim();
            if (qHead) lbl = `${qHead} | Option: ${optTxt}`.slice(0, 200);
        }
        controls.push({
            index: idx,
            tag: tag,
            type: type || (tag === 'button' ? 'button' : ''),
            role: role,
            id: elId.slice(0, 80),
            maxlength: el.getAttribute('maxlength') || '',
            label: lbl,
            text: (el.innerText || el.value || '').replace(/\s+/g, ' ').trim().slice(0, 120),
            required: Boolean(el.required || el.getAttribute('aria-required') === 'true' || /\*/.test(lbl)),
            value: val,
            checked: Boolean(el.checked || el.getAttribute('aria-checked') === 'true'),
            options: options
        });
    });

    // Ignore passive reCAPTCHA v3 / Enterprise corner badges (.grecaptcha-badge / anchor iframes);
    // only flag genuine interactive challenge modals (bframe, hcaptcha challenge, turnstile challenge).
    const hasCaptcha = Array.from(document.querySelectorAll('iframe')).some(f => {
        if (!isVisible(f)) return false;
        if (f.closest('.grecaptcha-badge')) return false;
        const src = (f.src || f.title || '').toLowerCase();
        const rect = f.getBoundingClientRect();
        if (src.includes('/anchor?') && rect.height < 120) return false;
        return (
            (/bframe|hcaptcha.*challenge|arkose|funcaptcha/.test(src) && rect.height >= 200 && rect.width >= 200) ||
            (/turnstile/.test(src) && rect.height >= 55 && rect.width >= 240)
        );
    });

    const bodyText = (document.body && document.body.innerText) ? document.body.innerText.slice(0, 4000) : '';
    return {
        url: location.href,
        title: document.title || '',
        has_captcha: hasCaptcha,
        body_excerpt: bodyText,
        controls: controls.slice(0, 80)
    };
}"""


class ExternalAtsWorker:
    """Executes multi-step applications on external ATS portals using hybrid deterministic + LLM mapping."""

    def __init__(self, runtime: Any, data_dir: Optional[Path] = None):
        self.runtime = runtime
        root = Path(data_dir or getattr(runtime, "data_dir", None) or os.environ.get("SABRINA_SOCIAL_DATA_DIR", "data/social"))
        self.data_dir = root
        self.profile = ensure_candidate_profile_and_resume(root)
        self.vault = AtsAccountVault(root / "social.sqlite3")

    def check_gmail_verification(self, context: Any, query_hint: str) -> Dict[str, Optional[str]]:
        """Check Chrome's Google session in a temporary Gmail tab for a 6-digit or 8-char ATS verification code or link."""
        temp_page = context.new_page()
        try:
            time.sleep(4.0)
            q = f'({query_hint} OR greenhouse OR verification OR "security code" OR passcode) newer_than:1d'
            temp_page.goto(
                "https://mail.google.com/mail/u/0/#search/" + quote(q, safe=""),
                wait_until="domcontentloaded",
                timeout=25000,
            )
            time.sleep(3.5)
            if "accounts.google.com" in (temp_page.url or "").lower():
                return {"code": None, "link": None, "reason": "gmail_not_signed_in"}
            rows = temp_page.locator("tr.zA")
            if rows.count() == 0:
                return {"code": None, "link": None, "reason": "no_matching_email"}
            rows.first.click(timeout=5000)
            time.sleep(2.5)
            body_data = temp_page.evaluate(
                """() => {
                    const body = document.querySelector('.a3s.aiL, .a3s') || document.body;
                    const text = body ? body.innerText : '';
                    const links = Array.from((body || document).querySelectorAll('a[href]'))
                        .map(a => a.href)
                        .filter(h => /verify|confirm|activate|token|workday|greenhouse|lever|icims|oracle|oraclecloud|ashby|smartrecruiters|eightfold/i.test(h));
                    return {text: text.slice(0, 4000), links: links.slice(0, 5)};
                }"""
            )
            text = body_data.get("text") or ""
            # Match Greenhouse 8-char alphanumeric security code OR standard 6-digit numeric OTP
            gh_match = re.search(
                r"(?:security code|verification code|passcode|paste this code)[^A-Za-z0-9]{1,60}\b([A-Za-z0-9]{8})\b",
                text,
                re.I,
            )
            six_match = re.search(r"\b(\d{6})\b", text)
            code = gh_match.group(1) if gh_match else (six_match.group(1) if six_match else None)
            links = body_data.get("links") or []
            return {
                "code": code,
                "link": links[0] if links else None,
                "reason": "found" if (code or links) else "no_code_in_email",
            }
        except Exception as exc:
            return {"code": None, "link": None, "reason": str(exc)}
        finally:
            try:
                if not temp_page.is_closed():
                    temp_page.close()
            except Exception:
                pass

    def _deterministic_field_value(self, ctrl: Dict[str, Any], creds: Dict[str, Any]) -> Optional[str]:
        """Fast deterministic mapping for standard identity, contact, EEO decline, consent, and Q&A fields."""
        label = re.sub(r"[_-]+", " ", (ctrl.get("label") or "").lower()).strip()
        label = re.sub(r"\s+", " ", label)
        ctype = (ctrl.get("type") or "").lower()
        if "display language" in label:
            return None
        if ctype == "password" or "password" in label:
            return creds["password"]
        if ctype == "email" or "email" in label:
            return self.profile["email"]
        if ctype == "tel" or (("phone" in label or "mobile" in label) and "country code" not in label):
            return self.profile["phone"]
        if "preferred" in label and "name" in label:
            return self.profile["first_name"]
        if "first name" in label or "given name" in label:
            return self.profile["first_name"]
        if "last name" in label or "family name" in label or "surname" in label:
            return self.profile["last_name"]
        if "full name" in label or "systemfield name" in label or re.match(r"^name\s*\*?$", label.strip()):
            return self.profile["full_name"]
        if "linkedin" in label:
            return self.profile["linkedin_url"]
        if "github" in label or "portfolio" in label or "website" in label:
            return self.profile["github_url"]
        if any(k in label for k in ("city", "candidate location", "where do you currently reside", "current location")):
            return f"{self.profile['city']}, {self.profile['state']}"
        if "postal" in label or "zip" in label:
            return self.profile["postal_code"]
        if re.search(r"\bstate\b|\bprovince\b", label):
            return self.profile["state"]
        if "country" in label and "code" not in label:
            return self.profile["country"]
        if "current company" in label or "current employer" in label:
            return "Independent Security & AI Consultant" if ctrl.get("required") else (self.profile.get("current_company") or "N/A")
        if "current title" in label or "job title" in label:
            return "Senior Security Engineer"
        if any(k in label for k in ("compensation", "salary", "pay expectation", "desired pay")):
            return str(self.profile.get("desired_salary") or "165000")
        if any(k in label for k in ("gdpr", "privacy notice", "data privacy", "data protection", "consent")):
            return "consent"
        eeo = self.profile.get("eeo_demographics") or {}
        if "hispanic" in label or "latino" in label:
            return str(eeo.get("hispanic_latino", "No"))
        if any(k in label for k in ("please identify your race", "race", "ethnicity", "racial")):
            return str(eeo.get("race", "White"))
        if "veteran" in label:
            return str(eeo.get("veteran_status", "I am not a protected veteran"))
        if "disability" in label or "handicap" in label:
            return str(eeo.get("disability_status", "No, I do not have a disability"))
        if any(k in label for k in ("gender", "sex", "sexual orientation", "transgender", "pronoun")):
            return str(eeo.get("gender", "Decline To Self Identify"))
        return match_screening_answer(label, ctype, ctrl.get("options"))

    def _select_combobox_option(self, page: Any, loc: Any, label: str, target_val: str) -> bool:
        """Open a React-Select / ARIA combobox, inspect live listbox options, and click the best match."""
        lbl_low = (label or "").lower()
        t_low = (target_val or "").strip().lower()
        try:
            loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        try:
            loc.click(force=True, timeout=2500)
        except Exception:
            pass
        time.sleep(0.45)

        is_location_async = any(k in lbl_low for k in ("city", "candidate location", "reside", "current location"))
        if is_location_async:
            loc.fill(target_val, timeout=2500)
            time.sleep(1.35)

        options_locator = page.locator('[role="listbox"] [role="option"], .select__menu .select__option, [class*="menu" i] [class*="option" i]')
        count = options_locator.count()
        if count == 0 and not is_location_async:
            try:
                loc.press("ArrowDown", timeout=1500)
                time.sleep(0.45)
                count = options_locator.count()
            except Exception:
                pass
        if count == 0 and not is_location_async:
            loc.fill(target_val, timeout=2500)
            time.sleep(0.75)
            count = options_locator.count()

        if count > 0:
            opt_texts: List[str] = []
            for i in range(min(count, 25)):
                try:
                    opt_texts.append((options_locator.nth(i).inner_text(timeout=800) or "").strip())
                except Exception:
                    opt_texts.append("")

            chosen_idx: Optional[int] = None
            if "decline" in t_low:
                for i, ot in enumerate(opt_texts):
                    ol = ot.lower()
                    if any(p in ol for p in ("decline", "prefer not", "don't wish", "do not wish", "choose not", "not wish")):
                        chosen_idx = i
                        break
            elif t_low == "consent":
                for i, ot in enumerate(opt_texts):
                    ol = ot.lower()
                    if any(p in ol for p in ("consent", "agree", "acknowledge", "yes", "accept")):
                        chosen_idx = i
                        break
            elif "not a protected veteran" in t_low:
                for i, ot in enumerate(opt_texts):
                    ol = ot.lower()
                    if "not a protected veteran" in ol or "not a veteran" in ol or ol == "no":
                        chosen_idx = i
                        break
            elif "do not have a disability" in t_low:
                for i, ot in enumerate(opt_texts):
                    ol = ot.lower()
                    if "do not have a disability" in ol or "don't have a disability" in ol or ol.startswith("no, i"):
                        chosen_idx = i
                        break
            else:
                for i, ot in enumerate(opt_texts):
                    if ot.lower() == t_low:
                        chosen_idx = i
                        break
                if chosen_idx is None:
                    for i, ot in enumerate(opt_texts):
                        ol = ot.lower()
                        if (
                            t_low in ol
                            or (t_low == "yes" and ("authorized" in ol or ol.startswith("yes")))
                            or (t_low == "no" and (ol == "no" or ol.startswith("no,") or "not hispanic" in ol or "do not require" in ol))
                        ):
                            chosen_idx = i
                            break
                if chosen_idx is None and is_location_async and opt_texts[0]:
                    chosen_idx = 0

            if chosen_idx is not None:
                options_locator.nth(chosen_idx).click(timeout=2500)
                time.sleep(0.3)
                return True

        # Fallback keyboard selection
        if not is_location_async:
            loc.fill(target_val, timeout=2000)
            time.sleep(0.4)
        loc.press("Enter", timeout=2000)
        return True

    def _llm_plan_unknown_controls(
        self, snapshot: Dict[str, Any], creds: Dict[str, Any], job: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Ask Sabrina's fast local LLM to map remaining unfilled controls and select the next progression button."""
        try:
            unfilled_inputs = [
                c for c in snapshot.get("controls", [])
                if c["tag"] in ("input", "textarea", "select")
                and c["type"] not in ("file", "submit", "button", "hidden")
                and not c.get("value")
            ]
            if not unfilled_inputs:
                return []
            system_prompt = (
                "You are Sabrina's External ATS Form Agent. Given a JSON list of unfilled form controls and "
                "Charles McGowen's candidate profile, return ONLY a JSON object with an 'actions' list to fill inputs: "
                "{\"actions\": [{\"index\": <int>, \"op\": \"fill|select|check\", \"value\": \"<str>\"}]}. "
                "Never invent credentials or degrees not in the profile (bachelor_degree is No; highest education is High School). "
                "Do NOT emit 'click' actions on dropdowns or random fields; form submission is handled automatically after you fill fields."
            )
            compact_controls = [
                {
                    "index": c["index"],
                    "tag": c["tag"],
                    "type": c["type"],
                    "role": c.get("role", ""),
                    "label": c["label"],
                    "text": c["text"],
                    "required": c["required"],
                    "options": c.get("options", [])[:8],
                }
                for c in snapshot.get("controls", [])
                if not c.get("value")
            ][:35]
            op_prof = load_operator_profile()
            career_meta = op_prof.get("career") or {}
            user_prompt = json.dumps(
                {
                    "job": {"title": job.get("title"), "company": job.get("company")},
                    "page_url": snapshot.get("url"),
                    "page_title": snapshot.get("title"),
                    "candidate_profile": {
                        **{
                            k: self.profile[k]
                            for k in (
                                "full_name", "first_name", "last_name", "email", "phone",
                                "location", "linkedin_url", "github_url", "years_experience",
                                "work_authorization", "requires_sponsorship", "desired_salary",
                            )
                            if k in self.profile
                        },
                        "summary": career_meta.get("summary", ""),
                        "skills": (career_meta.get("skills") or [])[:15],
                        "ai_coding_tools": "Daily expert user of AI coding & agentic harnesses (Claude Code, Codex, Antigravity, custom Python multi-agent pipelines)",
                    },
                    "account_password_if_needed": creds["password"],
                    "controls": compact_controls,
                },
                ensure_ascii=False,
            )
            import urllib.request
            ollama_base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
            chosen_model = "sabrina-qwen2.5-coder:1.5b-clean"
            try:
                ps_data = json.loads(urllib.request.urlopen(ollama_base + "/api/ps", timeout=3).read().decode("utf-8"))
                if any(int(m.get("size_vram") or 0) > 0 for m in ps_data.get("models", [])):
                    chosen_model = "llama3.1:8b"
            except Exception:
                pass
            req_body = json.dumps({
                "model": chosen_model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {"num_predict": 450, "temperature": 0.1},
            }).encode("utf-8")
            http_req = urllib.request.Request(
                ollama_base + "/api/chat", data=req_body, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(http_req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            raw = ((data.get("message") or {}).get("content") or "").strip()
            parsed = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
            actions = parsed.get("actions") if isinstance(parsed, dict) else []
            return actions if isinstance(actions, list) else []
        except Exception as exc:
            logger.warning("LLM ATS form planner fallback: %s", exc)
            return []

    def apply_on_external_page(
        self, page: Any, job: Dict[str, Any], max_steps: int = 8
    ) -> Dict[str, Any]:
        """Drive an external ATS application page (Greenhouse, Lever, Workday, iCIMS, Ashby, Rippling) up to max_steps."""
        steps_trace: List[str] = []
        filled_once_ats: set[str] = set()
        uploaded_resume_urls: set[str] = set()
        clicked_keys: set[str] = set()
        spawned_sub_pages: List[Any] = []
        creds = self.vault.get_or_create_credentials(
            page.url, email=self.profile["email"], job_id=str(job.get("job_id") or "")
        )
        resume_path = self.profile.get("resume_path")

        try:
            for step_num in range(1, max_steps + 1):
                time.sleep(1.8)
                snapshot = page.evaluate(ATS_DOM_EXTRACTOR)
                cur_url = snapshot.get("url") or page.url or ""
                body_lower = (snapshot.get("body_excerpt") or "").lower()

                if re.search(
                    r"application submitted|thank you for applying|application has been received|you have applied|thanks for applying|we have received your application",
                    body_lower,
                ):
                    steps_trace.append(f"step_{step_num}_confirmed_submission")
                    return {
                        "status": "submitted",
                        "external_url": cur_url,
                        "ats_domain": creds["domain"],
                        "steps_trace": steps_trace,
                    }

                if snapshot.get("has_captcha") or re.search(
                    r"verify you are human|complete the captcha|cloudflare turnstile", body_lower
                ):
                    steps_trace.append(f"step_{step_num}_paused_on_captcha")
                    return {
                        "status": "awaiting_human_captcha",
                        "external_url": cur_url,
                        "ats_domain": creds["domain"],
                        "steps_trace": steps_trace,
                        "reason": "External ATS presented a human CAPTCHA challenge",
                    }

                # Check if page is asking for an email verification / security code (single box OR 6-8 individual OTP boxes)
                otp_boxes = [
                    c for c in snapshot.get("controls", [])
                    if c["tag"] == "input" and (c.get("maxlength") == "1" or "security input" in c["label"].lower())
                ]
                if len(otp_boxes) >= 6 or re.search(
                    r"security code|verification code|verify your email|check your email|enter the 6-digit|enter the 8-|verification pin|enter pin",
                    body_lower,
                ):
                    ver = self.check_gmail_verification(page.context, job.get("company") or creds["domain"])
                    if ver.get("link"):
                        steps_trace.append(f"step_{step_num}_followed_gmail_verification_link")
                        page.goto(ver["link"], wait_until="domcontentloaded", timeout=25000)
                        self.vault.mark_verified(page.url)
                        continue
                    if ver.get("code"):
                        code_str = str(ver["code"]).strip()
                        if len(otp_boxes) >= len(code_str):
                            for ch_i, ch in enumerate(code_str):
                                loc = page.locator(f'[data-sabrina-ats-idx="{otp_boxes[ch_i]["index"]}"]').first
                                if loc.count() > 0:
                                    loc.fill(ch, timeout=2000)
                            steps_trace.append(f"step_{step_num}_filled_multibox_security_code:{len(code_str)}")
                            self.vault.mark_verified(page.url)
                        else:
                            for ctrl in snapshot.get("controls", []):
                                if ctrl["tag"] == "input" and (
                                    any(k in ctrl["label"].lower() for k in ("code", "pin", "passcode", "verify", "security"))
                                    or ctrl["type"] in ("text", "number", "tel")
                                ):
                                    loc = page.locator(f'[data-sabrina-ats-idx="{ctrl["index"]}"]').first
                                    if loc.count() > 0:
                                        loc.fill(code_str, timeout=3000)
                                        steps_trace.append(f"step_{step_num}_filled_gmail_verification_code")
                                        self.vault.mark_verified(page.url)
                                        break

                # 1. Upload resume once per URL to non-cover-letter file input
                if cur_url not in uploaded_resume_urls and resume_path and Path(resume_path).exists():
                    for ctrl in snapshot.get("controls", []):
                        if ctrl.get("type") == "file":
                            lbl = (ctrl.get("label") or "").lower()
                            cid = (ctrl.get("id") or "").lower()
                            if "cover" in lbl or "cover" in cid or "additional" in lbl:
                                continue
                            try:
                                rp = Path(resume_path)
                                mime = "application/pdf" if rp.suffix.lower() == ".pdf" else "text/html"
                                loc = page.locator(f'[data-sabrina-ats-idx="{ctrl["index"]}"]').first
                                loc.set_input_files(
                                    {"name": rp.name, "mimeType": mime, "buffer": rp.read_bytes()},
                                    timeout=6000,
                                )
                                uploaded_resume_urls.add(cur_url)
                                steps_trace.append(f"step_{step_num}_uploaded_resume:{rp.name}")
                                break
                            except Exception:
                                pass

                # 2. Deterministic fill (2 cascading sub-passes so dependent fields like Greenhouse 'Race' after 'Hispanic=No' are filled immediately)
                for sub_pass in range(2):
                    if sub_pass == 1:
                        time.sleep(0.5)
                        snapshot = page.evaluate(ATS_DOM_EXTRACTOR)
                    changed_in_subpass = False
                    for ctrl in snapshot.get("controls", []):
                        if ctrl["tag"] in ("input", "textarea") and ctrl["type"] not in ("file", "submit", "button", "checkbox", "radio"):
                            if ctrl.get("maxlength") == "1":
                                continue
                            cur_val = (ctrl.get("value") or "").strip()
                            lbl_low = (ctrl.get("label") or "").lower()
                            ctrl_key = f"{cur_url}|{ctrl['tag']}:{ctrl.get('id') or lbl_low[:45]}"
                            if ctrl_key in filled_once_ats:
                                continue
                            is_phone_ctrl = ctrl.get("type") == "tel" or (("phone" in lbl_low or "mobile" in lbl_low) and "country code" not in lbl_low)
                            if cur_val and not (is_phone_ctrl and re.sub(r"\D", "", cur_val)[-10:] != re.sub(r"\D", "", self.profile["phone"])[-10:]):
                                continue
                            val = self._deterministic_field_value(ctrl, creds)
                            if val is not None:
                                try:
                                    loc = page.locator(f'[data-sabrina-ats-idx="{ctrl["index"]}"]').first
                                    if ctrl.get("role") == "combobox":
                                        self._select_combobox_option(page, loc, ctrl["label"], str(val))
                                        changed_in_subpass = True
                                    else:
                                        loc.fill(str(val), timeout=3000)
                                    filled_once_ats.add(ctrl_key)
                                    steps_trace.append(f"step_{step_num}_filled:{ctrl['label'][:30]}")
                                except Exception:
                                    pass
                        elif ctrl["tag"] == "select" and not ctrl.get("value"):
                            val = self._deterministic_field_value(ctrl, creds)
                            if val is not None:
                                try:
                                    loc = page.locator(f'[data-sabrina-ats-idx="{ctrl["index"]}"]').first
                                    target_s = str(val).lower()
                                    for opt in ctrl.get("options", []):
                                        ol = opt.lower()
                                        if (
                                            target_s in ol
                                            or ("decline" in target_s and any(p in ol for p in ("decline", "prefer not", "don't wish", "do not wish")))
                                            or (target_s == "consent" and any(p in ol for p in ("consent", "agree", "acknowledge", "yes")))
                                        ):
                                            loc.select_option(label=opt, timeout=3000)
                                            changed_in_subpass = True
                                            steps_trace.append(f"step_{step_num}_selected:{ctrl['label'][:30]}={opt[:20]}")
                                            break
                                except Exception:
                                    pass
                        elif ctrl["type"] == "checkbox" and not ctrl.get("checked"):
                            cb_lbl = ctrl.get("label") or ""
                            if re.search(r"terms|privacy|acknowledge|consent|agree", cb_lbl, re.I) or match_screening_answer(cb_lbl, "checkbox") == "Yes":
                                try:
                                    page.locator(f'[data-sabrina-ats-idx="{ctrl["index"]}"]').first.check(timeout=3000)
                                    steps_trace.append(f"step_{step_num}_checked:{cb_lbl[:28]}")
                                except Exception:
                                    pass
                    if not changed_in_subpass:
                        break

                # Determine whether real application form fields are currently visible on screen
                refreshed = page.evaluate(ATS_DOM_EXTRACTOR)
                form_inputs_visible = any(
                    c["tag"] in ("input", "textarea", "select")
                    and c["type"] not in ("submit", "button", "hidden")
                    and "display language" not in (c.get("label") or "").lower()
                    and "search" not in (c.get("label") or "").lower()
                    for c in refreshed.get("controls", [])
                )

                # 3. LLM field planner for remaining custom controls
                llm_actions = self._llm_plan_unknown_controls(refreshed, creds, job)
                ctrl_by_idx = {c["index"]: c for c in refreshed.get("controls", [])}
                clicked_advance = False
                for act in llm_actions:
                    idx = act.get("index")
                    op = act.get("op")
                    val = str(act.get("value") or "")
                    if idx is None:
                        continue
                    loc = page.locator(f'[data-sabrina-ats-idx="{idx}"]').first
                    if loc.count() == 0:
                        continue
                    cmeta = ctrl_by_idx.get(idx) or {}
                    is_combo = cmeta.get("role") == "combobox"
                    try:
                        if op in ("fill", "select") and val and is_combo:
                            self._select_combobox_option(page, loc, cmeta.get("label", ""), val)
                            steps_trace.append(f"step_{step_num}_llm_combo:{idx}")
                        elif op == "fill" and val:
                            loc.fill(val, timeout=3000)
                            steps_trace.append(f"step_{step_num}_llm_fill:{idx}")
                        elif op == "select" and val:
                            loc.select_option(label=val, timeout=3000)
                            steps_trace.append(f"step_{step_num}_llm_select:{idx}")
                        elif op == "check":
                            if cmeta.get("type") == "radio":
                                loc.click(timeout=3000)
                            else:
                                try:
                                    loc.check(timeout=3000)
                                except Exception:
                                    loc.click(timeout=3000)
                            steps_trace.append(f"step_{step_num}_llm_check:{idx}")
                        elif op == "click" and not clicked_advance:
                            btn_txt = (cmeta.get("text") or cmeta.get("label") or "").strip().lower()
                            click_key = f"{cur_url}|idx:{idx}"
                            # Only allow LLM click if it is genuinely a submit/next/continue progression button
                            if click_key in clicked_keys:
                                continue
                            if not re.search(r"\b(submit|continue|next|create account|review|save|send)\b", btn_txt):
                                continue
                            loc.click(timeout=5000)
                            clicked_keys.add(click_key)
                            clicked_advance = True
                            steps_trace.append(f"step_{step_num}_llm_click:{idx}:{btn_txt[:20]}")
                    except Exception:
                        continue

                # 4. Strict Two-Tier Deterministic Button Progression
                if not clicked_advance:
                    if form_inputs_visible:
                        # Tier A: Inside a form — prioritize Submit Application / Continue / Next / button[type=submit]
                        tier_a_selectors = [
                            'button[type="submit"]:has-text("Submit")',
                            'button:has-text("Submit Application")',
                            'button:has-text("Submit application")',
                            'input[type="submit"][value*="Submit" i]',
                            'button:has-text("Review and Submit")',
                            'button:has-text("Save and Continue")',
                            'button:has-text("Save & Continue")',
                            'button:has-text("Continue")',
                            'button:has-text("Next")',
                            'button:has-text("Create Account")',
                            'button:has-text("I Agree")',
                            'button:has-text("Submit")',
                            'button[type="submit"]',
                            'input[type="submit"]',
                        ]
                        for sel in tier_a_selectors:
                            loc = page.locator(sel).first
                            if loc.count() > 0 and loc.is_visible():
                                click_key = f"{cur_url}|tierA:{sel}:{step_num}"
                                try:
                                    loc.scroll_into_view_if_needed(timeout=2000)
                                    loc.click(timeout=5000)
                                    clicked_keys.add(click_key)
                                    steps_trace.append(f"step_{step_num}_clicked_submit:{sel[:32]}")
                                    clicked_advance = True
                                    time.sleep(3.2)
                                    break
                                except Exception:
                                    continue
                    else:
                        # Tier B: Landing / Job Overview page (no form inputs visible yet) — click Apply Now / Apply Manually
                        tier_b_selectors = [
                            'button:has-text("Apply Manually")',
                            'a:has-text("Apply Manually")',
                            'button:has-text("Apply Now")',
                            'a:has-text("Apply Now")',
                            'button:has-text("Apply now")',
                            'a:has-text("Apply now")',
                            'button:has-text("Apply for this job")',
                            'a:has-text("Apply for this job")',
                            'button:has-text("Continue with Email")',
                            'button:has-text("Sign up with email")',
                            'button:has-text("Apply")',
                            'a:has-text("Apply")',
                        ]
                        for sel in tier_b_selectors:
                            loc = page.locator(sel).first
                            if loc.count() > 0 and loc.is_visible():
                                click_key = f"{cur_url}|tierB:{sel}"
                                if click_key in clicked_keys:
                                    continue
                                try:
                                    before_sub = set(page.context.pages)
                                    loc.click(timeout=5000)
                                    clicked_keys.add(click_key)
                                    steps_trace.append(f"step_{step_num}_clicked_entry:{sel[:32]}")
                                    clicked_advance = True
                                    time.sleep(3.0)
                                    new_sub = [p for p in page.context.pages if p not in before_sub]
                                    if new_sub:
                                        spawned_sub_pages.extend(new_sub)
                                        page = new_sub[-1]
                                    break
                                except Exception:
                                    continue

                if not clicked_advance:
                    break

            # Final check after loop
            final_snap = page.evaluate(ATS_DOM_EXTRACTOR)
            final_body = (final_snap.get("body_excerpt") or "").lower()
            if re.search(
                r"application submitted|thank you for applying|application has been received|you have applied|thanks for applying|we have received your application",
                final_body,
            ):
                return {
                    "status": "submitted",
                    "external_url": final_snap.get("url"),
                    "ats_domain": creds["domain"],
                    "steps_trace": steps_trace,
                }
            return {
                "status": "external_in_progress",
                "external_url": final_snap.get("url"),
                "ats_domain": creds["domain"],
                "steps_trace": steps_trace,
            }
        finally:
            for sp in spawned_sub_pages:
                try:
                    if not sp.is_closed():
                        sp.close()
                except Exception:
                    pass
