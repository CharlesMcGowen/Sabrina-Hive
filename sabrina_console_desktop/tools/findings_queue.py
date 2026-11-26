"""
tools/findings_queue.py
=======================
Append-only queue of CONFIRMED-but-unreported bug-bounty findings.

Motivation (from the RSI substrate audit, 2026-09-27):

    "No 'confirmed but unreported' state anywhere in the tree. A CONFIRMED verdict
     that doesn't get drafted into a Bugcrowd/HackerOne submission before session
     end is functionally lost -- the whole point of bug-bounty engagements is
     bugs REPORTED, not bugs CONFIRMED, and this final step has zero state
     backing it."

The api_request handler already runs ResponseClassifier on every call that
carries a vuln_type; when the verdict is CONFIRMED, its raw text ends up in a
result string that eventually rolls out of decide_action's rolling ~60-item
window. Nothing durable records it. This module fixes that: each CONFIRMED
verdict lands as one row in data/findings_queue.jsonl keyed by engagement
program, and decide_action's per-round prompt shows a "you have N undrafted
findings" bullet whenever list_undrafted_findings(program) is non-empty for the
current engagement.

Deliberately narrow scope:

  - Only CONFIRMED verdicts land here. NEEDS_REVIEW and FALSE_POSITIVE do not.
    This keeps the queue small and every row actionable -- Sabrina looking at
    her queue sees only findings that are worth writing up.
  - JSONL not SQL. No new database, no schema migration. Small, human-readable,
    trivially recoverable if corrupted.
  - Keyed on the SCOPE-FILE BASENAME, not scope['slug'] (which doesn't exist as
    a schema field -- caught by the adversarial critique of the audit). A row
    written during a hostgator-latam-bb.json session is keyed
    "hostgator-latam-bb".
  - Response snippet is scrubbed of JWTs before saving. JWTs from earlier in the
    same session are captured into self.known_credentials for legitimate reuse;
    persisting them in a findings queue that could later be exported would leak
    session tokens outside their intended context.
"""
import json
import os
import re
import time
from threading import Lock
from typing import Any, Dict, List, Optional

# Same JWT pattern as sabrina_gemini_loop.py:22 -- must match. Kept in sync by
# convention, not by import, so this module has no dependency on the main loop.
_JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")

# Anything longer than this in the raw response text is truncated. 1000 chars is
# enough context for a human triaging the finding without becoming a
# response-body dump that fills the queue file.
_MAX_SNIPPET_CHARS = 1000

_lock = Lock()


def _default_queue_path() -> str:
    """Resolve data/findings_queue.jsonl relative to the project root."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(project_root, "data", "findings_queue.jsonl")


def program_from_scope_path(scope_path: Optional[str]) -> str:
    """Return the engagement program key from a scope file path.

    Cross-platform on purpose: os.path.basename splits only on the RUNNING OS's
    separator, so a Windows-style path handled on the Linux lab would return
    the whole string. This module runs both on Sabrina's Windows dev box and on
    the Linux lab, and SABRINA_ACTIVE_SCOPE_FILE can be written from either --
    so split on both '/' and '\\' regardless of which OS we're on.

    Examples:
        config/engagements/hostgator-latam-bb.json  -> "hostgator-latam-bb"
        C:\\...\\juiceshop_scope.json               -> "juiceshop_scope"
        None or ""                                  -> "unknown"
    """
    if not scope_path:
        return "unknown"
    base = scope_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if base.lower().endswith(".json"):
        base = base[:-5]
    return base or "unknown"


def _scrub_snippet(text: str) -> str:
    """Truncate + strip JWTs from a response body before persistence."""
    if not text:
        return ""
    if len(text) > _MAX_SNIPPET_CHARS:
        text = text[:_MAX_SNIPPET_CHARS]
    return _JWT_PATTERN.sub("[JWT_REDACTED]", text)


def record_confirmed_finding(
    program: str,
    method: str,
    url: str,
    vuln_type: str,
    classifier_verdict: str,
    matched_patterns: Optional[List[str]],
    response_snippet: str,
    queue_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one CONFIRMED finding to the queue and return the row that was written.

    Idempotent-ish: the same (program, method, url, vuln_type) combination is
    permitted multiple times; a repeat probe that keeps confirming still counts
    as one finding to write up, but re-running the tool in a later round after
    the report was drafted does not automatically re-open the finding. The
    caller (list_undrafted_findings) is what filters on drafted_report_path.

    Raises no exceptions on IO error -- returns a row with an "error" key
    instead, so a queue failure never poisons the api_request handler.
    """
    if classifier_verdict != "CONFIRMED":
        # Guard: this module only records CONFIRMED. Callers should not call it
        # for other verdicts. Being explicit makes accidental misuse loud.
        raise ValueError(
            f"findings_queue only records CONFIRMED verdicts, got {classifier_verdict!r}"
        )
    row = {
        "program": program or "unknown",
        "ts": time.time(),
        "method": (method or "GET").upper(),
        "url": url or "",
        "vuln_type": (vuln_type or "").lower(),
        "classifier_verdict": classifier_verdict,
        "matched_patterns": list(matched_patterns or []),
        "response_snippet": _scrub_snippet(response_snippet or ""),
        "drafted_report_path": None,
    }
    path = queue_path or _default_queue_path()
    with _lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            return {**row, "error": f"write_failed: {type(e).__name__}: {e}"}
    return row


def _load_all(queue_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read every row from the queue file. Returns [] if the file is missing."""
    path = queue_path or _default_queue_path()
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with _lock:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # A corrupt line does not silently swallow the whole queue.
                    rows.append({"error": "malformed_line", "raw": line[:200]})
    return rows


def list_undrafted_findings(
    program: Optional[str] = None,
    queue_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return rows with drafted_report_path is None, optionally filtered by program.

    Ordered by ts ascending so decide_action sees oldest undrafted first (the
    most likely to be forgotten as the rolling window rolls past it).
    """
    rows = _load_all(queue_path=queue_path)
    out = []
    for i, r in enumerate(rows):
        if r.get("drafted_report_path"):
            continue
        if "error" in r and "matched_patterns" not in r:
            # Skip corrupt lines from surfacing as findings; they will still
            # show up in _load_all() for diagnostic purposes.
            continue
        if program and r.get("program") != program:
            continue
        out.append({**r, "queue_index": i})
    out.sort(key=lambda r: r.get("ts", 0))
    return out


def mark_drafted(
    queue_index: int,
    drafted_report_path: str,
    queue_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Set drafted_report_path on the row at queue_index and rewrite the file.

    Rewrites the whole JSONL. For a queue that stays under a few thousand rows
    (a real bounty engagement produces tens of findings, not thousands) this is
    fine and keeps the code trivially auditable.
    """
    path = queue_path or _default_queue_path()
    with _lock:
        rows = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        rows.append({"error": "malformed_line", "raw": line[:200]})
        if queue_index < 0 or queue_index >= len(rows):
            return {"error": f"queue_index {queue_index} out of range (len={len(rows)})"}
        rows[queue_index]["drafted_report_path"] = drafted_report_path
        rows[queue_index]["drafted_ts"] = time.time()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, path)
        return rows[queue_index]


def draft_finding_report(
    queue_index: int,
    report_dir: Optional[str] = None,
    queue_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a Markdown report skeleton for one undrafted finding.

    The report itself is intentionally minimal -- a scaffold with the raw
    signal (endpoint, verdict, matched patterns, response snippet) already
    filled in. Sabrina (or a human) still writes the impact/repro/severity
    sections; this just guarantees the finding does not slip past session end
    without SOMETHING on disk to submit.
    """
    rows = _load_all(queue_path=queue_path)
    if queue_index < 0 or queue_index >= len(rows):
        return {"error": f"queue_index {queue_index} out of range (len={len(rows)})"}
    row = rows[queue_index]
    if row.get("drafted_report_path"):
        return {"error": "already_drafted", "path": row["drafted_report_path"], "row": row}
    if report_dir is None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        report_dir = os.path.join(project_root, "data", "reports")
    os.makedirs(report_dir, exist_ok=True)
    # Filename: <program>_<ts>_<vuln_type>.md
    safe_program = re.sub(r"[^A-Za-z0-9._-]", "_", row.get("program") or "unknown")
    safe_vuln = re.sub(r"[^A-Za-z0-9._-]", "_", row.get("vuln_type") or "unknown")
    ts_int = int(row.get("ts") or time.time())
    report_path = os.path.join(report_dir, f"{safe_program}_{ts_int}_{safe_vuln}.md")
    md = render_finding_report(row)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(md)
    updated = mark_drafted(queue_index, report_path, queue_path=queue_path)
    return {"drafted_report_path": report_path, "row": updated}


def render_finding_report(row: Dict[str, Any]) -> str:
    """Render the shared local draft format, with optional verified evidence."""
    details = row.get("report_details") or {}
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(row.get("ts") or 0))
    matched = row.get("matched_patterns") or []
    matched_lines = "\n".join(f"- `{m}`" for m in matched) if matched else "(none)"
    evidence = details.get("verified_evidence")
    if evidence is None:
        evidence_heading = "Response snippet (JWT-redacted, truncated to 1000 chars)"
        evidence_text = row.get("response_snippet", "")
    else:
        evidence_heading = "Verified evidence metadata"
        evidence_text = json.dumps(evidence, indent=2, ensure_ascii=True)
    detected_at = f"**Detected at:** {ts}\n" if evidence is None or row.get("ts") else ""
    title = details.get("title") or f"Finding: {row.get('vuln_type', '?')} on {row.get('program', '?')}"
    return f"""# {title}

**Program:** {row.get('program', '?')}
**Endpoint:** `{row.get('method', '?')} {row.get('url', '?')}`
**Vulnerability class:** {row.get('vuln_type', '?')}
{detected_at}**Deterministic verdict:** {row.get('classifier_verdict', '?')}

## Matched patterns

{matched_lines}

## {evidence_heading}

```
{evidence_text}
```

## Impact

{details.get('impact') or '_(fill in: what a real attacker can do with this)_'}

## Reproduction

{details.get('reproduction') or '_(fill in: exact steps / curl / burp request)_'}

## Suggested severity

{details.get('severity') or '_(fill in: CVSS or program-specific severity tier)_'}

## Suggested remediation

{details.get('remediation') or '_(fill in)_'}
"""


def queue_summary(
    program: Optional[str] = None,
    queue_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Cheap counts for decide_action's per-round prompt.

    Returns {total, undrafted, undrafted_for_program} so the caller can emit a
    one-line bullet like "you have 3 undrafted findings" without pulling in the
    full row list every round.
    """
    rows = _load_all(queue_path=queue_path)
    total = 0
    undrafted = 0
    undrafted_for_program = 0
    for r in rows:
        if "error" in r and "matched_patterns" not in r:
            continue
        total += 1
        if not r.get("drafted_report_path"):
            undrafted += 1
            if program and r.get("program") == program:
                undrafted_for_program += 1
    return {
        "total": total,
        "undrafted": undrafted,
        "undrafted_for_program": undrafted_for_program if program else undrafted,
    }
