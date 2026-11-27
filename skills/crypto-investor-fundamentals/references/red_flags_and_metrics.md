# Deterministic Red Flags, Formulas & Metric Traps

Use these formulas and thresholds to assign `PASS`, `WARN`, or `FAIL` across all five pillars. When a required input is missing, never substitute a guess—add the field to `missing_data` and cap the pillar at `WARN` (or `FAIL` for missing security/unlock disclosures).

---

## 1. Core Formulas

| Metric | Formula | Interpretation |
| --- | --- | --- |
| **Circulating Float (%)** | `(circulating_supply / max_or_total_supply) * 100` | Measures unlocked supply vs. total overhang. |
| **FDV / MCap Ratio** | `fdv / market_cap` | Inverse of float; ratios `> 3.0` signal heavy future dilution. |
| **12-Month Unlock Overhang (%)** | `(tokens_unlocking_next_12m / circulating_supply) * 100` | Supply shock relative to existing liquid float (not total supply). |
| **Annualized Net Holder Value ($)** | `holder_revenue_usd - annualized_emissions_usd` | True economic accrual after dilution. Negative = net extractive. |
| **Revenue-to-Emissions Ratio** | `annualized_protocol_revenue_usd / annualized_emissions_usd` | `< 1.0` means the protocol pays more in inflation than it earns in fees. |
| **FDV / Protocol Revenue (P/Rev)** | `fdv / annualized_protocol_revenue_usd` | Valuation multiple normalized for full dilution. |
| **Fee per Active Address ($)** | `daily_fees_usd / daily_active_addresses` | Filters bot/sybil address spam from real economic users. |
| **MVRV Ratio** | `market_cap / realized_cap` | Historically overheated `> 3.0–3.5`, distressed/value `< 1.0`. |
| **NVT Ratio** | `market_cap / annualized_onchain_tx_volume_usd` | High NVT = valuation outpacing economic settlement. |

---

## 2. Deterministic Threshold Matrix

| Dimension | `PASS` | `WARN` | `FAIL` |
| --- | --- | --- | --- |
| **Circulating Float** | `>= 50%` | `20% – 49.9%` | `< 20%` (especially with insider cliffs) |
| **FDV / MCap Ratio** | `<= 2.0x` | `2.0x – 5.0x` | `> 5.0x` |
| **12M Unlock Overhang (vs. Float)** | `< 15%` | `15% – 34.9%` | `>= 35%` |
| **Revenue / Emissions Ratio** | `>= 1.0x` (net positive or neutral) | `0.3x – 0.99x` (subsidized growth, improving trend) | `< 0.3x` with flat/declining fees |
| **Top-10 Non-Custodial Whale Share** | `< 25%` of circulating | `25% – 45%` | `> 45%` or undisclosed team wallets |
| **Admin / Upgrade Control** | Immutable or `>= 48h` timelock + multisig (`>= 3/5`) | `< 48h` timelock or small multisig | Unilateral EOA admin key with mint/pause/upgrade rights |
| **Smart-Contract Audits** | `>= 2` reputable audits covering current deployed commit | Single audit or minor un-audited upgrade | No audit, self-audit only, or unverified bytecode |
| **Exit Liquidity (2% Depth)** | `>= 10x` planned position size | `3x – 9.9x` planned position size | `< 3x` planned position size or evaporating depth |

---

## 3. Common Metric Traps (Mandatory Checks)

1. **The Low-Float / High-FDV Illusion:**
   - *Trap:* Market cap looks "cheap" relative to peers (`$150M` MCap), while only `12%` of tokens circulate (`$1.25B` FDV).
   - *Check:* Always value on **FDV** and measure upcoming unlocks against **current circulating float**, not total supply (`40%` of total supply unlocking into a `12%` float is a `+333%` float expansion).
2. **The Fake Yield (Inflationary Staking) Trap:**
   - *Trap:* Protocol advertises `35%` staking APY, attracting retail buyers who ignore `45%` annualized token emissions.
   - *Check:* Real yield = `holder_fee_share_apy - net_dilution_rate`. If `net_holder_value_usd < 0`, stakers are merely reducing their own dilution relative to non-stakers.
3. **Price-Driven TVL Inflation:**
   - *Trap:* USD TVL doubles during a bull market while native token deposits (`ETH`, `SOL`, `USDC`) are flat or falling.
   - *Check:* Always inspect TVL in **native asset units** alongside USD TVL and compare `Protocol Revenue / TVL` (capital efficiency).
4. **Vanity Active Addresses (Sybil / Airdrop Farming):**
   - *Trap:* Daily active addresses surge `4x` ahead of a points campaign while total fees paid and DEX liquidity remain flat or shrink.
   - *Check:* Divide daily fees by daily active addresses (`Fee/DAA`). Collapsing `Fee/DAA` confirms low-value farming rather than sticky demand.
5. **Buyback Theater:**
   - *Trap:* Protocol announces a `$10M` annual buyback while issuing `$60M` in team/ecosystem emissions.
   - *Check:* Compute net supply change (`buybacks_usd - emissions_usd = -$50M` net dilution).
6. **Sector-Mismatched Multiples:**
   - *Trap:* Comparing an L1's P/Rev directly to a DeFi DEX's P/Rev without accounting for monetary/collateral premium vs. cash-flow equity-like multiples.
   - *Check:* Compare within peer groups (L1 vs. L1, DEX vs. DEX, Lending vs. Lending, DePIN vs. DePIN, Stablecoin issuers vs. T-bill yield spread).
