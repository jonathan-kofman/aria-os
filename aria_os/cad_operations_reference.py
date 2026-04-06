"""CadQuery operations reference — injected into LLM prompts for geometry generation.

Every operation is a tested, working CadQuery snippet. The LLM sees these as
available building blocks and can combine them freely.
"""

# Each entry: (operation_name, description, code_snippet)
# Code snippets are TESTED — they run and produce valid geometry.

OPERATIONS_REFERENCE: list[tuple[str, str, str]] = [
    # ── Basic shapes ──────────────────────────────────────────────────────
    ("box", "Rectangular solid",
     'cq.Workplane("XY").box(WIDTH, HEIGHT, THICKNESS)'),

    ("cylinder", "Circular disc or tall cylinder",
     'cq.Workplane("XY").circle(RADIUS).extrude(HEIGHT)'),

    ("tube", "Hollow cylinder (OD - bore)",
     'cq.Workplane("XY").circle(OD/2).circle(BORE/2).extrude(HEIGHT)'),

    # ── Cuts & holes ─────────────────────────────────────────────────────
    ("through_hole", "Simple through-hole on top face",
     'result.faces(">Z").workplane().circle(DIA/2).cutThruAll()'),

    ("counterbore", "Counterbore hole (bolt recess)",
     'result.faces(">Z").workplane().cboreHole(HOLE_DIA, CBORE_DIA, CBORE_DEPTH)'),

    ("countersink", "Countersink hole (screw flush)",
     'result.faces(">Z").workplane().cskHole(HOLE_DIA, CSK_DIA, CSK_ANGLE)'),

    ("pocket", "Rectangular pocket cut into top face",
     'result.faces(">Z").workplane().rect(PW, PH).cutBlind(-POCKET_DEPTH)'),

    ("slot", "Through-slot",
     'result.faces(">Z").workplane().slot2D(LENGTH, WIDTH).cutThruAll()'),

    # ── Bolt patterns ────────────────────────────────────────────────────
    ("circular_bolt_pattern", "N holes evenly spaced on a circle",
     """import math
pts = [(R * math.cos(i * 2*math.pi/N), R * math.sin(i * 2*math.pi/N)) for i in range(N)]
result = result.faces(">Z").workplane().pushPoints(pts).circle(DIA/2).cutThruAll()"""),

    ("rectangular_bolt_pattern", "4 holes at rectangle corners",
     """pts = [(-DX, -DY), (DX, -DY), (DX, DY), (-DX, DY)]
result = result.faces(">Z").workplane().pushPoints(pts).circle(DIA/2).cutThruAll()"""),

    # ── Shell (hollow parts) ─────────────────────────────────────────────
    ("shell", "Hollow out a solid, keeping wall thickness",
     'result = result.shell(WALL_THICKNESS)  # positive = outward, negative = inward'),

    ("shell_open_top", "Shell with open top face",
     'result = result.faces(">Z").shell(-WALL_THICKNESS)  # removes top, keeps walls'),

    # ── Revolve (axisymmetric parts) ─────────────────────────────────────
    ("revolve_profile", "Revolve a 2D profile around an axis (nozzles, cups, bowls)",
     """# Define profile as list of (x, y) points — x is radial distance, y is axial
profile_pts = [(0, 0), (R_BASE, 0), (R_BASE, H1), (R_TOP, H2), (0, H2)]
result = cq.Workplane("XZ").polyline(profile_pts).close().revolve(360, (0,0,0), (0,1,0))"""),

    ("revolve_hollow", "Revolve a hollow profile (tubes, vases)",
     """outer = [(INNER_R, 0), (OUTER_R, 0), (OUTER_R, HEIGHT), (INNER_R, HEIGHT)]
result = cq.Workplane("XZ").polyline(outer).close().revolve(360, (0,0,0), (0,1,0))"""),

    # ── Sweep (pipes, channels, rails) ───────────────────────────────────
    ("sweep_circle", "Sweep a circle along a path (pipe/tube routing)",
     """path = cq.Workplane("XZ").spline([(0,0), (L/3, H), (2*L/3, -H), (L, 0)])
result = cq.Workplane("XY").circle(PIPE_R).sweep(path)"""),

    ("sweep_rect", "Sweep a rectangle along a path (rail/channel)",
     """path = cq.Workplane("XZ").spline([(0,0), (L/2, H), (L, 0)])
result = cq.Workplane("XY").rect(W, T).sweep(path)"""),

    # ── Loft (transition between profiles) ───────────────────────────────
    ("loft_rect_to_circle", "Transition from rectangle to circle",
     """result = (cq.Workplane("XY").rect(W1, H1)
    .workplane(offset=LOFT_HEIGHT).circle(R2)
    .loft())"""),

    ("loft_two_rects", "Taper from large to small rectangle",
     """result = (cq.Workplane("XY").rect(W1, H1)
    .workplane(offset=HEIGHT).rect(W2, H2)
    .loft())"""),

    # ── Fillets & chamfers ───────────────────────────────────────────────
    ("fillet_vertical", "Round vertical edges",
     'result = result.edges("|Z").fillet(RADIUS)'),

    ("fillet_top", "Round top edges only",
     'result = result.edges(">Z").fillet(RADIUS)'),

    ("chamfer_edges", "Chamfer selected edges",
     'result = result.edges(">Z").chamfer(SIZE)'),

    # ── Bosses & ribs ────────────────────────────────────────────────────
    ("boss", "Cylindrical boss protruding from a face",
     """result = (result.faces(">Z").workplane()
    .circle(BOSS_OD/2).circle(BOSS_ID/2).extrude(BOSS_HEIGHT))"""),

    ("rib", "Structural rib on a face",
     """result = (result.faces(">Z").workplane()
    .rect(RIB_LENGTH, RIB_THICKNESS).extrude(RIB_HEIGHT))"""),

    # ── Text & engraving ─────────────────────────────────────────────────
    ("engrave_text", "Engrave text into top face",
     'result = result.faces(">Z").workplane().text("LABEL", FONT_SIZE, -DEPTH)'),

    # ── Mirror & pattern ─────────────────────────────────────────────────
    ("mirror", "Mirror body across a plane",
     'result = result.mirror("YZ")  # or "XZ", "XY"'),

    ("linear_pattern", "Repeat a feature in a line",
     """pts = [(i * SPACING, 0) for i in range(COUNT)]
result = result.faces(">Z").workplane().pushPoints(pts).circle(R).cutThruAll()"""),

    # ── Compound / multi-body ────────────────────────────────────────────
    ("union", "Join two solids",
     'result = body_a.union(body_b)'),

    ("cut", "Subtract one solid from another",
     'result = body_a.cut(body_b)'),

    ("intersect", "Keep only the overlap of two solids",
     'result = body_a.intersect(body_b)'),

    # ── Common part patterns ─────────────────────────────────────────
    ("gopro_3prong", "GoPro-style 3-prong mount (2 outer prongs + 1 inner)",
     """# GoPro mount: 2 outer tabs + 1 center tab with through-hole
PRONG_W = 3.0   # width of each prong
GAP = 3.5       # gap between prongs
PRONG_H = 10.0  # height of prongs
HOLE_D = 5.0    # bolt hole diameter

base = cq.Workplane("XY").box(BASE_W, BASE_D, BASE_H)
# Outer prongs (2)
for offset in [-(GAP/2 + PRONG_W/2), (GAP/2 + PRONG_W/2)]:
    prong = (cq.Workplane("XY").workplane(offset=BASE_H/2)
        .center(0, offset).box(PRONG_W, PRONG_W, PRONG_H).translate((0, 0, PRONG_H/2)))
    base = base.union(prong)
# Center prong (1)
center = (cq.Workplane("XY").workplane(offset=BASE_H/2)
    .box(PRONG_W, PRONG_W, PRONG_H).translate((0, 0, PRONG_H/2)))
base = base.union(center)
# Through-hole for bolt
result = base.faces(">Z").workplane().circle(HOLE_D/2).cutThruAll()"""),

    ("heat_sink_fins", "Parallel fin array on a base plate",
     """# Heat sink: base plate + N parallel fins
base = cq.Workplane("XY").box(W, D, BASE_T)
for i in range(N_FINS):
    x = -W/2 + FIN_T/2 + i * SPACING
    fin = (cq.Workplane("XY").workplane(offset=BASE_T/2)
        .center(x, 0).box(FIN_T, D, FIN_H).translate((0, 0, FIN_H/2)))
    base = base.union(fin)
result = base"""),

    ("l_bracket", "L-shaped bracket (two plates at 90 degrees)",
     """# L-bracket: horizontal base + vertical leg
base = cq.Workplane("XY").box(W, DEPTH, THICKNESS)
vert = (cq.Workplane("XY").workplane(offset=THICKNESS/2)
    .center(0, -DEPTH/2 + THICKNESS/2)
    .box(W, THICKNESS, LEG_H).translate((0, 0, LEG_H/2)))
result = base.union(vert)"""),
]


def get_operations_prompt() -> str:
    """Build the operations reference section for the LLM system prompt."""
    lines = [
        "## AVAILABLE CADQUERY OPERATIONS",
        "You can use ANY of these operations. Combine them freely.",
        "Each snippet is tested and produces valid geometry.\n",
    ]
    for name, desc, code in OPERATIONS_REFERENCE:
        lines.append(f"### {name} — {desc}")
        lines.append(f"```python\n{code}\n```\n")
    return "\n".join(lines)


def get_operations_for_goal(goal: str) -> str:
    """Return only the operations relevant to a goal description."""
    goal_lower = goal.lower()

    # Always include basics
    relevant = {"box", "cylinder", "through_hole", "circular_bolt_pattern", "union", "cut"}

    # Keyword → operation mapping
    if any(w in goal_lower for w in ("hollow", "shell", "thin wall", "case", "enclosure")):
        relevant.update({"shell", "shell_open_top"})
    if any(w in goal_lower for w in ("revolve", "nozzle", "cup", "bowl", "bell", "axisymmetric", "vase")):
        relevant.update({"revolve_profile", "revolve_hollow"})
    if any(w in goal_lower for w in ("sweep", "pipe", "tube", "channel", "rail", "curved")):
        relevant.update({"sweep_circle", "sweep_rect"})
    if any(w in goal_lower for w in ("loft", "transition", "taper", "funnel", "adapter")):
        relevant.update({"loft_rect_to_circle", "loft_two_rects"})
    if any(w in goal_lower for w in ("fillet", "round", "smooth")):
        relevant.update({"fillet_vertical", "fillet_top"})
    if any(w in goal_lower for w in ("chamfer", "bevel")):
        relevant.add("chamfer_edges")
    if any(w in goal_lower for w in ("bolt", "hole", "mount")):
        relevant.update({"through_hole", "counterbore", "countersink",
                         "circular_bolt_pattern", "rectangular_bolt_pattern"})
    if any(w in goal_lower for w in ("boss", "standoff", "pillar")):
        relevant.add("boss")
    if any(w in goal_lower for w in ("rib", "stiffener", "reinforcement")):
        relevant.add("rib")
    if any(w in goal_lower for w in ("pocket", "recess", "cavity")):
        relevant.add("pocket")
    if any(w in goal_lower for w in ("slot", "groove", "keyway")):
        relevant.add("slot")
    if any(w in goal_lower for w in ("text", "engrav", "label", "mark")):
        relevant.add("engrave_text")
    if any(w in goal_lower for w in ("mirror", "symmetric")):
        relevant.add("mirror")
    if any(w in goal_lower for w in ("pattern", "array", "repeat")):
        relevant.add("linear_pattern")
    if any(w in goal_lower for w in ("tube", "pipe", "hollow cylinder")):
        relevant.add("tube")

    lines = [
        "## CADQUERY OPERATIONS (use these — all tested and working)\n",
    ]
    for name, desc, code in OPERATIONS_REFERENCE:
        if name in relevant:
            lines.append(f"**{name}** — {desc}")
            lines.append(f"```python\n{code}\n```\n")
    return "\n".join(lines)
