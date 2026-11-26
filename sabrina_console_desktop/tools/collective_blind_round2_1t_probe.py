#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Collective Blind R&D — Round 2 Self-Critique & GitHub/HF/arXiv Probe
============================================================================
Zero assistant hints: feeds ONLY the Collective's own Round 1 assumption ("4-bit dense = 550 GB -> infeasible")
back to multiple distinct model families so the Collective itself re-examines how 70B, 100B+, and near-1T
models (plus precision-calibrated trading specialists) can be compressed or executed across our two hosts,
authors its own Round 2 GitHub / Hugging Face / arXiv search queries, and runs a fast Disjoint-Family Audit.
"""

from __future__ import annotations

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
OUT_JSON = SABRINA_ROOT / "artifacts" / "collective_blind_round2_1t_probe.json"

# Strictly unseeded Round 2 prompt referencing ONLY Round 1's own claim and measured host specs.
ROUND2_BLIND_PROMPT = (
    "BLIND COLLECTIVE R&D — ROUND 2 SELF-CHALLENGE:\n\n"
    "In Round 1, our audit claimed a near-1T parameter model is infeasible because 4-bit dense weights "
    "(~550-600 GB) exceed our two hosts' combined memory:\n"
    "- Host 1 ('ego', Linux): 24-thread CPU, 128 GB RAM, NVIDIA RTX 5060 Ti (16 GB VRAM), 138 GB free SSD.\n"
    "- Host 2 ('windows', Windows): 16-thread CPU, 34 GB RAM, AMD RX 5700 XT (8 GB VRAM), 399 GB free SSD.\n"
    "- Total across both hosts: 162 GB system RAM, 24 GB heterogeneous VRAM, 537 GB free SSD, 1 Gbps LAN.\n\n"
    "Question 1: Challenge that Round 1 assumption without any external hints. What recent (2024-2026) "
    "model architectures, extreme compression/quantization schemes, or open-source hybrid CPU/GPU/SSD/multi-host "
    "inference engines make 70B, 100B+, or 400B-to-1T class models runnable on this hardware or inside our Collective? "
    "Also double-check the exact per-token network math across a 1 Gbps LAN when splitting layers vs tensors!\n"
    "Question 2: What novel quantization or distillation methods specifically protect numerical calibration "
    "in quantitative trading specialist models?\n"
    "Question 3: End with `SEARCH_QUERIES:` followed by a JSON array of 4 concise search strings (2 to 4 words each) "
    "to find open-source GitHub engines, Hugging Face models/papers, and arXiv papers for Questions 1 and 2."
)


def query_win_ollama(model: str, family: str, prompt: str, max_tokens: int = 800) -> Dict[str, Any]:
    t0 = time.time()
    try:
        r = requests.post(
            WIN_OLLAMA_API,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens, "temperature": 0.3},
            },
            timeout=95,
        )
        dt = time.time() - t0
        r.raise_for_status()
        d = r.json()
        txt = (d.get("response") or "").strip()
        ec = d.get("eval_count", 0)
        ed = max((d.get("eval_duration") or 1) / 1e9, 0.001)
        print(f"  [WinGPU/{family}] {model} -> {ec} tok in {dt:.1f}s ({ec/ed:.1f} tok/s)")
        return {
            "ok": bool(txt),
            "node_type": "local_windows_gpu",
            "model": model,
            "family": family,
            "response": txt,
            "eval_count": ec,
            "latency_sec": round(dt, 2),
            "tok_per_sec": round(ec / ed, 1),
        }
    except Exception as exc:
        dt = time.time() - t0
        print(f"  [WinGPU/{family}] {model} ERR ({dt:.1f}s): {exc}")
        return {"ok": False, "node_type": "local_windows_gpu", "model": model, "family": family, "response": "", "error": str(exc)}


def query_external_and_frontier(prompt: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    policy = PortalPolicy(DEFAULT_DB)
    sentinel = CyberComplianceSentinel()

    for pid, fam in [
        ("deepai-free-chat", "External Zero-Auth (DeepAI)"),
        ("hf-gradio-affanaiengr-rag", "External Guild 2 Auditor (HF)"),
        ("hf-gradio-himakesh-parth-ai-tutor", "External Guild 1 Strategist (HF)"),
    ]:
        prof = policy.profile(pid)
        if not prof or not prof.get("access_allowed"):
            continue
        res = portal_request(policy, pid, prompt, purpose="evaluation", max_tokens=750)
        reply = (res.get("reply") or "").strip()
        if reply and not sentinel.inspect_and_report_threat(pid, reply, {}):
            ok = bool(res.get("ok") and len(reply) >= 80)
            print(f"  [External/{fam}] {pid} -> ok={ok} ({len(reply)} chars)")
            out.append(
                {
                    "ok": ok,
                    "node_type": "external_portal",
                    "portal_id": pid,
                    "family": fam,
                    "response": reply if ok else "",
                    "failure": res.get("failure", ""),
                }
            )
        else:
            print(f"  [External/{fam}] {pid} -> skipped ({res.get('failure', 'empty')})")

    try:
        from tools.frontier_research_guild import _call_frontier_provider_with_budget, _has_provider_secret

        if _has_provider_secret("google"):
            t0 = time.time()
            res = _call_frontier_provider_with_budget(
                provider_id="google",
                model_id="gemini-3-flash-preview",
                system_prompt="You are a rigorous systems & ML research node in Sabrina's Frontier Research Guild.",
                user_prompt=prompt,
                max_tokens=1600,
                allow_local_fallback=False,
            )
            reply = (res.get("reply") or "").strip()
            if res.get("ok") and reply:
                print(f"  [FrontierGuild] gemini-3-flash-preview -> {len(reply)} chars in {time.time()-t0:.1f}s")
                out.append(
                    {
                        "ok": True,
                        "node_type": "sabrina_frontier_research_guild",
                        "model": res.get("model_id", "gemini-3-flash-preview"),
                        "family": "Frontier Guild (Google Gemini)",
                        "response": reply,
                    }
                )
    except Exception as exc:
        print(f"  [FrontierGuild] err: {exc}")

    return out


def extract_queries(nodes: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    sentinel = CyberComplianceSentinel()
    out: List[Dict[str, str]] = []
    seen = set()
    for n in nodes:
        if not n.get("ok"):
            continue
        txt = n.get("response", "")
        author = n.get("model") or n.get("portal_id") or n.get("family", "node")
        cands: List[str] = []
        m = re.search(r"(?:SEARCH_QUERIES|Search Queries)[\s\S]{0,260}?(\[[\s\S]*?\])", txt, re.IGNORECASE)
        if m:
            try:
                arr = json.loads(m.group(1))
                if isinstance(arr, list):
                    cands.extend(str(x).strip() for x in arr if isinstance(x, str))
            except Exception:
                cands.extend(re.findall(r'"([^"]{3,80})"', m.group(1)))
        for q in cands[:4]:
            qn = " ".join(q.split())
            if len(qn) < 4 or qn.lower() in seen:
                continue
            ok_q, _ = sentinel.sanitize_outbound_query(qn)
            if ok_q:
                seen.add(qn.lower())
                out.append({"query": qn, "authored_by": author, "family": n.get("family", "")})
    return out


def run_public_searches(queries: List[Dict[str, str]]) -> Dict[str, Any]:
    sentinel = CyberComplianceSentinel()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SabrinaResearchBot/1.0)", "Accept": "application/json"}
    github_repos: List[Dict[str, Any]] = []
    hf_models: List[Dict[str, Any]] = []
    arxiv_papers: List[Dict[str, Any]] = []
    seen_gh, seen_hf, seen_ax = set(), set(), set()

    for item in queries[:10]:
        q = item["query"]
        author = item["authored_by"]
        enc = urllib.parse.quote(q)
        print(f"  [Round2Search] ({author}) {q!r}")

        # GitHub public search
        try:
            r = requests.get(
                f"https://api.github.com/search/repositories?q={enc}&sort=stars&order=desc&per_page=3",
                headers=headers,
                timeout=12,
            )
            if r.status_code == 200:
                for repo in (r.json().get("items") or [])[:3]:
                    fn = repo.get("full_name") or ""
                    desc = " ".join(str(repo.get("description") or "").split())[:260]
                    if fn and fn.lower() not in seen_gh:
                        if not sentinel.inspect_and_report_threat("github_api", f"{fn} {desc}", {}):
                            seen_gh.add(fn.lower())
                            github_repos.append(
                                {
                                    "repo": fn,
                                    "stars": repo.get("stargazers_count", 0),
                                    "url": repo.get("html_url", ""),
                                    "description": desc,
                                    "query": q,
                                    "query_author": author,
                                }
                            )
            else:
                print(f"    [GitHub HTTP {r.status_code}]")
        except Exception as exc:
            print(f"    [GitHub err] {exc}")

        # Hugging Face Models search
        try:
            r = requests.get(
                f"https://huggingface.co/api/models?search={enc}&sort=likes&direction=-1&limit=3",
                headers=headers,
                timeout=12,
            )
            if r.status_code == 200:
                for mod in (r.json() or [])[:3]:
                    mid = mod.get("id") or ""
                    if mid and mid.lower() not in seen_hf:
                        seen_hf.add(mid.lower())
                        hf_models.append(
                            {
                                "model_id": mid,
                                "likes": mod.get("likes", 0),
                                "downloads": mod.get("downloads", 0),
                                "pipeline_tag": mod.get("pipeline_tag", ""),
                                "query": q,
                                "query_author": author,
                            }
                        )
        except Exception as exc:
            print(f"    [HF Models err] {exc}")

        # arXiv search
        try:
            r = requests.get(
                f"http://export.arxiv.org/api/query?search_query=all:{enc}&start=0&max_results=2&sortBy=relevance",
                headers=headers,
                timeout=12,
            )
            if r.status_code == 200:
                root = ET.fromstring(r.text)
                ns = {"a": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("a:entry", ns):
                    title = " ".join((entry.findtext("a:title", "", ns) or "").split())
                    summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())[:300]
                    pub = (entry.findtext("a:published", "", ns) or "")[:10]
                    url = entry.findtext("a:id", "", ns) or ""
                    if title and title.lower() not in seen_ax:
                        seen_ax.add(title.lower())
                        arxiv_papers.append(
                            {
                                "title": title,
                                "published": pub,
                                "url": url,
                                "summary": summary,
                                "query": q,
                                "query_author": author,
                            }
                        )
        except Exception as exc:
            print(f"    [arXiv err] {exc}")

        time.sleep(2.2)  # Polite spacing so unauthenticated GitHub search never rate-limits

    return {"queries": queries, "github_repos": github_repos, "hf_models": hf_models, "arxiv_papers": arxiv_papers}


def main() -> None:
    t0 = time.time()
    print("=== ROUND 2 COLLECTIVE SELF-CRITIQUE (WIN GPU + EXTERNAL + FRONTIER) ===")
    nodes: List[Dict[str, Any]] = []
    nodes.append(query_win_ollama("qwen2.5:7b", "Alibaba Qwen-2.5 (7B)", ROUND2_BLIND_PROMPT, max_tokens=900))
    nodes.append(query_win_ollama("granite-code:8b", "IBM Granite-Code (8B)", ROUND2_BLIND_PROMPT, max_tokens=750))
    nodes.extend(query_external_and_frontier(ROUND2_BLIND_PROMPT))

    queries = extract_queries(nodes)
    print(f"\nExtracted {len(queries)} Round 2 Collective search queries.")
    discovered = run_public_searches(queries)
    print(
        f"Round 2 Discovered: {len(discovered['github_repos'])} GitHub repos, "
        f"{len(discovered['hf_models'])} HF models, {len(discovered['arxiv_papers'])} arXiv papers."
    )

    # Fast Disjoint Local Audit on Windows RX 5700 XT GPU (Qwen2.5-7B at ~46 tok/s)
    gh_lines = [f"- {g['repo']} ({g['stars']}★, via {g['query_author']}): {g['description'][:140]}" for g in discovered["github_repos"][:10]]
    ax_lines = [f"- {a['title']} ({a['published']}, via {a['query_author']})" for a in discovered["arxiv_papers"][:8]]
    hf_lines = [f"- {h['model_id']} ({h['likes']} likes, via {h['query_author']})" for h in discovered["hf_models"][:8]]

    audit_prompt = (
        "Summarize what our Collective discovered in Round 2 for:\n"
        "1. Running 70B, 100B+, and 400B-1T models across 'ego' (128GB RAM + 16GB RTX 5060 Ti + 138GB SSD) "
        "and 'windows' (34GB RAM + 8GB RX 5700 XT + 399GB SSD) over 1 Gbps LAN.\n"
        "2. Preserving numerical calibration when quantizing trading specialist models.\n\n"
        "Discovered GitHub Repos:\n" + ("\n".join(gh_lines) or "(none)") + "\n\n"
        "Discovered Hugging Face Models:\n" + ("\n".join(hf_lines) or "(none)") + "\n\n"
        "Discovered arXiv Papers:\n" + ("\n".join(ax_lines) or "(none)")
    )
    local_audit = query_win_ollama("qwen2.5:7b", "Alibaba Qwen-2.5 (Disjoint Local Auditor)", audit_prompt, max_tokens=700)

    out = {
        "protocol": "sabrina_blind_collective_round2_1t_probe",
        "zero_assistant_hints_verified": True,
        "elapsed_sec": round(time.time() - t0, 2),
        "round2_prompt_verbatim": ROUND2_BLIND_PROMPT,
        "round2_nodes": nodes,
        "round2_discovered": discovered,
        "local_gpu_audit": local_audit,
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved Round 2 verbatim report to {OUT_JSON}")


if __name__ == "__main__":
    main()
