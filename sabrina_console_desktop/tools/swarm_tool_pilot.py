"""Qualify and release one Sabrina-authored paper frame inventory tool."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
import sys
import time
import uuid

from tools import tool_sandbox
from tools.control_client import caller, configured
from tools.tool_development_context import development_context


ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = "paper.frame_inventory.qualify"
PILOT = "paper-frame-inventory-v1"
MODEL = "qwen2.5-coder:14b"
REPAIR_MODEL = "qwen3:14b"
VALIDATION_MODEL = "granite-code:8b"
IMAGE = "python:3.11-slim"
TAPE = Path("artifacts/kraken_paper/20261115-entry-exit-v1-forward")
PAYLOAD = {"pilot": PILOT, "title": "Build and qualify the paper frame inventory tool"}
AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {"code": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["code", "notes"],
    "additionalProperties": False,
}

CONTRACT = """Write a small standalone Python 3.11 program for inventorying paper journal frames.
Read a JSON array of requests from standard input. Print exactly one JSON array of results
in the same order, with no extra output. Standard library only. The caller supplies a
well-formed JSON array. Do not use the network, files, subprocesses, or other services.

A valid request is an object with exactly two keys: frame_count and indices.
frame_count is an integer from 0 through 4096. indices is an array of at most 4096
integers from 0 through 65535. JSON booleans and floats are not integers here.
For any invalid request, return {"error":"invalid_input"} for that element and continue.
For each valid request, return these four keys:
  missing: ascending list of integers in range(frame_count) absent from indices
  duplicates: ascending list of distinct indices that occur at least twice, including any unexpected ones
  unexpected: ascending list of distinct indices at or above frame_count
  complete: true exactly when all three lists are empty
An empty request array produces an empty result array. A frame_count of 0 with no
indices is complete. Index input order has no effect.

Return a JSON object with keys code and notes. code is the complete Python program as a
string. notes is a concise implementation note identifying the relevant documentation,
input validation choices, and verification you expect. The scheduler tests the program
independently. Do not claim tests or a deployment have already occurred."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temp.write_bytes(json.dumps(value, indent=2, allow_nan=False).encode("utf-8"))
    temp.replace(path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def inventory(missing=(), duplicates=(), unexpected=()):
    return {"missing": list(missing), "duplicates": list(duplicates),
            "unexpected": list(unexpected), "complete": not any((missing, duplicates, unexpected))}


def reference(request):
    counts = Counter(request["indices"])
    return inventory(sorted(set(range(request["frame_count"])) - counts.keys()),
                     sorted(k for k, count in counts.items() if count > 1),
                     sorted(k for k in counts if k >= request["frame_count"]))


def repair_payload(parent_id):
    if not re.fullmatch(r"job_[0-9a-f]{32}", parent_id):
        raise ValueError("Invalid parent scheduler job ID")
    return {**PAYLOAD, "title": "Repair and qualify the paper frame inventory tool",
            "repair_of": parent_id}


def independent_cases():
    invalid = {"error": "invalid_input"}
    cases = [
        ("empty_tape", {"frame_count": 0, "indices": []}, inventory()),
        ("out_of_order", {"frame_count": 4, "indices": [2, 0, 3, 1]}, inventory()),
        ("gaps", {"frame_count": 6, "indices": [4, 1, 0]}, inventory([2, 3, 5])),
        ("duplicate_and_unexpected", {"frame_count": 4, "indices": [7, 2, 0, 2, 7, 9]},
         inventory([1, 3], [2, 7], [7, 9])),
        ("empty_range_with_indices", {"frame_count": 0, "indices": [1, 0, 0]},
         inventory([], [0], [0, 1])),
        ("boolean_count", {"frame_count": True, "indices": []}, invalid),
        ("float_count", {"frame_count": 2.0, "indices": [0]}, invalid),
        ("negative_count", {"frame_count": -1, "indices": []}, invalid),
        ("count_limit", {"frame_count": 4097, "indices": []}, invalid),
        ("boolean_index", {"frame_count": 1, "indices": [False]}, invalid),
        ("float_index", {"frame_count": 1, "indices": [0.0]}, invalid),
        ("negative_index", {"frame_count": 1, "indices": [-1]}, invalid),
        ("index_limit", {"frame_count": 1, "indices": [65536]}, invalid),
        ("index_count_limit", {"frame_count": 1, "indices": [0] * 4097}, invalid),
        ("malformed_object", {"frame_count": 1, "indices": {"0": "0"}}, invalid),
        ("unknown_key", {"frame_count": 1, "indices": [0], "extra": 1}, invalid),
        ("missing_key", {"frame_count": 1}, invalid),
        ("not_an_object", [], invalid),
        ("valid_after_errors", {"frame_count": 1, "indices": [0]}, inventory()),
        ("maximum_valid_size", {"frame_count": 4096, "indices": list(range(4095)) + [65535]},
         inventory([4095], [], [65535])),
    ]
    rng = random.Random(71123)
    for index in range(16):
        count = rng.randrange(1, 40)
        request = {"frame_count": count,
                   "indices": [rng.randrange(count+5) for _ in range(rng.randrange(60))]}
        cases.append((f"varied_{index}", request, reference(request)))
    return cases


def valid_sandbox_output(receipt, code, image, stdin_text):
    return (
        receipt.get("status") == "script_completed" and receipt.get("exit_code") == 0
        and receipt.get("isolated") is True and receipt.get("network") == "none"
        and receipt.get("host_fallback") is False and receipt.get("code_sha256") == digest(code)
        and receipt.get("image_digest") == image and receipt.get("stdin_sha256") == digest(stdin_text)
    )


def evaluate(code, image, inputs, evidence_dir, sandbox):
    stdin_text = canonical(inputs)
    receipt = sandbox(code, timeout=12, image=image, receipt_root=evidence_dir, stdin_text=stdin_text)
    if not valid_sandbox_output(receipt, code, image, stdin_text):
        return receipt, None
    try:
        output = json.loads(receipt["stdout"])
        if isinstance(output, list) and len(output) == len(inputs):
            canonical(output)
            return receipt, output
    except (ValueError, RecursionError, KeyError):
        pass
    return receipt, None


def qualify(code, image, cases, evidence_dir, sandbox):
    receipt, output = evaluate(code, image, [case[1] for case in cases], evidence_dir, sandbox)
    failures = [case[0] for index, case in enumerate(cases)
                if output is None or canonical(output[index]) != canonical(case[2])]
    return {"passed": not failures, "cases": len(cases), "failed_cases": failures,
            "valid_output": output is not None,
            "sandbox_status": receipt.get("status"), "sandbox_receipt": receipt.get("receipt_path"),
            "source_sha256": digest(code), "image_digest": image, "sandbox_run_id": receipt.get("run_id")}


class Releases:
    """Store immutable release records and a transactional activation pointer."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY, release TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS active (slot INTEGER PRIMARY KEY CHECK(slot=1), "
                "version TEXT NOT NULL REFERENCES versions(id));"
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=6)
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def read_version(db, version):
        if version is None:
            return None
        row = db.execute("SELECT release FROM versions WHERE id=?", (version,)).fetchone()
        if row is None:
            raise ValueError("Release is missing")
        release = json.loads(row[0])
        if release["id"] != version or digest(release["code"]) != release["source_sha256"]:
            raise ValueError("Release source does not match the installed hash")
        return release

    @staticmethod
    def current(db):
        row = db.execute("SELECT version FROM active WHERE slot=1").fetchone()
        return row[0] if row else None

    def active(self):
        with closing(self.connect()) as db:
            return self.read_version(db, self.current(db))

    def snapshot(self, path):
        staging = Releases(path)
        with closing(self.connect()) as source, closing(staging.connect()) as target:
            source.backup(target)
        return staging

    def install(self, release):
        if digest(release["code"]) != release["source_sha256"]:
            raise ValueError("Release source changed since testing")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.current(db) != release["previous_version"]:
                raise ValueError("Active release changed during qualification")
            db.execute("INSERT INTO versions VALUES(?,?)", (release["id"], canonical(release)))
            db.execute("INSERT OR REPLACE INTO active VALUES(1,?)", (release["id"],))

    def rollback(self, version):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if self.current(db) != version:
                raise ValueError("Requested rollback does not match the active version")
            old = self.read_version(db, version)["previous_version"]
            if old is None:
                db.execute("DELETE FROM active WHERE slot=1")
            else:
                self.read_version(db, old)
                db.execute("UPDATE active SET version=? WHERE slot=1", (old,))
        return old


def active_version(registry):
    release = registry.active()
    return release["id"] if release else None


def run_active(registry, inputs, evidence_dir, sandbox):
    release = registry.active()
    if release is None:
        raise LookupError("The frame inventory tool has no active release")
    if not isinstance(inputs, list):
        raise ValueError("Expected a JSON array of frame inventory requests")
    receipt, output = evaluate(release["code"], release["image_digest"], inputs, evidence_dir, sandbox)
    if output is None:
        raise RuntimeError("Active tool returned an invalid sandbox result: " + str(receipt.get("receipt_path")))
    return {"version": release["id"], "source_sha256": release["source_sha256"],
            "output": output, "sandbox_receipt": receipt["receipt_path"]}


def smoke_input(root):
    path = root / TAPE
    if (path / "status.json").is_file():
        status = read(path / "status.json")
        if status.get("status") == "completed" and status.get("live_orders_submitted") == 0:
            indices = []
            for item in (path / "frames").glob("frame-*.json"):
                match = re.fullmatch(r"frame-([0-9]{1,5})\.json", item.name)
                if match and item.is_file():
                    indices.append(int(match[1]))
            return {"frame_count": status["completed_frames"], "indices": sorted(indices)}, TAPE.name
    return {"frame_count": 4, "indices": [0, 2, 4, 2, 1]}, "synthetic frame inventory"


def resolve_image():
    import subprocess

    proc = subprocess.run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"],
                          capture_output=True, text=True, timeout=10, check=True)
    image = proc.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Sandbox image does not have a local immutable digest")
    return image


def build_candidate(directory, job_id, docs, attempt, feedback, model=MODEL):
    instructions = (docs + "\n\n" + CONTRACT + feedback + "\n\nReturn the complete Python source "
                    "and implementation notes now, following this response schema: " + canonical(AUTHOR_SCHEMA))
    payload = {"model": model, "stream": False, "format": AUTHOR_SCHEMA, "think": False,
               "messages": [{"role": "system", "content": "You are Sabrina's local tool engineer. "
                             "Author the requested program. Development examples illustrate the program's behavior."},
                            {"role": "user", "content": instructions}],
               "options": {"num_ctx": 8192, "num_predict": 1800, "temperature": 0}}
    with caller(PILOT, "sabrina-tool-development"):
        client = configured()
        if client is None:
            raise RuntimeError("Lab coordinator is unavailable")
        body = {"client": client.client, "project": client.project, "operation_id": f"{job_id}-author-{attempt}",
                "path": "/api/chat", "payload": payload, "device": "gpu", "request_seconds": 210,
                "task_label": "Sabrina builds paper frame inventory"}
        save(directory / "inference-intent.json", body)
        request = client.request("/v1/inference", body)
        save(directory / "inference-receipt.json", {"id": request["id"], "model": model})
        reply = client.wait(request["id"], seconds=275)
    raw = reply.get("message", {}).get("content", "")
    (directory / "response.txt").write_bytes(raw.encode("utf-8"))
    if not raw.strip():
        raise ValueError("Coordinator completed without returning candidate source")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.keys() != AUTHOR_SCHEMA["properties"].keys():
        raise ValueError("Author response does not match the source and notes format")
    code, notes = data.get("code"), data.get("notes")
    if not isinstance(code, str) or not code.strip() or len(code.encode("utf-8")) > 16384:
        raise ValueError("Model did not return bounded candidate source")
    if not isinstance(notes, str) or not notes.strip() or len(notes) > 2000:
        raise ValueError("Model did not return the requested implementation note")
    (directory / "candidate.py").write_bytes(code.encode("utf-8"))
    save(directory / "authorship.json", {"model": reply.get("model", model), "request_id": request["id"],
                                         "source_sha256": digest(code), "notes": notes,
                                         "prompt_eval_count": reply.get("prompt_eval_count"),
                                         "eval_count": reply.get("eval_count"),
                                         "response_sha256": digest(raw)})
    return code, request["id"]


def release_path(root):
    return root / "data/sandbox_tools/paper-frame-inventory.sqlite3"


def repair_brief(root, parent, directory, image, sandbox):
    prior = root / "artifacts/swarm_tool_pilot" / parent
    failed = read(prior / "result.json")
    source_job = parent
    response_recovery = not failed["authors"]
    for _ in range(2):
        if failed["authors"] or not failed.get("repair_of"):
            break
        # A source-less response cannot become the basis of a code repair.
        source_job = repair_payload(failed["repair_of"])["repair_of"]
        prior = prior.parent / source_job
        failed = read(prior / "result.json")
    if not failed["authors"]:
        raise ValueError("No authored source was retained for this linked repair")
    author = failed["authors"][-1]
    if failed["status"] != "failed" or failed["active"] or author.get("passed") is not False:
        raise ValueError("Repair requires a previous, unqualified candidate")
    code = (prior / f"author-{author['attempt']}" / "candidate.py").read_bytes().decode("utf-8")
    if digest(code) != author["source_sha256"]:
        raise ValueError("Prior candidate differs from the recorded failed source")
    request = {"frame_count": 3, "indices": [5, 1, 5, 1, 2]}
    inputs = [None, request, {"frame_count": True, "indices": [False]}, {"frame_count": 0, "indices": []}]
    expected = [{"error": "invalid_input"}, reference(request), {"error": "invalid_input"}, inventory()]
    validation_repair = bool(failed.get("repair_of"))
    if validation_repair:
        inputs += [
            {"frame_count": 2, "indices": [0, True]},
            {"frame_count": 2, "indices": [1] * 4098},
            {"frame_count": 3, "indices": [0, 1, 2], "label": "extra"},
            {"frame_count": 2},
        ]
        expected += [{"error": "invalid_input"}] * 4
    receipt, actual = evaluate(code, image, inputs, directory / "sandboxes", sandbox)
    save(directory / "repair-reproduction.json", {
        "repair_of": parent, "source_job": source_job, "prior_source_sha256": digest(code), "inputs": inputs,
        "expected": expected, "actual": actual, "sandbox_receipt": receipt["receipt_path"],
    })
    displayed = [
        {**x, "indices": "<4098 copies of integer 1>"} if isinstance(x, dict) and len(x.get("indices", ())) > 4096 else x
        for x in inputs
    ]
    brief = (
        "\n\nRepair the previous Sabrina submission below. The independent verifier stays unchanged. "
        "This development example is separate from the independent qualification cases.\n"
        f"Previous source:\n{code}\n"
        f"Development input (long list abbreviated): {canonical(displayed)}\n"
        f"Required output: {canonical(expected)}\n"
        f"Actual output: {canonical(actual)[:1400]}\n"
        f"Sandbox error on this input: {receipt.get('stderr', '')[-1250:]}\n"
        "Validate each element's object shape before looking up fields. Preserve integer value "
        "types in all result lists, deduplicate unexpected values, and continue after invalid elements. "
        "Re-read all limits and the exact key requirements in the contract before returning your revision. "
        "In notes, name an applicable rule from the sandbox skill and one from the lifecycle guide."
    )
    if validation_repair:
        brief += (
            "\nThe previously tested inventory arithmetic works for valid requests. Preserve it. "
            "Focus the revision on input validation before any arithmetic. The set of keys must "
            "equal exactly frame_count and indices. An absent indices key is an error, so do not "
            "use a default. Check each number with type(value) is int. "
            "In Python, isinstance(True, int) accepts a boolean. Check the list length before reading "
            "its elements, and return invalid_input for more than 4096 elements. Review the "
            "development examples, including the boolean index after a valid integer."
        )
    return brief, VALIDATION_MODEL if validation_repair and not response_recovery else REPAIR_MODEL


def run_pilot(payload, job_id, *, root=ROOT, sandbox=tool_sandbox.execute_candidate,
              author=build_candidate, get_image=resolve_image):
    parent = payload.get("repair_of")
    if payload != (repair_payload(parent) if parent else PAYLOAD) or not re.fullmatch(r"job_[0-9a-f]{32}", job_id):
        raise ValueError("This worker accepts only the authorized paper tool pilot")
    directory = root / "artifacts/swarm_tool_pilot" / job_id
    directory.mkdir(parents=True, exist_ok=False)
    report_file, sandboxes = directory / "result.json", directory / "sandboxes"
    report = {"pilot": PILOT, "job_id": job_id, "started_at": time.time(), "status": "preparing",
              "authors": [], "active": False}
    if parent:
        report["repair_of"] = parent
    save(report_file, report)
    try:
        image = get_image()
        cases = independent_cases()
        save(directory / "independent-cases.json", cases)
        report["independent_cases_sha256"] = digest(canonical(cases))
        bad_example = [inventory([0])]
        negative = qualify(f"print({canonical(bad_example)!r})\n", image, cases[:1], sandboxes, sandbox)
        report["negative_control"] = negative
        if negative["passed"] or not negative["valid_output"]:
            raise RuntimeError("Independent verifier did not reject an incorrect zero-exit program")
        docs = development_context(root=root)
        (directory / "required-documents.txt").write_bytes(docs.encode("utf-8"))
        report.update(status="building", documentation_sha256=digest(docs))
        save(report_file, report)
        feedback, model = repair_brief(root, parent, directory, image, sandbox) if parent else ("", MODEL)
        candidate_dir = directory / "author-1"
        candidate_dir.mkdir()
        code, request_id = author(candidate_dir, job_id, docs, 1, feedback, model=model)
        verification = qualify(code, image, cases, sandboxes, sandbox)
        report["authors"].append({"attempt": 1, "request_id": request_id, "model": model, **verification})
        save(report_file, report)
        if not verification["passed"]:
            raise RuntimeError("Sabrina's candidate did not pass independent qualification")

        registry = Releases(release_path(root))
        previous = active_version(registry)
        release = {"id": job_id, "previous_version": previous, "code": code,
                   "source_sha256": verification["source_sha256"], "image_digest": image,
                   "inference_request": request_id, "qualification": str(report_file)}
        save(directory / "release.json", {k: v for k, v in release.items() if k != "code"})
        sample, tape = smoke_input(root)
        expected = [reference(sample)]
        rehearsal = registry.snapshot(directory / "rollback-rehearsal.sqlite3")
        prior_output = run_active(rehearsal, [sample], sandboxes, sandbox)["output"] if previous else None
        rehearsal.install(release)
        staged = run_active(rehearsal, [sample], sandboxes, sandbox)
        empty_batch = run_active(rehearsal, [], sandboxes, sandbox)
        restored = rehearsal.rollback(job_id)
        if restored != previous or active_version(rehearsal) != previous:
            raise RuntimeError("Rollback did not restore the prior release")
        if previous:
            after = run_active(rehearsal, [sample], sandboxes, sandbox)["output"]
            if canonical(after) != canonical(prior_output):
                raise RuntimeError("Restored release behavior changed")
        else:
            try:
                run_active(rehearsal, [sample], sandboxes, sandbox)
            except LookupError:
                pass
            else:
                raise RuntimeError("Rolled-back tool remained callable")
        if canonical(staged["output"]) != canonical(expected):
            raise RuntimeError("Staged tool failed the paper tape smoke test")
        report.update(status="qualified", staged_smoke=staged, empty_batch_smoke=empty_batch, rollback={
            "verified": True, "test_registry": str(rehearsal.path), "restored_version": previous,
            "restored_state": "previous_release" if previous else "uninstalled",
        })
        save(report_file, report)
        registry.install(release)
        try:
            live = run_active(registry, [sample], sandboxes, sandbox)
            if live["version"] != job_id or canonical(live["output"]) != canonical(expected):
                raise RuntimeError("Installed tool failed the post-release smoke test")
            report.update(status="completed", active=True, live_smoke=live, smoke_tape=tape,
                          smoke_frame_count=sample["frame_count"], release=job_id,
                          finished_at=time.time())
            save(report_file, report)
        except Exception:
            registry.rollback(job_id)
            report["deployment_rolled_back"] = active_version(registry) == previous
            report["active"] = not report["deployment_rolled_back"]
            raise
    except Exception as exc:
        report.update(status="failed", error=str(exc), finished_at=time.time())
        save(report_file, report)
        raise RuntimeError(f"{exc}. Evidence at {report_file}") from exc
    return {"status": "completed", "summary": f"Activated Sabrina's {PILOT} after independent tests and rollback rehearsal.",
            "release": job_id, "source_sha256": release["source_sha256"],
            "independent_cases_passed": verification["cases"], "rollback_verified": True,
            "inference_request": request_id, "evidence_path": str(report_file)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("schedule").add_argument("--repair-of")
    commands.add_parser("work").add_argument("job_id")
    commands.add_parser("run", help="Read a JSON array of requests from stdin")
    commands.add_parser("status")
    commands.add_parser("rollback").add_argument("version")
    args = parser.parse_args()

    if args.action in ("schedule", "work"):
        from sabrina_social.scheduler import JobStore, Scheduler

        jobs = JobStore(ROOT / "data/social/scheduler.sqlite3")
        if args.action == "schedule":
            import fcntl

            payload = PAYLOAD
            if args.repair_of:
                payload = repair_payload(args.repair_of)
                prior = jobs.get(args.repair_of)
                if not prior or prior["capability"] != CAPABILITY or prior["status"] != "failed":
                    raise ValueError("Repair requires a failed pilot job")
                last = prior
                for previous_repairs in range(4):
                    if last["payload"] == PAYLOAD:
                        break
                    ancestor_id = last["payload"].get("repair_of")
                    if not ancestor_id or last["payload"] != repair_payload(ancestor_id):
                        raise ValueError("Repair lineage does not match")
                    last = jobs.get(ancestor_id)
                    if not last or last["capability"] != CAPABILITY or last["status"] != "failed":
                        raise ValueError("Repair lineage does not match")
                else:
                    raise ValueError("This pilot's bounded repair sequence is exhausted")
                if previous_repairs >= 2:
                    evidence = ROOT / "artifacts/swarm_tool_pilot" / prior["id"]
                    failed = read(evidence / "result.json")
                    if failed["authors"] or not (evidence / "author-1/response.txt").is_file():
                        raise ValueError("The remaining recovery is for source-less author responses")
            # Serialize equivalent submissions on the Linux scheduler host.
            with jobs.path.with_name("paper-frame-inventory.submit.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with jobs.db() as db:
                    row = db.execute("SELECT id FROM scheduled_jobs WHERE capability=? AND payload=? "
                                     "ORDER BY created DESC LIMIT 1", (CAPABILITY, json.dumps(payload))).fetchone()
                job = jobs.get(row[0]) if row else jobs.create(
                    CAPABILITY, payload, [], time.time(), draft=True, max_attempts=1)
        else:
            adapter = lambda payload: run_pilot(payload, args.job_id)
            scheduler = Scheduler(store=jobs, adapters={CAPABILITY: adapter})
            job = scheduler.dispatch_once(draft=(args.job_id, CAPABILITY))
            if job is None:
                raise SystemExit("No matching pilot draft was available to claim")
        result = {k: job.get(k) for k in ("id", "status", "result", "last_error")}
    else:
        registry = Releases(release_path(ROOT))
        if args.action == "run":
            data = sys.stdin.buffer.read(128*1024 + 1)
            if len(data) > 128*1024:
                raise ValueError("Frame inventory input exceeds 128 KiB")
            result = run_active(registry, json.loads(data), ROOT / "artifacts/tool_sandbox_runs",
                                tool_sandbox.execute_candidate)
        elif args.action == "rollback":
            result = {"restored_version": registry.rollback(args.version)}
        else:
            release = registry.active()
            result = {k: v for k, v in release.items() if k != "code"} if release else {"active": None}
    print(json.dumps(result, allow_nan=False))
    return int(args.action == "work" and result["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
