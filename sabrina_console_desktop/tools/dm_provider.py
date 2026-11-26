"""Structured Dungeon Master providers with auditable, bounded requests.

The provider is advisory only.  Campaign code owns tasks, graders, budgets,
weight application, and promotion.  OpenAI use is explicit and never silently
falls back to a different provider after a request has started.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import urllib.request

from dm_weight_lab import dump
from secret_store import get_secret


DEFAULT_LOCAL_MODEL = "huihui_ai/qwen3-abliterated:14b"
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"


def _bounded_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_-]{1,48}", value):
        raise ValueError("Request name must be a lowercase identifier")
    return value


def _json_object(text: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("DM response must be one JSON object")
    return value


class OllamaDMProvider:
    provider = "ollama"

    def __init__(self, root: Path, model: str = DEFAULT_LOCAL_MODEL,
                 endpoint: str = "http://127.0.0.1:11434/api/chat"):
        self.root = Path(root)
        self.model = model
        self.endpoint = endpoint
        self.calls = 0

    def ask(self, name: str, system: str, payload: dict, schema: dict) -> dict:
        name = _bounded_name(name)
        if self.calls >= 2:
            raise RuntimeError("DM campaign call budget exhausted")
        self.calls += 1
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, sort_keys=True)},
        ]
        for attempt in range(2):
            request_body = {
                "model": self.model,
                "messages": messages,
                "format": schema,
                "stream": False,
                "think": False,
                "keep_alive": 0,
                "options": {
                    "temperature": 0,
                    "num_ctx": 8192,
                    "num_predict": 1200,
                    "seed": 1701,
                },
            }
            dump(self.root / f"{name}-request-{attempt}.json", {
                "provider": self.provider,
                "request": request_body,
            })
            started = time.monotonic()
            request = urllib.request.Request(
                self.endpoint,
                data=json.dumps(request_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=240) as response:
                raw = json.load(response)
            dump(self.root / f"{name}-response-{attempt}.json", {
                "provider": self.provider,
                "model": self.model,
                "elapsed_seconds": time.monotonic() - started,
                "response": raw,
            })
            try:
                return _json_object(raw["message"]["content"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                messages.append({
                    "role": "user",
                    "content": f"Invalid structured response: {error}. Return only the requested JSON object.",
                })
        raise RuntimeError("Local DM failed to return valid JSON twice")


class OpenAIDMProvider:
    provider = "openai"

    def __init__(self, root: Path, model: str = DEFAULT_OPENAI_MODEL,
                 reasoning_effort: str = "low", client_factory=None):
        if reasoning_effort not in {"low", "medium", "high"}:
            raise ValueError("reasoning_effort must be low, medium, or high")
        self.root = Path(root)
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.client_factory = client_factory
        self.calls = 0

    def ask(self, name: str, system: str, payload: dict, schema: dict) -> dict:
        name = _bounded_name(name)
        if self.calls >= 2:
            raise RuntimeError("DM campaign call budget exhausted")
        self.calls += 1
        encoded_payload = json.dumps(payload, sort_keys=True)
        if len(system) + len(encoded_payload) > 40_000:
            raise ValueError("DM input exceeds the 40,000-character campaign ceiling")
        key = get_secret("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "OpenAI DM requested, but OPENAI_API_KEY is not in the Sabrina credential store or environment"
            )
        if self.client_factory is None:
            from openai import OpenAI
            client = OpenAI(api_key=key, timeout=240)
        else:
            client = self.client_factory(key)

        request_record = {
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": encoded_payload},
            ],
            "schema": schema,
            "max_output_tokens": 1800,
        }
        dump(self.root / f"{name}-request-0.json", request_record)
        started = time.monotonic()
        response = client.responses.create(
            model=self.model,
            input=request_record["input"],
            reasoning={"effort": self.reasoning_effort},
            max_output_tokens=request_record["max_output_tokens"],
            text={"format": {
                "type": "json_schema",
                "name": f"sabrina_{name}",
                "schema": schema,
                "strict": True,
            }},
            store=False,
        )
        output = getattr(response, "output_text", "")
        usage = getattr(response, "usage", None)
        response_record = {
            "provider": self.provider,
            "model": self.model,
            "elapsed_seconds": time.monotonic() - started,
            "status": getattr(response, "status", None),
            "output_text": output,
            "usage": {
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            },
        }
        dump(self.root / f"{name}-response-0.json", response_record)
        if not output:
            raise RuntimeError(f"OpenAI DM returned no text (status={response_record['status']})")
        return _json_object(output)


def build_dm_provider(root: Path, provider: str, model: str | None = None,
                      reasoning_effort: str = "low"):
    """Build an explicit provider; `auto` is local to avoid accidental spend."""
    requested = provider.strip().lower()
    if requested == "auto":
        requested = os.environ.get("SABRINA_DM_PROVIDER", "ollama").strip().lower()
        if requested == "auto":
            requested = "ollama"
    if requested == "ollama":
        return OllamaDMProvider(root, model or os.environ.get("SABRINA_DM_MODEL", DEFAULT_LOCAL_MODEL))
    if requested == "openai":
        selected = model or os.environ.get("SABRINA_DM_MODEL", DEFAULT_OPENAI_MODEL)
        return OpenAIDMProvider(root, selected, reasoning_effort)
    raise ValueError("DM provider must be auto, ollama, or openai")
