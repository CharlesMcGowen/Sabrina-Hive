"""
tools/model_assignment_api.py
─────────────────────────────────────────────────────────────────────────────
Centralized Model Assignment & Swarm Topology API for Sabrina.

Provides a unified control plane where Sabrina dynamically manages, assigns,
and resolves which AI models and compute nodes (workstation GPU 127.0.0.1,
lab server GPU 192.168.2.69, or cloud oracles) are utilized across every
role in the entire application.
"""

import json
import logging
import os
import threading
import urllib.request
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("sabrina.model_assignment_api")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
ASSIGNMENTS_FILE = DATA_DIR / "model_assignments.json"

_LOCK = threading.RLock()


def _pick_available_gemini_model() -> Optional[str]:
    """
    Consult the centralized, cross-process oracle-usage/cooldown ledger (tools/oracle_
    providers.py's _is_on_cooldown, backed by the same Redis state and the /api/models/usage
    dashboard endpoint) before picking a Gemini model here, instead of hardcoding one.
    Live-confirmed 2026-09-15: this exact bug -- select a specific model name without checking
    whether it's currently known-exhausted -- existed independently in TWO places: here, and in
    ai_systems/agents/tool_developer.py's engineering LLM picker (fixed the same day). Returns
    None if every model in the ladder is currently cooling down, so the caller can fall through
    to a different provider instead of handing out a model guaranteed to fail immediately.
    """
    try:
        from tools.oracle_providers import GoogleGeminiOracle, _is_on_cooldown
        for m in GoogleGeminiOracle.DEFAULT_MODELS:
            if not _is_on_cooldown("gemini", m):
                return m
        return None
    except Exception:
        return "gemini-3.6-flash"  # fail open to the old hardcoded default if this check itself breaks

# Standard functional roles across Sabrina
STANDARD_ROLES = [
    "lead_architect",    # Autonomous loop planning and strategic guidance
    "worker",            # Main per-round ReAct loop worker
    "tool_engineer",     # CrewAI tool synthesis and refinement
    "web_navigator",     # Browser DOM navigation and workflow interaction
    "security_validator",# Hypothesis-testing and security probe engineer
    "researcher"         # Bounty dataset distillation and AST extraction
]

# Default assignments: balanced for performance and availability
DEFAULT_ASSIGNMENTS: Dict[str, Dict[str, Any]] = {
    "lead_architect": {
        "provider": "auto",
        "model": "auto",  # Cascades: local smart ladder -> cloud
        "host": "auto",
        "temperature": 0.2,
        "description": "High-level goal planning, meta-reviews, and strategy reformulation"
    },
    "worker": {
        "provider": "local",
        "model": "qwen2.5:7b",
        "host": "auto",  # Resolves: 127.0.0.1 workstation GPU -> 192.168.2.69
        "temperature": 0.2,
        "description": "Per-round ReAct tool decision and execution loop"
    },
    "tool_engineer": {
        "provider": "auto",
        "model": "auto",  # Resolves best available coding model on mesh or cloud
        "host": "auto",
        "temperature": 0.2,
        "description": "CrewAI agent building and refining modular engines in tools/engines/"
    },
    "web_navigator": {
        "provider": "local",
        "model": "qwen2.5:7b",
        "host": "auto",
        "temperature": 0.2,
        "description": "Interactive browser DOM exploration and CDP automation"
    },
    "security_validator": {
        "provider": "auto",
        "model": "auto",
        "host": "auto",
        "temperature": 0.2,
        "description": "Hypothesis verification probe builder and finding reporter"
    },
    "researcher": {
        "provider": "auto",
        "model": "auto",
        "host": "auto",
        "temperature": 0.2,
        "description": "Dataset distillation, report analysis, and AST logic extraction"
    }
}


def _load_persisted_assignments() -> Dict[str, Dict[str, Any]]:
    """Load assignments from disk, initializing with defaults if missing."""
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not ASSIGNMENTS_FILE.exists():
            _save_assignments(DEFAULT_ASSIGNMENTS)
            return dict(DEFAULT_ASSIGNMENTS)
        try:
            with open(ASSIGNMENTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Ensure all standard roles exist
            merged = dict(DEFAULT_ASSIGNMENTS)
            merged.update(data)
            return merged
        except Exception as e:
            logger.warning(f"Failed to read {ASSIGNMENTS_FILE}, using defaults: {e}")
            return dict(DEFAULT_ASSIGNMENTS)


def _save_assignments(data: Dict[str, Dict[str, Any]]) -> None:
    """Save current assignments to disk."""
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(ASSIGNMENTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save {ASSIGNMENTS_FILE}: {e}")


def discover_mesh_models() -> Dict[str, Any]:
    """
    Live query across all compute nodes in Sabrina's mesh (Workstation GPU 127.0.0.1,
    Lab GPU/CPU 192.168.1.133, and cloud APIs) to list all available models.
    """
    from tools.secret_store import get_secret
    try:
        from ai_systems.framework.lab_config import is_api_tiers_enabled, ollama_base_url, LAB_HOST
        api_tiers_on = is_api_tiers_enabled()
        lab_url = ollama_base_url(os.environ.get("SABRINA_LAB_HOST", "192.168.1.133"))
    except Exception:
        api_tiers_on = os.environ.get("SABRINA_API_TIERS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
        lab_url = "http://192.168.1.133:11434"
    
    nodes = {
        "workstation_local": {"url": "http://127.0.0.1:11434", "models": [], "reachable": False},
        "lab_server": {"url": lab_url, "models": [], "reachable": False},
        "cloud_gemini": {
            "available": api_tiers_on and bool(get_secret("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")),
            "models": ["gemini-3.6-flash", "gemini-3.1-pro-preview"],
        },
        "cloud_anthropic": {
            "available": api_tiers_on and bool(get_secret("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")),
            "models": ["claude-3-5-sonnet", "claude-haiku"],
        },
    }

    for key, info in [("workstation_local", nodes["workstation_local"]), ("lab_server", nodes["lab_server"])]:
        url = info["url"]
        try:
            req = urllib.request.Request(f"{url}/api/tags", headers={"User-Agent": "SabrinaModelAPI"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                info["models"] = [m["name"] for m in data.get("models", [])]
                info["reachable"] = True
        except Exception:
            info["reachable"] = False

    return nodes


def get_all_assignments() -> Dict[str, Any]:
    """Retrieve all current role assignments along with live mesh node discovery."""
    assignments = _load_persisted_assignments()
    mesh = discover_mesh_models()
    return {
        "assignments": assignments,
        "mesh_nodes": mesh
    }


def get_model_assignment(role: str) -> Dict[str, Any]:
    """
    Retrieve the configured model assignment for a specific role,
    resolving 'auto' values to the most capable live host and model.
    """
    assignments = _load_persisted_assignments()
    cfg = assignments.get(role, DEFAULT_ASSIGNMENTS.get(role, {
        "provider": "auto",
        "model": "auto",
        "host": "auto",
        "temperature": 0.2
    }))

    # Deep copy to resolve dynamic values without mutating persisted state
    resolved = dict(cfg)
    mesh = discover_mesh_models()

    ws_models = mesh["workstation_local"]["models"] if mesh["workstation_local"]["reachable"] else []
    lab_models = mesh["lab_server"]["models"] if mesh["lab_server"]["reachable"] else []
    lab_url = mesh["lab_server"]["url"]

    # Resolve provider and model if 'auto'
    if resolved.get("model") == "auto" or resolved.get("provider") == "auto":
        # Check role requirements
        if role in ("lead_architect", "researcher", "tool_engineer", "security_validator"):
            # Prefer 14B Pareto frontier models on ego (100% VRAM fit), or gemini if API tiers enabled
            smart_candidates = (
                ["qwen2.5-coder:14b", "qwen3:14b", "granite3.1-dense:8b", "llama3.1:8b"]
                if role == "tool_engineer"
                else ["qwen3:14b", "qwen2.5-coder:14b", "granite3.1-dense:8b", "llama3.1:8b"]
            )
            found_smart_lab = next((m for m in smart_candidates if m in lab_models), None)
            if found_smart_lab:
                resolved["model"] = found_smart_lab
                resolved["host"] = lab_url
                resolved["provider"] = "local"
            elif mesh["cloud_gemini"]["available"] and (gemini_model := _pick_available_gemini_model()):
                resolved["model"] = gemini_model
                resolved["provider"] = "gemini"
                resolved["host"] = "cloud"
            else:
                fast_candidates = ["granite3.1-dense:8b", "llama3.1:8b", "qwen2.5:7b", "granite-code:8b"]
                found_ws = next((m for m in fast_candidates if m in ws_models), None)
                found_lab = next((m for m in fast_candidates if m in lab_models), None)
                if found_ws:
                    resolved["model"] = found_ws
                    resolved["host"] = "http://127.0.0.1:11434"
                    resolved["provider"] = "local"
                elif found_lab:
                    resolved["model"] = found_lab
                    resolved["host"] = lab_url
                    resolved["provider"] = "local"
                elif ws_models:
                    resolved["model"] = ws_models[0]
                    resolved["host"] = "http://127.0.0.1:11434"
                    resolved["provider"] = "local"
                elif lab_models:
                    resolved["model"] = lab_models[0]
                    resolved["host"] = lab_url
                    resolved["provider"] = "local"
        else:
            # Worker / Web navigator: fast 7B/8B model, prioritize workstation GPU then ego lab GPU
            fast_candidates = ["granite3.1-dense:8b", "llama3.1:8b", "qwen2.5:7b", "granite-code:8b", "dolphin-mistral:7b"]
            found_ws = next((m for m in fast_candidates if m in ws_models), None)
            found_lab = next((m for m in fast_candidates if m in lab_models), None)
            if found_ws:
                resolved["model"] = found_ws
                resolved["host"] = "http://127.0.0.1:11434"
                resolved["provider"] = "local"
            elif found_lab:
                resolved["model"] = found_lab
                resolved["host"] = lab_url
                resolved["provider"] = "local"
            elif ws_models:
                resolved["model"] = ws_models[0]
                resolved["host"] = "http://127.0.0.1:11434"
                resolved["provider"] = "local"
            elif lab_models:
                resolved["model"] = lab_models[0]
                resolved["host"] = lab_url
                resolved["provider"] = "local"
            elif mesh["cloud_gemini"]["available"] and (gemini_model := _pick_available_gemini_model()):
                resolved["model"] = gemini_model
                resolved["provider"] = "gemini"
                resolved["host"] = "cloud"

    # If host is auto, find where the resolved model lives
    if resolved.get("host") == "auto" and resolved.get("provider") == "local":
        model_name = resolved.get("model", "")
        if model_name in ws_models:
            resolved["host"] = "http://127.0.0.1:11434"
        elif model_name in lab_models:
            resolved["host"] = lab_url
        else:
            resolved["host"] = lab_url if mesh["lab_server"]["reachable"] else "http://127.0.0.1:11434"

    return resolved


def set_model_assignment(
    role: str,
    model: Optional[str] = None,
    host: str = "auto",
    provider: str = "auto",
    temperature: Optional[float] = None,
    model_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Dynamically reassign a functional role to a specified model, host, or provider.
    Changes are persisted to data/model_assignments.json immediately.
    Accepts model or model_name.
    """
    target_model = model or model_name
    if not target_model:
        raise ValueError("Model must be specified via 'model' or 'model_name'.")

    with _LOCK:
        assignments = _load_persisted_assignments()
        current = assignments.get(role, {})
        
        updated = {
            "provider": provider if provider != "auto" else current.get("provider", "auto"),
            "model": target_model,
            "host": host,
            "temperature": temperature if temperature is not None else current.get("temperature", 0.2),
            "description": current.get("description", f"Assigned role: {role}")
        }

        # Infer provider if not explicitly given
        if provider == "auto":
            if target_model.startswith("gemini"):
                updated["provider"] = "gemini"
                updated["host"] = "cloud"
            elif "claude" in target_model:
                updated["provider"] = "claude"
                updated["host"] = "cloud"
            else:
                updated["provider"] = "local"

        assignments[role] = updated
        _save_assignments(assignments)
        logger.info(f"Model assignment updated: role='{role}' -> {updated}")
        return get_model_assignment(role)


def reset_to_defaults() -> Dict[str, Any]:
    """Reset all model assignments to standard defaults."""
    with _LOCK:
        _save_assignments(DEFAULT_ASSIGNMENTS)
        return get_all_assignments()


def get_crewai_llm(role: str, temperature: Optional[float] = None) -> Any:
    """
    Factory function returning a ready-to-use CrewAI LLM instance configured
    for the specified functional role based on current model assignments.
    """
    from crewai import LLM
    from tools.secret_store import get_secret

    assignment = get_model_assignment(role)
    provider = assignment.get("provider", "local")
    model = assignment.get("model", "qwen2.5:7b")
    host = assignment.get("host", "http://127.0.0.1:11434")
    temp = temperature if temperature is not None else assignment.get("temperature", 0.2)

    if provider == "gemini":
        gemini_key = get_secret("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY")
        target_model = model if model.startswith("gemini/") else f"gemini/{model}"
        return LLM(model=target_model, api_key=gemini_key, temperature=temp)
    elif provider == "claude":
        claude_key = get_secret("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        target_model = model if model.startswith("anthropic/") else f"anthropic/{model}"
        return LLM(model=target_model, api_key=claude_key, temperature=temp)
    else:
        # Local Ollama on resolved host
        base_url = host if host and host != "auto" else "http://127.0.0.1:11434"
        ollama_model = model if model.startswith("ollama/") else f"ollama/{model}"
        return LLM(model=ollama_model, base_url=base_url, temperature=temp)
