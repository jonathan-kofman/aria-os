"""Flange plan — emits a sequence of native feature ops for a bolted flange.

Output shape: `[{"kind": "<handler>", "params": {...}, "label": "human
label"}, ...]`. The dispatcher streams these through the bridge so each
one lands in Fusion's timeline as a real feature.

Geometry strategy: everything referenced off world XY, so the plan works
without any face-picking logic. The body is built by extruding up; the
bolt-hole pattern and bore cut down through it.
"""
from __future__ import annotations

from .iso_hardware import resolve_bolt_hole


def plan_flange(spec: dict, goal: str = "") -> list[dict]:
    od       = float(spec.get("od_mm",            120.0))
    bore     = float(spec.get("bore_mm",
                               spec.get("id_mm",   20.0)))
    thick    = float(spec.get("thickness_mm",
                               spec.get("height_mm", 6.0)))
    n_bolts  = int(spec.get("n_bolts", 4))
    bolt_r   = float(spec.get("bolt_circle_r_mm",
                               (od + bore) / 4))   # midway between bore and OD
    # Resolve hole size: if the prompt says "M6 holes" we use ISO 273
    # close-fit clearance (6.6mm), not the M6 nominal 6mm. That's what
    # a machinist would drill when handed a drawing that says "4x M6".
    hole = resolve_bolt_hole(spec, goal)
    bolt_dia = hole["hole_dia_mm"]

    # Extend the cut distance a bit past the thickness so bolts/bore go
    # fully through regardless of floating-point rounding.
    cut_dist = thick * 1.5

    plan: list[dict] = [
        {"kind": "beginPlan", "params": {},
         "label": "Reset feature registry"},
        # --- Declare User Parameters — users edit these in Fusion's
        # --- Parameters dialog to rebuild the whole part without re-
        # --- prompting ARIA. Every dim above references these.
        {"kind": "addParameter",
         "params": {"name": "flange_OD", "value_mm": od,
                     "comment": "Outer diameter"},
         "label": f"User Parameter: flange_OD = {od:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "flange_bore", "value_mm": bore,
                     "comment": "Center bore Ø"},
         "label": f"User Parameter: flange_bore = {bore:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "flange_thickness", "value_mm": thick,
                     "comment": "Flange plate thickness"},
         "label": f"User Parameter: flange_thickness = {thick:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "flange_bolt_circle_r",
                     "value_mm": bolt_r,
                     "comment": "Bolt-circle radius (PCD/2)"},
         "label": f"User Parameter: flange_bolt_circle_r = {bolt_r:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "flange_bolt_dia", "value_mm": bolt_dia,
                     "comment": (f"Clearance hole Ø for "
                                  f"{hole['thread']} "
                                  f"({hole['fit']} fit, ISO 273)"
                                  if hole["source"] == "iso"
                                  else "Bolt hole diameter")},
         "label": (f"User Parameter: flange_bolt_dia = {bolt_dia:g}mm "
                    f"({hole['thread']} {hole['fit']} clearance, "
                    f"ISO 273)"
                    if hole["source"] == "iso"
                    else f"User Parameter: flange_bolt_dia = {bolt_dia:g}mm")},
        # --- Body ---
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sketch_body",
                    "name": "ARIA Flange Body"},
         "label": "Sketch on XY plane"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sketch_body", "cx": 0, "cy": 0, "r": od / 2.0},
         "label": f"Outer circle Ø{od:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sketch_body", "distance": thick,
                    "operation": "new", "alias": "body_flange"},
         "label": f"Extrude {thick:g}mm (new body)"},
        # --- Bolt-hole pattern ---
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sketch_bolt",
                    "name": "ARIA Bolt Hole"},
         "label": "Sketch on XY plane"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sketch_bolt",
                    "cx": bolt_r, "cy": 0, "r": bolt_dia / 2.0},
         "label": f"Bolt circle Ø{bolt_dia:g}mm at r={bolt_r:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sketch_bolt", "distance": cut_dist,
                    "operation": "cut", "alias": "cut_bolt"},
         "label": f"Cut bolt hole through ({cut_dist:g}mm)"},
        {"kind": "circularPattern",
         "params": {"feature": "cut_bolt", "axis": "Z",
                    "count": n_bolts, "alias": "pat_bolts"},
         "label": f"Circular pattern × {n_bolts} around Z"},
        # --- Center bore ---
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sketch_bore",
                    "name": "ARIA Bore"},
         "label": "Sketch on XY plane"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sketch_bore", "cx": 0, "cy": 0, "r": bore / 2.0},
         "label": f"Bore circle Ø{bore:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sketch_bore", "distance": cut_dist,
                    "operation": "cut", "alias": "cut_bore"},
         "label": f"Cut bore through ({cut_dist:g}mm)"},
    ]
    return plan
