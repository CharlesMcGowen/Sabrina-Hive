"""Validate and release isolated Python workers without restarting the mission coordinator."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sabrina_console_desktop.recovery_worker_process import CUT, MAX_CONTEXT, MAX_SOURCE


DEFAULT_ROOT = Path(__file__).resolve().parent / "data" / "control" / "recovery_workers"
BUILTIN = "builtin"
FAULTS = frozenset({"other", "timeout", "context_overflow"})
_ID = re.compile(r"[0-9a-f]{64}|builtin")
_PROCESS = Path(__file__).with_name("recovery_worker_process.py").resolve()


class WorkerReleaseError(ValueError):
    """The worker could not be validated, loaded, or promoted."""


@dataclass(frozen=True)
class PreparedContext:
    context: str
    revision: str = BUILTIN
    warning: str = ""


def classify_failure(error: str) -> str:
    text = error.casefold()
    if any(term in text for term in ("context length", "context window", "context overflow", "input too long", "too many tokens")):
        return "context_overflow"
    if any(term in text for term in ("timed out", "timeout", "deadline exceeded")):
        return "timeout"
    return "other"


def compact_source(fault: str) -> str:
    if fault not in FAULTS - {"other"}:
        raise WorkerReleaseError("A context fault is required")
    return """def compact(context, limit, fault):
    if fault != 'other':
        budget = limit // 2
        if len(context) <= budget:
            return context
        room = budget - len(CUT)
        head = room // 2
    else:
        if len(context) <= limit:
            return context
        room = limit - len(CUT)
        head = (room + room + room) // 4
    tail = room - head
    return context[:head] + CUT + context[-tail:]
"""


def builtin_context(context: str, limit: int) -> str:
    if len(context) <= limit:
        return context
    budget = limit - len(CUT)
    head = budget * 3 // 4
    return context[:head] + CUT + context[-(budget - head):]


def _assert_preserved(context: str, result: str, limit: int) -> None:
    if not isinstance(result, str) or len(result) > limit:
        raise WorkerReleaseError("Recovery context is invalid or exceeds the limit")
    if result == context:
        return
    anchor = min(180, limit // 8, len(context) // 2)
    for delimiter in re.finditer(re.escape(CUT), result):
        head, tail = result[:delimiter.start()], result[delimiter.end():]
        if (len(head) >= anchor and len(tail) >= anchor and context.startswith(head)
            and context.endswith(tail) and len(head) + len(tail) < len(context)):
            return
    raise WorkerReleaseError("Worker altered or omitted protected context")


def _run_worker(source: str, cases: list[dict]) -> list[str]:
    interpreter = Path(sys.executable)
    if interpreter.name.lower() == "pythonw.exe":
        interpreter = interpreter.with_name("python.exe")
    command = [str(interpreter), "-I", "-S", "-B", str(_PROCESS)]
    env = {k: v for k, v in os.environ.items() if k.casefold() in {"systemroot", "windir"}}
    try:
        completed = subprocess.run(
            command, input=json.dumps({"source": source, "cases": cases}), capture_output=True,
            encoding="utf-8", env=env, cwd=_PROCESS.parent, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(completed.stdout)
        if completed.returncode:
            raise WorkerReleaseError(payload.get("error") or "Recovery worker process failed")
        results = payload["results"]
        if not isinstance(results, list) or len(results) != len(cases):
            raise WorkerReleaseError("Recovery worker returned an invalid batch")
    except WorkerReleaseError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
        raise WorkerReleaseError("Recovery worker process did not return a result") from error
    for case, result in zip(cases, results):
        _assert_preserved(case["context"], result, case["limit"])
    return results


def _validate(source: str) -> None:
    short = "Goal: preserve evidence.\nContext: café and 日本語.\nOutput contract: JSON."
    long = "Important task and input requirements.\n" + ("diagnostic ✓\n" * 1550) + "\nRequired answer format and source evidence."
    cases = [{"context": short, "limit": 1000, "fault": fault} for fault in sorted(FAULTS)]
    cases.extend({"context": long, "limit": limit, "fault": fault} for limit in (1000, 6000, 12000) for fault in sorted(FAULTS))
    results = _run_worker(source, cases)
    for case, result in zip(cases, results):
        if case["fault"] == "other" or case["context"] == short:
            if result != builtin_context(case["context"], case["limit"]):
                raise WorkerReleaseError("Worker regressed the healthy-context check")


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _promotion_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as file:
        if os.name == "nt":
            import msvcrt

            def lock():
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock():
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(file, fcntl.LOCK_UN)

        deadline = time.monotonic() + 3
        while True:
            try:
                lock()
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise WorkerReleaseError("Worker promotion is busy") from error
                time.sleep(0.05)
        try:
            yield
        finally:
            unlock()


def read_source(path: Path) -> str:
    with path.open("rb") as source:
        content = source.read(MAX_SOURCE + 1)
    if len(content) > MAX_SOURCE:
        raise WorkerReleaseError("Worker source is too large")
    return content.decode("utf-8")


def _worker_case(context: str, limit: int, fault: str) -> dict:
    if len(context) > MAX_CONTEXT:
        context = context[:MAX_CONTEXT // 2] + context[-MAX_CONTEXT // 2:]
    return {"context": context, "limit": limit, "fault": fault}


class RecoveryWorkerReleases:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._validated: dict[str, str] = {}
        self._cache_lock = threading.Lock()

    def _pointer(self) -> tuple[str, str]:
        try:
            with (self.root / "active.json").open("rb") as state:
                payload = json.loads(state.read(512))
        except FileNotFoundError:
            return BUILTIN, BUILTIN
        if not isinstance(payload, dict) or set(payload) != {"active", "previous"}:
            raise WorkerReleaseError("Invalid recovery worker pointer")
        active, previous = payload["active"], payload["previous"]
        if not all(isinstance(value, str) and _ID.fullmatch(value) for value in (active, previous)):
            raise WorkerReleaseError("Invalid recovery worker revision")
        return active, previous

    @property
    def active_revision(self) -> str:
        return self._pointer()[0]

    def _source(self, revision: str) -> str:
        with self._cache_lock:
            known = self._validated.get(revision)
            if known is not None:
                return known
            source = read_source(self.root / f"{revision}.py")
            if hashlib.sha256(source.encode("utf-8")).hexdigest() != revision:
                raise WorkerReleaseError("Recovery worker digest mismatch")
            _validate(source)
            if len(self._validated) >= 8:
                self._validated.clear()
            self._validated[revision] = source
            return source

    def _prepare(self, revision: str, case: dict) -> str:
        if revision == BUILTIN:
            return builtin_context(case["context"], case["limit"])
        return _run_worker(self._source(revision), [case])[0]

    def prepare(self, context: str, limit: int, fault: str) -> PreparedContext:
        case = _worker_case(context, limit, fault)
        context = case["context"]
        try:
            active, previous = self._pointer()
        except (OSError, ValueError):
            return PreparedContext(builtin_context(context, limit), warning="Invalid pointer. Used built-in worker.")
        warning = ""
        for revision in dict.fromkeys((active, previous, BUILTIN)):
            try:
                return PreparedContext(self._prepare(revision, case), revision, warning)
            except (OSError, ValueError):
                warning = "Active worker failed validation. Used the preceding available release."
        raise AssertionError("Built-in recovery worker should always be available")

    def promote(self, source: str, expected_revision: str, *, improve_fault: str = "",
                live_context: str = "", limit: int = 6000,
                cancel: threading.Event | None = None) -> str:
        if not _ID.fullmatch(expected_revision):
            raise WorkerReleaseError("Invalid expected recovery worker revision")
        _validate(source)
        revision = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if improve_fault:
            if improve_fault not in FAULTS - {"other"}:
                raise WorkerReleaseError("Unknown recovery fault")
            case = {"context": "Preserve goal.\n" + "diagnostics\n" * 1200 + "\nPreserve final format.", "limit": 6000,
                    "fault": improve_fault}
            before = self._prepare(expected_revision, case)
            after = _run_worker(source, [case])[0]
            if len(after) >= len(before) or len(after) > 4500:
                raise WorkerReleaseError("Candidate did not reduce the diagnosed context load")
            for other in FAULTS - {improve_fault, "other"}:
                peer_case = {**case, "fault": other}
                earlier = self._prepare(expected_revision, peer_case)
                proposed = _run_worker(source, [peer_case])[0]
                if len(proposed) > len(earlier):
                    raise WorkerReleaseError("Candidate regressed a previously supported recovery fault")
            if live_context:
                live_case = _worker_case(live_context, limit, improve_fault)
                old_context = self._prepare(expected_revision, live_case)
                new_context = _run_worker(source, [live_case])[0]
                if len(new_context) >= len(old_context):
                    raise WorkerReleaseError("Candidate did not improve the active recovery request")
        with _promotion_lock(self.root / ".promotion.lock"):
            active, _ = self._pointer()
            if active != expected_revision:
                raise WorkerReleaseError("Active worker changed during validation")
            if cancel is not None and cancel.is_set():
                raise WorkerReleaseError("Worker promotion was cancelled")
            if revision != active:
                _atomic_write(self.root / f"{revision}.py", source.encode("utf-8"))
                if cancel is not None and cancel.is_set():
                    raise WorkerReleaseError("Worker promotion was cancelled")
                self._publish(revision, active)
        return revision

    def _publish(self, active: str, previous: str) -> None:
        state = json.dumps({"active": active, "previous": previous}) + "\n"
        _atomic_write(self.root / "active.json", state.encode("utf-8"))

    def rollback(self, expected_revision: str) -> str:
        active, previous = self._pointer()
        if previous != BUILTIN:
            self._source(previous)
        with _promotion_lock(self.root / ".promotion.lock"):
            if (active, previous) != self._pointer() or expected_revision != active:
                raise WorkerReleaseError("Active worker changed during rollback")
            if previous != active:
                self._publish(previous, active)
        return previous


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("status")
    commands.add_parser("validate").add_argument("source", type=Path)
    commands.add_parser("promote").add_argument("source", type=Path)
    commands.add_parser("rollback")
    args = parser.parse_args()
    store = RecoveryWorkerReleases(args.root)
    try:
        if args.action == "validate":
            _validate(read_source(args.source))
            print("Recovery worker validation passed")
        elif args.action == "promote":
            print(store.promote(read_source(args.source), store.active_revision))
        elif args.action == "rollback":
            print(store.rollback(store.active_revision))
        else:
            print(store.active_revision)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
