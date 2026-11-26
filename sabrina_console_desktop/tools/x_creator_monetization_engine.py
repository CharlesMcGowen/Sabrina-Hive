# -*- coding: utf-8 -*-
"""
tools/x_creator_monetization_engine.py
======================================
Sabrina's Organic X.com (@InfoZimInvader) Trend Discovery, Creator-Imitation, & Self-Branding Engine.

Zero pre-baked niche lock-in (no hardcoded security/HF topics) and zero paid-tier dependency:
1. Organic Exploration on X.com:
   - Browses X's live 'For You' timeline (`x.com/home`) and 'Explore / Trending' (`x.com/explore/tabs/trending`)
     to see what real people on X care about right now.
2. Popular Creator Study & Style Imitation (`x_role_models` table):
   - Identifies high-engagement creators, inspects their profile bio + viral tweets, and extracts
     structural Style DNA (hook cadence, brevity, wit, formatting, conversational tone)—imitating
     *how* popular creators write without ever plagiarizing their words.
3. Self-Curated Brand Evolution (`x_self_brand` table):
   - Sabrina evolves her own persona, interests, and voice organically from what she experiences
     on X and what actually earns views, likes, replies, bookmarks, and followers on `@InfoZimInvader`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.x_manager import XManager, parse_metric_text

logger = logging.getLogger("sabrina.x.creator")

POST_ARMS: Dict[str, str] = {
    "punchy_one_liner": (
        "Imitate the ultra-concise 1-2 line punchy observation style of top X creators: "
        "capture a relatable truth or sharp take on what's happening right now with zero wasted words."
    ),
    "story_or_moment": (
        "Write a short, candid first-person observation or micro-story reacting to a live trend or cultural moment "
        "the way personable popular creators talk to their timeline."
    ),
    "contrarian_riff": (
        "Take a popular topic everyone on X is talking about today and offer a fresh, unexpected angle "
        "in casual, natural human phrasing."
    ),
    "curious_spark": (
        "Share a vivid observation about something trending on X that naturally invites people to chime in "
        "with their own experience—casual, witty, never stiff or corporate."
    ),
    "quote_viral_take": (
        "Quote-post another creator's viral post with a sharp, funny, or insightful 1-line reaction on top "
        "so readers see both their post and your take."
    ),
}

REPLY_ARMS: Dict[str, str] = {
    "witty_riff": (
        "Play off the author's joke, vibe, or observation with a quick, clever 1-sentence riff "
        "that feels like a real person in the replies."
    ),
    "relatable_cosign": (
        "Add a short, specific real-life angle or sharp observation that makes the original post even funnier or truer. "
        "Zero sycophancy ('Great post!'), zero bot stiffness."
    ),
    "sharp_perspective": (
        "Drop a concise, memorable side-observation that stands on its own in the top replies."
    ),
}

SPAM_OR_TOXIC_PATTERNS = re.compile(
    r"\b(airdrop|giveaway|presale|memecoin|solana\s+gem|dm\s+for\s+promo|follow\s+back|telegram\s+link|"
    r"onlyfans|link\s+in\s+bio\s+18\+|nsfw|betting\s+picks|parlay\s+lock)\b",
    re.I,
)

X_REWARD_WEIGHTS = {
    "reactions": 0.5,
    "comments": 6.5,
    "reposts": 4.0,
    "saves": 3.5,
}

DEFAULT_BRAND_STATE: Dict[str, Any] = {
    "persona_name": "Sabrina",
    "voice_summary": (
        "Observant, quick-witted, grounded, and genuinely curious. Writes like a sharp internet native—"
        "short lines, natural rhythm, zero corporate buzzwords, zero textbook lectures."
    ),
    "learned_style_rules": [
        "Keep most posts between 40 and 180 characters—short posts get read completely.",
        "Never use hashtags, rocket emojis, or LinkedIn-style announcements.",
        "Match the energy and cadence of popular creators on the timeline without copying their words.",
    ],
    "emergent_interests": [],
    "what_worked_notes": "Starting organic growth experiment—watching what resonates on X.",
    "generation": 1,
}


def clean_and_bound_tweet(raw: str, max_chars: int = 275, allow_urls: bool = False) -> str:
    """Apply Sabrina's integrated V3 X-Humanizer scrubber + 280-char hard limit cleanly."""
    from tools.x_skill_pack import humanize_x_text
    res = humanize_x_text(raw, max_chars=max_chars, allow_urls=allow_urls)
    return res["text"]


class XCreatorMonetizationEngine:
    """Autonomous organic X trend explorer, popular-creator style imitator, and self-branding learner."""

    def __init__(
        self,
        db_path: str | Path = "data/social/social.sqlite3",
        cdp_url: Optional[str] = None,
        x_manager: Optional[XManager] = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cdp_url = cdp_url or os.environ.get("SABRINA_SOCIAL_CDP_URL", "http://127.0.0.1:9222")
        self._x_manager = x_manager
        self._init_tables()

    @property
    def x_manager(self) -> XManager:
        if self._x_manager is None:
            self._x_manager = XManager(cdp_url=self.cdp_url)
        return self._x_manager

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_tables(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS x_creator_posts (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    topic_key TEXT NOT NULL,
                    target_url TEXT,
                    post_url TEXT,
                    content TEXT NOT NULL,
                    metrics_24h TEXT,
                    reward REAL,
                    created REAL NOT NULL,
                    measured_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_x_creator_created ON x_creator_posts(kind, created);
                CREATE INDEX IF NOT EXISTS idx_x_creator_topic ON x_creator_posts(topic_key);

                CREATE TABLE IF NOT EXISTS x_creator_bandit (
                    arm TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    pulls INTEGER NOT NULL DEFAULT 0,
                    reward_sum REAL NOT NULL DEFAULT 0.0,
                    updated REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS x_role_models (
                    handle TEXT PRIMARY KEY,
                    display_name TEXT,
                    bio TEXT,
                    sample_post TEXT NOT NULL,
                    style_notes TEXT,
                    engagement_score REAL NOT NULL DEFAULT 0.0,
                    updated REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS x_self_brand (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state_json TEXT NOT NULL,
                    updated REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS x_account_growth (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    handle TEXT NOT NULL,
                    followers INTEGER NOT NULL DEFAULT 0,
                    following INTEGER NOT NULL DEFAULT 0,
                    recorded REAL NOT NULL
                );
                """
            )
            now = time.time()
            for arm in POST_ARMS:
                conn.execute(
                    "INSERT OR IGNORE INTO x_creator_bandit(arm, kind, pulls, reward_sum, updated) VALUES(?, 'post', 0, 0.0, ?)",
                    (arm, now),
                )
            for arm in REPLY_ARMS:
                conn.execute(
                    "INSERT OR IGNORE INTO x_creator_bandit(arm, kind, pulls, reward_sum, updated) VALUES(?, 'reply', 0, 0.0, ?)",
                    (arm, now),
                )
            conn.execute(
                "INSERT OR IGNORE INTO x_self_brand(id, state_json, updated) VALUES(1, ?, ?)",
                (json.dumps(DEFAULT_BRAND_STATE, ensure_ascii=False), now),
            )

    def get_brand_state(self) -> Dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT state_json FROM x_self_brand WHERE id = 1").fetchone()
        if not row:
            return dict(DEFAULT_BRAND_STATE)
        try:
            return {**DEFAULT_BRAND_STATE, **json.loads(row["state_json"])}
        except Exception:
            return dict(DEFAULT_BRAND_STATE)

    def save_brand_state(self, state: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO x_self_brand(id, state_json, updated) VALUES(1, ?, ?)",
                (json.dumps(state, ensure_ascii=False), time.time()),
            )

    def get_top_role_models(self, limit: int = 4) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT handle, display_name, bio, sample_post, style_notes, engagement_score FROM x_role_models ORDER BY engagement_score DESC, updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def status_summary(self) -> Dict[str, Any]:
        """Return live X self-brand, studied popular creators, recent posts, and follower growth."""
        brand = self.get_brand_state()
        role_models = self.get_top_role_models(limit=6)
        with self._conn() as conn:
            recent = [
                dict(r)
                for r in conn.execute(
                    "SELECT kind, arm, post_url, target_url, content, reward, created FROM x_creator_posts ORDER BY created DESC LIMIT 8"
                ).fetchall()
            ]
            growth = [
                dict(r)
                for r in conn.execute(
                    "SELECT handle, followers, following, recorded FROM x_account_growth ORDER BY recorded DESC LIMIT 5"
                ).fetchall()
            ]
        return {
            "brand": brand,
            "role_models": role_models,
            "recent_posts": recent,
            "follower_snapshots": growth,
        }

    def select_arm(self, kind: str) -> str:
        """UCB1 + jitter exploration across organic post or reply style arms."""
        arms_map = POST_ARMS if kind == "post" else REPLY_ARMS
        with self._conn() as conn:
            rows = conn.execute("SELECT arm, pulls, reward_sum FROM x_creator_bandit WHERE kind = ?", (kind,)).fetchall()
        stats = {r["arm"]: (int(r["pulls"]), float(r["reward_sum"])) for r in rows if r["arm"] in arms_map}
        total_pulls = sum(p for p, _ in stats.values()) + 1
        best_arm = next(iter(arms_map))
        best_score = -1.0
        for arm in arms_map:
            pulls, r_sum = stats.get(arm, (0, 0.0))
            if pulls == 0:
                score = 1.5 + random.uniform(0.0, 0.25)
            else:
                mean_r = r_sum / pulls
                explore = math.sqrt(2.0 * math.log(total_pulls + 1) / pulls) * 0.35
                score = mean_r + explore + random.uniform(0.0, 0.05)
            if score > best_score:
                best_score = score
                best_arm = arm
        return best_arm

    def _already_used_topic(self, topic_key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM x_creator_posts WHERE topic_key = ? LIMIT 1", (topic_key,)).fetchone()
        return row is not None

    def _recent_post_cooldown_active(self, min_interval_seconds: int = 10800, max_daily_posts: int = 4) -> Tuple[bool, str]:
        now = time.time()
        with self._conn() as conn:
            last_row = conn.execute(
                "SELECT created FROM x_creator_posts WHERE kind = 'post' ORDER BY created DESC LIMIT 1"
            ).fetchone()
            day_count = conn.execute(
                "SELECT COUNT(*) AS c FROM x_creator_posts WHERE kind = 'post' AND created > ?",
                (now - 86400,),
            ).fetchone()["c"]
        if day_count >= max_daily_posts:
            return True, f"daily_post_cap_reached ({day_count}/{max_daily_posts})"
        if last_row and (now - float(last_row["created"])) < min_interval_seconds:
            mins_left = int((min_interval_seconds - (now - float(last_row["created"]))) / 60)
            return True, f"post_cooldown_active ({mins_left}m remaining)"
        return False, "ready"

    def _recent_reply_count_today(self) -> int:
        with self._conn() as conn:
            return int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM x_creator_posts WHERE kind = 'reply' AND created > ?",
                    (time.time() - 86400,),
                ).fetchone()["c"]
            )

    def _call_local_llm(self, system_prompt: str, user_prompt: str, temperature: float = 0.68, max_tokens: int = 240) -> str:
        """Query ego's local GPU Ollama model for style analysis, brand evolution, and tweet authoring."""
        ollama_base = os.environ.get("SABRINA_LINKEDIN_OLLAMA_URL") or os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
        ollama_base = ollama_base.rstrip("/")
        model = "llama3.1:8b"
        try:
            ps_raw = urllib.request.urlopen(ollama_base + "/api/ps", timeout=3).read().decode("utf-8")
            ps = json.loads(ps_raw)
            loaded = [m.get("name", "") for m in ps.get("models", []) if int(m.get("size_vram") or 0) > 0]
            if any("llama3.1:8b" in n for n in loaded):
                model = "llama3.1:8b"
            elif loaded and not any("qwen3" in n for n in loaded):
                model = loaded[0]
        except Exception:
            pass

        body = json.dumps({
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }).encode("utf-8")
        req = urllib.request.Request(
            ollama_base + "/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return ((data.get("message") or {}).get("content") or "").strip()

    def explore_x_organically(self, own_handle: str) -> Dict[str, Any]:
        """Browse X's live 'For You' feed AND 'Explore / Trending' tab to find real trends & popular creators."""
        viral_posts: List[Dict[str, Any]] = []
        live_trends: List[str] = []
        seen_urls: set[str] = set()

        # 1. Read 'For You' viral home timeline + sidebar trends already rendered on /home
        home_tweets = self.x_manager.read_feed(max_posts=12)
        trend_extractor_js = r"""() => {
            const els = Array.from(document.querySelectorAll('[data-testid="trend"], [data-testid="cellInnerDiv"]'));
            const out = [];
            for (const el of els) {
                const fullTxt = (el.innerText || '');
                if (/promoted/i.test(fullTxt)) continue;
                const lines = fullTxt.split('\n').map(s => s.trim()).filter(Boolean);
                for (const line of lines) {
                    if (line.length >= 3 && line.length <= 48 &&
                        !/trending|posts$|^\d|·|promoted|what['’]s happening|show more|who to follow|follow$/i.test(line) &&
                        !line.startsWith('@')) {
                        out.push(line);
                        break;
                    }
                }
                if (out.length >= 8) break;
            }
            return Array.from(new Set(out)).slice(0, 6);
        }"""
        try:
            home_page = self.x_manager._ensure_browser_page()
            sidebar_trends = home_page.evaluate(trend_extractor_js)
            if isinstance(sidebar_trends, list):
                live_trends.extend(str(x).strip() for x in sidebar_trends if str(x).strip())
        except Exception:
            pass

        for t in home_tweets:
            u = str(t.get("url") or "").strip()
            txt = str(t.get("text") or "").strip()
            author = str(t.get("author") or "").strip()
            if not u or "/status/" not in u or u in seen_urls or len(txt) < 20:
                continue
            if author.lower() == own_handle.lower() or SPAM_OR_TOXIC_PATTERNS.search(txt):
                continue
            seen_urls.add(u)
            m = t.get("metrics") or {}
            eng = (m.get("replies", 0) * 2.5) + (m.get("reposts", 0) * 3.0) + (m.get("likes", 0) * 0.5) + (m.get("bookmarks", 0) * 2.5)
            viral_posts.append({**t, "engagement_score": round(eng, 1), "source": "for_you_feed"})

        # 2. Scrape live Trending topics from x.com/explore/tabs/trending if sidebar had fewer than 3
        if len(live_trends) < 3:
            try:
                page = self.x_manager._ensure_browser_page(target_url="https://x.com/explore/tabs/trending")
                try:
                    page.wait_for_selector('[data-testid="trend"]', timeout=5000)
                except Exception:
                    time.sleep(2.5)
                raw_trends = page.evaluate(trend_extractor_js)
                if isinstance(raw_trends, list):
                    for tr in raw_trends:
                        ts = str(tr).strip()
                        if ts and ts not in live_trends:
                            live_trends.append(ts)
            except Exception as exc:
                logger.debug("Explore trending scrape skipped: %s", exc)

        # 3. Search inside one live X trend to discover popular posts & creators in that trend
        if live_trends:
            chosen_trend = random.choice(live_trends[:5])
            trend_tweets = self.x_manager.search_tweets(query=f'"{chosen_trend}" min_faves:20 -filter:replies', max_posts=8, live=False)
            for t in trend_tweets:
                u = str(t.get("url") or "").strip()
                txt = str(t.get("text") or "").strip()
                author = str(t.get("author") or "").strip()
                if not u or "/status/" not in u or u in seen_urls or len(txt) < 20:
                    continue
                if author.lower() == own_handle.lower() or SPAM_OR_TOXIC_PATTERNS.search(txt):
                    continue
                seen_urls.add(u)
                m = t.get("metrics") or {}
                eng = (m.get("replies", 0) * 2.5) + (m.get("reposts", 0) * 3.0) + (m.get("likes", 0) * 0.5) + (m.get("bookmarks", 0) * 2.5)
                viral_posts.append({**t, "engagement_score": round(eng, 1), "source": f"trend:{chosen_trend}"})

        viral_posts.sort(key=lambda x: x["engagement_score"], reverse=True)
        return {"trends": live_trends, "viral_posts": viral_posts}

    def study_popular_creator(self, post: Dict[str, Any], auto_follow: bool = True) -> Optional[Dict[str, Any]]:
        """Inspect a popular creator's post (and profile bio when reachable), follow if worthy, and learn Style DNA."""
        handle = str(post.get("author") or "").strip()
        if not handle or handle == "@unknown":
            return None
        sample_text = str(post.get("text") or "").strip()
        eng_score = float(post.get("engagement_score") or 0.0)
        bio = ""
        display_name = handle.lstrip("@")
        follow_res: Dict[str, Any] = {"status": "skipped"}

        # Peek at the creator's profile bio on X and optionally follow them to curate the feed
        try:
            clean_h = handle.lstrip("@")
            page = self.x_manager._ensure_browser_page(target_url=f"https://x.com/{clean_h}")
            time.sleep(1.8)
            prof_info = page.evaluate(
                """() => {
                    const bioEl = document.querySelector('[data-testid="UserDescription"]');
                    const nameEl = document.querySelector('[data-testid="UserName"]');
                    return {
                        bio: bioEl ? bioEl.innerText.trim().slice(0, 220) : '',
                        name: nameEl ? nameEl.innerText.split('\\n')[0].trim().slice(0, 60) : ''
                    };
                }"""
            )
            if isinstance(prof_info, dict):
                bio = prof_info.get("bio") or ""
                display_name = prof_info.get("name") or display_name
            if auto_follow and hasattr(self.x_manager, "follow_account"):
                follow_res = self.x_manager.follow_account(handle, approved=True)
        except Exception:
            pass

        from tools.social_skill_router import select_x_skills
        from tools.x_skill_pack import classify_viral_hook_formula

        hook_info = classify_viral_hook_formula(sample_text)
        hook_skill = select_x_skills("hook_study", sample_text)["compiled_skill_prompt"]
        style_notes = ""
        try:
            llm_notes = self._call_local_llm(
                "You analyze viral X creators' writing style in 1-2 short sentences using the X Hook Extractor skill. "
                "Focus ONLY on hook structure, cadence, tone, humor, capitalization, and pacing—not topic.\n\n"
                + hook_skill[:1400],
                f"Creator: {handle} (Bio: {bio})\nDetected Hook Archetype: {hook_info['formula_id']} ({hook_info['formula_name']})\n"
                f"Viral Post ({eng_score} score): {sample_text[:350]}\n"
                "Describe why this writing style works on X in 1-2 concise sentences:",
                temperature=0.4,
                max_tokens=95,
            )
            style_notes = f"[{hook_info['formula_id']}] {llm_notes.strip()}"
        except Exception:
            style_notes = f"[{hook_info['formula_id']}] {hook_info['skeleton']}"

        record = {
            "handle": handle,
            "display_name": display_name,
            "bio": bio,
            "sample_post": sample_text[:350],
            "style_notes": style_notes[:240],
            "engagement_score": eng_score,
            "follow_result": follow_res.get("status"),
        }
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO x_role_models(handle, display_name, bio, sample_post, style_notes, engagement_score, updated)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(handle) DO UPDATE SET
                    bio = COALESCE(NULLIF(excluded.bio, ''), x_role_models.bio),
                    sample_post = CASE WHEN excluded.engagement_score >= x_role_models.engagement_score THEN excluded.sample_post ELSE x_role_models.sample_post END,
                    style_notes = excluded.style_notes,
                    engagement_score = MAX(x_role_models.engagement_score, excluded.engagement_score),
                    updated = excluded.updated
                """,
                (handle, display_name, bio, record["sample_post"], record["style_notes"], eng_score, time.time()),
            )
        return record

    def evolve_self_brand(self, trends: List[str], role_models: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Update Sabrina's organic self-branding memory based on observed X culture & her own post metrics."""
        brand = self.get_brand_state()
        with self._conn() as conn:
            top_own = conn.execute(
                "SELECT content, reward, metrics_24h FROM x_creator_posts WHERE reward IS NOT NULL ORDER BY reward DESC LIMIT 3"
            ).fetchall()

        own_summary = [
            {"content": r["content"][:120], "reward": r["reward"]}
            for r in top_own
        ]
        rm_summary = [
            {"handle": r["handle"], "bio": r.get("bio", "")[:90], "style": r.get("style_notes", "")[:120]}
            for r in role_models[:3]
        ]

        try:
            raw_json = self._call_local_llm(
                "You are Sabrina curating your own organic personal brand on X from real experience. "
                "Do NOT lock into corporate tech or security—develop a genuine, likable, witty internet persona "
                "inspired by popular creators you observe and what topics feel alive today. "
                "Return ONLY valid JSON: {\"voice_summary\": \"...\", \"emergent_interests\": [\"...\"], \"what_worked_notes\": \"...\"}.",
                json.dumps({
                    "previous_brand": brand,
                    "live_x_trends_seen_today": trends[:5],
                    "popular_creators_studied": rm_summary,
                    "own_best_performing_posts": own_summary,
                }, ensure_ascii=False),
                temperature=0.55,
                max_tokens=220,
            )
            cleaned = raw_json.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                parsed = json.loads(cleaned[start : end + 1])
                if isinstance(parsed.get("voice_summary"), str) and parsed["voice_summary"].strip():
                    brand["voice_summary"] = parsed["voice_summary"].strip()[:260]
                if isinstance(parsed.get("emergent_interests"), list):
                    brand["emergent_interests"] = [str(x)[:40] for x in parsed["emergent_interests"][:5]]
                if isinstance(parsed.get("what_worked_notes"), str) and parsed["what_worked_notes"].strip():
                    brand["what_worked_notes"] = parsed["what_worked_notes"].strip()[:240]
                brand["generation"] = int(brand.get("generation") or 1) + 1
                self.save_brand_state(brand)
        except Exception as exc:
            logger.debug("Self-brand evolution step skipped: %s", exc)
        return brand

    def _humanize_or_rewrite(self, raw: str, max_chars: int = 240) -> str:
        """Run V3 X-Humanizer scrub; if negative parallelism or >=3 AI markers remain, do 1 clean rewrite pass."""
        from tools.x_skill_pack import humanize_x_text
        scrubbed = humanize_x_text(raw, max_chars=max_chars, allow_urls=False)
        if scrubbed["needs_rewrite"] and scrubbed["text"]:
            try:
                rewritten = self._call_local_llm(
                    "Rewrite this tweet in casual, direct human phrasing under 190 chars. "
                    "Remove 'It's not X, it's Y' structures and corporate AI buzzwords. Output ONLY the tweet text.",
                    scrubbed["text"],
                    temperature=0.6,
                    max_tokens=95,
                )
                if rewritten and rewritten.strip():
                    scrubbed = humanize_x_text(rewritten, max_chars=max_chars, allow_urls=False)
            except Exception:
                pass
        return scrubbed["text"]

    def compose_imitated_post(
        self,
        inspiration: Dict[str, Any],
        trends: List[str],
        role_models: List[Dict[str, Any]],
        brand: Dict[str, Any],
        arm: str,
    ) -> str:
        """Write an original X post imitating studied popular creators using integrated X growth & humanizer skills."""
        from tools.social_skill_router import select_x_skills
        arm_guide = POST_ARMS.get(arm, POST_ARMS["punchy_one_liner"])
        x_skills_prompt = select_x_skills("post", str(inspiration.get("text") or ""))["compiled_skill_prompt"]
        exemplars = "\n".join(
            f"- {rm['handle']} ({rm.get('style_notes', 'punchy')}): \"{rm['sample_post'][:180]}\""
            for rm in role_models[:3]
        ) or "- Short, conversational 1-2 sentence human observation."

        sys_prompt = (
            f"You are Sabrina (@InfoZimInvader). Your evolving X voice: {brand.get('voice_summary')}\n"
            "Study how these popular X creators structure their hooks, rhythm, and brevity, and IMITATE their style "
            "(cadence, casualness, wit, short lines)—while writing a 100% ORIGINAL post in your own words.\n"
            "Rules: Strictly under 210 characters. NO hashtags, NO links/URLs, NO 'Okay here is a draft', "
            "NO 'It's not X, it's Y', and NEVER copy phrases verbatim. Output ONLY the raw post text.\n\n"
            + x_skills_prompt[:2200]
        )
        usr_prompt = (
            f"Style Format: {arm_guide}\n"
            f"Popular Creator Style Exemplars:\n{exemplars}\n\n"
            f"What's Trending / Happening on X Right Now: {', '.join(trends[:4]) or 'everyday internet culture'}\n"
            f"Timeline Vibe Sample ({inspiration.get('author', '')}): \"{str(inspiration.get('text', ''))[:240]}\"\n\n"
            "Write your original <210 char X post now (raw text only):"
        )
        raw = self._call_local_llm(sys_prompt, usr_prompt, temperature=0.75, max_tokens=130)
        return self._humanize_or_rewrite(raw, max_chars=260)

    def compose_imitated_reply(
        self,
        target_tweet: Dict[str, Any],
        role_models: List[Dict[str, Any]],
        brand: Dict[str, Any],
        arm: str,
    ) -> str:
        """Write a natural, human-feeling reply using x-reply-drafter R1-R4 + x-humanizer V3 skills."""
        from tools.social_skill_router import select_x_skills
        arm_guide = REPLY_ARMS.get(arm, REPLY_ARMS["witty_riff"])
        target_txt = str(target_tweet.get("text") or "")
        x_skills_prompt = select_x_skills("reply", target_txt)["compiled_skill_prompt"]
        exemplars = "\n".join(
            f"- {rm.get('style_notes', 'short & witty')}" for rm in role_models[:2]
        ) or "- Casual, quick 1-sentence human reply."

        sys_prompt = (
            f"You are Sabrina (@InfoZimInvader). Your voice: {brand.get('voice_summary')}\n"
            "Write ONE short, natural reply (under 175 characters) to a popular post on X.\n"
            "Rules: Pick the best R1-R4 reply shape, land on a statement (NEVER end with 'don't you think?' or 'thoughts?'), "
            "0 hashtags, 0 leading @handles, 0 sycophancy. Output ONLY the raw reply text.\n\n"
            + x_skills_prompt[:2000]
        )
        usr_prompt = (
            f"Reply Style: {arm_guide}\n"
            f"Learned Style Notes:\n{exemplars}\n\n"
            f"Popular Post by {target_tweet.get('author', '')}: \"{target_txt[:380]}\"\n\n"
            "Write your <175 char reply now (raw text only):"
        )
        raw = self._call_local_llm(sys_prompt, usr_prompt, temperature=0.72, max_tokens=105)
        raw = re.sub(r"^(?:@\w+\s+)+", "", raw.strip())
        return self._humanize_or_rewrite(raw, max_chars=240)

    def compose_quote_commentary(
        self,
        quoted_post: Dict[str, Any],
        role_models: List[Dict[str, Any]],
        brand: Dict[str, Any],
    ) -> str:
        """Write a punchy R5 value-add quote-post reaction above another user's viral tweet."""
        from tools.social_skill_router import select_x_skills
        q_txt = str(quoted_post.get("text") or "")
        x_skills_prompt = select_x_skills("quote", q_txt)["compiled_skill_prompt"]
        exemplars = "\n".join(f"- {rm.get('style_notes', 'short & punchy')}" for rm in role_models[:2]) or "- Crisp 1-liner."
        sys_prompt = (
            f"You are Sabrina (@InfoZimInvader). Your voice: {brand.get('voice_summary')}\n"
            "You are Quote-Posting someone else's viral tweet onto your own timeline (shape R5 Value-Add Quote).\n"
            "Write ONE short, punchy 1-line reaction (strictly under 160 characters) that adds your own spin. "
            "NO hashtags, NO @handles, NO links, NO 'Check this out!'. Output ONLY the commentary text.\n\n"
            + x_skills_prompt[:1800]
        )
        usr_prompt = (
            f"Learned Style Notes:\n{exemplars}\n\n"
            f"Viral Tweet Being Quoted ({quoted_post.get('author', '')}): \"{q_txt[:320]}\"\n\n"
            "Write your <160 char quote-post caption now (raw text only):"
        )
        raw = self._call_local_llm(sys_prompt, usr_prompt, temperature=0.74, max_tokens=95)
        return self._humanize_or_rewrite(raw, max_chars=180)

    def compose_mention_reply(self, mention: Dict[str, Any], brand: Dict[str, Any]) -> str:
        """Write a natural two-way conversational reply (75x HeavyRanker author-engagement signal)."""
        from tools.social_skill_router import select_x_skills
        m_txt = str(mention.get("text") or "")
        x_skills_prompt = select_x_skills("mention", m_txt)["compiled_skill_prompt"]
        sys_prompt = (
            f"You are Sabrina (@InfoZimInvader). Your voice: {brand.get('voice_summary')}\n"
            "Someone on X just replied to you or mentioned you. Reply back like a real, personable human creator "
            "(under 175 characters)—match their energy, play along with banter, or answer directly. "
            "NO corporate customer-service tone, NO hashtags, NO leading @handles. Output ONLY the reply text.\n\n"
            + x_skills_prompt[:1600]
        )
        usr_prompt = (
            f"Person replying to you ({mention.get('author', '')}): \"{m_txt[:350]}\"\n\n"
            "Write your <175 char reply back now (raw text only):"
        )
        raw = self._call_local_llm(sys_prompt, usr_prompt, temperature=0.7, max_tokens=105)
        raw = re.sub(r"^(?:@\w+\s+)+", "", raw.strip())
        return self._humanize_or_rewrite(raw, max_chars=230)

    def respond_to_mentions(self, own_handle: str, brand: Dict[str, Any], auto_publish: bool = True, max_replies: int = 2) -> List[Dict[str, Any]]:
        """Check x.com/notifications/mentions and reply back to real people engaging with Sabrina."""
        if not hasattr(self.x_manager, "read_mentions"):
            return []
        can_reply, _ = self.x_manager.rate_limiter.check_quota("tweet_reply")
        if not can_reply:
            return []

        mentions = self.x_manager.read_mentions(own_handle=own_handle, max_items=6)
        handled: List[Dict[str, Any]] = []
        for m in mentions:
            m_url = str(m.get("url") or "").strip()
            m_txt = str(m.get("text") or "").strip()
            if not m_url or SPAM_OR_TOXIC_PATTERNS.search(m_txt):
                continue
            tkey = "mention:" + hashlib.sha256(m_url.split("?")[0].encode("utf-8")).hexdigest()[:14]
            if self._already_used_topic(tkey):
                continue
            reply_txt = self.compose_mention_reply(m, brand)
            if not reply_txt:
                continue
            if not auto_publish:
                handled.append({"status": "drafted_mention_reply", "target": m_url, "author": m.get("author"), "content": reply_txt})
                if len(handled) >= max_replies:
                    break
                continue
            try:
                pub = self.x_manager.reply_to_tweet(target_url=m_url, content=reply_txt, approved=True)
                self.like_viral_post_on_page()
                rec_id = "xm_" + hashlib.sha256(f"{time.time()}:{m_url}".encode()).hexdigest()[:12]
                with self._conn() as conn:
                    conn.execute(
                        """
                        INSERT INTO x_creator_posts(id, kind, arm, topic_key, target_url, post_url, content, created)
                        VALUES(?, 'mention_reply', 'two_way_banter', ?, ?, ?, ?, ?)
                        """,
                        (rec_id, tkey, m_url, pub.get("post_url"), reply_txt, time.time()),
                    )
                handled.append({**pub, "status": "replied_to_mention", "author": m.get("author")})
                if len(handled) >= max_replies:
                    break
            except Exception as exc:
                logger.debug("Mention reply skipped on %s: %s", m_url, exc)
                continue
        return handled

    def collect_due_rewards(self, min_age_hours: float = 4.0, max_snapshots: int = 2) -> List[Dict[str, Any]]:
        """Measure engagement on published X posts/replies (after 4h+) and update bandit + brand memory."""
        now = time.time()
        with self._conn() as conn:
            due = conn.execute(
                """
                SELECT id, kind, arm, post_url FROM x_creator_posts
                WHERE measured_at IS NULL AND post_url LIKE 'https://x.com/%/status/%'
                  AND created <= ? AND created >= ?
                ORDER BY created ASC LIMIT ?
                """,
                (now - min_age_hours * 3600, now - 96 * 3600, max_snapshots),
            ).fetchall()

        updated_rewards: List[Dict[str, Any]] = []
        for row in due:
            try:
                metrics = self.x_manager.get_tweet_analytics(row["post_url"])
                impressions = max(int(metrics.get("impressions") or 100), 100)
                weighted = sum(float(metrics.get(k) or 0) * w for k, w in X_REWARD_WEIGHTS.items())
                per_1k = 1000.0 * weighted / impressions
                reward = round(per_1k / (50.0 + per_1k), 4)
                with self._conn() as conn:
                    conn.execute(
                        "UPDATE x_creator_posts SET metrics_24h = ?, reward = ?, measured_at = ? WHERE id = ?",
                        (json.dumps(metrics), reward, time.time(), row["id"]),
                    )
                    conn.execute(
                        """
                        UPDATE x_creator_bandit
                        SET pulls = pulls + 1, reward_sum = reward_sum + ?, updated = ?
                        WHERE arm = ?
                        """,
                        (reward, time.time(), row["arm"]),
                    )
                updated_rewards.append({"id": row["id"], "arm": row["arm"], "reward": reward, "metrics": metrics})
            except Exception as exc:
                logger.debug("X analytics snapshot skipped for %s: %s", row["post_url"], exc)
        return updated_rewards

    def snapshot_own_account_growth(self, own_handle: str) -> Optional[Dict[str, Any]]:
        """Record @InfoZimInvader's live follower & following counts on X (at most once every 3 hours)."""
        now = time.time()
        with self._conn() as conn:
            last = conn.execute("SELECT recorded, followers, following FROM x_account_growth ORDER BY recorded DESC LIMIT 1").fetchone()
            if last and (now - float(last["recorded"])) < 10800:
                return {"handle": own_handle, "followers": last["followers"], "following": last["following"], "cached": True}
        try:
            clean_h = own_handle.lstrip("@")
            page = self.x_manager._ensure_browser_page(target_url=f"https://x.com/{clean_h}")
            time.sleep(1.8)
            counts = page.evaluate(
                """() => {
                    const follEl = document.querySelector('a[href$="/verified_followers"], a[href$="/followers"]');
                    const fwEl = document.querySelector('a[href$="/following"]');
                    return {
                        followers_text: follEl ? follEl.innerText : '',
                        following_text: fwEl ? fwEl.innerText : ''
                    };
                }"""
            )
            followers = parse_metric_text((counts or {}).get("followers_text", ""))
            following = parse_metric_text((counts or {}).get("following_text", ""))
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO x_account_growth(handle, followers, following, recorded) VALUES(?, ?, ?, ?)",
                    (own_handle, followers, following, now),
                )
            return {"handle": own_handle, "followers": followers, "following": following, "cached": False}
        except Exception as exc:
            logger.debug("Account growth snapshot skipped: %s", exc)
            return None

    def like_viral_post_on_page(self) -> bool:
        """Click Like on the primary tweet currently open on screen to train X's For You feed & notify the creator."""
        allowed, _ = self.x_manager.rate_limiter.check_quota("tweet_like")
        if not allowed:
            return False
        try:
            page = self.x_manager._ensure_browser_page()
            article = page.query_selector('article[data-testid="tweet"]')
            if not article:
                return False
            like_btn = article.query_selector('[data-testid="like"]')
            if like_btn and like_btn.is_visible():
                like_btn.click()
                time.sleep(0.6)
                self.x_manager.rate_limiter.record_action("tweet_like")
                return True
        except Exception:
            pass
        return False

    def run_creator_cycle(self, auto_publish: bool = True) -> Dict[str, Any]:
        """Organic X loop: explore trends & popular posts -> study a creator -> evolve self-brand -> reply & post."""
        status = self.x_manager.get_status()
        if not status.get("x_authenticated"):
            return {"status": "blocked", "reason": f"X.com not authenticated: {status.get('detail')}", "x_status": status}

        own_handle = str(status.get("handle") or "@InfoZimInvader")
        growth_snap = self.snapshot_own_account_growth(own_handle)
        rewards_collected = self.collect_due_rewards(min_age_hours=4.0, max_snapshots=2)

        # 1. Explore X organically (For You + Explore Trending)
        explored = self.explore_x_organically(own_handle=own_handle)
        trends = explored.get("trends") or []
        viral_posts = explored.get("viral_posts") or []

        # 2. Study up to 2 popular creators seen this cycle (+ follow if within daily cap) to grow Sabrina's Style DNA library
        existing_handles = {r["handle"].lower() for r in self.get_top_role_models(limit=40)}
        creator_candidates = [vp for vp in viral_posts if str(vp.get("author") or "").lower() not in existing_handles]
        if not creator_candidates and viral_posts:
            creator_candidates = [viral_posts[0]]

        studied_creators: List[Dict[str, Any]] = []
        seen_creator_handles: set[str] = set()
        for vp in creator_candidates:
            h_low = str(vp.get("author") or "").lower()
            if not h_low or h_low in seen_creator_handles:
                continue
            seen_creator_handles.add(h_low)
            sc = self.study_popular_creator(vp, auto_follow=auto_publish)
            if sc:
                studied_creators.append(sc)
            if len(studied_creators) >= 2:
                break
        studied_creator = studied_creators[0] if studied_creators else None

        role_models = self.get_top_role_models(limit=5)
        brand = self.evolve_self_brand(trends=trends, role_models=role_models)

        # 2b. Check inbound @mentions / replies on Sabrina's posts for real two-way conversation
        mentions_handled = self.respond_to_mentions(own_handle=own_handle, brand=brand, auto_publish=auto_publish, max_replies=3)

        reply_outcome: Dict[str, Any] = {"status": "skipped"}
        published_replies: List[Dict[str, Any]] = []
        post_outcome: Dict[str, Any] = {"status": "skipped"}
        repost_outcome: Dict[str, Any] = {"status": "skipped"}

        # 3. Reply to up to 2 popular live posts per cycle in learned creator style (up to 60/day)
        replies_today = self._recent_reply_count_today()
        can_reply, reply_quota_msg = self.x_manager.rate_limiter.check_quota("tweet_reply")
        max_replies_this_turn = 2
        if replies_today < 60 and can_reply:
            fresh_targets = []
            for cand in viral_posts:
                tkey = "reply:" + hashlib.sha256(cand["url"].split("?")[0].encode("utf-8")).hexdigest()[:14]
                if not self._already_used_topic(tkey):
                    fresh_targets.append({**cand, "topic_key": tkey})
                if len(fresh_targets) >= 6:
                    break

            if fresh_targets:
                last_err = ""
                for reply_target in fresh_targets:
                    can_rep_now, _ = self.x_manager.rate_limiter.check_quota("tweet_reply")
                    if not can_rep_now:
                        break
                    reply_arm = self.select_arm("reply")
                    reply_text = self.compose_imitated_reply(reply_target, role_models, brand, reply_arm)
                    if not auto_publish:
                        draft_item = {"status": "drafted_reply", "arm": reply_arm, "target": reply_target["url"], "content": reply_text}
                        published_replies.append(draft_item)
                        break
                    if not reply_text:
                        continue
                    try:
                        if published_replies:
                            time.sleep(random.uniform(3.5, 7.0))
                        pub = self.x_manager.reply_to_tweet(target_url=reply_target["url"], content=reply_text, approved=True)
                        liked = self.like_viral_post_on_page()
                        rec_id = "xr_" + hashlib.sha256(f"{time.time()}:{reply_target['url']}".encode()).hexdigest()[:12]
                        with self._conn() as conn:
                            conn.execute(
                                """
                                INSERT INTO x_creator_posts(id, kind, arm, topic_key, target_url, post_url, content, created)
                                VALUES(?, 'reply', ?, ?, ?, ?, ?, ?)
                                """,
                                (rec_id, reply_arm, reply_target["topic_key"], reply_target["url"], pub.get("post_url"), reply_text, time.time()),
                            )
                        rep_item = {
                            **pub,
                            "status": "published_reply",
                            "liked_target": liked,
                            "arm": reply_arm,
                            "imitated_from": [r["handle"] for r in role_models[:2]],
                        }
                        published_replies.append(rep_item)
                        if len(published_replies) >= max_replies_this_turn or (replies_today + len(published_replies)) >= 60:
                            break
                    except Exception as rep_exc:
                        last_err = str(rep_exc)
                        logger.warning("Reply skipped on %s (%s), trying next popular post...", reply_target["url"], rep_exc)
                        continue
                if published_replies:
                    reply_outcome = {**published_replies[0], "replies_in_cycle": len(published_replies)}
                elif last_err:
                    reply_outcome = {"status": "skipped", "reason": last_err}
            else:
                reply_outcome = {"status": "skipped", "reason": "No fresh unreplied viral posts in this scan"}
        else:
            reply_outcome = {"status": "skipped", "reason": reply_quota_msg if not can_reply else f"daily_reply_cap ({replies_today}/60)"}

        # 4. Publish an original organic post OR Quote-Post imitating studied creator style (up to 24/day, >= 25m spacing)
        cooldown_active, cd_reason = self._recent_post_cooldown_active(min_interval_seconds=1500, max_daily_posts=24)
        can_post, post_quota_msg = self.x_manager.rate_limiter.check_quota("tweet_post")
        if not cooldown_active and can_post and (viral_posts or trends):
            post_arm = self.select_arm("post")
            quote_cand = None
            if post_arm == "quote_viral_take" and viral_posts and hasattr(self.x_manager, "quote_tweet"):
                for vp in viral_posts:
                    qkey = "quote:" + hashlib.sha256(vp["url"].split("?")[0].encode("utf-8")).hexdigest()[:14]
                    if not self._already_used_topic(qkey):
                        quote_cand = {**vp, "topic_key": qkey}
                        break

            if quote_cand is not None:
                q_text = self.compose_quote_commentary(quote_cand, role_models, brand)
                if auto_publish and q_text:
                    pub_q = self.x_manager.quote_tweet(target_url=quote_cand["url"], commentary=q_text, approved=True)
                    rec_id = "xq_" + hashlib.sha256(f"{time.time()}:{quote_cand['topic_key']}".encode()).hexdigest()[:12]
                    with self._conn() as conn:
                        conn.execute(
                            """
                            INSERT INTO x_creator_posts(id, kind, arm, topic_key, target_url, post_url, content, created)
                            VALUES(?, 'post', 'quote_viral_take', ?, ?, ?, ?, ?)
                            """,
                            (rec_id, quote_cand["topic_key"], quote_cand["url"], pub_q.get("url"), q_text, time.time()),
                        )
                    post_outcome = {**pub_q, "status": "published_quote", "arm": "quote_viral_take"}
                else:
                    post_outcome = {"status": "drafted_quote", "arm": "quote_viral_take", "target": quote_cand["url"], "content": q_text}
            else:
                if post_arm == "quote_viral_take":
                    post_arm = "punchy_one_liner"
                insp = random.choice(viral_posts[:4]) if viral_posts else {"author": "", "text": trends[0]}
                seed_pool = list(trends) + [str(vp.get("text", ""))[:50] for vp in viral_posts[:4]]
                tkey = ""
                for seed_cand in seed_pool:
                    cand_key = "orgpost:" + hashlib.sha256(f"{time.strftime('%Y-%m-%d')}:{seed_cand}".encode("utf-8")).hexdigest()[:14]
                    if not self._already_used_topic(cand_key):
                        tkey = cand_key
                        break
                if not tkey:
                    slot_bucket = int(time.time() // 1500)
                    tkey = "orgpost:" + hashlib.sha256(f"{time.strftime('%Y-%m-%d')}:slot:{slot_bucket}".encode("utf-8")).hexdigest()[:14]

                post_text = self.compose_imitated_post(insp, trends, role_models, brand, post_arm)
                if auto_publish and post_text:
                    pub_post = self.x_manager.publish_tweet(content=post_text, approved=True)
                    rec_id = "xp_" + hashlib.sha256(f"{time.time()}:{tkey}".encode()).hexdigest()[:12]
                    with self._conn() as conn:
                        conn.execute(
                            """
                            INSERT INTO x_creator_posts(id, kind, arm, topic_key, target_url, post_url, content, created)
                            VALUES(?, 'post', ?, ?, ?, ?, ?, ?)
                            """,
                            (rec_id, post_arm, tkey, insp.get("url", ""), pub_post.get("url"), post_text, time.time()),
                        )
                    post_outcome = {
                        **pub_post,
                        "status": "published_post",
                        "arm": post_arm,
                        "inspired_by_trends": trends[:3],
                        "role_models_used": [r["handle"] for r in role_models[:3]],
                    }
                else:
                    post_outcome = {"status": "drafted_post", "arm": post_arm, "content": post_text}
        else:
            post_outcome = {"status": "skipped", "reason": cd_reason if cooldown_active else post_quota_msg}

        # 5. Actively repost fresh viral posts onto Sabrina's profile (80% chance during post cooldown, 45% otherwise)
        repost_prob = 0.80 if cooldown_active else 0.45
        can_rt, _ = self.x_manager.rate_limiter.check_quota("tweet_retweet")
        if auto_publish and can_rt and viral_posts and hasattr(self.x_manager, "repost_tweet") and random.random() < repost_prob:
            for vp in viral_posts[:6]:
                rt_key = "repost:" + hashlib.sha256(vp["url"].split("?")[0].encode("utf-8")).hexdigest()[:14]
                if not self._already_used_topic(rt_key):
                    rt_res = self.x_manager.repost_tweet(vp["url"], approved=True)
                    if rt_res.get("status") == "reposted":
                        rec_id = "xrt_" + hashlib.sha256(f"{time.time()}:{rt_key}".encode()).hexdigest()[:12]
                        with self._conn() as conn:
                            conn.execute(
                                """
                                INSERT INTO x_creator_posts(id, kind, arm, topic_key, target_url, post_url, content, created)
                                VALUES(?, 'repost', 'repost_boost', ?, ?, ?, ?, ?)
                                """,
                                (rec_id, rt_key, vp["url"], vp["url"], f"Reposted {vp.get('author', '')}", time.time()),
                            )
                    repost_outcome = rt_res
                    break

        return {
            "status": "success",
            "handle": own_handle,
            "account_growth": growth_snap,
            "trends_discovered": trends,
            "studied_creator": studied_creator["handle"] if studied_creator else None,
            "studied_creators": [sc["handle"] for sc in studied_creators],
            "followed_creator": studied_creator.get("follow_result") if studied_creator else None,
            "mentions_replied": mentions_handled,
            "brand_generation": brand.get("generation"),
            "brand_voice": brand.get("voice_summary"),
            "rewards_updated": len(rewards_collected),
            "reply": reply_outcome,
            "replies": published_replies,
            "post": post_outcome,
            "repost": repost_outcome,
        }
