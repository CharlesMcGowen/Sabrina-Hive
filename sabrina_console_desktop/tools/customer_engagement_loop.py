"""One bounded customer-email pass for Sabrina's allowlisted first account."""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path

import requests

from tools.customer_engagement import CustomerEngagementAssessor, load_customer_policy, standing_authorization_allows, validate_customer_decision
from tools.customer_followup import maybe_send_due_followup
from tools.gmail_customer_reader import GmailCustomerReader
from tools.gmail_message_sender import GmailMessageSender
from tools.messaging_loop import browser_lease
from tools.messaging_outbox import MessageOutbox
from tools.messaging_store import ConversationStore, ROOT, now


TRANSIENT_BROWSER_MARKERS = (
    "timeout",
    "connect_over_cdp",
    "connection refused",
    "connection reset",
    "target page, context or browser has been closed",
    "net::err_",
)
MODEL_RUNTIME_MARKERS = (
    "requests.exceptions.connect",
    "requests.exceptions.readtimeout",
    "requests.exceptions.timeout",
    "connectionerror",
    "readtimeout",
    "connecttimeout",
    "timeouterror",
    "http error",
    "ollama",
)


def _is_transient_browser_error(error):
    text = f"{type(error).__module__}.{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in TRANSIENT_BROWSER_MARKERS)


def _is_model_runtime_error(error):
    text = f"{type(error).__module__}.{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in MODEL_RUNTIME_MARKERS)


def resolve_customer_model_route(policy=None, runtime_path=None):
    """Resolve the routine customer route without permitting cloud fallback."""
    policy = policy or load_customer_policy()
    configured = policy.get("model_routing", {}).get("routine")
    if configured != "local_fast":
        raise ValueError("Routine customer model route must remain local_fast")
    runtime = json.loads(Path(runtime_path or ROOT / "config/agent_runtime.json").read_text(encoding="utf-8-sig"))
    nodes = runtime.get("nodes") or []
    models = (runtime.get("tiers") or {}).get("fast") or []
    if not nodes or not nodes[0].get("url") or not models:
        raise ValueError("The local fast customer model route is unavailable")
    return {
        "provider": "ollama",
        "policy_route": configured,
        "tier": "fast",
        "model": models[0],
        "node_id": nodes[0].get("id", "local"),
        "node_url": nodes[0]["url"].rstrip("/"),
        "cloud_fallback_allowed": False,
    }


def record_customer_model_usage(
    engagement: str,
    route: dict,
    payload: dict,
    wall_seconds: float,
    *,
    ledger_path=None,
):
    """Persist measured local-model time without storing prompts or customer text."""
    try:
        from tools.engagement_economics import EngagementEconomicsLedger

        duration_ns = payload.get("total_duration")
        if isinstance(duration_ns, (int, float)) and duration_ns > 0:
            duration_seconds = float(duration_ns) / 1_000_000_000
            measurement_basis = "ollama_total_duration"
        else:
            duration_seconds = max(0.0, float(wall_seconds))
            measurement_basis = "client_wall_clock"
        if duration_seconds <= 0:
            return {"status": "skipped", "reason": "no positive measured duration", "internal_only": True}
        receipt = {
            "measurement_basis": measurement_basis,
            "provider": route["provider"],
            "model": route["model"],
            "tier": route["tier"],
            "node_id": route["node_id"],
            "duration_seconds": duration_seconds,
            "prompt_eval_count": int(payload.get("prompt_eval_count") or 0),
            "eval_count": int(payload.get("eval_count") or 0),
            "customer_text_included": False,
            "prompt_included": False,
        }
        ledger = EngagementEconomicsLedger(ledger_path)
        try:
            event_id = ledger.record(
                engagement,
                "local_gpu_hours",
                duration_seconds / 3600,
                "hours",
                "customer_email_local_model",
                receipt,
            )
        finally:
            ledger.close()
        return {
            "status": "recorded",
            "event_id": event_id,
            "internal_only": True,
            "kind": "local_gpu_hours",
            "quantity_hours": duration_seconds / 3600,
            "measurement_basis": measurement_basis,
            "customer_text_included": False,
            "prompt_included": False,
        }
    except Exception as error:
        return {
            "status": "failed",
            "internal_only": True,
            "error_type": f"{type(error).__module__}.{type(error).__name__}",
            "error": str(error)[:300],
            "customer_text_included": False,
            "prompt_included": False,
        }


def local_model_call(system, prompt, *, route=None, usage_callback=None):
    route = route or resolve_customer_model_route()
    started = time.perf_counter()
    response = requests.post(
        route["node_url"] + "/api/chat",
        json={"model": route["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}], "stream": False, "format": "json"},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    reported_model = payload.get("model")
    if reported_model and reported_model != route["model"]:
        raise RuntimeError("Local customer model provenance mismatch")
    if usage_callback is not None:
        usage_callback(payload, time.perf_counter() - started)
    return payload["message"]["content"]


def _write_status(result, output_dir=None):
    output = Path(output_dir or ROOT / "artifacts/customer_engagement")
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / "status.json.tmp"
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output / "status.json")
    return str((output / "status.json").resolve())


def run_customer_pass(contact_id="understand-tech-prajwal", cdp_url=None, db_path=None, output_dir=None, send_safe=True, model_call=None):
    policy = load_customer_policy()
    store = ConversationStore(db_path)
    reader = GmailCustomerReader(cdp_url, policy)
    outbox = MessageOutbox(store)
    result = {
        "id": str(uuid.uuid4()),
        "started": now(),
        "contact_id": contact_id,
        "status": "starting",
        "phase": "browser_lease",
        "sent": False,
        "escalation": None,
    }
    if model_call is None:
        route = resolve_customer_model_route(policy)
        result["model_provenance"] = {
            key: route[key]
            for key in ("provider", "policy_route", "tier", "model", "node_id", "cloud_fallback_allowed")
        }
        result["model_provenance"]["invoked"] = False
        result["economics_telemetry"] = {
            "status": "not_invoked",
            "internal_only": True,
            "customer_text_included": False,
            "prompt_included": False,
        }

        def decision_model(system, prompt):
            result["model_provenance"]["invoked"] = True

            def usage_callback(payload, wall_seconds):
                result["economics_telemetry"] = record_customer_model_usage(
                    contact_id,
                    route,
                    payload,
                    wall_seconds,
                )

            return local_model_call(system, prompt, route=route, usage_callback=usage_callback)
    else:
        result["model_provenance"] = {
            "provider": "injected",
            "policy_route": "test_or_operator_override",
            "tier": None,
            "model": None,
            "node_id": None,
            "cloud_fallback_allowed": False,
            "invoked": False,
        }
        result["economics_telemetry"] = {
            "status": "not_applicable_injected_model",
            "internal_only": True,
            "customer_text_included": False,
            "prompt_included": False,
        }

        def decision_model(system, prompt):
            result["model_provenance"]["invoked"] = True
            return model_call(system, prompt)
    try:
        with browser_lease(ROOT / "data/customer_email_browser.lock"):
            result["phase"] = "connect"
            reader.connect()
            result["phase"] = "read_thread"
            snapshot = reader.open_allowlisted_thread(contact_id)
            version, changed = store.observe(snapshot)
            snapshot["version"] = version
            result.update({"account_id": snapshot["account_id"], "thread_id": snapshot["thread_id"], "conversation_changed": changed})
            assessor = CustomerEngagementAssessor(decision_model, policy)
            row = store.db.execute(
                "SELECT id,status,content FROM decisions WHERE account=? AND thread=? "
                "AND context_version=? AND profile_hash=? AND status!='stale'",
                (snapshot["account_id"], snapshot["thread_id"], version, assessor.profile_hash),
            ).fetchone()
            if row and row["status"] != "draft":
                if row["status"] in {"sending", "uncertain", "needs_input"}:
                    decision = json.loads(row["content"])
                    if row["status"] == "needs_input":
                        from tools.desktop_questions import notify_customer_question
                        notify_customer_question(snapshot, decision)
                    result["decision"] = decision
                    result["status"] = "uncertain" if row["status"] in {"sending", "uncertain"} else "needs_operator"
                    result["escalation"] = {"reason": decision.get("reason", "Saved decision requires review"), "next_action": "operator_reviews_customer_request", "minimum_meeting_notice_days": policy["meeting_policy"]["minimum_notice_days"]}
                    return result
                if send_safe:
                    result["phase"] = "send_followup"
                    followup = maybe_send_due_followup(reader, store, snapshot, policy)
                    result["followup"] = followup.get("followup")
                    if followup["status"] in {"sent", "uncertain"}:
                        result.update({key: value for key, value in followup.items() if key != "followup"})
                        return result
                result["status"] = "unchanged"
                return result
            result["phase"] = "classify"
            if row:
                # A saved draft is pending work, not proof of delivery. Reuse its
                # identity so the outbox's existing claim/receipt prevents retries.
                decision = validate_customer_decision(json.loads(row["content"]), snapshot, policy)
                result["reused_saved_draft"] = True
            else:
                decision = assessor.assess(snapshot)
            result["phase"] = "verify_thread"
            fresh = reader.read_saved_thread(snapshot)
            fresh_version, _ = store.observe(fresh)
            if fresh_version != version:
                # No outbox authorization or delivery has started. Discard this
                # obsolete proposal and let the bounded scheduler read again.
                result.update(status='retry',retry_after_seconds=60,
                              error='Customer thread changed while Sabrina was drafting',
                              error_source='customer_gmail',error_type='conversation_changed')
                return result
            if row is None:
                store.save_decision(snapshot["account_id"], snapshot["thread_id"], version, assessor.profile_hash, decision)
            result["decision"] = decision
            row = store.db.execute(
                "SELECT id FROM decisions WHERE account=? AND thread=? AND context_version=? AND profile_hash=?",
                (snapshot["account_id"], snapshot["thread_id"], version, assessor.profile_hash),
            ).fetchone()
            if decision["action"] == "ask_human":
                from tools.desktop_questions import notify_customer_question
                notify_customer_question(snapshot, decision)
                result["status"] = "needs_operator"
                result["escalation"] = {"reason": decision["reason"], "next_action": decision["next_action"], "minimum_meeting_notice_days": policy["meeting_policy"]["minimum_notice_days"]}
            elif send_safe and standing_authorization_allows(decision, policy):
                result["phase"] = "send"
                source = policy["standing_authorization"]["source"]
                outbox_id = outbox.authorize(row["id"], "Standing authorization: " + source)
                receipt = GmailMessageSender(reader, store, outbox).send(outbox_id)
                result["receipt"] = receipt
                result["sent"] = receipt["status"] == "sent"
                result["status"] = receipt["status"]
            else:
                result["status"] = "draft_saved" if decision["action"] == "draft_reply" else "no_reply"
                if result["status"] == "no_reply" and send_safe:
                    result["phase"] = "send_followup"
                    followup = maybe_send_due_followup(reader, store, snapshot, policy)
                    result["followup"] = followup.get("followup")
                    if followup["status"] in {"sent", "uncertain"}:
                        result.update({key: value for key, value in followup.items() if key != "followup"})
            return result
    except Exception as error:
        safe_retry_phases = {"browser_lease", "connect", "read_thread", "verify_thread"}
        phase = result.get("phase")
        error_sources = {
            "browser_lease": "customer_gmail",
            "connect": "customer_gmail",
            "read_thread": "customer_gmail",
            "classify": "customer_decision_model",
            "verify_thread": "customer_gmail",
            "send": "customer_delivery",
            "send_followup": "customer_delivery",
        }
        result["error_source"] = error_sources.get(phase, "customer_workflow")
        result["error_type"] = f"{type(error).__module__}.{type(error).__name__}"
        if phase in safe_retry_phases and _is_transient_browser_error(error):
            result["status"] = "retry"
        elif phase == "classify" and _is_model_runtime_error(error):
            result["status"] = "model_retry"
        else:
            result["status"] = "blocked"
        if result["status"] in {"retry", "model_retry"}:
            result["retry_after_seconds"] = 60
        result["error"] = str(error)
        return result
    finally:
        result["finished"] = now()
        result["status_path"] = _write_status(result, output_dir)
        if result.get('status') in {'sent','unchanged','no_reply','draft_saved'} and result.get('thread_id'):
            try:
                from tools.desktop_questions import withdraw_question
                withdraw_question({'account_id':result.get('account_id',''),'thread_id':result['thread_id']})
            except Exception:
                pass
        reader.disconnect()
        store.close()


def read_status(output_dir=None):
    path = Path(output_dir or ROOT / "artifacts/customer_engagement") / "status.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"status": "never_run"}


def run_customer_monitor(cycles=288, interval=300, *, contact_id="understand-tech-prajwal", cdp_url=None,
                         output_dir=None, pass_fn=None, sleep_fn=None, stop_path=None):
    """Run bounded passes and stop before any state that needs human judgment."""
    cycles=max(1,min(int(cycles),2016))
    interval=max(30,min(float(interval),3600))
    output=Path(output_dir or ROOT/"artifacts/customer_engagement")
    output.mkdir(parents=True,exist_ok=True)
    stop_file=Path(stop_path or ROOT/"data/customer_engagement.stop")
    stop_file.unlink(missing_ok=True)
    pass_fn=pass_fn or run_customer_pass
    sleep_fn=sleep_fn or time.sleep
    monitor={"id":str(uuid.uuid4()),"started":now(),"status":"running","cycles_requested":cycles,
             "interval_seconds":interval,"contact_id":contact_id,"passes":[]}
    try:
        for number in range(1,cycles+1):
            if stop_file.exists():
                monitor["status"]="stopped";break
            outcome=pass_fn(contact_id=contact_id,cdp_url=cdp_url,output_dir=output)
            monitor["passes"].append({"number":number,"finished":outcome.get("finished"),"status":outcome.get("status"),
                                      "sent":outcome.get("sent",False),"escalation":outcome.get("escalation")})
            monitor["passes"]=monitor["passes"][-50:]
            monitor["updated"]=now()
            temporary=output/"monitor.json.tmp"
            temporary.write_text(json.dumps(monitor,ensure_ascii=False,indent=2),encoding="utf-8")
            temporary.replace(output/"monitor.json")
            if outcome.get("status") in {"needs_operator","uncertain"}:
                monitor["status"]="attention_required"
                escalation={"created":now(),"contact_id":contact_id,"status":outcome.get("status"),
                            "escalation":outcome.get("escalation"),"receipt":outcome.get("receipt"),
                            "phone_sms_sent":False,"calendar_booked":False,
                            "reason":"External phone/SMS and calendar connections are not configured."}
                (output/"operator_escalation.json").write_text(json.dumps(escalation,ensure_ascii=False,indent=2),encoding="utf-8")
                break
            if outcome.get("status")=="blocked":
                monitor["status"]="blocked";break
            if number<cycles:
                sleep_fn(interval)
        else:
            monitor["status"]="complete"
    except KeyboardInterrupt:
        monitor["status"]="stopped"
    finally:
        monitor["finished"]=now()
        temporary=output/"monitor.json.tmp"
        temporary.write_text(json.dumps(monitor,ensure_ascii=False,indent=2),encoding="utf-8")
        temporary.replace(output/"monitor.json")
    return monitor


def main():
    parser = argparse.ArgumentParser(description="Run one bounded Sabrina customer email pass")
    parser.add_argument("--contact-id", default="understand-tech-prajwal")
    parser.add_argument("--cdp-url")
    parser.add_argument("--draft-only", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--monitor", action="store_true", help="deprecated; use tools.customer_job_scheduler run")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--cycles", type=int, default=288)
    parser.add_argument("--interval", type=float, default=300)
    args = parser.parse_args()
    if args.stop:
        path=ROOT/"data/customer_engagement.stop";path.parent.mkdir(parents=True,exist_ok=True);path.write_text(now(),encoding="utf-8")
        value={"status":"stop_requested","path":str(path)}
    elif args.status:
        value = read_status()
    elif args.monitor:
        value = {
            "status": "disabled",
            "reason": "The legacy monitor was replaced to prevent overlapping schedulers.",
            "replacement": "python -m tools.customer_job_scheduler run",
        }
    else:
        value = run_customer_pass(args.contact_id, args.cdp_url, send_safe=not args.draft_only)
    print(json.dumps(value, indent=2))
    return 0 if value.get("status") not in {"blocked", "uncertain"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
