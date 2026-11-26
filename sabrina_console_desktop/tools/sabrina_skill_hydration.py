"""Autonomous skill hydration and web search fallback engine for Sabrina.

When Sabrina encounters blockers or failures, instead of giving up, she searches
external documentation and web engines to hydrate new ideas, drafts/modifies
skills into standard SKILL.md format, and submits proposals to the Operator Console
for supervisor approval.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
PROPOSED_SKILLS_DIR = ROOT / "skills" / "proposed"
APPROVED_SKILLS_DIR = ROOT / "skills"


def ensure_skill_proposals_schema(db: sqlite3.Connection) -> None:
    """Ensure the assistant_skill_proposals table exists."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_skill_proposals (
            proposal_id TEXT PRIMARY KEY,
            run_id TEXT,
            skill_name TEXT NOT NULL,
            description TEXT NOT NULL,
            trigger_reason TEXT NOT NULL,
            query_used TEXT NOT NULL,
            search_sources TEXT NOT NULL,
            skill_content TEXT NOT NULL,
            target_path TEXT NOT NULL,
            status TEXT NOT NULL,
            reviewer TEXT,
            review_feedback TEXT,
            created REAL NOT NULL,
            updated REAL NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_proposals_status ON assistant_skill_proposals(status)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_skill_proposals_run_id ON assistant_skill_proposals(run_id)"
    )
    db.commit()


def search_engine_query(
    query: str,
    *,
    engine: str = "duckduckgo",
    timeout: float = 6.0,
    custom_search_fn: Optional[Callable[[str], List[Dict[str, str]]]] = None,
) -> List[Dict[str, str]]:
    """Query external search engine for documentation, APIs, and techniques.

    Returns a list of dicts: [{"title": ..., "url": ..., "snippet": ...}].
    """
    if custom_search_fn is not None:
        return custom_search_fn(query)
    if engine != "duckduckgo":
        raise ValueError(f"search engine {engine!r} has no configured live adapter")

    encoded = urllib.parse.quote_plus(query)
    results: List[Dict[str, str]] = []

    # Attempt light DuckDuckGo / web query if network permits
    try:
        url = f"https://html.duckduckgo.com/html/?q={encoded}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SabrinaHydrationEngine/1.0"
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # Basic HTML extraction for result snippets
        pattern = re.compile(
            r'<a class="result__snippet[^>]*href="(?P<url>[^"]+)"[^>]*>(?P<snippet>.*?)</a>',
            re.DOTALL | re.IGNORECASE,
        )
        matches = list(pattern.finditer(html))
        for m in matches[:5]:
            snippet_raw = re.sub(r"<[^>]+>", "", m.group("snippet")).strip()
            results.append({
                "title": f"Result for: {query[:40]}",
                "url": m.group("url"),
                "snippet": snippet_raw,
            })
    except Exception:
        # An unavailable search is not source evidence for a proposed skill.
        return []

    return results


def synthesize_skill_content(
    skill_name: str,
    description: str,
    task_context: str,
    search_results: List[Dict[str, str]],
    trigger_reason: str = "failure_recovery",
) -> str:
    """Format search findings and context into a standard SKILL.md document."""
    clean_name = re.sub(r"[^a-zA-Z0-9_\-]", "-", skill_name.strip().lower()).strip("-")
    if not clean_name:
        clean_name = "hydrated-skill"

    sources_md = "\n".join(
        f"- [{r.get('title', 'Source')}]({r.get('url', '#')}): {r.get('snippet', '')}"
        for r in search_results
    )

    content = f"""---
name: {clean_name}
description: {description.strip()}
---

# {clean_name.replace('-', ' ').title()}

## Overview
{description.strip()}

Hydrated autonomously by Sabrina via web search synthesis following {trigger_reason}.
Task context: {task_context.strip() if task_context else 'General procedural guidance'}

## External Sources & References
{sources_md}

## When to Use
- Trigger when encountering similar failure states or requiring specialized workflow patterns.
- Apply narrow scopes, verify file paths before write operations, and check preconditions.

## Step-by-Step Procedure
1. **Precondition Check**: Inspect target application state, process identity, and active lease.
2. **Execution**: Execute the targeted script or desktop action with explicit arguments.
3. **Observation & Verification**: Capture fresh post-action output/snapshot. Do not rely on unverified claims.
4. **Recovery on Failure**: If the expected outcome is not confirmed, halt and report uncertain state rather than looping.

## Verification & Acceptance
- Validate exit code is 0 and output contains expected evidence.
- Ensure all artifacts are recorded in the run audit.
"""
    return content


class SabrinaSkillHydration:
    """Manages skill hydration, search fallback, and proposal review cycle."""

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        skills_dir: Optional[Path | str] = None,
        proposed_dir: Optional[Path | str] = None,
        search_fn: Optional[Callable[[str], List[Dict[str, str]]]] = None,
    ):
        self.db_path = Path(db_path or DEFAULT_DB_PATH)
        self.skills_dir = Path(skills_dir or APPROVED_SKILLS_DIR)
        self.proposed_dir = Path(proposed_dir or PROPOSED_SKILLS_DIR)
        self.search_fn = search_fn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.proposed_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as db:
            ensure_skill_proposals_schema(db)

    def hydrate_skill_from_search(
        self,
        query: str,
        skill_name: str,
        description: str,
        task_context: str = "",
        trigger_reason: str = "search_hydration",
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search documentation/web, synthesize a candidate skill, and propose it."""
        results = search_engine_query(query, custom_search_fn=self.search_fn)
        if not results:
            raise RuntimeError("search_unavailable_no_source_evidence")
        clean_name = re.sub(r"[^a-zA-Z0-9_\-]", "-", skill_name.strip().lower()).strip("-")
        content = synthesize_skill_content(
            clean_name, description, task_context, results, trigger_reason=trigger_reason
        )

        proposal_id = f"prop_{clean_name}_{int(time.time())}"
        target_path = str(self.skills_dir / clean_name / "SKILL.md")
        proposed_file = self.proposed_dir / clean_name / "SKILL.md"
        proposed_file.parent.mkdir(parents=True, exist_ok=True)
        proposed_file.write_text(content, encoding="utf-8")

        now = time.time()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT OR REPLACE INTO assistant_skill_proposals (
                    proposal_id, run_id, skill_name, description, trigger_reason,
                    query_used, search_sources, skill_content, target_path,
                    status, reviewer, review_feedback, created, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    run_id,
                    clean_name,
                    description,
                    trigger_reason,
                    query,
                    json.dumps(results),
                    content,
                    target_path,
                    "pending_approval",
                    None,
                    None,
                    now,
                    now,
                ),
            )
            db.commit()

        return {
            "proposal_id": proposal_id,
            "skill_name": clean_name,
            "status": "pending_approval",
            "proposed_path": str(proposed_file),
            "target_path": target_path,
            "sources_count": len(results),
            "content": content,
        }

    def on_failure_trigger_hydration(
        self,
        error_message: str,
        task: str,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Autonomous fallback hook: generate and propose a skill when blocked or failed."""
        # Derive search query and candidate skill name from error & task
        norm_err = re.sub(r"[^a-zA-Z0-9\s]", " ", error_message).strip()
        first_err_phrase = norm_err.split("\n")[0][:80]
        query = f"fix {first_err_phrase} documentation best practices"
        skill_name = f"recover-{re.sub(r'[^a-zA-Z0-9]', '-', first_err_phrase[:30]).strip('-').lower()}"
        description = (
            f"Autonomous recovery skill generated after failure: {error_message[:120]}. "
            f"Guides resolution and prevents repeated errors in task: {task[:100]}."
        )

        return self.hydrate_skill_from_search(
            query=query,
            skill_name=skill_name,
            description=description,
            task_context=f"Task: {task}\nEncountered Error: {error_message}",
            trigger_reason="failure_recovery",
            run_id=run_id,
        )

    def list_proposals(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List skill proposals, optionally filtered by status."""
        query = "SELECT proposal_id, run_id, skill_name, description, trigger_reason, query_used, search_sources, skill_content, target_path, status, reviewer, review_feedback, created, updated FROM assistant_skill_proposals"
        params: List[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created DESC"

        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(query, params).fetchall()
            return [self._enrich_proposal(dict(r)) for r in rows]

    def _enrich_proposal(self, d: Dict[str, Any]) -> Dict[str, Any]:
        """Compute exact SHA-256 and version from skill content."""
        content = d.get("skill_content", "")
        d["sha256"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
        m = re.search(r"(?m)^version:\s*['\"]?([0-9A-Za-z_.-]+)['\"]?", content)
        d["version"] = m.group(1).strip() if m else "1.0.0"
        return d

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a specific proposal by ID."""
        with sqlite3.connect(self.db_path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM assistant_skill_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            return self._enrich_proposal(dict(row)) if row else None

    def create_proposal(
        self,
        skill_name: str,
        skill_content: str,
        trigger_reason: str = "manual_proposal",
        query_used: str = "",
        target_path: Optional[str] = None,
        run_id: Optional[str] = None,
        search_sources: str = "[]",
    ) -> str:
        """Create and store a reviewable skill proposal in assistant_skill_proposals."""
        proposal_id = f"prop_adapt_{skill_name}_{int(time.time()*1000)}"
        now = time.time()
        target = target_path or f"skills/{skill_name}/SKILL.md"
        with sqlite3.connect(self.db_path) as db:
            ensure_skill_proposals_schema(db)
            db.execute(
                """
                INSERT OR REPLACE INTO assistant_skill_proposals (
                    proposal_id, run_id, skill_name, description, trigger_reason,
                    query_used, search_sources, skill_content, target_path, status,
                    reviewer, review_feedback, created, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    run_id,
                    skill_name,
                    f"Proposal for {skill_name}",
                    trigger_reason,
                    query_used,
                    search_sources,
                    skill_content,
                    target,
                    "pending_approval",
                    "",
                    "",
                    now,
                    now,
                ),
            )
            db.commit()
        return proposal_id

    def approve_proposal(
        self,
        proposal_id: str,
        expected_version: str,
        expected_sha256: str,
        reviewer: str = "operator",
        feedback: str = "Approved by operator",
    ) -> Dict[str, Any]:
        """Approve a proposal and install the skill into Sabrina's active skills directory.

        Strictly enforces exact version and full 64-character SHA-256 verification.
        Rejects missing, empty, whitespace, truncated, or mismatched versions/hashes.
        Preserves any existing installed skill version to an archive before overwrite.
        """
        prop = self.get_proposal(proposal_id)
        if not prop:
            raise ValueError(f"Proposal {proposal_id} not found")

        # 1. Validate mandatory expected_version
        if not expected_version or not str(expected_version).strip():
            raise ValueError("Approval rejected: expected_version is mandatory and cannot be empty.")
        expected_version_str = str(expected_version).strip()

        # 2. Validate mandatory expected_sha256 (must be full 64 hex characters)
        if not expected_sha256 or not str(expected_sha256).strip():
            raise ValueError("Approval rejected: expected_sha256 is mandatory and cannot be empty.")
        norm_expected = str(expected_sha256).strip().lower()
        if len(norm_expected) != 64 or not re.fullmatch(r"[0-9a-f]{64}", norm_expected):
            raise ValueError(
                f"Approval rejected: expected_sha256 must be a full 64-character hex SHA-256 hash. "
                f"Received: {expected_sha256!r} (length={len(norm_expected)})."
            )

        content = prop["skill_content"]
        actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest().lower()

        # Extract version from YAML frontmatter if present
        actual_version = "1.0.0"
        m = re.search(r"(?m)^version:\s*['\"]?([0-9A-Za-z_.-]+)['\"]?", content)
        if m:
            actual_version = m.group(1).strip()

        # Exact version equality
        if expected_version_str != actual_version:
            raise ValueError(
                f"Version mismatch: Operator approved version {expected_version_str!r}, "
                f"but proposal {proposal_id} specifies version {actual_version!r}."
            )

        # Exact SHA-256 equality (no prefixes, no truncated matches)
        if norm_expected != actual_sha256:
            raise ValueError(
                f"Integrity check failed: Operator approved content SHA-256 {norm_expected!r}, "
                f"but proposal {proposal_id} content SHA-256 is {actual_sha256!r}."
            )

        skill_name = prop["skill_name"]
        target = self.skills_dir / skill_name / "SKILL.md"
        now = time.time()

        # 3. Preserve any replaced skill version before overwriting
        prior_version_archived: Optional[str] = None
        prior_archive_path: Optional[str] = None
        if target.exists():
            prior_content = target.read_text(encoding="utf-8", errors="replace")
            m_prior = re.search(r"(?m)^version:\s*['\"]?([0-9A-Za-z_.-]+)['\"]?", prior_content)
            prior_ver = m_prior.group(1).strip() if m_prior else "prior"
            prior_sha = hashlib.sha256(prior_content.encode("utf-8")).hexdigest()

            archive_dir = self.skills_dir / ".archive" / skill_name
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_filename = f"SKILL.v{prior_ver}.{int(now)}.{prior_sha[:8]}.md"
            archive_path = archive_dir / archive_filename
            archive_path.write_text(prior_content, encoding="utf-8")
            prior_version_archived = prior_ver
            prior_archive_path = str(archive_path)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                UPDATE assistant_skill_proposals
                SET status = 'approved', reviewer = ?, review_feedback = ?, updated = ?
                WHERE proposal_id = ?
                """,
                (reviewer, feedback, now, proposal_id),
            )
            db.commit()

        return {
            "proposal_id": proposal_id,
            "skill_name": skill_name,
            "version": actual_version,
            "sha256": actual_sha256,
            "status": "approved",
            "installed_path": str(target),
            "archived_prior_version": prior_version_archived,
            "archived_prior_path": prior_archive_path,
            "reviewer": reviewer,
            "feedback": feedback,
        }

    def reject_proposal(
        self,
        proposal_id: str,
        reason: str = "Rejected by operator",
        reviewer: str = "operator",
    ) -> Dict[str, Any]:
        """Reject a skill proposal."""
        prop = self.get_proposal(proposal_id)
        if not prop:
            raise ValueError(f"Proposal {proposal_id} not found")

        now = time.time()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                UPDATE assistant_skill_proposals
                SET status = 'rejected', reviewer = ?, review_feedback = ?, updated = ?
                WHERE proposal_id = ?
                """,
                (reviewer, reason, now, proposal_id),
            )
            db.commit()

        return {
            "proposal_id": proposal_id,
            "skill_name": prop["skill_name"],
            "status": "rejected",
            "reason": reason,
            "reviewer": reviewer,
        }
