"""Review-first outreach template catalog.

Templates are reusable copy, not sending authority.  Rendering for a campaign
requires an approved profile containing every fact named by the template and an
explicit exact-text authorization from the user.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from tools.messaging_skills import load_profile
from tools.messaging_store import ROOT, digest


TEMPLATES_PATH = ROOT / "config/recruiter_outreach_templates.json"
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_ALLOWED_PLACEHOLDERS = {"first_name", "your_name", "company", "role"}


def load_catalog(path=None):
    value = json.loads(Path(path or TEMPLATES_PATH).read_text(encoding="utf-8-sig"))
    if not isinstance(value.get("version"), int) or not isinstance(value.get("templates"), list):
        raise ValueError("Outreach template catalog needs a version and template list")
    ids = set()
    for template in value["templates"]:
        _validate_template(template, ids)
    return value


def _validate_template(template, existing_ids=None):
    if not isinstance(template, dict) or not template.get("id") or not template.get("name"):
        raise ValueError("Each outreach template needs an ID and name")
    if existing_ids is not None:
        if template["id"] in existing_ids:
            raise ValueError("Duplicate outreach template ID: " + template["id"])
        existing_ids.add(template["id"])
    text = template.get("text", "")
    if not isinstance(text, str) or not 15 <= len(text) <= 1400:
        raise ValueError("Outreach template text must be between 15 and 1400 characters")
    placeholders = _PLACEHOLDER.findall(text)
    declared = template.get("placeholders", [])
    if set(placeholders) - _ALLOWED_PLACEHOLDERS:
        raise ValueError("Unsupported outreach template placeholder")
    if set(placeholders) != set(declared):
        raise ValueError("Declared placeholders do not match template text")
    required = template.get("required_profile_facts", [])
    if not isinstance(required, list) or len(required) != len(set(required)):
        raise ValueError("Template required_profile_facts must be a unique list")
    if template.get("risk") != "human_review_required":
        raise ValueError("Every outreach template must require human review")
    if template.get("status") not in {"draft", "approved"}:
        raise ValueError("Unsupported outreach template status")


def get_template(template_id, path=None):
    for template in load_catalog(path)["templates"]:
        if template["id"] == template_id:
            return template
    raise KeyError(template_id)


def profile_readiness(template, profile=None):
    profile = profile if profile is not None else load_profile()
    approved = {fact["id"] for fact in profile.get("facts", []) if fact.get("approved") is True}
    missing = [fact_id for fact_id in template.get("required_profile_facts", []) if fact_id not in approved]
    return {"ready": not missing, "missing_profile_facts": missing, "profile_version": profile.get("version", 0)}


def preview_template(template_id, values=None, path=None):
    """Return copy suitable for the dashboard, without claiming send readiness."""
    template = get_template(template_id, path)
    values = values or {}
    text = template["text"]
    missing_values = []
    for placeholder in template["placeholders"]:
        if not isinstance(values.get(placeholder), str) or not values[placeholder].strip():
            missing_values.append(placeholder)
        else:
            text = text.replace("{" + placeholder + "}", values[placeholder].strip())
    return {"id": template["id"], "name": template["name"], "audience": template["audience"],
            "purpose": template["purpose"], "text": text, "missing_values": missing_values,
            "required_profile_facts": template["required_profile_facts"], "risk": template["risk"],
            "status": template["status"]}


def render_template(template_id, values, profile=None, path=None):
    template = get_template(template_id, path)
    readiness = profile_readiness(template, profile)
    if not readiness["ready"]:
        raise ValueError("Template requires approved profile facts: " + ", ".join(readiness["missing_profile_facts"]))
    preview = preview_template(template_id, values, path)
    if preview["missing_values"]:
        raise ValueError("Template values are missing: " + ", ".join(preview["missing_values"]))
    if not 15 <= len(preview["text"]) <= 1400:
        raise ValueError("Rendered outreach text must be between 15 and 1400 characters")
    return preview["text"]


def campaign_scope(template_id, account_id, authorization, values, profile=None, path=None, campaign_id=None):
    """Build the exact scope object consumed by recruiter_campaign.initialize."""
    if not account_id or not authorization or not authorization.strip():
        raise ValueError("Account and explicit user authorization are required")
    text = render_template(template_id, values, profile, path)
    template = get_template(template_id, path)
    return {"id": campaign_id or "template-" + template_id + "-" + digest([account_id, text])[:12],
            "account_id": account_id, "exact_text": text,
            "user_instruction": authorization.strip(), "template_id": template_id}


def list_templates(profile=None, path=None):
    profile = profile if profile is not None else load_profile()
    result = []
    for template in load_catalog(path)["templates"]:
        result.append({**template, "readiness": profile_readiness(template, profile)})
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "preview"])
    parser.add_argument("--template")
    args = parser.parse_args()
    if args.action == "list":
        print(json.dumps(list_templates(), ensure_ascii=False, indent=2))
    else:
        if not args.template:
            parser.error("--template is required for preview")
        print(json.dumps(preview_template(args.template), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

