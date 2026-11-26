"""
tools/engines/http_smuggling_gated.py
=====================================
Scope-gated, detection-only wrapper around Sabrina's existing HTTP request-
smuggling engine (ai_systems.http_smuggling.http_smuggler.HTTPSmuggler).

Why this exists: HTTPSmuggler is a powerful, general smuggling engine (CL.TE /
TE.CL / CL.0 / CL.CL). Pointing it straight at a host is exactly the kind of
live, potentially-disruptive traffic that must go through the one hard
authorization gate, EngagementGate, rather than trusting the caller. This module
is the ONLY sanctioned way the loop reaches HTTPSmuggler for a live target:

  run({"target_url": str, "scope_file": <path>, "mode": "detect", "path": "/"})

Contract (mirrors tests/unit/test_http_smuggling_gated.py):
  1. scope_file is REQUIRED; it builds EngagementGate from the loaded scope.
  2. GATE FIRST -- gate.check(target_url). Out-of-scope OR automated_testing_allowed
     false => {"scanned": False, "blocked_reason": ...} and HTTPSmuggler is never
     imported, constructed, or run.
  3. On pass: gate.throttle(), then lazily import + construct HTTPSmuggler(host,
     port, use_ssl=...) for the parsed target and run detection.
  4. HTTPSmuggler is imported LAZILY (inside run, after the gate) so an
     out-of-scope call never even imports it.
  5. mode "detect" (default) is signal-only: returns a boolean/confidence signal,
     NEVER captured request/response bodies belonging to other users (Tesla
     policy: detection-only, stop-and-report on others' data).
"""

import json
import os
from typing import Any, Dict
from urllib.parse import urlparse

from tools.engagement_scope import EngagementGate


def _load_scope(scope_file: str) -> Dict[str, Any]:
    with open(scope_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _target_parts(target_url: str):
    """(host, port, use_ssl) for the target. Defaults: https -> 443/ssl, http -> 80."""
    parsed = urlparse(target_url if "://" in target_url else f"//{target_url}")
    host = (parsed.hostname or "").lower().rstrip(".")
    use_ssl = (parsed.scheme or "https").lower() != "http"
    port = parsed.port or (443 if use_ssl else 80)
    return host, port, use_ssl


def _confirmed_signal(result: Any) -> bool:
    """Reduce a smuggler detection result to a single boolean signal WITHOUT surfacing
    any captured bodies. Handles both the real engine's return
    (Tuple[report_entries, summary, summary_str, findings]) and a simple
    {'confirmed': bool}-style dict."""
    if isinstance(result, dict):
        return bool(result.get("confirmed"))
    if isinstance(result, (tuple, list)):
        # Real perform_all_smuggling_tests -> (report_entries, summary, str, findings).
        # A non-empty findings list (last element) means a confirmed smuggling signal.
        for part in reversed(result):
            if isinstance(part, (list, tuple)):
                return len(part) > 0
        return False
    return bool(result)


def run(data: Dict[str, Any]) -> Dict[str, Any]:
    data = data or {}
    target_url = data.get("target_url") or ""
    scope_file = data.get("scope_file")
    mode = (data.get("mode") or "detect").lower()

    # 1. scope_file is REQUIRED -- fail closed with no scope.
    if not scope_file or not os.path.exists(str(scope_file)):
        return {"scanned": False,
                "blocked_reason": f"scope_file is required and must exist (got {scope_file!r}); "
                                  f"refusing to run the smuggler without an authorization scope."}

    try:
        scope = _load_scope(str(scope_file))
    except Exception as e:
        return {"scanned": False, "blocked_reason": f"could not load scope_file {scope_file!r}: {e}"}

    gate = EngagementGate(scope)

    # 2. GATE FIRST. No import/construct/run of HTTPSmuggler on failure.
    ok, reason = gate.check(target_url)
    if not ok:
        return {"scanned": False, "blocked_reason": reason}

    host, port, use_ssl = _target_parts(target_url)
    if not host:
        return {"scanned": False, "blocked_reason": f"could not parse a host from target_url {target_url!r}."}

    # 3. Respect the program's requested rate before any live traffic.
    gate.throttle()

    # 4. LAZY import -- only reached once the gate has passed. Imported via the module so
    #    the acceptance test's monkeypatch of HTTPSmuggler takes effect.
    import ai_systems.http_smuggling.http_smuggler as hs

    smuggler = hs.HTTPSmuggler(host, port, use_ssl=use_ssl)

    try:
        result = smuggler.perform_all_smuggling_tests()
    except Exception as e:
        return {"scanned": True, "confirmed": False, "target": host,
                "error": f"detection run failed: {type(e).__name__}: {e}"}

    confirmed = _confirmed_signal(result)

    # 5. Detection-only: boolean signal + target identity, NEVER captured bodies.
    return {"scanned": True, "confirmed": confirmed, "target": host, "mode": mode}
