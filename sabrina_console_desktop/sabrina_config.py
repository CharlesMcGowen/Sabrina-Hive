"""Centralized global configuration loaded from sabrina_config.json and environment variables.

Copy `sabrina_config.example.json` to `sabrina_config.json` (git-ignored) and customize
local endpoints, hostnames, model names, and paths without hardcoding them in source files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[1]
DESKTOP_ROOT: Path = Path(__file__).resolve().parent


def _load_config_dict() -> dict[str, Any]:
    candidates: list[Path] = []
    env_path = os.environ.get("SABRINA_CONFIG_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([
        WORKSPACE_ROOT / "sabrina_config.json",
        DESKTOP_ROOT / "sabrina_config.json",
    ])
    for path in candidates:
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}


_CFG: dict[str, Any] = _load_config_dict()


def _get_str(key: str, env_key: str, default: str) -> str:
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        return env_val
    cfg_val = _CFG.get(key)
    if isinstance(cfg_val, str) and cfg_val.strip():
        return cfg_val.strip()
    return default


def _get_list(key: str, env_key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        return tuple(item.strip() for item in env_val.split(",") if item.strip())
    cfg_val = _CFG.get(key)
    if isinstance(cfg_val, list):
        return tuple(str(item).strip() for item in cfg_val if str(item).strip())
    return default


OLLAMA_BASE_URL: str = _get_str("ollama_base_url", "SABRINA_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
SOCIAL_RESEARCH_OLLAMA_URL: str = _get_str("social_research_ollama_url", "SABRINA_SOCIAL_RESEARCH_OLLAMA", OLLAMA_BASE_URL).rstrip("/")
LAB_HOST: str = _get_str("lab_host", "SABRINA_LAB_HOST", "localhost")
LAB_SSH_HOST: str = _get_str("lab_ssh_host", "SABRINA_DOCKER_HOST", LAB_HOST)
LAB_IP: str = _get_str("lab_ip", "SABRINA_LAB_IP", "127.0.0.1")
FORGEJO_URL: str = _get_str("forgejo_url", "SABRINA_FORGEJO_URL", "http://127.0.0.1:3003").rstrip("/")
PHOENIX_URL: str = _get_str("phoenix_url", "PHOENIX_COLLECTOR_ENDPOINT", "http://127.0.0.1:6006").rstrip("/")
PHOENIX_TRACES_URL: str = _get_str("phoenix_traces_url", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", f"{PHOENIX_URL}/v1/traces").rstrip("/")
HITL_GATE_URL: str = _get_str("hitl_gate_url", "HITL_GATE_URL", "http://127.0.0.1:8890").rstrip("/")
REDIS_URL: str = _get_str("redis_url", "SABRINA_REDIS_URL", "redis://127.0.0.1:6379/0")
DEFAULT_LOCAL_MODEL: str = _get_str("default_local_model", "SABRINA_DEFAULT_LOCAL_MODEL", "ollama/local-model")
DEFAULT_CODER_MODEL: str = _get_str("default_coder_model", "SABRINA_DEFAULT_CODER_MODEL", "local-coder")
DEFAULT_GENERAL_MODEL: str = _get_str("default_general_model", "SABRINA_DEFAULT_GENERAL_MODEL", "local-general")
EXTERNAL_PORTAL_IDS: tuple[str, ...] = _get_list("external_portal_ids", "SABRINA_EXTERNAL_PORTAL_IDS", ())
RESEARCH_PROJECT_ROOT: str = _get_str("research_project_root", "SABRINA_RESEARCH_PROJECT_ROOT", "")
WHEELHOUSE_DIR: str = _get_str("wheelhouse_dir", "SABRINA_WHEELHOUSE_DIR", str(WORKSPACE_ROOT / "artifacts" / "wheels"))
