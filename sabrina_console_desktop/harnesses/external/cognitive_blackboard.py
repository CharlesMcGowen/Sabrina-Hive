import sqlite3
import threading
import datetime
import math
import json

class HierarchicalCognitiveBlackboard:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self.conn = self._connect()
        self._create_tables()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _create_tables(self):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS working_hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    domain TEXT,
                    model_id TEXT,
                    statement TEXT,
                    confidence REAL,
                    status TEXT,
                    created_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS semantic_solution_patterns (
                    pattern_id TEXT PRIMARY KEY,
                    domain TEXT,
                    bug_signature TEXT,
                    remedy TEXT,
                    ast_patch TEXT,
                    success_count INTEGER DEFAULT 0,
                    last_applied TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS model_competence_matrix (
                    model_id TEXT,
                    domain TEXT,
                    attempts INTEGER DEFAULT 0,
                    syntax_passed INTEGER DEFAULT 0,
                    tests_passed INTEGER DEFAULT 0,
                    avg_latency REAL DEFAULT 0,
                    reliability REAL DEFAULT 0,
                    PRIMARY KEY(model_id, domain)
                )
            """)
            self.conn.commit()

    def record_benchmark_result(self, request_id, campaign_id, portal_id, category, arm,
                                question, grade, source_ref, latency_ms=0):
        """Idempotent, provenance-bearing benchmark evidence; never grants permissions."""
        if not request_id:raise ValueError('A real request receipt is required')
        domain='hf_questionnaire:'+category+':'+arm
        with self._lock:
            with self.conn:
                self.conn.execute('''CREATE TABLE IF NOT EXISTS guild_benchmark_results (
                    request_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, portal_id TEXT NOT NULL,
                    domain TEXT NOT NULL, arm TEXT NOT NULL, question_json TEXT NOT NULL,
                    grade_json TEXT NOT NULL, source_ref TEXT NOT NULL, created TEXT NOT NULL)''')
                inserted=self.conn.execute('INSERT OR IGNORE INTO guild_benchmark_results VALUES (?,?,?,?,?,?,?,?,?)',
                    (request_id,campaign_id,portal_id,domain,arm,json.dumps(question),json.dumps(grade),
                     source_ref,datetime.datetime.now(datetime.timezone.utc).isoformat())).rowcount
                if not inserted:return False
                syntax_ok=int('parsed' in grade);correct=int(bool(grade.get('correct')))
                self.conn.execute('''INSERT INTO model_competence_matrix
                    (model_id,domain,attempts,syntax_passed,tests_passed,avg_latency,reliability)
                    VALUES (?,?,1,?,?,?,?) ON CONFLICT(model_id,domain) DO UPDATE SET
                    avg_latency=(model_competence_matrix.avg_latency*model_competence_matrix.attempts+excluded.avg_latency)/(model_competence_matrix.attempts+1),
                    attempts=model_competence_matrix.attempts+1,
                    syntax_passed=model_competence_matrix.syntax_passed+excluded.syntax_passed,
                    tests_passed=model_competence_matrix.tests_passed+excluded.tests_passed,
                    reliability=(model_competence_matrix.tests_passed+excluded.tests_passed)*1.0/(model_competence_matrix.attempts+1)''',
                    (portal_id,domain,syntax_ok,correct,float(latency_ms or 0),float(correct)))
                return True

    def record_hypothesis(self, hyp_id, domain, model_id, stmt, conf, status):
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.conn.execute("""
                INSERT OR REPLACE INTO working_hypotheses
                (hypothesis_id, domain, model_id, statement, confidence, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (hyp_id, domain, model_id, stmt, conf, status, now))
            self.conn.commit()

    def record_solution_pattern(self, pat_id, domain, bug_sig, remedy, ast_patch):
        with self._lock:
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.conn.execute("""
                INSERT INTO semantic_solution_patterns
                (pattern_id, domain, bug_signature, remedy, ast_patch, success_count, last_applied)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(pattern_id) DO UPDATE SET
                    domain=excluded.domain,
                    bug_signature=excluded.bug_signature,
                    remedy=excluded.remedy,
                    ast_patch=excluded.ast_patch,
                    success_count=semantic_solution_patterns.success_count + 1,
                    last_applied=excluded.last_applied
            """, (pat_id, domain, bug_sig, remedy, ast_patch, now))
            self.conn.commit()

    def query_patterns(self, domain, bug_sig=None):
        with self._lock:
            cur = self.conn.cursor()
            if bug_sig is None:
                cur.execute("""
                    SELECT * FROM semantic_solution_patterns
                    WHERE domain=?
                    ORDER BY success_count DESC
                """, (domain,))
            else:
                cur.execute("""
                    SELECT * FROM semantic_solution_patterns
                    WHERE domain=? AND bug_signature=?
                    ORDER BY success_count DESC
                """, (domain, bug_sig))
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def update_model_competence(self, model_id, domain, syntax_ok, tests_ok, latency_ms):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT attempts, syntax_passed, tests_passed, avg_latency, reliability
                FROM model_competence_matrix
                WHERE model_id=? AND domain=?
            """, (model_id, domain))
            row = cur.fetchone()
            if row:
                attempts = row["attempts"] + 1
                syntax_passed = row["syntax_passed"] + (1 if syntax_ok else 0)
                tests_passed = row["tests_passed"] + (1 if tests_ok else 0)
                new_avg_latency = ((row["avg_latency"] * (attempts - 1)) + latency_ms) / attempts
                reliability = tests_passed / attempts
                cur.execute("""
                    UPDATE model_competence_matrix
                    SET attempts=?, syntax_passed=?, tests_passed=?, avg_latency=?, reliability=?
                    WHERE model_id=? AND domain=?
                """, (attempts, syntax_passed, tests_passed, new_avg_latency, reliability, model_id, domain))
            else:
                attempts = 1
                syntax_passed = 1 if syntax_ok else 0
                tests_passed = 1 if tests_ok else 0
                reliability = tests_passed / attempts
                cur.execute("""
                    INSERT INTO model_competence_matrix
                    (model_id, domain, attempts, syntax_passed, tests_passed, avg_latency, reliability)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (model_id, domain, attempts, syntax_passed, tests_passed, latency_ms, reliability))
            self.conn.commit()

    def get_competence_ranking(self, domain):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT * FROM model_competence_matrix
                WHERE domain=?
                ORDER BY reliability DESC, avg_latency ASC
            """, (domain,))
            rows = cur.fetchall()
            return [dict(row) for row in rows]

    def begin_conversation_review(self, review_id, packet, models):
        """A private, source-bound workspace shared by conversation reviewers.

        Call this on a separate conversation database, not the public research
        board. Artifacts are evidence and proposals, never send permissions.
        """
        with self._lock:
            with self.conn:
                self.conn.executescript('''
                    CREATE TABLE IF NOT EXISTS conversation_review_runs (
                        id TEXT PRIMARY KEY, account TEXT NOT NULL, thread TEXT NOT NULL,
                        context_version INTEGER NOT NULL, profile_hash TEXT NOT NULL,
                        packet TEXT NOT NULL, models TEXT NOT NULL, status TEXT NOT NULL,
                        decision TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS conversation_review_artifacts (
                        review_id TEXT NOT NULL, role TEXT NOT NULL, attempt INTEGER NOT NULL,
                        model TEXT NOT NULL, family TEXT NOT NULL, request_id TEXT,
                        raw_output TEXT NOT NULL, parsed TEXT, validation_error TEXT,
                        created TEXT NOT NULL, PRIMARY KEY(review_id,role,attempt));
                ''')
                stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
                existing=self.conn.execute('SELECT packet,models FROM conversation_review_runs WHERE id=?',(review_id,)).fetchone()
                packet_json=json.dumps(packet,sort_keys=True,ensure_ascii=False)
                models_json=json.dumps(models,sort_keys=True)
                if existing and (existing['packet']!=packet_json or existing['models']!=models_json):
                    raise ValueError('Review identity cannot be reused with different conversation context')
                self.conn.execute('INSERT OR IGNORE INTO conversation_review_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                  (review_id,packet['thread']['account_id'],packet['thread']['thread_id'],
                                   packet['thread']['version'],packet['profile_hash'],packet_json,models_json,
                                   'prepared',None,None,stamp,stamp))
        return self.conversation_review(review_id)

    def conversation_review(self, review_id):
        with self._lock:
            row=self.conn.execute('SELECT * FROM conversation_review_runs WHERE id=?',(review_id,)).fetchone()
            if not row:return None
            value=dict(row)
            for key in ('packet','models','decision'):
                value[key]=json.loads(value[key]) if value[key] else None
            value['artifacts']=[]
            for artifact in self.conn.execute('SELECT * FROM conversation_review_artifacts WHERE review_id=? ORDER BY created,role,attempt',(review_id,)):
                item=dict(artifact)
                item['parsed']=json.loads(item['parsed']) if item['parsed'] else None
                value['artifacts'].append(item)
            return value

    def record_conversation_artifact(self, review_id, role, attempt, model, family,
                                     raw_output, parsed=None, validation_error=None, request_id=None):
        with self._lock:
            with self.conn:
                stamp=datetime.datetime.now(datetime.timezone.utc).isoformat()
                self.conn.execute('INSERT OR REPLACE INTO conversation_review_artifacts VALUES(?,?,?,?,?,?,?,?,?,?)',
                                  (review_id,role,attempt,model,family,request_id,str(raw_output),
                                   json.dumps(parsed,ensure_ascii=False) if parsed is not None else None,
                                   validation_error,stamp))
                self.conn.execute('UPDATE conversation_review_runs SET status=?,updated=? WHERE id=?',
                                  (role,stamp,review_id))

    def finish_conversation_review(self, review_id, status, decision=None, error=None):
        if status not in {'model_review_complete','draft_ready','no_reply','needs_input','blocked','stale'}:
            raise ValueError('Unsupported conversation review state')
        with self._lock:
            with self.conn:
                self.conn.execute('UPDATE conversation_review_runs SET status=?,decision=?,error=?,updated=? WHERE id=?',
                                  (status,json.dumps(decision,ensure_ascii=False) if decision is not None else None,
                                   error,datetime.datetime.now(datetime.timezone.utc).isoformat(),review_id))

    def close(self):
        with self._lock:
            if self.conn:
                self.conn.close()
                self.conn = None

if __name__ == "__main__":
    # Basic test harness
    db = HierarchicalCognitiveBlackboard(":memory:")
    db.record_hypothesis("h1", "math", "m1", "a + b", 0.9, "pending")
    db.record_solution_pattern("p1", "math", "sig1", "remedy1", "patch1")
    print(db.query_patterns("math"))
    db.update_model_competence("m1", "math", True, True, 120)
    print(db.get_competence_ranking("math"))
    db.close()
