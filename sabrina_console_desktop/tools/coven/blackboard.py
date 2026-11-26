"""
tools/coven/blackboard.py
=========================
GrimoireBlackboard — thread-safe shared attack surface map, task queue,
findings store, auth token store, and endpoint baseline registry.

Extracted from sabrina_coven_orchestrator_LAB.py (class GrimoireBlackboard).
"""

import os
import json
import time
import threading
from typing import List, Dict, Any, Optional

from .shared import C


class GrimoireBlackboard:
    """
    Thread-safe blackboard shared across all SalemFamiliar workers and Alakazam.
    Holds:
      - Task queue (enqueue / claim_task)
      - Discovered findings
      - Endpoint attack surface catalog
      - Auth token store (per-host JWT/Bearer)
      - Baseline response measurements
      - Infra map (per-host node descriptors)
      - Run log
    """

    def __init__(self):
        self._lock          = threading.Lock()
        self._queue         : List[Dict[str, Any]] = []
        self._claimed       : set = set()
        self._findings      : List[Dict[str, Any]] = []
        self._unvalidated   : List[Dict[str, Any]] = []
        self._log           : List[str] = []
        self._infra_map     : Dict[str, Any] = {}
        self._exploration   : set = set()
        self._gain_window   : List[int] = []
        self._new_infra_flag: bool = False
        self._attack_surface: Dict[str, Dict[str, Any]] = {}
        self._auth_tokens   : Dict[str, Dict[str, str]] = {}
        self._baselines     : Dict[str, Any] = {}
        self._traffic_log_path: str = "/tmp/sabrina_http_traffic.jsonl"
        self._scripts_dir   : str = "/tmp"
        self._tech_stack    : Dict[str, str] = {}
        self.finding_ledger : Optional[Any] = None

    # ── Auth token store ──────────────────────────────────────────────────────

    @staticmethod
    def _norm_origin(host: str) -> str:
        import urllib.parse as _up
        if host.startswith("http"):
            p = _up.urlparse(host)
            return f"{p.scheme}://{p.netloc}"
        return f"http://{host.split('/')[0]}"

    def set_auth_token(self, host: str, token: str, email: str = "") -> None:
        origin = self._norm_origin(host)
        with self._lock:
            self._auth_tokens[origin] = {"token": token, "email": email}
        print(f"  {C.G}[BLACKBOARD] Auth token stored for {origin}{C.X}")

    def get_auth_token(self, host: str) -> Optional[str]:
        origin = self._norm_origin(host)
        with self._lock:
            entry = self._auth_tokens.get(origin)
            return entry["token"] if entry else None

    def get_auth_headers(self, host: str) -> Dict[str, str]:
        token = self.get_auth_token(host)
        return {"Authorization": f"Bearer {token}"} if token else {}

    # ── Attack surface catalog ────────────────────────────────────────────────

    def register_endpoint(self, path: str, method: str = "GET",
                          params: list = None, param_type: str = "unknown",
                          source: str = "recon") -> bool:
        key = f"{method.upper()}:{path}"
        with self._lock:
            if key not in self._attack_surface:
                self._attack_surface[key] = {
                    "path": path, "method": method.upper(),
                    "params": params or [], "param_type": param_type,
                    "source": source, "tested": [],
                }
                return True
            return False

    def get_attack_surface(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._attack_surface.values())

    def mark_endpoint_tested(self, path: str, vuln_type: str) -> None:
        with self._lock:
            for ep in self._attack_surface.values():
                ep_path = ep.get("path", "")
                if ep_path and (ep_path == path or path.endswith(ep_path)):
                    tested = ep.setdefault("tested", [])
                    if vuln_type not in tested:
                        tested.append(vuln_type)

    # ── Task queue ────────────────────────────────────────────────────────────

    def enqueue(self, tasks: List[Dict[str, Any]]):
        with self._lock:
            for i, t in enumerate(tasks):
                t.setdefault("id", f"task-{len(self._queue)+i:04d}")
                exp_key = (t.get("target", ""), t.get("vuln_type", ""))
                if exp_key not in self._exploration:
                    self._exploration.add(exp_key)
                    self._queue.append(t)
        if tasks:
            print(f"  {C.B}[BLACKBOARD] Enqueued {len(tasks)} tasks.{C.X}")

    def claim_task(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            for task in self._queue:
                if task["id"] not in self._claimed:
                    self._claimed.add(task["id"])
                    return task
        return None

    def tasks_remaining(self) -> int:
        with self._lock:
            return sum(1 for t in self._queue if t["id"] not in self._claimed)

    # ── Findings ──────────────────────────────────────────────────────────────

    def post_finding(self, finding: Dict[str, Any]):
        with self._lock:
            finding.setdefault("timestamp", time.strftime("%H:%M:%S"))
            self._findings.append(finding)
            self._unvalidated.append(finding)
            self._gain_window.append(1)
            if len(self._gain_window) > 20:
                self._gain_window.pop(0)
        sev   = finding.get("severity", "info").upper()
        color = {"CRITICAL": C.R, "HIGH": C.R, "MEDIUM": C.Y,
                 "LOW": C.G, "INFO": C.CN}.get(sev, C.CN)
        print(f"  {color}[FINDING] [{sev}] {finding.get('vuln_type','?')} "
              f"on {finding.get('url','?')}{C.X}")

    def get_findings(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._findings)

    def record_no_finding(self):
        with self._lock:
            self._gain_window.append(0)
            if len(self._gain_window) > 20:
                self._gain_window.pop(0)

    def pop_unvalidated(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            unval = [f for f in self._unvalidated if not f.get("validated")]
            if unval:
                f = unval[0]
                f["validated"] = "pending"
                return f
        return None

    def has_unvalidated(self) -> bool:
        with self._lock:
            return any(f.get("validated") not in (True, False)
                       for f in self._unvalidated)

    # ── Baselines ─────────────────────────────────────────────────────────────

    def set_baseline(self, path: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._baselines[path] = data

    def get_baseline(self, path: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._baselines.get(path)

    # ── Infra map ─────────────────────────────────────────────────────────────

    def update_infra_node(self, host: str, data: Dict[str, Any]):
        with self._lock:
            node = self._infra_map.setdefault(host, {"host": host})
            for k, v in data.items():
                if isinstance(v, list):
                    existing = node.get(k, [])
                    combined = existing + v
                    try:
                        node[k] = list(dict.fromkeys(combined))
                    except TypeError:
                        seen: set = set()
                        deduped = []
                        for item in combined:
                            key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
                            if key not in seen:
                                seen.add(key)
                                deduped.append(item)
                        node[k] = deduped
                elif isinstance(v, dict):
                    node.setdefault(k, {}).update(v)
                else:
                    node[k] = v

    def get_infra_map(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._infra_map)

    def mark_new_infra(self):
        with self._lock:
            self._new_infra_flag = True

    def consume_new_infra_flag(self) -> bool:
        with self._lock:
            flag = self._new_infra_flag
            self._new_infra_flag = False
            return flag

    # ── Traffic log path ──────────────────────────────────────────────────────

    def get_traffic_log_path(self) -> str:
        with self._lock:
            return self._traffic_log_path

    def set_traffic_log_path(self, path: str) -> None:
        with self._lock:
            self._traffic_log_path = path

    # ── Log ───────────────────────────────────────────────────────────────────

    def write_log(self, sender: str, message: str):
        with self._lock:
            entry = f"[{time.strftime('%H:%M:%S')}] {sender}: {message}"
            self._log.append(entry)

    def get_log(self) -> str:
        with self._lock:
            return "\n".join(self._log)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> str:
        with self._lock:
            sev_counts: Dict[str, int] = {}
            for f in self._findings:
                s = f.get("severity", "info")
                sev_counts[s] = sev_counts.get(s, 0) + 1
            sev_str = " | ".join(f"{k.upper()}:{v}" for k, v in sev_counts.items())
            return (f"Tasks: {len(self._claimed)}/{len(self._queue)} claimed | "
                    f"Findings: {len(self._findings)}"
                    + (f" [{sev_str}]" if sev_str else ""))
