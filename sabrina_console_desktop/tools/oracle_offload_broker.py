"""
tools/oracle_offload_broker.py
================================
BERT-Gated External Oracle Offload Broker for Sabrina.

When Sabrina's local reasoning hits an impasse, this module:

1. Sanitizes the raw problem context through ContextSanitizer (BERT NER + regex)
   to produce a redacted, safe-to-disclose version with a local manifest.

2. Wraps the sanitized context in a limited-disclosure system prompt that:
   - Describes the system generically (no internal branding)
   - Tells the external model it has a REDACTED context and must reason abstractly
   - Constrains the oracle from requesting more specifics

3. Routes the wrapped prompt through UnifiedOracleHub to the best available
   external free-tier reasoner (Gemini free tier, duck.ai, HuggingFace Chat, etc.)

4. Returns structured OracleAdvice containing:
   - The oracle's raw response
   - The sanitized context (for logging / audit)
   - The manifest (for local re-contextualization)
   - Whether the oracle was successfully reached

The manifest allows Sabrina to call restore() on the oracle's advice to
translate any placeholder references back to real values in the local planner.

Task types supported:
    reasoning      — Why is X happening? What could cause this class of issue?
    planning       — What steps / phases would address this problem?
    classification — What category or type of problem is this?
    code_strategy  — What algorithmic or architectural approach fits this?
"""

import logging
import time
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from tools.context_sanitizer import (
    ContextSanitizer,
    SanitizedContext,
    get_sanitizer,
    LEVEL_LOW,
    LEVEL_MEDIUM,
    LEVEL_HIGH,
)
from tools.oracle_providers import ORACLE

logger = logging.getLogger("sabrina.oracle_offload_broker")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

# ---------------------------------------------------------------------------
# Supported task types and their oracle-facing descriptions
# ---------------------------------------------------------------------------
TASK_TYPES: Dict[str, Dict[str, Any]] = {
    "reasoning": {
        "description": "abstract causal reasoning about why a problem is occurring",
        "disclosure_level": LEVEL_LOW,
    },
    "planning": {
        "description": "high-level step-by-step planning to address a class of problem",
        "disclosure_level": LEVEL_LOW,
    },
    "classification": {
        "description": "classification of what category or type this problem belongs to",
        "disclosure_level": LEVEL_MEDIUM,
    },
    "code_strategy": {
        "description": "algorithmic or architectural strategy recommendations",
        "disclosure_level": LEVEL_MEDIUM,
    },
}

_DEFAULT_TASK_TYPE = "reasoning"

# ---------------------------------------------------------------------------
# Limited-disclosure system prompt template
# ---------------------------------------------------------------------------
_LIMITED_DISCLOSURE_SYSTEM = """\
You are an expert AI reasoning assistant helping an autonomous security \
engineering system that has encountered a problem it cannot resolve locally.

IMPORTANT: The context you are about to receive has been SANITIZED and REDACTED. \
All sensitive identifiers (IP addresses, hostnames, credentials, organization names, \
file paths) have been replaced with generic placeholders like [IP_1], [HOST_2], \
[CREDENTIAL_1], [PATH_1], etc. You will NOT be told the real values behind \
these placeholders.

Your task is to provide {task_description} based solely on the structure \
and nature of the problem as described. Do NOT:
  - Ask what the real values behind any placeholder are
  - Assume any specific technology, vendor, or organization
  - Request additional context beyond what is provided
  - Produce output that depends on knowing the exact placeholder values

DO:
  - Reason about the general class of problem described
  - Provide abstract, transferable strategies and approaches
  - Reference placeholders by their token name if needed (e.g., "the service at [HOST_1]")
  - Be concise, actionable, and structured in your response

Begin your response immediately without preamble."""

# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------
@dataclass
class OracleAdvice:
    """Structured result returned by the OracleOffloadBroker."""
    success: bool
    task_type: str
    oracle_response: str              # Raw oracle text
    sanitized_context: SanitizedContext  # The scrubbed context that was sent
    manifest: Dict[str, str]          # Placeholder → original (NEVER sent externally)
    elapsed_ms: int = 0
    oracle_used: str = "unknown"
    error: Optional[str] = None
    cache_hit: bool = False

    def restore_advice(self) -> str:
        """
        Re-apply the manifest to the oracle's response, translating any
        placeholder references the oracle used back to real values.
        This gives Sabrina a locally-contextualized version of the advice.
        """
        from tools.context_sanitizer import ContextSanitizer
        sanitizer = ContextSanitizer()
        return sanitizer.restore(self.oracle_response, self.manifest)


# ---------------------------------------------------------------------------
# Main broker class
# ---------------------------------------------------------------------------
class OracleOffloadBroker:
    """
    Orchestrates the full sanitize → wrap → query → return pipeline.

    Args:
        engagement_names:   Known engagement / company names to redact.
        enable_redis_cache: Cache sanitized oracle queries in Redis to avoid
                            redundant external calls for repeated stuck-states.
        redis_url:          Redis URL for caching (if enabled).
    """

    def __init__(
        self,
        engagement_names: Optional[List[str]] = None,
        enable_redis_cache: bool = True,
        redis_url: Optional[str] = None,
    ):
        self.engagement_names = engagement_names or []
        self.enable_redis_cache = enable_redis_cache
        self.redis_url = redis_url or os.environ.get(
            "SABRINA_REDIS_URL", "redis://192.168.2.69:6379/0"
        )
        self._redis = None
        if self.enable_redis_cache:
            self._try_connect_redis()

    def _try_connect_redis(self):
        try:
            import redis
            self._redis = redis.from_url(
                self.redis_url, decode_responses=True, socket_timeout=2
            )
            self._redis.ping()
            logger.info("[OracleOffloadBroker] Redis cache connected.")
        except Exception as e:
            logger.warning(f"[OracleOffloadBroker] Redis cache unavailable: {e}")
            self._redis = None

    def _cache_key(self, safe_text: str, task_type: str) -> str:
        """Generate a Redis cache key from a hash of the sanitized prompt + task."""
        h = hashlib.sha256(f"{task_type}:{safe_text}".encode()).hexdigest()[:16]
        return f"sabrina:oracle_offload:{h}"

    def _check_cache(self, cache_key: str) -> Optional[str]:
        if not self._redis:
            return None
        try:
            return self._redis.get(cache_key)
        except Exception:
            return None

    def _write_cache(self, cache_key: str, response: str, ttl_seconds: int = 3600):
        if not self._redis:
            return
        try:
            self._redis.setex(cache_key, ttl_seconds, response)
            logger.debug(f"[OracleOffloadBroker] Cached oracle response at {cache_key}")
        except Exception:
            pass

    def _build_system_prompt(self, task_type: str) -> str:
        task_meta = TASK_TYPES.get(task_type, TASK_TYPES[_DEFAULT_TASK_TYPE])
        return _LIMITED_DISCLOSURE_SYSTEM.format(
            task_description=task_meta["description"]
        )

    def _build_user_prompt(self, sanitized_context: SanitizedContext, task_type: str) -> str:
        lines = [
            f"TASK TYPE: {task_type.upper()}",
            "",
            "--- SANITIZED PROBLEM CONTEXT (all sensitive values redacted) ---",
            sanitized_context.safe_text.strip(),
            "--- END CONTEXT ---",
            "",
            f"Please provide your {task_type} assistance based on the above context.",
        ]
        return "\n".join(lines)

    def offload(
        self,
        problem_context: str,
        task_type: str = "reasoning",
        engagement_names: Optional[List[str]] = None,
        disclosure_level: Optional[str] = None,
    ) -> OracleAdvice:
        """
        Full offload pipeline: sanitize → wrap → query oracle → return advice.

        Args:
            problem_context: Raw, unsanitized description of Sabrina's stuck state.
            task_type:       One of 'reasoning', 'planning', 'classification', 'code_strategy'.
            engagement_names: Override engagement names to redact for this call.
            disclosure_level: Override disclosure level ('LOW', 'MEDIUM', 'HIGH').
                              If None, uses the task_type's default.

        Returns:
            OracleAdvice with oracle_response, manifest, and success flag.
        """
        t_start = time.time()

        if task_type not in TASK_TYPES:
            logger.warning(
                f"[OracleOffloadBroker] Unknown task_type '{task_type}', "
                f"defaulting to '{_DEFAULT_TASK_TYPE}'."
            )
            task_type = _DEFAULT_TASK_TYPE

        task_meta = TASK_TYPES[task_type]
        level = disclosure_level or task_meta["disclosure_level"]

        # Resolve engagement names: instance default + call-level override
        names = list(self.engagement_names)
        if engagement_names:
            names = list(set(names + engagement_names))

        # ── Step 1: Sanitize ────────────────────────────────────────────────
        sanitizer = get_sanitizer(engagement_names=names)
        sanitized = sanitizer.sanitize(problem_context, level=level)

        logger.info(
            f"[OracleOffloadBroker] Sanitized context: "
            f"{len(problem_context)} → {len(sanitized.safe_text)} chars | "
            f"{len(sanitized.manifest)} redactions | level={level}"
        )

        if sanitized.warnings:
            for w in sanitized.warnings:
                logger.warning(f"[OracleOffloadBroker] Sanitizer warning: {w}")

        # ── Step 2: Check Redis cache ────────────────────────────────────────
        cache_key = self._cache_key(sanitized.safe_text, task_type)
        cached_response = self._check_cache(cache_key)
        if cached_response:
            elapsed = int((time.time() - t_start) * 1000)
            logger.info(f"[OracleOffloadBroker] Cache hit for task_type={task_type}")
            return OracleAdvice(
                success=True,
                task_type=task_type,
                oracle_response=cached_response,
                sanitized_context=sanitized,
                manifest=sanitized.manifest,
                elapsed_ms=elapsed,
                oracle_used="cache",
                cache_hit=True,
            )

        # ── Step 3: Build limited-disclosure prompt ──────────────────────────
        system_prompt = self._build_system_prompt(task_type)
        user_prompt   = self._build_user_prompt(sanitized, task_type)

        # ── Step 4: Query the oracle ─────────────────────────────────────────
        logger.info(
            f"[OracleOffloadBroker] Dispatching to UnifiedOracleHub | "
            f"task_type={task_type} | safe_text_len={len(sanitized.safe_text)}"
        )
        oracle_response = None
        oracle_used     = "unknown"
        error_msg       = None

        try:
            oracle_response, oracle_model = ORACLE.query(
                prompt=user_prompt,
                system=system_prompt,
            )
            # Detect fallback notification from the oracle hub
            if oracle_response and "[ORACLE_FALLBACK" in oracle_response:
                oracle_used = "fallback_local"
                error_msg = "All external free-tier and local providers unavailable; hard fallback triggered."
                logger.warning(f"[OracleOffloadBroker] {error_msg}")
            else:
                oracle_used = oracle_model
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[OracleOffloadBroker] Oracle query failed: {e}")

        elapsed = int((time.time() - t_start) * 1000)

        if not oracle_response or not oracle_response.strip():
            return OracleAdvice(
                success=False,
                task_type=task_type,
                oracle_response="",
                sanitized_context=sanitized,
                manifest=sanitized.manifest,
                elapsed_ms=elapsed,
                oracle_used=oracle_used,
                error=error_msg or "Empty oracle response",
            )

        # ── Step 5: Cache successful response ────────────────────────────────
        self._write_cache(cache_key, oracle_response)

        logger.info(
            f"[OracleOffloadBroker] Oracle advice received | "
            f"oracle={oracle_used} | elapsed={elapsed}ms | "
            f"response_len={len(oracle_response)}"
        )

        return OracleAdvice(
            success=True,
            task_type=task_type,
            oracle_response=oracle_response,
            sanitized_context=sanitized,
            manifest=sanitized.manifest,
            elapsed_ms=elapsed,
            oracle_used=oracle_used,
            error=error_msg,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_BROKER_INSTANCE: Optional[OracleOffloadBroker] = None


def get_broker(engagement_names: Optional[List[str]] = None) -> OracleOffloadBroker:
    """
    Return the module-level singleton OracleOffloadBroker.
    Safe to call from multiple threads.
    """
    global _BROKER_INSTANCE
    if _BROKER_INSTANCE is None:
        _BROKER_INSTANCE = OracleOffloadBroker(engagement_names=engagement_names or [])
    return _BROKER_INSTANCE


# ---------------------------------------------------------------------------
# Convenience top-level function for single-call use
# ---------------------------------------------------------------------------
def offload_to_oracle(
    problem_context: str,
    task_type: str = "reasoning",
    engagement_names: Optional[List[str]] = None,
    disclosure_level: Optional[str] = None,
) -> OracleAdvice:
    """
    Top-level convenience function. Sanitizes and queries the oracle.

    Example usage from Sabrina's planner when stuck:

        from tools.oracle_offload_broker import offload_to_oracle

        advice = offload_to_oracle(
            problem_context=stuck_state_description,
            task_type="planning",
        )
        if advice.success:
            # Oracle gave abstract planning steps
            logger.info(f"Oracle advice: {advice.oracle_response}")
            # Optionally re-contextualize with real values
            local_advice = advice.restore_advice()
    """
    broker = get_broker(engagement_names=engagement_names)
    return broker.offload(
        problem_context=problem_context,
        task_type=task_type,
        engagement_names=engagement_names,
        disclosure_level=disclosure_level,
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("[*] Running OracleOffloadBroker self-test...\n")

    _mock_stuck_state = """
    Sabrina is attempting to enumerate valid users on the web application at
    http://192.168.2.69:3000 (OWASP Juice Shop) for CorpAcme Inc engagement.

    The hypothesis: the /rest/user/login endpoint leaks whether a user exists
    via different response timing between valid and invalid usernames.
    Current approach: POST requests with user@email.com and measuring delta-t.

    Problem: Response times are inconsistent due to server-side bcrypt hashing.
    Timing differences are below noise floor (< 5ms). Cannot reliably distinguish
    valid vs invalid accounts. Need alternative enumeration strategy.

    Credentials used: Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test
    Target path: /rest/user/login on server at 192.168.2.69:3000
    Log file: /home/ego/bot/logs/timing_test.log
    """

    advice = offload_to_oracle(
        problem_context=_mock_stuck_state,
        task_type="planning",
        engagement_names=["CorpAcme Inc", "OWASP Juice Shop"],
    )

    print(f"Success: {advice.success}")
    print(f"Oracle: {advice.oracle_used}")
    print(f"Elapsed: {advice.elapsed_ms}ms")
    print(f"Cache hit: {advice.cache_hit}")
    print(f"\n--- Sanitized context sent to oracle ---")
    print(advice.sanitized_context.safe_text)
    print(f"\n--- Manifest (kept local) ---")
    for k, v in advice.manifest.items():
        print(f"  {k} → {v!r}")
    print(f"\n--- Oracle response ---")
    print(advice.oracle_response)
    if advice.success:
        print(f"\n--- Re-contextualized advice (placeholders restored) ---")
        print(advice.restore_advice())
    if advice.error:
        print(f"\n[!] Error: {advice.error}")
