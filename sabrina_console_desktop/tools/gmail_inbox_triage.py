"""Track recent work mail beyond the one authorized customer conversation."""
from __future__ import annotations

import hashlib
import base64
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from tools.desktop_questions import notify_question, withdraw_question
from tools.gmail_customer_reader import GmailCustomerReader
from tools.messaging_loop import browser_lease
from tools.messaging_store import ROOT

DB_PATH = ROOT / 'data/gmail_inbox_triage.sqlite3'
INBOX_URL = 'https://mail.google.com/mail/u/0/#inbox'
JOB_ALERT_QUERY = 'newer_than:21d from:jobalerts-noreply@linkedin.com subject:agentic'
WORK_SUBJECT = re.compile(
    r'\b(?:recruiter|recruiting|interview|job offer|employment offer|'
    r'right to represent|\brtr\b|resume|résumé|candidate|submission|'
    r'penetration tester|security engineer|application status|'
    r'(?:agentic|generative ai|genai|llm|machine learning|\bai\b).{0,45}'
    r'(?:engineer|architect|developer|career|role|position|opportunity)|'
    r'(?:engineer|architect|developer).{0,45}(?:agentic|generative ai|genai|llm))\b', re.I)
RECEIPT_SUBJECT = re.compile(
    r'(?:application (?:was sent|received|submitted)|thanks? for applying|'
    r'thank you for (?:applying|your application))', re.I)
AUTOMATED_SENDER = re.compile(r'^(?:no[._-]?reply|jobs[._-]?noreply|notifications?)@', re.I)
LINKEDIN_MIRROR = {'hit-reply@linkedin.com', 'inmail-hit-reply@linkedin.com'}
JOB_ALERT_SENDER = 'jobalerts-noreply@linkedin.com'
AGENTIC_ROLE_SUBJECT = re.compile(r'\bagentic\b.{0,65}\b(?:engineer|architect|developer|career|role)\b|\b(?:engineer|architect|developer)\b.{0,65}\bagentic\b',re.I)
MARKETING_SUBJECT = re.compile(r'\b(?:last reminder|resume tips|career advice|job alerts?|update your resume)\b', re.I)


def classify(row, managed_addresses=()):
    sender = row['sender'].lower().strip()
    subject = row['subject'].strip()
    if sender in managed_addresses:
        return 'covered_by_customer_job'
    if sender in LINKEDIN_MIRROR:
        return 'covered_by_linkedin'
    if RECEIPT_SUBJECT.search(subject):
        return 'application_receipt'
    if sender==JOB_ALERT_SENDER and AGENTIC_ROLE_SUBJECT.search(subject):
        return 'job_lead'
    if MARKETING_SUBJECT.search(subject):
        return 'marketing'
    if AUTOMATED_SENDER.search(sender):
        return 'automated_notice'
    if WORK_SUBJECT.search(subject):
        return 'needs_review'
    return 'other_mail'


def gmail_thread_id(jslog):
    """Decode Gmail's list-row thread reference, never its untrusted action data."""
    match=re.search(r'\b1:([A-Za-z0-9_-]+)',jslog or '')
    if not match or len(match.group(1))>4096:
        return ''
    try:
        decoded=base64.urlsafe_b64decode(match.group(1)+'='*(-len(match.group(1))%4))
        value=json.loads(decoded)[0]
    except (ValueError,IndexError,TypeError):
        return ''
    return value if isinstance(value,str) and re.fullmatch(r'#thread-f:\d+',value) else ''


def collect_rows(page, *, max_pages=3):
    """Read Gmail list metadata only; do not open, mark read, or send mail."""
    page.goto(INBOX_URL, wait_until='domcontentloaded', timeout=30000)
    page.locator('tr.zA:visible').first.wait_for(state='visible', timeout=30000)
    return read_visible_rows(page,max_pages=max_pages)


def read_visible_rows(page, *, max_pages=3):
    result=[]
    for number in range(max_pages):
        rows=page.locator('tr.zA:visible').evaluate_all('''nodes=>nodes.map(n=>({
          thread_id:n.getAttribute('data-thread-id')||n.getAttribute('data-legacy-thread-id')||'',
          jslog:n.getAttribute('jslog')||'',
          sender:(n.querySelector('.yP,.zF,.yW')?.getAttribute('email')||
                  n.querySelector('.yP,.zF,.yW')?.textContent||'').trim(),
          subject:(n.querySelector('.bog')?.textContent||'').trim(),
          date:(n.querySelector('.xW')?.textContent||'').trim(),
          unread:n.classList.contains('zE')}))''')
        for row in rows:
            row['thread_id']=row['thread_id'] or gmail_thread_id(row.pop('jslog',''))
            if row['sender'] and row['subject']:
                result.append(row)
        older=page.locator('[aria-label="Older"], [data-tooltip="Older"]').first
        if number+1>=max_pages or older.count()!=1 or not older.is_enabled():
            break
        first=rows[0]['sender']+'|'+rows[0]['subject'] if rows else ''
        older.click()
        page.wait_for_function('id=>{let row=document.querySelector("tr.zA");return row&&'
                               '(row.querySelector(".yP,.zF,.yW")?.getAttribute("email")||'
                               'row.querySelector(".yP,.zF,.yW")?.textContent||"").trim()+"|"+'
                               '(row.querySelector(".bog")?.textContent||"").trim()!==id}',
                               arg=first, timeout=15000)
    return result


def collect_agentic_job_alerts(page):
    """Search all mail for recent agentic job alerts beyond inbox pagination."""
    page.goto('https://mail.google.com/mail/u/0/#search/'+quote(JOB_ALERT_QUERY,safe=''),
              wait_until='domcontentloaded',timeout=30000)
    # Reload clears the old inbox list before Gmail's hash-based search hydrates.
    page.reload(wait_until='domcontentloaded',timeout=30000)
    page.wait_for_function('q=>document.querySelector("input[name=q]")?.value.trim()===q',
                           arg=JOB_ALERT_QUERY,timeout=30000)
    page.wait_for_function('()=>document.querySelector("tr.zA") || '
                           'document.body.innerText.includes("No conversations found")',timeout=45000)
    if not page.locator('tr.zA:visible').count():
        return []
    return read_visible_rows(page,max_pages=1)


def job_lead_key(subject):
    base=re.sub(r'\band\s+\d+\s+other jobs.*','',subject.lower())
    ignored={'a','at','be','for','is','hiring','may','role','the','you'}
    return ' '.join(sorted(set(re.findall(r'[a-z0-9]+',base))-ignored))


def merge_rows(inbox_rows, alert_rows):
    """Keep the newest list entry for a thread or repeated job alert."""
    seen_threads=set();seen_leads=set();merged=[]
    for row in [*inbox_rows,*alert_rows]:
        thread=row.get('thread_id') or (row['sender'].lower(),row['subject'].lower())
        if thread in seen_threads:
            continue
        seen_threads.add(thread)
        if classify(row)=='job_lead':
            lead=job_lead_key(row['subject'])
            if lead in seen_leads:
                continue
            seen_leads.add(lead)
        merged.append(row)
    return merged


def inspect_work_threads(reader, rows, policy, *, max_threads=5):
    """Read complete work threads for reply ownership without downloading files."""
    for row in (item for item in rows if classify(item,{c['email'].lower() for c in policy['contacts']})=='needs_review'):
        if max_threads<=0:
            break
        max_threads-=1
        reader.policy={**policy,'contacts':[{'id':'inbox-review','name':row['sender'],
                     'email':row['sender'],'subject_allowlist':[row['subject']]}]}
        try:
            snapshot=reader.open_allowlisted_thread('inbox-review')
            latest=snapshot['messages'][-1]
            row['latest_owner']=latest['ownership']
            row['latest_message_id']=latest['id']
            row['latest_text']=latest.get('body_text','')[:800]
            row['complete']=snapshot['complete']
        except Exception as error:
            row['review_error']=str(error)[:160]
    reader.policy=policy
    return rows


def record_and_notify(rows, *, db_path=DB_PATH, notify=notify_question,
                      withdraw=withdraw_question, managed_addresses=(), limit=4):
    target=Path(db_path)
    target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(target,timeout=10) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS work_mail (
            id TEXT PRIMARY KEY, sender TEXT, subject TEXT, date_text TEXT,
            category TEXT, revision TEXT, notified_revision TEXT, last_seen REAL,
            last_message TEXT)''')
        if 'last_message' not in {row[1] for row in db.execute('PRAGMA table_info(work_mail)')}:
            db.execute('ALTER TABLE work_mail ADD COLUMN last_message TEXT')
        start=time.time();used_legacy=set()
        for position,row in enumerate(rows):
            category=classify(row, managed_addresses)
            if category=='needs_review' and row.get('latest_owner')=='self':
                category='awaiting_contact'
            fallback=hashlib.sha256((row['sender'].lower()+'|'+row['subject'].lower()).encode()).hexdigest()
            stable=hashlib.sha256(row['thread_id'].encode()).hexdigest() if row.get('thread_id') else fallback
            legacy=db.execute('SELECT 1 FROM work_mail WHERE id=?',(fallback,)).fetchone()
            key=fallback if legacy and fallback not in used_legacy else stable
            used_legacy.add(key)
            old=db.execute('SELECT category FROM work_mail WHERE id=?',(key,)).fetchone()
            if old and old[0] in {'needs_review','job_lead'} and category not in {'needs_review','job_lead'}:
                withdraw({'account_id':'gmail:work-inbox','thread_id':key})
            revision=hashlib.sha256(json.dumps([row['sender'],row['subject'],row['date'],
                row.get('latest_message_id'),row.get('latest_text'),row.get('review_error')],ensure_ascii=False).encode()).hexdigest()
            db.execute('''INSERT INTO work_mail VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET sender=excluded.sender,subject=excluded.subject,
                date_text=excluded.date_text,category=excluded.category,revision=excluded.revision,
                last_seen=excluded.last_seen,last_message=excluded.last_message''',
                (key,row['sender'],row['subject'],row['date'],category,revision,None,start-position*.001,
                 row.get('latest_text','')[:800]))
        pending=db.execute('''SELECT id,sender,subject,date_text,category,revision,last_message FROM work_mail
            WHERE category IN ('needs_review','job_lead') AND (notified_revision IS NULL OR notified_revision!=revision)
            ORDER BY last_seen DESC LIMIT ?''',(limit,)).fetchall()
        notices=[]
        for key,sender,subject,date,category,revision,last_message in pending:
            snapshot={'account_id':'gmail:work-inbox','thread_id':key,'peer_name':sender,
                      'url':'https://mail.google.com/mail/u/0/#search/'+quote('subject:"'+subject.replace('"','')+'"',safe=''),
                      'messages':[{'id':revision}]}
            excerpt=' '.join((last_message or '').split())[:140]
            if category=='job_lead':
                decision={'action':'ask_human','missing_information':[f'Review agentic engineering job lead: {subject}'],
                          'reason':f'LinkedIn job alert from {date}.',
                          'next_action':'Open the listing and decide whether to apply; this is not a recruiter reply.'}
            else:
                decision={'action':'ask_human','missing_information':[f'Review work email: {subject}'],
                          'reason':f'From {sender} ({date}). '+(excerpt or 'Sabrina is tracking this conversation.'),
                          'next_action':'Review the email in Gmail before a reply is prepared.'}
            receipt=notify(snapshot,decision)
            notices.append(receipt)
            if receipt.get('status') in {'shown','already_notified'}:
                db.execute('UPDATE work_mail SET notified_revision=? WHERE id=?',(revision,key))
        db.commit()
        counts=dict(db.execute('SELECT category,count(*) FROM work_mail GROUP BY category'))
    return {'scanned':len(rows),'categories':counts,'notices':notices}


def _run_inbox_triage_pass(max_pages):
    from tools.customer_engagement import load_customer_policy
    policy=load_customer_policy()
    managed={str(c['email']).lower() for c in policy['contacts']}
    class WorkReader(GmailCustomerReader):
        def _ingest_external_attachments(self, raw_messages, contact):
            # Unknown work mail can require review, but attachments are not
            # automatically downloaded by this read-only inbox job.
            return None

    reader=WorkReader(policy=policy)
    try:
        with browser_lease(ROOT/'data/customer_email_browser.lock'):
            reader.connect()
            inbox_rows=collect_rows(reader.page,max_pages=max_pages)
            alert_rows=collect_agentic_job_alerts(reader.page)
            rows=merge_rows(inbox_rows,alert_rows)
            rows=inspect_work_threads(reader,rows,policy)
    except Exception as error:
        return {'status':'retry','error':str(error)[:300], 'retry_after_seconds':60}
    finally:
        reader.disconnect()
    result=record_and_notify(rows,managed_addresses=managed)
    return {'status':'no_reply',**result}


def run_inbox_triage(job):
    """Keep the Gmail scan in a bounded child so a failed pass releases its lock."""
    pages=int(job['context'].get('max_pages',3))
    if not 1<=pages<=5:
        raise ValueError('Inbox scan page limit must be between one and five')
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    child=subprocess.Popen([sys.executable,'-m','tools.gmail_inbox_triage','--child',str(pages)],
                           cwd=str(ROOT),stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                           text=True,encoding='utf-8',errors='replace',creationflags=flags)
    try:
        stdout,stderr=child.communicate(timeout=150)
    except subprocess.TimeoutExpired:
        child.kill();stdout,stderr=child.communicate()
        outcome={'status':'retry','error':'Inbox scan exceeded its bounded browser window'}
    else:
        try:
            outcome=json.loads(stdout.strip().splitlines()[-1]) if child.returncode==0 else {
                'status':'retry','error':stderr.strip()[-300:] or 'Inbox scan failed'}
        except (ValueError,IndexError):
            outcome={'status':'retry','error':'Inbox scan returned no valid result'}
    return outcome


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--child',type=int,required=True)
    args=parser.parse_args()
    print(json.dumps(_run_inbox_triage_pass(args.child),ensure_ascii=False))
