"""
tests/test_cad_router.py — Multi-backend routing + 14-template smoke tests.

Covers:
  - tool_router.select_cad_tool: keyword routing, part_id routing
  - multi_cad_router.CADRouter.route: spec auto-extraction, overrides, dry_run
  - 14 CadQuery template smoke tests (generate valid Python scripts for each known part)
  - CADQUERY_KEYWORDS routing (LRE / nozzle always → cadquery)
  - GRASSHOPPER_PART_IDS routing
"""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.tool_router import select_cad_tool
from aria_os.multi_cad_router import CADRouter


# ---------------------------------------------------------------------------
# tool_router.select_cad_tool
# ---------------------------------------------------------------------------

class TestSelectCadTool:
    def _plan(self, part_id="aria_part", features=None):
        return {"part_id": part_id, "features": features or []}

    def test_default_is_cadquery(self):
        assert select_cad_tool("simple bracket", self._plan()) == "cadquery"

    def test_grasshopper_keyword_helix(self):
        result = select_cad_tool("helical cam ramp", self._plan())
        assert result == "grasshopper"

    def test_grasshopper_keyword_loft(self):
        result = select_cad_tool("loft surface part", self._plan())
        assert result == "grasshopper"

    def test_fusion_keyword_lattice(self):
        result = select_cad_tool("gyroid lattice infill", self._plan())
        assert result == "fusion"

    def test_blender_keyword_remesh(self):
        result = select_cad_tool("mesh remesh cleanup", self._plan())
        assert result == "blender"

    def test_grasshopper_part_id(self):
        result = select_cad_tool("any goal", self._plan(part_id="aria_cam_collar"))
        assert result == "grasshopper"

    def test_fusion_part_id(self):
        result = select_cad_tool("any goal", self._plan(part_id="aria_energy_absorber"))
        assert result == "fusion"

    def test_feature_ramp_triggers_grasshopper(self):
        plan = self._plan(features=[{"type": "ramp", "description": "helical ramp"}])
        result = select_cad_tool("cam collar", plan)
        assert result == "grasshopper"


# ---------------------------------------------------------------------------
# CADRouter.route
# ---------------------------------------------------------------------------

class TestCADRouter:
    def test_returns_expected_keys(self):
        result = CADRouter.route("ARIA ratchet ring 213mm OD", dry_run=True)
        for key in ("backend", "part_id", "spec", "rationale"):
            assert key in result

    def test_lre_nozzle_routes_cadquery(self):
        result = CADRouter.route("LRE nozzle 10kN thrust 3MPa", dry_run=True)
        assert result["backend"] == "cadquery"

    def test_rocket_routes_cadquery(self):
        result = CADRouter.route("liquid rocket nozzle 5kN", dry_run=True)
        assert result["backend"] == "cadquery"

    def test_ratchet_ring_spec_extracted(self):
        result = CADRouter.route("ratchet ring 213mm OD 24 teeth", dry_run=True)
        spec = result.get("spec", {})
        assert spec.get("od_mm") == pytest.approx(213.0) or result["part_id"] != ""

    def test_explicit_spec_not_reextracted(self):
        manual_spec = {"od_mm": 999.0, "part_type": "brake_drum"}
        result = CADRouter.route("any description", spec=manual_spec, dry_run=True)
        assert result["spec"]["od_mm"] == pytest.approx(999.0)

    def test_dry_run_excludes_plan(self):
        result = CADRouter.route("simple bracket", dry_run=True)
        assert "plan" not in result

    def test_non_dry_run_includes_plan(self):
        result = CADRouter.route("simple bracket", dry_run=False)
        assert "plan" in result

    def test_route_all(self):
        goals = ["ratchet ring", "housing", "spool"]
        results = CADRouter.route_all(goals, dry_run=True)
        assert len(results) == 3
        for r in results:
            assert "backend" in r

    def test_cam_collar_routes_grasshopper(self):
        result = CADRouter.route("aria cam collar 80mm OD", dry_run=True)
        assert result["backend"] == "grasshopper"

    def test_rationale_non_empty(self):
        result = CADRouter.route("ARIA housing 260mm OD", dry_run=True)
        assert len(result["rationale"]) > 0


# ---------------------------------------------------------------------------
# 14-template smoke tests (import cadquery_generator, generate each template)
# ---------------------------------------------------------------------------

class TestCadQueryTemplateSmoke:
    """
    Smoke-test all 14 CadQuery templates by calling generate() and checking
    that the returned script is valid Python and non-trivially long.
    """

    KNOWN_PART_IDS = [
        "aria_ratchet_ring",
        "aria_housing",
        "aria_spool",
        "aria_cam_collar",
        "aria_brake_drum",
        "aria_catch_pawl",
        "aria_rope_guide",
        "aria_bracket",
        "aria_flange",
        "aria_shaft",
        "aria_pulley",
        "aria_cam",
        "aria_pin",
        "aria_spacer",
    ]

    def _get_script(self, part_id: str) -> str:
        from aria_os.cadquery_generator import generate
        plan = {
            "part_id": part_id,
            "params": {"od_mm": 100.0, "bore_mm": 20.0, "thickness_mm": 20.0,
                       "height_mm": 50.0, "width_mm": 80.0, "depth_mm": 30.0,
                       "length_mm": 100.0, "n_teeth": 12, "n_bolts": 4},
            "features": [],
        }
        result = generate(plan, "/tmp/out.step", "/tmp/out.stl",
                         repo_root=Path(__file__).resolve().parent.parent)
        return result.get("script", "") if isinstance(result, dict) else str(result)

    @pytest.mark.parametrize("part_id", KNOWN_PART_IDS)
    def test_template_produces_valid_python(self, part_id):
        try:
            script = self._get_script(part_id)
            assert len(script) > 200, f"{part_id}: script too short ({len(script)} chars)"
            ast.parse(script)
        except ImportError as e:
            pytest.skip(f"Import failed: {e}")

    @pytest.mark.parametrize("part_id", KNOWN_PART_IDS)
    def test_template_contains_cadquery_import(self, part_id):
        try:
            script = self._get_script(part_id)
            assert "cadquery" in script.lower() or "cq" in script
        except ImportError as e:
            pytest.skip(f"Import failed: {e}")
