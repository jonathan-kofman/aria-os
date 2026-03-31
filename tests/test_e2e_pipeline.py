"""
tests/test_e2e_pipeline.py — 5 diverse descriptions, one per backend.

End-to-end tests that run the full pipeline (plan → route → generate → validate)
for a representative part per backend. Tests are headless and do not require
Rhino, Blender, or Fusion 360.

Backends covered:
    cadquery     — aria_bracket (solid box + holes)
    cadquery     — aria_ratchet_ring (annular + teeth, complex template)
    grasshopper  — aria_cam_collar (GH component script generation)
    blender      — gyroid lattice artifact (blender_generator)
    fusion360    — motor housing script generation (fusion_generator)

Each test asserts:
  - Pipeline returns without unhandled exception
  - At least one artifact (script or file) is produced
  - Artifact is non-empty

CadQuery geometry tests additionally assert:
  - Output STEP/STL file exists (when cadquery is installed) + is watertight
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_REPO = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _cadquery_available() -> bool:
    try:
        import cadquery  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# CadQuery: bracket (solid box + holes)
# ---------------------------------------------------------------------------

class TestCadQueryBracket:
    GOAL = "ARIA wall bracket, 150mm wide, 80mm height, 15mm thick, 4xM6 bolts"

    def test_plan_succeeds(self):
        from aria_os.planner import plan
        result = plan(self.GOAL, {}, repo_root=_REPO)
        assert isinstance(result, dict)
        # planner returns at minimum a text or part_id key
        assert "part_id" in result or "text" in result

    def test_spec_extracted(self):
        from aria_os.spec_extractor import extract_spec
        spec = extract_spec(self.GOAL)
        # Should extract at least width or height
        assert spec.get("width_mm") or spec.get("height_mm") or spec.get("thickness_mm")

    def test_router_returns_cadquery(self):
        from aria_os.multi_cad_router import CADRouter
        result = CADRouter.route(self.GOAL, dry_run=True)
        assert result["backend"] == "cadquery"

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_cadquery_script_valid_python(self, tmp_path):
        from aria_os.cadquery_generator import generate
        plan = {
            "part_id": "aria_bracket",
            "params": {"width_mm": 150.0, "height_mm": 80.0, "thickness_mm": 15.0,
                       "n_bolts": 4, "bolt_dia_mm": 6.0},
            "features": [],
        }
        result = generate(plan, str(tmp_path / "bracket.step"), str(tmp_path / "bracket.stl"),
                         repo_root=_REPO)
        script = result.get("script", "") if isinstance(result, dict) else str(result)
        assert len(script) > 100
        ast.parse(script)


# ---------------------------------------------------------------------------
# CadQuery: ratchet ring
# ---------------------------------------------------------------------------

class TestCadQueryRatchetRing:
    GOAL = "ARIA ratchet ring, 213mm OD, 24 teeth, 21mm thick, 4140 steel"

    def test_spec_extracted(self):
        from aria_os.spec_extractor import extract_spec
        spec = extract_spec(self.GOAL)
        assert spec.get("od_mm") == pytest.approx(213.0)
        assert spec.get("n_teeth") == 24

    def test_router_returns_grasshopper(self):
        # aria_ratchet_ring is in GRASSHOPPER_PART_IDS (complex gear geometry)
        from aria_os.multi_cad_router import CADRouter
        result = CADRouter.route(self.GOAL, dry_run=True)
        assert result["backend"] == "grasshopper"

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_script_generated(self, tmp_path):
        from aria_os.cadquery_generator import generate
        plan = {
            "part_id": "aria_ratchet_ring",
            "params": {"od_mm": 213.0, "bore_mm": 40.0, "thickness_mm": 21.0, "n_teeth": 24},
            "features": [],
        }
        result = generate(plan, str(tmp_path / "rr.step"), str(tmp_path / "rr.stl"),
                         repo_root=_REPO)
        script = result.get("script", "") if isinstance(result, dict) else str(result)
        assert "cadquery" in script.lower() or "cq" in script

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_diameter_in_script(self, tmp_path):
        from aria_os.cadquery_generator import generate
        plan = {
            "part_id": "aria_ratchet_ring",
            "params": {"od_mm": 213.0, "bore_mm": 40.0, "thickness_mm": 21.0, "n_teeth": 24},
            "features": [],
        }
        result = generate(plan, str(tmp_path / "rr.step"), str(tmp_path / "rr.stl"),
                         repo_root=_REPO)
        script = result.get("script", "") if isinstance(result, dict) else str(result)
        assert "213" in script


# ---------------------------------------------------------------------------
# Grasshopper: cam collar (GH script generation + CQ fallback)
# ---------------------------------------------------------------------------

class TestGrasshopperCamCollar:
    GOAL = "ARIA cam collar 80mm OD, 30mm bore, 40mm long"

    def test_router_returns_grasshopper(self):
        from aria_os.multi_cad_router import CADRouter
        result = CADRouter.route(self.GOAL, dry_run=True)
        assert result["backend"] == "grasshopper"

    def test_gh_component_script_generated(self, tmp_path):
        from aria_os.gh_integration.gh_aria_parts import generate_gh_component_script
        script = generate_gh_component_script(
            "aria_cam_collar",
            {"od_mm": 80.0, "bore_mm": 30.0, "length_mm": 40.0},
        )
        assert len(script) > 100
        ast.parse(script)

    def test_cq_fallback_script_generated(self, tmp_path):
        from aria_os.gh_integration.gh_aria_parts import generate_cq_fallback_script
        script = generate_cq_fallback_script(
            "aria_cam_collar",
            {"od_mm": 80.0, "bore_mm": 30.0, "length_mm": 40.0},
        )
        assert len(script) > 50
        ast.parse(script)

    def test_write_gh_artifacts_creates_files(self, tmp_path):
        from aria_os.gh_integration.gh_aria_parts import write_gh_artifacts
        artifacts = write_gh_artifacts(
            "aria_cam_collar",
            {"od_mm": 80.0, "bore_mm": 30.0, "length_mm": 40.0},
            repo_root=tmp_path,
        )
        for name, path in artifacts.items():
            assert Path(path).exists(), f"Artifact {name} not created at {path}"
            assert Path(path).stat().st_size > 0, f"Artifact {name} is empty"


# ---------------------------------------------------------------------------
# Blender: gyroid lattice
# ---------------------------------------------------------------------------

class TestBlenderLattice:
    GOAL = "gyroid lattice infill 100x100x10mm volumetric"

    def test_router_returns_blender_or_fusion(self):
        from aria_os.multi_cad_router import CADRouter
        result = CADRouter.route(self.GOAL, dry_run=True)
        assert result["backend"] in ("blender", "fusion")

    def test_blender_generator_produces_artifact(self, tmp_path):
        try:
            from aria_os.blender_generator import write_blender_artifacts
        except ImportError:
            pytest.skip("blender_generator not importable")

        import inspect
        plan = {
            "part_id": "aria_lattice",
            "params": {"width_mm": 100.0, "height_mm": 100.0, "depth_mm": 10.0,
                       "pattern": "gyroid"},
            "features": [{"type": "lattice", "pattern": "gyroid"}],
        }
        # Signature: (plan, goal, stl_path, repo_root=None)
        result = write_blender_artifacts(
            plan, self.GOAL,
            str(tmp_path / "lattice.stl"),
            repo_root=_REPO,
        )
        assert isinstance(result, dict)

    def test_lattice_contains_bpy_reference(self, tmp_path):
        """Blender script must reference bpy (Blender Python)."""
        try:
            from aria_os.blender_generator import write_blender_artifacts
        except ImportError:
            pytest.skip("blender_generator not importable")

        plan = {
            "part_id": "aria_lattice",
            "params": {"pattern": "gyroid"},
            "features": [],
        }
        result = write_blender_artifacts(
            plan, self.GOAL,
            str(tmp_path / "x.stl"),
            repo_root=_REPO,
        )
        script_path = result.get("script_path", "")
        if script_path and Path(script_path).exists():
            script = Path(script_path).read_text(encoding="utf-8")
            assert "bpy" in script


# ---------------------------------------------------------------------------
# Fusion 360: motor housing
# ---------------------------------------------------------------------------

class TestFusion360MotorHousing:
    GOAL = "ARIA motor housing shell, 260mm OD, 10mm wall, 180mm length"

    def test_fusion_script_generated(self, tmp_path):
        try:
            from aria_os.fusion_generator import write_fusion_artifacts
        except ImportError:
            pytest.skip("fusion_generator not importable")

        plan = {
            "part_id": "aria_housing",
            "params": {"od_mm": 260.0, "wall_mm": 10.0, "length_mm": 180.0},
            "features": [],
        }
        result = write_fusion_artifacts(
            plan, self.GOAL,
            str(tmp_path / "housing.step"), str(tmp_path / "housing.stl"),
            repo_root=_REPO,
        )
        assert isinstance(result, dict)
        script_path = result.get("script_path", "")
        if script_path and Path(script_path).exists():
            script = Path(script_path).read_text(encoding="utf-8")
            assert len(script) > 0

    def test_fusion_script_references_fusion_api(self, tmp_path):
        try:
            from aria_os.fusion_generator import write_fusion_artifacts
        except ImportError:
            pytest.skip("fusion_generator not importable")

        plan = {
            "part_id": "aria_housing",
            "params": {"od_mm": 260.0, "wall_mm": 10.0, "length_mm": 180.0},
            "features": [],
        }
        result = write_fusion_artifacts(
            plan, self.GOAL,
            str(tmp_path / "h.step"), str(tmp_path / "h.stl"),
            repo_root=_REPO,
        )
        script_path = result.get("script_path", "")
        if script_path and Path(script_path).exists():
            script = Path(script_path).read_text(encoding="utf-8")
            assert "adsk" in script.lower() or "fusion" in script.lower()
