"""Per-call token and cost ledger for swarm model calls.

Every record is one JSON line. Tokens are marked "measured" only when the provider
reported input and output counts. Character-count fallbacks are marked "estimated"
and never get a dollar cost, so estimates cannot silently mix into a cost comparison.
Dollar cost uses the Anthropic price table (USD per million tokens) for the models
listed in PRICES; any other model is recorded with cost_usd = null.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = ROOT / "artifacts" / "usage" / "usage_ledger.jsonl"

# USD per million tokens: (input, output). Source: the Claude API pricing table.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (8.00, 40.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

_lock = threading.Lock()
_run_label = ""


def set_run_label(label: str) -> None:
    """Tag subsequent records, e.g. 'baseline' or 'opus-haiku', so runs can be compared."""
    global _run_label
    _run_label = str(label or "")[:80]


def bare_model(model: str) -> str:
    text = str(model or "").strip()
    return text.rsplit("/", 1)[-1] if "/" in text else text


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(bare_model(model))
    if price is None:
        return None
    in_rate, out_rate = price
    return round((input_tokens * in_rate + output_tokens * out_rate) / 1_000_000, 8)


def build_record(role: str, result: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Turn one _invoke_provider_text result into a ledger record. Pure function, no I/O."""
    usage = result.get("token_usage") or {}
    raw_in, raw_out = usage.get("input"), usage.get("output")
    measured = isinstance(raw_in, (int, float)) and isinstance(raw_out, (int, float))
    model = str(result.get("model") or "")
    if measured:
        input_tokens, output_tokens = int(raw_in), int(raw_out)
        cost = cost_usd(model, input_tokens, output_tokens)
        basis = "measured"
    else:
        input_tokens = output_tokens = 0
        cost = None
        basis = "estimated"
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {
        "ts": stamp,
        "run": _run_label,
        "role": str(role or "")[:60],
        "provider": str(result.get("provider") or ""),
        "model": model,
        "ok": bool(result.get("ok")),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens_total_reported": int(result.get("tokens_used") or 0),
        "basis": basis,
        "cost_usd": cost,
    }


def record(role: str, result: dict[str, Any], *, path: Path = DEFAULT_LEDGER) -> dict[str, Any]:
    """Append one record. Never raises into the caller: logging must not break a swarm run."""
    try:
        entry = build_record(role, result)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, allow_nan=False)
        with _lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return entry
    except Exception:
        return {}


def summarize(path: Path = DEFAULT_LEDGER, *, run: str | None = None) -> dict[str, Any]:
    """Totals per role and per model, optionally for one run label."""
    totals: dict[str, Any] = {"calls": 0, "measured_calls": 0, "estimated_calls": 0,
                              "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                              "cost_unknown_calls": 0, "by_role": {}, "by_model": {}}
    if not path.exists():
        return totals
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if run is not None and row.get("run") != run:
                continue
            totals["calls"] += 1
            measured = row.get("basis") == "measured"
            totals["measured_calls" if measured else "estimated_calls"] += 1
            totals["input_tokens"] += int(row.get("input_tokens") or 0)
            totals["output_tokens"] += int(row.get("output_tokens") or 0)
            if row.get("cost_usd") is None:
                totals["cost_unknown_calls"] += 1
            else:
                totals["cost_usd"] += float(row["cost_usd"])
            for key, bucket in (("role", "by_role"), ("model", "by_model")):
                name = str(row.get(key) or "")
                slot = totals[bucket].setdefault(name, {"calls": 0, "input_tokens": 0,
                                                        "output_tokens": 0, "cost_usd": 0.0})
                slot["calls"] += 1
                slot["input_tokens"] += int(row.get("input_tokens") or 0)
                slot["output_tokens"] += int(row.get("output_tokens") or 0)
                if row.get("cost_usd") is not None:
                    slot["cost_usd"] += float(row["cost_usd"])
    totals["cost_usd"] = round(totals["cost_usd"], 6)
    return totals
