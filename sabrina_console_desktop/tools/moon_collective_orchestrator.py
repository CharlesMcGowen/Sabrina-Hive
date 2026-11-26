#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Multi-Family Collective Orchestrator — 2x ("Mooning") Detection Architecture
====================================================================================
Convenes BOTH:
  Track A: Live-Verified Zero-Auth External Portals (sanctioned via `PortalPolicy`
           + `CyberComplianceSentinel`, zero credentials).
  Track B: 6 Distinct Local Model Families on `ego` (`http://ego.local:11434`):
           - Mistral (`dolphin-mistral:7b`): Divergent Moon-Ignition Scout
           - Google Gemma-3 (`gemma3:12b`): Order-Book Microstructure & Squeeze Analyst
           - Alibaba Qwen Coder (`qwen2.5-coder:14b`): Tool Forge Quant Architect
           - Meta Llama-3.1 (`llama3.1:8b`): Disjoint-Family Adversarial Skeptic
           - IBM Granite-3.1 (`granite3.1-dense:8b`): Causality & Governance Auditor
           - Constitutional Jury Defender (`sabrina-jury-defender:0.2`): Minority Dissent Guardian
           - Alibaba Qwen3 (`qwen3:14b`, `num_predict=2200`): Integrative Collective Editor

Zero Assistant Ghostwriting: saves 100% verbatim model outputs (`response`, `thinking`,
`eval_count`, `latency_sec`, `tok_per_sec`, `portal_id`, `family`) to JSON and builds
an attributed collective architecture report with preserved minority dissent.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import requests

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.cyber_compliance_sentinel import CyberComplianceSentinel
from tools.portal_policy import DEFAULT_DB, PortalPolicy
from tools.portal_transport import request as portal_request

OLLAMA_API = "http://ego.local:11434/api/generate"
OUT_DIR = SABRINA_ROOT / "artifacts" / "moon_collective"
OUT_JSON = OUT_DIR / "moon_collective_raw.json"
OUT_MD = SABRINA_ROOT / "docs" / "MOON_COLLECTIVE_ARCHITECTURE.md"

# Live-verified Agent Arena External Guilds from AGENTS.md (zero credentials)
EXTERNAL_PORTAL_CANDIDATES: List[Dict[str, str]] = [
    {"portal_id": "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "family": "External Guild 1 (Strategist)", "guild": "Guild 1"},
    {"portal_id": "hf-gradio-himakesh-parth-ai-tutor", "family": "External Guild 1 (Strategist)", "guild": "Guild 1"},
    {"portal_id": "hf-gradio-htchu-coderhelper", "family": "External Guild 2 (Auditor)", "guild": "Guild 2"},
    {"portal_id": "hf-gradio-affanaiengr-rag", "family": "External Guild 2 (Auditor)", "guild": "Guild 2"},
    {"portal_id": "hf-gradio-rstanley2026-rodney-digital-twin", "family": "External Guild 4 (Synthesizer)", "guild": "Guild 4"},
    {"portal_id": "hf-gradio-free234-snap-rag-assistant", "family": "External Guild 2 (Auditor)", "guild": "Guild 2"},
]

UPSTREAM_ERROR_MARKERS = (
    "401 unauthorized",
    "404 not found",
    "invalid api key",
    "error code: 401",
    "error code: 404",
    "insufficient_quota",
    "rate_limit_exceeded",
    "bad gateway",
)


def query_ego_ollama(
    model: str,
    family: str,
    guild_role: str,
    prompt: str,
    system: str = "",
    max_tokens: int = 900,
    temperature: float = 0.35,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
        },
    }
    t0 = time.time()
    try:
        resp = requests.post(OLLAMA_API, json=payload, timeout=300)
        dt = time.time() - t0
        resp.raise_for_status()
        data = resp.json()
        eval_count = int(data.get("eval_count", 0) or 0)
        eval_dur = (data.get("eval_duration", 1) or 1) / 1e9
        tok_s = eval_count / eval_dur if eval_dur > 0 else 0.0
        text = (data.get("response") or "").strip()
        thinking = (data.get("thinking") or "").strip()
        print(f"  [Local/{family}] {model} -> {eval_count} tok in {dt:.2f}s ({tok_s:.1f} tok/s, {len(text)} chars)", flush=True)
        return {
            "ok": bool(text),
            "node_type": "local_ego_ollama",
            "model": model,
            "family": family,
            "guild_role": guild_role,
            "response": text,
            "thinking": thinking,
            "eval_count": eval_count,
            "latency_sec": round(dt, 2),
            "tok_per_sec": round(tok_s, 1),
        }
    except Exception as exc:
        print(f"  [Local/{family}] {model} ERROR: {exc}", flush=True)
        return {
            "ok": False,
            "node_type": "local_ego_ollama",
            "model": model,
            "family": family,
            "guild_role": guild_role,
            "response": "",
            "thinking": "",
            "error": str(exc),
            "latency_sec": round(time.time() - t0, 2),
        }


def run_external_zero_auth_track(sentinel: CyberComplianceSentinel, target_successes: int = 3) -> List[Dict[str, Any]]:
    """Query live-verified Zero-Auth External Portals via sanctioned PortalPolicy + Sentinel."""
    policy = PortalPolicy(DEFAULT_DB)
    raw_prompt = (
        "In 5 concise bullet points, what observable public market microstructure and on-chain/news "
        "signals precede a crypto altcoin doubling (+100%) on a spot exchange like Kraken, and what "
        "single trap causes the most false alarms?"
    )
    clean_ok, clean_msg = sentinel.sanitize_outbound_query(raw_prompt)
    if not clean_ok:
        raise RuntimeError(f"Outbound portal prompt blocked by CyberComplianceSentinel: {clean_msg}")

    results: List[Dict[str, Any]] = []
    successes = 0
    last_hf_finish = 0.0
    for item in EXTERNAL_PORTAL_CANDIDATES:
        if successes >= target_successes:
            break
        pid = item["portal_id"]
        prof = policy.profile(pid)
        if not prof or not prof.get("access_allowed"):
            print(f"  [External/{item['guild']}] {pid} skipped (not admitted)", flush=True)
            continue
        adp = json.loads(prof["adapter_json"])
        if adp.get("headers") or adp.get("api_key") or adp.get("token") or adp.get("key_env"):
            print(f"  [External/{item['guild']}] {pid} skipped (non-zero-auth adapter)", flush=True)
            continue
        if prof.get("provider") == "huggingface" and last_hf_finish > 0:
            wait_needed = 10.6 - (time.time() - last_hf_finish)
            if wait_needed > 0:
                time.sleep(wait_needed)

        print(f"  [External/{item['guild']}] Querying {pid}...", flush=True)
        try:
            res = portal_request(policy, pid, raw_prompt, purpose="evaluation", max_tokens=600)
            if prof.get("provider") == "huggingface":
                last_hf_finish = time.time()
            text = (res.get("reply") or "").strip()
            low = text.lower()
            upstream_err = any(m in low for m in UPSTREAM_ERROR_MARKERS)
            if text:
                threat = sentinel.inspect_and_report_threat(pid, text, {"claimed_model": prof.get("model_claim", "")})
                if threat:
                    res["ok"] = False
                    res["failure"] = "quarantined_by_sentinel"
            ok = bool(res.get("ok") and len(text) >= 60 and not upstream_err)
            if ok:
                successes += 1
            dt = round((res.get("latency_ms") or 0) / 1000.0, 2)
            print(f"    [{'+' if ok else '-'}] {pid} -> ok={ok} ({len(text)} chars, {dt}s, failure={res.get('failure', '')})", flush=True)
            results.append({
                "ok": ok,
                "node_type": "external_zero_auth_portal",
                "portal_id": pid,
                "claimed_model": prof.get("model_claim", ""),
                "family": item["family"],
                "guild_role": item["guild"],
                "response": text if ok else "",
                "failure": res.get("failure") or ("upstream_backend_error" if upstream_err else ""),
                "latency_sec": dt,
                "provenance_ref": res.get("provenance_ref", ""),
            })
        except Exception as exc:
            print(f"    [-] {pid} ERROR: {exc}", flush=True)
            results.append({
                "ok": False,
                "node_type": "external_zero_auth_portal",
                "portal_id": pid,
                "family": item["family"],
                "guild_role": item["guild"],
                "response": "",
                "failure": str(exc),
                "latency_sec": 0.0,
            })
    return results


def build_markdown(data: Dict[str, Any]) -> str:
    lines: List[str] = [
        "# Sabrina Multi-Family Collective — 2x ('Mooning') Detection Architecture",
        "",
        f"- **Generated UTC**: `{data['generated_utc']}`",
        f"- **Lean 4 Formal Proof Reference**: `Sabrina/proofs/MoonExpectancy.lean`",
        f"- **Zero Ghostwriting Policy**: Every section below contains 100% verbatim model output with full family, model/portal ID, token count, and latency attribution.",
        "",
        "## 1. Track A — Zero-Auth Public External Portals",
        "",
    ]
    ext_ok = [r for r in data["external_track"] if r.get("ok")]
    if not ext_ok:
        lines.append("_No external portals delivered a clean non-error reply within the polite window; see `moon_collective_raw.json` for diagnostics._")
        lines.append("")
    for r in ext_ok:
        lines.extend([
            f"### {r['portal_id']} ({r['family']})",
            f"- **Latency**: `{r['latency_sec']}s` | **Node**: `{r['node_type']}`",
            "",
            r["response"],
            "",
        ])

    lines.extend([
        "## 2. Track B — Local Multi-Family Hive on `ego` (6 Model Families)",
        "",
    ])
    for r in data["local_track"]:
        status = "OK" if r.get("ok") else f"ERROR: {r.get('error')}"
        lines.extend([
            f"### {r['guild_role']} — `{r['model']}` ({r['family']})",
            f"- **Status**: `{status}` | **Tokens**: `{r.get('eval_count', 0)}` | **Speed**: `{r.get('tok_per_sec', 0)} tok/s` | **Latency**: `{r.get('latency_sec', 0)}s`",
            "",
            r["response"] if r.get("ok") else "_No response._",
            "",
        ])

    syn = data.get("synthesis")
    if syn:
        lines.extend([
            "## 3. Integrative Collective Synthesis & Preserved Minority Dissent",
            f"### `{syn['model']}` ({syn['family']}) — {syn['guild_role']}",
            f"- **Tokens**: `{syn.get('eval_count', 0)}` | **Speed**: `{syn.get('tok_per_sec', 0)} tok/s` | **Latency**: `{syn.get('latency_sec', 0)}s`",
            "",
            syn.get("response", ""),
            "",
        ])
    return "\n".join(lines)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    sentinel = CyberComplianceSentinel()

    context_brief = (
        "CONTEXT: Sabrina is building an asymmetric 2x ('mooning', +100% runner) detection & paper-trading "
        "system for Kraken USD spot pairs. We already fixed the fee trap to Kraken Pro Maker Limit (16 bps/side "
        "= 32 bps round-trip) with a -3.20% widened stop-loss, and formally proved in Lean 4 that even a 4% hit "
        "rate (1 double in 25 tries) beats cash (+60.8 bps expected value), that every feature must obey "
        "published_at <= observed_at <= decision_at (zero lookahead), and that clearing the ask book to 2x "
        "requires net buy USD > cumulative resting ask depth below 2x spot ('Ask-Book Air Pocket'). "
        "Available zero-auth public inputs: Kraken /0/public/Ticker, /0/public/Depth (L2 order book), "
        "/0/public/OHLC, DexScreener pool buy/sell velocity, CoinGecko trending, and Google News RSS."
    )

    print("=== TRACK A: Zero-Auth External Portals ===", flush=True)
    external_results = run_external_zero_auth_track(sentinel, target_successes=3)

    print("\n=== TRACK B: Multi-Family Local Hive on ego ===", flush=True)
    local_specs = [
        {
            "model": "dolphin-mistral:7b",
            "family": "Mistral / Dolphin",
            "guild_role": "Divergent Moon-Ignition Hypothesis Scout",
            "temperature": 0.65,
            "max_tokens": 750,
            "system": "You are a creative quantitative researcher generating unconventional, testable hypotheses for catching early +100% crypto breakouts before the crowd.",
            "prompt": (
                f"{context_brief}\n\n"
                "Propose 3 concrete, testable pre-moon ignition hypotheses combining Kraken ask-book thinness, "
                "DEX-to-CEX lead-lag volume acceleration, and catalyst/news bursts. For each hypothesis specify: "
                "(1) exact observable trigger formula, (2) why it fires BEFORE the +100% move rather than after, "
                "and (3) one concrete falsification test."
            ),
        },
        {
            "model": "gemma3:12b",
            "family": "Google Gemma-3",
            "guild_role": "Order-Book Microstructure & Air-Pocket Analyst",
            "temperature": 0.30,
            "max_tokens": 800,
            "system": "You are a market-microstructure specialist focused on L2 order-book depth, liquidity voids, bid-ask spread compression, and real fillability.",
            "prompt": (
                f"{context_brief}\n\n"
                "Design the exact mathematical formula for Sabrina's 'Ask-Book Air-Pocket Score' using Kraken "
                "/0/public/Depth and /0/public/Ticker. Define: (1) how to compute ask_depth_to_25pct, "
                "ask_depth_to_50pct, and ask_depth_to_100pct relative to hourly USD volume, (2) bid-side exit "
                "safety floor so we never enter a ghost book we can't exit, and (3) how to avoid spoofed walls."
            ),
        },
        {
            "model": "qwen2.5-coder:14b",
            "family": "Alibaba Qwen2.5-Coder",
            "guild_role": "Tool Forge Quant & Uncapped Runner Architect",
            "temperature": 0.25,
            "max_tokens": 900,
            "system": "You are a senior quant systems engineer designing production-clean Python feature pipelines and trailing-stop exit rules for asymmetric runners.",
            "prompt": (
                f"{context_brief}\n\n"
                "Our old `kraken_surge_collective.py` capped take-profit at +20% (`take=0.20`), which cuts off "
                "+100% runners. Specify the exact Python scoring weights (0-100 composite Moon Ignition Score) "
                "and a multi-stage uncapped trailing-stop exit ladder (protecting downside at -3.2%/-8% while "
                "letting +20% -> +50% -> +100%+ runners compound without early caps)."
            ),
        },
    ]

    local_results: List[Dict[str, Any]] = []
    for spec in local_specs:
        local_results.append(query_ego_ollama(**spec))

    worker_digest = "\n\n".join(
        f"[{r['family']} / {r['model']} - {r['guild_role']}]:\n{r['response'][:1400]}"
        for r in local_results if r.get("ok")
    )

    # Disjoint-Family Adversarial Audits (Meta Llama, IBM Granite, Constitutional Jury Defender auditing Mistral/Gemma/Qwen)
    audit_specs = [
        {
            "model": "llama3.1:8b",
            "family": "Meta Llama-3.1",
            "guild_role": "Disjoint-Family Adversarial Skeptic & Trap Hunter",
            "temperature": 0.35,
            "max_tokens": 750,
            "system": "You are a ruthless adversarial trading auditor from an independent model family. Your job is to break the proposed moon detector and expose traps.",
            "prompt": (
                f"{context_brief}\n\nPROPOSALS TO AUDIT:\n{worker_digest}\n\n"
                "Critique these proposals adversarially. Identify the top 4 failure modes (e.g., wash-volume traps, "
                "already-exhausted parabolic tops, DEX honeypot/rug-pull spillover, or thin exit bids) and state "
                "exact hard rejection filters Sabrina must enforce."
            ),
        },
        {
            "model": "granite3.1-dense:8b",
            "family": "IBM Granite-3.1",
            "guild_role": "Causality, Governance & Zero-Lookahead Auditor",
            "temperature": 0.25,
            "max_tokens": 750,
            "system": "You are an independent governance and statistical integrity auditor verifying causal timestamps, backtest hygiene, and anti-overfitting safeguards.",
            "prompt": (
                f"{context_brief}\n\nPROPOSALS TO AUDIT:\n{worker_digest}\n\n"
                "Audit the proposed moon-detection pipeline for statistical validity and backtest integrity: "
                "(1) survivorship bias & quiet-period retention, (2) causal alignment between DEX/RSS timestamps "
                "and Kraken snapshots, and (3) exact validation pass/fail gates before any paper-to-live promotion."
            ),
        },
        {
            "model": "sabrina-jury-defender:0.2",
            "family": "Sabrina Constitutional Jury",
            "guild_role": "Minority Dissent Guardian & Tail-Risk Safeguard",
            "temperature": 0.30,
            "max_tokens": 600,
            "system": "You preserve dissenting perspectives and tail-risk warnings that optimistic consensus drafts often erase.",
            "prompt": (
                f"{context_brief}\n\nPROPOSALS TO REVIEW:\n{worker_digest}\n\n"
                "Write a signed Minority Dissent & Safeguard brief: what uncomfortable reality about hunting "
                "+100% altcoin moons on Kraken must NEVER be erased from Sabrina's operational checklist?"
            ),
        },
    ]

    for spec in audit_specs:
        local_results.append(query_ego_ollama(**spec))

    # Integrative Collective Synthesis by qwen3:14b (num_predict >= 2200 so <think> never starves output!)
    all_digest = "\n\n".join(
        f"[{r['family']} / {r.get('model') or r.get('portal_id')} - {r['guild_role']}]:\n{r['response'][:1100]}"
        for r in (external_results + local_results) if r.get("ok")
    )
    synthesis = query_ego_ollama(
        model="qwen3:14b",
        family="Alibaba Qwen3 (Deep Thinker)",
        guild_role="Integrative Collective Editor (Anti-Erasure Synthesis)",
        temperature=0.30,
        max_tokens=2200,
        system="You are the integrative editor for a multi-family swarm collective. Credit distinct model families by name, synthesize an actionable engineering spec, and preserve all minority dissent explicitly.",
        prompt=(
            f"{context_brief}\n\nCOLLECTIVE INPUTS FROM EXTERNAL PORTALS & 6 LOCAL FAMILIES:\n{all_digest}\n\n"
            "Synthesize a crisp, unified engineering blueprint for Sabrina's `kraken_moon_scanner.py` and "
            "uncapped runner backtest. Include:\n"
            "1. Unified Moon Ignition Score (0-100) with exact weights & hard-rejection gates.\n"
            "2. Uncapped 2x Runner Exit Ladder (16 bps Maker fees).\n"
            "3. Explicit Attribution Table crediting each contributing family/portal.\n"
            "4. Signed Minority Dissent section preserving every critical warning from Llama, Granite, and Jury Defender."
        ),
    )

    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "lean_proof_file": "Sabrina/proofs/MoonExpectancy.lean",
        "external_track": external_results,
        "local_track": local_results,
        "synthesis": synthesis,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    OUT_MD.write_text(build_markdown(payload), encoding="utf-8")
    print(f"\nSaved verbatim collective archive: {OUT_JSON}", flush=True)
    print(f"Saved attributed report: {OUT_MD}", flush=True)


if __name__ == "__main__":
    main()
