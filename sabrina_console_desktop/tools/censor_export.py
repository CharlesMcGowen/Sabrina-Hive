#!/usr/bin/env python3
"""
tools/censor_export.py
======================
Batch PII/IP censor for a directory tree of Python source, built as a thin
wrapper around the existing ContextSanitizer (tools/context_sanitizer.py).

It walks --src, sanitizes every *.py through the shared sanitizer at the chosen
disclosure level, and writes structure-preserving censored copies to --out.
The combined placeholder->real manifest is written to --manifest (a path OUTSIDE
--out) and chmod 600'd — it holds real secrets and must never leave the host or
land inside the censored tree.

Output is COUNTS ONLY. No matched value, and no manifest entry, is ever printed.

Runs where ContextSanitizer's models live (i.e. on the lab). Invoke from the repo
root so `models/sabrina-pii-ner` resolves:

    python -m tools.censor_export --src /media/ego/SANDISK \\
        --out /home/ego/censored_python_code \\
        --level LOW \\
        --manifest /home/ego/.censor_manifests/run.json --verify
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import stat
import sys
from collections import Counter
from pathlib import Path

# Allow running both as `python -m tools.censor_export` and `python censor_export.py`
try:
    from tools.context_sanitizer import get_sanitizer
except ImportError:  # pragma: no cover - fallback when cwd is tools/
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.context_sanitizer import get_sanitizer

# Directories and filename fragments that are logs/findings, never source to keep.
SKIP_DIRS = {
    "logs", "log", "findings", "findings_reports", "reports", "post_mortems",
    "__pycache__", ".git", "node_modules", "venv", ".venv", "chat_logs",
}
SKIP_NAME_SUBSTR = ("chat_log", "_log", "finding", "session_", "post_mortem")


def _iter_py_files(src: Path):
    """Yield *.py paths under src, pruning log/finding dirs and files."""
    for root, dirs, files in os.walk(src):
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS
            and not any(s in d.lower() for s in SKIP_NAME_SUBSTR)
        ]
        for fn in files:
            if not fn.lower().endswith(".py"):
                continue
            if any(s in fn.lower() for s in SKIP_NAME_SUBSTR):
                continue
            yield Path(root) / fn


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch PII/IP censor via ContextSanitizer")
    ap.add_argument("--src", required=True, help="Source dir (e.g. the SanDisk mount)")
    ap.add_argument("--out", required=True, help="Destination dir for censored *.py")
    ap.add_argument("--level", default="LOW", choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--manifest", default=None,
                    help="Where to write the combined manifest (kept OUTSIDE --out). "
                         "Default: /home/ego/.censor_manifests/censor_<ts>.json")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N files (0 = all)")
    ap.add_argument("--verify", action="store_true", help="py_compile each output file")
    args = ap.parse_args()

    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not src.is_dir():
        print(f"[!] --src is not a directory: {src}")
        return 2
    if out == src or src in out.parents:
        print("[!] --out must not be inside --src.")
        return 2

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    manifest_path = Path(
        args.manifest or f"/home/ego/.censor_manifests/censor_{ts}.json"
    ).expanduser().resolve()
    if manifest_path == out or out in manifest_path.parents:
        print("[!] --manifest must not live inside --out (it holds real secrets).")
        return 2

    # One shared sanitizer/singleton so the same secret dedups to one placeholder
    # across the whole run.
    sanitizer = get_sanitizer(disclosure_level=args.level)

    combined_manifest: dict[str, str] = {}
    totals: Counter = Counter()
    warnings_seen: Counter = Counter()
    n_files = n_written = n_bad = 0
    bad_files: list[str] = []

    for sp in _iter_py_files(src):
        if args.limit and n_files >= args.limit:
            break
        n_files += 1
        try:
            raw = sp.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            warnings_seen[f"read_error:{type(e).__name__}"] += 1
            continue

        result = sanitizer.sanitize(raw, level=args.level)
        combined_manifest.update(result.manifest)
        for k, v in result.entity_counts.items():
            totals[k] += v
        for w in result.warnings:
            warnings_seen[w[:60]] += 1

        dp = out / sp.relative_to(src)
        dp.parent.mkdir(parents=True, exist_ok=True)
        dp.write_text(result.safe_text, encoding="utf-8")
        n_written += 1

        if args.verify:
            import py_compile
            try:
                py_compile.compile(str(dp), doraise=True)
            except Exception:
                n_bad += 1
                bad_files.append(str(dp.relative_to(out)))

    # Write the combined manifest OUTSIDE the censored tree, locked down.
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "created": ts,
                "src": str(src),
                "out": str(out),
                "level": args.level,
                "entries": combined_manifest,  # placeholder -> real value (SECRET)
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    try:
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    except Exception:
        pass

    # ── Summary — counts only, never a value ──────────────────────────────────
    print("\n=== CENSOR EXPORT SUMMARY (values are NOT shown) ===")
    print(f"  level          : {args.level}")
    print(f"  files scanned  : {n_files}")
    print(f"  files written  : {n_written}  ->  {out}")
    if args.verify:
        print(f"  parse-ok       : {n_written - n_bad}/{n_written}")
        for bf in bad_files:
            print(f"    [!] re-check (structure changed): {bf}")
    print(f"  unique secrets : {len(combined_manifest)}")
    print(f"  manifest       : {manifest_path}  (chmod 600, keep on this host)")
    print("  redactions by type:")
    for typ, c in sorted(totals.items(), key=lambda kv: -kv[1]):
        print(f"    {typ:<12} {c}")
    if warnings_seen:
        print("  sanitizer notes:")
        for w, c in warnings_seen.most_common():
            print(f"    ({c}x) {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
