"""Conversation selection over the registered public portal pool, without promotion."""
import re
from datetime import datetime, timezone


def annotate(policy, entries):
    day = datetime.fromtimestamp(policy.clock(), timezone.utc).date().isoformat()
    cutoff = policy.clock() - 14 * 86400
    hard_failures = {
        'provider_error', 'unrecognized_response', 'endpoint_missing',
        'prompt_echo', 'ambiguous_output_schema', 'unsupported_adapter',
    }
    with policy.db() as c:
        budget = c.execute("SELECT portal_id,COUNT(*) n FROM portal_requests WHERE day=? AND purpose IN ('evaluation','repair','discovery','supervised_chat') GROUP BY portal_id", (day,)).fetchall()
        chats = c.execute("SELECT portal_id,COUNT(*) n,MAX(started) last FROM portal_requests WHERE day=? AND purpose='supervised_chat' GROUP BY portal_id", (day,)).fetchall()
        holds = c.execute('SELECT scope,reason,retry_after FROM portal_access_holds').fetchall()
        hist_rows = c.execute(
            "SELECT portal_id, result FROM portal_requests WHERE state='complete' AND started>=? ORDER BY started DESC",
            (cutoff,),
        ).fetchall()
    used = {r['portal_id']: r['n'] for r in budget}
    chat = {r['portal_id']: dict(r) for r in chats}
    ok_counts, fail_counts, trailing_fails, saw_ok = {}, {}, {}, set()
    for r in hist_rows:
        pid = r['portal_id']
        res = (r['result'] or '').strip()
        if not res:
            ok_counts[pid] = ok_counts.get(pid, 0) + 1
            saw_ok.add(pid)
        elif res in hard_failures:
            fail_counts[pid] = fail_counts.get(pid, 0) + 1
            if pid not in saw_ok:
                trailing_fails[pid] = trailing_fails.get(pid, 0) + 1
    total, chat_total = sum(used.values()), sum(r['n'] for r in chats)
    budget_mode = policy.setting('chat_budget_mode') or 'shared'
    per_portal_chat = budget_mode == 'per_portal'
    held = {r['scope']:r['reason'] for r in holds if not r['retry_after'] or r['retry_after'] > policy.clock()}
    result = []
    for entry in entries:
        pid = entry['portal_id']
        evidence = policy.chat_eligibility(pid)
        reason = evidence['reason']
        allowed = evidence['eligible']
        profile = policy.profile(pid)
        from tools.portal_usage_rules import assess, public_evidence
        with policy.db() as c:
            rules, rule_denial = assess(c, profile, policy.clock()) if profile else ([], '')
        if allowed:
            hold = held.get('portal:'+pid) or held.get('provider:'+profile['provider'])
            if hold:
                allowed, reason = False, hold
            elif rule_denial:
                allowed, reason = False, rule_denial
            elif budget_mode != 'unlimited' and not rules and ((not per_portal_chat and total >= policy.setting('daily_limit')) or used.get(pid,0) >= policy.setting('portal_daily_limit')):
                allowed, reason = False, 'daily_budget_exhausted'
            elif budget_mode != 'unlimited' and not rules and ((not per_portal_chat and chat_total >= (policy.setting('chat_daily_limit') or 50))
                                or chat.get(pid,{}).get('n',0) >= (policy.setting('chat_portal_daily_limit') or 5)):
                allowed, reason = False, 'chat_budget_exhausted'
        result.append({**entry, 'chat_eligible': allowed, 'chat_reason': reason,
                       'usage_policy_source': 'reviewed_inference_rules' if rules else 'local_fallback_not_provider_entitlement',
                       'usage_policy_rules': public_evidence(rules),
                       'last_chat':chat.get(pid,{}).get('last',0),
                       'recent_ok': ok_counts.get(pid, 0),
                       'recent_hard_failures': fail_counts.get(pid, 0),
                       'trailing_hard_failures': trailing_fails.get(pid, 0),
                       'status_badge': 'SUPERVISED CHAT' if allowed else 'CHAT BLOCKED: '+reason})
    return result


_NARROW_UTILITY_TOKENS = ('geocalc', 'geocod', 'weather', 'whisper', 'text-to-speech', 'tts', 'flux', 'sdxl')


def select(entries, message, limit=3, *, task_evidence=None, require_measured=False):
    """Rank measured task fit before label similarity; never broaden admission.

    Task evidence is supplied by the host's blackboard, not by model output.
    Two observed failures with no success exclude only this task/role. Unknown
    workers remain experimental unless the caller requires measured success.
    Persistently failing portals (>=2 trailing hard transport/provider failures)
    and narrow non-chat utilities (unless prompt-matched) are deprioritized/excluded
    so broken endpoints cannot poison collective runs.
    """
    words = set(re.findall(r'[a-z]{4,}', message.lower())) - {'with','what','kind','questions','help','hello','please','that','this','from','have'}
    evidence = task_evidence or {}
    def fit(entry):
        if int(entry.get('trailing_hard_failures', 0) or 0) >= 2:
            return False
        if int(entry.get('recent_hard_failures', 0) or 0) >= 2 and int(entry.get('recent_ok', 0) or 0) == 0:
            return False
        pid_lbl = (entry.get('portal_id', '') + ' ' + entry.get('label', '')).lower()
        if any(tok in pid_lbl for tok in _NARROW_UTILITY_TOKENS) and not any(tok in message.lower() for tok in _NARROW_UTILITY_TOKENS):
            return False
        row = evidence.get(entry['portal_id'], {})
        try:
            attempts, passed = int(row.get('attempts', 0)), int(row.get('tests_passed', 0))
        except (TypeError, ValueError, AttributeError):
            return False
        if attempts < 0 or not 0 <= passed <= attempts:
            return False
        return not (attempts >= 2 and passed == 0) and (not require_measured or passed > 0)
    def rank(entry):
        terms = set(re.findall(r'[a-z]{4,}', (entry.get('label','')+' '+entry['portal_id']).lower()))
        row = evidence.get(entry['portal_id'], {})
        attempts, passed = int(row.get('attempts',0)), int(row.get('tests_passed',0))
        measured = (-(passed+1)/(attempts+2), -(passed > 0)) if task_evidence is not None else ()
        proven_health = (
            int(entry.get('trailing_hard_failures', 0) or 0) > 0,
            not (int(entry.get('recent_ok', 0) or 0) > 0),
        )
        return measured + proven_health + (-len(words & terms), entry.get('access_state') != 'reachable', entry.get('last_chat',0), entry['portal_id'])
    safety_task = bool(words & {'safety','moderation','guardrail','security','privacy','firewall'})
    return sorted((e for e in entries if e.get('chat_eligible') and fit(e) and
                   (not e.get('is_guardrail') or safety_task)), key=rank)[:limit]
