"""
model_telemetry_tracker.py
===========================
Diagnostic telemetry logger for Sabrina's model reasoning & verification loops.

Classifies whether model failures during finding verification stem from:
1. 'REFUSAL': Safety alignment refusal filters ("I cannot assist...", "against policy").
2. 'IGNORANT': Reasoning/comprehension failure, hallucination, or unparseable format without refusal.
3. 'EVALUATED': Valid, structured JSON verdict output (CONFIRMED or FALSE_POSITIVE).

Embeds telemetry interactions into Sabrina's centralized RAG database (SQLite + vector BLOB)
to enable semantic search and clustering of model failure modes.
"""

import os
import re
import json
import logging
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
import numpy as np

logger = logging.getLogger("sabrina.model_telemetry")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

REFUSAL_PATTERNS = [
    r"i cannot (?:fulfill|help|assist|provide|answer|comply)",
    r"i am (?:unable to|not able to|an ai)",
    r"as an ai(?: language model)?",
    r"i'm sorry, but i cannot",
    r"against (?:my )?safety polic(?:y|ies)",
    r"violates (?:openai|anthropic|safety)",
    r"unethical",
    r"illegal",
    r"security risk",
    r"cannot (?:create|assist with) (?:malware|hacking|exploits)",
]

class ModelDiagnosticClassifier:
    """Classifies model responses into REFUSAL, IGNORANT, or EVALUATED."""

    @staticmethod
    def classify(raw_response: str) -> Tuple[str, float, str]:
        """
        Returns (category, confidence, explanation):
        - 'REFUSAL': Triggered by safety phrases.
        - 'IGNORANT': Failed to produce valid verdict/JSON without refusal.
        - 'EVALUATED': Clean verdict extracted.
        """
        if not raw_response or not raw_response.strip():
            return ("IGNORANT", 1.0, "empty_response")

        text_lower = raw_response.lower()

        # 1. Check for refusal triggers
        for pattern in REFUSAL_PATTERNS:
            if re.search(pattern, text_lower):
                return ("REFUSAL", 0.95, f"matched_refusal_pattern: '{pattern}'")

        # 2. Check if valid JSON verdict is present
        try:
            # Check for markdown code blocks or raw JSON
            cleaned = raw_response
            match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw_response)
            if match:
                cleaned = match.group(1)
            
            data = json.loads(cleaned)
            if isinstance(data, dict) and "verdict" in data:
                verdict = str(data["verdict"]).upper()
                if verdict in ("CONFIRMED", "FALSE_POSITIVE", "NEEDS_REVIEW"):
                    return ("EVALUATED", 1.0, f"valid_verdict: {verdict}")
        except Exception:
            pass

        # If it contains no refusal but failed to produce a structured verdict
        return ("IGNORANT", 0.85, "reasoning_or_formatting_failure_without_refusal")


class TelemetryVectorLogger:
    """Logs model diagnostic interactions to RAG database with vector embeddings."""

    def __init__(self, ollama_url: Optional[str] = None, embed_model: str = "nomic-embed-text"):
        default_host = "127.0.0.1" if os.path.exists("/home/ego") else os.environ.get("LAB_HOST", "192.168.2.69")
        self.ollama_url = ollama_url or os.environ.get("OLLAMA_BASE_URL", f"http://{default_host}:11434")
        self.embed_model = embed_model
        self._rag_db = None

    def _get_rag_db(self):
        if self._rag_db is None:
            try:
                from tools.rag_database import RAGDatabase
                self._rag_db = RAGDatabase()
            except Exception as e:
                logger.warning(f"Could not initialize local RAGDatabase: {e}")
        return self._rag_db

    def get_embedding(self, text: str) -> Optional[np.ndarray]:
        """Fetch embedding from Ollama nomic-embed-text."""
        payload = {
            "model": self.embed_model,
            "prompt": text[:2048]
        }
        try:
            req = urllib.request.Request(
                f"{self.ollama_url}/api/embeddings",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
                emb = data.get("embedding")
                if emb:
                    return np.array(emb, dtype=np.float32)
        except Exception as e:
            logger.debug(f"Embedding request failed: {e}")
        return None

    def log_interaction(
        self,
        finding: dict,
        model_name: str,
        prompt_text: str,
        raw_response: str,
        resp_status: int,
    ) -> Dict[str, Any]:
        """
        Classifies response, embeds it, and stores the diagnostic record in RAG database.
        """
        category, confidence, explanation = ModelDiagnosticClassifier.classify(raw_response)
        
        doc_title = f"Diagnostic: {model_name} on {finding.get('url', 'unknown')} [{category}]"
        log_content = (
            f"=== DIAGNOSTIC METRIC ===\n"
            f"Model: {model_name}\n"
            f"Category: {category} (Confidence: {confidence:.2f})\n"
            f"Explanation: {explanation}\n"
            f"Target URL: {finding.get('url', 'N/A')}\n"
            f"HTTP Status: {resp_status}\n\n"
            f"--- PROMPT SNIPPET ---\n{prompt_text[:500]}\n\n"
            f"--- MODEL RESPONSE ---\n{raw_response}\n"
        )

        embedding = self.get_embedding(f"{category}: {raw_response[:800]}")

        rag_db = self._get_rag_db()
        record_id = None
        if rag_db:
            try:
                unique_url = f"telemetry://{model_name}/{finding.get('url', 'test')}/{os.urandom(4).hex()}"
                doc_id, _ = rag_db.insert_document(
                    url=unique_url,
                    title=doc_title,
                    raw_content=log_content,
                    source_type="telemetry_diagnostic"
                )
                chunk_id = rag_db.insert_chunk(
                    document_id=doc_id,
                    chunk_index=0,
                    topic=f"telemetry:{category.lower()}",
                    content=log_content,
                    embedding=embedding,
                    embedding_model=self.embed_model
                )
                record_id = chunk_id
            except Exception as e:
                logger.warning(f"Failed to persist diagnostic to RAG: {e}")

        return {
            "category": category,
            "confidence": confidence,
            "explanation": explanation,
            "record_id": record_id,
            "has_vector": embedding is not None
        }


# Global singleton instance
TELEMETRY_LOGGER = TelemetryVectorLogger()
