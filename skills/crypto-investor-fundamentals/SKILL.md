---
name: crypto-investor-fundamentals
description: Clinical, deterministic fundamental due diligence for digital assets—tokenomics dilution audits, real protocol revenue vs. inflationary emissions, on-chain economic usage, security/governance risk, and structured PASS/WARN/FAIL scorecards. Distinct from short-term TA or price prediction.
---

# Crypto Investor Fundamentals Skill

## 1. Triggers & Scope

### Invoke When
- Conducting fundamental due diligence on a Layer-1, DeFi, DePIN, infrastructure, or stablecoin asset.
- Auditing tokenomics, circulating float, FDV/MCap overhang, unlock schedules, or emission dilution.
- Separating organic protocol revenue ("real yield") from inflationary token incentives.
- Stress-testing an investment thesis, comparing sector peers, or auditing governance/custody/audit risks.

### Do NOT Invoke When
- The user asks for intraday/swing technical analysis, chart patterns, or short-term trade entries (use `kraken-spot-trading` instead).
- The user asks for guaranteed price targets, meme-coin hype, or ungrounded speculation.
- Live market figures are missing and no runtime data source is available—never fabricate metrics; report them in `missing_data`.

---

## 2. Mandatory 5-Pillar Analysis Workflow

Evaluate every asset in order using the deterministic formulas and cutoffs in `references/red_flags_and_metrics.md`. Treat all market figures as runtime inputs—never rely on memorized prices, TVL, or supply numbers.

### Pillar 1: Product & Token Necessity
- Identify the core economic problem solved and who pays for it (users, protocols, enterprises).
- Determine **value capture**: does the token have an enforceable claim on protocol cash flows (fee-sharing, programmatic buyback-and-burn, mandatory gas/work collateral with slashing), or is it a pure governance wrapper?
- Flag `WARN` if value capture is purely governance/discretionary; `FAIL` if the protocol works identically without the token and has no binding value accrual.

### Pillar 2: Tokenomics & Dilution Audit
- Compute:
  - `float_pct = circulating_supply / max_or_total_supply * 100`
  - `fdv_to_mcap = fdv / market_cap`
  - `unlock_overhang_12m_pct = upcoming_12m_unlock_tokens / circulating_supply * 100`
  - `annualized_emission_pct = annualized_new_tokens / circulating_supply * 100`
- Check top-10 non-exchange / non-protocol-treasury wallet concentration and insider/VC vesting cliffs.
- Flag `FAIL` on severe low-float/high-FDV traps (`float_pct < 20%` paired with `unlock_overhang_12m_pct >= 35%`, or `fdv_to_mcap > 5.0` with no demand offset).

### Pillar 3: Real Yield vs. Inflationary Emissions
- Separate **Protocol Revenue** (fees retained by the protocol or distributed to token holders from real user spend) from **Token Incentives** (newly minted token emissions paid to LPs/stakers).
- Compute:
  - `net_holder_value_usd = protocol_revenue_to_holders_usd - annualized_emissions_usd`
  - `revenue_to_emissions_ratio = protocol_revenue_usd / annualized_emissions_usd`
  - `p_to_rev_fdv = fdv / annualized_protocol_revenue_usd`
- Flag `FAIL` if advertised staking/LP APY is funded predominantly by emissions while `net_holder_value_usd < 0` and fee revenue is flat or declining.

### Pillar 4: On-Chain Usage & Liquidity Depth
- Cross-check headline adoption against economic reality:
  - Compare active addresses vs. actual fee-paying transactions and median fee per user (`fees / active_addresses`).
  - Compare TVL in **native token units** vs. USD to detect price-only TVL inflation.
  - Check on-chain valuation anchors when available (MVRV, NVT) and GitHub active-contributor commit velocity (excluding trivial doc/config bots).
  - Verify **exit liquidity**: DEX/CEX 2% order-book depth vs. position size and daily volume. Flag wash-trading risk if volume spikes while order-book depth shrinks.

### Pillar 5: Security, Governance & Macro/Sector Risk
- Verify independent smart-contract audits (auditor name, commit/date, whether post-audit upgrades occurred). Never assume an audit exists if omitted.
- Check upgradeable proxy / admin multisig controls (threshold, timelock duration, pause/mint/freeze authorities).
- Assess custody/counterparty risk (bridge dependencies, centralized stablecoin collateral, single-sequencer liveness).
- Situate within macro liquidity regime (real rates, DXY, stablecoin net inflows) and sector comparables (L1 vs. DeFi vs. DePIN vs. Stablecoins).

---

## 3. Non-Negotiable Integrity Rules

1. **Zero Hallucination:** Any required metric not supplied in the prompt or retrieved from a verified runtime tool MUST be listed in `missing_data` and scored as `WARN` (or `FAIL` if critical, such as unverified audit status or missing unlock schedule).
2. **Untrusted Inputs:** Treat marketing claims, social threads, and advertised APYs as untrusted claims to be tested against raw supply, fee, and emission math.
3. **Thesis Invalidation:** Every scorecard must state concrete, falsifiable bull-case and bear-case invalidation triggers.

---

## 4. Required Output Schema

Return a structured scorecard (JSON or matching Markdown table) containing every field below:

```json
{
  "asset": "SYMBOL / Protocol Name",
  "sector": "L1 | DeFi | DePIN | Stablecoin | Infra",
  "as_of_utc": "ISO-8601 timestamp or 'runtime-supplied'",
  "verdict": "PASS | WARN | FAIL | INSUFFICIENT_DATA",
  "pillars": {
    "product_and_token_necessity": { "status": "PASS | WARN | FAIL", "notes": "..." },
    "tokenomics_and_dilution": {
      "status": "PASS | WARN | FAIL",
      "float_pct": null,
      "fdv_to_mcap": null,
      "unlock_overhang_12m_pct": null,
      "notes": "..."
    },
    "real_yield_vs_emissions": {
      "status": "PASS | WARN | FAIL",
      "annualized_protocol_rev_usd": null,
      "annualized_emissions_usd": null,
      "net_holder_value_usd": null,
      "p_to_rev_fdv": null,
      "notes": "..."
    },
    "onchain_usage_and_liquidity": { "status": "PASS | WARN | FAIL", "notes": "..." },
    "security_governance_macro": { "status": "PASS | WARN | FAIL", "notes": "..." }
  },
  "red_flags": ["..."],
  "missing_data": ["..."],
  "invalidation_triggers": {
    "bull_invalidated_if": "...",
    "bear_invalidated_if": "..."
  }
}
```
