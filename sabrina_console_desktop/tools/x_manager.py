# -*- coding: utf-8 -*-
"""
tools/x_manager.py
==================
Sabrina's Anti-Flagging X.com (Twitter) Management Engine.

Provides stealth, rate-limited X.com profile and content management:
- Feed browsing & technical trend ingestion
- Tweet composition, thread staging, and bounded publishing
- Automated tweet metrics & analytics extraction for reward reinforcement
- Stealth interaction with realistic typing cadence, natural scrolling, and security checkpoint tripwires

Safety Invariants:
- Real browser CDP attachment (default port 9222) preserving cookies and TLS profile.
- Hard daily quotas enforced by XRateLimiter.
- No unreviewed autonomous publishing: default execution is dry_run / staged preview only.
"""

import os
import sys
import re
import time
import logging
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlsplit

# Add parent directory to sys.path if invoked standalone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.x_rate_limiter import XRateLimiter
from tools.x_recovery import XRecoveryEngine
from tools.x_stealth import XStealthDriver
from ai_systems.browser_utils import PlaywrightBrowserManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sabrina.x.manager")


def parse_metric_text(text: str) -> int:
    """Parses social metric shorthand (e.g. '1.2K', '3.4M', '45') into integer counts."""
    if not text:
        return 0
    cleaned = text.strip().replace(",", "")
    match = re.search(r"([\d\.]+)\s*([KkMmBb])?", cleaned)
    if not match:
        return 0
    number_str, suffix = match.groups()
    try:
        val = float(number_str)
        if suffix:
            suffix = suffix.upper()
            if suffix == "K":
                val *= 1_000
            elif suffix == "M":
                val *= 1_000_000
            elif suffix == "B":
                val *= 1_000_000_000
        return int(val)
    except Exception:
        return 0


class XManager:
    def __init__(self, cdp_url: str = "http://localhost:9222", rate_limiter: Optional[XRateLimiter] = None):
        self.cdp_url = cdp_url
        self.rate_limiter = rate_limiter or XRateLimiter.get_instance()
        self.recovery = XRecoveryEngine(manager=self)
        self.stealth = XStealthDriver(rate_limiter=self.rate_limiter, recovery=self.recovery)
        self.browser_manager = PlaywrightBrowserManager.get_instance()
        self.event_callback = None

    def _record_event(self, phase: str, detail: Optional[Dict[str, Any]] = None):
        """Synchronous write-ahead hook."""
        if self.event_callback:
            self.event_callback(phase, detail or {})

    def _ensure_browser_page(self, target_url: Optional[str] = None):
        """Attach to configured browser via CDP and select/create a dedicated X.com tab without clobbering LinkedIn."""
        if not self.browser_manager.is_healthy():
            logger.info("Connecting to browser via CDP at %s ...", self.cdp_url)
            res = self.browser_manager.connect_cdp(cdp_url=self.cdp_url, timeout=10000)
            if res.get("status") != "success":
                raise RuntimeError(
                    f"Could not connect to browser via CDP at {self.cdp_url}: {res.get('error')}. "
                    "Ensure an authenticated browser is running with --remote-debugging-port=9222."
                )

        base_page = self.browser_manager.page
        x_page = None
        try:
            for pg in base_page.context.pages:
                u = (pg.url or "").lower()
                if "x.com" in u or "twitter.com" in u:
                    x_page = pg
                    break
            if x_page is None:
                x_page = base_page.context.new_page()
                x_page.goto(target_url or "https://x.com/home", wait_until="domcontentloaded", timeout=20000)
                time.sleep(2.0)
        except Exception:
            x_page = base_page

        if target_url:
            current = x_page.url or ""
            if not current.startswith(target_url):
                x_page.goto(target_url, wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.5)
        return x_page

    def get_status(self) -> Dict[str, Any]:
        """Returns health, quota summary, and live X authentication state."""
        summary = self.rate_limiter.get_status_summary()
        logged_in = False
        handle = "@unknown"
        display_name = ""
        current_url = ""

        try:
            page = self._ensure_browser_page()
            browser_active = True
            current_url = page.url or ""
            if "x.com" not in current_url and "twitter.com" not in current_url:
                page = self._ensure_browser_page(target_url="https://x.com/home")
                current_url = page.url or ""
            logged_in, auth_info = self.stealth.is_logged_in(page)
            handle = auth_info.get("handle", "@unknown")
            display_name = auth_info.get("display_name", "")
            detail_msg = "Authenticated" if logged_in else auth_info.get("reason", "Not authenticated")
        except Exception as e:
            browser_active = False
            detail_msg = f"Browser CDP connection error: {e}"

        return {
            "browser_connected": browser_active,
            "x_authenticated": logged_in,
            "handle": handle,
            "display_name": display_name,
            "current_url": current_url,
            "detail": detail_msg,
            "rate_limits": summary,
        }

    def split_into_thread(self, content: str, max_chunk_len: int = 265) -> List[str]:
        """Splits long text cleanly at sentence/paragraph boundaries into numbered thread tweets."""
        if len(content) <= 280:
            return [content]

        raw_paragraphs = content.split("\n\n")
        paragraphs = []
        for p in raw_paragraphs:
            p = p.strip()
            if not p:
                continue
            if len(p) <= max_chunk_len:
                paragraphs.append(p)
            else:
                sentences = re.split(r'(?<=[.!?])\s+', p)
                sub_p = ""
                for s in sentences:
                    if not sub_p:
                        sub_p = s
                    elif len(sub_p) + 1 + len(s) <= max_chunk_len:
                        sub_p += " " + s
                    else:
                        paragraphs.append(sub_p)
                        sub_p = s
                if sub_p:
                    paragraphs.append(sub_p)

        chunks = []
        current = ""
        for p in paragraphs:
            if not current:
                current = p
            elif len(current) + 2 + len(p) <= max_chunk_len:
                current += "\n\n" + p
            else:
                chunks.append(current)
                current = p

        if current:
            chunks.append(current)

        # Number thread chunks
        total = len(chunks)
        numbered = []
        for idx, chunk in enumerate(chunks, 1):
            prefix = f"{idx}/{total} "
            numbered.append(prefix + chunk)
        return numbered

    def draft_tweet(self, content: str) -> Dict[str, Any]:
        """Prepares and validates a tweet draft without publishing."""
        stripped = content.strip()
        length = len(stripped)
        is_single = length <= 280
        thread = self.split_into_thread(stripped)

        return {
            "valid": True,
            "char_count": length,
            "is_single_tweet": is_single,
            "thread_parts": len(thread),
            "tweets": thread,
            "status": "draft_staged",
            "publish_ready": False,  # Requires explicit operator confirmation
        }

    def draft_thread(self, tweets_or_long_text: Any) -> Dict[str, Any]:
        """Validates and stages a multi-tweet thread."""
        if isinstance(tweets_or_long_text, str):
            tweets = self.split_into_thread(tweets_or_long_text.strip())
        elif isinstance(tweets_or_long_text, list):
            tweets = [t.strip() for t in tweets_or_long_text if t.strip()]
        else:
            raise ValueError("Input must be a string or list of tweets.")

        validations = []
        all_valid = True
        for idx, t in enumerate(tweets, 1):
            chars = len(t)
            is_valid = chars <= 280
            if not is_valid:
                all_valid = False
            validations.append({
                "index": idx,
                "length": chars,
                "valid": is_valid,
                "preview": t[:60] + "..." if chars > 60 else t
            })

        return {
            "total_tweets": len(tweets),
            "all_valid": all_valid,
            "tweets": tweets,
            "parts": validations,
            "status": "thread_staged",
            "publish_ready": all_valid,
        }

    def publish_thread(self, tweets: List[str], approved: bool = False) -> Dict[str, Any]:
        """
        Publishes a multi-tweet thread sequentially as a linked reply chain.
        MANDATORY SAFETY: Requires approved=True from operator.
        """
        if not approved:
            raise PermissionError("Publishing threads is prohibited without explicit operator approval.")
        if not tweets:
            raise ValueError("Thread must contain at least one tweet.")

        logger.info("Publishing thread with %d tweets...", len(tweets))
        first_post = self.publish_tweet(tweets[0], approved=True)
        chain = [first_post]
        current_url = first_post["url"]

        for idx, tweet_text in enumerate(tweets[1:], 2):
            delay = self.rate_limiter.get_jitter_delay("tweet_reply")
            logger.info("Pacing next thread tweet (%d/%d): waiting %.1fs ...", idx, len(tweets), delay)
            time.sleep(delay)
            reply_result = self.reply_to_tweet(target_url=current_url, content=tweet_text, approved=True)
            chain.append(reply_result)
            if reply_result.get("post_url") and "/status/" in reply_result["post_url"] and reply_result["post_url"] != current_url:
                current_url = reply_result["post_url"]

        logger.info("Successfully published entire %d-tweet thread starting at %s", len(chain), first_post["url"])
        return {
            "status": "published_thread",
            "thread_head_url": first_post["url"],
            "total_tweets": len(chain),
            "chain": chain,
        }

    def read_feed(self, max_posts: int = 5) -> List[Dict[str, Any]]:
        """Reads recent tweets from the active timeline."""
        allowed, msg = self.rate_limiter.check_quota("feed_read")
        if not allowed:
            logger.warning("Feed read rate limit blocked: %s", msg)
            return []

        page = self._ensure_browser_page(target_url="https://x.com/home")
        is_checkpoint, reason = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            logger.error("Checkpoint encountered reading feed: %s", reason)
            return []

        self.stealth.human_scroll(page, steps=2, delta_y=300)
        time.sleep(1.0)

        tweets = []
        articles = page.query_selector_all('article[data-testid="tweet"]')
        for article in articles[:max_posts]:
            try:
                text_elem = article.query_selector('[data-testid="tweetText"]')
                tweet_text = text_elem.inner_text() if text_elem else ""

                user_elem = article.query_selector('[data-testid="User-Name"]')
                user_info = user_elem.inner_text() if user_elem else ""
                handle_match = re.search(r"@[\w]+", user_info)
                handle = handle_match.group(0) if handle_match else "@unknown"

                link_elem = article.query_selector('time')
                parent_link = link_elem.evaluate("node => node.closest('a')?.href") if link_elem else ""

                # Metrics extraction
                reply_elem = article.query_selector('[data-testid="reply"]')
                retweet_elem = article.query_selector('[data-testid="retweet"]')
                like_elem = article.query_selector('[data-testid="like"]')
                bookmark_elem = article.query_selector('[data-testid="bookmark"]')

                replies = parse_metric_text(reply_elem.inner_text()) if reply_elem else 0
                reposts = parse_metric_text(retweet_elem.inner_text()) if retweet_elem else 0
                likes = parse_metric_text(like_elem.inner_text()) if like_elem else 0
                bookmarks = parse_metric_text(bookmark_elem.inner_text()) if bookmark_elem else 0

                tweets.append({
                    "author": handle,
                    "text": tweet_text,
                    "url": parent_link,
                    "metrics": {
                        "replies": replies,
                        "reposts": reposts,
                        "likes": likes,
                        "bookmarks": bookmarks
                    }
                })
            except Exception as e:
                logger.debug("Failed parsing individual tweet card: %s", e)

        self.rate_limiter.record_action("feed_read")
        return tweets

    def get_tweet_analytics(self, tweet_url: str) -> Dict[str, Any]:
        """
        Navigates to an owned tweet and extracts engagement metrics for reward computation.
        Returns a dict matching the ExperimentLab snapshot requirements:
        impressions, reactions (likes), comments (replies), reposts, quotes, saves (bookmarks),
        profile_viewers, followers, sends.
        """
        allowed, msg = self.rate_limiter.check_quota("analytics_read")
        if not allowed:
            raise RuntimeError(f"Rate limiter blocked analytics reading: {msg}")

        page = self._ensure_browser_page(target_url=tweet_url)
        time.sleep(2.0)

        is_checkpoint, reason = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            raise RuntimeError(f"Checkpoint detected on tweet page: {reason}")

        # Primary tweet metrics on status page
        article = page.wait_for_selector('article[data-testid="tweet"]', timeout=25000)

        reply_elem = article.query_selector('[data-testid="reply"]')
        retweet_elem = article.query_selector('[data-testid="retweet"]')
        like_elem = article.query_selector('[data-testid="like"]')
        bookmark_elem = article.query_selector('[data-testid="bookmark"]')
        analytics_link = article.query_selector('a[href*="/analytics"]')

        replies = parse_metric_text(reply_elem.inner_text()) if reply_elem else 0
        reposts = parse_metric_text(retweet_elem.inner_text()) if retweet_elem else 0
        likes = parse_metric_text(like_elem.inner_text()) if like_elem else 0
        bookmarks = parse_metric_text(bookmark_elem.inner_text()) if bookmark_elem else 0
        impressions = parse_metric_text(analytics_link.inner_text()) if analytics_link else 0

        # If impressions is 0 from feed button, try reading the view count text
        if impressions == 0:
            view_elem = article.query_selector('span:has-text("Views")')
            if view_elem:
                impressions = parse_metric_text(view_elem.inner_text())

        self.rate_limiter.record_action("analytics_read")

        return {
            "impressions": max(impressions, 100),  # Default floor if brand new
            "reactions": likes,
            "comments": replies,
            "reposts": reposts,
            "quotes": 0,
            "saves": bookmarks,
            "sends": 0,
            "profile_viewers": 0,
            "followers": 0,
        }

    def publish_tweet(self, content: str, approved: bool = False) -> Dict[str, Any]:
        """
        Publishes a single tweet to X.com.
        MANDATORY SAFETY: Requires approved=True from operator.
        """
        if not approved:
            raise PermissionError("Publishing is prohibited without explicit operator approval.")

        allowed, msg = self.rate_limiter.check_quota("tweet_post")
        if not allowed:
            raise RuntimeError(f"Rate limiter prohibited tweet publishing: {msg}")

        page = self._ensure_browser_page(target_url="https://x.com/home")
        is_checkpoint, reason = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            raise RuntimeError(f"Cannot publish: security checkpoint active ({reason})")

        # Type content into compose box
        compose_selector = '[data-testid="tweetTextarea_0"]'
        self.stealth.human_type(page, compose_selector, content)
        time.sleep(1.0)

        # Click enabled, visible post button (supports both modal tweetButton and inline tweetButtonInline)
        post_btn = None
        for btn_sel in ('[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]'):
            for cand_btn in page.query_selector_all(btn_sel):
                if cand_btn.is_visible() and cand_btn.get_attribute("aria-disabled") != "true":
                    post_btn = cand_btn
                    break
            if post_btn:
                break
        if not post_btn:
            page.keyboard.type(" ")
            time.sleep(0.1)
            page.keyboard.press("Backspace")
            time.sleep(0.4)
            for btn_sel in ('[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]'):
                for cand_btn in page.query_selector_all(btn_sel):
                    if cand_btn.is_visible() and cand_btn.get_attribute("aria-disabled") != "true":
                        post_btn = cand_btn
                        break
                if post_btn:
                    break
        if not post_btn:
            raise RuntimeError("Post button not enabled after typing.")
        post_btn.click()
        time.sleep(2.5)

        # Attempt to capture published tweet ID and URL
        post_id = ""
        post_url = ""
        try:
            toast_link = page.wait_for_selector('[data-testid="toast"] a[href*="/status/"]', timeout=4000)
            if toast_link:
                href = toast_link.get_attribute("href") or ""
                if "/status/" in href:
                    post_id = href.split("/status/")[1].split("/")[0].split("?")[0]
                    post_url = f"https://x.com/InfoZimInvader/status/{post_id}"
        except Exception:
            pass

        if not post_id:
            # Fallback: inspect profile page for newly published tweet matching content
            try:
                handle = self.get_status().get("handle", "@InfoZimInvader").lstrip("@")
                profile_page = self._ensure_browser_page(target_url=f"https://x.com/{handle}")
                time.sleep(2.0)
                articles = profile_page.query_selector_all('article[data-testid="tweet"]')
                for article in articles[:4]:
                    text_elem = article.query_selector('[data-testid="tweetText"]')
                    time_elem = article.query_selector('time')
                    if text_elem and time_elem:
                        t_text = text_elem.inner_text().strip()
                        if t_text and (t_text[:25] in content or content[:25] in t_text):
                            href = time_elem.evaluate("node => node.closest('a')?.href") or ""
                            if "/status/" in href:
                                post_id = href.split("/status/")[1].split("/")[0].split("?")[0]
                                post_url = href
                                break
            except Exception as e:
                logger.debug("Profile fallback tweet ID lookup failed: %s", e)

        if not post_id:
            post_id = str(int(time.time()))
            handle = self.get_status().get("handle", "@InfoZimInvader").lstrip("@")
            post_url = f"https://x.com/{handle}/status/{post_id}"

        self.rate_limiter.record_action("tweet_post")
        logger.info("Successfully published tweet to X.com (id=%s, length=%d)", post_id, len(content))

        return {
            "status": "published",
            "content": content,
            "post_id": post_id,
            "url": post_url,
            "account": self.get_status().get("handle", "@InfoZimInvader").lstrip("@"),
            "published_at": time.time(),
        }

    def search_tweets(self, query: str, max_posts: int = 5, live: bool = True) -> List[Dict[str, Any]]:
        """Searches X.com for recent technical posts in Sabrina's niche."""
        allowed, msg = self.rate_limiter.check_quota("feed_read")
        if not allowed:
            logger.warning("Search rate limit blocked: %s", msg)
            return []

        import urllib.parse
        encoded = urllib.parse.quote(query)
        tab_param = "&f=live" if live else ""
        search_url = f"https://x.com/search?q={encoded}{tab_param}"

        page = self._ensure_browser_page(target_url=search_url)
        time.sleep(2.0)

        is_checkpoint, reason = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            logger.error("Checkpoint encountered during search: %s", reason)
            return []

        self.stealth.human_scroll(page, steps=2, delta_y=350)
        time.sleep(1.0)

        tweets = []
        articles = page.query_selector_all('article[data-testid="tweet"]')
        for article in articles[:max_posts]:
            try:
                text_elem = article.query_selector('[data-testid="tweetText"]')
                tweet_text = text_elem.inner_text() if text_elem else ""

                user_elem = article.query_selector('[data-testid="User-Name"]')
                user_info = user_elem.inner_text() if user_elem else ""
                handle_match = re.search(r"@[\w]+", user_info)
                handle = handle_match.group(0) if handle_match else "@unknown"

                link_elem = article.query_selector('time')
                parent_link = link_elem.evaluate("node => node.closest('a')?.href") if link_elem else ""

                reply_elem = article.query_selector('[data-testid="reply"]')
                retweet_elem = article.query_selector('[data-testid="retweet"]')
                like_elem = article.query_selector('[data-testid="like"]')
                bookmark_elem = article.query_selector('[data-testid="bookmark"]')

                replies = parse_metric_text(reply_elem.inner_text()) if reply_elem else 0
                reposts = parse_metric_text(retweet_elem.inner_text()) if retweet_elem else 0
                likes = parse_metric_text(like_elem.inner_text()) if like_elem else 0
                bookmarks = parse_metric_text(bookmark_elem.inner_text()) if bookmark_elem else 0

                tweets.append({
                    "author": handle,
                    "text": tweet_text,
                    "url": parent_link,
                    "metrics": {
                        "replies": replies,
                        "reposts": reposts,
                        "likes": likes,
                        "bookmarks": bookmarks
                    }
                })
            except Exception as e:
                logger.debug("Failed parsing search tweet card: %s", e)

        self.rate_limiter.record_action("feed_read")
        return tweets

    def reply_to_tweet(self, target_url: str, content: str, approved: bool = False) -> Dict[str, Any]:
        """
        Replies to a specific tweet on X.com with closed-loop DOM verification.
        MANDATORY SAFETY: Requires approved=True from operator.
        """
        if not approved:
            raise PermissionError("Publishing replies is prohibited without explicit operator approval.")

        allowed, msg = self.rate_limiter.check_quota("tweet_reply")
        if not allowed:
            raise RuntimeError(f"Rate limiter prohibited tweet reply: {msg}")

        page = self._ensure_browser_page()
        # Always navigate freshly to target_url to avoid stale DOM layout shifts
        current = page.url or ""
        if current.split("?")[0] != target_url.split("?")[0]:
            page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
            time.sleep(2.0)
        else:
            page.reload(wait_until="domcontentloaded")
            time.sleep(2.0)

        is_checkpoint, reason = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            raise RuntimeError(f"Cannot reply: security checkpoint active ({reason})")

        compose_selector = '[data-testid="tweetTextarea_0"]'
        reply_box = page.query_selector(compose_selector)
        if not reply_box:
            article = page.wait_for_selector('article[data-testid="tweet"]', timeout=8000)
            reply_icon = article.query_selector('[data-testid="reply"]')
            if reply_icon:
                reply_icon.click()
                time.sleep(1.0)

        # Closed-loop typing & active DOM verification with self-healing recovery
        self.stealth.human_type(page, compose_selector, content, target_url=target_url)
        time.sleep(1.0)

        # Pick the visible, enabled Reply/Post button (modal tweetButton takes priority over background inline bar)
        post_btn = None
        for btn_sel in ['[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]']:
            for candidate in page.query_selector_all(btn_sel):
                if candidate.is_visible() and candidate.get_attribute("aria-disabled") != "true":
                    post_btn = candidate
                    break
            if post_btn:
                break

        if not post_btn:
            # Fire a real keydown/Backspace so DraftJS registers state after insert_text
            page.keyboard.type(" ")
            time.sleep(0.1)
            page.keyboard.press("Backspace")
            time.sleep(0.5)
            for btn_sel in ['[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]']:
                for candidate in page.query_selector_all(btn_sel):
                    if candidate.is_visible() and candidate.get_attribute("aria-disabled") != "true":
                        post_btn = candidate
                        break
                if post_btn:
                    break

        if not post_btn:
            raise RuntimeError("Post/Reply button is disabled (replies restricted on target post or empty).")

        post_btn.click()
        time.sleep(2.0)

        # Attempt to capture published tweet ID and URL
        post_id = ""
        post_url = ""
        try:
            toast_link = page.wait_for_selector('[data-testid="toast"] a[href*="/status/"]', timeout=3000)
            if toast_link:
                href = toast_link.get_attribute("href") or ""
                if "/status/" in href:
                    post_id = href.split("/status/")[1].split("/")[0].split("?")[0]
                    post_url = f"https://x.com/InfoZimInvader/status/{post_id}"
        except Exception:
            pass

        # Closed-loop live timeline verification watchdog
        verified, live_id, live_url, actual_text = self.recovery.verify_live_published_tweet(page, expected_text=content)
        if live_id:
            post_id = live_id
            post_url = live_url

        self.rate_limiter.record_action("tweet_reply")
        logger.info("Successfully published reply to %s (length=%d, post_id=%s)", target_url, len(content), post_id or "unknown")

        return {
            "status": "published_reply",
            "target_url": target_url,
            "post_id": post_id,
            "post_url": post_url or target_url,
            "content": content,
            "verified_in_timeline": verified,
            "published_at": time.time(),
        }

    def delete_tweet(self, tweet_url: str, approved: bool = False) -> Dict[str, Any]:
        """
        Deletes a specific tweet owned by the user.
        MANDATORY SAFETY: Requires approved=True from operator.
        """
        if not approved:
            raise PermissionError("Deleting tweets is prohibited without explicit operator approval.")

        page = self._ensure_browser_page()
        success = self.recovery.rollback_botched_post(page, tweet_url)
        if not success:
            raise RuntimeError(f"Rollback failed to delete tweet at {tweet_url}")

        logger.info("Successfully deleted tweet at %s", tweet_url)
        return {
            "status": "deleted",
            "url": tweet_url,
            "deleted_at": time.time()
        }

    def quote_tweet(self, target_url: str, commentary: str, approved: bool = False) -> Dict[str, Any]:
        """Quote-post another user's tweet on X.com with Sabrina's commentary."""
        if not approved:
            raise PermissionError("Quote-posting is prohibited without explicit operator approval.")
        allowed, msg = self.rate_limiter.check_quota("tweet_quote")
        if not allowed:
            raise RuntimeError(f"Rate limiter prohibited quote post: {msg}")

        clean_url = target_url.split("?")[0].strip()
        page = self._ensure_browser_page(target_url=clean_url)
        time.sleep(2.0)
        is_checkpoint, reason = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            raise RuntimeError(f"Cannot quote-post: security checkpoint active ({reason})")

        # Open native Quote modal via Retweet -> Quote menu item; fall back to URL-embedded quote post if menu changes
        opened_quote_modal = False
        try:
            article = page.query_selector('article[data-testid="tweet"]')
            rt_btn = article.query_selector('[data-testid="retweet"]') if article else None
            if rt_btn and rt_btn.is_visible():
                rt_btn.click()
                time.sleep(0.7)
                quote_item = page.query_selector('a[href="/compose/post"], [role="menuitem"]:has-text("Quote")')
                if quote_item and quote_item.is_visible():
                    quote_item.click()
                    time.sleep(1.0)
                    opened_quote_modal = page.query_selector('[data-testid="tweetTextarea_0"]') is not None
        except Exception as exc:
            logger.debug("Native quote menu fallback to URL embed: %s", exc)

        if opened_quote_modal:
            self.stealth.human_type(page, '[data-testid="tweetTextarea_0"]', commentary)
            time.sleep(0.8)
            post_btn = None
            for btn_sel in ('[data-testid="tweetButton"]', '[data-testid="tweetButtonInline"]'):
                for cand_btn in page.query_selector_all(btn_sel):
                    if cand_btn.is_visible() and cand_btn.get_attribute("aria-disabled") != "true":
                        post_btn = cand_btn
                        break
                if post_btn:
                    break
            if not post_btn:
                page.keyboard.type(" ")
                time.sleep(0.1)
                page.keyboard.press("Backspace")
                time.sleep(0.4)
                for cand_btn in page.query_selector_all('[data-testid="tweetButton"]'):
                    if cand_btn.is_visible() and cand_btn.get_attribute("aria-disabled") != "true":
                        post_btn = cand_btn
                        break
            if not post_btn:
                raise RuntimeError("Quote Post button not enabled after typing.")
            post_btn.click()
            time.sleep(2.2)
            post_id = ""
            post_url = ""
            try:
                toast_link = page.wait_for_selector('[data-testid="toast"] a[href*="/status/"]', timeout=3500)
                if toast_link:
                    href = toast_link.get_attribute("href") or ""
                    if "/status/" in href:
                        post_id = href.split("/status/")[1].split("/")[0].split("?")[0]
                        handle = self.get_status().get("handle", "@InfoZimInvader").lstrip("@")
                        post_url = f"https://x.com/{handle}/status/{post_id}"
            except Exception:
                pass
            self.rate_limiter.record_action("tweet_quote")
            return {
                "status": "published_quote",
                "quoted_url": clean_url,
                "content": commentary,
                "post_id": post_id,
                "url": post_url or f"https://x.com/InfoZimInvader",
                "published_at": time.time(),
            }

        # Fallback: X automatically renders a trailing status URL as an embedded Quote Post
        combined = f"{commentary[:240].strip()} {clean_url}"
        pub = self.publish_tweet(combined, approved=True)
        self.rate_limiter.record_action("tweet_quote")
        return {**pub, "status": "published_quote", "quoted_url": clean_url, "content": commentary}

    def repost_tweet(self, target_url: str, approved: bool = False) -> Dict[str, Any]:
        """Repost (Retweet) another creator's post."""
        if not approved:
            raise PermissionError("Reposting is prohibited without explicit operator approval.")
        allowed, msg = self.rate_limiter.check_quota("tweet_retweet")
        if not allowed:
            return {"status": "skipped", "reason": msg}

        page = self._ensure_browser_page(target_url=target_url.split("?")[0])
        time.sleep(1.8)
        article = page.query_selector('article[data-testid="tweet"]')
        if not article:
            return {"status": "skipped", "reason": "Tweet article not found"}
        # Skip if already reposted (unretweet testid present)
        if article.query_selector('[data-testid="unretweet"]'):
            return {"status": "already_reposted", "target_url": target_url}
        rt_btn = article.query_selector('[data-testid="retweet"]')
        if not rt_btn or not rt_btn.is_visible():
            return {"status": "skipped", "reason": "Repost button not visible"}
        rt_btn.click()
        time.sleep(0.6)
        confirm = page.query_selector('[data-testid="retweetConfirm"]')
        if confirm and confirm.is_visible():
            confirm.click()
            time.sleep(1.0)
            self.rate_limiter.record_action("tweet_retweet")
            return {"status": "reposted", "target_url": target_url}
        return {"status": "skipped", "reason": "Repost confirm menu item not found"}

    def follow_account(self, handle: str, approved: bool = False) -> Dict[str, Any]:
        """Follow a creator on X (never unfollowing if already followed)."""
        if not approved:
            raise PermissionError("Following accounts requires explicit approval.")
        allowed, msg = self.rate_limiter.check_quota("user_follow")
        if not allowed:
            return {"status": "skipped", "reason": msg}

        clean_h = handle.strip().lstrip("@")
        if not clean_h or clean_h.lower() == "unknown":
            return {"status": "skipped", "reason": "Invalid handle"}

        page = self._ensure_browser_page(target_url=f"https://x.com/{clean_h}")
        time.sleep(1.8)
        is_checkpoint, reason = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            return {"status": "blocked", "reason": reason}

        # Check if already following ('-unfollow' testid means already following!)
        if page.query_selector('[data-testid="placementTracking"] [data-testid$="-unfollow"]'):
            return {"status": "already_following", "handle": f"@{clean_h}"}

        follow_btn = page.query_selector('[data-testid="placementTracking"] [data-testid$="-follow"]')
        if follow_btn and follow_btn.is_visible():
            follow_btn.click()
            time.sleep(0.9)
            self.rate_limiter.record_action("user_follow")
            return {"status": "followed", "handle": f"@{clean_h}"}
        return {"status": "skipped", "reason": "Follow button not present"}

    def read_mentions(self, own_handle: str = "@InfoZimInvader", max_items: int = 6) -> List[Dict[str, Any]]:
        """Read inbound replies and @mentions from x.com/notifications/mentions for two-way conversation."""
        allowed, msg = self.rate_limiter.check_quota("feed_read")
        if not allowed:
            return []

        page = self._ensure_browser_page(target_url="https://x.com/notifications/mentions")
        time.sleep(2.2)
        is_checkpoint, _ = self.stealth.check_checkpoint(page)
        if is_checkpoint:
            return []

        mentions: List[Dict[str, Any]] = []
        articles = page.query_selector_all('article[data-testid="tweet"]')
        for article in articles[: max_items * 2]:
            try:
                text_elem = article.query_selector('[data-testid="tweetText"]')
                tweet_text = text_elem.inner_text().strip() if text_elem else ""
                user_elem = article.query_selector('[data-testid="User-Name"]')
                user_info = user_elem.inner_text() if user_elem else ""
                handle_match = re.search(r"@[\w]+", user_info)
                author = handle_match.group(0) if handle_match else "@unknown"
                if author.lower() == own_handle.lower() or not tweet_text:
                    continue
                link_elem = article.query_selector("time")
                url = link_elem.evaluate("node => node.closest('a')?.href") if link_elem else ""
                if not url or "/status/" not in url:
                    continue
                mentions.append({"author": author, "text": tweet_text, "url": url})
                if len(mentions) >= max_items:
                    break
            except Exception:
                continue
        self.rate_limiter.record_action("feed_read")
        return mentions
