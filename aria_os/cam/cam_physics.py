"""
cam_physics.py — Physics-based feeds/speeds validation.

Validates chip load, tool deflection, and surface finish for a given tool,
material, and operation. Equivalent of per-part CEM SF checks for CAM.

Usage:
    from aria_os.cam_physics import compute_feeds_speeds, validate_cam_script_feeds
    result = compute_feeds_speeds(tool_dia_mm=10, flutes=3, material="aluminium_6061")
    val = validate_cam_script_feeds("outputs/cam/aria_housing/aria_housing_cam.py",
                                    material="aluminium_6061")
"""
from __future__ import annotations

import math
import re
from pathlib import Path

# ─── Material database ────────────────────────────────────────────────────────
# chip_load_per_flute_mm: keyed by nominal tool diameter (mm).
# Values are interpolated for intermediate diameters.
# axial_doc_ratio / radial_doc_ratio: fraction of tool diameter.
# Kt_N_mm2: specific cutting force constant (N/mm²) — used for deflection calc.
MATERIAL_PROFILES: dict[str, dict] = {
    "aluminium_6061": {
        "sfm": 300,
        "chip_load_per_flute_mm": {3: 0.025, 6: 0.050, 10: 0.075, 12: 0.089},
        "axial_doc_ratio": 1.0,
        "radial_doc_ratio": 0.5,
        "Kt_N_mm2": 700,
    },
    "aluminium_7075": {
        "sfm": 260,
        "chip_load_per_flute_mm": {3: 0.022, 6: 0.045, 10: 0.068, 12: 0.080},
        "axial_doc_ratio": 0.9,
        "radial_doc_ratio": 0.45,
        "Kt_N_mm2": 750,
    },
    "steel_4140": {
        "sfm": 90,
        "chip_load_per_flute_mm": {6: 0.020, 10: 0.030, 12: 0.038},
        "axial_doc_ratio": 0.5,
        "radial_doc_ratio": 0.3,
        "Kt_N_mm2": 2000,
    },
    "steel_mild": {
        "sfm": 120,
        "chip_load_per_flute_mm": {6: 0.025, 10: 0.035, 12: 0.045},
        "axial_doc_ratio": 0.5,
        "radial_doc_ratio": 0.3,
        "Kt_N_mm2": 1800,
    },
    "stainless_316": {
        "sfm": 80,
        "chip_load_per_flute_mm": {6: 0.015, 10: 0.022, 12: 0.028},
        "axial_doc_ratio": 0.4,
        "radial_doc_ratio": 0.25,
        "Kt_N_mm2": 2200,
    },
    "x1_420i": {
        "sfm": 85,
        "chip_load_per_flute_mm": {6: 0.018, 10: 0.025, 12: 0.032},
        "axial_doc_ratio": 0.4,
        "radial_doc_ratio": 0.25,
        "Kt_N_mm2": 2000,
    },
    "inconel_718": {
        "sfm": 40,
        "chip_load_per_flute_mm": {6: 0.008, 10: 0.012, 12: 0.015},
        "axial_doc_ratio": 0.3,
        "radial_doc_ratio": 0.15,
        "Kt_N_mm2": 3000,
    },
    "titanium_ti6al4v": {
        "sfm": 60,
        "chip_load_per_flute_mm": {6: 0.012, 10: 0.018, 12: 0.022},
        "axial_doc_ratio": 0.35,
        "radial_doc_ratio": 0.20,
        "Kt_N_mm2": 1800,
    },
    "pla": {
        "sfm": 500,
        "chip_load_per_flute_mm": {3: 0.040, 6: 0.080, 10: 0.120},
        "axial_doc_ratio": 2.0,
        "radial_doc_ratio": 0.5,
        "Kt_N_mm2": 80,
    },
    "abs": {
        "sfm": 450,
        "chip_load_per_flute_mm": {3: 0.035, 6: 0.070, 10: 0.100},
        "axial_doc_ratio": 1.8,
        "radial_doc_ratio": 0.5,
        "Kt_N_mm2": 70,
    },
}

# Carbide modulus of elasticity (N/mm²)
_E_CARBIDE = 620_000.0

# Maximum permissible tool deflection (mm) — 0.001" = 0.0254mm, use 0.025mm
_MAX_DEFLECTION_MM = 0.025


def _interpolate_chip_load(chip_load_table: dict[int, float], dia_mm: float) -> float:
    """
    Linearly interpolate chip load for a given diameter from the table.
    Clamps to the nearest boundary if outside table range.
    """
    if not chip_load_table:
        return 0.040  # safe fallback

    keys = sorted(chip_load_table.keys())

    if dia_mm <= keys[0]:
        return chip_load_table[keys[0]]
    if dia_mm >= keys[-1]:
        return chip_load_table[keys[-1]]

    # Find bracketing entries
    for i in range(len(keys) - 1):
        k_lo, k_hi = keys[i], keys[i + 1]
        if k_lo <= dia_mm <= k_hi:
            t = (dia_mm - k_lo) / (k_hi - k_lo)
            return chip_load_table[k_lo] + t * (chip_load_table[k_hi] - chip_load_table[k_lo])

    return chip_load_table[keys[-1]]


def compute_feeds_speeds(
    tool_dia_mm: float,
    flutes: int,
    material: str,
    overhang_mm: float | None = None,
) -> dict:
    """
    Compute optimal RPM, feed rate, and (optionally) tool deflection.

    Parameters
    ----------
    tool_dia_mm : float
        Tool diameter in mm.
    flutes : int
        Number of cutting flutes.
    material : str
        Material key — must match a key in MATERIAL_PROFILES.
    overhang_mm : float, optional
        Tool overhang (stick-out) in mm. When provided, deflection is computed
        and flagged if it exceeds 0.025mm (0.001").

    Returns
    -------
    dict with keys:
        rpm, feed_mmpm, chip_load_mm,
        deflection_mm (only when overhang_mm given),
        warnings: list[str]
    """
    profile = MATERIAL_PROFILES.get(material, MATERIAL_PROFILES.get("steel_4140"))
    if profile is None:
        profile = {"sfm": 150, "chip_load_per_flute_mm": {6: 0.030, 10: 0.040},
                   "axial_doc_ratio": 0.5, "radial_doc_ratio": 0.3, "Kt_N_mm2": 1500}

    sfm = profile["sfm"]
    chip_load_table = profile["chip_load_per_flute_mm"]
    axial_doc_ratio = profile["axial_doc_ratio"]
    Kt = profile.get("Kt_N_mm2", 1500)

    warnings: list[str] = []

    # ── RPM ───────────────────────────────────────────────────────────────────
    dia_inches = tool_dia_mm / 25.4
    if dia_inches <= 0:
        dia_inches = 0.001
    rpm_raw = (sfm * 3.82) / dia_inches
    rpm = int(min(rpm_raw, 24000))
    if rpm_raw > 24000:
        warnings.append(
            f"Computed RPM {int(rpm_raw)} exceeds 24000 limit — capped. "
            f"Verify spindle can reach 24000 RPM."
        )

    # ── Chip load + feed ──────────────────────────────────────────────────────
    chip_load = _interpolate_chip_load(chip_load_table, tool_dia_mm)
    chip_load = round(chip_load, 4)

    feed_mmpm = int(chip_load * flutes * rpm)

    result: dict = {
        "rpm": rpm,
        "feed_mmpm": feed_mmpm,
        "chip_load_mm": chip_load,
        "warnings": warnings,
    }

    # ── Deflection (optional) ─────────────────────────────────────────────────
    if overhang_mm is not None and overhang_mm > 0:
        # Axial depth of cut
        axial_doc = tool_dia_mm * axial_doc_ratio

        # Tangential cutting force: F = chip_load × axial_doc × Kt
        # chip_load in mm, axial_doc in mm → F in N
        F_tangential = chip_load * axial_doc * Kt  # N

        # Second moment of area for a solid cylinder: I = π×r⁴/4
        r = tool_dia_mm / 2.0
        I = math.pi * r**4 / 4.0  # mm⁴

        # Cantilever deflection: δ = F×L³ / (3×E×I)
        L = overhang_mm
        delta = (F_tangential * L**3) / (3.0 * _E_CARBIDE * I)
        delta = round(delta, 5)

        result["deflection_mm"] = delta

        if delta > _MAX_DEFLECTION_MM:
            warnings.append(
                f"Tool deflection {delta:.4f}mm at {overhang_mm}mm overhang exceeds "
                f"0.025mm limit. Reduce overhang, reduce axial DOC, or use a larger "
                f"diameter tool."
            )
        else:
            # Still warn if approaching limit
            if delta > _MAX_DEFLECTION_MM * 0.7:
                warnings.append(
                    f"Tool deflection {delta:.4f}mm is within 30% of the 0.025mm limit. "
                    f"Monitor chatter."
                )

    return result


def validate_cam_script_feeds(cam_script_path: str, material: str) -> dict:
    """
    Parse feed rates from a generated Fusion 360 CAM script and compare them
    against physics-computed optimal values.

    Flags feeds that are:
    - > 150% of optimal (too aggressive — tool breakage / poor finish risk)
    - < 30% of optimal (too conservative — poor chip evacuation, built-up edge)

    Returns:
        {passed: bool, violations: list[str], recommendations: list[str]}
    """
    path = Path(cam_script_path)
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {"passed": False,
                "violations": [f"Cannot read CAM script: {exc}"],
                "recommendations": []}

    violations: list[str] = []
    recommendations: list[str] = []

    # ── Extract tool entries: (name, dia_cm, flutes, rpm, feed_cmpm) ──────────
    tool_pattern = re.compile(
        r'make_flat_endmill\(\s*"([^"]+)"\s*,\s*([\d.]+)\s*,\s*(\d+)\s*,'
        r'\s*([\d.]+)\s*,\s*([\d.]+)',
    )
    tool_entries: list[dict] = []
    for m in tool_pattern.finditer(text):
        dia_mm = round(float(m.group(2)) * 10, 3)
        flutes = int(m.group(3))
        feed_cmpm = float(m.group(5))
        feed_mmpm = round(feed_cmpm * 10, 1)
        tool_entries.append({
            "name": m.group(1),
            "dia_mm": dia_mm,
            "flutes": flutes,
            "script_feed_mmpm": feed_mmpm,
        })

    if not tool_entries:
        # Fallback: scan for any feedrate = N lines
        feed_vals = [float(m.group(1)) for m in
                     re.finditer(r'feed[_\s]?rate\s*[=:]\s*([\d.]+)', text, re.IGNORECASE)]
        if not feed_vals:
            return {"passed": True,
                    "violations": [],
                    "recommendations": ["No tool/feed data found in CAM script — manual review recommended."]}
        # Generic check against a default 10mm 3-flute tool
        tool_entries = [{"name": "unknown", "dia_mm": 10.0, "flutes": 3,
                          "script_feed_mmpm": fv} for fv in feed_vals[:2]]

    passed = True
    for entry in tool_entries:
        dia = entry["dia_mm"]
        flutes = entry["flutes"]
        name = entry["name"]
        script_feed = entry["script_feed_mmpm"]

        optimal = compute_feeds_speeds(dia, flutes, material)
        opt_feed = optimal["feed_mmpm"]

        if opt_feed <= 0:
            continue

        ratio = script_feed / opt_feed

        if ratio > 1.5:
            passed = False
            violations.append(
                f"Tool '{name}' ({dia}mm, {flutes}fl): script feed {script_feed:.0f}mm/min "
                f"is {ratio:.0%} of optimal {opt_feed:.0f}mm/min — too aggressive. "
                f"Risk of tool breakage or chatter."
            )
            recommendations.append(
                f"Reduce '{name}' feed to <= {int(opt_feed * 1.5)}mm/min."
            )
        elif ratio < 0.30:
            # Not a hard violation, but flag as a warning
            violations.append(
                f"Tool '{name}' ({dia}mm, {flutes}fl): script feed {script_feed:.0f}mm/min "
                f"is only {ratio:.0%} of optimal {opt_feed:.0f}mm/min — "
                f"may cause rubbing, built-up edge, or poor surface finish."
            )
            recommendations.append(
                f"Increase '{name}' feed to >= {int(opt_feed * 0.30)}mm/min "
                f"for efficient chip formation."
            )

    return {
        "passed": passed,
        "violations": violations,
        "recommendations": recommendations,
    }


def predict_surface_finish(
    chip_load_mm: float,
    tool_dia_mm: float,
    rpm: float,
) -> float:
    """
    Predict theoretical surface roughness Ra in micrometres.

    Uses the cusp-height formula:
        Ra ≈ chip_load² / (8 × tool_nose_radius)
    where tool_nose_radius ≈ tool_dia / 10 (corner radius approximation).

    Returns Ra in µm.
    """
    if tool_dia_mm <= 0 or chip_load_mm <= 0:
        return 0.0

    nose_radius_mm = tool_dia_mm / 10.0
    Ra_mm = (chip_load_mm**2) / (8.0 * nose_radius_mm)
    Ra_um = Ra_mm * 1000.0
    return round(Ra_um, 3)


# ─── Machine profiles ─────────────────────────────────────────────────────────

_MACHINE_PROFILES: dict[str, dict] = {
    "tormach": {
        "max_spindle_power_w": 1500,
        "max_torque_nm": 10,
        "max_rpm": 10000,
        "travel_x_mm": 457,
        "travel_y_mm": 305,
        "travel_z_mm": 457,
        "name": "Tormach 1100",
    },
    "haas": {
        "max_spindle_power_w": 22000,
        "max_torque_nm": 122,
        "max_rpm": 12000,
        "travel_x_mm": 762,
        "travel_y_mm": 406,
        "travel_z_mm": 508,
        "name": "HAAS VF2",
    },
}

_GENERIC_MACHINE_PROFILE_TEMPLATE: dict = {
    "max_spindle_power_w": 7500,
    "max_torque_nm": 40,
    "max_rpm": 10000,
}


def get_machine_profile(machine_name: str) -> dict:
    """
    Return machine capability profile by name.

    Recognises "tormach" and "haas" as case-insensitive substrings.
    Unknown names fall back to a generic 7.5 kW profile.

    Returns a dict with keys:
        max_spindle_power_w, max_torque_nm, max_rpm, name
        (and travel_x/y/z_mm for known machines).
    """
    lower = machine_name.lower()
    for key, profile in _MACHINE_PROFILES.items():
        if key in lower:
            return dict(profile)

    # Generic fallback
    result = dict(_GENERIC_MACHINE_PROFILE_TEMPLATE)
    result["name"] = machine_name
    return result


# ─── Feed/speed validation ────────────────────────────────────────────────────

def validate_feeds_speeds(
    tool_dia_mm: float,
    material: str,
    depth_of_cut_mm: float,
    width_of_cut_mm: float,
    overhang_mm: float,
    spindle_power_w: float = 1500,
) -> dict:
    """
    Validate feeds and speeds against machine power limits and tool deflection.

    Computes base RPM/feed from ``compute_feeds_speeds``, then checks:
    - Material Removal Rate (MRR) and required spindle power vs rated limit
    - Tool deflection vs 0.025 mm limit
    - Surface finish estimate

    Parameters
    ----------
    tool_dia_mm : float
        Tool diameter in mm.
    material : str
        Material key from MATERIAL_PROFILES.
    depth_of_cut_mm : float
        Axial depth of cut in mm.
    width_of_cut_mm : float
        Radial width of cut in mm.
    overhang_mm : float
        Tool stick-out from holder in mm.
    spindle_power_w : float
        Machine rated spindle power in watts (default 1500 W = Tormach 1100).

    Returns
    -------
    dict with keys:
        rpm, feed_mmpm, chip_load_mm,
        mrr_mm3_min, required_power_w,
        surface_finish_ra_um,
        deflection_mm,
        warnings: list[str],
        passed: bool
    """
    warnings: list[str] = []

    # Base feeds/speeds (includes deflection and its own warnings)
    base = compute_feeds_speeds(
        tool_dia_mm=tool_dia_mm,
        flutes=3,
        material=material,
        overhang_mm=overhang_mm,
    )
    warnings.extend(base["warnings"])

    rpm = base["rpm"]
    feed_mmpm = base["feed_mmpm"]
    chip_load = base["chip_load_mm"]
    deflection_mm = base.get("deflection_mm", 0.0)

    # ── MRR and required spindle power ────────────────────────────────────────
    # MRR [mm³/min] = axial_doc × radial_doc × feed_rate
    mrr = depth_of_cut_mm * width_of_cut_mm * feed_mmpm  # mm³/min

    profile = MATERIAL_PROFILES.get(material, MATERIAL_PROFILES.get("steel_4140"))
    if profile is None:
        Kt = 1500.0
    else:
        Kt = float(profile.get("Kt_N_mm2", 1500))

    # Power [W] = MRR [mm³/min] × Kt [N/mm²] / 60000
    # (MRR/60 → mm³/s, × Kt → N·mm/s = mW, ÷1000 → W)
    required_power_w = (mrr * Kt) / 60_000.0

    power_limit_w = spindle_power_w * 0.8
    if required_power_w > power_limit_w:
        warnings.append(
            f"Required spindle power {required_power_w:.0f} W exceeds 80% of rated "
            f"{spindle_power_w:.0f} W ({power_limit_w:.0f} W limit). "
            f"Reduce depth/width of cut or feed rate."
        )

    # ── Surface finish ────────────────────────────────────────────────────────
    surface_finish_ra_um = predict_surface_finish(
        chip_load_mm=chip_load,
        tool_dia_mm=tool_dia_mm,
        rpm=rpm,
    )

    # ── Pass/fail ─────────────────────────────────────────────────────────────
    power_fail = required_power_w > power_limit_w
    deflection_fail = deflection_mm > _MAX_DEFLECTION_MM

    passed = not (power_fail or deflection_fail)

    result: dict = {
        "rpm": rpm,
        "feed_mmpm": feed_mmpm,
        "chip_load_mm": chip_load,
        "mrr_mm3_min": round(mrr, 1),
        "required_power_w": round(required_power_w, 1),
        "surface_finish_ra_um": surface_finish_ra_um,
        "deflection_mm": deflection_mm,
        "warnings": warnings,
        "passed": passed,
    }

    return result
