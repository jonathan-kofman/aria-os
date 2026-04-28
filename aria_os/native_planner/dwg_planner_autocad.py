"""AutoCAD drawing planner — emits op sequence for AutoCAD drawings.

Unlike SolidWorks/Fusion drawings (which are views of a 3D model), AutoCAD
drawings are primarily 2D geometric constructs. This planner generates:
  - 2D geometry (circles, polylines, rectangles)
  - Professional dimensioning (DIMLINEAR, DIMDIAMETER — native AutoCAD)
  - GD&T frames (TOLERANCE command — native, powerful)
  - Datum references (A, B, C)

For parts that do have 3D geometry (extrude, fillet), AutoCAD can handle those
too via EXTRUDE/FILLET commands, so the ops are compatible.

Output is a list of op dicts suitable for POST to the aria_autocad_server /op endpoint.
"""
from __future__ import annotations


def plan_autocad_drawing(spec: dict, part_meta: dict | None = None) -> list[dict]:
    """Professional 2D/3D drawing for AutoCAD with full dimensioning and GD&T.

    Args:
        spec: Dict with part_id, material, description, n_bolts, od_mm, bore_mm, etc.
        part_meta: Computed metadata (material, finish, mass).

    Returns: List of drawing operations in execution order.
    """
    if part_meta is None:
        part_meta = {}

    part_number = spec.get("part_id") or spec.get("name") or "ARIA_PART"
    material = spec.get("material") or part_meta.get("material", "Steel")
    finish = part_meta.get("finish", "As machined")
    mass_g = part_meta.get("mass_g", 0.0)
    revision = spec.get("revision", "A")
    description = spec.get("description", "")
    n_bolts = int(spec.get("n_bolts", 0))

    plan: list[dict] = [
        {
            "kind": "beginPlan",
            "params": {"name": f"{part_number}_drawing"},
            "label": "New AutoCAD drawing (modelspace)",
        },
    ]

    # Main geometry: if spec has bore/od (flange-like), draw circles
    # Otherwise, draw a general rectangle and fill in as needed
    od_mm = spec.get("od_mm")
    bore_mm = spec.get("bore_mm")
    width_mm = spec.get("width_mm")
    height_mm = spec.get("height_mm")
    depth_mm = spec.get("depth_mm")

    if od_mm and bore_mm:
        # Flange/disc-like: concentric circles
        plan.append({
            "kind": "sketchCircle",
            "params": {"x_mm": 0, "y_mm": 0, "radius_mm": float(od_mm) / 2},
            "label": f"Outer circle ø{od_mm:.1f}mm",
        })
        plan.append({
            "kind": "sketchCircle",
            "params": {"x_mm": 0, "y_mm": 0, "radius_mm": float(bore_mm) / 2},
            "label": f"Center bore ø{bore_mm:.1f}mm",
        })
    elif width_mm and height_mm:
        # Plate-like: rectangle
        plan.append({
            "kind": "sketchRect",
            "params": {
                "x_mm": 0,
                "y_mm": 0,
                "width_mm": float(width_mm),
                "height_mm": float(height_mm),
            },
            "label": f"Rectangle {width_mm}×{height_mm}mm",
        })

    # Bolt holes (if PCD pattern exists)
    bolt_circle_r = spec.get("bolt_circle_r_mm")
    bolt_dia = spec.get("bolt_dia_mm", 5.0)
    if n_bolts > 0 and bolt_circle_r:
        # Draw bolt holes on a circle
        import math
        angle_step = 360.0 / n_bolts
        for i in range(n_bolts):
            angle_rad = math.radians(i * angle_step)
            x = float(bolt_circle_r) * math.cos(angle_rad)
            y = float(bolt_circle_r) * math.sin(angle_rad)
            plan.append({
                "kind": "sketchCircle",
                "params": {
                    "x_mm": x,
                    "y_mm": y,
                    "radius_mm": float(bolt_dia) / 2,
                },
                "label": f"Bolt hole {i+1}/{n_bolts} ø{bolt_dia:.1f}mm at PCD",
            })

    # Dimensions: OD, bore, width, height, depth as appropriate
    if od_mm:
        plan.append({
            "kind": "diameterDimension",
            "params": {
                "x_mm": 0,
                "y_mm": float(od_mm) / 2 + 10,
                "diameter_mm": float(od_mm),
                "label": f"ø{od_mm:.1f}",
                "view": "top",
            },
            "label": f"Dimension: OD ø{od_mm:.1f}mm",
        })

    if bore_mm:
        plan.append({
            "kind": "diameterDimension",
            "params": {
                "x_mm": 0,
                "y_mm": -float(bore_mm) / 2 - 10,
                "diameter_mm": float(bore_mm),
                "label": f"ø{bore_mm:.1f}",
                "view": "top",
            },
            "label": f"Dimension: Bore ø{bore_mm:.1f}mm",
        })

    if width_mm:
        plan.append({
            "kind": "linearDimension",
            "params": {
                "x1_mm": 0,
                "y1_mm": -float(height_mm) / 2 - 15 if height_mm else -25,
                "x2_mm": float(width_mm),
                "y2_mm": -float(height_mm) / 2 - 15 if height_mm else -25,
                "label": f"{width_mm:.1f}",
                "view": "top",
            },
            "label": f"Dimension: Width {width_mm:.1f}mm",
        })

    if height_mm:
        plan.append({
            "kind": "linearDimension",
            "params": {
                "x1_mm": float(width_mm) / 2 + 15 if width_mm else 25,
                "y1_mm": 0,
                "x2_mm": float(width_mm) / 2 + 15 if width_mm else 25,
                "y2_mm": float(height_mm),
                "label": f"{height_mm:.1f}",
                "view": "front",
            },
            "label": f"Dimension: Height {height_mm:.1f}mm",
        })

    if depth_mm:
        plan.append({
            "kind": "linearDimension",
            "params": {
                "x1_mm": float(width_mm) / 2 + 15 if width_mm else 25,
                "y1_mm": 0,
                "x2_mm": float(width_mm) / 2 + 15 if width_mm else 25,
                "y2_mm": float(depth_mm),
                "label": f"{depth_mm:.1f}",
                "view": "right",
            },
            "label": f"Dimension: Depth {depth_mm:.1f}mm",
        })

    # Datum references: A = primary (bottom/front), B = secondary, C = tertiary
    plan.append({
        "kind": "datumLabel",
        "params": {"feature": "bottom_face", "label": "A", "view": "front"},
        "label": "Datum A (primary — bottom face)",
    })
    plan.append({
        "kind": "datumLabel",
        "params": {"feature": "front_face", "label": "B", "view": "top"},
        "label": "Datum B (secondary — front face)",
    })
    plan.append({
        "kind": "datumLabel",
        "params": {"feature": "right_face", "label": "C", "view": "right"},
        "label": "Datum C (tertiary — right face)",
    })

    # GD&T frames (AutoCAD TOLERANCE command — very powerful)
    # Flatness on datum A: 0.05mm
    plan.append({
        "kind": "gdtFrame",
        "params": {
            "characteristic": "flatness",
            "tolerance": 0.05,
            "datum_ref": "A",
            "feature": "bottom_face",
            "view": "front",
        },
        "label": "GD&T: Flatness 0.05mm on datum A",
    })

    # Perpendicularity of datum B to A: 0.1mm
    plan.append({
        "kind": "gdtFrame",
        "params": {
            "characteristic": "perpendicularity",
            "tolerance": 0.1,
            "datum_ref": "A",
            "feature": "front_face",
            "view": "top",
        },
        "label": "GD&T: Perpendicularity 0.1mm to datum A",
    })

    # Position tolerance on bolt holes (if present): 0.2mm MMC
    if n_bolts > 0:
        plan.append({
            "kind": "gdtFrame",
            "params": {
                "characteristic": "position",
                "tolerance": 0.2,
                "datum_ref": "A",
                "feature": "bolt_holes",
                "view": "top",
            },
            "label": f"GD&T: Position 0.2mm MMC on {n_bolts} bolt holes",
        })

    # Runout tolerance (for parts with a central axis like flanges)
    if bore_mm and od_mm:
        plan.append({
            "kind": "gdtFrame",
            "params": {
                "characteristic": "runout",
                "tolerance": 0.1,
                "datum_ref": "A",
                "feature": "bore",
                "view": "front",
            },
            "label": "GD&T: Runout 0.1mm on bore",
        })

    # Material and finish note (stored as a parameter for reference)
    plan.append({
        "kind": "addParameter",
        "params": {"name": "material", "value": material},
        "label": f"Material: {material}",
    })
    plan.append({
        "kind": "addParameter",
        "params": {"name": "finish", "value": finish},
        "label": f"Finish: {finish}",
    })
    if mass_g > 0:
        plan.append({
            "kind": "addParameter",
            "params": {"name": "mass_g", "value": str(mass_g)},
            "label": f"Mass: {mass_g:.1f}g",
        })

    return plan


# Smoke test: print sample ops without requiring pyautocad
if __name__ == "__main__":
    import json

    test_spec = {
        "part_id": "FLANGE_001",
        "material": "Aluminum 6061",
        "od_mm": 80.0,
        "bore_mm": 30.0,
        "thickness_mm": 10.0,
        "n_bolts": 4,
        "bolt_circle_r_mm": 50.0,
        "bolt_dia_mm": 8.0,
        "width_mm": 100.0,
        "height_mm": 100.0,
        "depth_mm": 50.0,
    }

    ops = plan_autocad_drawing(test_spec)
    print(f"Generated {len(ops)} ops:\n")
    for i, op in enumerate(ops, 1):
        print(f"{i:2d}. {op.get('label', 'unlabeled')}")
        print(f"    kind: {op['kind']}")
        if op.get("params"):
            print(f"    params: {json.dumps(op['params'], indent=6)}")
        print()
