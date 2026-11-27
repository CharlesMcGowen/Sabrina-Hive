#!/usr/bin/env python3
"""Canonical isolated weight-space core for registered HF safetensors models.

The Sabrina loop can call this module over SSH using the rpc subcommand.
Model paths and run artifacts live outside the source tree:

  SABRINA_WEIGHT_SPACE_MODEL_REGISTRY (default: ~/.config/sabrina/weight_space_models.json)
  SABRINA_WEIGHT_SPACE_RUNS_ROOT      (default: ~/.local/share/sabrina/weight_space/runs)

This tool never downloads a model, executes model-authored code, mutates the
registered source checkpoint, or activates a candidate in a serving system.

The selected-column direction projection follows the MIT-licensed helper from
https://github.com/xero00000/weight-space-programming. See
licenses/weight-space-programming-MIT.txt for its copyright and license notice.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any


MAX_PROMPTS = 32
MAX_PROMPT_CHARS = 20_000
MAX_NEW_TOKENS = 512
MAX_EDITS = 2_000
MAX_RPC_BYTES = 2_000_000
MAX_INSPECT_TENSORS = 200
try:
    from sabrina_config import LAB_HOST as DEFAULT_REMOTE_HOST
except ImportError:
    DEFAULT_REMOTE_HOST = os.environ.get("SABRINA_LAB_HOST", "localhost")
DEFAULT_REMOTE_ROOT = os.environ.get("SABRINA_REMOTE_ROOT", "/opt/sabrina/workspace")
DEFAULT_REMOTE_USER = os.environ.get("SABRINA_LAB_USER", "sabrina")
SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))


def _registry_path() -> Path:
    return Path(os.environ.get(
        "SABRINA_WEIGHT_SPACE_MODEL_REGISTRY",
        str(_config_home() / "sabrina" / "weight_space_models.json"),
    )).expanduser()


def _runs_root() -> Path:
    return Path(os.environ.get(
        "SABRINA_WEIGHT_SPACE_RUNS_ROOT",
        str(_data_home() / "sabrina" / "weight_space" / "runs"),
    )).expanduser().resolve()


def _load_runtime():
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError(
            "Weight-space experiments require torch and transformers in the lab Python environment."
        ) from exc
    return torch, AutoModelForCausalLM, AutoTokenizer


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read valid JSON from {path}: {exc}") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _weight_files(model_dir: Path) -> list[Path]:
    files = sorted(
        path for path in model_dir.glob("*.safetensors")
        if path.name == "model.safetensors"
        or re.fullmatch(r"model-\d+-of-\d+\.safetensors", path.name)
    )
    if not files:
        raise ValueError(
            f"No Hugging Face safetensors weights found in {model_dir}; "
            "Ollama/GGUF and adapter-only directories are unsupported."
        )
    return files


def _checkpoint_hashes(model_dir: Path) -> dict[str, str]:
    return {path.name: _sha256_file(path) for path in _weight_files(model_dir)}


def _model_file_hashes(model_dir: Path) -> dict[str, str]:
    files = sorted(path for path in model_dir.iterdir() if path.is_file())
    return {path.name: _sha256_file(path) for path in files}


def _load_registry() -> dict[str, Path]:
    path = _registry_path()
    if not path.is_file():
        raise RuntimeError(
            f"Weight-space model registry is missing: {path}. Create it outside the code tree "
            "with a JSON object like {'models': {'alias': '/absolute/HF/model/path'}}."
        )
    raw = _json(path)
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, dict):
        raise ValueError(f"Registry {path} must contain an object field named 'models'.")
    resolved: dict[str, Path] = {}
    for alias, value in models.items():
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", alias):
            raise ValueError(f"Invalid model alias in registry: {alias!r}.")
        model_path = value.get("path") if isinstance(value, dict) else value
        if not isinstance(model_path, str) or not Path(model_path).is_absolute():
            raise ValueError(f"Model alias {alias!r} must map to an absolute model directory.")
        resolved[alias] = Path(model_path).resolve()
    return resolved


def _resolve_model(alias: Any) -> tuple[str, Path]:
    if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", alias):
        raise ValueError("model_alias must be a configured alias, not a filesystem path.")
    registry = _load_registry()
    if alias not in registry:
        raise ValueError(f"Unknown model alias {alias!r}; registered aliases: {', '.join(sorted(registry)) or '(none)'}")
    model_dir = registry[alias]
    if not model_dir.is_dir():
        raise ValueError(f"Registered model directory is unavailable: {model_dir}")
    if model_dir == SOURCE_ROOT or SOURCE_ROOT in model_dir.parents:
        raise ValueError("Registered model directories must be outside the Sabrina source tree.")
    if not (model_dir / "config.json").is_file():
        raise ValueError(f"Registered path is not a Transformers model directory (config.json missing): {model_dir}")
    _weight_files(model_dir)
    return alias, model_dir


def _safe_run_dir(label: str, runs_root: Path) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", label):
        raise ValueError("Run label must be 1–48 letters, digits, dots, underscores, or hyphens.")
    runs_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = runs_root / f"{stamp}_{label}"
    run_dir.mkdir()
    return run_dir


def _read_prompts(data: Any) -> list[dict[str, str]]:
    if not isinstance(data, list) or not data or len(data) > MAX_PROMPTS:
        raise ValueError(f"prompts must be a non-empty JSON list with at most {MAX_PROMPTS} entries.")
    prompts = []
    for i, item in enumerate(data):
        if isinstance(item, str):
            prompt, name = item, f"prompt_{i + 1}"
        elif isinstance(item, dict) and isinstance(item.get("prompt"), str):
            prompt, name = item["prompt"], str(item.get("name", f"prompt_{i + 1}"))
        else:
            raise ValueError(f"Prompt entry {i + 1} must be a string or have a string 'prompt' field.")
        if not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"Prompt entry {i + 1} must contain 1–{MAX_PROMPT_CHARS} characters.")
        prompts.append({"name": name[:120], "prompt": prompt})
    return prompts


def _validate_manifest(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or data.get("format_version") != 1:
        raise ValueError("Edit manifest must be an object with format_version: 1.")
    edits = data.get("edits")
    if not isinstance(edits, list) or not edits or len(edits) > MAX_EDITS:
        raise ValueError(f"Manifest 'edits' must contain 1–{MAX_EDITS} edits.")
    allowed_ops = {"add", "multiply", "set", "project", "low_rank_add"}
    clean = []
    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"Edit {i + 1} must be an object.")
        name, op = edit.get("tensor"), edit.get("op")
        if not isinstance(name, str) or not name or len(name) > 512:
            raise ValueError(f"Edit {i + 1} needs a tensor name.")
        if op not in allowed_ops:
            raise ValueError(f"Edit {i + 1} op must be one of: {', '.join(sorted(allowed_ops))}.")
        if op == 'low_rank_add':
            left,right=edit.get('left'),edit.get('right')
            if not isinstance(left,list) or not left or not isinstance(right,list) or not right:
                raise ValueError('low_rank_add requires two nonempty factor matrices')
            rank=len(right)
            if rank>8 or not isinstance(right[0],list) or not right[0]:raise ValueError('Invalid low-rank factor rank')
            width=len(right[0])
            if len(left)*rank>65536 or rank*width>65536:raise ValueError('Low-rank factor budget exceeded')
            for matrix,size in [(left,rank),(right,width)]:
                for row in matrix:
                    if not isinstance(row,list) or len(row)!=size or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) for x in row):
                        raise ValueError('Invalid low-rank factor values or shape')
            clean.append({'tensor':name,'op':op,'left':[[float(x) for x in row] for row in left],
                          'right':[[float(x) for x in row] for row in right]})
            continue
        if op == "project":
            direction, columns = edit.get("direction"), edit.get("columns")
            alpha = edit.get("alpha", 1.0)
            if not isinstance(direction, list) or not direction or len(direction) > 65_536:
                raise ValueError(f"Edit {i + 1} project direction must be a non-empty numeric vector.")
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in direction):
                raise ValueError(f"Edit {i + 1} direction values must be finite numbers.")
            if not isinstance(columns, list) or not columns or any(type(x) is not int or x < 0 for x in columns):
                raise ValueError(f"Edit {i + 1} project columns must be a non-empty list of non-negative integers.")
            if len(set(columns)) != len(columns):
                raise ValueError(f"Edit {i + 1} project columns must be unique.")
            if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(alpha) or not 0 < alpha <= 1:
                raise ValueError(f"Edit {i + 1} alpha must be finite and in (0, 1].")
            clean.append({"tensor": name, "op": op, "direction": [float(x) for x in direction],
                          "columns": columns, "alpha": float(alpha)})
            continue
        value = edit.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Edit {i + 1} value must be a finite number.")
        selector = edit.get("index")
        if selector is not None and (
            not isinstance(selector, list) or not selector
            or any(type(idx) is not int or idx < 0 for idx in selector)
        ):
            raise ValueError(f"Edit {i + 1} index must be a list of non-negative integer indices.")
        clean.append({"tensor": name, "op": op, "value": float(value), "index": selector})
    return clean


def _select(tensor, selector, name: str):
    if selector is None:
        return tensor
    if len(selector) > tensor.ndim:
        raise ValueError(f"Index for {name} has {len(selector)} dimensions, tensor has {tensor.ndim}.")
    for axis, idx in enumerate(selector):
        if idx >= tensor.shape[axis]:
            raise ValueError(f"Index {idx} is outside dimension {axis} of {name} (size {tensor.shape[axis]}).")
    return tensor[tuple(selector) + (slice(None),) * (tensor.ndim - len(selector))]


def _project_tensor(weight, direction: list[float], columns: list[int], alpha: float):
    """Remove alpha of a direction from selected input columns of an [out, in] matrix."""
    import torch

    if weight.ndim != 2 or not weight.is_floating_point():
        raise ValueError("project requires a 2D floating-point parameter.")
    if not torch.isfinite(weight).all().item():
        raise ValueError("project source tensor contains NaN or infinity.")
    d = torch.tensor(direction, dtype=torch.float64, device="cpu")
    if d.ndim != 1 or d.numel() != weight.shape[0]:
        raise ValueError("project direction length must equal the tensor output dimension (axis 0).")
    if not torch.isfinite(d).all().item():
        raise ValueError("project direction contains NaN or infinity.")
    largest = float(d.abs().max().item())
    if largest == 0:
        raise ValueError("project direction must be nonzero.")
    unit = d / largest
    unit /= torch.linalg.vector_norm(unit)
    if not columns or len(set(columns)) != len(columns) or min(columns) < 0 or max(columns) >= weight.shape[1]:
        raise ValueError("project columns must be unique valid indices in the tensor input dimension.")
    with torch.no_grad():
        selected = weight[:, columns].to(torch.float64)
        device_unit = unit.to(weight.device)
        projected = selected - alpha * device_unit[:, None] * (device_unit @ selected)[None, :]
        result = weight.detach().clone()
        result[:, columns] = projected.to(device=weight.device, dtype=weight.dtype)
    if not torch.isfinite(result).all().item():
        raise ValueError("project edit overflowed the stored tensor dtype.")
    return result


def _apply_edits(model, edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    import torch

    params = dict(model.named_parameters())
    applied = []
    for i, edit in enumerate(edits):
        name = edit["tensor"]
        if name not in params:
            raise ValueError(f"Manifest edit {i + 1} names unknown parameter: {name}")
        param = params[name]
        if edit["op"] == "project":
            if param.ndim != 2 or max(edit["columns"]) >= param.shape[1]:
                raise ValueError(f"Edit {i + 1} project columns are outside the 2D tensor shape.")
            target = param[:, edit["columns"]]
        elif edit['op']=='low_rank_add':
            target=param
        else:
            target = _select(param, edit["index"], name)
        before_tensor = target.detach().clone()
        before_norm = float(target.detach().float().norm().item())
        with torch.no_grad():
            if edit['op']=='low_rank_add':
                if param.ndim!=2 or not param.is_floating_point() or param.numel()>67108864:
                    raise ValueError('low_rank_add requires a bounded floating-point matrix')
                left=torch.tensor(edit['left'],dtype=torch.float64,device=param.device)
                right=torch.tensor(edit['right'],dtype=torch.float64,device=param.device)
                if left.shape[0]!=param.shape[0] or right.shape[1]!=param.shape[1]:raise ValueError('Low-rank factors do not match parameter shape')
                param.copy_((param.double()+left@right).to(param.dtype))
            elif edit["op"] == "project":
                param.copy_(_project_tensor(param, edit["direction"], edit["columns"], edit["alpha"]))
                target = param[:, edit["columns"]]
            elif edit["op"] == "add":
                target.add_(edit["value"])
            elif edit["op"] == "multiply":
                target.mul_(edit["value"])
            else:
                target.fill_(edit["value"])
        if not torch.isfinite(param).all().item():
            with torch.no_grad():
                if edit["op"] == "project":
                    param[:, edit["columns"]] = before_tensor
                else:
                    target.copy_(before_tensor)
            raise ValueError(f"Edit {i + 1} made {name} contain NaN or infinity; experiment stopped.")
        delta = target.detach().float() - before_tensor.float()
        recorded_edit = dict(edit)
        if edit['op']=='low_rank_add':
            for key in ['left','right']:
                values=recorded_edit.pop(key)
                recorded_edit[key+'_sha256']=hashlib.sha256(json.dumps(values,separators=(',',':')).encode()).hexdigest()
            recorded_edit['rank']=len(edit['right'])
        if edit["op"] == "project":
            recorded_edit.pop("direction", None)
            recorded_edit["direction_sha256"] = hashlib.sha256(
                json.dumps(edit["direction"], separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        applied.append({
            **recorded_edit, "shape": list(param.shape), "selected_shape": list(target.shape),
            "changed_elements": int((target.detach() != before_tensor).sum().item()),
            "delta_l2": float(delta.norm().item()),
            "norm_before": before_norm,
            "norm_after": float(target.detach().float().norm().item()),
        })
        del before_tensor
    return applied


def _load_model(model_dir: Path, device_preference: str = "auto"):
    if device_preference not in {"auto", "cpu", "gpu"}:
        raise ValueError("device must be auto, cpu, or gpu")
    torch, model_cls, tokenizer_cls = _load_runtime()
    if not model_dir.is_dir():
        raise ValueError(f"Model directory does not exist: {model_dir}")
    _weight_files(model_dir)
    if not (model_dir / "config.json").is_file():
        raise ValueError("Expected config.json in a local Transformers model folder.")
    if not (model_dir / "tokenizer.json").is_file() and not (model_dir / "tokenizer_config.json").is_file():
        raise ValueError("Expected local tokenizer.json or tokenizer_config.json; downloads are disabled.")

    if device_preference == "gpu" and not torch.cuda.is_available():
        raise RuntimeError("GPU was requested but CUDA is unavailable")
    device = "cuda" if device_preference != "cpu" and torch.cuda.is_available() else "cpu"
    tokenizer = tokenizer_cls.from_pretrained(str(model_dir), local_files_only=True, trust_remote_code=False)
    model = model_cls.from_pretrained(
        str(model_dir), local_files_only=True, trust_remote_code=False,
        use_safetensors=True, torch_dtype="auto", low_cpu_mem_usage=True,
    )
    model.to(device)
    model.eval()
    return torch, model, tokenizer, device


def _check_capacity(source: Path, runs_root: Path) -> dict[str, Any]:
    total_bytes = sum(path.stat().st_size for path in _weight_files(source))
    probe = runs_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    required_disk = int(total_bytes * 1.15) + 512 * 1024 * 1024
    if usage.free < required_disk:
        raise RuntimeError(
            f"Insufficient disk space for an isolated candidate: need about "
            f"{required_disk / 1024**3:.1f} GiB, have {usage.free / 1024**3:.1f} GiB."
        )
    torch, _, _ = _load_runtime()
    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        required_vram = int(total_bytes * 1.25)
        if free < required_vram:
            raise RuntimeError(
                f"Insufficient free GPU memory for this model: need about {required_vram / 1024**3:.1f} GiB, "
                f"have {free / 1024**3:.1f} GiB. No CPU fallback is attempted when CUDA is available."
            )
        return {"device": "cuda", "gpu_free_bytes": int(free), "gpu_total_bytes": int(total),
                "estimated_model_bytes": total_bytes}
    return {"device": "cpu", "estimated_model_bytes": total_bytes}


def _generate(model_dir: Path, prompts: list[dict[str, str]], max_new_tokens: int) -> list[dict[str, str]]:
    torch, model, tokenizer, device = _load_model(model_dir)
    results = []
    try:
        for item in prompts:
            batch = tokenizer(item["prompt"], return_tensors="pt", truncation=True, max_length=4096)
            batch = {key: val.to(device) for key, val in batch.items()}
            with torch.inference_mode():
                output = model.generate(
                    **batch, max_new_tokens=max_new_tokens, do_sample=False,
                    pad_token_id=tokenizer.eos_token_id,
                )
            completion = output[0, batch["input_ids"].shape[1]:]
            results.append({
                "name": item["name"], "prompt": item["prompt"],
                "response": tokenizer.decode(completion, skip_special_tokens=True),
            })
    finally:
        del model, tokenizer
        if device == "cuda":
            torch.cuda.empty_cache()
    return results


def _experiment(
    source: Path, manifest: Any, prompt_data: Any, label: str,
    max_new_tokens: int, runs_root: Path,
) -> dict[str, Any]:
    source = source.resolve()
    runs_root = runs_root.resolve()
    if runs_root == SOURCE_ROOT or SOURCE_ROOT in runs_root.parents:
        raise ValueError("Weight-space run artifacts must be stored outside the Sabrina source tree.")
    if runs_root == source or source in runs_root.parents:
        raise ValueError("Run artifacts cannot be written inside the source checkpoint directory.")
    edits = _validate_manifest(manifest)
    prompts = _read_prompts(prompt_data)
    if not 1 <= max_new_tokens <= MAX_NEW_TOKENS:
        raise ValueError(f"max_new_tokens must be between 1 and {MAX_NEW_TOKENS}.")
    capacity = _check_capacity(source, runs_root)
    source_hashes_before = _checkpoint_hashes(source)
    source_file_hashes_before = _model_file_hashes(source)
    run_dir = _safe_run_dir(label, runs_root)
    edited_dir = run_dir / "edited_model"
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False), encoding="utf-8")
    (run_dir / "prompts.json").write_text(json.dumps(prompts, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        print(f"Loading baseline model from {source}", file=sys.stderr, flush=True)
        torch, model, tokenizer, device = _load_model(source)
        applied = _apply_edits(model, edits)
        model.save_pretrained(edited_dir, safe_serialization=True)
        tokenizer.save_pretrained(edited_dir)
        del model, tokenizer
        if device == "cuda":
            torch.cuda.empty_cache()

        print("Generating matched baseline responses", file=sys.stderr, flush=True)
        baseline = _generate(source, prompts, max_new_tokens)
        print("Generating matched candidate responses", file=sys.stderr, flush=True)
        candidate = _generate(edited_dir, prompts, max_new_tokens)
        source_hashes_after = _checkpoint_hashes(source)
        source_file_hashes_after = _model_file_hashes(source)
        if source_hashes_after != source_hashes_before or source_file_hashes_after != source_file_hashes_before:
            raise RuntimeError("Source checkpoint changed during the experiment; candidate is retained but marked failed.")
        candidate_hashes = _checkpoint_hashes(edited_dir)
        candidate_file_hashes = _model_file_hashes(edited_dir)
        (run_dir / "baseline.json").write_text(json.dumps(baseline, indent=2, ensure_ascii=False), encoding="utf-8")
        (run_dir / "candidate.json").write_text(json.dumps(candidate, indent=2, ensure_ascii=False), encoding="utf-8")
        summary = []
        for base, edited in zip(baseline, candidate):
            summary.append({
                "name": base["name"],
                "baseline": base["response"][:2000],
                "candidate": edited["response"][:2000],
                "changed": base["response"] != edited["response"],
            })
        metadata = {
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source_model_dir": str(source),
            "source_weight_sha256": source_hashes_before,
            "source_file_sha256": source_file_hashes_before,
            "source_unchanged": True,
            "candidate_weight_sha256": candidate_hashes,
            "candidate_file_sha256": candidate_file_hashes,
            "manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True, allow_nan=False).encode("utf-8")
            ).hexdigest(),
            "prompt_set_sha256": hashlib.sha256(
                json.dumps(prompts, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
            "capacity": capacity,
            "device": device,
            "max_new_tokens": max_new_tokens,
            "greedy_decoding": True,
            "edits_applied": applied,
            "outputs": ["baseline.json", "candidate.json", "edited_model/"],
            "candidate_activated": False,
        }
        (run_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
        return {"status": "complete", "run_dir": str(run_dir), "metadata": metadata, "comparisons": summary}
    except Exception as exc:
        (run_dir / "FAILED.txt").write_text(
            f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        raise


def _inspect(source: Path, tensor_filter: str = "", limit: int = 100, device: str = "auto") -> dict[str, Any]:
    if not 1 <= limit <= MAX_INSPECT_TENSORS:
        raise ValueError(f"inspect limit must be between 1 and {MAX_INSPECT_TENSORS}.")
    _, model, _, device = _load_model(source, device)
    rows = []
    truncated = False
    try:
        total_params = sum(param.numel() for param in model.parameters())
        for name, param in model.named_parameters():
            if tensor_filter and tensor_filter.lower() not in name.lower():
                continue
            if len(rows) >= limit:
                truncated = True
                break
            values = param.detach().float()
            rows.append({
                "tensor": name, "shape": list(param.shape), "dtype": str(param.dtype),
                "numel": param.numel(), "norm": float(values.norm().item()),
                "mean": float(values.mean().item()), "std": float(values.std(unbiased=False).item()),
            })
    finally:
        del model
        if device == "cuda":
            import torch
            torch.cuda.empty_cache()
    return {
        "model_dir": str(source.resolve()), "device": device,
        "source_weight_sha256": _checkpoint_hashes(source),
        "parameter_count": total_params, "tensor_count_returned": len(rows),
        "truncated": truncated, "tensor_filter": tensor_filter, "tensors": rows,
    }


def handle_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise ValueError("RPC request must be a JSON object.")
    operation = request.get("action")
    alias, model_dir = _resolve_model(request.get("model_alias"))
    if operation == "inspect":
        report = _inspect(
            model_dir, str(request.get("tensor_filter", "")),
            int(request.get("limit", 100)), str(request.get("device", "auto")),
        )
        report["model_alias"] = alias
        return report
    if operation == "experiment":
        result = _experiment(
            model_dir, request.get("manifest"), request.get("prompts"),
            str(request.get("label", "experiment")),
            int(request.get("max_new_tokens", 128)), _runs_root(),
        )
        result["model_alias"] = alias
        return result
    raise ValueError("action must be 'inspect' or 'experiment'.")


def _rpc(_args: argparse.Namespace) -> int:
    raw = sys.stdin.buffer.read(MAX_RPC_BYTES + 1)
    if len(raw) > MAX_RPC_BYTES:
        print(json.dumps({"status": "error", "error": f"RPC request exceeds {MAX_RPC_BYTES} bytes."}))
        return 2
    try:
        request = json.loads(raw.decode("utf-8"))
        import fcntl
        runs_root = _runs_root()
        runs_root.mkdir(parents=True, exist_ok=True)
        lock_path = runs_root / ".weight_space_lab.lock"
        with lock_path.open("a", encoding="utf-8") as lock_stream:
            try:
                fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another weight-space action is already using the lab GPU.") from exc
            with contextlib.redirect_stdout(sys.stderr):
                result = handle_request(request)
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 2


def run_remote(request: dict[str, Any], *, helper: str = "weight_space_lab", timeout_seconds: int | None = None) -> dict[str, Any]:
    """Execute one alias-based RPC request on the lab using SSH or same-host execution."""
    import subprocess
    import shlex
    import sys

    host = os.environ.get("SABRINA_WEIGHT_SPACE_HOST", DEFAULT_REMOTE_HOST)
    user = os.environ.get("SABRINA_WEIGHT_SPACE_USER", DEFAULT_REMOTE_USER)
    root = os.environ.get("SABRINA_WEIGHT_SPACE_REMOTE_ROOT", DEFAULT_REMOTE_ROOT)
    timeout = int(os.environ.get("SABRINA_WEIGHT_SPACE_TIMEOUT_SECONDS", "7200"))
    if not 60 <= timeout <= 21_600:
        raise ValueError("SABRINA_WEIGHT_SPACE_TIMEOUT_SECONDS must be between 60 and 21600.")
    if timeout_seconds is not None:
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 21_600:
            raise ValueError("Invalid explicit lab RPC timeout")
        timeout = timeout_seconds
    if helper not in {"weight_space_lab", "model_archive", "coder_agent/research_pilot", "coder_agent/research_status", "coder_agent/calibrated_wsp", "coder_agent/jury_qlora", "customer_activation_probe"}:
        raise ValueError("Unsupported lab helper")
    script = f"{root.rstrip('/')}/tools/{helper}.py"
    quoted_script = shlex.quote(script)
    remote_command = (
        f"if ! test -f {quoted_script}; then "
        f"echo 'weight-space helper missing from configured lab deployment' >&2; exit 4; fi; "
        f"python3 {quoted_script} rpc"
    )
    ssh_bin = os.environ.get("SABRINA_SSH_EXECUTABLE", "ssh")
    if Path(script).resolve() == (Path(__file__).resolve().parent / f"{helper}.py").resolve():
        command = [sys.executable, script, "rpc"]
    else:
        command = [ssh_bin, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                   "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=6",
                   "-T", "-l", user, host, remote_command]
    try:
        completed = subprocess.run(
            command, input=json.dumps(request, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"OpenSSH executable {ssh_bin!r} is unavailable.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Lab weight-space action timed out after {timeout} seconds.") from exc
    if completed.returncode != 0:
        output = completed.stdout.strip()
        if output:
            try:
                payload = json.loads(output)
                raise RuntimeError(payload.get("error", output))
            except json.JSONDecodeError:
                pass
        detail = completed.stderr.strip()[-2000:] or output[-2000:] or f"SSH exit {completed.returncode}"
        raise RuntimeError(f"Lab weight-space action failed: {detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Lab returned invalid weight-space JSON. stderr tail: {completed.stderr[-1000:]}"
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError("Lab returned an invalid weight-space result.")
    if result.get("status") == "error":
        raise RuntimeError(result.get("error", "Unknown lab error."))
    return result


def inspect_model(args: argparse.Namespace) -> int:
    report = _inspect(Path(args.model_dir).expanduser().resolve(), args.tensor_filter, args.limit)
    serialized = json.dumps(report, indent=2, allow_nan=False)
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
        print(f"Inspection saved to: {output}")
    else:
        print(serialized)
    return 0


def run_experiment_cli(args: argparse.Namespace) -> int:
    manifest = _json(Path(args.manifest).expanduser().resolve())
    prompts = _json(Path(args.prompts).expanduser().resolve())
    result = _experiment(
        Path(args.model_dir).expanduser().resolve(), manifest, prompts,
        args.label, args.max_new_tokens, _runs_root(),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Inspect a local Transformers safetensors model.")
    inspect.add_argument("model_dir")
    inspect.add_argument("--tensor-filter", default="")
    inspect.add_argument("--limit", type=int, default=100)
    inspect.add_argument("--output")
    run = sub.add_parser("run", help="Apply a JSON edit manifest and compare deterministic generations.")
    run.add_argument("model_dir")
    run.add_argument("--manifest", required=True)
    run.add_argument("--prompts", required=True)
    run.add_argument("--label", default="experiment")
    run.add_argument("--max-new-tokens", type=int, default=128)
    run.set_defaults(func=run_experiment_cli)
    sub.add_parser("rpc", help="Read one alias-based JSON request from stdin (lab use only).").set_defaults(func=_rpc)
    inspect.set_defaults(func=inspect_model)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ValueError, RuntimeError, OSError, TypeError) as exc:
        print(f"[weight_space_lab] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
