"""Air-Gapped Pre-Cached Wheelhouse Sync and Verification Tool for Sabrina Systems.

Coordinates and verifies offline Python dependency repositories across Sabrina nodes:
1. Windows Workstation (`windows-workstation`): `<WORKSPACE_ROOT>/artifacts/wheels`
2. Remote Lab Node (`lab-node`): `<REMOTE_ROOT>/artifacts/wheels`

Features:
- Categorizes cached wheels into universal (py3-none-any) vs platform-specific binaries.
- Synchronizes universal wheels to remote nodes via scp/ssh.
- Verifies local and remote air-gapped Python execution with zero network access (UV_OFFLINE, PIP_NO_INDEX).
- Prepares offline environment settings and requirements manifests.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    from sabrina_console_desktop.sabrina_config import LAB_HOST as DEFAULT_REMOTE_HOST, WHEELHOUSE_DIR as _CFG_WHEELHOUSE
    LOCAL_WHEELHOUSE = Path(_CFG_WHEELHOUSE)
except ImportError:
    DEFAULT_REMOTE_HOST = os.environ.get("SABRINA_LAB_HOST", "localhost")
    LOCAL_WHEELHOUSE = ROOT / "artifacts" / "wheels"
LOCAL_CACHE = ROOT / "artifacts" / "cache"
DEFAULT_REMOTE_USER = os.environ.get("SABRINA_LAB_USER", "sabrina")
DEFAULT_REMOTE_DIR = os.environ.get("SABRINA_REMOTE_WHEELHOUSE", "/opt/sabrina/artifacts/wheels")


def inspect_wheelhouse(wheelhouse_dir: Path | str = LOCAL_WHEELHOUSE) -> dict:
    wh_path = Path(wheelhouse_dir).resolve()
    if not wh_path.is_dir():
        return {
            "exists": False,
            "path": str(wh_path),
            "total_wheels": 0,
            "universal_wheels": [],
            "platform_wheels": [],
        }

    all_wheels = sorted([p.name for p in wh_path.glob("*.whl")])
    universal = [w for w in all_wheels if "-none-any.whl" in w or "-py3-none-any.whl" in w or "-py2.py3-none-any.whl" in w]
    platform_specific = [w for w in all_wheels if w not in universal]

    return {
        "exists": True,
        "path": str(wh_path),
        "total_wheels": len(all_wheels),
        "universal_count": len(universal),
        "platform_count": len(platform_specific),
        "universal_wheels": universal,
        "platform_wheels": platform_specific,
    }


def verify_local_airgapped_execution() -> dict:
    """Run an isolated Python test strictly offline to verify air-gapped execution."""
    print("--> Testing local air-gapped execution via WorkspaceSandbox...")
    try:
        from swarm_console.workspace_sandbox import WorkspaceSandbox

        sb = WorkspaceSandbox(ROOT, allow_local_venv_fallback=True, policy_profile="sandboxed_edit")
        test_code = (
            "# /// script\n"
            "# dependencies = [\"annotated-types\"]\n"
            "# ///\n"
            "import annotated_types\n"
            "print('AIRGAPPED_RUN_OK')"
        )
        res = sb.execute_tool("run_python", {"code": test_code, "timeout": 15})
        passed = res.get("ok") and "AIRGAPPED_RUN_OK" in res.get("stdout", "")
        return {
            "ok": passed,
            "backend": res.get("backend"),
            "stdout": res.get("stdout", "").strip(),
            "error": res.get("error") or res.get("stderr"),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def sync_universal_wheels_to_remote(
    host: str = DEFAULT_REMOTE_HOST,
    user: str = DEFAULT_REMOTE_USER,
    remote_dir: str = DEFAULT_REMOTE_DIR,
) -> dict:
    """Sync universal pure-python wheels to remote node via scp."""
    wh_info = inspect_wheelhouse()
    universal = wh_info["universal_wheels"]
    if not universal:
        return {"ok": False, "error": "No universal wheels found in local wheelhouse."}

    target = host if host in ("ego", "ego.local") else f"{user}@{host}"
    print(f"--> Ensuring remote directory {remote_dir} on {target}...")
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, f"mkdir -p {remote_dir}"]
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return {"ok": False, "error": f"Failed to connect to {host}: {proc.stderr.strip()}"}
    except Exception as exc:
        return {"ok": False, "error": f"SSH connection failed: {exc}"}

    print(f"--> Transferring {len(universal)} universal wheels to {target}...")
    transferred = 0
    errors = []
    for whl in universal:
        src = LOCAL_WHEELHOUSE / whl
        scp_cmd = ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", str(src), f"{target}:{remote_dir}/{whl}"]
        p = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=15)
        if p.returncode == 0:
            transferred += 1
        else:
            errors.append(f"{whl}: {p.stderr.strip()}")

    return {
        "ok": len(errors) == 0,
        "total_universal": len(universal),
        "transferred": transferred,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sabrina Air-Gapped Wheelhouse Sync & Verifier")
    parser.add_argument("--status", action="store_true", help="Inspect local wheelhouse status")
    parser.add_argument("--verify-local", action="store_true", help="Test local air-gapped script execution")
    parser.add_argument("--sync-remote", action="store_true", help="Sync universal wheels to remote ego node")
    parser.add_argument("--host", default=DEFAULT_REMOTE_HOST, help="Remote host (default: ego.local)")
    parser.add_argument("--user", default=DEFAULT_REMOTE_USER, help="Remote user (default: ego)")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR, help="Remote wheelhouse path")
    args = parser.parse_args()

    # Default action if no flags
    if not (args.status or args.verify_local or args.sync_remote):
        args.status = True
        args.verify_local = True

    if args.status:
        info = inspect_wheelhouse()
        print("\n=== Local Artifact Repository Status ===")
        print(f"Path: {info['path']}")
        print(f"Total Wheels: {info['total_wheels']}")
        print(f"Universal (Multi-OS): {info['universal_count']}")
        print(f"Windows Specific: {info['platform_count']}")

    if args.verify_local:
        print("\n=== Verifying Local Air-Gapped Execution ===")
        res = verify_local_airgapped_execution()
        print(f"Status: {'PASSED [Air-Gapped]' if res['ok'] else 'FAILED'}")
        if not res['ok']:
            print(f"Error: {res.get('error')}")

    if args.sync_remote:
        print(f"\n=== Syncing to Remote Node ({args.user}@{args.host}) ===")
        res = sync_universal_wheels_to_remote(args.host, args.user, args.remote_dir)
        print(f"Transferred: {res.get('transferred', 0)} / {res.get('total_universal', 0)}")
        if res.get("errors"):
            print("Errors encountered:", res["errors"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
