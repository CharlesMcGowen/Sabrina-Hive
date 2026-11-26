#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v19.3 — Forge Host-Side RFC 6238 Google Authenticator Plugin (`totp_authenticator`)
===================================================================================
Uses Frontier `GoogleGeminiOracle` (`gemini-3-flash-preview`) + Disjoint `IBM Granite 3.1 8B`
via `SabrinaToolSkillForge` to build, RFC-6238-test in sandbox, and hot-deploy Sabrina's
host-side Google Authenticator TOTP generator (`tools/custom/totp_authenticator_plugin.py`
+ `skills/totp-authenticator/SKILL.md` v1.0.0), backed by Windows Credential Manager
(`tools/secret_store.py`) so raw TOTP seeds never enter chat or model prompts.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.oracle_providers import GoogleGeminiOracle
from tools.run_v19_1_frontier_assisted_swarm_forge import (
    call_frontier_gemini,
    call_local_ollama,
    extract_fenced_files,
)
from tools.sabrina_tool_skill_forge import SabrinaToolSkillForge
from tools.tool_registry import TOOL_REGISTRY

OUT_JSON = (
    SABRINA_ROOT
    / "artifacts"
    / "collective-repair-20261108"
    / "swarm_self_forged_totp_authenticator_v19_3.json"
)


def main() -> int:
    print("[v19.3] Forging Sabrina's RFC 6238 Google Authenticator (`totp_authenticator`) plugin...")
    forge = SabrinaToolSkillForge()
    gemini_oracle = GoogleGeminiOracle()

    note = forge.ingest_docs_and_reason(
        problem_slug="totp-google-authenticator",
        problem_statement=(
            "Provide Sabrina with a pure-stdlib RFC 6238 Google Authenticator (TOTP) generator "
            "backed by Windows Credential Manager (`tools.secret_store.get_secret`) so she can "
            "generate 6-digit 2FA codes host-side for authorized operator accounts (e.g. "
            "`BUGCROWD_TOTP_SECRET`, `HACKERONE_TOTP_SECRET`) without ever exposing raw Base32 "
            "seeds in logs or LLM prompts."
        ),
        doc_paths=[str(SABRINA_ROOT / "tools" / "secret_store.py")],
        rag_queries=["secret_store Windows Credential Manager", "RFC 6238 TOTP"],
        extracted_invariants=[
            "RFC 6238 TOTP computes HOTP(K, T) where T = floor((unix_time - T0) / period), default period=30, digits=6, algo=SHA1.",
            "Google Authenticator secrets are Base32-encoded (often lowercase, with spaces/hyphens, or missing '=' padding) or `otpauth://totp/...?secret=...` URIs.",
            "Raw TOTP secret seeds must be read strictly via `tools.secret_store.get_secret('<SERVICE>_TOTP_SECRET')` and NEVER echoed in tool output or exceptions.",
        ],
        test_hypotheses=[
            "Matches official RFC 6238 Appendix B test vector at t=59 (SHA1 seed 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ' -> 8-digit '94287082', 6-digit '287082').",
            "Parses both raw Base32 seeds with spaces/lowercase/unpadded characters and `otpauth://totp/...` URIs.",
            "Returns `enrolled=False` cleanly with the expected Windows Credential Manager key name when not yet provisioned, and never leaks the raw secret when `enrolled=True`.",
        ],
        author_model="gemini-3-flash-preview",
    )

    spec_prompt = """You are `gemini-3-flash-preview` forging Sabrina's pure-Python-stdlib RFC 6238 Google Authenticator plugin.
Output TWO complete Python 3.11 files in separate ```python fenced code blocks:
Block 1: `totp_authenticator_plugin.py`
Block 2: `test_totp_authenticator.py`

STRICT REQUIREMENTS FOR Block 1 (`totp_authenticator_plugin.py`):
1. Module-level constants:
   - `TOOL_NAME = "totp_authenticator"`
   - `TOOL_PROMPT = "Generate 6-digit RFC 6238 Google Authenticator (TOTP) codes host-side from Windows Credential Manager (`<SERVICE>_TOTP_SECRET`) without exposing raw seeds."`
   - `PARALLEL_SAFE = True`
2. NEVER mention any of these filenames anywhere in comments or strings: `sabrina_gemini_loop.py`, `oracle_providers.py`, `agent_skills.py`, `blackboard.py`, `rag_knowledge.py`, `lab_knowledge.py`, `browser_utils.py`, `probe_api.py`.
3. Pure Python stdlib ONLY (`base64`, `hashlib`, `hmac`, `json`, `re`, `struct`, `time`, `urllib.parse`) plus lazy `from tools.secret_store import get_secret`.
4. Implement:
   - `parse_totp_input(raw_secret_or_uri: str) -> dict`:
     If input starts with `"otpauth://"` (case-insensitive), parses query params via `urllib.parse.urlsplit` + `parse_qs`: extracts `secret`, `digits` (default `6`), `period` (default `30`), `algorithm` (`SHA1` | `SHA256` | `SHA512`, default `SHA1`).
     Otherwise treats input as a raw Base32 secret (`digits=6`, `period=30`, `algorithm="SHA1"`).
     Normalizes the Base32 secret by stripping whitespace and hyphens, uppercasing, and appending `"="` padding to a multiple of 8 characters (`(-len(s)) % 8`), then decodes via `base64.b32decode(padded, casefold=True)` to validate. Never include the raw secret string in `ValueError` messages!
     Returns `{"key_bytes": key_bytes, "digits": digits, "period": period, "algorithm": algo}`.
   - `generate_totp(raw_secret_or_uri: str, timestamp: float | None = None, digits: int | None = None, period: int | None = None, algorithm: str | None = None) -> dict`:
     Parses via `parse_totp_input(raw_secret_or_uri)`, allows explicit `digits`/`period`/`algorithm` overrides if provided, uses `now = time.time() if timestamp is None else float(timestamp)`.
     Computes `counter = int(now // p_period)`, big-endian 8-byte `struct.pack(">Q", counter)`, HMAC digest (`hashlib.sha1` / `sha256` / `sha512`), dynamic truncation (`offset = digest[-1] & 0x0F`, `binary = struct.unpack(">I", digest[offset:offset+4])[0] & 0x7FFFFFFF`, `code = str(binary % (10 ** p_digits)).zfill(p_digits)`), and `seconds_remaining = int(p_period - (int(now) % p_period))`.
     Returns `{"code": code, "digits": p_digits, "period": p_period, "algorithm": p_algo, "seconds_remaining": seconds_remaining}`. (NEVER include `key_bytes` or `raw_secret_or_uri`!)
   - `secret_key_name_for_service(service: str) -> str`:
     Sanitizes `service` to uppercase alphanumeric + underscore (e.g. `"bugcrowd"` -> `"BUGCROWD_TOTP_SECRET"`, `"hackerone"` -> `"HACKERONE_TOTP_SECRET"`; if it already ends with `"_TOTP_SECRET"`, keeps it).
   - `execute(action: dict, loop=None, secret_getter=None) -> str` (and `execute_plugin = execute`):
     Supports `operation = (action or {}).get("operation", "check_status")`:
       * `"check_status"`: checks `services = action.get("services") or ["bugcrowd", "hackerone"]` using `getter = secret_getter or get_secret`, returning `{"ok": True, "services": {svc: {"secret_key_name": key_name, "enrolled": bool(getter(key_name))} for svc in services}}`.
       * `"generate_code"`: looks up `service = action["service"]` via `getter(key_name)`. If empty, returns `{"ok": False, "service": service, "secret_key_name": key_name, "enrolled": False, "error": f"No TOTP seed stored under {key_name} in Windows Credential Manager."}`. If present, calls `generate_totp(...)` (allowing optional `timestamp` in `action` for deterministic tests) and returns `{"ok": True, "service": service, "secret_key_name": key_name, "enrolled": True, **totp_dict}`.

STRICT REQUIREMENTS FOR Block 2 (`test_totp_authenticator.py`):
Write 5 offline unit tests in `unittest.TestCase`:
1. `test_rfc6238_sha1_vector`: RFC 6238 seed `"GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"` (ASCII `"12345678901234567890"`) at `timestamp=59`, `digits=8` -> `"94287082"`, and default `digits=6` -> `"287082"`.
2. `test_unpadded_spaced_google_authenticator_secret`: lowercase spaced secret `"jbsw y3dp ehpk 3pxp"` parses cleanly and generates a 6-digit numeric code without leaking the secret in the returned dict.
3. `test_otpauth_uri_parsing`: `"otpauth://totp/Bugcrowd:charles?secret=JBSWY3DPEHPK3PXP&issuer=Bugcrowd&digits=6&period=30"` parses and generates matching code to raw `"JBSWY3DPEHPK3PXP"` at fixed `timestamp=1700000000`.
4. `test_execute_missing_secret`: calls `plugin.execute({"operation": "generate_code", "service": "bugcrowd"}, secret_getter=lambda k: "")` and asserts `ok is False`, `enrolled is False`, `secret_key_name == "BUGCROWD_TOTP_SECRET"`.
5. `test_execute_enrolled_never_leaks_seed`: passes `secret_getter=lambda k: "JBSWY3DPEHPK3PXP"` and asserts `"JBSWY3DPEHPK3PXP"` is NOT in the returned JSON string while `ok is True` and `len(code) == 6`.

Output ONLY the two complete ```python fenced code blocks.
"""

    gem_res = call_frontier_gemini(gemini_oracle, spec_prompt)
    print(f"[v19.3] {gem_res['model']} returned {gem_res['eval_count']} tokens in {gem_res['latency_sec']}s.")

    plugin_code, test_code = extract_fenced_files(gem_res["response"])
    if not plugin_code or not test_code:
        raise RuntimeError("Failed to extract fenced blocks for totp_authenticator.")

    sandbox_res = forge.sandbox_test_tool(
        tool_name="totp_authenticator",
        plugin_code=plugin_code,
        test_code=test_code,
        sample_action={"operation": "check_status", "services": ["bugcrowd", "hackerone"]},
        timeout_sec=30,
    )
    if not sandbox_res.get("ok"):
        raise RuntimeError(f"Sandbox failed: {sandbox_res}")
    print(f"[v19.3] Sandbox RFC 6238 unittest suite PASSED ({sandbox_res['elapsed_sec']}s)!")

    granite_prompt = (
        f"You are `granite3.1-dense:8b` auditing `totp_authenticator_plugin.py` authored by `{gem_res['model']}`.\n\n"
        f"```python\n{plugin_code}\n```\n\n"
        f"Unittest Output:\n```\n{sandbox_res.get('test_output', '')}\n```\n\n"
        "Verify in 3 bullets: (1) RFC 6238 HMAC truncation correctness, (2) zero raw secret leakage in outputs/errors, "
        "(3) valid Sabrina plugin contract -> `APPROVE_FOR_HOT_DEPLOY`."
    )
    granite_res = call_local_ollama("granite3.1-dense:8b", granite_prompt, num_predict=450)
    print(f"[v19.3] granite3.1-dense:8b disjoint audit completed ({granite_res['eval_count']} tokens, {granite_res['latency_sec']}s).")

    deployment = forge.deploy_and_evolve_skill(
        sandbox_result=sandbox_res,
        skill_name="totp-authenticator",
        skill_description=(
            "Host-side RFC 6238 Google Authenticator (TOTP) 6-digit code generator backed by "
            "Windows Credential Manager (`tools/secret_store.py`) with zero raw seed exposure."
        ),
        reasoning_note=note,
        when_to_use=(
            "Use whenever Sabrina needs to check enrollment status or generate a live 6-digit "
            "Google Authenticator 2FA code host-side for an operator-provisioned account "
            "(such as `BUGCROWD_TOTP_SECRET` or `HACKERONE_TOTP_SECRET`)."
        ),
        step_by_step_procedure=[
            "Check enrollment via `totp_authenticator` (`operation='check_status'`, `services=['bugcrowd', 'hackerone']`).",
            "If `enrolled == False`, ask the operator to store the Base32 TOTP seed locally in Windows Credential Manager via `secret_store.set_secret('<SERVICE>_TOTP_SECRET', ...)` (never in chat).",
            "Once enrolled, generate the live 6-digit code host-side via `operation='generate_code'` (`service='bugcrowd'`).",
        ],
        worker_model=gem_res["model"],
        worker_summary=f"Synthesized pure-stdlib RFC 6238 `totp_authenticator` + 5/5 passing tests ({sandbox_res['elapsed_sec']}s).",
        auditor_model="granite3.1-dense:8b",
        auditor_verdict=granite_res["response"][:500].replace("\n", " "),
        minority_dissent="Never accept or echo raw TOTP Base32 seeds in chat transcripts; enroll strictly via Windows Credential Manager.",
    )
    print(f"[v19.3] Deployed plugin -> {deployment['deployed_plugin_path']}")
    print(f"[v19.3] Installed SKILL.md -> {deployment['installed_skill_path']} (v{deployment['skill_version']})")

    status_raw = TOOL_REGISTRY.dispatch(
        "totp_authenticator",
        {"operation": "check_status", "services": ["bugcrowd", "hackerone"]},
    )
    print(f"[v19.3] Live totp_authenticator check_status ->\n{status_raw}")

    OUT_JSON.write_text(
        json.dumps(
            {
                "version": "v19.3-totp-authenticator",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "models_invoked": [gem_res, granite_res],
                "sandbox_result": sandbox_res,
                "deployment": deployment,
                "live_check_status": json.loads(status_raw),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
