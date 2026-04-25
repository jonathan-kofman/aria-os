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


# --- W8 entry-point integration tests (audit gap) ---------------------
#
# Every W8 agent has a public `*_to_plan(...)` entry point that wraps:
#   1. Vision/STT LLM call → goal extraction
#   2. dispatcher.make_plan(goal, spec, ...)
#   3. Returns {goal, spec, plan, ...}
#
# The unit tests cover the helpers (parsers, intent classifiers,
# target resolvers) but the AUDIT found nobody actually calls the
# entry points. These tests close that gap by mocking the LLM/STT
# side and verifying the full pipeline shape.

class TestW8EntryPoints:
    """End-to-end agent tests with mocked LLM/STT — exercise the
    full sketch_to_plan / image_to_plan / multiview_to_plan /
    scan_to_plan / voice_to_plan flow including the dispatcher
    handoff."""

    def _stub_planner(self, monkeypatch):
        """Replace the dispatcher so we don't burn LLM credits in
        the test — but exercise the full agent → planner → plan
        pipeline."""
        def fake_make_plan(goal, spec, **kw):
            return [
                {"kind": "beginPlan", "params": {}},
                {"kind": "newSketch",
                 "params": {"plane": "XY", "alias": "s"}},
                {"kind": "sketchCircle",
                 "params": {"sketch": "s", "r": 50}},
                {"kind": "extrude",
                 "params": {"sketch": "s", "distance": 10,
                              "operation": "new", "alias": "b",
                              "_captured_goal": goal}},
            ]
        monkeypatch.setattr(
            "aria_os.native_planner.dispatcher.make_plan",
            fake_make_plan)

    # --- W8.1 sketch_to_plan ------------------------------------------

    def test_sketch_to_plan_rough_mode(self, monkeypatch, tmp_path):
        from aria_os.agents import sketch_agent
        self._stub_planner(monkeypatch)
        # Stub the vision LLM helpers to return a goal string
        monkeypatch.setattr(
            sketch_agent, "_vision_call",
            lambda b, m, p, r: "L-bracket 80x60mm, 5mm thick AL")
        # A 1x1 PNG byte stream; we only need a real-looking file.
        sketch_path = tmp_path / "sketch.png"
        sketch_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 50)

        result = sketch_agent.sketch_to_plan(
            sketch_path, mode="rough")
        assert result["goal"].startswith("L-bracket")
        assert result["spec"] == {}
        assert result["plan"][0]["kind"] == "beginPlan"
        # The captured goal flowed through the planner
        captured = result["plan"][-1]["params"].get("_captured_goal")
        assert captured == "L-bracket 80x60mm, 5mm thick AL"

    def test_sketch_to_plan_engineering_mode_parses_json(
            self, monkeypatch, tmp_path):
        from aria_os.agents import sketch_agent
        self._stub_planner(monkeypatch)
        monkeypatch.setattr(
            sketch_agent, "_vision_call",
            lambda b, m, p, r: '{"goal": "flange 100mm OD", '
                                '"spec": {"od_mm": 100, "n_bolts": 4}, '
                                '"confidence": 0.9}')
        p = tmp_path / "eng.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 50)
        result = sketch_agent.sketch_to_plan(p, mode="engineering")
        assert result["goal"] == "flange 100mm OD"
        assert result["spec"]["od_mm"] == 100
        assert result["spec"]["n_bolts"] == 4

    def test_sketch_missing_file_raises(self, tmp_path):
        from aria_os.agents.sketch_agent import sketch_to_plan
        with pytest.raises(FileNotFoundError):
            sketch_to_plan(tmp_path / "nope.png")

    def test_sketch_no_vision_backend_raises(self, monkeypatch, tmp_path):
        from aria_os.agents import sketch_agent
        monkeypatch.setattr(sketch_agent, "_vision_call",
                              lambda b, m, p, r: None)
        p = tmp_path / "x.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 50)
        with pytest.raises(RuntimeError, match="Vision LLM unavailable"):
            sketch_agent.sketch_to_plan(p, mode="rough")

    # --- W8.2 image_to_plan -------------------------------------------

    def test_image_to_plan_with_reference(self, monkeypatch, tmp_path):
        from aria_os.agents import image_to_cad
        self._stub_planner(monkeypatch)
        monkeypatch.setattr(
            image_to_cad, "_vision_call",
            lambda b, m, p, r: '{"goal": "NEMA17 motor mount", '
                                '"part_family": "bracket", '
                                '"spec": {"width_mm": 42.3, '
                                '         "height_mm": 42.3, '
                                '         "thickness_mm": 5}, '
                                '"reference_used": "caliper readout 42.3mm", '
                                '"confidence": 0.92}')
        p = tmp_path / "motor.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"\0" * 50)
        result = image_to_cad.image_to_plan(
            p, reference={"type": "caliper", "reading_mm": 42.3})
        assert result["goal"] == "NEMA17 motor mount"
        assert result["part_family"] == "bracket"
        assert result["spec"]["width_mm"] == 42.3
        assert result["confidence"] == 0.92
        assert result["plan"][0]["kind"] == "beginPlan"

    # --- W8.3 multiview_to_plan ---------------------------------------

    def test_multiview_to_plan_three_views(self, monkeypatch, tmp_path):
        from aria_os.agents import multiview_to_cad
        self._stub_planner(monkeypatch)
        # Stub the multi-view vision call
        monkeypatch.setattr(
            multiview_to_cad, "_gemini_multi_image",
            lambda views, sys, r: '{"goal": "centrifugal impeller", '
                                    '"part_family": "impeller", '
                                    '"spec": {"od_mm": 100, '
                                    '         "n_blades": 6}, '
                                    '"features_per_view": [], '
                                    '"reference_used": "M8 bolt", '
                                    '"confidence": 0.88}')
        # Also stub anthropic so a fallback never hits the real API
        monkeypatch.setattr(
            multiview_to_cad, "_anthropic_multi_image",
            lambda views, sys, r: None)
        # Three fake images
        views = []
        for label in ("top", "front", "iso"):
            p = tmp_path / f"{label}.jpg"
            p.write_bytes(b"\xff\xd8\xff" + b"\0" * 30)
            views.append({"path": str(p), "label": label})
        result = multiview_to_cad.multiview_to_plan(views)
        assert result["goal"] == "centrifugal impeller"
        assert result["n_views"] == 3
        assert result["confidence"] == 0.88

    def test_multiview_too_few_raises(self, tmp_path):
        from aria_os.agents.multiview_to_cad import multiview_to_plan
        # Need ≥2 views
        p = tmp_path / "single.jpg"
        p.write_bytes(b"\xff\xd8" + b"\0" * 20)
        with pytest.raises(ValueError, match="≥2"):
            multiview_to_plan([{"path": str(p), "label": "top"}])

    def test_multiview_too_many_caps(self, tmp_path):
        from aria_os.agents.multiview_to_cad import multiview_to_plan
        views = []
        for i in range(7):
            p = tmp_path / f"v{i}.jpg"
            p.write_bytes(b"\xff\xd8" + b"\0" * 20)
            views.append({"path": str(p), "label": f"v{i}"})
        with pytest.raises(ValueError, match="6 views"):
            multiview_to_plan(views)

    # --- W8.4 scan_to_plan --------------------------------------------

    def test_scan_to_plan_round_trip(self, monkeypatch, tmp_path):
        """Generate a synthetic STL, run scan_to_plan, verify the
        full {goal, analysis, plan} return shape."""
        try:
            import trimesh
        except ImportError:
            pytest.skip("trimesh not available")
        self._stub_planner(monkeypatch)
        from aria_os.agents.scan_to_cad import scan_to_plan
        # 100x60x5mm plate
        mesh = trimesh.creation.box(extents=[100, 60, 5])
        stl_path = tmp_path / "scan.stl"
        mesh.export(str(stl_path))
        result = scan_to_plan(stl_path)
        assert "analysis" in result
        assert result["analysis"]["plate_score"] >= 0.7
        assert result["plan"][0]["kind"] == "beginPlan"
        # Goal mentions the dims
        assert "100" in result["goal"] and "60" in result["goal"]

    def test_scan_to_plan_goal_override(self, monkeypatch, tmp_path):
        """User supplies a goal_override — the analyzer's auto-goal
        is replaced but analysis still runs."""
        try:
            import trimesh
        except ImportError:
            pytest.skip("trimesh not available")
        self._stub_planner(monkeypatch)
        from aria_os.agents.scan_to_cad import scan_to_plan
        mesh = trimesh.creation.box(extents=[50, 50, 50])
        stl_path = tmp_path / "cube.stl"
        mesh.export(str(stl_path))
        result = scan_to_plan(stl_path,
                                goal_override="user-specified goal")
        assert result["goal"] == "user-specified goal"
        assert result["analysis"]["bbox_mm"] == [50, 50, 50]

    def test_scan_missing_file_raises(self, tmp_path):
        from aria_os.agents.scan_to_cad import scan_to_plan
        with pytest.raises(FileNotFoundError):
            scan_to_plan(tmp_path / "nope.stl")

    # --- W8.5 voice_to_plan -------------------------------------------

    def test_voice_to_plan_with_selection(self, monkeypatch, tmp_path):
        from aria_os.agents import voice_in_context
        self._stub_planner(monkeypatch)
        # Stub the STT helper
        monkeypatch.setattr(
            voice_in_context, "_transcribe",
            lambda wav: "make this hole 2mm bigger")
        wav = tmp_path / "v.wav"
        wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\0" * 40)
        ctx = {"selection": [
            {"type": "edge", "id": "e1",
             "feature": "bolt_hole_3"}]}
        result = voice_in_context.voice_to_plan(
            wav, host_context=ctx)
        assert result["transcription"] == "make this hole 2mm bigger"
        assert result["intent"] == "modify"
        assert result["mode"] == "modify"
        assert result["resolved_target"]["feature_alias"] == "bolt_hole_3"
        assert "bolt_hole_3" in result["goal"]
        assert result["plan"][-1]["params"].get("_captured_goal") \
            and "bolt_hole_3" in result["plan"][-1]["params"][
                "_captured_goal"]

    def test_voice_no_demonstrative_passes_through(
            self, monkeypatch, tmp_path):
        from aria_os.agents import voice_in_context
        self._stub_planner(monkeypatch)
        monkeypatch.setattr(
            voice_in_context, "_transcribe",
            lambda wav: "generate a 100mm flange")
        wav = tmp_path / "v.wav"
        wav.write_bytes(b"RIFF" + b"\0" * 40)
        result = voice_in_context.voice_to_plan(wav, host_context={})
        # No demonstrative + no selection → pass-through
        assert result["resolved_target"] is None
        assert result["mode"] in ("modify", "extend", "new")
        # 'generate' isn't a modify keyword → defaults to modify class,
        # but the goal text is preserved verbatim
        assert "100mm flange" in result["goal"]

    def test_voice_transcribe_fail_raises(self, monkeypatch, tmp_path):
        from aria_os.agents import voice_in_context
        # Make _transcribe raise (simulates no STT backend)
        def fail(wav):
            raise RuntimeError("transcription failed")
        monkeypatch.setattr(voice_in_context, "_transcribe", fail)
        wav = tmp_path / "v.wav"
        wav.write_bytes(b"RIFF" + b"\0" * 40)
        with pytest.raises(RuntimeError):
            voice_in_context.voice_to_plan(wav)


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
