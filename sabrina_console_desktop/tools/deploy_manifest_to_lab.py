"""Deploy approved Sabrina manifest to lab host.

Syncs approved manifest files to ego.local:/home/ego/bot/Sabrina-windows-sync,
preserving replaced versions as backups, verifying exact SHA-256 hashes,
validating remote syntax via python3 -m py_compile, and checking coordinator health.
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import sys
import time
import urllib.request
from pathlib import Path
import paramiko

ROOT = Path(__file__).resolve().parents[1]

APPROVED_MANIFEST = [
    "tools/sabrina_host_execution.py",
    "tests/unit/test_sabrina_host_execution.py",
    "tools/sabrina_evaluation_engine.py",
    "tests/unit/test_sabrina_evaluation_engine.py",
    "tools/sabrina_console_performance.py",
    "sabrina_operator_console.py",
    "missions/__init__.py",
    "missions/cross_app_handoff_helper/__init__.py",
    "missions/cross_app_handoff_helper/task_handoff_helper.py",
    "missions/cross_app_handoff_helper/test_task_handoff_helper.py",
    "missions/cross_app_handoff/__init__.py",
    "missions/cross_app_handoff/task_handoff_helper.py",
    "missions/cross_app_handoff/test_task_handoff_helper.py",
    "skills/cross-app-handoff/SKILL.md",
]


def check_coordinator_health(url: str = "http://ego.local:8090/health") -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "sabrina-deploy/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Coordinator returned HTTP {resp.status}")
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("service") != "sabrina-lab-coordinator":
            raise RuntimeError(f"Unexpected coordinator service identity: {data}")
        return data


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    cur = ""
    for part in parts:
        cur += "/" + part
        try:
            sftp.stat(cur)
        except FileNotFoundError:
            sftp.mkdir(cur)


def run_remote_cmd(client: paramiko.SSHClient, cmd: str) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(cmd)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8")
    err = stderr.read().decode("utf-8")
    return exit_code, out, err


def deploy(
    host: str = "ego.local",
    user: str = "ego",
    password: str = "ego",
    remote_base: str = "/home/ego/bot/Sabrina-windows-sync",
    manifest: list[str] | None = None,
) -> dict:
    files_to_sync = manifest or APPROVED_MANIFEST
    results = []
    timestamp = int(time.time())

    print(f"=== [1/5] Checking Coordinator Health Pre-Deploy ===")
    pre_health = check_coordinator_health()
    print(f"Coordinator OK: {pre_health}")

    print(f"\n=== [2/5] Validating Local Syntax for Manifest ===")
    for rel_path in files_to_sync:
        local_path = ROOT / rel_path
        if not local_path.exists():
            raise FileNotFoundError(f"Manifest file missing locally: {local_path}")
        if local_path.suffix == ".py":
            import py_compile
            py_compile.compile(str(local_path), doraise=True)
            print(f"  [OK py_compile] {rel_path}")

    print(f"\n=== [3/5] Connecting to {user}@{host} ===")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.load_system_host_keys()
    client.connect(host, username=user, password=password, timeout=10)
    sftp = client.open_sftp()

    try:
        print(f"\n=== [4/5] Deploying Manifest with Version Preservation ===")
        for rel_path in files_to_sync:
            local_path = ROOT / rel_path
            local_hash = sha256_file(local_path)
            posix_rel = rel_path.replace("\\", "/")
            remote_target = posixpath.join(remote_base, posix_rel)
            remote_dir = posixpath.dirname(remote_target)

            ensure_remote_dir(sftp, remote_dir)

            # Check if remote file exists and differs
            remote_exists = False
            backup_created = None
            try:
                sftp.stat(remote_target)
                remote_exists = True
                # Get remote sha256
                c, out, _ = run_remote_cmd(client, f"sha256sum '{remote_target}'")
                remote_sha = out.split()[0] if c == 0 and out else ""
                if remote_sha != local_hash:
                    # Preserve replaced version
                    backup_target = f"{remote_target}.bak.{timestamp}"
                    run_remote_cmd(client, f"cp -p '{remote_target}' '{backup_target}'")
                    backup_created = backup_target
            except FileNotFoundError:
                remote_exists = False

            # Transfer file
            sftp.put(str(local_path), remote_target)

            # Verify remote hash
            c, out, err = run_remote_cmd(client, f"sha256sum '{remote_target}'")
            if c != 0:
                raise RuntimeError(f"Failed to compute remote sha256 for {remote_target}: {err}")
            remote_verified_hash = out.split()[0]
            if remote_verified_hash != local_hash:
                raise ValueError(f"Hash mismatch on {rel_path}: local={local_hash} remote={remote_verified_hash}")

            # Verify remote syntax for python files
            remote_syntax_ok = True
            if local_path.suffix == ".py":
                c, out, err = run_remote_cmd(client, f"python3 -m py_compile '{remote_target}'")
                if c != 0:
                    raise RuntimeError(f"Remote syntax error in {remote_target}: {err}")
                remote_syntax_ok = True

            item_result = {
                "file": rel_path,
                "local_sha256": local_hash,
                "remote_sha256": remote_verified_hash,
                "backup_preserved": backup_created,
                "remote_syntax_valid": remote_syntax_ok,
            }
            results.append(item_result)
            status_str = f"[DEPLOYED] {rel_path} -> SHA256 matches ({local_hash[:12]}...)"
            if backup_created:
                status_str += f" [BACKUP: {posixpath.basename(backup_created)}]"
            print(f"  {status_str}")

        print(f"\n=== [5/5] Checking Coordinator Health Post-Deploy ===")
        post_health = check_coordinator_health()
        print(f"Coordinator OK: {post_health}")

    finally:
        sftp.close()
        client.close()

    summary = {
        "ok": True,
        "files_deployed": len(results),
        "pre_deploy_coordinator": pre_health,
        "post_deploy_coordinator": post_health,
        "results": results,
    }
    return summary


if __name__ == "__main__":
    summary = deploy()
    print(f"\nDeployment Succeeded! Total files deployed: {summary['files_deployed']}")
