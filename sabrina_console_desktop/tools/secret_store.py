"""
tools/secret_store.py
======================
Centralized secret storage using the OS credential store (Windows Credential
Manager, via `keyring`) instead of scattering API keys across dozens of .env
files in project clones/sandboxes/backups.

NOTE: this module was originally named tools/secrets.py. Renamed after
discovering a real bug it caused: running any script directly from inside
tools/ (e.g. `python tools/whatever.py`, which is exactly how every engine in
tools/engines/ is invoked via run_shell) puts tools/ itself on sys.path,
which meant a bare `import secrets` anywhere in that process's import chain
resolved to THIS file instead of Python's own stdlib `secrets` module.
Confirmed this silently broke numpy (whose numpy.random.bit_generator does
`from secrets import randbits`-style internal seeding) with a confusing
`ImportError: cannot import name randbits` that had nothing to do with numpy
itself. Any module name here must never collide with a stdlib module name.

Why this exists: while investigating a "credit balance too low" Anthropic API
error, the user needed to find a different (funded) key they'd saved before --
searching turned up 15+ .env-style files across bot/, bot2/, and backup/
project clones/sandboxes, most templates or duplicates, with no way to tell
which one (if any) held the current real key without checking each by hand.
A single OS-level entry, addressable by name regardless of which project
directory you're standing in, makes "where is the current key" a question
that never needs asking again.

Verified live: keyring correctly resolves to keyring.backends.Windows.WinVaultKeyring
on this machine (real Windows Credential Manager, DPAPI-encrypted, tied to the
Windows user login -- not a plaintext file), and a set/get/delete round-trip works.

Falls back to environment variables / .env files for any secret not yet
migrated, so nothing breaks during the transition -- this is additive, not a
hard cutover.
"""

import os

SERVICE_NAME = "sabrina"

try:
    import keyring
    _KEYRING_AVAILABLE = True
except ImportError:
    _KEYRING_AVAILABLE = False


def get_secret(name: str, env_fallback_paths=None) -> str:
    """
    Look up a secret by name: OS credential store first, then os.environ,
    then any .env-style files given (default: just ".env" in the cwd).
    """
    if _KEYRING_AVAILABLE:
        try:
            val = keyring.get_password(SERVICE_NAME, name)
            if val:
                return val
        except Exception:
            pass  # backend unavailable (e.g. headless/CI environment) -- fall through

    val = os.environ.get(name, "")
    if val:
        return val

    for path in (env_fallback_paths or [".env"]):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith(f"{name}="):
                            return line.strip().split("=", 1)[1].strip("'\"")
            except Exception:
                pass
    return ""


def set_secret(name: str, value: str) -> None:
    """Store a secret in the OS credential store."""
    if not _KEYRING_AVAILABLE:
        raise RuntimeError("keyring package not installed -- pip install keyring")
    keyring.set_password(SERVICE_NAME, name, value)


def delete_secret(name: str) -> None:
    if not _KEYRING_AVAILABLE:
        raise RuntimeError("keyring package not installed -- pip install keyring")
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except keyring.errors.PasswordDeleteError:
        pass  # already gone


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


if __name__ == "__main__":
    import getpass
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "set":
        set_secret(sys.argv[2], getpass.getpass(f"Value for {sys.argv[2]}: "))
        print(f"Stored '{sys.argv[2]}' in the OS credential store under service '{SERVICE_NAME}'.")
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        print(f"'{sys.argv[2]}' present: {has_secret(sys.argv[2])}")
    else:
        print("Usage: python tools/secret_store.py set <NAME>   -- store a secret (hidden prompt)")
        print("       python tools/secret_store.py check <NAME> -- check if a secret is resolvable")
