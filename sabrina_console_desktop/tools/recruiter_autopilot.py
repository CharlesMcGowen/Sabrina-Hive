"""Bounded, idempotent autonomous clarification guard.

This module intentionally defaults to shadow mode.  A caller must inject a
sender and explicitly enable it; no browser or model call is hidden here.
"""
from __future__ import annotations

import os

from tools.messaging_store import ROOT
from tools.recruiter_strategy import clarification_text


class RecruiterAutopilot:
    def __init__(self, graph, *, enabled=None, daily_limit=3, respect_global_pause=True):
        self.graph = graph
        self.enabled = (os.environ.get("SABRINA_RECRUITER_AUTONOMY", "0") == "1") if enabled is None else bool(enabled)
        self.daily_limit = int(daily_limit)
        self.respect_global_pause = bool(respect_global_pause)

    def evaluate(self, decision, thread, graph_snapshot, prior_delivery=None):
        reasons = []
        if not decision.get("auto_reply_eligible"):
            reasons.append("strategy did not mark this clarification eligible")
        if not thread.get("complete"):
            reasons.append("conversation capture is incomplete")
        if not self.graph.autonomy_allowed(thread):
            reasons.append("autonomy is disabled for this thread")
        if self.graph.autonomous_count_today(thread["account_id"]) >= self.daily_limit:
            reasons.append("account daily autonomous-reply limit has been reached")
        if self.respect_global_pause and (ROOT / "data/recruiter-outreach.pause").exists():
            reasons.append("global recruiter pause switch is active")
        if not thread.get("messages") or thread["messages"][-1].get("ownership") != "external":
            reasons.append("latest message is not external")
        if not graph_snapshot.get("person", {}).get("platform_id"):
            reasons.append("stable contact identity is missing")
        if decision.get("user_questions"):
            reasons.append("user-specific information is required")
        if len(decision.get("role_questions", [])) != 1:
            reasons.append("exactly one missing role fact is required")
        if decision.get("action") != "draft_reply":
            reasons.append("decision is not a clarification draft")
        if any(item.get("status") in {"uncertain", "sending"} for item in (prior_delivery or [])):
            reasons.append("a prior delivery is uncertain")
        if any(item.get("autonomous") or item.get("kind") == "autonomous_clarification" for item in (prior_delivery or [])):
            reasons.append("this thread already received an autonomous clarification")
        text = decision.get("proposed_text", "")
        try:
            expected = clarification_text(decision.get("role_questions", [None])[0])
        except (IndexError, TypeError, ValueError):
            expected = ""
        if not expected or text != expected:
            reasons.append("response is not the exact validated clarification template")
        # The guard rejects personal claims even if a model put them into the
        # decision packet after the strategist ran.
        lowered = text.lower()
        if any(token in lowered for token in ("resume", "salary", "rate", "available", "interested", "i can", "i am", "my experience")):
            reasons.append("response contains user-specific or commitment language")
        return {"eligible": not reasons, "reasons": reasons, "text": text if not reasons else ""}

    def execute(self, decision, thread, graph_snapshot, *, prior_delivery=None, sender=None, observe=None):
        """Prepare or send one validated clarification.

        In the default rollout this records a shadow decision and returns the
        exact text without touching the browser.  When explicitly enabled, the
        injected sender must return a receipt containing a platform message ID;
        uncertain results are persisted and are never retried automatically.
        """
        check = self.evaluate(decision, thread, graph_snapshot, prior_delivery)
        if not check["eligible"]:
            return {"status": "needs_input", "eligible": False, "reasons": check["reasons"], "text": ""}
        text = check["text"]
        if not self.enabled:
            aid = self.graph.record_autonomy(thread, decision, text, status="shadow")
            return {"status": "shadow", "eligible": True, "action_id": aid, "text": text,
                    "reason": "Autonomy feature flag is disabled; no message was sent"}
        if sender is None:
            return {"status": "needs_input", "eligible": False, "reasons": ["enabled autonomy requires an explicit send adapter"], "text": text}
        aid = self.graph.record_autonomy(thread, decision, text, status="prepared")
        try:
            receipt = sender(text, thread)
        except Exception as exc:
            receipt = {"status": "uncertain", "reason": str(exc), "retry": False}
        if receipt.get("status") == "sent" and not receipt.get("message_id"):
            receipt = {"status": "uncertain", "reason": "Sent receipt did not contain a platform message ID", "retry": False}
        status = "sent" if receipt.get("status") == "sent" else "uncertain"
        self.graph.record_autonomy(thread, decision, text, status=status, receipt=receipt)
        if status == "sent" and observe:
            observe()
        return {"status": status, "eligible": True, "action_id": aid, "text": text, "receipt": receipt}

