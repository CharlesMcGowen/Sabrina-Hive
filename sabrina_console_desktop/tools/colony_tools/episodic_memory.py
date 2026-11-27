"""Lightweight JSONL episodic outcome store for Rogue Colony workers."""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any, Dict, List


def _default_storage_dir() -> Path:
    try:
        from sabrina_config import DESKTOP_ROOT
        return DESKTOP_ROOT / "data" / "colony_storage"
    except ImportError:
        return Path(__file__).resolve().parents[2] / "data" / "colony_storage"


class EpisodicMemory:
    """Stores task execution outcomes to JSONL and recalls relevant prior runs by keyword overlap."""

    def __init__(self, storage_dir: Path | str | None = None) -> None:
        self.storage_dir = Path(storage_dir) if storage_dir else _default_storage_dir()
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.memory_file = self.storage_dir / "colony_memory.jsonl"
        self._entries: List[Dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.memory_file.is_file():
            return
        try:
            for line in self.memory_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._entries.append(json.loads(line))
        except Exception:
            pass

    def record(self, task_type: str, summary: str, ok: bool, metadata: Dict[str, Any] | None = None) -> Dict[str, Any]:
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "task_type": task_type,
            "summary": summary,
            "ok": bool(ok),
            "metadata": metadata or {},
        }
        self._entries.append(entry)
        with self.memory_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    def recall(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        tokens = {tok.lower() for tok in query.split() if len(tok) > 2}
        if not tokens:
            return list(reversed(self._entries[-limit:]))
        scored: List[tuple[int, Dict[str, Any]]] = []
        for entry in self._entries:
            haystack = f"{entry.get('task_type', '')} {entry.get('summary', '')}".lower()
            score = sum(1 for tok in tokens if tok in haystack)
            if score > 0:
                scored.append((score, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [pair[1] for pair in scored[:limit]]
