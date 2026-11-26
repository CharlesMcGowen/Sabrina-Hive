"""Three-Layer Context-First Coordinator Architecture for Sabrina Swarm Missions.

Splits every mission launch into three clean layers so the Coordinator makes
informed setup decisions while Python enforces safety deterministically:

1. Deterministic Pre-Flight Manifest (Python runs first, 0 LLM calls):
   - Healthy models only: Filters ProviderKeyVault and configured bindings against
     the active cooldown table, zero-quota/unsupported sets, and live local Ollama
     status (e.g., ``["ollama/gemma3:12b (local, ready)", "gemini/gemini-3.5-flash-lite (cloud, 15 RPM)"]``).
     Dead, locked, or cooled-down models never enter the list.
   - Available tool shed: Lists only tools currently permitted under
     ``execution_authorized`` (e.g., ``["search_files", "read_file", "run_diagnostics"]``
     — no browser or mutating write tool when unauthorized).
   - Playbook menu: 4 pre-tested execution patterns.

2. Coordinator Setup Decision (1 fast LLM call, strict JSON schema):
   - Feeds the user's goal + Pre-Flight Manifest to the Coordinator on turn 0.
   - Emits a strictly validated ``SwarmBlueprint``:
     * ``playbook``: one of ``direct_single_agent``, ``iterative_debug_loop``,
       ``parallel_research_swarm``, or ``blocked_missing_capability``.
     * ``role_bindings``: maps ONLY the roles required by that playbook to model IDs
       chosen strictly from the manifest's healthy list.
     * ``stop_condition``: defines what counts as done—or returns
       ``blocked_missing_capability`` immediately if the mission requires a
       browser/write tool that isn't in the manifest.

3. Deterministic Harness Execution (Python runs the chosen playbook):
   - Executes only the chosen playbook's loop, handling inner ``tool:*`` calls,
     retries, timeouts, and ``CriticVerdictV1`` gates deterministically.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from sabrina_console_desktop.agent_browser_service.protocol import BROWSER_TOOL
from sabrina_console_desktop.agent_desktop_service import DESKTOP_TOOL
from swarm_command.models import AgentRole
from swarm_command.role_binding import ROLE_ALIASES, infer_model_provider, normalize_role_name


VALID_PLAYBOOKS: tuple[str, ...] = (
    "direct_single_agent",
    "iterative_debug_loop",
    "parallel_research_swarm",
    "blocked_missing_capability",
)

PLAYBOOK_REQUIRED_ROLES: dict[str, tuple[str, ...]] = {
    "direct_single_agent": (
        AgentRole.EXECUTION_SPECIALIST.value,
        AgentRole.CRITIC_VERIFIER.value,
    ),
    "iterative_debug_loop": (
        AgentRole.RESEARCH_SCOUT.value,
        AgentRole.EXECUTION_SPECIALIST.value,
        AgentRole.CRITIC_VERIFIER.value,
    ),
    "parallel_research_swarm": (
        AgentRole.PLANNER_ARCHITECT.value,
        AgentRole.RESEARCH_SCOUT.value,
        AgentRole.EXECUTION_SPECIALIST.value,
        AgentRole.CRITIC_VERIFIER.value,
    ),
    "blocked_missing_capability": (),
}

PLAYBOOK_MENU: tuple[dict[str, Any], ...] = (
    {
        "id": "direct_single_agent",
        "description": "Single Execution Specialist pass with inner read-only tools followed by CriticVerdictV1 verification.",
        "required_roles": list(PLAYBOOK_REQUIRED_ROLES["direct_single_agent"]),
    },
    {
        "id": "iterative_debug_loop",
        "description": "Research Scout context scan + iterative Execution Specialist tool/diagnostic repair loop + CriticVerdictV1 gate.",
        "required_roles": list(PLAYBOOK_REQUIRED_ROLES["iterative_debug_loop"]),
    },
    {
        "id": "parallel_research_swarm",
        "description": "Planner decomposition + parallel Research Scout fan-out + Execution Specialist synthesis + CriticVerdictV1 gate.",
        "required_roles": list(PLAYBOOK_REQUIRED_ROLES["parallel_research_swarm"]),
    },
    {
        "id": "blocked_missing_capability",
        "description": "Immediate fail-closed halt when the goal requires a tool (e.g. browser or write_file) absent from available_tool_shed.",
        "required_roles": [],
    },
)

DEFAULT_READONLY_TOOLS: tuple[str, ...] = (
    "search_files",
    "read_file",
    "run_diagnostics",
)

DEFAULT_CLOUD_RPM_HINTS: dict[str, str] = {
    "gemini": "cloud, 15 RPM",
    "openai": "cloud, 60 RPM",
    "anthropic": "cloud, 50 RPM",
    "openrouter": "cloud, 30 RPM",
    "huggingface": "cloud, 30 RPM",
    "cerebras": "wafer-scale, 60 RPM",
    "sambanova": "cloud, 30 RPM",
    "fireworks": "cloud, 60 RPM",
    "nvidia": "nim, 40 RPM",
    "github": "cloud, 15 RPM",
    "cohere": "cloud, 20 RPM",
    "perplexity": "search-ai, 30 RPM",
    "pollinations": "open, 20 RPM",
    "lmstudio": "local-openai, ready",
    "portal": "collective-portal, polite",
    "xai": "cloud, 30 RPM",
    "deepseek": "cloud, 30 RPM",
    "groq": "cloud, 30 RPM",
    "mistral": "cloud, 30 RPM",
    "together": "cloud, 30 RPM",
}

DEFAULT_CLOUD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "gemini": ("gemini/gemini-3.5-flash-lite", "gemini/gemini-4-hydrogen"),
    "openai": ("openai/gpt-5-mini", "openai/gpt-5"),
    "anthropic": ("anthropic/claude-sonnet-4-5",),
    "openrouter": ("openrouter/google/gemini-3.5-flash-lite", "openrouter/qwen/qwen3-32b:free"),
    "huggingface": ("huggingface/Qwen/Qwen2.5-72B-Instruct", "huggingface/deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"),
    "cerebras": ("cerebras/llama-3.3-70b", "cerebras/qwen-3-32b"),
    "sambanova": ("sambanova/Meta-Llama-3.3-70B-Instruct", "sambanova/DeepSeek-R1"),
    "fireworks": ("fireworks/accounts/fireworks/models/llama-v3p3-70b-instruct",),
    "nvidia": ("nvidia/meta/llama-3.3-70b-instruct",),
    "github": ("github/gpt-4.1-mini", "github/DeepSeek-R1"),
    "cohere": ("cohere/command-a-03-2025",),
    "perplexity": ("perplexity/sonar", "perplexity/sonar-reasoning"),
    "pollinations": ("pollinations/openai-fast",),
    "lmstudio": ("lmstudio/local-model",),
    "xai": ("xai/grok-3-mini",),
    "deepseek": ("deepseek/deepseek-chat",),
    "groq": ("groq/llama-3.3-70b-versatile",),
    "mistral": ("mistral/mistral-small-latest",),
    "together": ("together/meta-llama/Llama-3.3-70B-Instruct-Turbo",),
}

NON_CHAT_MODEL_TOKENS: tuple[str, ...] = (
    "embed",
    "tts",
    "imagen",
    "nano-banana",
    "flash-lite-image",
    "flash-image",
    "pro-image",
    "gpt-image",
    "chatgpt-image",
    "gpt-audio",
    "gpt-live",
    "computer-use",
    "robotics",
    "whisper",
    "veo",
    "aqa",
    "moderation",
    "omni-moderation",
    "realtime",
    "transcribe",
    "lyria",
    "genie",
    "decision",
)


class BlueprintValidationError(ValueError):
    """Raised when a Coordinator SwarmBlueprint violates the strict JSON contract or Pre-Flight Manifest."""


def _bare_model_name(provider: str, model_id: str) -> str:
    bare = str(model_id or "").strip().lower()
    for prefix in (f"{provider}/", "models/", "gemini/", "local:", "ollama/"):
        if bare.startswith(prefix):
            bare = bare[len(prefix) :]
    return bare.strip()


def canonicalize_model_id(raw_model: str) -> str:
    """Strip any parenthetical annotation (e.g. ' (local, ready)') and normalize provider prefix."""
    cleaned = str(raw_model or "").strip()
    if " (" in cleaned:
        cleaned = cleaned.split(" (", 1)[0].strip()
    if not cleaned:
        return ""
    while cleaned.lower().startswith("ollama/ollama/"):
        cleaned = cleaned[len("ollama/") :]
    if cleaned.lower().startswith("local:"):
        cleaned = f"ollama/{cleaned[len('local:'):]}"
    prov = infer_model_provider(cleaned)
    if prov != "unknown" and "/" not in cleaned:
        return f"{prov}/{cleaned}"
    return cleaned


def is_model_cooled_down(
    model_id: str,
    *,
    cooldowns: dict[Any, float] | None = None,
    zero_quota_models: set[str] | None = None,
    unsupported_models: set[str] | None = None,
    now_mono: float | None = None,
) -> bool:
    """Return True if a model or its provider is currently in the cooldown / dead model table."""
    canon = canonicalize_model_id(model_id)
    if not canon:
        return True
    prov = infer_model_provider(canon)
    if prov == "unknown" and "/" in canon:
        prov = canon.split("/", 1)[0].lower()
    bare = _bare_model_name(prov, canon)

    zero_set = {str(m).strip().lower() for m in (zero_quota_models or set()) if str(m).strip()}
    if bare in zero_set or canon.lower() in zero_set:
        return True

    unsup_set = {str(m).strip().lower() for m in (unsupported_models or set()) if str(m).strip()}
    if bare in unsup_set or canon.lower() in unsup_set:
        return True

    if not cooldowns:
        return False

    now_val = time.monotonic() if now_mono is None else float(now_mono)
    keys_to_check = (
        prov,
        (prov, "*"),
        (prov, bare),
        (prov, canon.lower()),
        canon,
        canon.lower(),
        bare,
    )
    for k in keys_to_check:
        until = cooldowns.get(k)
        if until is not None and float(until) > now_val:
            return True
    return False


@dataclass(frozen=True)
class PreflightManifest:
    """Layer 1: Deterministic Pre-Flight Manifest assembled in Python with 0 LLM calls."""

    healthy_models: tuple[str, ...]
    healthy_model_ids: tuple[str, ...]
    available_tool_shed: tuple[str, ...]
    playbook_menu: tuple[dict[str, Any], ...] = PLAYBOOK_MENU
    execution_authorized: bool = False
    healthy_media_models: dict[str, tuple[str, ...]] = field(default_factory=dict)
    browser: dict[str, str] = field(default_factory=dict)
    desktop: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy_models": list(self.healthy_models),
            "healthy_model_ids": list(self.healthy_model_ids),
            "healthy_media_models": {k: list(v) for k, v in self.healthy_media_models.items()},
            "available_tool_shed": list(self.available_tool_shed),
            "playbook_menu": [dict(item) for item in self.playbook_menu],
            "execution_authorized": bool(self.execution_authorized),
            **({"browser": dict(self.browser)} if self.browser else {}),
            **({"desktop": dict(self.desktop)} if self.desktop else {}),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def resolve_healthy_media_model(self, capability: str, candidate: str = "") -> str | None:
        """Resolve a Coordinator/worker-chosen media model against `healthy_media_models[capability]`."""
        cap = "tts" if str(capability or "").strip().lower() == "audio" else str(capability or "").strip().lower()
        pool = list(self.healthy_media_models.get(cap) or ())
        raw = str(candidate or "").strip()
        if not raw:
            return pool[0] if pool else None
        canon_cand = canonicalize_model_id(raw).lower()
        bare_cand = canon_cand.split("/", 1)[-1]
        for m_id in pool:
            m_canon = canonicalize_model_id(m_id).lower()
            m_bare = m_canon.split("/", 1)[-1]
            if raw.lower() == m_id.lower() or canon_cand == m_canon or bare_cand == m_bare:
                return m_id
        return None

    def resolve_healthy_model(self, candidate: str) -> str | None:
        """Validate that `candidate` is strictly in the healthy model list and return its canonical ID."""
        raw = str(candidate or "").strip()
        if not raw:
            return None
        canon_candidate = canonicalize_model_id(raw).lower()
        bare_candidate = canon_candidate.split("/", 1)[-1]
        for h_id, h_label in zip(self.healthy_model_ids, self.healthy_models):
            if raw.lower() == h_label.lower() or raw.lower() == h_id.lower():
                return h_id
            h_canon = canonicalize_model_id(h_id).lower()
            if canon_candidate == h_canon:
                return h_id
            h_bare = h_canon.split("/", 1)[-1]
            if bare_candidate == h_bare and (
                "/" not in raw or infer_model_provider(raw) == infer_model_provider(h_id)
            ):
                return h_id
        return None


def build_preflight_manifest(
    *,
    vault: Any | None = None,
    registry: Any | None = None,
    unlocked_providers: Iterable[str] | None = None,
    cooldowns: dict[Any, float] | None = None,
    zero_quota_models: set[str] | None = None,
    unsupported_models: set[str] | None = None,
    ollama_models: Iterable[dict[str, Any] | str] | None = None,
    ollama_ready: bool | None = None,
    candidate_models: Iterable[str] | None = None,
    execution_authorized: bool = False,
    allow_workspace_writes: bool = False,
    allow_browser: bool = False,
    allow_web_grounding: bool = False,
    extra_tools: Iterable[str] | None = None,
    now_mono: float | None = None,
) -> PreflightManifest:
    """Assemble the Layer 1 Deterministic Pre-Flight Manifest with 0 LLM calls.

    - Filters ProviderKeyVault and local Ollama status against `cooldowns`,
      `zero_quota_models`, and `unsupported_models`.
    - Lists only tools permitted under `execution_authorized` (and explicit flags).
    - Attaches the 4-pattern playbook menu.
    """
    unlocked: set[str] = set()
    if unlocked_providers is not None:
        unlocked.update(str(p).strip().lower() for p in unlocked_providers if str(p).strip())
    elif vault is not None and hasattr(vault, "list_providers"):
        try:
            st = vault.list_providers()
            for p in st.get("unlocked_providers") or []:
                if str(p).strip():
                    unlocked.add(str(p).strip().lower())
        except Exception:
            pass

    ollama_entries: list[str] = []
    for item in ollama_models or ():
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("id") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            ollama_entries.append(name)

    is_ollama_ready = bool(ollama_entries) if ollama_ready is None else bool(ollama_ready)
    if is_ollama_ready:
        unlocked.add("ollama")
    else:
        unlocked.discard("ollama")

    healthy_labels: list[str] = []
    healthy_ids: list[str] = []
    seen_canon: set[str] = set()

    def _try_add_model(raw_id: str, descriptor: str = "") -> None:
        canon = canonicalize_model_id(raw_id)
        if not canon:
            return
        prov = infer_model_provider(canon)
        if prov == "unknown" and "/" in canon:
            prov = canon.split("/", 1)[0].lower()
        if prov == "ollama":
            if not is_ollama_ready:
                return
        elif unlocked and prov not in unlocked:
            return
        elif not unlocked and vault is not None and hasattr(vault, "is_provider_unlocked"):
            if not vault.is_provider_unlocked(prov):
                return
        if is_model_cooled_down(
            canon,
            cooldowns=cooldowns,
            zero_quota_models=zero_quota_models,
            unsupported_models=unsupported_models,
            now_mono=now_mono,
        ):
            return
        key = canon.lower()
        if key in seen_canon:
            return
        seen_canon.add(key)
        if not descriptor:
            if prov == "ollama":
                descriptor = "local, ready"
            else:
                descriptor = DEFAULT_CLOUD_RPM_HINTS.get(prov, "cloud, ready")
        healthy_ids.append(canon)
        healthy_labels.append(f"{canon} ({descriptor})")

    # 1. Explicit candidate models (if supplied by caller/tests)
    for cm in candidate_models or ():
        raw_str = str(cm or "").strip()
        if not raw_str:
            continue
        desc = ""
        if " (" in raw_str and raw_str.endswith(")"):
            _, rest = raw_str.split(" (", 1)
            desc = rest[:-1].strip()
        _try_add_model(raw_str, desc)

    # 2. Local ready Ollama models
    if is_ollama_ready:
        for om in ollama_entries:
            _try_add_model(f"ollama/{om}" if not om.startswith("ollama/") else om, "local, ready")

    # 3. Probed models from ProviderKeyVault
    if vault is not None and hasattr(vault, "get_probed_models"):
        try:
            for pm in vault.get_probed_models():
                if not isinstance(pm, dict):
                    continue
                pid = str(pm.get("id") or "").strip()
                pprov = str(pm.get("provider") or "").strip().lower()
                if not pid or any(tok in pid.lower() for tok in NON_CHAT_MODEL_TOKENS):
                    continue
                full_id = pid if "/" in pid else (f"{pprov}/{pid}" if pprov else pid)
                _try_add_model(full_id)
        except Exception:
            pass

    # 4. Safe defaults for unlocked cloud providers
    for prov in sorted(unlocked):
        if prov == "ollama":
            continue
        for default_mid in DEFAULT_CLOUD_CANDIDATES.get(prov, ()):
            _try_add_model(default_mid)

    # 5. Live capability-tagged multimodal catalog (`image`, `tts`, `vision`) from ModelSkillRegistry / builtin catalog
    media_lists: dict[str, list[str]] = {"image": [], "tts": [], "vision": []}
    seen_media: dict[str, set[str]] = {"image": set(), "tts": set(), "vision": set()}

    def _try_add_media(cap: str, raw_id: str, prov_hint: str = "") -> None:
        canon = canonicalize_model_id(raw_id if "/" in raw_id or not prov_hint else f"{prov_hint}/{raw_id}")
        if not canon:
            return
        prov = infer_model_provider(canon)
        if prov == "unknown" and "/" in canon:
            prov = canon.split("/", 1)[0].lower()
        if prov == "ollama":
            if not is_ollama_ready:
                return
        elif unlocked and prov not in unlocked:
            return
        elif not unlocked and vault is not None and hasattr(vault, "is_provider_unlocked"):
            if not vault.is_provider_unlocked(prov):
                return
        else:
            if not unlocked and vault is None:
                return
        if is_model_cooled_down(
            canon,
            cooldowns=cooldowns,
            zero_quota_models=zero_quota_models,
            unsupported_models=unsupported_models,
            now_mono=now_mono,
        ):
            return
        key = canon.lower()
        if key not in seen_media[cap]:
            seen_media[cap].add(key)
            media_lists[cap].append(canon)

    probed_media_entries: list[dict[str, Any]] = []
    if vault is not None and hasattr(vault, "get_probed_models"):
        try:
            for pm in vault.get_probed_models():
                if isinstance(pm, dict):
                    probed_media_entries.append(pm)
        except Exception:
            pass

    probed_media_entries.sort(
        key=lambda pm: (
            1 if any(leg in str(pm.get("id") or "").lower() for leg in ("gemini-2.5-", "gemini-2.0-", "gemini-1.5-", "-pro-image")) else 0,
            0 if any(fast in str(pm.get("id") or "").lower() for fast in ("gemini-3.1-flash-image", "nano-banana")) else 1,
        )
    )
    for pm in probed_media_entries:
        pid = str(pm.get("id") or "").strip()
        pprov = str(pm.get("provider") or "").strip().lower()
        plow = pid.lower()
        if not pid or any(leg in plow for leg in ("gemini-2.5-", "gemini-2.0-", "gemini-1.5-")):
            continue
        if any(itok in plow for itok in ("-image", "nano-banana", "imagen", "gpt-image", "chatgpt-image")):
            _try_add_media("image", pid, pprov)
        if "tts" in plow:
            _try_add_media("tts", pid, pprov)

    catalog_entries: list[dict[str, Any]] = []
    if registry is not None and hasattr(registry, "search_models"):
        try:
            res_cat = registry.search_models(unlocked_only=False, include_deprecated=False, limit=200)
            catalog_entries.extend(res_cat.get("items") or [])
        except Exception:
            pass
    if not catalog_entries:
        try:
            from swarm_console.registry import BUILTIN_GEMINI_MODELS

            catalog_entries.extend(dict(m) for m in BUILTIN_GEMINI_MODELS)
        except Exception:
            pass

    for entry in catalog_entries:
        if not isinstance(entry, dict) or entry.get("deprecated"):
            continue
        mid = str(entry.get("id") or "").strip()
        mprov = str(entry.get("provider") or "").strip().lower()
        mlow = mid.lower()
        caps = {str(c).strip().lower() for c in (entry.get("capabilities") or [])}
        if not mid or not caps:
            continue
        is_dedicated_img = (
            any(itok in mlow for itok in ("-image", "nano-banana", "imagen"))
            or ("image" in caps and "chat" not in caps and "embedding" not in caps and "decision" not in caps and "video" not in caps)
        )
        if is_dedicated_img:
            _try_add_media("image", mid, mprov)
        if "tts" in caps or "tts" in mlow:
            _try_add_media("tts", mid, mprov)
        if "chat" in caps and "image" in caps and not any(itok in mlow for itok in ("-image", "nano-banana", "imagen")):
            _try_add_media("vision", mid, mprov)

    if is_ollama_ready:
        for om in ollama_entries:
            low_om = om.lower()
            if any(vtok in low_om for vtok in ("gemma3", "llava", "vision", "vl", "minicpm-v", "pixtral", "moondream")):
                _try_add_media("vision", f"ollama/{om}" if not om.startswith("ollama/") else om, "ollama")

    # Add multi-provider image generators when their providers are unlocked in Vault
    extra_image_defaults = (
        ("huggingface", "huggingface/black-forest-labs/FLUX.1-schnell"),
        ("together", "together/black-forest-labs/FLUX.1-schnell-Free"),
        ("fireworks", "fireworks/accounts/fireworks/models/flux-1-schnell-fp8"),
        ("pollinations", "pollinations/flux"),
        ("openai", "openai/gpt-image-1"),
    )
    for img_prov, img_mid in extra_image_defaults:
        _try_add_media("image", img_mid, img_prov)

    healthy_media = {k: tuple(v) for k, v in media_lists.items() if v}

    # Assemble available tool shed strictly under execution_authorized & flags
    tools: list[str] = list(DEFAULT_READONLY_TOOLS)
    if allow_workspace_writes or execution_authorized:
        if "write_file" not in tools:
            tools.append("write_file")
        if healthy_media.get("image") and "generate_image" not in tools:
            tools.append("generate_image")
        if healthy_media.get("tts") and "generate_audio" not in tools:
            tools.append("generate_audio")
    if allow_web_grounding:
        for wtool in ("web_search", "fetch_url"):
            if wtool not in tools:
                tools.append(wtool)
    if allow_browser and execution_authorized:
        if "browser_open" not in tools:
            tools.append("browser_open")
    for et in extra_tools or ():
        clean_tool = str(et or "").strip()
        if not clean_tool or clean_tool in {BROWSER_TOOL, DESKTOP_TOOL}:
            continue
        # Block browser or mutating tools when execution_authorized is False (unless allow_workspace_writes for write_file)
        if not execution_authorized and clean_tool in {"browser", "browser_open", "browser_action", "shell_exec"}:
            continue
        if not execution_authorized and not allow_workspace_writes and clean_tool in {"write_file", "edit_file", "delete_file", "generate_image", "generate_audio"}:
            continue
        if clean_tool not in tools:
            tools.append(clean_tool)

    return PreflightManifest(
        healthy_models=tuple(healthy_labels),
        healthy_model_ids=tuple(healthy_ids),
        available_tool_shed=tuple(tools),
        playbook_menu=PLAYBOOK_MENU,
        execution_authorized=False,
        healthy_media_models=healthy_media,
    )


@dataclass(frozen=True)
class SwarmBlueprint:
    """Layer 2: Validated turn-0 Coordinator setup decision."""

    playbook: str
    role_bindings: dict[str, str] = field(default_factory=dict)
    stop_condition: str = ""
    media_bindings: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "playbook": self.playbook,
            "role_bindings": dict(self.role_bindings),
            "stop_condition": self.stop_condition,
        }
        if self.media_bindings:
            out["media_bindings"] = dict(self.media_bindings)
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def _unique_object_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise BlueprintValidationError(f"Duplicate JSON key in SwarmBlueprint: {k!r}")
        out[k] = v
    return out


def parse_swarm_blueprint(raw: str | dict[str, Any], manifest: PreflightManifest) -> SwarmBlueprint:
    """Strictly validate a turn-0 Coordinator SwarmBlueprint against the Pre-Flight Manifest."""
    if isinstance(raw, dict):
        data = dict(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", text, flags=re.I | re.S)
        candidate = fenced.group(1).strip() if fenced else text
        if not candidate:
            raise BlueprintValidationError("SwarmBlueprint JSON response is empty.")
        try:
            parsed = json.loads(candidate, object_pairs_hook=_unique_object_hook)
        except Exception as exc:
            raise BlueprintValidationError(f"Invalid SwarmBlueprint JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise BlueprintValidationError("SwarmBlueprint must be a JSON object.")
        data = parsed
    else:
        raise BlueprintValidationError("SwarmBlueprint must be a JSON string or dict.")

    required_keys = {"playbook", "role_bindings", "stop_condition"}
    allowed_keys = {"playbook", "role_bindings", "stop_condition", "media_bindings"}
    missing_keys = required_keys - data.keys()
    if missing_keys:
        raise BlueprintValidationError(
            f"SwarmBlueprint missing required keys: {sorted(missing_keys)}"
        )
    extra_keys = data.keys() - allowed_keys
    if extra_keys:
        raise BlueprintValidationError(
            f"SwarmBlueprint contains unexpected keys: {sorted(extra_keys)}"
        )

    playbook = str(data.get("playbook") or "").strip()
    if playbook not in VALID_PLAYBOOKS:
        raise BlueprintValidationError(
            f"Invalid playbook {playbook!r}; must be one of {list(VALID_PLAYBOOKS)}."
        )

    stop_condition = data.get("stop_condition")
    if not isinstance(stop_condition, str) or not stop_condition.strip():
        raise BlueprintValidationError("SwarmBlueprint.stop_condition must be a non-empty string.")
    clean_stop = stop_condition.strip()

    raw_bindings = data.get("role_bindings")
    if not isinstance(raw_bindings, dict):
        raise BlueprintValidationError("SwarmBlueprint.role_bindings must be a JSON object.")

    required_roles = PLAYBOOK_REQUIRED_ROLES[playbook]
    if playbook == "blocked_missing_capability":
        if raw_bindings:
            raise BlueprintValidationError(
                "blocked_missing_capability playbook must have empty role_bindings ({})."
            )
        return SwarmBlueprint(
            playbook=playbook,
            role_bindings={},
            stop_condition=clean_stop,
        )

    normalized_bindings: dict[str, str] = {}
    for raw_role, raw_model in raw_bindings.items():
        if not isinstance(raw_role, str) or not isinstance(raw_model, str):
            raise BlueprintValidationError("role_bindings keys and values must be strings.")
        role_key = raw_role.strip().lower().replace("-", "_")
        if role_key not in ROLE_ALIASES:
            raise BlueprintValidationError(f"Unknown role in role_bindings: {raw_role!r}")
        canon_role = normalize_role_name(role_key)
        if canon_role in normalized_bindings:
            raise BlueprintValidationError(f"Duplicate role binding for {canon_role!r}")
        resolved_healthy = manifest.resolve_healthy_model(raw_model)
        if resolved_healthy is None:
            raise BlueprintValidationError(
                f"Model {raw_model!r} bound to role {canon_role!r} is not in the Pre-Flight Manifest healthy_models list: "
                f"{list(manifest.healthy_model_ids)}"
            )
        normalized_bindings[canon_role] = resolved_healthy

    expected_set = set(required_roles)
    actual_set = set(normalized_bindings.keys())
    if actual_set != expected_set:
        missing_roles = sorted(expected_set - actual_set)
        unrequired_roles = sorted(actual_set - expected_set)
        details: list[str] = []
        if missing_roles:
            details.append(f"missing required roles {missing_roles}")
        if unrequired_roles:
            details.append(f"extra unrequired roles {unrequired_roles}")
        raise BlueprintValidationError(
            f"Playbook {playbook!r} requires exact roles {list(required_roles)}; got {', '.join(details)}."
        )

    normalized_media: dict[str, str] = {}
    if "media_bindings" in data and data["media_bindings"] is not None:
        raw_media = data["media_bindings"]
        if not isinstance(raw_media, dict):
            raise BlueprintValidationError("SwarmBlueprint.media_bindings must be a JSON object.")
        for m_cap, m_val in raw_media.items():
            if not isinstance(m_cap, str) or not isinstance(m_val, str):
                raise BlueprintValidationError("media_bindings keys and values must be strings.")
            cap_key = "tts" if m_cap.strip().lower() == "audio" else m_cap.strip().lower()
            if cap_key not in {"image", "tts", "vision"}:
                raise BlueprintValidationError(f"Unknown capability in media_bindings: {m_cap!r}")
            resolved_m = manifest.resolve_healthy_media_model(cap_key, m_val)
            if resolved_m is None:
                raise BlueprintValidationError(
                    f"Media model {m_val!r} for capability {cap_key!r} is not in healthy_media_models: "
                    f"{dict(manifest.healthy_media_models)}"
                )
            normalized_media[cap_key] = resolved_m

    return SwarmBlueprint(
        playbook=playbook,
        role_bindings={role: normalized_bindings[role] for role in required_roles},
        stop_condition=clean_stop,
        media_bindings=normalized_media,
    )


def detect_missing_capability(goal: str, manifest: PreflightManifest) -> str | None:
    """Return a stop_condition reason if the goal requires a capability absent from `manifest.available_tool_shed`."""
    low = str(goal or "").strip().lower()
    shed = set(manifest.available_tool_shed)

    needs_browser = bool(
        re.search(
            r"\b(open\s+(?:in\s+)?(?:browser|chrome|edge|firefox|internet\s+explorer)|"
            r"navigate\s+to\s+https?://|browse\s+https?://|click\s+on\s+page|scrape\s+live\s+webpage|"
            r"linkedin\s+profile\s+opened\s+in)\b",
            low,
        )
    )
    if needs_browser and not ({"browser", "browser_open", "browser_action", BROWSER_TOOL, DESKTOP_TOOL} & shed):
        return (
            "Blocked: Mission requires a browser tool, which is not permitted in available_tool_shed "
            f"({list(manifest.available_tool_shed)}) under execution_authorized={manifest.execution_authorized}."
        )

    needs_write = bool(
        re.search(
            r"\b(write_file|modify\s+files?\s+in\s+place|overwrite\s+source\s+file|"
            r"apply\s+patch\s+to\s+disk|create\s+and\s+save\s+file)\b",
            low,
        )
    )
    if needs_write and not ({"write_file", "edit_file"} & shed):
        return (
            "Blocked: Mission requires a file-writing tool (write_file), which is not present in "
            f"available_tool_shed ({list(manifest.available_tool_shed)})."
        )

    if not manifest.healthy_model_ids:
        return "Blocked: No healthy models are available in the Pre-Flight Manifest (all models locked or cooled down)."

    return None


def build_coordinator_setup_prompts(goal: str, manifest: PreflightManifest) -> tuple[str, str]:
    """Construct the system and user prompts for the Turn-0 Coordinator Setup Decision."""
    system_prompt = (
        "You are Sabrina's Context-First Swarm Coordinator on Turn 0.\n"
        "Your ONLY job is to inspect the user's goal and the deterministic Pre-Flight Manifest, "
        "then emit a single strict JSON object matching SwarmBlueprint:\n"
        "{\n"
        '  "playbook": "direct_single_agent" | "iterative_debug_loop" | "parallel_research_swarm" | "blocked_missing_capability",\n'
        '  "role_bindings": { "<required_role>": "<healthy_model_id>" },\n'
        '  "media_bindings": { "image": "<optional_healthy_image_model>", "tts": "<optional_healthy_tts_model>", "vision": "<optional_healthy_vision_model>" },\n'
        '  "stop_condition": "<concise verifiable completion condition or block reason>"\n'
        "}\n"
        "STRICT RULES:\n"
        "1. If the goal requires a tool not in `available_tool_shed` (such as a browser or write_file tool) "
        "or if `healthy_models` is empty, you MUST pick `\"blocked_missing_capability\"` with `\"role_bindings\": {}`.\n"
        "2. Map ONLY the exact roles required by the chosen `playbook` in `role_bindings`.\n"
        "3. Every model ID in `role_bindings` MUST be chosen strictly from `healthy_model_ids` in the manifest.\n"
        "4. If the mission generates or inspects images/audio and `healthy_media_models` is non-empty, choose `media_bindings` strictly from `healthy_media_models` (omit `media_bindings` if unused).\n"
        "5. Output raw JSON only—no markdown commentary."
    )
    if manifest.browser or manifest.desktop:
        system_prompt += (
            "\nThe capability named in the manifest is authorized for this mission. "
            "Prefer direct_single_agent. Define the stop condition in terms "
            "of visible evidence after acting. Preserve every part of the user's completion criteria, "
            "including any actions the user asks to do personally, without adding new requirements. "
            "The selected browser_session or desktop_session remains authorized even when "
            "execution_authorized is false; that flag does not revoke this selected session. "
            "Do not assume other execution or write tools are permitted."
        )
    if manifest.browser:
        system_prompt += (
            "\nThe browser_session tool in available_tool_shed opens and operates the selected browser. "
            "It supplies browser access; do not require a separately named browser tool."
        )
    if manifest.desktop:
        system_prompt += (
            "\nThe desktop_session tool in available_tool_shed observes and operates the selected window."
        )
        if str(manifest.desktop.get("app") or "").lower() in {"msedge.exe", "chrome.exe", "brave.exe"}:
            system_prompt += (
                " That selected window is a web browser, so desktop_session supplies browser access; "
                "do not require a separately named browser tool."
            )
    user_prompt = (
        f"Mission Goal:\n{goal.strip()}\n\n"
        f"Deterministic Pre-Flight Manifest (Ground Truth):\n{manifest.to_json()}"
    )
    return system_prompt, user_prompt


def deterministic_fallback_blueprint(goal: str, manifest: PreflightManifest) -> SwarmBlueprint:
    """Produce a deterministic validated SwarmBlueprint when no LLM caller is attached or turn-0 check blocks."""
    missing_reason = detect_missing_capability(goal, manifest)
    if missing_reason:
        return SwarmBlueprint(
            playbook="blocked_missing_capability",
            role_bindings={},
            stop_condition=missing_reason,
        )

    healthy = list(manifest.healthy_model_ids)
    # Prefer fast cloud model for planning/verifying and local model (if present) for heavy file reading
    cloud_models = [m for m in healthy if not m.startswith("ollama/")]
    local_models = [m for m in healthy if m.startswith("ollama/")]
    fast_plan_verify = cloud_models[0] if cloud_models else healthy[0]
    heavy_reader = local_models[0] if local_models else healthy[0]
    exec_model = cloud_models[0] if cloud_models else healthy[0]

    low = goal.lower()
    if manifest.browser or manifest.desktop:
        playbook = "direct_single_agent"
    elif any(k in low for k in ("parallel", "fan-out", "fanout", "multi-part", "## ", "1.", "2.")) or "\n" in goal.strip():
        playbook = "parallel_research_swarm"
    elif any(k in low for k in ("debug", "fix", "diagnos", "test", "trace", "repair", "audit", "inspect")):
        playbook = "iterative_debug_loop"
    else:
        playbook = "direct_single_agent"

    required = PLAYBOOK_REQUIRED_ROLES[playbook]
    bindings: dict[str, str] = {}
    for role in required:
        if role in (AgentRole.PLANNER_ARCHITECT.value, AgentRole.CRITIC_VERIFIER.value):
            bindings[role] = fast_plan_verify
        elif role == AgentRole.RESEARCH_SCOUT.value:
            bindings[role] = heavy_reader
        else:
            bindings[role] = exec_model

    return SwarmBlueprint(
        playbook=playbook,
        role_bindings=bindings,
        stop_condition=f"Deliverable satisfies '{goal.strip()[:120]}' and passes CriticVerdictV1 verification.",
    )


def decide_swarm_blueprint(
    goal: str,
    manifest: PreflightManifest,
    *,
    llm_caller: Callable[[str, str, str, str], dict[str, Any]] | None = None,
    coordinator_model: str | None = None,
    strict: bool = False,
) -> tuple[SwarmBlueprint, dict[str, Any]]:
    """Layer 2: Run the 1 fast Coordinator LLM call to obtain a validated SwarmBlueprint.

    Returns ``(blueprint, telemetry_dict)``.
    If ``strict=True``, raises ``BlueprintValidationError`` if the Coordinator emits an
    invalid blueprint (e.g. binding a cooled-down model or wrong roles for the playbook).
    """
    missing_reason = detect_missing_capability(goal, manifest)
    if not manifest.healthy_model_ids:
        bp = SwarmBlueprint(
            playbook="blocked_missing_capability",
            role_bindings={},
            stop_condition=missing_reason or "Blocked: No healthy models available in Pre-Flight Manifest.",
        )
        return bp, {"llm_calls": 0, "ok": True, "source": "preflight_no_healthy_models"}

    if llm_caller is None:
        bp = deterministic_fallback_blueprint(goal, manifest)
        return bp, {"llm_calls": 0, "ok": True, "source": "deterministic_offline"}

    chosen_coord_model = coordinator_model
    if not chosen_coord_model or manifest.resolve_healthy_model(chosen_coord_model) is None:
        # Pick Gemini Neo / Neon orchestrator if healthy, else first healthy cloud model, else first healthy model
        neo_candidates = [m for m in manifest.healthy_model_ids if any(k in m.lower() for k in ("gemini-4-neon", "gemini-neo"))]
        cloud = [m for m in manifest.healthy_model_ids if not m.startswith("ollama/")]
        chosen_coord_model = neo_candidates[0] if neo_candidates else (cloud[0] if cloud else manifest.healthy_model_ids[0])
    else:
        chosen_coord_model = manifest.resolve_healthy_model(chosen_coord_model) or manifest.healthy_model_ids[0]

    sys_prompt, user_prompt = build_coordinator_setup_prompts(goal, manifest)
    res = llm_caller("coordinator", chosen_coord_model, sys_prompt, user_prompt)
    telemetry: dict[str, Any] = {
        "llm_calls": 1 if res.get("ok") else 0,
        "ok": bool(res.get("ok")),
        "model": str(res.get("model") or chosen_coord_model),
        "provider": str(res.get("provider") or infer_model_provider(chosen_coord_model)),
        "latency_ms": float(res.get("latency_ms") or 0.0),
        "tokens_used": int(res.get("tokens_used") or 0),
        "source": "coordinator_llm",
    }
    if not res.get("ok"):
        err = str(res.get("error") or "Coordinator setup LLM call failed")
        telemetry["error"] = err
        if strict:
            raise BlueprintValidationError(f"Coordinator Turn-0 LLM call failed: {err}")
        bp = deterministic_fallback_blueprint(goal, manifest)
        telemetry["source"] = "deterministic_fallback_on_llm_error"
        return bp, telemetry

    raw_text = str(res.get("text") or res.get("reply") or "").strip()
    telemetry["raw_output"] = raw_text
    try:
        bp = parse_swarm_blueprint(raw_text, manifest)
    except BlueprintValidationError as exc:
        telemetry["validation_error"] = str(exc)
        if strict:
            raise
        bp = deterministic_fallback_blueprint(goal, manifest)
        telemetry["source"] = "deterministic_fallback_on_schema_error"
        return bp, telemetry

    # Enforce Python safety invariant: if the mission requires a missing capability and the LLM failed to block,
    # Python deterministically overrides to blocked_missing_capability.
    if missing_reason and bp.playbook != "blocked_missing_capability":
        bp = SwarmBlueprint(
            playbook="blocked_missing_capability",
            role_bindings={},
            stop_condition=missing_reason,
        )
        telemetry["enforced_capability_block"] = True

    return bp, telemetry
