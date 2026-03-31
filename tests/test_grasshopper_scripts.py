"""
Tests for grasshopper_generator.py — verify generated RhinoCommon scripts
use correct API patterns and produce valid Python.
"""
import ast
from pathlib import Path

import pytest

# Allow running from repo root without install
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.grasshopper_generator import write_grasshopper_artifacts


def test_ratchet_ring_script_valid_python(tmp_path):
    plan = {
        "part_id": "aria_ratchet_ring",
        "params": {"od_mm": 213.0, "bore_mm": 185.0, "thickness_mm": 21.0, "n_teeth": 24},
        "features": [{"type": "teeth", "count": 24}],
    }
    step = str(tmp_path / "test.step")
    stl  = str(tmp_path / "test.stl")
    result = write_grasshopper_artifacts(plan, "ARIA ratchet ring 213mm", step, stl,
                                         repo_root=tmp_path)
    script = Path(result["script_path"]).read_text(encoding="utf-8")

    # Must parse as valid Python
    ast.parse(script)

    # Must use correct API calls
    assert "CreateBooleanDifference" in script
    assert "CreateBooleanUnion" in script
    assert "ToBrep" in script
    assert "AddBrep" in script
    assert "BBOX:" in script

    # Old wrong API must NOT appear
    assert "rg.BooleanDifference(" not in script
    assert "rg.BooleanUnion(" not in script


def test_housing_script_valid_python(tmp_path):
    plan = {
        "part_id": "aria_housing",
        "params": {"width_mm": 700.0, "height_mm": 680.0, "depth_mm": 344.0},
    }
    step = str(tmp_path / "housing.step")
    stl  = str(tmp_path / "housing.stl")
    result = write_grasshopper_artifacts(plan, "ARIA housing", step, stl,
                                         repo_root=tmp_path)
    script = Path(result["script_path"]).read_text(encoding="utf-8")
    ast.parse(script)
    assert "CreateBooleanDifference" in script
    assert "ToBrep" in script
    assert "AddBrep" in script
    assert "BBOX:" in script
    assert "rg.BooleanDifference(" not in script


def test_brake_drum_script_valid_python(tmp_path):
    plan = {
        "part_id": "aria_brake_drum",
        "params": {"diameter": 200.0, "width": 40.0, "shaft_diameter": 20.0, "wall_thickness": 8.0},
    }
    result = write_grasshopper_artifacts(plan, "ARIA brake drum",
                                         str(tmp_path / "bd.step"),
                                         str(tmp_path / "bd.stl"),
                                         repo_root=tmp_path)
    script = Path(result["script_path"]).read_text(encoding="utf-8")
    ast.parse(script)
    assert "CreateBooleanDifference" in script
    assert "ToBrep" in script
    assert "AddBrep" in script
    assert "BBOX:" in script
    assert "rg.BooleanDifference(" not in script


def test_spool_script_valid_python(tmp_path):
    plan = {
        "part_id": "aria_spool",
        "params": {"diameter": 600.0, "width": 50.0, "flange_diameter": 640.0,
                   "flange_thickness": 8.0, "hub_diameter": 47.2},
    }
    result = write_grasshopper_artifacts(plan, "ARIA spool",
                                         str(tmp_path / "spool.step"),
                                         str(tmp_path / "spool.stl"),
                                         repo_root=tmp_path)
    script = Path(result["script_path"]).read_text(encoding="utf-8")
    ast.parse(script)
    assert "CreateBooleanUnion" in script
    assert "CreateBooleanDifference" in script
    assert "ToBrep" in script
    assert "AddBrep" in script
    assert "BBOX:" in script


def test_no_backslash_paths(tmp_path):
    """Paths in generated scripts must use forward slashes, not raw backslashes."""
    plan = {"part_id": "aria_catch_pawl", "params": {}}
    # Simulate a Windows-style path with backslashes
    step = "C:\\outputs\\cad\\step\\test.step"
    stl  = "C:\\outputs\\cad\\stl\\test.stl"
    result = write_grasshopper_artifacts(plan, "ARIA catch pawl", step, stl,
                                         repo_root=tmp_path)
    script = Path(result["script_path"]).read_text(encoding="utf-8")

    # After forward-slash conversion the generated path lines must be valid Python
    ast.parse(script)

    # Must not contain double-escaped backslashes (the old bug pattern)
    assert "C:\\\\outputs" not in script
    # Must not raise unicodeescape at parse time (already verified by ast.parse above)

    # The runner must also be valid Python
    runner = Path(result["runner_path"]).read_text(encoding="utf-8")
    ast.parse(runner)
    assert "C:\\\\outputs" not in runner


def test_all_templates_produce_valid_python(tmp_path):
    """Smoke test: every template in _TEMPLATE_MAP must produce parseable Python."""
    from aria_os.grasshopper_generator import _TEMPLATE_MAP

    for part_id, fn in _TEMPLATE_MAP.items():
        plan = {"part_id": part_id, "params": {}}
        result = write_grasshopper_artifacts(plan, f"test {part_id}",
                                             str(tmp_path / f"{part_id}.step"),
                                             str(tmp_path / f"{part_id}.stl"),
                                             repo_root=tmp_path)
        script = Path(result["script_path"]).read_text(encoding="utf-8")
        try:
            ast.parse(script)
        except SyntaxError as e:
            pytest.fail(f"SyntaxError in template {part_id}: {e}")
        assert "AddBrep" in script, f"{part_id}: missing AddBrep"
        assert "BBOX:" in script, f"{part_id}: missing BBOX print"
        assert "rg.BooleanDifference(" not in script, f"{part_id}: uses old wrong API"
        assert "rg.BooleanUnion(" not in script, f"{part_id}: uses old wrong API"
