"""Cross-drive storage inventory, verified data migration, and backup manager.

Supports both local Windows drives and remote Linux drives on ``ego`` (``ego@ego``
via SSH). Mutating operations default to ``dry_run=True`` and require explicit
``--execute`` / ``dry_run=False`` plus verification before any source removal.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import uuid
from typing import Any

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
RECEIPT_DIR = WORKSPACE_ROOT / "data" / "storage_receipts"
DEFAULT_EGO_HOST = os.environ.get("SABRINA_EGO_SSH_HOST", "ego@ego")
DEFAULT_BACKUP_ROOT_EGO = "/mnt/14tb/backups"
WARN_PERCENT = 90.0
CRIT_PERCENT = 95.0

# Paths on ego that must never be moved or deleted as a whole root.
PROTECTED_EGO_PATHS = {
    "/",
    "/boot",
    "/boot/efi",
    "/dev",
    "/etc",
    "/home",
    "/proc",
    "/root",
    "/run",
    "/sys",
    "/tmp",
    "/usr",
    "/var",
    "/mnt",
    "/mnt/14tb",
    "/mnt/nvme",
    "/mnt/webapps-nvme",
}


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts_slug() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def _human_bytes(n: int) -> str:
    val = float(max(0, n))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if val < 1024.0 or unit == "PiB":
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PiB"


def _run_local(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _run_ego(remote_cmd: str, host: str = DEFAULT_EGO_HOST, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        host,
        remote_cmd,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _write_receipt(kind: str, payload: dict[str, Any]) -> str:
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    receipt_id = f"{_ts_slug()}_{kind}_{uuid.uuid4().hex[:8]}"
    path = RECEIPT_DIR / f"{receipt_id}.json"
    record = {"receipt_id": receipt_id, "kind": kind, "recorded_at": _utc_now(), **payload}
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return str(path)


def _validate_ego_path(path: str) -> str:
    p = path.strip()
    if not p.startswith("/"):
        raise ValueError(f"ego path must be absolute: {path!r}")
    norm = os.path.normpath(p).replace("\\", "/")
    if norm in PROTECTED_EGO_PATHS:
        raise ValueError(f"refusing operation on protected root/mount path: {norm!r}")
    return norm


def _ego_fstype(path: str, host: str = DEFAULT_EGO_HOST) -> str:
    q = shlex.quote(path)
    res = _run_ego(f"df -T --output=fstype {q} 2>/dev/null | tail -n 1", host=host, timeout=15)
    return res.stdout.strip().lower() if res.returncode == 0 else ""


def _ego_free_bytes(path: str, host: str = DEFAULT_EGO_HOST) -> int:
    q = shlex.quote(path)
    # Probe parent directory if target path does not yet exist.
    cmd = (
        f"p={q}; while [ ! -e \"$p\" ] && [ \"$p\" != \"/\" ]; do p=$(dirname \"$p\"); done; "
        f"df -B1 --output=avail \"$p\" | tail -n 1"
    )
    res = _run_ego(cmd, host=host, timeout=15)
    if res.returncode != 0:
        raise RuntimeError(f"failed to query free space on ego for {path}: {res.stderr.strip()}")
    return int(res.stdout.strip())


def _ego_path_size_bytes(path: str, host: str = DEFAULT_EGO_HOST) -> int:
    q = shlex.quote(path)
    res = _run_ego(f"du -sb {q} | cut -f1", host=host, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"failed to measure size of {path} on ego: {res.stderr.strip()}")
    return int(res.stdout.strip())


def _windows_drives() -> list[dict[str, Any]]:
    drives: list[dict[str, Any]] = []
    if hasattr(os, "listdrives"):
        roots = os.listdrives()
    else:
        roots = [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
    for root in roots:
        try:
            usage = shutil.disk_usage(root)
            pct = (usage.used / usage.total * 100.0) if usage.total else 0.0
            drives.append({
                "host": "windows",
                "mountpoint": root,
                "fstype": "windows",
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "avail_bytes": usage.free,
                "total_human": _human_bytes(usage.total),
                "used_human": _human_bytes(usage.used),
                "avail_human": _human_bytes(usage.free),
                "use_percent": round(pct, 1),
                "status": "critical" if pct >= CRIT_PERCENT else ("warning" if pct >= WARN_PERCENT else "ok"),
            })
        except OSError as exc:
            drives.append({"host": "windows", "mountpoint": root, "status": "error", "error": str(exc)})
    return drives


def _ego_drives(host: str = DEFAULT_EGO_HOST) -> list[dict[str, Any]]:
    cmd = "df -B1 -P -T -x tmpfs -x devtmpfs -x squashfs -x efivarfs"
    res = _run_ego(cmd, host=host, timeout=20)
    if res.returncode != 0:
        return [{"host": host, "status": "error", "error": res.stderr.strip() or f"exit {res.returncode}"}]
    drives: list[dict[str, Any]] = []
    for line in res.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        device, fstype, total_s, used_s, avail_s, pct_s, mount = parts[:7]
        try:
            total, used, avail = int(total_s), int(used_s), int(avail_s)
        except ValueError:
            continue
        pct = (used / total * 100.0) if total else float(pct_s.rstrip("%") or 0.0)
        drives.append({
            "host": host,
            "device": device,
            "fstype": fstype,
            "mountpoint": mount,
            "total_bytes": total,
            "used_bytes": used,
            "avail_bytes": avail,
            "total_human": _human_bytes(total),
            "used_human": _human_bytes(used),
            "avail_human": _human_bytes(avail),
            "use_percent": round(pct, 1),
            "status": "critical" if pct >= CRIT_PERCENT else ("warning" if pct >= WARN_PERCENT else "ok"),
        })
    return drives


def inventory(target: str = "all", ego_host: str = DEFAULT_EGO_HOST) -> dict[str, Any]:
    """Return drive inventory and capacity alerts for local Windows and/or ego."""
    items: list[dict[str, Any]] = []
    if target in ("all", "windows", "local"):
        items.extend(_windows_drives())
    if target in ("all", "ego"):
        items.extend(_ego_drives(host=ego_host))
    alerts = [d for d in items if d.get("status") in ("warning", "critical", "error")]
    return {"checked_at": _utc_now(), "target": target, "drives": items, "alerts": alerts}


def inspect_path(
    path: str,
    target: str = "ego",
    max_depth: int = 1,
    limit: int = 25,
    ego_host: str = DEFAULT_EGO_HOST,
) -> dict[str, Any]:
    """List top subdirectories/files by size under a given path."""
    depth = max(1, min(3, int(max_depth)))
    lim = max(1, min(100, int(limit)))
    if target == "ego":
        q = shlex.quote(path)
        res = _run_ego(
            f"du -B1 --max-depth={depth} {q} 2>/dev/null | sort -nr | head -n {lim}",
            host=ego_host,
            timeout=120,
        )
        entries = []
        for line in res.stdout.splitlines():
            if "\t" not in line:
                continue
            size_s, p = line.split("\t", 1)
            size = int(size_s)
            entries.append({"path": p, "bytes": size, "human": _human_bytes(size)})
        return {"target": target, "path": path, "entries": entries, "stderr": res.stderr.strip()}

    root = Path(path)
    if not root.exists():
        raise FileNotFoundError(f"local path does not exist: {path}")
    entries = []
    for child in root.iterdir():
        size = child.stat().st_size if child.is_file() else sum(
            f.stat().st_size for f in child.rglob("*") if f.is_file()
        )
        entries.append({"path": str(child), "bytes": size, "human": _human_bytes(size)})
    entries.sort(key=lambda e: e["bytes"], reverse=True)
    return {"target": "windows", "path": str(root), "entries": entries[:lim]}


def docker_audit(ego_host: str = DEFAULT_EGO_HOST) -> dict[str, Any]:
    """Inspect Docker disk usage on ego without modifying containers or images."""
    res = _run_ego("docker system df --format '{{json .}}'", host=ego_host, timeout=30)
    rows = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"raw": line})
    return {"host": ego_host, "checked_at": _utc_now(), "docker_df": rows, "stderr": res.stderr.strip()}


def _rsync_flags_for_fstype(fstype: str) -> list[str]:
    # NTFS/vfat cannot preserve POSIX ownership, permissions, or Linux xattrs.
    if fstype in ("ntfs", "fuseblk", "vfat", "exfat"):
        return ["-rltDv", "--modify-window=2"]
    return ["-aHAX"]


def plan_move(
    src: str,
    dst: str,
    target: str = "ego",
    safety_factor: float = 1.05,
    ego_host: str = DEFAULT_EGO_HOST,
) -> dict[str, Any]:
    """Preflight check for moving data from src to dst without modifying files."""
    if target == "ego":
        src_norm = _validate_ego_path(src)
        dst_norm = _validate_ego_path(dst)
        if src_norm == dst_norm or dst_norm.startswith(src_norm.rstrip("/") + "/"):
            raise ValueError(f"destination {dst_norm!r} cannot be inside source {src_norm!r}")
        src_bytes = _ego_path_size_bytes(src_norm, host=ego_host)
        avail_bytes = _ego_free_bytes(dst_norm, host=ego_host)
        required = int(src_bytes * safety_factor)
        fstype = _ego_fstype(dst_norm, host=ego_host)
        return {
            "target": "ego",
            "src": src_norm,
            "dst": dst_norm,
            "src_bytes": src_bytes,
            "src_human": _human_bytes(src_bytes),
            "dst_avail_bytes": avail_bytes,
            "dst_avail_human": _human_bytes(avail_bytes),
            "required_bytes": required,
            "dst_fstype": fstype or "unknown",
            "rsync_flags": _rsync_flags_for_fstype(fstype),
            "space_ok": avail_bytes >= required,
        }

    src_p = Path(src).resolve()
    dst_p = Path(dst).resolve()
    if not src_p.exists():
        raise FileNotFoundError(f"source does not exist: {src}")
    src_bytes = src_p.stat().st_size if src_p.is_file() else sum(
        f.stat().st_size for f in src_p.rglob("*") if f.is_file()
    )
    probe = dst_p if dst_p.exists() else dst_p.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    avail_bytes = shutil.disk_usage(probe).free
    required = int(src_bytes * safety_factor)
    return {
        "target": "windows",
        "src": str(src_p),
        "dst": str(dst_p),
        "src_bytes": src_bytes,
        "src_human": _human_bytes(src_bytes),
        "dst_avail_bytes": avail_bytes,
        "dst_avail_human": _human_bytes(avail_bytes),
        "required_bytes": required,
        "space_ok": avail_bytes >= required,
    }


def move_data(
    src: str,
    dst: str,
    target: str = "ego",
    dry_run: bool = True,
    delete_source: bool = False,
    symlink_source: bool = False,
    ego_host: str = DEFAULT_EGO_HOST,
) -> dict[str, Any]:
    """Copy or move data across drives with preflight, verification, and receipt."""
    plan = plan_move(src, dst, target=target, ego_host=ego_host)
    if not plan["space_ok"]:
        raise RuntimeError(
            f"insufficient space on destination {dst}: need {plan['src_human']} "
            f"(with margin {_human_bytes(plan['required_bytes'])}), have {plan['dst_avail_human']}"
        )

    if target == "ego":
        src_q = shlex.quote(plan["src"].rstrip("/") + "/")
        dst_q = shlex.quote(plan["dst"].rstrip("/") + "/")
        flags = " ".join(plan["rsync_flags"])
        dry_flag = "--dry-run --itemize-changes" if dry_run else "--stats"
        cmd = f"mkdir -p {shlex.quote(plan['dst'])} && rsync {flags} {dry_flag} {src_q} {dst_q}"
        res = _run_ego(cmd, host=ego_host, timeout=1800)
        if res.returncode != 0:
            raise RuntimeError(f"rsync failed (exit {res.returncode}): {res.stderr.strip()}")

        result: dict[str, Any] = {
            "status": "dry_run" if dry_run else "copied",
            "plan": plan,
            "command": cmd,
            "stdout_tail": "\n".join(res.stdout.splitlines()[-25:]),
        }
        if dry_run:
            return result

        dst_bytes = _ego_path_size_bytes(plan["dst"], host=ego_host)
        verified = dst_bytes >= int(plan["src_bytes"] * 0.98)
        result["dst_bytes"] = dst_bytes
        result["dst_human"] = _human_bytes(dst_bytes)
        result["verified"] = verified
        if not verified:
            raise RuntimeError(
                f"post-copy verification failed: src={plan['src_human']}, dst={_human_bytes(dst_bytes)}"
            )

        if delete_source:
            src_exact = shlex.quote(plan["src"])
            dst_exact = shlex.quote(plan["dst"])
            rm_cmd = f"rm -rf {src_exact}"
            if symlink_source:
                rm_cmd += f" && ln -s {dst_exact} {src_exact}"
            rm_res = _run_ego(rm_cmd, host=ego_host, timeout=300)
            if rm_res.returncode != 0:
                raise RuntimeError(f"source cleanup/symlink failed: {rm_res.stderr.strip()}")
            result["status"] = "moved_and_symlinked" if symlink_source else "moved"

        result["receipt_path"] = _write_receipt("move", result)
        return result

    # Local Windows move
    src_p = Path(plan["src"])
    dst_p = Path(plan["dst"])
    if dry_run:
        return {"status": "dry_run", "plan": plan}
    if src_p.is_file():
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_p, dst_p)
        verified = dst_p.stat().st_size == src_p.stat().st_size
    else:
        shutil.copytree(src_p, dst_p, dirs_exist_ok=True)
        dst_bytes = sum(f.stat().st_size for f in dst_p.rglob("*") if f.is_file())
        verified = dst_bytes >= plan["src_bytes"]
    if not verified:
        raise RuntimeError("post-copy size verification failed on Windows")
    if delete_source:
        if src_p.is_file():
            src_p.unlink()
        else:
            shutil.rmtree(src_p)
    out = {"status": "moved" if delete_source else "copied", "plan": plan, "verified": True}
    out["receipt_path"] = _write_receipt("move", out)
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_path(
    src: str,
    backup_root: str = DEFAULT_BACKUP_ROOT_EGO,
    target: str = "ego",
    mode: str = "rsync",
    label: str = "",
    dry_run: bool = True,
    keep: int = 5,
    ego_host: str = DEFAULT_EGO_HOST,
) -> dict[str, Any]:
    """Create a verified backup (sqlite, rsync, or tar) under backup_root."""
    if mode not in ("sqlite", "rsync", "tar"):
        raise ValueError(f"unsupported backup mode: {mode!r}")
    tag = f"{_ts_slug()}_{label}" if label else _ts_slug()

    if target == "ego":
        src_norm = _validate_ego_path(src)
        root_norm = _validate_ego_path(backup_root)
        name = Path(src_norm).name or "root"
        dest_dir = f"{root_norm.rstrip('/')}/{name}/{tag}"
        plan = plan_move(src_norm, dest_dir, target="ego", ego_host=ego_host)
        if not plan["space_ok"]:
            raise RuntimeError(f"insufficient backup space on {root_norm}: need {plan['src_human']}")
        if dry_run:
            return {"status": "dry_run", "mode": mode, "dest": dest_dir, "plan": plan}

        if mode == "sqlite":
            out_file = f"{dest_dir}/{Path(src_norm).name}"
            py_code = (
                "import hashlib, pathlib, sqlite3; "
                f"s=sqlite3.connect({src_norm!r}); d=sqlite3.connect({out_file!r}); "
                "s.backup(d); d.close(); s.close(); "
                f"h=hashlib.sha256(pathlib.Path({out_file!r}).read_bytes()).hexdigest(); print(h)"
            )
            cmd = f"mkdir -p {shlex.quote(dest_dir)} && python3 -c {shlex.quote(py_code)}"
            res = _run_ego(cmd, host=ego_host, timeout=600)
            if res.returncode != 0:
                raise RuntimeError(f"sqlite backup failed: {res.stderr.strip()}")
            sha = res.stdout.strip().splitlines()[-1]
            out = {"status": "backed_up", "mode": "sqlite", "dest": out_file, "sha256": sha, "plan": plan}
        elif mode == "tar":
            archive = f"{dest_dir}.tar.gz"
            parent = shlex.quote(str(Path(src_norm).parent))
            base = shlex.quote(Path(src_norm).name)
            cmd = (
                f"mkdir -p {shlex.quote(str(Path(dest_dir).parent))} && "
                f"tar -czf {shlex.quote(archive)} -C {parent} {base} && "
                f"sha256sum {shlex.quote(archive)}"
            )
            res = _run_ego(cmd, host=ego_host, timeout=1800)
            if res.returncode != 0:
                raise RuntimeError(f"tar backup failed: {res.stderr.strip()}")
            sha = res.stdout.strip().split()[0]
            out = {"status": "backed_up", "mode": "tar", "dest": archive, "sha256": sha, "plan": plan}
        else:
            out = move_data(src_norm, dest_dir, target="ego", dry_run=False, delete_source=False, ego_host=ego_host)
            out["mode"] = "rsync"

        # Prune old snapshots beyond `keep` only after new backup succeeds.
        if keep > 0:
            snap_parent = shlex.quote(f"{root_norm.rstrip('/')}/{name}")
            _run_ego(
                f"ls -1dt {snap_parent}/* 2>/dev/null | tail -n +{keep + 1} | xargs -r rm -rf",
                host=ego_host,
                timeout=120,
            )
        out["receipt_path"] = _write_receipt("backup", out)
        return out

    # Local Windows backup
    src_p = Path(src).resolve()
    dest_base = Path(backup_root).resolve() / src_p.name
    dest_dir = dest_base / tag
    plan = plan_move(str(src_p), str(dest_dir), target="windows")
    if not plan["space_ok"]:
        raise RuntimeError(f"insufficient backup space on {backup_root}")
    if dry_run:
        return {"status": "dry_run", "mode": mode, "dest": str(dest_dir), "plan": plan}

    dest_dir.mkdir(parents=True, exist_ok=True)
    if mode == "sqlite":
        out_file = dest_dir / src_p.name
        with sqlite3.connect(str(src_p)) as s, sqlite3.connect(str(out_file)) as d:
            s.backup(d)
        out = {"status": "backed_up", "mode": "sqlite", "dest": str(out_file), "sha256": _sha256_file(out_file)}
    elif mode == "tar":
        archive = shutil.make_archive(str(dest_dir), "gztar", root_dir=str(src_p.parent), base_dir=src_p.name)
        out = {"status": "backed_up", "mode": "tar", "dest": archive, "sha256": _sha256_file(Path(archive))}
    else:
        out = move_data(str(src_p), str(dest_dir), target="windows", dry_run=False, delete_source=False)
        out["mode"] = "rsync"

    if keep > 0 and dest_base.exists():
        snaps = sorted(dest_base.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in snaps[keep:]:
            if old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink(missing_ok=True)
    out["receipt_path"] = _write_receipt("backup", out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sabrina cross-drive storage & backup manager")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inv = sub.add_parser("inventory", help="Show drive capacity across Windows and ego")
    p_inv.add_argument("--target", choices=("all", "ego", "windows"), default="all")

    p_insp = sub.add_parser("inspect", help="Show largest entries under a path")
    p_insp.add_argument("path")
    p_insp.add_argument("--target", choices=("ego", "windows"), default="ego")
    p_insp.add_argument("--depth", type=int, default=1)

    sub.add_parser("docker-audit", help="Inspect Docker disk usage on ego")

    p_mv = sub.add_parser("move", help="Preflight or execute a cross-drive move")
    p_mv.add_argument("src")
    p_mv.add_argument("dst")
    p_mv.add_argument("--target", choices=("ego", "windows"), default="ego")
    p_mv.add_argument("--execute", action="store_true", help="Actually copy/move (default is dry-run)")
    p_mv.add_argument("--delete-source", action="store_true", help="Remove source after verified copy")
    p_mv.add_argument("--symlink-source", action="store_true", help="Replace removed source with symlink to dst")

    p_bk = sub.add_parser("backup", help="Create a verified backup")
    p_bk.add_argument("src")
    p_bk.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT_EGO)
    p_bk.add_argument("--target", choices=("ego", "windows"), default="ego")
    p_bk.add_argument("--mode", choices=("rsync", "sqlite", "tar"), default="rsync")
    p_bk.add_argument("--label", default="")
    p_bk.add_argument("--keep", type=int, default=5)
    p_bk.add_argument("--execute", action="store_true", help="Actually run backup (default is dry-run)")

    args = parser.parse_args(argv)
    if args.cmd == "inventory":
        out = inventory(target=args.target)
    elif args.cmd == "inspect":
        out = inspect_path(args.path, target=args.target, max_depth=args.depth)
    elif args.cmd == "docker-audit":
        out = docker_audit()
    elif args.cmd == "move":
        out = move_data(
            args.src,
            args.dst,
            target=args.target,
            dry_run=not args.execute,
            delete_source=args.delete_source,
            symlink_source=args.symlink_source,
        )
    else:
        out = backup_path(
            args.src,
            backup_root=args.backup_root,
            target=args.target,
            mode=args.mode,
            label=args.label,
            dry_run=not args.execute,
            keep=args.keep,
        )
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
