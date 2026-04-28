"""Stepped-shaft plan — emits feature ops for a multi-diameter shaft.

Output shape: standard `[{kind, params, label}, ...]`. The dispatcher
streams these through the bridge so each lands as a real feature in the
host CAD's timeline. Mirrors flange_planner.py / impeller_planner.py.

Geometry strategy: we build the shaft as a stack of extruded cylinders
along Z. Single-diameter requests (just `diameter_mm` + `length_mm`)
collapse to one extrude. "Stepped" / multi-diameter requests parse the
goal text for diameter callouts ("12mm dia ends, 20mm dia center") and
emit one extrude per segment, each joined to the previous.

Optional features:
  - keyway: rectangular pocket on the central segment, depth 0.4 *
    largest_dia, width inferred from the largest diameter (DIN 6885
    nominal — 8mm key for Ø22-30 shafts, 6mm for Ø17-22, etc.)
"""
from __future__ import annotations

import re
from typing import Optional


# DIN 6885 standard rectangular key widths (key_mm) for shaft diameter
# bands (low, high). Used when the goal asks for a keyway but doesn't
# specify a width.
_DIN_6885: list[tuple[float, float, float]] = [
    # (shaft_dia_low_mm, shaft_dia_high_mm, key_width_mm)
    ( 6.0,  8.0, 2.0),
    ( 8.0, 10.0, 3.0),
    (10.0, 12.0, 4.0),
    (12.0, 17.0, 5.0),
    (17.0, 22.0, 6.0),
    (22.0, 30.0, 8.0),
    (30.0, 38.0,10.0),
    (38.0, 44.0,12.0),
    (44.0, 50.0,14.0),
    (50.0, 58.0,16.0),
    (58.0, 65.0,18.0),
    (65.0, 75.0,20.0),
]


def _key_width_for(dia_mm: float) -> float:
    for low, high, w in _DIN_6885:
        if low <= dia_mm < high:
            return w
    # > 75mm: scale linearly past the top of the table
    return max(20.0, dia_mm * 0.27)


# Capture "20mm dia", "Ø20", "20mm OD", "12 mm diameter", "20mm diameter ends"
_DIA_PATTERNS = [
    re.compile(r"(?P<v>\d+(?:\.\d+)?)\s*(?:mm)?\s*(?:dia|diameter|Ø|OD)",
               re.IGNORECASE),
    re.compile(r"Ø\s*(?P<v>\d+(?:\.\d+)?)", re.IGNORECASE),
]


def _extract_diameters(goal: str) -> list[float]:
    """Pull every diameter callout out of the goal text in order. Used
    to figure out a stepped-shaft cross-section when the spec dict only
    carries the dominant diameter."""
    if not goal:
        return []
    seen: list[float] = []
    for pat in _DIA_PATTERNS:
        for m in pat.finditer(goal):
            try:
                v = float(m.group("v"))
            except (TypeError, ValueError):
                continue
            if v > 0 and v < 1000:
                seen.append(v)
    return seen


def _segments_from_goal(goal: str, default_dia: float, total_len: float
                          ) -> list[tuple[float, float]]:
    """Return [(diameter_mm, segment_length_mm), ...] summing to
    total_len. Heuristic — good enough for the common 'stepped shaft
    Lmm long, Dc dia center, De dia ends' phrasing."""
    g = (goal or "").lower()
    dias = _extract_diameters(goal)
    # Filter to plausible shaft diameters (drop 200 / 100 which are
    # usually lengths). Heuristic: shaft diameters are <80mm in the
    # vast majority of mechanical designs we see; lengths are >60mm.
    # When a value appears as both "dia" and just a number it would
    # already be in dias via the regex.
    if not dias:
        return [(default_dia, total_len)]

    has_ends = "end" in g
    has_center = ("center" in g or "centre" in g or "middle" in g)
    has_step = ("step" in g or "shoulder" in g)
    if has_ends and (has_center or has_step) and len(dias) >= 2:
        # Pick the dominant central + end diameters. If the goal lists
        # them in order (centre first, ends next or vice-versa) we
        # default to: largest = centre, smaller = ends.
        sorted_d = sorted(set(dias))
        end_d = sorted_d[0]
        center_d = sorted_d[-1]
        # Allocate length: ends 20% each, center 60%. This is a sane
        # default for shafts where the user didn't dimension the steps.
        end_l = total_len * 0.20
        ctr_l = total_len - 2 * end_l
        return [(end_d, end_l), (center_d, ctr_l), (end_d, end_l)]
    if has_step and len(dias) >= 2:
        # Two-segment shoulder: small first, large second.
        sorted_d = sorted(set(dias))
        small, large = sorted_d[0], sorted_d[-1]
        return [(small, total_len * 0.5), (large, total_len * 0.5)]
    # Single-diameter
    return [(dias[0] if dias else default_dia, total_len)]


def plan_shaft(spec: dict, goal: str = "") -> list[dict]:
    length   = float(spec.get("length_mm",
                                spec.get("height_mm", 100.0)))
    main_dia = float(spec.get("diameter_mm",
                                spec.get("od_mm",
                                spec.get("outer_dia_mm", 20.0))))

    segments = _segments_from_goal(goal, main_dia, length)
    largest_dia = max(d for d, _ in segments)

    # Optional keyway: parse goal for "keyway" / "key seat".
    g = (goal or "").lower()
    has_keyway = ("keyway" in g or "key seat" in g or "keyseat" in g
                   or "key slot" in g)
    key_w = _key_width_for(largest_dia)
    key_depth = round(largest_dia * 0.20, 2)   # ~half the key height
    # Keyway length: 50% of central segment if multi-seg, else 30% of total.
    if len(segments) >= 3:
        cent_len = segments[len(segments) // 2][1]
        key_len = cent_len * 0.6
        # Z-start at the leading face of the central segment + 20% in.
        z_lead = sum(seg[1] for seg in segments[:len(segments) // 2])
        key_z0 = z_lead + (cent_len - key_len) / 2
    else:
        key_len = length * 0.3
        key_z0 = (length - key_len) / 2

    plan: list[dict] = [
        {"kind": "beginPlan", "params": {},
         "label": "Reset feature registry"},
        {"kind": "addParameter",
         "params": {"name": "shaft_length", "value_mm": length,
                    "comment": "Overall shaft length"},
         "label": f"User Parameter: shaft_length = {length:g}mm"},
        {"kind": "addParameter",
         "params": {"name": "shaft_main_dia", "value_mm": largest_dia,
                    "comment": "Largest shaft diameter"},
         "label": f"User Parameter: shaft_main_dia = {largest_dia:g}mm"},
    ]

    # --- Body: stack of cylinders along Z ----------------------------
    z_cursor = 0.0
    for i, (d, l) in enumerate(segments):
        seg_id = f"seg_{i}"
        plan += [
            {"kind": "newSketch",
             "params": {"plane": "XY", "alias": f"sketch_{seg_id}",
                        "name": f"ARIA Shaft Segment {i+1}"},
             "label": f"Sketch on XY (segment {i+1})"},
            {"kind": "sketchCircle",
             "params": {"sketch": f"sketch_{seg_id}",
                         "cx": 0, "cy": 0, "r": d / 2.0},
             "label": f"Circle Ø{d:g}mm"},
            # Use offset so subsequent segments stack along Z. Most
            # bridges accept `start_offset` — those that don't will
            # just stack from origin which still produces a usable
            # geometry for visual confirmation.
            {"kind": "extrude",
             "params": {"sketch": f"sketch_{seg_id}", "distance": l,
                        "start_offset": z_cursor,
                        "operation": "new" if i == 0 else "join",
                        "alias": f"body_{seg_id}"},
             "label": (f"Extrude segment {i+1}: Ø{d:g}mm × {l:g}mm "
                       f"@ z={z_cursor:g}mm "
                       f"({'new' if i == 0 else 'join'})")},
        ]
        z_cursor += l

    # --- Optional keyway -------------------------------------------------
    if has_keyway:
        plan += [
            {"kind": "newSketch",
             "params": {"plane": "XZ", "alias": "sketch_keyway",
                        "name": "ARIA Keyway"},
             "label": "Sketch on XZ plane (keyway)"},
            {"kind": "sketchRect",
             "params": {"sketch": "sketch_keyway",
                         # Center on shaft axis, length along Z, width
                         # straddling X. We parameterise on key_w/key_len.
                         "cx": 0, "cy": key_z0 + key_len / 2,
                         "w": key_w, "h": key_len},
             "label": (f"Keyway rectangle {key_w:g}×{key_len:g}mm "
                       f"(DIN 6885 nominal)")},
            {"kind": "extrude",
             "params": {"sketch": "sketch_keyway",
                         "distance": key_depth,
                         "operation": "cut", "alias": "cut_keyway"},
             "label": f"Cut keyway {key_depth:g}mm deep"},
        ]
    return plan
