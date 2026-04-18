"""
DEPRECATION SHIM
================
TeachingMixin has moved to `manufacturing_core.agents.mixin`.

This module is kept as a re-export so existing ariaOS imports keep working.
"""
from manufacturing_core.agents.mixin import TeachingMixin

__all__ = ["TeachingMixin"]
