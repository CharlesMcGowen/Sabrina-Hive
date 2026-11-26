#!/usr/bin/env python3
"""Convene Sabrina's Multi-Family Collective + Internet Resources to Co-Design `sabrina-mesh`.

Queries:
1. Live Internet Resources (GitHub distributed-inference repos + Sabrina's InternetResourceIndexer).
2. Local Windows RX 5700 XT GPU (`FrameworkGranite3b:latest` / `granite3-dense:8b` — IBM Granite family).
3. External Frontier Reasoning (`gemini-3-flash-preview` — Google Gemini family).
4. External Zero-Auth Reasoning (`pollinations:openai-fast` — OpenAI GPT-OSS family).
5. Sanctioned Zero-Auth Hugging Face Space Portals (`PortalPolicy` + `portal_transport.request`).

Saves 100% verbatim, `UpstreamSanitizer`-scrubbed provenance to JSON for `CharlesMcGowen/sabrina-mesh`.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, List
import urllib.request

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.cyber_compliance_sentinel import CyberComplianceSentinel
from tools.portal_policy import DEFAULT_DB, PortalPolicy
from tools.portal_transport import request as portal_request
from tools.sabrina_self_repair_workshop import _gemini_chat, _pollinations_chat
from tools.sabrina_upstream_forge import UpstreamSanitizer

OUT_RAW_JSON = SABRINA_ROOT / "data" / "sabrina_mesh_collective_raw.json"


def harvest_internet_references() -> List[Dict[str, Any]]:
    """Fetch public open-source architectural metadata from GitHub + local indexed resources."""
    refs: List[Dict[str, Any]] = []
    for repo in ("Mesh-LLM/mesh-llm", "exo-explore/exo", "ggml-org/llama.cpp"):
        try:
            req = urllib.request.Request(
                f"https://api.github.com/repos/{repo}",
                headers={"User-Agent": "SabrinaCollectiveResearch/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8"))
            refs.append({
                "source": "github_public_api",
                "id": repo,
                "stars": d.get("stargazers_count", 0),
                "language": d.get("language", ""),
                "description": d.get("description", ""),
                "topics": d.get("topics", [])[:8],
            })
        except Exception as exc:
            refs.append({"source": "github_public_api", "id": repo, "error": str(exc)})

    idx_db = SABRINA_ROOT / "data" / "sabrina_assistant.sqlite3"
    if idx_db.is_file():
        try:
            with sqlite3.connect(str(idx_db)) as conn:
                conn.row_factory = sqlite3.Row
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='internet_resource_index'").fetchone():
                    rows = conn.execute(
                        "SELECT title, url, resource_kind, quality_score FROM internet_resource_index WHERE status='active' ORDER BY quality_score DESC LIMIT 5"
                    ).fetchall()
                    for row in rows:
                        refs.append({"source": "sabrina_internet_resource_indexer", **dict(row)})
        except Exception:
            pass
    return refs


def call_local_granite_gpu(prompt: str, system_prompt: str) -> Dict[str, Any]:
    for mod in ("granite3-dense:8b", "FrameworkGranite3b:latest"):
        t0 = time.monotonic()
        try:
            payload = json.dumps({
                "model": mod,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "options": {"num_predict": 650, "temperature": 0.25},
            }).encode("utf-8")
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=75) as r:
                d = json.loads(r.read().decode("utf-8"))
            txt = str(((d.get("message") or {}).get("content")) or "").strip()
            if txt:
                return {
                    "ok": True,
                    "family": "IBM Granite (Local Vulkan GPU)",
                    "model_id": mod,
                    "role": "Guild Governance, Peer Lease & Failover Architect",
                    "latency_sec": round(time.monotonic() - t0, 2),
                    "eval_count": int(d.get("eval_count") or 0),
                    "response": txt,
                }
        except Exception:
            continue
    return {"ok": False, "family": "IBM Granite", "model_id": "granite", "response": ""}


def call_external_hf_guilds(sentinel: CyberComplianceSentinel, target_ok: int = 2) -> List[Dict[str, Any]]:
    policy = PortalPolicy(DEFAULT_DB)
    prompt = (
        "We are designing `sabrina-mesh`, an open-source Python distributed LLM layer/MoE sharding planner "
        "that splits transformer layers and Mixture-of-Experts weights across heterogeneous GPUs (e.g., 16 GB + 8 GB VRAM), "
        "system DDR RAM, and NVMe mmap storage. In 180-280 words, identify:\n"
        "1. The top 2 real-world failure modes or bottlenecks (e.g. KV-cache growth, slow LAN stragglers, activation transfer overhead).\n"
        "2. One concrete dissenting safeguard or fallback rule the planner MUST enforce so it never crashes with OOM."
    )
    ok_out, msg = sentinel.sanitize_outbound_query(prompt)
    if not ok_out:
        raise RuntimeError(f"Outbound sentinel blocked prompt: {msg}")

    candidates = [
        ("hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api", "External Strategist Guild (HF Space)"),
        ("hf-gradio-himakesh-parth-ai-tutor", "External Strategist Guild (HF Space)"),
        ("hf-gradio-htchu-coderhelper", "External Auditor Guild (HF Space)"),
        ("hf-gradio-rstanley2026-rodney-digital-twin", "External Synthesizer Guild (HF Space)"),
        ("hf-gradio-nakaassoumatine-einstein-chatbot", "External Synthesizer Guild (HF Space)"),
    ]
    out: List[Dict[str, Any]] = []
    ok_cnt = 0
    last_hf = 0.0
    for pid, label in candidates:
        if ok_cnt >= target_ok:
            break
        prof = policy.profile(pid)
        if not prof or not prof.get("access_allowed"):
            continue
        adp = json.loads(prof["adapter_json"])
        if adp.get("headers") or adp.get("api_key") or adp.get("token") or adp.get("key_env"):
            continue
        wait = 10.6 - (time.monotonic() - last_hf)
        if last_hf > 0 and wait > 0:
            time.sleep(wait)
        res = portal_request(policy, pid, prompt, purpose="evaluation", max_tokens=550)
        last_hf = time.monotonic()
        reply = (res.get("reply") or "").strip()
        if reply:
            threat = sentinel.inspect_and_report_threat(pid, reply, {"claimed_model": prof["model_claim"]})
            if threat:
                continue
        if res.get("ok") and len(reply) >= 80 and "401" not in reply and "Error" not in reply[:30]:
            ok_cnt += 1
            out.append({
                "ok": True,
                "family": label,
                "model_id": pid,
                "claimed_model": prof["model_claim"],
                "role": "Adversarial Edge-Case & Dissent Auditor",
                "latency_sec": round((res.get("latency_ms") or 0) / 1000.0, 2),
                "response": reply,
            })
    return out


def main() -> None:
    sentinel = CyberComplianceSentinel()
    sanitizer = UpstreamSanitizer(sentinel)
    refs = harvest_internet_references()
    ref_summary = json.dumps(refs, indent=2)

    contributions: List[Dict[str, Any]] = []

    # 1. Google Gemini Family — DP Sharding & MoE Placement Formalism
    t0 = time.monotonic()
    gem_txt = _gemini_chat(
        "gemini-3-flash-preview",
        "You are the Mathematical Sharding Strategist in the Sabrina Multi-Family Collective. Be concise, rigorous, and concrete.",
        (
            f"Internet Reference Context:\n{ref_summary}\n\n"
            "Formulate the exact mathematical cost model and Dynamic Programming (DP) recurrence for `sabrina-mesh`'s "
            "VRAM- & Bandwidth-Aware Layer + MoE Expert Sharding Planner across K heterogeneous computers "
            "(mixing CUDA GPUs, Vulkan GPUs, DDR RAM, and NVMe mmap). Include:\n"
            "1. Exact per-layer weight + GQA KV-cache byte formula.\n"
            "2. Why MoE attention/router weights belong in GPU VRAM while cold routed experts spill to DDR RAM/NVMe.\n"
            "3. Per-token latency roofline formula combining memory-bandwidth read time + inter-node activation transfer."
        ),
        max_tokens=4096,
        temperature=0.2,
    )
    contributions.append({
        "ok": bool(gem_txt),
        "family": "Google Gemini",
        "model_id": "gemini-3-flash-preview",
        "role": "Mathematical DP Layer & MoE Sharding Strategist",
        "latency_sec": round(time.monotonic() - t0, 2),
        "response": sanitizer.scrub_text(gem_txt),
    })

    # 2. IBM Granite Family (Local RX 5700 XT GPU) — Peer Governance & Re-Sharding
    gran_res = call_local_granite_gpu(
        prompt=(
            "Design the self-managing peer node health, lease, and automatic dead-node re-sharding protocol "
            "for `sabrina-mesh` (200-300 words). Explain how nodes advertise free VRAM, free RAM, bandwidth GB/s, "
            "and link latency, and how the mesh safely re-computes layer assignments if a peer drops mid-session."
        ),
        system_prompt="You are the IBM Granite Governance Architect in the Sabrina Multi-Family Collective.",
    )
    if gran_res.get("ok"):
        gran_res["response"] = sanitizer.scrub_text(gran_res["response"])
        contributions.append(gran_res)

    # 3. OpenAI GPT-OSS Family (Pollinations Zero-Auth) — OpenAI-Compatible Mesh Gateway
    t1 = time.monotonic()
    try:
        pol_txt = _pollinations_chat(
            "openai-fast",
            "You are the Distributed Systems API Lead in the Sabrina Multi-Family Collective.",
            (
                "Specify the clean OpenAI-compatible HTTP gateway contract (`/v1/models`, `/v1/chat/completions`, "
                "`/v1/mesh/nodes`, `/v1/mesh/plan`) for `sabrina-mesh` in 200-300 words, including how single-node "
                "fit, peer routing, and multi-node pipeline layer-splitting are selected automatically."
            ),
            temperature=0.2,
        )
        if pol_txt:
            contributions.append({
                "ok": True,
                "family": "OpenAI GPT-OSS (Pollinations Zero-Auth)",
                "model_id": "pollinations:openai-fast",
                "role": "OpenAI-Compatible Gateway & Routing Architect",
                "latency_sec": round(time.monotonic() - t1, 2),
                "response": sanitizer.scrub_text(pol_txt),
            })
    except Exception as exc:
        contributions.append({"ok": False, "family": "Pollinations", "error": str(exc), "response": ""})

    # 4. External Zero-Auth Hugging Face Spaces Guilds — Adversarial Critique & Dissent
    for ext in call_external_hf_guilds(sentinel, target_ok=2):
        ext["response"] = sanitizer.scrub_text(ext["response"])
        contributions.append(ext)

    payload = {
        "project": "sabrina-mesh",
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "internet_references": refs,
        "distinct_families_participating": sum(1 for c in contributions if c.get("ok")),
        "contributions": [c for c in contributions if c.get("ok")],
    }
    OUT_RAW_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_RAW_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"SAVED {payload['distinct_families_participating']} verbatim family contributions -> {OUT_RAW_JSON}")


if __name__ == "__main__":
    main()
