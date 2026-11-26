#!/usr/bin/env python3
"""
tools/oracle_providers.py
==========================
Unified Free-Tier AI Oracle Hub & Browser Navigation Interface for Sabrina.

Provides external high-capacity reasoning, architectural guidance, and web navigation
without burdening local GPU VRAM or incurring cloud API costs:
1. GoogleGeminiOracle: Uses Google AI Studio free tier (gemini-3.6-flash / gemini-flash-latest)
   with automatic RPM rate limiting and exponential backoff.
2. BrowserWebAIOracle: Connects via Playwright (CDP or managed browser) for web navigation,
   DOM inspection, and interacting with browser-based AI chats.
3. UnifiedOracleHub: Master dispatcher that routes Oracle/Architect tasks across free-tier
   cloud models, browser engines, and local fallbacks.
"""

import os
import sys
import json
import time
import threading
import urllib.request
import urllib.error
import redis
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

# Single source of truth for lab host / model defaults -- see
# ai_systems/framework/lab_config.py. Falls back to this file's own
# long-standing defaults if the import path isn't set up (e.g. this module
# is run completely standalone outside the repo root).
try:
    from ai_systems.framework import lab_config
except Exception:
    lab_config = None


def _api_tiers_enabled() -> bool:
    """Return True only when API-key tiers (OpenAI, Anthropic, Gemini, HF Router, Groq, NIM, GitHub Models)
    are explicitly enabled by the operator."""
    if lab_config and hasattr(lab_config, "is_api_tiers_enabled"):
        return lab_config.is_api_tiers_enabled()
    return os.environ.get("SABRINA_API_TIERS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")

_SHARED_REDIS_CLIENT = None
_SHARED_REDIS_CHECKED = False


def _get_shared_redis_client():
    """
    Lazily connect to the same lab Redis tools/lab_knowledge.py already uses, for oracle
    state that must survive across processes and fresh instances -- e.g. a Gemini daily-quota
    cooldown. Live-confirmed 2026-09-15: GoogleGeminiOracle's cooldown tracking (self._model_
    cooldowns) was a plain in-memory dict, thrown away every time a fresh instance was created
    -- which happens on EVERY tool_developer.py pre-flight probe (`GoogleGeminiOracle(api_key=
    ...)`, a new object each call) and every fresh UnifiedOracleHub(). So a model already known
    exhausted for the rest of the day (a 429 with quotaId 'GenerateRequestsPerDayPerProjectPer
    Model-FreeTier', a 20/day free-tier cap) got re-probed from scratch by the very next call,
    "succeeded" on the cheap probe alone, got selected for a real multi-call CrewAI task, then
    hit the same wall mid-task with the task itself carrying no fallback once an LLM is picked.
    Fails open (returns None) if Redis is unreachable -- this is a pure optimization layer, not
    a safety boundary, so its absence should never block a real oracle call.
    """
    global _SHARED_REDIS_CLIENT, _SHARED_REDIS_CHECKED
    if _SHARED_REDIS_CHECKED:
        return _SHARED_REDIS_CLIENT
    _SHARED_REDIS_CHECKED = True
    try:
        url = os.environ.get("SABRINA_REDIS_URL", "redis://192.168.2.69:6379/0")
        client = redis.from_url(url, decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.5)
        client.ping()
        _SHARED_REDIS_CLIENT = client
    except Exception:
        _SHARED_REDIS_CLIENT = None
    return _SHARED_REDIS_CLIENT


def _report_oracle_usage(provider: str, model: Optional[str] = None, outcome: str = "success",
                          error_type: Optional[str] = None, cooldown_until_iso: Optional[str] = None,
                          latency_ms: Optional[int] = None, caller: Optional[str] = None,
                          detail: Optional[str] = None) -> None:
    """
    Best-effort, fire-and-forget report of one real (or skipped) attempt against an external
    model provider to the centralized usage ledger (probe_api.py's /api/models/usage) -- the
    "one API endpoint" every oracle-selection decision point should be able to consult before
    choosing a provider. Built 2026-09-15 alongside Gemini's Redis-backed cooldown fix, once it
    became clear NEITHER the metered Claude API nor the Claude subscription CLI tracked usage
    or cooldowns at all, in-memory or otherwise -- every probe re-discovered "credit too low" or
    "not logged in" from scratch, forever, with zero memory across calls or processes.
    Never raises and never meaningfully blocks: this is observability, not a safety boundary,
    so a dashboard that's down (or not yet up) must never break a real oracle call.
    """
    try:
        payload = json.dumps({
            "provider": provider, "model": model, "outcome": outcome,
            "error_type": error_type, "cooldown_until": cooldown_until_iso,
            "latency_ms": latency_ms, "caller": caller, "detail": detail,
        }).encode("utf-8")
        url = os.environ.get("SABRINA_DASHBOARD_URL", "http://127.0.0.1:8086") + "/api/models/usage"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=1.5).read()
    except Exception:
        pass


def _cooldown_key(provider: str, model: Optional[str] = None) -> str:
    return f"sabrina:oracle_cooldown:{provider}:{model or '*'}"


def _is_on_cooldown(provider: str, model: Optional[str] = None) -> bool:
    """
    Shared, cross-process, cross-provider cooldown check (Redis-backed) -- ONE mechanism
    for "is this provider/model currently known bad," used by Gemini (per-model, daily-quota
    aware), the metered Claude API (credit-exhausted), and the Claude subscription CLI
    (not-logged-in) alike, instead of three separate bespoke (or, before 2026-09-15, entirely
    absent) implementations. Fails open (False) if Redis is unreachable.
    """
    client = _get_shared_redis_client()
    if not client:
        return False
    try:
        return bool(client.exists(_cooldown_key(provider, model)))
    except Exception:
        return False


def _set_cooldown(provider: str, model: Optional[str], ttl_seconds: int, reason: str = "rate_limited") -> None:
    client = _get_shared_redis_client()
    if not client:
        return
    try:
        client.set(_cooldown_key(provider, model), reason, ex=max(1, int(ttl_seconds)))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 1. Google Free-Tier Oracle (Gemini 2.5 Flash / Flash Latest)
# ---------------------------------------------------------------------------

class GoogleGeminiOracle:
    """
    Google AI Studio Free Tier provider.
    Provides 15 Requests/Min, 1,000,000 Tokens/Min, and 1,500 Requests/Day completely free.
    """
    DEFAULT_MODELS = (
        lab_config.GEMINI_MODEL_LADDER if lab_config
        else ["gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.1-flash-lite", "gemini-flash-latest"]
    )

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._load_key_from_env()
        self.last_model_used: Optional[str] = None
        self._lock = threading.Lock()
        self._last_call_time = 0.0
        self._min_interval = 4.0  # Safe spacing for 15 RPM free tier limit
        self._dead = False  # only set on hard auth failures (401/403) -- see generate()
        self._call_count = 0
        # This class's own docstring says the free tier allows 1,500 requests/day;
        # this was previously hardcoded to 50, which silently burned out Gemini
        # after ~25 rounds (2 calls/round) and permanently failed over every
        # subsequent call to a much weaker local Ollama model for the rest of the
        # process. 1400 leaves headroom under the real quota for other usage of
        # the same key that day.
        self._max_calls = int(os.environ.get("GEMINI_MAX_DAILY_CALLS", "1400"))
        # Per-model cooldown: model_name -> unix timestamp it's safe to retry after.
        # A 429 means THIS model's quota is exhausted, not that the whole oracle
        # (or the other models in the ladder) is broken -- previously any 429
        # eventually set self._dead = True, which permanently disabled Gemini for
        # the rest of the process even after the quota window reset.
        self._model_cooldowns: Dict[str, float] = {}

    def _is_on_persistent_cooldown(self, model: str) -> bool:
        return _is_on_cooldown("gemini", model)

    def _set_persistent_cooldown(self, model: str, ttl_seconds: int, reason: str = "rate_limited") -> None:
        _set_cooldown("gemini", model, ttl_seconds, reason)

    @staticmethod
    def _seconds_until_pacific_midnight() -> int:
        """Google's Gemini free-tier daily quotas reset at midnight Pacific Time (documented
        behavior) -- compute the actual deterministic reset point instead of guessing a fixed
        backoff window."""
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime, timedelta
            now = datetime.now(ZoneInfo("America/Los_Angeles"))
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
            return max(60, int((tomorrow - now).total_seconds()))
        except Exception:
            return 6 * 3600  # conservative fallback if the system has no tz database

    def _load_key_from_env(self) -> str:
        # tools/secret_store.py checks the OS credential store (Windows Credential Manager)
        # first, then falls back to os.environ / .env files -- this is what actually
        # closes the "which of 15 .env files has the real key" problem, verified live
        # while chasing a lost Anthropic key across bot/bot2/backup clones.
        from tools.secret_store import get_secret
        env_paths = [".env", os.path.join(os.path.dirname(__file__), "..", ".env")]
        return get_secret("GEMINI_API_KEY", env_fallback_paths=env_paths)

    def is_available(self) -> bool:
        return _api_tiers_enabled() and bool(self.api_key) and not self._dead

    def generate(self, system: str, prompt: str, model: Optional[str] = None, max_retries: int = 2, response_format: Optional[str] = None) -> Optional[str]:
        if not self.is_available():
            return None

        # Deterministic short-circuit: if every candidate model is already known exhausted
        # (persisted in Redis by a prior 429 -- see _set_persistent_cooldown), don't even spend
        # this call attempting one -- a cheap probe like tool_developer.py's "reply with OK"
        # pre-flight check would otherwise still appear to succeed (or fail ambiguously) while
        # the real quota stays exhausted, misleading the caller into selecting Gemini anyway.
        candidates = [model] if model else list(self.DEFAULT_MODELS)
        if candidates and all(self._is_on_persistent_cooldown(m) for m in candidates):
            print(f"[GoogleGeminiOracle] all candidate models ({', '.join(candidates)}) are on a known cooldown -- skipping without a network call.")
            for m in candidates:
                _report_oracle_usage("gemini", model=m, outcome="skipped_cooldown")
            return None

        with self._lock:
            if self._call_count >= self._max_calls:
                print(f"[GoogleGeminiOracle] Daily safety limit reached ({self._max_calls} calls). Failing over to local cluster mesh to prevent API exhaustion.")
                self._dead = True
                return None
            
            self._call_count += 1
            now = time.time()
            elapsed = now - self._last_call_time
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call_time = time.time()

        models_to_try = [model] if model else self.DEFAULT_MODELS

        full_prompt = f"{system}\n\nUser Request:\n{prompt}" if system else prompt
        gen_config = {
            "temperature": 0.2,
            "maxOutputTokens": 16384,
        }
        if response_format == "json":
            gen_config["responseMimeType"] = "application/json"

        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": gen_config
        }
        data = json.dumps(payload).encode("utf-8")

        now_ts = time.time()
        for m in models_to_try:
            # Skip a model that's still on cooldown from a recent 429 rather than hammering it
            # again -- self._model_cooldowns covers this process/instance; _is_on_persistent_
            # cooldown covers every OTHER process/instance too (see _get_shared_redis_client).
            if self._model_cooldowns.get(m, 0) > now_ts:
                continue
            if self._is_on_persistent_cooldown(m):
                continue

            # NOTE: Google now issues "AQ."-prefixed API keys (prepaid billing).
            # Those keys 404 when passed as the old `?key=` query parameter and
            # must be sent via the `x-goog-api-key` header instead. The header
            # form works for both AQ.-format and legacy AIzaSy-format keys, so
            # we use it unconditionally rather than branching on key shape.
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
            )

            for attempt in range(max_retries):
                call_start = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        text = res["candidates"][0]["content"]["parts"][0]["text"]
                        self.last_model_used = m
                        _report_oracle_usage("gemini", model=m, outcome="success",
                                              latency_ms=int((time.time() - call_start) * 1000))
                        return text.strip()
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        # Genuine auth failure -- the key itself is bad, not recoverable
                        # by waiting or trying another model. This is the ONLY case that
                        # should permanently disable this oracle for the process.
                        self._dead = True
                        _report_oracle_usage("gemini", model=m, outcome="error", error_type="auth_fail",
                                              detail=f"HTTP {e.code}")
                        return None
                    if e.code == 429:
                        # This model's quota is exhausted for now, not the whole oracle -- bench
                        # just this model and move on to the next one in the ladder. A flat 60s
                        # guess used to apply to EVERY 429 alike -- live-confirmed 2026-09-15 this
                        # is wrong for the actual failure seen: 'GenerateRequestsPerDayPerProject
                        # PerModel-FreeTier', a 20-calls/day cap that won't recover for hours, not
                        # 60 seconds. Read the body Google actually sent instead of guessing:
                        # honor its own retryDelay for a transient per-minute limit, or bench until
                        # the real daily reset (Pacific midnight) when the quotaId says "PerDay".
                        err_body = ""
                        try:
                            err_body = e.read().decode("utf-8", errors="replace")
                        except Exception:
                            pass
                        is_daily_quota = "PerDay" in err_body and "quotaId" in err_body
                        error_type = "daily_quota" if is_daily_quota else "rate_limit"
                        if is_daily_quota:
                            cooldown_s = self._seconds_until_pacific_midnight()
                            print(f"[GoogleGeminiOracle] model {m} hit its DAILY free-tier quota -- "
                                  f"benching it for {cooldown_s}s (until Pacific midnight) instead of "
                                  f"a blind 60s retry, and persisting that to Redis for every other caller.")
                            self._set_persistent_cooldown(m, cooldown_s, reason="daily_quota_exhausted")
                        else:
                            retry_s = 60
                            try:
                                body = json.loads(err_body) if err_body else {}
                                for d in body.get("error", {}).get("details", []):
                                    if str(d.get("@type", "")).endswith("RetryInfo") and "retryDelay" in d:
                                        retry_s = int(float(str(d["retryDelay"]).rstrip("s"))) + 2
                            except Exception:
                                pass
                            print(f"[GoogleGeminiOracle] model {m} rate-limited (429) -- {retry_s}s cooldown "
                                  f"(Google's own retryDelay, not a guess), trying next model.")
                            cooldown_s = retry_s
                        self._model_cooldowns[m] = time.time() + cooldown_s
                        cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)).isoformat()
                        _report_oracle_usage("gemini", model=m, outcome="error", error_type=error_type,
                                              cooldown_until_iso=cooldown_iso, detail=err_body[:300])
                        break
                    if e.code == 404:
                        # Model name invalid/unavailable; no point retrying, try next model.
                        print(f"[GoogleGeminiOracle] model {m} returned 404 (invalid/unavailable model name) -- trying next model.")
                        break
                    print(f"[GoogleGeminiOracle] model {m} HTTP {e.code}: {e.reason}")
                except Exception as e:
                    print(f"[GoogleGeminiOracle] model {m} attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(0.5)
                        continue

        # Every model was either on cooldown or failed this round. This is NOT a
        # permanent failure -- do not set self._dead here. The caller (UnifiedOracleHub)
        # cascades to local/paid for THIS call; the next call will try Gemini again,
        # skipping only whichever specific models are still within their 60s cooldown.
        return None


# ---------------------------------------------------------------------------
# 1.4. OpenAI Oracle (GPT-4o / o3-mini)
# ---------------------------------------------------------------------------

class OpenAIOracle:
    """
    OpenAI API Provider (GPT-4o, o3-mini, GPT-4o-mini).
    Provides frontier reasoning, code architecture, and structured outputs.
    Automatically activates when OPENAI_API_KEY is available.
    """
    DEFAULT_MODELS = ["gpt-4o", "o3-mini", "gpt-4o-mini"]

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._load_key_from_env()
        self.last_model_used: Optional[str] = None
        self._dead = False
        self._lock = threading.Lock()
        self._model_cooldowns: Dict[str, float] = {}

    def _load_key_from_env(self) -> str:
        from tools.secret_store import get_secret
        env_paths = [".env", os.path.join(os.path.dirname(__file__), "..", ".env")]
        return get_secret("OPENAI_API_KEY", env_fallback_paths=env_paths)

    def is_available(self) -> bool:
        return _api_tiers_enabled() and bool(self.api_key) and not self._dead

    def generate(
        self,
        system: str,
        prompt: str,
        model: Optional[str] = None,
        max_retries: int = 2,
        response_format: Optional[str] = None,
    ) -> Optional[str]:
        if not self.is_available():
            return None

        candidates = [model] if model else list(self.DEFAULT_MODELS)
        now_ts = time.time()
        for m in candidates:
            if self._model_cooldowns.get(m, 0) > now_ts:
                continue
            if _is_on_cooldown("openai", m):
                continue

            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})

            payload: Dict[str, Any] = {
                "model": m,
                "messages": messages,
            }
            if m.startswith(("o1", "o3")):
                payload["max_completion_tokens"] = 4096
            else:
                payload["temperature"] = 0.2
                payload["max_tokens"] = 4096

            if response_format == "json":
                payload["response_format"] = {"type": "json_object"}

            data = json.dumps(payload).encode("utf-8")
            url = "https://api.openai.com/v1/chat/completions"
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": "SabrinaOracle/1.0",
                },
            )

            for attempt in range(max_retries):
                call_start = time.time()
                try:
                    with urllib.request.urlopen(req, timeout=45) as resp:
                        res = json.loads(resp.read().decode("utf-8"))
                        text = res["choices"][0]["message"]["content"]
                        self.last_model_used = m
                        _report_oracle_usage("openai", model=m, outcome="success",
                                              latency_ms=int((time.time() - call_start) * 1000))
                        return text.strip() if text else ""
                except urllib.error.HTTPError as e:
                    if e.code in (401, 403):
                        self._dead = True
                        _report_oracle_usage("openai", model=m, outcome="error", error_type="auth_fail",
                                              detail=f"HTTP {e.code}")
                        return None
                    if e.code == 429:
                        cooldown_s = 60
                        self._model_cooldowns[m] = time.time() + cooldown_s
                        _set_cooldown("openai", m, cooldown_s, reason="rate_limited")
                        cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)).isoformat()
                        _report_oracle_usage("openai", model=m, outcome="error", error_type="rate_limit",
                                              cooldown_until_iso=cooldown_iso, detail="HTTP 429 rate limit")
                        break
                    if e.code == 404:
                        break
                    print(f"[OpenAIOracle] model {m} HTTP {e.code}: {e.reason}")
                except Exception as e:
                    print(f"[OpenAIOracle] model {m} attempt {attempt + 1} failed: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(0.5)
                        continue

        return None


# ---------------------------------------------------------------------------
# 1.5. Anthropic API Oracle (Claude 3.5)
# ---------------------------------------------------------------------------

class AnthropicClaudeOracle:
    """
    Anthropic API provider (Claude 3.5 Sonnet).
    Used as a high-capacity fallback when Gemini hits limits.
    """
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._load_key_from_env()

    def _load_key_from_env(self) -> str:
        from tools.secret_store import get_secret
        env_paths = [".env", os.path.join(os.path.dirname(__file__), "..", ".env")]
        return get_secret("ANTHROPIC_API_KEY", env_fallback_paths=env_paths)

    def is_available(self) -> bool:
        if not _api_tiers_enabled() or not self.api_key:
            return False
        try:
            import anthropic
            return True
        except ImportError:
            return False

    def generate(self, system: str, prompt: str) -> Optional[str]:
        if not self.is_available():
            return None

        # Deterministic short-circuit, same mechanism as GoogleGeminiOracle's daily-quota skip
        # (see _is_on_cooldown): a "credit balance too low" 400 doesn't self-resolve on a timer
        # the way a quota does, but re-probing it on every single call (every decide_action
        # round, every delegate_to_engineer pre-flight check) is still pure waste -- bench it for
        # a short window so a human has time to notice and top up before the next real attempt.
        if _is_on_cooldown("claude_api"):
            print("[AnthropicClaudeOracle] known credit-exhausted/broken from a recent call -- skipping without a network call.")
            _report_oracle_usage("claude_api", outcome="skipped_cooldown")
            return None

        # SABRINA_SIMULATE_CLAUDE=1 -- test call frequency/looping behavior against this
        # tier for free before spending real money on a newly-funded key. Routes to the
        # local SMART_LADDER instead and labels the result so it's never mistaken for a
        # real Claude response. Set by the user explicitly; never assumed.
        if os.environ.get("SABRINA_SIMULATE_CLAUDE") == "1":
            print(f"[AnthropicClaudeOracle] SIMULATED (SABRINA_SIMULATE_CLAUDE=1) -- no real API call, no cost. Routing to local smart tier instead.")
            local = LocalOllamaOracle()
            text, model_used = local.generate(system, prompt, tier="smart")
            if text:
                return f"[SIMULATED CLAUDE via {model_used}]\n{text}"
            return None

        import anthropic
        # Every other oracle tier in this cascade has an explicit bound (Gemini: 30s,
        # local big-model Ollama: 300s) -- this client previously had none, and the
        # anthropic SDK's own default (10 minutes) is long enough to look like a full
        # hang of the whole autonomous loop (verified live: a round stalled for 6+
        # minutes at near-zero CPU with no other pending call to blame). 60s is
        # generous for a single Haiku completion; a real stall now fails over to the
        # next tier instead of freezing everything.
        client = anthropic.Anthropic(api_key=self.api_key, timeout=60.0)
        model_id = "claude-haiku-4-5-20251001"
        call_start = time.time()
        try:
            # This is the automatic last-resort fallback tier (only reached after both
            # Gemini and the local Ollama fleet fail), so it deliberately uses the
            # cheapest current Claude model (Haiku) rather than Sonnet — an unattended
            # background safety net shouldn't default to the most expensive option.
            # (claude-3-5-haiku-20241022 -- the prior pin -- was retired by Anthropic;
            # live-confirmed 404 not_found_error on 2026-09-15. Repinned to the current
            # Haiku 4.5 model id.)
            message = client.messages.create(
                model=model_id,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": prompt}]
            )
            _report_oracle_usage("claude_api", model=model_id, outcome="success",
                                  latency_ms=int((time.time() - call_start) * 1000))
            return message.content[0].text.strip()
        except Exception as e:
            err_str = str(e)
            if "credit balance is too low" in err_str.lower():
                error_type = "credit_exhausted"
                # Doesn't resolve on any predictable timer -- a short bench just stops every
                # caller in the meantime from re-discovering the same thing one call at a time.
                cooldown_s = 15 * 60
                cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)).isoformat()
                _set_cooldown("claude_api", None, cooldown_s, reason="credit_exhausted")
            elif "429" in err_str or "rate" in err_str.lower():
                error_type = "rate_limit"
                cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
                _set_cooldown("claude_api", None, 60, reason="rate_limited")
            elif "401" in err_str or "authenticat" in err_str.lower():
                error_type = "auth_fail"
                cooldown_iso = None
            else:
                error_type = "other"
                cooldown_iso = None
            print(f"[AnthropicClaudeOracle] API call failed: {e}")
            _report_oracle_usage("claude_api", model=model_id, outcome="error", error_type=error_type,
                                  cooldown_until_iso=cooldown_iso, detail=err_str[:300])
            return None


# ---------------------------------------------------------------------------
# 1.6. Claude Code CLI Oracle -- subscription quota, not metered API billing
# ---------------------------------------------------------------------------

class ClaudeCLIOracle:
    """
    Shells out to the `claude` CLI in non-interactive mode (`claude -p`).

    The critical distinction from AnthropicClaudeOracle: that class calls the
    Anthropic API directly with an ANTHROPIC_API_KEY, billed per-token against
    whatever balance is funded on that key. `claude -p` WITHOUT `--bare`
    authenticates via the CLI's own logged-in session (OAuth / system keychain)
    -- i.e. Charles's actual Claude Pro/Max subscription -- and draws on that
    plan's included usage instead (verified against the official docs,
    2026-09-15: "bare mode doesn't use your subscription login", meaning
    non-bare -p mode does). Built specifically so Sabrina's Claude usage can
    draw on subscription quota that would otherwise go partly unused during
    the day, rather than only ever spending the separately-funded API credit.

    Requires the CLI installed (`npm install -g @anthropic-ai/claude-code`)
    and logged in once interactively (`claude login`) -- this class can't do
    that part itself, OAuth needs a real human in a browser.

    All tool use is denied (--permission-mode dontAsk --permission-prompts
    none) -- this is meant as a plain text-in/text-out oracle call, the same
    shape as every other tier here, not a second agentic loop running work
    Sabrina's own loop should be doing.
    """
    def __init__(self):
        self._claude_path: Optional[str] = None
        self._checked: bool = False

    def is_available(self) -> bool:
        if not self._checked:
            import shutil
            # shutil.which resolves PATHEXT (.CMD, .EXE, ...) correctly on Windows --
            # verified live: subprocess.run(["claude", ...]) with the bare name fails
            # with WinError 2 ("cannot find the file specified") because the npm
            # install is a claude.CMD shim, which CreateProcess won't launch from a
            # bare, extensionless name the way cmd.exe's own resolution would.
            self._claude_path = shutil.which("claude")
            self._checked = True
        return self._claude_path is not None

    def generate(self, system: str, prompt: str, timeout: int = 300) -> Optional[str]:
        if not self.is_available():
            return None

        # Same shared cooldown mechanism as Gemini/the metered API: "not logged in" doesn't
        # resolve on any timer, but re-spawning a subprocess to rediscover that on every single
        # call (every decide_action round with preferred="claude") is still pure waste while a
        # human hasn't run `claude login` yet.
        if _is_on_cooldown("claude_cli"):
            print("[ClaudeCLIOracle] known not-logged-in/broken from a recent call -- skipping without spawning a subprocess.")
            _report_oracle_usage("claude_cli", outcome="skipped_cooldown")
            return None

        import subprocess
        import tempfile
        # `system` must NOT be passed as a raw CLI argument (`--append-system-prompt <system>`)
        # -- live-confirmed 2026-09-15 via two real failure modes on BOTH active reasoning-loop
        # tasks: `[WinError 206] The filename or extension is too long`, and later a 120s hang on
        # every single round. Root cause: self._claude_path resolves to claude.CMD (an npm shim),
        # which Windows launches via cmd.exe -- subject to cmd.exe's much smaller ~8191-char
        # command-line limit (or the raw CreateProcess ~32,767-char one), and Sabrina's system
        # prompt (the ~3.5K-char CORE OPERATING RULES block, sabrina_gemini_loop.py call_gemini())
        # combined with the other args was enough to blow past it -- unlike `prompt`, which
        # already correctly goes through stdin (`input=`) and has no such limit. Since these are
        # the ONLY oracle tier `preferred="claude"` tasks ever reach (no Gemini/local fallback by
        # design -- "pin to exactly that provider"), this silently meant ZERO real model calls
        # ever succeeded on those tasks once history grew past a certain size: every round just
        # burned the full timeout and fell to fallback_none, repeating the same static
        # ask_human question forever with no actual inference happening.
        # Fix: write `system` to a temp file and use `--append-system-prompt-file <path>` (a real,
        # live-verified-working flag) instead -- only a short file path crosses the CLI argument
        # boundary, so this is immune to system-prompt size regardless of how large it grows.
        sysfile_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="sabrina_claude_sysprompt_",
                encoding="utf-8", delete=False
            ) as sysfile:
                sysfile.write(system or "")
                sysfile_path = sysfile.name

            call_start = time.time()
            result = subprocess.run(
                [self._claude_path, "-p",
                 # --model sonnet 2026-09-19: without an explicit model, `claude -p` used the
                 # configured default (Opus + extended thinking) and a full agent spin-up, which
                 # blew past the 120s oracle timeout on Sabrina's large system+context prompt every
                 # time -> timeout -> cooldown -> no reasoning ever landed. Sonnet is fast and still
                 # easily strong enough to drive the decision loop, and far lighter on subscription
                 # quota for a many-call autonomous loop.
                 "--model", "sonnet",
                 "--append-system-prompt-file", sysfile_path,
                 "--permission-mode", "dontAsk",
                 "--permission-prompts", "none",
                 "--output-format", "json"],
                input=prompt, capture_output=True, text=True, timeout=timeout
            )
            if result.returncode != 0:
                # Per the CLI's own docs: "When a failure happens inside the run, such as
                # missing authentication, Claude Code prints the failure as the result on
                # stdout" -- NOT stderr. Verified live: an unauthenticated call exits 1 with
                # empty stderr and a real JSON body on stdout whose "result" field says
                # "Not logged in - Please run /login". Surface that instead of a bare
                # "exited 1" with nothing to act on.
                detail = (result.stderr or "").strip()
                if not detail and result.stdout:
                    try:
                        detail = json.loads(result.stdout).get("result", "")
                    except Exception:
                        detail = result.stdout[:300]
                print(f"[ClaudeCLIOracle] CLI exited {result.returncode}: {detail[:300]}")
                if "not logged in" in detail.lower():
                    error_type = "not_logged_in"
                    cooldown_s = 15 * 60  # human has to run `claude login`; don't hammer meanwhile
                else:
                    # Live-confirmed 2026-09-16: previously ONLY the exact "not logged in" text
                    # match set a cooldown -- every OTHER failure shape (a raw crash, a timeout)
                    # retried forever with no memory, and on a day the CLI simply wasn't logged
                    # in at all, that meant repeated slow subprocess spawns and at least one
                    # outright crash (a Node.js EPIPE broken-pipe error killing the CLI process
                    # mid-write), each one contributing to the supervisor's stall/restart churn.
                    # A shorter bench than the confirmed-not-logged-in case, since an "other"
                    # failure could in principle be more transient -- but still don't hammer it.
                    error_type = "other"
                    cooldown_s = 5 * 60
                _set_cooldown("claude_cli", None, cooldown_s, reason=error_type)
                cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)).isoformat()
                _report_oracle_usage("claude_cli", outcome="error", error_type=error_type,
                                      cooldown_until_iso=cooldown_iso, detail=detail[:300])
                return None
            data = json.loads(result.stdout)
            text = data.get("result")
            _report_oracle_usage("claude_cli", outcome="success",
                                  latency_ms=int((time.time() - call_start) * 1000))
            return text.strip() if text else None
        except subprocess.TimeoutExpired:
            print(f"[ClaudeCLIOracle] timed out after {timeout}s")
            cooldown_s = 5 * 60
            _set_cooldown("claude_cli", None, cooldown_s, reason="timeout")
            cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)).isoformat()
            _report_oracle_usage("claude_cli", outcome="error", error_type="timeout",
                                  cooldown_until_iso=cooldown_iso, detail=f"timed out after {timeout}s")
            return None
        except Exception as e:
            print(f"[ClaudeCLIOracle] call failed: {e}")
            cooldown_s = 5 * 60
            _set_cooldown("claude_cli", None, cooldown_s, reason="crash")
            cooldown_iso = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_s)).isoformat()
            _report_oracle_usage("claude_cli", outcome="error", error_type="other",
                                  cooldown_until_iso=cooldown_iso, detail=str(e)[:300])
            return None
        finally:
            # delete=False above (required -- a delete=True NamedTemporaryFile is removed the
            # instant its `with` block closes, before subprocess.run ever gets to read it), so
            # cleanup is manual here instead.
            if sysfile_path:
                try:
                    os.remove(sysfile_path)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# 2. Browser-Driven Oracle & Web Navigation (Playwright / CDP)
# ---------------------------------------------------------------------------

class BrowserWebAIOracle:
    """
    Browser-Driven Oracle and Web Navigation interface.
    Can attach to existing Chromium browsers via CDP (FLEET_NODES) or launch a managed browser.
    Allows Sabrina to navigate the web, inspect web apps, and query web-based AI tools.
    """
    def __init__(self, cdp_endpoints: Optional[List[str]] = None):
        self.cdp_endpoints = cdp_endpoints or [
            "http://127.0.0.1:9222",
            "ws://192.168.1.151:9222"
        ]
        self._has_playwright = self._check_playwright()

    def _check_playwright(self) -> bool:
        try:
            import playwright
            return True
        except ImportError:
            return False

    def is_available(self) -> bool:
        return self._has_playwright

    def navigate_and_extract(self, url: str, wait_selector: Optional[str] = None, timeout_ms: int = 15000, prompt: Optional[str] = None) -> Dict[str, Any]:
        """Navigates to a target URL, waits for content, optionally submits a prompt, and returns page title and body text."""
        if not self._has_playwright:
            return {"error": "Playwright is not installed."}

        from playwright.sync_api import sync_playwright

        try:
            with sync_playwright() as p:
                browser = None
                for ep in self.cdp_endpoints:
                    try:
                        if ep.startswith("http"):
                            browser = p.chromium.connect_over_cdp(ep, timeout=2000)
                        elif ep.startswith("ws"):
                            browser = p.chromium.connect(ep, timeout=2000)
                        if browser:
                            break
                    except Exception:
                        browser = None

                if not browser:
                    browser = p.chromium.launch(
                        headless=True,
                        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                    )

                context = browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                page = context.new_page()

                # If wait_selector is 'CHAT_MODE', we run a heuristic AI chat query.
                if wait_selector == "CHAT_MODE":
                    page.goto(url, timeout=timeout_ms)
                    page.wait_for_load_state("domcontentloaded")
                    selectors = [
                        'textarea[placeholder*="message" i]',
                        'textarea[placeholder*="prompt" i]',
                        'textarea[placeholder*="ask" i]',
                        'textarea[placeholder*="search" i]',
                        '[contenteditable="true"]',
                        'input[type="text"]'
                    ]
                    input_loc = None
                    for sel in selectors:
                        try:
                            loc = page.locator(sel).first
                            if loc.is_visible(timeout=2000):
                                input_loc = loc
                                break
                        except: pass
                    if input_loc:
                        # Use the provided prompt, or fallback to a liveness check
                        text_to_send = prompt if prompt else "Please repeat the following text verbatim: 'Testing 123'"
                        input_loc.fill(text_to_send)
                        input_loc.press("Enter")
                        page.wait_for_timeout(5000)
                    title = page.title()
                    content = page.content()
                    inner_text = page.inner_text("body")
                    browser.close()
                    return {"success": True, "title": title, "text_snippet": inner_text[:3000], "html_length": len(content)}

                page.goto(url, timeout=timeout_ms)
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=timeout_ms)
                else:
                    page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)

                title = page.title()
                content = page.content()
                inner_text = page.inner_text("body")

                browser.close()
                return {
                    "success": True,
                    "title": title,
                    "text_snippet": inner_text[:3000],
                    "html_length": len(content)
                }
        except Exception as e:
            return {"error": str(e), "success": False}


# ---------------------------------------------------------------------------
# 3. Local Ollama Oracle (Fleet Cascade)
# ---------------------------------------------------------------------------

class LocalOllamaOracle:
    """
    Local Ollama fleet provider.
    Cascades through installed models across multiple cluster nodes.

    Split into two ladders, not one -- a single priority list always picking the
    "best" available model meant that once a 70B-class model (llama3.3, mixed
    67%/33% CPU/GPU) was discoverable, it got selected for EVERY local call,
    including routine per-round decide_action/reflect calls that used to run in
    seconds on a fully-GPU 7-8B model and now took minutes on a partially
    CPU-bound 70B one -- while the actual escalation path ("cloud") skipped local
    models entirely, so the big model wasn't even being used for the hard moments
    it was meant for. FAST_LADDER is for routine calls (tier="fast", the default);
    SMART_LADDER is for genuine escalation (tier="smart").
    """
    # granite3-dense:8b was first in this ladder and is what actually served
    # decide_action in both live runs checked via data/probe_runs.db's live_thoughts
    # table -- the dominant failure mode across rounds was repeated "unable to parse
    # the action decision as JSON" rather than any real testing happening. Verified
    # via live_thoughts, not assumed: e.g. rounds 3/5/6/8/9 of the smuggling-engine
    # run all failed to parse. qwen2.5:7b promoted to first -- it's already the
    # default sabrina_model/WORKER_MODEL elsewhere in this codebase and the Qwen2.5
    # line is generally more reliable at strict single-JSON-object output than
    # Granite-Dense for this kind of long, multi-block tool-calling prompt.
    FAST_LADDER = [
        # qwen2.5:32b (instruct) promoted to first 2026-09-19: on the A100 pod (kgn0uwgzfk2zhf)
        # routine decide_action/reflect were falling THROUGH this whole ladder -- none of the
        # 7b/8b entries are installed there -- and landing on generate()'s "first model on the
        # node" fallback, which is qwen2.5-coder:32b. The code-specialized coder kept emitting
        # broken/nonsensical decision logic (e.g. a malformed SQLite introspection query repeated
        # until the stagnation guard fired). The general instruct variant is markedly better at
        # multi-step planning and strict single-JSON tool-calling, so decisions should prefer it
        # wherever it's resident. Nodes that don't have it (the 16GB LAN host can't hold a 32b)
        # simply fall through to qwen2.5:7b exactly as before. delegate_to_engineer is unaffected:
        # it routes via create_tool_developer -> SMART_LADDER/QUEEN_MODEL_LADDER (coder-first),
        # not this ladder, so the coder keeps the code-writing.
        #
        # qwq:32b promoted ABOVE the instruct 2026-09-19: this loop's system prompt is written
        # for a genuine reasoning model (it instructs <think> deliberation blocks and "advanced
        # reasoning capabilities"), which neither the coder nor the plain instruct actually is.
        # qwq is Qwen's dedicated reasoning model -- best fit for understanding the algorithm and
        # debugging its own decision logic. qwen2.5:32b (instruct) stays as the next fallback for
        # any node where qwq isn't resident. Both are ~24GB resident and fit beside the coder.
        "qwq:32b",
        "qwen2.5:32b",
        "qwen2.5-coder:14b",
        "huihui_ai/qwen3-abliterated:14b",
        "qwen2.5:7b",
        # "granite3-dense:8b"/"granite3-dense:latest" were phantom entries -- neither matches
        # what's actually installed (real name is "granite3.1-dense:8b", confirmed live via
        # LocalOllamaOracle._node_models 2026-09-17). Same phantom-entry class already fixed in
        # SMART_LADDER; harmless while qwen2.5:7b (position 0) is always available, but silently
        # dead weight otherwise.
        "granite3.1-dense:8b",
        "granite-code:8b",
        "FrameworkGranite3b:latest",
    ]
    SMART_LADDER = (
        lab_config.QUEEN_MODEL_LADDER if lab_config
        # Fallback only used if the lab_config import fails entirely (see the
        # try/except at the top of this file). "deepseek-r1:32b" and
        # "qwen2.5:32b" were never actually pulled on either host -- phantom
        # entries removed to match the same fix applied to lab_config.py's
        # QUEEN_MODEL_LADDER.
        else [
            "deepseek-r1:32b",
            "qwen2.5-coder:14b",
            "huihui_ai/qwen3-abliterated:14b",
            "qwen2.5:72b",  # replaced llama3.3:latest 2026-09-17
            "command-r:35b",
        ]
    )
    # Kept for any external caller still referencing the old combined name.
    PRIORITY_LADDER = SMART_LADDER + FAST_LADDER

    def __init__(self, urls: Optional[List[str]] = None):
        # Mesh across local workstation and Lab Host GPU/CPU. "ego"
        # (192.168.1.151) is retired -- 192.168.2.69 is the one real lab host.
        if urls:
            self.urls = urls
        elif lab_config:
            # lab_config.OLLAMA_URLS is the full, ready-made list -- the free LAN lab host plus
            # any RunPod pods (added 2026-09-19), which need their full https://...proxy.runpod.net
            # URL since they don't expose a raw public IP:port for HTTP at all.
            self.urls = list(lab_config.OLLAMA_URLS)
        else:
            self.urls = ["http://127.0.0.1:11434", "http://192.168.2.69:11434"]
        self._node_models = {}  # url -> list of model names
        self._node_model_sizes = {}  # url -> {model name: size in bytes}

    # Live-confirmed 2026-09-18: this lab host's GPU is 16GB (RTX 5060 Ti), and every SMART_LADDER
    # candidate above this size (deepseek-r1:32b ~20GB, command-r:35b ~18.7GB, qwen2.5:72b ~47GB)
    # cannot fully fit regardless of what else is unloaded -- confirmed via /api/ps showing
    # size_vram=0 for deepseek-r1:32b even with every other model freed. 14GB leaves headroom for
    # context/KV-cache overhead beyond the raw model weights. Per-user request 2026-09-18: rather
    # than accept 10-15+ min CPU-bound calls, "smart" tier should skip straight to the largest
    # model that actually fits at real GPU speed.
    # Configurable via SABRINA_MAX_SMART_VRAM_GB so a larger remote node (e.g. a rented
    # A100/H100 pod) can actually be given credit for its real VRAM instead of being capped
    # at the lab Threadripper box's 16GB card -- without this, pointing FALLBACK_HOSTS at a
    # bigger GPU still silently skipped every model above 14GB.
    MAX_SMART_TIER_VRAM_BYTES = int(os.environ.get("SABRINA_MAX_SMART_VRAM_GB", "14")) * (1024 ** 3)

    def is_available(self) -> bool:
        any_available = False
        for url in self.urls:
            try:
                req = urllib.request.Request(f"{url}/api/tags", headers={"User-Agent": "Sabrina"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self._node_models[url] = [m["name"] for m in data.get("models", [])]
                    self._node_model_sizes[url] = {m["name"]: m.get("size", 0) for m in data.get("models", [])}
                    any_available = True
            except Exception as e:
                print(f"[LocalOllamaOracle] node {url} unreachable during is_available() check: {e}")
                self._node_models[url] = []
                self._node_model_sizes[url] = {}
        return any_available

    def generate(self, system: str, prompt: str, tier: str = "fast", response_format: Optional[str] = None, return_thinking: bool = False):
        """
        tier="fast" (default, for routine per-round calls): only considers small,
        fully-GPU-fittable models -- fails over to "any available model" rather
        than silently reaching for a big CPU-bound one, which would defeat the
        point of asking for "fast".
        tier="smart" (for genuine escalation): prefers the big local reasoners
        first, but falls back to the fast ladder rather than returning nothing if
        none of them are loaded/available right now.
        """
        if not self._node_models and not self.is_available():
            print(f"[LocalOllamaOracle] generate() aborted: No Ollama nodes reachable at {self.urls}")
            return None, "unknown"

        if tier == "smart":
            ladder = list(self.SMART_LADDER) + list(self.FAST_LADDER)
        else:
            ladder = list(self.FAST_LADDER)

        # Find the highest priority model (per the selected ladder) across all nodes.
        # For tier="smart", a FIRST PASS skips any candidate too large to fit in this node's
        # GPU (per MAX_SMART_TIER_VRAM_BYTES) -- landing on the largest model that actually runs
        # at real GPU speed instead of the first name in the ladder regardless of fit. If NOTHING
        # in the ladder fits anywhere (all nodes too small for every smart candidate), a SECOND
        # PASS drops the size filter -- still prefers a genuinely too-big model over returning
        # nothing, matching this function's existing "always find something" guarantee.
        best_model = None
        best_url = None

        # Per-process model pin for the multi-dreamer swarm (added 2026-09-19): if
        # SABRINA_FORCE_LOCAL_MODEL is set, THIS process pins every local decision to that exact
        # model wherever it's resident, bypassing ladder selection -- so several dreamer instances
        # can each be pinned to a different model (qwq:32b / qwen2.5:72b / deepseek-r1:32b / ...)
        # via their own supervisor task env and genuinely dream in parallel, sharing findings
        # through the common knowledge base + probe_runs.db (surfaced back to each via
        # read_environment's self-model). Opt-in: unset = normal ladder behavior. If the pinned
        # model isn't resident on any reachable node, fall through to the ladder rather than fail.
        forced_model = os.environ.get("SABRINA_FORCE_LOCAL_MODEL")
        if forced_model:
            for url, models in self._node_models.items():
                if forced_model in models:
                    best_model, best_url = forced_model, url
                    break
            if not best_model:
                print(f"[LocalOllamaOracle] SABRINA_FORCE_LOCAL_MODEL={forced_model} not resident on any node "
                      f"{list(self._node_models)} -- falling back to normal ladder selection.")

        # Warm VRAM check: prefer already-resident ladder models to eliminate cold swapping and avoid coordinator capacity waits
        if not best_model:
            for url in self._node_models:
                try:
                    ps_req = urllib.request.Request(f"{url}/api/ps", headers={"User-Agent": "Sabrina"})
                    with urllib.request.urlopen(ps_req, timeout=2) as resp:
                        resident_list = [rm.get("name") for rm in json.loads(resp.read().decode("utf-8")).get("models", [])]
                    for target_model in ladder:
                        if target_model in resident_list:
                            best_model = target_model
                            best_url = url
                            break
                    if best_model:
                        break
                except Exception:
                    pass

        size_passes = (True, False) if tier == "smart" else (False,)
        for enforce_fit in ((size_passes) if not best_model else ()):
            for target_model in ladder:
                for url, models in self._node_models.items():
                    if target_model not in models:
                        continue
                    if enforce_fit:
                        size = self._node_model_sizes.get(url, {}).get(target_model, 0)
                        # Per-node cap (added 2026-09-19 alongside the RunPod pods) -- a single
                        # global cutoff tuned for the LAN host's 16GB card would otherwise also
                        # apply to the 24GB/80GB RunPod nodes, keeping a small LAN-host model
                        # selected over a genuinely bigger, better-fitting model sitting unused
                        # on the more powerful hardware. Falls back to the flat default for any
                        # node not in that map (e.g. a custom urls=[...] override).
                        node_cap = (lab_config.NODE_VRAM_BYTES.get(url) if lab_config else None) or self.MAX_SMART_TIER_VRAM_BYTES
                        if size > node_cap:
                            continue
                    best_model = target_model
                    best_url = url
                    break
                if best_model:
                    break
            if best_model:
                break

        # Fallback to any available model if the ladder fails entirely
        if not best_model:
            for url, models in self._node_models.items():
                if models:
                    best_model = models[0]
                    best_url = url
                    break

        if not best_model or not best_url:
            print(f"[LocalOllamaOracle] generate() aborted: No models found on any node for tier '{tier}'. Discovered node models: {self._node_models}")
            return None, "unknown"

        # SMART-TIER DEGRADATION GUARD (added 2026-09-20): when a genuine escalation ("smart")
        # can't reach any real reasoner -- e.g. the big RunPod pod is stopped/terminated -- the
        # ladder silently falls through SMART_LADDER into a small FAST_LADDER model on the LAN
        # host. That is exactly how smart-tier decisions ended up running on an 8B and emitting
        # malformed edits (obfuscated-TE RSI run, 2026-09-20). Make the degradation LOUD, and let a
        # correctness-sensitive caller (engagements / RSI) opt into failing over to the cloud tier
        # instead of a weak local model via SABRINA_SMART_TIER_STRICT=1.
        if tier == "smart" and best_model not in self.SMART_LADDER:
            print(f"[LocalOllamaOracle] WARNING: smart-tier requested but NO smart-ladder model is "
                  f"reachable -- degraded to '{best_model}' on {best_url}. A big reasoner node "
                  f"(the RunPod pod) is likely down; decisions may be low quality.")
            if os.environ.get("SABRINA_SMART_TIER_STRICT"):
                print("[LocalOllamaOracle] SABRINA_SMART_TIER_STRICT=1 -> refusing the weak fallback; "
                      "returning None so the caller can escalate to the cloud tier (Gemini/Claude).")
                return None, "unknown"

        # Live-confirmed 2026-09-18: a SMART_LADDER model (e.g. deepseek-r1:32b, ~21GB) landing
        # on a host where a FAST_LADDER model (e.g. qwen2.5:7b, ~6.6GB) is ALSO resident gets
        # squeezed to 0 bytes of VRAM (size_vram=0 in /api/ps) on this host's 16GB GPU -- runs
        # 100% on CPU instead, turning a normal call into a 10-15+ minute stall. Ollama doesn't
        # dynamically re-balance already-loaded models onto freed VRAM on its own. Proactively
        # unloading resident FAST-tier models before a genuine SMART-tier call fixes this at the
        # source rather than relying on a human noticing and running `ollama stop` by hand.
        # Only cull on a VRAM-CONSTRAINED node. On a big node (the 80GB A100, NODE_VRAM_BYTES cap
        # high) there's ample room for several models to stay co-resident, and culling here would
        # actively evict the OTHER dreamers' models in the multi-model swarm -- each smart-tier
        # call from one dreamer was unloading another dreamer's FAST_LADDER model (qwen2.5:32b),
        # so "all models running at once" never actually held (2026-09-19: GPU at 27%, only qwq
        # resident, 53GB free, because A's qwq calls kept evicting B's qwen2.5:32b). Skip the cull
        # whenever the node has plenty of VRAM; keep it for the 16-24GB hosts it was written for.
        _cull_node_cap = (lab_config.NODE_VRAM_BYTES.get(best_url) if lab_config else None) or self.MAX_SMART_TIER_VRAM_BYTES
        _vram_constrained = _cull_node_cap < (40 * (1024 ** 3))
        from tools.control_client import configured, caller
        shared_coordinator = configured(best_url)
        if not shared_coordinator and tier == "smart" and best_model in self.SMART_LADDER and _vram_constrained:
            try:
                # Live-confirmed 2026-09-19: a RunPod SECURE-cloud pod's proxy.runpod.net domain
                # sits behind Cloudflare, which blocks Python's default urllib User-Agent with a
                # 403 (Cloudflare error 1010) -- every request to a Runpod-hosted node needs the
                # same explicit User-Agent the /api/tags check already uses, not just that one.
                ps_req = urllib.request.Request(f"{best_url}/api/ps", headers={"User-Agent": "Sabrina"})
                with urllib.request.urlopen(ps_req, timeout=3) as resp:
                    resident = json.loads(resp.read()).get("models", [])
                for m in resident:
                    if m.get("name") in self.FAST_LADDER and m.get("name") != best_model:
                        print(f"[LocalOllamaOracle] freeing VRAM: unloading resident fast-tier model {m['name']} before smart-tier call")
                        unload_req = urllib.request.Request(
                            f"{best_url}/api/generate",
                            data=json.dumps({"model": m["name"], "keep_alive": 0}).encode("utf-8"),
                            headers={"Content-Type": "application/json", "User-Agent": "Sabrina"},
                        )
                        urllib.request.urlopen(unload_req, timeout=5).read()
            except Exception as e:
                print(f"[LocalOllamaOracle] VRAM pre-free check failed (non-fatal, continuing): {e}")

        payload = {
            "model": best_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            # Sabrina's decide_action/reflect prompts (tool descriptions, working plan,
            # RAG results, up to 60 history entries) can easily exceed 4096 tokens once
            # a run has any real history -- Ollama silently truncates from the START of
            # the context when the prompt doesn't fit, i.e. it drops OLDER entries first,
            # which is exactly where information from a few rounds back (e.g. an obtained
            # auth token) would live. 8192 matches what sabrina_security_engineer.py
            # already uses elsewhere in this project for the same reason.
            "options": {"temperature": 0.2,
                        # num_ctx is env-tunable: default 8192 for fast tier (frees VRAM for 14B models on 16GB GPUs), 16384 for smart tier
                        "num_ctx": int(os.environ.get("SABRINA_NUM_CTX") or (8192 if tier == "fast" else 16384))}
        }
        if return_thinking:
            payload["options"]["think"] = True
        if response_format:
            payload["format"] = response_format
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(f"{best_url}/api/chat", data=data, headers={"Content-Type": "application/json", "User-Agent": "Sabrina"})
        try:
            # 70B/32B models can take a while to process, especially under heavy server load, so increase timeout
            if shared_coordinator:
                with caller('oracle', 'sabrina'):
                    from tools.lab_http import LabHTTP
                    res = LabHTTP(best_url).request('/api/chat', payload, timeout=300)
            else:
                with urllib.request.urlopen(req, timeout=1200) as resp:
                    res = json.loads(resp.read().decode("utf-8"))
            if res is not None:
                message = res.get("message", {})
                text = message.get("content", "").strip()
                thinking = message.get("thinking")
                
                # Fallback to extract <think> tag if it's embedded in content
                if not thinking and "<think>" in text:
                    parts = text.split("</think>")
                    if len(parts) > 1:
                        thinking = parts[0].replace("<think>", "").strip()
                        text = parts[1].strip()
                        
                if text:
                    node_identifier = "localhost" if "127.0.0.1" in best_url else best_url.split("//")[1].split(":")[0]
                    if return_thinking:
                        return text, f"local:{node_identifier}:{best_model}", thinking
                    return text, f"local:{node_identifier}:{best_model}"
                else:
                    print(f"[LocalOllamaOracle] generate() warning: empty content returned by {best_url} (model={best_model}). Raw response: {res}")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            print(f"[LocalOllamaOracle] generate() HTTPError {e.code} from {best_url} (model={best_model}): {e.reason} {err_body}")
        except Exception as e:
            print(f"[LocalOllamaOracle] generate() call to {best_url} (model={best_model}) failed: {e}")

        if return_thinking:
            return None, "unknown", None
        return None, "unknown"


# ---------------------------------------------------------------------------
# 4. Unified Oracle Hub (Master Routing)
# ---------------------------------------------------------------------------

class UnifiedOracleHub:
    """
    Master Dispatcher for Free-Tier AI Oracles.
    Routes complex reasoning, bug bounty triage, and architectural planning
    to free cloud/browser models to preserve local hardware.
    """
    def __init__(self):
        self.google_oracle = GoogleGeminiOracle()
        self.openai_oracle = OpenAIOracle()
        self.claude_oracle = AnthropicClaudeOracle()
        self.claude_cli_oracle = ClaudeCLIOracle()
        self.browser_oracle = BrowserWebAIOracle()
        self.local_oracle = LocalOllamaOracle()
        try:
            from tools.free_compute_providers import GitHubModelsOracle, NvidiaNimOracle, GroqOracle
            self.github_models_oracle = GitHubModelsOracle()
            self.nvidia_nim_oracle = NvidiaNimOracle()
            self.groq_oracle = GroqOracle()
        except Exception:
            self.github_models_oracle = None
            self.nvidia_nim_oracle = None
            self.groq_oracle = None
        self.redis_url = os.environ.get("SABRINA_REDIS_URL", "redis://192.168.2.69:6379/0")
        self.redis_client = None
        try:
            self.redis_client = redis.from_url(self.redis_url, decode_responses=True, socket_timeout=0.5, socket_connect_timeout=0.2)
            self.redis_client.ping()
        except Exception:
            self.redis_client = None
        self.enable_live_collective = os.environ.get("SABRINA_ENABLE_LIVE_COLLECTIVE", "1").lower() in ("1", "true", "yes")
        self._collective_router = None

    @property
    def collective_router(self):
        if self._collective_router is None:
            from tools.collective_router_and_shadow_daemon import CollectiveRouter
            self._collective_router = CollectiveRouter()
        return self._collective_router

    def query_collective(
        self,
        prompt: str,
        *,
        tier: str = "auto",
        task_id: str = "",
        use_cache: bool = True,
        allow_tier_escalation: bool = True,
        required_capability: str = "grounded_summarization",
        tool_allowlist: tuple = (),
        supervised: bool = False,
    ) -> dict:
        """
        Guild 1 + Guild 4 Upgrade: Route a query through Sabrina's 29+ live-qualified
        external collective portals using 3-tier latency/entropy routing (`flash`,
        `deep_reasoning`, `domain_rag`) and SQLite Semantic Query Caching (`<1ms` cache hits).
        """
        return self.collective_router.route(
            prompt,
            tier=tier,
            task_id=task_id,
            use_cache=use_cache,
            allow_tier_escalation=allow_tier_escalation,
            required_capability=required_capability,
            tool_allowlist=tool_allowlist, supervised=supervised,
        )

    def _get_dynamic_providers(self) -> List[dict]:
        # Catalog URLs and legacy Redis scores are not an execution interface.
        # External calls must use query_collective with current role evidence.
        return []

    @staticmethod
    def validate_provider_attribution(record: dict) -> dict:
        """Verify that requested vs actual provider/model/interface are honest and never attribute a local fallback to Gemini."""
        if not isinstance(record, dict):
            raise ValueError("Attribution record must be a dict")
        if record.get("execution_mode") == "live" and record.get("is_simulated"):
            raise ValueError("Simulated response presented as live in UnifiedOracleHub")
        req_prov = str(record.get("requested_provider") or "").strip().lower()
        act_prov = str(record.get("actual_provider") or "").strip().lower()
        act_model = str(record.get("actual_model") or "unknown").strip() or "unknown"
        act_iface = str(record.get("actual_interface") or "unknown").strip().lower()
        # Reject local fallback attributed to Gemini / Google
        is_local_artifact = (
            "ollama" in act_iface
            or act_model.startswith("local:")
            or any(k in act_model.lower() for k in ("qwen", "llama", "deepseek-r1", "huihui_ai"))
        )
        if act_prov in {"google", "gemini"} and (is_local_artifact or record.get("fallback_to_local")):
            raise ValueError(
                f"Local fallback ({act_model} via {act_iface}) cannot be attributed to {act_prov}"
            )
        return record

    @staticmethod
    def _parse_model_label(model_label: str) -> tuple:
        """Return ``(provider, model, interface, source)`` from a routed model label.

        Local and collective labels carry an extra routing identity:
        ``local:<node>:<model>`` and ``collective:<portal>:<model>``.  Keeping
        that identity separate prevents provenance consumers from mistaking a
        node/portal name for the model that actually produced the response.
        """
        label = str(model_label or "").strip()
        if not label or label == "fallback_none":
            return "none", "unknown", "unknown", None

        prefix, separator, remainder = label.partition(":")
        if not separator:
            return label, "unknown", "unknown", None

        if prefix == "local":
            node, separator, raw_model = remainder.partition(":")
            return (
                "local_ollama",
                raw_model.strip() if separator and raw_model.strip() else "unknown",
                "ollama_http",
                node.strip() or None,
            )

        if prefix == "collective":
            portal, separator, raw_model = remainder.partition(":")
            return (
                "collective",
                raw_model.strip() if separator and raw_model.strip() else "unknown",
                "external_collective",
                portal.strip() or None,
            )

        if prefix in {"google", "gemini"}:
            return "google", remainder.strip() or "unknown", "rest_api", None
        if prefix == "openai":
            return "openai", remainder.strip() or "unknown", "rest_api", None
        if prefix == "claude-cli":
            return "claude-cli", remainder.strip() or "unknown", "cli", None
        if prefix in {"groq", "github", "nvidia"}:
            return prefix, remainder.strip() or "unknown", "rest_api", None
        return prefix, remainder.strip() or "unknown", "unknown", None

    def query_with_provenance(
        self,
        prompt: str,
        system: str = "",
        preferred: str = "auto",
        model: Optional[str] = None,
        response_format: Optional[str] = None,
        return_thinking: bool = False,
        execution_mode: str = "live",
        allow_fallback: bool = True,
    ) -> dict:
        """Execute an oracle query and return full requested vs. actual provider/model/interface provenance."""
        if execution_mode not in {"live", "simulation", "replay"}:
            raise ValueError(f"execution_mode must be 'live', 'simulation', or 'replay'; got {execution_mode!r}")
        t0 = time.perf_counter()
        requested_provider = preferred
        requested_model = model or "provider_default"
        requested_interface = (
            "ollama_http"
            if preferred in {"local", "local_first", "escalate"}
            else ("cli" if preferred in {"claude", "claude_cli"} else "rest_api")
        )
        if preferred in {
            "collective",
            "swarm",
            "collective_router",
            "collective_flash",
            "collective_reasoning",
            "collective_rag",
            "collective_live",
        }:
            requested_interface = "external_collective"

        res_text, model_label, thinking = None, "fallback_none", None
        res_tuple = self.query(
            prompt=prompt,
            system=system,
            preferred=preferred,
            model=model,
            response_format=response_format,
            return_thinking=True,
            allow_fallback=allow_fallback,
        )
        res_text, model_label, thinking = res_tuple[0], res_tuple[1], res_tuple[2]
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)

        actual_provider, actual_model, actual_interface, route_source = self._parse_model_label(model_label)

        primary_map = {
            "google": "google",
            "gemini": "google",
            "openai": "openai",
            "local": "local_ollama",
            "local_first": "local_ollama",
            "escalate": "local_ollama",
            "claude": "claude-cli",
            "claude_cli": "claude-cli",
            "auto": "google",
            "cloud": "google",
            "collective": "collective",
            "swarm": "collective",
            "collective_router": "collective",
            "collective_flash": "collective",
            "collective_reasoning": "collective",
            "collective_rag": "collective",
            "collective_live": "collective",
        }
        expected_primary = primary_map.get(preferred, preferred)
        fallback_occurred = (
            actual_provider != "none"
            and expected_primary != actual_provider
        ) or (
            model is not None
            and actual_model not in {"unknown", model}
        )

        if not allow_fallback and fallback_occurred:
            raise RuntimeError(
                f"Provider fallback disallowed: requested {requested_provider}/{requested_model}, "
                f"but primary was unavailable (would have used {actual_provider}/{actual_model})."
            )

        est_prompt_tokens = max(1, (len(system) + len(prompt)) // 4)
        est_completion_tokens = max(0, len(res_text or "") // 4)
        record = {
            "ok": actual_provider != "none" and bool(res_text),
            "execution_mode": execution_mode,
            "is_simulated": False,
            "requested_provider": requested_provider,
            "requested_model": requested_model,
            "requested_interface": requested_interface,
            "actual_provider": actual_provider,
            "actual_model": actual_model if actual_model else "unknown",
            "actual_interface": actual_interface,
            "route_source": route_source,
            "fallback_allowed": bool(allow_fallback),
            "fallback_occurred": fallback_occurred,
            "fallback_to_local": fallback_occurred and actual_provider == "local_ollama",
            "latency_ms": elapsed_ms,
            "observed_usage": None,
            "estimated_usage": {
                "prompt_tokens_estimate": est_prompt_tokens,
                "completion_tokens_estimate": est_completion_tokens,
                "is_estimate": True,
            },
            "response": res_text,
            "model_label": model_label,
            "thinking": thinking if return_thinking else None,
        }
        return self.validate_provider_attribution(record)

    def query(
        self,
        prompt: str,
        system: str = "",
        preferred: str = "auto",
        model: Optional[str] = None,
        response_format: Optional[str] = None,
        return_thinking: bool = False,
        allow_fallback: bool = True,
    ):
        """
        Queries the best available oracle, cost-ordered: free options first, then the
        Claude Pro/Max subscription CLI (already paid for monthly, not metered).

        POLICY (added 2026-09-15): the metered Claude API (ANTHROPIC_API_KEY, real
        per-token billing) is NEVER reached from this method, for any `preferred`
        value. This hub backs `call_gemini()`, i.e. every decide_action/reasoning/
        planning round -- the loop that *drives* bug-bounty engagements. A single
        such loop burned the funded $5 credit in 38 minutes with zero submittable
        findings (109 calls, prompt size unbounded-growing to 154K chars/round,
        mostly re-narrating status instead of progressing). Metered spend is
        reserved for the engineering/tool-building track instead: local/auditing
        models drive testing and reasoning for free (local Ollama, then Google's
        free tier, then subscription CLI), and when THAT loop decides it needs new
        or better code, it calls `query_sabrina_crewai()` (tools/agent_skills.py),
        which has its own separate LLM wiring in `_select_tool_developer_llm()`
        (ai_systems/agents/tool_developer.py) that still uses the metered key --
        framed as a neutral "build/refine this code" engineering request, never as
        live vuln-testing (same reasoning as the Gemini-refusal-avoidance framing
        already used there). If you need to spend metered dollars on a NEW call
        site, route it through that engineering path, not through this hub.
        preferred: 'google' | 'local' | 'claude' | 'claude_cli' | 'escalate' | 'cloud' | 'local_first' | 'auto'
          - 'claude_cli': pin to the `claude` CLI in -p mode ONLY -- uses Charles's logged-in
            Claude Pro/Max subscription quota (via OAuth), not the separately-funded
            ANTHROPIC_API_KEY (which, per the POLICY note above, this hub never reaches at all,
            for any preferred value). Requires the CLI installed and `claude login` run once.
          - 'local': ONLY the local fleet's FAST tier (small, fully-GPU models), no fallback at
            all. If local is mid-reload or briefly unavailable, this returns nothing -- use
            'local_first' instead for routine calls that need SOME answer.
          - 'local_first': FAST local tier (cost-free); if it fails for any reason (a model swap
            on the lab host, a transient error), fall through to google then the subscription CLI
            rather than returning nothing. This is what routine per-round calls should use.
          - 'escalate': for a call that already proved hard (e.g. stagnation) -- tries the local
            fleet's SMART tier first (a 70B/32B-class model, e.g. llama3.3 -- free, genuinely
            better reasoning than the fast tier), then falls through to google, then the
            subscription CLI. Deliberately does NOT use the fast local tier -- if that already got
            it wrong, asking it again isn't a real escalation.
          - 'cloud': Google then the subscription CLI, explicitly SKIPPING all local models -- for
            the rare case you know local (fast or smart) is unavailable/unwanted.
          - 'google' / 'claude': pin to exactly that one free/subscription provider only ('claude'
            here means the subscription CLI, never the metered API).
          - 'auto': google -> local (fast) -> subscription CLI.
        allow_fallback: when false, execute only the primary route implied by
          ``preferred``.  This is used by provenance-sensitive callers so an
          unavailable primary provider becomes an explicit ``fallback_none``
          result instead of silently switching model families.
        Returns: (response_text, model_used)
        """
        # Normalize compound preferences before entering the provider branches.
        # The old implementation checked fallback attribution only after the
        # full cascade had already run, which made allow_fallback=False a
        # reporting flag rather than a routing guarantee.
        if not allow_fallback:
            strict_preference = {
                "auto": "google",
                "cloud": "google",
                "local_first": "local",
            }.get(preferred, preferred)
            if strict_preference != preferred:
                return self.query(
                    prompt=prompt,
                    system=system,
                    preferred=strict_preference,
                    model=model,
                    response_format=response_format,
                    return_thinking=return_thinking,
                    allow_fallback=False,
                )

        # 0. Collective / Multi-Mind Swarm Consensus Mode (wired to live CollectiveRouter + Swarm-Authored Guild B Bridge)
        if preferred in [
            "collective_router",
            "collective_flash",
            "collective_reasoning",
            "collective_rag",
            "collective_live",
        ]:
            from harnesses.external.swarm_system_bridges import oracle_collective_fallback
            tier_map = {
                "collective_router": "auto",
                "collective_live": "auto",
                "collective_flash": "flash",
                "collective_reasoning": "deep_reasoning",
                "collective_rag": "domain_rag",
            }
            rep, label = oracle_collective_fallback(
                self.collective_router,
                self.collective_router.blackboard,
                system,
                prompt,
                tier=tier_map[preferred],
                domain=f"oracle_{tier_map[preferred]}",
            )
            if rep:
                if return_thinking:
                    return rep, label, None
                return rep, label

        if preferred in ["collective", "swarm"]:
            if getattr(self, "enable_live_collective", False):
                from harnesses.external.swarm_system_bridges import oracle_collective_fallback
                rep, label = oracle_collective_fallback(
                    self.collective_router,
                    self.collective_router.blackboard,
                    system,
                    prompt,
                    tier="auto",
                    domain="oracle_collective",
                )
                if rep:
                    if return_thinking:
                        return rep, label, None
                    return rep, label
            raise RuntimeError(
                "Live collective portal adapter is not enabled on UnifiedOracleHub.query; "
                "refusing to silently invoke synthetic swarm callbacks."
            )

        # 1. Try Google Free Tier first (sub-second latency, massive context)
        if preferred in ["google", "cloud", "auto"] and self.google_oracle.is_available():
            res = self.google_oracle.generate(system=system, prompt=prompt, model=model, response_format=response_format)
            if res:
                if return_thinking:
                    return res, f"google:{self.google_oracle.last_model_used or 'gemini'}", None
                return res, f"google:{self.google_oracle.last_model_used or 'gemini'}"

        # 1.5. Try OpenAI Oracle (frontier GPT-4o / o3-mini) if explicitly preferred or available
        if (preferred in ["openai"] or (preferred in ["cloud", "auto"] and self.openai_oracle.is_available())):
            res = self.openai_oracle.generate(system=system, prompt=prompt, model=model, response_format=response_format)
            if res:
                if return_thinking:
                    return res, f"openai:{self.openai_oracle.last_model_used or 'openai'}", None
                return res, f"openai:{self.openai_oracle.last_model_used or 'openai'}"

        # 1.6 Free-tier cloud inference gateways (Groq, GitHub Models, NVIDIA NIM)
        if preferred == "groq" or (preferred in ["cloud", "auto"] and getattr(self, "groq_oracle", None) and self.groq_oracle.is_available()):
            res = self.groq_oracle.generate(system=system, prompt=prompt, model=model, response_format=response_format)
            if res:
                if return_thinking:
                    return res, f"groq:{self.groq_oracle.last_model_used or 'groq'}", None
                return res, f"groq:{self.groq_oracle.last_model_used or 'groq'}"

        if preferred in ["github", "github_models"] or (preferred in ["cloud", "auto"] and getattr(self, "github_models_oracle", None) and self.github_models_oracle.is_available()):
            res = self.github_models_oracle.generate(system=system, prompt=prompt, model=model, response_format=response_format)
            if res:
                if return_thinking:
                    return res, f"github:{self.github_models_oracle.last_model_used or 'github'}", None
                return res, f"github:{self.github_models_oracle.last_model_used or 'github'}"

        if preferred in ["nvidia", "nvidia_nim"] or (preferred in ["cloud", "auto"] and getattr(self, "nvidia_nim_oracle", None) and self.nvidia_nim_oracle.is_available()):
            res = self.nvidia_nim_oracle.generate(system=system, prompt=prompt, model=model, response_format=response_format)
            if res:
                if return_thinking:
                    return res, f"nvidia:{self.nvidia_nim_oracle.last_model_used or 'nvidia'}", None
                return res, f"nvidia:{self.nvidia_nim_oracle.last_model_used or 'nvidia'}"

        # 2. Browser fallback has been disabled by user request.
        # (It previously attempted to scrape web AI interfaces like Mistral, which caused issues with login walls).

        # 3. Cascade to Local Ollama Fleet -- FAST tier for routine calls (free, small,
        # fully-GPU models); SMART tier for genuine escalation (free, big, better reasoning,
        # but partially CPU-bound and much slower per call).
        if preferred in ["local", "local_first", "auto"] and self.local_oracle.is_available():
            result = self.local_oracle.generate(system=system, prompt=prompt, tier="fast", response_format=response_format, return_thinking=return_thinking)
            res, model = result[0], result[1]
            if res:
                return result

        if preferred == "escalate" and self.local_oracle.is_available():
            result = self.local_oracle.generate(system=system, prompt=prompt, tier="smart", response_format=response_format, return_thinking=return_thinking)
            res, model = result[0], result[1]
            if res:
                return result

        # 3.5 'local_first' and 'escalate' both degrade to a cloud cascade if local didn't come through.
        if allow_fallback and preferred in ["local_first", "escalate"]:
            if self.google_oracle.is_available():
                res = self.google_oracle.generate(system=system, prompt=prompt, response_format=response_format)
                if res:
                    if return_thinking:
                        return res, f"google:{self.google_oracle.last_model_used or 'gemini'}", None
                    return res, f"google:{self.google_oracle.last_model_used or 'gemini'}"
            if self.claude_cli_oracle.is_available():
                res = self.claude_cli_oracle.generate(system=system, prompt=prompt)
                if res:
                    if return_thinking:
                        return res, "claude-cli:subscription", None
                    return res, "claude-cli:subscription"
            # Metered claude_oracle (ANTHROPIC_API_KEY) is deliberately NOT tried here -- see
            # policy note above `query()`. Reasoning/planning calls stop at free local/Google/CLI
            # tiers; if all three miss, this falls through to fallback_none below.

        # 4. CLI/subscription Claude: last resort for 'auto', or the second half of an explicit
        # 'cloud' escalation. This draws on Charles's already-paid Pro/Max subscription quota, not
        # the metered API key, so it's fine for reasoning/planning to reach it.
        if preferred in ["claude", "claude_cli", "cloud", "auto"] and self.claude_cli_oracle.is_available():
            res = self.claude_cli_oracle.generate(system=system, prompt=prompt)
            if res:
                if return_thinking:
                    return res, "claude-cli:subscription", None
                return res, "claude-cli:subscription"

        # The metered claude_oracle (ANTHROPIC_API_KEY, real per-token billing) is intentionally
        # NOT reachable from this hub at all, for ANY `preferred` value -- see policy note above
        # `query()` (added 2026-09-15 after a single reasoning-loop task exhausted the funded $5
        # credit in 38 minutes driving bug-bounty decide_action rounds with no submittable
        # finding). The metered tier is reserved for the engineering/tool-building track only:
        # `query_sabrina_crewai()` (tools/agent_skills.py) -> `_select_tool_developer_llm()`
        # (ai_systems/agents/tool_developer.py) has its own separate LLM wiring that still uses
        # it, framed as a neutral "build/refine this code" request. If this hub's local+Google+CLI
        # cascade all miss, that's a real "everything's down" state -- report it via fallback_none
        # rather than spending metered dollars to paper over it.

        # 5. Return fallback notification if every provider fails
        if response_format == "json":
            fallback_json = json.dumps({
                "tool": "ask_human",
                "question": "All free/subscription oracle providers (Local Ollama, Google Gemini, Claude subscription CLI) are currently unavailable or unresponsive. Please verify local model status or the CLI login. (The metered Claude API is not used for reasoning by policy -- see UnifiedOracleHub.query().)",
                "options": ["Retry last action", "Abort loop"]
            })
            if return_thinking:
                return fallback_json, "fallback_none", None
            return fallback_json, "fallback_none"

        if return_thinking:
            return "[ORACLE_FALLBACK: Free/subscription providers all unavailable (metered API intentionally excluded from reasoning by policy).]", "fallback_none", None
        return "[ORACLE_FALLBACK: Free/subscription providers all unavailable (metered API intentionally excluded from reasoning by policy).]", "fallback_none"

    def query_sanitized(
        self,
        raw_context: str,
        task_type: str = "reasoning",
        engagement_names=None,
        disclosure_level=None,
    ) -> str:
        """
        BERT-gated oracle query with automatic context sanitization.

        Sanitizes `raw_context` through ContextSanitizer before sending it to
        any external model, then returns the oracle's abstract advice.

        Use this instead of `query()` whenever the context may contain internal
        hostnames, IPs, credentials, or other sensitive engagement data.

        Args:
            raw_context:      Unsanitized problem description from Sabrina's planner.
            task_type:        'reasoning' | 'planning' | 'classification' | 'code_strategy'
            engagement_names: List of known engagement/company name strings to redact.
            disclosure_level: Override disclosure level ('LOW', 'MEDIUM', 'HIGH').
                              Defaults to the task_type's policy.

        Returns:
            The oracle's advice string (abstract / placeholder-aware).
        """
        try:
            from tools.oracle_offload_broker import offload_to_oracle
            advice = offload_to_oracle(
                problem_context=raw_context,
                task_type=task_type,
                engagement_names=engagement_names,
                disclosure_level=disclosure_level,
            )
            if advice.success:
                return advice.oracle_response
            return (
                f"[ORACLE_OFFLOAD_FAILED: {advice.error or 'Unknown error'}] "
                "Dispatching to local fleet."
            )
        except Exception as e:
            import logging
            logging.getLogger("sabrina.oracle_providers").warning(
                f"query_sanitized() error: {e}. Falling back to unsanitized query."
            )
            res, _ = self.query(prompt=raw_context)
            return res


# Singleton instance
ORACLE = UnifiedOracleHub()


# ---------------------------------------------------------------------------
# Self-Test Execution
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("[*] Running self-test suite for tools/oracle_providers.py...")
    
    # Test 1: Instantiation
    hub = UnifiedOracleHub()
    assert hub.google_oracle is not None
    assert hub.browser_oracle is not None
    assert hub.local_oracle is not None
    print("[+] Test 1: Instantiation PASSED.")

    # Test 2: Unified Hub Cascade Check
    print("[*] Testing Unified Hub Query (Expect cascade to local if Gemini fails)...")
    res, model_used = hub.query(
        system="You are a verification arbiter.",
        prompt="Reply with the exact word: VERIFIED"
    )
    if res and "VERIFIED" in res.upper():
        print(f"[+] Test 2: Hub Query PASSED (used: {model_used})")
    else:
        print(f"[-] Test 2: Hub Query FAILED (used: {model_used}, got: {res})")

    # Test 3: Playwright capability check
    assert hub.browser_oracle.is_available() is True
    print("[+] Test 3: Browser Playwright automation engine PASSED.")

    print("\n[SELF-TEST] ALL TESTS COMPLETED")
