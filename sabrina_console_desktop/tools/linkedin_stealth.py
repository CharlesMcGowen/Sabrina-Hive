# -*- coding: utf-8 -*-
"""
tools/linkedin_stealth.py
=========================
Anti-flagging interaction primitives and security checkpoint guard for LinkedIn.

Provides:
- Human typing cadence (variable inter-keystroke latency, punctuation pauses)
- Natural viewport scrolling with reading delays
- Realistic mouse hover & click gestures
- Automatic checkpoint/challenge/CAPTCHA detection with circuit-breaker integration
"""

import time
import random
import logging
from typing import Dict, Any, Optional, Tuple
from tools.linkedin_rate_limiter import LinkedInRateLimiter

logger = logging.getLogger("sabrina.linkedin.stealth")


CHECKPOINT_URL_PATTERNS = [
    "/checkpoint/",
    "/challenge/",
    "/security-check",
    "/uas/consumer-email-challenge",
    "/authwall",
    "captcha"
]

CHECKPOINT_TEXT_INDICATORS = [
    "complete this security verification",
    "quick security check",
    "we noticed unusual activity",
    "unusual activity on your account",
    "enter the 6-digit code",
    "enter the code we sent",
    "restricted your account",
    "temporarily restricted",
]

SHORT_PAGE_CHECKPOINT_INDICATORS = [
    "security verification",
    "verify your identity",
    "unusual activity",
]


class LinkedInStealthDriver:
    def __init__(self, rate_limiter: Optional[LinkedInRateLimiter] = None):
        self.rate_limiter = rate_limiter or LinkedInRateLimiter.get_instance()

    def check_checkpoint(self, page) -> Tuple[bool, str]:
        """
        Inspects URL and page content for LinkedIn security challenges or account restrictions.
        If a checkpoint is found, trips the circuit breaker to prevent repeated strikes.
        """
        try:
            current_url = (page.url or "").lower()
            for pattern in CHECKPOINT_URL_PATTERNS:
                if pattern in current_url:
                    reason = f"Security checkpoint detected in URL ({pattern}): {page.url}"
                    self.rate_limiter.trip_circuit_breaker(reason, cooldown_hours=24.0)
                    return True, reason

            # Inspect page text for restriction warnings (avoid false positives on long cybersecurity feed/job pages)
            try:
                body_text = (page.evaluate("() => document.body ? document.body.innerText.toLowerCase() : ''") or "").strip()
                indicators = list(CHECKPOINT_TEXT_INDICATORS)
                if len(body_text) < 1200:
                    indicators.extend(SHORT_PAGE_CHECKPOINT_INDICATORS)
                for indicator in indicators:
                    if indicator in body_text:
                        reason = f"Security verification or restriction indicator found in page text: '{indicator}'"
                        self.rate_limiter.trip_circuit_breaker(reason, cooldown_hours=24.0)
                        return True, reason
            except Exception:
                pass

            # Check for iframe captchas
            try:
                has_captcha_frame = page.evaluate("""() => {
                    const iframes = Array.from(document.querySelectorAll('iframe'));
                    return iframes.some(f => {
                        if (!/captcha|challenge/i.test(f.src || '')) return false;
                        const rect = f.getBoundingClientRect();
                        const style = getComputedStyle(f);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none'
                            && style.visibility !== 'hidden' && Number(style.opacity) !== 0;
                    });
                }""")
                if has_captcha_frame:
                    reason = "CAPTCHA iframe challenge present on page."
                    self.rate_limiter.trip_circuit_breaker(reason, cooldown_hours=24.0)
                    return True, reason
            except Exception:
                pass

            return False, ""
        except Exception as e:
            return False, f"Error checking checkpoint: {e}"

    def is_logged_in(self, page) -> Tuple[bool, str]:
        """Determines if the current page has an active authenticated LinkedIn session."""
        try:
            url = page.url or ""
            if "linkedin.com/login" in url or "linkedin.com/signup" in url:
                return False, "Browser is on login/signup page."

            # Check for global navigation search bar or profile icon
            logged_in_selectors = [
                "nav.global-nav",
                "#global-nav",
                "button[aria-label*='Me']",
                ".global-nav__me",
                "input[aria-label*='Search']",
                ".feed-identity-module"
            ]
            for sel in logged_in_selectors:
                if page.locator(sel).first.is_visible(timeout=1500):
                    return True, f"Found authenticated UI element '{sel}'"

            # Check cookies for li_at
            cookies = page.context.cookies(["https://www.linkedin.com"])
            has_li_at = any(c.get("name") == "li_at" and bool(c.get("value")) for c in cookies)
            if has_li_at:
                return True, "Valid li_at session cookie found."

            return False, "No active LinkedIn session elements detected."
        except Exception as e:
            return False, f"Session check failed: {e}"

    def safe_navigate(self, page, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> Dict[str, Any]:
        """
        Navigates to URL, applies jitter, and automatically checks for checkpoints.
        """
        # Quota and circuit breaker check
        tripped, cb_reason = self.rate_limiter.is_circuit_breaker_active()
        if tripped:
            return {"status": "error", "circuit_breaker": True, "error": cb_reason}

        self.rate_limiter.wait_jitter("profile_view")

        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            
            # Post-navigation stabilization
            time.sleep(random.uniform(1.2, 2.8))

            # Security checkpoint inspection
            is_cp, cp_msg = self.check_checkpoint(page)
            if is_cp:
                return {
                    "status": "error",
                    "checkpoint_detected": True,
                    "error": f"HALTED BY CHECKPOINT GUARD: {cp_msg}",
                    "url": page.url
                }

            return {
                "status": "success",
                "url": page.url,
                "title": page.title()
            }
        except Exception as e:
            return {"status": "error", "error": f"Navigation failed: {e}"}

    def human_scroll(self, page, min_scrolls: int = 2, max_scrolls: int = 4):
        """
        Simulates natural human reading and browsing with smooth, decelerating viewport scrolls.
        """
        num_scrolls = random.randint(min_scrolls, max_scrolls)
        for _ in range(num_scrolls):
            scroll_delta = random.randint(180, 480)
            # Break the delta into a few micro-steps for smooth movement
            steps = random.randint(3, 6)
            step_delta = scroll_delta // steps
            for _ in range(steps):
                page.mouse.wheel(0, step_delta)
                time.sleep(random.uniform(0.04, 0.09))
            
            # Pause to simulate user reading the content
            time.sleep(random.uniform(1.1, 2.7))

    def human_type(self, page, selector: str, text: str, clear_first: bool = True, timeout: int = 10000) -> Dict[str, Any]:
        """
        Types text with human cadence, variable keystroke delays, and natural micro-pauses
        at punctuation to prevent triggering bot input alarms.
        """
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.scroll_into_view_if_needed(timeout=timeout)
            
            # Realistic hover and click before typing
            box = locator.bounding_box()
            if box:
                target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
                target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
                page.mouse.move(target_x, target_y)
                time.sleep(random.uniform(0.15, 0.35))
                page.mouse.click(target_x, target_y)
            else:
                locator.click()

            time.sleep(random.uniform(0.2, 0.5))

            if clear_first:
                # Clear using Select All + Backspace rather than instant DOM wipe
                page.keyboard.press("Control+A")
                time.sleep(random.uniform(0.08, 0.18))
                page.keyboard.press("Backspace")
                time.sleep(random.uniform(0.12, 0.25))

            # Character by character typing with variable delay
            for char in text:
                page.keyboard.type(char)
                
                # Base inter-keystroke delay (60 - 160 ms)
                delay = random.uniform(0.06, 0.16)
                
                # Extra pause on punctuation / newlines
                if char in ".,!?;":
                    delay += random.uniform(0.25, 0.55)
                elif char == "\n":
                    delay += random.uniform(0.40, 0.85)
                elif char == " ":
                    delay += random.uniform(0.05, 0.15)
                
                # Occasional brief hesitation (3% chance)
                if random.random() < 0.03:
                    delay += random.uniform(0.3, 0.7)

                time.sleep(delay)

            # Post-typing pause
            time.sleep(random.uniform(0.4, 0.9))

            return {"status": "success", "length": len(text)}
        except Exception as e:
            return {"status": "error", "error": f"Human typing failed: {e}"}

    def human_click(self, page, selector: str, timeout: int = 10000) -> Dict[str, Any]:
        """
        Moves mouse to element with realistic velocity and coordinate offset, hovers, then clicks.
        """
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            locator.scroll_into_view_if_needed(timeout=timeout)

            box = locator.bounding_box()
            if box:
                # Add slight random offset from center
                target_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
                target_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)

                # Move mouse in small steps
                page.mouse.move(target_x, target_y)
                time.sleep(random.uniform(0.18, 0.42))
                
                # Click with realistic down/up duration
                page.mouse.down()
                time.sleep(random.uniform(0.06, 0.14))
                page.mouse.up()
            else:
                locator.click(timeout=timeout)

            time.sleep(random.uniform(0.8, 1.8))
            return {"status": "success", "selector": selector}
        except Exception as e:
            return {"status": "error", "error": f"Human click failed: {e}"}
