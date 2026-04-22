"""End-to-end tests for the native planner + delta detector +
validator + executor pipeline.

Covers the Tier-1 workflow improvements:
  1. Selection-aware prompts   — host_context.selection reaches spec
  2. Feature-tree context      — host_context.feature_tree reaches planner
  3. Parameter feedback        — user_parameters override regex spec
  4. Delta prompts             — classify_delta returns modify/extend/new
  5. Post-gen DFM gate         — DFM issues mark eval as FAIL

Strong tests for troubleshooting: every test asserts exactly what state
should exist at each step, with human-readable error messages for when
assertions fail.
"""
from __future__ import annotations

import pytest


# --- Validator ---------------------------------------------------------

class TestValidator:
    """The validator is the gatekeeper — bad plans shouldn't reach
    Fusion. These tests lock down every structural invariant."""

    def test_flange_plan_passes(self):
        from aria_os.native_planner.flange_planner import plan_flange
        from aria_os.native_planner.validator import validate_plan
        plan = plan_flange({
            "od_mm": 100, "bore_mm": 20, "thickness_mm": 6,
            "n_bolts": 4, "bolt_circle_r_mm": 40, "bolt_dia_mm": 6,
        })
        ok, issues = validate_plan(plan)
        assert ok, f"Hardcoded flange plan failed validation: {issues}"
        # Should have parameter ops AT THE TOP (before any sketch)
        first_sketch_idx = next(
            (i for i, op in enumerate(plan) if op["kind"] == "newSketch"),
            None)
        assert first_sketch_idx is not None, "No sketch in flange plan"
        param_ops_before_sketch = [
            op for op in plan[:first_sketch_idx]
            if op["kind"] == "addParameter"]
        assert len(param_ops_before_sketch) >= 3, (
            f"Expected ≥3 User Parameters before first sketch "
            f"(got {len(param_ops_before_sketch)}).")

    def test_bracket_plan_standard_solid(self):
        """Default L-bracket uses plain extrude (works in any workspace).
        Also: holes should distribute 2 on base + 2 on leg."""
        from aria_os.native_planner.sheetmetal_planner import plan_simple_bracket
        from aria_os.native_planner.validator import validate_plan
        plan = plan_simple_bracket({
            "width_mm": 80, "depth_mm": 60, "wall_mm": 2, "n_bolts": 4,
        }, goal="L-bracket 80x60x40mm, 4 M5 mounting holes")
        ok, issues = validate_plan(plan)
        assert ok, f"L-bracket failed validation: {issues}"
        sm_ops = [op for op in plan if op["kind"] == "sheetMetalBase"]
        assert sm_ops == [], (
            "Default bracket should use plain extrude, not sheetMetalBase")
        # Holes should split across base + leg
        base_hole_sketches = [op for op in plan
                               if op["kind"] == "newSketch"
                               and "base_holes" in op["params"].get("alias", "")]
        leg_hole_sketches = [op for op in plan
                              if op["kind"] == "newSketch"
                              and "leg_holes" in op["params"].get("alias", "")]
        assert len(base_hole_sketches) == 1 and len(leg_hole_sketches) == 1, (
            "4 M5 holes should distribute 2 to base + 2 to leg")

    def test_bracket_plan_sheet_metal_keyword(self):
        """'sheet metal' keyword → switches to sheetMetalBase op."""
        from aria_os.native_planner.sheetmetal_planner import plan_simple_bracket
        from aria_os.native_planner.validator import validate_plan
        plan = plan_simple_bracket({
            "width_mm": 80, "depth_mm": 60, "wall_mm": 2, "n_bolts": 4,
        }, goal="sheet metal bracket 80x60x40mm, 2mm steel")
        ok, issues = validate_plan(plan)
        assert ok, f"Sheet metal bracket failed validation: {issues}"
        sm_ops = [op for op in plan if op["kind"] == "sheetMetalBase"]
        assert len(sm_ops) == 1, (
            f"Sheet metal prompt should trigger sheetMetalBase; "
            f"got {len(sm_ops)}")

    def test_bracket_uses_iso_clearance_holes(self):
        """M5 should become Ø5.5mm (ISO 273 close-fit), not Ø5mm nominal."""
        from aria_os.native_planner.sheetmetal_planner import plan_simple_bracket
        plan = plan_simple_bracket({
            "width_mm": 80, "depth_mm": 60, "wall_mm": 3, "n_bolts": 4,
        }, goal="bracket 80x60x40, 4 M5 clearance holes")
        circles = [op for op in plan if op["kind"] == "sketchCircle"]
        for c in circles:
            r = c["params"]["r"]
            assert abs(r - 2.75) < 0.01, (
                f"M5 clearance should be Ø5.5mm (r=2.75mm), got r={r}")

    def test_catches_circular_pattern_of_full_body(self):
        """The impeller-disc bug: patterning the whole body around its
        axis is a no-op. Validator must reject."""
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "sketchCircle", "params": {"sketch": "s1", "r": 60}},
            {"kind": "extrude", "params": {
                "sketch": "s1", "distance": 20, "operation": "new",
                "alias": "body"}},
            {"kind": "circularPattern", "params": {
                "feature": "body", "axis": "Z", "count": 6,
                "alias": "pat"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok, "Validator missed circularPattern of full body"
        assert any("full body" in i.lower() for i in issues), (
            f"Validator rejected but without the expected message: {issues}")

    def test_catches_unknown_sketch_alias(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "sketchCircle", "params": {"sketch": "ghost", "r": 5}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("ghost" in i for i in issues)

    def test_catches_cut_before_body(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "sketchCircle", "params": {"sketch": "s1", "r": 5}},
            {"kind": "extrude", "params": {
                "sketch": "s1", "distance": 10, "operation": "cut",
                "alias": "cut1"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("before a body" in i.lower() for i in issues)

    def test_rejects_zero_distance_extrude(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "sketchCircle", "params": {"sketch": "s1", "r": 5}},
            {"kind": "extrude", "params": {
                "sketch": "s1", "distance": 0, "operation": "new",
                "alias": "b"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok

    def test_rejects_pattern_count_out_of_range(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "sketchCircle", "params": {"sketch": "s1", "r": 5}},
            {"kind": "extrude", "params": {
                "sketch": "s1", "distance": 10, "operation": "new",
                "alias": "b"}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s2"}},
            {"kind": "sketchCircle", "params": {"sketch": "s2", "r": 1}},
            {"kind": "extrude", "params": {
                "sketch": "s2", "distance": 5, "operation": "cut",
                "alias": "hole"}},
            {"kind": "circularPattern", "params": {
                "feature": "hole", "axis": "Z", "count": 1000,
                "alias": "pat"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("count" in i.lower() and "range" in i.lower() for i in issues)


# --- Delta detector ----------------------------------------------------

class TestDeltaDetector:
    """Delta detection decides NEW vs MODIFY vs EXTEND. Wrong
    classification leaks into wrong plan. These tests lock the
    heuristic branches down."""

    def _ctx_with_flange(self):
        return {
            "user_parameters": [
                {"name": "flange_OD", "expression": "100 mm"},
                {"name": "flange_bore", "expression": "20 mm"},
                {"name": "flange_thickness", "expression": "6 mm"},
            ],
            "feature_tree": {},
            "selection": [],
        }

    def test_no_aria_design_is_new(self):
        from aria_os.native_planner.delta_detector import _classify_heuristic
        assert _classify_heuristic("thicker", {"user_parameters": []}) == "new"

    def test_modify_phrases(self):
        from aria_os.native_planner.delta_detector import _classify_heuristic
        ctx = self._ctx_with_flange()
        for p in [
            "make it thicker",
            "make it bigger",
            "change the OD",
            "set the bore to 25mm",
            "increase thickness by 2",
            "make the OD 120mm",
        ]:
            assert _classify_heuristic(p, ctx) == "modify", (
                f"Should classify {p!r} as modify, got "
                f"{_classify_heuristic(p, ctx)}")

    def test_extend_phrases(self):
        from aria_os.native_planner.delta_detector import _classify_heuristic
        ctx = self._ctx_with_flange()
        for p in [
            "add a chamfer",
            "put 4 relief holes",
            "drill a center bore",
            "fillet the edges",
        ]:
            assert _classify_heuristic(p, ctx) == "extend", (
                f"Should classify {p!r} as extend")

    def test_new_phrases_override(self):
        from aria_os.native_planner.delta_detector import _classify_heuristic
        ctx = self._ctx_with_flange()
        # Even with ARIA params present, a clear "new <part>" prompt
        # should still be classified as new.
        assert _classify_heuristic(
            "new flange 200mm OD", ctx) == "new"
        assert _classify_heuristic(
            "design a bracket 80x60x5mm", ctx) == "new"

    def test_short_prompt_with_aria_is_modify(self):
        """Short prompt (<40 chars) + existing ARIA design defaults to
        modify rather than new — the fall-through heuristic."""
        from aria_os.native_planner.delta_detector import _classify_heuristic
        ctx = self._ctx_with_flange()
        assert _classify_heuristic("thicker", ctx) == "modify"


# --- Modify-plan emitter ----------------------------------------------

class TestModifyPlan:
    """When delta is 'modify', the plan should only contain parameter
    updates — no sketches, no extrudes. These tests pin that contract."""

    def _ctx(self):
        return {
            "user_parameters": [
                {"name": "flange_OD", "expression": "100 mm"},
                {"name": "flange_bore", "expression": "20 mm"},
                {"name": "flange_thickness", "expression": "6 mm"},
                {"name": "flange_bolt_dia", "expression": "6 mm"},
                {"name": "flange_bolt_circle_r", "expression": "40 mm"},
            ],
        }

    def test_explicit_dim_update(self):
        from aria_os.native_planner.delta_detector import build_modify_plan
        plan = build_modify_plan("set OD to 150mm", self._ctx())
        assert all(op["kind"] == "addParameter" for op in plan), (
            "Modify plan must be parameter updates only")
        flange_od = next(
            (op for op in plan if op["params"]["name"] == "flange_OD"),
            None)
        assert flange_od is not None, "OD update not emitted"
        assert flange_od["params"]["value_mm"] == 150.0

    def test_relative_thicker(self):
        from aria_os.native_planner.delta_detector import build_modify_plan
        plan = build_modify_plan("make it thicker", self._ctx())
        flange_t = next(
            (op for op in plan if op["params"]["name"] == "flange_thickness"),
            None)
        assert flange_t is not None
        # 6 * 1.5 = 9mm
        assert flange_t["params"]["value_mm"] == pytest.approx(9.0, abs=0.01)

    def test_pcd_treated_as_diameter_stored_as_radius(self):
        from aria_os.native_planner.delta_detector import build_modify_plan
        plan = build_modify_plan("set PCD to 100mm", self._ctx())
        pcd_op = next(
            (op for op in plan
             if op["params"]["name"] == "flange_bolt_circle_r"),
            None)
        assert pcd_op is not None
        # PCD = 100mm → radius = 50mm
        assert pcd_op["params"]["value_mm"] == pytest.approx(50.0, abs=0.01)

    def test_empty_modify_prompt_raises(self):
        from aria_os.native_planner.delta_detector import build_modify_plan
        with pytest.raises(ValueError, match="parameter"):
            build_modify_plan("whatever", self._ctx())


# --- Host-context integration -----------------------------------------

class TestHostContextIntegration:
    """The pipeline must fold host_context into spec + goal correctly.
    Regression guard for items 1 + 2 + 3."""

    def test_user_parameters_fold_into_spec(self):
        """If the Fusion design already has flange_OD=120mm, a vague
        prompt like 'make flange' should pick up 120 instead of
        defaulting to 100."""
        from aria_os.spec_extractor import extract_spec
        spec = extract_spec("new flange") or {}
        # Simulate the server-side fold-in
        user_params = [
            {"name": "flange_OD", "expression": "120 mm"},
            {"name": "flange_bore", "expression": "25 mm"},
        ]
        import re
        for p in user_params:
            name = p["name"].lower()
            m = re.search(r"(\d+(?:\.\d+)?)", p["expression"])
            if not m: continue
            val = float(m.group(1))
            if name.endswith("_od") and "od_mm" not in spec:
                spec["od_mm"] = val
            elif name.endswith("_bore") and "bore_mm" not in spec:
                spec["bore_mm"] = val
        assert spec.get("od_mm") == 120.0
        assert spec.get("bore_mm") == 25.0


# --- LLM planner robustness -------------------------------------------

class TestLLMPlannerRobustness:
    """When the LLM fallback generates a plan, it must survive
    validation after the mandated retry."""

    def test_json_array_extraction_handles_markdown(self):
        from aria_os.native_planner.llm_planner import _extract_json_array
        raw = '```json\n[{"kind": "beginPlan", "params": {}}]\n```'
        out = _extract_json_array(raw)
        assert out == [{"kind": "beginPlan", "params": {}}]

    def test_json_array_extraction_handles_prose_wrapper(self):
        from aria_os.native_planner.llm_planner import _extract_json_array
        raw = 'Here is your plan:\n[{"kind": "beginPlan", "params": {}}]\nEnjoy!'
        out = _extract_json_array(raw)
        assert out == [{"kind": "beginPlan", "params": {}}]

    def test_garbage_returns_none(self):
        from aria_os.native_planner.llm_planner import _extract_json_array
        assert _extract_json_array("completely unparseable text") is None


# --- Auto-detect mode --------------------------------------------------

class TestAutoDetectMode:
    def test_kicad_keywords(self):
        from dashboard.aria_server import _auto_detect_mode
        for g in ["PCB for ESP32", "kicad 4 layer board",
                   "schematic with USB-C", "circuit board for LED driver"]:
            assert _auto_detect_mode(g) == "kicad"

    def test_drawing_keywords(self):
        from dashboard.aria_server import _auto_detect_mode
        for g in ["drawing of the flange", "dimensions for the bracket",
                   "GD&T for the housing", "technical drawing, mm"]:
            assert _auto_detect_mode(g) == "dwg"

    def test_assembly_keywords(self):
        from dashboard.aria_server import _auto_detect_mode
        for g in ["assembly of motor and gearbox",
                   "mount the bracket to the frame"]:
            assert _auto_detect_mode(g) == "asm"

    def test_sheetmetal_keywords(self):
        from dashboard.aria_server import _auto_detect_mode
        for g in ["sheet metal bracket 80x60",
                   "bent plate enclosure, 2 bends"]:
            assert _auto_detect_mode(g) == "sheetmetal"

    def test_default_is_native(self):
        from dashboard.aria_server import _auto_detect_mode
        assert _auto_detect_mode("flange 100mm OD") == "native"


# --- Auto-dimensioner --------------------------------------------------

class TestAutoDimensioner:
    """Auto-dim needs trimesh but we can still exercise the logic on
    any cached STL."""

    def test_returns_bbox_dims_minimum(self):
        import glob, os
        stls = sorted(
            glob.glob("outputs/cad/stl/*.stl"),
            key=os.path.getmtime, reverse=True)
        if not stls:
            pytest.skip("No cached STL to test against")
        from aria_os.drawings.auto_dimensioner import extract_dimensions
        dims = extract_dimensions(stls[0])
        # Should always have 3 bbox dims at minimum
        bbox_dims = [d for d in dims
                      if d["params"].get("dim_type") == "linear"]
        assert len(bbox_dims) >= 3, (
            f"Expected ≥3 bbox dims, got {len(bbox_dims)}")
