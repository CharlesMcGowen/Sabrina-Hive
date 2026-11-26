"""
Sabrina CrewAI Documentation Ingester
======================================
Crawls the complete https://docs.crewai.com/ documentation index
(via https://docs.crewai.com/llms.txt) and ingests every page
into Sabrina's centralized RAG database (sabrina_knowledge_rag.db)
using the existing KnowledgeSynthesizer pipeline.

Source type tag: 'crewai_doc' for fast filtered retrieval.

Usage:
    # Full ingest (all ~200+ pages)
    python -m tools.ingest_crewai_docs

    # Smoke test (first N pages)
    python -m tools.ingest_crewai_docs --max-pages 5

    # Force re-ingest even if already indexed
    python -m tools.ingest_crewai_docs --force-refresh

    # Only ingest specific section(s) matching keyword
    python -m tools.ingest_crewai_docs --filter concepts
"""

import sys
import os
import re
import json
import time
import argparse
import logging
from pathlib import Path
from typing import List, Tuple, Optional, Dict

import requests

# ── Path bootstrap so we can import tools.* ─────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.knowledge_synthesizer import KnowledgeSynthesizer, ScraperUtils
from tools.rag_database import RAGDatabase, DEFAULT_DB_PATH

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] CrewAI-Ingest: %(message)s",
)
logger = logging.getLogger("sabrina.ingest_crewai_docs")

# ── Constants ────────────────────────────────────────────────────────────────
LLMS_TXT_URL   = "https://docs.crewai.com/llms.txt"
SOURCE_TYPE    = "crewai_doc"
DEFAULT_DELAY  = 0.8   # polite crawl delay (seconds) between pages
TIMEOUT        = 20    # seconds per HTTP request

# Priority core pages to always ingest first (human-curated)
PRIORITY_PAGES = [
    "https://docs.crewai.com/v1.15.22/en/introduction.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/agents.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/tasks.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/crews.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/flows.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/memory.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/tools.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/llms.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/knowledge.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/processes.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/planning.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/reasoning.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/training.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/streaming.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/checkpointing.md",
    "https://docs.crewai.com/v1.15.22/en/guides/crews/first-crew.md",
    "https://docs.crewai.com/v1.15.22/en/guides/flows/first-flow.md",
    "https://docs.crewai.com/v1.15.22/en/guides/flows/mastering-flow-state.md",
    "https://docs.crewai.com/v1.15.22/en/guides/agents/crafting-effective-agents.md",
    "https://docs.crewai.com/v1.15.22/en/guides/advanced/customizing-prompts.md",
    "https://docs.crewai.com/v1.15.22/en/guides/migration/migrating-from-langgraph.md",
    "https://docs.crewai.com/v1.15.22/en/mcp/overview.md",
    "https://docs.crewai.com/v1.15.22/en/concepts/cli.md",
]


def fetch_llms_txt(url: str = LLMS_TXT_URL) -> List[Tuple[str, str]]:
    """
    Parse the llms.txt index and return a list of (title, url) tuples
    for every .md documentation page discovered.
    """
    logger.info(f"Fetching documentation index from: {url}")
    try:
        resp = requests.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        raw = resp.text
    except Exception as e:
        logger.error(f"Failed to fetch llms.txt: {e}")
        return []

    pages = []
    # Format: - [Title](url): description
    pattern = re.compile(r"-\s+\[(.+?)\]\((https://docs\.crewai\.com/[^\)]+\.md)\)")
    for match in pattern.finditer(raw):
        title, doc_url = match.group(1), match.group(2)
        pages.append((title, doc_url))

    logger.info(f"Discovered {len(pages)} documentation pages in llms.txt")
    return pages


def fetch_markdown_direct(url: str) -> Tuple[str, str]:
    """
    Fetch a raw .md URL directly. Returns (title, content).
    Derives title from the first H1 header found, or from the URL path.
    """
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "Sabrina-DocIngest/1.0"})
        resp.raise_for_status()
        content = resp.text

        # Try to extract title from first # heading
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            title = match.group(1).strip()
        else:
            # Fall back to path stem
            title = "CrewAI: " + url.rstrip("/").split("/")[-1].replace(".md", "").replace("-", " ").title()

        # Strip frontmatter / Note boxes that are XML-like (docs.crewai uses MDX)
        # Remove <Tip> / <Note> / <Warning> / <Check> XML-style tags
        content = re.sub(r"<(Tip|Note|Warning|Check)[^>]*>", "\n> ", content, flags=re.IGNORECASE)
        content = re.sub(r"</(Tip|Note|Warning|Check)>", "\n", content, flags=re.IGNORECASE)

        # Keep code fences but remove stray HTML remnants
        content = re.sub(r"<[^>]{1,80}>", "", content)

        # Collapse excess blank lines
        content = re.sub(r"\n{4,}", "\n\n\n", content)
        content = content.strip()

        return title, content
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return "", ""


def build_unique_page_list(
    discovered: List[Tuple[str, str]],
    filter_keyword: Optional[str] = None,
    max_pages: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """
    Merge priority pages (always first) with the discovered index list,
    removing duplicates. Optionally filter by keyword and cap to max_pages.
    """
    seen = set()
    ordered = []

    # Priority pages first
    for url in PRIORITY_PAGES:
        if url not in seen:
            # Derive a working title for priority pages
            stem = url.rstrip("/").split("/")[-1].replace(".md", "").replace("-", " ").title()
            ordered.append((f"CrewAI: {stem}", url))
            seen.add(url)

    # Then all discovered pages (deduplicated)
    for title, url in discovered:
        if url not in seen:
            ordered.append((title, url))
            seen.add(url)

    # Optional keyword filter
    if filter_keyword:
        kw = filter_keyword.lower()
        ordered = [(t, u) for t, u in ordered if kw in u.lower() or kw in t.lower()]
        logger.info(f"After filter '{filter_keyword}': {len(ordered)} pages")

    if max_pages is not None:
        ordered = ordered[:max_pages]
        logger.info(f"Capped to {max_pages} pages")

    return ordered


def chunk_markdown_by_headers(title: str, content: str, max_chunk_chars: int = 3000) -> List[Dict[str, str]]:
    """
    Fast deterministic chunker: splits markdown by H2/H3 headers.
    No LLM call required. Each section becomes one chunk.
    Sections larger than max_chunk_chars are further split by paragraphs.
    """
    import re as _re
    chunks = []
    # Split on ## or ### headings
    sections = _re.split(r"(?=^#{2,3} )", content, flags=_re.MULTILINE)
    for section in sections:
        section = section.strip()
        if not section:
            continue
        # Extract topic from first line if it's a header
        lines = section.splitlines()
        if lines and lines[0].startswith("#"):
            topic = lines[0].lstrip("# ").strip()
            body = "\n".join(lines[1:]).strip()
        else:
            topic = title
            body = section
        if not body:
            body = section  # keep header-only sections
        # Split oversized sections by paragraph
        if len(body) > max_chunk_chars:
            paras = _re.split(r"\n{2,}", body)
            current, current_topic = "", topic
            for p_idx, para in enumerate(paras):
                if len(current) + len(para) < max_chunk_chars:
                    current = (current + "\n\n" + para).strip()
                else:
                    if current:
                        chunks.append({"topic": current_topic, "content": current})
                    current = para
                    current_topic = f"{topic} (cont.)"
            if current:
                chunks.append({"topic": current_topic, "content": current})
        else:
            chunks.append({"topic": topic, "content": body or section})
    if not chunks:
        chunks = [{"topic": title, "content": content[:3000]}]
    return chunks


def ingest_page(
    synth: KnowledgeSynthesizer,
    db: RAGDatabase,
    index_title: str,
    url: str,
    force_refresh: bool = False,
    no_synth: bool = False,
) -> dict:
    """
    Ingest a single .md documentation page into the RAG database.
    Returns a status dict with 'status', 'title', 'chunks_count', 'error'.
    """
    # Skip if already indexed (fast hash check)
    if not force_refresh:
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM documents WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row:
                logger.info(f"  [SKIP] Already indexed: {url}")
                return {"status": "already_indexed", "title": index_title, "url": url, "chunks_count": 0}

    # Fetch raw markdown directly (more accurate than HTML parsing for .md endpoints)
    title, content = fetch_markdown_direct(url)
    if not content:
        return {"status": "fetch_error", "title": index_title, "url": url, "chunks_count": 0, "error": "Empty response"}

    # Use index title if we couldn't extract one from content
    if not title:
        title = index_title

    content_hash = db.compute_hash(content)

    # Insert document record
    doc_id, is_new = db.insert_document(
        url=url,
        title=title,
        raw_content=content,
        source_type=SOURCE_TYPE,
    )

    if not is_new and not force_refresh:
        return {"status": "already_indexed", "title": title, "url": url, "chunks_count": 0}

    # Either fast deterministic chunking OR LLM synthesis
    if no_synth:
        logger.info(f"  [CHUNK] Splitting by headers (no-synth): {title}")
        chunks = chunk_markdown_by_headers(title, content)
        logger.info(f"  [CHUNK] Produced {len(chunks)} chunk(s)")
    else:
        logger.info(f"  [SYNTH] Synthesizing: {title}")
        chunks = synth.synthesize_chunks(title, content)
        logger.info(f"  [SYNTH] Produced {len(chunks)} chunk(s)")

    saved_chunk_ids = []
    for idx, chunk in enumerate(chunks):
        topic   = chunk.get("topic", title)
        chunk_content = chunk.get("content", "")
        if not chunk_content:
            continue

        # Generate vector embedding
        embedding_text = f"CrewAI Documentation\nTopic: {topic}\n\n{chunk_content}"
        vec = synth.get_embedding(embedding_text)

        chunk_id = db.insert_chunk(
            document_id=doc_id,
            chunk_index=idx,
            topic=f"CrewAI: {topic}",
            content=chunk_content,
            embedding=vec,
            embedding_model=synth.embed_model,
            token_count=len(chunk_content.split()),
        )
        saved_chunk_ids.append(chunk_id)

    return {
        "status": "success",
        "title": title,
        "url": url,
        "document_id": doc_id,
        "chunks_count": len(saved_chunk_ids),
    }


def run_ingestion(
    max_pages: Optional[int] = None,
    filter_keyword: Optional[str] = None,
    force_refresh: bool = False,
    delay: float = DEFAULT_DELAY,
    no_synth: bool = False,
) -> None:
    """
    Main orchestration: discover pages → ingest each one with progress tracking.
    """
    logger.info("=" * 60)
    logger.info("  SABRINA — CrewAI Documentation Full Ingest")
    logger.info("=" * 60)

    # Set up database and synthesizer
    db = RAGDatabase(DEFAULT_DB_PATH)
    synth = KnowledgeSynthesizer(db=db)
    logger.info(f"Database: {DEFAULT_DB_PATH}")
    if no_synth:
        logger.info("Synthesis mode:  DISABLED (fast header-based chunking)")
    else:
        logger.info(f"Synthesis model: {synth.synthesis_model}")
    logger.info(f"Embedding model:  {synth.embed_model}")

    # Discover all pages
    discovered = fetch_llms_txt()
    pages = build_unique_page_list(discovered, filter_keyword=filter_keyword, max_pages=max_pages)

    total = len(pages)
    logger.info(f"\nIngesting {total} pages...\n")

    results = {"success": 0, "already_indexed": 0, "fetch_error": 0, "total": total}
    summary_rows = []

    for i, (index_title, url) in enumerate(pages, 1):
        logger.info(f"[{i:3d}/{total}] {index_title}")
        logger.info(f"         {url}")

        try:
            result = ingest_page(synth, db, index_title, url, force_refresh=force_refresh, no_synth=no_synth)
            status = result.get("status", "unknown")
            chunks = result.get("chunks_count", 0)
            results[status] = results.get(status, 0) + 1
            summary_rows.append({
                "index": i,
                "title": result.get("title", index_title),
                "url": url,
                "status": status,
                "chunks": chunks,
            })
            logger.info(f"         → {status.upper()} | chunks: {chunks}")
        except Exception as e:
            logger.error(f"         → EXCEPTION: {e}")
            results["fetch_error"] = results.get("fetch_error", 0) + 1
            summary_rows.append({"index": i, "title": index_title, "url": url, "status": "exception", "chunks": 0})

        if i < total:
            time.sleep(delay)

    # Write JSON summary
    summary_path = Path(__file__).resolve().parent.parent / "data" / "crewai_ingest_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump({"run_stats": results, "pages": summary_rows}, f, indent=2)

    logger.info("\n" + "=" * 60)
    logger.info("  INGEST COMPLETE")
    logger.info(f"  Total Pages:      {total}")
    logger.info(f"  Newly Ingested:   {results.get('success', 0)}")
    logger.info(f"  Already Indexed:  {results.get('already_indexed', 0)}")
    logger.info(f"  Errors:           {results.get('fetch_error', 0) + results.get('exception', 0)}")
    logger.info(f"  Summary written:  {summary_path}")
    logger.info("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Ingest CrewAI docs into Sabrina RAG database")
    parser.add_argument("--max-pages", type=int, default=None,
                        help="Limit number of pages (default: all)")
    parser.add_argument("--filter", type=str, default=None, dest="filter_keyword",
                        help="Only ingest pages with this keyword in the URL/title")
    parser.add_argument("--force-refresh", action="store_true",
                        help="Re-ingest pages that are already indexed")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Delay between requests in seconds (default: {DEFAULT_DELAY})")
    parser.add_argument("--no-synth", action="store_true",
                        help="Skip LLM synthesis. Use fast deterministic header-based chunking instead. "
                             "Much faster for bulk ingestion (seconds/page vs minutes/page).")
    args = parser.parse_args()

    run_ingestion(
        max_pages=args.max_pages,
        filter_keyword=args.filter_keyword,
        force_refresh=args.force_refresh,
        delay=args.delay,
        no_synth=args.no_synth,
    )


if __name__ == "__main__":
    main()
