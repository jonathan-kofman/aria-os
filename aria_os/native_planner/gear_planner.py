"""Gear planner — emits a spur-gear blank (disc + bore) plus N teeth
arrayed via circularPattern.

Tooth profile is approximated by a small rectangle riding on the
addendum circle. That's geometrically a stub-tooth gear, not a true
involute, but it's enough for visual + DFM verification and for the
SW addin's circularPattern to produce a recognizable gear silhouette.
A real involute profile would need sketchSpline/sketchTangentArc work
that the LLM planner is currently unreliable at — better to ship a
stable stub and let the user request true involute via a future op.
"""
from __future__ import annotations

import math


def plan_gear(spec: dict, goal: str = "") -> list[dict]:
    od = float(spec.get("od_mm", 50.0))
    n_teeth = int(spec.get("n_teeth", 24))
    height = float(spec.get("height_mm",
                              spec.get("thickness_mm", 15.0)))
    bore = float(spec.get("bore_mm",
                            spec.get("id_mm", 10.0)))
    module = float(spec.get("module_mm", od / max(n_teeth, 1)))
    # Standard module geometry: addendum = 1×module, dedendum = 1.25×module.
    # Tooth height = 2.25×module. Outside diameter = pitch + 2×module.
    # Reverse-engineer a "pitch diameter" from the user's OD if no module
    # was given so the tooth height stays plausible.
    addendum = max(0.5, module * 1.0)
    pitch_r = max(od / 2 - addendum, bore / 2 + 1.0)
    tooth_h = addendum * 2.0
    # Tooth width along the pitch circle: half the circular pitch (one
    # tooth fills half the spacing, the gap is the other half).
    circ_pitch = (2 * math.pi * pitch_r) / max(n_teeth, 1)
    tooth_w = max(circ_pitch * 0.5, 1.0)
    # Place ONE tooth at the +X end of the pitch circle, then pattern
    # n_teeth times around Z.
    t_cx = pitch_r + tooth_h / 2.0
    t_cy = 0.0

    plan: list[dict] = [
        {"kind": "beginPlan", "params": {},
         "label": "Reset registry"},
        {"kind": "addParameter",
         "params": {"name": "gear_OD", "value_mm": od,
                    "comment": "Gear outside diameter"},
         "label": f"User Parameter: gear_OD = {od:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "gear_bore", "value_mm": bore,
                    "comment": "Shaft bore"},
         "label": f"User Parameter: gear_bore = {bore:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "gear_height", "value_mm": height,
                    "comment": "Face width / thickness"},
         "label": f"User Parameter: gear_height = {height:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "gear_n_teeth", "value_mm": n_teeth,
                    "unit": "",
                    "comment": "Tooth count"},
         "label": f"User Parameter: gear_n_teeth = {n_teeth}"},
        {"kind": "addParameter",
         "params": {"name": "gear_module", "value_mm": module,
                    "comment": "ISO module (mm/tooth)"},
         "label": f"User Parameter: gear_module = {module:g}"},
        # Blank disc — start with the pitch-circle disc, NOT od/2, so the
        # patterned teeth ride above the blank and the silhouette matches
        # n_teeth points poking out.
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_blank",
                    "name": "ARIA Gear Blank"},
         "label": "Sketch blank on XY"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_blank", "cx": 0, "cy": 0,
                    "r": pitch_r},
         "label": f"Pitch-circle disc Ø{pitch_r*2:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sk_blank", "distance": height,
                    "operation": "new", "alias": "gear_body"},
         "label": f"Extrude blank disc {height:g}mm (new body)"},
    ]
    # Emit N tooth-rectangles at rotated positions instead of using
    # circularPattern (broken in SW2024 — see
    # feedback_sw2024_idispatch_quirks). For a rect tooth we have to
    # emit each one inline since the rect rotates with the angle —
    # sketchRect doesn't have a built-in rotation param, so we offset
    # the center to (rRot*cos, rRot*sin) but keep the rect axis-aligned
    # (acceptable visual approximation for a stub-tooth gear).
    for i in range(n_teeth):
        theta = 2 * math.pi * i / n_teeth
        cx = t_cx * math.cos(theta) - t_cy * math.sin(theta)
        cy = t_cx * math.sin(theta) + t_cy * math.cos(theta)
        sk_alias = f"sk_tooth_{i}"
        plan.extend([
            {"kind": "newSketch",
             "params": {"plane": "XY", "alias": sk_alias,
                        "name": f"ARIA Gear Tooth {i+1}"},
             "label": f"Sketch tooth {i+1}/{n_teeth}"},
            {"kind": "sketchRect",
             "params": {"sketch": sk_alias, "cx": cx, "cy": cy,
                         "w": tooth_h, "h": tooth_w},
             "label": (f"Tooth {i+1}/{n_teeth} {tooth_h:g}×{tooth_w:.2f}mm "
                        f"at ({cx:+.1f},{cy:+.1f})mm")},
            {"kind": "extrude",
             "params": {"sketch": sk_alias, "distance": height,
                         "operation": "join",
                         "alias": f"tooth_{i}"},
             "label": f"Join tooth {i+1}/{n_teeth}, extrude {height:g}mm"},
        ])
    plan.extend([
        # Center bore — cut last so it's straightforward
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_bore",
                    "name": "ARIA Gear Bore"},
         "label": "Sketch bore on XY"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_bore", "cx": 0, "cy": 0,
                    "r": bore / 2.0},
         "label": f"Bore circle Ø{bore:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sk_bore", "distance": height * 1.5,
                    "operation": "cut", "alias": "cut_bore"},
         "label": f"Cut bore through ({height * 1.5:g}mm)"},
    ])
    return plan
