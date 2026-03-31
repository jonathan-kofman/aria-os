"""
standards_library.py — Civil engineering design standards by discipline and state.

National defaults follow AASHTO Green Book (7th Ed.), FHWA, MUTCD, ASCE 7-22,
ADA Standards for Accessible Design, and PROWAG.

State entries override only the parameters that differ from national defaults.
"""
from __future__ import annotations

# ── National defaults ─────────────────────────────────────────────────────────
_NATIONAL: dict = {
    "name": "National — AASHTO / FHWA / ASCE / ADA",
    "source": "AASHTO Green Book 7th Ed.; FHWA Design Standards; ASCE 7-22; ADA 2010",
    "roads": {
        "min_lane_width_ft": 12,
        "min_lane_width_local_ft": 10,
        "min_lane_width_rural_ft": 11,
        "shoulder_width_arterial_ft": 8,
        "shoulder_width_collector_ft": 6,
        "shoulder_width_local_ft": 2,
        "max_grade_flat_pct": 5,
        "max_grade_rolling_pct": 7,
        "max_grade_mountainous_pct": 12,
        "max_superelevation_pct": 8,
        "min_curve_radius_30mph_ft": 273,
        "min_curve_radius_35mph_ft": 392,
        "min_curve_radius_40mph_ft": 511,
        "min_curve_radius_45mph_ft": 709,
        "min_curve_radius_50mph_ft": 926,
        "min_curve_radius_55mph_ft": 1190,
        "min_curve_radius_60mph_ft": 1432,
        "min_curve_radius_65mph_ft": 1792,
        "min_curve_radius_70mph_ft": 2083,
        "min_sight_distance_stop_30mph_ft": 200,
        "min_sight_distance_stop_45mph_ft": 360,
        "min_sight_distance_stop_60mph_ft": 570,
        "min_sight_distance_stop_75mph_ft": 820,
        "bike_lane_width_ft": 5,
        "bike_lane_width_min_ft": 4,
        "sidewalk_width_min_ft": 5,
        "curb_radius_intersection_ft": 15,
        "design_vehicle": "WB-67",
    },
    "drainage": {
        "min_pipe_dia_storm_in": 15,
        "min_pipe_dia_sanitary_in": 8,
        "min_pipe_cover_ft": 2.0,
        "min_pipe_cover_frost_ft": 3.0,
        "max_pipe_velocity_fps": 10.0,
        "min_pipe_velocity_fps": 2.0,
        "design_storm_minor_year": 10,
        "design_storm_major_year": 100,
        "runoff_method": "rational",
        "mannings_n_concrete": 0.013,
        "mannings_n_hdpe": 0.012,
        "mannings_n_corrugated_metal": 0.024,
        "mannings_n_pvc": 0.011,
        "mannings_n_open_channel_earth": 0.030,
        "mannings_n_open_channel_grass": 0.035,
        "inlet_efficiency_gutter": 0.80,
        "max_spread_arterial_ft": 4,
        "max_spread_collector_ft": 6,
    },
    "grading": {
        "max_cut_slope_hv": "2:1",
        "max_fill_slope_hv": "3:1",
        "max_fill_slope_embankment_hv": "2:1",
        "min_finished_grade_pct": 0.5,
        "max_finished_grade_pct": 10.0,
        "retwall_max_height_unreinforced_ft": 4,
        "bench_width_min_ft": 10,
        "bench_required_height_ft": 20,
        "min_freeboard_ft": 1.0,
    },
    "structural": {
        "code": "IBC 2021",
        "concrete_fc_psi": 4000,
        "rebar_fy_ksi": 60,
        "wind_exposure": "B",
        "seismic_design_category": "B",
        "frost_depth_in": 36,
        "live_load_floor_psf": 50,
        "live_load_roof_psf": 20,
        "dead_load_psf": 15,
    },
    "ada": {
        "max_ramp_slope": 0.0833,
        "max_cross_slope": 0.02,
        "min_landing_width_ft": 5.0,
        "min_landing_length_ft": 5.0,
        "min_clear_width_ft": 5.0,
        "detectable_warning_depth_ft": 2.0,
        "detectable_warning_width_ft": 4.0,
        "max_vertical_change_in": 0.25,
        "max_running_slope_walkway": 0.05,
        "standard": "ADA 2010 + PROWAG 2023",
    },
}

# ── State overrides ───────────────────────────────────────────────────────────
# Only include parameters that differ from _NATIONAL defaults.
# Seismic: A=lowest, B, C, D, E=highest
# Wind speed: basic (3-sec gust, mph) for Risk Category II

_STATE_OVERRIDES: dict[str, dict] = {
    "AL": {
        "name": "Alabama DOT",
        "source": "ALDOT Highway Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 130, "frost_depth_in": 6},
        "drainage": {"design_storm_minor_year": 10, "min_pipe_cover_ft": 1.5},
    },
    "AK": {
        "name": "Alaska DOT & PF",
        "source": "Alaska DOT Highway Preconstruction Manual",
        "structural": {"seismic_design_category": "D", "wind_speed_mph": 150, "frost_depth_in": 96},
        "drainage": {"min_pipe_cover_ft": 6.0, "min_pipe_cover_frost_ft": 8.0, "design_storm_minor_year": 10},
        "roads": {"max_grade_mountainous_pct": 14, "min_lane_width_ft": 11},
    },
    "AZ": {
        "name": "ADOT — Arizona DOT",
        "source": "ADOT Roadway Design Guidelines",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 100, "frost_depth_in": 6},
        "drainage": {"min_pipe_cover_ft": 1.5, "design_storm_minor_year": 10},
    },
    "AR": {
        "name": "ARDOT — Arkansas DOT",
        "source": "ARDOT Design Division Standards",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 10},
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "CA": {
        "name": "Caltrans — California DOT",
        "source": "Caltrans Highway Design Manual (HDM), 6th Ed.",
        "structural": {"seismic_design_category": "D", "wind_speed_mph": 110, "frost_depth_in": 12},
        "drainage": {"design_storm_minor_year": 10, "design_storm_major_year": 100},
        "roads": {
            "min_lane_width_ft": 12,
            "bike_lane_width_ft": 6,
            "max_superelevation_pct": 10,
            "min_curve_radius_45mph_ft": 770,
        },
        "ada": {"standard": "ADA 2010 + Caltrans Standard Plans A88A"},
    },
    "CO": {
        "name": "CDOT — Colorado DOT",
        "source": "CDOT Roadway Design Guide",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 115, "frost_depth_in": 36},
        "drainage": {"min_pipe_cover_ft": 3.0, "design_storm_minor_year": 10},
        "roads": {"max_grade_mountainous_pct": 14},
    },
    "CT": {
        "name": "ConnDOT — Connecticut DOT",
        "source": "ConnDOT Highway Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 42},
        "drainage": {"min_pipe_cover_ft": 4.0},
    },
    "DE": {
        "name": "DelDOT — Delaware DOT",
        "source": "DelDOT Road Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 120, "frost_depth_in": 30},
        "drainage": {"min_pipe_cover_ft": 3.0},
    },
    "FL": {
        "name": "FDOT — Florida DOT",
        "source": "FDOT Plans Preparation Manual Vol. 1",
        "structural": {
            "seismic_design_category": "A",
            "wind_speed_mph": 140,
            "wind_exposure": "C",
            "frost_depth_in": 0,
        },
        "drainage": {
            "min_pipe_cover_ft": 1.0,
            "min_pipe_cover_frost_ft": 1.0,
            "design_storm_minor_year": 10,
            "design_storm_major_year": 100,
        },
        "roads": {"bike_lane_width_ft": 5, "sidewalk_width_min_ft": 5},
    },
    "GA": {
        "name": "GDOT — Georgia DOT",
        "source": "GDOT Design Policy Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 130, "frost_depth_in": 6},
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "HI": {
        "name": "HDOT — Hawaii DOT",
        "source": "HDOT Highways Design Manual",
        "structural": {
            "seismic_design_category": "D",
            "wind_speed_mph": 130,
            "wind_exposure": "D",
            "frost_depth_in": 0,
        },
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "ID": {
        "name": "ITD — Idaho Transportation Dept",
        "source": "ITD Highway Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 110, "frost_depth_in": 36},
        "drainage": {"min_pipe_cover_ft": 3.5},
        "roads": {"max_grade_mountainous_pct": 14},
    },
    "IL": {
        "name": "IDOT — Illinois DOT",
        "source": "IDOT Bureau of Design and Environment Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 105, "frost_depth_in": 42},
        "drainage": {"min_pipe_cover_ft": 4.0},
    },
    "IN": {
        "name": "INDOT — Indiana DOT",
        "source": "INDOT Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 105, "frost_depth_in": 42},
        "drainage": {"min_pipe_cover_ft": 3.5},
    },
    "IA": {
        "name": "Iowa DOT",
        "source": "Iowa DOT Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 105, "frost_depth_in": 60},
        "drainage": {"min_pipe_cover_ft": 5.0, "min_pipe_cover_frost_ft": 6.0},
    },
    "KS": {
        "name": "KDOT — Kansas DOT",
        "source": "KDOT Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 115, "frost_depth_in": 24},
        "drainage": {"min_pipe_cover_ft": 2.5},
    },
    "KY": {
        "name": "KYTC — Kentucky Transportation Cabinet",
        "source": "KYTC Highway Design Guidance",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 105, "frost_depth_in": 18},
        "drainage": {"min_pipe_cover_ft": 2.0},
    },
    "LA": {
        "name": "LADOTD — Louisiana DOTD",
        "source": "LADOTD Road Design Procedures Manual",
        "structural": {
            "seismic_design_category": "B",
            "wind_speed_mph": 140,
            "wind_exposure": "D",
            "frost_depth_in": 0,
        },
        "drainage": {"min_pipe_cover_ft": 1.0, "min_pipe_cover_frost_ft": 1.0},
    },
    "ME": {
        "name": "MaineDOT",
        "source": "MaineDOT Bridge Design Guide / Highway Design Guide",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 120, "frost_depth_in": 60},
        "drainage": {"min_pipe_cover_ft": 5.0, "min_pipe_cover_frost_ft": 6.5},
    },
    "MD": {
        "name": "MDOT SHA — Maryland DOT State Highway Admin",
        "source": "MDOT SHA Highway Design Guidelines",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 30},
        "drainage": {"min_pipe_cover_ft": 2.5},
    },
    "MA": {
        "name": "MassDOT",
        "source": "MassDOT Project Development and Design Guide",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 120, "frost_depth_in": 48},
        "drainage": {"min_pipe_cover_ft": 4.5},
    },
    "MI": {
        "name": "MDOT — Michigan DOT",
        "source": "MDOT Road Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 105, "frost_depth_in": 42},
        "drainage": {"min_pipe_cover_ft": 4.0},
    },
    "MN": {
        "name": "MnDOT — Minnesota DOT",
        "source": "MnDOT Road Design Manual",
        "structural": {"seismic_design_category": "A", "wind_speed_mph": 105, "frost_depth_in": 80},
        "drainage": {"min_pipe_cover_ft": 6.5, "min_pipe_cover_frost_ft": 7.5},
    },
    "MS": {
        "name": "MDOT — Mississippi DOT",
        "source": "MDOT Highway Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 130, "frost_depth_in": 6},
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "MO": {
        "name": "MoDOT — Missouri DOT",
        "source": "MoDOT Engineering Policy Guide",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 110, "frost_depth_in": 30},
        "drainage": {"min_pipe_cover_ft": 2.5},
    },
    "MT": {
        "name": "MDT — Montana DOT",
        "source": "MDT Road Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 72},
        "drainage": {"min_pipe_cover_ft": 6.0, "min_pipe_cover_frost_ft": 7.0},
        "roads": {"max_grade_mountainous_pct": 14},
    },
    "NE": {
        "name": "NDOR — Nebraska DOT",
        "source": "NDOR Road Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 115, "frost_depth_in": 42},
        "drainage": {"min_pipe_cover_ft": 4.0},
    },
    "NV": {
        "name": "NDOT — Nevada DOT",
        "source": "NDOT Roadway Design Guidance Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 105, "frost_depth_in": 18},
        "drainage": {"min_pipe_cover_ft": 2.0},
    },
    "NH": {
        "name": "NHDOT — New Hampshire DOT",
        "source": "NHDOT Highway Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 120, "frost_depth_in": 60},
        "drainage": {"min_pipe_cover_ft": 5.5},
    },
    "NJ": {
        "name": "NJDOT — New Jersey DOT",
        "source": "NJDOT Roadway Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 36},
        "drainage": {"min_pipe_cover_ft": 3.0},
    },
    "NM": {
        "name": "NMDOT — New Mexico DOT",
        "source": "NMDOT Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 105, "frost_depth_in": 12},
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "NY": {
        "name": "NYSDOT — New York State DOT",
        "source": "NYSDOT Highway Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 48},
        "drainage": {"min_pipe_cover_ft": 4.5, "design_storm_minor_year": 10},
        "roads": {"min_lane_width_ft": 11, "bike_lane_width_ft": 6},
    },
    "NC": {
        "name": "NCDOT — North Carolina DOT",
        "source": "NCDOT Roadway Standard Drawings",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 130, "frost_depth_in": 12},
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "ND": {
        "name": "NDDOT — North Dakota DOT",
        "source": "NDDOT Design Manual",
        "structural": {"seismic_design_category": "A", "wind_speed_mph": 115, "frost_depth_in": 84},
        "drainage": {"min_pipe_cover_ft": 7.0, "min_pipe_cover_frost_ft": 8.0},
    },
    "OH": {
        "name": "ODOT — Ohio DOT",
        "source": "ODOT Location and Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 105, "frost_depth_in": 36},
        "drainage": {"min_pipe_cover_ft": 3.0},
    },
    "OK": {
        "name": "ODOT — Oklahoma DOT",
        "source": "ODOT Road Design Standards",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 115, "frost_depth_in": 18},
        "drainage": {"min_pipe_cover_ft": 2.0},
    },
    "OR": {
        "name": "ODOT — Oregon DOT",
        "source": "ODOT Highway Design Manual",
        "structural": {"seismic_design_category": "D", "wind_speed_mph": 115, "frost_depth_in": 24},
        "drainage": {"min_pipe_cover_ft": 2.5},
        "roads": {"max_grade_mountainous_pct": 14, "bike_lane_width_ft": 6},
    },
    "PA": {
        "name": "PennDOT — Pennsylvania DOT",
        "source": "PennDOT Design Manual Part 2",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 115, "frost_depth_in": 42},
        "drainage": {"min_pipe_cover_ft": 3.5, "design_storm_minor_year": 10},
    },
    "RI": {
        "name": "RIDOT — Rhode Island DOT",
        "source": "RIDOT Road and Bridge Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 120, "frost_depth_in": 42},
        "drainage": {"min_pipe_cover_ft": 4.0},
    },
    "SC": {
        "name": "SCDOT — South Carolina DOT",
        "source": "SCDOT Road Design Standards",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 130, "frost_depth_in": 6},
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "SD": {
        "name": "SDDOT — South Dakota DOT",
        "source": "SDDOT Road Design Manual",
        "structural": {"seismic_design_category": "A", "wind_speed_mph": 115, "frost_depth_in": 60},
        "drainage": {"min_pipe_cover_ft": 5.5},
    },
    "TN": {
        "name": "TDOT — Tennessee DOT",
        "source": "TDOT Highway Design Guidelines",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 110, "frost_depth_in": 12},
        "drainage": {"min_pipe_cover_ft": 1.5},
    },
    "TX": {
        "name": "TxDOT — Texas DOT",
        "source": "TxDOT Roadway Design Manual",
        "structural": {
            "seismic_design_category": "A",
            "wind_speed_mph": 130,
            "wind_exposure": "B",
            "frost_depth_in": 6,
        },
        "drainage": {
            "min_pipe_cover_ft": 1.5,
            "min_pipe_cover_frost_ft": 1.5,
            "design_storm_minor_year": 10,
            "design_storm_major_year": 100,
            "runoff_method": "rational",
        },
        "roads": {
            "min_lane_width_ft": 12,
            "shoulder_width_arterial_ft": 10,
            "max_grade_flat_pct": 4,
        },
    },
    "UT": {
        "name": "UDOT — Utah DOT",
        "source": "UDOT Road Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 30},
        "drainage": {"min_pipe_cover_ft": 3.0},
    },
    "VT": {
        "name": "VTrans — Vermont Agency of Transportation",
        "source": "VTrans Highway Engineering Guidelines",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 60},
        "drainage": {"min_pipe_cover_ft": 5.5},
    },
    "VA": {
        "name": "VDOT — Virginia DOT",
        "source": "VDOT Road Design Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 24},
        "drainage": {"min_pipe_cover_ft": 2.5},
    },
    "WA": {
        "name": "WSDOT — Washington State DOT",
        "source": "WSDOT Design Manual M22-01",
        "structural": {"seismic_design_category": "D", "wind_speed_mph": 115, "frost_depth_in": 24},
        "drainage": {"min_pipe_cover_ft": 2.5},
        "roads": {"bike_lane_width_ft": 6, "max_grade_mountainous_pct": 14},
    },
    "WV": {
        "name": "WVDOH — West Virginia DOH",
        "source": "WVDOH Design Directive Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 105, "frost_depth_in": 30},
        "drainage": {"min_pipe_cover_ft": 2.5},
    },
    "WI": {
        "name": "WisDOT — Wisconsin DOT",
        "source": "WisDOT Facilities Development Manual",
        "structural": {"seismic_design_category": "A", "wind_speed_mph": 105, "frost_depth_in": 60},
        "drainage": {"min_pipe_cover_ft": 5.5},
    },
    "WY": {
        "name": "WYDOT — Wyoming DOT",
        "source": "WYDOT Road Design Manual",
        "structural": {"seismic_design_category": "B", "wind_speed_mph": 115, "frost_depth_in": 48},
        "drainage": {"min_pipe_cover_ft": 4.5},
        "roads": {"max_grade_mountainous_pct": 14},
    },
    "DC": {
        "name": "DDOT — DC DOT",
        "source": "DDOT Design and Engineering Manual",
        "structural": {"seismic_design_category": "C", "wind_speed_mph": 115, "frost_depth_in": 24},
        "drainage": {"min_pipe_cover_ft": 2.5},
        "roads": {"bike_lane_width_ft": 6, "sidewalk_width_min_ft": 8},
    },
}


def get_standard(state: str | None, discipline: str | None = None) -> dict:
    """
    Get design standard for a state (2-letter code) and optional discipline.
    Falls back to national defaults when state not found or override absent.
    state=None → national defaults only.
    discipline=None → full standard dict.
    """
    import copy
    import re

    state_key = (state or "").strip().upper()[:2] if state else None

    # Deep-merge national + state overrides
    result: dict = copy.deepcopy(_NATIONAL)

    if state_key and state_key in _STATE_OVERRIDES:
        override = _STATE_OVERRIDES[state_key]
        result["name"] = override.get("name", result["name"])
        result["source"] = override.get("source", result["source"])
        result["state"] = state_key
        for disc_key in ("roads", "drainage", "grading", "structural", "ada"):
            if disc_key in override:
                result[disc_key].update(override[disc_key])
    elif state_key:
        result["state"] = state_key
        result["source"] += f" (no state-specific override for {state_key} — national defaults apply)"
    else:
        result["state"] = None

    if discipline:
        d = discipline.lower()
        return result.get(d, result)
    return result


def list_standards(discipline: str | None = None) -> list[str]:
    """List all available state codes plus 'national'."""
    states = ["national"] + sorted(_STATE_OVERRIDES.keys())
    return states


def get_pipe_design(
    state: str,
    pipe_dia_in: float,
    slope_pct: float,
    material: str = "concrete",
) -> dict:
    """
    Compute Manning's pipe flow and velocity. Check min/max velocity compliance.
    Uses Manning's equation: Q = (1.486/n)*A*R^(2/3)*S^(1/2)  [imperial, full-flow]
    """
    import math
    std = get_standard(state, "drainage")
    n_key = f"mannings_n_{material.lower().replace(' ','_')}"
    n = std.get(n_key, std.get("mannings_n_concrete", 0.013))

    dia_ft = pipe_dia_in / 12.0
    area = math.pi * (dia_ft / 2) ** 2
    rh = dia_ft / 4  # hydraulic radius for full circle
    s = slope_pct / 100.0
    q_cfs = (1.486 / n) * area * (rh ** (2/3)) * (s ** 0.5)
    v_fps = q_cfs / area

    min_v = std.get("min_pipe_velocity_fps", 2.0)
    max_v = std.get("max_pipe_velocity_fps", 10.0)
    passed = min_v <= v_fps <= max_v
    violations = []
    if v_fps < min_v:
        violations.append(f"Velocity {v_fps:.1f} fps < minimum {min_v} fps (sedimentation risk)")
    if v_fps > max_v:
        violations.append(f"Velocity {v_fps:.1f} fps > maximum {max_v} fps (erosion risk)")

    return {
        "pipe_dia_in": pipe_dia_in,
        "slope_pct": slope_pct,
        "material": material,
        "mannings_n": n,
        "flow_cfs": round(q_cfs, 3),
        "velocity_fps": round(v_fps, 2),
        "passed": passed,
        "violations": violations,
    }


def check_road_geometry(
    state: str,
    speed_mph: int,
    lane_width_ft: float,
    grade_pct: float,
    curve_radius_ft: float,
) -> dict:
    """Check road geometry against state standard."""
    std = get_standard(state, "roads")
    violations = []

    min_lane = std.get("min_lane_width_ft", 12)
    if lane_width_ft < min_lane:
        violations.append(f"Lane width {lane_width_ft}ft < minimum {min_lane}ft")

    max_grade = std.get("max_grade_rolling_pct", 7)
    if grade_pct > max_grade:
        violations.append(f"Grade {grade_pct}% > maximum {max_grade}%")

    # Find required radius for given speed
    speed_key = f"min_curve_radius_{speed_mph}mph_ft"
    # Find closest speed
    available = [int(k.split("_")[3].replace("mph","")) for k in std if "min_curve_radius" in k]
    if available:
        closest = min(available, key=lambda s: abs(s - speed_mph))
        req_r = std.get(f"min_curve_radius_{closest}mph_ft", 0)
        if curve_radius_ft > 0 and curve_radius_ft < req_r:
            violations.append(
                f"Curve radius {curve_radius_ft}ft < minimum {req_r}ft for {closest}mph"
            )

    return {
        "state": state,
        "speed_mph": speed_mph,
        "lane_width_ft": lane_width_ft,
        "grade_pct": grade_pct,
        "curve_radius_ft": curve_radius_ft,
        "passed": len(violations) == 0,
        "violations": violations,
        "standard": std.get("source", "AASHTO"),
    }


def check_ada_compliance(
    ramp_slope: float,
    cross_slope: float,
    landing_width_ft: float,
    landing_length_ft: float = 5.0,
) -> dict:
    """Check ADA/PROWAG compliance for pedestrian facilities."""
    std = _NATIONAL["ada"]
    violations = []

    max_ramp = std["max_ramp_slope"]
    max_cross = std["max_cross_slope"]
    min_land_w = std["min_landing_width_ft"]
    min_land_l = std["min_landing_length_ft"]

    if ramp_slope > max_ramp:
        violations.append(
            f"Ramp slope {ramp_slope*100:.2f}% ({ramp_slope:.4f}) > max {max_ramp*100:.2f}% (1:12)"
        )
    if cross_slope > max_cross:
        violations.append(
            f"Cross slope {cross_slope*100:.2f}% > max {max_cross*100:.1f}%"
        )
    if landing_width_ft < min_land_w:
        violations.append(f"Landing width {landing_width_ft}ft < min {min_land_w}ft")
    if landing_length_ft < min_land_l:
        violations.append(f"Landing length {landing_length_ft}ft < min {min_land_l}ft")

    sf_slope = max_ramp / ramp_slope if ramp_slope > 0 else 999
    sf_cross = max_cross / cross_slope if cross_slope > 0 else 999

    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "sf_ramp_slope": round(sf_slope, 2),
        "sf_cross_slope": round(sf_cross, 2),
        "standard": std["standard"],
    }
