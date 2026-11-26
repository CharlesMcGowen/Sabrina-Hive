# -*- coding: utf-8 -*-
"""
tools/paper_trader.py
=====================
Zero-capital forward testing and paper trading simulation engine.

Simulates entries, exits, realistic DEX friction (slippage, DEX fees, gas/priority fees),
and records trade metrics into SQLite to measure actual strategy expectancy.
Enforces hard RugCheck contract security gating before any simulated position is opened.
"""

import os
import time
import uuid
import sqlite3
import logging
from typing import Dict, Any, List, Optional, Callable
from tools.rugcheck_auditor import RugCheckAuditor

logger = logging.getLogger("sabrina.crypto.paper_trader")

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "social", "paper_trading.sqlite3"
)


class PaperTradingEngine:
    """Forward testing engine with realistic on-chain execution friction and security gates."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        starting_balance_usd: float = 1000.0,
        security_auditor: Optional[RugCheckAuditor] = None,
        default_slippage_pct: float = 0.01,  # 1.0%
        dex_fee_pct: float = 0.003,          # 0.3%
        gas_fee_usd: float = 0.05,           # $0.05 per tx
        stop_loss_pct: float = -12.0,        # -12%
        take_profit_1_pct: float = 25.0,     # +25%
        take_profit_2_pct: float = 60.0,     # +60%
        max_hold_hours: float = 4.0,
    ):
        self.db_path = db_path
        self.starting_balance_usd = starting_balance_usd
        self.auditor = security_auditor or RugCheckAuditor()
        self.default_slippage_pct = default_slippage_pct
        self.dex_fee_pct = dex_fee_pct
        self.gas_fee_usd = gas_fee_usd
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_1_pct = take_profit_1_pct
        self.take_profit_2_pct = take_profit_2_pct
        self.max_hold_hours = max_hold_hours

        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS portfolio (
                id INTEGER PRIMARY KEY,
                balance_usd REAL NOT NULL,
                starting_balance REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """)
            conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                token_address TEXT NOT NULL,
                chain TEXT NOT NULL,
                entry_price REAL NOT NULL,
                position_size_usd REAL NOT NULL,
                token_amount REAL NOT NULL,
                status TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                rug_score INTEGER,
                rejection_reason TEXT,
                entry_time REAL NOT NULL,
                exit_price REAL,
                exit_time REAL,
                exit_reason TEXT,
                fee_deductions_usd REAL DEFAULT 0.0,
                gross_pnl_usd REAL DEFAULT 0.0,
                net_pnl_usd REAL DEFAULT 0.0,
                net_roi_pct REAL DEFAULT 0.0
            )
            """)
            # Initialize portfolio if empty
            row = conn.execute("SELECT * FROM portfolio WHERE id=1").fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO portfolio VALUES (1, ?, ?, ?)",
                    (self.starting_balance_usd, self.starting_balance_usd, time.time())
                )

    def get_portfolio(self) -> Dict[str, Any]:
        """Returns current simulated cash balance and open trade counts."""
        with self._get_conn() as conn:
            p_row = conn.execute("SELECT * FROM portfolio WHERE id=1").fetchone()
            balance = p_row["balance_usd"] if p_row else self.starting_balance_usd

            open_trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()]
            allocated = sum(t["position_size_usd"] for t in open_trades)

        return {
            "cash_balance_usd": round(balance, 2),
            "allocated_in_trades_usd": round(allocated, 2),
            "total_portfolio_value_usd": round(balance + allocated, 2),
            "open_positions_count": len(open_trades),
            "open_positions": open_trades,
        }

    def open_trade(
        self,
        symbol: str,
        token_address: str,
        entry_price: float,
        chain: str = "solana",
        signal_type: str = "ACCUMULATE_EARLY_MOMENTUM",
        position_size_usd: float = 50.0,
        skip_security_check: bool = False
    ) -> Dict[str, Any]:
        """
        Evaluates contract security and opens a simulated position if safe.
        Applies entry slippage and fee friction automatically.
        """
        if entry_price <= 0:
            raise ValueError(f"Invalid entry price: {entry_price}")

        portfolio = self.get_portfolio()
        if portfolio["cash_balance_usd"] < position_size_usd:
            return {
                "status": "REJECTED_INSUFFICIENT_FUNDS",
                "reason": f"Required ${position_size_usd}, available cash: ${portfolio['cash_balance_usd']}",
            }

        # Step 1: Mandatory Contract Security Verification Gate
        rug_score = -1
        if not skip_security_check:
            sec_report = self.auditor.verify_token_security(token_address, chain=chain)
            rug_score = sec_report.get("score", -1)
            if not sec_report.get("is_safe"):
                trade_id = uuid.uuid4().hex
                reasons = "; ".join(sec_report.get("risks", ["Failed contract security gate"]))
                with self._get_conn() as conn:
                    conn.execute("""
                    INSERT INTO trades (
                        id, symbol, token_address, chain, entry_price, position_size_usd,
                        token_amount, status, signal_type, rug_score, rejection_reason, entry_time
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        trade_id, symbol, token_address, chain, entry_price, position_size_usd,
                        0.0, "REJECTED_BY_SECURITY", signal_type, rug_score, reasons, time.time()
                    ))
                logger.warning("Paper trade REJECTED by security gate for %s: %s", symbol, reasons)
                return {
                    "status": "REJECTED_BY_SECURITY",
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "rug_score": rug_score,
                    "reason": reasons,
                }

        # Step 2: Realistic Execution & Token Allocation
        effective_entry_price = entry_price * (1.0 + self.default_slippage_pct)
        token_amount = position_size_usd / effective_entry_price

        trade_id = uuid.uuid4().hex
        now = time.time()

        with self._get_conn() as conn:
            # Deduct cash from portfolio
            new_balance = portfolio["cash_balance_usd"] - position_size_usd
            conn.execute("UPDATE portfolio SET balance_usd=?, updated_at=? WHERE id=1", (new_balance, now))

            # Record open trade
            conn.execute("""
            INSERT INTO trades (
                id, symbol, token_address, chain, entry_price, position_size_usd,
                token_amount, status, signal_type, rug_score, entry_time
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id, symbol, token_address, chain, entry_price, position_size_usd,
                token_amount, "OPEN", signal_type, rug_score, now
            ))

        logger.info("Paper trade OPENED for %s @ $%.6f (effective $%.6f). Size: $%.2f",
                    symbol, entry_price, effective_entry_price, position_size_usd)

        return {
            "status": "OPENED",
            "trade_id": trade_id,
            "symbol": symbol,
            "token_address": token_address,
            "entry_price": entry_price,
            "effective_entry_price": round(effective_entry_price, 6),
            "token_amount": token_amount,
            "position_size_usd": position_size_usd,
            "rug_score": rug_score,
            "entry_time": now,
        }

    def close_trade(self, trade_id: str, exit_price: float, reason: str = "MANUAL_EXIT") -> Dict[str, Any]:
        """
        Closes an open paper trade, applying exit slippage, DEX fees, and network gas.
        Calculates exact gross & net PnL and updates portfolio cash balance.
        """
        with self._get_conn() as conn:
            trade = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
            if not trade:
                raise KeyError(f"Trade {trade_id} not found")
            if trade["status"] != "OPEN":
                raise ValueError(f"Trade {trade_id} is already {trade['status']}")

            pos_size = trade["position_size_usd"]
            token_amt = trade["token_amount"]

            # Realistic exit value after slippage and fees
            effective_exit_price = exit_price * (1.0 - self.default_slippage_pct)
            gross_val = token_amt * exit_price
            dex_fee = gross_val * self.dex_fee_pct
            total_friction = (pos_size * (self.default_slippage_pct + self.dex_fee_pct)) + \
                             (gross_val * (self.default_slippage_pct + self.dex_fee_pct)) + \
                             self.gas_fee_usd

            effective_exit_val = (token_amt * effective_exit_price) - dex_fee - (self.gas_fee_usd / 2)
            net_pnl = effective_exit_val - pos_size
            net_roi_pct = (net_pnl / pos_size) * 100.0
            gross_pnl = gross_val - pos_size

            now = time.time()

            # Credit back portfolio cash
            p_row = conn.execute("SELECT balance_usd FROM portfolio WHERE id=1").fetchone()
            new_balance = p_row["balance_usd"] + effective_exit_val
            conn.execute("UPDATE portfolio SET balance_usd=?, updated_at=? WHERE id=1", (new_balance, now))

            # Update trade row
            conn.execute("""
            UPDATE trades SET
                status='CLOSED',
                exit_price=?,
                exit_time=?,
                exit_reason=?,
                fee_deductions_usd=?,
                gross_pnl_usd=?,
                net_pnl_usd=?,
                net_roi_pct=?
            WHERE id=?
            """, (
                exit_price, now, reason, round(total_friction, 3),
                round(gross_pnl, 2), round(net_pnl, 2), round(net_roi_pct, 2),
                trade_id
            ))

        logger.info("Paper trade CLOSED for %s @ $%.6f (%s). Net PnL: $%.2f (%+.2f%%)",
                    trade["symbol"], exit_price, reason, net_pnl, net_roi_pct)

        return {
            "status": "CLOSED",
            "trade_id": trade_id,
            "symbol": trade["symbol"],
            "entry_price": trade["entry_price"],
            "exit_price": exit_price,
            "exit_reason": reason,
            "fee_deductions_usd": round(total_friction, 2),
            "gross_pnl_usd": round(gross_pnl, 2),
            "net_pnl_usd": round(net_pnl, 2),
            "net_roi_pct": round(net_roi_pct, 2),
        }

    def evaluate_active_trades(self, price_lookup_func: Callable[[str, str], Optional[float]]) -> List[Dict[str, Any]]:
        """
        Polls current prices for all open trades and auto-executes risk exits:
        - Stop-Loss hit (<= stop_loss_pct)
        - Take-Profit hit (>= take_profit_1_pct)
        - Max hold time expired (>= max_hold_hours)
        """
        actions_taken = []
        with self._get_conn() as conn:
            open_trades = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()]

        now = time.time()
        for t in open_trades:
            trade_id = t["id"]
            symbol = t["symbol"]
            token_addr = t["token_address"]
            entry_price = t["entry_price"]
            entry_time = t["entry_time"]

            curr_price = price_lookup_func(token_addr, symbol)
            if curr_price is None or curr_price <= 0:
                continue

            pct_change = ((curr_price - entry_price) / entry_price) * 100.0
            hold_hours = (now - entry_time) / 3600.0

            # 1. Stop-Loss Trigger
            if pct_change <= self.stop_loss_pct:
                res = self.close_trade(trade_id, curr_price, reason=f"STOP_LOSS_{pct_change:.1f}%")
                actions_taken.append(res)
            # 2. Take-Profit 2 Trigger (+60%)
            elif pct_change >= self.take_profit_2_pct:
                res = self.close_trade(trade_id, curr_price, reason=f"TAKE_PROFIT_2_{pct_change:.1f}%")
                actions_taken.append(res)
            # 3. Take-Profit 1 Trigger (+25%)
            elif pct_change >= self.take_profit_1_pct:
                res = self.close_trade(trade_id, curr_price, reason=f"TAKE_PROFIT_1_{pct_change:.1f}%")
                actions_taken.append(res)
            # 4. Time Expiration (stagnant trade after 4h)
            elif hold_hours >= self.max_hold_hours:
                res = self.close_trade(trade_id, curr_price, reason=f"TIME_EXPIRED_{hold_hours:.1f}h")
                actions_taken.append(res)

        return actions_taken

    def get_performance_summary(self) -> Dict[str, Any]:
        """Calculates win rate, profit factor, total net PnL, and ROI metrics."""
        with self._get_conn() as conn:
            closed = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='CLOSED'").fetchall()]
            rejected = conn.execute("SELECT COUNT(*) FROM trades WHERE status='REJECTED_BY_SECURITY'").fetchone()[0]
            open_count = conn.execute("SELECT COUNT(*) FROM trades WHERE status='OPEN'").fetchone()[0]
            portfolio = conn.execute("SELECT * FROM portfolio WHERE id=1").fetchone()

        total_closed = len(closed)
        if total_closed == 0:
            return {
                "total_trades": 0,
                "open_trades": open_count,
                "security_rejected_trades": rejected,
                "win_rate_pct": 0.0,
                "net_pnl_usd": 0.0,
                "profit_factor": 0.0,
                "total_fees_paid_usd": 0.0,
                "current_portfolio_usd": portfolio["balance_usd"] if portfolio else self.starting_balance_usd,
            }

        wins = [t for t in closed if t["net_pnl_usd"] > 0]
        losses = [t for t in closed if t["net_pnl_usd"] <= 0]

        total_net_pnl = sum(t["net_pnl_usd"] for t in closed)
        total_fees = sum(t["fee_deductions_usd"] for t in closed)
        win_rate = (len(wins) / total_closed) * 100.0

        gross_profits = sum(t["net_pnl_usd"] for t in wins)
        gross_losses = abs(sum(t["net_pnl_usd"] for t in losses))
        profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else (99.0 if gross_profits > 0 else 1.0)
        avg_roi = sum(t["net_roi_pct"] for t in closed) / total_closed

        return {
            "total_trades": total_closed,
            "open_trades": open_count,
            "security_rejected_trades": rejected,
            "wins_count": len(wins),
            "losses_count": len(losses),
            "win_rate_pct": round(win_rate, 2),
            "net_pnl_usd": round(total_net_pnl, 2),
            "profit_factor": round(profit_factor, 2),
            "avg_trade_roi_pct": round(avg_roi, 2),
            "total_fees_paid_usd": round(total_fees, 2),
            "current_portfolio_usd": round(portfolio["balance_usd"] if portfolio else self.starting_balance_usd, 2),
        }
