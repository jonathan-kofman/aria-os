"""
aria_os/grasshopper_generator.py
RhinoCommon script generator for ARIA parts.

Template map routes named parts to pre-built RhinoCommon geometry functions.
Unknown parts fall back to LLM-generated RhinoCommon via generate_rhino_python().

RHINO_COMPUTE_URL env var (default: http://localhost:6500) controls endpoint.
See docs/rhino_compute_setup.md for installation instructions.
"""
import json
import math as _math
import os
from pathlib import Path
from typing import Any, Optional

from .. import event_bus

RHINO_COMPUTE_URL = os.environ.get("RHINO_COMPUTE_URL", "http://localhost:6500")

# --------------------------------------------------------------------------- #
# Shared script header / footer
# --------------------------------------------------------------------------- #

_SCRIPT_HEADER = (
    "import rhinoscriptsyntax as rs\n"
    "import Rhino.Geometry as rg\n"
    "import scriptcontext as sc\n"
    "import os\n"
    "import math\n"
    "import System\n"
)


def _script_footer(step_path: str, stl_path: str) -> str:
    """Return BBOX print + STEP/STL export block."""
    # Use forward slashes — Rhino accepts them and they avoid escape issues
    sp = step_path.replace("\\", "/")
    st = stl_path.replace("\\", "/")
    return "\n".join([
        "",
        "# === BBOX + EXPORT ===",
        "bb = result.GetBoundingBox(True)",
        "xlen = bb.Max.X - bb.Min.X",
        "ylen = bb.Max.Y - bb.Min.Y",
        "zlen = bb.Max.Z - bb.Min.Z",
        'print("BBOX:{:.3f},{:.3f},{:.3f}".format(xlen, ylen, zlen))',
        "",
        f'STEP_PATH = "{sp}"',
        f'STL_PATH  = "{st}"',
        "try: os.makedirs(os.path.dirname(STEP_PATH) or '.')",
        "except OSError: pass",
        "try: os.makedirs(os.path.dirname(STL_PATH) or '.')",
        "except OSError: pass",
        "_obj_id = sc.doc.Objects.AddBrep(result)",
        "sc.doc.Objects.Select(_obj_id)",
        'rs.Command(\'_-Export "\' + STEP_PATH + \'" _Enter\', False)',
        'rs.Command(\'_-Export "\' + STL_PATH  + \'" _Enter _Enter\', False)',
        'print("STEP: " + STEP_PATH)',
        'print("STL:  " + STL_PATH)',
        "",
    ]) + "\n"


# --------------------------------------------------------------------------- #
# Template: aria_cam_collar
# --------------------------------------------------------------------------- #

def _script_cam_collar(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params = plan.get("params", {})
    od     = float(params.get("od_mm",            47.0))
    bore   = float(params.get("id_mm",            20.0))
    height = float(params.get("length_mm",        20.0))
    ramp   = float(params.get("ramp_height_mm",    4.0))
    ss_dia = float(params.get("set_screw_dia_mm",  4.0))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"OD_MM            = {od}",
        f"BORE_MM          = {bore}",
        f"HEIGHT_MM        = {height}",
        f"RAMP_HEIGHT_MM   = {ramp}",
        f"SET_SCREW_DIA_MM = {ss_dia}",
        "# === END PARAMETERS ===",
        "",
        "# --- Outer cylinder ---",
        "outer_circle = rg.Circle(rg.Plane.WorldXY, OD_MM / 2.0)",
        "outer_cyl    = rg.Cylinder(outer_circle, HEIGHT_MM).ToBrep(True, True)",
        "",
        "# --- Bore ---",
        "bore_circle = rg.Circle(rg.Plane.WorldXY, BORE_MM / 2.0)",
        "bore_cyl    = rg.Cylinder(bore_circle, HEIGHT_MM * 1.1).ToBrep(True, True)",
        "hollowed    = rg.Brep.CreateBooleanDifference([outer_cyl], [bore_cyl], 0.001)",
        "result      = hollowed[0] if hollowed else outer_cyl",
        "",
        "# --- Helical ramp (90-deg sweep on top face) ---",
        "if RAMP_HEIGHT_MM > 0:",
        "    r_inner = BORE_MM / 2.0 + 1.0",
        "    r_outer = OD_MM  / 2.0",
        "    pts = [",
        "        rg.Point3d(r_inner, 0, HEIGHT_MM - RAMP_HEIGHT_MM),",
        "        rg.Point3d(r_outer, 0, HEIGHT_MM - RAMP_HEIGHT_MM),",
        "        rg.Point3d(r_outer, 0, HEIGHT_MM),",
        "        rg.Point3d(r_inner, 0, HEIGHT_MM),",
        "        rg.Point3d(r_inner, 0, HEIGHT_MM - RAMP_HEIGHT_MM),",
        "    ]",
        "    profile    = rg.PolylineCurve(pts)",
        "    axis       = rg.Line(rg.Point3d(0, 0, 0), rg.Point3d(0, 0, 1))",
        "    rev_srf    = rg.RevSurface.Create(profile, axis, 0, math.pi / 2.0)",
        "    if rev_srf:",
        "        ramp_brep = rg.Brep.CreateFromRevSurface(rev_srf, True, True)",
        "        if ramp_brep:",
        "            cut = rg.Brep.CreateBooleanDifference([result], [ramp_brep], 0.001)",
        "            if cut:",
        "                result = cut[0]",
        "",
        "# --- Set screw (radial) ---",
        "if SET_SCREW_DIA_MM > 0:",
        "    ss_origin = rg.Point3d(OD_MM / 2.0, 0, HEIGHT_MM / 2.0)",
        "    ss_plane  = rg.Plane(ss_origin, rg.Vector3d(1, 0, 0))",
        "    ss_circle = rg.Circle(ss_plane, SET_SCREW_DIA_MM / 2.0)",
        "    ss_cyl    = rg.Cylinder(ss_circle, OD_MM * 0.6).ToBrep(True, True)",
        "    cut2 = rg.Brep.CreateBooleanDifference([result], [ss_cyl], 0.001)",
        "    if cut2:",
        "        result = cut2[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_ratchet_ring
# --------------------------------------------------------------------------- #

def _script_ratchet_ring(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params   = plan.get("params", {})
    od       = float(params.get("od_mm",           213.0))
    bore     = float(params.get("bore_mm",         185.0))
    thick    = float(params.get("thickness_mm",     21.0))
    n_teeth  = int(  params.get("n_teeth",           24))

    # Pre-compute tooth geometry (outer Python, embedded as constants)
    r_tip    = od / 2.0
    tooth_h  = max((od - bore) * 0.08, 4.0)
    r_root   = r_tip - tooth_h
    drive_y  = round(tooth_h * _math.tan(_math.radians(8)),  3)
    back_y   = round(tooth_h * _math.tan(_math.radians(60)), 3)
    root_w   = round(back_y * 0.3, 3)

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"OD_MM       = {od}",
        f"BORE_MM     = {bore}",
        f"THICK_MM    = {thick}",
        f"N_TEETH     = {n_teeth}",
        f"R_TIP       = {r_tip}",
        f"TOOTH_H     = {tooth_h}",
        f"R_ROOT      = {r_root}",
        f"DRIVE_Y     = {drive_y}",
        f"BACK_Y      = {back_y}",
        f"ROOT_W      = {root_w}",
        "# === END PARAMETERS ===",
        "",
        "# --- Annular ring body ---",
        "outer_circle = rg.Circle(rg.Plane.WorldXY, OD_MM / 2.0)",
        "outer_cyl    = rg.Cylinder(outer_circle, THICK_MM).ToBrep(True, True)",
        "bore_circle  = rg.Circle(rg.Plane.WorldXY, BORE_MM / 2.0)",
        "bore_cyl     = rg.Cylinder(bore_circle, THICK_MM * 1.01).ToBrep(True, True)",
        "ring_body    = rg.Brep.CreateBooleanDifference([outer_cyl], [bore_cyl], 0.001)",
        "result       = ring_body[0] if ring_body else outer_cyl",
        "",
        "# --- Asymmetric tooth (drive ~8 deg, back ~60 deg) ---",
        "tooth_pts = [",
        "    rg.Point3d(R_ROOT,           -ROOT_W / 2.0, 0),",
        "    rg.Point3d(R_TIP,            -DRIVE_Y / 2.0, 0),",
        "    rg.Point3d(R_TIP,             DRIVE_Y / 2.0, 0),",
        "    rg.Point3d(R_ROOT,            ROOT_W / 2.0 + BACK_Y, 0),",
        "    rg.Point3d(R_ROOT,           -ROOT_W / 2.0, 0),",
        "]",
        "tooth_profile = rg.PolylineCurve(tooth_pts)",
        "tooth_extr    = rg.Extrusion.Create(tooth_profile, THICK_MM, True)",
        "tooth_brep    = tooth_extr.ToBrep() if tooth_extr else None",
        "",
        "# --- Polar array + union ---",
        "if tooth_brep:",
        "    for i in range(N_TEETH):",
        "        angle  = i * 2.0 * math.pi / N_TEETH",
        "        xform  = rg.Transform.Rotation(angle, rg.Vector3d(0, 0, 1), rg.Point3d.Origin)",
        "        t_copy = tooth_brep.DuplicateBrep()",
        "        t_copy.Transform(xform)",
        "        united = rg.Brep.CreateBooleanUnion([result, t_copy], 0.001)",
        "        if united:",
        "            result = united[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_housing
# --------------------------------------------------------------------------- #

def _script_housing(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params      = plan.get("params", {})
    w           = float(params.get("width_mm",          700.0))
    h           = float(params.get("height_mm",         680.0))
    d           = float(params.get("depth_mm",          344.0))
    wall        = float(params.get("wall_thickness_mm",   8.0))
    bearing_od  = float(params.get("bearing_od_mm",      62.0))
    ratchet_od  = float(params.get("ratchet_pocket_dia",213.0))
    rope_w      = float(params.get("rope_slot_width_mm", 12.0))
    rope_d      = float(params.get("rope_slot_depth_mm", 60.0))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"W_MM              = {w}",
        f"H_MM              = {h}",
        f"D_MM              = {d}",
        f"WALL_MM           = {wall}",
        f"BEARING_OD_MM     = {bearing_od}",
        f"RATCHET_POCKET_DIA= {ratchet_od}",
        f"ROPE_SLOT_W_MM    = {rope_w}",
        f"ROPE_SLOT_D_MM    = {rope_d}",
        "# === END PARAMETERS ===",
        "",
        "# --- Outer shell ---",
        "outer = rg.Box(rg.Plane.WorldXY,",
        "               rg.Interval(0, W_MM),",
        "               rg.Interval(0, H_MM),",
        "               rg.Interval(0, D_MM)).ToBrep()",
        "",
        "# --- Inner void ---",
        "inner = rg.Box(rg.Plane.WorldXY,",
        "               rg.Interval(WALL_MM, W_MM - WALL_MM),",
        "               rg.Interval(WALL_MM, H_MM - WALL_MM),",
        "               rg.Interval(WALL_MM, D_MM - WALL_MM)).ToBrep()",
        "hollowed = rg.Brep.CreateBooleanDifference([outer], [inner], 0.001)",
        "result   = hollowed[0] if hollowed else outer",
        "",
        "# --- Front bearing bore ---",
        "brg_plane_f = rg.Plane(rg.Point3d(W_MM / 2, H_MM / 2, -1),",
        "                       rg.Vector3d(0, 0, 1))",
        "brg_f = rg.Cylinder(rg.Circle(brg_plane_f, BEARING_OD_MM / 2.0),",
        "                    WALL_MM * 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [brg_f], 0.001)",
        "if cut: result = cut[0]",
        "",
        "# --- Rear bearing bore ---",
        "brg_plane_r = rg.Plane(rg.Point3d(W_MM / 2, H_MM / 2, D_MM - WALL_MM),",
        "                       rg.Vector3d(0, 0, 1))",
        "brg_r = rg.Cylinder(rg.Circle(brg_plane_r, BEARING_OD_MM / 2.0),",
        "                    WALL_MM * 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [brg_r], 0.001)",
        "if cut: result = cut[0]",
        "",
        "# --- Ratchet pocket (rear face) ---",
        "rp_plane = rg.Plane(rg.Point3d(W_MM / 2, H_MM / 2, D_MM - WALL_MM * 0.5),",
        "                    rg.Vector3d(0, 0, 1))",
        "rp_cyl   = rg.Cylinder(rg.Circle(rp_plane, RATCHET_POCKET_DIA / 2.0),",
        "                       WALL_MM).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [rp_cyl], 0.001)",
        "if cut: result = cut[0]",
        "",
        "# --- Rope slot (top face) ---",
        "slot = rg.Box(rg.Plane.WorldXY,",
        "              rg.Interval(W_MM / 2 - ROPE_SLOT_W_MM / 2,",
        "                          W_MM / 2 + ROPE_SLOT_W_MM / 2),",
        "              rg.Interval(H_MM - WALL_MM * 0.5, H_MM + 1),",
        "              rg.Interval(D_MM / 2 - ROPE_SLOT_D_MM / 2,",
        "                          D_MM / 2 + ROPE_SLOT_D_MM / 2)).ToBrep()",
        "cut = rg.Brep.CreateBooleanDifference([result], [slot], 0.001)",
        "if cut: result = cut[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_catch_pawl
# --------------------------------------------------------------------------- #

def _script_catch_pawl(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params      = plan.get("params", {})
    length      = float(params.get("length_mm",        60.0))
    width       = float(params.get("width_mm",         12.0))
    thick       = float(params.get("thickness_mm",      6.0))
    pivot_dia   = float(params.get("pivot_hole_dia_mm", 6.0))
    pivot_off   = float(params.get("pivot_offset_mm",  10.0))
    tip_bevel   = float(params.get("tip_bevel_mm",      3.0))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"LENGTH_MM        = {length}",
        f"WIDTH_MM         = {width}",
        f"THICK_MM         = {thick}",
        f"PIVOT_HOLE_DIA   = {pivot_dia}",
        f"PIVOT_OFFSET_MM  = {pivot_off}",
        f"TIP_BEVEL_MM     = {tip_bevel}",
        "# === END PARAMETERS ===",
        "",
        "# --- Body ---",
        "body = rg.Box(rg.Plane.WorldXY,",
        "              rg.Interval(0, LENGTH_MM),",
        "              rg.Interval(0, WIDTH_MM),",
        "              rg.Interval(0, THICK_MM)).ToBrep()",
        "result = body",
        "",
        "# --- Pivot bore ---",
        "pivot_plane = rg.Plane(rg.Point3d(PIVOT_OFFSET_MM, WIDTH_MM / 2.0, -1),",
        "                       rg.Vector3d(0, 0, 1))",
        "pivot_cyl   = rg.Cylinder(rg.Circle(pivot_plane, PIVOT_HOLE_DIA / 2.0),",
        "                          THICK_MM + 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [pivot_cyl], 0.001)",
        "if cut: result = cut[0]",
        "",
        "# --- Tip bevel (diagonal cut at free end) ---",
        "if TIP_BEVEL_MM > 0:",
        "    bevel_pts = [",
        "        rg.Point3d(LENGTH_MM - TIP_BEVEL_MM, 0,          -1),",
        "        rg.Point3d(LENGTH_MM,                 TIP_BEVEL_MM, -1),",
        "        rg.Point3d(LENGTH_MM + 1,             TIP_BEVEL_MM, -1),",
        "        rg.Point3d(LENGTH_MM + 1,             0,          -1),",
        "        rg.Point3d(LENGTH_MM - TIP_BEVEL_MM, 0,          -1),",
        "    ]",
        "    bevel_profile = rg.PolylineCurve(bevel_pts)",
        "    bevel_extr    = rg.Extrusion.Create(bevel_profile, THICK_MM + 2.0, True)",
        "    bevel_brep    = bevel_extr.ToBrep() if bevel_extr else None",
        "    if bevel_brep:",
        "        cut2 = rg.Brep.CreateBooleanDifference([result], [bevel_brep], 0.001)",
        "        if cut2: result = cut2[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_brake_drum
# --------------------------------------------------------------------------- #

def _script_brake_drum(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params    = plan.get("params", {})
    od        = float(params.get("diameter",       200.0))
    width     = float(params.get("width",           40.0))
    shaft_dia = float(params.get("shaft_diameter",  20.0))
    wall      = float(params.get("wall_thickness",   8.0))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"OD_MM        = {od}",
        f"WIDTH_MM     = {width}",
        f"SHAFT_DIA_MM = {shaft_dia}",
        f"WALL_MM      = {wall}",
        "# === END PARAMETERS ===",
        "",
        "# --- Outer cylinder (full solid to start) ---",
        "outer_cyl = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, OD_MM / 2.0),",
        "                        WIDTH_MM).ToBrep(True, True)",
        "",
        "# --- Inner void: open at top (z=WIDTH_MM), floor at z=0 ---",
        "inner_plane  = rg.Plane(rg.Point3d(0, 0, WALL_MM), rg.Vector3d(0, 0, 1))",
        "inner_cyl    = rg.Cylinder(",
        "    rg.Circle(inner_plane, OD_MM / 2.0 - WALL_MM),",
        "    WIDTH_MM - WALL_MM + 1.0).ToBrep(True, False)",
        "hollow = rg.Brep.CreateBooleanDifference([outer_cyl], [inner_cyl], 0.001)",
        "if hollow is None or len(hollow) == 0:",
        "    raise RuntimeError('Brake drum: hollow boolean failed')",
        "result = hollow[0]",
        "",
        "# --- Shaft bore through closed bottom ---",
        "shaft_plane = rg.Plane(rg.Point3d(0, 0, -1), rg.Vector3d(0, 0, 1))",
        "shaft_cyl   = rg.Cylinder(",
        "    rg.Circle(shaft_plane, SHAFT_DIA_MM / 2.0),",
        "    WALL_MM + 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [shaft_cyl], 0.001)",
        "if cut is None or len(cut) == 0:",
        "    raise RuntimeError('Brake drum: shaft bore boolean failed')",
        "result = cut[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_spool
# --------------------------------------------------------------------------- #

def _script_spool(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params    = plan.get("params", {})
    drum_od   = float(params.get("diameter",         600.0))
    drum_w    = float(params.get("width",             50.0))
    flange_od = float(params.get("flange_diameter",  640.0))
    flange_th = float(params.get("flange_thickness",   8.0))
    hub_od    = float(params.get("hub_diameter",      47.2))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"DRUM_OD_MM   = {drum_od}",
        f"DRUM_W_MM    = {drum_w}",
        f"FLANGE_OD_MM = {flange_od}",
        f"FLANGE_TH_MM = {flange_th}",
        f"HUB_OD_MM    = {hub_od}",
        "# === END PARAMETERS ===",
        "",
        "# --- Drum body ---",
        "drum = rg.Cylinder(rg.Circle(rg.Plane.WorldXY, DRUM_OD_MM / 2.0),",
        "                   DRUM_W_MM).ToBrep(True, True)",
        "",
        "# --- Bottom flange (z = -FLANGE_TH_MM to 0) ---",
        "fl_b_plane = rg.Plane(rg.Point3d(0, 0, -FLANGE_TH_MM), rg.Vector3d(0, 0, 1))",
        "fl_b = rg.Cylinder(rg.Circle(fl_b_plane, FLANGE_OD_MM / 2.0),",
        "                   FLANGE_TH_MM).ToBrep(True, True)",
        "",
        "# --- Top flange (z = DRUM_W_MM to DRUM_W_MM + FLANGE_TH_MM) ---",
        "fl_t_plane = rg.Plane(rg.Point3d(0, 0, DRUM_W_MM), rg.Vector3d(0, 0, 1))",
        "fl_t = rg.Cylinder(rg.Circle(fl_t_plane, FLANGE_OD_MM / 2.0),",
        "                   FLANGE_TH_MM).ToBrep(True, True)",
        "",
        "# --- Union drum + flanges ---",
        "united = rg.Brep.CreateBooleanUnion([drum, fl_b, fl_t], 0.001)",
        "if united is None or len(united) == 0:",
        "    raise RuntimeError('Spool: union failed')",
        "result = united[0]",
        "",
        "# --- Hub bore through entire assembly ---",
        "hub_plane = rg.Plane(rg.Point3d(0, 0, -FLANGE_TH_MM - 1.0), rg.Vector3d(0, 0, 1))",
        "hub_cyl   = rg.Cylinder(",
        "    rg.Circle(hub_plane, HUB_OD_MM / 2.0),",
        "    DRUM_W_MM + 2.0 * FLANGE_TH_MM + 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [hub_cyl], 0.001)",
        "if cut is None or len(cut) == 0:",
        "    raise RuntimeError('Spool: hub bore boolean failed')",
        "result = cut[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_trip_lever
# --------------------------------------------------------------------------- #

def _script_trip_lever(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params      = plan.get("params", {})
    length      = float(params.get("length_mm",        80.0))
    width       = float(params.get("width_mm",         12.0))
    thick       = float(params.get("thickness_mm",      5.0))
    pivot_dia   = float(params.get("pivot_hole_dia_mm", 6.0))
    pivot_off   = float(params.get("pivot_offset_mm",  15.0))
    tip_w       = float(params.get("tip_width_mm",      8.0))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"LENGTH_MM       = {length}",
        f"WIDTH_MM        = {width}",
        f"THICK_MM        = {thick}",
        f"PIVOT_HOLE_DIA  = {pivot_dia}",
        f"PIVOT_OFFSET_MM = {pivot_off}",
        f"TIP_WIDTH_MM    = {tip_w}",
        "# === END PARAMETERS ===",
        "",
        "# --- Body ---",
        "body   = rg.Box(rg.Plane.WorldXY,",
        "               rg.Interval(0, LENGTH_MM),",
        "               rg.Interval(0, WIDTH_MM),",
        "               rg.Interval(0, THICK_MM)).ToBrep()",
        "result = body",
        "",
        "# --- Pivot bore ---",
        "piv_plane = rg.Plane(rg.Point3d(PIVOT_OFFSET_MM, WIDTH_MM / 2.0, -1),",
        "                     rg.Vector3d(0, 0, 1))",
        "piv_cyl   = rg.Cylinder(rg.Circle(piv_plane, PIVOT_HOLE_DIA / 2.0),",
        "                        THICK_MM + 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [piv_cyl], 0.001)",
        "if cut is None or len(cut) == 0:",
        "    raise RuntimeError('Trip lever: pivot bore boolean failed')",
        "result = cut[0]",
        "",
        "# --- Narrowed trip tip (taper cut at free end) ---",
        "if TIP_WIDTH_MM < WIDTH_MM:",
        "    side_cut = (WIDTH_MM - TIP_WIDTH_MM) / 2.0",
        "    tip_pts  = [",
        "        rg.Point3d(LENGTH_MM * 0.6,  0,                  -1),",
        "        rg.Point3d(LENGTH_MM + 1,    0,                  -1),",
        "        rg.Point3d(LENGTH_MM + 1,    side_cut,           -1),",
        "        rg.Point3d(LENGTH_MM * 0.6,  0,                  -1),",
        "    ]",
        "    tip_profile = rg.PolylineCurve(tip_pts)",
        "    tip_extr    = rg.Extrusion.Create(tip_profile, THICK_MM + 2.0, True)",
        "    tip_brep    = tip_extr.ToBrep() if tip_extr else None",
        "    if tip_brep:",
        "        cut2 = rg.Brep.CreateBooleanDifference([result], [tip_brep], 0.001)",
        "        if cut2: result = cut2[0]",
        "    # Mirror cut for other side",
        "    tip_pts_r = [",
        "        rg.Point3d(LENGTH_MM * 0.6,  WIDTH_MM,           -1),",
        "        rg.Point3d(LENGTH_MM + 1,    WIDTH_MM,           -1),",
        "        rg.Point3d(LENGTH_MM + 1,    WIDTH_MM - side_cut,-1),",
        "        rg.Point3d(LENGTH_MM * 0.6,  WIDTH_MM,           -1),",
        "    ]",
        "    tip_profile_r = rg.PolylineCurve(tip_pts_r)",
        "    tip_extr_r    = rg.Extrusion.Create(tip_profile_r, THICK_MM + 2.0, True)",
        "    tip_brep_r    = tip_extr_r.ToBrep() if tip_extr_r else None",
        "    if tip_brep_r:",
        "        cut3 = rg.Brep.CreateBooleanDifference([result], [tip_brep_r], 0.001)",
        "        if cut3: result = cut3[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_flyweight
# --------------------------------------------------------------------------- #

def _script_flyweight(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params      = plan.get("params", {})
    mass_dia    = float(params.get("mass_dia_mm",     25.0))
    mass_len    = float(params.get("mass_length_mm",  30.0))
    arm_len     = float(params.get("arm_length_mm",   40.0))
    arm_w       = float(params.get("arm_width_mm",    10.0))
    arm_thick   = float(params.get("arm_thick_mm",     6.0))
    pivot_dia   = float(params.get("pivot_dia_mm",     6.0))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"MASS_DIA_MM   = {mass_dia}",
        f"MASS_LEN_MM   = {mass_len}",
        f"ARM_LEN_MM    = {arm_len}",
        f"ARM_W_MM      = {arm_w}",
        f"ARM_THICK_MM  = {arm_thick}",
        f"PIVOT_DIA_MM  = {pivot_dia}",
        "# === END PARAMETERS ===",
        "",
        "# --- Pivot arm (box) ---",
        "arm = rg.Box(rg.Plane.WorldXY,",
        "             rg.Interval(0, ARM_LEN_MM),",
        "             rg.Interval(0, ARM_W_MM),",
        "             rg.Interval(0, ARM_THICK_MM)).ToBrep()",
        "result = arm",
        "",
        "# --- Mass body (cylinder at far end of arm) ---",
        "mass_plane  = rg.Plane(rg.Point3d(ARM_LEN_MM + MASS_DIA_MM / 2.0,",
        "                                  ARM_W_MM / 2.0, 0),",
        "                       rg.Vector3d(0, 0, 1))",
        "mass_cyl    = rg.Cylinder(rg.Circle(mass_plane, MASS_DIA_MM / 2.0),",
        "                          MASS_LEN_MM).ToBrep(True, True)",
        "united = rg.Brep.CreateBooleanUnion([result, mass_cyl], 0.001)",
        "if united is None or len(united) == 0:",
        "    raise RuntimeError('Flyweight: arm+mass union failed')",
        "result = united[0]",
        "",
        "# --- Pivot bore (at near end of arm) ---",
        "piv_plane = rg.Plane(rg.Point3d(PIVOT_DIA_MM, ARM_W_MM / 2.0, -1),",
        "                     rg.Vector3d(0, 0, 1))",
        "piv_cyl   = rg.Cylinder(rg.Circle(piv_plane, PIVOT_DIA_MM / 2.0),",
        "                        ARM_THICK_MM + 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [piv_cyl], 0.001)",
        "if cut is None or len(cut) == 0:",
        "    raise RuntimeError('Flyweight: pivot bore boolean failed')",
        "result = cut[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template: aria_rope_guide
# --------------------------------------------------------------------------- #

def _script_rope_guide(plan: dict[str, Any], step_path: str, stl_path: str) -> str:
    params      = plan.get("params", {})
    roller_od   = float(params.get("roller_diameter",   30.0))
    roller_w    = float(params.get("roller_width",      30.0))
    brk_thick   = float(params.get("bracket_thickness",  6.0))
    brk_w       = float(params.get("bracket_width",     80.0))
    brk_h       = float(params.get("bracket_height",    40.0))
    bore        = float(params.get("bore",               8.0))

    lines = [
        _SCRIPT_HEADER,
        "# === PART PARAMETERS (tunable) ===",
        f"ROLLER_OD_MM    = {roller_od}",
        f"ROLLER_W_MM     = {roller_w}",
        f"BRACKET_TH_MM   = {brk_thick}",
        f"BRACKET_W_MM    = {brk_w}",
        f"BRACKET_H_MM    = {brk_h}",
        f"BORE_MM         = {bore}",
        "# === END PARAMETERS ===",
        "",
        "# --- Mounting plate ---",
        "plate  = rg.Box(rg.Plane.WorldXY,",
        "               rg.Interval(0, BRACKET_W_MM),",
        "               rg.Interval(0, BRACKET_TH_MM),",
        "               rg.Interval(0, BRACKET_H_MM)).ToBrep()",
        "result = plate",
        "",
        "# --- Roller boss (cylinder protruding from plate centre) ---",
        "boss_cx   = BRACKET_W_MM / 2.0",
        "boss_cz   = BRACKET_H_MM / 2.0",
        "boss_plane = rg.Plane(rg.Point3d(boss_cx, BRACKET_TH_MM, boss_cz),",
        "                      rg.Vector3d(0, 1, 0))",
        "boss_cyl   = rg.Cylinder(rg.Circle(boss_plane, ROLLER_OD_MM / 2.0),",
        "                         ROLLER_W_MM).ToBrep(True, True)",
        "united = rg.Brep.CreateBooleanUnion([result, boss_cyl], 0.001)",
        "if united is None or len(united) == 0:",
        "    raise RuntimeError('Rope guide: boss union failed')",
        "result = united[0]",
        "",
        "# --- Axle bore through roller boss ---",
        "axle_plane = rg.Plane(rg.Point3d(boss_cx, BRACKET_TH_MM - 1.0, boss_cz),",
        "                      rg.Vector3d(0, 1, 0))",
        "axle_cyl   = rg.Cylinder(rg.Circle(axle_plane, BORE_MM / 2.0),",
        "                         ROLLER_W_MM + 2.0).ToBrep(True, True)",
        "cut = rg.Brep.CreateBooleanDifference([result], [axle_cyl], 0.001)",
        "if cut is None or len(cut) == 0:",
        "    raise RuntimeError('Rope guide: axle bore boolean failed')",
        "result = cut[0]",
    ]
    lines.append(_script_footer(step_path, stl_path))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLM fallback
# --------------------------------------------------------------------------- #

def _script_generic_llm(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Path,
) -> str:
    """Call generate_rhino_python() from llm_generator; return raw script string."""
    from aria_os.generators import llm_generator
    return llm_generator.generate_rhino_python(
        plan=plan,
        goal=goal,
        step_path=step_path,
        stl_path=stl_path,
        repo_root=repo_root,
    )


# --------------------------------------------------------------------------- #
# Template dispatch map
# --------------------------------------------------------------------------- #

_TEMPLATE_MAP: dict[str, Any] = {
    "aria_cam_collar":   _script_cam_collar,
    "aria_ratchet_ring": _script_ratchet_ring,
    "aria_housing":      _script_housing,
    "aria_catch_pawl":   _script_catch_pawl,
    "aria_brake_drum":   _script_brake_drum,
    "aria_spool":        _script_spool,
    "aria_trip_lever":   _script_trip_lever,
    "aria_flyweight":    _script_flyweight,
    "aria_rope_guide":   _script_rope_guide,
}


# --------------------------------------------------------------------------- #
# Runner writer
# --------------------------------------------------------------------------- #

def _write_runner(
    runner_path: Path,
    script_path: Path,
    step_path: str,
    stl_path: str,
    part_id: str,
) -> None:
    """Write a Rhino Compute runner script that posts the RhinoCommon script to the API."""
    # Use forward slashes — avoids raw-string escape edge cases on Windows
    sp   = str(script_path).replace("\\", "/")
    step = str(step_path).replace("\\", "/")
    stl  = str(stl_path).replace("\\", "/")

    lines = [
        '"""',
        f"Rhino Compute runner for {part_id}.",
        f'Usage:  python "{sp}"',
        f'        RHINO_COMPUTE_URL=http://your-server:6500 python "{sp}"',
        "See docs/rhino_compute_setup.md for setup.",
        '"""',
        "import json, os, sys",
        "from pathlib import Path",
        "",
        f'SCRIPT_PATH = Path("{sp}")',
        f'STEP_PATH   = "{step}"',
        f'STL_PATH    = "{stl}"',
        f'PART_NAME   = "{part_id}"',
        f'COMPUTE_URL = os.environ.get("RHINO_COMPUTE_URL", "{RHINO_COMPUTE_URL}")',
        "",
        "",
        "def _run():",
        "    import urllib.request",
        "    script_code = SCRIPT_PATH.read_text(encoding='utf-8')",
        "    payload = json.dumps({'script': script_code}).encode('utf-8')",
        "    req = urllib.request.Request(",
        "        f'{COMPUTE_URL}/grasshopper',",
        "        data=payload,",
        "        headers={'Content-Type': 'application/json'},",
        "        method='POST',",
        "    )",
        "    try:",
        "        with urllib.request.urlopen(req, timeout=120) as resp:",
        "            result = json.loads(resp.read())",
        "    except Exception as e:",
        "        print(f'[RHINO-COMPUTE] Unavailable: {e}', file=sys.stderr)",
        f"        print(f'[RHINO-COMPUTE] Artifacts written. Run manually: {script_path.name}', file=sys.stderr)",
        "        sys.exit(0)  # Non-fatal; pipeline continues",
        "    for line in result.get('stdout', '').splitlines():",
        "        print(line)",
        "    rc = result.get('returncode', 0)",
        "    if rc != 0:",
        "        err = result.get('stderr', '')[:300]",
        "        print(f'[RHINO-COMPUTE] Script failed (rc={rc}): {err}', file=sys.stderr)",
        "        sys.exit(rc)",
        "",
        "",
        'if __name__ == "__main__":',
        "    _run()",
        "",
    ]
    runner_path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Validation helper
# --------------------------------------------------------------------------- #

def validate_grasshopper_output(
    step_path: str,
    stdout: str = "",
    min_size_kb: float = 1.0,
) -> dict[str, Any]:
    """Validate Grasshopper/Rhino Compute output."""
    result: dict[str, Any] = {"valid": False, "errors": [], "bbox": None}
    p = Path(step_path)
    if not p.exists():
        result["errors"].append(f"STEP not found: {step_path}")
        return result
    size_kb = p.stat().st_size / 1024
    if size_kb < min_size_kb:
        result["errors"].append(f"STEP too small: {size_kb:.1f} KB (min {min_size_kb} KB)")
        return result
    result["valid"] = True
    for line in stdout.splitlines():
        if line.startswith("BBOX:"):
            try:
                parts = line[5:].split(",")
                result["bbox"] = [float(x) for x in parts[:3]]
            except ValueError:
                pass
            break
    return result


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def write_grasshopper_artifacts(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    """
    Write Grasshopper/RhinoCommon artifacts for a part.

    Routes named parts through _TEMPLATE_MAP; all others fall back to LLM.

    Returns dict with artifact paths:
      - params_path
      - script_path
      - runner_path
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    part_id = (plan.get("part_id") or "aria_part").replace("/", "_")
    out_dir = repo_root / "outputs" / "cad" / "grasshopper" / part_id
    out_dir.mkdir(parents=True, exist_ok=True)

    event_bus.emit("grasshopper", f"Writing artifacts for {part_id}", {"part_id": part_id})

    artifacts: dict[str, str] = {}

    # --- params.json ---
    params_data = {
        "goal":      goal,
        "part_id":   part_id,
        "params":    plan.get("params", {}),
        "step_path": step_path,
        "stl_path":  stl_path,
    }
    params_path = out_dir / "params.json"
    params_path.write_text(json.dumps(params_data, indent=2), encoding="utf-8")
    artifacts["params_path"] = str(params_path)

    # --- Select template or LLM ---
    template_fn = _TEMPLATE_MAP.get(part_id)
    if template_fn is not None:
        event_bus.emit("step", f"Using RhinoCommon template: {part_id}")
        try:
            script_code = template_fn(plan, step_path, stl_path)
        except Exception as e:
            event_bus.emit("error", f"Template failed for {part_id}: {e}")
            script_code = (
                f"# Template generation failed for {part_id}: {e}\n"
                "# Implement RhinoCommon script manually.\n"
            )
    else:
        event_bus.emit("step", f"LLM fallback for {part_id}")
        try:
            script_code = _script_generic_llm(plan, goal, step_path, stl_path, repo_root)
        except Exception as e:
            event_bus.emit("error", f"LLM generation failed for {part_id}: {e}")
            script_code = (
                f"# LLM generation failed for {part_id}: {e}\n"
                "# Implement RhinoCommon script manually.\n"
            )

    script_path_obj = out_dir / f"{part_id}_rhinoscript.py"
    script_path_obj.write_text(script_code, encoding="utf-8")
    artifacts["script_path"] = str(script_path_obj)

    runner_path = out_dir / "run_rhino_compute.py"
    _write_runner(runner_path, script_path_obj, step_path, stl_path, part_id)
    artifacts["runner_path"] = str(runner_path)

    script_size = script_path_obj.stat().st_size
    event_bus.emit(
        "grasshopper",
        f"[GRASSHOPPER] Script ready: {script_path_obj} ({script_size} bytes)",
        {"part_id": part_id, "script_path": str(script_path_obj), "size_bytes": script_size},
    )
    print(f"[GRASSHOPPER] Script ready: {script_path_obj} ({script_size} bytes)")

    return artifacts


# Keep backward-compat alias used by a few older callers
def generate_unknown_part(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    """Alias — routes to write_grasshopper_artifacts (LLM path)."""
    return write_grasshopper_artifacts(plan, goal, step_path, stl_path, repo_root)
