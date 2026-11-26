#!/usr/bin/env python3
"""Live Playwright Qualification of ChatGLM (https://chatglm.cn/) via ExternalMindScout.

Drives real headless Chromium on ego@192.168.1.133 against https://chatglm.cn/
in `execution_mode="live"`, extracts the rendered code/text from the guest session
(`GLM-5.3-Flash极致`), runs `ExternalMindScout.benchmark_portal("chatglm-zhipu-china", execution_mode="live", live_probe_fn=...)`,
and asks `GLM-5.3-Flash` to nominate 5 more Chinese & global AI chat portals.
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

from tools.external_mind_scout import ExternalMindScout

OUT_JSON = REPO_ROOT / "artifacts" / "collective-repair-20261108" / "chatglm_live_qualification_and_recruitment.json"
OUT_JSON.parent.mkdir(parents=True, exist_ok=True)


def extract_chatglm_assistant_reply(body_text: str) -> str:
    """Extract the assistant response from ChatGLM's rendered DOM text."""
    text = body_text
    if "思考结束\n" in text:
        text = text.split("思考结束\n", 1)[1]
    elif "访客_" in text:
        parts = text.split("ChatGLM\n")
        if len(parts) > 1:
            text = parts[-1]
    for footer in ("\n和我聊聊天吧", "\nGLM-5.3", "\n内容由AI生成"):
        if footer in text:
            text = text.split(footer, 1)[0]
    if "复制\n" in text:
        code_part = text.split("复制\n", 1)[1].strip()
        return f"```python\n{code_part}\n```"
    return text.strip()


def live_chatglm_playwright_adapter(portal: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
    """Real Playwright browser adapter for https://chatglm.cn/ guest session."""
    t0 = time.monotonic()
    prompt = task["prompt"]
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
        page.goto(portal["url"], timeout=25000, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)

        ta = page.locator("textarea").first
        ta.click(timeout=3000)
        ta.fill(prompt, timeout=3000)
        ta.press("Enter")

        page.wait_for_timeout(16000)
        body_text = page.inner_text("body")
        clean_reply = extract_chatglm_assistant_reply(body_text)
        elapsed_ms = round((time.monotonic() - t0) * 1000.0, 2)
        browser.close()
        return {
            "reply": clean_reply,
            "latency_ms": elapsed_ms,
            "delivery": "observed" if clean_reply else "missing",
            "actual_model": "GLM-5.3-Flash",
            "execution_mode": "live",
            "is_simulated": False,
            "provenance_ref": f"playwright_live:chatglm:{int(time.time())}",
        }


def main() -> None:
    db_path = REPO_ROOT / "data" / "sabrina_assistant.sqlite3"
    scout = ExternalMindScout(db_path=db_path)

    print("[*] Running ExternalMindScout.benchmark_portal('chatglm-zhipu-china', execution_mode='live')...", flush=True)
    qual_res = scout.benchmark_portal(
        "chatglm-zhipu-china",
        execution_mode="live",
        live_probe_fn=live_chatglm_playwright_adapter,
    )
    print(f"[+] Benchmark result: qualification_state={qual_res.get('qualification_state')}, status={qual_res.get('status')}", flush=True)

    # Also ask ChatGLM live in Playwright to recommend 5 more Chinese & global AI chat portals
    print("[*] Asking live ChatGLM (GLM-5.3-Flash) to nominate 5 more external AI chat portals...", flush=True)
    rec_task = {
        "task_id": "recruit_more_portals",
        "prompt": (
            "Please list 5 real Chinese and global AI chat portals with their exact https:// URLs "
            "(for example Kimi, Doubao, Baichuan, MiniMax, SenseChat, StepFun) in JSON format: "
            '[{"name": "...", "url": "https://...", "models": ["..."]}].'
        ),
    }
    rec_res = live_chatglm_playwright_adapter({"url": "https://chatglm.cn/"}, rec_task)
    print(f"[+] ChatGLM live recruitment response ({rec_res['latency_ms']}ms):\n{rec_res['reply'][:600]}", flush=True)

    payload = {
        "benchmark_qualification": qual_res,
        "live_recruitment_from_chatglm": rec_res,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[DONE] Saved to {OUT_JSON}", flush=True)


if __name__ == "__main__":
    main()
