"""Pre-flight packaging and security audit script for Sabrina Operator Console release."""
from __future__ import annotations

import os
import re
import sys
import hashlib
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

# Regexes matching actual live API keys or private keys
SECRET_REGEXES = [
    (re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----"), "Private Key Header"),
    (re.compile(r"AIzaSy[A-Za-z0-9_-]{33}"), "Google API Key"),
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{40,}"), "OpenAI Project Key"),
    (re.compile(r"sk-[A-Za-z0-9]{48}"), "OpenAI API Key"),
]


def check_git_exclusions() -> bool:
    print("[1/5] Verifying .gitignore rules...")
    gi_path = WORKSPACE / ".gitignore"
    if not gi_path.is_file():
        print("  FAIL: .gitignore is missing!")
        return False
    text = gi_path.read_text(encoding="utf-8")
    required = ["provider_vault.json", "*.sqlite3", "__pycache__/", ".env"]
    for req in required:
        if req not in text:
            print(f"  FAIL: .gitignore missing rule for '{req}'")
            return False
    print("  PASS: .gitignore excludes credentials, local SQLite databases, and caches.")
    return True


def check_synced_html() -> bool:
    print("[2/5] Checking HTML synchronization...")
    h1 = WORKSPACE / "sabrina_console_desktop" / "sabrina_desktop_ui.html"
    h2 = WORKSPACE / "sabrina_desktop_ui.html"
    if not h1.is_file() or not h2.is_file():
        print("  FAIL: One or both desktop UI HTML files are missing.")
        return False
    h1_hash = hashlib.sha256(h1.read_bytes()).hexdigest()
    h2_hash = hashlib.sha256(h2.read_bytes()).hexdigest()
    if h1_hash != h2_hash:
        print("  FAIL: Root sabrina_desktop_ui.html is not synced with sabrina_console_desktop copy.")
        return False
    print("  PASS: HTML files are bit-for-bit synchronized.")
    return True


def check_leaked_secrets() -> bool:
    print("[3/5] Auditing codebase for leaked secrets...")
    suspicious: list[str] = []
    text_extensions = {".py", ".html", ".json", ".md", ".yml", ".yaml", ".txt"}
    for root, dirs, files in os.walk(WORKSPACE):
        # Skip ignored directories
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "venv", "__pycache__", "availability-backups"}]
        for f in files:
            path = Path(root) / f
            if path.suffix.lower() not in text_extensions:
                continue
            if path.name == "provider_vault.json" or path.name.endswith(".sqlite3") or path.name == "package_for_github.py":
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
                for regex, label in SECRET_REGEXES:
                    match = regex.search(content)
                    if match:
                        suspicious.append(f"{path.relative_to(WORKSPACE)}: matched {label} ({match.group(0)[:8]}...)")
            except Exception:
                pass

    if suspicious:
        print("  FAIL: Potential secrets detected:")
        for s in suspicious[:5]:
            print(f"    - {s}")
        return False
    print("  PASS: No raw API keys or credentials detected in audited text files.")
    return True


def check_safety_invariants() -> bool:
    print("[4/5] Checking safety boundary invariant (execution_authorized=False)...")
    runner_py = WORKSPACE / "sabrina_console_desktop" / "live_swarm_runner.py"
    if not runner_py.is_file():
        print("  FAIL: live_swarm_runner.py not found.")
        return False
    text = runner_py.read_text(encoding="utf-8")
    if '"execution_authorized": False' not in text and "'execution_authorized': False" not in text:
        print("  FAIL: execution_authorized: False invariant not found in runner.")
        return False
    print("  PASS: execution_authorized: False invariant verified.")
    return True


def run_unit_tests() -> bool:
    print("[5/5] Running core test suite...")
    import unittest
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames([
        "sabrina_console_desktop.test_console_bridge_wiring",
        "sabrina_console_desktop.test_live_swarm_runner",
        "swarm_command.test_swarm_command",
        "swarm_console.test_swarm_console",
    ])
    runner = unittest.TextTestRunner(verbosity=0)
    result = runner.run(suite)
    if not result.wasSuccessful():
        print(f"  FAIL: Unit tests failed (errors={len(result.errors)}, failures={len(result.failures)})")
        return False
    print(f"  PASS: All {result.testsRun} unit tests passed.")
    return True


def main() -> int:
    print("=== SABRINA OPERATOR CONSOLE PRE-FLIGHT AUDIT ===")
    checks = [
        check_git_exclusions(),
        check_synced_html(),
        check_leaked_secrets(),
        check_safety_invariants(),
        run_unit_tests(),
    ]
    if all(checks):
        print("\nSUCCESS: Codebase is verified, sanitized, and ready for GitHub publication!")
        return 0
    else:
        print("\nFAILURE: One or more pre-flight checks failed. Review issues above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
