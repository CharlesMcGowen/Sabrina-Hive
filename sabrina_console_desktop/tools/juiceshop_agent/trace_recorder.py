#!/usr/bin/env python3
"""
tools/juiceshop_agent/trace_recorder.py
=======================================
The missing bridge: turn a VALIDATED (scoreboard-confirmed) <think>+<action> trajectory
into a ChatML training row and append it to the STaR SFT corpus.

Why this had to be built (design ref tasks/wru2sb529.output): probe_api stores reasoning
traces and reward outcomes in SEPARATE tables that are never joined, so there was no path
from "this reasoning led to a real solve" to "train on it." This module is that join.

Only reward==1.0 (real solved-flag flip) trajectories should be recorded -- STaR / expert
iteration trains exclusively on successes. The row schema matches what
tools/nuclei_agent/train_sft_nuclei.py consumes: {"messages":[system,user,assistant]},
assistant_only_loss trains on the assistant turn (the <think>+<action> the model must learn
to produce).

Guards:
  - JWTs scrubbed before persistence (a solved trace often carries a live session token).
  - Deduped by (challenge_key, normalized-action-signature) so best-of-N duplicates of the
    same solving action don't flood the corpus with near-identical rows.
"""
import json
import os
import re
from threading import Lock
from typing import Any, Dict, List, Optional

_JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_MAX_SNIPPET_CHARS = 1000  # cap response-body length stored in a detector example
_lock = Lock()


def _scrub(text: str) -> str:
    return _JWT_PATTERN.sub("[JWT_REDACTED]", text or "")


_HYPOTHESIS_RE = re.compile(r'<hypothesis\s+vuln="([^"]+)"', re.IGNORECASE)


def _action_signature(assistant_text: str) -> str:
    """Normalize the <tool_call> http_request action so trivially-different duplicates
    collapse to one signature.

    The action rides Qwen's native <tool_call>{"name","arguments"} grammar, so the identity
    of an HTTP attempt is the arguments' method+path. Falls back to the whole scrubbed text
    if no parseable <tool_call> is present.
    """
    m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", assistant_text, re.DOTALL)
    if not m:
        return _scrub(assistant_text).strip()
    try:
        call = json.loads(m.group(1))
        args = call.get("arguments", {}) if isinstance(call, dict) else {}
        if not isinstance(args, dict):
            args = {}
        sig = json.dumps({
            "name": call.get("name", ""),
            "method": str(args.get("method", "")).upper(),
            "path": args.get("path", ""),
        }, sort_keys=True)
        return _scrub(sig)
    except json.JSONDecodeError:
        return _scrub(m.group(1)).strip()


def _extract_vuln_class(text: str) -> Optional[str]:
    """Pull the vuln class from a <hypothesis vuln="CLASS"> tag, if present.

    Recorded as row metadata so tools/rsi_distill.py can later allocate generation budget
    per vuln class (its build_pool keys on rec['vuln_class'])."""
    m = _HYPOTHESIS_RE.search(text or "")
    return m.group(1).lower() if m else None


def _existing_signatures(corpus_path: str) -> set:
    sigs = set()
    if not os.path.exists(corpus_path):
        return sigs
    with open(corpus_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get("_challenge_key", "")
            asst = ""
            for msg in row.get("messages", []):
                if msg.get("role") == "assistant":
                    asst = msg.get("content", "")
            sigs.add((key, _action_signature(asst)))
    return sigs


def record_solved_trace(
    corpus_path: str,
    challenge_key: str,
    system_prompt: str,
    user_prompt: str,
    assistant_text: str,
) -> Dict[str, Any]:
    """Append one solved trajectory as a ChatML row. Returns {'written':bool,'reason':...}.

    Idempotent by (challenge_key, action-signature): a duplicate solving action for the
    same challenge is skipped rather than re-appended.
    """
    assistant_text = _scrub(assistant_text)
    sig = (challenge_key, _action_signature(assistant_text))
    with _lock:
        if sig in _existing_signatures(corpus_path):
            return {"written": False, "reason": "duplicate"}
        row = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": assistant_text},
            ],
            # metadata; the SFT trainer ignores keys it doesn't use (verified: to_chat keeps
            # only "messages"). _vuln_class feeds rsi_distill's per-class curriculum later.
            "_challenge_key": challenge_key,
            "_vuln_class": _extract_vuln_class(assistant_text),
        }
        os.makedirs(os.path.dirname(os.path.abspath(corpus_path)) or ".", exist_ok=True)
        with open(corpus_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return {"written": True, "reason": "ok"}


def record_solved_conversation(
    corpus_path: str,
    challenge_key: str,
    messages: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Append a MULTI-TURN solved trajectory (system,user,assistant,user,assistant,...).

    For multi-step challenges the teacher takes several think+action steps, feeding each
    live response back as a user observation. assistant_only_loss trains on every assistant
    turn, so the student learns to use observations, not just fire one shot. Deduped by
    (challenge_key, signature of the FINAL assistant action -- the one that flipped the flag).
    JWTs scrubbed in every turn.
    """
    scrubbed = [{"role": m["role"], "content": _scrub(m.get("content", ""))} for m in messages]
    final_asst = ""
    for m in reversed(scrubbed):
        if m["role"] == "assistant":
            final_asst = m["content"]
            break
    sig = (challenge_key, _action_signature(final_asst))
    # vuln_class from any assistant turn's <hypothesis vuln="..."> (first one found)
    vuln_class = None
    for m in scrubbed:
        if m["role"] == "assistant":
            vc = _extract_vuln_class(m["content"])
            if vc:
                vuln_class = vc
                break
    with _lock:
        if sig in _existing_signatures(corpus_path):
            return {"written": False, "reason": "duplicate"}
        row = {"messages": scrubbed, "_challenge_key": challenge_key, "_vuln_class": vuln_class}
        os.makedirs(os.path.dirname(os.path.abspath(corpus_path)) or ".", exist_ok=True)
        with open(corpus_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return {"written": True, "reason": "ok"}


DETECTOR_SYSTEM = (
    "You are a web-application vulnerability detector. Given an HTTP request and the response "
    "it produced against a target, decide whether the response indicates a CONFIRMED "
    "vulnerability, and if so name the class. Answer ONLY with JSON: "
    '{"vulnerable": true|false, "class": "<vuln class or null>"}.'
)


def _detector_signature(method: str, path: str, vulnerable: bool, vuln_class: Optional[str]) -> str:
    return json.dumps({
        "m": str(method).upper(), "p": path or "/",
        "v": bool(vulnerable), "c": vuln_class if vulnerable else None,
    }, sort_keys=True)


def _existing_detector_signatures(corpus_path: str) -> set:
    sigs = set()
    if not os.path.exists(corpus_path):
        return sigs
    with open(corpus_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("_det_sig"):
                sigs.add(row["_det_sig"])
    return sigs


def record_detection_example(
    corpus_path: str,
    request: Dict[str, Any],
    response: Dict[str, Any],
    vulnerable: bool,
    vuln_class: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one detector example (request+response -> verdict) as a ChatML row.

    The SECOND projection of the shared rollout stream: every executed step becomes a
    labeled req/resp pair. flip -> {vulnerable:true, class:<hypothesis vuln>}; miss ->
    {vulnerable:false} (weak negative label). JWT-scrubbed; deduped by
    (method, path, vulnerable, class) so best-of-N repeats collapse.
    """
    request = request or {}
    response = response or {}
    method = str(request.get("method") or "GET").upper()
    path = request.get("path") or "/"
    req_headers = request.get("headers") if isinstance(request.get("headers"), dict) else {}
    req_body = request.get("body")
    status = response.get("status")
    resp_body = (response.get("body") or "")[:_MAX_SNIPPET_CHARS]

    req_text = (f"{method} {path}\n"
                f"headers: {json.dumps(req_headers)}\n"
                f"body: {json.dumps(req_body)}")
    user = _scrub(
        f"REQUEST:\n{req_text}\n\nRESPONSE:\nstatus: {status}\n{resp_body}\n\n"
        "Is this response evidence of a confirmed vulnerability? Answer with the JSON verdict."
    )
    verdict = {"vulnerable": bool(vulnerable), "class": (vuln_class if vulnerable else None)}
    assistant = json.dumps(verdict)
    sig = _detector_signature(method, path, vulnerable, vuln_class)

    with _lock:
        if sig in _existing_detector_signatures(corpus_path):
            return {"written": False, "reason": "duplicate"}
        row = {
            "messages": [
                {"role": "system", "content": DETECTOR_SYSTEM},
                {"role": "user", "content": user},
                {"role": "assistant", "content": assistant},
            ],
            "_det_sig": sig,
            "_label": bool(vulnerable),
            "_vuln_class": vuln_class if vulnerable else None,
        }
        os.makedirs(os.path.dirname(os.path.abspath(corpus_path)) or ".", exist_ok=True)
        with open(corpus_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return {"written": True, "reason": "ok"}


def detector_stats(corpus_path: str) -> Dict[str, Any]:
    """Counts for the detector corpus: total, positives, negatives, per-class positives."""
    if not os.path.exists(corpus_path):
        return {"rows": 0, "positive": 0, "negative": 0, "per_class": {}}
    rows = pos = neg = 0
    per: Dict[str, int] = {}
    with open(corpus_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows += 1
            if r.get("_label"):
                pos += 1
                c = r.get("_vuln_class") or "?"
                per[c] = per.get(c, 0) + 1
            else:
                neg += 1
    return {"rows": rows, "positive": pos, "negative": neg, "per_class": per}


def corpus_stats(corpus_path: str) -> Dict[str, Any]:
    """Counts for reporting: total rows and per-challenge breakdown."""
    if not os.path.exists(corpus_path):
        return {"rows": 0, "per_challenge": {}}
    per: Dict[str, int] = {}
    rows = 0
    with open(corpus_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows += 1
            k = r.get("_challenge_key", "?")
            per[k] = per.get(k, 0) + 1
    return {"rows": rows, "per_challenge": per}
