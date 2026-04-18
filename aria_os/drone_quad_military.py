"""
Military-grade tactical reconnaissance drone — armored, vision-equipped, fiber-tethered.

Extends the parametric drone_quad assembly with armor sandwich plates, a nose
vision pod with 2-axis gimbal yoke, a rear fiber-optic tether spool + payout
eyelet, top-mounted GPS puck, ELRS receiver, and an underside payload rail.

Same artifact set as run_drone_quad: STEP + STL assembly, per-part STEPs,
BOM JSON, iso render PNG, params.json snapshot.

Entry point:

    from aria_os.drone_quad_military import run_drone_quad_military
    result = run_drone_quad_military()
"""
from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aria_os.validation import Contract, validate_part
from aria_os.drone_quad import (
    DEFAULT_PARAMS, PARTS as BASE_PARTS,
    PartSpec, DroneAssemblyResult,
    _merge, _color_for, _render_assembly, _render_assembly_by_material,
    # Reuse base part placement helpers
    _motor_xy, _stack_half,
)


# ---------------------------------------------------------------------------
# Default params — extends the 5" base to a 7" military recon platform
# ---------------------------------------------------------------------------

DEFAULT_MILITARY_PARAMS: dict[str, Any] = {
    "name": "drone_recon_military_7inch",
    "frame": {
        "diagonal_mm":     295.0,    # 7" prop quad
        "plate_size_mm":   100.0,
        "plate_bottom_thk_mm": 6.0,  # reinforced
        "plate_top_thk_mm":    4.0,
        "arm_length_mm":   145.0,
        "arm_width_mm":     22.0,
        "arm_thk_mm":        5.0,
        "stack_pitch_mm":   30.5,
        "standoff_len_mm":  35.0,
        "standoff_dia_mm":   5.0,
        "battery_strap_slot_l_mm": 30.0,
        "battery_strap_slot_w_mm":  4.0,
        "battery_strap_slot_y_mm": 18.0,
    },
    "fc_pcb":  {"size_mm": 36.0, "thk_mm": 1.6, "z_offset_mm": 16.0},
    "esc_pcb": {"size_mm": 36.0, "thk_mm": 1.6, "z_offset_mm":  4.0},
    "motor":   {"stator_dia_mm": 32.0, "stator_ht_mm":  9.0,
                "bell_dia_mm":   33.0, "bell_ht_mm":   13.0,
                "shaft_dia_mm":   5.0},
    "prop":    {"dia_mm": 178.0, "thk_mm": 4.0, "n_blades": 3,
                "hub_dia_mm": 14.0, "bore_dia_mm": 5.0},
    "battery": {"l_mm": 95.0, "w_mm": 45.0, "h_mm": 28.0},
    "canopy":  {"l_mm": 55.0, "w_mm": 50.0, "h_mm": 28.0, "wall_mm": 2.0,
                "camera_window_w_mm": 24.0, "camera_window_h_mm": 18.0},

    # ── Military-specific subsections ────────────────────────────────────
    "armor": {
        "thk_mm":          4.0,    # aramid composite layer
        "size_mm":       100.0,    # matches plate
        "stack_clearance_mm": 0.5, # bonded with thin adhesive
    },
    "vision_pod": {
        "l_mm": 50.0, "w_mm": 35.0, "h_mm": 30.0, "wall_mm": 2.0,
        "fpv_window_w_mm": 18.0, "fpv_window_h_mm": 14.0,
        "thermal_aperture_mm": 9.0,  # round window for thermal
    },
    "gimbal": {
        "yoke_w_mm": 28.0, "yoke_h_mm": 20.0, "yoke_thk_mm": 3.0,
        "yoke_arm_len_mm": 14.0,
    },
    "fiber_spool": {
        "od_mm":      85.0,
        "hub_dia_mm": 22.0,
        "width_mm":   30.0,
        "flange_thk_mm": 2.0,
        "fiber_capacity_m": 500,
    },
    "fiber_eyelet": {
        "od_mm": 14.0, "id_mm": 5.0, "thk_mm": 4.0,
    },
    "gps_puck": {
        "od_mm": 32.0, "h_mm": 12.0,
    },
    "rx_module": {
        "l_mm": 28.0, "w_mm": 14.0, "h_mm": 5.0,
    },
    "payload_rail": {
        "l_mm": 70.0, "w_mm": 14.0, "h_mm": 8.0,
        "n_holes": 4, "hole_dia_mm": 4.5,
    },

    "validation": {
        "strict": True,
        "bbox_tol":      0.10,
        "min_lobe_ratio": 0.20,
    },
}


# ---------------------------------------------------------------------------
# Builders + contracts for military-only parts
# ---------------------------------------------------------------------------

def _build_armor_plate(params):
    import cadquery as cq
    a = params["armor"]
    s, t = a["size_mm"], a["thk_mm"]
    plate = cq.Workplane("XY").box(s, s, t, centered=(True, True, False))
    # Match 4 stack holes so it bonds cleanly to top/bottom plate
    half = params["frame"]["stack_pitch_mm"] / 2.0
    plate = (plate.faces(">Z").workplane()
             .pushPoints([(+half, +half), (-half, +half),
                          (-half, -half), (+half, -half)])
             .hole(3.2))
    return plate


def _contract_armor_plate(params):
    a = params["armor"]
    return Contract(
        name="armor_plate",
        expected_bbox_mm=(a["size_mm"], a["size_mm"], a["thk_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=4,
        expected_solid_count=1,
        is_watertight=True,
    )


def _build_vision_pod(params):
    """Hardened vision pod with realistic lens housings.

    Open-back shell with rounded edges; FPV camera lens tube (raised cylinder)
    on the front; thermal sensor aperture with hood; mounting flange on the
    back where it attaches to the canopy. Replaces the plain box-with-2-cuts.
    """
    import cadquery as cq
    v = params["vision_pod"]
    L, W, H = v["l_mm"], v["w_mm"], v["h_mm"]
    wall = v["wall_mm"]

    # Open-back shell with filleted top + side edges (looks like an actual
    # injection-molded sensor housing, not a literal cardboard box)
    box = cq.Workplane("XY").box(L, W, H, centered=(True, True, False))
    try:
        box = box.edges("|X").fillet(min(3.0, H * 0.20))   # fillet top/bot edges
    except Exception:
        pass
    try:
        box = box.edges(">Z").fillet(min(2.0, H * 0.10))   # fillet top corners
    except Exception:
        pass
    shell = box.faces("<X").shell(-wall)

    # FPV camera lens tube — raised cylinder on +X face, centered
    fpv_tube_d = v["fpv_window_h_mm"] + 2.0  # slightly larger than window
    fpv_tube_l = 4.0
    lens_z = H / 2.0 - v["fpv_window_h_mm"] / 2.0 - 1.0
    fpv_tube = (cq.Workplane("YZ")
                .workplane(offset=L / 2.0)
                .center(0, lens_z)
                .circle(fpv_tube_d / 2.0)
                .extrude(fpv_tube_l))
    shell = shell.union(fpv_tube)
    # FPV lens hole through the tube
    shell = (shell.faces(">X").workplane()
             .center(0, lens_z - H / 2.0)  # workplane is on tube face
             .circle(v["fpv_window_w_mm"] / 2.0)
             .cutThruAll())

    # Thermal sensor aperture on the same face, above FPV
    thermal_z = lens_z + v["fpv_window_h_mm"] / 2.0 + 5.0
    thermal_tube = (cq.Workplane("YZ")
                    .workplane(offset=L / 2.0)
                    .center(0, thermal_z)
                    .circle(v["thermal_aperture_mm"] / 2.0 + 1.0)
                    .extrude(2.5))
    shell = shell.union(thermal_tube)
    shell = (shell.faces(">X").workplane()
             .center(0, thermal_z - H / 2.0)
             .circle(v["thermal_aperture_mm"] / 2.0)
             .cutThruAll())

    # Mounting flange on the BACK (-X) face — small lip aligned with box Z.
    # YZ workplane origin is at world (0,0,0); the box is centered=(T,T,F)
    # so its Z extent is [0, H]. Shift the rect up by H/2 to align it with
    # the box midplane (otherwise flange floats below the box, blowing bbox
    # Z up to ~47mm instead of the expected ~30mm).
    flange_thk = 1.5
    flange = (cq.Workplane("YZ")
              .workplane(offset=-L / 2.0)
              .center(0, H / 2.0)
              .rect(W + 4.0, H + 4.0)
              .extrude(-flange_thk))
    shell = shell.union(flange)
    return shell


def _contract_vision_pod(params):
    """Vision pod with lens tubes (extends +X by ~4mm), thermal hood
    (extends +X by ~2.5mm), and mounting flange (extends -X by 1.5mm).
    bbox X grows by ~6mm; W grows by ~4mm (flange extends past base box)."""
    v = params["vision_pod"]
    return Contract(
        name="vision_pod",
        expected_bbox_mm=(v["l_mm"] + 6.0, v["w_mm"] + 4.0, v["h_mm"] + 4.0),
        bbox_tol=0.25,
        expected_solid_count=1,
        is_watertight=True,
    )


def _build_gimbal_yoke(params):
    """U-bracket gimbal yoke — base bar at z=[0,t] + 2 vertical arms standing on top."""
    import cadquery as cq
    g = params["gimbal"]
    w, t, arm_l = g["yoke_w_mm"], g["yoke_thk_mm"], g["yoke_arm_len_mm"]
    # Base bar: Z extent [0, t]
    base = cq.Workplane("XY").box(w, t, t, centered=(True, True, False))
    # Vertical arm: Z extent [0, arm_l] before translate, place base AT z=t
    arm = cq.Workplane("XY").box(t, t, arm_l, centered=(True, True, False))
    yoke = base.union(arm.translate((+w/2 - t/2, 0, t)))
    yoke = yoke.union(arm.translate((-w/2 + t/2, 0, t)))
    return yoke


def _contract_gimbal_yoke(params):
    """Yoke bbox: w wide × t deep × (t + arm_l) tall (base + arm height)."""
    g = params["gimbal"]
    return Contract(
        name="gimbal_yoke",
        expected_bbox_mm=(g["yoke_w_mm"], g["yoke_thk_mm"],
                          g["yoke_thk_mm"] + g["yoke_arm_len_mm"]),
        bbox_tol=0.10,
        expected_solid_count=1,
        is_watertight=True,
    )


def _build_fiber_spool(params):
    """Cylindrical fiber spool — central drum + 2 flanges + center hub bore."""
    import cadquery as cq
    s = params["fiber_spool"]
    od, hub, w, ft = s["od_mm"], s["hub_dia_mm"], s["width_mm"], s["flange_thk_mm"]
    # Drum (the wound section) — hub_dia at center, half-width on each side of flange
    drum_w = w - 2 * ft
    drum_dia = (od + hub) / 2  # nominal wound diameter (sits between hub and flange OD)
    drum = (cq.Workplane("XY")
            .circle(drum_dia / 2)
            .extrude(drum_w)
            .translate((0, 0, ft)))
    # Two flanges
    flange_lo = cq.Workplane("XY").circle(od / 2).extrude(ft)
    flange_hi = (cq.Workplane("XY").circle(od / 2).extrude(ft)
                 .translate((0, 0, ft + drum_w)))
    spool = drum.union(flange_lo).union(flange_hi)
    # Hub bore through center
    spool = spool.faces(">Z").workplane().circle(hub / 2).cutThruAll()
    return spool


def _contract_fiber_spool(params):
    s = params["fiber_spool"]
    return Contract(
        name="fiber_spool",
        expected_bbox_mm=(s["od_mm"], s["od_mm"], s["width_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=1,    # hub bore (1 through-hole)
        expected_solid_count=1,
        is_watertight=True,
    )


def _build_fiber_eyelet(params):
    """Small ring — fiber payout guide on aft edge."""
    import cadquery as cq
    e = params["fiber_eyelet"]
    ring = (cq.Workplane("XY").circle(e["od_mm"] / 2).circle(e["id_mm"] / 2)
            .extrude(e["thk_mm"]))
    return ring


def _contract_fiber_eyelet(params):
    e = params["fiber_eyelet"]
    return Contract(
        name="fiber_eyelet",
        expected_bbox_mm=(e["od_mm"], e["od_mm"], e["thk_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=1,
        expected_solid_count=1,
        is_watertight=True,
    )


def _build_gps_puck(params):
    """Cylindrical GPS housing — used for many u-blox modules."""
    import cadquery as cq
    g = params["gps_puck"]
    return cq.Workplane("XY").circle(g["od_mm"] / 2).extrude(g["h_mm"])


def _contract_gps_puck(params):
    g = params["gps_puck"]
    return Contract(
        name="gps_puck",
        expected_bbox_mm=(g["od_mm"], g["od_mm"], g["h_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=0,
        expected_solid_count=1,
        is_watertight=True,
    )


def _build_rx_module(params):
    """ELRS receiver — small rectangular module."""
    import cadquery as cq
    r = params["rx_module"]
    return cq.Workplane("XY").box(r["l_mm"], r["w_mm"], r["h_mm"], centered=(True, True, False))


def _contract_rx_module(params):
    r = params["rx_module"]
    return Contract(
        name="rx_module",
        expected_bbox_mm=(r["l_mm"], r["w_mm"], r["h_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=0,
        expected_solid_count=1,
        is_watertight=True,
    )


def _build_payload_rail(params):
    """Picatinny-style rail bar with mounting holes."""
    import cadquery as cq
    p = params["payload_rail"]
    rail = cq.Workplane("XY").box(p["l_mm"], p["w_mm"], p["h_mm"], centered=(True, True, False))
    # Cut N evenly-spaced through holes along the length
    n = int(p["n_holes"])
    if n > 0:
        spacing = p["l_mm"] / (n + 1)
        pts = [(spacing * (i + 1) - p["l_mm"] / 2, 0) for i in range(n)]
        rail = (rail.faces(">Z").workplane()
                .pushPoints(pts).hole(p["hole_dia_mm"]))
    return rail


def _contract_payload_rail(params):
    p = params["payload_rail"]
    return Contract(
        name="payload_rail",
        expected_bbox_mm=(p["l_mm"], p["w_mm"], p["h_mm"]),
        bbox_tol=params["validation"]["bbox_tol"],
        expected_hole_count=int(p["n_holes"]),
        expected_solid_count=1,
        is_watertight=True,
    )


# ---------------------------------------------------------------------------
# Placement for military-only parts
# ---------------------------------------------------------------------------

def _place_armor_top(params, i):
    z = (params["frame"]["standoff_len_mm"]
         + params["frame"]["plate_top_thk_mm"]
         + params["armor"]["stack_clearance_mm"])
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


def _place_armor_bottom(params, i):
    z = -(params["armor"]["thk_mm"] + params["armor"]["stack_clearance_mm"])
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


def _place_vision_pod(params, i):
    """Mount FLUSH against canopy front (+X side).

    User feedback: pod was sticking out awkwardly with a gap. Two fixes:
      1. Reduce the +X gap from 2mm to 0mm (back-flange of pod sits flush
         against canopy front face, like a real bolt-on housing)
      2. Lower the pod so its base sits AT the canopy mid-plane, not just
         vertically centered — this puts the camera lens at canopy
         midline (real drones) instead of floating above
    """
    f = params["frame"]
    fc = params["fc_pcb"]
    can = params["canopy"]
    v = params["vision_pod"]
    canopy_z = f["plate_bottom_thk_mm"] + fc["z_offset_mm"] + fc["thk_mm"] + 1.0
    # Flush mount: pod center at canopy_front + pod_l/2 (no gap)
    px = can["l_mm"] / 2 + v["l_mm"] / 2
    # Vertical: pod base at canopy mid-height (lens centerline = canopy mid)
    pz = canopy_z + can["h_mm"] / 2 - v["h_mm"] / 2
    return ((px, 0.0, pz), (0.0, 0.0, 0.0))


def _place_gimbal_yoke(params, i):
    """Yoke sits in front of vision pod at the camera centerline."""
    f = params["frame"]
    fc = params["fc_pcb"]
    can = params["canopy"]
    v = params["vision_pod"]
    canopy_z = f["plate_bottom_thk_mm"] + fc["z_offset_mm"] + fc["thk_mm"] + 1.0
    # Yoke origin: in front of vision pod, centerline of FPV camera window
    px = can["l_mm"] / 2 + v["l_mm"] + 6.0
    pz = canopy_z + can["h_mm"] / 2 - v["h_mm"] / 2  # match pod base z
    return ((px, 0.0, pz), (0.0, 0.0, 0.0))


def _place_fiber_spool(params, i):
    """Spool mounted rear (-X), oriented spool-axis along Y so it pays out aft."""
    f = params["frame"]
    s = params["fiber_spool"]
    # Sits behind the top plate, on Z = (top_plate_z + standoff_clearance)
    px = -(f["plate_size_mm"] / 2 + s["od_mm"] / 2 + 2.0)
    py = 0.0
    pz = f["standoff_len_mm"] + f["plate_top_thk_mm"] + s["od_mm"] / 2  # spool axis on this Z
    # Rotate 90° around X so spool axis is Y (cylinder lies on its side)
    return ((px, py, pz), (90.0, 0.0, 0.0))


def _place_fiber_eyelet(params, i):
    """Eyelet on aft edge for fiber to pass out."""
    f = params["frame"]
    e = params["fiber_eyelet"]
    px = -(f["plate_size_mm"] / 2 + e["od_mm"] / 2)
    pz = f["standoff_len_mm"] / 2  # mid-stack height
    return ((px, 0.0, pz), (90.0, 0.0, 0.0))


def _place_gps_puck(params, i):
    """GPS puck on top of armor, aft-center."""
    f = params["frame"]
    a = params["armor"]
    g = params["gps_puck"]
    z_top = (f["standoff_len_mm"] + f["plate_top_thk_mm"]
             + a["stack_clearance_mm"] + a["thk_mm"])
    # Place aft of center to avoid battery
    return ((-25.0, 0.0, z_top), (0.0, 0.0, 0.0))


def _place_rx_module(params, i):
    """Rx module on top plate next to FC."""
    f = params["frame"]
    z = f["plate_bottom_thk_mm"] + params["fc_pcb"]["z_offset_mm"] + params["fc_pcb"]["thk_mm"] + 0.5
    return ((+18.0, +14.0, z), (0.0, 0.0, 0.0))


def _place_payload_rail(params, i):
    """Picatinny rail underside, centered on bottom plate."""
    a = params["armor"]
    p = params["payload_rail"]
    # Below the bottom armor plate
    z = -(a["thk_mm"] + a["stack_clearance_mm"] + p["h_mm"])
    return ((0.0, 0.0, z), (0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# Combined parts list — base + military
# ---------------------------------------------------------------------------

MILITARY_PARTS: list[PartSpec] = BASE_PARTS + [
    PartSpec("armor_top",     _build_armor_plate,   _contract_armor_plate,   _place_armor_top,    1, material="aramid"),
    PartSpec("armor_bottom",  _build_armor_plate,   _contract_armor_plate,   _place_armor_bottom, 1, material="aramid"),
    PartSpec("vision_pod",    _build_vision_pod,    _contract_vision_pod,    _place_vision_pod,   1, material="petg"),
    PartSpec("gimbal_yoke",   _build_gimbal_yoke,   _contract_gimbal_yoke,   _place_gimbal_yoke,  1, material="aluminum_6061"),
    PartSpec("fiber_spool",   _build_fiber_spool,   _contract_fiber_spool,   _place_fiber_spool,  1, material="petg"),
    PartSpec("fiber_eyelet",  _build_fiber_eyelet,  _contract_fiber_eyelet,  _place_fiber_eyelet, 1, material="aluminum_6061"),
    PartSpec("gps_puck",      _build_gps_puck,      _contract_gps_puck,      _place_gps_puck,     1, material="abs"),
    PartSpec("rx_module",     _build_rx_module,     _contract_rx_module,     _place_rx_module,    1, material="abs"),
    PartSpec("payload_rail",  _build_payload_rail,  _contract_payload_rail,  _place_payload_rail, 1, material="aluminum_7075"),
]


# Material colors not in base table — extend at runtime
def _color_for_military(material: str):
    from cadquery import Color
    if material == "aramid":
        return Color(0.45, 0.40, 0.30, 1.0)   # OD tan
    if material == "abs":
        return Color(0.20, 0.20, 0.20, 1.0)   # dark gray
    return _color_for(material)


# ---------------------------------------------------------------------------
# Run / regenerate
# ---------------------------------------------------------------------------

def run_drone_quad_military(
    *,
    name: str | None = None,
    output_dir: str | Path | None = None,
    params: dict | None = None,
) -> DroneAssemblyResult:
    """Parametric military reconnaissance drone — armored, vision-equipped, fiber-tethered."""
    t0 = time.monotonic()
    cfg = _merge(_merge(DEFAULT_PARAMS, DEFAULT_MILITARY_PARAMS), params)
    name = name or cfg.get("name", "drone_recon_military_7inch")
    cfg["name"] = name

    if output_dir is None:
        output_dir = Path(__file__).resolve().parent.parent / "outputs" / "drone_quad" / name
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    parts_dir = output_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    result = DroneAssemblyResult(name=name, output_dir=str(output_dir))

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

    # ── ECAD FIRST — produces populated PCB STEPs that replace plain stubs ──
    # Originally ECAD ran AFTER assembly export, so the assembly only had flat
    # green rectangles for FC/ESC. Now we run ECAD up front, build populated
    # PCBs (with chips + connectors as 3D bumps), and use those in the
    # mechanical assembly so the user sees a real-looking FC stack.
    ecad_dir = output_dir / "ecad"
    ecad_dir.mkdir(parents=True, exist_ok=True)
    ecad_artifacts = _run_ecad_for_drone(cfg, ecad_dir)
    populated_pcb_steps = _build_populated_pcbs(ecad_artifacts, ecad_dir)

    for spec in MILITARY_PARTS:
        try:
            # Substitute populated PCB STEP for fc_pcb / esc_pcb stubs when
            # ECAD has produced one. Populated PCBs are multi-solid by design
            # (substrate + N component bumps), so we use a lenient contract
            # that checks bbox + watertightness only.
            populated_step = populated_pcb_steps.get(spec.name)
            if populated_step and Path(populated_step).is_file():
                import cadquery as cq2
                shape = cq2.importers.importStep(str(populated_step))
                contract = _populated_pcb_contract(spec.name, cfg)
            else:
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
                    part_records.append({
                        "name": spec.name,
                        "validation": "FAILED",
                        "failures": res.failures,
                    })
                    continue
            for i in range(spec.instances):
                inst_name = (spec.instance_names[i]
                             if spec.instance_names else spec.name)
                pos, rot = spec.placer(cfg, i)
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
                         color=_color_for_military(spec.material))
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

    if failures and strict:
        result.error = (f"{len(failures)} part(s) failed validation in strict mode — "
                        f"see validation_failures")
        result.elapsed_s = time.monotonic() - t0
        (output_dir / "drone_quad_result.json").write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        return result

    step_path = output_dir / f"{name}_assembly.step"
    stl_path = output_dir / f"{name}_assembly.stl"
    try:
        assy.export(str(step_path), exportType="STEP")
        assy.export(str(stl_path), exportType="STL")
        result.step_path = str(step_path)
        result.stl_path = str(stl_path)
    except Exception as exc:
        result.error = f"assembly export failed: {type(exc).__name__}: {exc}"

    bom_path = output_dir / "bom.json"
    bom = {
        "assembly_name": name,
        "platform": "military_recon",
        "n_parts": len(part_records),
        "parts": part_records,
        "params_snapshot": cfg,
        "purchased_components_note": (
            "Purchased catalog items (FC, ESC, GPS, motors, props, battery, Rx, "
            "fiber bundle, gimbal servos) referenced via material codes; "
            "physical mating done via mating_features in catalog specs."
        ),
    }
    bom_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")
    result.bom_path = str(bom_path)

    # Render — prefer the per-material colored render (each part shaded with
    # its real material color so layered components separate visually).
    # Falls back to the single-color STL render if BOM or parts dir missing.
    render_path = output_dir / f"{name}_render.png"
    try:
        if bom_path.is_file() and parts_dir.is_dir():
            _render_assembly_by_material(bom_path, parts_dir, render_path, name)
            result.render_path = str(render_path)
        elif result.stl_path:
            _render_assembly(Path(result.stl_path), render_path, name)
            result.render_path = str(render_path)
    except Exception as exc:
        print(f"[render] per-material render failed: {type(exc).__name__}: {exc}")
        try:
            if result.stl_path:
                _render_assembly(Path(result.stl_path), render_path, name)
                result.render_path = str(render_path)
        except Exception:
            pass

    # ── Drawings: GD&T SVG drawings for the top-level mechanical parts ───
    drawings_dir = output_dir / "drawings"
    drawings_dir.mkdir(parents=True, exist_ok=True)
    drawings_artifacts = _run_drawings_for_drone(parts_dir, drawings_dir)

    result.success = (result.step_path is not None and not failures)
    result.elapsed_s = time.monotonic() - t0
    final = result.to_dict()
    final["ecad"] = ecad_artifacts
    final["drawings"] = drawings_artifacts
    (output_dir / "drone_quad_result.json").write_text(
        json.dumps(final, indent=2), encoding="utf-8")
    return result


def _populated_pcb_contract(pcb_name: str, cfg: dict) -> Contract:
    """Lenient contract for a populated PCB STEP — bbox only.

    The PCB substrate + components is multi-solid by design, and component
    placement positions vary by ECAD run. Strict validation would require
    re-deriving expectations per BOM, which is more brittle than helpful.
    Bbox sanity (PCB size matches spec) is the meaningful check.
    """
    pcb = cfg.get(pcb_name, {}) or cfg.get("fc_pcb", {})
    s = pcb.get("size_mm", 36.0)
    # Allow ±25% bbox slack — components extend above the PCB plane and may
    # add to the bbox Z by 5-15mm depending on connectors.
    return Contract(
        name=f"{pcb_name}_populated",
        expected_bbox_mm=(s, s, pcb.get("thk_mm", 1.6) + 8.0),  # board + tallest comp ~8mm
        bbox_tol=0.50,         # very loose — components vary widely
        is_watertight=True,    # each solid in the assembly is watertight
    )


def _build_populated_pcbs(ecad_artifacts: dict, ecad_dir: Path) -> dict[str, str]:
    """Build populated PCB STEPs from each ECAD BOM. Returns {pcb_name: step_path}.

    Each PCB is built as a green FR-4 substrate with components extruded as
    3D bumps at their placed positions. Heights/colors are class-aware:
    connectors stand 5-10mm tall, ICs 1-3mm, passives <2mm.
    """
    out: dict[str, str] = {}
    try:
        from aria_os.ecad.pcb_3d import build_populated_pcb
    except Exception as exc:
        print(f"[POPULATED-PCB] module unavailable: {exc}")
        return out

    for pcb_name, info in ecad_artifacts.items():
        if not isinstance(info, dict):
            continue
        bom_path = info.get("bom_path")
        if not bom_path or not Path(bom_path).is_file():
            continue
        try:
            out_step = ecad_dir / f"{pcb_name}_populated.step"
            build_populated_pcb(bom_path, out_step)
            out[pcb_name] = str(out_step)
            print(f"[POPULATED-PCB] {pcb_name}: built {out_step.name}")
        except Exception as exc:
            print(f"[POPULATED-PCB] {pcb_name} FAILED: {type(exc).__name__}: {exc}")
    return out


def _run_ecad_for_drone(cfg: dict, ecad_dir: Path) -> dict:
    """Generate KiCad ECAD outputs for the FC PCB and ESC PCB.

    Returns artifact paths per board. Failures per board are recorded but do
    not abort the overall run — ECAD is independent of mechanical assembly.
    """
    artifacts: dict = {}
    try:
        from aria_os.ecad.ecad_generator import generate_ecad
    except Exception as exc:
        return {"error": f"ECAD generator unavailable: {exc}"}

    fc_spec = (
        f"flight controller PCB {cfg['fc_pcb']['size_mm']:.0f}x{cfg['fc_pcb']['size_mm']:.0f}mm, "
        "STM32F405 MCU, MPU6000 IMU, BMP280 barometer, "
        "QMC5883 magnetometer, JST-GH GPS connector, "
        "XT60 battery input, USB-C, 4-in-1 ESC pad header, "
        "ELRS receiver header, 4x M3 mounting holes at 30.5mm pitch"
    )
    esc_spec = (
        f"4-in-1 ESC PCB {cfg['esc_pcb']['size_mm']:.0f}x{cfg['esc_pcb']['size_mm']:.0f}mm, "
        "BLHeli32 firmware MCU, 4x BLDC motor outputs, "
        "XT60 power input, current sensor, "
        "4x M3 mounting holes at 30.5mm pitch"
    )
    for label, spec in (("fc_pcb", fc_spec), ("esc_pcb", esc_spec)):
        sub = ecad_dir / label
        sub.mkdir(parents=True, exist_ok=True)
        try:
            script_path, bom_path = generate_ecad(spec, out_dir=sub)
            val_path = sub / "validation.json"

            # Generate the actual .kicad_pcb file (s-expression format) so
            # the user gets a fabricable PCB they can open in KiCad directly,
            # not just a Python script they have to run inside pcbnew.
            kicad_pcb_path = None
            gerber_info = None
            gerber_dir = None
            n_gerber_files = 0
            gerber_zip_path = None
            if bom_path and Path(bom_path).is_file():
                try:
                    from aria_os.ecad.kicad_pcb_writer import (
                        write_kicad_pcb, export_gerbers,
                    )
                    kicad_pcb_path = write_kicad_pcb(
                        bom_path, sub / f"{label}.kicad_pcb",
                        board_name=label,
                    )
                    # Try Gerber export if kicad-cli is installed (no-op
                    # otherwise — gerbers can be generated later).
                    gerber_info = export_gerbers(kicad_pcb_path,
                                                 sub / "gerbers")
                    if isinstance(gerber_info, dict) and gerber_info.get("available"):
                        gerber_dir = gerber_info.get("gerber_dir")
                        n_gerber_files = int(gerber_info.get("n_files") or 0)
                        # Pack all Gerber/drill files into a single zip so
                        # JLCPCB / OSHPark / Aisler can ingest it directly
                        # from the bundle. No-op if no files were produced.
                        if gerber_dir and n_gerber_files > 0:
                            gerber_zip_path = _zip_gerbers(
                                Path(gerber_dir),
                                sub / f"{label}_gerbers.zip",
                            )
                except Exception as exc:
                    print(f"[ecad] kicad_pcb writer failed for {label}: "
                          f"{type(exc).__name__}: {exc}")

            artifacts[label] = {
                "spec": spec,
                "script_path": str(script_path) if script_path else None,
                "bom_path": str(bom_path) if bom_path else None,
                "validation_path": str(val_path) if val_path.is_file() else None,
                "kicad_pcb_path": str(kicad_pcb_path) if kicad_pcb_path else None,
                "gerbers": gerber_info,
                "gerber_dir": gerber_dir,
                "n_gerber_files": n_gerber_files,
                "gerber_zip_path": str(gerber_zip_path) if gerber_zip_path else None,
            }
        except Exception as exc:
            artifacts[label] = {"error": f"{type(exc).__name__}: {exc}",
                                "spec": spec}
    return artifacts


def _zip_gerbers(gerber_dir: Path, zip_path: Path) -> Path | None:
    """Zip every Gerber (.gbr/.gbrjob) and drill (.drl) file in `gerber_dir`
    into `zip_path` for one-click fab submission.

    Returns the zip path on success, None on failure. Failures are logged
    but non-fatal — the unzipped gerbers remain available.
    """
    import zipfile
    try:
        gerber_exts = {".gbr", ".gbrjob", ".drl", ".gm1", ".gko", ".gtl",
                       ".gbl", ".gts", ".gbs", ".gto", ".gbo"}
        files = [p for p in gerber_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in gerber_exts]
        if not files:
            return None
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, arcname=f.name)
        return zip_path
    except Exception as exc:
        print(f"[ecad] gerber zip failed for {gerber_dir}: "
              f"{type(exc).__name__}: {exc}")
        return None


def _run_drawings_for_drone(parts_dir: Path, drawings_dir: Path) -> dict:
    """Generate GD&T SVG drawings for top-level mechanical parts.

    Picks the parts that benefit most from a drawing (plates, arms, spool).
    Skips small fixed components (standoffs, eyelet) where drawings add no
    value.
    """
    artifacts: dict = {}
    try:
        from aria_os.drawing_generator import generate_gdnt_drawing
    except Exception as exc:
        return {"error": f"drawing module unavailable: {exc}"}

    targets = [
        "bottom_plate", "top_plate", "arm",
        "armor_top", "armor_bottom",
        "vision_pod", "fiber_spool", "payload_rail",
    ]
    for part_name in targets:
        step_file = parts_dir / f"{part_name}.step"
        if not step_file.is_file():
            continue
        try:
            svg_path = generate_gdnt_drawing(
                step_file, part_id=part_name, params={}, repo_root=None,
            )
            target = drawings_dir / f"{part_name}.svg"
            if Path(svg_path).is_file():
                target.write_bytes(Path(svg_path).read_bytes())
                artifacts[part_name] = str(target)
        except Exception as exc:
            artifacts[part_name] = {"error": f"{type(exc).__name__}: {exc}"}
    return artifacts


if __name__ == "__main__":
    r = run_drone_quad_military()
    if r.validation_failures:
        print(f"\n{len(r.validation_failures)} part(s) FAILED validation:")
        for f in r.validation_failures:
            print(f"  {f['part']}:")
            for msg in f["failures"]:
                print(f"    - {msg}")
    print(f"\nsuccess={r.success}  parts={len(r.parts)}  step={r.step_path}  render={r.render_path}")
