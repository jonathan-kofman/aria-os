"""Circular-pattern helper — emits N explicit cut-extrudes at rotated
positions instead of using the `circularPattern` op.

Background: SW2024's COM IDispatch silently rejects FeatureCircularPattern5
even with selection state correct (verified via probe / addin.log). The
software fallback in OpCircularPattern works for some cuts but not all
when batched through a single sketch. The architectural fix is to skip
the pattern op entirely on planner side and emit each rotated cut as a
discrete sketch+cut sequence — extrude-cut works flawlessly across all
SW versions.

Trade-off vs native circularPattern:
  + Works on every SW build (and Rhino/Fusion/Onshape via the same op
    contract — no per-CAD branching needed).
  + Each cut is independently inspectable in the feature tree, easier
    to debug visually.
  - Larger feature tree (N entries instead of 1 pattern node).
  - Edits to count/radius require regenerating the plan; native
    pattern features could parametrically update.

The trade-off favors reliability over edit ergonomics — and our pipeline
regenerates plans from spec on every change anyway, so the parametric
update advantage of the native pattern wasn't being used.
"""
from __future__ import annotations

import math


def emit_circular_cuts(
    count: int,
    radius_mm: float,
    hole_dia_mm: float,
    cut_dist_mm: float,
    plane: str = "XY",
    phase_deg: float = 0.0,
    alias_prefix: str = "cp",
    label_prefix: str = "Hole",
) -> list[dict]:
    """Generate `count` discrete cut-extrudes at rotated positions.

    Each iteration emits 3 ops: newSketch + sketchCircle + extrude(cut).
    Aliases are prefixed so the planner can reference them downstream
    (e.g. for downstream patterning, mating, or fillet selection).
    """
    plan: list[dict] = []
    for i in range(count):
        theta = math.radians(phase_deg) + 2 * math.pi * i / count
        cx = radius_mm * math.cos(theta)
        cy = radius_mm * math.sin(theta)
        sk_alias = f"sk_{alias_prefix}_{i}"
        cut_alias = f"{alias_prefix}_{i}"
        plan.extend([
            {"kind": "newSketch",
             "params": {"plane": plane, "alias": sk_alias,
                        "name": f"ARIA {label_prefix} {i+1}"},
             "label": f"Sketch for {label_prefix.lower()} {i+1}/{count}"},
            {"kind": "sketchCircle",
             "params": {"sketch": sk_alias, "cx": cx, "cy": cy,
                         "r": hole_dia_mm / 2.0},
             "label": f"{label_prefix} {i+1}/{count} at "
                       f"({cx:+.1f}, {cy:+.1f})mm"},
            {"kind": "extrude",
             "params": {"sketch": sk_alias, "distance": cut_dist_mm,
                         "operation": "cut", "alias": cut_alias},
             "label": f"Cut {label_prefix.lower()} {i+1}/{count}"},
        ])
    return plan


def emit_circular_bosses(
    count: int,
    radius_mm: float,
    feature_dia_mm: float,
    height_mm: float,
    plane: str = "XY",
    phase_deg: float = 0.0,
    alias_prefix: str = "boss",
    label_prefix: str = "Boss",
    operation: str = "join",
) -> list[dict]:
    """Boss/protrusion variant — same idea, but joins material instead of
    cutting. Used by gear teeth, fan blade hubs, etc."""
    plan: list[dict] = []
    for i in range(count):
        theta = math.radians(phase_deg) + 2 * math.pi * i / count
        cx = radius_mm * math.cos(theta)
        cy = radius_mm * math.sin(theta)
        sk_alias = f"sk_{alias_prefix}_{i}"
        boss_alias = f"{alias_prefix}_{i}"
        plan.extend([
            {"kind": "newSketch",
             "params": {"plane": plane, "alias": sk_alias,
                        "name": f"ARIA {label_prefix} {i+1}"},
             "label": f"Sketch for {label_prefix.lower()} {i+1}/{count}"},
            {"kind": "sketchCircle",
             "params": {"sketch": sk_alias, "cx": cx, "cy": cy,
                         "r": feature_dia_mm / 2.0},
             "label": f"{label_prefix} {i+1}/{count} at "
                       f"({cx:+.1f}, {cy:+.1f})mm"},
            {"kind": "extrude",
             "params": {"sketch": sk_alias, "distance": height_mm,
                         "operation": operation, "alias": boss_alias},
             "label": f"{label_prefix} {i+1}/{count} {operation}"},
        ])
    return plan
