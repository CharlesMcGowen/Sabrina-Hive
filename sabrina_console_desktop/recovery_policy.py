"""Validate and atomically promote the settings used for new recovery workers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "control" / "recovery_policy.json"


@dataclass(frozen=True)
class RecoveryPolicy:
    model: str = "ollama/gemma3:12b"
    timeout_seconds: int = 90
    max_context_chars: int = 6000
    max_reply_tokens: int = 768
    max_workers_per_mission: int = 8
    allow_worker_hotfixes: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()[:12]

    @classmethod
    def load(cls, path: Path) -> RecoveryPolicy:
        with path.open("rb") as file:
            contents = file.read(4097)
        if len(contents) > 4096:
            raise ValueError("Recovery policy must be at most 4096 bytes")
        values = json.loads(contents)
        ranges = {
            "timeout_seconds": (5, 120),
            "max_context_chars": (1000, 12000),
            "max_reply_tokens": (128, 2048),
            "max_workers_per_mission": (0, 8),
        }
        if not isinstance(values, dict) or values.keys() - {"model", "allow_worker_hotfixes", *ranges}:
            raise ValueError("Recovery policy contains unsupported settings")
        policy = cls(**values)
        if not isinstance(policy.model, str) or not re.fullmatch(r"ollama/[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,95}", policy.model):
            raise ValueError("Recovery model must be an Ollama model ID")
        if type(policy.allow_worker_hotfixes) is not bool:
            raise ValueError("allow_worker_hotfixes must be true or false")
        for name, (minimum, maximum) in ranges.items():
            value = getattr(policy, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
        return policy


def promote(candidate: Path, destination: Path = DEFAULT_PATH) -> RecoveryPolicy:
    policy = RecoveryPolicy.load(candidate)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=".recovery-", dir=destination.parent)
    pending = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(policy.to_json())
            file.flush()
            os.fsync(file.fileno())
        os.replace(pending, destination)
    finally:
        pending.unlink(missing_ok=True)
    return policy


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "promote"))
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args()
    try:
        policy = promote(args.candidate, args.output) if args.action == "promote" else RecoveryPolicy.load(args.candidate)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Recovery policy {policy.revision}")
