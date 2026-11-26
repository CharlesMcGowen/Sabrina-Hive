"""Model Fleet & Skill Registry for Sabrina's Swarm Console.

Discovers, normalizes, indexes, and searches collective, API, external/web,
and local Ollama models alongside custom user-added models, plus a separate
index of local `SKILL.md` skill directories using Python stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

from .protocol import guard_safe_path

if TYPE_CHECKING:
    from .vault import ProviderKeyVault

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
MODELS_DEV_URL = "https://models.dev/api.json"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434/api/tags"
LEGACY_GEMINI_RE = re.compile(r"(?:^|/)(?:gemini-(?:1\.5|2\.0|2\.5)-)", re.IGNORECASE)

BUILTIN_GEMINI_MODELS: tuple[dict[str, Any], ...] = (
    {
        "id": "gemini-4-carbon",
        "name": "Gemini 4 Carbon",
        "provider": "gemini",
        "capabilities": ["chat", "reasoning", "image", "audio", "video"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-4-neon",
        "name": "Gemini 4 Neon",
        "provider": "gemini",
        "capabilities": ["chat", "reasoning", "orchestrator", "image", "audio", "video"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-neo",
        "name": "Gemini Neo Orchestrator",
        "provider": "gemini",
        "capabilities": ["chat", "reasoning", "orchestrator", "image", "audio", "video"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-neo-orchestrator",
        "name": "Gemini Neo Orchestrator",
        "provider": "gemini",
        "capabilities": ["chat", "reasoning", "orchestrator", "image", "audio", "video"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-4-argon",
        "name": "Gemini 4 Argon",
        "provider": "gemini",
        "capabilities": ["chat", "reasoning", "image", "audio", "video"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-4-hydrogen",
        "name": "Gemini 4 Hydrogen",
        "provider": "gemini",
        "capabilities": ["chat", "reasoning", "image", "audio", "video"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-3.5-flash-lite",
        "name": "Gemini 3.5 Flash-Lite",
        "provider": "gemini",
        "capabilities": ["chat", "image", "audio", "video"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-3.1-flash-lite",
        "name": "Gemini 3.1 Flash-Lite",
        "provider": "gemini",
        "capabilities": ["chat"],
        "context_window": 1_000_000,
        "max_output_tokens": 65_536,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-3.5-transcribe",
        "name": "Gemini 3.5 Transcribe",
        "provider": "gemini",
        "capabilities": ["audio", "stt"],
        "context_window": 131_072,
        "max_output_tokens": 16_384,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-nano-banana-2.1",
        "name": "Gemini Nano Banana 2.1",
        "provider": "gemini",
        "capabilities": ["image"],
        "context_window": 131_072,
        "max_output_tokens": 32_768,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-3-pro-image",
        "name": "Gemini 3 Pro Image",
        "provider": "gemini",
        "capabilities": ["image"],
        "context_window": 65_536,
        "max_output_tokens": 32_768,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-3.1-flash-lite-image",
        "name": "Gemini 3.1 Flash-Lite Image",
        "provider": "gemini",
        "capabilities": ["image"],
        "context_window": 65_536,
        "max_output_tokens": 32_768,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-3.8-flash-tts",
        "name": "Gemini 3.8 Flash TTS",
        "provider": "gemini",
        "capabilities": ["audio", "tts"],
        "context_window": 65_536,
        "max_output_tokens": 16_384,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-3.8-flash-lite-tts",
        "name": "Gemini 3.8 Flash-Lite TTS",
        "provider": "gemini",
        "capabilities": ["audio", "tts"],
        "context_window": 65_536,
        "max_output_tokens": 16_384,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "lyria-3.5",
        "name": "Lyria 3.5",
        "provider": "gemini",
        "capabilities": ["audio"],
        "context_window": 32_768,
        "max_output_tokens": None,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-omni-1.2-flash",
        "name": "Gemini Omni 1.2 Flash",
        "provider": "gemini",
        "capabilities": ["video", "image", "audio"],
        "context_window": 131_072,
        "max_output_tokens": None,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "models/genie-4",
        "name": "Genie 4 Live World",
        "provider": "gemini",
        "capabilities": ["video", "audio", "world"],
        "context_window": 131_072,
        "max_output_tokens": None,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemma-4-31b-it",
        "name": "Gemma 4 31B IT",
        "provider": "gemma",
        "capabilities": ["chat", "reasoning"],
        "context_window": 131_072,
        "max_output_tokens": 32_768,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemma-4-26b-a4b-it",
        "name": "Gemma 4 26B A4B MoE",
        "provider": "gemma",
        "capabilities": ["chat", "reasoning"],
        "context_window": 131_072,
        "max_output_tokens": 32_768,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-decision-1",
        "name": "Gemini Decision 1",
        "provider": "gemini",
        "capabilities": ["decision", "image", "audio", "video"],
        "context_window": 262_144,
        "max_output_tokens": 0,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-embedding-2",
        "name": "Gemini Embedding 2",
        "provider": "gemini",
        "capabilities": ["embedding", "image", "audio", "video"],
        "context_window": 8_192,
        "max_output_tokens": 0,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "gemini-embedding-001",
        "name": "Gemini Embedding 001",
        "provider": "gemini",
        "capabilities": ["embedding"],
        "context_window": 2_048,
        "max_output_tokens": 0,
        "deprecated": False,
        "source": "builtin",
    },
    # OpenAI built-in catalog entries (augmented by OpenRouter & models.dev when online)
    {
        "id": "openai/gpt-5",
        "name": "GPT-5",
        "provider": "openai",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 400_000,
        "max_output_tokens": 128_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/gpt-5-mini",
        "name": "GPT-5 Mini",
        "provider": "openai",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 400_000,
        "max_output_tokens": 128_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/gpt-4.1",
        "name": "GPT-4.1",
        "provider": "openai",
        "capabilities": ["chat", "image"],
        "context_window": 1_047_576,
        "max_output_tokens": 32_768,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/gpt-4.1-mini",
        "name": "GPT-4.1 Mini",
        "provider": "openai",
        "capabilities": ["chat", "image"],
        "context_window": 1_047_576,
        "max_output_tokens": 32_768,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/gpt-4o",
        "name": "GPT-4o",
        "provider": "openai",
        "capabilities": ["chat", "image", "audio"],
        "context_window": 128_000,
        "max_output_tokens": 16_384,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/o3",
        "name": "OpenAI o3",
        "provider": "openai",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 200_000,
        "max_output_tokens": 100_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/o3-pro",
        "name": "OpenAI o3-pro",
        "provider": "openai",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 200_000,
        "max_output_tokens": 100_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/o4-mini",
        "name": "OpenAI o4-mini",
        "provider": "openai",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 200_000,
        "max_output_tokens": 100_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/codex-astra",
        "name": "Codex Astra",
        "provider": "openai",
        "capabilities": ["chat", "reasoning"],
        "context_window": 400_000,
        "max_output_tokens": 128_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/gpt-image-1",
        "name": "GPT Image 1",
        "provider": "openai",
        "capabilities": ["image"],
        "context_window": 32_768,
        "max_output_tokens": None,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "openai/text-embedding-3-large",
        "name": "Text Embedding 3 Large",
        "provider": "openai",
        "capabilities": ["embedding"],
        "context_window": 8_191,
        "max_output_tokens": 0,
        "deprecated": False,
        "source": "builtin",
    },
    # Anthropic built-in catalog entries
    {
        "id": "anthropic/claude-opus-4",
        "name": "Claude Opus 4",
        "provider": "anthropic",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 200_000,
        "max_output_tokens": 32_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "anthropic/claude-sonnet-4",
        "name": "Claude Sonnet 4",
        "provider": "anthropic",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 200_000,
        "max_output_tokens": 64_000,
        "deprecated": False,
        "source": "builtin",
    },
    {
        "id": "anthropic/claude-3-7-sonnet",
        "name": "Claude 3.7 Sonnet",
        "provider": "anthropic",
        "capabilities": ["chat", "reasoning", "image"],
        "context_window": 200_000,
        "max_output_tokens": 64_000,
        "deprecated": False,
        "source": "builtin",
    },
)


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _to_per_1m_price(value: Any, *, per_token: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num < 0:
        return None
    if per_token:
        num *= 1_000_000.0
    return round(num, 4)


def _infer_capabilities_from_text(text: str) -> list[str]:
    lower = text.lower()
    caps: list[str] = []
    if any(tok in lower for tok in ("embed", "embedding")):
        caps.append("embedding")
    if any(tok in lower for tok in ("image", "vision", "flux", "sdxl", "imagen", "nano-banana")):
        caps.append("image")
    if any(tok in lower for tok in ("audio", "tts", "speech", "whisper", "transcribe", "lyria")):
        caps.append("audio")
    if any(tok in lower for tok in ("video", "omni", "veo", "genie")):
        caps.append("video")
    if any(tok in lower for tok in ("reason", "thinking", "r1", "o1", "o3", "o4", "carbon", "neon", "neo", "argon")):
        caps.append("reasoning")
    if any(tok in lower for tok in ("orchestrat", "coordinator", "planner", "swarm")):
        caps.append("orchestrator")
    if not caps or any(tok in lower for tok in ("chat", "instruct", "it", "claude", "gpt", "gemini", "llama", "qwen", "mistral")):
        if "embedding" not in caps and "chat" not in caps:
            caps.insert(0, "chat")
    return caps


VALID_MODEL_TIERS: tuple[str, ...] = ("collective", "api", "external", "local")

_API_PROVIDERS: frozenset[str] = frozenset(
    {
        "gemini",
        "google",
        "gemma",
        "openai",
        "anthropic",
        "xai",
        "x-ai",
        "deepseek",
        "groq",
        "mistral",
        "together",
    }
)


def _infer_model_tier(provider: str, source: str, raw_tier: Any = None) -> str:
    explicit = str(raw_tier or "").strip().lower()
    if explicit in VALID_MODEL_TIERS:
        return explicit
    src = (source or "").strip().lower()
    prov = (provider or "").strip().lower()
    if src == "collective":
        return "collective"
    if prov in ("ollama", "local") or src == "ollama" or "ollama" in prov:
        return "local"
    if src in ("builtin", "live_probe") or prov in _API_PROVIDERS:
        return "api"
    return "external"


def normalize_model_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and normalize a model dict into the canonical registry schema."""
    if not isinstance(raw, dict):
        return None
    model_id = str(raw.get("id") or raw.get("name") or "").strip()
    if not model_id:
        return None

    name = str(raw.get("name") or model_id).strip()
    provider = str(raw.get("provider") or "unknown").strip().lower()
    caps_raw = raw.get("capabilities")
    if isinstance(caps_raw, list):
        capabilities = sorted({str(c).strip().lower() for c in caps_raw if str(c).strip()})
    else:
        capabilities = _infer_capabilities_from_text(f"{model_id} {name}")

    pricing_raw = raw.get("pricing")
    pricing: dict[str, float | None] | None = None
    if isinstance(pricing_raw, dict):
        in_p = _to_per_1m_price(pricing_raw.get("input_per_1m"))
        out_p = _to_per_1m_price(pricing_raw.get("output_per_1m"))
        if in_p is not None or out_p is not None:
            pricing = {"input_per_1m": in_p, "output_per_1m": out_p}

    retiring_at = raw.get("retiring_at")
    retiring_str = str(retiring_at).strip() if retiring_at else None
    deprecated = bool(raw.get("deprecated", False)) or bool(LEGACY_GEMINI_RE.search(model_id))
    source = str(raw.get("source") or provider)
    tier = _infer_model_tier(provider, source, raw.get("tier"))

    role_val = str(raw.get("role") or "").strip() or None
    residency_val = str(raw.get("residency") or "").strip() or None
    size_val = str(raw.get("size_gb") or raw.get("size") or "").strip() or None
    endpoint_val = str(raw.get("endpoint") or "").strip() or None

    return {
        "id": model_id,
        "name": name,
        "provider": provider,
        "tier": tier,
        "capabilities": capabilities or ["chat"],
        "context_window": _to_int(raw.get("context_window")),
        "max_output_tokens": _to_int(raw.get("max_output_tokens")),
        "pricing": pricing,
        "deprecated": deprecated,
        "retiring_at": retiring_str,
        "source": source,
        "role": role_val,
        "residency": residency_val,
        "size_gb": size_val,
        "endpoint": endpoint_val,
        "custom": bool(raw.get("custom", False)),
    }


def normalize_openrouter_catalog(payload: Any) -> list[dict[str, Any]]:
    """Normalize an OpenRouter `/api/v1/models` JSON payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    results: list[dict[str, Any]] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id:
            continue
        provider = model_id.split("/", 1)[0] if "/" in model_id else "openrouter"
        arch = item.get("architecture") if isinstance(item.get("architecture"), dict) else {}
        modality = str(arch.get("modality") or "")
        in_mods = arch.get("input_modalities") if isinstance(arch.get("input_modalities"), list) else []
        out_mods = arch.get("output_modalities") if isinstance(arch.get("output_modalities"), list) else []

        caps = set(_infer_capabilities_from_text(f"{model_id} {item.get('name', '')} {modality}"))
        for m in (*in_mods, *out_mods):
            m_low = str(m).strip().lower()
            if m_low in ("text",):
                caps.add("chat")
            elif m_low in ("image", "audio", "video", "embedding"):
                caps.add(m_low)

        pricing_raw = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        top_provider = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
        norm = normalize_model_record(
            {
                "id": f"openrouter/{model_id}",
                "name": item.get("name") or model_id,
                "provider": f"openrouter:{provider}",
                "capabilities": sorted(caps),
                "context_window": item.get("context_length") or top_provider.get("context_length"),
                "max_output_tokens": top_provider.get("max_completion_tokens"),
                "pricing": {
                    "input_per_1m": _to_per_1m_price(pricing_raw.get("prompt"), per_token=True),
                    "output_per_1m": _to_per_1m_price(pricing_raw.get("completion"), per_token=True),
                },
                "deprecated": bool(item.get("deprecated", False)),
                "retiring_at": item.get("deprecation_date"),
                "source": "openrouter",
            }
        )
        if norm:
            results.append(norm)
    return results


def normalize_models_dev_catalog(payload: Any) -> list[dict[str, Any]]:
    """Normalize a `models.dev/api.json` provider-keyed catalog."""
    if not isinstance(payload, dict):
        return []
    results: list[dict[str, Any]] = []
    for provider_id, provider_obj in payload.items():
        if not isinstance(provider_obj, dict):
            continue
        models_map = provider_obj.get("models")
        if not isinstance(models_map, dict):
            continue
        for model_key, item in models_map.items():
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get("id") or model_key).strip()
            if not raw_id:
                continue
            limit = item.get("limit") if isinstance(item.get("limit"), dict) else {}
            cost = item.get("cost") if isinstance(item.get("cost"), dict) else {}
            modalities = item.get("modalities") if isinstance(item.get("modalities"), dict) else {}
            in_mods = modalities.get("input") if isinstance(modalities.get("input"), list) else []
            out_mods = modalities.get("output") if isinstance(modalities.get("output"), list) else []

            caps = set(_infer_capabilities_from_text(f"{raw_id} {item.get('name', '')}"))
            if item.get("reasoning"):
                caps.add("reasoning")
            for m in (*in_mods, *out_mods):
                m_low = str(m).strip().lower()
                if m_low in ("image", "audio", "video", "embedding"):
                    caps.add(m_low)
                elif m_low == "text":
                    caps.add("chat")

            status = str(item.get("status") or "").strip().lower()
            norm = normalize_model_record(
                {
                    "id": f"{provider_id}/{raw_id}",
                    "name": item.get("name") or raw_id,
                    "provider": str(provider_id).lower(),
                    "capabilities": sorted(caps),
                    "context_window": limit.get("context"),
                    "max_output_tokens": limit.get("output"),
                    "pricing": {
                        "input_per_1m": _to_per_1m_price(cost.get("input")),
                        "output_per_1m": _to_per_1m_price(cost.get("output")),
                    },
                    "deprecated": status in ("deprecated", "retired"),
                    "retiring_at": item.get("release_date") if status == "deprecated" else None,
                    "source": "models_dev",
                }
            )
            if norm:
                results.append(norm)
    return results


def normalize_ollama_catalog(payload: Any) -> list[dict[str, Any]]:
    """Normalize an Ollama `/api/tags` JSON payload."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return []
    results: list[dict[str, Any]] = []
    for item in payload["models"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("model") or "").strip()
        if not name:
            continue
        size_bytes = _to_int(item.get("size"))
        size_gb = f"{size_bytes / (1024**3):.1f} GB" if size_bytes else None
        norm = normalize_model_record(
            {
                "id": f"ollama/{name}",
                "name": name,
                "provider": "ollama",
                "tier": "local",
                "capabilities": _infer_capabilities_from_text(name),
                "context_window": None,
                "max_output_tokens": None,
                "deprecated": False,
                "source": "ollama",
                "residency": str(item.get("residency") or "installed"),
                "size_gb": size_gb,
            }
        )
        if norm:
            results.append(norm)
    return results


def _parse_skill_frontmatter(text: str) -> tuple[str | None, str | None]:
    """Parse minimal YAML frontmatter (`name`, `description`) without external deps."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, None
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        return None, None

    name: str | None = None
    description: str | None = None
    i = 1
    while i < end_idx:
        line = lines[i]
        if line.startswith("name:"):
            name = line.split(":", 1)[1].strip().strip("\"'")
        elif line.startswith("description:"):
            val = line.split(":", 1)[1].strip()
            if val in (">-", "|", ">", "|-"):
                parts: list[str] = []
                j = i + 1
                while j < end_idx and (lines[j].startswith("  ") or lines[j].startswith("\t") or not lines[j].strip()):
                    if lines[j].strip():
                        parts.append(lines[j].strip())
                    j += 1
                description = " ".join(parts)
                i = j - 1
            else:
                description = val.strip("\"'")
        i += 1
    return name or None, description or None


def default_skill_roots() -> list[Path]:
    """Return existing local skill directories to scan read-only."""
    home = Path.home()
    candidates = [
        Path.cwd() / "skills",
        Path.cwd() / ".gemini" / "skills",
        home / ".gemini" / "antigravity" / "builtin" / "skills",
    ]
    plugins_dir = home / ".gemini" / "config" / "plugins"
    if plugins_dir.is_dir():
        for plugin_sub in plugins_dir.iterdir():
            skills_sub = plugin_sub / "skills"
            if skills_sub.is_dir():
                candidates.append(skills_sub)
    return [p for p in candidates if p.is_dir()]


def scan_skills(skill_roots: list[Path] | None = None) -> list[dict[str, Any]]:
    """Scan local directories for `*/SKILL.md` and return normalized skill entries."""
    roots = skill_roots if skill_roots is not None else default_skill_roots()
    skills: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fm_name, fm_desc = _parse_skill_frontmatter(content)
            skill_id = entry.name
            if skill_id in seen_ids:
                continue
            seen_ids.add(skill_id)
            skills.append(
                {
                    "id": skill_id,
                    "name": fm_name or skill_id,
                    "description": fm_desc or "",
                    "path": str(skill_md),
                    "source": root.parent.name if root.name == "skills" else root.name,
                }
            )
    return skills


def _default_fetch_json(url: str, timeout: float = 12.0) -> Any:
    req = Request(
        url,
        headers={
            "User-Agent": "sabrina-swarm-console/1.0",
            "Accept": "application/json",
        },
    )
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


_ENV_FALLBACK_VARS: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "x-ai": ("XAI_API_KEY",),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
}


def _is_provider_unlocked_env(provider: str) -> bool:
    norm = (provider or "").strip().lower()
    if not norm:
        return False
    if norm.startswith("openrouter:"):
        sub = norm.split(":", 1)[1].strip()
        return _is_provider_unlocked_env("openrouter") or (
            bool(sub) and _is_provider_unlocked_env(sub)
        )
    if norm == "gemma":
        return _is_provider_unlocked_env("gemini") or True
    if norm == "ollama":
        return True
    return any(bool(os.environ.get(var, "").strip()) for var in _ENV_FALLBACK_VARS.get(norm, ()))


@dataclass
class ModelSkillRegistry:
    """Unified, searchable registry of collective/API/external/local models and separate local skills."""

    ttl_seconds: float = 300.0
    fetch_json: Callable[[str], Any] = _default_fetch_json
    skill_roots: list[Path] | None = None
    enable_remote: bool = True
    vault: ProviderKeyVault | None = None
    custom_models_path: Path | None = None
    runtime_fleet: list[dict[str, Any]] = field(default_factory=list)
    _models: list[dict[str, Any]] = field(default_factory=list, init=False)
    _model_search_blobs: list[str] = field(default_factory=list, init=False)
    _skills: list[dict[str, Any]] = field(default_factory=list, init=False)
    _last_models_sync: float = field(default=0.0, init=False)
    _last_skills_sync: float = field(default=0.0, init=False)
    _source_status: dict[str, str] = field(default_factory=dict, init=False)

    def _load_custom_models(self) -> list[dict[str, Any]]:
        if self.custom_models_path is None:
            return []
        safe_path = guard_safe_path(self.custom_models_path)
        if not safe_path.is_file():
            return []
        try:
            raw = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = raw.get("models", raw) if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["custom"] = True
            norm = normalize_model_record(item)
            if norm:
                out.append(norm)
        return out

    def _save_custom_models(self, models: list[dict[str, Any]]) -> None:
        if self.custom_models_path is None:
            return
        safe_path = guard_safe_path(self.custom_models_path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "models": models}, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(safe_path.parent),
            prefix="custom_models_",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_path, safe_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def set_runtime_fleet(self, fleet_models: list[dict[str, Any]]) -> None:
        """Update live collective & local Ollama residency records and rebuild index."""
        cleaned: list[dict[str, Any]] = []
        for raw in fleet_models or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            prov_raw = str(item.get("provider") or "").lower()
            is_local = "ollama" in prov_raw or "local" in prov_raw
            if not item.get("id"):
                raw_name = str(item.get("name") or "").strip()
                item["id"] = f"ollama/{raw_name}" if (is_local and not raw_name.startswith("ollama/")) else raw_name
            if is_local:
                item.setdefault("tier", "local")
                item["provider"] = "ollama"
            else:
                item.setdefault("tier", "collective")
                item.setdefault("source", "collective")
            norm = normalize_model_record(item)
            if norm:
                cleaned.append(norm)
        if cleaned != self.runtime_fleet:
            self.runtime_fleet = cleaned
            if self._models:
                by_id = {m["id"]: dict(m) for m in self._models}
                for item in self.runtime_fleet:
                    existing = by_id.get(item["id"])
                    if existing:
                        merged = dict(existing)
                        merged["tier"] = item.get("tier") or merged.get("tier")
                        merged["role"] = item.get("role") or merged.get("role")
                        merged["residency"] = item.get("residency") or merged.get("residency")
                        merged["size_gb"] = item.get("size_gb") or merged.get("size_gb")
                        if item.get("custom"):
                            merged["custom"] = True
                        by_id[item["id"]] = merged
                    else:
                        by_id[item["id"]] = dict(item)
                self._source_status["runtime_fleet"] = f"ok ({len(self.runtime_fleet)})"
                def _tier_rank(m: dict[str, Any]) -> int:
                    if m.get("custom"):
                        return 0
                    t = m.get("tier")
                    if t == "collective":
                        return 1
                    if t == "local":
                        return 2
                    if t == "api":
                        return 3
                    return 4
                models = sorted(
                    by_id.values(),
                    key=lambda m: (m["deprecated"], _tier_rank(m), m["provider"], m["id"]),
                )
                self._models = models
                self._model_search_blobs = [
                    f"{m['id']} {m['name']} {m['provider']} {m.get('tier', '')} {m.get('role') or ''} {' '.join(m['capabilities'])}".lower()
                    for m in models
                ]

    def add_model(
        self,
        *,
        model_id: str,
        name: str = "",
        provider: str = "",
        tier: str = "external",
        role: str = "",
        residency: str = "",
        size_gb: str = "",
        endpoint: str = "",
        context_window: int | None = None,
        capabilities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add or update a custom model in the unified fleet and persist it atomically."""
        cleaned_id = str(model_id or "").strip()
        if not cleaned_id:
            raise ValueError("Model ID is required.")
        cleaned_tier = str(tier or "external").strip().lower()
        if cleaned_tier not in VALID_MODEL_TIERS:
            cleaned_tier = "external"
        inferred_prov = str(provider or "").strip().lower()
        if not inferred_prov:
            if "/" in cleaned_id:
                inferred_prov = cleaned_id.split("/", 1)[0].lower()
            elif cleaned_tier == "local" or ":" in cleaned_id:
                inferred_prov = "ollama"
            elif cleaned_tier == "collective":
                inferred_prov = "collective"
            else:
                inferred_prov = "custom"
        if cleaned_tier == "local" and inferred_prov == "ollama" and not cleaned_id.startswith("ollama/"):
            cleaned_id = f"ollama/{cleaned_id}"

        default_residency = residency.strip() or (
            "collective member"
            if cleaned_tier == "collective"
            else ("installed" if cleaned_tier == "local" else "registered")
        )
        record = normalize_model_record(
            {
                "id": cleaned_id,
                "name": str(name or "").strip() or cleaned_id,
                "provider": inferred_prov,
                "tier": cleaned_tier,
                "role": str(role or "").strip() or None,
                "residency": default_residency,
                "size_gb": str(size_gb or "").strip() or None,
                "endpoint": str(endpoint or "").strip() or None,
                "context_window": context_window,
                "capabilities": capabilities,
                "source": "custom",
                "custom": True,
            }
        )
        if record is None:
            raise ValueError("Invalid model definition.")

        existing = [m for m in self._load_custom_models() if m["id"] != record["id"]]
        existing.insert(0, record)
        if self.custom_models_path is not None:
            self._save_custom_models(existing)
        else:
            # Keep in runtime_fleet if no persistence path is configured
            self.runtime_fleet = [m for m in self.runtime_fleet if m["id"] != record["id"]]
            self.runtime_fleet.insert(0, record)

        self.refresh_models(force=True)
        return record

    def remove_model(self, model_id: str) -> bool:
        """Remove a custom user-added model by ID."""
        target = str(model_id or "").strip()
        if not target:
            return False
        removed = False
        if self.custom_models_path is not None:
            existing = self._load_custom_models()
            kept = [m for m in existing if m["id"] != target]
            if len(kept) != len(existing):
                self._save_custom_models(kept)
                removed = True
        before_rt = len(self.runtime_fleet)
        self.runtime_fleet = [
            m for m in self.runtime_fleet if not (m.get("custom") and m.get("id") == target)
        ]
        if len(self.runtime_fleet) != before_rt:
            removed = True
        if removed:
            self.refresh_models(force=True)
        return removed

    def refresh_models(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._models and (now - self._last_models_sync) < self.ttl_seconds:
            return

        by_id: dict[str, dict[str, Any]] = {}
        status: dict[str, str] = {}

        for raw in BUILTIN_GEMINI_MODELS:
            norm = normalize_model_record(raw)
            if norm:
                by_id[norm["id"]] = norm
        status["builtin"] = f"ok ({len(by_id)})"

        if self.enable_remote:
            remote_sources: list[tuple[str, str, Callable[[Any], list[dict[str, Any]]]]] = [
                ("openrouter", os.environ.get("SWARM_OPENROUTER_MODELS_URL", OPENROUTER_MODELS_URL), normalize_openrouter_catalog),
                ("models_dev", os.environ.get("SWARM_MODELS_DEV_URL", MODELS_DEV_URL), normalize_models_dev_catalog),
            ]
            for name, url, normalizer in remote_sources:
                if not url:
                    continue
                try:
                    payload = self.fetch_json(url)
                    items = normalizer(payload)
                    for item in items:
                        by_id.setdefault(item["id"], item)
                    status[name] = f"ok ({len(items)})"
                except (URLError, OSError, ValueError, TypeError) as exc:
                    status[name] = f"unavailable ({exc})"

            ollama_candidates: list[str] = []
            explicit_ollama = os.environ.get("SWARM_OLLAMA_TAGS_URL")
            if explicit_ollama is not None:
                if explicit_ollama.strip():
                    ollama_candidates.append(explicit_ollama.strip())
            else:
                sabrina_ollama = os.environ.get("SABRINA_OLLAMA_BASE_URL", "").strip().rstrip("/")
                if sabrina_ollama:
                    ollama_candidates.append(f"{sabrina_ollama}/api/tags")
                ollama_candidates.append(DEFAULT_OLLAMA_URL)
                if "ego.local" not in (sabrina_ollama or ""):
                    ollama_candidates.append("http://ego.local:11434/api/tags")

            if ollama_candidates:
                ollama_ok = False
                for ollama_url in dict.fromkeys(ollama_candidates):
                    try:
                        payload = self.fetch_json(ollama_url)
                        items = normalize_ollama_catalog(payload)
                        for item in items:
                            if item["id"] in by_id:
                                merged = dict(by_id[item["id"]])
                                merged["residency"] = item.get("residency") or merged.get("residency")
                                merged["size_gb"] = item.get("size_gb") or merged.get("size_gb")
                                merged["tier"] = "local"
                                by_id[item["id"]] = merged
                            else:
                                by_id[item["id"]] = item
                        status["ollama"] = f"ok ({len(items)})"
                        ollama_ok = True
                        break
                    except (URLError, OSError, ValueError, TypeError):
                        continue
                if not ollama_ok:
                    status["ollama"] = "offline (no local Ollama instance responding)"

        if self.vault is not None:
            probed = self.vault.get_probed_models()
            for item in probed:
                by_id.setdefault(item["id"], item)
            status["vault_probed"] = f"ok ({len(probed)})"

        if self.runtime_fleet:
            for item in self.runtime_fleet:
                existing = by_id.get(item["id"])
                if existing:
                    merged = dict(existing)
                    merged["tier"] = item.get("tier") or merged.get("tier")
                    merged["role"] = item.get("role") or merged.get("role")
                    merged["residency"] = item.get("residency") or merged.get("residency")
                    merged["size_gb"] = item.get("size_gb") or merged.get("size_gb")
                    if item.get("custom"):
                        merged["custom"] = True
                    by_id[item["id"]] = merged
                else:
                    by_id[item["id"]] = dict(item)
            status["runtime_fleet"] = f"ok ({len(self.runtime_fleet)})"

        custom_list = self._load_custom_models()
        if custom_list:
            for item in custom_list:
                by_id[item["id"]] = dict(item)
            status["custom"] = f"ok ({len(custom_list)})"

        def _tier_rank(m: dict[str, Any]) -> int:
            if m.get("custom"):
                return 0
            t = m.get("tier")
            if t == "collective":
                return 1
            if t == "local":
                return 2
            if t == "api":
                return 3
            return 4

        models = sorted(
            by_id.values(),
            key=lambda m: (m["deprecated"], _tier_rank(m), m["provider"], m["id"]),
        )
        self._models = models
        self._model_search_blobs = [
            f"{m['id']} {m['name']} {m['provider']} {m.get('tier', '')} {m.get('role') or ''} {' '.join(m['capabilities'])}".lower()
            for m in models
        ]
        self._source_status = status
        self._last_models_sync = now

    def refresh_skills(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._skills and (now - self._last_skills_sync) < self.ttl_seconds:
            return
        self._skills = scan_skills(self.skill_roots)
        self._last_skills_sync = now

    def search_models(
        self,
        *,
        query: str = "",
        provider: str = "",
        capability: str = "",
        tier: str = "",
        include_deprecated: bool = False,
        unlocked_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any]:
        self.refresh_models(force=refresh)
        q_terms = [t for t in query.strip().lower().split() if t]
        prov_filter = provider.strip().lower()
        cap_filter = capability.strip().lower()
        tier_filter = tier.strip().lower()

        providers_set: set[str] = set()
        caps_set: set[str] = set()
        tier_counts: dict[str, int] = {
            "all": 0,
            "collective": 0,
            "api": 0,
            "external": 0,
            "local": 0,
        }
        matched: list[dict[str, Any]] = []
        unlocked_cache: dict[str, bool] = {}

        def _is_unlocked(m: dict[str, Any]) -> bool:
            if m.get("custom") or m.get("tier") in ("collective", "local"):
                return True
            prov = m["provider"]
            if prov not in unlocked_cache:
                if self.vault is not None:
                    unlocked_cache[prov] = self.vault.is_provider_unlocked(prov)
                else:
                    unlocked_cache[prov] = _is_provider_unlocked_env(prov)
            return unlocked_cache[prov]

        unlocked_total = 0
        for model, blob in zip(self._models, self._model_search_blobs):
            providers_set.add(model["provider"])
            caps_set.update(model["capabilities"])
            if not include_deprecated and model["deprecated"]:
                continue
            m_tier = model.get("tier") or "external"
            tier_counts["all"] += 1
            if m_tier in tier_counts:
                tier_counts[m_tier] += 1

            if tier_filter and m_tier != tier_filter:
                continue
            if prov_filter and model["provider"] != prov_filter:
                continue
            if cap_filter and cap_filter not in model["capabilities"]:
                continue
            if q_terms and not all(term in blob for term in q_terms):
                continue
            is_unlocked = _is_unlocked(model)
            if is_unlocked:
                unlocked_total += 1
            if unlocked_only and not is_unlocked:
                continue
            annotated = dict(model)
            annotated["unlocked"] = is_unlocked
            matched.append(annotated)

        if self.vault is not None:
            unlocked_providers = self.vault.list_providers()["unlocked_providers"]
        else:
            unlocked_providers = sorted(
                p for p in ("gemini", "openai", "anthropic", "openrouter", "xai", "deepseek", "groq", "mistral", "together", "ollama")
                if _is_provider_unlocked_env(p)
            )

        safe_offset = max(0, offset)
        safe_limit = max(1, min(200, limit))
        page = matched[safe_offset : safe_offset + safe_limit]

        return {
            "items": page,
            "total": len(matched),
            "catalog_total": len(self._models),
            "unlocked_total": unlocked_total,
            "unlocked_providers": unlocked_providers,
            "providers": sorted(providers_set),
            "capabilities": sorted(caps_set),
            "tier_counts": tier_counts,
            "sources": dict(self._source_status),
        }

    def search_skills(
        self,
        *,
        query: str = "",
        source: str = "",
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any]:
        self.refresh_skills(force=refresh)
        q_terms = [t for t in query.strip().lower().split() if t]
        src_filter = source.strip().lower()
        sources_set = {str(s.get("source") or "local") for s in self._skills}
        matched: list[dict[str, Any]] = []
        for skill in self._skills:
            s_src = str(skill.get("source") or "local")
            if src_filter and s_src.lower() != src_filter:
                continue
            if q_terms:
                blob = f"{skill['id']} {skill['name']} {skill['description']} {s_src}".lower()
                if not all(term in blob for term in q_terms):
                    continue
            matched.append(skill)

        safe_offset = max(0, offset)
        safe_limit = max(1, min(200, limit))
        return {
            "items": matched[safe_offset : safe_offset + safe_limit],
            "total": len(matched),
            "catalog_total": len(self._skills),
            "sources": sorted(sources_set),
        }

    def get_skill_detail(self, skill_id: str) -> dict[str, Any] | None:
        """Return metadata and `SKILL.md` body for a single skill by `id`."""
        self.refresh_skills(force=False)
        target = str(skill_id or "").strip()
        if not target:
            return None
        for skill in self._skills:
            if skill["id"] == target:
                content = ""
                path = Path(skill["path"])
                if path.is_file():
                    try:
                        content = path.read_text(encoding="utf-8")[:32768]
                    except (OSError, UnicodeDecodeError):
                        content = ""
                return {**skill, "content": content}
        return None
