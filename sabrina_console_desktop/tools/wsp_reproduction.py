"""Prepare pinned research artifacts and check released-patch tensor identities.

No model download, tensor mutation, upstream code execution, or inference occurs.
Downloaded source is retained for review, not imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "research" / "wsp_001" / "protocol.json"
ARTIFACTS = (
    "exp8_math/README.md",
    "exp8_math/code/apply_edit.py",
    "exp8_math/code/st_numpy.py",
    "exp8_math/code/profiles.py",
    "exp8_math/runs/p50c/edit/manifest.json",
    "exp8_math/runs/p50c/edit/edit.npz",
)
DEFAULT_CACHE = Path.home() / ".cache" / "sabrina" / "wsp_001"
MAX_DOWNLOAD = 16 * 1024 * 1024


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(destination: Path) -> dict:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    commit = protocol["upstream_commit"]
    root = destination / commit
    records = []
    for relative in ARTIFACTS:
        url = f"https://raw.githubusercontent.com/{protocol['upstream_repository']}/{commit}/{relative}"
        with urllib.request.urlopen(url, timeout=30) as response:
            data = response.read(MAX_DOWNLOAD + 1)
        if len(data) > MAX_DOWNLOAD:
            raise ValueError(f"Artifact exceeds download limit: {relative}")
        target = root / relative
        if target.exists() and target.read_bytes() != data:
            raise ValueError(f"Existing artifact differs; preserve it for review: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        records.append({"path": relative, "url": url, "bytes": len(data), "sha256": digest(data)})
    report = {"commit": commit, "artifacts": records,
              "provenance": "Retrieved from commit-pinned upstream URLs; hashes record downloaded bytes, not independent signatures.",
              "model_downloaded": False, "upstream_code_executed": False}
    (root / "download_manifest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return {"artifact_dir": str(root), **report}


def tensor_hashes(path: Path, manifest: dict, candidate: bool = False) -> list[dict]:
    """Hash original BF16 storage directly; no torch load or dtype conversion."""
    results = []
    size = path.stat().st_size
    with path.open("rb") as stream:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise ValueError("Truncated safetensors header")
        header_size = struct.unpack("<Q", prefix)[0]
        if not 2 <= header_size <= min(64 * 1024 * 1024, size - 8):
            raise ValueError("Invalid safetensors header size")
        header = json.loads(stream.read(header_size))
        start = 8 + header_size
        for expected in manifest["tensors"]:
            name = expected["name"]
            item = header.get(name)
            if not isinstance(item, dict):
                raise ValueError(f"Missing tensor: {name}")
            if item.get("dtype") != "BF16" or item.get("shape") != expected["shape"]:
                raise ValueError(f"Unexpected dtype/shape: {name}")
            lo, hi = item["data_offsets"]
            count = 1
            for dim in expected["shape"]:
                count *= dim
            if not (type(lo) is int and type(hi) is int and 0 <= lo < hi <= size - start
                    and hi - lo == count * 2):
                raise ValueError(f"Invalid tensor bounds: {name}")
            stream.seek(start + lo)
            actual = digest(stream.read(hi - lo))
            wanted = expected["edited_sha256" if candidate else "base_sha256"]
            results.append({"tensor": name, "sha256": actual, "expected": wanted, "matches": actual == wanted})
    return results


def check(cache: Path, model: Path | None, candidate: bool = False) -> dict:
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    root = cache / protocol["upstream_commit"]
    blockers = []
    download_manifest = root / "download_manifest.json"
    if not download_manifest.is_file():
        return {"ready_for_patch_integrity_check": False, "blockers": ["Run prepare first."]}
    downloaded = json.loads(download_manifest.read_text(encoding="utf-8"))
    records = {item["path"]: item for item in downloaded["artifacts"]}
    for relative in ARTIFACTS:
        path = root / relative
        if relative not in records or not path.is_file() or digest(path.read_bytes()) != records[relative]["sha256"]:
            blockers.append(f"Missing or altered cached artifact: {relative}")
    tensors = []
    if model is None:
        blockers.append("No local pinned base checkpoint supplied.")
    elif not blockers:
        manifest = json.loads((root / protocol["patch"] / "manifest.json").read_text(encoding="utf-8"))
        tensors = tensor_hashes(model, manifest, candidate)
        if not all(item["matches"] for item in tensors):
            blockers.append("Released tensor hashes do not match this checkpoint.")
    return {"ready_for_patch_integrity_check": not blockers, "blockers": blockers,
            "checked_kind": "candidate" if candidate else "base", "tensors": tensors,
            "benchmark_run": False,
            "limits": "Checks only patched tensor identities. Full checkpoint revision, configuration, tokenizer and runtime still require verification.",
            "existing_sabrina_evaluator_compatible": False,
            "evaluator_gap": "Current weight_space_lab uses greedy decoding and <=512 new tokens; upstream uses sampled thinking up to 32768 tokens."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "check"))
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--model", type=Path, help="Single model.safetensors file to check")
    parser.add_argument("--candidate", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = prepare(args.cache) if args.action == "prepare" else check(args.cache, args.model, args.candidate)
    text = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if args.action == "prepare" or report["ready_for_patch_integrity_check"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
