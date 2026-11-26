"""Bounded, draft-only Sabrina messaging loop. No send or booking tool is exposed."""
import argparse
import html
import json
import os
import time
import uuid
import threading
from contextlib import contextmanager
from pathlib import Path
from tools.messaging_store import ConversationStore,ROOT,now
from tools.messaging_skills import MessagingAssessor,load_profile
from tools.linkedin_messaging_reader import LinkedInMessagingReader

@contextmanager
def browser_lease(path=None):
    lock_path=Path(path or ROOT/'data/messaging_browser.lock')
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    handle=open(lock_path,'a+b')
    try:
        handle.seek(0)
        if os.name=='nt':
            import msvcrt
            if lock_path.stat().st_size==0:handle.write(b'0');handle.flush()
            handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except (OSError,BlockingIOError):
        handle.close();raise RuntimeError('Another messaging pass owns the browser')
    try:yield
    finally:
        if os.name=='nt':
            handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            fcntl.flock(handle,fcntl.LOCK_UN)
        handle.close()


def export_review(store,output_dir,run):
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    rows=store.review(run.get('account_id'))
    deliveries=[]
    if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_outbox'").fetchone():
        query="SELECT * FROM message_outbox WHERE status IN ('sent','uncertain','sending')"
        params=()
        if run.get('account_id'):query+=' AND account=?';params=(run['account_id'],)
        for row in store.db.execute(query+' ORDER BY id DESC',params):
            conversation=store.snapshot(row['account'],row['thread'])
            deliveries.append({'contact':conversation['peer_name'],'status':row['status'],'text':row['text'],
                               'thread_url':conversation['url'],'updated':row['updated'],
                               'receipt':json.loads(row['receipt']) if row['receipt'] else None})
    payload={'mode':'review_only','sending_enabled':False,'booking_enabled':False,'run':run,'items':rows,'deliveries':deliveries}
    (output/'review.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    esc=lambda value:html.escape(str(value))
    cards=[]
    for row in rows:
        conversation,decision=row['conversation'],row['decision']
        incoming=next((m for m in reversed(conversation['messages']) if m['ownership']=='external'),{})
        messages=''.join('<p><b>'+esc(m['author'] or m['ownership'])+'</b>: '+esc(m['text']).replace('\n','<br>')+'</p>' for m in conversation['messages'])
        cards.append('<article><h2>'+esc(conversation['peer_name'])+' <small>'+esc(row['status'])+'</small></h2>'
                     '<p><a href="'+esc(conversation['url'])+'">Open LinkedIn conversation</a></p>'
                     '<h3>Latest incoming message</h3><p>'+esc(incoming.get('text',''))+'</p>'
                     '<h3>Assessment</h3><p>'+esc(decision['reason'])+'</p>'
                     '<h3>Draft for your review</h3><pre>'+esc(decision.get('draft') or '(No reply drafted)')+'</pre>'
                     '<p><b>Missing information:</b> '+esc(', '.join(decision.get('missing_information',[])))+'</p>'
                     '<p><b>Next action:</b> '+esc(decision['next_action'])+'</p>'
                     '<details><summary>Captured conversation</summary>'+messages+'</details></article>')
    document='''<!doctype html><html><head><meta charset="utf-8"><title>Sabrina recruiter review</title>
<style>body{font:16px system-ui;max-width:960px;margin:40px auto;padding:0 24px;background:#f5f6fa;color:#172033}article{background:white;padding:24px;margin:24px 0;border-radius:12px;border:1px solid #dce1ec}small{font-size:14px;color:#566}pre{white-space:pre-wrap;font:inherit;background:#edf3ff;padding:16px}p{line-height:1.6}a{color:#2459a6}</style></head><body>
<h1>Sabrina recruiter review</h1><p>The review loop prepares drafts. Explicitly authorized messages appear in the delivery log below. Interview booking is unavailable. Missing career facts remain unknown.</p>'''
    document+='<p>Run status: '+esc(run.get('status','running'))+' · Updated '+esc(now())+'</p>'
    if deliveries:
        document+='<h2>Delivery log</h2>'
        for delivery in deliveries:
            document+='<article><h3>'+esc(delivery['contact'])+' — '+esc(delivery['status'])+'</h3><pre>'+esc(delivery['text'])+'</pre><p><a href="'+esc(delivery['thread_url'])+'">Open conversation</a></p></article>'
    document+='<h2>Current review items</h2>'
    document+=''.join(cards) or '<p>No review items yet.</p>'
    document+='</body></html>'
    (output/'review.html').write_text(document,encoding='utf-8')
    return str((output/'review.html').resolve())


def review_pass(reader,store,assessor,max_threads=4,account_id=None,progress=None,stop_requested=None):
    result={'read':0,'new_decisions':0,'unchanged':0,'errors':[],'account_id':account_id}
    for item in reader.list_threads(max_threads):
        assessor.refresh_profile()
        if stop_requested and stop_requested():break
        if progress:progress('Reading '+item['name'])
        try:
            snapshot=reader.read_thread(item)
            if result['account_id'] and snapshot['account_id']!=result['account_id']:
                raise RuntimeError('Account changed; refusing to mix mailbox state')
            result['account_id']=snapshot['account_id']
            version,changed=store.observe(snapshot)
            result['read']+=1
            if store.has_decision(snapshot['account_id'],snapshot['thread_id'],version,assessor.profile_hash):
                result['unchanged']+=1
                if progress:progress(item['name']+': unchanged; existing review retained')
                continue
            snapshot['version']=version
            if progress:progress(item['name']+': assessing full conversation with lab model')
            decision=assessor.assess(snapshot)
            # Re-read immutable identity and visible events after model latency before accepting the draft.
            fresh=reader.read_thread(item)
            if fresh['account_id']!=snapshot['account_id'] or fresh['thread_id']!=snapshot['thread_id']:
                raise RuntimeError('Account/thread changed while model was drafting')
            fresh_version,_=store.observe(fresh)
            if fresh_version!=version:
                result['errors'].append({'contact':item['name'],'error':'Conversation changed during drafting; stale draft discarded'})
                continue
            store.save_decision(snapshot['account_id'],snapshot['thread_id'],version,assessor.profile_hash,decision)
            result['new_decisions']+=1
            if progress:progress(item['name']+': '+decision['action']+' saved for review')
        except Exception as error:
            result['errors'].append({'contact':item['name'],'error':str(error)})
            if progress:progress(item['name']+': '+str(error))
    return result


def run_loop(cycles=3,interval=20,max_threads=4,cdp_url='http://localhost:9222',profile_path=None,
             db_path=None,output_dir=None,test_override=False,stop_event=None,stop_path=None):
    cycles=max(1,min(int(cycles),100));interval=max(1,min(float(interval),3600))
    max_threads=max(1,min(int(max_threads),50))
    stop_event=stop_event or threading.Event()
    store=ConversationStore(db_path)
    output_dir=Path(output_dir or ROOT/'artifacts/messaging')
    reader=LinkedInMessagingReader(cdp_url,test_override)
    stop_file=Path(stop_path or ROOT/'data/messaging_loop.stop')
    run={'id':str(uuid.uuid4()),'pid':os.getpid(),'started':now(),'status':'starting','mode':'review_only','cycles':[],
         'test_override_cooldown':test_override,'account_id':None}
    output_dir.mkdir(parents=True,exist_ok=True)
    def progress(message):
        line=now()+' '+message
        print(line,flush=True)
        with (output_dir/'loop.log').open('a',encoding='utf-8') as f:f.write(line+'\n')
        run['last_progress']=message;run['updated']=now()
        temporary=output_dir/'run.json.tmp'
        temporary.write_text(json.dumps(run,indent=2),encoding='utf-8')
        temporary.replace(output_dir/'run.json')
    try:
        with browser_lease():
            stop_file.unlink(missing_ok=True)
            reader.connect()
            assessor=MessagingAssessor(lambda system,prompt:reader.manager._generate_llm_response(system,prompt,max_tokens=1800),load_profile(profile_path),profile_loader=lambda:load_profile(profile_path))
            run['status']='running'
            for cycle in range(cycles):
                if stop_file.exists():stop_event.set()
                if stop_event.is_set():break
                progress(f'Cycle {cycle+1}/{cycles}: full-thread recruiter review; sending disabled')
                result=review_pass(reader,store,assessor,max_threads,run['account_id'],progress,lambda:stop_file.exists() or stop_event.is_set())
                if stop_file.exists():stop_event.set()
                run['account_id']=result['account_id'];run['cycles'].append(result)
                export_review(store,output_dir,run)
                (output_dir/'run.json').write_text(json.dumps(run,indent=2),encoding='utf-8')
                if result['read']==0 and result['errors']:raise RuntimeError('No conversations could be read reliably')
                if cycle+1<cycles:
                    progress(f'Waiting {interval:g}s for next pass; no local model or send worker')
                    deadline=time.monotonic()+interval
                    while time.monotonic()<deadline and not stop_event.is_set():
                        if stop_file.exists():stop_event.set();break
                        stop_event.wait(min(1, max(0,deadline-time.monotonic())))
                    if stop_event.is_set():break
            run['status']='stopped' if stop_event.is_set() else ('complete_with_errors' if any(c['errors'] for c in run['cycles']) else 'complete')
    except KeyboardInterrupt:
        run['status']='stopped'
    except Exception as error:
        run['status']='blocked';run['error']=str(error);progress('Blocked: '+str(error))
    finally:
        run['finished']=now()
        run['review_path']=export_review(store,output_dir,run)
        (output_dir/'run.json').write_text(json.dumps(run,indent=2),encoding='utf-8')
        reader.disconnect();store.close()
    return run


def main():
    parser=argparse.ArgumentParser(description='Sabrina recruiter conversation loop (review only; no send capability)')
    controls=parser.add_mutually_exclusive_group()
    controls.add_argument('--stop',action='store_true',help='Request a clean stop after the current bounded operation')
    controls.add_argument('--status',action='store_true',help='Read the current or most recent loop status')
    parser.add_argument('--cycles',type=int,default=3)
    parser.add_argument('--interval',type=float,default=20)
    parser.add_argument('--max-threads',type=int,default=4)
    parser.add_argument('--cdp-url',default='http://localhost:9222')
    parser.add_argument('--profile')
    parser.add_argument('--test-override-cooldown',action='store_true',help='Authorized test only: ignore prior cooldown/hours, retain fresh challenge checks')
    args=parser.parse_args()
    if args.stop:
        path=ROOT/'data/messaging_loop.stop';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(now(),encoding='utf-8')
        print('Stop requested; current bounded thread review will finish without sending anything.');return 0
    if args.status:
        path=ROOT/'artifacts/messaging/run.json'
        print(path.read_text(encoding='utf-8') if path.exists() else 'No messaging loop has run.');return 0
    result=run_loop(args.cycles,args.interval,args.max_threads,args.cdp_url,args.profile,test_override=args.test_override_cooldown)
    print(json.dumps(result,indent=2),flush=True)
    return 0 if result['status'] in ('complete','stopped') else 1

if __name__=='__main__':raise SystemExit(main())
