"""Read-only recruiter reviews. This module has no message-sending adapter."""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
import time

from tools.control_client import caller,resource_lease
from tools.worker_identity import operation_identity
from tools.linkedin_messaging_reader import LinkedInMessagingReader
from tools.messaging_loop import browser_lease
from tools.messaging_store import ROOT,now,digest
from tools.messaging_skills import load_profile
from tools.recruiter_tracker import RecruiterTracker
from tools.recruiter_conversation_swarm import RecruiterConversationSwarm,BOARD_PATH,MODELS,POLICY_VERSION
from tools.recruiter_graph import RecruiterGraph
from tools.recruiter_strategy import plan as plan_strategy
from tools.recruiter_autopilot import RecruiterAutopilot
from tools.recruiter_campaign import STATE,export
from sabrina_social.availability_support import instance_lock,backup_database


def read_context(tracker,saved,interactive=False):
    # A user-requested interactive read can run after unattended operating hours.
    # Fresh authentication/challenge checks remain active and the sender rejects this session.
    reader=LinkedInMessagingReader('http://127.0.0.1:9222',read_only_session=interactive)
    failure=None;result=None
    with caller('recruiter-context-reader','social'),operation_identity('read-recruiter:'+saved['thread_id']),resource_lease('browser:linkedin-primary'),browser_lease():
        try:
            reader.connect()
            result=tracker.observe(reader.read_saved_thread(saved))
        except Exception as exc:failure=exc
        finally:reader.disconnect()
    # A completed read-only driver has no abandoned browser write to quarantine.
    if failure:raise failure
    return result


def candidates(tracker,name=None):
    result=[]
    for row in tracker.db.execute('SELECT account,thread,snapshot FROM threads ORDER BY observed DESC'):
        snapshot=json.loads(row['snapshot'])
        if not snapshot['account_id'].startswith('urn:li:fsd_profile:'):continue
        if name and snapshot['peer_name']!=name:continue
        result.append(tracker.store.snapshot(row['account'],row['thread']))
    if not name:
        known={(r['account'],r['thread']) for r in tracker.db.execute("SELECT account,thread FROM recruiter_contacts WHERE classification='recruiter'")}
        result.sort(key=lambda s:0 if (s['account_id'],s['thread_id']) in known else 1)
    return result


def run_review(name=None,max_threads=1,force=False,interactive=False):
    state=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    if not force and time.time()<state.get('next_review_epoch',0):return state
    tracker=RecruiterTracker();swarm=None
    graph=RecruiterGraph(tracker.store)
    result=[]
    try:
        if not interactive and not 8<=dt.datetime.now().hour<21:
            morning=dt.datetime.now().replace(hour=8,minute=0,second=0,microsecond=0)
            if dt.datetime.now().hour>=21:morning+=dt.timedelta(days=1)
            state.update(state='review_only',detail='Recruiter sending is paused. Scheduled read-only reviews resume at 08:00.',
                         outbound_paused=True,next_review_epoch=morning.timestamp())
            return state
        backup_database(tracker.store.path,keep=7)
        if BOARD_PATH.exists():backup_database(BOARD_PATH,keep=7)
        swarm=RecruiterConversationSwarm()
        for saved in candidates(tracker,name):
            # An unchanged completed review is reused; no three-model loop over stale messages.
            tables=swarm.board.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_review_runs'").fetchone()
            if tables and not name:
                prior=swarm.board.conn.execute('SELECT context_version,profile_hash,status,packet FROM conversation_review_runs WHERE account=? AND thread=? ORDER BY created DESC LIMIT 1',(saved['account_id'],saved['thread_id'])).fetchone()
                if (prior and prior['context_version']==saved['version'] and prior['profile_hash']==digest(load_profile())
                        and json.loads(prior['packet']).get('policy_version')==POLICY_VERSION
                        and prior['status'] in {'draft_ready','no_reply','needs_input','blocked'}):continue
            state.update(state='review_only',detail='Reading '+saved['peer_name']+'; all outbound messages remain paused',outbound_paused=True)
            export(tracker,state)
            try:
                fresh=read_context(tracker,saved,interactive)
                prior_deliveries=[dict(r) for r in tracker.db.execute("SELECT id,status,text FROM recruiter_actions WHERE account=? AND thread=? AND status IN ('sending','uncertain')",(fresh['account_id'],fresh['thread_id']))]
                if tracker.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_outbox'").fetchone():
                    prior_deliveries += [dict(r) for r in tracker.db.execute("SELECT id,status,text FROM message_outbox WHERE account=? AND thread=? AND status IN ('sending','uncertain')",(fresh['account_id'],fresh['thread_id']))]
                prior_deliveries += [{**dict(r), 'autonomous': True, 'kind': 'autonomous_clarification'} for r in graph.db.execute(
                    "SELECT id,status,text,created FROM recruiter_graph_autonomy WHERE account=? AND thread=? AND status IN ('shadow','prepared','sent','uncertain')",
                    (fresh['account_id'],fresh['thread_id']))]
                graph_snapshot=graph.observe(fresh,load_profile())
                strategy=plan_strategy(graph_snapshot,fresh,prior_deliveries,
                                       daily_autonomous_count=graph.autonomous_count_today(fresh['account_id']))
                def progress(role,model):
                    state.update(state='review_only',detail=saved['peer_name']+': '+role+' is reading the captured chat and approved profile ('+model+'); sending remains paused')
                    print(state['detail'],flush=True);export(tracker,state)
                swarm.progress=progress
                review=swarm.review(fresh,prior_delivery=prior_deliveries,graph_snapshot=graph_snapshot,strategy=strategy)
                reader_artifact=next((a['parsed'] for a in review['artifacts'] if a['role']=='reader' and a['parsed'] is not None and not a['validation_error']),None)
                if reader_artifact:
                    graph.apply_reader(fresh,reader_artifact,load_profile())
                    classification='recruiter' if reader_artifact['intent'] in {'recruiter_inquiry','scheduling'} else 'not_recruiter'
                    facts=reader_artifact['known_role_facts']
                    evidence={'basis':'Model reading of captured conversation','review_id':review['id'],
                              'source_id':facts[0]['source_id'] if facts else reader_artifact['source_message_ids'][-1],
                              'quote':facts[0]['quote'] if facts else ''}
                    with tracker.db:
                        tracker.db.execute('UPDATE recruiter_contacts SET classification=?,evidence=? WHERE account=? AND thread=?',(classification,json.dumps(evidence),fresh['account_id'],fresh['thread_id']))
                if review['status']=='model_review_complete':
                    reobserved=read_context(tracker,fresh,interactive)
                    review=swarm.commit(review['id'],tracker.store,reobserved)
                    if review.get('decision') and review['status'] in {'draft_ready','no_reply','needs_input'}:
                        graph.apply_strategy(reobserved,strategy,load_profile())
                        current_graph=graph.dossier(reobserved,load_profile())
                        autonomy=RecruiterAutopilot(graph).execute(review['decision'],reobserved,current_graph,prior_delivery=prior_deliveries)
                    else:
                        autonomy={'status':'not_run','reason':'Fresh source verification made the proposal stale'}
                else:
                    autonomy={'status':'not_run','reason':'Model review did not produce a committed decision'}
                result.append({'name':fresh['peer_name'],'review_id':review['id'],'status':review['status'],'decision':review['decision'],'autonomy':autonomy,'error':review['error']})
                print(json.dumps(result[-1],ensure_ascii=True),flush=True)
            except Exception as exc:
                result.append({'name':saved['peer_name'],'status':'blocked','error':str(exc)})
                print(json.dumps(result[-1]),flush=True)
            if len(result)>=max_threads:break
        state.update(state='review_only',detail='Conversation review completed. No messages were sent; outbound remains paused.',
                     outbound_paused=True,last_model_reviews=result,next_review_epoch=time.time()+900,
                     review_models=MODELS,last_review_completed=now())
    finally:
        if swarm:swarm.close()
        export(tracker,state)
        tracker.store.close()
    return state


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['tick','review'])
    p.add_argument('--name')
    p.add_argument('--max-threads',type=int,default=1)
    p.add_argument('--interactive-read',action='store_true',help='Current user-requested read-only review; never permits sending')
    args=p.parse_args()
    try:
        with instance_lock(ROOT/'data/recruiter-worker.lock'):
            print(json.dumps(run_review(args.name,max(1,min(3,args.max_threads)),force=args.action=='review',interactive=args.interactive_read),ensure_ascii=True))
    except RuntimeError as exc:
        if str(exc)=='availability controller already running':print('A recruiter review is already running')
        else:raise


if __name__=='__main__':main()
