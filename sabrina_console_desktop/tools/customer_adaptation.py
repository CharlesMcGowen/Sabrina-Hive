"""Bounded adaptation trigger for repeated customer-communication failures.

Only normalized failure signals are persisted. Research uses the frozen
synthetic suite and has no customer text, browser, messaging, training, weight
editing, or promotion authority.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tools.customer_model_research import (
    load_eval_suite,
    request_cloud_review,
    research_recommendation,
    run_evaluation,
)
from tools.customer_prompt_lab import run_prompt_candidate_experiment
from tools.messaging_store import ROOT


DEFAULT_DB_PATH = ROOT / "data/customer_adaptation.sqlite3"
DEFAULT_OUTPUT_ROOT = ROOT / "artifacts/customer_model_research/triggers"
SIGNAL_CLASSIFICATION_VERSION = 2


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_failure_signal(outcome: dict[str, Any]) -> dict[str, Any] | None:
    decision = outcome.get("decision") or {}
    reason = str(decision.get("reason") or outcome.get("error") or "")
    if "Customer decision rejected:" in reason:
        code = "customer_decision_rejected"
        detail = reason.split("Customer decision rejected:", 1)[1].strip()[:240]
    elif (
        outcome.get("status") == "model_retry"
        and outcome.get("phase") == "classify"
        and outcome.get("error_source") == "customer_decision_model"
        and str(outcome.get("error_type") or "").strip()
    ):
        code = "model_runtime_failure"
        detail = "Customer decision model transport failed: " + str(outcome["error_type"])[:160]
    else:
        return None
    fingerprint = hashlib.sha256(
        f"v{SIGNAL_CLASSIFICATION_VERSION}:{code}:{detail}".encode("utf-8")
    ).hexdigest()
    return {
        "classification_version": SIGNAL_CLASSIFICATION_VERSION,
        "code": code,
        "detail": detail,
        "fingerprint": fingerprint,
        "customer_text_included": False,
    }


class AdaptationStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals(
              engagement TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              code TEXT NOT NULL,
              detail TEXT NOT NULL,
              occurrences INTEGER NOT NULL,
              research_cycles INTEGER NOT NULL,
              first_seen TEXT NOT NULL,
              last_seen TEXT NOT NULL,
              classification_version INTEGER NOT NULL DEFAULT 2,
              valid INTEGER NOT NULL DEFAULT 1,
              PRIMARY KEY(engagement,fingerprint)
            );
            CREATE TABLE IF NOT EXISTS research_runs(
              id TEXT PRIMARY KEY,
              engagement TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              artifact TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(signals)")}
        if "classification_version" not in columns:
            self.db.execute("ALTER TABLE signals ADD COLUMN classification_version INTEGER NOT NULL DEFAULT 1")
        if "valid" not in columns:
            self.db.execute("ALTER TABLE signals ADD COLUMN valid INTEGER NOT NULL DEFAULT 1")
        # Version 1 runtime signals used keyword matching and could confuse browser or delivery
        # failures with model failures. They are retained for audit but cannot trigger research.
        self.db.execute(
            "UPDATE signals SET valid=0 WHERE classification_version=1 AND code='model_runtime_failure'"
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def record(self, engagement: str, signal: dict[str, Any]) -> dict[str, Any]:
        stamp = utc_now()
        with self.db:
            self.db.execute(
                """INSERT INTO signals(
                     engagement,fingerprint,code,detail,occurrences,research_cycles,first_seen,last_seen,
                     classification_version,valid
                   ) VALUES(?,?,?,?,1,0,?,?,?,1)
                   ON CONFLICT(engagement,fingerprint) DO UPDATE SET
                     occurrences=occurrences+1,last_seen=excluded.last_seen,valid=1""",
                (
                    engagement,
                    signal["fingerprint"],
                    signal["code"],
                    signal["detail"],
                    stamp,
                    stamp,
                    int(signal.get("classification_version", SIGNAL_CLASSIFICATION_VERSION)),
                ),
            )
        return dict(self.db.execute(
            "SELECT * FROM signals WHERE engagement=? AND fingerprint=?",
            (engagement, signal["fingerprint"]),
        ).fetchone())

    def reserve_cycle(self, engagement: str, fingerprint: str, *, threshold: int, maximum: int) -> bool:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT occurrences,research_cycles FROM signals WHERE engagement=? AND fingerprint=? AND valid=1",
                (engagement, fingerprint),
            ).fetchone()
            allowed = bool(row and row["occurrences"] >= threshold and row["research_cycles"] < maximum)
            if allowed:
                self.db.execute(
                    "UPDATE signals SET research_cycles=research_cycles+1 WHERE engagement=? AND fingerprint=?",
                    (engagement, fingerprint),
                )
            self.db.commit()
            return allowed
        except Exception:
            self.db.rollback()
            raise

    def save_run(self, run_id: str, engagement: str, fingerprint: str, artifact: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO research_runs VALUES(?,?,?,?,?)",
                (run_id, engagement, fingerprint, artifact, utc_now()),
            )


def handle_customer_adaptation(
    outcome: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    output_root: str | Path | None = None,
    evaluator: Callable[[], dict[str, Any]] = run_evaluation,
    cloud_reviewer: Callable[[dict[str, Any]], dict[str, Any]] = request_cloud_review,
    activation_probe: Callable[[], dict[str, Any]] | None = None,
    prompt_experiment: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    signal = classify_failure_signal(outcome)
    if signal is None:
        return {"status": "not_applicable", "customer_text_included": False}
    suite = load_eval_suite()
    engagement = suite["engagement_id"]
    threshold = int(suite.get("repeated_failure_threshold", 2))
    maximum = int(suite.get("max_research_cycles", 2))
    store = AdaptationStore(db_path)
    try:
        row = store.record(engagement, signal)
        if not store.reserve_cycle(engagement, signal["fingerprint"], threshold=threshold, maximum=maximum):
            return {
                "status": "signal_recorded",
                "signal": signal,
                "occurrences": row["occurrences"],
                "research_cycles": row["research_cycles"],
                "threshold": threshold,
                "customer_text_included": False,
            }
        run_id = str(uuid.uuid4())
        result: dict[str, Any] = {
            "version": 1,
            "id": run_id,
            "created_at": utc_now(),
            "engagement": engagement,
            "signal": signal,
            "occurrences": row["occurrences"],
            "synthetic_suite_only": True,
            "customer_text_included": False,
            "browser_access": False,
            "messaging_access": False,
            "training_steps": 0,
            "weight_edits": 0,
            "promotion_performed": False,
        }
        try:
            report = evaluator()
            recommendation = research_recommendation(report)
            result["evaluation"] = report
            result["recommendation"] = recommendation
            use_cloud_review = (
                os.environ.get("SABRINA_CUSTOMER_USE_CLOUD_REVIEW") == "1"
                or os.environ.get("SABRINA_CUSTOMER_USE_ASTRA") == "1"
            )
            if use_cloud_review:
                try:
                    result["cloud_review"] = cloud_reviewer(report)
                except Exception as error:
                    result["cloud_review"] = {"status": "unavailable", "error": str(error)[:300], "analysis_only": True}
            else:
                result["cloud_review"] = {"status": "not_requested", "analysis_only": True}
            prompt_result = None
            if recommendation["action"] == "prompt_experiment":
                try:
                    prompt_result = (prompt_experiment or run_prompt_candidate_experiment)(report)
                    result["prompt_experiment"] = prompt_result
                except Exception as error:
                    prompt_result = {"status": "unavailable", "error": str(error)[:300], "eligible_for_promotion": False}
                    result["prompt_experiment"] = prompt_result
            activation_fallback = bool(
                recommendation.get("activation_inspection_if_prompt_fails")
                and not (prompt_result or {}).get("eligible_for_promotion")
            )
            if activation_fallback:
                if suite.get("activation_observation_enabled") and os.environ.get("SABRINA_CUSTOMER_ACTIVATION_PROBE") == "1":
                    try:
                        if activation_probe is None:
                            from tools.customer_activation_probe import run_probe
                            activation_probe = run_probe
                        result["activation_observation"] = activation_probe()
                    except Exception as error:
                        result["activation_observation"] = {"status": "unavailable", "error": str(error)[:300]}
                else:
                    result["activation_observation"] = {
                        "status": "gated",
                        "reason": "Prompt candidate did not clear promotion gates. Activation observation requires both policy enablement and an explicit runtime flag.",
                    }
            result["status"] = "research_complete"
        except Exception as error:
            result["status"] = "research_failed"
            result["error"] = str(error)[:500]
        root = Path(output_root or DEFAULT_OUTPUT_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{run_id}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        store.save_run(run_id, engagement, signal["fingerprint"], str(path.resolve()))
        return {**result, "artifact": str(path.resolve())}
    finally:
        store.close()
