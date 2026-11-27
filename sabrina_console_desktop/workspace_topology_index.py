"""Live Workspace Delta Tracker & Multi-Machine Code Catalog Bridge for Sabrina Topology UI.

Provides real-time visibility inside Live Multi-Agent Topology (`#view-swarm`) for:
1. Files created (`CREATED`) and modified (`MODIFIED`) during Swarm missions, Harness turns,
   Workspace Agent edits, or on-disk workspace updates, with per-agent attribution, SHA-256
   digests, line-delta counts (`+added / -removed`), and unified diffs.
2. Safe in-workspace file inspection (numbered source, unified diff, AST symbols/imports, and
   bounded base64 visual previews for `.svg`, `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`).
3. Integration with Sabrina's existing machine & codebase indexing infrastructure:
   - `tools/lab_code_catalog.py` (CPU-only SQLite + FTS5 AST symbol/import & SHA-256 catalog)
   - `tools/code_indexer.py` & `tools/rag_knowledge.py` (`sabrina_knowledge_rag.db` FTS5 chunks)
   - `tools/lab_index.py` (`LAB_INDEX` across `ego.local` projects, services, and containers)
   - `tools/sabrina_compute_federation.py` (`windows-workstation`, `ego-lab`, and enrolled nodes)

Safety Invariants:
- `execution_authorized` is strictly `False` on every returned payload.
- Every filesystem read is jailed inside `workspace_root` and validated via `guard_safe_path()`.
- Secret, credential, key, and SQLite database files are strictly excluded from content inspection.
"""

from __future__ import annotations

import ast
import base64
import copy
import difflib
import hashlib
import json
import mimetypes
import os
import platform
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_ROOT = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(DESKTOP_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from swarm_console.protocol import PathIsolationError, guard_safe_path

DEFAULT_CATALOG_DB = DESKTOP_ROOT / "data" / "code_catalog.sqlite3"
DEFAULT_RAG_DB = DESKTOP_ROOT / "tools" / "sabrina_knowledge_rag.db"
DEFAULT_FEDERATION_NODES_JSON = DESKTOP_ROOT / "data" / "compute_federation_nodes.json"

SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "site-packages",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".sabrina_checkpoints",
    "models",
    "security_items",
    "archives",
    "datastore",
    "logs",
    "Sabrina-dev",
    "github_variant",
    "rocm_venv",
    "unsloth_compiled_cache",
})

BLOCKED_FILE_NAMES: frozenset[str] = frozenset({
    ".env",
    "provider_vault.json",
    "connectors.dpapi.json",
    ".federation_token",
})

BLOCKED_SUFFIXES: tuple[str, ...] = (
    ".dpapi.json",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".db",
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".token",
    ".pyc",
    ".pyo",
)

IMAGE_PREVIEW_EXTS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}

TEXT_INSPECT_EXTS: frozenset[str] = frozenset({
    ".py", ".pyi", ".html", ".htm", ".css", ".svg", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".json", ".jsonc", ".md", ".rst", ".txt",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".bat", ".cmd",
    ".sql", ".c", ".h", ".cpp", ".hpp", ".rs", ".go", ".java", ".rb", ".php",
    ".xml", ".csv",
})

MAX_TEXT_SNAPSHOT_BYTES = 262_144      # 256 KiB for unified diff / inline text preview
MAX_HASH_FILE_BYTES = 4_194_304        # 4 MiB max for SHA-256 digest
MAX_IMAGE_PREVIEW_BYTES = 2_097_152    # 2 MiB max for inline data-URI image preview
MAX_WORKSPACE_SCAN_FILES = 450


def _clock_str(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def _is_blocked_rel_path(rel_posix: str, filename: str) -> bool:
    low_name = filename.lower()
    low_rel = rel_posix.lower()
    if low_name in BLOCKED_FILE_NAMES:
        return True
    if low_name.startswith(("id_rsa", "id_ed25519", ".env.")):
        return True
    if low_rel.endswith(BLOCKED_SUFFIXES):
        return True
    return False


def _should_skip_dir(dir_name: str, rel_dir_posix: str = "") -> bool:
    if dir_name in SKIP_DIR_NAMES or dir_name.startswith(".") or dir_name.startswith(".sandbox_clone_"):
        return True
    # When scanning the top-level Sabrina repo root, skip runtime DB & virtualenv subtrees for speed
    if rel_dir_posix in {"sabrina_console_desktop/data", "freqtrade_pivot"}:
        return True
    return False


def _hash_and_sample_file(path: Path) -> dict[str, Any]:
    """Read bounded stat, SHA-256 digest, and optional UTF-8 text sample for diffing."""
    try:
        st = path.stat()
    except OSError:
        return {"existed": False, "size": 0, "mtime_ns": 0, "sha256": "", "text": None}
    size = int(st.st_size)
    mtime_ns = int(st.st_mtime_ns)
    sha256_hex = ""
    text_sample: str | None = None
    ext = path.suffix.lower()
    if size <= MAX_HASH_FILE_BYTES:
        try:
            raw = path.read_bytes()
            sha256_hex = hashlib.sha256(raw).hexdigest()
            if size <= MAX_TEXT_SNAPSHOT_BYTES and b"\x00" not in raw[:4096]:
                if ext in TEXT_INSPECT_EXTS or path.name in {"Dockerfile", "Makefile", "CMakeLists.txt"}:
                    text_sample = raw.decode("utf-8", errors="replace")
        except OSError:
            pass
    return {
        "existed": True,
        "size": size,
        "mtime_ns": mtime_ns,
        "sha256": sha256_hex,
        "text": text_sample,
    }


def _compute_diff_and_counts(
    rel_posix: str,
    old_text: str | None,
    new_text: str | None,
    *,
    existed_before: bool,
    max_diff_chars: int = 4200,
) -> tuple[int, int, str]:
    """Compute (`lines_added`, `lines_removed`, `unified_diff_snippet`)."""
    if new_text is None:
        return 0, 0, ""
    new_lines = new_text.splitlines()
    if not existed_before or old_text is None:
        added = len(new_lines)
        preview_lines = [f"--- /dev/null", f"+++ b/{rel_posix}", f"@@ -0,0 +1,{added} @@"]
        for ln in new_lines[:90]:
            preview_lines.append(f"+{ln}")
        if added > 90:
            preview_lines.append(f"... (+{added - 90} more lines)")
        return added, 0, "\n".join(preview_lines)[:max_diff_chars]

    old_lines = old_text.splitlines()
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{rel_posix}",
            tofile=f"b/{rel_posix}",
            lineterm="",
            n=3,
        )
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
    diff_str = "\n".join(diff_lines)
    if len(diff_str) > max_diff_chars:
        diff_str = diff_str[:max_diff_chars] + "\n... [diff truncated]"
    return added, removed, diff_str


def _extract_symbols_light(text: str, suffix: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Use `tools.lab_code_catalog.extract` for Python AST symbols/imports, plus lightweight HTML/CSS tags."""
    if suffix == ".py":
        try:
            from sabrina_console_desktop.tools import lab_code_catalog
            symbols, imports, _ = lab_code_catalog.extract(text, suffix)
            return list(symbols[:40]), list(imports[:30])
        except Exception:
            try:
                tree = ast.parse(text)
                syms = [
                    {"name": n.name[:120], "line": n.lineno, "kind": type(n).__name__}
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                ]
                return syms[:40], []
            except Exception:
                return [], []
    return [], []


class WorkspaceActivityTracker:
    """Thread-safe tracker for workspace file creation, modification, attribution, and delta scans."""

    execution_authorized: bool = False

    def __init__(
        self,
        workspace_root: Path | str,
        *,
        max_scan_files: int = MAX_WORKSPACE_SCAN_FILES,
        scan_ttl_sec: float = 2.0,
    ) -> None:
        self._lock = threading.RLock()
        self.max_scan_files = max(40, int(max_scan_files))
        self.scan_ttl_sec = max(0.0, float(scan_ttl_sec))
        self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._baseline: dict[str, dict[str, Any]] = {}
        self._baseline_captured_at: float = 0.0
        self._file_records: dict[str, dict[str, Any]] = {}
        self._mutation_events: list[dict[str, Any]] = []
        self._cached_tree_entries: list[dict[str, Any]] = []
        self._last_scan_mono: float = 0.0
        self._scanned_total_files: int = 0
        self._scan_truncated: bool = False
        self.capture_baseline(clear_history=True)

    @property
    def workspace_root(self) -> Path:
        with self._lock:
            return self._workspace_root

    def set_workspace_root(self, new_root: Path | str, *, clear_history: bool = True) -> Path:
        resolved = Path(new_root).expanduser().resolve()
        with self._lock:
            if resolved == self._workspace_root and self._baseline_captured_at > 0 and not clear_history:
                return resolved
            self._workspace_root = resolved
            self.capture_baseline(clear_history=clear_history)
            return resolved

    def _walk_safe_workspace(self, root: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], bool]:
        """Walk `root` safely up to `self.max_scan_files`, returning `(file_meta_map, tree_entries, truncated)`."""
        meta_map: dict[str, dict[str, Any]] = {}
        entries: list[dict[str, Any]] = []
        truncated = False
        if not root.is_dir():
            return meta_map, entries, truncated

        file_count = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            p_dir = Path(dirpath)
            try:
                rel_dir = "" if p_dir.resolve() == root else p_dir.resolve().relative_to(root).as_posix()
            except Exception:
                dirnames[:] = []
                continue

            kept_dirs: list[str] = []
            for d in sorted(dirnames, key=str.lower):
                rel_sub = f"{rel_dir}/{d}".lstrip("/")
                if _should_skip_dir(d, rel_sub):
                    continue
                cand_d = p_dir / d
                if cand_d.is_symlink():
                    continue
                try:
                    guard_safe_path(cand_d)
                except PathIsolationError:
                    continue
                kept_dirs.append(d)
                if len(entries) < self.max_scan_files:
                    entries.append({
                        "path": rel_sub,
                        "name": d,
                        "type": "dir",
                        "depth": rel_sub.count("/"),
                    })
            dirnames[:] = kept_dirs

            for fname in sorted(filenames, key=str.lower):
                if file_count >= self.max_scan_files:
                    truncated = True
                    break
                rel_file = f"{rel_dir}/{fname}".lstrip("/")
                if _is_blocked_rel_path(rel_file, fname):
                    continue
                cand_f = p_dir / fname
                if cand_f.is_symlink():
                    continue
                try:
                    guard_safe_path(cand_f)
                    st = cand_f.stat()
                except (OSError, PathIsolationError):
                    continue
                file_count += 1
                size = int(st.st_size)
                mtime_ns = int(st.st_mtime_ns)
                ext = cand_f.suffix.lower()
                # Compute SHA-256 only for reasonably sized files to keep baseline/delta scans sub-15ms
                sha = ""
                if size <= 524_288:
                    try:
                        sha = hashlib.sha256(cand_f.read_bytes()).hexdigest()
                    except OSError:
                        sha = ""
                meta = {
                    "path": rel_file,
                    "name": fname,
                    "size": size,
                    "mtime_ns": mtime_ns,
                    "mtime": st.st_mtime,
                    "sha256": sha,
                    "ext": ext,
                }
                meta_map[rel_file] = meta
                entries.append({
                    "path": rel_file,
                    "name": fname,
                    "type": "file",
                    "size": size,
                    "mtime": round(st.st_mtime, 2),
                    "ext": ext,
                    "depth": rel_file.count("/"),
                })
            if truncated:
                break
        return meta_map, entries, truncated

    def capture_baseline(self, *, clear_history: bool = False) -> dict[str, Any]:
        """Snapshot current workspace state as baseline for detecting subsequent CREATED / MODIFIED files."""
        with self._lock:
            root = self._workspace_root
            meta_map, entries, truncated = self._walk_safe_workspace(root)
            # Store text sample for small files in small workspaces (<= 80 files) so disk-only edits also show diffs!
            if len(meta_map) <= 80:
                for rel_p, meta in meta_map.items():
                    if meta["size"] <= MAX_TEXT_SNAPSHOT_BYTES and meta["ext"] in TEXT_INSPECT_EXTS:
                        try:
                            raw = (root / rel_p).read_bytes()
                            if b"\x00" not in raw[:2048]:
                                meta["text"] = raw.decode("utf-8", errors="replace")
                        except OSError:
                            pass
            self._baseline = meta_map
            self._baseline_captured_at = time.time()
            self._cached_tree_entries = entries
            self._scanned_total_files = len(meta_map)
            self._scan_truncated = truncated
            self._last_scan_mono = time.monotonic()
            if clear_history:
                self._file_records.clear()
                self._mutation_events.clear()
            return {
                "ok": True,
                "workspace_root": str(root),
                "baseline_files": len(meta_map),
                "truncated": truncated,
                "captured_at": self._baseline_captured_at,
                "execution_authorized": False,
            }

    def snapshot_before_write(self, rel_or_abs_path: str | Path) -> dict[str, Any]:
        """Capture pre-write state for a target file inside `workspace_root`."""
        with self._lock:
            root = self._workspace_root
        try:
            p = Path(rel_or_abs_path)
            cand = p.resolve() if p.is_absolute() else (root / str(rel_or_abs_path).lstrip("/\\")).resolve()
            if not cand.is_relative_to(root):
                return {"existed": False, "rel_posix": ""}
            rel_posix = cand.relative_to(root).as_posix()
            if _is_blocked_rel_path(rel_posix, cand.name):
                return {"existed": False, "rel_posix": rel_posix}
            guard_safe_path(cand)
            snap = _hash_and_sample_file(cand)
            snap["rel_posix"] = rel_posix
            return snap
        except Exception:
            return {"existed": False, "rel_posix": ""}

    def record_after_write(
        self,
        rel_or_abs_path: str | Path,
        before: dict[str, Any] | None = None,
        *,
        worker_id: str = "",
        worker_name: str = "",
        role: str = "",
        mission_id: str = "",
        source: str = "swarm_write",
    ) -> dict[str, Any] | None:
        """Record a post-write file mutation (`CREATED` or `MODIFIED`) with diff & agent attribution."""
        with self._lock:
            root = self._workspace_root
        try:
            p = Path(rel_or_abs_path)
            cand = p.resolve() if p.is_absolute() else (root / str(rel_or_abs_path).lstrip("/\\")).resolve()
            if not cand.is_relative_to(root) or not cand.is_file():
                return None
            rel_posix = cand.relative_to(root).as_posix()
            if _is_blocked_rel_path(rel_posix, cand.name):
                return None
            guard_safe_path(cand)
        except Exception:
            return None

        after = _hash_and_sample_file(cand)
        if not after.get("existed"):
            return None

        now = time.time()
        with self._lock:
            base_entry = self._baseline.get(rel_posix)
            prior_rec = self._file_records.get(rel_posix)
            existed_before_write = bool(before.get("existed")) if isinstance(before, dict) else bool(base_entry or prior_rec)
            old_sha = str((before or {}).get("sha256") or (prior_rec or {}).get("sha256") or (base_entry or {}).get("sha256") or "")
            old_size = int((before or {}).get("size") if isinstance(before, dict) and before.get("existed") else ((prior_rec or base_entry or {}).get("size") or 0))
            old_text = (before or {}).get("text") if isinstance(before, dict) else None
            if old_text is None and base_entry and isinstance(base_entry.get("text"), str):
                old_text = base_entry["text"]

            if not existed_before_write:
                step_op = "CREATED"
            elif old_sha and after["sha256"] and old_sha == after["sha256"] and old_size == after["size"]:
                step_op = "TOUCHED"
            else:
                step_op = "MODIFIED"

            # Net status relative to workspace baseline:
            # If the file existed in `_baseline`, net status is MODIFIED (unless unchanged).
            # If the file did not exist in `_baseline`, it was CREATED in this workspace session!
            in_baseline = rel_posix in self._baseline
            if in_baseline:
                b_sha = str(self._baseline[rel_posix].get("sha256") or "")
                b_sz = int(self._baseline[rel_posix].get("size") or 0)
                if b_sha and after["sha256"] and b_sha == after["sha256"] and b_sz == after["size"]:
                    net_status = "UNCHANGED"
                else:
                    net_status = "MODIFIED"
            else:
                net_status = "CREATED"

            lines_added, lines_removed, diff_snippet = _compute_diff_and_counts(
                rel_posix,
                old_text,
                after.get("text"),
                existed_before=existed_before_write,
            )
            revisions = int((prior_rec or {}).get("revisions") or 0) + (0 if step_op == "TOUCHED" else 1)
            eff_worker_id = str(worker_id or (prior_rec or {}).get("worker_id") or "sabrina")
            eff_worker_name = str(worker_name or (prior_rec or {}).get("worker_name") or eff_worker_id)
            eff_role = str(role or (prior_rec or {}).get("role") or "execution_specialist")
            eff_mission = str(mission_id or (prior_rec or {}).get("mission_id") or "")

            rec = {
                "path": rel_posix,
                "name": cand.name,
                "ext": cand.suffix.lower(),
                "status": net_status,
                "last_op": step_op,
                "revisions": max(1, revisions),
                "bytes_before": old_size if existed_before_write else 0,
                "size": int(after["size"]),
                "sha256_before": old_sha[:16] if old_sha else "",
                "sha256": str(after.get("sha256") or ""),
                "lines_added": lines_added,
                "lines_removed": lines_removed,
                "diff": diff_snippet,
                "worker_id": eff_worker_id,
                "worker_name": eff_worker_name,
                "role": eff_role,
                "mission_id": eff_mission,
                "source": source,
                "ts": now,
                "time_str": _clock_str(now),
                "is_image": cand.suffix.lower() in IMAGE_PREVIEW_EXTS,
            }
            # A byte-identical write is not a new revision. Keep the latest
            # actual mutation and its attribution available in the inspector.
            if step_op == "TOUCHED" and prior_rec is not None:
                return copy.deepcopy(rec)

            if net_status != "UNCHANGED":
                self._file_records[rel_posix] = rec
            elif rel_posix in self._file_records:
                self._file_records.pop(rel_posix, None)

            if step_op in ("CREATED", "MODIFIED"):
                ev = {
                    "id": f"fmut_{len(self._mutation_events) + 1}_{int(now * 1000) % 100000}",
                    "path": rel_posix,
                    "name": cand.name,
                    "op": step_op,
                    "status": net_status,
                    "size": int(after["size"]),
                    "lines_added": lines_added,
                    "lines_removed": lines_removed,
                    "worker_id": eff_worker_id,
                    "worker_name": eff_worker_name,
                    "role": eff_role,
                    "mission_id": eff_mission,
                    "source": source,
                    "ts": now,
                    "time_str": _clock_str(now),
                }
                self._mutation_events.insert(0, ev)
                if len(self._mutation_events) > 120:
                    del self._mutation_events[120:]
            # Force next snapshot to refresh tree entries immediately
            self._last_scan_mono = 0.0
            return copy.deepcopy(rec)

    def scan_delta(self, *, force: bool = False) -> dict[str, Any]:
        """Incrementally reconcile on-disk workspace state against baseline & recorded agent writes."""
        now_mono = time.monotonic()
        with self._lock:
            if not force and (now_mono - self._last_scan_mono) < self.scan_ttl_sec and self._baseline_captured_at > 0:
                return self._build_summary_locked()
            root = self._workspace_root

        current_map, tree_entries, truncated = self._walk_safe_workspace(root)
        now_ts = time.time()

        with self._lock:
            self._last_scan_mono = now_mono
            self._scanned_total_files = len(current_map)
            self._scan_truncated = truncated

            # 1. Detect newly created or modified files on disk that weren't explicitly logged yet
            for rel_p, cur in current_map.items():
                base = self._baseline.get(rel_p)
                existing_rec = self._file_records.get(rel_p)
                if base is None:
                    # File did not exist at baseline -> CREATED
                    if existing_rec is None or existing_rec.get("size") != cur["size"] or (
                        cur["sha256"] and existing_rec.get("sha256") != cur["sha256"]
                    ):
                        snap = _hash_and_sample_file(root / rel_p)
                        la, lr, d_str = _compute_diff_and_counts(rel_p, None, snap.get("text"), existed_before=False)
                        self._file_records[rel_p] = {
                            "path": rel_p,
                            "name": cur["name"],
                            "ext": cur["ext"],
                            "status": "CREATED",
                            "last_op": "CREATED" if existing_rec is None else "MODIFIED",
                            "revisions": int((existing_rec or {}).get("revisions") or 1),
                            "bytes_before": 0,
                            "size": cur["size"],
                            "sha256_before": "",
                            "sha256": snap.get("sha256") or cur["sha256"],
                            "lines_added": la,
                            "lines_removed": lr,
                            "diff": d_str,
                            "worker_id": (existing_rec or {}).get("worker_id") or "workspace",
                            "worker_name": (existing_rec or {}).get("worker_name") or "Workspace Disk",
                            "role": (existing_rec or {}).get("role") or "workspace",
                            "mission_id": (existing_rec or {}).get("mission_id") or "",
                            "source": (existing_rec or {}).get("source") or "disk_scan",
                            "ts": cur.get("mtime") or now_ts,
                            "time_str": _clock_str(cur.get("mtime") or now_ts),
                            "is_image": cur["ext"] in IMAGE_PREVIEW_EXTS,
                        }
                else:
                    # File existed at baseline -> check if modified
                    changed = False
                    if cur["size"] != base["size"]:
                        changed = True
                    elif cur["mtime_ns"] != base["mtime_ns"]:
                        if cur["sha256"] and base.get("sha256"):
                            changed = cur["sha256"] != base["sha256"]
                        else:
                            changed = True
                    if changed:
                        if existing_rec is None or existing_rec.get("size") != cur["size"] or (
                            cur["sha256"] and existing_rec.get("sha256") != cur["sha256"]
                        ):
                            snap = _hash_and_sample_file(root / rel_p)
                            la, lr, d_str = _compute_diff_and_counts(
                                rel_p,
                                base.get("text"),
                                snap.get("text"),
                                existed_before=True,
                            )
                            self._file_records[rel_p] = {
                                "path": rel_p,
                                "name": cur["name"],
                                "ext": cur["ext"],
                                "status": "MODIFIED",
                                "last_op": "MODIFIED",
                                "revisions": int((existing_rec or {}).get("revisions") or 1),
                                "bytes_before": int(base.get("size") or 0),
                                "size": cur["size"],
                                "sha256_before": str(base.get("sha256") or "")[:16],
                                "sha256": snap.get("sha256") or cur["sha256"],
                                "lines_added": la,
                                "lines_removed": lr,
                                "diff": d_str,
                                "worker_id": (existing_rec or {}).get("worker_id") or "workspace",
                                "worker_name": (existing_rec or {}).get("worker_name") or "Workspace Disk",
                                "role": (existing_rec or {}).get("role") or "workspace",
                                "mission_id": (existing_rec or {}).get("mission_id") or "",
                                "source": (existing_rec or {}).get("source") or "disk_scan",
                                "ts": cur.get("mtime") or now_ts,
                                "time_str": _clock_str(cur.get("mtime") or now_ts),
                                "is_image": cur["ext"] in IMAGE_PREVIEW_EXTS,
                            }
                    elif existing_rec is not None and not truncated:
                        # Reverted back to exact baseline
                        self._file_records.pop(rel_p, None)

            # 2. Check any explicitly tracked files outside the 450-file walk cap (e.g. inside outputs/... when at repo root)
            stale_keys: list[str] = []
            for rel_p, rec in list(self._file_records.items()):
                if rel_p in current_map:
                    continue
                cand = root / rel_p
                if not cand.is_file():
                    stale_keys.append(rel_p)
                else:
                    try:
                        st = cand.stat()
                        rec["size"] = int(st.st_size)
                    except OSError:
                        stale_keys.append(rel_p)
            for k in stale_keys:
                self._file_records.pop(k, None)

            # 3. Annotate tree entries with live status (`CREATED` | `MODIFIED` | `UNCHANGED`)
            seen_tree_paths = {e["path"] for e in tree_entries}
            annotated_tree: list[dict[str, Any]] = []
            for entry in tree_entries:
                item = dict(entry)
                if item["type"] == "file":
                    f_rec = self._file_records.get(item["path"])
                    if f_rec:
                        item["status"] = f_rec["status"]
                        item["worker_id"] = f_rec.get("worker_id", "")
                        item["worker_name"] = f_rec.get("worker_name", "")
                        item["mission_id"] = f_rec.get("mission_id", "")
                        item["lines_added"] = f_rec.get("lines_added", 0)
                        item["lines_removed"] = f_rec.get("lines_removed", 0)
                    else:
                        item["status"] = "UNCHANGED"
                annotated_tree.append(item)

            # Ensure all explicitly created/modified files appear in tree_entries even if scan was truncated
            for rel_p, f_rec in self._file_records.items():
                if rel_p not in seen_tree_paths:
                    annotated_tree.insert(0, {
                        "path": rel_p,
                        "name": f_rec["name"],
                        "type": "file",
                        "size": f_rec["size"],
                        "mtime": round(float(f_rec.get("ts") or now_ts), 2),
                        "ext": f_rec["ext"],
                        "depth": rel_p.count("/"),
                        "status": f_rec["status"],
                        "worker_id": f_rec.get("worker_id", ""),
                        "worker_name": f_rec.get("worker_name", ""),
                        "mission_id": f_rec.get("mission_id", ""),
                        "lines_added": f_rec.get("lines_added", 0),
                        "lines_removed": f_rec.get("lines_removed", 0),
                    })
            self._cached_tree_entries = annotated_tree
            return self._build_summary_locked()

    def _build_summary_locked(self) -> dict[str, Any]:
        sorted_changes = sorted(
            self._file_records.values(),
            key=lambda r: float(r.get("ts") or 0.0),
            reverse=True,
        )
        created_count = sum(1 for r in sorted_changes if r.get("status") == "CREATED")
        modified_count = sum(1 for r in sorted_changes if r.get("status") == "MODIFIED")
        # Compact payload for 1s snapshot polling (omit full diff text in high-frequency poll; fetched via inspect_file)
        compact_changes = [
            {
                "path": r["path"],
                "name": r["name"],
                "ext": r["ext"],
                "status": r["status"],
                "last_op": r.get("last_op", r["status"]),
                "revisions": r.get("revisions", 1),
                "bytes_before": r.get("bytes_before", 0),
                "size": r["size"],
                "sha256": (r.get("sha256") or "")[:12],
                "lines_added": r.get("lines_added", 0),
                "lines_removed": r.get("lines_removed", 0),
                "has_diff": bool(r.get("diff")),
                "worker_id": r.get("worker_id", ""),
                "worker_name": r.get("worker_name", ""),
                "role": r.get("role", ""),
                "mission_id": r.get("mission_id", ""),
                "source": r.get("source", ""),
                "ts": r.get("ts", 0.0),
                "time_str": r.get("time_str", ""),
                "is_image": bool(r.get("is_image")),
            }
            for r in sorted_changes[:80]
        ]
        return {
            "execution_authorized": False,
            "workspace_root": str(self._workspace_root),
            "workspace_name": self._workspace_root.name or str(self._workspace_root),
            "baseline_captured_at": self._baseline_captured_at,
            "scanned_files": self._scanned_total_files,
            "scan_truncated": self._scan_truncated,
            "created_count": created_count,
            "modified_count": modified_count,
            "total_changes": len(sorted_changes),
            "changes": compact_changes,
            "mutation_events": copy.deepcopy(self._mutation_events[:40]),
            "tree_entries": copy.deepcopy(self._cached_tree_entries[:300]),
        }

    def inspect_file(self, rel_path: str) -> dict[str, Any]:
        """Read jailed file source, unified diff, AST symbols/imports, and safe image data-URI preview."""
        clean_rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not clean_rel:
            return {"ok": False, "error": "Empty file path.", "execution_authorized": False}
        with self._lock:
            root = self._workspace_root
            rec = copy.deepcopy(self._file_records.get(clean_rel))
            base = copy.deepcopy(self._baseline.get(clean_rel))

        try:
            cand = (root / clean_rel).resolve()
            if not cand.is_relative_to(root):
                return {"ok": False, "error": f"Path escapes active workspace: {clean_rel}", "execution_authorized": False}
            rel_posix = cand.relative_to(root).as_posix()
            if _is_blocked_rel_path(rel_posix, cand.name):
                return {"ok": False, "error": f"Protected secret or database file blocked: {rel_posix}", "execution_authorized": False}
            guard_safe_path(cand)
            if not cand.is_file():
                return {"ok": False, "error": f"File not found in workspace: {rel_posix}", "execution_authorized": False}
            st = cand.stat()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

        size = int(st.st_size)
        ext = cand.suffix.lower()
        sha256_hex = ""
        content_text: str | None = None
        is_binary = False
        image_data_uri = ""

        if size <= MAX_HASH_FILE_BYTES:
            try:
                raw = cand.read_bytes()
                sha256_hex = hashlib.sha256(raw).hexdigest()
                if ext in IMAGE_PREVIEW_EXTS and size <= MAX_IMAGE_PREVIEW_BYTES and size > 0:
                    mime = IMAGE_PREVIEW_EXTS[ext]
                    b64 = base64.b64encode(raw).decode("ascii")
                    image_data_uri = f"data:{mime};base64,{b64}"
                if b"\x00" in raw[:4096] and ext != ".svg":
                    is_binary = True
                elif size <= MAX_TEXT_SNAPSHOT_BYTES:
                    content_text = raw.decode("utf-8", errors="replace")
            except OSError as exc:
                return {"ok": False, "error": f"Read failed: {exc}", "execution_authorized": False}
        else:
            is_binary = ext not in TEXT_INSPECT_EXTS

        diff_text = str((rec or {}).get("diff") or "")
        if not diff_text and rec and content_text is not None:
            _, _, diff_text = _compute_diff_and_counts(
                rel_posix,
                (base or {}).get("text"),
                content_text,
                existed_before=(rec.get("status") == "MODIFIED"),
                max_diff_chars=8000,
            )

        symbols: list[dict[str, Any]] = []
        imports: list[str] = []
        if content_text is not None:
            symbols, imports = _extract_symbols_light(content_text, ext)

        line_count = len(content_text.splitlines()) if content_text is not None else 0
        mime_guess = IMAGE_PREVIEW_EXTS.get(ext) or mimetypes.guess_type(cand.name)[0] or "application/octet-stream"

        return {
            "ok": True,
            "execution_authorized": False,
            "path": rel_posix,
            "abs_path": str(cand),
            "name": cand.name,
            "ext": ext,
            "mime_type": mime_guess,
            "size": size,
            "mtime": st.st_mtime,
            "mtime_str": _clock_str(st.st_mtime),
            "sha256": sha256_hex,
            "status": (rec or {}).get("status") or ("UNCHANGED" if base else "EXISTING"),
            "last_op": (rec or {}).get("last_op") or "READ",
            "revisions": int((rec or {}).get("revisions") or 1),
            "lines_added": int((rec or {}).get("lines_added") or 0),
            "lines_removed": int((rec or {}).get("lines_removed") or 0),
            "line_count": line_count,
            "worker_id": (rec or {}).get("worker_id") or "",
            "worker_name": (rec or {}).get("worker_name") or "",
            "role": (rec or {}).get("role") or "",
            "mission_id": (rec or {}).get("mission_id") or "",
            "is_binary": is_binary,
            "is_image": bool(image_data_uri),
            "is_html": ext in {".html", ".htm"},
            "image_data_uri": image_data_uri,
            "content": content_text[:120_000] if content_text is not None else "",
            "truncated_content": bool(content_text is not None and len(content_text) > 120_000),
            "diff": diff_text,
            "symbols": symbols,
            "imports": imports,
        }


# ==============================================================================
# Multi-Machine & Code Catalog Indexer Bridge (`lab_code_catalog`, `lab_index`,
# `sabrina_compute_federation`, `code_indexer` / `rag_knowledge`)
# ==============================================================================


def get_machine_and_catalog_snapshot(
    active_workspace_root: Path | str,
    *,
    recent_workspaces: list[str] | None = None,
    catalog_db_path: Path | None = None,
    rag_db_path: Path | None = None,
    federation_nodes_path: Path | None = None,
) -> dict[str, Any]:
    """Build a read-only summary of indexed machines, local roots, and SQLite code catalogs."""
    ws_root = Path(active_workspace_root).expanduser().resolve()
    cat_db = Path(catalog_db_path or os.environ.get("SABRINA_CODE_CATALOG") or DEFAULT_CATALOG_DB).resolve()
    r_db = Path(rag_db_path or DEFAULT_RAG_DB).resolve()
    fed_json = Path(federation_nodes_path or DEFAULT_FEDERATION_NODES_JSON).resolve()

    # 1. Local & Enrolled Compute Machines (`windows-workstation`, `ego-lab`, custom federation nodes)
    machines: list[dict[str, Any]] = [
        {
            "node_id": "windows-workstation",
            "host": platform.node() or "localhost",
            "platform": f"{platform.system()}-{platform.machine()}",
            "role": "Active Console Host & Local Workspace Sandbox",
            "status": "ONLINE",
            "roots": list(dict.fromkeys([str(ws_root), *[str(x) for x in (recent_workspaces or [])[:6] if x]])),
            "projects": [],
        }
    ]

    # Pull curated lab host infrastructure from `tools/lab_index.py`
    lab_projects: list[dict[str, Any]] = []
    try:
        from sabrina_console_desktop.tools.lab_index import LAB_INDEX
        for key, item in LAB_INDEX.items():
            lab_projects.append({
                "id": key,
                "name": item.get("name", key),
                "category": item.get("category", "project"),
                "host": item.get("host", os.environ.get("SABRINA_LAB_HOST", "localhost")),
                "root_path": item.get("root_path", ""),
                "description": item.get("description", ""),
                "containers": [c.get("name") for c in (item.get("containers") or []) if isinstance(c, dict) and c.get("name")],
                "services": [s.get("name") for s in (item.get("services") or []) if isinstance(s, dict) and s.get("name")],
            })
    except Exception:
        pass

    try:
        from sabrina_config import LAB_HOST as _CFG_LAB_HOST
    except ImportError:
        from sabrina_console_desktop.sabrina_config import LAB_HOST as _CFG_LAB_HOST  # type: ignore[no-redef]
    lab_roots = sorted({p["root_path"] for p in lab_projects if p.get("root_path")})
    machines.append({
        "node_id": "lab-node",
        "host": _CFG_LAB_HOST,
        "platform": "Linux-x86_64 (Lab Mesh)",
        "role": "Lab Compute Host · Docker Stack & Synced Worktrees",
        "status": "INDEXED",
        "roots": lab_roots,
        "projects": lab_projects,
    })

    if fed_json.is_file():
        try:
            custom_nodes = json.loads(fed_json.read_text(encoding="utf-8"))
            node_items = custom_nodes if isinstance(custom_nodes, list) else (custom_nodes.get("nodes") or [])
            for nd in node_items[:12]:
                if isinstance(nd, dict) and nd.get("node_id") not in {"windows-workstation", "ego-lab"}:
                    machines.append({
                        "node_id": str(nd.get("node_id") or nd.get("host") or "custom-node"),
                        "host": str(nd.get("host") or "unknown"),
                        "platform": str(nd.get("platform") or "enrolled-node"),
                        "role": str(nd.get("role") or "Enrolled Compute Federation Node"),
                        "status": str(nd.get("status") or "ENROLLED").upper(),
                        "roots": list(nd.get("roots") or ([nd["root_path"]] if nd.get("root_path") else [])),
                        "projects": [],
                    })
        except Exception:
            pass

    # 2. `lab_code_catalog` SQLite status
    catalog_stats: dict[str, Any] = {
        "db_path": str(cat_db),
        "initialized": cat_db.is_file(),
        "indexed_files": 0,
        "indexed_roots": [],
        "last_run_at": None,
        "last_run_str": "never",
    }
    if cat_db.is_file():
        try:
            with sqlite3.connect(str(cat_db), timeout=3) as db:
                db.row_factory = sqlite3.Row
                cnt_row = db.execute("SELECT COUNT(*) AS n FROM files").fetchone()
                catalog_stats["indexed_files"] = int(cnt_row["n"] if cnt_row else 0)
                root_rows = db.execute(
                    "SELECT root, COUNT(*) AS cnt, MAX(checked_at) AS last_ts FROM files GROUP BY root ORDER BY cnt DESC LIMIT 12"
                ).fetchall()
                catalog_stats["indexed_roots"] = [
                    {
                        "root": r["root"],
                        "files": int(r["cnt"]),
                        "checked_str": _clock_str(float(r["last_ts"])) if r["last_ts"] else "",
                    }
                    for r in root_rows
                ]
                run_row = db.execute("SELECT ended, summary FROM runs ORDER BY ended DESC LIMIT 1").fetchone()
                if run_row and run_row["ended"]:
                    catalog_stats["last_run_at"] = float(run_row["ended"])
                    catalog_stats["last_run_str"] = _clock_str(float(run_row["ended"]))
        except Exception as exc:
            catalog_stats["error"] = str(exc)[:160]

    # 3. `rag_knowledge` (`sabrina_knowledge_rag.db` -> `lab_code_index` FTS5 chunks)
    rag_stats: dict[str, Any] = {
        "db_path": str(r_db),
        "initialized": r_db.is_file(),
        "code_chunks": 0,
    }
    if r_db.is_file():
        try:
            with sqlite3.connect(str(r_db), timeout=3) as db:
                row = db.execute(
                    "SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.source_type = 'lab_code_index'"
                ).fetchone()
                rag_stats["code_chunks"] = int(row[0] if row else 0)
        except Exception:
            pass

    return {
        "ok": True,
        "execution_authorized": False,
        "active_workspace": str(ws_root),
        "machines": machines,
        "catalog": catalog_stats,
        "rag_code_index": rag_stats,
    }


def refresh_and_search_machine_catalog(
    roots: list[str | Path],
    *,
    query: str = "",
    include_rag_fts: bool = False,
    catalog_db_path: Path | None = None,
    rag_db_path: Path | None = None,
    max_seconds: int = 25,
    max_files: int = 5000,
) -> dict[str, Any]:
    """Run `tools.lab_code_catalog.refresh` (and optional FTS5 `code_indexer` / symbol search) safely."""
    cat_db = Path(catalog_db_path or os.environ.get("SABRINA_CODE_CATALOG") or DEFAULT_CATALOG_DB).resolve()
    r_db = Path(rag_db_path or DEFAULT_RAG_DB).resolve()
    safe_roots: list[str] = []
    for raw_r in roots:
        if not raw_r:
            continue
        try:
            rp = Path(raw_r).expanduser().resolve()
            guard_safe_path(rp)
            if rp.is_dir():
                safe_roots.append(str(rp))
        except Exception:
            continue

    refresh_summary: dict[str, Any] = {}
    rag_summary: dict[str, Any] = {}
    search_hits: list[dict[str, Any]] = []

    if safe_roots and not query.strip():
        try:
            from sabrina_console_desktop.tools import lab_code_catalog
            # Temporarily include web extensions (.html, .css, .svg) alongside CODE_EXTS/META_EXTS
            orig_meta = set(lab_code_catalog.META_EXTS)
            lab_code_catalog.META_EXTS = orig_meta | {".html", ".htm", ".css", ".svg", ".txt"}
            try:
                refresh_summary = lab_code_catalog.refresh(
                    safe_roots,
                    db_path=cat_db,
                    max_seconds=max_seconds,
                    max_files=max_files,
                )
            finally:
                lab_code_catalog.META_EXTS = orig_meta
        except Exception as exc:
            refresh_summary = {"error": str(exc)}

        if include_rag_fts:
            try:
                from sabrina_console_desktop.tools import code_indexer
                rag_summary = code_indexer.index_paths(
                    safe_roots,
                    db_path=r_db,
                    clear_existing=False,
                )
            except Exception as exc:
                rag_summary = {"error": str(exc)}

    clean_q = str(query or "").strip()
    if clean_q and cat_db.is_file():
        try:
            from sabrina_console_desktop.tools import lab_code_catalog
            raw_hits = lab_code_catalog.search(clean_q, limit=15, db_path=cat_db)
            for h in raw_hits:
                search_hits.append({
                    "path": h.get("path", ""),
                    "root": h.get("root", ""),
                    "size": h.get("size", 0),
                    "language": h.get("language", ""),
                    "status": h.get("status", ""),
                    "source_state": h.get("source_state", ""),
                    "provenance": h.get("provenance", ""),
                    "symbols": h.get("symbols", [])[:10],
                    "imports": h.get("imports", [])[:12],
                })
        except Exception as exc:
            refresh_summary["search_error"] = str(exc)

    snap = get_machine_and_catalog_snapshot(
        safe_roots[0] if safe_roots else REPO_ROOT,
        catalog_db_path=cat_db,
        rag_db_path=r_db,
    )
    return {
        "ok": True,
        "execution_authorized": False,
        "refreshed_roots": safe_roots,
        "refresh_summary": refresh_summary,
        "rag_summary": rag_summary,
        "query": clean_q,
        "search_hits": search_hits,
        "snapshot": snap,
    }
