"""Trusted manifest snapshots, read-only/editable/protected file guards, and combined-revision integration.

These checks enforce scope and provenance on candidate artifacts; they complement isolated verification.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def safe_name(name: str) -> str:
    if not isinstance(name, str) or "\\" in name or ":" in name:
        raise ValueError("Invalid manifest path")
    p = PurePosixPath(name)
    if p.is_absolute() or not p.parts or any(x in ("..", ".") for x in name.split("/")):
        raise ValueError("Manifest path escapes workspace")
    denied = {".git", ".ssh", ".codex", ".gemini", "data", "logs", "__pycache__", "node_modules"}
    if any(x.lower() in denied for x in p.parts):
        raise ValueError("Sensitive or runtime directory excluded")
    if re.search(r"(^\.|credential|secret|token|cookie|session|auth)", p.name, re.I):
        raise ValueError("Sensitive file name excluded")
    allowed_exact_names = {"Makefile", "CMakeLists.txt", "GNUmakefile"}
    allowed_suffixes = {
        ".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".ini", ".js", ".ts", ".tsx", ".html", ".css",
        ".c", ".cc", ".cpp", ".cxx", ".cu", ".cuh", ".h", ".hpp", ".rs", ".sh", ".patch", ".cmake",
    }
    if p.name not in allowed_exact_names and p.suffix.lower() not in allowed_suffixes:
        raise ValueError("File type not approved for source manifest")
    return p.as_posix()


def checked_file(root: Path | str, name: str) -> Path:
    root = Path(root).resolve(strict=True)
    name = safe_name(name)
    path = root / name
    cursor = root
    for part in PurePosixPath(name).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Symlinks are not accepted")
    if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
        raise ValueError("Invalid source file")
    if path.stat().st_nlink > 1:
        raise ValueError("Hard-linked files are not accepted")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Source file exceeds one MiB")
    return path


def read_manifest(root: Path | str, names: list[str] | tuple[str, ...] | set[str]) -> dict[str, bytes]:
    name_list = list(names)
    if not name_list or len(name_list) > 500 or len(set(name_list)) != len(name_list):
        raise ValueError("Invalid manifest size or duplicate names")
    files = {safe_name(n): checked_file(root, n).read_bytes() for n in name_list}
    if sum(map(len, files.values())) > 16 * 1024 * 1024:
        raise ValueError("Manifest exceeds sixteen MiB")
    return files


def manifest_for(files: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    return {
        name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        for name, raw in sorted(files.items())
    }


def prepare_workspace(
    source: Path | str,
    workspace: Path | str,
    names: list[str],
    expected_source_hash: str,
) -> dict[str, dict[str, Any]]:
    """Explicit maintainer-reviewed manifest only; no recursive repository copy."""
    files = read_manifest(source, names)
    manifest = manifest_for(files)
    if digest(manifest) != expected_source_hash:
        raise ValueError("Frozen source changed")
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    for name, raw in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    if manifest_for(read_manifest(source, names)) != manifest:
        raise ValueError("Source changed during copy; workspace cannot be assigned")
    return manifest


class ArtifactCollector:
    """Captures immutable candidate artifacts and enforces editable/read-only/protected file boundaries."""

    def __init__(self, store: Path | str):
        self.store = Path(store).resolve()
        self.store.mkdir(parents=True, exist_ok=True)

    def __call__(
        self,
        workspace: Path | str,
        allowed_files: list[str],
        *,
        baseline_manifest: dict[str, dict[str, Any]] | None = None,
        editable_files: list[str] | None = None,
        readonly_files: list[str] | None = None,
        protected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        workspace = Path(workspace).resolve(strict=True)
        if self.store.is_relative_to(workspace) or workspace.is_relative_to(self.store):
            raise ValueError("Artifact store must be outside editable workspace")

        expected = {safe_name(f) for f in allowed_files}
        actual: set[str] = set()
        for path in workspace.rglob("*"):
            if path.is_symlink():
                raise ValueError("Symlink artifact rejected")
            if path.is_file():
                rel = path.relative_to(workspace).as_posix()
                actual.add(rel)

        protected_set = {safe_name(f) for f in (protected_files or [])}
        readonly_set = {safe_name(f) for f in (readonly_files or [])}
        editable_set = {safe_name(f) for f in (editable_files if editable_files is not None else allowed_files)}

        # Check for unauthorized extra or missing files
        if actual != expected:
            extra = actual - expected
            if extra & protected_set or any(p.startswith("tests/") for p in extra):
                raise ValueError("Protected acceptance test modification rejected")
            raise ValueError("Changed file manifest outside contract or missing required files")

        files = read_manifest(workspace, list(expected))
        manifest = manifest_for(files)

        changed_files: list[str] = []
        if baseline_manifest is not None:
            for fname, meta in manifest.items():
                base_meta = baseline_manifest.get(fname)
                if not base_meta or base_meta.get("sha256") != meta["sha256"]:
                    changed_files.append(fname)

            # Reject modifications to protected acceptance tests
            touched_protected = [
                f for f in changed_files
                if f in protected_set or (f not in editable_set and f.startswith("tests/"))
            ]
            if touched_protected:
                raise ValueError(
                    f"Protected acceptance test modification rejected: {sorted(touched_protected)}"
                )

            # Reject modifications to read-only context files or files outside editable_set
            touched_readonly = [
                f for f in changed_files
                if f in readonly_set or f not in editable_set
            ]
            if touched_readonly:
                raise ValueError(
                    f"Unauthorized modification to read-only or non-editable file: {sorted(touched_readonly)}"
                )

        revision = digest(manifest)
        destination = self.store / revision
        if not destination.exists():
            destination.mkdir()
            for name, raw in files.items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
                try:
                    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                except OSError:
                    pass
        result = {
            "artifact_hash": revision,
            "manifest": manifest,
            "changed_files": sorted(changed_files),
            "artifact_path": str(destination),
        }
        self.validate(result)
        if manifest_for(read_manifest(workspace, list(expected))) != manifest:
            raise ValueError("Workspace changed during capture")
        return result

    def validate(self, revision: dict[str, Any]) -> bool:
        if not isinstance(revision, dict) or not re.fullmatch("[0-9a-f]{64}", str(revision.get("artifact_hash", ""))):
            raise ValueError("Invalid revision hash")
        location = Path(revision["artifact_path"])
        if location != self.store / revision["artifact_hash"] or location.is_symlink():
            raise ValueError("Foreign artifact revision")
        actual: set[str] = set()
        for path in location.rglob("*"):
            if path.is_symlink():
                raise ValueError("Symlink artifact rejected")
            if path.is_file():
                actual.add(path.relative_to(location).as_posix())
        if actual != set(revision["manifest"]):
            raise ValueError("Captured manifest changed")
        files = read_manifest(location, list(revision["manifest"]))
        if manifest_for(files) != revision["manifest"] or digest(revision["manifest"]) != revision["artifact_hash"]:
            raise ValueError("Captured artifact hash mismatch")
        return True

    def integrate_revisions(
        self,
        baseline_files: dict[str, bytes],
        revisions: list[dict[str, Any]],
        *,
        protected_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """Combine multiple verified task revisions into a single immutable combined revision.

        Rejects conflicting edits to the same file across different revisions and
        rejects any modification to protected acceptance test files.
        """
        baseline_manifest = manifest_for(baseline_files)
        combined = dict(baseline_files)
        protected_set = {safe_name(f) for f in (protected_files or [])}
        file_owners: dict[str, str] = {}
        all_changed: set[str] = set()

        for rev in revisions:
            self.validate(rev)
            rev_hash = rev["artifact_hash"]
            rev_loc = Path(rev["artifact_path"])
            rev_files = read_manifest(rev_loc, list(rev["manifest"]))
            for fname, raw in rev_files.items():
                f_sha = hashlib.sha256(raw).hexdigest()
                base_sha = baseline_manifest.get(fname, {}).get("sha256")
                if f_sha != base_sha:
                    if fname in protected_set:
                        raise ValueError(f"Protected acceptance test modification rejected in {fname}")
                    if fname in file_owners and hashlib.sha256(combined[fname]).hexdigest() != f_sha:
                        raise ValueError(
                            f"Conflicting edits to {fname} between revisions {file_owners[fname]} and {rev_hash}"
                        )
                    file_owners[fname] = rev_hash
                    combined[fname] = raw
                    all_changed.add(fname)

        combined_manifest = manifest_for(combined)
        combined_hash = digest(combined_manifest)
        destination = self.store / combined_hash
        if not destination.exists():
            destination.mkdir()
            for name, raw in combined.items():
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
                try:
                    target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                except OSError:
                    pass
        result = {
            "artifact_hash": combined_hash,
            "manifest": combined_manifest,
            "changed_files": sorted(all_changed),
            "source_revisions": [r["artifact_hash"] for r in revisions],
            "artifact_path": str(destination),
        }
        self.validate(result)
        return result
