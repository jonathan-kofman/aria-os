"""
Internal helpers for CadQuery geometry generation inside the components package.

Keeps the component modules focused on data (designation, dimensions, cost)
while geometry is built here with consistent patterns.
"""
from __future__ import annotations

import math
from typing import Any


def _export_step(solid, output_path: str) -> str:
    """Export a CadQuery Workplane/Shape to STEP. Returns output_path."""
    import cadquery as cq
    cq.exporters.export(solid, output_path, exportType="STEP")
    return output_path


def _hex_polygon(r: float, n: int = 6):
    """Return list of (x, y) points for a regular n-gon inscribed in radius r."""
    return [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n))
            for i in range(n)]


# ---------------------------------------------------------------------------
# Bolt (socket head cap screw) — ISO 4762 style
# ---------------------------------------------------------------------------

def generate_socket_head_bolt(
    thread_dia_mm: float,
    length_mm: float,
    head_dia_mm: float,
    head_height_mm: float,
    socket_dia_mm: float,
    socket_depth_mm: float,
    *,
    output_path: str,
) -> str:
    """ISO 4762 socket head cap screw. Returns STEP path.

    Geometry: cylindrical head with hex socket + threaded shaft (modeled as
    smooth cylinder — real threads are purely cosmetic in CAD assemblies).
    """
    import cadquery as cq

    # Head
    head = cq.Workplane("XY").circle(head_dia_mm / 2).extrude(head_height_mm)
    # Hex socket (inset from top)
    hex_points = _hex_polygon(socket_dia_mm / 2, 6)
    head = (head.faces(">Z").workplane()
                .polyline(hex_points).close()
                .cutBlind(-socket_depth_mm))
    # Shaft — extrudes downward from head
    shaft = cq.Workplane("XY").circle(thread_dia_mm / 2).extrude(-length_mm)
    result = head.union(shaft)
    return _export_step(result, output_path)


# ---------------------------------------------------------------------------
# Hex nut — ISO 4032
# ---------------------------------------------------------------------------

def generate_hex_nut(
    thread_dia_mm: float,
    across_flats_mm: float,
    thickness_mm: float,
    *,
    output_path: str,
) -> str:
    import cadquery as cq

    radius = across_flats_mm / math.cos(math.pi / 6) / 2  # across-corners / 2
    hex_points = _hex_polygon(radius, 6)
    nut = (cq.Workplane("XY")
              .polyline(hex_points).close()
              .extrude(thickness_mm))
    # Through hole
    result = nut.faces(">Z").workplane().hole(thread_dia_mm)
    return _export_step(result, output_path)


# ---------------------------------------------------------------------------
# Flat washer — ISO 7089
# ---------------------------------------------------------------------------

def generate_flat_washer(
    bore_dia_mm: float,
    od_mm: float,
    thickness_mm: float,
    *,
    output_path: str,
) -> str:
    import cadquery as cq

    washer = (cq.Workplane("XY")
                 .circle(od_mm / 2)
                 .circle(bore_dia_mm / 2)
                 .extrude(thickness_mm))
    return _export_step(washer, output_path)


# ---------------------------------------------------------------------------
# Deep-groove ball bearing — simplified
# ---------------------------------------------------------------------------

def generate_deep_groove_bearing(
    bore_mm: float,
    od_mm: float,
    width_mm: float,
    *,
    output_path: str,
) -> str:
    """Simplified bearing — concentric rings representing inner + outer race.
    Good enough for assembly fit-checks; not a detailed bearing model.
    """
    import cadquery as cq

    inner_race_od = bore_mm + (od_mm - bore_mm) * 0.25
    outer_race_id = od_mm - (od_mm - bore_mm) * 0.25

    outer_race = (cq.Workplane("XY")
                    .circle(od_mm / 2)
                    .circle(outer_race_id / 2)
                    .extrude(width_mm))
    inner_race = (cq.Workplane("XY")
                    .circle(inner_race_od / 2)
                    .circle(bore_mm / 2)
                    .extrude(width_mm))
    result = outer_race.union(inner_race)
    return _export_step(result, output_path)


# ---------------------------------------------------------------------------
# NEMA stepper motor — simplified body shape
# ---------------------------------------------------------------------------

def generate_nema_stepper(
    frame_size_mm: float,
    body_length_mm: float,
    shaft_dia_mm: float,
    shaft_length_mm: float,
    mount_bolt_dia_mm: float,
    mount_pcd_mm: float,
    pilot_dia_mm: float,
    pilot_height_mm: float,
    *,
    output_path: str,
) -> str:
    """NEMA-standard stepper motor body. Square frame + cylindrical pilot + shaft."""
    import cadquery as cq

    # Body — square frame
    body = (cq.Workplane("XY")
               .rect(frame_size_mm, frame_size_mm)
               .extrude(-body_length_mm))

    # Front face pilot (concentric with shaft)
    pilot = (cq.Workplane("XY")
                .circle(pilot_dia_mm / 2)
                .extrude(pilot_height_mm))

    # Shaft
    shaft = (cq.Workplane("XY")
                .workplane(offset=pilot_height_mm)
                .circle(shaft_dia_mm / 2)
                .extrude(shaft_length_mm))

    # Mounting holes on front face
    body_with_mounts = body.faces("<Z").workplane()
    pcd_r = mount_pcd_mm / 2
    hole_positions = [(pcd_r, pcd_r), (-pcd_r, pcd_r),
                      (pcd_r, -pcd_r), (-pcd_r, -pcd_r)]
    for x, y in hole_positions:
        body_with_mounts = body_with_mounts.moveTo(x, y).hole(mount_bolt_dia_mm)

    result = body_with_mounts.union(pilot).union(shaft)
    return _export_step(result, output_path)


# ---------------------------------------------------------------------------
# Rigid shaft coupling — clamping style
# ---------------------------------------------------------------------------

def generate_rigid_coupling(
    bore_a_mm: float,
    bore_b_mm: float,
    od_mm: float,
    length_mm: float,
    *,
    output_path: str,
) -> str:
    import cadquery as cq

    body = (cq.Workplane("XY")
               .circle(od_mm / 2)
               .extrude(length_mm))
    # Two through bores (top half and bottom half)
    half = length_mm / 2
    result = (body
              .faces(">Z").workplane()
              .hole(bore_a_mm, depth=half)
              .faces("<Z").workplane()
              .hole(bore_b_mm, depth=half))
    return _export_step(result, output_path)


# ---------------------------------------------------------------------------
# Flexible coupling (beam/spiral style — simplified cylinder)
# ---------------------------------------------------------------------------

def generate_flexible_coupling(
    bore_a_mm: float,
    bore_b_mm: float,
    od_mm: float,
    length_mm: float,
    *,
    output_path: str,
) -> str:
    """Beam-style flexible coupling.

    Approximated as a cylinder with annular grooves. Real beam couplings have
    a helical slot — we model the visual effect with rings rather than radial
    cuts because the radial-cut approach (.faces(">X")) becomes ambiguous after
    the first cut splits the cylindrical face.
    """
    import cadquery as cq

    body = (cq.Workplane("XY")
               .circle(od_mm / 2)
               .extrude(length_mm))

    # Annular grooves at three locations along the length
    groove_depth = od_mm * 0.15
    groove_width = max(1.0, length_mm * 0.04)
    grooved = body
    for z_frac in (0.3, 0.5, 0.7):
        z = length_mm * z_frac
        groove = (cq.Workplane("XY")
                    .workplane(offset=z - groove_width / 2)
                    .circle(od_mm / 2)
                    .circle(od_mm / 2 - groove_depth)
                    .extrude(groove_width))
        grooved = grooved.cut(groove)

    half = length_mm / 2
    result = (grooved
              .faces(">Z").workplane()
              .hole(bore_a_mm, depth=half)
              .faces("<Z").workplane()
              .hole(bore_b_mm, depth=half))
    return _export_step(result, output_path)


# ---------------------------------------------------------------------------
# Dowel pin — simple ground cylinder
# ---------------------------------------------------------------------------

def generate_dowel_pin(
    diameter_mm: float,
    length_mm: float,
    *,
    output_path: str,
) -> str:
    import cadquery as cq

    pin = cq.Workplane("XY").circle(diameter_mm / 2).extrude(length_mm)
    return _export_step(pin, output_path)


# ---------------------------------------------------------------------------
# Retaining ring (external, E-clip style — simplified flat ring with slot)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Linear rail (MGN/HGH style) — profile extrusion
# ---------------------------------------------------------------------------

def generate_linear_rail(
    width_mm: float,
    height_mm: float,
    length_mm: float,
    bolt_pitch_mm: float,
    bolt_dia_mm: float,
    *,
    output_path: str,
) -> str:
    """Linear rail — rectangular profile with countersunk mounting holes along the top."""
    import cadquery as cq

    rail = cq.Workplane("XY").rect(width_mm, length_mm).extrude(height_mm)
    # Holes on top face at regular pitch
    n_holes = max(1, int(length_mm // bolt_pitch_mm))
    start = -(n_holes - 1) * bolt_pitch_mm / 2
    for i in range(n_holes):
        y = start + i * bolt_pitch_mm
        rail = (rail.faces(">Z").workplane()
                    .moveTo(0, y).hole(bolt_dia_mm))
    return _export_step(rail, output_path)


# ---------------------------------------------------------------------------
# Linear carriage (block) — slides on a rail
# ---------------------------------------------------------------------------

def generate_linear_carriage(
    width_mm: float,
    length_mm: float,
    height_mm: float,
    mount_pattern_x_mm: float,
    mount_pattern_y_mm: float,
    mount_bolt_dia_mm: float,
    rail_slot_width_mm: float,
    *,
    output_path: str,
) -> str:
    """Linear motion carriage block."""
    import cadquery as cq

    carriage = cq.Workplane("XY").rect(width_mm, length_mm).extrude(height_mm)
    # Slot for rail underneath
    slot = (cq.Workplane("XY").rect(rail_slot_width_mm, length_mm + 2)
              .extrude(height_mm * 0.4).translate((0, 0, 0)))
    carriage = carriage.cut(slot)
    # 4 mounting holes on top
    for dx in (-mount_pattern_x_mm / 2, mount_pattern_x_mm / 2):
        for dy in (-mount_pattern_y_mm / 2, mount_pattern_y_mm / 2):
            carriage = (carriage.faces(">Z").workplane()
                         .moveTo(dx, dy).hole(mount_bolt_dia_mm))
    return _export_step(carriage, output_path)


# ---------------------------------------------------------------------------
# Ballscrew — threaded rod with end journals
# ---------------------------------------------------------------------------

def generate_ballscrew(
    screw_dia_mm: float,
    lead_mm: float,
    total_length_mm: float,
    end_journal_dia_mm: float,
    end_journal_length_mm: float,
    *,
    output_path: str,
) -> str:
    """Simplified ballscrew — smooth rod with end journals."""
    import cadquery as cq

    body_length = total_length_mm - 2 * end_journal_length_mm
    screw = cq.Workplane("XY").circle(screw_dia_mm / 2).extrude(body_length)
    # End journals (top and bottom)
    j_top = (cq.Workplane("XY").workplane(offset=body_length)
               .circle(end_journal_dia_mm / 2).extrude(end_journal_length_mm))
    j_bot = (cq.Workplane("XY").workplane(offset=0)
               .circle(end_journal_dia_mm / 2).extrude(-end_journal_length_mm))
    return _export_step(screw.union(j_top).union(j_bot), output_path)


# ---------------------------------------------------------------------------
# Ballscrew nut
# ---------------------------------------------------------------------------

def generate_ballscrew_nut(
    bore_dia_mm: float,
    flange_dia_mm: float,
    body_dia_mm: float,
    total_length_mm: float,
    flange_thickness_mm: float,
    flange_bolt_pcd_mm: float,
    flange_bolt_dia_mm: float,
    n_bolts: int,
    *,
    output_path: str,
) -> str:
    """Flanged ballscrew nut."""
    import cadquery as cq
    import math as _math

    body = (cq.Workplane("XY").circle(body_dia_mm / 2)
              .extrude(total_length_mm - flange_thickness_mm))
    flange = (cq.Workplane("XY").workplane(offset=total_length_mm - flange_thickness_mm)
                .circle(flange_dia_mm / 2).extrude(flange_thickness_mm))
    nut = body.union(flange)
    # Through bore
    nut = nut.faces(">Z").workplane().hole(bore_dia_mm)
    # Flange bolt circle. Limit hole depth to flange thickness — by default
    # `.hole()` drills through the entire part, but bolt holes at PCD often
    # sit OUTSIDE the body radius and only need to clear the flange. Drilling
    # full-depth makes the cutter graze/intersect the body's outer cylindrical
    # wall, producing degenerate booleans → non-manifold edges → non-watertight
    # mesh. Caught by the contract validator on SFU-series ballscrew nuts.
    for i in range(n_bolts):
        angle = 2 * _math.pi * i / n_bolts
        x = (flange_bolt_pcd_mm / 2) * _math.cos(angle)
        y = (flange_bolt_pcd_mm / 2) * _math.sin(angle)
        nut = (nut.faces(">Z").workplane()
                 .moveTo(x, y).hole(flange_bolt_dia_mm, depth=flange_thickness_mm))
    return _export_step(nut, output_path)


# ---------------------------------------------------------------------------
# GT2 timing belt pulley
# ---------------------------------------------------------------------------

def generate_gt2_pulley(
    n_teeth: int,
    bore_mm: float,
    belt_width_mm: float,
    flange_dia_mm: float,
    total_length_mm: float,
    *,
    output_path: str,
) -> str:
    """GT2 timing pulley — approximated as a cylinder with flanges."""
    import cadquery as cq

    # GT2 tooth pitch = 2mm, so pitch diameter = 2*n_teeth/pi
    import math as _math
    pitch_dia = 2.0 * n_teeth / _math.pi
    body_dia = pitch_dia + 0.5  # tips ~0.25mm above pitch circle

    flange_thickness = 1.5
    core_length = belt_width_mm + 1.0
    total_length = max(total_length_mm, core_length + 2 * flange_thickness)

    pulley = cq.Workplane("XY").circle(body_dia / 2).extrude(total_length)
    # Two flanges
    for z in (flange_thickness, total_length - flange_thickness):
        flange = (cq.Workplane("XY").workplane(offset=z)
                    .circle(flange_dia_mm / 2).circle(body_dia / 2)
                    .extrude(flange_thickness))
        pulley = pulley.union(flange)
    # Through bore
    pulley = pulley.faces(">Z").workplane().hole(bore_mm)
    return _export_step(pulley, output_path)


# ---------------------------------------------------------------------------
# BLDC motor (outrunner) — for drones, small robots
# ---------------------------------------------------------------------------

def generate_bldc_outrunner(
    stator_od_mm: float,
    can_od_mm: float,
    height_mm: float,
    shaft_dia_mm: float,
    shaft_length_mm: float,
    mount_pcd_mm: float,
    mount_bolt_dia_mm: float,
    n_mount_bolts: int,
    *,
    output_path: str,
) -> str:
    """Outrunner BLDC motor — hollow can spinning around central stator."""
    import cadquery as cq
    import math as _math

    # Can (outer rotating part)
    can = cq.Workplane("XY").circle(can_od_mm / 2).extrude(height_mm)
    # Mounting base plate below
    base_thick = 3.0
    base = (cq.Workplane("XY").workplane(offset=-base_thick)
              .circle(stator_od_mm / 2 + 2).extrude(base_thick))
    # Shaft protruding through top
    shaft = (cq.Workplane("XY").workplane(offset=height_mm)
                .circle(shaft_dia_mm / 2).extrude(shaft_length_mm))
    motor = can.union(base).union(shaft)
    # Mount holes on base
    for i in range(n_mount_bolts):
        angle = 2 * _math.pi * i / n_mount_bolts
        x = (mount_pcd_mm / 2) * _math.cos(angle)
        y = (mount_pcd_mm / 2) * _math.sin(angle)
        motor = (motor.faces("<Z").workplane()
                   .moveTo(x, y).hole(mount_bolt_dia_mm))
    return _export_step(motor, output_path)


# ---------------------------------------------------------------------------
# Propeller (2-blade) — simplified flat airfoil
# ---------------------------------------------------------------------------

def generate_propeller(
    diameter_mm: float,
    pitch_mm: float,
    hub_dia_mm: float,
    hub_height_mm: float,
    shaft_dia_mm: float,
    n_blades: int = 2,
    *,
    output_path: str,
) -> str:
    """Simplified fixed-pitch propeller.

    Pitch is encoded in the twist angle of each blade. Blades are flat plates
    approximating a cambered airfoil (good enough for visual assembly checks).
    """
    import cadquery as cq
    import math as _math

    radius = diameter_mm / 2
    hub = (cq.Workplane("XY").circle(hub_dia_mm / 2).extrude(hub_height_mm))
    # Blades: flat plates rotated around Z, twisted based on pitch
    blade_chord = radius * 0.15
    blade_thickness = max(1.0, radius * 0.02)
    blade_length = radius - hub_dia_mm / 2
    # Approximate pitch angle at 75% radius (conventional reference)
    ref_r = 0.75 * radius
    pitch_angle_deg = _math.degrees(_math.atan2(pitch_mm, 2 * _math.pi * ref_r))

    # Cut the shaft bore in the hub BEFORE adding blades — once blades are
    # unioned in, .faces(">Z") would also select blade-top faces and the
    # selection becomes non-coplanar.
    hub = hub.faces(">Z").workplane().hole(shaft_dia_mm)

    prop = hub
    for i in range(n_blades):
        phi = 360 * i / n_blades
        blade = (cq.Workplane("XY")
                   .rect(blade_chord, blade_thickness)
                   .extrude(blade_length))
        blade = blade.rotate((0, 0, 0), (1, 0, 0), pitch_angle_deg)
        blade = blade.rotate((0, 0, 0), (0, 0, 1), phi)
        blade = blade.translate(
            ( (hub_dia_mm / 2 + blade_length / 2) * _math.cos(_math.radians(phi)),
              (hub_dia_mm / 2 + blade_length / 2) * _math.sin(_math.radians(phi)),
              hub_height_mm / 2 ))
        prop = prop.union(blade)
    return _export_step(prop, output_path)


def generate_retaining_ring(
    shaft_dia_mm: float,
    thickness_mm: float,
    *,
    output_path: str,
) -> str:
    """External retaining ring. Simplified as a flat washer with a radial slot."""
    import cadquery as cq

    od = shaft_dia_mm * 1.8
    bore = shaft_dia_mm * 0.93  # grips slightly undersize
    ring = (cq.Workplane("XY")
              .circle(od / 2)
              .circle(bore / 2)
              .extrude(thickness_mm))
    # Radial slot on one side (for installation tool)
    slot = (cq.Workplane("XY")
               .rect(od * 0.3, shaft_dia_mm * 0.3)
               .extrude(thickness_mm)
               .translate((od * 0.3, 0, 0)))
    result = ring.cut(slot)
    return _export_step(result, output_path)
