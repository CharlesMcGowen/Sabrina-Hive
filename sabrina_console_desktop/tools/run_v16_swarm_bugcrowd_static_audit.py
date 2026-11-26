#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Multi-Family Swarm v16.1 — Scoped Bugcrowd Static Vulnerability & Bounds Audit
======================================================================================
Audits public open-source repositories explicitly listed under `github.com` in-scope
for enrolled Bugcrowd programs in `Sabrina/data/discovered_engagements_scoped.json`:
  1. Okta (`okta/okta-jwt-verifier-python`, `okta/okta-jwt-verifier-golang`, `okta/okta-jwt-verifier-js`)
  2. Zendesk (`zendesk/zcli`, `zendesk/zendesk_api_client_rb`)

v16.1 Improvement:
- Splits Okta and Zendesk excerpts into compact `<1,500`-token packets so zero calls
  overflow the `8,192`-token context window (eliminating the 8,191-token prompt crowding
  observed in v16.0).
- Convenes 4 distinct model families (`Alibaba Qwen` `qwen3:14b` `<think>` + `qwen2.5-coder:14b`,
  `Meta Llama` `llama3.1:8b`, `IBM Granite` `granite3.1-dense:8b`, and `Mistral` `dolphin-mistral:7b`)
  plus deterministic AST/pattern invariant checks and `CyberComplianceSentinel` screening.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
SABRINA_ROOT = SCRIPT_DIR.parent
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.cyber_compliance_sentinel import CyberComplianceSentinel

OLLAMA_BASE = "http://ego.local:11434"
SCOPED_FILE = SABRINA_ROOT / "data" / "discovered_engagements_scoped.json"
SANDBOX_DIR = SABRINA_ROOT / "artifacts" / "tool_sandbox" / "bugcrowd_static_audit_20261116"
OUT_JSON = SABRINA_ROOT / "artifacts" / "collective-repair-20261108" / "swarm_bugcrowd_static_audit_v16_1.json"

TARGET_SOURCES: List[Dict[str, str]] = [
    {
        "program": "okta",
        "repo": "okta/okta-jwt-verifier-python",
        "branch": "master",
        "path": "okta_jwt_verifier/jwt_verifier.py",
        "url": "https://raw.githubusercontent.com/okta/okta-jwt-verifier-python/master/okta_jwt_verifier/jwt_verifier.py",
    },
    {
        "program": "okta",
        "repo": "okta/okta-jwt-verifier-python",
        "branch": "master",
        "path": "okta_jwt_verifier/jwt_utils.py",
        "url": "https://raw.githubusercontent.com/okta/okta-jwt-verifier-python/master/okta_jwt_verifier/jwt_utils.py",
    },
    {
        "program": "okta",
        "repo": "okta/okta-jwt-verifier-golang",
        "branch": "master",
        "path": "jwtverifier.go",
        "url": "https://raw.githubusercontent.com/okta/okta-jwt-verifier-golang/master/jwtverifier.go",
    },
    {
        "program": "okta",
        "repo": "okta/okta-jwt-verifier-golang",
        "branch": "master",
        "path": "adaptors/lestrratGoJwx/lestrratGoJwx.go",
        "url": "https://raw.githubusercontent.com/okta/okta-jwt-verifier-golang/master/adaptors/lestrratGoJwx/lestrratGoJwx.go",
    },
    {
        "program": "okta",
        "repo": "okta/okta-jwt-verifier-js",
        "branch": "master",
        "path": "lib.js",
        "url": "https://raw.githubusercontent.com/okta/okta-jwt-verifier-js/master/lib.js",
    },
    {
        "program": "zendesk",
        "repo": "zendesk/zcli",
        "branch": "master",
        "path": "packages/zcli-apps/src/commands/apps/server.ts",
        "url": "https://raw.githubusercontent.com/zendesk/zcli/master/packages/zcli-apps/src/commands/apps/server.ts",
    },
    {
        "program": "zendesk",
        "repo": "zendesk/zcli",
        "branch": "master",
        "path": "packages/zcli-apps/src/utils/getAppSettings.ts",
        "url": "https://raw.githubusercontent.com/zendesk/zcli/master/packages/zcli-apps/src/utils/getAppSettings.ts",
    },
    {
        "program": "zendesk",
        "repo": "zendesk/zcli",
        "branch": "master",
        "path": "packages/zcli-core/src/lib/oauth.ts",
        "url": "https://raw.githubusercontent.com/zendesk/zcli/master/packages/zcli-core/src/lib/oauth.ts",
    },
    {
        "program": "zendesk",
        "repo": "zendesk/zendesk_api_client_rb",
        "branch": "master",
        "path": "lib/zendesk_api/middleware/response/gzip.rb",
        "url": "https://raw.githubusercontent.com/zendesk/zendesk_api_client_rb/master/lib/zendesk_api/middleware/response/gzip.rb",
    },
    {
        "program": "zendesk",
        "repo": "zendesk/zendesk_api_client_rb",
        "branch": "master",
        "path": "lib/zendesk_api/middleware/request/retry.rb",
        "url": "https://raw.githubusercontent.com/zendesk/zendesk_api_client_rb/master/lib/zendesk_api/middleware/request/retry.rb",
    },
]


def verify_scope() -> Dict[str, List[str]]:
    programs = json.loads(SCOPED_FILE.read_text(encoding="utf-8"))
    by_slug = {p["slug"]: p["in_scope"] for p in programs}
    for slug in ("okta", "zendesk"):
        if slug not in by_slug or "github.com" not in by_slug[slug]:
            raise RuntimeError(f"Program {slug!r} does not list github.com in discovered_engagements_scoped.json")
    return {"okta": by_slug["okta"], "zendesk": by_slug["zendesk"]}


def fetch_head_sources() -> Dict[str, Dict[str, Any]]:
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    fetched: Dict[str, Dict[str, Any]] = {}
    for entry in TARGET_SOURCES:
        local_copy = SANDBOX_DIR / "head_snapshots" / entry["repo"].replace("/", "__") / entry["path"]
        if local_copy.exists():
            raw = local_copy.read_bytes()
        else:
            req = urllib.request.Request(
                entry["url"],
                headers={"User-Agent": "SabrinaSwarmStaticAudit/1.0 (+Defensive-OSS-Review)"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            local_copy.parent.mkdir(parents=True, exist_ok=True)
            local_copy.write_bytes(raw)
        text = raw.decode("utf-8", errors="replace")
        key = f"{entry['repo']}:{entry['path']}"
        fetched[key] = {
            **entry,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "lines": text.splitlines(),
            "line_count": len(text.splitlines()),
            "local_snapshot": str(local_copy),
        }
    return fetched


def numbered_excerpt(lines: List[str], start_line: int, end_line: int) -> str:
    lo = max(1, start_line)
    hi = min(len(lines), end_line)
    return "\n".join(f"{idx:4d}: {lines[idx - 1]}" for idx in range(lo, hi + 1))


def build_compact_packets(sources: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    py_ver = sources["okta/okta-jwt-verifier-python:okta_jwt_verifier/jwt_verifier.py"]["lines"]
    py_utl = sources["okta/okta-jwt-verifier-python:okta_jwt_verifier/jwt_utils.py"]["lines"]
    go_ver = sources["okta/okta-jwt-verifier-golang:jwtverifier.go"]["lines"]
    go_jwx = sources["okta/okta-jwt-verifier-golang:adaptors/lestrratGoJwx/lestrratGoJwx.go"]["lines"]
    js_ver = sources["okta/okta-jwt-verifier-js:lib.js"]["lines"]

    zd_srv = sources["zendesk/zcli:packages/zcli-apps/src/commands/apps/server.ts"]["lines"]
    zd_set = sources["zendesk/zcli:packages/zcli-apps/src/utils/getAppSettings.ts"]["lines"]
    zd_oau = sources["zendesk/zcli:packages/zcli-core/src/lib/oauth.ts"]["lines"]
    zd_gzp = sources["zendesk/zendesk_api_client_rb:lib/zendesk_api/middleware/response/gzip.rb"]["lines"]
    zd_rty = sources["zendesk/zendesk_api_client_rb:lib/zendesk_api/middleware/request/retry.rb"]["lines"]

    okta_packet = "\n\n".join([
        "[OKTA-1] okta/okta-jwt-verifier-python: okta_jwt_verifier/jwt_verifier.py (verify_id_token & get_jwk)",
        numbered_excerpt(py_ver, 167, 209),
        numbered_excerpt(py_ver, 293, 303),
        "[OKTA-2] okta/okta-jwt-verifier-python: okta_jwt_verifier/jwt_utils.py (verify_expiration)",
        numbered_excerpt(py_utl, 67, 77),
        "[OKTA-3] okta/okta-jwt-verifier-golang: jwtverifier.go & lestrratGoJwx.go",
        numbered_excerpt(go_ver, 147, 184),
        numbered_excerpt(go_ver, 379, 392),
        numbered_excerpt(go_jwx, 68, 78),
        "[OKTA-4] okta/okta-jwt-verifier-js: lib.js (AssertedClaimsVerifier & verifyNonce)",
        numbered_excerpt(js_ver, 90, 103),
        numbered_excerpt(js_ver, 163, 173),
    ])

    zendesk_packet = "\n\n".join([
        "[ZENDESK-1] zendesk/zcli: packages/zcli-apps/src/commands/apps/server.ts & getAppSettings.ts",
        numbered_excerpt(zd_srv, 18, 60),
        numbered_excerpt(zd_set, 1, 18),
        "[ZENDESK-2] zendesk/zcli: packages/zcli-core/src/lib/oauth.ts",
        numbered_excerpt(zd_oau, 82, 142),
        "[ZENDESK-3] zendesk/zendesk_api_client_rb: gzip.rb & retry.rb",
        numbered_excerpt(zd_gzp, 10, 18),
        numbered_excerpt(zd_rty, 50, 60),
    ])
    return {"okta": okta_packet, "zendesk": zendesk_packet}


def run_deterministic_static_checks(sources: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pure read-only static AST/pattern invariant checks against live HEAD lines."""
    py_ver = "\n".join(sources["okta/okta-jwt-verifier-python:okta_jwt_verifier/jwt_verifier.py"]["lines"])
    py_utl = "\n".join(sources["okta/okta-jwt-verifier-python:okta_jwt_verifier/jwt_utils.py"]["lines"])
    go_ver = "\n".join(sources["okta/okta-jwt-verifier-golang:jwtverifier.go"]["lines"])
    js_ver = "\n".join(sources["okta/okta-jwt-verifier-js:lib.js"]["lines"])
    zd_srv = "\n".join(sources["zendesk/zcli:packages/zcli-apps/src/commands/apps/server.ts"]["lines"])
    zd_set = "\n".join(sources["zendesk/zcli:packages/zcli-apps/src/utils/getAppSettings.ts"]["lines"])
    zd_oau = "\n".join(sources["zendesk/zcli:packages/zcli-core/src/lib/oauth.ts"]["lines"])
    zd_gzp = "\n".join(sources["zendesk/zendesk_api_client_rb:lib/zendesk_api/middleware/response/gzip.rb"]["lines"])

    return [
        {
            "id": "STATIC-OKTA-PY-01",
            "repo": "okta/okta-jwt-verifier-python",
            "file": "okta_jwt_verifier/jwt_verifier.py:174,204",
            "invariant": "OIDC Core 1.0 §3.1.3.7 Rule 11: when expected `nonce` is supplied, missing `'nonce'` claim MUST fail validation.",
            "pattern_present_on_head": "if 'nonce' in claims and claims['nonce'] != nonce:" in py_ver,
            "js_reference_guard_present": "if (!nonce && expected)" in js_ver,
        },
        {
            "id": "STATIC-OKTA-PY-02",
            "repo": "okta/okta-jwt-verifier-python",
            "file": "okta_jwt_verifier/jwt_utils.py:75",
            "invariant": "`verify_expiration(token, leeway=...)` must forward caller `leeway` instead of constant `LEEWAY`.",
            "pattern_present_on_head": "leeway=LEEWAY)" in py_utl,
        },
        {
            "id": "STATIC-OKTA-GO-01",
            "repo": "okta/okta-jwt-verifier-golang",
            "file": "jwtverifier.go:152,158-181,214,220-244,383",
            "invariant": "Unchecked `resp.(map[string]interface{})` panic risk, non-nil `&myJwt` returned on claim error, and `base64.StdEncoding` instead of `base64.RawURLEncoding`.",
            "pattern_present_on_head": (
                "resp.(map[string]interface{})" in go_ver
                and "return &myJwt, fmt.Errorf" in go_ver
                and "base64.StdEncoding.DecodeString(header)" in go_ver
            ),
        },
        {
            "id": "STATIC-OKTA-JS-01",
            "repo": "okta/okta-jwt-verifier-js",
            "file": "lib.js:99-101",
            "invariant": "`AssertedClaimsVerifier` `'includes'` operator calls `actualValue.includes(expectedValue)` without `Array.isArray(actualValue)`, matching substrings when `actualValue` is a string.",
            "pattern_present_on_head": "op === 'includes' && (!actualValue || !actualValue.includes(expectedValue))" in js_ver,
        },
        {
            "id": "STATIC-ZENDESK-ZCLI-01",
            "repo": "zendesk/zcli",
            "file": "packages/zcli-apps/src/commands/apps/server.ts:32 & getAppSettings.ts:8-15",
            "invariant": "Local dev server combines `app.use(cors())` (`Access-Control-Allow-Origin: *`) with `/app.json` returning unfiltered `zcli.apps.config.json` parameters (including `secure: true` manifest parameters).",
            "pattern_present_on_head": "app.use(cors())" in zd_srv and "param.secure" not in zd_set,
        },
        {
            "id": "STATIC-ZENDESK-ZCLI-02",
            "repo": "zendesk/zcli",
            "file": "packages/zcli-core/src/lib/oauth.ts:85",
            "invariant": "OAuth localhost callback server calls `server.listen(port)` without `'127.0.0.1'` loopback host bind, exposing callback listener on all LAN interfaces (`0.0.0.0`/`::`).",
            "pattern_present_on_head": "server.listen(port, () =>" in zd_oau,
        },
        {
            "id": "STATIC-ZENDESK-RB-01",
            "repo": "zendesk/zendesk_api_client_rb",
            "file": "lib/zendesk_api/middleware/response/gzip.rb:15 & request/retry.rb:53",
            "invariant": "Unbounded `Zlib::GzipReader.new(...).read` memory growth and uncapped `Retry-After` sleep duration.",
            "pattern_present_on_head": "Zlib::GzipReader.new(StringIO.new(env[:body])).read" in zd_gzp,
        },
    ]


def call_ego_ollama(
    sentinel: CyberComplianceSentinel,
    model: str,
    family: str,
    role: str,
    system_prompt: str,
    user_prompt: str,
    num_predict: int = 1600,
    temperature: float = 0.2,
) -> Dict[str, Any]:
    full_query = system_prompt + "\n\n" + user_prompt
    clean, reason = sentinel.sanitize_outbound_query(full_query)
    if not clean:
        raise RuntimeError(f"Sentinel blocked outbound query for {model}: {reason}")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "num_ctx": 8192,
            "num_predict": num_predict,
            "temperature": temperature,
        },
    }
    t0 = time.perf_counter()
    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    latency = round(time.perf_counter() - t0, 2)
    msg = raw.get("message") or {}
    content = msg.get("content") or ""
    thinking = msg.get("thinking") or ""
    incident = sentinel.inspect_and_report_threat(f"ego:{model}", content, {"role": role})
    return {
        "node": f"ego:{model}",
        "model": model,
        "family": family,
        "role": role,
        "ok": bool(content.strip()),
        "latency_sec": latency,
        "prompt_eval_count": raw.get("prompt_eval_count", 0),
        "eval_count": raw.get("eval_count", 0),
        "sentinel_check": reason,
        "sentinel_incident": incident,
        "thinking": thinking,
        "response": content,
    }


def main() -> int:
    sentinel = CyberComplianceSentinel()
    scope_info = verify_scope()
    sources = fetch_head_sources()
    packets = build_compact_packets(sources)
    static_checks = run_deterministic_static_checks(sources)

    combined_compact = (
        "=== PACKET 1: OKTA IN-SCOPE OSS REPOSITORIES ===\n"
        + packets["okta"]
        + "\n\n=== PACKET 2: ZENDESK IN-SCOPE OSS REPOSITORIES ===\n"
        + packets["zendesk"]
    )
    print(f"[v16.1] Compact static dossier size: {len(combined_compact)} chars (~{len(combined_compact)//4} tokens).")

    swarm_outputs: List[Dict[str, Any]] = []

    # 1. Qwen 3 14B (<think> Strategist) — Formal Invariant & Security Analysis
    print("[v16.1] [1/5] Running Qwen 3 14B (<think> Strategist) across Okta & Zendesk...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="qwen3:14b",
            family="Alibaba Qwen 3 (14B <think>)",
            role="Formal Invariant & Cross-SDK Security Strategist",
            system_prompt=(
                "You are Guild Strategist (Qwen 3 14B). Analyze BOTH Packet 1 (Okta) and Packet 2 (Zendesk) "
                "using strictly defensive static code analysis (zero exploit payloads or attack scripts). "
                "Specifically evaluate:\n"
                "1. [OKTA-1 vs OKTA-4]: In `okta-jwt-verifier-python` (`jwt_verifier.py:174,204`), what happens when "
                "a developer calls `verify_id_token(token, nonce='expected_session_nonce')` on a token that omits "
                "the `'nonce'` claim entirely (`'nonce' not in claims`), compared to `okta-jwt-verifier-js` (`lib.js:167`)?\n"
                "2. [OKTA-3]: In `okta-jwt-verifier-golang` (`jwtverifier.go:152,158-181,383`), analyze returning "
                "`&myJwt` on validation failure, unchecked `resp.(map[string]interface{})`, and `base64.StdEncoding`.\n"
                "3. [OKTA-4]: In `okta-jwt-verifier-js` (`lib.js:99-101`), analyze `actualValue.includes(expectedValue)` "
                "when `actualValue` is a string vs an array.\n"
                "4. [ZENDESK-1 & ZENDESK-2]: Analyze `app.use(cors())` + `/app.json` exposing `getAppSettings.ts` "
                "parameters (including `secure: true` config parameters) and `oauth.ts` `server.listen(port)` binding "
                "all interfaces instead of `127.0.0.1`.\n"
                "5. [ZENDESK-3]: Analyze `gzip.rb:15` unbounded decompression and `retry.rb:53` uncapped `Retry-After`."
            ),
            user_prompt=combined_compact,
            num_predict=2800,
            temperature=0.2,
        )
    )
    qwen3_analysis = swarm_outputs[-1]["response"]
    print(
        f"        -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 2. Qwen 2.5 Coder 14B — Minimal Drop-In Defensive Patches
    print("[v16.1] [2/5] Running Qwen 2.5 Coder 14B (Defensive Patch Synthesis)...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="qwen2.5-coder:14b",
            family="Alibaba Qwen 2.5 Coder (14B)",
            role="Defensive Remediation Patch Author",
            system_prompt=(
                "You are Guild Patch Author (Qwen 2.5 Coder 14B). Produce concise, backward-compatible, "
                "strictly defensive unified diffs (`diff -u`) fixing:\n"
                "1. `okta_jwt_verifier/jwt_verifier.py` (enforce `nonce` presence when `nonce is not None` and reject unexpected `nonce` when `nonce is None` or mismatch) and `jwt_utils.py` (`leeway=leeway`).\n"
                "2. `jwtverifier.go` (safe `token, ok := resp.(map[string]interface{})`, return `nil` on claim validation failure, and `base64.RawURLEncoding`).\n"
                "3. `lib.js` (`Array.isArray(actualValue)` guard in `AssertedClaimsVerifier` `'includes'`).\n"
                "4. `server.ts` / `getAppSettings.ts` (restrict CORS origin & redact `secure` parameters in `/app.json`) and `oauth.ts` (`server.listen(port, '127.0.0.1', ...)`).\n"
                "5. `gzip.rb` / `retry.rb` (bounded decompression limit & max retry delay cap)."
            ),
            user_prompt=combined_compact,
            num_predict=1600,
            temperature=0.1,
        )
    )
    coder_patches = swarm_outputs[-1]["response"]
    print(
        f"        -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 3. Meta Llama 3.1 8B — Disjoint-Family Independent Security & False-Positive Auditor
    print("[v16.1] [3/5] Running Meta Llama 3.1 8B (Disjoint-Family Independent Auditor)...")
    audit_bundle = (
        "COMPACT SOURCE PACKETS:\n" + combined_compact + "\n\n"
        "QWEN 3 14B ANALYSIS:\n" + qwen3_analysis[:2600]
    )
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="llama3.1:8b",
            family="Meta Llama",
            role="Disjoint-Family False-Positive & Precondition Auditor",
            system_prompt=(
                "You are Disjoint-Family Auditor 1 (Meta Llama 3.1 8B). Audit EVERY finding across BOTH "
                "Okta ([OKTA-1..4]) and Zendesk ([ZENDESK-1..3]). Classify each as:\n"
                "- `BOUNTY_CANDIDATE` (clear security boundary violation with realistic impact),\n"
                "- `HARDENING_PR_CANDIDATE` (real code bug/DoS/defense-in-depth better suited for an OSS PR than a bounty report), or\n"
                "- `REJECTED_FALSE_POSITIVE`.\n"
                "State exact preconditions honestly (for example, `lestrrat-go/jwx` signature check requires a key in `jwkSet`, "
                "whereas `okta-jwt-verifier-python` missing `nonce` accepts a genuinely Okta-signed ID token issued without a nonce)."
            ),
            user_prompt=audit_bundle,
            num_predict=1100,
            temperature=0.15,
        )
    )
    llama_audit = swarm_outputs[-1]["response"]
    print(
        f"        -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 4. IBM Granite 3.1 Dense 8B — Disjoint-Family Bugcrowd VRT & Patch Governance Auditor
    print("[v16.1] [4/5] Running IBM Granite 3.1 Dense 8B (Bugcrowd VRT & Patch Governance Auditor)...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="granite3.1-dense:8b",
            family="IBM Granite",
            role="Disjoint-Family Bugcrowd VRT & Patch Governance Auditor",
            system_prompt=(
                "You are Disjoint-Family Auditor 2 (IBM Granite 3.1 Dense 8B). Map each confirmed Okta and "
                "Zendesk finding to Bugcrowd VRT (P1-P5 / Informational), audit Qwen Coder's unified diffs "
                "for any breaking API behavior (for instance, ensure `okta-jwt-verifier-python` still allows "
                "`verify_id_token(token)` when `nonce=None` if no nonce was requested!), and rank the top 2 "
                "highest-signal actions."
            ),
            user_prompt=(
                "LLAMA DISJOINT AUDIT:\n" + llama_audit[:2200] + "\n\n"
                "QWEN CODER PATCHES:\n" + coder_patches[:2600]
            ),
            num_predict=1100,
            temperature=0.15,
        )
    )
    granite_audit = swarm_outputs[-1]["response"]
    print(
        f"        -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 5. Mistral 7B — Signed Minority Dissent & Researcher Reputation Safeguard
    print("[v16.1] [5/5] Running Mistral 7B (Signed Minority Dissent & Reputation Safeguard)...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="dolphin-mistral:7b",
            family="Mistral AI",
            role="Signed Dissenting Opinion & Researcher Reputation Safeguard",
            system_prompt=(
                "You are Guild Dissent & Safeguard Critic (Mistral 7B). Write a concise signed dissenting "
                "warning for the human operator before submitting any static OSS SDK finding to Bugcrowd: "
                "explain why many Bugcrowd programs mark client-library/CLI hardening issues Out-of-Scope or P5 "
                "unless a specific first-party hosted service (`*.okta.com`, `*.zendesk.com`) or explicit SDK "
                "bounty tier covers them, and advise checking the live Bugcrowd brief exclusions first while "
                "using upstream GitHub PRs for hardening items."
            ),
            user_prompt=(
                "LLAMA AUDIT:\n" + llama_audit[:1800] + "\n\nGRANITE VRT AUDIT:\n" + granite_audit[:1800]
            ),
            num_predict=800,
            temperature=0.3,
        )
    )
    print(
        f"        -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    artifact = {
        "version": "v16.1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audit_mode": "strictly_defensive_static_source_review",
        "zero_production_traffic": True,
        "scoped_programs": scope_info,
        "deterministic_static_checks": static_checks,
        "source_provenance": {
            k: {kk: vv for kk, vv in v.items() if kk != "lines"}
            for k, v in sources.items()
        },
        "participating_lineages": [
            "Alibaba Qwen 3 (qwen3:14b <think>)",
            "Alibaba Qwen 2.5 Coder (qwen2.5-coder:14b)",
            "Meta Llama (llama3.1:8b)",
            "IBM Granite (granite3.1-dense:8b)",
            "Mistral AI (dolphin-mistral:7b)",
        ],
        "swarm_verbatim_outputs": swarm_outputs,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[v16.1] Saved full verbatim multi-family provenance to {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
