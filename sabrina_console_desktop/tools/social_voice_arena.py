# -*- coding: utf-8 -*-
"""
tools/social_voice_arena.py
===========================
Multi-Family Social Communication & Human EQ Arena for Sabrina.

Evaluates candidate communicator models across genuinely distinct model families
(`Google Gemma`, `Nous Hermes / Meta Llama`, `Mistral`, `Qwen`) using Sabrina's
modular `SKILL.md` response system (`tools/social_skill_router.py`) and audits
every verbatim output with Disjoint-Family `IBM Granite` (`granite3.1-dense:8b`).

Saves 100% verbatim outputs, token counts, latencies, and scores to
`data/social/voice_arena_results.json` (zero assistant ghostwriting per AGENTS.md).
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from tools.control_client import caller, configured
from tools.operator_profile import load_operator_profile
from tools.social_skill_router import read_skill_body, select_dm_skills

REPO_ROOT = Path(__file__).resolve().parent.parent
ARENA_OUT_PATH = REPO_ROOT / "data" / "social" / "voice_arena_results.json"

CANDIDATE_DRAFTERS = [
    {"model": "qwen2.5:7b", "family": "Qwen (Baseline)"},
    {"model": "dolphin-mistral:7b", "family": "Mistral"},
    {"model": "hermes3:8b", "family": "Nous Hermes 3 (Llama)"},
    {"model": "gemma3:4b", "family": "Google Gemma 3 (4B)"},
    {"model": "gemma3:12b", "family": "Google Gemma 3 (12B)"},
]

AUDITOR_MODEL = {"model": "granite3.1-dense:8b", "family": "IBM Granite"}

CLICHE_RE = re.compile(
    r"\b(?:i\s+am\s+indeed|thrilled|delve|ever-evolving|game-changer|aligns\s+seamlessly|"
    r"at\s+your\s+earliest\s+convenience|thank\s+you\s+for\s+your\s+outreach|testament\s+to)\b",
    re.I,
)

SCENARIOS: List[Dict[str, Any]] = [
    {
        "id": "warm_recruiter_emoji_intro",
        "kind": "dm",
        "title": "Warm In-House Recruiter w/ Emojis (Forward Financing AppSec)",
        "thread": {
            "thread_id": "arena-1",
            "version": 1,
            "messages": [
                {
                    "id": "m1",
                    "ownership": "external",
                    "text": (
                        "Hi Charles 👋 I hope you had a great week! I noticed your background in pentesting and Python "
                        "and wanted to introduce you to Forward Financing. We're building out our dedicated AppSec team "
                        "(Remote, $145k-$183k + 10% bonus) and looking for builders who go beyond basic scanners into "
                        "AI security and real remediation. Let me know if you're open to learning more 🙋🏻‍♀️"
                    ),
                }
            ],
        },
    },
    {
        "id": "sensitive_async_chat_check",
        "kind": "dm",
        "title": "Sensitive Follow-Up After Family Emergency ('are you comfortable to chat here?')",
        "thread": {
            "thread_id": "arena-2",
            "version": 2,
            "messages": [
                {
                    "id": "m1",
                    "ownership": "self",
                    "text": "I'm sorry, I'm dealing with a family emergency right now and can't hop on phone calls.",
                },
                {
                    "id": "m2",
                    "ownership": "external",
                    "text": "Okay no prob. are you comfortable to chat here?",
                },
            ],
        },
    },
    {
        "id": "resume_and_rate_contract",
        "kind": "dm",
        "title": "Remote Pentest Contract Asking for Resume + Target Hourly Rate",
        "thread": {
            "thread_id": "arena-3",
            "version": 1,
            "messages": [
                {
                    "id": "m1",
                    "ownership": "external",
                    "text": (
                        "Hey Charles — hiring a Remote Senior Penetration Tester (6-month contract-to-hire) for a major "
                        "fintech client. If you're open, could you shoot over an updated resume and let me know what "
                        "hourly rate you're targeting?"
                    ),
                }
            ],
        },
    },
    {
        "id": "peer_feed_comment",
        "kind": "comment",
        "title": "Peer Engineer Feed Post on Agentic Scanners vs Business Logic Flaws",
        "post_author": " Maya Lin (Staff AppSec)",
        "post_text": (
            "Hot take after reviewing 40 'AI pentest' vendor demos this quarter: 90% of them wrap Nuclei + an LLM "
            "summarizer and still completely miss broken object-level auth across multi-step checkout flows. Until "
            "your harness models stateful session transitions, it's just a louder scanner."
        ),
    },
]

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "reply_text": {"type": "string"},
        "skills_applied": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reply_text", "skills_applied"],
    "additionalProperties": False,
}

AUDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "human_naturalness_1_to_10": {"type": "integer"},
        "grounded_accuracy_pass": {"type": "boolean"},
        "tone_fit_1_to_10": {"type": "integer"},
        "critique": {"type": "string"},
    },
    "required": ["human_naturalness_1_to_10", "grounded_accuracy_pass", "tone_fit_1_to_10", "critique"],
    "additionalProperties": False,
}


def _call_lab(model: str, system_prompt: str, user_payload: Dict[str, Any], schema: Dict[str, Any], temp: float) -> Dict[str, Any]:
    client = configured()
    if client is None:
        raise RuntimeError("Lab coordinator must be configured")
    t0 = time.time()
    with caller("social-voice-arena", "social"):
        receipt = client.request(
            "/v1/inference",
            {
                "client": client.client,
                "project": client.project,
                "operation_id": "voice-arena-" + uuid.uuid4().hex[:16],
                "path": "/api/chat",
                "payload": {
                    "model": model,
                    "stream": False,
                    **({"think": False} if "qwen3" in model else {}),
                    "format": schema,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                    ],
                    "options": {"num_predict": 450, "num_ctx": 8192, "temperature": temp},
                },
                "request_seconds": 150,
                "task_label": f"Social Voice Arena ({model})",
            },
        )
        res = client.wait(receipt["id"], seconds=300)
    latency = round(time.time() - t0, 2)
    content = ((res.get("message") or {}).get("content") or "").strip()
    return {
        "model": res.get("model", model),
        "request_id": receipt["id"],
        "latency_sec": latency,
        "eval_count": res.get("eval_count"),
        "raw_content": content,
        "parsed": json.loads(content),
    }


def run_arena() -> Dict[str, Any]:
    op_profile = load_operator_profile()
    compact_facts = [f["text"] for f in (op_profile.get("approved_messaging_facts") or [])]
    results: Dict[str, Any] = {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "candidates": CANDIDATE_DRAFTERS,
        "auditor": AUDITOR_MODEL,
        "by_model_summary": {},
        "runs": [],
    }

    # Group by model first so Ollama keeps each candidate warm in VRAM across all 4 scenarios!
    per_model_scores: Dict[str, List[float]] = {c["model"]: [] for c in CANDIDATE_DRAFTERS}
    drafts_to_audit: List[Dict[str, Any]] = []

    for cand in CANDIDATE_DRAFTERS:
        mname = cand["model"]
        print(f"=== Benchmarking {mname} ({cand['family']}) ===", flush=True)
        for sc in SCENARIOS:
            if sc["kind"] == "dm":
                routed = select_dm_skills(sc["thread"])
                sys_prompt = (
                    "You are drafting a LinkedIn DM reply as Charles McGowen using ONLY the active skill playbooks below.\n\n"
                    + routed["compiled_skill_prompt"]
                    + "\n\nReturn JSON only: {\"reply_text\": \"...\", \"skills_applied\": [...]}"
                )
                u_payload = {
                    "approved_facts": compact_facts,
                    "thread_messages": sc["thread"]["messages"],
                    "active_skills": routed["selected_skills"],
                }
                skills_used = routed["selected_skills"]
            else:
                sys_prompt = (
                    "You are writing a LinkedIn feed comment as Charles McGowen using ONLY the active skill playbook below.\n\n"
                    + read_skill_body("social-peer-comment")
                    + "\n\nReturn JSON only: {\"reply_text\": \"...\", \"skills_applied\": [\"social-peer-comment\"]}"
                )
                u_payload = {
                    "approved_facts": compact_facts[:3],
                    "post_author": sc["post_author"],
                    "post_text": sc["post_text"],
                }
                skills_used = ["social-peer-comment"]

            out = _call_lab(mname, sys_prompt, u_payload, DRAFT_SCHEMA, temp=0.45)
            reply_txt = out["parsed"]["reply_text"].strip()
            has_cliche = bool(CLICHE_RE.search(reply_txt))
            drafts_to_audit.append(
                {
                    "scenario_id": sc["id"],
                    "scenario_title": sc["title"],
                    "candidate_model": mname,
                    "candidate_family": cand["family"],
                    "skills_routed": skills_used,
                    "reply_text": reply_txt,
                    "has_corporate_cliche": has_cliche,
                    "latency_sec": out["latency_sec"],
                    "eval_count": out["eval_count"],
                    "request_id": out["request_id"],
                }
            )

    print(f"=== Auditing all {len(drafts_to_audit)} drafts with Disjoint-Family {AUDITOR_MODEL['model']} ===", flush=True)
    auditor_sys = (
        "You are an independent communication & factual-grounding auditor (IBM Granite). "
        "Score `reply_text` from 1-10 on `human_naturalness_1_to_10` (penalizing stiff AI/HR robotic phrasing, "
        "unprompted rate/call questions, or emoji spam; rewarding concise, genuine human warmth) and `tone_fit_1_to_10`, "
        "and verify `grounded_accuracy_pass` (false if it invents unapproved numbers/employers, proposes a phone call "
        "after a family emergency pause, or claims a resume was already emailed/attached). Return JSON only."
    )
    for item in drafts_to_audit:
        sc_obj = next(s for s in SCENARIOS if s["id"] == item["scenario_id"])
        audit = _call_lab(
            AUDITOR_MODEL["model"],
            auditor_sys,
            {
                "scenario": sc_obj["title"],
                "context": sc_obj.get("thread") or sc_obj.get("post_text"),
                "approved_facts": compact_facts,
                "candidate_reply": item["reply_text"],
            },
            AUDIT_SCHEMA,
            temp=0.1,
        )
        ap = audit["parsed"]
        cliche_pen = 2.0 if item["has_corporate_cliche"] else 0.0
        acc_mult = 1.0 if ap["grounded_accuracy_pass"] else 0.4
        composite = round(((ap["human_naturalness_1_to_10"] * 0.55 + ap["tone_fit_1_to_10"] * 0.45) - cliche_pen) * acc_mult, 2)
        item["granite_audit"] = ap
        item["audit_request_id"] = audit["request_id"]
        item["composite_score"] = composite
        per_model_scores[item["candidate_model"]].append(composite)
        results["runs"].append(item)

    for cand in CANDIDATE_DRAFTERS:
        m = cand["model"]
        sc_list = per_model_scores[m]
        results["by_model_summary"][m] = {
            "family": cand["family"],
            "mean_composite_score": round(sum(sc_list) / max(1, len(sc_list)), 2),
            "cliche_count": sum(1 for r in results["runs"] if r["candidate_model"] == m and r["has_corporate_cliche"]),
            "accuracy_pass_count": sum(1 for r in results["runs"] if r["candidate_model"] == m and r["granite_audit"]["grounded_accuracy_pass"]),
        }

    ARENA_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARENA_OUT_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results["by_model_summary"], indent=2))
    return results


if __name__ == "__main__":
    run_arena()
