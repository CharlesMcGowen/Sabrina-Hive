"""Siloed direct checkpoint-programming package.

Only :mod:`tools.weight_space.core` owns tensor-edit semantics. Other Sabrina
modules may submit bounded requests through its public entry points, but they
must not duplicate checkpoint mutation or activation logic.
"""

from .core import main, run_remote

__all__ = ["main", "run_remote"]
