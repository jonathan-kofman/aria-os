"""Sheet-metal planner — emits ops that use Fusion's Sheet Metal
workspace commands (flange, bend) rather than generic extrude/cut.

Fusion's sheet metal tools auto-unfold to flat patterns, calculate
bend allowances from the rule library, and produce DXF exports ready
for laser cutting. That's significantly better than us emulating
sheet metal with extrude/cut primitives.
"""
from __future__ import annotations

from .iso_hardware import resolve_bolt_hole


def plan_simple_bracket(spec: dict, goal: str = "") -> list[dict]:
    """L-bracket / flat bracket planner.

    Uses standard extrude ops by default so it works in any Fusion
    workspace. If the prompt explicitly mentions sheet metal, forming,
    or bending, uses `sheetMetalBase` which requires the Sheet Metal
    workspace to be active.
    """
    w = float(spec.get("width_mm", 80.0))
    d = float(spec.get("depth_mm", 60.0))
    t = float(spec.get("wall_mm",
                          spec.get("thickness_mm", 5.0)))
    leg_h = float(spec.get("height_mm",
                             spec.get("leg_height_mm", 40.0)))
    n_holes = int(spec.get("n_bolts", spec.get("n_mounting_holes", 4)))
    # Resolve the hole size from the prompt: "M5 holes" → Ø5.5mm (ISO 273
    # close-fit clearance). That's what a machinist would drill.
    hole_info = resolve_bolt_hole(spec, goal)
    bolt_dia = hole_info["hole_dia_mm"]
    thread_label = (f" ({hole_info['thread']} {hole_info['fit']} clearance)"
                     if hole_info["source"] == "iso" else "")
    g = (goal or "").lower()
    use_sm = any(k in g for k in ("sheet metal", "sheet-metal",
                                     "formed", "folded", "bent"))
    # Split holes between base and vertical leg. Default: symmetric
    # pair on each face — that's how 4 holes on an L-bracket are
    # conventionally placed (2 for mounting to the floor surface,
    # 2 for attaching the load to the leg).
    n_base = max(2, n_holes // 2)
    n_leg  = n_holes - n_base
    # Edge offset = 2× hole Ø from the free edges, min 8mm
    edge_off = max(bolt_dia * 2.0, 8.0)

    plan: list[dict] = [
        {"kind": "beginPlan", "params": {}, "label": "Reset registry"},
        {"kind": "addParameter",
         "params": {"name": "sm_width", "value_mm": w,
                    "comment": "Sheet metal base width"},
         "label": f"User Parameter: sm_width = {w:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "sm_depth", "value_mm": d,
                    "comment": "Sheet metal base depth"},
         "label": f"User Parameter: sm_depth = {d:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "sm_thickness", "value_mm": t,
                    "comment": "Sheet gauge"},
         "label": f"User Parameter: sm_thickness = {t:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "sm_leg_h", "value_mm": leg_h,
                    "comment": "Bent leg height"},
         "label": f"User Parameter: sm_leg_h = {leg_h:g}mm"},
        # Base profile sketch
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_base",
                    "name": "ARIA Bracket Base"},
         "label": "Sketch on XY plane"},
        {"kind": "sketchRect",
         "params": {"sketch": "sk_base", "cx": 0, "cy": 0,
                    "w": w, "h": d},
         "label": f"Rectangle {w:g}×{d:g}mm"},
    ]
    # Body: sheet-metal flange op (needs SM workspace) OR standard
    # extrude (works anywhere)
    if use_sm:
        plan.append({"kind": "sheetMetalBase",
                     "params": {"sketch": "sk_base", "thickness_mm": t,
                                 "alias": "sm_body"},
                     "label": f"Sheet metal base flange, {t:g}mm gauge"})
    else:
        plan.append({"kind": "extrude",
                     "params": {"sketch": "sk_base", "distance": t,
                                 "operation": "new", "alias": "sm_body"},
                     "label": f"Extrude base plate {t:g}mm"})
    # Add the vertical leg if leg_h > 0 (makes it an actual L-bracket).
    # SW addin mirrors sketch-y on XZ plane (per MirrorYIfNeeded), so a
    # planner emitting cy=+leg_h/2 ends up with the leg at world Z=-leg_h..0
    # — below the base, no overlap, no boolean join. Compensate by
    # emitting cy=-leg_h/2 so the mirror lands the leg at Z=0..+leg_h
    # where it shares volume with the base and joins cleanly.
    if leg_h > 0:
        plan.extend([
            {"kind": "newSketch",
             "params": {"plane": "XZ", "alias": "sk_leg",
                        "name": "ARIA Bracket Leg"},
             "label": "Sketch on XZ plane for vertical leg"},
            {"kind": "sketchRect",
             "params": {"sketch": "sk_leg",
                        "cx": 0, "cy": -leg_h / 2,
                        "w": w, "h": leg_h},
             "label": f"Leg profile {w:g}×{leg_h:g}mm"},
            {"kind": "extrude",
             "params": {"sketch": "sk_leg", "distance": t,
                        "operation": "join", "alias": "leg_body"},
             "label": f"Join leg, extrude {t:g}mm"},
        ])
    # --- Base holes: N_base, along the free edge of the base, symmetric
    if n_base > 0:
        plan.append({"kind": "newSketch",
                     "params": {"plane": "XY", "alias": "sk_base_holes",
                                 "name": "ARIA Base Mounting Holes"},
                     "label": f"Sketch for {n_base} base mounting hole(s)"})
        y_off = d / 2 - edge_off
        # Distribute across width with edge offsets
        usable_w = w - 2 * edge_off
        for i in range(n_base):
            x_off = (-usable_w / 2 +
                      (i + 0.5) * usable_w / n_base if n_base > 0 else 0)
            plan.append({
                "kind": "sketchCircle",
                "params": {"sketch": "sk_base_holes",
                            "cx": x_off, "cy": y_off,
                            "r": bolt_dia / 2.0},
                "label": (f"Base hole {i+1}/{n_base}: "
                           f"Ø{bolt_dia:g}mm{thread_label} "
                           f"at ({x_off:.1f}, {y_off:.1f}mm)"),
            })
        plan.append({
            "kind": "extrude",
            "params": {"sketch": "sk_base_holes",
                        "distance": t * 1.5,
                        "operation": "cut", "alias": "cut_base_holes"},
            "label": f"Cut {n_base} base mounting hole(s)",
        })

    # --- Leg holes: N_leg, near the top edge of the vertical leg
    if n_leg > 0 and leg_h > 0:
        plan.append({"kind": "newSketch",
                     "params": {"plane": "XZ", "alias": "sk_leg_holes",
                                 "name": "ARIA Leg Mounting Holes"},
                     "label": f"Sketch for {n_leg} leg mounting hole(s)"})
        # Near the top of the leg so the mounted item hangs below.
        # World Z=+leg_h-edge_off, but the addin's XZ mirror flips
        # sketch-y → world-Z, so we emit -(leg_h-edge_off) here.
        z_off = -(leg_h - edge_off)
        usable_w = w - 2 * edge_off
        for i in range(n_leg):
            x_off = (-usable_w / 2 +
                      (i + 0.5) * usable_w / n_leg if n_leg > 0 else 0)
            plan.append({
                "kind": "sketchCircle",
                "params": {"sketch": "sk_leg_holes",
                            "cx": x_off, "cy": z_off,
                            "r": bolt_dia / 2.0},
                "label": (f"Leg hole {i+1}/{n_leg}: "
                           f"Ø{bolt_dia:g}mm{thread_label} "
                           f"at ({x_off:.1f}, z={abs(z_off):.1f}mm)"),
            })
        plan.append({
            "kind": "extrude",
            "params": {"sketch": "sk_leg_holes",
                        "distance": t * 1.5,
                        "operation": "cut", "alias": "cut_leg_holes"},
            "label": f"Cut {n_leg} leg mounting hole(s)",
        })

    # --- Inside corner fillet for stress relief (ISO engineering practice)
    # R = 0.5 × wall thickness is a reasonable default
    fillet_r = max(t * 0.5, 1.0)
    plan.append({
        "kind": "fillet",
        "params": {"body": "sm_body", "r": fillet_r, "alias": "inside_fillet"},
        "label": f"Fillet edges R{fillet_r:g}mm (stress relief + deburr)",
    })
    return plan
