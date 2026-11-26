"""Run a bounded set of Docker management operations on Sabrina's lab host."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import subprocess
from typing import Any


DEFAULT_HOST = "ego.local"
DEFAULT_USER = "ego"
DEFAULT_COMPOSE_ROOTS = (
    "/home/ego/bot",
    "/mnt/webapps-nvme",
    "/home/ego/kraken-explorer",
    "/mnt/14tb/kraken-explorer",
)
MAX_OUTPUT_CHARS = 16_000


class DockerRemoteError(RuntimeError):
    pass


def _target() -> tuple[str, str]:
    host = os.environ.get("SABRINA_DOCKER_HOST", DEFAULT_HOST).strip()
    user = os.environ.get("SABRINA_DOCKER_USER", DEFAULT_USER).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", host) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", user):
        raise DockerRemoteError("Invalid SABRINA_DOCKER_HOST or SABRINA_DOCKER_USER configuration.")
    return host, user


def _identifier(value: Any, label: str) -> str:
    value = str(value or "").strip()
    if not value or len(value) > 200 or any(c.isspace() for c in value):
        raise DockerRemoteError(f"A valid {label} is required.")
    if value.startswith("-"):
        raise DockerRemoteError(f"{label.capitalize()} cannot begin with '-'.")
    return value


def _compose_dir(value: Any) -> str:
    path = posixpath.normpath(str(value or "").strip())
    if not path.startswith("/"):
        raise DockerRemoteError("compose_dir must be an absolute Linux path on the lab host.")
    configured_env = [
        posixpath.normpath(p.strip())
        for p in os.environ.get("SABRINA_DOCKER_COMPOSE_ROOTS", "").split(",")
        if p.strip()
    ]
    roots = tuple(set(configured_env or list(DEFAULT_COMPOSE_ROOTS)))
    if not any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots):
        raise DockerRemoteError(f"compose_dir must be under one of the configured roots: {', '.join(sorted(roots))}")
    return path


def _run(args: list[str], *, timeout: int = 90, cwd: str | None = None) -> str:
    host, user = _target()
    remote = " ".join(shlex.quote(part) for part in args)
    if cwd:
        remote = f"cd -- {shlex.quote(cwd)} && {remote}"
    cmd = ["ssh", "-4", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", f"{user}@{host}", remote]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as exc:
        raise DockerRemoteError("OpenSSH client ('ssh') is unavailable on Sabrina's orchestrator.") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerRemoteError(f"Remote Docker operation timed out after {timeout}s.") from exc
    output = (proc.stdout or "") + (("\nStderr:\n" + proc.stderr) if proc.stderr else "")
    output = output.strip() or "(no output)"
    if proc.returncode:
        raise DockerRemoteError(f"Remote command exited {proc.returncode}:\n{output[:MAX_OUTPUT_CHARS]}")
    return output[:MAX_OUTPUT_CHARS]


def execute(action: dict[str, Any]) -> str:
    """Execute a documented read or lifecycle action; never accepts a shell command."""
    op = str(action.get("operation", "")).strip().lower()
    target = action.get("target")
    if op == "host_status":
        return _run(["bash", "-lc", "printf 'Host: '; hostname; printf '\\nDocker: '; docker version --format '{{.Server.Version}}'; printf '\\nDocker disk:\\n'; df -h /mnt/nvme/docker; printf '\\nDocker usage:\\n'; docker system df"])
    if op == "containers":
        return _run(["docker", "ps", "--all", "--size"])
    if op == "images":
        return _run(["docker", "image", "ls", "--digests"])
    if op == "networks":
        return _run(["docker", "network", "ls"])
    if op == "volumes":
        return _run(["docker", "volume", "ls"])
    if op == "inspect":
        return _run(["docker", "inspect", _identifier(target, "container or image")])
    if op == "logs":
        container = _identifier(target, "container")
        tail = int(action.get("tail", 100))
        if not 1 <= tail <= 1000:
            raise DockerRemoteError("tail must be between 1 and 1000.")
        return _run(["docker", "logs", "--tail", str(tail), container])
    if op == "stats":
        return _run(["docker", "stats", "--no-stream", _identifier(target, "container")])
    if op in {"start", "stop", "restart"}:
        timeout = 30 if op in {"stop", "restart"} else 90
        return _run(["docker", op, _identifier(target, "container")], timeout=timeout)
    if op == "pull":
        image = _identifier(target, "image")
        free = _run(["bash", "-lc", "df -Pk /mnt/nvme/docker | awk 'NR==2 {print $4}'"])
        try:
            free_gib = int(free.splitlines()[0]) / (1024 * 1024)
        except (ValueError, IndexError) as exc:
            raise DockerRemoteError(f"Could not determine free Docker storage: {free}") from exc
        min_free = float(os.environ.get("SABRINA_DOCKER_MIN_FREE_GB", "10"))
        if free_gib < min_free:
            raise DockerRemoteError(
                f"Image pull stopped: Docker storage has {free_gib:.1f} GiB free; configured minimum is {min_free:.1f} GiB."
            )
        return _run(["docker", "pull", image], timeout=1800)
    if op in {"compose_ps", "compose_logs", "compose_up", "compose_restart"}:
        project = _compose_dir(action.get("compose_dir"))
        compose = ["docker", "compose"]
        if op == "compose_ps":
            args = compose + ["ps", "--all"]
            timeout = 90
        elif op == "compose_logs":
            args = compose + ["logs", "--tail", "100"]
            service = action.get("service")
            if service:
                args.append(_identifier(service, "service"))
            timeout = 90
        elif op == "compose_up":
            args = compose + ["up", "--no-build", "--pull", "never", "--detach"]
            services = action.get("services") or []
            if not isinstance(services, list) or len(services) > 20:
                raise DockerRemoteError("services must be a list of at most 20 Compose service names.")
            args.extend(_identifier(s, "service") for s in services)
            timeout = 1800
        else:
            service = action.get("service")
            args = compose + ["restart"]
            if service:
                args.append(_identifier(service, "service"))
            timeout = 300
        return _run(args, timeout=timeout, cwd=project)
    raise DockerRemoteError(
        "Unsupported operation. Available: host_status, containers, images, networks, volumes, inspect, logs, stats, "
        "start, stop, restart, pull, compose_ps, compose_logs, compose_up, compose_restart."
    )
