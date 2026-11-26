"""Fail-closed authorization for Sabrina's secure-code review workspaces.

This is intentionally separate from URL engagement scope. A URL authorization
does not grant Sabrina authority to inspect a local checkout, and a local
checkout does not grant authority to test a deployed service.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ALLOWED_KINDS = {"owned", "training", "explicit_permission"}


@dataclass(frozen=True)
class RepositoryAuthorization:
    """Operator-supplied authority for one or more local repository roots."""

    authorization_id: str
    kind: str
    roots: tuple[Path, ...]
    evidence: str

    @classmethod
    def from_mapping(cls, record: Mapping[str, Any]) -> "RepositoryAuthorization":
        authorization_id = str(record.get("authorization_id", "")).strip()
        kind = str(record.get("kind", "")).strip().lower()
        evidence = str(record.get("evidence", "")).strip()
        raw_roots = record.get("roots")
        if not authorization_id:
            raise ValueError("repository authorization requires a non-empty authorization_id")
        if kind not in ALLOWED_KINDS:
            raise ValueError("repository authorization kind must be owned, training, or explicit_permission")
        if not evidence:
            raise ValueError("repository authorization requires operator evidence or a permission reference")
        if not isinstance(raw_roots, list) or not raw_roots:
            raise ValueError("repository authorization requires at least one absolute root")
        roots: list[Path] = []
        for raw_root in raw_roots:
            root = Path(str(raw_root))
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("each authorized repository root must be an existing absolute directory")
            roots.append(root.resolve())
        return cls(authorization_id, kind, tuple(roots), evidence)

    def check_root(self, root: str | Path) -> tuple[bool, str]:
        candidate = Path(root)
        if not candidate.is_absolute() or not candidate.is_dir():
            return False, "repository root must be an existing absolute directory"
        resolved = candidate.resolve()
        if resolved not in self.roots:
            return False, "repository root is not listed in the operator authorization"
        return True, ""

    def check_path(self, path: str | Path) -> tuple[bool, str]:
        """Allow only a file or directory contained in an authorized root."""
        candidate = Path(path)
        if not candidate.is_absolute():
            return False, "repository path must be absolute before authorization"
        resolved = candidate.resolve()
        if any(resolved == root or root in resolved.parents for root in self.roots):
            return True, ""
        return False, "repository path is outside the operator-authorized roots"
