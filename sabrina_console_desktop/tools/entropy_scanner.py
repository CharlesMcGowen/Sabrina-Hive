"""
Entropy and Secret Scanner for Sabrina Bulk Sanitizer.

Uses Yelp's open-source detect-secrets engine locally and offline to detect
high-entropy strings (Shannon entropy), private keys, API tokens, cloud credentials,
and embedded authentication headers in source code, configs, and payloads.

Runs 100% offline with zero external network requests or telemetry.
"""

import logging
import threading
from typing import Dict, List, Tuple, Set, Optional, Callable

logger = logging.getLogger("sabrina.entropy_scanner")

_HAS_DETECT_SECRETS = False
_scan_line = None
_init_lock = threading.Lock()
_initialized = False

# Plugins that cause false positives on normal code syntax, variable names, or URLs:
DEFAULT_DISABLED_PLUGINS = [
    "KeywordDetector",     # Catches variable names like secret=, which _RE_API_KEY handles cleanly
    "BasicAuthDetector",   # Catches '//' in normal URLs
    "IPPublicDetector",    # Handled by _RE_IPV4 / _RE_IPV6
]

try:
    from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
    from detect_secrets.settings import configure_settings_from_baseline
    from detect_secrets.core.scan import scan_line
    _scan_line = scan_line
    _HAS_DETECT_SECRETS = True
except ImportError:
    _HAS_DETECT_SECRETS = False


def _initialize_detect_secrets(disabled_plugins: Optional[List[str]] = None):
    """Globally configures detect-secrets plugins once."""
    global _initialized
    if not _HAS_DETECT_SECRETS or _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        try:
            disabled = set(DEFAULT_DISABLED_PLUGINS)
            if disabled_plugins:
                disabled.update(disabled_plugins)
            mapping = get_mapping_from_secret_type_to_class()
            plugins_used = [
                {"name": plugin_cls.__name__}
                for plugin_cls in mapping.values()
                if plugin_cls.__name__ not in disabled
            ]
            configure_settings_from_baseline({"plugins_used": plugins_used})
            _initialized = True
            logger.debug(f"[SecretScanner] Initialized detect-secrets with {len(plugins_used)} plugins.")
        except Exception as e:
            logger.warning(f"[SecretScanner] Failed to configure detect-secrets: {e}")


class SecretEntropyScanner:
    """
    Scans text locally for cryptographic secrets, API keys, private keys,
    and high-entropy tokens using Shannon entropy heuristics and pattern matchers.
    """

    def __init__(
        self,
        min_entropy_length: int = 8,
        disabled_plugins: Optional[List[str]] = None,
    ):
        self.min_entropy_length = min_entropy_length
        self.disabled_plugins = disabled_plugins or []
        self._available = _HAS_DETECT_SECRETS
        if self._available:
            _initialize_detect_secrets(self.disabled_plugins)

    @property
    def is_available(self) -> bool:
        """Returns True if detect-secrets is installed and configured."""
        return self._available

    def scan(self, text: str) -> List[Tuple[str, str]]:
        """
        Scans lines of text and returns a list of (secret_type, secret_value) tuples.
        """
        if not self._available or not text:
            return []

        results: List[Tuple[str, str]] = []
        for line in text.splitlines():
            # Skip empty lines or excessively long single minified lines for performance
            if not line or len(line) > 8192:
                continue
            try:
                for secret in _scan_line(line):
                    val = secret.secret_value
                    stype = secret.type
                    if not val:
                        continue
                    # Strip quotes or assignment delimiters
                    val_clean = val.strip().strip("'\"= ")
                    if len(val_clean) < self.min_entropy_length:
                        continue
                    # Ignore common URL artifacts or path delimiters
                    if val_clean.startswith("//") or val_clean.startswith("http"):
                        continue
                    results.append((stype, val_clean))
            except Exception:
                continue

        return results

    def scrub(
        self,
        text: str,
        manifest: Dict[str, str],
        counts: Dict[str, int],
        placeholder_prefix: str = "SECRET",
        next_placeholder_fn: Optional[Callable[[Dict[str, str], str, str], str]] = None,
    ) -> str:
        """
        Detects secrets and replaces them with numbered placeholders in text.
        Sorts secrets longest-first to prevent substring collisions.
        """
        if not self._available or not text:
            return text

        detected = self.scan(text)
        if not detected:
            return text

        # Deduplicate secrets while retaining type
        unique_secrets: Set[str] = set()
        for stype, sval in detected:
            # Ignore common code identifiers and falsy strings
            if sval in ("true", "false", "null", "undefined", "None", "True", "False"):
                continue
            unique_secrets.add(sval)

        # Sort longest first to avoid partial string replacements
        sorted_secrets = sorted(unique_secrets, key=len, reverse=True)

        for secret_val in sorted_secrets:
            if secret_val in text:
                if next_placeholder_fn:
                    ph = next_placeholder_fn(manifest, placeholder_prefix, secret_val)
                else:
                    # Fallback internal placeholder generation
                    ph = None
                    for existing_ph, orig in manifest.items():
                        if orig == secret_val:
                            ph = existing_ph
                            break
                    if not ph:
                        idx = sum(1 for k in manifest if k.startswith(f"[{placeholder_prefix}_")) + 1
                        ph = f"[{placeholder_prefix}_{idx}]"
                        manifest[ph] = secret_val

                # Count redaction occurrences
                occ = text.count(secret_val)
                counts[placeholder_prefix] = counts.get(placeholder_prefix, 0) + occ
                text = text.replace(secret_val, ph)

        return text
