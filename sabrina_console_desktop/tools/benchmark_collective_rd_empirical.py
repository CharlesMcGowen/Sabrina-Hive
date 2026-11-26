#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Empirical Bare-Metal Verification of the Sabrina Collective's R&D Proposals
===========================================================================
Executes 3 real hardware & live-market benchmarks (zero live orders submitted):
  1. Trading Specialist Quantization Benchmark (`CryptoJevDecisionNet` `crypto_jev_decision_v02.pt`
     evaluated on real out-of-sample Kraken 1m/15m OHLCV windows across 8 USD pairs):
       - FP32 Exact Baseline
       - Naive INT4 / INT3 / INT2 Uniform PTQ
       - BitNet-style 1.58-bit Ternary AbsMean PTQ (+ FP16 heads)
       - SpQR Outlier-Protected 3-bit (+ 1% FP16 sparse outliers + FP16 heads)
       - QuIP Walsh-Hadamard Incoherence + Market-Calibrated AWQ (2-bit & 3-bit + FP16 heads)
  2. Live Two-Host (`windows` <-> `ego` 192.168.2.69) Distributed Split-Layer Pipeline Parallelism
     vs. 80-Layer Tensor Parallelism TCP Wire Benchmark (verifying numerical equivalence + real LAN ms/token).
  3. Live 70.6B Model (`llama3.3:latest`, 42.52 GB Q4_K_M) Bare-Metal Memory & Inference Benchmark
     on `ego` (128 GB DDR4 Threadripper host).
"""

from __future__ import annotations

import copy
import json
import math
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import requests
import torch
from torch import nn
from torch.nn import functional as F

SABRINA_ROOT = Path(r"c:\Users\bcmcg\Desktop\bot\Sabrina")
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.kraken_jev_live_gate import (
    DEFAULT_CHOICE_MARGIN,
    DEFAULT_NOUL_THRESHOLD,
    MIN_EXPECTED_SCORE_BPS,
    compute_causal_major_features,
    load_jev_checkpoint,
)

CKPT_PATH = SABRINA_ROOT / "artifacts" / "custom_model_arena" / "crypto_jev_decision_v02.pt"
OUT_JSON = SABRINA_ROOT / "artifacts" / "collective_rd_empirical_verification.json"
PAIRS = ("XBTUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD", "LINKUSD", "DOTUSD", "AVAXUSD")


# ---------------------------------------------------------------------------
# 1. Fetch Real Kraken Historical 1m + 15m Candles & Build Market Dataset
# ---------------------------------------------------------------------------

def fetch_public_ohlc(pair: str, interval: int) -> List[List[float]]:
    url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": "SabrinaEmpiricalBench/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    res = data.get("result") or {}
    for k, v in res.items():
        if k != "last" and isinstance(v, list):
            return [[float(x) for x in row[:7]] for row in v]
    return []


def build_real_kraken_feature_matrix(f_mean: np.ndarray, f_std: np.ndarray) -> Tuple[torch.Tensor, torch.Tensor, Dict[str, Any]]:
    """Build real causal 12-feature market vectors across 8 Kraken USD pairs, split chronologically 50/50 into Calib vs OOS Test."""
    calib_rows: List[np.ndarray] = []
    test_rows: List[np.ndarray] = []
    pair_counts: Dict[str, int] = {}

    for pidx, pair in enumerate(PAIRS):
        try:
            c1 = fetch_public_ohlc(pair, 1)
            c15 = fetch_public_ohlc(pair, 15)
            if len(c1) < 200 or len(c15) < 120:
                continue
            # Align 15m timestamps for causal lookup at each 1m step
            ts15 = np.array([r[0] for r in c15], dtype=np.float64)
            pair_vecs: List[np.ndarray] = []
            # Step every 3 minutes across the 720-minute window for dense real setups
            for i in range(66, len(c1), 3):
                t_now = c1[i - 1][0]
                idx15 = int(np.searchsorted(ts15, t_now, side="right"))
                if idx15 < 98:
                    continue
                vec = compute_causal_major_features(c1[i - 65 : i], c15[idx15 - 97 : idx15], pidx, len(PAIRS))
                if vec is not None:
                    norm_vec = (vec - f_mean) / (f_std + 1e-8)
                    pair_vecs.append(norm_vec[0])
            if len(pair_vecs) >= 20:
                mid = len(pair_vecs) // 2
                calib_rows.extend(pair_vecs[:mid])
                test_rows.extend(pair_vecs[mid:])
                pair_counts[pair] = len(pair_vecs)
        except Exception as exc:
            print(f"  [OHLC warn] {pair}: {exc}")

    x_cal = torch.tensor(np.stack(calib_rows, axis=0), dtype=torch.float32)
    x_test = torch.tensor(np.stack(test_rows, axis=0), dtype=torch.float32)
    return x_cal, x_test, {"pairs_loaded": pair_counts, "calib_samples": int(x_cal.shape[0]), "oos_test_samples": int(x_test.shape[0])}


# ---------------------------------------------------------------------------
# 2. Mathematical Implementations of the Collective's Quantization Methods
# ---------------------------------------------------------------------------

def sylvester_hadamard(n: int) -> torch.Tensor:
    """Construct exact normalized orthogonal Sylvester Walsh-Hadamard matrix H_n (n = power of 2), H H^T = I."""
    assert n > 0 and (n & (n - 1)) == 0, f"n must be power of 2, got {n}"
    h = torch.tensor([[1.0]], dtype=torch.float32)
    while h.shape[0] < n:
        h = torch.cat([torch.cat([h, h], dim=1), torch.cat([h, -h], dim=1)], dim=0)
    return h / math.sqrt(float(n))


def uniform_row_quant_dequant(w: torch.Tensor, bits: int) -> torch.Tensor:
    """Per-output-channel asymmetric uniform integer quantization to `bits` bits."""
    qmax = (1 << bits) - 1
    w_min = w.min(dim=1, keepdim=True).values
    w_max = w.max(dim=1, keepdim=True).values
    scale = (w_max - w_min).clamp(min=1e-8) / float(qmax)
    q = torch.round((w - w_min) / scale).clamp(0, qmax)
    return q * scale + w_min


def ternary_bitnet_quant_dequant(w: torch.Tensor) -> torch.Tensor:
    """BitNet b1.58 AbsMean ternary quantization {-gamma, 0, +gamma} (~1.58 bits/weight)."""
    gamma = w.abs().mean(dim=1, keepdim=True).clamp(min=1e-8)
    q = torch.round(w / gamma).clamp(-1.0, 1.0)
    return q * gamma


def spqr_quant_dequant(w: torch.Tensor, act_rms: torch.Tensor, base_bits: int = 3, outlier_frac: float = 0.01) -> torch.Tensor:
    """SpQR (arXiv:2306.03078): isolate top `outlier_frac` activation-weighted sensitive weights in FP16, quantize remaining 99% to `base_bits`."""
    importance = w.abs() * act_rms.unsqueeze(0)
    k = max(1, int(round(outlier_frac * w.numel())))
    thresh = torch.topk(importance.reshape(-1), k).values.min()
    outlier_mask = importance >= thresh
    w_suppressed = w.masked_fill(outlier_mask, 0.0)
    w_q = uniform_row_quant_dequant(w_suppressed, base_bits)
    w_out = torch.where(outlier_mask, w.to(torch.float16).to(torch.float32), w_q)
    return w_out


def quip_hadamard_awq_quant_dequant(w: torch.Tensor, act_rms: torch.Tensor, bits: int) -> torch.Tensor:
    """
    QuIP-style Randomized Walsh-Hadamard Incoherence + Market-Tape AWQ Scaling:
      1. Scale columns by calibrated market activation RMS `s = act_rms^0.5`: W_s = W * diag(s)
      2. Pad input dim to next power-of-2 and apply orthogonal random-sign Walsh-Hadamard rotation:
         W_rot = H_out @ W_s @ (D_in @ H_in)^T  (smears weight outliers evenly across coordinates!)
      3. Quantize W_rot to `bits` (2-bit or 3-bit)
      4. Invert orthogonal Hadamard transform and unscale `diag(1/s)`.
    """
    out_dim, in_dim = w.shape
    s = act_rms.clamp(min=1e-4).pow(0.5)
    w_scaled = w * s.unsqueeze(0)

    # Pad dims to power of 2 for exact orthogonal Sylvester-Hadamard transform
    p_out = 1 << (out_dim - 1).bit_length()
    p_in = 1 << (in_dim - 1).bit_length()
    w_pad = torch.zeros((p_out, p_in), dtype=torch.float32)
    w_pad[:out_dim, :in_dim] = w_scaled

    g = torch.Generator().manual_seed(1337 + out_dim * 31 + in_dim)
    d_out = torch.where(torch.rand(p_out, generator=g) > 0.5, 1.0, -1.0)
    d_in = torch.where(torch.rand(p_in, generator=g) > 0.5, 1.0, -1.0)

    u_out = sylvester_hadamard(p_out) * d_out.unsqueeze(1)
    u_in = sylvester_hadamard(p_in) * d_in.unsqueeze(1)

    w_incoher = u_out @ w_pad @ u_in.T
    w_q_incoher = uniform_row_quant_dequant(w_incoher, bits)
    w_rec_pad = u_out.T @ w_q_incoher @ u_in
    w_rec = w_rec_pad[:out_dim, :in_dim] / s.unsqueeze(0)
    return w_rec


def collect_linear_input_rms(model: nn.Module, x_cal: torch.Tensor) -> Dict[str, torch.Tensor]:
    rms_map: Dict[str, torch.Tensor] = {}
    hooks = []

    def make_hook(name: str):
        def _hook(_mod, inp, _out):
            x = inp[0].detach()
            rms_map[name] = torch.sqrt(torch.mean(x * x, dim=0) + 1e-8)
        return _hook

    for name, mod in model.named_modules():
        if isinstance(mod, nn.Linear):
            hooks.append(mod.register_forward_hook(make_hook(name)))
    with torch.no_grad():
        model(x_cal)
    for h in hooks:
        h.remove()
    return rms_map


def evaluate_quantized_specialist(
    base_model: nn.Module,
    rms_map: Dict[str, torch.Tensor],
    x_test: torch.Tensor,
    scheme: str,
) -> Dict[str, Any]:
    m = copy.deepcopy(base_model)
    m.eval()

    total_weights = 0
    total_bits = 0.0

    for name, mod in m.named_modules():
        if not isinstance(mod, nn.Linear):
            continue
        # `predictor` is only used during self-supervised pretraining, not forward()
        if name.startswith("predictor"):
            continue
        w = mod.weight.data.clone()
        n_w = w.numel()
        total_weights += n_w
        is_final_head = name in ("noul_head.2", "choice_head.2", "score_head.2")
        act_rms = rms_map.get(name, torch.ones(w.shape[1]))

        if scheme == "fp32_exact":
            total_bits += n_w * 32.0
        elif scheme == "naive_int4_all":
            mod.weight.data = uniform_row_quant_dequant(w, 4)
            total_bits += n_w * 4.0 + w.shape[0] * 32.0
        elif scheme == "naive_int3_all":
            mod.weight.data = uniform_row_quant_dequant(w, 3)
            total_bits += n_w * 3.0 + w.shape[0] * 32.0
        elif scheme == "naive_int2_all":
            mod.weight.data = uniform_row_quant_dequant(w, 2)
            total_bits += n_w * 2.0 + w.shape[0] * 32.0
        elif scheme == "bitnet_1_58b_fp16heads":
            if is_final_head:
                mod.weight.data = w.to(torch.float16).to(torch.float32)
                total_bits += n_w * 16.0
            else:
                mod.weight.data = ternary_bitnet_quant_dequant(w)
                total_bits += n_w * 1.585 + w.shape[0] * 16.0
        elif scheme == "spqr_3b_1pct_fp16outliers":
            if is_final_head:
                mod.weight.data = w.to(torch.float16).to(torch.float32)
                total_bits += n_w * 16.0
            else:
                mod.weight.data = spqr_quant_dequant(w, act_rms, base_bits=3, outlier_frac=0.01)
                # 99% at 3 bits + 1% sparse FP16 (16b val + 16b col/row index = 32b)
                total_bits += n_w * (0.99 * 3.0 + 0.01 * 32.0) + w.shape[0] * 16.0
        elif scheme == "quip_hadamard_awq_3b_fp16heads":
            if is_final_head:
                mod.weight.data = w.to(torch.float16).to(torch.float32)
                total_bits += n_w * 16.0
            else:
                mod.weight.data = quip_hadamard_awq_quant_dequant(w, act_rms, bits=3)
                total_bits += n_w * 3.0 + (w.shape[0] + w.shape[1]) * 16.0
        elif scheme == "quip_hadamard_awq_2b_fp16heads":
            if is_final_head:
                mod.weight.data = w.to(torch.float16).to(torch.float32)
                total_bits += n_w * 16.0
            else:
                mod.weight.data = quip_hadamard_awq_quant_dequant(w, act_rms, bits=2)
                total_bits += n_w * 2.0 + (w.shape[0] + w.shape[1]) * 16.0
        else:
            raise ValueError(scheme)

    with torch.no_grad():
        _, noul_ref, logits_ref, score_ref = base_model(x_test)
        _, noul_q, logits_q, score_q = m(x_test)

        p_ref = F.softmax(logits_ref, dim=-1)
        p_q = F.softmax(logits_q, dim=-1)

        # KL(p_ref || p_q) calibration divergence
        kl_div = float(
            torch.sum(p_ref * (torch.log(p_ref.clamp(min=1e-9)) - torch.log(p_q.clamp(min=1e-9))), dim=-1).mean().item()
        )
        noul_mae = float((noul_q - noul_ref).abs().mean().item())
        noul_p99 = float(torch.quantile((noul_q - noul_ref).abs().reshape(-1), 0.99).item())
        score_rmse_bps = float(torch.sqrt(torch.mean((score_q - score_ref) ** 2)).item())

        # Exact production JEV entry gate boolean decision:
        #   noul_hazard = noul[:, 0] <= DEFAULT_NOUL_THRESHOLD
        #   score >= MIN_EXPECTED_SCORE_BPS
        #   long_prob (p[:, 0] + p[:, 1] - p[:, 2]) >= DEFAULT_CHOICE_MARGIN
        def gate_pass(noul_t, prob_t, sc_t):
            hazard_ok = noul_t[:, 0] <= DEFAULT_NOUL_THRESHOLD
            score_ok = sc_t >= MIN_EXPECTED_SCORE_BPS
            dir_edge = (prob_t[:, 0] + prob_t[:, 1]) - prob_t[:, 2]
            choice_ok = dir_edge >= DEFAULT_CHOICE_MARGIN
            return hazard_ok & score_ok & choice_ok

        pass_ref = gate_pass(noul_ref, p_ref, score_ref)
        pass_q = gate_pass(noul_q, p_q, score_q)
        gate_agreement_pct = float((pass_ref == pass_q).to(torch.float32).mean().item() * 100.0)
        choice_top1_agree_pct = float((p_ref.argmax(dim=-1) == p_q.argmax(dim=-1)).to(torch.float32).mean().item() * 100.0)

    eff_bpw = total_bits / max(1, total_weights)
    return {
        "scheme": scheme,
        "effective_bits_per_weight": round(eff_bpw, 2),
        "compression_vs_fp32": round(32.0 / eff_bpw, 2),
        "noul_prob_mae": round(noul_mae, 6),
        "noul_prob_p99_drift": round(noul_p99, 6),
        "choice_kl_divergence": round(kl_div, 7),
        "score_rmse_bps": round(score_rmse_bps, 4),
        "choice_top1_agreement_pct": round(choice_top1_agree_pct, 2),
        "live_entry_gate_agreement_pct": round(gate_agreement_pct, 2),
        "fp32_pass_count": int(pass_ref.sum().item()),
        "quant_pass_count": int(pass_q.sum().item()),
    }


# ---------------------------------------------------------------------------
# 3. Live Two-Host (`windows` <-> `ego`) Split-Layer Pipeline vs Tensor Parallel Benchmark
# ---------------------------------------------------------------------------

def send_msg(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_msg(sock: socket.socket) -> bytes:
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            raise EOFError("socket closed")
        hdr += chunk
    (n,) = struct.unpack("!I", hdr)
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise EOFError("socket closed mid-payload")
        buf.extend(chunk)
    return bytes(buf)


def run_live_two_host_lan_benchmark(ego_ip: str, port: int = 19877) -> Dict[str, Any]:
    """
    Launches a temporary PyTorch Stage-2 server on `ego` (`192.168.2.69:19877`) over SSH,
    verifies exact two-host split forward-pass equivalence (`windows` runs Stage 1 -> TCP -> `ego` runs Stage 2),
    and measures real physical LAN latency (p50 / p95 ms/token) for:
      - 70B Pipeline Parallelism (1 hop/token, d_model=8192 FP16 = 16,384 bytes)
      - 1T Pipeline Parallelism  (1 hop/token, d_model=12288 FP16 = 24,576 bytes)
      - 80-Layer Tensor Parallelism (80 All-Reduce syncs/token, d_model=12288 FP16)
    """
    remote_code = (
        "import socket, struct, torch\n"
        "torch.manual_seed(77)\n"
        "w2 = torch.randn(256, 256, dtype=torch.float16)\n"
        "srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"srv.bind(('0.0.0.0', {port}))\n"
        "srv.settimeout(25.0)\n"
        "srv.listen(1)\n"
        "conn, _ = srv.accept()\n"
        "conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)\n"
        "while True:\n"
        "    hdr = b''\n"
        "    while len(hdr) < 4:\n"
        "        c = conn.recv(4 - len(hdr))\n"
        "        if not c: break\n"
        "        hdr += c\n"
        "    if len(hdr) < 4: break\n"
        "    (n,) = struct.unpack('!I', hdr)\n"
        "    if n == 0: break\n"
        "    buf = bytearray()\n"
        "    while len(buf) < n:\n"
        "        buf.extend(conn.recv(min(65536, n - len(buf))))\n"
        "    mode = buf[0]\n"
        "    raw = bytes(buf[1:])\n"
        "    if mode == 1:\n"
        "        h = torch.frombuffer(bytearray(raw), dtype=torch.float16).reshape(1, 256)\n"
        "        out = torch.nn.functional.gelu(h) @ w2.T\n"
        "        rep = out.numpy().tobytes()\n"
        "    else:\n"
        "        rep = raw\n"
        "    conn.sendall(struct.pack('!I', len(rep)) + rep)\n"
        "conn.close()\n"
        "srv.close()\n"
    )

    proc = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", f"ego@{ego_ip}", f"python3 -c {json.dumps(remote_code)}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(1.4)

    try:
        sock = socket.create_connection((ego_ip, port), timeout=8.0)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # 1. Verify exact Two-Host PyTorch Split Pipeline Forward Pass (Windows Stage 1 -> Ego Stage 2)
        torch.manual_seed(42)
        w1_win = torch.randn(256, 256, dtype=torch.float16)
        x_in = torch.randn(1, 256, dtype=torch.float16)
        torch.manual_seed(77)
        w2_ref = torch.randn(256, 256, dtype=torch.float16)

        # Single-host local reference
        h_local = x_in @ w1_win.T
        y_ref = F.gelu(h_local) @ w2_ref.T

        # Two-host split execution over TCP
        send_msg(sock, b"\x01" + h_local.numpy().tobytes())
        y_ego_bytes = recv_msg(sock)
        y_two_host = torch.frombuffer(bytearray(y_ego_bytes), dtype=torch.float16).reshape(1, 256)
        max_abs_diff = float((y_ref.float() - y_two_host.float()).abs().max().item())

        # 2. Warmup socket (5 hops)
        dummy_16k = b"\x02" + (b"\x5a" * 16384)
        for _ in range(5):
            send_msg(sock, dummy_16k)
            recv_msg(sock)

        # 3. Measure 70B Pipeline-Parallel per-token hop (16,384 B FP16 activation, d_model=8192)
        pp70_ms = []
        for _ in range(60):
            t0 = time.perf_counter()
            send_msg(sock, dummy_16k)
            recv_msg(sock)
            pp70_ms.append((time.perf_counter() - t0) * 1000.0)

        # 4. Measure 1T Pipeline-Parallel per-token hop (24,576 B FP16 activation, d_model=12288)
        dummy_24k = b"\x02" + (b"\x5a" * 24576)
        pp1t_ms = []
        for _ in range(60):
            t0 = time.perf_counter()
            send_msg(sock, dummy_24k)
            recv_msg(sock)
            pp1t_ms.append((time.perf_counter() - t0) * 1000.0)

        # 5. Measure 80-Layer Tensor-Parallel All-Reduce per-token overhead (80 sequential syncs per token!)
        tp80_ms = []
        for _ in range(5):
            t0 = time.perf_counter()
            for _layer in range(80):
                send_msg(sock, dummy_24k)
                recv_msg(sock)
            tp80_ms.append((time.perf_counter() - t0) * 1000.0)

        sock.sendall(struct.pack("!I", 0))
        sock.close()
        proc.wait(timeout=5)

        return {
            "ok": True,
            "hosts": f"windows -> ego ({ego_ip}:{port})",
            "two_host_pytorch_split_max_abs_diff": max_abs_diff,
            "pipeline_parallel_70b_16kb_rtt_ms": {
                "p50_ms": round(float(np.median(pp70_ms)), 3),
                "p95_ms": round(float(np.percentile(pp70_ms, 95)), 3),
                "one_way_est_ms": round(float(np.median(pp70_ms)) / 2.0, 3),
            },
            "pipeline_parallel_1t_24kb_rtt_ms": {
                "p50_ms": round(float(np.median(pp1t_ms)), 3),
                "p95_ms": round(float(np.percentile(pp1t_ms, 95)), 3),
                "one_way_est_ms": round(float(np.median(pp1t_ms)) / 2.0, 3),
            },
            "tensor_parallel_80layers_per_token_ms": {
                "median_ms_per_token": round(float(np.median(tp80_ms)), 2),
                "max_token_cap_from_network_alone_tok_s": round(1000.0 / float(np.median(tp80_ms)), 2),
            },
        }
    except Exception as exc:
        proc.kill()
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 4. Live 70.6B Model (`llama3.3:latest`, 42.52 GB) Execution on `ego` (128 GB RAM)
# ---------------------------------------------------------------------------

def run_live_70b_model_probe(ego_ip: str) -> Dict[str, Any]:
    """Load and execute `llama3.3:latest` (70.6B Q4_K_M, 42.52 GB) on `ego` and measure real RAM + tok/s."""
    mem_before = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", f"ego@{ego_ip}", "free -m | awk '/Mem:/ {print $2, $3, $7}'"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()

    t0 = time.time()
    try:
        resp = requests.post(
            f"http://{ego_ip}:11434/api/generate",
            json={
                "model": "llama3.3:latest",
                "prompt": "In one concise sentence, state why pipeline parallelism beats tensor parallelism on 1 Gbps Ethernet.",
                "stream": False,
                "keep_alive": "15s",
                "options": {"num_predict": 32, "temperature": 0.1},
            },
            timeout=210,
        )
        wall_sec = time.time() - t0
        resp.raise_for_status()
        d = resp.json()

        mem_during = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", f"ego@{ego_ip}", "free -m | awk '/Mem:/ {print $2, $3, $7}'; ollama ps"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

        load_sec = (d.get("load_duration") or 0) / 1e9
        eval_cnt = d.get("eval_count") or 0
        eval_sec = max((d.get("eval_duration") or 1) / 1e9, 0.001)
        prompt_cnt = d.get("prompt_eval_count") or 0
        prompt_sec = max((d.get("prompt_eval_duration") or 1) / 1e9, 0.001)

        return {
            "ok": True,
            "model": "llama3.3:latest (70.6B Q4_K_M, 42.52 GB)",
            "host": f"ego ({ego_ip}, 128 GB RAM)",
            "ram_mb_before_total_used_avail": mem_before,
            "ram_mb_during_and_ollama_ps": mem_during,
            "cold_load_sec": round(load_sec, 2),
            "prompt_eval_tok_s": round(prompt_cnt / prompt_sec, 2),
            "decode_tok_s": round(eval_cnt / eval_sec, 2),
            "eval_tokens": eval_cnt,
            "wall_sec": round(wall_sec, 2),
            "verbatim_response": (d.get("response") or "").strip(),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "wall_sec": round(time.time() - t0, 2)}


def main() -> None:
    t_all = time.time()
    ego_ip = socket.gethostbyname("ego.local")
    print(f"Resolved ego.local -> {ego_ip}")

    print("\n=== EXPERIMENT 1: LIVE KRAKEN TRADING SPECIALIST QUANTIZATION BENCHMARK ===")
    ckpt = load_jev_checkpoint(CKPT_PATH)
    base_model = ckpt["model"]
    x_cal, x_test, ds_meta = build_real_kraken_feature_matrix(ckpt["f_mean"], ckpt["f_std"])
    print(f"Built real Kraken dataset: {ds_meta['calib_samples']} calib rows, {ds_meta['oos_test_samples']} unseen OOS test rows across {len(ds_meta['pairs_loaded'])} pairs.")
    rms_map = collect_linear_input_rms(base_model, x_cal)

    schemes = [
        "fp32_exact",
        "naive_int4_all",
        "naive_int3_all",
        "naive_int2_all",
        "bitnet_1_58b_fp16heads",
        "spqr_3b_1pct_fp16outliers",
        "quip_hadamard_awq_3b_fp16heads",
        "quip_hadamard_awq_2b_fp16heads",
    ]
    quant_results = []
    for sc in schemes:
        res = evaluate_quantized_specialist(base_model, rms_map, x_test, sc)
        quant_results.append(res)
        print(
            f"  {sc:31s} | {res['effective_bits_per_weight']:5.2f} bpw ({res['compression_vs_fp32']:4.1f}x) "
            f"| NOUL MAE={res['noul_prob_mae']:.5f} | KL={res['choice_kl_divergence']:.6f} "
            f"| Score RMSE={res['score_rmse_bps']:.3f} bps | Gate Agree={res['live_entry_gate_agreement_pct']:6.2f}%"
        )

    print("\n=== EXPERIMENT 2: LIVE TWO-HOST (WINDOWS <-> EGO) PIPELINE VS TENSOR PARALLEL BENCHMARK ===")
    lan_bench = run_live_two_host_lan_benchmark(ego_ip)
    print(json.dumps(lan_bench, indent=2))

    print("\n=== EXPERIMENT 3: LIVE 70.6B MODEL (llama3.3:latest, 42.52 GB) EXECUTION ON EGO ===")
    probe_70b = run_live_70b_model_probe(ego_ip)
    print(json.dumps(probe_70b, indent=2))

    report = {
        "benchmark": "sabrina_collective_rd_empirical_verification_v1",
        "live_orders_submitted": 0,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_sec": round(time.time() - t_all, 2),
        "experiment_1_trading_specialist_quantization": {
            "checkpoint": str(CKPT_PATH),
            "checkpoint_sha256": ckpt["sha256"],
            "dataset": ds_meta,
            "results": quant_results,
        },
        "experiment_2_two_host_lan_pipeline_vs_tensor_parallel": lan_bench,
        "experiment_3_live_70b_execution_on_ego": probe_70b,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved full empirical benchmark report to: {OUT_JSON}")


if __name__ == "__main__":
    main()
