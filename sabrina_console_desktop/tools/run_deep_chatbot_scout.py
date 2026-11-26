#!/usr/bin/env python3
"""Autonomous Deep Recursive Chatbot Scout & Tree Builder.

Continuously discovers and recruits chat models/leaves:
1. Root seeds anchor chat leaves across diverse families (DeepSeek, Qwen, Llama, Mistral, Gemma, Cohere).
2. Recursively prompts recruited chat leaves to nominate new chatbots, open endpoints, and web chat services.
3. Ingests live trending chat models from HuggingFace and OpenRouter indexes.
4. Executes live HTTP/TLS handshakes and validates each chatbot endpoint.
5. Saves a live, streaming recruitment tree to disk with progress timestamps.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OUT_JSON = REPO_ROOT / "artifacts" / "collective-repair-20261108" / "deep_scouted_chatbots.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
MAX_WORKERS = 6


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def query_leaf_for_chatbots(parent_id: str, model_runner: str) -> List[Dict[str, Any]]:
    """Prompt an active leaf model to recommend 5 distinct chatbot services/endpoints."""
    prompt = (
        f"You are chat node '{parent_id}' in an autonomous AI collective. "
        "Recommend 5 real, active AI chat models or web chatbot portals. "
        "Include models from different open-source and frontier families "
        "(e.g., DeepSeek, Qwen, Llama, Mistral, Gemma, Claude, Command-R, Kimi, MiniMax, Yi). "
        "Return ONLY a valid JSON list of objects: "
        '[{"name": "...", "provider": "...", "chat_url": "https://...", "category": "frontier|open_weights|research|coding"}].'
    )
    payload = json.dumps({
        "model": model_runner,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.4, "num_predict": 400},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        txt = data.get("response", "").strip()
        match = re.search(r"\[.*\]", txt, re.DOTALL)
        if match:
            items = json.loads(match.group(0))
            if isinstance(items, list):
                for it in items:
                    it["discovered_by"] = parent_id
                return items
    except Exception:
        pass
    return []


def test_endpoint_reachability(url: str, timeout: float = 4.0) -> Dict[str, Any]:
    """Test live network handshake from ego@ego.local."""
    t0 = time.monotonic()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = round((time.monotonic() - t0) * 1000.0, 2)
            return {"reachable": True, "status": resp.getcode(), "latency_ms": elapsed}
    except urllib.error.HTTPError as exc:
        elapsed = round((time.monotonic() - t0) * 1000.0, 2)
        return {"reachable": True, "status": exc.code, "latency_ms": elapsed}
    except Exception as exc:
        elapsed = round((time.monotonic() - t0) * 1000.0, 2)
        return {"reachable": False, "status": 0, "latency_ms": elapsed, "error": str(exc)}


def run_continuous_scout(target_count: int = 50) -> None:
    start_time = time.time()
    discovered_chatbots: Dict[str, Dict[str, Any]] = {}
    growth_log: List[Dict[str, Any]] = []

    def _save_state():
        payload = {
            "campaign": "deep_recursive_chatbot_scout",
            "started_at": _now_iso(),
            "elapsed_seconds": round(time.time() - start_time, 2),
            "total_chatbots_recruited": len(discovered_chatbots),
            "growth_curve": growth_log,
            "chatbots": list(discovered_chatbots.values()),
        }
        OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _add_bot(bot_id: str, name: str, provider: str, url: str, parent: str, category: str):
        if bot_id in discovered_chatbots:
            return
        t = round(time.time() - start_time, 2)
        item = {
            "id": bot_id,
            "name": name,
            "provider": provider,
            "url": url,
            "parent_referrer": parent,
            "category": category,
            "discovered_at_seconds": t,
            "timestamp": _now_iso(),
        }
        discovered_chatbots[bot_id] = item
        growth_log.append({
            "elapsed_seconds": t,
            "count": len(discovered_chatbots),
            "added_bot": bot_id,
            "name": name,
            "parent": parent,
        })
        print(f"[{t:6.2f}s] N={len(discovered_chatbots):2d} | Chatbot Recruited: {name} ({provider}) via {parent}", flush=True)
        _save_state()

    print("[*] Launching Deep Chatbot Scout...", flush=True)

    # 1. Foundation Seed Chatbots
    seeds = [
        ("bot:deepseek-chat", "DeepSeek Chat (V3/R1)", "DeepSeek", "https://chat.deepseek.com", "ROOT", "frontier_reasoning", "qwen2.5-coder:14b"),
        ("bot:qwen-chat", "Qwen Chat / Tongyi", "Alibaba Qwen", "https://chat.qwen.ai", "ROOT", "frontier_polyglot", "qwen2.5-coder:14b"),
        ("bot:duckai-chat", "DuckAI Anonymous Chat", "DuckDuckGo", "https://duck.ai", "ROOT", "anonymous_aggregator", "qwen3:14b"),
        ("bot:huggingchat", "HuggingChat Open Models", "HuggingFace", "https://huggingface.co/chat", "ROOT", "open_weights", "llama3.1:8b"),
        ("bot:mistral-lechat", "Mistral Le Chat", "Mistral AI", "https://chat.mistral.ai", "ROOT", "european_frontier", "dolphin-mistral:7b"),
        ("bot:lmarena", "LMSYS Chatbot Arena", "LMSYS", "https://lmarena.ai", "ROOT", "evaluation_arena", "qwen2.5:7b"),
        ("bot:kimi-chat", "Kimi Moonshot Chat", "Moonshot AI", "https://kimi.moonshot.cn", "ROOT", "long_context", "qwen2.5-coder:14b"),
        ("bot:chatglm-qingyan", "ChatGLM / Qingyan", "Zhipu AI", "https://chatglm.cn", "ROOT", "bilingual_agentic", "qwen2.5-coder:14b"),
        ("bot:doubao-chat", "Doubao Chat", "ByteDance", "https://www.doubao.com/chat", "ROOT", "fast_drafting", "granite-code:8b"),
        ("bot:yi-chat", "Yi Chat / Wanzhi", "01.AI", "https://www.wanzhi.com", "ROOT", "reasoning_synthesis", "qwen2.5:7b"),
    ]

    for bot_id, name, prov, url, parent, cat, _ in seeds:
        _add_bot(bot_id, name, prov, url, parent, cat)

    # 2. Recursive Wave 1: Ask Seeds to Find More
    print("\n[*] Wave 1: Asking Seeds to recruit new chatbots...", flush=True)
    wave1_parents = seeds[:6]
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {
            ex.submit(query_leaf_for_chatbots, s[0], s[6]): s
            for s in wave1_parents
        }
        for fut in concurrent.futures.as_completed(futs):
            parent_info = futs[fut]
            recs = fut.result()
            print(f"[+] Seed {parent_info[1]} recommended {len(recs)} chatbots.", flush=True)
            for r in recs:
                name = r.get("name") or "Unknown"
                prov = r.get("provider") or "Open AI Ecosystem"
                url = r.get("chat_url") or "https://huggingface.co/chat"
                clean_id = f"bot:{name.lower().replace(' ', '-').replace('/', '-')[:30]}"
                _add_bot(clean_id, name, prov, url, parent_info[0], r.get("category", "chat_model"))

    # 3. Wave 2: Open Model Registry Crawl (HuggingFace + OpenRouter Free Chat Models)
    print("\n[*] Wave 2: Scouting live open chat models from global indexes...", flush=True)
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            models = data.get("data", [])
            for m in models:
                m_id = m.get("id", "")
                if ":free" in m_id or "chat" in m_id.lower() or "instruct" in m_id.lower():
                    name = m.get("name", m_id)
                    bid = f"bot:openrouter:{m_id.replace('/', ':')}"
                    _add_bot(bid, name, m_id.split("/")[0] if "/" in m_id else "OpenRouter", f"https://openrouter.ai/{m_id}", "bot:openrouter-registry", "cloud_endpoint")
                    if len(discovered_chatbots) >= target_count:
                        break
    except Exception as exc:
        print(f"[-] Registry crawl note: {exc}", flush=True)

    # 4. Wave 3: Reachability Verification
    print(f"\n[*] Wave 3: Verifying network reachability of all {len(discovered_chatbots)} recruited chatbots...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {
            ex.submit(test_endpoint_reachability, b["url"]): b["id"]
            for b in discovered_chatbots.values()
            if b.get("url") and b["url"].startswith("http")
        }
        for fut in concurrent.futures.as_completed(futs):
            bid = futs[fut]
            res = fut.result()
            discovered_chatbots[bid]["reachability"] = res

    _save_state()
    total_time = round(time.time() - start_time, 2)
    print(f"\n[DONE] Successfully scouted and recruited {len(discovered_chatbots)} chatbots in {total_time}s!", flush=True)


if __name__ == "__main__":
    run_continuous_scout(target_count=50)
