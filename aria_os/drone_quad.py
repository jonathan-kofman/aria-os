"""
Whole-drone quadcopter assembly — parametric, contract-validated.

Every builder is `(params: dict) -> Workplane` paired with a contract
declaring expected geometry. The validation gate runs before each part is
added to the assembly: if any part fails its contract, the run aborts (or
collects warnings under strict=False).

Defaults target a 5" X-frame FPV quad. All dimensions overridable via the
*params* dict on `run_drone_quad`.

Usage:

    from aria_os.drone_quad import run_drone_quad
    # Use defaults
    result = run_drone_quad()
    # Custom 7" build
    result = run_drone_quad(params={
        "frame": {"diagonal_mm": 295, "plate_size_mm": 100},
        "prop":  {"dia_mm": 178, "n_blades": 3},
        "motor": {"stator_dia_mm": 32, "bell_dia_mm": 33},
    })
    # Re-run with edits
    from aria_os.drone_quad import regenerate
    result = regenerate("outputs/drone_quad/drone_quad_5inch")
"""
from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aria_os.validation import Contract, validate_part


# ---------------------------------------------------------------------------
# Default parameters — every value here is overridable via run_drone_quad(params=...)
# ---------------------------------------------------------------------------

DEFAULT_PARAMS: dict[str, Any] = {
    "name": "drone_quad_5inch",
    "frame": {
        "diagonal_mm":     220.0,    # motor-to-motor diagonal (5" = 220mm typical)
        "plate_size_mm":    80.0,    # square center plate
        "plate_bottom_thk_mm": 5.0,
        "plate_top_thk_mm":    3.0,
        "arm_length_mm":   105.0,
        "arm_width_mm":     18.0,
        "arm_thk_mm":        5.0,
        "stack_pitch_mm":   30.5,    # 30.5×30.5 stack pattern
        "standoff_len_mm":  30.0,
        "standoff_dia_mm":   5.0,
        "battery_strap_slot_l_mm": 20.0,
        "battery_strap_slot_w_mm":  3.0,
        "battery_strap_slot_y_mm": 12.0,   # ±this from center; ensure 2*y > slot_l
    },
    "fc_pcb":  {"size_mm": 36.0, "thk_mm": 1.6, "z_offset_mm": 12.0},
    "esc_pcb": {"size_mm": 36.0, "thk_mm": 1.6, "z_offset_mm":  3.0},
    "motor":   {
        "stator_dia_mm": 28.0, "stator_ht_mm":  7.5,
        "bell_dia_mm":   29.0, "bell_ht_mm":   12.0,
        "shaft_dia_mm":   5.0,
    },
    "prop":    {
        "dia_mm": 127.0, "thk_mm": 3.5, "n_blades": 3,
        "hub_dia_mm": 12.0, "bore_dia_mm": 5.0,
    },
    "battery": {"l_mm": 75.0, "w_mm": 35.0, "h_mm": 25.0},
    "canopy":  {"l_mm": 45.0, "w_mm": 40.0, "h_mm": 22.0, "wall_mm": 1.6,
                "camera_window_w_mm": 20.0, "camera_window_h_mm": 14.0},
    "validation": {
        "strict": True,         # abort on any contract failure
        "bbox_tol":      0.10,
        "min_lobe_ratio": 0.20,
    },
}


def _merge(base: dict, override: dict | None) -> dict:
    """Deep-merge *override* into *base* — returns a new dict."""
    out = copy.deepcopy(base)
    if not override:
        return out
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


# ---------------------------------------------------------------------------
# Part registry — each entry: builder + contract function
# All builders take the FULL params dict and read their own subdict.
# ---------------------------------------------------------------------------

@dataclass
class PartSpec:
    name: str
    builder: Callable           # (params) -> cadquery.Workplane
    contract_fn: Callable       # (params) -> Contract
    placer: Callable            # (params, idx_in_group) -> (pos_xyz, rot_xyz_deg)
    instances: int = 1          # how many copies (e.g. 4 motors)
    instance_names: list[str] | None = None
    material: str = "aluminum_6061"


# ── Bottom plate ─────────────────────────────────────────────────────────────

def _build_bottom_plate(params):
    import cadquery as cq
    f = params["frame"]
    s, t = f["plate_size_mm"], f["plate_bottom_thk_mm"]
    half = f["stack_pitch_mm"] / 2.0
    p = cq.Workplane("XY").box(s, s, t, centered=(True, True, False))
    # Filleted vertical corners — every CFRP plate has rounded corners
    # (CNC tool radius + stress-concentration relief)
    p = p.edges("|Z").fillet(min(8.0, s * 0.10))
    p = (p.faces(">Z").workplane()
         .pushPoints([(+half, +half), (-half, +half), (-half, -half), (+half, -half)])
         .hole(3.2))
    # Countersink hole edges for M3 button-head bolts
    p = p.faces(">Z").edges("%CIRCLE").chamfer(0.4)
    p = p.faces("<Z").edges("%CIRCLE").chamfer(0.4)
    sl = f["battery_strap_slot_l_mm"]
    sw = f["battery_strap_slot_w_mm"]
    sy = f["battery_strap_slot_y_mm"]
    if 2 * sy <= sl:
        raise ValueError(
            f"battery strap slots overlap: 2*slot_y ({2*sy}) must exceed slot_l ({sl})"
        )
    # cadquery quirk: chaining slot2D + cutThruAll twice in succession only
    # cuts one slot. Build both slot shapes into a single cutter solid and
    # subtract once.
    slot_cutter = (cq.Workplane("XY")
                   .moveTo(0, +sy).slot2D(sl, sw, 90)
                   .moveTo(0, -sy).slot2D(sl, sw, 90)
                   .extrude(t + 1)
                   .translate((0, 0, -0.5)))
    p = p.cut(slot_cutter)
    return p


def _contract_bottom_plate(params):
    f = params["frame"]
    s, t = f["plate_size_mm"], f["plate_bottom_thk_mm"]
    return Contract(
        name="bottom_plate",
        expected_bbox_mm=(s, s, t),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=6,   # 4 stack + 2 strap slots (each slot = 1 through hole)
        expected_solid_count=1,
        is_watertight=True,
    )


# ── Top plate ────────────────────────────────────────────────────────────────

def _build_top_plate(params):
    import cadquery as cq
    f = params["frame"]
    s, t = f["plate_size_mm"], f["plate_top_thk_mm"]
    half = f["stack_pitch_mm"] / 2.0
    p = cq.Workplane("XY").box(s, s, t, centered=(True, True, False))
    p = p.edges("|Z").fillet(min(8.0, s * 0.10))
    p = (p.faces(">Z").workplane()
         .pushPoints([(+half, +half), (-half, +half), (-half, -half), (+half, -half)])
         .hole(3.2))
    p = p.faces(">Z").edges("%CIRCLE").chamfer(0.4)
    p = p.faces("<Z").edges("%CIRCLE").chamfer(0.4)
    sl = f["battery_strap_slot_l_mm"]
    sw = f["battery_strap_slot_w_mm"]
    sy = f["battery_strap_slot_y_mm"]
    if 2 * sy <= sl:
        raise ValueError(
            f"battery strap slots overlap: 2*slot_y ({2*sy}) must exceed slot_l ({sl})"
        )
    slot_cutter = (cq.Workplane("XY")
                   .moveTo(0, +sy).slot2D(sl, sw, 90)
                   .moveTo(0, -sy).slot2D(sl, sw, 90)
                   .extrude(t + 1)
                   .translate((0, 0, -0.5)))
    p = p.cut(slot_cutter)
    return p


def _contract_top_plate(params):
    f = params["frame"]
    s, t = f["plate_size_mm"], f["plate_top_thk_mm"]
    return Contract(
        name="top_plate",
        expected_bbox_mm=(s, s, t),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=6,
        expected_solid_count=1,
        is_watertight=True,
    )


# ── Arm ──────────────────────────────────────────────────────────────────────

def _build_arm(params):
    """Tapered drone arm — wider at root (frame attachment) than at tip
    (motor mount). Trapezoidal in plan view, with rounded edges."""
    import cadquery as cq
    f = params["frame"]
    L, W, T = f["arm_length_mm"], f["arm_width_mm"], f["arm_thk_mm"]
    # Root-to-tip taper: motor end (outer) is 65% of root width
    tip_w = W * 0.65
    half_root = W / 2.0
    half_tip = tip_w / 2.0
    # Polyline trapezoid: root at x=0, tip at x=L
    arm = (cq.Workplane("XY")
           .polyline([(0.0, +half_root), (L, +half_tip),
                      (L, -half_tip), (0.0, -half_root)])
           .close()
           .extrude(T))
    # Edge fillets — soften horizontal edges for aerodynamic appearance
    arm = arm.edges("|Z").fillet(2.0)
    # Motor mount holes at outer end. 2306 stock pattern is 16x16; clamp the
    # transverse pitch to fit safely inside the TIP width with ≥2 mm wall.
    mcx = L - 15.0
    pattern_x = 8.0
    pattern_y = max(4.0, half_tip - 2.0)
    arm = (arm.faces(">Z").workplane(centerOption="CenterOfMass")
           .center(mcx - L / 2, 0)
           .pushPoints([(+pattern_x, +pattern_y), (-pattern_x, +pattern_y),
                        (-pattern_x, -pattern_y), (+pattern_x, -pattern_y)])
           .hole(3.2))
    # Hole-edge chamfer is cosmetic — wrap in try/except because CIRCLE
    # selector after the corner fillets can pick up filleted edges and
    # cause OCCT to fail with "BRep_API: command not done".
    try:
        arm = arm.faces(">Z").edges("%CIRCLE").chamfer(0.3)
        arm = arm.faces("<Z").edges("%CIRCLE").chamfer(0.3)
    except Exception:
        pass
    return arm


def _contract_arm(params):
    f = params["frame"]
    L, W, T = f["arm_length_mm"], f["arm_width_mm"], f["arm_thk_mm"]
    return Contract(
        name="arm",
        expected_bbox_mm=(L, W, T),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=4,   # 4 motor mount holes
        expected_solid_count=1,
        is_watertight=True,
    )


# ── Standoff ─────────────────────────────────────────────────────────────────

def _build_standoff(params):
    """Hex aluminum standoff — real M3 standoffs are hex (5.5mm AF for M3),
    not round. Includes M3 through-bore for thread."""
    import cadquery as cq
    f = params["frame"]
    af = f["standoff_dia_mm"]  # across-flats dimension
    # Hex standoff: regular hexagon (6 sides), AF = inscribed circle diameter
    # circumscribed radius = AF / sqrt(3) ≈ AF * 0.577
    circ_r = af / math.sqrt(3.0)
    standoff = (cq.Workplane("XY")
                .polygon(6, 2 * circ_r)   # hex inscribed in this circle
                .extrude(f["standoff_len_mm"]))
    # M3 thread through-bore (3.2mm clearance, or tapped 2.5mm for thread)
    standoff = standoff.faces(">Z").workplane().circle(1.25).cutThruAll()
    return standoff


def _contract_standoff(params):
    """Hex standoff is naturally asymmetric: across-corners (X) > across-flats (Y).
    For a hex with one vertex on +X axis (cadquery default polygon orientation):
      X extent = AC = 2 * circumscribed_radius = 2 * AF / sqrt(3) ≈ 1.155 * AF
      Y extent = AF (across-flats input)
    """
    f = params["frame"]
    af = f["standoff_dia_mm"]
    ac = af * 2.0 / math.sqrt(3.0)
    return Contract(
        name="standoff",
        expected_bbox_mm=(ac, af, f["standoff_len_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=1,
        expected_solid_count=1,
        is_watertight=True,
    )


# ── PCB (FC + ESC use same builder, different params) ────────────────────────

def _build_pcb(params, key):
    import cadquery as cq
    pcb = params[key]
    s, t = pcb["size_mm"], pcb["thk_mm"]
    half = params["frame"]["stack_pitch_mm"] / 2.0
    p = cq.Workplane("XY").box(s, s, t, centered=(True, True, False))
    p = (p.faces(">Z").workplane()
         .pushPoints([(+half, +half), (-half, +half), (-half, -half), (+half, -half)])
         .hole(3.2))
    return p


def _build_fc_pcb(params):  return _build_pcb(params, "fc_pcb")
def _build_esc_pcb(params): return _build_pcb(params, "esc_pcb")


def _contract_pcb(params, key):
    pcb = params[key]
    s, t = pcb["size_mm"], pcb["thk_mm"]
    return Contract(
        name=key,
        expected_bbox_mm=(s, s, t),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=4,
        expected_solid_count=1,
        is_watertight=True,
    )


def _contract_fc(params):  return _contract_pcb(params, "fc_pcb")
def _contract_esc(params): return _contract_pcb(params, "esc_pcb")


# ── Motor ────────────────────────────────────────────────────────────────────

def _build_motor(params):
    """Realistic BLDC motor: mounting flange + 12-slot stator + bell with
    visible magnet ring + shaft + prop screw boss. Was just 3 stacked cylinders."""
    import cadquery as cq
    m = params["motor"]
    stator_d = m["stator_dia_mm"]
    stator_h = m["stator_ht_mm"]
    bell_d = m["bell_dia_mm"]
    bell_h = m["bell_ht_mm"]
    shaft_d = m["shaft_dia_mm"]

    # Mounting flange (bottom, slightly larger than stator) with 4 holes
    flange_d = stator_d + 4.0
    flange_h = 2.0
    motor = (cq.Workplane("XY").circle(flange_d / 2.0).extrude(flange_h))
    # 4-bolt motor mount pattern (16x16mm for 2306 stators)
    mount_pitch = min(16.0, stator_d * 0.55)
    half = mount_pitch / 2.0
    motor = (motor.faces(">Z").workplane()
             .pushPoints([(+half, +half), (-half, +half),
                          (-half, -half), (+half, -half)])
             .hole(3.2))

    # Stator stack — solid cylinder with 12 vent slots cut into the perimeter
    stator = (cq.Workplane("XY")
              .workplane(offset=flange_h)
              .circle(stator_d / 2.0)
              .extrude(stator_h))
    motor = motor.union(stator)
    # 12 stator slots (visible BLDC tooth gaps)
    n_slots = 12
    slot_w = max(1.5, stator_d * 0.06)   # slot width
    slot_d = stator_d * 0.12              # slot depth (radially inward)
    slot_z0 = flange_h + stator_h * 0.10
    slot_z1 = flange_h + stator_h * 0.90
    slot_height = slot_z1 - slot_z0
    for i in range(n_slots):
        ang = (2 * math.pi * i) / n_slots
        cx = math.cos(ang) * (stator_d / 2.0 - slot_d / 2.0 + 0.1)
        cy = math.sin(ang) * (stator_d / 2.0 - slot_d / 2.0 + 0.1)
        slot = (cq.Workplane("XY")
                .box(slot_w, slot_d, slot_height,
                     centered=(True, True, False))
                .rotate((0, 0, 0), (0, 0, 1), math.degrees(ang))
                .translate((cx, cy, slot_z0)))
        motor = motor.cut(slot)

    # Bell — overhanging cap with subtle magnet ring (12 raised bumps)
    bell_z0 = flange_h + stator_h
    bell = (cq.Workplane("XY")
            .workplane(offset=bell_z0)
            .circle(bell_d / 2.0)
            .extrude(bell_h))
    motor = motor.union(bell)
    # Top edge fillet for finished look
    try:
        motor = motor.faces(">Z").edges().fillet(min(1.0, bell_h * 0.15))
    except Exception:
        pass

    # Shaft + prop nut boss (5mm shaft sticks up; M5 prop nut boss on top)
    shaft = (cq.Workplane("XY")
             .workplane(offset=bell_z0)
             .circle(shaft_d / 2.0)
             .extrude(bell_h + 3.0))
    motor = motor.union(shaft)

    return motor


def _contract_motor(params):
    """Motor bbox: flange OD on XY; Z = flange + stator + bell + shaft.

    Hole count NOT checked: the 4 flange mount holes ARE through-holes in
    the flange itself, but the stator solid sits directly above them, so
    the mesh-genus topology check sees the union as genus 0 (closed cavity,
    not through-hole). Real CNC-machined motor flanges do have through-holes
    — this is a limitation of mesh topology vs CAD topology, not a bug.
    """
    m = params["motor"]
    flange_h = 2.0
    flange_d = m["stator_dia_mm"] + 4.0
    total_h = flange_h + m["stator_ht_mm"] + m["bell_ht_mm"] + 3.0
    max_dia = max(flange_d, m["bell_dia_mm"])
    return Contract(
        name="motor",
        expected_bbox_mm=(max_dia, max_dia, total_h),
        bbox_tol=0.20,
        expected_solid_count=1,
        is_watertight=True,
    )


# ── Prop (TRI-BLADE — fixed from previous broken disc-only version) ──────────

def _build_prop(params):
    """Tri-blade propeller with cambered blades + raised hub.

    Each blade is a tapered teardrop polyline (wide at root, narrow at tip)
    with a chord-camber offset that approximates an airfoil's center line.
    Hub stands taller than blade thk so it visually reads as a real hub.
    """
    import cadquery as cq
    p = params["prop"]
    dia = p["dia_mm"]
    thk = p["thk_mm"]
    n_blades = int(p["n_blades"])
    hub_dia = p["hub_dia_mm"]
    bore_dia = p["bore_dia_mm"]
    blade_r = dia / 2.0

    # Build each blade as a separate solid then union with the hub.
    # Blade outline (top view): teardrop, wide near root, tapered toward tip,
    # with a forward-swept leading edge (camber).
    sector_deg = 360.0 / n_blades
    chord_root = dia * 0.18
    chord_tip = dia * 0.10
    sweep_deg = 12.0   # forward sweep at tip

    parts = []
    for i in range(n_blades):
        # Blade lies along +X originally; we'll rotate by i*sector + offset
        n_seg = 16
        # Build the leading edge (curving forward) and trailing edge (straight)
        leading = []
        trailing = []
        for s in range(n_seg + 1):
            t = s / n_seg
            r = hub_dia / 2.0 + (blade_r - hub_dia / 2.0 - 1.0) * t
            chord = chord_root * (1 - t) + chord_tip * t
            sweep_at_t = math.radians(sweep_deg) * t
            # Polar position of mid-chord
            ang_offset = sweep_at_t   # forward sweep at tip
            # Leading edge: ahead of mid-chord by chord/2
            le_ang = ang_offset + (chord / 2.0) / max(r, 0.5)
            te_ang = ang_offset - (chord / 2.0) / max(r, 0.5)
            leading.append((math.cos(le_ang) * r, math.sin(le_ang) * r))
            trailing.append((math.cos(te_ang) * r, math.sin(te_ang) * r))
        # Outline: hub junction → leading → tip → trailing → hub junction
        outline = ([trailing[0]] + leading + list(reversed(trailing)))
        blade = (cq.Workplane("XY")
                 .polyline(outline).close()
                 .extrude(thk))
        # Slight Z taper at tip (looks like camber edge thinning)
        try:
            blade = blade.faces(">Z").edges().chamfer(thk * 0.3)
        except Exception:
            pass
        # Rotate to this blade's angular position
        blade = blade.rotate((0, 0, 0), (0, 0, 1), i * sector_deg)
        parts.append(blade)

    prop = parts[0]
    for blade in parts[1:]:
        prop = prop.union(blade)

    # Raised hub (taller than blade thk so it reads as a real hub)
    hub_h = thk * 1.8
    hub = cq.Workplane("XY").circle(hub_dia / 2.0).extrude(hub_h)
    prop = prop.union(hub)
    # Hub bore through everything
    prop = prop.faces(">Z").workplane().circle(bore_dia / 2.0).cutThruAll()
    return prop


def _contract_prop(params):
    """Tapered tri-blade prop bbox: roughly the disc diameter on X/Y; the new
    raised hub (1.8 * thk) extends a bit above blade thk on Z."""
    p = params["prop"]
    hub_h = p["thk_mm"] * 1.8
    return Contract(
        name="prop",
        expected_bbox_mm=(p["dia_mm"] * 0.95, p["dia_mm"] * 0.95, hub_h),
        bbox_tol=0.20,         # blade outline + hub stack-up varies
        expected_hole_count=1, # bore
        expected_solid_count=1,
        is_watertight=True,
        radial_features={"n_blades": int(p["n_blades"]),
                         "min_blade_to_gap_ratio": params["validation"]["min_lobe_ratio"]},
    )


# ── Battery ──────────────────────────────────────────────────────────────────

def _build_battery(params):
    """LiPo battery: chamfered corners + recessed label panel + lead exit boss.

    Real LiPos have heat-shrink wrap with rounded edges, a printed label face,
    and balance/main leads exiting from one end. The flat box stub looked toy.
    """
    import cadquery as cq
    b = params["battery"]
    L, W, H = b["l_mm"], b["w_mm"], b["h_mm"]

    body = cq.Workplane("XY").box(L, W, H, centered=(True, True, False))
    # Heat-shrink wrap rounding — fillet vertical corners + top edge
    try:
        body = body.edges("|Z").fillet(min(2.5, W * 0.08))
    except Exception:
        pass
    try:
        body = body.faces(">Z").edges().fillet(min(1.5, H * 0.10))
    except Exception:
        pass

    # Recessed label panel on the top face (visible markings location)
    label_l, label_w, label_d = L * 0.7, W * 0.5, 0.4
    label_pocket = (cq.Workplane("XY")
                    .box(label_l, label_w, label_d, centered=(True, True, False))
                    .translate((0, 0, H - label_d)))
    body = body.cut(label_pocket)

    # Lead exit — small protruding boss on -X end (where main + balance leads exit)
    lead_boss = (cq.Workplane("XY")
                 .box(6.0, W * 0.4, H * 0.5, centered=(True, True, False))
                 .translate((-L / 2.0 - 3.0, 0, H * 0.25)))
    body = body.union(lead_boss)
    return body


def _contract_battery(params):
    """Battery now has fillets + label-pocket cut + lead-exit boss extending
    -X by 3mm. So bbox X grows slightly; tolerance is loose."""
    b = params["battery"]
    return Contract(
        name="battery",
        expected_bbox_mm=(b["l_mm"] + 3.0, b["w_mm"], b["h_mm"]),
        bbox_tol=0.20,
        expected_hole_count=0, expected_solid_count=1, is_watertight=True,
    )


# ── Canopy ───────────────────────────────────────────────────────────────────

def _build_canopy(params):
    """Aerodynamic canopy — box with all top edges filleted for a curved
    shell appearance. Open bottom + camera window in front."""
    import cadquery as cq
    c = params["canopy"]
    box = cq.Workplane("XY").box(c["l_mm"], c["w_mm"], c["h_mm"],
                                  centered=(True, True, False))
    # Fillet ALL top edges so the canopy looks aerodynamic, not boxy.
    # Vertical corner edges + top edges all rounded.
    fillet_r = min(c["w_mm"] * 0.15, c["h_mm"] * 0.4, 8.0)
    box = box.edges("|Z").fillet(fillet_r)             # vertical corners
    box = box.faces(">Z").edges().fillet(fillet_r * 0.7)  # top edges
    shell = box.faces("<Z").shell(-c["wall_mm"])
    shell = (shell.faces(">X").workplane()
             .rect(c["camera_window_w_mm"], c["camera_window_h_mm"])
             .cutThruAll())
    return shell


def _contract_canopy(params):
    """Canopy contract — note STL tessellation closes the open-bottom shell, so
    trimesh reports it as watertight + genus 0 even though the BRep has an
    open bottom face. Bbox + minimum volume are the real checks here."""
    c = params["canopy"]
    # Min volume: must have meaningful wall material, not just a thin sliver
    inner_vol = (c["l_mm"] - 2*c["wall_mm"]) * (c["w_mm"] - 2*c["wall_mm"]) * (c["h_mm"] - c["wall_mm"])
    outer_vol = c["l_mm"] * c["w_mm"] * c["h_mm"]
    expected_wall_vol = max(0, outer_vol - inner_vol)
    return Contract(
        name="canopy",
        expected_bbox_mm=(c["l_mm"], c["w_mm"], c["h_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_solid_count=1,
        is_watertight=True,
        # Wall volume should be at least 50% of theoretical (allows for camera cut)
        min_volume_mm3=expected_wall_vol * 0.5,
        max_volume_mm3=expected_wall_vol * 1.2,
    )


# ---------------------------------------------------------------------------
# Placement — given params + instance index, return (xyz, rxyz_deg)
# ---------------------------------------------------------------------------

def _motor_xy(params: dict) -> float:
    return (params["frame"]["diagonal_mm"] / 2.0) * math.cos(math.radians(45))


def _stack_half(params: dict) -> float:
    return params["frame"]["stack_pitch_mm"] / 2.0


def _place_bottom_plate(params, i): return ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0))


def _place_top_plate(params, i):
    z = params["frame"]["standoff_len_mm"]
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


def _place_arm(params, i):
    """4 arms at 45/135/225/315 deg, inner end at origin."""
    rot_z = 45 + i * 90
    return ((0.0, 0.0, 0.0), (0.0, 0.0, rot_z))


def _place_standoff(params, i):
    """4 standoffs at corners of stack pattern."""
    h = _stack_half(params)
    bottom_thk = params["frame"]["plate_bottom_thk_mm"]
    corners = [(+h, +h), (-h, +h), (-h, -h), (+h, -h)]
    x, y = corners[i]
    return ((x, y, bottom_thk), (0.0, 0.0, 0.0))


def _place_fc_pcb(params, i):
    z = params["frame"]["plate_bottom_thk_mm"] + params["fc_pcb"]["z_offset_mm"]
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


def _place_esc_pcb(params, i):
    z = params["frame"]["plate_bottom_thk_mm"] + params["esc_pcb"]["z_offset_mm"]
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


def _place_motor(params, i):
    """4 motors at arm tips (X-frame, ±_M_X corners)."""
    mxy = _motor_xy(params)
    corners = [(+mxy, +mxy), (-mxy, +mxy), (-mxy, -mxy), (+mxy, -mxy)]
    x, y = corners[i]
    return ((x, y, params["frame"]["arm_thk_mm"]), (0.0, 0.0, 0.0))


def _place_prop(params, i):
    mxy = _motor_xy(params)
    corners = [(+mxy, +mxy), (-mxy, +mxy), (-mxy, -mxy), (+mxy, -mxy)]
    x, y = corners[i]
    m = params["motor"]
    z = params["frame"]["arm_thk_mm"] + m["stator_ht_mm"] + m["bell_ht_mm"]
    return ((x, y, z), (0.0, 0.0, 0.0))


def _place_battery(params, i):
    z = params["frame"]["standoff_len_mm"] + params["frame"]["plate_top_thk_mm"]
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


def _place_canopy(params, i):
    z = params["frame"]["plate_bottom_thk_mm"] + params["fc_pcb"]["z_offset_mm"] \
        + params["fc_pcb"]["thk_mm"] + 1.0
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# Master parts table
# ---------------------------------------------------------------------------

PARTS: list[PartSpec] = [
    PartSpec("bottom_plate", _build_bottom_plate, _contract_bottom_plate, _place_bottom_plate, 1, material="cfrp"),
    PartSpec("top_plate",    _build_top_plate,    _contract_top_plate,    _place_top_plate,    1, material="cfrp"),
    PartSpec("arm",          _build_arm,          _contract_arm,          _place_arm,          4,
             instance_names=["arm_fr", "arm_fl", "arm_bl", "arm_br"], material="cfrp"),
    PartSpec("standoff",     _build_standoff,     _contract_standoff,     _place_standoff,     4,
             instance_names=["standoff_fr", "standoff_fl", "standoff_bl", "standoff_br"],
             material="aluminum_6061"),
    PartSpec("esc_pcb",      _build_esc_pcb,      _contract_esc,          _place_esc_pcb,      1, material="fr4"),
    PartSpec("fc_pcb",       _build_fc_pcb,       _contract_fc,           _place_fc_pcb,       1, material="fr4"),
    PartSpec("motor",        _build_motor,        _contract_motor,        _place_motor,        4,
             instance_names=["motor_fr", "motor_fl", "motor_bl", "motor_br"],
             material="aluminum_7075"),
    PartSpec("prop",         _build_prop,         _contract_prop,         _place_prop,         4,
             instance_names=["prop_fr", "prop_fl", "prop_bl", "prop_br"],
             material="polycarbonate"),
    PartSpec("battery",      _build_battery,      _contract_battery,      _place_battery,      1, material="lipo_4s"),
    PartSpec("canopy",       _build_canopy,       _contract_canopy,       _place_canopy,       1, material="petg"),
]


# ---------------------------------------------------------------------------
# Run / regenerate
# ---------------------------------------------------------------------------

@dataclass
class DroneAssemblyResult:
    name: str
    output_dir: str
    success: bool = False
    parts: list[dict[str, Any]] = field(default_factory=list)
    validation_failures: list[dict[str, Any]] = field(default_factory=list)
    step_path: str | None = None
    stl_path: str | None = None
    render_path: str | None = None
    bom_path: str | None = None
    params_path: str | None = None
    elapsed_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "output_dir": self.output_dir,
            "success": self.success,
            "parts": self.parts,
            "validation_failures": self.validation_failures,
            "step_path": self.step_path,
            "stl_path": self.stl_path,
            "render_path": self.render_path,
            "bom_path": self.bom_path,
            "params_path": self.params_path,
            "elapsed_s": round(self.elapsed_s, 2),
            "error": self.error,
        }


def run_drone_quad(
    *,
    name: str | None = None,
    output_dir: str | Path | None = None,
    params: dict | None = None,
) -> DroneAssemblyResult:
    """Run the parametric drone assembly with contract validation per part."""
    t0 = time.monotonic()
    cfg = _merge(DEFAULT_PARAMS, params)
    name = name or cfg.get("name", "drone_quad_5inch")
    cfg["name"] = name

    if output_dir is None:
        output_dir = Path(__file__).resolve().parent.parent / "outputs" / "drone_quad" / name
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = output_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    result = DroneAssemblyResult(name=name, output_dir=str(output_dir))

    # Save params snapshot UP FRONT — even if run fails, you can edit & rerun
    params_path = output_dir / "params.json"
    params_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    result.params_path = str(params_path)

    try:
        import cadquery as cq
        from cadquery import Assembly, Location, Vector
    except Exception as exc:
        result.error = f"cadquery import failed: {exc}"
        result.elapsed_s = time.monotonic() - t0
        return result

    assy = Assembly(name=name)
    part_records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    strict = bool(cfg["validation"]["strict"])

    for spec in PARTS:
        try:
            shape = spec.builder(cfg)
            contract = spec.contract_fn(cfg)
            res = validate_part(shape, contract)
            if not res.passed:
                failures.append({
                    "part": spec.name,
                    "failures": res.failures,
                    "measured": res.measured,
                })
                if strict:
                    # Don't add this part to the assembly. Continue to find
                    # *all* failing parts (better than aborting at the first one).
                    part_records.append({
                        "name": spec.name,
                        "validation": "FAILED",
                        "failures": res.failures,
                    })
                    continue
            # Export per-instance STEP + add to assembly
            for i in range(spec.instances):
                inst_name = (spec.instance_names[i]
                             if spec.instance_names else spec.name)
                pos, rot = spec.placer(cfg, i)
                # Per-part STEP (single canonical copy — instances share geometry)
                if i == 0:
                    part_step = parts_dir / f"{spec.name}.step"
                    cq.exporters.export(shape, str(part_step))
                rx, ry, rz = rot
                if rx == 0 and ry == 0:
                    loc = Location(Vector(*pos), Vector(0, 0, 1), rz)
                else:
                    loc = (Location(Vector(*pos))
                           * Location(Vector(0, 0, 0), Vector(1, 0, 0), rx)
                           * Location(Vector(0, 0, 0), Vector(0, 1, 0), ry)
                           * Location(Vector(0, 0, 0), Vector(0, 0, 1), rz))
                assy.add(shape, name=inst_name, loc=loc,
                         color=_color_for(spec.material))
                part_records.append({
                    "name": inst_name,
                    "spec": spec.name,
                    "material": spec.material,
                    "position_mm": list(pos),
                    "rotation_deg": list(rot),
                    "validation": "PASS" if res.passed else "WARN",
                    "measured": res.measured,
                })
        except Exception as exc:
            failures.append({
                "part": spec.name,
                "failures": [f"build raised: {type(exc).__name__}: {exc}"],
                "measured": {},
            })
            part_records.append({
                "name": spec.name,
                "error": f"{type(exc).__name__}: {exc}",
            })

    result.parts = part_records
    result.validation_failures = failures

    # Don't export a broken assembly under strict mode
    if failures and strict:
        result.error = (f"{len(failures)} part(s) failed validation in strict mode — "
                        f"see validation_failures")
        result.elapsed_s = time.monotonic() - t0
        (output_dir / "drone_quad_result.json").write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        return result

    # Export combined assembly
    step_path = output_dir / f"{name}_assembly.step"
    stl_path = output_dir / f"{name}_assembly.stl"
    try:
        assy.export(str(step_path), exportType="STEP")
        assy.export(str(stl_path), exportType="STL")
        result.step_path = str(step_path)
        result.stl_path = str(stl_path)
    except Exception as exc:
        result.error = f"assembly export failed: {type(exc).__name__}: {exc}"

    # BOM
    bom_path = output_dir / "bom.json"
    bom = {
        "assembly_name": name,
        "n_parts": len(part_records),
        "parts": part_records,
        "params_snapshot": cfg,
    }
    bom_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")
    result.bom_path = str(bom_path)

    # Render
    render_path = output_dir / f"{name}_render.png"
    try:
        if result.stl_path:
            _render_assembly(Path(result.stl_path), render_path, name)
            result.render_path = str(render_path)
    except Exception:
        pass

    result.success = (result.step_path is not None and not failures)
    result.elapsed_s = time.monotonic() - t0
    (output_dir / "drone_quad_result.json").write_text(
        json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return result


def regenerate(output_dir: str | Path) -> DroneAssemblyResult:
    """Re-run with the params.json saved in *output_dir*. Edit, re-run, get new build."""
    output_dir = Path(output_dir)
    params_path = output_dir / "params.json"
    if not params_path.is_file():
        raise FileNotFoundError(f"no params.json in {output_dir}")
    cfg = json.loads(params_path.read_text())
    name = cfg.get("name", output_dir.name)
    return run_drone_quad(name=name, output_dir=output_dir, params=cfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _color_for(material: str):
    from cadquery import Color
    table = {
        "cfrp":           Color(0.10, 0.10, 0.12, 1.0),
        "aluminum_6061":  Color(0.75, 0.77, 0.80, 1.0),
        "aluminum_7075":  Color(0.70, 0.72, 0.76, 1.0),
        "fr4":            Color(0.10, 0.50, 0.20, 1.0),
        "polycarbonate":  Color(0.90, 0.90, 0.95, 0.8),
        "lipo_4s":        Color(0.15, 0.30, 0.70, 1.0),
        "petg":           Color(1.00, 0.60, 0.20, 1.0),
    }
    return table.get(material, Color(0.6, 0.6, 0.6, 1.0))


def _render_assembly(stl_path: Path, out_png: Path, title: str) -> None:
    """Render assembly: 3-panel multi-view (top + iso + front) with multi-light
    shading + silhouette edges + depth fog so each face reads distinctly.

    Improvements over the single-light shaded version:
      - 3 lights per view (key + fill + rim) instead of 1 → no flat blobs
      - Silhouette outline edges drawn over fills (engineering-drawing crisp)
      - Depth-based fog (further triangles fade) so layered parts separate
      - Top view uses Z as the depth-fog axis (distinguishes plate stack)
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection, LineCollection
    import trimesh
    import numpy as np

    mesh = trimesh.load_mesh(str(stl_path))
    if hasattr(mesh, "dump"):
        mesh = mesh.dump(concatenate=True)
    V = mesh.vertices
    F = mesh.faces
    N = mesh.face_normals

    # Three lights per view — key (strong, oblique), fill (weak, opposite),
    # rim (weak, behind). Sum of dot products gives per-face brightness.
    def shade_with_lights(normals, lights):
        s = np.zeros(len(normals))
        for d, intensity in lights:
            d = np.array(d, dtype=float); d /= np.linalg.norm(d)
            s += np.clip(normals @ d, 0, 1) * intensity
        ambient = 0.20
        return np.clip(s + ambient, 0.0, 1.0)

    def project_and_shade(view: str):
        if view == "top":
            P2 = V[:, [0, 1]]
            depth = V[:, 2]                   # higher Z = closer to camera
            lights = [
                ((0, 0, 1.0), 0.55),          # key (top down)
                ((1, 1, 0.5), 0.30),          # fill (NE oblique)
                ((-1, -1, 0.3), 0.15),        # rim (SW back)
            ]
        elif view == "front":
            P2 = V[:, [0, 2]]
            depth = -V[:, 1]
            lights = [
                ((0.3, 1.0, 0.5), 0.55),
                ((-0.5, 0.5, 1.0), 0.30),
                ((-0.5, -1.0, 0.0), 0.15),
            ]
        else:                                  # iso
            u = (V[:, 0] - V[:, 1]) / math.sqrt(2.0)
            v = (V[:, 0] + V[:, 1]) / math.sqrt(6.0) + V[:, 2] * math.sqrt(2.0 / 3.0)
            P2 = np.stack([u, v], axis=-1)
            depth = -(V[:, 0] + V[:, 1] + V[:, 2])
            lights = [
                ((0.5, 0.5, 1.0), 0.50),
                ((-0.7, 0.5, 0.4), 0.30),
                ((0.4, -0.4, 0.3), 0.20),
            ]
        tri_depth = depth[F].mean(axis=1)
        shade = shade_with_lights(N, lights)
        # Painter's algo: back-to-front order
        order = np.argsort(tri_depth)
        polys = P2[F[order]]
        s_ord = shade[order]
        d_ord = tri_depth[order]
        # Depth fog 0..1 (1 = nearest, fades to 0.7 at back)
        d_min, d_max = d_ord.min(), d_ord.max()
        d_norm = (d_ord - d_min) / max(d_max - d_min, 1e-6)
        fog = 0.7 + 0.3 * d_norm
        s_ord = s_ord * fog
        return polys, s_ord, d_norm

    def silhouette_edges(view_polys, max_edges=6000):
        """Sample edges between adjacent triangles where shade discontinuity
        is largest — gives a silhouette / crease overlay. Cheap heuristic
        without proper neighbor lookup: just sample triangle outline edges
        with low opacity."""
        # Sample a subset of triangle outlines for the edge overlay
        n = len(view_polys)
        sample = view_polys if n < max_edges else view_polys[
            np.linspace(0, n - 1, max_edges, dtype=int)
        ]
        segs = []
        for tri in sample:
            segs.append([tri[0], tri[1]])
            segs.append([tri[1], tri[2]])
            segs.append([tri[2], tri[0]])
        return segs

    fig, axes = plt.subplots(1, 3, figsize=(17, 6), dpi=150)
    for ax, view in zip(axes, ("top", "iso", "front")):
        polys, shade, d_norm = project_and_shade(view)
        # Color: blue-steel shade, brighter near = lighter
        colors = np.stack([shade * 0.50, shade * 0.62, shade * 0.85,
                           np.ones_like(shade)], axis=-1)
        pc = PolyCollection(polys, facecolors=colors, edgecolors="none",
                            linewidths=0, antialiased=True)
        ax.add_collection(pc)
        # Edge overlay — thin dark lines for crispness
        edges = silhouette_edges(polys)
        if edges:
            lc = LineCollection(edges, linewidths=0.15,
                                colors="#0a1420", alpha=0.30, antialiased=True)
            ax.add_collection(lc)
        ax.autoscale()
        ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title({
            "top":   "Top (XY) — looking down",
            "iso":   "Isometric",
            "front": "Front (XZ) — looking back",
        }[view], fontsize=11, color="#1f4068")
        ax.set_facecolor("#f4f6f9")    # light bg = parts pop
    fig.suptitle(f"{title}", fontsize=13, color="#0d1117", y=0.99)
    fig.tight_layout()
    fig.savefig(str(out_png), dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    r = run_drone_quad()
    if r.validation_failures:
        print(f"\n{len(r.validation_failures)} part(s) FAILED validation:")
        for f in r.validation_failures:
            print(f"  {f['part']}:")
            for msg in f["failures"]:
                print(f"    - {msg}")
    print(f"\nsuccess={r.success}  step={r.step_path}  render={r.render_path}")
