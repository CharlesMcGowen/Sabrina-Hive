"""
tools/context_sanitizer.py
===========================
BERT-Gated Context Sanitizer for Sabrina's External Oracle Offloading System.

When Sabrina cannot solve a problem locally, this module scrubs the raw problem
context through two passes before it is ever sent to an external model:

Pass 1 — BERT NER (dslim/bert-base-NER):
    Detects and replaces PII-class entities: person names, organizations,
    locations. These are generalised to [ENTITY_TYPE_N] placeholders.

Pass 2 — SecureBERT Classification + Regex Hard-Scrub:
    Uses the existing SecureBERT triage model for cyber-domain awareness
    (tactics/techniques that shouldn't be disclosed), combined with
    deterministic regex patterns for IPs, hostnames, URLs, credentials,
    API keys, file paths, and JWT tokens.

A local-only redaction manifest maps each [PLACEHOLDER_N] back to the
original value. The manifest is NEVER sent externally — it lets Sabrina
re-contextualize the oracle's abstract advice after the fact.

Disclosure Levels:
    LOW    — Max scrub: NER entities + all regex patterns (default)
    MEDIUM — Redact only credentials + IPs/hostnames; keep org names
    HIGH   — Redact only credentials/tokens; keep everything else
"""

import os
import re
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger("sabrina.context_sanitizer")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

try:
    from tools.enterprise_dictionary import get_enterprise_dictionary, EnterpriseDictionaryMatcher
except ImportError:
    try:
        from enterprise_dictionary import get_enterprise_dictionary, EnterpriseDictionaryMatcher
    except ImportError:
        get_enterprise_dictionary = None
        EnterpriseDictionaryMatcher = None

try:
    from tools.entropy_scanner import SecretEntropyScanner
except ImportError:
    try:
        from entropy_scanner import SecretEntropyScanner
    except ImportError:
        SecretEntropyScanner = None

# ---------------------------------------------------------------------------
# Disclosure level & NER mode constants
# ---------------------------------------------------------------------------
LEVEL_LOW    = "LOW"
LEVEL_MEDIUM = "MEDIUM"
LEVEL_HIGH   = "HIGH"

_VALID_LEVELS = {LEVEL_LOW, LEVEL_MEDIUM, LEVEL_HIGH}

NER_MODE_AUTO   = "auto"    # Fast heuristic: runs BERT only when potential entity prose is detected
NER_MODE_ALWAYS = "always"  # Always run BERT NER
NER_MODE_OFF    = "off"     # Never run BERT NER (ultra-fast regex + enterprise dictionary only)

_VALID_NER_MODES = {NER_MODE_AUTO, NER_MODE_ALWAYS, NER_MODE_OFF}

# ---------------------------------------------------------------------------
# Compiled regex patterns (evaluated before NER as a fast pre-pass)
# ---------------------------------------------------------------------------

# IPv4 addresses (including CIDR)
_RE_IPV4 = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
    r"(?:/\d{1,2})?\b"
)

# IPv6 addresses (simplified)
_RE_IPV6 = re.compile(
    r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
    r"|\b(?:[0-9a-fA-F]{1,4}:)*::(?:[0-9a-fA-F]{1,4}:)*[0-9a-fA-F]{1,4}\b"
)

# Emails
_RE_EMAIL = re.compile(
    r"\b[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+\b"
)

# Generic & modern TLDs + common ccTLDs + multi-level ccTLDs
_TLDS = (
    r"(?:com|net|org|edu|gov|mil|int|io|ai|co|app|dev|cloud|tech|site|online|store|"
    r"info|biz|me|xyz|pro|link|live|digital|network|systems|zone|top|vip|shop|global|"
    r"internal|corp|lan|local|test|staging|prod|lab|home|"
    r"us|uk|ca|mx|cn|de|fr|jp|in|au|br|ru|nl|se|no|es|it|ch|kr|sg|hk|tw|eu|za|nz|"
    r"co\.uk|com\.mx|com\.br|com\.au|co\.jp|co\.in|co\.nz|co\.za|gov\.uk|org\.uk|ac\.uk|"
    r"com\.cn|net\.cn|org\.cn|com\.tw|com\.hk|com\.sg)"
)

# Hostnames and FQDNs
_RE_HOSTNAME = re.compile(
    r"(?<![a-zA-Z0-9_\-\.])"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+"
    + _TLDS +
    r"(?::\d{1,5})?"
    r"(?![a-zA-Z0-9_\-])",
    re.IGNORECASE,
)

# Full URLs (http/https/ftp/sftp/ws/wss/ssh/git or www. with optional auth, ports, paths)
_RE_URL = re.compile(
    r"(?:(?:https?|ftp|sftp|ws|wss|git|ssh)://(?:[^@\s]+@)?[^\s\"'<>\`\)\]\}]+|"
    r"\bwww\.[a-zA-Z0-9\-]+(?:\.[a-zA-Z0-9\-]+)+(?::\d{1,5})?(?:/[^\s\"'<>\`\)\]\}]*)?)",
    re.IGNORECASE,
)

# API keys, secrets, tokens (generic high-entropy strings ≥24 chars in key=value context)
_RE_API_KEY = re.compile(
    r"(?:api[_\-]?key|secret|token|password|passwd|pwd|auth|bearer|access[_\-]?token)"
    r"\s*[=:]\s*[\"']?([A-Za-z0-9\-_/+=]{16,})[\"']?",
    re.IGNORECASE,
)

# JWT tokens (header.payload.signature)
_RE_JWT = re.compile(
    r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+"
)

# File system paths (Unix and Windows)
_RE_PATH_UNIX = re.compile(r"(?<!\w)/(?:[a-zA-Z0-9_\-\.]+/){2,}[a-zA-Z0-9_\-\.]*")
_RE_PATH_WIN  = re.compile(r"[A-Za-z]:\\(?:[^\\/:*?\"<>|\r\n]+\\)*[^\\/:*?\"<>|\r\n]*")

# Engagement / company names injected via config (populated at runtime)
# These are matched case-insensitively if provided via sanitizer config.
_RE_ENGAGEMENT_TMPL = None  # Set dynamically per instance


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SanitizedContext:
    """The result of a sanitization pass."""
    safe_text: str              # The scrubbed text — safe to send externally
    manifest: Dict[str, str]    # placeholder → original value (NEVER sent externally)
    entity_counts: Dict[str, int] = field(default_factory=dict)
    disclosure_level: str = LEVEL_LOW
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Main sanitizer class
# ---------------------------------------------------------------------------

class ContextSanitizer:
    """
    Two-pass BERT + regex context sanitizer.

    Args:
        ner_model:        HuggingFace NER model ID or local path.
                          Defaults to 'dslim/bert-base-NER'.
        securebert_path:  Optional path to the local SecureBERT classifier
                          for cyber-domain tactic filtering.
        engagement_names: List of known engagement / company name strings
                          to deterministically redact.
        device:           'cuda', 'cpu', or None (auto-detect).
    """

    DEFAULT_NER_MODEL = "dslim/bert-base-NER"

    # NER label → placeholder prefix mapping (Updated for fine-tuned model)
    _ENTITY_LABEL_MAP = {
        # Original fallback map
        "PER":  "PERSON",
        "ORG":  "ORG",
        "LOC":  "LOCATION",
        "MISC": "ENTITY",
        
        # New fine-tuned custom model map
        "IP":       "IP",
        "KEY":      "CREDENTIAL",
        "PASSWORD": "CREDENTIAL",
        "EMAIL":    "EMAIL",
        "USERNAME": "USERNAME",
        "NAME":     "PERSON",
        "ID":       "ID",
        "DOC":      "DOCUMENT",
    }

    def __init__(
        self,
        ner_model: str = None,
        securebert_path: Optional[str] = "/home/ego/bot/securebert_classifier_14",
        engagement_names: Optional[List[str]] = None,
        device: Optional[str] = None,
        enable_entropy_scrub: bool = True,
        min_entropy_length: int = 8,
    ):
        # Default to local custom model if not provided
        self.ner_model_name = ner_model or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "models", "sabrina-pii-ner"))
        # If the local model directory doesn't exist yet, fallback to standard huggingface model
        if not os.path.exists(self.ner_model_name):
            logger.info(f"[ContextSanitizer] Custom NER model not found at {self.ner_model_name}. Falling back to default.")
            self.ner_model_name = self.DEFAULT_NER_MODEL
            
        self.securebert_path = securebert_path
        self.engagement_names = engagement_names or []
        self.device = device
        self.enable_entropy_scrub = enable_entropy_scrub

        self.enterprise_dict = None
        if get_enterprise_dictionary:
            self.enterprise_dict = get_enterprise_dictionary(custom_terms=self.engagement_names)

        self.entropy_scanner = None
        if self.enable_entropy_scrub and SecretEntropyScanner:
            try:
                self.entropy_scanner = SecretEntropyScanner(min_entropy_length=min_entropy_length)
            except Exception as e:
                logger.warning(f"[ContextSanitizer] Failed to initialize SecretEntropyScanner: {e}")

        self._ner_pipeline = None   # lazy-loaded
        self._securebert = None     # lazy-loaded

        # Build engagement-name regex if names provided
        self._engagement_re: Optional[re.Pattern] = None
        if self.engagement_names:
            self._build_engagement_regex()

    # ------------------------------------------------------------------
    # Lazy loaders
    # ------------------------------------------------------------------

    def _load_ner(self):
        """Lazy-load the HuggingFace NER pipeline."""
        if self._ner_pipeline is not None:
            return
        try:
            from transformers import pipeline as hf_pipeline
            import torch
            dev = self.device
            if dev is None:
                dev = 0 if torch.cuda.is_available() else -1  # -1 = CPU for pipeline
            logger.info(f"[ContextSanitizer] Loading NER model '{self.ner_model_name}' on device={dev}")
            self._ner_pipeline = hf_pipeline(
                "ner",
                model=self.ner_model_name,
                aggregation_strategy="simple",
                device=dev,
            )
            logger.info("[ContextSanitizer] NER model loaded.")
        except Exception as e:
            logger.warning(f"[ContextSanitizer] Could not load NER model: {e}. NER pass will be skipped.")
            self._ner_pipeline = "FAILED"

    def _load_securebert(self):
        """Lazy-load the SecureBERT triage engine if path is valid."""
        if self._securebert is not None:
            return
        try:
            import os
            if self.securebert_path and os.path.isdir(self.securebert_path):
                from tools.securebert_triage import SecureBERTTriageEngine
                self._securebert = SecureBERTTriageEngine(model_path=self.securebert_path)
                logger.info("[ContextSanitizer] SecureBERT triage engine loaded.")
            else:
                self._securebert = "UNAVAILABLE"
        except Exception as e:
            logger.warning(f"[ContextSanitizer] SecureBERT unavailable: {e}")
            self._securebert = "UNAVAILABLE"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_engagement_regex(self):
        escaped = [re.escape(n) for n in self.engagement_names if n.strip()]
        if escaped:
            self._engagement_re = re.compile(
                r"(?<![a-zA-Z0-9])(?:" + "|".join(escaped) + r")(?![a-zA-Z0-9])",
                re.IGNORECASE,
            )
        if self.enterprise_dict and self.engagement_names:
            self.enterprise_dict.add_terms(self.engagement_names)

    def _next_placeholder(self, manifest: Dict[str, str], prefix: str, original: str) -> str:
        """
        Returns an existing placeholder if this exact string was already redacted,
        otherwise creates a new numbered one.
        """
        # Check if we've seen this value before (dedup)
        for ph, orig in manifest.items():
            if orig == original:
                return ph
        idx = sum(1 for k in manifest if k.startswith(f"[{prefix}_")) + 1
        ph = f"[{prefix}_{idx}]"
        manifest[ph] = original
        return ph

    # ------------------------------------------------------------------
    # Pass 1 — Regex scrub
    # ------------------------------------------------------------------

    def _regex_scrub(
        self,
        text: str,
        manifest: Dict[str, str],
        counts: Dict[str, int],
        level: str,
    ) -> str:
        """Apply regex-based redactions. Returns scrubbed text."""

        def replace_match(m: re.Match, prefix: str) -> str:
            original = m.group(0)
            
            # Ignore loopback/null IPs and localhost (even inside URLs)
            lower_val = original.lower()
            if "127." in lower_val or "0.0.0.0" in lower_val or "localhost" in lower_val or "::1" in lower_val:
                return original
                
            counts[prefix] = counts.get(prefix, 0) + 1
            return self._next_placeholder(manifest, prefix, original)

        def replace_cred(m: re.Match) -> str:
            # Preserve the key name (e.g. 'api_key=') and only redact the value
            key_part = m.group(0)[:m.start(1) - m.start(0)]
            secret   = m.group(1)
            counts["CREDENTIAL"] = counts.get("CREDENTIAL", 0) + 1
            return key_part + self._next_placeholder(manifest, "CREDENTIAL", secret)

        # 0. Shannon entropy & specialized secrets (detect-secrets pass)
        if self.entropy_scanner and self.entropy_scanner.is_available:
            text = self.entropy_scanner.scrub(
                text,
                manifest=manifest,
                counts=counts,
                placeholder_prefix="SECRET",
                next_placeholder_fn=self._next_placeholder,
            )

        # Always scrub credentials and JWTs regardless of level
        text = _RE_JWT.sub(lambda m: replace_match(m, "JWT"), text)
        text = _RE_API_KEY.sub(replace_cred, text)

        if level in (LEVEL_LOW, LEVEL_MEDIUM):
            # 1. Emails (scrub before URLs/hostnames can fragment them)
            text = _RE_EMAIL.sub(lambda m: replace_match(m, "EMAIL"), text)
            # 2. Full URLs (scrub before hostnames so hostname replacement doesn't mangle URLs)
            text = _RE_URL.sub(lambda m: replace_match(m, "URL"), text)
            # 3. IPs
            text = _RE_IPV4.sub(lambda m: replace_match(m, "IP"), text)
            text = _RE_IPV6.sub(lambda m: replace_match(m, "IP"), text)
            # 4. Hostnames and FQDNs
            text = _RE_HOSTNAME.sub(lambda m: replace_match(m, "HOST"), text)
            # 5. High-speed enterprise dictionary scrub
            if self.enterprise_dict:
                text = self.enterprise_dict.scrub(
                    text, manifest, counts, placeholder_prefix="ORG", next_placeholder_fn=self._next_placeholder
                )

        if level == LEVEL_LOW:
            text = _RE_PATH_UNIX.sub(lambda m: replace_match(m, "PATH"), text)
            text = _RE_PATH_WIN.sub(lambda m: replace_match(m, "PATH"), text)
            # 6. User-specified engagement names
            if self._engagement_re:
                text = self._engagement_re.sub(
                    lambda m: replace_match(m, "ENGAGEMENT"), text
                )

        return text

    # ------------------------------------------------------------------
    # Pass 2 — BERT NER scrub
    # ------------------------------------------------------------------

    def _ner_scrub(
        self,
        text: str,
        manifest: Dict[str, str],
        counts: Dict[str, int],
        level: str,
        ner_mode: str = NER_MODE_AUTO,
    ) -> Tuple[str, List[str]]:
        """
        Run BERT NER on the (already regex-scrubbed) text and replace
        detected entities with typed placeholders.
        Returns (scrubbed_text, warnings).
        """
        warnings = []

        if level == LEVEL_HIGH or ner_mode == NER_MODE_OFF:
            # In HIGH mode or NER OFF mode, skip neural network pass entirely
            return text, warnings

        # Heuristic Gating (Fast-Path):
        # If ner_mode is 'auto', inspect whether the text has any prose-like capitalized entity candidates.
        # If there are no title-cased words of 3+ letters outside of code syntax, skip the heavy BERT pass.
        if ner_mode == NER_MODE_AUTO:
            if not re.search(r"\b[A-Z][a-z]{2,}\b", text):
                return text, warnings

        self._load_ner()

        if self._ner_pipeline == "FAILED":
            warnings.append("NER model unavailable; skipping NER pass.")
            return text, warnings

        try:
            entities = self._ner_pipeline(text)
        except Exception as e:
            warnings.append(f"NER inference error: {e}")
            return text, warnings

        # Sort by start position descending so replacements don't shift offsets
        entities_sorted = sorted(entities, key=lambda e: e["start"], reverse=True)

        for ent in entities_sorted:
            # Skip low confidence hallucinations
            if ent.get("score", 1.0) < 0.85:
                continue

            ent_group = ent.get("entity_group", "MISC")
            prefix = self._ENTITY_LABEL_MAP.get(ent_group, "ENTITY")
            original = ent.get("word", "")
            if not original or original in ("[IP]", "[HOST]", "[URL]", "[PATH]", "[ORG]"):
                continue  # Already replaced by regex pass
                
            # Ignore loopback/localhost in NER pass too
            lower_val = original.lower()
            if "127." in lower_val or "0.0.0.0" in lower_val or "localhost" in lower_val or "::1" in lower_val:
                continue
                
            # Skip LOC in MEDIUM mode (locations may be needed for context)
            if level == LEVEL_MEDIUM and ent_group == "LOC":
                continue
                
            start = ent["start"]
            end   = ent["end"]
            
            # Word-Boundary Check: If the entity is squished inside a larger alphanumeric token 
            # (e.g. replacing 'ja' inside 'ajax'), it's a sub-word hallucination. Skip it.
            if start > 0 and text[start - 1].isalnum():
                continue
            if end < len(text) and text[end].isalnum():
                continue

            ph = self._next_placeholder(manifest, prefix, original)
            text = text[:start] + ph + text[end:]
            counts[prefix] = counts.get(prefix, 0) + 1

        return text, warnings

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sanitize(
        self, 
        text: str, 
        level: str = LEVEL_LOW,
        ner_mode: str = NER_MODE_AUTO
    ) -> SanitizedContext:
        """
        Sanitize a raw context string for external disclosure.

        Args:
            text:     The raw problem context from Sabrina's stuck state.
            level:    Disclosure level — 'LOW' (max scrub), 'MEDIUM', or 'HIGH'.
            ner_mode: 'auto' (heuristic gating), 'always' (full BERT), or 'off' (dictionary only).

        Returns:
            SanitizedContext with safe_text, manifest, and entity_counts.
        """
        if level not in _VALID_LEVELS:
            logger.warning(f"[ContextSanitizer] Invalid level '{level}', defaulting to LOW.")
            level = LEVEL_LOW

        if ner_mode not in _VALID_NER_MODES:
            logger.warning(f"[ContextSanitizer] Invalid ner_mode '{ner_mode}', defaulting to AUTO.")
            ner_mode = NER_MODE_AUTO

        manifest: Dict[str, str] = {}
        counts:   Dict[str, int] = {}
        all_warnings: List[str] = []

        # Pass 1 — fast regex + enterprise dictionary scrub
        scrubbed = self._regex_scrub(text, manifest, counts, level)

        # Pass 2 — BERT NER scrub (with heuristic gating)
        scrubbed, ner_warnings = self._ner_scrub(scrubbed, manifest, counts, level, ner_mode=ner_mode)
        all_warnings.extend(ner_warnings)

        total_redactions = sum(counts.values())
        logger.info(
            f"[ContextSanitizer] Sanitized {len(text)} → {len(scrubbed)} chars | "
            f"{total_redactions} redactions | level={level} | ner_mode={ner_mode} | "
            f"breakdown={counts}"
        )

        return SanitizedContext(
            safe_text=scrubbed,
            manifest=manifest,
            entity_counts=counts,
            disclosure_level=level,
            warnings=all_warnings,
        )

    def restore(self, text: str, manifest: Dict[str, str]) -> str:
        """
        Reverse-apply a manifest to restore placeholders in oracle output.
        This lets Sabrina re-contextualize abstract oracle advice.

        Placeholders are applied longest-first so that a placeholder whose
        name is a substring of another (e.g. [IP_1] vs [IP_10]) can never
        accidentally get double-substituted.
        """
        for placeholder, original in sorted(manifest.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(placeholder, original)
        return text


# ---------------------------------------------------------------------------
# Module-level singleton (lazy-init, zero VRAM cost until first use)
# ---------------------------------------------------------------------------
_SANITIZER_INSTANCE: Optional[ContextSanitizer] = None


def get_sanitizer(
    engagement_names: Optional[List[str]] = None,
    disclosure_level: str = LEVEL_LOW,
    enable_entropy_scrub: bool = True,
) -> ContextSanitizer:
    """
    Return the module-level singleton ContextSanitizer.
    Creates it on first call. Safe to call from multiple threads (models are
    loaded lazily with their own internal locks).
    """
    global _SANITIZER_INSTANCE
    if _SANITIZER_INSTANCE is None:
        _SANITIZER_INSTANCE = ContextSanitizer(
            engagement_names=engagement_names or [],
            enable_entropy_scrub=enable_entropy_scrub,
        )
    elif engagement_names is not None:
        # Update engagement names only when the caller explicitly passes a list
        _SANITIZER_INSTANCE.engagement_names = engagement_names
        _SANITIZER_INSTANCE._build_engagement_regex()
    return _SANITIZER_INSTANCE


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _test_text = """
    Sabrina is stuck trying to exploit the login form at http://192.168.2.69:3000/rest/user/login
    for engagement CorpAcme Inc. The JWT token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiYWRtaW4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c
    was captured from /home/ego/bot/logs/capture.txt. API key: api_key=sk-ABC123VERYLONGSECRETKEY9999.
    The target server admin@192.168.2.69 belongs to John Smith at CorpAcme Inc.
    """

    sanitizer = ContextSanitizer(engagement_names=["CorpAcme Inc"])
    result = sanitizer.sanitize(_test_text, level=LEVEL_LOW)

    print("\n=== SANITIZED TEXT (safe to send externally) ===")
    print(result.safe_text)
    print("\n=== MANIFEST (NEVER SENT EXTERNALLY) ===")
    for k, v in result.manifest.items():
        print(f"  {k} → {v!r}")
    print("\n=== ENTITY COUNTS ===")
    print(result.entity_counts)
    print(f"\n=== WARNINGS ===")
    for w in result.warnings:
        print(f"  ! {w}")
