"""Shared capability discovery, qualification lifecycle, and handoff contracts for Sabrina agents."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VALID_EXECUTION_MODES = {"live", "simulation", "replay"}
VALID_EXECUTION_KINDS = {"code", "agent", "browser"}


def compute_capability_harness_hash() -> str:
    """Compute a deterministic SHA-256 fingerprint of the capability & verification harness."""
    h = hashlib.sha256()
    for rel in (
        "tools/capability_registry.py",
        "tools/sabrina_workflow.py",
        "tools/sabrina_workflow_verifier.py",
        "tools/sabrina_delegation_bridge.py",
    ):
        p = PROJECT_ROOT / rel
        if p.exists():
            h.update(rel.encode("utf-8"))
            h.update(p.read_bytes())
    return h.hexdigest()


@dataclass(frozen=True)
class Capability:
    name: str
    owner: str
    actions: tuple[str, ...]
    resources: tuple[str, ...] = ()
    availability: str = "ready"
    approval_required: bool = False
    catalog_state: str = "cataloged"
    auth_state: str = "authenticated"
    reachability_state: str = "reachable"
    tested_state: str = "tested"
    qualification_state: str = "qualified"
    execution_kind: str = "code"

    def __post_init__(self) -> None:
        if self.execution_kind not in VALID_EXECUTION_KINDS:
            raise ValueError(
                f"execution_kind must be one of {sorted(VALID_EXECUTION_KINDS)}, got {self.execution_kind!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


GEMINI_FAST_SWARM_MODELS: tuple[str, ...] = (
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-3.1-flash-lite",
    "gemini-3.7-flash",
)

WORKER_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "Antigravity.exe": {
        "worker": "Antigravity.exe",
        "owner": "antigravity-ide",
        "tier": "quality",
        "prior_quality": 0.95,
        "prior_speed": 0.68,
        "transports": ("desktop_ui", "subagent_swarm", "cli_plan"),
        "roles": ("thought", "implement", "critique", "swarm"),
        "model_family": "gemini-pro-and-flash-subagents",
        "catalog_state": "cataloged",
        "auth_state": "authenticated",
        "reachability_state": "reachable",
        "tested_state": "tested",
        "qualification_state": "qualified",
        "execution_mode": "live",
    },
    "Codex.exe": {
        "worker": "Codex.exe",
        "owner": "codex-astra",
        "tier": "quality",
        "prior_quality": 0.92,
        "prior_speed": 0.78,
        "transports": ("desktop_ui", "app_server_stdio"),
        "roles": ("thought", "implement", "verify"),
        "model_family": "codex-astra",
        "catalog_state": "cataloged",
        "auth_state": "authenticated",
        "reachability_state": "reachable",
        "tested_state": "tested",
        "qualification_state": "qualified",
        "execution_mode": "live",
    },
    "Codex.cli": {
        "worker": "Codex.cli",
        "owner": "codex-astra",
        "tier": "balanced",
        "prior_quality": 0.90,
        "prior_speed": 0.86,
        "transports": ("app_server_stdio", "cli_exec"),
        "roles": ("implement", "verify", "thought", "critique"),
        "model_family": "codex-cli",
        "catalog_state": "cataloged",
        "auth_state": "authenticated",
        "reachability_state": "reachable",
        "tested_state": "tested",
        "qualification_state": "qualified",
        "execution_mode": "live",
    },
    "GeminiFastSwarm": {
        "worker": "GeminiFastSwarm",
        "owner": "google-gemini-oracle",
        "tier": "fast",
        "prior_quality": 0.78,
        "prior_speed": 0.96,
        "transports": ("oracle_parallel_ladder",),
        "roles": ("swarm", "triage", "decompose", "draft"),
        "model_family": "gemini-flash-ladder",
        "models": GEMINI_FAST_SWARM_MODELS,
        "catalog_state": "cataloged",
        "auth_state": "authenticated",
        "reachability_state": "reachable",
        "tested_state": "tested",
        "qualification_state": "qualified",
        "execution_mode": "live",
    },
    "ExternalMind:HuggingFaceChat": {
        "worker": "ExternalMind:HuggingFaceChat",
        "owner": "huggingface-portal",
        "tier": "external",
        "prior_quality": 0.88,
        "prior_speed": 0.85,
        "transports": ("browser_web_oracle", "hf_hub_api"),
        "roles": ("thought", "implement", "critique", "swarm", "external_mind"),
        "model_family": "open-weights-huggingchat",
        "models": (
            "Qwen/Qwen2.5-Coder-32B-Instruct",
            "meta-llama/Llama-3.3-70B-Instruct",
            "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        ),
        "catalog_state": "cataloged",
        "auth_state": "unauthenticated",
        "reachability_state": "unprobed",
        "tested_state": "untested",
        "qualification_state": "revalidation_required",
        "execution_mode": "simulation",
    },
    "ExternalMind:DuckDuckGoAIChat": {
        "worker": "ExternalMind:DuckDuckGoAIChat",
        "owner": "duckduckgo-portal",
        "tier": "external",
        "prior_quality": 0.87,
        "prior_speed": 0.88,
        "transports": ("browser_web_oracle", "rest_json"),
        "roles": ("thought", "critique", "swarm", "external_mind"),
        "model_family": "ddg-ai-chat",
        "models": ("claude-3-haiku", "gpt-4o-mini", "meta-llama/Llama-3-70b-instruct"),
        "catalog_state": "cataloged",
        "auth_state": "unauthenticated",
        "reachability_state": "unprobed",
        "tested_state": "untested",
        "qualification_state": "revalidation_required",
        "execution_mode": "simulation",
    },
}


DEFAULT_CAPABILITIES = (
    Capability("kraken.inspect", "kraken-objectives", ("discovery", "service_status", "review"), execution_kind="agent"),
    Capability("kraken.objective", "kraken-skill-harness", ("research", "prepare_changes"), execution_kind="agent"),
    Capability("system.health_report", "operations-monitor", ("inspect", "report"), (), execution_kind="code"),
    Capability("linkedin.performance_report", "social-research", ("report",), (), execution_kind="code"),
    Capability("model.cpu_selection", "lab-coordinator", ("inspect", "recommend"), ("compute:lab",), execution_kind="code"),
    Capability("linkedin.browser", "social-browser", ("status", "feed_scan", "engage"), ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("linkedin.replies", "social-browser", ("scan", "draft", "publish"), ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("linkedin.publish_post", "social-browser", ("schedule", "publish"),
               ("browser:linkedin-primary",), approval_required=True, execution_kind="browser"),
    Capability("linkedin.reshare", "social-browser", ("schedule", "repost"),
               ("browser:linkedin-primary",), approval_required=True, execution_kind="browser"),
    Capability("social.cybersecurity_scout", "social-research",
               ("scan", "explore", "evaluate", "stage_reshare"),
               ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("linkedin.jobs_discover", "social-research",
               ("search_jobs", "score", "stage_application"),
               ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("linkedin.jobs_inspect", "social-browser",
               ("inspect_job", "prepare_easy_apply"),
               ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("linkedin.jobs_apply", "social-browser",
               ("apply_job", "submit_easy_apply"),
               ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("linkedin.inbox_triage", "social-browser",
               ("triage_inbox", "check_replies"),
               ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("ai_services.health_and_recover", "operations-monitor",
               ("check_ollama", "heal_coordinator", "check_services"),
               (), execution_kind="code"),
    Capability("rsi.universal_reflect", "sabrina-orchestrator",
               ("reflect", "evolve_queries", "expand_qa", "update_policy"),
               (), execution_kind="agent"),
    Capability("browser.recovery", "desktop-bridge",
               ("preflight", "reconnect", "recreate_forwards", "verify_cdp", "verify_auth"),
               ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("social.semantic_review", "social-research", ("classify", "draft"), ("browser:linkedin-primary",), execution_kind="browser"),
    Capability("x.feed", "social-browser", ("status", "feed_scan", "draft"), ("browser:x-primary",), execution_kind="browser"),
    Capability("x.status", "social-browser", ("status",), ("browser:x-primary",), execution_kind="browser"),
    Capability("x.analytics", "social-research", ("report", "metrics"), ("browser:x-primary",), execution_kind="browser"),
    Capability("x.publish", "social-browser", ("schedule", "publish"), ("browser:x-primary",), approval_required=True, execution_kind="browser"),
    Capability(
        "codex.astra.thought",
        "codex-astra",
        ("reason", "architect", "plan", "decompose", "review_spec"),
        ("desktop:primary", "codex:app-server"),
        execution_kind="agent",
    ),
    Capability(
        "codex.astra.implement",
        "codex-astra",
        ("implement", "patch", "generate_tests", "verify_code"),
        ("desktop:primary", "codex:app-server"),
        execution_kind="agent",
    ),
    Capability(
        "antigravity.thought",
        "antigravity-ide",
        ("reason", "architect", "plan", "decompose", "review_spec"),
        ("desktop:primary", "antigravity:ide"),
        execution_kind="agent",
    ),
    Capability(
        "antigravity.implement",
        "antigravity-ide",
        ("implement", "patch", "generate_tests", "verify_code"),
        ("desktop:primary", "antigravity:ide"),
        execution_kind="agent",
    ),
    Capability(
        "claude.thought",
        "claude-desktop",
        ("reason", "architect", "specify", "critique"),
        ("desktop:primary",),
        execution_kind="agent",
    ),
    Capability(
        "gemini.fast.swarm",
        "google-gemini-oracle",
        ("swarm", "triage", "decompose", "draft", "synthesize"),
        ("oracle:gemini-flash",),
        execution_kind="agent",
    ),
    Capability(
        "capabilities.dual_orchestration",
        "sabrina-orchestrator",
        (
            "codex_thought_antigravity_implement",
            "antigravity_thought_codex_implement",
            "select_weighted",
            "swarm_fast",
            "swap_roles",
            "status",
        ),
        ("desktop:primary", "codex:app-server", "antigravity:ide", "oracle:gemini-flash"),
        execution_kind="agent",
    ),
    Capability(
        "external.minds.collective",
        "external-mind-scout",
        (
            "scout_portals",
            "benchmark_portal",
            "adapt_portal",
            "ingest_portal",
            "swarm_portals",
            "scaling_tournament",
        ),
        ("browser:external-portals", "oracle:web-ai"),
        auth_state="unauthenticated",
        reachability_state="unprobed",
        tested_state="untested",
        qualification_state="revalidation_required",
        execution_kind="agent",
    ),
    Capability(
        "frontier.research.rsi_augment",
        "frontier-research-guild",
        ("evaluate_proposals", "augment_skills", "think", "promote"),
        ("oracle:frontier-budget",),
        execution_kind="agent",
    ),
    Capability(
        "frontier.research.wsp_thinking",
        "frontier-research-guild",
        ("task_vector_surgery", "dare_ties", "counterfactual_isolation", "think"),
        ("oracle:frontier-budget", "compute:lab"),
        execution_kind="agent",
    ),
    Capability(
        "frontier.research.hard_challenge",
        "frontier-research-guild",
        ("adversarial_audit", "toctou_analysis", "ssti_analysis", "patch_synthesis"),
        ("oracle:frontier-budget",),
        execution_kind="agent",
    ),
    Capability(
        "frontier.research.synthesis",
        "frontier-research-guild",
        ("multi_provider_council", "research_paper", "budget_reconciliation"),
        ("oracle:frontier-budget",),
        execution_kind="agent",
    ),
    Capability(
        "frontier.research.blt_augmentation",
        "frontier-research-guild",
        ("entropy_byte_patching", "ngram_hash_embedding", "blt_benchmark", "local_model_augmentation"),
        ("oracle:frontier-budget", "compute:lab"),
        execution_kind="agent",
    ),
    Capability(
        "storage.inventory",
        "operations-monitor",
        ("inventory", "inspect", "docker_audit"),
        (),
        execution_kind="code",
    ),
    Capability(
        "storage.migrate",
        "operations-monitor",
        ("plan_move", "move_dry_run", "move_execute"),
        ("storage:ego",),
        approval_required=True,
        execution_kind="code",
    ),
    Capability(
        "storage.backup",
        "operations-monitor",
        ("backup_dry_run", "backup_sqlite", "backup_rsync", "backup_tar"),
        ("storage:ego",),
        approval_required=True,
        execution_kind="code",
    ),
    Capability(
        "resources.internet_index_sweep",
        "internet-resource-indexer",
        ("sweep_public_apis", "sweep_datasets", "sweep_doc_repos", "evolve_bandit_queries"),
        (),
        execution_kind="code",
    ),
    Capability(
        "compression.autonomous_experiment",
        "neural-compressor-lab",
        ("train_byte_transformer", "arithmetic_encode_decode", "bpb_benchmark", "mutate_config"),
        ("compute:lab",),
        execution_kind="code",
    ),
    Capability(
        "exploitgym.rsi_cycle",
        "exploitgym-rsi-controller",
        ("evaluate_dev", "classify_bottleneck", "research_plane", "workshop_candidate", "replay_and_promote"),
        ("compute:lab", "docker:exploitgym"),
        execution_kind="agent",
    ),
    Capability(
        "system.self_repair_workshop",
        "self-repair-workshop",
        ("harvest_bugs", "sandbox_patch", "disjoint_audit", "stage_or_apply"),
        ("compute:lab",),
        execution_kind="agent",
    ),
    Capability(
        "system.compute_federation",
        "compute-federation-mesh",
        ("probe_nodes", "discover_web_compute", "spread_workload"),
        (),
        execution_kind="code",
    ),
    Capability(
        "github.autonomous_repo_forge",
        "upstream-oss-forge",
        ("browser_create_repo", "browser_generate_token", "sanitized_push", "stage_draft_pr"),
        (),
        execution_kind="browser",
    ),
    Capability(
        "forge.daily_paper_to_code",
        "daily-paper-tool-forge",
        ("ingest_daily_papers", "synthesize_tool", "disjoint_blind_review", "publish_plugin"),
        (),
        execution_kind="agent",
    ),
    Capability(
        "bounty.cve_variant_hunter",
        "cve-variant-hunter",
        ("harvest_in_scope_ghsas", "audit_sibling_parity", "stage_dossier_draft"),
        (),
        execution_kind="agent",
    ),
    Capability(
        "automation.gemini",
        "automation-connectors",
        ("generate", "classify_json", "evaluate_goal"),
        ("connector:gemini",),
        execution_kind="agent",
    ),
    Capability(
        "automation.plaid",
        "automation-connectors",
        ("accounts.balance", "transactions.sync", "recurring.list"),
        ("connector:plaid",),
        execution_kind="code",
    ),
    Capability(
        "automation.shopify",
        "automation-connectors",
        ("orders.list", "products.list", "order.note"),
        ("connector:shopify",),
        approval_required=True,
        execution_kind="code",
    ),
    Capability(
        "automation.woocommerce",
        "automation-connectors",
        ("orders.list", "products.list", "order.update"),
        ("connector:woocommerce",),
        approval_required=True,
        execution_kind="code",
    ),
    Capability(
        "automation.stripe",
        "automation-connectors",
        ("payments.list", "payouts.list", "refund.create"),
        ("connector:stripe",),
        approval_required=True,
        execution_kind="code",
    ),
    Capability(
        "automation.mcp",
        "automation-connectors",
        ("tools.list", "tools.call_read", "tools.call_write"),
        ("connector:mcp",),
        approval_required=True,
        execution_kind="code",
    ),
    Capability(
        "automation.webhook",
        "automation-connectors",
        ("http.get", "http.post"),
        ("connector:webhook",),
        approval_required=True,
        execution_kind="code",
    ),
    Capability(
        "automation.opensource",
        "automation-connectors",
        ("web.search", "page.crawl", "states.get", "service.call", "feed.poll"),
        ("connector:opensource",),
        execution_kind="code",
    ),
)

DUAL_ROLE_MODES = {
    "codex_thought_antigravity_implement": {
        "mode": "codex_thought_antigravity_implement",
        "thought_app": "Codex.exe",
        "thought_capability": "codex.astra.thought",
        "thought_owner": "codex-astra",
        "implement_app": "Antigravity.exe",
        "implement_capability": "antigravity.implement",
        "implement_owner": "antigravity-ide",
        "description": "Codex Astra performs deep reasoning/architecture; Antigravity implements and verifies.",
    },
    "antigravity_thought_codex_implement": {
        "mode": "antigravity_thought_codex_implement",
        "thought_app": "Antigravity.exe",
        "thought_capability": "antigravity.thought",
        "thought_owner": "antigravity-ide",
        "implement_app": "Codex.exe",
        "implement_capability": "codex.astra.implement",
        "implement_owner": "codex-astra",
        "description": "Antigravity performs deep reasoning/architecture; Codex Astra implements and verifies.",
    },
}

MODE_ALIASES = {
    "codex_thought_antigravity_implement": "codex_thought_antigravity_implement",
    "codex_to_antigravity": "codex_thought_antigravity_implement",
    "codex_astra_thought": "codex_thought_antigravity_implement",
    "antigravity_thought_codex_implement": "antigravity_thought_codex_implement",
    "antigravity_to_codex": "antigravity_thought_codex_implement",
    "antigravity_thought": "antigravity_thought_codex_implement",
    "vice_versa": "antigravity_thought_codex_implement",
}

QUALITY_WEIGHTS = {
    "quality": (0.80, 0.20),
    "balanced": (0.55, 0.45),
    "fast": (0.25, 0.75),
}


class CapabilityRegistry:
    def __init__(self, path: str | Path = "data/control/capabilities.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY,
                    capability TEXT,
                    objective TEXT,
                    state TEXT,
                    payload TEXT,
                    result TEXT,
                    created REAL,
                    updated REAL
                );
                CREATE TABLE IF NOT EXISTS capability_qualifications (
                    capability TEXT PRIMARY KEY,
                    catalog_state TEXT NOT NULL DEFAULT 'cataloged',
                    auth_state TEXT NOT NULL DEFAULT 'authenticated',
                    reachability_state TEXT NOT NULL DEFAULT 'reachable',
                    tested_state TEXT NOT NULL DEFAULT 'tested',
                    qualification_state TEXT NOT NULL DEFAULT 'qualified',
                    execution_mode TEXT NOT NULL DEFAULT 'live',
                    requires_revalidation INTEGER NOT NULL DEFAULT 0,
                    qualified_model TEXT NOT NULL DEFAULT '',
                    qualified_endpoint TEXT NOT NULL DEFAULT '',
                    qualified_adapter_hash TEXT NOT NULL DEFAULT '',
                    qualified_playbook_hash TEXT NOT NULL DEFAULT '',
                    qualified_harness_hash TEXT NOT NULL DEFAULT '',
                    provenance_ref TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL
                );
                """
            )
        self._seed_qualifications()

    def db(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _seed_qualifications(self) -> None:
        now = time.time()
        harness = compute_capability_harness_hash()
        with self.db() as db:
            for c in DEFAULT_CAPABILITIES:
                row = db.execute(
                    "SELECT capability FROM capability_qualifications WHERE capability = ?",
                    (c.name,),
                ).fetchone()
                if not row:
                    req_reval = 1 if c.qualification_state != "qualified" else 0
                    exec_mode = "simulation" if req_reval else "live"
                    db.execute(
                        """
                        INSERT INTO capability_qualifications
                        (capability, catalog_state, auth_state, reachability_state,
                         tested_state, qualification_state, execution_mode,
                         requires_revalidation, qualified_harness_hash, provenance_ref, updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            c.name,
                            c.catalog_state,
                            c.auth_state,
                            c.reachability_state,
                            c.tested_state,
                            c.qualification_state,
                            exec_mode,
                            req_reval,
                            harness,
                            "default_seed",
                            now,
                        ),
                    )
            db.commit()

    def record_qualification(
        self,
        capability: str,
        *,
        execution_mode: str = "live",
        catalog_state: str = "cataloged",
        auth_state: str = "authenticated",
        reachability_state: str = "reachable",
        tested_state: str = "tested",
        qualification_state: str = "qualified",
        model: str = "",
        endpoint: str = "",
        adapter_hash: str = "",
        playbook_hash: str = "",
        harness_hash: str = "",
        provenance_ref: str = "",
    ) -> dict[str, Any]:
        """Record capability qualification, enforcing revalidation for synthetic or provenance-free inputs."""
        mode = str(execution_mode or "").strip().lower()
        if mode not in VALID_EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of {sorted(VALID_EXECUTION_MODES)}")
        # Synthetic or provenance-free results require revalidation
        if mode != "live" or not provenance_ref:
            qual_state = "revalidation_required"
            req_reval = 1
        else:
            qual_state = qualification_state
            req_reval = 1 if qual_state in ("revalidation_required", "invalidated", "unqualified") else 0

        eff_harness = harness_hash or compute_capability_harness_hash()
        now = time.time()
        with self.db() as db:
            db.execute(
                """
                INSERT INTO capability_qualifications
                (capability, catalog_state, auth_state, reachability_state,
                 tested_state, qualification_state, execution_mode, requires_revalidation,
                 qualified_model, qualified_endpoint, qualified_adapter_hash,
                 qualified_playbook_hash, qualified_harness_hash, provenance_ref, updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(capability) DO UPDATE SET
                    catalog_state = excluded.catalog_state,
                    auth_state = excluded.auth_state,
                    reachability_state = excluded.reachability_state,
                    tested_state = excluded.tested_state,
                    qualification_state = excluded.qualification_state,
                    execution_mode = excluded.execution_mode,
                    requires_revalidation = excluded.requires_revalidation,
                    qualified_model = excluded.qualified_model,
                    qualified_endpoint = excluded.qualified_endpoint,
                    qualified_adapter_hash = excluded.qualified_adapter_hash,
                    qualified_playbook_hash = excluded.qualified_playbook_hash,
                    qualified_harness_hash = excluded.qualified_harness_hash,
                    provenance_ref = excluded.provenance_ref,
                    updated = excluded.updated
                """,
                (
                    capability,
                    catalog_state,
                    auth_state,
                    reachability_state,
                    tested_state,
                    qual_state,
                    mode,
                    req_reval,
                    model,
                    endpoint,
                    adapter_hash,
                    playbook_hash,
                    eff_harness,
                    provenance_ref,
                    now,
                ),
            )
            db.commit()
        return {
            "capability": capability,
            "catalog_state": catalog_state,
            "auth_state": auth_state,
            "reachability_state": reachability_state,
            "tested_state": tested_state,
            "qualification_state": qual_state,
            "execution_mode": mode,
            "requires_revalidation": bool(req_reval),
            "qualified_model": model,
            "qualified_endpoint": endpoint,
            "qualified_adapter_hash": adapter_hash,
            "qualified_playbook_hash": playbook_hash,
            "qualified_harness_hash": eff_harness,
            "provenance_ref": provenance_ref,
        }

    def invalidate_on_change(
        self,
        capability: str,
        *,
        model: str | None = None,
        endpoint: str | None = None,
        adapter_hash: str | None = None,
        playbook_hash: str | None = None,
        harness_hash: str | None = None,
    ) -> dict[str, Any]:
        """Invalidate applicable qualification when model, endpoint, adapter, playbook, or harness changes."""
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM capability_qualifications WHERE capability = ?",
                (capability,),
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown capability qualification: {capability}")
            reasons: list[str] = []
            if model is not None and row["qualified_model"] and row["qualified_model"] != model:
                reasons.append(f"model_changed:{row['qualified_model']}->{model}")
            if endpoint is not None and row["qualified_endpoint"] and row["qualified_endpoint"] != endpoint:
                reasons.append(f"endpoint_changed:{row['qualified_endpoint']}->{endpoint}")
            if adapter_hash is not None and row["qualified_adapter_hash"] and row["qualified_adapter_hash"] != adapter_hash:
                reasons.append("adapter_changed")
            if playbook_hash is not None and row["qualified_playbook_hash"] and row["qualified_playbook_hash"] != playbook_hash:
                reasons.append("playbook_changed")
            if harness_hash is not None and row["qualified_harness_hash"] and row["qualified_harness_hash"] != harness_hash:
                reasons.append("harness_changed")
            if reasons:
                db.execute(
                    """
                    UPDATE capability_qualifications
                    SET qualification_state = 'invalidated',
                        requires_revalidation = 1,
                        updated = ?
                    WHERE capability = ?
                    """,
                    (time.time(), capability),
                )
                db.commit()
            return {
                "capability": capability,
                "invalidated": bool(reasons),
                "reasons": reasons,
                "qualification_state": "invalidated" if reasons else row["qualification_state"],
                "requires_revalidation": True if reasons else bool(row["requires_revalidation"]),
            }

    def discover(
        self,
        action: str | None = None,
        capability: str | None = None,
        qualification_state: str | None = None,
        execution_kind: str | None = None,
    ) -> list[dict]:
        with self.db() as db:
            q_rows = {
                r["capability"]: dict(r)
                for r in db.execute("SELECT * FROM capability_qualifications").fetchall()
            }
        values: list[dict] = []
        for c in DEFAULT_CAPABILITIES:
            if capability and c.name != capability:
                continue
            if action and action not in c.actions:
                continue
            if execution_kind and c.execution_kind != execution_kind:
                continue
            d = c.to_dict()
            q = q_rows.get(c.name)
            if q:
                d["catalog_state"] = q["catalog_state"]
                d["auth_state"] = q["auth_state"]
                d["reachability_state"] = q["reachability_state"]
                d["tested_state"] = q["tested_state"]
                d["qualification_state"] = q["qualification_state"]
                d["execution_mode"] = q["execution_mode"]
                d["requires_revalidation"] = bool(q["requires_revalidation"])
            else:
                d["requires_revalidation"] = d["qualification_state"] != "qualified"
            if qualification_state and d["qualification_state"] != qualification_state:
                continue
            values.append(d)
        return values

    def handoff(self, capability: str, objective: str, payload: dict | None = None) -> str:
        if not any(c.name == capability for c in DEFAULT_CAPABILITIES):
            raise ValueError(f"Unknown capability: {capability}")
        ident = "cap_" + uuid.uuid4().hex
        now = time.time()
        enriched_payload = dict(payload or {})
        enriched_payload.setdefault("delivery_status", "queued")
        enriched_payload.setdefault("verification_status", "unverified")
        enriched_payload.setdefault("requested_provider", capability.split(".")[0])
        enriched_payload.setdefault("requested_model", "provider_default")
        enriched_payload.setdefault("actual_provider", "")
        enriched_payload.setdefault("actual_model", "unknown")
        with self.db() as db:
            db.execute(
                "INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,?)",
                (ident, capability, objective, "queued", json.dumps(enriched_payload), "{}", now, now),
            )
            db.commit()
        return ident

    def complete(self, handoff_id: str, result: dict) -> None:
        """Complete a handoff delivery.

        Records delivery completion separately from independent answer correctness.
        """
        enriched_res = dict(result or {})
        enriched_res.setdefault("delivery_reliability_pass", True)
        enriched_res.setdefault(
            "answer_correctness_verified",
            bool(enriched_res.get("independently_verified", False)),
        )
        with self.db() as db:
            changed = db.execute(
                "UPDATE handoffs SET state='completed',result=?,updated=? WHERE id=? AND state='queued'",
                (json.dumps(enriched_res), time.time(), handoff_id),
            )
            db.commit()
        if changed.rowcount != 1:
            raise ValueError("Unknown or already completed capability handoff")

    def get(self, handoff_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM handoffs WHERE id=?", (handoff_id,)).fetchone()
        if not row:
            raise KeyError(handoff_id)
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["result"] = json.loads(value["result"])
        return value

    def list_handoffs(self, state: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM handoffs"
        params: list = []
        if state:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY created DESC LIMIT ?"
        params.append(max(1, min(200, int(limit))))
        with self.db() as db:
            rows = db.execute(sql, tuple(params)).fetchall()
        results = []
        for row in rows:
            val = dict(row)
            val["payload"] = json.loads(val["payload"] or "{}")
            val["result"] = json.loads(val["result"] or "{}")
            results.append(val)
        return results

    def _load_empirical_app_stats(self, assistant_db_path: str | Path | None) -> dict[str, dict[str, float]]:
        """Load separate delivery_reliability, answer_correctness, and orchestration_performance metrics."""
        stats: dict[str, dict[str, float]] = {}
        if not assistant_db_path:
            return stats
        db_file = Path(assistant_db_path)
        if not db_file.exists():
            return stats
        try:
            with sqlite3.connect(str(db_file), timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "assistant_cross_app_handoffs" not in tables:
                    return stats
                rows = conn.execute(
                    """SELECT app,
                              COUNT(*) AS total,
                              SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS delivered,
                              SUM(CASE WHEN status='completed' AND COALESCE(verification_state, '')='verified' THEN 1 ELSE 0 END) AS verified_correct,
                              AVG(CASE WHEN status='completed' AND elapsed_seconds IS NOT NULL THEN elapsed_seconds END) AS avg_elapsed
                       FROM assistant_cross_app_handoffs
                       WHERE status IN ('completed', 'failed', 'stopped')
                       GROUP BY app"""
                ).fetchall()
                for r in rows:
                    app_name = str(r["app"] or "")
                    if not app_name:
                        continue
                    total = float(r["total"] or 0)
                    delivered = float(r["delivered"] or 0)
                    verified = float(r["verified_correct"] or 0)
                    stats[app_name] = {
                        "total": total,
                        "passed": delivered,
                        "delivered": delivered,
                        "verified_correct": verified,
                        "delivery_reliability": round(delivered / total, 4) if total > 0 else 0.0,
                        "answer_correctness": round(verified / total, 4) if total > 0 else 0.0,
                        "avg_elapsed": float(r["avg_elapsed"] or 30.0),
                    }
        except Exception:
            return {}
        return stats

    def weight_and_select_worker(
        self,
        task_role: str = "thought",
        quality_preference: str = "balanced",
        assistant_db_path: str | Path | None = None,
        allow_external: bool = False,
        include_unqualified: bool = False,
    ) -> dict[str, Any]:
        """Rank qualified live workers, separating delivery_reliability, answer_correctness, and orchestration_performance."""
        pref = str(quality_preference or "balanced").strip().lower()
        if pref not in QUALITY_WEIGHTS:
            raise ValueError(f"quality_preference must be one of {sorted(QUALITY_WEIGHTS.keys())}")
        role_norm = str(task_role or "thought").strip().lower()
        q_weight, s_weight = QUALITY_WEIGHTS[pref]
        empirical = self._load_empirical_app_stats(assistant_db_path)

        candidates: list[dict[str, Any]] = []
        excluded_unqualified: list[dict[str, Any]] = []
        for worker_key, prof in WORKER_MODEL_PROFILES.items():
            if prof.get("tier") == "external" and not allow_external and role_norm != "external_mind":
                continue
            if role_norm not in prof["roles"] and role_norm != "any":
                continue
            qual_state = prof.get("qualification_state", "unqualified")
            exec_mode = prof.get("execution_mode", "unknown")
            if (qual_state != "qualified" or exec_mode != "live") and not include_unqualified:
                excluded_unqualified.append(
                    {
                        "worker": worker_key,
                        "qualification_state": qual_state,
                        "execution_mode": exec_mode,
                        "requires_revalidation": True,
                    }
                )
                continue

            emp = empirical.get(worker_key, {})
            total = emp.get("total", 0.0)
            delivered = emp.get("delivered", 0.0)
            verified = emp.get("verified_correct", 0.0)
            avg_elapsed = emp.get("avg_elapsed", 0.0)

            prior_q = float(prof["prior_quality"])
            empirical_q = (verified + 4.0 * prior_q) / (total + 4.0)

            prior_s = float(prof["prior_speed"])
            if total > 0 and avg_elapsed > 0:
                observed_speed = max(0.20, min(1.0, 1.0 - (avg_elapsed / 120.0)))
                empirical_s = (total * observed_speed + 2.0 * prior_s) / (total + 2.0)
            else:
                empirical_s = prior_s

            composite = round(q_weight * empirical_q + s_weight * empirical_s, 4)
            candidates.append({
                "worker": worker_key,
                "owner": prof["owner"],
                "tier": prof["tier"],
                "model_family": prof["model_family"],
                "transports": list(prof["transports"]),
                "catalog_state": prof["catalog_state"],
                "auth_state": prof["auth_state"],
                "reachability_state": prof["reachability_state"],
                "tested_state": prof["tested_state"],
                "qualification_state": qual_state,
                "execution_mode": exec_mode,
                "requires_revalidation": qual_state != "qualified",
                "delivery_reliability": round(delivered / total, 4) if total > 0 else None,
                "answer_correctness": round(verified / total, 4) if total > 0 else None,
                "orchestration_performance": {
                    "avg_elapsed_seconds": round(avg_elapsed, 2) if avg_elapsed else None,
                },
                "quality_score": round(empirical_q, 4),
                "speed_score": round(empirical_s, 4),
                "composite_score": composite,
                "empirical_attempts": int(total),
                "empirical_passes": int(verified),
                "avg_elapsed_seconds": round(avg_elapsed, 2) if avg_elapsed else None,
            })

        if allow_external and role_norm in {"grounded_summarization", "structured_extraction", "translation", "code_generation", "critique"}:
            from tools.portal_policy import PortalPolicy, DEFAULT_DB
            portal_policy = PortalPolicy(assistant_db_path or DEFAULT_DB)
            with portal_policy.db() as db:
                portal_ids = [r[0] for r in db.execute("SELECT portal_id FROM portal_profiles")]
            for portal_id in portal_ids:
                evidence = portal_policy.eligibility(portal_id, role_norm)
                if evidence["eligible"]:
                    candidates.append({"worker": "ExternalMind:" + portal_id, "tier": "external",
                                       "quality_score": 0.8, "composite_score": 0.8,
                                       "qualification": evidence, "required_capability": role_norm,
                                       "execution_interface": "tools.portal_transport.invoke_portal"})
        candidates.sort(key=lambda c: (c["composite_score"], c["quality_score"]), reverse=True)
        selected = candidates[0] if candidates else None

        agy_score = next((c["composite_score"] for c in candidates if c["worker"] == "Antigravity.exe"), 0.0)
        cdx_score = next((c["composite_score"] for c in candidates if c["worker"] == "Codex.exe"), 0.0)
        if role_norm == "implement":
            recommended_mode = (
                "codex_thought_antigravity_implement"
                if agy_score >= cdx_score
                else "antigravity_thought_codex_implement"
            )
        else:
            recommended_mode = (
                "antigravity_thought_codex_implement"
                if agy_score > cdx_score
                else "codex_thought_antigravity_implement"
            )

        return {
            "ok": selected is not None,
            "task_role": role_norm,
            "quality_preference": pref,
            "weights": {"quality": q_weight, "speed": s_weight},
            "selected_worker": selected,
            "recommended_dual_mode": recommended_mode,
            "candidates": candidates,
            "excluded_unqualified": excluded_unqualified,
        }

    def swarm_gemini_fast(
        self,
        prompts: list[str],
        system: str = "Reply concisely and accurately based only on supplied evidence.",
        max_workers: int = 4,
        synthesize: bool = True,
        oracle_fn: Callable[[str, str, str], str | dict[str, Any] | None] | None = None,
        *,
        execution_mode: str = "live",
        allow_fallback: bool = True,
    ) -> dict[str, Any]:
        """Fan out parallel subtasks (max 4 concurrent workers) with honest provider/model attribution."""
        mode = str(execution_mode or "").strip().lower()
        if mode not in VALID_EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of {sorted(VALID_EXECUTION_MODES)}")
        if not isinstance(prompts, list) or not prompts or len(prompts) > 12:
            raise ValueError("prompts must be a non-empty list of 1-12 subtask strings")
        cleaned_prompts = [str(p).strip() for p in prompts if str(p).strip()]
        if not cleaned_prompts:
            raise ValueError("prompts must contain at least one non-empty prompt")

        # Enforce the 4-worker concurrency ceiling
        bounded_workers = max(1, min(int(max_workers or 4), len(cleaned_prompts), 4))
        ladder = GEMINI_FAST_SWARM_MODELS

        def _default_call_with_provenance(sys_prompt: str, user_prompt: str, model_name: str) -> dict[str, Any]:
            from tools.oracle_providers import GoogleGeminiOracle, UnifiedOracleHub
            gem = GoogleGeminiOracle()
            if gem.is_available():
                out = gem.generate(sys_prompt, user_prompt, model=model_name, max_retries=1)
                if out:
                    return {
                        "reply": str(out),
                        "requested_provider": "gemini",
                        "requested_model": model_name,
                        "requested_interface": "rest_api",
                        "actual_provider": "gemini",
                        "actual_model": str(gem.last_model_used or model_name),
                        "actual_interface": "rest_api",
                        "fallback_occurred": False,
                    }
            if not allow_fallback:
                raise RuntimeError(
                    f"Gemini model '{model_name}' unavailable and fallback is disabled (allow_fallback=False)."
                )
            hub = UnifiedOracleHub()
            prov_res = hub.query_with_provenance(
                user_prompt,
                system=sys_prompt,
                preferred="local",
                execution_mode=mode,
                allow_fallback=True,
            )
            return {
                "reply": str(prov_res.get("response") or ""),
                "requested_provider": "gemini",
                "requested_model": model_name,
                "requested_interface": "rest_api",
                # Honest attribution: never attribute a local fallback to Gemini
                "actual_provider": str(prov_res.get("actual_provider") or "local_ollama"),
                "actual_model": str(prov_res.get("actual_model") or "unknown"),
                "actual_interface": str(prov_res.get("actual_interface") or "ollama_http"),
                "fallback_occurred": True,
            }

        def _run_one(idx: int, sub_prompt: str) -> dict[str, Any]:
            assigned_model = ladder[idx % len(ladder)]
            t0 = time.time()
            try:
                if oracle_fn is not None:
                    raw_out = oracle_fn(system, sub_prompt, assigned_model)
                    if isinstance(raw_out, dict):
                        from tools.oracle_providers import UnifiedOracleHub
                        UnifiedOracleHub.validate_provider_attribution(raw_out)
                        reply = str(raw_out.get("reply") or raw_out.get("response") or "")
                        act_prov = str(raw_out.get("actual_provider") or "gemini")
                        act_mod = str(raw_out.get("actual_model") or assigned_model)
                        act_iface = str(raw_out.get("actual_interface") or "rest_api")
                        fb = bool(raw_out.get("fallback_occurred", act_prov != "gemini"))
                    else:
                        reply = str(raw_out or "")
                        act_prov = "gemini"
                        act_mod = assigned_model
                        act_iface = "rest_api"
                        fb = False
                else:
                    prov = _default_call_with_provenance(system, sub_prompt, assigned_model)
                    reply = prov["reply"]
                    act_prov = prov["actual_provider"]
                    act_mod = prov["actual_model"]
                    act_iface = prov["actual_interface"]
                    fb = prov["fallback_occurred"]

                elapsed_ms = int((time.time() - t0) * 1000)
                ok = bool(reply and reply.strip())
                return {
                    "index": idx,
                    "model": assigned_model,
                    "requested_provider": "gemini",
                    "requested_model": assigned_model,
                    "requested_interface": "rest_api",
                    "actual_provider": act_prov if ok else "none",
                    "actual_model": act_mod if ok else "unknown",
                    "actual_interface": act_iface if ok else "none",
                    "fallback_occurred": fb,
                    "ok": ok,
                    "elapsed_ms": elapsed_ms,
                    "prompt": sub_prompt,
                    "reply": reply.strip() if ok else "",
                    "error": "" if ok else "Empty response from worker",
                }
            except Exception as exc:
                return {
                    "index": idx,
                    "model": assigned_model,
                    "requested_provider": "gemini",
                    "requested_model": assigned_model,
                    "requested_interface": "rest_api",
                    "actual_provider": "none",
                    "actual_model": "unknown",
                    "actual_interface": "none",
                    "fallback_occurred": False,
                    "ok": False,
                    "elapsed_ms": int((time.time() - t0) * 1000),
                    "prompt": sub_prompt,
                    "reply": "",
                    "error": str(exc),
                }

        t_start = time.time()
        results_by_idx: dict[int, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=bounded_workers) as pool:
            futures = {pool.submit(_run_one, i, p): i for i, p in enumerate(cleaned_prompts)}
            for fut in as_completed(futures):
                res_item = fut.result()
                results_by_idx[res_item["index"]] = res_item

        ordered_results = [results_by_idx[i] for i in range(len(cleaned_prompts))]
        succeeded = [r for r in ordered_results if r["ok"]]

        synthesis_text = ""
        if synthesize and succeeded:
            combined_inputs = "\n\n".join(
                f"[Worker {r['index'] + 1} (requested={r['requested_model']}, actual={r['actual_provider']}:{r['actual_model']})]:\n{r['reply']}"
                for r in succeeded
            )
            synth_prompt = (
                "Synthesize the following parallel worker findings into one concise summary, preserving disagreements:\n\n"
                + combined_inputs
            )
            try:
                if oracle_fn is not None:
                    raw_s = oracle_fn(system, synth_prompt, ladder[0])
                    synthesis_text = str(raw_s.get("reply") if isinstance(raw_s, dict) else (raw_s or "")).strip()
                else:
                    s_prov = _default_call_with_provenance(system, synth_prompt, ladder[0])
                    synthesis_text = str(s_prov.get("reply") or "").strip()
            except Exception:
                synthesis_text = combined_inputs

        return {
            "ok": len(succeeded) > 0,
            "execution_mode": mode,
            "worker_count": len(cleaned_prompts),
            "succeeded_count": len(succeeded),
            "max_parallel_workers": bounded_workers,
            "models_used": sorted({r["model"] for r in ordered_results}),
            "actual_providers_used": sorted({r["actual_provider"] for r in ordered_results if r["ok"]}),
            "fallback_count": sum(1 for r in ordered_results if r.get("fallback_occurred")),
            "elapsed_ms": int((time.time() - t_start) * 1000),
            "results": ordered_results,
            "synthesis": synthesis_text,
        }

    @staticmethod
    def resolve_dual_mode(mode: str = "codex_thought_antigravity_implement") -> dict:
        key = MODE_ALIASES.get(str(mode or "").strip().lower())
        if not key or key not in DUAL_ROLE_MODES:
            raise ValueError(
                f"Unsupported dual capability mode: {mode!r}. "
                f"Expected one of {sorted(DUAL_ROLE_MODES.keys())} (or alias 'vice_versa')."
            )
        return dict(DUAL_ROLE_MODES[key])

    @classmethod
    def swap_roles(cls, current_mode: str = "codex_thought_antigravity_implement") -> dict:
        resolved = cls.resolve_dual_mode(current_mode)
        if resolved["mode"] == "codex_thought_antigravity_implement":
            return dict(DUAL_ROLE_MODES["antigravity_thought_codex_implement"])
        return dict(DUAL_ROLE_MODES["codex_thought_antigravity_implement"])

    def plan_thought_and_implement(
        self,
        objective: str,
        mode: str = "codex_thought_antigravity_implement",
        payload: dict | None = None,
        quality_preference: str = "balanced",
        assistant_db_path: str | Path | None = None,
    ) -> dict:
        """Create a chained 2-stage capability pipeline: Thought -> Implement (or vice versa / auto-weighted)."""
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be a non-empty string")
        weighting = None
        if str(mode or "").strip().lower() == "auto":
            weighting = self.weight_and_select_worker(
                task_role="thought",
                quality_preference=quality_preference,
                assistant_db_path=assistant_db_path,
            )
            cfg = self.resolve_dual_mode(weighting["recommended_dual_mode"])
        else:
            cfg = self.resolve_dual_mode(mode)
        base_payload = dict(payload or {})
        pipeline_id = "pipe_" + uuid.uuid4().hex[:16]

        thought_payload = {
            **base_payload,
            "pipeline_id": pipeline_id,
            "stage": "thought",
            "mode": cfg["mode"],
            "quality_preference": quality_preference,
            "assigned_app": cfg["thought_app"],
            "downstream_app": cfg["implement_app"],
            "downstream_capability": cfg["implement_capability"],
        }
        thought_hid = self.handoff(
            cfg["thought_capability"],
            f"[THOUGHT / ARCHITECTURE ({cfg['thought_app']})] {objective.strip()}",
            thought_payload,
        )

        implement_payload = {
            **base_payload,
            "pipeline_id": pipeline_id,
            "stage": "implement",
            "mode": cfg["mode"],
            "quality_preference": quality_preference,
            "assigned_app": cfg["implement_app"],
            "upstream_app": cfg["thought_app"],
            "upstream_capability": cfg["thought_capability"],
            "depends_on_handoff_id": thought_hid,
        }
        implement_hid = self.handoff(
            cfg["implement_capability"],
            f"[IMPLEMENT / VERIFY ({cfg['implement_app']})] {objective.strip()}",
            implement_payload,
        )

        out = {
            "ok": True,
            "pipeline_id": pipeline_id,
            "mode": cfg["mode"],
            "quality_preference": quality_preference,
            "description": cfg["description"],
            "objective": objective.strip(),
            "stages": [
                {
                    "stage": "thought",
                    "handoff_id": thought_hid,
                    "capability": cfg["thought_capability"],
                    "app": cfg["thought_app"],
                    "owner": cfg["thought_owner"],
                },
                {
                    "stage": "implement",
                    "handoff_id": implement_hid,
                    "capability": cfg["implement_capability"],
                    "app": cfg["implement_app"],
                    "owner": cfg["implement_owner"],
                    "depends_on": thought_hid,
                },
            ],
        }
        if weighting is not None:
            out["weighting"] = weighting
        return out
