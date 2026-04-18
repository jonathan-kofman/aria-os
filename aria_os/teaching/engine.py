"""
DEPRECATION SHIM
================
TeachingEngine has moved to `manufacturing_core.agents.engine`.

This module is kept as a re-export so existing ariaOS imports keep working.
The shared engine knows how to call ariaOS's llm_client when running inside
ariaOS — the import order is set up so that fallback works seamlessly.
"""
from manufacturing_core.agents.engine import (
    TeachingEngine,
    Teaching,
    DifficultyLevel,
)

__all__ = ["TeachingEngine", "Teaching", "DifficultyLevel"]
