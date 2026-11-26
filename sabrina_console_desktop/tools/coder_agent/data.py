#!/usr/bin/env python3
"""
data.py
=======
Load public English + Python instruction-tuning data (CodeAlpaca-20k + OASST1-en assistant turns),
filter aggressively to English/ASCII, convert every record to ChatML shape that
`trl.SFTTrainer(assistant_only_loss=True)` accepts.

The English guardrail is deliberately cheap and belt-and-suspenders: many "English-labeled" sources
still leak CJK, Cyrillic, and Arabic in edge cases. We reject records where fewer than 95% of chars
are in the ASCII/Latin-1 range OR the record's own `lang` field says non-English. If a record
survives both checks, we trust it.

Output format (per row): `{"messages": [{"role": "system"|"user"|"assistant", "content": str}, ...]}`.
The system turn is a small, consistent persona. User turn is the instruction. Assistant turn is the
answer -- this is the training target under assistant_only_loss.
"""
import argparse
import json
import os
import random
import re
from typing import Any, Dict, Iterable, List, Optional

SYSTEM_PROMPT = (
    "You are a careful Python coding assistant. When asked to write code, respond with a fenced "
    "```python code block; when asked a general programming question, answer clearly in English."
)

_ASCII_RE = re.compile(r"[\x20-\x7E\t\n\r]")


def ascii_ratio(text: str) -> float:
    """Fraction of chars that are printable ASCII (plus tab/newline). English + code is >0.95."""
    if not text:
        return 0.0
    return len(_ASCII_RE.findall(text)) / len(text)


def is_english_safe(text: str, min_ratio: float = 0.95) -> bool:
    """Reject records with non-ASCII load above tolerance -- protects the "English-only" invariant."""
    return ascii_ratio(text) >= min_ratio


def _clean(text: Optional[str]) -> str:
    return (text or "").strip()


# ── CodeAlpaca-20k adapter ──────────────────────────────────────────────────────
# Rows: {"instruction": str, "input": str (often empty), "output": str}

def codealpaca_to_chatml(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one CodeAlpaca record to ChatML. Skip if English filter fails or fields are empty."""
    instr = _clean(row.get("instruction"))
    inp = _clean(row.get("input"))
    out = _clean(row.get("output"))
    if not instr or not out:
        return None
    user = instr if not inp else f"{instr}\n\n{inp}"
    joined = user + "\n" + out
    if not is_english_safe(joined):
        return None
    return {"messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
        {"role": "assistant", "content": out},
    ]}


# ── OASST1 adapter ─────────────────────────────────────────────────────────────
# The `OpenAssistant/oasst1` dataset is a flat forest of messages with (message_id, parent_id,
# role, text, lang). To make chat pairs we walk each conversation tree and pull (prompter -> its
# best assistant reply) 2-turn dialogs. Higher-order threads (multi-turn) can be built later if
# we want longer contexts.

def _build_oasst_pairs(rows: Iterable[Dict[str, Any]], lang: str = "en") -> Iterable[Dict[str, Any]]:
    """Yield (prompter_text, assistant_text) pairs from a stream of oasst1 rows, English only."""
    by_id: Dict[str, Dict[str, Any]] = {}
    kids: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        if r.get("lang") != lang:
            continue
        by_id[r["message_id"]] = r
        parent = r.get("parent_id")
        if parent:
            kids.setdefault(parent, []).append(r)
    for msg_id, r in by_id.items():
        if r.get("role") != "prompter":
            continue
        replies = [c for c in kids.get(msg_id, []) if c.get("role") == "assistant"]
        if not replies:
            continue
        # rank the assistant replies: prefer non-deleted, non-flagged, high rank, then longest.
        def _score(reply):
            neg = 0
            for k in ("deleted", "review_result", "synthetic", "detoxify"):
                if isinstance(reply.get(k), (bool, int, float)) and reply.get(k):
                    neg -= 1
            return (neg, -(reply.get("rank") or 999), len(reply.get("text") or ""))
        best = max(replies, key=_score)
        yield {"prompter": r.get("text", ""), "assistant": best.get("text", "")}


def oasst_pair_to_chatml(pair: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Convert one prompter/assistant pair to ChatML, English-filtered."""
    user = _clean(pair.get("prompter"))
    ans = _clean(pair.get("assistant"))
    if not user or not ans:
        return None
    if not is_english_safe(user + "\n" + ans):
        return None
    return {"messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
        {"role": "assistant", "content": ans},
    ]}


# ── Combined loader ────────────────────────────────────────────────────────────

def load_codealpaca(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load `sahil2801/CodeAlpaca-20k` (streaming), convert, return list of ChatML records."""
    from datasets import load_dataset  # lazy: keeps unit tests hermetic when we monkey-patch
    ds = load_dataset("sahil2801/CodeAlpaca-20k", split="train", streaming=True)
    out: List[Dict[str, Any]] = []
    for row in ds:
        rec = codealpaca_to_chatml(row)
        if rec is not None:
            out.append(rec)
        if limit and len(out) >= limit:
            break
    return out


def load_code_instructions(dataset_id: str,
                           split: str = "train",
                           limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load an alpaca-schema code-instruction dataset and convert via codealpaca_to_chatml.

    Schema-flexible: reuses codealpaca_to_chatml, which reads instruction/input/output. Datasets
    that only carry instruction/output (no `input`) work unchanged -- the missing `input` becomes
    "". Datasets that use prompt/completion or text/output are mapped onto instruction/output
    first, so one loader covers the common code-instruction layouts without a per-dataset adapter.
    Used to make the mix CODE-HEAVY (CodeAlpaca + a Python-instruction set), replacing the chatty
    OASST1 turns that measurably cost ~6 pts HumanEval in the first full-FT run.
    """
    from datasets import load_dataset
    ds = load_dataset(dataset_id, split=split, streaming=True)
    out: List[Dict[str, Any]] = []
    for row in ds:
        # Normalize onto the alpaca instruction/input/output shape codealpaca_to_chatml expects.
        norm = dict(row)
        if "instruction" not in norm:
            if "prompt" in norm:
                norm["instruction"] = norm.get("prompt")
            elif "text" in norm:
                norm["instruction"] = norm.get("text")
        if "output" not in norm:
            if "completion" in norm:
                norm["output"] = norm.get("completion")
            elif "response" in norm:
                norm["output"] = norm.get("response")
        rec = codealpaca_to_chatml(norm)
        if rec is not None:
            out.append(rec)
        if limit and len(out) >= limit:
            break
    return out


def load_oasst_en(limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Load OASST1, filter to English, build best (prompter, assistant) pairs.
    limit=0 means SKIP the dataset entirely (avoids the ~200 MB download for smoke runs)."""
    if limit == 0:
        return []
    from datasets import load_dataset
    ds = load_dataset("OpenAssistant/oasst1", split="train", streaming=False)
    rows = list(ds)
    out: List[Dict[str, Any]] = []
    for pair in _build_oasst_pairs(rows, lang="en"):
        rec = oasst_pair_to_chatml(pair)
        if rec is not None:
            out.append(rec)
        if limit and len(out) >= limit:
            break
    return out


# Extra code-instruction sources for the code-heavy recipe. Alpaca-schema (instruction/
# input/output), Python-focused, English -- distinct problems from CodeAlpaca-20k so the
# mix gets broader coding coverage without the general-chat drift OASST1 introduced.
CODE_HEAVY_SOURCES = [
    "iamtarun/python_code_instructions_18k_alpaca",
]


def build_dataset(codealpaca_limit: Optional[int] = None,
                  oasst_limit: Optional[int] = None,
                  seed: int = 0) -> List[Dict[str, Any]]:
    """Full recipe: CodeAlpaca + OASST1-en, filtered + shuffled deterministically.
    Set oasst_limit=0 to skip OASST entirely (useful for a fast CodeAlpaca-only smoke)."""
    ca = load_codealpaca(limit=codealpaca_limit)
    oa = load_oasst_en(limit=oasst_limit)
    combined = ca + oa
    random.Random(seed).shuffle(combined)
    return combined


def build_code_heavy_dataset(codealpaca_limit: Optional[int] = None,
                             extra_limit: Optional[int] = None,
                             sources: Optional[List[str]] = None,
                             seed: int = 0) -> List[Dict[str, Any]]:
    """Code-heavy recipe: CodeAlpaca-20k + one or more Python-instruction sets, NO OASST1.

    Motivation: the first full-FT (CodeAlpaca + OASST1-en) regressed HumanEval 66.5% -> 60.4%.
    OASST1's general-chat turns pulled the coder model off its code-completion optimum. This
    recipe drops OASST entirely and adds distinct code-instruction data instead, to preserve or
    beat the untuned base on HumanEval. Pair with a lower LR (2e-6) + 1 epoch in train.py."""
    srcs = sources if sources is not None else CODE_HEAVY_SOURCES
    combined = load_codealpaca(limit=codealpaca_limit)
    for src in srcs:
        combined += load_code_instructions(src, limit=extra_limit)
    random.Random(seed).shuffle(combined)
    return combined


def write_jsonl(records: List[Dict[str, Any]], out_path: str) -> None:
    """Write records to a JSONL file the trainer can stream."""
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output JSONL path")
    ap.add_argument("--recipe", choices=["default", "code_heavy"], default="default",
                    help="default = CodeAlpaca + OASST1-en; code_heavy = CodeAlpaca + Python-instruction sets, no OASST")
    ap.add_argument("--codealpaca-limit", type=int, default=None)
    ap.add_argument("--oasst-limit", type=int, default=None)
    ap.add_argument("--extra-limit", type=int, default=None,
                    help="per-source cap for code_heavy extra sources")
    a = ap.parse_args()
    if a.recipe == "code_heavy":
        recs = build_code_heavy_dataset(codealpaca_limit=a.codealpaca_limit, extra_limit=a.extra_limit)
    else:
        recs = build_dataset(codealpaca_limit=a.codealpaca_limit, oasst_limit=a.oasst_limit)
    write_jsonl(recs, a.out)
    print(f"[coder-data] recipe={a.recipe} wrote {len(recs)} ChatML records -> {a.out}")


if __name__ == "__main__":
    main()
