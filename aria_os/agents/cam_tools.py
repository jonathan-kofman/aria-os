"""CAM agent tool functions — geometry analysis, tool selection, feeds/speeds, validation."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Embedded tool library
# ---------------------------------------------------------------------------

TOOL_LIBRARY: list[dict[str, Any]] = [
    {"id": "EM-3",  "type": "endmill",   "diameter_mm": 3.0,  "flutes": 2, "length_mm": 38},
    {"id": "EM-6",  "type": "endmill",   "diameter_mm": 6.0,  "flutes": 3, "length_mm": 57},
    {"id": "EM-10", "type": "endmill",   "diameter_mm": 10.0, "flutes": 3, "length_mm": 72},
    {"id": "EM-12", "type": "endmill",   "diameter_mm": 12.0, "flutes": 3, "length_mm": 76},
    {"id": "EM-16", "type": "endmill",   "diameter_mm": 16.0, "flutes": 4, "length_mm": 92},
    {"id": "EM-20", "type": "endmill",   "diameter_mm": 20.0, "flutes": 4, "length_mm": 104},
    {"id": "BN-6",  "type": "ball_nose", "diameter_mm": 6.0,  "flutes": 2, "length_mm": 57},
    {"id": "BN-10", "type": "ball_nose", "diameter_mm": 10.0, "flutes": 2, "length_mm": 72},
    {"id": "DR-3",  "type": "drill",     "diameter_mm": 3.0,  "length_mm": 60},
    {"id": "DR-4",  "type": "drill",     "diameter_mm": 4.0,  "length_mm": 65},
    {"id": "DR-5",  "type": "drill",     "diameter_mm": 5.0,  "length_mm": 70},
    {"id": "DR-6",  "type": "drill",     "diameter_mm": 6.0,  "length_mm": 75},
    {"id": "DR-8",  "type": "drill",     "diameter_mm": 8.0,  "length_mm": 85},
    {"id": "DR-10", "type": "drill",     "diameter_mm": 10.0, "length_mm": 95},
]

# ---------------------------------------------------------------------------
# SFM tables (Surface Feet per Minute)
# ---------------------------------------------------------------------------

SFM_TABLE: dict[str, dict[str, float]] = {
    "aluminium_6061": {"sfm": 300, "chip_load_mm": 0.10, "depth_factor": 1.0},
    "aluminium_7075": {"sfm": 260, "chip_load_mm": 0.08, "depth_factor": 0.9},
    "steel_1018":     {"sfm": 100, "chip_load_mm": 0.05, "depth_factor": 0.7},
    "steel_4140":     {"sfm": 90,  "chip_load_mm": 0.04, "depth_factor": 0.6},
    "stainless_304":  {"sfm": 65,  "chip_load_mm": 0.03, "depth_factor": 0.5},
    "stainless_316":  {"sfm": 55,  "chip_load_mm": 0.025, "depth_factor": 0.45},
    "titanium":       {"sfm": 40,  "chip_load_mm": 0.02, "depth_factor": 0.3},
    "inconel_718":    {"sfm": 25,  "chip_load_mm": 0.015, "depth_factor": 0.2},
    "brass":          {"sfm": 200, "chip_load_mm": 0.10, "depth_factor": 1.0},
    "copper":         {"sfm": 150, "chip_load_mm": 0.08, "depth_factor": 0.9},
    "pla":            {"sfm": 500, "chip_load_mm": 0.15, "depth_factor": 1.5},
    "abs":            {"sfm": 450, "chip_load_mm": 0.12, "depth_factor": 1.3},
    "polycarbonate":  {"sfm": 400, "chip_load_mm": 0.10, "depth_factor": 1.2},
    "nylon":          {"sfm": 350, "chip_load_mm": 0.10, "depth_factor": 1.1},
    "delrin":         {"sfm": 500, "chip_load_mm": 0.15, "depth_factor": 1.5},
    "peek":           {"sfm": 200, "chip_load_mm": 0.05, "depth_factor": 0.8},
}

# Specific cutting force constants (N/mm^2) for power calculation
_KT_TABLE: dict[str, float] = {
    "aluminium_6061": 700,
    "aluminium_7075": 750,
    "steel_1018":     1800,
    "steel_4140":     2000,
    "stainless_304":  2200,
    "stainless_316":  2200,
    "titanium":       1800,
    "inconel_718":    3000,
    "brass":          600,
    "copper":         700,
    "pla":            80,
    "abs":            70,
    "polycarbonate":  120,
    "nylon":          100,
    "delrin":         90,
    "peek":           400,
}

# Machine profiles
MACHINE_PROFILES: dict[str, dict[str, Any]] = {
    "tormach_1100": {
        "name": "Tormach 1100",
        "max_rpm": 10000,
        "min_rpm": 100,
        "max_feed_mmpm": 5000,
        "min_feed_mmpm": 1,
        "max_power_kw": 1.5,
        "max_torque_nm": 10,
    },
    "haas_vf2": {
        "name": "HAAS VF2",
        "max_rpm": 12000,
        "min_rpm": 100,
        "max_feed_mmpm": 16500,
        "min_feed_mmpm": 1,
        "max_power_kw": 22.0,
        "max_torque_nm": 122,
    },
    "generic_vmc": {
        "name": "Generic VMC",
        "max_rpm": 24000,
        "min_rpm": 100,
        "max_feed_mmpm": 5000,
        "min_feed_mmpm": 1,
        "max_power_kw": 7.5,
        "max_torque_nm": 40,
    },
}

# Carbide modulus of elasticity (N/mm^2)
_E_CARBIDE_N_MM2 = 620_000.0
_MAX_DEFLECTION_MM = 0.025


# ---------------------------------------------------------------------------
# Tool function: analyze_step
# ---------------------------------------------------------------------------

def analyze_step(step_path: str) -> dict[str, Any]:
    """Load a STEP file via CadQuery and extract geometry data for CAM planning.

    Returns dict with bbox, face_count, min_feature_mm, holes, pocket_depths, volume_cm3.
    """
    result: dict[str, Any] = {
        "bbox": None,
        "face_count": 0,
        "edge_count": 0,
        "min_feature_mm": 10.0,
        "holes": [],
        "pocket_depths": [],
        "volume_cm3": None,
        "max_dim_mm": 0.0,
        "error": None,
    }

    p = Path(step_path)
    if not p.exists():
        result["error"] = f"STEP file not found: {step_path}"
        return result

    try:
        import cadquery as cq
    except ImportError:
        result["error"] = "cadquery not available"
        return result

    try:
        solid = cq.importers.importStep(str(p))
        val = solid.val()
        bb = val.BoundingBox()

        bbox = {
            "x_mm": round(bb.xlen, 2),
            "y_mm": round(bb.ylen, 2),
            "z_mm": round(bb.zlen, 2),
        }
        result["bbox"] = bbox
        result["max_dim_mm"] = round(max(bb.xlen, bb.ylen, bb.zlen), 2)

        # Face and edge counts
        faces = val.Faces()
        edges = val.Edges()
        result["face_count"] = len(faces)
        result["edge_count"] = len(edges)

        # Min feature estimate: smallest bbox dim / 4
        result["min_feature_mm"] = round(min(bb.xlen, bb.ylen, bb.zlen) / 4.0, 2)

        # Detect holes (circular edges)
        holes: list[float] = []
        try:
            for edge in solid.edges("%Circle").vals():
                try:
                    r = edge.radius()
                    dia = round(r * 2, 2)
                    if 1.0 <= dia <= 50.0:
                        holes.append(dia)
                except Exception:
                    continue
            holes = sorted(set(round(h, 1) for h in holes))
        except Exception:
            pass
        result["holes"] = holes

        # Estimate pocket depths from Z-axis features
        pocket_depths: list[float] = []
        try:
            z_values: list[float] = []
            for face in faces:
                try:
                    face_bb = face.BoundingBox()
                    z_values.append(round(face_bb.zmin, 2))
                    z_values.append(round(face_bb.zmax, 2))
                except Exception:
                    continue
            if z_values:
                z_top = max(z_values)
                unique_z = sorted(set(z_values), reverse=True)
                for z in unique_z[1:6]:  # up to 5 depth levels
                    depth = round(z_top - z, 2)
                    if depth > 0.5:
                        pocket_depths.append(depth)
        except Exception:
            pass
        result["pocket_depths"] = pocket_depths

        # Volume
        try:
            vol = val.Volume() / 1000.0  # mm^3 -> cm^3
            result["volume_cm3"] = round(vol, 2)
        except Exception:
            pass

    except Exception as exc:
        result["error"] = str(exc)[:300]

    return result


# ---------------------------------------------------------------------------
# Tool function: select_tools
# ---------------------------------------------------------------------------

def select_tools(
    min_feature_mm: float | str,
    max_dim_mm: float | str,
    holes: list[float] | str = "",
) -> list[dict[str, Any]]:
    """Select tools from the embedded library based on geometry constraints.

    Args:
        min_feature_mm: Smallest feature to machine (determines finish tool).
        max_dim_mm: Largest part dimension (constrains roughing tool max).
        holes: List of hole diameters in mm, or comma-separated string.

    Returns list of selected tools with assigned roles.
    """
    min_feat = float(min_feature_mm)
    max_dim = float(max_dim_mm)

    # Parse holes if string
    hole_list: list[float] = []
    if isinstance(holes, str) and holes.strip():
        try:
            hole_list = [float(h.strip()) for h in holes.split(",") if h.strip()]
        except ValueError:
            hole_list = []
    elif isinstance(holes, list):
        hole_list = [float(h) for h in holes]

    selected: list[dict[str, Any]] = []
    endmills = [t for t in TOOL_LIBRARY if t["type"] == "endmill"]
    ball_noses = [t for t in TOOL_LIBRARY if t["type"] == "ball_nose"]
    drills = [t for t in TOOL_LIBRARY if t["type"] == "drill"]

    # Roughing: largest endmill that fits (dia <= min_feature AND dia <= max_dim * 0.4)
    roughing = None
    for t in sorted(endmills, key=lambda x: -x["diameter_mm"]):
        if t["diameter_mm"] <= min_feat and t["diameter_mm"] <= max_dim * 0.4:
            roughing = t
            break
    if roughing is None and endmills:
        roughing = min(endmills, key=lambda x: x["diameter_mm"])

    if roughing:
        selected.append({**roughing, "role": "roughing"})

    # Finishing: smallest endmill strictly smaller than roughing
    finishing = None
    rough_dia = roughing["diameter_mm"] if roughing else 999
    for t in sorted(endmills, key=lambda x: x["diameter_mm"]):
        if t["diameter_mm"] < rough_dia:
            finishing = t
            break
    if finishing is None:
        finishing = roughing
    if finishing:
        selected.append({**finishing, "role": "finishing"})

    # Contour: ball nose closest to finishing endmill size, or same as finish
    contour = None
    if ball_noses:
        finish_dia = finishing["diameter_mm"] if finishing else 6.0
        contour = min(ball_noses, key=lambda x: abs(x["diameter_mm"] - finish_dia))
        selected.append({**contour, "role": "contour"})

    # Drills: match each hole diameter
    for hole_dia in hole_list:
        best_drill = None
        best_diff = 999.0
        for d in drills:
            diff = abs(d["diameter_mm"] - hole_dia)
            if diff < best_diff and diff < 0.5:  # within 0.5mm tolerance
                best_drill = d
                best_diff = diff
        if best_drill:
            already = any(s["id"] == best_drill["id"] for s in selected)
            if not already:
                selected.append({
                    **best_drill,
                    "role": "drill",
                    "target_hole_mm": hole_dia,
                })

    return selected


# ---------------------------------------------------------------------------
# Tool function: calc_feeds
# ---------------------------------------------------------------------------

def calc_feeds(
    tool_dia_mm: float | str,
    material: str,
    n_flutes: int | str = "3",
    depth_mm: float | str = "0",
) -> dict[str, Any]:
    """Calculate RPM, feed rate, DOC, and width of cut for a tool + material combo.

    Args:
        tool_dia_mm: Tool diameter in mm.
        material: Material key from SFM_TABLE.
        n_flutes: Number of flutes.
        depth_mm: Requested depth of cut (0 = auto-calculate).

    Returns dict with rpm, feed_mm_per_min, depth_of_cut_mm, width_of_cut_mm,
    chip_load_mm, plunge_rate_mmpm.
    """
    dia = float(tool_dia_mm)
    flutes = int(n_flutes)
    req_depth = float(depth_mm)

    mat = SFM_TABLE.get(material, SFM_TABLE.get("aluminium_6061", {}))
    if not mat:
        mat = {"sfm": 150, "chip_load_mm": 0.05, "depth_factor": 0.7}

    sfm = mat["sfm"]
    chip_load = mat["chip_load_mm"]
    depth_factor = mat["depth_factor"]

    # RPM = (SFM * 304.8) / (pi * tool_dia_mm)
    if dia <= 0:
        dia = 3.0
    rpm = (sfm * 304.8) / (math.pi * dia)
    rpm = int(min(rpm, 24000))
    rpm = max(rpm, 100)

    # Feed = RPM * chip_load * flutes
    feed_mm_per_min = int(rpm * chip_load * flutes)
    feed_mm_per_min = max(feed_mm_per_min, 1)

    # Depth of cut: 50% of tool dia * depth_factor, unless overridden
    doc = req_depth if req_depth > 0 else round(dia * depth_factor * 0.5, 2)

    # Width of cut: 40% stepover for adaptive clearing
    woc = round(dia * 0.4, 2)

    # Plunge rate: 25% of cutting feed
    plunge = max(int(feed_mm_per_min * 0.25), 1)

    return {
        "rpm": rpm,
        "feed_mm_per_min": feed_mm_per_min,
        "depth_of_cut_mm": doc,
        "width_of_cut_mm": woc,
        "chip_load_mm": chip_load,
        "plunge_rate_mmpm": plunge,
        "sfm": sfm,
    }


# ---------------------------------------------------------------------------
# Tool function: validate_cam_physics
# ---------------------------------------------------------------------------

def validate_cam_physics(
    operations: list[dict[str, Any]] | str,
    machine: str = "generic_vmc",
) -> dict[str, Any]:
    """Validate CAM operations against machine limits: RPM, feed, power, deflection.

    Args:
        operations: List of operation dicts, each with keys:
            tool_dia_mm, flutes, rpm, feed_mm_per_min, depth_of_cut_mm,
            width_of_cut_mm, overhang_mm, material
        machine: Machine profile key.

    Returns dict with passed, violations, warnings.
    """
    # Parse if string (from LLM)
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except (json.JSONDecodeError, ValueError):
            return {"passed": False, "violations": ["Could not parse operations JSON"],
                    "warnings": []}

    profile = MACHINE_PROFILES.get(machine, MACHINE_PROFILES["generic_vmc"])
    violations: list[str] = []
    warnings: list[str] = []

    for i, op in enumerate(operations):
        op_name = op.get("name", f"Op_{i + 1}")
        rpm = float(op.get("rpm", 0))
        feed = float(op.get("feed_mm_per_min", 0))
        dia = float(op.get("tool_dia_mm", 10))
        flutes = int(op.get("flutes", 3))
        doc = float(op.get("depth_of_cut_mm", 0))
        woc = float(op.get("width_of_cut_mm", 0))
        overhang = float(op.get("overhang_mm", dia * 3))
        material = op.get("material", "aluminium_6061")

        # RPM check
        if rpm > profile["max_rpm"]:
            violations.append(
                f"{op_name}: RPM {int(rpm)} exceeds machine max {profile['max_rpm']}")
        elif rpm < profile["min_rpm"]:
            violations.append(
                f"{op_name}: RPM {int(rpm)} below machine min {profile['min_rpm']}")

        # Feed check
        if feed > profile["max_feed_mmpm"]:
            violations.append(
                f"{op_name}: feed {int(feed)} mm/min exceeds machine max {profile['max_feed_mmpm']}")

        # Power check: P_kW = MRR * Kt / 60_000_000
        if doc > 0 and woc > 0 and feed > 0:
            mrr_mm3_min = doc * woc * feed
            Kt = _KT_TABLE.get(material, 1500)
            power_kw = (mrr_mm3_min * Kt) / 60_000_000.0
            limit_kw = profile["max_power_kw"] * 0.8  # 80% safety margin

            if power_kw > limit_kw:
                violations.append(
                    f"{op_name}: required power {power_kw:.2f} kW exceeds "
                    f"80% limit ({limit_kw:.2f} kW) of {profile['name']}")
            elif power_kw > limit_kw * 0.8:
                warnings.append(
                    f"{op_name}: power {power_kw:.2f} kW approaching limit "
                    f"({limit_kw:.2f} kW)")

        # Tool deflection: delta = (F * L^3) / (3 * E * I)
        if overhang > 0 and dia > 0:
            chip_load_mm = SFM_TABLE.get(material, {}).get("chip_load_mm", 0.05)
            depth_factor = SFM_TABLE.get(material, {}).get("depth_factor", 0.7)
            axial_doc = doc if doc > 0 else dia * depth_factor * 0.5
            Kt_local = _KT_TABLE.get(material, 1500)
            F_tangential = chip_load_mm * axial_doc * Kt_local  # N
            r = dia / 2.0
            I = math.pi * r ** 4 / 4.0  # mm^4
            if I > 0:
                delta = (F_tangential * overhang ** 3) / (3.0 * _E_CARBIDE_N_MM2 * I)
                delta = round(delta, 5)
                if delta > _MAX_DEFLECTION_MM:
                    violations.append(
                        f"{op_name}: tool deflection {delta:.4f} mm at "
                        f"{overhang:.0f} mm overhang exceeds {_MAX_DEFLECTION_MM} mm limit")
                elif delta > _MAX_DEFLECTION_MM * 0.7:
                    warnings.append(
                        f"{op_name}: deflection {delta:.4f} mm approaching "
                        f"{_MAX_DEFLECTION_MM} mm limit")

    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Tool function: estimate_cycle_time
# ---------------------------------------------------------------------------

def estimate_cycle_time(
    operations: list[dict[str, Any]] | str,
    bbox: dict[str, float] | None = None,
    volume_cm3: float | None = None,
) -> float:
    """Estimate total machining cycle time in minutes.

    Uses MRR-based volume estimation for roughing and area-based for finishing.
    Includes tool change time (30s each) and rapid traverse overhead (15%).

    Args:
        operations: List of operation dicts with rpm, feed, doc, woc, name/role.
        bbox: Part bounding box {x_mm, y_mm, z_mm}.
        volume_cm3: Part volume in cm^3.

    Returns estimated time in minutes.
    """
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except (json.JSONDecodeError, ValueError):
            return 0.0

    if not operations:
        return 0.0

    bbox = bbox or {}
    x = bbox.get("x_mm", 100)
    y = bbox.get("y_mm", 100)
    z = bbox.get("z_mm", 20)

    # Rough stock volume (bbox + stock allowance)
    stock_vol_mm3 = (x + 2) * (y + 2) * (z + 3)  # 1mm sides, 1.5mm top
    # Part volume
    part_vol_mm3 = (volume_cm3 or stock_vol_mm3 * 0.4e-3) * 1000  # cm3->mm3
    # Material to remove
    removal_vol_mm3 = max(stock_vol_mm3 - part_vol_mm3, stock_vol_mm3 * 0.3)

    total_min = 0.0
    n_tool_changes = 0

    for op in operations:
        role = op.get("role", op.get("name", "")).lower()
        feed = float(op.get("feed_mm_per_min", 1000))
        doc = float(op.get("depth_of_cut_mm", 5))
        woc = float(op.get("width_of_cut_mm", 4))
        dia = float(op.get("tool_dia_mm", 10))

        if "rough" in role or "adaptive" in role:
            # Roughing: time = volume_to_remove / MRR
            mrr = doc * woc * feed  # mm^3/min
            if mrr > 0:
                total_min += removal_vol_mm3 / mrr

        elif "finish" in role or "parallel" in role:
            # Finishing: surface area scan at stepover
            surface_area = 2 * (x * y + y * z + x * z)
            stepover = dia * 0.1 if dia > 0 else 1.0  # 10% for finish
            n_passes = surface_area / (stepover * 1.0)  # 1mm effective width
            path_length = n_passes  # mm of travel
            if feed > 0:
                total_min += path_length / feed

        elif "contour" in role:
            # Contour: perimeter * height / feed
            perimeter = 2 * (x + y)
            n_z_passes = max(1, z / (doc if doc > 0 else z))
            path_length = perimeter * n_z_passes
            if feed > 0:
                total_min += path_length / feed

        elif "drill" in role:
            # Drill: pecking cycles
            n_holes = max(1, int(op.get("n_holes", 1)))
            depth = float(op.get("hole_depth_mm", z))
            peck = float(op.get("peck_mm", 2.0))
            n_pecks = max(1, math.ceil(depth / peck))
            plunge_feed = float(op.get("plunge_rate_mmpm", feed * 0.25))
            if plunge_feed > 0:
                time_per_hole = (depth * n_pecks * 1.5) / plunge_feed  # 1.5x for retract
                total_min += time_per_hole * n_holes

        n_tool_changes += 1

    # Tool change time (30s each, first tool is free)
    total_min += max(0, n_tool_changes - 1) * 0.5

    # Rapid traverse overhead: 15%
    total_min *= 1.15

    return round(total_min, 1)
