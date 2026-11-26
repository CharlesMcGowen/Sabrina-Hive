"""Scope-bound HTTP request-smuggling sweep across a program's in-scope subdomains.

The missing link between Sabrina's pieces: crt.sh subdomain enum -> filter through THIS engagement's
EngagementGate (only in-scope hosts survive; out-of-scope carve-outs dropped) -> http_smuggling_engine
per host (timing-only, never auto-confirms), throttled to the engagement's rate limit. The scope_file is
bound for BOTH the filter and each engine call, so a caller can never point the sweep off-scope.

DEFAULT-SAFE: enumeration + in-scope filtering send ZERO traffic to the target (crt.sh is public
certificate-transparency data; the filter is pure logic). Active smuggling probes run ONLY when BOTH
(a) --active is passed AND (b) the engagement's automated_testing_allowed=true (the engine's own gate
re-checks this per host). So with a fresh/compliant scope file (automated_testing_allowed=false) this
prints the in-scope target list and stops -- nothing is sent to the target. Findings are timing LEADS
that require careful manual verification; the engine never performs the data-capturing exploit.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools.engagement_scope import EngagementGate  # noqa: E402


def _roots_from_scope(gate) -> list:
    """Registrable roots to enumerate: the domain of each '*.domain' wildcard + each exact host."""
    roots = set()
    for e in gate.in_scope:
        e = (e or "").strip()
        if not e:
            continue
        if e.startswith("*."):
            roots.add(e[2:].lower().rstrip("."))
        else:
            h = urlparse(e if "://" in e else f"//{e}").hostname
            if h:
                roots.add(h.lower().rstrip("."))
    return sorted(roots)


def _crtsh(root: str, timeout: int = 25, fetch=None) -> set:
    """Passive subdomain enumeration via crt.sh certificate-transparency logs (no target traffic)."""
    url = f"https://crt.sh/?q=%.{root}&output=json"
    subs = set()
    try:
        if fetch is not None:
            data = fetch(root)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Security-Recon/1.0)"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
        for entry in data:
            for part in (entry.get("name_value", "") or "").split("\n"):
                d = part.strip().lower().lstrip("*.")
                if d and d.endswith(root) and " " not in d:
                    subs.add(d)
    except Exception as ex:
        print(f"[sweep] crt.sh error for {root}: {ex}", flush=True)
    return subs


def enumerate_in_scope(gate, fetch=None) -> list:
    """All crt.sh subdomains that pass THIS engagement's gate (in-scope, not carved out)."""
    hosts = set()
    for root in _roots_from_scope(gate):
        for sub in _crtsh(root, fetch=fetch):
            ok, _ = gate.check_url(f"https://{sub}/")
            if ok:
                hosts.add(sub)
    return sorted(hosts)


def sweep(scope_file: str, active: bool = False, max_hosts: int = 100, rps=None, path: str = "/",
          fetch=None, smuggle=None, verbose: bool = True):
    scope = json.load(open(scope_file, encoding="utf-8"))
    gate = EngagementGate(scope)
    hosts = enumerate_in_scope(gate, fetch=fetch)
    if verbose:
        print(f"[sweep] program={gate.program}: {len(hosts)} in-scope hosts after gate filter", flush=True)
    out_dir = REPO_ROOT / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"smuggle_targets_{gate.program}.txt").write_text("\n".join(hosts), encoding="utf-8")

    if not active:
        if verbose:
            print("[sweep] enumeration only -- no traffic sent to the target. Re-run with --active after "
                  "setting automated_testing_allowed=true to run timing-only smuggling probes.", flush=True)
        return {"program": gate.program, "hosts": hosts, "active": False}

    # Active: each probe is STILL re-gated by the engine (automated_testing_allowed + in-scope url),
    # so this no-ops safely until the flag is deliberately set true.
    if smuggle is None:
        from tools.engines.http_smuggling_engine import run as smuggle
    rate = rps or gate.rate_limit_rps or 0.5
    interval = (1.0 / rate) if rate else 0.0
    leads, blocked = [], 0
    for h in hosts[:max_hosts]:
        if interval:
            time.sleep(interval)
        res = smuggle({"target_url": f"https://{h}{path}", "path": path, "scope_file": scope_file})
        if res.get("blocked_reason"):
            blocked += 1
        elif res.get("confirmed"):
            leads.append({"host": h, **res})
        if verbose:
            tag = "SIGNAL" if res.get("confirmed") else (("blocked:" + res.get("blocked_reason", "")[:40]) if res.get("blocked_reason") else "no-signal")
            print(f"  {h}: {tag}", flush=True)
    rep = out_dir / f"smuggle_leads_{gate.program}.json"
    rep.write_text(json.dumps(leads, indent=2), encoding="utf-8")
    if verbose:
        print(f"[sweep] {len(leads)} timing lead(s), {blocked} gate-blocked -> {rep}. Leads need MANUAL, "
              f"low-volume verification; the engine never auto-confirms or captures other users' data.", flush=True)
    return {"program": gate.program, "hosts": hosts, "leads": leads, "blocked": blocked, "active": True}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Scope-bound HTTP smuggling sweep over in-scope subdomains.")
    ap.add_argument("--scope-file", required=True)
    ap.add_argument("--active", action="store_true",
                    help="Run timing-only smuggling probes (needs automated_testing_allowed=true in the scope).")
    ap.add_argument("--max-hosts", type=int, default=100)
    ap.add_argument("--rps", type=float, default=None)
    ap.add_argument("--path", default="/")
    a = ap.parse_args()
    sweep(a.scope_file, active=a.active, max_hosts=a.max_hosts, rps=a.rps, path=a.path)
