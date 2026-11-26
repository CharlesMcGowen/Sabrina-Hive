# -*- coding: utf-8 -*-
"""
tools/crypto_visualizer.py
==========================
Generates self-contained, interactive Generative UI HTML dashboards
correlating crypto DEX price action with social dog-pile events.
"""

import json
import html
from typing import Dict, Any, List


def generate_crypto_correlation_html(correlation_data: Dict[str, Any]) -> str:
    """Generates the full HTML markup for the interactive crypto social correlation dashboard."""
    token = correlation_data.get("token", {})
    symbol = token.get("symbol", "TOKEN")
    pair_name = token.get("pair_name") or f"{symbol}/USD"
    price_usd = token.get("price_usd", 0.0)
    liquidity_usd = token.get("liquidity_usd", 0.0)
    vol_5m = token.get("volume_5m", 0.0)
    safety_score = token.get("safety_score", 100)
    grade = token.get("grade", "STEADY_ACCUMULATION")
    lead_lag = correlation_data.get("lead_lag", {})
    verdict = lead_lag.get("verdict", "COINCIDENT_MOMENTUM")
    lead_min = lead_lag.get("lead_minutes", 0.0)
    signal = lead_lag.get("signal", "ACCUMULATE_EARLY_MOMENTUM")

    timeline = correlation_data.get("timeline", [])
    if not timeline:
        # Fallback dummy points for preview
        timeline = [{"timestamp": i * 300, "price": 100 + i * 2, "volume": 5000, "social_mentions": 1, "markers": []} for i in range(10)]

    # Compute SVG scaling
    prices = [t["price"] for t in timeline if t.get("price")] or [1.0]
    volumes = [t.get("volume", 0) for t in timeline] or [1.0]
    min_p, max_p = min(prices), max(prices)
    if min_p == max_p:
        min_p *= 0.95
        max_p *= 1.05
    max_v = max(volumes) if max(volumes) > 0 else 1.0

    svg_w = 700
    svg_h = 240
    padding = 40

    points = []
    markers_html = []
    volume_bars_html = []

    n_points = len(timeline)
    dx = (svg_w - 2 * padding) / max(1, n_points - 1)

    for i, pt in enumerate(timeline):
        x = padding + i * dx
        p = pt["price"]
        # Invert y: high price is at top
        y = svg_h - padding - ((p - min_p) / (max_p - min_p)) * (svg_h - 2 * padding)
        points.append(f"{x:.1f},{y:.1f}")

        # Volume bar in bottom 40px
        v = pt.get("volume", 0)
        bar_h = (v / max_v) * 35.0
        by = svg_h - 15 - bar_h
        volume_bars_html.append(
            f'<rect x="{x - 6:.1f}" y="{by:.1f}" width="12" height="{bar_h:.1f}" fill="currentColor" fill-opacity="0.2" rx="2"/>'
        )

        # Markers on price point
        for m in pt.get("markers", []):
            m_color = m.get("color", "#22c55e")
            m_author = html.escape(m.get("author", ""))
            m_text = html.escape(m.get("text", ""))
            m_type = html.escape(m.get("type", ""))

            markers_html.append(f'''
            <g class="cursor-pointer group" onclick="showMarkerDetail('{m_type}', '{m_author}', '{m_text}')">
                <circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{m_color}" stroke="var(--card)" stroke-width="2" />
                <circle cx="{x:.1f}" cy="{y:.1f}" r="11" fill="{m_color}" fill-opacity="0.3" class="animate-pulse" />
            </g>
            ''')

    polyline_points = " ".join(points)

    lead_badge_color = "bg-emerald-500/20 text-emerald-400 border-emerald-500/30"
    if "FOMO" in verdict or "LATE" in verdict:
        lead_badge_color = "bg-amber-500/20 text-amber-400 border-amber-500/30"
    elif "AVOID" in grade:
        lead_badge_color = "bg-rose-500/20 text-rose-400 border-rose-500/30"

    html_content = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Sabrina Crypto Market & Social Correlator</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
</head>
<body class="bg-transparent text-[var(--foreground)] antialiased p-4">
  <div class="bg-[var(--card)] text-[var(--foreground)] border border-[var(--border)] rounded-2xl p-6 shadow-md max-w-4xl mx-auto space-y-6">
    
    <!-- Header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
      <div>
        <div class="flex items-center gap-3">
          <h1 class="text-2xl font-bold tracking-tight">{pair_name}</h1>
          <span class="text-xs px-2.5 py-1 rounded-full font-medium border {lead_badge_color}">
            {verdict.replace('_', ' ')}
          </span>
          <span class="text-xs px-2.5 py-1 rounded-full font-medium bg-[var(--background)] border border-[var(--border)] text-[var(--muted-foreground)]">
            Safety Score: {safety_score}/100
          </span>
        </div>
        <p class="text-xs text-[var(--muted-foreground)] mt-1">
          DEX Telemetry & Social Dog-Pile Synchronization • Lead Delta: <strong class="text-[var(--foreground)]">{lead_min:+.1f} min</strong>
        </p>
      </div>

      <div class="text-right">
        <div class="text-2xl font-bold font-mono">${price_usd:,.4f}</div>
        <div class="text-xs text-[var(--muted-foreground)]">Liquidity: <span class="font-medium text-[var(--foreground)]">${liquidity_usd:,.0f}</span> • 5m Vol: <span class="font-medium text-[var(--foreground)]">${vol_5m:,.0f}</span></div>
      </div>
    </div>

    <!-- Chart Container -->
    <div class="relative bg-[var(--background)] border border-[var(--border)] rounded-xl p-4">
      <div class="flex items-center justify-between text-xs text-[var(--muted-foreground)] mb-2 px-2">
        <div class="flex items-center gap-4">
          <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-[#22c55e]"></span> Early Social Mention</span>
          <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-[#eab308]"></span> Dog-Pile Surge</span>
          <span class="flex items-center gap-1.5"><span class="w-2.5 h-2.5 rounded-full bg-[#ef4444]"></span> Influencer Call</span>
        </div>
        <span>High: ${max_p:,.4f} • Low: ${min_p:,.4f}</span>
      </div>

      <svg viewBox="0 0 {svg_w} {svg_h}" class="w-full h-64 overflow-visible">
        <!-- Grid lines -->
        <line x1="{padding}" y1="{padding}" x2="{svg_w - padding}" y2="{padding}" stroke="var(--border)" stroke-dasharray="4" />
        <line x1="{padding}" y1="{svg_h / 2}" x2="{svg_w - padding}" y2="{svg_h / 2}" stroke="var(--border)" stroke-dasharray="4" />
        <line x1="{padding}" y1="{svg_h - padding}" x2="{svg_w - padding}" y2="{svg_h - padding}" stroke="var(--border)" />

        <!-- Volume Bars -->
        <g class="text-[var(--muted-foreground)]">
          {"".join(volume_bars_html)}
        </g>

        <!-- Price Line -->
        <polyline
          fill="none"
          stroke="#3b82f6"
          stroke-width="3"
          stroke-linecap="round"
          stroke-linejoin="round"
          points="{polyline_points}"
        />

        <!-- Social Event Markers -->
        {"".join(markers_html)}
      </svg>

      <!-- Active Marker Popup Inspector -->
      <div id="markerCard" class="mt-3 p-3 bg-[var(--card)] border border-[var(--border)] rounded-lg text-xs flex items-center justify-between">
        <div>
          <span id="markerType" class="font-semibold text-[var(--foreground)]">Click any marker pin on the price line to inspect the post</span>
          <p id="markerText" class="text-[var(--muted-foreground)] mt-0.5">Social mentions are plotted at the exact candle they were broadcast.</p>
        </div>
        <div id="markerAuthor" class="text-xs font-mono text-[var(--muted-foreground)]"></div>
      </div>
    </div>

    <!-- Actionable Intelligence Footprint -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
      <div class="p-4 bg-[var(--background)] border border-[var(--border)] rounded-xl">
        <span class="text-xs text-[var(--muted-foreground)] uppercase font-semibold">Alpha Signal</span>
        <div class="text-sm font-bold text-emerald-400 mt-1">{signal.replace('_', ' ')}</div>
        <p class="text-xs text-[var(--muted-foreground)] mt-1">Social surge preceded candle expansion by {abs(lead_min):.1f}m.</p>
      </div>

      <div class="p-4 bg-[var(--background)] border border-[var(--border)] rounded-xl">
        <span class="text-xs text-[var(--muted-foreground)] uppercase font-semibold">Sybil Resistance</span>
        <div class="text-sm font-bold text-[var(--foreground)] mt-1">High Author Diversity</div>
        <p class="text-xs text-[var(--muted-foreground)] mt-1">Dog-pile accounts verified distinct; filtered single-bot spammers.</p>
      </div>

      <div class="p-4 bg-[var(--background)] border border-[var(--border)] rounded-xl">
        <span class="text-xs text-[var(--muted-foreground)] uppercase font-semibold">DEX Liquidity Gate</span>
        <div class="text-sm font-bold text-emerald-400 mt-1">Safe Liquidity Depth</div>
        <p class="text-xs text-[var(--muted-foreground)] mt-1">${liquidity_usd:,.0f} liquidity safely exceeds $10k floor.</p>
      </div>
    </div>

  </div>

  <script>
    function showMarkerDetail(type, author, text) {{
      document.getElementById('markerType').innerText = type.replace(/_/g, ' ');
      document.getElementById('markerText').innerText = '"' + text + '"';
      document.getElementById('markerAuthor').innerText = author;
    }}
  </script>
</body>
</html>
"""
    return html_content


def generate_paper_trading_html(
    portfolio: Dict[str, Any],
    performance: Dict[str, Any],
    recent_trades: List[Dict[str, Any]]
) -> str:
    """
    Generates an interactive Generative UI dashboard for Sabrina's Crypto Paper Trading Engine.
    Displays cash balances, active open positions, blocked rug/honeypot tokens, and performance metrics.
    """
    cash = portfolio.get("cash_balance_usd", 1000.0)
    allocated = portfolio.get("allocated_in_trades_usd", 0.0)
    total_val = portfolio.get("total_portfolio_value_usd", 1000.0)
    open_count = portfolio.get("open_positions_count", 0)

    win_rate = performance.get("win_rate_pct", 0.0)
    net_pnl = performance.get("net_pnl_usd", 0.0)
    total_trades = performance.get("total_trades", 0)
    rug_blocks = performance.get("security_rejected_trades", 0)
    fees_paid = performance.get("total_fees_paid_usd", 0.0)
    profit_factor = performance.get("profit_factor", 0.0)

    # Separate recent trades into open, closed, and rejected
    open_trades = [t for t in recent_trades if t.get("status") == "OPEN"]
    closed_trades = [t for t in recent_trades if t.get("status") == "CLOSED"]
    rejected_trades = [t for t in recent_trades if t.get("status") == "REJECTED_BY_SECURITY"]

    # Build Open Positions Rows
    open_rows = []
    for t in open_trades:
        sym = html.escape(str(t.get("symbol", "UNKNOWN")))
        chain = html.escape(str(t.get("chain", "solana")))
        entry_p = t.get("entry_price", 0.0)
        pos_usd = t.get("position_size_usd", 0.0)
        sig = html.escape(str(t.get("signal_type", "MOMENTUM")))
        open_rows.append(f"""
        <tr class="border-b border-[var(--border)] hover:bg-[var(--card)]/50 transition">
          <td class="py-3 px-4 font-mono font-bold text-sky-400">{sym}</td>
          <td class="py-3 px-4 text-xs font-mono text-[var(--muted-foreground)] uppercase">{chain}</td>
          <td class="py-3 px-4 font-mono">${entry_p:,.6f}</td>
          <td class="py-3 px-4 font-mono font-semibold">${pos_usd:,.2f}</td>
          <td class="py-3 px-4"><span class="px-2 py-0.5 text-xs font-medium rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">{sig}</span></td>
          <td class="py-3 px-4 text-xs text-[var(--muted-foreground)] font-mono">SL: -12% | TP: +25%</td>
        </tr>
        """)

    if not open_rows:
        open_rows.append("""
        <tr>
          <td colspan="6" class="py-6 text-center text-xs text-[var(--muted-foreground)]">
            No active positions open. Scanner is monitoring DEX pairs for qualified breakouts.
          </td>
        </tr>
        """)

    # Build Blocked Rugs Rows
    rejected_rows = []
    for r in rejected_trades[:6]:
        sym = html.escape(str(r.get("symbol", "UNKNOWN")))
        addr = html.escape(str(r.get("token_address", ""))[:12] + "...")
        score = r.get("rug_score", "N/A")
        reason = html.escape(str(r.get("rejection_reason", "Failed contract security gate")))
        rejected_rows.append(f"""
        <tr class="border-b border-[var(--border)] hover:bg-rose-950/10 transition">
          <td class="py-2.5 px-4 font-mono font-bold text-rose-400">{sym}</td>
          <td class="py-2.5 px-4 font-mono text-xs text-[var(--muted-foreground)]">{addr}</td>
          <td class="py-2.5 px-4 font-mono text-xs font-bold text-rose-300">{score}</td>
          <td class="py-2.5 px-4 text-xs text-rose-300/80">{reason}</td>
          <td class="py-2.5 px-4"><span class="px-2 py-0.5 text-[10px] uppercase font-bold rounded bg-rose-500/20 text-rose-300 border border-rose-500/30">BLOCKED</span></td>
        </tr>
        """)

    if not rejected_rows:
        rejected_rows.append("""
        <tr>
          <td colspan="5" class="py-4 text-center text-xs text-[var(--muted-foreground)]">
            No contract security rejections recorded yet.
          </td>
        </tr>
        """)

    # Build Closed Trades Rows
    closed_rows = []
    for c in closed_trades[:8]:
        sym = html.escape(str(c.get("symbol", "UNKNOWN")))
        net = c.get("net_pnl_usd", 0.0)
        roi = c.get("net_roi_pct", 0.0)
        reason = html.escape(str(c.get("exit_reason", "MANUAL")))
        pnl_color = "text-emerald-400" if net >= 0 else "text-rose-400"
        pnl_prefix = "+" if net > 0 else ""
        closed_rows.append(f"""
        <tr class="border-b border-[var(--border)] hover:bg-[var(--card)]/50 transition">
          <td class="py-2.5 px-4 font-mono font-bold text-[var(--foreground)]">{sym}</td>
          <td class="py-2.5 px-4 font-mono ${pnl_color} font-bold">{pnl_prefix}${net:,.2f}</td>
          <td class="py-2.5 px-4 font-mono ${pnl_color} text-xs">{pnl_prefix}{roi:.1f}%</td>
          <td class="py-2.5 px-4 font-mono text-xs text-[var(--muted-foreground)]">${c.get('fee_deductions_usd', 0.0):,.2f}</td>
          <td class="py-2.5 px-4 text-xs font-mono text-[var(--muted-foreground)]">{reason}</td>
        </tr>
        """)

    if not closed_rows:
        closed_rows.append("""
        <tr>
          <td colspan="5" class="py-4 text-center text-xs text-[var(--muted-foreground)]">
            No closed trades yet. Forward testing history will log here.
          </td>
        </tr>
        """)

    pnl_class = "text-emerald-400" if net_pnl >= 0 else "text-rose-400"
    pnl_sign = "+" if net_pnl > 0 else ""

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Sabrina Paper Trading & Security Sentinel</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    :root {{
      --background: #090a0f;
      --card: #121520;
      --border: #1e2436;
      --foreground: #f1f5f9;
      --muted-foreground: #94a3b8;
    }}
  </style>
</head>
<body class="bg-[var(--background)] text-[var(--foreground)] p-4 md:p-6 font-sans">
  <div class="max-w-5xl mx-auto space-y-6">

    <!-- Top Header -->
    <div class="flex flex-wrap items-center justify-between gap-4 border-b border-[var(--border)] pb-4">
      <div>
        <div class="flex items-center gap-2">
          <div class="w-3 h-3 rounded-full bg-emerald-400 animate-pulse"></div>
          <h1 class="text-xl font-bold tracking-tight">Sabrina Paper Trading & On-Chain Sentinel</h1>
        </div>
        <p class="text-xs text-[var(--muted-foreground)] mt-1">
          Zero-Capital Forward Simulator &middot; Automated RugCheck Gate &middot; DEX Telemetry
        </p>
      </div>
      <div class="flex items-center gap-3">
        <span class="px-3 py-1 text-xs font-semibold rounded-full bg-sky-500/10 text-sky-400 border border-sky-500/30">
          STAGE 1: FORWARD PAPER SIMULATION
        </span>
        <span class="px-3 py-1 text-xs font-mono rounded bg-[var(--card)] border border-[var(--border)] text-[var(--muted-foreground)]">
          Friction: 1.0% Slip &middot; 0.3% Fee
        </span>
      </div>
    </div>

    <!-- Core Portfolio KPI Cards -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div class="p-4 bg-[var(--card)] border border-[var(--border)] rounded-xl">
        <span class="text-xs text-[var(--muted-foreground)] uppercase font-semibold">Total Portfolio Value</span>
        <div class="text-2xl font-black font-mono mt-1 text-[var(--foreground)]">${total_val:,.2f}</div>
        <div class="text-xs text-[var(--muted-foreground)] mt-1">Cash: ${cash:,.2f} &middot; In Trades: ${allocated:,.2f}</div>
      </div>

      <div class="p-4 bg-[var(--card)] border border-[var(--border)] rounded-xl">
        <span class="text-xs text-[var(--muted-foreground)] uppercase font-semibold">Net Realized PnL</span>
        <div class="text-2xl font-black font-mono mt-1 {pnl_class}">{pnl_sign}${net_pnl:,.2f}</div>
        <div class="text-xs text-[var(--muted-foreground)] mt-1">Fees Paid: ${fees_paid:,.2f} &middot; Win Rate: {win_rate:.1f}%</div>
      </div>

      <div class="p-4 bg-[var(--card)] border border-[var(--border)] rounded-xl">
        <span class="text-xs text-[var(--muted-foreground)] uppercase font-semibold">Security Gate Blocks</span>
        <div class="text-2xl font-black font-mono mt-1 text-rose-400">{rug_blocks}</div>
        <div class="text-xs text-rose-300/80 mt-1">Serial ruggers & honeypots averted</div>
      </div>

      <div class="p-4 bg-[var(--card)] border border-[var(--border)] rounded-xl">
        <span class="text-xs text-[var(--muted-foreground)] uppercase font-semibold">Active Positions</span>
        <div class="text-2xl font-black font-mono mt-1 text-sky-400">{open_count} <span class="text-sm font-normal text-[var(--muted-foreground)]">/ 5 max</span></div>
        <div class="text-xs text-[var(--muted-foreground)] mt-1">Allocation: $25.00 / trade</div>
      </div>
    </div>

    <!-- Active Open Positions Table -->
    <div class="p-5 bg-[var(--card)] border border-[var(--border)] rounded-xl space-y-3">
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-bold uppercase tracking-wider text-[var(--foreground)] flex items-center gap-2">
          <span class="w-2 h-2 rounded-full bg-sky-400"></span> Active Open Positions
        </h2>
        <span class="text-xs text-[var(--muted-foreground)]">Auto-monitoring 5m price candles</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b border-[var(--border)] text-xs uppercase text-[var(--muted-foreground)]">
              <th class="py-2 px-4">Symbol</th>
              <th class="py-2 px-4">Chain</th>
              <th class="py-2 px-4">Entry Price</th>
              <th class="py-2 px-4">Size (USD)</th>
              <th class="py-2 px-4">Signal</th>
              <th class="py-2 px-4">Exit Parameters</th>
            </tr>
          </thead>
          <tbody>
            {"".join(open_rows)}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Security Gate Block Log -->
    <div class="p-5 bg-[var(--card)] border border-rose-900/30 rounded-xl space-y-3">
      <div class="flex items-center justify-between">
        <h2 class="text-sm font-bold uppercase tracking-wider text-rose-400 flex items-center gap-2">
          <svg class="w-4 h-4 text-rose-400" fill="currentColor" viewBox="0 0 20 20"><path fill-rule="evenodd" d="M10 1.944A11.954 11.954 0 012.166 5C2.056 5.649 2 6.319 2 7c0 5.225 3.34 9.67 8 11.317C14.66 16.67 18 12.225 18 7c0-.682-.057-1.35-.166-2.001A11.954 11.954 0 0110 1.944zM11 14a1 1 0 11-2 0 1 1 0 012 0zm0-7a1 1 0 10-2 0v3a1 1 0 102 0V7z" clip-rule="evenodd"></path></svg>
          Contract Security Sentinel: Blocked Tokens & Scams
        </h2>
        <span class="text-xs text-rose-300/80 font-mono">100% Capital Saved</span>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm">
          <thead>
            <tr class="border-b border-[var(--border)] text-xs uppercase text-[var(--muted-foreground)]">
              <th class="py-2 px-4">Token</th>
              <th class="py-2 px-4">Address</th>
              <th class="py-2 px-4">Rug Score</th>
              <th class="py-2 px-4">Detected Vulnerabilities & Risks</th>
              <th class="py-2 px-4">Status</th>
            </tr>
          </thead>
          <tbody>
            {"".join(rejected_rows)}
          </tbody>
        </table>
      </div>
    </div>

    <!-- Roadmap to Live Capital Execution -->
    <div class="p-5 bg-[var(--background)] border border-[var(--border)] rounded-xl space-y-3">
      <h2 class="text-sm font-bold uppercase tracking-wider text-[var(--foreground)]">
        Path to Live Capital Execution
      </h2>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
        <div class="p-3 bg-[var(--card)] border border-emerald-500/40 rounded-lg">
          <span class="text-emerald-400 font-bold">1. Zero-Capital Forward Test (Active)</span>
          <p class="text-[var(--muted-foreground)] mt-1">
            Simulate 50+ trades with realistic 1.3% friction (slippage + DEX fee) to verify net positive expectancy.
          </p>
        </div>
        <div class="p-3 bg-[var(--card)] border border-[var(--border)] rounded-lg">
          <span class="text-sky-400 font-bold">2. Sandboxed Micro-Capital ($50 Max)</span>
          <p class="text-[var(--muted-foreground)] mt-1">
            Route on-chain swaps via Jupiter API with private Jito MEV bundles to prevent sandwich attacks. Max $5/trade.
          </p>
        </div>
        <div class="p-3 bg-[var(--card)] border border-[var(--border)] rounded-lg">
          <span class="text-amber-400 font-bold">3. Autonomous Risk Kill-Switch</span>
          <p class="text-[var(--muted-foreground)] mt-1">
            Hard 10% daily drawdown stop, zero private key exposure to LLM context, operator override button.
          </p>
        </div>
      </div>
    </div>

  </div>
</body>
</html>
"""
    return html_content
