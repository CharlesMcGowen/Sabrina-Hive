"""One-time Bugcrowd login capture -- RUN THIS ON WINDOWS (a machine with a screen).

Opens a real, visible browser at the Bugcrowd login page. You log in (password + any 2FA), then come
back to this terminal and press Enter. It saves your Bugcrowd session to browser_auth_state.json.
Copy that file to the lab repo root and Sabrina's headless browser is then logged in as you.

    pip install playwright
    python -m playwright install chromium
    python tools/save_bugcrowd_auth.py
    # then: scp browser_auth_state.json ego@192.168.2.69:/home/ego/bot/Sabrina-windows-sync/

Nothing here is sent anywhere -- the session file stays on your machine until you copy it.
"""
import sys

OUT = "browser_auth_state.json"
LOGIN_URL = "https://identity.bugcrowd.com/login"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright isn't installed. Run:\n  pip install playwright\n  python -m playwright install chromium")
    sys.exit(1)

import time

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)  # visible window so you can log in
    context = browser.new_context()
    page = context.new_page()
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    print("\n" + "=" * 70)
    print(">>> Log into Bugcrowd in the browser window that just opened (password + 2FA).")
    print(">>> This will auto-save the moment you're logged in -- nothing else to do here.")
    print("=" * 70)
    # Auto-detect login: poll for a real bugcrowd SESSION cookie while you're off the /login page.
    deadline = time.time() + 300
    saved = False
    while time.time() < deadline:
        try:
            cookies = context.cookies()
            bc = [c for c in cookies if "bugcrowd" in (c.get("domain") or "")]
            cur = page.url
            logged_in = any(("session" in (c.get("name") or "").lower()) or
                            ("_crowdcontrol" in (c.get("name") or "").lower()) for c in bc) \
                        and "/login" not in cur and "/sign" not in cur
            if logged_in:
                context.storage_state(path=OUT)
                print(f"\nLogged in — saved session -> {OUT}  ({len(cookies)} cookies, {len(bc)} bugcrowd)")
                saved = True
                break
        except Exception:
            pass
        time.sleep(2)
    if not saved:
        # Fallback: save whatever we have (you can re-run if it wasn't complete).
        try:
            context.storage_state(path=OUT)
            print(f"\nTimed out waiting for a clear login; saved current state -> {OUT} (re-run if needed).")
        except Exception as e:
            print(f"could not save: {e}")
    browser.close()
print("\nNext: copy it to the lab, e.g.")
print("  scp browser_auth_state.json ego@192.168.2.69:/home/ego/bot/Sabrina-windows-sync/")
