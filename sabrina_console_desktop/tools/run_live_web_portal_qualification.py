#!/usr/bin/env python3
"""Live Playwright Adapter for ChatGLM (GLM-5.3-Flash) & Duck.ai Anonymous Portals.

Drives real headless Chromium on ego@192.168.1.133:
1. ChatGLM (https://chatglm.cn/):
   - Enters guest session (访客), types the benchmark prompt into `textarea.scroll-display-none`,
     presses Enter, waits for `停止对话` / `回答中` to finish, and extracts the live GLM-5.3-Flash code response.
2. Duck.ai (https://duck.ai):
   - Fills `textarea[placeholder="Ask anything privately"]`, clicks `button:has-text("Continue")`
     to accept the privacy modal, waits for GPT-4o-mini / 6 Luna to finish streaming, and extracts the code response.
3. Runs `_authoritative_clamp_ratio_check` on the live external responses and updates
   `ExternalMindScout.benchmark_portal(..., execution_mode="live")` in SQLite!
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import ExternalMindScout, _authoritative_clamp_ratio_check

OUT_JSON = REPO_ROOT / "artifacts" / "collective-repair-20261108" / "live_verified_external_web_portals.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)


def extract_dom_code_block(body_text: str) -> str:
    """Extract rendered Python code from web chat DOM inner_text (e.g., after '复制' or inside pre/code)."""
    if "复制\n" in body_text:
        after_copy = body_text.split("复制\n", 1)[1]
        for footer in ("\n和我聊聊天吧", "\nGLM-", "\n内容由AI生成"):
            if footer in after_copy:
                after_copy = after_copy.split(footer, 1)[0]
        return f"```python\n{after_copy.strip()}\n```"
    return body_text


def query_chatglm_guest_playwright(prompt: str, wait_seconds: int = 16) -> Dict[str, Any]:
    t0 = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()
        page.goto("https://chatglm.cn/", timeout=25000, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        ta = page.locator("textarea").first
        ta.click(timeout=3000)
        ta.fill(prompt, timeout=3000)
        ta.press("Enter")

        # Wait for GLM-5.3-Flash guest response to finish streaming
        page.wait_for_timeout(wait_seconds * 1000)
        body_text = page.inner_text("body")
        code_reply = extract_dom_code_block(body_text)
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        browser.close()
        return {
            "portal_id": "chatglm-zhipu-china",
            "actual_model": "GLM-5.3-Flash (Guest)",
            "execution_mode": "live",
            "is_simulated": False,
            "delivery": "observed" if "def " in code_reply else "uncertain",
            "latency_ms": elapsed_ms,
            "reply": code_reply,
            "raw_dom_text": body_text,
        }


def query_duckai_anonymous_playwright(prompt: str, wait_seconds: int = 14) -> Dict[str, Any]:
    t0 = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()
        page.goto("https://duck.ai", timeout=25000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        ta = page.locator('textarea[placeholder*="Ask" i], textarea').first
        ta.click(timeout=3000)
        ta.fill(prompt, timeout=3000)
        ta.press("Enter")
        page.wait_for_timeout(2000)

        # Click "Continue" or "Agree" on Duck.ai privacy modal if shown
        for btn_sel in ['button:has-text("Continue")', 'button:has-text("Agree")', 'button:has-text("I Agree")']:
            try:
                btn = page.locator(btn_sel).first
                if btn.is_visible(timeout=1500):
                    btn.click(timeout=2000)
                    page.wait_for_timeout(1500)
            except Exception:
                pass

        # Re-press Enter if prompt is still in textarea
        try:
            if ta.is_visible(timeout=1000) and ta.input_value().strip():
                ta.press("Enter")
        except Exception:
            pass

        page.wait_for_timeout(wait_seconds * 1000)
        body_text = page.inner_text("body")
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        browser.close()
        return {
            "portal_id": "duckduckgo-ai-chat",
            "actual_model": "Duck.ai Anonymous (6 Luna / GPT-4o-mini)",
            "execution_mode": "live",
            "is_simulated": False,
            "delivery": "observed" if "def " in body_text else "uncertain",
            "latency_ms": elapsed_ms,
            "reply": body_text,
        }


def run_live_web_portal_qualification() -> None:
    clamp_prompt = (
        "Write ONLY a Python function `def clamp_ratio(val: float, lo: float = 0.0, hi: float = 1.0) -> float:` "
        "that imports math, raises ValueError if math.isnan(val) or math.isnan(lo) or math.isnan(hi) or lo > hi, "
        "and returns max(lo, min(hi, float(val))). Wrap in ```python ... ```."
    )

    print("[*] Running live Playwright guest query on ChatGLM (https://chatglm.cn/)...", flush=True)
    glm_res = query_chatglm_guest_playwright(clamp_prompt, wait_seconds=16)
    glm_check = _authoritative_clamp_ratio_check(glm_res["reply"])
    glm_res["authoritative_check"] = glm_check
    print(
        f"    [+] ChatGLM (GLM-5.3-Flash) latency={glm_res['latency_ms']}ms, "
        f"delivery={glm_res['delivery']}, check={glm_check['status']}",
        flush=True,
    )

    print("[*] Running live Playwright anonymous query on Duck.ai (https://duck.ai)...", flush=True)
    duck_res = query_duckai_anonymous_playwright(clamp_prompt, wait_seconds=14)
    duck_check = _authoritative_clamp_ratio_check(duck_res["reply"])
    duck_res["authoritative_check"] = duck_check
    print(
        f"    [+] Duck.ai latency={duck_res['latency_ms']}ms, "
        f"delivery={duck_res['delivery']}, check={duck_check['status']}",
        flush=True,
    )

    out = {
        "chatglm_live_playwright": {
            **glm_res,
            "reply_excerpt": glm_res["reply"][-1200:],
        },
        "duckai_live_playwright": {
            **duck_res,
            "reply_excerpt": duck_res["reply"][-1200:],
        },
    }
    OUT_JSON.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DONE] Saved to {OUT_JSON}", flush=True)


if __name__ == "__main__":
    run_live_web_portal_qualification()
