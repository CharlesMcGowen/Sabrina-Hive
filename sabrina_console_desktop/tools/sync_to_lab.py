"""
tools/sync_to_lab.py
=====================
A minimal, dependency-light replacement for rsync (not available in this
Windows/Git Bash environment) to push the Windows-side Sabrina codebase to a
real target on the lab host. Built after confirming tonight that NO real sync
pipeline existed: two lab-host copies (Sabrina-windows-sync, sabrina_win_arena)
were both stale (dated Sep 10 and Sep 17), had no .git, no cron job, no
rsync/syncthing/unison daemon -- every fix all night had to be scp'd by hand,
one file at a time, and it was easy to forget one (the LAB_HOST fix never made
it over).

This only pushes files that are actually new or changed (by size + mtime,
then a content hash if those match but differ slightly), so repeat runs are
fast. It never deletes anything on the remote side -- a missing local file
just doesn't get pushed, nothing is removed remotely. Run with --dry-run
first on any new destination to see exactly what would change before it
touches anything.

Usage:
    python -m tools.sync_to_lab --dest /home/ego/bot/Sabrina-windows-sync --dry-run
    python -m tools.sync_to_lab --dest /home/ego/bot/Sabrina-windows-sync
"""

import argparse
import hashlib
import os
import posixpath
import stat as statmod
import sys
from pathlib import Path

import os
import paramiko

try:
    from sabrina_config import LAB_IP as DEFAULT_HOST
except ImportError:
    DEFAULT_HOST = os.environ.get("SABRINA_LAB_IP", "127.0.0.1")
DEFAULT_USER = os.environ.get("SABRINA_LAB_USER", "sabrina")

EXCLUDE_DIR_NAMES = {
    ".git", "__pycache__", ".pytest_cache", ".vscode", "node_modules",
    ".sandbox_clone_7bc2e1", ".sandbox_clone_9cdbb5", "venv", ".venv",
    "Claude outputs", "storage", "evidence", "fleet_logs", "bench",
    # Big data/vendored/duplicate/runtime trees -- this is a CODE sync, not a data sync. Excluding
    # data/ especially protects the lab's OWN runtime DBs/ledgers from being clobbered by Windows copies.
    "models", "security_items", "data", "dev", "Sabrina-dev", "archives", "scratch",
    "logs", "github_variant", "datastore", "deploy", "analysis",
}
# Also skip any .sandbox_clone_* (numbered variants) generically.

# Live-observed tonight: a dry run against one subfolder swept up 89 files,
# almost all of them runtime data (queen_cot_*.jsonl conversation logs, a
# packaged amass.exe binary, evidence JSON) rather than source. This is a
# CODE sync, not a data/log sync -- runtime artifacts regenerate on the lab
# host from real runs and don't belong in a source push.
EXCLUDE_FILE_SUFFIXES = {".pyc", ".pyo", ".jsonl", ".exe", ".log"}
EXCLUDE_EXACT_NAMES = {".env"}  # never push real secrets; .env.example is fine


def _should_skip_dir(name: str) -> bool:
    return name in EXCLUDE_DIR_NAMES or name.startswith(".sandbox_clone_")


def _should_skip_file(name: str) -> bool:
    if name in EXCLUDE_EXACT_NAMES:
        return True
    return any(name.endswith(suf) for suf in EXCLUDE_FILE_SUFFIXES)


def _local_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _remote_sha256(sftp: paramiko.SFTPClient, remote_path: str) -> str:
    h = hashlib.sha256()
    with sftp.open(remote_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sync(local_root: Path, host: str, user: str, remote_root: str, dry_run: bool) -> None:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.load_system_host_keys()
    client.connect(host, username=user, timeout=10)
    sftp = client.open_sftp()

    pushed, skipped_same, created_dirs = 0, 0, 0

    for dirpath, dirnames, filenames in os.walk(local_root):
        dirnames[:] = [d for d in dirnames if not _should_skip_dir(d)]
        rel_dir = os.path.relpath(dirpath, local_root)
        remote_dir = remote_root if rel_dir == "." else posixpath.join(remote_root, *rel_dir.replace("\\", "/").split("/"))

        if not dry_run:
            try:
                _ensure_remote_dir(sftp, remote_dir)
            except Exception as e:
                print(f"FAILED creating remote dir '{remote_dir}' (from local '{dirpath}'): {type(e).__name__}: {e}")
                raise

        for fname in filenames:
            if _should_skip_file(fname):
                continue
            local_path = Path(dirpath) / fname
            remote_path = posixpath.join(remote_dir, fname)

            try:
                local_size = local_path.stat().st_size
            except OSError:
                continue

            needs_push = True
            try:
                remote_stat = sftp.stat(remote_path)
                if remote_stat.st_size == local_size:
                    if _local_sha256(local_path) == _remote_sha256(sftp, remote_path):
                        needs_push = False
            except FileNotFoundError:
                pass

            if not needs_push:
                skipped_same += 1
                continue

            rel_path = os.path.relpath(local_path, local_root)
            if dry_run:
                print(f"[would push] {rel_path}")
            else:
                sftp.put(str(local_path), remote_path)
                print(f"[pushed]     {rel_path}")
            pushed += 1

    sftp.close()
    client.close()
    verb = "Would push" if dry_run else "Pushed"
    print(f"\n{verb} {pushed} file(s), {skipped_same} already up to date.")


def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    parts = remote_dir.strip("/").split("/")
    current = ""
    for part in parts:
        current += "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def push_single_file(local_path: Path, remote_path: str, host: str = DEFAULT_HOST,
                      user: str = DEFAULT_USER) -> None:
    """
    Push exactly one file to exactly one remote path. Built for the
    approve-a-patch-then-deploy-it step in the engineering harness -- a whole
    tree-walk sync is unnecessary overhead when only one file changed.
    """
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.load_system_host_keys()
    client.connect(host, username=user, timeout=10)
    sftp = client.open_sftp()
    try:
        _ensure_remote_dir(sftp, posixpath.dirname(remote_path))
        sftp.put(str(local_path), remote_path)
    finally:
        sftp.close()
        client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dest", required=True, help="Remote destination root, e.g. /home/ego/bot/Sabrina-windows-sync")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--source", default=str(Path(__file__).resolve().parent.parent),
                     help="Local Sabrina root (defaults to the repo this script lives in)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be pushed without touching the remote")
    args = ap.parse_args()

    sync(Path(args.source), args.host, args.user, args.dest, args.dry_run)
