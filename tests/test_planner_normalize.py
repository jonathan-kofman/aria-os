"""Regression tests for the planner pre-validation normalizer.

The normalizer rewrites LLM-hallucinated param names and plane shorthands
to canonical forms before validation. Without it, a plan that says
`{"plane": "Top"}` or `{"diameter": 10}` would fail validation and force
a full regeneration round-trip — burning tokens and time on cosmetic drift.
"""
from __future__ import annotations

from aria_os.native_planner.validator import (
    _normalize_plan, validate_plan,
)


def test_plane_aliases_normalize_to_xy_xz_yz() -> None:
    cases = [
        ("Top",          "XZ"),
        ("Front",        "XY"),
        ("Right",        "YZ"),
        ("Front Plane",  "XY"),
        ("Top Plane",    "XZ"),
        ("Right Plane",  "YZ"),
        ("XY",           "XY"),  # already canonical
        ("xy",           "XY"),  # case insensitive
        ("X-Y",          "XY"),
        ("horizontal",   "XZ"),
        ("vertical",     "XY"),
        ("+Z",           "XY"),
        ("-Z",           "XY"),
    ]
    for raw, expected in cases:
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": raw, "alias": "s1"}},
        ]
        _normalize_plan(plan)
        assert plan[1]["params"]["plane"] == expected, (
            f"plane {raw!r} -> {plan[1]['params']['plane']!r}, "
            f"expected {expected!r}")


def test_diameter_normalizes_to_radius() -> None:
    plan = [
        {"kind": "beginPlan", "params": {}},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
        {"kind": "sketchCircle",
          "params": {"sketch": "s1", "diameter": 10}},
    ]
    _normalize_plan(plan)
    assert plan[2]["params"]["r"] == 5.0
    assert "diameter" not in plan[2]["params"]
    assert "diameter->r" in plan[2]["_normalized"]


def test_radius_alias_passes_through_unchanged_value() -> None:
    plan = [
        {"kind": "beginPlan", "params": {}},
        {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
        {"kind": "sketchCircle",
          "params": {"sketch": "s1", "radius": 7}},
    ]
    _normalize_plan(plan)
    assert plan[2]["params"]["r"] == 7
    assert "radius" not in plan[2]["params"]


def test_extrude_depth_and_type_normalize() -> None:
    plan = [
        {"kind": "beginPlan", "params": {}},
        {"kind": "newSketch",
          "params": {"plane": "XY", "alias": "s1"}},
        {"kind": "sketchCircle",
          "params": {"sketch": "s1", "r": 5}},
        {"kind": "extrude",
          "params": {"sketch": "s1", "depth": 12, "type": "boss"}},
    ]
    _normalize_plan(plan)
    p = plan[3]["params"]
    assert p["distance"] == 12
    assert p["operation"] == "new"
    assert "depth" not in p
    assert "type" not in p


def test_normalized_plan_passes_validation() -> None:
    """The whole point: a plan that uses LLM-style names validates clean
    after normalization, where it would have failed before."""
    plan = [
        {"kind": "beginPlan", "params": {}},
        {"kind": "newSketch",
          "params": {"plane": "Top", "alias": "s1"}},
        {"kind": "sketchCircle",
          "params": {"sketch": "s1", "diameter": 20}},
        {"kind": "extrude",
          "params": {"sketch": "s1", "depth": 5, "type": "boss"}},
    ]
    ok, issues = validate_plan(plan)
    assert ok, f"normalized plan should validate; issues={issues}"


def test_canonical_plan_is_idempotent() -> None:
    """A plan that's already canonical must not be touched."""
    plan = [
        {"kind": "beginPlan", "params": {}},
        {"kind": "newSketch",
          "params": {"plane": "XY", "alias": "s1"}},
        {"kind": "sketchCircle",
          "params": {"sketch": "s1", "r": 5}},
    ]
    before = [op.get("_normalized") for op in plan]
    _normalize_plan(plan)
    after = [op.get("_normalized") for op in plan]
    assert before == after == [None, None, None]


def test_circular_pattern_count_alias() -> None:
    plan = [
        {"kind": "beginPlan", "params": {}},
        {"kind": "circularPattern",
          "params": {"feature": "f1", "instances": 4}},
    ]
    _normalize_plan(plan)
    assert plan[1]["params"]["count"] == 4
    assert "instances" not in plan[1]["params"]
