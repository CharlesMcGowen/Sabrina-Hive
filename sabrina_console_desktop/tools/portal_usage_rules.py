"""Operator-reviewed inference rules. Model output cannot install or widen rules."""
import json
from pathlib import Path
from urllib.parse import urlsplit

RULES_PATH = Path(__file__).resolve().parents[1] / 'config/portal_usage_rules.json'


def assess(connection, profile, now, path=RULES_PATH):
    """Return applicable evidence and a denial; no rule means local fallback.

    Numeric rules are ceilings, never targets. An explicit rate-only contract may
    replace local volume limits; missing daily-quota documentation cannot do so.
    Discovery metadata is intentionally outside these inference contracts.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    host = urlsplit(json.loads(profile.get('adapter_json', '{}')).get('url', '')).hostname
    applicable = []
    for rule in data['rules']:
        scope = rule.get('scope')
        target = profile['provider'] if scope == 'provider' else host if scope == 'origin' else None
        if not target or rule.get('target') != target:
            continue
        required = ('id', 'source_url', 'evidence_summary', 'reviewed_at', 'expires_at', 'reviewer')
        if (any(not rule.get(k) for k in required) or not rule['source_url'].startswith('https://')
                or rule.get('traffic') != 'inference' or rule.get('authority') not in ('provider', 'owner')
                or rule.get('review_status') != 'approved'
                or not isinstance(rule['reviewed_at'], (int, float))
                or not isinstance(rule['expires_at'], (int, float))
                or not rule['reviewed_at'] <= now < rule['expires_at']):
            return applicable, 'usage_policy_review_required'
        daily = rule.get('daily_requests')
        rate = rule.get('rate')
        if daily is not None and (type(daily) is not int or daily < 1):
            return applicable, 'usage_policy_invalid'
        if daily is not None and rule.get('daily_window') not in ('utc', 'rolling_24h'):
            return applicable, 'usage_policy_invalid'
        if rate is not None and (not isinstance(rate, dict)
                or type(rate.get('requests')) is not int or rate['requests'] < 1
                or type(rate.get('seconds')) is not int or rate['seconds'] < 1):
            return applicable, 'usage_policy_invalid'
        if rule.get('mode') not in ('daily', 'rate_only') or (rule['mode'] == 'daily' and daily is None):
            return applicable, 'usage_policy_invalid'
        if rule['mode'] == 'rate_only' and (not rate or daily is not None or not rule.get('explicit_rate_only_evidence')):
            return applicable, 'usage_policy_invalid'
        applicable.append(rule)
        if scope == 'provider':
            rows = connection.execute("SELECT started FROM portal_requests WHERE provider=? AND purpose<>'discovery'", (target,)).fetchall()
        else:
            ids = [r['portal_id'] for r in connection.execute('SELECT portal_id,adapter_json FROM portal_profiles')
                   if urlsplit(json.loads(r['adapter_json']).get('url', '')).hostname == host]
            rows = connection.execute("SELECT started FROM portal_requests WHERE portal_id IN (" + ','.join('?' for _ in ids) + ") AND purpose<>'discovery'", ids).fetchall() if ids else []
        starts = [r['started'] for r in rows]
        window_start = now - 86400 if rule.get('daily_window') == 'rolling_24h' else int(now // 86400) * 86400
        if daily is not None and sum(t >= window_start for t in starts) >= daily:
            return applicable, 'documented_daily_quota'
        if rate and sum(t > now - rate['seconds'] for t in starts) >= rate['requests']:
            return applicable, 'documented_rate_limit'
    return applicable, ''


def public_evidence(rules):
    return [{k: r.get(k) for k in ('id', 'scope', 'target', 'source_url', 'evidence_summary',
                                  'reviewed_at', 'expires_at', 'mode', 'daily_requests', 'daily_window', 'rate')}
            for r in rules]
