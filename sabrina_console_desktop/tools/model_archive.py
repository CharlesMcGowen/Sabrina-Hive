"""Bounded model archive catalog and repair RPC; never moves or deletes originals."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import weight_space_lab as wsp


def config():
    path = Path(os.environ.get("SABRINA_MODEL_ARCHIVE_CONFIG", str(wsp._config_home() / "sabrina/model_archive.json")))
    data = wsp._json(path)
    if not isinstance(data.get("roots"), dict) or not data["roots"]:
        raise ValueError("Archive configuration requires named roots.")
    return data


def inside(root, relative):
    root = Path(root).resolve()
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError("Use a relative checkpoint path without parent traversal.")
    path = (root / part).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path escapes configured archive root.")
    return path


def root_for(cfg, name):
    if name not in cfg["roots"]:
        raise ValueError("Unknown archive root; available: " + ", ".join(cfg["roots"]))
    root = Path(cfg["roots"][name]).resolve()
    if not root.is_dir():
        raise ValueError(f"Archive root unavailable: {root}")
    if root == wsp.SOURCE_ROOT or root.is_relative_to(wsp.SOURCE_ROOT):
        raise ValueError("Archive roots must be outside Sabrina source.")
    return root


def checkpoint_file(path, name):
    """Resolve a weight locally, including a standard HF snapshot-to-blob link."""
    if Path(name).name != name or "\\" in name:
        raise ValueError("Shard index contains a nonlocal filename")
    path = Path(path).resolve()
    try:
        return inside(path, name)
    except ValueError:
        # HF snapshots intentionally symlink to the same repository's blobs.
        # Do not relax the general archive traversal guard or permit other links.
        target = (path / name).resolve()
        for snapshot in (path, *path.parents):
            if (snapshot.parent.name != "snapshots"
                    or not snapshot.parent.parent.name.startswith("models--")
                    or not re.fullmatch(r"[0-9a-f]{40}", snapshot.name)):
                continue
            blobs = snapshot.parent.parent / "blobs"
            if (not blobs.is_symlink() and target.parent == blobs
                    and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", target.name)):
                return target
        raise ValueError("Weight path escapes checkpoint and its Hugging Face repository blob store")


def validate(path):
    """Header/index validation only. Does not load weights or execute model code."""
    from safetensors import safe_open
    path = Path(path)
    errors, warnings, headers = [], [], {}
    names = {p.name for p in path.iterdir() if p.is_file()}
    kind = "hf_safetensors"
    if "adapter_config.json" in names:
        kind = "adapter"
    elif not any(n.endswith(".safetensors") for n in names) and any(n.endswith(".gguf") for n in names):
        kind = "gguf"
    cfg = {}
    try:
        cfg = wsp._json(path / "config.json")
        if not isinstance(cfg, dict) or not cfg.get("model_type"):
            errors.append("config.json lacks model_type")
    except ValueError as exc:
        errors.append(str(exc))
    if not (names & {"tokenizer.json", "tokenizer.model", "spiece.model"} or {"vocab.json", "merges.txt"} <= names):
        errors.append("Local tokenizer files missing")
    index = path / "model.safetensors.index.json"
    mapping = {}
    if index.is_file():
        try:
            mapping = wsp._json(index)["weight_map"]
            if not isinstance(mapping, dict) or not mapping or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
                raise ValueError("Invalid weight_map")
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"Invalid shard index: {exc}")
            mapping = {}
    weights = sorted(n for n in names if n == "model.safetensors" or re.fullmatch(r"model-\d+-of-\d+\.safetensors", n))
    expected = set(mapping.values()) if mapping else set(weights)
    if not weights:
        errors.append("No supported full-model safetensors weights")
    if any(n.startswith("model-") for n in weights) and not mapping:
        errors.append("Sharded checkpoint requires a valid model.safetensors.index.json")
    missing = sorted(expected - names)
    for name in sorted(expected):
        try:
            if Path(name).name != name or "\\" in name:
                raise ValueError("Shard index contains a nonlocal filename")
            shard = checkpoint_file(path, name)
            with safe_open(str(shard), framework="pt", device="cpu") as handle:
                keys = set(handle.keys())
                if not keys:
                    raise ValueError("Empty weights file")
                headers[name] = len(keys)
                wrong = [k for k, v in mapping.items() if v == name and k not in keys]
                if wrong:
                    errors.append(f"{name}: {len(wrong)} indexed tensors missing")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    extras = sorted(set(weights) - expected)
    if extras:
        errors.append("Unexpected weight files: " + ", ".join(extras))
    if kind != "hf_safetensors":
        errors.append(f"{kind} is cataloged but not supported by weight_space_lab")
    if cfg.get("auto_map"):
        warnings.append("Custom model code may be required; execution is disabled")
    return {"format": kind, "status": "valid" if not errors else "needs_attention", "model_type": cfg.get("model_type"),
            "architectures": cfg.get("architectures"), "missing_shards": missing, "weight_files": weights,
            "header_tensor_counts": headers, "errors": errors, "warnings": warnings,
            "validation_scope": "index, safetensors headers and tokenizer presence; not inference or payload checksums"}


def inventory(root, limit=200, depth=8):
    if type(limit) is not int or not 1 <= limit <= 500 or type(depth) is not int or not 0 <= depth <= 12:
        raise ValueError("limit must be 1..500; depth must be 0..12")
    result, errors = [], []
    start, visited, truncated = time.monotonic(), 0, False
    def onerror(exc):
        errors.append(str(exc))
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
        visited += 1
        rel = Path(directory).relative_to(root)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__", "venv"} and not (Path(directory) / d).is_symlink())
        if len(rel.parts) >= depth:
            if dirs:
                truncated = True
            dirs[:] = []
        if visited > 20000 or time.monotonic() - start > 45:
            truncated = True
            break
        if {"config.json", "adapter_config.json", "model.safetensors.index.json"} & set(files) or any(n.endswith((".safetensors", ".gguf", ".incomplete")) for n in files):
            result.append({"relative_path": str(rel), "safetensors_files": sum(n.endswith(".safetensors") for n in files),
                           "gguf_files": sum(n.endswith(".gguf") for n in files), "partial_files": sum(n.endswith(".incomplete") for n in files),
                           "has_index": "model.safetensors.index.json" in files, "has_config": "config.json" in files,
                           "adapter": "adapter_config.json" in files})
            if len(result) >= limit:
                truncated = True
                break
    return {"entries": result, "truncated": truncated, "scan_errors": errors, "directories_visited": visited}


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def source_layout(cfg, source_id):
    """Resolve operator-configured provenance and storage, including collections."""
    source = cfg.get("sources", {}).get(source_id)
    if not source:
        raise ValueError("Unknown source_id. Configure verified repo_id and commit revision outside the repo first.")
    repo, revision = source["repo_id"], source["revision"]
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repo) or not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Repair requires a repository ID and full 40-character commit revision")
    subfolder = source.get("subfolder", "")
    if not isinstance(subfolder, str) or (subfolder and not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", subfolder)) or any(p in {".", ".."} for p in subfolder.split("/")):
        raise ValueError("Invalid source subfolder")
    managed = root_for(cfg, source["root"]) if source.get("root") else Path(cfg["managed_root"]).resolve()
    if not managed.is_dir() or managed == wsp.SOURCE_ROOT or managed.is_relative_to(wsp.SOURCE_ROOT):
        raise ValueError("Managed root must exist outside Sabrina source")
    destination = managed / (repo.replace("/", "--") + "--" + revision)
    return source, destination, inside(destination, subfolder or ".")


def repair(cfg, source_id):
    """Resume into a separate managed copy, pinned by operator-configured source ID."""
    from huggingface_hub import HfApi, snapshot_download
    source, destination, checkpoint = source_layout(cfg, source_id)
    repo, revision = source["repo_id"], source["revision"]
    subfolder = source.get("subfolder", "")
    prefix = subfolder + "/" if subfolder else ""
    if destination.is_symlink():
        raise ValueError("Managed destination cannot be a symlink")
    if destination.exists() and any(p.is_symlink() for p in destination.rglob("*")):
        raise ValueError("Managed destination contains symlinks")
    auth = {"token": False} if source.get("anonymous") else {}
    info = HfApi(**auth).model_info(repo, revision=revision, files_metadata=True)
    allowed = []
    total = 0
    for item in info.siblings:
        name = item.rfilename
        if not name.startswith(prefix):
            continue
        relative = name[len(prefix):]
        if "/" not in relative and (relative.endswith(".safetensors") or relative in {
            "config.json", "generation_config.json", "model.safetensors.index.json", "tokenizer.json", "tokenizer_config.json",
            "special_tokens_map.json", "tokenizer.model", "spiece.model", "vocab.json", "merges.txt", "added_tokens.json", "chat_template.jinja"}):
            if not isinstance(item.size, int) or item.size < 0:
                raise ValueError("Hub did not provide a reliable file size")
            allowed.append(name)
            total += item.size
    budget = min(int(source.get("max_download_bytes", cfg.get("max_download_bytes", 20 * 1024**3))), 100 * 1024**3)
    if not allowed or total > budget:
        raise ValueError(f"Snapshot size {total} exceeds configured budget {budget}, or contains no supported files")
    if shutil.disk_usage(destination.parent).free < total + 2 * 1024**3:
        raise ValueError("Insufficient free disk space for snapshot and 2 GiB reserve")
    snapshot_download(repo_id=repo, revision=revision, local_dir=str(destination), allow_patterns=allowed, max_workers=2, **auth)
    verified = {}
    for name, expected in source.get("sha256", {}).items():
        if not re.fullmatch(r"[a-f0-9]{64}", expected):
            raise ValueError("Invalid configured SHA256")
        path = inside(checkpoint, name)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("Checkpoint checksum mismatch: " + name)
        verified[name] = expected
    return {"destination": str(checkpoint), "download_root": str(destination), "repo_id": repo,
            "revision": revision, "subfolder": subfolder, "snapshot_bytes": total, "sha256_verified": verified,
            "validation": validate(checkpoint), "originals_modified": False}


def handle_request(request):
    if not isinstance(request, dict):
        raise ValueError("Request must be an object")
    cfg = config()
    action = request.get("action")
    if action == "resume_download":
        result = repair(cfg, request.get("source_id"))
    elif action == "audit_lineage":
        from tools.coder_agent.model_lineage import audit_merge
        alias=request.get("model_alias")
        declared=cfg.get("lineage_sources", {}).get(alias)
        if not isinstance(declared, dict) or set(declared)!={"base", "adapter"}:
            raise ValueError("Lineage audit requires an operator-configured base and adapter for this alias")
        _,merged=wsp._resolve_model(alias)
        result=audit_merge(merged,Path(declared["adapter"]),Path(declared["base"]))
        result["model_alias"]=alias
    elif action in {"audit_security_qa", "prepare_security_qa"}:
        from tools.coder_agent import security_qa
        sources = cfg.get("security_qa_sources", {})
        source_id = request.get("source_id")
        if source_id not in sources:
            raise ValueError("Unknown security QA source_id; use an operator-configured dataset")
        path = Path(sources[source_id]).resolve()
        if path == wsp.SOURCE_ROOT or path.is_relative_to(wsp.SOURCE_ROOT):
            raise ValueError("Security QA datasets must be outside Sabrina source")
        if action == "audit_security_qa":
            result = security_qa.audit(path)
        else:
            runs = Path(cfg["research_run_root"]).resolve()
            if runs == wsp.SOURCE_ROOT or runs.is_relative_to(wsp.SOURCE_ROOT):
                raise ValueError("Research runs must be outside Sabrina source")
            result = security_qa.prepare(path, runs)
        result["source_id"] = source_id
    else:
        root = root_for(cfg, request.get("root"))
        if action in {"inventory", "organize"}:
            result = inventory(root, request.get("limit", 200), request.get("depth", 8))
            if action == "organize":
                result["proposals"] = [{"relative_path": e["relative_path"], "category": "adapters" if e["adapter"] else "gguf" if e["gguf_files"] else "checkpoints-to-validate",
                                        "operation": "catalog_in_place"} for e in result["entries"]]
                result["files_moved_or_deleted"] = False
        elif action in {"validate", "register"}:
            path = inside(root, request.get("relative_path", "."))
            if not path.is_dir():
                raise ValueError("Checkpoint directory unavailable")
            result = validate(path)
            result["path"] = str(path)
            if action == "register":
                alias = request.get("model_alias", "")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", alias):
                    raise ValueError("Invalid model alias")
                if result["status"] != "valid":
                    raise ValueError("Checkpoint did not pass validation: " + "; ".join(result["errors"]))
                registry_path = wsp._registry_path()
                registry = wsp._json(registry_path) if registry_path.exists() else {"models": {}}
                existing = registry["models"].get(alias)
                if existing is not None and existing != str(path):
                    raise ValueError("Alias already exists; refusing to replace it")
                registry["models"][alias] = str(path)
                save_json(registry_path, registry)
                result["model_alias"] = alias
        else:
            raise ValueError("Unknown action: use inventory, validate, organize, register, resume_download, audit_security_qa, prepare_security_qa, audit_lineage")
    report_root = Path(cfg["report_root"]).resolve()
    if report_root == wsp.SOURCE_ROOT or report_root.is_relative_to(wsp.SOURCE_ROOT):
        raise ValueError("Reports must be outside Sabrina source")
    report = report_root / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".json")
    result.update({"action": action, "report_path": str(report)})
    save_json(report, {"request": request, "result": result})
    return result


def run_remote(request):
    return wsp.run_remote(request, helper="model_archive")


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("Request exceeds 64 KiB")
        import fcntl
        lock = wsp._config_home() / "sabrina/.model_archive.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with contextlib.redirect_stdout(sys.stderr):
                result = handle_request(json.loads(raw))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
