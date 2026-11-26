"""Isolated Sabrina Social runtime.

This package intentionally does not import the cyber/pentest loop.
"""

from .runtime import SocialRuntime

__all__ = ["SocialRuntime"]
