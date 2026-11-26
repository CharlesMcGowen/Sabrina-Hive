"""tools/coven/__init__.py"""
from .shared import C, ScopePolicy, CredentialStore, STORAGE_DIR, _ollama_chat, _http_get
from .blackboard import GrimoireBlackboard
from .akashic_memory import AkashicMemory, SwarmMetaLearner
from .waf_bypass import MrMimeWAFBypass, HypnoSessionManager, DeserializationHunter
from .alakazam import AlakazamMasterCoordinator
from .salem_familiar import SalemFamiliar

__all__ = [
    "C", "ScopePolicy", "CredentialStore", "STORAGE_DIR",
    "GrimoireBlackboard",
    "AkashicMemory", "SwarmMetaLearner",
    "MrMimeWAFBypass", "HypnoSessionManager", "DeserializationHunter",
    "AlakazamMasterCoordinator",
    "SalemFamiliar",
]
