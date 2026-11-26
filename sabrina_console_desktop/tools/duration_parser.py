"""Robust human and ISO-8601 duration parser for Sabrina workflows.

Supports compound units (1w, 2d, 3h, 4m, 5s, 500ms, 2.5h) and ISO-8601 strings (PT1H30M).
Strictly validates boundaries and raises ValueError on negative or invalid durations.
"""
from __future__ import annotations

import re
from typing import Union

_HUMAN_UNIT_SECONDS = {
    "w": 7 * 86400.0,
    "week": 7 * 86400.0,
    "weeks": 7 * 86400.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "ms": 0.001,
    "millisecond": 0.001,
    "milliseconds": 0.001,
}

_ISO_8601_RE = re.compile(
    r"^P(?:(?P<weeks>[0-9]+(?:\.[0-9]+)?)W)?(?:(?P<days>[0-9]+(?:\.[0-9]+)?)D)?"
    r"(?:T(?:(?P<hours>[0-9]+(?:\.[0-9]+)?)H)?(?:(?P<minutes>[0-9]+(?:\.[0-9]+)?)M)?"
    r"(?:(?P<seconds>[0-9]+(?:\.[0-9]+)?)S)?)?$",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(
    r"(?P<val>[0-9]+(?:\.[0-9]+)?)\s*(?P<unit>[A-Za-z]+)",
    re.IGNORECASE,
)


def parse_duration_seconds(duration: Union[str, int, float]) -> float:
    """Parse a human or ISO-8601 duration string into fractional seconds.

    Args:
        duration: Duration expression (e.g. '1h 30m', '45s', 'PT1H30M', 120).

    Returns:
        float: Equivalent seconds.

    Raises:
        ValueError: If input is empty, malformed, negative, or NaN.
    """
    if duration is None:
        raise ValueError("Duration cannot be None")

    if isinstance(duration, (int, float)):
        import math
        val = float(duration)
        if math.isnan(val):
            raise ValueError("Duration cannot be NaN")
        if val < 0:
            raise ValueError(f"Duration cannot be negative: {val}")
        return val

    text = str(duration).strip()
    if not text:
        raise ValueError("Duration string cannot be empty")

    # Plain numeric string check
    try:
        val = float(text)
        import math
        if math.isnan(val):
            raise ValueError("Duration cannot be NaN")
        if val < 0:
            raise ValueError(f"Duration cannot be negative: {val}")
        return val
    except ValueError:
        if text.startswith("-"):
            raise ValueError(f"Duration cannot be negative: {text}")

    # Check ISO-8601 format
    if text.upper().startswith("P"):
        m = _ISO_8601_RE.match(text)
        if not m or text.upper() == "P" or text.upper() == "PT":
            raise ValueError(f"Malformed ISO-8601 duration: {text!r}")
        total = 0.0
        parts = m.groupdict()
        if parts.get("weeks"):
            total += float(parts["weeks"]) * 7 * 86400.0
        if parts.get("days"):
            total += float(parts["days"]) * 86400.0
        if parts.get("hours"):
            total += float(parts["hours"]) * 3600.0
        if parts.get("minutes"):
            total += float(parts["minutes"]) * 60.0
        if parts.get("seconds"):
            total += float(parts["seconds"])
        return total

    # Parse compound human format (e.g. '1d 2h 30m 15s', '2.5hr')
    tokens = list(_TOKEN_RE.finditer(text))
    if not tokens:
        raise ValueError(f"Cannot parse duration string: {text!r}")

    # Verify every non-whitespace character was matched
    matched_chars = sum(m.end() - m.start() for m in tokens)
    clean_text = "".join(text.split())
    matched_clean_chars = sum(len(m.group("val")) + len(m.group("unit")) for m in tokens)
    if matched_clean_chars != len(clean_text):
        raise ValueError(f"Unrecognized duration tokens in: {text!r}")

    total = 0.0
    for m in tokens:
        val = float(m.group("val"))
        unit = m.group("unit").lower()
        if unit not in _HUMAN_UNIT_SECONDS:
            raise ValueError(f"Unknown duration unit: {unit!r}")
        total += val * _HUMAN_UNIT_SECONDS[unit]

    return round(total, 6)
