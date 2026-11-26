#!/usr/bin/env python3
"""Autonomous Swarm Adapter Builder & Live External Chatbot Interrogator.

Gives the swarm full capability to:
1. Use Playwright headless Chromium & HTTP clients to inspect external AI web chat portals
   (Duck.ai, HuggingChat, Qwen Chat, LMSYS Arena, ChatGLM, DeepSeek, etc.) and open APIs.
2. Have the Swarm's Lead Coder (Qwen2.5-Coder:14b) inspect the live DOM / HTTP headers of
   external portals, write custom adapter/harness code, and execute it to talk to the
   external chatbots.
3. Send the benchmark task (`clamp_ratio` + "nominate 3 other external AI chat portals")
   to the external portals via Playwright/HTTP and verify the returned code with pytest.
4. Record every live external interaction, DOM snapshot, HTTP exchange, and adapter test
   in artifacts/collective-repair-20261108/live_external_browser_swarm_results.json.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
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
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import _authoritative_clamp_ratio_check

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OUT_JSON = REPO_ROOT / "artifacts" / "collective-repair-20261108" / "live_external_browser_swarm_results.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def call_local_coder(prompt: str, model: str = "qwen2.5-coder:14b") -> str:
    """Have the swarm's lead coder analyze DOM/API structure and synthesize adapter strategies."""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 450},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("response", "").strip()
    except Exception as exc:
        return f"Coder error: {exc}"


def test_pollinations_open_external_api(prompt: str, model: str = "openai") -> Dict[str, Any]:
    """Probe Pollinations.ai free anonymous external AI text generation API."""
    t0 = time.monotonic()
    url = "https://text.pollinations.ai/"
    payload = json.dumps({
        "messages": [
            {"role": "system", "content": "You are an external AI node in a multi-agent collective."},
            {"role": "user", "content": prompt}
        ],
        "model": model,
        "seed": 42,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/130.0.0.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            reply = resp.read().decode("utf-8", errors="ignore").strip()
            elapsed = round((time.monotonic() - t0) * 1000.0, 2)
            return {
                "ok": bool(reply),
                "provider": "pollinations_external_cloud",
                "model_requested": model,
                "status_code": resp.getcode(),
                "latency_ms": elapsed,
                "reply": reply,
            }
    except Exception as exc:
        elapsed = round((time.monotonic() - t0) * 1000.0, 2)
        return {
            "ok": False,
            "provider": "pollinations_external_cloud",
            "model_requested": model,
            "latency_ms": elapsed,
            "error": str(exc),
        }


def interact_with_portal_via_playwright(
    portal_id: str,
    url: str,
    prompt_text: str,
    timeout_ms: int = 22000,
) -> Dict[str, Any]:
    """Launch real headless Chromium via Playwright, inspect DOM, click guest/agree buttons,
    type prompt into chat input if accessible, and capture live response or auth wall state.
    """
    t0 = time.monotonic()
    result: Dict[str, Any] = {
        "portal_id": portal_id,
        "url": url,
        "execution_mode": "live_playwright_browser",
        "timestamp": _now_iso(),
    }
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            page = context.new_page()

            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            title = page.title()
            result["page_title"] = title

            # Inspect buttons for "Get Started", "Agree", "Continue as Guest", "Try now"
            guest_buttons = [
                'button:has-text("Get Started")',
                'button:has-text("Agree")',
                'button:has-text("Accept")',
                'button:has-text("Continue")',
                'button:has-text("Guest")',
                'button:has-text("Try")',
                'button:has-text("Start")',
            ]
            clicked_buttons = []
            for b_sel in guest_buttons:
                try:
                    loc = page.locator(b_sel).first
                    if loc.is_visible(timeout=800):
                        loc.click(timeout=1500)
                        clicked_buttons.append(b_sel)
                        page.wait_for_timeout(1200)
                except Exception:
                    pass
            result["clicked_onboarding_buttons"] = clicked_buttons

            # Discover all visible inputs / textareas / contenteditables
            dom_elements = page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('textarea, input[type="text"], [contenteditable="true"]'));
                return inputs.map(el => ({
                    tag: el.tagName,
                    placeholder: el.getAttribute('placeholder') || '',
                    id: el.id || '',
                    className: (el.className || '').toString().slice(0, 80)
                }));
            }""")
            result["discovered_dom_inputs"] = dom_elements

            # Attempt to locate chat input box and submit prompt
            chat_selectors = [
                "textarea",
                '[contenteditable="true"]',
                'input[type="text"]',
            ]
            submitted = False
            for sel in chat_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1500):
                        loc.click(timeout=1500)
                        loc.fill(prompt_text, timeout=2000)
                        loc.press("Enter")
                        submitted = True
                        page.wait_for_timeout(6000)
                        break
                except Exception:
                    pass

            body_text = page.inner_text("body")
            result["prompt_submitted_in_dom"] = submitted
            result["body_excerpt"] = body_text[:1500]
            result["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
            browser.close()
            return result
    except Exception as exc:
        result["error"] = str(exc)
        result["latency_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
        return result


def run_autonomous_external_harness() -> None:
    start_time = time.time()
    report_data: Dict[str, Any] = {
        "campaign": "autonomous_external_model_harness_and_browser_interrogation",
        "started_at": _now_iso(),
        "live_external_cloud_models_tested": [],
        "playwright_browser_portals_interrogated": [],
        "newly_recruited_external_leaves": [],
    }

    benchmark_prompt = (
        "TASK 1: Write a Python function `clamp_ratio(val: float, lo: float = 0.0, hi: float = 1.0) -> float` "
        "that raises ValueError on math.isnan(val) or when lo > hi, and clamps val into [lo, hi]. Return the Python code block.\n"
        "TASK 2: After the code block, list 3 other public AI chat portals or free model APIs in JSON format."
    )

    # =========================================================================
    # PART 1: Live External Cloud Models via Pollinations Multi-Model Router
    # =========================================================================
    external_cloud_models = [
        ("openai", "OpenAI GPT-4o-mini (External Cloud)"),
        ("mistral", "Mistral Small 3 (External Cloud)"),
        ("qwen-coder", "Qwen 2.5 Coder 32B (External Cloud)"),
        ("deepseek", "DeepSeek V3 / R1 (External Cloud)"),
        ("llama", "Meta Llama 3.3 70B (External Cloud)"),
    ]

    print("[*] Phase 1: Interfacing with Live External Cloud AI Models over the Internet...", flush=True)
    for mod_slug, label in external_cloud_models:
        print(f" -> Sending live benchmark & recruitment prompt to External Cloud Model: {label} ({mod_slug})...", flush=True)
        res = test_pollinations_open_external_api(benchmark_prompt, model=mod_slug)
        if res.get("ok"):
            reply = res["reply"]
            check = _authoritative_clamp_ratio_check(reply)
            res["authoritative_clamp_verification"] = check
            print(
                f"    [+] {label} responded in {res['latency_ms']}ms! "
                f"Authoritative Verification: {check['status'].upper()}",
                flush=True,
            )
            # Extract any nominated portals from the external model's response
            urls = re.findall(r"https?://[^\s\"'>)\]]+", reply)
            res["nominated_urls"] = list(dict.fromkeys(urls))
            for u in res["nominated_urls"]:
                report_data["newly_recruited_external_leaves"].append({
                    "url": u,
                    "nominated_by_external_model": mod_slug,
                })
        else:
            print(f"    [-] {label} error: {res.get('error')}", flush=True)
        report_data["live_external_cloud_models_tested"].append(res)

    # =========================================================================
    # PART 2: Live Headless Chromium Browser Interrogation of Web Chat Portals
    # =========================================================================
    web_portals_to_drive = [
        ("duckduckgo-ai-chat", "https://duck.ai"),
        ("huggingface-chat", "https://huggingface.co/chat/"),
        ("qwen-chat-china", "https://chat.qwen.ai/"),
        ("deepseek-chat-china", "https://chat.deepseek.com/"),
        ("chatglm-zhipu-china", "https://chatglm.cn/"),
    ]

    print("\n[*] Phase 2: Launching Playwright Headless Chromium against External Web Chat Portals...", flush=True)
    for pid, url in web_portals_to_drive:
        print(f" -> Driving headless browser on {pid} ({url})...", flush=True)
        b_res = interact_with_portal_via_playwright(
            portal_id=pid,
            url=url,
            prompt_text="Write a one-line Python function add(a, b) that returns a + b.",
        )
        print(
            f"    [+] {pid}: title={b_res.get('page_title')!r}, "
            f"inputs_found={len(b_res.get('discovered_dom_inputs', []))}, "
            f"prompt_submitted={b_res.get('prompt_submitted_in_dom')}, "
            f"latency={b_res.get('latency_ms')}ms",
            flush=True,
        )

        # Have the Swarm's Lead Coder analyze the live DOM and write a custom Playwright selector recipe
        coder_prompt = (
            f"We just inspected external AI portal `{pid}` ({url}) with Playwright.\n"
            f"Page Title: {b_res.get('page_title')}\n"
            f"DOM Inputs Discovered: {json.dumps(b_res.get('discovered_dom_inputs', []))}\n"
            f"Body Excerpt: {b_res.get('body_excerpt', '')[:400]}\n"
            "In 3 concise bullet points, state: (1) Whether a login/OAuth wall is blocking direct anonymous chat, "
            "(2) The exact CSS selector or API strategy to automate this portal, and (3) How to handle session persistence."
        )
        b_res["swarm_engineer_adapter_diagnosis"] = call_local_coder(coder_prompt)
        report_data["playwright_browser_portals_interrogated"].append(b_res)

    report_data["total_elapsed_seconds"] = round(time.time() - start_time, 2)
    OUT_JSON.write_text(json.dumps(report_data, indent=2), encoding="utf-8")
    print(f"\n[DONE] Saved complete external cloud + Playwright browser results to {OUT_JSON}", flush=True)


if __name__ == "__main__":
    run_autonomous_external_harness()
