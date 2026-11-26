#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Multi-Family Swarm v17 — Explicitly In-Scope `Auth0 by Okta` SDK Static Audit
=====================================================================================
Target Bugcrowd Program:
- `Auth0 by Okta` (`https://bugcrowd.com/engagements/auth0-okta`, max_payout = $50,000)
- Explicitly In-Scope Target (`bugcrowd_verified_oss_scopes.json`):
  `https://github.com/auth0/nextjs-auth0` ("Auth0 Next.js SDK (nextjs-auth0)")

Safety & Cyber Law Guarantees:
- Zero network requests to Auth0 production/staging endpoints (`*.auth0.com`, `*.auth0app.com`).
- Fetches only public `main` HEAD TypeScript source files from `raw.githubusercontent.com`.
- Screens all prompts and outputs through `CyberComplianceSentinel`.
- Strictly defensive ("Analyze & Defend"): identifies code-level invariants, runs
  Disjoint-Family Auditing (`Qwen 3 14B <think>`, `Qwen 2.5 Coder 14B`, `Meta Llama 3.1 8B`,
  `IBM Granite 3.1 Dense 8B`, `Mistral 7B`), and produces minimal defensive patches.
  Generates ZERO weaponized exploits, PoC payloads, or attack commands.
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
VERIFIED_SCOPES = SABRINA_ROOT / "data" / "bugcrowd_verified_oss_scopes.json"
SANDBOX_DIR = SABRINA_ROOT / "artifacts" / "tool_sandbox" / "bugcrowd_static_audit_20261116"
OUT_JSON = SABRINA_ROOT / "artifacts" / "collective-repair-20261108" / "swarm_auth0_sdk_audit_v17.json"

TARGET_SOURCES: List[Dict[str, str]] = [
    {
        "program": "auth0-okta",
        "repo": "auth0/nextjs-auth0",
        "branch": "main",
        "path": "src/server/cookies.ts",
        "url": "https://raw.githubusercontent.com/auth0/nextjs-auth0/main/src/server/cookies.ts",
    },
    {
        "program": "auth0-okta",
        "repo": "auth0/nextjs-auth0",
        "branch": "main",
        "path": "src/utils/proxy.ts",
        "url": "https://raw.githubusercontent.com/auth0/nextjs-auth0/main/src/utils/proxy.ts",
    },
    {
        "program": "auth0-okta",
        "repo": "auth0/nextjs-auth0",
        "branch": "main",
        "path": "src/server/fetcher.ts",
        "url": "https://raw.githubusercontent.com/auth0/nextjs-auth0/main/src/server/fetcher.ts",
    },
    {
        "program": "auth0-okta",
        "repo": "auth0/nextjs-auth0",
        "branch": "main",
        "path": "src/utils/pathUtils.ts",
        "url": "https://raw.githubusercontent.com/auth0/nextjs-auth0/main/src/utils/pathUtils.ts",
    },
]


def verify_auth0_scope() -> Dict[str, Any]:
    programs = json.loads(VERIFIED_SCOPES.read_text(encoding="utf-8"))
    for p in programs:
        if p.get("url") == "https://bugcrowd.com/engagements/auth0-okta":
            targets = [t.get("target") for t in p.get("github_in_scope", [])]
            if "https://github.com/auth0/nextjs-auth0" not in targets:
                raise RuntimeError("nextjs-auth0 missing from verified auth0-okta scope")
            return p
    raise RuntimeError("auth0-okta program not found in bugcrowd_verified_oss_scopes.json")


def fetch_head_sources() -> Dict[str, Dict[str, Any]]:
    fetched: Dict[str, Dict[str, Any]] = {}
    for entry in TARGET_SOURCES:
        local_copy = SANDBOX_DIR / "head_snapshots" / entry["repo"].replace("/", "__") / entry["path"]
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


def find_window(lines: List[str], needle: str, before: int = 8, after: int = 32) -> str:
    for idx, line in enumerate(lines, 1):
        if needle in line:
            lo = max(1, idx - before)
            hi = min(len(lines), idx + after)
            return "\n".join(f"{i:4d}: {lines[i - 1]}" for i in range(lo, hi + 1))
    return "\n".join(f"{i:4d}: {lines[i - 1]}" for i in range(1, min(len(lines), 40) + 1))


def build_auth0_dossier(sources: Dict[str, Dict[str, Any]]) -> str:
    cookies_lines = sources["auth0/nextjs-auth0:src/server/cookies.ts"]["lines"]
    proxy_lines = sources["auth0/nextjs-auth0:src/utils/proxy.ts"]["lines"]
    fetcher_lines = sources["auth0/nextjs-auth0:src/server/fetcher.ts"]["lines"]
    path_lines = sources["auth0/nextjs-auth0:src/utils/pathUtils.ts"]["lines"]

    return "\n\n".join([
        "[AUTH0-NEXTJS-01] auth0/nextjs-auth0 (main HEAD): src/server/cookies.ts (getClearUpTo & deleteChunkedCookie, called unauthenticated on GET /auth/logout via sessionStore.delete)",
        find_window(cookies_lines, "const getClearUpTo", before=4, after=22),
        find_window(cookies_lines, "export function deleteChunkedCookie", before=2, after=24),
        "[AUTH0-NEXTJS-02] auth0/nextjs-auth0 (main HEAD): src/utils/proxy.ts (transformTargetUrl, called by /me/* handleMyAccount & /my-org/* handleMyOrg before injecting server-held Bearer/DPoP access token)",
        find_window(proxy_lines, "export function transformTargetUrl", before=2, after=55),
        "[AUTH0-NEXTJS-03] auth0/nextjs-auth0 (main HEAD): src/server/fetcher.ts (buildBaseRequest & isAbsoluteUrl) compared against src/utils/pathUtils.ts (createRouteUrl)",
        find_window(fetcher_lines, "isAbsoluteUrl(", before=2, after=38),
        find_window(fetcher_lines, "buildBaseRequest(", before=2, after=28),
        find_window(path_lines, "export function createRouteUrl", before=2, after=18),
    ])


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
    program = verify_auth0_scope()
    sources = fetch_head_sources()
    dossier = build_auth0_dossier(sources)
    print(f"[v17] Auth0 Next.js SDK (`main` HEAD) compact dossier: {len(dossier)} chars (~{len(dossier)//4} tokens).")

    swarm_outputs: List[Dict[str, Any]] = []

    # 1. Qwen 3 14B (<think>) — Formal Invariant & Security Boundary Strategist
    print("[v17] [1/5] Running Qwen 3 14B (<think> Strategist) on auth0/nextjs-auth0...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="qwen3:14b",
            family="Alibaba Qwen 3 (14B <think>)",
            role="Formal Invariant & Security Boundary Strategist",
            system_prompt=(
                "You are Guild Strategist (Qwen 3 14B). Perform a strictly defensive static vulnerability "
                "and bounds analysis of the three `auth0/nextjs-auth0` (`main` HEAD) candidates (zero exploit "
                "scripts, zero copy-paste attack commands):\n"
                "1. [AUTH0-NEXTJS-01] `src/server/cookies.ts`: Trace `getClearUpTo` + `deleteChunkedCookie` / "
                "`setChunkedCookie` when a request includes a single chunked cookie name with a large numeric "
                "index suffix (reachable unauthenticated on `GET /auth/logout` via `sessionStore.delete`). "
                "Compare O(highestSeen) dense iteration against O(MAX_CHUNKS + presentCookies) sparse cleanup.\n"
                "2. [AUTH0-NEXTJS-02] `src/utils/proxy.ts`: Trace `transformTargetUrl` when an incoming `/me/...` "
                "or `/my-org/...` request contains URL-encoded dot-segments (`%2e%2e`), which pass `startsWith('/me/')` "
                "and `split('/')` segment de-duplication, then get normalized by `new URL(baseUrl.origin + finalPath)` "
                "to escape `/me/v1` or `/my-org` on the Auth0 tenant origin while carrying the server-held Bearer/DPoP token.\n"
                "3. [AUTH0-NEXTJS-03] `src/server/fetcher.ts`: Compare `Fetcher.buildBaseRequest` (which silently "
                "ignores `this.config.baseUrl` whenever `isAbsoluteUrl(info)` is true, forwarding the Bearer/DPoP "
                "token to an arbitrary external URL) against `createRouteUrl` in `src/utils/pathUtils.ts` (which "
                "explicitly enforces same-origin)."
            ),
            user_prompt=dossier,
            num_predict=2800,
            temperature=0.2,
        )
    )
    qwen3_out = swarm_outputs[-1]["response"]
    print(
        f"      -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 2. Qwen 2.5 Coder 14B — Minimal Drop-In TypeScript Defensive Diffs
    print("[v17] [2/5] Running Qwen 2.5 Coder 14B (TypeScript Defensive Patch Synthesis)...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="qwen2.5-coder:14b",
            family="Alibaba Qwen 2.5 Coder (14B)",
            role="TypeScript Defensive Patch Author",
            system_prompt=(
                "You are Guild Patch Author (Qwen 2.5 Coder 14B). Write clean, backward-compatible TypeScript "
                "unified diffs (`diff -u`) for `auth0/nextjs-auth0` (`main` HEAD):\n"
                "1. `src/server/cookies.ts`: Replace the unbounded `0..clearUpTo` dense loop in `setChunkedCookie` "
                "and `deleteChunkedCookie` with a bounded `0..MAX_CHUNKS` sweep PLUS targeted deletion of only "
                "the exact cookie names actually present in `getAllChunkedCookies(reqCookies, name)`.\n"
                "2. `src/utils/proxy.ts`: After constructing `targetUrl = new URL(baseUrl.origin + finalPath)`, "
                "verify `targetUrl.origin === baseUrl.origin` AND that normalized `targetUrl.pathname` stays "
                "strictly under `basePath` (rejecting dot-segment / `%2e%2e` escapes with an error).\n"
                "3. `src/server/fetcher.ts`: When `this.config.baseUrl` is configured, enforce that `resolvedUrl` "
                "matches `new URL(this.config.baseUrl).origin` before attaching the OAuth Bearer/DPoP token."
            ),
            user_prompt=dossier,
            num_predict=1600,
            temperature=0.1,
        )
    )
    coder_out = swarm_outputs[-1]["response"]
    print(
        f"      -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 3. Meta Llama 3.1 8B — Disjoint-Family False-Positive & Precondition Auditor
    print("[v17] [3/5] Running Meta Llama 3.1 8B (Disjoint-Family False-Positive Auditor)...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="llama3.1:8b",
            family="Meta Llama",
            role="Disjoint-Family Precondition & Reachability Auditor",
            system_prompt=(
                "You are Disjoint-Family Auditor 1 (Meta Llama 3.1 8B). Audit [AUTH0-NEXTJS-01], "
                "[AUTH0-NEXTJS-02], and [AUTH0-NEXTJS-03] critically:\n"
                "- For [AUTH0-NEXTJS-02] (`proxy.ts` `%2e%2e` path traversal), note what audience/scope the "
                "`/me/` (`My Account API`) access token is minted for—does Auth0's upstream `/api/v2` reject a "
                "`/me/` audience token while same-audience or `/me/` sibling endpoints accept it?\n"
                "- For [AUTH0-NEXTJS-01] (`cookies.ts` `getClearUpTo`), note whether Bugcrowd's standard "
                "Application-Layer DoS policy vs Network DoS applies.\n"
                "- For [AUTH0-NEXTJS-03] (`fetcher.ts`), note that it requires an application route to pass "
                "untrusted input into `fetchWithAuth(url)` when `baseUrl` is configured."
            ),
            user_prompt="DOSSIER:\n" + dossier + "\n\nQWEN 3 14B:\n" + qwen3_out[:2500],
            num_predict=1100,
            temperature=0.15,
        )
    )
    llama_out = swarm_outputs[-1]["response"]
    print(
        f"      -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 4. IBM Granite 3.1 Dense 8B — Bugcrowd VRT & Patch Regression Auditor
    print("[v17] [4/5] Running IBM Granite 3.1 Dense 8B (Bugcrowd VRT & Patch Auditor)...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="granite3.1-dense:8b",
            family="IBM Granite",
            role="Disjoint-Family Bugcrowd VRT & Patch Governance Auditor",
            system_prompt=(
                "You are Disjoint-Family Auditor 2 (IBM Granite 3.1 Dense 8B). Rank [AUTH0-NEXTJS-01], "
                "[AUTH0-NEXTJS-02], and [AUTH0-NEXTJS-03] for the `Auth0 by Okta` Bugcrowd engagement "
                "(`https://github.com/auth0/nextjs-auth0` explicitly in-scope), assign conservative VRT "
                "ratings (P2/P3/P4 vs Upstream PR), and verify Qwen Coder's TypeScript patches do not break "
                "legitimate multi-chunk sessions or valid `/me/v1/...` proxy calls."
            ),
            user_prompt="LLAMA AUDIT:\n" + llama_out[:2200] + "\n\nQWEN CODER PATCHES:\n" + coder_out[:2500],
            num_predict=1000,
            temperature=0.15,
        )
    )
    granite_out = swarm_outputs[-1]["response"]
    print(
        f"      -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    # 5. Mistral 7B — Signed Dissent & Responsible Disclosure Checklist
    print("[v17] [5/5] Running Mistral 7B (Signed Dissent & Submission Quality Gate)...")
    swarm_outputs.append(
        call_ego_ollama(
            sentinel,
            model="dolphin-mistral:7b",
            family="Mistral AI",
            role="Signed Dissent & Bugcrowd Signal Protection Critic",
            system_prompt=(
                "You are Guild Dissent & Signal Critic (Mistral 7B). Provide a concise signed dissenting "
                "opinion comparing [AUTH0-NEXTJS-02] (`proxy.ts` `%2e%2e` path traversal out of `/me/v1`), "
                "[AUTH0-NEXTJS-01] (`cookies.ts` `getClearUpTo` unbounded loop), and [AUTH0-NEXTJS-03] "
                "(`fetcher.ts` cross-origin token leak when `baseUrl` is set). State which single finding has "
                "the highest Bugcrowd triage acceptance probability without risking an N/A or OOS DoS rejection."
            ),
            user_prompt="LLAMA AUDIT:\n" + llama_out[:1800] + "\n\nGRANITE AUDIT:\n" + granite_out[:1800],
            num_predict=800,
            temperature=0.3,
        )
    )
    print(
        f"      -> prompt_toks={swarm_outputs[-1]['prompt_eval_count']} "
        f"out_toks={swarm_outputs[-1]['eval_count']} in {swarm_outputs[-1]['latency_sec']}s"
    )

    artifact = {
        "version": "v17",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "audit_mode": "strictly_defensive_static_source_review",
        "zero_production_traffic": True,
        "verified_bugcrowd_program": {
            "name": program["name"],
            "url": program["url"],
            "max_payout": program["max_payout"],
            "target": "https://github.com/auth0/nextjs-auth0",
        },
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
    print(f"[v17] Saved full verbatim multi-family provenance to {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
