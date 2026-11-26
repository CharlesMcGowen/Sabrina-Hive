# -*- coding: utf-8 -*-
"""
PEP 562 Virtual Portal Package Dispatcher (`Sabrina/harnesses/external/__init__.py`)
=====================================================================================
Allows any registered portal in `sabrina_assistant_fresh.sqlite3` to be accessed
or imported dynamically as an attribute/module (`harnesses.external.<portal_slug>`)
without requiring a physical `.py` file on disk.
Powered by Guild E Swarm-Authored `resolve_virtual_portal_spec` (`swarm_system_bridges.py`).
"""
from __future__ import annotations

import types
from pathlib import Path
from typing import Any, Dict

from harnesses.external.swarm_system_bridges import resolve_virtual_portal_spec

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "sabrina_assistant_fresh.sqlite3"


class VirtualPortalModule(types.ModuleType):
    """Dynamic in-memory module representing a SQLite-registered external portal."""

    def __init__(self, name: str, spec: Dict[str, Any], db_path: Path = DEFAULT_DB_PATH) -> None:
        super().__init__(name)
        self.PORTAL_ID = spec["portal_id"]
        self.MODEL_ID = spec["model_id"]
        self.ENDPOINT_URL = spec.get("url") or ""
        self.QUALIFICATION_STATE = spec.get("qualification_state") or "unqualified"
        self.QUALIFIED_ROLES = spec.get("qualified_roles") or {}
        self._db_path = db_path
        self.__file__ = f"<virtual_sqlite_portal:{self.PORTAL_ID}>"

    def invoke(self, prompt: str, task_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        from tools.portal_transport import invoke_portal

        return invoke_portal(self.PORTAL_ID, prompt, task_id, db_path=self._db_path, **kwargs)


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(name)
    spec = resolve_virtual_portal_spec(str(DEFAULT_DB_PATH), name)
    if spec is not None:
        return VirtualPortalModule(name, spec, db_path=DEFAULT_DB_PATH)
    raise AttributeError(f"module {__name__!r} has no attribute or registered SQLite portal {name!r}")
