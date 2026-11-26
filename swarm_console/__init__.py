"""Zero-dependency Swarm Console for visualizing and managing agent swarms."""

from .client import SwarmClient, seed_demo_swarm
from .gemini_copilot import (
    DEFAULT_GEMINI_MODEL,
    SUPPORTED_GEMINI_MODELS,
    GeminiCopilotError,
    analyze_swarm_state,
    get_copilot_status,
)
from .protocol import (
    LockConflictError,
    PathIsolationError,
    SwarmProtocolError,
    SwarmStore,
    guard_safe_path,
)
from .registry import ModelSkillRegistry
from .server import create_server
from .vault import SUPPORTED_PROVIDERS, ProviderKeyVault

__all__ = [
    "DEFAULT_GEMINI_MODEL",
    "GeminiCopilotError",
    "LockConflictError",
    "ModelSkillRegistry",
    "PathIsolationError",
    "ProviderKeyVault",
    "SUPPORTED_GEMINI_MODELS",
    "SUPPORTED_PROVIDERS",
    "SwarmClient",
    "SwarmProtocolError",
    "SwarmStore",
    "analyze_swarm_state",
    "create_server",
    "get_copilot_status",
    "guard_safe_path",
    "seed_demo_swarm",
]
