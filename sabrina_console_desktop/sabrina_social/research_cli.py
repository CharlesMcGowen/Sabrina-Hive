"""Research JSON import/export and deadline inspection, usable by an external scheduler."""
import argparse
import json
import os
from pathlib import Path
from .experiments import ExperimentLab
from .research_model import LabResearchModel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "observe", "publication", "snapshot", "show", "list", "due", "policy"])
    parser.add_argument("--db", default=str(Path(os.environ.get("SABRINA_SOCIAL_DATA_DIR", "data/social")) / "experiments.sqlite3"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--id")
    args = parser.parse_args()
    lab = ExperimentLab(args.db, LabResearchModel())
    data = json.loads(args.input.read_text(encoding="utf-8")) if args.input else None
    if args.action in ("create", "observe", "publication", "snapshot") and data is None:
        parser.error("This action requires --input JSON_FILE")
    if args.action in ("show", "publication", "snapshot", "policy") and not args.id:
        parser.error("This action requires --id EXPERIMENT_ID")
    actions = {"create": lambda: lab.create(data), "observe": lambda: lab.observe(data),
               "publication": lambda: lab.publish_evidence(args.id, data),
               "snapshot": lambda: lab.snapshot(args.id, data), "show": lambda: lab.get(args.id),
               "list": lab.list, "due": lab.due,
               "policy": lambda: lab.policy(lab.get(args.id)["context"])}
    print(json.dumps(actions[args.action](), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
