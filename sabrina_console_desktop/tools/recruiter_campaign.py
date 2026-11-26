"""Resume a fixed, user-authorized recruiter introduction and track later replies.

Only the exact campaign message is sent. Subsequent conversation replies stay drafts.
The existing account, browser, coordinator, rate limits and delivery verifier are reused.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if sys.stdout is None or sys.stderr is None:
    log_path=Path(__file__).resolve().parents[1]/'data/recruiter-worker.log'
    log_path.parent.mkdir(parents=True,exist_ok=True)
    background_log=log_path.open('a',encoding='utf-8',buffering=1)
    sys.stdout=sys.stderr=background_log

from tools.control_client import caller, configured, resource_lease
from tools.linkedin_message_sender import LinkedInMessageSender
from tools.linkedin_messaging_reader import LinkedInMessagingReader, LIST_SCRIPT
from tools.messaging_loop import browser_lease
from tools.messaging_store import ROOT, digest, now
from tools.recruiter_tracker import RecruiterTracker, PROFILE_PATH, SNAPSHOT_PATH, atomic_json
from sabrina_social.availability_support import instance_lock,backup_database
from tools.worker_identity import operation_identity

STATE = ROOT / 'data/recruiter-worker.json'
STOP = ROOT / 'data/recruiter-outreach.pause'


def publish():
    """The lab serves an atomic read-only mirror; Windows owns the messaging DB."""
    key = str(Path.home() / '.ssh/id_ed25519')
    options = ['-i', key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8']
    remote = '/home/ego/bot/Sabrina-windows-sync/data/social/recruiter-tracker.json'
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.run(['scp', *options, str(SNAPSHOT_PATH), 'ego@ego.local:' + remote + '.incoming'],
                   check=True, capture_output=True, timeout=20, creationflags=flags)
    subprocess.run(['ssh', *options, 'ego@ego.local', 'mv -- ' + remote + '.incoming ' + remote],
                   check=True, capture_output=True, timeout=20, creationflags=flags)


def export(tracker, state):
    state['updated'] = now()
    atomic_json(STATE, state)
    atomic_json(SNAPSHOT_PATH, tracker.snapshot(state))
    try:
        from tools.desktop_questions import notify_pending_recruiter_questions
        notify_pending_recruiter_questions(tracker.store, state)
    except Exception as exc:
        print('Desktop question notification pending: ' + type(exc).__name__, flush=True)
    try:
        publish()
    except Exception as exc:
        print('Tracker mirror pending: ' + type(exc).__name__, flush=True)


def initialize(scope):
    tracker = RecruiterTracker()
    try:
        campaign = tracker.campaign(scope['id'], scope['account_id'], scope['exact_text'], scope['user_instruction'])
        if not PROFILE_PATH.exists():
            atomic_json(PROFILE_PATH, {'version': 1, 'facts': [
                {'id': 'job-search-status-2026-11-13', 'text': 'I am looking for work', 'approved': True,
                 'source': 'Exact user instruction in this chat', 'approved_at': now()}],
                'preferences': {}, 'availability': None})
        for row in tracker.db.execute('SELECT snapshot FROM threads WHERE account=?', (scope['account_id'],)).fetchall():
            snapshot = tracker.observe(json.loads(row['snapshot']))
            tracker.queue(campaign['id'], snapshot)
        export(tracker, {'state': 'ready', 'detail': 'Exact introduction authorized; recruiter discovery and delivery pending',
                         'campaign_id': campaign['id'], 'next_epoch': 0, 'coverage': 'Inbox discovery in progress'})
        return campaign
    finally:
        tracker.store.close()


def select_folder(reader, folder):
    """Use the observed LinkedIn folder controls, excluding the Spam folder."""
    page=reader.page
    if '/messaging/' not in page.url:
        page.goto('https://www.linkedin.com/messaging/',wait_until='domcontentloaded',timeout=30000)
    trigger=page.locator('button[data-test-messaging-inbox-filters__folder-pill]')
    trigger.wait_for(state='visible',timeout=10000)
    if trigger.inner_text().strip().lower()!=folder:
        trigger.click(timeout=5000)
        option=page.locator('[data-test-messaging-inbox-filters__folder-dropdown="'+folder+'"]')
        option.wait_for(state='visible',timeout=5000)
        option.click(timeout=5000)
        page.wait_for_timeout(800)
    reader._guard()


def discover(reader,tracker,campaign,state,max_reads):
    """Walk each virtualized inbox incrementally; DOM indices are never durable IDs."""
    folders=['focused','other','archived']
    folder=folders[int(state.get('folder_index',0))%len(folders)]
    select_folder(reader,folder)
    pane=reader.page.locator('.msg-conversations-container__conversations-list')
    if pane.count()!=1:
        raise RuntimeError('Inbox scroll container is ambiguous')
    progress=state.setdefault('folders',{}).setdefault(folder,{'cursor':0,'end_reached':False})
    pane.evaluate('(e,y)=>e.scrollTop=y',progress['cursor'])
    reader.page.wait_for_timeout(700)
    items=reader.page.evaluate(LIST_SCRIPT)
    if not items:
        # An empty or changed layout is not evidence that all conversations were scanned.
        progress['note']='No conversation rows visible; verify this folder in LinkedIn'
    with tracker.db:
        for item in items:
            tracker.db.execute('INSERT OR IGNORE INTO recruiter_inventory(fingerprint,name,preview,updated) VALUES(?,?,?,?)',
                               (digest([item['name'],item['preview']]),item['name'],item['preview'],now()))
    reads=0
    for item in items:
        key=digest([item['name'],item['preview']])
        previous=tracker.db.execute('SELECT state FROM recruiter_inventory WHERE fingerprint=?',(key,)).fetchone()
        if previous['state']!='unread' or reads>=max_reads:
            continue
        reads+=1
        state.update(state='reading',detail='Reading '+item['name']+' ('+folder+' inbox)')
        export(tracker,state)
        try:
            fresh=reader.read_thread(item)
            if fresh['account_id']!=campaign['account']:
                raise ValueError('Mailbox account differs from the explicitly authorized account')
            fresh=tracker.observe(fresh)
            tracker.queue(campaign['id'],fresh)
            with tracker.db:
                tracker.db.execute("UPDATE recruiter_inventory SET state='read',thread=?,updated=? WHERE fingerprint=?",(fresh['thread_id'],now(),key))
        except Exception as exc:
            with tracker.db:
                tracker.db.execute("UPDATE recruiter_inventory SET state='needs_review',error=?,updated=? WHERE fingerprint=?",(str(exc),now(),key))
            print(item['name']+': '+str(exc),flush=True)
    pending=any(tracker.db.execute("SELECT state FROM recruiter_inventory WHERE fingerprint=?",(digest([i['name'],i['preview']]),)).fetchone()['state']=='unread' for i in items)
    if not pending and items:
        metrics=pane.evaluate('e=>({top:e.scrollTop,height:e.scrollHeight,view:e.clientHeight})')
        if metrics['top']+metrics['view']>=metrics['height']-2:
            before=[(i['name'],i['preview']) for i in items]
            pane.evaluate('e=>e.scrollTop=e.scrollHeight');reader.page.wait_for_timeout(1000)
            after=[(i['name'],i['preview']) for i in reader.page.evaluate(LIST_SCRIPT)]
            progress['end_reached']=before==after
            progress['cursor']=0 if progress['end_reached'] else metrics['top']
        else:
            progress['cursor']=metrics['top']+max(300,int(metrics['view']*.8))
    state['coverage']='; '.join(f+(': inbox end reached' if state.get('folders',{}).get(f,{}).get('end_reached') else ': discovery in progress') for f in folders)
    state['folder_index']=(folders.index(folder)+1)%len(folders)
    state['next_discovery_epoch']=time.time()+180
    reader.manager.rate_limiter.record_action('inbox_check',{'workflow':'recruiter_tracker','folder':folder,'read':reads})


def _tick(max_reads=8, max_sends=3, force=False):
    state = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    if not force and time.time() < state.get('next_epoch', 0):
        return state
    tracker = RecruiterTracker()
    reader = LinkedInMessagingReader('http://127.0.0.1:9222')
    backup_database(tracker.store.path,keep=7)
    state.update({'updated': now(), 'pid': os.getpid(), 'state': 'checking', 'detail': 'Checking saved recruiter campaign'})
    try:
        campaigns = tracker.db.execute('SELECT * FROM recruiter_campaigns WHERE active=1 ORDER BY created').fetchall()
        if not campaigns:
            return state
        campaign = dict(campaigns[0])
        state['campaign_id'] = campaign['id']
        if STOP.exists():
            state.update(state='paused', detail='Paused by operator; no browser actions')
            return state
        hour = dt.datetime.now().hour
        if not 8 <= hour < 21:
            next_time = dt.datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
            if hour >= 21:
                next_time += dt.timedelta(days=1)
            state.update(state='waiting_for_operating_hours', detail='Resumes at 08:00 in the existing Windows session', next_epoch=next_time.timestamp())
            return state
        if configured() is None:
            raise RuntimeError('Coordinator configuration is required; refusing uncoordinated browser access')
        with caller('recruiter-campaign', 'social'), operation_identity('recruiter-campaign:'+campaign['id']), resource_lease('browser:linkedin-primary'), browser_lease():
            try:
                reader.connect()
                # Read before sending, including older threads whose owner already replied.
                # Campaign scope explicitly authorizes one fresh introduction to each recruiter.
                quota, quota_reason = reader.manager.rate_limiter.check_quota('inbox_check')
                if quota and (force or time.time() >= state.get('next_discovery_epoch', 0)):
                    state.update(state='discovering', detail='Discovering recruiter conversations in the current inbox')
                    export(tracker, state)
                    discover(reader,tracker,campaign,state,max_reads)
                sender = LinkedInMessageSender(reader, tracker.store, tracker)
                sent = 0
                for entry in tracker.db.execute("SELECT * FROM recruiter_actions WHERE campaign=? AND status='authorized' ORDER BY id", (campaign['id'],)).fetchall():
                    if sent >= max_sends or STOP.exists():
                        break
                    allowed, reason = reader.manager.rate_limiter.check_quota('direct_message')
                    if not allowed:
                        state.update(state='waiting_for_daily_quota', detail=reason)
                        break
                    state.update(state='sending', detail='Sending approved introduction to ' + tracker.store.snapshot(entry['account'], entry['thread'])['peer_name'])
                    export(tracker, state)
                    try:
                        saved=tracker.store.snapshot(entry['account'],entry['thread'])
                        fresh=tracker.observe(reader.read_saved_thread(saved))
                        tracker.prepare(entry['id'],fresh)
                        reader.manager.rate_limiter.wait_jitter('direct_message')
                        receipt = sender.send(entry['id'])
                        print(json.dumps({'action_id': entry['id'], **receipt}, ensure_ascii=True), flush=True)
                        sent += 1
                    except Exception as exc:
                        tracker.block(entry['id'], str(exc))
                        print('Send held for review: ' + str(exc), flush=True)
                    # Uncertain messages are never retried; other independent recipients remain eligible.
                # Refresh a bounded group of contacted recruiters even while delivery is rate limited.
                # Later replies are intentionally left to recruiter_review's shared
                # graph-aware Llama/Qwen/Granite pipeline.  Keeping a second
                # single-model assessor here would create two competing sources
                # of truth for the same conversation.
                for contact in tracker.db.execute("SELECT c.* FROM recruiter_contacts c JOIN recruiter_actions a ON c.account=a.account AND c.thread=a.thread WHERE a.status='sent' ORDER BY c.observed LIMIT 2").fetchall():
                    try:
                        saved = tracker.store.snapshot(contact['account'], contact['thread'])
                        fresh = tracker.observe(reader.read_saved_thread(saved))
                        if fresh['complete'] and fresh['messages'][-1]['ownership'] == 'external':
                            print('Later recruiter reply queued for the graph-aware scheduled reviewer: '+fresh['thread_id'], flush=True)
                    except Exception as exc:
                        print('Reply review deferred: ' + str(exc), flush=True)
                if state['state'] != 'waiting_for_daily_quota':
                    state.update(state='monitoring', detail=f'Pass completed; {sent} introduction(s) attempted. Later replies remain drafts for review.')
                state['last_success'] = now()
            finally:
                reader.disconnect()
        state['next_epoch'] = time.time() + 180
    except Exception as exc:
        # Browser lease exits normally only after disconnect; uncertain writes stay quarantined in the outbox.
        state.update(state='blocked', detail=str(exc), next_epoch=time.time() + 300)
        print('Recruiter worker deferred: ' + str(exc), flush=True)
    finally:
        if state.get('next_epoch', 0) <= time.time():
            state['next_epoch'] = time.time() + 180
        state['next_attempt'] = dt.datetime.fromtimestamp(state['next_epoch']).astimezone().isoformat()
        export(tracker, state)
        tracker.store.close()
    return state


def tick(max_reads=8,max_sends=3,force=False):
    try:
        with instance_lock(ROOT/'data/recruiter-worker.lock'):
            return _tick(max_reads,max_sends,force)
    except RuntimeError as exc:
        if str(exc)=='availability controller already running':
            return {'state':'already_running'}
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'tick', 'status', 'pause'])
    parser.add_argument('--scope')
    parser.add_argument('--max-reads', type=int, default=8)
    parser.add_argument('--max-sends', type=int, default=3)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    if args.action == 'init':
        print(json.dumps(initialize(json.loads(Path(args.scope).read_text(encoding='utf-8-sig')))))
    elif args.action == 'tick':
        print(json.dumps(tick(max(1, min(20, args.max_reads)), max(0, min(5, args.max_sends)), args.force)))
    elif args.action == 'pause':
        STOP.write_text(now(), encoding='utf-8')
        print('Paused recruiter outreach')
    else:
        print(STATE.read_text(encoding='utf-8') if STATE.exists() else 'No campaign initialized')


if __name__ == '__main__':
    main()
