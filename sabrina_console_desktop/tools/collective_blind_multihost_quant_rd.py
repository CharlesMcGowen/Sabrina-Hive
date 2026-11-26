#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Collective Blind R&D Discovery Orchestrator
===================================================
Zero-Hint / Zero-Seeded Discovery Protocol:
  Phase 1: Convene genuinely distinct model families across:
           - Host 1 (`ego` Linux Lab Ollama)
           - Host 2 (`windows` Workstation GPU Ollama)
           - Sanctioned Zero-Auth Public External Portal Guilds (`PortalPolicy` + `CyberComplianceSentinel`)
           - Sabrina Frontier Research Guild (`frontier_research_guild.py`, if budgeted/configured)
           using ONLY the user's open-ended R&D question and raw host hardware specs.
           Zero algorithm names, zero framework names, zero paper hints supplied by assistant.
  Phase 2: Extract ONLY the `SEARCH_QUERIES` authored by the Collective models themselves,
           screen via `CyberComplianceSentinel`, and execute read-only public searches on
           arXiv, Hugging Face Papers/Models, and GitHub Repositories.
  Phase 3: Convene disjoint model families (`IBM Granite`, `Meta Llama`, and External/Frontier Guild)
           to audit hardware feasibility (70B / 100B / ~1T across the two hosts + specialist quantization)
           and preserve minority dissenting proposals verbatim.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

import requests

SABRINA_ROOT = Path(r"c:\Users\bcmcg\Desktop\bot\Sabrina")
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.cyber_compliance_sentinel import CyberComplianceSentinel
from tools.portal_policy import DEFAULT_DB, PortalPolicy
from tools.portal_transport import request as portal_request

WIN_OLLAMA_API = "http://127.0.0.1:11434/api/generate"
EGO_OLLAMA_API = "http://192.168.2.69:11434/api/generate"
OUT_JSON = SABRINA_ROOT / "artifacts" / "collective_blind_multihost_quant_rd.json"

# Strictly unseeded prompt: contains ONLY the user's plain-language questions and measured host specs.
BLIND_RD_PROMPT = (
    "BLIND COLLECTIVE R&D CHALLENGE:\n\n"
    "Our two-host lab has the following physical hardware:\n"
    "- Host 1 ('ego', Linux): 12-core / 24-thread CPU, 128 GB system RAM, "
    "NVIDIA RTX 5060 Ti GPU (16 GB VRAM), 138 GB free SSD.\n"
    "- Host 2 ('windows', Windows): 8-core / 16-thread CPU, 34 GB system RAM, "
    "AMD Radeon RX 5700 XT GPU (8 GB VRAM), 399 GB free SSD.\n"
    "- Network between Host 1 and Host 2: 1 Gbps local Ethernet LAN.\n\n"
    "Please investigate and answer ALL THREE parts thoroughly from your own knowledge:\n"
    "Part A: What new research and novel techniques exist to quantize or compress quantitative "
    "trading specialist models (and decision/reasoning models used around trading) without "
    "destroying their numerical precision or calibration?\n"
    "Part B: What new research, open-source projects, model architectures, or distributed "
    "execution methods make it possible to add and run a much larger model (70B, 100B, or "
    "near 1T / 1 trillion parameters) across our heterogeneous two-host lab ('ego' + 'windows') "
    "or into our multi-model Collective? Name concrete methods, papers, or open-source projects "
    "you know.\n"
    "Part C: At the very end of your response, write `SEARCH_QUERIES:` followed by a valid JSON "
    "array of 4 short search strings (3 to 6 words each) that our research daemon should run on "
    "arXiv, Hugging Face, and GitHub to discover recent research papers and open-source tools "
    "for Part A and Part B."
)

EXTERNAL_PORTAL_CANDIDATES: List[Dict[str, str]] = [
    {"portal_id": "pollinations-openai-fast", "family": "Pollinations / OpenAI-Fast", "guild": "External Guild Alpha"},
    {"portal_id": "chatglm-zhipu-china", "family": "Zhipu GLM-5.3-Flash", "guild": "External Guild Beta"},
    {"portal_id": "hf-gradio-zerolimites-deepseek-ai-deepseek-r1-distill-qwen-14b", "family": "DeepSeek-R1-Distill-14B", "guild": "External Guild Gamma"},
    {"portal_id": "hf-gradio-kingpin92-qwen3-8-27b-iq2-chat", "family": "Qwen3-27B (IQ2 Space)", "guild": "External Guild Delta"},
    {"portal_id": "hf-gradio-joeworld-qwen38-flash-next", "family": "Qwen3.8-Flash-Next", "guild": "External Guild Alpha"},
    {"portal_id": "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "family": "Guild 1 Strategist (HF)", "guild": "External Guild Strategist"},
    {"portal_id": "hf-gradio-htchu-coderhelper", "family": "Guild 2 Auditor (HF)", "guild": "External Guild Auditor"},
    {"portal_id": "hf-gradio-rstanley2026-rodney-digital-twin", "family": "Guild 4 Synthesizer (HF)", "guild": "External Guild Synthesizer"},
    {"portal_id": "deepai-free-chat", "family": "DeepAI Zero-Auth", "guild": "External Guild Beta"},
]


def query_ollama(
    api_url: str,
    host_label: str,
    model: str,
    family: str,
    guild_role: str,
    prompt: str,
    system: str = "",
    max_tokens: int = 550,
    temperature: float = 0.35,
    timeout_sec: int = 150,
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
        resp = requests.post(api_url, json=payload, timeout=timeout_sec)
        dt = time.time() - t0
        resp.raise_for_status()
        data = resp.json()
        eval_count = data.get("eval_count", 0)
        eval_dur = max((data.get("eval_duration") or 1) / 1e9, 0.001)
        tok_s = eval_count / eval_dur if eval_count else 0.0
        text = (data.get("response") or "").strip()
        thinking = (data.get("thinking") or "").strip()
        print(f"  [{host_label}/{family}] {model} -> {eval_count} tok in {dt:.1f}s ({tok_s:.1f} tok/s, {len(text)} chars)")
        return {
            "ok": bool(text),
            "node_type": f"local_{host_label.lower()}_ollama",
            "host": host_label,
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
        dt = time.time() - t0
        print(f"  [{host_label}/{family}] {model} ERROR after {dt:.1f}s: {exc}")
        return {
            "ok": False,
            "node_type": f"local_{host_label.lower()}_ollama",
            "host": host_label,
            "model": model,
            "family": family,
            "guild_role": guild_role,
            "response": "",
            "thinking": "",
            "error": str(exc),
            "latency_sec": round(dt, 2),
        }


def run_win_gpu_stream(prompt: str) -> List[Dict[str, Any]]:
    specs = [
        {
            "model": "granite3-dense:8b",
            "family": "IBM Granite-3 (8B)",
            "guild_role": "Windows GPU Guild — Systems & Memory Hierarchy Lead",
            "system": "You are the IBM Granite systems engineering node in the Sabrina Multi-Family Collective.",
            "max_tokens": 650,
        },
        {
            "model": "qwen2.5:7b",
            "family": "Alibaba Qwen-2.5 (7B)",
            "guild_role": "Windows GPU Guild — Compression & Numerical Methods Lead",
            "system": "You are the Alibaba Qwen quantitative compression node in the Sabrina Multi-Family Collective.",
            "max_tokens": 650,
        },
    ]
    out = []
    for sp in specs:
        out.append(
            query_ollama(
                WIN_OLLAMA_API,
                "Windows_RX5700XT",
                sp["model"],
                sp["family"],
                sp["guild_role"],
                prompt,
                system=sp["system"],
                max_tokens=sp["max_tokens"],
                timeout_sec=120,
            )
        )
    return out


def run_ego_lab_stream(prompt: str) -> List[Dict[str, Any]]:
    specs = [
        {
            "model": "llama3.1:8b",
            "family": "Meta Llama-3.1 (8B)",
            "guild_role": "Ego Lab Guild — Distributed Architecture & Anti-Monoculture Lead",
            "system": "You are the Meta Llama-3.1 distributed systems researcher in the Sabrina Multi-Family Collective.",
            "max_tokens": 450,
        },
        {
            "model": "dolphin-mistral:7b",
            "family": "Mistral / Dolphin (7B)",
            "guild_role": "Ego Lab Guild — Divergent Hypothesis & Unconventional Scaling Lead",
            "system": "You are the Mistral-7B divergent R&D theorist in the Sabrina Multi-Family Collective.",
            "max_tokens": 420,
        },
        {
            "model": "promptbreeder:latest",
            "family": "PromptBreeder (8B)",
            "guild_role": "Ego Lab Guild — Evolutionary Collective Routing Lead",
            "system": "You are PromptBreeder in the Sabrina Multi-Family Collective.",
            "max_tokens": 380,
        },
    ]
    out = []
    for sp in specs:
        out.append(
            query_ollama(
                EGO_OLLAMA_API,
                "Ego_Threadripper_128GB",
                sp["model"],
                sp["family"],
                sp["guild_role"],
                prompt,
                system=sp["system"],
                max_tokens=sp["max_tokens"],
                timeout_sec=130,
            )
        )
    return out


def run_external_portals_stream(prompt: str, target_successes: int = 5) -> List[Dict[str, Any]]:
    policy = PortalPolicy(DEFAULT_DB)
    sentinel = CyberComplianceSentinel()
    clean_ok, clean_msg = sentinel.sanitize_outbound_query(prompt)
    if not clean_ok:
        raise RuntimeError(f"Sentinel blocked outbound prompt: {clean_msg}")

    results: List[Dict[str, Any]] = []
    successes = 0
    last_hf_finish = 0.0

    for cand in EXTERNAL_PORTAL_CANDIDATES:
        if successes >= target_successes:
            break
        pid = cand["portal_id"]
        prof = policy.profile(pid)
        if not prof or not prof.get("access_allowed"):
            continue
        adp = json.loads(prof["adapter_json"])
        if adp.get("headers") or adp.get("api_key") or adp.get("token") or adp.get("key_env"):
            continue

        if prof["provider"] == "huggingface" and last_hf_finish > 0:
            wait_needed = 10.6 - (time.time() - last_hf_finish)
            if wait_needed > 0:
                time.sleep(wait_needed)

        print(f"  [External/{cand['family']}] Querying {pid}...")
        res = portal_request(policy, pid, prompt, purpose="evaluation", max_tokens=750)
        if prof["provider"] == "huggingface":
            last_hf_finish = time.time()

        reply = (res.get("reply") or "").strip()
        if reply:
            threat = sentinel.inspect_and_report_threat(pid, reply, {"claimed_model": prof["model_claim"]})
            if threat:
                res["ok"] = False
                res["failure"] = "quarantined_by_sentinel"

        ok = bool(res.get("ok") and len(reply) >= 100)
        entry = {
            "ok": ok,
            "node_type": "external_zero_auth_portal",
            "portal_id": pid,
            "claimed_model": prof["model_claim"],
            "family": cand["family"],
            "guild_role": cand["guild"],
            "response": reply if ok else "",
            "failure": res.get("failure", ""),
            "latency_sec": round((res.get("latency_ms") or 0) / 1000.0, 2),
            "provenance_ref": res.get("provenance_ref", ""),
        }
        results.append(entry)
        if ok:
            successes += 1
            print(f"    [+] {pid} ({cand['family']}) SUCCEEDED ({len(reply)} chars in {entry['latency_sec']}s)")
        else:
            print(f"    [-] {pid} skipped/deferred: {entry['failure'] or 'short_reply'}")

    return results


def run_sabrina_frontier_guild_stream(prompt: str) -> List[Dict[str, Any]]:
    """Invoke Sabrina's budgeted Frontier Research Guild (`frontier_research_guild.py`) with the exact same blind prompt."""
    out: List[Dict[str, Any]] = []
    try:
        from tools.frontier_research_guild import (
            FRONTIER_PROVIDERS,
            _call_frontier_provider_with_budget,
            _has_provider_secret,
        )

        for provider_id in ("google", "openai", "spacex_xai", "hf_router", "anthropic"):
            if not _has_provider_secret(provider_id):
                continue
            meta = FRONTIER_PROVIDERS[provider_id]
            # For google, try gemini-3-flash-preview first for reliable availability
            model_candidates = (
                ["gemini-3-flash-preview", "gemini-flash-lite-latest"]
                if provider_id == "google"
                else [meta["default_model"]]
            )
            for mid in model_candidates:
                t0 = time.time()
                res = _call_frontier_provider_with_budget(
                    provider_id=provider_id,
                    model_id=mid,
                    system_prompt="You are a research node in Sabrina's Frontier Research Guild. Answer directly and rigorously.",
                    user_prompt=prompt,
                    max_tokens=1400,
                    allow_local_fallback=False,
                )
                dt = time.time() - t0
                reply = (res.get("reply") or "").strip()
                if res.get("ok") and len(reply) >= 100:
                    print(f"  [FrontierGuild/{provider_id}] {res.get('model_id', mid)} -> {len(reply)} chars in {dt:.1f}s")
                    out.append(
                        {
                            "ok": True,
                            "node_type": "sabrina_frontier_research_guild",
                            "provider_id": provider_id,
                            "model": res.get("model_id", mid),
                            "family": f"Frontier Guild ({meta['label']})",
                            "guild_role": "Sabrina Budgeted Frontier Research Seat",
                            "response": reply,
                            "thinking": (res.get("thinking") or "").strip(),
                            "latency_sec": round(dt, 2),
                        }
                    )
                    break
                else:
                    print(f"  [FrontierGuild/{provider_id}] {mid} skipped: {res.get('error', 'empty')}")
    except Exception as exc:
        print(f"  [FrontierGuild] stream warning: {exc}")
    return out


def extract_collective_search_queries(phase1_nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Extract ONLY search queries authored by the Collective models themselves (zero hardcoded queries)."""
    sentinel = CyberComplianceSentinel()
    extracted: List[Dict[str, str]] = []
    seen = set()

    for node in phase1_nodes:
        if not node.get("ok"):
            continue
        text = node.get("response", "")
        author = node.get("model") or node.get("portal_id") or node.get("family", "unknown")

        candidates: List[str] = []
        # Match JSON list after SEARCH_QUERIES
        m = re.search(r"SEARCH_QUERIES\s*:\s*(\[[\s\S]*?\])", text, re.IGNORECASE)
        if m:
            raw_arr = m.group(1)
            try:
                parsed = json.loads(raw_arr)
                if isinstance(parsed, list):
                    candidates.extend(str(x).strip() for x in parsed if isinstance(x, str))
            except Exception:
                candidates.extend(re.findall(r'"([^"]{4,90})"', raw_arr))
        else:
            # Fallback if model formatted bullets under SEARCH_QUERIES:
            m2 = re.search(r"SEARCH_QUERIES\s*:([\s\S]{10,500})$", text, re.IGNORECASE)
            if m2:
                for line in m2.group(1).splitlines():
                    clean = re.sub(r"^[\s\-\*\d\.\)\"\'`]+|[\"\'`,\s]+$", "", line).strip()
                    if 6 <= len(clean) <= 90 and not clean.startswith("{") and not clean.startswith("["):
                        candidates.append(clean)

        for q in candidates[:4]:
            q_norm = " ".join(q.split())
            key = q_norm.lower()
            if len(q_norm) < 5 or key in seen:
                continue
            ok_q, _ = sentinel.sanitize_outbound_query(q_norm)
            if not ok_q:
                continue
            seen.add(key)
            extracted.append({"query": q_norm, "authored_by": author, "family": node.get("family", "")})

    return extracted


def execute_collective_public_literature_search(queries: List[Dict[str, str]], max_queries: int = 12) -> Dict[str, Any]:
    """Run the Collective's own search queries against public zero-auth arXiv, Hugging Face, and GitHub APIs."""
    sentinel = CyberComplianceSentinel()
    headers = {"User-Agent": "SabrinaCollectiveResearch/1.0"}
    arxiv_hits: List[Dict[str, Any]] = []
    hf_paper_hits: List[Dict[str, Any]] = []
    github_hits: List[Dict[str, Any]] = []
    seen_titles = set()
    seen_repos = set()

    selected = queries[:max_queries]
    for item in selected:
        q = item["query"]
        author = item["authored_by"]
        print(f"  [BlindSearch] Running query by {author}: {q!r}")

        # 1. arXiv API (sorted by submittedDate descending + relevance)
        try:
            enc = urllib.parse.quote(q)
            url = f"http://export.arxiv.org/api/query?search_query=all:{enc}&start=0&max_results=3&sortBy=relevance"
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                ns = {"a": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("a:entry", ns):
                    title = " ".join((entry.findtext("a:title", "", ns) or "").split())
                    summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())[:380]
                    published = (entry.findtext("a:published", "", ns) or "")[:10]
                    link = entry.findtext("a:id", "", ns) or ""
                    if not title or title.lower() in seen_titles:
                        continue
                    if sentinel.inspect_and_report_threat("arxiv_api", f"{title} {summary}", {"query": q}):
                        continue
                    seen_titles.add(title.lower())
                    arxiv_hits.append(
                        {
                            "title": title,
                            "published": published,
                            "url": link,
                            "summary": summary,
                            "discovered_via_query": q,
                            "query_author": author,
                        }
                    )
        except Exception as exc:
            print(f"    [arXiv warn] {exc}")

        # 2. Hugging Face Papers API
        try:
            enc = urllib.parse.quote(q)
            url = f"https://huggingface.co/api/papers?q={enc}"
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                items = r.json()
                if isinstance(items, list):
                    for p in items[:3]:
                        paper = p.get("paper", p) if isinstance(p, dict) else {}
                        title = " ".join(str(paper.get("title") or p.get("title") or "").split())
                        summary = " ".join(str(paper.get("summary") or "").split())[:360]
                        pid = str(paper.get("id") or "")
                        pub = str(paper.get("publishedAt") or "")[:10]
                        if not title or title.lower() in seen_titles:
                            continue
                        if sentinel.inspect_and_report_threat("hf_papers_api", f"{title} {summary}", {"query": q}):
                            continue
                        seen_titles.add(title.lower())
                        hf_paper_hits.append(
                            {
                                "title": title,
                                "published": pub,
                                "url": f"https://huggingface.co/papers/{pid}" if pid else "",
                                "summary": summary,
                                "discovered_via_query": q,
                                "query_author": author,
                            }
                        )
        except Exception as exc:
            print(f"    [HF Papers warn] {exc}")

        # 3. GitHub Public Repositories Search API
        try:
            enc = urllib.parse.quote(q)
            url = f"https://api.github.com/search/repositories?q={enc}&sort=stars&order=desc&per_page=3"
            r = requests.get(url, headers=headers, timeout=12)
            if r.status_code == 200:
                items = (r.json() or {}).get("items") or []
                for repo in items[:3]:
                    full_name = repo.get("full_name") or ""
                    desc = " ".join(str(repo.get("description") or "").split())[:300]
                    stars = repo.get("stargazers_count", 0)
                    html_url = repo.get("html_url") or ""
                    if not full_name or full_name.lower() in seen_repos:
                        continue
                    if sentinel.inspect_and_report_threat("github_search_api", f"{full_name} {desc}", {"query": q}):
                        continue
                    seen_repos.add(full_name.lower())
                    github_hits.append(
                        {
                            "repo": full_name,
                            "stars": stars,
                            "url": html_url,
                            "description": desc,
                            "discovered_via_query": q,
                            "query_author": author,
                        }
                    )
        except Exception as exc:
            print(f"    [GitHub warn] {exc}")

        time.sleep(0.6)

    return {
        "queries_executed": selected,
        "arxiv_papers": arxiv_hits,
        "hf_papers": hf_paper_hits,
        "github_repos": github_hits,
    }


def run_phase3_disjoint_synthesis(
    phase1_nodes: List[Dict[str, Any]],
    search_bundle: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Have disjoint model families synthesize Phase 1 Collective proposals + Phase 2 discovered literature."""
    p1_digest_lines = []
    for n in phase1_nodes:
        if n.get("ok"):
            ident = n.get("model") or n.get("portal_id") or n.get("family")
            snippet = " ".join(n["response"].split())[:520]
            p1_digest_lines.append(f"- [{n.get('family')} / {ident}]: {snippet}")

    paper_lines = []
    for p in (search_bundle.get("hf_papers", [])[:8] + search_bundle.get("arxiv_papers", [])[:8]):
        paper_lines.append(
            f"- PAPER ({p['published']}, via {p['query_author']}): {p['title']} — {p['summary'][:200]}"
        )

    repo_lines = []
    for g in search_bundle.get("github_repos", [])[:10]:
        repo_lines.append(
            f"- REPO ({g['stars']} stars, via {g['query_author']}): {g['repo']} — {g['description'][:180]}"
        )

    synth_prompt = (
        "PHASE 3 COLLECTIVE FEASIBILITY AUDIT & SYNTHESIS:\n\n"
        "Hardware reminder:\n"
        "- Host 1 ('ego', Linux): 24-thread CPU, 128 GB RAM, RTX 5060 Ti 16 GB VRAM, 138 GB SSD.\n"
        "- Host 2 ('windows', Windows): 16-thread CPU, 34 GB RAM, Radeon RX 5700 XT 8 GB VRAM, 399 GB SSD.\n"
        "- Interconnect: 1 Gbps Ethernet LAN.\n\n"
        "=== PHASE 1 MULTI-FAMILY PROPOSALS ===\n"
        + "\n".join(p1_digest_lines[:10])
        + "\n\n=== PHASE 2 COLLECTIVE-DISCOVERED PAPERS ===\n"
        + ("\n".join(paper_lines) if paper_lines else "(none)")
        + "\n\n=== PHASE 2 COLLECTIVE-DISCOVERED OPEN-SOURCE REPOS ===\n"
        + ("\n".join(repo_lines) if repo_lines else "(none)")
        + "\n\nAudit these Collective findings rigorously:\n"
        "1. Which discovered open-source frameworks, model architectures, and memory/network techniques "
        "actually let our two-host lab run a 70B, 100B+, or near-1T parameter model (calculate exact RAM/VRAM/SSD "
        "footprint and 1 Gbps LAN bottleneck impact)?\n"
        "2. Which discovered quantization/compression methods best preserve numerical precision for our trading specialists?\n"
        "3. Credit specific Guild members/queries and preserve any strong minority or unconventional ideas."
    )

    auditors: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_granite = pool.submit(
            query_ollama,
            WIN_OLLAMA_API,
            "Windows_RX5700XT",
            "granite3-dense:8b",
            "IBM Granite-3 (Phase 3 Systems Auditor)",
            "Disjoint-Family Hardware & Memory Feasibility Auditor",
            synth_prompt,
            "You are IBM Granite-3 auditing multi-family R&D proposals with strict memory and bandwidth math.",
            700,
            0.25,
            130,
        )
        f_llama = pool.submit(
            query_ollama,
            EGO_OLLAMA_API,
            "Ego_Threadripper_128GB",
            "llama3.1:8b",
            "Meta Llama-3.1 (Phase 3 Dissent & Architecture Auditor)",
            "Disjoint-Family Architecture & Dissent Preservation Auditor",
            synth_prompt,
            "You are Meta Llama-3.1 auditing multi-family R&D proposals and preserving minority breakthroughs.",
            480,
            0.25,
            140,
        )
        auditors.append(f_granite.result())
        auditors.append(f_llama.result())

    # Also let Sabrina's Frontier Guild Chair audit the Collective's own discoveries if available
    frontier_synth = run_sabrina_frontier_guild_stream(synth_prompt)
    for item in frontier_synth:
        item["guild_role"] = "Phase 3 Integrative Systems Auditor"
        auditors.append(item)

    return auditors


def main() -> None:
    t_start = time.time()
    print("=== PHASE 1: BLIND MULTI-FAMILY COLLECTIVE BRAINSTORM & QUERY GENERATION ===")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fut_win = pool.submit(run_win_gpu_stream, BLIND_RD_PROMPT)
        fut_ego = pool.submit(run_ego_lab_stream, BLIND_RD_PROMPT)
        fut_ext = pool.submit(run_external_portals_stream, BLIND_RD_PROMPT, 5)
        fut_frn = pool.submit(run_sabrina_frontier_guild_stream, BLIND_RD_PROMPT)

        win_nodes = fut_win.result()
        ego_nodes = fut_ego.result()
        ext_nodes = fut_ext.result()
        frn_nodes = fut_frn.result()

    phase1_all = win_nodes + ego_nodes + ext_nodes + frn_nodes
    ok_p1 = [n for n in phase1_all if n.get("ok")]
    print(f"\nPhase 1 complete: {len(ok_p1)}/{len(phase1_all)} Collective nodes delivered responses.")

    print("\n=== PHASE 2: COLLECTIVE-AUTHORED AUTONOMOUS LITERATURE & REPO DISCOVERY ===")
    queries = extract_collective_search_queries(phase1_all)
    print(f"Extracted {len(queries)} unique Collective-authored search queries.")
    search_bundle = execute_collective_public_literature_search(queries, max_queries=14)
    print(
        f"Discovered {len(search_bundle['arxiv_papers'])} arXiv papers, "
        f"{len(search_bundle['hf_papers'])} Hugging Face papers, and "
        f"{len(search_bundle['github_repos'])} GitHub repositories."
    )

    print("\n=== PHASE 3: DISJOINT-FAMILY HARDWARE FEASIBILITY & DISSENT AUDIT ===")
    phase3_auditors = run_phase3_disjoint_synthesis(phase1_all, search_bundle)

    payload = {
        "protocol": "sabrina_blind_collective_multihost_quant_rd_v1",
        "zero_assistant_hints_verified": True,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(time.time() - t_start, 2),
        "blind_prompt_verbatim": BLIND_RD_PROMPT,
        "host_hardware_measured": {
            "ego_linux": {
                "cpu": "AMD Ryzen Threadripper 1920X (12C/24T, 2 NUMA nodes)",
                "ram_gb": 128,
                "gpu": "NVIDIA GeForce RTX 5060 Ti (16 GB VRAM)",
                "free_ssd_gb": 138,
            },
            "windows_host": {
                "cpu": "AMD Ryzen 8C/16T",
                "ram_gb": 34.3,
                "gpu": "AMD Radeon RX 5700 XT (8 GB VRAM)",
                "free_ssd_gb": 399.5,
            },
            "interconnect": "1 Gbps Ethernet LAN",
        },
        "phase1_collective_nodes": phase1_all,
        "phase2_autonomous_discovery": search_bundle,
        "phase3_disjoint_auditors": phase3_auditors,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved verbatim Blind Collective R&D report to: {OUT_JSON}")


if __name__ == "__main__":
    main()
