#!/usr/bin/env python3
"""Compatibility entrypoint for the siloed :mod:`tools.weight_space.core`.

New code should import ``tools.weight_space`` or ``tools.weight_space.core``.
This module remains so deployed helpers and existing experiments keep working.
It aliases the canonical module instead of maintaining a second implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from tools.weight_space import core as _core


if __name__ == "__main__":
    raise SystemExit(_core.main())

# Preserve legacy access to private validation helpers used by existing tests and
# research consumers, while keeping one module object and one implementation.
sys.modules[__name__] = _core
