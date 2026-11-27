"""
tools/coven/shared.py
=====================
Shared constants, helpers, ScopePolicy, CredentialStore used across
all Sabrina Coven modules.
"""

import os
import sys
import re
import io
import json
import time
import urllib.request
import urllib.parse
import urllib.error
import datetime
import threading
from typing import List, Dict, Any, Optional

if hasattr(sys.stdout, "buffer") and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.platform == "win32":
    os.system("")


# ── ANSI colours ──────────────────────────────────────────────────────────────
class C:
    G  = "\033[92m"; Y  = "\033[93m"; R  = "\033[91m"
    B  = "\033[94m"; M  = "\033[35m"; CN = "\033[96m"
    BD = "\033[1m";  DM = "\033[2m";  X  = "\033[0m"


# ── Environment / model config ────────────────────────────────────────────────
try:
    from sabrina_config import OLLAMA_BASE_URL as _CFG_OLLAMA_BASE
except ImportError:
    _CFG_OLLAMA_BASE = "http://127.0.0.1:11434"
OLLAMA_URL          = os.environ.get("AGENT_OLLAMA_URL",    f"{_CFG_OLLAMA_BASE}/api/chat")
OLLAMA_WORKER_MODEL = os.environ.get("WORKER_MODEL_NAME",  "local-worker")
OLLAMA_PLANNER_MODEL= os.environ.get("PLANNER_MODEL_NAME", "local-planner")
GEMINI_KEY          = os.environ.get("GEMINI_API_KEY",      "")
GEMINI_MODEL        = os.environ.get("AGENT_GEMINI_MODEL",  "gemini-2.0-flash")

STORAGE_DIR = os.environ.get(
    "SABRINA_STORAGE_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "coven_storage")
)


# ── Simple Ollama chat wrapper (stdlib only) ──────────────────────────────────
def _ollama_chat(system: str, user: str, max_tokens: int = 1024,
                 model: str = None, task_type: str = "worker") -> str:
    model = model or OLLAMA_WORKER_MODEL
    payload = {
        "model":   model,
        "messages": [
            {"role": "system",  "content": system},
            {"role": "user",    "content": user},
        ],
        "stream":  False,
        "options": {"temperature": 0.1, "num_predict": max_tokens, "num_gpu": 99},
    }
    data = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=data, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8")).get("message", {}).get("content", "")
    except Exception as e:
        return f"[ERROR: {e}]"


# ── Simple HTTP GET helper (stdlib only) ──────────────────────────────────────
def _http_get(url: str, headers: Dict[str, str] = None,
              timeout: int = 10) -> tuple:
    """Returns (status_code, body_str, headers_dict). Never raises."""
    hdrs = {"User-Agent": "Mozilla/5.0 (Sabrina Security Research)"}
    if headers:
        hdrs.update(headers)
    try:
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", errors="ignore")
                return r.status, body, dict(r.headers)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")
            return e.code, body, dict(e.headers) if e.headers else {}
    except Exception:
        return 0, "", {}


# ── Scope Policy ──────────────────────────────────────────────────────────────
class ScopePolicy:
    """Parses and enforces engagement scope rules from a JSON file."""

    def __init__(self, raw: Dict[str, Any]):
        self.platform      = raw.get("platform", "local_lab")
        self.program       = raw.get("program",  "unknown")
        self.in_scope      = [s.lower().strip() for s in raw.get("in_scope", [])]
        self.out_of_scope  = [s.lower().strip() for s in raw.get("out_of_scope", [])]
        self.focus         = raw.get("focus", ["xss", "sqli", "idor", "ssrf"])
        self.vuln_focus    = self.focus
        self.focus_details = raw.get("focus_details", {})
        self.credentials   = raw.get("credentials", {})
        self.notes         = raw.get("notes", "")
        self.raw           = raw

    def is_in_scope(self, target: str) -> bool:
        def _strip(s: str) -> str:
            return s.lower().replace("https://", "").replace("http://", "").split("/")[0]
        t = _strip(target)
        norm_in  = [_strip(x) for x in self.in_scope]
        norm_oos = [_strip(x) for x in self.out_of_scope]
        if t in norm_in:
            return True
        for oos in norm_oos:
            if self._matches(t, oos):
                return False
        for ins in norm_in:
            if self._matches(t, ins):
                return True
        return False

    @staticmethod
    def _matches(target: str, pattern: str) -> bool:
        if pattern.startswith("*."):
            domain = pattern[2:]
            return target.endswith(domain) or target == domain
        return target == pattern or target.endswith("." + pattern)

    def summary(self) -> str:
        return (f"Program: {self.program} | Platform: {self.platform}\n"
                f"In-scope: {', '.join(self.in_scope[:6])}\n"
                f"Focus: {', '.join(self.focus[:8])}")

    @classmethod
    def from_file(cls, path: str) -> "ScopePolicy":
        with open(path, "r", encoding="utf-8") as f:
            return cls(json.load(f))

    @classmethod
    def from_text(cls, text: str, program: str = "manual") -> "ScopePolicy":
        parts = [p.strip() for p in re.split(r"[,\n]+", text) if p.strip()]
        return cls({"platform": "manual", "program": program, "in_scope": parts})


# ── Credential Store ──────────────────────────────────────────────────────────
class CredentialStore:
    """Loads and provides credentialed org configs from a JSON creds file."""

    def __init__(self, creds_path: Optional[str] = None):
        self.orgs: List[Dict[str, Any]] = []
        if creds_path and os.path.exists(creds_path):
            with open(creds_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.orgs = data.get("orgs", [])

    def get_org(self, index: int) -> Optional[Dict[str, Any]]:
        return self.orgs[index % len(self.orgs)] if self.orgs else None

    def all_orgs(self) -> List[Dict[str, Any]]:
        return self.orgs
