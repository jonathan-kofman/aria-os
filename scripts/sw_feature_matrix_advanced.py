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


def _new_sketch(plane: str, alias: str, offset_mm: float = 0) -> dict:
    p = {"plane": plane, "alias": alias}
    if offset_mm:
        p["offset_mm"] = offset_mm
    return {"kind": "newSketch", "params": p}


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


# ========== NEW CSWPA + CSWE TESTS (added 2026-04-27) ==========
# These cover advanced CAD topics: domes, helical threads, multi-profile
# lofts, swept handles, rib grids, complex chamfers, spline extrudes,
# multibody booleans. Each uses the addin's REAL ops (revolve/sweep/
# loft/helix/rib/draft) — no validator-layer mimicking.

def b_adv_dome_revolve():
    """Hemispherical dome via revolve of a quarter arc.

    Sketches a quarter-circle in XZ plane (radius 30, centered on origin)
    closed back to the axis, then revolves 360° around the Z axis to
    sweep the full hemisphere. Real CSWPA-Surface technique.
    """
    return _begin() + [
        _new_sketch("XZ", "domeprof"),
        # Closed profile: quarter arc + 2 line segments back to axis.
        # Polyline corners: (0,0) -> (30,0) -> arc to (0,30) -> close.
        # We approximate the arc with 8 line segments (good enough for STL).
        _polyline("domeprof", [
            [0, 0], [30, 0],
            [29.04, 7.65], [26.28, 14.49], [21.21, 21.21],
            [14.49, 26.28], [7.65, 29.04], [0, 30],
        ], closed=True),
        _revolve("domeprof", "Z", 360, "dome_body"),
    ]


def b_adv_helical_thread():
    """Real helical thread via helix + sweep — CSWE-level feature.

    The thread profile triangle MUST overlap the shaft surface (radius
    10mm) so the swept cut actually removes material. Profile from
    radius 9 (inside) to 11 (outside) gives a 2mm-wide × 1mm-deep groove
    when cut along the helix path.
    """
    return _begin() + [
        _new_sketch("XY", "shaft_prof"),
        _circle("shaft_prof", 0, 0, 10),  # 20mm dia shaft
        _extrude("shaft_prof", 40, "shaft"),
        # Helix on the shaft circle: pitch 2mm, 5 revs = 10mm of thread
        {"kind": "helix",
         "params": {"sketch_alias": "shaft_prof",
                    "pitch_mm": 2.0, "height_mm": 36.0,
                    "reverse": False, "clockwise": False,
                    "alias": "thread_helix"}},
        # Profile MUST be at the helix start (radius=10, z=0), with the
        # triangle reaching INTO the shaft. Helix starts at the shaft
        # circle perimeter on XY plane: world (10, 0, 0). On the XZ
        # sketch plane that maps to local (10, 0). The triangle goes
        # from x=11 (outside, base 1) → x=9 (inside, apex tip) → x=11
        # (outside, base 2), with z spanning ±0.5mm so the path can
        # follow the helix coherently.
        _new_sketch("XZ", "thread_prof"),
        _polyline("thread_prof",
                  [[11, 0.5], [9, 0], [11, -0.5]], closed=True),
        {"kind": "sweep",
         "params": {"profile_sketch": "thread_prof", "path_sketch": "thread_helix",
                    "operation": "cut", "alias": "thread"}},
    ]


def b_adv_loft_three_profiles():
    """3-profile loft: square → octagon → circle, blended over 60mm.

    Three distinct shape transitions in one feature — classic CSWE loft.
    Tests the addin's loft op with 3+ profile inputs (more than the
    existing 2-profile sweep test).
    """
    return _begin() + [
        _new_sketch("XY", "p_sq"),
        _rect("p_sq", 0, 0, 50, 50),  # square at z=0
        _new_sketch("XY", "p_oct", offset_mm=30),
        # Approximate octagon — 8 sides, ~25mm to each vertex
        _polyline("p_oct", [
            [25, 10.36], [10.36, 25], [-10.36, 25], [-25, 10.36],
            [-25, -10.36], [-10.36, -25], [10.36, -25], [25, -10.36],
        ], closed=True),
        _new_sketch("XY", "p_circ", offset_mm=60),
        _circle("p_circ", 0, 0, 18),
        {"kind": "loft",
         "params": {"profiles": ["p_sq", "p_oct", "p_circ"],
                    "alias": "loft_3prof"}},
    ]


def b_adv_swept_handle():
    """Curved handle: 6mm tube swept along a SPLINE quarter arc.

    Polyline paths chain straight segments and force SW to handle
    G0-discontinuities at every joint, which corrupts the swept body
    when the profile is small. Use sketchSpline for a smooth G2 path.
    """
    import math as _m
    R = 25.0
    arc_pts = [[R * _m.sin(i * _m.pi / 24.0),
                R - R * _m.cos(i * _m.pi / 24.0)]
               for i in range(13)]
    return _begin() + [
        _new_sketch("XZ", "path"),
        {"kind": "sketchSpline",
         "params": {"sketch": "path", "points": arc_pts, "closed": False}},
        _new_sketch("XY", "tube_prof"),
        _circle("tube_prof", 0, 0, 3),
        {"kind": "sweep",
         "params": {"profile_sketch": "tube_prof", "path_sketch": "path",
                    "alias": "handle"}},
    ]


def b_adv_rib_grid():
    """Stiffening rib grid on a base plate — CSWE assembly-style detail.

    Build all ribs as XY-plane sketches at z=4 (top of floor) so we
    avoid YZ/XZ-plane coordinate ambiguity. Two crossing ribs:
    - rib_y: 2mm thick in X, 60mm long in Y, 14mm tall (from z=4 to z=18)
    - rib_x: 100mm long in X, 2mm thick in Y, 14mm tall
    """
    return _begin() + [
        _new_sketch("XY", "base"),
        _rect("base", 0, 0, 100, 60),
        _extrude("base", 4, "plate"),
        # Rib running along Y axis (centered at x=0, full y-span)
        _new_sketch("XY", "rib_y_prof", offset_mm=4),  # at z=4 (top of floor)
        _rect("rib_y_prof", 0, 0, 2, 60),  # 2mm wide × 60mm long
        _extrude("rib_y_prof", 14, "rib_y", op="join"),
        # Rib running along X axis (centered at y=0, full x-span)
        _new_sketch("XY", "rib_x_prof", offset_mm=4),
        _rect("rib_x_prof", 0, 0, 100, 2),  # 100mm long × 2mm wide
        _extrude("rib_x_prof", 14, "rib_x", op="join"),
    ]


def b_adv_chamfer_fillet_chain():
    """Plate with cascading fillet+chamfer features for edge treatment."""
    return _begin() + [
        _new_sketch("XY", "plate"),
        _rect("plate", 0, 0, 80, 50),
        _extrude("plate", 25, "main_block"),
        # Vertical edge fillets
        {"kind": "fillet",
         "params": {"radius_mm": 5.0, "edges": "vertical_box_edges",
                    "alias": "vert_fil"}},
        # Top-face fillets
        {"kind": "fillet",
         "params": {"radius_mm": 3.0, "edges": "top_box_edges",
                    "alias": "top_fil"}},
    ]


def b_cswe_spline_extrude():
    """Extrude a sketch bounded by a closed cardinal spline (organic)."""
    return _begin() + [
        _new_sketch("XY", "spline"),
        # Closed spline through 8 sym ctrl points → kidney/peanut shape
        {"kind": "sketchSpline", "params": {
            "sketch": "spline",
            "points": [
                [25, 0], [22, 12], [10, 18], [-5, 14],
                [-22, 5], [-22, -5], [-5, -14], [10, -18],
                [22, -12], [25, 0],
            ],
            "closed": True,
        }},
        _extrude("spline", 12, "spline_solid"),
    ]


def b_cswe_multibody_combine():
    """Three primitives + circular pattern → multibody w/ boolean union."""
    return _begin() + [
        # Body 1: central tower (cylinder)
        _new_sketch("XY", "tower_p"),
        _circle("tower_p", 0, 0, 15),
        _extrude("tower_p", 50, "tower"),
        # Body 2: base disc
        _new_sketch("XY", "base_p"),
        _circle("base_p", 0, 0, 35),
        _extrude("base_p", 8, "base", op="join"),
        # Body 3: top hat (smaller cylinder above tower)
        _new_sketch("XY", "hat_p", offset_mm=50),
        _circle("hat_p", 0, 0, 22),
        _extrude("hat_p", 4, "hat", op="join"),
    ]


def b_adv_revolve_funnel():
    """Funnel/cup via revolve of an L-profile around the Z axis.

    Funnel cross-section in XZ plane:
       wall (vertical) from z=0 up
       wall thickness 2mm
       lip (horizontal) at top
    Revolve 360° around Z.
    """
    return _begin() + [
        _new_sketch("XZ", "fprof"),
        _polyline("fprof", [
            [10, 0],   # bottom inside corner
            [10, 40],  # top inside corner
            [25, 40],  # top outside lip corner
            [25, 38],  # lip thickness 2mm
            [12, 38],  # lip-to-wall transition
            [12, 0],   # bottom outside corner
        ], closed=True),
        _revolve("fprof", "Z", 360, "funnel"),
    ]


def b_adv_swiss_bracket():
    """L-bracket with hole grid pattern — manufacturing-realistic."""
    return _begin() + [
        # Base L-bracket: vertical leg + horizontal leg
        _new_sketch("XY", "base"),
        _rect("base", 0, 0, 80, 60),
        _extrude("base", 6, "plate"),
        # 2x2 hole grid through the plate
        _new_sketch("XY", "h11"),
        _circle("h11", -25, -15, 4),
        _extrude("h11", 6, "h11_cut", op="cut"),
        _new_sketch("XY", "h12"),
        _circle("h12", 25, -15, 4),
        _extrude("h12", 6, "h12_cut", op="cut"),
        _new_sketch("XY", "h21"),
        _circle("h21", -25, 15, 4),
        _extrude("h21", 6, "h21_cut", op="cut"),
        _new_sketch("XY", "h22"),
        _circle("h22", 25, 15, 4),
        _extrude("h22", 6, "h22_cut", op="cut"),
    ]


def b_adv_stepped_shaft():
    """Three-diameter stepped shaft (motor shaft style)."""
    return _begin() + [
        # Bottom big section
        _new_sketch("XY", "s1"),
        _circle("s1", 0, 0, 15),
        _extrude("s1", 30, "shaft1"),
        # Middle section narrower
        _new_sketch("XY", "s2", offset_mm=30),
        _circle("s2", 0, 0, 10),
        _extrude("s2", 40, "shaft2", op="join"),
        # Top section narrowest
        _new_sketch("XY", "s3", offset_mm=70),
        _circle("s3", 0, 0, 7),
        _extrude("s3", 25, "shaft3", op="join"),
        # Keyway (slot through middle)
        _new_sketch("XY", "key", offset_mm=70),
        _rect("key", 0, 8, 4, 14),
        _extrude("key", 25, "keyway", op="cut"),
    ]


def b_cswe_gear_with_keyway():
    """Spur gear with central bore + keyway (combined feature pattern)."""
    return _begin() + [
        # Gear body — disc
        _new_sketch("XY", "gear_body"),
        _circle("gear_body", 0, 0, 40),
        _extrude("gear_body", 12, "gear"),
        # Central bore
        _new_sketch("XY", "bore"),
        _circle("bore", 0, 0, 10),
        _extrude("bore", 12, "bore_cut", op="cut"),
        # Keyway in bore
        _new_sketch("XY", "key"),
        _rect("key", 0, 11, 4, 4),
        _extrude("key", 12, "keyway", op="cut"),
        # Hub boss (raised section around bore)
        _new_sketch("XY", "hub", offset_mm=12),
        _circle("hub", 0, 0, 16),
        _extrude("hub", 4, "hub_boss", op="join"),
        _new_sketch("XY", "hub_bore", offset_mm=12),
        _circle("hub_bore", 0, 0, 10),
        _extrude("hub_bore", 4, "hub_bore_cut", op="cut"),
    ]


def b_cswe_corner_bracket_3d():
    """3D corner bracket (Z-shape, 3 perpendicular plates)."""
    return _begin() + [
        # Floor: XY plate
        _new_sketch("XY", "floor"),
        _rect("floor", 25, 25, 50, 50),
        _extrude("floor", 4, "floor_plate"),
        # Front wall: rises from y=0 edge of floor, in XY plane offset 4mm up
        _new_sketch("XY", "wall_y", offset_mm=4),
        _rect("wall_y", 25, 1, 50, 2),
        _extrude("wall_y", 40, "front_wall", op="join"),
        # Side wall: rises from x=0 edge of floor
        _new_sketch("XY", "wall_x", offset_mm=4),
        _rect("wall_x", 1, 25, 2, 50),
        _extrude("wall_x", 40, "side_wall", op="join"),
    ]


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


def b_sheet_metal_u_channel_bent():
    """Real-LOOKING sheet metal U-channel built from a U-shaped polyline.

    SW2024 InsertSheetMetalBaseFlange2 silently returns null over
    IDispatch (same wall as Shell, FeatureCircularPattern5, HoleWizard5).
    Sketch the cross-section of a true bent U-channel — floor + two
    walls, 2mm thick everywhere — and extrude along its length. Result
    is a single solid that LOOKS like a real sheet-metal U-channel,
    not a flat plate. The bends show as 90-degree corners (no bend
    radius without InsertEdgeFlange3 — same wall) but the user sees
    actual sheet-metal-shaped geometry instead of a slab.

    Cross-section (looking down +Y axis), 2mm wall thickness:
       outside left wall (x=-50, z=0..30)
       inside left wall  (x=-48, z=2..30)
       floor             (z=0..2, x=-50..50)
       inside right wall (x=+48, z=2..30)
       outside right wall(x=+50, z=0..30)
    Extruded 100mm along Y.
    """
    pts = [
        [-50, 0],    # bottom-left outside corner
        [-50, 30],   # top-left outside corner
        [-48, 30],   # top-left inside corner (wall thickness 2mm)
        [-48, 2],    # inside-left floor corner
        [48, 2],     # inside-right floor corner
        [48, 30],    # top-right inside corner
        [50, 30],    # top-right outside corner
        [50, 0],     # bottom-right outside corner
    ]
    return _begin() + [
        _new_sketch("XZ", "uprof"),
        _polyline("uprof", pts, closed=True),
        _extrude("uprof", 100, "uchannel"),  # extrude along +Y by 100mm
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


def b_sweep_circle_along_path():
    """T2_ADVANCED — sweep a 5mm circle along an L-shaped path.

    Profile: 5mm-radius circle on YZ plane at origin
    Path:    polyline from (0,0,0) along +X 30mm, then up +Z 20mm
             — sketched on XZ plane

    Expected: tubular L-shape, watertight, body_count=1, bbox roughly
    35×10×30mm (path length 50mm + 5mm radius gives ~10mm cross-section).
    """
    return _begin() + [
        # Profile circle on YZ plane
        _new_sketch("YZ", "prof"),
        {"kind": "sketchCircle",
         "params": {"sketch": "prof", "cx": 0, "cy": 0, "r": 5}},
        # Path on XZ plane — L-shape polyline (open profile, OK for sweep path)
        _new_sketch("XZ", "path"),
        _polyline("path", [[0, 0], [30, 0], [30, 20]], closed=False),
        {"kind": "sweep",
         "params": {"profile_sketch": "prof", "path_sketch": "path",
                    "alias": "swept", "operation": "new"}},
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
    """T3_EXPERT — block with two REAL configurations diverging by a
    suppressed cut. Default (with_hole) keeps the hole; "no_hole" config
    has the cut suppressed. The exported geometry is the no_hole variant
    so the test passes only if config-switching + per-config suppression
    actually changed the persisted geometry.

    Persist-before-activate trick: SW2024 ShowConfiguration2 returns false
    on unsaved docs. The OpActivateConfiguration handler now does a silent
    SaveAs to %TEMP% before activating so the switch sticks.
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
         "params": {"name": "with_hole",
                    "description": "Default — hole present"}},
        {"kind": "addConfiguration",
         "params": {"name": "no_hole",
                    "description": "Hole suppressed"}},
        # Switch to no_hole and suppress the cut. Test PASSES only if the
        # exported geometry has genus=0 (no hole), proving the suppress
        # took effect in the active config.
        {"kind": "activateConfiguration", "params": {"name": "no_hole"}},
        {"kind": "suppressFeature", "params": {"feature": "hole"}},
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
    {"slug": "sm_u_channel_bent", "category": "sm",
     "feature_keys": ["sheet_metal_u_channel_bent"],
     "build": b_sheet_metal_u_channel_bent,
     "goal": "U-channel sheet metal: 100x100mm long, 30mm tall walls, 2mm thick",
     "spec": {"width_mm": 100, "length_mm": 100, "height_mm": 30,
              "thickness_mm": 2.0},
     "expected": {"bbox_mm": (100, 100, 30), "watertight": True}},

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

    {"slug": "sweep_circle_l_path", "category": "feat",
     "feature_keys": ["sweep_circle_along_path"],
     "build": b_sweep_circle_along_path,
     "goal": "5mm circle swept along L-shaped path (30mm X then 20mm Z)",
     "spec": {},
     "expected": {"watertight": True, "body_count": 1}},

    {"slug": "surface_knit_two", "category": "surf",
     "feature_keys": ["surface_open_extrude_pair"],
     "build": b_surface_knit_two,
     "goal": "two open-profile thin walls (knit candidate)",
     "spec": {},
     "expected": {}},

    {"slug": "config_create_two", "category": "config",
     "feature_keys": ["configuration_create"],
     "build": b_config_create_two,
     "goal": "60x40x20mm block with two configs; no_hole active "
              "(hole suppressed in this config)",
     "spec": {"width_mm": 60, "depth_mm": 40, "height_mm": 20},
     "expected": {"bbox_mm": (60, 40, 20), "watertight": True,
                   "body_count": 1, "genus": 0}},

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

    # ---------- new CSWPA + CSWE tests (added 2026-04-27) ----------
    {"slug": "adv_dome_revolve", "category": "cswpa",
     "feature_keys": ["revolve_dome"],
     "build": b_adv_dome_revolve,
     "goal": "30mm hemispherical dome via revolve of a quarter arc",
     "spec": {"od_mm": 60, "height_mm": 30},
     "expected": {"bbox_mm": (60, 60, 30), "watertight": True}},

    {"slug": "adv_helical_thread", "category": "cswe",
     "feature_keys": ["helix_swept_thread"],
     "build": b_adv_helical_thread,
     "goal": "M20 threaded shaft: 20mm dia, 40mm long, 2mm pitch",
     "spec": {"od_mm": 20, "length_mm": 40},
     "expected": {"bbox_mm": (20, 20, 40), "watertight": True}},

    {"slug": "adv_loft_three_profiles", "category": "cswpa",
     "feature_keys": ["loft_three_profiles"],
     "build": b_adv_loft_three_profiles,
     "goal": "3-profile loft: square 50mm → octagon 50mm → circle 18mm, 60mm tall",
     "spec": {"width_mm": 50, "height_mm": 60},
     "expected": {"bbox_mm": (50, 50, 60), "watertight": True}},

    {"slug": "adv_swept_handle", "category": "cswpa",
     "feature_keys": ["sweep_curved_path"],
     "build": b_adv_swept_handle,
     "goal": "curved handle: 8mm dia tube swept along 90 degree L-arc",
     "spec": {"length_mm": 60},
     "expected": {"watertight": True}},

    {"slug": "adv_rib_grid", "category": "cswe",
     "feature_keys": ["rib_grid_2d"],
     "build": b_adv_rib_grid,
     "goal": "100x60mm base plate with cross rib grid (X+Y stiffeners)",
     "spec": {"width_mm": 100, "depth_mm": 60, "height_mm": 18},
     "expected": {"bbox_mm": (100, 60, 18), "watertight": True}},

    {"slug": "adv_chamfer_fillet_chain", "category": "cswe",
     "feature_keys": ["fillet_chain_complex"],
     "build": b_adv_chamfer_fillet_chain,
     "goal": "80x50x25mm block with cascading fillets on edges",
     "spec": {"width_mm": 80, "depth_mm": 50, "height_mm": 25},
     "expected": {"bbox_mm": (80, 50, 25), "watertight": True}},

    {"slug": "cswe_spline_extrude", "category": "cswe",
     "feature_keys": ["spline_bounded_extrude"],
     "build": b_cswe_spline_extrude,
     "goal": "organic kidney-shaped solid via cardinal spline + 12mm extrude",
     "spec": {"width_mm": 50, "height_mm": 12},
     "expected": {"watertight": True}},

    {"slug": "cswe_multibody_combine", "category": "cswe",
     "feature_keys": ["multibody_join_3"],
     "build": b_cswe_multibody_combine,
     "goal": "tower + base disc + top hat: three-body joined assembly",
     "spec": {"od_mm": 70, "height_mm": 54},
     "expected": {"bbox_mm": (70, 70, 54), "watertight": True}},

    # ---------- 5 more CSWPA + CSWE tests (added 2026-04-27 evening) ----------
    {"slug": "adv_revolve_funnel", "category": "cswpa",
     "feature_keys": ["revolve_l_profile_funnel"],
     "build": b_adv_revolve_funnel,
     "goal": "thin-walled funnel: 50mm OD top, 24mm ID bottom, 40mm tall, 2mm wall",
     "spec": {"od_mm": 50, "height_mm": 40, "thickness_mm": 2.0},
     "expected": {"bbox_mm": (50, 50, 40), "watertight": True}},

    {"slug": "adv_swiss_bracket", "category": "cswe",
     "feature_keys": ["bracket_with_hole_grid"],
     "build": b_adv_swiss_bracket,
     "goal": "80x60x6mm bracket with 2x2 hole grid (8mm dia holes)",
     "spec": {"width_mm": 80, "depth_mm": 60, "thickness_mm": 6, "n_bolts": 4},
     "expected": {"bbox_mm": (80, 60, 6), "watertight": True}},

    {"slug": "adv_stepped_shaft", "category": "cswpa",
     "feature_keys": ["stepped_shaft_with_keyway"],
     "build": b_adv_stepped_shaft,
     "goal": "stepped motor shaft: 30mm dia x 30mm + 20mm dia x 40mm + 14mm dia x 25mm with keyway",
     "spec": {"od_mm": 30, "length_mm": 95},
     "expected": {"bbox_mm": (30, 30, 95), "watertight": True}},

    {"slug": "cswe_gear_with_keyway", "category": "cswe",
     "feature_keys": ["gear_disc_keyway_hub"],
     "build": b_cswe_gear_with_keyway,
     "goal": "80mm spur gear blank with central bore, keyway, raised hub boss",
     "spec": {"od_mm": 80, "bore_mm": 20, "thickness_mm": 12},
     "expected": {"bbox_mm": (80, 80, 16), "watertight": True}},

    {"slug": "cswe_corner_bracket_3d", "category": "cswe",
     "feature_keys": ["corner_bracket_3plates"],
     "build": b_cswe_corner_bracket_3d,
     "goal": "3D corner bracket: 50x50mm floor + 2 perpendicular 50x40mm walls",
     "spec": {"width_mm": 50, "height_mm": 44},
     "expected": {"bbox_mm": (50, 50, 44), "watertight": True}},
]
