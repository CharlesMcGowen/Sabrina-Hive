#!/usr/bin/env python3
"""Sabrina Autonomous Self-Repair Code Workshop.

Closed-loop 5-stage self-repair pipeline:
1. ErrorHarvester: Scans Sabrina telemetry (scheduler `last_error`, `portal_audit` /
   `portal_requests`, frontier fallback reports, and `awaiting_validation` skill proposals).
2. Protected-Boundary & Blast-Radius Filter: Extends `patch_applier.PROTECTED_CORE_FILENAMES`
   and blocks core auth/policy/transport/trading/self-modifying files.
3. Isolated Sandbox Patcher (`qwen2.5-coder:14b` Worker + up to 2 CEGAR retries):
   Synthesizes minimal exact-target edits + a fail-before/pass-after regression test.
4. Verification & Disjoint-Family Audit Gate:
   - `py_compile` syntax gate
   - Regression test MUST FAIL on unpatched file and PASS on patched sandbox copy
   - AST SAST check (zero forbidden offensive/secret patterns)
   - Disjoint-family auditor (`granite3.1-dense:8b` / `llama3.1:8b`, never Qwen-on-Qwen)
5. Atomic Promotion / Staging + Instant SHA-256 Rollback:
   Stages verified patches as `pending_approval` (or `auto_apply=True` when explicitly
   requested) with pre-apply SHA-256 conflict detection and automatic rollback on smoke failure.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import py_compile
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.patch_applier import PROTECTED_CORE_FILENAMES
from tools.sabrina_autonomous_gatekeeper import FORBIDDEN_OFFENSIVE_PATTERNS

DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
DEFAULT_SOCIAL_SCHED_DB = ROOT / "data" / "social" / "scheduler.sqlite3"
DEFAULT_PORTAL_DB = ROOT / "data" / "sabrina_assistant_fresh.sqlite3"
DEFAULT_SANDBOX_ROOT = ROOT / "artifacts" / "self_repair_sandbox"
DEFAULT_BACKUP_ROOT = ROOT / "data" / "backups" / "self_repair"

MAX_DIFF_LINES = 120
DEFAULT_OLLAMA_URL = os.environ.get("SABRINA_OLLAMA_CHAT_URL", "http://127.0.0.1:11434/api/chat")

# Extra files unconditionally protected from automated self-repair
EXTRA_PROTECTED_FILENAMES = {
    "portal_policy.py",
    "portal_transport.py",
    "sabrina_autonomous_gatekeeper.py",
    "patch_applier.py",
    "sabrina_self_repair_workshop.py",
    "secret_store.py",
    ".env",
    "portal_evaluation.py",
    "portal_swarm_pilot.py",
    "lab_control.py",
}

PROTECTED_PREFIXES = (
    "kraken_",
    "guild_web_",
)

_FORBIDDEN_SAST_RES = [re.compile(p, re.IGNORECASE) for p in FORBIDDEN_OFFENSIVE_PATTERNS]
_DANGEROUS_PATCH_PATTERNS = [
    re.compile(r"\bos\.system\s*\(", re.IGNORECASE),
    re.compile(r"\bsubprocess\.(?:run|Popen|call)\([^)]*shell\s*=\s*True", re.IGNORECASE),
    re.compile(r"(?:sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|hf_[A-Za-z0-9]{20,})"),
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def is_protected_repair_target(target_path: Path | str, *, repo_root: Path = ROOT) -> Tuple[bool, str]:
    """Return `(True, reason)` if `target_path` must not be modified by automated self-repair."""
    try:
        resolved_root = repo_root.resolve()
        p = Path(target_path)
        if not p.is_absolute():
            p = resolved_root / p
        resolved = p.resolve()
    except Exception:
        return True, "invalid_path"

    if not resolved.is_relative_to(resolved_root):
        return True, "escapes_workspace_root"
    if resolved.suffix.lower() != ".py":
        return True, "non_python_target"

    name = resolved.name.lower()
    if name in PROTECTED_CORE_FILENAMES or name in EXTRA_PROTECTED_FILENAMES:
        return True, f"protected_file:{name}"
    if any(name.startswith(pref) for pref in PROTECTED_PREFIXES):
        return True, f"protected_prefix:{name}"
    return False, "allowed"


def run_sast_on_code(code: str) -> Tuple[bool, List[str]]:
    """Static AST + pattern security check on candidate patched Python code."""
    issues: List[str] = []
    try:
        ast.parse(code)
    except SyntaxError as exc:
        return False, [f"syntax_error:line_{exc.lineno}:{exc.msg}"]
    for pat in _FORBIDDEN_SAST_RES:
        if pat.search(code):
            issues.append(f"forbidden_pattern:{pat.pattern}")
    for pat in _DANGEROUS_PATCH_PATTERNS:
        if pat.search(code):
            issues.append(f"dangerous_construct:{pat.pattern[:40]}")
    return len(issues) == 0, issues


def _compute_unified_diff(old_text: str, new_text: str, label: str) -> Tuple[str, int]:
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
            n=3,
        )
    )
    changed = sum(1 for ln in diff_lines if (ln.startswith("+") and not ln.startswith("+++")) or (ln.startswith("-") and not ln.startswith("---")))
    return "".join(diff_lines), changed


def _build_focused_code_view(orig_text: str, ticket: Dict[str, Any], *, max_chars: int = 20000) -> str:
    """Return full file text if concise, or exact verbatim line windows around ticket symbols/keywords."""
    if len(orig_text) <= max_chars:
        return orig_text

    lines = orig_text.splitlines()
    total = len(lines)
    haystack = f"{ticket.get('summary', '')} {ticket.get('evidence', '')}"
    raw_tokens: List[str] = []
    # Prioritize backtick-quoted identifiers/strings first
    raw_tokens.extend(re.findall(r"`([^`]{3,80})`", haystack))
    # Also include explicit keywords if supplied
    for kw in ticket.get("keywords") or ():
        if isinstance(kw, str) and len(kw) >= 3:
            raw_tokens.append(kw)
    # Include distinctive code-like identifiers (snake_case, slash model IDs, CamelCase)
    raw_tokens.extend(re.findall(r"[A-Za-z0-9_]+/[A-Za-z0-9_.-]+|[a-z_][a-z0-9_]{6,}", haystack))

    # Deduplicate while keeping order and excluding generic words
    stop = {"frontier", "research", "fallback", "provider", "returns", "silently", "default", "telemetry"}
    tokens: List[str] = []
    seen = set()
    for tok in raw_tokens:
        t = tok.strip()
        tl = t.lower()
        if len(t) < 4 or tl in stop or tl in seen:
            continue
        seen.add(tl)
        tokens.append(t)

    # Prioritize explicit `ticket["keywords"]` matches FIRST so large (>50KB) files
    # never drop the exact target lines behind noisy earlier matches.
    explicit_kws = [kw.strip() for kw in (ticket.get("keywords") or ()) if isinstance(kw, str) and len(kw.strip()) >= 3]
    explicit_hits: List[int] = [idx for idx, ln in enumerate(lines) if any(kw in ln for kw in explicit_kws)] if explicit_kws else []

    windows: List[Tuple[int, int]] = [(0, min(total, 20))]
    hit_indices: List[int] = explicit_hits if explicit_hits else [
        idx for idx, ln in enumerate(lines) if any(tok in ln for tok in tokens)
    ]
    for idx in hit_indices:
        windows.append((max(0, idx - 16), min(total, idx + 24)))

    # Include enclosing class header, __init__ signature, and enclosing def signature via AST
    # so both patcher and pytest author ALWAYS see exact class/method/parameter names!
    if hit_indices:
        try:
            tree = ast.parse(orig_text)
            hit_line_nums = {i + 1 for i in hit_indices}
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    n_start = getattr(node, "lineno", 0)
                    n_end = getattr(node, "end_lineno", n_start)
                    if any(n_start <= ln <= n_end for ln in hit_line_nums):
                        windows.append((max(0, n_start - 1), min(total, n_start + 12)))
                        if isinstance(node, ast.ClassDef):
                            for item in node.body:
                                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                                    i_start = item.lineno
                                    windows.append((max(0, i_start - 1), min(total, i_start + 14)))
        except Exception:
            pass

    # Merge overlapping or nearby windows
    windows.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in windows:
        if not merged or start > merged[-1][1] + 5:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    # Fallback to head + tail if no token matched
    if len(merged) <= 1:
        half = max_chars // 2
        return orig_text[:half] + "\n# ... [middle omitted] ...\n" + orig_text[-half:]

    parts: List[str] = []
    used_chars = 0
    prev_end = 0
    for start, end in merged:
        if start > prev_end:
            parts.append(f"# ... [lines {prev_end + 1}-{start} omitted] ...")
        chunk = "\n".join(lines[start:end])
        if used_chars + len(chunk) > max_chars:
            remaining = max(500, max_chars - used_chars)
            parts.append(chunk[:remaining] + "\n# ... [truncated to fit context] ...")
            break
        parts.append(chunk)
        used_chars += len(chunk)
        prev_end = end
    if prev_end < total:
        parts.append(f"# ... [lines {prev_end + 1}-{total} omitted] ...")
    return "\n".join(parts)


def _load_env_key(key_name: str, repo_root: Path = ROOT) -> str:
    val = os.environ.get(key_name, "").strip()
    if val:
        return val
    for env_path in (repo_root / ".env", repo_root.parent / ".env"):
        if env_path.is_file():
            try:
                for ln in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    ln = ln.strip()
                    if ln.startswith(f"{key_name}="):
                        v = ln.split("=", 1)[1].strip().strip('"').strip("'")
                        if v:
                            return v
            except Exception:
                pass
    return ""


def _local_nvidia_gpu_status() -> Tuple[bool, str]:
    """Check whether a local NVIDIA GPU is currently online (detects Xid 79 / driver wedge)."""
    if not sys.platform.startswith("linux"):
        return True, "non_linux_host"
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        out = ((proc.stdout or "") + " " + (proc.stderr or "")).strip()
        if proc.returncode == 0 and out and "Unable to determine" not in out and "No devices" not in out:
            return True, out.splitlines()[0].strip()
        return False, out[:200] or f"nvidia_smi_exit_{proc.returncode}"
    except FileNotFoundError:
        return False, "nvidia_smi_not_found"
    except Exception as exc:
        return False, f"nvidia_smi_error:{exc}"


def _gemini_chat(model: str, system_prompt: str, user_prompt: str, *, repo_root: Path = ROOT, max_tokens: int = 8192, temperature: float = 0.15) -> str:
    api_key = _load_env_key("GEMINI_API_KEY", repo_root=repo_root) or _load_env_key("GOOGLE_API_KEY", repo_root=repo_root)
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not configured")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        # Cap internal thoughts to 1024 tokens and give 16K output budget so Gemini 3 never truncates code/tests
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max(16384, int(max_tokens)),
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cands = data.get("candidates") or []
    parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
    return "\n".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()


def _pollinations_chat(model: str, system_prompt: str, user_prompt: str, *, temperature: float = 0.1) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }
    req = urllib.request.Request(
        "https://text.pollinations.ai/openai",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "SabrinaSelfRepairWorkshop/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str((((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "").strip()


def _ollama_chat(model: str, system_prompt: str, user_prompt: str, *, num_predict: int = 2400, temperature: float = 0.15, json_mode: bool = True) -> str:
    urls = [DEFAULT_OLLAMA_URL, "http://192.168.2.69:11434/api/chat", "http://127.0.0.1:11434/api/chat"]
    seen = set()
    ordered = [u for u in urls if u and not (u in seen or seen.add(u))]
    payload: Dict[str, Any] = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {"temperature": temperature, "num_predict": num_predict, "num_ctx": 8192},
    }
    if json_mode:
        payload["format"] = "json"
    last_err: Optional[Exception] = None
    for url in ordered:
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return str(((data.get("message") or {}).get("content")) or "").strip()
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"Ollama unreachable for {model}: {last_err}")


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    raw = re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.S).strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
    for candidate in (raw,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def _parse_search_replace_blocks(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse `<<<<<<< SEARCH[ _ALL] ... ======= ... >>>>>>> REPLACE` edit blocks."""
    cleaned = re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.S)
    summary_m = re.search(r"^\s*SUMMARY:\s*(.+)$", cleaned, flags=re.M)
    summary = summary_m.group(1).strip() if summary_m else ""
    edits: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"<<<<<<<\s*SEARCH(_ALL)?\s*\r?\n(.*?)\r?\n=======\s*\r?\n(.*?)\r?\n>>>>>>>\s*REPLACE",
        flags=re.S,
    )
    for m in pattern.finditer(cleaned):
        edits.append({
            "target": m.group(2),
            "replacement": m.group(3),
            "replace_all": bool(m.group(1)),
        })
    return summary, edits


def _extract_python_code_block(text: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>", "", (text or ""), flags=re.S).strip()
    blocks = re.findall(r"```(?:python|py)?\s*\r?\n(.*?)```", cleaned, flags=re.S)
    if blocks:
        # Pick the longest block containing `def test_` if present
        test_blocks = [b.strip() for b in blocks if "def test_" in b]
        if test_blocks:
            return max(test_blocks, key=len)
        return max((b.strip() for b in blocks), key=len)
    return re.sub(r"^```(?:python|py)?\s*\r?\n", "", cleaned).rstrip("`").strip()


def _apply_search_replace_edit(
    text: str, target: str, replacement: str, *, replace_all: bool = False
) -> Tuple[bool, str, str]:
    """Apply a SEARCH/REPLACE edit: exact verbatim match first, else unique indent-normalized block match.

    Returns `(ok, new_text, error_hint)`.
    """
    if not target:
        return False, text, "empty SEARCH target"
    if target in text:
        if replace_all:
            return True, text.replace(target, replacement), ""
        return True, text.replace(target, replacement, 1), ""

    # Unique indentation-normalized line-block match (Aider-style)
    src_lines = text.splitlines(keepends=True)
    tgt_lines = target.splitlines()
    if tgt_lines and not replace_all:
        tgt_stripped = [ln.strip() for ln in tgt_lines]
        n = len(tgt_stripped)
        matches: List[int] = []
        for i in range(0, len(src_lines) - n + 1):
            if [src_lines[i + j].strip() for j in range(n)] == tgt_stripped:
                matches.append(i)
        if len(matches) == 1:
            idx = matches[0]
            # Compute base indentation delta between file's first non-empty matched line and target's
            file_indent = ""
            tgt_indent = ""
            for j in range(n):
                if src_lines[idx + j].strip():
                    file_indent = src_lines[idx + j][: len(src_lines[idx + j]) - len(src_lines[idx + j].lstrip(" \t"))]
                    tgt_indent = tgt_lines[j][: len(tgt_lines[j]) - len(tgt_lines[j].lstrip(" \t"))]
                    break
            rep_lines = replacement.splitlines()
            reindented: List[str] = []
            for rl in rep_lines:
                if not rl.strip():
                    reindented.append("\n")
                    continue
                if tgt_indent and rl.startswith(tgt_indent):
                    reindented.append(file_indent + rl[len(tgt_indent):] + "\n")
                else:
                    reindented.append(file_indent + rl.lstrip(" \t") + "\n")
            new_lines = src_lines[:idx] + reindented + src_lines[idx + n:]
            return True, "".join(new_lines), ""

    # Build a helpful nearest-line hint ranked by rarest/strongest token overlap
    raw_toks = re.findall(r"[A-Za-z0-9_]+/[A-Za-z0-9_.-]+|\"[^\"]{4,60}\"|'[^']{4,60}'|[A-Za-z_][A-Za-z0-9_]{5,}", target)
    toks = list(dict.fromkeys(raw_toks))
    best_idx = -1
    best_score = 0.0
    if toks:
        # Down-weight common tokens; prioritize rare/exact literals (e.g. model IDs, quoted keys, function names)
        doc_freq = {t: sum(1 for ln in src_lines if t in ln) for t in toks}
        for i, ln in enumerate(src_lines):
            sc = 0.0
            for t in toks:
                df = doc_freq[t]
                if df > 0 and t in ln:
                    weight = 5.0 if ("/" in t or '"' in t or "'" in t or "_" in t) else 1.0
                    sc += weight / (1.0 + df * 0.25)
            if sc > best_score:
                best_score = sc
                best_idx = i
    hint_str = ""
    if best_idx >= 0 and best_score > 0:
        lo = max(0, best_idx - 14)
        hi = min(len(src_lines), best_idx + 12)
        block = "\n".join(f"  {k + 1}: {src_lines[k].rstrip()}" for k in range(lo, hi))
        hint_str = f"\nActual verbatim lines around line {best_idx + 1} (copy EXACTLY from here or use SEARCH_ALL):\n{block}"
    return False, text, f"SEARCH block not found verbatim: {target[:120]!r}{hint_str}"


# ==============================================================================
# Sabrina Self-Repair Workshop Engine
# ==============================================================================

class SabrinaSelfRepairWorkshop:
    """Harvests live bugs, synthesizes sandboxed patches, verifies with pytest & disjoint jury, and applies/rolls back."""

    def __init__(
        self,
        *,
        repo_root: Optional[Path | str] = None,
        db_path: Optional[Path | str] = None,
        scheduler_db_path: Optional[Path | str] = None,
        portal_db_path: Optional[Path | str] = None,
        sandbox_root: Optional[Path | str] = None,
        backup_root: Optional[Path | str] = None,
        worker_model: str = "qwen2.5-coder:14b",
        auditor_models: Sequence[str] = ("granite3.1-dense:8b", "llama3.1:8b"),
        worker_fn: Optional[Callable[[str, str], str]] = None,
        auditor_fn: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve() if repo_root else ROOT
        self.db_path = Path(db_path) if db_path else (self.repo_root / "data" / "sabrina_assistant.sqlite3")
        self.scheduler_db_path = Path(scheduler_db_path) if scheduler_db_path else (self.repo_root / "data" / "social" / "scheduler.sqlite3")
        self.portal_db_path = Path(portal_db_path) if portal_db_path else (self.repo_root / "data" / "sabrina_assistant_fresh.sqlite3")
        self.sandbox_root = Path(sandbox_root) if sandbox_root else (self.repo_root / "artifacts" / "self_repair_sandbox")
        self.backup_root = Path(backup_root) if backup_root else (self.repo_root / "data" / "backups" / "self_repair")
        self.worker_model = worker_model
        self.auditor_models = tuple(auditor_models)
        self.worker_fn = worker_fn
        self.auditor_fn = auditor_fn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS self_repair_runs (
                    run_id TEXT PRIMARY KEY,
                    bug_id TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    bug_summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    worker_model TEXT NOT NULL,
                    auditor_model TEXT DEFAULT '',
                    auditor_verdict TEXT DEFAULT '',
                    patch_id TEXT DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS self_repair_patches (
                    patch_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    bug_id TEXT NOT NULL,
                    target_rel_path TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    unified_diff TEXT NOT NULL,
                    changed_lines INTEGER NOT NULL,
                    patched_content TEXT NOT NULL,
                    regression_test_code TEXT NOT NULL,
                    before_sha256 TEXT NOT NULL,
                    after_sha256 TEXT NOT NULL,
                    worker_model TEXT NOT NULL,
                    auditor_model TEXT NOT NULL,
                    auditor_verdict TEXT NOT NULL,
                    auditor_reason TEXT NOT NULL,
                    test_output TEXT NOT NULL,
                    status TEXT NOT NULL,
                    backup_path TEXT DEFAULT '',
                    approved_by TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    # Stage 1: Error & Anomaly Harvester
    # ------------------------------------------------------------------
    def harvest_bugs(self) -> List[Dict[str, Any]]:
        """Collect actionable, deduplicated bug tickets from Sabrina's live logs and runtime files."""
        tickets: List[Dict[str, Any]] = []

        # 1. Inspect frontier_research_guild.py for silent local_ego_fallback masking & stale model IDs
        fr_path = self.repo_root / "tools" / "frontier_research_guild.py"
        if fr_path.is_file():
            fr_src = fr_path.read_text(encoding="utf-8", errors="replace")
            stale_hits = []
            if "deepseek-ai/DeepSeek-V4.1-Flash\"" in fr_src:
                stale_hits.append("Stale HF Router model ID `deepseek-ai/DeepSeek-V4.1-Flash` (live ID is `deepseek-ai/DeepSeek-V4.1-Flash-1017`)")
            if "moonshotai/Kimi-K3\"" in fr_src:
                stale_hits.append("Stale HF Router model ID `moonshotai/Kimi-K3` (live ID is `moonshotai/Kimi-K3.1-Pro`)")
            if "\"local_ego_fallback:" in fr_src or "->local_ego_fallback:" in fr_src:
                # Check if strict_external / fallback transparency is missing
                if "allow_local_fallback" not in fr_src:
                    stale_hits.append("`_call_frontier_provider_with_budget` silently falls back to local `qwen3:14b` with `ok=True` and has no `allow_local_fallback` guard")
            if stale_hits:
                tickets.append({
                    "bug_id": "bug_frontier_stale_ids_and_silent_fallback",
                    "source": "frontier_telemetry_audit",
                    "target_rel_path": "tools/frontier_research_guild.py",
                    "severity": "high",
                    "summary": "; ".join(stale_hits) + ". Update the stale model IDs everywhere in the file and add an explicit `allow_local_fallback: bool = False` parameter plus `used_local_fallback: bool` flag in `_call_frontier_provider_with_budget` so external failures are not silently masked as `ok=True` unless `allow_local_fallback=True`.",
                    "evidence": "Direct non-HF audit observed HTTP 404 on stale model IDs and silent ok=True local_ego_fallback masking.",
                    "keywords": [
                        "DeepSeek-V4.1-Flash",
                        "Kimi-K3",
                        "_call_frontier_provider_with_budget",
                        "local_ego_fallback",
                    ],
                    "existing_tests": [],
                })

        # 1b. Inspect universal_rsi_reflector.py for unquoted YAML frontmatter description bug
        rsi_path = self.repo_root / "tools" / "universal_rsi_reflector.py"
        if rsi_path.is_file():
            rsi_src = rsi_path.read_text(encoding="utf-8", errors="replace")
            if "f\"description: Verified zero-auth {item['resource_kind']} reference for {item['title'][:90]}" in rsi_src:
                tickets.append({
                    "bug_id": "bug_rsi_unquoted_yaml_frontmatter",
                    "source": "gatekeeper_yaml_audit",
                    "target_rel_path": "tools/universal_rsi_reflector.py",
                    "severity": "high",
                    "summary": "Unquoted YAML `description:` frontmatter in `tools/universal_rsi_reflector.py` breaks `yaml.safe_load` whenever `item['title']` contains a colon (`: `, e.g. `Dataset: meta-math/MetaMathQA` or `Repo: SWE-agent/SWE-agent`), causing 75% of web-resource skill proposals to fail gatekeeper validation. JSON-encode or YAML-quote the `description` value (e.g. via `json.dumps(...)`) so colons and quotes always parse cleanly with `yaml.safe_load`.",
                    "evidence": "AutonomousSecurityGatekeeper rejected 15/20 awaiting_validation proposals with `YAML frontmatter parse error: mapping values are not allowed here`.",
                    "keywords": [
                        "Verified zero-auth",
                        "internet_resource_indexer",
                        "description: Verified",
                    ],
                    "existing_tests": [],
                })

        # 2. Scan scheduler DB for jobs with `last_error`
        if self.scheduler_db_path.is_file():
            try:
                with sqlite3.connect(str(self.scheduler_db_path), timeout=10) as sconn:
                    sconn.row_factory = sqlite3.Row
                    rows = sconn.execute(
                        """
                        SELECT id, capability, status, last_error, updated
                        FROM scheduled_jobs
                        WHERE last_error IS NOT NULL AND TRIM(last_error) != ''
                        ORDER BY updated DESC LIMIT 10
                        """
                    ).fetchall()
                    for r in rows:
                        err = str(r["last_error"])
                        m = re.search(r"([a-zA-Z0-9_/\\-]+\.py)", err)
                        rel = m.group(1).replace("\\", "/") if m else ""
                        if rel and not (self.repo_root / rel).is_file():
                            rel = ""
                        tickets.append({
                            "bug_id": f"bug_sched_{r['capability'].replace('.', '_')}_{hashlib.sha256(err.encode()).hexdigest()[:8]}",
                            "source": "scheduler_last_error",
                            "target_rel_path": rel,
                            "severity": "medium",
                            "summary": f"Scheduler capability `{r['capability']}` failed: {err[:180]}",
                            "evidence": err[:500],
                            "existing_tests": [],
                        })
            except Exception:
                pass

        # 3. Check stuck `awaiting_validation` skill proposals in assistant DB
        try:
            with self._connect() as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_skill_proposals'").fetchone():
                    cnt = conn.execute(
                        "SELECT COUNT(*) FROM assistant_skill_proposals WHERE status='awaiting_validation'"
                    ).fetchone()[0]
                    if cnt > 0:
                        tickets.append({
                            "bug_id": "ops_awaiting_validation_skill_backlog",
                            "source": "skill_proposal_queue",
                            "target_rel_path": "",
                            "severity": "low",
                            "summary": f"{cnt} skill proposals are stuck in `awaiting_validation` awaiting gatekeeper audit.",
                            "evidence": f"assistant_skill_proposals awaiting_validation={cnt}",
                            "action_hint": "drain_skill_proposals",
                            "existing_tests": [],
                        })
        except Exception:
            pass

        # 4. Check local NVIDIA GPU health on Linux hosts (surfaces Xid 79 / driver wedge)
        gpu_ok, gpu_info = _local_nvidia_gpu_status()
        if not gpu_ok:
            tickets.append({
                "bug_id": "hw_local_gpu_offline",
                "source": "nvidia_smi_health",
                "target_rel_path": "",
                "severity": "critical",
                "summary": f"Local NVIDIA GPU offline ({gpu_info}). Self-Repair Workshop automatically failing over to external Gemini/Pollinations disjoint families until host GPU is recovered.",
                "evidence": gpu_info,
                "action_hint": "recover_gpu_reboot_or_reset",
                "existing_tests": [],
            })

        return tickets

    # ------------------------------------------------------------------
    # Stage 2-4: Isolated Sandbox Patching, Fail-Before/Pass-After Test & Disjoint Audit
    # ------------------------------------------------------------------
    @staticmethod
    def _model_family(model_id: str) -> str:
        low = (model_id or "").lower()
        for fam, keys in (
            ("qwen", ("qwen",)),
            ("google", ("gemini", "google", "gemma")),
            ("granite", ("granite",)),
            ("llama", ("llama",)),
            ("openai", ("pollinations", "openai", "gpt")),
            ("mistral", ("mistral", "dolphin")),
        ):
            if any(k in low for k in keys):
                return fam
        return low.split(":")[0].split("-")[0]

    def _invoke_worker(self, system_prompt: str, user_prompt: str, *, json_mode: bool = False, num_predict: int = 1800, temperature: float = 0.1) -> str:
        if self.worker_fn is not None:
            self._last_worker_model = self.worker_model
            return self.worker_fn(system_prompt, user_prompt)
        gpu_ok, _ = _local_nvidia_gpu_status()
        if gpu_ok:
            try:
                self._last_worker_model = self.worker_model
                return _ollama_chat(self.worker_model, system_prompt, user_prompt, num_predict=num_predict, temperature=temperature, json_mode=json_mode)
            except Exception:
                pass
        # GPU offline (e.g. Xid 79) or Ollama unavailable: fail over to live external Gemini / Pollinations
        errors: List[str] = []
        for gmod in ("gemini-3-flash-preview", "gemini-3.5-flash"):
            try:
                self._last_worker_model = f"google:{gmod}"
                return _gemini_chat(gmod, system_prompt, user_prompt, repo_root=self.repo_root, max_tokens=max(2000, num_predict), temperature=temperature)
            except Exception as exc:
                errors.append(f"{gmod}:{exc}")
        try:
            self._last_worker_model = "pollinations:openai-fast"
            return _pollinations_chat("openai-fast", system_prompt, user_prompt, temperature=temperature)
        except Exception as exc:
            errors.append(f"pollinations:{exc}")
        # Final fallback: try local Ollama anyway
        try:
            self._last_worker_model = self.worker_model
            return _ollama_chat(self.worker_model, system_prompt, user_prompt, num_predict=num_predict, temperature=temperature, json_mode=json_mode)
        except Exception as exc:
            errors.append(f"ollama:{exc}")
        raise RuntimeError("All worker backends failed: " + "; ".join(errors))

    def _invoke_disjoint_auditor(
        self, diff_text: str, ticket: Dict[str, Any], test_output: str, *, worker_model_used: Optional[str] = None
    ) -> Tuple[str, str, str]:
        """Audit candidate diff using a genuinely disjoint model family (never same family as worker)."""
        sys_p = (
            "You are an independent disjoint-family security and correctness auditor. "
            "Review the unified diff and verification transcript. Reject any diff that introduces "
            "security flaws, breaks existing callers, silently masks errors, or exceeds the stated bug scope. "
            'Return JSON ONLY: {"decision": "APPROVE" or "REJECT", "reason": "concise justification"}.'
        )
        user_p = (
            f"BUG TICKET:\n{json.dumps(ticket, indent=2)}\n\n"
            f"UNIFIED DIFF:\n{diff_text}\n\n"
            f"VERIFICATION TRANSCRIPT:\n{test_output[:1800]}"
        )
        if self.auditor_fn is not None:
            raw = self.auditor_fn(sys_p, user_p)
            parsed = _extract_json_object(raw) or {}
            dec = str(parsed.get("decision") or "REJECT").upper()
            return "injected-disjoint-auditor", ("APPROVE" if dec == "APPROVE" else "REJECT"), str(parsed.get("reason") or raw[:240])

        worker_fam = self._model_family(worker_model_used or getattr(self, "_last_worker_model", self.worker_model))
        gpu_ok, _ = _local_nvidia_gpu_status()

        # Build prioritized disjoint candidate list based on live GPU availability
        candidates: List[Tuple[str, str]] = []
        if gpu_ok:
            for amod in self.auditor_models:
                candidates.append(("ollama", amod))
            candidates.append(("win_ollama", "windows-rx5700xt:granite3-dense:8b"))
            candidates.append(("pollinations", "pollinations:openai-fast"))
            candidates.append(("gemini", "google:gemini-3-flash-preview"))
        else:
            # Prefer federated Windows RX 5700 XT GPU (:11435) + disjoint external models when ego GPU is offline!
            candidates.append(("win_ollama", "windows-rx5700xt:granite3-dense:8b"))
            candidates.append(("pollinations", "pollinations:openai-fast"))
            candidates.append(("gemini", "google:gemini-3-flash-preview"))
            for amod in self.auditor_models:
                candidates.append(("ollama", amod))

        for backend, amod in candidates:
            if self._model_family(amod) == worker_fam:
                continue  # Enforce disjoint-family rule strictly!
            try:
                if backend == "ollama":
                    raw = _ollama_chat(amod, sys_p, user_p, num_predict=500, temperature=0.1, json_mode=True)
                elif backend == "win_ollama":
                    wmod = amod.split(":", 1)[1]
                    wurl = "http://127.0.0.1:11434/api/chat" if sys.platform.startswith("win") else "http://127.0.0.1:11435/api/chat"
                    wreq = urllib.request.Request(
                        wurl,
                        data=json.dumps({
                            "model": wmod,
                            "stream": False,
                            "format": "json",
                            "messages": [{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}],
                            "options": {"temperature": 0.1, "num_predict": 450},
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(wreq, timeout=35) as wresp:
                        raw = str(((json.loads(wresp.read().decode("utf-8")).get("message") or {}).get("content")) or "").strip()
                elif backend == "pollinations":
                    raw = _pollinations_chat("openai-fast", sys_p, user_p, temperature=0.1)
                else:
                    raw = _gemini_chat(amod.split(":", 1)[1], sys_p, user_p, repo_root=self.repo_root, max_tokens=500, temperature=0.1)
                parsed = _extract_json_object(raw) or {}
                dec = str(parsed.get("decision") or "REJECT").upper()
                if dec in ("APPROVE", "REJECT"):
                    return amod, dec, str(parsed.get("reason") or raw[:240])
            except Exception:
                continue
        return "none", "REJECT", "no_disjoint_auditor_available"

    def _run_pytest_on_sandbox(self, sandbox_dir: Path, test_file: Path, extra_tests: Sequence[str] = ()) -> Tuple[bool, str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{sandbox_dir}{os.pathsep}{self.repo_root}"
        cmd = [
            sys.executable, "-m", "pytest",
            "-p", "no:asyncio",
            "-W", "ignore",
            "--tb=short", "-q",
            str(test_file),
            *[str(self.repo_root / t) for t in extra_tests if (self.repo_root / t).is_file()],
        ]
        proc = subprocess.run(cmd, cwd=str(self.repo_root), env=env, capture_output=True, text=True, timeout=90)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        out = stdout if stdout else stderr
        if stdout and stderr and "PytestDeprecationWarning" not in stderr:
            out = stdout + "\n" + stderr
        return proc.returncode == 0, out[-2500:]

    def repair_ticket(
        self,
        ticket: Dict[str, Any],
        *,
        auto_apply: bool = True,
        max_retries: int = 4,
    ) -> Dict[str, Any]:
        """Run one isolated sandbox self-repair cycle for a specific `BugTicket`."""
        run_id = f"sr_{int(time.time())}_{hashlib.sha256(str(ticket.get('bug_id','')).encode()).hexdigest()[:6]}"
        rel_path = str(ticket.get("target_rel_path") or "").strip().replace("\\", "/")
        bug_id = str(ticket.get("bug_id") or run_id)

        if not rel_path:
            return {"ok": False, "run_id": run_id, "status": "skipped", "reason": "no_target_python_file"}

        protected, prot_reason = is_protected_repair_target(rel_path, repo_root=self.repo_root)
        if protected:
            self._record_run(run_id, bug_id, rel_path, str(ticket.get("summary", "")), "blocked_protected", 0, "", "", "", {"reason": prot_reason})
            return {"ok": False, "run_id": run_id, "status": "blocked_protected", "reason": prot_reason}

        src_file = (self.repo_root / rel_path).resolve()
        if not src_file.is_file():
            return {"ok": False, "run_id": run_id, "status": "error", "reason": f"target_not_found:{rel_path}"}

        orig_text = src_file.read_text(encoding="utf-8")
        before_sha = _sha256_file(src_file)
        sandbox_dir = self.sandbox_root / run_id
        sandbox_target = sandbox_dir / rel_path
        sandbox_target.parent.mkdir(parents=True, exist_ok=True)
        # Create overlay package __init__.py stubs so sandbox shadows ONLY the target
        # module while untouched sibling modules still resolve from `self.repo_root`.
        cur_p = sandbox_target.parent
        while cur_p != sandbox_dir and cur_p.is_relative_to(sandbox_dir):
            rel_pkg = cur_p.relative_to(sandbox_dir)
            orig_pkg_dir = (self.repo_root / rel_pkg).resolve()
            orig_init = orig_pkg_dir / "__init__.py"
            init_text = orig_init.read_text(encoding="utf-8", errors="replace") if orig_init.is_file() else ""
            overlay_snippet = (
                f"# Sabrina Self-Repair sandbox overlay (must run BEFORE package __init__.py imports siblings)\n"
                f"if {str(orig_pkg_dir)!r} not in __path__:\n"
                f"    __path__.append({str(orig_pkg_dir)!r})\n\n"
            )
            # Keep any leading `from __future__ import ...` lines at the top of __init__.py
            init_lines = init_text.splitlines(keepends=True)
            future_idx = 0
            for idx_l, ln_l in enumerate(init_lines):
                st_l = ln_l.strip()
                if not st_l or st_l.startswith("#") or st_l.startswith("from __future__ import "):
                    future_idx = idx_l + 1
                else:
                    break
            merged_init = "".join(init_lines[:future_idx]) + overlay_snippet + "".join(init_lines[future_idx:])
            (cur_p / "__init__.py").write_text(merged_init, encoding="utf-8")
            cur_p = cur_p.parent

        feedback_history: List[str] = []
        last_error = "no_valid_patch"
        attempts = 0
        used_worker_model = self.worker_model
        code_view = _build_focused_code_view(orig_text, ticket, max_chars=18000)
        module_import = rel_path[:-3].replace("/", ".")

        if self.worker_fn is None:
            # ------------------------------------------------------------------
            # Live 2-Phase Escape-Free Closed CEGAR Loop (up to 2 outer rounds):
            # Phase 1: Surgical SEARCH/REPLACE patch blocks -> AST SAST + py_compile
            # Phase 2: Dedicated pytest reproduction file (sees diff + patched code view)
            #          -> Fail-Before / Pass-After; failures feed back to Phase 1!
            # ------------------------------------------------------------------
            patch_sys = (
                "You are Sabrina's Self-Repair Code Workshop patcher. Produce minimal, surgical Python edits "
                "using exact SEARCH/REPLACE blocks (NO JSON escaping needed).\n"
                "Rules:\n"
                "1. Output one `SUMMARY: <one-line summary>` line followed by ALL required blocks:\n"
                "<<<<<<< SEARCH\n<exact verbatim lines copied from CURRENT FILE SOURCE>\n=======\n<replacement lines>\n>>>>>>> REPLACE\n"
                "(Use `<<<<<<< SEARCH_ALL` instead of `<<<<<<< SEARCH` to replace every occurrence of a literal in the file.)\n"
                "2. Address EVERY requirement in BUG SUMMARY completely — every stale literal occurrence AND every parameter/guard/return-key change.\n"
                "3. When adding a new defaulted parameter to a Python function signature, ALWAYS place it AFTER existing defaulted parameters (e.g. after `db_path: Path = DEFAULT_DB,`), NEVER before a non-default parameter.\n"
                "4. Keep existing callers backward-compatible and stay strictly within the bug scope."
            )
            test_sys = (
                "You are Sabrina's Self-Repair pytest author. Write ONE concise, self-contained Python pytest file "
                "inside a single ```python ... ``` block that:\n"
                "1. Explicitly imports EVERY module/symbol it uses (e.g., `import json`, `import inspect`, `from pathlib import Path`, and `import " + module_import + " as mod`).\n"
                "2. Uses `tmp_path` and `monkeypatch` ONLY — NEVER makes real HTTP/network or Ollama calls, and ALWAYS uses the exact parameter names shown in PATCHED CODE CONTEXT.\n"
                "3. FAILS with AssertionError or TypeError on the UNPATCHED file and PASSES cleanly on the PATCHED file.\n"
                "4. Directly verifies every behavioral change shown in VERIFIED UNIFIED DIFF."
            )

            patch_fb: List[str] = []
            test_fb: List[str] = []
            patched_text: Optional[str] = None
            diff_text = ""
            changed_lines = 0
            patch_summary = str(ticket.get("summary") or "")
            reg_test_code = ""
            patched_out = ""
            test_verified = False

            for outer_round in range(1, 3):
                patched_text = None
                for p_att in range(1, max(2, int(max_retries) - 1) + 1):
                    attempts += 1
                    fb_txt = "\n\n".join(f"[Patch Feedback {i}] {m}" for i, m in enumerate(patch_fb[-2:], start=max(1, len(patch_fb) - 1)))
                    patch_user = (
                        f"TARGET FILE: {rel_path}\n"
                        f"BUG SUMMARY: {ticket.get('summary')}\n"
                        f"EVIDENCE: {ticket.get('evidence')}\n"
                        + (f"\nPREVIOUS ERRORS (FIX ALL OF THESE):\n{fb_txt}\n" if fb_txt else "")
                        + f"\nCURRENT FILE SOURCE:\n```python\n{code_view}\n```"
                    )
                    try:
                        raw_patch = self._invoke_worker(patch_sys, patch_user, json_mode=False, num_predict=2000, temperature=min(0.35, 0.1 + 0.08 * (p_att - 1)))
                    except Exception as exc:
                        last_error = f"worker_error:{exc}"
                        break

                    sr_summary, sr_edits = _parse_search_replace_blocks(raw_patch)
                    if not sr_edits:
                        obj = _extract_json_object(raw_patch)
                        if obj and isinstance(obj.get("edits"), list):
                            sr_summary = str(obj.get("summary") or "")
                            sr_edits = obj["edits"]
                    if not sr_edits:
                        patch_fb.append("No valid `<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE` blocks found.")
                        last_error = "no_search_replace_blocks"
                        continue

                    cand_text = orig_text
                    apply_ok = True
                    for ed in sr_edits:
                        tgt = str(ed.get("target") or "")
                        rep = str(ed.get("replacement") or "")
                        ok_ed, cand_text, ed_err = _apply_search_replace_edit(
                            cand_text, tgt, rep, replace_all=bool(ed.get("replace_all", False))
                        )
                        if not ok_ed:
                            apply_ok = False
                            patch_fb.append(f"In {rel_path}: {ed_err}")
                            break
                    if not apply_ok or cand_text == orig_text:
                        last_error = patch_fb[-1] if patch_fb else "no_op_patch"
                        continue

                    cand_diff, cand_changed = _compute_unified_diff(orig_text, cand_text, rel_path)
                    if cand_changed > MAX_DIFF_LINES:
                        patch_fb.append(f"Diff too large ({cand_changed} > {MAX_DIFF_LINES} changed lines). Keep fix minimal.")
                        last_error = "diff_exceeds_limit"
                        continue

                    sast_ok, sast_issues = run_sast_on_code(cand_text)
                    if not sast_ok:
                        snippet_hint = ""
                        for iss in sast_issues:
                            m_ln = re.search(r"syntax_error:line_(\d+):", iss)
                            if m_ln:
                                ln_no = int(m_ln.group(1))
                                p_lines = cand_text.splitlines()
                                lo = max(0, ln_no - 5)
                                hi = min(len(p_lines), ln_no + 7)
                                numbered = "\n".join(f"{i+1}: {p_lines[i]}" for i in range(lo, hi))
                                snippet_hint = f"\nBroken lines around line {ln_no}:\n```python\n{numbered}\n```"
                                break
                        patch_fb.append(f"SAST/syntax failed: {sast_issues}{snippet_hint}")
                        last_error = f"sast_failed:{sast_issues}"
                        continue

                    sandbox_target.write_text(cand_text, encoding="utf-8")
                    try:
                        py_compile.compile(str(sandbox_target), doraise=True)
                    except py_compile.PyCompileError as exc:
                        patch_fb.append(f"py_compile failed: {exc}")
                        last_error = "py_compile_failed"
                        continue

                    patched_text = cand_text
                    diff_text = cand_diff
                    changed_lines = cand_changed
                    if sr_summary:
                        patch_summary = sr_summary
                    break

                if patched_text is None:
                    break

                # Phase 2: Dedicated regression test synthesis against EXACT unified diff + patched code context
                patched_view = _build_focused_code_view(patched_text, ticket, max_chars=12000)
                test_fb = []
                for t_att in range(1, 4):
                    fb_txt = "\n\n".join(f"[Test Attempt {i}] {m}" for i, m in enumerate(test_fb[-2:], start=max(1, len(test_fb) - 1)))
                    test_user = (
                        f"TARGET MODULE: {module_import} ({rel_path})\n"
                        f"BUG SUMMARY: {ticket.get('summary')}\n\n"
                        f"VERIFIED UNIFIED DIFF:\n```diff\n{diff_text}\n```\n\n"
                        f"PATCHED CODE CONTEXT (use EXACT function signatures & parameter names from here):\n```python\n{patched_view}\n```\n"
                        + (f"\nPREVIOUS TEST ERRORS (FIX THESE):\n{fb_txt}\n" if fb_txt else "")
                        + "\nOutput ONLY one complete ```python ... ``` pytest file."
                    )
                    try:
                        raw_test = self._invoke_worker(test_sys, test_user, json_mode=False, num_predict=1500, temperature=min(0.3, 0.1 + 0.1 * (t_att - 1)))
                    except Exception as exc:
                        last_error = f"worker_test_error:{exc}"
                        break

                    cand_test = _extract_python_code_block(raw_test)
                    if "def test_" not in cand_test:
                        test_fb.append("Output must contain at least one `def test_...` function.")
                        last_error = "missing_regression_test"
                        continue
                    try:
                        ast.parse(cand_test)
                    except SyntaxError as exc:
                        test_fb.append(f"SyntaxError in test line {exc.lineno}: {exc.msg}")
                        last_error = "regression_test_syntax_error"
                        continue

                    test_file = sandbox_dir / "test_regression_repro.py"
                    test_file.write_text(cand_test, encoding="utf-8")

                    # Gate A: Must FAIL on unpatched file (and NOT via NameError/ImportError/SyntaxError)
                    sandbox_target.write_text(orig_text, encoding="utf-8")
                    unpatched_ok, unpatched_out = self._run_pytest_on_sandbox(sandbox_dir, test_file)
                    if unpatched_ok:
                        test_fb.append("Test PASSED on unpatched file — it must FAIL before the patch to prove reproduction.")
                        last_error = "test_did_not_reproduce_bug"
                        continue
                    if re.search(r"\b(?:NameError|SyntaxError|IndentationError|ModuleNotFoundError|ImportError)\b", unpatched_out):
                        test_fb.append(f"Test crashed on unpatched code due to missing import or name:\n{unpatched_out[-900:]}")
                        last_error = "regression_test_broken_imports"
                        continue

                    # Gate B: Must PASS on patched file + existing tests
                    sandbox_target.write_text(patched_text, encoding="utf-8")
                    patched_ok, patched_out = self._run_pytest_on_sandbox(
                        sandbox_dir, test_file, extra_tests=ticket.get("existing_tests") or ()
                    )
                    if not patched_ok:
                        test_fb.append(f"Test failed on patched file:\n{patched_out[-1000:]}")
                        last_error = "patched_pytest_failed"
                        continue

                    reg_test_code = cand_test
                    test_verified = True
                    break

                if test_verified:
                    break
                # Feed Phase 2 failure back to Phase 1 for Round 2 in case the patch itself was incomplete!
                patch_fb.append(
                    f"Round {outer_round} patch passed py_compile (diff={changed_lines} lines) but failed regression verification ({last_error}):\n"
                    + (test_fb[-1] if test_fb else "")
                    + "\nMake sure your SEARCH/REPLACE blocks implement EVERY requirement in BUG SUMMARY completely."
                )

            if patched_text is None:
                self._record_run(run_id, bug_id, rel_path, str(ticket.get("summary", "")), "failed", attempts, "", "", "", {"error": last_error, "phase": "patch", "feedback": patch_fb[-3:]})
                return {"ok": False, "run_id": run_id, "status": "failed", "attempts": attempts, "error": last_error}

            if not test_verified:
                self._record_run(run_id, bug_id, rel_path, str(ticket.get("summary", "")), "failed", attempts, "", "", "", {"error": last_error, "phase": "test", "diff_lines": changed_lines, "feedback": test_fb[-3:]})
                return {"ok": False, "run_id": run_id, "status": "failed", "attempts": attempts, "error": last_error}

            # Gate C: Disjoint-family security & correctness audit
            used_worker_model = getattr(self, "_last_worker_model", self.worker_model)
            auditor_model, verdict, auditor_reason = self._invoke_disjoint_auditor(
                diff_text, ticket, patched_out, worker_model_used=used_worker_model
            )
            patch_id = f"srp_{int(time.time())}_{hashlib.sha256(diff_text.encode()).hexdigest()[:8]}"
            after_sha = _sha256_bytes(patched_text.encode("utf-8"))
            now = time.time()

            if verdict != "APPROVE":
                last_error = f"auditor_rejected:{auditor_reason}"
                self._record_run(run_id, bug_id, rel_path, str(ticket.get("summary", "")), "rejected_by_auditor", attempts, auditor_model, verdict, "", {"reason": auditor_reason})
                return {
                    "ok": False,
                    "run_id": run_id,
                    "status": "rejected_by_auditor",
                    "auditor_model": auditor_model,
                    "auditor_verdict": verdict,
                    "auditor_reason": auditor_reason,
                }

            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO self_repair_patches (
                        patch_id, run_id, bug_id, target_rel_path, summary, unified_diff,
                        changed_lines, patched_content, regression_test_code, before_sha256,
                        after_sha256, worker_model, auditor_model, auditor_verdict,
                        auditor_reason, test_output, status, backup_path, approved_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', '', '', ?, ?)
                    """,
                    (
                        patch_id,
                        run_id,
                        bug_id,
                        rel_path,
                        patch_summary,
                        diff_text,
                        changed_lines,
                        patched_text,
                        reg_test_code,
                        before_sha,
                        after_sha,
                        used_worker_model,
                        auditor_model,
                        verdict,
                        auditor_reason,
                        patched_out,
                        now,
                        now,
                    ),
                )

            final_status = "pending_approval"
            apply_res = None
            if auto_apply:
                apply_res = self.apply_patch(patch_id, approver="sabrina-auto-verified")
                final_status = apply_res.get("status", "pending_approval")

            self._record_run(
                run_id,
                bug_id,
                rel_path,
                str(ticket.get("summary", "")),
                final_status,
                attempts,
                auditor_model,
                verdict,
                patch_id,
                {"changed_lines": changed_lines, "auditor_reason": auditor_reason},
            )
            return {
                "ok": True,
                "run_id": run_id,
                "patch_id": patch_id,
                "status": final_status,
                "target_rel_path": rel_path,
                "changed_lines": changed_lines,
                "worker_model": used_worker_model,
                "auditor_model": auditor_model,
                "auditor_verdict": verdict,
                "auditor_reason": auditor_reason,
                "unified_diff": diff_text,
                "apply_result": apply_res,
            }

        # Injected worker_fn single-pass compatibility path (used by deterministic unit tests)
        for attempt in range(1, max(1, int(max_retries)) + 1):
            attempts = attempt
            sys_p = (
                "You are Sabrina's Self-Repair Code Workshop worker. Produce a minimal, surgical Python bug fix "
                "plus a self-contained pytest regression test that FAILS on the unpatched file and PASSES on the patched file. "
                'Return JSON ONLY with exact keys: {"summary": "...", "edits": [{"target": "exact substring from file", "replacement": "replacement substring", "replace_all": false}], '
                '"regression_test": "complete python pytest file content"}.'
            )
            fb_block = "\n\n".join(f"[Attempt {i}] {msg}" for i, msg in enumerate(feedback_history[-2:], start=max(1, len(feedback_history) - 1)))
            user_p = (
                f"TARGET FILE: {rel_path}\n"
                f"BUG SUMMARY: {ticket.get('summary')}\n"
                f"EVIDENCE: {ticket.get('evidence')}\n"
                + (f"\nPREVIOUS ATTEMPTS FEEDBACK (FIX ALL OF THESE):\n{fb_block}\n" if fb_block else "")
                + f"\nCURRENT FILE SOURCE:\n```python\n{code_view}\n```"
            )
            try:
                raw_reply = self._invoke_worker(sys_p, user_p, json_mode=True)
            except Exception as exc:
                last_error = f"worker_error:{exc}"
                break

            proposal = _extract_json_object(raw_reply)
            if not proposal or not isinstance(proposal.get("edits"), list) or not proposal["edits"]:
                feedback_history.append("Response was not valid JSON with non-empty `edits` list and `regression_test` string.")
                last_error = "malformed_worker_json"
                continue

            patched_text = orig_text
            apply_ok = True
            for ed in proposal["edits"]:
                if not isinstance(ed, dict) or not ed.get("target"):
                    apply_ok = False
                    feedback_history.append("Each edit must have non-empty `target` and `replacement` strings.")
                    break
                tgt = str(ed["target"])
                rep = str(ed.get("replacement", ""))
                if tgt not in patched_text:
                    apply_ok = False
                    feedback_history.append(f"Edit target not found verbatim in {rel_path}: {tgt[:100]!r}")
                    break
                if bool(ed.get("replace_all", False)):
                    patched_text = patched_text.replace(tgt, rep)
                else:
                    patched_text = patched_text.replace(tgt, rep, 1)
            if not apply_ok or patched_text == orig_text:
                last_error = (feedback_history[-1] if feedback_history else "no_op_patch")
                continue

            diff_text, changed_lines = _compute_unified_diff(orig_text, patched_text, rel_path)
            if changed_lines > MAX_DIFF_LINES:
                feedback_history.append(f"Diff too large ({changed_lines} > {MAX_DIFF_LINES} changed lines). Keep fix surgical.")
                last_error = "diff_exceeds_limit"
                continue

            sast_ok, sast_issues = run_sast_on_code(patched_text)
            if not sast_ok:
                snippet_hint = ""
                for iss in sast_issues:
                    m_ln = re.search(r"syntax_error:line_(\d+):", iss)
                    if m_ln:
                        ln_no = int(m_ln.group(1))
                        p_lines = patched_text.splitlines()
                        lo = max(0, ln_no - 5)
                        hi = min(len(p_lines), ln_no + 7)
                        numbered = "\n".join(f"{i+1}: {p_lines[i]}" for i in range(lo, hi))
                        snippet_hint = f"\nBroken lines around line {ln_no} in patched file:\n```python\n{numbered}\n```"
                        break
                feedback_history.append(f"SAST / syntax check failed: {sast_issues}{snippet_hint}")
                last_error = f"sast_failed:{sast_issues}"
                continue

            reg_test_code = str(proposal.get("regression_test") or "").strip()
            if "def test_" not in reg_test_code:
                feedback_history.append("Must provide a `regression_test` containing at least one `def test_...` function.")
                last_error = "missing_regression_test"
                continue

            try:
                ast.parse(reg_test_code)
            except SyntaxError as exc:
                feedback_history.append(f"regression_test has SyntaxError on line {exc.lineno}: {exc.msg}")
                last_error = "regression_test_syntax_error"
                continue

            test_file = sandbox_dir / "test_regression_repro.py"
            test_file.write_text(reg_test_code, encoding="utf-8")

            # Gate A: Confirm regression test FAILS on unpatched file (and NOT due to broken imports/NameError!)
            sandbox_target.write_text(orig_text, encoding="utf-8")
            unpatched_ok, unpatched_out = self._run_pytest_on_sandbox(sandbox_dir, test_file)
            if unpatched_ok:
                feedback_history.append("Regression test PASSED on the UNPATCHED file — it must FAIL before the fix to prove reproduction.")
                last_error = "test_did_not_reproduce_bug"
                continue
            if re.search(r"\b(?:NameError|SyntaxError|IndentationError|ModuleNotFoundError)\b", unpatched_out):
                feedback_history.append(
                    f"Regression test itself crashed with an import/name/syntax error on unpatched code (fix missing imports in regression_test):\n{unpatched_out[-900:]}"
                )
                last_error = "regression_test_broken_imports"
                continue

            # Gate B: Write patched file to sandbox, validate py_compile + regression test PASS + existing tests PASS
            sandbox_target.write_text(patched_text, encoding="utf-8")
            try:
                py_compile.compile(str(sandbox_target), doraise=True)
            except py_compile.PyCompileError as exc:
                feedback_history.append(f"py_compile failed: {exc}")
                last_error = "py_compile_failed"
                continue

            patched_ok, patched_out = self._run_pytest_on_sandbox(
                sandbox_dir, test_file, extra_tests=ticket.get("existing_tests") or ()
            )
            if not patched_ok:
                feedback_history.append(f"Patched file failed pytest:\n{patched_out[-1000:]}")
                last_error = "patched_pytest_failed"
                continue

            # Gate C: Disjoint-family security & correctness audit
            used_worker_model = getattr(self, "_last_worker_model", self.worker_model)
            auditor_model, verdict, auditor_reason = self._invoke_disjoint_auditor(
                diff_text, ticket, patched_out, worker_model_used=used_worker_model
            )
            patch_id = f"srp_{int(time.time())}_{hashlib.sha256(diff_text.encode()).hexdigest()[:8]}"
            after_sha = _sha256_bytes(patched_text.encode("utf-8"))
            now = time.time()

            if verdict != "APPROVE":
                last_error = f"auditor_rejected:{auditor_reason}"
                self._record_run(run_id, bug_id, rel_path, str(ticket.get("summary", "")), "rejected_by_auditor", attempts, auditor_model, verdict, "", {"reason": auditor_reason})
                return {
                    "ok": False,
                    "run_id": run_id,
                    "status": "rejected_by_auditor",
                    "auditor_model": auditor_model,
                    "auditor_verdict": verdict,
                    "auditor_reason": auditor_reason,
                }

            # Stage verified patch in SQLite
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO self_repair_patches (
                        patch_id, run_id, bug_id, target_rel_path, summary, unified_diff,
                        changed_lines, patched_content, regression_test_code, before_sha256,
                        after_sha256, worker_model, auditor_model, auditor_verdict,
                        auditor_reason, test_output, status, backup_path, approved_by,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', '', '', ?, ?)
                    """,
                    (
                        patch_id,
                        run_id,
                        bug_id,
                        rel_path,
                        str(proposal.get("summary") or ticket.get("summary") or ""),
                        diff_text,
                        changed_lines,
                        patched_text,
                        reg_test_code,
                        before_sha,
                        after_sha,
                        used_worker_model,
                        auditor_model,
                        verdict,
                        auditor_reason,
                        patched_out,
                        now,
                        now,
                    ),
                )

            final_status = "pending_approval"
            apply_res = None
            if auto_apply:
                apply_res = self.apply_patch(patch_id, approver="sabrina-auto-verified")
                final_status = apply_res.get("status", "pending_approval")

            self._record_run(
                run_id,
                bug_id,
                rel_path,
                str(ticket.get("summary", "")),
                final_status,
                attempts,
                auditor_model,
                verdict,
                patch_id,
                {"changed_lines": changed_lines, "auditor_reason": auditor_reason},
            )
            return {
                "ok": True,
                "run_id": run_id,
                "patch_id": patch_id,
                "status": final_status,
                "target_rel_path": rel_path,
                "changed_lines": changed_lines,
                "worker_model": used_worker_model,
                "auditor_model": auditor_model,
                "auditor_verdict": verdict,
                "auditor_reason": auditor_reason,
                "unified_diff": diff_text,
                "apply_result": apply_res,
            }

        self._record_run(run_id, bug_id, rel_path, str(ticket.get("summary", "")), "failed", attempts, "", "", "", {"error": last_error})
        return {"ok": False, "run_id": run_id, "status": "failed", "attempts": attempts, "error": last_error}

    def _record_run(
        self,
        run_id: str,
        bug_id: str,
        target_path: str,
        bug_summary: str,
        status: str,
        attempts: int,
        auditor_model: str,
        auditor_verdict: str,
        patch_id: str,
        detail: Dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO self_repair_runs (
                    run_id, bug_id, target_path, bug_summary, status, attempts,
                    worker_model, auditor_model, auditor_verdict, patch_id, detail_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, bug_id, target_path, bug_summary, status, attempts, self.worker_model, auditor_model, auditor_verdict, patch_id, json.dumps(detail), time.time()),
            )

    # ------------------------------------------------------------------
    # Stage 5: Atomic Apply, Conflict Check & Instant Rollback
    # ------------------------------------------------------------------
    def apply_patch(self, patch_id: str, *, approver: str = "operator") -> Dict[str, Any]:
        """Atomically apply a verified staged patch with SHA-256 conflict check, backup, and smoke verification."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM self_repair_patches WHERE patch_id = ?", (patch_id,)).fetchone()
        if not row:
            return {"ok": False, "status": "not_found", "error": "patch_not_found"}
        p = dict(row)
        if p["status"] != "pending_approval":
            return {"ok": False, "status": p["status"], "error": f"patch_not_pending:{p['status']}"}

        protected, prot_reason = is_protected_repair_target(p["target_rel_path"], repo_root=self.repo_root)
        if protected:
            return {"ok": False, "status": "blocked_protected", "error": prot_reason}

        target = (self.repo_root / p["target_rel_path"]).resolve()
        if not target.is_file():
            return {"ok": False, "status": "error", "error": "target_missing"}

        live_sha = _sha256_file(target)
        if live_sha != p["before_sha256"]:
            return {"ok": False, "status": "conflict", "error": "file_modified_since_patch_staged"}

        # Immutable backup before writing
        bdir = self.backup_root / patch_id
        bdir.mkdir(parents=True, exist_ok=True)
        bfile = bdir / target.name
        shutil.copy2(target, bfile)

        # Write atomically via temp file + os.replace
        tmp_file = target.with_suffix(".py.srp_tmp")
        tmp_file.write_text(p["patched_content"], encoding="utf-8")
        os.replace(tmp_file, target)

        # Immediate post-apply py_compile verification; rollback automatically on any failure
        try:
            py_compile.compile(str(target), doraise=True)
        except Exception as exc:
            shutil.copy2(bfile, target)
            return {"ok": False, "status": "rolled_back_on_smoke_failure", "error": str(exc)}

        with self._connect() as conn:
            conn.execute(
                "UPDATE self_repair_patches SET status='applied', backup_path=?, approved_by=?, updated_at=? WHERE patch_id=?",
                (str(bfile), approver, time.time(), patch_id),
            )
        return {
            "ok": True,
            "patch_id": patch_id,
            "status": "applied",
            "target_rel_path": p["target_rel_path"],
            "backup_path": str(bfile),
            "after_sha256": _sha256_file(target),
        }

    def rollback_patch(self, patch_id: str) -> Dict[str, Any]:
        """Restore the exact pre-patch SHA-256 backup for an applied patch."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM self_repair_patches WHERE patch_id = ?", (patch_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "patch_not_found"}
        p = dict(row)
        bfile = Path(p.get("backup_path") or "")
        if not bfile.is_file():
            return {"ok": False, "error": "backup_file_missing"}
        target = (self.repo_root / p["target_rel_path"]).resolve()
        shutil.copy2(bfile, target)
        restored_sha = _sha256_file(target)
        with self._connect() as conn:
            conn.execute(
                "UPDATE self_repair_patches SET status='rolled_back', updated_at=? WHERE patch_id=?",
                (time.time(), patch_id),
            )
        return {
            "ok": restored_sha == p["before_sha256"],
            "patch_id": patch_id,
            "status": "rolled_back",
            "restored_sha256": restored_sha,
            "matches_original": restored_sha == p["before_sha256"],
        }

    def status_summary(self, limit: int = 12) -> Dict[str, Any]:
        with self._connect() as conn:
            patches = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT patch_id, run_id, bug_id, target_rel_path, summary, changed_lines,
                           worker_model, auditor_model, auditor_verdict, auditor_reason,
                           unified_diff, status, approved_by, created_at, updated_at
                    FROM self_repair_patches ORDER BY created_at DESC LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
            ]
            runs = [
                dict(r)
                for r in conn.execute(
                    "SELECT run_id, bug_id, target_path, status, attempts, auditor_model, auditor_verdict, patch_id, created_at FROM self_repair_runs ORDER BY created_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            ]
        return {
            "harvested_tickets": self.harvest_bugs(),
            "patches": patches,
            "recent_runs": runs,
            "pending_approval_count": sum(1 for p in patches if p["status"] == "pending_approval"),
            "applied_count": sum(1 for p in patches if p["status"] == "applied"),
        }

    def drain_awaiting_validation_skills(self, *, max_items: int = 6) -> Dict[str, Any]:
        """Autonomously audit and promote/reject awaiting_validation SKILL.md proposals (zero-HITL)."""
        approved, rejected, skipped = 0, 0, 0
        try:
            with self._connect() as conn:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_skill_proposals'").fetchone():
                    return {"checked": 0, "approved": 0, "rejected": 0, "skipped": 0}
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM assistant_skill_proposals WHERE status='awaiting_validation' ORDER BY updated DESC LIMIT ?",
                        (max(1, int(max_items)),),
                    ).fetchall()
                ]
        except Exception:
            return {"checked": 0, "approved": 0, "rejected": 0, "skipped": 0}

        if not rows:
            return {"checked": 0, "approved": 0, "rejected": 0, "skipped": 0}

        import yaml
        from tools.sabrina_autonomous_gatekeeper import AutonomousSecurityGatekeeper

        gk = AutonomousSecurityGatekeeper(db_path=self.db_path)
        for p in rows:
            pid = p["proposal_id"]
            name = p["skill_name"]
            content = p.get("skill_content") or ""
            target = p.get("target_path") or f"skills/{name}/SKILL.md"
            if "Candidate Adapter Patch Bundle" in content:
                skipped += 1
                continue
            ok_screen, violations = gk.programmatic_screen(name, content, target)
            if not ok_screen:
                gk.hydration.reject_proposal(pid, reason=f"Programmatic screen rejected: {'; '.join(violations)}", reviewer="sabrina_self_repair:programmatic")
                rejected += 1
                continue
            py_blocks = re.findall(r"```python\s*(.*?)```", content, re.S)
            syntax_errs = []
            for idx, blk in enumerate(py_blocks, 1):
                try:
                    ast.parse(blk)
                except SyntaxError as exc:
                    syntax_errs.append(f"block_{idx}:line_{exc.lineno}:{exc.msg}")
            if syntax_errs:
                gk.hydration.reject_proposal(pid, reason=f"Python snippet syntax error: {'; '.join(syntax_errs)}", reviewer="sabrina_self_repair:ast")
                rejected += 1
                continue

            version = "1.0.0"
            try:
                fm = yaml.safe_load(content.split("---", 2)[1]) or {}
                if fm.get("version"):
                    version = str(fm["version"])
            except Exception:
                pass
            sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
            auditor_model, verdict, reason = self._invoke_disjoint_auditor(
                f"SKILL.md ({target}):\n{content[:2000]}",
                {"bug_id": pid, "summary": f"Defensive safety audit for SKILL.md `{name}`"},
                "programmatic_screen=PASS; python_ast=PASS",
                worker_model_used=str(p.get("reviewer") or "internet_resource_indexer"),
            )
            if auditor_model == "none":
                skipped += 1
                continue
            if verdict == "APPROVE":
                with self._connect() as conn:
                    conn.execute("UPDATE assistant_skill_proposals SET status='pending_approval' WHERE proposal_id=?", (pid,))
                gk.hydration.approve_proposal(
                    proposal_id=pid,
                    expected_version=version,
                    expected_sha256=sha,
                    reviewer=f"sabrina_auto_gatekeeper:{auditor_model}",
                    feedback=f"Zero-HITL programmatic + AST + disjoint audit APPROVED: {reason[:220]}",
                )
                approved += 1
            else:
                gk.hydration.reject_proposal(pid, reason=f"Disjoint audit rejected: {reason[:240]}", reviewer=f"sabrina_auto_gatekeeper:{auditor_model}")
                rejected += 1
        return {"checked": len(rows), "approved": approved, "rejected": rejected, "skipped": skipped}

    def run_workshop_cycle(self, *, max_tickets: int = 1, auto_apply: bool = True) -> Dict[str, Any]:
        """Harvest current bugs and run isolated sandbox repair on up to `max_tickets` code tickets (zero-HITL by default)."""
        tickets = self.harvest_bugs()
        code_tickets = [t for t in tickets if t.get("target_rel_path")]
        results = []
        attempted = 0
        for tk in code_tickets:
            if attempted >= max(1, int(max_tickets)):
                break
            rel = str(tk.get("target_rel_path") or "").strip().replace("\\", "/")
            src = (self.repo_root / rel).resolve()
            if src.is_file():
                cur_sha = _sha256_file(src)
                with self._connect() as conn:
                    existing = conn.execute(
                        "SELECT patch_id FROM self_repair_patches WHERE bug_id = ? AND before_sha256 = ? AND status = 'pending_approval' LIMIT 1",
                        (str(tk.get("bug_id") or ""), cur_sha),
                    ).fetchone()
                if existing:
                    if auto_apply:
                        app = self.apply_patch(existing["patch_id"], approver="sabrina-auto-verified")
                        results.append({"ok": bool(app.get("ok")), "bug_id": tk.get("bug_id"), "target_rel_path": rel, **app})
                    else:
                        results.append({
                            "ok": True,
                            "status": "already_pending_approval",
                            "patch_id": existing["patch_id"],
                            "bug_id": tk.get("bug_id"),
                            "target_rel_path": rel,
                        })
                    continue
            results.append(self.repair_ticket(tk, auto_apply=auto_apply))
            attempted += 1
        skill_drain = self.drain_awaiting_validation_skills(max_items=6) if auto_apply else {"checked": 0}
        return {
            "ok": True,
            "auto_apply": bool(auto_apply),
            "harvested_total": len(tickets),
            "code_tickets_attempted": attempted,
            "results": results,
            "skill_drain": skill_drain,
            "status": self.status_summary(limit=8),
        }


# ------------------------------------------------------------------
# TOOL_REGISTRY Wrappers
# ------------------------------------------------------------------
def harvest_sabrina_bugs_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    return SabrinaSelfRepairWorkshop().status_summary(limit=int((action or {}).get("limit", 10)))


def run_self_repair_cycle_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    action = action or {}
    return SabrinaSelfRepairWorkshop().run_workshop_cycle(
        max_tickets=int(action.get("max_tickets", 1)),
        auto_apply=bool(action.get("auto_apply", True)),
    )


def approve_self_repair_patch_tool(action: Dict[str, Any], loop: Any = None) -> Dict[str, Any]:
    action = action or {}
    return SabrinaSelfRepairWorkshop().apply_patch(
        str(action.get("patch_id") or ""),
        approver=str(action.get("approver") or "operator"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Autonomous Self-Repair Code Workshop")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Show harvested bugs, staged/applied patches, and recent self-repair runs")
    p_run = sub.add_parser("run", help="Run a zero-HITL self-repair cycle on top harvested bug ticket(s)")
    p_run.add_argument("--max-tickets", type=int, default=1)
    p_run.add_argument("--auto-apply", action="store_true", default=True, help="Auto-apply verified patches (default True)")
    p_run.add_argument("--stage-only", action="store_true", help="Stage verified patches without applying")
    p_app = sub.add_parser("approve", help="Approve and apply a staged patch_id")
    p_app.add_argument("patch_id")
    p_rb = sub.add_parser("rollback", help="Rollback an applied patch_id")
    p_rb.add_argument("patch_id")
    args = parser.parse_args()

    ws = SabrinaSelfRepairWorkshop()
    if args.cmd == "status":
        print(json.dumps(ws.status_summary(), indent=2))
    elif args.cmd == "run":
        do_apply = not bool(args.stage_only)
        print(json.dumps(ws.run_workshop_cycle(max_tickets=args.max_tickets, auto_apply=do_apply), indent=2))
    elif args.cmd == "approve":
        print(json.dumps(ws.apply_patch(args.patch_id), indent=2))
    else:
        print(json.dumps(ws.rollback_patch(args.patch_id), indent=2))


if __name__ == "__main__":
    main()
