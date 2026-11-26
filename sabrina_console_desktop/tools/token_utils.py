import functools
from typing import Optional

_ENCODING_CACHE = {}

def get_encoding(name: str = "cl100k_base"):
    """
    Get and cache a tiktoken encoding instance.
    Returns None if tiktoken is not available.
    """
    if name in _ENCODING_CACHE:
        return _ENCODING_CACHE[name]
    try:
        import tiktoken
        enc = tiktoken.get_encoding(name)
        _ENCODING_CACHE[name] = enc
        return enc
    except Exception:
        return None

def count_tokens(text: str, encoding_name: str = "cl100k_base") -> int:
    """
    Return exact token count using tiktoken (cl100k_base by default).
    Gracefully falls back to a 4-chars-per-token heuristic if tiktoken fails.
    """
    if not text:
        return 0
    enc = get_encoding(encoding_name)
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    # Fallback heuristic: roughly 4 characters per token
    return max(1, len(text) // 4)

def truncate_to_tokens(
    text: str,
    max_tokens: int,
    encoding_name: str = "cl100k_base",
    suffix: str = "\n...[TRUNCATED]"
) -> str:
    """
    Safely truncate a string so that its token count does not exceed max_tokens.
    Appends `suffix` if truncated.
    """
    if not text or max_tokens <= 0:
        return ""

    enc = get_encoding(encoding_name)
    if enc is not None:
        try:
            tokens = enc.encode(text, disallowed_special=())
            if len(tokens) <= max_tokens:
                return text
            
            suffix_tokens = len(enc.encode(suffix, disallowed_special=())) if suffix else 0
            allowed_tokens = max(1, max_tokens - suffix_tokens)
            truncated_tokens = tokens[:allowed_tokens]
            decoded = enc.decode(truncated_tokens, errors="replace")
            return decoded + (suffix if suffix else "")
        except Exception:
            pass

    # Fallback heuristic if tiktoken fails
    suffix_tokens = max(1, len(suffix) // 4) if suffix else 0
    allowed_chars = max(4, (max_tokens - suffix_tokens) * 4)
    if len(text) <= max_tokens * 4:
        return text
    return text[:allowed_chars] + (suffix if suffix else "")
