"""Windows-side recovery watcher for the Sabrina browser bridge."""
from __future__ import annotations

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading
import time
import hmac

from tools.capability_registry import CapabilityRegistry
from .recovery import RecoveryJournal, recovery_lease


class BridgeController:
    def __init__(self, script: str, token: str, run=subprocess.run, *, journal=None,
                 registry=None, lease=recovery_lease):
        if not token:
            raise ValueError("A nonempty bridge token is required")
        self.script, self.token, self.run = script, token, run
        self.journal = journal or RecoveryJournal("data/control/bridge-recovery.sqlite3", infrastructure=True)
        self.registry = registry or CapabilityRegistry()
        self.lease = lease
        self.lock = threading.Lock()
        self.last = {"service": "sabrina-bridge", "status": "starting", "updated": time.time()}

    def invoke(self, action="inspect", timeout=60):
        # Only fixed action names reach the fixed, operator-installed script.
        if action not in {"inspect", "reconnect", "recreate_forwards"}:
            raise ValueError("Unregistered bridge action")
        result = self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", self.script, "-Action", action],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            return {"status": "bridge_failed", "exit_code": result.returncode}
        try:
            value = json.loads(result.stdout.strip())
            if not isinstance(value, dict):
                raise ValueError("Invalid bridge result")
            return value
        except (ValueError, TypeError):
            return {"status": "invalid_bridge_response"}

    def tick(self):
        # HTTP requests and the autonomous watchdog share the same serial path.
        if not self.lock.acquire(blocking=False):
            return {"status": "recovery_in_progress"}
        try:
            try:
                observed = self.invoke()
            except Exception as exc:
                observed = {"status": "observation_failed", "error_type": type(exc).__name__}
            if observed.get("status") == "bridge_ready":
                self.journal.resolve("windows-bridge")
                self.last = {"service": "sabrina-bridge", **observed,
                             "capabilities": self.registry.discover(), "updated": time.time()}
                return self.last
            action = {"browser_down": "reconnect", "forwards_missing": "recreate_forwards"}.get(observed.get("status"))
            if action is None:
                if observed.get('status') == 'ssh_unreachable':
                    self.last = {'service': 'sabrina-bridge', **observed, 'updated': time.time(),
                                 'next_try': time.time() + 30}
                    return self.last
                if observed.get('status') in {'bridge_failed', 'invalid_bridge_response'}:
                    self.journal.require_operator('windows-bridge', observed['status'])
                    self.last = {'service': 'sabrina-bridge', 'status': 'operator_required',
                                 'reason': observed['status'], 'updated': time.time()}
                    return self.last
                # Unknown evidence is not permission to restart infrastructure.
                receipt = self.journal.begin("windows-bridge", "observe", observed.get("status", "unknown"))
                if receipt["status"] == "approved":
                    self.journal.finish("windows-bridge", receipt, observed)
                self.last = {"service": "sabrina-bridge", **observed,
                             **({} if receipt["status"] == "approved" else receipt), "updated": time.time()}
                return self.last
            capabilities = self.registry.discover(capability="browser.recovery", action=action)
            if not any(c["availability"] == "ready" and c["owner"] == "desktop-bridge" for c in capabilities):
                self.last = {"service": "sabrina-bridge", "status": "capability_unavailable"}
                return self.last
            receipt = self.journal.begin("windows-bridge", action, observed.get("status", "unknown"))
            if receipt["status"] != "approved":
                self.last = {"service": "sabrina-bridge", **receipt, "updated": time.time()}
                return self.last
            verified = False
            try:
                # Includes waiting for a lease in the persisted time budget.
                with self.lease():
                    remaining = receipt["deadline"] - self.journal.clock()
                    if remaining < 25:
                        outcome = {"status": "recovery_deadline"}
                    else:
                        # Reobserve after acquiring the browser lease.
                        current = self.invoke(timeout=min(25, remaining))
                        if current.get("status") == "bridge_ready":
                            outcome = current
                        else:
                            remaining = receipt["deadline"] - self.journal.clock()
                            if remaining <= 25:
                                outcome = {"status": "recovery_deadline"}
                            else:
                                self.invoke(action, timeout=min(125, remaining - 25))
                                remaining = receipt["deadline"] - self.journal.clock()
                                outcome = self.invoke(timeout=min(25, max(0.1, remaining)))
                        verified = outcome.get("status") == "bridge_ready"
            except Exception as exc:
                outcome = {"status": "repair_failed", "error_type": type(exc).__name__}
            self.journal.finish("windows-bridge", receipt, outcome, verified)
            self.last = {"service": "sabrina-bridge", **outcome, "attempt": receipt["attempt"], "updated": time.time()}
            return self.last
        except Exception as exc:
            self.last = {"service": "sabrina-bridge", "status": "observation_failed", "error_type": type(exc).__name__, "updated": time.time()}
            return self.last
        finally:
            try:
                self.last.setdefault('updated', time.time())
                from .availability_support import atomic_json
                from tools.worker_identity import executor_identity
                self.last['executor'] = executor_identity()
                atomic_json(self.journal.path.with_name('bridge-watcher-status.json'),
                            self.last)
            except Exception:
                pass
            finally:
                self.lock.release()

    def recover(self, supplied: str) -> dict:
        if not hmac.compare_digest(supplied, self.token):
            return {"status": "unauthorized"}
        return self.tick()

    def watch(self, stop, interval=30):
        while not stop.is_set():
            self.tick()
            stop.wait(interval)


def make_handler(controller: BridgeController):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def send_json(self, value, code=200):
            body = json.dumps(value).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health": self.send_json(controller.last); return
            if self.path == "/capabilities":
                self.send_json(controller.registry.discover()); return
            self.send_json({"status": "not_found"}, 404)

        def do_POST(self):
            if self.path != "/recover": self.send_json({"status": "not_found"}, 404); return
            supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
            result = controller.recover(supplied)
            self.send_json(result, 200 if result.get("status") != "unauthorized" else 401)
    return Handler


def main():
    import sys
    if sys.stdout is None or sys.stderr is None:
        _bw_log_dir = Path(__file__).resolve().parents[1] / "logs" / "availability"
        _bw_log_dir.mkdir(parents=True, exist_ok=True)
        _bw_log = (_bw_log_dir / "bridge-watcher.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _bw_log
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1"); parser.add_argument("--port", type=int, default=19225)
    parser.add_argument("--script", default=str(Path(__file__).parents[1] / "launch_social_bridge.ps1"))
    parser.add_argument("--token-file", default=os.environ.get("SABRINA_BRIDGE_TOKEN_FILE", "data/control/bridge.token"))
    parser.add_argument("--journal", default="data/control/bridge-recovery.sqlite3")
    args = parser.parse_args()
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    controller = BridgeController(args.script, token, journal=RecoveryJournal(args.journal, infrastructure=True))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(controller))
    stop = threading.Event()
    threading.Thread(target=controller.watch, args=(stop,), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__": main()
