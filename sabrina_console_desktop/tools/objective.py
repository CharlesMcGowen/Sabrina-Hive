"""Objective Contract, Evidence Store, and Completion Gating for Sabrina.

Defines the contract for an autonomous task:
- Goal and operational scope (permitted hosts, permitted roots, tool restrictions, read-only enforcement).
- Selected skills and runtime constraints.
- Evidence store tracking verified artifacts, file citations, and service observations.
- Completion gate that requires concrete proof before allowing a run to finish.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("sabrina.objective")


@dataclass
class EvidenceStore:
    """Tracks verified technical evidence collected during an objective execution."""
    verified_paths: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    service_observations: List[Dict[str, Any]] = field(default_factory=list)
    component_citations: Dict[str, str] = field(default_factory=dict)
    unresolved_questions: List[str] = field(default_factory=list)
    tool_invocations: List[Dict[str, Any]] = field(default_factory=list)

    def record_path_verification(self, path: str, host: str = "ego.local", exists: bool = True, details: str = "") -> None:
        key = f"{host}:{path}"
        self.verified_paths[key] = {
            "path": path,
            "host": host,
            "exists": exists,
            "details": details[:500],
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

    def record_service_observation(
        self,
        service_name: str,
        state: str,
        is_running: bool,
        image: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.service_observations.append({
            "service": service_name,
            "state": state,
            "is_running": is_running,
            "image": image,
            "details": details or {},
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    def record_component_citation(self, component_name: str, file_path: str) -> None:
        self.component_citations[component_name] = file_path

    def record_unresolved_question(self, question_or_issue: str) -> None:
        if question_or_issue and question_or_issue not in self.unresolved_questions:
            self.unresolved_questions.append(question_or_issue)

    def record_tool_call(self, tool_name: str, params: Dict[str, Any], result_preview: str = "") -> None:
        self.tool_invocations.append({
            "tool": tool_name,
            "params": params,
            "preview": result_preview[:300],
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verified_paths": self.verified_paths,
            "service_observations": self.service_observations,
            "component_citations": self.component_citations,
            "unresolved_questions": self.unresolved_questions,
            "tool_invocation_count": len(self.tool_invocations),
        }

    def save(self, filepath: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump({
                "verified_paths": self.verified_paths,
                "service_observations": self.service_observations,
                "component_citations": self.component_citations,
                "unresolved_questions": self.unresolved_questions,
                "tool_invocations": self.tool_invocations,
            }, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, filepath: str) -> "EvidenceStore":
        store = cls()
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                store.verified_paths = data.get("verified_paths", {})
                store.service_observations = data.get("service_observations", [])
                store.component_citations = data.get("component_citations", {})
                store.unresolved_questions = data.get("unresolved_questions", [])
                store.tool_invocations = data.get("tool_invocations", [])
            except Exception as e:
                logger.warning("Failed loading evidence store from %s: %e", filepath, e)
        return store


@dataclass
class ObjectiveContract:
    """Defines the authorization scope, expectations, and evidence requirements of a task."""
    goal: str
    permitted_hosts: Tuple[str, ...] = ("ego.local", "192.168.2.69", "localhost", "127.0.0.1")
    permitted_roots: Tuple[str, ...] = ("/home/ego/kraken-explorer", "/mnt/14tb/kraken-explorer")
    permitted_tools: Optional[Set[str]] = None
    read_only: bool = True
    max_rounds: Optional[int] = None
    max_seconds: Optional[int] = None
    selected_skills: List[Dict[str, Any]] = field(default_factory=list)
    required_evidence: List[str] = field(default_factory=lambda: [
        "verified_source_locations",
        "component_map",
        "service_observations",
        "unresolved_questions",
    ])
    evidence_store: EvidenceStore = field(default_factory=EvidenceStore)
    slug: str = field(default="")

    def __post_init__(self) -> None:
        if not self.slug:
            safe_text = re.sub(r"[^a-zA-Z0-9_\-]", "_", self.goal[:32].strip().lower())
            hash_suffix = hashlib.sha1(self.goal.encode("utf-8")).hexdigest()[:8]
            self.slug = f"{safe_text}_{hash_suffix}"

    def is_path_permitted(self, path: str) -> bool:
        """Check if a path falls under one of the permitted roots."""
        if not path:
            return False
        clean_path = path.replace("\\", "/").rstrip("/")
        for root in self.permitted_roots:
            clean_root = root.replace("\\", "/").rstrip("/")
            if clean_path == clean_root or clean_path.startswith(clean_root + "/"):
                return True
        return False

    def is_host_permitted(self, host: str) -> bool:
        """Check if a host is within permitted targets."""
        if not host:
            return True
        clean_host = host.strip().lower()
        return any(clean_host == h.lower() for h in self.permitted_hosts)

    def pin_skill(self, skill_dict: Dict[str, Any]) -> None:
        """Pin a loaded skill into this objective contract."""
        name = skill_dict.get("name")
        if not name:
            return
        # Replace existing or append
        self.selected_skills = [s for s in self.selected_skills if s.get("name") != name]
        self.selected_skills.append({
            "name": name,
            "version": skill_dict.get("version", "1.0.0"),
            "sha256": skill_dict.get("sha256", ""),
            "description": skill_dict.get("description", ""),
            "procedure": skill_dict.get("procedure", skill_dict.get("body", "")),
            "pinned_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        })

    def checkpoint_path(self) -> str:
        return os.path.join("data", "objectives", f"{self.slug}_progress.json")

    def persist_progress(self, round_num: int, plan: List[str], plan_progress: Dict[int, int]) -> None:
        data = {
            "slug": self.slug,
            "goal": self.goal,
            "round_num": round_num,
            "plan": plan,
            "plan_progress": plan_progress,
            "selected_skills": [
                {"name": s["name"], "sha256": s.get("sha256", ""), "version": s.get("version", "")}
                for s in self.selected_skills
            ],
            "evidence": self.evidence_store.to_dict(),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        os.makedirs(os.path.join("data", "objectives"), exist_ok=True)
        with open(self.checkpoint_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


class CompletionValidator:
    """Verifies that an objective's required evidence is satisfied before allowing completion."""

    @staticmethod
    def validate(objective: ObjectiveContract, action: Dict[str, Any]) -> Tuple[bool, str, List[str]]:
        """Validate completion action and evidence store against objective contract.

        Returns: (is_valid, verdict_message, missing_requirements)
        """
        report = str(action.get("report") or action.get("summary") or "")
        reason = str(action.get("reason") or "")
        combined_text = f"{reason}\n\n{report}"
        missing = []

        # 1. Verified Source Locations
        if "verified_source_locations" in objective.required_evidence:
            has_store_paths = any(p.get("exists") for p in objective.evidence_store.verified_paths.values())
            mentions_candidate = any(
                root in combined_text for root in objective.permitted_roots
            )
            has_files_cited = len(re.findall(r"[a-zA-Z0-9_\-]+\.(?:py|json|yml|yaml|md|sh|js|ts)", report)) >= 2
            if not (has_store_paths or mentions_candidate) or not has_files_cited:
                missing.append(
                    "verified_source_locations (must verify and cite actual files under candidate roots, "
                    f"e.g. {', '.join(objective.permitted_roots)})"
                )

        # 2. Component Map
        if "component_map" in objective.required_evidence:
            comp_headers = ["component", "architecture", "entry point", "entrypoint", "modules", "daemons"]
            has_comp_section = any(h in combined_text.lower() for h in comp_headers)
            has_enough_components = len(re.findall(r"-\s*[`\*]?[a-zA-Z0-9_\-\.\/]+[`\*]?\s*:", report)) >= 2 or len(objective.evidence_store.component_citations) >= 2
            if not has_comp_section and not has_enough_components:
                missing.append(
                    "component_map (must provide a structured component inventory mapping entrypoints/files to roles)"
                )

        # 3. Service Observations (running vs configured)
        if "service_observations" in objective.required_evidence:
            has_store_services = len(objective.evidence_store.service_observations) > 0
            mentions_docker_or_service = any(w in combined_text.lower() for w in ["container", "docker", "service", "running", "dormant", "stopped"])
            distinguishes_state = ("running" in combined_text.lower() and ("configured" in combined_text.lower() or "dormant" in combined_text.lower() or "stopped" in combined_text.lower() or "not running" in combined_text.lower()))
            if not (has_store_services or (mentions_docker_or_service and distinguishes_state)):
                missing.append(
                    "service_observations (must observe deployed container/service states and explicitly "
                    "distinguish configured/dormant services from actively running ones)"
                )

        # 4. Unresolved Questions / Access Failures
        if "unresolved_questions" in objective.required_evidence:
            unres_markers = ["unresolved", "open question", "access failure", "limitation", "not verified", "access gap", "access issue"]
            has_unres_section = any(m in combined_text.lower() for m in unres_markers)
            if not has_unres_section and not objective.evidence_store.unresolved_questions:
                missing.append(
                    "unresolved_questions (must explicitly document any unresolved questions, candidate leads that were not verified, or access failures)"
                )

        # Minimum substantive length check
        if len(report.strip()) < 150:
            missing.append("substantive_report (the completion report is too brief; provide detailed technical evidence)")

        if missing:
            msg = (
                f"COMPLETION BLOCKED: Objective evidence requirements not yet met.\n"
                f"Missing or incomplete:\n" + "\n".join(f"- {m}" for m in missing) + "\n\n"
                f"Please execute the necessary discovery and inspection actions before calling completed."
            )
            return False, msg, missing

        return True, "COMPLETION ACCEPTED: All objective evidence requirements verified.", []


def create_kraken_objective(goal: Optional[str] = None) -> ObjectiveContract:
    """Create the canonical Kraken source & service discovery objective contract."""
    effective_goal = goal or (
        "Locate Kraken's source, identify its components and entry points, "
        "map its deployed services, and produce a status report with file and service evidence."
    )
    return ObjectiveContract(
        goal=effective_goal,
        permitted_hosts=("ego.local", "192.168.2.69", "localhost", "127.0.0.1"),
        permitted_roots=("/home/ego/kraken-explorer", "/mnt/14tb/kraken-explorer"),
        read_only=True,
        required_evidence=[
            "verified_source_locations",
            "component_map",
            "service_observations",
            "unresolved_questions",
        ],
        slug="kraken_discovery",
    )
