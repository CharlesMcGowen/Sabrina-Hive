"""Autonomous, multi-family-verified LinkedIn DM responder.

Continuously monitors LinkedIn Messaging (~60s cadence via dedicated /messaging/ tab)
for active incoming DMs where the external contact spoke last, evaluates each
conversation with Sabrina's 3-family Swarm (Qwen reader, Gemma drafter,
IBM Granite independent critic) grounded strictly in the approved career profile,
re-verifies the live DOM thread, claims a single-use MessageOutbox entry, and
delivers the reply via LinkedInMessageSender with receipt verification.
Never sends cold boilerplate outreach and never resurrects archived/ancient threads.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

if sys.stdout is None or sys.stderr is None:
    _log_path = Path(__file__).resolve().parents[1] / "data/recruiter-worker.log"
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _bg_log = _log_path.open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = _bg_log
else:
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

from sabrina_social.availability_support import backup_database, instance_lock
from tools.control_client import caller, configured, resource_lease
from tools.dm_external_portal_bridge import (
    DmExternalPortalStore,
    attempt_external_portal_application,
    extract_external_job_urls,
    process_thread_external_portals,
)
from tools.linkedin_message_sender import LinkedInMessageSender, find_delivery
from tools.linkedin_messaging_reader import LinkedInMessagingReader
from tools.messaging_loop import browser_lease
from tools.messaging_outbox import MessageOutbox
from tools.messaging_skills import load_profile
from tools.messaging_store import ROOT, digest, now
from tools.recruiter_campaign import STATE, STOP, export
from tools.recruiter_conversation_swarm import BOARD_PATH, MODELS, POLICY_VERSION, RecruiterConversationSwarm
from tools.recruiter_graph import RecruiterGraph
from tools.recruiter_strategy import plan as plan_strategy
from tools.recruiter_tracker import RecruiterTracker, classify
from tools.worker_identity import DependencyUnavailable, operation_identity

AUTHORIZATION_NOTE = (
    "Autonomous incoming-DM reply enabled by operator (2026-11-16); "
    "verified by Qwen+Gemma+Granite swarm against approved_profile v2"
)

RICH_LIST_SCRIPT = r"""() => Array.from(document.querySelectorAll('.msg-conversation-listitem')).map((e, i) => {
  e.setAttribute('data-sabrina-thread', String(i));
  const name = (e.querySelector('.msg-conversation-listitem__participant-names,.msg-conversation-card__participant-names')?.innerText || '').trim();
  const preview = (e.querySelector('.msg-conversation-card__message-snippet')?.innerText || '').trim();
  const timeText = (e.querySelector('.msg-conversation-card__time-stamp, time')?.innerText || '').trim();
  const unread = !!e.querySelector('.msg-conversation-card__unread-count, .notification-badge--show, [class*="unread"]');
  return {index: i, name, preview, time_text: timeText, unread};
}).filter(x => x.name)"""

_STALE_DATE_RE = re.compile(
    r"\b(?:201\d|202[0-4])\b",
    re.I,
)

_NON_RECRUITER_DISQUALIFIER_RE = re.compile(
    r"(?:"
    r"\bas\s+a\s+ward\b|"
    r"\b(?:agent|rollout)\s+is\s+picking\s+up\s+inside\b|"
    r"\bcertification\s+or\s+training\b|"
    r"\bearly\s+release\s+tickets\b|"
    r"\bcompliance\s+deadline\s+is\b|"
    r"\bcfp\s+ended\b|"
    r"\bspeaking\s+opportunities\b|"
    r"\battack\s+simulations\b"
    r")",
    re.I,
)

_HIRING_SIGNAL_RE = re.compile(
    r"\b(?:"
    r"recruiter|recruiting|talent\s+acquisition|staffing|hiring|hire|"
    r"role|position|opportunity|openings?|job\s+description|submittal|candidate|resume|cv|"
    r"contract|full[- ]time|fte|w-?2|c2c|remote\s+role|"
    r"penetration\s+tester|pen\s*testing|pentest(?:er|ing)?|red\s+team|"
    r"appsec|application\s+security|security\s+engineer|offensive\s+security|"
    r"growing\s+our\s+[\w\s()/-]{1,35}team"
    r")\b",
    re.I,
)

_POLITE_SIGNOFF_RE = re.compile(
    r"^\s*(?:thanks|thank\s+you|okay|ok|sounds\s+good|got\s+it|yes|no\s+problem|no\s+prob|take\s+care|hope\s+you(?:'re|\s+are)\s+doing\s+well)\b[^?]*$",
    re.I,
)


def _has_verified_hiring_signal(snapshot: dict) -> tuple[bool, str]:
    """Deterministic guard ensuring autonomous replies ONLY fire on genuine hiring/recruiter threads,
    never on personal/ward/friend chats, B2B vendor pitches, ticket/training promos, or polite sign-offs.
    """
    msgs = snapshot.get("messages") or []
    ext_msgs = [m for m in msgs if m.get("ownership") == "external"]
    if not ext_msgs:
        return False, "No external messages in thread"
    combined_ext = "\n".join((m.get("text") or "") for m in ext_msgs)
    m_disq = _NON_RECRUITER_DISQUALIFIER_RE.search(combined_ext)
    if m_disq:
        return False, f"Excluded non-recruiter/personal/vendor thread ({m_disq.group(0)})"
    latest_ext = " ".join((ext_msgs[-1].get("text") or "").split()).strip()
    if len(latest_ext) <= 95 and "?" not in latest_ext and _POLITE_SIGNOFF_RE.match(latest_ext):
        return False, "Latest external message is a polite sign-off/acknowledgement without a question"
    if not _HIRING_SIGNAL_RE.search(combined_ext):
        return False, "No recruiting or job-opportunity signal in external messages"
    return True, "verified_hiring_signal"


def _init_inbox_table(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS dm_inbox_slots(
          slot_key TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          fingerprint TEXT NOT NULL,
          thread TEXT,
          updated TEXT NOT NULL
        )"""
    )
    db.commit()


def _ensure_dedicated_messaging_tab(cdp_url: str) -> None:
    """Ensure Chrome has a dedicated /messaging/ tab so feed pulses do not navigate it away."""
    try:
        base = cdp_url.rstrip("/")
        with urllib.request.urlopen(base + "/json/list", timeout=3) as r:
            tabs = json.load(r)
        has_msg = any("linkedin.com/messaging" in str(t.get("url", "")) for t in tabs)
        has_li = any("linkedin.com" in str(t.get("url", "")) for t in tabs)
        if has_li and not has_msg:
            req = urllib.request.Request(base + "/json/new?https://www.linkedin.com/messaging/", method="PUT")
            with urllib.request.urlopen(req, timeout=5):
                pass
    except Exception:
        pass


def _is_active_recent_thread(snapshot: dict, live_focused_threads: set[str]) -> bool:
    """Exclude archived conversations and multi-year-old (<=2024) historical threads while catching up on all Focused DMs."""
    url = snapshot.get("url") or ""
    if "filter=archived" in url.lower():
        return False
    msgs = snapshot.get("messages") or []
    if not msgs:
        return False
    recent_dates = " ".join((m.get("date_text") or "") for m in msgs[-3:])
    if _STALE_DATE_RE.search(recent_dates):
        return False
    return True


def _prior_deliveries(tracker: RecruiterTracker, graph: RecruiterGraph, account_id: str, thread_id: str) -> list[dict]:
    items = [
        dict(r)
        for r in tracker.db.execute(
            "SELECT id,status,text FROM recruiter_actions WHERE account=? AND thread=? AND status IN ('sending','uncertain')",
            (account_id, thread_id),
        )
    ]
    if tracker.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_outbox'").fetchone():
        items += [
            dict(r)
            for r in tracker.db.execute(
                "SELECT id,status,text FROM message_outbox WHERE account=? AND thread=? AND status IN ('sending','uncertain')",
                (account_id, thread_id),
            )
        ]
    items += [
        {**dict(r), "autonomous": True, "kind": "autonomous_dm_reply"}
        for r in graph.db.execute(
            "SELECT id,status,text,created FROM recruiter_graph_autonomy WHERE account=? AND thread=? AND status IN ('sending','uncertain')",
            (account_id, thread_id),
        )
    ]
    return items


def _needs_swarm_review(tracker: RecruiterTracker, swarm: RecruiterConversationSwarm, snapshot: dict, profile: dict) -> tuple[bool, dict | None]:
    """Return (should_process, existing_completed_run_if_draft_ready)."""
    if not snapshot.get("complete"):
        return False, None
    if "filter=archived" in (snapshot.get("url") or "").lower():
        return False, None
    messages = snapshot.get("messages") or []
    if not messages or messages[-1].get("ownership") != "external":
        return False, None
    if classify(snapshot)[0] == "do_not_contact":
        return False, None
    ok_hiring, _ = _has_verified_hiring_signal(snapshot)
    if not ok_hiring:
        return False, None
    account_id, thread_id, version = snapshot["account_id"], snapshot["thread_id"], snapshot["version"]
    sent_row = tracker.db.execute(
        "SELECT 1 FROM decisions WHERE account=? AND thread=? AND context_version=? AND status='sent'",
        (account_id, thread_id, version),
    ).fetchone()
    if sent_row:
        return False, None
    has_table = swarm.board.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_review_runs'"
    ).fetchone()
    if not has_table:
        return True, None
    prior = swarm.board.conn.execute(
        "SELECT id, context_version, profile_hash, status, packet, decision, error, updated FROM conversation_review_runs "
        "WHERE account=? AND thread=? ORDER BY created DESC LIMIT 1",
        (account_id, thread_id),
    ).fetchone()
    if not prior:
        return True, None
    same_context = (
        prior["context_version"] == version
        and prior["profile_hash"] == digest(profile)
        and json.loads(prior["packet"]).get("policy_version") == POLICY_VERSION
    )
    if not same_context:
        return True, None
    if prior["status"] == "draft_ready":
        return True, swarm.board.conversation_review(prior["id"])
    if prior['status']=='blocked' and any(marker in (prior['error'] or '') for marker in (
        'Lab wait expired; retained request ', 'Inference wait budget exhausted before submission',
    )):
        # The original inference remains owned by the coordinator. Re-entering
        # this review reattaches to its stable operation ID, without another send.
        try:
            age=time.time()-dt.datetime.fromisoformat(prior['updated']).timestamp()
        except (TypeError,ValueError):
            return False,None
        return age>=300,None
    if prior["status"] in {"no_reply", "needs_input", "blocked"}:
        return False, None
    return True, None


def _scan_inbox(
    tracker: RecruiterTracker,
    swarm: RecruiterConversationSwarm,
    profile: dict,
    state: dict,
    cdp_url: str,
    max_scan: int = 12,
    max_reads: int = 6,
    test_override: bool = False,
) -> dict:
    _ensure_dedicated_messaging_tab(cdp_url)
    reader = LinkedInMessagingReader(cdp_url, test_override=test_override)
    read_count = 0
    scanned = 0
    live_threads: list[str] = []
    with (
        caller("autonomous-dm-responder", "social"),
        operation_identity("dm-inbox-scan:" + str(int(time.time()))),
        resource_lease("browser:linkedin-primary", wait_seconds=210),
        browser_lease(),
    ):
        try:
            reader.connect()
            navigated = False
            pane_already_open = (
                "/messaging/" in reader.page.url
                and "filter=" not in reader.page.url.lower()
                and reader.page.locator(".msg-conversations-container__conversations-list").count() == 1
            )
            if not pane_already_open:
                allowed, quota_reason = reader.manager.rate_limiter.check_quota("inbox_check")
                if not allowed:
                    return {"status": "quota_deferred", "reason": quota_reason, "scanned": 0, "read": 0, "live_threads": []}
                reader.page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded", timeout=30000)
                navigated = True
            reader.page.locator(".msg-conversation-listitem").first.wait_for(state="visible", timeout=15000)
            reader._guard()
            pane = reader.page.locator(".msg-conversations-container__conversations-list")
            if pane.count() == 1:
                pane.evaluate("e => e.scrollTop = 0")
                reader.page.wait_for_timeout(400)
            top_items = (reader.page.evaluate(RICH_LIST_SCRIPT) or [])[: max(1, min(int(max_scan), 25))]
            items = list(top_items)
            # Also progressively scroll Focused inbox so older unanswered conversations are discovered & caught up
            if pane.count() == 1:
                catchup_y = int(state.get("focused_catchup_cursor", 0) or 0)
                if catchup_y > 0:
                    metrics = pane.evaluate("e => ({height: e.scrollHeight, view: e.clientHeight})")
                    if catchup_y >= max(0, metrics["height"] - metrics["view"]):
                        catchup_y = 0
                    else:
                        pane.evaluate("(e, y) => e.scrollTop = y", catchup_y)
                        reader.page.wait_for_timeout(500)
                        seen_names = {(x["name"], x.get("preview", "")[:45]) for x in items}
                        for extra in reader.page.evaluate(RICH_LIST_SCRIPT) or []:
                            k = (extra["name"], extra.get("preview", "")[:45])
                            if k not in seen_names:
                                items.append({**extra, "_scroll_y": catchup_y})
                                seen_names.add(k)
                        pane.evaluate("e => e.scrollTop = 0")
                        reader.page.wait_for_timeout(300)
                        reader.page.evaluate(RICH_LIST_SCRIPT)
                state["focused_catchup_cursor"] = catchup_y + 450
            scanned = len(items)
            if navigated:
                reader.manager.rate_limiter.record_action(
                    "inbox_check", {"workflow": "autonomous_dm_responder", "scanned": scanned}
                )
            for item in items:
                if STOP.exists():
                    break
                name = item["name"]
                preview = item.get("preview", "")
                time_text = item.get("time_text", "")
                unread = bool(item.get("unread"))
                slot_key = f"{name}::{preview[:45]}"
                fingerprint = f"{time_text}|{int(unread)}|{preview}"
                saved_row = tracker.db.execute(
                    "SELECT fingerprint, thread FROM dm_inbox_slots WHERE slot_key=?", (slot_key,)
                ).fetchone()
                if saved_row and saved_row["thread"]:
                    live_threads.append(saved_row["thread"])
                should_read = (
                    unread
                    or saved_row is None
                    or saved_row["fingerprint"] != fingerprint
                    or not saved_row["thread"]
                )
                if not should_read and saved_row["thread"]:
                    t_row = tracker.db.execute(
                        "SELECT account, thread FROM threads WHERE thread=? LIMIT 1", (saved_row["thread"],)
                    ).fetchone()
                    if t_row:
                        snap = tracker.store.snapshot(t_row["account"], t_row["thread"])
                        needs, _ = _needs_swarm_review(tracker, swarm, snap, profile)
                        should_read = needs
                if not should_read or read_count >= max_reads:
                    continue
                state.update(state="reading_dm", detail=f"Reading active DM thread with {name}")
                export(tracker, state)
                try:
                    if pane.count() == 1 and item.get("_scroll_y"):
                        pane.evaluate("(e, y) => e.scrollTop = y", int(item["_scroll_y"]))
                        reader.page.wait_for_timeout(350)
                        reader.page.evaluate(RICH_LIST_SCRIPT)
                    expected = None
                    if saved_row and saved_row["thread"]:
                        known = tracker.db.execute(
                            "SELECT account, thread FROM threads WHERE thread=? LIMIT 1", (saved_row["thread"],)
                        ).fetchone()
                        if known:
                            expected = tracker.store.snapshot(known["account"], known["thread"])
                    fresh = tracker.observe(reader.read_thread(item, expected=expected))
                    live_threads.append(fresh["thread_id"])
                    with tracker.db:
                        tracker.db.execute(
                            "INSERT OR REPLACE INTO dm_inbox_slots(slot_key, name, fingerprint, thread, updated) VALUES(?,?,?,?,?)",
                            (slot_key, name, f"{time_text}|0|{preview}", fresh["thread_id"], now()),
                        )
                    read_count += 1
                    if pane.count() == 1:
                        pane.evaluate("e => e.scrollTop = 0")
                        reader.page.wait_for_timeout(350)
                        reader.page.evaluate(RICH_LIST_SCRIPT)
                except Exception as exc:
                    print(f"[DM Scan] {name}: {exc}", flush=True)

            # Reconcile any prior uncertain outbox attempts if platform evidence now shows delivery
            outbox = MessageOutbox(tracker.store)
            for u_row in tracker.db.execute(
                "SELECT id, account, thread, text FROM message_outbox WHERE status='uncertain' ORDER BY id DESC LIMIT 2"
            ).fetchall():
                try:
                    saved_u = tracker.store.snapshot(u_row["account"], u_row["thread"])
                    fresh_u = reader.read_saved_thread(saved_u)
                    raw_u = {
                        "url": fresh_u["url"],
                        "peer_name": fresh_u["peer_name"],
                        "peer_id": fresh_u["peer_id"],
                        "messages": fresh_u["messages"],
                    }
                    receipt_u = find_delivery(raw_u, saved_u, u_row["text"], set())
                    if receipt_u:
                        outbox.reconcile_late_delivery(u_row["id"], receipt_u)
                        tracker.observe(fresh_u)
                        tracker.db.execute(
                            "UPDATE recruiter_graph_autonomy SET status='sent', receipt=? WHERE account=? AND thread=? AND status='uncertain'",
                            (json.dumps(receipt_u), u_row["account"], u_row["thread"]),
                        )
                        tracker.db.commit()
                    # Return dedicated tab back to /messaging/ list view after direct thread check
                    reader.page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
        except Exception as exc:
            return {"status": "scan_error", "error": str(exc), "scanned": scanned, "read": read_count, "live_threads": list(dict.fromkeys(live_threads))}
        finally:
            reader.disconnect()
    return {"status": "ok", "scanned": scanned, "read": read_count, "live_threads": list(dict.fromkeys(live_threads))}


def _deliver_verified_reply(
    tracker: RecruiterTracker,
    graph: RecruiterGraph,
    outbox: MessageOutbox,
    swarm: RecruiterConversationSwarm,
    profile: dict,
    saved_snapshot: dict,
    review_id: str,
    cdp_url: str,
    test_override: bool = False,
    wait_jitter: bool = True,
) -> dict:
    reader = LinkedInMessagingReader(cdp_url, test_override=test_override)
    with (
        caller("autonomous-dm-responder", "social"),
        operation_identity("dm-reply-send:" + saved_snapshot["thread_id"]),
        resource_lease("browser:linkedin-primary", wait_seconds=210),
        browser_lease(),
    ):
        try:
            reader.connect()
            allowed, quota_reason = reader.manager.rate_limiter.check_quota("direct_message")
            if not allowed:
                return {"status": "quota_deferred", "reason": quota_reason}
            reobserved = tracker.observe(reader.read_saved_thread(saved_snapshot))
            if not reobserved.get("complete") or reobserved["messages"][-1]["ownership"] != "external":
                return {"status": "skipped", "reason": "Conversation changed or already answered before send"}
            ok_hiring, hiring_reason = _has_verified_hiring_signal(reobserved)
            if not ok_hiring:
                return {"status": "skipped", "reason": hiring_reason}
            if isinstance(LinkedInMessagingReader, type) and isinstance(reader, LinkedInMessagingReader):
                composer_open = reader.page.locator('.msg-thread form.msg-form [contenteditable="true"].msg-form__contenteditable')
                inmail_locked = composer_open.count() == 0 or not composer_open.first.is_visible()
                quick_yes = reader.page.locator(".msg-thread button.conversations-quick-replies__reply-button").filter(has_text="Yes")
                if inmail_locked and quick_yes.count() >= 1 and quick_yes.first.is_visible():
                    if wait_jitter:
                        reader.manager.rate_limiter.wait_jitter("direct_message")
                    quick_yes.first.click(timeout=5000)
                    reader.page.wait_for_timeout(2800)
                    latest_after_accept = tracker.observe(reader.read_saved_thread(saved_snapshot))
                    if latest_after_accept.get("messages") and latest_after_accept["messages"][-1]["ownership"] == "self":
                        reader.manager.rate_limiter.record_action("direct_message", {"thread": reobserved["thread_id"], "phase": "inmail_accept"})
                        if "/messaging/thread/" in reader.page.url:
                            reader.page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded", timeout=20000)
                        return {
                            "status": "sent",
                            "inmail_accepted": True,
                            "message_id": latest_after_accept["messages"][-1]["id"],
                            "text": latest_after_accept["messages"][-1]["text"],
                            "thread_id": latest_after_accept["thread_id"],
                            "thread_url": latest_after_accept["url"],
                        }
                    # If 'Yes' unlocked/prefilled the composer without auto-sending, clear LinkedIn's prefill and send our verified Swarm reply!
                    ed = reader.page.locator('.msg-thread form.msg-form [contenteditable="true"].msg-form__contenteditable')
                    if ed.count() == 1 and ed.inner_text().strip():
                        ed.fill("", timeout=3000)
                        ed.dispatch_event("input")
                    reobserved = latest_after_accept
            committed = swarm.commit(review_id, tracker.store, reobserved, profile)
            decision = committed.get("decision") or {}
            if committed["status"] != "draft_ready" or decision.get("action") != "draft_reply":
                return {"status": committed["status"], "reason": committed.get("error") or decision.get("reason", "")}
            draft_rows = [
                r
                for r in tracker.store.review(reobserved["account_id"])
                if r["thread"] == reobserved["thread_id"] and r["status"] == "draft"
            ]
            if not draft_rows:
                return {"status": "skipped", "reason": "No current draft row available after commit"}
            decision_id = draft_rows[0]["id"]
            outbox_id = outbox.authorize(decision_id, AUTHORIZATION_NOTE)
            if wait_jitter:
                reader.manager.rate_limiter.wait_jitter("direct_message")
            sender = LinkedInMessageSender(reader, tracker.store, outbox)
            try:
                receipt = sender.send(outbox_id)
            except Exception as pre_send_exc:
                # Pre-click failure leaves outbox row in 'authorized'; mark 'cancelled' so it never wedges future ticks.
                with tracker.db:
                    tracker.db.execute(
                        "UPDATE message_outbox SET status='cancelled', receipt=?, updated=? WHERE id=? AND status='authorized'",
                        (json.dumps({"pre_send_error": str(pre_send_exc)}), now(), outbox_id),
                    )
                    tracker.db.execute(
                        "UPDATE decisions SET status='blocked' WHERE id=? AND status='draft'",
                        (decision_id,),
                    )
                return {"outbox_id": outbox_id, "status": "blocked", "error": str(pre_send_exc)}
            rec_status = "sent" if receipt.get("status") == "sent" else "uncertain"
            graph.record_autonomy(
                reobserved,
                {"strategy_version": decision.get("strategy_version", "swarm-conversational-v12")},
                decision.get("draft", ""),
                status=rec_status,
                receipt=receipt,
            )
            try:
                latest_saved = tracker.store.snapshot(reobserved["account_id"], reobserved["thread_id"])
                tracker.observe(latest_saved)
                # Keep split-pane /messaging/ view in place when conversation list is already mounted (zero extra reload)
                if "/messaging/" not in reader.page.url or reader.page.locator(".msg-conversations-container__conversations-list").count() == 0:
                    reader.page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            return {"outbox_id": outbox_id, "text": decision.get("draft", ""), **receipt}
        except Exception as exc:
            return {"status": "blocked", "error": str(exc)}
        finally:
            reader.disconnect()


def _tick(
    *,
    cdp_url: str = "http://127.0.0.1:9222",
    max_scan: int = 12,
    max_reads: int = 6,
    max_replies: int = 4,
    force: bool = False,
    skip_scan: bool = False,
    dry_run: bool = False,
    name_filter: str | None = None,
) -> dict:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    if not force and time.time() < state.get("next_epoch", 0):
        return state
    tracker = RecruiterTracker()
    _init_inbox_table(tracker.db)
    graph = RecruiterGraph(tracker.store)
    outbox = MessageOutbox(tracker.store)
    swarm = None
    backup_database(tracker.store.path, keep=7)
    if BOARD_PATH.exists():
        backup_database(BOARD_PATH, keep=7)
    state.update(
        {
            "updated": now(),
            "pid": os.getpid(),
            "mode": "autonomous_dm_replies",
            "outbound_paused": False,
            "state": "checking",
            "detail": "Monitoring live LinkedIn Focused inbox (~60s cadence)",
        }
    )
    try:
        if STOP.exists():
            state.update(
                state="paused",
                outbound_paused=True,
                detail="Paused via data/recruiter-outreach.pause; no browser actions",
                next_epoch=time.time() + 120,
            )
            return state
        hour = dt.datetime.now().hour
        if not force and not (8 <= hour < 21):
            next_time = dt.datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
            if hour >= 21:
                next_time += dt.timedelta(days=1)
            state.update(
                state="waiting_for_operating_hours",
                detail="Autonomous DM replies resume at 08:00 local time",
                next_epoch=next_time.timestamp(),
            )
            return state
        if configured() is None:
            raise RuntimeError("Shared lab coordinator configuration is required")

        profile = load_profile()
        swarm = RecruiterConversationSwarm()

        scan_summary = {"status": "cached", "scanned": 0, "read": 0, "live_threads": state.get("live_focused_threads", [])}
        if not skip_scan:
            state.update(state="scanning_inbox", detail="Checking live LinkedIn Focused inbox DOM for new replies")
            export(tracker, state)
            try:
                scan_summary = _scan_inbox(
                    tracker,
                    swarm,
                    profile,
                    state,
                    cdp_url=cdp_url,
                    max_scan=max_scan,
                    max_reads=max_reads,
                    test_override=force,
                )
                if scan_summary.get("live_threads"):
                    state["live_focused_threads"] = scan_summary["live_threads"]
            except DependencyUnavailable as busy_exc:
                scan_summary = {
                    "status": "browser_busy_deferred",
                    "reason": str(busy_exc),
                    "scanned": 0,
                    "read": 0,
                    "live_threads": state.get("live_focused_threads", []),
                }
                print(f"[DM Scan] Browser busy ({busy_exc}); proceeding to review captured active DMs.", flush=True)

        live_focused_set = set(state.get("live_focused_threads") or [])
        live_order = {tid: idx for idx, tid in enumerate(state.get("live_focused_threads") or [])}

        candidates = []
        for row in tracker.db.execute("SELECT account, thread, snapshot, observed FROM threads ORDER BY observed DESC"):
            snap = json.loads(row["snapshot"])
            if not snap.get("account_id", "").startswith("urn:li:fsd_profile:"):
                continue
            if name_filter and snap.get("peer_name") != name_filter:
                continue
            if not name_filter and not _is_active_recent_thread(snap, live_focused_set):
                continue
            current = tracker.store.snapshot(row["account"], row["thread"])
            if not graph.autonomy_allowed(current):
                continue
            if _prior_deliveries(tracker, graph, current["account_id"], current["thread_id"]):
                continue
            needs, existing_run = _needs_swarm_review(tracker, swarm, current, profile)
            if needs:
                priority = (0, live_order[current["thread_id"]]) if current["thread_id"] in live_order else (1, 0)
                candidates.append((priority, current, existing_run))

        candidates.sort(key=lambda x: x[0])

        outcomes = []
        sent_count = 0
        for _, saved, existing_run in candidates:
            if len(outcomes) >= max_replies or STOP.exists():
                break
            peer_name = saved.get("peer_name", "Unknown")
            try:
                if existing_run and existing_run.get("status") == "draft_ready":
                    review = existing_run
                    decision = review.get("decision") or {}
                else:
                    graph_snapshot = graph.observe(saved, profile)
                    strategy = plan_strategy(
                        graph_snapshot,
                        saved,
                        [],
                        daily_autonomous_count=graph.autonomous_count_today(saved["account_id"]),
                    )

                    portal_outcome = None
                    if extract_external_job_urls(saved) and not dry_run:
                        state.update(
                            state="applying_external_portal",
                            detail=f"{peer_name}: inspecting/applying on external career portal link",
                        )
                        print(state["detail"], flush=True)
                        export(tracker, state)
                        try:
                            with (
                                caller("autonomous-dm-responder", "social"),
                                operation_identity("dm-ext-portal:" + saved["thread_id"]),
                                resource_lease("browser:linkedin-primary", wait_seconds=120),
                                browser_lease(),
                            ):
                                portal_outcome = process_thread_external_portals(saved, cdp_url=cdp_url)
                        except Exception as p_exc:
                            portal_outcome = {"status": "portal_unreachable", "reason": str(p_exc)[:180]}

                    def _progress(role, model_name, who=peer_name):
                        state.update(
                            state="swarm_reviewing",
                            detail=f"{who}: {role} ({model_name}) evaluating incoming DM",
                        )
                        print(state["detail"], flush=True)
                        export(tracker, state)

                    swarm.progress = _progress
                    review = swarm.review(
                        saved,
                        profile=profile,
                        prior_delivery=[],
                        graph_snapshot=graph_snapshot,
                        strategy=strategy,
                        external_portal_outcome=portal_outcome,
                    )
                    reader_artifact = next(
                        (
                            a["parsed"]
                            for a in review.get("artifacts", [])
                            if a["role"] == "reader" and a["parsed"] is not None and not a["validation_error"]
                        ),
                        None,
                    )
                    if reader_artifact:
                        graph.apply_reader(saved, reader_artifact, profile)
                        classification = (
                            "recruiter"
                            if reader_artifact["intent"] in {"recruiter_inquiry", "scheduling"}
                            else "not_recruiter"
                        )
                        facts = reader_artifact["known_role_facts"]
                        evidence = {
                            "basis": "Model reading of captured conversation",
                            "review_id": review["id"],
                            "source_id": facts[0]["source_id"]
                            if facts
                            else reader_artifact["source_message_ids"][-1],
                            "quote": facts[0]["quote"] if facts else "",
                        }
                        with tracker.db:
                            tracker.db.execute(
                                "UPDATE recruiter_contacts SET classification=?, evidence=? WHERE account=? AND thread=?",
                                (classification, json.dumps(evidence), saved["account_id"], saved["thread_id"]),
                            )
                    decision = review.get("decision") or {}

                if review.get('status')=='blocked':
                    from tools.desktop_questions import notify_recruiter_review_problem
                    notify_recruiter_review_problem(saved,review)

                if (
                    review["status"] in {"model_review_complete", "draft_ready"}
                    and decision.get("action") == "draft_reply"
                    and decision.get("auto_reply_eligible")
                ):
                    if dry_run:
                        committed = (
                            swarm.commit(review["id"], tracker.store, saved, profile)
                            if review["status"] == "model_review_complete"
                            else review
                        )
                        outcomes.append(
                            {
                                "name": peer_name,
                                "review_id": review["id"],
                                "status": "dry_run_ready",
                                "draft": (committed.get("decision") or {}).get("draft", ""),
                            }
                        )
                    else:
                        state.update(state="sending_dm_reply", detail=f"Sending verified reply to {peer_name}")
                        export(tracker, state)
                        delivery = _deliver_verified_reply(
                            tracker,
                            graph,
                            outbox,
                            swarm,
                            profile,
                            saved,
                            review["id"],
                            cdp_url=cdp_url,
                            test_override=force,
                        )
                        if delivery.get("status") == "sent":
                            sent_count += 1
                        outcomes.append({"name": peer_name, "review_id": review["id"], **delivery})
                        print(json.dumps(outcomes[-1], ensure_ascii=False), flush=True)
                        if delivery.get("status") in {"uncertain", "quota_deferred"}:
                            break
                else:
                    if review["status"] == "model_review_complete":
                        committed = swarm.commit(review["id"], tracker.store, saved, profile)
                    else:
                        committed = review
                    outcomes.append(
                        {
                            "name": peer_name,
                            "review_id": review["id"],
                            "status": committed["status"],
                            "reason": (committed.get("decision") or {}).get("reason") or committed.get("error", ""),
                        }
                    )
                    print(json.dumps(outcomes[-1], ensure_ascii=False), flush=True)
            except Exception as exc:
                outcomes.append({"name": peer_name, "status": "blocked", "error": str(exc)})
                print(json.dumps(outcomes[-1], ensure_ascii=False), flush=True)

        remaining = max(0, len(candidates) - len(outcomes))
        # Poll every ~65 seconds via the persistent /messaging/ tab so active conversations are answered within ~1-2 min
        state.update(
            state="monitoring",
            outbound_paused=False,
            detail=(
                f"Active DM monitor: {scan_summary.get('read', 0)} active thread(s) read, "
                f"{sent_count} reply(s) sent, {remaining} active remaining."
            ),
            last_scan={k: v for k, v in scan_summary.items() if k != "live_threads"},
            last_model_reviews=outcomes,
            review_models=MODELS,
            last_success=now(),
            last_review_completed=now(),
            next_epoch=time.time() + 65,
        )
    except Exception as exc:
        state.update(state="blocked", detail=str(exc), next_epoch=time.time() + 90)
        print("Autonomous DM responder deferred: " + str(exc), flush=True)
    finally:
        if swarm is not None:
            swarm.close()
        if state.get("next_epoch", 0) <= time.time():
            state["next_epoch"] = time.time() + 65
        state["next_attempt"] = dt.datetime.fromtimestamp(state["next_epoch"]).astimezone().isoformat()
        export(tracker, state)
        tracker.store.close()
    return state


def tick(**kwargs) -> dict:
    try:
        with instance_lock(ROOT / "data/recruiter-worker.lock"):
            return _tick(**kwargs)
    except RuntimeError as exc:
        if str(exc) == "availability controller already running":
            return {"state": "already_running"}
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["tick", "status", "pause", "resume"], nargs="?", default="tick")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument("--max-scan", type=int, default=12)
    parser.add_argument("--max-reads", type=int, default=6)
    parser.add_argument("--max-replies", type=int, default=4)
    parser.add_argument("--name", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-scan", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.action == "pause":
        STOP.write_text(now(), encoding="utf-8")
        print(json.dumps({"state": "paused", "pause_file": str(STOP)}))
    elif args.action == "resume":
        if STOP.exists():
            STOP.unlink()
        print(json.dumps({"state": "resumed"}))
    elif args.action == "status":
        print(STATE.read_text(encoding="utf-8") if STATE.exists() else json.dumps({"state": "uninitialized"}))
    else:
        res = tick(
            cdp_url=args.cdp_url,
            max_scan=max(1, min(25, args.max_scan)),
            max_reads=max(1, min(10, args.max_reads)),
            max_replies=max(1, min(5, args.max_replies)),
            force=args.force,
            skip_scan=args.skip_scan,
            dry_run=args.dry_run,
            name_filter=args.name,
        )
        print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
