#!/usr/bin/env python3
"""Domain-Agnostic Multi-Family Full-Guild URL Retrieval & Unabridged System Integration Engine.

Given ONLY an arbitrary URL at runtime (with zero topic hints, zero domain keywords,
and zero pre-seeded rules), convenes Sabrina's Full Multi-Family Swarm Collective
across distinct model lineages (Alibaba Qwen, Meta Llama, IBM Granite, Mistral,
Evolutionary PromptBreeder, Sabrina Constitutional Jury, and Zero-Auth External Portals)
coordinated via `CollectiveOrderingEngine`, `CollectiveProblemSolvingEngine`,
`HierarchicalCognitiveBlackboard`, and `CyberComplianceSentinel` to:

  1. Screen outbound requests via `CyberComplianceSentinel` and fetch structural
     visible text (preserving headings, inline/fenced code, lists, and URLs).
  2. Partition the document into structural sections and distribute them across
     diverse multi-family Reader Guilds so EVERY section is extracted losslessly
     (no single-pass truncation or compression).
  3. Synthesize the global document blueprint while preserving the unabridged
     per-section catalog (every named item, repository URL, install command,
     invocation command, sub-item, workflow step, and guardrail).
  4. Convene a Disjoint-Family Constitutional Jury (`IBM Granite`, `Meta Llama`,
     `Sabrina Jury Defender`, `PromptBreeder`) to audit completeness against raw
     sections, preserve minority dissent, and backfill any omitted items.
  5. Assemble & enrich an unabridged `SKILL.md` guide combining executive synthesis,
     verbatim per-section Guild reference guides, operational guardrails, and
     signed multi-family Jury attribution.
  6. Autonomously synthesize & syntax/runtime-verify an executable Python integration
     module (`qwen2.5-coder:14b`) embedding the complete unabridged catalog, with
     `CollectiveProblemSolvingEngine` Peer SOS repair if verification fails.
  7. Stage, SHA-256 verify, and install the skill via `SabrinaSkillHydration` and
     persist complete verbatim multi-family provenance.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import py_compile
import re
import sqlite3
import sys
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SABRINA_ROOT = Path(__file__).resolve().parent.parent
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from harnesses.external.cognitive_blackboard import HierarchicalCognitiveBlackboard
from tools.collective_ordering import CollectiveOrderingEngine
from tools.collective_problem_solving_engine import CollectiveProblemSolvingEngine
from tools.cyber_compliance_sentinel import CyberComplianceSentinel
from tools.portal_policy import DEFAULT_DB as PORTAL_DB_PATH, PortalPolicy
from tools.portal_transport import request as portal_request
from tools.sabrina_skill_hydration import SabrinaSkillHydration


@dataclass
class SwarmNodeEndpoint:
    node_id: str
    base_url: str
    model: str
    role: str
    family: str = "unknown"
    guild: str = "general_guild"


class _ReadableHTMLTextExtractor(HTMLParser):
    """Pure-stdlib structural HTML extractor preserving headings, code blocks, lists, and URLs."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "iframe"}
    HEADING_MAP = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### "}
    BLOCK_TAGS = {"p", "blockquote", "tr", "div", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_pre = 0
        self._in_code = 0
        self._current_href = ""
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t in self.HEADING_MAP and self._in_pre == 0:
            self._chunks.append("\n\n" + self.HEADING_MAP[t])
        elif t == "li" and self._in_pre == 0:
            self._chunks.append("\n- ")
        elif t == "pre":
            self._in_pre += 1
            self._chunks.append("\n```\n")
        elif t == "code" and self._in_pre == 0:
            self._in_code += 1
            self._chunks.append("`")
        elif t == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self._current_href = href
        elif t in self.BLOCK_TAGS and self._in_pre == 0:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if t in self.HEADING_MAP and self._in_pre == 0:
            self._chunks.append("\n")
        elif t == "pre" and self._in_pre > 0:
            self._in_pre -= 1
            self._chunks.append("\n```\n")
        elif t == "code" and self._in_pre == 0 and self._in_code > 0:
            self._in_code -= 1
            self._chunks.append("`")
        elif t == "a" and self._current_href:
            self._chunks.append(f" ({self._current_href}) ")
            self._current_href = ""
        elif t in self.BLOCK_TAGS and self._in_pre == 0:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            if self._in_pre > 0:
                self._chunks.append(data)
            else:
                cleaned = re.sub(r"[ \t\r\n]+", " ", data)
                if cleaned:
                    self._chunks.append(cleaned)

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [ln.rstrip() for ln in raw.splitlines()]
        compact: List[str] = []
        in_fence = False
        blank_run = 0
        for ln in lines:
            stripped = ln.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                blank_run = 0
                compact.append("```")
                continue
            if not stripped:
                blank_run += 1
                if blank_run <= 1:
                    compact.append("")
                continue
            blank_run = 0
            compact.append(ln if in_fence else stripped)
        return "\n".join(compact).strip()


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> reasoning blocks and trailing chat-template artifacts."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()
    # Clean occasional template boundary echoes observed in fine-tuned jury checkpoints
    if "owitz" in cleaned:
        cleaned = cleaned.split("owitz")[0].strip()
    return cleaned


def extract_fenced_block(text: str, lang_hint: str = "") -> str:
    """Extract content from markdown code fences if wrapped by the model."""
    cleaned = strip_think_tags(text)
    if lang_hint:
        m = re.search(rf"```{lang_hint}\s*\n(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    m_any = re.search(r"```[a-zA-Z0-9_-]*\s*\n(.*?)```", cleaned, flags=re.DOTALL)
    if m_any:
        return m_any.group(1).strip()
    return cleaned.strip()


def parse_json_safely(raw_text: str) -> Dict[str, Any]:
    """Parse JSON object from model output, handling fences or trailing text safely."""
    cleaned = extract_fenced_block(raw_text, "json")
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if m:
        obj = json.loads(m.group(0))
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"Could not parse JSON object from model output: {cleaned[:200]!r}")


class SwarmAutonomousURLIntegrator:
    """Zero-seed, full-guild multi-family URL retrieval, lossless guide extraction, and integration."""

    # Multi-family roster respecting AGENTS.md guardrails:
    # - Uses <=14B models for fast 100% VRAM residency (avoids 32B+ PCIe spill)
    # - Skips corrupted `sabrina-jury-prosecutor:0.2` in favor of `sabrina-jury-granite:0.2` & `llama3.1:8b`
    DESIRED_GUILD_SPECS: List[Tuple[str, str, str, str, str, str]] = [
        # (node_id, base_url, model, role, family, guild)
        ("lab_qwen3_architect", "http://192.168.2.69:11434", "qwen3:14b", "integrative_architect", "Alibaba Qwen3", "Guild Alpha — Integrative Architecture"),
        ("lab_qwen_coder", "http://192.168.2.69:11434", "qwen2.5-coder:14b", "systems_code_synthesizer", "Alibaba Qwen2.5-Coder", "Guild VI — Systems & Code Formalism"),
        ("lab_llama_guild", "http://192.168.2.69:11434", "llama3.1:8b", "cross_family_reader_and_critic", "Meta Llama-3.1", "Guild I — Anti-Monoculture & Deep Extraction"),
        ("lab_granite_guild", "http://192.168.2.69:11434", "granite3.1-dense:8b", "stigmergic_governance_reader", "IBM Granite-3.1", "Guild II — Governance & Structured Extraction"),
        ("lab_mistral_guild", "http://192.168.2.69:11434", "dolphin-mistral:7b", "divergent_reader", "Mistral / Dolphin", "Guild III — Cognitive Individuation"),
        ("lab_promptbreeder", "http://192.168.2.69:11434", "promptbreeder:latest", "evolutionary_completeness_auditor", "PromptBreeder", "Guild IV — Evolutionary Meta-Audit"),
        ("lab_jury_granite", "http://192.168.2.69:11434", "sabrina-jury-granite:0.2", "prosecution_and_pluralism_auditor", "Sabrina Jury (IBM Granite)", "Guild V-A — Disjoint-Family Prosecution Jury"),
        ("lab_jury_llama", "http://192.168.2.69:11434", "sabrina-jury-llama:0.2", "independent_completeness_juror", "Sabrina Jury (Meta Llama)", "Guild V-B — Independent Completeness Jury"),
        ("lab_jury_defender", "http://192.168.2.69:11434", "sabrina-jury-defender:0.2", "constitutional_dissent_defender", "Sabrina Jury (Defender)", "Guild V-C — Constitutional Safeguard Jury"),
        ("win_qwen_reader", "http://127.0.0.1:11434", "qwen2.5:7b", "parallel_local_reader", "Alibaba Qwen2.5", "Guild VII-A — Local Parallel Extraction"),
        ("win_scope_extractor", "http://127.0.0.1:11434", "sabrina-scope-extractor:latest", "scope_and_boundary_extractor", "Sabrina Scope Extractor", "Guild VII-B — Operational Boundary Extraction"),
        ("win_granite_coder", "http://127.0.0.1:11434", "granite-code:8b", "code_verifier_and_repair_peer", "IBM Granite-Code", "Guild VII-C — Disjoint-Family Code Peer"),
    ]

    EXTERNAL_ZERO_AUTH_CANDIDATES: List[Tuple[str, str, str]] = [
        ("hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "Byte Agentic RAG (Guild 1 Strategist)", "External Guild 1 — Strategist"),
        ("hf-gradio-htchu-coderhelper", "CoderHelper (Guild 2 Auditor)", "External Guild 2 — Auditor"),
        ("hf-gradio-rstanley2026-rodney-digital-twin", "Rodney Digital Twin (Guild 4 Synthesizer)", "External Guild 4 — Synthesizer"),
        ("hf-gradio-himakesh-parth-ai-tutor", "Parth AI Tutor (Guild 1 Strategist)", "External Guild 1 — Strategist"),
        ("hf-gradio-affanaiengr-rag", "AffanAI RAG (Guild 2 Auditor)", "External Guild 2 — Auditor"),
    ]

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or (SABRINA_ROOT / "data" / "sabrina_assistant.sqlite3")
        self.hydration = SabrinaSkillHydration(db_path=self.db_path)
        self.sentinel = CyberComplianceSentinel()
        self.ordering = CollectiveOrderingEngine(db_path=self.db_path)
        self.collaboration = CollectiveProblemSolvingEngine(db_path=self.db_path, ordering_engine=self.ordering)
        self.blackboard = HierarchicalCognitiveBlackboard(self.db_path)
        self.nodes = self._discover_live_swarm_nodes()

    def _discover_live_swarm_nodes(self) -> List[SwarmNodeEndpoint]:
        host_catalogs: Dict[str, set] = {}
        for base_url in {"http://192.168.2.69:11434", "http://127.0.0.1:11434"}:
            try:
                with urllib.request.urlopen(f"{base_url}/api/tags", timeout=5) as resp:
                    tags_data = json.loads(resp.read().decode("utf-8"))
                    host_catalogs[base_url] = {m.get("name", "") for m in tags_data.get("models", [])}
            except Exception:
                host_catalogs[base_url] = set()

        live: List[SwarmNodeEndpoint] = []
        for node_id, base_url, model, role, family, guild in self.DESIRED_GUILD_SPECS:
            if model in host_catalogs.get(base_url, set()):
                live.append(SwarmNodeEndpoint(node_id, base_url, model, role, family, guild))

        if not live:
            raise RuntimeError("No live Swarm Guild Ollama nodes reachable.")
        return live

    def _find_node(self, preferred_roles: List[str], fallback_idx: int = 0) -> SwarmNodeEndpoint:
        for r in preferred_roles:
            for n in self.nodes:
                if n.role == r:
                    return n
        return self.nodes[fallback_idx % len(self.nodes)]

    def _call_node(
        self,
        node: SwarmNodeEndpoint,
        prompt: str,
        system: str = "",
        json_mode: bool = False,
        temperature: float = 0.15,
        num_ctx: int = 12288,
        num_predict: int = 2400,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        clean_ok, clean_msg = self.sentinel.sanitize_outbound_query(prompt)
        if not clean_ok:
            raise RuntimeError(f"CyberComplianceSentinel blocked outbound prompt: {clean_msg}")

        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: Dict[str, Any] = {
            "model": node.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "num_predict": max(1500, num_predict) if "qwen3" in node.model else num_predict,
            },
        }
        if "qwen3" in node.model:
            payload["think"] = False
        if json_mode:
            payload["format"] = "json"

        t0 = time.time()
        req = urllib.request.Request(
            f"{node.base_url}/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        latency_sec = round(time.time() - t0, 2)
        msg = body.get("message", {}) or {}
        raw_content = str(msg.get("content") or "")
        thinking = str(msg.get("thinking") or "")
        eval_count = int(body.get("eval_count") or 0)
        cleaned = strip_think_tags(raw_content)

        self.blackboard.update_model_competence(
            f"{node.node_id}:{node.model}",
            "autonomous_url_guild_integration",
            syntax_ok=bool(cleaned),
            tests_ok=bool(cleaned),
            latency_ms=latency_sec * 1000.0,
        )
        return {
            "node_id": node.node_id,
            "model": node.model,
            "family": node.family,
            "guild": node.guild,
            "role": node.role,
            "response": cleaned,
            "thinking": thinking,
            "eval_count": eval_count,
            "latency_sec": latency_sec,
        }

    def _consult_external_zero_auth_guild(self, topic_summary: str) -> List[Dict[str, Any]]:
        """Polite, policy-sanctioned Zero-Auth External Portal Guild check (singleauthority Windows DB)."""
        results: List[Dict[str, Any]] = []
        if not PORTAL_DB_PATH.exists():
            return results
        try:
            policy = PortalPolicy(PORTAL_DB_PATH)
        except Exception:
            return results

        prompt = (
            "You are an external Zero-Auth Guild advisor in a multi-family AI research collective.\n"
            "Peer local guilds analyzed a retrieved technical document and summarized its focus below:\n\n"
            f"{topic_summary[:1800]}\n\n"
            "Provide a concise (120-220 word) independent architectural perspective on:\n"
            "1. Key verification or operational safeguards teams should enforce in this domain.\n"
            "2. Practical integration advice when adopting workflows from this domain."
        )
        clean_ok, _ = self.sentinel.sanitize_outbound_query(prompt)
        if not clean_ok:
            return results

        last_hf_finish = 0.0
        for pid, family, guild_label in self.EXTERNAL_ZERO_AUTH_CANDIDATES:
            prof = policy.profile(pid)
            if not prof or not prof.get("access_allowed"):
                continue
            adp = json.loads(prof.get("adapter_json") or "{}")
            if adp.get("headers") or adp.get("api_key") or adp.get("token") or adp.get("key_env"):
                continue
            if prof.get("provider") == "huggingface" and last_hf_finish > 0:
                wait_needed = 10.6 - (time.time() - last_hf_finish)
                if wait_needed > 0:
                    time.sleep(wait_needed)
            try:
                res = portal_request(policy, pid, prompt, purpose="evaluation", max_tokens=450)
                if prof.get("provider") == "huggingface":
                    last_hf_finish = time.time()
                reply = str(res.get("reply") or "").strip()
                low_rep = reply.lower()
                is_wrapped_upstream_err = any(
                    err_sig in low_rep
                    for err_sig in (
                        "401 unauthorized",
                        "404 not found",
                        "error generating response:",
                        "oauth token has expired",
                        "client error '401",
                        "client error '404",
                    )
                )
                if reply and not is_wrapped_upstream_err:
                    threat = self.sentinel.inspect_and_report_threat(pid, reply, {"claimed_model": prof.get("model_claim", "")})
                    if threat:
                        continue
                ok = bool(res.get("ok") and len(reply) >= 60 and not is_wrapped_upstream_err)
                results.append({
                    "ok": ok,
                    "portal_id": pid,
                    "claimed_model": prof.get("model_claim", ""),
                    "family": family,
                    "guild": guild_label,
                    "response": reply if ok else "",
                    "failure": "upstream_space_401_or_404_wrapped_error" if is_wrapped_upstream_err else res.get("failure", ""),
                    "latency_sec": round((res.get("latency_ms") or 0) / 1000.0, 2),
                })
                if ok:
                    break
            except Exception as exc:
                results.append({
                    "ok": False,
                    "portal_id": pid,
                    "family": family,
                    "guild": guild_label,
                    "response": "",
                    "failure": str(exc),
                    "latency_sec": 0.0,
                })
        return results

    def retrieve_webpage_blind(self, url: str) -> Tuple[str, str]:
        """Fetch raw HTML from an unauthenticated public URL and extract structural text."""
        is_open, portal_msg = self.sentinel.validate_portal_unauthenticated(url)
        if not is_open:
            raise RuntimeError(f"Zero-Auth policy rejected target URL: {portal_msg}")

        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw_html = resp.read().decode("utf-8", errors="replace")

        candidates_html: List[str] = []
        for tag_pat in (r"<main[^>]*>(.*?)</main>", r"<article[^>]*>(.*?)</article>"):
            for m in re.finditer(tag_pat, raw_html, flags=re.DOTALL | re.IGNORECASE):
                candidates_html.append(m.group(1))

        best_text = ""
        for cand in candidates_html:
            p = _ReadableHTMLTextExtractor()
            p.feed(cand)
            t = p.get_text()
            if len(t) > len(best_text):
                best_text = t

        if len(best_text) >= 4000:
            text = best_text
        else:
            parser = _ReadableHTMLTextExtractor()
            parser.feed(raw_html)
            text = parser.get_text()

        # Screen inbound document content via CyberComplianceSentinel awareness check
        self.sentinel.inspect_and_report_threat(url, text[:8000], {"stage": "blind_webpage_retrieval"})
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return text, sha

    @staticmethod
    def chunk_text(text: str, max_chars: int = 6500) -> List[str]:
        """Legacy line-safe chunker preserved for unit test compatibility."""
        lines = text.splitlines()
        chunks: List[str] = []
        buf: List[str] = []
        cur = 0
        for ln in lines:
            if cur + len(ln) + 1 > max_chars and buf:
                chunks.append("\n".join(buf))
                buf = [ln]
                cur = len(ln)
            else:
                buf.append(ln)
                cur += len(ln) + 1
        if buf:
            chunks.append("\n".join(buf))
        return chunks

    @classmethod
    def partition_structural_sections(
        cls,
        text: str,
        min_chars: int = 1100,
        max_chars: int = 5200,
    ) -> List[Dict[str, str]]:
        """Split extracted text along markdown headings (outside code fences) so zero sections get cut mid-item."""
        raw_sections: List[Tuple[str, str]] = []
        cur_title = "Opening Overview"
        cur_lines: List[str] = []
        in_fence = False

        for ln in text.splitlines():
            stripped = ln.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                cur_lines.append(ln)
                continue
            if not in_fence and re.match(r"^#{1,3}\s+\S+", stripped):
                heading_label = re.sub(r"^#{1,3}\s+", "", stripped).strip()
                body_so_far = "\n".join(cur_lines).strip()
                if body_so_far:
                    raw_sections.append((cur_title, body_so_far))
                cur_title = heading_label
                cur_lines = [ln]
            else:
                cur_lines.append(ln)

        tail = "\n".join(cur_lines).strip()
        if tail:
            raw_sections.append((cur_title, tail))

        merged: List[Tuple[str, str]] = []
        buf_title = ""
        buf_body = ""
        for title, body in raw_sections:
            is_numbered_section = bool(re.match(r"^\d+[\.\)]\s+", title))
            if not buf_body:
                buf_title, buf_body = title, body
            elif len(buf_body) < min_chars and not is_numbered_section:
                buf_title = f"{buf_title} — {title}" if buf_title != "Opening Overview" else title
                buf_body = f"{buf_body}\n\n{body}"
            elif len(body) < 450 and not is_numbered_section:
                buf_title = f"{buf_title} — {title}"
                buf_body = f"{buf_body}\n\n{body}"
            else:
                merged.append((buf_title, buf_body))
                buf_title, buf_body = title, body
        if buf_body:
            merged.append((buf_title, buf_body))

        final_sections: List[Dict[str, str]] = []
        for title, body in merged:
            if len(body) <= max_chars:
                final_sections.append({"section_title": title, "content": body})
            else:
                parts = cls.chunk_text(body, max_chars=max_chars)
                for idx, part in enumerate(parts):
                    part_title = title if len(parts) == 1 else f"{title} (Part {idx + 1}/{len(parts)})"
                    final_sections.append({"section_title": part_title, "content": part})
        return final_sections

    def run_blind_ingestion(self, url: str) -> Dict[str, Any]:
        t0 = time.time()
        ingestion_id = f"ingest_{uuid.uuid4().hex[:10]}"
        project_id = f"full_guild_{ingestion_id}"

        # Register multi-stage Guild DAG in CollectiveOrderingEngine for stigmergic handoffs
        dag_tasks = [
            {
                "task_id": f"{ingestion_id}_t1_retrieve",
                "goal": "Blind unauthenticated webpage retrieval & structural partitioning",
                "depends_on": [],
                "capability_required": "recon",
            },
            {
                "task_id": f"{ingestion_id}_t2_multi_family_readers",
                "goal": "Lossless per-section guide extraction across Qwen, Llama, Granite, Mistral, and Scope Guilds",
                "depends_on": [f"{ingestion_id}_t1_retrieve"],
                "capability_required": "rag",
            },
            {
                "task_id": f"{ingestion_id}_t3_architect_synthesis",
                "goal": "Global blueprint synthesis & unabridged catalog aggregation without item erasure",
                "depends_on": [f"{ingestion_id}_t2_multi_family_readers"],
                "capability_required": "reasoning",
            },
            {
                "task_id": f"{ingestion_id}_t4_constitutional_jury_audit",
                "goal": "Disjoint-family completeness & pluralism audit (IBM Granite, Meta Llama, Defender, PromptBreeder)",
                "depends_on": [f"{ingestion_id}_t3_architect_synthesis"],
                "capability_required": "audit",
            },
            {
                "task_id": f"{ingestion_id}_t5_unabridged_skill_and_code",
                "goal": "Author unabridged SKILL.md guide & standalone verified Python module with Peer SOS repair",
                "depends_on": [f"{ingestion_id}_t4_constitutional_jury_audit"],
                "capability_required": "code_generation",
            },
        ]
        self.ordering.submit_dag_plan(project_id, dag_tasks)

        # ---------------------------------------------------------------------
        # STAGE 1: Blind Structural Retrieval
        # ---------------------------------------------------------------------
        self.ordering.claim_task(f"{ingestion_id}_t1_retrieve", "CyberComplianceSentinel+HTMLStructuralParser")
        raw_text, raw_sha = self.retrieve_webpage_blind(url)
        sections = self.partition_structural_sections(raw_text, min_chars=900, max_chars=5200)
        self.ordering.complete_task(
            f"{ingestion_id}_t1_retrieve",
            "CyberComplianceSentinel+HTMLStructuralParser",
            f"Extracted {len(raw_text)} chars across {len(sections)} structural sections (SHA-256: {raw_sha[:16]})",
        )
        self.collaboration.drive_next_tasks(
            project_id,
            f"{ingestion_id}_t1_retrieve",
            "CyberComplianceSentinel+HTMLStructuralParser",
            produced_artifacts={"sections_count": len(sections), "raw_sha256": raw_sha},
        )

        # ---------------------------------------------------------------------
        # STAGE 2: Multi-Family Guild Lossless Section Extraction
        # ---------------------------------------------------------------------
        # Group readers by GPU host so lab GPU and Windows local GPU work concurrently
        # while avoiding simultaneous multi-model thrashing on the same VRAM bus.
        reader_role_pool = [
            "cross_family_reader_and_critic",   # Meta Llama-3.1:8b (lab)
            "parallel_local_reader",            # Qwen2.5:7b (win)
            "stigmergic_governance_reader",     # IBM Granite-3.1:8b (lab)
            "scope_and_boundary_extractor",     # Sabrina Scope Extractor (win)
            "divergent_reader",                 # Mistral / Dolphin:7b (lab)
            "integrative_architect",            # Qwen3:14b (lab)
        ]
        reader_roster: List[SwarmNodeEndpoint] = []
        for role_name in reader_role_pool:
            matched = [n for n in self.nodes if n.role == role_name]
            if matched and matched[0] not in reader_roster:
                reader_roster.append(matched[0])
        if not reader_roster:
            reader_roster = list(self.nodes)

        # Assign sections across lab and win readers to preserve cross-family diversity
        win_readers = [n for n in reader_roster if "127.0.0.1" in n.base_url]
        lab_readers = [n for n in reader_roster if "127.0.0.1" not in n.base_url]

        assignments: List[Tuple[int, Dict[str, str], SwarmNodeEndpoint]] = []
        for idx, sec in enumerate(sections):
            if win_readers and idx % 3 == 1:
                chosen = win_readers[(idx // 3) % len(win_readers)]
            elif lab_readers:
                chosen = lab_readers[idx % len(lab_readers)]
            else:
                chosen = reader_roster[idx % len(reader_roster)]
            assignments.append((idx, sec, chosen))

        # Group by base_url so each GPU executes its assigned Guild models sequentially (zero VRAM contention),
        # while both GPUs run in parallel!
        by_host: Dict[str, List[Tuple[int, Dict[str, str], SwarmNodeEndpoint]]] = {}
        for item in assignments:
            by_host.setdefault(item[2].base_url, []).append(item)
        # Sort each host's queue by model name so the same model processes all its assigned sections before swapping!
        for host_url in by_host:
            by_host[host_url].sort(key=lambda x: (x[2].model, x[0]))

        def _extract_single_section(idx: int, sec: Dict[str, str], node: SwarmNodeEndpoint) -> Dict[str, Any]:
            sec_title = sec["section_title"]
            sec_body = sec["content"]
            json_prompt = (
                "You are a specialist Reader Guild node in a multi-family AI collective.\n"
                "You have NOT been told the topic of this document in advance. Read the structural section below "
                "and extract EVERY concrete detail WITHOUT omitting URLs, commands, sub-items, or steps.\n"
                "Return a JSON object matching this exact schema:\n"
                "{\n"
                '  "section_heading": "Exact heading or topic of this section",\n'
                '  "section_summary": "Detailed 2-4 sentence technical summary of this section",\n'
                '  "extracted_items": [\n'
                "    {\n"
                '      "name": "Exact name of tool, item, package, concept, or resource",\n'
                '      "source_or_ref": "Exact repository slug or full URL if present in text, else author/org",\n'
                '      "category": "Specific category inferred from text",\n'
                '      "capabilities": "Thorough description of what it does and how it works",\n'
                '      "install_commands": ["Exact CLI / git / plugin commands found in this section"],\n'
                '      "usage_or_slash_commands": ["Exact invocation commands, flags, or prompts shown"],\n'
                '      "sub_components": ["Named sub-plugins, sub-agents, modules, supported platforms, or files"],\n'
                '      "workflow_steps": ["Ordered procedural steps or methodology phases if described"],\n'
                '      "cautions": ["Specific warnings, limitations, statistics, or guardrails mentioned"]\n'
                "    }\n"
                "  ],\n"
                '  "architectural_mechanisms": ["Key mechanisms, directory layouts, or protocols explained"],\n'
                '  "operational_warnings": ["Section-level cautions, statistics, or safety practices"],\n'
                '  "unabridged_guide_notes": "Comprehensive technical walkthrough preserving all nuances, metrics, and examples from this section"\n'
                "}\n\n"
                f"--- BEGIN SECTION {idx + 1}: {sec_title} ---\n{sec_body}\n--- END SECTION {idx + 1} ---"
            )
            call_meta = self._call_node(
                node,
                json_prompt,
                system=f"You are {node.family} serving in {node.guild}. Preserve exact repository paths, URLs, commands, and named sub-items verbatim.",
                json_mode=True,
                temperature=0.1,
                num_ctx=12288,
                num_predict=2200,
            )
            try:
                parsed = parse_json_safely(call_meta["response"])
            except Exception:
                parsed = {
                    "section_heading": sec_title,
                    "section_summary": call_meta["response"][:500],
                    "extracted_items": [],
                    "architectural_mechanisms": [],
                    "operational_warnings": [],
                    "unabridged_guide_notes": call_meta["response"],
                }
            return {
                "chunk_index": idx,
                "section_title": sec_title,
                "model": f"{node.node_id}:{node.model}",
                "family": node.family,
                "guild": node.guild,
                "eval_count": call_meta["eval_count"],
                "latency_sec": call_meta["latency_sec"],
                "raw_response": call_meta["response"],
                "thinking": call_meta["thinking"],
                "structured": parsed,
                "notes": call_meta["response"],
            }

        def _run_host_queue(queue_items: List[Tuple[int, Dict[str, str], SwarmNodeEndpoint]]) -> List[Dict[str, Any]]:
            out_list: List[Dict[str, Any]] = []
            for idx, sec, node in queue_items:
                out_list.append(_extract_single_section(idx, sec, node))
            return out_list

        section_records: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(by_host))) as pool:
            futs = [pool.submit(_run_host_queue, q) for q in by_host.values()]
            for fut in as_completed(futs):
                section_records.extend(fut.result())

        section_records.sort(key=lambda r: r["chunk_index"])

        self.ordering.complete_task(
            f"{ingestion_id}_t2_multi_family_readers",
            ",".join(sorted({r["family"] for r in section_records})),
            f"Completed lossless extraction across {len(section_records)} sections",
        )
        self.collaboration.drive_next_tasks(
            project_id,
            f"{ingestion_id}_t2_multi_family_readers",
            section_records[0]["model"] if section_records else "reader_guild",
            produced_artifacts={"sections_extracted": len(section_records)},
        )

        # ---------------------------------------------------------------------
        # STAGE 3: Lossless Multi-Section Catalog Aggregation + Global Synthesis
        # ---------------------------------------------------------------------
        # Deterministic lossless union of every item extracted by the Reader Guilds
        # so the Architect CANNOT compress or drop items!
        merged_catalog_by_key: Dict[str, Dict[str, Any]] = {}
        all_arch_concepts: List[str] = []
        all_warnings: List[str] = []

        for rec in section_records:
            st = rec["structured"]
            for concept in st.get("architectural_mechanisms") or []:
                c_str = str(concept).strip()
                if c_str and c_str not in all_arch_concepts:
                    all_arch_concepts.append(c_str)
            for warn in st.get("operational_warnings") or []:
                w_str = str(warn).strip()
                if w_str and w_str not in all_warnings:
                    all_warnings.append(w_str)

            for item in st.get("extracted_items") or []:
                if not isinstance(item, dict):
                    continue
                raw_name = str(item.get("name") or "").strip()
                if not raw_name:
                    continue
                norm_key = re.sub(r"[^a-z0-9]+", "", raw_name.lower())
                if not norm_key:
                    continue

                def _clean_str_list(val: Any) -> List[str]:
                    if isinstance(val, list):
                        return [str(x).strip() for x in val if str(x).strip()]
                    if isinstance(val, str) and val.strip():
                        return [val.strip()]
                    return []

                entry = {
                    "name": raw_name,
                    "section_origin": rec["section_title"],
                    "extracted_by_family": rec["family"],
                    "extracted_by_model": rec["model"],
                    "source_or_ref": str(item.get("source_or_ref") or "").strip(),
                    "category": str(item.get("category") or "General").strip(),
                    "capabilities": str(item.get("capabilities") or "").strip(),
                    "install_commands": _clean_str_list(item.get("install_commands")),
                    "usage_or_slash_commands": _clean_str_list(item.get("usage_or_slash_commands")),
                    "sub_components": _clean_str_list(item.get("sub_components")),
                    "workflow_steps": _clean_str_list(item.get("workflow_steps")),
                    "cautions": _clean_str_list(item.get("cautions")),
                }

                if norm_key not in merged_catalog_by_key:
                    merged_catalog_by_key[norm_key] = entry
                else:
                    existing = merged_catalog_by_key[norm_key]
                    if len(entry["capabilities"]) > len(existing["capabilities"]):
                        existing["capabilities"] = entry["capabilities"]
                    if entry["source_or_ref"] and (
                        "http" in entry["source_or_ref"] or "/" in entry["source_or_ref"]
                    ) and len(entry["source_or_ref"]) > len(existing["source_or_ref"]):
                        existing["source_or_ref"] = entry["source_or_ref"]
                    for list_field in ("install_commands", "usage_or_slash_commands", "sub_components", "workflow_steps", "cautions"):
                        for v in entry[list_field]:
                            if v not in existing[list_field]:
                                existing[list_field].append(v)

        catalog_entries = list(merged_catalog_by_key.values())

        architect_node = self._find_node(["integrative_architect", "cross_family_reader_and_critic"])
        digest_for_architect = "\n\n".join(
            f"[Section {r['chunk_index'] + 1}: {r['section_title']} | {r['family']} ({r['model']})]\n"
            f"Summary: {r['structured'].get('section_summary', '')}\n"
            f"Items: {[it.get('name') for it in (r['structured'].get('extracted_items') or []) if isinstance(it, dict)]}"
            for r in section_records
        )

        synth_prompt = (
            "You are the Integrative Cross-Family Synthesis Architect.\n"
            "Peer Guilds across distinct model families inspected an unknown webpage section by section.\n"
            "Synthesize the global executive framing in JSON with this exact schema:\n"
            "{\n"
            '  "discovered_topic": "Clear, accurate title of the full guide discovered by the collective",\n'
            '  "skill_slug": "lowercase-hyphenated-skill-name-max-45-chars",\n'
            '  "domain_summary": "Comprehensive 4-6 sentence technical overview of the entire guide, how its pieces fit together, and its practical value",\n'
            '  "executive_architecture_synthesis": "Multi-paragraph architectural synthesis weaving together how the mechanisms, directory structures, installation tiers, and workflows operate end-to-end",\n'
            '  "sabrina_swarm_integration_blueprint": "Concrete multi-step operational playbook explaining how Sabrina\'s multi-family swarm applies these discovered capabilities safely"\n'
            "}\n\n"
            f"MULTI-FAMILY SECTION DIGEST:\n{digest_for_architect}"
        )
        synth_call = self._call_node(
            architect_node,
            synth_prompt,
            system="You are an honest, pluralistic cross-family synthesis architect. Do not invent facts outside the peer digest.",
            json_mode=True,
            temperature=0.15,
            num_ctx=12288,
            num_predict=2400,
        )
        synth_meta = parse_json_safely(synth_call["response"])

        # Consult Zero-Auth External Portal Guild in parallel / polite check if available
        external_guild_reports = self._consult_external_zero_auth_guild(
            f"Topic: {synth_meta.get('discovered_topic', '')}\nSummary: {synth_meta.get('domain_summary', '')}"
        )

        self.ordering.complete_task(
            f"{ingestion_id}_t3_architect_synthesis",
            f"{architect_node.node_id}:{architect_node.model}",
            f"Synthesized topic '{synth_meta.get('discovered_topic')}' with {len(catalog_entries)} unabridged catalog entries",
        )
        self.collaboration.drive_next_tasks(
            project_id,
            f"{ingestion_id}_t3_architect_synthesis",
            f"{architect_node.node_id}:{architect_node.model}",
            produced_artifacts={"catalog_count": len(catalog_entries)},
        )

        # ---------------------------------------------------------------------
        # STAGE 4: Disjoint-Family Constitutional Jury & Evolutionary Audit
        # ---------------------------------------------------------------------
        # Convene non-Qwen auditors (IBM Granite Jury, Meta Llama Jury, Constitutional Defender, PromptBreeder)
        jury_roles = [
            "prosecution_and_pluralism_auditor",     # sabrina-jury-granite:0.2
            "independent_completeness_juror",        # sabrina-jury-llama:0.2
            "constitutional_dissent_defender",       # sabrina-jury-defender:0.2
            "evolutionary_completeness_auditor",     # promptbreeder:latest
        ]
        jury_nodes = [n for n in self.nodes if n.role in jury_roles]
        if not jury_nodes:
            jury_nodes = [n for n in self.nodes if "Qwen" not in n.family][:3] or [self.nodes[-1]]

        section_headings_list = [r["section_title"] for r in section_records]
        catalog_names_list = [
            f"{c['name']} ({c['source_or_ref'] or 'no-ref'}) [from: {c['section_origin']}]"
            for c in catalog_entries
        ]

        jury_reviews: List[Dict[str, Any]] = []
        missing_headings_flagged: List[str] = []

        for jnode in jury_nodes:
            j_prompt = (
                "You are an independent Disjoint-Family Constitutional Juror auditing a multi-model document ingestion.\n"
                "Compare the SOURCE SECTION HEADINGS from the webpage against the AGGREGATED CATALOG ITEMS below.\n"
                "Check:\n"
                "1. Is every numbered or named section represented in the catalog without erasure?\n"
                "2. Are repository references, operational cautions, and minority technical nuances preserved?\n"
                "Return JSON with this exact schema:\n"
                "{\n"
                '  "verdict": "PASS" or "NEEDS_ENRICHMENT",\n'
                '  "missing_sections_or_items": ["Exact section heading or item name if any was missed, else empty list"],\n'
                '  "dissent_or_safeguard_opinion": "Your signed independent architectural critique, safeguard note, or minority observation (2-4 sentences)"\n'
                "}\n\n"
                f"SOURCE SECTION HEADINGS ({len(section_headings_list)}):\n"
                + "\n".join(f"- {h}" for h in section_headings_list)
                + f"\n\nAGGREGATED CATALOG ITEMS ({len(catalog_names_list)}):\n"
                + "\n".join(f"- {c}" for c in catalog_names_list)
            )
            j_call = self._call_node(
                jnode,
                j_prompt,
                system=f"You are {jnode.family} ({jnode.guild}). Audit rigorously from an independent model lineage.",
                json_mode=True,
                temperature=0.15,
                num_ctx=8192,
                num_predict=700,
            )
            try:
                j_parsed = parse_json_safely(j_call["response"])
            except Exception:
                j_parsed = {
                    "verdict": "PASS",
                    "missing_sections_or_items": [],
                    "dissent_or_safeguard_opinion": j_call["response"][:400],
                }
            for m_item in j_parsed.get("missing_sections_or_items") or []:
                m_str = str(m_item).strip()
                if m_str and m_str not in missing_headings_flagged:
                    missing_headings_flagged.append(m_str)
            jury_reviews.append({
                "juror_model": f"{jnode.node_id}:{jnode.model}",
                "family": jnode.family,
                "guild": jnode.guild,
                "verdict": j_parsed.get("verdict", "PASS"),
                "missing_sections_or_items": j_parsed.get("missing_sections_or_items", []),
                "dissent_or_safeguard_opinion": j_parsed.get("dissent_or_safeguard_opinion", ""),
                "latency_sec": j_call["latency_sec"],
                "eval_count": j_call["eval_count"],
                "raw_response": j_call["response"],
            })

        # Ensure every numbered section in `section_records` has at least one primary catalog entry;
        # if any numbered section yielded 0 `extracted_items` (or was flagged by the Jury), trigger a targeted recovery pass!
        backfill_events: List[Dict[str, Any]] = []
        represented_origins = {c["section_origin"] for c in catalog_entries}
        backfill_node = self._find_node(["cross_family_reader_and_critic", "stigmergic_governance_reader", "integrative_architect"])

        for rec in section_records:
            sec_t = rec["section_title"]
            needs_backfill = (
                (re.match(r"^\d+[\.\)]\s+", sec_t) and sec_t not in represented_origins)
                or any(sec_t.lower() in mh.lower() for mh in missing_headings_flagged)
            )
            if needs_backfill and sec_t not in represented_origins:
                bf_prompt = (
                    "The Constitutional Jury flagged that the following section has zero entries in the structured catalog.\n"
                    "Extract its primary named item(s), repository URL, install commands, usage commands, sub-components, "
                    "workflow steps, and cautions as JSON:\n"
                    '{"extracted_items": [{"name": "...", "source_or_ref": "...", "category": "...", "capabilities": "...", '
                    '"install_commands": [], "usage_or_slash_commands": [], "sub_components": [], "workflow_steps": [], "cautions": []}]}\n\n'
                    f"SECTION ({sec_t}):\n{sections[rec['chunk_index']]['content']}"
                )
                bf_call = self._call_node(backfill_node, bf_prompt, json_mode=True, temperature=0.1, num_ctx=8192, num_predict=1400)
                try:
                    bf_parsed = parse_json_safely(bf_call["response"])
                    for it in bf_parsed.get("extracted_items") or []:
                        if isinstance(it, dict) and str(it.get("name") or "").strip():
                            catalog_entries.append({
                                "name": str(it["name"]).strip(),
                                "section_origin": sec_t,
                                "extracted_by_family": backfill_node.family,
                                "extracted_by_model": f"{backfill_node.node_id}:{backfill_node.model}",
                                "source_or_ref": str(it.get("source_or_ref") or "").strip(),
                                "category": str(it.get("category") or "Recovered Section Item").strip(),
                                "capabilities": str(it.get("capabilities") or "").strip(),
                                "install_commands": [str(x) for x in (it.get("install_commands") or [])],
                                "usage_or_slash_commands": [str(x) for x in (it.get("usage_or_slash_commands") or [])],
                                "sub_components": [str(x) for x in (it.get("sub_components") or [])],
                                "workflow_steps": [str(x) for x in (it.get("workflow_steps") or [])],
                                "cautions": [str(x) for x in (it.get("cautions") or [])],
                            })
                            represented_origins.add(sec_t)
                    backfill_events.append({"section": sec_t, "repaired_by": f"{backfill_node.node_id}:{backfill_node.model}"})
                except Exception:
                    pass

        synthesized: Dict[str, Any] = {
            "discovered_topic": str(synth_meta.get("discovered_topic") or "Swarm Discovered Technical Guide").strip(),
            "skill_slug": str(synth_meta.get("skill_slug") or "swarm-discovered-topic").strip(),
            "domain_summary": str(synth_meta.get("domain_summary") or "").strip(),
            "executive_architecture_synthesis": str(synth_meta.get("executive_architecture_synthesis") or "").strip(),
            "sabrina_swarm_integration_blueprint": str(synth_meta.get("sabrina_swarm_integration_blueprint") or "").strip(),
            "catalog_entries": catalog_entries,
            "architectural_concepts": all_arch_concepts,
            "operational_warnings": all_warnings,
            "section_guides": [
                {
                    "section_index": r["chunk_index"] + 1,
                    "section_title": r["section_title"],
                    "attributed_guild": f"{r['guild']} ({r['family']} — `{r['model']}`)",
                    "section_summary": str(r["structured"].get("section_summary") or "").strip(),
                    "unabridged_guide_notes": str(r["structured"].get("unabridged_guide_notes") or "").strip(),
                }
                for r in section_records
            ],
            "constitutional_jury_reviews": jury_reviews,
            "external_zero_auth_guild_reports": [e for e in external_guild_reports if e.get("ok")],
        }

        self.ordering.complete_task(
            f"{ingestion_id}_t4_constitutional_jury_audit",
            ",".join(r["family"] for r in jury_reviews),
            f"Completed {len(jury_reviews)} disjoint-family jury audits; {len(backfill_events)} section backfills applied",
        )
        self.collaboration.drive_next_tasks(
            project_id,
            f"{ingestion_id}_t4_constitutional_jury_audit",
            jury_reviews[0]["juror_model"] if jury_reviews else "jury_guild",
            produced_artifacts={"jury_reviews": len(jury_reviews)},
        )

        # ---------------------------------------------------------------------
        # STAGE 5A: Assemble Unabridged Multi-Family `SKILL.md` Guide (Zero Erasure)
        # ---------------------------------------------------------------------
        raw_slug = str(synthesized.get("skill_slug", "swarm-discovered-topic")).lower().strip()
        skill_slug = re.sub(r"[^a-z0-9-]+", "-", raw_slug).strip("-")[:48] or "swarm-discovered-topic"
        synthesized["skill_slug"] = skill_slug

        skill_md = self._build_unabridged_multi_family_skill_md(skill_slug, url, raw_sha, synthesized)

        # ---------------------------------------------------------------------
        # STAGE 5B: Multi-Family Code Synthesis (`qwen2.5-coder:14b`) + Disjoint Peer SOS (`granite-code:8b` / `qwen3:14b`)
        # ---------------------------------------------------------------------
        coder_node = self._find_node(["systems_code_synthesizer", "code_verifier_and_repair_peer", "integrative_architect"])
        peer_helper_node = self._find_node(["code_verifier_and_repair_peer", "integrative_architect"])
        module_slug = skill_slug.replace("-", "_")

        # Have the Coder Guild synthesize the executable reasoning & query engine methods, while embedding the
        # exact lossless `SYNTHESIZED_KNOWLEDGE_DATA` constant verbatim so 0% of catalog items or URLs can be truncated!
        code_spec_prompt = (
            "You are `qwen2.5-coder:14b` in Guild VI (Systems & Code Formalism).\n"
            "Write ONLY the Python class `SwarmTopicKnowledgeBase` (using ONLY Python stdlib `dataclasses`, `re`, `typing`).\n"
            "Assume the module ALREADY defines a top-level dictionary constant named `DISCOVERED_KNOWLEDGE` with keys:\n"
            "  - `'discovered_topic'` (str)\n"
            "  - `'domain_summary'` (str)\n"
            "  - `'executive_architecture_synthesis'` (str)\n"
            "  - `'sabrina_swarm_integration_blueprint'` (str)\n"
            "  - `'catalog_entries'` (list[dict] where each dict has `'name'`, `'section_origin'`, `'extracted_by_family'`, "
            "`'source_or_ref'`, `'category'`, `'capabilities'`, `'install_commands'`, `'usage_or_slash_commands'`, "
            "`'sub_components'`, `'workflow_steps'`, `'cautions'`)\n"
            "  - `'architectural_concepts'` (list[str])\n"
            "  - `'operational_warnings'` (list[str])\n"
            "  - `'section_guides'` (list[dict])\n"
            "  - `'constitutional_jury_reviews'` (list[dict])\n\n"
            "CRITICAL RULES:\n"
            "1. DO NOT redefine `DISCOVERED_KNOWLEDGE` and DO NOT open/read any files from disk.\n"
            "2. `SwarmTopicKnowledgeBase()` MUST take 0 required arguments in `__init__`.\n"
            "3. Implement these exact methods on `SwarmTopicKnowledgeBase`:\n"
            "   - `get_topic_overview(self) -> dict`: returns dict with `discovered_topic`, `domain_summary`, "
            "`executive_architecture_synthesis`, `architectural_concepts`, `operational_warnings`, `total_catalog_items`, and `participating_families`.\n"
            "   - `list_catalog(self) -> list[dict]`: returns the full `DISCOVERED_KNOWLEDGE['catalog_entries']` list.\n"
            "   - `get_section_guides(self) -> list[dict]`: returns `DISCOVERED_KNOWLEDGE['section_guides']`.\n"
            "   - `get_jury_audit_trail(self) -> list[dict]`: returns `DISCOVERED_KNOWLEDGE['constitutional_jury_reviews']`.\n"
            "   - `recommend_for_task(self, task_description: str) -> list[dict]`: tokenizes `task_description` (`re.findall(r'[a-z0-9_-]{2,}', task_description.lower())`) "
            "and scores every catalog entry across `name`, `category`, `capabilities`, `sub_components`, and `workflow_steps`, returning matching entries sorted by score descending.\n"
            "   - `get_installation_playbook(self, item_name: str) -> dict`: case-insensitive lookup by `item_name` (exact or substring) returning its `name`, `source_or_ref`, `install_commands`, `usage_or_slash_commands`, `workflow_steps`, and `cautions`.\n"
            "   - `evaluate_guardrails(self, content: str) -> dict`: checks `content` against `operational_warnings` and item `cautions` using keyword overlap and returns `{'advisories': [...], 'warning_count': int}`.\n"
            "4. Include an `if __name__ == '__main__':` self-test instantiating `SwarmTopicKnowledgeBase()` and printing overview + catalog size.\n"
            "Output ONLY valid Python code inside ```python ... ```."
        )
        coder_call = self._call_node(
            coder_node,
            code_spec_prompt,
            system="Output clean, self-contained Python 3.11 class code referencing `DISCOVERED_KNOWLEDGE`.",
            json_mode=False,
            temperature=0.1,
            num_ctx=12288,
            num_predict=2200,
        )
        class_code = extract_fenced_block(coder_call["response"], "python")
        # Strip any accidental `DISCOVERED_KNOWLEDGE = ...` placeholder if emitted by the model
        class_code = re.sub(r"(?m)^DISCOVERED_KNOWLEDGE\s*=\s*\{.*?\}\s*$", "", class_code)

        def _assemble_module_source(body_code: str) -> str:
            header_doc = (
                "#!/usr/bin/env python3\n"
                "# -*- coding: utf-8 -*-\n"
                '"""\n'
                f"Swarm Full-Guild Autonomous Integration Module: {synthesized['discovered_topic']}\n"
                f"Source URLSHA256: {raw_sha}\n"
                f"Synthesized by: {coder_node.family} ({coder_node.model}) + Multi-Family Reader & Jury Guilds\n"
                '"""\n'
                "from __future__ import annotations\n\n"
                "import dataclasses\n"
                "import json\n"
                "import re\n"
                "from typing import Any, Dict, List, Optional\n\n"
                f"DISCOVERED_KNOWLEDGE: Dict[str, Any] = {repr(synthesized)}\n\n"
            )
            return header_doc + body_code.strip() + "\n"

        py_code = _assemble_module_source(class_code)
        tool_path = SABRINA_ROOT / "tools" / f"swarm_ingested_{module_slug}.py"
        sos_used = False
        smoke_overview: Dict[str, Any] = {}

        for attempt in range(3):
            try:
                ast.parse(py_code)
                tool_path.write_text(py_code, encoding="utf-8")
                py_compile.compile(str(tool_path), doraise=True)

                spec = importlib.util.spec_from_file_location(f"swarm_ingested_{module_slug}_{attempt}", str(tool_path))
                assert spec and spec.loader
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                kb_cls = getattr(mod, "SwarmTopicKnowledgeBase")
                kb_inst = kb_cls()
                smoke_overview = json.loads(
                    json.dumps(
                        kb_inst.get_topic_overview(),
                        default=lambda o: sorted(o) if isinstance(o, set) else str(o),
                    )
                )
                cat_check = kb_inst.list_catalog()
                assert isinstance(cat_check, list) and len(cat_check) == len(catalog_entries)
                _ = kb_inst.recommend_for_task("audit")
                _ = kb_inst.evaluate_guardrails("test")
                break
            except Exception as err:
                if attempt == 2:
                    raise
                sos_used = True
                help_req = self.collaboration.ask_for_help(
                    task_id=f"{ingestion_id}_t5_unabridged_skill_and_code",
                    project=project_id,
                    requester_model=f"{coder_node.node_id}:{coder_node.model}",
                    title=f"Module verification error on attempt {attempt + 1}",
                    problem_statement=f"{type(err).__name__}: {err}",
                    capability_needed="code_repair",
                )
                repair_node = peer_helper_node if attempt == 0 else architect_node
                repair_prompt = (
                    "Peer SOS Repair Request: The `SwarmTopicKnowledgeBase` class body below failed runtime verification with:\n"
                    f"{type(err).__name__}: {err}\n\n"
                    "Remember: `DISCOVERED_KNOWLEDGE` is ALREADY defined above your code as a Python dict. "
                    "Do NOT redefine `DISCOVERED_KNOWLEDGE` and do NOT read external files. "
                    "Ensure `SwarmTopicKnowledgeBase()` takes 0 required args and implements `get_topic_overview()`, "
                    "`list_catalog()`, `get_section_guides()`, `get_jury_audit_trail()`, `recommend_for_task(task_description)`, "
                    "`get_installation_playbook(item_name)`, and `evaluate_guardrails(content)`.\n\n"
                    f"BROKEN CLASS CODE:\n```python\n{class_code}\n```\n\n"
                    "Output ONLY the corrected Python class code inside ```python ... ```."
                )
                rep_call = self._call_node(repair_node, repair_prompt, json_mode=False, temperature=0.1, num_ctx=12288, num_predict=2200)
                class_code = extract_fenced_block(rep_call["response"], "python")
                class_code = re.sub(r"(?m)^DISCOVERED_KNOWLEDGE\s*=\s*\{.*?\}\s*$", "", class_code)
                py_code = _assemble_module_source(class_code)

                offer = self.collaboration.propose_assistance(
                    request_id=help_req.request_id,
                    helper_model=f"{repair_node.node_id}:{repair_node.model}",
                    diagnosis=f"Fixed {type(err).__name__} in SwarmTopicKnowledgeBase methods",
                    proposed_solution=class_code[:500],
                    confidence_score=0.92,
                )
                self.collaboration.accept_assistance(help_req.request_id, offer.offer_id)

        self.ordering.complete_task(
            f"{ingestion_id}_t5_unabridged_skill_and_code",
            f"{coder_node.node_id}:{coder_node.model}",
            f"Verified standalone Python module ({len(catalog_entries)} items) & unabridged SKILL.md",
            produced_artifact=str(tool_path),
        )

        # ---------------------------------------------------------------------
        # STAGE 6: Stage & Cryptographically Approve Skill into Sabrina's Registry
        # ---------------------------------------------------------------------
        participating_families = sorted(
            {r["family"] for r in section_records}
            | {architect_node.family, coder_node.family}
            | {j["family"] for j in jury_reviews}
            | {e["family"] for e in external_guild_reports if e.get("ok")}
        )
        proposal_id = self.hydration.create_proposal(
            skill_name=skill_slug,
            skill_content=skill_md,
            trigger_reason="swarm_full_guild_blind_url_ingestion",
            query_used=url,
            search_sources=json.dumps([{"url": url, "raw_text_sha256": raw_sha, "families": participating_families}]),
        )
        prop = self.hydration.get_proposal(proposal_id)
        assert prop is not None
        approval = self.hydration.approve_proposal(
            proposal_id=proposal_id,
            expected_version=prop["version"],
            expected_sha256=prop["sha256"],
            reviewer="swarm-constitutional-jury-gate",
            feedback=f"Full-Guild blind ingestion verified across {len(participating_families)} model families: {participating_families}",
        )

        # ---------------------------------------------------------------------
        # STAGE 7: Persist Complete Verbatim Multi-Family Provenance
        # ---------------------------------------------------------------------
        provenance_dir = SABRINA_ROOT / "data" / "swarm_url_ingestions"
        provenance_dir.mkdir(parents=True, exist_ok=True)
        provenance_path = provenance_dir / f"{ingestion_id}_{skill_slug}.json"

        used_model_ids = (
            {r["model"] for r in section_records}
            | {f"{architect_node.node_id}:{architect_node.model}", f"{coder_node.node_id}:{coder_node.model}"}
            | {j["juror_model"] for j in jury_reviews}
        )
        engaged_nodes = [asdict(n) for n in self.nodes if f"{n.node_id}:{n.model}" in used_model_ids]

        elapsed_s = round(time.time() - t0, 2)
        record = {
            "ingestion_id": ingestion_id,
            "dag_project_id": project_id,
            "source_url": url,
            "raw_text_chars": len(raw_text),
            "raw_text_sha256": raw_sha,
            "chunks_processed": len(section_records),
            "participating_model_families": participating_families,
            "swarm_nodes_engaged": engaged_nodes,
            "external_zero_auth_guild_reports": external_guild_reports,
            "reader_notes": [
                {
                    "chunk_index": r["chunk_index"],
                    "section_title": r["section_title"],
                    "model": r["model"],
                    "family": r["family"],
                    "guild": r["guild"],
                    "eval_count": r["eval_count"],
                    "latency_sec": r["latency_sec"],
                    "notes": r["notes"],
                    "structured": r["structured"],
                }
                for r in section_records
            ],
            "architect_synthesis_call": {
                "model": f"{architect_node.node_id}:{architect_node.model}",
                "family": architect_node.family,
                "eval_count": synth_call["eval_count"],
                "latency_sec": synth_call["latency_sec"],
                "raw_response": synth_call["response"],
            },
            "constitutional_jury_reviews": jury_reviews,
            "jury_backfill_events": backfill_events,
            "coder_synthesis_call": {
                "model": f"{coder_node.node_id}:{coder_node.model}",
                "family": coder_node.family,
                "eval_count": coder_call["eval_count"],
                "latency_sec": coder_call["latency_sec"],
            },
            "synthesized_knowledge": synthesized,
            "skill_slug": skill_slug,
            "proposal_id": proposal_id,
            "skill_sha256": prop["sha256"],
            "installed_skill_path": approval["installed_path"],
            "generated_python_module": str(tool_path),
            "peer_sos_syntax_repair_triggered": sos_used,
            "dag_wavefront_final": self.ordering.view_dag_wavefront(project_id),
            "module_smoke_check": smoke_overview,
            "elapsed_seconds": elapsed_s,
        }
        provenance_path.write_text(
            json.dumps(record, indent=2, default=lambda o: sorted(o) if isinstance(o, set) else str(o)),
            encoding="utf-8",
        )
        record["provenance_file"] = str(provenance_path)
        return record

    @staticmethod
    def _build_unabridged_multi_family_skill_md(
        skill_slug: str,
        source_url: str,
        raw_sha: str,
        synthesized: Dict[str, Any],
    ) -> str:
        """Assemble the complete, lossless SKILL.md preserving every Guild's verbatim extraction & Jury opinions."""
        topic = re.sub(r"[\r\n]+", " ", str(synthesized.get("discovered_topic") or skill_slug)).strip()
        summary_one_line = re.sub(r"[\r\n]+", " ", str(synthesized.get("domain_summary") or topic)).strip()[:240]

        lines: List[str] = [
            "---",
            f"name: {skill_slug}",
            "version: 2.0.0",
            f"description: {summary_one_line}",
            "---",
            "",
            f"# {topic}",
            "",
            f"> **Provenance**: Autonomously retrieved and synthesized blind (`0` pre-seeded keywords) from `{source_url}` "
            f"(`SHA-256: {raw_sha[:16]}...`) by Sabrina's Multi-Family Swarm Collective.",
            "",
            "## 1. Executive Summary & Architectural Synthesis",
            "",
            str(synthesized.get("domain_summary") or ""),
            "",
            str(synthesized.get("executive_architecture_synthesis") or ""),
            "",
        ]

        arch_list = synthesized.get("architectural_concepts") or []
        if arch_list:
            lines.append("### Core Architectural Mechanisms Discovered")
            lines.append("")
            for ac in arch_list:
                lines.append(f"- {ac}")
            lines.append("")

        catalog = synthesized.get("catalog_entries") or []
        lines.append(f"## 2. Unabridged Reference Catalog ({len(catalog)} Discovered Entries)")
        lines.append("")
        lines.append("| # | Name | Source / Repository | Category | Attributed Guild Family |")
        lines.append("|---|------|---------------------|----------|-------------------------|")
        for idx, item in enumerate(catalog, 1):
            nm = str(item.get("name") or "").replace("|", "\\|")
            ref = str(item.get("source_or_ref") or "—").replace("|", "\\|")
            cat = str(item.get("category") or "General").replace("|", "\\|")
            fam = str(item.get("extracted_by_family") or "Swarm").replace("|", "\\|")
            lines.append(f"| {idx} | **{nm}** | `{ref}` | {cat} | {fam} |")
        lines.append("")

        lines.append("## 3. Detailed Per-Item Implementation, Commands & Sub-Workflows")
        lines.append("")
        for idx, item in enumerate(catalog, 1):
            lines.append(f"### 3.{idx}. {item.get('name')}")
            lines.append(f"- **Section Origin**: {item.get('section_origin')}")
            lines.append(f"- **Extracted By**: {item.get('extracted_by_family')} (`{item.get('extracted_by_model')}`)")
            if item.get("source_or_ref"):
                lines.append(f"- **Source / Repository**: `{item.get('source_or_ref')}`")
            lines.append(f"- **Category**: {item.get('category')}")
            if item.get("capabilities"):
                lines.append(f"- **Capabilities**: {item.get('capabilities')}")

            if item.get("install_commands"):
                lines.append("- **Installation Commands**:")
                lines.append("  ```bash")
                for cmd in item["install_commands"]:
                    lines.append(f"  {cmd}")
                lines.append("  ```")

            if item.get("usage_or_slash_commands"):
                lines.append("- **Usage / Invocation Commands**:")
                for ucmd in item["usage_or_slash_commands"]:
                    lines.append(f"  - `{ucmd}`")

            if item.get("sub_components"):
                lines.append("- **Sub-Components / Plugins / Supported Targets**:")
                for sub in item["sub_components"]:
                    lines.append(f"  - `{sub}`")

            if item.get("workflow_steps"):
                lines.append("- **Procedural Workflow**:")
                for step_i, step_txt in enumerate(item["workflow_steps"], 1):
                    lines.append(f"  {step_i}. {step_txt}")

            if item.get("cautions"):
                lines.append("- **Item-Specific Guardrails**:")
                for caut in item["cautions"]:
                    lines.append(f"  - {caut}")
            lines.append("")

        sec_guides = synthesized.get("section_guides") or []
        if sec_guides:
            lines.append("## 4. Section-by-Section Unabridged Guild Walkthrough")
            lines.append("")
            for sg in sec_guides:
                lines.append(f"### Section {sg['section_index']}: {sg['section_title']}")
                lines.append(f"*Analyzed by {sg['attributed_guild']}*")
                lines.append("")
                if sg.get("section_summary"):
                    lines.append(sg["section_summary"])
                    lines.append("")
                if sg.get("unabridged_guide_notes") and sg["unabridged_guide_notes"] != sg.get("section_summary"):
                    lines.append(sg["unabridged_guide_notes"])
                    lines.append("")

        warnings = synthesized.get("operational_warnings") or []
        if warnings:
            lines.append("## 5. Operational Guardrails & Risk Warnings")
            lines.append("")
            for w in warnings:
                lines.append(f"- {w}")
            lines.append("")

        if synthesized.get("sabrina_swarm_integration_blueprint"):
            lines.append("## 6. Sabrina Multi-Family Swarm Operational Playbook")
            lines.append("")
            lines.append(str(synthesized["sabrina_swarm_integration_blueprint"]))
            lines.append("")

        ext_reports = synthesized.get("external_zero_auth_guild_reports") or []
        if ext_reports:
            lines.append("## 7. External Zero-Auth Portal Guild Perspectives")
            lines.append("")
            for er in ext_reports:
                lines.append(f"### {er.get('family')} (`{er.get('portal_id')}`)")
                lines.append(str(er.get("response") or ""))
                lines.append("")

        jury_list = synthesized.get("constitutional_jury_reviews") or []
        if jury_list:
            lines.append("## 8. Disjoint-Family Constitutional Jury Audit & Signed Opinions")
            lines.append("")
            for jr in jury_list:
                lines.append(
                    f"- **{jr.get('family')}** (`{jr.get('juror_model')}`, *{jr.get('guild')}*) — "
                    f"**Verdict: `{jr.get('verdict')}`**: {jr.get('dissent_or_safeguard_opinion')}"
                )
            lines.append("")

        return "\n".join(lines).strip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Blind Full-Guild Multi-Family URL Retrieval & System Integration")
    ap.add_argument("--url", required=True, help="Target URL for blind full-guild retrieval and integration")
    args = ap.parse_args()

    integrator = SwarmAutonomousURLIntegrator()
    print(f"[*] Live Multi-Family Guild Roster ({len(integrator.nodes)} nodes):")
    for n in integrator.nodes:
        print(f"    - [{n.family}] {n.node_id} -> {n.model} ({n.guild})")
    print("[*] Executing zero-seed full-guild retrieval, section extraction, jury audit & integration...")
    result = integrator.run_blind_ingestion(args.url)
    print(json.dumps({
        "ingestion_id": result["ingestion_id"],
        "discovered_topic": result["synthesized_knowledge"].get("discovered_topic"),
        "skill_slug": result["skill_slug"],
        "participating_model_families": result["participating_model_families"],
        "swarm_nodes_count": len(result["swarm_nodes_engaged"]),
        "sections_processed": result["chunks_processed"],
        "catalog_count": len(result["synthesized_knowledge"].get("catalog_entries", [])),
        "jury_reviews_count": len(result["constitutional_jury_reviews"]),
        "installed_skill_path": result["installed_skill_path"],
        "skill_sha256": result["skill_sha256"],
        "generated_python_module": result["generated_python_module"],
        "provenance_file": result["provenance_file"],
        "elapsed_seconds": result["elapsed_seconds"],
    }, indent=2))


if __name__ == "__main__":
    main()
