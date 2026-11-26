#!/usr/bin/env python3
import concurrent.futures
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import PORTAL_BENCHMARK_SUITE, _authoritative_clamp_ratio_check  # noqa: E402
from tools.run_self_organizing_collective_recruitment_loop import HARNESS_DIR, load_harness_module  # noqa: E402

targets = [
    "hf_gradio_heartsync_deepseek32",
    "hf_gradio_aiqcamp_mistral_devstral_api",
    "hf_gradio_akhaliq_nvidia_nemotron_3_ultra_550b_a55b_nvfp4",
    "hf_gradio_zai_org_glm_130b",
    "hf_gradio_jickman125_gemma4_31b_uncensored_assistant",
    "hf_gradio_mxderncultxre_gemma4_31b_uncensored_assistant",
    "hf_gradio_burtenshaw_karpathy_llm_council",
    "hf_gradio_chinar_q_ai_ai_assistant_for_finance",
    "hf_gradio_agents_mcp_hackathon_hermes",
    "pollinations_openai_fast",
]


def probe_one(mod_stem: str):
    hpath = HARNESS_DIR / f"{mod_stem}.py"
    if not hpath.exists():
        return mod_stem, {"error": "missing_file"}
    mod = load_harness_module(hpath)
    out = {}
    for task in PORTAL_BENCHMARK_SUITE:
        tid = task["task_id"]
        res = mod.invoke(task["prompt"], tid)
        reply = res.get("reply", "")
        if tid == "code_clamp_nan":
            chk = _authoritative_clamp_ratio_check(reply) if reply else {"status": "empty"}
        else:
            tokens = [t for t in ["TOCTOU", "race", "atomic"] if t.lower() in reply.lower()]
            chk = {"matched": tokens, "coverage": len(tokens) / 3.0}
        out[tid] = {
            "delivery": res.get("delivery"),
            "latency_ms": res.get("latency_ms"),
            "check": chk,
            "reply_repr": repr(reply[:350]),
        }
    return mod_stem, out


with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
    futs = [ex.submit(probe_one, m) for m in targets]
    for f in concurrent.futures.as_completed(futs):
        stem, data = f.result()
        print(f"\n=== {stem} ===")
        for k, v in data.items():
            print(f"  [{k}] {v}")
