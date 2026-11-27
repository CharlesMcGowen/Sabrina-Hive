"""Role-to-model binding, provider inference, and ordered fallback execution."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .models import AgentRole

DEFAULT_ROLE_BINDINGS_PATH = (
    Path(__file__).resolve().parent.parent
    / "sabrina_console_desktop"
    / "data"
    / "control"
    / "role_bindings.json"
)

CANONICAL_ROLES: tuple[str, ...] = (
    "coordinator",
    AgentRole.SWARM_COMMAND.value,
    AgentRole.PLANNER_ARCHITECT.value,
    AgentRole.RESEARCH_SCOUT.value,
    AgentRole.EXECUTION_SPECIALIST.value,
    AgentRole.CRITIC_VERIFIER.value,
    "fast_worker",
)

ROLE_ALIASES: dict[str, str] = {
    "coordinator": "coordinator",
    "sabrina": "coordinator",
    "swarm_command": AgentRole.SWARM_COMMAND.value,
    "command": AgentRole.SWARM_COMMAND.value,
    "planner_architect": AgentRole.PLANNER_ARCHITECT.value,
    "planner": AgentRole.PLANNER_ARCHITECT.value,
    "architect": AgentRole.PLANNER_ARCHITECT.value,
    "research_scout": AgentRole.RESEARCH_SCOUT.value,
    "scout": AgentRole.RESEARCH_SCOUT.value,
    "execution_specialist": AgentRole.EXECUTION_SPECIALIST.value,
    "executor": AgentRole.EXECUTION_SPECIALIST.value,
    "specialist": AgentRole.EXECUTION_SPECIALIST.value,
    "critic_verifier": AgentRole.CRITIC_VERIFIER.value,
    "verifier": AgentRole.CRITIC_VERIFIER.value,
    "critic": AgentRole.CRITIC_VERIFIER.value,
    "qa_verifier": AgentRole.CRITIC_VERIFIER.value,
    "qa_engineer": AgentRole.CRITIC_VERIFIER.value,
    "product_qa": AgentRole.CRITIC_VERIFIER.value,
    "fast_worker": "fast_worker",
    "worker": "fast_worker",
}

DEFAULT_ROLE_BINDINGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "coordinator": (
        "gemini-4-neon",
        ("gemini-neo", "gemini-4-carbon", "gemini-3.5-flash-lite", "openai/gpt-5", "anthropic/claude-sonnet-4-5"),
    ),
    AgentRole.SWARM_COMMAND.value: (
        "gemini-4-neon",
        ("gemini-neo", "gemini-4-carbon", "gemini-4-hydrogen", "gemini-3.5-flash-lite"),
    ),
    AgentRole.PLANNER_ARCHITECT.value: (
        "gemini-4-neon",
        ("gemini-neo", "gemini-4-carbon", "gemini-4-argon", "openai/gpt-5", "gemini-3.5-flash-lite"),
    ),
    AgentRole.RESEARCH_SCOUT.value: (
        "gemini-4-hydrogen",
        ("gemini-3.5-flash-lite", "openai/gpt-5-mini"),
    ),
    AgentRole.EXECUTION_SPECIALIST.value: (
        "gemini-4-hydrogen",
        ("gemini-4-argon", "openai/gpt-5", "gemini-3.5-flash-lite"),
    ),
    AgentRole.CRITIC_VERIFIER.value: (
        "gemini-4-carbon",
        ("gemini-4-neon", "anthropic/claude-sonnet-4-5", "openai/gpt-5", "gemini-3.5-flash-lite"),
    ),
    "fast_worker": (
        "gemini-4-hydrogen",
        ("gemini-3.5-flash-lite", "openai/gpt-5-mini"),
    ),
}


class RoleBindingResolutionError(RuntimeError):
    """Raised when no healthy or unlocked model can be resolved for a role."""


def infer_model_provider(model_id: str) -> str:
    """Infer the provider key for a model identifier."""
    raw = str(model_id or "").strip().lower()
    if not raw:
        return "unknown"

    if raw.startswith("portal:"):
        return "portal"

    prefix_map = (
        ("openrouter/", "openrouter"),
        ("google_vertex/", "google_vertex"),
        ("vertex/", "google_vertex"),
        ("aws_bedrock/", "aws_bedrock"),
        ("bedrock/", "aws_bedrock"),
        ("runpod/", "runpod"),
        ("vastai/", "vastai"),
        ("vast/", "vastai"),
        ("lambda_labs/", "lambda_labs"),
        ("lambda/", "lambda_labs"),
        ("modal/", "modal"),
        ("coreweave/", "coreweave"),
        ("custom_gpu/", "custom_gpu"),
        ("huggingface/", "huggingface"),
        ("cerebras/", "cerebras"),
        ("sambanova/", "sambanova"),
        ("fireworks/", "fireworks"),
        ("nvidia/", "nvidia"),
        ("github/", "github"),
        ("cohere/", "cohere"),
        ("perplexity/", "perplexity"),
        ("pollinations/", "pollinations"),
        ("lmstudio/", "lmstudio"),
        ("ollama/", "ollama"),
        ("openai/", "openai"),
        ("anthropic/", "anthropic"),
        ("xai/", "xai"),
        ("deepseek/", "deepseek"),
        ("groq/", "groq"),
        ("mistral/", "mistral"),
        ("together/", "together"),
    )
    for prefix, provider in prefix_map:
        if raw.startswith(prefix):
            return provider

    if raw.startswith(("gemini-", "models/gemini-", "models/genie-", "lyria-", "gemma-")):
        return "gemini"
    if raw.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if raw.startswith("claude-"):
        return "anthropic"
    if raw.startswith("grok-"):
        return "xai"
    if raw.startswith("deepseek-"):
        return "deepseek"
    if ":" in raw and "/" not in raw:
        return "ollama"
    return "unknown"


def normalize_role_name(role: str | AgentRole) -> str:
    """Normalize an AgentRole enum or console alias into a canonical role name."""
    raw = role.value if isinstance(role, AgentRole) else str(role or "").strip().lower().replace("-", "_")
    canonical = ROLE_ALIASES.get(raw)
    if canonical is None:
        raise ValueError(f"Unsupported role for model binding: {role!r}")
    return canonical


@dataclass
class RoleModelBinding:
    """Primary and ordered fallback model binding for a single swarm role."""

    role: str
    primary_model: str
    fallback_models: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.role = normalize_role_name(self.role)
        primary = str(self.primary_model or "").strip()
        if not primary:
            raise ValueError(f"primary_model must be non-empty for role {self.role!r}")
        self.primary_model = primary
        self.fallback_models = _dedupe_fallbacks(primary, self.fallback_models)

    def candidate_chain(self) -> list[str]:
        return [self.primary_model, *self.fallback_models]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "primary_model": self.primary_model,
            "fallback_models": list(self.fallback_models),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoleModelBinding:
        return cls(
            role=str(data["role"]),
            primary_model=str(data["primary_model"]),
            fallback_models=list(data.get("fallback_models") or []),
        )


def _dedupe_fallbacks(primary_model: str, fallbacks: Iterable[str] | None) -> list[str]:
    seen = {primary_model}
    cleaned: list[str] = []
    for item in fallbacks or ():
        candidate = str(item or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        cleaned.append(candidate)
    return cleaned


class RoleBindingConfig:
    """Manages role-to-model bindings, atomic JSON persistence, and fallback resolution."""

    def __init__(self, config_path: Path | str | None = None) -> None:
        self.config_path = Path(config_path) if config_path is not None else None
        self._bindings: dict[str, RoleModelBinding] = self._build_defaults()
        if self.config_path is not None and self.config_path.is_file():
            self._load()

    @staticmethod
    def _build_defaults() -> dict[str, RoleModelBinding]:
        return {
            role: RoleModelBinding(role=role, primary_model=primary, fallback_models=list(fallbacks))
            for role, (primary, fallbacks) in DEFAULT_ROLE_BINDINGS.items()
        }

    def _load(self) -> None:
        if self.config_path is None or not self.config_path.is_file():
            return
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        entries = raw.get("bindings", raw) if isinstance(raw, dict) else {}
        if not isinstance(entries, dict):
            return
        for role_key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            try:
                canonical = normalize_role_name(role_key)
                primary = str(entry.get("primary_model") or "").strip()
                if not primary:
                    continue
                fallbacks = list(entry.get("fallback_models") or [])
                self._bindings[canonical] = RoleModelBinding(
                    role=canonical,
                    primary_model=primary,
                    fallback_models=fallbacks,
                )
            except ValueError:
                continue

    def _save(self) -> None:
        if self.config_path is None:
            return
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "bindings": {
                role: self._bindings[role].to_dict()
                for role in CANONICAL_ROLES
                if role in self._bindings
            },
        }
        serialized = json.dumps(payload, indent=2, sort_keys=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix="role_bindings_",
            suffix=".tmp",
            dir=str(self.config_path.parent),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialized)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.config_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def get_binding(self, role: str | AgentRole) -> RoleModelBinding:
        canonical = normalize_role_name(role)
        binding = self._bindings[canonical]
        return RoleModelBinding(
            role=binding.role,
            primary_model=binding.primary_model,
            fallback_models=list(binding.fallback_models),
        )

    def set_binding(
        self,
        role: str | AgentRole,
        primary_model: str,
        fallback_models: list[str] | tuple[str, ...] | None = None,
    ) -> RoleModelBinding:
        canonical = normalize_role_name(role)
        primary = str(primary_model or "").strip()
        if not primary:
            raise ValueError("primary_model must be a non-empty string.")
        if fallback_models is None:
            existing = self._bindings[canonical]
            fallbacks = [m for m in existing.fallback_models if m != primary]
        else:
            fallbacks = _dedupe_fallbacks(primary, fallback_models)
        updated = RoleModelBinding(
            role=canonical,
            primary_model=primary,
            fallback_models=fallbacks,
        )
        self._bindings[canonical] = updated
        self._save()
        return self.get_binding(canonical)

    def reset_binding(self, role: str | AgentRole) -> RoleModelBinding:
        canonical = normalize_role_name(role)
        primary, fallbacks = DEFAULT_ROLE_BINDINGS[canonical]
        self._bindings[canonical] = RoleModelBinding(
            role=canonical,
            primary_model=primary,
            fallback_models=list(fallbacks),
        )
        self._save()
        return self.get_binding(canonical)

    def list_bindings(self, unlocked_providers: set[str] | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for role in CANONICAL_ROLES:
            binding = self._bindings[role]
            resolved = self.resolve_model(role, unlocked_providers=unlocked_providers)
            results.append(
                {
                    "role": binding.role,
                    "primary_model": binding.primary_model,
                    "fallback_models": list(binding.fallback_models),
                    "effective_model": resolved["selected_model"],
                    "provider": resolved["provider"],
                    "used_fallback": resolved["used_fallback"],
                    "available": resolved["unlocked"],
                }
            )
        return results

    def resolve_model(
        self,
        role: str | AgentRole,
        *,
        unlocked_providers: set[str] | None = None,
        unhealthy_models: set[str] | None = None,
        strict_unlock: bool = False,
    ) -> dict[str, Any]:
        binding = self.get_binding(role)
        chain = binding.candidate_chain()
        unhealthy = {str(m).strip() for m in (unhealthy_models or set()) if str(m).strip()}
        healthy_chain = [m for m in chain if m not in unhealthy]

        if not healthy_chain:
            raise RoleBindingResolutionError(
                f"No healthy models left for role {binding.role!r}; attempted={chain}"
            )

        if unlocked_providers is None:
            selected = healthy_chain[0]
            used_fallback = selected != binding.primary_model
            return {
                "role": binding.role,
                "selected_model": selected,
                "provider": infer_model_provider(selected),
                "primary_model": binding.primary_model,
                "fallback_models": list(binding.fallback_models),
                "used_fallback": used_fallback,
                "unlocked": True,
                "attempted": [m for m in chain[: chain.index(selected) + 1]],
                "reason": "primary_selected" if not used_fallback else "primary_unhealthy_fallback_selected",
            }

        normalized_unlocked = {str(p).strip().lower() for p in unlocked_providers if str(p).strip()}
        attempted: list[str] = []
        for candidate in healthy_chain:
            attempted.append(candidate)
            provider = infer_model_provider(candidate)
            if provider in normalized_unlocked:
                used_fallback = candidate != binding.primary_model
                return {
                    "role": binding.role,
                    "selected_model": candidate,
                    "provider": provider,
                    "primary_model": binding.primary_model,
                    "fallback_models": list(binding.fallback_models),
                    "used_fallback": used_fallback,
                    "unlocked": True,
                    "attempted": attempted,
                    "reason": "primary_selected" if not used_fallback else "unlocked_fallback_selected",
                }

        if strict_unlock:
            raise RoleBindingResolutionError(
                f"No unlocked healthy model for role {binding.role!r}; "
                f"attempted={attempted}, unlocked_providers={sorted(normalized_unlocked)}"
            )

        fallback_first = healthy_chain[0]
        return {
            "role": binding.role,
            "selected_model": fallback_first,
            "provider": infer_model_provider(fallback_first),
            "primary_model": binding.primary_model,
            "fallback_models": list(binding.fallback_models),
            "used_fallback": fallback_first != binding.primary_model,
            "unlocked": False,
            "attempted": attempted,
            "reason": "no_unlocked_provider_using_first_healthy",
        }

    def execute_with_fallback(
        self,
        role: str | AgentRole,
        fn: Callable[[str], Any],
        *,
        unlocked_providers: set[str] | None = None,
        unhealthy_models: set[str] | None = None,
        strict_unlock: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        binding = self.get_binding(role)
        failed = {str(m).strip() for m in (unhealthy_models or set()) if str(m).strip()}
        attempts: list[dict[str, Any]] = []
        last_exc: Exception | None = None

        while True:
            try:
                resolution = self.resolve_model(
                    binding.role,
                    unlocked_providers=unlocked_providers,
                    unhealthy_models=failed,
                    strict_unlock=strict_unlock,
                )
            except RoleBindingResolutionError as exc:
                if last_exc is not None:
                    raise RoleBindingResolutionError(
                        f"All fallback models failed for role {binding.role!r}: {attempts}; last_error={last_exc}"
                    ) from last_exc
                raise exc

            candidate = resolution["selected_model"]
            provider = resolution["provider"]
            try:
                result = fn(candidate)
                attempts.append({"model": candidate, "provider": provider, "ok": True})
                telemetry = {
                    "role": binding.role,
                    "selected_model": candidate,
                    "provider": provider,
                    "primary_model": binding.primary_model,
                    "used_fallback": candidate != binding.primary_model or len(attempts) > 1,
                    "unlocked": resolution["unlocked"],
                    "attempts": attempts,
                }
                return result, telemetry
            except (RuntimeError, ConnectionError, TimeoutError, OSError) as exc:
                last_exc = exc
                attempts.append(
                    {
                        "model": candidate,
                        "provider": provider,
                        "ok": False,
                        "error": str(exc),
                    }
                )
                failed.add(candidate)
