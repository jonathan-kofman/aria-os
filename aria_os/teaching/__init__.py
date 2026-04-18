"""ARIA-OS Teaching Layer — proactive narration + reactive manufacturing Q&A."""

from .engine import TeachingEngine, Teaching, DifficultyLevel
from .mixin import TeachingMixin
from .user_profile import UserProfile, MistakeDetector

__all__ = [
    "TeachingEngine",
    "Teaching",
    "TeachingMixin",
    "DifficultyLevel",
    "UserProfile",
    "MistakeDetector",
]
