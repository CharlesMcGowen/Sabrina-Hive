"""Thread-safe shared task queue, findings store, and run log for Rogue Colony workers."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional


class ColonyBlackboard:
    """In-memory thread-safe blackboard for coordinating swarm worker tasks and outputs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: List[Dict[str, Any]] = []
        self._claimed: set[str] = set()
        self._findings: List[Dict[str, Any]] = []
        self._log: List[str] = []

    def enqueue(self, task_id: str, payload: Dict[str, Any]) -> bool:
        with self._lock:
            if any(item.get("task_id") == task_id for item in self._queue):
                return False
            self._queue.append({"task_id": task_id, "created_at": time.time(), **payload})
            return True

    def claim_task(self, worker_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for item in self._queue:
                tid = str(item.get("task_id", ""))
                if tid and tid not in self._claimed:
                    self._claimed.add(tid)
                    return {"worker_id": worker_id, **item}
            return None

    def record_finding(self, finding: Dict[str, Any]) -> None:
        with self._lock:
            self._findings.append({"recorded_at": time.time(), **finding})

    def list_findings(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._findings)

    def append_log(self, message: str) -> None:
        with self._lock:
            self._log.append(message)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "queued": len(self._queue),
                "claimed": len(self._claimed),
                "findings": len(self._findings),
                "log_lines": len(self._log),
            }
