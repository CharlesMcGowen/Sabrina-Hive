"""
tools/ingest_bounty_methodology.py
====================================
One-time (re-runnable) ingestion of the real HackerOne top-report bug-type
corpus into sabrina_knowledge_rag.db -- the actual database CrewAI's
tool-developer agent searches via search_knowledge_base (see
ai_systems/agents/tool_developer.py).

Why this exists: found that this corpus (27 real bug-type categories,
reddelexc_hackerone_reports/docs/tops_by_bug_type/) was never actually loaded
into any queryable store -- the RAG database CrewAI/Claude would search only
had 17 generic documents (mostly mirrored save_knowledge topics from live
runs), nothing from the real methodology data sitting on disk. Before Claude
can meaningfully "design new systems using the bug bounty methodology RAG,"
the RAG needs to actually contain that methodology.

Chunking: fixed-size (~2000 chars) on paragraph boundaries where possible --
some source files (e.g. TOPXSS.md) are 2000+ lines of many individual real
report writeups, and a single chunk-per-file would return one giant
undifferentiated blob per search hit instead of a focused, relevant snippet.
Embeddings ARE generated per chunk (via nomic-embed-text on the lab Ollama host)
-- verified live this actually matters: a first pass without embeddings inserted
617 real chunks correctly, but search_hybrid's default 0.7 vector-weight meant
un-embedded (real, relevant) chunks lost to embedded-but-irrelevant old seed
chunks in the final ranking. A test query for "IDOR bypass techniques" returned
a generic "Lab Server Dataset Catalog" chunk instead of the real IDOR content.
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__)) + "/.."
sys.path.insert(0, os.path.abspath(_PROJECT_ROOT))

from tools.rag_database import RAGDatabase, DEFAULT_DB_PATH
from tools.knowledge_synthesizer import KnowledgeSynthesizer

SOURCE_DIR = r"C:\Users\bcmcg\Desktop\bot\LivingArchive-Sabrina-pro\data\external_datasets\reddelexc_hackerone_reports\docs\tops_by_bug_type"
CHUNK_SIZE = 2000


def chunk_text(text: str, size: int = CHUNK_SIZE):
    """Fixed-size chunking that tries to break on a paragraph boundary near `size`."""
    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # Prefer to break at the last blank line within this window
            break_at = text.rfind("\n\n", start, end)
            if break_at > start:
                end = break_at
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end
    return chunks


def main():
    if not os.path.isdir(SOURCE_DIR):
        print(f"Source directory not found: {SOURCE_DIR}")
        return

    db = RAGDatabase(DEFAULT_DB_PATH)
    synth = KnowledgeSynthesizer(db=db)
    total_docs = 0
    total_chunks = 0
    embed_failures = 0

    for fname in sorted(os.listdir(SOURCE_DIR)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(SOURCE_DIR, fname)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        bug_type = fname.replace("TOP", "").replace(".md", "")
        title = f"HackerOne Top Reports: {bug_type}"
        url = f"internal://bounty_methodology/{fname}"

        doc_id, is_new = db.insert_document(url=url, title=title, raw_content=content, source_type="bounty_methodology")
        total_docs += 1

        if not is_new:
            print(f"  {fname}: unchanged, skipped chunk re-insert")
            continue

        chunks = chunk_text(content)
        for idx, chunk in enumerate(chunks):
            embedding = synth.get_embedding(chunk)
            if embedding is None:
                embed_failures += 1
            db.insert_chunk(document_id=doc_id, chunk_index=idx, topic=bug_type, content=chunk, embedding=embedding)
            total_chunks += 1

        print(f"  {fname}: {len(chunks)} chunks")

    print(f"\nIngested {total_docs} documents, {total_chunks} new chunks ({embed_failures} embedding failures) into {DEFAULT_DB_PATH}")


if __name__ == "__main__":
    main()
