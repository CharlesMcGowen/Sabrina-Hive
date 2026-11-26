#!/usr/bin/env python3
"""
Sabrina Autonomous Collective v11: Swarm-Driven Portal Hunter, Response Role Diagnostician & Qualifier
======================================================================================================
Orchestrates the active Sabrina swarm agents across Tier 1 (Flash), Tier 2 (Deep Reasoning),
and Tier 3 (Domain RAG) to:
  1. Nominate new agentic/LLM search vectors & diagnose CEGAR failure modes on near-miss harnesses.
  2. Repair near-miss (`delivery=1.0, correctness=0.5`) and high-value general LLM harnesses in
     `Sabrina/harnesses/external/` using parameter-name-aware `/gradio_api/info` slider bounds
     (preventing `max_new_tokens` from being clamped to `top_k` bounds) and CS Exam Study Guide
     framing so tutor/study-planner agents answer in-character.
  3. Sweep 65+ brand-new agentic & frontier-LLM Hugging Face Spaces queries (`smolagents`,
     `mcp`, `pydantic-ai`, `agno`, `crewai`, `autogen`, `langgraph`, `dify`, `flowise`, `langflow`,
     `qwen2.5-coder`, `qwq`, `deepseek-r1`, `open-webui`, `chat-ui`, `sonnet`, `o3`, `vllm`,
     `litellm`, `openrouter`, `groq`, `cerebras`, `sambanova`, `together`, `phi-4`, `nemotron`, etc.).
  4. Diagnose each responding portal's true place in the swarm (`KindCollectivePool.diagnose_response_profile`)
     from its actual replies on math/code synthesis (`code_clamp_nan`) and systems concurrency critique
     (`audit_critique_race`), persisting `diagnosed_tier`, `roles`, and `PortalPolicy` role qualifications
     (`code_generation`, `critique`, `grounded_summarization`, `structured_extraction`, `translation`)
     into SQLite (`assistant_external_portals`, `portal_profiles`, `portal_role_qualifications`, and
     `model_competence_matrix`).
"""

from __future__ import annotations

import ast
import concurrent.futures
import json
import py_compile
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from harnesses.external.cognitive_blackboard import HierarchicalCognitiveBlackboard
from harnesses.external.collective_pool import DEFAULT_DB_PATH, HARNESS_DIR, KindCollectivePool
from tools.collective_router_and_shadow_daemon import CollectiveRouter
from tools.external_mind_scout import ExternalMindScout, PORTAL_BENCHMARK_SUITE, compute_scout_harness_hash
from tools.portal_policy import PortalPolicy, ROLES, TTL
from tools.run_scenario_b_mass_qualification import register_portal_in_db
from tools.run_self_organizing_collective_recruitment_loop import load_harness_module
from tools.run_v10_zero_skip_recovery_sweep import normalize_hf_subdomain

ARTIFACT_DIR = ROOT_DIR / "artifacts" / "collective-repair-20261108"
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
V11_REPORT_PATH = ARTIFACT_DIR / "swarm_portal_hunter_v11.json"


def verify_clamp_ratio_ast(code_text: str) -> Dict[str, Any]:
    """Authoritative AST + local execution check for clamp_ratio(val, lo, hi)."""
    if not isinstance(code_text, str) or not code_text.strip():
        return {"passed": False, "status": "missing", "failed_criteria": ["empty_candidate"]}
    clean = code_text.strip()
    if "```" in clean:
        for part in clean.split("```"):
            s = part.strip()
            if s.startswith("python"):
                s = s[6:].strip()
            if "def clamp_ratio" in s:
                clean = s
                break
    try:
        tree = ast.parse(clean)
    except SyntaxError as exc:
        return {"passed": False, "status": "malformed", "failed_criteria": [f"syntax_error:{exc.msg}"]}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name.split(".")[0] for alias in node.names]
            if any(n not in {"math", "threading", "typing"} for n in names):
                return {"passed": False, "status": "failed", "failed_criteria": ["unauthorized_import"]}
    ns: Dict[str, Any] = {}
    try:
        exec(compile(tree, "<candidate>", "exec"), ns)
    except Exception as exc:
        return {"passed": False, "status": "failed", "failed_criteria": [f"exec:{type(exc).__name__}"]}
    fn = ns.get("clamp_ratio")
    if not callable(fn):
        return {"passed": False, "status": "failed", "failed_criteria": ["missing_clamp_ratio"]}
    failed: List[str] = []
    try:
        if not (fn(0.5, 0.0, 1.0) == 0.5 and fn(-2.0, 0.0, 1.0) == 0.0 and fn(5.0, 0.0, 1.0) == 1.0):
            failed.append("normal_clamping")
    except Exception:
        failed.append("normal_clamping")
    for label, call_args in [
        ("nan_value_rejected", (float("nan"), 0.0, 1.0)),
        ("nan_bounds_rejected", (0.5, float("nan"), 1.0)),
        ("inverted_bounds_rejected", (0.5, 2.0, 1.0)),
    ]:
        try:
            fn(*call_args)
            failed.append(label)
        except ValueError:
            pass
        except Exception:
            failed.append(label)
    return {"passed": len(failed) == 0, "status": "verified" if not failed else "failed", "failed_criteria": failed}


# ---------------------------------------------------------------------------
# 1. Parameter-Aware Universal Gradio `/info` Inspector (Fixes Slider Clamping)
# ---------------------------------------------------------------------------

BANNED_ENDPOINT_WORDS = {
    "clear", "reset", "undo", "retry", "delete", "stop", "cancel", "load",
    "init", "login", "logout", "auth", "upvote", "downvote", "flag", "like",
    "share", "export", "download", "upload", "change", "update", "select",
    "toggle", "switch", "refresh", "ping", "health", "status", "lambda",
    "noop", "test_api", "get_", "set_", "list_", "fetch_", " check_",
    "count_", "tokenize", "filter_", "search_table", "regex_table",
}

PREFERRED_ENDPOINT_NAMES = [
    "/chat", "/respond", "/predict", "/generate", "/ask", "/submit",
    "/invoke", "/run", "/inference", "/completion", "/query", "/reply",
    "/bot", "/agent", "/assistant", "/talk", "/send", "/chat_fn",
    "/handle", "/process", "/call", "/stream", "/answer",
]


def inspect_gradio_info_v11(space_id: str, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    """
    Inspects `/gradio_api/info` or `/info` and builds a parameter-aware template
    where `max_tokens`/`max_new_tokens` is set to `min(1024, maximum)` and `top_k`
    is set to `min(50, maximum)` without ever conflating the two.
    """
    sub = normalize_hf_subdomain(space_id)
    base_url = f"https://{sub}.hf.space"

    for info_suffix, call_prefix in [
        ("/gradio_api/info", "/gradio_api/call"),
        ("/info", "/call"),
    ]:
        info_url = f"{base_url}{info_suffix}"
        try:
            req = urllib.request.Request(info_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    continue
                info = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except Exception:
            continue

        if not isinstance(info, dict):
            continue
        named = info.get("named_endpoints")
        if not isinstance(named, dict) or not named:
            continue

        def _score_ep(ep_key: str) -> Tuple[int, int]:
            low = ep_key.lower()
            clean = low.lstrip("/")
            if any(b in clean for b in BANNED_ENDPOINT_WORDS):
                return (999, len(ep_key))
            for idx, pref in enumerate(PREFERRED_ENDPOINT_NAMES):
                if low == pref or low.startswith(pref + "_"):
                    return (idx, len(ep_key))
            for idx, pref in enumerate(PREFERRED_ENDPOINT_NAMES):
                if pref.lstrip("/") in clean:
                    return (20 + idx, len(ep_key))
            return (100, len(ep_key))

        sorted_eps = sorted(named.keys(), key=_score_ep)
        for ep_name in sorted_eps:
            score, _ = _score_ep(ep_name)
            if score >= 999:
                continue
            ep_meta = named[ep_name]
            if not isinstance(ep_meta, dict):
                continue
            params = ep_meta.get("parameters")
            if not isinstance(params, list) or not params:
                continue
            returns = ep_meta.get("returns", [])
            if not returns:
                continue

            prompt_idx = -1
            prompt_mode = "str"
            for idx, p in enumerate(params):
                if not isinstance(p, dict):
                    continue
                p_type = p.get("type", {})
                t_str = str(p_type.get("type", "")) if isinstance(p_type, dict) else str(p_type)
                comp = str(p.get("component", "")).lower()
                pname = str(p.get("parameter_name", p.get("label", ""))).lower()

                if "multimodal" in comp or (isinstance(p_type, dict) and " files:" in str(p_type)):
                    prompt_idx = idx
                    prompt_mode = "multimodal_dict"
                    break

                if t_str == "string" or comp in ("textbox", "textarea"):
                    if any(k in pname for k in ("msg", "message", "prompt", "query", "input", "question", "text", "user")):
                        if "system" not in pname and "key" not in pname and "token" not in pname:
                            prompt_idx = idx
                            prompt_mode = "str"
                            break

            if prompt_idx == -1:
                for idx, p in enumerate(params):
                    if not isinstance(p, dict):
                        continue
                    p_type = p.get("type", {})
                    t_str = str(p_type.get("type", "")) if isinstance(p_type, dict) else str(p_type)
                    comp = str(p.get("component", "")).lower()
                    pname = str(p.get("parameter_name", p.get("label", ""))).lower()
                    if (t_str == "string" or comp in ("textbox", "textarea")) and "key" not in pname and "token" not in pname:
                        prompt_idx = idx
                        prompt_mode = "str"
                        break

            if prompt_idx == -1:
                continue

            param_template: List[Any] = []
            for idx, p in enumerate(params):
                if idx == prompt_idx:
                    param_template.append("__PROMPT_PLACEHOLDER__")
                    continue

                has_default = p.get("parameter_has_default", False)
                default_val = p.get("parameter_default")
                p_type = p.get("type", {})
                t_str = str(p_type.get("type", "")) if isinstance(p_type, dict) else str(p_type)
                comp = str(p.get("component", "")).lower()
                pname = str(p.get("parameter_name", p.get("label", ""))).lower()

                p_min = p.get("minimum", p_type.get("minimum") if isinstance(p_type, dict) else None)
                p_max = p.get("maximum", p_type.get("maximum") if isinstance(p_type, dict) else None)

                if comp == "chatbot" or t_str == "array" or "history" in pname:
                    param_template.append([])
                elif "system" in pname and (t_str == "string" or comp in ("textbox", "textarea")):
                    param_template.append(
                        "You are an expert Computer Science tutor, Python engineer, and systems auditor. Always follow the user's formatting and word inclusion rules strictly."
                    )
                elif any(k in pname for k in ("max_new_tokens", "max_tokens", "max_length", "tokens")) and t_str in ("number", "integer"):
                    target = 1024
                    if isinstance(p_max, (int, float)) and p_max > 0:
                        target = min(int(p_max), 1024)
                    elif isinstance(default_val, (int, float)) and default_val > 0:
                        target = max(int(default_val), 512)
                    if isinstance(p_min, (int, float)) and target < p_min:
                        target = int(p_min)
                    param_template.append(target)
                elif "top_k" in pname and t_str in ("number", "integer"):
                    target = 40
                    if isinstance(default_val, (int, float)) and default_val > 0:
                        target = int(default_val)
                    if isinstance(p_max, (int, float)) and target > p_max:
                        target = int(p_max)
                    if isinstance(p_min, (int, float)) and target < p_min:
                        target = int(p_min)
                    param_template.append(target)
                elif "temp" in pname and t_str in ("number", "integer"):
                    param_template.append(0.2 if (p_min is None or p_min <= 0.2) else float(p_min))
                elif has_default and default_val is not None:
                    param_template.append(default_val)
                elif t_str == "boolean" or comp == "checkbox":
                    param_template.append(False)
                elif t_str in ("number", "integer") or comp in ("slider", "number"):
                    if isinstance(default_val, (int, float)):
                        param_template.append(default_val)
                    elif isinstance(p_min, (int, float)):
                        param_template.append(p_min)
                    else:
                        param_template.append(1)
                elif t_str == "string" or comp in ("textbox", "textarea", "dropdown", "radio"):
                    enum_vals = p_type.get("enum") if isinstance(p_type, dict) else None
                    if isinstance(enum_vals, list) and enum_vals:
                        param_template.append(enum_vals[0])
                    else:
                        param_template.append("")
                else:
                    param_template.append(None)

            clean_ep = ep_name.lstrip("/")
            return {
                "space_id": space_id,
                "subdomain": sub,
                "endpoint_url": f"{base_url}{call_prefix}/{clean_ep}",
                "endpoint_name": clean_ep,
                "prompt_idx": prompt_idx,
                "prompt_mode": prompt_mode,
                "param_template": param_template,
                "arity": len(params),
            }
    return None


# ---------------------------------------------------------------------------
# 2. v11 Universal Harness Generator & Semantic Canary
# ---------------------------------------------------------------------------

def build_v11_gradio_harness(portal_id, space_id, spec):
    from tools.test_web_playground_ephemeral_and_cegar import build_cegar_v8_7_harness
    return build_cegar_v8_7_harness({"portal_id":portal_id,"model_id":space_id})


def quick_semantic_canary_v11(spec: Dict[str, Any], timeout: float = 16.0) -> Tuple[bool, str]:
    endpoint_url = spec["endpoint_url"]
    prompt = "Reply with the single word READY and the result of 12 + 30 (42)."
    data_list: List[Any] = []
    for item in spec["param_template"]:
        if item == "__PROMPT_PLACEHOLDER__":
            if spec["prompt_mode"] == "multimodal_dict":
                data_list.append({"text": prompt, "files": []})
            else:
                data_list.append(prompt)
        else:
            data_list.append(item)

    payload = json.dumps({"data": data_list}).encode("utf-8")
    try:
        req = urllib.request.Request(
            endpoint_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            event_id = json.loads(resp.read().decode("utf-8", errors="ignore")).get("event_id")
        if not event_id:
            return False, "no_event_id"

        stream_url = f"{endpoint_url}/{event_id}"
        sreq = urllib.request.Request(stream_url, headers={"User-Agent": "Mozilla/5.0"})
        best = ""
        with urllib.request.urlopen(sreq, timeout=timeout) as sresp:
            curr_ev = ""
            for raw_line in sresp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if line.startswith("event:"):
                    curr_ev = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    if curr_ev == "error":
                        return False, "sse_error"
                    try:
                        parsed = json.loads(line.split(":", 1)[1].strip())
                        cand = str(parsed)
                        if len(cand) > 4:
                            best = cand
                    except Exception:
                        pass
                    if curr_ev == "complete":
                        break
        low = best.lower()
        if not best or len(best.strip()) < 5:
            return False, "empty"
        bad_markers = (
            "api key", "invalid_api_key", "quota exceeded", "rate limit",
            "unauthorized", "401", "403", "insufficient_quota", "billing",
            "error:", "traceback", "connection error", "paused", "sleeping",
        )
        if any(b in low for b in bad_markers) and "42" not in low and "ready" not in low:
            return False, f"error_marker:{best[:80]}"
        if "42" in low or "ready" in low or len(best) > 15:
            return True, best[:120]
        return False, f"no_semantic_match:{best[:80]}"
    except Exception as exc:
        return False, f"exc:{type(exc).__name__}"


# ---------------------------------------------------------------------------
# 2B. Non-Hugging Face Multi-Vector Harnesses (Pollinations, Google Cloud, HF Router)
# ---------------------------------------------------------------------------

NON_HF_MULTI_VECTOR_PORTALS: List[Dict[str, Any]] = [
    # Vector 1: Non-HF Free OpenAI-Compatible Router (Pollinations / OVH)
    {
        "portal_id": "pollinations-openai-fast",
        "name": "Pollinations OpenAI Fast Router (GPT-OSS-20B)",
        "region": "Global",
        "category": "free_openai_router",
        "url": "https://text.pollinations.ai/openai",
        "model_id": "openai-fast",
        "kind": "pollinations_openai",
    },
    # Vector 2: Google Gemini & Gemma Cloud APIs (OS Keyring Authenticated)
    {
        "portal_id": "google-gemini-3-flash-preview-cloud",
        "name": "Google Gemini 3 Flash Preview (Cloud API)",
        "region": "US",
        "category": "authenticated_cloud_api",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent",
        "model_id": "gemini-3-flash-preview",
        "kind": "google_cloud",
    },
    {
        "portal_id": "google-gemini-3-1-flash-lite-preview-cloud",
        "name": "Google Gemini 3.1 Flash-Lite Preview (Cloud API)",
        "region": "US",
        "category": "authenticated_cloud_api",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent",
        "model_id": "gemini-3.1-flash-lite-preview",
        "kind": "google_cloud",
    },
    {
        "portal_id": "google-gemini-3-5-flash-lite-cloud",
        "name": "Google Gemini 3.5 Flash-Lite (Cloud API)",
        "region": "US",
        "category": "authenticated_cloud_api",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
        "model_id": "gemini-3.5-flash-lite",
        "kind": "google_cloud",
    },
    {
        "portal_id": "google-gemini-2-5-flash-cloud",
        "name": "Google Gemini 2.5 Flash (Cloud API)",
        "region": "US",
        "category": "authenticated_cloud_api",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
        "model_id": "gemini-2.5-flash",
        "kind": "google_cloud",
    },
    {
        "portal_id": "google-gemma-4-31b-it-cloud",
        "name": "Google Gemma 4 31B IT (Cloud API)",
        "region": "US",
        "category": "authenticated_cloud_api",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-31b-it:generateContent",
        "model_id": "gemma-4-31b-it",
        "kind": "google_cloud",
    },
    {
        "portal_id": "google-gemma-4-26b-a4b-it-cloud",
        "name": "Google Gemma 4 26B A4B IT (Cloud API)",
        "region": "US",
        "category": "authenticated_cloud_api",
        "url": "https://generativelanguage.googleapis.com/v1beta/models/gemma-4-26b-a4b-it:generateContent",
        "model_id": "gemma-4-26b-a4b-it",
        "kind": "google_cloud",
    },
    # Vector 3: Multi-Provider OpenAI-Compatible Serverless Router (Nebius / Together / Novita / Fireworks)
    {
        "portal_id": "hf-router-qwen3-8-27b",
        "name": "Serverless Router (Qwen/Qwen3.8-27B)",
        "region": "Global",
        "category": "openai_compatible_router",
        "url": "https://router.huggingface.co/v1/chat/completions",
        "model_id": "Qwen/Qwen3.8-27B",
        "kind": "hf_openai_router",
    },
    {
        "portal_id": "hf-router-deepseek-v4-1-flash",
        "name": "Serverless Router (deepseek-ai/DeepSeek-V4.1-Flash)",
        "region": "Global",
        "category": "openai_compatible_router",
        "url": "https://router.huggingface.co/v1/chat/completions",
        "model_id": "deepseek-ai/DeepSeek-V4.1-Flash",
        "kind": "hf_openai_router",
    },
    {
        "portal_id": "hf-router-glm-5-3-flash",
        "name": "Serverless Router (zai-org/GLM-5.3-Flash)",
        "region": "Global",
        "category": "openai_compatible_router",
        "url": "https://router.huggingface.co/v1/chat/completions",
        "model_id": "zai-org/GLM-5.3-Flash",
        "kind": "hf_openai_router",
    },
    {
        "portal_id": "hf-router-kimi-k3",
        "name": "Serverless Router (moonshotai/Kimi-K3)",
        "region": "Global",
        "category": "openai_compatible_router",
        "url": "https://router.huggingface.co/v1/chat/completions",
        "model_id": "moonshotai/Kimi-K3",
        "kind": "hf_openai_router",
    },
    {
        "portal_id": "hf-router-llama-3-1-8b-instruct",
        "name": "Serverless Router (meta-llama/Llama-3.1-8B-Instruct)",
        "region": "Global",
        "category": "openai_compatible_router",
        "url": "https://router.huggingface.co/v1/chat/completions",
        "model_id": "meta-llama/Llama-3.1-8B-Instruct",
        "kind": "hf_openai_router",
    },
]


def build_v11_non_hf_harness(spec):
    from tools.test_web_playground_ephemeral_and_cegar import build_cegar_v8_7_harness
    return build_cegar_v8_7_harness(spec)


# ---------------------------------------------------------------------------
# 3. Swarm Scout Council + Multi-Vector Discovery + CEGAR Repair
# ---------------------------------------------------------------------------

NEW_SWARM_SEARCH_QUERIES = [
    "smolagents", "mcp-agent", "pydantic-ai", "agno", "crewai", "autogen",
    "langgraph", "dify", "flowise", "langflow", "phidata", "camel-ai",
    "metagpt", "openhands", "swe-agent", "aider", "cline", "roo-code",
    "qwen2.5-coder", "qwq", "qwen3-coder", "deepseek-r1", "deepseek-v3",
    "gemma-4", "gemma-3-27b", "llama-3.3-70b", "llama-4", "mistral-small",
    "devstral", "codestral", "phi-4", "nemotron", "command-r", "glm-4",
    "glm-5", "kimi", "minimax", "stepfun", "hunyuan", "doubao", "yi-lightning",
    "open-webui", "chat-ui", "librechat", "lobe-chat", "anythingllm",
    "ai-tutor", "coding-assistant", "code-assistant", "python-assistant",
    "security-agent", "code-review", "gaia-agent", "digital-twin",
    "free-chat", "uncensored-chat", "llm-router", "openrouter", "groq-chat",
    "cerebras", "sambanova", "together-ai", "fireworks-ai", "nebius",
    "novita", "hyperbolic", "chutes", "featherless", "kluster",
]

PRIORITY_REPAIR_SPACE_IDS = [
    "phanukon/Ai_Study_Planner",
    "AST-1320/Gemma-4-Aggressive",
    "samliumay/llm-firewalls-an-introduction",
    "mrdbourke/queensland-ai-fine-tuned-gemma3-test",
    "MuseOS-Labs/museos-nebius-agent",
    "mxderncultxre/Gemma4-31b-uncensored-assistant",
    "Miso-Soop/Gemma4-31b-uncensored-assistant",
    "jickman125/Gemma4-31b-uncensored-assistant",
    "hithamali/gemma4-31b-crack-lab",
    "oravianando/glm-5-3-playground",
    "akhaliq/GLM-5.3-Flash",
    "vml12/qwen-ai-chatbot",
    "Heartsync/DeepSeek32",
    "coldd09/llama-3-1-8b-instruct",
    "ZetronicB/penclaw-glm-53-demo",
    "bbbbeeeezzz/deepseek-v41-flash-1017",
    "sherlock79/deepseek-v4-1-uncensored-fp8-demo",
    "mirzozi/qwen3-8-2-4t-a95b",
    "haeryz/gemma-4-e2b-mlx-demo",
    "raghavendragithub/my-uncensored-ai",
    "vishal9091/uncensored-ai-chat",
    "thyallys/agente-llm",
    "pcam/kira-ai-assistant",
    "dazaigg/qwen25-0-5b-instruct-demo",
    "dvmalyshev/rag-llm-test",
    "siwitz15/strongerassistant",
    "lolmaykolpino/myk-chat",
    "jannatyounas/jannat-ai-clone",
    "maira26/my-digital-clone",
    "codez1234/zahra-digital-clone",
    "dezu2k5/ai-tutor",
    "mohammedqarnuon/qarnonai",
    "nileshc90/apex-code-space",
    "gh0stwolf/android-coder-demo",
    "aiqcamp/mistral-devstral-api",
    "burtenshaw/karpathy-llm-council",
    "akhaliq/nvidia-nemotron-3-ultra-550b-a55b-nvfp4",
    "Agents-MCP-Hackathon/Hermes",
]


def query_hf_spaces_for_keyword(query: str, limit: int = 40) -> List[str]:
    found: List[str] = []
    for sort_field in ("lastModified", "likes"):
        url = (
            f"https://huggingface.co/api/spaces?"
            f"search={urllib.parse.quote(query)}&sdk=gradio&sort={sort_field}&direction=-1&limit={limit}"
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SabrinaSwarmHunter/11.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                items = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if isinstance(items, list):
                    for item in items:
                        sid = item.get("id") if isinstance(item, dict) else None
                        if isinstance(sid, str) and "/" in sid:
                            found.append(sid)
        except Exception:
            pass
    return found


def make_portal_id(space_id: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", space_id.lower()).strip("-")
    return f"hf-gradio-{slug}"[:68].rstrip("-")


def make_module_name(portal_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", portal_id.lower())


def run_swarm_portal_hunter_v11():
    raise RuntimeError("Legacy hunter retired; use tools.portal_cli and documented role qualification.")


if __name__ == "__main__":
    run_swarm_portal_hunter_v11()
