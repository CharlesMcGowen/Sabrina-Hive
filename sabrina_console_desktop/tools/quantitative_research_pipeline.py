"""Autonomous Quantitative Research & Weight Space Programming Pipeline for Trading.

Bridges quantitative market analysis (Kraken OHLCV candles, technical indicators,
order flow metrics) with Weight Space Programming (WSP) and scientific compute:
1. Multi-Regime Candle & Feature Engineering:
   - Technical feature extraction (RSI, MACD, Bollinger Width, EMA ratios, Parkinson volatility).
   - PCA Eigen-factor decomposition of market feature space.
2. Market Prediction Architecture (ExitRouterMLP):
   - 5-action decision router: FULL_EXIT, SKIM_PROFIT, SCALE_OUT, TRAIL_ONLY, FOREX_SCALP.
3. Weight Space Programming for Regime Adaptation:
   - Task vector extraction for market regimes (Trend, Mean Reversion, Defensive Risk-Off).
   - TIES and DARE model merging to combine multi-regime intelligence without parameter interference.
   - Task Vector Negation for trap suppression (neutralizing false breakout traps from trap_catcher).
   - Dynamic SLERP interpolation conditioned on live regime entropy/volatility.
4. Out-of-Sample Walk-Forward Backtesting:
   - Strict lookahead prevention (train/test temporal split).
   - Real-world fee modeling (Kraken 26 bps taker, 16 bps maker) and execution slippage.
5. Vectorized Monte Carlo Stress Testing:
   - 20,000+ stochastic return paths evaluating empirical VaR(0.05), CVaR(0.05), and Max Drawdown.
6. Durable SQLite Experiment Tracking:
   - Persists all artifacts and cryptographic fingerprints to `assistant_research_experiments`.
   - Strict competence segregation (counts_toward_competence = 0).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

# Ensure Sabrina root is on sys.path
SABRINA_ROOT = Path(__file__).resolve().parent.parent
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from tools.compute_research_engine import (
    ComputeResearchRunner,
    DataScienceToolkit,
    MonteCarloToolkit,
    ScientificJobSpec,
    WeightSpaceToolkit,
    ensure_research_experiments_schema,
    record_research_experiment,
)

LOGGER = logging.getLogger("sabrina.quantitative_research")
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "sabrina_assistant.sqlite3"

# Standard Kraken Strategy Router Decision Classes
ACTIONS = ["FULL_EXIT", "SKIM_PROFIT", "SCALE_OUT", "TRAIL_ONLY", "FOREX_SCALP"]
ACTION_TO_IDX = {act: i for i, act in enumerate(ACTIONS)}
IDX_TO_ACTION = {i: act for i, act in enumerate(ACTIONS)}


# -----------------------------------------------------------------------------
# Data Models & Feature Extraction
# -----------------------------------------------------------------------------

@dataclass
class MarketCandle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class MarketFeatureExtractor:
    """Computes fixed-order quantitative features aligned with Kraken Strategy Router."""

    FEATURE_NAMES = [
        "log_return",
        "rsi_14",
        "macd_hist",
        "bb_width",
        "ema_ratio",
        "parkinson_vol",
        "roc_10",
        "drawdown_n30",
        "vol_trend",
    ]

    @classmethod
    def extract_features(cls, candles: List[MarketCandle]) -> np.ndarray:
        """Extract an (N, D) normalized feature matrix from a sequence of OHLCV candles."""
        n = len(candles)
        if n < 35:
            raise ValueError(f"Need at least 35 candles for feature extraction, got {n}")

        closes = np.array([c.close for c in candles], dtype=np.float64)
        highs = np.array([c.high for c in candles], dtype=np.float64)
        lows = np.array([c.low for c in candles], dtype=np.float64)
        volumes = np.array([c.volume for c in candles], dtype=np.float64)

        # 1. Log returns
        log_ret = np.zeros(n)
        log_ret[1:] = np.log(closes[1:] / np.maximum(closes[:-1], 1e-8))

        # 2. RSI (14 period)
        rsi = np.full(n, 50.0)
        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        if len(deltas) >= 14:
            avg_gain = np.mean(gains[:14])
            avg_loss = np.mean(losses[:14])
            for i in range(14, len(deltas)):
                avg_gain = (avg_gain * 13 + gains[i]) / 14.0
                avg_loss = (avg_loss * 13 + losses[i]) / 14.0
                rs = avg_gain / max(avg_loss, 1e-8)
                rsi[i + 1] = 100.0 - (100.0 / (1.0 + rs))

        # 3. MACD Histogram (12, 26, 9)
        def _ema(arr: np.ndarray, span: int) -> np.ndarray:
            alpha = 2.0 / (span + 1)
            out = np.zeros_like(arr)
            out[0] = arr[0]
            for i in range(1, len(arr)):
                out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]
            return out

        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        macd_line = ema12 - ema26
        macd_signal = _ema(macd_line, 9)
        macd_hist = macd_line - macd_signal

        # 4. Bollinger Band Width (20 period, 2 std)
        bb_width = np.zeros(n)
        for i in range(20, n):
            window = closes[i - 20 : i]
            mean = np.mean(window)
            std = np.std(window)
            bb_width[i] = (4.0 * std) / max(mean, 1e-8)

        # 5. EMA ratio (8 vs 21)
        ema8 = _ema(closes, 8)
        ema21 = _ema(closes, 21)
        ema_ratio = ema8 / np.maximum(ema21, 1e-8) - 1.0

        # 6. Parkinson Volatility (20 period)
        parkinson_vol = np.zeros(n)
        hl_ratio = np.log(highs / np.maximum(lows, 1e-8)) ** 2 / (4.0 * math.log(2.0))
        for i in range(20, n):
            parkinson_vol[i] = math.sqrt(np.mean(hl_ratio[i - 20 : i]))

        # 7. Price ROC (10 period)
        roc_10 = np.zeros(n)
        roc_10[10:] = (closes[10:] - closes[:-10]) / np.maximum(closes[:-10], 1e-8)

        # 8. Drawdown over last 30 bars
        drawdown_n30 = np.zeros(n)
        for i in range(30, n):
            peak = np.max(closes[i - 30 : i + 1])
            drawdown_n30[i] = (closes[i] - peak) / max(peak, 1e-8)

        # 9. Volume Trend (5-bar vs 20-bar volume)
        vol_ema5 = _ema(volumes, 5)
        vol_ema20 = _ema(volumes, 20)
        vol_trend = vol_ema5 / np.maximum(vol_ema20, 1e-8) - 1.0

        features = np.column_stack([
            log_ret,
            (rsi - 50.0) / 25.0,  # centered around 0
            macd_hist / np.maximum(closes * 0.01, 1e-8),
            bb_width * 10.0,
            ema_ratio * 100.0,
            parkinson_vol * 100.0,
            roc_10 * 10.0,
            drawdown_n30 * 10.0,
            vol_trend,
        ])

        # Replace any potential nan/inf
        features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
        return features


# -----------------------------------------------------------------------------
# Neural Policy Architecture
# -----------------------------------------------------------------------------

class ExitRouterMLP(nn.Module):
    """Deep Neural Policy mapping feature vectors to 5 Kraken exit/entry actions."""

    def __init__(self, in_features: int = 9, hidden_dim: int = 64, num_classes: int = 5):
        super().__init__()
        self.in_features = in_features
        self.hidden_dim = hidden_dim
        self.num_classes = num_classes
        self.net = nn.Sequential(
            nn.Linear(in_features, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.LayerNorm(hidden_dim // 2),
            nn.GELU(),
            nn.Linear(hidden_dim // 2, num_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)

    def get_weights(self) -> Dict[str, torch.Tensor]:
        return {k: v.detach().clone() for k, v in self.state_dict().items()}

    def load_weights(self, weights: Dict[str, torch.Tensor]) -> None:
        self.load_state_dict(weights)

    def predict_action(self, features: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        with torch.no_grad():
            logits = self.forward(features)
            probs = torch.softmax(logits, dim=-1)
            actions = torch.argmax(probs, dim=-1)
        return actions, probs


# -----------------------------------------------------------------------------
# Multi-Regime Market Simulation Generator
# -----------------------------------------------------------------------------

class SyntheticMarketGenerator:
    """Generates synthetic, realistic multi-regime financial candle series for testing."""

    @staticmethod
    def generate_regime_candles(
        regime: str,
        n_candles: int = 300,
        s0: float = 100.0,
        start_ts: int = 1700000000,
        seed: Optional[int] = None,
    ) -> List[MarketCandle]:
        if seed is not None:
            np.random.seed(seed)

        candles: List[MarketCandle] = []
        cur_price = s0
        cur_ts = start_ts

        # Set regime-specific parameters
        if regime == "trend":
            # Sustained bull momentum
            drift = 0.0008
            vol = 0.003
            jump_prob = 0.02
        elif regime == "mean_revert":
            # Choppy, tight range bound
            drift = 0.0
            vol = 0.002
            jump_prob = 0.01
        elif regime == "defensive":
            # High volatility downward cascading selloff
            drift = -0.0015
            vol = 0.008
            jump_prob = 0.05
        elif regime == "trap":
            # Fake breakout: sharp up-spike on low volume followed by dump
            drift = 0.0001
            vol = 0.004
            jump_prob = 0.08
        else:
            # Balanced general market
            drift = 0.0001
            vol = 0.003
            jump_prob = 0.02

        for i in range(n_candles):
            # Price shock
            shock = np.random.normal(drift, vol)
            if regime == "trap" and i in (50, 100, 150, 200, 250):
                # Fake pump tick
                shock = 0.025
            elif regime == "trap" and i in (51, 101, 151, 201, 251):
                # Immediate collapse
                shock = -0.035

            next_price = max(0.1, cur_price * (1.0 + shock))
            high = max(cur_price, next_price) * (1.0 + abs(np.random.normal(0, vol * 0.5)))
            low = min(cur_price, next_price) * (1.0 - abs(np.random.normal(0, vol * 0.5)))
            open_px = cur_price
            close_px = next_price
            volume = max(10.0, np.random.exponential(1000.0) * (2.0 if abs(shock) > 0.01 else 1.0))

            candles.append(MarketCandle(
                timestamp=cur_ts,
                open=round(float(open_px), 4),
                high=round(float(high), 4),
                low=round(float(low), 4),
                close=round(float(close_px), 4),
                volume=round(float(volume), 2),
            ))

            cur_price = next_price
            cur_ts += 60  # 1-minute candle

        return candles


# -----------------------------------------------------------------------------
# Weight Space Market Optimizer
# -----------------------------------------------------------------------------

class WeightSpaceMarketOptimizer:
    """Orchestrates model training, task vector arithmetic, TIES/DARE merging, and trap negation."""

    @staticmethod
    def train_specialized_model(
        candles: List[MarketCandle],
        target_action: int,
        base_model: Optional[ExitRouterMLP] = None,
        epochs: int = 15,
        lr: float = 0.005,
        seed: int = 42,
    ) -> ExitRouterMLP:
        """Train or fine-tune an ExitRouterMLP to specialize in a given action policy."""
        torch.manual_seed(seed)
        features = MarketFeatureExtractor.extract_features(candles)
        # Skip the first 30 warmup bars
        x_data = torch.tensor(features[30:], dtype=torch.float32)

        # Labels biased towards target action
        n_samples = len(x_data)
        y_data = torch.full((n_samples,), target_action, dtype=torch.long)

        in_feat = base_model.in_features if base_model else x_data.shape[1]
        h_dim = base_model.hidden_dim if base_model else 64
        n_cls = base_model.num_classes if base_model else len(ACTIONS)

        model = ExitRouterMLP(in_features=in_feat, hidden_dim=h_dim, num_classes=n_cls)
        if base_model:
            model.load_weights(base_model.get_weights())

        criterion = nn.CrossEntropyLoss()
        optimizer = optim.Adam(model.parameters(), lr=lr)

        model.train()
        for _ in range(epochs):
            optimizer.zero_grad()
            outputs = model(x_data)
            loss = criterion(outputs, y_data)
            loss.backward()
            optimizer.step()

        model.eval()
        return model

    @classmethod
    def create_regime_task_vectors(
        cls,
        base_model: ExitRouterMLP,
        regime_datasets: Dict[str, List[MarketCandle]],
    ) -> Tuple[Dict[str, Dict[str, torch.Tensor]], Dict[str, ExitRouterMLP]]:
        """Fine-tune specialized models on regime data and extract their task vectors."""
        base_weights = base_model.get_weights()
        task_vectors: Dict[str, Dict[str, torch.Tensor]] = {}
        specialized_models: Dict[str, ExitRouterMLP] = {}

        # Action mapping for regimes
        regime_action_map = {
            "trend": ACTION_TO_IDX["TRAIL_ONLY"],
            "mean_revert": ACTION_TO_IDX["FOREX_SCALP"],
            "defensive": ACTION_TO_IDX["FULL_EXIT"],
            "trap": ACTION_TO_IDX["SCALE_OUT"],
        }

        for regime_name, candles in regime_datasets.items():
            target_act = regime_action_map.get(regime_name, ACTION_TO_IDX["SKIM_PROFIT"])
            spec_model = cls.train_specialized_model(
                candles,
                target_action=target_act,
                base_model=base_model,
                epochs=12,
            )
            specialized_models[regime_name] = spec_model

            # Compute tau = theta_regime - theta_base
            tau = WeightSpaceToolkit.compute_task_vector(base_weights, spec_model.get_weights())
            task_vectors[regime_name] = tau

        return task_vectors, specialized_models

    @staticmethod
    def negate_trap_vector(
        weights: Dict[str, torch.Tensor],
        trap_vector: Dict[str, torch.Tensor],
        suppression_factor: float = 0.5,
    ) -> Dict[str, torch.Tensor]:
        """Negate trap vector: theta_clean = theta - lambda * tau_trap."""
        clean_weights: Dict[str, torch.Tensor] = {}
        for k, param in weights.items():
            if k in trap_vector:
                clean_weights[k] = param - suppression_factor * trap_vector[k]
            else:
                clean_weights[k] = param.clone()
        return clean_weights

    @staticmethod
    def dynamic_regime_slerp(
        model_defensive: ExitRouterMLP,
        model_alpha: ExitRouterMLP,
        regime_entropy: float,  # 0.0 (defensive) to 1.0 (full alpha)
    ) -> ExitRouterMLP:
        """SLERP interpolation between defensive safety policy and alpha-seeking policy."""
        w_def = model_defensive.get_weights()
        w_alpha = model_alpha.get_weights()

        merged_weights: Dict[str, torch.Tensor] = {}
        t = float(np.clip(regime_entropy, 0.0, 1.0))

        for k in w_def:
            if k in w_alpha:
                merged_weights[k] = WeightSpaceToolkit.slerp(t, w_def[k], w_alpha[k])
            else:
                merged_weights[k] = w_def[k].clone()

        interpolated_model = ExitRouterMLP(
            in_features=model_defensive.in_features,
            hidden_dim=model_defensive.hidden_dim,
            num_classes=model_defensive.num_classes,
        )
        interpolated_model.load_weights(merged_weights)
        interpolated_model.eval()
        return interpolated_model


# -----------------------------------------------------------------------------
# Backtest Simulation Engine (Walk-Forward & Friction Modeling)
# -----------------------------------------------------------------------------

class QuantitativeBacktester:
    """Walk-forward backtest simulator with realistic fees and execution slippage."""

    @staticmethod
    def simulate_trading(
        candles: List[MarketCandle],
        model: ExitRouterMLP,
        initial_cash: float = 1000.0,
        fee_bps: float = 26.0,  # Kraken taker fee: 0.26%
        slippage_bps: float = 5.0,  # Slippage: 0.05%
    ) -> Dict[str, Any]:
        features = MarketFeatureExtractor.extract_features(candles)
        # Skip warmup
        warmup = 35
        x_test = torch.tensor(features[warmup:], dtype=torch.float32)
        test_candles = candles[warmup:]

        actions, probs = model.predict_action(x_test)
        actions_np = actions.cpu().numpy()

        cash = initial_cash
        position_qty = 0.0
        entry_price = 0.0
        trades: List[Dict[str, Any]] = []
        equity_curve: List[float] = [cash]
        total_fees = 0.0

        fee_rate = fee_bps / 10000.0
        slip_rate = slippage_bps / 10000.0

        for i, candle in enumerate(test_candles):
            act = IDX_TO_ACTION[actions_np[i]]
            px = candle.close

            # Trading logic based on predicted action
            if act in ("TRAIL_ONLY", "FOREX_SCALP") and position_qty == 0.0:
                # Enter Long
                exec_px = px * (1.0 + slip_rate)
                allocation = cash * 0.95
                fee = allocation * fee_rate
                position_qty = (allocation - fee) / exec_px
                cash -= allocation
                total_fees += fee
                entry_price = exec_px
                trades.append({"type": "BUY", "time": candle.timestamp, "price": exec_px, "action": act})

            elif act == "FULL_EXIT" and position_qty > 0.0:
                # Full Exit
                exec_px = px * (1.0 - slip_rate)
                gross = position_qty * exec_px
                fee = gross * fee_rate
                net = gross - fee
                cash += net
                total_fees += fee
                trades.append({
                    "type": "SELL_FULL",
                    "time": candle.timestamp,
                    "price": exec_px,
                    "pnl": net - (position_qty * entry_price),
                    "action": act,
                })
                position_qty = 0.0

            elif act in ("SKIM_PROFIT", "SCALE_OUT") and position_qty > 0.0:
                # Partial Scale Out (50%)
                scale_qty = position_qty * 0.5
                exec_px = px * (1.0 - slip_rate)
                gross = scale_qty * exec_px
                fee = gross * fee_rate
                net = gross - fee
                cash += net
                total_fees += fee
                position_qty -= scale_qty
                trades.append({
                    "type": "SELL_SCALE",
                    "time": candle.timestamp,
                    "price": exec_px,
                    "pnl": net - (scale_qty * entry_price),
                    "action": act,
                })

            current_equity = cash + (position_qty * px)
            equity_curve.append(current_equity)

        # Close out any open position at end
        if position_qty > 0.0:
            final_px = test_candles[-1].close * (1.0 - slip_rate)
            gross = position_qty * final_px
            fee = gross * fee_rate
            cash += (gross - fee)
            total_fees += fee
            position_qty = 0.0

        final_equity = cash
        total_return_pct = (final_equity - initial_cash) / initial_cash * 100.0

        # Equity curve stats
        eq = np.array(equity_curve)
        peak = np.maximum.accumulate(eq)
        drawdowns = (eq - peak) / peak
        max_drawdown_pct = float(-np.min(drawdowns) * 100.0)

        # Returns Sharpe Ratio
        step_returns = np.diff(eq) / np.maximum(eq[:-1], 1e-8)
        std_ret = np.std(step_returns)
        sharpe = float((np.mean(step_returns) / std_ret * math.sqrt(252 * 1440)) if std_ret > 1e-8 else 0.0)

        return {
            "initial_cash": initial_cash,
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": round(sharpe, 2),
            "total_fees_paid": round(total_fees, 2),
            "trade_count": len(trades),
            "trades": trades[:10],  # summary snippet
            "step_returns": step_returns,
        }


# -----------------------------------------------------------------------------
# End-to-End Quantitative Research Pipeline Runner
# -----------------------------------------------------------------------------

def run_quantitative_market_experiment(
    project_name: str = "wsp_market_regime_fusion",
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute complete end-to-end WSP quantitative finance research experiment."""
    started_at = time.time()
    db = db_path or DEFAULT_DB
    ensure_research_experiments_schema(sqlite3.connect(db))

    LOGGER.info("Starting Quantitative Market Research Experiment: %s", project_name)

    # 1. Synthesize multi-regime training datasets
    trend_data = SyntheticMarketGenerator.generate_regime_candles("trend", n_candles=250, seed=101)
    revert_data = SyntheticMarketGenerator.generate_regime_candles("mean_revert", n_candles=250, seed=102)
    defensive_data = SyntheticMarketGenerator.generate_regime_candles("defensive", n_candles=250, seed=103)
    trap_data = SyntheticMarketGenerator.generate_regime_candles("trap", n_candles=250, seed=104)

    # 2. Extract features & perform PCA eigen-portfolio decomposition
    combined_candles = trend_data + revert_data + defensive_data
    feat_matrix = MarketFeatureExtractor.extract_features(combined_candles)
    pca_results = DataScienceToolkit.pca_decomposition(feat_matrix[35:], n_components=4)

    # 3. Initialize Base Model
    base_model = ExitRouterMLP(in_features=9, hidden_dim=64, num_classes=5)

    # 4. Extract regime task vectors
    task_vectors, specialized_models = WeightSpaceMarketOptimizer.create_regime_task_vectors(
        base_model,
        {
            "trend": trend_data,
            "mean_revert": revert_data,
            "defensive": defensive_data,
            "trap": trap_data,
        },
    )

    # 5. Layer Diagnostics: Cosine similarities between regime models and base
    cosine_trend = WeightSpaceToolkit.layer_cosine_diagnostics(
        base_model.get_weights(), specialized_models["trend"].get_weights()
    )
    cosine_defensive = WeightSpaceToolkit.layer_cosine_diagnostics(
        base_model.get_weights(), specialized_models["defensive"].get_weights()
    )

    # 6. Apply Weight Space Programming:
    # A) TIES Merging across positive alpha regimes (Trend, Mean Revert, Defensive)
    positive_vectors = [task_vectors["trend"], task_vectors["mean_revert"], task_vectors["defensive"]]
    ties_weights = WeightSpaceToolkit.ties_merge(
        base_weights=base_model.get_weights(),
        task_vectors=positive_vectors,
        weights=[0.45, 0.35, 0.20],
        top_k_ratio=0.3,
    )

    # B) Trap Negation: suppress harmful fakeout patterns
    clean_ties_weights = WeightSpaceMarketOptimizer.negate_trap_vector(
        weights=ties_weights,
        trap_vector=task_vectors["trap"],
        suppression_factor=0.35,
    )
    wsp_model = ExitRouterMLP(in_features=9, hidden_dim=64, num_classes=5)
    wsp_model.load_weights(clean_ties_weights)

    # C) Dynamic SLERP model: interpolate towards defensive in high entropy
    slerp_model = WeightSpaceMarketOptimizer.dynamic_regime_slerp(
        specialized_models["defensive"], wsp_model, regime_entropy=0.75
    )

    # 7. Walk-Forward Out-of-Sample Evaluation
    # Out-of-sample test candles (unseen seed, mixed regimes)
    test_candles = (
        SyntheticMarketGenerator.generate_regime_candles("trend", n_candles=120, seed=201)
        + SyntheticMarketGenerator.generate_regime_candles("trap", n_candles=80, seed=202)
        + SyntheticMarketGenerator.generate_regime_candles("defensive", n_candles=100, seed=203)
    )

    # Backtest Baseline vs WSP-Merged vs Dynamic-SLERP
    res_base = QuantitativeBacktester.simulate_trading(test_candles, base_model)
    res_wsp = QuantitativeBacktester.simulate_trading(test_candles, wsp_model)
    res_slerp = QuantitativeBacktester.simulate_trading(test_candles, slerp_model)

    # 8. Vectorized Monte Carlo Stress Testing on WSP returns
    step_ret = res_wsp["step_returns"]
    mu_daily = float(np.mean(step_ret) * 1440)
    sigma_daily = float(np.std(step_ret) * math.sqrt(1440))
    mc_paths = MonteCarloToolkit.simulate_gbm(
        s0=1000.0,
        mu=mu_daily,
        sigma=max(0.01, sigma_daily),
        t_years=0.1,  # ~36 days
        n_steps=100,
        n_paths=20000,
        seed=42,
    )
    mc_stats = MonteCarloToolkit.calculate_distribution_stats(mc_paths, alpha=0.05)

    duration_ms = (time.time() - started_at) * 1000.0

    # 9. Format Final Metrics & Artifacts
    metrics = {
        "pca_variance_top4": pca_results["explained_variance_ratio"],
        "cosine_trend_diagnostics": cosine_trend,
        "cosine_defensive_diagnostics": cosine_defensive,
        "backtest_comparison": {
            "baseline": {
                "total_return_pct": res_base["total_return_pct"],
                "max_drawdown_pct": res_base["max_drawdown_pct"],
                "sharpe_ratio": res_base["sharpe_ratio"],
                "trades": res_base["trade_count"],
            },
            "wsp_ties_trap_negated": {
                "total_return_pct": res_wsp["total_return_pct"],
                "max_drawdown_pct": res_wsp["max_drawdown_pct"],
                "sharpe_ratio": res_wsp["sharpe_ratio"],
                "trades": res_wsp["trade_count"],
            },
            "dynamic_slerp": {
                "total_return_pct": res_slerp["total_return_pct"],
                "max_drawdown_pct": res_slerp["max_drawdown_pct"],
                "sharpe_ratio": res_slerp["sharpe_ratio"],
                "trades": res_slerp["trade_count"],
            },
        },
        "monte_carlo_stress_test_20k": {
            "value_at_risk_5pct": mc_stats["value_at_risk_5pct"],
            "expected_shortfall_5pct": mc_stats["expected_shortfall_5pct"],
            "max_drawdown_pct": mc_stats["max_drawdown_pct"],
            "terminal_mean": mc_stats["terminal_mean"],
        },
        "latency_ms": round(duration_ms, 2),
    }

    # 10. Durable SQLite Logging
    spec = ScientificJobSpec(
        job_type="wsp_market_regime_fusion",
        project=project_name,
        hyperparameters={
            "ties_top_k": 0.3,
            "ties_weights": [0.45, 0.35, 0.20],
            "trap_suppression_lambda": 0.35,
            "slerp_regime_entropy": 0.75,
            "mc_paths": 20000,
        },
    )

    record_research_experiment(
        conn=sqlite3.connect(db),
        spec=spec,
        runtime_host="local_cpu:pytorch_wsp",
        metrics=metrics,
        status="completed",
        latency_ms=duration_ms,
    )

    return {
        "ok": True,
        "job_id": spec.job_id,
        "fingerprint": spec.compute_fingerprint(),
        "metrics": metrics,
    }
