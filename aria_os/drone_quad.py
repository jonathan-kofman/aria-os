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
    import cadquery as cq
    m = params["motor"]
    stator = cq.Workplane("XY").circle(m["stator_dia_mm"] / 2.0).extrude(m["stator_ht_mm"])
    bell = (cq.Workplane("XY").workplane(offset=m["stator_ht_mm"])
            .circle(m["bell_dia_mm"] / 2.0).extrude(m["bell_ht_mm"]))
    shaft = (cq.Workplane("XY").workplane(offset=m["stator_ht_mm"])
             .circle(m["shaft_dia_mm"] / 2.0).extrude(m["bell_ht_mm"] + 2.0))
    return stator.union(bell).union(shaft)


def _contract_motor(params):
    m = params["motor"]
    total_h = m["stator_ht_mm"] + m["bell_ht_mm"] + 2.0
    max_dia = max(m["stator_dia_mm"], m["bell_dia_mm"])
    return Contract(
        name="motor",
        expected_bbox_mm=(max_dia, max_dia, total_h),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=0,
        expected_solid_count=1,
        is_watertight=True,
    )


# ── Prop (TRI-BLADE — fixed from previous broken disc-only version) ──────────

def _build_prop(params):
    """Tri-blade propeller — full geometry, properly cuts gap sectors."""
    import cadquery as cq
    p = params["prop"]
    dia = p["dia_mm"]
    thk = p["thk_mm"]
    n_blades = int(p["n_blades"])
    hub_dia = p["hub_dia_mm"]
    bore_dia = p["bore_dia_mm"]
    blade_r = dia / 2.0

    prop = cq.Workplane("XY").circle(blade_r).extrude(thk)
    sector_deg = 360.0 / n_blades
    blade_half_deg = sector_deg * 0.20  # blade occupies 40% of its sector
    for i in range(n_blades):
        center_deg = i * sector_deg
        gap_start = center_deg + blade_half_deg
        gap_end   = center_deg + sector_deg - blade_half_deg
        n_seg = 24
        cutter_pts = [(0.0, 0.0)]
        for s in range(n_seg + 1):
            a_deg = gap_start + (gap_end - gap_start) * s / n_seg
            a = math.radians(a_deg)
            cutter_pts.append((math.cos(a) * (blade_r + 5),
                               math.sin(a) * (blade_r + 5)))
        cutter_pts.append((0.0, 0.0))
        cutter = (cq.Workplane("XY")
                  .polyline(cutter_pts).close()
                  .extrude(thk + 1))
        prop = prop.cut(cutter)
    # Hub disc + bore
    prop = prop.union(cq.Workplane("XY").circle(hub_dia / 2.0).extrude(thk))
    prop = prop.faces(">Z").workplane().circle(bore_dia / 2.0).cutThruAll()
    return prop


def _contract_prop(params):
    p = params["prop"]
    return Contract(
        name="prop",
        expected_bbox_mm=(p["dia_mm"] * 0.95, p["dia_mm"] * 0.95, p["thk_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=1,
        expected_solid_count=1,
        is_watertight=True,
        radial_features={"n_blades": int(p["n_blades"]),
                         "min_blade_to_gap_ratio": params["validation"]["min_lobe_ratio"]},
    )


# ── Battery ──────────────────────────────────────────────────────────────────

def _build_battery(params):
    import cadquery as cq
    b = params["battery"]
    return cq.Workplane("XY").box(b["l_mm"], b["w_mm"], b["h_mm"], centered=(True, True, False))


def _contract_battery(params):
    b = params["battery"]
    return Contract(
        name="battery",
        expected_bbox_mm=(b["l_mm"], b["w_mm"], b["h_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
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
    """Render assembly as 3-panel image: top + iso + front views, with shaded
    silhouettes (filled polygons via matplotlib Poly3DCollection) so that
    fillets, taper, and other surface curvature is actually visible.

    The previous wireframe iso buried fine details (fillets, hex shapes)
    in line clutter at full-assembly scale.
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
    N = mesh.face_normals  # shape (NF, 3)

    # Project triangles to a 2D view + shade by angle to a fixed light direction
    def project_and_shade(view: str):
        if view == "top":     # XY plane (look down +Z)
            P2 = V[:, [0, 1]]
            depth = -V[:, 2]               # smaller = front
            light_dir = np.array([0, 0, 1.0])
        elif view == "front": # XZ plane (look from +Y)
            P2 = V[:, [0, 2]]
            depth = -V[:, 1]
            light_dir = np.array([0, 1.0, 0.5])
        else:                 # iso
            u = (V[:, 0] - V[:, 1]) / math.sqrt(2.0)
            v = (V[:, 0] + V[:, 1]) / math.sqrt(6.0) + V[:, 2] * math.sqrt(2.0 / 3.0)
            P2 = np.stack([u, v], axis=-1)
            depth = -(V[:, 0] + V[:, 1] + V[:, 2])
            light_dir = np.array([0.5, 0.5, 1.0])
            light_dir = light_dir / np.linalg.norm(light_dir)
        # Per-triangle: average depth + shade
        tri_depth = depth[F].mean(axis=1)
        # Shade: cosine of angle between face normal and light dir, clamp
        shade = np.clip((N @ light_dir) * 0.6 + 0.4, 0.15, 1.0)
        # Sort back-to-front so painter's algorithm renders correctly
        order = np.argsort(tri_depth)
        polys = P2[F[order]]      # (NF, 3, 2)
        gray = shade[order]       # (NF,)
        return polys, gray

    fig, axes = plt.subplots(1, 3, figsize=(16, 6), dpi=140)
    for ax, view in zip(axes, ("top", "iso", "front")):
        polys, gray = project_and_shade(view)
        # RGB color from grayscale shade, slight blue tint
        colors = np.stack([gray * 0.45, gray * 0.55, gray * 0.75], axis=-1)
        pc = PolyCollection(polys, facecolors=colors, edgecolors="none",
                            linewidths=0, antialiased=False)
        ax.add_collection(pc)
        ax.autoscale()
        ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title({
            "top":   "Top (XY) — looking down",
            "iso":   "Isometric",
            "front": "Front (XZ) — looking back",
        }[view], fontsize=11, color="#1f4068")
        # Subtle grid background
        ax.set_facecolor("#0a0e15")
    fig.suptitle(f"{title}", fontsize=13, color="#0d1117", y=0.99)
    fig.tight_layout()
    fig.savefig(str(out_png), dpi=140, bbox_inches="tight", facecolor="white")
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
