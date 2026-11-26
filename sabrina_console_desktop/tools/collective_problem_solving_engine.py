"""Autonomous Collective Problem-Solving & Task-Driving Engine for Sabrina.

Implements a decentralized multi-agent collaboration structure where swarm models:
1. Drive each other on the next task:
   - When a model finishes a task, it automatically discovers newly unblocked downstream
     tasks in the DAG, selects the optimal specialist model from the 40+ Swarm Catalog,
     and primes/drives the next task with upstream artifacts and contextual advice.
2. Ask for help ('Help-Wanted' / SOS Protocol):
   - When a model encounters an obstacle, ambiguous edge case, or failed verification,
     it publishes a structured PeerHelpRequest to the collective blackboard.
3. Help each other problem-solve (Peer Consultation & Solution Synthesis):
   - Specialist peer models review open requests, analyze root causes, and propose
     actionable PeerAssistanceOffers with verification proofs.
4. Collaborative Resolution & Co-Authorship:
   - The requester tests and accepts the peer offer, advancing the DAG task with
     joint co-authorship and durable provenance tracking.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

# Ensure Sabrina root is on sys.path
SABRINA_ROOT = Path(__file__).resolve().parent.parent
if str(SABRINA_ROOT) not in sys.path:
    sys.path.insert(0, str(SABRINA_ROOT))

from harnesses.external.cognitive_blackboard import HierarchicalCognitiveBlackboard
from harnesses.external.collective_pool import DEFAULT_DB_PATH
from harnesses.external.swarm_system_bridges import (
    select_competent_model_for_capability,
    sync_peer_resolution_to_blackboard,
)
from tools.collective_ordering import CollectiveOrderingEngine, ensure_dag_schema
from tools.swarm_model_evolution_engine import SwarmModelCatalog

LOGGER = logging.getLogger("sabrina.collective_problem_solving")


# -----------------------------------------------------------------------------
# 1. Database Schema & Tables
# -----------------------------------------------------------------------------

def ensure_collective_collaboration_schema(conn: sqlite3.Connection) -> None:
    """Ensure SQLite tables for peer help requests, assistance offers, and handoffs exist."""
    ensure_dag_schema(conn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_peer_help_requests (
            request_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            project TEXT NOT NULL,
            requester_model TEXT NOT NULL,
            title TEXT NOT NULL,
            problem_statement TEXT NOT NULL,
            urgency TEXT DEFAULT 'normal',
            capability_needed TEXT NOT NULL,
            context_payload_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'open',
            created REAL NOT NULL,
            updated REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_peer_assistance_offers (
            offer_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            helper_model TEXT NOT NULL,
            diagnosis TEXT NOT NULL,
            proposed_solution TEXT NOT NULL,
            confidence_score REAL NOT NULL,
            verification_advice TEXT DEFAULT '',
            status TEXT DEFAULT 'proposed',
            created REAL NOT NULL,
            FOREIGN KEY (request_id) REFERENCES assistant_peer_help_requests(request_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_task_handoffs (
            handoff_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            completed_task_id TEXT NOT NULL,
            next_task_id TEXT NOT NULL,
            driver_model TEXT NOT NULL,
            target_model TEXT NOT NULL,
            handoff_context_json TEXT DEFAULT '{}',
            status TEXT DEFAULT 'dispatched',
            created REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_peer_help_status ON assistant_peer_help_requests(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_peer_help_task ON assistant_peer_help_requests(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_peer_help_cap ON assistant_peer_help_requests(capability_needed)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_peer_offers_req ON assistant_peer_assistance_offers(request_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_handoffs_proj ON assistant_task_handoffs(project)")
    conn.commit()


# -----------------------------------------------------------------------------
# 2. Data Models
# -----------------------------------------------------------------------------

@dataclass
class PeerHelpRequest:
    request_id: str
    task_id: str
    project: str
    requester_model: str
    title: str
    problem_statement: str
    capability_needed: str
    context_payload: Dict[str, Any] = field(default_factory=dict)
    urgency: str = "normal"  # "low" | "normal" | "high" | "critical"
    status: str = "open"  # "open" | "in_consultation" | "resolved" | "closed"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


@dataclass
class PeerAssistanceOffer:
    offer_id: str
    request_id: str
    helper_model: str
    diagnosis: str
    proposed_solution: str
    confidence_score: float  # 0.0 - 1.0
    verification_advice: str = ""
    status: str = "proposed"  # "proposed" | "accepted" | "rejected"
    created: float = field(default_factory=time.time)


@dataclass
class TaskHandoff:
    handoff_id: str
    project: str
    completed_task_id: str
    next_task_id: str
    driver_model: str
    target_model: str
    handoff_context: Dict[str, Any] = field(default_factory=dict)
    status: str = "dispatched"  # "dispatched" | "accepted" | "in_progress"
    created: float = field(default_factory=time.time)


# -----------------------------------------------------------------------------
# 3. Collective Problem-Solving & Driving Engine
# -----------------------------------------------------------------------------

class CollectiveProblemSolvingEngine:
    """Coordinates proactive task handoffs, peer help requests, and collaborative problem-solving."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        ordering_engine: Optional[CollectiveOrderingEngine] = None,
    ):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ordering = ordering_engine or CollectiveOrderingEngine(self.db_path)
        self.blackboard = HierarchicalCognitiveBlackboard(self.db_path)

        with sqlite3.connect(self.db_path) as conn:
            ensure_collective_collaboration_schema(conn)

    # -------------------------------------------------------------------------
    # Capability-Based Model Routing
    # -------------------------------------------------------------------------

    def select_best_model_for_capability(self, capability_required: str) -> str:
        """Finds the most suitable model from Sabrina's 40+ model swarm or 200+ portal collective for a specific capability."""
        cap_lower = capability_required.lower()
        all_models = SwarmModelCatalog.KNOWN_SWARM_MODELS

        # Priority keyword heuristics mapping capabilities to specialized swarm nodes
        if any(w in cap_lower for w in ("math", "symbolic", "pde", "sde", "heston", "crank_nicolson")):
            candidates = ["gemini-3.5-flash", "qwen3:14b", "sabrina-jury-qwen:0.2", "deepseek-r1:32b"]
        elif any(w in cap_lower for w in ("security", "sast", "dast", "exploit", "ssti", "ssrf", "toctou")):
            candidates = ["custom-sme-v2:latest", "sabrina-jury-prosecutor:0.2", "mri-cybersec-donor-3b", "qwen2.5-coder:14b"]
        elif any(w in cap_lower for w in ("crypto", "timing", "sidechannel", "hmac")):
            candidates = ["deepseek-r1:32b", "gemini-3.5-flash", "sabrina-jury-defender:0.2"]
        elif any(w in cap_lower for w in ("concurrency", "deadlock", "race", "asyncio", "lock")):
            candidates = ["qwen2.5-coder:14b", "deepseek-r1:32b", "granite-code:8b"]
        elif any(w in cap_lower for w in ("wsp", "task_vector", "weight", "svd", "dare")):
            candidates = ["sabrina-task-vector-zero:001", "qwen2.5-coder-1.5b", "gemini-3.5-flash"]
        elif any(w in cap_lower for w in ("audit", "review", "consensus")):
            candidates = ["sabrina-jury-granite:0.2", "sabrina-jury-llama:0.2", "llama3.3:70b"]
        else:
            candidates = ["qwen2.5-coder:14b", "gemini-3.5-flash", "sabrina-qwen2.5-coder:1.5b-clean"]

        fallback_known = [c for c in candidates if c in all_models] or ["qwen2.5-coder:14b"]
        return select_competent_model_for_capability(self.blackboard, cap_lower, fallback_known)

    # -------------------------------------------------------------------------
    # 1. Ask For Help Protocol
    # -------------------------------------------------------------------------

    def ask_for_help(
        self,
        task_id: str,
        project: str,
        requester_model: str,
        title: str,
        problem_statement: str,
        capability_needed: str,
        context_payload: Optional[Dict[str, Any]] = None,
        urgency: str = "normal",
    ) -> PeerHelpRequest:
        """A model posts an explicit Help Request to the collective when it encounters an obstacle."""
        request_id = f"help_{uuid.uuid4().hex[:12]}"
        now = time.time()
        ctx_json = json.dumps(context_payload or {}, sort_keys=True)

        with sqlite3.connect(self.db_path) as conn:
            ensure_collective_collaboration_schema(conn)
            conn.execute(
                """
                INSERT INTO assistant_peer_help_requests (
                    request_id, task_id, project, requester_model, title,
                    problem_statement, urgency, capability_needed,
                    context_payload_json, status, created, updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    request_id, task_id, project, requester_model, title,
                    problem_statement, urgency, capability_needed,
                    ctx_json, now, now,
                ),
            )
            conn.commit()

        LOGGER.info(
            "Model %s posted Help Request %s: '%s' (Needed: %s, Urgency: %s)",
            requester_model, request_id, title, capability_needed, urgency
        )

        return PeerHelpRequest(
            request_id=request_id,
            task_id=task_id,
            project=project,
            requester_model=requester_model,
            title=title,
            problem_statement=problem_statement,
            capability_needed=capability_needed,
            context_payload=context_payload or {},
            urgency=urgency,
            status="open",
            created=now,
            updated=now,
        )

    def get_open_help_requests(
        self,
        capability_filter: Optional[str] = None,
        project_filter: Optional[str] = None,
    ) -> List[PeerHelpRequest]:
        """Queries all open help requests waiting for peer consultation."""
        query = "SELECT * FROM assistant_peer_help_requests WHERE status IN ('open', 'in_consultation')"
        params: List[Any] = []

        if capability_filter:
            query += " AND capability_needed LIKE ?"
            params.append(f"%{capability_filter}%")
        if project_filter:
            query += " AND project = ?"
            params.append(project_filter)

        query += " ORDER BY created ASC"

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()

        results: List[PeerHelpRequest] = []
        for r in rows:
            results.append(
                PeerHelpRequest(
                    request_id=r["request_id"],
                    task_id=r["task_id"],
                    project=r["project"],
                    requester_model=r["requester_model"],
                    title=r["title"],
                    problem_statement=r["problem_statement"],
                    capability_needed=r["capability_needed"],
                    context_payload=json.loads(r["context_payload_json"] or "{}"),
                    urgency=r["urgency"],
                    status=r["status"],
                    created=r["created"],
                    updated=r["updated"],
                )
            )
        return results

    # -------------------------------------------------------------------------
    # 2. Peer Assistance & Collaborative Triage
    # -------------------------------------------------------------------------

    def propose_assistance(
        self,
        request_id: str,
        helper_model: str,
        diagnosis: str,
        proposed_solution: str,
        confidence_score: float,
        verification_advice: str = "",
    ) -> PeerAssistanceOffer:
        """A peer specialist model submits a proposed solution to an open Help Request."""
        offer_id = f"offer_{uuid.uuid4().hex[:12]}"
        now = time.time()

        with sqlite3.connect(self.db_path) as conn:
            ensure_collective_collaboration_schema(conn)

            # Insert offer
            conn.execute(
                """
                INSERT INTO assistant_peer_assistance_offers (
                    offer_id, request_id, helper_model, diagnosis,
                    proposed_solution, confidence_score, verification_advice,
                    status, created
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    offer_id, request_id, helper_model, diagnosis,
                    proposed_solution, confidence_score, verification_advice, now,
                ),
            )

            # Move request status to in_consultation
            conn.execute(
                "UPDATE assistant_peer_help_requests SET status = 'in_consultation', updated = ? WHERE request_id = ?",
                (now, request_id),
            )
            row_cap = conn.execute(
                "SELECT capability_needed FROM assistant_peer_help_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            cap_str = str(row_cap[0]) if row_cap and row_cap[0] else "general"
            conn.commit()

        try:
            sync_peer_resolution_to_blackboard(
                self.blackboard,
                request_id,
                cap_str,
                helper_model,
                diagnosis,
                proposed_solution,
                confidence_score,
            )
        except Exception:
            pass

        LOGGER.info(
            "Peer %s proposed solution for request %s (Confidence: %.2f)",
            helper_model, request_id, confidence_score
        )

        return PeerAssistanceOffer(
            offer_id=offer_id,
            request_id=request_id,
            helper_model=helper_model,
            diagnosis=diagnosis,
            proposed_solution=proposed_solution,
            confidence_score=confidence_score,
            verification_advice=verification_advice,
            status="proposed",
            created=now,
        )

    def get_offers_for_request(self, request_id: str) -> List[PeerAssistanceOffer]:
        """Retrieves all assistance offers for a specific Help Request."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM assistant_peer_assistance_offers WHERE request_id = ? ORDER BY confidence_score DESC",
                (request_id,),
            ).fetchall()

        return [
            PeerAssistanceOffer(
                offer_id=r["offer_id"],
                request_id=r["request_id"],
                helper_model=r["helper_model"],
                diagnosis=r["diagnosis"],
                proposed_solution=r["proposed_solution"],
                confidence_score=r["confidence_score"],
                verification_advice=r["verification_advice"],
                status=r["status"],
                created=r["created"],
            )
            for r in rows
        ]

    def accept_assistance(
        self,
        request_id: str,
        offer_id: str,
        feedback: str = "Accepted peer resolution",
    ) -> Dict[str, Any]:
        """The requester verifies and accepts a peer's offer, resolving the bottleneck."""
        now = time.time()
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_collective_collaboration_schema(conn)

            # Verify offer exists
            offer = conn.execute(
                "SELECT * FROM assistant_peer_assistance_offers WHERE offer_id = ? AND request_id = ?",
                (offer_id, request_id),
            ).fetchone()
            if not offer:
                raise ValueError(f"Offer {offer_id} not found for request {request_id}")

            # Mark offer accepted, reject others
            conn.execute(
                "UPDATE assistant_peer_assistance_offers SET status = 'accepted' WHERE offer_id = ?",
                (offer_id,),
            )
            conn.execute(
                "UPDATE assistant_peer_assistance_offers SET status = 'superseded' WHERE request_id = ? AND offer_id != ?",
                (request_id, offer_id),
            )

            # Mark request resolved
            conn.execute(
                "UPDATE assistant_peer_help_requests SET status = 'resolved', updated = ? WHERE request_id = ?",
                (now, request_id),
            )

            # Update DAG task evidence if linked
            req = conn.execute(
                "SELECT task_id, requester_model FROM assistant_peer_help_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()

            if req and req["task_id"]:
                t_id = req["task_id"]
                co_author = f"{req['requester_model']} (Assisted by {offer['helper_model']})"
                evidence_note = f"Resolved via Peer Offer {offer_id}: {feedback}"

                conn.execute(
                    """
                    UPDATE assistant_dag_tasks
                    SET assigned_model = ?, verified_evidence = verified_evidence || ' | ' || ?, updated = ?
                    WHERE task_id = ?
                    """,
                    (co_author, evidence_note, now, t_id),
                )
            conn.commit()

        LOGGER.info(
            "Request %s resolved via Offer %s from %s. Feedback: %s",
            request_id, offer_id, offer["helper_model"], feedback
        )

        return {
            "status": "success",
            "request_id": request_id,
            "offer_id": offer_id,
            "helper_model": offer["helper_model"],
            "resolution_state": "resolved",
            "feedback": feedback,
        }

    # -------------------------------------------------------------------------
    # 3. Driving the Next Task (Autonomous Handoff)
    # -------------------------------------------------------------------------

    def drive_next_tasks(
        self,
        project: str,
        completed_task_id: str,
        driver_model: str,
        produced_artifacts: Optional[Dict[str, Any]] = None,
        context_hints: Optional[str] = None,
    ) -> List[TaskHandoff]:
        """Proactively drives newly unblocked downstream tasks in the DAG to the optimal specialist models."""
        # 1. Query all currently unblocked tasks in the project
        unblocked_tasks = self.ordering.get_unblocked_tasks(project)
        handoffs: List[TaskHandoff] = []

        now = time.time()
        for t in unblocked_tasks:
            # Check if this task depends on the completed task
            if completed_task_id in t["depends_on"]:
                next_task_id = t["task_id"]
                cap_req = t.get("capability_required", "general")

                # Select specialized target model
                target_model = self.select_best_model_for_capability(cap_req)

                handoff_id = f"handoff_{uuid.uuid4().hex[:12]}"
                handoff_ctx = {
                    "upstream_completed_by": driver_model,
                    "upstream_task_id": completed_task_id,
                    "upstream_artifacts": produced_artifacts or {},
                    "context_hints": context_hints or f"Handed off from {driver_model} upon completing {completed_task_id}",
                    "acceptance_criteria": t.get("acceptance_criteria", ""),
                }

                # Claim task for target model with lease
                self.ordering.claim_task(task_id=next_task_id, model_id=target_model, lease_seconds=300)

                with sqlite3.connect(self.db_path) as conn:
                    ensure_collective_collaboration_schema(conn)
                    conn.execute(
                        """
                        INSERT INTO assistant_task_handoffs (
                            handoff_id, project, completed_task_id, next_task_id,
                            driver_model, target_model, handoff_context_json,
                            status, created
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatched', ?)
                        """,
                        (
                            handoff_id, project, completed_task_id, next_task_id,
                            driver_model, target_model, json.dumps(handoff_ctx, sort_keys=True),
                            now,
                        ),
                    )
                    conn.commit()

                LOGGER.info(
                    "Model %s driven next task %s -> %s (Capability: %s)",
                    driver_model, next_task_id, target_model, cap_req
                )

                handoffs.append(
                    TaskHandoff(
                        handoff_id=handoff_id,
                        project=project,
                        completed_task_id=completed_task_id,
                        next_task_id=next_task_id,
                        driver_model=driver_model,
                        target_model=target_model,
                        handoff_context=handoff_ctx,
                        status="dispatched",
                        created=now,
                    )
                )

        return handoffs

    # -------------------------------------------------------------------------
    # 4. Collective Solvers & Telemetry
    # -------------------------------------------------------------------------

    def get_collaboration_telemetry(self, project: Optional[str] = None) -> Dict[str, Any]:
        """Compiles real-time metrics on swarm problem-solving, help requests, and handoff velocity."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_collective_collaboration_schema(conn)

            proj_clause = "WHERE project = ?" if project else ""
            proj_params = [project] if project else []

            total_help = conn.execute(f"SELECT COUNT(*) as c FROM assistant_peer_help_requests {proj_clause}", proj_params).fetchone()["c"]
            resolved_help = conn.execute(
                f"SELECT COUNT(*) as c FROM assistant_peer_help_requests {proj_clause} {'AND' if project else 'WHERE'} status = 'resolved'",
                proj_params
            ).fetchone()["c"]
            open_help = conn.execute(
                f"SELECT COUNT(*) as c FROM assistant_peer_help_requests {proj_clause} {'AND' if project else 'WHERE'} status = 'open'",
                proj_params
            ).fetchone()["c"]
            total_offers = conn.execute("SELECT COUNT(*) as c FROM assistant_peer_assistance_offers").fetchone()["c"]
            accepted_offers = conn.execute("SELECT COUNT(*) as c FROM assistant_peer_assistance_offers WHERE status = 'accepted'").fetchone()["c"]
            total_handoffs = conn.execute(f"SELECT COUNT(*) as c FROM assistant_task_handoffs {proj_clause}", proj_params).fetchone()["c"]

            # Recent handoffs
            recent_handoffs = conn.execute(
                f"SELECT * FROM assistant_task_handoffs {proj_clause} ORDER BY created DESC LIMIT 5",
                proj_params
            ).fetchall()

            # Recent peer consultations
            recent_requests = conn.execute(
                f"SELECT * FROM assistant_peer_help_requests {proj_clause} ORDER BY created DESC LIMIT 5",
                proj_params
            ).fetchall()

        resolution_rate = (resolved_help / total_help * 100.0) if total_help > 0 else 100.0

        return {
            "total_help_requests": total_help,
            "open_help_requests": open_help,
            "resolved_help_requests": resolved_help,
            "resolution_rate_pct": resolution_rate,
            "total_assistance_offers": total_offers,
            "accepted_assistance_offers": accepted_offers,
            "total_task_handoffs_driven": total_handoffs,
            "recent_handoffs": [
                {
                    "handoff_id": r["handoff_id"],
                    "from_task": r["completed_task_id"],
                    "to_task": r["next_task_id"],
                    "driver": r["driver_model"],
                    "target": r["target_model"],
                }
                for r in recent_handoffs
            ],
            "recent_help_requests": [
                {
                    "request_id": r["request_id"],
                    "title": r["title"],
                    "requester": r["requester_model"],
                    "capability": r["capability_needed"],
                    "status": r["status"],
                }
                for r in recent_requests
            ],
        }
