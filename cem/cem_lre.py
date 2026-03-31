"""
cem_lre.py — Liquid Rocket Engine (LRE) CEM Module

Computes nozzle geometry from thrust + chamber pressure requirements.
Registered in cem_registry.py under keywords: lre, nozzle, rocket, turbopump, injector.

Physics encoded:
  - Nozzle contour from thrust coefficient + area ratio
  - Throat sizing from mass flow + chamber conditions
  - Wall thickness from hoop stress (chamber pressure)
  - Bell nozzle parabolic approximation (Rao 80% bell)

Standards:
  - NASA SP-8076 Liquid Rocket Engine Turbopump Inducers
  - Sutton & Biblarz "Rocket Propulsion Elements"

Usage:
    from cem_lre import compute_lre_nozzle, LREInputs
    geom = compute_lre_nozzle(LREInputs(thrust_kN=10.0, Pc_MPa=3.0))
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

try:
    from .cem_core import Material, MATERIAL_INCONEL718, MATERIAL_COPPER_C18150
    _INCONEL = MATERIAL_INCONEL718
    _COPPER  = MATERIAL_COPPER_C18150
except ImportError:
    _INCONEL = None
    _COPPER  = None


# ---------------------------------------------------------------------------
# Propellant combinations
# ---------------------------------------------------------------------------

PROPELLANTS: dict[str, dict] = {
    "lox_rp1": {
        "name":       "LOX / RP-1",
        "OF_ratio":   2.56,
        "Isp_vac_s":  311.0,
        "c_star_m_s": 1774.0,
        "gamma":      1.24,
        "MW_kg_mol":  0.0215,
        "Tc_K":       3670.0,
    },
    "lox_lh2": {
        "name":       "LOX / LH2",
        "OF_ratio":   5.5,
        "Isp_vac_s":  450.0,
        "c_star_m_s": 2390.0,
        "gamma":      1.26,
        "MW_kg_mol":  0.0112,
        "Tc_K":       3500.0,
    },
    "lox_ipa": {
        "name":       "LOX / IPA",
        "OF_ratio":   1.3,
        "Isp_vac_s":  280.0,
        "c_star_m_s": 1630.0,
        "gamma":      1.22,
        "MW_kg_mol":  0.0235,
        "Tc_K":       3100.0,
    },
    "n2o4_udmh": {
        "name":       "N2O4 / UDMH",
        "OF_ratio":   2.1,
        "Isp_vac_s":  289.0,
        "c_star_m_s": 1720.0,
        "gamma":      1.24,
        "MW_kg_mol":  0.0220,
        "Tc_K":       3300.0,
    },
}


# ---------------------------------------------------------------------------
# Input dataclass
# ---------------------------------------------------------------------------

@dataclass
class LREInputs:
    """All LRE design requirements — change these, geometry recomputes."""
    thrust_kN:        float = 10.0     # vacuum thrust target
    Pc_MPa:           float = 3.0      # chamber pressure (absolute)
    Pe_kPa:           float = 10.0     # nozzle exit pressure (vacuum ≈ 0)
    propellant:       str   = "lox_rp1"
    safety_factor:    float = 2.0      # structural SF on wall thickness
    wall_material:    str   = "inconel718"  # inconel718 | copper_c18150
    regenerative:     bool  = True     # regenerative cooling flag
    # Geometry overrides (None = compute from physics)
    throat_r_mm:      Optional[float] = None
    exit_area_ratio:  Optional[float] = None
    conv_half_angle_deg: float = 30.0  # convergent half-angle
    div_half_angle_deg:  float = 15.0  # divergent half-angle (initial, Rao bell)


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class NozzleGeom:
    """Nozzle contour geometry — all dimensions in mm."""
    throat_r_mm:       float   # throat radius
    throat_area_m2:    float   # throat area
    exit_r_mm:         float   # exit radius
    exit_area_m2:      float   # exit area
    area_ratio:        float   # exit / throat
    entry_r_mm:        float   # entry (chamber-side) radius
    conv_length_mm:    float   # convergent section length
    div_length_mm:     float   # divergent section length
    total_length_mm:   float   # throat-to-exit length
    wall_mm:           float   # wall thickness (structural minimum)
    mass_kg:           float   # nozzle mass estimate
    throat_sf:         float   # safety factor at throat (highest pressure)
    exit_sf:           float   # safety factor at exit
    Cf:                float   # thrust coefficient
    c_star_m_s:        float   # characteristic velocity
    Isp_s:             float   # specific impulse (vacuum)
    mdot_kg_s:         float   # mass flow rate
    warnings:          list[str] = field(default_factory=list)


@dataclass
class LREGeom:
    """Complete LRE geometry output."""
    inputs:   LREInputs
    nozzle:   NozzleGeom
    chamber:  dict          # chamber pressure, volume, L* etc.
    mass_flow_kg_s: float


# ---------------------------------------------------------------------------
# Physics computations
# ---------------------------------------------------------------------------

def _get_propellant(name: str) -> dict:
    key = name.lower().replace("-", "_").replace(" ", "_")
    if key in PROPELLANTS:
        return PROPELLANTS[key]
    # keyword matching
    if "rp1" in key or "rp-1" in key or "kerosene" in key:
        return PROPELLANTS["lox_rp1"]
    if "lh2" in key or "hydrogen" in key:
        return PROPELLANTS["lox_lh2"]
    if "ipa" in key or "isopropanol" in key:
        return PROPELLANTS["lox_ipa"]
    return PROPELLANTS["lox_rp1"]  # default


def _area_ratio_from_mach(Me: float, gamma: float) -> float:
    """Isentropic area ratio A/A* = f(Mach, gamma)."""
    g = gamma
    t = (2 / (g + 1)) * (1 + (g - 1) / 2 * Me ** 2)
    return (1 / Me) * (t ** ((g + 1) / (2 * (g - 1))))


def _exit_mach(Pe_Pa: float, Pc_Pa: float, gamma: float, tol: float = 1e-6) -> float:
    """
    Solve exit Mach number from pressure ratio Pe/Pc via isentropic relation.
    Bisection search.
    """
    pr = Pe_Pa / Pc_Pa
    if pr <= 0 or pr >= 1:
        return 3.0  # fallback for vacuum / near-vacuum
    # isentropic: Pe/Pc = (1 + (g-1)/2 * Me^2)^(-g/(g-1))
    exp = -gamma / (gamma - 1)
    Me_lo, Me_hi = 1.0, 20.0
    for _ in range(60):
        Me_mid = 0.5 * (Me_lo + Me_hi)
        pr_mid = (1 + (gamma - 1) / 2 * Me_mid ** 2) ** exp
        if pr_mid > pr:
            Me_lo = Me_mid
        else:
            Me_hi = Me_mid
        if Me_hi - Me_lo < tol:
            break
    return 0.5 * (Me_lo + Me_hi)


def _thrust_coefficient(Me: float, Pe_Pa: float, Pc_Pa: float,
                         gamma: float, area_ratio: float) -> float:
    """
    Vacuum thrust coefficient Cf.
    Cf = sqrt(2*gamma^2/(gamma-1) * (2/(gamma+1))^((gamma+1)/(gamma-1))
              * (1 - (Pe/Pc)^((gamma-1)/gamma)))
         + (Pe/Pc) * (Ae/At)
    """
    g = gamma
    term1 = math.sqrt(
        2 * g ** 2 / (g - 1) *
        (2 / (g + 1)) ** ((g + 1) / (g - 1)) *
        (1 - (Pe_Pa / Pc_Pa) ** ((g - 1) / g))
    )
    term2 = (Pe_Pa / Pc_Pa) * area_ratio
    return term1 + term2


def compute_lre_nozzle(inp: LREInputs) -> LREGeom:
    """
    Derive nozzle geometry from thrust + chamber pressure.
    Returns LREGeom with all scalars needed for CAD generation.
    """
    prop = _get_propellant(inp.propellant)
    F_N  = inp.thrust_kN * 1000
    Pc   = inp.Pc_MPa * 1e6   # Pa
    Pe   = inp.Pe_kPa * 1000  # Pa
    g    = prop["gamma"]
    Isp  = prop["Isp_vac_s"]
    c_star = prop["c_star_m_s"]

    # Exit Mach and area ratio
    Me = _exit_mach(Pe, Pc, g)
    if inp.exit_area_ratio:
        AR = inp.exit_area_ratio
    else:
        AR = _area_ratio_from_mach(Me, g)
        AR = max(AR, 2.0)  # minimum expansion

    Cf = _thrust_coefficient(Me, Pe, Pc, g, AR)
    Cf = max(Cf, 0.5)  # guard against degenerate inputs

    # Throat area and radius
    At = F_N / (Cf * Pc)  # m²
    Rt = math.sqrt(At / math.pi) * 1000  # mm

    if inp.throat_r_mm:
        Rt = inp.throat_r_mm
        At = math.pi * (Rt / 1000) ** 2

    # Exit area and radius
    Ae = At * AR
    Re = math.sqrt(Ae / math.pi) * 1000  # mm

    # Entry (chamber-side) radius: typically 1.5× throat
    R_entry = Rt * 1.5  # mm

    # Convergent length from geometry
    conv_angle = math.radians(inp.conv_half_angle_deg)
    conv_length = (R_entry - Rt) / math.tan(conv_angle)  # mm

    # Divergent (nozzle) length — 80% bell approximation
    div_angle = math.radians(inp.div_half_angle_deg)
    div_length_conical = (Re - Rt) / math.tan(div_angle)
    div_length = div_length_conical * 0.80  # Rao 80% bell shortening

    total_length = conv_length + div_length

    # Wall thickness from hoop stress at throat (highest pressure + temp)
    mat_name = inp.wall_material.lower()
    if "copper" in mat_name and _COPPER:
        yield_MPa = _COPPER.yield_strength_MPa
        density   = _COPPER.density_kg_m3
    elif _INCONEL:
        yield_MPa = _INCONEL.yield_strength_MPa * 0.6  # 60% of RT yield at operating temp
        density   = _INCONEL.density_kg_m3
    else:
        yield_MPa = 420.0  # Inconel 718 hot strength fallback
        density   = 8220.0

    # Hoop stress: sigma = Pc * Rt / t → t = Pc * Rt / (sigma_allow)
    sigma_allow = yield_MPa / inp.safety_factor
    t_throat = (inp.Pc_MPa * 1e6 * Rt / 1000) / (sigma_allow * 1e6)  # m
    t_throat_mm = max(t_throat * 1000, 1.5)  # minimum 1.5mm wall

    # SFs
    sigma_actual = inp.Pc_MPa * 1e6 * (Rt / 1000) / (t_throat_mm / 1000) / 1e6  # MPa
    sf_throat = yield_MPa / sigma_actual if sigma_actual > 0 else 99.0
    # At exit the pressure is much lower → SF much higher
    sigma_exit = inp.Pe_kPa * 1000 * (Re / 1000) / (t_throat_mm / 1000) / 1e6
    sf_exit = yield_MPa / max(sigma_exit, 0.001)

    # Mass estimate (truncated cone + cylinder average)
    avg_r_m = (Rt + Re) / 2 / 1000
    area_lateral = math.pi * (Rt / 1000 + Re / 1000) * math.sqrt(
        (Re / 1000 - Rt / 1000) ** 2 + (div_length / 1000) ** 2
    )
    volume_shell = area_lateral * t_throat_mm / 1000
    mass_nozzle = density * volume_shell

    # Mass flow
    mdot = F_N / (Isp * 9.80665)

    # Chamber params (L* = 1.0m for LOX/RP-1)
    L_star = 1.0  # m (characteristic chamber length)
    V_chamber = L_star * At
    D_chamber = math.sqrt(4 * V_chamber / (math.pi * 0.3)) * 1000  # mm, assume L/D=0.3
    chamber = {
        "Pc_MPa":        inp.Pc_MPa,
        "L_star_m":      L_star,
        "volume_m3":     V_chamber,
        "diameter_mm":   D_chamber,
        "length_mm":     L_star / 0.3 * 1000,
    }

    warnings: list[str] = []
    if inp.thrust_kN > 500:
        warnings.append("Thrust > 500 kN: regenerative cooling strongly recommended.")
    if AR > 40:
        warnings.append(f"Area ratio {AR:.1f} is very high — verify altitude compensation.")
    if sf_throat < inp.safety_factor:
        warnings.append(f"Throat SF {sf_throat:.2f} < required {inp.safety_factor}. Increase wall thickness.")

    nozzle = NozzleGeom(
        throat_r_mm    = Rt,
        throat_area_m2 = At,
        exit_r_mm      = Re,
        exit_area_m2   = Ae,
        area_ratio     = AR,
        entry_r_mm     = R_entry,
        conv_length_mm = conv_length,
        div_length_mm  = div_length,
        total_length_mm= total_length,
        wall_mm        = t_throat_mm,
        mass_kg        = mass_nozzle,
        throat_sf      = sf_throat,
        exit_sf        = sf_exit,
        Cf             = Cf,
        c_star_m_s     = c_star,
        Isp_s          = Isp,
        mdot_kg_s      = mdot,
        warnings       = warnings,
    )

    return LREGeom(
        inputs          = inp,
        nozzle          = nozzle,
        chamber         = chamber,
        mass_flow_kg_s  = mdot,
    )


def compute_for_goal(goal: str, params: dict | None = None) -> dict:
    """
    Entry point used by the CEM pipeline orchestrator.
    Returns a flat dict of geometry scalars for plan["params"] injection.
    """
    inp_kwargs: dict = {}
    if params:
        float_fields = {
            "thrust_kN", "Pc_MPa", "Pe_kPa", "safety_factor",
            "throat_r_mm", "exit_area_ratio",
            "conv_half_angle_deg", "div_half_angle_deg",
        }
        str_fields = {"propellant", "wall_material"}
        bool_fields = {"regenerative"}
        for f in float_fields:
            if f in params and params[f] is not None:
                try:
                    inp_kwargs[f] = float(params[f])
                except (TypeError, ValueError):
                    pass
        for f in str_fields:
            if f in params and params[f]:
                inp_kwargs[f] = str(params[f])
        for f in bool_fields:
            if f in params and params[f] is not None:
                inp_kwargs[f] = bool(params[f])

    # Parse thrust/pressure from goal text if not in params
    import re
    if "thrust_kN" not in inp_kwargs:
        m = re.search(r'(\d+(?:\.\d+)?)\s*k[Nn]', goal)
        if m:
            inp_kwargs["thrust_kN"] = float(m.group(1))
    if "Pc_MPa" not in inp_kwargs:
        m = re.search(r'(\d+(?:\.\d+)?)\s*MPa', goal, re.I)
        if m:
            inp_kwargs["Pc_MPa"] = float(m.group(1))
        else:
            m = re.search(r'(\d+(?:\.\d+)?)\s*bar', goal, re.I)
            if m:
                inp_kwargs["Pc_MPa"] = float(m.group(1)) * 0.1

    inp  = LREInputs(**inp_kwargs)
    geom = compute_lre_nozzle(inp)
    n    = geom.nozzle

    return {
        "part_family":    "lre",
        # Nozzle geometry (CadQuery template keys)
        "throat_r_mm":    n.throat_r_mm,
        "exit_r_mm":      n.exit_r_mm,
        "entry_r_mm":     n.entry_r_mm,
        "conv_length_mm": n.conv_length_mm,
        "length_mm":      n.total_length_mm,
        "wall_mm":        n.wall_mm,
        # Derived
        "area_ratio":     n.area_ratio,
        "Cf":             n.Cf,
        "Isp_s":          n.Isp_s,
        "mdot_kg_s":      n.mdot_kg_s,
        "throat_sf":      n.throat_sf,
        # Chamber
        "chamber_Pc_MPa": geom.chamber["Pc_MPa"],
        "chamber_dia_mm": geom.chamber["diameter_mm"],
    }
