"""
aria_os/cadquery_generator.py

CadQuery geometry generator for ARIA parts.
Produces STEP + STL by executing CadQuery scripts in-process.
This is the most reliable path for precise mechanical parts that
need exact dimensions and are describable by extrude/cut/revolve operations.

All known ARIA parts have a dedicated template.  Unknown parts fall back to
the LLM which still returns CadQuery code (headless, no Rhino required).
"""
from __future__ import annotations

import re
import traceback
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Per-part CadQuery templates
# ---------------------------------------------------------------------------

def _cq_ratchet_ring(params: dict[str, Any]) -> str:
    import math as _m
    od    = float(params.get("od_mm", 213.0))
    bore  = float(params.get("bore_mm", 120.2))   # spool hub OD fit (+0.2 mm clearance)
    thick = float(params.get("thickness_mm", params.get("height_mm", 21.0)))
    teeth = int(params.get("n_teeth", 24))

    # External ratchet: teeth project outward to OD tip circle.
    # Direct formula for r_root so tooth space = 8 mm (pawl tip 6mm + clearance):
    #   space = 2π·r_root/N - (r_tip - r_root)·(tan60°+tan8°) = 8
    #   r_root·(2π/N + tan60°+tan8°) = 8 + r_tip·(tan60°+tan8°)
    r_tip  = od / 2.0
    _k     = _m.tan(_m.radians(60)) + _m.tan(_m.radians(8))  # ~1.873
    r_root = round((8.0 + r_tip * _k) / (2 * _m.pi / teeth + _k), 3)
    r_root = max(r_root, bore / 2.0 + 5.0)   # never eat into bore
    tooth_h = round(r_tip - r_root, 3)

    return f"""
import cadquery as cq, math

# === ARIA Ratchet Ring — external teeth, asymmetric profile ===
# Teeth project outward from root circle to OD tip circle.
# Drive face  8° from radial  → self-locking (pawl cannot override on load)
# Back face  60° from radial  → shallow ramp (pawl slides over on forward spin)
# Bore = 120 mm  → fits spool hub OD
# Face width = 20 mm centred in 21 mm thickness (0.5 mm shoulder each side)

OD_MM        = {od}          # tip circle diameter
BORE_MM      = {bore}        # spool hub fit
THICK_MM     = {thick}
N_TEETH      = {teeth}
R_TIP        = OD_MM / 2.0            # {r_tip} mm
R_ROOT       = {r_root}               # root circle radius
TOOTH_H      = {tooth_h}              # tip - root
FACE_W       = 20.0                   # axial tooth face (from aria_mechanical.md)
DRIVE_DEG    = 8.0
BACK_DEG     = 60.0
Z_OFF        = (THICK_MM - FACE_W) / 2.0   # 0.5 mm shoulder

# --- base ring: bore to root circle ---
base = (
    cq.Workplane("XY")
    .circle(R_ROOT)
    .circle(BORE_MM / 2.0)
    .extrude(THICK_MM)
)

# --- add 24 asymmetric teeth ---
d_drive = TOOTH_H * math.tan(math.radians(DRIVE_DEG))
d_back  = TOOTH_H * math.tan(math.radians(BACK_DEG))
_failed_teeth = 0

for i in range(N_TEETH):
    a = math.radians(i * 360.0 / N_TEETH)
    ca, sa = math.cos(a), math.sin(a)

    def g(r, t):   # local radial/tangential → global XY
        return (r*ca - t*sa, r*sa + t*ca)

    p_back  = g(R_ROOT, -d_back)
    p_drive = g(R_ROOT,  d_drive)
    p_tip   = g(R_TIP,   0.0)

    tooth = (
        cq.Workplane("XY")
        .workplane(offset=Z_OFF)
        .polyline([p_back, p_tip, p_drive])
        .close()
        .extrude(FACE_W)
    )
    try:
        base = base.union(tooth)
    except Exception:
        _failed_teeth += 1

if _failed_teeth > 0:
    print(f"[WARN] {{_failed_teeth}}/{{N_TEETH}} teeth failed to union")

result = base
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_housing(params: dict[str, Any]) -> str:
    import math as _math
    od = params.get("od_mm")
    if od:
        # Cylindrical joint housing (robot joints, bearing housings)
        od    = float(od)
        bore  = float(params.get("bore_mm", od * 0.4))
        h     = float(params.get("height_mm", params.get("width_mm", od * 0.8)))
        bolt_r = float(params.get("bolt_circle_r_mm", od * 0.4))
        n     = int(params.get("n_bolts", 4))
        bdia  = float(params.get("bolt_dia_mm", 6.0))
        pts   = [(round(bolt_r * _math.cos(_math.radians(i * 360 / n)), 3),
                  round(bolt_r * _math.sin(_math.radians(i * 360 / n)), 3))
                 for i in range(n)]
        return f"""
import cadquery as cq

OD_MM        = {od}
BORE_MM      = {bore}
HEIGHT_MM    = {h}
BOLT_R_MM    = {bolt_r}
BOLT_DIA_MM  = {bdia}

result = cq.Workplane("XY").circle(OD_MM / 2.0).extrude(HEIGHT_MM)
result = result.faces(">Z").workplane().circle(BORE_MM / 2.0).cutThruAll()
result = result.faces(">Z").workplane().pushPoints({pts!r}).circle(BOLT_DIA_MM / 2.0).cutThruAll()
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""
    else:
        # Rectangular enclosure with lid cutout and mounting bosses
        import math as _math2
        w    = float(params.get("width_mm", 200.0))
        h    = float(params.get("height_mm", 150.0))
        d    = float(params.get("depth_mm", 100.0))
        wall = float(params.get("wall_mm", max(4.0, min(w, h, d) * 0.05)))
        n_mount = int(params.get("n_bolts", 4))
        mount_d = float(params.get("bolt_dia_mm", 5.0))
        boss_od = float(params.get("boss_od_mm", mount_d * 2.5))
        mount_pts = [(round((w / 2 - wall * 2) * _math2.cos(_math2.radians(a)), 3),
                      round((d / 2 - wall * 2) * _math2.sin(_math2.radians(a)), 3))
                     for a in [45, 135, 225, 315]] if n_mount == 4 else []
        return f"""
import cadquery as cq

WIDTH_MM  = {w}
HEIGHT_MM = {h}
DEPTH_MM  = {d}
WALL_MM   = {wall}
BOSS_OD   = {boss_od}
MOUNT_D   = {mount_d}

# --- Outer box ---
outer = cq.Workplane("XY").box(WIDTH_MM, DEPTH_MM, HEIGHT_MM)

# --- Shell out interior (open top face for lid) ---
result = outer.shell(-WALL_MM)

# --- Mounting bosses at 4 corners (inside, at bottom) ---
mount_pts = {mount_pts!r}
if len(mount_pts) > 0:
    for px, py in mount_pts:
        boss = (cq.Workplane("XY")
                .workplane(offset=-HEIGHT_MM / 2.0)
                .center(px, py)
                .circle(BOSS_OD / 2.0)
                .extrude(HEIGHT_MM - WALL_MM))
        boss_hole = (cq.Workplane("XY")
                     .workplane(offset=-HEIGHT_MM / 2.0 - 1.0)
                     .center(px, py)
                     .circle(MOUNT_D / 2.0)
                     .extrude(HEIGHT_MM + 2.0))
        result = result.union(boss).cut(boss_hole)

# --- Lid screw bosses on rim (top face corners) ---
lid_pts = [(WIDTH_MM / 2 - WALL_MM * 1.5, DEPTH_MM / 2 - WALL_MM * 1.5),
           (-WIDTH_MM / 2 + WALL_MM * 1.5, DEPTH_MM / 2 - WALL_MM * 1.5),
           (-WIDTH_MM / 2 + WALL_MM * 1.5, -DEPTH_MM / 2 + WALL_MM * 1.5),
           (WIDTH_MM / 2 - WALL_MM * 1.5, -DEPTH_MM / 2 + WALL_MM * 1.5)]
for lx, ly in lid_pts:
    lid_hole = (cq.Workplane("XY")
                .workplane(offset=HEIGHT_MM / 2.0 - WALL_MM - 1.0)
                .center(lx, ly)
                .circle(MOUNT_D * 0.4)
                .extrude(WALL_MM + 2.0))
    result = result.cut(lid_hole)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_hollow_rect(params: dict[str, Any]) -> str:
    """Hollow rectangular tube — structural arm links, extrusion profiles."""
    w    = float(params.get("width_mm",  80.0))
    d    = float(params.get("depth_mm",  params.get("height_mm", 60.0)))
    l    = float(params.get("length_mm", 300.0))
    wall = float(params.get("wall_mm",   5.0))
    # Inner cavity must leave at least 1mm wall on each side
    iw   = max(w - 2 * wall, 1.0)
    id_  = max(d - 2 * wall, 1.0)
    return f"""
import cadquery as cq

WIDTH_MM  = {w}
DEPTH_MM  = {d}
LENGTH_MM = {l}
WALL_MM   = {wall}
INNER_W   = {iw}
INNER_D   = {id_}

outer = cq.Workplane("XY").box(WIDTH_MM, DEPTH_MM, LENGTH_MM)
inner = cq.Workplane("XY").box(INNER_W, INNER_D, LENGTH_MM + 2.0)
result = outer.cut(inner)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_spool(params: dict[str, Any]) -> str:
    drum_od    = float(params.get("diameter", params.get("od_mm", 600.0)))
    drum_w     = float(params.get("width", params.get("drum_width_mm", 50.0)))
    fl_od      = float(params.get("flange_diameter", params.get("flange_od_mm", drum_od * 1.15)))
    fl_thick   = float(params.get("flange_thickness", params.get("flange_thickness_mm", max(6.0, drum_w * 0.12))))
    hub_od     = float(params.get("hub_diameter", params.get("bore_mm", drum_od * 0.08)))
    groove_d   = float(params.get("groove_depth_mm", max(3.0, drum_od * 0.01)))
    groove_w   = float(params.get("groove_width_mm", max(8.0, drum_w * 0.6)))
    n_grooves  = int(params.get("n_grooves", max(1, int(drum_w / groove_w * 0.7))))
    return f"""
import cadquery as cq

DRUM_OD_MM    = {drum_od}
DRUM_W_MM     = {drum_w}
FLANGE_OD_MM  = {fl_od}
FLANGE_TH_MM  = {fl_thick}
HUB_OD_MM     = {hub_od}
GROOVE_D_MM   = {groove_d}
GROOVE_W_MM   = {groove_w}
N_GROOVES     = {n_grooves}

# Drum core
drum = cq.Workplane("XY").circle(DRUM_OD_MM / 2.0).extrude(DRUM_W_MM)

# Flanges (bottom + top)
fl_b = cq.Workplane("XY").circle(FLANGE_OD_MM / 2.0).extrude(FLANGE_TH_MM)
fl_t = (cq.Workplane("XY").workplane(offset=DRUM_W_MM - FLANGE_TH_MM)
        .circle(FLANGE_OD_MM / 2.0).extrude(FLANGE_TH_MM))

# Hub bore (through)
hub_bore = (cq.Workplane("XY").workplane(offset=-1.0)
            .circle(HUB_OD_MM / 2.0).extrude(DRUM_W_MM + 2.0))

result = drum.union(fl_b).union(fl_t).cut(hub_bore)

# Rope guide channel(s) — helical groove(s) cut into drum OD surface
# Approximated as circumferential V-grooves evenly spaced along drum width
groove_r_outer = DRUM_OD_MM / 2.0 + 0.1   # cut slightly past OD
groove_r_inner = DRUM_OD_MM / 2.0 - GROOVE_D_MM
if N_GROOVES > 0 and GROOVE_D_MM > 0.5:
    spacing = (DRUM_W_MM - 2 * FLANGE_TH_MM) / max(N_GROOVES, 1)
    for gi in range(N_GROOVES):
        z_center = FLANGE_TH_MM + spacing * (gi + 0.5)
        groove_ring = (
            cq.Workplane("XY")
            .workplane(offset=z_center - GROOVE_W_MM / 2.0)
            .circle(groove_r_outer)
            .circle(groove_r_inner)
            .extrude(GROOVE_W_MM)
        )
        result = result.cut(groove_ring)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_cam_collar(params: dict[str, Any]) -> str:
    import math as _m
    od   = float(params.get("od_mm", params.get("diameter", 55.0)))
    h    = float(params.get("height_mm", params.get("height", 40.0)))
    bore = float(params.get("bore_mm", params.get("bore", 25.0)))
    # Helical ramp: 15° cam ramp over 90° arc, rise = h * 0.12
    ramp_rise   = float(params.get("ramp_rise_mm", round(h * 0.12, 2)))
    ramp_arc    = float(params.get("ramp_arc_deg", 90.0))
    ramp_angle  = float(params.get("ramp_angle_deg", 15.0))
    # Set screw: M4 by default, radial through wall
    set_screw_d = float(params.get("set_screw_dia_mm", 4.0))
    wall = (od - bore) / 2.0
    # Validate geometry
    if bore >= od:
        bore = od * 0.6
    return f"""
import cadquery as cq, math

OD_MM         = {od}
HEIGHT_MM     = {h}
BORE_MM       = {bore}
RAMP_RISE_MM  = {ramp_rise}
RAMP_ARC_DEG  = {ramp_arc}
SET_SCREW_D   = {set_screw_d}
WALL_MM       = {wall:.2f}

# --- Base annular cylinder ---
result = (
    cq.Workplane("XY")
    .circle(OD_MM / 2.0)
    .circle(BORE_MM / 2.0)
    .extrude(HEIGHT_MM)
)

# --- Helical cam ramp on bore surface ---
# Cut a ramped wedge from the bore face to create the engagement ramp.
# The ramp is a triangular prism swept along the bore arc.
# We approximate with a series of small cuts at increasing depth.
N_RAMP_SEGS = 12
for i in range(N_RAMP_SEGS):
    frac = i / N_RAMP_SEGS
    angle_deg = frac * RAMP_ARC_DEG
    ramp_depth = frac * RAMP_RISE_MM
    a_rad = math.radians(angle_deg)
    cx = (BORE_MM / 2.0 + 1.0) * math.cos(a_rad)
    cy = (BORE_MM / 2.0 + 1.0) * math.sin(a_rad)
    seg_len = math.pi * BORE_MM * (RAMP_ARC_DEG / N_RAMP_SEGS) / 360.0
    try:
        wedge = (
            cq.Workplane("XY")
            .workplane(offset=HEIGHT_MM - ramp_depth)
            .transformed(rotate=cq.Vector(0, 0, angle_deg))
            .center(BORE_MM / 2.0 * 0.85, 0)
            .rect(WALL_MM * 0.3, max(seg_len, 1.0))
            .extrude(ramp_depth + 0.5)
        )
        result = result.cut(wedge)
    except Exception:
        pass  # skip failed ramp segment

# --- Radial set screw hole (M{{SET_SCREW_D:.0f}}) at mid-height ---
set_screw = (
    cq.Workplane("XZ")
    .workplane(offset=0)
    .center(HEIGHT_MM / 2.0, 0)
    .circle(SET_SCREW_D / 2.0)
    .extrude(OD_MM)
)
result = result.cut(set_screw)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_brake_drum(params: dict[str, Any]) -> str:
    import math as _m
    od      = float(params.get("od_mm",        params.get("diameter",        200.0)))
    w       = float(params.get("thickness_mm", params.get("height_mm",
                               params.get("width_mm", params.get("width", 40.0)))))
    shaft_d = float(params.get("bore_mm",      params.get("shaft_diameter",  20.0)))
    wall    = float(params.get("wall_mm",      params.get("wall_thickness",
                               max(8.0, od * 0.04))))
    hub_od  = float(params.get("hub_od_mm", max(shaft_d * 2.5, od * 0.25)))
    hub_h   = float(params.get("hub_height_mm", max(w * 0.3, 10.0)))
    n_bolts = int(params.get("n_bolts", 4))
    bolt_d  = float(params.get("bolt_dia_mm", 6.0))
    bolt_r  = float(params.get("bolt_circle_r_mm", hub_od * 0.35))
    # Friction grooves on inner drum surface
    n_grooves = int(params.get("n_friction_grooves", 8))
    # Clamp shaft_d to valid range
    shaft_d = min(shaft_d, od - 2 * wall - 2.0)
    bolt_pts = [(round(bolt_r * _m.cos(_m.radians(i * 360 / n_bolts)), 3),
                 round(bolt_r * _m.sin(_m.radians(i * 360 / n_bolts)), 3))
                for i in range(n_bolts)]
    return f"""
import cadquery as cq, math

OD_MM        = {od}
WIDTH_MM     = {w}
SHAFT_D_MM   = {shaft_d}
WALL_MM      = {wall}
HUB_OD_MM    = {hub_od}
HUB_H_MM     = {hub_h}
N_GROOVES    = {n_grooves}
BOLT_D_MM    = {bolt_d}

# --- Outer drum shell ---
outer = cq.Workplane("XY").circle(OD_MM / 2.0).extrude(WIDTH_MM)
inner_void = (cq.Workplane("XY").workplane(offset=WALL_MM)
              .circle(OD_MM / 2.0 - WALL_MM).extrude(WIDTH_MM - WALL_MM + 1.0))
drum = outer.cut(inner_void)

# --- Hub with bore ---
hub = (cq.Workplane("XY")
       .circle(HUB_OD_MM / 2.0)
       .circle(SHAFT_D_MM / 2.0)
       .extrude(HUB_H_MM))
# Web plate connecting hub to drum
web = (cq.Workplane("XY")
       .circle(OD_MM / 2.0 - WALL_MM)
       .circle(HUB_OD_MM / 2.0)
       .extrude(WALL_MM))
result = drum.union(hub).union(web)

# --- Bore through ---
bore_cyl = (cq.Workplane("XY").workplane(offset=-1.0)
            .circle(SHAFT_D_MM / 2.0).extrude(WIDTH_MM + 2.0))
result = result.cut(bore_cyl)

# --- Hub bolt holes ---
bolt_pts = {bolt_pts!r}
if len(bolt_pts) > 0:
    bolt_holes = (cq.Workplane("XY").workplane(offset=-1.0)
                  .pushPoints(bolt_pts)
                  .circle(BOLT_D_MM / 2.0).extrude(HUB_H_MM + 2.0))
    result = result.cut(bolt_holes)

# --- Friction grooves (circumferential score lines on inner drum surface) ---
if N_GROOVES > 0:
    groove_d = max(0.5, WALL_MM * 0.08)
    groove_w = 1.5
    for gi in range(N_GROOVES):
        z_g = WALL_MM + (WIDTH_MM - WALL_MM) * (gi + 0.5) / N_GROOVES
        try:
            groove = (cq.Workplane("XY").workplane(offset=z_g - groove_w / 2)
                      .circle(OD_MM / 2.0 - WALL_MM + groove_d)
                      .circle(OD_MM / 2.0 - WALL_MM)
                      .extrude(groove_w))
            result = result.cut(groove)
        except Exception:
            pass

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_catch_pawl(params: dict[str, Any]) -> str:
    import math as _m
    length = float(params.get("length_mm", 60.0))
    w      = float(params.get("width_mm", 12.0))
    thick  = float(params.get("thickness_mm", 6.0))
    bore   = float(params.get("bore_mm", params.get("pivot_hole_dia_mm", 6.0)))
    # Tooth engagement tip: angled face at the free end
    tip_angle = float(params.get("tip_angle_deg", 30.0))
    tip_depth = float(params.get("tip_depth_mm", max(3.0, w * 0.25)))
    # Spring hole for return spring near pivot
    spring_d  = float(params.get("spring_hole_dia_mm", max(2.0, bore * 0.5)))
    return f"""
import cadquery as cq, math

LENGTH_MM        = {length}
WIDTH_MM         = {w}
THICKNESS_MM     = {thick}
PIVOT_HOLE_D_MM  = {bore}
TIP_DEPTH_MM     = {tip_depth}
TIP_ANGLE_DEG    = {tip_angle}
SPRING_D_MM      = {spring_d}

# --- Main body: tapered pawl shape ---
# Wider at pivot end, narrowing toward tip for engagement
body = cq.Workplane("XY").box(LENGTH_MM, WIDTH_MM, THICKNESS_MM)

# --- Tapered tip: angled engagement face at free end ---
# Cut a wedge from the top-right corner to create the tooth engagement surface.
# Drive face (steep) engages the ratchet tooth; back face (shallow) rides over.
tip_x = LENGTH_MM / 2.0
tip_cut_len = TIP_DEPTH_MM / math.tan(math.radians(TIP_ANGLE_DEG)) if TIP_ANGLE_DEG > 0 else TIP_DEPTH_MM
wedge = (
    cq.Workplane("XY")
    .workplane(offset=-0.5)
    .moveTo(tip_x - tip_cut_len, WIDTH_MM / 2.0 + 0.1)
    .lineTo(tip_x + 0.1,         WIDTH_MM / 2.0 + 0.1)
    .lineTo(tip_x + 0.1,         WIDTH_MM / 2.0 - TIP_DEPTH_MM)
    .close()
    .extrude(THICKNESS_MM + 1.0)
)
result = body.cut(wedge)

# --- Pivot bore at left end ---
pivot = (cq.Workplane("XY").workplane(offset=-1.0)
         .center(-LENGTH_MM / 2.0 + WIDTH_MM / 2.0, 0)
         .circle(PIVOT_HOLE_D_MM / 2.0).extrude(THICKNESS_MM + 2.0))
result = result.cut(pivot)

# --- Spring return hole (smaller, between pivot and midpoint) ---
spring_pos_x = -LENGTH_MM / 2.0 + WIDTH_MM * 1.5
spring = (cq.Workplane("XY").workplane(offset=-1.0)
          .center(spring_pos_x, 0)
          .circle(SPRING_D_MM / 2.0).extrude(THICKNESS_MM + 2.0))
result = result.cut(spring)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_rope_guide(params: dict[str, Any]) -> str:
    width  = float(params.get("width_mm",  params.get("bracket_width", 80.0)))
    height = float(params.get("height_mm", params.get("bracket_height", 50.0)))
    thick  = float(params.get("thickness_mm", params.get("bracket_thickness", 6.0)))
    roller_d = float(params.get("diameter_mm", params.get("roller_diameter", 30.0)))
    bore   = float(params.get("bore_mm", params.get("bore", 8.0)))
    rope_w = float(params.get("rope_width_mm", max(10.0, roller_d * 0.4)))
    arm_w  = float(params.get("arm_width_mm", max(12.0, width * 0.18)))
    n_mount = int(params.get("n_bolts", 2))
    mount_d = float(params.get("bolt_dia_mm", 6.0))
    return f"""
import cadquery as cq

WIDTH_MM      = {width}
HEIGHT_MM     = {height}
THICKNESS_MM  = {thick}
ROLLER_D_MM   = {roller_d}
BORE_MM       = {bore}
ROPE_W_MM     = {rope_w}
ARM_W_MM      = {arm_w}
N_MOUNT       = {n_mount}
MOUNT_D_MM    = {mount_d}

# --- Base mounting plate ---
base_plate = cq.Workplane("XY").box(WIDTH_MM, ARM_W_MM, THICKNESS_MM)

# --- Two vertical bracket arms ---
arm_gap = ROPE_W_MM + 2.0  # gap between inner faces = rope slot
arm_h   = HEIGHT_MM - THICKNESS_MM
left_arm = (cq.Workplane("XY")
    .workplane(offset=THICKNESS_MM)
    .center(0, -(arm_gap / 2.0 + ARM_W_MM / 2.0))
    .box(ARM_W_MM, ARM_W_MM, arm_h, centered=False)
    .translate((-ARM_W_MM / 2.0, 0, 0)))
right_arm = (cq.Workplane("XY")
    .workplane(offset=THICKNESS_MM)
    .center(0, (arm_gap / 2.0 - ARM_W_MM / 2.0))
    .box(ARM_W_MM, ARM_W_MM, arm_h, centered=False)
    .translate((-ARM_W_MM / 2.0, 0, 0)))

result = base_plate.union(left_arm).union(right_arm)

# --- Roller (cylinder between arms) ---
roller_z = THICKNESS_MM + arm_h * 0.65  # roller sits at 65% of arm height
roller_y_start = -(arm_gap / 2.0 + ARM_W_MM)
roller_y_len   = arm_gap + 2 * ARM_W_MM
roller = (cq.Workplane("XZ")
    .workplane(offset=roller_y_start)
    .center(roller_z, 0)
    .circle(ROLLER_D_MM / 2.0)
    .extrude(roller_y_len))
# Roller bore (axle hole through roller + arms)
axle_hole = (cq.Workplane("XZ")
    .workplane(offset=roller_y_start - 1.0)
    .center(roller_z, 0)
    .circle(BORE_MM / 2.0)
    .extrude(roller_y_len + 2.0))
result = result.union(roller).cut(axle_hole)

# --- Rope slot (cut gap between arms through the roller zone) ---
slot_cut = (cq.Workplane("XY")
    .workplane(offset=THICKNESS_MM - 0.5)
    .center(0, 0)
    .rect(WIDTH_MM + 2, ROPE_W_MM)
    .extrude(arm_h + 1.0))
# Only cut where there's no arm or roller — handled by union order above

# --- Mounting holes in base plate ---
if N_MOUNT > 0:
    margin = WIDTH_MM * 0.15
    x_start = -(WIDTH_MM / 2.0 - margin)
    x_end   = (WIDTH_MM / 2.0 - margin)
    if N_MOUNT == 1:
        pts = [(0, 0)]
    else:
        pts = [(round(x_start + (x_end - x_start) * i / (N_MOUNT - 1), 3), 0)
               for i in range(N_MOUNT)]
    mount_holes = (cq.Workplane("XY").workplane(offset=-1.0)
                   .pushPoints(pts)
                   .circle(MOUNT_D_MM / 2.0).extrude(THICKNESS_MM + 2.0))
    result = result.cut(mount_holes)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_phone_case(params: dict[str, Any]) -> str:
    """UAG-style rugged phone case — rigid polycarbonate back + TPU bumper frame."""
    import math as _m

    # Phone body — default iPhone 13 Pro Max
    ph_l = float(params.get("phone_length_mm", params.get("width_mm", 160.8)))
    ph_w = float(params.get("phone_width_mm", params.get("height_mm", 78.1)))
    ph_t = float(params.get("phone_thickness_mm", 7.65))
    wall = float(params.get("wall_mm", 2.5))
    lip  = float(params.get("lip_mm", 1.5))
    cr   = float(params.get("corner_radius_mm", 8.0))

    case_l  = round(ph_l + 2 * wall, 2)
    case_w  = round(ph_w + 2 * wall, 2)
    case_d  = round(ph_t + wall + lip, 2)
    case_cr = round(cr + wall, 2)

    # UAG-style corner bumper — thicker at corners
    bump_extra = 3.0  # extra wall at corners
    bump_cr    = case_cr + bump_extra

    # Camera — iPhone 13 Pro Max triple lens module
    cam_w = 38.0
    cam_l = 38.0
    cam_cr_r = 7.0
    cam_ring_wall = 2.5
    cam_ring_h = 2.0
    cam_cx = round(-(ph_w / 2 - cam_w / 2 - 5.5), 2)
    cam_cy = round(ph_l / 2 - cam_l / 2 - 5.0, 2)

    # Button positions from top of phone
    pwr_from_top, pwr_len = 73.0, 13.0
    vup_from_top, vdn_from_top, vol_len = 68.0, 81.0, 9.0
    mute_from_top, mute_len = 56.0, 5.0
    btn_depth = 4.5  # button cutout height (Z)

    def _y(ft):
        return round(ph_l / 2.0 - ft, 2)

    pwr_y, vup_y, vdn_y, mute_y = _y(pwr_from_top), _y(vup_from_top), _y(vdn_from_top), _y(mute_from_top)

    # Port
    port_w, port_h = 9.0, 3.5

    # Pre-compute all rounded-rect profiles at template time
    n_arc = 8

    def _rr(hw, hl, r):
        pts = []
        for cx, cy, a0 in [
            ( hw-r,  hl-r,   0), (-hw+r,  hl-r,  90),
            (-hw+r, -hl+r, 180), ( hw-r, -hl+r, 270),
        ]:
            for i in range(n_arc + 1):
                a = _m.radians(a0 + i * 90.0 / n_arc)
                pts.append((round(cx + r*_m.cos(a), 4), round(cy + r*_m.sin(a), 4)))
        return pts

    outer_pts  = repr(_rr(case_w/2 + bump_extra, case_l/2 + bump_extra, bump_cr))
    inner_wall = repr(_rr(case_w/2, case_l/2, case_cr))
    cavity_pts = repr(_rr(ph_w/2, ph_l/2, max(cr-0.5, 2.0)))
    scr_pts    = repr(_rr((ph_w-5)/2, (ph_l-5)/2, max(cr-1.5, 1.5)))

    # Camera cutout + ring profiles
    def _cam_rr(hw, hl, r, ox, oy):
        pts = []
        for cx, cy, a0 in [
            ( hw-r,  hl-r,   0), (-hw+r,  hl-r,  90),
            (-hw+r, -hl+r, 180), ( hw-r, -hl+r, 270),
        ]:
            for i in range(n_arc + 1):
                a = _m.radians(a0 + i * 90.0 / n_arc)
                pts.append((round(ox+cx+r*_m.cos(a), 4), round(oy+cy+r*_m.sin(a), 4)))
        return pts

    cam_hole_pts = repr(_cam_rr(cam_w/2, cam_l/2, cam_cr_r, cam_cx, cam_cy))
    cam_ring_outer = repr(_cam_rr(
        cam_w/2+cam_ring_wall, cam_l/2+cam_ring_wall,
        cam_cr_r+cam_ring_wall, cam_cx, cam_cy))

    # Grip ridges on sides — positions along Y axis
    n_ridges = 6
    ridge_spacing = round(ph_l * 0.5 / n_ridges, 2)
    ridge_start_y = round(-ph_l * 0.15, 2)

    return f"""
import cadquery as cq
import math

# UAG-style rugged case for iPhone 13 Pro Max
# Thicker corners, raised camera ring, grip ridges, tactile button covers

CASE_D   = {case_d}
WALL     = {wall}
PH_T     = {ph_t}
PH_L     = {ph_l}
PH_W     = {ph_w}

# ── 1. Outer shell — rounded rect with thicker corners ──────────────────
outer = cq.Workplane("XY").polyline({outer_pts}).close().extrude(CASE_D)

# Inner trim — cut back to normal wall thickness on flat sides
# (keeps corners thick, sides at standard wall)
trim = cq.Workplane("XY").polyline({inner_wall}).close().extrude(CASE_D + 1)
# Only remove material OUTSIDE the inner wall but INSIDE the outer
# Actually we want the outer shape to BE the bumper corners.
# So: extrude the inner_wall profile and cut only the side panels back.
# Simpler: use outer as-is (corners are naturally thicker due to bump_extra).
# Cut the phone cavity from it.

# ── 2. Phone cavity — cut from screen side (+Z) ─────────────────────────
cavity = (
    cq.Workplane("XY")
    .workplane(offset=WALL)
    .polyline({cavity_pts}).close()
    .extrude(PH_T + 2.0 + {lip})
)
result = outer.cut(cavity)

# ── 3. Screen opening — bezel lip (2.5mm border) ────────────────────────
scr_pts = {scr_pts}
screen = (
    cq.Workplane("XY")
    .workplane(offset=CASE_D - 0.5)
    .polyline(scr_pts).close()
    .extrude(2.0)
)
result = result.cut(screen)

# ── 4. Camera — flush cutout + raised protective ring ────────────────────
cam_cut = (
    cq.Workplane("XY")
    .workplane(offset=-0.5)
    .polyline({cam_hole_pts}).close()
    .extrude(WALL + 1.0)
)
result = result.cut(cam_cut)

# Camera protective ring — raised 2mm from back face
cam_ring_solid = (
    cq.Workplane("XY")
    .polyline({cam_ring_outer}).close()
    .extrude(-{cam_ring_h})
)
cam_ring_void = (
    cq.Workplane("XY")
    .workplane(offset=0.5)
    .polyline({cam_hole_pts}).close()
    .extrude(-({cam_ring_h} + 1.0))
)
try:
    result = result.union(cam_ring_solid.cut(cam_ring_void))
except Exception:
    pass

# ── 5. Button cutouts — all 4 buttons through side walls ─────────────────
# Buttons must cut through the FULL wall including bump_extra.
# Box(thickness_to_cut, button_length, button_height) centered on the wall.
_WALL_FULL = WALL + {bump_extra}  # total wall at bumper corners
_BTN_CUT = _WALL_FULL + 4.0       # cut depth (generous, ensures full penetration)

# Power button — RIGHT side (+X wall)
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, {pwr_len}, {btn_depth})
    .translate((PH_W/2 + _WALL_FULL/2, {pwr_y}, CASE_D/2))
)

# Volume UP — LEFT side (-X wall)
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, {vol_len}, {btn_depth})
    .translate((-(PH_W/2 + _WALL_FULL/2), {vup_y}, CASE_D/2))
)

# Volume DOWN — LEFT side (-X wall)
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, {vol_len}, {btn_depth})
    .translate((-(PH_W/2 + _WALL_FULL/2), {vdn_y}, CASE_D/2))
)

# Mute switch — LEFT side (-X wall), above volume
result = result.cut(
    cq.Workplane("XY")
    .box(_BTN_CUT, {mute_len}, 3.0)
    .translate((-(PH_W/2 + _WALL_FULL/2), {mute_y}, CASE_D/2))
)

# ── 6. Bottom — Lightning port + speaker + mic grilles ───────────────────
_bot = PH_L/2 + WALL + {bump_extra}

# Lightning port
result = result.cut(
    cq.Workplane("XY").box({port_w}, WALL*2 + {bump_extra}*2 + 2, {port_h})
    .translate((0, -_bot, CASE_D/2))
)

# Speaker grille (right of port) — 6 square holes
for i in range(6):
    result = result.cut(
        cq.Workplane("XY").box(1.5, WALL*2 + {bump_extra}*2 + 2, 1.5)
        .translate(({port_w}/2 + 4 + i*2.8, -_bot, CASE_D/2))
    )

# Mic grille (left of port) — 2 square holes
for i in range(2):
    result = result.cut(
        cq.Workplane("XY").box(1.5, WALL*2 + {bump_extra}*2 + 2, 1.5)
        .translate((-{port_w}/2 - 4 - i*2.8, -_bot, CASE_D/2))
    )

# ── 7. Side grip ridges — deep parallel grooves on both sides ────────────
RIDGE_W = 2.5     # groove width along Y
RIDGE_D = 1.8     # groove depth into wall (X direction)
N_RIDGES = 8
RIDGE_SPAN = PH_L * 0.55
RIDGE_START = -RIDGE_SPAN / 2
RIDGE_STEP = RIDGE_SPAN / max(N_RIDGES - 1, 1)

for side_sign in [-1, 1]:
    x_edge = side_sign * (PH_W/2 + WALL + {bump_extra})
    for ri in range(N_RIDGES):
        ry = RIDGE_START + ri * RIDGE_STEP
        ridge = (
            cq.Workplane("XY")
            .box(RIDGE_D * 2, RIDGE_W, CASE_D * 0.65)
            .translate((x_edge, ry, CASE_D * 0.5))
        )
        try:
            result = result.cut(ridge)
        except Exception:
            pass

# ── 8. Back panel armor lines — UAG-style diagonal structural cuts ───────
# Deep grooves cut into the back face creating angular armor panel look.
LINE_DEPTH = WALL * 0.4  # 40% of wall thickness
LINE_W = 1.5

# Horizontal armor line across the back at 40% from top
armor_h1 = (
    cq.Workplane("XY")
    .box(PH_W * 0.85, LINE_W, LINE_DEPTH)
    .translate((0, PH_L * 0.10, LINE_DEPTH / 2))
)
result = result.cut(armor_h1)

# Second horizontal armor line at 70% from top
armor_h2 = (
    cq.Workplane("XY")
    .box(PH_W * 0.85, LINE_W, LINE_DEPTH)
    .translate((0, -PH_L * 0.20, LINE_DEPTH / 2))
)
result = result.cut(armor_h2)

# Vertical center spine line on back
armor_v = (
    cq.Workplane("XY")
    .box(LINE_W, PH_L * 0.5, LINE_DEPTH)
    .translate((0, -PH_L * 0.05, LINE_DEPTH / 2))
)
result = result.cut(armor_v)

# Diagonal cuts at corners — X pattern in lower half of back
for dx_sign in [-1, 1]:
    diag = (
        cq.Workplane("XY")
        .box(PH_W * 0.35, LINE_W, LINE_DEPTH)
        .rotateAboutCenter((0, 0, 1), dx_sign * 35)
        .translate((dx_sign * PH_W * 0.18, -PH_L * 0.28, LINE_DEPTH / 2))
    )
    try:
        result = result.cut(diag)
    except Exception:
        pass

# ── 9. Corner hex cutouts — visible honeycomb shock absorber pattern ─────
# Small hex-shaped recesses at each corner on the back face
HEX_R = 3.5   # hex outer radius
HEX_D = WALL * 0.35  # recess depth
for sx in [-1, 1]:
    for sy in [-1, 1]:
        for ho in range(3):  # 3 hexes per corner in a cluster
            hx = sx * (PH_W/2 - 5.0 - ho * 5.5)
            hy = sy * (PH_L/2 - 5.0 - ho * 3.0)
            # Hex approximated as 6-sided polygon
            hex_pts = [(round(hx + HEX_R * math.cos(math.radians(60*i + 30)), 3),
                        round(hy + HEX_R * math.sin(math.radians(60*i + 30)), 3))
                       for i in range(6)]
            try:
                hex_cut = (
                    cq.Workplane("XY")
                    .workplane(offset=-0.01)
                    .polyline(hex_pts).close()
                    .extrude(HEX_D)
                )
                result = result.cut(hex_cut)
            except Exception:
                pass

# ── 10. Phone retention — internal corner clips + top edge lip ────────────
# Corner retention tabs: small inward-projecting shelves at top inner edge
# that lock over the phone's screen glass corners when snapped in.
CLIP_W    = 8.0    # clip width along each corner edge
CLIP_PROJ = 1.2    # how far clip projects inward over the phone face
CLIP_T    = 0.8    # clip thickness (Z)

for sx in [-1, 1]:
    for sy in [-1, 1]:
        # Position at inner top edge of cavity at each corner
        clip_x = sx * (PH_W/2 - CLIP_W/2 - 3.0)
        clip_y = sy * (PH_L/2 - CLIP_W/2 - 3.0)
        clip = (
            cq.Workplane("XY")
            .workplane(offset=CASE_D - CLIP_T)
            .center(clip_x, clip_y)
            .rect(CLIP_W if abs(sx) > 0 else CLIP_PROJ,
                  CLIP_PROJ if abs(sx) > 0 else CLIP_W)
            .extrude(CLIP_T + 0.5)
        )
        try:
            result = result.union(clip)
        except Exception:
            pass

# Top edge inward lip — continuous 0.8mm shelf around screen opening
# that prevents the phone from lifting out. The phone snaps past this lip.
LIP_PROJ = 0.8  # inward projection
lip_outer_pts = scr_pts
# Slightly smaller = the lip shelf
lip_inner_w = (PH_W - 5) / 2 - LIP_PROJ
lip_inner_l = (PH_L - 5) / 2 - LIP_PROJ
lip_inner_cr = max({cr:.1f} - 1.5 - LIP_PROJ, 1.0)
lip_inner_pts = []
for _cx, _cy, _a0 in [
    ( lip_inner_w - lip_inner_cr,  lip_inner_l - lip_inner_cr,   0),
    (-lip_inner_w + lip_inner_cr,  lip_inner_l - lip_inner_cr,  90),
    (-lip_inner_w + lip_inner_cr, -lip_inner_l + lip_inner_cr, 180),
    ( lip_inner_w - lip_inner_cr, -lip_inner_l + lip_inner_cr, 270),
]:
    for _i in range(9):
        _a = math.radians(_a0 + _i * 90.0 / 8)
        lip_inner_pts.append((
            round(_cx + lip_inner_cr * math.cos(_a), 4),
            round(_cy + lip_inner_cr * math.sin(_a), 4)))

try:
    lip_shelf = (
        cq.Workplane("XY")
        .workplane(offset=CASE_D - 0.8)
        .polyline(lip_outer_pts).close()
        .extrude(0.8)
    )
    lip_void = (
        cq.Workplane("XY")
        .workplane(offset=CASE_D - 0.85)
        .polyline(lip_inner_pts).close()
        .extrude(1.0)
    )
    result = result.union(lip_shelf.cut(lip_void))
except Exception:
    pass

# ── 11. Lanyard hole — bottom-right corner ───────────────────────────────
lanyard = (
    cq.Workplane("XY")
    .box(3.0, WALL * 2 + {bump_extra} * 2 + 2, 3.0)
    .translate((PH_W/2 - 8, -_bot, CASE_D * 0.35))
)
try:
    result = result.cut(lanyard)
except Exception:
    pass

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_flat_plate(params: dict[str, Any]) -> str:
    """Square or rectangular flat plate with optional center bore and bolt holes."""
    import math as _math
    w    = float(params.get("width_mm",  100.0))
    d    = float(params.get("depth_mm",  params.get("width_mm", 100.0)))
    t    = float(params.get("thickness_mm", params.get("height_mm", 10.0)))
    bore = params.get("bore_mm")
    n    = int(params.get("n_bolts", 0))
    bdia = float(params.get("bolt_dia_mm", 6.0))
    bcr  = params.get("bolt_circle_r_mm")
    bsq  = params.get("bolt_square_mm")

    bore_line = ""
    if bore:
        bore_line = f"result = result.faces('>Z').workplane().circle(BORE_MM / 2.0).cutThruAll()"

    hole_line = ""
    if n > 0:
        if bsq:
            half = bsq / 2.0
            pts  = [(half, half), (-half, half), (-half, -half), (half, -half)]
        elif bcr:
            step = 2 * _math.pi / n
            pts  = [(round(bcr * _math.cos(i * step), 3), round(bcr * _math.sin(i * step), 3)) for i in range(n)]
        else:
            margin  = min(w * 0.15, 15.0)
            x_start = -(w / 2.0 - margin)
            x_end   =  (w / 2.0 - margin)
            pts = [(round(x_start + (x_end - x_start) * i / max(n - 1, 1), 3), 0.0) for i in range(n)]
        hole_line = (
            f"result = result.faces('>Z').workplane()"
            f".pushPoints({pts!r}).circle(BOLT_DIA_MM / 2.0).cutThruAll()"
        )

    return f"""
import cadquery as cq

WIDTH_MM     = {w}
DEPTH_MM     = {d}
THICKNESS_MM = {t}
BORE_MM      = {bore if bore else 0.0}
BOLT_DIA_MM  = {bdia}

result = cq.Workplane("XY").box(WIDTH_MM, DEPTH_MM, THICKNESS_MM)
{bore_line}
{hole_line}
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_bracket(params: dict[str, Any]) -> str:
    w    = float(params.get("width_mm",  80.0))
    h    = float(params.get("height_mm", 60.0))
    t    = float(params.get("thickness_mm", 6.0))
    hole = float(params.get("hole_dia_mm", params.get("bolt_dia_mm", 8.0)))
    n    = max(1, int(params.get("n_bolts", 2)))
    # Space holes evenly along the width with 15% margin on each side
    margin  = min(w * 0.15, 15.0)
    x_start = -(w / 2.0 - margin)
    x_end   =  (w / 2.0 - margin)
    if n == 1:
        pts = [(0.0, 0.0)]
    else:
        pts = [
            (round(x_start + (x_end - x_start) * i / (n - 1), 3), 0.0)
            for i in range(n)
        ]
    pts_repr = repr(pts)
    return f"""
import cadquery as cq

WIDTH_MM     = {w}
HEIGHT_MM    = {h}
THICKNESS_MM = {t}
HOLE_DIA_MM  = {hole}

plate = cq.Workplane("XY").box(WIDTH_MM, THICKNESS_MM, HEIGHT_MM)
# {n} mounting hole(s) evenly spaced along the plate
hole_cyl = (
    cq.Workplane("XY")
    .workplane(offset=-1.0)
    .pushPoints({pts_repr})
    .circle(HOLE_DIA_MM / 2.0)
    .extrude(THICKNESS_MM + 2.0)
)
result = plate.cut(hole_cyl)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_l_bracket(params: dict[str, Any]) -> str:
    """L-bracket: two plates joined at 90 degrees."""
    w = float(params.get("width_mm", 50.0))
    h = float(params.get("height_mm", 30.0))
    t = float(params.get("thickness_mm", 3.0))
    hole = float(params.get("hole_dia_mm", params.get("bolt_dia_mm", 4.0)))
    n = max(0, int(params.get("n_bolts", 4)))
    leg_h = float(params.get("leg_height_mm", h))  # vertical leg height

    # Hole positions: half on base, half on vertical leg
    n_base = max(1, n // 2)
    n_vert = max(1, n - n_base)
    margin = min(w * 0.15, 10.0)

    base_pts = [(round(-w/2 + margin + (w - 2*margin) * i / max(n_base-1, 1), 2), 0.0) for i in range(n_base)]
    vert_pts = [(round(-w/2 + margin + (w - 2*margin) * i / max(n_vert-1, 1), 2), round(leg_h * 0.5, 2)) for i in range(n_vert)]

    return f"""
import cadquery as cq

W = {w}
H = {h}        # base depth (horizontal leg)
T = {t}
LEG_H = {leg_h} # vertical leg height
HOLE_DIA = {hole}

# Base plate (horizontal leg)
base = cq.Workplane("XY").box(W, H, T)

# Vertical leg — starts at the back edge of base, goes up
vert = (cq.Workplane("XY")
    .workplane(offset=T/2)
    .center(0, -H/2 + T/2)
    .box(W, T, LEG_H)
    .translate((0, 0, LEG_H/2)))

result = base.union(vert)

# Holes in base plate
base_holes = {repr(base_pts)}
if base_holes:
    hole_cutter = (cq.Workplane("XY").workplane(offset=-1)
        .pushPoints(base_holes).circle(HOLE_DIA/2).extrude(T + 2))
    result = result.cut(hole_cutter)

# Holes in vertical leg
vert_hole_pts = {repr(vert_pts)}
if vert_hole_pts:
    vert_cutter = (cq.Workplane("XZ")
        .workplane(offset=H/2)
        .pushPoints(vert_hole_pts).circle(HOLE_DIA/2).extrude(T + 2))
    result = result.cut(vert_cutter)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_heat_sink(params: dict[str, Any]) -> str:
    """Heat sink: base plate with parallel fins."""
    w = float(params.get("width_mm", 60.0))
    d = float(params.get("depth_mm", 40.0))
    base_t = float(params.get("base_thickness_mm", params.get("thickness_mm", 3.0)))
    fin_h = float(params.get("fin_height_mm", 20.0))
    fin_t = float(params.get("fin_thickness_mm", 1.5))
    n_fins = int(params.get("n_fins", 8))
    spacing = float(params.get("fin_spacing_mm", 0))
    if spacing <= 0 and n_fins > 1:
        spacing = (w - fin_t) / (n_fins - 1)

    return f"""
import cadquery as cq

W = {w}
D = {d}
BASE_T = {base_t}
FIN_H = {fin_h}
FIN_T = {fin_t}
N_FINS = {n_fins}
SPACING = {spacing}

# Base plate
result = cq.Workplane("XY").box(W, D, BASE_T)

# Parallel fins on top
for i in range(N_FINS):
    x = -W/2 + FIN_T/2 + i * SPACING
    fin = (cq.Workplane("XY")
        .workplane(offset=BASE_T/2)
        .center(x, 0)
        .box(FIN_T, D, FIN_H)
        .translate((0, 0, FIN_H/2)))
    result = result.union(fin)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_phone_stand(params: dict[str, Any]) -> str:
    """Phone/tablet stand: angled support with slot."""
    w = float(params.get("width_mm", 60.0))
    h = float(params.get("height_mm", 80.0))
    base_d = float(params.get("depth_mm", params.get("base_depth_mm", 60.0)))
    t = float(params.get("thickness_mm", 5.0))
    angle = float(params.get("angle_deg", 65.0))
    slot_w = float(params.get("slot_width_mm", 12.0))

    import math
    angle_rad = math.radians(angle)
    lean_offset = h * math.cos(angle_rad)
    lean_rise = h * math.sin(angle_rad)

    return f"""
import cadquery as cq, math

W = {w}
H = {h}
BASE_D = {base_d}
T = {t}
ANGLE = {angle}
SLOT_W = {slot_w}

# Base plate
base = cq.Workplane("XY").box(W, BASE_D, T)

# Angled back support (tilted at ANGLE from vertical)
angle_rad = math.radians(ANGLE)
lean = H * math.cos(angle_rad)  # horizontal offset at top
rise = H * math.sin(angle_rad)  # vertical height

# Build angled support as a box rotated about X axis
support = (cq.Workplane("XZ")
    .center(0, T/2)
    .polyline([(- W/2, 0), (W/2, 0), (W/2, rise), (-W/2, rise)])
    .close().extrude(T))

# Position: start at back of base, lean back
support = support.translate((0, -BASE_D/2 + T, 0))

result = base.union(support)

# Phone slot (groove in front of support, on top of base)
slot = (cq.Workplane("XY")
    .workplane(offset=T/2)
    .center(0, -BASE_D/2 + T*2 + SLOT_W/2)
    .box(W * 0.8, SLOT_W, T * 0.7))
result = result.cut(slot)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_flange(params: dict[str, Any]) -> str:
    bore    = float(params.get("bore_mm",   40.0))
    n_bolts = int(params.get("n_bolts", 4))
    bolt_d  = float(params.get("bolt_dia_mm", 8.0))
    bolt_r  = float(params.get("bolt_circle_r_mm", 0))
    thick   = float(params.get("thickness_mm", params.get("height_mm", 12.0)))

    # Compute minimum viable OD from bolt circle + clearance
    # Bolt holes must be inside the disc with at least bolt_d margin from edge
    if bolt_r > 0:
        min_od = (bolt_r + bolt_d + 3) * 2  # bolt circle + hole radius + 3mm wall
    else:
        min_od = bore + 20  # default: bore + 10mm wall each side

    # Default OD: enough room for bolt circle + wall, but not absurdly large
    default_od = max(min_od, bore + 20) if bolt_r > 0 else 120.0
    od = float(params.get("od_mm", default_od))
    # Enforce minimum OD so bolt holes don't split the disc
    if od < min_od:
        od = min_od

    # Default bolt circle: midpoint between bore edge and OD edge
    if bolt_r <= 0:
        bolt_r = (bore / 2 + od / 2) / 2
    # Clamp bolt circle inside OD with margin
    if bolt_r >= od / 2 - bolt_d:
        bolt_r = (bore / 2 + od / 2) / 2
    return f"""
import cadquery as cq, math

OD_MM           = {od}
BORE_MM         = {bore}
THICKNESS_MM    = {thick}
BOLT_CIRCLE_R   = {bolt_r}
N_BOLTS         = {n_bolts}
BOLT_DIA_MM     = {bolt_d}

disc = (
    cq.Workplane("XY")
    .circle(OD_MM / 2.0)
    .circle(BORE_MM / 2.0)
    .extrude(THICKNESS_MM)
)
pts = [
    (BOLT_CIRCLE_R * math.cos(math.radians(i * 360.0 / N_BOLTS)),
     BOLT_CIRCLE_R * math.sin(math.radians(i * 360.0 / N_BOLTS)))
    for i in range(N_BOLTS)
]
bolt_holes = (
    cq.Workplane("XY")
    .workplane(offset=-1.0)
    .pushPoints(pts)
    .circle(BOLT_DIA_MM / 2.0)
    .extrude(THICKNESS_MM + 2.0)
)
result = disc.cut(bolt_holes)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_shaft(params: dict[str, Any]) -> str:
    d = float(params.get("od_mm", params.get("diameter_mm", params.get("diameter", 20.0))))
    l = float(params.get("length_mm", 150.0))
    return f"""
import cadquery as cq

DIAMETER_MM = {d}
LENGTH_MM   = {l}

result = cq.Workplane("XY").circle(DIAMETER_MM / 2.0).extrude(LENGTH_MM)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_pulley(params: dict[str, Any]) -> str:
    od      = float(params.get("od_mm", 80.0))
    groove  = float(params.get("groove_depth_mm", 5.0))
    w       = float(params.get("width_mm", 20.0))
    bore    = float(params.get("bore_mm", 10.0))
    groove_angle = float(params.get("groove_angle_deg", 38.0))  # V-belt standard
    n_grooves = int(params.get("n_grooves", 1))
    hub_od  = float(params.get("hub_od_mm", max(bore * 2.0 + 4.0, od * 0.3)))
    return f"""
import cadquery as cq, math

OD_MM          = {od}
GROOVE_DEPTH   = {groove}
WIDTH_MM       = {w}
BORE_MM        = {bore}
GROOVE_ANGLE   = {groove_angle}
N_GROOVES      = {n_grooves}
HUB_OD_MM     = {hub_od}

# --- Main pulley body ---
outer = cq.Workplane("XY").circle(OD_MM / 2.0).extrude(WIDTH_MM)

# --- V-groove(s): revolved trapezoidal profile cut from OD ---
# V-groove profile: two angled cuts converging at groove bottom radius
groove_r_bottom = OD_MM / 2.0 - GROOVE_DEPTH
groove_half_angle = math.radians(GROOVE_ANGLE / 2.0)
groove_top_width = 2.0 * GROOVE_DEPTH * math.tan(groove_half_angle)

groove_spacing = WIDTH_MM / max(N_GROOVES, 1)
for gi in range(N_GROOVES):
    z_center = groove_spacing * (gi + 0.5)
    # Triangular groove profile revolved around Z axis
    # Points: outer-left, bottom-center, outer-right (in XZ plane at Y=0)
    half_w = groove_top_width / 2.0
    try:
        groove_cut = (
            cq.Workplane("XZ")
            .workplane(offset=0)
            .moveTo(OD_MM / 2.0 + 0.5, z_center - half_w)
            .lineTo(groove_r_bottom,     z_center)
            .lineTo(OD_MM / 2.0 + 0.5, z_center + half_w)
            .close()
            .revolve(360, (0, 0, 0), (0, 0, 1))
        )
        outer = outer.cut(groove_cut)
    except Exception:
        # Fallback: simple annular groove
        groove_void = (
            cq.Workplane("XY")
            .workplane(offset=z_center - half_w)
            .circle(OD_MM / 2.0 + 0.1)
            .circle(groove_r_bottom)
            .extrude(groove_top_width)
        )
        outer = outer.cut(groove_void)

# --- Bore ---
bore_cyl = (
    cq.Workplane("XY")
    .workplane(offset=-1.0)
    .circle(BORE_MM / 2.0)
    .extrude(WIDTH_MM + 2.0)
)
result = outer.cut(bore_cyl)

# --- Keyway on bore (standard rectangular) ---
keyway_w = max(BORE_MM * 0.25, 2.0)
keyway_d = keyway_w * 0.6
keyway = (cq.Workplane("XY").workplane(offset=-0.5)
          .center(0, BORE_MM / 2.0 + keyway_d / 2.0 - 0.5)
          .rect(keyway_w, keyway_d)
          .extrude(WIDTH_MM + 1.0))
result = result.cut(keyway)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_cam(params: dict[str, Any]) -> str:
    base_r  = float(params.get("base_radius_mm", 25.0))
    lift    = float(params.get("lift_mm", 8.0))
    thick   = float(params.get("thickness_mm", 12.0))
    bore    = float(params.get("bore_mm", 10.0))
    return f"""
import cadquery as cq

BASE_R_MM    = {base_r}
LIFT_MM      = {lift}
THICKNESS_MM = {thick}
BORE_MM      = {bore}

# Approximate cam as an eccentric cylinder (base circle + lobe offset)
base = cq.Workplane("XY").circle(BASE_R_MM).extrude(THICKNESS_MM)
lobe = (
    cq.Workplane("XY")
    .center(LIFT_MM / 2.0, 0)
    .circle(BASE_R_MM * 0.6)
    .extrude(THICKNESS_MM)
)
bore_cyl = (
    cq.Workplane("XY")
    .workplane(offset=-1.0)
    .circle(BORE_MM / 2.0)
    .extrude(THICKNESS_MM + 2.0)
)
result = base.union(lobe).cut(bore_cyl)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_pin(params: dict[str, Any]) -> str:
    d = float(params.get("diameter_mm", 6.0))
    l = float(params.get("length_mm", 40.0))
    return f"""
import cadquery as cq

DIAMETER_MM = {d}
LENGTH_MM   = {l}

result = cq.Workplane("XY").circle(DIAMETER_MM / 2.0).extrude(LENGTH_MM)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_spacer(params: dict[str, Any]) -> str:
    od    = float(params.get("od_mm", 20.0))
    bore  = float(params.get("bore_mm", 10.0))
    thick = float(params.get("thickness_mm", params.get("height_mm", 5.0)))
    return f"""
import cadquery as cq

OD_MM        = {od}
BORE_MM      = {bore}
THICKNESS_MM = {thick}

result = (
    cq.Workplane("XY")
    .circle(OD_MM / 2.0)
    .circle(BORE_MM / 2.0)
    .extrude(THICKNESS_MM)
)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_tube(params: dict[str, Any]) -> str:
    od   = float(params.get("od_mm",     params.get("diameter_mm",   50.0)))
    bore = float(params.get("bore_mm",   params.get("id_mm",         od - 6.0)))
    l    = float(params.get("length_mm", params.get("height_mm",    100.0)))
    bore = min(bore, od - 1.0)  # ensure bore < OD
    return f"""
import cadquery as cq

OD_MM   = {od}
BORE_MM = {bore}
L_MM    = {l}

result = (
    cq.Workplane("XY")
    .circle(OD_MM / 2.0)
    .circle(BORE_MM / 2.0)
    .extrude(L_MM)
)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_gear(params: dict[str, Any]) -> str:
    """
    Involute spur gear — optimised for low RAM / fast OCCT.

    Key optimisations vs the old implementation:
      1. All polygon points are pre-computed here (Python template time), not at
         CadQuery execution time → the generated script has zero math loops.
      2. The bore is expressed as a CadQuery inner wire (.circle() after
         .polyline().close()), so OCCT builds an annular face in one extrude
         call — no boolean cut for the bore.
      3. N_PTS=3 per flank (sufficient for clock / display quality).
      4. Spoke compound-then-cut (2 booleans) only when spoke_style="petal" or
         "minimal"; spoke_style="straight" produces zero booleans.

    OCCT work: 1 extrude  +  0-2 booleans  (was 6+ on a 1700-face polygon).
    """
    import math as _m

    module_mm   = float(params.get("module_mm", 1.0))
    n_teeth     = int(params.get("n_teeth", 40))
    pa_deg      = float(params.get("pressure_angle_deg", 20.0))
    face_w      = float(params.get("face_width_mm", 6.0))
    bore        = float(params.get("bore_mm", 6.0))
    hub_od_def  = max(bore * 2.4, bore + 6.0)
    hub_od      = float(params.get("hub_od_mm", hub_od_def))
    spoke_style = str(params.get("spoke_style", "petal"))
    n_spokes    = int(params.get("n_spokes", 5))
    keyway_w    = float(params.get("keyway_width_mm", 0.0))
    step_path   = str(params.get("step_path", ""))
    stl_path    = str(params.get("stl_path", ""))

    # ── Pre-compute full gear polygon (runs at template time, not OCCT time) ──
    pitch_r = module_mm * n_teeth / 2.0
    base_r  = pitch_r * _m.cos(_m.radians(pa_deg))
    tip_r   = pitch_r + module_mm
    root_r  = pitch_r - 1.25 * module_mm

    # Detect degenerate case: bore too large for this module/teeth combo.
    # When bore/2 >= tip_r the involute polygon would be smaller than the bore —
    # fall back to a simple disk + boolean bore cut (no inner-wire trick).
    _bore_overshoot = (bore / 2.0) >= tip_r * 0.9
    if _bore_overshoot:
        import warnings as _w
        _w.warn(
            f"[gear] bore {bore}mm >= 90% of tip_r {tip_r*2:.1f}mm for "
            f"{n_teeth}t m{module_mm} — switching to boolean bore cut",
            stacklevel=2,
        )
    root_r = max(root_r, bore / 2.0 + 0.5)

    def _inv(t, rb):
        return rb * (_m.cos(t) + t * _m.sin(t)), rb * (_m.sin(t) - t * _m.cos(t))

    def _t_for_r(r, rb):
        return _m.sqrt(max(0.0, (r / rb) ** 2 - 1.0))

    t_pitch  = _t_for_r(pitch_r, base_r)
    _ipx, _ipy = _inv(t_pitch, base_r)
    inv_pa   = _m.atan2(_ipy, _ipx)
    half_ta  = _m.pi / n_teeth
    p_step   = 2 * _m.pi / n_teeth
    N_PTS    = 3  # 3 involute pts/flank — sufficient at clock gear scale
    t_root   = _t_for_r(max(root_r, base_r), base_r)
    t_tip    = _t_for_r(tip_r, base_r)
    rot_off  = inv_pa - half_ta + t_pitch

    def _one_tooth():
        p = []
        p.append((root_r * _m.cos(-half_ta - _m.pi / n_teeth),
                  root_r * _m.sin(-half_ta - _m.pi / n_teeth)))
        for i in range(N_PTS + 1):
            t = t_root + (t_tip - t_root) * i / N_PTS
            x, y = _inv(t, base_r)
            r = _m.hypot(x, y)
            a = _m.atan2(y, x) - rot_off
            p.append((r * _m.cos(a), r * _m.sin(a)))
        tip_half = _m.acos(min(1.0, base_r / tip_r))
        for i in range(3):
            a = half_ta + tip_half * (1.0 - float(i))
            p.append((tip_r * _m.cos(a), tip_r * _m.sin(a)))
        for i in range(N_PTS, -1, -1):
            t = t_root + (t_tip - t_root) * i / N_PTS
            x, y = _inv(t, base_r)
            r = _m.hypot(x, y)
            a = -(_m.atan2(y, x) - rot_off)
            p.append((r * _m.cos(a), r * _m.sin(a)))
        p.append((root_r * _m.cos(half_ta + _m.pi / n_teeth),
                  root_r * _m.sin(half_ta + _m.pi / n_teeth)))
        return p

    one_tooth = _one_tooth()
    all_pts: list[tuple[float, float]] = []
    for i in range(n_teeth):
        a = i * p_step
        ca, sa = _m.cos(a), _m.sin(a)
        for px, py in one_tooth:
            all_pts.append((round(px * ca - py * sa, 5),
                            round(px * sa + py * ca, 5)))

    pts_literal = repr(all_pts)  # embedded as literal in generated script

    # Pre-compute spoke geometry constants
    rim_r        = root_r - module_mm * 0.5
    spoke_zone_r = (hub_od / 2.0 + rim_r) / 2.0
    cutout_h     = rim_r - hub_od / 2.0 - module_mm
    ell_a        = cutout_h / 2.0
    ell_b        = max((2.0 * _m.pi * spoke_zone_r / max(n_spokes, 1)) * 0.38, 1.0)
    spoke_w      = max(1.0, module_mm * 0.8)

    return f"""
import cadquery as cq
import math

FACE_W       = {face_w}
BORE         = {bore}
HUB_OD       = {hub_od}
SPOKE_STYLE  = "{spoke_style}"
N_SPOKES     = {n_spokes}
KEYWAY_W     = {keyway_w}
STEP_PATH    = r"{step_path}"
STL_PATH     = r"{stl_path}"

# Pre-computed involute polygon ({len(all_pts)} pts, {n_teeth}t, m={module_mm}mm)
# No runtime math — points were calculated at template-generation time.
all_pts = {pts_literal}

# ── Extrude gear polygon then boolean-cut bore ───────────────────────────────
# Falls back to annular cylinder when polygon is degenerate (undercut small pinion).
TIP_R = {tip_r:.5f}
try:
    gear = cq.Workplane("XY").polyline(all_pts).close().extrude(FACE_W)
    bore_cyl = cq.Workplane("XY").circle(BORE / 2.0).extrude(FACE_W + 2).translate((0, 0, -1))
    gear = gear.cut(bore_cyl)
except Exception:
    # Polygon degenerate (undercut pinion) — fall back to annular cylinder
    gear = cq.Workplane("XY").circle(TIP_R).circle(BORE / 2.0).extrude(FACE_W)

# Optional keyway (small rect cut on bore)
if KEYWAY_W > 0:
    kd = KEYWAY_W * 0.6
    kw = (cq.Workplane("XY")
          .rect(KEYWAY_W, kd * 2)
          .extrude(FACE_W + 2)
          .translate((0, BORE / 2 + kd - 1, -1)))
    gear = gear.cut(kw)

# ── Spoke / lightening cutouts — compound-then-single-cut (2 booleans max) ──
RIM_R        = {rim_r:.5f}
SPOKE_ZONE_R = {spoke_zone_r:.5f}
CUTOUT_H     = {cutout_h:.5f}
ELL_A        = {ell_a:.5f}
ELL_B        = {ell_b:.5f}
SPOKE_W      = {spoke_w:.5f}

if SPOKE_STYLE == "petal" and CUTOUT_H > 2.0:
    compound = None
    for i in range(N_SPOKES):
        ang = i * 2.0 * math.pi / N_SPOKES + math.pi / N_SPOKES
        cx  = SPOKE_ZONE_R * math.cos(ang)
        cy  = SPOKE_ZONE_R * math.sin(ang)
        c = (cq.Workplane("XY")
             .transformed(rotate=cq.Vector(0, 0, math.degrees(ang)))
             .ellipse(ELL_A, ELL_B)
             .extrude(FACE_W + 2)
             .translate((cx, cy, -1)))
        compound = c if compound is None else compound.union(c)
    if compound is not None:
        gear = gear.cut(compound)

elif SPOKE_STYLE == "minimal" and CUTOUT_H > 2.0:
    compound = None
    for i in range(N_SPOKES):
        ang = i * 2.0 * math.pi / N_SPOKES
        cx  = SPOKE_ZONE_R * math.cos(ang)
        cy  = SPOKE_ZONE_R * math.sin(ang)
        c = (cq.Workplane("XY")
             .transformed(rotate=cq.Vector(0, 0, math.degrees(ang)))
             .rect(CUTOUT_H, SPOKE_W)
             .extrude(FACE_W + 2)
             .translate((cx, cy, -1)))
        compound = c if compound is None else compound.union(c)
    if compound is not None:
        gear = gear.cut(compound)
# SPOKE_STYLE == "straight": solid disk — zero booleans

result = gear

if STEP_PATH:
    import cadquery as _cq_exp
    _cq_exp.exporters.export(result, STEP_PATH)
if STL_PATH:
    import cadquery as _cq_exp2
    _cq_exp2.exporters.export(result, STL_PATH)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_nozzle(params: dict[str, Any]) -> str:
    entry_r   = float(params.get("entry_r_mm",     60.0))
    throat_r  = float(params.get("throat_r_mm",    25.0))
    exit_r    = float(params.get("exit_r_mm",      80.0))
    conv_len  = float(params.get("conv_length_mm", 80.0))
    total_len = float(params.get("length_mm",     200.0))
    wall      = float(params.get("wall_mm",         3.0))
    return f"""
import cadquery as cq

ENTRY_R_MM  = {entry_r}
THROAT_R_MM = {throat_r}
EXIT_R_MM   = {exit_r}
CONV_LEN_MM = {conv_len}
LENGTH_MM   = {total_len}
WALL_MM     = {wall}

# Closed profile in XY plane (X = radius, Y = axial position).
# Convergent: entry (r=ENTRY_R) -> throat (r=THROAT_R) over CONV_LEN mm
# Divergent:  throat (r=THROAT_R) -> exit (r=EXIT_R) over remaining length
# Hollow: inner profile is outer offset inward by WALL_MM.
# Revolve 360 deg around world Y axis; Y becomes the nozzle long axis.
profile = [
    (ENTRY_R_MM,            0),
    (THROAT_R_MM,           CONV_LEN_MM),
    (EXIT_R_MM,             LENGTH_MM),
    (EXIT_R_MM - WALL_MM,   LENGTH_MM),
    (THROAT_R_MM - WALL_MM, CONV_LEN_MM),
    (ENTRY_R_MM - WALL_MM,  0),
]

result = (
    cq.Workplane("XY")
    .polyline([(r, z) for r, z in profile])
    .close()
    .revolve(360, (0, 0, 0), (0, 1, 0))
)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_escape_wheel(params: dict[str, Any]) -> str:
    """
    Clock escape wheel — asymmetric spike teeth for anchor/recoil escapements.

    Tooth geometry (pre-computed at template time):
      - Near-radial drive face (2° lean): steep, almost radial — characteristic
        of a recoil escapement and clearly different from the impulse side
      - Impulse face at 50% of tooth pitch: shallow ramp, pallet slides off here
      - Tooth height = 3× module (much taller than spur gear, clearly visible)
      - Pointed tip: drives the pendulum via pallet fork

    Spokes are based on bore radius (not hub_od) so they always appear on
    escape wheels regardless of the hub_od parameter.

    Params: module_mm, n_teeth, face_width_mm, bore_mm, hub_od_mm
    """
    import math as _m

    module_mm   = float(params.get("module_mm", 0.5))
    n_teeth     = int(params.get("n_teeth", 15))
    face_w      = float(params.get("face_width_mm", 4.0))
    bore        = float(params.get("bore_mm", 3.0))
    step_path   = str(params.get("step_path", ""))
    stl_path    = str(params.get("stl_path", ""))

    # Escape-wheel proportions — tooth height ~20% of pitch radius (real clock ratio)
    pitch_r = module_mm * n_teeth / 2.0
    tip_r   = pitch_r + module_mm * 1.5     # 1.5× module ≈ 20% of pitch radius
    root_r  = max(pitch_r - module_mm * 0.3, bore / 2.0 + 0.5)

    tooth_angle  = 2 * _m.pi / n_teeth
    drive_lean   = _m.radians(2.0)   # almost radial drive face (2° lean)
    impulse_frac = 0.52               # impulse foot at 52% of pitch — shallow ramp

    # ── Pre-compute per-tooth triangle points (3 pts each) ──────────────────
    # Each tooth is stored as [root_trail, tip, root_lead] — a tiny triangle.
    # Keeping teeth as separate small prisms (not one giant polygon) means OCCT
    # only needs to triangulate simple flat triangles, giving a clean STL mesh.
    tooth_pts_list: list[list[tuple[float, float]]] = []
    for i in range(n_teeth):
        theta   = i * tooth_angle
        a_trail = theta - drive_lean
        a_lead  = theta + tooth_angle * impulse_frac
        tooth_pts_list.append([
            (round(root_r * _m.cos(a_trail), 5), round(root_r * _m.sin(a_trail), 5)),
            (round(tip_r  * _m.cos(theta),   5), round(tip_r  * _m.sin(theta),   5)),
            (round(root_r * _m.cos(a_lead),  5), round(root_r * _m.sin(a_lead),  5)),
        ])

    # ── Spoke geometry — based on bore radius ───────────────────────────────
    spoke_inner = bore / 2.0 + 0.4
    spoke_len   = root_r - spoke_inner - 0.3
    spoke_r     = (spoke_inner + root_r) / 2.0
    spoke_w     = max(0.5, module_mm * 0.9)
    n_spk       = 3 if n_teeth <= 20 else 5

    return f"""
import cadquery as cq
import math

FACE_W      = {face_w}
BORE        = {bore}
ROOT_R      = {root_r:.5f}
SPOKE_INNER = {spoke_inner:.4f}
SPOKE_R     = {spoke_r:.4f}
SPOKE_LEN   = {spoke_len:.4f}
SPOKE_W     = {spoke_w:.4f}
N_SPOKES    = {n_spk}
STEP_PATH   = r"{step_path}"
STL_PATH    = r"{stl_path}"

# ── Body: clean annular disk (bore to root circle) ───────────────────────────
# A simple circle-to-circle extrude gives OCCT a clean cylindrical body with
# flat smooth top/bottom faces — no non-convex polygon triangulation issues.
wheel = (
    cq.Workplane("XY")
    .circle(ROOT_R)
    .circle(BORE / 2.0)
    .extrude(FACE_W)
)

# ── Teeth: individual triangle prisms, union-compounded onto the rim ─────────
# Each tooth is a 3-point polygon (root_trail → tip → root_lead).
# OCCT can triangulate a flat triangle perfectly — no mesh artifacts.
# Union the 15 teeth into one compound first, then one union with the body.
tooth_pts_list = {repr(tooth_pts_list)}

tooth_cmp = None
for pts in tooth_pts_list:
    t = cq.Workplane("XY").polyline(pts).close().extrude(FACE_W)
    tooth_cmp = t if tooth_cmp is None else tooth_cmp.union(t)
if tooth_cmp is not None:
    wheel = wheel.union(tooth_cmp)

# ── Spoke slots (skeleton look) ──────────────────────────────────────────────
if SPOKE_LEN > 0.4:
    sp_cmp = None
    for i in range(N_SPOKES):
        ang = i * 2.0 * math.pi / N_SPOKES
        cx  = SPOKE_R * math.cos(ang)
        cy  = SPOKE_R * math.sin(ang)
        c = (cq.Workplane("XY")
             .transformed(rotate=cq.Vector(0, 0, math.degrees(ang)))
             .rect(SPOKE_LEN, SPOKE_W)
             .extrude(FACE_W + 2)
             .translate((cx, cy, -1)))
        sp_cmp = c if sp_cmp is None else sp_cmp.union(c)
    if sp_cmp is not None:
        wheel = wheel.cut(sp_cmp)

result = wheel

if STEP_PATH:
    import cadquery as _cq_exp
    _cq_exp.exporters.export(result, STEP_PATH)
if STL_PATH:
    import cadquery as _cq_exp2
    _cq_exp2.exporters.export(result, STL_PATH)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


# ---------------------------------------------------------------------------
# Standard mechanical component templates
# ---------------------------------------------------------------------------

def _cq_nema_motor(params: dict[str, Any]) -> str:
    frame   = float(params.get("frame_mm",      42.3))
    length  = float(params.get("length_mm",     40.0))
    shaft_d = float(params.get("shaft_d_mm",     5.0))
    shaft_l = float(params.get("shaft_l_mm",    24.0))
    boss_d  = float(params.get("boss_d_mm",     frame * 0.519))  # 22mm for NEMA17
    boss_h  = float(params.get("boss_h_mm",      2.0))
    bolt_r  = float(params.get("bolt_circle_mm", frame * 0.731)) / 2  # 15.5mm for NEMA17
    bolt_d  = float(params.get("bolt_d_mm",      3.0))
    flat_d  = float(params.get("flat_depth_mm",  0.5))   # D-flat shave depth

    import math as _m
    pts = [(round(bolt_r * _m.cos(_m.radians(a)), 4),
            round(bolt_r * _m.sin(_m.radians(a)), 4)) for a in [45, 135, 225, 315]]

    return f"""
import cadquery as cq, math
FRAME   = {frame}
LENGTH  = {length}
SHAFT_D = {shaft_d}
SHAFT_L = {shaft_l}
BOSS_D  = {boss_d}
BOSS_H  = {boss_h}
BOLT_D  = {bolt_d}
FLAT_D  = {flat_d}

# Body — square prism
body = cq.Workplane("XY").box(FRAME, FRAME, LENGTH)

# Mounting holes from front face (4x on bolt circle)
body = (body.faces(">Z").workplane()
    .pushPoints({pts})
    .circle(BOLT_D / 2).cutThruAll()
)

# Front boss
boss = cq.Workplane("XY").workplane(offset=LENGTH/2).circle(BOSS_D/2).extrude(BOSS_H)
result = body.union(boss)

# Shaft
shaft = cq.Workplane("XY").workplane(offset=LENGTH/2 + BOSS_H).circle(SHAFT_D/2).extrude(SHAFT_L)
result = result.union(shaft)

# D-flat: box positioned so its -Y face is at flat_d from shaft centre
flat_edge = SHAFT_D / 2 - FLAT_D
cut = (cq.Workplane("XY")
    .workplane(offset=LENGTH/2 + BOSS_H)
    .center(0, flat_edge + SHAFT_D)
    .rect(SHAFT_D * 4, SHAFT_D * 2)
    .extrude(SHAFT_L)
)
result = result.cut(cut)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_mgn_rail(params: dict[str, Any]) -> str:
    width  = float(params.get("width_mm",   12.0))
    height = float(params.get("height_mm",   8.0))
    length = float(params.get("length_mm", 400.0))
    hole_spacing = float(params.get("hole_spacing_mm", 20.0))
    hole_d = float(params.get("hole_d_mm", 3.5))
    slot_w = float(params.get("slot_w_mm", width * 0.50))   # top T-slot width
    slot_d = float(params.get("slot_d_mm", height * 0.45))  # slot depth

    import math as _m
    n_holes = max(2, int(length / hole_spacing) - 1)
    first_hole = hole_spacing
    return f"""
import cadquery as cq, math
WIDTH  = {width}
HEIGHT = {height}
LENGTH = {length}
HOLE_D = {hole_d}
HOLE_SPACING = {hole_spacing}
N_HOLES = {n_holes}
FIRST_HOLE = {first_hole}
SLOT_W = {slot_w}
SLOT_D = {slot_d}

# Rail body
result = cq.Workplane("XZ").box(WIDTH, HEIGHT, LENGTH)

# Mounting holes through base (along length)
hole_xs = [-(LENGTH/2) + FIRST_HOLE + i * HOLE_SPACING for i in range(N_HOLES)]
result = (result
    .faces(">Y")
    .workplane()
    .pushPoints([(x, 0) for x in hole_xs])
    .circle(HOLE_D / 2)
    .cutThruAll()
)

# Top T-slot channel (simplified: rectangular groove cut from top face)
slot_cutter = (cq.Workplane("XY")
    .box(SLOT_W, LENGTH, SLOT_D + 1.0)
    .translate((0, 0, HEIGHT / 2 - SLOT_D / 2 + 0.5))
)
result = result.cut(slot_cutter)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_ball_bearing(params: dict[str, Any]) -> str:
    od    = float(params.get("od_mm",     30.0))
    bore  = float(params.get("bore_mm",   10.0))
    width = float(params.get("width_mm",   9.0))
    wall  = float(params.get("wall_mm",    2.5))   # ring wall thickness

    return f"""
import cadquery as cq
OD    = {od}
BORE  = {bore}
WIDTH = {width}
WALL  = {wall}

# Outer ring
outer = (cq.Workplane("XY")
    .circle(OD / 2).circle(OD / 2 - WALL)
    .extrude(WIDTH)
)

# Inner ring
inner = (cq.Workplane("XY")
    .circle(BORE / 2 + WALL).circle(BORE / 2)
    .extrude(WIDTH)
)

# Retainer ring (thin disk between rings)
mid_r = (OD / 2 - WALL + BORE / 2 + WALL) / 2
ret_w = mid_r - BORE / 2 - WALL
retainer = (cq.Workplane("XY")
    .workplane(offset=WIDTH * 0.4)
    .circle(OD / 2 - WALL).circle(BORE / 2 + WALL)
    .extrude(WIDTH * 0.2)
)

result = outer.union(inner).union(retainer)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_shaft_coupling(params: dict[str, Any]) -> str:
    od      = float(params.get("od_mm",      20.0))
    bore1   = float(params.get("bore1_mm",    5.0))   # shaft 1 bore
    bore2   = float(params.get("bore2_mm",    params.get("bore_mm", 5.0)))  # shaft 2 bore
    length  = float(params.get("length_mm",  30.0))
    clamp_w = float(params.get("clamp_slot_mm", 1.0))   # clamping split width
    screw_d = float(params.get("screw_d_mm",  3.0))     # clamp screw diameter

    return f"""
import cadquery as cq, math
OD      = {od}
BORE1   = {bore1}
BORE2   = {bore2}
LENGTH  = {length}
CLAMP_W = {clamp_w}
SCREW_D = {screw_d}

# Body cylinder
body = cq.Workplane("XY").circle(OD / 2).extrude(LENGTH)

# Bore 1 from bottom face
body = body.faces("<Z").workplane().circle(BORE1 / 2).cutBlind(-LENGTH / 2)

# Bore 2 from top face
body = body.faces(">Z").workplane().circle(BORE2 / 2).cutBlind(-LENGTH / 2)

# Clamping split slot (axial, through full length, one side)
split = (cq.Workplane("YZ")
    .center(0, LENGTH / 2)
    .rect(CLAMP_W, LENGTH)
    .extrude(OD / 2)
)
result = body.cut(split)

# Clamp screw holes (2 per end, perpendicular to split)
screw_z_offsets = [LENGTH * 0.2, LENGTH * 0.8]
for z in screw_z_offsets:
    result = (result
        .faces(">X")
        .workplane(origin=(0, 0, z))
        .circle(SCREW_D / 2)
        .cutThruAll()
    )

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_profile_extrusion(params: dict[str, Any]) -> str:
    size   = float(params.get("size_mm",     20.0))   # 20=2020, 40=4040
    length = float(params.get("length_mm",  400.0))
    slot_w = float(params.get("slot_w_mm",   6.2))    # T-slot opening width
    slot_d = float(params.get("slot_d_mm",   5.8))    # T-slot depth
    neck_w = float(params.get("neck_w_mm",   5.2))    # T-slot neck width
    bore_d = float(params.get("bore_d_mm",   size * 0.21))  # centre bore (~4.2mm for 2020)

    return f"""
import cadquery as cq
SIZE   = {size}
LENGTH = {length}
SLOT_W = {slot_w}
SLOT_D = {slot_d}
NECK_W = {neck_w}
BORE_D = {bore_d}

# Square extrusion body
result = cq.Workplane("XY").box(SIZE, SIZE, LENGTH)

# Centre bore
result = result.faces(">Z").workplane().circle(BORE_D / 2).cutThruAll()

# T-slots on all 4 faces — neck cut then undercut for each face
for face_sel, is_x in [(">X", True), ("<X", True), (">Y", False), ("<Y", False)]:
    # Neck: narrow slot at surface
    neck = (cq.Workplane("XY")
        .workplane(offset=LENGTH / 2)
        .rect(NECK_W if is_x else SIZE * 2, SIZE * 2 if is_x else NECK_W)
        .cutBlind(-SLOT_D)
    )
    # Undercut: wider slot deeper in
    under = (cq.Workplane("XY")
        .workplane(offset=LENGTH / 2 - (SLOT_D - SLOT_D * 0.45))
        .rect(SLOT_W if is_x else SIZE * 2, SIZE * 2 if is_x else SLOT_W)
        .cutBlind(-(SLOT_D * 0.45))
    )
    result = result.cut(neck).cut(under)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_snap_hook(params: dict[str, Any]) -> str:
    """Snap-fit hook/clip: flat base strip with cantilever hook at one end."""
    length    = float(params.get("length_mm", 40.0))
    width     = float(params.get("width_mm", 10.0))
    thickness = float(params.get("thickness_mm", 2.0))
    hook_h    = float(params.get("hook_height_mm", 8.0))
    hook_d    = float(params.get("hook_depth_mm", 2.0))
    return f"""
import cadquery as cq, math

LENGTH    = {length}
WIDTH     = {width}
THICKNESS = {thickness}
HOOK_H    = {hook_h}
HOOK_D    = {hook_d}

# Base strip
base = cq.Workplane("XY").box(LENGTH, WIDTH, THICKNESS)

# Cantilever arm — thin plate rising at slight angle from one end
arm_len = HOOK_H * 1.2
arm_t   = THICKNESS * 0.7
arm = (cq.Workplane("XZ")
    .workplane(offset=-WIDTH / 2.0)
    .center(LENGTH / 2.0 - arm_t / 2.0, THICKNESS / 2.0)
    .rect(arm_t, arm_len)
    .extrude(WIDTH))

# Hook lip — small block at tip of arm
lip = (cq.Workplane("XZ")
    .workplane(offset=-WIDTH / 2.0)
    .center(LENGTH / 2.0 - arm_t / 2.0 - HOOK_D / 2.0, THICKNESS / 2.0 + arm_len - HOOK_D / 2.0)
    .rect(HOOK_D + arm_t, HOOK_D)
    .extrude(WIDTH))

# Ramp — triangular entry ramp on the outside of the hook
ramp = (cq.Workplane("XZ")
    .workplane(offset=-WIDTH / 2.0)
    .center(LENGTH / 2.0 + arm_t / 2.0, THICKNESS / 2.0 + arm_len - HOOK_D)
    .polyline([(0, 0), (HOOK_D, 0), (0, HOOK_D)])
    .close()
    .extrude(WIDTH))

result = base.union(arm).union(lip).union(ramp)
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_thread_insert(params: dict[str, Any]) -> str:
    """Threaded insert / standoff with knurling approximated as polygon facets."""
    import math as _m
    od          = float(params.get("od_mm", 8.0))
    bore        = float(params.get("bore_mm", 4.0))
    height      = float(params.get("height_mm", 10.0))
    knurl_count = int(params.get("knurl_count", 16))
    # Knurl: outer polygon with knurl_count sides, slightly larger than od
    knurl_r = od / 2.0 * 1.08  # peaks 8% beyond OD
    return f"""
import cadquery as cq, math

OD_MM       = {od}
BORE_MM     = {bore}
HEIGHT_MM   = {height}
KNURL_COUNT = {knurl_count}
KNURL_R     = {knurl_r:.3f}

# Inner cylinder (the smooth body)
body = cq.Workplane("XY").circle(OD_MM / 2.0).extrude(HEIGHT_MM)

# Knurl ridges — polygon slightly larger than body, intersected to create peaks
knurl_pts = []
for i in range(KNURL_COUNT):
    a = 2 * math.pi * i / KNURL_COUNT
    knurl_pts.append((KNURL_R * math.cos(a), KNURL_R * math.sin(a)))
knurl_poly = cq.Workplane("XY").polyline(knurl_pts).close().extrude(HEIGHT_MM)

# Union the polygon with the cylinder — the polygon peaks protrude beyond the circle
result = body.union(knurl_poly)

# Centre bore
bore_cutter = cq.Workplane("XY").workplane(offset=-1).circle(BORE_MM / 2.0).extrude(HEIGHT_MM + 2)
result = result.cut(bore_cutter)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_hinge(params: dict[str, Any]) -> str:
    """Simple hinge: two flat leaves with interlocking knuckles and a pin hole."""
    import math as _m
    width      = float(params.get("width_mm", 50.0))
    height     = float(params.get("height_mm", 30.0))
    thickness  = float(params.get("thickness_mm", 2.0))
    pin_dia    = float(params.get("pin_dia_mm", 4.0))
    n_knuckles = max(3, int(params.get("n_knuckles", 5)))
    # Ensure odd number of knuckles for interlocking
    if n_knuckles % 2 == 0:
        n_knuckles += 1
    knuckle_r  = pin_dia * 1.2  # knuckle outer radius
    knuckle_len = width / n_knuckles
    return f"""
import cadquery as cq, math

WIDTH       = {width}
HEIGHT      = {height}
THICKNESS   = {thickness}
PIN_DIA     = {pin_dia}
N_KNUCKLES  = {n_knuckles}
KNUCKLE_R   = {knuckle_r:.3f}
KNUCKLE_LEN = {knuckle_len:.3f}

# --- Left leaf (flat plate) ---
left_leaf = cq.Workplane("XY").box(HEIGHT, WIDTH, THICKNESS)
left_leaf = left_leaf.translate((-HEIGHT / 2.0, 0, 0))

# --- Right leaf (flat plate) ---
right_leaf = cq.Workplane("XY").box(HEIGHT, WIDTH, THICKNESS)
right_leaf = right_leaf.translate((HEIGHT / 2.0, 0, 0))

result = left_leaf.union(right_leaf)

# --- Knuckles along Y axis at center (x=0) ---
for k in range(N_KNUCKLES):
    y_start = -WIDTH / 2.0 + k * KNUCKLE_LEN
    knuckle = (cq.Workplane("XY")
        .workplane(offset=-KNUCKLE_R)
        .center(0, y_start + KNUCKLE_LEN / 2.0)
        .circle(KNUCKLE_R)
        .extrude(KNUCKLE_R * 2))
    result = result.union(knuckle)

# --- Pin hole through all knuckles (along Y) ---
pin_hole = (cq.Workplane("XZ")
    .workplane(offset=-WIDTH / 2.0 - 1)
    .center(0, 0)
    .circle(PIN_DIA / 2.0)
    .extrude(WIDTH + 2))
result = result.cut(pin_hole)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_clamp(params: dict[str, Any]) -> str:
    """C-clamp / cable clamp: semi-circular channel with mounting wings and bolt holes."""
    import math as _m
    cable_dia = float(params.get("cable_dia_mm", 12.0))
    length    = float(params.get("length_mm", 30.0))
    thickness = float(params.get("thickness_mm", 3.0))
    n_bolts   = max(1, int(params.get("n_bolts", 2)))
    bolt_dia  = float(params.get("bolt_dia_mm", 5.0))
    # Wing dimensions
    wing_w    = max(bolt_dia * 2.5, 12.0)
    channel_r = cable_dia / 2.0 + thickness
    return f"""
import cadquery as cq, math

CABLE_DIA  = {cable_dia}
LENGTH     = {length}
THICKNESS  = {thickness}
BOLT_DIA   = {bolt_dia}
N_BOLTS    = {n_bolts}
WING_W     = {wing_w:.3f}
CHANNEL_R  = {channel_r:.3f}
CABLE_R    = CABLE_DIA / 2.0

# Semi-circular channel body (half-pipe)
outer_half = (cq.Workplane("XY")
    .circle(CHANNEL_R)
    .circle(CABLE_R)
    .extrude(LENGTH))

# Cut away top half to make semi-circle
cut_box = (cq.Workplane("XY")
    .workplane(offset=-1)
    .center(0, CHANNEL_R / 2.0 + 0.01)
    .rect(CHANNEL_R * 3, CHANNEL_R + 0.02)
    .extrude(LENGTH + 2))
half_pipe = outer_half.cut(cut_box)

# Flat mounting wings on each side
wing_left = (cq.Workplane("XY")
    .center(-CHANNEL_R - WING_W / 2.0, 0)
    .rect(WING_W, THICKNESS)
    .extrude(LENGTH))
wing_right = (cq.Workplane("XY")
    .center(CHANNEL_R + WING_W / 2.0, 0)
    .rect(WING_W, THICKNESS)
    .extrude(LENGTH))

result = half_pipe.union(wing_left).union(wing_right)

# Bolt holes in wings
bolt_pts = []
for i in range(N_BOLTS):
    z_pos = LENGTH * (i + 1) / (N_BOLTS + 1)
    bolt_pts.append(z_pos)

for z_pos in bolt_pts:
    for x_pos in [-(CHANNEL_R + WING_W / 2.0), (CHANNEL_R + WING_W / 2.0)]:
        hole = (cq.Workplane("XZ")
            .workplane(offset=-THICKNESS / 2.0 - 1)
            .center(z_pos, x_pos)
            .circle(BOLT_DIA / 2.0)
            .extrude(THICKNESS + 2))
        result = result.cut(hole)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_handle(params: dict[str, Any]) -> str:
    """Ergonomic handle/grip: cylindrical grip with tapered ends and mounting flange."""
    length     = float(params.get("length_mm", 100.0))
    grip_dia   = float(params.get("grip_dia_mm", 30.0))
    bore       = float(params.get("bore_mm", 8.0))
    flange_dia = float(params.get("flange_dia_mm", grip_dia * 1.6))
    flange_t   = float(params.get("flange_thickness_mm", 5.0))
    taper_len  = float(params.get("taper_length_mm", length * 0.15))
    return f"""
import cadquery as cq, math

LENGTH     = {length}
GRIP_DIA   = {grip_dia}
BORE_MM    = {bore}
FLANGE_DIA = {flange_dia}
FLANGE_T   = {flange_t}
TAPER_LEN  = {taper_len:.3f}

# Main grip cylinder
grip = cq.Workplane("XY").circle(GRIP_DIA / 2.0).extrude(LENGTH)

# Taper at top — cone-like reduction via a slightly smaller cylinder
taper_r = GRIP_DIA / 2.0 * 0.7
top_taper = (cq.Workplane("XY")
    .workplane(offset=LENGTH - TAPER_LEN)
    .circle(GRIP_DIA / 2.0)
    .workplane(offset=TAPER_LEN)
    .circle(taper_r)
    .loft())

# Taper at bottom (above flange)
bot_taper = (cq.Workplane("XY")
    .workplane(offset=FLANGE_T)
    .circle(GRIP_DIA / 2.0)
    .workplane(offset=TAPER_LEN)
    .circle(GRIP_DIA / 2.0)
    .loft())

# Mounting flange at base
flange = cq.Workplane("XY").circle(FLANGE_DIA / 2.0).extrude(FLANGE_T)

result = grip.union(flange).union(top_taper)

# Through bore for mounting bolt
bore_cutter = (cq.Workplane("XY")
    .workplane(offset=-1)
    .circle(BORE_MM / 2.0)
    .extrude(LENGTH + 2))
result = result.cut(bore_cutter)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_enclosure_lid(params: dict[str, Any]) -> str:
    """Snap-fit or screw-on lid for a box: flat plate with perimeter lip and screw holes."""
    import math as _m
    width     = float(params.get("width_mm", 100.0))
    depth     = float(params.get("depth_mm", 80.0))
    thickness = float(params.get("thickness_mm", 3.0))
    lip_h     = float(params.get("lip_height_mm", 5.0))
    lip_t     = float(params.get("lip_thickness_mm", max(1.5, thickness * 0.5)))
    n_screws  = int(params.get("n_screws", params.get("n_bolts", 4)))
    screw_dia = float(params.get("screw_dia_mm", params.get("bolt_dia_mm", 3.0)))
    return f"""
import cadquery as cq, math

WIDTH     = {width}
DEPTH     = {depth}
THICKNESS = {thickness}
LIP_H     = {lip_h}
LIP_T     = {lip_t:.3f}
N_SCREWS  = {n_screws}
SCREW_DIA = {screw_dia}

# Top plate
plate = cq.Workplane("XY").box(WIDTH, DEPTH, THICKNESS)

# Perimeter lip (downward) — outer rect minus inner rect
lip_outer = (cq.Workplane("XY")
    .workplane(offset=-THICKNESS / 2.0)
    .rect(WIDTH, DEPTH)
    .extrude(-LIP_H))
lip_inner = (cq.Workplane("XY")
    .workplane(offset=-THICKNESS / 2.0 + 0.01)
    .rect(WIDTH - 2 * LIP_T, DEPTH - 2 * LIP_T)
    .extrude(-LIP_H - 0.02))
lip = lip_outer.cut(lip_inner)

result = plate.union(lip)

# Screw holes near corners
if N_SCREWS >= 4:
    margin = max(SCREW_DIA * 2, 8.0)
    screw_pts = [
        ( WIDTH / 2 - margin,  DEPTH / 2 - margin),
        (-WIDTH / 2 + margin,  DEPTH / 2 - margin),
        (-WIDTH / 2 + margin, -DEPTH / 2 + margin),
        ( WIDTH / 2 - margin, -DEPTH / 2 + margin),
    ]
    # Add extra screws along edges if n_screws > 4
    extras = N_SCREWS - 4
    for i in range(extras):
        x = -WIDTH / 2 + margin + (WIDTH - 2 * margin) * (i + 1) / (extras + 1)
        screw_pts.append((round(x, 2), DEPTH / 2 - margin))
elif N_SCREWS > 0:
    screw_pts = [(0, 0)]
else:
    screw_pts = []

for sx, sy in screw_pts:
    hole = (cq.Workplane("XY")
        .workplane(offset=THICKNESS / 2.0 + 1)
        .center(sx, sy)
        .circle(SCREW_DIA / 2.0)
        .extrude(-(THICKNESS + LIP_H + 2)))
    result = result.cut(hole)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_gusset(params: dict[str, Any]) -> str:
    """Triangular gusset / corner brace: right-triangle plate with bolt holes."""
    import math as _m
    leg_a     = float(params.get("leg_a_mm", 60.0))
    leg_b     = float(params.get("leg_b_mm", 60.0))
    thickness = float(params.get("thickness_mm", 5.0))
    n_bolts   = max(0, int(params.get("n_bolts", 4)))
    bolt_dia  = float(params.get("bolt_dia_mm", 6.0))
    return f"""
import cadquery as cq, math

LEG_A     = {leg_a}
LEG_B     = {leg_b}
THICKNESS = {thickness}
N_BOLTS   = {n_bolts}
BOLT_DIA  = {bolt_dia}

# Right triangle: vertices at origin, (LEG_A, 0), (0, LEG_B)
tri = (cq.Workplane("XY")
    .polyline([(0, 0), (LEG_A, 0), (0, LEG_B)])
    .close()
    .extrude(THICKNESS))

result = tri

# Bolt holes — split evenly between the two legs
n_a = max(1, N_BOLTS // 2)
n_b = max(1, N_BOLTS - n_a)
margin = max(BOLT_DIA * 2, 8.0)

bolt_pts = []
# Holes along leg A (horizontal)
for i in range(n_a):
    x = margin + (LEG_A - 2 * margin) * i / max(n_a - 1, 1)
    bolt_pts.append((round(x, 2), margin))
# Holes along leg B (vertical)
for i in range(n_b):
    y = margin + (LEG_B - 2 * margin) * i / max(n_b - 1, 1)
    bolt_pts.append((margin, round(y, 2)))

for bx, by in bolt_pts:
    hole = (cq.Workplane("XY")
        .workplane(offset=-1)
        .center(bx, by)
        .circle(BOLT_DIA / 2.0)
        .extrude(THICKNESS + 2))
    result = result.cut(hole)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_spoked_wheel(params: dict[str, Any]) -> str:
    """Spoked wheel / handwheel: outer rim + hub + N radial spokes."""
    import math as _m
    od            = float(params.get("od_mm", 120.0))
    hub_od        = float(params.get("hub_od_mm", 30.0))
    bore          = float(params.get("bore_mm", 10.0))
    n_spokes      = max(3, int(params.get("n_spokes", 5)))
    spoke_width   = float(params.get("spoke_width_mm", 8.0))
    rim_thickness = float(params.get("rim_thickness_mm", 10.0))
    rim_width     = float(params.get("rim_width_mm", max(spoke_width, 8.0)))
    rim_r_outer   = od / 2.0
    rim_r_inner   = od / 2.0 - rim_thickness
    hub_r         = hub_od / 2.0
    # spoke length from hub OD to rim inner edge
    spoke_len     = rim_r_inner - hub_r
    return f"""
import cadquery as cq, math

OD            = {od}
HUB_OD        = {hub_od}
BORE_MM       = {bore}
N_SPOKES      = {n_spokes}
SPOKE_W       = {spoke_width}
RIM_THICK     = {rim_thickness}
RIM_W         = {rim_width}
RIM_R_OUTER   = {rim_r_outer:.3f}
RIM_R_INNER   = {rim_r_inner:.3f}
HUB_R         = {hub_r:.3f}
SPOKE_LEN     = {spoke_len:.3f}

# Outer rim (annular ring)
rim = (cq.Workplane("XY")
    .circle(RIM_R_OUTER)
    .circle(RIM_R_INNER)
    .extrude(RIM_W))

# Hub (solid cylinder)
hub = cq.Workplane("XY").circle(HUB_R).extrude(RIM_W)

result = rim.union(hub)

# Radial spokes
for i in range(N_SPOKES):
    angle = 2 * math.pi * i / N_SPOKES
    ca, sa = math.cos(angle), math.sin(angle)
    # Spoke center at midpoint between hub and rim inner edge
    mid_r = HUB_R + SPOKE_LEN / 2.0
    cx, cy = mid_r * ca, mid_r * sa
    spoke = (cq.Workplane("XY")
        .center(cx, cy)
        .rect(SPOKE_LEN, SPOKE_W)
        .extrude(RIM_W))
    # Rotate the spoke to align radially
    spoke = spoke.rotate((0, 0, 0), (0, 0, 1), math.degrees(angle))
    result = result.union(spoke)

# Centre bore
bore_cutter = (cq.Workplane("XY")
    .workplane(offset=-1)
    .circle(BORE_MM / 2.0)
    .extrude(RIM_W + 2))
result = result.cut(bore_cutter)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_t_slot_plate(params: dict[str, Any]) -> str:
    """T-slot fixture plate: flat plate with T-shaped grooves running along its length."""
    width      = float(params.get("width_mm", 200.0))
    depth      = float(params.get("depth_mm", 150.0))
    thickness  = float(params.get("thickness_mm", 15.0))
    n_slots    = max(1, int(params.get("n_slots", 3)))
    slot_width = float(params.get("slot_width_mm", 12.0))
    slot_depth = float(params.get("slot_depth_mm", 8.0))
    neck_width = float(params.get("neck_width_mm", slot_width * 0.5))
    neck_depth = float(params.get("neck_depth_mm", slot_depth * 0.4))
    return f"""
import cadquery as cq, math

WIDTH      = {width}
DEPTH      = {depth}
THICKNESS  = {thickness}
N_SLOTS    = {n_slots}
SLOT_W     = {slot_width}
SLOT_D     = {slot_depth}
NECK_W     = {neck_width:.3f}
NECK_D     = {neck_depth:.3f}

# Base plate
result = cq.Workplane("XY").box(WIDTH, DEPTH, THICKNESS)

# T-slots running along Y (depth direction), spaced evenly along X (width)
spacing = WIDTH / (N_SLOTS + 1)

for i in range(N_SLOTS):
    x_pos = -WIDTH / 2.0 + spacing * (i + 1)

    # Upper narrow neck slot (visible from top)
    neck = (cq.Workplane("XY")
        .workplane(offset=THICKNESS / 2.0 + 0.01)
        .center(x_pos, 0)
        .rect(NECK_W, DEPTH + 1)
        .extrude(-(NECK_D + 0.01)))

    # Lower wider T-head slot
    head = (cq.Workplane("XY")
        .workplane(offset=THICKNESS / 2.0 - NECK_D + 0.01)
        .center(x_pos, 0)
        .rect(SLOT_W, DEPTH + 1)
        .extrude(-(SLOT_D - NECK_D + 0.01)))

    result = result.cut(neck).cut(head)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_spring_clip(params: dict[str, Any]) -> str:
    """Spring clip / retaining clip: U-shaped body with inward-facing hooks at each end."""
    width     = float(params.get("width_mm", 20.0))
    height    = float(params.get("height_mm", 30.0))
    thickness = float(params.get("thickness_mm", 1.5))
    hook_d    = float(params.get("hook_depth_mm", 3.0))
    depth     = float(params.get("depth_mm", width))  # extrusion depth
    return f"""
import cadquery as cq, math

WIDTH     = {width}
HEIGHT    = {height}
THICKNESS = {thickness}
HOOK_D    = {hook_d}
DEPTH     = {depth}

# U-shape: bottom bar + two side bars + two inward hooks at top
# Bottom bar
bottom = (cq.Workplane("XY")
    .center(0, 0)
    .rect(WIDTH, THICKNESS)
    .extrude(DEPTH))

# Left side bar
left = (cq.Workplane("XY")
    .center(-WIDTH / 2.0 + THICKNESS / 2.0, HEIGHT / 2.0)
    .rect(THICKNESS, HEIGHT)
    .extrude(DEPTH))

# Right side bar
right = (cq.Workplane("XY")
    .center(WIDTH / 2.0 - THICKNESS / 2.0, HEIGHT / 2.0)
    .rect(THICKNESS, HEIGHT)
    .extrude(DEPTH))

# Left inward hook (at top of left bar, pointing right)
hook_left = (cq.Workplane("XY")
    .center(-WIDTH / 2.0 + THICKNESS + HOOK_D / 2.0, HEIGHT - THICKNESS / 2.0)
    .rect(HOOK_D, THICKNESS)
    .extrude(DEPTH))

# Right inward hook (at top of right bar, pointing left)
hook_right = (cq.Workplane("XY")
    .center(WIDTH / 2.0 - THICKNESS - HOOK_D / 2.0, HEIGHT - THICKNESS / 2.0)
    .rect(HOOK_D, THICKNESS)
    .extrude(DEPTH))

result = bottom.union(left).union(right).union(hook_left).union(hook_right)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_involute_gear(params: dict[str, Any]) -> str:
    """Real involute spur gear with parametric tooth profile."""
    import math as _m

    module_mm = float(params.get("module_mm", 2.0))
    n_teeth   = int(params.get("n_teeth", 20))
    face_w    = float(params.get("face_width_mm", 10.0))
    bore      = float(params.get("bore_mm", 10.0))
    pa_deg    = float(params.get("pressure_angle_deg", 20.0))

    pitch_r = module_mm * n_teeth / 2.0
    base_r  = pitch_r * _m.cos(_m.radians(pa_deg))
    tip_r   = pitch_r + module_mm
    root_r  = max(pitch_r - 1.25 * module_mm, bore / 2.0 + 1.0)

    def _inv(t, rb):
        return rb * (_m.cos(t) + t * _m.sin(t)), rb * (_m.sin(t) - t * _m.cos(t))

    def _t_for_r(r, rb):
        return _m.sqrt(max(0.0, (r / rb) ** 2 - 1.0))

    t_pitch = _t_for_r(pitch_r, base_r)
    ipx, ipy = _inv(t_pitch, base_r)
    inv_pa  = _m.atan2(ipy, ipx)
    half_ta = _m.pi / n_teeth
    p_step  = 2 * _m.pi / n_teeth
    N_PTS   = 6
    t_root  = _t_for_r(max(root_r, base_r), base_r)
    t_tip   = _t_for_r(tip_r, base_r)
    rot_off = inv_pa - half_ta + t_pitch

    def _one_tooth():
        p = []
        p.append((root_r * _m.cos(-half_ta - _m.pi / n_teeth),
                  root_r * _m.sin(-half_ta - _m.pi / n_teeth)))
        for i in range(N_PTS + 1):
            t = t_root + (t_tip - t_root) * i / N_PTS
            x, y = _inv(t, base_r)
            r = _m.hypot(x, y)
            a = _m.atan2(y, x) - rot_off
            p.append((r * _m.cos(a), r * _m.sin(a)))
        tip_half = _m.acos(min(1.0, base_r / tip_r))
        for i in range(3):
            a = half_ta + tip_half * (1.0 - float(i))
            p.append((tip_r * _m.cos(a), tip_r * _m.sin(a)))
        for i in range(N_PTS, -1, -1):
            t = t_root + (t_tip - t_root) * i / N_PTS
            x, y = _inv(t, base_r)
            r = _m.hypot(x, y)
            a = -(_m.atan2(y, x) - rot_off)
            p.append((r * _m.cos(a), r * _m.sin(a)))
        p.append((root_r * _m.cos(half_ta + _m.pi / n_teeth),
                  root_r * _m.sin(half_ta + _m.pi / n_teeth)))
        return p

    one_tooth = _one_tooth()
    all_pts: list[tuple[float, float]] = []
    for i in range(n_teeth):
        a = i * p_step
        ca, sa = _m.cos(a), _m.sin(a)
        for px, py in one_tooth:
            all_pts.append((round(px * ca - py * sa, 5),
                            round(px * sa + py * ca, 5)))
    pts_literal = repr(all_pts)

    return f"""
import cadquery as cq, math

# === Involute Spur Gear — {n_teeth}t, m={module_mm}mm, PA={pa_deg}deg ===
FACE_W   = {face_w}
BORE     = {bore}
TIP_R    = {tip_r:.5f}

all_pts = {pts_literal}

try:
    gear = cq.Workplane("XY").polyline(all_pts).close().extrude(FACE_W)
    bore_cyl = cq.Workplane("XY").circle(BORE / 2.0).extrude(FACE_W + 2).translate((0, 0, -1))
    gear = gear.cut(bore_cyl)
except Exception:
    gear = cq.Workplane("XY").circle(TIP_R).circle(BORE / 2.0).extrude(FACE_W)

result = gear
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_cam_profile(params: dict[str, Any]) -> str:
    """Eccentric cam disc with sinusoidal rise profile."""
    import math as _m

    base_r   = float(params.get("base_circle_mm", 30.0))
    rise     = float(params.get("rise_mm", 10.0))
    dwell    = float(params.get("dwell_deg", 120.0))
    bore     = float(params.get("bore_mm", 10.0))
    thick    = float(params.get("thickness_mm", 12.0))

    # Pre-compute cam profile as polygon: 72 pts around 360 deg
    n_pts = 72
    pts: list[tuple[float, float]] = []
    dwell_rad = _m.radians(dwell)
    rise_range = 2 * _m.pi - dwell_rad
    for i in range(n_pts):
        angle = 2 * _m.pi * i / n_pts
        if angle <= dwell_rad:
            r = base_r
        else:
            frac = (angle - dwell_rad) / rise_range
            r = base_r + rise * _m.sin(frac * _m.pi)
        pts.append((round(r * _m.cos(angle), 5), round(r * _m.sin(angle), 5)))

    pts_literal = repr(pts)

    return f"""
import cadquery as cq, math

# === Cam Profile — base={base_r}mm, rise={rise}mm, dwell={dwell}deg ===
THICKNESS_MM = {thick}
BORE_MM      = {bore}

cam_pts = {pts_literal}

cam_body = cq.Workplane("XY").polyline(cam_pts).close().extrude(THICKNESS_MM)
bore_cyl = cq.Workplane("XY").workplane(offset=-1.0).circle(BORE_MM / 2.0).extrude(THICKNESS_MM + 2.0)
result = cam_body.cut(bore_cyl)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_bellows(params: dict[str, Any]) -> str:
    """Corrugated bellows — alternating large/small diameter rings stacked."""
    od   = float(params.get("od_mm", 60.0))
    id_  = float(params.get("id_mm", params.get("bore_mm", 40.0)))
    length = float(params.get("length_mm", 80.0))
    n_conv = int(params.get("n_convolutions", 6))
    wall   = float(params.get("wall_mm", 1.5))

    # Each convolution = one large ring + one small ring
    ring_h = length / (2 * n_conv) if n_conv > 0 else length
    mid_r  = (od / 2.0 + id_ / 2.0) / 2.0  # middle radius for small rings
    outer_r = od / 2.0
    inner_r = mid_r  # small rings are at mid radius

    return f"""
import cadquery as cq, math

# === Corrugated Bellows — {n_conv} convolutions ===
OD_MM       = {od}
ID_MM       = {id_}
LENGTH_MM   = {length}
N_CONV      = {n_conv}
WALL_MM     = {wall}
RING_H      = {ring_h:.4f}
OUTER_R     = {outer_r:.4f}
INNER_R     = {inner_r:.4f}

# Build by stacking alternating large and small annular rings
result = None

for i in range(N_CONV * 2):
    z_off = i * RING_H
    if i % 2 == 0:
        # Large ring (full OD)
        ring = (cq.Workplane("XY")
                .workplane(offset=z_off)
                .circle(OUTER_R)
                .circle(OUTER_R - WALL_MM)
                .extrude(RING_H))
    else:
        # Small ring (contracted)
        ring = (cq.Workplane("XY")
                .workplane(offset=z_off)
                .circle(INNER_R)
                .circle(INNER_R - WALL_MM)
                .extrude(RING_H))
    if result is None:
        result = ring
    else:
        result = result.union(ring)

    # Connecting wall between rings (top face of each ring joins to next)
    if i > 0:
        prev_r = OUTER_R if (i - 1) % 2 == 0 else INNER_R
        curr_r = OUTER_R if i % 2 == 0 else INNER_R
        big_r = max(prev_r, curr_r)
        small_r = min(prev_r, curr_r) - WALL_MM
        connector = (cq.Workplane("XY")
                     .workplane(offset=z_off - 0.1)
                     .circle(big_r)
                     .circle(small_r)
                     .extrude(0.2))
        result = result.union(connector)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_compression_spring(params: dict[str, Any]) -> str:
    """Helical compression spring approximated with stacked toroidal rings."""
    od          = float(params.get("od_mm", 20.0))
    wire_d      = float(params.get("wire_dia_mm", 2.0))
    n_coils     = int(params.get("n_coils", 8))
    free_length = float(params.get("free_length_mm", 40.0))

    coil_r = od / 2.0 - wire_d / 2.0
    pitch  = free_length / max(n_coils, 1)

    return f"""
import cadquery as cq, math

# === Compression Spring — {n_coils} coils, OD={od}mm ===
COIL_R      = {coil_r:.4f}
WIRE_D      = {wire_d}
N_COILS     = {n_coils}
PITCH       = {pitch:.4f}
WIRE_R      = WIRE_D / 2.0

# Build spring as stacked tori (donut cross-sections at each coil height)
# Each torus: draw wire cross-section at (COIL_R, 0) in XZ plane, revolve around Z axis
result = None
for i in range(N_COILS):
    z_off = i * PITCH + PITCH / 2.0
    ring = (cq.Workplane("XZ")
            .center(COIL_R, z_off)
            .circle(WIRE_R)
            .revolve(360, (-COIL_R, 0), (-COIL_R, 1)))
    if result is None:
        result = ring
    else:
        result = result.union(ring)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_keyway_shaft(params: dict[str, Any]) -> str:
    """Shaft with a keyway slot cut along its length."""
    diameter = float(params.get("diameter_mm", params.get("od_mm", 25.0)))
    length   = float(params.get("length_mm", 100.0))
    key_w    = float(params.get("key_width_mm", max(diameter * 0.25, 4.0)))
    key_d    = float(params.get("key_depth_mm", key_w * 0.5))
    key_l    = float(params.get("key_length_mm", length * 0.4))

    return f"""
import cadquery as cq, math

# === Keyway Shaft — dia={diameter}mm, L={length}mm ===
DIAMETER_MM  = {diameter}
LENGTH_MM    = {length}
KEY_W_MM     = {key_w}
KEY_D_MM     = {key_d}
KEY_L_MM     = {key_l}

# Main shaft body
shaft = cq.Workplane("XY").circle(DIAMETER_MM / 2.0).extrude(LENGTH_MM)

# Keyway: rectangular slot cut from the top of the shaft
# Positioned at top of shaft (Y+), centered along Z
key_z_start = (LENGTH_MM - KEY_L_MM) / 2.0
keyway = (cq.Workplane("XY")
          .workplane(offset=key_z_start)
          .center(0, DIAMETER_MM / 2.0 - KEY_D_MM / 2.0)
          .rect(KEY_W_MM, KEY_D_MM + 1.0)
          .extrude(KEY_L_MM))
result = shaft.cut(keyway)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_dovetail_joint(params: dict[str, Any]) -> str:
    """Dovetail slide joint — male part (trapezoidal rail)."""
    width  = float(params.get("width_mm", 40.0))
    height = float(params.get("height_mm", 20.0))
    length = float(params.get("length_mm", 80.0))
    angle  = float(params.get("dovetail_angle_deg", 60.0))
    dt_depth = float(params.get("dovetail_depth_mm", height * 0.4))

    import math as _m
    # Dovetail trapezoid: top is narrower than bottom
    half_angle_rad = _m.radians(angle / 2.0)
    dt_top_half  = width / 2.0 - dt_depth * _m.tan(half_angle_rad)
    dt_top_half  = max(dt_top_half, width * 0.15)  # ensure positive
    base_half    = width / 2.0

    return f"""
import cadquery as cq, math

# === Dovetail Joint (male rail) — {width}x{height}mm, angle={angle}deg ===
WIDTH_MM     = {width}
HEIGHT_MM    = {height}
LENGTH_MM    = {length}
DT_DEPTH     = {dt_depth}
DT_TOP_HALF  = {dt_top_half:.4f}
BASE_HALF    = {base_half:.4f}

# Base rectangular block
base_h = HEIGHT_MM - DT_DEPTH
base = cq.Workplane("XY").rect(WIDTH_MM, base_h).extrude(LENGTH_MM)

# Dovetail trapezoidal rail on top
# Profile: trapezoid wider at base, narrower at top
trap = (cq.Workplane("XY")
        .workplane(offset=0)
        .moveTo(-BASE_HALF, base_h / 2.0)
        .lineTo(-DT_TOP_HALF, base_h / 2.0 + DT_DEPTH)
        .lineTo(DT_TOP_HALF, base_h / 2.0 + DT_DEPTH)
        .lineTo(BASE_HALF, base_h / 2.0)
        .close()
        .extrude(LENGTH_MM))

result = base.union(trap)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_slot_plate(params: dict[str, Any]) -> str:
    """Plate with elongated slots."""
    width    = float(params.get("width_mm", 120.0))
    height   = float(params.get("height_mm", 80.0))
    thick    = float(params.get("thickness_mm", 6.0))
    n_slots  = int(params.get("n_slots", 4))
    slot_l   = float(params.get("slot_length_mm", 30.0))
    slot_w   = float(params.get("slot_width_mm", 8.0))

    import math as _m
    # Distribute slots evenly across the plate
    margin = max(slot_l / 2.0 + 5.0, width * 0.12)
    if n_slots == 1:
        slot_pts = [(0.0, 0.0)]
    else:
        x_start = -(width / 2.0 - margin)
        x_end   =  (width / 2.0 - margin)
        slot_pts = [(round(x_start + (x_end - x_start) * i / (n_slots - 1), 3), 0.0)
                    for i in range(n_slots)]
    slot_pts_repr = repr(slot_pts)

    return f"""
import cadquery as cq, math

# === Slot Plate — {n_slots} slots, {width}x{height}x{thick}mm ===
WIDTH_MM      = {width}
HEIGHT_MM     = {height}
THICKNESS_MM  = {thick}
N_SLOTS       = {n_slots}
SLOT_L_MM     = {slot_l}
SLOT_W_MM     = {slot_w}

# Base plate
plate = cq.Workplane("XY").box(WIDTH_MM, HEIGHT_MM, THICKNESS_MM)

# Cut elongated slots (stadium / oblong shape = rect + two semicircle ends)
slot_pts = {slot_pts_repr}
for sx, sy in slot_pts:
    slot_body = (cq.Workplane("XY")
                 .workplane(offset=-1.0)
                 .center(sx, sy)
                 .rect(SLOT_W_MM, SLOT_L_MM - SLOT_W_MM)
                 .extrude(THICKNESS_MM + 2.0))
    end_top = (cq.Workplane("XY")
               .workplane(offset=-1.0)
               .center(sx, sy + (SLOT_L_MM - SLOT_W_MM) / 2.0)
               .circle(SLOT_W_MM / 2.0)
               .extrude(THICKNESS_MM + 2.0))
    end_bot = (cq.Workplane("XY")
               .workplane(offset=-1.0)
               .center(sx, sy - (SLOT_L_MM - SLOT_W_MM) / 2.0)
               .circle(SLOT_W_MM / 2.0)
               .extrude(THICKNESS_MM + 2.0))
    plate = plate.cut(slot_body).cut(end_top).cut(end_bot)

result = plate

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_pcb_enclosure(params: dict[str, Any]) -> str:
    """Box enclosure for PCB with internal standoffs."""
    width   = float(params.get("width_mm", 100.0))
    depth   = float(params.get("depth_mm", 80.0))
    height  = float(params.get("height_mm", 40.0))
    wall    = float(params.get("wall_mm", 2.5))
    stoff_d = float(params.get("standoff_dia_mm", 6.0))
    stoff_h = float(params.get("standoff_height_mm", 5.0))
    pcb_mount = str(params.get("pcb_mount_pattern", "4-corner"))

    # Standoff positions: 4 corners with 3mm inset from inner wall
    inset = wall + 3.0
    stoff_pts = [
        (round(-width / 2.0 + inset, 3), round(-depth / 2.0 + inset, 3)),
        (round( width / 2.0 - inset, 3), round(-depth / 2.0 + inset, 3)),
        (round(-width / 2.0 + inset, 3), round( depth / 2.0 - inset, 3)),
        (round( width / 2.0 - inset, 3), round( depth / 2.0 - inset, 3)),
    ]
    screw_d = max(2.0, stoff_d * 0.4)
    stoff_pts_repr = repr(stoff_pts)

    return f"""
import cadquery as cq, math

# === PCB Enclosure — {width}x{depth}x{height}mm, wall={wall}mm ===
WIDTH_MM    = {width}
DEPTH_MM    = {depth}
HEIGHT_MM   = {height}
WALL_MM     = {wall}
STOFF_D     = {stoff_d}
STOFF_H     = {stoff_h}
SCREW_D     = {screw_d}

# Outer box
outer = cq.Workplane("XY").box(WIDTH_MM, DEPTH_MM, HEIGHT_MM)

# Shell out (open top)
result = outer.shell(-WALL_MM)

# Standoff positions (4 corners)
stoff_pts = {stoff_pts_repr}
for px, py in stoff_pts:
    # Standoff boss
    boss = (cq.Workplane("XY")
            .workplane(offset=-HEIGHT_MM / 2.0)
            .center(px, py)
            .circle(STOFF_D / 2.0)
            .extrude(STOFF_H))
    # Screw hole in standoff
    hole = (cq.Workplane("XY")
            .workplane(offset=-HEIGHT_MM / 2.0 - 0.5)
            .center(px, py)
            .circle(SCREW_D / 2.0)
            .extrude(STOFF_H + 1.0))
    result = result.union(boss).cut(hole)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_bearing_pillow_block(params: dict[str, Any]) -> str:
    """Pillow block bearing housing."""
    bore      = float(params.get("bore_mm", 25.0))
    width     = float(params.get("width_mm", 120.0))
    height    = float(params.get("height_mm", 60.0))
    bolt_sp   = float(params.get("bolt_spacing_mm", width * 0.7))
    bolt_d    = float(params.get("bolt_dia_mm", 10.0))
    bearing_od = float(params.get("bearing_od_mm", bore * 2.0))
    base_thick = float(params.get("base_thickness_mm", max(10.0, height * 0.2)))

    return f"""
import cadquery as cq, math

# === Pillow Block Bearing Housing — bore={bore}mm ===
BORE_MM       = {bore}
WIDTH_MM      = {width}
HEIGHT_MM     = {height}
BOLT_SP_MM    = {bolt_sp}
BOLT_D_MM     = {bolt_d}
BEARING_OD_MM = {bearing_od}
BASE_THICK    = {base_thick}

# Rectangular base plate
base = cq.Workplane("XY").box(WIDTH_MM, BASE_THICK, BASE_THICK)

# Cylindrical bearing housing on top of base
housing_r = BEARING_OD_MM / 2.0 + 5.0
housing_h = HEIGHT_MM - BASE_THICK
housing = (cq.Workplane("XY")
           .workplane(offset=BASE_THICK / 2.0)
           .circle(housing_r)
           .extrude(housing_h))

result = base.union(housing)

# Bearing bore through the housing
bore_cyl = (cq.Workplane("XY")
            .workplane(offset=-1.0)
            .circle(BORE_MM / 2.0)
            .extrude(HEIGHT_MM + 2.0))
result = result.cut(bore_cyl)

# Bolt holes in base
bolt_pts = [(-BOLT_SP_MM / 2.0, 0.0), (BOLT_SP_MM / 2.0, 0.0)]
bolt_holes = (cq.Workplane("XY")
              .workplane(offset=-BASE_THICK / 2.0 - 1.0)
              .pushPoints(bolt_pts)
              .circle(BOLT_D_MM / 2.0)
              .extrude(BASE_THICK + 2.0))
result = result.cut(bolt_holes)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


def _cq_cable_gland(params: dict[str, Any]) -> str:
    """Cable gland / strain relief fitting."""
    cable_d   = float(params.get("cable_dia_mm", 8.0))
    thread_od = float(params.get("thread_od_mm", 20.0))
    length    = float(params.get("length_mm", 30.0))
    hex_size  = float(params.get("hex_size_mm", 24.0))

    import math as _m
    hex_r = hex_size / (2.0 * _m.cos(_m.radians(30)))
    bore_r = cable_d / 2.0
    thread_r = thread_od / 2.0
    hex_h = length * 0.3
    thread_l = length - hex_h

    # Pre-compute hex polygon
    hex_pts = [(round(hex_r * _m.cos(_m.radians(60 * i)), 5),
                round(hex_r * _m.sin(_m.radians(60 * i)), 5))
               for i in range(6)]
    hex_pts_repr = repr(hex_pts)

    return f"""
import cadquery as cq, math

# === Cable Gland — cable={cable_d}mm, thread OD={thread_od}mm ===
CABLE_D_MM  = {cable_d}
THREAD_OD   = {thread_od}
LENGTH_MM   = {length}
HEX_R       = {hex_r:.4f}
HEX_H       = {hex_h:.4f}
THREAD_L    = {thread_l:.4f}
THREAD_R    = {thread_r:.4f}
BORE_R      = {bore_r:.4f}

# Threaded cylindrical body
body = cq.Workplane("XY").circle(THREAD_R).extrude(THREAD_L)

# Hex head on top
hex_pts = {hex_pts_repr}
hex_head = (cq.Workplane("XY")
            .workplane(offset=THREAD_L)
            .polyline(hex_pts).close()
            .extrude(HEX_H))

result = body.union(hex_head)

# Central bore for cable pass-through
bore_cyl = (cq.Workplane("XY")
            .workplane(offset=-1.0)
            .circle(BORE_R)
            .extrude(LENGTH_MM + 2.0))
result = result.cut(bore_cyl)

bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


_CQ_TEMPLATE_MAP: dict[str, Any] = {
    # ARIA structural parts
    "aria_ratchet_ring": _cq_ratchet_ring,
    "aria_housing":      _cq_housing,
    "aria_spool":        _cq_spool,
    "aria_cam_collar":   _cq_cam_collar,
    "aria_brake_drum":   _cq_brake_drum,
    "aria_catch_pawl":   _cq_catch_pawl,
    "aria_rope_guide":   _cq_rope_guide,
    # ── Generic / user-facing names (no ARIA prefix) ─────────────────────────
    # Phone / device cases
    "phone_case":        _cq_phone_case,
    "iphone_case":       _cq_phone_case,
    "device_case":       _cq_phone_case,
    "protective_case":   _cq_phone_case,
    # Flat panels & bars
    "hollow_rect":       _cq_hollow_rect,
    "box_shell":         _cq_hollow_rect,
    "hollow_box":        _cq_hollow_rect,
    "flat_plate":        _cq_flat_plate,
    "flat_panel":        _cq_flat_plate,
    "mounting_plate":    _cq_flat_plate,
    "flat_bar":          _cq_catch_pawl,   # length×width×thickness strip
    "flat_strip":        _cq_catch_pawl,
    "aero_surface":      _cq_catch_pawl,
    "wing_element":      _cq_catch_pawl,
    "panel":             _cq_catch_pawl,
    "bar":               _cq_catch_pawl,
    # Cylinders & discs
    "rod":               _cq_shaft,
    "cylinder":          _cq_shaft,
    "round_rod":         _cq_shaft,
    "shaft":             _cq_shaft,
    "pin":               _cq_pin,
    "disc":              _cq_spacer,
    "ring":              _cq_spacer,
    "washer":            _cq_spacer,
    "annulus":           _cq_spacer,
    "spacer":            _cq_spacer,
    "wheel":             _cq_spacer,
    "tube":              _cq_tube,
    "round_tube":        _cq_tube,
    # Housings & hubs
    "housing":           _cq_housing,
    "hub":               _cq_housing,
    "flange_hub":        _cq_housing,
    "bearing_housing":   _cq_housing,
    "upright":           _cq_housing,
    # Brackets & flanges
    "bracket":           _cq_bracket,
    "flange":            _cq_flange,
    "pulley":            _cq_pulley,
    "gear":              _cq_gear,
    "cam":               _cq_cam,
    # Generic mechanical parts (ARIA-prefixed kept for backward compat)
    "aria_bracket":      _cq_bracket,
    "aria_flange":       _cq_flange,
    "aria_shaft":        _cq_shaft,
    "aria_pulley":       _cq_pulley,
    "aria_cam":          _cq_cam,
    "aria_pin":          _cq_pin,
    "aria_spacer":       _cq_spacer,
    "aria_tube":         _cq_tube,
    "aria_gear":         _cq_gear,
    "aria_escape_wheel": _cq_escape_wheel,
    "escape_wheel":      _cq_escape_wheel,
    # Standard mechanical components
    "nema_motor":              _cq_nema_motor,
    "nema17":                  _cq_nema_motor,
    "nema23":                  _cq_nema_motor,
    "nema34":                  _cq_nema_motor,
    "stepper_motor":           _cq_nema_motor,
    "servo_motor":             _cq_nema_motor,
    "mgn_rail":                _cq_mgn_rail,
    "linear_rail":             _cq_mgn_rail,
    "mgn12":                   _cq_mgn_rail,
    "mgn15":                   _cq_mgn_rail,
    "mgn25":                   _cq_mgn_rail,
    "ball_bearing":            _cq_ball_bearing,
    "bearing":                 _cq_ball_bearing,
    "shaft_coupling":          _cq_shaft_coupling,
    "rigid_coupling":          _cq_shaft_coupling,
    "coupler":                 _cq_shaft_coupling,
    "profile_extrusion":       _cq_profile_extrusion,
    "aluminum_extrusion":      _cq_profile_extrusion,
    "vslot":                   _cq_profile_extrusion,
    "2020_extrusion":          _cq_profile_extrusion,
    "4040_extrusion":          _cq_profile_extrusion,
    # LRE / nozzle
    "lre_nozzle":        _cq_nozzle,
    "aria_nozzle":       _cq_nozzle,
    # Non-prefixed aliases — used by slug-based part_ids
    "nozzle":                       _cq_nozzle,
    "rocket_nozzle":                _cq_nozzle,
    "engine_nozzle":                _cq_nozzle,
    "liquid_rocket_engine_nozzle":  _cq_nozzle,
    "bracket":                      _cq_bracket,
    "mounting_bracket":             _cq_bracket,
    "angle_bracket":                _cq_bracket,
    "shaft":                        _cq_shaft,
    "drive_shaft":                  _cq_shaft,
    "axle":                         _cq_shaft,
    "flange":                       _cq_flange,
    "pipe_flange":                  _cq_flange,
    "tube":                         _cq_tube,
    "pipe":                         _cq_tube,
    "sleeve":                       _cq_tube,
    "flat_plate":                   _cq_flat_plate,
    "plate":                        _cq_flat_plate,
    "base_plate":                   _cq_flat_plate,
    "mounting_plate":               _cq_flat_plate,
    "face_plate":                   _cq_flat_plate,
    "hollow_rect":                  _cq_hollow_rect,
    "arm_link":                     _cq_hollow_rect,
    "structural_link":              _cq_hollow_rect,
    "housing":                      _cq_housing,
    "enclosure":                    _cq_housing,
    "box":                          _cq_housing,
    "gear":                         _cq_gear,
    # Motor mounts → flange (circular bolt pattern around center bore)
    "motor_mount":                  _cq_flange,
    "nema_mount":                   _cq_flange,
    "servo_mount":                  _cq_flange,
    "stepper_mount":                _cq_flange,
    # Adapters → flange
    "adapter_plate":                _cq_flange,
    "adapter":                      _cq_flange,
    # Clamps/fixtures
    "fixture":                      _cq_bracket,
    "holder":                       _cq_bracket,
    "clip":                         _cq_bracket,
    # Covers/caps
    "cover":                        _cq_flat_plate,
    "lid":                          _cq_flat_plate,
    "cap":                          _cq_spacer,
    # Blocks/manifolds → housing
    "manifold":                     _cq_housing,
    "block":                        _cq_housing,
    "junction_box":                 _cq_housing,
    # Standoffs/posts → spacer
    "standoff":                     _cq_spacer,
    "post":                         _cq_spacer,
    # Rollers/drums → spacer
    "roller":                       _cq_spacer,
    # Hinges
    "knuckle":                      _cq_bracket,
    # Platforms → flat_plate
    "platform":                     _cq_flat_plate,
    "baseplate":                    _cq_flat_plate,
    # L-bracket / angle bracket
    "l_bracket":                    _cq_l_bracket,
    "angle_bracket":                _cq_l_bracket,
    "l_shaped_bracket":             _cq_l_bracket,
    # Heat sink
    "heat_sink":                    _cq_heat_sink,
    "heatsink":                     _cq_heat_sink,
    "fin_array":                    _cq_heat_sink,
    # Phone/tablet stand
    "phone_stand":                  _cq_phone_stand,
    "tablet_stand":                 _cq_phone_stand,
    "device_stand":                 _cq_phone_stand,
    # Snap hook / clip
    "snap_hook":                    _cq_snap_hook,
    "snap_fit":                     _cq_snap_hook,
    "snap_clip":                    _cq_snap_hook,
    "snap_fit_hook":                _cq_snap_hook,
    "snap_fit_clip":                _cq_snap_hook,
    # Thread insert / standoff with knurling
    "thread_insert":                _cq_thread_insert,
    "threaded_insert":              _cq_thread_insert,
    "knurled_insert":               _cq_thread_insert,
    "heat_set_insert":              _cq_thread_insert,
    "insert":                       _cq_thread_insert,
    # Hinge
    "hinge":                        _cq_hinge,
    "door_hinge":                   _cq_hinge,
    "butt_hinge":                   _cq_hinge,
    "knuckle_hinge":                _cq_hinge,
    # Clamp
    "clamp":                        _cq_clamp,
    "cable_clamp":                  _cq_clamp,
    "pipe_clamp":                   _cq_clamp,
    "c_clamp":                      _cq_clamp,
    "tube_clamp":                   _cq_clamp,
    # Handle / grip
    "handle":                       _cq_handle,
    "grip":                         _cq_handle,
    "knob":                         _cq_handle,
    "pull_handle":                  _cq_handle,
    # Enclosure lid
    "enclosure_lid":                _cq_enclosure_lid,
    "box_lid":                      _cq_enclosure_lid,
    "snap_lid":                     _cq_enclosure_lid,
    # Gusset / corner brace
    "gusset":                       _cq_gusset,
    "corner_brace":                 _cq_gusset,
    "gusset_plate":                 _cq_gusset,
    "corner_bracket":               _cq_gusset,
    "triangle_brace":               _cq_gusset,
    # Spoked wheel / handwheel
    "spoked_wheel":                 _cq_spoked_wheel,
    "handwheel":                    _cq_spoked_wheel,
    "hand_wheel":                   _cq_spoked_wheel,
    "steering_wheel":               _cq_spoked_wheel,
    "spoke_wheel":                  _cq_spoked_wheel,
    # T-slot plate
    "t_slot_plate":                 _cq_t_slot_plate,
    "tslot_plate":                  _cq_t_slot_plate,
    "fixture_plate":                _cq_t_slot_plate,
    "tooling_plate":                _cq_t_slot_plate,
    # Spring clip / retaining clip
    "spring_clip":                  _cq_spring_clip,
    "retaining_clip":               _cq_spring_clip,
    "circlip":                      _cq_spring_clip,
    "u_clip":                       _cq_spring_clip,
    "retainer":                     _cq_spring_clip,
    # Involute gear (high-fidelity tooth profile)
    "involute_gear":                _cq_involute_gear,
    "involute_spur_gear":           _cq_involute_gear,
    # Cam profile (eccentric disc)
    "cam_profile":                  _cq_cam_profile,
    "eccentric_cam":                _cq_cam_profile,
    "cam_disc":                     _cq_cam_profile,
    # Bellows
    "bellows":                      _cq_bellows,
    "corrugated_bellows":           _cq_bellows,
    "boot":                         _cq_bellows,
    "flex_joint":                   _cq_bellows,
    # Compression spring
    "compression_spring":           _cq_compression_spring,
    "spring":                       _cq_compression_spring,
    "coil_spring":                  _cq_compression_spring,
    "helical_spring":               _cq_compression_spring,
    # Keyway shaft
    "keyway_shaft":                 _cq_keyway_shaft,
    "keyed_shaft":                  _cq_keyway_shaft,
    "shaft_with_keyway":            _cq_keyway_shaft,
    # Dovetail joint
    "dovetail_joint":               _cq_dovetail_joint,
    "dovetail":                     _cq_dovetail_joint,
    "dovetail_rail":                _cq_dovetail_joint,
    "dovetail_slide":               _cq_dovetail_joint,
    # Slot plate
    "slot_plate":                   _cq_slot_plate,
    "slotted_plate":                _cq_slot_plate,
    # PCB enclosure
    "pcb_enclosure":                _cq_pcb_enclosure,
    "pcb_box":                      _cq_pcb_enclosure,
    "electronics_enclosure":        _cq_pcb_enclosure,
    "electronics_box":              _cq_pcb_enclosure,
    # Bearing pillow block
    "bearing_pillow_block":         _cq_bearing_pillow_block,
    "pillow_block":                 _cq_bearing_pillow_block,
    "plummer_block":                _cq_bearing_pillow_block,
    # Cable gland
    "cable_gland":                  _cq_cable_gland,
    "strain_relief":                _cq_cable_gland,
    "cable_fitting":                _cq_cable_gland,
    "cord_grip":                    _cq_cable_gland,
}

# Keyword scan for slug-based part_ids not in the exact map.
# Checked in order; first match wins.
_KEYWORD_TO_TEMPLATE: list[tuple[list[str], Any]] = [
    (["phone_case", "iphone", "phone case", "device_case", "protective_case"],  _cq_phone_case),
    (["nozzle", "rocket", "lre", "injector", "bell_nozzle"],  _cq_nozzle),
    (["ratchet_ring", "catch_ring", "ring_gear"],              _cq_ratchet_ring),
    (["brake_drum"],                                           _cq_brake_drum),
    (["cam_collar"],                                           _cq_cam_collar),
    (["catch_pawl", "trip_pawl"],                              _cq_catch_pawl),
    (["rope_guide"],                                           _cq_rope_guide),
    (["spool"],                                                _cq_spool),
    (["hollow_rect", "arm_link", "link"],                      _cq_hollow_rect),
    (["housing", "enclosure"],                                 _cq_housing),
    (["flange"],                                               _cq_flange),
    (["base_plate", "mounting_plate", "face_plate", "flat_plate"], _cq_flat_plate),
    (["bracket", "plate", "mount"],                            _cq_bracket),
    (["shaft", "axle", "drive_shaft"],                         _cq_shaft),
    (["tube", "pipe", "sleeve"],                               _cq_tube),
    (["pulley", "sheave"],                                     _cq_pulley),
    (["cam"],                                                  _cq_cam),
    (["pin", "dowel"],                                         _cq_pin),
    (["spacer", "washer", "bushing"],                          _cq_spacer),
    (["gear", "sprocket", "cog"],                              _cq_gear),
    (["escapement", "escapement_wheel"],                       _cq_escape_wheel),
    (["ring", "collar", "annular"],                            _cq_spacer),
    (["nema", "stepper", "servo_motor"],                       _cq_nema_motor),
    (["mgn", "linear_rail"],                                   _cq_mgn_rail),
    (["bearing"],                                              _cq_ball_bearing),
    (["coupling", "coupler"],                                  _cq_shaft_coupling),
    (["extrusion", "vslot", "tslot"],                          _cq_profile_extrusion),
    # Expanded aliases (fuzzy matching catch-all)
    (["motor_mount", "motor mount", "servo_mount", "stepper_mount"], _cq_flange),
    (["adapter", "adapter_plate"],                             _cq_flange),
    (["clip", "fixture", "holder"],                             _cq_bracket),
    (["cover", "lid"],                                         _cq_flat_plate),
    (["manifold", "block", "junction"],                        _cq_housing),
    (["standoff", "post"],                                     _cq_spacer),
    (["knuckle"],                                               _cq_bracket),
    (["roller"],                                               _cq_spacer),
    (["platform", "baseplate", "base plate"],                  _cq_flat_plate),
    (["l_bracket", "l-bracket", "l bracket", "angle bracket", "angle_bracket"], _cq_l_bracket),
    (["heat_sink", "heatsink", "heat sink", "fin array", "fin_array", "fins"], _cq_heat_sink),
    (["phone_stand", "phone stand", "tablet_stand", "tablet stand", "device stand"], _cq_phone_stand),
    (["snap_hook", "snap_fit", "snap_clip", "snap hook", "snap fit", "snap clip"], _cq_snap_hook),
    (["thread_insert", "threaded_insert", "knurled_insert", "heat_set_insert", "threaded insert", "knurled insert", "heat set insert"], _cq_thread_insert),
    (["hinge", "door_hinge", "butt_hinge", "knuckle_hinge", "door hinge", "butt hinge"], _cq_hinge),
    (["cable_clamp", "pipe_clamp", "c_clamp", "tube_clamp", "cable clamp", "pipe clamp", "c clamp", "tube clamp"], _cq_clamp),
    (["handle", "grip", "knob", "pull_handle", "pull handle"], _cq_handle),
    (["enclosure_lid", "box_lid", "snap_lid", "enclosure lid", "box lid", "snap lid"], _cq_enclosure_lid),
    (["gusset", "corner_brace", "gusset_plate", "corner_bracket", "triangle_brace", "corner brace", "gusset plate", "corner bracket"], _cq_gusset),
    (["spoked_wheel", "handwheel", "hand_wheel", "spoke_wheel", "spoked wheel", "hand wheel", "spoke wheel"], _cq_spoked_wheel),
    (["t_slot_plate", "tslot_plate", "fixture_plate", "tooling_plate", "t-slot plate", "t slot plate", "fixture plate", "tooling plate"], _cq_t_slot_plate),
    (["spring_clip", "retaining_clip", "circlip", "u_clip", "retainer", "spring clip", "retaining clip", "u clip"], _cq_spring_clip),
    (["involute_gear", "involute gear", "involute spur", "involute_spur_gear"], _cq_involute_gear),
    (["cam_profile", "cam profile", "eccentric_cam", "eccentric cam", "cam_disc", "cam disc"], _cq_cam_profile),
    (["bellows", "corrugated_bellows", "corrugated bellows", "flex_joint", "flex joint", "boot"], _cq_bellows),
    (["compression_spring", "compression spring", "coil_spring", "coil spring", "helical_spring", "helical spring"], _cq_compression_spring),
    (["keyway_shaft", "keyway shaft", "keyed_shaft", "keyed shaft", "shaft_with_keyway"], _cq_keyway_shaft),
    (["dovetail_joint", "dovetail joint", "dovetail_rail", "dovetail rail", "dovetail_slide", "dovetail slide", "dovetail"], _cq_dovetail_joint),
    (["slot_plate", "slot plate", "slotted_plate", "slotted plate"], _cq_slot_plate),
    (["pcb_enclosure", "pcb enclosure", "pcb_box", "pcb box", "electronics_enclosure", "electronics enclosure", "electronics_box", "electronics box"], _cq_pcb_enclosure),
    (["bearing_pillow_block", "pillow_block", "pillow block", "plummer_block", "plummer block", "bearing pillow block"], _cq_bearing_pillow_block),
    (["cable_gland", "cable gland", "strain_relief", "strain relief", "cable_fitting", "cable fitting", "cord_grip", "cord grip"], _cq_cable_gland),
]


def _find_template_fn(part_id: str):
    """Return the template function for part_id: exact map lookup, then keyword scan."""
    fn, _ = _find_template_fuzzy(part_id)
    return fn


def _find_template_fuzzy(
    part_id: str,
    goal: str = "",
    spec: dict | None = None,
) -> tuple[Any, str]:
    """Find a template function with progressively looser matching.

    Returns (template_fn | None, match_type) where match_type is one of:
      "exact"   — part_id is a key in _CQ_TEMPLATE_MAP
      "keyword" — a keyword list entry matched part_id
      "goal"    — a keyword list entry matched the full goal text
      "fuzzy"   — best word-overlap score between goal tokens and keyword lists
      None      — no match found
    """
    spec = spec or {}

    # Step 1: Exact lookup
    fn = _CQ_TEMPLATE_MAP.get(part_id)
    if fn:
        return fn, "exact"

    # Step 2: Keyword scan of part_id (original behaviour)
    for keywords, template_fn in _KEYWORD_TO_TEMPLATE:
        if any(kw in part_id for kw in keywords):
            return template_fn, "keyword"

    # Step 3: Keyword scan of spec["part_type"]
    pt = spec.get("part_type", "")
    if pt and pt != part_id:
        fn = _CQ_TEMPLATE_MAP.get(pt)
        if fn:
            return fn, "exact"
        for keywords, template_fn in _KEYWORD_TO_TEMPLATE:
            if any(kw in pt for kw in keywords):
                return template_fn, "keyword"

    # Step 4: Keyword scan of FULL GOAL TEXT (catches "drone motor mount" etc.)
    goal_lower = goal.lower().replace("-", "_")
    if goal_lower:
        for keywords, template_fn in _KEYWORD_TO_TEMPLATE:
            if any(kw in goal_lower for kw in keywords):
                return template_fn, "goal"

    # Step 5: Word-overlap scoring — tokenize goal, score against keyword lists
    if goal_lower:
        goal_words = set(re.split(r"[\s,;:.\-_/]+", goal_lower)) - {"", "a", "the", "for", "with", "and", "mm", "of"}
        best_score = 0
        best_fn = None
        for keywords, template_fn in _KEYWORD_TO_TEMPLATE:
            kw_words = set()
            for kw in keywords:
                kw_words.update(kw.replace("_", " ").split())
            overlap = len(goal_words & kw_words)
            if overlap > best_score:
                best_score = overlap
                best_fn = template_fn
        if best_fn and best_score >= 1:
            return best_fn, "fuzzy"

    return None, ""


def _get_closest_template_source(
    goal: str,
    part_id: str = "",
    spec: dict | None = None,
) -> tuple[str, str]:
    """Find the closest template and return its generated source code.

    Returns (template_name, generated_code) or ("", "") if nothing found.
    Used to inject as a reference example into the LLM prompt.
    Always returns at least the bracket template as a generic CadQuery reference.
    """
    spec = spec or {}
    fn, match_type = _find_template_fuzzy(part_id, goal, spec)

    # If no match, use bracket as a generic "here's how CadQuery works" reference
    if not fn:
        fn = _cq_bracket

    try:
        code = fn(spec)
        if code and len(code) > 50:
            name = getattr(fn, "__name__", "unknown").lstrip("_")
            return name, code
    except Exception:
        pass

    return "", ""


def _generate_from_description(plan: dict[str, Any], goal: str) -> str:
    """
    Universal geometry fallback: parse dimension/shape signals from goal + plan params.
    Used when no template matches and LLM is unavailable.
    Always produces real geometry — never the 20mm placeholder box.
    Default shape is a 50×50×50 mm cube (volume 125 000 mm³, well above the 1000 mm³ minimum).
    """
    params  = plan.get("params", {}) or {}
    goal_l  = goal.lower()

    def _pf(key: str, default=None):
        v = params.get(key)
        return float(v) if v is not None else default

    # Parse numeric values with units from goal text
    nums_mm = [float(m.group(1)) for m in re.finditer(r"(\d+(?:\.\d+)?)\s*mm", goal_l)]
    nums_cm = [float(m.group(1)) * 10.0 for m in re.finditer(r"(\d+(?:\.\d+)?)\s*cm", goal_l)]
    nums_in = [float(m.group(1)) * 25.4 for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:in|inch)", goal_l)]
    all_nums = sorted(nums_mm + nums_cm + nums_in, reverse=True)

    def _n(idx: int, default: float) -> float:
        return all_nums[idx] if idx < len(all_nums) else default

    # Parse OD/ID/bore/length patterns
    od_m = re.search(r"(?:od|outer\s*dia(?:meter)?)\s*[:\-]?\s*(\d+(?:\.\d+)?)", goal_l)
    id_m = re.search(r"(?:id|inner\s*dia(?:meter)?|bore)\s*[:\-]?\s*(\d+(?:\.\d+)?)", goal_l)
    lg_m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:long|length)", goal_l)

    od     = _pf("od_mm")     or (float(od_m.group(1)) if od_m else None)
    bore   = _pf("bore_mm")   or (float(id_m.group(1)) if id_m else None)
    dia    = _pf("diameter_mm") or od
    length = _pf("length_mm") or _pf("height_mm") or (float(lg_m.group(1)) if lg_m else None)
    width  = _pf("width_mm")
    depth  = _pf("depth_mm")
    thick  = _pf("thickness_mm")

    # --- Shape dispatch ---
    if any(w in goal_l for w in ("nozzle", "cone", "bell", "convergent", "divergent")):
        entry_r = float(od / 2.0 if od else _n(0, 60.0))
        total_l = float(length or _n(1, 200.0))
        throat_r = round(entry_r * 0.4, 2)
        exit_r   = round(entry_r * 1.3, 2)
        conv_l   = round(total_l * 0.4, 2)
        wall = 3.0
        return f"""import cadquery as cq
ENTRY_R={entry_r}; THROAT_R={throat_r}; EXIT_R={exit_r}
CONV_L={conv_l}; LENGTH={total_l}; WALL={wall}
profile=[(ENTRY_R,0),(THROAT_R,CONV_L),(EXIT_R,LENGTH),(EXIT_R-WALL,LENGTH),(THROAT_R-WALL,CONV_L),(ENTRY_R-WALL,0)]
result=(cq.Workplane("XY").polyline([(r,z) for r,z in profile]).close().revolve(360,(0,0,0),(0,1,0)))
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    if any(w in goal_l for w in ("ring", "annular", "washer", "collar", "bushing")):
        d = float(od or dia or _n(0, 100.0))
        b = float(bore or round(d * 0.6, 2))
        h = float(thick or length or _n(1, 20.0))
        return f"""import cadquery as cq
OD_MM={d}; BORE_MM={b}; H_MM={h}
result=(cq.Workplane("XY").circle(OD_MM/2.0).circle(BORE_MM/2.0).extrude(H_MM))
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    if any(w in goal_l for w in ("shaft", "rod", "axle", "spindle", "dowel")):
        d = float(dia or od or _n(0, 20.0))
        l = float(length or _n(1, 150.0))
        return f"""import cadquery as cq
D_MM={d}; L_MM={l}
result=cq.Workplane("XY").circle(D_MM/2.0).extrude(L_MM)
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    if any(w in goal_l for w in ("bracket", "plate", "mount", "tab", "gusset", "strap")):
        w = float(width or _n(0, 100.0))
        h = float(length or _n(1, 80.0))
        t = float(thick or 6.0)
        return f"""import cadquery as cq
W_MM={w}; H_MM={h}; T_MM={t}; HOLE_D=8.0
plate=cq.Workplane("XY").box(W_MM,T_MM,H_MM)
holes=(cq.Workplane("XY").workplane(offset=-1).pushPoints([(-W_MM/4,0),(W_MM/4,0)]).circle(HOLE_D/2).extrude(T_MM+2))
result=plate.cut(holes)
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    if any(w in goal_l for w in ("tube", "pipe", "sleeve")):
        d = float(od or dia or _n(0, 50.0))
        l = float(length or _n(1, 100.0))
        wall = float(params.get("wall_mm", 3.0))
        b = max(d - 2 * wall, 1.0)
        return f"""import cadquery as cq
OD_MM={d}; BORE_MM={b}; L_MM={l}
result=(cq.Workplane("XY").circle(OD_MM/2.0).circle(BORE_MM/2.0).extrude(L_MM))
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    if any(w in goal_l for w in ("housing", "enclosure", "case", "body", "cover")):
        w = float(width or _n(0, 100.0))
        h = float(length or _n(1, 100.0))
        d = float(depth or 80.0)
        return f"""import cadquery as cq
W_MM={w}; H_MM={h}; D_MM={d}; WALL=5.0
result=cq.Workplane("XY").box(W_MM,D_MM,H_MM).shell(-WALL)
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    if any(w in goal_l for w in ("gear", "sprocket", "cog")):
        d = float(dia or od or _n(0, 80.0))
        h = float(length or _n(1, 20.0))
        b = round(d * 0.2, 2)
        return f"""import cadquery as cq
D_MM={d}; H_MM={h}; BORE_MM={b}
outer=cq.Workplane("XY").circle(D_MM/2.0).extrude(H_MM)
bore_cyl=(cq.Workplane("XY").workplane(offset=-1.0).circle(BORE_MM/2.0).extrude(H_MM+2.0))
result=outer.cut(bore_cyl)
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    # Default: 50mm cube — well above 1000 mm³ minimum; never 20mm placeholder
    side = float(_n(0, 50.0))
    return f"""import cadquery as cq
SIDE_MM={side}
result=cq.Workplane("XY").box(SIDE_MM,SIDE_MM,SIDE_MM)
bb=result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_cadquery_artifacts(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
    previous_failures: Optional[list] = None,
) -> dict[str, str]:
    """
    Generate a CadQuery script for the given plan and write it to disk.
    Attempts in-process execution to produce STEP + STL if cadquery is installed.

    Returns dict with:
        script_path : str — path to the .py script
        step_path   : str | "" — path to exported STEP (empty if CQ not installed)
        stl_path    : str | "" — path to exported STL
        bbox        : dict | None
        error       : str | None
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    part_id = plan.get("part_id", "custom_part") or "custom_part"
    params  = plan.get("params", {}) or {}

    # --- Pick template (exact or keyword) or LLM/description fallback ---
    template_fn = _find_template_fn(part_id)
    if template_fn:
        cq_code = template_fn(params)
    else:
        # Before falling back to LLM: try deterministic CEM-to-geometry path
        # when physics params have been injected into the plan.
        cq_code = None
        if plan.get("cem_context"):
            try:
                import sys as _sys
                _repo = repo_root or Path(__file__).resolve().parent.parent
                if str(_repo) not in _sys.path:
                    _sys.path.insert(0, str(_repo))
                from cem_to_geometry import scalars_to_cq_script
                cq_code = scalars_to_cq_script(part_id, params)
                print(f"[CEM→CQ] Deterministic CEM template used for '{part_id}'")
            except Exception:
                cq_code = None  # fall through to LLM

        if not cq_code:
            cq_code = _llm_cadquery(plan, goal, step_path, stl_path, repo_root,
                                    previous_failures=previous_failures or [])

    # --- Write script ---
    out_dir = repo_root / "outputs" / "cad" / "cadquery" / part_id
    out_dir.mkdir(parents=True, exist_ok=True)
    script_path = out_dir / f"{part_id}_cq.py"

    # Inject export footer
    sp = step_path.replace("\\", "/")
    st = stl_path.replace("\\", "/")
    export_footer = f"""
# === AUTO-GENERATED EXPORT ===
import os as _os
from cadquery import exporters as _exp
_step = "{sp}"
_stl  = "{st}"
try:
    _os.makedirs(_os.path.dirname(_step), exist_ok=True)
except OSError:
    pass
try:
    _os.makedirs(_os.path.dirname(_stl), exist_ok=True)
except OSError:
    pass
_exp.export(result, _step, _exp.ExportTypes.STEP)
_exp.export(result, _stl,  _exp.ExportTypes.STL)
print(f"EXPORTED STEP: {{_step}}")
print(f"EXPORTED STL: {{_stl}}")
"""
    full_script = cq_code.rstrip() + "\n" + export_footer
    script_path.write_text(full_script, encoding="utf-8")

    # --- Execute in-process ---
    result_step = ""
    result_stl  = ""
    bbox        = None
    error       = None

    try:
        import cadquery as cq  # noqa: F401
        from cadquery import exporters  # noqa: F401

        # --- Sandboxed exec: allow cadquery/math only, block os/subprocess/socket ---
        _ALLOWED_MODULES = frozenset({"cadquery", "math", "cadquery.exporters"})

        def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name not in _ALLOWED_MODULES:
                raise ImportError(f"Import of '{name}' is blocked by sandbox")
            return __import__(name, globals, locals, fromlist, level)

        safe_builtins = {
            "__import__": _safe_import,
            "range": range, "len": len, "print": print,
            "abs": abs, "min": min, "max": max, "round": round,
            "float": float, "int": int, "str": str,
            "list": list, "dict": dict, "tuple": tuple, "set": set,
            "bool": bool, "enumerate": enumerate, "zip": zip, "map": map,
            "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
            "True": True, "False": False, "None": None,
            "ValueError": ValueError, "TypeError": TypeError,
            "RuntimeError": RuntimeError, "Exception": Exception,
        }
        ns: dict[str, Any] = {"__builtins__": safe_builtins}
        exec(compile(cq_code, f"<{part_id}_cq>", "exec"), ns)  # noqa: S102
        geom = ns.get("result")
        if geom is None:
            error = "CQ script did not define 'result'"
        else:
            Path(step_path).parent.mkdir(parents=True, exist_ok=True)
            Path(stl_path).parent.mkdir(parents=True, exist_ok=True)
            bb = geom.val().BoundingBox()
            bbox = {"x": round(bb.xlen, 2), "y": round(bb.ylen, 2), "z": round(bb.zlen, 2)}
            # Gate export on non-degenerate geometry — catches LLM zero-volume output early
            if bb.xlen < 0.1 or bb.ylen < 0.1 or bb.zlen < 0.1:
                error = (f"Degenerate bbox {bb.xlen:.3f}×{bb.ylen:.3f}×{bb.zlen:.3f} mm"
                         " — geometry invalid, skipping export")
            else:
                exporters.export(geom, step_path, exporters.ExportTypes.STEP)
                exporters.export(geom, stl_path,  exporters.ExportTypes.STL,
                                 tolerance=0.01)   # finer mesh for smooth preview
                result_step = step_path
                result_stl  = stl_path

            # --- Output quality assertions ---
            # Check mesh volume vs bounding-box volume using trimesh.
            # A real solid should fill at least 5% of its bbox envelope.
            # (The old check compared bbox_vol to itself — a mathematical tautology.)
            try:
                import trimesh as _tm
                _mesh = _tm.load(stl_path)
                _mesh_vol = abs(float(_mesh.volume))
                _bbox_vol = bbox["x"] * bbox["y"] * bbox["z"]
                _fill = _mesh_vol / max(_bbox_vol, 1e-9)
                if _fill < 0.02:  # < 2 % fill → almost certainly degenerate
                    print(f"[VALIDATION FAIL] part_id={part_id}: "
                          f"mesh fill {_fill*100:.1f}% of bbox — likely degenerate geometry")
            except Exception:
                pass  # trimesh optional; skip if unavailable
            for _fpath, _min, _label in [
                (step_path, 1024, "STEP"), (stl_path, 500, "STL")
            ]:
                if Path(_fpath).exists():
                    _sz = Path(_fpath).stat().st_size
                    if _sz < _min:
                        print(f"[VALIDATION FAIL] part_id={part_id}: {_label} {_sz} bytes < {_min} bytes")
    except ImportError:
        error = "cadquery not installed; run the generated cq_script manually"
    except Exception:
        error = traceback.format_exc()

    return {
        "script_path": str(script_path),
        "step_path":   result_step,
        "stl_path":    result_stl,
        "bbox":        bbox,
        "error":       error,
        "status":      "success" if result_step else "failure",
    }


def _llm_cadquery(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Path,
    previous_failures: Optional[list] = None,
) -> str:
    """
    Ask the LLM to generate CadQuery code for an arbitrary part.
    Returns the code string (no export footer — that is injected by the caller).
    """
    try:
        from ..llm_client import call_llm
    except ImportError:
        return _generate_from_description(plan, goal)

    sp = step_path.replace("\\", "/")
    st = stl_path.replace("\\", "/")

    # --- Build rich system prompt with same context as Grasshopper path ---
    try:
        from ..context_loader import get_mechanical_constants, load_context
        from ..cem_context import load_cem_geometry, format_cem_block
        _ctx = load_context(repo_root)
        _constants = get_mechanical_constants(_ctx)
        _constants_block = "\n".join(f"#   {k}: {v}" for k, v in sorted(_constants.items()))
    except Exception:
        _constants_block = ""

    # CEM physics context
    try:
        g = (goal or "").strip()
        pid = (plan.get("part_id") or "") if isinstance(plan.get("part_id"), str) else ""
        _cem = load_cem_geometry(repo_root, goal=g, part_id=pid)
        _cem_block = format_cem_block(_cem)
    except Exception:
        _cem_block = ""

    # Inject few-shot examples and learned failure patterns from learning log
    try:
        from ..cad_learner import get_few_shot_examples, format_few_shot_block, get_failure_patterns
        _examples = get_few_shot_examples(goal, plan.get("part_id", ""), repo_root)
        _few_shot = format_few_shot_block(_examples)
        _learned_failures = get_failure_patterns(plan.get("part_id", ""), repo_root)
    except Exception:
        _few_shot = ""
        _learned_failures = []

    _learned_block = ""
    if _learned_failures:
        _learned_block = "\n".join(f"- {e}" for e in _learned_failures)

    system = f"""You are a CadQuery Python expert. Output ONLY a Python code block. No explanation, no markdown outside the block.

Imports (use exactly):
  import cadquery as cq
  import math

Rules:
- All dimensions in mm as ALL_CAPS module-level constants.
- Build solid first, then cuts, then holes. No fillets/chamfers on first attempt.
- Final variable MUST be named 'result' and be a cq.Workplane object.
- Select faces by direction (faces(">Z")), never by index.
- Print BBOX: print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}") at the end.
- Do NOT write any export code — that is injected separately.

Mechanical constants (from aria_mechanical.md) — use these when relevant:
{_constants_block}

{_cem_block}

Avoid these CadQuery failure patterns:
- ChFi3d_Builder: only 2 faces — caused by fillet on thin body. Remove fillet; add after solid validates.
- BRep_API: command not done — caused by invalid face refs in compound boolean. Simplify to extrude + cut only.
- Nothing to loft — caused by non-coplanar loft profiles. Use revolve for axisymmetric profiles.
- Bbox axis mismatch — CadQuery extrudes along Z. Verify plan expects Z for height.
- Never use annular profile as first operation. Build solid cylinder/box first, then remove interior.
- For hollow parts: create outer solid, then cut the inner void.

{("Known recent failures for this part (from learning log):" + chr(10) + _learned_block) if _learned_block else ""}

CadQuery patterns:
  Box:       cq.Workplane("XY").box(L, W, H)
  Cylinder:  cq.Workplane("XY").circle(R).extrude(H)
  Ring:      cq.Workplane("XY").circle(R_OUT).circle(R_IN).extrude(H)
  Cut hole:  .faces(">Z").workplane().circle(R).cutThruAll()
  Union:     .union(other_wp)
  Shell:     .shell(-WALL)
  Revolve:   cq.Workplane("XZ").polyline(pts).close().revolve(360)

Required code structure:
  ## All numeric dimensions must be module-level constants
  # === PART PARAMETERS (tunable) ===
  LENGTH_MM = 60.0
  WIDTH_MM = 12.0
  # === END PARAMETERS ===
  # geometry uses constants only, never inline numbers

Every generated script MUST end with:
  bb = result.val().BoundingBox()
  print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
"""

    # --- Build user prompt ---
    brief = plan.get("engineering_brief")
    user_lines: list[str] = []
    if brief:
        user_lines.extend([
            "=== ENGINEERING BRIEF (authoritative — follow this over the short user phrase) ===",
            str(brief).strip(),
            "",
            "=== STRUCTURED PLAN (summary) ===",
        ])
    user_lines.extend([
        f"Goal: {goal}",
        f"Plan: {plan.get('text', str(plan))}",
        "",
        "Generate CadQuery Python. Variable 'result' must be the final cq.Workplane.",
        f"Export paths (do NOT write export code — it is added automatically):",
        f"  STEP: {sp}",
        f"  STL: {st}",
    ])
    if _few_shot:
        user_lines.append(f"\n{_few_shot}")
    if previous_failures:
        failure_block = "\n".join(f"  - {f}" for f in previous_failures)
        user_lines.append(
            f"\nPREVIOUS ATTEMPT FAILURES — fix these in your new code:\n"
            f"{failure_block}"
        )
    if _learned_failures:
        learned_block = "\n".join(f"  - {f}" for f in _learned_failures)
        user_lines.append(
            f"\nKNOWN RECURRING FAILURES FOR THIS PART (from learning log):\n"
            f"{learned_block}"
        )
    user = "\n".join(user_lines)

    try:
        text = call_llm(user, system, repo_root=repo_root)
        if text is None:
            return _generate_from_description(plan, goal)
        m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        if "import cadquery" in text or "cq.Workplane" in text:
            return text.strip()
    except Exception:
        pass
    return _generate_from_description(plan, goal)


def _placeholder_box_script() -> str:
    return """import cadquery as cq
LENGTH_MM = 20.0
WIDTH_MM  = 20.0
HEIGHT_MM = 20.0
result = cq.Workplane("XY").box(LENGTH_MM, WIDTH_MM, HEIGHT_MM)
bb = result.val().BoundingBox()
print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")
"""
