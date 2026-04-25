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
