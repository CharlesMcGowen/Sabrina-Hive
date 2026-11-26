"""Deduplicated Windows questions; desktop notices never grant send authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
APP_ID = 'Sabrina.Questions'
DASHBOARD = 'http://127.0.0.1:8087/recruiters'
CONFIG = ROOT / 'config/desktop_questions.json'
LEDGER = ROOT / 'data/desktop_questions.sqlite3'


def settings():
    return json.loads(CONFIG.read_text(encoding='utf-8-sig')) if CONFIG.exists() else {'enabled': False}


def question_payload(snapshot, decision):
    name = str(snapshot.get('peer_name') or 'A conversation')[:70]
    missing = decision.get('missing_information') or []
    question = '; '.join(str(x) for x in missing) or decision.get('reason') or 'Please review this request.'
    source = str(snapshot.get('url') or '')
    parsed = urlparse(source)
    gmail = parsed.scheme == 'https' and parsed.hostname == 'mail.google.com' and parsed.path.startswith('/mail/u/0/')
    reason=str(decision.get('reason') or '')
    body=str(question)[:150]+(('\n'+reason[:150]) if reason and reason!=question else '')
    return {'title': name + ' needs your answer', 'body': body[:240],
            'url': source if gmail else DASHBOARD,
            'account': snapshot.get('account_id',''), 'thread': snapshot.get('thread_id',''),
            'notes': {'missing_information':missing,'reason':decision.get('reason',''),
                      'next_action':decision.get('next_action',''),'context_version':snapshot.get('version')}}


def case_id(payload):
    return hashlib.sha256((payload.get('account','')+':'+payload.get('thread','')).encode()).hexdigest()


def initialize_ledger(db):
    db.execute('CREATE TABLE IF NOT EXISTS notices (id TEXT PRIMARY KEY,payload TEXT,status TEXT,updated REAL,error TEXT)')
    db.execute('''CREATE TABLE IF NOT EXISTS questions (id TEXT PRIMARY KEY,scope TEXT,payload TEXT,
                  revision TEXT,status TEXT,updated REAL,error TEXT)''')


def register_shortcut():
    """Give native notifications their own Sabrina identity in Notification Center."""
    import pythoncom
    from win32com.shell import shell
    from win32com.propsys import propsys, pscon
    shortcut = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Sabrina Questions.lnk'
    pythoncom.CoInitialize()
    try:
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
        if shortcut.exists():
            link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(shortcut), 2)  # STGM_READWRITE
            if link.GetArguments()==DASHBOARD:return
        link.SetPath(str(Path(os.environ['WINDIR']) / 'explorer.exe'))
        link.SetArguments(DASHBOARD)
        link.SetDescription('Questions Sabrina needs you to review')
        properties = link.QueryInterface(propsys.IID_IPropertyStore)
        properties.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
        properties.Commit()
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(shortcut), 0)
    finally:
        pythoncom.CoUninitialize()


def send_windows_toast(payload, key):
    if os.name != 'nt': return {'status': 'unsupported_platform'}
    register_shortcut()
    folder = ROOT / 'data/desktop_question_payloads'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (key + '.json')
    tag = case_id(payload)[:16]
    path.write_text(json.dumps({**payload, 'app_id': APP_ID, 'tag': tag}, ensure_ascii=False), encoding='utf-8')
    result = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                             '-File',str(ROOT/'scripts/show_question_toast.ps1'),'-PayloadPath',str(path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:500] or 'Windows notification failed')
    return json.loads(result.stdout.strip())


def notify_question(snapshot, decision, *, ledger_path=LEDGER, sender=send_windows_toast, enabled=None, clock=time.time):
    if enabled is None: enabled = settings().get('enabled', False)
    if not enabled or decision.get('action') != 'ask_human': return {'status': 'disabled_or_no_question'}
    payload = question_payload(snapshot, decision)
    identity = {'account':payload['account'],'thread':payload['thread'],
                'latest':(snapshot.get('messages') or [{}])[-1].get('id',''),
                'question':payload['notes'],'url':payload['url'],'title':payload['title']}
    key = hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
    case=case_id(payload)
    scope='recruiter' if str(payload['account']).startswith('urn:li:') else 'customer_email'
    Path(ledger_path).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(ledger_path,timeout=5) as db:
        initialize_ledger(db)
        db.execute('BEGIN IMMEDIATE')
        active=db.execute('SELECT status FROM questions WHERE id=?',(case,)).fetchone()
        db.execute('INSERT OR REPLACE INTO questions VALUES(?,?,?,?,?,?,?)',
                   (case,scope,json.dumps(payload),key,'pending',clock(),None))
        previous = db.execute('SELECT status,updated FROM notices WHERE id=?',(key,)).fetchone()
        if previous and ((previous[0]=='shown' and active and active[0]=='pending') or
                         (previous[0]!='shown' and clock()-previous[1]<300)):
            db.commit();return {'status':'already_notified','id':key}
        db.execute('INSERT OR REPLACE INTO notices VALUES(?,?,?,?,?)',(key,json.dumps(payload),'sending',clock(),None))
        db.commit()
        try:
            receipt=sender(payload,key)
            status=receipt.get('status','failed')
            error=None if status=='shown' else json.dumps(receipt)
        except Exception as exc:
            status='failed';error=str(exc)[:500]
        db.execute('UPDATE notices SET status=?,updated=?,error=? WHERE id=?',(status,clock(),error,key))
        return {'status':status,'id':key,**({'error':error} if error else {})}


def withdraw_question(snapshot, *, ledger_path=LEDGER, sender=send_windows_toast, enabled=None):
    if enabled is None:enabled=settings().get('enabled',False)
    if not enabled:return {'status':'disabled'}
    payload={'account':snapshot.get('account_id',''),'thread':snapshot.get('thread_id','')}
    case=case_id(payload)
    Path(ledger_path).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(ledger_path,timeout=5) as db:
        initialize_ledger(db)
        row=db.execute("SELECT payload,status,updated,error FROM questions WHERE id=?",(case,)).fetchone()
        if not row or row[1]=='withdrawn':return {'status':'no_current_notice'}
        if row[3] and time.time()-row[2]<300:return {'status':'retry_deferred'}
        try:
            receipt=sender({**json.loads(row[0]),'operation':'remove'},'withdraw-'+case)
            error=None if receipt.get('status')=='removed' else json.dumps(receipt)
        except Exception as exc:error=str(exc)[:500]
        db.execute('UPDATE questions SET status=?,updated=?,error=? WHERE id=?',
                   ('pending' if error else 'withdrawn',time.time(),error,case))
        return {'status':'failed' if error else 'withdrawn'}


def notify_pending_recruiter_questions(store, state, *, notifier=None, profile_hash=None,
                                      ledger_path=LEDGER, sender=send_windows_toast):
    if not settings().get('enabled',False): return []
    if profile_hash is None:
        from tools.messaging_skills import load_profile
        from tools.messaging_store import digest
        from tools.recruiter_conversation_swarm import MODELS,POLICY_VERSION
        profile_hash=digest({'profile':load_profile(),'swarm_policy':POLICY_VERSION,'models':MODELS})
    active=set(state.get('live_focused_threads') or [])
    rows=store.db.execute('''SELECT d.content,t.snapshot,t.version FROM decisions d JOIN threads t
        ON d.account=t.account AND d.thread=t.thread AND d.context_version=t.version
        WHERE d.profile_hash=? ORDER BY d.id DESC''',(profile_hash,)).fetchall()
    results=[];resolved=set();new_notices=0
    for row in rows:
        snapshot=json.loads(row['snapshot'])
        snapshot['version']=row['version']
        if snapshot['thread_id'] in active:
            decision=json.loads(row['content'])
            if decision.get('action')!='ask_human':
                resolved.add(case_id({'account':snapshot['account_id'],'thread':snapshot['thread_id']}))
                continue
            if new_notices<3:
                result=notifier(snapshot,decision) if notifier else notify_question(
                    snapshot,decision,ledger_path=ledger_path,sender=sender)
                results.append(result)
                if result.get('status') not in {'already_notified','disabled_or_no_question'}:new_notices+=1
    # The live decisions remain authoritative. A new note replaces the same
    # conversation's toast. Keep existing questions while a new review is pending;
    # absence of a current decision is not evidence that the question was answered.
    Path(ledger_path).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(ledger_path) as db:
        initialize_ledger(db)
        obsolete=db.execute("SELECT id,payload FROM questions WHERE scope='recruiter' AND status='pending'").fetchall()
    for key,raw in obsolete:
        p=json.loads(raw)
        if key in resolved or p['thread'] not in active:
            withdraw_question({'account_id':p['account'],'thread_id':p['thread']},ledger_path=ledger_path,sender=sender)
    return results


def notify_customer_question(snapshot, decision):
    try:
        return notify_question(snapshot,decision)
    except Exception as exc:
        # A notification failure must never interrupt the email scheduler.
        return {'status':'failed','error':str(exc)[:300]}


def notify_recruiter_review_problem(snapshot, review, **kwargs):
    """Surface a held review without turning rejected model text into a reply."""
    error=review.get('error') or ''
    if review.get('status')!='blocked' or any(marker in error for marker in (
        'Lab wait expired; retained request ', 'Inference wait budget exhausted before submission',
    )):
        return {'status':'no_operator_action'}
    try:
        return notify_question(snapshot,{
            'action':'ask_human','draft':'',
            'missing_information':['Please review the unresolved requests in this conversation.'],
            'reason':'Sabrina could not verify a reply and needs your help reviewing the messages.',
            'next_action':'Review conversation '+str(review.get('id',''))+'; '+error,
        },**kwargs)
    except Exception as exc:
        return {'status':'failed','error':str(exc)[:300]}
