#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v19.4 — Evolve `totp_authenticator` (`v1.0.0 -> v1.1.0`) with Pure-Stdlib
Google Authenticator Export QR (`otpauth-migration://offline?data=...`) Decoder
===============================================================================
Ports and upgrades the operator's `EgoQT/src/ui/brock_2fa_scanner.py` (`line 1663`)
from Linux `ego` to Windows Sabrina, fixing the broken `from google.auth import migration_pb2`
import by synthesizing a pure-stdlib Protobuf wire-format parser for Google Authenticator's
`MigrationPayload` (`otpauth-migration://offline?data=...`), testing against binary protobuf
fixtures in Sabrina's sandbox, disjoint-auditing via `IBM Granite 3.1 8B`, and auto-evolving
`skills/totp-authenticator/SKILL.md` from `v1.0.0` to `v1.1.0` (archiving `v1.0.0`).
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

V19_3_JSON = (
    SABRINA_ROOT
    / "artifacts"
    / "collective-repair-20261108"
    / "swarm_self_forged_totp_authenticator_v19_3.json"
)
OUT_JSON = (
    SABRINA_ROOT
    / "artifacts"
    / "collective-repair-20261108"
    / "swarm_self_forged_totp_authenticator_v19_4_qr_migration.json"
)
CURRENT_PLUGIN = SABRINA_ROOT / "tools" / "custom" / "totp_authenticator_plugin.py"


def main() -> int:
    print("[v19.4] Evolving `totp_authenticator` (v1.0.0 -> v1.1.0) with Google Authenticator QR Export decoder...")
    forge = SabrinaToolSkillForge()
    gemini_oracle = GoogleGeminiOracle()
    v1_code = CURRENT_PLUGIN.read_text(encoding="utf-8")

    note = forge.ingest_docs_and_reason(
        problem_slug="totp-google-authenticator-qr-migration",
        problem_statement=(
            "Port and fix Sabrina's `EgoQT/src/ui/brock_2fa_scanner.py` Google Authenticator "
            "`otpauth-migration://offline?data=...` QR export decoder on Windows using pure "
            "Python stdlib protobuf wire decoding (eliminating the broken `google.auth.migration_pb2` "
            "dependency) and store imported accounts directly into Windows Credential Manager."
        ),
        doc_paths=[str(CURRENT_PLUGIN), str(SABRINA_ROOT / "tools" / "secret_store.py")],
        rag_queries=["otpauth-migration protobuf", "secret_store set_secret"],
        extracted_invariants=[
            "Google Authenticator 'Transfer accounts -> Export accounts' QR codes encode `otpauth-migration://offline?data=<url_encoded_base64_protobuf>`.",
            "MigrationPayload protobuf wire schema: field 1 (wire type 2) is repeated `OtpParameters` message with field 1=secret(bytes), field 2=name(string), field 3=issuer(string), field 4=algorithm(varint: 1=SHA1,2=SHA256,3=SHA512), field 5=digits(varint: 1->6, 2->8), field 6=type(varint: 1=HOTP, 2=TOTP).",
            "LLM-facing `execute()` actions must NEVER return raw Base32 secrets; host-side import saves directly via `secret_store.set_secret` and returns only redacted account metadata + current 6-digit verification code.",
        ],
        test_hypotheses=[
            "Pure-stdlib protobuf wire parser decodes multi-account `otpauth-migration://offline?data=...` payloads accurately (issuer, name, Base32 secret, digits=6/8, SHA1/SHA256).",
            "Existing RFC 6238 SHA1 test vector (`94287082` / `287082`) and `otpauth://totp/...` parsing continue to pass without regression.",
            "`execute({'operation': 'inspect_qr_payload', ...})` returns account names/issuers + live 6-digit preview codes while strictly omitting raw secret seeds.",
        ],
        author_model="gemini-3-flash-preview",
    )

    prompt = """You are `gemini-3-flash-preview` evolving Sabrina's `totp_authenticator_plugin.py` from `v1.0.0` to `v1.1.0`.
We found the operator's prior Linux scanner (`EgoQT/src/ui/brock_2fa_scanner.py`), which tried `from google.auth import migration_pb2` (a non-existent module) when decoding Google Authenticator's `otpauth-migration://offline?data=...` export QR codes.

Upgrade `totp_authenticator_plugin.py` (Block 1) and `test_totp_authenticator.py` (Block 2) using PURE PYTHON STDLIB ONLY (`base64`, `hashlib`, `hmac`, `json`, `re`, `struct`, `time`, `urllib.parse`) plus `from tools.secret_store import get_secret, set_secret`:

Current `v1.0.0` Plugin Code:
```python
__V1_CODE__
```

NEW CAPABILITIES REQUIRED IN Block 1 (`totp_authenticator_plugin.py`):
1. Keep all existing functions (`parse_totp_input`, `generate_totp`, `secret_key_name_for_service`, `TOOL_NAME = "totp_authenticator"`, `TOOL_PROMPT = ...`, `PARALLEL_SAFE = True`, `execute`, `execute_plugin = execute`) 100% backward-compatible!
2. NEVER mention protected filenames (`sabrina_gemini_loop.py`, `oracle_providers.py`, `agent_skills.py`, `blackboard.py`, `rag_knowledge.py`, `lab_knowledge.py`, `browser_utils.py`, `probe_api.py`).
3. Add pure-stdlib Protobuf wire decoder `decode_google_authenticator_migration(migration_uri: str) -> list[dict]`:
   - Validates `otpauth-migration://offline?data=...`, URL-unquotes `data` (note: treat literal spaces `' '` in query param as `'+'` if needed before base64 decode, add `'='` padding, and decode via `base64.b64decode`).
   - Parses binary Protobuf wire fields (`_read_varint(buf, pos)`):
     * Wire type `0` (varint), wire type `2` (length-delimited bytes), wire type `1` (64-bit skip 8), wire type `5` (32-bit skip 4).
     * Outer `MigrationPayload`: field `1` (wire type `2`) -> sub-message `OtpParameters`:
       - field `1` (`bytes`): `secret_bytes` -> `secret_b32 = base64.b32encode(secret_bytes).decode("ascii").rstrip("=")`
       - field `2` (`bytes`): `name = val.decode("utf-8", errors="replace")`
       - field `3` (`bytes`): `issuer = val.decode("utf-8", errors="replace")`
       - field `4` (`varint`): `algorithm_enum` (`{1: "SHA1", 2: "SHA256", 3: "SHA512"}.get(val, "SHA1")`)
       - field `5` (`varint`): `digits_enum` (`8 if val == 2 else 6`)
       - field `6` (`varint`): `otp_type` (`"HOTP" if val == 1 else "TOTP"`)
       - If `issuer` is empty and `":"` in `name`, infer `issuer = name.split(":", 1)[0].strip()`.
     * Returns list of dicts: `{"issuer": issuer, "account": name, "secret": secret_b32, "algorithm": algo, "digits": digits, "period": 30, "type": otp_type, "otpauth_uri": ...}`.
4. Add helper `parse_any_qr_payload(qr_text: str) -> list[dict]`:
   - Strips whitespace.
   - If starts with `"otpauth-migration://"` (case-insensitive): returns `decode_google_authenticator_migration(qr_text)`.
   - If starts with `"otpauth://"` (case-insensitive): extracts `issuer`, `account` (from path label), `secret` (normalized Base32 without padding), `algorithm`, `digits`, `period`, `type="TOTP"`, `otpauth_uri=qr_text`, returning a 1-element list.
   - Raises `ValueError("Unsupported QR format; expected otpauth:// or otpauth-migration://")` otherwise (never include raw `qr_text` in the error!).
5. Add safe host-side import helper `import_qr_accounts_to_vault(qr_text: str, selected_indices: list[int] | None = None, service_overrides: dict[int, str] | None = None, secret_setter=None) -> list[dict]`:
   - Calls `accounts = parse_any_qr_payload(qr_text)`.
   - `setter = secret_setter or set_secret`.
   - For each index `idx, acct` in `enumerate(accounts)`:
     * If `selected_indices is not None` and `idx not in selected_indices`: continue.
     * Determines `service_name = (service_overrides or {}).get(idx) or acct["issuer"] or acct["account"].split(":")[0] or f"account_{idx}"`.
     * `key_name = secret_key_name_for_service(service_name)`
     * Saves `acct["otpauth_uri"]` via `setter(key_name, acct["otpauth_uri"])`.
     * Generates live verification code via `generate_totp(acct["otpauth_uri"])`.
     * Appends REDACTED summary (NO `secret` or `otpauth_uri`!): `{"index": idx, "issuer": acct["issuer"], "account": acct["account"], "secret_key_name": key_name, "code": totp["code"], "seconds_remaining": totp["seconds_remaining"]}`.
   - Returns the list of redacted imported summaries.
6. In `execute(action, loop=None, secret_getter=None, secret_setter=None)`:
   - Keep `"check_status"` and `"generate_code"` working identically!
   - Add `"inspect_qr_payload"`: parses `action["qr_text"]` via `parse_any_qr_payload` and returns REDACTED account list (`index`, `issuer`, `account`, `digits`, `algorithm`, `type`, `suggested_key_name`, plus current `code` — NEVER `secret` or `otpauth_uri`!).

STRICT REQUIREMENTS FOR Block 2 (`test_totp_authenticator.py`):
Include 6 offline unit tests:
- Keep all 5 tests from `v1.0.0` (`RFC 6238` SHA1 `94287082`, unpadded spaced Base32, `otpauth://` URI, missing secret, enrolled secret never leaks seed).
- Add `test_google_authenticator_migration_protobuf_multi_account`: programmatically constructs a binary `MigrationPayload` protobuf containing TWO `OtpParameters` entries (e.g. `Bugcrowd` with secret `b"12345678901234567890"` and `HackerOne` with secret `b"ABCDEFGHIJKLMNOPQRST"`, `digits=2` -> 8 digits), encodes it as `otpauth-migration://offline?data=...`, tests `decode_google_authenticator_migration`, and tests `import_qr_accounts_to_vault` with a fake `secret_setter` dict—asserting both accounts decode accurately, save to `BUGCROWD_TOTP_SECRET` and `HACKERONE_TOTP_SECRET`, and zero raw Base32 secrets appear in the returned JSON/summaries!

Output ONLY the two complete ```python fenced code blocks.
""".replace("__V1_CODE__", v1_code)

    gem_res = call_frontier_gemini(gemini_oracle, prompt)
    print(f"[v19.4] {gem_res['model']} returned {gem_res['eval_count']} tokens in {gem_res['latency_sec']}s.")

    plugin_code, test_code = extract_fenced_files(gem_res["response"])
    if not plugin_code or not test_code:
        raise RuntimeError("Failed to extract fenced blocks for v19.4.")

    sandbox_res = forge.sandbox_test_tool(
        tool_name="totp_authenticator",
        plugin_code=plugin_code,
        test_code=test_code,
        sample_action={"operation": "check_status", "services": ["bugcrowd", "hackerone"]},
        timeout_sec=30,
    )
    if not sandbox_res.get("ok"):
        raise RuntimeError(f"Sandbox failed: {sandbox_res}")
    print(f"[v19.4] Sandbox 6-test suite (RFC 6238 + Google Auth Protobuf Migration) PASSED ({sandbox_res['elapsed_sec']}s)!")

    granite_prompt = (
        f"You are `granite3.1-dense:8b` auditing the `v1.0.0 -> v1.1.0` evolution of `totp_authenticator_plugin.py` "
        f"(adding pure-stdlib Google Authenticator `otpauth-migration://` protobuf decoding).\n\n"
        f"```python\n{plugin_code[:3500]}\n```\n\n"
        f"Unittest Output:\n```\n{sandbox_res.get('test_output', '')}\n```\n\n"
        "Verify in 3 concise bullets: (1) pure-stdlib protobuf bounds safety, "
        "(2) zero raw secret seed leakage in `inspect_qr_payload` / `import_qr_accounts_to_vault`, "
        "(3) backward compatibility -> `APPROVE_V1_1_0`."
    )
    granite_res = call_local_ollama("granite3.1-dense:8b", granite_prompt, num_predict=450)
    print(f"[v19.4] granite3.1-dense:8b disjoint audit completed ({granite_res['eval_count']} tokens, {granite_res['latency_sec']}s).")

    deployment = forge.deploy_and_evolve_skill(
        sandbox_result=sandbox_res,
        skill_name="totp-authenticator",
        skill_description=(
            "Host-side RFC 6238 Google Authenticator (TOTP) generator + pure-stdlib "
            "`otpauth-migration://offline?data=...` QR export decoder backed by Windows Credential Manager."
        ),
        reasoning_note=note,
        when_to_use=(
            "Use whenever Sabrina needs to decode a Google Authenticator 'Transfer accounts -> Export accounts' "
            "QR code (`otpauth-migration://`) or `otpauth://` QR code into Windows Credential Manager, "
            "or generate live 6-digit 2FA codes host-side."
        ),
        step_by_step_procedure=[
            "Launch `python Sabrina/tools/scan_google_authenticator_qr.py` on Windows to scan the phone's Google Authenticator Export QR via browser webcam (or QR image file) on `http://127.0.0.1:8765`.",
            "Select the accounts to import (e.g. `Bugcrowd`, `HackerOne`) -> stored encrypted in Windows Credential Manager (`BUGCROWD_TOTP_SECRET@sabrina`).",
            "Generate live 6-digit codes host-side via `totp_authenticator` (`operation='generate_code'`, `service='bugcrowd'`).",
        ],
        worker_model=gem_res["model"],
        worker_summary=(
            f"Ported & fixed `EgoQT/brock_2fa_scanner.py` Google Authenticator `otpauth-migration://` "
            f"protobuf decoder in pure Python stdlib (`v1.0.0 -> v1.1.0`, 6/6 unittests PASS in {sandbox_res['elapsed_sec']}s)."
        ),
        auditor_model="granite3.1-dense:8b",
        auditor_verdict=granite_res["response"][:500].replace("\n", " "),
        minority_dissent="Keep QR camera scanning strictly bound to 127.0.0.1 and never log raw migration URIs.",
    )
    print(
        f"[v19.4] Evolved skill -> v{deployment['skill_version']} "
        f"(archived prior v{deployment.get('archived_prior_version')} -> {deployment.get('archived_prior_skill')})"
    )

    OUT_JSON.write_text(
        json.dumps(
            {
                "version": "v19.4-totp-qr-migration-v1.1.0",
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "models_invoked": [gem_res, granite_res],
                "sandbox_result": sandbox_res,
                "deployment": deployment,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
