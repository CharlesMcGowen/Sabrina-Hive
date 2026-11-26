"""Paired, frozen smoke evaluation of the published Qwen3.5 repetition edit.

This is a small reproduction of loop behavior, not the paper-scale benchmark.
It loads the exact base and edited checkpoints sequentially, uses matched seeds,
and records full generations plus deterministic repetition metrics.  It never
edits, registers, serves, or promotes either checkpoint.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import statistics
import time

from dm_weight_lab import digest, dump


DATASET_REPO = "HuggingFaceH4/MATH-500"
DATASET_REVISION = "6e4ed1a2a79af7d8630a6b768ec859cb5af4d3be"
BASE_REVISION = "2fc06364715b967f1860aea9cf38778875588b17"
EDITED_REVISION = "d1671a178705bcab7644806d23c535517f35723b"
MODEL_FILE = "model.safetensors-00001-of-00001.safetensors"
EXPECTED_EDITED_TENSORS = {
    "model.language_model.layers.15.mlp.down_proj.weight":
        "82e2d733ea64feaeb4af93a1e065641d3df98b494a8b7576ef2f52e90fe2ee53",
    "model.language_model.layers.19.mlp.down_proj.weight":
        "8ef3c61bc36af113ad58e89dbb0a4a14dcba5a45cb750c15ab515c82ddd12eb2",
}
SAMPLING = {
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "presence_penalty": 1.5,
    "seed": 0,
}
PROMPT_SUFFIX = "\nPlease reason step by step, and put your final answer within \\boxed{}."
TOKEN_CAP_MODEL = "model"


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                        allow_nan=False).encode("utf-8")).hexdigest()


def parse_token_cap(value: str) -> int | str:
    normalized = value.strip().lower()
    if normalized in {"model", "max"}:
        return TOKEN_CAP_MODEL
    try:
        parsed = int(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("token cap must be an integer or 'model'") from error
    if parsed < 256:
        raise argparse.ArgumentTypeError("numeric token cap must be at least 256")
    return parsed


def declared_context_limit(model_path: Path) -> int:
    config = json.loads((Path(model_path) / "config.json").read_text(encoding="utf-8"))
    text_config = config.get("text_config", config)
    value = text_config.get("max_position_embeddings")
    if type(value) is not int or not 256 <= value <= 1_048_576:
        raise ValueError("Checkpoint has no bounded max_position_embeddings declaration")
    return value


def resolve_generation_cap(token_cap: int | str, prompt_tokens: int,
                           model_context_limit: int) -> int:
    if type(prompt_tokens) is not int or not 1 <= prompt_tokens < model_context_limit:
        raise ValueError("Prompt does not fit the declared model context")
    available = model_context_limit - prompt_tokens
    if token_cap == TOKEN_CAP_MODEL:
        return available
    if type(token_cap) is not int or token_cap < 256:
        raise ValueError("Token cap must be an integer >=256 or 'model'")
    if token_cap > available:
        raise ValueError(
            f"Requested {token_cap} new tokens but only {available} fit after the prompt"
        )
    return token_cap


def raw_tensor_hash(tensor) -> str:
    import torch
    return hashlib.sha256(tensor.contiguous().view(torch.uint8).numpy().tobytes()).hexdigest()


def verify_pair(base: Path, edited: Path) -> dict:
    """Verify the released sparse diff before any inference."""
    import torch
    from safetensors import safe_open
    base_file = base / MODEL_FILE
    edited_file = edited / MODEL_FILE
    if not base_file.is_file() or not edited_file.is_file():
        raise FileNotFoundError("Pinned model safetensors files are missing")
    changed = []
    with safe_open(base_file, framework="pt", device="cpu") as left, \
            safe_open(edited_file, framework="pt", device="cpu") as right:
        if left.keys() != right.keys():
            raise RuntimeError("Base and edited tensor catalogs differ")
        for name in left.keys():
            a = left.get_tensor(name)
            b = right.get_tensor(name)
            if a.shape != b.shape or a.dtype != b.dtype:
                raise RuntimeError(f"Tensor metadata differs: {name}")
            if not torch.equal(a, b):
                changed.append({
                    "tensor": name,
                    "shape": list(a.shape),
                    "base_raw_sha256": raw_tensor_hash(a),
                    "edited_raw_sha256": raw_tensor_hash(b),
                    "changed_values": int((a != b).sum()),
                })
    if {row["tensor"] for row in changed} != set(EXPECTED_EDITED_TENSORS):
        raise RuntimeError("Checkpoint diff is not the published two-tensor edit")
    for row in changed:
        if row["edited_raw_sha256"] != EXPECTED_EDITED_TENSORS[row["tensor"]]:
            raise RuntimeError(f"Published raw tensor hash mismatch: {row['tensor']}")
    for name in ["config.json", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja"]:
        if (base / name).read_bytes() != (edited / name).read_bytes():
            raise RuntimeError(f"Model metadata differs: {name}")
    return {
        "base_file_sha256": digest(base_file),
        "edited_file_sha256": digest(edited_file),
        "changed_tensors": changed,
        "changed_values": sum(row["changed_values"] for row in changed),
    }


def select_cases(rows: list[dict], count: int) -> list[dict]:
    if not 1 <= count <= 32 or len(rows) < count:
        raise ValueError("Invalid case count")
    ranked = []
    for index, row in enumerate(rows):
        problem = row.get("problem")
        if not isinstance(problem, str) or not problem.strip():
            raise ValueError("Dataset row is missing a problem")
        key = hashlib.sha256(
            f"sabrina-wsp-smoke-v1\0{index}\0{problem}".encode("utf-8")
        ).hexdigest()
        ranked.append((key, index, row))
    selected = []
    for _, index, row in sorted(ranked)[:count]:
        selected.append({
            "case_id": f"math500-{index:03d}",
            "dataset_index": index,
            "problem": row["problem"],
            "reference_answer": row.get("answer"),
            "subject": row.get("subject"),
            "level": row.get("level"),
        })
    return selected


def freeze_cases(root: Path, count: int) -> list[dict]:
    from huggingface_hub import hf_hub_download
    path = hf_hub_download(DATASET_REPO, "test.jsonl", revision=DATASET_REVISION,
                           repo_type="dataset")
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = select_cases(rows, count)
    dump(root / "frozen-cases.json", {
        "dataset": DATASET_REPO,
        "revision": DATASET_REVISION,
        "selection": f"{count} smallest sha256(sabrina-wsp-smoke-v1\\0index\\0problem) values",
        "cases": cases,
    })
    return cases


def duplicate_ngram_fraction(tokens: list[int], n: int = 8, tail_tokens: int = 512) -> float:
    tail = tokens[-tail_tokens:]
    if len(tail) < n:
        return 0.0
    grams = [tuple(tail[i:i + n]) for i in range(len(tail) - n + 1)]
    return 1.0 - len(set(grams)) / len(grams)


def best_period_match(tokens: list[int], max_period: int = 64, tail_tokens: int = 512) -> dict:
    tail = tokens[-tail_tokens:]
    best = {"period": None, "fraction": 0.0}
    for period in range(1, min(max_period, len(tail) // 2) + 1):
        comparisons = len(tail) - period
        fraction = sum(tail[i] == tail[i - period] for i in range(period, len(tail))) / comparisons
        if fraction > best["fraction"]:
            best = {"period": period, "fraction": fraction}
    return best


def repetition_metrics(tokens: list[int], text: str, max_new_tokens: int, eos_reached: bool) -> dict:
    duplicate8 = duplicate_ngram_fraction(tokens)
    periodic = best_period_match(tokens)
    hit_limit = len(tokens) >= max_new_tokens and not eos_reached
    whitespace_tail = text[-1000:]
    whitespace_fraction = (sum(ch.isspace() for ch in whitespace_tail) / len(whitespace_tail)
                           if whitespace_tail else 0.0)
    loop_detected = bool(hit_limit and (duplicate8 >= 0.5 or periodic["fraction"] >= 0.8))
    return {
        "generated_tokens": len(tokens),
        "eos_reached": eos_reached,
        "hit_token_limit": hit_limit,
        "tail_duplicate_8gram_fraction": duplicate8,
        "tail_best_period": periodic,
        "tail_whitespace_fraction": whitespace_fraction,
        "loop_detected": loop_detected,
        "loop_rule": "hit_token_limit and (duplicate8>=0.5 or best_period_fraction>=0.8)",
    }


class OutputPresencePenalty:
    """Transformers-compatible additive presence penalty over generated tokens only."""
    def __init__(self, prompt_length: int, penalty: float):
        self.prompt_length = prompt_length
        self.penalty = penalty
        self.processed_length = prompt_length
        self.seen = None

    def __call__(self, input_ids, scores):
        import torch
        if self.seen is None:
            self.seen = torch.zeros_like(scores, dtype=torch.bool)
        new_tokens = input_ids[:, self.processed_length:]
        for batch in range(new_tokens.shape[0]):
            if new_tokens.shape[1]:
                self.seen[batch, new_tokens[batch]] = True
        self.processed_length = input_ids.shape[1]
        scores[self.seen] -= self.penalty
        return scores


class GenerationHeartbeat:
    """Low-frequency progress artifact for long, non-resumable-within-case generations."""
    def __init__(self, path: Path, prompt_length: int, generation_cap: int,
                 interval: int = 1024):
        self.path = Path(path)
        self.prompt_length = prompt_length
        self.generation_cap = generation_cap
        self.interval = interval
        self.next_write = interval
        self.started = time.monotonic()

    def _write(self, status: str, generated_tokens: int, error: str | None = None):
        payload = {
            "status": status,
            "generated_tokens": generated_tokens,
            "generation_cap_tokens": self.generation_cap,
            "fraction": generated_tokens / self.generation_cap,
            "elapsed_seconds": time.monotonic() - self.started,
            "within_case_resume_supported": False,
        }
        if error:
            payload["error"] = error[:500]
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        dump(temporary, payload)
        temporary.replace(self.path)

    def start(self):
        self._write("running", 0)

    def finish(self, generated_tokens: int):
        self._write("completed", generated_tokens)

    def fail(self, generated_tokens: int, error: Exception):
        self._write("failed", generated_tokens, f"{type(error).__name__}: {error}")

    def __call__(self, input_ids, scores):
        generated_tokens = input_ids.shape[1] - self.prompt_length
        if generated_tokens >= self.next_write:
            self._write("running", generated_tokens)
            self.next_write = ((generated_tokens // self.interval) + 1) * self.interval
        return scores


def load_model(path: Path):
    import torch
    from transformers import AutoModelForMultimodalLM, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
    model = AutoModelForMultimodalLM.from_pretrained(
        path, local_files_only=True, trust_remote_code=False,
        dtype=torch.bfloat16, device_map="cuda",
    )
    model.eval()
    return tokenizer, model


def generate_case(tokenizer, model, case: dict, token_cap: int | str, seed: int,
                  model_context_limit: int, progress_path: Path | None = None) -> dict:
    import torch
    from transformers import LogitsProcessorList
    prompt = case["problem"] + PROMPT_SUFFIX
    rendered = tokenizer.apply_chat_template(
        [{"role": "user", "content": prompt}], tokenize=False,
        add_generation_prompt=True, enable_thinking=True,
    )
    inputs = tokenizer(rendered, return_tensors="pt")
    prompt_length = inputs.input_ids.shape[-1]
    if prompt_length > 4096:
        raise ValueError("Frozen prompt exceeds 4096 tokens")
    generation_cap = resolve_generation_cap(token_cap, prompt_length, model_context_limit)
    device_inputs = {key: value.to("cuda") for key, value in inputs.items()}
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    started = time.monotonic()
    processors = [OutputPresencePenalty(prompt_length, SAMPLING["presence_penalty"])]
    heartbeat = GenerationHeartbeat(progress_path, prompt_length, generation_cap) if progress_path else None
    if heartbeat:
        heartbeat.start()
        processors.append(heartbeat)
    generated_count = 0
    try:
        with torch.inference_mode():
            generated = model.generate(
                **device_inputs,
                do_sample=True,
                temperature=SAMPLING["temperature"],
                top_p=SAMPLING["top_p"],
                top_k=SAMPLING["top_k"],
                max_new_tokens=generation_cap,
                logits_processor=LogitsProcessorList(processors),
                pad_token_id=tokenizer.eos_token_id,
                eos_token_id=tokenizer.eos_token_id,
                use_cache=True,
            )
        generated_count = generated.shape[1] - prompt_length
        if heartbeat:
            heartbeat.finish(generated_count)
    except Exception as error:
        if heartbeat:
            heartbeat.fail(generated_count, error)
        raise
    new_tokens = generated[0, prompt_length:].tolist()
    eos_reached = bool(new_tokens and new_tokens[-1] == tokenizer.eos_token_id)
    text = tokenizer.decode(new_tokens, skip_special_tokens=False)
    return {
        "case_id": case["case_id"],
        "dataset_index": case["dataset_index"],
        "seed": seed,
        "prompt_tokens": prompt_length,
        "elapsed_seconds": time.monotonic() - started,
        "requested_token_cap": token_cap,
        "generation_cap_tokens": generation_cap,
        "model_context_limit": model_context_limit,
        "metrics": repetition_metrics(new_tokens, text, generation_cap, eos_reached),
        "output_text": text,
    }


def aggregate(rows: list[dict]) -> dict:
    metrics = [row["metrics"] for row in rows]
    return {
        "cases": len(rows),
        "loops": sum(item["loop_detected"] for item in metrics),
        "loop_rate": sum(item["loop_detected"] for item in metrics) / len(metrics),
        "token_limit_hits": sum(item["hit_token_limit"] for item in metrics),
        "mean_tail_duplicate_8gram_fraction": statistics.fmean(
            item["tail_duplicate_8gram_fraction"] for item in metrics
        ),
        "median_tail_duplicate_8gram_fraction": statistics.median(
            item["tail_duplicate_8gram_fraction"] for item in metrics
        ),
        "median_generated_tokens": statistics.median(item["generated_tokens"] for item in metrics),
        "median_generation_cap_tokens": statistics.median(
            row.get("generation_cap_tokens", item["generated_tokens"])
            for row, item in zip(rows, metrics)
        ),
        "total_elapsed_seconds": sum(row["elapsed_seconds"] for row in rows),
    }


def run(root: Path, base: Path, edited: Path, case_count: int,
        max_new_tokens: int | str) -> dict:
    if max_new_tokens != TOKEN_CAP_MODEL and (type(max_new_tokens) is not int or max_new_tokens < 256):
        raise ValueError("max_new_tokens must be an integer >=256 or 'model'")
    base_context = declared_context_limit(base)
    edited_context = declared_context_limit(edited)
    if base_context != edited_context:
        raise RuntimeError("Base and edited checkpoints declare different context limits")
    if type(max_new_tokens) is int and max_new_tokens >= base_context:
        raise ValueError("Numeric token cap cannot equal or exceed the model context limit")
    root = Path(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (root.parent / ".dm_weight_lab.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run_locked(root, Path(base), Path(edited), case_count, max_new_tokens,
                           base_context)


def _run_locked(root: Path, base: Path, edited: Path, case_count: int,
                max_new_tokens: int | str, model_context_limit: int) -> dict:
    root.mkdir(exist_ok=True)
    verification_path = root / "checkpoint-verification.json"
    if verification_path.exists():
        verification = json.loads(verification_path.read_text())
    else:
        verification = verify_pair(base, edited)
        dump(verification_path, verification)
    cases_path = root / "frozen-cases.json"
    cases = (json.loads(cases_path.read_text())["cases"] if cases_path.exists()
             else freeze_cases(root, case_count))
    if len(cases) != case_count:
        raise RuntimeError("Frozen case count does not match request")
    protocol = {
        "format_version": 1,
        "label": "small paired loop-rate smoke; not the paper-scale benchmark",
        "dataset": {"repo": DATASET_REPO, "revision": DATASET_REVISION},
        "frozen_cases_sha256": digest(cases_path),
        "base": {"path": str(base), "revision": BASE_REVISION,
                 "file_sha256": verification["base_file_sha256"]},
        "edited": {"path": str(edited), "revision": EDITED_REVISION,
                   "file_sha256": verification["edited_file_sha256"]},
        "sampling": SAMPLING,
        "max_new_tokens": max_new_tokens,
        "samples_per_case": 1,
        "matched_seeds": True,
        "full_outputs_saved": True,
        "weights_changed_by_runner": False,
        "models_registered_or_served": False,
    }
    if max_new_tokens == TOKEN_CAP_MODEL:
        protocol["model_context_limit"] = model_context_limit
        protocol["token_cap_policy"] = "per-case max_position_embeddings minus rendered prompt tokens"
    protocol_path = root / "protocol.json"
    if protocol_path.exists() and json.loads(protocol_path.read_text()) != protocol:
        raise RuntimeError("Existing protocol differs; use a new run root")
    dump(protocol_path, protocol)

    all_rows = {}
    for label, path in [("base", base), ("edited", edited)]:
        folder = root / label
        folder.mkdir(exist_ok=True)
        rows = []
        pending = []
        for index, case in enumerate(cases):
            output = folder / f"{case['case_id']}.json"
            if output.exists():
                rows.append(json.loads(output.read_text()))
            else:
                pending.append((index, case, output))
        if pending:
            tokenizer, model = load_model(path)
            try:
                for index, case, output in pending:
                    progress = folder / f"{case['case_id']}.progress.json"
                    row = generate_case(tokenizer, model, case, max_new_tokens,
                                        SAMPLING["seed"] + index, model_context_limit,
                                        progress)
                    row["model"] = label
                    dump(output, row)
                    rows.append(row)
                    print(json.dumps({"phase": "generated", "model": label,
                                      "case_id": case["case_id"], **row["metrics"]}), flush=True)
            finally:
                del model, tokenizer
                gc.collect()
                import torch
                torch.cuda.empty_cache()
        rows.sort(key=lambda row: row["case_id"])
        all_rows[label] = rows
        dump(folder / "summary.json", aggregate(rows))

    base_by_id = {row["case_id"]: row for row in all_rows["base"]}
    edited_by_id = {row["case_id"]: row for row in all_rows["edited"]}
    paired = []
    for case in cases:
        case_id = case["case_id"]
        left = base_by_id[case_id]["metrics"]
        right = edited_by_id[case_id]["metrics"]
        paired.append({
            "case_id": case_id,
            "base_loop": left["loop_detected"],
            "edited_loop": right["loop_detected"],
            "base_duplicate8": left["tail_duplicate_8gram_fraction"],
            "edited_duplicate8": right["tail_duplicate_8gram_fraction"],
            "duplicate8_delta_edited_minus_base": (
                right["tail_duplicate_8gram_fraction"] - left["tail_duplicate_8gram_fraction"]
            ),
            "base_tokens": left["generated_tokens"],
            "edited_tokens": right["generated_tokens"],
        })
    outcome = {
        "status": "completed",
        "base": aggregate(all_rows["base"]),
        "edited": aggregate(all_rows["edited"]),
        "paired": paired,
        "base_loops_rescued": sum(row["base_loop"] and not row["edited_loop"] for row in paired),
        "new_edited_loops": sum(not row["base_loop"] and row["edited_loop"] for row in paired),
        "pairs_with_lower_edited_duplicate8": sum(
            row["duplicate8_delta_edited_minus_base"] < 0 for row in paired
        ),
        "mean_duplicate8_delta_edited_minus_base": statistics.fmean(
            row["duplicate8_delta_edited_minus_base"] for row in paired
        ),
        "median_duplicate8_delta_edited_minus_base": statistics.median(
            row["duplicate8_delta_edited_minus_base"] for row in paired
        ),
        "weight_edits_applied_by_runner": 0,
        "interpretation": "Frozen small behavioral smoke only; no accuracy or broad-generalization claim.",
    }
    dump(root / "outcome.json", outcome)
    return outcome


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--edited", required=True, type=Path)
    parser.add_argument("--cases", type=int, default=4, choices=range(1, 33))
    parser.add_argument(
        "--max-new-tokens", type=parse_token_cap, default=4096,
        help="integer cap, or 'model'/'max' for max_position_embeddings minus prompt tokens",
    )
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.base, args.edited, args.cases, args.max_new_tokens), indent=2))


if __name__ == "__main__":
    main()
