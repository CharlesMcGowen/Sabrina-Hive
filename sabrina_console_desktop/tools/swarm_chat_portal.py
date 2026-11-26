#!/usr/bin/env python3
"""
Sabrina Multi-Family Swarm Collective Chat Portal & Tkinter View
================================================================
Provides:
   - Default `external_council`: the original KindCollectivePool selects and
     broadcasts to external HF contributors, then performs an external review.
     This module renders receipts and does not implement a second guild coordinator.
1. Unified Swarm Chat Engine (`send_swarm_chat`, `get_swarm_roster`, `get_chat_history`, `clear_chat_history`):
   - `hive_council` mode: Queries Meta Llama (`llama3.1:8b`), IBM Granite (`granite3.1-dense:8b`),
     Mistral (`dolphin-mistral:7b`), and any `qualified` WSP External `hf-gradio` Guild portal in parallel,
     followed by `qwen3:14b` integrative synthesis — preserving 100% verbatim per-node replies, `<think>`
     traces, token counts, and latencies (Zero Ghostwriting).
   - `direct_node` mode: Chat 1-on-1 with any local `ego` model family OR any External Zero-Auth WSP /
     Agent Arena `hf-gradio` portal via sanctioned `tools.portal_transport.invoke_portal` + `CyberComplianceSentinel`.
2. Standalone FastAPI Web Portal (`http://127.0.0.1:8095/`) with CORS enabled so `:8087` (`sabrina_social`)
   and `sabrina_control_center.html` can talk directly to Windows's Single-Authority portal database.
3. `SwarmChatTab`: Native Tkinter tab for `sabrina_operator_console.py`.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools.console_remote import call_console_backend, run_console_chat

try:
    import tkinter as tk
    from tkinter import ttk
    _TkFrameBase = ttk.Frame
except ImportError:
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    _TkFrameBase = object  # type: ignore[assignment,misc]

DEFAULT_OLLAMA_CHAT = os.environ.get("SABRINA_OLLAMA_CHAT_URL", "http://ego.local:11434/api/chat")
FALLBACK_LOCAL_OLLAMA = "http://127.0.0.1:11434/api/chat"
HISTORY_PATH = ROOT_DIR / "data" / "swarm_chat_history.json"
PORTAL_DB_PATH = Path(os.environ.get("SABRINA_ASSISTANT_DB", str(ROOT_DIR / "data" / "sabrina_assistant_fresh.sqlite3")))
_HISTORY_LOCK = threading.Lock()

LOCAL_FLEET: List[Dict[str, Any]] = [
    {"id": "local:qwen3:14b", "model": "qwen3:14b", "family": "Alibaba Qwen3 (14B)", "label": "qwen3:14b — Deep <think> Strategist (14B)", "num_predict": 1800},
    {"id": "local:qwen2.5-coder:14b", "model": "qwen2.5-coder:14b", "family": "Alibaba Qwen2.5-Coder (14B)", "label": "qwen2.5-coder:14b — Systems & Formalism (14B)", "num_predict": 1600},
    {"id": "local:llama3.1:8b", "model": "llama3.1:8b", "family": "Meta Llama-3.1 (8B)", "label": "llama3.1:8b — Guild I Anti-Monoculture Lead (8B)", "num_predict": 900},
    {"id": "local:granite3.1-dense:8b", "model": "granite3.1-dense:8b", "family": "IBM Granite-3.1 (8B)", "label": "granite3.1-dense:8b — Guild II Governance & Blackboard (8B)", "num_predict": 900},
    {"id": "local:granite-code:8b", "model": "granite-code:8b", "family": "IBM Granite-Code (8B)", "label": "granite-code:8b — Cross-Family Code Auditor (8B)", "num_predict": 900},
    {"id": "local:dolphin-mistral:7b", "model": "dolphin-mistral:7b", "family": "Mistral (7B)", "label": "dolphin-mistral:7b — Guild III Divergent Hypotheses (7B)", "num_predict": 900},
    {"id": "local:promptbreeder:latest", "model": "promptbreeder:latest", "family": "PromptBreeder", "label": "promptbreeder:latest — Evolutionary Meta-Prompter", "num_predict": 800},
    {"id": "local:sabrina-jury-granite:0.2", "model": "sabrina-jury-granite:0.2", "family": "IBM Granite Jury", "label": "sabrina-jury-granite:0.2 — Attribution & Policy Jury", "num_predict": 800},
    {"id": "local:sabrina-jury-defender:0.2", "model": "sabrina-jury-defender:0.2", "family": "Constitutional Jury", "label": "sabrina-jury-defender:0.2 — Dissent & Safeguard Jury", "num_predict": 800},
    {"id": "local:sabrina-qwen2.5-coder:1.5b-clean", "model": "sabrina-qwen2.5-coder:1.5b-clean", "family": "Qwen2.5-Coder (1.5B)", "label": "sabrina-qwen2.5-coder:1.5b-clean — Fast Boundary Prober (1.5B)", "num_predict": 700},
]

EXTERNAL_WSP_AND_ARENA_PORTALS: List[Dict[str, str]] = [
    {"id": "portal:hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "portal_id": "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "role": "structured_extraction", "family": "External WSP Guild 1–3 (HF Gradio)", "label": "WSP Star #1: jayeshmurthi49/Agentic-RAG-Chatbot"},
    {"id": "portal:hf-gradio-dkt3-digital-twin", "portal_id": "hf-gradio-dkt3-digital-twin", "role": "structured_extraction", "family": "External WSP Guild 4 (HF Gradio)", "label": "WSP Star #2: dkT3/digital-twin"},
    {"id": "portal:hf-gradio-parmeshlata-portfolio-chatbot", "portal_id": "hf-gradio-parmeshlata-portfolio-chatbot", "role": "structured_extraction", "family": "External Arena Guild 4 (HF Gradio)", "label": "Arena Guild 4: parmeshlata/Portfolio-Chatbot"},
    {"id": "portal:hf-gradio-himakesh-parth-ai-tutor", "portal_id": "hf-gradio-himakesh-parth-ai-tutor", "role": "grounded_summarization", "family": "External Arena Guild 1 (HF Gradio)", "label": "Arena Guild 1: Himakesh/parth-ai-tutor"},
    {"id": "portal:hf-gradio-htchu-coderhelper", "portal_id": "hf-gradio-htchu-coderhelper", "role": "critique", "family": "External Arena Guild 2 (HF Gradio)", "label": "Arena Guild 2: htchu/coderhelper"},
    {"id": "portal:hf-gradio-affanaiengr-rag", "portal_id": "hf-gradio-affanaiengr-rag", "role": "critique", "family": "External Arena Guild 2 (HF Gradio)", "label": "Arena Guild 2: AffanAIEngr/RAG"},
    {"id": "portal:hf-gradio-free234-snap-rag-assistant", "portal_id": "hf-gradio-free234-snap-rag-assistant", "role": "grounded_summarization", "family": "External Arena Guild 2 (HF Gradio)", "label": "Arena Guild 2: free234/SNAP-RAG-Assistant"},
    {"id": "portal:hf-gradio-rstanley2026-rodney-digital-twin", "portal_id": "hf-gradio-rstanley2026-rodney-digital-twin", "role": "grounded_summarization", "family": "External Arena Guild 4 (HF Gradio)", "label": "Arena Guild 4: rstanley2026/Rodney-Digital-Twin"},
    {"id": "portal:hf-gradio-nakaassoumatine-einstein-chatbot", "portal_id": "hf-gradio-nakaassoumatine-einstein-chatbot", "role": "grounded_summarization", "family": "External Arena Guild 4 (HF Gradio)", "label": "Arena Guild 4: nakaassoumatine/einstein-chatbot"},
]


def _split_thinking(raw_content: str, explicit_thinking: str = "") -> Tuple[str, str]:
    thinking = explicit_thinking.strip()
    text = raw_content or ""
    match = re.search(r"<think>(.*?)</think>", text, re.S | re.I)
    if match:
        extracted = match.group(1).strip()
        thinking = (thinking + "\n" + extracted).strip() if thinking else extracted
        text = (text[:match.start()] + text[match.end():]).strip()
    elif "<think>" in text.lower():
        idx = text.lower().find("<think>")
        extracted = text[idx + 7:].strip()
        thinking = (thinking + "\n" + extracted).strip() if thinking else extracted
        text = text[:idx].strip()
    return text.strip(), thinking


_AUTOMATED_JSON_PROBE_MARKERS: Tuple[str, ...] = (
    "Return compact JSON",
    "Return compact JSON:",
    "Respond only as compact JSON",
    "Return JSON only",
    "Return only JSON",
    "Public research questionnaire",
    "This is an invitation to a short research questionnaire",
    "Reply with exactly HF_CHAT_READY_",
    "Operator request to the HF guild:",
    "Operator-authorized live portal access experiment",
    "You are an HF guild worker directing one live external chat",
    "Final evidence review for this live HF guild task",
    "For this bounded research experiment, nominate up to",
)


def _is_automated_json_probe_turn(turn: Dict[str, Any]) -> bool:
    user_msg = str(turn.get("user_message") or "")
    if any(marker in user_msg for marker in _AUTOMATED_JSON_PROBE_MARKERS):
        return True
    reply = str(turn.get("primary_reply") or "").strip()
    if reply.startswith("{") and reply.endswith("}") and "Return" in user_msg and "JSON" in user_msg:
        return True
    return False


def format_structured_as_markdown(data: Any, title: str = "", _depth: int = 0) -> str:
    """Format a Python dict/list/scalar into clean, human-readable Markdown."""
    lines: List[str] = []
    if title and _depth == 0:
        lines.append(f"### {title}")
    indent = "  " * _depth
    if isinstance(data, dict):
        if not data:
            lines.append(f"{indent}- *(empty)*")
        for k, v in data.items():
            label = str(k).replace("_", " ")
            if isinstance(v, dict):
                lines.append(f"{indent}- **{label}**:")
                lines.append(format_structured_as_markdown(v, _depth=_depth + 1))
            elif isinstance(v, list):
                if not v:
                    lines.append(f"{indent}- **{label}**: *(none)*")
                elif all(not isinstance(x, (dict, list)) for x in v):
                    joined = ", ".join(f"`{x}`" for x in v)
                    lines.append(f"{indent}- **{label}**: {joined}")
                else:
                    lines.append(f"{indent}- **{label}**:")
                    lines.append(format_structured_as_markdown(v, _depth=_depth + 1))
            elif isinstance(v, bool):
                lines.append(f"{indent}- **{label}**: `{'true' if v else 'false'}`")
            elif v is None:
                lines.append(f"{indent}- **{label}**: `null`")
            else:
                lines.append(f"{indent}- **{label}**: {v}")
    elif isinstance(data, list):
        if not data:
            lines.append(f"{indent}- *(none)*")
        for idx, item in enumerate(data, 1):
            if isinstance(item, (dict, list)):
                lines.append(f"{indent}{idx}.")
                lines.append(format_structured_as_markdown(item, _depth=_depth + 1))
            else:
                lines.append(f"{indent}- {item}")
    else:
        lines.append(f"{indent}{data}")
    return "\n".join(lines)


def _try_parse_json_object(candidate: str) -> Optional[Any]:
    s = candidate.strip()
    if s.startswith("```"):
        m = re.match(r"^```(?:json)?\s*(\{.*\}|\[.*\])\s*```$", s, re.S | re.I)
        if m:
            s = m.group(1).strip()
    if not ((s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]"))):
        return None
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, (dict, list)) else None
    except Exception:
        return None


def format_text_or_json_as_markdown(text: str) -> str:
    """Convert standalone or trailing JSON blobs in prompts/replies into clean Markdown while preserving prose."""
    raw = (text or "").strip()
    if not raw:
        return ""
    parsed_whole = _try_parse_json_object(raw)
    if parsed_whole is not None:
        return format_structured_as_markdown(parsed_whole)

    brace_idx = raw.find("{")
    if brace_idx > 0 and raw.endswith("}"):
        prefix = raw[:brace_idx].strip()
        json_tail = raw[brace_idx:].strip()
        parsed_tail = _try_parse_json_object(json_tail)
        if parsed_tail is not None:
            return f"{prefix}\n\n{format_structured_as_markdown(parsed_tail, title='Structured Context')}"
    return raw


def _portal_qualification_snapshot() -> Dict[str, Dict[str, Any]]:
    snap: Dict[str, Dict[str, Any]] = {}
    if not PORTAL_DB_PATH.exists():
        return snap
    try:
        conn = sqlite3.connect(PORTAL_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            settings = {r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM portal_settings").fetchall()}
            research_on = bool(settings.get("research_enabled", False))
            for item in EXTERNAL_WSP_AND_ARENA_PORTALS:
                pid = item["portal_id"]
                role = item["role"]
                prof = conn.execute("SELECT access_allowed, access_state FROM portal_profiles WHERE portal_id=?", (pid,)).fetchone()
                qual = conn.execute("SELECT responsibility, expires FROM portal_role_qualifications WHERE portal_id=? AND role=?", (pid, role)).fetchone()
                run = conn.execute(
                    "SELECT state, summary_json FROM portal_eval_runs WHERE portal_id=? AND role=? ORDER BY created DESC LIMIT 1",
                    (pid, role),
                ).fetchone()
                summary = json.loads(run["summary_json"]) if run and run["summary_json"] else {}
                is_qual = bool(qual and qual["responsibility"] == "qualified" and float(qual["expires"] or 0) > time.time())
                obs = summary.get("observed_cases", 0)
                passed = summary.get("passed", 0)
                snap[pid] = {
                    "research_enabled": research_on,
                    "access_state": prof["access_state"] if prof else "unregistered",
                    "qualified": is_qual,
                    "responsibility": qual["responsibility"] if qual else "unassessed",
                    "progress": f"{passed}/{obs} passed (of 20)" if obs else "queued (0/20)",
                }
        finally:
            conn.close()
    except Exception:
        pass
    return snap


_COLLECTIVE_POOLS = {}


def get_collective_pool():
    from harnesses.external.collective_pool import KindCollectivePool
    key = str(PORTAL_DB_PATH)
    if key not in _COLLECTIVE_POOLS:
        _COLLECTIVE_POOLS[key] = KindCollectivePool(db_path=PORTAL_DB_PATH,
            enable_semantic_cache=False, external_hf_only=True)
    return _COLLECTIVE_POOLS[key]


def get_swarm_roster() -> Dict[str, Any]:
    curated = {p['portal_id']: p for p in EXTERNAL_WSP_AND_ARENA_PORTALS}
    externals = [
        {
            **entry,
            'label': curated.get(entry['portal_id'], {}).get('label', entry['label']),
            'curated_guild': entry['portal_id'] in curated,
        }
        for entry in get_collective_pool().supervised_hf_members()
    ]
    externals.sort(
        key=lambda e: (
            0 if e.get("chat_eligible") else 1,
            0 if e.get("curated_guild") else 1,
            -int(e.get("recent_ok") or 0),
            str(e.get("label") or "").lower(),
        )
    )
    eligible_count = sum(1 for e in externals if e.get("chat_eligible"))
    blocked_count = len(externals) - eligible_count
    api_tiers_on = False
    try:
        from tools.portal_policy import PortalPolicy
        api_tiers_on = bool(PortalPolicy(PORTAL_DB_PATH).setting("api_tiers_enabled"))
    except Exception:
        pass
    thread_info = list_chat_threads()
    return {
        "modes": [
            {"id": "hive_council", "label": "Full Swarm Council (Local ego + HF Guilds + Qwen3 Chair)"},
            {"id": "external_council", "label": "Public HF Guilds Only (External Zero-Auth)"},
            {"id": "direct_node", "label": "1-on-1 Direct Model Picker (Local Family or HF Guild)"},
        ],
        "local_nodes": LOCAL_FLEET,
        "external_nodes": externals,
        "eligible_external_count": eligible_count,
        "blocked_external_count": blocked_count,
        "api_tiers_enabled": api_tiers_on,
        "open_web_portals_count": eligible_count or len(externals),
        "active_thread_id": thread_info["active_thread_id"],
        "threads": thread_info["threads"],
    }


def set_swarm_api_tiers(enabled: bool) -> Dict[str, Any]:
    """Toggle paid/API-key cloud tiers (OpenAI, Anthropic, Gemini, HF Router, Groq, NIM, GitHub Models)
    while keeping Open Zero-Auth Web Portals + Local ego Fleet 100% active."""
    from tools.portal_policy import PortalPolicy
    flag = bool(enabled)
    os.environ["SABRINA_API_TIERS_ENABLED"] = "1" if flag else "0"
    policy = PortalPolicy(PORTAL_DB_PATH)
    policy.set_api_tiers_enabled(flag)
    return {
        "ok": True,
        "api_tiers_enabled": flag,
        "roster": get_swarm_roster(),
    }


def call_local_ollama_node(
    model: str,
    family: str,
    system_prompt: str,
    user_message: str,
    history_turns: Optional[List[Dict[str, str]]] = None,
    num_predict: int = 1000,
    temperature: float = 0.35,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    system_prompt += (' You are in a discussion-only chat with no tools, repository access, or execution authority. '
                      'Do not claim you inspected files, ran tests, changed code, or contacted portals. '
                      'Describe proposed work as proposed, and distinguish supplied evidence from assumptions. '
                      'Format your response in clean, readable Markdown (headings, bullet lists, and fenced code blocks where helpful) unless the user explicitly asks for JSON.')
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    for h in (history_turns or [])[-6:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": model,
        "stream": False,
        "messages": messages,
        "options": {"num_predict": num_predict, "temperature": temperature},
    }
    try:
        eff_timeout = float(timeout if timeout is not None else os.environ.get("SABRINA_SWARM_CHAT_TIMEOUT_SEC", "60"))
    except (TypeError, ValueError):
        eff_timeout = 60.0
    eff_timeout = max(10.0, min(180.0, eff_timeout))
    wait_budget = min(120, int(eff_timeout) + 20)
    t0 = time.monotonic()
    last_err = ""

    def _is_url_reachable(u: str) -> bool:
        try:
            import socket
            from urllib.parse import urlparse
            p = urlparse(u)
            h = p.hostname or "127.0.0.1"
            port = p.port or 11434
            with socket.create_connection((h, port), timeout=0.25):
                return True
        except OSError:
            return False

    if not _is_url_reachable(DEFAULT_OLLAMA_CHAT):
        return {
            "ok": False,
            "node_id": f"local:{model}",
            "speaker": model,
            "family": family,
            "source": "local_ego_ollama",
            "reply": "",
            "display_markdown": "",
            "thinking": "",
            "tokens": 0,
            "latency_sec": 0.0,
            "error": f"Local Ollama node offline ({DEFAULT_OLLAMA_CHAT} unreachable).",
        }

    for url in (DEFAULT_OLLAMA_CHAT,):
        try:
            from tools.lab_http import LabHTTP
            from tools.control_client import caller, inference_wait_budget
            base, path = url.rsplit('/api/', 1)
            with caller('swarm-chat', 'swarm-chat'), inference_wait_budget(wait_budget):
                data = LabHTTP(base).request('/api/' + path, payload, timeout=eff_timeout)
            msg = data.get("message") or {}
            clean_text, thinking = _split_thinking(msg.get("content", ""), msg.get("thinking", ""))
            reply_text = clean_text or "(Model spent budget in <think>; expand thinking trace.)"
            return {
                "ok": bool(clean_text or thinking),
                "node_id": f"local:{model}",
                "speaker": model,
                "family": family,
                "source": "local_ego_ollama",
                "reply": reply_text,
                "display_markdown": format_text_or_json_as_markdown(reply_text),
                "thinking": thinking,
                "tokens": int(data.get("eval_count") or 0),
                "latency_sec": round(time.monotonic() - t0, 2),
                "error": "",
            }
        except Exception as exc:
            last_err = str(exc)
    return {
        "ok": False,
        "node_id": f"local:{model}",
        "speaker": model,
        "family": family,
        "source": "local_ego_ollama",
        "reply": "",
        "display_markdown": "",
        "thinking": "",
        "tokens": 0,
        "latency_sec": round(time.monotonic() - t0, 2),
        "error": last_err,
    }


def format_external_result(pid, result):
    raw_reply = result.get('reply', '')
    return {**result, 'ok':bool(result.get('ok')), 'node_id':'portal:'+pid,
            'speaker':pid, 'family':'External HF portal (underlying model unverified)',
            'source':'external_zero_auth_portal', 'reply':raw_reply,
            'display_markdown':format_text_or_json_as_markdown(raw_reply),
            'thinking':'', 'tokens':None, 'latency_sec':round(result.get('latency_ms',0)/1000,3),
            'error':result.get('error') or result.get('failure',''),
            'guild_engine':'KindCollectivePool'}


def call_external_wsp_portal(portal_meta: Dict[str, str], prompt: str) -> Dict[str, Any]:
    pid=portal_meta['portal_id']
    result=get_collective_pool().invoke_member(pid,prompt,task_id=f'chat-{time.time_ns()}',
                                              supervised=True,conversation=True)
    return format_external_result(pid,result)


_ACTIVE_THREAD_ID: str = "main"


def _read_all_history_entries() -> List[Dict[str, Any]]:
    if not HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_all_history_entries(entries: List[Dict[str, Any]]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(entries[-240:], ensure_ascii=False, indent=2), encoding="utf-8")


def get_chat_history(thread_id: Optional[str] = None, include_all_threads: bool = False) -> List[Dict[str, Any]]:
    all_entries = _read_all_history_entries()
    if include_all_threads:
        return all_entries
    target_thread = (thread_id or _ACTIVE_THREAD_ID or "main").strip() or "main"
    return [t for t in all_entries if (t.get("thread_id") or "main") == target_thread]


def list_chat_threads() -> Dict[str, Any]:
    all_entries = _read_all_history_entries()
    by_thread: Dict[str, Dict[str, Any]] = {
        "main": {
            "thread_id": "main",
            "title": "Main Thread",
            "turn_count": 0,
            "forked_from_turn_id": None,
            "updated_at": 0.0,
        }
    }
    for t in all_entries:
        tid = (t.get("thread_id") or "main").strip() or "main"
        info = by_thread.setdefault(
            tid,
            {
                "thread_id": tid,
                "title": t.get("thread_title") or (f"Fork ({tid[-6:]})" if tid != "main" else "Main Thread"),
                "turn_count": 0,
                "forked_from_turn_id": t.get("forked_from_turn_id"),
                "updated_at": 0.0,
            },
        )
        info["turn_count"] += 1
        if t.get("thread_title"):
            info["title"] = t["thread_title"]
        if t.get("forked_from_turn_id") and not info.get("forked_from_turn_id"):
            info["forked_from_turn_id"] = t["forked_from_turn_id"]
        ts = float(t.get("timestamp") or 0.0)
        if ts >= info["updated_at"]:
            info["updated_at"] = ts
    if _ACTIVE_THREAD_ID not in by_thread:
        by_thread[_ACTIVE_THREAD_ID] = {
            "thread_id": _ACTIVE_THREAD_ID,
            "title": "Main Thread" if _ACTIVE_THREAD_ID == "main" else f"Fork ({_ACTIVE_THREAD_ID[-6:]})",
            "turn_count": 0,
            "forked_from_turn_id": None,
            "updated_at": time.time(),
        }
    threads = sorted(
        by_thread.values(),
        key=lambda x: (0 if x["thread_id"] == "main" else 1, -float(x.get("updated_at") or 0.0)),
    )
    return {
        "active_thread_id": _ACTIVE_THREAD_ID,
        "threads": threads,
    }


def set_active_chat_thread(thread_id: str) -> Dict[str, Any]:
    global _ACTIVE_THREAD_ID
    clean_tid = (thread_id or "main").strip() or "main"
    with _HISTORY_LOCK:
        _ACTIVE_THREAD_ID = clean_tid
    info = list_chat_threads()
    return {
        "ok": True,
        "active_thread_id": _ACTIVE_THREAD_ID,
        "threads": info["threads"],
        "history": get_chat_history(_ACTIVE_THREAD_ID),
    }


def fork_chat_history(turn_id: str, title: Optional[str] = None) -> Dict[str, Any]:
    """Branch the conversation from `turn_id` (inclusive) into a new thread without mutating the parent thread."""
    global _ACTIVE_THREAD_ID
    clean_turn_id = (turn_id or "").strip()
    if not clean_turn_id:
        raise ValueError("turn_id is required to fork chat.")
    with _HISTORY_LOCK:
        all_entries = _read_all_history_entries()
        target_idx = next((i for i, t in enumerate(all_entries) if t.get("id") == clean_turn_id), None)
        if target_idx is None:
            raise ValueError(f"Turn '{clean_turn_id}' not found in chat history.")
        anchor = all_entries[target_idx]
        source_thread = (anchor.get("thread_id") or "main").strip() or "main"
        branch_turns = [
            t for t in all_entries[: target_idx + 1]
            if (t.get("thread_id") or "main") == source_thread
        ]
        new_tid = f"fork-{int(time.time() * 1000)}"
        snippet = (anchor.get("user_message") or "").strip().splitlines()[0][:28] if anchor.get("user_message") else clean_turn_id[-6:]
        branch_title = (title or "").strip() or f"⑂ Fork: {snippet}"
        cloned: List[Dict[str, Any]] = []
        for idx, item in enumerate(branch_turns):
            copy_item = json.loads(json.dumps(item))
            orig_id = copy_item.get("id") or f"turn-{idx}"
            copy_item["id"] = f"{orig_id}@{new_tid}"
            copy_item["thread_id"] = new_tid
            copy_item["thread_title"] = branch_title
            if idx == len(branch_turns) - 1:
                copy_item["forked_from_turn_id"] = clean_turn_id
                copy_item["forked_from_thread_id"] = source_thread
            cloned.append(copy_item)
        all_entries.extend(cloned)
        _write_all_history_entries(all_entries)
        _ACTIVE_THREAD_ID = new_tid
    info = list_chat_threads()
    return {
        "ok": True,
        "thread_id": new_tid,
        "title": branch_title,
        "forked_from_turn_id": clean_turn_id,
        "forked_from_thread_id": source_thread,
        "forked_turn": cloned[-1] if cloned else None,
        "active_thread_id": _ACTIVE_THREAD_ID,
        "threads": info["threads"],
        "history": get_chat_history(new_tid),
    }


def clear_chat_history(thread_id: Optional[str] = None, all_threads: bool = False) -> Dict[str, Any]:
    global _ACTIVE_THREAD_ID
    with _HISTORY_LOCK:
        if all_threads or (thread_id is None and _ACTIVE_THREAD_ID == "main"):
            _ACTIVE_THREAD_ID = "main"
            _write_all_history_entries([])
        else:
            target = (thread_id or _ACTIVE_THREAD_ID or "main").strip() or "main"
            remaining = [t for t in _read_all_history_entries() if (t.get("thread_id") or "main") != target]
            _write_all_history_entries(remaining)
            if target != "main":
                _ACTIVE_THREAD_ID = "main"
    return {"cleared": True, "active_thread_id": _ACTIVE_THREAD_ID, **list_chat_threads()}


def _append_history(entry: Dict[str, Any]) -> None:
    with _HISTORY_LOCK:
        all_entries = _read_all_history_entries()
        if not entry.get("thread_id"):
            entry["thread_id"] = _ACTIVE_THREAD_ID or "main"
        all_entries.append(entry)
        _write_all_history_entries(all_entries)


def send_swarm_chat(
    message: str,
    mode: str = "external_council",
    target: str = "local:qwen3:14b",
    progress=None,
    context: Optional[Dict[str, Any]] = None,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    clean_msg = (message or "").strip()
    if context and isinstance(context, dict):
        md_ctx = format_structured_as_markdown(context, title="Supplied Context")
        clean_msg = f"{clean_msg}\n\n{md_ctx}".strip() if clean_msg else md_ctx
    if not clean_msg:
        raise ValueError("Message cannot be empty.")
    if mode not in {'hive_council', 'external_council', 'direct_node'}:
        raise ValueError('Unknown chat mode.')
    progress = progress or (lambda _message: None)

    prior_turns: List[Dict[str, str]] = []
    conversational_history = [t for t in get_chat_history() if not _is_automated_json_probe_turn(t)]
    for item in conversational_history[-4:]:
        prior_turns.append({"role": "user", "content": format_text_or_json_as_markdown(item.get("user_message", ""))})
        if item.get("primary_reply"):
            prior_turns.append({"role": "assistant", "content": format_text_or_json_as_markdown(item["primary_reply"])[:1200]})

    started = time.monotonic()
    responses: List[Dict[str, Any]] = []
    synthesis: Optional[Dict[str, Any]] = None

    if mode == "direct_node":
        progress('Waiting for ' + target + ' through the shared scheduler')
        local_map = {n["id"]: n for n in LOCAL_FLEET}
        ext_map = {p["id"]: p for p in get_swarm_roster()['external_nodes']}
        if target in local_map:
            node = local_map[target]
            res = call_local_ollama_node(
                node["model"],
                node["family"],
                f"You are `{node['model']}` ({node['family']}) inside the Sabrina Multi-Family Swarm Collective. Speak in your own authentic voice with technical precision.",
                clean_msg,
                history_turns=prior_turns,
                num_predict=int(node.get("num_predict", 1000)),
            )
            responses.append(res)
        elif target in ext_map:
            responses.append(call_external_wsp_portal(ext_map[target], clean_msg))
        else:
            raise ValueError(f"Unknown target node: {target}")
        primary_reply = responses[0]["reply"] or f"[{responses[0]['speaker']}] {responses[0]['error']}"
    elif mode == 'external_council':
        guild=get_collective_pool().run_guild_round(clean_msg,task_id=f'guild-chat-{time.time_ns()}',progress=progress)
        responses=[format_external_result(r['portal_id'],r) for r in guild['responses']]
        synthesis=(format_external_result(guild['synthesis']['portal_id'],guild['synthesis'])
                   if guild.get('synthesis') else None)
        primary_reply=(synthesis['reply'] if synthesis and synthesis.get('ok') else
                       '\n\n'.join('['+r['speaker']+']\n'+(r['reply'] or r['error']) for r in responses))
        if not responses:
            primary_reply='No HF guild member is currently admitted. See the roster for recorded access reasons.'
    else:
        # Multi-Family Hive Council: 3 fast 7B-8B distinct local families in parallel + any qualified WSP hf-gradio portal!
        council_specs = [
            ("llama3.1:8b", "Meta Llama-3.1 (Guild I)", "You are Guild I (Meta Llama-3.1) of the Sabrina Swarm Collective: focus on architectural diversity, structural clarity, and anti-monoculture critique. Keep response concise (~180 words) in clean Markdown.", 0.35),
            ("granite3.1-dense:8b", "IBM Granite-3.1 (Guild II)", "You are Guild II (IBM Granite-3.1) of the Sabrina Swarm Collective: focus on governance, verification, stigmergic coordination, and concrete engineering rigor (~180 words) in clean Markdown.", 0.25),
            ("dolphin-mistral:7b", "Mistral-7B (Guild III)", "You are Guild III (Mistral-7B) of the Sabrina Swarm Collective: contribute creative, divergent hypotheses and non-obvious angles (~180 words) in clean Markdown.", 0.6),
        ]
        snap = _portal_qualification_snapshot()
        qualified_ext = [p for p in EXTERNAL_WSP_AND_ARENA_PORTALS if snap.get(p["portal_id"], {}).get("qualified")]
        if not qualified_ext:
            qualified_ext = []

        cancelled = False
        for idx, (m, fam, sys_p, temp) in enumerate(council_specs, start=1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            progress(f"Querying {m} ({idx}/{len(council_specs)})…")
            responses.append(call_local_ollama_node(m, fam, sys_p, clean_msg, prior_turns, 550, temp))

        if not cancelled and (cancel_event is None or not cancel_event.is_set()):
            if qualified_ext:
                progress(f"Querying external guild {qualified_ext[0]['portal_id']}…")
                responses.append(call_external_wsp_portal(qualified_ext[0], clean_msg))
            else:
                wsp_prog = snap.get("hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", {}).get("progress", "qualifying")
                responses.append({
                    "ok": False,
                    "node_id": "portal:hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api",
                    "speaker": "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api",
                    "family": "External WSP Guild 1–3 (HF Gradio)",
                    "source": "external_zero_auth_portal",
                    "reply": "",
                    "display_markdown": "",
                    "thinking": "",
                    "tokens": 0,
                    "latency_sec": 0.0,
                    "error": f"Awaiting 20-case PortalPolicy promotion ({wsp_prog}) — auto-joins Hive Council once qualified.",
                })
        else:
            cancelled = True

        if cancelled or (cancel_event is not None and cancel_event.is_set()):
            return {
                "ok": False,
                "cancelled": True,
                "error": "Chat cancelled by operator.",
                "responses": responses,
                "total_elapsed_sec": round(time.monotonic() - started, 2),
            }

        live_council = [r for r in responses if r.get("ok") and r.get("reply")]
        progress('Synthesizing ' + str(len(live_council)) + ' completed contributions via qwen3:14b…')
        transcript_block = "\n\n".join(f"[{r['speaker']} | {r['family']}]:\n{r['reply']}" for r in live_council)
        synth_prompt = (
            f"USER MESSAGE:\n{clean_msg}\n\n"
            f"VERBATIM MULTI-FAMILY GUILD DELIBERATIONS:\n{transcript_block}\n\n"
            "Synthesize a unified Swarm response in clean Markdown (with headings and bullet points) that explicitly credits each contributing model family (`Llama-3.1`, `IBM Granite-3.1`, `Mistral-7B`, and any `External WSP Guild`) and preserves any productive dissent."
        )
        synthesis = call_local_ollama_node(
            "qwen3:14b",
            "Alibaba Qwen3-14B (Integrative Chair)",
            "You are the Integrative Chair (`qwen3:14b`) of the Sabrina Multi-Family Swarm Collective. Never erase minority Guild insights; credit each family explicitly in clean Markdown.",
            synth_prompt,
            history_turns=None,
            num_predict=1500,
            temperature=0.3,
        ) if (live_council and (cancel_event is None or not cancel_event.is_set())) else None
        primary_reply = synthesis["reply"] if synthesis and synthesis.get("ok") else transcript_block

    record = {
        "id": f"turn-{int(time.time() * 1000)}",
        "thread_id": _ACTIVE_THREAD_ID or "main",
        "timestamp": time.time(),
        "mode": mode,
        "target": target if mode == "direct_node" else mode,
        "user_message": clean_msg,
        "user_message_markdown": format_text_or_json_as_markdown(clean_msg),
        "responses": responses,
        "synthesis": synthesis,
        "primary_reply": primary_reply,
        "primary_reply_markdown": format_text_or_json_as_markdown(primary_reply),
        "guild_engine": "KindCollectivePool" if mode == "external_council" or target.startswith("portal:") else None,
        "total_elapsed_sec": round(time.monotonic() - started, 2),
    }
    _append_history(record)
    return record


# ===========================================================================
# Native Tkinter Tab for `sabrina_operator_console.py`
# ===========================================================================
class SwarmChatTab(_TkFrameBase):
    """Native Windows Operator Console tab for chatting directly with the Multi-Family Swarm."""

    def __init__(self, parent: Any, console: Any = None) -> None:
        super().__init__(parent)
        self.console = console
        self._node_id_by_label: Dict[str, str] = {}
        self._busy = False
        self._build_ui()
        self._load_roster_and_history()

    def _build_ui(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(10, 6))

        ttk.Label(top, text="Mode:").pack(side="left", padx=(0, 4))
        self.mode_var = tk.StringVar(value="external_council")
        self.mode_box = ttk.Combobox(
            top,
            textvariable=self.mode_var,
            state="readonly",
            width=26,
            values=["external_council", "hive_council", "direct_node"],
        )
        self.mode_box.pack(side="left", padx=(0, 10))

        ttk.Label(top, text="Direct Node / WSP Portal:").pack(side="left", padx=(0, 4))
        self.target_var = tk.StringVar(value="")
        self.target_box = ttk.Combobox(top, textvariable=self.target_var, state="readonly", width=46)
        self.target_box.pack(side="left", padx=(0, 8))

        ttk.Button(top, text="Refresh Roster", command=self._load_roster_and_history).pack(side="left", padx=3)
        ttk.Button(top, text="⑂ Fork Last Turn", command=self._fork_last_turn).pack(side="left", padx=3)
        ttk.Button(top, text="Clear Chat", command=self._clear_chat).pack(side="left", padx=3)

        self.status_var = tk.StringVar(value="Ready · 0 cloud tokens · Local ego fleet + WSP hf-gradio Guilds")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=12, pady=(0, 4))

        self.transcript = tk.Text(
            self,
            bg="#0f172a",
            fg="#e5e7eb",
            wrap="word",
            font=("Segoe UI", 10),
            relief="flat",
            state="disabled",
            padx=12,
            pady=10,
        )
        self.transcript.pack(fill="both", expand=True, padx=10, pady=4)
        self.transcript.tag_configure("user", foreground="#93c5fd", font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("guild", foreground="#fcd34d", font=("Consolas", 9, "bold"))
        self.transcript.tag_configure("synth", foreground="#6ee7b7", font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("muted", foreground="#94a3b8", font=("Consolas", 9))
        self.transcript.tag_configure("md_h", foreground="#f8fafc", font=("Segoe UI", 11, "bold"))
        self.transcript.tag_configure("md_bold", foreground="#f1f5f9", font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("md_code", foreground="#67e8f9", background="#1e293b", font=("Consolas", 9))

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=(4, 10))
        self.input_box = tk.Entry(bottom, bg="#1f2937", fg="#f9fafb", insertbackground="#f9fafb", font=("Segoe UI", 10))
        self.input_box.pack(side="left", fill="x", expand=True, padx=(0, 8), ipady=5)
        self.input_box.bind("<Return>", lambda _e: self._on_send())
        self.send_btn = ttk.Button(bottom, text="Send to Swarm", command=self._on_send)
        self.send_btn.pack(side="right")

    def _load_roster_and_history(self) -> None:
        try:
            roster = call_console_backend("SABRINA_SWARM_API_BASE", "/api/swarm-chat/roster", get_swarm_roster)
            history = self._chat_history()
        except Exception as exc:
            self.status_var.set(f"Swarm connection unavailable: {exc}")
            return
        labels = []
        self._node_id_by_label.clear()
        for n in roster["local_nodes"]:
            lbl = f"[Local ego] {n['label']}"
            labels.append(lbl)
            self._node_id_by_label[lbl] = n["id"]
        for p in roster["external_nodes"]:
            lbl = f"[{p['status_badge']}] {p['label']}"
            labels.append(lbl)
            self._node_id_by_label[lbl] = p["id"]
        self.target_box["values"] = labels
        if labels and self.target_var.get() not in self._node_id_by_label:
            self.target_var.set(labels[0])
        self._render_history(history)
        if os.environ.get("SABRINA_SWARM_API_BASE", "").strip():
            self.status_var.set(
                f"Connected to Ego Lab · {roster.get('eligible_external_count', 0)} eligible external workers"
                f" · {roster.get('blocked_external_count', 0)} blocked"
            )

    def _chat_history(self) -> List[Dict[str, Any]]:
        result = call_console_backend(
            "SABRINA_SWARM_API_BASE", "/api/swarm-chat/history",
            lambda: {"history": get_chat_history()},
        )
        return result["history"]

    def _insert_markdown_block(self, raw_text: str, indent: str = "") -> None:
        md_text = format_text_or_json_as_markdown(raw_text)
        in_fence = False
        for line in md_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                self.transcript.insert("end", f"{indent}{line}\n", "md_code")
                continue
            if re.match(r"^#{1,4}\s+", stripped):
                heading_txt = re.sub(r"^#{1,4}\s+", "", stripped)
                self.transcript.insert("end", f"{indent}{heading_txt}\n", "md_h")
                continue
            self.transcript.insert("end", indent)
            pos = 0
            for m in re.finditer(r"(\*\*[^*]+\*\*|`[^`]+`)", line):
                if m.start() > pos:
                    self.transcript.insert("end", line[pos:m.start()])
                tok = m.group(0)
                if tok.startswith("**"):
                    self.transcript.insert("end", tok[2:-2], "md_bold")
                else:
                    self.transcript.insert("end", tok[1:-1], "md_code")
                pos = m.end()
            if pos < len(line):
                self.transcript.insert("end", line[pos:])
            self.transcript.insert("end", "\n")
        self.transcript.insert("end", "\n")

    def _render_history(self, turns: List[Dict[str, Any]]) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        if not turns:
            self.transcript.insert("end", "Convene the Multi-Family Hive Council or pick any individual Local / WSP hf-gradio model below.\n\n", "muted")
        for t in turns[-20:]:
            self.transcript.insert("end", f"You ({t.get('mode', 'hive_council')}):\n", "user")
            self._insert_markdown_block(t.get("user_message_markdown") or t.get("user_message", ""), indent="")
            for r in t.get("responses", []):
                hdr = f"  [{r.get('family')} · {r.get('speaker')} · {r.get('latency_sec')}s · {r.get('tokens')} tok]\n"
                self.transcript.insert("end", hdr, "guild")
                if r.get("ok"):
                    self._insert_markdown_block(r.get("display_markdown") or r.get("reply", ""), indent="  ")
                else:
                    self.transcript.insert("end", f"  (Status: {r.get('error', 'unavailable')})\n\n", "muted")
            syn = t.get("synthesis")
            if syn and syn.get("reply"):
                self.transcript.insert("end", f"  [Hive Synthesis · {syn.get('speaker')} · {syn.get('latency_sec')}s]\n", "synth")
                self._insert_markdown_block(syn.get("display_markdown") or syn.get("reply", ""), indent="  ")
            self.transcript.insert("end", "─" * 72 + "\n\n", "muted")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _clear_chat(self) -> None:
        call_console_backend("SABRINA_SWARM_API_BASE", "/api/swarm-chat/clear", clear_chat_history, {})
        self._render_history([])

    def _fork_last_turn(self) -> None:
        turns = self._chat_history()
        if not turns:
            self.status_var.set("No turn available to fork.")
            return
        last_turn = turns[-1]
        res = call_console_backend(
            "SABRINA_SWARM_API_BASE", "/api/swarm-chat/fork",
            lambda: fork_chat_history(last_turn["id"]), {"turn_id": last_turn["id"]},
        )
        self.status_var.set(f"Forked conversation to branch '{res['title']}' ({res['thread_id']})")
        self._render_history(res["history"])

    def _on_send(self) -> None:
        if self._busy:
            return
        msg = self.input_box.get().strip()
        if not msg:
            return
        mode = self.mode_var.get()
        target_id = self._node_id_by_label.get(self.target_var.get(), "local:qwen3:14b")
        self.input_box.delete(0, "end")
        self._busy = True
        self.send_btn.configure(state="disabled")
        self._chat_operation_id = uuid.uuid4().hex
        operation_id = self._chat_operation_id
        self.status_var.set(f"Dispatching job {operation_id} to Swarm ({mode})")
        print(f"Swarm Chat operation ID: {operation_id}", flush=True)

        def progress(message: str) -> None:
            self.after(0, lambda value=message: self.status_var.set(value))

        def worker() -> None:
            try:
                turn = run_console_chat(
                    msg, mode, target_id, operation_id, progress,
                    lambda: send_swarm_chat(msg, mode=mode, target=target_id, progress=progress),
                )
                self.after(0, lambda: self._finish_send(f"Job {operation_id} completed in {turn['total_elapsed_sec']}s"))
            except Exception as exc:
                self.after(0, lambda err=str(exc): self._finish_send(f"Job {operation_id}: {err}"))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_send(self, status_msg: str) -> None:
        self._busy = False
        self.send_btn.configure(state="normal")
        self.status_var.set(status_msg)
        self._render_history(self._chat_history())


# ===========================================================================
# Standalone Web Portal & JSON API (`http://127.0.0.1:8095`)
# ===========================================================================
PORTAL_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Sabrina Multi-Family Swarm Collective Chat Portal</title>
  <style>
    * { box-sizing: border-box; }
    body { margin: 0; font: 14px/1.55 system-ui, -apple-system, Segoe UI, sans-serif; background: #0b1120; color: #e2e8f0; display: flex; flex-direction: column; height: 100vh; }
    header { padding: 14px 22px; background: #111827; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
    header h1 { margin: 0; font-size: 18px; color: #f8fafc; }
    .pill { font-size: 12px; padding: 4px 10px; border-radius: 999px; background: #064e3b; color: #6ee7b7; border: 1px solid #047857; font-family: monospace; }
    .toolbar { padding: 10px 22px; background: #0f172a; border-bottom: 1px solid #1e293b; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    select, input, textarea, button { font: inherit; background: #1e293b; color: #f8fafc; border: 1px solid #334155; border-radius: 8px; padding: 8px 12px; }
    button { background: #2563eb; border-color: #1d4ed8; font-weight: 600; cursor: pointer; }
    button.secondary { background: #334155; border-color: #475569; }
    button:disabled { opacity: 0.5; cursor: wait; }
    #chatStream { flex: 1; overflow-y: auto; padding: 18px 22px; display: flex; flex-direction: column; gap: 16px; }
    .turn { background: #111827; border: 1px solid #1f2937; border-radius: 12px; padding: 14px 16px; }
    .user-msg { color: #93c5fd; font-weight: 600; margin-bottom: 12px; }
    .turn-hdr { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 6px; }
    .fork-btn { background: #1e293b; border: 1px solid #38bdf866; color: #7dd3fc; font-size: 11.5px; padding: 3px 9px; border-radius: 6px; cursor: pointer; font-weight: 600; }
    .fork-btn:hover { background: #0284c733; border-color: #38bdf8; }
    .fork-badge { display: inline-block; font-size: 11px; color: #fcd34d; background: #78350f44; border: 1px solid #f59e0b55; border-radius: 4px; padding: 1px 6px; margin-left: 8px; font-family: monospace; }
    .api-tier-off { background: #064e3b !important; border-color: #059669 !important; color: #6ee7b7 !important; font-size: 12px; }
    .api-tier-on { background: #7c2d12 !important; border-color: #ea580c !important; color: #fdba74 !important; font-size: 12px; }
    .guild-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 10px; margin-bottom: 12px; }
    .guild-card { background: #0f172a; border: 1px solid #334155; border-radius: 10px; padding: 12px 14px; }
    .guild-hdr { font-size: 13px; font-weight: 700; color: #fcd34d; margin-bottom: 6px; display: flex; justify-content: space-between; gap: 6px; }
    .guild-body { font-size: 14px; line-height: 1.6; color: #f1f5f9; }
    .synth-card { background: #064e3b66; border: 1px solid #10b98188; border-radius: 10px; padding: 14px 16px; }
    .synth-hdr { font-size: 13.5px; font-weight: 700; color: #6ee7b7; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; gap: 8px; }
    .synth-body { font-size: 14.5px; line-height: 1.65; color: #f8fafc; }
    .promote-btn { background: #4f46e5; border: 1px solid #6366f1; color: #ffffff; font-size: 12px; padding: 4px 10px; border-radius: 6px; cursor: pointer; font-weight: 600; }
    .promote-btn:hover { background: #4338ca; }
    .md-body p { margin: 0 0 8px 0; }
    .md-body p:last-child { margin-bottom: 0; }
    .md-body h1, .md-body h2, .md-body h3, .md-body h4 { margin: 8px 0 4px 0; color: #f8fafc; font-weight: 700; line-height: 1.3; }
    .md-body h1 { font-size: 16px; }
    .md-body h2 { font-size: 15px; }
    .md-body h3, .md-body h4 { font-size: 13.5px; color: #93c5fd; }
    .md-body ul, .md-body ol { margin: 4px 0 8px 18px; padding: 0; }
    .md-body li { margin: 2px 0; }
    .md-body code { background: #1e293b; color: #67e8f9; padding: 1px 5px; border-radius: 4px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; }
    .md-body pre { background: #020617; border: 1px solid #1e293b; border-radius: 6px; padding: 8px 10px; overflow-x: auto; margin: 6px 0; }
    .md-body pre code { background: transparent; padding: 0; color: #e2e8f0; }
    .md-body blockquote { margin: 6px 0; padding: 4px 10px; border-left: 3px solid #3b82f6; background: #1e293b66; color: #cbd5e1; }
    details { margin-top: 6px; font-size: 12px; color: #94a3b8; }
    footer { padding: 12px 22px; background: #111827; border-top: 1px solid #1f2937; display: flex; gap: 10px; }
    footer textarea { flex: 1; height: 54px; resize: none; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Sabrina Multi-Family Swarm Collective Chat</h1>
      <small style="color:#94a3b8">Verbatim Multi-Lineage Attribution &bull; Local ego Fleet (Llama, Granite, Mistral, Qwen, Jury) + WSP Zero-Auth hf-gradio Guilds</small>
    </div>
    <span class="pill" id="statusPill">0 Cloud Tokens &bull; Ready</span>
  </header>
  <div class="toolbar">
    <label>Thread / Branch:
      <select id="threadSel" onchange="switchThread(this.value)"></select>
    </label>
    <label>Routing Mode:
      <select id="modeSel" onchange="toggleTarget()">
        <option value="hive_council">🏛️ Full Swarm Council (Local ego + HF Guilds + Qwen3 Chair)</option>
        <option value="external_council">🌐 Public HF Guilds Only (External Zero-Auth)</option>
        <option value="direct_node">🎯 1-on-1 Direct Model Picker</option>
      </select>
    </label>
    <label id="targetWrap" style="display:none">Target Model:
      <select id="targetSel" style="max-width:340px"></select>
    </label>
    <label style="font-size:12.5px;color:#cbd5e1;display:flex;align-items:center;gap:5px;cursor:pointer">
      <input type="checkbox" id="hideProbesChk" checked onchange="loadRosterAndHistory()" style="padding:0"> Hide Automated Probe Packets
    </label>
    <button id="apiTiersBtn" class="secondary api-tier-off" onclick="toggleApiTiers()" title="Toggle paid/API-key cloud tiers (OpenAI, Anthropic, Gemini, HF Router) vs. Open Zero-Auth Web Portals + Local Fleet">API Tiers: OFF (Open Web + Local Only)</button>
    <button class="secondary" onclick="loadRosterAndHistory()">Refresh Status</button>
    <button class="secondary" onclick="clearChat()">Clear Branch</button>
  </div>
  <main id="chatStream"></main>
  <footer>
    <textarea id="msgBox" placeholder="Ask the Multi-Family Swarm Collective anything (Ctrl+Enter or Enter to send)..." onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg();}"></textarea>
    <button id="sendBtn" onclick="sendMsg()">Send to Swarm</button>
  </footer>
  <script>
    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }
    function jsonToMd(val, depth = 0) {
      const pad = '  '.repeat(depth);
      if (Array.isArray(val)) {
        if (!val.length) return pad + '- *(none)*';
        return val.map((item, i) => (item && typeof item === 'object') ? pad + (i + 1) + '.\\n' + jsonToMd(item, depth + 1) : pad + '- ' + String(item)).join('\\n');
      }
      if (val && typeof val === 'object') {
        const keys = Object.keys(val);
        if (!keys.length) return pad + '- *(empty)*';
        return keys.map(k => {
          const label = k.replace(/_/g, ' ');
          const v = val[k];
          if (Array.isArray(v)) {
            if (!v.length) return pad + '- **' + label + '**: *(none)*';
            if (v.every(x => !x || typeof x !== 'object')) return pad + '- **' + label + '**: ' + v.map(x => '`' + String(x) + '`').join(', ');
            return pad + '- **' + label + '**:\\n' + jsonToMd(v, depth + 1);
          }
          if (v && typeof v === 'object') return pad + '- **' + label + '**:\\n' + jsonToMd(v, depth + 1);
          if (typeof v === 'boolean' || v === null) return pad + '- **' + label + '**: `' + String(v) + '`';
          return pad + '- **' + label + '**: ' + String(v);
        }).join('\\n');
      }
      return pad + String(val);
    }
    function normalizeMdSource(raw) {
      const s = String(raw || '').trim();
      if (!s) return { md: '', rawJson: null };
      let candidate = s;
      const fenceMatch = s.match(/^```(?:json)?\\s*([\\s\\S]*?)\\s*```$/i);
      if (fenceMatch) candidate = fenceMatch[1].trim();
      if ((candidate.startsWith('{') && candidate.endsWith('}')) || (candidate.startsWith('[') && candidate.endsWith(']'))) {
        try {
          const parsed = JSON.parse(candidate);
          if (parsed && typeof parsed === 'object') return { md: jsonToMd(parsed), rawJson: candidate };
        } catch (_) {}
      }
      const braceIdx = s.indexOf('{');
      if (braceIdx > 0 && s.endsWith('}')) {
        const prefix = s.slice(0, braceIdx).trim();
        const tail = s.slice(braceIdx).trim();
        try {
          const parsed = JSON.parse(tail);
          if (parsed && typeof parsed === 'object') {
            return { md: prefix + '\\n\\n### Structured Context\\n' + jsonToMd(parsed), rawJson: tail };
          }
        } catch (_) {}
      }
      return { md: s, rawJson: null };
    }
    function formatInlineMd(text) {
      let out = esc(text);
      out = out.replace(/`([^`]+)`/g, '<code>$1</code>');
      out = out.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
      out = out.replace(/(^|[^*])\\*([^*\\n]+)\\*/g, '$1<em>$2</em>');
      return out;
    }
    function renderMarkdown(raw) {
      const { md, rawJson } = normalizeMdSource(raw);
      if (!md) return '';
      const lines = md.split(/\\r?\\n/);
      let html = '';
      let inCode = false, codeBuf = [];
      let inUl = false, inOl = false;
      const closeLists = () => {
        if (inUl) { html += '</ul>'; inUl = false; }
        if (inOl) { html += '</ol>'; inOl = false; }
      };
      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith('```')) {
          closeLists();
          if (!inCode) { inCode = true; codeBuf = []; }
          else { html += '<pre><code>' + esc(codeBuf.join('\\n')) + '</code></pre>'; inCode = false; }
          continue;
        }
        if (inCode) { codeBuf.push(line); continue; }
        if (!trimmed) { closeLists(); continue; }
        const hMatch = trimmed.match(/^(#{1,4})\\s+(.*)$/);
        if (hMatch) {
          closeLists();
          const lvl = hMatch[1].length;
          html += '<h' + lvl + '>' + formatInlineMd(hMatch[2]) + '</h' + lvl + '>';
          continue;
        }
        const ulMatch = line.match(/^\\s*[-*]\\s+(.*)$/);
        if (ulMatch) {
          if (inOl) { html += '</ol>'; inOl = false; }
          if (!inUl) { html += '<ul>'; inUl = true; }
          html += '<li>' + formatInlineMd(ulMatch[1]) + '</li>';
          continue;
        }
        const olMatch = line.match(/^\\s*\\d+\\.\\s+(.*)$/);
        if (olMatch) {
          if (inUl) { html += '</ul>'; inUl = false; }
          if (!inOl) { html += '<ol>'; inOl = true; }
          html += '<li>' + formatInlineMd(olMatch[1]) + '</li>';
          continue;
        }
        if (trimmed.startsWith('>')) {
          closeLists();
          html += '<blockquote>' + formatInlineMd(trimmed.replace(/^>\\s?/, '')) + '</blockquote>';
          continue;
        }
        closeLists();
        html += '<p>' + formatInlineMd(line) + '</p>';
      }
      closeLists();
      if (inCode) html += '<pre><code>' + esc(codeBuf.join('\\n')) + '</code></pre>';
      if (rawJson) {
        html += '<details><summary>View verbatim JSON payload</summary><pre><code>' + esc(rawJson) + '</code></pre></details>';
      }
      return '<div class="md-body">' + html + '</div>';
    }
    function toggleTarget() { document.getElementById('targetWrap').style.display = document.getElementById('modeSel').value === 'direct_node' ? 'inline-block' : 'none'; }
    let currentApiTiersEnabled = false;
    function updateApiTierButton(enabled, openCount) {
      currentApiTiersEnabled = !!enabled;
      const btn = document.getElementById('apiTiersBtn');
      if (!btn) return;
      if (currentApiTiersEnabled) {
        btn.className = 'secondary api-tier-on';
        btn.textContent = 'API Tiers: ON (Cloud Keys Enabled)';
      } else {
        btn.className = 'secondary api-tier-off';
        btn.textContent = 'API Tiers: OFF (' + (openCount || 76) + ' Open Web Portals + Local Fleet)';
      }
    }
    function renderThreadsDropdown(threads, activeId) {
      const tSel = document.getElementById('threadSel');
      if (!tSel) return;
      tSel.innerHTML = '';
      for (const th of (threads || [{thread_id: 'main', title: 'Main Thread', turn_count: 0}])) {
        const o = document.createElement('option');
        o.value = th.thread_id;
        o.textContent = th.title + ' (' + (th.turn_count || 0) + ')';
        if (th.thread_id === activeId) o.selected = true;
        tSel.appendChild(o);
      }
    }
    async function switchThread(threadId) {
      const res = await fetch('/api/swarm-chat/thread', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ thread_id: threadId })
      });
      const data = await res.json();
      renderThreadsDropdown(data.threads, data.active_thread_id);
      renderHistory(data.history || []);
      document.getElementById('statusPill').textContent = 'Switched to branch: ' + data.active_thread_id;
    }
    async function forkTurn(turnId) {
      const res = await fetch('/api/swarm-chat/fork', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ turn_id: turnId })
      });
      const data = await res.json();
      if (!res.ok) {
        document.getElementById('statusPill').textContent = 'Fork error: ' + (data.detail || 'failed');
        return;
      }
      renderThreadsDropdown(data.threads, data.active_thread_id);
      renderHistory(data.history || []);
      document.getElementById('statusPill').textContent = 'Forked new branch: ' + data.title;
      const box = document.getElementById('msgBox');
      if (box) box.focus();
    }
    async function toggleApiTiers() {
      const nextState = !currentApiTiersEnabled;
      const res = await fetch('/api/swarm-chat/api-tiers', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ enabled: nextState })
      });
      const data = await res.json();
      if (res.ok) {
        updateApiTierButton(data.api_tiers_enabled, data.roster && data.roster.open_web_portals_count);
        document.getElementById('statusPill').textContent = data.api_tiers_enabled
          ? 'API-Key Cloud Tiers ENABLED'
          : 'API-Key Cloud Tiers OFF · Open Web Portals + Local Fleet Active';
        await loadRosterAndHistory();
      }
    }
    let replyPayloadMap = {};
    async function promoteToSabrina(key) {
      const text = replyPayloadMap[key];
      if (!text) return;
      document.getElementById('statusPill').textContent = 'Directing Sabrina Active Objective...';
      try {
        const res = await fetch('/api/active-prompt/direct', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ prompt: text, execution_mode: 'autonomous_objective' })
        });
        const d = await res.json();
        if (!res.ok) throw new Error(d.detail || 'Failed');
        document.getElementById('statusPill').textContent = '⚡ Active Objective Started: ' + (d.objective_id || 'OK');
      } catch (e) {
        document.getElementById('statusPill').textContent = 'Active Prompt error: ' + e.message;
      }
    }
    async function loadRosterAndHistory() {
      const [rRes, hRes] = await Promise.all([fetch('/api/swarm-chat/roster'), fetch('/api/swarm-chat/history')]);
      const roster = await rRes.json();
      const hist = await hRes.json();
      updateApiTierButton(roster.api_tiers_enabled, roster.open_web_portals_count);
      renderThreadsDropdown(hist.threads || roster.threads, hist.active_thread_id || roster.active_thread_id);
      const sel = document.getElementById('targetSel');
      const prev = sel.value;
      sel.innerHTML = '';
      const og1 = document.createElement('optgroup'); og1.label = '★ Local ego Multi-Family Fleet (RTX 5060 Ti)';
      for (const n of roster.local_nodes) { const o = document.createElement('option'); o.value = n.id; o.textContent = n.label; og1.appendChild(o); }
      const og2 = document.createElement('optgroup'); og2.label = '★ Ready Public HF Guilds (' + (roster.eligible_external_count || 0) + ' Ready)';
      for (const p of (roster.external_nodes || []).filter(x => x.chat_eligible)) {
        const o = document.createElement('option');
        o.value = p.id;
        o.textContent = (p.curated_guild ? '★ ' : '') + p.label + ' [' + p.status_badge + ']';
        og2.appendChild(o);
      }
      sel.appendChild(og1); sel.appendChild(og2);
      if (prev) sel.value = prev;
      renderHistory(hist.history || []);
    }
    function renderHistory(turns) {
      const stream = document.getElementById('chatStream');
      const hideProbes = document.getElementById('hideProbesChk') ? document.getElementById('hideProbesChk').checked : true;
      const visible = hideProbes ? turns.filter(t => !t.is_automated_probe) : turns;
      const hiddenCount = turns.length - visible.length;
      replyPayloadMap = {};
      if (!visible.length) {
        stream.innerHTML = '<div class="turn" style="color:#cbd5e1;font-size:14px">No operator chat messages in this branch yet' + (hiddenCount ? ' (' + hiddenCount + ' automated diagnostic probe packets hidden — uncheck <b>Hide Automated Probe Packets</b> above to view them)' : '') + '.<br><br>Send a prompt below to convene <b>Meta Llama</b>, <b>IBM Granite</b>, <b>Mistral</b>, <b>HF Guilds</b>, and <b>Qwen3</b>, then click <b>⚡ Direct Sabrina with This Plan</b> on any answer to execute it!</div>';
        return;
      }
      stream.innerHTML = visible.map((t, tIdx) => {
        const cards = (t.responses || []).map((r, rIdx) => {
          const pKey = 'r_' + tIdx + '_' + rIdx;
          if (r.ok && r.reply) replyPayloadMap[pKey] = 'Execute plan from ' + r.speaker + ' for "' + (t.user_message || '').slice(0, 140) + '":\\n\\n' + r.reply;
          const body = r.ok ? '<div class="guild-body">' + renderMarkdown(r.display_markdown || r.reply) + '</div>' : '<div class="guild-body" style="color:#cbd5e1"><i>' + esc(r.error) + '</i></div>';
          const think = r.thinking ? '<details><summary>View &lt;think&gt; trace</summary><div class="guild-body">' + renderMarkdown(r.thinking) + '</div></details>' : '';
          const actBtn = (r.ok && r.reply) ? '<div style="margin-top:8px"><button class="promote-btn" onclick="promoteToSabrina(\\'' + pKey + '\\')">⚡ Direct Sabrina with This Reply</button></div>' : '';
          return '<div class="guild-card"><div class="guild-hdr"><span>' + esc(r.family) + '</span><span>' + esc(r.latency_sec + 's' + (r.tokens != null ? ' · ' + r.tokens + ' tok' : '')) + '</span></div><div style="font-size:12px;color:#7dd3fc;margin-bottom:6px;font-family:monospace">' + esc(r.speaker) + '</div>' + body + think + actBtn + '</div>';
        }).join('');
        let syn = '';
        if (t.synthesis && t.synthesis.reply) {
          const sKey = 's_' + tIdx;
          replyPayloadMap[sKey] = 'Execute Swarm Council Synthesis for "' + (t.user_message || '').slice(0, 140) + '":\\n\\n' + t.synthesis.reply;
          syn = '<div class="synth-card"><div class="synth-hdr"><span>Cross-Family Synthesis (' + esc(t.synthesis.speaker) + ' · ' + esc(t.synthesis.latency_sec + 's') + ')</span><button class="promote-btn" onclick="promoteToSabrina(\\'' + sKey + '\\')">⚡ Direct Sabrina with Synthesis</button></div><div class="synth-body">' + renderMarkdown(t.synthesis.display_markdown || t.synthesis.reply) + '</div>' + (t.synthesis.thinking ? '<details><summary>View Chair &lt;think&gt; trace</summary><div class="guild-body">' + renderMarkdown(t.synthesis.thinking) + '</div></details>' : '') + '</div>';
        }
        const forkBadge = t.forked_from_turn_id ? '<span class="fork-badge">⑂ Forked from ' + esc(t.forked_from_turn_id) + '</span>' : '';
        const forkBtn = t.id ? '<button class="fork-btn" onclick="forkTurn(\\'' + esc(t.id) + '\\')" title="Branch conversation from this turn into a new thread">⑂ Fork from here</button>' : '';
        return '<div class="turn"><div class="user-msg"><div class="turn-hdr"><span>You (' + esc(t.mode) + ')' + forkBadge + ':</span>' + forkBtn + '</div>' + renderMarkdown(t.user_message_markdown || t.user_message) + '</div><div class="guild-grid">' + cards + '</div>' + syn + '</div>';
      }).join('');
      stream.scrollTop = stream.scrollHeight;
    }
    async function sendMsg() {
      const box = document.getElementById('msgBox');
      const msg = box.value.trim();
      if (!msg) return;
      const mode = document.getElementById('modeSel').value;
      const target = document.getElementById('targetSel').value;
      box.value = '';
      document.getElementById('sendBtn').disabled = true;
      document.getElementById('statusPill').textContent = 'Swarm deliberating (' + mode + ')...';
      try {
        const res = await fetch('/api/swarm-chat/send', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ message: msg, mode, target }) });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || 'Swarm chat error');
        document.getElementById('statusPill').textContent = 'Completed in ' + data.total_elapsed_sec + 's · 0 Cloud Tokens';
        await loadRosterAndHistory();
      } catch (e) {
        document.getElementById('statusPill').textContent = 'Error: ' + e.message;
      } finally {
        document.getElementById('sendBtn').disabled = false;
      }
    }
    async function clearChat() {
      await fetch('/api/swarm-chat/clear', { method: 'POST' });
      await loadRosterAndHistory();
    }
    loadRosterAndHistory();
  </script>
  <script src="/chat-client.js"></script>
</body>
</html>
"""


def create_app(jobs_path=None, *, port=8095, telemetry_only=False):
    if telemetry_only:
        from tools.model_telemetry_server import create_app as telemetry_app
        return telemetry_app(port=port)
    from fastapi import Body, FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import HTMLResponse
    from tools.swarm_chat_jobs import ChatJobs

    app = FastAPI(title="Sabrina Multi-Family Swarm Chat Portal", version="1.0.0")
    jobs = ChatJobs(jobs_path or ROOT_DIR / 'data' / 'swarm_chat_jobs.sqlite3', send_swarm_chat)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"status": "ok", "service": "swarm_chat_portal", "port": 8095}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PORTAL_HTML

    @app.get('/chat-client.js')
    def chat_client():
        from fastapi.responses import Response
        return Response((ROOT_DIR / 'tools' / 'swarm_chat_client.js').read_text(encoding='utf-8'),
                        media_type='application/javascript', headers={'Cache-Control': 'no-store'})

    @app.post('/api/swarm-chat/jobs', status_code=202)
    def submit_job(payload: Dict[str, Any] = Body(...)):
        try:
            return jobs.submit(str(payload.get('operation_id', '')), str(payload.get('message', '')),
                               str(payload.get('mode', 'external_council')), str(payload.get('target', 'local:qwen3:14b')))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/swarm-chat/jobs/{job_id}')
    def job_status(job_id: str):
        try:
            return jobs.get(job_id)
        except KeyError:
            raise HTTPException(404, 'Chat receipt not found; do not automatically resend.')

    @app.get("/api/swarm-chat/roster")
    def api_roster() -> Dict[str, Any]:
        return get_swarm_roster()

    @app.get("/api/swarm-chat/history")
    def api_history(thread_id: Optional[str] = None) -> Dict[str, Any]:
        info = list_chat_threads()
        raw_turns = get_chat_history(thread_id=thread_id)
        enriched = [{**t, "is_automated_probe": _is_automated_json_probe_turn(t)} for t in raw_turns]
        return {
            "history": enriched,
            "active_thread_id": info["active_thread_id"],
            "threads": info["threads"],
        }

    @app.get("/api/swarm/telemetry")
    def api_swarm_telemetry() -> Dict[str, Any]:
        """Live swarm telemetry indexed by agent_id, strictly separated from operator chat history."""
        try:
            from tools.telemetry_blackboard import get_telemetry_blackboard
            bb = get_telemetry_blackboard()
            telemetry = bb.get_latest_telemetry_by_agent()
        except Exception:
            telemetry = {}
        return {
            "ok": True,
            "service": "swarm_chat_portal",
            "source": "dnd_rsi_blackboard.sqlite3",
            "telemetry_by_agent": telemetry,
            "agent_telemetry": telemetry,
        }

    @app.get("/api/active-prompt/state")
    def api_active_prompt_state() -> Dict[str, Any]:
        from tools.active_prompt_director import get_active_prompt_state
        return get_active_prompt_state()

    @app.post("/api/active-prompt/direct")
    def api_active_prompt_direct(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        from tools.active_prompt_director import direct_sabrina_prompt
        try:
            return direct_sabrina_prompt(
                prompt=str(payload.get("prompt", "")),
                execution_mode=str(payload.get("execution_mode", "autonomous_objective")),
                physical_input=bool(payload.get("physical_input", False)),
                target_objective_id=payload.get("target_objective_id"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/active-prompt/control")
    def api_active_prompt_control(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        from tools.active_prompt_director import control_active_prompt
        try:
            return control_active_prompt(
                objective_id=str(payload.get("objective_id", "")),
                action=str(payload.get("action", "")),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/swarm-chat/clear")
    def api_clear(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
        body = payload or {}
        return clear_chat_history(
            thread_id=body.get("thread_id"),
            all_threads=bool(body.get("all_threads", False)),
        )

    @app.post("/api/swarm-chat/fork")
    def api_fork(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        try:
            return fork_chat_history(
                str(payload.get("turn_id", "")),
                title=payload.get("title"),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/swarm-chat/thread")
    def api_switch_thread(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return set_active_chat_thread(str(payload.get("thread_id", "main")))

    @app.post("/api/swarm-chat/api-tiers")
    def api_toggle_api_tiers(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        return set_swarm_api_tiers(bool(payload.get("enabled", False)))

    @app.post("/api/swarm-chat/send")
    def api_send(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        try:
            ctx = payload.get("context")
            return send_swarm_chat(
                str(payload.get("message", "")),
                mode=str(payload.get("mode", "external_council")),
                target=str(payload.get("target", "local:qwen3:14b")),
                context=ctx if isinstance(ctx, dict) else None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/frontier-research/state")
    def api_frontier_state(limit: int = 50) -> Dict[str, Any]:
        from tools.frontier_research_guild import list_frontier_research_queue
        return list_frontier_research_queue(limit=limit, db_path=PORTAL_DB_PATH)

    @app.post("/api/frontier-research/budget")
    def api_frontier_budget(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        from tools.frontier_research_guild import update_weekly_budget
        try:
            return update_weekly_budget(
                provider_id=str(payload.get("provider_id", "")),
                weekly_budget_tokens=payload.get("weekly_budget_tokens"),
                enabled=payload.get("enabled"),
                db_path=PORTAL_DB_PATH,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/frontier-research/enqueue")
    def api_frontier_enqueue(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        from tools.frontier_research_guild import enqueue_frontier_research_task
        try:
            return enqueue_frontier_research_task(
                capability=str(payload.get("capability", "frontier.research.rsi_augment")),
                title=str(payload.get("title", "")),
                prompt=str(payload.get("prompt", "")),
                provider_id=str(payload.get("provider_id", "auto")),
                model_id=str(payload.get("model_id", "auto")),
                thinking_budget_tokens=int(payload.get("thinking_budget_tokens", 12000)),
                schedule_iso=payload.get("schedule_iso") or None,
                duration_minutes=int(payload.get("duration_minutes", 45)),
                repeat_every_minutes=int(payload["repeat_every_minutes"]) if payload.get("repeat_every_minutes") else None,
                run_immediately=bool(payload.get("run_immediately", False)),
                db_path=PORTAL_DB_PATH,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/frontier-research/run/{task_id}")
    def api_frontier_run(task_id: str) -> Dict[str, Any]:
        from tools.frontier_research_guild import execute_frontier_research_task
        try:
            return execute_frontier_research_task(task_id, db_path=PORTAL_DB_PATH)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/frontier-research/seed-weekly")
    def api_frontier_seed_weekly(payload: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
        from tools.frontier_research_guild import seed_weekly_frontier_research_schedule
        body = payload or {}
        return seed_weekly_frontier_research_schedule(
            anchor_date=body.get("anchor_date"),
            db_path=PORTAL_DB_PATH,
        )

    @app.get("/api/calendar/granular")
    def api_calendar_granular(anchor_iso: Optional[str] = None, view_mode: str = "week", selected_hour: Optional[int] = None) -> Dict[str, Any]:
        from tools.sabrina_console_calendar import get_granular_calendar_data
        return get_granular_calendar_data(anchor_iso=anchor_iso, view_mode=view_mode, selected_hour=selected_hour)

    @app.post("/api/calendar/allocate")
    def api_calendar_allocate(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        from tools.sabrina_console_calendar import schedule_calendar_block
        try:
            return schedule_calendar_block(
                capability=str(payload.get("capability", "frontier.research.rsi_augment")),
                start_iso=str(payload.get("start_iso", "")),
                duration_minutes=int(payload.get("duration_minutes", 45)),
                repeat_every_minutes=int(payload["repeat_every_minutes"]) if payload.get("repeat_every_minutes") else None,
                objective=str(payload.get("objective", "")),
                mode=str(payload.get("mode", "codex_thought_antigravity_implement")),
                payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                approved_at_schedule=bool(payload.get("approved_at_schedule", False)),
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/calendar/seed-daily")
    def api_calendar_seed_daily(anchor_date: Optional[str] = None) -> Dict[str, Any]:
        from tools.sabrina_console_calendar import seed_default_daily_blocks
        return seed_default_daily_blocks(anchor_date=anchor_date)

    return app


def main() -> None:
    import uvicorn

    if sys.stdout is None or sys.stderr is None:
        log_dir = ROOT_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        bg_log = (log_dir / "swarm_chat_portal.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = bg_log

    parser = argparse.ArgumentParser(description="Sabrina Multi-Family Swarm Chat Portal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8095)
    parser.add_argument("--telemetry-only", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        create_app(port=args.port, telemetry_only=args.telemetry_only),
        host=args.host,
        port=args.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
