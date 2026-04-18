"""
DEPRECATION SHIM
================
UserProfile and MistakeDetector have moved to `manufacturing_core.profile`.

This module is kept as a re-export so existing ariaOS imports keep working.
The legacy ~/.aria_os/user_profile.json is auto-migrated to the shared
~/.manufacturing-core/user_profile.json on first load.
"""
from manufacturing_core.profile.user_profile import (
    UserProfile,
    MistakeDetector,
    _classify_topic,
    _ISSUE_TIPS,
    _GENERIC_TIP,
)

__all__ = ["UserProfile", "MistakeDetector", "_classify_topic", "_ISSUE_TIPS", "_GENERIC_TIP"]
