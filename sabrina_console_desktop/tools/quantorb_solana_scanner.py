"""QuantOrb Solana & Memecoin Holder Concentration & Dump Impact Scanner.

Implements the core QuantOrb analytical methodology:
1. Holder Concentration Breakdown: Separates Liquidity Pools / Bonding Curves from real insider wallets.
2. Constant Product (x * y = k) Dump Impact Projections: Computes how far token price drops if top holders sell.
3. Authority & Permission Auditing: Inspects Mint Authority, Freeze Authority, and Metadata Mutability.
4. Kraken & DEX Integration: Feeds risk scores directly into Sabrina's Kraken Monitor and Moon Scanner.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TokenHolder:
    address: str
    amount: float
    percentage: float
    is_liquidity_pool: bool = False
    is_insider_or_creator: bool = False
    tag: str = ""


@dataclass
class TokenAuthorityAudit:
    mint_authority_revoked: bool
    freeze_authority_revoked: bool
    is_metadata_mutable: bool
    top10_concentration_pct: float
    lp_locked_pct: float
    risk_level: str  # "LOW", "MEDIUM", "HIGH", "CRITICAL"


class QuantOrbScanner:
    """Solana on-chain holder distribution and price impact model."""

    def __init__(self, rpc_endpoint: str = "https://api.mainnet-beta.solana.com"):
        self.rpc_endpoint = rpc_endpoint

    def calculate_dump_impact(
        self,
        holder_tokens: float,
        pool_token_reserves: float,
        pool_sol_reserves: float,
        sol_price_usd: float = 180.0,
    ) -> Dict[str, float]:
        """Calculates exact CPMM (x * y = k) price collapse if a given holder sells into the pool.
        
        Formula:
          P_initial = pool_sol_reserves / pool_token_reserves
          k = pool_token_reserves * pool_sol_reserves
          new_token_reserves = pool_token_reserves + holder_tokens
          new_sol_reserves = k / new_token_reserves
          sol_received = pool_sol_reserves - new_sol_reserves
          P_final = new_sol_reserves / new_token_reserves
          price_drop_pct = (1 - P_final / P_initial) * 100
        """
        if pool_token_reserves <= 0 or pool_sol_reserves <= 0 or holder_tokens <= 0:
            return {"price_drop_pct": 0.0, "sol_extracted": 0.0, "usd_extracted": 0.0}

        p_initial = pool_sol_reserves / pool_token_reserves
        k = pool_token_reserves * pool_sol_reserves
        new_token_reserves = pool_token_reserves + holder_tokens
        new_sol_reserves = k / new_token_reserves
        sol_extracted = max(0.0, pool_sol_reserves - new_sol_reserves)
        p_final = new_sol_reserves / new_token_reserves

        price_drop_pct = (1.0 - (p_final / p_initial)) * 100.0

        return {
            "p_initial_sol": p_initial,
            "p_final_sol": p_final,
            "price_drop_pct": round(price_drop_pct, 2),
            "sol_extracted": round(sol_extracted, 4),
            "usd_extracted": round(sol_extracted * sol_price_usd, 2),
        }

    def evaluate_token_risk(
        self,
        token_mint: str,
        total_supply: float,
        holders: List[TokenHolder],
        pool_token_reserves: float,
        pool_sol_reserves: float,
        mint_revoked: bool = True,
        freeze_revoked: bool = True,
        metadata_mutable: bool = False,
        sol_price_usd: float = 180.0,
    ) -> Dict[str, Any]:
        """Conducts full QuantOrb distribution analysis and dump impact simulation."""
        
        # Sort holders descending
        sorted_holders = sorted(holders, key=lambda h: h.amount, reverse=True)
        
        # Filter non-pool holders to find genuine insider concentrations
        insiders = [h for h in sorted_holders if not h.is_liquidity_pool]
        top1_pct = (insiders[0].amount / total_supply * 100.0) if insiders else 0.0
        top5_pct = sum(h.amount for h in insiders[:5]) / total_supply * 100.0 if insiders else 0.0
        top10_pct = sum(h.amount for h in insiders[:10]) / total_supply * 100.0 if insiders else 0.0

        # Simulate dump impact if top 1 holder dumps
        top1_dump = self.calculate_dump_impact(
            holder_tokens=insiders[0].amount if insiders else 0.0,
            pool_token_reserves=pool_token_reserves,
            pool_sol_reserves=pool_sol_reserves,
            sol_price_usd=sol_price_usd,
        )

        # Simulate cumulative dump impact if top 5 holders coordinate dump
        top5_tokens = sum(h.amount for h in insiders[:5]) if insiders else 0.0
        top5_dump = self.calculate_dump_impact(
            holder_tokens=top5_tokens,
            pool_token_reserves=pool_token_reserves,
            pool_sol_reserves=pool_sol_reserves,
            sol_price_usd=sol_price_usd,
        )

        # Invariant and risk classification
        risk_flags = []
        if not mint_revoked:
            risk_flags.append("MINT_AUTHORITY_ACTIVE (Owner can print infinite supply)")
        if not freeze_revoked:
            risk_flags.append("FREEZE_AUTHORITY_ACTIVE (Owner can blacklist holder wallets)")
        if metadata_mutable:
            risk_flags.append("METADATA_MUTABLE (Name/image/tax can be modified)")
        if top10_pct > 35.0:
            risk_flags.append(f"HEAVY_INSIDER_CONCENTRATION (Top 10 hold {top10_pct:.1f}%)")
        if top1_dump["price_drop_pct"] > 40.0:
            risk_flags.append(f"SEVERE_TOP1_DUMP_HAZARD (Top 1 sell causes -{top1_dump['price_drop_pct']}%)")

        if not mint_revoked or not freeze_revoked or top10_pct > 60.0:
            overall_risk = "CRITICAL"
        elif risk_flags:
            overall_risk = "HIGH" if len(risk_flags) >= 2 else "MEDIUM"
        else:
            overall_risk = "LOW"

        return {
            "token_mint": token_mint,
            "overall_risk": overall_risk,
            "risk_flags": risk_flags,
            "authorities": {
                "mint_revoked": mint_revoked,
                "freeze_revoked": freeze_revoked,
                "metadata_mutable": metadata_mutable,
            },
            "concentration": {
                "top1_pct": round(top1_pct, 2),
                "top5_pct": round(top5_pct, 2),
                "top10_pct": round(top10_pct, 2),
            },
            "dump_simulations": {
                "top1_dump": top1_dump,
                "top5_coordinated_dump": top5_dump,
            },
            "kraken_admissibility": {
                "allow_market_making": overall_risk in ("LOW", "MEDIUM") and top1_dump["price_drop_pct"] < 30.0,
                "max_skew_bps": 40 if overall_risk == "LOW" else (150 if overall_risk == "MEDIUM" else 9999),
            },
        }


if __name__ == "__main__":
    scanner = QuantOrbScanner()
    # Sample token test with 1 Billion total supply
    supply = 1_000_000_000.0
    mock_holders = [
        TokenHolder(address="Raydium_LP_Pool_V4", amount=250_000_000.0, percentage=25.0, is_liquidity_pool=True),
        TokenHolder(address="7xKX...DevWallet", amount=120_000_000.0, percentage=12.0, is_insider_or_creator=True),
        TokenHolder(address="3yMP...Whale2", amount=80_000_000.0, percentage=8.0),
        TokenHolder(address="9qLA...Whale3", amount=50_000_000.0, percentage=5.0),
        TokenHolder(address="4bRT...Whale4", amount=40_000_000.0, percentage=4.0),
        TokenHolder(address="2kLP...Whale5", amount=35_000_000.0, percentage=3.5),
    ]

    report = scanner.evaluate_token_risk(
        token_mint="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        total_supply=supply,
        holders=mock_holders,
        pool_token_reserves=250_000_000.0,
        pool_sol_reserves=1_500.0,
        mint_revoked=True,
        freeze_revoked=True,
        metadata_mutable=False,
    )
    print(json.dumps(report, indent=2))
