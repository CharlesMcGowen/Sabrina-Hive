import argparse
from pathlib import Path

from .common import digest, load, save, compute


def coordinator():
    from tools.control_client import configured
    client = configured()
    if client is None:
        raise RuntimeError("Shared coordinator must be configured")
    client.client, client.project = "sabrina-math-proof", "printed-math-specialist"
    return client


def main():
    parser = argparse.ArgumentParser(description="Sabrina's printed mathematics proof specialist")
    parser.add_argument("action", choices=("freeze", "compile", "evaluate", "select", "prove", "recruit", "status", "preflight", "worker", "collective", "publish"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--candidate", default="candidate-001")
    parser.add_argument("--split", choices=("development", "holdout"), default="development")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--goal", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if Path(args.candidate).name != args.candidate or args.candidate in (".", ".."):
        raise ValueError("Candidate must be a single directory name")
    run, candidate = args.run.resolve(), args.run.resolve() / "candidates" / args.candidate
    if args.action == "freeze":
        from .goals import freeze
        print(freeze(run))
    elif args.action == "publish":
        from .publish import publish
        publish(run, args.output)
    elif args.action == "recruit":
        from .recruit import campaign
        campaign(run)
    elif args.action == "worker":
        from .worker import work
        from .locking import run_lock
        with run_lock(run / "worker.lock"):
            work(run, coordinator())
    elif args.action == "collective":
        from .collective import run_collective
        from .locking import run_lock
        with run_lock(run / "controller.lock"):
            run_collective(run)
    elif args.action == "compile":
        from .compiler import build
        from .policy import export
        if (candidate / "policy.safetensors").exists():
            raise ValueError("Candidate already exists")
        source = args.source.read_text(encoding="utf-8")
        with compute(coordinator()):
            export(build(source), candidate, dict(generator_sha256=digest(source),
                   submission=load(args.source.with_suffix(".json"))))
        (candidate / "generator.py").write_text(source, encoding="utf-8")
    elif args.action == "evaluate":
        from .evaluate import evaluate
        print(evaluate(run, candidate, args.split, coordinator()))
    elif args.action == "select":
        if (run / "finalist.json").exists():
            raise ValueError("Finalist has already been frozen")
        viable = [(load(p), p.parent) for p in (run / "candidates").glob("*/development-summary.json")]
        viable = [(s, p) for s, p in viable if s["metrics"]["policy"]["solved"]]
        if not viable:
            raise ValueError("No evaluated development candidate")
        _, best = min(viable, key=lambda item: (-item[0]["metrics"]["policy"]["solved"],
                    item[0]["metrics"]["policy"]["median_attempts"], item[1].name))
        save(run / "finalist.json", dict(candidate=best.name,
             weight_hash=digest(list((best / "policy.safetensors").read_bytes()))))
        print(best.name)
    elif args.action == "prove":
        from .lean import Checker, theorem, HEADER
        from .policy import ProofPolicy
        from .search import solve
        from .goals import validate
        goal = load(args.goal)
        validate(goal)
        with compute(coordinator()):
            checker = Checker(run / "fresh-process-checks")
            result = solve(goal, ProofPolicy.load(candidate / "policy.safetensors"), checker)
            if result["solved"]:
                from .lean import proof
                bodies = [proof(goal, action) for action in result["actions"]]
                body = bodies[0] if len(bodies) == 1 else "\n".join("all_goals (\n" + part + "\n)" for part in bodies)
                verification = checker.check(goal, body)
                result["emitted_proof_checked"] = verification["ok"]
                result["solved"] = verification["ok"]
        print(result)
        if result["solved"]:
            print(HEADER + theorem(goal, body, "result"))
        else:
            raise SystemExit(2)
    elif args.action == "preflight":
        from .lean import Checker
        from .goals import specimen, ACTIONS
        import random
        checker = Checker(run / "lean-cache")
        with compute(coordinator()):
            for split in (False, True):
                for family in range(4):
                    goal = specimen(random.Random(100+family), family, split)
                    outcome = checker.actions(goal)
                    print(split, family, outcome, flush=True)
                    if not any(s["ok"] for s in outcome.values()):
                        raise RuntimeError("No supported proof for the preflight goal")
    else:
        print(load(run / "finalist.json") if (run / "finalist.json").exists() else "No finalist")
        for p in (run / "candidates").glob("*/*-summary.json"):
            print(p.parent.name, load(p))


if __name__ == "__main__":
    main()
