"""Tests for core.igl_validator — semantic validation."""
from __future__ import annotations

import pytest

from core.igl_schema import parse
from core.igl_validator import validate


def _doc_with(features: list[dict]) -> dict:
    return {
        "igl_version": "1.0",
        "part": {"name": "test", "units": "mm"},
        "stock": {"type": "block", "x": 100, "y": 100, "z": 50},
        "features": features,
    }


def test_empty_features_is_ok():
    report = validate(_doc_with([]))
    assert report.ok
    assert not report.errors


def test_valid_pocket_passes():
    report = validate(_doc_with([
        {
            "id": "f1",
            "type": "pocket",
            "params": {"face": "top", "depth": 5.0, "length": 20, "width": 20},
        }
    ]))
    assert report.ok, report.errors


def test_pocket_missing_depth_flagged():
    report = validate(_doc_with([
        {"id": "f1", "type": "pocket", "params": {"face": "top"}},
    ]))
    assert not report.ok
    assert any("depth" in e.message for e in report.errors)


def test_pocket_deeper_than_stock_flagged():
    """Pocket depth exceeding min stock dimension is an error."""
    doc = _doc_with([
        {"id": "f1", "type": "pocket",
         "params": {"face": "top", "depth": 1000, "length": 5, "width": 5}},
    ])
    report = validate(doc)
    assert not report.ok
    assert any("depth" in e.message.lower() for e in report.errors)


def test_hole_missing_required_fields():
    report = validate(_doc_with([
        {"id": "f1", "type": "hole", "params": {"face": "top"}},
    ]))
    assert not report.ok
    missing = [e.message for e in report.errors]
    assert any("center_x" in m for m in missing)
    assert any("center_y" in m for m in missing)
    assert any("diameter" in m for m in missing)


def test_depends_on_forward_ref_is_error():
    report = validate(_doc_with([
        {"id": "f1", "type": "pocket",
         "params": {"face": "top", "depth": 2, "length": 10, "width": 10},
         "depends_on": ["f2"]},
        {"id": "f2", "type": "pocket",
         "params": {"face": "top", "depth": 2, "length": 10, "width": 10}},
    ]))
    assert not report.ok
    assert any("depends on" in e.message for e in report.errors)


def test_depends_on_backward_ref_is_ok():
    report = validate(_doc_with([
        {"id": "f1", "type": "pocket",
         "params": {"face": "top", "depth": 2, "length": 10, "width": 10}},
        {"id": "f2", "type": "pocket",
         "params": {"face": "top", "depth": 2, "length": 10, "width": 10},
         "depends_on": ["f1"]},
    ]))
    assert report.ok, report.errors


def test_unknown_feature_type_is_warning_not_error():
    report = validate(_doc_with([
        {"id": "f1", "type": "some_unknown_thing", "params": {}},
    ]))
    # Unknown type is a warning, not an error.
    assert report.ok
    assert any("unknown type" in w.message for w in report.warnings)


def test_fillet_target_missing_is_warning():
    report = validate(_doc_with([
        {"id": "f1", "type": "fillet",
         "params": {"radius": 2, "target": "nonexistent"}},
    ]))
    # Missing radius is checked by _check_required_params (radius satisfied);
    # the fillet warning about missing target should be present.
    assert any("nonexistent" in w.message for w in report.warnings)
