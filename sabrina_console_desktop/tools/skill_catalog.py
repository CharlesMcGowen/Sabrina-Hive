"""Skill Catalog and Dynamic Discovery for Sabrina.

Discovers, parses, and provides on-demand access to editable SKILL.md workflows.
Supports:
- discover_skills: Lists available skills with descriptions and readiness.
- read_skill: Loads full operational procedures, criteria, and requirements.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional
import yaml

from tools.tool_registry import TOOL_REGISTRY

logger = logging.getLogger("sabrina.skill_catalog")
ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"


def _is_tool_supported(tool_name: str) -> bool:
    try:
        from tools.tool_inventory import is_tool_available
        return is_tool_available(tool_name)
    except Exception:
        return TOOL_REGISTRY.has_tool(tool_name)


class SkillCatalog:
    """Discovers, caches, and serves Sabrina SKILL.md workflow documents."""

    def __init__(self, skills_dir: Optional[Path] = None) -> None:
        self.skills_dir = skills_dir or SKILLS_DIR
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_mtimes: Dict[str, float] = {}

    def _scan(self) -> None:
        """Scan skills directory and parse frontmatter."""
        if not self.skills_dir.exists():
            return

        for item in sorted(self.skills_dir.iterdir()):
            if not item.is_dir() or item.name.startswith((".", "_")) or item.name == "proposed":
                continue

            skill_file = item / "SKILL.md"
            if not skill_file.exists():
                continue

            mtime = skill_file.stat().st_mtime
            if item.name in self._cache and self._cache_mtimes.get(item.name) == mtime:
                continue

            try:
                raw_text = skill_file.read_text(encoding="utf-8", errors="replace")
                meta = {}
                body = raw_text

                fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw_text, re.DOTALL)
                if fm_match:
                    try:
                        meta = yaml.safe_load(fm_match.group(1)) or {}
                    except Exception:
                        meta = {}
                    body = raw_text[fm_match.end():].strip()

                name = meta.get("name", item.name).strip()
                description = meta.get("description", "").strip()
                req_tools = meta.get("requires_tools", [])
                if isinstance(req_tools, str):
                    req_tools = [req_tools]

                self._cache[name] = {
                    "name": name,
                    "version": str(meta.get("version", "1.0.0")),
                    "description": description,
                    "requires_tools": list(req_tools),
                    "path": str(skill_file),
                    "body": body,
                    "sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16],
                }
                self._cache_mtimes[item.name] = mtime
            except Exception as exc:
                logger.warning("Failed parsing skill at %s: %s", skill_file, exc)

    def discover(self, query: str = "") -> List[Dict[str, Any]]:
        """Search and list available skills."""
        self._scan()
        query_terms = [t.lower() for t in query.split() if len(t) > 2]

        results = []
        for name, skill in self.cache.items():
            # Check tool readiness
            missing_tools = [t for t in skill["requires_tools"] if not _is_tool_supported(t)]
            status = "ready" if not missing_tools else f"missing_tools: {', '.join(missing_tools)}"

            score = 0
            if not query_terms:
                score = 1
            else:
                text_to_search = f"{skill['name']} {skill['description']} {skill['body'][:500]}".lower()
                for term in query_terms:
                    if term in text_to_search:
                        score += 1

            if score > 0:
                results.append({
                    "name": skill["name"],
                    "version": skill["version"],
                    "description": skill["description"],
                    "requires_tools": skill["requires_tools"],
                    "status": status,
                    "ready": len(missing_tools) == 0,
                })

        return results

    @property
    def cache(self) -> Dict[str, Dict[str, Any]]:
        self._scan()
        return self._cache

    def read(self, skill_name: str) -> Optional[Dict[str, Any]]:
        """Read the full content and procedure of a specific skill."""
        self._scan()
        name_clean = skill_name.strip()
        skill = self._cache.get(name_clean)
        if not skill:
            # Case-insensitive fallback
            for k, v in self._cache.items():
                if k.lower() == name_clean.lower():
                    skill = v
                    break
        if not skill:
            return None

        missing_tools = [t for t in skill["requires_tools"] if not _is_tool_supported(t)]
        return {
            "name": skill["name"],
            "version": skill["version"],
            "description": skill["description"],
            "requires_tools": skill["requires_tools"],
            "ready": len(missing_tools) == 0,
            "missing_tools": missing_tools,
            "procedure": skill["body"],
            "sha256": skill["sha256"],
        }


SKILL_CATALOG = SkillCatalog()


def discover_skills(query: str = "") -> List[Dict[str, Any]]:
    """Discover available skills in the catalog."""
    return SKILL_CATALOG.discover(query)


def read_skill(skill_name: str) -> Optional[Dict[str, Any]]:
    """Read a specific skill from the catalog."""
    return SKILL_CATALOG.read(skill_name)


# ==============================================================================
# Tool Handlers for Sabrina
# ==============================================================================

def _handle_discover_skills(action: Dict[str, Any], loop: Any = None) -> str:
    query = action.get("query") or action.get("search") or ""
    results = SKILL_CATALOG.discover(query)
    if not results:
        return f"No skills matched query '{query}'. Call discover_skills without query to list all skills."
    return json.dumps(results, indent=2, ensure_ascii=False)


def _handle_read_skill(action: Dict[str, Any], loop: Any = None) -> str:
    name = action.get("name") or action.get("skill") or action.get("skill_name") or action.get("target") or ""
    if not name:
        name = "service-mapping"
    skill = SKILL_CATALOG.read(name)
    if not skill:
        available = list(SKILL_CATALOG.cache.keys())
        return f"Error: Skill '{name}' not found. Available skills: {', '.join(available)}"
    return json.dumps(skill, indent=2, ensure_ascii=False)


TOOL_REGISTRY.register(
    name="discover_skills",
    handler=_handle_discover_skills,
    description="Discover available editable SKILL.md workflows by query or list all available skills.",
    parameters={"query": "optional search query (e.g. 'docker', 'mapping', 'analysis')"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)

TOOL_REGISTRY.register(
    name="read_skill",
    handler=_handle_read_skill,
    description="Read the complete procedure, requirements, and workflow criteria for a specific skill.",
    parameters={"name": "exact skill name (e.g. 'service-mapping', 'docker-management')"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=True,
)


def _handle_load_skill(action: Dict[str, Any], loop: Any = None) -> str:
    name = action.get("name") or action.get("skill") or action.get("skill_name") or ""
    if not name:
        return "Error: load_skill requires 'name' (e.g. 'service-mapping')."
    skill = SKILL_CATALOG.read(name)
    if not skill:
        available = list(SKILL_CATALOG.cache.keys())
        return f"Error: Skill '{name}' not found. Available skills: {', '.join(available)}"

    if loop is not None:
        if not hasattr(loop, "active_skills"):
            loop.active_skills = []
        loop.active_skills = [s for s in loop.active_skills if s.get("name") != skill["name"]]
        loop.active_skills.append(skill)

        if hasattr(loop, "objective") and loop.objective is not None:
            loop.objective.pin_skill(skill)
            loop.objective.evidence_store.record_tool_call("load_skill", {"name": name}, f"Pinned skill {name}")

    v_str = f"v{skill.get('version', '1.0.0')}"
    sha_str = skill.get("sha256", "")[:16]
    return (
        f"SUCCESS: Loaded and pinned skill '{skill['name']}' ({v_str}, SHA-256:{sha_str}).\n"
        f"Its operational procedure is now active and will be strictly enforced in subsequent rounds.\n\n"
        f"Procedure preview:\n{skill.get('procedure', '')[:500]}..."
    )


TOOL_REGISTRY.register(
    name="load_skill",
    handler=_handle_load_skill,
    description="Explicitly select and load a skill from the inventory, pinning its procedure and SHA-256 for the run.",
    parameters={"name": "exact skill name (e.g. 'service-mapping')"},
    allowed_modes=("read", "plan", "write", "yolo", "secure_code"),
    parallel_safe=False,
)

