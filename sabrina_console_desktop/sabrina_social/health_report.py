"""Read-only scheduled diagnostics; never submits work or reconciles leases."""
from collections import Counter
import time


def health_report(store, client_factory=None):
    if client_factory is None:
        from tools.control_client import configured
        client_factory = configured
    jobs = store.list(limit=500)
    counts = dict(Counter(j['status'] for j in jobs))
    findings = []
    for job in jobs:
        if job['status'] in {'paused', 'failed', 'blocked'}:
            findings.append({'source':'scheduled_jobs', 'id':job['id'],
                             'capability':job['capability'], 'state':job['status'],
                             'reason':(job.get('last_error') or '')[:500]})
    lab = {'available':False}
    try:
        client = client_factory()
        if client is None:
            raise RuntimeError('coordinator not configured')
        state = client.request('/v1/status')
        pending = [r for r in state.get('requests', []) if r.get('state') not in
                   {'completed','failed','cancelled'}]
        lab = {'available':True, 'request_counts':dict(Counter(r['state'] for r in pending)),
               'leases':[{'resource':x.get('resource'), 'state':x.get('state')}
                         for x in state.get('leases', [])]}
        for r in pending:
            if r.get('error') or r.get('state') == 'uncertain':
                findings.append({'source':'lab', 'id':r['id'], 'state':r['state'],
                                 'reason':r.get('error','')[:500]})
    except Exception as exc:
        lab['error'] = type(exc).__name__
        findings.append({'source':'lab','state':'unavailable','reason':str(exc)[:300]})
    return {'status':'report_ready', 'observed_at':time.time(), 'read_only':True,
            'scheduled_job_counts':counts, 'lab':lab, 'findings':findings,
            'vision_status':'not_tested',
            'summary':f"Read-only report: {len(findings)} blocker/paused findings; lab "
                      + ('reachable' if lab['available'] else 'unavailable')}
