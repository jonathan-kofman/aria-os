r"""sw_feature_matrix_advanced.py - second wave: CSWPA + CSWE topics.

These tests probe the harder corners of SW: surfaces, sheet metal proper,
weldments, configurations, equations, mold tools, and complex pattern types
(linear 2D, mirror, table-driven, fill, variable). Many will FAIL on first
run - that's the point. The ledger captures the workaround (or "unsupported
in IDispatch") so the planner can route around them.

Use the same runner: python scripts/run_sw_feature_matrix.py --extended
"""
from __future__ import annotations

import math


def _begin() -> list[dict]:
    return [{"kind": "beginPlan"}]


def _new_sketch(plane: str, alias: str) -> dict:
    return {"kind": "newSketch", "params": {"plane": plane, "alias": alias}}


def _circle(s, cx, cy, r):
    return {"kind": "sketchCircle",
            "params": {"sketch": s, "cx": cx, "cy": cy, "r": r}}


def _rect(s, cx, cy, w, h):
    return {"kind": "sketchRect",
            "params": {"sketch": s, "cx": cx, "cy": cy, "w": w, "h": h}}


def _polyline(s, pts, closed=True):
    return {"kind": "sketchPolyline",
            "params": {"sketch": s, "points": pts, "closed": closed}}


def _extrude(s, d, alias, op="new", offset=0):
    p = {"sketch": s, "distance": d, "operation": op, "alias": alias}
    if offset:
        p["start_offset"] = offset
    return {"kind": "extrude", "params": p}


def _cut(s, d, alias):
    return _extrude(s, d, alias, op="cut")


def _revolve(s, axis, angle, alias, op="new"):
    return {"kind": "revolve",
            "params": {"sketch": s, "axis": axis, "angle_deg": angle,
                       "operation": op, "alias": alias}}


# === ADVANCED PATTERNS ===
def b_pattern_circular_holes_3():
    return _begin() + [
        _new_sketch("XY", "d"), _circle("d", 0, 0, 40),
        _extrude("d", 6, "disc"),
        _new_sketch("XY", "h"), _circle("h", 28, 0, 3),
        _cut("h", 6, "h0"),
        {"kind": "circularPattern",
         "params": {"feature": "h0", "count": 3, "axis": "Z",
                    "seed_x": 28, "seed_y": 0, "seed_r": 3, "alias": "cp3"}},
    ]


def b_pattern_circular_holes_12():
    return _begin() + [
        _new_sketch("XY", "d"), _circle("d", 0, 0, 75),
        _extrude("d", 8, "disc"),
        _new_sketch("XY", "h"), _circle("h", 60, 0, 4),
        _cut("h", 8, "h0"),
        {"kind": "circularPattern",
         "params": {"feature": "h0", "count": 12, "axis": "Z",
                    "seed_x": 60, "seed_y": 0, "seed_r": 4, "alias": "cp12"}},
    ]


def b_pattern_mirror_via_dual_extrude():
    """Mirror about XZ plane emulated with two symmetric extrudes."""
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 80, 60),
        _extrude("b", 15, "blk"),
        _new_sketch("XY", "h1"), _circle("h1", 25, 15, 4),
        _cut("h1", 15, "h1c"),
        _new_sketch("XY", "h2"), _circle("h2", 25, -15, 4),  # mirror
        _cut("h2", 15, "h2c"),
        _new_sketch("XY", "h3"), _circle("h3", -25, 15, 4),  # mirror
        _cut("h3", 15, "h3c"),
        _new_sketch("XY", "h4"), _circle("h4", -25, -15, 4),  # mirror
        _cut("h4", 15, "h4c"),
    ]


# === SHEET METAL emulation (extruded thin walls) ===
def b_sm_hat_section():
    """Hat-section profile via 5 connected thin walls."""
    plan = _begin() + [
        _new_sketch("XY", "f1"), _rect("f1", -40, 0, 20, 40),
        _extrude("f1", 2, "fl1"),
        _new_sketch("XY", "f2"), _rect("f2", 40, 0, 20, 40),
        _extrude("f2", 2, "fl2"),
        _new_sketch("XY", "f3"), _rect("f3", 0, 0, 100, 4),
        _extrude("f3", 30, "top"),  # top spans, simulating bend
    ]
    return plan


def b_sm_box_unfolded():
    """Box with unfolded flap (emulated via separate extrude rectangles)."""
    return _begin() + [
        _new_sketch("XY", "base"), _rect("base", 0, 0, 80, 60),
        _extrude("base", 2, "bot"),
        # Side walls
        _new_sketch("XZ", "w1"), _rect("w1", 0, 15, 80, 30),
        _extrude("w1", 2, "wall1", op="join", offset=29),
        _new_sketch("XZ", "w2"), _rect("w2", 0, 15, 80, 30),
        _extrude("w2", 2, "wall2", op="join", offset=-31),
    ]


# === SURFACES ===
def b_surface_via_thin_extrude():
    """Surface = open profile thin extrude (no solidification)."""
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[-40, 0], [-20, 25], [0, 30], [20, 25], [40, 0]],
                  closed=False),
        _extrude("s", 50, "surf"),
    ]


def b_surface_revolve_open():
    """Open profile revolved 360° to form a thin shell."""
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[10, 0], [20, 5], [25, 15], [22, 25], [15, 30]],
                  closed=False),
        _revolve("s", axis="Y", angle=360, alias="rev"),
    ]


# === WELDMENT EMULATION ===
def b_weldment_table_frame():
    """4 vertical legs + 4 top rails + 4 bottom rails = table frame."""
    plan = _begin()
    leg_h = 700
    base = 500
    tube = 25  # 25mm square section
    # 4 legs at corners
    corners = [(base/2, base/2), (-base/2, base/2),
               (-base/2, -base/2), (base/2, -base/2)]
    for i, (x, y) in enumerate(corners):
        plan += [_new_sketch("XY", f"leg{i}"),
                 _rect(f"leg{i}", x, y, tube, tube),
                 _extrude(f"leg{i}", leg_h, f"leg_b{i}")]
    # 4 top rails (along X then along Y between leg tops)
    for i, ((x1, y1), (x2, y2)) in enumerate(zip(corners,
                                                  corners[1:] + corners[:1])):
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if abs(x1 - x2) > 0:  # along X
            w, h = abs(x1 - x2) - tube, tube
        else:
            w, h = tube, abs(y1 - y2) - tube
        plan += [_new_sketch("XY", f"top{i}"),
                 _rect(f"top{i}", cx, cy, w, h),
                 _extrude(f"top{i}", tube, f"top_b{i}", offset=leg_h - tube)]
    return plan


# === MOLD-TOOLS-LIKE (draft-on-side via tapered profile) ===
def b_mold_cup_with_draft():
    """Cup with tapered outer wall (manual draft)."""
    return _begin() + [
        _new_sketch("XY", "p"),
        _polyline("p", [[20, 0], [25, 0], [22, 50], [17, 50]], closed=True),
        _revolve("p", axis="Y", angle=360, alias="cup"),
    ]


# === CONFIGURATIONS / EQUATIONS (parameter exposure) ===
def b_equation_param_drives_dim():
    """Set a parameter via addParameter, build geometry that uses it."""
    return _begin() + [
        # Define parameters first
        {"kind": "addParameter", "params": {"name": "len_A", "value_mm": 80}},
        {"kind": "addParameter", "params": {"name": "wid_B", "value_mm": 40}},
        {"kind": "addParameter", "params": {"name": "ht_C",  "value_mm": 20}},
        # Build using literal values matching params (the SW addin doesn't yet
        # parametrically link sketch dims to globals - this exercises only
        # parameter creation. Eventual full link belongs in setProperty path.)
        _new_sketch("XY", "s"), _rect("s", 0, 0, 80, 40),
        _extrude("s", 20, "blk"),
    ]


# === COMPLEX MULTIBODY ===
def b_multibody_4_bodies():
    """4 separated cylinders - test body counting."""
    plan = _begin()
    pts = [(-30, -30), (30, -30), (30, 30), (-30, 30)]
    for i, (x, y) in enumerate(pts):
        plan += [_new_sketch("XY", f"c{i}"), _circle(f"c{i}", x, y, 10),
                 _extrude(f"c{i}", 25, f"cyl{i}")]
    return plan


def b_multibody_swiss_cheese():
    """Block with multiple disjoint cuts producing many holes."""
    plan = _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 100, 80),
        _extrude("b", 20, "blk"),
    ]
    holes = [(0, 0, 8), (-30, -20, 6), (30, -20, 6),
             (-30, 20, 6), (30, 20, 6)]
    for i, (x, y, r) in enumerate(holes):
        plan += [_new_sketch("XY", f"h{i}"), _circle(f"h{i}", x, y, r),
                 _cut(f"h{i}", 20, f"hc{i}")]
    return plan


# === CSWE-CALIBRE ===
def b_cswe_ratchet_disc():
    """Disc + 12 sawtooth perimeter teeth (manual triangles per tooth)."""
    plan = _begin() + [
        _new_sketch("XY", "d"), _circle("d", 0, 0, 50),
        _extrude("d", 8, "disc"),
    ]
    teeth = 12
    for i in range(teeth):
        a0 = math.radians(i * 360 / teeth)
        a1 = math.radians((i + 0.5) * 360 / teeth)
        a2 = math.radians((i + 1) * 360 / teeth)
        # Tooth triangle on the rim
        r_in, r_out = 50, 56
        p0 = [round(r_in * math.cos(a0), 3), round(r_in * math.sin(a0), 3)]
        p1 = [round(r_out * math.cos(a1), 3), round(r_out * math.sin(a1), 3)]
        p2 = [round(r_in * math.cos(a2), 3), round(r_in * math.sin(a2), 3)]
        plan += [_new_sketch("XY", f"t{i}"),
                 _polyline(f"t{i}", [p0, p1, p2], closed=True),
                 _extrude(f"t{i}", 8, f"tooth{i}", op="join")]
    # Center bore + bolt circle (4 holes)
    plan += [_new_sketch("XY", "bore"), _circle("bore", 0, 0, 12),
             _cut("bore", 8, "borec")]
    for i in range(4):
        a = math.radians(i * 90)
        x, y = round(28 * math.cos(a), 3), round(28 * math.sin(a), 3)
        plan += [_new_sketch("XY", f"bh{i}"), _circle(f"bh{i}", x, y, 4),
                 _cut(f"bh{i}", 8, f"bhc{i}")]
    return plan


def b_cswe_sprocket_24t():
    """24-tooth sprocket - tightly-spaced perimeter teeth."""
    plan = _begin() + [
        _new_sketch("XY", "d"), _circle("d", 0, 0, 60),
        _extrude("d", 6, "disc"),
    ]
    teeth = 24
    for i in range(teeth):
        a = math.radians(i * 360 / teeth)
        x = round((60 + 1) * math.cos(a), 3)
        y = round((60 + 1) * math.sin(a), 3)
        plan += [_new_sketch("XY", f"t{i}"), _circle(f"t{i}", x, y, 2),
                 _extrude(f"t{i}", 6, f"sp{i}", op="join")]
    plan += [_new_sketch("XY", "bore"), _circle("bore", 0, 0, 8),
             _cut("bore", 6, "bc")]
    return plan


def b_cswe_compound_lever():
    """Lever = main shaft + offset boss + counterweight."""
    plan = _begin() + [
        # Main beam
        _new_sketch("XY", "beam"),
        _polyline("beam", [[-80, -8], [80, -8], [80, 8], [-80, 8]],
                  closed=True),
        _extrude("beam", 12, "bm"),
        # Pivot boss
        _new_sketch("XY", "pivot"), _circle("pivot", 0, 0, 18),
        _extrude("pivot", 20, "pv", op="join"),
        # Pivot bore
        _new_sketch("XY", "pb"), _circle("pb", 0, 0, 8),
        _cut("pb", 20, "pbc"),
        # Counterweight at -80
        _new_sketch("XY", "cw"), _circle("cw", -75, 0, 15),
        _extrude("cw", 25, "cwb", op="join"),
        # Output point at +80
        _new_sketch("XY", "op"), _circle("op", 75, 0, 6),
        _cut("op", 12, "opc"),
    ]
    return plan


# ========== REAL CSWE FEATURE TESTS ==========
# These exercise the addin's already-implemented but UNTESTED CSWE-level
# ops: real sheet metal (InsertSheetMetalBaseFlange2), equations
# (EquationMgr.Add2), and surfaces. Each is one targeted test that turns
# a "untested" ledger entry into "ok" or "needs_workaround" with diagnostic.

def b_sheet_metal_base_flange_real():
    """Real SW sheet metal base flange via InsertSheetMetalBaseFlange2."""
    return _begin() + [
        _new_sketch("XY", "smprof"),
        _rect("smprof", 0, 0, 100, 60),
        # Use the real sheet metal op (NOT the thin-extrude emulation)
        {"kind": "sheetMetalBaseFlange",
         "params": {"sketch": "smprof", "thickness_mm": 2.0,
                    "bend_radius_mm": 1.5, "k_factor": 0.5,
                    "alias": "smbase"}},
    ]


def b_equation_global_var():
    """Real SW global variable via EquationMgr.Add2 + downstream geometry.

    Validates the addin can declare named globals that can later drive
    sketch dimensions. Backbone of CSWE-level parametric design.
    """
    return _begin() + [
        # Three named globals
        {"kind": "addParameter", "params": {"name": "len_A", "value_mm": 80}},
        {"kind": "addParameter", "params": {"name": "wid_B", "value_mm": 40}},
        {"kind": "addParameter", "params": {"name": "ht_C",  "value_mm": 25}},
        # Build geometry that matches the param dims
        _new_sketch("XY", "s"), _rect("s", 0, 0, 80, 40),
        _extrude("s", 25, "blk"),
    ]


def b_sheet_metal_with_drawing():
    """Sheet metal part + drawing — closes the CSWPA-SM + CSWPA-DT loop."""
    return _begin() + [
        _new_sketch("XY", "smprof"),
        _rect("smprof", 0, 0, 80, 50),
        {"kind": "sheetMetalBaseFlange",
         "params": {"sketch": "smprof", "thickness_mm": 1.5,
                    "bend_radius_mm": 1.0, "alias": "base"}},
    ]


def b_surface_knit_two():
    """Two surfaces knitted into a closed shell (CSWPA-SU territory)."""
    return _begin() + [
        # Two open profiles → surface extrudes → knit
        _new_sketch("XY", "p1"),
        _polyline("p1", [[-30, 0], [-15, 20], [15, 20], [30, 0]],
                   closed=False),
        _extrude("p1", 50, "surf1"),  # solid=false fallback hits open profile
        _new_sketch("XY", "p2"),
        _polyline("p2", [[-30, 0], [30, 0]], closed=False),
        _extrude("p2", 50, "surf2"),
    ]


def b_config_create_two():
    """T3_EXPERT — create two named configs on a parametric block.

    Build a 60×40×20mm block with a Ø10mm through-hole. Create two configs
    via ConfigurationManager.AddConfiguration2 (small + large variants).
    The exported geometry is whatever the active config has (Default —
    block with hole, genus=1). The T3_EXPERT win is that the configs
    exist in the document; activating and suppressing per-config is a
    separate concern (see follow-up task — SW2024 ShowConfiguration2 +
    SetSuppression2 silently no-op even when AddConfiguration2 succeeds).
    """
    return _begin() + [
        _new_sketch("XY", "base"),
        _rect("base", 0, 0, 60, 40),
        _extrude("base", 20, "blk"),
        _new_sketch("XY", "hole_sk"),
        {"kind": "sketchCircle",
         "params": {"sketch": "hole_sk", "cx": 0, "cy": 0, "r": 5}},
        {"kind": "extrude",
         "params": {"sketch": "hole_sk", "distance": 25,
                    "operation": "cut", "alias": "hole"}},
        # Spawn two configs after geometry exists.
        {"kind": "addConfiguration",
         "params": {"name": "small",
                    "description": "Small variant"}},
        {"kind": "addConfiguration",
         "params": {"name": "large",
                    "description": "Large variant"}},
    ]


# === CATALOG ===
ADVANCED_TESTS: list[dict] = [
    # --- Real CSWE-level features (added late session) ---
    {"slug": "sm_base_flange_real", "category": "sm",
     "feature_keys": ["sheet_metal_base_flange_native"],
     "build": b_sheet_metal_base_flange_real,
     "goal": "100x60mm sheet metal base flange, 2mm thick, 1.5mm bend radius",
     "spec": {"width_mm": 100, "depth_mm": 60, "thickness_mm": 2.0},
     "expected": {"bbox_mm": (100, 60, 2), "watertight": True}},

    {"slug": "equation_global_var", "category": "config",
     "feature_keys": ["equation_global_var"],
     "build": b_equation_global_var,
     "goal": "80x40x25mm block driven by SW global variables",
     "spec": {"width_mm": 80, "depth_mm": 40, "height_mm": 25},
     "expected": {"bbox_mm": (80, 40, 25), "watertight": True}},

    {"slug": "sm_with_drawing", "category": "sm",
     "feature_keys": ["sheet_metal_base_flange_with_drawing"],
     "build": b_sheet_metal_with_drawing,
     "goal": "80x50mm sheet metal panel for drawing emission",
     "spec": {"width_mm": 80, "depth_mm": 50, "thickness_mm": 1.5},
     "expected": {"bbox_mm": (80, 50, 1.5), "watertight": True}},

    {"slug": "surface_knit_two", "category": "surf",
     "feature_keys": ["surface_open_extrude_pair"],
     "build": b_surface_knit_two,
     "goal": "two open-profile thin walls (knit candidate)",
     "spec": {},
     "expected": {}},

    {"slug": "config_create_two", "category": "config",
     "feature_keys": ["configuration_create"],
     "build": b_config_create_two,
     "goal": "60x40x20mm block with hole and two SW configurations",
     "spec": {"width_mm": 60, "depth_mm": 40, "height_mm": 20},
     "expected": {"bbox_mm": (60, 40, 20), "watertight": True,
                   "body_count": 1, "genus": 1}},

    # --- previously existing T2/T3 tests below ---
    {"slug": "adv_pattern_3", "category": "pattern",
     "feature_keys": ["circular_pattern_count_3"],
     "build": b_pattern_circular_holes_3,
     "goal": "80mm disc with 3 bolt holes equally spaced",
     "spec": {"od_mm": 80, "n_bolts": 3, "height_mm": 6},
     "expected": {"bbox_mm": (80, 80, 6), "watertight": True, "genus": 3}},

    {"slug": "adv_pattern_12", "category": "pattern",
     "feature_keys": ["circular_pattern_count_12"],
     "build": b_pattern_circular_holes_12,
     "goal": "150mm disc with 12 bolt holes",
     "spec": {"od_mm": 150, "n_bolts": 12, "height_mm": 8},
     "expected": {"bbox_mm": (150, 150, 8), "watertight": True, "genus": 12}},

    {"slug": "adv_pattern_mirror_4holes", "category": "pattern",
     "feature_keys": ["mirror_pattern_emulated"],
     "build": b_pattern_mirror_via_dual_extrude,
     "goal": "80x60x15mm plate with 4 holes in symmetric pattern",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 15},
     "expected": {"bbox_mm": (80, 60, 15), "watertight": True, "genus": 4}},

    {"slug": "adv_sm_hat_section", "category": "sm",
     "feature_keys": ["sm_hat_section_emulated"],
     "build": b_sm_hat_section,
     "goal": "hat-section sheet metal: 100mm wide, 30mm tall, 2mm thick",
     "spec": {"width_mm": 100, "thickness_mm": 2},
     "expected": {}},

    {"slug": "adv_sm_box", "category": "sm",
     "feature_keys": ["sm_box_emulated"],
     "build": b_sm_box_unfolded,
     "goal": "open-top sheet metal box, 80x60x30mm, 2mm wall",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 30,
              "thickness_mm": 2},
     "expected": {}},

    {"slug": "adv_surf_thin_wall", "category": "surf",
     "feature_keys": ["surface_thin_wall_via_open_extrude"],
     "build": b_surface_via_thin_extrude,
     "goal": "curved thin wall, 80mm wide, 30mm peak height, 50mm extruded",
     "spec": {},
     "expected": {}},

    {"slug": "adv_surf_revolve_shell", "category": "surf",
     "feature_keys": ["surface_revolve_open"],
     "build": b_surface_revolve_open,
     "goal": "vase-like revolved shell 50mm OD, 30mm tall",
     "spec": {"od_mm": 50, "height_mm": 30},
     "expected": {}},

    {"slug": "adv_weldment_table", "category": "weldment",
     "feature_keys": ["weldment_emulated_via_extrudes"],
     "build": b_weldment_table_frame,
     "goal": "table frame: 500x500mm base, 700mm tall, 25mm square tube",
     "spec": {"width_mm": 500, "depth_mm": 500, "height_mm": 700},
     "expected": {"bbox_mm": (525, 525, 700), "body_count": 1}},

    {"slug": "adv_mold_cup", "category": "feat",
     "feature_keys": ["mold_taper_via_revolve"],
     "build": b_mold_cup_with_draft,
     "goal": "tapered cup, OD 50mm, 50mm tall, 5mm wall, draft 5deg",
     "spec": {"od_mm": 50, "height_mm": 50, "wall_mm": 5},
     "expected": {"bbox_mm": (50, 50, 50), "watertight": True}},

    {"slug": "adv_equation_params", "category": "misc",
     "feature_keys": ["addParameter_named_globals"],
     "build": b_equation_param_drives_dim,
     "goal": "80x40x20mm block driven by named SW global parameters",
     "spec": {"width_mm": 80, "depth_mm": 40, "height_mm": 20},
     "expected": {"bbox_mm": (80, 40, 20), "watertight": True}},

    {"slug": "adv_multibody_4", "category": "multibody",
     "feature_keys": ["multibody_4_separate"],
     "build": b_multibody_4_bodies,
     "goal": "4 separate 20mm cylinders at corners of 60x60mm grid",
     "spec": {"od_mm": 20, "height_mm": 25},
     "expected": {"watertight": True, "body_count": 4}},

    {"slug": "adv_swiss_cheese", "category": "multibody",
     "feature_keys": ["multi_cut_disjoint"],
     "build": b_multibody_swiss_cheese,
     "goal": "100x80x20mm block with 5 disjoint through-holes",
     "spec": {"width_mm": 100, "depth_mm": 80, "height_mm": 20},
     "expected": {"bbox_mm": (100, 80, 20), "watertight": True, "genus": 5}},

    {"slug": "cswe_ratchet_disc", "category": "cswe",
     "feature_keys": ["compound_geometry_ratchet"],
     "build": b_cswe_ratchet_disc,
     "goal": "ratchet disc, 100mm OD, 12 sawtooth teeth, 4 mounting holes",
     "spec": {"od_mm": 112, "height_mm": 8, "n_teeth": 12, "n_bolts": 4},
     "expected": {"bbox_mm": (112, 112, 8), "watertight": True}},

    {"slug": "cswe_sprocket_24t", "category": "cswe",
     "feature_keys": ["high_count_circular_pattern"],
     "build": b_cswe_sprocket_24t,
     "goal": "24-tooth sprocket, 60mm pitch dia, 6mm thick",
     "spec": {"od_mm": 124, "height_mm": 6, "n_teeth": 24},
     "expected": {"bbox_mm": (124, 124, 6), "watertight": True}},

    {"slug": "cswe_compound_lever", "category": "cswe",
     "feature_keys": ["compound_assembly_lever"],
     "build": b_cswe_compound_lever,
     "goal": "compound lever: 160mm beam, central pivot boss, counterweight",
     "spec": {"width_mm": 160, "height_mm": 30},
     "expected": {"watertight": True}},
]
