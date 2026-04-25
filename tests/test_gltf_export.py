"""
tests/test_gltf_export.py — glTF 2.0 binary exporter for the structsight-vr handoff.

Covers:
  - pure-mesh export (no StructSight JSON) writes a .glb and returns counts
  - StructSight JSON tinting:
      * high-risk flag (corrosion / fatigue / etc.) -> red tint
      * verification_required non-empty             -> amber tint
      * clean (no flags, no verify)                 -> green tint
  - empty STL raises ValueError
  - missing inputs raise FileNotFoundError
  - default out_path lands next to the STL as part.glb
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# trimesh + numpy are already required by ARIA-OS proper, but guard the import
# so the test suite at least *runs* without them.
trimesh = pytest.importorskip("trimesh")
np = pytest.importorskip("numpy")

from aria_os.generators.gltf_export import (  # noqa: E402
    _TINT_AMBER,
    _TINT_GREEN,
    _TINT_RED,
    _classify_risk,
    export_to_gltf,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def cube_stl(tmp_path: Path) -> Path:
    """Write a simple 10mm cube to disk as STL and return the path."""
    mesh = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    stl_path = tmp_path / "cube.stl"
    mesh.export(str(stl_path))
    return stl_path


@pytest.fixture
def empty_stl(tmp_path: Path) -> Path:
    """Write a syntactically valid but empty ASCII STL."""
    stl_path = tmp_path / "empty.stl"
    stl_path.write_text("solid empty\nendsolid empty\n", encoding="utf-8")
    return stl_path


def _ss_json(tmp_path: Path, name: str, payload: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Risk classification helper
# --------------------------------------------------------------------------- #


class TestClassifyRisk:
    def test_high_risk_flag_returns_red(self):
        data = {"risk_flags": ["marine corrosion"], "verification_required": []}
        assert _classify_risk(data) == _TINT_RED

    def test_thermal_substring_matches(self):
        data = {"risk_flags": ["thermal runaway"], "verification_required": []}
        assert _classify_risk(data) == _TINT_RED

    def test_verify_only_returns_amber(self):
        data = {"risk_flags": [], "verification_required": ["FEA pending"]}
        assert _classify_risk(data) == _TINT_AMBER

    def test_clean_returns_green(self):
        data = {"risk_flags": [], "verification_required": []}
        assert _classify_risk(data) == _TINT_GREEN

    def test_low_risk_unknown_flag_returns_green(self):
        # "permit required" contains "permit" -> red. "supply chain" doesn't
        # match any high-risk keyword and there's no verification required.
        data = {"risk_flags": ["supply chain"], "verification_required": []}
        assert _classify_risk(data) == _TINT_GREEN

    def test_high_risk_overrides_verify(self):
        data = {
            "risk_flags": ["fatigue loading"],
            "verification_required": ["FEA"],
        }
        assert _classify_risk(data) == _TINT_RED


# --------------------------------------------------------------------------- #
# Pure mesh export (no JSON)
# --------------------------------------------------------------------------- #


class TestPureMeshExport:
    def test_writes_glb_file(self, cube_stl: Path, tmp_path: Path):
        out = tmp_path / "out.glb"
        result = export_to_gltf(cube_stl, structsight_json=None, out_path=out)
        assert out.is_file()
        assert result["glb_path"] == str(out)

    def test_glb_has_correct_magic(self, cube_stl: Path, tmp_path: Path):
        out = tmp_path / "out.glb"
        export_to_gltf(cube_stl, out_path=out)
        # First 4 bytes of a glTF 2.0 binary container = "glTF"
        magic = out.read_bytes()[:4]
        assert magic == b"glTF"

    def test_glb_version_is_2(self, cube_stl: Path, tmp_path: Path):
        out = tmp_path / "out.glb"
        export_to_gltf(cube_stl, out_path=out)
        version = struct.unpack("<I", out.read_bytes()[4:8])[0]
        assert version == 2

    def test_returns_vertex_and_face_counts(self, cube_stl: Path, tmp_path: Path):
        out = tmp_path / "out.glb"
        result = export_to_gltf(cube_stl, out_path=out)
        # A box has 12 triangles. Vertex count is implementation-dependent
        # (trimesh dedupes), but it must be > 0.
        assert result["face_count"] == 12
        assert result["vertex_count"] > 0

    def test_tint_is_none_without_json(self, cube_stl: Path, tmp_path: Path):
        out = tmp_path / "out.glb"
        result = export_to_gltf(cube_stl, structsight_json=None, out_path=out)
        assert result["tint"] is None

    def test_default_out_path_writes_part_glb_next_to_stl(self, cube_stl: Path):
        # No out_path -> defaults to <stl_dir>/part.glb
        result = export_to_gltf(cube_stl, structsight_json=None)
        expected = cube_stl.with_name("part.glb")
        assert Path(result["glb_path"]) == expected
        assert expected.is_file()


# --------------------------------------------------------------------------- #
# Vertex-color tinting from synthetic structsight.json
# --------------------------------------------------------------------------- #


def _read_glb_vertex_colors(glb_path: Path) -> "np.ndarray":
    """Round-trip the .glb back through trimesh and grab vertex colors."""
    loaded = trimesh.load(str(glb_path), file_type="glb", process=False)
    if isinstance(loaded, trimesh.Scene):
        loaded = trimesh.util.concatenate(list(loaded.geometry.values()))
    colors = loaded.visual.vertex_colors
    assert colors is not None and len(colors) > 0
    return colors


class TestVertexColorTinting:
    def test_red_tint_for_high_risk(self, cube_stl: Path, tmp_path: Path):
        ssj = _ss_json(
            tmp_path,
            "high.json",
            {
                "risk_flags": ["fatigue loading", "marine corrosion"],
                "verification_required": [],
            },
        )
        out = tmp_path / "high.glb"
        result = export_to_gltf(cube_stl, structsight_json=ssj, out_path=out)
        assert result["tint"] == "red"
        colors = _read_glb_vertex_colors(out)
        # First vertex should be reddish: R dominant, G low, B low
        r, g, b, _ = colors[0]
        assert r > 200, f"expected red R~255, got {r}"
        assert g < 120, f"expected red G low, got {g}"
        assert b < 120, f"expected red B low, got {b}"

    def test_amber_tint_for_verification_required(
        self, cube_stl: Path, tmp_path: Path
    ):
        ssj = _ss_json(
            tmp_path,
            "amber.json",
            {
                "risk_flags": ["supply chain"],
                "verification_required": ["FEA confirming yield margin"],
            },
        )
        out = tmp_path / "amber.glb"
        result = export_to_gltf(cube_stl, structsight_json=ssj, out_path=out)
        assert result["tint"] == "amber"
        colors = _read_glb_vertex_colors(out)
        r, g, b, _ = colors[0]
        # Amber = ~ff a0 30: high R, mid G, low B
        assert r > 200
        assert 100 < g < 200
        assert b < 100

    def test_green_tint_for_clean(self, cube_stl: Path, tmp_path: Path):
        ssj = _ss_json(
            tmp_path,
            "clean.json",
            {"risk_flags": [], "verification_required": []},
        )
        out = tmp_path / "clean.glb"
        result = export_to_gltf(cube_stl, structsight_json=ssj, out_path=out)
        assert result["tint"] == "green"
        colors = _read_glb_vertex_colors(out)
        r, g, b, _ = colors[0]
        # Green = ~40 c0 60: low R, high G, low B
        assert r < 130
        assert g > 150
        assert b < 130

    def test_accepts_dict_directly(self, cube_stl: Path, tmp_path: Path):
        out = tmp_path / "dict.glb"
        result = export_to_gltf(
            cube_stl,
            structsight_json={"risk_flags": ["thermal"], "verification_required": []},
            out_path=out,
        )
        assert result["tint"] == "red"


# --------------------------------------------------------------------------- #
# Error paths
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    def test_missing_stl_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            export_to_gltf(tmp_path / "does_not_exist.stl")

    def test_empty_stl_raises_value_error(self, empty_stl: Path, tmp_path: Path):
        with pytest.raises(ValueError):
            export_to_gltf(empty_stl, out_path=tmp_path / "empty.glb")

    def test_missing_structsight_json_raises(
        self, cube_stl: Path, tmp_path: Path
    ):
        with pytest.raises(FileNotFoundError):
            export_to_gltf(
                cube_stl,
                structsight_json=tmp_path / "no_such.json",
                out_path=tmp_path / "out.glb",
            )

    def test_non_dict_structsight_json_raises(
        self, cube_stl: Path, tmp_path: Path
    ):
        bad = tmp_path / "bad.json"
        bad.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError):
            export_to_gltf(
                cube_stl, structsight_json=bad, out_path=tmp_path / "out.glb"
            )
