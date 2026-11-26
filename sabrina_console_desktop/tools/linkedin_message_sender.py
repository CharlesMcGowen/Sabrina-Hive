"""One explicitly authorized message per call, with an exact LinkedIn delivery receipt."""
import time
from tools.linkedin_messaging_reader import READ_SCRIPT,LinkedInMessagingReader


def _norm_ws(value: str) -> str:
    return ' '.join((value or '').split())


def find_delivery(raw,expected,text,before_ids):
    current=LinkedInMessagingReader.normalize(raw,False)
    if any(current[k]!=expected[k] for k in ('account_id','thread_id','peer_id')):
        raise RuntimeError('Account or recipient changed during delivery verification')
    # LinkedIn's current virtualized DOM sometimes omits both the self CSS class
    # and the author link on a freshly appended outgoing row.  A new platform
    # message ID, exact authorized text, and exact account/thread/peer identity
    # still constitute unambiguous delivery evidence.  Explicitly external rows
    # remain ineligible.
    matches=[m for m in current['messages'] if m['id'] not in before_ids
             and m['ownership']!='external' and _norm_ws(m['text'])==_norm_ws(text)]
    if len(matches)==1:
        return {'status':'sent','message_id':matches[0]['id'],'account_id':current['account_id'],
                'thread_id':current['thread_id'],'text':text,'thread_url':current['url']}
    if len(matches)>1:raise RuntimeError('Multiple matching new messages; delivery is ambiguous')
    return None

class LinkedInMessageSender:
    def __init__(self,reader,store,outbox):
        self.reader=reader;self.store=store;self.outbox=outbox

    def send(self,outbox_id):
        if getattr(self.reader,'read_only_session',False) is True:
            raise ValueError('This browser session is read-only; sending is prohibited')
        entry=self.outbox.get(outbox_id)
        if entry['status']!='authorized':raise ValueError('Send is not authorized or has already been attempted')
        saved=self.store.snapshot(entry['account'],entry['thread'])
        fresh=self.reader.read_saved_thread(saved)
        version,_=self.store.observe(fresh)
        fresh['version']=version
        if version!=entry['context_version']:raise ValueError('Thread changed since approval; re-review is required')
        self.reader._guard()
        allowed,reason=self.reader.manager.rate_limiter.check_quota('direct_message')
        if not allowed:raise RuntimeError(reason)
        page=self.reader.page
        root=page.locator('.msg-thread')
        if root.count()!=1:raise RuntimeError('Active message thread is ambiguous')
        form=root.locator('form.msg-form')
        if form.count()!=1:raise RuntimeError('Could not uniquely identify the conversation composer')
        editor=form.locator('[contenteditable="true"].msg-form__contenteditable')
        if editor.count()!=1:raise RuntimeError('Could not uniquely identify the message editor')
        if editor.inner_text().strip():raise RuntimeError('Existing human draft preserved; nothing sent')
        button=form.locator('button.msg-form__send-button')
        if button.count()!=1:raise RuntimeError('Could not uniquely identify Send')
        text=entry['text']
        before_ids={m['id'] for m in fresh['messages']}
        editor.fill(text,timeout=5000)
        if isinstance(self.reader,LinkedInMessagingReader):
            editor.dispatch_event('input')
        try:
            page.wait_for_function("() => {const b=document.querySelector('.msg-thread form.msg-form button.msg-form__send-button');return b && !b.disabled}",timeout=5000)
            # Revalidate source messages immediately before committing the send claim.
            raw=page.evaluate(READ_SCRIPT)
            current=LinkedInMessagingReader.normalize(raw,False)
            if any(current[k]!=fresh[k] for k in ('account_id','thread_id','peer_id')):
                raise RuntimeError('Account or recipient changed before Send')
            if [(m['id'],m['text']) for m in current['messages']]!=[(m['id'],m['text']) for m in fresh['messages']]:
                raise RuntimeError('Messages changed before Send; nothing sent')
            if _norm_ws(editor.inner_text())!=_norm_ws(text):raise RuntimeError('Draft changed before Send')
            self.outbox.claim(outbox_id,fresh)
        except Exception:
            # Only clear text we just inserted, never an edited human draft.
            if _norm_ws(editor.inner_text())==_norm_ws(text):editor.fill('')
            raise
        try:
            self.reader.manager.rate_limiter.record_action('direct_message',{'outbox_id':outbox_id,'phase':'attempt'})
            button.click(timeout=5000)  # Exactly one click. Never repeat after an uncertain acknowledgement.
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                raw=page.evaluate(READ_SCRIPT)
                receipt=find_delivery(raw,fresh,text,before_ids)
                if receipt:
                    self.outbox.finish(outbox_id,receipt)
                    try:
                        latest=self.reader.read_saved_thread(fresh)
                        self.store.observe(latest)
                    except Exception as error:receipt['refresh_warning']=str(error)
                    return receipt
                page.wait_for_timeout(400)
            raise RuntimeError('No new own message ID was observed before the verification timeout')
        except Exception as error:
            receipt={'status':'uncertain','reason':str(error),'retry':False}
            self.outbox.finish(outbox_id,receipt)
            return receipt
