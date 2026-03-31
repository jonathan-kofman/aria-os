"""
cem_civil.py — CEM module for civil engineering design.

compute_for_goal(goal, params) → geometry dict consumed by the autocad generator
and optionally by cem_to_geometry for DXF/STEP coordination.

Covers: road geometry, storm drainage (Manning's), grading slopes,
utility burial, structural load paths, ADA compliance.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from cem.cem_core import Material

# ── Safety factors ─────────────────────────────────────────────────────────────
SF_ROAD_PAVEMENT   = 3.0   # structural number × safety
SF_PIPE_WALL       = 2.0   # D-load / computed load
SF_RETWALL_SLIDING = 1.5
SF_RETWALL_OVERTURNING = 2.0
SF_BRIDGE_DECK     = 3.5   # AASHTO HL-93


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class RoadGeometry:
    lane_width_ft: float = 12.0
    n_lanes: int = 2
    shoulder_width_ft: float = 8.0
    design_speed_mph: float = 45.0
    row_width_ft: float = 60.0
    cross_slope_pct: float = 2.0
    # Computed
    total_paved_width_ft: float = field(init=False)
    min_turning_radius_ft: float = field(init=False)
    min_sight_distance_ft: float = field(init=False)

    def __post_init__(self) -> None:
        self.total_paved_width_ft = self.lane_width_ft * self.n_lanes
        # AASHTO Green Book: SSD = 1.47V*t + V²/(30*f)  (t=2.5s, f=0.35@45mph)
        v = self.design_speed_mph
        t_react = 2.5
        f_brake = max(0.28, 0.40 - 0.003 * v)
        self.min_sight_distance_ft = (1.47 * v * t_react +
                                       v ** 2 / (30 * f_brake))
        # Minimum radius: R = V²/(15*(e+f))  e=0.08 max, f from AASHTO table
        e_max = 0.08
        f_lat = max(0.12, 0.18 - 0.001 * v)
        self.min_turning_radius_ft = v ** 2 / (15 * (e_max + f_lat))


@dataclass
class StormDrainResult:
    pipe_dia_in: float
    slope_ft_ft: float
    flow_cfs: float         # Manning's Q
    velocity_fps: float
    froude: float
    capacity_ratio: float   # Q_design / Q_full
    sf: float               # capacity / design Q
    passed: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class GradingResult:
    max_fill_slope: float   # H:V
    max_cut_slope: float
    min_bench_width_ft: float
    compaction_pct: float
    earthwork_cy: float
    sf_global_stability: float
    passed: bool
    warnings: list[str] = field(default_factory=list)


@dataclass
class RetainingWallResult:
    wall_height_ft: float
    base_width_ft: float
    toe_width_ft: float
    heel_width_ft: float
    footing_depth_ft: float
    sf_sliding: float
    sf_overturning: float
    bearing_pressure_psf: float
    passed: bool
    warnings: list[str] = field(default_factory=list)


# ── Manning's pipe flow ────────────────────────────────────────────────────────

def _mannings_full_flow(dia_in: float, slope: float, n: float = 0.013) -> tuple[float, float]:
    """
    Full-flow Q (cfs) and V (fps) for a circular pipe.
    Manning's:  Q = (1.486/n) * A * R^(2/3) * S^(1/2)
    """
    dia_ft = dia_in / 12.0
    area = math.pi * (dia_ft / 2) ** 2      # ft²
    radius = dia_ft / 4                      # hydraulic radius for full circle
    q_full = (1.486 / n) * area * (radius ** (2.0 / 3.0)) * (slope ** 0.5)
    v_full = q_full / area
    return q_full, v_full


def design_storm_pipe(
    flow_cfs: float,
    slope_ft_ft: float = 0.005,
    n_manning: float = 0.013,
    max_capacity_ratio: float = 0.80,
) -> StormDrainResult:
    """
    Size a storm sewer pipe using Manning's equation.
    Iterates standard pipe sizes until capacity_ratio ≤ max_capacity_ratio.
    """
    standard_sizes_in = [12, 15, 18, 21, 24, 27, 30, 36, 42, 48, 54, 60, 66, 72]
    warnings: list[str] = []

    if slope_ft_ft < 0.003:
        warnings.append(f"Slope {slope_ft_ft:.4f} < 0.003 minimum — check grade")

    for dia in standard_sizes_in:
        q_full, v_full = _mannings_full_flow(dia, slope_ft_ft, n_manning)
        ratio = flow_cfs / q_full
        if ratio <= max_capacity_ratio:
            froude = v_full / math.sqrt(32.2 * (dia / 12.0) / 4)
            if v_full < 2.0:
                warnings.append(f"Velocity {v_full:.2f} fps < 2.0 minimum — self-cleaning concern")
            if v_full > 10.0:
                warnings.append(f"Velocity {v_full:.2f} fps > 10.0 — erosion risk at outfall")
            return StormDrainResult(
                pipe_dia_in=float(dia),
                slope_ft_ft=slope_ft_ft,
                flow_cfs=flow_cfs,
                velocity_fps=v_full,
                froude=froude,
                capacity_ratio=ratio,
                sf=1.0 / ratio,
                passed=True,
                warnings=warnings,
            )

    # Nothing fits — return max size with warning
    dia = standard_sizes_in[-1]
    q_full, v_full = _mannings_full_flow(dia, slope_ft_ft, n_manning)
    warnings.append(f"Design flow {flow_cfs:.1f} cfs exceeds 72\" pipe — box culvert required")
    return StormDrainResult(
        pipe_dia_in=float(dia),
        slope_ft_ft=slope_ft_ft,
        flow_cfs=flow_cfs,
        velocity_fps=v_full,
        froude=v_full / math.sqrt(32.2 * (dia / 12.0) / 4),
        capacity_ratio=flow_cfs / q_full,
        sf=q_full / flow_cfs,
        passed=False,
        warnings=warnings,
    )


# ── Grading ────────────────────────────────────────────────────────────────────

def compute_grading(
    fill_height_ft: float = 10.0,
    cut_depth_ft: float = 8.0,
    soil_type: str = "clay",   # "sand", "clay", "rock"
    seismic_zone: int = 1,
) -> GradingResult:
    """
    Compute grading slopes, benching requirements, and global stability SF.
    """
    warnings: list[str] = []
    # OSHA / geotechnical slope limits
    slope_table = {
        "rock":  {"cut": 0.25, "fill": 0.5},    # 4:1 H:V cut / 2:1 H:V fill
        "sand":  {"cut": 1.5,  "fill": 1.5},
        "clay":  {"cut": 1.0,  "fill": 2.0},
    }
    slopes = slope_table.get(soil_type.lower(), slope_table["clay"])
    max_fill_slope = slopes["fill"]
    max_cut_slope  = slopes["cut"]

    # Bench every 20 ft of cut/fill per standard practice
    min_bench_width = 8.0  # ft

    # Compaction
    compaction_pct = 95.0 if soil_type != "rock" else 100.0

    # Rough earthwork (trapezoidal prism, arbitrary 200-ft run)
    run_ft = 200.0
    avg_section_sf = (fill_height_ft * max_fill_slope + cut_depth_ft * max_cut_slope) * run_ft
    earthwork_cy = avg_section_sf * run_ft / 27.0

    # Simplified Bishop slope stability SF
    c_psf = {"sand": 0, "clay": 500, "rock": 2000}.get(soil_type.lower(), 500)
    phi_deg = {"sand": 32, "clay": 20, "rock": 40}.get(soil_type.lower(), 20)
    phi = math.radians(phi_deg)
    gamma_pcf = {"sand": 110, "clay": 120, "rock": 150}.get(soil_type.lower(), 120)
    # Bishop simplified: SF ≈ (c + gamma*H*cos²β*tanφ) / (gamma*H*sinβ*cosβ)
    beta = math.atan(1.0 / max_fill_slope)
    h = max(fill_height_ft, cut_depth_ft)
    normal = gamma_pcf * h * math.cos(beta) ** 2
    driving = gamma_pcf * h * math.sin(beta) * math.cos(beta)
    sf_stability = (c_psf + normal * math.tan(phi)) / max(driving, 1e-6)

    # Seismic reduction (simplified pseudo-static)
    if seismic_zone >= 3:
        kh = 0.15 * seismic_zone
        seismic_penalty = gamma_pcf * h * kh * math.cos(beta)
        sf_stability = (c_psf + normal * math.tan(phi)) / max(driving + seismic_penalty, 1e-6)
        warnings.append(f"Seismic zone {seismic_zone} — pseudo-static SF applied")

    if sf_stability < 1.5:
        warnings.append(f"Global stability SF={sf_stability:.2f} < 1.5 — flatten slopes or add geosynthetics")

    return GradingResult(
        max_fill_slope=max_fill_slope,
        max_cut_slope=max_cut_slope,
        min_bench_width_ft=min_bench_width,
        compaction_pct=compaction_pct,
        earthwork_cy=earthwork_cy,
        sf_global_stability=sf_stability,
        passed=sf_stability >= 1.5,
        warnings=warnings,
    )


# ── Retaining wall ─────────────────────────────────────────────────────────────

def design_retaining_wall(
    height_ft: float = 8.0,
    surcharge_psf: float = 250.0,   # live load surcharge
    soil_type: str = "clay",
    footing_bearing_psf: float = 3000.0,
) -> RetainingWallResult:
    """
    Gravity retaining wall preliminary sizing (Coulomb active earth pressure).
    Returns base/toe/heel widths + stability SF.
    """
    warnings: list[str] = []
    phi_deg = {"sand": 32, "clay": 20, "rock": 40}.get(soil_type.lower(), 25)
    phi = math.radians(phi_deg)
    gamma_soil = {"sand": 110, "clay": 120, "rock": 150}.get(soil_type.lower(), 120)
    # Coulomb Ka
    ka = (1 - math.sin(phi)) / (1 + math.sin(phi))
    # Total active thrust (triangle + rectangle from surcharge)
    Pa_triangle = 0.5 * ka * gamma_soil * height_ft ** 2     # lb/ft
    Pa_surcharge = ka * surcharge_psf * height_ft             # lb/ft
    Pa = Pa_triangle + Pa_surcharge

    # Point of application from base
    y_tri = height_ft / 3.0
    y_sur = height_ft / 2.0
    Pa_moment = Pa_triangle * y_tri + Pa_surcharge * y_sur

    # Wall geometry: B ≈ 0.5–0.7 H for concrete gravity
    base_w = 0.6 * height_ft
    footing_d = max(1.5, 0.15 * height_ft)
    toe_w = 0.15 * base_w
    heel_w = base_w - toe_w - 0.3   # stem width = 0.3 ft

    # Weight of wall (concrete 150 pcf) + soil on heel
    w_conc = 150 * footing_d * base_w              # footing
    w_stem = 150 * 0.3 * height_ft                 # stem
    w_soil = gamma_soil * heel_w * height_ft        # backfill on heel
    W_total = w_conc + w_stem + w_soil

    # Resisting moment about toe
    x_conc  = base_w / 2
    x_stem  = toe_w + 0.15
    x_soil  = toe_w + 0.3 + heel_w / 2
    M_resist = w_conc * x_conc + w_stem * x_stem + w_soil * x_soil
    M_overturn = Pa_moment

    sf_overturning = M_resist / max(M_overturn, 1e-6)
    sf_sliding = (0.5 * W_total) / max(Pa, 1e-6)

    # Bearing pressure
    e = base_w / 2 - (M_resist - M_overturn) / max(W_total, 1e-6)
    q_max = W_total / base_w * (1 + 6 * e / base_w)

    if sf_overturning < SF_RETWALL_OVERTURNING:
        warnings.append(f"OT SF={sf_overturning:.2f} < {SF_RETWALL_OVERTURNING} — increase base width")
    if sf_sliding < SF_RETWALL_SLIDING:
        warnings.append(f"Sliding SF={sf_sliding:.2f} < {SF_RETWALL_SLIDING} — add shear key")
    if q_max > footing_bearing_psf:
        warnings.append(f"Bearing pressure {q_max:.0f} psf > {footing_bearing_psf} psf allowable")

    passed = (sf_overturning >= SF_RETWALL_OVERTURNING and
              sf_sliding >= SF_RETWALL_SLIDING and
              q_max <= footing_bearing_psf)

    return RetainingWallResult(
        wall_height_ft=height_ft,
        base_width_ft=base_w,
        toe_width_ft=toe_w,
        heel_width_ft=heel_w,
        footing_depth_ft=footing_d,
        sf_sliding=sf_sliding,
        sf_overturning=sf_overturning,
        bearing_pressure_psf=q_max,
        passed=passed,
        warnings=warnings,
    )


# ── Rational method (runoff) ───────────────────────────────────────────────────

def rational_method_flow(
    area_acres: float,
    c_runoff: float = 0.7,         # runoff coefficient
    i_in_per_hr: float = 3.0,      # rainfall intensity
) -> float:
    """Q = C * i * A  (cfs)"""
    return c_runoff * i_in_per_hr * area_acres


# ── Utility burial ─────────────────────────────────────────────────────────────

def check_utility_burial(
    utility_type: str,
    cover_ft: float,
    state: str = "national",
) -> dict[str, Any]:
    """
    Check minimum cover requirement for a buried utility.
    Returns {required_ft, provided_ft, passed, note}.
    """
    from aria_os.autocad.standards_library import get_standard
    std = get_standard(state, "drainage")
    min_cover = std.get("drainage", {}).get("min_pipe_cover_ft", 2.0)

    # Specific overrides by utility type
    type_map = {
        "water":    {"cover_ft": 4.0,  "note": "AWWA / DOT min cover"},
        "sewer":    {"cover_ft": 3.0,  "note": "DOT min cover"},
        "gas":      {"cover_ft": 3.0,  "note": "NFPA 54 / DOT"},
        "electric": {"cover_ft": 2.5,  "note": "NEC Table 300.5"},
        "fiber":    {"cover_ft": 2.0,  "note": "Telco standard"},
        "storm":    {"cover_ft": min_cover, "note": "State DOT"},
    }
    req = type_map.get(utility_type.lower(), {"cover_ft": 2.0, "note": "General"})
    passed = cover_ft >= req["cover_ft"]
    return {
        "utility_type": utility_type,
        "required_ft": req["cover_ft"],
        "provided_ft": cover_ft,
        "passed": passed,
        "note": req["note"],
    }


# ── CEM entry point ───────────────────────────────────────────────────────────

def compute_for_goal(goal: str, params: dict) -> dict:
    """
    CEM entry point called by aria_os.cem_generator.resolve_and_compute().

    Returns a flat dict of physics-derived geometry parameters + SF values
    for injection into the DXF generator prompt.
    """
    goal_lower = goal.lower()
    result: dict[str, Any] = {"cem_module": "cem_civil"}

    # --- Road geometry -------------------------------------------------------
    if any(kw in goal_lower for kw in ["road", "street", "highway", "lane"]):
        rg = RoadGeometry(
            lane_width_ft=params.get("lane_width_ft", 12.0),
            n_lanes=params.get("n_lanes", 2),
            shoulder_width_ft=params.get("shoulder_width_ft", 8.0),
            design_speed_mph=params.get("design_speed_mph", 45.0),
            row_width_ft=params.get("row_width_ft", 60.0),
        )
        result.update({
            "lane_width_ft": rg.lane_width_ft,
            "n_lanes": rg.n_lanes,
            "total_paved_width_ft": rg.total_paved_width_ft,
            "shoulder_width_ft": rg.shoulder_width_ft,
            "row_width_ft": rg.row_width_ft,
            "design_speed_mph": rg.design_speed_mph,
            "min_sight_distance_ft": round(rg.min_sight_distance_ft, 1),
            "min_turning_radius_ft": round(rg.min_turning_radius_ft, 1),
            "sf_road": SF_ROAD_PAVEMENT,
        })

    # --- Storm drainage ------------------------------------------------------
    if any(kw in goal_lower for kw in ["drain", "storm", "pipe", "culvert", "sewer"]):
        area = params.get("drainage_area_acres", 5.0)
        c = params.get("runoff_coefficient", 0.70)
        i_in = params.get("rainfall_intensity_in_hr", 3.0)
        slope = params.get("pipe_slope_ft_ft", 0.005)
        q = rational_method_flow(area, c, i_in)
        pipe = design_storm_pipe(q, slope)
        result.update({
            "design_flow_cfs": round(q, 2),
            "pipe_dia_in": pipe.pipe_dia_in,
            "pipe_slope_ft_ft": pipe.slope_ft_ft,
            "pipe_velocity_fps": round(pipe.velocity_fps, 2),
            "pipe_capacity_ratio": round(pipe.capacity_ratio, 3),
            "sf_pipe": round(pipe.sf, 2),
            "pipe_passed": pipe.passed,
            "pipe_warnings": pipe.warnings,
        })

    # --- Grading / earthwork --------------------------------------------------
    if any(kw in goal_lower for kw in ["grade", "grading", "cut", "fill", "slope", "earthwork"]):
        grading = compute_grading(
            fill_height_ft=params.get("fill_height_ft", 10.0),
            cut_depth_ft=params.get("cut_depth_ft", 8.0),
            soil_type=params.get("soil_type", "clay"),
            seismic_zone=params.get("seismic_zone", 1),
        )
        result.update({
            "max_fill_slope_hv": grading.max_fill_slope,
            "max_cut_slope_hv": grading.max_cut_slope,
            "min_bench_width_ft": grading.min_bench_width_ft,
            "compaction_pct": grading.compaction_pct,
            "earthwork_cy": round(grading.earthwork_cy, 0),
            "sf_global_stability": round(grading.sf_global_stability, 2),
            "grading_passed": grading.passed,
            "grading_warnings": grading.warnings,
        })

    # --- Retaining wall -------------------------------------------------------
    if any(kw in goal_lower for kw in ["retaining wall", "retwall", "retain"]):
        rw = design_retaining_wall(
            height_ft=params.get("wall_height_ft", 8.0),
            surcharge_psf=params.get("surcharge_psf", 250.0),
            soil_type=params.get("soil_type", "clay"),
            footing_bearing_psf=params.get("bearing_capacity_psf", 3000.0),
        )
        result.update({
            "retwall_height_ft": rw.wall_height_ft,
            "retwall_base_width_ft": round(rw.base_width_ft, 2),
            "retwall_footing_depth_ft": round(rw.footing_depth_ft, 2),
            "sf_sliding": round(rw.sf_sliding, 2),
            "sf_overturning": round(rw.sf_overturning, 2),
            "bearing_pressure_psf": round(rw.bearing_pressure_psf, 0),
            "retwall_passed": rw.passed,
            "retwall_warnings": rw.warnings,
        })

    # Always pass through state for downstream use
    result["state"] = params.get("state", "national")
    result["discipline"] = _infer_discipline(goal_lower)

    return result


def _infer_discipline(goal_lower: str) -> str:
    if any(k in goal_lower for k in ["road", "street", "highway", "lane"]):
        return "transportation"
    if any(k in goal_lower for k in ["drain", "storm", "pipe", "culvert"]):
        return "drainage"
    if any(k in goal_lower for k in ["grade", "grading", "cut", "fill"]):
        return "grading"
    if any(k in goal_lower for k in ["water", "gas", "electric", "utility"]):
        return "utilities"
    return "site"
