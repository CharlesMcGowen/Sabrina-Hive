"""Scope-gated HTTP for exploit/recon engines -- the CORRECT shared guard.

Every engine that sends traffic to a target must route it through gate_guarded_request so an
out-of-scope host can never be hit. This replaces an earlier auto-generated version that did NOT
actually gate anything:
  * it did `if not gate.check_url(url)` -- but check_url returns a (bool, reason) TUPLE, which is
    always truthy, so the abort branch was dead code and every request went through; and
  * it built the gate from a per-call `scope=[]` kwarg, which an engine could leave empty (or wrong)
    -- i.e. the caller got to decide its own scope, defeating the point.

DESIGN (fail-closed, non-spoofable):
  - The authorized scope comes ONLY from a TRUSTED channel the orchestrator controls, never from a
    caller argument: env SABRINA_ACTIVE_SCOPE_FILE (path to a scope JSON) or SABRINA_ACTIVE_SCOPE_JSON
    (inline JSON). An engine (or a model driving one) cannot widen its own scope.
  - If no authorized scope is configured, we FAIL CLOSED: the request is refused. An engine running
    outside an authorized engagement sends no traffic.
  - The real check is `ok, reason = gate.check(url)` -- automated_testing_allowed authorization
    THEN out_of_scope precedence + in_scope allowlist, exactly as the api_request tool enforces it.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from tools.engagement_scope import EngagementGate

REPO_ROOT = Path(__file__).resolve().parent.parent


class GateBlocked(Exception):
    """Raised (by gate_guarded_request(..., raise_on_block=True)) when a URL is out of scope."""


def _load_authorized_scope() -> Optional[Dict[str, Any]]:
    """Load the session's authorized scope from the trusted env channel only. None => fail closed."""
    inline = os.environ.get("SABRINA_ACTIVE_SCOPE_JSON")
    if inline:
        try:
            data = json.loads(inline)
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    path = os.environ.get("SABRINA_ACTIVE_SCOPE_FILE")
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = REPO_ROOT / p
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def is_url_allowed(url: str):
    """(allowed: bool, reason: str) for `url` under the current authorized scope. Fail-closed."""
    scope = _load_authorized_scope()
    if scope is None:
        return False, ("BLOCKED: no authorized engagement scope configured "
                       "(set SABRINA_ACTIVE_SCOPE_FILE). Refusing to send request.")
    # check() = automated_testing_allowed authorization + scope (out_of_scope precedence +
    # in_scope allowlist). Use the COMBINED check, not check_url() alone: an active exploit
    # engine must not fire at an in-scope host when automated_testing_allowed is False.
    return EngagementGate(scope).check(url)


def gate_guarded_request(method, url, raise_on_block=False, **kwargs):
    """requests.request(method, url, **kwargs), but only if `url` is in the authorized scope.

    Any caller-supplied 'scope' kwarg is IGNORED (scope is trusted-channel only) and stripped so it
    never reaches requests. On an out-of-scope/unauthorized URL: log + return None (or raise
    GateBlocked if raise_on_block=True) -- never sends the request.
    """
    kwargs.pop("scope", None)  # caller scope is not trusted; drop it
    allowed, reason = is_url_allowed(url)
    if not allowed:
        print(f"[gate] {reason}  ({method} {url})", flush=True)
        if raise_on_block:
            raise GateBlocked(reason)
        return None
    return requests.request(method, url, **kwargs)
