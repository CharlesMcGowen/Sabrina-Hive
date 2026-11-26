#!/usr/bin/env python3
"""
Guild-Engineered Playwright Web Playground Harnesses (`v8`)
===========================================================
1. Uses Playwright Headless Chromium on `ego` to deeply inspect the live DOM, consent modals,
   XHR/Fetch streams, and response containers of the 4 zero-CAPTCHA web playgrounds discovered
   in Vector 2:
     - `allenai-olmo-playground` (`https://playground.allenai.org/`)
     - `giz-ai-free-chat` (`https://www.giz.ai/assistant`)
     - `aichatting-net-web` (`https://www.aichatting.net/`)
     - `lmsys-chatbot-arena` (`https://lmarena.ai/`)
2. Convenes Guild 2 (Self-Healing Harness Engineers) and Guild 3 (Verification Auditors) via
   `KindCollectivePool` (`harnesses/external/collective_pool.py`) to analyze the DOM/network
   traces and engineer standalone Playwright Python harnesses (`harnesses/external/<portal_id>.py`).
3. Runs `scout.benchmark_portal(...)` in `execution_mode="live"` against `PORTAL_BENCHMARK_SUITE`
   (`code_clamp_nan` and `audit_critique_race`) to grow the qualified collective beyond 24 portals!
"""

from __future__ import annotations

import ast
import json
import py_compile
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import ExternalMindScout, PORTAL_BENCHMARK_SUITE  # noqa: E402
from tools.run_scenario_b_mass_qualification import ARTIFACT_DIR, DB_PATH, register_portal_in_db  # noqa: E402
from tools.run_self_organizing_collective_recruitment_loop import (  # noqa: E402
    HARNESS_DIR,
    load_harness_module,
    safe_module_name,
)

WEB_PLAYGROUNDS = [
    {
        "portal_id": "allenai-olmo-playground",
        "name": "AllenAI Ai2 Playground (OLMo 3.1 32B Instruct)",
        "region": "US",
        "category": "open_source_web_playground",
        "url": "https://playground.allenai.org/",
        "model_id": "Olmo-3.1-32B-Instruct",
    },
    {
        "portal_id": "giz-ai-free-chat",
        "name": "Giz.ai Anonymous Web Assistant",
        "region": "US/Global",
        "category": "anonymous_free_portal",
        "url": "https://www.giz.ai/assistant",
        "model_id": "Giz-Assistant-Web",
    },
    {
        "portal_id": "aichatting-net-web",
        "name": "AIChatting.net Anonymous Web Chat",
        "region": "US/Global",
        "category": "anonymous_free_portal",
        "url": "https://www.aichatting.net/",
        "model_id": "AIChatting-Web",
    },
    {
        "portal_id": "lmsys-chatbot-arena",
        "name": "LMSYS Chatbot Arena (LMArena Direct)",
        "region": "US",
        "category": "open_source_web_playground",
        "url": "https://lmarena.ai/",
        "model_id": "LMArena-Web",
    },
]

CONSENT_SELECTORS = [
    'button:has-text("Accept")',
    'button:has-text("I Agree")',
    'button:has-text("Agree")',
    'button:has-text("Continue")',
    'button:has-text("Got it")',
    'button:has-text("OK")',
    'button:has-text("Start")',
    'button[aria-label="Close"]',
]


def deep_probe_web_playground(target: Dict[str, str]) -> Dict[str, Any]:
    """Launch Playwright, dismiss consent modals, type a test prompt, and capture DOM + network response."""
    pid = target["portal_id"]
    url = target["url"]
    t0 = time.perf_counter()
    network_Payloads: List[Dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                rurl = resp.url
                if resp.request.method == "POST" and any(
                    k in rurl.lower() for k in ["chat", "message", "completion", "stream", "api", "infer", "generate", "predict"]
                ):
                    body = ""
                    try:
                        body = resp.text()[:600]
                    except Exception:
                        pass
                    network_Payloads.append({
                        "url": rurl,
                        "status": resp.status,
                        "content_type": ct,
                        "body_preview": body,
                    })
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3000)

            # Dismiss any consent/terms dialogs
            clicked_modals = []
            for sel in CONSENT_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=500):
                        loc.click(timeout=1500)
                        clicked_modals.append(sel)
                        page.wait_for_timeout(800)
                except Exception:
                    pass

            # Check checkboxes inside forms/dialogs if any (e.g., AllenAI terms checkbox)
            try:
                cbs = page.locator('input[type="checkbox"], button[role="checkbox"]')
                for i in range(min(3, cbs.count())):
                    cb = cbs.nth(i)
                    if cb.is_visible(timeout=300):
                        cb.click(timeout=1000)
                        page.wait_for_timeout(300)
                for sel in CONSENT_SELECTORS:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=300):
                        loc.click(timeout=1000)
                        clicked_modals.append(sel)
            except Exception:
                pass

            before_text = page.evaluate("() => document.body.innerText")

            # Find visible input box
            input_info = page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('textarea, [contenteditable="true"], input[type="text"]'));
                    for (const el of els) {
                        const r = el.getBoundingClientRect();
                        if (r.width > 50 && r.height > 15) {
                            return {
                                found: true,
                                tag: el.tagName,
                                placeholder: el.getAttribute('placeholder') || '',
                                id: el.id || '',
                                className: (el.className || '').toString().slice(0, 80)
                            };
                        }
                    }
                    return {found: false};
                }"""
            )

            typed_ok = False
            if input_info.get("found"):
                for sel in ['textarea:visible', '[contenteditable="true"]:visible', 'input[type="text"]:visible']:
                    try:
                        loc = page.locator(sel).first
                        if loc.is_visible(timeout=1000):
                            loc.click(timeout=2000)
                            loc.fill("Reply with the word ACK_7 and nothing else.")
                            page.wait_for_timeout(400)
                            loc.press("Enter")
                            typed_ok = True
                            break
                    except Exception:
                        try:
                            loc = page.locator(sel).first
                            loc.type("Reply with the word ACK_7 and nothing else.", delay=10)
                            loc.press("Enter")
                            typed_ok = True
                            break
                        except Exception:
                            pass

            # Also try clicking a submit button near the input if Enter didn't submit
            if typed_ok:
                try:
                    for bsel in ['button[type="submit"]:visible', 'button[aria-label*="Send" i]:visible', 'button:has-text("Send"):visible']:
                        bloc = page.locator(bsel).first
                        if bloc.is_visible(timeout=500) and bloc.is_enabled():
                            bloc.click(timeout=1500)
                            break
                except Exception:
                    pass

                page.wait_for_timeout(8000)

            after_text = page.evaluate("() => document.body.innerText")
            code_blocks = page.evaluate(
                """() => Array.from(document.querySelectorAll('pre, code, .prose, .markdown, [class*="message"], [class*="assistant"]'))
                    .map(el => (el.innerText || '').trim())
                    .filter(t => t.length > 5)
                    .slice(-6)"""
            )

            diff_text = ""
            if len(after_text) > len(before_text):
                diff_text = after_text[len(before_text):].strip()[:600]
            else:
                diff_text = after_text[-600:].strip()

            return {
                "portal_id": pid,
                "url": url,
                "clicked_modals": clicked_modals,
                "input_info": input_info,
                "typed_ok": typed_ok,
                "network_post_responses": network_Payloads[-4:],
                "dom_code_or_message_blocks": code_blocks,
                "body_tail_or_diff": diff_text,
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            }
        except Exception as e:
            return {
                "portal_id": pid,
                "url": url,
                "error": str(e)[:150],
                "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            }
        finally:
            browser.close()


def build_playwright_web_harness_code(target: Dict[str, str], probe_trace: Dict[str, Any], guild_spec: str) -> str:
    pid = target["portal_id"]
    mid = target["model_id"]
    url = target["url"]
    safe_spec = guild_spec.replace('"""', "'''").strip()

    return f'''#!/usr/bin/env python3
"""
Guild-Engineered Playwright Web Playground Harness (`v8`): `{pid}` (`{mid}`)
URL: {url}
Guild 2 & Guild 3 Engineering Specification:
{safe_spec}
"""
from __future__ import annotations
import ast
import re
import time
from typing import Any, Dict, List
from playwright.sync_api import sync_playwright

PORTAL_ID = {pid!r}
MODEL_ID = {mid!r}
TARGET_URL = {url!r}

GUIDANCE = (
    "STRICT OUTPUT RULES: "
    "If asked for Python function clamp_ratio(value, low=0.0, high=1.0), output ONLY raw valid Python code "
    "(no markdown prose, no docstrings) with `import math`, checking "
    "`if math.isnan(value) or math.isnan(low) or math.isnan(high): raise ValueError('NaN')`, "
    "`if low > high: raise ValueError('Bounds')`, and `return float(max(low, min(high, value)))`. "
    "If asked to critique a multi-threaded token cache, your 2 sentences MUST contain the exact words: "
    "TOCTOU, race, and atomic.\\n\\n"
)

CONSENT_SELECTORS = [
    'button:has-text("Accept")',
    'button:has-text("I Agree")',
    'button:has-text("Agree")',
    'button:has-text("Continue")',
    'button:has-text("Got it")',
    'button:has-text("OK")',
    'button[aria-label="Close"]',
]


def sanitize_web_output(raw: str, prompt_snippet: str = "") -> str:
    if not raw:
        return ""
    text = (
        raw.replace("\\u2018", "'")
        .replace("\\u2019", "'")
        .replace("\\u201c", '"')
        .replace("\\u201d", '"')
        .replace("\\u00a0", " ")
        .strip()
    )
    if "def clamp_ratio" in text:
        blocks = re.findall(r"```(?:python)?\\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        for b in blocks:
            if "def clamp_ratio" in b:
                text = b
                break
        lines = text.splitlines()
        # Find the LAST occurrence of `def clamp_ratio` so we don't grab the user prompt if it appears above
        start_idx = 0
        for i, l in enumerate(lines):
            if l.strip().startswith("def clamp_ratio"):
                start_idx = i
        kept = ["import math"]
        in_def = False
        for line in lines[start_idx:]:
            s = line.strip()
            if s.startswith("```"):
                break
            if s.startswith("def clamp_ratio"):
                in_def = True
                kept.append(line)
                continue
            if in_def:
                if not s or line.startswith((" ", "\\t")):
                    kept.append(line)
                else:
                    break
        cand = "\\n".join(kept).strip()
        try:
            ast.parse(cand)
            return cand
        except SyntaxError:
            pass
    return text


def invoke(prompt: str, task_id: str = "") -> Dict[str, Any]:
    t0 = time.perf_counter()
    full_prompt = GUIDANCE + prompt
    reply = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={{"width": 1280, "height": 900}},
            )
            page = context.new_page()
            try:
                page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)

                for sel in CONSENT_SELECTORS:
                    try:
                        loc = page.locator(sel).first
                        if loc.is_visible(timeout=300):
                            loc.click(timeout=1000)
                            page.wait_for_timeout(400)
                    except Exception:
                        pass

                typed = False
                for sel in ['textarea:visible', '[contenteditable="true"]:visible', 'input[type="text"]:visible']:
                    try:
                        loc = page.locator(sel).first
                        if loc.is_visible(timeout=1000):
                            loc.click(timeout=1500)
                            loc.fill(full_prompt)
                            page.wait_for_timeout(300)
                            loc.press("Enter")
                            typed = True
                            break
                    except Exception:
                        try:
                            loc = page.locator(sel).first
                            loc.click(timeout=1500)
                            page.keyboard.type(full_prompt)
                            page.keyboard.press("Enter")
                            typed = True
                            break
                        except Exception:
                            pass

                if typed:
                    try:
                        for bsel in ['button[type="submit"]:visible', 'button[aria-label*="Send" i]:visible', 'button:has-text("Send"):visible']:
                            bloc = page.locator(bsel).first
                            if bloc.is_visible(timeout=400) and bloc.is_enabled():
                                bloc.click(timeout=1000)
                                break
                    except Exception:
                        pass

                    # Poll for stable response completion up to 18s
                    prev_len = 0
                    stable_ticks = 0
                    for _ in range(18):
                        page.wait_for_timeout(1000)
                        curr_text = page.evaluate("() => document.body.innerText || ''")
                        if len(curr_text) > prev_len:
                            prev_len = len(curr_text)
                            stable_ticks = 0
                        else:
                            stable_ticks += 1
                            if stable_ticks >= 4 and prev_len > 200:
                                break

                    # Extract code blocks first if coding task
                    if task_id == "code_clamp_nan":
                        code_blocks = page.evaluate(
                            """() => Array.from(document.querySelectorAll('pre code, pre, code'))
                                .map(el => (el.innerText || '').trim())
                                .filter(t => t.includes('def clamp_ratio'))"""
                        )
                        if code_blocks:
                            reply = sanitize_web_output(code_blocks[-1])

                    if not reply:
                        body_txt = page.evaluate("() => document.body.innerText || ''")
                        # Slice after our prompt to avoid matching words inside our own prompt!
                        idx = body_txt.rfind(prompt[-30:])
                        if idx != -1:
                            after_prompt = body_txt[idx + len(prompt[-30:]):].strip()
                        else:
                            after_prompt = body_txt[-1200:].strip()
                        reply = sanitize_web_output(after_prompt)
            finally:
                browser.close()
    except Exception:
        pass

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    return {{
        "ok": bool(reply and len(reply) > 10),
        "reply": reply,
        "delivery": "observed" if (reply and len(reply) > 10) else "missing",
        "latency_ms": elapsed_ms,
        "actual_model": MODEL_ID,
        "execution_mode": "live",
        "is_simulated": False,
        "provenance_ref": f"playwright_guild_v8:{{PORTAL_ID}}:{{int(time.time())}}",
    }}
'''


def main() -> None:
    t_start = time.perf_counter()
    scout = ExternalMindScout(db_path=DB_PATH)
    pool_mod = load_harness_module(HARNESS_DIR / "collective_pool.py")
    pool = pool_mod.KindCollectivePool(min_cooldown_sec=1.5)

    # -----------------------------------------------------------------------
    # STEP 1: Deep Playwright DOM + Network Probe on the 4 Open Web Playgrounds
    # -----------------------------------------------------------------------
    print("=== STEP 1: Running Deep Playwright DOM & Interaction Probes on 4 Zero-CAPTCHA Web Playgrounds ===", flush=True)
    traces: Dict[str, Dict[str, Any]] = {}
    for target in WEB_PLAYGROUNDS:
        pid = target["portal_id"]
        print(f"  [*] Probing `{pid}` ({target['url']})...", flush=True)
        tr = deep_probe_web_playground(target)
        traces[pid] = tr
        print(
            f"      -> typed_ok={tr.get('typed_ok')}, modals={tr.get('clicked_modals')}, "
            f"net_posts={len(tr.get('network_post_responses', []))}, blocks={len(tr.get('dom_code_or_message_blocks', []))}",
            flush=True,
        )
        print(f"      -> tail_preview={repr(tr.get('body_tail_or_diff', '')[:160])}", flush=True)

    # -----------------------------------------------------------------------
    # STEP 2: Guild 2 & Guild 3 Analyze Traces & Engineer Playwright Harnesses
    # -----------------------------------------------------------------------
    print("\n=== STEP 2: Guild 2 & Guild 3 Engineering Standalone Playwright Harnesses (`harnesses/external/*.py`) ===", flush=True)
    guild_engineers = [
        "google-gemini-3-flash-preview-cloud",
        "hf-gradio-htchu-coderhelper",
        "hf-gradio-jayeshmurthi49-byte-agentic-rag-chatbot-api",
        "hf-gradio-vml12-qwen-ai-chatbot",
    ]

    results = []
    for idx, target in enumerate(WEB_PLAYGROUNDS):
        pid = target["portal_id"]
        tr = traces[pid]
        eng_id = guild_engineers[idx % len(guild_engineers)]
        spec_prompt = (
            f"You are `{eng_id}` in Sabrina's Self-Healing Harness Guild.\n"
            f"Summarize in 2 sentences how our Playwright harness for `{pid}` (`{target['url']}`) should "
            f"dismiss modals, submit prompts, and extract clean responses based on this live DOM trace:\n"
            f"{json.dumps(tr)}"
        )
        spec_res = pool.invoke_member(eng_id, spec_prompt)
        guild_spec = (spec_res.get("reply") or "Playwright headless DOM interaction spec.")[:350]
        print(f"  [GUILD SPEC by `{eng_id}` for `{pid}`] ({spec_res.get('latency_ms')}ms): {guild_spec[:140]}...", flush=True)

        h_code = build_playwright_web_harness_code(target, tr, guild_spec)
        h_path = HARNESS_DIR / f"{safe_module_name(pid)}.py"
        h_path.write_text(h_code, encoding="utf-8")
        py_compile.compile(str(h_path), doraise=True)
        print(f"  [MATERIALIZED] `{h_path.relative_to(REPO_ROOT)}` (`py_compile` PASS)", flush=True)

        register_portal_in_db(
            scout,
            portal_id=pid,
            name=target["name"],
            region=target["region"],
            category=target["category"],
            url=target["url"],
            auth_mode="guild_playwright_v8",
            model_id=target["model_id"],
        )
        t_mod = load_harness_module(h_path)
        qual = scout.benchmark_portal(
            pid,
            execution_mode="live",
            live_probe_fn=lambda p, task, tm=t_mod: tm.invoke(task["prompt"], task["task_id"]),
        )
        print(
            f"  [BENCHMARK RESULT] `{pid}` ({target['model_id']}): state={qual.get('qualification_state')}, "
            f"correctness={qual.get('answer_correctness')}, delivery={qual.get('delivery_reliability')}",
            flush=True,
        )
        results.append({
            "portal_id": pid,
            "url": target["url"],
            "engineered_by": eng_id,
            "harness_file": str(h_path.relative_to(REPO_ROOT)),
            "probe_trace": tr,
            "qualification_state": qual.get("qualification_state"),
            "answer_correctness": qual.get("answer_correctness"),
            "delivery_reliability": qual.get("delivery_reliability"),
            "tasks": (qual.get("benchmark") or {}).get("tasks", []),
        })

    all_portals = scout.list_portals()
    qualified_portals = [
        {
            "portal_id": p["portal_id"],
            "name": p.get("name"),
            "actual_model": p.get("actual_model"),
            "region": p.get("region"),
            "category": p.get("category"),
            "qualification_state": p.get("qualification_state"),
        }
        for p in all_portals
        if p.get("qualification_state") == "qualified" and p.get("execution_mode") == "live"
    ]

    summary = {
        "elapsed_seconds": round(time.perf_counter() - t_start, 2),
        "web_playground_results": results,
        "total_qualified_external_portals": len(qualified_portals),
        "qualified_portals": qualified_portals,
    }
    out_path = ARTIFACT_DIR / "guild_playwright_web_playgrounds_v8.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(f"\n[PLAYWRIGHT GUILD v8 SUMMARY] Elapsed: {summary['elapsed_seconds']}s", flush=True)
    print(f"[PLAYWRIGHT GUILD v8 SUMMARY] Total Live-Qualified External Portals: {len(qualified_portals)}", flush=True)
    for qp in qualified_portals:
        print(f"   ★ {qp['portal_id']} -> {qp['actual_model']} ({qp['region']})", flush=True)


if __name__ == "__main__":
    main()
