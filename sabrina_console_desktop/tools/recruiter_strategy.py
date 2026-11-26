"""Deterministic relationship-state and conversation-objective policy."""
from __future__ import annotations

import datetime as dt
import re


STRATEGY_VERSION = "recruiter-strategy-v1"
STATES = {"new", "qualifying", "awaiting_user_information", "awaiting_recruiter",
          "clarification_sent", "needs_human_review", "closed"}
ROLE_QUESTIONS = {"job_description", "hiring_company", "employment_type", "location",
                  "compensation_range", "role"}
USER_FACT_QUESTIONS = {"resume", "availability", "preferred_hourly_rate", "location_preference",
                       "interest"}
QUESTION_LABELS = {
    "job_description": "the job description and key responsibilities",
    "hiring_company": "the hiring company or end client",
    "employment_type": "whether the role is full-time or contract",
    "location": "the work location and remote or onsite expectations",
    "compensation_range": "the compensation range",
    "role": "the role title and responsibilities",
}


def _external(thread):
    return [m for m in thread.get("messages", []) if m.get("ownership") == "external"]


def _has_opt_out(thread):
    return bool(re.search(r"\b(?:do not|don't|stop|remove me).{0,25}(?:contact|messag|reach|mail|list)",
                          "\n".join(m.get("text", "") for m in _external(thread)), re.I))


def _has_attachment(thread):
    return any(m.get("attachments") or m.get("media") or m.get("links_unreadable") for m in thread.get("messages", []))


def _prior_autonomous(prior_delivery):
    return any(item.get("autonomous") or item.get("kind") == "autonomous_clarification" for item in (prior_delivery or []))


def _daily_count(graph_snapshot, prior_delivery):
    return sum(1 for item in (prior_delivery or []) if item.get("status") in {"prepared", "sent"}
               and item.get("created", "")[:10] == dt.datetime.now(dt.timezone.utc).date().isoformat())


def clarification_text(question):
    label = QUESTION_LABELS.get(question)
    if not label:
        raise ValueError("Question is not an approved role clarification")
    return "Thanks for reaching out. Could you share " + label + "?"


def plan(graph_snapshot, thread, prior_delivery=None, daily_autonomous_count=0):
    """Return a review decision that contains no send authority."""
    prior_delivery = prior_delivery or []
    state = graph_snapshot.get("conversation_state") or {}
    latest = (thread.get("messages") or [{}])[-1]
    latest_external = latest.get("ownership") == "external"
    recruiter = graph_snapshot.get("person", {}).get("platform_id") and graph_snapshot.get("contact_classification") == "recruiter"
    complete = bool(thread.get("complete"))
    requests = {r.get("question"): r for r in graph_snapshot.get("requests", [])}
    role_facts = {f.get("field") for f in graph_snapshot.get("role_facts", [])}
    open_questions = [q for q in graph_snapshot.get("open_questions", []) if q not in role_facts]
    user_questions = [q for q in open_questions if q in USER_FACT_QUESTIONS]
    role_questions = [q for q in open_questions if q in ROLE_QUESTIONS]
    reasons = []

    if not complete:
        relationship_state = "needs_human_review"
        response_owner = "user"
        objective = "capture_complete_conversation"
        action = "ask_human"
        reasons.append("Conversation capture is incomplete")
    elif not latest_external:
        relationship_state = "awaiting_recruiter"
        response_owner = "contact"
        objective = "wait_for_recruiter_response"
        action = "no_reply"
        reasons.append("The latest message is self-authored or there is no external message")
    elif _has_opt_out(thread):
        relationship_state = "closed"
        response_owner = "none"
        objective = "honor_opt_out"
        action = "no_reply"
        reasons.append("The contact opted out or the message is a stop request")
    elif _has_attachment(thread):
        relationship_state = "needs_human_review"
        response_owner = "user"
        objective = "inspect_attachment_or_link"
        action = "ask_human"
        reasons.append("Attachment, media, or unreadable link requires human inspection")
    elif user_questions:
        relationship_state = "awaiting_user_information"
        response_owner = "user"
        objective = "resolve_user_specific_information"
        action = "ask_human"
        reasons.append("The recruiter needs a user-specific fact or decision: " + ", ".join(user_questions))
    elif role_questions:
        relationship_state = "qualifying"
        response_owner = "user"
        objective = "obtain_" + role_questions[0]
        action = "draft_reply"
        reasons.append("A source-backed role detail is still missing")
    else:
        relationship_state = "awaiting_recruiter"
        response_owner = "contact"
        objective = "wait_for_recruiter_response"
        action = "no_reply"
        reasons.append("No unanswered low-risk objective remains")

    no_duplicate = _prior_autonomous(prior_delivery)
    clear_autonomy = (action == "draft_reply" and complete and latest_external and recruiter
                      and len(role_questions) == 1 and not user_questions and not _has_opt_out(thread)
                      and not _has_attachment(thread) and not no_duplicate
                      and max(int(daily_autonomous_count or 0), _daily_count(graph_snapshot, prior_delivery)) < 3)
    eligible = bool(clear_autonomy)
    if action == "draft_reply" and not eligible:
        reasons.append("Human review is required before any response")
    text = clarification_text(role_questions[0]) if eligible else ""
    return {
        "strategy_version": STRATEGY_VERSION,
        "graph_version": graph_snapshot.get("graph_version", ""),
        "relationship_state": relationship_state,
        "conversation_goal": objective,
        "open_questions": open_questions,
        "response_owner": response_owner,
        "next_action": objective,
        "action": action if eligible else ("ask_human" if action == "draft_reply" else action),
        "auto_reply_eligible": eligible,
        "autonomous_reason": "; ".join(reasons),
        "proposed_text": text,
        "role_questions": role_questions,
        "user_questions": user_questions,
        "turn_count": len(thread.get("messages", [])),
        "daily_autonomous_count": int(daily_autonomous_count or 0),
    }

