"""Feature flag system for ARIA-OS build profiles."""
from __future__ import annotations

import os
import functools
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ARIAFeatures:
    """Compile-time feature flags. Loaded from profile + env overrides."""
    GRASSHOPPER_BACKEND: bool = False
    BLENDER_LATTICE: bool = False
    ANSYS_SIMULATION: bool = False
    MILLFORGE_BRIDGE: bool = False
    MOCK_HARDWARE: bool = True
    WEB_SEARCH: bool = True
    SSE_STREAMING: bool = True
    DEBUG_GEOMETRY: bool = False
    OLLAMA_AGENTS: bool = True
    CLOUD_LLM_FALLBACK: bool = True


PROFILES: dict[str, dict[str, bool]] = {
    "dev": {
        "GRASSHOPPER_BACKEND": True, "BLENDER_LATTICE": True,
        "ANSYS_SIMULATION": False, "MILLFORGE_BRIDGE": False,
        "MOCK_HARDWARE": True, "WEB_SEARCH": True,
        "SSE_STREAMING": True, "DEBUG_GEOMETRY": True,
        "OLLAMA_AGENTS": True, "CLOUD_LLM_FALLBACK": True,
    },
    "demo": {
        "GRASSHOPPER_BACKEND": False, "BLENDER_LATTICE": False,
        "ANSYS_SIMULATION": False, "MILLFORGE_BRIDGE": False,
        "MOCK_HARDWARE": True, "WEB_SEARCH": True,
        "SSE_STREAMING": True, "DEBUG_GEOMETRY": False,
        "OLLAMA_AGENTS": True, "CLOUD_LLM_FALLBACK": True,
    },
    "production": {
        "GRASSHOPPER_BACKEND": True, "BLENDER_LATTICE": True,
        "ANSYS_SIMULATION": True, "MILLFORGE_BRIDGE": True,
        "MOCK_HARDWARE": False, "WEB_SEARCH": True,
        "SSE_STREAMING": True, "DEBUG_GEOMETRY": False,
        "OLLAMA_AGENTS": True, "CLOUD_LLM_FALLBACK": True,
    },
    "millforge-integration": {
        "GRASSHOPPER_BACKEND": True, "BLENDER_LATTICE": False,
        "ANSYS_SIMULATION": False, "MILLFORGE_BRIDGE": True,
        "MOCK_HARDWARE": False, "WEB_SEARCH": False,
        "SSE_STREAMING": False, "DEBUG_GEOMETRY": False,
        "OLLAMA_AGENTS": True, "CLOUD_LLM_FALLBACK": False,
    },
}

# Singleton instance
_features: ARIAFeatures | None = None


def load_features(profile: str = "") -> ARIAFeatures:
    """Load feature flags from profile + env overrides."""
    global _features

    profile = profile or os.environ.get("ARIA_PROFILE", "dev")
    base = PROFILES.get(profile, PROFILES["dev"])

    features = ARIAFeatures()
    for key, val in base.items():
        if hasattr(features, key):
            # Env override: ARIA_FEATURE_GRASSHOPPER_BACKEND=1
            env_val = os.environ.get(f"ARIA_FEATURE_{key}")
            if env_val is not None:
                setattr(features, key, env_val.lower() in ("1", "true", "yes"))
            else:
                setattr(features, key, val)

    _features = features
    return features


def get_features() -> ARIAFeatures:
    """Get current feature flags (auto-loads dev profile if not initialized)."""
    global _features
    if _features is None:
        _features = load_features()
    return _features


def requires_feature(feature_name: str):
    """Decorator that skips the function if the feature is disabled."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            features = get_features()
            if not getattr(features, feature_name, False):
                print(f"  [SKIP] {fn.__name__}: {feature_name} disabled")
                return None
            return fn(*args, **kwargs)

        @functools.wraps(fn)
        async def async_wrapper(*args, **kwargs):
            features = get_features()
            if not getattr(features, feature_name, False):
                print(f"  [SKIP] {fn.__name__}: {feature_name} disabled")
                return None
            return await fn(*args, **kwargs)

        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return async_wrapper
        return wrapper
    return decorator


def print_features() -> None:
    """Log active features at startup."""
    f = get_features()
    print("  ARIA-OS Feature Flags:")
    for key in sorted(vars(f)):
        if key.startswith("_"):
            continue
        val = getattr(f, key)
        tag = "[ON] " if val else "[OFF]"
        print(f"    {tag} {key}")
