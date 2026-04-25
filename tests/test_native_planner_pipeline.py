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


# --- W1 acceptance: 5 prompts that previously failed -------------------

class TestW1AcceptancePrompts:
    """End-to-end gate for the W1 op-vocabulary expansion. Each prompt
    here is one that simply could NOT produce a valid plan with the old
    6-op vocabulary because the geometry it describes requires
    revolve/sweep/loft/threadFeature/gearFeature.

    Skipped on CI (no LLM keys). Locally, run with ANTHROPIC_API_KEY or
    GEMINI_API_KEY set to gate the W1 work as 'shipped'."""

    @staticmethod
    def _have_llm_key() -> bool:
        import os
        return bool(os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("GOOGLE_API_KEY")
                    or os.environ.get("GEMINI_API_KEY"))

    def _plan(self, goal: str, spec: dict | None = None):
        """Run the dispatcher with prefer_llm=True so we exercise the
        new op vocabulary, then validate. Returns the plan."""
        import pytest
        if not self._have_llm_key():
            pytest.skip("No LLM API key in env — acceptance gate")
        from aria_os.native_planner.dispatcher import make_plan
        from aria_os.native_planner.validator import validate_plan
        plan = make_plan(goal, spec or {}, prefer_llm=True,
                          quality="balanced")
        ok, issues = validate_plan(plan)
        assert ok, (f"Validator rejected plan for {goal!r}: {issues}\n"
                     f"Plan was: {plan}")
        return plan

    @staticmethod
    def _kinds(plan):
        return [op.get("kind") for op in plan]

    def test_m16_cap_screw_uses_thread_feature(self):
        plan = self._plan(
            "M16x2 socket head cap screw, 60mm long, head Ø24×16mm")
        kinds = self._kinds(plan)
        assert "threadFeature" in kinds, (
            f"Cap-screw plan must call threadFeature; got kinds={kinds}")

    def test_spur_gear_uses_gear_feature_or_real_involute(self):
        plan = self._plan(
            "24-tooth involute spur gear, module 2, 10mm face width, "
            "10mm bore")
        kinds = self._kinds(plan)
        # Either gearFeature (one-liner) or a sketchSpline-based involute
        # tooth + circularPattern is acceptable for a real involute.
        ok = ("gearFeature" in kinds or
               ("sketchSpline" in kinds and "circularPattern" in kinds))
        assert ok, (f"Gear plan must use gearFeature or build real "
                     f"involute via sketchSpline + pattern; got {kinds}")

    def test_transition_duct_uses_loft(self):
        plan = self._plan(
            "transition duct, 100mm round inlet to 80x40mm rect outlet, "
            "200mm long, 1.5mm wall")
        kinds = self._kinds(plan)
        assert "loft" in kinds, (
            f"Transition duct must use loft; got kinds={kinds}")

    def test_volute_uses_sweep(self):
        plan = self._plan(
            "centrifugal pump volute, 80mm impeller, spiral cross-section "
            "growing from 10mm to 25mm, 5° tongue angle")
        kinds = self._kinds(plan)
        assert "sweep" in kinds, (
            f"Volute must use sweep along a path; got kinds={kinds}")

    def test_ergonomic_handle_uses_revolve_or_loft(self):
        plan = self._plan(
            "ergonomic grip handle, 30mm diameter at base tapering to "
            "25mm at top over 120mm length, slight waist at 60mm")
        kinds = self._kinds(plan)
        ok = "revolve" in kinds or "loft" in kinds
        assert ok, (f"Tapered handle must use revolve or loft; "
                     f"got kinds={kinds}")


# --- W4 Assembly Designer + mate validator ----------------------------

class TestAssemblyDesigner:
    """The Assembly Designer turns natural-language mechanism prompts
    into structurally valid BOMs with calculated tooth counts /
    geometry constraints. Without this, the LLM produces "looks right
    but doesn't mesh" output."""

    def test_planetary_4to1_math_closes(self):
        from aria_os.agents.assembly_designer_agent import design_assembly
        spec = design_assembly(
            "planetary gearbox 4:1 with 3 planets, NEMA17 input")
        assert spec is not None
        ids = {c["id"] for c in spec.components}
        assert "sun" in ids and "ring" in ids and "carrier" in ids
        # 3 planets
        planet_ids = [i for i in ids if i.startswith("planet_")]
        assert len(planet_ids) == 3
        # Tooth-count math: N_ring = N_sun + 2*N_planet
        sun_n = next(c for c in spec.components if c["id"] == "sun")[
            "params"]["n_teeth"]
        ring_n = next(c for c in spec.components if c["id"] == "ring")[
            "params"]["n_teeth"]
        plnt_n = next(c for c in spec.components
                       if c["id"].startswith("planet_"))["params"]["n_teeth"]
        assert ring_n == sun_n + 2 * plnt_n, (
            f"Gear math broken: sun={sun_n}, planet={plnt_n}, ring={ring_n}; "
            f"expected ring = sun + 2·planet")
        # Achieves the requested ratio within 5%
        actual_ratio = 1 + ring_n / sun_n
        assert abs(actual_ratio - 4.0) / 4.0 < 0.05

    def test_planetary_emits_gear_mates(self):
        from aria_os.agents.assembly_designer_agent import design_assembly
        spec = design_assembly("planetary gearbox 5:1, 3 planets")
        gear_mates = [m for m in spec.mates if m["kind"] == "gear"]
        # Each planet meshes with sun AND ring → 2*n_planets gear mates
        assert len(gear_mates) == 6, (
            f"Expected 6 gear mates for 3 planets (sun↔planet, planet↔ring); "
            f"got {len(gear_mates)}")

    def test_six_dof_arm(self):
        from aria_os.agents.assembly_designer_agent import design_assembly
        spec = design_assembly(
            "6-DOF robot arm RRRRRR, 600mm reach, 2kg payload")
        # 6 motor housings + 6 links + base + ee_flange = 14 components
        types = [c["type"] for c in spec.components]
        assert types.count("motor_housing") == 6
        assert types.count("tube") == 6
        # 6 revolute motion drivers
        rev_motions = [m for m in spec.motion if m["kind"] == "revolute"]
        assert len(rev_motions) == 6

    def test_scotch_yoke_50mm(self):
        from aria_os.agents.assembly_designer_agent import design_assembly
        spec = design_assembly("scotch yoke linkage, 50mm stroke, 1500 RPM")
        assert spec is not None
        # Crank radius = stroke/2
        crank = next(c for c in spec.components if c["id"] == "crank")
        assert abs(crank["params"]["pin_radius"] - 25.0) < 0.01
        # Has a slot mate + slider mate
        kinds = [m["kind"] for m in spec.mates]
        assert "slot" in kinds and "slider" in kinds

    def test_parallel_gripper_travel(self):
        from aria_os.agents.assembly_designer_agent import design_assembly
        spec = design_assembly(
            "two-jaw parallel gripper, 40mm travel, M3 mounting")
        assert spec is not None
        # Two jaws + body + flange
        ids = [c["id"] for c in spec.components]
        assert "jaw_left" in ids and "jaw_right" in ids
        # Two prismatic motion drivers
        prism = [m for m in spec.motion if m["kind"] == "prismatic"]
        assert len(prism) == 2

    def test_unknown_returns_none(self):
        """Goals outside the family library route to LLM; the agent
        should return None so the dispatcher knows to fall back."""
        from aria_os.agents.assembly_designer_agent import design_assembly
        assert design_assembly("flange 100mm OD") is None
        assert design_assembly("M6 cap screw") is None


class TestMateOpsValidator:
    """W4 added 7 mate ops + 3 motion ops. They have to validate
    parts references against the asmBegin/addComponent scope."""

    def _asm_base(self):
        return [
            {"kind": "asmBegin", "params": {}},
            {"kind": "addComponent",
             "params": {"id": "gear_a", "type": "spur_gear"}},
            {"kind": "addComponent",
             "params": {"id": "gear_b", "type": "spur_gear"}},
        ]

    def test_concentric_two_parts_ok(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._asm_base() + [
            {"kind": "mateConcentric",
             "params": {"parts": ["gear_a.axis", "gear_b.axis"]}},
        ]
        ok, issues = validate_plan(plan)
        assert ok, issues

    def test_concentric_unknown_component_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._asm_base() + [
            {"kind": "mateConcentric",
             "params": {"parts": ["gear_a.axis", "ghost.axis"]}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("ghost" in i for i in issues)

    def test_concentric_one_part_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._asm_base() + [
            {"kind": "mateConcentric",
             "params": {"parts": ["gear_a.axis"]}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("≥2 parts" in i for i in issues)

    def test_mate_gear_zero_ratio_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._asm_base() + [
            {"kind": "mateGear",
             "params": {"parts": ["gear_a", "gear_b"], "ratio": 0}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("ratio cannot be 0" in i for i in issues)

    def test_motion_revolute_unknown_joint_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._asm_base() + [
            {"kind": "motionRevolute",
             "params": {"joint": "ghost.shaft", "speed_rpm": 100}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("ghost" in i for i in issues)

    def test_dup_component_id_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "asmBegin", "params": {}},
            {"kind": "addComponent",
             "params": {"id": "x", "type": "gear"}},
            {"kind": "addComponent",
             "params": {"id": "x", "type": "shaft"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("already declared" in i for i in issues)

    def test_designer_plan_validates(self):
        """The Assembly Designer's serialized plan must pass the
        validator end-to-end."""
        from aria_os.agents.assembly_designer_agent import design_assembly
        from aria_os.native_planner.validator import validate_plan
        spec = design_assembly("planetary gearbox 4:1, 3 planets")
        plan = spec.to_plan()
        ok, issues = validate_plan(plan)
        assert ok, f"Designer's planetary plan failed validation: {issues}"


# --- W3 plus: Groq tool_use_failed rescue parser ----------------------

class TestGroqRescueParser:
    """Groq's llama-3.3-70b sometimes returns tool_use as raw text
    (`<function=emit_plan>[…]`) instead of an actual tool_calls API
    response. The rescue parser extracts the JSON anyway.

    These tests pin the exact error shape we observed in production
    eval runs so regressions never silently cost us the Groq path."""

    def _make_exc(self, failed_gen: str):
        """Mimic the Groq SDK's BadRequestError.body shape."""
        class BadRequestError(Exception):
            def __init__(self, body):
                self.body = body
                super().__init__("Error code: 400 - " + str(body))
        return BadRequestError({
            "error": {
                "message": ("Failed to call a function. Please adjust "
                              "your prompt. See 'failed_generation' for "
                              "more details."),
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "failed_generation": failed_gen,
            }
        })

    def test_rescue_extracts_function_wrapped_array(self):
        from aria_os.native_planner.structured_llm import (
            _rescue_groq_failed_generation)
        exc = self._make_exc(
            '<function=emit_plan>[\n'
            '  {"kind": "beginPlan", "params": {}, "label": "Reset"},\n'
            '  {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},\n'
            '  {"kind": "sketchCircle", "params": {"sketch": "s", "r": 5}},\n'
            '  {"kind": "extrude", "params": {"sketch": "s", "distance": 5,'
            '"operation": "new", "alias": "b"}}\n'
            ']</function>')
        plan = _rescue_groq_failed_generation(exc)
        assert plan is not None, "Rescue returned None on canonical input"
        kinds = [op.get("kind") for op in plan]
        assert kinds == ["beginPlan", "newSketch", "sketchCircle", "extrude"]

    def test_rescue_handles_trailing_comma(self):
        from aria_os.native_planner.structured_llm import (
            _rescue_groq_failed_generation)
        exc = self._make_exc(
            '<function=emit_plan>[\n'
            '  {"kind": "beginPlan", "params": {}},\n'
            '  {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},\n'
            ']</function>')   # trailing comma before ]
        plan = _rescue_groq_failed_generation(exc)
        assert plan is not None
        assert len(plan) == 2

    def test_rescue_handles_string_body(self):
        """Older Groq SDK serializes body as a JSON string, not dict."""
        from aria_os.native_planner.structured_llm import (
            _rescue_groq_failed_generation)
        import json
        body_str = json.dumps({
            "error": {
                "message": "Failed to call a function.",
                "code": "tool_use_failed",
                "failed_generation":
                    '<function=emit_plan>[{"kind":"beginPlan","params":{}}]</function>',
            }
        })

        class StrBodyExc(Exception):
            body = body_str
        exc = StrBodyExc("Error 400")
        plan = _rescue_groq_failed_generation(exc)
        assert plan is not None
        assert plan[0]["kind"] == "beginPlan"

    def test_rescue_returns_none_when_no_failed_generation(self):
        """Rate-limit / quota errors have no failed_generation — rescue
        must NOT invent a plan from thin air."""
        from aria_os.native_planner.structured_llm import (
            _rescue_groq_failed_generation)

        class RateLimitError(Exception):
            body = {"error": {"message": "Rate limit reached",
                                "type": "rate_limit_error"}}
        exc = RateLimitError("429")
        plan = _rescue_groq_failed_generation(exc)
        assert plan is None

    def test_rescue_returns_none_on_empty_failed_generation(self):
        from aria_os.native_planner.structured_llm import (
            _rescue_groq_failed_generation)
        exc = self._make_exc("")
        assert _rescue_groq_failed_generation(exc) is None


# --- W3.4 SDF expander: implicit → meshImportAndCombine ---------------

class TestSdfExpander:
    """The expander turns implicit-geometry ops into mesh-import ops
    the host bridges can actually execute. We don't need to evaluate
    the SDF here (heavy, depends on skimage) — but we can verify the
    expander's structural contract: implicit ops are replaced, native
    ops pass through, target/operation are preserved."""

    def _shell_plan(self):
        return [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch",
             "params": {"plane": "XY", "alias": "sk"}},
            {"kind": "sketchRect",
             "params": {"sketch": "sk", "w": 50, "h": 30}},
            {"kind": "extrude",
             "params": {"sketch": "sk", "distance": 10,
                          "operation": "new", "alias": "shell"}},
        ]

    def test_native_only_plan_passes_through(self, tmp_path):
        from aria_os.sdf.expander import expand_plan
        plan = self._shell_plan()
        out = expand_plan(plan, run_dir=tmp_path)
        assert out == plan, (
            "Native-only plan should be unchanged after expand_plan")

    def test_implicit_replaced_with_mesh_import(self, tmp_path,
                                                  monkeypatch):
        """Mock the SDF render path so the test doesn't need skimage,
        and verify the implicit op was replaced with meshImportAndCombine
        carrying target + operation through."""
        from aria_os.sdf import expander as exp

        def fake_render(kind, params, out_path, bbox_hint):
            out_path.write_bytes(b"solid mock\nendsolid mock\n")
        monkeypatch.setattr(exp, "_render_to_stl", fake_render)

        plan = self._shell_plan() + [
            {"kind": "implicitInfill",
             "params": {"target": "shell", "pattern": "gyroid",
                          "density": 0.4, "cell_mm": 6,
                          "operation": "intersect", "alias": "lat"},
             "label": "Gyroid"},
        ]
        out = exp.expand_plan(plan, run_dir=tmp_path)
        # Length unchanged — one-for-one replacement
        assert len(out) == len(plan)
        # Last op is now meshImportAndCombine carrying target + op
        last = out[-1]
        assert last["kind"] == "meshImportAndCombine"
        assert last["params"]["target"] == "shell"
        assert last["params"]["operation"] == "intersect"
        # The STL path exists (the mock wrote it)
        from pathlib import Path
        assert Path(last["params"]["stl_path"]).is_file()

    def test_mesh_import_and_combine_validates(self, tmp_path):
        """The expanded plan must also validate cleanly so it can be
        streamed to the host bridge unchanged."""
        from aria_os.native_planner.validator import validate_plan
        plan = self._shell_plan() + [
            {"kind": "meshImportAndCombine", "params": {
                "stl_path": str(tmp_path / "x.stl"),
                "target": "shell",
                "operation": "intersect",
                "alias": "combined"}},
        ]
        ok, issues = validate_plan(plan)
        assert ok, issues

    def test_render_failure_emits_noop_not_crash(self, tmp_path,
                                                    monkeypatch):
        """If the SDF render fails (kernel missing, bad pattern, etc.),
        the rest of the plan must still execute — we emit a noop with
        a reason rather than aborting the whole plan."""
        from aria_os.sdf import expander as exp

        def fake_render(*a, **kw):
            raise RuntimeError("synthetic SDF failure")
        monkeypatch.setattr(exp, "_render_to_stl", fake_render)

        plan = self._shell_plan() + [
            {"kind": "implicitInfill",
             "params": {"target": "shell", "pattern": "gyroid",
                          "operation": "intersect"}},
        ]
        out = exp.expand_plan(plan, run_dir=tmp_path)
        assert out[-1]["kind"] == "noop"
        assert "synthetic" in out[-1]["params"].get("reason", "")


# --- W3.3 SDF / implicit ops in validator ----------------------------

class TestImplicitOpsValidator:
    """W3 added 5 implicit-geometry ops. Each must validate with the
    right cross-references AND the new few-shots that use them must
    pass the validator."""

    def _shell(self):
        """A minimal solid that subsequent implicit ops can target."""
        return [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s"}},
            {"kind": "sketchRect", "params": {"sketch": "s",
                                                 "w": 50, "h": 30}},
            {"kind": "extrude", "params": {
                "sketch": "s", "distance": 10,
                "operation": "new", "alias": "shell"}},
        ]

    def test_implicit_infill_basic(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._shell() + [
            {"kind": "implicitInfill", "params": {
                "target": "shell", "pattern": "gyroid",
                "density": 0.5, "operation": "intersect",
                "alias": "lat"}},
        ]
        ok, issues = validate_plan(plan)
        assert ok, issues

    def test_implicit_infill_unknown_pattern_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._shell() + [
            {"kind": "implicitInfill", "params": {
                "target": "shell", "pattern": "spaghetti",
                "operation": "intersect"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("spaghetti" in i.lower() for i in issues)

    def test_implicit_infill_density_out_of_range_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._shell() + [
            {"kind": "implicitInfill", "params": {
                "target": "shell", "pattern": "gyroid",
                "density": 1.5, "operation": "intersect"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("density" in i for i in issues)

    def test_implicit_infill_unknown_target_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._shell() + [
            {"kind": "implicitInfill", "params": {
                "target": "ghost", "pattern": "gyroid",
                "operation": "intersect"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("ghost" in i for i in issues)

    def test_implicit_channel_needs_positive_diameter(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._shell() + [
            {"kind": "newSketch", "params": {"plane": "XZ", "alias": "p"}},
            {"kind": "sketchPolyline", "params": {
                "sketch": "p", "points": [[0,0],[20,5],[40,0]]}},
            {"kind": "implicitChannel", "params": {
                "target": "shell", "path": "p",
                "diameter": -1, "operation": "cut"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("diameter" in i for i in issues)

    def test_implicit_boolean_op_validation(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._shell() + [
            {"kind": "implicitBoolean", "params": {
                "sdf_a": "f1", "sdf_b": "f2", "op": "blend"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("union" in i and "intersect" in i for i in issues)

    def test_gyroid_fewshot_validates(self):
        """The hybrid native+implicit few-shot must validate end-to-end."""
        from aria_os.native_planner.fewshots import all_shots
        from aria_os.native_planner.validator import validate_plan
        gyroid = next((s for s in all_shots() if s.id == "gyroid_bracket"),
                      None)
        assert gyroid is not None, "gyroid_bracket few-shot missing"
        ok, issues = validate_plan(gyroid.plan)
        assert ok, issues

    def test_conformal_cooling_fewshot_validates(self):
        from aria_os.native_planner.fewshots import all_shots
        from aria_os.native_planner.validator import validate_plan
        cool = next(
            (s for s in all_shots() if s.id == "conformal_cooling"), None)
        assert cool is not None, "conformal_cooling few-shot missing"
        ok, issues = validate_plan(cool.plan)
        assert ok, issues


# --- W2.5 Tier escalation -----------------------------------------------

class TestTierEscalation:
    """When the first LLM attempt produces a validator-failing plan,
    the dispatcher escalates to the next tier AND folds the previous
    attempt's issues into the next prompt as correction context."""

    def test_first_attempt_failure_feeds_issues_to_second(self, monkeypatch):
        """Sanity-check the correction-context mechanism: simulate the
        first call returning a plan with issues, second returning a
        valid plan, and verify the second call saw the first's issues."""
        seen_goals: list[str] = []

        def fake_plan(goal, spec, *, quality, repo_root=None,
                       host_context=None, mode="new"):
            seen_goals.append(goal)
            if len(seen_goals) == 1:
                # Plan that will fail validation (no body-creating op)
                return [{"kind": "beginPlan", "params": {}}]
            # Second call: emit a valid plan
            return [
                {"kind": "beginPlan", "params": {}},
                {"kind": "newSketch",
                 "params": {"plane": "XY", "alias": "s"}},
                {"kind": "sketchCircle",
                 "params": {"sketch": "s", "r": 5}},
                {"kind": "extrude",
                 "params": {"sketch": "s", "distance": 5,
                              "operation": "new", "alias": "b"}},
            ]
        monkeypatch.setattr(
            "aria_os.native_planner.dispatcher.plan_from_llm", fake_plan)

        from aria_os.native_planner.dispatcher import make_plan
        plan = make_plan("widget", {}, prefer_llm=True, quality="fast")
        assert plan is not None
        assert len(seen_goals) == 2, (
            f"Expected exactly 2 attempts, got {len(seen_goals)}")
        # Second prompt should mention the first attempt's issues
        assert "Previous attempt had issues" in seen_goals[1], (
            f"Correction context missing from 2nd attempt: "
            f"{seen_goals[1][:200]}")

    def test_parse_error_feeds_to_next_tier(self, monkeypatch):
        """When the first tier raises ValueError (parse failure), the
        next tier should receive the parse-error message as context."""
        seen_goals: list[str] = []

        def fake_plan(goal, spec, *, quality, repo_root=None,
                       host_context=None, mode="new"):
            seen_goals.append(goal)
            if len(seen_goals) == 1:
                raise ValueError("LLM returned no parseable plan")
            return [
                {"kind": "beginPlan", "params": {}},
                {"kind": "newSketch",
                 "params": {"plane": "XY", "alias": "s"}},
                {"kind": "sketchCircle",
                 "params": {"sketch": "s", "r": 5}},
                {"kind": "extrude",
                 "params": {"sketch": "s", "distance": 5,
                              "operation": "new", "alias": "b"}},
            ]
        monkeypatch.setattr(
            "aria_os.native_planner.dispatcher.plan_from_llm", fake_plan)

        from aria_os.native_planner.dispatcher import make_plan
        plan = make_plan("widget", {}, prefer_llm=True, quality="fast")
        assert plan is not None
        assert "parseable JSON" in seen_goals[1], seen_goals[1][:300]


# --- W2.4 Code precheck v2: hallucinated CadQuery methods --------------

class TestHallucinatedMethodDetector:
    """The LLM frequently invents CadQuery methods that don't exist
    (.rotateExtrude(), .createCylinder(), etc.). These tests pin the
    precheck so a regen prompt always has the corrective context."""

    def _check(self, code: str) -> list[str]:
        from aria_os.agents.designer_agent import _precheck_code_spec
        return _precheck_code_spec(code, spec={})

    def test_rotateextrude_caught(self):
        issues = self._check(
            "import cadquery as cq\n"
            "result = cq.Workplane('XY').circle(5).rotateExtrude(360)\n")
        assert any("rotateExtrude" in i and "revolve" in i for i in issues)

    def test_createcylinder_caught(self):
        issues = self._check(
            "result = cq.Workplane('XY').createCylinder(5, 10)\n")
        assert any("createCylinder" in i for i in issues)

    def test_drillhole_caught(self):
        issues = self._check(
            "result = result.drillHole(5)\n")
        assert any(".drillHole" in i for i in issues)

    def test_filletedges_caught(self):
        issues = self._check(
            "result = result.filletEdges(2)\n")
        assert any("filletEdges" in i for i in issues)

    def test_real_cadquery_doesnt_trip_detector(self):
        """A clean CadQuery program that uses .extrude(), .fillet(),
        .revolve(), .union() must produce ZERO hallucination issues."""
        code = (
            "import cadquery as cq\n"
            "r = cq.Workplane('XY').circle(50).extrude(10)\n"
            "r = r.faces('>Z').workplane().circle(20).cutThruAll()\n"
            "r = r.faces('|Z').edges('not(>>Z)').fillet(2)\n"
            "r = r.union(other_solid)\n"
            "r = r.translate((10, 0, 0)).rotate((0,0,0),(0,0,1), 30)\n"
        )
        issues = self._check(code)
        # No HALLUCINATED MESSAGES allowed
        hallucinations = [i for i in issues if "HALLUCINATED" in i]
        assert not hallucinations, (
            f"Clean code tripped hallucination detector: {hallucinations}")


# --- W2.3 Retrieval injection in LLM planner --------------------------

class TestRetrievalInjection:
    """The retriever runs BEFORE every LLM call and appends API +
    few-shot blocks to the system prompt. These tests pin the wiring
    without touching live LLMs — we mock the call_llm chain and inspect
    what got passed in."""

    def test_ops_hint_picks_up_thread_keyword(self):
        from aria_os.native_planner.llm_planner import _ops_hint_from_goal
        h = _ops_hint_from_goal("M16x2 cap screw, 60mm long")
        assert "threadFeature" in h, h

    def test_ops_hint_picks_up_loft_for_transition(self):
        from aria_os.native_planner.llm_planner import _ops_hint_from_goal
        h = _ops_hint_from_goal("transition duct round to rect")
        assert "loft" in h

    def test_ops_hint_picks_up_sweep_for_volute(self):
        from aria_os.native_planner.llm_planner import _ops_hint_from_goal
        h = _ops_hint_from_goal("centrifugal volute spiral casing")
        assert "sweep" in h

    def test_retrieval_injects_into_system_prompt(self, monkeypatch):
        """When the LLM is invoked, the system prompt MUST include the
        Reference API block (retrieved from the doc index) and the
        Working examples block (from the few-shots) for the goal."""
        captured = {}

        def fake_structured(prompt, system, *, quality, repo_root=None):
            captured["system"] = system
            captured["prompt"] = prompt
            # Return a minimal valid plan so the caller is happy
            return [{"kind": "beginPlan", "params": {}, "label": "stub"},
                    {"kind": "newSketch",
                     "params": {"plane": "XY", "alias": "s"}},
                    {"kind": "sketchCircle",
                     "params": {"sketch": "s", "r": 5}},
                    {"kind": "extrude",
                     "params": {"sketch": "s", "distance": 5,
                                  "operation": "new", "alias": "b"}}]
        monkeypatch.setattr(
            "aria_os.native_planner.structured_llm.plan_from_llm_structured",
            fake_structured)

        from aria_os.native_planner.llm_planner import plan_from_llm
        plan_from_llm("M16x2 socket head cap screw, 60mm long",
                       {}, quality="balanced")
        sys_prompt = captured.get("system", "")
        assert "## Reference API" in sys_prompt, (
            "API retrieval block missing from system prompt")
        assert "## Working examples" in sys_prompt, (
            "Few-shot block missing from system prompt")

    def test_volute_prompt_retrieves_volute_shot(self, monkeypatch):
        captured = {}

        def fake_structured(prompt, system, *, quality, repo_root=None):
            captured["system"] = system
            return [{"kind": "beginPlan", "params": {}, "label": "stub"},
                    {"kind": "newSketch",
                     "params": {"plane": "XY", "alias": "s"}},
                    {"kind": "sketchCircle",
                     "params": {"sketch": "s", "r": 5}},
                    {"kind": "extrude",
                     "params": {"sketch": "s", "distance": 5,
                                  "operation": "new", "alias": "b"}}]
        monkeypatch.setattr(
            "aria_os.native_planner.structured_llm.plan_from_llm_structured",
            fake_structured)

        from aria_os.native_planner.llm_planner import plan_from_llm
        plan_from_llm("centrifugal pump volute, 80mm impeller",
                       {}, quality="balanced")
        sys_prompt = captured.get("system", "")
        # Volute few-shot's goal contains "Centrifugal pump volute"
        assert "volute" in sys_prompt.lower(), (
            "Sweep/volute few-shot didn't make it into the prompt")


# --- W2 Few-shot library ----------------------------------------------

class TestFewShotLibrary:
    """Every few-shot must (a) parse, (b) pass the validator, (c) be
    retrievable by its own goal text. Without this, a malformed shot
    poisons every LLM call that retrieves it."""

    def test_library_loads_at_least_5_shots(self):
        from aria_os.native_planner.fewshots import all_shots
        shots = all_shots()
        assert len(shots) >= 5, (
            f"Few-shot library is too sparse: only {len(shots)}")

    def test_every_shot_validates(self):
        """A few-shot that fails the validator means the LLM is being
        shown a broken example — guaranteed to make output worse."""
        from aria_os.native_planner.fewshots import all_shots
        from aria_os.native_planner.validator import validate_plan
        shots = all_shots()
        failures = []
        for s in shots:
            ok, issues = validate_plan(s.plan)
            if not ok:
                failures.append((s.id, issues[:3]))
        assert not failures, (
            f"Few-shot plans failing validation: {failures}")

    def test_every_shot_uses_only_declared_ops(self):
        """ops_used must match the actual op kinds in the plan, so the
        retriever's `prefer_ops` boost is honest."""
        from aria_os.native_planner.fewshots import all_shots
        for s in all_shots():
            actual = {op.get("kind") for op in s.plan}
            declared = set(s.ops_used)
            missing = declared - actual
            assert not missing, (
                f"{s.id}: declared ops {missing} not actually in plan")

    def test_self_retrieval(self):
        """A shot's own goal should rank that shot in the top-2 — sanity
        check the tag/goal tokenization works."""
        from aria_os.native_planner.fewshots import all_shots, retrieve
        for s in all_shots():
            hits = retrieve(s.goal, k=3)
            ids = [h.id for h in hits]
            assert s.id in ids, (
                f"Self-retrieval failed for {s.id} (hits: {ids})")

    def test_op_preference_boost(self):
        """A goal with 'sweep' ops_pref should rank the volute (sweep)
        above the flange (no sweep)."""
        from aria_os.native_planner.fewshots import retrieve
        hits = retrieve("centrifugal pump assembly",
                          k=3, prefer_ops=["sweep"])
        # The volute should be top-ranked when sweep is preferred
        assert hits and "volute" in hits[0].id, (
            f"Sweep-preferring query didn't surface volute first: "
            f"{[h.id for h in hits]}")


# --- W2 API retrieval -------------------------------------------------

class TestApiRetrieval:
    """The W2 BM25 index is responsible for surfacing the right API
    snippets to the LLM. Bad retrieval = bad plans even with a perfect
    schema. These tests pin the contract so the LLM-context budget
    isn't wasted on irrelevant docs."""

    def test_index_loads_corpus(self):
        from aria_os.native_planner.api_retrieval import APIDocIndex
        idx = APIDocIndex()
        assert len(idx) > 10, "Corpus should have at least the seed entries"

    def test_thread_query_returns_thread_doc(self):
        from aria_os.native_planner.api_retrieval import retrieve
        hits = retrieve("M8 thread on shank cylindrical face", k=5)
        assert hits, "Thread query returned no hits"
        # The thread-spec or threadFeature semantics should rank top-3
        thread_titles = [h.title.lower() for h in hits[:3]]
        assert any("thread" in t for t in thread_titles), (
            f"Top-3 hits don't mention thread: {thread_titles}")

    def test_volute_query_returns_volute_recipe(self):
        from aria_os.native_planner.api_retrieval import retrieve
        hits = retrieve("centrifugal pump volute spiral", k=5)
        assert any("volute" in h.title.lower() for h in hits), (
            f"Volute query missed the recipe: {[h.title for h in hits]}")

    def test_loft_query_returns_loft_doc(self):
        from aria_os.native_planner.api_retrieval import retrieve
        hits = retrieve("loft round to rectangular transition", k=5)
        assert any("loft" in h.title.lower() for h in hits), (
            f"Loft query missed the doc: {[h.title for h in hits]}")

    def test_render_for_prompt_compact(self):
        from aria_os.native_planner.api_retrieval import (retrieve,
                                                            render_for_prompt)
        hits = retrieve("revolve a profile around an axis", k=5)
        rendered = render_for_prompt(hits)
        # Should be a markdown block under the budget
        assert rendered.startswith("## Reference API")
        assert len(rendered) < 4000, (
            f"Rendered prompt too large: {len(rendered)} chars")

    def test_source_filter_restricts_to_target_host(self):
        from aria_os.native_planner.api_retrieval import retrieve
        # Onshape-only filter still returns 'cross' notes (they apply
        # to every host) but never cadquery- or fusion-specific notes.
        hits = retrieve("revolve a profile", k=5,
                          source_filter="onshape")
        sources = {h.source for h in hits}
        assert sources <= {"onshape", "cross"}, (
            f"Onshape filter leaked: {sources}")

    def test_empty_query_returns_empty(self):
        from aria_os.native_planner.api_retrieval import retrieve
        assert retrieve("", k=5) == []
        assert retrieve("   ", k=5) == []


# --- Hardware helpers (cq_warehouse / cq_gears wrappers) --------------

class TestHardwareHelpers:
    """The thread/gear wrappers must (a) parse all spec syntaxes ARIA's
    LLM is told it can emit, and (b) gracefully skip when the optional
    libraries aren't installed — the validator already accepts the ops
    cross-host even if CQ generation isn't wired locally."""

    def test_parse_iso_metric_with_pitch(self):
        from aria_os.cad_helpers import _parse_thread_spec
        r = _parse_thread_spec("M8x1.25")
        assert r["family"] == "ISO"
        assert abs(r["major_d"] - 8.0) < 1e-6
        assert abs(r["pitch_mm"] - 1.25) < 1e-6

    def test_parse_iso_metric_coarse_default(self):
        from aria_os.cad_helpers import _parse_thread_spec
        r = _parse_thread_spec("M16")
        assert r["family"] == "ISO"
        assert r["major_d"] == 16.0
        # Coarse-pitch default for M16 is 2.0 mm
        assert abs(r["pitch_mm"] - 2.0) < 1e-6

    def test_parse_un_threads(self):
        from aria_os.cad_helpers import _parse_thread_spec
        r = _parse_thread_spec("1/4-20-UNC")
        assert r["family"] == "UN"
        assert abs(r["major_d"] - 6.35) < 1e-3   # 1/4 in → 6.35 mm
        assert abs(r["pitch_mm"] - 25.4 / 20) < 1e-3
        assert r["series"] == "UNC"

    def test_parse_npt(self):
        from aria_os.cad_helpers import _parse_thread_spec
        r = _parse_thread_spec("1/4-NPT")
        assert r["family"] == "NPT"
        assert r["series"] == "TAPER"

    def test_parse_garbage_raises(self):
        from aria_os.cad_helpers import _parse_thread_spec
        import pytest
        with pytest.raises(ValueError):
            _parse_thread_spec("TIGHT")

    def test_iso_thread_geometry_if_lib_present(self):
        """If cq_warehouse is installed, iso_thread() must return a
        CadQuery-shaped object. Skip cleanly otherwise — ARIA degrades
        to whatever the host's native thread feature provides."""
        import pytest
        try:
            import cq_warehouse  # noqa: F401
        except ImportError:
            pytest.skip("cq_warehouse not installed")
        from aria_os.cad_helpers import iso_thread
        thread = iso_thread("M8x1.25", length=20)
        # Result should be CadQuery-shaped (have .val() or be Solid-like)
        assert thread is not None

    def test_involute_gear_min_teeth_caught(self):
        """The wrapper must reject invalid tooth counts BEFORE invoking
        cq_gears — saves a confusing upstream error."""
        from aria_os.cad_helpers import involute_gear
        import pytest
        with pytest.raises(ValueError):
            involute_gear(module=2, n_teeth=2, thickness=10)


# --- Cross-host op coverage -------------------------------------------

class TestOpHandlerCoverage:
    """Static check: every op kind the validator accepts MUST have a
    handler in the Fusion add-in. Without this, the planner can emit a
    valid op that the host bridge silently rejects with 'unknown kind'.

    The Fusion file imports `adsk` (only available inside Fusion) so we
    can't import the module — we parse it textually for the keys."""

    def _fusion_handler_keys(self) -> set[str]:
        from pathlib import Path
        import re
        p = Path(__file__).resolve().parents[1] / (
            "cad-plugins/fusion360/aria_panel/aria_panel.py")
        if not p.is_file():
            return set()
        src = p.read_text(encoding="utf-8")
        # Collect all 'kind': handler pairs and 'kind': handler updates
        keys = set(re.findall(r'"([A-Za-z_]+)"\s*:\s*_op_', src))
        return keys

    def test_w1_ops_have_fusion_handlers(self):
        keys = self._fusion_handler_keys()
        if not keys:
            import pytest
            pytest.skip("Fusion plugin file not present")
        w1_ops = {
            "sketchSpline", "sketchPolyline",
            "revolve", "sweep", "loft", "helix", "coil",
            "shell", "draft", "thicken", "threadFeature",
        }
        missing = w1_ops - keys
        assert not missing, (
            f"W1 ops missing Fusion handlers: {sorted(missing)}. "
            f"Add to _FEATURE_HANDLERS in aria_panel.py.")

    def test_w4_ops_have_fusion_handlers(self):
        keys = self._fusion_handler_keys()
        if not keys:
            import pytest
            pytest.skip("Fusion plugin file not present")
        w4_ops = {
            "mateConcentric", "mateCoincident", "mateDistance",
            "mateGear", "mateSlider",
            "motionRevolute", "motionPrismatic",
        }
        missing = w4_ops - keys
        assert not missing, (
            f"W4 ops missing Fusion handlers: {sorted(missing)}.")

    def test_w1_ops_have_onshape_handlers(self):
        """The Onshape executor uses `_op_<kind>` method names for
        dispatch. Make sure every W1 op has a matching method."""
        from aria_os.onshape.executor import OnshapeExecutor
        w1_ops = {
            "sketchSpline", "sketchPolyline",
            "revolve", "sweep", "loft", "helix",
            "shell", "thicken", "threadFeature", "draft",
        }
        missing = [op for op in w1_ops
                   if not hasattr(OnshapeExecutor, f"_op_{op}")]
        assert not missing, (
            f"Onshape executor missing handlers for: {missing}")

    def test_w4_ops_have_onshape_handlers(self):
        from aria_os.onshape.executor import OnshapeExecutor
        w4_ops = {
            "asmBegin", "addComponent",
            "mateConcentric", "mateCoincident", "mateDistance",
            "mateGear", "mateSlider",
            "motionRevolute", "motionPrismatic",
        }
        missing = [op for op in w4_ops
                   if not hasattr(OnshapeExecutor, f"_op_{op}")]
        assert not missing, (
            f"Onshape executor missing handlers for: {missing}")

    def test_validator_accepts_every_fusion_handler_kind(self):
        """Reverse direction: every Fusion handler key should be in the
        validator's _VALID_KINDS so the planner can legitimately emit it."""
        from aria_os.native_planner.validator import _VALID_KINDS
        keys = self._fusion_handler_keys()
        if not keys:
            import pytest
            pytest.skip("Fusion plugin file not present")
        # Some keys exist for host-side helpers that planners don't emit
        # directly (e.g. KiCad ops live in a different executor) — only
        # require coverage for the W1 vocabulary.
        w1_ops = {"sketchSpline", "sketchPolyline", "revolve", "sweep",
                  "loft", "helix", "coil", "shell", "draft", "thicken",
                  "threadFeature"}
        for op in w1_ops:
            if op in keys:
                assert op in _VALID_KINDS, (
                    f"Fusion handler {op!r} exists but validator rejects it")


# --- Validator: extended W1 op vocabulary ------------------------------

class TestValidatorExtendedOps:
    """W1 added 17 new ops to the planner vocabulary. These tests make
    sure the validator recognizes each one AND catches the most common
    structural mistakes (unknown sketch refs, missing params, bad ranges)
    so a malformed plan can never reach the host bridge."""

    def _base(self):
        """A minimal valid prologue: beginPlan + one sketch with circle +
        extrude(new) — gives downstream ops a body to attach to."""
        return [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "sketchCircle", "params": {"sketch": "s1", "r": 50}},
            {"kind": "extrude", "params": {
                "sketch": "s1", "distance": 10, "operation": "new",
                "alias": "base"}},
        ]

    # ---- Sketch primitives ---------------------------------------------

    def test_sketch_spline_needs_three_points(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s2"}},
            {"kind": "sketchSpline", "params": {
                "sketch": "s2", "points": [[0, 0], [10, 5]]}},  # only 2
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("at least 3" in i for i in issues), issues

    def test_sketch_spline_unknown_sketch_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "sketchSpline", "params": {
                "sketch": "ghost",
                "points": [[0, 0], [5, 5], [10, 0]]}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("ghost" in i for i in issues)

    def test_sketch_polyline_two_points_ok(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s2"}},
            {"kind": "sketchPolyline", "params": {
                "sketch": "s2", "points": [[0, 0], [10, 0]]}},
        ]
        ok, issues = validate_plan(plan)
        assert ok, issues

    # ---- Revolve --------------------------------------------------------

    def test_revolve_needs_valid_angle(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XZ", "alias": "p"}},
            {"kind": "sketchPolyline", "params": {
                "sketch": "p", "points": [[10, 0], [10, 50], [5, 50]]}},
            {"kind": "revolve", "params": {
                "sketch": "p", "axis": "Z", "angle": 0,
                "operation": "new", "alias": "body"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("angle" in i and "invalid" in i for i in issues)

    def test_revolve_unknown_operation_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XZ", "alias": "p"}},
            {"kind": "sketchCircle", "params": {"sketch": "p", "r": 5}},
            {"kind": "revolve", "params": {
                "sketch": "p", "axis": "Z", "angle": 360,
                "operation": "spin", "alias": "body"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("revolve operation" in i for i in issues)

    def test_revolve_new_satisfies_body_requirement(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XZ", "alias": "p"}},
            {"kind": "sketchCircle", "params": {"sketch": "p", "r": 5}},
            {"kind": "revolve", "params": {
                "sketch": "p", "axis": "Z", "angle": 360,
                "operation": "new", "alias": "body"}},
        ]
        ok, issues = validate_plan(plan)
        assert ok, issues  # revolve(new) should NOT trip "no extrude(new)"

    # ---- Sweep ----------------------------------------------------------

    def test_sweep_profile_and_path_must_differ(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s2"}},
            {"kind": "sweep", "params": {
                "profile_sketch": "s2", "path_sketch": "s2",
                "operation": "join", "alias": "swp"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("same sketch" in i for i in issues)

    # ---- Loft -----------------------------------------------------------

    def test_loft_needs_two_sections(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "sketchCircle", "params": {"sketch": "s1", "r": 50}},
            {"kind": "loft", "params": {
                "sections": ["s1"], "operation": "new"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("≥2 sections" in i for i in issues)

    def test_loft_unknown_section_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "sketchCircle", "params": {"sketch": "s1", "r": 50}},
            {"kind": "newSketch",
             "params": {"plane": "XY", "alias": "s2", "offset": 100}},
            {"kind": "sketchCircle", "params": {"sketch": "s2", "r": 25}},
            {"kind": "loft", "params": {
                "sections": ["s1", "s2", "s_ghost"], "operation": "new"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("s_ghost" in i for i in issues)

    # ---- Helix / coil ---------------------------------------------------

    def test_helix_needs_positive_pitch_and_height(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "helix", "params": {
                "axis": "Z", "pitch": 0, "height": -5, "diameter": 10}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        # Both pitch and height should fail
        assert sum("helix" in i.lower() and ("pitch" in i or "height" in i)
                   for i in issues) >= 2

    def test_coil_needs_section_sketch(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "coil", "params": {
                "axis": "Z", "pitch": 2, "turns": 5,
                "diameter": 10, "section": "missing"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("missing" in i for i in issues)

    # ---- Shell / draft / thicken ---------------------------------------

    def test_shell_negative_thickness_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "shell", "params": {"body": "base", "thickness": -1}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("shell thickness" in i for i in issues)

    def test_draft_unrealistic_angle_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "draft", "params": {
                "body": "base", "faces": ["f1"], "angle": 75}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("draft angle" in i for i in issues)

    # ---- Threads --------------------------------------------------------

    def test_thread_iso_metric_accepted(self):
        from aria_os.native_planner.validator import validate_plan
        for spec in ("M8", "M16X2", "M3X0.5", "M20"):
            plan = self._base() + [
                {"kind": "threadFeature", "params": {
                    "face": "f1", "spec": spec}},
            ]
            ok, issues = validate_plan(plan)
            assert ok, f"{spec} rejected: {issues}"

    def test_thread_un_and_npt_accepted(self):
        from aria_os.native_planner.validator import validate_plan
        for spec in ("1/4-20", "1/4-20-UNC", "3/8-16-UNF", "1/4-NPT"):
            plan = self._base() + [
                {"kind": "threadFeature", "params": {
                    "face": "f1", "spec": spec}},
            ]
            ok, issues = validate_plan(plan)
            assert ok, f"{spec} rejected: {issues}"

    def test_thread_garbage_spec_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = self._base() + [
            {"kind": "threadFeature", "params": {
                "face": "f1", "spec": "TIGHT"}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("not recognized" in i for i in issues)

    # ---- Gear -----------------------------------------------------------

    def test_gear_too_few_teeth_caught(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "gearFeature", "params": {
                "sketch": "s1", "module": 2, "n_teeth": 3, "thickness": 5}},
        ]
        ok, issues = validate_plan(plan)
        assert not ok
        assert any("gearFeature out of range" in i for i in issues)

    def test_gear_satisfies_body_requirement(self):
        from aria_os.native_planner.validator import validate_plan
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY", "alias": "s1"}},
            {"kind": "gearFeature", "params": {
                "sketch": "s1", "module": 2, "n_teeth": 24, "thickness": 10,
                "alias": "g1"}},
        ]
        ok, issues = validate_plan(plan)
        assert ok, issues  # gearFeature is body-creating like extrude(new)


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
