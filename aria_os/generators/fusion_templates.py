"""
aria_os/generators/fusion_templates.py

Parametric Fusion 360 script templates for complex part families.

Each template function returns a Python code string ready for exec() inside
Fusion 360.  The LLM fills in dimensions only --- the script structure is
hand-written and known-good.

Pre-defined in the exec() environment:
    app, ui, design, rootComp, adsk, math

All dimensions are in CM.  Design is already ParametricDesignType.
Do NOT set designType, close doc, or create doc.

Patterns follow turbopump_v7.py exactly:
  - NewBodyFeatureOperation for every extrude, join one at a time via combine
  - Shell simple bodies BEFORE adding internal features
  - Ribs: sketch on offset XY plane, extrude vertically, join individually
  - Bolt holes: sketch on XY, cut through all, circular pattern around Z axis
  - Never sketch on faces --- use construction planes only
"""

from typing import Dict, Any
import math as _math


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _build_rib_configs(n_ribs: int, inner_r: float, outer_r: float,
                       rib_t: float) -> str:
    """Return a Python list-of-tuples literal for rib rectangle configs.

    Each tuple: (center_x, center_y, half_length_x, half_length_y).
    Ribs are evenly spaced around the Z axis.
    """
    entries = []
    for i in range(n_ribs):
        angle = i * (2 * _math.pi / n_ribs)
        cos_a = _math.cos(angle)
        sin_a = _math.sin(angle)

        mid_r = (inner_r + outer_r) / 2.0
        cx = round(mid_r * cos_a, 6)
        cy = round(mid_r * sin_a, 6)
        half_len = (outer_r - inner_r) / 2.0

        if abs(cos_a) >= abs(sin_a):
            hlx = round(half_len, 6)
            hly = round(rib_t, 6)
        else:
            hlx = round(rib_t, 6)
            hly = round(half_len, 6)

        entries.append(f"({cx}, {cy}, {hlx}, {hly})")

    return "[" + ", ".join(entries) + "]"


# ---------------------------------------------------------------------------
# 1. Centrifugal Impeller
# ---------------------------------------------------------------------------

def impeller_template(params: Dict[str, Any]) -> str:
    """Centrifugal impeller with hub, disk, blades, shroud, and bore.

    params
    ------
    hub_r_cm    : hub cylinder radius
    tip_r_cm    : blade tip (outer) radius
    height_cm   : total impeller height
    n_blades    : number of blades
    blade_t_cm  : blade thickness
    bore_r_cm   : center bore radius
    """
    p = {
        "hub_r_cm":   float(params.get("hub_r_cm", 2.0)),
        "tip_r_cm":   float(params.get("tip_r_cm", 6.0)),
        "height_cm":  float(params.get("height_cm", 4.0)),
        "n_blades":   int(params.get("n_blades", 8)),
        "blade_t_cm": float(params.get("blade_t_cm", 0.3)),
        "bore_r_cm":  float(params.get("bore_r_cm", 1.0)),
    }

    return f"""\
import math

# ============================================================
# Centrifugal Impeller --- ARIA-OS parametric template
# All dimensions in CM
# ============================================================
HUB_R     = {p['hub_r_cm']}
TIP_R     = {p['tip_r_cm']}
HEIGHT    = {p['height_cm']}
N_BLADES  = {p['n_blades']}
BLADE_T   = {p['blade_t_cm']}
BORE_R    = {p['bore_r_cm']}

# Derived
DISK_H    = HEIGHT * 0.15          # base disk thickness
SHROUD_H  = HEIGHT * 0.10          # top shroud ring thickness
BLADE_H   = HEIGHT - DISK_H - SHROUD_H  # blade passage height
BLADE_HLT = BLADE_T / 2.0         # blade half-thickness

# === STEP 1: Hub cylinder (NewBody) ===
sk_hub = rootComp.sketches.add(rootComp.xYConstructionPlane)
sk_hub.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), HUB_R)
ext_hub = rootComp.features.extrudeFeatures.createInput(
    sk_hub.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_hub.setDistanceExtent(False, adsk.core.ValueInput.createByReal(HEIGHT))
rootComp.features.extrudeFeatures.add(ext_hub)

# === STEP 2: Base disk (NewBody + Join) ===
sk_disk = rootComp.sketches.add(rootComp.xYConstructionPlane)
sk_disk.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), TIP_R)
ext_disk = rootComp.features.extrudeFeatures.createInput(
    sk_disk.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_disk.setDistanceExtent(False, adsk.core.ValueInput.createByReal(DISK_H))
disk_body = rootComp.features.extrudeFeatures.add(ext_disk).bodies.item(0)

main = rootComp.bRepBodies.item(0)
tc_disk = adsk.core.ObjectCollection.create()
tc_disk.add(disk_body)
ci_disk = rootComp.features.combineFeatures.createInput(main, tc_disk)
ci_disk.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
rootComp.features.combineFeatures.add(ci_disk)

# === STEP 3: Single blade (NewBody + Join), then circular pattern ===
# Blade is a thin radial rectangle from hub to tip, on top of base disk.
pi_blade = rootComp.constructionPlanes.createInput()
pi_blade.setByOffset(rootComp.xYConstructionPlane,
                     adsk.core.ValueInput.createByReal(DISK_H))
blade_plane = rootComp.constructionPlanes.add(pi_blade)

blade_cx = (HUB_R + TIP_R) / 2.0
blade_hlx = (TIP_R - HUB_R) / 2.0

sk_bl = rootComp.sketches.add(blade_plane)
sk_bl.sketchCurves.sketchLines.addTwoPointRectangle(
    adsk.core.Point3D.create(blade_cx - blade_hlx, -BLADE_HLT, 0),
    adsk.core.Point3D.create(blade_cx + blade_hlx,  BLADE_HLT, 0))
ext_bl = rootComp.features.extrudeFeatures.createInput(
    sk_bl.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_bl.setDistanceExtent(False, adsk.core.ValueInput.createByReal(BLADE_H))
blade_body = rootComp.features.extrudeFeatures.add(ext_bl).bodies.item(0)

main = rootComp.bRepBodies.item(0)
tc_bl = adsk.core.ObjectCollection.create()
tc_bl.add(blade_body)
ci_bl = rootComp.features.combineFeatures.createInput(main, tc_bl)
ci_bl.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
blade_combine = rootComp.features.combineFeatures.add(ci_bl)

# Circular pattern of the combine feature (which includes the blade)
if N_BLADES > 1:
    pat_bl_objs = adsk.core.ObjectCollection.create()
    pat_bl_objs.add(blade_combine)
    pat_bl_in = rootComp.features.circularPatternFeatures.createInput(
        pat_bl_objs, rootComp.zConstructionAxis)
    pat_bl_in.quantity = adsk.core.ValueInput.createByReal(N_BLADES)
    rootComp.features.circularPatternFeatures.add(pat_bl_in)

# === STEP 4: Top shroud ring (NewBody + Join) ===
# Annular ring from hub to tip at top of blades
shroud_z = DISK_H + BLADE_H
pi_shr = rootComp.constructionPlanes.createInput()
pi_shr.setByOffset(rootComp.xYConstructionPlane,
                   adsk.core.ValueInput.createByReal(shroud_z))
shroud_plane = rootComp.constructionPlanes.add(pi_shr)

sk_shr = rootComp.sketches.add(shroud_plane)
sk_shr.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), TIP_R)
sk_shr.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), HUB_R)
# Find the annular profile (area between two circles)
shr_prof = None
for i in range(sk_shr.profiles.count):
    prof = sk_shr.profiles.item(i)
    area = prof.areaProperties().area
    outer_area = math.pi * TIP_R * TIP_R
    inner_area = math.pi * HUB_R * HUB_R
    ring_area = outer_area - inner_area
    if abs(area - ring_area) < abs(area - outer_area) and abs(area - ring_area) < abs(area - inner_area):
        shr_prof = prof
        break
if shr_prof is None:
    shr_prof = sk_shr.profiles.item(0)

ext_shr = rootComp.features.extrudeFeatures.createInput(
    shr_prof,
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_shr.setDistanceExtent(False, adsk.core.ValueInput.createByReal(SHROUD_H))
shr_body = rootComp.features.extrudeFeatures.add(ext_shr).bodies.item(0)

main = rootComp.bRepBodies.item(0)
tc_shr = adsk.core.ObjectCollection.create()
tc_shr.add(shr_body)
ci_shr = rootComp.features.combineFeatures.createInput(main, tc_shr)
ci_shr.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
rootComp.features.combineFeatures.add(ci_shr)

# === STEP 5: Bore cut (through all, from top) ===
pi_top = rootComp.constructionPlanes.createInput()
pi_top.setByOffset(rootComp.xYConstructionPlane,
                   adsk.core.ValueInput.createByReal(HEIGHT))
top_plane = rootComp.constructionPlanes.add(pi_top)

sk_bore = rootComp.sketches.add(top_plane)
sk_bore.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), BORE_R)
ext_bore = rootComp.features.extrudeFeatures.createInput(
    sk_bore.profiles.item(0),
    adsk.fusion.FeatureOperations.CutFeatureOperation)
ext_bore.setAllExtent(adsk.fusion.ExtentDirections.NegativeExtentDirection)
rootComp.features.extrudeFeatures.add(ext_bore)
"""


# ---------------------------------------------------------------------------
# 2. Volute Housing (pump / turbopump)
# ---------------------------------------------------------------------------

def volute_housing_template(params: Dict[str, Any]) -> str:
    """Pump/turbopump volute housing.

    Follows the EXACT turbopump_v7.py pattern:
      cylinder -> shell top -> flange -> outlet -> ribs (one-by-one join) ->
      bore cut -> bolt holes + circular pattern

    params
    ------
    cyl_r_cm      : main cylinder radius
    height_cm     : main cylinder height
    wall_cm       : shell wall thickness
    flange_r_cm   : flange outer radius
    flange_h_cm   : flange thickness
    bore_r_cm     : bearing bore radius
    bore_depth_cm : bearing bore depth
    n_bolts       : number of bolt holes
    bolt_r_cm     : bolt hole radius
    bolt_pcd_cm   : bolt PCD (pitch circle diameter / 2)
    outlet_r_cm   : outlet pipe radius
    outlet_h_cm   : outlet pipe height
    n_ribs        : number of internal ribs (2 = cross, 4 = cruciform)
    rib_inner_cm  : rib inner radius (start of rib, typically bore_r)
    rib_outer_cm  : rib outer radius (end of rib, typically cyl_r - wall)
    rib_t_cm      : rib half-thickness (full width = 2 * rib_t)
    rib_h_cm      : rib extrude height
    """
    p = {
        "cyl_r_cm":      float(params.get("cyl_r_cm", 6.0)),
        "height_cm":     float(params.get("height_cm", 18.0)),
        "wall_cm":       float(params.get("wall_cm", 0.8)),
        "flange_r_cm":   float(params.get("flange_r_cm", 8.0)),
        "flange_h_cm":   float(params.get("flange_h_cm", 1.5)),
        "bore_r_cm":     float(params.get("bore_r_cm", 2.5)),
        "bore_depth_cm": float(params.get("bore_depth_cm", 2.5)),
        "n_bolts":       int(params.get("n_bolts", 6)),
        "bolt_r_cm":     float(params.get("bolt_r_cm", 0.4)),
        "bolt_pcd_cm":   float(params.get("bolt_pcd_cm", 7.0)),
        "outlet_r_cm":   float(params.get("outlet_r_cm", 1.5)),
        "outlet_h_cm":   float(params.get("outlet_h_cm", 4.0)),
        "n_ribs":        int(params.get("n_ribs", 4)),
        "rib_inner_cm":  float(params.get("rib_inner_cm", 2.5)),
        "rib_outer_cm":  float(params.get("rib_outer_cm", 5.2)),
        "rib_t_cm":      float(params.get("rib_t_cm", 0.15)),
        "rib_h_cm":      float(params.get("rib_h_cm", 15.0)),
    }

    rib_configs_str = _build_rib_configs(
        p["n_ribs"], p["rib_inner_cm"], p["rib_outer_cm"], p["rib_t_cm"])

    return f"""\
import math

# ============================================================
# Volute Housing --- ARIA-OS parametric template
# Pattern: turbopump_v7.py (verified working)
# All dimensions in CM
# ============================================================
CYL_R       = {p['cyl_r_cm']}
CYL_H       = {p['height_cm']}
WALL        = {p['wall_cm']}
INNER_R     = CYL_R - WALL
FLANGE_R    = {p['flange_r_cm']}
FLANGE_H    = {p['flange_h_cm']}
OUTLET_R    = {p['outlet_r_cm']}
OUTLET_H    = {p['outlet_h_cm']}
BORE_R      = {p['bore_r_cm']}
BORE_D      = {p['bore_depth_cm']}
BOLT_R      = {p['bolt_r_cm']}
BOLT_PCD    = {p['bolt_pcd_cm']}
N_BOLTS     = {p['n_bolts']}
RIB_T       = {p['rib_t_cm']}
RIB_H       = {p['rib_h_cm']}

# === STEP 1: Main cylinder (NewBody) ===
sk = rootComp.sketches.add(rootComp.xYConstructionPlane)
sk.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), CYL_R)
ext = rootComp.features.extrudeFeatures.createInput(
    sk.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext.setDistanceExtent(False, adsk.core.ValueInput.createByReal(CYL_H))
rootComp.features.extrudeFeatures.add(ext)

# === STEP 2: Shell (remove top face, keep walls) ===
body = rootComp.bRepBodies.item(0)
top_face = None
max_z = -999
for face in body.faces:
    bb = face.boundingBox
    mid_z = (bb.minPoint.z + bb.maxPoint.z) / 2
    geo = face.geometry
    if hasattr(geo, 'normal') and abs(geo.normal.z - 1.0) < 0.01:
        if mid_z > max_z:
            max_z = mid_z
            top_face = face

if top_face:
    faces_coll = adsk.core.ObjectCollection.create()
    faces_coll.add(top_face)
    shell_in = rootComp.features.shellFeatures.createInput(faces_coll, False)
    shell_in.insideThickness = adsk.core.ValueInput.createByReal(WALL)
    rootComp.features.shellFeatures.add(shell_in)

# === STEP 3: Flange (NewBody + Join) ===
sk_fl = rootComp.sketches.add(rootComp.xYConstructionPlane)
sk_fl.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), FLANGE_R)
ext_fl = rootComp.features.extrudeFeatures.createInput(
    sk_fl.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_fl.setDistanceExtent(False, adsk.core.ValueInput.createByReal(FLANGE_H))
fl_body = rootComp.features.extrudeFeatures.add(ext_fl).bodies.item(0)

main = rootComp.bRepBodies.item(0)
tc = adsk.core.ObjectCollection.create()
tc.add(fl_body)
ci = rootComp.features.combineFeatures.createInput(main, tc)
ci.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
rootComp.features.combineFeatures.add(ci)

# === STEP 4: Outlet pipe (offset plane + NewBody + Join) ===
pi_top = rootComp.constructionPlanes.createInput()
pi_top.setByOffset(rootComp.xYConstructionPlane,
                   adsk.core.ValueInput.createByReal(CYL_H))
top_plane = rootComp.constructionPlanes.add(pi_top)

sk_out = rootComp.sketches.add(top_plane)
sk_out.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), OUTLET_R)
ext_out = rootComp.features.extrudeFeatures.createInput(
    sk_out.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_out.setDistanceExtent(False, adsk.core.ValueInput.createByReal(OUTLET_H))
out_body = rootComp.features.extrudeFeatures.add(ext_out).bodies.item(0)

main = rootComp.bRepBodies.item(0)
tc2 = adsk.core.ObjectCollection.create()
tc2.add(out_body)
ci2 = rootComp.features.combineFeatures.createInput(main, tc2)
ci2.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
rootComp.features.combineFeatures.add(ci2)

# === STEP 5: Internal ribs (sketch on offset XY plane, extrude up, join each) ===
rib_configs = {rib_configs_str}

pi_rib = rootComp.constructionPlanes.createInput()
pi_rib.setByOffset(rootComp.xYConstructionPlane,
                   adsk.core.ValueInput.createByReal(FLANGE_H))
rib_plane = rootComp.constructionPlanes.add(pi_rib)

for cx, cy, hlx, hly in rib_configs:
    sk_r = rootComp.sketches.add(rib_plane)
    sk_r.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(cx - hlx, cy - hly, 0),
        adsk.core.Point3D.create(cx + hlx, cy + hly, 0))
    ext_r = rootComp.features.extrudeFeatures.createInput(
        sk_r.profiles.item(0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ext_r.setDistanceExtent(False, adsk.core.ValueInput.createByReal(RIB_H))
    r_body = rootComp.features.extrudeFeatures.add(ext_r).bodies.item(0)

    main = rootComp.bRepBodies.item(0)
    tc_r = adsk.core.ObjectCollection.create()
    tc_r.add(r_body)
    ci_r = rootComp.features.combineFeatures.createInput(main, tc_r)
    ci_r.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
    rootComp.features.combineFeatures.add(ci_r)

# === STEP 6: Bearing bore (cut from top) ===
sk_bore = rootComp.sketches.add(top_plane)
sk_bore.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), BORE_R)
ext_bore = rootComp.features.extrudeFeatures.createInput(
    sk_bore.profiles.item(0),
    adsk.fusion.FeatureOperations.CutFeatureOperation)
ext_bore.setDistanceExtent(True, adsk.core.ValueInput.createByReal(BORE_D))
rootComp.features.extrudeFeatures.add(ext_bore)

# === STEP 7: Bolt holes with circular pattern ===
sk_b = rootComp.sketches.add(rootComp.xYConstructionPlane)
sk_b.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(BOLT_PCD, 0, 0), BOLT_R)
ext_b = rootComp.features.extrudeFeatures.createInput(
    sk_b.profiles.item(0),
    adsk.fusion.FeatureOperations.CutFeatureOperation)
ext_b.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
bolt_feat = rootComp.features.extrudeFeatures.add(ext_b)

pat_objs = adsk.core.ObjectCollection.create()
pat_objs.add(bolt_feat)
pat_in = rootComp.features.circularPatternFeatures.createInput(
    pat_objs, rootComp.zConstructionAxis)
pat_in.quantity = adsk.core.ValueInput.createByReal(N_BOLTS)
rootComp.features.circularPatternFeatures.add(pat_in)
"""


# ---------------------------------------------------------------------------
# 3. Pipe Fitting (elbow with flanges)
# ---------------------------------------------------------------------------

def pipe_fitting_template(params: Dict[str, Any]) -> str:
    """Pipe elbow fitting with flanges and bolt holes.

    Build: sweep circle along arc path, shell, flanges at both ends,
    bolt holes with circular pattern.

    params
    ------
    pipe_r_cm    : pipe outer radius
    wall_cm      : pipe wall thickness
    bend_r_cm    : bend centerline radius
    angle_deg    : bend angle in degrees (90 = standard elbow)
    flange_r_cm  : flange outer radius
    flange_h_cm  : flange thickness
    n_bolts      : number of bolt holes per flange
    bolt_r_cm    : bolt hole radius
    bolt_pcd_cm  : bolt pitch circle radius
    """
    p = {
        "pipe_r_cm":   float(params.get("pipe_r_cm", 2.5)),
        "wall_cm":     float(params.get("wall_cm", 0.3)),
        "bend_r_cm":   float(params.get("bend_r_cm", 7.5)),
        "angle_deg":   float(params.get("angle_deg", 90.0)),
        "flange_r_cm": float(params.get("flange_r_cm", 4.0)),
        "flange_h_cm": float(params.get("flange_h_cm", 1.0)),
        "n_bolts":     int(params.get("n_bolts", 6)),
        "bolt_r_cm":   float(params.get("bolt_r_cm", 0.4)),
        "bolt_pcd_cm": float(params.get("bolt_pcd_cm", 3.5)),
    }

    return f"""\
import math

# ============================================================
# Pipe Fitting (Elbow) --- ARIA-OS parametric template
# All dimensions in CM
# ============================================================
PIPE_R    = {p['pipe_r_cm']}
WALL      = {p['wall_cm']}
BEND_R    = {p['bend_r_cm']}
ANGLE_DEG = {p['angle_deg']}
FLANGE_R  = {p['flange_r_cm']}
FLANGE_H  = {p['flange_h_cm']}
N_BOLTS   = {p['n_bolts']}
BOLT_R    = {p['bolt_r_cm']}
BOLT_PCD  = {p['bolt_pcd_cm']}

ANGLE_RAD = math.radians(ANGLE_DEG)

# === STEP 1: Sweep profile circle along arc path (NewBody) ===
# Path sketch: arc in the XZ plane, center at (BEND_R, 0, 0),
# radius = BEND_R, from origin sweeping upward by ANGLE_DEG.

# Path sketch on XZ construction plane
sk_path = rootComp.sketches.add(rootComp.xZConstructionPlane)
# Arc start at origin, sweeps through bend
arc_start = adsk.core.Point3D.create(0, 0, 0)
end_x = BEND_R - BEND_R * math.cos(ANGLE_RAD)
end_y = BEND_R * math.sin(ANGLE_RAD)
arc_end = adsk.core.Point3D.create(end_x, end_y, 0)
arc_mid = adsk.core.Point3D.create(
    BEND_R - BEND_R * math.cos(ANGLE_RAD / 2),
    BEND_R * math.sin(ANGLE_RAD / 2), 0)
sk_path.sketchCurves.sketchArcs.addByThreePoints(arc_start, arc_mid, arc_end)
path_curve = sk_path.sketchCurves.item(0)

# Profile sketch: circle on YZ plane (perpendicular to path at start)
sk_prof = rootComp.sketches.add(rootComp.yZConstructionPlane)
sk_prof.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), PIPE_R)

# Create sweep path and sweep
sweep_path = rootComp.features.createPath(path_curve, False)
sweep_in = rootComp.features.sweepFeatures.createInput(
    sk_prof.profiles.item(0), sweep_path,
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
rootComp.features.sweepFeatures.add(sweep_in)

# === STEP 2: Shell the swept pipe body ===
pipe_body = rootComp.bRepBodies.item(0)
# Find the two planar end faces by matching expected circle area
end_faces = adsk.core.ObjectCollection.create()
circle_area = math.pi * PIPE_R * PIPE_R
for face in pipe_body.faces:
    geo = face.geometry
    if hasattr(geo, 'normal'):
        area = face.area
        if abs(area - circle_area) / circle_area < 0.15:
            end_faces.add(face)

shell_in = rootComp.features.shellFeatures.createInput(end_faces, False)
shell_in.insideThickness = adsk.core.ValueInput.createByReal(WALL)
rootComp.features.shellFeatures.add(shell_in)

# === STEP 3: Flange A at pipe start (inlet, on YZ plane) ===
sk_flA = rootComp.sketches.add(rootComp.yZConstructionPlane)
sk_flA.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), FLANGE_R)
ext_flA = rootComp.features.extrudeFeatures.createInput(
    sk_flA.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
# Extrude in -X direction (away from pipe)
ext_flA.setDistanceExtent(True, adsk.core.ValueInput.createByReal(FLANGE_H))
flA_body = rootComp.features.extrudeFeatures.add(ext_flA).bodies.item(0)

main = rootComp.bRepBodies.item(0)
tc_flA = adsk.core.ObjectCollection.create()
tc_flA.add(flA_body)
ci_flA = rootComp.features.combineFeatures.createInput(main, tc_flA)
ci_flA.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
rootComp.features.combineFeatures.add(ci_flA)

# === STEP 4: Flange B at pipe end (outlet, on offset XY plane) ===
# XZ sketch coords: sketch_x -> world X, sketch_y -> world Z
# So pipe end in world is (end_x, 0, end_y)
pi_flB_offset = rootComp.constructionPlanes.createInput()
pi_flB_offset.setByOffset(rootComp.xYConstructionPlane,
                          adsk.core.ValueInput.createByReal(end_y))
flB_offset_plane = rootComp.constructionPlanes.add(pi_flB_offset)

sk_flB = rootComp.sketches.add(flB_offset_plane)
sk_flB.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(end_x, 0, 0), FLANGE_R)
ext_flB = rootComp.features.extrudeFeatures.createInput(
    sk_flB.profiles.item(0),
    adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
ext_flB.setDistanceExtent(False, adsk.core.ValueInput.createByReal(FLANGE_H))
flB_body = rootComp.features.extrudeFeatures.add(ext_flB).bodies.item(0)

main = rootComp.bRepBodies.item(0)
tc_flB = adsk.core.ObjectCollection.create()
tc_flB.add(flB_body)
ci_flB = rootComp.features.combineFeatures.createInput(main, tc_flB)
ci_flB.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
rootComp.features.combineFeatures.add(ci_flB)

# === STEP 5: Bolt holes on Flange A (YZ plane, circular pattern around X axis) ===
sk_bA = rootComp.sketches.add(rootComp.yZConstructionPlane)
sk_bA.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, BOLT_PCD, 0), BOLT_R)
ext_bA = rootComp.features.extrudeFeatures.createInput(
    sk_bA.profiles.item(0),
    adsk.fusion.FeatureOperations.CutFeatureOperation)
ext_bA.setAllExtent(adsk.fusion.ExtentDirections.NegativeExtentDirection)
boltA_feat = rootComp.features.extrudeFeatures.add(ext_bA)

pat_A_objs = adsk.core.ObjectCollection.create()
pat_A_objs.add(boltA_feat)
pat_A_in = rootComp.features.circularPatternFeatures.createInput(
    pat_A_objs, rootComp.xConstructionAxis)
pat_A_in.quantity = adsk.core.ValueInput.createByReal(N_BOLTS)
rootComp.features.circularPatternFeatures.add(pat_A_in)

# === STEP 6: Bolt holes on Flange B (offset plane, pattern around local Z axis) ===
sk_bB = rootComp.sketches.add(flB_offset_plane)
sk_bB.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(end_x + BOLT_PCD, 0, 0), BOLT_R)
ext_bB = rootComp.features.extrudeFeatures.createInput(
    sk_bB.profiles.item(0),
    adsk.fusion.FeatureOperations.CutFeatureOperation)
ext_bB.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
boltB_feat = rootComp.features.extrudeFeatures.add(ext_bB)

# Construction axis through pipe end parallel to Z for bolt pattern
pi_axB = rootComp.constructionAxes.createInput()
pi_axB.setByTwoPoints(
    adsk.core.Point3D.create(end_x, 0, 0),
    adsk.core.Point3D.create(end_x, 0, end_y + 10))
axB = rootComp.constructionAxes.add(pi_axB)

pat_B_objs = adsk.core.ObjectCollection.create()
pat_B_objs.add(boltB_feat)
pat_B_in = rootComp.features.circularPatternFeatures.createInput(
    pat_B_objs, axB)
pat_B_in.quantity = adsk.core.ValueInput.createByReal(N_BOLTS)
rootComp.features.circularPatternFeatures.add(pat_B_in)
"""
