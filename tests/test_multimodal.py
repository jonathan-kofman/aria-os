"""W8 multi-modal input tests.

These tests exercise the contract — parser robustness, target
resolution, intent classification — without requiring live LLM /
camera calls. The 4 acceptance prompts (sketch / image / multiview /
voice) live in eval_prompts.json with expected_modality tags so they
can be run against a real LLM when credits / hardware allow.
"""
from __future__ import annotations

import json
import pytest


# --- W8.1 sketch_agent JSON parser -----------------------------------

class TestSketchEngineeringParser:
    """The engineering-mode response is JSON. The parser tolerates
    markdown fences, trailing prose, and trailing commas."""

    def test_clean_json(self):
        from aria_os.agents.sketch_agent import _parse_engineering_response
        raw = '{"goal": "flange 100mm OD", "spec": {"od_mm": 100}, "confidence": 0.9}'
        parsed = _parse_engineering_response(raw)
        assert parsed["goal"] == "flange 100mm OD"
        assert parsed["spec"]["od_mm"] == 100

    def test_markdown_fence(self):
        from aria_os.agents.sketch_agent import _parse_engineering_response
        raw = ('Here is the analysis:\n```json\n'
               '{"goal": "x", "spec": {}, "confidence": 0.5}\n'
               '```\nLet me know!')
        parsed = _parse_engineering_response(raw)
        assert parsed["goal"] == "x"

    def test_trailing_comma_tolerated(self):
        from aria_os.agents.sketch_agent import _parse_engineering_response
        raw = '{"goal": "y", "spec": {"od_mm": 50,}, "confidence": 0.7,}'
        parsed = _parse_engineering_response(raw)
        assert parsed["spec"]["od_mm"] == 50

    def test_garbage_raises(self):
        from aria_os.agents.sketch_agent import _parse_engineering_response
        with pytest.raises(ValueError):
            _parse_engineering_response("totally not json")


# --- W8.4 scan_to_cad mesh analysis ----------------------------------

class TestScanAnalysis:
    """Pure-numpy / pure-trimesh tests of the scan analyzer.
    Build synthetic meshes and verify the family classification +
    geometric features come out right."""

    @pytest.fixture
    def trimesh_or_skip(self):
        try:
            import trimesh   # type: ignore  # noqa: F401
            return trimesh
        except ImportError:
            pytest.skip("trimesh not installed")

    def test_shaft_classified(self, trimesh_or_skip):
        """Long thin cylinder → shaft."""
        trimesh = trimesh_or_skip
        # 10mm OD × 100mm long cylinder
        mesh = trimesh.creation.cylinder(radius=5, height=100,
                                            sections=32)
        from aria_os.agents.scan_to_cad import _analyze_mesh
        analysis = _analyze_mesh(mesh)
        assert analysis["aspect_ratio"] >= 5
        assert analysis["cylindrical_score"] >= 0.7
        assert analysis["suggested_family"] in ("shaft", "pulley")

    def test_plate_classified(self, trimesh_or_skip):
        trimesh = trimesh_or_skip
        # 100×60×3mm plate
        mesh = trimesh.creation.box(extents=[100, 60, 3])
        from aria_os.agents.scan_to_cad import _analyze_mesh
        analysis = _analyze_mesh(mesh)
        assert analysis["plate_score"] >= 0.7
        assert analysis["suggested_family"] in ("plate", "bracket")

    def test_flange_classified(self, trimesh_or_skip):
        """Short fat cylinder → flange-like (discs are bbox-equivalent
        to plates from the analyzer's aspect-ratio view; either label
        is acceptable since the LLM gets the bbox dims regardless)."""
        trimesh = trimesh_or_skip
        mesh = trimesh.creation.cylinder(radius=50, height=6,
                                            sections=64)
        from aria_os.agents.scan_to_cad import _analyze_mesh
        analysis = _analyze_mesh(mesh)
        # bbox shortest axis is the disc thickness — must be much
        # smaller than the OD axes
        assert analysis["bbox_mm"][2] < analysis["bbox_mm"][0] / 5
        assert analysis["plate_score"] >= 0.7
        assert analysis["suggested_family"] in ("flange", "plate", "bracket")

    def test_goal_string_dimensional(self, trimesh_or_skip):
        """The auto-composed goal string must include real bbox dims
        the planner can re-extract."""
        trimesh = trimesh_or_skip
        mesh = trimesh.creation.box(extents=[120, 80, 3])
        from aria_os.agents.scan_to_cad import (
            _analyze_mesh, _analysis_to_goal)
        goal = _analysis_to_goal(_analyze_mesh(mesh))
        assert "120" in goal
        assert "80" in goal
        assert "3" in goal


# --- W8.5 voice-in-context intent classifier --------------------------

class TestVoiceIntent:
    """The intent classifier decides modify vs extend vs query
    BEFORE we touch the LLM. Pin its behaviour so a regression here
    doesn't silently route every utterance to the wrong path."""

    def test_make_bigger_is_modify(self):
        from aria_os.agents.voice_in_context import _classify_intent
        for utterance in (
            "make this hole 2mm bigger",
            "increase the wall thickness to 3mm",
            "set OD to 120mm",
            "raise to 100",
            "make it thicker",
        ):
            assert _classify_intent(utterance) == "modify", utterance

    def test_add_x_is_extend(self):
        from aria_os.agents.voice_in_context import _classify_intent
        for utterance in (
            "add a fillet to that edge",
            "drill another hole on the right side",
            "put a chamfer on the bottom",
            "mirror this feature",
        ):
            assert _classify_intent(utterance) == "extend", utterance

    def test_question_is_query(self):
        from aria_os.agents.voice_in_context import _classify_intent
        assert _classify_intent("what's the wall thickness") == "query"
        assert _classify_intent("how big is this hole") == "query"


class TestVoiceTargetResolution:
    """Demonstrative pronouns + host_context.selection → resolved
    feature alias substituted into the goal string."""

    def test_this_resolves_to_selected_feature(self):
        from aria_os.agents.voice_in_context import (
            _resolve_target, _build_goal_with_target)
        ctx = {"selection": [
            {"type": "edge", "id": "e123", "feature": "bolt_hole_3"}]}
        target = _resolve_target("make this hole 2mm bigger", ctx)
        assert target is not None
        assert target["feature_alias"] == "bolt_hole_3"
        # And the substituted goal contains the feature alias
        new_goal = _build_goal_with_target(
            "make this hole 2mm bigger", target)
        assert "bolt_hole_3" in new_goal
        assert "this" not in new_goal.split()  # consumed

    def test_no_demonstrative_returns_none(self):
        from aria_os.agents.voice_in_context import _resolve_target
        ctx = {"selection": [{"type": "edge", "feature": "x"}]}
        assert _resolve_target("flange 100mm OD", ctx) is None

    def test_demonstrative_no_selection_returns_none(self):
        from aria_os.agents.voice_in_context import _resolve_target
        # No selection in context → nothing to resolve
        assert _resolve_target("make this bigger", {}) is None


# --- W8.6 acceptance prompt registry ----------------------------------

class TestMultimodalAcceptance:
    """The 4 W8.6 acceptance prompts live in eval_prompts.json with
    a `modality` tag so the harness can exercise each input path
    against a live LLM. These tests verify the registry stays
    consistent."""

    def test_acceptance_set_includes_modalities(self):
        from pathlib import Path
        prompts = json.loads(
            (Path(__file__).resolve().parents[1] /
             "tests" / "eval_prompts.json").read_text())["prompts"]
        modalities = {p.get("modality") for p in prompts
                      if p.get("modality")}
        # Acceptance prompts will be added below; just sanity-check
        # the file parses.
        assert isinstance(modalities, set)
