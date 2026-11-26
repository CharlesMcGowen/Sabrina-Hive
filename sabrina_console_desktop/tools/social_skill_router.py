# -*- coding: utf-8 -*-
"""
tools/social_skill_router.py
============================
Dynamic Skill-Based Response Router for Sabrina's Social & DM Interactions.

Replaces monolithic catch-all prompts with scoped `SKILL.md` playbooks selected from
conversation evidence (`reader_artifact` + latest external message), ensuring rules for
resume delivery, compensation boundaries, or call scheduling ONLY enter the prompt when
that specific situation is present.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_ROOT = REPO_ROOT / "skills"

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F1E6-\U0001F1FF]"
)

_RESUME_ASK_RE = re.compile(r"\b(?:resume|cv|updated\s+profile|send\s+over\s+your)\b", re.I)
_COMP_ASK_RE = re.compile(
    r"\b(?:hourly\s+rate|pay\s+rate|target\s+rate|desired\s+rate|salary\s+expectation|"
    r"desired\s+salary|target\s+comp|what\s+rate|your\s+rate|expected\s+compensation)\b",
    re.I,
)
_CALL_OR_CHAT_RE = re.compile(
    r"\b(?:schedule\s+a\s+(?:call|time|chat)|hop\s+on\s+a\s+call|set\s+up\s+a\s+time|"
    r"comfortable\s+to\s+chat|time\s+to\s+(?:chat|speak|talk|connect)|available\s+for\s+a\s+(?:call|chat))\b",
    re.I,
)


def read_skill_body(skill_name: str) -> str:
    """Load a skill's Markdown body from `skills/<skill_name>/SKILL.md`."""
    path = SKILLS_ROOT / skill_name / "SKILL.md"
    if not path.exists():
        return ""
    raw = path.read_text(encoding="utf-8")
    # Strip YAML frontmatter for compact prompt injection
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return raw.strip()


def detect_sender_emoji_tone(thread: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect external messages in `thread` to calibrate natural tone & emoji mirroring."""
    ext_msgs = [m for m in (thread.get("messages") or []) if m.get("ownership") == "external"]
    latest_text = (ext_msgs[-1].get("text") or "") if ext_msgs else ""
    emojis_found = _EMOJI_RE.findall(latest_text)
    self_msgs = [m for m in (thread.get("messages") or []) if m.get("ownership") == "self"]
    has_sensitive_pause = any(
        re.search(r"\b(?:family\s+emergency|personal\s+emergency|hospital|bereavement)\b", m.get("text") or "", re.I)
        for m in self_msgs
    )
    if has_sensitive_pause:
        return {
            "tone": "warm_empathetic_serious",
            "sender_used_emojis": False,
            "emoji_budget": 0,
            "guidance": "Sensitive personal context exists in thread: use 0 emojis, warm human brevity, and do NOT propose phone calls.",
        }
    if emojis_found:
        return {
            "tone": "warm_casual_peer",
            "sender_used_emojis": True,
            "emoji_budget": 1,
            "guidance": "Sender used friendly emojis: match their warm, conversational tone and optionally include at most 1 natural emoji (e.g. 👋 or 👍), never emoji clusters.",
        }
    return {
        "tone": "natural_conversational_direct",
        "sender_used_emojis": False,
        "emoji_budget": 0,
        "guidance": "Sender wrote without emojis: reply in relaxed, natural human prose with 0 emojis.",
    }


def select_dm_skills(
    thread: Dict[str, Any],
    reader_artifact: Optional[Dict[str, Any]] = None,
    external_portal_outcome: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Select the minimal set of focused `SKILL.md` playbooks needed for this exact DM turn."""
    from tools.dm_external_portal_bridge import extract_external_job_urls

    msgs = thread.get("messages") or []
    ext_msgs = [m for m in msgs if m.get("ownership") == "external"]
    latest_ext = (ext_msgs[-1].get("text") or "") if ext_msgs else ""
    reader = reader_artifact or {}
    latest_req = str(reader.get("latest_request") or "")
    combined_focus = f"{latest_ext}\n{latest_req}"

    selected: List[str] = []
    ext_urls = extract_external_job_urls(thread)
    if external_portal_outcome or ext_urls:
        selected.append("dm-external-portal-apply")
    if _RESUME_ASK_RE.search(combined_focus):
        selected.append("dm-resume-delivery")
    if _COMP_ASK_RE.search(combined_focus):
        selected.append("dm-compensation-boundary")
    if _CALL_OR_CHAT_RE.search(combined_focus) or reader.get("intent") == "scheduling":
        selected.append("dm-schedule-coordination")
    if not selected:
        selected.append("dm-recruiter-intro")

    tone_meta = detect_sender_emoji_tone(thread)
    skill_blocks = []
    for sname in selected:
        body = read_skill_body(sname)
        if body:
            skill_blocks.append(f"=== ACTIVE SKILL: {sname} ===\n{body}")
    if external_portal_outcome:
        import json
        skill_blocks.append(
            "=== VERIFIED EXTERNAL PORTAL OUTCOME (GROUND TRUTH) ===\n"
            + json.dumps(external_portal_outcome, ensure_ascii=False)
        )

    return {
        "selected_skills": selected,
        "tone": tone_meta,
        "external_portal_urls": ext_urls,
        "compiled_skill_prompt": (
            "\n\n".join(skill_blocks)
            + f"\n\n=== TONE & EMOJI CALIBRATION ===\n{tone_meta['guidance']}"
        ).strip(),
    }


def select_x_skills(task_kind: str, target_text: str = "") -> Dict[str, Any]:
    """Select and compile modular X.com (`skills/x-*`) playbooks for the exact X turn."""
    kind = (task_kind or "post").strip().lower()
    mapping = {
        "hook_study": ["x-hook-extractor"],
        "reply": ["x-reply-drafter", "x-humanizer"],
        "quote": ["x-reply-drafter", "x-humanizer"],
        "mention": ["x-reply-drafter", "x-humanizer"],
        "post": ["x-twitter-growth", "x-hook-extractor", "x-humanizer"],
        "strategy": ["x-engagement-routine", "x-twitter-growth"],
    }
    selected = list(mapping.get(kind, ["x-twitter-growth", "x-humanizer"]))

    blocks: List[str] = []
    for sname in selected:
        body = read_skill_body(sname)
        if body:
            blocks.append(f"=== ACTIVE X SKILL: {sname} ===\n{body}")

    has_question = "?" in (target_text or "")
    extra_hint = ""
    if kind == "reply" and has_question:
        extra_hint = "\nTarget post contains a question: prefer shape R1 (Direct Answer + 1 concrete detail)."
    elif kind == "quote":
        extra_hint = "\nUse shape R5 (Value-Add Quote Reframe); never cheap-dunk small accounts."

    return {
        "task_kind": kind,
        "selected_skills": selected,
        "compiled_skill_prompt": ("\n\n".join(blocks) + extra_hint).strip(),
    }

