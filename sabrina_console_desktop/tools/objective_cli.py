"""Command-line interface for Sabrina's autonomous objective harness.

Usage:
    python -m tools.objective_cli create --goal "..." [--read-only] [--budget 10] [--timeout 300] [--run]
    python -m tools.objective_cli run <objective_id>
    python -m tools.objective_cli step <objective_id>
    python -m tools.objective_cli status <objective_id>
    python -m tools.objective_cli list [--limit 20]
    python -m tools.objective_cli report <objective_id> [--output path]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.objective_harness import ObjectiveContract, ObjectiveHarness


def _get_db_path(args_db: str | None = None) -> Path:
    return Path(args_db or os.environ.get("SABRINA_ASSISTANT_DB", str(ROOT / "data/sabrina_assistant.sqlite3"))).resolve()


def cmd_create(args) -> int:
    obj_id = args.id or f"obj-{uuid.uuid4().hex[:12]}"
    contract = ObjectiveContract(
        id=obj_id,
        goal=args.goal,
        scope={"read_only": not args.allow_write},
        budget_rounds=args.budget,
        budget_seconds=float(args.timeout),
    )
    db_path = _get_db_path(args.db)
    harness = ObjectiveHarness(contract)
    harness.save_to_db(db_path)

    print(json.dumps({
        "status": "created",
        "objective_id": obj_id,
        "goal": args.goal,
        "scope": contract.scope,
        "budget_rounds": contract.budget_rounds,
        "budget_seconds": contract.budget_seconds,
    }, indent=2))

    if args.run:
        print(f"\n[Running objective {obj_id}...]")
        evidence = harness.run()
        print(json.dumps({
            "objective_id": obj_id,
            "status": evidence.status,
            "rounds": harness.round_counter,
            "has_report": bool(evidence.report),
        }, indent=2))
    return 0


def cmd_run(args) -> int:
    db_path = _get_db_path(args.db)
    harness = ObjectiveHarness.load_from_db(args.objective_id, db_path)
    if not harness:
        print(f"Error: Objective '{args.objective_id}' not found in {db_path}", file=sys.stderr)
        return 1

    print(f"Starting execution for objective: {harness.contract.id} ({harness.contract.goal})")
    evidence = harness.run()
    harness.save_to_db(db_path)
    print(json.dumps({
        "objective_id": harness.contract.id,
        "status": evidence.status,
        "rounds": harness.round_counter,
        "verified_states": len(evidence.verified_states),
        "has_report": bool(evidence.report),
    }, indent=2))
    return 0


def cmd_step(args) -> int:
    db_path = _get_db_path(args.db)
    harness = ObjectiveHarness.load_from_db(args.objective_id, db_path)
    if not harness:
        print(f"Error: Objective '{args.objective_id}' not found in {db_path}", file=sys.stderr)
        return 1

    res = harness.step()
    harness.save_to_db(db_path)
    print(json.dumps(res, indent=2))
    return 0


def cmd_status(args) -> int:
    db_path = _get_db_path(args.db)
    harness = ObjectiveHarness.load_from_db(args.objective_id, db_path)
    if not harness:
        print(f"Error: Objective '{args.objective_id}' not found in {db_path}", file=sys.stderr)
        return 1

    last_obs = harness.evidence.observations[-1] if harness.evidence.observations else None
    print(json.dumps({
        "objective_id": harness.contract.id,
        "goal": harness.contract.goal,
        "status": harness.evidence.status,
        "round": harness.round_counter,
        "budget_rounds": harness.contract.budget_rounds,
        "scope": harness.contract.scope,
        "total_observations": len(harness.evidence.observations),
        "verified_states": harness.evidence.verified_states,
        "last_observation": last_obs,
        "has_report": bool(harness.evidence.report),
    }, indent=2))
    return 0


def cmd_list(args) -> int:
    db_path = _get_db_path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT id, goal, state, created FROM workflow_objectives ORDER BY created DESC LIMIT ?",
            (args.limit,)
        )
        rows = [dict(r) for r in cur.fetchall()]

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'ID':<28} {'STATE':<14} {'GOAL'}")
        print("-" * 80)
        for r in rows:
            print(f"{r['id']:<28} {r['state']:<14} {r['goal'][:36]}")
    return 0


def cmd_report(args) -> int:
    db_path = _get_db_path(args.db)
    harness = ObjectiveHarness.load_from_db(args.objective_id, db_path)
    if not harness or not harness.evidence.report:
        with sqlite3.connect(db_path) as con:
            row = con.execute(
                "SELECT raw_response FROM workflow_evidence WHERE objective_id=? ORDER BY created DESC LIMIT 1",
                (args.objective_id,)
            ).fetchone()
            if row:
                report = row[0]
            else:
                print(f"No report found for objective '{args.objective_id}'", file=sys.stderr)
                return 1
    else:
        report = harness.evidence.report

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"Report written to {out_path}")
    else:
        print(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sabrina Autonomous Objective CLI")
    parser.add_argument("--db", default=None, help="Path to SQLite assistant database")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create a new objective contract")
    p_create.add_argument("--goal", required=True, help="Goal description for the objective")
    p_create.add_argument("--id", default=None, help="Custom objective ID")
    p_create.add_argument("--budget", type=int, default=10, help="Maximum rounds budget (default 10)")
    p_create.add_argument("--timeout", type=float, default=300.0, help="Maximum seconds budget (default 300)")
    p_create.add_argument("--allow-write", action="store_true", help="Allow write operations (default is read-only)")
    p_create.add_argument("--run", action="store_true", help="Immediately run until completion")

    # run
    p_run = subparsers.add_parser("run", help="Run an objective to completion")
    p_run.add_argument("objective_id", help="Objective ID to run")

    # step
    p_step = subparsers.add_parser("step", help="Execute a single step for an objective")
    p_step.add_argument("objective_id", help="Objective ID to step")

    # status
    p_status = subparsers.add_parser("status", help="Get status of an objective")
    p_status.add_argument("objective_id", help="Objective ID to inspect")

    # list
    p_list = subparsers.add_parser("list", help="List recent objectives")
    p_list.add_argument("--limit", type=int, default=20, help="Limit number of objectives (default 20)")
    p_list.add_argument("--json", action="store_true", help="Output as JSON")

    # report
    p_report = subparsers.add_parser("report", help="View or save the evidence report of an objective")
    p_report.add_argument("objective_id", help="Objective ID")
    p_report.add_argument("--output", "-o", default=None, help="Output file path (optional)")

    args = parser.parse_args(argv)

    handlers = {
        "create": cmd_create,
        "run": cmd_run,
        "step": cmd_step,
        "status": cmd_status,
        "list": cmd_list,
        "report": cmd_report,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
