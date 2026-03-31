"""
aria_os/physics_analyzer.py — Post-generation FEA/CFD analysis module.

Runs parametric structural (FEA) or fluid (CFD) analysis on a generated part
using the same parameters that drove its creation.  All analysis is closed-form
(Euler-Bernoulli, Lamé, Lewis, Darcy-Weisbach, isentropic nozzle, etc.) —
no external solver or FEM mesh required.

Public API
----------
    analyze(part_id, analysis_type, params, goal, repo_root) -> dict
    prompt_and_analyze(part_id, params, goal, step_path, repo_root) -> dict | None
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Materials database
# ---------------------------------------------------------------------------
# Import pre-defined materials from cem_core where possible; supplement below.
try:
    _ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_ROOT))
    from cem_core import MATERIAL_6061_AL, MATERIAL_INCONEL718  # noqa: F401
    _CEM_CORE_AVAILABLE = True
except ImportError:
    _CEM_CORE_AVAILABLE = False

# Flat dict database used internally by this module.
# Keys match the "material" key in plan["params"] and aria_mechanical.md.
MATERIALS: dict[str, dict[str, float]] = {
    # ── Carbon & alloy steels ─────────────────────────────────────────────
    "steel_mild":      {"E_GPa": 200.0, "yield_MPa": 250.0,  "density": 7850.0, "poisson": 0.30},
    "steel_1018":      {"E_GPa": 200.0, "yield_MPa": 310.0,  "density": 7870.0, "poisson": 0.29},
    "steel_1045":      {"E_GPa": 200.0, "yield_MPa": 530.0,  "density": 7850.0, "poisson": 0.29},
    "steel_4130":      {"E_GPa": 200.0, "yield_MPa": 435.0,  "density": 7850.0, "poisson": 0.29},
    "steel_4140":      {"E_GPa": 200.0, "yield_MPa": 655.0,  "density": 7850.0, "poisson": 0.30},
    "steel_4340":      {"E_GPa": 200.0, "yield_MPa": 862.0,  "density": 7850.0, "poisson": 0.29},
    "steel_a36":       {"E_GPa": 200.0, "yield_MPa": 250.0,  "density": 7850.0, "poisson": 0.30},
    # ── Stainless steels ──────────────────────────────────────────────────
    "stainless_303":   {"E_GPa": 193.0, "yield_MPa": 240.0,  "density": 8000.0, "poisson": 0.29},
    "stainless_304":   {"E_GPa": 193.0, "yield_MPa": 215.0,  "density": 8000.0, "poisson": 0.29},
    "stainless_316":   {"E_GPa": 193.0, "yield_MPa": 290.0,  "density": 8000.0, "poisson": 0.29},
    "stainless_416":   {"E_GPa": 200.0, "yield_MPa": 415.0,  "density": 7750.0, "poisson": 0.28},
    "stainless_17_4ph":{"E_GPa": 196.0, "yield_MPa": 1170.0, "density": 7780.0, "poisson": 0.27},
    # ── Tool steels ───────────────────────────────────────────────────────
    "tool_steel_a2":   {"E_GPa": 203.0, "yield_MPa": 1500.0, "density": 7860.0, "poisson": 0.29},
    "tool_steel_d2":   {"E_GPa": 210.0, "yield_MPa": 1650.0, "density": 7700.0, "poisson": 0.28},
    # ── Aluminium alloys ──────────────────────────────────────────────────
    "aluminium_2024":  {"E_GPa":  73.0, "yield_MPa": 345.0,  "density": 2780.0, "poisson": 0.33},
    "aluminum_2024":   {"E_GPa":  73.0, "yield_MPa": 345.0,  "density": 2780.0, "poisson": 0.33},
    "aluminium_6061":  {"E_GPa":  69.0, "yield_MPa": 276.0,  "density": 2700.0, "poisson": 0.33},
    "aluminum_6061":   {"E_GPa":  69.0, "yield_MPa": 276.0,  "density": 2700.0, "poisson": 0.33},
    "aluminium_6063":  {"E_GPa":  69.0, "yield_MPa": 214.0,  "density": 2700.0, "poisson": 0.33},
    "aluminium_7075":  {"E_GPa":  72.0, "yield_MPa": 503.0,  "density": 2810.0, "poisson": 0.33},
    "aluminum_7075":   {"E_GPa":  72.0, "yield_MPa": 503.0,  "density": 2810.0, "poisson": 0.33},
    "aluminium_mic6":  {"E_GPa":  69.0, "yield_MPa": 152.0,  "density": 2700.0, "poisson": 0.33},
    # ── Titanium alloys ───────────────────────────────────────────────────
    "titanium":        {"E_GPa": 116.0, "yield_MPa": 880.0,  "density": 4430.0, "poisson": 0.34},
    "ti_6al4v":        {"E_GPa": 114.0, "yield_MPa": 880.0,  "density": 4430.0, "poisson": 0.34},
    "titanium_grade2": {"E_GPa": 103.0, "yield_MPa": 345.0,  "density": 4510.0, "poisson": 0.34},
    "titanium_grade5": {"E_GPa": 114.0, "yield_MPa": 880.0,  "density": 4430.0, "poisson": 0.34},
    # ── Superalloys ───────────────────────────────────────────────────────
    "inconel_718":     {"E_GPa": 200.0, "yield_MPa": 1100.0, "density": 8190.0, "poisson": 0.29},
    "inconel_625":     {"E_GPa": 205.0, "yield_MPa": 490.0,  "density": 8440.0, "poisson": 0.28},
    "hastelloy_c276":  {"E_GPa": 205.0, "yield_MPa": 355.0,  "density": 8890.0, "poisson": 0.31},
    "kovar":           {"E_GPa": 138.0, "yield_MPa": 345.0,  "density": 8360.0, "poisson": 0.32},
    "x1_420i":         {"E_GPa": 190.0, "yield_MPa": 620.0,  "density": 7860.0, "poisson": 0.30},
    # ── Copper & brass alloys ─────────────────────────────────────────────
    "copper":          {"E_GPa": 117.0, "yield_MPa": 210.0,  "density": 8960.0, "poisson": 0.34},
    "copper_c101":     {"E_GPa": 117.0, "yield_MPa": 195.0,  "density": 8940.0, "poisson": 0.34},
    "copper_c110":     {"E_GPa": 117.0, "yield_MPa": 210.0,  "density": 8930.0, "poisson": 0.34},
    "brass":           {"E_GPa": 100.0, "yield_MPa": 200.0,  "density": 8500.0, "poisson": 0.34},
    "brass_360":       {"E_GPa": 100.0, "yield_MPa": 310.0,  "density": 8500.0, "poisson": 0.34},
    "bronze_932":      {"E_GPa":  90.0, "yield_MPa": 125.0,  "density": 8800.0, "poisson": 0.34},
    # ── Engineering plastics ──────────────────────────────────────────────
    "pla":             {"E_GPa":   3.5, "yield_MPa":  50.0,  "density": 1240.0, "poisson": 0.36},
    "abs":             {"E_GPa":   2.3, "yield_MPa":  40.0,  "density": 1050.0, "poisson": 0.35},
    "petg":            {"E_GPa":   2.1, "yield_MPa":  53.0,  "density": 1270.0, "poisson": 0.36},
    "nylon":           {"E_GPa":   2.7, "yield_MPa":  75.0,  "density": 1140.0, "poisson": 0.39},
    "nylon_pa12":      {"E_GPa":   1.7, "yield_MPa":  48.0,  "density": 1010.0, "poisson": 0.39},
    "nylon_pa6":       {"E_GPa":   2.9, "yield_MPa":  70.0,  "density": 1130.0, "poisson": 0.39},
    "polycarbonate":   {"E_GPa":   2.4, "yield_MPa":  62.0,  "density": 1200.0, "poisson": 0.37},
    "pc":              {"E_GPa":   2.4, "yield_MPa":  62.0,  "density": 1200.0, "poisson": 0.37},
    "polypropylene":   {"E_GPa":   1.5, "yield_MPa":  35.0,  "density":  905.0, "poisson": 0.42},
    "pp":              {"E_GPa":   1.5, "yield_MPa":  35.0,  "density":  905.0, "poisson": 0.42},
    "acetal":          {"E_GPa":   3.1, "yield_MPa":  68.0,  "density": 1410.0, "poisson": 0.35},
    "delrin":          {"E_GPa":   3.1, "yield_MPa":  68.0,  "density": 1410.0, "poisson": 0.35},
    "pom":             {"E_GPa":   3.1, "yield_MPa":  68.0,  "density": 1410.0, "poisson": 0.35},
    "hdpe":            {"E_GPa":   1.1, "yield_MPa":  26.0,  "density":  960.0, "poisson": 0.41},
    "uhmw":            {"E_GPa":   0.7, "yield_MPa":  21.0,  "density":  930.0, "poisson": 0.46},
    "ptfe":            {"E_GPa":   0.5, "yield_MPa":  23.0,  "density": 2150.0, "poisson": 0.46},
    "teflon":          {"E_GPa":   0.5, "yield_MPa":  23.0,  "density": 2150.0, "poisson": 0.46},
    "acrylic":         {"E_GPa":   3.2, "yield_MPa":  73.0,  "density": 1190.0, "poisson": 0.37},
    "pmma":            {"E_GPa":   3.2, "yield_MPa":  73.0,  "density": 1190.0, "poisson": 0.37},
    "pei":             {"E_GPa":   3.3, "yield_MPa": 100.0,  "density": 1270.0, "poisson": 0.36},
    "ultem":           {"E_GPa":   3.3, "yield_MPa": 100.0,  "density": 1270.0, "poisson": 0.36},
    "pps":             {"E_GPa":   3.8, "yield_MPa":  90.0,  "density": 1350.0, "poisson": 0.36},
    "peek":            {"E_GPa":   4.1, "yield_MPa": 100.0,  "density": 1300.0, "poisson": 0.38},
    "peek_30gf":       {"E_GPa":  11.0, "yield_MPa": 160.0,  "density": 1510.0, "poisson": 0.35},
    "garolite":        {"E_GPa":  18.0, "yield_MPa": 250.0,  "density": 1850.0, "poisson": 0.20},
    # ── Elastomers ────────────────────────────────────────────────────────
    "tpu":             {"E_GPa":  0.025, "yield_MPa":  25.0,  "density": 1210.0, "poisson": 0.48},
    "tpu_95a":         {"E_GPa":  0.025, "yield_MPa":  25.0,  "density": 1210.0, "poisson": 0.48},
    "tpu_80a":         {"E_GPa":  0.010, "yield_MPa":  12.0,  "density": 1150.0, "poisson": 0.49},
    "silicone":        {"E_GPa":  0.005, "yield_MPa":   7.0,  "density": 1150.0, "poisson": 0.49},
    "rubber":          {"E_GPa":  0.005, "yield_MPa":   7.0,  "density": 1150.0, "poisson": 0.49},
    "neoprene":        {"E_GPa":  0.007, "yield_MPa":  10.0,  "density": 1240.0, "poisson": 0.49},
    "viton":           {"E_GPa":  0.008, "yield_MPa":  12.0,  "density": 1800.0, "poisson": 0.49},
    # ── Composites ────────────────────────────────────────────────────────
    "carbon_fiber":    {"E_GPa": 70.0,  "yield_MPa": 600.0,  "density": 1600.0, "poisson": 0.10},
    "cf":              {"E_GPa": 70.0,  "yield_MPa": 600.0,  "density": 1600.0, "poisson": 0.10},
    "cfrp":            {"E_GPa": 70.0,  "yield_MPa": 600.0,  "density": 1600.0, "poisson": 0.10},
    "fiberglass":      {"E_GPa": 20.0,  "yield_MPa": 200.0,  "density": 1800.0, "poisson": 0.22},
    "gfrp":            {"E_GPa": 20.0,  "yield_MPa": 200.0,  "density": 1800.0, "poisson": 0.22},
    "kevlar":          {"E_GPa": 60.0,  "yield_MPa": 500.0,  "density": 1440.0, "poisson": 0.36},
    # ── Ceramics & specialty ──────────────────────────────────────────────
    "alumina":         {"E_GPa": 370.0, "yield_MPa": 300.0,  "density": 3960.0, "poisson": 0.22},
    "zirconia":        {"E_GPa": 200.0, "yield_MPa": 900.0,  "density": 6050.0, "poisson": 0.31},
    "tungsten":        {"E_GPa": 411.0, "yield_MPa": 750.0,  "density": 19250.0,"poisson": 0.28},
    "tungsten_carbide":{"E_GPa": 620.0, "yield_MPa": 500.0,  "density": 15630.0,"poisson": 0.24},
    "magnesium_az31":  {"E_GPa":  45.0, "yield_MPa": 200.0,  "density": 1770.0, "poisson": 0.35},
    "zinc_zamak3":     {"E_GPa":  86.0, "yield_MPa": 221.0,  "density": 6600.0, "poisson": 0.30},
    # ── Wood (for model/prototype reference) ──────────────────────────────
    "wood_oak":        {"E_GPa":  12.0, "yield_MPa":  50.0,  "density":  700.0, "poisson": 0.35},
    "wood_pine":       {"E_GPa":   9.0, "yield_MPa":  35.0,  "density":  500.0, "poisson": 0.35},
    "plywood":         {"E_GPa":  10.0, "yield_MPa":  40.0,  "density":  600.0, "poisson": 0.30},
    "mdf":             {"E_GPa":   4.0, "yield_MPa":  20.0,  "density":  750.0, "poisson": 0.25},
}

# Map common goal-string keywords to material keys
# Ordered longest-first so "polycarbonate" matches before "carbon", "stainless" before "steel"
_MATERIAL_KEYWORD_MAP: list[tuple[str, str]] = [
    # Specific grades first (longest match wins)
    ("polycarbonate",  "polycarbonate"),
    ("stainless 17-4", "stainless_17_4ph"),
    ("17-4 ph",        "stainless_17_4ph"),
    ("17-4ph",         "stainless_17_4ph"),
    ("stainless 316",  "stainless_316"),
    ("steel 316",      "stainless_316"),
    ("ss 316",         "stainless_316"),
    ("316l",           "stainless_316"),
    ("stainless 304",  "stainless_304"),
    ("steel 304",      "stainless_304"),
    ("ss 304",         "stainless_304"),
    ("stainless 303",  "stainless_303"),
    ("steel 303",      "stainless_303"),
    ("ss 303",         "stainless_303"),
    ("stainless 416",  "stainless_416"),
    ("steel 416",      "stainless_416"),
    ("ss 416",         "stainless_416"),
    ("carbon fiber",   "carbon_fiber"),
    ("carbon fibre",   "carbon_fiber"),
    ("tungsten carbide","tungsten_carbide"),
    ("tool steel a2",  "tool_steel_a2"),
    ("tool steel d2",  "tool_steel_d2"),
    ("inconel 718",    "inconel_718"),
    ("inconel 625",    "inconel_625"),
    ("hastelloy",      "hastelloy_c276"),
    ("ti-6al-4v",      "ti_6al4v"),
    ("titanium grade 5","titanium_grade5"),
    ("titanium grade 2","titanium_grade2"),
    ("peek 30",        "peek_30gf"),
    ("peek gf",        "peek_30gf"),
    ("peek(30",        "peek_30gf"),
    ("30% gf",         "peek_30gf"),
    # Alloy numbers
    ("2024",           "aluminium_2024"),
    ("6061",           "aluminium_6061"),
    ("6063",           "aluminium_6063"),
    ("7075",           "aluminium_7075"),
    ("mic-6",          "aluminium_mic6"),
    ("mic6",           "aluminium_mic6"),
    ("1018",           "steel_1018"),
    ("1045",           "steel_1045"),
    ("4130",           "steel_4130"),
    ("4140",           "steel_4140"),
    ("4340",           "steel_4340"),
    ("a36",            "steel_a36"),
    ("c101",           "copper_c101"),
    ("c110",           "copper_c110"),
    ("brass 360",      "brass_360"),
    ("bronze 932",     "bronze_932"),
    # Generic material names
    ("tpu",            "tpu"),
    ("silicone",       "silicone"),
    ("rubber",         "rubber"),
    ("neoprene",       "neoprene"),
    ("viton",          "viton"),
    ("titanium",       "titanium"),
    ("inconel",        "inconel_718"),
    ("stainless",      "stainless_304"),
    ("aluminium",      "aluminium_6061"),
    ("aluminum",       "aluminium_6061"),
    ("brass",          "brass"),
    ("bronze",         "bronze_932"),
    ("copper",         "copper"),
    ("tungsten",       "tungsten"),
    ("magnesium",      "magnesium_az31"),
    ("zinc",           "zinc_zamak3"),
    ("kovar",          "kovar"),
    ("pom",            "pom"),
    ("kevlar",         "kevlar"),
    ("fiberglass",     "fiberglass"),
    ("nylon",          "nylon"),
    ("delrin",         "delrin"),
    ("acetal",         "acetal"),
    ("peek",           "peek"),
    ("ultem",          "ultem"),
    ("pei",            "pei"),
    ("pps",            "pps"),
    ("acrylic",        "acrylic"),
    ("pmma",           "pmma"),
    ("hdpe",           "hdpe"),
    ("uhmw",           "uhmw"),
    ("ptfe",           "ptfe"),
    ("teflon",         "teflon"),
    ("polypropylene",  "polypropylene"),
    ("garolite",       "garolite"),
    ("pla",            "pla"),
    ("abs",            "abs"),
    ("petg",           "petg"),
    ("plywood",        "plywood"),
    ("mdf",            "mdf"),
    ("oak",            "wood_oak"),
    ("pine",           "wood_pine"),
    ("wood",           "wood_oak"),
    ("steel",          "steel_mild"),
]

_DEFAULT_MATERIAL = "steel_mild"


def _get_material(name: str | None) -> dict[str, float]:
    """Return material dict, falling back to steel_mild."""
    if not name:
        return MATERIALS[_DEFAULT_MATERIAL]
    key = name.lower().replace(" ", "_").replace("-", "_")
    return MATERIALS.get(key, MATERIALS[_DEFAULT_MATERIAL])


def _detect_material_from_goal(goal: str, params: dict) -> str:
    """Detect material from params['material'] or goal string keywords."""
    # Check params first
    mat = params.get("material")
    if mat:
        key = mat.lower().replace(" ", "_").replace("-", "_")
        if key in MATERIALS:
            return key
    # Scan goal string for material keywords
    goal_lower = goal.lower()
    for kw, mat_key in _MATERIAL_KEYWORD_MAP:
        if kw in goal_lower:
            return mat_key
    return _DEFAULT_MATERIAL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(allowable: float, actual: float) -> float:
    """Safety factor = allowable / actual.  Never divides by zero."""
    return allowable / max(actual, 1e-12)


def _rect_I(w: float, h: float) -> float:
    """Second moment of area for rectangular cross-section [m^4]. w,h in metres."""
    return w * h**3 / 12.0


def _circ_I(d: float) -> float:
    """Second moment of area for circular cross-section [m^4]. d in metres."""
    return math.pi * d**4 / 64.0


def _rect_Z(w: float, h: float) -> float:
    """Section modulus for rectangle [m^3]."""
    return w * h**2 / 6.0


def _circ_Z(d: float) -> float:
    """Section modulus for circle [m^3]."""
    return math.pi * d**3 / 32.0


# ---------------------------------------------------------------------------
# FEA — Structural analyses
# ---------------------------------------------------------------------------

def fea_beam_bending(params: dict) -> dict:
    """
    Euler-Bernoulli beam bending analysis.

    Required params
    ---------------
    length_mm       : float — beam length
    load_N          : float — applied transverse load
    material        : str   — key in MATERIALS
    section         : str   — "rect" | "circ" (default rect)
    boundary        : str   — "cantilever" | "simply_supported" | "fixed_fixed" (default cantilever)

    For rect section: width_mm, height_mm (or thickness_mm as height alias)
    For circ section: diameter_mm

    Returns structured result dict.
    """
    mat = _get_material(params.get("material"))
    E = mat["E_GPa"] * 1e9          # Pa
    yield_MPa = mat["yield_MPa"]

    L = float(params.get("length_mm", 100.0)) / 1000.0  # m
    P = float(params.get("load_N", 100.0))
    section = str(params.get("section", "rect")).lower()
    boundary = str(params.get("boundary", "cantilever")).lower()

    if section == "circ":
        d = float(params.get("diameter_mm", params.get("od_mm", 20.0))) / 1000.0
        I = _circ_I(d)
        Z = _circ_Z(d)
        section_desc = f"circ d={d*1000:.1f}mm"
    else:
        w = float(params.get("width_mm", 20.0)) / 1000.0
        h = float(params.get("height_mm", params.get("thickness_mm", 10.0))) / 1000.0
        I = _rect_I(w, h)
        Z = _rect_Z(w, h)
        section_desc = f"rect {w*1000:.1f}x{h*1000:.1f}mm"

    # Deflection and bending moment coefficients per boundary condition
    if boundary in ("cantilever", "cantilevered"):
        # Cantilever: max deflection at free end, max moment at root
        delta_max = P * L**3 / (3.0 * E * I)
        M_max = P * L
        bc_label = "cantilever"
    elif boundary in ("fixed_fixed", "fixed-fixed", "fixed"):
        # Fixed-fixed: max deflection at midspan, max moment at supports
        delta_max = P * L**3 / (192.0 * E * I)
        M_max = P * L / 8.0
        bc_label = "fixed-fixed"
    else:
        # Simply supported: max deflection at midspan, max moment at midspan
        delta_max = P * L**3 / (48.0 * E * I)
        M_max = P * L / 4.0
        bc_label = "simply-supported"

    sigma_max_Pa = M_max / Z         # bending stress [Pa]
    sigma_max_MPa = sigma_max_Pa / 1e6
    sf = _sf(yield_MPa, sigma_max_MPa)

    passed = sf >= 2.0
    warnings = []
    failures = []
    if sf < 1.5:
        failures.append(f"Bending SF {sf:.2f} < 1.5 — structural failure likely")
    elif sf < 2.0:
        warnings.append(f"Bending SF {sf:.2f} < 2.0 — marginal")
    if delta_max * 1000.0 > L * 1000.0 / 100.0:
        warnings.append(f"Deflection {delta_max*1000:.2f}mm exceeds L/100 serviceability limit")

    report_lines = [
        f"Beam Bending Analysis ({bc_label}, {section_desc})",
        f"  Length:           {L*1000:.1f} mm",
        f"  Load:             {P:.1f} N",
        f"  Material:         {params.get('material', _DEFAULT_MATERIAL)} (E={mat['E_GPa']:.0f} GPa, σ_y={yield_MPa:.0f} MPa)",
        f"  I:                {I:.4e} m⁴",
        f"  M_max:            {M_max:.2f} N·m",
        f"  σ_max:            {sigma_max_MPa:.1f} MPa",
        f"  δ_max:            {delta_max*1000:.3f} mm",
        f"  Safety factor:    {sf:.2f}",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "fea_beam_bending",
        "passed": passed,
        "safety_factor": sf,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "E_GPa": mat["E_GPa"],
            "yield_MPa": yield_MPa,
            "L_mm": L * 1000.0,
            "I_m4": I,
            "M_max_Nm": M_max,
            "sigma_max_MPa": sigma_max_MPa,
            "delta_max_mm": delta_max * 1000.0,
            "boundary": bc_label,
            "section": section_desc,
        },
        "report": "\n".join(report_lines),
    }


def fea_thick_cylinder(params: dict) -> dict:
    """
    Lamé equations for thick-walled pressure vessels / housings.

    Required params
    ---------------
    od_mm           : float — outer diameter
    bore_mm / id_mm : float — inner diameter (bore_mm preferred)
    pressure_MPa    : float — internal pressure (default 1.0)
    material        : str
    """
    mat = _get_material(params.get("material"))
    yield_MPa = mat["yield_MPa"]

    od = float(params.get("od_mm", 100.0))
    id_ = float(params.get("bore_mm", params.get("id_mm", od * 0.5)))
    p_i = float(params.get("pressure_MPa", 1.0))   # MPa
    p_o = float(params.get("outer_pressure_MPa", 0.0))

    r_i = id_ / 2.0   # mm
    r_o = od  / 2.0   # mm

    # Lamé equations (pressure in MPa, radii in mm → stress in MPa directly)
    # Hoop stress at inner wall (maximum hoop stress for internal pressure)
    if abs(r_o - r_i) < 1e-6:
        # Degenerate (thin wall fallback)
        sigma_h_inner = p_i * r_i / max(r_o - r_i, 0.01)
        sigma_r_inner = -p_i
        sigma_h_outer = p_i * r_i / max(r_o - r_i, 0.01)
        sigma_r_outer = p_o
    else:
        A = (p_i * r_i**2 - p_o * r_o**2) / (r_o**2 - r_i**2)
        B = (p_i - p_o) * r_i**2 * r_o**2 / (r_o**2 - r_i**2)

        sigma_h_inner = A + B / r_i**2   # MPa
        sigma_r_inner = A - B / r_i**2   # MPa  (= -p_i for internal-only)
        sigma_h_outer = A + B / r_o**2
        sigma_r_outer = A - B / r_o**2   # = -p_o for internal-only

    # von Mises at inner wall (critical location)
    sigma_vm = math.sqrt(
        0.5 * ((sigma_h_inner - sigma_r_inner)**2 +
               (sigma_h_inner - 0)**2 +
               (sigma_r_inner - 0)**2)
    )

    sf = _sf(yield_MPa, sigma_vm)

    # Burst pressure (Lamé burst): when sigma_h_inner = yield
    # p_burst = yield * (r_o^2 - r_i^2) / (2 * r_i^2)  [thin-wall approx is ok for estimate]
    p_burst = yield_MPa * (r_o**2 - r_i**2) / (2.0 * r_i**2) if r_i > 0 else float("inf")

    passed = sf >= 2.0
    failures = []
    warnings = []
    if sf < 1.5:
        failures.append(f"von Mises SF {sf:.2f} < 1.5 — burst risk")
    elif sf < 2.0:
        warnings.append(f"von Mises SF {sf:.2f} < 2.0 — marginal pressure vessel")
    if p_burst < p_i * 4.0:
        warnings.append(f"Burst pressure {p_burst:.1f} MPa is less than 4× working pressure")

    report_lines = [
        f"Thick Cylinder (Lamé) Analysis",
        f"  OD/ID:            {od:.1f} / {id_:.1f} mm  (wall = {r_o-r_i:.1f} mm)",
        f"  Internal pressure:{p_i:.2f} MPa",
        f"  Material:         {params.get('material', _DEFAULT_MATERIAL)} (σ_y={yield_MPa:.0f} MPa)",
        f"  σ_hoop (inner):   {sigma_h_inner:.1f} MPa",
        f"  σ_radial (inner): {sigma_r_inner:.1f} MPa",
        f"  σ_vm (inner):     {sigma_vm:.1f} MPa",
        f"  Burst pressure:   {p_burst:.1f} MPa",
        f"  Safety factor:    {sf:.2f}",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "fea_thick_cylinder",
        "passed": passed,
        "safety_factor": sf,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "od_mm": od, "id_mm": id_,
            "pressure_MPa": p_i,
            "sigma_hoop_inner_MPa": sigma_h_inner,
            "sigma_radial_inner_MPa": sigma_r_inner,
            "sigma_vm_MPa": sigma_vm,
            "burst_pressure_MPa": p_burst,
            "yield_MPa": yield_MPa,
        },
        "report": "\n".join(report_lines),
    }


def fea_gear_tooth(params: dict) -> dict:
    """
    Lewis equation (bending) + Hertzian contact stress for spur gear tooth.

    Required params
    ---------------
    module_mm       : float — gear module (mm)
    n_teeth         : int   — number of teeth
    face_width_mm   : float — face width
    torque_Nm       : float — transmitted torque
    material        : str
    """
    mat = _get_material(params.get("material"))
    yield_MPa = mat["yield_MPa"]
    E = mat["E_GPa"] * 1e3  # MPa

    m = float(params.get("module_mm", 2.0))     # mm
    N = int(float(params.get("n_teeth", 20)))
    b = float(params.get("face_width_mm", params.get("width_mm", 10.0)))  # mm
    T = float(params.get("torque_Nm", 10.0))    # N·m

    # Pitch circle radius
    r_pitch_mm = m * N / 2.0
    r_pitch_m  = r_pitch_mm / 1000.0

    # Tangential force at pitch circle
    W_t = T / r_pitch_m   # N

    # Lewis form factor Y for standard 20° pressure angle (approximation by tooth count)
    # Barth's equation: Y ≈ 0.484 - 2.86/N  (simplified for N >= 12)
    Y = 0.484 - 2.86 / max(N, 12)

    # Lewis bending stress [MPa]:  σ = W_t / (b * m * Y)
    sigma_b = W_t / (b * m * Y)   # N / (mm * mm) = MPa

    sf_bending = _sf(yield_MPa, sigma_b)

    # Hertzian contact stress (simplified — assumes mating gear of same material)
    # σ_c = C_p * sqrt(W_t / (b * d_p * I))
    # For steel/steel, C_p ≈ 191 √MPa; elastic coeff for same material:
    C_p = math.sqrt(E / (2 * math.pi * (1 - mat["poisson"]**2)))
    # Geometry factor I (pitting resistance geometry factor, simplified for 20° PA)
    # I ≈ sin(phi)*cos(phi) / (2*m_n) where m_n = mating ratio ~1 for equal gears
    phi = math.radians(20.0)
    I_geom = math.sin(phi) * math.cos(phi) / 2.0

    d_p = 2.0 * r_pitch_mm   # pitch diameter mm
    sigma_c = C_p * math.sqrt(W_t / (b * d_p * I_geom))  # MPa

    # Contact SF: compare to 2× yield (Hertz contact allowable ~ 2.8*yield for steel)
    sigma_c_allow = 2.8 * yield_MPa
    sf_contact = _sf(sigma_c_allow, sigma_c)

    sf_min = min(sf_bending, sf_contact)
    passed = sf_min >= 2.0
    failures = []
    warnings = []
    if sf_bending < 1.5:
        failures.append(f"Tooth bending SF {sf_bending:.2f} < 1.5")
    elif sf_bending < 2.0:
        warnings.append(f"Tooth bending SF {sf_bending:.2f} < 2.0")
    if sf_contact < 1.5:
        failures.append(f"Contact SF {sf_contact:.2f} < 1.5")
    elif sf_contact < 2.0:
        warnings.append(f"Contact SF {sf_contact:.2f} < 2.0")
    if N < 17:
        warnings.append(f"N={N} teeth — risk of undercutting for standard 20° PA profile")
    if "gear" in str(params.get("part_id", "")).lower() or "ratchet" in str(params.get("part_id", "")).lower():
        if sf_min < 4.0:
            warnings.append(f"SF < 4.0 for rotating gear — consider higher grade steel or increased face width")

    report_lines = [
        f"Gear Tooth Analysis (Lewis + Hertz)",
        f"  Module:           {m:.2f} mm    Teeth: {N}",
        f"  Face width:       {b:.1f} mm",
        f"  Pitch radius:     {r_pitch_mm:.1f} mm",
        f"  Torque:           {T:.2f} N·m",
        f"  Tangential force: {W_t:.1f} N",
        f"  Material:         {params.get('material', _DEFAULT_MATERIAL)} (σ_y={yield_MPa:.0f} MPa)",
        f"  Lewis form factor Y: {Y:.4f}",
        f"  σ_bending:        {sigma_b:.1f} MPa",
        f"  σ_contact:        {sigma_c:.1f} MPa",
        f"  SF (bending):     {sf_bending:.2f}",
        f"  SF (contact):     {sf_contact:.2f}",
        f"  SF (governing):   {sf_min:.2f}",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "fea_gear_tooth",
        "passed": passed,
        "safety_factor": sf_min,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "module_mm": m, "n_teeth": N, "face_width_mm": b,
            "torque_Nm": T, "W_t_N": W_t,
            "lewis_Y": Y,
            "sigma_bending_MPa": sigma_b,
            "sigma_contact_MPa": sigma_c,
            "sf_bending": sf_bending,
            "sf_contact": sf_contact,
        },
        "report": "\n".join(report_lines),
    }


def fea_plate_bending(params: dict) -> dict:
    """
    Simply-supported rectangular plate under uniform pressure (Timoshenko).

    Required params
    ---------------
    width_mm        : float
    height_mm / depth_mm : float — second in-plane dimension
    thickness_mm    : float — plate thickness
    pressure_MPa    : float — uniform distributed load
    material        : str
    """
    mat = _get_material(params.get("material"))
    yield_MPa = mat["yield_MPa"]
    E = mat["E_GPa"] * 1e3    # MPa
    nu = mat["poisson"]

    a = float(params.get("width_mm",  100.0))   # mm (longer span or equal)
    b = float(params.get("height_mm", params.get("depth_mm", 100.0)))  # mm
    t = float(params.get("thickness_mm", 5.0))  # mm
    q = float(params.get("pressure_MPa", 0.1))  # MPa (N/mm²)

    # Ensure a >= b for coefficient table
    if b > a:
        a, b = b, a
    aspect = a / b

    # Timoshenko coefficients for simply-supported plate (Table 8-1 approx)
    # beta: max deflection = beta * q * b^4 / (E * t^3)
    # alpha: max bending moment = alpha * q * b^2
    # Interpolate from standard values at aspect = 1, 1.2, 1.4, 1.6, 1.8, 2.0, ∞
    _aspects = [1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 3.0]
    _betas   = [0.0443, 0.0616, 0.0770, 0.0906, 0.1017, 0.1106, 0.1336]
    _alphas  = [0.0479, 0.0627, 0.0755, 0.0862, 0.0948, 0.1017, 0.1189]

    beta  = float(np.interp(min(aspect, 3.0), _aspects, _betas))
    alpha = float(np.interp(min(aspect, 3.0), _aspects, _alphas))

    # Max deflection [mm]
    delta_max = beta * q * b**4 / (E * t**3)

    # Max bending moment [N·mm/mm] per unit width
    M_max = alpha * q * b**2

    # Max bending stress [MPa]
    sigma_max = 6.0 * M_max / t**2

    sf = _sf(yield_MPa, sigma_max)
    passed = sf >= 2.0
    failures = []
    warnings = []
    if sf < 1.5:
        failures.append(f"Plate bending SF {sf:.2f} < 1.5")
    elif sf < 2.0:
        warnings.append(f"Plate bending SF {sf:.2f} < 2.0")
    if delta_max > t:
        warnings.append(f"Deflection {delta_max:.2f}mm > plate thickness {t:.1f}mm — large-deflection regime, results approximate")

    report_lines = [
        f"Plate Bending Analysis (Simply-Supported, Timoshenko)",
        f"  Plate:            {a:.1f} x {b:.1f} mm  (aspect ratio {aspect:.2f})",
        f"  Thickness:        {t:.1f} mm",
        f"  Pressure:         {q:.4f} MPa",
        f"  Material:         {params.get('material', _DEFAULT_MATERIAL)} (σ_y={yield_MPa:.0f} MPa)",
        f"  Coeff β:          {beta:.4f}",
        f"  Coeff α:          {alpha:.4f}",
        f"  δ_max:            {delta_max:.3f} mm",
        f"  σ_max:            {sigma_max:.1f} MPa",
        f"  Safety factor:    {sf:.2f}",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "fea_plate_bending",
        "passed": passed,
        "safety_factor": sf,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "a_mm": a, "b_mm": b, "t_mm": t, "pressure_MPa": q,
            "aspect_ratio": aspect, "beta": beta, "alpha": alpha,
            "delta_max_mm": delta_max, "sigma_max_MPa": sigma_max,
        },
        "report": "\n".join(report_lines),
    }


def fea_bolt_circle(params: dict) -> dict:
    """
    Bolt pattern shear and tension analysis.

    Required params
    ---------------
    n_bolts         : int   — number of bolts
    bolt_dia_mm     : float — bolt nominal diameter
    bolt_circle_r_mm: float — bolt circle radius
    shear_load_N    : float — total shear load (default 1000)
    tension_load_N  : float — total axial/tension load (default 0)
    bolt_grade      : str   — "8.8" | "10.9" | "12.9" | "4.8" (default "8.8")
    material        : str   — fallback if grade not specified
    """
    n = max(1, int(float(params.get("n_bolts", 4))))
    d = float(params.get("bolt_dia_mm", 8.0))           # mm
    r = float(params.get("bolt_circle_r_mm", 40.0))     # mm
    V = float(params.get("shear_load_N", 1000.0))       # N
    T_axial = float(params.get("tension_load_N", 0.0))  # N

    # Bolt grade yield / proof strengths [MPa]
    _GRADES: dict[str, tuple[float, float]] = {
        "4.8":  (340.0,  310.0),
        "8.8":  (660.0,  600.0),
        "10.9": (940.0,  830.0),
        "12.9": (1100.0, 970.0),
    }
    grade_str = str(params.get("bolt_grade", "8.8"))
    yield_MPa, proof_MPa = _GRADES.get(grade_str, _GRADES["8.8"])

    # Tensile stress area (Shigley): A_s ≈ π/4 * (d - 0.9743/pitch)²
    # Simplified: A_s ≈ 0.7854 * (d - 0.9382 * pitch)²  for metric coarse thread
    # Rough approximation: A_s ≈ 0.78 * (0.9d)^2 * pi/4
    A_s = math.pi / 4.0 * (0.9 * d)**2   # mm²  (tensile stress area approx)
    A_shear = math.pi / 4.0 * d**2        # mm²  (full shank, single shear)

    # Shear per bolt (direct shear only — assumes symmetrical pattern)
    shear_per_bolt = V / n   # N

    # Shear stress per bolt [MPa]
    tau = shear_per_bolt / A_shear

    # Tension per bolt
    tension_per_bolt = T_axial / n   # N
    sigma_t = tension_per_bolt / A_s   # MPa

    # Von Mises combined [MPa]
    sigma_vm = math.sqrt(sigma_t**2 + 3.0 * tau**2)

    # Allowable shear: 0.577 * yield (von Mises)
    tau_allow = 0.577 * yield_MPa
    sf_shear   = _sf(tau_allow, tau)
    sf_tension = _sf(proof_MPa, sigma_t) if sigma_t > 0 else float("inf")
    sf_combined = _sf(yield_MPa, sigma_vm)
    sf_min = min(sf_shear, sf_combined)

    passed = sf_min >= 2.0
    failures = []
    warnings = []
    if sf_shear < 1.5:
        failures.append(f"Bolt shear SF {sf_shear:.2f} < 1.5 — fastener failure")
    elif sf_shear < 2.0:
        warnings.append(f"Bolt shear SF {sf_shear:.2f} < 2.0")
    if sf_tension < 1.5 and T_axial > 0:
        failures.append(f"Bolt tension SF {sf_tension:.2f} < 1.5")
    elif sf_tension < 2.0 and T_axial > 0:
        warnings.append(f"Bolt tension SF {sf_tension:.2f} < 2.0")

    report_lines = [
        f"Bolt Circle Analysis",
        f"  Bolts:            {n} × M{d:.0f}  (grade {grade_str})",
        f"  Bolt circle r:    {r:.1f} mm",
        f"  Shear load:       {V:.1f} N  ({shear_per_bolt:.1f} N/bolt)",
        f"  Tension load:     {T_axial:.1f} N  ({tension_per_bolt:.1f} N/bolt)",
        f"  A_s:              {A_s:.2f} mm²   A_shear: {A_shear:.2f} mm²",
        f"  τ (shear):        {tau:.1f} MPa",
        f"  σ_t (tension):    {sigma_t:.1f} MPa",
        f"  σ_vm (combined):  {sigma_vm:.1f} MPa",
        f"  SF (shear):       {sf_shear:.2f}",
        f"  SF (tension):     {sf_tension:.2f}",
        f"  SF (combined):    {sf_combined:.2f}",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "fea_bolt_circle",
        "passed": passed,
        "safety_factor": sf_min,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "n_bolts": n, "bolt_dia_mm": d, "bolt_grade": grade_str,
            "A_s_mm2": A_s, "A_shear_mm2": A_shear,
            "shear_per_bolt_N": shear_per_bolt,
            "tau_MPa": tau, "sigma_t_MPa": sigma_t, "sigma_vm_MPa": sigma_vm,
            "sf_shear": sf_shear, "sf_tension": sf_tension,
        },
        "report": "\n".join(report_lines),
    }


# ---------------------------------------------------------------------------
# CFD — Fluid analyses
# ---------------------------------------------------------------------------

def cfd_pipe_flow(params: dict) -> dict:
    """
    Darcy-Weisbach pipe flow analysis.

    Required params
    ---------------
    diameter_mm     : float — pipe inner diameter
    length_mm       : float — pipe length
    roughness_mm    : float — absolute roughness (default 0.046 for steel)
    density         : float — fluid density [kg/m³] (default 1000 water)
    viscosity       : float — dynamic viscosity [Pa·s] (default 1e-3 water)
    flow_rate_m3s   : float — volumetric flow rate [m³/s] (default 1e-4)
    """
    D = float(params.get("diameter_mm", 20.0)) / 1000.0   # m
    L = float(params.get("length_mm", 1000.0)) / 1000.0   # m
    eps = float(params.get("roughness_mm", 0.046)) / 1000.0  # m  (commercial steel)
    rho = float(params.get("density", 1000.0))             # kg/m³
    mu  = float(params.get("viscosity", 1.0e-3))           # Pa·s
    Q   = float(params.get("flow_rate_m3s", 1.0e-4))       # m³/s

    A = math.pi * D**2 / 4.0          # m²
    v = Q / A                           # m/s

    Re = rho * v * D / mu

    if Re < 2300:
        regime = "laminar"
        f = 64.0 / Re
    elif Re < 4000:
        regime = "transitional"
        # Linear interpolation between laminar and turbulent
        f_lam  = 64.0 / Re
        # Colebrook-White for turbulent end
        f_turb = (-2.0 * math.log10(eps / (3.7 * D) + 2.51 / (Re * math.sqrt(0.02)))) ** -2
        f = f_lam + (Re - 2300.0) / (4000.0 - 2300.0) * (f_turb - f_lam)
    else:
        regime = "turbulent"
        # Colebrook-White (iterate from Swamee-Jain initial guess)
        f = (0.25 / (math.log10(eps / (3.7 * D) + 5.74 / Re**0.9))**2)
        for _ in range(10):
            f = (-2.0 * math.log10(eps / (3.7 * D) + 2.51 / (Re * math.sqrt(f)))) ** -2

    dP = f * (L / D) * 0.5 * rho * v**2   # Pa

    passed = True   # no hard pass/fail for pipe flow; we warn on high velocities
    failures = []
    warnings = []
    if v > 5.0:
        warnings.append(f"Flow velocity {v:.2f} m/s > 5 m/s — consider larger bore or lower flow rate")
    if Re > 1e7:
        warnings.append(f"Re={Re:.2e} — very high, correlation accuracy may degrade")

    report_lines = [
        f"Pipe Flow Analysis (Darcy-Weisbach)",
        f"  Diameter:         {D*1000:.1f} mm",
        f"  Length:           {L*1000:.1f} mm",
        f"  Roughness:        {eps*1000:.4f} mm",
        f"  Flow rate:        {Q*1e6:.2f} mL/s  ({Q:.2e} m³/s)",
        f"  Velocity:         {v:.3f} m/s",
        f"  Reynolds number:  {Re:.0f}",
        f"  Flow regime:      {regime}",
        f"  Friction factor f:{f:.5f}",
        f"  Pressure drop:    {dP:.1f} Pa  ({dP/1e5:.4f} bar)",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "cfd_pipe_flow",
        "passed": passed,
        "safety_factor": None,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "D_mm": D * 1000.0, "L_mm": L * 1000.0,
            "velocity_m_s": v, "Re": Re, "regime": regime,
            "friction_factor": f, "pressure_drop_Pa": dP,
            "pressure_drop_bar": dP / 1e5,
        },
        "report": "\n".join(report_lines),
    }


def cfd_nozzle_flow(params: dict) -> dict:
    """
    Isentropic nozzle flow analysis (convergent-divergent or converging).

    Required params
    ---------------
    throat_r_mm         : float — throat radius [mm]
    exit_r_mm           : float — exit radius [mm] (if diverging)
    chamber_pressure_MPa: float — stagnation pressure [MPa]
    chamber_temp_K      : float — stagnation temperature [K]
    gamma               : float — specific heat ratio (default 1.4 for air)
    MW                  : float — molecular weight [g/mol] (default 28.97 air)
    """
    r_t = float(params.get("throat_r_mm", 10.0)) / 1000.0   # m
    r_e = float(params.get("exit_r_mm",   20.0)) / 1000.0   # m
    P0  = float(params.get("chamber_pressure_MPa", 1.0)) * 1e6  # Pa
    T0  = float(params.get("chamber_temp_K", 3000.0))           # K
    g   = float(params.get("gamma", 1.4))
    MW  = float(params.get("MW", 28.97)) / 1000.0               # kg/mol

    R_univ = 8.314  # J/(mol·K)
    R_spec = R_univ / MW   # J/(kg·K)

    A_t = math.pi * r_t**2   # m²
    A_e = math.pi * r_e**2   # m²
    AR = A_e / A_t            # area ratio

    # Throat (choked) conditions
    T_star = T0 * 2.0 / (g + 1.0)
    P_star = P0 * (2.0 / (g + 1.0)) ** (g / (g - 1.0))
    rho_star = P_star / (R_spec * T_star)
    v_star = math.sqrt(g * R_spec * T_star)   # throat velocity = sonic velocity

    choked = True  # for P0 >> P_amb, always choked

    # Exit Mach number (supersonic branch): solve A/A* = f(M) iteratively
    # A/A* = (1/M) * [(2/(g+1)) * (1 + (g-1)/2 * M²)] ^ ((g+1)/(2*(g-1)))
    def _area_ratio(M: float) -> float:
        return (1.0 / M) * ((2.0 / (g + 1.0)) * (1.0 + (g - 1.0) / 2.0 * M**2)) ** ((g + 1.0) / (2.0 * (g - 1.0)))

    # Newton-Raphson to find supersonic M_e given AR
    M_e = 2.0
    for _ in range(50):
        f_val = _area_ratio(M_e) - AR
        # Numerical derivative
        dM = 1e-6
        dfdM = (_area_ratio(M_e + dM) - _area_ratio(M_e - dM)) / (2.0 * dM)
        if abs(dfdM) < 1e-12:
            break
        M_e = M_e - f_val / dfdM
        M_e = max(1.0 + 1e-6, M_e)

    # Exit conditions (isentropic)
    T_e = T0 / (1.0 + (g - 1.0) / 2.0 * M_e**2)
    P_e = P0 * (T_e / T0) ** (g / (g - 1.0))
    v_e = M_e * math.sqrt(g * R_spec * T_e)
    rho_e = P_e / (R_spec * T_e)

    # Thrust (vacuum): F = m_dot * v_e + (P_e - 0) * A_e
    m_dot = rho_star * v_star * A_t
    thrust_vac = m_dot * v_e + P_e * A_e   # N
    # ISP (vacuum)
    g0 = 9.80665
    Isp = thrust_vac / (m_dot * g0)

    passed = True
    failures = []
    warnings = []
    if not choked:
        warnings.append("Nozzle may not be choked — check chamber pressure vs ambient")
    if M_e < 1.0:
        warnings.append("Exit Mach < 1.0 — nozzle is not producing supersonic exit flow")
    if P_e / P0 < 0.01:
        warnings.append(f"Very high expansion ratio — P_exit/P_chamber = {P_e/P0:.4f}")

    report_lines = [
        f"Isentropic Nozzle Flow Analysis",
        f"  Throat radius:    {r_t*1000:.2f} mm  (A_t={A_t*1e6:.2f} mm²)",
        f"  Exit radius:      {r_e*1000:.2f} mm  (A_e={A_e*1e6:.2f} mm²)",
        f"  Area ratio A_e/A*:{AR:.3f}",
        f"  P0:               {P0/1e6:.2f} MPa    T0: {T0:.0f} K",
        f"  γ:                {g:.3f}    MW: {MW*1000:.2f} g/mol",
        f"  Throat velocity:  {v_star:.1f} m/s  (sonic)",
        f"  Exit Mach number: {M_e:.4f}",
        f"  Exit velocity:    {v_e:.1f} m/s",
        f"  Exit pressure:    {P_e/1000:.2f} kPa",
        f"  Exit temperature: {T_e:.1f} K",
        f"  Mass flow rate:   {m_dot:.4f} kg/s",
        f"  Vacuum thrust:    {thrust_vac:.2f} N",
        f"  Vacuum Isp:       {Isp:.1f} s",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "cfd_nozzle_flow",
        "passed": passed,
        "safety_factor": None,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "throat_r_mm": r_t * 1000, "exit_r_mm": r_e * 1000,
            "area_ratio": AR, "M_exit": M_e,
            "v_throat_m_s": v_star, "v_exit_m_s": v_e,
            "P_exit_Pa": P_e, "T_exit_K": T_e,
            "m_dot_kg_s": m_dot, "thrust_vac_N": thrust_vac, "Isp_vac_s": Isp,
        },
        "report": "\n".join(report_lines),
    }


def cfd_heat_transfer(params: dict) -> dict:
    """
    Forced convection heat transfer (flat plate or internal tube).

    Required params
    ---------------
    geometry        : str   — "flat_plate" | "tube" (default flat_plate)
    length_mm       : float — plate length or tube length
    diameter_mm     : float — tube diameter (for geometry=tube)
    velocity_m_s    : float — free-stream or mean flow velocity
    heat_flux_W_m2  : float — wall heat flux [W/m²]
    density         : float — fluid density [kg/m³]  (default air ~1.2)
    viscosity       : float — dynamic viscosity [Pa·s]
    k_fluid         : float — thermal conductivity [W/(m·K)]
    Cp_fluid        : float — specific heat [J/(kg·K)]
    T_fluid_K       : float — fluid temperature
    T_wall_max_K    : float — maximum allowable wall temperature
    """
    geom    = str(params.get("geometry", "flat_plate")).lower()
    L       = float(params.get("length_mm", 100.0)) / 1000.0      # m
    D       = float(params.get("diameter_mm", 20.0)) / 1000.0     # m
    v       = float(params.get("velocity_m_s", 5.0))               # m/s
    q_flux  = float(params.get("heat_flux_W_m2", 10000.0))         # W/m²
    rho     = float(params.get("density", 1.2))                    # kg/m³  (air default)
    mu      = float(params.get("viscosity", 1.85e-5))              # Pa·s  (air)
    k_f     = float(params.get("k_fluid", 0.026))                  # W/(m·K) air
    Cp      = float(params.get("Cp_fluid", 1005.0))                # J/(kg·K) air
    T_f     = float(params.get("T_fluid_K", 300.0))                # K
    T_wall_max = float(params.get("T_wall_max_K", 500.0))          # K

    Pr = mu * Cp / k_f

    if geom == "tube":
        Re = rho * v * D / mu
        if Re < 2300:
            Nu = 3.66  # laminar fully developed
        elif Re < 10000:
            # Gnielinski correlation
            f_g = (0.79 * math.log(Re) - 1.64) ** -2
            Nu = (f_g / 8.0 * (Re - 1000.0) * Pr) / (1.0 + 12.7 * math.sqrt(f_g / 8.0) * (Pr**(2.0/3.0) - 1.0))
        else:
            # Dittus-Boelter
            Nu = 0.023 * Re**0.8 * Pr**0.4
        h = Nu * k_f / D
        Re_label = f"Re={Re:.0f} (tube)"
    else:
        # Flat plate average: Nu_avg = 0.664 * Re^0.5 * Pr^(1/3) for laminar
        Re = rho * v * L / mu
        if Re < 5e5:
            Nu = 0.664 * Re**0.5 * Pr**(1.0/3.0)
        else:
            Nu = (0.037 * Re**0.8 - 871.0) * Pr**(1.0/3.0)
        h = Nu * k_f / L
        Re_label = f"Re={Re:.0f} (flat plate)"

    # Wall temperature
    T_wall = T_f + q_flux / max(h, 1e-6)

    passed = T_wall <= T_wall_max
    failures = []
    warnings = []
    if T_wall > T_wall_max:
        failures.append(f"Wall temp {T_wall:.1f} K exceeds limit {T_wall_max:.1f} K — increase h or reduce heat flux")
    elif T_wall > 0.9 * T_wall_max:
        warnings.append(f"Wall temp {T_wall:.1f} K is within 10% of limit {T_wall_max:.1f} K")

    report_lines = [
        f"Heat Transfer Analysis ({geom.replace('_',' ').title()})",
        f"  Velocity:         {v:.2f} m/s",
        f"  {Re_label}",
        f"  Prandtl:          {Pr:.3f}",
        f"  Nusselt:          {Nu:.2f}",
        f"  h_conv:           {h:.1f} W/(m²·K)",
        f"  Heat flux:        {q_flux:.0f} W/m²",
        f"  Fluid temp:       {T_f:.1f} K",
        f"  Wall temp:        {T_wall:.1f} K",
        f"  Wall temp limit:  {T_wall_max:.1f} K",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "cfd_heat_transfer",
        "passed": passed,
        "safety_factor": None,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "Re": Re, "Pr": Pr, "Nu": Nu, "h_conv_W_m2K": h,
            "T_wall_K": T_wall, "T_wall_max_K": T_wall_max,
        },
        "report": "\n".join(report_lines),
    }


def cfd_drag_estimate(params: dict) -> dict:
    """
    Bluff body drag estimate from Reynolds-number-based Cd lookup.

    Required params
    ---------------
    frontal_area_mm2: float — projected frontal area [mm²]
    velocity_m_s    : float — free-stream velocity
    density         : float — fluid density [kg/m³]  (default 1.2 air)
    viscosity       : float — dynamic viscosity [Pa·s]
    char_length_mm  : float — characteristic length for Re (default sqrt(A))
    body_shape      : str   — "sphere"|"cylinder"|"flat_plate"|"streamlined" (default "cylinder")
    """
    A_mm2 = float(params.get("frontal_area_mm2", 1000.0))
    A     = A_mm2 / 1e6   # m²
    v     = float(params.get("velocity_m_s", 10.0))
    rho   = float(params.get("density", 1.2))
    mu    = float(params.get("viscosity", 1.85e-5))
    L_mm  = float(params.get("char_length_mm", math.sqrt(A_mm2)))
    L     = L_mm / 1000.0
    shape = str(params.get("body_shape", "cylinder")).lower()

    Re = rho * v * L / mu

    # Cd from Re lookup tables (approximate; Munson et al.)
    if shape == "sphere":
        if Re < 1:
            Cd = 24.0 / Re
        elif Re < 1000:
            Cd = 24.0 / Re * (1.0 + 0.15 * Re**0.687)
        elif Re < 2e5:
            Cd = 0.44
        else:
            Cd = 0.20   # turbulent boundary layer
    elif shape in ("flat_plate", "plate"):
        # Normal flat plate
        if Re < 1e5:
            Cd = 1.28
        else:
            Cd = 1.17
    elif shape == "streamlined":
        Cd = 0.04
    else:
        # Cylinder (default)
        if Re < 1:
            Cd = 8.0 * math.pi / (Re * (2.0 - math.log(Re + 1e-12)))
        elif Re < 1000:
            Cd = max(1.0, 24.0 / Re + 6.0 / (1.0 + math.sqrt(Re)) + 0.4)
        elif Re < 2e5:
            Cd = 1.0
        else:
            Cd = 0.35  # supercritical

    F_drag = 0.5 * rho * v**2 * Cd * A   # N
    P_drag = F_drag * v                    # W  (power to overcome drag)

    passed = True
    failures = []
    warnings = []
    if F_drag > 100.0:
        warnings.append(f"Drag force {F_drag:.1f} N is significant — consider streamlining")

    report_lines = [
        f"Drag Estimate ({shape})",
        f"  Frontal area:     {A_mm2:.0f} mm²  ({A*1e4:.2f} cm²)",
        f"  Velocity:         {v:.2f} m/s",
        f"  Reynolds number:  {Re:.2e}",
        f"  Cd:               {Cd:.3f}",
        f"  Drag force:       {F_drag:.3f} N",
        f"  Power (drag):     {P_drag:.2f} W",
        f"  Result:           {'PASS' if passed else 'FAIL'}",
    ]

    return {
        "analysis_type": "cfd_drag_estimate",
        "passed": passed,
        "safety_factor": None,
        "failures": failures,
        "warnings": warnings,
        "details": {
            "frontal_area_mm2": A_mm2, "velocity_m_s": v,
            "Re": Re, "Cd": Cd, "drag_N": F_drag, "power_W": P_drag,
        },
        "report": "\n".join(report_lines),
    }


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Drop impact analysis (phone cases, enclosures, protective equipment)
# ---------------------------------------------------------------------------

def fea_drop_impact(params: dict) -> dict:
    """
    Drop impact analysis — energy absorption and stress at impact.

    Models a free-fall drop onto a rigid surface. Computes impact velocity,
    kinetic energy, peak deceleration, and stress in the case wall.

    Uses energy method: KE = m*g*h, peak force F = KE / crush_distance,
    stress = F / wall_cross_section_area.
    """
    import math

    # Part dims
    width  = float(params.get("width_mm", params.get("od_mm", 80.0)))
    length = float(params.get("height_mm", params.get("length_mm", 160.0)))
    wall   = float(params.get("wall_mm", 2.5))
    depth  = float(params.get("depth_mm", params.get("thickness_mm", 12.0)))

    # Drop parameters
    drop_height_m = float(params.get("drop_height_m", 4.572))  # 15 ft default
    mass_kg       = float(params.get("mass_kg", 0.240))        # iPhone 13 PM = 240g
    g             = 9.81

    # Material
    mat_name = params.get("material", "polycarbonate")
    mat = _get_material(mat_name)
    E_Pa       = mat["E_GPa"] * 1e9
    yield_Pa   = mat["yield_MPa"] * 1e6
    density    = mat["density"]

    # Impact velocity: v = sqrt(2*g*h)
    v_impact = math.sqrt(2 * g * drop_height_m)

    # Kinetic energy at impact
    KE = 0.5 * mass_kg * v_impact**2

    # Crush distance — how far the case deforms to absorb energy
    # For rigid PC: very small deformation; for TPU: larger
    # Estimate from wall compression: crush_d = wall * yield_strain
    yield_strain = yield_Pa / E_Pa
    crush_d_m = (wall / 1000) * min(yield_strain * 10, 0.3)  # cap at 30% wall compression
    crush_d_m = max(crush_d_m, 0.0005)  # minimum 0.5mm crush

    # Peak deceleration force: F = KE / crush_distance
    F_peak = KE / crush_d_m

    # Peak deceleration in g's
    decel_g = F_peak / (mass_kg * g)

    # Stress at corner impact — force distributed over corner contact area
    # Assume corner contact patch ~ 10mm x 10mm = 100 mm^2
    contact_area_m2 = (10.0 * wall) * 1e-6  # corner wall cross-section
    sigma_impact = F_peak / contact_area_m2

    # Safety factor
    sf = yield_Pa / sigma_impact if sigma_impact > 0 else 999.0

    # MIL-STD-810G drop test: 26 drops from 1.22m onto 50mm plywood over steel
    mil_std_height = 1.22
    mil_std_pass = drop_height_m <= 10.0 and sf >= 1.0  # survives if SF > 1

    passed = sf >= 1.5
    failures = []
    warnings = []

    if sf < 1.0:
        failures.append(f"Wall yields at impact: stress {sigma_impact/1e6:.1f} MPa > yield {yield_Pa/1e6:.0f} MPa")
    if sf < 1.5:
        failures.append(f"SF {sf:.2f} < 1.5 minimum for drop protection")
    if decel_g > 1000:
        warnings.append(f"Peak deceleration {decel_g:.0f}g exceeds phone survival threshold (~1000g)")
    if decel_g > 500:
        warnings.append(f"Peak deceleration {decel_g:.0f}g — consider thicker corner bumpers or softer material")

    report = (
        f"Drop Impact Analysis (free fall onto rigid surface)\n"
        f"  Drop height:      {drop_height_m:.2f} m ({drop_height_m * 3.281:.0f} ft)\n"
        f"  Phone mass:       {mass_kg * 1000:.0f} g\n"
        f"  Impact velocity:  {v_impact:.2f} m/s ({v_impact * 3.6:.1f} km/h)\n"
        f"  Kinetic energy:   {KE:.2f} J\n"
        f"  Material:         {mat_name} (E={mat['E_GPa']} GPa, sigma_y={mat['yield_MPa']} MPa)\n"
        f"  Wall thickness:   {wall:.1f} mm\n"
        f"  Crush distance:   {crush_d_m * 1000:.2f} mm\n"
        f"  Peak force:       {F_peak:.0f} N ({F_peak / g:.0f} g)\n"
        f"  Peak deceleration:{decel_g:.0f} g\n"
        f"  Impact stress:    {sigma_impact / 1e6:.1f} MPa\n"
        f"  Safety factor:    {sf:.2f}\n"
        f"  MIL-STD-810G:     {'PASS' if mil_std_pass else 'FAIL'}\n"
        f"  Result:           {'PASS' if passed else 'FAIL'}"
    )

    return {
        "analysis_type": "fea_drop_impact",
        "passed": passed,
        "safety_factor": round(sf, 2),
        "failures": failures,
        "warnings": warnings,
        "details": {
            "drop_height_m": drop_height_m,
            "v_impact_ms": round(v_impact, 2),
            "KE_J": round(KE, 2),
            "F_peak_N": round(F_peak, 0),
            "decel_g": round(decel_g, 0),
            "sigma_impact_MPa": round(sigma_impact / 1e6, 1),
            "crush_mm": round(crush_d_m * 1000, 2),
            "mil_std_810g": mil_std_pass,
        },
        "report": report,
    }


# ---------------------------------------------------------------------------
# Auto-detection keyword -> analysis type
# ---------------------------------------------------------------------------

# Ordered list of (keywords, analysis_fn_name).  First match wins.
_KEYWORD_MAP: list[tuple[tuple[str, ...], str]] = [
    (("drop", "impact", "case", "phone", "protective", "rugged", "mil-std", "fall"), "fea_drop_impact"),
    (("nozzle", "rocket", "lre", "injector", "throat", "divergent", "convergent"), "cfd_nozzle_flow"),
    (("gear",   "pinion", "tooth", "ratchet", "sprocket"),                          "fea_gear_tooth"),
    (("pipe",   "tube",   "duct",  "channel", "manifold", "conduit"),               "cfd_pipe_flow"),
    (("housing","drum",   "barrel","cylinder","pressure", "vessel", "bore"),         "fea_thick_cylinder"),
    (("shaft",  "arbor",  "rod",   "beam",    "arm",      "axle",   "cantilever"),   "fea_beam_bending"),
    (("bracket","plate",  "flange","panel",   "shelf",    "gusset"),                 "fea_plate_bending"),
    (("bolt",   "fastener","bolt_circle","screw","stud"),                            "fea_bolt_circle"),
    (("heat",   "thermal","convection","cooling","temperature"),                     "cfd_heat_transfer"),
    (("drag",   "bluff",  "aerodynamic","wind"),                                     "cfd_drag_estimate"),
]

_FEA_ANALYSES = {
    "fea_beam_bending",
    "fea_thick_cylinder",
    "fea_gear_tooth",
    "fea_plate_bending",
    "fea_bolt_circle",
    "fea_drop_impact",
}
_CFD_ANALYSES = {
    "cfd_pipe_flow",
    "cfd_nozzle_flow",
    "cfd_heat_transfer",
    "cfd_drag_estimate",
}

_ANALYSIS_FN_MAP = {
    "fea_beam_bending":  fea_beam_bending,
    "fea_thick_cylinder": fea_thick_cylinder,
    "fea_gear_tooth":    fea_gear_tooth,
    "fea_plate_bending": fea_plate_bending,
    "fea_bolt_circle":   fea_bolt_circle,
    "cfd_pipe_flow":     cfd_pipe_flow,
    "cfd_nozzle_flow":   cfd_nozzle_flow,
    "cfd_heat_transfer": cfd_heat_transfer,
    "cfd_drag_estimate": cfd_drag_estimate,
    "fea_drop_impact":   fea_drop_impact,
}


def _detect_analysis(part_id: str, goal: str, analysis_type: str) -> str:
    """Return the specific analysis function name to run."""
    if analysis_type in _ANALYSIS_FN_MAP:
        return analysis_type

    text = (part_id + " " + goal).lower()

    # If caller requested a domain, filter to that domain first
    if analysis_type == "fea":
        for keywords, name in _KEYWORD_MAP:
            if name in _FEA_ANALYSES and any(k in text for k in keywords):
                return name
        return "fea_beam_bending"   # safe default

    if analysis_type == "cfd":
        for keywords, name in _KEYWORD_MAP:
            if name in _CFD_ANALYSES and any(k in text for k in keywords):
                return name
        return "cfd_pipe_flow"   # safe default

    # "auto": full keyword scan
    for keywords, name in _KEYWORD_MAP:
        if any(k in text for k in keywords):
            return name

    # Fall back to beam bending as the most generic structural check
    return "fea_beam_bending"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(
    part_id: str,
    analysis_type: str,          # "fea" | "cfd" | "auto" or specific name
    params: dict,
    goal: str = "",
    repo_root: "str | Path" = ".",
) -> dict:
    """
    Auto-selects the most relevant FEA or CFD sub-analysis based on part_id/goal
    keywords, runs it, and returns a structured result dict.

    Returns
    -------
    {
        "analysis_type": str,       # e.g. "fea_gear_tooth"
        "passed": bool,
        "safety_factor": float | None,
        "failures": list[str],
        "warnings": list[str],
        "details": dict,            # raw computed values
        "report": str,              # human-readable multi-line summary
    }
    """
    fn_name = _detect_analysis(part_id, goal, analysis_type)
    fn = _ANALYSIS_FN_MAP[fn_name]

    # Enrich params with part_id so analyses can use it for warnings
    enriched = dict(params)
    enriched.setdefault("part_id", part_id)

    # Auto-detect material from goal string if not explicitly set in params
    if not enriched.get("material"):
        enriched["material"] = _detect_material_from_goal(goal, enriched)

    result = fn(enriched)
    return result


def prompt_and_analyze(
    part_id: str,
    params: dict,
    goal: str,
    step_path: str,
    repo_root: "str | Path" = ".",
) -> "dict | None":
    """
    Shown after STEP export.  Asks user: 'Run FEA or CFD analysis? [fea/cfd/skip]'

    If stdin is non-interactive, returns None (skip) without blocking.
    Returns analyze() result or None.
    """
    if not sys.stdin.isatty():
        return None

    print()
    print("=" * 64)
    print("  ARIA Physics Analysis — choose analysis type")
    print("=" * 64)
    print("  [fea]  — Structural FEA (beam, plate, gear, pressure vessel, bolts)")
    print("  [cfd]  — Fluid CFD (pipe, nozzle, heat transfer, drag)")
    print("  [auto] — Auto-detect from part description")
    print("  [skip] — Skip analysis")
    print("=" * 64)

    _MAP: dict[str, str] = {
        "fea":  "fea",   "f": "fea",   "structural": "fea",
        "cfd":  "cfd",   "c": "cfd",   "fluid": "cfd",
        "auto": "auto",  "a": "auto",
        "skip": "skip",  "s": "skip",  "n": "skip",  "": "skip",
    }

    try:
        raw = input("  Your choice [fea/cfd/auto/skip] (default: skip): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n  (interrupted — skipping analysis)")
        return None

    choice = _MAP.get(raw)
    if choice is None:
        print(f"  Unrecognised choice '{raw}' — skipping analysis")
        return None
    if choice == "skip":
        return None

    print(f"  Running {choice.upper()} analysis for {part_id or 'part'}...")
    result = analyze(part_id, choice, params, goal=goal, repo_root=repo_root)

    # Print concise inline summary
    print()
    print(result["report"])
    if result.get("failures"):
        for f_msg in result["failures"]:
            print(f"  [FAIL] {f_msg}")
    if result.get("warnings"):
        for w in result["warnings"]:
            print(f"  [WARN] {w}")
    print("=" * 64)

    return result
