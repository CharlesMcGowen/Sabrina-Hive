# -*- coding: utf-8 -*-
"""
tools/crypto_scout.py
=====================
Social momentum & dog-pile scanner for crypto tokens across X.com.

Extracts cashtags ($TICKER), Solana mints (Base58), EVM addresses (0x...),
calculates sybil-resistant author diversity, and triggers dog-pile alerts.
"""

import re
import time
import logging
from typing import Dict, Any, List, Optional, Set
from tools.dex_pair_auditor import DexPairAuditor

logger = logging.getLogger("sabrina.crypto.scout")

# Regex patterns for crypto identifiers
RE_CASHTAG = re.compile(r'\$([A-Za-z0-9]{2,10})\b')
RE_SOLANA_CA = re.compile(r'\b([1-9A-HJ-NP-Za-km-z]{32,44}(?:pump)?)\b')
RE_EVM_CA = re.compile(r'\b(0x[a-fA-F0-9]{40})\b')
RE_DEXSCREENER_URL = re.compile(r'dexscreener\.com/([a-zA-Z0-9_\-]+)/([a-zA-Z0-9_\-]+)')
RE_PUMPFUN_URL = re.compile(r'pump\.fun/([1-9A-HJ-NP-Za-km-z]+)')

# Common non-token tickers / false positives to ignore
CASHTAG_BLACKLIST = {"USD", "USDT", "USDC", "EUR", "GBP", "JPY", "CAD", "AUD", "CEO", "AI", "API", "ATH", "DEX", "CEX", "NFT", "ETH", "BTC"}


class XCryptoScout:
    """Discovers, parses, and scores viral social momentum on crypto tokens."""

    def __init__(self, manager=None, auditor: Optional[DexPairAuditor] = None):
        self.manager = manager
        self.auditor = auditor or DexPairAuditor()

    @staticmethod
    def extract_identifiers(text: str) -> Dict[str, List[str]]:
        """Extracts cashtags, contract addresses, and DEX links from text."""
        raw_text = text or ""
        cashtags = [
            c.upper() for c in RE_CASHTAG.findall(raw_text)
            if c.upper() not in CASHTAG_BLACKLIST
        ]
        evm_cas = RE_EVM_CA.findall(raw_text)
        
        # Filter solana CAs to avoid matching common english words that happen to be base58
        potential_sol = RE_SOLANA_CA.findall(raw_text)
        solana_cas = [
            s for s in potential_sol
            if (len(s) >= 32 and (s.endswith("pump") or not s.isalpha() or len(s) >= 43))
        ]

        dex_links = []
        for match in RE_DEXSCREENER_URL.findall(raw_text):
            dex_links.append(f"{match[0]}:{match[1]}")

        for match in RE_PUMPFUN_URL.findall(raw_text):
            solana_cas.append(match)

        return {
            "cashtags": list(set(cashtags)),
            "solana_cas": list(set(solana_cas)),
            "evm_cas": list(set(evm_cas)),
            "dex_links": list(set(dex_links)),
        }

    def aggregate_social_pulses(self, posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Aggregates raw social posts into token clusters with sybil-resistant metrics:
        - Mentions count
        - Unique author count & diversity ratio
        - Aggregate engagement (replies, reposts, likes)
        - Dog-pile momentum flag
        """
        clusters: Dict[str, Dict[str, Any]] = {}

        for post in posts:
            author = post.get("author", "@unknown")
            text = post.get("text", "")
            metrics = post.get("metrics", {})
            post_url = post.get("url", "")
            post_time = post.get("created_at") or time.time()

            ids = self.extract_identifiers(text)
            # Unified token keys to attribute to: prefer CA over cashtag
            targets = []
            if ids["solana_cas"]:
                targets.extend([(ca, "solana_ca") for ca in ids["solana_cas"]])
            if ids["evm_cas"]:
                targets.extend([(ca, "evm_ca") for ca in ids["evm_cas"]])
            if ids["cashtags"] and not targets:
                targets.extend([(tag, "cashtag") for tag in ids["cashtags"]])

            for token_key, token_type in targets:
                if token_key not in clusters:
                    clusters[token_key] = {
                        "token_key": token_key,
                        "token_type": token_type,
                        "mentions": 0,
                        "authors": set(),
                        "posts": [],
                        "total_likes": 0,
                        "total_reposts": 0,
                        "total_replies": 0,
                        "first_seen": post_time,
                        "last_seen": post_time,
                    }

                c = clusters[token_key]
                c["mentions"] += 1
                c["authors"].add(author)
                c["total_likes"] += int(metrics.get("likes") or 0)
                c["total_reposts"] += int(metrics.get("reposts") or 0)
                c["total_replies"] += int(metrics.get("replies") or 0)
                c["first_seen"] = min(c["first_seen"], post_time)
                c["last_seen"] = max(c["last_seen"], post_time)
                c["posts"].append({
                    "author": author,
                    "text": text[:120],
                    "url": post_url,
                    "time": post_time,
                })

        # Calculate diversity and momentum indicators
        results = []
        for token_key, c in clusters.items():
            author_count = len(c["authors"])
            mentions = c["mentions"]
            diversity_ratio = author_count / mentions if mentions > 0 else 0.0

            # Dog-pile condition: At least 3 mentions, >= 70% unique author diversity
            is_dog_pile = (mentions >= 3 and diversity_ratio >= 0.70)
            is_sybil_spam = (mentions >= 3 and diversity_ratio < 0.40)

            results.append({
                "token_key": token_key,
                "token_type": c["token_type"],
                "mentions": mentions,
                "unique_authors": author_count,
                "diversity_ratio": round(diversity_ratio, 3),
                "is_dog_pile": is_dog_pile,
                "is_sybil_spam": is_sybil_spam,
                "engagement": {
                    "likes": c["total_likes"],
                    "reposts": c["total_reposts"],
                    "replies": c["total_replies"],
                },
                "first_seen": c["first_seen"],
                "last_seen": c["last_seen"],
                "sample_posts": c["posts"][:3],
            })

        # Sort by mentions descending, then unique authors
        results.sort(key=lambda x: (x["is_dog_pile"], x["mentions"], x["unique_authors"]), reverse=True)
        return results

    def scout_and_audit(self, query: str = "$SOL", max_posts: int = 15) -> List[Dict[str, Any]]:
        """
        Executes an end-to-end information gathering scout:
        1. Searches X.com for live technical / crypto posts via XManager.
        2. Aggregates tokens and detects dog-pile clusters.
        3. Cross-references discovered tokens against DexScreener for live pair telemetry.
        """
        if not self.manager:
            raise RuntimeError("XManager instance required for live search scouting.")

        logger.info("Scouting X.com for query %r (max_posts=%d)...", query, max_posts)
        posts = self.manager.search_tweets(query=query, max_posts=max_posts, live=True)
        logger.info("Retrieved %d raw posts from X.com. Aggregating tokens...", len(posts))

        clusters = self.aggregate_social_pulses(posts)
        audited_opportunities = []

        for cluster in clusters:
            token_key = cluster["token_key"]
            logger.info("Auditing pair telemetry for token %s (%s)...", token_key, cluster["token_type"])
            pair_report = self.auditor.find_best_pair_for_token(token_key)

            audited_opportunities.append({
                "social": cluster,
                "onchain_pair": pair_report,
            })

        return audited_opportunities
