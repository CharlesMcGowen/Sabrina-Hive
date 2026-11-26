"""
tools/engines/http_smuggling_engine.py
========================================
HTTP Request Smuggling (CL.TE / TE.CL) detection engine.

Usage (same convention as every other engine in this folder):
    python tools/engines/http_smuggling_engine.py '{"target_url": "https://host", "path": "/", "scope_file": "config/engagements/acme.json"}'

WHY THIS ENGINE IS DIFFERENT FROM THE OTHERS IN THIS FOLDER:

1. Raw sockets, not `requests`. Smuggling detection depends on sending a
   Content-Length and a Transfer-Encoding header that DISAGREE with each other
   (that's the whole point -- the front-end and back-end server must disagree
   about where the request body ends). `requests`/`http.client` will refuse to
   send that, or silently "fix" it. This engine opens a raw TCP (or TLS)
   socket and writes the exact bytes.

2. Detection-only, not exploitation. There are two escalating techniques in
   real request-smuggling research:
     (a) TIMING-BASED DETECTION (what this engine does): send ONE ambiguous
         request on ONE connection you own, and observe whether the response
         hangs. This only affects your own probe connection.
     (b) RESPONSE-QUEUE POISONING / DESYNC EXPLOITATION (deliberately NOT
         implemented here): appending a second, "smuggled" request designed
         to get prepended to some OTHER visitor's response on a shared
         backend connection. This is where request smuggling testing can
         cause real collateral damage to other users of the target's
         infrastructure -- several bug-bounty programs' policies explicitly
         restrict or require prior written approval for this. This engine
         never does it. A CONFIRMED-looking timing signal here still requires
         manual, careful, single-target verification before it's reported --
         treat "timing_signal" as a lead, not a finding.

3. Embedded scope gate. This engine is invoked via run_shell, which (per
   AGENTS.md's "Engagement Scope" section) has NO code-level scope gate the
   way api_request does -- run_shell doesn't go through execute_tool's
   in_scope/automated_testing_allowed check at all. Given how much more
   consequential a botched smuggling probe is than a normal GET, this engine
   loads the scope file itself and refuses to open a single socket if the
   target isn't authorized. Don't rely on the caller to have checked.
"""

import sys
import json
import time
import socket
import ssl
from urllib.parse import urlsplit

import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.engagement_scope import EngagementGate


CONNECT_TIMEOUT = 10
READ_TIMEOUT = 10          # max time to wait for a probe response before calling it a timeout
DELAY_THRESHOLD_S = 5.0    # probe response must be at least this much slower than baseline to count as a signal


def _split_target(target_url: str, path: str):
    parts = urlsplit(target_url)
    scheme = parts.scheme or "https"
    host = parts.hostname
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _open_socket(scheme: str, host: str, port: int) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    if scheme == "https":
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.settimeout(READ_TIMEOUT)
    return sock


def _send_raw(scheme: str, host: str, port: int, request_bytes: bytes):
    """
    Opens a fresh connection, sends the raw request bytes, and measures how
    long it takes to get ANY response data back (or a timeout/reset).
    A new connection per probe -- never reused -- so this probe can't leak
    into a second, unrelated request on the same socket.
    """
    start = time.monotonic()
    outcome = {"status": None, "elapsed_s": None, "note": ""}
    sock = None
    try:
        sock = _open_socket(scheme, host, port)
        sock.sendall(request_bytes)
        data = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                # Enough to read the status line; no need to wait for a full body.
                if b"\r\n" in data:
                    break
        except socket.timeout:
            outcome["status"] = "TIMEOUT"
            outcome["elapsed_s"] = time.monotonic() - start
            outcome["note"] = "No response within READ_TIMEOUT -- classic smuggling desync signal."
            return outcome

        outcome["elapsed_s"] = time.monotonic() - start
        if data:
            status_line = data.split(b"\r\n", 1)[0].decode(errors="replace")
            outcome["status"] = status_line
        else:
            outcome["status"] = "CONNECTION_CLOSED_NO_DATA"
        return outcome
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
        outcome["status"] = "CONNECTION_RESET"
        outcome["elapsed_s"] = time.monotonic() - start
        outcome["note"] = str(e)
        return outcome
    except Exception as e:
        outcome["status"] = "ERROR"
        outcome["elapsed_s"] = time.monotonic() - start
        outcome["note"] = str(e)
        return outcome
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _baseline_request(host: str, path: str) -> bytes:
    body = b"a=1"
    return (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body


def _cl_te_probe(host: str, path: str) -> bytes:
    # Ambiguous body: valid as a zero-length chunked body ("0\r\n\r\n"), with one
    # extra trailing byte "X" that only a CONTENT-LENGTH-trusting parser will
    # include as part of "the body". Content-Length is computed from the
    # actual bytes below, not hardcoded, so this can't be off-by-one.
    smuggle_body = b"0\r\n\r\nX"
    return (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(smuggle_body)}\r\n"
        f"Transfer-Encoding: chunked\r\n"
        f"\r\n"
    ).encode() + smuggle_body


def _cl_te_probe_obfuscated(host: str, path: str, te_header_line: str) -> bytes:
    # Same ambiguous CL.TE body as _cl_te_probe, but the Transfer-Encoding header
    # is OBFUSCATED so it slips past a front-end that only normalizes the exact
    # canonical "Transfer-Encoding: chunked" while the back-end still parses it as
    # chunked -- the classic obfuscated-TE desync that a plain probe misses. The
    # header line is passed in verbatim (e.g. a space before the colon, a tab
    # after it, or a duplicated Transfer-Encoding header). Content-Length is still
    # present, so the request stays genuinely ambiguous.
    smuggle_body = b"0\r\n\r\nX"
    return (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: {len(smuggle_body)}\r\n"
        f"{te_header_line}\r\n"
        f"\r\n"
    ).encode() + smuggle_body


# Obfuscated Transfer-Encoding header lines that still parse as chunked on a
# lenient back-end. Each is a separate probe variant so a target that only
# desyncs on an obfuscated TE (but not the canonical one) is still caught.
_OBFUSCATED_TE_VARIANTS = {
    "CL.TE-obf-space": "Transfer-Encoding : chunked",                          # space before the colon
    "CL.TE-obf-tab": "Transfer-Encoding:\tchunked",                            # tab after the colon
    "CL.TE-obf-dup": "Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked",  # duplicated header
}


def _te_cl_probe(host: str, path: str) -> bytes:
    # Valid chunked body (one 8-byte chunk "SMUGGLED" + terminating chunk) --
    # a TE-trusting parser reads the whole thing correctly. Content-Length is
    # deliberately much shorter than the true body, so a CL-trusting parser
    # only consumes the first 3 bytes ("8\r\n") and treats the rest as the
    # start of a second, incomplete pipelined request.
    chunk_data = b"SMUGGLED"
    chunked_body = f"{len(chunk_data):x}\r\n".encode() + chunk_data + b"\r\n0\r\n\r\n"
    return (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\n"
        f"Content-Length: 3\r\n"
        f"Transfer-Encoding: chunked\r\n"
        f"\r\n"
    ).encode() + chunked_body


def _classify(baseline_elapsed: float, probe_outcome: dict) -> str:
    if probe_outcome["status"] == "TIMEOUT":
        return "TIMING_SIGNAL_LIKELY_VULNERABLE"
    if probe_outcome["status"] in ("CONNECTION_RESET", "ERROR"):
        return "INCONCLUSIVE"
    if probe_outcome["status"] == "CONNECTION_CLOSED_NO_DATA":
        return "INCONCLUSIVE"
    # Got a real status line quickly.
    elapsed = probe_outcome["elapsed_s"] or 0
    if elapsed >= baseline_elapsed + DELAY_THRESHOLD_S:
        return "TIMING_SIGNAL_LIKELY_VULNERABLE"
    if probe_outcome["status"].startswith("HTTP/1.1 400") or probe_outcome["status"].startswith("HTTP/1.1 501"):
        return "REJECTED_BY_SERVER"
    return "NO_SIGNAL"


def run(data: dict) -> dict:
    target_url = data.get("target_url", "")
    path = data.get("path", "/")
    scope_file = data.get("scope_file")

    if not scope_file:
        return {"confirmed": False, "blocked_reason": "No 'scope_file' provided -- refusing to probe without an explicit engagement scope to check against."}

    if not os.path.isfile(scope_file):
        return {"confirmed": False, "blocked_reason": f"scope_file '{scope_file}' not found."}

    with open(scope_file, "r", encoding="utf-8") as f:
        scope = json.load(f)
    gate = EngagementGate(scope)

    gate_ok, gate_reason = gate.check(target_url)
    if not gate_ok:
        return {"confirmed": False, "blocked_reason": gate_reason}

    scheme, host, port = _split_target(target_url, path)
    if not host:
        return {"confirmed": False, "blocked_reason": f"Could not parse host from target_url '{target_url}'."}

    results = {"confirmed": False, "target": f"{scheme}://{host}:{port}{path}", "probes": {}}

    gate.throttle()
    baseline = _send_raw(scheme, host, port, _baseline_request(host, path))
    baseline_elapsed = baseline.get("elapsed_s") or 1.0
    results["baseline"] = baseline

    probe_specs = [
        ("CL.TE", _cl_te_probe(host, path)),
        ("TE.CL", _te_cl_probe(host, path)),
    ]
    # Obfuscated-TE variants: catch a target that only desyncs when the
    # Transfer-Encoding header is non-canonical (space before colon / tab / dup).
    for name, te_line in _OBFUSCATED_TE_VARIANTS.items():
        probe_specs.append((name, _cl_te_probe_obfuscated(host, path, te_line)))

    for technique, request_bytes in probe_specs:
        gate.throttle()
        outcome = _send_raw(scheme, host, port, request_bytes)
        classification = _classify(baseline_elapsed, outcome)
        results["probes"][technique] = {**outcome, "classification": classification}
        if classification == "TIMING_SIGNAL_LIKELY_VULNERABLE":
            results["confirmed"] = False  # deliberately never auto-confirmed -- see module docstring
            results.setdefault("techniques_needing_manual_review", []).append(technique)

    if results.get("techniques_needing_manual_review"):
        results["summary"] = (
            f"Timing signal(s) suggesting possible {', '.join(results['techniques_needing_manual_review'])} "
            f"desync on {host}. This is a LEAD, not a confirmed finding -- verify manually with a single, "
            f"careful, isolated request before reporting. Do not scale this up to repeated/parallel probing "
            f"against the same host; a false positive here is often just network jitter or a slow WAF, but "
            f"repeated ambiguous requests against a real desync-vulnerable target risks affecting other users."
        )
    else:
        results["summary"] = f"No smuggling timing signal observed on {host} for CL.TE or TE.CL."

    return results


def main():
    if len(sys.argv) == 2:
        raw_input = sys.argv[1]
    elif not sys.stdin.isatty():
        raw_input = sys.stdin.read().strip()
    else:
        raise ValueError("Please provide a JSON string as the first argument or via stdin.")
    if not raw_input:
        raise ValueError("Empty JSON input received.")
    data = json.loads(raw_input)
    output = run(data)
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
