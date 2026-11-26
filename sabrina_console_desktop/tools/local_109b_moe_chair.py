#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Local `109B-A17B` Sparse-MoE Heavy Escalation Chair Client (`local_109b_moe_chair.py`)
=============================================================================================
Connects Sabrina's Collective routers (`CollectiveRouter`, `FrontierResearchGuild`, `SwarmChatPortal`,
and `FairLaborMarket`) directly to the warm OpenAI-compatible `Llama-4-Scout-109B-A17B` (`IQ2_M`)
`llama-server` daemon (`sabrina-llama4-scout-109b.service`, port `18089` on `ego`) BEFORE burning
cloud Frontier token budgets.

Hardware Profile Verified Live (`task-3040`):
  - Model: `Llama-4-Scout-17B-16E-Instruct-IQ2_M` (`107.77B` total / `17B` active, `34.55 GiB`)
  - Footprint: `~4.4 GiB` VRAM on `ego` `RTX 5060 Ti` (`-ngl 99 --n-cpu-moe 48 -ctk q8_0 -ctv q8_0 -fa on`)
    + `~30 GiB` in `ego` `128 GiB` RAM (runs concurrently alongside `14B`/`8B` Ollama guilds + live Kraken trader).
  - Throughput: `20.06 tok/s` prompt prefill, `8.40 tok/s` autoregressive generation (`0 ms` model reload when warm).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from tools.cyber_compliance_sentinel import CyberComplianceSentinel

DEFAULT_109B_ENDPOINTS: Tuple[str, ...] = (
    "http://192.168.2.69:18089",
    "http://127.0.0.1:18089",
    "http://ego.local:18089",
)
MODEL_ALIAS = "llama4-scout-109b-a17b"
MODEL_FAMILY = "Meta Llama-4 Scout MoE (109B-A17B)"


def _candidate_base_urls() -> List[str]:
    env_url = os.environ.get("SABRINA_109B_MOE_URL", "").strip().rstrip("/")
    urls: List[str] = []
    if env_url:
        urls.append(env_url)
    for u in DEFAULT_109B_ENDPOINTS:
        if u not in urls:
            urls.append(u)
    return urls


def _split_thinking(text: str, explicit_reasoning: str = "") -> Tuple[str, str]:
    thinking = (explicit_reasoning or "").strip()
    visible = text or ""
    if "<think>" in visible and "</think>" in visible:
        m = re.search(r"<think>(.*?)</think>(.*)", visible, flags=re.S | re.I)
        if m:
            ext = m.group(1).strip()
            thinking = f"{thinking}\n{ext}".strip() if thinking else ext
            visible = m.group(2).strip()
    return visible.strip(), thinking


def check_109b_chair_health(timeout_sec: float = 2.5) -> Dict[str, Any]:
    """Return live health status and active endpoint for `sabrina-llama4-scout-109b.service`."""
    last_err = ""
    for base in _candidate_base_urls():
        try:
            req = urllib.request.Request(f"{base}/health", method="GET")
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            status = str(data.get("status") or "").lower()
            if status == "ok":
                return {
                    "ok": True,
                    "status": "ok",
                    "endpoint": base,
                    "model_id": MODEL_ALIAS,
                    "family": MODEL_FAMILY,
                }
            last_err = f"{base}/health status={status}"
        except Exception as exc:
            last_err = f"{base}: {exc}"
    return {
        "ok": False,
        "status": "unreachable",
        "endpoint": "",
        "model_id": MODEL_ALIAS,
        "family": MODEL_FAMILY,
        "error": last_err,
    }


def call_local_109b_moe_chair(
    user_prompt: str,
    *,
    system_prompt: str = "",
    history_turns: Optional[List[Dict[str, str]]] = None,
    max_tokens: int = 1200,
    temperature: float = 0.2,
    timeout_sec: float = 240.0,
    response_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Invoke the warm `Llama-4-Scout-109B-A17B` Sparse-MoE server via OpenAI `/v1/chat/completions`."""
    sentinel = CyberComplianceSentinel()
    ok_q, reason = sentinel.sanitize_outbound_query((system_prompt + "\n" + user_prompt)[:4000])
    if not ok_q:
        return {
            "ok": False,
            "model_id": MODEL_ALIAS,
            "family": MODEL_FAMILY,
            "reply": "",
            "thinking": "",
            "error": f"CyberComplianceSentinel blocked outbound query: {reason}",
        }

    health = check_109b_chair_health(timeout_sec=2.5)
    if not health.get("ok"):
        return {
            "ok": False,
            "model_id": MODEL_ALIAS,
            "family": MODEL_FAMILY,
            "reply": "",
            "thinking": "",
            "error": f"109B Chair offline: {health.get('error', 'unreachable')}",
        }

    base = health["endpoint"]
    messages: List[Dict[str, str]] = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    for turn in (history_turns or [])[-6:]:
        r = str(turn.get("role") or "user")
        c = str(turn.get("content") or "")
        if c:
            messages.append({"role": r, "content": c})
    messages.append({"role": "user", "content": user_prompt})

    payload = {
        "model": MODEL_ALIAS,
        "messages": messages,
        "max_tokens": max(32, min(4096, int(max_tokens))),
        "temperature": float(temperature),
        "stream": False,
    }
    if response_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "structured_reply", "strict": True, "schema": response_schema},
        }

    t0 = time.perf_counter()
    try:
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        dt_s = round(time.perf_counter() - t0, 2)

        choice = (raw.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        raw_content = str(msg.get("content") or "")
        raw_reasoning = str(msg.get("reasoning_content") or "")
        visible, thinking = _split_thinking(raw_content, raw_reasoning)
        if visible and sentinel.inspect_and_report_threat(f"ego:{MODEL_ALIAS}", visible[:4000]):
            return {
                "ok": False,
                "model_id": MODEL_ALIAS,
                "family": MODEL_FAMILY,
                "reply": "",
                "thinking": "",
                "error": "CyberComplianceSentinel quarantined response",
            }

        usage = raw.get("usage") or {}
        timings = raw.get("timings") or {}
        comp_tokens = int(usage.get("completion_tokens") or timings.get("predicted_n") or max(1, len(visible) // 4))
        prompt_tokens = int(usage.get("prompt_tokens") or timings.get("prompt_n") or max(1, len(user_prompt) // 4))
        tok_s = round(float(timings.get("predicted_per_second") or (comp_tokens / max(dt_s, 0.01))), 2)

        return {
            "ok": bool(visible or thinking),
            "endpoint": base,
            "model_id": MODEL_ALIAS,
            "family": MODEL_FAMILY,
            "source": "local_ego_109b_moe_chair",
            "reply": visible,
            "thinking": thinking,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": comp_tokens,
            "total_tokens": prompt_tokens + comp_tokens,
            "tok_per_sec": tok_s,
            "latency_sec": dt_s,
            "error": "",
        }
    except Exception as exc:
        dt_s = round(time.perf_counter() - t0, 2)
        return {
            "ok": False,
            "endpoint": base,
            "model_id": MODEL_ALIAS,
            "family": MODEL_FAMILY,
            "source": "local_ego_109b_moe_chair",
            "reply": "",
            "thinking": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tok_per_sec": 0.0,
            "latency_sec": dt_s,
            "error": str(exc),
        }
