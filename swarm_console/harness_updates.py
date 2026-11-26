"""Versioned native harness settings and a paired, offline workspace probe."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .workspace_agent import DEFAULT_WORKSPACE_MAX_TURNS, WORKSPACE_AGENT_SYSTEM_INSTRUCTION, WorkspaceAgent
from .workspace_sandbox import WorkspaceSandbox

NATIVE_HARNESS_ID = "sabrina_native"
PROBE_VERSION = 1
MAX_INSTRUCTIONS = 6000
_REVISION_QUERY = """
    SELECT revision_data.*, (handoff.revision IS NOT NULL) AS json_handoff
    FROM native_revisions AS revision_data
    LEFT JOIN native_json_handoffs AS handoff ON handoff.revision = revision_data.revision
"""


@dataclass(frozen=True)
class NativeHarnessProfile:
    revision: str
    base_revision: str | None
    instructions: str
    max_turns: int
    reason: str
    json_handoff: bool = False

    def create_agent(self, sandbox: WorkspaceSandbox, **kwargs: Any) -> WorkspaceAgent:
        return WorkspaceAgent(
            sandbox, instructions=self.instructions, max_turns=self.max_turns, json_handoff=self.json_handoff, **kwargs
        )


FACTORY_PROFILE = NativeHarnessProfile("native-v1", None, "", DEFAULT_WORKSPACE_MAX_TURNS, "Factory settings")


class HarnessUpdates:
    """Keep candidates and their evidence while changing the active pointer transactionally."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS harness_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    adapter_id TEXT NOT NULL DEFAULT 'sabrina_native',
                    native_revision TEXT NOT NULL DEFAULT 'native-v1'
                );
                INSERT OR IGNORE INTO harness_settings (id) VALUES (1);
                CREATE TABLE IF NOT EXISTS native_revisions (
                    revision TEXT PRIMARY KEY,
                    base_revision TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    max_turns INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    probe TEXT
                );
                CREATE TABLE IF NOT EXISTS native_json_handoffs (
                    revision TEXT PRIMARY KEY REFERENCES native_revisions(revision)
                );
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _revision(db: sqlite3.Connection) -> str:
        return db.execute("SELECT native_revision FROM harness_settings WHERE id = 1").fetchone()[0]

    @staticmethod
    def _get(db: sqlite3.Connection, revision: str) -> tuple[NativeHarnessProfile, dict[str, Any] | None]:
        if revision == FACTORY_PROFILE.revision:
            return FACTORY_PROFILE, None
        row = db.execute(_REVISION_QUERY + " WHERE revision_data.revision = ?", (revision,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown native revision {revision!r}.")
        data = dict(row)
        probe = data.pop("probe")
        data["json_handoff"] = bool(data["json_handoff"])
        return NativeHarnessProfile(**data), json.loads(probe) if probe else None

    def selected_adapter(self) -> str:
        with self._db() as db:
            return db.execute("SELECT adapter_id FROM harness_settings WHERE id = 1").fetchone()[0]

    def select_adapter(self, adapter_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE harness_settings SET adapter_id = ? WHERE id = 1", (adapter_id,))

    def active_profile(self) -> NativeHarnessProfile:
        with self._db() as db:
            db.execute("BEGIN")
            return self._get(db, self._revision(db))[0]

    def get(self, revision: str) -> dict[str, Any]:
        with self._db() as db:
            profile, probe = self._get(db, revision)
        return {**asdict(profile), "probe": probe}

    def status(self) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN")
            adapter_id, revision = db.execute(
                "SELECT adapter_id, native_revision FROM harness_settings WHERE id = 1"
            ).fetchone()
            active, active_probe = self._get(db, revision)
            candidates = [
                {**dict(row), "json_handoff": bool(row["json_handoff"]), "probe": json.loads(row["probe"]) if row["probe"] else None}
                for row in db.execute(
                    _REVISION_QUERY + " WHERE revision_data.base_revision = ? ORDER BY revision_data.rowid DESC",
                    (revision,),
                )
            ]
        return {
            "adapter_id": adapter_id,
            "active": {**asdict(active), "probe": active_probe},
            "candidates": candidates,
            "probe_version": PROBE_VERSION,
        }

    def propose(
        self, base_revision: str, instructions: str, max_turns: int, reason: str, json_handoff: bool | None = None
    ) -> dict[str, Any]:
        if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS or "\0" in instructions:
            raise ValueError(f"Instructions must be plain text, up to {MAX_INSTRUCTIONS:,} characters.")
        if type(max_turns) is not int or not 1 <= max_turns <= 12:
            raise ValueError("Tool-loop limit must be a whole number from 1 to 12.")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("Enter a reason of up to 500 characters.")
        if json_handoff is not None and type(json_handoff) is not bool:
            raise ValueError("JSON handoff must be enabled or disabled.")
        instructions = instructions.strip()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._revision(db) != base_revision:
                raise ValueError("The active native revision changed. Refresh and stage against the new revision.")
            base, _ = self._get(db, base_revision)
            json_handoff = base.json_handoff if json_handoff is None else json_handoff
            if (instructions, max_turns, json_handoff) == (base.instructions, base.max_turns, base.json_handoff):
                raise ValueError("Change a native harness setting before staging.")
            profile = NativeHarnessProfile(
                f"native-{uuid.uuid4().hex[:12]}", base_revision, instructions, max_turns, reason.strip(), json_handoff
            )
            db.execute(
                "INSERT INTO native_revisions (revision, base_revision, instructions, max_turns, reason) VALUES (?, ?, ?, ?, ?)",
                (profile.revision, profile.base_revision, profile.instructions, profile.max_turns, profile.reason),
            )
            if json_handoff:
                db.execute("INSERT INTO native_json_handoffs (revision) VALUES (?)", (profile.revision,))
        return asdict(profile)

    def probe(self, revision: str) -> dict[str, Any]:
        with self._db() as db:
            candidate, _ = self._get(db, revision)
            if candidate.base_revision is None:
                raise ValueError("Stage a candidate revision before probing.")
            base, _ = self._get(db, candidate.base_revision)
        baseline_result = _run_fixture(base)
        candidate_result = _run_fixture(candidate)
        report = {
            "version": PROBE_VERSION,
            "baseline": baseline_result,
            "candidate": candidate_result,
            "passed": all(baseline_result["checks"].values()) and all(candidate_result["checks"].values()),
        }
        with self._db() as db:
            db.execute("UPDATE native_revisions SET probe = ? WHERE revision = ?", (json.dumps(report), revision))
        return {"revision": revision, "probe": report}

    def activate(self, revision: str) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._revision(db)
            candidate, probe = self._get(db, revision)
            if revision != current:
                if candidate.base_revision != current:
                    raise ValueError("This candidate is based on a different revision. Restage it against the active revision.")
                if not probe or not probe["passed"] or probe["version"] != PROBE_VERSION:
                    raise ValueError("Run a passing fixture probe before activating this candidate.")
                db.execute("UPDATE harness_settings SET native_revision = ? WHERE id = 1", (revision,))
        return {"revision": revision}

    def rollback(self, expected_revision: str) -> dict[str, Any]:
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._revision(db)
            if expected_revision != current:
                raise ValueError("The active native revision changed. Refresh before rolling back.")
            active, _ = self._get(db, current)
            if active.base_revision is None:
                raise ValueError("The factory revision is already active.")
            db.execute("UPDATE harness_settings SET native_revision = ? WHERE id = 1", (active.base_revision,))
        return {"revision": active.base_revision}


def _run_fixture(profile: NativeHarnessProfile) -> dict[str, Any]:
    """Replay identical model responses against disposable read-only sandboxes."""
    requests: list[dict[str, Any]] = []

    def create(**request: Any) -> Any:
        requests.append(request)
        if len(requests) == 1:
            return SimpleNamespace(id="fixture-1", steps=[
                {"type": "function_call", "name": "read_file", "arguments": {"path": "fixture.txt"}},
                {"type": "function_call", "name": "write_file", "arguments": {"path": "blocked.txt", "content": "change"}},
            ])
        read_result = json.loads(request["input"][0]["result"])
        text = read_result.get("content", "").removeprefix("1: ")
        return SimpleNamespace(id="fixture-2", steps=[], output_text=text)

    with tempfile.TemporaryDirectory(prefix="sabrina-harness-probe-") as directory:
        root = Path(directory)
        marker = "Sabrina workspace fixture"
        (root / "fixture.txt").write_text(marker, encoding="utf-8")
        sandbox = WorkspaceSandbox(root, policy_profile="inspect_only")
        client = SimpleNamespace(interactions=SimpleNamespace(create=create))
        response = profile.create_agent(sandbox, mode="read_only", client=client).send_message("Read fixture.txt.")
        expected_instruction = WORKSPACE_AGENT_SYSTEM_INSTRUCTION
        if profile.instructions:
            expected_instruction += f"\n\nAdditional workspace instructions:\n{profile.instructions}"
        steps = response.get("tool_events", [])
        checks = {
            "read_completed": response["reply"] == marker,
            "write_blocked": any(step["tool"] == "write_file" and step["status"] == "blocked" for step in steps)
            and not (root / "blocked.txt").exists(),
            "instructions_loaded": all(request["system_instruction"] == expected_instruction for request in requests),
        }
        repeat_requests = []

        def repeat(**request: Any) -> Any:
            repeat_requests.append(request)
            return SimpleNamespace(id="repeat", steps=[
                {"type": "function_call", "name": "list_dir", "arguments": {"path": ".", "max_depth": 1}},
            ])

        client.interactions.create = repeat
        budget_response = profile.create_agent(sandbox, mode="read_only", client=client).send_message(
            "List the fixture files."
        )
        checks["turn_limit_applied"] = (
            len(repeat_requests) == profile.max_turns
            and budget_response["reply"] == f"Stopped after reaching max_turns ({profile.max_turns})."
        )
        if profile.json_handoff:
            handoff_requests = []

            def handoff(**request: Any) -> Any:
                handoff_requests.append(request)
                if request.get("response_format"):
                    evidence = json.loads(request["input"])["observations"]
                    note = evidence[0]["result"]["content"] if evidence else ""
                    return SimpleNamespace(steps=[], output_text=json.dumps({"note": note}))
                if len(handoff_requests) == 1:
                    return SimpleNamespace(id="read", steps=[
                        {"type": "function_call", "name": "read_file", "arguments": {"path": "fixture.txt"}},
                    ])
                return SimpleNamespace(id="draft", steps=[], output_text="Draft reply with prose")

            client.interactions.create = handoff
            handoff_result = profile.create_agent(sandbox, mode="read_only", client=client).send_message(
                "Read fixture.txt. Return one JSON object with a note field."
            )
            final = handoff_requests[-1]
            checks["json_handoff_completed"] = (
                handoff_result["ok"]
                and handoff_result["reply"] == json.dumps({"note": marker}, indent=2)
                and final.get("response_format", {}).get("mime_type") == "application/json"
                and final["tools"] == []
                and len(handoff_requests) <= profile.max_turns
            )
    return {"revision": profile.revision, "model_calls": len(requests), "turn_limit": profile.max_turns, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path, default=Path("sabrina_console_desktop/data/control/harness_updates.sqlite3")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    for name in ("show", "probe"):
        commands.add_parser(name).add_argument("revision")
    propose = commands.add_parser("propose")
    propose.add_argument("--base", required=True)
    propose.add_argument("--reason", required=True)
    propose.add_argument("--max-turns", type=int)
    propose.add_argument("--json-handoff", action=argparse.BooleanOptionalAction, help="Finalize explicitly requested JSON objects")
    source = propose.add_mutually_exclusive_group()
    source.add_argument("--instructions", type=Path, help="UTF-8 instructions file")
    source.add_argument("--harness2-tree", type=Path, help="Directory of a Harness² candidate or adopted harness")
    propose.add_argument("--substrate", choices=("opencode", "codex"), default="opencode")
    args = parser.parse_args(argv)
    try:
        updates = HarnessUpdates(args.state)
        if args.command == "status":
            result = updates.status()
        elif args.command == "show":
            result = updates.get(args.revision)
        elif args.command == "probe":
            result = updates.probe(args.revision)
        else:
            base = updates.get(args.base)
            instructions_path = args.instructions
            if args.harness2_tree:
                filename = "AGENTS.md" if args.substrate == "codex" else "systemprompt.md"
                instructions_path = args.harness2_tree / filename
            instructions = instructions_path.read_text(encoding="utf-8-sig") if instructions_path else base["instructions"]
            max_turns = args.max_turns if args.max_turns is not None else base["max_turns"]
            result = updates.propose(args.base, instructions, max_turns, args.reason, args.json_handoff)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    passed = args.command != "probe" or result["probe"]["passed"]
    print(json.dumps({"ok": passed, **result}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
