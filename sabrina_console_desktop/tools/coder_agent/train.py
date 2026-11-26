#!/usr/bin/env python3
"""
train.py
========
Full-parameter fine-tune of Qwen2.5-Coder-1.5B-Instruct on ChatML records from data.py.

Key mechanical choices (all load-bearing on the RTX 5060 Ti 16GB):
  - **Full-FT (NOT LoRA)**: no peft_config passed to SFTTrainer. All 1.5B parameters trainable.
  - **bf16 model weights** (not 4-bit): full-FT needs unquantized activations to flow real
    gradients into the base weights.
  - **8-bit AdamW via bitsandbytes** (`bnb.optim.PagedAdamW8bit`): drops optimizer states from
    ~12GB (fp32 AdamW) to ~6GB. This is the memory difference that makes full-FT of a 1.5B fit
    in 16GB.
  - **Gradient checkpointing** + `model.enable_input_require_grads()`: needed so the checkpoint
    barrier lets gradients back through the input embeddings.
  - **`assistant_only_loss=True`**: TRL 1.9's replacement for DataCollatorForCompletionOnlyLM.
  - `save_strategy="epoch"`, `save_safetensors=True`: full sharded safetensors at each epoch so
    an interruption still leaves a usable model.
"""
import argparse
import json
import os

import torch
from datasets import Dataset
from transformers import AutoModelForCausalLM, AutoTokenizer
from trl import SFTConfig, SFTTrainer

DEFAULT_BASE = "Qwen/Qwen2.5-Coder-1.5B-Instruct"


def load_chatml(path: str):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def _build_optimizer(model, lr: float):
    """8-bit paged AdamW. Falls back to plain AdamW if bitsandbytes fails to load (e.g. no CUDA)."""
    try:
        import bitsandbytes as bnb  # type: ignore
        # Paged optimizer offloads state pages between CPU/GPU under memory pressure.
        return bnb.optim.PagedAdamW8bit(model.parameters(), lr=lr, betas=(0.9, 0.95),
                                        weight_decay=0.01)
    except Exception as e:
        print(f"[train] bitsandbytes optimizer unavailable ({e}); falling back to torch AdamW",
              flush=True)
        return torch.optim.AdamW(model.parameters(), lr=lr, betas=(0.9, 0.95), weight_decay=0.01)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_BASE)
    ap.add_argument("--data", required=True, help="JSONL of ChatML records from data.py")
    ap.add_argument("--out", default="./coder_ft_out")
    ap.add_argument("--smoke", action="store_true",
                    help="20-step debug run on 200 records; must complete in ~5-10 min")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--max-steps", type=int, default=-1)
    ap.add_argument("--lr", type=float, default=5e-6)
    ap.add_argument("--bs", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=16)
    ap.add_argument("--max-length", type=int, default=2048)
    a = ap.parse_args()

    print(f"[train] base = {a.model}   data = {a.data}   full-FT (no LoRA)", flush=True)

    tok = AutoTokenizer.from_pretrained(a.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    print("[train] loading base model (bf16, all params trainable) ...", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        a.model, torch_dtype=torch.bfloat16, device_map={"": 0})
    model.config.use_cache = False
    # gradient checkpointing + no LoRA still needs input embeddings to require grad, otherwise
    # activation checkpointing detaches the graph before the first trainable param.
    model.enable_input_require_grads()

    # Sanity: full-FT invariant -- every param must be trainable.
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    n_total = sum(p.numel() for p in model.parameters())
    print(f"[train] trainable params: {n_train:,} / {n_total:,} "
          f"({100*n_train/n_total:.1f}%) -- should be ~100% for full-FT", flush=True)
    assert n_train == n_total, f"expected full-FT but only {n_train}/{n_total} params trainable"

    recs = load_chatml(a.data)
    if a.smoke:
        recs = recs[:200]
    ds = Dataset.from_list([{"messages": r["messages"]} for r in recs])
    print(f"[train] train records = {len(ds)}", flush=True)

    cfg = SFTConfig(
        output_dir=a.out,
        num_train_epochs=(1 if a.smoke else a.epochs),
        max_steps=(20 if a.smoke else a.max_steps),
        per_device_train_batch_size=a.bs,
        gradient_accumulation_steps=(1 if a.smoke else a.grad_accum),
        learning_rate=a.lr, bf16=True, logging_steps=1,
        save_strategy=("no" if a.smoke else "epoch"),
        # transformers saves safetensors by default; the explicit save_safetensors kwarg is not
        # accepted by TRL 1.9's SFTConfig, so we rely on the default.
        max_length=a.max_length,
        gradient_checkpointing=True,
        assistant_only_loss=True,
        packing=False,
        warmup_ratio=0.03, lr_scheduler_type="cosine",
        report_to=[],
        remove_unused_columns=False,
    )
    print(f"[train] SFTConfig ready  max_steps={cfg.max_steps}  epochs={cfg.num_train_epochs}",
          flush=True)

    # Wire the 8-bit AdamW manually. SFTTrainer accepts `optimizers=(opt, sched)` at construction;
    # passing (opt, None) lets Trainer build the LR scheduler around our optimizer.
    optimizer = _build_optimizer(model, lr=a.lr)
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=ds,
                         processing_class=tok, optimizers=(optimizer, None))

    print("[train] starting training ...", flush=True)
    trainer.train()

    if a.smoke:
        # explicit save on smoke since save_strategy="no"
        smoke_dir = os.path.join(a.out, "smoke")
        trainer.save_model(smoke_dir)
        print(f"[train] smoke checkpoint -> {smoke_dir}", flush=True)
    else:
        final = os.path.join(a.out, "final")
        trainer.save_model(final)
        print(f"[train] final checkpoint -> {final}", flush=True)
    print("[train] done.", flush=True)


if __name__ == "__main__":
    main()
