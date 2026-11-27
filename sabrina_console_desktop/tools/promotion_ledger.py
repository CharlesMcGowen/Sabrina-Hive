"""Two-tier ledger for swarm output: speculative proposals and checker-finalized results.

Agents append artifacts freely to speculative.jsonl. An artifact reaches finalized.jsonl
only when the deterministic checker registered for its kind accepts it. Votes, author
counts and model agreement never finalize anything.

Contract: proofs/promotion_gate_mission_brief.md
Tests:    test_promotion_ledger_acceptance.py
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

Checker = Callable[[dict], tuple[bool, str]]


def _canonical(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not JSON-serializable: {exc}") from exc


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PromotionLedger:
    def __init__(self, root: Path | str, checkers: dict[str, Checker]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.checkers = dict(checkers)
        self._speculative = self.root / "speculative.jsonl"
        self._finalized = self.root / "finalized.jsonl"
        self._lock = threading.Lock()

    # --- storage ---------------------------------------------------------
    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _append_line(path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _artifacts(self) -> dict[str, dict]:
        return {r["artifact_id"]: r for r in self._read(self._speculative) if r.get("type") == "artifact"}

    # --- speculative log -------------------------------------------------
    def append(self, kind: str, payload: dict, *, author: str, slot: str, parents: Iterable[str] = ()) -> str:
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict")
        if not (isinstance(kind, str) and kind and isinstance(slot, str) and slot):
            raise ValueError("kind and slot must be non-empty strings")
        parent_ids = sorted(set(parents))
        with self._lock:
            known = self._artifacts()
            for parent in parent_ids:
                if parent not in known:
                    raise ValueError(f"unknown parent {parent}")
            artifact_id = _sha(_canonical({"kind": kind, "slot": slot, "payload": payload, "parents": parent_ids}))
            if artifact_id not in known:
                self._append_line(self._speculative, {
                    "type": "artifact", "artifact_id": artifact_id, "kind": kind, "slot": slot,
                    "payload": payload, "parents": parent_ids, "author": str(author), "ts": time.time(),
                })
        return artifact_id

    def rejections(self, artifact_id: str) -> list[dict]:
        return [r for r in self._read(self._speculative)
                if r.get("type") == "rejection" and r.get("artifact_id") == artifact_id]

    def _reject(self, artifact_id: str, error: str) -> dict:
        self._append_line(self._speculative,
                          {"type": "rejection", "artifact_id": artifact_id, "error": error, "ts": time.time()})
        return {"ok": False, "artifact_id": artifact_id, "error": error}

    # --- finalized log ---------------------------------------------------
    def finalized(self) -> list[dict]:
        return self._read(self._finalized)

    def is_finalized(self, artifact_id: str) -> bool:
        return any(r.get("artifact_id") == artifact_id for r in self.finalized())

    def promote(self, artifact_id: str) -> dict:
        with self._lock:
            artifact = self._artifacts().get(artifact_id)
            if artifact is None:
                return {"ok": False, "artifact_id": artifact_id, "error": "unknown artifact"}
            done = self.finalized()
            if any(r["artifact_id"] == artifact_id for r in done):
                return {"ok": True, "artifact_id": artifact_id, "error": ""}
            for record in done:
                if record["slot"] == artifact["slot"]:
                    return self._reject(
                        artifact_id, f"slot {artifact['slot']!r} already finalized by {record['artifact_id']}")
            done_ids = {r["artifact_id"] for r in done}
            for parent in artifact["parents"]:
                if parent not in done_ids:
                    return self._reject(artifact_id, f"parent {parent} is not finalized")
            checker = self.checkers.get(artifact["kind"])
            if checker is None:
                return self._reject(artifact_id, f"no checker registered for kind {artifact['kind']!r}")
            try:
                ok, detail = checker(artifact["payload"])
            except Exception as exc:  # a broken checker is a rejection, never a crash
                return self._reject(artifact_id, f"checker raised {type(exc).__name__}: {exc}")
            if not ok:
                return self._reject(artifact_id, str(detail) or "check failed")
            record = {
                "artifact_id": artifact_id, "kind": artifact["kind"], "slot": artifact["slot"],
                "payload": artifact["payload"], "parents": artifact["parents"], "detail": str(detail),
                "seq": len(done), "prev": done[-1]["hash"] if done else "", "ts": time.time(),
            }
            record["hash"] = _sha(_canonical(record))
            self._append_line(self._finalized, record)
            return {"ok": True, "artifact_id": artifact_id, "error": ""}

    def verify(self) -> dict:
        """Check the finalized hash chain: any edited, removed or reordered record breaks it."""
        prev = ""
        try:
            for index, record in enumerate(self.finalized()):
                body = {k: v for k, v in record.items() if k != "hash"}
                if record.get("seq") != index or record.get("prev") != prev or record.get("hash") != _sha(_canonical(body)):
                    return {"ok": False, "error": f"finalized record {index} does not match the hash chain"}
                prev = record["hash"]
        except (ValueError, AttributeError, TypeError) as exc:
            return {"ok": False, "error": f"finalized log is unreadable: {exc}"}
        return {"ok": True, "error": ""}
