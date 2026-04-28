"""materials.py — single source of truth for FEA material lookup.

Three tiers consume material strings differently:
    1. SW Simulation — uses the SOLIDWORKS material library by visible name
       (e.g. "AISI 1018", "Aluminum 6061-T6"). Wrong name → silent fallback
       to "Plain Carbon Steel".
    2. CalculiX (calculix_stage.MATERIAL_PROPS) — uses snake_case keys like
       "aluminum_6061", "steel_1018", "titanium_gr5".
    3. Closed-form (verification.fea_gate._MATERIAL_YIELD_MPA) — uses keys
       like "al_6061_t6", "steel_4140", "ti_6al_4v".

This module exposes a single `resolve(material)` that takes any user
spelling and returns a `MaterialResolution` with all three canonical
keys, plus the underlying physical properties. Use this instead of
the per-tier lookup tables when wiring loads through `auto_fea`.

Adding a new material: append one row to `_REGISTRY`. The aliases list
captures every spelling the regression suite has seen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class MaterialResolution:
    canonical: str           # snake_case, our default
    sw_name: str             # SOLIDWORKS library display name
    ccx_key: str             # key in calculix_stage.MATERIAL_PROPS
    cf_key: str              # key in verification.fea_gate._MATERIAL_YIELD_MPA
    yield_mpa: float
    e_gpa: float
    nu: float
    density_kg_m3: float
    aliases: tuple = field(default_factory=tuple)


_REGISTRY: list[MaterialResolution] = [
    MaterialResolution(
        canonical="aluminum_6061",
        sw_name="6061-T6 Alloy",
        ccx_key="aluminum_6061",
        cf_key="al_6061_t6",
        yield_mpa=276, e_gpa=68.9, nu=0.33, density_kg_m3=2700,
        aliases=("6061", "6061t6", "6061-t6", "6061_t6", "al_6061",
                 "al6061", "aluminum 6061", "aluminium_6061",
                 "aluminum6061", "aluminum-6061-t6", "alu_6061")),
    MaterialResolution(
        canonical="aluminum_7075",
        sw_name="7075-T6 Alloy",
        ccx_key="aluminum_7075",
        cf_key="al_7075_t6",
        yield_mpa=503, e_gpa=71.7, nu=0.33, density_kg_m3=2810,
        aliases=("7075", "7075t6", "7075-t6", "al_7075", "al7075",
                 "aluminium_7075", "aluminum 7075")),
    MaterialResolution(
        canonical="aluminum_5052",
        sw_name="5052-O Alloy",
        ccx_key="aluminum_5052",
        cf_key="al_5052",
        yield_mpa=193, e_gpa=70.3, nu=0.33, density_kg_m3=2680,
        aliases=("5052", "al_5052", "aluminium_5052", "al5052")),
    MaterialResolution(
        canonical="steel_1018",
        sw_name="AISI 1020 Steel",   # SW lacks 1018; 1020 is closest
        ccx_key="steel_1018",
        cf_key="steel_1018",
        yield_mpa=370, e_gpa=200, nu=0.29, density_kg_m3=7870,
        aliases=("1018", "1020", "mild_steel", "mildsteel", "a36",
                 "carbon_steel", "steel", "low_carbon_steel",
                 "plain_carbon_steel")),
    MaterialResolution(
        canonical="steel_4140",
        sw_name="AISI 4140 Steel, normalized",
        ccx_key="steel_4140",
        cf_key="steel_4140",
        yield_mpa=655, e_gpa=205, nu=0.29, density_kg_m3=7850,
        aliases=("4140", "4140_normalized", "alloy_steel",
                 "alloysteel", "chromoly")),
    MaterialResolution(
        canonical="stainless_304",
        sw_name="AISI 304",
        ccx_key="stainless_304",
        cf_key="stainless_304",
        yield_mpa=215, e_gpa=193, nu=0.29, density_kg_m3=8000,
        aliases=("304", "ss304", "ss_304", "ss-304", "304ss",
                 "18_8_stainless", "18-8")),
    MaterialResolution(
        canonical="stainless_316",
        sw_name="AISI 316",
        ccx_key="stainless_316",
        cf_key="stainless_316",
        yield_mpa=205, e_gpa=193, nu=0.29, density_kg_m3=8000,
        aliases=("316", "ss316", "ss_316", "ss-316", "316ss",
                 "marine_stainless", "316l")),
    MaterialResolution(
        canonical="titanium_gr5",
        sw_name="Titanium Ti-6Al-4V",
        ccx_key="titanium_gr5",
        cf_key="ti_6al_4v",
        yield_mpa=880, e_gpa=113.8, nu=0.34, density_kg_m3=4430,
        aliases=("ti", "titanium", "ti_6al4v", "ti-6al-4v",
                 "ti6al4v", "grade5_ti", "gr5_ti", "ti_grade_5")),
    MaterialResolution(
        canonical="abs",
        sw_name="ABS",
        ccx_key="abs", cf_key="abs",
        yield_mpa=40, e_gpa=2.3, nu=0.35, density_kg_m3=1040,
        aliases=("abs", "abs_plastic")),
    MaterialResolution(
        canonical="pla",
        sw_name="PLA",
        ccx_key="pla", cf_key="pla",
        yield_mpa=50, e_gpa=3.5, nu=0.36, density_kg_m3=1250,
        aliases=("pla", "polylactic", "polylactic_acid", "pla_plastic")),
    MaterialResolution(
        canonical="petg",
        sw_name="PETG",
        ccx_key="petg", cf_key="petg",
        yield_mpa=50, e_gpa=2.1, nu=0.38, density_kg_m3=1270,
        aliases=("petg", "pet_g", "pet-g")),
    MaterialResolution(
        canonical="nylon_pa12",
        sw_name="Nylon 6/10",
        ccx_key="nylon_pa12", cf_key="nylon",
        yield_mpa=48, e_gpa=1.7, nu=0.39, density_kg_m3=1010,
        aliases=("nylon", "pa12", "nylon_12", "nylon12",
                 "polyamide", "pa")),
]


def _norm(s: str) -> str:
    return (s or "").strip().lower().replace("-", "_").replace(" ", "_")


def resolve(material: str) -> Optional[MaterialResolution]:
    """Resolve any user spelling → MaterialResolution.

    First tries exact canonical match, then exhaustive alias scan
    (longest alias matching first to avoid 'pla' shadowing 'plain').
    Returns None if nothing matches.
    """
    if not material:
        return None
    n = _norm(material)
    for m in _REGISTRY:
        if n == m.canonical or n == m.ccx_key or n == m.cf_key:
            return m
    # Exhaustive: rank aliases by length (descending) so 'plain_carbon_steel'
    # beats 'pla' when matching 'plain carbon steel'.
    matches = []
    for m in _REGISTRY:
        for a in (m.canonical, m.ccx_key, m.cf_key, *m.aliases):
            if a and a in n:
                matches.append((len(a), m))
    if matches:
        matches.sort(key=lambda x: -x[0])
        return matches[0][1]
    return None


def known_materials() -> list[str]:
    return [m.canonical for m in _REGISTRY]


def coerce_for_tier(material: str, tier: str) -> str:
    """Return the right key for a specific tier; falls back to the
    raw input on miss so existing callers don't crash.

    tier in {"sw", "ccx", "cf"}.
    """
    r = resolve(material)
    if r is None:
        return material
    if tier == "sw":   return r.sw_name
    if tier == "ccx":  return r.ccx_key
    if tier == "cf":   return r.cf_key
    return r.canonical


__all__ = ["MaterialResolution", "resolve", "known_materials",
           "coerce_for_tier"]
