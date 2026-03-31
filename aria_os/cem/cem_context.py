"""
aria_os/cem/cem_context.py — Load live CEM geometry into a dict for LLM prompt injection.

Reads from:
  1. cem_design_history.json (latest run, most up-to-date)
  2. Fallback: calls aria_cem.ARIAInputs defaults directly

Returns a flat dict of key=value strings suitable for prompt injection.
"""
import json
import sys
from pathlib import Path
from typing import Optional

HISTORY_FILE = "cem_design_history.json"


def load_cem_geometry(repo_root: Optional[Path] = None) -> dict:
    """
    Return latest CEM geometry as flat dict.
    Keys are engineering parameter names, values are floats with units embedded in key names.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    history_path = repo_root / HISTORY_FILE
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
            if history:
                latest = history[-1]
                data = latest.get("data", {})
                inputs = data.get("inputs", {})
                outputs = data.get("outputs", {})
                ansi = data.get("ansi", {})
                return {
                    "source": "cem_history",
                    "timestamp": latest.get("timestamp", ""),
                    **{f"input_{k}": v for k, v in inputs.items()},
                    **{f"output_{k}": v for k, v in outputs.items()},
                    "ansi_all_pass": ansi.get("all_pass", False),
                    "ansi_arrest_pass": ansi.get("arrest_pass", False),
                    "ansi_force_pass": ansi.get("force_pass", False),
                }
        except Exception:
            pass

    # Fallback: compute from ARIAInputs defaults
    try:
        sys.path.insert(0, str(repo_root))
        from aria_cem import ARIAInputs, compute_aria

        inputs = ARIAInputs()
        geom = compute_aria(inputs)
        return {
            "source": "cem_defaults",
            "input_brake_drum_diameter_mm": inputs.brake_drum_diameter_mm,
            "input_rope_spool_hub_diameter_mm": inputs.rope_spool_hub_diameter_mm,
            "input_rope_spool_od_mm": inputs.rope_spool_od_mm,
            "input_housing_od_mm": inputs.housing_od_mm,
            "input_target_tension_N": inputs.target_tension_N,
            "input_min_hold_force_kN": inputs.min_hold_force_kN,
            "input_gearbox_ratio": inputs.gearbox_ratio,
            "output_brake_drum_wall_mm": round(geom.brake_drum.wall_thickness_mm, 3),
            "output_brake_drum_sf": round(geom.brake_drum.safety_factor, 2),
            "output_ratchet_n_teeth": geom.ratchet.n_teeth,
            "output_ratchet_face_width_mm": round(geom.ratchet.face_width_mm, 2),
            "output_ratchet_sf": round(geom.ratchet.safety_factor, 2),
            "output_flyweight_mass_g": round(geom.clutch.flyweight_mass_g, 2),
            "output_flyweight_radius_mm": round(geom.clutch.flyweight_radius_mm, 2),
            "output_engagement_v_ms": round(geom.clutch.engagement_v_m_s, 3),
            "output_engagement_rpm": round(geom.clutch.engagement_rpm, 1),
            "output_detection_margin_x": round(geom.clutch.safety_margin, 2),
            "output_spool_hub_d_mm": round(geom.spool.hub_diameter_mm, 1),
            "output_spool_flange_d_mm": round(geom.spool.flange_diameter_mm, 1),
            "output_spool_width_mm": round(geom.spool.width_mm, 1),
            "output_rope_capacity_m": round(geom.spool.capacity_m, 1),
            "output_gearbox_ratio": round(geom.motor.gearbox_ratio, 1),
            "output_housing_od_mm": round(geom.housing.od_mm, 1),
            "output_housing_wall_mm": round(geom.housing.wall_thickness_mm, 2),
            "output_housing_length_mm": round(geom.housing.length_mm, 1),
            "output_arrest_distance_m": round(geom.predicted_arrest_distance_m, 4),
            "output_peak_force_kN": round(geom.predicted_peak_force_kN, 3),
            "output_catch_time_ms": round(geom.predicted_catch_time_ms, 1),
            "ansi_all_pass": geom.predicted_peak_force_kN <= 6.0 and geom.predicted_arrest_distance_m <= 1.0,
        }
    except Exception:
        return {"source": "unavailable"}


def format_cem_block(cem: dict) -> str:
    """Format as a comment block for injection into Claude system prompts."""
    if cem.get("source") == "unavailable":
        return "# CEM geometry: unavailable — run CEM Design tab and click Regenerate first\n"

    lines = [
        f"# === CEM PHYSICS-DERIVED GEOMETRY (source: {cem.get('source','?')}, {cem.get('timestamp','')[:19]}) ===",
        "# These are the ground-truth dimensions derived from ANSI Z359.14 physics.",
        "# Every ARIA part must be dimensioned to match these values.",
        "# DESIGN INPUTS:",
    ]
    for k, v in sorted(cem.items()):
        if k.startswith("input_"):
            lines.append(f"#   {k[6:]}: {v}")
    lines.append("# PHYSICS-DERIVED OUTPUTS (use these for all part geometry):")
    for k, v in sorted(cem.items()):
        if k.startswith("output_"):
            lines.append(f"#   {k[7:]}: {v}")
    ansi_status = "PASS [OK]" if cem.get("ansi_all_pass") else "FAIL [X] - geometry may need redesign"
    lines.append(f"# ANSI Z359.14 compliance: {ansi_status}")
    lines.append("# === END CEM GEOMETRY ===")
    return "\n".join(lines)


def get_part_dimensions(part_id: str, cem: Optional[dict] = None, repo_root: Optional[Path] = None) -> dict:
    """
    Return CEM-derived dimensions most relevant to a part_id.
    """
    if cem is None:
        cem = load_cem_geometry(repo_root)

    def g(key: str, fallback):
        return cem.get(f"output_{key}", cem.get(f"input_{key}", fallback))

    mapping = {
        "aria_housing": {
            "od_mm": g("housing_od_mm", 260.0),
            "wall_mm": g("housing_wall_mm", 4.0),
            "length_mm": g("housing_length_mm", 200.0),
        },
        "aria_spool": {
            "hub_diameter_mm": g("spool_hub_d_mm", 120.0),
            "flange_diameter_mm": g("spool_flange_d_mm", 180.0),
            "width_mm": g("spool_width_mm", 50.0),
        },
        "aria_brake_drum": {
            "diameter_mm": g("input_brake_drum_diameter_mm", 200.0),
            "wall_mm": g("brake_drum_wall_mm", 3.0),
        },
        "aria_ratchet": {
            "n_teeth": g("ratchet_n_teeth", 20),
            "face_width_mm": g("ratchet_face_width_mm", 25.0),
        },
        "aria_motor_mount": {
            "housing_od_mm": g("housing_od_mm", 260.0),
        },
        "aria_rope_guide": {
            "rope_slot_width_mm": g("input_rope_diameter_mm", 10.0) + 2.0,
        },
    }
    return mapping.get(part_id, {})
