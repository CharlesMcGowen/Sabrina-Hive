"""
tools/engagement_scope.py
==========================
Hard scope/authorization gate for a bug-bounty or lab engagement.

sabrina_gemini_loop.py previously only checked url.startswith() against
scope["in_scope"] inside the api_request tool handler. Two real gaps in that:

1. scope["out_of_scope"] was read into juiceshop_scope.json's schema but never
   actually checked anywhere -- an out_of_scope entry was pure documentation
   with zero enforcement.
2. There was no concept of "is automated/scripted testing even allowed on this
   program" or "what request rate is this program's policy asking for" -- both
   of which real bug-bounty programs (Bugcrowd/HackerOne) specify per-program,
   and both of which matter a lot more once Sabrina points at a live customer
   program instead of a local Juice Shop lab (where the user IS the only
   stakeholder and unrestricted automated testing against localhost is fine).

EngagementGate centralizes both checks so there is exactly one place that
decides "can this URL be hit right now", mirroring how PROTECTED_PATHS in
sabrina_gemini_loop.py is the one place that decides "can this file be
written" -- a hard, non-LLM-judgment gate rather than a prompt rule that can
be lost in a long context window.

Fails closed: a real (non local_lab) program with automated_testing_allowed
missing or false blocks every live request, not just a warning.
"""

import time
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse


def _hostname_of(entry: str) -> str:
    """Extract the real hostname a scope-file entry refers to, or '' if it isn't a real
    host at all (a prose/notes entry, or a "*.domain" wildcard -- those are handled by
    _entry_matches, not here)."""
    entry = entry.strip()
    if not entry or entry.startswith("*."):
        return ""
    parsed = urlparse(entry if "://" in entry else f"//{entry}")
    return (parsed.hostname or "").lower().rstrip(".")


def _url_parts(url: str) -> Tuple[str, str]:
    """(hostname, path) of a URL, lowercased host, path defaulting to '/'."""
    parsed = urlparse(url if "://" in url else f"//{url}")
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or "/"
    return host, path


def _entry_matches(host: str, path: str, entry: str) -> bool:
    """Does the request (host, path) match a single scope-file entry?

    Supports three real bug-bounty entry forms (finally enforcing what the old code punted on):
      - exact host:            "api.tesla.com"            -> host == entry
      - wildcard subdomain:    "*.tesla.com"              -> host == tesla.com OR host endswith ".tesla.com"
      - host + path prefix:    "service.tesla.com/docs/*" -> host == service.tesla.com AND path startswith /docs/
    Prose/notes entries (no parseable host) match nothing. Wildcard uses a leading-dot suffix test so
    lookalikes are rejected: "*.tesla.com" does NOT match tesla.com.evil.com or eviltesla.com."""
    entry = (entry or "").strip()
    if not entry:
        return False
    if entry.startswith("*."):
        dom = entry[2:].strip().lower().rstrip(".")
        return bool(dom) and (host == dom or host.endswith("." + dom))
    # host[/path] form
    ehost, epath = _url_parts(entry)
    if not ehost:
        return False  # prose / unparseable
    if epath and epath not in ("/", ""):
        prefix = epath.rstrip("*").rstrip("/")
        return host == ehost and (path.startswith(prefix) if prefix else True)
    return host == ehost


class EngagementGate:
    def __init__(self, scope: Optional[Dict[str, Any]]):
        self.scope: Dict[str, Any] = scope or {}
        self.platform: str = (self.scope.get("platform") or "").lower()
        self.program: str = self.scope.get("program") or "unknown"
        self.in_scope = self.scope.get("in_scope") or []
        self.out_of_scope = self.scope.get("out_of_scope") or []
        self.automated_testing_allowed: bool = bool(self.scope.get("automated_testing_allowed", False))
        self.rate_limit_rps: float = float(self.scope.get("rate_limit_rps") or 0) or None
        self._lock = threading.Lock()
        self._last_request_ts: float = 0.0

        # Local lab engagements (the user's own infrastructure, e.g. Juice Shop)
        # are implicitly self-authorized -- there's no third-party policy to
        # violate. Anything else (bugcrowd/hackerone/intigriti/direct/etc.)
        # requires the flag to be set explicitly after the program's actual
        # policy has been read, so a freshly-created scope file defaults to
        # blocking live traffic rather than silently allowing it.
        #
        # CRITICAL: "" (empty platform) used to be included in this trusted set,
        # which meant running sabrina_gemini_loop.py with NO --scope-file at all
        # (self.scope == {}) silently granted automated_testing_allowed=True and
        # in_scope=[] (no restriction) for EVERY url -- verified live: a real
        # session with no scope file loaded proposed sending api_request calls
        # directly at bugcrowd.com's own infrastructure, and check() returned
        # True. "No scope configured" must fail closed like any other
        # unrecognized platform, not be silently treated as a trusted local lab.
        # Only an EXPLICIT platform of local_lab/local/lab gets the implicit trust.
        if self.platform in ("local_lab", "local", "lab"):
            self.automated_testing_allowed = True if "automated_testing_allowed" not in self.scope else self.automated_testing_allowed

    def has_scope(self) -> bool:
        return bool(self.scope)

    def check_automation_allowed(self) -> Tuple[bool, str]:
        if not self.automated_testing_allowed:
            return False, (
                f"BLOCKED: engagement '{self.program}' (platform={self.platform or 'unknown'}) does not have "
                f"automated_testing_allowed=true in its scope file. Real bug-bounty programs frequently "
                f"restrict or prohibit automated scanning -- verify the program's actual policy page and "
                f"set automated_testing_allowed:true in the scope file only after confirming it permits this. "
                f"Refusing to send live traffic until then."
            )
        return True, ""

    def check_url(self, url: str) -> Tuple[bool, str]:
        """Live-confirmed 2026-09-19: the previous version of this check used raw
        url.startswith(prefix) string matching, which is a classic domain-allowlist bypass --
        "https://financeiro.hostgator.com.br.attacker-controlled.evil/phish" and
        "https://financeiro.hostgator.com.brXnotreallyhostgator.com/" both PASSED the in_scope
        check, because they textually start with the in_scope entry even though their real
        hostname is a completely different, uncontrolled domain. Now compares actual parsed
        hostnames instead. Note: "*.domain" wildcard entries in a scope file's in_scope/
        out_of_scope lists (and plain prose/notes entries) still never match anything here --
        same limitation as before this fix, not newly introduced by it. Making wildcards
        actually match would require deciding precedence against more-specific exact entries
        (e.g. hostgator-latam-bb.json's "*.hostgator.com.br" out_of_scope wildcard would, if
        literally enforced, also block the two subdomains its OWN in_scope list explicitly
        authorizes) -- a real behavior change to the live scope gate that needs a human decision,
        not something to silently ship inside a bypass fix.

        WILDCARDS + PRECEDENCE (fixed 2026 -- the human decision the note above deferred): real
        programs like Tesla scope "*.tesla.com" IN and carve specific hosts/paths OUT. Enforced in order:
          1. out_of_scope WINS. If (host, path) matches any out_of_scope entry -- exact host,
             "*.sub.domain" wildcard, or "host/path/*" prefix -- BLOCK, even if it also matches an
             in_scope wildcard. (So "*.tesla.com" in + "engage.tesla.com"/"*.engage.tesla.com" out
             blocks every engage host; "service.tesla.com/docs/*" out blocks only that path.)
          2. in_scope allowlist. With a non-empty in_scope, host must match some entry (exact or "*.").
             Wildcard match is a leading-dot suffix test, so lookalikes (tesla.com.evil.com,
             eviltesla.com) never match.
        """
        if not url:
            return False, "BLOCKED: empty URL."

        host, path = _url_parts(url)
        if not host:
            return False, f"BLOCKED: could not parse a real hostname from {url!r}."

        for entry in self.out_of_scope:
            if _entry_matches(host, path, entry):
                return False, f"BLOCKED: {url} (host={host}) matches the engagement's out_of_scope list ({entry}). Refusing to send request."

        if self.in_scope and not any(_entry_matches(host, path, entry) for entry in self.in_scope):
            return False, f"BLOCKED: {url} (host={host}) is not within the engagement's in_scope list ({self.in_scope}). Refusing to send request."

        return True, ""

    def check(self, url: str) -> Tuple[bool, str]:
        """Combined authorization + scope check. Call before any live request."""
        ok, reason = self.check_automation_allowed()
        if not ok:
            return ok, reason
        return self.check_url(url)

    def throttle(self):
        """Blocks the calling thread as needed to respect rate_limit_rps, if set."""
        if not self.rate_limit_rps:
            return
        min_interval = 1.0 / self.rate_limit_rps
        with self._lock:
            now = time.monotonic()
            wait = (self._last_request_ts + min_interval) - now
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def summary(self) -> str:
        return (
            f"platform={self.platform or 'unknown'} program={self.program} "
            f"automated_testing_allowed={self.automated_testing_allowed} "
            f"rate_limit_rps={self.rate_limit_rps or 'none'} "
            f"in_scope={len(self.in_scope)} out_of_scope={len(self.out_of_scope)}"
        )
