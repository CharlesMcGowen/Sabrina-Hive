"""Explicitly authorized direct-message outbox; the model cannot approve a send."""
import json
from tools.messaging_store import digest,now

class MessageOutbox:
    def __init__(self,store):
        self.store=store;self.db=store.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS message_outbox(
          id INTEGER PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
          account TEXT NOT NULL,thread TEXT NOT NULL,decision_id INTEGER NOT NULL,
          context_version INTEGER NOT NULL,draft_hash TEXT NOT NULL,text TEXT NOT NULL,
          authorization TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT,created TEXT NOT NULL,updated TEXT NOT NULL)''')
        self.db.commit()

    def authorize(self,decision_id,user_instruction):
        if not isinstance(user_instruction,str) or not user_instruction.strip():
            raise ValueError('Explicit user authorization is required')
        with self.db:
            row=self.db.execute('SELECT * FROM decisions WHERE id=?',(decision_id,)).fetchone()
            if not row or row['status']!='draft':raise ValueError('Only a current review draft can be authorized')
            current=self.store.snapshot(row['account'],row['thread'])
            if current['version']!=row['context_version'] or not current['complete']:
                raise ValueError('Conversation changed or history incomplete')
            if current['messages'][-1]['ownership']!='external':raise ValueError('Account owner already replied')
            decision=json.loads(row['content']);text=decision.get('draft','').strip()
            if decision.get('action')!='draft_reply' or not 15<=len(text)<=1400:raise ValueError('Invalid draft')
            draft_hash=digest(text)
            key=digest([row['account'],row['thread'],row['context_version'],draft_hash])
            self.db.execute('INSERT OR IGNORE INTO message_outbox(action_key,account,thread,decision_id,context_version,draft_hash,text,authorization,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                            (key,row['account'],row['thread'],decision_id,row['context_version'],draft_hash,text,user_instruction,'authorized',now(),now()))
            return self.db.execute('SELECT id FROM message_outbox WHERE action_key=?',(key,)).fetchone()['id']

    def get(self,outbox_id):
        row=self.db.execute('SELECT * FROM message_outbox WHERE id=?',(outbox_id,)).fetchone()
        if not row:raise KeyError(outbox_id)
        return dict(row)

    def claim(self,outbox_id,expected):
        # A conditional write under the SQLite transaction prevents two successful claims.
        with self.db:
            self.db.execute('UPDATE message_outbox SET updated=updated WHERE id=?',(outbox_id,))
            row=self.get(outbox_id)
            if row['status']!='authorized':raise ValueError('Message already claimed, sent or uncertain; do not retry')
            if row['account']!=expected['account_id'] or row['thread']!=expected['thread_id']:
                raise ValueError('Wrong account or recipient thread')
            current=self.store.snapshot(row['account'],row['thread'])
            if current['version']!=row['context_version'] or expected['version']!=row['context_version']:
                raise ValueError('Stale send authorization')
            decision=self.db.execute('SELECT status,content FROM decisions WHERE id=?',(row['decision_id'],)).fetchone()
            if not decision or decision['status']!='draft' or digest(json.loads(decision['content']).get('draft','').strip())!=row['draft_hash']:
                raise ValueError('Draft changed after authorization')
            if not current['complete'] or current['messages'][-1]['ownership']!='external':raise ValueError('No unanswered external message')
            pending=self.db.execute("SELECT 1 FROM message_outbox WHERE account=? AND thread=? AND id!=? AND status IN ('sending','uncertain')",(row['account'],row['thread'],outbox_id)).fetchone()
            if pending:raise ValueError('Prior uncertain send must be reconciled first')
            changed=self.db.execute("UPDATE message_outbox SET status='sending',updated=? WHERE id=? AND status='authorized'",(now(),outbox_id)).rowcount
            if changed!=1:raise ValueError('Send claim lost')
        return row

    def finish(self,outbox_id,receipt):
        if receipt.get('status') not in ('sent','uncertain'):raise ValueError('Invalid delivery state')
        row=self.get(outbox_id)
        if row['status']!='sending':raise ValueError('No active send claim')
        if receipt['status']=='sent':
            if (not receipt.get('message_id') or receipt.get('account_id')!=row['account']
                or receipt.get('thread_id')!=row['thread'] or receipt.get('text')!=row['text']):
                raise ValueError('Delivery evidence does not match the authorized message')
        with self.db:
            self.db.execute('UPDATE message_outbox SET status=?,receipt=?,updated=? WHERE id=? AND status=\'sending\'',
                            (receipt['status'],json.dumps(receipt),now(),outbox_id))
            self.db.execute('UPDATE decisions SET status=? WHERE id=?',
                            ('sent' if receipt['status']=='sent' else 'uncertain',row['decision_id']))

    def release_before_send(self,outbox_id,error):
        """Rearm only when the delivery adapter proves no send click occurred."""
        with self.db:
            changed=self.db.execute(
                "UPDATE message_outbox SET status='authorized',receipt=?,updated=? WHERE id=? AND status='sending'",
                (json.dumps({'pre_send_error':str(error),'released_at':now()}),now(),outbox_id)).rowcount
            if changed!=1:raise ValueError('Pre-send claim could not be released')

    def rearm_after_human_verified_absent(self,outbox_id,human_confirmation):
        """Permit one new attempt only after a human verifies the message is absent.

        This deliberately reuses the original outbox row so the prior uncertain
        receipt and the human reconciliation remain in one audit trail.
        """
        if not isinstance(human_confirmation,str) or not human_confirmation.strip():
            raise ValueError('Human verification that the message is absent is required')
        with self.db:
            row=self.get(outbox_id)
            if row['status']!='uncertain':
                raise ValueError('Only an uncertain send can be reconciled this way')
            prior=json.loads(row['receipt'] or '{}')
            audit={'prior':prior,'human_verified_absent':human_confirmation.strip(),
                   'rearmed_at':now(),'retry_limit':1}
            changed=self.db.execute(
                "UPDATE message_outbox SET status='authorized',authorization=?,receipt=?,updated=? "
                "WHERE id=? AND status='uncertain'",
                (row['authorization']+'\nHuman reconciliation: '+human_confirmation.strip(),
                 json.dumps(audit),now(),outbox_id)).rowcount
            if changed!=1:raise ValueError('Uncertain-send reconciliation lost')
            self.db.execute("UPDATE decisions SET status='draft' WHERE id=? AND status='uncertain'",
                            (row['decision_id'],))
        return outbox_id

    def reconcile_late_delivery(self,outbox_id,receipt):
        """Promote an uncertain attempt when later platform evidence proves delivery."""
        row=self.get(outbox_id)
        if row['status']!='uncertain':raise ValueError('Only an uncertain send can be reconciled')
        if (receipt.get('status')!='sent' or not receipt.get('message_id')
            or receipt.get('account_id')!=row['account']
            or receipt.get('thread_id')!=row['thread'] or receipt.get('text')!=row['text']):
            raise ValueError('Late delivery evidence does not match the authorized message')
        with self.db:
            changed=self.db.execute(
                "UPDATE message_outbox SET status='sent',receipt=?,updated=? WHERE id=? AND status='uncertain'",
                (json.dumps({**receipt,'reconciled_after_timeout':True}),now(),outbox_id)).rowcount
            if changed!=1:raise ValueError('Late-delivery reconciliation lost')
            self.db.execute("UPDATE decisions SET status='sent' WHERE id=?",(row['decision_id'],))
        return receipt

    def receipts(self):
        return [{**dict(r),'receipt':json.loads(r['receipt']) if r['receipt'] else None}
                for r in self.db.execute('SELECT * FROM message_outbox ORDER BY id DESC')]
