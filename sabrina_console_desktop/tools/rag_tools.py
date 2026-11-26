"""
Sabrina RAG Tools
=================
Callable tools for Sabrina to autonomously query the centralized knowledge base
or dynamically uptake and synthesize external documentation when encountering
new frameworks, libraries, or APIs.
"""

import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from tools.rag_database import RAGDatabase, DEFAULT_DB_PATH
from tools.knowledge_synthesizer import KnowledgeSynthesizer, LAB_HOST, OLLAMA_BASE_URL, EMBED_MODEL

logger = logging.getLogger("sabrina.rag_tools")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

_db_instance: Optional[RAGDatabase] = None
_synthesizer_instance: Optional[KnowledgeSynthesizer] = None


def get_rag_db() -> RAGDatabase:
    global _db_instance
    if _db_instance is None:
        _db_instance = RAGDatabase(DEFAULT_DB_PATH)
    return _db_instance


def get_synthesizer() -> KnowledgeSynthesizer:
    global _synthesizer_instance
    if _synthesizer_instance is None:
        _synthesizer_instance = KnowledgeSynthesizer(db=get_rag_db())
    return _synthesizer_instance


def query_rag(query: str, top_k: int = 4) -> str:
    """
    Search the centralized RAG knowledge base for technical documentation,
    API syntax, working code examples, and methodology notes.

    Args:
        query: The search term or conceptual question (e.g. "CrewAI Agent Task setup")
        top_k: Maximum number of knowledge chunks to return (default 4)

    Returns:
        Formatted markdown context string with source references and code snippets.
    """
    db = get_rag_db()
    synth = get_synthesizer()

    # Generate query embedding
    query_vec = synth.get_embedding(query)

    # Hybrid search: combining vector similarity and FTS keyword matching
    results = db.search_hybrid(query=query, query_embedding=query_vec, top_k=top_k)

    if not results:
        return f"No relevant documentation found in RAG database for query: '{query}'."

    output_lines = [f"### RAG Knowledge Retrieval for: '{query}'\n"]
    for idx, item in enumerate(results, 1):
        output_lines.append(f"#### Chunk {idx}: {item.get('topic', 'Untitled')}")
        output_lines.append(f"- **Source**: [{item.get('doc_title', 'Documentation')}]({item.get('doc_url', '#')})")
        output_lines.append(f"- **Relevance Score**: {item.get('score', 0.0)}")
        output_lines.append("\n```text\n" + item.get('content', '').strip() + "\n```\n")

    return "\n".join(output_lines)


def fetch_and_synthesize_docs(url: str, force_refresh: bool = False) -> str:
    """
    Fetch external documentation from a URL, clean HTML, distill canonical
    API specs using the lab LLM, generate vector embeddings, and ingest
    into Sabrina's centralized RAG database.

    Args:
        url: The web URL of the documentation or guide to uptake.
        force_refresh: Re-ingest even if the URL has already been processed.

    Returns:
        Status summary detailing newly added knowledge chunks and topics.
    """
    synth = get_synthesizer()
    try:
        res = synth.ingest_url(url=url, force_refresh=force_refresh)
        if res.get("status") == "already_indexed":
            return f"Documentation at '{url}' is already indexed and up-to-date (Doc ID: {res['document_id']})."

        chunks = res.get("chunks", [])
        topics = [c["topic"] for c in chunks]
        summary = (
            f"Successfully ingested and synthesized documentation from: {url}\n"
            f"- Document ID: {res['document_id']}\n"
            f"- Title: {res['title']}\n"
            f"- Synthesized Chunks: {res['chunks_count']}\n"
            f"- Topics Covered: {', '.join(topics)}"
        )
        return summary
    except Exception as e:
        logger.error(f"Failed to ingest URL {url}: {e}")
        return f"Error ingesting documentation from '{url}': {str(e)}"


# Standard OpenAI / Ollama Tool Definitions for Sabrina
RAG_TOOLS_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "query_rag",
            "description": "Query Sabrina's centralized RAG knowledge base for exact API syntax, method signatures, working code snippets, and library documentation before writing code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Technical query or library concept (e.g. 'CrewAI Agent definition', 'Playwright file upload syntax')."
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of documentation chunks to retrieve (default 4)."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_and_synthesize_docs",
            "description": "Autonomously download, clean, and synthesize external documentation from the web into canonical RAG chunks when encountering an unfamiliar framework or API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the documentation, tutorial, or API reference to ingest."
                    },
                    "force_refresh": {
                        "type": "boolean",
                        "description": "Whether to force re-ingestion if the URL was previously processed."
                    }
                },
                "required": ["url"]
            }
        }
    }
]


def execute_rag_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    """Dispatcher to execute a RAG tool call."""
    if tool_name == "query_rag":
        query = arguments.get("query", "")
        top_k = arguments.get("top_k", 4)
        return query_rag(query=query, top_k=top_k)
    elif tool_name == "fetch_and_synthesize_docs":
        url = arguments.get("url", "")
        force_refresh = arguments.get("force_refresh", False)
        return fetch_and_synthesize_docs(url=url, force_refresh=force_refresh)
    else:
        return f"Unknown RAG tool: {tool_name}"
