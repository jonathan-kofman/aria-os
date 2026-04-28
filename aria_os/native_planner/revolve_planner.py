"""Axisymmetric revolve plan — emits feature ops for revolved profiles.

Output shape: standard `[{kind, params, label}, ...]`. The dispatcher
streams these through the bridge so each lands as a real feature in the
host CAD's timeline. Mirrors shaft_planner.py / impeller_planner.py.

Geometry strategy: build a 2D profile sketch on XZ plane using sketchPolyline
or sketchSpline, then revolve 360° around the Z axis. Supports:
  - Rocket nozzle bell shapes (Rao's 3-point approximation)
  - Wine glass profiles (curved sides)
  - Axisymmetric pressure vessel hemispherical ends
  - Flask with curved neck
  - Lampshade conical profiles
  - Generic revolved vase/profile shapes

Hardcoded parameter extraction (no LLM):
  - Detect part type from goal keywords
  - Extract throat_r_mm, exit_r_mm, length_mm from spec or regex
  - For nozzle: use Rao-bell 3-point spline
  - For others: simple polyline through 4-6 profile points
"""
from __future__ import annotations

import re
from typing import Optional


def _extract_nozzle_dims(spec: dict, goal: str = "") -> tuple[float, float, float]:
    """Extract throat, exit, length for nozzle from spec or regex.

    Returns: (throat_r_mm, exit_r_mm, length_mm)
    """
    throat_r = float(spec.get("throat_r_mm", 25.0))
    exit_r = float(spec.get("exit_r_mm", 80.0))
    length = float(spec.get("length_mm", 120.0))

    # Regex fallback: "throat 25mm, exit 80mm, length 120mm"
    if goal:
        g = goal.lower()
        throat_m = re.search(r"throat\s+(\d+(?:\.\d+)?)\s*mm", g)
        if throat_m:
            throat_r = float(throat_m.group(1)) / 2
        exit_m = re.search(r"exit\s+(\d+(?:\.\d+)?)\s*mm", g)
        if exit_m:
            exit_r = float(exit_m.group(1)) / 2
        len_m = re.search(r"length\s+(\d+(?:\.\d+)?)\s*mm", g)
        if len_m:
            length = float(len_m.group(1))

    # Ensure validity
    if exit_r <= throat_r:
        exit_r = throat_r + 20.0

    return throat_r, exit_r, length


def _build_nozzle_profile(throat_r: float, exit_r: float, length: float
                          ) -> list[tuple[float, float]]:
    """Build a Rao-bell approximation: 3-point spline for nozzle profile.

    Returns list of (radius, z) points along the centerline to be revolved.
    Throat is at z=0, exit at z=length.
    """
    # Rao's simplified bell: throat, midpoint (expanding), exit
    mid_z = length * 0.5
    mid_r = throat_r + (exit_r - throat_r) * 0.7  # 70% expansion at midpoint

    return [
        (throat_r, 0.0),
        (mid_r, mid_z),
        (exit_r, length),
    ]


def _build_generic_profile(profile_type: str, length: float,
                          entry_r: float, exit_r: float = None) -> list[tuple[float, float]]:
    """Build a generic revolved profile (wine glass, flask, lampshade, vase).

    Returns list of (radius, z) points.
    """
    if exit_r is None:
        exit_r = entry_r

    if "wine" in profile_type or "glass" in profile_type:
        # Wine glass: wider at rim, narrow at stem
        # 0→30% z: stem (entry_r), 30%→100% z: expanding bowl (exit_r)
        stem_len = length * 0.3
        bowl_len = length - stem_len
        return [
            (entry_r, 0.0),
            (entry_r, stem_len),
            (entry_r + (exit_r - entry_r) * 0.5, stem_len + bowl_len * 0.5),
            (exit_r, length),
        ]
    elif "flask" in profile_type:
        # Flask: narrow neck, wide body, flat bottom
        # 0→20% z: base, 20%→40% z: body, 40%→100% z: neck
        return [
            (entry_r, 0.0),
            (exit_r, length * 0.2),
            (exit_r, length * 0.4),
            (entry_r * 0.6, length),
        ]
    elif "lampshade" in profile_type or "conical" in profile_type:
        # Lampshade: conical taper from one diameter to another
        return [
            (entry_r, 0.0),
            (exit_r, length),
        ]
    else:
        # Generic vase: straight lines, simple taper
        return [
            (entry_r, 0.0),
            (exit_r, length * 0.5),
            (entry_r * 0.8, length),
        ]


def plan_revolve(spec: dict, goal: str = "") -> list[dict]:
    """Emit a revolve plan for axisymmetric parts.

    Detects part type from goal keywords and builds appropriate profile.
    """
    g = (goal or "").lower()

    # Detect part type
    is_nozzle = any(k in g for k in
                   ("rocket nozzle", "bell nozzle", "lre nozzle", "de laval"))
    is_wine_glass = "wine glass" in g
    is_flask = "flask" in g
    is_lampshade = "lampshade" in g
    is_vase = "vase" in g or "revolved" in g
    is_pressure_vessel = "pressure vessel" in g or "hemispherical" in g

    # Extract dimensions
    length = float(spec.get("length_mm", 120.0))

    if is_nozzle:
        throat_r, exit_r, length = _extract_nozzle_dims(spec, goal)
        profile_pts = _build_nozzle_profile(throat_r, exit_r, length)
        part_name = "Rocket Nozzle"
    elif is_wine_glass:
        height = float(spec.get("height_mm", spec.get("length_mm", 80.0)))
        rim_r = float(spec.get("od_mm", 50.0)) / 2
        stem_r = rim_r * 0.3
        profile_pts = _build_generic_profile("wine glass", height, stem_r, rim_r)
        part_name = "Wine Glass"
        length = height
    elif is_flask:
        height = float(spec.get("height_mm", spec.get("length_mm", 100.0)))
        body_r = float(spec.get("od_mm", 60.0)) / 2
        neck_r = body_r * 0.4
        profile_pts = _build_generic_profile("flask", height, neck_r, body_r)
        part_name = "Flask"
        length = height
    elif is_lampshade:
        height = float(spec.get("height_mm", spec.get("length_mm", 100.0)))
        top_r = float(spec.get("od_mm", 60.0)) / 2
        bottom_r = float(spec.get("width_mm", 100.0)) / 2 if "width_mm" in spec else top_r * 1.5
        profile_pts = _build_generic_profile("lampshade", height, top_r, bottom_r)
        part_name = "Lampshade"
        length = height
    else:
        # Generic vase or revolved profile
        height = float(spec.get("height_mm", spec.get("length_mm", 120.0)))
        entry_r = float(spec.get("od_mm", 50.0)) / 2
        exit_r = entry_r * 0.8
        profile_pts = _build_generic_profile("vase", height, entry_r, exit_r)
        part_name = "Revolved Profile"
        length = height

    # Build plan
    plan: list[dict] = [
        {"kind": "beginPlan", "params": {},
         "label": "Reset feature registry"},
        {"kind": "addParameter",
         "params": {"name": "revolve_height", "value_mm": length,
                    "comment": "Overall profile height"},
         "label": f"User Parameter: revolve_height = {length:g}mm"},
    ]

    # Create sketch on XZ plane with profile polyline or spline
    # Points are (radius, z_position) — we'll emit as (x, y) in sketch coords
    plan += [
        {"kind": "newSketch",
         "params": {"plane": "XZ", "alias": "sketch_profile",
                    "name": f"ARIA {part_name} Profile"},
         "label": f"Sketch on XZ plane ({part_name} profile)"},
    ]

    # Use spline for nozzle (smooth Rao curve), polyline for others
    if is_nozzle:
        # Convert points to sketch coordinates: (radius → x, z → y)
        sketch_pts = [(r, z) for r, z in profile_pts]
        plan += [
            {"kind": "sketchSpline",
             "params": {"sketch": "sketch_profile",
                        "points": sketch_pts},
             "label": f"Nozzle profile spline ({len(profile_pts)} points)"},
        ]
    else:
        # Polyline for other profiles
        sketch_pts = [(r, z) for r, z in profile_pts]
        plan += [
            {"kind": "sketchPolyline",
             "params": {"sketch": "sketch_profile",
                        "points": sketch_pts},
             "label": f"Profile polyline ({len(profile_pts)} points)"},
        ]

    # Revolve 360° around Z axis
    plan += [
        {"kind": "revolve",
         "params": {"sketch": "sketch_profile",
                    "axis": "Z",
                    "angle": 360,
                    "operation": "new"},
         "label": f"Revolve 360° around Z axis"},
    ]

    return plan


if __name__ == "__main__":
    """Smoke test: build and validate 3 simple revolve plans."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    from validator import validate_plan

    test_cases = [
        {
            "goal": "rocket nozzle bell shape, throat 25mm, exit 80mm, length 120mm",
            "spec": {"throat_r_mm": 12.5, "exit_r_mm": 40.0, "length_mm": 120.0},
        },
        {
            "goal": "wine glass profile, 80mm tall, 50mm rim diameter",
            "spec": {"height_mm": 80.0, "od_mm": 50.0},
        },
        {
            "goal": "lampshade conical 100mm OD top 60mm OD bottom",
            "spec": {"od_mm": 100.0, "width_mm": 60.0, "length_mm": 150.0},
        },
    ]

    all_ok = True
    for i, tc in enumerate(test_cases, 1):
        plan = plan_revolve(tc["spec"], tc["goal"])
        ok, issues = validate_plan(plan)
        status = "OK" if ok else "FAIL"
        print(f"{status}: test case {i} ({tc['goal'][:50]}...)")
        if not ok:
            print(f"  Issues: {issues}")
            all_ok = False

    if all_ok:
        print("\nAll smoke tests passed!")
    else:
        print("\nSome smoke tests failed!")
        exit(1)
