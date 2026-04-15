"""Tests for core.igl_schema — shape validation only (no semantics)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.igl_schema import (
    IGLDocument,
    PartInfo,
    StockBlock,
    StockCylinder,
    StockTube,
    Units,
    parse,
    serialize,
)


# ---------------------------------------------------------------------------
# Happy path: every example file parses
# ---------------------------------------------------------------------------

EXAMPLES_DIR = Path(__file__).parent.parent / "core" / "igl_examples"
EXAMPLE_FILES = sorted(p for p in EXAMPLES_DIR.glob("*.json"))


@pytest.mark.parametrize("example_path", EXAMPLE_FILES, ids=lambda p: p.stem)
def test_example_parses(example_path: Path):
    """Every JSON example in core/igl_examples must parse cleanly."""
    data = json.loads(example_path.read_text())
    doc = parse(data)
    assert isinstance(doc, IGLDocument)
    assert doc.part.name
    assert doc.stock is not None
    # Round-trip: serialize then re-parse
    round_tripped = parse(serialize(doc))
    assert round_tripped.part.name == doc.part.name
    assert len(round_tripped.features) == len(doc.features)


def test_every_example_is_loaded():
    """Sanity check: there is at least one example file."""
    assert EXAMPLE_FILES, "no IGL example files found"


# ---------------------------------------------------------------------------
# Stock variants
# ---------------------------------------------------------------------------

def test_block_stock_ok():
    doc = parse({
        "igl_version": "1.0",
        "part": {"name": "b", "units": "mm"},
        "stock": {"type": "block", "x": 10, "y": 10, "z": 10},
        "features": [],
    })
    assert isinstance(doc.stock, StockBlock)
    assert doc.stock.x == 10.0


def test_cylinder_stock_ok():
    doc = parse({
        "igl_version": "1.0",
        "part": {"name": "c", "units": "mm"},
        "stock": {"type": "cylinder", "diameter": 50, "height": 20},
        "features": [],
    })
    assert isinstance(doc.stock, StockCylinder)


def test_tube_stock_ok():
    doc = parse({
        "igl_version": "1.0",
        "part": {"name": "t", "units": "mm"},
        "stock": {"type": "tube", "outer_diameter": 50, "inner_diameter": 30, "height": 20},
        "features": [],
    })
    assert isinstance(doc.stock, StockTube)


def test_tube_inner_must_be_less_than_outer():
    with pytest.raises(Exception):
        parse({
            "igl_version": "1.0",
            "part": {"name": "t", "units": "mm"},
            "stock": {"type": "tube", "outer_diameter": 30, "inner_diameter": 30, "height": 20},
            "features": [],
        })


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def test_default_units_is_mm():
    doc = parse({
        "igl_version": "1.0",
        "part": {"name": "x"},
        "stock": {"type": "block", "x": 1, "y": 1, "z": 1},
        "features": [],
    })
    assert doc.part.units == Units.MM


def test_inches_units_parse():
    doc = parse({
        "igl_version": "1.0",
        "part": {"name": "x", "units": "inches"},
        "stock": {"type": "block", "x": 1, "y": 1, "z": 1},
        "features": [],
    })
    assert doc.part.units == Units.INCHES


def test_unknown_units_rejected():
    with pytest.raises(Exception):
        parse({
            "igl_version": "1.0",
            "part": {"name": "x", "units": "furlongs"},
            "stock": {"type": "block", "x": 1, "y": 1, "z": 1},
            "features": [],
        })


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def test_duplicate_feature_ids_rejected():
    with pytest.raises(Exception):
        parse({
            "igl_version": "1.0",
            "part": {"name": "x", "units": "mm"},
            "stock": {"type": "block", "x": 10, "y": 10, "z": 10},
            "features": [
                {"id": "f1", "type": "hole", "params": {}},
                {"id": "f1", "type": "hole", "params": {}},
            ],
        })


def test_unknown_feature_type_accepted_by_schema():
    """Schema is permissive on feature types — validator warns, not errors."""
    doc = parse({
        "igl_version": "1.0",
        "part": {"name": "x", "units": "mm"},
        "stock": {"type": "block", "x": 10, "y": 10, "z": 10},
        "features": [
            {"id": "f1", "type": "custom_thing", "params": {"depth": 2}},
        ],
    })
    assert doc.features[0].type == "custom_thing"


def test_depends_on_field_parses():
    doc = parse({
        "igl_version": "1.0",
        "part": {"name": "x", "units": "mm"},
        "stock": {"type": "block", "x": 10, "y": 10, "z": 10},
        "features": [
            {"id": "f1", "type": "pocket", "params": {"face": "top", "depth": 2}},
            {
                "id": "f2",
                "type": "fillet",
                "params": {"radius": 0.5},
                "depends_on": ["f1"],
            },
        ],
    })
    assert doc.features[1].depends_on == ["f1"]


# ---------------------------------------------------------------------------
# Stock type forbids extras
# ---------------------------------------------------------------------------

def test_extra_fields_rejected_in_part():
    with pytest.raises(Exception):
        parse({
            "igl_version": "1.0",
            "part": {"name": "x", "units": "mm", "weirdkey": 5},
            "stock": {"type": "block", "x": 10, "y": 10, "z": 10},
            "features": [],
        })
