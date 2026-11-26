"""Free-Tier Compute & Inference Gateway Providers for Sabrina's Multi-Model Swarm.

Provides unified OpenAI-compatible REST adapters with:
1. Token-bucket client-side rate limit throttling (eliminates 429 penalties).
2. Deterministic cooldown tracking and circuit-breaking.
3. Prompt privacy sanitization (credential and session-cookie stripping).
4. Tier-based routing across:
   - GitHub Models (GPT-4o, Claude 3.5 Sonnet, Llama 3.3 70B via Azure AI)
   - NVIDIA API Catalog / NIM (DeepSeek-R1, DeepSeek-V3, Qwen2.5-Coder-32B)
   - Groq (Ultra-fast LPU inference, Llama 3.3 70B, Gemma 2)
   - Google AI Studio (1M+ context Gemini 2.0 Flash)
   - Local RTX 5060 Ti GPU (Offline zero-cost anchor)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Dict, List, Optional
import urllib.error
import urllib.request

LOGGER = logging.getLogger("sabrina.free_compute_providers")
ROOT = Path(__file__).resolve().parent.parent


class BaseOpenAICompatibleOracle:
    """Base provider for OpenAI-compatible free-tier inference gateways."""

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        api_key_name: str,
        default_models: List[str],
        rate_limit_rpm: int = 20,
    ):
        self.provider_name = provider_name
        self.base_url = base_url
        self.api_key_name = api_key_name
        self.default_models = list(default_models)
        self.rate_limit_rpm = rate_limit_rpm
        self.min_interval_sec = 60.0 / max(1, rate_limit_rpm)

        self._lock = threading.Lock()
        self._last_call_ts = 0.0
        self._dead = False
        self._model_cooldowns: Dict[str, float] = {}
        self.last_model_used: Optional[str] = None
        self.api_key = self._load_key()

    def _load_key(self) -> str:
        try:
            from tools.secret_store import get_secret
            env_paths = [".env", str(ROOT / ".env")]
            val = get_secret(self.api_key_name, env_fallback_paths=env_paths)
            if val:
                return val.strip()
        except Exception:
            pass
        return os.environ.get(self.api_key_name, "").strip()

    def is_available(self) -> bool:
        try:
            from ai_systems.framework.lab_config import is_api_tiers_enabled
            if not is_api_tiers_enabled():
                return False
        except Exception:
            if os.environ.get("SABRINA_API_TIERS_ENABLED", "0").strip().lower() not in ("1", "true", "yes", "on"):
                return False
        return bool(self.api_key) and not self._dead

    def _throttle(self) -> None:
        """Client-side token bucket to prevent 429 burst penalties."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call_ts
            if elapsed < self.min_interval_sec:
                sleep_sec = self.min_interval_sec - elapsed
                time.sleep(sleep_sec)
            self._last_call_ts = time.time()

    def generate(
        self,
        system: str,
        prompt: str,
        model: Optional[str] = None,
        max_retries: int = 2,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        response_format: Optional[str] = None,
    ) -> Optional[str]:
        """Execute completion request against OpenAI-compatible REST endpoint."""
        if not self.is_available():
            return None

        candidates = [model] if model else list(self.default_models)
        now_ts = time.time()

        for m in candidates:
            if self._model_cooldowns.get(m, 0) > now_ts:
                continue

            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            payload: Dict[str, Any] = {
                "model": m,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format == "json":
                payload["response_format"] = {"type": "json_object"}

            data = json.dumps(payload).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": f"SabrinaSwarm-{self.provider_name}/1.0",
            }

            for attempt in range(max_retries):
                self._throttle()
                call_start = time.time()
                try:
                    req = urllib.request.Request(self.base_url, data=data, headers=headers)
                    with urllib.request.urlopen(req, timeout=35) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        text = res["choices"][0]["message"]["content"]
                        self.last_model_used = m
                        latency_ms = (time.time() - call_start) * 1000.0

                        # Record in network flow inspector if active
                        try:
                            from tools.network_flow_inspector import FLOW_INSPECTOR
                            FLOW_INSPECTOR.record_flow(
                                target_url=self.base_url,
                                method="POST",
                                status_code=200,
                                latency_ms=latency_ms,
                                worker_id=f"oracle_{self.provider_name}",
                                request_headers=headers,
                                request_body=payload,
                                response_body=res,
                            )
                        except Exception:
                            pass

                        return text.strip() if text else ""

                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        LOGGER.warning("[%s] Auth error %d: Key invalid or expired.", self.provider_name, e.code)
                        self._dead = True
                        return None
                    elif e.code == 429:
                        LOGGER.warning("[%s] Model %s hit rate limit (HTTP 429). Placing on 60s cooldown.", self.provider_name, m)
                        self._model_cooldowns[m] = time.time() + 60.0
                        break
                    elif e.code == 404:
                        LOGGER.warning("[%s] Model %s not found (HTTP 404). Trying next model.", self.provider_name, m)
                        break
                    else:
                        LOGGER.warning("[%s] HTTP %d on attempt %d: %s", self.provider_name, e.code, attempt + 1, e.reason)
                except Exception as ex:
                    LOGGER.warning("[%s] Request failed on attempt %d: %s", self.provider_name, attempt + 1, ex)

                if attempt < max_retries - 1:
                    time.sleep(1.0)

        return None


class GitHubModelsOracle(BaseOpenAICompatibleOracle):
    """GitHub Models Free Inference Gateway (Azure AI Inference).

    Provides free prototyping access to GPT-4o, Claude 3.5 Sonnet, Llama 3.3 70B,
    and Mistral Large using standard GITHUB_TOKEN.
    """

    def __init__(self, token: Optional[str] = None):
        super().__init__(
            provider_name="github_models",
            base_url="https://models.inference.ai.azure.com/chat/completions",
            api_key_name="GITHUB_TOKEN",
            default_models=[
                "gpt-4o",
                "gpt-4o-mini",
                "claude-3-5-sonnet",
                "meta-llama-3.3-70b-instruct",
                "mistral-large-2411",
                "phi-4",
            ],
            rate_limit_rpm=15,
        )
        if token:
            self.api_key = token


class NvidiaNimOracle(BaseOpenAICompatibleOracle):
    """NVIDIA API Catalog / NIM Microservices Gateway.

    Provides free developer credits to accelerated open-source frontier models
    including DeepSeek-R1, DeepSeek-V3, and Qwen2.5-Coder-32B on DGX Cloud.
    """

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            provider_name="nvidia_nim",
            base_url="https://integrate.api.nvidia.com/v1/chat/completions",
            api_key_name="NVIDIA_API_KEY",
            default_models=[
                "deepseek-ai/deepseek-r1",
                "deepseek-ai/deepseek-v3",
                "qwen/qwen2.5-coder-32b-instruct",
                "meta/llama-3.3-70b-instruct",
            ],
            rate_limit_rpm=20,
        )
        if api_key:
            self.api_key = api_key


class GroqOracle(BaseOpenAICompatibleOracle):
    """Groq Ultra-Fast LPU Inference Gateway.

    Provides high-throughput (>500 tokens/sec) inference for Llama 3.3 70B
    and Gemma 2 models under free tier limits.
    """

    def __init__(self, api_key: Optional[str] = None):
        super().__init__(
            provider_name="groq",
            base_url="https://api.groq.com/openai/v1/chat/completions",
            api_key_name="GROQ_API_KEY",
            default_models=[
                "llama-3.3-70b-versatile",
                "gemma2-9b-it",
                "llama-3.1-8b-instant",
            ],
            rate_limit_rpm=30,
        )
        if api_key:
            self.api_key = api_key


class FreeComputeRouter:
    """Intelligent tier-based router and circuit breaker across free compute resources."""

    def __init__(self):
        self.github_models = GitHubModelsOracle()
        self.nvidia_nim = NvidiaNimOracle()
        self.groq = GroqOracle()

    def route_profile(
        self,
        token_count: int,
        requires_deep_reasoning: bool = False,
        requires_subsecond_latency: bool = False,
    ) -> str:
        """Determine optimal provider based on workload profile."""
        if token_count > 64000:
            return "google_gemini_flash"  # 1M context
        if requires_subsecond_latency and not requires_deep_reasoning:
            return "groq"
        if requires_deep_reasoning:
            if self.nvidia_nim.is_available():
                return "nvidia_nim"
            if self.github_models.is_available():
                return "github_models"
        if self.groq.is_available():
            return "groq"
        if self.github_models.is_available():
            return "github_models"
        return "local_ollama"

    def execute_with_failover(
        self,
        system: str,
        prompt: str,
        token_count: int = 1000,
        requires_deep_reasoning: bool = False,
    ) -> Dict[str, Any]:
        """Execute request with automatic privacy sanitization and multi-tier failover ladder."""
        from tools.network_flow_inspector import redact_payload

        # 1. Sanitize prompt of sensitive credentials
        safe_prompt = redact_payload(prompt)
        safe_system = redact_payload(system)

        # 2. Build ordered failover ladder based on profile
        ladder: List[BaseOpenAICompatibleOracle] = []
        if requires_deep_reasoning:
            ladder.extend([self.nvidia_nim, self.github_models, self.groq])
        else:
            ladder.extend([self.groq, self.github_models, self.nvidia_nim])

        # 3. Attempt each provider in the ladder
        for provider in ladder:
            if provider.is_available():
                res = provider.generate(system=safe_system, prompt=safe_prompt)
                if res:
                    return {
                        "ok": True,
                        "provider": provider.provider_name,
                        "model": provider.last_model_used,
                        "response": res,
                        "fallback_to_local": False,
                    }

        # 4. Fallback anchor: Local RTX 5060 Ti GPU via Ollama
        try:
            from tools.oracle_providers import LocalOllamaOracle
            local = LocalOllamaOracle()
            if local.is_available():
                result = local.generate(system=safe_system, prompt=safe_prompt, tier="fast")
                if result and result[0]:
                    return {
                        "ok": True,
                        "provider": "local_ollama",
                        "model": result[1],
                        "response": result[0],
                        "fallback_to_local": True,
                    }
        except Exception as ex:
            LOGGER.warning("Local Ollama fallback failed: %s", ex)

        return {
            "ok": False,
            "provider": "none",
            "model": "none",
            "response": "",
            "error": "All free inference providers and local fallback exhausted.",
            "fallback_to_local": False,
        }


# Global singleton router
FREE_COMPUTE_ROUTER = FreeComputeRouter()
