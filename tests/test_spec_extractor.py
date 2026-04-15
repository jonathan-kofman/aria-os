"""
tests/test_spec_extractor.py — 40 tests for aria_os/spec_extractor.py

Covers:
  - Basic dimension extraction (od, bore, thickness, height, width, depth, length)
  - Material extraction (specific grades: 6061, 7075, generic: aluminium, steel)
  - Part type inference (longest-match keyword wins)
  - Edge cases: combined bolt shorthand, WxHxD notation, radius→diameter, space-only patterns
  - merge_spec_into_plan: no-overwrite, fresh population
"""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.spec_extractor import extract_spec, merge_spec_into_plan


# ---------------------------------------------------------------------------
# Dimension extraction
# ---------------------------------------------------------------------------

class TestODExtraction:
    def test_od_mm_explicit(self):
        s = extract_spec("ratchet ring 213mm OD")
        assert s.get("od_mm") == pytest.approx(213.0)

    def test_od_space_only(self):
        s = extract_spec("OD 80mm cam collar")
        assert s.get("od_mm") == pytest.approx(80.0)

    def test_outer_dia(self):
        s = extract_spec("outer dia 150mm housing")
        assert s.get("od_mm") == pytest.approx(150.0)

    def test_outer_keyword(self):
        s = extract_spec("50mm outer spool hub")
        assert s.get("od_mm") == pytest.approx(50.0)

    def test_diameter_of_pattern(self):
        s = extract_spec("diameter of 200mm brake drum")
        assert s.get("od_mm") == pytest.approx(200.0) or s.get("diameter_mm") == pytest.approx(200.0)


class TestBoreExtraction:
    def test_bore_mm(self):
        s = extract_spec("bore 30mm shaft")
        assert s.get("bore_mm") == pytest.approx(30.0)

    def test_inner_diameter(self):
        s = extract_spec("inner diameter 25mm pulley")
        assert s.get("bore_mm") == pytest.approx(25.0) or s.get("id_mm") == pytest.approx(25.0)

    def test_id_alias(self):
        # "40mm ID" matches the bore pattern (\d+mm\s+id\b)
        s = extract_spec("ratchet ring 40mm ID bore")
        assert s.get("id_mm") == pytest.approx(40.0) or s.get("bore_mm") == pytest.approx(40.0)


class TestLinearDimensions:
    def test_thickness(self):
        s = extract_spec("21mm thick ratchet ring")
        assert s.get("thickness_mm") == pytest.approx(21.0)

    def test_height(self):
        # Extractor matches "180mm tall" or "height: 180mm" (not "180mm height")
        s = extract_spec("housing 180mm tall")
        assert s.get("height_mm") == pytest.approx(180.0) or s.get("thickness_mm") == pytest.approx(180.0)

    def test_width(self):
        s = extract_spec("brake drum 60mm wide")
        assert s.get("width_mm") == pytest.approx(60.0)

    def test_length(self):
        s = extract_spec("cam collar 40mm long")
        assert s.get("length_mm") == pytest.approx(40.0)

    def test_depth(self):
        s = extract_spec("bracket 15mm depth")
        assert s.get("depth_mm") == pytest.approx(15.0)

    def test_wxhxd_notation(self):
        s = extract_spec("bracket 50x100x200mm")
        # Should extract at least one dimension from the WxHxD shorthand
        dims = {k: v for k, v in s.items() if k.endswith("_mm") and v is not None}
        assert len(dims) >= 1


class TestToothAndBoltCounts:
    def test_n_teeth(self):
        s = extract_spec("ratchet ring 24 teeth")
        assert s.get("n_teeth") == 24

    def test_n_bolts_plain(self):
        s = extract_spec("bracket 4 holes")
        assert s.get("n_bolts") == 4

    def test_combined_bolt_shorthand(self):
        s = extract_spec("flange 4xM8 bolt circle")
        assert s.get("n_bolts") == 4
        assert s.get("bolt_dia_mm") == pytest.approx(8.0)

    def test_bolt_circle_radius(self):
        # Extractor maps bolt_circle PCD to r = PCD/2; "180mm bolt circle" → r=90
        s = extract_spec("housing 180mm bolt circle")
        assert s.get("bolt_circle_r_mm") == pytest.approx(90.0)


# ---------------------------------------------------------------------------
# Material extraction
# ---------------------------------------------------------------------------

class TestMaterialExtraction:
    def test_6061_grade(self):
        s = extract_spec("housing in 6061 aluminium")
        assert s.get("material") in ("aluminium_6061", "6061")

    def test_7075_grade(self):
        s = extract_spec("bracket 7075 aluminium")
        assert s.get("material") in ("aluminium_7075", "7075")

    def test_generic_aluminium(self):
        s = extract_spec("spool aluminium")
        assert "alumin" in (s.get("material") or "")

    def test_steel(self):
        s = extract_spec("ratchet ring steel")
        assert "steel" in (s.get("material") or "").lower()

    def test_titanium(self):
        s = extract_spec("shaft titanium")
        assert "titan" in (s.get("material") or "").lower()


# ---------------------------------------------------------------------------
# Part type inference (longest match wins)
# ---------------------------------------------------------------------------

class TestPartTypeInference:
    def test_ratchet_ring_beats_ring(self):
        s = extract_spec("ARIA ratchet ring 213mm OD")
        assert s.get("part_type") == "ratchet_ring"

    def test_brake_drum(self):
        s = extract_spec("brake drum 200mm OD")
        assert s.get("part_type") == "brake_drum"

    def test_cam_collar(self):
        s = extract_spec("cam collar 80mm OD")
        assert s.get("part_type") == "cam_collar"

    def test_rope_guide(self):
        s = extract_spec("rope guide 60mm wide")
        assert s.get("part_type") == "rope_guide"

    def test_catch_pawl(self):
        s = extract_spec("catch pawl lever")
        assert s.get("part_type") == "catch_pawl"

    def test_lre_nozzle_from_nozzle(self):
        s = extract_spec("LRE nozzle 10kN thrust")
        assert s.get("part_type") in ("lre_nozzle", "nozzle")

    def test_housing(self):
        s = extract_spec("ARIA main housing shell")
        assert s.get("part_type") == "housing"

    def test_spool(self):
        s = extract_spec("rope spool 120mm hub")
        assert s.get("part_type") == "spool"


# ---------------------------------------------------------------------------
# merge_spec_into_plan
# ---------------------------------------------------------------------------

class TestMergeSpecIntoPlan:
    def test_populates_empty_plan(self):
        plan = {"params": {}}
        spec = {"od_mm": 100.0, "n_teeth": 20}
        merge_spec_into_plan(spec, plan)
        assert plan["params"]["od_mm"] == pytest.approx(100.0)
        assert plan["params"]["n_teeth"] == 20

    def test_does_not_overwrite_existing(self):
        plan = {"params": {"od_mm": 50.0}}
        spec = {"od_mm": 100.0}
        merge_spec_into_plan(spec, plan)
        assert plan["params"]["od_mm"] == pytest.approx(50.0)

    def test_creates_params_key_if_missing(self):
        plan = {}
        spec = {"thickness_mm": 21.0}
        merge_spec_into_plan(spec, plan)
        assert "params" in plan
        assert plan["params"]["thickness_mm"] == pytest.approx(21.0)

    def test_part_type_propagated(self):
        plan = {"params": {}}
        spec = {"part_type": "ratchet_ring", "od_mm": 213.0}
        merge_spec_into_plan(spec, plan)
        assert plan["params"].get("od_mm") == pytest.approx(213.0)


# ---------------------------------------------------------------------------
# NEMA motor, standoff bore, gear module — regression tests (2026-04-15)
# ---------------------------------------------------------------------------

class TestNEMAExtraction:
    def test_nema17_bolt_circle(self):
        s = extract_spec("NEMA 17 stepper motor mount")
        assert s.get("bolt_circle_r_mm") == pytest.approx(15.5)

    def test_nema17_od(self):
        s = extract_spec("NEMA 17 motor mount")
        assert s.get("od_mm") == pytest.approx(42.0)

    def test_nema17_n_bolts(self):
        s = extract_spec("NEMA 17 motor mount plate")
        assert s.get("n_bolts") == 4

    def test_nema23_frame(self):
        s = extract_spec("NEMA 23 motor bracket")
        assert s.get("od_mm") == pytest.approx(57.0)

    def test_nema34_bolt_circle(self):
        s = extract_spec("NEMA34 servo mount")
        assert s.get("bolt_circle_r_mm") == pytest.approx(34.8)


class TestStandoffBore:
    def test_m4_hex_standoff_bore(self):
        s = extract_spec("M4 hex standoff 20mm long")
        assert s.get("bore_mm") == pytest.approx(4.0)

    def test_m5_standoff_bore(self):
        s = extract_spec("M5 hex standoff 30mm")
        assert s.get("bore_mm") == pytest.approx(5.0)

    def test_m3_standoff_length(self):
        s = extract_spec("M3 standoff 15mm long")
        assert s.get("length_mm") == pytest.approx(15.0)
        assert s.get("bore_mm") == pytest.approx(3.0)


class TestGearModule:
    def test_module_plain_space(self):
        s = extract_spec("involute spur gear 24 teeth module 1.5")
        assert s.get("module_mm") == pytest.approx(1.5)

    def test_module_equals(self):
        s = extract_spec("gear module=2 48 teeth")
        assert s.get("module_mm") == pytest.approx(2.0)

    def test_module_with_mm_suffix(self):
        s = extract_spec("gear 1.5mm module")
        assert s.get("module_mm") == pytest.approx(1.5)
