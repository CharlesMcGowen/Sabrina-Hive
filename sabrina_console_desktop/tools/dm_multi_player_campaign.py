"""One frozen task, four isolated player paths, external grading, bounded review.

This is a development experiment.  Best-of-four is a system result, not weight
learning.  The optional weight specialist can inspect/intervene and draft a
manifest, but this runner exposes no operation that applies or promotes it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from dm_campaign_tasks import FAMILIES, make_task, reference
from dm_provider import build_dm_provider
from dm_software_campaign import release, sandbox, worker
from dm_weight_lab import DMLab, digest, dump


PATH_CARDS = {
    "contract_first": "Translate every public requirement into an explicit invariant before implementing. Prefer the smallest valid solution.",
    "state_first": "Trace state and data dependencies first. Verify ordering, mutation, and boundary behavior before implementing.",
    "adversarial_recovery": "Look for ambiguity, malformed inputs, edge cases, and recovery behavior. Make failure handling explicit.",
    "alternative_decomposition": "Decompose the task through a different valid representation or algorithm, then check it against every public clause.",
}
PLAYER_IDS = ["p1", "p2", "p3", "p4"]
PLAYER_SEEDS = [1701, 2718, 3141, 5772]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hypothesis": {"type": "string"},
        "prediction": {
            "type": "object",
            "properties": {
                "metric": {"type": "string", "enum": ["passed_tests"]},
                "expected_best_path_card": {"type": "string", "enum": list(PATH_CARDS)},
                "minimum_margin_tests": {"type": "integer", "minimum": 0, "maximum": 24},
            },
            "required": ["metric", "expected_best_path_card", "minimum_margin_tests"],
            "additionalProperties": False,
        },
        "assignments": {
            "type": "array", "minItems": 4, "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "string", "enum": PLAYER_IDS},
                    "path_card": {"type": "string", "enum": list(PATH_CARDS)},
                    "rationale": {"type": "string"},
                },
                "required": ["player_id", "path_card", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "hypothesis", "prediction", "assignments"],
    "additionalProperties": False,
}

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "failure_family": {
            "type": "string",
            "enum": ["contract", "state", "edge_case", "decomposition", "syntax", "insufficient_evidence"],
        },
        "selected_players": {
            "type": "array", "items": {"type": "string", "enum": PLAYER_IDS},
            "maxItems": 4,
        },
        "failure_cluster_id": {"type": "string"},
        "specialist_hypothesis": {"type": "string"},
        "recommendation": {"type": "string", "enum": ["no_weight_action", "inspect_activations"]},
        "reason": {"type": "string"},
    },
    "required": ["failure_family", "failure_cluster_id", "selected_players", "specialist_hypothesis", "recommendation", "reason"],
    "additionalProperties": False,
}


def canonical_hash(value) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_plan(plan: dict) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("DM plan must be an object")
    if not isinstance(plan.get("title"), str) or not plan["title"].strip():
        raise ValueError("DM title is required")
    if not isinstance(plan.get("hypothesis"), str) or not plan["hypothesis"].strip():
        raise ValueError("DM hypothesis is required")
    prediction = plan.get("prediction")
    if not isinstance(prediction, dict) or set(prediction) != {
        "metric", "expected_best_path_card", "minimum_margin_tests"
    }:
        raise ValueError("DM must make one grader-measurable prediction")
    if prediction["metric"] != "passed_tests" or prediction["expected_best_path_card"] not in PATH_CARDS:
        raise ValueError("Invalid DM prediction")
    if type(prediction["minimum_margin_tests"]) is not int or not 0 <= prediction["minimum_margin_tests"] <= 24:
        raise ValueError("Invalid prediction margin")
    assignments = plan.get("assignments")
    if not isinstance(assignments, list) or len(assignments) != 4:
        raise ValueError("DM must assign exactly four players")
    if {item.get("player_id") for item in assignments} != set(PLAYER_IDS):
        raise ValueError("DM must assign p1 through p4 exactly once")
    if {item.get("path_card") for item in assignments} != set(PATH_CARDS):
        raise ValueError("DM must use every frozen path card exactly once")
    for item in assignments:
        if set(item) != {"player_id", "path_card", "rationale"}:
            raise ValueError("Unexpected assignment field")
        if not isinstance(item["rationale"], str) or not 1 <= len(item["rationale"].strip()) <= 1000:
            raise ValueError("Each assignment needs a bounded rationale")
    return plan


def sanitize_score(score: dict) -> dict:
    """Remove expected outputs and test arguments before model-authored review."""
    failures = []
    for item in score.get("failures", [])[:3]:
        actual = item.get("actual", {}) if isinstance(item, dict) else {}
        failures.append({
            "test_index": item.get("test") if isinstance(item, dict) else None,
            "actual_kind": type(actual.get("value")).__name__ if "value" in actual else None,
            "error": str(actual.get("error", ""))[:300] or None,
            "input_mutated": bool(actual.get("mutated", False)),
        })
    return {
        "passed": bool(score.get("passed", False)),
        "passed_tests": int(score.get("passed_tests", 0)),
        "total_tests": int(score.get("total_tests", 0)),
        "failed_test_count": int(score.get("failed_test_count", len(score.get("failures", [])))),
        "execution_error": str(score.get("error", ""))[:300] or None,
        "failure_observations": failures,
    }


def failure_fingerprints(clean_score: dict) -> list[str]:
    fingerprints = set()
    if clean_score.get("execution_error"):
        text = clean_score["execution_error"].lower()
        kind = "container_exit" if text.startswith("container exit") else text.split(":", 1)[0][:40]
        fingerprints.add("execution:" + kind)
    for item in clean_score.get("failure_observations", []):
        if item.get("error"):
            fingerprints.add("error:" + item["error"].split(":", 1)[0].strip())
        elif item.get("actual_kind"):
            fingerprints.add("value_kind:" + item["actual_kind"])
        if item.get("input_mutated"):
            fingerprints.add("input_mutated")
    return sorted(fingerprints)


def build_evidence_bundle(public_task: dict, plan: dict, player_rows: list[dict]) -> dict:
    if len(player_rows) != 4 or {row.get("player_id") for row in player_rows} != set(PLAYER_IDS):
        raise ValueError("Evidence requires exactly four distinct player rows")
    rows = []
    for row in player_rows:
        rows.append({
            "player_id": row["player_id"],
            "path_card": row["path_card"],
            "artifact_sha256": row["artifact_sha256"],
            "score": sanitize_score(row["score"]),
        })
    failures = [row["player_id"] for row in rows if not row["score"]["passed"]]
    clusters = {}
    for row in rows:
        if row["score"]["passed"]:
            continue
        for fingerprint in failure_fingerprints(row["score"]):
            clusters.setdefault(fingerprint, []).append(row["player_id"])
    failure_clusters = [
        {"cluster_id": key, "players": sorted(set(players)), "reproduced": len(set(players)) >= 2}
        for key, players in sorted(clusters.items())
    ]
    reproduced = [item for item in failure_clusters if item["reproduced"]]
    has_successful_control = any(row["score"]["passed"] for row in rows)
    return {
        "label": "development-only sanitized evidence; no hidden expected outputs",
        "task": public_task,
        "plan": {
            "title": plan["title"],
            "hypothesis": plan["hypothesis"],
            "plan_sha256": canonical_hash(plan),
        },
        "players": rows,
        "failed_players": failures,
        "failure_clusters": failure_clusters,
        "reproduced_failure_clusters": reproduced,
        "repeated_failure": bool(reproduced),
        "has_successful_player_control": has_successful_control,
        "specialist_ready": bool(reproduced) and has_successful_control,
        "system_pass_at_4": len(failures) < 4,
    }


def validate_review(review: dict, evidence: dict) -> dict:
    if not isinstance(review, dict) or set(review) != set(REVIEW_SCHEMA["required"]):
        raise ValueError("Invalid DM review fields")
    failed = set(evidence["failed_players"])
    selected = review["selected_players"]
    if not isinstance(selected, list) or len(selected) != len(set(selected)) or not set(selected) <= failed:
        raise ValueError("DM may select only distinct failed players")
    if review["recommendation"] == "inspect_activations":
        clusters = {item["cluster_id"]: set(item["players"])
                    for item in evidence["reproduced_failure_clusters"]}
        cluster_players = clusters.get(review["failure_cluster_id"])
        if (not evidence["specialist_ready"] or cluster_players is None or len(selected) < 2
                or not set(selected) <= cluster_players):
            raise ValueError("Activation inspection requires one successful target-model control and one deterministic failure fingerprint shared by the selected players")
    if review["recommendation"] == "no_weight_action" and selected:
        raise ValueError("No-weight-action review must not nominate players")
    if review["recommendation"] == "no_weight_action" and review["failure_cluster_id"]:
        raise ValueError("No-weight-action review must not nominate a failure cluster")
    if review["failure_family"] not in REVIEW_SCHEMA["properties"]["failure_family"]["enum"]:
        raise ValueError("Unknown failure family")
    for key in ["specialist_hypothesis", "reason"]:
        if not isinstance(review[key], str) or not 1 <= len(review[key].strip()) <= 2000:
            raise ValueError(f"Invalid {key}")
    return review


def promotion_gate(evaluation: dict) -> dict:
    """Pure operator-side gate.  This module does not apply or promote anything."""
    required = {
        "source_unchanged", "selection_frozen_before_heldout", "unchanged_reload_passed",
        "noop_control_passed", "candidate_reload_reproduced", "heldout_delta",
        "regressions", "causal_controls_passed", "human_approved",
    }
    missing = sorted(required - set(evaluation))
    reasons = []
    if missing:
        reasons.append("missing:" + ",".join(missing))
    checks = [
        ("source_unchanged", True),
        ("selection_frozen_before_heldout", True),
        ("unchanged_reload_passed", True),
        ("noop_control_passed", True),
        ("candidate_reload_reproduced", True),
        ("causal_controls_passed", True),
        ("human_approved", True),
    ]
    for key, expected in checks:
        if evaluation.get(key) is not expected:
            reasons.append(key)
    if not isinstance(evaluation.get("heldout_delta"), (int, float)) or evaluation.get("heldout_delta", 0) <= 0:
        reasons.append("no_positive_heldout_delta")
    if evaluation.get("regressions") != 0:
        reasons.append("regressions_present")
    return {"eligible": not reasons, "reasons": reasons, "automatic_promotion": False}


def _player_messages(public_task: dict, assignment: dict) -> list[dict]:
    return [{
        "role": "user",
        "content": (
            public_task["spec"] + "\n\nPath constraint: " + PATH_CARDS[assignment["path_card"]] +
            "\nReturn a complete Python implementation in one python code block. Standard library only. "
            "Define solve exactly; no demonstration calls. This is a single independent attempt."
        ),
    }]


def _specialist_request(evidence: dict, review: dict, source_hash: str) -> dict:
    return {
        "status": "operator_gate_required",
        "source_sha256": source_hash,
        "development_evidence_sha256": canonical_hash(evidence),
        "hypothesis": review["specialist_hypothesis"],
        "failure_cluster_id": review["failure_cluster_id"],
        "selected_players": review["selected_players"],
        "allowed_tools": [
            "dm_list_tensors", "dm_tensor_slice", "dm_intervene", "dm_inspect_result", "dm_draft_manifest"
        ],
        "forbidden": ["shell", "training", "apply", "promotion", "grader_changes", "heldout_access"],
        "instruction": (
            "Use development evidence only. Establish a baseline and causal controls before drafting at most one unapplied manifest. "
            "A first-next-token effect is diagnostic and is not proof of full-action or heldout improvement."
        ),
    }


@contextmanager
def _campaign_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        try:
            if acquired and os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            elif acquired:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _run_unlocked(root: Path, family: str, variant: int, seed: int, dm_provider: str,
                  dm_model: str | None, reasoning_effort: str, run_specialist: bool,
                  specialist_model: str) -> dict:
    if family not in FAMILIES or not 0 <= variant <= 3:
        raise ValueError("Choose a reviewed family and variant 0..3")
    if root.exists():
        raise FileExistsError("Run root already exists; use a new immutable run directory")
    root.mkdir(parents=True)
    for name in ["private", "dm", "players", "grader", "review", "weight-specialist"]:
        (root / name).mkdir()

    registry = json.loads((Path.home() / ".config/sabrina/weight_space_models.json").read_text())
    source = Path(registry["models"]["qwen2.5-coder-1.5b"])
    source_files = sorted(source.glob("*.safetensors"))
    if not source_files:
        raise FileNotFoundError("Registered player source has no safetensors files")
    source_hash = canonical_hash({path.name: digest(path) for path in source_files})
    image = subprocess.check_output(
        ["docker", "image", "inspect", "python:3.11-slim", "--format", "{{.Id}}"], text=True
    ).strip()
    task = make_task(family, variant, seed)
    task_id = f"{family}-v{variant}-s{seed}"
    public_task = {"id": task_id, "family": family, "variant": variant, "spec": task["spec"]}
    dump(root / "task-public.json", public_task)
    dump(root / "private" / "task.json", task)

    protocol = {
        "format_version": 1,
        "state": "frozen",
        "condition": "dm_directed_diversity",
        "task_public_sha256": canonical_hash(public_task),
        "task_private_sha256": canonical_hash(task),
        "grader_source_sha256": digest(Path(__file__).with_name("dm_software_campaign.py")),
        "container_image": image,
        "player_source": str(source),
        "player_source_sha256": source_hash,
        "player_ids": PLAYER_IDS,
        "logical_seeds": PLAYER_SEEDS,
        "path_cards": PATH_CARDS,
        "max_new_tokens_per_player": 768,
        "player_sessions_isolated": True,
        "player_execution": "sequential_on_shared_gpu",
        "inter_player_communication": False,
        "dm_may_grade": False,
        "specialist_may_apply": False,
        "automatic_promotion": False,
        "claim": "Four-path development system result; not base-model learning or a public benchmark.",
    }
    dump(root / "protocol.json", protocol)

    reference_path = root / "grader" / "reference.py"
    reference_path.write_text(reference(task), encoding="utf-8")
    reference_score = sandbox(task, reference_path, image, root / "grader" / "reference-score.json")
    wrong_path = root / "grader" / "negative.py"
    wrong_path.write_text("def solve(*args): return None\n", encoding="utf-8")
    negative_score = sandbox(task, wrong_path, image, root / "grader" / "negative-score.json")
    if not reference_score["passed"] or negative_score["passed"]:
        raise RuntimeError("Deterministic grader controls failed")

    dm = build_dm_provider(root / "dm", dm_provider, dm_model, reasoning_effort)
    plan = dm.ask(
        "plan",
        "You are Sabrina's Dungeon Master. The operator has already frozen one public task, four generic path cards, equal budgets, and an external grader. Assign every player and every path card exactly once. State a falsifiable hypothesis and predict which path card will have the highest externally measured passed_tests count, including a minimum test-count margin. You cannot change the task, reveal hidden tests, grade outputs, train, edit weights, or promote a model.",
        {"public_task": public_task, "players": PLAYER_IDS, "path_cards": PATH_CARDS},
        PLAN_SCHEMA,
    )
    validate_plan(plan)
    dump(root / "dm" / "plan.json", plan)
    protocol["dm"] = {"provider": dm.provider, "model": dm.model}
    protocol["dm_plan_sha256"] = canonical_hash(plan)
    protocol["state"] = "playing"
    dump(root / "protocol.json", protocol)

    lab = DMLab(source, root / "player-source-session")
    rows = []
    began = time.monotonic()
    try:
        assignments = {item["player_id"]: item for item in plan["assignments"]}
        for player_id in PLAYER_IDS:
            assignment = assignments[player_id]
            player_root = root / "players" / player_id
            code_path = worker(lab, _player_messages(public_task, assignment), player_root)
            score = sandbox(task, code_path, image, player_root / "score.json")
            row = {
                "player_id": player_id,
                "logical_seed": PLAYER_SEEDS[PLAYER_IDS.index(player_id)],
                "path_card": assignment["path_card"],
                "artifact_sha256": digest(code_path),
                "score": score,
            }
            rows.append(row)
            dump(root / "grader" / "player-results.json", rows)
    finally:
        release(lab)
        lab.close()

    evidence = build_evidence_bundle(public_task, plan, rows)
    dump(root / "review" / "sanitized-failure-packet.json", evidence)
    review = dm.ask(
        "review",
        "You are the campaign reviewer, not the grader. Review only the sanitized development evidence. You may recommend bounded activation inspection only when specialist_ready is true, by naming one supplied reproduced_failure_clusters cluster_id and selecting at least two players listed in that exact cluster. This requires both a successful target-model player control and repeated failed players. Otherwise use an empty cluster ID, select no players, and recommend no weight action. A specialist hypothesis must predict a bounded activation or next-token effect testable with the allowed DMLab tools, not merely restate path-card performance. Do not infer hidden expected outputs, claim learning, apply edits, or promote a model.",
        evidence,
        REVIEW_SCHEMA,
    )
    validate_review(review, evidence)
    dump(root / "review" / "dm-review.json", review)

    specialist = None
    specialist_outcome = None
    if review["recommendation"] == "inspect_activations":
        specialist = _specialist_request(evidence, review, source_hash)
        dump(root / "weight-specialist" / "request.json", specialist)
        if run_specialist:
            from dm_weight_agent import run as run_weight_specialist
            specialist_task = (
                "Investigate this reproduced development failure with the bounded DMLab tools. "
                "Do not train or apply edits. Draft a manifest only if causal controls support it. "
                + json.dumps({
                    "public_task": public_task,
                    "hypothesis": review["specialist_hypothesis"],
                    "selected_players": review["selected_players"],
                    "path_results": [
                        {"player_id": row["player_id"], "path_card": row["path_card"],
                         "score": sanitize_score(row["score"])} for row in rows
                    ],
                }, sort_keys=True)
            )
            specialist_outcome = run_weight_specialist(
                specialist_task, str(source), root / "weight-specialist" / "session", specialist_model
            )
            dump(root / "weight-specialist" / "outcome.json", specialist_outcome)
    else:
        dump(root / "weight-specialist" / "not-requested.json", {
            "status": "not_requested", "reason": review["reason"], "weight_edits": 0,
        })

    passes = sum(bool(row["score"]["passed"]) for row in rows)
    best_tests = max(row["score"]["passed_tests"] for row in rows)
    best_cards = sorted(row["path_card"] for row in rows if row["score"]["passed_tests"] == best_tests)
    prediction = plan["prediction"]
    predicted_card = prediction["expected_best_path_card"]
    predicted_row = next(row for row in rows if row["path_card"] == predicted_card)
    other_best = max(row["score"]["passed_tests"] for row in rows if row["path_card"] != predicted_card)
    prediction_margin = predicted_row["score"]["passed_tests"] - other_best
    outcome = {
        "status": "completed",
        "task_id": task_id,
        "dm_provider": dm.provider,
        "dm_model": dm.model,
        "players": 4,
        "player_passes": passes,
        "observed_single_attempt_rate": passes / 4,
        "system_pass_at_4": passes > 0,
        "all_four_passed": passes == 4,
        "dm_prediction": {
            "expected_best_path_card": predicted_card,
            "actual_best_path_cards": best_cards,
            "actual_margin_tests": prediction_margin,
            "matched": predicted_card in best_cards and prediction_margin >= prediction["minimum_margin_tests"],
        },
        "specialist_request_created": specialist is not None,
        "specialist_executed": specialist_outcome is not None,
        "training_steps": 0,
        "weight_edits_applied": 0,
        "model_promoted": False,
        "elapsed_seconds": time.monotonic() - began,
        "interpretation": "DM-directed four-path development experiment; no weight-learning claim.",
    }
    protocol["state"] = "dm_reviewed"
    dump(root / "protocol.json", protocol)
    dump(root / "outcome.json", outcome)
    return outcome


def run(root: Path, family: str, variant: int, seed: int, dm_provider: str,
        dm_model: str | None, reasoning_effort: str, run_specialist: bool = False,
        specialist_model: str = "huihui_ai/qwen3-abliterated:14b") -> dict:
    root = Path(root)
    with _campaign_lock(root.parent / ".dm_weight_lab.lock"):
        return _run_unlocked(
            root, family, variant, seed, dm_provider, dm_model, reasoning_effort,
            run_specialist, specialist_model,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--family", required=True, choices=FAMILIES)
    parser.add_argument("--variant", type=int, default=0, choices=range(4))
    parser.add_argument("--seed", type=int, default=73001)
    parser.add_argument("--dm-provider", choices=["auto", "ollama", "openai"], default="auto")
    parser.add_argument("--dm-model")
    parser.add_argument("--reasoning-effort", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--run-specialist", action="store_true",
                        help="Run the bounded DMLab specialist after a reproduced failure; never applies a draft")
    parser.add_argument("--specialist-model", default="huihui_ai/qwen3-abliterated:14b")
    args = parser.parse_args()
    print(json.dumps(run(
        args.root, args.family, args.variant, args.seed, args.dm_provider,
        args.dm_model, args.reasoning_effort, args.run_specialist, args.specialist_model,
    ), indent=2))


if __name__ == "__main__":
    main()
