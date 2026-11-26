"""CodeAgent patch applier for candidate adapter patches.

Operates outside Sabrina's main loop to apply candidate adapter patches after
Operator review. Strictly enforces protected path boundaries to keep orchestration
core files safe from automated modification.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import py_compile
import sqlite3
import time
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATCH_DIR = ROOT / "patches"
DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"

PROTECTED_CORE_FILENAMES = {
    "sabrina_gemini_loop.py",
    "agent_runtime.py",
    "sabrina_supervisor.py",
    "system_checkpoint.py",
    "pod_control.py",
    "agent_runtime.json",
    "run_improvement_session.py",
    "improvement_tracker.py",
    "token_utils.py",
    "oracle_providers.py",
    "blackboard.py",
    "rag_knowledge.py",
    "lab_knowledge.py",
}


def check_is_protected(target_path: Path | str) -> bool:
    """Check if target path belongs to protected orchestration core files."""
    p = Path(target_path).resolve()
    # Check exact filename
    if p.name.lower() in PROTECTED_CORE_FILENAMES:
        return True

    # Check directory prefix (e.g. config/engagements)
    try:
        rel = p.relative_to(ROOT).as_posix().lower()
        if rel.startswith("config/engagements"):
            return True
    except Exception:
        pass

    try:
        from sabrina_gemini_loop import is_protected_path
        return is_protected_path(str(target_path))
    except Exception:
        return False


class CodeAgentPatchApplier:
    """Applies reviewed candidate adapter patches to non-protected locations."""

    def __init__(self, patch_dir: Optional[Path | str] = None, db_path: Optional[Path | str] = None):
        self.patch_dir = Path(patch_dir or DEFAULT_PATCH_DIR)
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.patch_dir.mkdir(parents=True, exist_ok=True)

    def is_proposal_approved(self, proposal_id: str) -> bool:
        """Check if proposal is marked approved in assistant_skill_proposals."""
        if not self.db_path.exists() or not proposal_id:
            return False
        try:
            with sqlite3.connect(self.db_path) as db:
                row = db.execute(
                    "SELECT status FROM assistant_skill_proposals WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                return bool(row and row[0] == "approved")
        except Exception:
            return False

    def apply_patch(
        self,
        patch_path: Path | str,
        approved: bool = False,
        approver: str = "operator",
    ) -> Dict[str, Any]:
        """Apply candidate patch bundle if approved and target path is safe."""
        patch_file = Path(patch_path)
        if not patch_file.exists():
            return {"applied": False, "error": f"Patch bundle not found: {patch_file}"}

        try:
            data = json.loads(patch_file.read_text(encoding="utf-8"))
        except Exception as e:
            return {"applied": False, "error": f"Failed to parse patch bundle: {e}"}

        target_rel = data.get("target_file", "")
        if not target_rel:
            return {"applied": False, "error": "Patch bundle missing target_file."}

        target_file = (ROOT / target_rel).resolve()

        # Enforce protected paths invariant FIRST
        if check_is_protected(target_file):
            return {
                "applied": False,
                "error": f"ProtectedPathViolation: Target path '{target_rel}' is part of Sabrina's protected orchestration core.",
            }

        proposal_id = data.get("proposal_id", "")
        # Check approval
        if not approved and not self.is_proposal_approved(proposal_id):
            return {
                "applied": False,
                "error": f"Patch application blocked: proposal '{proposal_id}' has not been approved by Operator.",
            }

        code_content = data.get("code", "")
        if not code_content.strip():
            return {"applied": False, "error": "Patch bundle contains empty code."}

        # Verify hash if provided
        expected_sha = data.get("sha256")
        actual_sha = hashlib.sha256(code_content.encode("utf-8")).hexdigest()
        if expected_sha and expected_sha != actual_sha:
            return {
                "applied": False,
                "error": f"Patch hash mismatch: expected {expected_sha}, got {actual_sha}",
            }

        # Write to target path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        backup_content = target_file.read_text(encoding="utf-8") if target_file.exists() else None

        try:
            target_file.write_text(code_content, encoding="utf-8")
            # Rigorous syntax validation
            py_compile.compile(str(target_file), doraise=True)
        except Exception as err:
            # Rollback
            if backup_content is not None:
                target_file.write_text(backup_content, encoding="utf-8")
            elif target_file.exists():
                target_file.unlink()
            return {"applied": False, "error": f"Syntax validation failed on applied patch: {err}"}

        # Update patch bundle state
        data["status"] = "applied"
        data["applied_at"] = time.time()
        data["applied_by"] = approver
        patch_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

        return {
            "applied": True,
            "target_file": str(target_file),
            "sha256": actual_sha,
            "status": "applied",
            "approver": approver,
        }
