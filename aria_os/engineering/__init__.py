"""Centralized engineering knowledge for ARIA planners.

One canonical place for the ISO / ASTM / ASME / DIN standards every
planner and LLM prompt references. No more split-brain between
Python lookups and LLM prompt text.

Modules:
    iso_273    — metric clearance / tap drill / counterbore sizes
    astm_mat   — material grades + properties (yield, UTS, density)
    iso_2768   — general tolerance classes (f, m, c, v)
    iso_1302   — surface finish roughness grades
    iso_1101   — GD&T symbols + their intended use
    asme_b165  — ASME B16.5 pipe flange dimensions per class
"""
from .iso_273 import (
    clearance_hole_mm, tap_drill_mm, counterbore_mm,
    parse_bolt_spec, resolve_bolt_hole,
)
from .astm_mat import (
    material_properties, resolve_material,
    CNC_WALL_MIN_MM, FDM_WALL_MIN_MM,
)
from .iso_2768 import tolerance_band_mm, DEFAULT_CLASS
from .iso_1302 import surface_finish_default, Ra_BY_PROCESS
from .iso_1101 import GDT_SYMBOLS, gdt_callout_for_feature

__all__ = [
    # ISO 273
    "clearance_hole_mm", "tap_drill_mm", "counterbore_mm",
    "parse_bolt_spec", "resolve_bolt_hole",
    # ASTM materials
    "material_properties", "resolve_material",
    "CNC_WALL_MIN_MM", "FDM_WALL_MIN_MM",
    # Tolerances
    "tolerance_band_mm", "DEFAULT_CLASS",
    # Surface finish
    "surface_finish_default", "Ra_BY_PROCESS",
    # GD&T
    "GDT_SYMBOLS", "gdt_callout_for_feature",
]
