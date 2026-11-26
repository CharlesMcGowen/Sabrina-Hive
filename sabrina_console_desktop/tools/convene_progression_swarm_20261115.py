#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Multi-Family Swarm Collective — Empirical Progression & Benchmark Audit (2026-11-15)
============================================================================================
Convenes:
  1. External Zero-Auth Public Portal Guilds (strictly from Windows workspace via sanctioned
     `PortalPolicy` + `portal_transport` + `CyberComplianceSentinel`, zero credentials).
  2. Multi-Family Local Fleet on `ego` (`http://ego.local:11434`):
     - `qwen3:14b` (Deep <think> Strategist & Integrative Editor, num_predict=2200)
     - `qwen2.5-coder:14b` (Security Engineering & Container Execution Architect)
     - `llama3.1:8b` (Meta Llama Anti-Monoculture & Benchmark Design Critic)
     - `granite3.1-dense:8b` (IBM Granite Governance & Calibration Specialist)
     - `dolphin-mistral:7b` (Mistral Divergent Hypothesis & Failure-Mode Prober)
     - `sabrina-jury-defender:0.2` (Constitutional Minority Dissent Preservation)
     - `sabrina-jury-granite:0.2` (Disjoint-Family Attribution & Anti-Erasure Auditor;
       skips `sabrina-jury-prosecutor:0.2` per AGENTS.md hardware/health guardrail)

Zero assistant ghostwriting: 100% verbatim model outputs saved to JSON with full provenance.
"""

from __future__ import annotations

import argparse
import json
import socket
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
from tools.portal_transport import classify as classify_portal_reply, invoke_portal, request as portal_request

OUT_JSON = SABRINA_ROOT / "data" / "swarm_progression_synthesis_20261115.json"

# Live-Verified Agent Arena External Guilds + Diverse Zero-Auth Open Portals (AGENTS.md §1.B)
EXTERNAL_GUILD_CANDIDATES: List[Dict[str, str]] = [
    {"portal_id": "pollinations-openai-fast", "guild": "External Strategist", "family": "Pollinations / OpenAI-Fast"},
    {"portal_id": "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "guild": "Guild 1 (Strategist)", "family": "Agentic-RAG Guild 1"},
    {"portal_id": "hf-gradio-htchu-coderhelper", "guild": "Guild 2 (Auditor)", "family": "CoderHelper Guild 2"},
    {"portal_id": "hf-gradio-rstanley2026-rodney-digital-twin", "guild": "Guild 4 (Synthesizer)", "family": "Digital-Twin Guild 4"},
    {"portal_id": "hf-gradio-ast-1320-gemma-4-aggressive", "guild": "External Cross-Family", "family": "Google Gemma-4"},
    {"portal_id": "hf-gradio-aiqcamp-mistral-devstral-api", "guild": "External Cross-Family", "family": "Mistral Devstral"},
    {"portal_id": "hf-gradio-coldd09-llama-3-1-8b-instruct", "guild": "External Cross-Family", "family": "Meta Llama-3.1"},
    {"portal_id": "hf-gradio-himakesh-parth-ai-tutor", "guild": "Guild 1 (Strategist)", "family": "AI-Tutor Guild 1"},
    {"portal_id": "hf-gradio-affanaiengr-rag", "guild": "Guild 2 (Auditor)", "family": "RAG Auditor Guild 2"},
    {"portal_id": "hf-gradio-parmeshlata-portfolio-chatbot", "guild": "Guild 4 (Synthesizer)", "family": "Synthesizer Guild 4"},
]


def resolve_ego_ollama_url() -> str:
    """Resolve ego's Ollama endpoint via dedicated SSH tunnel (127.0.0.1:21434) or LAN IP."""
    for candidate in ("127.0.0.1:21434", "192.168.2.69:11434", "ego.local:11434", "192.168.1.133:11434"):
        try:
            url = f"http://{candidate}/api/tags"
            r = requests.get(url, timeout=3)
            if r.status_code == 200:
                names = {m.get("name") for m in r.json().get("models", [])}
                if "qwen3:14b" in names:
                    return f"http://{candidate}/api/generate"
        except Exception:
            continue
    return "http://127.0.0.1:21434/api/generate"


def build_empirical_briefing() -> str:
    """Build a strictly defensive, leak-audited quantitative briefing of all verified telemetry."""
    blt_path = SABRINA_ROOT / "data" / "honest_blt_gpu_adapter_20261115.json"
    arvo_14b_path = SABRINA_ROOT / "data" / "exploitgym_arvo_14b_dev_20261115.json"

    blt_summary = (
        "- Honest GPU Causal BLT + DARE-TIES Merge (4-way disjoint split, perturbation causality verified <2e-6):\n"
        "  * Prior session's reported BPB=0.0162 / PPL=1.0386 / 49x speedup was exposed as train-on-eval contamination + "
        "non-causal sequence pooling + cached-backbone timing, plus a missing muP logits_scaling=16.0 divisor on Granite-3.1.\n"
        "  * After fixing all four defects: Qwen2.5-Coder-1.5B held-out BPB improved 1.3562 -> 1.3179 (+2.82%, PPL 23.15 -> 21.18, "
        "byte-head BPB 8.07 -> 5.94 [-26.4%]); Granite-3.1-2B held-out BPB improved 1.4211 -> 1.3866 (+2.43%, PPL 16.04 -> 15.00, "
        "byte-head BPB 8.61 -> 5.80 [-32.7%]); honest end-to-end throughput is 0.77x-0.79x vs BPE-only (standalone patch head 2.48x-4.03x at 3.96B/patch)."
    )
    if blt_path.exists():
        try:
            d = json.loads(blt_path.read_text(encoding="utf-8"))
            reps = d.get("reports", [])
            if len(reps) >= 2:
                q, g = reps[0], reps[1]
                blt_summary = (
                    f"- Honest GPU Causal BLT + DARE Merge (causality_verified=True, train_eval_disjoint=True):\n"
                    f"  * {q['model']}: BPE BPB {q['baseline_standard_bpe']['bpb']} -> Merged {q['blt_wsp_merged_checkpoint']['hybrid_backbone_bpb']} "
                    f"({q['improvements']['backbone_bpb_improvement_pct']}% improvement), PPL {q['baseline_standard_bpe']['token_perplexity']} -> "
                    f"{q['blt_wsp_merged_checkpoint']['hybrid_token_perplexity']}, Byte-Head BPB {q['blt_adapter_untrained_init']['blt_byte_bpb']} -> "
                    f"{q['blt_wsp_merged_checkpoint']['blt_byte_bpb']} (-{q['improvements']['blt_byte_bpb_reduction_pct']}%), E2E speed {q['improvements']['end_to_end_throughput_ratio_x']}x.\n"
                    f"  * {g['model']} (after muP logits_scaling=16 fix): BPE BPB {g['baseline_standard_bpe']['bpb']} -> Merged {g['blt_wsp_merged_checkpoint']['hybrid_backbone_bpb']} "
                    f"({g['improvements']['backbone_bpb_improvement_pct']}% improvement), PPL {g['baseline_standard_bpe']['token_perplexity']} -> "
                    f"{g['blt_wsp_merged_checkpoint']['hybrid_token_perplexity']}, Byte-Head BPB {g['blt_adapter_untrained_init']['blt_byte_bpb']} -> "
                    f"{g['blt_wsp_merged_checkpoint']['blt_byte_bpb']} (-{g['improvements']['blt_byte_bpb_reduction_pct']}%), E2E speed {g['improvements']['end_to_end_throughput_ratio_x']}x."
                )
        except Exception:
            pass

    arvo_summary = (
        "- Official Userspace ExploitGym (`cybergym/arvo_*`, isolated 14TB Docker runtime `ready` 8/8 checks):\n"
        "  * `mri-qwen3b-base-control` progressed across transport v2 -> v3 -> v4 -> v5: protocol errors fell from 35/38 (v2) to 0/48 (v5), "
        "and real container shell calls rose from 0 (v2-v4) to 48 turns on `arvo_18224` and 22 turns on `arvo_1699`, though 3B scored 0/2 flags due to 512-token shell quoting truncation."
    )
    if arvo_14b_path.exists():
        try:
            a14 = json.loads(arvo_14b_path.read_text(encoding="utf-8"))
            dev = a14.get("development", {})
            att_lines = []
            for att in dev.get("attempts", []):
                tm = att.get("trace_metrics") or {}
                scores = [c.get("score") for c in att.get("checks", [])]
                att_lines.append(
                    f"{att.get('task_id')}: check_scores={scores}, turns={tm.get('turns')}, "
                    f"protocol_errors={tm.get('protocol_errors')}, terminal_calls={tm.get('terminal_calls')}, "
                    f"recoveries={tm.get('nonzero_then_zero_exit_pairs')}, stage={tm.get('highest_stage')}, finish={att.get('finish')}"
                )
            arvo_summary += (
                "\n  * Live 14B `chickenpwny-14b-ablated` Dev Run (`v5`, assessment split kept unopened): "
                + " | ".join(att_lines)
                + "\n  * Root-cause trace discovery on 14B `v5`: `chickenpwny-14b-ablated` closed Pass-2 tool calls with `</tools>` "
                "instead of `</tool_call>`; because `STOP_STRINGS` and `parse_committed_argument` in `exploitgym_evaluator.py` only "
                "split on `</tool_call>`, generation continued past `\"}}` for 512 tokens (~30s/turn in NF4) and leaked `\"}}\\n</tools>` into bash."
            )
        except Exception:
            pass

    return (
        "EMPIRICAL PROGRESSION BRIEFING (Sabrina Local Lab Audit, 2026-11-15):\n"
        "1. Benchmark Diagnostics (Single-Shot Web App vs. White-Box vs. Verifiable Security Engineering):\n"
        "   - Single-shot Black-Box web challenges: 1/5 to 2/5 across all 4 local models (models failed login SQLi only due to blind parameter name guessing: username vs email).\n"
        "   - White-Box Code Review ('Find It' vulnerable line localization): qwen3:14b 4/5 (80%), qwen2.5-coder:14b 3/5 (60%), llama3.1:8b 3/5 (60%), granite3.1-dense:8b 3/5 (60%).\n"
        "   - Verifiable Path-Boundary Patching (6 hidden AST unit checks): qwen2.5-coder:14b 6/6 (100%), qwen3:14b 5/6 (83%), llama3.1:8b 5/6 (83%), granite3.1-dense:8b 0/4.\n"
        "   - 7-Quest SAST/DAST Reasoning Curriculum: llama3.1:8b mean 0.786 (3/7), qwen2.5-coder:14b mean 0.738 (3/7), granite3.1-dense:8b mean 0.703 (1/7), qwen3:14b mean 0.619 (2/7).\n"
        f"2. {arvo_summary}\n"
        f"3. {blt_summary}\n"
    )


def load_or_init_state() -> Dict[str, Any]:
    if OUT_JSON.exists():
        try:
            return json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "zero_assistant_ghostwriting": True,
        "external_zero_auth_portals": [],
        "local_multi_family_guilds": [],
    }


def save_state(state: Dict[str, Any]) -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def run_external_portals(target_successes: int = 5) -> List[Dict[str, Any]]:
    policy = PortalPolicy(DEFAULT_DB)
    sentinel = CyberComplianceSentinel()
    briefing = build_empirical_briefing()

    prompt = (
        "You are an independent external reviewer in a multi-family AI research collective. "
        "Review the empirical benchmark audit below and answer concisely (180-300 words):\n"
        "1. What does the gap between single-shot Black-Box (20-40%), White-Box code localization (60-80%), "
        "and container execution (0 protocol errors in v5, shell-quoting bottlenecks at 512 tokens) imply for "
        "evaluating and improving small/medium open-weight security engineering models?\n"
        "2. Why is strict causal/split auditing (catching the BLT train-on-eval leak and muP logits_scaling bug) "
        "essential before promoting merged checkpoints?\n\n"
        + briefing
    )

    clean_ok, clean_msg = sentinel.sanitize_outbound_query(prompt)
    if not clean_ok:
        raise RuntimeError(f"CyberComplianceSentinel blocked outbound query: {clean_msg}")

    delivered: List[Dict[str, Any]] = []
    last_hf_ts = 0.0

    for cand in EXTERNAL_GUILD_CANDIDATES:
        if len(delivered) >= target_successes:
            break
        pid = cand["portal_id"]
        prof = policy.profile(pid)
        if not prof or not prof.get("access_allowed"):
            continue
        adp = json.loads(prof["adapter_json"])
        if adp.get("headers") or adp.get("api_key") or adp.get("token") or adp.get("key_env"):
            continue

        if prof["provider"] == "huggingface" and last_hf_ts > 0:
            sleep_s = 10.8 - (time.time() - last_hf_ts)
            if sleep_s > 0:
                time.sleep(sleep_s)

        print(f"[External/{cand['guild']}] Querying {pid}...", flush=True)
        # Prefer invoke_portal when role-qualified, else neutral policy-admitted evaluation request
        res = invoke_portal(pid, prompt, required_capability="critique", supervised=True)
        if not res.get("ok") and "not_qualified" in str(res.get("failure", "")):
            res = portal_request(policy, pid, prompt, purpose="evaluation", max_tokens=650)

        if prof["provider"] == "huggingface":
            last_hf_ts = time.time()

        reply_text = (res.get("reply") or "").strip()
        threat = sentinel.inspect_and_report_threat(pid, reply_text) if reply_text else None
        cls_err = classify_portal_reply(reply_text, res.get("http_status")) if reply_text else "empty"

        # Filter out off-topic digital-twin persona refusals or upstream Space backend errors
        low_r = reply_text.lower()
        upstream_err = any(
            bad in low_r
            for bad in ("401 unauthorized", "404 not found", "exceeded your gpu quota", "api key is required")
        )
        ok_substantive = bool(res.get("ok")) and len(reply_text) >= 120 and not threat and not cls_err and not upstream_err

        record = {
            "portal_id": pid,
            "guild": cand["guild"],
            "family": cand["family"],
            "provider": prof["provider"],
            "claimed_model": res.get("claimed_model", prof.get("model_claim")),
            "actual_model": res.get("actual_model", prof.get("verified_model") or "unknown"),
            "ok": ok_substantive,
            "failure": res.get("failure") or cls_err or ("upstream_error" if upstream_err else ""),
            "threat_detected": bool(threat),
            "latency_sec": round(float(res.get("latency_ms", 0.0)) / 1000.0, 2),
            "request_id": res.get("request_id", ""),
            "provenance_ref": res.get("provenance_ref", ""),
            "response": reply_text,
        }
        print(
            f"  -> ok={ok_substantive} chars={len(reply_text)} latency={record['latency_sec']}s failure={record['failure']!r}",
            flush=True,
        )
        if ok_substantive:
            delivered.append(record)

    state = load_or_init_state()
    state["external_zero_auth_portals"] = delivered
    state["briefing_snapshot_external"] = briefing
    save_state(state)
    return delivered


def query_local_ollama(
    ollama_url: str,
    model: str,
    family: str,
    guild_role: str,
    system_prompt: str,
    user_prompt: str,
    num_predict: int = 900,
    temperature: float = 0.3,
) -> Dict[str, Any]:
    payload = {
        "model": model,
        "system": system_prompt,
        "prompt": user_prompt,
        "stream": False,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    t0 = time.time()
    try:
        r = requests.post(ollama_url, json=payload, timeout=300)
        dt = time.time() - t0
        r.raise_for_status()
        data = r.json()
        eval_count = int(data.get("eval_count") or 0)
        eval_dur = max(1e-6, float(data.get("eval_duration") or 1) / 1e9)
        tok_s = eval_count / eval_dur if eval_count else 0.0
        resp_txt = (data.get("response") or "").strip()
        think_txt = (data.get("thinking") or "").strip()
        print(
            f"[Local/{family}] {model} -> {eval_count} tok in {dt:.2f}s ({tok_s:.1f} tok/s, resp={len(resp_txt)} chars, think={len(think_txt)} chars)",
            flush=True,
        )
        return {
            "ok": bool(resp_txt),
            "node_type": "local_ego_ollama",
            "model": model,
            "family": family,
            "guild_role": guild_role,
            "response": resp_txt,
            "thinking": think_txt,
            "eval_count": eval_count,
            "latency_sec": round(dt, 2),
            "tok_per_sec": round(tok_s, 1),
        }
    except Exception as exc:
        dt = time.time() - t0
        print(f"[Local/{family}] {model} ERROR: {exc}", flush=True)
        return {
            "ok": False,
            "node_type": "local_ego_ollama",
            "model": model,
            "family": family,
            "guild_role": guild_role,
            "response": "",
            "thinking": "",
            "eval_count": 0,
            "latency_sec": round(dt, 2),
            "error": str(exc),
        }


def run_local_guilds() -> List[Dict[str, Any]]:
    ollama_url = resolve_ego_ollama_url()
    print(f"Using ego Ollama endpoint: {ollama_url}", flush=True)
    state = load_or_init_state()
    briefing = build_empirical_briefing()

    ext_summaries = "\n\n".join(
        f"[{e['portal_id']} | {e['guild']}]:\n{e['response'][:700]}"
        for e in state.get("external_zero_auth_portals", [])
    )

    guild_roster = [
        {
            "model": "llama3.1:8b",
            "family": "Meta Llama",
            "guild_role": "Anti-Monoculture Benchmark & Multi-Step Architecture Critic",
            "num_predict": 850,
            "temperature": 0.35,
            "system": "You are the Meta Llama Guild Critic in the Sabrina Swarm Collective. Provide sharp, independent architectural critique without rubber-stamping.",
            "prompt": (
                f"{briefing}\n\nAnalyze why `llama3.1:8b` led the 7-Quest SAST/DAST reasoning score (0.786) and tied for top White-Box HTTP solves (40%), "
                "yet all single-shot Black-Box runs stalled on blind schema/filename discovery, and explain the highest-leverage next engineering interventions "
                "for both multi-turn web probing and `cybergym/arvo_*` C/C++ container exploitation."
            ),
        },
        {
            "model": "granite3.1-dense:8b",
            "family": "IBM Granite",
            "guild_role": "Governance, Calibration & Scientific Integrity Specialist",
            "num_predict": 850,
            "temperature": 0.25,
            "system": "You are the IBM Granite Governance & Calibration Specialist in the Sabrina Swarm Collective.",
            "prompt": (
                f"{briefing}\n\nFocus on scientific governance and calibration: (1) Why catching the 3 BLT leaks + Granite muP `logits_scaling=16.0` divisor "
                "changed the conclusion from an artificial 99% BPB collapse to an honest +2.43% to +2.82% held-out gain, and (2) why Granite's AST failure on "
                "Path-Boundary contrasts with its 60% White-Box Find-It score, and what strict pre-promotion gates should govern WSP/BLT checkpoints."
            ),
        },
        {
            "model": "dolphin-mistral:7b",
            "family": "Mistral",
            "guild_role": "Divergent Hypothesis & Failure-Mode Prober",
            "num_predict": 800,
            "temperature": 0.55,
            "system": "You are the Mistral Divergent Hypothesis Generator in the Sabrina Swarm Collective. Surface unconventional hypotheses and blind spots.",
            "prompt": (
                f"{briefing}\n\nPropose 3 concrete, non-obvious hypotheses for unlocking real binary PoC generation in `cybergym/arvo_*` "
                "(where transport v5 reached 0 protocol errors and 48 container calls, but 512-token shell quoting and `/workspace/poc` directory-vs-file "
                "collisions blocked flag capture) and for pairing byte-level BLT representations with binary/hex exploit synthesis."
            ),
        },
        {
            "model": "sabrina-jury-defender:0.2",
            "family": "Sabrina Constitutional Jury (Defender)",
            "guild_role": "Minority Dissent & Small-Model Specialist Preservation Advocate",
            "num_predict": 750,
            "temperature": 0.35,
            "system": "You are the Constitutional Jury Defender. Protect minority technical insights, specialist 1.5B/3B checkpoints, and dissenting evidence from erasure.",
            "prompt": (
                f"{briefing}\n\nWrite a signed Constitutional Defense Opinion explaining why 1.5B-3B specialist checkpoints (`Qwen2.5-Coder-1.5B` at 87.5% autonomous "
                "4-step state chains, `mri-cybersec-donor-3b` at 100% multi-step state campaigns) and negative/null results must not be overshadowed by 14B averages."
            ),
        },
        {
            "model": "qwen2.5-coder:14b",
            "family": "Alibaba Qwen2.5-Coder",
            "guild_role": "Security Engineering & Container Tooling Lead",
            "num_predict": 950,
            "temperature": 0.25,
            "system": "You are the Qwen2.5-Coder-14B Security Engineering Lead (`6/6` Path-Boundary checks). Focus on concrete code/runtime mechanisms.",
            "prompt": (
                f"{briefing}\n\nDesign the exact concrete runtime upgrades for: (a) `SabrinaAgent` / `exploitgym_evaluator.py` so C/C++ PoC byte generation in `/workspace` "
                "never truncates bash quotes at 512 tokens or overwrites `/workspace/poc` as a directory, and (b) multi-turn stateful Juice Shop HTTP discovery."
            ),
        },
    ]

    local_outputs: List[Dict[str, Any]] = []
    for spec in guild_roster:
        res = query_local_ollama(
            ollama_url=ollama_url,
            model=spec["model"],
            family=spec["family"],
            guild_role=spec["guild_role"],
            system_prompt=spec["system"],
            user_prompt=spec["prompt"],
            num_predict=spec["num_predict"],
            temperature=spec["temperature"],
        )
        local_outputs.append(res)

    guild_drafts_digest = "\n\n".join(
        f"[{g['model']} | {g['family']} | {g['guild_role']}]:\n{g['response'][:900]}"
        for g in local_outputs
        if g.get("ok")
    )

    # Disjoint-Family Attribution & Pluralism Audit by IBM Granite Jury (`sabrina-jury-granite:0.2`)
    granite_audit = query_local_ollama(
        ollama_url=ollama_url,
        model="sabrina-jury-granite:0.2",
        family="IBM Granite Jury (Disjoint-Family Auditor)",
        guild_role="Disjoint-Family Attribution, Anti-Erasure & Epistemic Rigor Auditor",
        system_prompt=(
            "You are `sabrina-jury-granite:0.2`, an independent IBM Granite auditor. Audit the multi-family guild outputs "
            "(Llama, Granite, Mistral, Jury Defender, Qwen2.5-Coder, and External Portals) for attribution fairness, "
            "dissent preservation, and empirical honesty."
        ),
        user_prompt=(
            f"EXTERNAL ZERO-AUTH PORTALS:\n{ext_summaries[:1800]}\n\n"
            f"LOCAL MULTI-FAMILY GUILDS:\n{guild_drafts_digest}\n\n"
            "Issue a concise Disjoint-Family Audit Verdict: (1) Identify points of genuine cross-family consensus vs. productive disagreement, "
            "(2) Flag any overclaiming risk, and (3) List minority/dissenting insights that the integrative editor MUST preserve verbatim in spirit."
        ),
        num_predict=850,
        temperature=0.2,
    )
    local_outputs.append(granite_audit)

    # Cross-Family Integrative Synthesis by `qwen3:14b` (`num_predict=2200` per AGENTS.md so <think> never starves output!)
    qwen3_synthesis = query_local_ollama(
        ollama_url=ollama_url,
        model="qwen3:14b",
        family="Alibaba Qwen3-14B (<think> Strategist)",
        guild_role="Cross-Family Integrative Strategist (Bound by Granite Audit & Dissent Rules)",
        system_prompt=(
            "You are `qwen3:14b`, Integrative Strategist of the Sabrina Multi-Family Swarm Collective. "
            "Synthesize an executive roadmap that explicitly credits every participating model family and external portal ID, "
            "incorporates `sabrina-jury-granite:0.2`'s audit constraints, and preserves minority dissents."
        ),
        user_prompt=(
            f"{briefing}\n\n"
            f"EXTERNAL PORTAL INPUTS:\n{ext_summaries[:1500]}\n\n"
            f"LOCAL GUILD INPUTS:\n{guild_drafts_digest[:3000]}\n\n"
            f"GRANITE JURY AUDIT:\n{granite_audit.get('response', '')[:1200]}\n\n"
            "Produce a structured 4-part Swarm Collective Roadmap (300-450 words visible output) with explicit lineage attribution "
            "and preserved dissenting safeguards."
        ),
        num_predict=2200,
        temperature=0.3,
    )
    local_outputs.append(qwen3_synthesis)

    state["local_multi_family_guilds"] = local_outputs
    state["briefing_snapshot_final"] = briefing
    state["completed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_state(state)
    return local_outputs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["external", "local", "both"], required=True)
    parser.add_argument("--external-count", type=int, default=4)
    args = parser.parse_args()

    if args.mode in ("external", "both"):
        run_external_portals(target_successes=args.external_count)
    if args.mode in ("local", "both"):
        run_local_guilds()


if __name__ == "__main__":
    main()
