import json
import numpy as np
from datetime import datetime, timezone, timedelta

with open('outputs/tread_detailed_analysis.json') as f:
    d = json.load(f)

# Current time and 24h window
now_ts = datetime.now(timezone.utc).timestamp()
day_ago_ts = now_ts - 24 * 3600

# 1. 24h OHLCV Analysis (from 15m bars)
bars_15m = d['bars_15m']
# format: [time, open, high, low, close, vwap, volume, count]
bars_24h = [b for b in bars_15m if float(b[0]) >= day_ago_ts]

if not bars_24h:
    # fallback to last 96 bars (96 * 15m = 24h)
    bars_24h = bars_15m[-96:]

opens = [float(b[1]) for b in bars_24h]
highs = [float(b[2]) for b in bars_24h]
lows = [float(b[3]) for b in bars_24h]
closes = [float(b[4]) for b in bars_24h]
volumes = [float(b[6]) for b in bars_24h]
vwaps = [float(b[5]) for b in bars_24h]
turnovers = [float(b[5]) * float(b[6]) for b in bars_24h]

p_open_24h = opens[0]
p_high_24h = max(highs)
p_low_24h = min(lows)
p_current = closes[-1]
tot_volume_token = sum(volumes)
tot_volume_usd = sum(turnovers)

# Peak gain from 24h low and from 24h open
peak_gain_from_open = (p_high_24h / p_open_24h - 1) * 100
peak_gain_from_low = (p_high_24h / p_low_24h - 1) * 100
current_gain_from_open = (p_current / p_open_24h - 1) * 100
pullback_from_peak = (p_current / p_high_24h - 1) * 100

# 2. Spread Analysis
spreads_raw = d['spreads']
# format: [time, bid, ask]
spread_points = []
for s in spreads_raw:
    t = float(s[0])
    b = float(s[1])
    a = float(s[2])
    if b > 0 and a > 0 and a >= b:
        sp_usd = a - b
        mid = (a + b) / 2.0
        sp_bps = (sp_usd / mid) * 10000.0
        sp_pct = (sp_usd / mid) * 100.0
        spread_points.append({'ts': t, 'bid': b, 'ask': a, 'mid': mid, 'usd': sp_usd, 'bps': sp_bps, 'pct': sp_pct})

spread_usd_list = [s['usd'] for s in spread_points]
spread_bps_list = [s['bps'] for s in spread_points]
spread_pct_list = [s['pct'] for s in spread_points]

mean_spread_usd = np.mean(spread_usd_list)
median_spread_usd = np.median(spread_usd_list)
min_spread_usd = np.min(spread_usd_list)
max_spread_usd = np.max(spread_usd_list)

mean_spread_bps = np.mean(spread_bps_list)
median_spread_bps = np.median(spread_bps_list)
min_spread_bps = np.min(spread_bps_list)
max_spread_bps = np.max(spread_bps_list)

current_spread = spread_points[-1]

# 3. Order Book Depth & Slippage
depth = d['depth']
bids = [[float(p), float(v)] for p, v, _ in depth.get('bids', [])]
asks = [[float(p), float(v)] for p, v, _ in depth.get('asks', [])]

def simulate_market_order(levels, notional_usd, is_buy):
    rem_usd = notional_usd
    total_token = 0.0
    weighted_px = 0.0
    for px, qty in levels:
        level_usd = px * qty
        if rem_usd <= level_usd:
            qty_taken = rem_usd / px
            total_token += qty_taken
            weighted_px += rem_usd
            rem_usd = 0.0
            break
        else:
            total_token += qty
            weighted_px += level_usd
            rem_usd -= level_usd
    if rem_usd > 0:
        return None, None, f"Insufficient depth for ${notional_usd}"
    avg_price = notional_usd / total_token if total_token > 0 else 0
    ref_price = levels[0][0]
    slip_pct = abs(avg_price / ref_price - 1.0) * 100.0
    slip_bps = slip_pct * 100.0
    return avg_price, slip_pct, slip_bps

sim_sizes = [50, 100, 250, 500, 1000]
sim_results = {}
for sz in sim_sizes:
    sell_avg_px, sell_slip_pct, sell_slip_bps = simulate_market_order(bids, sz, is_buy=False)
    buy_avg_px, buy_slip_pct, buy_slip_bps = simulate_market_order(asks, sz, is_buy=True)
    sim_results[sz] = {
        'sell_avg_price': sell_avg_px,
        'sell_slippage_bps': sell_slip_bps,
        'buy_avg_price': buy_avg_px,
        'buy_slippage_bps': buy_slip_bps,
    }

# 4. Round Trip Cost Calculations
# Standard Kraken fees for Entry Tier / Base Tier:
# Maker: 0.25% (25 bps) or 0.40% (Sabrina standard assumption)
# Taker: 0.40% (40 bps)
# We test 3 fee regimes:
# 1) Sabrina / Freqtrade conservative default: 40 bps / 40 bps
# 2) Standard Kraken Tier 1 (<$50k vol): 25 bps maker / 40 bps taker
# 3) Zero-fee / High tier

fee_scenarios = [
    {"name": "Sabrina Model Standard (40 bps maker / 40 bps taker)", "maker_fee_bps": 40, "taker_fee_bps": 40},
    {"name": "Kraken Tier 1 (<$10k 30d vol: 25 bps maker / 40 bps taker)", "maker_fee_bps": 25, "taker_fee_bps": 40},
    {"name": "Kraken Tier 2 ($10k-$50k vol: 20 bps maker / 35 bps taker)", "maker_fee_bps": 20, "taker_fee_bps": 35},
]

# Calculate round-trip hurdle for:
# Scenario A: Market Sell + Market Buy back (Taker + Taker)
# Cost = 2 * taker_fee + 2 * slippage + spread
# Scenario B: Limit Sell (Maker) + Limit Buy back (Maker)
# Cost = 2 * maker_fee (no spread crossing, but execution risk)
# Scenario C: Market Sell (immediate exit) + Limit Buy back (Maker)

hurdles = []
for sc in fee_scenarios:
    mf = sc['maker_fee_bps']
    tf = sc['taker_fee_bps']
    
    # Taker/Taker roundtrip cost
    # Crosses spread once (or twice if exiting and re-entering via market)
    # Total friction bps: 2*tf + median_spread_bps
    taker_roundtrip_bps = 2 * tf + median_spread_bps
    taker_roundtrip_pct = taker_roundtrip_bps / 100.0
    
    # Maker/Maker roundtrip cost
    maker_roundtrip_bps = 2 * mf
    maker_roundtrip_pct = maker_roundtrip_bps / 100.0
    
    # Taker Sell + Maker Buy
    hybrid_roundtrip_bps = tf + mf + (median_spread_bps / 2.0)
    hybrid_roundtrip_pct = hybrid_roundtrip_bps / 100.0
    
    hurdles.append({
        'name': sc['name'],
        'maker_maker_cost_pct': maker_roundtrip_pct,
        'maker_maker_cost_bps': maker_roundtrip_bps,
        'taker_taker_cost_pct': taker_roundtrip_pct,
        'taker_taker_cost_bps': taker_roundtrip_bps,
        'hybrid_cost_pct': hybrid_roundtrip_pct,
        'hybrid_cost_bps': hybrid_roundtrip_bps,
    })

res = {
    'price_action': {
        'open_24h': p_open_24h,
        'high_24h': p_high_24h,
        'low_24h': p_low_24h,
        'current': p_current,
        'peak_gain_from_open_pct': peak_gain_from_open,
        'peak_gain_from_low_pct': peak_gain_from_low,
        'current_gain_from_open_pct': current_gain_from_open,
        'pullback_from_peak_pct': pullback_from_peak,
        'turnover_usd_24h': tot_volume_usd,
        'volume_token_24h': tot_volume_token,
    },
    'spread_stats': {
        'mean_usd': mean_spread_usd,
        'median_usd': median_spread_usd,
        'min_usd': min_spread_usd,
        'max_usd': max_spread_usd,
        'mean_bps': mean_spread_bps,
        'median_bps': median_spread_bps,
        'min_bps': min_spread_bps,
        'max_bps': max_spread_bps,
        'current_bid': current_spread['bid'],
        'current_ask': current_spread['ask'],
        'current_spread_usd': current_spread['usd'],
        'current_spread_bps': current_spread['bps'],
    },
    'depth_simulation': sim_results,
    'roundtrip_hurdles': hurdles
}

with open('outputs/tread_calc_results.json', 'w') as f:
    json.dump(res, f, indent=2)

print("Calculation complete! Results saved to outputs/tread_calc_results.json")
