"""Heat-sink planner — emits a base plate plus N parallel rectangular fins.

The SW addin doesn't have a `linearPattern` op, so we emit one sketch
per fin (cheap — sketches are fast). For an 8-fin heat sink that's
8 sketches + 8 extrude-joins on top of the base.
"""
from __future__ import annotations


def plan_heat_sink(spec: dict, goal: str = "") -> list[dict]:
    w = float(spec.get("width_mm", 80.0))           # body width (X)
    d = float(spec.get("depth_mm", 60.0))           # body depth (Y)
    base_t = float(spec.get("base_thickness_mm",
                             spec.get("thickness_mm", 10.0)))
    fin_h = float(spec.get("fin_height_mm",
                            spec.get("height_mm", 30.0)))
    fin_t = float(spec.get("fin_thickness_mm", 3.0))
    n_fins = int(spec.get("n_fins", spec.get("n_blades", 8)))
    if n_fins < 1:
        n_fins = 1
    # Fins span the depth (Y axis); array them along width (X axis)
    # with even spacing and an edge offset so the outer fins aren't
    # flush with the base side wall.
    edge_off = max(fin_t, 2.0)
    usable_w = max(w - 2 * edge_off, fin_t * n_fins * 1.2)
    if n_fins == 1:
        positions = [0.0]
    else:
        step = usable_w / (n_fins - 1)
        positions = [-usable_w / 2 + i * step for i in range(n_fins)]

    plan: list[dict] = [
        {"kind": "beginPlan", "params": {}, "label": "Reset registry"},
        {"kind": "addParameter",
         "params": {"name": "hs_width", "value_mm": w,
                    "comment": "Heat sink overall width"},
         "label": f"User Parameter: hs_width = {w:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "hs_depth", "value_mm": d,
                    "comment": "Heat sink overall depth"},
         "label": f"User Parameter: hs_depth = {d:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "hs_base_t", "value_mm": base_t,
                    "comment": "Base plate thickness"},
         "label": f"User Parameter: hs_base_t = {base_t:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "hs_fin_h", "value_mm": fin_h,
                    "comment": "Fin height above base"},
         "label": f"User Parameter: hs_fin_h = {fin_h:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "hs_fin_t", "value_mm": fin_t,
                    "comment": "Fin thickness"},
         "label": f"User Parameter: hs_fin_t = {fin_t:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "hs_n_fins", "value_mm": n_fins, "unit": "",
                    "comment": "Number of parallel fins"},
         "label": f"User Parameter: hs_n_fins = {n_fins}"},
        # Base plate
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_base",
                    "name": "ARIA Heat Sink Base"},
         "label": "Sketch base on XY"},
        {"kind": "sketchRect",
         "params": {"sketch": "sk_base", "cx": 0, "cy": 0,
                    "w": w, "h": d},
         "label": f"Base rect {w:g}×{d:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sk_base", "distance": base_t,
                    "operation": "new", "alias": "hs_body"},
         "label": f"Extrude base {base_t:g}mm (new body)"},
    ]
    # Fins: one sketch + extrude-join per fin, marching across the X axis
    for i, x_off in enumerate(positions):
        sk_alias = f"sk_fin_{i}"
        body_alias = f"fin_{i}"
        plan.append({
            "kind": "newSketch",
            "params": {"plane": "XY", "alias": sk_alias,
                       "name": f"ARIA Heat Sink Fin {i+1}"},
            "label": f"Sketch fin {i+1}/{n_fins} on XY"})
        plan.append({
            "kind": "sketchRect",
            "params": {"sketch": sk_alias, "cx": x_off, "cy": 0,
                       "w": fin_t, "h": d},
            "label": (f"Fin {i+1} rect {fin_t:g}×{d:g}mm "
                       f"at x={x_off:.1f}mm")})
        plan.append({
            "kind": "extrude",
            "params": {"sketch": sk_alias,
                       "distance": fin_h,
                       "start_offset": base_t,
                       "operation": "join",
                       "alias": body_alias},
            "label": (f"Extrude fin {i+1} ({fin_h:g}mm tall) "
                       f"on top of base")})
    # Fillet base edges for hand-safety + better thermal contact perimeter
    plan.append({
        "kind": "fillet",
        "params": {"body": "hs_body",
                   "r": max(min(base_t * 0.5, fin_t), 1.0),
                   "alias": "hs_edge_fillet"},
        "label": "Fillet base edges"})
    return plan
