# -*- coding: utf-8 -*-
"""
tools/operator_profile.py
=========================
Unified cross-platform loader and model-skill resolver for `config/operator_profile.json`.

Eliminates duplicated hardcoded profile dicts across LinkedIn Easy Apply, External ATS,
Recruiter DM Swarm, and Social Post Swarm, and exposes the `operator-profile-identity`
skill so Sabrina's local LLM resolves contact/phone/location/screening fields cleanly
without re-typing fields in loops.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OPERATOR_PROFILE_PATH = REPO_ROOT / "config" / "operator_profile.json"
SKILL_MD_PATH = REPO_ROOT / "skills" / "operator-profile-identity" / "SKILL.md"


def load_operator_profile(path: Optional[Path | str] = None) -> Dict[str, Any]:
    """Load the single canonical cross-platform operator profile."""
    target = Path(path or os.environ.get("SABRINA_OPERATOR_PROFILE", DEFAULT_OPERATOR_PROFILE_PATH))
    return json.loads(target.read_text(encoding="utf-8-sig"))


def load_profile_skill_instructions() -> str:
    """Read the `operator-profile-identity` SKILL.md instructions for model-guided resolution."""
    if SKILL_MD_PATH.exists():
        return SKILL_MD_PATH.read_text(encoding="utf-8")
    return "Resolve fields strictly from operator_profile.json; return action='skip' when unknown."


def to_screening_qa_bank(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Project `config/operator_profile.json` into the Easy Apply Q&A bank structure."""
    p = profile or load_operator_profile()
    contact = p.get("contact") or {}
    loc = p.get("location") or {}
    auth = p.get("work_authorization") or {}
    edu = p.get("education") or {}
    eeo = p.get("eeo_demographics") or {}
    links = p.get("links") or {}
    career = p.get("career") or {}
    comp = career.get("compensation_and_notice") or {}
    return {
        "work_authorization": {
            "authorized_us": auth.get("authorized_us", "Yes"),
            "require_sponsorship": auth.get("requires_sponsorship", "No"),
            "us_citizen": auth.get("us_citizen", "Yes"),
        },
        "years_experience": dict(career.get("years_by_skill") or {"default_years": career.get("years_experience_total", "7")}),
        "location_and_workplace": {
            "city": loc.get("city_state", "Cumming, Georgia"),
            "city_only": loc.get("city", "Cumming"),
            "state": loc.get("state", "Georgia"),
            "state_abbr": loc.get("state_abbr", "GA"),
            "zip": loc.get("postal_code", "30040"),
            "commute_or_relocate": loc.get("willing_to_commute", "Yes"),
            "remote_or_hybrid": loc.get("remote_or_hybrid", "Yes"),
        },
        "compensation_and_notice": {
            "salary_annual": str(comp.get("salary_annual", "165000")),
            "hourly_rate": str(comp.get("hourly_rate", "85")),
            "notice_period_weeks": str(comp.get("notice_period_weeks", "2")),
            "start_date": str(comp.get("start_date", "Immediately")),
        },
        "education_and_links": {
            "bachelor_degree": edu.get("bachelor_degree", "No"),
            "degree": edu.get("degree", "High School"),
            "linkedin_url": links.get("linkedin_url", "https://www.linkedin.com/in/charlie-mcgowen/"),
            "github_url": links.get("github_url", "https://github.com/CharlesMcGowen"),
            "current_title": career.get("current_title", "Senior Security Engineer"),
            "current_company": career.get("current_company", ""),
        },
        "eeo_demographics": {
            "gender": eeo.get("gender", "Decline To Self Identify"),
            "hispanic_latino": eeo.get("hispanic_latino", "No"),
            "race": eeo.get("race", "White"),
            "veteran_status": eeo.get("veteran_status", "I am not a protected veteran"),
            "disability_status": eeo.get("disability_status", "No, I do not have a disability"),
        },
        "contact": {
            "phone": contact.get("phone_digits", "4794039960"),
            "phone_formatted": contact.get("phone_formatted", "(479) 403-9960"),
            "phone_e164": contact.get("phone_e164", "+14794039960"),
            "phone_country_code": contact.get("phone_country_code", "United States (+1)"),
            "email": contact.get("email", "charlesmcgowen@gmail.com"),
        },
    }


def to_ats_candidate_profile(profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Project `config/operator_profile.json` into the External ATS candidate dictionary."""
    p = profile or load_operator_profile()
    ident = p.get("identity") or {}
    contact = p.get("contact") or {}
    loc = p.get("location") or {}
    links = p.get("links") or {}
    auth = p.get("work_authorization") or {}
    edu = p.get("education") or {}
    eeo = p.get("eeo_demographics") or {}
    career = p.get("career") or {}
    comp = career.get("compensation_and_notice") or {}
    return {
        "first_name": ident.get("first_name", "Charles"),
        "last_name": ident.get("last_name", "McGowen"),
        "full_name": ident.get("full_name", "Charles McGowen"),
        "email": contact.get("email", "charlesmcgowen@gmail.com"),
        "phone": os.environ.get("SABRINA_CANDIDATE_PHONE", contact.get("phone_digits", "4794039960")),
        "phone_formatted": contact.get("phone_formatted", "(479) 403-9960"),
        "phone_e164": contact.get("phone_e164", "+14794039960"),
        "city": loc.get("city", "Cumming"),
        "state": loc.get("state_abbr", "GA"),
        "postal_code": loc.get("postal_code", "30040"),
        "country": loc.get("country", "United States"),
        "location": loc.get("location_short", "Cumming, GA"),
        "linkedin_url": links.get("linkedin_url", "https://www.linkedin.com/in/charlie-mcgowen/"),
        "github_url": links.get("github_url", "https://github.com/CharlesMcGowen"),
        "website_url": links.get("website_url", "https://github.com/CharlesMcGowen"),
        "current_title": career.get("current_title", "Senior Security Engineer"),
        "current_company": career.get("current_company", ""),
        "years_experience": str(career.get("years_experience_total", "7")),
        "work_authorization": auth.get("authorized_us", "Yes"),
        "requires_sponsorship": auth.get("requires_sponsorship", "No"),
        "willing_to_relocate": loc.get("willing_to_relocate", "Yes"),
        "preferred_work_setting": "Remote or Hybrid",
        "desired_salary": str(comp.get("salary_annual", "165000")),
        "resume_filename": career.get("resume_filename", "Charles_McGowen_Resume_2026.pdf"),
        "education": {
            "degree": edu.get("degree", "High School"),
            "field_of_study": edu.get("field_of_study", ""),
        },
        "eeo_demographics": {
            "gender": eeo.get("gender", "Decline To Self Identify"),
            "hispanic_latino": eeo.get("hispanic_latino", "No"),
            "race": eeo.get("race", "White"),
            "veteran_status": eeo.get("veteran_status", "I am not a protected veteran"),
            "disability_status": eeo.get("disability_status", "No, I do not have a disability"),
        },
        "skills": list(career.get("skills") or []),
        "summary": career.get("summary", ""),
    }


def sync_messaging_profile_file(target_path: Optional[Path] = None) -> Dict[str, Any]:
    """Keep `data/messaging_profile.json` synchronized with `config/operator_profile.json`."""
    p = load_operator_profile()
    out_path = Path(target_path or (REPO_ROOT / "data" / "messaging_profile.json"))
    payload = {
        "version": int(p.get("schema_version", 1)) + 3,
        "facts": list(p.get("approved_messaging_facts") or []),
        "preferences": {},
        "availability": None,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def phone_matches_profile(raw_value: str, profile: Optional[Dict[str, Any]] = None) -> bool:
    """Return True if `raw_value` already represents the canonical 10-digit phone in any formatting."""
    p = profile or load_operator_profile()
    target_digits = re.sub(r"\D", "", str((p.get("contact") or {}).get("phone_digits", "")))[-10:]
    cur_digits = re.sub(r"\D", "", str(raw_value or ""))[-10:]
    return bool(target_digits and len(cur_digits) == 10 and cur_digits == target_digits)


RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "resolutions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_key": {"type": "string"},
                    "action": {"type": "string", "enum": ["fill", "select", "check", "skip"]},
                    "value": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["field_key", "action", "value", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["resolutions"],
    "additionalProperties": False,
}


def resolve_fields_with_profile_skill(
    fields: List[Dict[str, Any]],
    inline_error_text: str = "",
    profile: Optional[Dict[str, Any]] = None,
    model_call: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Invoke Sabrina's LLM equipped with `skills/operator-profile-identity/SKILL.md` and
    `config/operator_profile.json` to resolve ambiguous or inline-error form controls in a single pass.
    """
    if not fields:
        return []
    prof = profile or load_operator_profile()
    skill_md = load_profile_skill_instructions()
    compact_prof = {
        k: prof[k]
        for k in ("identity", "contact", "location", "links", "work_authorization", "education", "eeo_demographics", "career", "approved_messaging_facts")
        if k in prof
    }
    compact_prof = {
        **compact_prof,
        "career": {k: v for k, v in (compact_prof.get("career") or {}).items() if k != "skills"},
    }
    user_payload = {
        "operator_profile": compact_prof,
        "inline_validation_banner": (inline_error_text or "")[:400],
        "controls_to_resolve": fields[:16],
    }

    if model_call is not None:
        raw = model_call(skill_md, user_payload, RESOLUTION_SCHEMA)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return list((parsed or {}).get("resolutions") or [])

    from tools.control_client import caller, configured
    client = configured()
    if client is None:
        return []
    with caller("profile-skill-resolver", "social"):
        receipt = client.request(
            "/v1/inference",
            {
                "client": client.client,
                "project": client.project,
                "operation_id": "profile-skill-" + uuid.uuid4().hex[:16],
                "path": "/api/chat",
                "payload": {
                    "model": "gemma3:12b",
                    "stream": False,
                    "format": RESOLUTION_SCHEMA,
                    "messages": [
                        {"role": "system", "content": skill_md},
                        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                    ],
                    "options": {"num_predict": 900, "num_ctx": 8192, "temperature": 0.2},
                },
                "request_seconds": 120,
                "task_label": "Operator profile skill form resolution (gemma3:12b)",
            },
        )
        res = client.wait(receipt["id"], seconds=180)
        content = ((res.get("message") or {}).get("content") or "").strip()
        if not content:
            return []
        parsed = json.loads(content)
        return list((parsed or {}).get("resolutions") or [])
