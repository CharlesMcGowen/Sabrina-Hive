#!/usr/bin/env python3
"""
Targeted Inspector for Giz.ai, AllenAI OLMo, and LMSYS Arena:
1. Giz.ai: waits up to 28s for "Planning response..." AND "Generating response..." to disappear, and prints the resulting text & `<pre><code>` blocks.
2. AllenAI: checks why `.MuiDialog-root` didn't close (e.g., is there an Osano cookie banner or scroll requirement inside the dialog, or does `form.requestSubmit()` close it?).
3. LMSYS Arena: inspects all textareas/inputs on `https://lmarena.ai/`.
"""
from __future__ import annotations
import json
from playwright.sync_api import sync_playwright


def main():
    prompt = "Write a Python function `clamp_ratio(val: float, lo: float = 0.0, hi: float = 1.0) -> float` that raises ValueError on NaN or when lo > hi, and clamps val into [lo, hi]. Return ONLY a python code block."
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )

        # 1. GIZ.AI with 28s wait for "Planning response..." and "Generating response..."
        print("=== 1. GIZ.AI ===", flush=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto("https://www.giz.ai/assistant", wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(3500)
        loc = page.locator("textarea:visible").first
        loc.click()
        loc.fill(prompt)
        page.wait_for_timeout(300)
        loc.press("Enter")
        for tick in range(28):
            page.wait_for_timeout(1000)
            txt = page.evaluate("() => document.body.innerText")
            if tick >= 4 and "Planning response..." not in txt and "Generating response..." not in txt and "Qwen" in txt:
                page.wait_for_timeout(1500)
                break
        txt = page.evaluate("() => document.body.innerText")
        codes = page.evaluate("() => Array.from(document.querySelectorAll('pre, code')).map(e => e.innerText)")
        print(f"GIZ after {tick+1}s CODES ({len(codes)}):", json.dumps(codes), flush=True)
        print("GIZ BODY:", repr(txt), flush=True)
        ctx.close()

        # 2. ALLENAI OLMO
        print("\n=== 2. ALLENAI OLMO ===", flush=True)
        ctx2 = browser.new_context(viewport={"width": 1280, "height": 900})
        page2 = ctx2.new_page()
        allen_reqs = []
        page2.on(
            "response",
            lambda r: allen_reqs.append({"url": r.url, "status": r.status, "method": r.request.method, "body": r.text()[:800] if r.request.method == "POST" else ""})
            if not any(x in r.url for x in ("plausible", "heap", "google", "osano"))
            else None,
        )
        page2.goto("https://playground.allenai.org/", wait_until="domcontentloaded", timeout=20000)
        page2.wait_for_timeout(3500)
        # Click labels inside MuiDialog-root and click button[type="submit"] inside MuiDialog-root
        res_dlg = page2.evaluate(
            """() => {
                const dlg = document.querySelector('.MuiDialog-root');
                if (!dlg) return 'no_dlg';
                const labels = Array.from(dlg.querySelectorAll('label'));
                labels.forEach(l => l.click());
                const btn = dlg.querySelector('button[type="submit"]');
                if (btn) btn.click();
                return {labels_clicked: labels.length, btn_found: !!btn};
            }"""
        )
        print("ALLENAI JS click result:", res_dlg, flush=True)
        page2.wait_for_timeout(2000)
        still_dlg = page2.evaluate("() => !!document.querySelector('.MuiDialog-root')")
        print("ALLENAI dialog still open?:", still_dlg, flush=True)
        if still_dlg:
            # Check why it's still open! Print full text of dialog
            dlg_text = page2.evaluate("() => document.querySelector('.MuiDialog-root').innerText")
            print("ALLENAI dialog text:", repr(dlg_text), flush=True)
        else:
            ta = page2.locator("textarea").first
            ta.click()
            ta.fill(prompt)
            page2.wait_for_timeout(400)
            ta.press("Enter")
            page2.wait_for_timeout(12000)
            print("ALLENAI REQS:", json.dumps(allen_reqs[-6:], indent=2), flush=True)
            print("ALLENAI BODY:", repr(page2.evaluate("() => document.body.innerText")[-1000:]), flush=True)
        ctx2.close()

        # 3. LMSYS ARENA
        print("\n=== 3. LMSYS ARENA ===", flush=True)
        ctx3 = browser.new_context(viewport={"width": 1280, "height": 900})
        page3 = ctx3.new_page()
        page3.goto("https://lmarena.ai/", wait_until="domcontentloaded", timeout=20000)
        page3.wait_for_timeout(4000)
        els3 = page3.evaluate(
            "() => Array.from(document.querySelectorAll('textarea, input, [contenteditable]')).map(e => ({tag: e.tagName, ph: e.placeholder, vis: e.getBoundingClientRect().width > 0}))"
        )
        print("LMSYS inputs:", els3, flush=True)
        ctx3.close()
        browser.close()


if __name__ == "__main__":
    main()
