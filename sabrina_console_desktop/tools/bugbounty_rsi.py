"""Bug-bounty RSI benchmark -- drive Sabrina's self-improvement with REAL disclosed vulnerabilities.

The existing capability cycle (tools/capability_bench + config/capability_tasks.json) grades her on
LeetCode-style coding tasks. This module builds a SECURITY benchmark from the Hugging Face dataset
Hacker0x01/hackerone_disclosed_reports (1M+ real disclosed HackerOne reports): each report becomes a
graded task where she must identify the vulnerability class from the researcher's write-up, scored
against the report's ground-truth `weakness` label. That score is the RSI objective -- rsi_improve's
dream loop optimizes her vuln-analysis approach to raise it.

Data only flows IN (public dataset -> local task file); Sabrina's own source never leaves the box.

Pieces:
  fetch_reports(limit)      -> raw rows via the HF datasets-server (no auth, public)
  report_to_task(row)       -> a graded task dict, or None to skip
  build_tasks(limit)        -> writes config/bugbounty_tasks.json
  grade_weakness(pred, truth) -> 0..1 match score (the RSI signal)
  run_benchmark(solver)     -> mean score over the task set  [needs a live model]
"""

import json
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
TASKS_PATH = REPO_ROOT / "config" / "bugbounty_tasks.json"
SCORES_PATH = REPO_ROOT / "data" / "bugbounty_scores.json"
DATASET = "Hacker0x01/hackerone_disclosed_reports"
_ROWS_URL = "https://datasets-server.huggingface.co/rows"
_FIRSTROWS_URL = "https://datasets-server.huggingface.co/first-rows"

# Common HackerOne weakness names -> the key tokens a correct answer must contain. Lets a free-text
# model answer ("this is a stored cross-site scripting bug") match the label ("Cross-site Scripting").
_WEAKNESS_TOKENS = {
    "cross-site scripting": ["xss", "cross-site scripting", "cross site scripting"],
    "sql injection": ["sql injection", "sqli"],
    "server-side request forgery": ["ssrf", "server-side request forgery", "server side request forgery"],
    "cross-site request forgery": ["csrf", "cross-site request forgery"],
    "insecure direct object reference": ["idor", "insecure direct object", "bola", "broken object level"],
    "information disclosure": ["information disclosure", "info disclosure", "sensitive data exposure"],
    "improper authentication": ["authentication bypass", "auth bypass", "improper authentication"],
    "privilege escalation": ["privilege escalation", "privesc"],
    "open redirect": ["open redirect"],
    "remote code execution": ["remote code execution", "rce", "command injection"],
    "path traversal": ["path traversal", "directory traversal", "lfi", "local file inclusion"],
    "xml external entities": ["xxe", "xml external entit"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).strip()


def _tokens_for(truth: str) -> List[str]:
    n = _norm(truth)
    for key, toks in _WEAKNESS_TOKENS.items():
        if key in n or any(_norm(t) in n for t in toks):
            return toks
    # fall back to the significant words of the label itself
    return [w for w in n.split() if len(w) > 3] or [n]


def grade_weakness(prediction: str, truth: str) -> float:
    """1.0 if the prediction names the true weakness class, else 0.0. Token/synonym aware so a
    correct free-text answer isn't marked wrong for wording. Empty truth -> 0.0 (unscoreable)."""
    if not truth or not prediction:
        return 0.0
    p = _norm(prediction)
    return 1.0 if any(_norm(t) in p for t in _tokens_for(truth)) else 0.0


def _weakness_name(row: Dict[str, Any]) -> Optional[str]:
    w = row.get("weakness")
    if isinstance(w, dict):
        return w.get("name") or w.get("id")
    if isinstance(w, str) and w.strip():
        return w
    return None


def report_to_task(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """One disclosed report -> a graded task, or None to skip (no content / no weakness label)."""
    if row.get("visibility") not in ("full", None):
        return None
    truth = _weakness_name(row)
    if not truth:
        return None
    info = (row.get("vulnerability_information") or "").strip()
    if len(info) < 40:  # too thin to reason about
        return None
    scope = row.get("structured_scope")
    target = ""
    if isinstance(scope, dict):
        target = scope.get("asset_identifier") or scope.get("asset_type") or ""
    title = (row.get("title") or "").strip()
    prompt = (
        "A security researcher disclosed the vulnerability below. Read the write-up and: "
        "(1) name the vulnerability CLASS (e.g. SQL Injection, Cross-site Scripting, SSRF, IDOR, RCE), "
        "and (2) briefly state the exploitation primitive and likely severity (P1-P5).\n\n"
        f"Title: {title}\n"
        f"Target: {target}\n"
        f"Report:\n{info[:2000]}"
    )
    return {
        "name": f"h1-{row.get('id')}",
        "prompt": prompt,
        "ground_truth_weakness": truth,
        "has_bounty": bool(row.get("has_bounty?")),
        "target": target,
    }


def _rows_from(url_base: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
    req = urllib.request.Request(f"{url_base}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "Sabrina-RSI"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return [item.get("row", {}) for item in json.load(r).get("rows", [])]


def fetch_reports(limit: int = 100, offset: int = 0, split: str = "train") -> List[Dict[str, Any]]:
    """Fetch raw report rows from the HF datasets-server.

    This dataset has the paginated /rows viewer DISABLED (is-valid -> viewer:false, preview:true),
    so we use /first-rows (the working preview, ~44-100 rows). For a larger corpus, iterate the
    train/test/validation splits, or swap in the parquet files / `datasets` library on the lab.
    """
    try:
        rows = _rows_from(_FIRSTROWS_URL, {"dataset": DATASET, "config": "default", "split": split})
        return rows[offset:offset + limit] if (offset or limit < len(rows)) else rows
    except Exception:
        # last resort: try the paginated endpoint in case the viewer gets enabled later
        try:
            return _rows_from(_ROWS_URL, {"dataset": DATASET, "config": "default",
                                          "split": split, "offset": offset, "length": min(100, limit)})
        except Exception as e:
            print(f"[bugbounty_rsi] fetch failed: {e}", flush=True)
            return []


def build_tasks(limit: int = 200, only_bountied: bool = True) -> Dict[str, Any]:
    """Fetch reports across all splits, convert to graded tasks, write config/bugbounty_tasks.json."""
    tasks, seen = [], set()
    for split in ("train", "validation", "test"):
        for row in fetch_reports(limit=200, split=split):
            t = report_to_task(row)
            if not t or t["name"] in seen:
                continue
            if only_bountied and not t["has_bounty"]:
                continue
            seen.add(t["name"])
            tasks.append(t)
            if len(tasks) >= limit:
                break
        if len(tasks) >= limit:
            break
    TASKS_PATH.write_text(json.dumps(tasks, indent=2), encoding="utf-8")
    return {"written": len(tasks), "file": str(TASKS_PATH.relative_to(REPO_ROOT))}


def _pod_url() -> str:
    """First RunPod ollama URL from the env (same channel the loop/capability_bench use)."""
    urls = [u.strip() for u in os.environ.get("SABRINA_RUNPOD_OLLAMA_URLS", "").split(",") if u.strip()]
    return urls[0] if urls else ""


def solve_with_pod(prompt: str, timeout: int = 180) -> str:
    """Ask the pod's 72B to answer a task prompt. Stdlib-only so this runs from anywhere (no lab
    runtime needed): POST to {pod}/api/chat. Mirrors tools/capability_bench.solve_with_pod. The
    'User-Agent: Sabrina' header is REQUIRED -- RunPod's Cloudflare rejects urllib's default UA."""
    url = _pod_url()
    if not url:
        raise RuntimeError("SABRINA_RUNPOD_OLLAMA_URLS is unset -- no pod to query.")
    model = os.environ.get("SABRINA_FORCE_LOCAL_MODEL", "qwen2.5:72b")
    body = json.dumps({"model": model, "stream": False, "options": {"temperature": 0.2},
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/api/chat", data=body, method="POST",
                                 headers={"User-Agent": "Sabrina", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return (json.load(r).get("message", {}) or {}).get("content", "")


def run_benchmark(solver, tasks_path: Path = TASKS_PATH, limit: Optional[int] = None,
                  verbose: bool = True) -> Tuple[float, List[Dict[str, Any]]]:
    """Return (mean_score, results) over the task set -- the (score, results) tuple contract the RSI
    cycle uses (cf. tools/capability_bench.run_benchmark). `solver(prompt) -> str` is any live model
    (e.g. solve_with_pod against the pod 72B). NEEDS a running model -- this is the RSI objective."""
    tasks = json.loads(Path(tasks_path).read_text(encoding="utf-8"))
    if limit:
        tasks = tasks[:limit]
    results = []
    for t in tasks:
        answer = solver(t["prompt"])
        score = grade_weakness(answer, t["ground_truth_weakness"])
        results.append({"name": t["name"], "truth": t["ground_truth_weakness"], "score": score})
        if verbose:
            print(f"  {t['name']}: {t['ground_truth_weakness']:32s} -> {score}", flush=True)
    mean = sum(r["score"] for r in results) / len(results) if results else 0.0
    return mean, results


def log_score(score: float, results: List[Dict[str, Any]], note: str = "") -> None:
    """Append a benchmark run to data/bugbounty_scores.json (mirrors capability_bench._log)."""
    import datetime
    SCORES_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        hist = json.loads(SCORES_PATH.read_text(encoding="utf-8"))
    except Exception:
        hist = []
    hist.append({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                 "score": round(score, 4), "n": len(results), "note": note})
    SCORES_PATH.write_text(json.dumps(hist, indent=2), encoding="utf-8")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Bug-bounty RSI benchmark.")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build", help="Fetch reports -> config/bugbounty_tasks.json")
    b.add_argument("-n", "--num", type=int, default=200)
    b.add_argument("--all", action="store_true", help="Include non-bountied reports too")
    m = sub.add_parser("measure", help="Score the pod's 72B on the vuln-classification benchmark")
    m.add_argument("-n", "--num", type=int, default=None, help="Only the first N tasks (quick check)")
    a = ap.parse_args()
    if a.cmd == "build":
        print(json.dumps(build_tasks(limit=a.num, only_bountied=not a.all), indent=2))
    elif a.cmd == "measure":
        score, results = run_benchmark(solve_with_pod, limit=a.num, verbose=True)
        log_score(score, results, note="cli measure")
        print(f"\n[bugbounty] score = {score:.3f} over {len(results)} tasks "
              f"(correct vuln-class identifications) -> {SCORES_PATH.relative_to(REPO_ROOT)}", flush=True)
    else:
        ap.print_help()
