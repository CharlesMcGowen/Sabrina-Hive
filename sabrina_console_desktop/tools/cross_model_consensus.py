#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/cross_model_consensus.py
==============================
EXPERIMENTAL & UNAPPROVED: Multi-Model Consensus & Audited Workflow Bridge.

STATUS: EXPERIMENTAL (NOT APPROVED FOR PRODUCTION OR AUTONOMOUS DEPLOYMENT).
This module uses the canonical WorkflowStore controller, transport-neutral evidence
records (api / cli / desktop), and the isolated Docker LabVerifier.

Key Architectural Guarantees:
1. Transport-Neutral Provenance:
   Explicitly distinguishes 'desktop', 'cli', and 'api' provenance in workflow_evidence.
   API calls record actual provider, model, submitted prompt hash, raw response reference,
   and timestamps. Does not manufacture desktop typing/press/snapshot events for API calls.
2. Bounded Corrective Loop:
   If initial model output is malformed (e.g. unescaped quotes or JSON decode error),
   executes exactly ONE bounded corrective request to the same provider, preserving the
   original raw response and linking the correction via parent_evidence_id.
3. Isolated Trusted Verification:
   No hardcoded solutions in live workflow. Only WorkflowWorker invokes the isolated
   Docker LabVerifier (python:3.11-slim on ego@ego.local) on the artifact actually
   retrieved from the model.
4. Honest Blockers:
   Invalid or missing provider credentials (e.g. HTTP 401) produce explicit blockers
   in the workflow attempt ledger without simulation or masking.
"""

from __future__ import annotations

import os
import sys
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

# Workspace setup
WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

# Module governance constants
EXPERIMENTAL: bool = True
APPROVAL_STATE: str = "UNAPPROVED"
PLAYBOOK_VERSION: str = "experimental-proposed-artifact-v1"

# ANSI styling
class C:
    G  = "\033[92m"; Y  = "\033[93m"; R  = "\033[91m"
    B  = "\033[94m"; M  = "\033[35m"; CN = "\033[96m"
    BD = "\033[1m";  DM = "\033[2m";  X  = "\033[0m"

if sys.platform == "win32":
    os.system("")

# Import canonical workflow and verifier interfaces
from tools.sabrina_workflow import (
    WorkflowStore,
    WorkflowWorker,
    DEFAULT_DB,
    harness_hash,
    visible_artifact,
)
from tools.sabrina_workflow_verifier import (
    LabVerifier,
    public_contract,
    digest,
)
from tools.secret_store import get_secret
from tools.oracle_providers import LocalOllamaOracle, AnthropicClaudeOracle


# ─────────────────────────────────────────────────────────────────────────────
# 1. Genuine Provider Call Helpers (Explicit API Provenance)
# ─────────────────────────────────────────────────────────────────────────────

def call_local_ollama_api(prompt: str, model_name: Optional[str] = None) -> Dict[str, Any]:
    """Execute live HTTP API inference on the local Ollama node."""
    oracle = LocalOllamaOracle()
    if not oracle.is_available():
        return {
            "ok": False,
            "provider": "ollama",
            "model": model_name or "unavailable",
            "error": "No reachable Ollama nodes",
        }

    try:
        system = (
            "You are an expert Python engineer. Return ONLY a single valid JSON object. "
            "Ensure all strings inside the JSON are validly escaped."
        )
        text, used_model = oracle.generate(system, prompt, tier="fast")
        if not text:
            return {
                "ok": False,
                "provider": "ollama",
                "model": used_model,
                "error": "Empty response from Ollama",
            }
        return {
            "ok": True,
            "provider": "ollama",
            "model": used_model,
            "raw_text": text.strip(),
            "request_id": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "ollama",
            "model": model_name or "unknown",
            "error": str(exc),
        }


def call_anthropic_api(prompt: str) -> Dict[str, Any]:
    """Execute live call to Anthropic API. Missing/invalid keys produce explicit blockers."""
    key = get_secret("ANTHROPIC_API_KEY")
    if not key:
        return {
            "ok": False,
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "error": "ANTHROPIC_API_KEY not configured",
        }

    oracle = AnthropicClaudeOracle(api_key=key)
    try:
        system = "Return ONLY valid JSON matching the task contract."
        response = oracle.generate(system=system, prompt=prompt)
        if response is None:
            return {
                "ok": False,
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "error": "Anthropic API rejected request or balance exhausted",
            }
        return {
            "ok": True,
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "raw_text": response.strip(),
            "request_id": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "error": str(exc),
        }


def call_openai_api(prompt: str) -> Dict[str, Any]:
    """Execute live call to OpenAI API. Missing/invalid keys produce explicit blockers."""
    key = get_secret("OPENAI_API_KEY")
    if not key:
        return {
            "ok": False,
            "provider": "openai",
            "model": os.environ.get("SABRINA_OPENAI_AGENT_MODEL", "gpt-4o"),
            "error": "OPENAI_API_KEY not configured",
        }

    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=60)
        model = os.environ.get("SABRINA_OPENAI_AGENT_MODEL", "gpt-4o")
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "Return ONLY valid JSON matching the task contract."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content or ""
        return {
            "ok": True,
            "provider": "openai",
            "model": model,
            "raw_text": content.strip(),
            "request_id": completion.id,
        }
    except Exception as exc:
        return {
            "ok": False,
            "provider": "openai",
            "model": os.environ.get("SABRINA_OPENAI_AGENT_MODEL", "gpt-4o"),
            "error": str(exc),
        }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Transport-Neutral Audited Adapter (With Bounded Corrective Request)
# ─────────────────────────────────────────────────────────────────────────────

class AuditedTransportAdapter:
    """
    Transport-neutral adapter that records genuine evidence into workflow_evidence
    with explicit 'api', 'cli', or 'desktop' provenance.

    When output is malformed, executes exactly ONE bounded corrective request to
    the same provider and links the correction via parent_evidence_id.
    """

    def __init__(self, transport: str = "api", provider: str = "ollama", model: Optional[str] = None):
        self.transport = transport.lower()
        self.provider = provider.lower()
        self.model = model

    def advance(self, store: WorkflowStore, attempt: Dict[str, Any]) -> None:
        aid = attempt["id"]
        tid = attempt["task_id"]
        with store.connect() as db:
            row = db.execute("SELECT objective_id FROM workflow_tasks WHERE id=?", (tid,)).fetchone()
            oid = row["objective_id"] if row else attempt.get("objective_id", "")
        marker = attempt["marker"]
        expected_prompt = attempt["expected_prompt"]
        prompt_hash = attempt["prompt_hash"]

        if self.transport == "desktop":
            # Native desktop transport requires actual broker execution
            from tools.sabrina_workflow import DesktopAssignmentAdapter
            return DesktopAssignmentAdapter().advance(store, attempt)

        now = store.clock()
        print(f"  {C.M}[PROVIDER DISPATCH: {self.transport.upper()}]{C.X} Querying {self.provider}...")

        # 1. Primary provider invocation
        call_res = self._dispatch_call(expected_prompt)
        t_complete = store.clock()

        ev1_id = f"ev_{uuid.uuid4().hex[:12]}"
        raw_text_1 = call_res.get("raw_text", "")
        error_1 = call_res.get("error")
        status_1 = "received" if call_res["ok"] else "failed"

        with store.connect() as db:
            db.execute("""
                INSERT INTO workflow_evidence(
                    id, objective_id, attempt_id, transport, provider, model,
                    request_id, prompt_hash, raw_response, raw_response_hash,
                    delivery_status, error, parent_evidence_id, created, completed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """, (
                ev1_id, oid, aid, self.transport, call_res.get("provider", self.provider),
                call_res.get("model", "unknown"), call_res.get("request_id"),
                prompt_hash, raw_text_1, digest(raw_text_1), status_1, error_1, now, t_complete
            ))
            db.execute("UPDATE workflow_attempts SET delivery=? WHERE id=?",
                       (f"failed_via_{self.transport}" if not call_res["ok"] else f"observed_via_{self.transport}", aid))

        if not call_res["ok"]:
            print(f"  {C.R}✗ Provider Error Recorded:{C.X} {error_1}")
            return  # WorkflowWorker will inspect evidence, see failure, and block

        print(f"  {C.G}✓ Primary Output Received:{C.X} {len(raw_text_1)} chars from {call_res.get('model')}")

        # 2. Check if primary response contains a valid marker-matched artifact
        needs_correction = False
        parse_error = ""
        try:
            visible_artifact(raw_text_1, marker)
            print(f"  {C.G}✓ Valid Artifact Pre-check Passed.{C.X}")
        except ValueError as ve:
            needs_correction = True
            parse_error = str(ve)

        # 3. If malformed output, execute ONE bounded corrective request to the same provider
        if needs_correction:
            print(f"  {C.Y}• Output Malformed ({parse_error}). Dispatching ONE bounded corrective request...{C.X}")
            corrective_prompt = (
                f"Your previous response for task marker '{marker}' could not be parsed: {parse_error}\n\n"
                f"Previous output was:\n```\n{raw_text_1[:2000]}\n```\n\n"
                f"Please correct the output. Return ONLY a single valid JSON object matching the contract:\n"
                f"{{\"task_marker\": \"{marker}\", \"complete\": true, \"files\": {{\"solution.py\": \"<valid Python code>\"}}}}\n"
                "CRITICAL: Escape all double-quotes inside the Python code string (use \\\"). "
                "Do not include conversational text or markdown code blocks outside the JSON."
            )

            t2_start = store.clock()
            call_res_corr = self._dispatch_call(corrective_prompt)
            t2_complete = store.clock()

            ev2_id = f"ev_{uuid.uuid4().hex[:12]}"
            raw_text_2 = call_res_corr.get("raw_text", "")
            error_2 = call_res_corr.get("error")
            status_2 = "received" if call_res_corr["ok"] else "failed"

            with store.connect() as db:
                db.execute("""
                    INSERT INTO workflow_evidence(
                        id, objective_id, attempt_id, transport, provider, model,
                        request_id, prompt_hash, raw_response, raw_response_hash,
                        delivery_status, error, parent_evidence_id, created, completed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ev2_id, oid, aid, self.transport, call_res_corr.get("provider", self.provider),
                    call_res_corr.get("model", "unknown"), call_res_corr.get("request_id"),
                    digest(corrective_prompt), raw_text_2, digest(raw_text_2), status_2, error_2, ev1_id, t2_start, t2_complete
                ))
                store._event(db, oid, "bounded_correction_dispatched", {
                    "original_evidence_id": ev1_id,
                    "corrective_evidence_id": ev2_id,
                    "reason": parse_error,
                    "provider": self.provider,
                }, attempt["task_id"], aid)

            if call_res_corr["ok"]:
                print(f"  {C.G}✓ Corrected Output Received:{C.X} {len(raw_text_2)} chars (Linked to {ev1_id})")
            else:
                print(f"  {C.R}✗ Corrective Request Failed:{C.X} {error_2}")

    def _dispatch_call(self, prompt: str) -> Dict[str, Any]:
        if self.provider == "ollama":
            return call_local_ollama_api(prompt, model_name=self.model)
        elif self.provider == "anthropic":
            return call_anthropic_api(prompt)
        elif self.provider == "openai":
            return call_openai_api(prompt)
        return {"ok": False, "provider": self.provider, "error": f"Unsupported provider: {self.provider}"}


# ─────────────────────────────────────────────────────────────────────────────
# 3. Canonical Workflow Execution (WorkflowWorker + LabVerifier)
# ─────────────────────────────────────────────────────────────────────────────

def run_live_workflow_task(
    transport: str = "api",
    provider: str = "ollama",
    suite_id: str = "duration-v1",
    db_path: Path = DEFAULT_DB,
) -> Dict[str, Any]:
    """
    Executes one live workflow step using WorkflowWorker, AuditedTransportAdapter,
    and LabVerifier. Verifies ONLY the artifact actually retrieved from the provider.
    """
    store = WorkflowStore(db_path)
    contract = public_contract(suite_id)
    suite_sha = contract["suite_sha256"]

    print(f"\n{C.BD}══════════════════════════════════════════════════════════════════════{C.X}")
    print(f"{C.BD}CANONICAL WORKFLOW STEP — {APPROVAL_STATE} (EXPERIMENTAL){C.X}")
    print(f"Transport: {transport.upper()} | Provider: {provider}")
    print(f"Suite:     {suite_id} ({suite_sha[:16]}...)")
    print(f"Database:  {db_path}")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print(f"{C.BD}══════════════════════════════════════════════════════════════════════{C.X}")

    # 1. Create experimental objective
    goal = f"Live delegation of {suite_id} via {transport}:{provider} with isolated Docker LabVerifier."
    obj = store.create(goal=goal, suite_id=suite_id, experimental=True)
    oid = obj["objective"]["id"]
    print(f"{C.CN}[WORKFLOW STORE]{C.X} Created objective `{oid}`")

    # 2. Initialize canonical worker with transport-neutral adapter and LabVerifier
    adapter = AuditedTransportAdapter(transport=transport, provider=provider)
    verifier = LabVerifier()
    worker = WorkflowWorker(store=store, adapter=adapter, verifier=verifier)

    # 3. Execute canonical step (advance -> collect_and_verify)
    print(f"\n{C.M}[WORKFLOW WORKER]{C.X} Advancing attempt and executing independent verification...")
    status = worker.step(oid)

    obj_state = status["objective"]["state"]
    attempts = status.get("attempts", [])
    latest_attempt = attempts[-1] if attempts else {}
    aid = latest_attempt.get("id", "none")
    att_state = latest_attempt.get("state", "unknown")
    failure_kind = latest_attempt.get("failure_kind")
    verifications = latest_attempt.get("verifications", [])

    # Fetch stored evidence records
    with store.connect() as db:
        evidence_rows = [
            dict(r) for r in db.execute(
                "SELECT id, transport, provider, model, delivery_status, error, parent_evidence_id, created, completed "
                "FROM workflow_evidence WHERE attempt_id=? ORDER BY created", (aid,)
            ).fetchall()
        ]

    print(f"\n{C.BD}══════════════════════════════════════════════════════════════════════{C.X}")
    print(f"WORKFLOW RESULT: Objective={obj_state} | Attempt {aid[:8]}={att_state}")
    if failure_kind:
        print(f"Failure Kind:    {failure_kind}")
    print(f"Evidence Records: {len(evidence_rows)}")
    for ev in evidence_rows:
        parent_str = f" (parent: {ev['parent_evidence_id']})" if ev.get("parent_evidence_id") else ""
        print(f"  - [{ev['transport']}] {ev['provider']}:{ev['model']} -> {ev['delivery_status']}{parent_str}")
    if verifications:
        latest_verdict = json.loads(verifications[-1]["verdict"])
        print(f"LabVerifier:     status={latest_verdict.get('status')}, isolation={latest_verdict.get('isolation')}")
        checks = latest_verdict.get("checks", [])
        for c in checks:
            print(f"  Check '{c.get('id')}': passed={c.get('passed')}")
    print(f"{C.BD}══════════════════════════════════════════════════════════════════════{C.X}")

    # Generate genuine delegated-task audit trace
    trace = {
        "experimental": True,
        "approval_state": APPROVAL_STATE,
        "playbook_version": PLAYBOOK_VERSION,
        "objective_id": oid,
        "attempt_id": aid,
        "transport": transport,
        "provider": provider,
        "suite_id": suite_id,
        "suite_sha256": suite_sha,
        "harness_hash": harness_hash(),
        "workflow_attempt": {
            "id": aid,
            "state": att_state,
            "failure_kind": failure_kind,
            "delivery": latest_attempt.get("delivery"),
            "artifact_path": latest_attempt.get("artifact_path"),
            "revision_hash": latest_attempt.get("revision_hash"),
            "correction": latest_attempt.get("correction"),
        },
        "evidence_chain": evidence_rows,
        "verifications": verifications,
        "objective_final_state": obj_state,
    }

    out_dir = WORKSPACE_ROOT / "artifacts" / "consensus_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_path = out_dir / f"live_trace_{aid}.json"
    with open(trace_path, "w", encoding="utf-8") as f:
        json.dump(trace, f, indent=2)
    print(f"Audited Trace Saved: {trace_path}")

    return trace


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sabrina Experimental Cross-Model Workflow Task")
    parser.add_argument("--transport", choices=["api", "cli", "desktop"], default="api", help="Evidence transport mechanism")
    parser.add_argument("--provider", choices=["ollama", "anthropic", "openai"], default="ollama", help="Target model provider")
    parser.add_argument("--suite", default="duration-v1", help="Target verification suite")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Path to assistant SQLite database")
    args = parser.parse_args()

    run_live_workflow_task(
        transport=args.transport,
        provider=args.provider,
        suite_id=args.suite,
        db_path=Path(args.db),
    )


if __name__ == "__main__":
    main()
