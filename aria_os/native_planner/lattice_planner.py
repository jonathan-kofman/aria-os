"""Editable-lattice plan — emits an L-bracket / housing / etc. host body
plus a `latticeFeature` op that re-bakes the SDF on SW user-parameter
changes.

Why dedicated: the LLM planner via `latticeFeature` works for arbitrary
prompts, but a hardcoded path produces dimensionally-stable plans for
the common case ("L-bracket with gyroid infill", "housing with octet
truss core"). Mirrors flange_planner / shaft_planner shape.
"""
from __future__ import annotations

import re
from typing import Optional

from .shaft_planner import _extract_diameters  # reuse: pulls Ø values


_PATTERN_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("gyroid",),                         "gyroid"),
    (("schwarz-p", "schwarz p", "schwarzp"), "schwarz_p"),
    (("schwarz-w", "schwarz w"),          "schwarz_w"),
    (("diamond tpms", "diamond lattice"), "diamond"),
    (("iwp",),                            "iwp"),
    (("neovius",),                        "neovius"),
    (("octet truss", "octet-truss", "octet"), "octet_truss"),
    (("bcc lattice", "bcc"),              "bcc"),
    (("fcc lattice", "fcc"),              "fcc"),
    (("kagome",),                         "kagome"),
    (("honeycomb",),                      "honeycomb"),
    (("tpms",),                           "gyroid"),  # generic TPMS → gyroid
    (("infill", "lattice"),               "gyroid"),  # generic → gyroid
]


def _detect_pattern(goal: str) -> str:
    g = (goal or "").lower()
    for keywords, pat in _PATTERN_KEYWORDS:
        if any(k in g for k in keywords):
            return pat
    return "gyroid"


def _detect_cell_mm(goal: str) -> float:
    """Pull the cell size from phrases like '5mm cell', '8mm gyroid',
    'cell size 6mm'. Falls back to 8mm — the printable default for
    consumer FDM."""
    g = (goal or "")
    patterns = [
        r"(\d+(?:\.\d+)?)\s*mm\s*cell",
        r"cell\s*(?:size)?\s*[:=]?\s*(\d+(?:\.\d+)?)\s*mm",
        r"(\d+(?:\.\d+)?)\s*mm\s*(?:gyroid|lattice|tpms|infill)",
    ]
    for pat in patterns:
        m = re.search(pat, g, re.IGNORECASE)
        if m:
            try:
                v = float(m.group(1))
                if 1.0 <= v <= 50.0:
                    return v
            except (TypeError, ValueError):
                pass
    return 8.0


def _detect_wall_mm(goal: str) -> float:
    g = (goal or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*wall", g, re.IGNORECASE)
    if m:
        try:
            v = float(m.group(1))
            if 0.2 <= v <= 10.0:
                return v
        except (TypeError, ValueError):
            pass
    return 1.0


def _host_dims(spec: dict, goal: str) -> tuple[float, float, float]:
    """Pick a sensible host-body bounding box from spec + goal text.
    Used for both the host extrude and the lattice bbox."""
    width = float(spec.get("width_mm",
                            spec.get("od_mm",
                            spec.get("length_mm", 60.0))))
    height = float(spec.get("height_mm",
                              spec.get("thickness_mm", 40.0)))
    depth = float(spec.get("depth_mm",
                             spec.get("length_mm", 40.0)))
    # If the goal lists 3 numbers like "60x40x30", parse and use those.
    m = re.search(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)"
                    r"\s*[xX×]\s*(\d+(?:\.\d+)?)\s*mm?", goal or "")
    if m:
        try:
            width = float(m.group(1))
            height = float(m.group(2))
            depth = float(m.group(3))
        except (TypeError, ValueError):
            pass
    return width, height, depth


def plan_lattice(spec: dict, goal: str = "") -> list[dict]:
    """Emit an editable-lattice plan: host body + latticeFeature op.

    The output uses the existing extrude flow for the host shell and
    appends a single `latticeFeature` op that the SW addin handles by
    baking an STL via aria_os.sdf.lattice_op.bake() and importing as
    a Mesh BREP body. SW user parameters (lattice_pattern, lattice_cell,
    lattice_wall) are recorded so changes trigger a regen.
    """
    pattern = _detect_pattern(goal)
    cell_mm = _detect_cell_mm(goal)
    wall_mm = _detect_wall_mm(goal)
    width, height, depth = _host_dims(spec, goal)

    # bbox in HOST-local coords (centered on extrude). Z runs along the
    # extrusion axis; depth = extrude distance, so bbox z = (0, depth).
    bbox = [-width / 2, -height / 2, 0.0,
             width / 2, height / 2, depth]

    plan: list[dict] = [
        {"kind": "beginPlan", "params": {},
         "label": "Reset feature registry"},
        # User parameters — these are what the SW user edits. Changes
        # trigger the regen hook (see SW addin OpLatticeFeature).
        {"kind": "addParameter",
         "params": {"name": "lattice_cell_mm",
                    "value_mm": cell_mm,
                    "comment": "Lattice cell size — edit & rebuild to "
                               "re-bake"},
         "label": f"User Parameter: lattice_cell_mm = {cell_mm:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "lattice_wall_mm",
                    "value_mm": wall_mm,
                    "comment": "Lattice wall thickness — edit & rebuild"},
         "label": f"User Parameter: lattice_wall_mm = {wall_mm:g}mm"},
        # Host body — simple extruded rectangle. The lattice intersects
        # this body at bake time so the user can shape the outer
        # geometry independently of the lattice pattern.
        {"kind": "newSketch",
         "params": {"plane": "XY", "alias": "sk_host",
                    "name": "ARIA Lattice Host"},
         "label": "Sketch on XY plane"},
        {"kind": "sketchRect",
         "params": {"sketch": "sk_host", "cx": 0, "cy": 0,
                    "w": width, "h": height},
         "label": f"Host rectangle {width:g}×{height:g}mm"},
        {"kind": "extrude",
         "params": {"sketch": "sk_host", "distance": depth,
                    "operation": "new", "alias": "host_body"},
         "label": f"Extrude host body {depth:g}mm"},
        # Lattice — references the host body alias so the SW addin can
        # pull the host's bounding box at bake time. The cell/wall
        # values are baked into the recipe key for cache hits.
        {"kind": "latticeFeature",
         "params": {
             "target":    "host_body",
             "pattern":   pattern,
             "cell_mm":   cell_mm,
             "wall_mm":   wall_mm,
             "operation": "intersect",
             "alias":     "lattice_body",
             "bbox":      bbox,
             "param_links": {
                 "cell_mm": "lattice_cell_mm",
                 "wall_mm": "lattice_wall_mm",
             },
         },
         "label": (f"Lattice {pattern} cell={cell_mm:g}mm "
                   f"wall={wall_mm:g}mm (editable)")},
    ]
    return plan


if __name__ == "__main__":
    from .validator import validate_plan
    test_goals = [
        "L-bracket 80x60x40mm with 5mm gyroid infill",
        "housing 60x60x60mm with octet truss core, 4mm cell, 1mm wall",
        "60x40x30mm bracket with schwarz-p lattice",
    ]
    for g in test_goals:
        plan = plan_lattice({}, goal=g)
        ok, issues = validate_plan(plan)
        marker = "OK" if ok else "FAIL"
        print(f"  [{marker}] ({len(plan)} ops) {g}")
        if not ok:
            for i in issues[:3]:
                print(f"    ! {i}")
