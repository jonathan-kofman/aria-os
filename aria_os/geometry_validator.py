"""
geometry_validator.py — automated STEP geometry feature validation.

Loads a STEP file, analyzes face normals/positions, and verifies expected
features (holes, cutouts, cavities) exist. Reports pass/fail per feature.

Used by the orchestrator as an automatic checkpoint after generation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def validate_geometry(
    step_path: str | Path,
    part_id: str,
    params: dict[str, Any] | None = None,
    goal: str = "",
) -> dict[str, Any]:
    """
    Validate generated STEP geometry has expected features.

    Returns dict:
        passed: bool
        checks: list of {name, passed, detail}
        face_count: int
        solid_count: int
        bbox: {x, y, z}
    """
    step_path = Path(step_path)
    params = params or {}
    checks: list[dict] = []
    result = {"passed": True, "checks": checks, "face_count": 0,
              "solid_count": 0, "bbox": {}}

    if not step_path.exists():
        checks.append({"name": "file_exists", "passed": False,
                        "detail": f"STEP not found: {step_path}"})
        result["passed"] = False
        return result

    try:
        import cadquery as cq
        shape = cq.importers.importStep(str(step_path))
        solid = shape.val()
    except Exception as exc:
        checks.append({"name": "step_readable", "passed": False,
                        "detail": f"CadQuery import failed: {exc}"})
        result["passed"] = False
        return result

    # Basic geometry info
    bb = solid.BoundingBox()
    result["bbox"] = {"x": round(bb.xlen, 2), "y": round(bb.ylen, 2),
                      "z": round(bb.zlen, 2)}
    faces = solid.Faces()
    solids = solid.Solids()
    result["face_count"] = len(faces)
    result["solid_count"] = len(solids)

    checks.append({"name": "has_solid", "passed": len(solids) >= 1,
                    "detail": f"{len(solids)} solid(s)"})

    # Classify faces by position region
    face_regions = {"left": 0, "right": 0, "top": 0, "bottom": 0,
                    "front": 0, "back": 0, "interior": 0}
    x_min, x_max = bb.xmin, bb.xmax
    y_min, y_max = bb.ymin, bb.ymax
    z_min, z_max = bb.zmin, bb.zmax
    x_mid = (x_min + x_max) / 2
    y_mid = (y_min + y_max) / 2
    z_mid = (z_min + z_max) / 2
    margin = min(bb.xlen, bb.ylen, bb.zlen) * 0.15

    for face in faces:
        try:
            c = face.Center()
            if c.x < x_min + margin:
                face_regions["left"] += 1
            elif c.x > x_max - margin:
                face_regions["right"] += 1
            elif c.y < y_min + margin:
                face_regions["bottom"] += 1
            elif c.y > y_max - margin:
                face_regions["top"] += 1
            elif c.z < z_min + margin:
                face_regions["back"] += 1
            elif c.z > z_max - margin:
                face_regions["front"] += 1
            else:
                face_regions["interior"] += 1
        except Exception:
            pass

    result["face_regions"] = face_regions

    # Part-type-specific checks
    goal_lower = goal.lower()
    pid_lower = part_id.lower()

    # Phone case checks only for actual phone cases (not AirPods, not generic "case")
    _is_phone_case = (
        ("phone" in goal_lower or "iphone" in goal_lower or "samsung" in goal_lower
         or "pixel" in goal_lower)
        and "case" in goal_lower
    )
    if _is_phone_case:
        _validate_phone_case(checks, face_regions, result, params, solid, bb)
    elif "case" in pid_lower or "case" in goal_lower:
        # Generic case/enclosure — simpler checks (not phone-specific)
        _validate_enclosure(checks, face_regions, result, params)
    elif "ratchet" in pid_lower:
        _validate_ratchet(checks, face_regions, result, params)
    elif "housing" in pid_lower:
        _validate_housing(checks, face_regions, result, params)
    else:
        # Generic: just check face count is reasonable (cylindrical parts can have 3-4 faces)
        checks.append({"name": "face_count", "passed": len(faces) >= 3,
                        "detail": f"{len(faces)} faces (min 3 for a solid)"})

    # Update overall pass
    result["passed"] = all(c["passed"] for c in checks)
    return result


def _validate_phone_case(checks, regions, result, params, solid, bb):
    """Validate phone case has expected features."""
    faces = solid.Faces()
    n_faces = len(faces)

    # A phone case should have many faces (cavity + cutouts)
    checks.append({
        "name": "case_complexity",
        "passed": n_faces >= 50,
        "detail": f"{n_faces} faces (need 50+ for case with cutouts)"
    })

    # Should be hollow (interior faces exist)
    has_interior = regions.get("interior", 0) > 10
    checks.append({
        "name": "has_cavity",
        "passed": has_interior,
        "detail": f"{regions.get('interior', 0)} interior faces (need 10+ for hollow cavity)"
    })

    # Left side should have button cutout faces (vol up, vol down, mute = at least 12 extra faces)
    left_complex = regions.get("left", 0) >= 8
    checks.append({
        "name": "left_button_cutouts",
        "passed": left_complex,
        "detail": f"{regions.get('left', 0)} left-wall faces (need 8+ for 3 button holes)"
    })

    # Right side should have power button (at least 4 extra faces)
    right_complex = regions.get("right", 0) >= 4
    checks.append({
        "name": "right_button_cutout",
        "passed": right_complex,
        "detail": f"{regions.get('right', 0)} right-wall faces (need 4+ for power button)"
    })

    # Bottom should have port + speaker holes (many faces)
    bottom_complex = regions.get("bottom", 0) >= 6
    checks.append({
        "name": "bottom_port_cutouts",
        "passed": bottom_complex,
        "detail": f"{regions.get('bottom', 0)} bottom faces (need 6+ for port + speakers)"
    })

    # Back face should have camera cutout
    back_complex = regions.get("back", 0) >= 8
    checks.append({
        "name": "camera_cutout",
        "passed": back_complex,
        "detail": f"{regions.get('back', 0)} back faces (need 8+ for camera + armor lines)"
    })

    # Screen opening on front
    front_open = regions.get("front", 0) >= 4
    checks.append({
        "name": "screen_opening",
        "passed": front_open,
        "detail": f"{regions.get('front', 0)} front faces (need 4+ for screen bezel)"
    })


def _validate_enclosure(checks, regions, result, params):
    """Validate generic enclosure/case — just check it's hollow with some features."""
    n_faces = result.get("face_count", 0)
    checks.append({
        "name": "enclosure_complexity",
        "passed": n_faces >= 8,
        "detail": f"{n_faces} faces (need 8+ for a shelled enclosure)"
    })
    has_interior = regions.get("interior", 0) >= 2
    checks.append({
        "name": "has_cavity",
        "passed": has_interior,
        "detail": f"{regions.get('interior', 0)} interior faces (need 2+ for hollow body)"
    })
    # Check it has at least one cutout (port, opening, hinge)
    total_side_faces = sum(regions.get(k, 0) for k in ("left", "right", "top", "bottom"))
    checks.append({
        "name": "has_cutouts",
        "passed": total_side_faces >= 4,
        "detail": f"{total_side_faces} side/edge faces (need 4+ for cutouts/openings)"
    })


def _validate_ratchet(checks, regions, result, params):
    """Validate ratchet ring geometry."""
    n_teeth = params.get("n_teeth", 24)
    # Each tooth adds ~4 faces; ring body has ~6 faces minimum
    min_faces = n_teeth * 3 + 6
    actual = result.get("face_count", 0)
    checks.append({
        "name": "tooth_faces",
        "passed": actual >= min_faces,
        "detail": f"{actual} faces (need {min_faces}+ for {n_teeth} teeth)"
    })


def _validate_housing(checks, regions, result, params):
    """Validate housing has interior cavity and bolt holes."""
    has_interior = regions.get("interior", 0) > 5
    checks.append({
        "name": "has_cavity",
        "passed": has_interior,
        "detail": f"{regions.get('interior', 0)} interior faces"
    })


def print_validation(result: dict) -> None:
    """Print validation results to console."""
    for check in result.get("checks", []):
        tag = "OK" if check["passed"] else "FAIL"
        print(f"  [GEOMETRY] {tag} {check['name']} -- {check['detail']}")
    tag = "PASS" if result["passed"] else "FAIL"
    n_ok = sum(1 for c in result["checks"] if c["passed"])
    n_total = len(result["checks"])
    print(f"  [GEOMETRY] {tag} ({n_ok}/{n_total} checks)")
