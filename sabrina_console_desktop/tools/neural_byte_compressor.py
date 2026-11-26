#!/usr/bin/env python3
"""Alias and direct entrypoint for Sabrina's Order-N PPM / BWT / LZMA Context-Mixing
+ Online Byte Transformer Hybrid Arithmetic Coder (`tools.neural_compressor_lab`).
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.neural_compressor_lab import (
    ArithmeticDecoder,
    ArithmeticEncoder,
    CausalByteBlock,
    DEFAULT_CONFIG,
    FastOrderNPPM,
    NeuralCompressorLab,
    RMSNorm,
    ReversibleWikiBWTContextCodec,
    SabrinaByteCompressor,
    classical_baselines,
    compress_with_model,
    decompress_with_model,
    get_compression_leaderboard_tool,
    load_benchmark_corpus,
    main,
    run_compression_experiment_tool,
)

__all__ = [
    "ArithmeticDecoder",
    "ArithmeticEncoder",
    "CausalByteBlock",
    "DEFAULT_CONFIG",
    "FastOrderNPPM",
    "NeuralCompressorLab",
    "RMSNorm",
    "ReversibleWikiBWTContextCodec",
    "SabrinaByteCompressor",
    "classical_baselines",
    "compress_with_model",
    "decompress_with_model",
    "get_compression_leaderboard_tool",
    "load_benchmark_corpus",
    "main",
    "run_compression_experiment_tool",
]

if __name__ == "__main__":
    main()
