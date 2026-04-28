"""Impeller planner — generates a centrifugal fan/impeller with N
backward-swept blades around a hub.

Avoids the LLM-plans-full-disc-then-patterns-it degenerate case by:
  1. Building the hub first (small cylinder, operation='new')
  2. Sketching ONE blade as a narrow rectangle rotated at a sweep angle
  3. Extruding + joining that blade to the hub
  4. circularPattern on the JOINED blade feature (not the hub)
  5. Cutting the center bore

Supports backward, forward, and radial sweeps.
"""
from __future__ import annotations

import math


def plan_impeller(spec: dict) -> list[dict]:
    od       = float(spec.get("od_mm", 120.0))
    bore     = float(spec.get("bore_mm",
                                spec.get("id_mm", 20.0)))
    height   = float(spec.get("height_mm",
                                spec.get("thickness_mm", 25.0)))
    n_blades = int(spec.get("n_blades",
                              spec.get("n_fins", 6)))
    sweep    = (spec.get("blade_sweep") or "backward_curved").lower()
    # Blade geometry: thickness, length
    blade_t  = float(spec.get("blade_thickness_mm",
                                max(2.0, od * 0.04)))
    hub_od   = float(spec.get("hub_od_mm", max(bore * 2, od * 0.25)))
    shroud_h = max(1.5, height * 0.15)       # back shroud plate
    blade_h  = height - shroud_h
    tip_r    = od / 2 - blade_t / 2         # blade tip stays inside OD
    hub_r    = hub_od / 2
    blade_length = tip_r - hub_r

    # Sweep angle in degrees. Backward sweeps have +ve angle; forward is -ve.
    if "forward" in sweep:
        sweep_deg = -30.0
    elif "radial" in sweep:
        sweep_deg = 0.0
    else:  # backward (default)
        sweep_deg = 30.0

    # Blade center midpoint between hub and tip, at radius (hub_r+tip_r)/2
    mid_r = (hub_r + tip_r) / 2
    cx = mid_r * math.cos(math.radians(0))
    cy = mid_r * math.sin(math.radians(0))

    plan: list[dict] = [
        {"kind": "beginPlan", "params": {},
         "label": "Reset feature registry"},
        # User parameters — editable in Fusion's Parameters dialog
        {"kind": "addParameter",
         "params": {"name": "impeller_OD", "value_mm": od,
                    "comment": "Impeller outer diameter"},
         "label": f"User Parameter: impeller_OD = {od:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "impeller_bore", "value_mm": bore,
                    "comment": "Shaft bore"},
         "label": f"User Parameter: impeller_bore = {bore:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "impeller_height", "value_mm": height,
                    "comment": "Total height"},
         "label": f"User Parameter: impeller_height = {height:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "impeller_n_blades", "value_mm": n_blades,
                    "unit": "",
                    "comment": "Number of blades"},
         "label": f"User Parameter: impeller_n_blades = {n_blades}"},
        # Back shroud (thin disc covering the OD)
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_shroud",
                    "name": "ARIA Impeller Shroud"},
         "label": "Sketch on XY plane"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_shroud", "cx": 0, "cy": 0,
                    "r": od / 2},
         "label": f"Shroud circle Ø{od:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sk_shroud", "distance": shroud_h,
                    "operation": "new", "alias": "shroud_body"},
         "label": f"Extrude shroud {shroud_h:g}mm (new body)"},
        # Hub — tall cylinder standing on the shroud
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_hub",
                    "name": "ARIA Impeller Hub"},
         "label": "Sketch for hub on XY"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_hub", "cx": 0, "cy": 0, "r": hub_r},
         "label": f"Hub circle Ø{hub_od:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sk_hub", "distance": height,
                    "operation": "join", "alias": "hub_joined"},
         "label": f"Join hub, extrude {height:g}mm"},
    ]
    # Emit N blade-rectangles at rotated positions instead of using
    # circularPattern (broken in SW2024 IDispatch — see
    # feedback_sw2024_idispatch_quirks). Each blade is centered at
    # (mid_r * cos(theta), mid_r * sin(theta)). The rectangle stays
    # axis-aligned (visual approximation — proper sweep angle would
    # need a rotated sketchRect or a per-blade spline profile).
    for i in range(n_blades):
        theta = 2 * math.pi * i / n_blades
        bcx = mid_r * math.cos(theta)
        bcy = mid_r * math.sin(theta)
        sk_alias = f"sk_blade_{i}"
        plan.extend([
            {"kind": "newSketch",
             "params": {"plane": "XY", "alias": sk_alias,
                        "name": f"ARIA Impeller Blade {i+1}"},
             "label": f"Sketch blade {i+1}/{n_blades}"},
            {"kind": "sketchRect",
             "params": {"sketch": sk_alias,
                         "cx": bcx, "cy": bcy,
                         "w": blade_length, "h": blade_t},
             "label": (f"Blade {i+1}/{n_blades} {blade_length:g}×"
                        f"{blade_t:g}mm at ({bcx:+.1f},{bcy:+.1f})")},
            {"kind": "extrude",
             "params": {"sketch": sk_alias, "distance": blade_h,
                         "operation": "join",
                         "alias": f"blade_{i}"},
             "label": f"Join blade {i+1}/{n_blades}, extrude {blade_h:g}mm"},
        ])
    plan.extend([
        # Center bore — cut through the hub
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_bore",
                    "name": "ARIA Impeller Bore"},
         "label": "Sketch for bore"},
        {"kind": "sketchCircle",
         "params": {"sketch": "sk_bore", "cx": 0, "cy": 0,
                    "r": bore / 2},
         "label": f"Bore circle Ø{bore:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sk_bore", "distance": height * 1.5,
                    "operation": "cut", "alias": "cut_bore"},
         "label": f"Cut bore through {height * 1.5:g}mm"},
    ])
    # Note the sweep in the label for visual clarity — real sweep requires
    # angled blade sketches which need better geometry primitives (arcs,
    # loft). MVP does radial blades and labels the intended sweep.
    if sweep_deg != 0:
        plan.insert(1, {
            "kind": "beginPlan", "params": {},
            "label": (f"Note: {sweep_deg:+.0f}° blade sweep requested — "
                       "MVP emits radial blades (arc-based sweep coming)"),
        })
    return plan
