#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/train_benchmark_jev_v03_quip.py
=====================================
Trains and benchmarks `CryptoJevDecisionNet v0.3` on `ego`'s RTX 5060 Ti (16 GB VRAM),
combining:
  1. True Kraken Fee & Path-Dependent Exit Physics (`38.2 bps/leg` = `76.4 bps` round-trip
     Maker fee + `6.0 bps` spread = `82.4 bps` total cost, over 180-minute 1m high/low/close
     barrier paths with stop-before-target ordering: `-210 bps` `trend_break_stop` vs
     `+135 bps` `profit_shield` surge vs `180m` time exit).
  2. `QuIP` Walsh-Hadamard Incoherence + Market-Tape `AWQ` Quantization-Aware Training (`QAT`)
     with Straight-Through Estimator (`STE`) in rotated Sylvester-Hadamard space (`3-bit` & `2-bit`,
     preserving `FP16` prediction heads).
  3. Zero-Leakage Head-to-Head Benchmark against Production `v0.2` (`crypto_jev_decision_v02.pt`)
     across:
       - Unseen 9-Month Holdout (`Jul 2025 – Mar 2026`, 5 liquid pairs, calibrated strictly on `2025 H1`)
       - Real Nov 2026 Live Kraken `market-inputs/*.json` Exchange Snapshots (`38.2 bps/leg` fee)
     with `live_orders_submitted = 0` and without overwriting `v0.2`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

FEATURE_COLS: List[str] = [
    "ret_5m",
    "ret_15m",
    "ret_60m",
    "ret_240m",
    "ret_1440m",
    "vol_15m",
    "vol_60m",
    "volume_z_60m",
    "range_frac_60m",
    "pos_in_24h_range",
    "ema60_ratio",
    "pair_id_norm",
]

TRUE_KRAKEN_FEE_PER_LEG = 0.00382      # 38.2 bps/leg measured from live Kraken fills
TRUE_KRAKEN_SPREAD_ALLOWANCE = 0.00060 # 6.0 bps tight spread allowance -> 82.4 bps total RT cost
TRUE_RT_COST = 2.0 * TRUE_KRAKEN_FEE_PER_LEG + TRUE_KRAKEN_SPREAD_ALLOWANCE  # 0.00824 (82.4 bps)


class CryptoJevDecisionNet(nn.Module):
    """100% drop-in compatible 4-head JEV System-1 Decision Network."""

    def __init__(self, in_features: int = 12, hidden: int = 128, latent: int = 64):
        super().__init__()
        self.encoder = nn.Sequential(
            nn.Linear(in_features, hidden),
            nn.LayerNorm(hidden),
            nn.GELU(),
            nn.Dropout(0.15),
            nn.Linear(hidden, hidden),
            nn.LayerNorm(hidden),
            nn.GELU(),
            nn.Linear(hidden, latent),
        )
        self.predictor = nn.Sequential(
            nn.Linear(latent, latent),
            nn.GELU(),
            nn.Linear(latent, latent),
        )
        self.noul_head = nn.Sequential(
            nn.Linear(latent, hidden // 2),
            nn.GELU(),
            nn.Linear(hidden // 2, 2),
            nn.Sigmoid(),
        )
        self.choice_head = nn.Sequential(
            nn.Linear(latent, hidden // 2),
            nn.GELU(),
            nn.Linear(hidden // 2, 3),
        )
        self.score_head = nn.Sequential(
            nn.Linear(latent, hidden // 2),
            nn.GELU(),
            nn.Linear(hidden // 2, 1),
        )

    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        z = self.encoder(x)
        noul = self.noul_head(z)
        choice_logits = self.choice_head(z)
        score = self.score_head(z).squeeze(-1)
        return z, noul, choice_logits, score


# ---------------------------------------------------------------------------
# 1. Vectorized Path-Dependent 1-Minute Barrier Simulator (Zero Leakage)
# ---------------------------------------------------------------------------

def compute_path_dependent_returns(
    open_arr: np.ndarray,
    high_arr: np.ndarray,
    low_arr: np.ndarray,
    sample_indices: np.ndarray,
    horizon_min: int = 360,
    stop_gross: float = -0.0160,   # -160 bps protective stop (-242.4 bps net after 82.4 bps RT cost)
    target_gross: float = 0.0320,  # +320 bps 2:1 trend expansion target (+237.6 bps net -> ~50.5% break-even WR!)
) -> Tuple[np.ndarray, np.ndarray]:
    """
    For each sampled decision index `t` in `sample_indices`:
      - Enters at `entry_px = open_arr[t + 1]`
      - Tracks forward 1-minute bars `k = 1 .. horizon_min` (`t + 1 .. t + horizon_min`):
        * Long leg: if `low` hits `entry_px * (1 + stop_gross)` BEFORE `high` hits
          `entry_px * (1 + target_gross)` (with conservative stop-wins-ties on same 1m bar!),
          realizes `stop_gross`; else if `target_gross` hits first, realizes `target_gross`;
          else exits at `open_arr[t + 1 + horizon_min] / entry_px - 1.0`.
        * Symmetric Short leg computed identically (`stop` on upside `+210 bps`, `target` on `-145 bps`).
    Returns `(gross_long_ret, gross_short_ret)` arrays of shape `(len(sample_indices),)`.
    """
    n_samples = len(sample_indices)
    if n_samples == 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)

    entry_idx = sample_indices + 1
    entry_px = open_arr[entry_idx]
    time_exit_px = open_arr[sample_indices + 1 + horizon_min]

    # Build 2D index grid (n_samples, horizon_min) in chunks so RAM stays tiny (<200 MB)
    gross_long = np.empty(n_samples, dtype=np.float32)
    gross_short = np.empty(n_samples, dtype=np.float32)
    chunk = 16384
    offsets = np.arange(1, horizon_min + 1, dtype=np.int64)[None, :]

    for start in range(0, n_samples, chunk):
        end = min(n_samples, start + chunk)
        idx_c = sample_indices[start:end, None] + offsets
        ep = entry_px[start:end, None]
        tx = time_exit_px[start:end]

        hi_rel = (high_arr[idx_c] / ep) - 1.0
        lo_rel = (low_arr[idx_c] / ep) - 1.0

        # Long barriers (conservative: stop checked <= first on ties)
        l_stop_hit = lo_rel <= stop_gross
        l_targ_hit = hi_rel >= target_gross
        l_has_stop = l_stop_hit.any(axis=1)
        l_has_targ = l_targ_hit.any(axis=1)
        l_first_stop = np.where(l_has_stop, l_stop_hit.argmax(axis=1), horizon_min + 1)
        l_first_targ = np.where(l_has_targ, l_targ_hit.argmax(axis=1), horizon_min + 1)

        l_time_ret = (tx / entry_px[start:end]) - 1.0
        l_out = l_time_ret.copy()
        targ_wins = l_has_targ & (l_first_targ < l_first_stop)
        stop_wins = l_has_stop & (l_first_stop <= l_first_targ)
        l_out[targ_wins] = target_gross
        l_out[stop_wins] = stop_gross
        gross_long[start:end] = l_out.astype(np.float32)

        # Short barriers (upside move >= -stop_gross stops out short; drop <= -target_gross wins)
        s_stop_hit = hi_rel >= (-stop_gross)
        s_targ_hit = lo_rel <= (-target_gross)
        s_has_stop = s_stop_hit.any(axis=1)
        s_has_targ = s_targ_hit.any(axis=1)
        s_first_stop = np.where(s_has_stop, s_stop_hit.argmax(axis=1), horizon_min + 1)
        s_first_targ = np.where(s_has_targ, s_targ_hit.argmax(axis=1), horizon_min + 1)

        s_time_ret = -l_time_ret
        s_out = s_time_ret.copy()
        s_targ_wins = s_has_targ & (s_first_targ < s_first_stop)
        s_stop_wins = s_has_stop & (s_first_stop <= s_first_targ)
        s_out[s_targ_wins] = target_gross
        s_out[s_stop_wins] = stop_gross
        gross_short[start:end] = s_out.astype(np.float32)

    return gross_long, gross_short


# ---------------------------------------------------------------------------
# 2. QuIP Walsh-Hadamard Incoherence + Market-AWQ Straight-Through QAT
# ---------------------------------------------------------------------------

_HADAMARD_CACHE: Dict[Tuple[int, str], torch.Tensor] = {}


def get_sylvester_hadamard(n: int, device: torch.device) -> torch.Tensor:
    key = (n, str(device))
    if key in _HADAMARD_CACHE:
        return _HADAMARD_CACHE[key]
    assert n > 0 and (n & (n - 1)) == 0
    h = torch.tensor([[1.0]], dtype=torch.float32, device=device)
    while h.shape[0] < n:
        h = torch.cat([torch.cat([h, h], dim=1), torch.cat([h, -h], dim=1)], dim=0)
    h = h / math.sqrt(float(n))
    _HADAMARD_CACHE[key] = h
    return h


def quip_hadamard_awq_ste(w: torch.Tensor, act_rms: torch.Tensor, bits: int, seed_tag: int) -> torch.Tensor:
    """
    Differentiable Straight-Through Estimator (`STE`) for QuIP Walsh-Hadamard + Market-AWQ:
    Forward pass uses exact `bits`-bit quantized rotated weights; backward pass flows smooth
    gradients directly into master FP32 weights (`w + (w_rec - w).detach()`).
    """
    out_dim, in_dim = w.shape
    dev = w.device
    s = act_rms.to(dev).clamp(min=1e-4).pow(0.5)
    w_scaled = w * s.unsqueeze(0)

    p_out = 1 << (out_dim - 1).bit_length()
    p_in = 1 << (in_dim - 1).bit_length()
    w_pad = torch.zeros((p_out, p_in), dtype=torch.float32, device=dev)
    w_pad[:out_dim, :in_dim] = w_scaled

    g = torch.Generator(device="cpu").manual_seed(1337 + seed_tag)
    d_out = torch.where(torch.rand(p_out, generator=g) > 0.5, 1.0, -1.0).to(dev)
    d_in = torch.where(torch.rand(p_in, generator=g) > 0.5, 1.0, -1.0).to(dev)

    u_out = get_sylvester_hadamard(p_out, dev) * d_out.unsqueeze(1)
    u_in = get_sylvester_hadamard(p_in, dev) * d_in.unsqueeze(1)

    w_rot = u_out @ w_pad @ u_in.T
    qmax = float((1 << bits) - 1)
    w_min = w_rot.min(dim=1, keepdim=True).values
    w_max = w_rot.max(dim=1, keepdim=True).values
    scale = (w_max - w_min).clamp(min=1e-8) / qmax
    q = torch.round((w_rot - w_min) / scale).clamp(0.0, qmax)
    w_q_rot = q * scale + w_min

    w_rec_pad = u_out.T @ w_q_rot @ u_in
    w_rec = w_rec_pad[:out_dim, :in_dim] / s.unsqueeze(0)
    return w + (w_rec - w).detach()


def bake_quip_quantized_model(model: CryptoJevDecisionNet, rms_map: Dict[str, torch.Tensor], bits: int) -> Tuple[CryptoJevDecisionNet, float]:
    """Bake exact QuIP Walsh-Hadamard + Market-AWQ `bits`-bit weights (+ FP16 final heads) into a standalone model."""
    m = copy.deepcopy(model).cpu()
    m.eval()
    total_w = 0
    total_b = 0.0
    for idx, (name, mod) in enumerate(m.named_modules()):
        if not isinstance(mod, nn.Linear) or name.startswith("predictor"):
            continue
        w = mod.weight.data.clone()
        nw = w.numel()
        total_w += nw
        if name in ("noul_head.2", "choice_head.2", "score_head.2"):
            mod.weight.data = w.to(torch.float16).to(torch.float32)
            total_b += nw * 16.0
        else:
            act_rms = rms_map.get(name, torch.ones(w.shape[1])).cpu()
            with torch.no_grad():
                mod.weight.data = quip_hadamard_awq_ste(w, act_rms, bits=bits, seed_tag=idx * 17 + w.shape[0] * 31 + w.shape[1])
            total_b += nw * float(bits) + (w.shape[0] + w.shape[1]) * 16.0
    return m, total_b / max(1, total_w)


def collect_activation_rms(model: nn.Module, x_sample: torch.Tensor) -> Dict[str, torch.Tensor]:
    rms_map: Dict[str, torch.Tensor] = {}
    hooks = []

    def mk_hook(name: str):
        def _fn(_m, inp, _out):
            t = inp[0].detach()
            rms_map[name] = torch.sqrt(torch.mean(t * t, dim=0) + 1e-8).cpu()
        return _fn

    for name, mod in model.named_modules():
        if isinstance(mod, nn.Linear) and not name.startswith("predictor"):
            hooks.append(mod.register_forward_hook(mk_hook(name)))
    was_train = model.training
    model.eval()
    with torch.no_grad():
        model(x_sample)
    if was_train:
        model.train()
    for h in hooks:
        h.remove()
    return rms_map


def forward_with_quip_qat(
    model: CryptoJevDecisionNet,
    x: torch.Tensor,
    rms_map: Dict[str, torch.Tensor],
    bits: int = 3,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """Execute forward pass using STE fake-quantized QuIP Walsh-Hadamard weights so gradients adapt to quantization."""
    saved: List[Tuple[nn.Linear, nn.Parameter]] = []
    # Temporarily compute linear outputs via F.linear on STE weights
    orig_forwards = {}
    for idx, (name, mod) in enumerate(model.named_modules()):
        if not isinstance(mod, nn.Linear) or name.startswith("predictor"):
            continue
        is_head = name in ("noul_head.2", "choice_head.2", "score_head.2")
        act_rms = rms_map.get(name, torch.ones(mod.weight.shape[1]))
        seed_tag = idx * 17 + mod.weight.shape[0] * 31 + mod.weight.shape[1]
        orig_forwards[name] = mod.forward

        def make_ste_forward(m_ref: nn.Linear, head_flag: bool, rms_t: torch.Tensor, stag: int):
            def _ste_fwd(inp: torch.Tensor) -> torch.Tensor:
                if head_flag:
                    w_q = m_ref.weight + (m_ref.weight.to(torch.float16).to(m_ref.weight.dtype) - m_ref.weight).detach()
                else:
                    w_q = quip_hadamard_awq_ste(m_ref.weight, rms_t, bits=bits, seed_tag=stag)
                return F.linear(inp, w_q, m_ref.bias)
            return _ste_fwd

        mod.forward = make_ste_forward(mod, is_head, act_rms, seed_tag)  # type: ignore[method-assign]

    try:
        return model(x)
    finally:
        for name, mod in model.named_modules():
            if name in orig_forwards:
                mod.forward = orig_forwards[name]  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# 3. Purged 5-Year Dataset Builder + GPU Training + OOS Evaluation
# ---------------------------------------------------------------------------

def load_five_year_purged_splits(
    raw_root: Path,
    horizon_min: int = 180,
    stride_min: int = 30,
) -> Dict[str, Any]:
    import pandas as pd

    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT")
    train_end = pd.Timestamp("2025-01-01", tz="UTC")
    cal_end = pd.Timestamp("2025-07-01", tz="UTC")

    splits: Dict[str, List[pd.DataFrame]] = {"train": [], "calibration": [], "historical_test": []}
    pair_stats: Dict[str, Any] = {}

    for p_idx, sym in enumerate(symbols):
        pq_path = raw_root / sym / "data" / "train-00000-of-00001.parquet"
        if not pq_path.exists():
            continue
        df = pd.read_parquet(pq_path, columns=["timestamp", "open", "high", "low", "close", "volume"])
        ts = pd.DatetimeIndex(pd.to_datetime(df.pop("timestamp"), utc=True))
        df.index = ts

        close = df["close"].to_numpy(dtype=np.float64)
        high = df["high"].to_numpy(dtype=np.float64)
        low = df["low"].to_numpy(dtype=np.float64)
        open_p = df["open"].to_numpy(dtype=np.float64)
        vol = df["volume"].to_numpy(dtype=np.float64)

        s_close = pd.Series(close, index=ts)
        s_high = pd.Series(high, index=ts)
        s_low = pd.Series(low, index=ts)
        s_vol = pd.Series(vol, index=ts)
        logret = np.log(s_close).diff()

        feat = pd.DataFrame(index=ts)
        feat["ret_5m"] = s_close.pct_change(5, fill_method=None)
        feat["ret_15m"] = s_close.pct_change(15, fill_method=None)
        feat["ret_60m"] = s_close.pct_change(60, fill_method=None)
        feat["ret_240m"] = s_close.pct_change(240, fill_method=None)
        feat["ret_1440m"] = s_close.pct_change(1440, fill_method=None)
        feat["vol_15m"] = logret.rolling(15).std() * math.sqrt(60.0)
        feat["vol_60m"] = logret.rolling(60).std() * math.sqrt(60.0)

        vmean = s_vol.rolling(60).mean()
        vstd = s_vol.rolling(60).std()
        feat["volume_z_60m"] = ((s_vol - vmean) / (vstd + 1e-9)).clip(-5.0, 5.0)
        feat["range_frac_60m"] = (s_high.rolling(60).max() - s_low.rolling(60).min()) / (s_close + 1e-9)

        hi24 = s_high.rolling(1440).max()
        lo24 = s_low.rolling(1440).min()
        feat["pos_in_24h_range"] = ((s_close - lo24) / (hi24 - lo24 + 1e-9)).clip(0.0, 1.0)
        ema60 = s_close.ewm(span=60, adjust=False).mean()
        ema240 = s_close.ewm(span=240, adjust=False).mean()
        feat["ema60_ratio"] = (ema60 / (ema240 + 1e-9)) - 1.0
        feat["pair_id_norm"] = float(p_idx) / float(max(1, len(symbols) - 1))

        idx_s = pd.Series(ts, index=ts)
        cont = (idx_s - idx_s.shift(1440)) == pd.Timedelta(minutes=1440)
        fut_time = idx_s.shift(-(1 + horizon_min))
        fut_ok = (fut_time - idx_s) == pd.Timedelta(minutes=1 + horizon_min)
        valid_mask = (cont & fut_ok & feat[FEATURE_COLS].notna().all(axis=1)).to_numpy()

        valid_positions = np.flatnonzero(valid_mask)[::stride_min]
        gl, gs = compute_path_dependent_returns(open_p, high_arr=high, low_arr=low, sample_indices=valid_positions, horizon_min=horizon_min)

        sampled = feat.iloc[valid_positions].copy()
        sampled["decision_time"] = ts[valid_positions]
        sampled["exit_time"] = ts[valid_positions + 1 + horizon_min]
        sampled["pair"] = sym
        sampled["gross_long_path"] = gl
        sampled["gross_short_path"] = gs

        tr_m = (sampled["decision_time"] < train_end) & (sampled["exit_time"] < train_end)
        ca_m = (sampled["decision_time"] >= train_end) & (sampled["decision_time"] < cal_end) & (sampled["exit_time"] < cal_end)
        te_m = sampled["decision_time"] >= cal_end

        splits["train"].append(sampled.loc[tr_m])
        splits["calibration"].append(sampled.loc[ca_m])
        splits["historical_test"].append(sampled.loc[te_m])
        pair_stats[sym] = {
            "raw_1m_rows": int(len(df)),
            "train_rows": int(tr_m.sum()),
            "cal_rows": int(ca_m.sum()),
            "test_rows": int(te_m.sum()),
        }

    return {
        "train": pd.concat(splits["train"], ignore_index=True),
        "calibration": pd.concat(splits["calibration"], ignore_index=True),
        "historical_test": pd.concat(splits["historical_test"], ignore_index=True),
        "pair_stats": pair_stats,
    }


def evaluate_model_oos(
    model: nn.Module,
    f_mean: np.ndarray,
    f_std: np.ndarray,
    df_ca: Any,
    df_te: Any,
    device: torch.device,
    bpw: float = 32.0,
) -> Dict[str, Any]:
    x_ca = np.clip((df_ca[FEATURE_COLS].to_numpy(dtype=np.float32) - f_mean) / f_std, -8.0, 8.0)
    x_te = np.clip((df_te[FEATURE_COLS].to_numpy(dtype=np.float32) - f_mean) / f_std, -8.0, 8.0)
    gl_ca = df_ca["gross_long_path"].to_numpy(dtype=np.float32)
    gl_te = df_te["gross_long_path"].to_numpy(dtype=np.float32)

    model_dev = copy.deepcopy(model).to(device)
    model_dev.eval()
    with torch.no_grad():
        _, ca_noul, ca_ch, ca_sc = model_dev(torch.tensor(x_ca, device=device))
        ca_noul_np = ca_noul.cpu().numpy()
        ca_cp = F.softmax(ca_ch, dim=-1).cpu().numpy()
        ca_sc_bps = ca_sc.cpu().numpy() * 100.0

        _, te_noul, te_ch, te_sc = model_dev(torch.tensor(x_te, device=device))
        te_noul_np = te_noul.cpu().numpy()
        te_cp = F.softmax(te_ch, dim=-1).cpu().numpy()
        te_sc_bps = te_sc.cpu().numpy() * 100.0

    # Brier calibration score on unseen test split for P(net_long > +10 bps at 82.4 bps cost)
    y_true_win = ((gl_te - TRUE_RT_COST) > 0.0010).astype(np.float32)
    brier_score = float(np.mean((te_noul_np[:, 0] - y_true_win) ** 2))

    out_regimes: Dict[str, Any] = {}
    for reg_name, rt_cost in (
        ("true_kraken_82_4bps_rt", TRUE_RT_COST),
        ("kraken_pro_36_0bps_rt", 0.0036),
    ):
        # Strictly calibrate gate thresholds on 2025 H1 Calibration split
        best_cfg = (0.50, 0.02, 4.0)
        best_cal_obj = -1e9
        for n_thr in (0.28, 0.32, 0.36, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
            for c_marg in (-0.02, 0.00, 0.02, 0.05, 0.08):
                for s_hurdle in (-4.0, 0.0, 4.0, 8.0, 15.0):
                    sig = (
                        (ca_noul_np[:, 0] >= n_thr)
                        & ((ca_cp[:, 1] - ca_cp[:, 2]) >= c_marg)
                        & (ca_sc_bps >= s_hurdle)
                    )
                    ntr = int(sig.sum())
                    if ntr < 20:
                        continue
                    nets = gl_ca[sig] - rt_cost
                    mean_net = float(nets.mean())
                    total_net = float(nets.sum())
                    std_net = float(nets.std()) + 1e-6
                    sharpe_like = (mean_net / std_net) * math.sqrt(float(ntr))
                    obj = sharpe_like + 2.0 * total_net
                    if obj > best_cal_obj:
                        best_cal_obj = obj
                        best_cfg = (n_thr, c_marg, s_hurdle)

        n_thr, c_marg, s_hurdle = best_cfg
        te_sig = (
            (te_noul_np[:, 0] >= n_thr)
            & ((te_cp[:, 1] - te_cp[:, 2]) >= c_marg)
            & (te_sc_bps >= s_hurdle)
        )
        te_nets = gl_te[te_sig] - rt_cost
        ntr = int(len(te_nets))
        wins = int((te_nets > 0).sum()) if ntr else 0
        mean_bps = float(te_nets.mean() * 10000.0) if ntr else 0.0
        sum_bps = float(te_nets.sum() * 10000.0) if ntr else 0.0

        # Chronological non-overlapping 1-slot spot compounding ($1,000 start, 3h lock per trade!)
        eq = 1000.0
        peak = 1000.0
        max_dd = 0.0
        exec_trades = 0
        exec_wins = 0
        if ntr > 0:
            sub = df_te.loc[te_sig, ["decision_time", "exit_time", "pair", "gross_long_path"]].copy()
            sub["score_rank"] = te_sc_bps[te_sig] * te_noul_np[te_sig, 0]
            sub.sort_values(["decision_time", "score_rank"], ascending=[True, False], inplace=True)
            busy_until = None
            for row in sub.itertuples(index=False):
                if busy_until is not None and row.decision_time < busy_until:
                    continue
                net_r = float(row.gross_long_path) - rt_cost
                eq *= max(0.01, 1.0 + 0.98 * net_r)
                peak = max(peak, eq)
                max_dd = min(max_dd, (eq - peak) / peak)
                busy_until = row.exit_time
                exec_trades += 1
                if net_r > 0:
                    exec_wins += 1

        out_regimes[reg_name] = {
            "calibrated_gate": {"noul_thr": n_thr, "choice_margin": c_marg, "score_hurdle_bps": s_hurdle},
            "all_signal_count": ntr,
            "all_signal_win_rate_pct": round((wins / ntr) * 100.0, 2) if ntr else 0.0,
            "all_signal_mean_net_bps": round(mean_bps, 2),
            "all_signal_sum_net_bps": round(sum_bps, 1),
            "one_slot_exec_trades": exec_trades,
            "one_slot_win_rate_pct": round((exec_wins / exec_trades) * 100.0, 2) if exec_trades else 0.0,
            "one_slot_final_1000_usd": round(eq, 2),
            "one_slot_return_pct": round(((eq / 1000.0) - 1.0) * 100.0, 2),
            "one_slot_max_dd_pct": round(max_dd * 100.0, 2),
        }

    return {
        "effective_bits_per_weight": round(bpw, 2),
        "compression_vs_fp32": round(32.0 / bpw, 2),
        "oos_brier_calibration_score": round(brier_score, 5),
        "regimes": out_regimes,
    }


def evaluate_on_nov2026_live_snapshots(
    models_dict: Dict[str, Tuple[nn.Module, np.ndarray, np.ndarray, Dict[str, float]]],
    market_inputs_dir: Path,
) -> Dict[str, Any]:
    """
    Replay v0.2 vs v0.3 models on the real Nov 2026 `market-inputs/*.json` snapshots recorded on `ego`
    at the true `38.2 bps/leg` (`76.4 bps` round-trip) fee!
    """
    if not market_inputs_dir.is_dir():
        return {"available": False}

    # Collect chronological pair price tape + closed candle histories from recorded frames
    frames = []
    for p in sorted(market_inputs_dir.glob("*.json"), key=lambda f: f.stat().st_mtime):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("quotes") and d.get("bars"):
                at = float(d.get("at") or p.stat().st_mtime)
                frames.append((at, d))
        except Exception:
            continue
    frames.sort(key=lambda x: x[0])
    if len(frames) < 20:
        return {"available": False, "frames": len(frames)}

    # Build future price lookup per pair for causal realized forward return check (up to 3h ahead)
    pair_quotes: Dict[str, List[Tuple[float, float, float]]] = {}
    for at, d in frames:
        for pair, q in (d.get("quotes") or {}).items():
            bid, ask = float(q.get("bid") or 0), float(q.get("ask") or 0)
            if bid > 0 and ask >= bid:
                pair_quotes.setdefault(pair, []).append((at, bid, ask))

    results: Dict[str, Any] = {"frames_loaded": len(frames), "models": {}}

    for m_name, (mod, f_mean, f_std, gate_cfg) in models_dict.items():
        mod.eval()
        trades = 0
        wins = 0
        net_bps_list: List[float] = []
        busy_until = 0.0

        for at, d in frames:
            if at < busy_until:
                continue
            best_cand: Optional[Tuple[float, str, float]] = None
            for pair, c15 in (d.get("bars") or {}).items():
                q = (d.get("quotes") or {}).get(pair)
                if not q or not isinstance(c15, list) or len(c15) < 97:
                    continue
                bid, ask = float(q["bid"]), float(q["ask"])
                spread_bps = ((ask - bid) / bid) * 10000.0 if bid > 0 else 9999.0
                if spread_bps > 15.0:
                    continue
                c_arr = np.asarray([float(r[4]) for r in c15], dtype=np.float64)
                h_arr = np.asarray([float(r[2]) for r in c15], dtype=np.float64)
                l_arr = np.asarray([float(r[3]) for r in c15], dtype=np.float64)
                v_arr = np.asarray([float(r[5]) for r in c15], dtype=np.float64)
                if np.any(c_arr <= 0):
                    continue
                c_now = float(c_arr[-1])
                ret_15m = (c_now / float(c_arr[-2])) - 1.0
                ret_5m = ret_15m / 3.0
                ret_60m = (c_now / float(c_arr[-5])) - 1.0
                ret_240m = (c_now / float(c_arr[-17])) - 1.0
                ret_1440m = (c_now / float(c_arr[-97])) - 1.0
                lret15 = np.diff(np.log(c_arr[-17:]))
                vol_60m = float(np.std(lret15[-4:], ddof=1)) * 2.0 if len(lret15) >= 4 else 0.01
                vol_15m = vol_60m
                v4 = v_arr[-4:]
                vz = max(-5.0, min(5.0, (float(v4[-1]) - float(np.mean(v4))) / (float(np.std(v4)) + 1e-9)))
                rf60 = (float(np.max(h_arr[-4:])) - float(np.min(l_arr[-4:]))) / (c_now + 1e-9)
                pos24 = max(0.0, min(1.0, (c_now - float(np.min(l_arr[-96:]))) / (float(np.max(h_arr[-96:])) - float(np.min(l_arr[-96:])) + 1e-9)))
                ef, es = float(c_arr[-32]), float(c_arr[-32])
                for val in c_arr[-31:]:
                    ef = (2.0 / 5.0) * float(val) + (3.0 / 5.0) * ef
                    es = (2.0 / 17.0) * float(val) + (15.0 / 17.0) * es
                ema_r = (ef / (es + 1e-9)) - 1.0
                raw_v = np.asarray([[ret_5m, ret_15m, ret_60m, ret_240m, ret_1440m, vol_15m, vol_60m, vz, rf60, pos24, ema_r, 0.5]], dtype=np.float32)
                norm_v = np.clip((raw_v - f_mean) / f_std, -8.0, 8.0)
                with torch.no_grad():
                    _, nl, chl, sc = mod(torch.from_numpy(norm_v))
                    nl_long = float(nl[0, 0].item())
                    cp = F.softmax(chl, dim=-1)[0]
                    c_diff = float(cp[1].item() - cp[2].item())
                    sc_bps = float(sc[0].item()) * 100.0
                if nl_long >= gate_cfg["noul_thr"] and c_diff >= gate_cfg["choice_margin"] and sc_bps >= gate_cfg["score_hurdle_bps"]:
                    rank = sc_bps * nl_long
                    if best_cand is None or rank > best_cand[0]:
                        best_cand = (rank, pair, bid)

            if best_cand is not None:
                _, pair, entry_bid = best_cand
                fut_tape = [row for row in pair_quotes.get(pair, []) if at < row[0] <= at + 10800.0]
                if len(fut_tape) >= 3:
                    # Simulate Maker entry at bid, exit on +125 bps shield/ask or -210 bps stop or end of 3h window
                    exit_px = fut_tape[-1][2]
                    exit_at = fut_tape[-1][0]
                    peak_bps = 0.0
                    for fat, fbid, fask in fut_tape:
                        cur_bps = ((fask / entry_bid) - 1.0) * 10000.0
                        peak_bps = max(peak_bps, cur_bps)
                        if cur_bps <= -210.0:
                            exit_px = fbid
                            exit_at = fat
                            break
                        if peak_bps >= 125.0 and cur_bps <= max(95.0, peak_bps * 0.75):
                            exit_px = fask
                            exit_at = fat
                            break
                    gross_bps = ((exit_px / entry_bid) - 1.0) * 10000.0
                    net_bps = gross_bps - 76.4  # True 38.2 bps/leg Maker round-trip fee
                    trades += 1
                    if net_bps > 0:
                        wins += 1
                    net_bps_list.append(net_bps)
                    busy_until = exit_at + 300.0

        results["models"][m_name] = {
            "trades": trades,
            "wins": wins,
            "win_rate_pct": round((wins / trades) * 100.0, 2) if trades else 0.0,
            "total_net_bps_after_76_4bps_fee": round(float(sum(net_bps_list)), 2),
            "mean_net_bps_per_trade": round(float(np.mean(net_bps_list)), 2) if trades else 0.0,
        }

    return results


def train_jev_v03_and_benchmark(
    raw_root: Path,
    v02_ckpt_path: Path,
    out_dir: Path,
    market_inputs_dir: Path,
    fp32_epochs: int = 16,
    qat_epochs: int = 6,
    seed: int = 20261120,
) -> Dict[str, Any]:
    t0 = time.time()
    torch.manual_seed(seed)
    np.random.seed(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    gpu_name = torch.cuda.get_device_name(0) if device.type == "cuda" else "cpu"
    print(f"Running on device={device} ({gpu_name})")

    ds = load_five_year_purged_splits(raw_root=raw_root, horizon_min=360, stride_min=30)
    df_tr, df_ca, df_te = ds["train"], ds["calibration"], ds["historical_test"]
    print(f"Loaded purged 5y splits: train={len(df_tr):,} | cal={len(df_ca):,} | unseen_test={len(df_te):,}")

    X_tr_raw = df_tr[FEATURE_COLS].to_numpy(dtype=np.float32)
    f_mean = X_tr_raw.mean(axis=0, keepdims=True)
    f_std = np.maximum(X_tr_raw.std(axis=0, keepdims=True), 1e-6)
    X_tr = np.clip((X_tr_raw - f_mean) / f_std, -8.0, 8.0)

    gl_tr = df_tr["gross_long_path"].to_numpy(dtype=np.float32)
    gs_tr = df_tr["gross_short_path"].to_numpy(dtype=np.float32)

    # True 82.4 bps fee+spread net returns!
    net_long_tr = gl_tr - TRUE_RT_COST
    net_short_tr = gs_tr - TRUE_RT_COST
    win_hurdle = 0.0010  # +10 bps clear net profit ABOVE the 82.4 bps round-trip cost!

    y_noul = np.column_stack([
        (net_long_tr > win_hurdle).astype(np.float32),
        (net_short_tr > win_hurdle).astype(np.float32),
    ])
    y_choice = np.zeros(len(X_tr), dtype=np.int64)
    y_choice[(net_long_tr > win_hurdle) & (net_long_tr > net_short_tr)] = 1
    y_choice[(net_short_tr > win_hurdle) & (net_short_tr >= net_long_tr)] = 2
    # y_score is TRUE expected NET return in % after 82.4 bps round-trip fee+spread!
    y_score = np.clip(net_long_tr * 100.0, -8.0, 8.0)

    model = CryptoJevDecisionNet(in_features=12, hidden=128, latent=64).to(device)
    target_enc = copy.deepcopy(model.encoder).requires_grad_(False)
    opt = torch.optim.AdamW(model.parameters(), lr=1.2e-3, weight_decay=4e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=fp32_epochs)

    xt_t = torch.tensor(X_tr, device=device)
    yn_t = torch.tensor(y_noul, device=device)
    yc_t = torch.tensor(y_choice, device=device)
    ys_t = torch.tensor(y_score, device=device)

    c_counts = np.bincount(y_choice, minlength=3).astype(np.float32)
    c_weights = torch.tensor(float(len(y_choice)) / (3.0 * np.maximum(c_counts, 1.0)), device=device)

    batch_size = 2048
    n_tr = len(X_tr)
    model.train()
    for ep in range(fp32_epochs):
        perm = torch.randperm(n_tr - 1, device=device)
        for i in range(0, len(perm), batch_size):
            idx = perm[i : i + batch_size]
            nxt = torch.clamp(idx + 1, max=n_tr - 1)
            z, noul_p, ch_l, sc_p = model(xt_t[idx])
            with torch.no_grad():
                nz = target_enc(xt_t[nxt])

            byol_loss = F.mse_loss(model.predictor(z), nz)
            # Strictly proper unweighted BCE + Brier score so Noul outputs exact posterior probabilities!
            noul_loss = F.binary_cross_entropy(noul_p, yn_t[idx]) + 0.25 * F.mse_loss(noul_p, yn_t[idx])
            ch_loss = F.cross_entropy(ch_l, yc_t[idx], weight=c_weights)
            sc_loss = F.smooth_l1_loss(sc_p, ys_t[idx])

            loss = noul_loss + 0.45 * ch_loss + 0.45 * sc_loss + 0.08 * byol_loss
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 4.0)
            opt.step()
            with torch.no_grad():
                for tp, mp in zip(target_enc.parameters(), model.encoder.parameters()):
                    tp.mul_(0.99).add_(mp, alpha=0.01)
        sched.step()

    v03_fp32_cpu = copy.deepcopy(model).cpu().eval()
    rms_map = collect_activation_rms(v03_fp32_cpu, torch.tensor(X_tr[:16384], dtype=torch.float32))

    # Build PTQ 3-bit from v03_fp32
    v03_quip3b_ptq, bpw_3b = bake_quip_quantized_model(v03_fp32_cpu, rms_map, bits=3)

    # Precompute FP32 Teacher soft targets for Posterior-Matching / KL-Distillation QAT!
    teacher_gpu = copy.deepcopy(v03_fp32_cpu).to(device).eval()
    with torch.no_grad():
        _, t_noul_all, t_chl_all, t_sc_all = teacher_gpu(xt_t)
        t_ch_logp_all = F.log_softmax(t_chl_all, dim=-1)
        t_ch_prob_all = F.softmax(t_chl_all, dim=-1)

    # Phase B: QuIP Walsh-Hadamard + Market-AWQ + Posterior-Matching KL-Distillation QAT (3-bit & 2-bit)!
    def run_qat_branch(bits: int) -> Tuple[CryptoJevDecisionNet, float]:
        m_qat = copy.deepcopy(v03_fp32_cpu).to(device)
        m_qat.eval()  # Freeze dropout during QAT distillation so student matches teacher logits deterministically!
        q_opt = torch.optim.AdamW(m_qat.parameters(), lr=2.5e-4, weight_decay=5e-5)
        for _ in range(qat_epochs):
            perm = torch.randperm(n_tr, device=device)
            for i in range(0, len(perm), batch_size):
                idx = perm[i : i + batch_size]
                _, noul_p, ch_l, sc_p = forward_with_quip_qat(m_qat, xt_t[idx], rms_map, bits=bits)
                # Posterior-Matching Distillation: match FP32 teacher's full soft distributions + 20% ground-truth anchor
                kl_choice = F.kl_div(F.log_softmax(ch_l, dim=-1), t_ch_prob_all[idx], reduction="batchmean")
                mse_noul = F.mse_loss(noul_p, t_noul_all[idx]) + 0.2 * F.binary_cross_entropy(noul_p, yn_t[idx])
                sc_distill = F.smooth_l1_loss(sc_p, t_sc_all[idx]) + 0.2 * F.smooth_l1_loss(sc_p, ys_t[idx])
                loss = 2.0 * mse_noul + 1.5 * kl_choice + 0.5 * sc_distill
                q_opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(m_qat.parameters(), 2.0)
                q_opt.step()
        m_qat_cpu = m_qat.cpu().eval()
        rms_fresh = collect_activation_rms(m_qat_cpu, torch.tensor(X_tr[:16384], dtype=torch.float32))
        return bake_quip_quantized_model(m_qat_cpu, rms_fresh, bits=bits)

    print("Running QuIP Walsh-Hadamard + Market-AWQ QAT (3-bit & 2-bit)...")
    v03_quip3b_qat, bpw_3b_qat = run_qat_branch(bits=3)
    v03_quip2b_qat, bpw_2b_qat = run_qat_branch(bits=2)

    # Load production v0.2 for strict head-to-head comparison
    v02_raw = torch.load(v02_ckpt_path, map_location="cpu", weights_only=False)
    v02_model = CryptoJevDecisionNet(in_features=12, hidden=128, latent=64)
    v02_model.load_state_dict(v02_raw["state_dict"], strict=True)
    v02_model.eval()
    v02_mean = np.asarray(v02_raw["f_mean"], dtype=np.float32)
    v02_std = np.asarray(v02_raw["f_std"], dtype=np.float32)

    print("Evaluating all 5 models on Unseen 9-Month Holdout (Jul 2025 – Mar 2026)...")
    evals = {
        "v02_prod_fp32_baseline": evaluate_model_oos(v02_model, v02_mean, v02_std, df_ca, df_te, device, bpw=32.0),
        "v03_38bps_fp32": evaluate_model_oos(v03_fp32_cpu, f_mean, f_std, df_ca, df_te, device, bpw=32.0),
        "v03_38bps_quip3b_ptq": evaluate_model_oos(v03_quip3b_ptq, f_mean, f_std, df_ca, df_te, device, bpw=bpw_3b),
        "v03_38bps_quip3b_qat": evaluate_model_oos(v03_quip3b_qat, f_mean, f_std, df_ca, df_te, device, bpw=bpw_3b_qat),
        "v03_38bps_quip2b_qat": evaluate_model_oos(v03_quip2b_qat, f_mean, f_std, df_ca, df_te, device, bpw=bpw_2b_qat),
    }

    # Save new v0.3 checkpoints cleanly alongside v0.2 (zero overwrite of v0.2!)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_v03_fp32 = out_dir / "crypto_jev_decision_v03.pt"
    ckpt_v03_quip3b = out_dir / "crypto_jev_decision_v03_quip3b_qat.pt"

    for p_out, mod_obj, bpw_val in [
        (ckpt_v03_fp32, v03_fp32_cpu, 32.0),
        (ckpt_v03_quip3b, v03_quip3b_qat, bpw_3b_qat),
    ]:
        torch.save(
            {
                "version": "crypto_jev_decision_v0.3",
                "fee_calibrated_bps_per_leg": 38.2,
                "effective_bits_per_weight": round(bpw_val, 2),
                "state_dict": mod_obj.state_dict(),
                "f_mean": f_mean,
                "f_std": f_std,
                "feature_cols": FEATURE_COLS,
                "calibrated_gate_82_4bps_rt": evals["v03_38bps_quip3b_qat" if bpw_val < 16 else "v03_38bps_fp32"]["regimes"]["true_kraken_82_4bps_rt"]["calibrated_gate"],
            },
            p_out,
        )

    # Evaluate on Nov 2026 Live Recorded Kraken `market-inputs/*.json`!
    print("Replaying on Nov 2026 live recorded Kraken `market-inputs/*.json` snapshots...")
    live_snap_eval = evaluate_on_nov2026_live_snapshots(
        {
            "v02_prod_current_live_gate": (
                v02_model,
                v02_mean,
                v02_std,
                {"noul_thr": 0.395, "choice_margin": 0.02, "score_hurdle_bps": 4.0},
            ),
            "v03_38bps_fp32": (
                v03_fp32_cpu,
                f_mean,
                f_std,
                evals["v03_38bps_fp32"]["regimes"]["true_kraken_82_4bps_rt"]["calibrated_gate"],
            ),
            "v03_38bps_quip3b_qat": (
                v03_quip3b_qat,
                f_mean,
                f_std,
                evals["v03_38bps_quip3b_qat"]["regimes"]["true_kraken_82_4bps_rt"]["calibrated_gate"],
            ),
            "v03_38bps_quip2b_qat": (
                v03_quip2b_qat,
                f_mean,
                f_std,
                evals["v03_38bps_quip2b_qat"]["regimes"]["true_kraken_82_4bps_rt"]["calibrated_gate"],
            ),
        },
        market_inputs_dir=market_inputs_dir,
    )

    return {
        "benchmark": "crypto_jev_decision_v03_quip_38_2bps_rd",
        "live_orders_submitted": 0,
        "device": gpu_name,
        "elapsed_sec": round(time.time() - t0, 2),
        "true_fee_assumptions": {
            "maker_fee_bps_per_leg": 38.2,
            "round_trip_fee_bps": 76.4,
            "spread_allowance_bps": 6.0,
            "total_round_trip_cost_bps": 82.4,
        },
        "dataset_splits": {
            "pair_stats": ds["pair_stats"],
            "train_2021_2024": int(len(df_tr)),
            "calibration_2025H1": int(len(df_ca)),
            "unseen_test_2025H2_2026Q1": int(len(df_te)),
        },
        "checkpoints_written": {
            "v03_fp32": str(ckpt_v03_fp32),
            "v03_fp32_sha256": hashlib.sha256(ckpt_v03_fp32.read_bytes()).hexdigest(),
            "v03_quip3b_qat": str(ckpt_v03_quip3b),
            "v03_quip3b_qat_sha256": hashlib.sha256(ckpt_v03_quip3b.read_bytes()).hexdigest(),
        },
        "holdout_1_unseen_9month_test": evals,
        "holdout_2_nov2026_live_kraken_snapshots": live_snap_eval,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Train & Benchmark CryptoJevDecisionNet v0.3 with QuIP Walsh-Hadamard QAT & 38.2 bps Fee Physics")
    parser.add_argument("--raw-root", default="/mnt/14tb/sabrina-model-data/crypto-decision-v0.2/raw")
    parser.add_argument("--v02-ckpt", default="/home/ego/bot/Sabrina-windows-sync/artifacts/custom_model_arena/crypto_jev_decision_v02.pt")
    parser.add_argument("--out-dir", default="/home/ego/bot/Sabrina-windows-sync/artifacts/custom_model_arena")
    parser.add_argument("--market-inputs", default="/home/ego/.local/state/sabrina-kraken/execution-v1/market-inputs")
    parser.add_argument("--report-out", default="/home/ego/bot/Sabrina-windows-sync/artifacts/custom_model_arena/jev_v03_quip_benchmark_report.json")
    args = parser.parse_args()

    report = train_jev_v03_and_benchmark(
        raw_root=Path(args.raw_root),
        v02_ckpt_path=Path(args.v02_ckpt),
        out_dir=Path(args.out_dir),
        market_inputs_dir=Path(args.market_inputs),
    )
    out_p = Path(args.report_out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nSaved complete v0.3 QuIP benchmark report to: {out_p} in {report['elapsed_sec']}s")
    for m_key, m_val in report["holdout_1_unseen_9month_test"].items():
        r82 = m_val["regimes"]["true_kraken_82_4bps_rt"]
        print(
            f"  {m_key:26s} | {m_val['effective_bits_per_weight']:5.2f} bpw ({m_val['compression_vs_fp32']:4.1f}x) "
            f"| Brier={m_val['oos_brier_calibration_score']:.4f} "
            f"| 82.4bp OOS: {r82['one_slot_return_pct']:+6.2f}% (${r82['one_slot_final_1000_usd']:,.2f}, "
            f"WR={r82['one_slot_win_rate_pct']:5.1f}%, {r82['one_slot_exec_trades']}tr, DD={r82['one_slot_max_dd_pct']:+5.2f}%)"
        )
    print("  Nov 2026 Live Kraken Snapshots (76.4 bps fee):", json.dumps(report["holdout_2_nov2026_live_kraken_snapshots"]))


if __name__ == "__main__":
    main()
