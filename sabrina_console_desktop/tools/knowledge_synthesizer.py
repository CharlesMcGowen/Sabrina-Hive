"""
Sabrina Knowledge Synthesizer
==============================
Unified pipeline to uptake raw web documentation (e.g. CrewAI, FastAPI, etc.),
clean noisy HTML, distill canonical technical specs via lab LLMs,
generate vector embeddings, and ingest into the centralized RAG database.
"""

import os
import re
import json
import logging
import urllib.request
import urllib.parse
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

import requests
from bs4 import BeautifulSoup
import numpy as np

from tools.rag_database import RAGDatabase, DEFAULT_DB_PATH

logger = logging.getLogger("sabrina.knowledge_synthesizer")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# Lab server configurations
LAB_HOST = os.environ.get("SABRINA_LAB_HOST", "192.168.2.69")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", f"http://{LAB_HOST}:11434")
SYNTHESIS_MODEL = os.environ.get("SYNTHESIS_MODEL", "llama3.1:latest")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "nomic-embed-text")


class ScraperUtils:
    """Utilities for fetching and converting web pages into clean technical text."""

    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    @classmethod
    def fetch_url(cls, url: str, timeout: int = 20) -> str:
        """Fetch raw HTML/text from a given URL."""
        logger.info(f"Fetching URL: {url}")
        resp = requests.get(url, headers=cls.DEFAULT_HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text

    @classmethod
    def clean_html(cls, html_content: str) -> Tuple[str, str]:
        """
        Strips boilerplate, navigation, ads, and footers from HTML.
        Returns (extracted_title, clean_markdown_or_text).
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # Extract title
        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        elif soup.find("h1"):
            title = soup.find("h1").get_text().strip()
        else:
            title = "Documentation Page"

        # Remove irrelevant non-content tags
        for element in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "form"]):
            element.decompose()

        # Target main content container if available
        main_content = (
            soup.find("main")
            or soup.find("article")
            or soup.find("div", class_=re.compile(r"content|documentation|docs-body|markdown", re.I))
            or soup.find("body")
            or soup
        )

        # Convert code blocks nicely
        for pre in main_content.find_all("pre"):
            code = pre.find("code")
            code_text = code.get_text() if code else pre.get_text()
            pre.replace_with(f"\n```python\n{code_text.strip()}\n```\n")

        # Convert inline code
        for code in main_content.find_all("code"):
            code.replace_with(f" `{code.get_text()}` ")

        # Convert headers
        for level in range(6, 0, -1):
            for h in main_content.find_all(f"h{level}"):
                h.replace_with(f"\n{'#' * level} {h.get_text().strip()}\n")

        # Convert list items
        for li in main_content.find_all("li"):
            li.replace_with(f"\n- {li.get_text().strip()}")

        text = main_content.get_text()

        # Normalize multiple spaces and blank lines
        lines = [line.strip() for line in text.splitlines()]
        clean_text = "\n".join(chunk for chunk in lines if chunk)

        return title, clean_text


class KnowledgeSynthesizer:
    """
    Distills noisy documentation into dense, canonical technical knowledge
    using LLMs on the lab host, then generates embeddings and saves to DB.
    """

    def __init__(
        self,
        db: Optional[RAGDatabase] = None,
        ollama_url: str = OLLAMA_BASE_URL,
        synthesis_model: str = SYNTHESIS_MODEL,
        embed_model: str = EMBED_MODEL
    ):
        self.db = db or RAGDatabase()
        self.ollama_url = ollama_url.rstrip("/")
        self.synthesis_model = synthesis_model
        self.embed_model = embed_model

    def get_embedding(self, text: str) -> Optional[np.ndarray]:
        """Fetch vector embedding from Ollama /api/embeddings."""
        url = f"{self.ollama_url}/api/embeddings"
        payload = json.dumps({
            "model": self.embed_model,
            "prompt": text[:8000]
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                vec = data.get("embedding")
                if vec:
                    return np.array(vec, dtype=np.float32)
        except Exception as e:
            logger.warning(f"Embedding request failed: {e}")
        return None

    def synthesize_chunks(self, title: str, clean_text: str) -> List[Dict[str, str]]:
        """
        Uses the lab model to extract discrete, high-quality technical chunks.
        Splits clean_text into manageable slices if it's large.
        """
        # Truncate or window text if massive
        max_chars = 12000
        text_slice = clean_text[:max_chars]

        prompt = f"""You are an elite technical documentation synthesizer.
Analyze the following documentation excerpt for "{title}".
Distill this text into 2 to 5 discrete, highly technical knowledge chunks.
For each chunk:
1. Provide a precise, descriptive TOPIC name (e.g. "CrewAI Agent Class Definition", "CrewAI Task Configuration").
2. In the CONTENT, provide the exact API syntax, constructor parameters, argument types, return types, and concrete working code examples.
3. Exclude marketing fluff, navigation links, and conversational filler.

Format your response STRICTLY as follows with no extraneous markdown:
=== CHUNK ===
TOPIC: <precise topic title>
CONTENT:
<dense technical documentation and code snippet>
=== END CHUNK ===
"""

        url = f"{self.ollama_url}/api/generate"
        payload = {
            "model": self.synthesis_model,
            "prompt": prompt + f"\n\n--- DOCUMENTATION ---\n{text_slice}\n--- END DOCUMENTATION ---",
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": 4096
            }
        }

        try:
            logger.info(f"Synthesizing documentation via {self.synthesis_model}...")
            resp = requests.post(url, json=payload, timeout=300)
            resp.raise_for_status()
            response_text = resp.json().get("response", "")

            chunks = self._parse_synthesized_chunks(response_text)
            if not chunks:
                # Fallback: create a single default chunk if LLM format deviated
                logger.info("Parsing fell back to raw text chunking")
                chunks = [{"topic": title, "content": text_slice[:2000]}]
            return chunks
        except Exception as e:
            logger.error(f"Synthesis failed with error: {e}")
            # Fallback: slice raw text directly into chunks
            return self._fallback_raw_chunking(title, clean_text)

    def _parse_synthesized_chunks(self, response_text: str) -> List[Dict[str, str]]:
        """Parse structured chunks from the LLM output."""
        chunks = []
        raw_chunks = re.split(r"=== CHUNK ===", response_text)
        for rc in raw_chunks:
            rc = rc.replace("=== END CHUNK ===", "").strip()
            if not rc:
                continue

            topic_match = re.search(r"TOPIC:\s*(.+)", rc)
            content_match = re.search(r"CONTENT:\s*(.*)", rc, re.DOTALL)

            if topic_match and content_match:
                topic = topic_match.group(1).strip()
                content = content_match.group(1).strip()
                if topic and content:
                    chunks.append({"topic": topic, "content": content})
            elif len(rc) > 50:
                # Best-effort extraction
                lines = rc.splitlines()
                first_line = lines[0].replace("#", "").replace("TOPIC:", "").strip()
                rest = "\n".join(lines[1:]).strip()
                chunks.append({"topic": first_line or "Documentation Chunk", "content": rest or rc})

        return chunks

    def _fallback_raw_chunking(self, title: str, text: str, chunk_size: int = 1500) -> List[Dict[str, str]]:
        """Direct text chunking when synthesis model is unreachable."""
        paragraphs = text.split("\n\n")
        chunks = []
        current = []
        current_len = 0
        idx = 1

        for p in paragraphs:
            p = p.strip()
            if not p:
                continue
            if current_len + len(p) > chunk_size and current:
                chunks.append({
                    "topic": f"{title} (Part {idx})",
                    "content": "\n\n".join(current)
                })
                current = []
                current_len = 0
                idx += 1
            current.append(p)
            current_len += len(p)

        if current:
            chunks.append({
                "topic": f"{title} (Part {idx})",
                "content": "\n\n".join(current)
            })

        return chunks

    def ingest_url(
        self,
        url: str,
        source_type: str = "documentation",
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Full ingestion pipeline:
        1. Fetches and cleans HTML.
        2. Deduplicates against database content hash.
        3. Synthesizes knowledge into canonical chunks.
        4. Computes embeddings.
        5. Saves into RAG database.
        """
        logger.info(f"Starting ingestion for: {url}")
        html_content = ScraperUtils.fetch_url(url)
        title, clean_text = ScraperUtils.clean_html(html_content)

        # Check existing
        content_hash = self.db.compute_hash(clean_text)
        with self.db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, content_hash FROM documents WHERE url = ?", (url,))
            row = cursor.fetchone()
            if row and not force_refresh:
                if row["content_hash"] == content_hash:
                    logger.info(f"Document already ingested and up-to-date (ID: {row['id']})")
                    return {
                        "status": "already_indexed",
                        "document_id": row["id"],
                        "title": title,
                        "url": url,
                        "chunks_count": 0
                    }

        # Insert / update document
        doc_id, is_new = self.db.insert_document(
            url=url,
            title=title,
            raw_content=clean_text,
            source_type=source_type
        )

        # Synthesize chunks
        chunks = self.synthesize_chunks(title, clean_text)
        logger.info(f"Produced {len(chunks)} synthesized chunks for: {title}")

        saved_chunk_ids = []
        for idx, chunk in enumerate(chunks):
            topic = chunk["topic"]
            content = chunk["content"]

            # Compute embedding
            vec = self.get_embedding(f"{topic}\n{content}")

            chunk_id = self.db.insert_chunk(
                document_id=doc_id,
                chunk_index=idx,
                topic=topic,
                content=content,
                embedding=vec,
                embedding_model=self.embed_model,
                token_count=len(content.split())
            )
            saved_chunk_ids.append(chunk_id)

        logger.info(f"Ingestion complete: Doc ID {doc_id} with {len(saved_chunk_ids)} chunks.")
        return {
            "status": "success",
            "document_id": doc_id,
            "title": title,
            "url": url,
            "chunks_count": len(saved_chunk_ids),
            "chunks": chunks
        }
