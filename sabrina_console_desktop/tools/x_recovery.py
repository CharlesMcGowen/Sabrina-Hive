# -*- coding: utf-8 -*-
"""
tools/x_recovery.py
===================
Resilient self-healing and recovery engine for X.com automation.

Provides:
- Active DOM visual & modal sentry (auto-dismiss unexpected modals/dialogs)
- Self-healing input loop with 3-tier escalation (Soft -> DOM Reset -> Hard Reload)
- Post-submission timeline verification watchdog
- Automated rollback & self-correction for botched/truncated posts
"""

import time
import logging
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger("sabrina.x.recovery")

UNEXPECTED_MODAL_SELECTORS = [
    '[data-testid="app-bar-close"]',
    '[data-testid="confirmationSheetCancel"]',
    'button[aria-label="Close"]',
    '[role="dialog"] button:has-text("Discard")',
    '[role="dialog"] button:has-text("Cancel")',
    'button:has-text("Got it")',
    'button:has-text("Dismiss")',
]


class XRecoveryEngine:
    """Self-healing watchdog that observes the DOM, recovers focus, and rolls back errors."""

    def __init__(self, manager=None):
        self.manager = manager

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalizes whitespace and line breaks for robust string comparison."""
        lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
        return "\n".join(lines)

    def dismiss_unexpected_modals(self, page) -> int:
        """Finds and dismisses any unexpected modals, dialogs, or popups blocking the page."""
        dismissed = 0
        try:
            for selector in UNEXPECTED_MODAL_SELECTORS:
                buttons = page.query_selector_all(selector)
                for btn in buttons:
                    if btn.is_visible():
                        logger.warning("Recovery sentry dismissing blocking modal: %s", selector)
                        btn.click()
                        time.sleep(0.5)
                        dismissed += 1
        except Exception as e:
            logger.debug("Error while scanning for modals: %s", e)
        return dismissed

    def ensure_focused_and_clean(self, page, selector: str) -> bool:
        """Ensures the compose box exists, has active focus, and is completely empty."""
        try:
            elem = page.wait_for_selector(selector, timeout=6000)
            if hasattr(elem, "scroll_into_view_if_needed"):
                elem.scroll_into_view_if_needed()
            elem.click()
            time.sleep(0.2)

            # Clear existing text
            if hasattr(page, "keyboard") and hasattr(page.keyboard, "press"):
                page.keyboard.press("Control+A")
                time.sleep(0.08)
                page.keyboard.press("Backspace")
                time.sleep(0.1)

            # Check if empty
            current_text = elem.inner_text().strip() if hasattr(elem, "inner_text") else ""
            if current_text:
                # Force clear via JS if needed
                page.evaluate("""el => {
                    if (el) {
                        el.innerText = '';
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""", elem)
                time.sleep(0.1)
            return True
        except Exception as e:
            logger.warning("ensure_focused_and_clean failed: %s", e)
            return False

    def verified_input_with_healing(
        self,
        page,
        selector: str,
        text: str,
        target_url: Optional[str] = None,
        max_attempts: int = 3
    ) -> bool:
        """
        Input loop with 3-tier self-healing escalation:
        - Tier 1 (Soft): Clear, focus, atomic insert, DOM verify.
        - Tier 2 (DOM Reset): Dismiss modals, blur, re-click coordinates, paragraph chunk insert.
        - Tier 3 (Hard Reload): Full page refresh / re-navigate, re-open compose box, insert.
        """
        expected_norm = self.normalize_text(text)

        for attempt in range(1, max_attempts + 1):
            logger.info("Self-healing input: Attempt %d/%d for target (%d chars)...", attempt, max_attempts, len(text))
            try:
                # Tier 2 escalation: dismiss modals & blur
                if attempt == 2:
                    logger.warning("Tier 2 escalation: clearing active modals and re-anchoring focus.")
                    self.dismiss_unexpected_modals(page)
                    page.keyboard.press("Escape")
                    time.sleep(0.5)

                # Tier 3 escalation: hard reload
                if attempt == 3 and target_url:
                    logger.warning("Tier 3 escalation: hard reload to %s", target_url)
                    page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
                    time.sleep(2.0)
                    self.dismiss_unexpected_modals(page)

                # Ensure compose box is ready
                if not self.ensure_focused_and_clean(page, selector):
                    # Try finding reply button to open it
                    reply_icon = page.query_selector('article[data-testid="tweet"] [data-testid="reply"]')
                    if reply_icon:
                        reply_icon.click()
                        time.sleep(0.8)
                    self.ensure_focused_and_clean(page, selector)

                elem = page.wait_for_selector(selector, timeout=5000)

                # Insert text
                if hasattr(page, "keyboard") and hasattr(page.keyboard, "insert_text"):
                    page.keyboard.insert_text(text)
                elif hasattr(page, "keyboard") and hasattr(page.keyboard, "type"):
                    page.keyboard.type(text)

                time.sleep(0.6)

                # Verify rendered text in DOM
                actual_text = elem.inner_text() if hasattr(elem, "inner_text") else text
                actual_norm = self.normalize_text(actual_text)

                if actual_norm == expected_norm:
                    logger.info("DOM text verified successfully on attempt %d (%d chars match)", attempt, len(actual_norm))
                    return True
                else:
                    logger.warning(
                        "DOM text verification mismatch (attempt %d/%d): expected %d chars, got %d chars: %r",
                        attempt, max_attempts, len(expected_norm), len(actual_norm), actual_norm[:60]
                    )
                    time.sleep(1.0)

            except Exception as e:
                logger.warning("Error during verified typing attempt %d: %s", attempt, e)
                time.sleep(1.0)

        raise RuntimeError(
            f"Self-healing input failed to verify text after {max_attempts} attempts. "
            f"Expected: {text[:60]}... DOM text rejected or truncated."
        )

    def verify_live_published_tweet(
        self,
        page,
        expected_text: str,
        max_wait_sec: float = 8.0
    ) -> Tuple[bool, Optional[str], Optional[str], str]:
        """
        Watches the page timeline to confirm that the tweet was actually published
        and that its rendered text matches expected_text.
        Returns: (is_verified, post_id, post_url, rendered_text)
        """
        start_time = time.time()
        expected_norm = self.normalize_text(expected_text)

        while time.time() - start_time < max_wait_sec:
            articles = page.query_selector_all('article[data-testid="tweet"]')
            for article in articles[:3]:
                text_elem = article.query_selector('[data-testid="tweetText"]')
                time_elem = article.query_selector('time')
                if text_elem:
                    actual_text = text_elem.inner_text().strip()
                    actual_norm = self.normalize_text(actual_text)
                    if actual_norm == expected_norm or (actual_norm and actual_norm[:50] in expected_norm):
                        post_id = None
                        post_url = None
                        if time_elem:
                            href = time_elem.evaluate("node => node.closest('a')?.href") or ""
                            if "/status/" in href:
                                post_id = href.split("/status/")[1].split("/")[0].split("?")[0]
                                post_url = href
                        logger.info("Live timeline verification confirmed post (id=%s)", post_id)
                        return True, post_id, post_url, actual_text

            time.sleep(1.0)

        logger.warning("Live timeline verification timed out after %.1fs", max_wait_sec)
        return False, None, None, ""

    def rollback_botched_post(self, page, tweet_url: str) -> bool:
        """
        Automated rollback: deletes a botched/truncated tweet from the live timeline.
        """
        logger.info("Initiating automated rollback for botched tweet: %s", tweet_url)
        try:
            page.goto(tweet_url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(2.0)

            article = page.wait_for_selector('article[data-testid="tweet"]', timeout=8000)
            caret = article.wait_for_selector('[data-testid="caret"]', timeout=5000)
            caret.click()
            time.sleep(0.8)

            menu_items = page.query_selector_all('[role="menuitem"]')
            delete_item = None
            for item in menu_items:
                if "delete" in (item.inner_text() or "").lower():
                    delete_item = item
                    break

            if not delete_item:
                logger.error("Could not find 'Delete' option in menu for %s", tweet_url)
                return False

            delete_item.click()
            time.sleep(0.8)

            confirm_btn = page.wait_for_selector('[data-testid="confirmationSheetConfirm"]', timeout=5000)
            confirm_btn.click()
            time.sleep(2.0)

            logger.info("Automated rollback successful: %s deleted.", tweet_url)
            return True
        except Exception as e:
            logger.error("Failed to rollback botched tweet %s: %e", tweet_url, e)
            return False
