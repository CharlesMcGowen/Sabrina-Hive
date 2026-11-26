# -*- coding: utf-8 -*-
"""
tools/dex_pair_auditor.py
=========================
Real-time DEX pair telemetry gathering, liquidity safety vetting,
and momentum analysis across Solana, Base, Ethereum, and BSC.

Powered by DexScreener's real-time feeds with zero API key requirement.
"""

import json
import time
import logging
import urllib.request
import urllib.parse
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("sabrina.crypto.dex_auditor")

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
MIN_LIQUIDITY_USD_DEFAULT = 10000.0  # $10k liquidity floor


class DexPairAuditor:
    """Audits on-chain liquidity, volume velocity, and transaction dynamics for crypto pairs."""

    def __init__(self, min_liquidity_usd: float = MIN_LIQUIDITY_USD_DEFAULT, timeout_sec: float = 8.0):
        self.min_liquidity_usd = min_liquidity_usd
        self.timeout_sec = timeout_sec
        self.base_url = "https://api.dexscreener.com"

    def _http_get_json(self, url: str) -> Optional[Any]:
        """Performs an HTTP GET with robust timeout and error handling."""
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                if resp.status == 200:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw)
        except Exception as e:
            logger.warning("DexScreener API GET failed for %s: %s", url, e)
        return None

    def get_token_pairs(self, token_address: str) -> List[Dict[str, Any]]:
        """Fetches all active DEX trading pairs for a given token contract/mint address."""
        clean_addr = token_address.strip()
        url = f"{self.base_url}/latest/dex/tokens/{clean_addr}"
        data = self._http_get_json(url)
        if data and isinstance(data, dict):
            return data.get("pairs") or []
        return []

    def get_pair_by_address(self, chain_id: str, pair_address: str) -> Optional[Dict[str, Any]]:
        """Fetches telemetry for a specific chain and pair contract address."""
        clean_chain = chain_id.strip().lower()
        clean_pair = pair_address.strip()
        url = f"{self.base_url}/latest/dex/pairs/{clean_chain}/{clean_pair}"
        data = self._http_get_json(url)
        if data and isinstance(data, dict):
            pairs = data.get("pairs") or []
            return pairs[0] if pairs else None
        return None

    def search_pairs(self, query: str) -> List[Dict[str, Any]]:
        """Searches pairs by token ticker, name, or address."""
        encoded = urllib.parse.quote(query.strip())
        url = f"{self.base_url}/latest/dex/search?q={encoded}"
        data = self._http_get_json(url)
        if data and isinstance(data, dict):
            return data.get("pairs") or []
        return []

    def get_trending_boosted_tokens(self) -> List[Dict[str, Any]]:
        """Fetches the latest trending/boosted tokens on DEXes."""
        url = f"{self.base_url}/token-boosts/top/v1"
        data = self._http_get_json(url)
        if data and isinstance(data, list):
            return data
        return []

    def audit_pair(self, pair: Dict[str, Any]) -> Dict[str, Any]:
        """
        Performs in-depth telemetry and risk analysis on a single DEX trading pair:
        - Liquidity depth and liquidity-to-FDV ratio
        - Volume velocity (5m volume relative to liquidity)
        - Buy/sell transaction distribution & honeypot risk
        - Price change momentum (5m, 1h, 24h)
        - Computes an aggregate Safety Score (0-100) and Momentum Grade
        """
        dex_id = pair.get("dexId", "unknown")
        chain_id = pair.get("chainId", "unknown")
        pair_address = pair.get("pairAddress", "")
        base_token = pair.get("baseToken", {})
        quote_token = pair.get("quoteToken", {})

        symbol = base_token.get("symbol", "UNKNOWN")
        name = base_token.get("name", "Unknown Token")
        token_address = base_token.get("address", "")
        quote_symbol = quote_token.get("symbol", "UNKNOWN")

        # Telemetry metrics
        price_usd = float(pair.get("priceUsd") or 0.0)
        fdv = float(pair.get("fdv") or 0.0)
        market_cap = float(pair.get("marketCap") or 0.0)

        liquidity_data = pair.get("liquidity") or {}
        liquidity_usd = float(liquidity_data.get("usd") or 0.0)

        volume_data = pair.get("volume") or {}
        vol_5m = float(volume_data.get("m5") or 0.0)
        vol_1h = float(volume_data.get("h1") or 0.0)
        vol_24h = float(volume_data.get("h24") or 0.0)

        price_change = pair.get("priceChange") or {}
        pct_5m = float(price_change.get("m5") or 0.0)
        pct_1h = float(price_change.get("h1") or 0.0)
        pct_24h = float(price_change.get("h24") or 0.0)

        txns_data = pair.get("txns") or {}
        txns_5m = txns_data.get("m5") or {}
        buys_5m = int(txns_5m.get("buys") or 0)
        sells_5m = int(txns_5m.get("sells") or 0)
        total_txns_5m = buys_5m + sells_5m

        # Safety & Risk Checks
        risk_flags = []
        is_safe = True

        # 1. Liquidity Floor Check
        if liquidity_usd < self.min_liquidity_usd:
            risk_flags.append(f"Low liquidity (${liquidity_usd:,.0f} < ${self.min_liquidity_usd:,.0f})")
            if liquidity_usd < 2000.0:
                is_safe = False

        # 2. Honeypot / Zero-Sell Check
        if buys_5m >= 15 and sells_5m == 0:
            risk_flags.append("POTENTIAL_HONEYPOT: 15+ buys with zero sells in last 5m")
            is_safe = False

        # 3. Buy Pressure Ratio
        buy_ratio_5m = (buys_5m / total_txns_5m) if total_txns_5m > 0 else 0.5
        if total_txns_5m >= 10 and buy_ratio_5m < 0.25:
            risk_flags.append(f"Heavy sell dumping ({sells_5m} sells vs {buys_5m} buys in 5m)")

        # 4. Volume-to-Liquidity Velocity
        vol_to_liq_5m = (vol_5m / liquidity_usd) if liquidity_usd > 0 else 0.0

        # Calculate Safety Score (0 to 100)
        safety_score = 100
        if liquidity_usd < self.min_liquidity_usd:
            safety_score -= 30
        if liquidity_usd < 5000.0:
            safety_score -= 30
        if "POTENTIAL_HONEYPOT" in str(risk_flags):
            safety_score = 0
        if buy_ratio_5m < 0.35 and total_txns_5m > 10:
            safety_score -= 20
        safety_score = max(0, min(100, safety_score))

        # Classify Momentum Grade
        if not is_safe or safety_score < 40:
            grade = "AVOID_HIGH_RISK"
        elif vol_to_liq_5m > 0.4 and pct_5m > 15.0 and buy_ratio_5m > 0.6:
            grade = "HIGH_MOMENTUM_BREAKOUT"
        elif pct_5m > 5.0 and buy_ratio_5m >= 0.55:
            grade = "STEADY_ACCUMULATION"
        elif pct_5m < -10.0 and sells_5m > buys_5m:
            grade = "DUMP_CORRECTION"
        elif pct_24h > 150.0 and pct_5m < 0:
            grade = "LATE_PUMP_EXHAUSTION"
        else:
            grade = "NEUTRAL_CONSOLIDATION"

        return {
            "symbol": symbol,
            "name": name,
            "chain_id": chain_id,
            "dex_id": dex_id,
            "pair_address": pair_address,
            "token_address": token_address,
            "quote_symbol": quote_symbol,
            "pair_name": f"{symbol}/{quote_symbol}",
            "url": pair.get("url") or f"https://dexscreener.com/{chain_id}/{pair_address}",
            "price_usd": price_usd,
            "fdv": fdv,
            "market_cap": market_cap,
            "liquidity_usd": liquidity_usd,
            "volume_5m": vol_5m,
            "volume_1h": vol_1h,
            "volume_24h": vol_24h,
            "price_change_5m": pct_5m,
            "price_change_1h": pct_1h,
            "price_change_24h": pct_24h,
            "txns_5m": {
                "buys": buys_5m,
                "sells": sells_5m,
                "total": total_txns_5m,
                "buy_ratio": round(buy_ratio_5m, 3),
            },
            "vol_to_liq_5m": round(vol_to_liq_5m, 3),
            "safety_score": safety_score,
            "grade": grade,
            "risk_flags": risk_flags,
            "is_safe_to_monitor": is_safe,
            "audited_at": time.time(),
        }

    def find_best_pair_for_token(self, token_address_or_symbol: str) -> Optional[Dict[str, Any]]:
        """Resolves token address or symbol and returns the audited highest-liquidity pair."""
        pairs = []
        # If it looks like an on-chain address (base58 or 0x hex)
        query = token_address_or_symbol.strip()
        if len(query) >= 30:
            pairs = self.get_token_pairs(query)

        if not pairs:
            pairs = self.search_pairs(query)

        if not pairs:
            return None

        # Sort by liquidity USD descending
        pairs_sorted = sorted(
            pairs,
            key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0),
            reverse=True
        )
        return self.audit_pair(pairs_sorted[0])
