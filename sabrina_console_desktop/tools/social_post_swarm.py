# -*- coding: utf-8 -*-
"""
tools/social_post_swarm.py
==========================
Multi-Family Swarm Authoring & Disjoint-Auditor Pipeline for LinkedIn Posts.

Replaces single-model deterministic RSI telemetry templates with genuine 3-family
collaborative authoring via the shared Lab Coordinator:
  1. Strategist (`llama3.1:8b`, Meta Llama): selects a fresh engineering angle and
     concrete architectural thesis distinct from recent post drafts/publications.
  2. Author (`qwen2.5:7b`, Qwen): writes a first-person practitioner post grounded
     strictly in Charles McGowen's approved profile without template clichés or
     raw internal loop counters.
  3. Independent Auditor (`granite3.1-dense:8b`, IBM Granite): disjoint-family critic
     that blocks/revises repetitive openings, internal RSI counter dumps
     (`Generation N`, `V(g)=`, `screening Q&A patterns`), clichés, or unapproved
     personal claims.

Saves verbatim outputs, request IDs, and model attribution per AGENTS.md Section 3.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tools.control_client import caller, configured, inference_wait_budget
from tools.operator_profile import load_operator_profile
from tools.social_skill_router import REPO_ROOT as ROOT, read_skill_body


def _default_post_profile() -> Dict[str, Any]:
    try:
        from tools.messaging_skills import load_profile
        prof = load_profile()
        if prof.get("facts"):
            return prof
    except Exception:
        pass
    op = load_operator_profile()
    return {"version": op.get("version", 1), "facts": op.get("approved_messaging_facts", [])}

POST_SWARM_MODELS: Dict[str, Dict[str, str]] = {
    "strategist": {"model": "llama3.1:8b", "family": "Meta Llama"},
    "author": {"model": "gemma3:12b", "family": "Google Gemma"},
    "auditor": {"model": "granite3.1-dense:8b", "family": "IBM Granite"},
}

FORBIDDEN_BOILERPLATE_RE = re.compile(
    r"(?:"
    r"completed\s+generation\s+\d+|"
    r"hit\s+generation\s+\d+|"
    r"reached\s+generation\s+\d+|"
    r"\bv\(g\)\s*=|"
    r"screening\s+q&a\s+patterns|"
    r"self-evolved\s+search\s+queries|"
    r"dream-rsi\s+utility|"
    r"boosted\s+her\s+job\s+pipeline|"
    r"roles\s+evaluated,\s+with\s+\d+\s+high|"
    r"in\s+the\s+ever-evolving\s+landscape|"
    r"at\s+charles\s+mcgowen\s*&\s*co"
    r")",
    re.I,
)

STRATEGIST_SCHEMA = {
    "type": "object",
    "properties": {
        "angle_title": {"type": "string"},
        "thesis": {"type": "string"},
        "concrete_mechanism": {"type": "string"},
        "opening_hook_idea": {"type": "string"},
        "approved_fact_ids": {"type": "array", "items": {"type": "string"}},
        "differentiation_note": {"type": "string"},
    },
    "required": [
        "angle_title",
        "thesis",
        "concrete_mechanism",
        "opening_hook_idea",
        "approved_fact_ids",
        "differentiation_note",
    ],
    "additionalProperties": False,
}

AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "post_text": {"type": "string"},
        "facts_used": {"type": "array", "items": {"type": "string"}},
        "core_takeaway": {"type": "string"},
    },
    "required": ["post_text", "facts_used", "core_takeaway"],
    "additionalProperties": False,
}

AUDITOR_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "reason": {"type": "string"},
        "revision_instructions": {"type": "string"},
        "facts_verified": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "reason", "revision_instructions", "facts_verified"],
    "additionalProperties": False,
}

STRATEGIST_SYSTEM = """You are the Meta Llama Angle Strategist in Sabrina's 3-Family Social Authoring Swarm.
Your job is to propose ONE fresh, genuinely interesting practitioner engineering thesis for a LinkedIn post
written in first-person singular by Charles McGowen (Senior Security Engineer & Python/Go Backend Architect).
Read `approved_profile` for true career facts and `recent_posts` for recently drafted/published posts.
STRICT RULES:
1. Choose a completely DIFFERENT angle, subject, and opening structure than everything in `recent_posts`.
2. Focus on real practitioner engineering lessons grounded in `approved_profile` (for example: designing
   recursively self-improving vulnerability harnesses without reward hacking, logic-flaw vs scanner coverage in
   web/mobile penetration testing, fail-closed browser state machines with single-use outbox leases, disjoint
   multi-family LLM auditing to prevent model monoculture, gRPC/FastAPI security boundaries, etc.).
3. NEVER propose reciting internal loop counters like 'Generation 273', 'V(g)=84.55', 'screening Q&A patterns',
   or 'job pipeline roles evaluated'. Return JSON only."""

AUTHOR_SYSTEM = """You are the Qwen Technical Author in Sabrina's 3-Family Social Authoring Swarm.
Write an authentic, practitioner-grade first-person LinkedIn post ('I', 'my') for Charles McGowen based on
the Strategist's thesis and `approved_profile`.
STRICT RULES:
1. Sound like a real senior offensive/agentic security engineer sharing a hard-won architectural insight —
   direct, specific, thoughtful, never promotional boilerplate or bragging loop metrics.
2. NEVER write internal telemetry boilerplate ('Generation N', 'V(g)=', 'screening Q&A patterns',
   'self-evolved search queries', 'roles evaluated', 'Dream-RSI').
3. NEVER use AI clichés ('In the ever-evolving landscape', 'game-changer', 'delve', 'testament to', 'thrilled to').
4. NEVER invent employers, client names, or unapproved metrics; every personal career claim must come strictly
   from `approved_profile` (list used IDs in `facts_used`).
5. Length: 420 to 880 characters across 2-3 short paragraphs, ending with 2-3 relevant technical hashtags.
Return JSON only."""

AUDITOR_SYSTEM = """You are the IBM Granite Disjoint-Family Auditor in Sabrina's 3-Family Social Authoring Swarm.
Independently audit the Qwen Author's LinkedIn post draft against `approved_profile` and `recent_posts`.
Reject (`verdict="revise"`) if ANY of the following occur:
1. The draft recites internal loop telemetry boilerplate ('Generation N', 'V(g)=', 'screening Q&A patterns',
   'self-evolved search queries', 'job pipeline', 'roles evaluated').
2. The opening sentence or core structure closely repeats any item in `recent_posts`.
3. The draft invents employers, client names, or personal metrics absent from `approved_profile`.
4. The draft uses generic AI filler clichés ('ever-evolving landscape', 'game-changer', 'delve') or is cut off mid-sentence.
Otherwise return `verdict="approve"`. Return JSON only."""


def _parse_json(raw: str) -> Dict[str, Any]:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)


def load_recent_posts(social_db_path: Optional[Path] = None, limit: int = 8) -> List[str]:
    """Load recent drafted or published post texts from social.sqlite3 to prevent repetition."""
    db_path = Path(
        social_db_path
        or Path(os.environ.get("SABRINA_SOCIAL_DATA_DIR", str(ROOT / "data/social"))) / "social.sqlite3"
    )
    if not db_path.exists():
        return []
    snippets: List[str] = []
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            rows = conn.execute(
                "SELECT payload, result FROM operations WHERE kind IN ('draft_post', 'publish_post') "
                "ORDER BY created DESC LIMIT 25"
            ).fetchall()
        for payload_raw, result_raw in rows:
            for raw in (result_raw, payload_raw):
                if not raw:
                    continue
                try:
                    data = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                txt = str(data.get("post_content") or data.get("content") or "").strip()
                if txt and txt not in snippets:
                    snippets.append(txt[:360])
                    if len(snippets) >= limit:
                        return snippets
    except Exception:
        pass
    return snippets


def recent_post_cooldown_active(
    social_db_path: Optional[Path] = None,
    cooldown_seconds: int = 43200,
) -> Optional[Dict[str, Any]]:
    """Return info if an autonomous draft_post or publish_post already succeeded within cooldown_seconds."""
    db_path = Path(
        social_db_path
        or Path(os.environ.get("SABRINA_SOCIAL_DATA_DIR", str(ROOT / "data/social"))) / "social.sqlite3"
    )
    if not db_path.exists():
        return None
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - max(0, int(cooldown_seconds))
    try:
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            row = conn.execute(
                "SELECT id, kind, status, created FROM operations "
                "WHERE kind IN ('draft_post', 'publish_post') "
                "AND status IN ('completed', 'published', 'drafted_for_review') "
                "AND created >= ? ORDER BY created DESC LIMIT 1",
                (cutoff,),
            ).fetchone()
        if row:
            return {"operation_id": row[0], "kind": row[1], "status": row[2], "created": row[3]}
    except Exception:
        pass
    return None


def validate_post_candidate(post_text: str, recent_posts: List[str]) -> Optional[str]:
    """Deterministic post-check against RSI boilerplate, truncation, and near-duplicate openings."""
    text = (post_text or "").strip()
    if len(text) < 220:
        return "Draft is too short (<220 chars); write 2-3 substantive technical paragraphs."
    if len(text) > 1600:
        return "Draft exceeds 1600 chars; tighten to 420-880 chars."
    m = FORBIDDEN_BOILERPLATE_RE.search(text)
    if m:
        return f"Draft contains forbidden deterministic boilerplate phrase: {m.group(0)!r}"
    # Check that post does not end mid-word/mid-clause (allowing punctuation or hashtag end)
    last_line = text.splitlines()[-1].strip()
    if not re.search(r"[.!?\"')]$|^#\w+", last_line) and "#" not in last_line:
        return "Draft appears truncated at the end; finish with complete sentences and hashtags."
    norm_new = " ".join(text.lower().split())
    new_opening = norm_new[:110]
    for prev in recent_posts:
        norm_prev = " ".join(prev.lower().split())
        if not norm_prev:
            continue
        if difflib.SequenceMatcher(None, new_opening, norm_prev[:110]).ratio() > 0.72:
            return "Opening hook is too similar to a recent post draft; open from a fresh angle."
        if difflib.SequenceMatcher(None, norm_new[:340], norm_prev[:340]).ratio() > 0.70:
            return "Post body closely repeats a recent draft; write on a distinct technical mechanism."
    return None


class SocialPostSwarm:
    """Three-family local swarm (`Meta Llama` + `Qwen` + `IBM Granite`) for non-boilerplate posts."""

    def __init__(
        self,
        models: Optional[Dict[str, Dict[str, str]]] = None,
        model_call: Optional[Callable[..., Dict[str, Any]]] = None,
        social_db_path: Optional[Path] = None,
        provenance_path: Optional[Path] = None,
    ):
        self.models = models or POST_SWARM_MODELS
        if set(self.models) != {"strategist", "author", "auditor"} or len({m["family"] for m in self.models.values()}) != 3:
            raise ValueError("SocialPostSwarm requires 3 distinct model families (strategist, author, auditor)")
        self.social_db_path = social_db_path
        default_prov = (
            Path(os.environ.get("SABRINA_SOCIAL_DATA_DIR", str(ROOT / "data/social")))
            / "post_swarm_provenance.jsonl"
        )
        self.provenance_path = Path(provenance_path or default_prov)
        self.call = model_call or self._coordinator_call

    def _coordinator_call(
        self,
        run_id: str,
        role: str,
        spec: Dict[str, str],
        attempt: int,
        system_prompt: str,
        user_payload: Dict[str, Any],
        schema: Dict[str, Any],
        temperature: float,
    ) -> Dict[str, Any]:
        with caller("social-post-swarm", "social"):
            client = configured()
            if client is None:
                raise RuntimeError("Shared lab coordinator must be configured for SocialPostSwarm")
            op_id = f"{run_id}:{role}:{attempt}"
            payload = {
                "model": spec["model"],
                "stream": False,
                **({"think": False} if "qwen3" in spec["model"] else {}),
                "format": schema,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "options": {
                    "num_predict": 900,
                    "num_ctx": 8192,
                    "temperature": temperature,
                },
            }
            receipt = client.request(
                "/v1/inference",
                {
                    "client": client.client,
                    "project": client.project,
                    "operation_id": op_id,
                    "path": "/api/chat",
                    "payload": payload,
                    "request_seconds": 150,
                    "task_label": f"Social post swarm {role} ({spec['family']})",
                },
            )
            result = client.wait(receipt["id"], seconds=360)
            return {
                "content": (result.get("message") or {}).get("content", ""),
                "thinking": (result.get("message") or {}).get("thinking", ""),
                "model": result.get("model", spec["model"]),
                "request_id": receipt["id"],
                "eval_count": result.get("eval_count"),
            }

    def compose_post(
        self,
        topic: str,
        profile: Optional[Dict[str, Any]] = None,
        recent_posts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        profile_data = profile if profile is not None else _default_post_profile()
        approved_facts = [
            {"id": f["id"], "text": f["text"]}
            for f in profile_data.get("facts", [])
            if f.get("approved") is True
        ]
        approved_ids = {f["id"] for f in approved_facts}
        prior_posts = recent_posts if recent_posts is not None else load_recent_posts(self.social_db_path)
        # Filter any old RSI-boilerplate snippets out of positive examples while listing them as forbidden repeats
        recent_avoid = [p[:180] for p in prior_posts[:6]]
        run_id = "post-swarm-" + uuid.uuid4().hex[:16]
        artifacts: List[Dict[str, Any]] = []

        with inference_wait_budget(480):
            # Stage 1: Meta Llama Strategist
            strat_input = {
                "topic_focus": topic,
                "approved_profile_facts": approved_facts,
                "recent_posts_to_differentiate_from": recent_avoid,
            }
            strat_raw = self.call(
                run_id,
                "strategist",
                self.models["strategist"],
                1,
                STRATEGIST_SYSTEM,
                strat_input,
                STRATEGIST_SCHEMA,
                0.75,
            )
            if strat_raw.get("model", self.models["strategist"]["model"]) != self.models["strategist"]["model"]:
                raise ValueError("Strategist response came from unexpected model")
            strategist_plan = _parse_json(strat_raw["content"])
            artifacts.append(
                {
                    "role": "strategist",
                    "attempt": 1,
                    "model": self.models["strategist"]["model"],
                    "family": self.models["strategist"]["family"],
                    "request_id": strat_raw.get("request_id"),
                    "raw": strat_raw["content"],
                    "parsed": strategist_plan,
                }
            )

            feedback: Optional[str] = None
            previous_draft: Optional[str] = None
            approved_text: Optional[str] = None
            final_author: Optional[Dict[str, Any]] = None
            final_audit: Optional[Dict[str, Any]] = None

            # Stage 2 + Stage 3: Qwen Author + IBM Granite Disjoint Auditor (up to 2 CEGAR rounds)
            for attempt in (1, 2):
                author_input: Dict[str, Any] = {
                    "topic_focus": topic,
                    "approved_profile_facts": approved_facts,
                    "strategist_thesis": strategist_plan,
                    "recent_posts_do_not_repeat": recent_avoid,
                }
                if feedback:
                    author_input["auditor_revision_required"] = feedback
                    author_input["previous_rejected_draft"] = previous_draft
                practitioner_skill = read_skill_body("social-practitioner-post")
                author_sys = (
                    AUTHOR_SYSTEM + ("\n\n=== ACTIVE SKILL: social-practitioner-post ===\n" + practitioner_skill if practitioner_skill else "")
                )
                auth_raw = self.call(
                    run_id,
                    "author",
                    self.models["author"],
                    attempt,
                    author_sys,
                    author_input,
                    AUTHOR_SCHEMA,
                    0.65 if attempt == 1 else 0.5,
                )
                if auth_raw.get("model", self.models["author"]["model"]) != self.models["author"]["model"]:
                    raise ValueError("Author response came from unexpected model")
                author_parsed = _parse_json(auth_raw["content"])
                draft_text = str(author_parsed.get("post_text") or "").strip()
                used_ids = [fid for fid in (author_parsed.get("facts_used") or []) if fid in approved_ids]
                author_parsed["facts_used"] = used_ids
                artifacts.append(
                    {
                        "role": "author",
                        "attempt": attempt,
                        "model": self.models["author"]["model"],
                        "family": self.models["author"]["family"],
                        "request_id": auth_raw.get("request_id"),
                        "raw": auth_raw["content"],
                        "parsed": author_parsed,
                    }
                )

                det_err = validate_post_candidate(draft_text, prior_posts)
                if det_err:
                    feedback = det_err
                    previous_draft = draft_text
                    if attempt == 2:
                        raise ValueError(f"Post candidate rejected by anti-boilerplate guard: {det_err}")
                    continue

                audit_input = {
                    "topic_focus": topic,
                    "approved_profile_facts": approved_facts,
                    "strategist_thesis": strategist_plan,
                    "candidate_post": draft_text,
                    "recent_posts_do_not_repeat": recent_avoid,
                }
                aud_raw = self.call(
                    run_id,
                    "auditor",
                    self.models["auditor"],
                    attempt,
                    AUDITOR_SYSTEM,
                    audit_input,
                    AUDITOR_SCHEMA,
                    0.1,
                )
                if aud_raw.get("model", self.models["auditor"]["model"]) != self.models["auditor"]["model"]:
                    raise ValueError("Auditor response came from unexpected model")
                audit_parsed = _parse_json(aud_raw["content"])
                artifacts.append(
                    {
                        "role": "auditor",
                        "attempt": attempt,
                        "model": self.models["auditor"]["model"],
                        "family": self.models["auditor"]["family"],
                        "request_id": aud_raw.get("request_id"),
                        "raw": aud_raw["content"],
                        "parsed": audit_parsed,
                    }
                )

                if audit_parsed.get("verdict") == "approve":
                    approved_text = draft_text
                    final_author = author_parsed
                    final_audit = audit_parsed
                    break

                feedback = (
                    f"{audit_parsed.get('reason', '')} — {audit_parsed.get('revision_instructions', '')}".strip(" —")
                )
                previous_draft = draft_text
                if attempt == 2:
                    raise ValueError(f"IBM Granite auditor held post draft: {feedback}")

        if not approved_text:
            raise RuntimeError("SocialPostSwarm did not produce an auditor-approved post")

        record = {
            "run_id": run_id,
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "topic": topic,
            "models": self.models,
            "post_text": approved_text,
            "facts_used": (final_author or {}).get("facts_used", []),
            "auditor_reason": (final_audit or {}).get("reason", ""),
            "artifacts": artifacts,
        }
        try:
            self.provenance_path.parent.mkdir(parents=True, exist_ok=True)
            with self.provenance_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return record
