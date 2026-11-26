# -*- coding: utf-8 -*-
"""
tools/x_stealth.py
==================
Stealth interaction primitives and checkpoint guards for X.com (Twitter).

Provides:
- Human typing cadence (variable inter-keystroke latency, punctuation pauses)
- Natural viewport scrolling
- Security challenge & authwall detection with circuit-breaker integration
- Session login verification
"""

import time
import random
import logging
from typing import Dict, Any, Optional, Tuple
from tools.x_rate_limiter import XRateLimiter
from tools.x_recovery import XRecoveryEngine

logger = logging.getLogger("sabrina.x.stealth")

CHECKPOINT_URL_PATTERNS = [
    "/account/access",
    "/account/suspended",
    "/i/flow/consent_flow",
    "/i/flow/login",
    "/challenge"
]

CHECKPOINT_TEXT_INDICATORS = [
    "help us keep your account safe",
    "confirm your phone number",
    "your account has been locked",
    "authenticate your account",
    "suspicious login attempt",
    "verify your identity"
]


class XStealthDriver:
    def __init__(self, rate_limiter: Optional[XRateLimiter] = None, recovery: Optional[XRecoveryEngine] = None):
        self.rate_limiter = rate_limiter or XRateLimiter.get_instance()
        self.recovery = recovery or XRecoveryEngine()

    def check_checkpoint(self, page) -> Tuple[bool, str]:
        """Inspects URL and page content for X.com security challenges or verification locks."""
        try:
            current_url = (page.url or "").lower()
            for pattern in CHECKPOINT_URL_PATTERNS:
                if pattern in current_url:
                    reason = f"Security checkpoint detected in URL ({pattern}): {page.url}"
                    self.rate_limiter.trip_circuit_breaker(reason, cooldown_hours=12.0)
                    return True, reason

            # Active challenge / CAPTCHA element check
            challenge_frame = page.query_selector('iframe[src*="arkose"], iframe[src*="funcaptcha"], div[data-testid="challenge"]')
            if challenge_frame:
                reason = "Active challenge / CAPTCHA element detected on page"
                self.rate_limiter.trip_circuit_breaker(reason, cooldown_hours=12.0)
                return True, reason

            visible_text = ""
            try:
                visible_text = (page.inner_text("body") or "").lower()
            except Exception:
                pass
            for text in CHECKPOINT_TEXT_INDICATORS:
                if text in visible_text:
                    reason = f"Security checkpoint text detected on page ('{text}')"
                    self.rate_limiter.trip_circuit_breaker(reason, cooldown_hours=12.0)
                    return True, reason

            return False, "Page clear"
        except Exception as e:
            return False, f"Could not inspect page: {e}"

    def is_logged_in(self, page) -> Tuple[bool, Dict[str, Any]]:
        """Verifies whether the page has an active authenticated X.com session."""
        try:
            url = page.url or ""
            if "x.com" not in url and "twitter.com" not in url:
                return False, {"authenticated": False, "reason": f"Not on X.com (current URL: {url})"}

            is_checkpoint, reason = self.check_checkpoint(page)
            if is_checkpoint:
                return False, {"authenticated": False, "reason": reason, "checkpoint": True}

            # Check for home tab, account switcher, tweet compose textarea, or profile link
            account_btn = page.query_selector('[data-testid="SideNav_AccountSwitcher_Button"]')
            home_link = page.query_selector('[data-testid="AppTabBar_Home_Link"]')
            compose_box = page.query_selector('[data-testid="tweetTextarea_0"]')
            profile_link = page.query_selector('[data-testid="AppTabBar_Profile_Link"]')

            if account_btn or home_link or compose_box or profile_link:
                handle = ""
                display_name = ""
                if account_btn:
                    btn_text = account_btn.inner_text()
                    lines = [line.strip() for line in btn_text.split("\n") if line.strip()]
                    for line in lines:
                        if line.startswith("@"):
                            handle = line
                        elif not display_name:
                            display_name = line

                if not handle and profile_link:
                    href = profile_link.get_attribute("href") or ""
                    clean_handle = href.strip("/").split("/")[0]
                    if clean_handle:
                        handle = "@" + clean_handle

                return True, {
                    "authenticated": True,
                    "handle": handle or "@unknown",
                    "display_name": display_name or "Sabrina User",
                    "url": url
                }

            login_btn = page.query_selector('[data-testid="loginButton"]')
            if login_btn or "/login" in url:
                return False, {"authenticated": False, "reason": "Login button present / logged out"}

            return False, {"authenticated": False, "reason": "No authenticated session markers found"}
        except Exception as e:
            return False, {"authenticated": False, "reason": str(e)}

    def human_type(
        self,
        page,
        selector: str,
        text: str,
        max_retries: int = 3,
        target_url: Optional[str] = None
    ) -> bool:
        """
        Types text with active closed-loop DOM verification and 3-tier self-healing escalation.
        Delegates to XRecoveryEngine to ensure the text in the rendered DOM matches character-for-character.
        Never blindly submits or leaves truncated inputs.
        """
        return self.recovery.verified_input_with_healing(
            page=page,
            selector=selector,
            text=text,
            target_url=target_url,
            max_attempts=max_retries
        )

    def human_scroll(self, page, steps: int = 3, delta_y: int = 400):
        """Natural scrolling with reading pauses."""
        for _ in range(steps):
            jitter = random.randint(-50, 50)
            page.mouse.wheel(0, delta_y + jitter)
            time.sleep(random.uniform(1.0, 2.5))
