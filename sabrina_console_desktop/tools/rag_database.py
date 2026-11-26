"""
Sabrina Centralized RAG Database Engine
=======================================
Provides SQLite storage with full-text search (FTS5) and vector similarity
search (via numpy-accelerated cosine similarity) for technical documentation,
API references, and security knowledge chunks.
"""

import os
import sqlite3
import hashlib
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import numpy as np

logger = logging.getLogger("sabrina.rag_database")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "sabrina_knowledge_rag.db"


class RAGDatabase:
    def __init__(self, db_path: Optional[str or Path] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_db(self):
        """Initialize database schema including FTS5 tables and indices."""
        with self._get_connection() as conn:
            # 1. Documents table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE,
                    title TEXT NOT NULL,
                    source_type TEXT DEFAULT 'documentation',
                    raw_content TEXT,
                    content_hash TEXT UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 2. Chunks table (with binary BLOB for embeddings)
            conn.execute("""
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
                );
            """)

            # 3. Create indices
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(document_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_topic ON chunks(topic);")

            # 4. FTS5 Virtual Table for full-text keyword search
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                        chunk_id UNINDEXED,
                        topic,
                        content,
                        tokenize = 'porter unicode61'
                    );
                """)
            except sqlite3.OperationalError as e:
                logger.warning(f"FTS5 virtual table initialization notice: {e}")

            conn.commit()

    @staticmethod
    def compute_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    def insert_document(
        self,
        url: str,
        title: str,
        raw_content: str,
        source_type: str = "documentation"
    ) -> Tuple[int, bool]:
        """
        Insert or update a document.
        Returns (document_id, is_new).
        """
        content_hash = self.compute_hash(raw_content)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Check existing document by URL or hash
            cursor.execute("SELECT id, content_hash FROM documents WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row:
                doc_id = row["id"]
                if row["content_hash"] == content_hash:
                    # Content unchanged
                    return doc_id, False
                # Content changed: update document and clear old chunks
                cursor.execute("""
                    UPDATE documents
                    SET title = ?, raw_content = ?, content_hash = ?, source_type = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (title, raw_content, content_hash, source_type, doc_id))
                # Remove from FTS
                cursor.execute("""
                    DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)
                """, (doc_id,))
                cursor.execute("DELETE FROM chunks WHERE document_id = ?", (doc_id,))
                conn.commit()
                return doc_id, True

            # Insert new
            cursor.execute("""
                INSERT INTO documents (url, title, source_type, raw_content, content_hash)
                VALUES (?, ?, ?, ?, ?)
            """, (url, title, source_type, raw_content, content_hash))
            conn.commit()
            return cursor.lastrowid, True

    def insert_chunk(
        self,
        document_id: int,
        chunk_index: int,
        topic: str,
        content: str,
        embedding: Optional[np.ndarray] = None,
        embedding_model: str = "nomic-embed-text",
        token_count: int = 0
    ) -> int:
        """Insert a single synthesized chunk with vector embedding and FTS indexing."""
        blob = None
        if embedding is not None:
            arr = np.array(embedding, dtype=np.float32)
            blob = arr.tobytes()

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chunks (document_id, chunk_index, topic, content, embedding, embedding_model, token_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (document_id, chunk_index, topic, content, blob, embedding_model, token_count))
            chunk_id = cursor.lastrowid

            # Insert into FTS
            try:
                cursor.execute("""
                    INSERT INTO chunks_fts (chunk_id, topic, content)
                    VALUES (?, ?, ?)
                """, (chunk_id, topic, content))
            except Exception as e:
                logger.warning(f"Failed to index chunk {chunk_id} in FTS: {e}")

            conn.commit()
            return chunk_id

    def search_vector(
        self,
        query_embedding: np.ndarray,
        top_k: int = 5,
        min_score: float = 0.0
    ) -> List[Dict[str, Any]]:
        """
        Fast cosine similarity search across all stored chunk embeddings.
        Vectorized with numpy for sub-millisecond query latency.
        """
        query_vec = np.array(query_embedding, dtype=np.float32)
        q_norm = np.linalg.norm(query_vec)
        if q_norm == 0:
            return []

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT c.id, c.document_id, c.chunk_index, c.topic, c.content, c.embedding,
                       d.title as doc_title, d.url as doc_url
                FROM chunks c
                JOIN documents d ON c.document_id = d.id
                WHERE c.embedding IS NOT NULL
            """)
            rows = cursor.fetchall()

            if not rows:
                return []

            chunk_ids = []
            metadata_list = []
            vectors = []

            for r in rows:
                blob = r["embedding"]
                if blob:
                    vec = np.frombuffer(blob, dtype=np.float32)
                    vectors.append(vec)
                    chunk_ids.append(r["id"])
                    metadata_list.append({
                        "id": r["id"],
                        "document_id": r["document_id"],
                        "chunk_index": r["chunk_index"],
                        "topic": r["topic"],
                        "content": r["content"],
                        "doc_title": r["doc_title"],
                        "doc_url": r["doc_url"]
                    })

            if not vectors:
                return []

            mat = np.stack(vectors)
            mat_norms = np.linalg.norm(mat, axis=1)
            mat_norms[mat_norms == 0] = 1e-10

            # Cosine similarity = (A . B) / (||A|| * ||B||)
            sims = np.dot(mat, query_vec) / (mat_norms * q_norm)

            # Sort indices descending
            top_indices = np.argsort(sims)[::-1][:top_k]

            results = []
            for idx in top_indices:
                score = float(sims[idx])
                if score >= min_score:
                    item = metadata_list[idx].copy()
                    item["score"] = round(score, 4)
                    results.append(item)

            return results

    def search_fts(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """BM25 full-text keyword search using SQLite FTS5."""
        clean_query = "".join(c if c.isalnum() or c.isspace() else " " for c in query).strip()
        if not clean_query:
            return []

        terms = [f'"{t}"*' for t in clean_query.split() if len(t) > 1]
        if not terms:
            terms = [f'"{t}"' for t in clean_query.split() if t]
        if not terms:
            return []

        fts_query = " OR ".join(terms)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    SELECT c.id, c.document_id, c.chunk_index, c.topic, c.content,
                           d.title as doc_title, d.url as doc_url,
                           rank as fts_rank
                    FROM chunks_fts f
                    JOIN chunks c ON f.chunk_id = c.id
                    JOIN documents d ON c.document_id = d.id
                    WHERE chunks_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, top_k))
                rows = cursor.fetchall()
                results = []
                for r in rows:
                    score = float(-r["fts_rank"]) if r["fts_rank"] is not None else 1.0
                    results.append({
                        "id": r["id"],
                        "document_id": r["document_id"],
                        "chunk_index": r["chunk_index"],
                        "topic": r["topic"],
                        "content": r["content"],
                        "doc_title": r["doc_title"],
                        "doc_url": r["doc_url"],
                        "score": round(score, 4)
                    })
                return results
            except Exception as e:
                logger.error(f"FTS search error: {e}")
                return []

    def search_hybrid(
        self,
        query: str,
        query_embedding: Optional[np.ndarray] = None,
        top_k: int = 5,
        vector_weight: float = 0.7
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search combining semantic vector retrieval and BM25 FTS5 keyword matching.
        Uses Reciprocal Rank Fusion (RRF) to merge score ranks smoothly.
        """
        fts_results = self.search_fts(query, top_k=top_k * 2)
        vec_results = self.search_vector(query_embedding, top_k=top_k * 2) if query_embedding is not None else []

        if not vec_results and not fts_results:
            return []
        if not vec_results:
            return fts_results[:top_k]
        if not fts_results:
            return vec_results[:top_k]

        rrf_constant = 60.0
        scores: Dict[int, float] = {}
        items: Dict[int, Dict[str, Any]] = {}

        for rank, r in enumerate(vec_results):
            cid = r["id"]
            items[cid] = r
            scores[cid] = scores.get(cid, 0.0) + vector_weight * (1.0 / (rrf_constant + rank + 1))

        keyword_weight = 1.0 - vector_weight
        for rank, r in enumerate(fts_results):
            cid = r["id"]
            if cid not in items:
                items[cid] = r
            scores[cid] = scores.get(cid, 0.0) + keyword_weight * (1.0 / (rrf_constant + rank + 1))

        sorted_cids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)

        merged = []
        for cid in sorted_cids[:top_k]:
            res = items[cid].copy()
            res["score"] = round(scores[cid], 5)
            merged.append(res)

        return merged

    def get_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as count FROM documents")
            doc_count = cursor.fetchone()["count"]

            cursor.execute("SELECT COUNT(*) as count FROM chunks")
            chunk_count = cursor.fetchone()["count"]

            cursor.execute("SELECT DISTINCT embedding_model FROM chunks WHERE embedding_model IS NOT NULL")
            models = [r["embedding_model"] for r in cursor.fetchall()]

            return {
                "db_path": str(self.db_path),
                "total_documents": doc_count,
                "total_chunks": chunk_count,
                "embedding_models": models
            }
