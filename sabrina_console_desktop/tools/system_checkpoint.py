"""Full-system checkpoints for catastrophic-loss protection during core RSI.

The loop is allowed to refactor its OWN core (the loop, oracle routing, prompts). To make that safe
without a test-net, we keep restorable snapshots of the ENTIRE source tree -- core included -- in
`../sabrina_checkpoints/` OUTSIDE the repo, so no in-repo corruption (or even an errant `run_shell rm`
inside the tree) can destroy the restore points. Everything is local; nothing leaves the box
([[sabrina-repo-never-leaves-environment]]).

This module is part of the IMMUTABLE RECOVERY LAYER: it (and the supervisor, launcher, tracker,
pod_control) is on the loop's protected list, so a bad self-edit can never disable its own recovery.

    python tools/system_checkpoint.py create --label pre-core-session
    python tools/system_checkpoint.py list
    python tools/system_checkpoint.py restore <ts>
    python tools/system_checkpoint.py promote <ts>        # mark a checkpoint known-good
    python tools/system_checkpoint.py latest-known-good
"""

import argparse
import datetime as _dt
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKPOINT_ROOT = REPO_ROOT.parent / "sabrina_checkpoints"   # OUTSIDE the repo on purpose

# Real source lives here (core included -- unlike improvement_tracker, we WANT the core in checkpoints).
SOURCE_ROOTS = ("tools", "ai_systems", "config", "mcp_server", "api", "tests")
SOURCE_EXTS = (".py", ".yaml", ".yml", ".json", ".md", ".txt", ".cfg", ".ini", ".toml")
EXCLUDE_SEGMENTS = {"__pycache__", ".git", ".venv", "node_modules", "data", "models",
                    "security_items", "logs", "archives", "scratch", "dev"}
MAX_FILE_BYTES = 1_000_000   # skip embedded data blobs; source files are tiny
KNOWN_GOOD_MARKER = ".known_good"


def _iter_source_files():
    """Yield repo-relative source paths to checkpoint: root-level source files + SOURCE_ROOTS trees,
    excluding vendored/generated/data dirs, big blobs, and sandbox clones."""
    # root-level source files (sabrina_gemini_loop.py, pytest.ini, etc.)
    for entry in sorted(REPO_ROOT.iterdir()):
        if entry.is_file() and entry.suffix in SOURCE_EXTS and entry.stat().st_size <= MAX_FILE_BYTES:
            yield entry.relative_to(REPO_ROOT)
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames
                           if d not in EXCLUDE_SEGMENTS and not d.startswith(".sandbox_clone_")]
            for fn in filenames:
                p = Path(dirpath) / fn
                if p.suffix not in SOURCE_EXTS:
                    continue
                try:
                    if p.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                yield p.relative_to(REPO_ROOT)


def create(label=""):
    """Snapshot the whole source tree to a new timestamped checkpoint dir. Returns its Path."""
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{label}" if label else ts
    dest = CHECKPOINT_ROOT / name
    n = 0
    for rel in _iter_source_files():
        src = REPO_ROOT / rel
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            n += 1
        except Exception:
            pass
    (dest / ".manifest").write_text(f"files={n}\ncreated={ts}\nlabel={label}\n", encoding="utf-8")
    print(f"[checkpoint] created {dest} ({n} files)", flush=True)
    return dest


def list_checkpoints():
    if not CHECKPOINT_ROOT.exists():
        return []
    return sorted((d for d in CHECKPOINT_ROOT.iterdir() if d.is_dir()), key=lambda d: d.name)


def _resolve(ts_or_path):
    p = Path(ts_or_path)
    if p.is_dir():
        return p
    cand = CHECKPOINT_ROOT / ts_or_path
    if cand.is_dir():
        return cand
    # allow a bare timestamp prefix match
    matches = [d for d in list_checkpoints() if d.name.startswith(ts_or_path)]
    return matches[-1] if matches else None


def restore(ts_or_path):
    """Copy a checkpoint's files back over the working tree (overwrites corrupted core files).
    Does NOT delete files added since the checkpoint -- it heals broken files, safely and idempotently."""
    ckpt = _resolve(ts_or_path)
    if not ckpt:
        return False, f"no checkpoint matching {ts_or_path!r}"
    n = 0
    for dirpath, _dirs, filenames in os.walk(ckpt):
        for fn in filenames:
            if fn in (".manifest", KNOWN_GOOD_MARKER):
                continue
            src = Path(dirpath) / fn
            rel = src.relative_to(ckpt)
            dst = REPO_ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                n += 1
            except Exception:
                pass
    print(f"[checkpoint] restored {n} files from {ckpt.name}", flush=True)
    return True, f"restored {n} files from {ckpt.name}"


def promote(ts_or_path):
    """Mark a checkpoint known-good (the system provably ran at that point)."""
    ckpt = _resolve(ts_or_path)
    if not ckpt:
        return False, f"no checkpoint matching {ts_or_path!r}"
    (ckpt / KNOWN_GOOD_MARKER).write_text(_dt.datetime.now().isoformat(), encoding="utf-8")
    return True, f"promoted {ckpt.name} to known-good"


def latest_known_good():
    goods = [d for d in list_checkpoints() if (d / KNOWN_GOOD_MARKER).exists()]
    return goods[-1] if goods else None


def prune(keep=30):
    """Delete oldest non-known-good checkpoints beyond `keep` (known-good are always kept)."""
    ckpts = list_checkpoints()
    disposable = [d for d in ckpts if not (d / KNOWN_GOOD_MARKER).exists()]
    for d in disposable[:-keep] if keep else disposable:
        try:
            shutil.rmtree(d)
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Full-system checkpoints (outside the repo).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--label", default="")
    sub.add_parser("list")
    r = sub.add_parser("restore"); r.add_argument("ts")
    p = sub.add_parser("promote"); p.add_argument("ts")
    sub.add_parser("latest-known-good")
    pr = sub.add_parser("prune"); pr.add_argument("--keep", type=int, default=30)
    a = ap.parse_args()
    if a.cmd == "create":
        create(a.label)
    elif a.cmd == "list":
        for d in list_checkpoints():
            good = " [known-good]" if (d / KNOWN_GOOD_MARKER).exists() else ""
            print(d.name + good)
    elif a.cmd == "restore":
        ok, detail = restore(a.ts); print(detail); sys.exit(0 if ok else 1)
    elif a.cmd == "promote":
        ok, detail = promote(a.ts); print(detail); sys.exit(0 if ok else 1)
    elif a.cmd == "latest-known-good":
        g = latest_known_good(); print(g.name if g else "(none)")
    elif a.cmd == "prune":
        prune(a.keep); print("pruned")
