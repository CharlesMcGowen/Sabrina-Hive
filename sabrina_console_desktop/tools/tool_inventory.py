"""Unified Tool Inventory for Sabrina.

Unifies tool availability across:
1. ToolRegistry (tools/tool_registry.py)
2. Declarative catalog (config/tools.yaml)
3. Dynamic plugins (tools/custom/*_plugin.py and tools/engines/*_engine.py)
4. Capability Registry (tools/capability_registry.py)
5. Built-in dispatcher handlers

Eliminates the disagreement between skill validation, prompt rendering,
and execution dispatch.
"""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import yaml

logger = logging.getLogger("sabrina.tool_inventory")
ROOT = Path(__file__).resolve().parents[1]
TOOLS_YAML = ROOT / "config" / "tools.yaml"
CUSTOM_PLUGINS_DIR = ROOT / "tools" / "custom"
ENGINES_DIR = ROOT / "tools" / "engines"

# Core built-in dispatcher tool names in sabrina_gemini_loop.py
BUILTIN_DISPATCHER_TOOLS: Dict[str, Dict[str, Any]] = {
    "run_shell": {"modes": ["yolo"], "description": "Execute shell command on local orchestrator."},
    "execute_python": {"modes": ["yolo"], "description": "Execute Python code directly."},
    "api_request": {"modes": ["write", "yolo"], "description": "Send HTTP/REST API request."},
    "ask_human": {"modes": ["read", "plan", "write", "yolo", "secure_code"], "description": "Ask human operator for guidance."},
    "completed": {"modes": ["read", "plan", "write", "yolo", "secure_code"], "description": "Mark objective as complete."},
    "search_memory": {"modes": ["read", "plan", "write", "yolo", "secure_code"], "description": "Search conversation history and code index."},
    "index_codebase": {"modes": ["write", "yolo"], "description": "Index disk paths into searchable RAG store."},
    "record_endpoint": {"modes": ["write", "yolo"], "description": "Record endpoint in Blackboard."},
    "save_knowledge": {"modes": ["write", "yolo", "secure_code"], "description": "Save topic/content to knowledge base."},
    "delegate_to_engineer": {"modes": ["write", "yolo"], "description": "Delegate module build to CrewAI engineer."},
    "browser_connect_cdp": {"modes": ["read", "plan", "write", "yolo"], "description": "Connect to Chrome CDP session."},
    "browser_navigate": {"modes": ["read", "plan", "write", "yolo"], "description": "Navigate browser to URL."},
    "browser_inspect": {"modes": ["read", "plan", "write", "yolo"], "description": "Inspect browser DOM elements."},
    "browser_click": {"modes": ["write", "yolo"], "description": "Click element in browser."},
    "browser_type": {"modes": ["write", "yolo"], "description": "Type into input element in browser."},
    "browser_screenshot": {"modes": ["read", "plan", "write", "yolo"], "description": "Take screenshot of browser page."},
}


def discover_dynamic_plugins() -> Dict[str, Dict[str, Any]]:
    """Scan tools/custom and tools/engines for executable plugins."""
    plugins: Dict[str, Dict[str, Any]] = {}
    sources = [
        (CUSTOM_PLUGINS_DIR, "_plugin.py"),
        (ENGINES_DIR, "_engine.py"),
    ]

    for p_dir, suffix in sources:
        if not p_dir.exists():
            continue
        for fname in os.listdir(p_dir):
            if fname.endswith(suffix):
                fpath = p_dir / fname
                try:
                    spec = importlib.util.spec_from_file_location(fname[:-3], str(fpath))
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(mod)
                        t_name = getattr(mod, "TOOL_NAME", None)
                        if t_name and hasattr(mod, "execute"):
                            plugins[t_name] = {
                                "name": t_name,
                                "description": getattr(mod, "TOOL_PROMPT", f"Dynamic plugin: {t_name}"),
                                "modes": ["write", "yolo"],
                                "source": str(fpath),
                                "parallel_safe": getattr(mod, "PARALLEL_SAFE", True),
                            }
                except Exception as e:
                    logger.debug("Failed loading plugin %s: %s", fname, e)

    return plugins


def get_unified_tool_catalog(mode: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Aggregate all tools across ToolRegistry, tools.yaml, plugins, and built-ins.

    Returns mapping: tool_name -> tool metadata dictionary.
    """
    catalog: Dict[str, Dict[str, Any]] = {}

    # 1. Built-in dispatcher tools
    for name, meta in BUILTIN_DISPATCHER_TOOLS.items():
        catalog[name] = {
            "name": name,
            "description": meta.get("description", ""),
            "allowed_modes": meta.get("modes", ["read", "plan", "write", "yolo"]),
            "source": "builtin",
        }

    # 2. Declarative config/tools.yaml
    if TOOLS_YAML.exists():
        try:
            with open(TOOLS_YAML, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
                yaml_tools = raw.get("tools", {})
                for name, meta in yaml_tools.items():
                    catalog[name] = {
                        "name": name,
                        "description": meta.get("description", ""),
                        "allowed_modes": meta.get("modes", ["read", "plan", "write", "yolo"]),
                        "format": meta.get("format", ""),
                        "source": "tools_yaml",
                    }
        except Exception as e:
            logger.warning("Failed loading tools.yaml: %s", e)

    # 3. ToolRegistry (includes docker_manager, discover_lab, remote_read_file, etc.)
    try:
        from tools.tool_registry import TOOL_REGISTRY
        # Force import tool-registering modules so they populate TOOL_REGISTRY
        try:
            import tools.skill_catalog  # registers discover_skills, read_skill
            import tools.lab_index      # registers discover_lab
        except Exception:
            pass

        for t_dict in TOOL_REGISTRY.list_tools(mode=None):
            t_name = t_dict["name"]
            catalog[t_name] = {
                "name": t_name,
                "description": t_dict.get("description", ""),
                "parameters": t_dict.get("parameters", {}),
                "allowed_modes": t_dict.get("allowed_modes", ["read", "plan", "write", "yolo"]),
                "parallel_safe": t_dict.get("parallel_safe", True),
                "source": "tool_registry",
            }
    except Exception as e:
        logger.warning("Failed loading from TOOL_REGISTRY: %s", e)

    # 4. Dynamic plugins from tools/custom and tools/engines
    dynamic = discover_dynamic_plugins()
    for name, p_meta in dynamic.items():
        catalog[name] = {
            "name": name,
            "description": p_meta.get("description", ""),
            "allowed_modes": p_meta.get("modes", ["write", "yolo"]),
            "source": p_meta.get("source", "dynamic_plugin"),
        }

    # Mode filtering if requested
    if mode is not None:
        filtered = {}
        for name, info in catalog.items():
            modes = info.get("allowed_modes", [])
            if mode in modes or "yolo" in modes:
                filtered[name] = info
        return filtered

    return catalog


def is_tool_available(tool_name: str, mode: Optional[str] = None) -> bool:
    """Return True if tool_name is present and accessible under the given mode."""
    cat = get_unified_tool_catalog(mode=mode)
    if tool_name in cat:
        return True

    # Also check capability registry
    try:
        from tools.capability_registry import DEFAULT_CAPABILITIES
        for cap in DEFAULT_CAPABILITIES:
            if cap.name == tool_name and (mode is None or cap.availability.get(mode, True)):
                return True
    except Exception:
        pass

    return False


def get_available_tool_names(mode: Optional[str] = None) -> Set[str]:
    """Return set of all available tool and capability names."""
    cat = get_unified_tool_catalog(mode=mode)
    names = set(cat.keys())
    try:
        from tools.capability_registry import DEFAULT_CAPABILITIES
        for cap in DEFAULT_CAPABILITIES:
            if mode is None or cap.availability.get(mode, True):
                names.add(cap.name)
    except Exception:
        pass
    return names
