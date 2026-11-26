"""Optional Gemini API Swarm Copilot for Sabrina's Swarm Console.

Uses the Gemini Interactions API (`google-genai`) with `gemini-4-hydrogen`
(or another Gemini 4 model) to analyze live `.swarm/` state, diagnose blockers
and lock conflicts, and draft `@mention` board messages.

Zero-dependency safe: `google-genai` is imported lazily so `swarm_console`
continues to run with Python stdlib only when the SDK or `GEMINI_API_KEY`
is not configured.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
SUPPORTED_GEMINI_MODELS = (
    "gemini-4-hydrogen",
    "gemini-4-argon",
    "gemini-4-neon",
    "gemini-4-carbon",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
)
SUPPORTED_THINKING_LEVELS = ("minimal", "low", "medium", "high")
DEFAULT_THINKING_LEVEL = "medium"

COPILOT_SYSTEM_INSTRUCTION = (
    "You are Sabrina's Swarm Research Console Copilot. "
    "Analyze the provided `.swarm/` state (plan, safety gate, worker roster, "
    "disjoint lock matrix, and recent board messages). "
    "Be concise, factual, and grounded strictly in the provided state. "
    "Never invent worker names, file locks, or board events that are not present. "
    "When suggesting a board message, format it with `@worker` mentions and "
    "optional `[PASS]`, `[BLOCK]`, or `[done]` tags matching the console conventions."
)

COPILOT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "health": {
            "type": "string",
            "enum": ["healthy", "attention_needed", "blocked"],
        },
        "summary": {"type": "string"},
        "blockers": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommended_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
        "suggested_board_message": {
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["agent", "text"],
        },
    },
    "required": [
        "health",
        "summary",
        "blockers",
        "recommended_actions",
        "suggested_board_message",
    ],
}


class GeminiCopilotError(RuntimeError):
    """Raised when the Gemini Copilot cannot run or returns an invalid response."""

    def __init__(self, message: str, *, code: str = "gemini_error", status: int = 500) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _load_genai_module() -> Any | None:
    try:
        from google import genai  # type: ignore[import-not-found]
    except ImportError:
        return None
    return genai


def get_copilot_status() -> dict[str, Any]:
    """Report whether the `google-genai` SDK and `GEMINI_API_KEY` are available."""
    sdk_installed = _load_genai_module() is not None
    api_key_set = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    available = sdk_installed and api_key_set

    if available:
        reason = "Ready"
    elif not sdk_installed:
        reason = "Install `google-genai` (>=3.5.0) to enable Gemini Copilot."
    else:
        reason = "Set `GEMINI_API_KEY` in the server environment to enable Gemini Copilot."

    return {
        "available": available,
        "sdk_installed": sdk_installed,
        "api_key_set": api_key_set,
        "default_model": DEFAULT_GEMINI_MODEL,
        "supported_models": list(SUPPORTED_GEMINI_MODELS),
        "reason": reason,
    }


def build_swarm_prompt(state: dict[str, Any], question: str | None = None) -> str:
    """Build a compact, structured prompt from the current `.swarm/` state."""
    plan = state.get("plan") or {}
    safety = state.get("safety") or {}
    roster = (state.get("roster") or {}).get("workers") or {}
    lock_matrix = state.get("lock_matrix") or {}
    board = state.get("board") or []

    snapshot = {
        "plan": {
            "title": plan.get("title"),
            "goal": plan.get("goal"),
            "phase": plan.get("phase"),
            "threads": plan.get("threads", []),
        },
        "safety": {
            "execution_authorized": bool(safety.get("execution_authorized", False)),
        },
        "workers": roster,
        "lock_conflicts": lock_matrix.get("conflicts", []),
        "recent_board": board[-20:],
    }

    prompt_parts = [
        "Current `.swarm/` state snapshot (JSON):",
        json.dumps(snapshot, indent=2, ensure_ascii=False),
    ]
    clean_question = (question or "").strip()
    if clean_question:
        prompt_parts.append(f"\nCoordinator question:\n{clean_question}")
    else:
        prompt_parts.append(
            "\nProvide an executive triage summary, highlight any blockers or lock risks, "
            "recommend concrete next actions, and draft the most useful next board message."
        )
    return "\n".join(prompt_parts)


def analyze_swarm_state(
    state: dict[str, Any],
    *,
    question: str | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
    thinking_level: str | None = DEFAULT_THINKING_LEVEL,
    previous_interaction_id: str | None = None,
    client: Any | None = None,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Analyze `.swarm/` state with Gemini Interactions API and return structured triage."""
    selected_model = (model or DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    if selected_model not in SUPPORTED_GEMINI_MODELS:
        raise GeminiCopilotError(
            f"Unsupported Gemini model '{selected_model}'. Choose one of: {', '.join(SUPPORTED_GEMINI_MODELS)}.",
            code="invalid_model",
            status=400,
        )
    selected_effort = (thinking_level or DEFAULT_THINKING_LEVEL).strip().lower()
    if selected_effort not in SUPPORTED_THINKING_LEVELS:
        raise GeminiCopilotError(
            f"Unsupported thinking_level '{selected_effort}'. Choose one of: {', '.join(SUPPORTED_THINKING_LEVELS)}.",
            code="invalid_thinking_level",
            status=400,
        )

    if client is None:
        if client_factory is not None:
            client = client_factory()
        else:
            status = get_copilot_status()
            if not status["sdk_installed"]:
                raise GeminiCopilotError(
                    status["reason"],
                    code="sdk_missing",
                    status=503,
                )
            if not status["api_key_set"]:
                raise GeminiCopilotError(
                    status["reason"],
                    code="api_key_missing",
                    status=503,
                )
            genai = _load_genai_module()
            assert genai is not None
            client = genai.Client()

    prompt = build_swarm_prompt(state, question=question)
    kwargs: dict[str, Any] = {
        "model": selected_model,
        "input": prompt,
        "system_instruction": COPILOT_SYSTEM_INSTRUCTION,
        "generation_config": {"thinking_level": selected_effort},
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": COPILOT_RESPONSE_SCHEMA,
        },
    }
    if previous_interaction_id:
        kwargs["previous_interaction_id"] = previous_interaction_id

    try:
        interaction = client.interactions.create(**kwargs)
    except Exception as exc:
        raise GeminiCopilotError(
            f"Gemini API request failed: {exc}",
            code="gemini_api_error",
            status=502,
        ) from exc

    raw_text = (getattr(interaction, "output_text", None) or "").strip()
    if not raw_text:
        raise GeminiCopilotError(
            "Gemini API returned an empty response.",
            code="empty_response",
            status=502,
        )

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = {
            "health": "attention_needed",
            "summary": raw_text,
            "blockers": [],
            "recommended_actions": [],
            "suggested_board_message": {"agent": "coordinator", "text": ""},
        }

    return {
        "model": selected_model,
        "interaction_id": getattr(interaction, "id", None),
        "analysis": {
            "health": parsed.get("health", "attention_needed"),
            "summary": parsed.get("summary", ""),
            "blockers": list(parsed.get("blockers") or []),
            "recommended_actions": list(parsed.get("recommended_actions") or []),
            "suggested_board_message": parsed.get("suggested_board_message")
            or {"agent": "coordinator", "text": ""},
        },
    }
