#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Masked interactive CLI to enroll a Google Authenticator (RFC 6238 Base32 seed or
`otpauth://totp/...` URI) into Sabrina's Windows Credential Manager vault (`secret_store.py`).

Usage (run in your own local PowerShell window — NEVER paste secrets into chat):
    python Sabrina\tools\enroll_totp_vault.py bugcrowd
    python Sabrina\tools\enroll_totp_vault.py hackerone
"""
from __future__ import annotations

import argparse
import getpass
from pathlib import Path
import sys

SABRINA_ROOT = Path(__file__).resolve().parents[1]
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from tools.secret_store import set_secret


def main() -> int:
    parser = argparse.ArgumentParser(description="Enroll a TOTP seed in Sabrina's Windows Credential Manager vault.")
    parser.add_argument("service", help="Service name (e.g. bugcrowd, hackerone)")
    args = parser.parse_args()

    from tools.custom.totp_authenticator_plugin import generate_totp, secret_key_name_for_service

    key_name = secret_key_name_for_service(args.service)
    raw = getpass.getpass(f"Paste Google Authenticator Base32 secret or otpauth:// URI for {args.service} (hidden): ").strip()
    if not raw:
        print("Aborted: empty input.")
        return 1

    # Validate format and generate live 6-digit check code before saving
    totp_info = generate_totp(raw)
    set_secret(key_name, raw)
    print(f"\n[OK] Saved encrypted in Windows Credential Manager as '{key_name}@sabrina'.")
    print(
        f"[OK] Live Google Authenticator code right now: {totp_info['code']} "
        f"(valid for {totp_info['seconds_remaining']}s — verify this matches your Authenticator app!)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
