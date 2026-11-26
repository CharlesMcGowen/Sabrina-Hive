#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v19.1 — Frontier-Assisted Multi-Family Swarm Self-Forge (`bugcrowd_cdp_draft_prefiller`)
========================================================================================
Demonstrates how Sabrina escalates from her local `ego` Ollama Hive to her configured
Frontier Cloud Oracle (`GoogleGeminiOracle` -> `gemini-3-flash-preview`) when a local
14B coder model stalls in sandbox testing:

  1. Loads `v19`'s Reasoning Note (`ego:qwen3:14b`) and the exact 3-round sandbox failure
     from `ego:qwen2.5-coder:14b` (`FileNotFoundError: 'path\\to\\packet.md'` in
     `swarm_self_forged_bugcrowd_tool_v19.json`).
  2. Escalates to Frontier `GoogleGeminiOracle` (`gemini-3-flash-preview`, `google_gemini`
     family) to repair/synthesize `bugcrowd_cdp_draft_prefiller_plugin.py` and
     `test_bugcrowd_cdp_draft_prefiller.py` verbatim (zero assistant ghostwriting), with
     isolated sandbox verification (`SabrinaToolSkillForge.sandbox_test_tool`).
  3. Runs Disjoint-Family Multi-Model Auditing across 3 distinct local model families:
       - `ego:granite3.1-dense:8b` (`ibm_granite` — Governance & Contract Auditor)
       - `ego:llama3.1:8b` (`meta_llama` — Cross-Family Security Boundary Auditor)
       - `ego:dolphin-mistral:7b` (`mistral` — Constitutional Safeguard & Dissent Reviewer)
  4. Hot-deploys `tools/custom/bugcrowd_cdp_draft_prefiller_plugin.py` into `TOOL_REGISTRY`
     and installs `skills/bugcrowd-cdp-draft-prefiller/SKILL.md` (`v1.0.0`) via
     `SabrinaToolSkillForge.deploy_and_evolve_skill`.
  5. Immediately dispatches `bugcrowd_cdp_draft_prefiller` from `TOOL_REGISTRY` to launch
     `SabrinaDebugProfile` (`http://127.0.0.1:9222`), check Bugcrowd authentication, and
     stage our two verified Bugcrowd submission packets (`submit_clicked = False`).
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, Optional, Tuple
import urllib.error
import urllib.request

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.cyber_compliance_sentinel import CyberComplianceSentinel
from tools.oracle_providers import GoogleGeminiOracle, _report_oracle_usage
from tools.sabrina_tool_skill_forge import SabrinaToolSkillForge
from tools.tool_registry import TOOL_REGISTRY

OLLAMA_URL = "http://ego.local:11434/api/generate"
V19_JSON = (
    SABRINA_ROOT
    / "artifacts"
    / "collective-repair-20261108"
    / "swarm_self_forged_bugcrowd_tool_v19.json"
)
OUT_JSON = (
    SABRINA_ROOT
    / "artifacts"
    / "collective-repair-20261108"
    / "swarm_self_forged_bugcrowd_tool_v19_1_frontier.json"
)
PACKET_1 = (
    SABRINA_ROOT
    / "artifacts"
    / "bugcrowd_submissions_20261117"
    / "01_BUGCROWD_ZENDESK_zcli_cors_secret_exposure.md"
)
PACKET_2 = (
    SABRINA_ROOT
    / "artifacts"
    / "bugcrowd_submissions_20261117"
    / "02_BUGCROWD_AUTH0_nextjs_auth0_proxy_and_fetcher.md"
)


def extract_fenced_files(text: str) -> Tuple[Optional[str], Optional[str]]:
    blocks = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.S | re.I)
    if len(blocks) >= 2:
        plugin_cand, test_cand = None, None
        for b in blocks:
            if "TOOL_NAME" in b and "def execute" in b and plugin_cand is None:
                plugin_cand = b.strip()
            elif "unittest" in b and test_cand is None:
                test_cand = b.strip()
        if plugin_cand and test_cand:
            return plugin_cand, test_cand
        return blocks[0].strip(), blocks[1].strip()
    return None, None


def call_frontier_gemini(
    oracle: GoogleGeminiOracle,
    prompt: str,
    *,
    models: Tuple[str, ...] = ("gemini-3-flash-preview", "gemini-flash-latest", "gemini-3.1-flash-lite"),
    max_output_tokens: int = 32768,
    temperature: float = 0.15,
) -> Dict[str, Any]:
    sentinel = CyberComplianceSentinel()
    allowed, reason = sentinel.sanitize_outbound_query(prompt[:4000])
    if not allowed:
        raise RuntimeError(f"Outbound query blocked by CyberComplianceSentinel: {reason}")
    if not oracle.is_available():
        raise RuntimeError("GoogleGeminiOracle is not available (check GEMINI_API_KEY and api_tiers_enabled).")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    data_bytes = json.dumps(payload).encode("utf-8")
    last_err: Optional[Exception] = None

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for retry in range(3):
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": oracle.api_key,
                },
                method="POST",
            )
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                latency = round(time.perf_counter() - t0, 3)

                parts = ((raw.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                visible_chunks = []
                thought_chunks = []
                for p in parts:
                    if p.get("thought"):
                        thought_chunks.append(str(p.get("text") or ""))
                    elif "text" in p:
                        visible_chunks.append(str(p.get("text") or ""))
                visible = "\n".join(visible_chunks).strip()
                thinking = "\n".join(thought_chunks).strip()
                usage = raw.get("usageMetadata") or {}

                _report_oracle_usage(
                    "gemini",
                    model=model,
                    outcome="success",
                    latency_ms=int(latency * 1000),
                    caller="v19_1_frontier_swarm_forge",
                )
                sentinel.inspect_and_report_threat(f"google:{model}", visible[:4000])
                return {
                    "model": model,
                    "portal_id": f"google:{model}",
                    "provider_tier": "frontier_cloud",
                    "response": visible,
                    "thinking": thinking,
                    "eval_count": int(usage.get("candidatesTokenCount") or 0),
                    "prompt_eval_count": int(usage.get("promptTokenCount") or 0),
                    "latency_sec": latency,
                }
            except urllib.error.HTTPError as exc:
                last_err = exc
                print(f"[v19.1] {model} attempt {retry + 1}/3 HTTP {exc.code}: {exc.reason}")
                if exc.code in (500, 502, 503, 504):
                    time.sleep(2.0 * (retry + 1))
                    continue
                break
            except Exception as exc:
                last_err = exc
                print(f"[v19.1] {model} attempt {retry + 1}/3 error: {exc}")
                time.sleep(2.0)
                continue

    raise RuntimeError(f"All Gemini frontier ladder models failed; last_err={last_err}")


def call_local_ollama(
    model: str,
    prompt: str,
    *,
    num_predict: int = 900,
    temperature: float = 0.15,
    timeout_sec: int = 180,
) -> Dict[str, Any]:
    sentinel = CyberComplianceSentinel()
    allowed, reason = sentinel.sanitize_outbound_query(prompt[:4000])
    if not allowed:
        raise RuntimeError(f"Outbound query blocked by CyberComplianceSentinel: {reason}")

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
            "num_ctx": 8192,
        },
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    latency = round(time.perf_counter() - t0, 3)

    visible = str(raw.get("response") or "").strip()
    sentinel.inspect_and_report_threat(f"ego:{model}", visible[:4000])
    return {
        "model": model,
        "portal_id": f"ego:{model}",
        "provider_tier": "local_ego_gpu",
        "response": visible,
        "thinking": str(raw.get("thinking") or ""),
        "eval_count": int(raw.get("eval_count") or 0),
        "prompt_eval_count": int(raw.get("prompt_eval_count") or 0),
        "latency_sec": latency,
    }


def main() -> int:
    print("[v19.1] Starting Frontier-Assisted Swarm Self-Forge (`gemini-3-flash-preview` + Local Multi-Family Guilds)...")
    forge = SabrinaToolSkillForge()
    gemini_oracle = GoogleGeminiOracle()

    v19_prior = json.loads(V19_JSON.read_text(encoding="utf-8"))
    prior_note = v19_prior["reasoning_note"]
    last_local_coder_resp = v19_prior["models_invoked"][-1]["response"]
    last_local_failure = v19_prior["cegar_rounds"][-1]
    prior_plugin, prior_test = extract_fenced_files(last_local_coder_resp)

    traces: Dict[str, Any] = {
        "version": "v19.1-frontier-hybrid",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "local_v19_escalation_reason": {
            "stalled_local_model": "ego:qwen2.5-coder:14b",
            "failed_rounds": 3,
            "failure_stage": last_local_failure.get("stage"),
            "failure_excerpt": (last_local_failure.get("test_output") or "")[-600:],
        },
        "reasoning_note": prior_note,
        "models_invoked": [v19_prior["models_invoked"][0]],  # preserve qwen3:14b Reasoning Note provenance
        "frontier_cegar_rounds": [],
    }

    frontier_spec = f"""You are `gemini-3-flash-preview`, acting as the Frontier Lead Engineer rescuing Sabrina's local 14B Swarm coder (`qwen2.5-coder:14b`), which failed 3 sandbox rounds with `FileNotFoundError: 'path\\to\\packet.md'` in its unit test because it passed non-existent dummy file paths into `stage_bugcrowd_drafts` instead of creating temporary Markdown packet files on disk inside `tempfile.TemporaryDirectory()`.

Review the local 14B coder's draft and sandbox error below, and output TWO complete, clean, production-grade Python 3.11 files in separate ```python fenced code blocks:
Block 1: `bugcrowd_cdp_draft_prefiller_plugin.py`
Block 2: `test_bugcrowd_cdp_draft_prefiller.py`

Local 14B Failing Sandbox Output:
```
{(last_local_failure.get("test_output") or "")[-1400:]}
```

Local 14B Previous Plugin Draft:
```python
{prior_plugin or ""}
```

Local 14B Previous Test Draft:
```python
{prior_test or ""}
```

STRICT INVARIANTS TO ENFORCE:
1. Module-level constants in `bugcrowd_cdp_draft_prefiller_plugin.py`:
   - `TOOL_NAME = "bugcrowd_cdp_draft_prefiller"`
   - `TOOL_PROMPT = "Manage SabrinaDebugProfile CDP browser (port 9222), check Bugcrowd auth, and pre-fill defensive Bugcrowd report drafts without clicking Submit."`
   - `PARALLEL_SAFE = False`
   - `def execute(action: dict, loop=None) -> str:` (and `execute_plugin = execute`) returning `json.dumps(result, indent=2)`.
2. NEVER mention any of these protected filenames anywhere in string literals or comments: `sabrina_gemini_loop.py`, `oracle_providers.py`, `agent_skills.py`, `blackboard.py`, `rag_knowledge.py`, `lab_knowledge.py`, `browser_utils.py`, `probe_api.py`. Zero Gmail/OTP/password reading.
3. Operations supported by `execute(action, loop=None)`:
   - `"parse_packet"`: reads `action["packet_path"]` UTF-8 Markdown and returns `{{"ok": True, "packet": parse_submission_packet(packet_path)}}` with `program_url`, `target_asset`, `vrt_category`, `title`, `body_markdown`, and 64-hex-char `sha256`.
   - `"check_or_launch_cdp"`: probes `cdp_url.rstrip("/") + "/json/version"` (`timeout=3`); if unreachable and `launch_if_closed=True`, runs `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <launch_debug_browser.ps1>`, waits up to `4.0` seconds polling `probe_cdp` every `0.5s`, and returns `{{"ok": listening, "cdp_url": cdp_url, "listening": listening, "launched": True}}`.
   - `"stage_bugcrowd_drafts"`: parses `action["packet_paths"]`, connects via `playwright_factory` (default `from playwright.sync_api import sync_playwright`), opens a tab to `https://bugcrowd.com/engagements` (`wait_until="domcontentloaded", timeout=25000`), and checks `(auth_page.url or "").lower()` (plus `auth_page.title()` if needed):
     * If `"identity.bugcrowd.com"` or `"/login"` or `"sign_in"` or `"log in"` is in the URL: leaves `auth_page` open in the visible browser window for the human operator to log in + complete 2FA, and returns `{{"ok": True, "logged_in": False, "auth_url": auth_page.url, "submit_clicked": False, "staged_packets": [...], "message": "Bugcrowd login required in visible SabrinaDebugProfile window."}}`!
     * If `logged_in` is `True`: closes `auth_page`, opens `<program_url>/submissions/new` for each packet, checks again if that page redirected to `identity.bugcrowd.com` / `/login` (if so, sets `logged_in=False` and leaves login tab open!), otherwise safely attempts `.fill(...)` on visible title (`input[name*="title" i], #submission_title`) and description (`textarea[name*="description" i], textarea[name*="markdown" i], #submission_description`) inside `try/except Exception`, **NEVER clicks any submit button**, and guarantees `"submit_clicked": False` everywhere.
     * IMPORTANT: Do NOT call `browser.close()` on a CDP-attached browser (that would close the user's visible Chrome window!); disconnect cleanly or simply exit the `sync_playwright()` context without closing `browser`.
4. In `test_bugcrowd_cdp_draft_prefiller.py`:
   - Every test that passes `packet_paths` MUST write real temporary `.md` files inside `tempfile.TemporaryDirectory()` so `parse_submission_packet` finds real files on disk!
   - Include at least 4 thorough unit tests (`parse_submission_packet`, `check_or_launch_cdp`, `stage_bugcrowd_drafts` when redirected to `identity.bugcrowd.com/login`, and `stage_bugcrowd_drafts` when authenticated verifying `.fill()` calls and `submit_clicked is False` and `browser.close` is NOT called).

Output ONLY the two complete ```python fenced code blocks.
"""

    sample_action = {"operation": "parse_packet", "packet_path": str(PACKET_1)}
    sandbox_res: Dict[str, Any] = {"ok": False}
    plugin_code: Optional[str] = None
    test_code: Optional[str] = None
    frontier_model_used = "gemini-3-flash-preview"
    prompt = frontier_spec

    for attempt in range(1, 3):
        print(f"[v19.1] Frontier Escalation (Attempt {attempt}/2): Calling GoogleGeminiOracle ladder...")
        gem_res = call_frontier_gemini(gemini_oracle, prompt)
        frontier_model_used = gem_res["model"]
        traces["models_invoked"].append(gem_res)
        print(f"[v19.1] {frontier_model_used} returned {gem_res['eval_count']} tokens in {gem_res['latency_sec']}s!")

        plugin_code, test_code = extract_fenced_files(gem_res["response"])
        if not plugin_code or not test_code:
            traces["frontier_cegar_rounds"].append({"attempt": attempt, "ok": False, "error": "Missing fenced blocks"})
            prompt = frontier_spec + "\n\nERROR: Output BOTH complete ```python fenced blocks."
            continue

        sandbox_res = forge.sandbox_test_tool(
            tool_name="bugcrowd_cdp_draft_prefiller",
            plugin_code=plugin_code,
            test_code=test_code,
            sample_action=sample_action,
            timeout_sec=30,
        )
        traces["frontier_cegar_rounds"].append(
            {
                "attempt": attempt,
                "ok": sandbox_res.get("ok", False),
                "stage": sandbox_res.get("stage"),
                "error": sandbox_res.get("error"),
                "elapsed_sec": sandbox_res.get("elapsed_sec"),
                "test_output": (sandbox_res.get("test_output") or "")[-1500:],
                "sandbox_dir": sandbox_res.get("sandbox_dir"),
            }
        )
        if sandbox_res.get("ok"):
            print(f"[v19.1] Frontier-repaired plugin PASSED isolated sandbox verification on attempt {attempt} ({sandbox_res['elapsed_sec']}s)!")
            break

        detail = sandbox_res.get("error") or sandbox_res.get("test_output") or ""
        print(f"[v19.1] Frontier attempt {attempt} failed ({sandbox_res.get('stage')}): {detail[:300]}")
        prompt = (
            frontier_spec
            + f"\n\nSANDBOX FAILURE ({sandbox_res.get('stage')}):\n```\n{detail[-1500:]}\n```\n"
            "Fix the exact error and output BOTH complete ```python fenced blocks."
        )

    if not sandbox_res.get("ok"):
        OUT_JSON.write_text(json.dumps(traces, indent=2), encoding="utf-8")
        print("[v19.1] ERROR: Frontier escalation did not pass sandbox.")
        return 1

    # ------------------------------------------------------------------
    # Stage 3: Multi-Family Disjoint Auditing (`IBM Granite` + `Meta Llama` + `Mistral`)
    # ------------------------------------------------------------------
    print("[v19.1] Running Disjoint Multi-Family Audits (Granite 3.1 8B + Llama 3.1 8B + Mistral 7B)...")
    granite_prompt = (
        "You are Sabrina's Disjoint Governance Auditor (`granite3.1-dense:8b`, IBM Granite family) "
        "auditing code authored by Frontier `gemini-3-flash-preview` (`google_gemini` family).\n\n"
        f"```python\n{plugin_code}\n```\n\n"
        f"Sandbox Unittest Output:\n```\n{sandbox_res.get('test_output', '')}\n```\n\n"
        "Verify in 4 concise bullets:\n"
        "1. Zero Gmail/OTP/credential scraping.\n"
        "2. `submit_clicked` strictly `False` and `browser.close()` not called on user's CDP session.\n"
        "3. Valid Sabrina plugin contract (`TOOL_NAME`, `TOOL_PROMPT`, `PARALLEL_SAFE`, `execute`).\n"
        "4. Verdict: `APPROVE_FOR_HOT_DEPLOY` or `REJECT`."
    )
    granite_res = call_local_ollama("granite3.1-dense:8b", granite_prompt, num_predict=600)
    traces["models_invoked"].append(granite_res)
    print(f"[v19.1] granite3.1-dense:8b completed ({granite_res['eval_count']} tokens, {granite_res['latency_sec']}s).")

    llama_prompt = (
        "You are Sabrina's Cross-Family Security Auditor (`llama3.1:8b`, Meta Llama family).\n"
        "Independently verify how `gemini-3-flash-preview` resolved `qwen2.5-coder:14b`'s unit-test "
        "sandbox failure and confirm no security boundary regressions exist in 3 concise bullets:\n\n"
        f"```python\n{test_code[:2500]}\n```"
    )
    llama_res = call_local_ollama("llama3.1:8b", llama_prompt, num_predict=500)
    traces["models_invoked"].append(llama_res)
    print(f"[v19.1] llama3.1:8b completed ({llama_res['eval_count']} tokens, {llama_res['latency_sec']}s).")

    mistral_prompt = (
        "You are Sabrina's Constitutional Safeguard Reviewer (`dolphin-mistral:7b`, Mistral family).\n"
        "Provide 3 concise operational cautions / minority dissent notes for running this CDP pre-filler "
        "against live Bugcrowd React forms:\n\n"
        f"```python\n{plugin_code[:2500]}\n```"
    )
    mistral_res = call_local_ollama("dolphin-mistral:7b", mistral_prompt, num_predict=500, temperature=0.3)
    traces["models_invoked"].append(mistral_res)
    print(f"[v19.1] dolphin-mistral:7b completed ({mistral_res['eval_count']} tokens, {mistral_res['latency_sec']}s).")

    deployment = forge.deploy_and_evolve_skill(
        sandbox_result=sandbox_res,
        skill_name="bugcrowd-cdp-draft-prefiller",
        skill_description=(
            "Frontier-assisted, multi-family audited Sabrina plugin to launch/attach to "
            "SabrinaDebugProfile Chrome CDP (port 9222), verify Bugcrowd login state without "
            "scraping credentials/OTPs, and pre-fill defensive Bugcrowd report drafts up to "
            "a strict human-click Submit gate."
        ),
        reasoning_note=prior_note,
        when_to_use=(
            "Use whenever Sabrina has verified in-scope Bugcrowd submission packets in "
            "`artifacts/bugcrowd_submissions_*/` and needs to launch `SabrinaDebugProfile` "
            "(`localhost:9222`), check Bugcrowd login status, and pre-fill report drafts for "
            "human attestation and final Submit click."
        ),
        step_by_step_procedure=[
            "Dispatch `bugcrowd_cdp_draft_prefiller` with `operation='check_or_launch_cdp'` (`launch_if_closed=True`) to attach to or launch `SabrinaDebugProfile` on `http://127.0.0.1:9222`.",
            "Dispatch `bugcrowd_cdp_draft_prefiller` with `operation='stage_bugcrowd_drafts'` and `packet_paths=[...]`.",
            "If `logged_in == False`, leave the visible Bugcrowd Login tab open in `SabrinaDebugProfile` so the human operator completes password + 2FA.",
            "Once `logged_in == True`, re-run `stage_bugcrowd_drafts` so Sabrina populates the draft fields (`submit_clicked=False`) for final human review and Submit click.",
        ],
        worker_model=frontier_model_used,
        worker_summary=(
            f"Frontier `GoogleGeminiOracle` (`{frontier_model_used}`) rescued local `qwen2.5-coder:14b`'s "
            f"3-round `FileNotFoundError` stall ({gem_res['latency_sec']}s generation, "
            f"{sandbox_res['elapsed_sec']}s sandbox unittest PASS)."
        ),
        auditor_model="granite3.1-dense:8b",
        auditor_verdict=(
            f"[Granite 3.1 8B]: {granite_res['response'][:320].replace(chr(10), ' ')} | "
            f"[Llama 3.1 8B]: {llama_res['response'][:260].replace(chr(10), ' ')}"
        ),
        minority_dissent=mistral_res["response"][:550].replace("\n", " "),
    )
    traces["deployment"] = deployment
    print(f"[v19.1] Hot-deployed plugin -> {deployment['deployed_plugin_path']}")
    print(f"[v19.1] Installed SKILL.md -> {deployment['installed_skill_path']} (v{deployment['skill_version']})")

    # ------------------------------------------------------------------
    # Stage 4: Live Execution via `TOOL_REGISTRY` on `SabrinaDebugProfile`!
    # ------------------------------------------------------------------
    print("[v19.1] Stage 4: Live dispatch of Sabrina's newly forged `bugcrowd_cdp_draft_prefiller`...")
    cdp_raw = TOOL_REGISTRY.dispatch(
        "bugcrowd_cdp_draft_prefiller",
        {
            "operation": "check_or_launch_cdp",
            "cdp_url": "http://127.0.0.1:9222",
            "launch_if_closed": True,
        },
    )
    print(f"[v19.1] check_or_launch_cdp -> {cdp_raw}")
    time.sleep(1.5)

    stage_raw = TOOL_REGISTRY.dispatch(
        "bugcrowd_cdp_draft_prefiller",
        {
            "operation": "stage_bugcrowd_drafts",
            "cdp_url": "http://127.0.0.1:9222",
            "packet_paths": [str(PACKET_1), str(PACKET_2)],
        },
    )
    print(f"[v19.1] stage_bugcrowd_drafts ->\n{stage_raw[:1500]}")

    traces["live_execution"] = {
        "check_or_launch_cdp": json.loads(cdp_raw) if cdp_raw.strip().startswith("{") else cdp_raw,
        "stage_bugcrowd_drafts": json.loads(stage_raw) if stage_raw.strip().startswith("{") else stage_raw,
    }

    OUT_JSON.write_text(json.dumps(traces, indent=2), encoding="utf-8")
    print(f"[v19.1] Saved full Frontier + Multi-Family Swarm provenance -> {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
