# -*- coding: utf-8 -*-
"""
tools/ai_service_manager.py
===========================
Autonomous AI Services Health & Self-Healing Manager for Sabrina.

Monitors and recovers:
1. Local Ollama inference server (`http://127.0.0.1:11434/api/ps` & `/api/tags`).
2. Sabrina Lab Coordinator (`http://127.0.0.1:8090` / `control.sqlite3` stale inference requests & leases).
3. Systemd user services (`sabrina-lab-coordinator`, `sabrina-social-scheduler`, `sabrina-social-pulse`, `sabrina-social-api`, `sabrina-availability`).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List


class AIServiceManager:
    def __init__(
        self,
        ollama_url: str | None = None,
        coordinator_url: str | None = None,
        control_db_path: str | Path | None = None,
    ):
        self.ollama_url = (ollama_url or os.environ.get("SABRINA_LINKEDIN_OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.coordinator_url = (coordinator_url or os.environ.get("SABRINA_CONTROL_URL", "http://127.0.0.1:8090")).rstrip("/")
        default_ctrl = Path(os.environ.get("SABRINA_CONTROL_DB", "data/control/coordinator.sqlite3"))
        self.control_db_path = Path(control_db_path) if control_db_path else default_ctrl

    def check_ollama(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"url": self.ollama_url, "reachable": False, "running_models": [], "installed_count": 0}
        try:
            with urllib.request.urlopen(self.ollama_url + "/api/ps", timeout=4) as resp:
                ps_data = json.loads(resp.read().decode("utf-8"))
                models = ps_data.get("models") or []
                result["reachable"] = True
                result["running_models"] = [
                    {
                        "name": m.get("name"),
                        "size_vram": m.get("size_vram", 0),
                        "expires_at": m.get("expires_at"),
                    }
                    for m in models
                ]
        except Exception as exc:
            result["error"] = str(exc)
            return result

        try:
            with urllib.request.urlopen(self.ollama_url + "/api/tags", timeout=4) as resp:
                tags_data = json.loads(resp.read().decode("utf-8"))
                result["installed_count"] = len(tags_data.get("models") or [])
        except Exception:
            pass
        return result

    def check_and_heal_control_db(self, heal_stale_seconds: int = 900) -> Dict[str, Any]:
        """Inspect only; the coordinator owns request and lease recovery.

        Keep the historical argument for callers, but never infer executor death
        from age, queued social work, or an expired lease.
        """
        info: Dict[str, Any] = {
            "db_path": str(self.control_db_path),
            "exists": self.control_db_path.is_file(),
            "queued_requests": 0,
            "running_requests": 0,
            "stale_requests_healed": 0,
            "stale_requests_detected": 0,
            "expired_leases_detected": 0,
            "recovery_required": False,
            "recovery_policy": "Coordinator recovery requires verified executor shutdown; health reads never release leases.",
            "active_leases": [],
        }
        if not info["exists"]:
            return info
        now = time.time()
        try:
            uri = self.control_db_path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as db:
                db.row_factory = sqlite3.Row
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                req_table = "control_requests" if "control_requests" in tables else "requests" if "requests" in tables else None
                if req_table:
                    cols = {r[1] for r in db.execute(f"PRAGMA table_info({req_table})").fetchall()}
                    ts_col = "updated" if "updated" in cols else "created"
                    for row in db.execute(f"SELECT state, COUNT(*) AS cnt FROM {req_table} GROUP BY state").fetchall():
                        st = str(row["state"])
                        if st == "queued":
                            info["queued_requests"] = int(row["cnt"])
                        elif st in ("running", "claimed"):
                            info["running_requests"] += int(row["cnt"])

                    if heal_stale_seconds > 0 and ts_col in cols:
                        cutoff = now - max(180, int(heal_stale_seconds))
                        count = db.execute(
                            f"""SELECT COUNT(*) FROM {req_table}
                                WHERE state IN ('queued', 'running', 'claimed') AND {ts_col} < ?""",
                            (cutoff,),
                        ).fetchone()[0]
                        info["stale_requests_detected"] = int(count)

                lease_table = "control_leases" if "control_leases" in tables else "leases" if "leases" in tables else None
                if lease_table:
                    cols = {r[1] for r in db.execute(f"PRAGMA table_info({lease_table})").fetchall()}
                    state_column = "state" if "state" in cols else "'unavailable' AS state"
                    for row in db.execute(f"SELECT resource, owner, expires, {state_column} FROM {lease_table}").fetchall():
                        expired = float(row["expires"]) <= now
                        info["expired_leases_detected"] += int(expired)
                        info["recovery_required"] |= expired or row["state"] == "uncertain"
                        info["active_leases"].append({
                            "resource": row["resource"],
                            "owner": row["owner"],
                            "remaining_sec": round(max(0.0, float(row["expires"]) - now), 1),
                            "state": row["state"],
                            "expired": expired,
                        })
        except Exception as exc:
            info["error"] = str(exc)
        return info

    def check_systemd_services(self, auto_restart: bool = False) -> Dict[str, Any]:
        units = [
            "sabrina-lab-coordinator.service",
            "sabrina-availability.service",
            "sabrina-social-api.service",
            "sabrina-social-scheduler.service",
            "sabrina-social-pulse.service",
        ]
        statuses: Dict[str, Any] = {}
        restarted: List[str] = []
        if os.name == "nt":
            return {"platform": "windows", "units": statuses, "restarted": restarted}
        for unit in units:
            try:
                proc = subprocess.run(
                    ["systemctl", "--user", "is-active", unit],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    check=False,
                )
                state = (proc.stdout or "").strip() or "unknown"
                statuses[unit] = state
                if auto_restart and state not in ("active", "activating"):
                    subprocess.run(
                        ["systemctl", "--user", "restart", unit],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    restarted.append(unit)
            except Exception as exc:
                statuses[unit] = f"error: {exc}"
        return {"platform": "linux", "units": statuses, "restarted": restarted}

    def check_and_heal_containers(self, auto_heal: bool = True) -> Dict[str, Any]:
        """Audits docker container health statuses and restarts unhealthy/dead containers."""
        if os.name == "nt":
            return {"platform": "windows", "audited_containers": 0}
        try:
            from tools.sabrina_container_autoheal import run_autoheal_cycle
            return run_autoheal_cycle()
        except Exception as exc:
            return {"error": str(exc)}

    def health_and_recover(self, heal: bool = True) -> Dict[str, Any]:
        ollama = self.check_ollama()
        control = self.check_and_heal_control_db(heal_stale_seconds=600 if heal else 0)
        services = self.check_systemd_services(auto_restart=heal)
        containers = self.check_and_heal_containers(auto_heal=heal)
        healthy = (
            bool(ollama.get("reachable"))
            and not control.get("error")
            and not control.get("recovery_required")
            and not containers.get("error")
        )
        return {
            "status": "healthy" if healthy else "degraded",
            "observed_at": time.time(),
            "ollama": ollama,
            "coordinator": control,
            "services": services,
            "containers": containers,
        }
