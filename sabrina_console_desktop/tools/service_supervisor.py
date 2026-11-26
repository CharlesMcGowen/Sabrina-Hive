"""Lightweight local microservice supervisor with targeted mtime-based hot restart."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Sequence

WATCHED_EXTENSIONS = {".py", ".html"}


@dataclass
class ServiceSpec:
    """Configuration for a supervised local microservice."""

    name: str
    command: Sequence[str]
    watch_paths: Sequence[Path | str] = ()
    cwd: Path | str | None = None
    env: dict[str, str] | None = None
    graceful_timeout: float = 5.0
    auto_restart: bool = True


@dataclass
class _ServiceRuntime:
    spec: ServiceSpec
    process: Any = None
    pid: int | None = None
    status: str = "stopped"
    restarts: int = 0
    last_reload_ts: float | None = None
    watched_mtimes: dict[str, float] = field(default_factory=dict)


def _snapshot_mtimes(watch_paths: Sequence[Path | str], cwd: Path | str | None = None) -> dict[str, float]:
    """Collect mtimes for watched .py/.html files (or directories containing them)."""
    base = Path(cwd) if cwd else None
    mtimes: dict[str, float] = {}
    for raw_path in watch_paths:
        p = Path(raw_path)
        if not p.is_absolute() and base is not None:
            p = base / p
        if not p.exists():
            continue
        if p.is_dir():
            for child in p.rglob("*"):
                if child.is_file() and child.suffix.lower() in WATCHED_EXTENSIONS:
                    try:
                        mtimes[str(child.resolve())] = child.stat().st_mtime
                    except OSError:
                        pass
        elif p.is_file():
            try:
                mtimes[str(p.resolve())] = p.stat().st_mtime
            except OSError:
                pass
    return mtimes


class MicroserviceSupervisor:
    """Manages independent child processes and restarts only services whose watched files change."""

    def __init__(
        self,
        specs: Sequence[ServiceSpec] = (),
        *,
        poll_interval: float = 1.0,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.poll_interval = max(0.05, float(poll_interval))
        self._popen_factory = popen_factory
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._services: dict[str, _ServiceRuntime] = {}
        for spec in specs:
            self.add_service(spec)

    def add_service(self, spec: ServiceSpec) -> None:
        with self._lock:
            if spec.name in self._services:
                raise ValueError(f"Duplicate service spec: {spec.name}")
            self._services[spec.name] = _ServiceRuntime(spec=spec)

    def _spawn(self, rt: _ServiceRuntime) -> None:
        spec = rt.spec
        env = os.environ.copy()
        if spec.env:
            env.update(spec.env)
        cwd = str(spec.cwd) if spec.cwd is not None else None
        proc = self._popen_factory(list(spec.command), cwd=cwd, env=env)
        rt.process = proc
        rt.pid = getattr(proc, "pid", None)
        rt.status = "running"
        rt.last_reload_ts = time.time()
        rt.watched_mtimes = _snapshot_mtimes(spec.watch_paths, cwd=spec.cwd)

    def _terminate(self, rt: _ServiceRuntime) -> None:
        proc = rt.process
        if proc is None:
            rt.pid = None
            rt.status = "stopped"
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=rt.spec.graceful_timeout)
                except Exception:
                    proc.kill()
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
        finally:
            rt.process = None
            rt.pid = None
            rt.status = "stopped"

    def start_service(self, name: str) -> dict[str, Any]:
        with self._lock:
            rt = self._services[name]
            if rt.process is not None and rt.process.poll() is None:
                return self.service_status(name)
            self._spawn(rt)
            return self.service_status(name)

    def start_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            for name in self._services:
                self.start_service(name)
            return self.status()

    def stop_service(self, name: str) -> dict[str, Any]:
        with self._lock:
            rt = self._services[name]
            self._terminate(rt)
            return self.service_status(name)

    def stop_all(self) -> None:
        with self._lock:
            self._stop_event.set()
            for name in reversed(list(self._services.keys())):
                self._terminate(self._services[name])

    def restart_service(self, name: str, *, reason: str = "manual") -> dict[str, Any]:
        with self._lock:
            rt = self._services[name]
            rt.status = "restarting"
            self._terminate(rt)
            rt.restarts += 1
            self._spawn(rt)
            info = self.service_status(name)
            info["reason"] = reason
            return info

    def poll_once(self) -> list[dict[str, Any]]:
        """Check watched file mtimes and child liveness; restart only affected services."""
        events: list[dict[str, Any]] = []
        with self._lock:
            for name, rt in self._services.items():
                if rt.status == "stopped" and rt.process is None:
                    continue
                current_mtimes = _snapshot_mtimes(rt.spec.watch_paths, cwd=rt.spec.cwd)
                changed = [
                    path
                    for path, mtime in current_mtimes.items()
                    if rt.watched_mtimes.get(path) != mtime
                ]
                removed = [
                    path
                    for path in rt.watched_mtimes
                    if path not in current_mtimes
                ]
                if changed or removed:
                    info = self.restart_service(name, reason="file_changed")
                    events.append(
                        {
                            "service": name,
                            "event": "reloaded",
                            "changed_files": changed + removed,
                            "pid": info["pid"],
                            "restarts": info["restarts"],
                            "last_reload_ts": info["last_reload_ts"],
                        }
                    )
                    continue

                if rt.process is not None and rt.process.poll() is not None:
                    if rt.spec.auto_restart:
                        info = self.restart_service(name, reason="process_exited")
                        events.append(
                            {
                                "service": name,
                                "event": "restarted_after_exit",
                                "pid": info["pid"],
                                "restarts": info["restarts"],
                                "last_reload_ts": info["last_reload_ts"],
                            }
                        )
                    else:
                        rt.status = "crashed"
                        rt.pid = None
                        rt.process = None
                        events.append({"service": name, "event": "crashed", "pid": None})
        return events

    def service_status(self, name: str) -> dict[str, Any]:
        with self._lock:
            rt = self._services[name]
            return {
                "name": name,
                "pid": rt.pid,
                "status": rt.status,
                "restarts": rt.restarts,
                "last_reload_ts": rt.last_reload_ts,
                "watch_count": len(rt.watched_mtimes),
            }

    def status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: self.service_status(name) for name in self._services}

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        stop_ev = stop_event or self._stop_event
        self.start_all()
        try:
            while not stop_ev.is_set():
                self.poll_once()
                stop_ev.wait(self.poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop_all()


def default_local_service_specs(root: Path, python_exe: str | None = None) -> list[ServiceSpec]:
    """Build default local microservice specs for API, scheduler, and desktop UI."""
    py = python_exe or sys.executable
    return [
        ServiceSpec(
            name="api_8087",
            command=[py, "-m", "sabrina_social.api"],
            watch_paths=[
                root / "sabrina_social" / "api.py",
                root / "tools" / "capability_registry.py",
            ],
            cwd=root,
        ),
        ServiceSpec(
            name="scheduler",
            command=[py, "-m", "sabrina_social.scheduler"],
            watch_paths=[
                root / "sabrina_social" / "scheduler.py",
            ],
            cwd=root,
        ),
        ServiceSpec(
            name="desktop_ui",
            command=[py, str(root / "sabrina_desktop_ui.py")],
            watch_paths=[
                root / "sabrina_desktop_ui.py",
                root / "sabrina_desktop_ui.html",
            ],
            cwd=root,
        ),
    ]
