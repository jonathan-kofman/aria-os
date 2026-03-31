"""
cem_registry.py — CEM Domain Registry

Maps goal keywords and part_id prefixes to CEM module names.
All new CEM domains must be registered here.

Usage:
    from cem_registry import resolve_cem_module
    module_name = resolve_cem_module(goal, part_id)
    # Returns: "cem_aria" | "cem_lre" | "cem_clock" | None
"""
from __future__ import annotations

from typing import Optional

# ---------------------------------------------------------------------------
# Keyword → module name mapping
# Order matters: first match wins for keyword scan.
# ---------------------------------------------------------------------------

_KEYWORD_MAP: list[tuple[list[str], str]] = [
    # LRE (liquid rocket engine) keywords
    (["lre", "liquid rocket", "nozzle", "rocket", "turbopump", "injector",
      "combustion", "chamber pressure", "isp", "thrust", "propellant",
      "kerosene", "lox", "rp-1", "ipa", "meth"], "cem_lre"),
    # Mechanical clock / horology keywords
    (["clock", "skeleton clock", "pendulum", "mainspring", "gear train",
      "escapement", "horology"], "cem_clock"),
    # Civil engineering keywords
    (["road plan", "street plan", "highway plan", "drainage plan", "storm sewer",
      "grading plan", "site plan", "utility plan", "civil", "culvert",
      "retaining wall", "dxf", "autocad", "road design", "storm drain",
      "right of way", "row plan", "earthwork", "pavement design",
      "site civil", "land development"], "cem_civil"),
    # ARIA auto-belay keywords
    (["aria", "belay", "ratchet", "brake drum", "spool", "cam collar",
      "centrifugal", "clutch", "rope guide", "catch pawl", "flyweight",
      "arrest", "climbing", "auto belay", "rope"], "cem_aria"),
]

# Part-ID prefix → module name (checked before keyword scan)
_PART_ID_PREFIX_MAP: dict[str, str] = {
    "aria_":  "cem_aria",
    "lre_":   "cem_lre",
    "civil_": "cem_civil",
    "road_":  "cem_civil",
    "drain_": "cem_civil",
}


def resolve_cem_module(goal: str, part_id: str = "") -> Optional[str]:
    """
    Return the CEM module name for this goal/part_id combination.

    Returns None when no domain match is found (heuristic fallback will be used).
    """
    pid = (part_id or "").lower().strip()
    for prefix, module in _PART_ID_PREFIX_MAP.items():
        if pid.startswith(prefix):
            return module

    goal_lower = (goal or "").lower()
    for keywords, module in _KEYWORD_MAP:
        if any(kw in goal_lower for kw in keywords):
            return module

    return None


def list_registered_modules() -> list[str]:
    """Return all unique module names registered in this registry."""
    seen: list[str] = []
    for _, m in _KEYWORD_MAP:
        if m not in seen:
            seen.append(m)
    return seen
