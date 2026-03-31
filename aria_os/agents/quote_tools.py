"""
quote_tools.py — Tool functions for the instant quoting agent.

Extracts geometry from STEP files, provides material rate lookups,
machining time estimation, and lead time prediction.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any


# ─── Material rates ($/cm3) ──────────────────────────────────────────────────

MATERIAL_RATES: dict[str, dict[str, float]] = {
    # Aluminium
    "aluminium_6061":  {"rate_per_cm3": 0.008, "density_g_cm3": 2.70, "machinability": 1.0},
    "aluminum_6061":   {"rate_per_cm3": 0.008, "density_g_cm3": 2.70, "machinability": 1.0},
    "aluminium_7075":  {"rate_per_cm3": 0.012, "density_g_cm3": 2.81, "machinability": 0.9},
    "aluminum_7075":   {"rate_per_cm3": 0.012, "density_g_cm3": 2.81, "machinability": 0.9},
    # Steel
    "steel_1018":      {"rate_per_cm3": 0.025, "density_g_cm3": 7.87, "machinability": 0.78},
    "steel_mild":      {"rate_per_cm3": 0.025, "density_g_cm3": 7.85, "machinability": 0.78},
    "steel_4140":      {"rate_per_cm3": 0.035, "density_g_cm3": 7.85, "machinability": 0.65},
    "stainless_304":   {"rate_per_cm3": 0.045, "density_g_cm3": 8.00, "machinability": 0.45},
    "stainless_316":   {"rate_per_cm3": 0.055, "density_g_cm3": 8.00, "machinability": 0.40},
    "x1_420i":         {"rate_per_cm3": 0.045, "density_g_cm3": 7.86, "machinability": 0.50},
    # Titanium
    "titanium":        {"rate_per_cm3": 0.15,  "density_g_cm3": 4.43, "machinability": 0.25},
    "ti_6al4v":        {"rate_per_cm3": 0.15,  "density_g_cm3": 4.43, "machinability": 0.25},
    "titanium_ti6al4v": {"rate_per_cm3": 0.15, "density_g_cm3": 4.43, "machinability": 0.25},
    # Non-ferrous
    "brass_360":       {"rate_per_cm3": 0.04,  "density_g_cm3": 8.50, "machinability": 1.2},
    "brass":           {"rate_per_cm3": 0.04,  "density_g_cm3": 8.50, "machinability": 1.2},
    "copper_c110":     {"rate_per_cm3": 0.06,  "density_g_cm3": 8.96, "machinability": 0.6},
    "copper":          {"rate_per_cm3": 0.06,  "density_g_cm3": 8.96, "machinability": 0.6},
    # Superalloy
    "inconel_718":     {"rate_per_cm3": 0.20,  "density_g_cm3": 8.19, "machinability": 0.15},
    # Plastics (CNC)
    "delrin":          {"rate_per_cm3": 0.02,  "density_g_cm3": 1.41, "machinability": 1.5},
    "nylon":           {"rate_per_cm3": 0.025, "density_g_cm3": 1.14, "machinability": 1.3},
    "nylon_pa12":      {"rate_per_cm3": 0.025, "density_g_cm3": 1.01, "machinability": 1.3},
    "polycarbonate":   {"rate_per_cm3": 0.035, "density_g_cm3": 1.20, "machinability": 1.2},
    "pc":              {"rate_per_cm3": 0.035, "density_g_cm3": 1.20, "machinability": 1.2},
    "peek":            {"rate_per_cm3": 0.08,  "density_g_cm3": 1.30, "machinability": 0.7},
    # 3D printing filaments
    "pla":             {"rate_per_cm3": 0.015, "density_g_cm3": 1.24, "machinability": 2.0},
    "abs":             {"rate_per_cm3": 0.018, "density_g_cm3": 1.05, "machinability": 1.8},
    "petg":            {"rate_per_cm3": 0.020, "density_g_cm3": 1.27, "machinability": 1.6},
    "tpu":             {"rate_per_cm3": 0.030, "density_g_cm3": 1.21, "machinability": 1.0},
}

# Material removal rates (min per cm3 of material removed)
REMOVAL_RATES: dict[str, float] = {
    "aluminium":  0.8,
    "aluminum":   0.8,
    "steel":      2.5,
    "stainless":  3.0,
    "titanium":   5.0,
    "inconel":    8.0,
    "brass":      0.9,
    "copper":     1.2,
    "plastic":    0.5,
}

# Machine rates ($/min)
MACHINE_RATES: dict[str, float] = {
    "cnc_3axis":   1.50,
    "cnc_4axis":   2.25,
    "cnc_5axis":   3.50,
    "cnc_turning": 1.25,
    "fdm":         0.083,   # $5/hr
    "sla":         0.167,   # $10/hr
    "sls":         0.250,   # $15/hr
}

# Setup costs ($)
SETUP_COSTS: dict[str, float] = {
    "cnc_3axis":   75.0,
    "cnc_4axis":   125.0,
    "cnc_5axis":   200.0,
    "cnc_turning": 50.0,
    "fdm":         5.0,
    "sla":         10.0,
    "sls":         15.0,
    "sheet_metal": 40.0,
    "injection_mold": 5000.0,
}

# Finishing rates ($/cm2)
FINISHING_RATES: dict[str, float] = {
    "as_machined":    0.005,
    "bead_blasted":   0.01,
    "anodized":       0.02,
    "powder_coated":  0.015,
    "polished":       0.03,
    "none":           0.0,
}


def _resolve_removal_category(material: str) -> str:
    """Map a material key to a removal rate category."""
    mat_lower = material.lower()
    for cat in ("inconel", "titanium", "stainless", "steel", "aluminium", "aluminum",
                "brass", "copper"):
        if cat in mat_lower:
            return cat
    # Plastics
    if any(p in mat_lower for p in ("pla", "abs", "petg", "nylon", "delrin",
                                     "peek", "polycarbonate", "pc", "tpu")):
        return "plastic"
    return "steel"  # conservative default


def extract_geometry_for_quote(step_path: str) -> dict[str, Any]:
    """
    Extract geometric features from a STEP file for quoting.

    Returns dict with: volume_cm3, surface_area_cm2, bbox_mm (list[3]),
    face_count, complexity (low/medium/high).
    """
    result: dict[str, Any] = {
        "volume_cm3": 0.0,
        "surface_area_cm2": 0.0,
        "bbox_mm": [0.0, 0.0, 0.0],
        "face_count": 0,
        "complexity": "medium",
        "error": None,
    }

    path = Path(step_path)

    # Try CadQuery first (most reliable for STEP)
    try:
        import cadquery as cq
        shape = cq.importers.importStep(str(path))
        bb = shape.val().BoundingBox()
        result["bbox_mm"] = [
            round(bb.xlen, 2),
            round(bb.ylen, 2),
            round(bb.zlen, 2),
        ]
        # Volume in mm3 -> cm3
        vol_mm3 = 0.0
        for solid in shape.solids().vals():
            vol_mm3 += solid.Volume()
        result["volume_cm3"] = round(vol_mm3 / 1000.0, 4)

        # Surface area mm2 -> cm2
        area_mm2 = 0.0
        for solid in shape.solids().vals():
            area_mm2 += solid.Area()
        result["surface_area_cm2"] = round(area_mm2 / 100.0, 4)

        # Face count for complexity estimation
        faces = shape.faces().vals()
        result["face_count"] = len(faces)

        # Complexity heuristic
        fc = result["face_count"]
        if fc <= 12:
            result["complexity"] = "low"
        elif fc <= 50:
            result["complexity"] = "medium"
        else:
            result["complexity"] = "high"

        return result
    except ImportError:
        pass
    except Exception as exc:
        result["error"] = f"CadQuery import failed: {exc}"

    # Fallback: try trimesh on STL if available
    stl_path = path.with_suffix(".stl")
    if not stl_path.exists():
        stl_dir = path.parent.parent / "stl"
        stl_path = stl_dir / (path.stem + ".stl")

    if stl_path.exists():
        try:
            import trimesh
            mesh = trimesh.load(str(stl_path))
            bb = mesh.bounding_box.extents
            result["bbox_mm"] = [round(float(bb[0]), 2), round(float(bb[1]), 2), round(float(bb[2]), 2)]
            result["volume_cm3"] = round(float(mesh.volume) / 1000.0, 4)
            result["surface_area_cm2"] = round(float(mesh.area) / 100.0, 4)
            result["face_count"] = len(mesh.faces)
            fc = result["face_count"]
            if fc <= 500:
                result["complexity"] = "low"
            elif fc <= 5000:
                result["complexity"] = "medium"
            else:
                result["complexity"] = "high"
            result["error"] = None
            return result
        except Exception as exc:
            result["error"] = f"Trimesh fallback failed: {exc}"

    # Last resort: try to parse STEP header for bbox hints
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            # Look for BBOX comment if our pipeline wrote one
            import re
            bbox_match = re.search(r"BBOX:\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)", text)
            if bbox_match:
                x, y, z = float(bbox_match.group(1)), float(bbox_match.group(2)), float(bbox_match.group(3))
                result["bbox_mm"] = [x, y, z]
                # Rough volume estimate (50% of bbox for typical machined part)
                result["volume_cm3"] = round((x * y * z * 0.5) / 1000.0, 4)
                result["surface_area_cm2"] = round(2 * (x * y + y * z + x * z) / 100.0, 4)
                result["complexity"] = "medium"
                result["error"] = "estimated_from_bbox"
        except Exception:
            pass

    return result


def get_material_rate(material: str) -> dict[str, float]:
    """
    Look up material cost rate, density, and machinability factor.

    Returns dict with: rate_per_cm3, density_g_cm3, machinability (1.0 = baseline aluminium).
    Falls back to steel_4140 if material not found.
    """
    mat_lower = material.lower().replace("-", "_").replace(" ", "_")

    # Direct lookup
    if mat_lower in MATERIAL_RATES:
        return dict(MATERIAL_RATES[mat_lower])

    # Fuzzy match
    for key, data in MATERIAL_RATES.items():
        if key in mat_lower or mat_lower in key:
            return dict(data)

    # Default fallback
    return {"rate_per_cm3": 0.035, "density_g_cm3": 7.85, "machinability": 0.65}


def estimate_machining_time(
    volume_cm3: float,
    material: str,
    axes: str = "3axis",
    removal_ratio: float = 0.5,
    stock_volume_cm3: float | None = None,
) -> float:
    """
    Estimate machining time in minutes.

    Parameters
    ----------
    volume_cm3 : Part volume in cm3.
    material : Material key.
    axes : "3axis", "4axis", "5axis", or "turning".
    removal_ratio : Fraction of stock that is removed (0-1).
    stock_volume_cm3 : If provided, used instead of computing from removal_ratio.

    Returns minutes.
    """
    category = _resolve_removal_category(material)
    base_rate = REMOVAL_RATES.get(category, 2.5)  # min per cm3

    if stock_volume_cm3 is not None:
        removal_volume = max(stock_volume_cm3 - volume_cm3, 0.0)
    else:
        # Estimate stock from part volume + removal ratio
        if removal_ratio >= 1.0:
            removal_ratio = 0.95
        stock_vol = volume_cm3 / max(1.0 - removal_ratio, 0.05)
        removal_volume = stock_vol - volume_cm3

    time_min = removal_volume * base_rate

    # Axis complexity multiplier
    axis_mult = {"3axis": 1.0, "4axis": 1.3, "5axis": 1.6, "turning": 0.8}
    time_min *= axis_mult.get(axes, 1.0)

    # Minimum machining time
    time_min = max(time_min, 2.0)

    return round(time_min, 2)


def estimate_lead_time(
    process: str,
    complexity: str = "medium",
    quantity: int = 1,
) -> int:
    """
    Estimate lead time in business days.

    Parameters
    ----------
    process : "cnc_3axis", "cnc_5axis", "fdm", "sla", "sheet_metal", "injection_mold".
    complexity : "low", "medium", "high".
    quantity : Number of units.

    Returns business days.
    """
    base_days: dict[str, int] = {
        "cnc_3axis":       5,
        "cnc_4axis":       7,
        "cnc_5axis":       10,
        "cnc_turning":     4,
        "fdm":             3,
        "sla":             3,
        "sls":             5,
        "sheet_metal":     5,
        "injection_mold":  20,
    }

    days = base_days.get(process, 7)

    # Complexity adder
    if complexity == "high":
        days += 3
    elif complexity == "low":
        days = max(days - 1, 2)

    # Quantity scaling
    if quantity > 100:
        days += 5
    elif quantity > 10:
        days += 2
    elif quantity > 1:
        days += 1

    return days


def estimate_print_time_hr(
    volume_cm3: float,
    process: str = "fdm",
    support_factor: float = 0.2,
) -> float:
    """Estimate 3D print time in hours."""
    # Deposition rates in cm3/hr
    rates = {
        "fdm": 15.0,
        "sla": 8.0,
        "sls": 12.0,
    }
    rate = rates.get(process, 15.0)
    hours = (volume_cm3 / rate) * (1.0 + support_factor)
    return round(max(hours, 0.25), 2)
