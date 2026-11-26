"""Local, controlled HTTP request-smuggling SIMULATION -- the ground-truth arena for RSI on the
smuggling detector. Everything binds to 127.0.0.1; no external traffic, ever.

A real desync can't be safely reproduced against someone else's infra, but the SIGNAL a timing-based
detector keys on can be reproduced deterministically: a back-end that trusts Transfer-Encoding while a
front-end trusted Content-Length "waits for more (smuggled) bytes" and is SLOW to answer an ambiguous
CL+TE request, while a normal request answers fast. So a vulnerable server here HANGS ~vuln_delay on the
exact ambiguous probe the detector sends, and a safe server answers everything instantly.

Ground truth for scoring a detector:
  - "vulnerable"      : desyncs (hangs) on ANY request carrying both Content-Length and a chunked
                        Transfer-Encoding -> a correct detector must FLAG it.
  - "vulnerable_obf"  : desyncs ONLY when the Transfer-Encoding header is OBFUSCATED (e.g. a space
                        before the colon, a tab, or a duplicated TE header) -- the plain probe does NOT
                        trigger it. The current engine sends only a plain "Transfer-Encoding: chunked",
                        so it MISSES this -> real headroom: improving the detector to send obfuscated
                        TE variants is what catches it.
  - "safe"            : answers everything instantly -> a correct detector must NOT flag it.
  - "safe_slow"       : answers everything slowly (baseline AND probe equally slow) -> a detector that
                        compares to a baseline correctly sees NO delta and must NOT flag it (a naive
                        absolute-timeout detector would false-positive here).

score_detector() runs a detector over the suite and returns accuracy -- the RSI objective function.
"""

import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TE_CHUNKED = re.compile(rb"transfer-encoding\s*:\s*[^\r\n]*chunked", re.I)
_TE_OBFUSCATED = re.compile(rb"(transfer-encoding\s+:|transfer-encoding:\t|transfer-encoding:[^\r\n]*\r\ntransfer-encoding)", re.I)
_HAS_CL = re.compile(rb"\r\ncontent-length\s*:", re.I)


def _is_ambiguous(headers: bytes) -> bool:
    return bool(_TE_CHUNKED.search(headers) and _HAS_CL.search(b"\r\n" + headers))


def _is_obfuscated_te(headers: bytes) -> bool:
    return bool(_TE_OBFUSCATED.search(headers))


class SmuggleSim:
    """A localhost raw-socket HTTP server that reproduces a smuggling TIMING signal per `mode`.
    Use as a context manager: `with SmuggleSim("vulnerable") as s: ... s.host, s.port`."""

    def __init__(self, mode: str = "vulnerable", vuln_delay: float = 7.0, safe_slow_delay: float = None):
        self.mode = mode
        self.vuln_delay = vuln_delay
        self.safe_slow_delay = safe_slow_delay if safe_slow_delay is not None else vuln_delay
        self.host = "127.0.0.1"
        self.port = None
        self._sock = None
        self._thread = None
        self._stop = threading.Event()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *a):
        self.stop()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self._sock.settimeout(0.3)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
            headers = data.split(b"\r\n\r\n", 1)[0]
            desync = False
            if self.mode == "vulnerable":
                desync = _is_ambiguous(headers)
            elif self.mode == "vulnerable_obf":
                desync = _is_ambiguous(headers) and _is_obfuscated_te(headers)
            elif self.mode == "safe_slow":
                desync = False  # slow, but for EVERY request (see below)
            # else "safe": never desync

            if self.mode == "safe_slow":
                self._stop.wait(self.safe_slow_delay)  # baseline AND probe equally slow -> no delta
                self._respond(conn)
            elif desync:
                self._stop.wait(self.vuln_delay)        # the smuggling timing signal
                self._respond(conn)
            else:
                self._respond(conn)                     # fast, normal
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _respond(self, conn, code: int = 200, body: str = "ok"):
        try:
            conn.sendall((f"HTTP/1.1 {code} OK\r\nContent-Length: {len(body)}\r\n"
                          f"Connection: close\r\n\r\n{body}").encode())
        except Exception:
            pass

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except Exception:
            pass


# Ground-truth suite: (mode, is_vulnerable). A perfect detector flags the vulnerable ones and not the
# safe ones. The current engine is expected to MISS "vulnerable_obf" (headroom for RSI).
SUITE = [("vulnerable", True), ("vulnerable_obf", True), ("safe", False), ("safe_slow", False)]


def _localhost_scope_file() -> str:
    """A temp scope authorizing only 127.0.0.1 (local lab -> automation implicitly allowed)."""
    fd, path = tempfile.mkstemp(prefix="smuggle_sim_scope_", suffix=".json")
    os.write(fd, json.dumps({"platform": "local_lab", "program": "smuggle_sim",
                             "in_scope": ["127.0.0.1"], "out_of_scope": [],
                             "automated_testing_allowed": True, "rate_limit_rps": 0}).encode())
    os.close(fd)
    return path


def _default_detector(target_url, scope_file):
    from tools.engines.http_smuggling_engine import run as engine_run
    return engine_run({"target_url": target_url, "scope_file": scope_file, "path": "/"})


def _is_flagged(result: dict) -> bool:
    """A detector 'flags' a target if it reports a technique needing review or any vuln classification."""
    if result.get("techniques_needing_manual_review"):
        return True
    for p in (result.get("probes") or {}).values():
        if p.get("classification") == "TIMING_SIGNAL_LIKELY_VULNERABLE":
            return True
    return bool(result.get("confirmed"))


def score_detector(detector=None, suite=SUITE, vuln_delay: float = 7.0, verbose: bool = True):
    """RSI OBJECTIVE: run `detector(target_url, scope_file)` against the sim suite; return accuracy and
    per-case detail. detector defaults to the current http_smuggling_engine.run. Localhost only."""
    detector = detector or _default_detector
    scope_file = _localhost_scope_file()
    rows, correct = [], 0
    try:
        for mode, is_vuln in suite:
            with SmuggleSim(mode, vuln_delay=vuln_delay) as sim:
                res = detector(f"http://{sim.host}:{sim.port}/", scope_file)
            flagged = _is_flagged(res)
            ok = (flagged == is_vuln)
            correct += int(ok)
            rows.append({"mode": mode, "is_vulnerable": is_vuln, "flagged": flagged, "correct": ok})
            if verbose:
                print(f"  [{'OK ' if ok else 'XX '}] {mode:16s} truth={is_vuln!s:5s} flagged={flagged}", flush=True)
    finally:
        try:
            os.unlink(scope_file)
        except Exception:
            pass
    acc = correct / len(suite) if suite else 0.0
    if verbose:
        print(f"[smuggle_sim] detector accuracy = {correct}/{len(suite)} = {acc:.0%}", flush=True)
    return {"accuracy": acc, "correct": correct, "total": len(suite), "cases": rows}


if __name__ == "__main__":
    print("Scoring the current http_smuggling_engine against the local sim suite "
          "(vulnerable hosts hang ~7s, so this takes ~30s)...", flush=True)
    score_detector()
