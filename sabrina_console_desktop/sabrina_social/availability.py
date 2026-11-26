"""Independent lab recovery controller. No inference or unjournaled social actions."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import subprocess
import sys
import time
import urllib.request

from .availability_support import (IncidentLedger, atomic_json, backup_database,
                                   instance_lock, notify_systemd, read_json)
from .recovery import RecoveryJournal
from tools.worker_identity import executor_identity, stopped_evidence

ROOT = Path(__file__).resolve().parents[1]
SERVICES = {'sabrina-social-api.service', 'sabrina-social-scheduler.service',
            'sabrina-social-pulse.service'}
RECOVERABLE = {
    'linkedin.browser', 'linkedin.replies', 'social.semantic_review',
    'x.feed', 'x.status', 'x.analytics',
    'social.cybersecurity_scout', 'linkedin.jobs_discover', 'linkedin.jobs_inspect',
    'linkedin.inbox_triage', 'linkedin.performance_report',
    'ai_services.health_and_recover', 'rsi.universal_reflect', 'system.health_report',
    'kraken.inspect', 'capabilities.dual_orchestration',
    'frontier.research.rsi_augment', 'frontier.research.wsp_thinking',
    'frontier.research.hard_challenge', 'frontier.research.synthesis',
    'frontier.research.blt_augmentation',
    'resources.internet_index_sweep',
    'compression.autonomous_experiment',
    'exploitgym.rsi_cycle',
    'system.self_repair_workshop',
    'system.compute_federation',
    'github.autonomous_repo_forge',
    'forge.daily_paper_to_code',
    'bounty.cve_variant_hunter',
}


def get_json(url, timeout=3):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout) as response:
        return json.load(response)


def status_snapshot(data_dir=None, now=None):
    root = Path(data_dir or os.environ.get('SABRINA_SOCIAL_DATA_DIR', ROOT / 'data/social'))
    now = time.time() if now is None else now
    value = read_json(root / 'availability-status.json')
    if not value or not 0 <= now - value.get('updated', 0) < 120:
        return {**value, 'status': 'degraded', 'ready': False,
                'blocking_dependencies': ['recovery_controller_stale'],
                'operator_action': 'Inspect the availability controller'}
    return value


def reconcile_local_leases(client, leases, repair=False):
    results = []
    for lease in leases:
        if lease.get('state') != 'uncertain' or not lease.get('resource', '').startswith('browser:'):
            continue
        proof = stopped_evidence(lease.get('executor'))
        result = {'resource': lease['resource'], 'owner': lease['owner'], **proof}
        if proof['stopped'] and repair:
            receipt = client.request('/v1/leases/evidence', {
                'resource': lease['resource'], 'owner': lease['owner'],
                'executor': lease['executor'], 'observer': executor_identity(), 'evidence': proof})
            result['result'] = client.request('/v1/leases/reconcile', {
                'resource': lease['resource'], 'expected_owner': lease['owner'],
                'confirmed_stopped': True, 'automatic': True,
                'evidence_id': receipt['evidence_id'], 'reason': proof['reason']})
        results.append(result)
    return results


def restore_deferred_jobs(path, now=None):
    """Only historical infrastructure failures of recurring workflows, never operator pauses."""
    now = now or time.time()
    restored = []
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT * FROM scheduled_jobs WHERE status='failed' AND interval_min IS NOT NULL").fetchall():
            if row['capability'] not in RECOVERABLE or not (row['last_error'] or '').startswith('Waiting for resource: '):
                continue
            db.execute("UPDATE scheduled_jobs SET status='queued',attempts=0,next_run=?,updated=?,last_error=NULL WHERE id=? AND status='failed'",
                       (now, now, row['id']))
            db.execute('INSERT INTO scheduled_job_events(job_id,phase,detail,created) VALUES(?,?,?,?)',
                       (row['id'], 'dependency_restored', json.dumps({'coalesced': True}), now))
            restored.append(row['id'])
    return restored


def restore_wrongly_blocked_readonly_jobs(path, now=None):
    """Un-wedge recurring read-only jobs previously blocked by legacy worker-stop classification."""
    now = now or time.time()
    restored = []
    safe_readonly = RECOVERABLE - {'linkedin.browser'}
    with closing(sqlite3.connect(path, timeout=5)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute(
            "SELECT * FROM scheduled_jobs WHERE status='blocked' AND interval_min IS NOT NULL"
        ).fetchall():
            if row['capability'] not in safe_readonly:
                continue
            if (row['last_error'] or '') != 'Stopped worker requires social outcome verification; automatic replay refused':
                continue
            db.execute(
                "UPDATE scheduled_jobs SET status='queued',attempts=0,next_run=?,updated=?,last_error=NULL WHERE id=? AND status='blocked'",
                (now, now, row['id']),
            )
            db.execute(
                'INSERT INTO scheduled_job_events(job_id,phase,detail,created) VALUES(?,?,?,?)',
                (row['id'], 'readonly_block_cleared', json.dumps({'capability': row['capability']}), now),
            )
            restored.append(row['id'])
    return restored


def recover_abandoned_jobs(path, social_path):
    """Known-dead read workers can resume; submissions retain an individual review block."""
    recovered = []
    with closing(sqlite3.connect(path, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_executors'").fetchone():
            return recovered
        rows = db.execute("SELECT j.*,e.identity FROM scheduled_jobs j JOIN scheduled_executors e ON j.id=e.job_id WHERE j.status='running'").fetchall()
        for row in rows:
            if not stopped_evidence(json.loads(row['identity']))['stopped']:
                continue
            safe = row['capability'] in RECOVERABLE - {'linkedin.browser'}
            if row['capability'] == 'linkedin.browser' and Path(social_path).exists():
                identity = json.loads(row['identity'])
                from .store import SocialStore
                ident = SocialStore.text_hash('engage_feed:' + str(identity.get('operation_id')))
                with closing(sqlite3.connect(social_path, timeout=3)) as social:
                    intent = social.execute('SELECT phase FROM action_intents WHERE id=?', (ident,)).fetchone()
                    safe = intent is None or intent[0] in {'prepared', 'not_submitted'}
                    if intent and intent[0] == 'prepared':
                        social.execute("UPDATE action_intents SET phase='not_submitted',updated=? WHERE id=? AND phase='prepared'", (time.time(), ident)); social.commit()
            state = 'queued' if safe else 'blocked'
            error = None if safe else 'Stopped worker requires social outcome verification; automatic replay refused'
            backoff = max(900, min(int(row['interval_min'] or 1800), 3600)) if safe else 0
            with db:
                db.execute('UPDATE scheduled_jobs SET status=?,next_run=?,updated=?,attempts=MAX(0,attempts-1),last_error=? WHERE id=? AND status=\'running\'',
                           (state, time.time() + backoff, time.time(), error, row['id']))
                db.execute('INSERT INTO scheduled_job_events(job_id,phase,detail,created) VALUES(?,?,?,?)',
                           (row['id'], 'worker_reconciled', json.dumps({'state': state}), time.time()))
            recovered.append({'id': row['id'], 'status': state})
    return recovered


class AvailabilityController:
    def __init__(self, data_dir=None, repair=False, client=None, probe=get_json, run=subprocess.run):
        self.root = Path(data_dir or os.environ.get('SABRINA_SOCIAL_DATA_DIR', ROOT / 'data/social'))
        self.root.mkdir(parents=True, exist_ok=True)
        self.repair, self.client, self.probe, self.run = repair, client, probe, run
        self.journal = RecoveryJournal(self.root / 'availability-recovery.sqlite3', infrastructure=True)
        self.incidents = IncidentLedger(self.root / 'availability.sqlite3')
        self.auth = read_json(self.root / 'authentication-status.json')
        self.last_backup = 0
        self.api_failures = 0

    def ensure_service(self, unit, stale=False):
        if unit not in SERVICES:
            raise ValueError('Unregistered availability service')
        state = self.run(['systemctl', '--user', 'is-active', unit], capture_output=True, text=True, timeout=5)
        if state.stdout.strip() == 'active' and not stale:
            self.journal.resolve(unit)
            return {'status': 'running'}
        if not self.repair:
            return {'status': 'would_restart' if stale else 'would_start'}
        receipt = self.journal.begin(unit, 'restart' if stale else 'start', 'progress_stalled' if stale else 'service_down')
        if receipt['status'] != 'approved':
            return receipt
        result = self.run(['systemctl', '--user', 'restart' if stale else 'start', unit],
                          capture_output=True, text=True, timeout=40)
        observed = self.run(['systemctl', '--user', 'is-active', unit], capture_output=True, text=True, timeout=5)
        ready = result.returncode == 0 and observed.stdout.strip() == 'active'
        value = {'status': 'running' if ready else 'service_start_failed'}
        self.journal.finish(unit, receipt, value, verified=ready)
        return value

    def tick(self):
        now, blockers, details = time.time(), [], {}
        # Each blocking step is bounded; watchdog pings reflect actual loop progress.
        from tools.control_client import configured
        client = self.client or configured()
        state = {}
        try:
            if client is None:
                raise RuntimeError('coordinator_not_configured')
            state = client.request('/v1/status')
            details['leases'] = reconcile_local_leases(client, state.get('leases', []), self.repair)
            if self.repair and any(x.get('result') for x in details['leases']):
                state = client.request('/v1/status')
        except Exception as exc:
            blockers.append('coordinator_unavailable')
            details['coordinator_error'] = type(exc).__name__
        notify_systemd()
        browser_uncertain = [l for l in state.get('leases', [])
                             if l.get('resource') == 'browser:linkedin-primary' and l.get('state') == 'uncertain']
        browser_busy = any(l.get('resource') == 'browser:linkedin-primary' and l.get('state') == 'held'
                           for l in state.get('leases', []))
        if browser_uncertain:
            blockers.append('browser_lease_requires_reconciliation')
        bridge, cdp = {}, {}
        for key, url in [('bridge', os.environ.get('SABRINA_BRIDGE_RECOVERY_URL', 'http://127.0.0.1:19225') + '/health'),
                         ('cdp', os.environ.get('SABRINA_SOCIAL_CDP_URL', 'http://127.0.0.1:19224') + '/json/version')]:
            try:
                value = self.probe(url)
                if key == 'bridge': bridge = value
                else: cdp = value
            except Exception:
                blockers.append(key + '_unreachable')
        if bridge and bridge.get('status') != 'bridge_ready':
            blockers.append('bridge_' + bridge.get('status', 'unknown'))
        pulse = RecoveryJournal(self.root / 'recovery.sqlite3', infrastructure=True)
        if self.repair:
            details['pulse_guard'] = pulse.reconcile_stopped_pulse()
        else:
            with pulse.db() as db:
                guard = db.execute("SELECT state FROM pulse_guard WHERE name='social'").fetchone()
            details['pulse_guard'] = {'status': guard['state'] if guard else 'clear'}
        if details['pulse_guard'].get('status') in {'operator_required', 'uncertain'}:
            blockers.append('pulse_outcome_requires_review')
        supervisor = read_json(self.root / 'supervisor-status.json')
        try:
            self.probe('http://127.0.0.1:8087/livez')
            self.api_failures = 0
        except Exception:
            self.api_failures += 1
            blockers.append('social_api_unreachable')
        if self.repair:
            for unit in sorted(SERVICES):
                stale = (unit == 'sabrina-social-pulse.service' and bool(supervisor) and now - supervisor.get('updated', 0) > 120) or (
                    unit == 'sabrina-social-api.service' and self.api_failures >= 3)
                details[unit] = self.ensure_service(unit, stale)
                notify_systemd()
        if now - supervisor.get('updated', 0) > 120:
            blockers.append('supervisor_heartbeat_stale')
        if cdp.get('webSocketDebuggerUrl') and not browser_uncertain and not browser_busy and state and now - self.auth.get('updated', 0) >= 300:
            # DOM/session validation is a read-only, separately bounded worker, not a URL inference.
            try:
                checked = self.run([sys.executable, '-m', 'sabrina_social.supervisor', '--check-auth',
                                    '--cdp-url', os.environ.get('SABRINA_SOCIAL_CDP_URL', 'http://127.0.0.1:19224')],
                                   capture_output=True, text=True, timeout=55)
                value = json.loads(checked.stdout.strip().splitlines()[-1]) if checked.returncode == 0 else {}
                self.auth = {'authenticated': value.get('authenticated') is True,
                             'status': value.get('status', 'preflight_failed'), 'updated': time.time()}
                atomic_json(self.root / 'authentication-status.json', self.auth)
            except Exception:
                self.auth = {'authenticated': False, 'status': 'preflight_unavailable', 'updated': time.time()}
        if not self.auth.get('authenticated') or now - self.auth.get('updated', 0) > 300 and not browser_busy:
            blockers.append('authentication_preflight_required')
        scheduler_path = self.root / 'scheduler.sqlite3'
        if scheduler_path.exists():
            if self.repair:
                details['abandoned_jobs'] = recover_abandoned_jobs(scheduler_path, self.root / 'social.sqlite3')
                details['unwedged_readonly_jobs'] = restore_wrongly_blocked_readonly_jobs(scheduler_path, now)
            try:
                with closing(sqlite3.connect(scheduler_path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                    row = db.execute("SELECT updated,detail FROM scheduler_heartbeat WHERE name='scheduler'").fetchone()
                detail = json.loads(row[1]) if row else {}
                with closing(sqlite3.connect(scheduler_path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                    workers = db.execute("SELECT e.identity FROM scheduled_executors e JOIN scheduled_jobs j ON j.id=e.job_id WHERE j.status='running'").fetchall()
                overdue = any(0 < json.loads(w[0]).get('deadline', 0) < now for w in workers)
                if overdue or not row or now - row[0] > 120 and now > detail.get('worker_deadline', 0):
                    blockers.append('scheduler_heartbeat_stale')
                    if self.repair:
                        details['scheduler_recovery'] = self.ensure_service('sabrina-social-scheduler.service', stale=True)
            except sqlite3.Error:
                blockers.append('scheduler_state_unavailable')
        else:
            blockers.append('scheduler_state_missing')
        ready = not blockers
        if ready and self.repair:
            details['resumed_jobs'] = restore_deferred_jobs(scheduler_path)
        last_success = None
        review_count = 0
        social = self.root / 'social.sqlite3'
        if social.exists():
            with closing(sqlite3.connect(social.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                last_success = db.execute("SELECT MAX(updated) FROM operations WHERE kind='social_pulse' AND status='completed'").fetchone()[0]
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='action_intents'").fetchone():
                    review_count = db.execute("SELECT count(*) FROM action_intents WHERE phase IN ('unknown','submission_started')").fetchone()[0]
        if now - self.last_backup >= 86400:
            for path in list(self.root.glob('*.sqlite3')) + [ROOT / 'data/control/coordinator.sqlite3']:
                backup_database(path)
            self.last_backup = now
        self.incidents.observe('social_workflow', ready, ', '.join(blockers))
        value = {'service': 'sabrina-availability', 'status': 'ready' if ready else 'degraded',
                 'ready': ready, 'mode': 'repair' if self.repair else 'observe', 'updated': time.time(),
                 'blocking_dependencies': blockers, 'authentication': self.auth,
                 'last_successful_social_run': last_success, 'details': details,
                 'operation_review_count': review_count,
                 'recovery': self.journal.snapshot(), **self.incidents.snapshot(),
                 'operator_action': 'Review quarantined operation/executor or authentication' if any(
                     x in blockers for x in ('pulse_outcome_requires_review', 'browser_lease_requires_reconciliation',
                                            'authentication_preflight_required')) else None}
        atomic_json(self.root / 'availability-status.json', value)
        notify_systemd()
        return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    controller = AvailabilityController(repair=args.repair)
    with instance_lock(controller.root / 'availability.lock'):
        notify_systemd('READY=1\nWATCHDOG=1')
        while True:
            try:
                result = controller.tick()
            except Exception as exc:
                result = {'status': 'degraded', 'ready': False, 'updated': time.time(),
                          'blocking_dependencies': ['controller_error'], 'error_type': type(exc).__name__}
                atomic_json(controller.root / 'availability-status.json', result)
            if args.once:
                print(json.dumps(result)); return
            notify_systemd()
            time.sleep(30)


if __name__ == '__main__':
    main()
