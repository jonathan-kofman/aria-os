r"""sw_feature_matrix.py - the catalog of SW micro-tests.

Each test exercises ONE feature (or a small bundle that's pointless to test in
isolation, like sketch+extrude). Every test ships:
  - slug:          unique short name (used for filenames)
  - category:      sketch | feat | pattern | sm | surf | drw | multibody | misc
  - feature_keys:  list of ledger keys this test reports against
  - goal:          natural-language goal text (fed to visual_verifier)
  - spec:          dict of expected dimensions for geometry precheck
  - expected:      dict of strict checks (bbox, watertight, body_count, holes)
  - build:         fn() -> list of {kind,params} ops

The runner builds, exports, geometry-prechecks, and updates the ledger.
"""
from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Plan helpers
# ---------------------------------------------------------------------------
def _begin() -> list[dict]:
    return [{"kind": "beginPlan"}]


def _new_sketch(plane: str, alias: str) -> dict:
    return {"kind": "newSketch", "params": {"plane": plane, "alias": alias}}


def _circle(sketch: str, cx: float, cy: float, r: float) -> dict:
    return {"kind": "sketchCircle",
            "params": {"sketch": sketch, "cx": cx, "cy": cy, "r": r}}


def _rect(sketch: str, cx: float, cy: float, w: float, h: float) -> dict:
    return {"kind": "sketchRect",
            "params": {"sketch": sketch, "cx": cx, "cy": cy, "w": w, "h": h}}


def _polyline(sketch: str, points: list, closed: bool = True) -> dict:
    return {"kind": "sketchPolyline",
            "params": {"sketch": sketch, "points": points, "closed": closed}}


def _spline(sketch: str, points: list) -> dict:
    return {"kind": "sketchSpline",
            "params": {"sketch": sketch, "points": points}}


def _extrude(sketch: str, distance: float, alias: str,
             operation: str = "new", start_offset: float = 0.0) -> dict:
    p = {"sketch": sketch, "distance": distance,
         "operation": operation, "alias": alias}
    if start_offset:
        p["start_offset"] = start_offset
    return {"kind": "extrude", "params": p}


def _cut(sketch: str, distance: float, alias: str) -> dict:
    return {"kind": "extrude",
            "params": {"sketch": sketch, "distance": distance,
                       "operation": "cut", "alias": alias}}


def _revolve(sketch: str, axis: str, angle_deg: float, alias: str,
             operation: str = "new") -> dict:
    return {"kind": "revolve",
            "params": {"sketch": sketch, "axis": axis,
                       "angle_deg": angle_deg, "operation": operation,
                       "alias": alias}}


def _fillet(edges: list, radius: float, alias: str) -> dict:
    return {"kind": "fillet",
            "params": {"edges": edges, "radius": radius, "alias": alias}}


def _shell(thickness: float, remove: list | None = None) -> dict:
    p: dict = {"thickness": thickness}
    if remove:
        p["remove_faces"] = remove
    return {"kind": "shell", "params": p}


def _circular_pattern(feature: str, count: int, axis: str = "Z",
                      seed_x: float = 0, seed_y: float = 0,
                      seed_r: float = 0, alias: str = "cp") -> dict:
    return {"kind": "circularPattern",
            "params": {"feature": feature, "count": count, "axis": axis,
                       "seed_x": seed_x, "seed_y": seed_y,
                       "seed_r": seed_r, "alias": alias}}


# ---------------------------------------------------------------------------
# Builders - one per feature
# ---------------------------------------------------------------------------

# === SKETCH === #
def b_sketch_circle():
    return _begin() + [
        _new_sketch("XY", "s"), _circle("s", 0, 0, 20),
        _extrude("s", 10, "cyl"),
    ]


def b_sketch_rect():
    return _begin() + [
        _new_sketch("XY", "s"), _rect("s", 0, 0, 60, 40),
        _extrude("s", 15, "box"),
    ]


def b_sketch_polyline_triangle():
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[0, 0], [40, 0], [20, 35]], closed=True),
        _extrude("s", 12, "tri"),
    ]


def b_sketch_polyline_pentagon():
    pts = []
    for i in range(5):
        a = math.radians(90 + i * 72)
        pts.append([round(25 * math.cos(a), 3),
                    round(25 * math.sin(a), 3)])
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", pts, closed=True),
        _extrude("s", 10, "pent"),
    ]


def b_sketch_polyline_hexagon():
    pts = []
    for i in range(6):
        a = math.radians(i * 60)
        pts.append([round(30 * math.cos(a), 3),
                    round(30 * math.sin(a), 3)])
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", pts, closed=True),
        _extrude("s", 10, "hex"),
    ]


def b_sketch_spline_blob():
    pts = [[0, 0], [20, 5], [35, -10], [50, 5], [60, 0]]
    return _begin() + [
        _new_sketch("XY", "s"),
        _spline("s", pts + pts[::-1][1:]),
        _extrude("s", 8, "spl"),
    ]


# === FEATURE - ADDITIVE === #
def b_extrude_blind():
    return _begin() + [
        _new_sketch("XY", "s"), _circle("s", 0, 0, 15),
        _extrude("s", 25, "cyl"),
    ]


def b_extrude_midplane():
    """Symmetrical about the sketch plane."""
    return _begin() + [
        _new_sketch("XY", "s"), _rect("s", 0, 0, 50, 30),
        _extrude("s", 40, "midext", start_offset=-20),
    ]


def b_revolve_full():
    """Revolve a profile 360° around the Y-axis to get a torus-like shape."""
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[20, 0], [30, 0], [30, 10], [20, 10]], closed=True),
        _revolve("s", axis="Y", angle_deg=360, alias="rev"),
    ]


def b_revolve_partial():
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[20, 0], [40, 0], [40, 15], [20, 15]], closed=True),
        _revolve("s", axis="Y", angle_deg=180, alias="rev180"),
    ]


def b_helix_constant_pitch():
    return _begin() + [
        _new_sketch("XY", "s"), _circle("s", 0, 0, 10),
        {"kind": "helix",
         "params": {"sketch": "s", "pitch_mm": 5, "revolutions": 4,
                    "alias": "hp"}},
    ]


def b_loft_two_profiles():
    """Loft from a 25mm circle on XY to a 15mm circle on a plane offset +50.

    Requires newSketch to support offset_mm (creates a parallel reference
    plane and sketches on it). Without offset both profiles are coplanar
    and SW refuses the loft. Geometry: a frustum-like body, ~50mm tall,
    bbox ~50x50x50mm, watertight.
    """
    return _begin() + [
        _new_sketch("XY", "p1"), _circle("p1", 0, 0, 25),
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "p2", "offset_mm": 50}},
        _circle("p2", 0, 0, 15),
        {"kind": "loft",
         "params": {"profile_sketches": ["p1", "p2"], "alias": "lof"}},
    ]


# === FEATURE - SUBTRACTIVE === #
def b_extrude_cut():
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 60, 60),
        _extrude("b", 20, "blk"),
        _new_sketch("XY", "h"), _circle("h", 0, 0, 10),
        _cut("h", 25, "hole"),
    ]


def b_revolve_cut():
    """Make a flat puck, then revolve-cut a circular groove."""
    return _begin() + [
        _new_sketch("XY", "p"), _circle("p", 0, 0, 30),
        _extrude("p", 20, "puck"),
        _new_sketch("XZ", "g"),
        _polyline("g", [[10, 5], [12, 5], [12, 8], [10, 8]], closed=True),
        _revolve("g", axis="Z", angle_deg=360, operation="cut", alias="grv"),
    ]


def b_hole_wizard_drill():
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 80, 60),
        _extrude("b", 15, "plate"),
        {"kind": "holeWizard",
         "params": {"x": 0, "y": 0, "diameter": 8, "depth": 15,
                    "type": "drill", "alias": "hw1"}},
    ]


def b_hole_wizard_cbore():
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 80, 60),
        _extrude("b", 20, "plate"),
        {"kind": "holeWizard",
         "params": {"x": 0, "y": 0, "diameter": 8, "depth": 20,
                    "type": "cbore", "cbore_diameter": 14,
                    "cbore_depth": 5, "alias": "hw2"}},
    ]


# === FEATURE - MODIFY === #
def b_fillet_constant():
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 60, 40),
        _extrude("b", 30, "blk"),
        _fillet([], 5, "fil"),  # empty edges = all edges (addin convention)
    ]


def b_chamfer_distance():
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 50, 50),
        _extrude("b", 25, "blk"),
        {"kind": "fillet",
         "params": {"edges": [], "radius": 3, "type": "chamfer",
                    "alias": "ch"}},
    ]


def b_shell_outer_only():
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 80, 60),
        _extrude("b", 50, "box"),
        _shell(thickness=4),  # all faces - hollow with no opening
    ]


def b_shell_face_remove():
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 80, 60),
        _extrude("b", 50, "box"),
        _shell(thickness=4, remove=[[0, 0, 50]]),  # +Z face open
    ]


def b_rib_diagonal():
    return _begin() + [
        _new_sketch("XY", "v"), _rect("v", 0, 0, 100, 6),
        _extrude("v", 60, "wall"),
        _new_sketch("XZ", "r"),
        _polyline("r", [[-30, 0], [30, 30]], closed=False),
        {"kind": "rib",
         "params": {"sketch": "r", "thickness": 5,
                    "edge_type": 1, "thickness_side": 0,
                    "alias": "rb"}},
    ]


def b_draft_neutral_plane():
    """Tapered boss using draft."""
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 60, 60),
        _extrude("b", 5, "base"),
        _new_sketch("XY", "boss"), _rect("boss", 0, 0, 40, 40),
        _extrude("boss", 20, "boss", operation="join"),
        {"kind": "draft",
         "params": {"angle_deg": 5, "neutral_face": [0, 0, 5],
                    "draft_faces": [[20, 0, 15], [-20, 0, 15],
                                    [0, 20, 15], [0, -20, 15]],
                    "alias": "drf"}},
    ]


# === PATTERNS === #
def b_pattern_circular_6():
    """Disc + 1 hole + circular pattern x6 (validator should expand)."""
    return _begin() + [
        _new_sketch("XY", "d"), _circle("d", 0, 0, 50),
        _extrude("d", 8, "disc"),
        _new_sketch("XY", "h"), _circle("h", 35, 0, 4),
        _cut("h", 8, "h0"),
        _circular_pattern("h0", count=6, seed_x=35, seed_y=0, seed_r=4),
    ]


def b_pattern_circular_8_blades():
    """Disc + 1 small radial blade-shape + pattern x8."""
    return _begin() + [
        _new_sketch("XY", "d"), _circle("d", 0, 0, 30),
        _extrude("d", 5, "disc"),
        _new_sketch("XY", "b"),
        _polyline("b", [[20, -2], [45, -2], [45, 2], [20, 2]], closed=True),
        _extrude("b", 12, "bld0", operation="join"),
        # Cannot pattern an extrude with a non-circular seed via the helper -
        # use raw circular pattern; expect validator-layer expansion to
        # rewrite this into 7 explicit rotated-sketch extrudes.
        _circular_pattern("bld0", count=8, seed_x=32, seed_y=0, seed_r=2),
    ]


def b_pattern_linear_grid():
    """Plate with 2x2 grid of holes via 4 explicit cuts (kept small - SW
    feature tree gets very slow past ~10 sequential cuts on the same body)."""
    plan = _begin() + [
        _new_sketch("XY", "p"), _rect("p", 0, 0, 80, 60),
        _extrude("p", 10, "plate"),
    ]
    for ix in range(2):
        for iy in range(2):
            x = -25 + ix * 50
            y = -15 + iy * 30
            sk = f"h_{ix}_{iy}"
            plan.append(_new_sketch("XY", sk))
            plan.append(_circle(sk, x, y, 4))
            plan.append(_cut(sk, 10, f"cut_{ix}_{iy}"))
    return plan


# === MULTI-BODY === #
def b_multibody_two_separate():
    """Two cylinders that don't overlap - should produce 2 bodies."""
    return _begin() + [
        _new_sketch("XY", "a"), _circle("a", -30, 0, 12),
        _extrude("a", 20, "cylA"),
        _new_sketch("XY", "b"), _circle("b", 30, 0, 12),
        _extrude("b", 20, "cylB"),  # operation=new, separate body
    ]


def b_multibody_combine_subtract():
    """Big block - small block subtracted in place via cut."""
    return _begin() + [
        _new_sketch("XY", "a"), _rect("a", 0, 0, 80, 60),
        _extrude("a", 40, "main"),
        _new_sketch("XY", "h"), _rect("h", 0, 0, 30, 20),
        _cut("h", 40, "carve"),
    ]


# === SHEET METAL (emulated via thin extrudes per ledger) === #
def b_sm_emulated_lbracket():
    """L-bracket: horizontal flange + vertical flange, each 2mm thick."""
    return _begin() + [
        # Horizontal flange (footprint)
        _new_sketch("XY", "f1"), _rect("f1", 0, 0, 100, 40),
        _extrude("f1", 2, "flat"),
        # Vertical flange standing on the +Y edge
        _new_sketch("XZ", "f2"),
        _rect("f2", 0, 25, 100, 50),
        _extrude("f2", 2, "vert", operation="join", start_offset=20),
    ]


# === SURFACES === #
def b_surface_extrude():
    """Open profile extruded as a thin wall (no closure)."""
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[-30, 0], [-10, 20], [10, 20], [30, 0]],
                  closed=False),
        _extrude("s", 30, "wall"),  # thin wall - tests open-profile extrude
    ]


# === FILLET / CHAMFER WITH SPECIFIC EDGES === #
def b_fillet_top_only():
    """Box with fillet on the top 4 edges (cosmetic)."""
    return _begin() + [
        _new_sketch("XY", "b"), _rect("b", 0, 0, 60, 40),
        _extrude("b", 30, "blk"),
        _fillet([], 4, "fil_all"),
    ]


# === REVOLVE-BASED PARTS === #
def b_revolve_pulley():
    """V-belt pulley profile - rectangle with V-groove."""
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[10, 0], [30, 0], [30, 5], [25, 10],
                        [25, 18], [30, 23], [30, 28], [10, 28]],
                  closed=True),
        _revolve("s", axis="Y", angle_deg=360, alias="pulley"),
    ]


def b_revolve_funnel():
    """Funnel/cone via revolve of a tapered profile."""
    return _begin() + [
        _new_sketch("XY", "s"),
        _polyline("s", [[5, 0], [25, 30], [27, 30], [7, 0]], closed=True),
        _revolve("s", axis="Y", angle_deg=360, alias="fnl"),
    ]


# === COMBINED DRAWING TESTS === #
# These reuse a builder above and the runner appends drawing ops.
DRW_TARGETS = ["sketch_circle", "extrude_blind", "fillet_constant",
               "shell_face_remove", "pattern_circular_6", "revolve_full"]


# ---------------------------------------------------------------------------
# CATALOG
# ---------------------------------------------------------------------------
# expected.bbox_mm = (xlen, ylen, zlen) - precheck within +/-15%
# expected.body_count optional
# expected.watertight: True if part should be a closed solid
TESTS: list[dict] = [
    # ----- SKETCH primitives -----
    {"slug": "sketch_circle", "category": "sketch",
     "feature_keys": ["sketchCircle", "extrude_blind"],
     "build": b_sketch_circle,
     "goal": "20mm radius cylinder, 10mm tall",
     "spec": {"od_mm": 40, "height_mm": 10},
     "expected": {"bbox_mm": (40, 40, 10), "watertight": True}},

    {"slug": "sketch_rect", "category": "sketch",
     "feature_keys": ["sketchRect"],
     "build": b_sketch_rect,
     "goal": "60x40x15mm block",
     "spec": {"width_mm": 60, "depth_mm": 40, "height_mm": 15},
     "expected": {"bbox_mm": (60, 40, 15), "watertight": True}},

    {"slug": "sketch_polyline_triangle", "category": "sketch",
     "feature_keys": ["sketchPolyline_closed"],
     "build": b_sketch_polyline_triangle,
     "goal": "triangular prism 40mm base, 35mm height, 12mm thick",
     "spec": {"width_mm": 40, "height_mm": 35, "depth_mm": 12},
     "expected": {"bbox_mm": (40, 35, 12), "watertight": True}},

    {"slug": "sketch_polyline_pentagon", "category": "sketch",
     "feature_keys": ["sketchPolyline_closed"],
     "build": b_sketch_polyline_pentagon,
     "goal": "pentagonal prism, 25mm circumradius, 10mm tall",
     "spec": {"od_mm": 50, "height_mm": 10},
     "expected": {"bbox_mm": (50, 50, 10), "watertight": True}},

    {"slug": "sketch_polyline_hexagon", "category": "sketch",
     "feature_keys": ["sketchPolyline_closed"],
     "build": b_sketch_polyline_hexagon,
     "goal": "hexagonal prism, 30mm circumradius, 10mm tall",
     "spec": {"od_mm": 60, "height_mm": 10},
     "expected": {"bbox_mm": (60, 52, 10), "watertight": True}},

    {"slug": "sketch_spline_blob", "category": "sketch",
     "feature_keys": ["sketchSpline"],
     "build": b_sketch_spline_blob,
     "goal": "amorphous spline-based extrusion 8mm tall",
     "spec": {"height_mm": 8},
     "expected": {"watertight": True}},

    # ----- FEATURE - ADDITIVE -----
    {"slug": "extrude_blind", "category": "feat",
     "feature_keys": ["extrude_blind"],
     "build": b_extrude_blind,
     "goal": "30mm diameter cylinder, 25mm tall",
     "spec": {"od_mm": 30, "height_mm": 25},
     "expected": {"bbox_mm": (30, 30, 25), "watertight": True}},

    {"slug": "extrude_midplane", "category": "feat",
     "feature_keys": ["extrude_midplane"],
     "build": b_extrude_midplane,
     "goal": "50x30x40mm block centered on XY plane (-20..+20 in Z)",
     "spec": {"width_mm": 50, "depth_mm": 30, "height_mm": 40},
     "expected": {"bbox_mm": (50, 30, 40), "watertight": True}},

    {"slug": "revolve_full", "category": "feat",
     "feature_keys": ["revolve_360"],
     "build": b_revolve_full,
     "goal": "torus 60mm OD, 40mm bore, 10mm tall",
     "spec": {"od_mm": 60, "bore_mm": 40, "height_mm": 10},
     "expected": {"bbox_mm": (60, 10, 60), "watertight": True}},

    {"slug": "revolve_partial", "category": "feat",
     "feature_keys": ["revolve_180"],
     "build": b_revolve_partial,
     "goal": "half-revolved C-shaped ring",
     "spec": {"od_mm": 80},
     "expected": {"watertight": True}},

    {"slug": "helix_constant", "category": "feat",
     "feature_keys": ["helix_constant_pitch"],
     "build": b_helix_constant_pitch,
     "goal": "helical sweep path, pitch 5mm, 4 revolutions, on a 20mm circle",
     "spec": {"od_mm": 20, "n_revs": 4},
     "expected": {}},  # helix is a curve, no body

    {"slug": "loft_two_profiles", "category": "feat",
     "feature_keys": ["loft_two_profiles"],
     "build": b_loft_two_profiles,
     "goal": "loft frustum: 25mm radius circle on XY to 15mm radius circle "
              "on XY+50mm plane",
     "spec": {"od_mm": 50, "height_mm": 50},
     "expected": {"bbox_mm": (50, 50, 50), "watertight": True,
                   "body_count": 1}},

    # ----- FEATURE - SUBTRACTIVE -----
    {"slug": "extrude_cut", "category": "feat",
     "feature_keys": ["extrude_cut"],
     "build": b_extrude_cut,
     "goal": "60x60x20mm block with a 20mm dia through-hole in the center",
     "spec": {"width_mm": 60, "depth_mm": 60, "height_mm": 20, "bore_mm": 20},
     "expected": {"bbox_mm": (60, 60, 20), "watertight": True, "genus": 1}},

    {"slug": "revolve_cut", "category": "feat",
     "feature_keys": ["revolve_cut"],
     "build": b_revolve_cut,
     "goal": "30mm dia 20mm tall puck with a circumferential groove",
     "spec": {"od_mm": 60, "height_mm": 20},
     "expected": {"bbox_mm": (60, 60, 20), "watertight": True}},

    {"slug": "hole_wizard_drill", "category": "feat",
     "feature_keys": ["holeWizard_drill"],
     "build": b_hole_wizard_drill,
     "goal": "80x60x15mm plate with 8mm drilled through-hole at center",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 15, "bore_mm": 8},
     "expected": {"bbox_mm": (80, 60, 15), "watertight": True, "genus": 1}},

    {"slug": "hole_wizard_cbore", "category": "feat",
     "feature_keys": ["holeWizard_cbore"],
     "build": b_hole_wizard_cbore,
     "goal": "80x60x20mm plate, M8 cbore (14mm cbore, 5mm deep) through center",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 20, "bore_mm": 8},
     "expected": {"bbox_mm": (80, 60, 20), "watertight": True}},

    # ----- FEATURE - MODIFY -----
    {"slug": "fillet_constant", "category": "feat",
     "feature_keys": ["fillet_all_edges"],
     "build": b_fillet_constant,
     "goal": "60x40x30mm block with all edges rounded to 5mm radius",
     "spec": {"width_mm": 60, "depth_mm": 40, "height_mm": 30},
     "expected": {"bbox_mm": (60, 40, 30), "watertight": True}},

    {"slug": "chamfer_distance", "category": "feat",
     "feature_keys": ["chamfer_distance"],
     "build": b_chamfer_distance,
     "goal": "50x50x25mm block with all edges chamfered 3mm",
     "spec": {"width_mm": 50, "depth_mm": 50, "height_mm": 25},
     "expected": {"bbox_mm": (50, 50, 25), "watertight": True}},

    {"slug": "shell_outer_only", "category": "feat",
     "feature_keys": ["shell_no_remove"],
     "build": b_shell_outer_only,
     "goal": "80x60x50mm hollow box, 4mm wall, fully enclosed",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 50, "wall_mm": 4},
     "expected": {"bbox_mm": (80, 60, 50), "watertight": True,
                  "min_volume_ratio": 0.10, "max_volume_ratio": 0.55}},

    {"slug": "shell_face_remove", "category": "feat",
     "feature_keys": ["shell_face_remove"],
     "build": b_shell_face_remove,
     "goal": "80x60x50mm box hollowed with the top face removed, 4mm wall",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 50, "wall_mm": 4},
     "expected": {"bbox_mm": (80, 60, 50),
                  "min_volume_ratio": 0.10, "max_volume_ratio": 0.55}},

    {"slug": "rib_diagonal", "category": "feat",
     "feature_keys": ["rib_open_profile"],
     "build": b_rib_diagonal,
     "goal": "wall plate with diagonal reinforcement rib 5mm thick",
     "spec": {"thickness_mm": 5},
     "expected": {"watertight": True}},

    {"slug": "draft_neutral", "category": "feat",
     "feature_keys": ["draft_neutral_plane"],
     "build": b_draft_neutral_plane,
     "goal": "molded boss base 60x60x5mm with 40x40x20mm tapered boss on top",
     "spec": {"width_mm": 60, "depth_mm": 60, "height_mm": 25},
     "expected": {"bbox_mm": (60, 60, 25), "watertight": True}},

    # ----- PATTERNS -----
    {"slug": "pattern_circular_6", "category": "pattern",
     "feature_keys": ["circularPattern_via_validator"],
     "build": b_pattern_circular_6,
     "goal": "100mm disc with 6 bolt holes equally spaced on PCD 70mm",
     "spec": {"od_mm": 100, "height_mm": 8, "n_bolts": 6},
     "expected": {"bbox_mm": (100, 100, 8), "watertight": True, "genus": 6}},

    {"slug": "pattern_circular_8", "category": "pattern",
     "feature_keys": ["circularPattern_blades_via_validator"],
     "build": b_pattern_circular_8_blades,
     "goal": "30mm hub with 8 radial blades extending to 45mm OD",
     "spec": {"od_mm": 90, "height_mm": 12, "n_blades": 8},
     "expected": {"bbox_mm": (90, 90, 12), "watertight": True}},

    {"slug": "pattern_linear_grid", "category": "pattern",
     "feature_keys": ["linear_pattern_explicit_cuts"],
     "build": b_pattern_linear_grid,
     "goal": "80x60x10mm plate with 2x2 grid of 4mm holes",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 10},
     "expected": {"bbox_mm": (80, 60, 10), "watertight": True, "genus": 4}},

    # ----- MULTI-BODY -----
    {"slug": "multibody_two_cyl", "category": "multibody",
     "feature_keys": ["multibody_independent_extrude"],
     "build": b_multibody_two_separate,
     "goal": "two separate 24mm cylinders 60mm apart, 20mm tall each",
     "spec": {"od_mm": 24, "height_mm": 20},
     "expected": {"watertight": True, "body_count": 2}},

    {"slug": "multibody_subtract", "category": "multibody",
     "feature_keys": ["combine_subtract_via_cut"],
     "build": b_multibody_combine_subtract,
     "goal": "80x60x40mm block with 30x20mm rectangular pocket through center",
     "spec": {"width_mm": 80, "depth_mm": 60, "height_mm": 40},
     "expected": {"bbox_mm": (80, 60, 40), "watertight": True, "genus": 1}},

    # ----- SHEET METAL (emulated) -----
    {"slug": "sm_lbracket_emulated", "category": "sm",
     "feature_keys": ["sheet_metal_emulated_extrude"],
     "build": b_sm_emulated_lbracket,
     "goal": "L-shaped bracket, 100x40mm flat, 100x50mm vertical, 2mm thick",
     "spec": {"width_mm": 100, "depth_mm": 40, "height_mm": 50,
              "thickness_mm": 2},
     "expected": {"watertight": True}},

    # ----- SURFACES (open extrude) -----
    {"slug": "surface_open_wall", "category": "surf",
     "feature_keys": ["extrude_open_profile"],
     "build": b_surface_extrude,
     "goal": "thin curved wall extruded from open polyline",
     "spec": {},
     "expected": {}},  # open extrude may produce thin solid or surface

    # ----- REVOLVE PARTS -----
    {"slug": "revolve_pulley", "category": "feat",
     "feature_keys": ["revolve_complex_profile"],
     "build": b_revolve_pulley,
     "goal": "V-belt pulley, 60mm OD, 28mm wide, V-groove at center",
     "spec": {"od_mm": 60, "height_mm": 28},
     "expected": {"bbox_mm": (60, 28, 60), "watertight": True}},

    {"slug": "revolve_funnel", "category": "feat",
     "feature_keys": ["revolve_tapered_profile"],
     "build": b_revolve_funnel,
     "goal": "funnel cone, 50mm dia top, 10mm dia bottom, 30mm tall",
     "spec": {"od_mm": 54, "height_mm": 30},
     "expected": {"watertight": True}},
]
