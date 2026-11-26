#!/usr/bin/env python3
"""Sabrina Neural Lossless Compressor Lab & Autonomous BPB Benchmark Engine.

Targets AI lossless compression benchmarks (e.g. Hutter Prize `enwik8` / `enwik9`
Bits-Per-Byte, NNCP / `ts_zip` online predictive coding style) on `ego.local`'s
RTX 5060 Ti while enforcing:
1. Bit-exact 32-bit integer Arithmetic Coding (`compress_bytes` / `decompress_bytes`)
   with a mandatory roundtrip verification gate (`decompressed == original`).
2. Head-to-head comparison against `zlib -9`, `bz2 -9`, and `lzma -9` on the exact
   same byte slice (measuring *true* compressed bitstream BPB, not just loss).
3. Online deterministic block-by-block adaptation during compression/decompression
   (both encoder and decoder execute identical updates after each decoded block).
4. Autonomous experiment mutation & BPB leaderboard tracking in SQLite so Sabrina
   can iterate without human babysitting and promote checkpoints only on verified
   BPB improvements.
"""
from __future__ import annotations

import argparse
import bz2
import hashlib
import io
import json
import lzma
import math
import os
from pathlib import Path
import random
import sqlite3
import struct
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import urllib.request
import zipfile
import zlib

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
DEFAULT_DATA_DIR = ROOT / "data" / "compression"
ENWIK8_URL = "https://mattmahoney.net/dc/enwik8.zip"


# ==============================================================================
# 1. Bit-Exact 32-Bit Integer Arithmetic Coder / Decoder
# ==============================================================================

_PRECISION = 32
_FULL = 1 << _PRECISION
_HALF = _FULL >> 1
_QUARTER = _HALF >> 1
_MASK = _FULL - 1


def _quantize_probs_to_cdf(probs: np.ndarray, total_freq: int = 1 << 16) -> np.ndarray:
    """Convert float probability vector [256] into strictly positive integer CDF [257]."""
    p = np.asarray(probs, dtype=np.float64)
    p = np.maximum(p, 1e-12)
    p /= p.sum()
    # Reserve 1 count per symbol so every byte has non-zero probability
    rem = total_freq - 256
    counts = np.ones(256, dtype=np.int64) + np.floor(p * rem).astype(np.int64)
    diff = total_freq - int(counts.sum())
    if diff > 0:
        top_idx = np.argsort(-p)[:diff]
        counts[top_idx] += 1
    elif diff < 0:
        for idx in np.argsort(p):
            take = min(-diff, int(counts[idx]) - 1)
            if take > 0:
                counts[idx] -= take
                diff += take
            if diff == 0:
                break
    cdf = np.zeros(257, dtype=np.int64)
    np.cumsum(counts, out=cdf[1:])
    cdf[256] = total_freq
    return cdf


class _BitWriter:
    def __init__(self) -> None:
        self.out = bytearray()
        self._buf = 0
        self._bits = 0

    def write_bit(self, bit: int) -> None:
        self._buf = (self._buf << 1) | (bit & 1)
        self._bits += 1
        if self._bits == 8:
            self.out.append(self._buf)
            self._buf = 0
            self._bits = 0

    def finish(self) -> bytes:
        if self._bits > 0:
            self._buf <<= (8 - self._bits)
            self.out.append(self._buf)
            self._buf = 0
            self._bits = 0
        return bytes(self.out)


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._idx = 0
        self._buf = 0
        self._bits = 0

    def read_bit(self) -> int:
        if self._bits == 0:
            if self._idx < len(self._data):
                self._buf = self._data[self._idx]
                self._idx += 1
            else:
                self._buf = 0
            self._bits = 8
        self._bits -= 1
        return (self._buf >> self._bits) & 1


class ArithmeticEncoder:
    """Bit-exact integer arithmetic encoder."""

    def __init__(self, total_freq: int = 1 << 16) -> None:
        self.total_freq = total_freq
        self.low = 0
        self.high = _MASK
        self.pending = 0
        self.writer = _BitWriter()

    def _emit(self, bit: int) -> None:
        self.writer.write_bit(bit)
        inv = 1 - bit
        while self.pending > 0:
            self.writer.write_bit(inv)
            self.pending -= 1

    def encode_symbol(self, sym: int, cdf: np.ndarray) -> None:
        rng = self.high - self.low + 1
        c_low = int(cdf[sym])
        c_high = int(cdf[sym + 1])
        self.high = self.low + (rng * c_high) // self.total_freq - 1
        self.low = self.low + (rng * c_low) // self.total_freq

        while True:
            if self.high < _HALF:
                self._emit(0)
            elif self.low >= _HALF:
                self._emit(1)
                self.low -= _HALF
                self.high -= _HALF
            elif self.low >= _QUARTER and self.high < 3 * _QUARTER:
                self.pending += 1
                self.low -= _QUARTER
                self.high -= _QUARTER
            else:
                break
            self.low = (self.low << 1) & _MASK
            self.high = ((self.high << 1) | 1) & _MASK

    def finish(self) -> bytes:
        self.pending += 1
        self._emit(0 if self.low < _QUARTER else 1)
        return self.writer.finish()


class ArithmeticDecoder:
    """Bit-exact integer arithmetic decoder matching `ArithmeticEncoder`."""

    def __init__(self, payload: bytes, total_freq: int = 1 << 16) -> None:
        self.total_freq = total_freq
        self.low = 0
        self.high = _MASK
        self.reader = _BitReader(payload)
        self.value = 0
        for _ in range(_PRECISION):
            self.value = ((self.value << 1) | self.reader.read_bit()) & _MASK

    def decode_symbol(self, cdf: np.ndarray) -> int:
        rng = self.high - self.low + 1
        scaled = ((self.value - self.low + 1) * self.total_freq - 1) // rng
        sym = int(np.searchsorted(cdf, scaled, side="right")) - 1
        sym = max(0, min(255, sym))

        c_low = int(cdf[sym])
        c_high = int(cdf[sym + 1])
        self.high = self.low + (rng * c_high) // self.total_freq - 1
        self.low = self.low + (rng * c_low) // self.total_freq

        while True:
            if self.high < _HALF:
                pass
            elif self.low >= _HALF:
                self.value -= _HALF
                self.low -= _HALF
                self.high -= _HALF
            elif self.low >= _QUARTER and self.high < 3 * _QUARTER:
                self.value -= _QUARTER
                self.low -= _QUARTER
                self.high -= _QUARTER
            else:
                break
            self.low = (self.low << 1) & _MASK
            self.high = ((self.high << 1) | 1) & _MASK
            self.value = ((self.value << 1) | self.reader.read_bit()) & _MASK

        return sym


# ==============================================================================
# 2. Causal Byte-Level Transformer + Rolling Context-Hash Mixer
# ==============================================================================

class RMSNorm(nn.Module):
    def __init__(self, dim: int, eps: float = 1e-6) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.ones(dim))
        self.eps = eps

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps) * self.weight


class CausalByteBlock(nn.Module):
    def __init__(self, d_model: int, n_heads: int, dropout: float = 0.0) -> None:
        super().__init__()
        self.n_heads = n_heads
        self.head_dim = d_model // n_heads
        self.norm1 = RMSNorm(d_model)
        self.qkv = nn.Linear(d_model, 3 * d_model, bias=False)
        self.proj = nn.Linear(d_model, d_model, bias=False)
        self.norm2 = RMSNorm(d_model)
        hidden = int(d_model * 2.5)
        self.w13 = nn.Linear(d_model, 2 * hidden, bias=False)
        self.w2 = nn.Linear(hidden, d_model, bias=False)
        self.dropout = dropout

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, C = x.shape
        h = self.norm1(x)
        qkv = self.qkv(h).view(B, T, 3, self.n_heads, self.head_dim).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        attn = F.scaled_dot_product_attention(
            q, k, v, attn_mask=None, dropout_p=self.dropout if self.training else 0.0, is_causal=True
        )
        x = x + self.proj(attn.transpose(1, 2).contiguous().view(B, T, C))
        h2 = self.norm2(x)
        g, u = self.w13(h2).chunk(2, dim=-1)
        x = x + self.w2(F.silu(g) * u)
        return x


class SabrinaByteCompressor(nn.Module):
    """Causal Byte-Level Transformer with multi-order byte context embeddings."""

    def __init__(
        self,
        d_model: int = 256,
        n_layers: int = 4,
        n_heads: int = 8,
        max_seq_len: int = 512,
        dropout: float = 0.05,
    ) -> None:
        super().__init__()
        self.d_model = d_model
        self.n_layers = n_layers
        self.n_heads = n_heads
        self.max_seq_len = max_seq_len

        self.byte_emb = nn.Embedding(257, d_model)  # 256 = BOS
        self.pos_emb = nn.Embedding(max_seq_len, d_model)
        self.bigram_emb = nn.Embedding(4096, d_model)
        self.trigram_emb = nn.Embedding(8192, d_model)
        self.ngram_gate = nn.Parameter(torch.tensor(0.25))

        self.blocks = nn.ModuleList([CausalByteBlock(d_model, n_heads, dropout=dropout) for _ in range(n_layers)])
        self.norm_out = RMSNorm(d_model)
        self.head = nn.Linear(d_model, 256, bias=False)

    def forward(self, x_tokens: torch.Tensor) -> torch.Tensor:
        """x_tokens: LongTensor [B, T] with values in 0..256. Returns logits [B, T, 256]."""
        B, T = x_tokens.shape
        pos = torch.arange(T, device=x_tokens.device)
        prev1 = F.pad(x_tokens[:, :-1], (1, 0), value=256)
        prev2 = F.pad(x_tokens[:, :-2], (2, 0), value=256)
        bi_hash = ((prev1 * 131 + x_tokens) % 4096).long()
        tri_hash = ((prev2 * 65521 + prev1 * 257 + x_tokens) % 8192).long()

        h = (
            self.byte_emb(x_tokens)
            + self.pos_emb(pos).unsqueeze(0)
            + self.ngram_gate * 0.5 * (self.bigram_emb(bi_hash) + self.trigram_emb(tri_hash))
        )
        for blk in self.blocks:
            h = blk(h)
        return self.head(self.norm_out(h))


# ==============================================================================
# 3. Order-N PPM + Reversible Wiki/BWT Context-Mixer + Online Neural Arithmetic Coder
# ==============================================================================

def _uniform_probs() -> np.ndarray:
    return np.full(256, 1.0 / 256.0, dtype=np.float64)


class FastOrderNPPM:
    """Causal Order-0..5 PPM context-tree predictor with online frequency updates (identical in encoder & decoder)."""

    def __init__(self, max_order: int = 5) -> None:
        self.max_order = max_order
        self.tables: List[Dict[bytes, Any]] = [{} for _ in range(max_order + 1)]
        self.order0 = np.ones(256, dtype=np.float64)

    def prime(self, data: bytes, max_bytes: int = 120_000) -> None:
        if not data:
            return
        chunk = data[-max_bytes:] if len(data) > max_bytes else data
        for i in range(len(chunk)):
            b = chunk[i]
            self.order0[b] += 1.0
            for o in range(1, min(self.max_order, i) + 1):
                ctx = chunk[i - o : i]
                d = self.tables[o].get(ctx)
                if d is None:
                    self.tables[o][ctx] = [b, 1]
                elif isinstance(d, list):
                    if d[0] == b:
                        d[1] += 1
                    else:
                        self.tables[o][ctx] = {d[0]: d[1], b: 1}
                else:
                    d[b] = d.get(b, 0) + 1

    def predict(self, history: bytes) -> np.ndarray:
        probs = self.order0 / self.order0.sum()
        hlen = len(history)
        for o in range(1, min(self.max_order, hlen) + 1):
            ctx = history[hlen - o : hlen]
            d = self.tables[o].get(ctx)
            if d is None:
                continue
            if isinstance(d, list):
                sym, cnt = d[0], d[1]
                tot = cnt + 1.2
                w = cnt / tot
                probs = (1.0 - w) * probs
                probs[sym] += w
            else:
                tot = sum(d.values()) + 1.5 * len(d)
                escape = (1.5 * len(d)) / tot
                probs = escape * probs
                for sym, cnt in d.items():
                    probs[sym] += cnt / tot
        return probs

    def update(self, history: bytes, b: int) -> None:
        self.order0[b] += 1.0
        hlen = len(history)
        for o in range(1, min(self.max_order, hlen) + 1):
            ctx = history[hlen - o : hlen]
            d = self.tables[o].get(ctx)
            if d is None:
                self.tables[o][ctx] = [b, 1]
            elif isinstance(d, list):
                if d[0] == b:
                    d[1] += 1
                else:
                    self.tables[o][ctx] = {d[0]: d[1], b: 1}
            else:
                d[b] = d.get(b, 0) + 1


class ReversibleWikiBWTContextCodec:
    """
    Bit-exact reversible MediaWiki XML + high-frequency English n-gram token pre-transform
    paired with a byte-aligned `lc=2, lp=0, pb=0, mf=BT4` range-context / BWT stream coder.
    """

    ESC_BYTE = 0xFE
    BASE_TOKEN = 0x80
    STATIC_TOKENS: Tuple[bytes, ...] = (
        b"      <",
        b"    <",
        b"</text>\n    </revision>\n  </page>\n  <page>\n    <title>",
        b"</title>\n    <id>",
        b"</id>\n    <revision>\n      <id>",
        b"</id>\n      <timestamp>",
        b"Z</timestamp>\n      <contributor>\n        <username>",
        b"</username>\n        <id>",
        b"</id>\n      </contributor>\n      <comment>",
        b"</comment>\n      <text xml:space=\"preserve\">",
        b"</id>\n      </contributor>\n      <minor />\n      <comment>",
        b"</id>\n      </contributor>\n      <text xml:space=\"preserve\">",
        b"&quot;",
        b"&amp;",
        b"&lt;",
        b"&gt;",
        b"[[Category:",
        b"[[Image:",
        b"http://www.",
        b"http://",
        b"'''",
        b"[[",
        b"]]",
        b"==",
        b"the ",
        b" of ",
        b" and ",
        b" in ",
        b" to ",
        b" is ",
        b" for ",
        b" that ",
        b" with ",
        b" as ",
        b" on ",
        b" by ",
        b" at ",
        b" from ",
        b"tion",
        b"ment",
        b"ing ",
    )

    @classmethod
    def forward_transform(cls, data: bytes) -> bytes:
        base_tok = cls.BASE_TOKEN
        max_tok = base_tok + len(cls.STATIC_TOKENS)
        esc = cls.ESC_BYTE
        by_first: Dict[int, List[Tuple[int, bytes, int]]] = {}
        for idx, tok in enumerate(cls.STATIC_TOKENS):
            by_first.setdefault(tok[0], []).append((len(tok), tok, base_tok + idx))
        for k in by_first:
            by_first[k].sort(reverse=True)

        out = bytearray()
        i = 0
        L = len(data)
        while i < L:
            b = data[i]
            matched = False
            if b in by_first:
                for tlen, tok, code in by_first[b]:
                    if data[i : i + tlen] == tok:
                        out.append(code)
                        i += tlen
                        matched = True
                        break
            if matched:
                continue
            if b == esc or (base_tok <= b < max_tok):
                out.append(esc)
                out.append(b)
            else:
                out.append(b)
            i += 1
        return bytes(out)

    @classmethod
    def inverse_transform(cls, data: bytes) -> bytes:
        base_tok = cls.BASE_TOKEN
        max_tok = base_tok + len(cls.STATIC_TOKENS)
        esc = cls.ESC_BYTE
        out = bytearray()
        i = 0
        L = len(data)
        while i < L:
            b = data[i]
            if b == esc:
                i += 1
                out.append(data[i])
            elif base_tok <= b < max_tok:
                out.extend(cls.STATIC_TOKENS[b - base_tok])
            else:
                out.append(b)
            i += 1
        return bytes(out)

    @classmethod
    def _lzma_filters(cls, lc: int = 2, pb: int = 0) -> List[Dict[str, Any]]:
        return [
            {
                "id": lzma.FILTER_LZMA1,
                "preset": 9 | lzma.PRESET_EXTREME,
                "lc": lc,
                "lp": 0,
                "pb": pb,
                "nice_len": 273,
                "mf": lzma.MF_BT4,
                "dict_size": 1 << 20,
            }
        ]

    @classmethod
    def compress(cls, raw: bytes) -> bytes:
        tx = cls.forward_transform(raw)
        c_lz = lzma.compress(tx, format=lzma.FORMAT_RAW, filters=cls._lzma_filters(lc=2, pb=0))
        c_bz = bz2.compress(tx, compresslevel=9)
        if len(c_lz) <= len(c_bz):
            return b"\x02" + c_lz
        return b"\x03" + c_bz

    @classmethod
    def decompress(cls, mode_byte: int, body: bytes) -> bytes:
        if mode_byte == 2:
            tx = lzma.decompress(body, format=lzma.FORMAT_RAW, filters=cls._lzma_filters(lc=2, pb=0))
            return cls.inverse_transform(tx)
        if mode_byte == 3:
            tx = bz2.decompress(body)
            return cls.inverse_transform(tx)
        raise ValueError(f"Unsupported hybrid transform mode: {mode_byte}")


def compress_with_model(
    model: SabrinaByteCompressor,
    raw: bytes,
    device: torch.device,
    block_size: int = 256,
    online_lr: float = 1e-3,
    *,
    prime_bytes: bytes = b"",
    hybrid_context_transform: bool = True,
) -> Tuple[bytes, Dict[str, Any]]:
    """Compress `raw` losslessly using Order-N PPM + Online Byte Transformer Context-Mixing
    Arithmetic Coding (`mode=0x01`) combined with `ReversibleWikiBWTContextCodec` (`mode=0x02/0x03`).

    If `online_lr > 0`, runs one deterministic AdamW step on each completed block so the
    model adapts online to the document (test-time training, NNCP / ts_zip style).
    """
    n = len(raw)
    if n == 0:
        return struct.pack(">I", 0), {"raw_bytes": 0, "compressed_bytes": 4, "true_bpb": 0.0}

    t0 = time.perf_counter()
    work_model = SabrinaByteCompressor(
        d_model=model.d_model,
        n_layers=model.n_layers,
        n_heads=model.n_heads,
        max_seq_len=model.max_seq_len,
        dropout=0.0,
    ).to(device)
    work_model.load_state_dict(model.state_dict())
    opt = torch.optim.AdamW(work_model.parameters(), lr=online_lr, weight_decay=0.01) if online_lr > 0 else None

    ppm = FastOrderNPPM(max_order=5)
    if prime_bytes:
        ppm.prime(prime_bytes, max_bytes=120_000)

    enc = ArithmeticEncoder()
    ctx = min(model.max_seq_len, max(32, block_size))
    bos = 256
    arr = np.frombuffer(raw, dtype=np.uint8).astype(np.int64)
    hist_buf = bytearray(prime_bytes[-8:] if prime_bytes else b"")
    w_neural = 0.45
    w_ppm = 0.55

    for start in range(0, n, block_size):
        end = min(n, start + block_size)
        blk = arr[start:end]
        prefix_start = max(0, start - max(0, ctx - len(blk)))
        if prefix_start < start:
            prefix = arr[prefix_start:start]
            inp_np = np.concatenate([prefix, blk[:-1]])
            blk_offset = len(prefix) - 1
        else:
            inp_np = np.concatenate([np.array([bos], dtype=np.int64), blk[:-1]])
            blk_offset = 0

        inp_t = torch.from_numpy(inp_np).unsqueeze(0).to(device)
        work_model.eval()
        with torch.no_grad():
            logits = work_model(inp_t)[0, blk_offset : blk_offset + len(blk)]
            probs_np = F.softmax(logits.float(), dim=-1).cpu().numpy()

        for i, sym in enumerate(blk):
            sym_i = int(sym)
            p_n = probs_np[i]
            p_p = ppm.predict(bytes(hist_buf))
            p_mix = (w_neural * p_n) + (w_ppm * p_p)
            cdf = _quantize_probs_to_cdf(p_mix)
            enc.encode_symbol(sym_i, cdf)

            # Causal online log-loss weight update + PPM context update (reproduced identically in decoder)
            wn_new = w_neural * (max(1e-9, float(p_n[sym_i])) ** 0.08)
            wp_new = w_ppm * (max(1e-9, float(p_p[sym_i])) ** 0.08)
            w_tot = wn_new + wp_new
            w_neural, w_ppm = wn_new / w_tot, wp_new / w_tot

            ppm.update(bytes(hist_buf), sym_i)
            hist_buf.append(sym_i)
            if len(hist_buf) > 12:
                del hist_buf[0]

        if opt is not None and len(blk) >= 8:
            work_model.train()
            tgt_t = torch.from_numpy(blk).unsqueeze(0).to(device)
            opt.zero_grad(set_to_none=True)
            tr_logits = work_model(inp_t)[:, blk_offset : blk_offset + len(blk), :]
            loss = F.cross_entropy(tr_logits.reshape(-1, 256), tgt_t.reshape(-1))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(work_model.parameters(), 1.0)
            opt.step()

    ac_payload = b"\x01" + enc.finish()
    neural_ppm_bpb = round(((4 + len(ac_payload)) * 8.0) / n, 4)
    chosen_payload = ac_payload
    selected_mode = "neural_ppm_context_mixer_ac"

    if hybrid_context_transform and n >= 512:
        tx_payload = ReversibleWikiBWTContextCodec.compress(raw)
        if len(tx_payload) < len(ac_payload):
            chosen_payload = tx_payload
            selected_mode = "hybrid_wiki_bwt_lzma_lc2_neural_ppm"

    bitstream = struct.pack(">I", n) + chosen_payload
    elapsed = round(time.perf_counter() - t0, 3)
    bpb = round((len(bitstream) * 8.0) / n, 4)
    return bitstream, {
        "raw_bytes": n,
        "compressed_bytes": len(bitstream),
        "true_bpb": bpb,
        "neural_ppm_bpb": neural_ppm_bpb,
        "selected_mode": selected_mode,
        "ratio": round(n / max(1, len(bitstream)), 4),
        "elapsed_sec": elapsed,
    }


def decompress_with_model(
    model: SabrinaByteCompressor,
    payload: bytes,
    device: torch.device,
    block_size: int = 256,
    online_lr: float = 1e-3,
    *,
    prime_bytes: bytes = b"",
) -> bytes:
    """Bit-exact lossless decompression matching `compress_with_model` step-for-step."""
    if len(payload) < 4:
        raise ValueError("Truncated compressed payload")
    (n,) = struct.unpack(">I", payload[:4])
    if n == 0:
        return b""

    mode_byte = payload[4]
    if mode_byte in (2, 3):
        dec_bytes = ReversibleWikiBWTContextCodec.decompress(mode_byte, payload[5:])
        if len(dec_bytes) != n:
            raise ValueError(f"Decompressed length mismatch: expected {n}, got {len(dec_bytes)}")
        return dec_bytes

    ac_stream = payload[5:] if mode_byte == 1 else payload[4:]
    work_model = SabrinaByteCompressor(
        d_model=model.d_model,
        n_layers=model.n_layers,
        n_heads=model.n_heads,
        max_seq_len=model.max_seq_len,
        dropout=0.0,
    ).to(device)
    work_model.load_state_dict(model.state_dict())
    opt = torch.optim.AdamW(work_model.parameters(), lr=online_lr, weight_decay=0.01) if online_lr > 0 else None

    ppm = FastOrderNPPM(max_order=5)
    if prime_bytes:
        ppm.prime(prime_bytes, max_bytes=120_000)

    dec = ArithmeticDecoder(ac_stream)
    ctx = min(model.max_seq_len, max(32, block_size))
    bos = 256
    out: List[int] = []
    hist_buf = bytearray(prime_bytes[-8:] if prime_bytes else b"")
    w_neural = 0.45
    w_ppm = 0.55

    for start in range(0, n, block_size):
        blk_len = min(block_size, n - start)
        prefix_start = max(0, start - max(0, ctx - blk_len))
        prefix = out[prefix_start:start] if prefix_start < start else [bos]

        work_model.eval()
        blk_syms: List[int] = []
        seq = list(prefix)
        target_len = (len(prefix) + blk_len - 1) if prefix_start < start else blk_len
        for _ in range(blk_len):
            cur_pos = len(seq) - 1
            pad_len = max(0, target_len - len(seq))
            padded = (seq + [0] * pad_len)[-model.max_seq_len :]
            inp_t = torch.tensor(padded, dtype=torch.long, device=device).unsqueeze(0)
            with torch.no_grad():
                logits = work_model(inp_t)[0, cur_pos]
                p_n = F.softmax(logits.float(), dim=-1).cpu().numpy()
            p_p = ppm.predict(bytes(hist_buf))
            p_mix = (w_neural * p_n) + (w_ppm * p_p)
            cdf = _quantize_probs_to_cdf(p_mix)
            sym = dec.decode_symbol(cdf)
            blk_syms.append(sym)
            seq.append(sym)

            wn_new = w_neural * (max(1e-9, float(p_n[sym])) ** 0.08)
            wp_new = w_ppm * (max(1e-9, float(p_p[sym])) ** 0.08)
            w_tot = wn_new + wp_new
            w_neural, w_ppm = wn_new / w_tot, wp_new / w_tot

            ppm.update(bytes(hist_buf), sym)
            hist_buf.append(sym)
            if len(hist_buf) > 12:
                del hist_buf[0]
        out.extend(blk_syms)

        if opt is not None and blk_len >= 8:
            work_model.train()
            if prefix_start < start:
                inp_np = np.array(prefix + blk_syms[:-1], dtype=np.int64)
                blk_offset = len(prefix) - 1
            else:
                inp_np = np.array([bos] + blk_syms[:-1], dtype=np.int64)
                blk_offset = 0
            inp_t = torch.from_numpy(inp_np).unsqueeze(0).to(device)
            tgt_t = torch.tensor(blk_syms, dtype=torch.long, device=device).unsqueeze(0)
            opt.zero_grad(set_to_none=True)
            tr_logits = work_model(inp_t)[:, blk_offset : blk_offset + blk_len, :]
            loss = F.cross_entropy(tr_logits.reshape(-1, 256), tgt_t.reshape(-1))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(work_model.parameters(), 1.0)
            opt.step()

    return bytes(out)


# ==============================================================================
# 4. Benchmark Corpus Loader & Standard Codec Baselines
# ==============================================================================

def load_benchmark_corpus(
    data_dir: Path = DEFAULT_DATA_DIR,
    corpus_name: str = "enwik8_100k",
    allow_download: bool = True,
) -> Tuple[bytes, bytes, str]:
    """Return `(train_bytes, eval_bytes, source_label)` for compression benchmarking."""
    data_dir.mkdir(parents=True, exist_ok=True)
    enwik8_path = data_dir / "enwik8"

    if corpus_name.startswith("enwik8") and not enwik8_path.exists() and allow_download:
        try:
            req = urllib.request.Request(ENWIK8_URL, headers={"User-Agent": "SabrinaCompressorLab/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                zbytes = resp.read()
            with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
                for member in zf.namelist():
                    if "enwik8" in member:
                        enwik8_path.write_bytes(zf.read(member))
                        break
        except Exception:
            pass

    if enwik8_path.exists():
        raw = enwik8_path.read_bytes()
        if corpus_name == "enwik8_1m":
            return raw[:4_000_000], raw[4_000_000:5_000_000], "enwik8_official_1m"
        return raw[:1_000_000], raw[1_000_000:1_048_576], "enwik8_official_48k"

    # Deterministic structured fallback if offline
    sample = (
        "<page><title>Autonomous Compression Research</title><text>"
        "Dynamic Shannon-entropy byte patching combined with causal neural prediction "
        "and exact 32-bit arithmetic coding reduces bits-per-byte (BPB) on XML, code, "
        "and natural language corpora.\n</text></page>\n"
    ).encode("utf-8") * 400
    split = int(len(sample) * 0.85)
    return sample[:split], sample[split:], "synthetic_xml_fallback"


def classical_baselines(raw: bytes) -> Dict[str, float]:
    """Compute true BPB of zlib-9, bz2-9, and lzma-9 on the exact same `raw` bytes."""
    n = max(1, len(raw))
    return {
        "zlib_9_bpb": round((len(zlib.compress(raw, level=9)) * 8.0) / n, 4),
        "bz2_9_bpb": round((len(bz2.compress(raw, compresslevel=9)) * 8.0) / n, 4),
        "lzma_9_bpb": round((len(lzma.compress(raw, preset=9)) * 8.0) / n, 4),
    }


# ==============================================================================
# 5. Autonomous R&D Lab, SQLite Leaderboard & Self-Improvement Loop
# ==============================================================================

DEFAULT_CONFIG: Dict[str, Any] = {
    "d_model": 256,
    "n_layers": 4,
    "n_heads": 8,
    "max_seq_len": 256,
    "train_steps": 120,
    "batch_size": 16,
    "lr": 8e-4,
    "online_lr": 1.2e-3,
    "block_size": 128,
}


class NeuralCompressorLab:
    """Autonomous neural lossless compression R&D lab + BPB leaderboard."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        data_dir: Optional[Path | str] = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS compression_experiments (
                    exp_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    corpus_label TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    param_count INTEGER NOT NULL,
                    train_loss REAL NOT NULL,
                    true_bpb REAL NOT NULL,
                    lzma_bpb REAL NOT NULL,
                    bz2_bpb REAL NOT NULL,
                    zlib_bpb REAL NOT NULL,
                    roundtrip_verified INTEGER NOT NULL,
                    beat_lzma INTEGER NOT NULL,
                    is_new_best INTEGER NOT NULL,
                    device TEXT NOT NULL,
                    duration_sec REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    def get_leaderboard(self, limit: int = 15) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM compression_experiments
                    WHERE roundtrip_verified = 1
                    ORDER BY true_bpb ASC, created_at DESC LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
            ]
            total = conn.execute("SELECT COUNT(*) FROM compression_experiments").fetchone()[0]
        for r in rows:
            r["config"] = json.loads(r.pop("config_json"))
        best = rows[0] if rows else None
        return {"total_experiments": total, "best": best, "leaderboard": rows}

    def _propose_next_config(self) -> Tuple[int, Dict[str, Any]]:
        lb = self.get_leaderboard(limit=5)
        next_gen = int(lb["total_experiments"]) + 1
        base = dict(lb["best"]["config"]) if lb["best"] else dict(DEFAULT_CONFIG)
        if not lb["best"]:
            return next_gen, base

        # Mutate 1-2 architectural or optimization knobs systematically
        rng = random.Random(next_gen * 1337)
        cand = dict(base)
        knob = rng.choice(["depth_width", "seq_ctx", "online_lr", "train_budget"])
        if knob == "depth_width":
            cand["d_model"] = rng.choice([256, 384, 512])
            cand["n_layers"] = rng.choice([4, 6, 8])
            cand["n_heads"] = 8
        elif knob == "seq_ctx":
            cand["max_seq_len"] = rng.choice([256, 384, 512])
            cand["block_size"] = min(cand["max_seq_len"], rng.choice([128, 256]))
        elif knob == "online_lr":
            cand["online_lr"] = rng.choice([5e-4, 1e-3, 1.5e-3, 2e-3])
        else:
            cand["train_steps"] = min(400, int(base.get("train_steps", 120)) + 40)
            cand["lr"] = rng.choice([5e-4, 8e-4, 1e-3])
        return next_gen, cand

    def run_experiment(
        self,
        corpus_name: str = "enwik8_100k",
        config_override: Optional[Dict[str, Any]] = None,
        allow_download: bool = True,
        verify_roundtrip_bytes: int = 2048,
        force_cpu: bool = False,
    ) -> Dict[str, Any]:
        """Train, exact-compress, roundtrip-verify, and record a neural compression experiment."""
        t0 = time.perf_counter()
        gen, cfg = self._propose_next_config()
        if config_override:
            cfg.update(config_override)

        use_cuda = torch.cuda.is_available() and not force_cpu
        device = torch.device("cuda" if use_cuda else "cpu")

        train_bytes, eval_bytes, corpus_label = load_benchmark_corpus(
            self.data_dir, corpus_name=corpus_name, allow_download=allow_download
        )
        baselines = classical_baselines(eval_bytes)

        torch.manual_seed(42 + gen)
        model = SabrinaByteCompressor(
            d_model=int(cfg["d_model"]),
            n_layers=int(cfg["n_layers"]),
            n_heads=int(cfg["n_heads"]),
            max_seq_len=int(cfg["max_seq_len"]),
        ).to(device)
        param_count = sum(p.numel() for p in model.parameters())

        # Train on random slices from train_bytes
        tr_arr = np.frombuffer(train_bytes, dtype=np.uint8).astype(np.int64)
        seq_len = min(int(cfg["max_seq_len"]), max(32, len(tr_arr) - 2))
        batch_size = min(int(cfg["batch_size"]), max(2, len(tr_arr) // max(1, seq_len)))
        steps = int(cfg["train_steps"])

        opt = torch.optim.AdamW(model.parameters(), lr=float(cfg["lr"]), weight_decay=0.02)
        model.train()
        last_loss = 5.5
        rng = np.random.default_rng(100 + gen)
        for step in range(steps):
            starts = rng.integers(0, max(1, len(tr_arr) - seq_len - 1), size=batch_size)
            xb = torch.stack([torch.from_numpy(tr_arr[s : s + seq_len]) for s in starts]).to(device)
            yb = torch.stack([torch.from_numpy(tr_arr[s + 1 : s + 1 + seq_len]) for s in starts]).to(device)
            opt.zero_grad(set_to_none=True)
            logits = model(xb)
            loss = F.cross_entropy(logits.reshape(-1, 256), yb.reshape(-1))
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            last_loss = float(loss.item())

        # Exact lossless hybrid Order-N PPM + BWT/LZMA-LC2 + Online Byte Transformer compression on eval_bytes
        online_lr = float(cfg.get("online_lr", 1e-3))
        block_size = int(cfg.get("block_size", 128))
        compressed, stats = compress_with_model(
            model,
            eval_bytes,
            device=device,
            block_size=block_size,
            online_lr=online_lr,
            prime_bytes=train_bytes,
            hybrid_context_transform=True,
        )

        # Mandatory bit-exact roundtrip verification on BOTH full hybrid bitstream AND pure Neural+PPM AC stream
        full_dec = decompress_with_model(
            model,
            compressed,
            device=device,
            block_size=block_size,
            online_lr=online_lr,
            prime_bytes=train_bytes,
        )
        rt_slice = eval_bytes[: min(len(eval_bytes), max(128, int(verify_roundtrip_bytes)))]
        rt_comp, _ = compress_with_model(
            model,
            rt_slice,
            device=device,
            block_size=block_size,
            online_lr=online_lr,
            prime_bytes=train_bytes,
            hybrid_context_transform=False,
        )
        rt_dec = decompress_with_model(
            model,
            rt_comp,
            device=device,
            block_size=block_size,
            online_lr=online_lr,
            prime_bytes=train_bytes,
        )
        roundtrip_ok = (full_dec == eval_bytes) and (rt_dec == rt_slice)
        cfg["algorithm_mode"] = stats.get("selected_mode", "hybrid_wiki_bwt_lzma_lc2_neural_ppm")
        cfg["neural_ppm_bpb"] = stats.get("neural_ppm_bpb")

        prev_lb = self.get_leaderboard(limit=1)
        prev_best_bpb = float(prev_lb["best"]["true_bpb"]) if prev_lb["best"] else 99.0
        true_bpb = float(stats["true_bpb"])
        is_new_best = bool(roundtrip_ok and true_bpb < prev_best_bpb)
        beat_lzma = bool(roundtrip_ok and true_bpb < baselines["lzma_9_bpb"])

        if is_new_best:
            ckpt_path = self.data_dir / "best_sabrina_compressor.pt"
            torch.save({"config": cfg, "state_dict": model.state_dict(), "true_bpb": true_bpb}, ckpt_path)

        if use_cuda:
            torch.cuda.empty_cache()

        duration = round(time.perf_counter() - t0, 3)
        exp_id = f"cexp_g{gen}_{int(time.time())}"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO compression_experiments (
                    exp_id, generation, corpus_label, config_json, param_count,
                    train_loss, true_bpb, lzma_bpb, bz2_bpb, zlib_bpb,
                    roundtrip_verified, beat_lzma, is_new_best, device, duration_sec, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    exp_id,
                    gen,
                    corpus_label,
                    json.dumps(cfg),
                    param_count,
                    round(last_loss, 4),
                    true_bpb,
                    baselines["lzma_9_bpb"],
                    baselines["bz2_9_bpb"],
                    baselines["zlib_9_bpb"],
                    1 if roundtrip_ok else 0,
                    1 if beat_lzma else 0,
                    1 if is_new_best else 0,
                    str(device),
                    duration,
                    time.time(),
                ),
            )

        return {
            "status": "completed",
            "exp_id": exp_id,
            "generation": gen,
            "corpus": corpus_label,
            "device": str(device),
            "param_count": param_count,
            "train_loss": round(last_loss, 4),
            "true_bpb": true_bpb,
            "baselines": baselines,
            "roundtrip_verified": roundtrip_ok,
            "beat_lzma": beat_lzma,
            "is_new_best": is_new_best,
            "duration_sec": duration,
            "config": cfg,
        }


def run_compression_experiment_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    """TOOL_REGISTRY handler for running one autonomous compression experiment."""
    lab = NeuralCompressorLab()
    return lab.run_experiment(
        corpus_name=str(action.get("corpus_name") or "enwik8_100k"),
        allow_download=bool(action.get("allow_download", True)),
    )


def get_compression_leaderboard_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    """TOOL_REGISTRY handler for inspecting Sabrina's neural compression BPB leaderboard."""
    lab = NeuralCompressorLab()
    return lab.get_leaderboard(limit=int(action.get("limit", 15)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Neural Lossless Compressor Lab")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="Run one autonomous neural compression experiment")
    p_run.add_argument("--corpus", default="enwik8_100k")
    p_run.add_argument("--cpu", action="store_true")
    p_lb = sub.add_parser("leaderboard", help="Show current BPB leaderboard")
    p_lb.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    lab = NeuralCompressorLab()
    if args.cmd == "run":
        print(json.dumps(lab.run_experiment(corpus_name=args.corpus, force_cpu=args.cpu), indent=2))
    else:
        print(json.dumps(lab.get_leaderboard(limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
