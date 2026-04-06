"""Tests for the scan-to-CAD reverse pipeline."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Fixtures — programmatic test meshes
# ---------------------------------------------------------------------------

@pytest.fixture
def cube_stl(tmp_path):
    """50x30x20mm box as STL."""
    mesh = trimesh.creation.box(extents=[50, 30, 20])
    path = tmp_path / "cube.stl"
    mesh.export(str(path))
    return path


@pytest.fixture
def cylinder_stl(tmp_path):
    """30mm dia x 40mm tall cylinder as STL."""
    mesh = trimesh.creation.cylinder(radius=15, height=40, sections=64)
    path = tmp_path / "cylinder.stl"
    mesh.export(str(path))
    return path


@pytest.fixture
def sphere_stl(tmp_path):
    """20mm radius sphere as STL."""
    mesh = trimesh.creation.icosphere(radius=20, subdivisions=3)
    path = tmp_path / "sphere.stl"
    mesh.export(str(path))
    return path


@pytest.fixture
def degenerate_stl(tmp_path):
    """Mesh with some degenerate (zero-area) faces."""
    mesh = trimesh.creation.box(extents=[50, 30, 20])
    # Add degenerate face (three identical vertices)
    degen_face = np.array([[0, 0, 0], [0, 0, 0], [0, 0, 0]], dtype=np.float64)
    verts = np.vstack([mesh.vertices, degen_face])
    n = len(mesh.vertices)
    faces = np.vstack([mesh.faces, [[n, n + 1, n + 2]]])
    mesh = trimesh.Trimesh(vertices=verts, faces=faces)
    path = tmp_path / "degenerate.stl"
    mesh.export(str(path))
    return path


# ---------------------------------------------------------------------------
# MeshInterpretAgent tests
# ---------------------------------------------------------------------------

class TestMeshInterpretAgent:

    def test_loads_stl(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        result = agent.run(cube_stl)
        assert result.vertices > 0
        assert result.faces > 0
        assert result.file_path

    def test_cleans_degenerate(self, degenerate_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        result = agent.run(degenerate_stl)
        # Should still have valid geometry after cleaning
        assert result.faces > 0
        assert result.vertices > 0

    def test_bounding_box_correct(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        result = agent.run(cube_stl)
        bb = result.bounding_box
        # 50x30x20 box — order may vary depending on orientation
        dims = sorted([bb.x, bb.y, bb.z])
        assert dims == pytest.approx([20.0, 30.0, 50.0], abs=0.1)

    def test_watertight_status(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        result = agent.run(cube_stl)
        assert result.watertight is True

    def test_volume_computed(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        result = agent.run(cube_stl)
        # 50*30*20 = 30000 mm^3
        assert result.volume_mm3 == pytest.approx(30000.0, rel=0.01)

    def test_rejects_bad_extension(self, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        bad_file = tmp_path / "file.txt"
        bad_file.write_text("not a mesh")
        agent = MeshInterpretAgent()
        with pytest.raises(ValueError, match="Unsupported format"):
            agent.run(bad_file)

    def test_rejects_missing_file(self):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        agent = MeshInterpretAgent()
        with pytest.raises(FileNotFoundError):
            agent.run("/nonexistent/file.stl")

    def test_saves_cleaned_mesh(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        out_dir = tmp_path / "out"
        agent = MeshInterpretAgent(output_dir=out_dir)
        result = agent.run(cube_stl)
        assert Path(result.file_path).exists()
        assert Path(result.file_path).stat().st_size > 0


# ---------------------------------------------------------------------------
# FeatureExtractionAgent tests
# ---------------------------------------------------------------------------

class TestFeatureExtractionAgent:

    def test_detects_planes_on_cube(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        from aria_os.agents.feature_extraction_agent import FeatureExtractionAgent

        mesh_agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        cleaned = mesh_agent.run(cube_stl)
        feat_agent = FeatureExtractionAgent()
        features = feat_agent.run(cleaned)

        plane_count = sum(1 for p in features.primitives if p.type == "plane")
        assert plane_count >= 3  # cube has 6 faces, should detect several

    def test_detects_cylinder(self, cylinder_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        from aria_os.agents.feature_extraction_agent import FeatureExtractionAgent

        mesh_agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        cleaned = mesh_agent.run(cylinder_stl)
        feat_agent = FeatureExtractionAgent()
        features = feat_agent.run(cleaned)

        cyl_count = sum(1 for p in features.primitives if p.type == "cylinder")
        assert cyl_count >= 1

    def test_classifies_cylinder_as_turned(self, cylinder_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        from aria_os.agents.feature_extraction_agent import FeatureExtractionAgent

        mesh_agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        cleaned = mesh_agent.run(cylinder_stl)
        feat_agent = FeatureExtractionAgent()
        features = feat_agent.run(cleaned)

        assert features.topology in ("turned_part", "freeform")  # cylinder should be turned

    def test_classifies_cube_as_prismatic(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        from aria_os.agents.feature_extraction_agent import FeatureExtractionAgent

        mesh_agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        cleaned = mesh_agent.run(cube_stl)
        feat_agent = FeatureExtractionAgent()
        features = feat_agent.run(cleaned)

        assert features.topology == "prismatic"

    def test_coverage_nonzero(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        from aria_os.agents.feature_extraction_agent import FeatureExtractionAgent

        mesh_agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        cleaned = mesh_agent.run(cube_stl)
        feat_agent = FeatureExtractionAgent()
        features = feat_agent.run(cleaned)

        assert features.coverage > 0.3

    def test_parametric_description_has_bbox(self, cube_stl, tmp_path):
        from aria_os.agents.mesh_interpret_agent import MeshInterpretAgent
        from aria_os.agents.feature_extraction_agent import FeatureExtractionAgent

        mesh_agent = MeshInterpretAgent(output_dir=tmp_path / "out")
        cleaned = mesh_agent.run(cube_stl)
        feat_agent = FeatureExtractionAgent()
        features = feat_agent.run(cleaned)

        assert "bounding_box_mm" in features.parametric_description
        assert "topology" in features.parametric_description


# ---------------------------------------------------------------------------
# ScanCatalogAgent tests
# ---------------------------------------------------------------------------

class TestScanCatalogAgent:

    def test_stores_and_retrieves(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent
        from aria_os.models.scan_models import BoundingBox, CatalogEntry

        catalog = ScanCatalogAgent(catalog_path=tmp_path / "catalog.json")
        entry = CatalogEntry(
            source_file="test.stl",
            bounding_box=BoundingBox(x=50, y=30, z=20),
            volume_mm3=30000,
            topology="prismatic",
            confidence=0.85,
        )
        catalog.add(entry)

        retrieved = catalog.get(entry.id)
        assert retrieved is not None
        assert retrieved.source_file == "test.stl"
        assert retrieved.topology == "prismatic"

    def test_search_by_dimension_range(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent
        from aria_os.models.scan_models import BoundingBox, CatalogEntry

        catalog = ScanCatalogAgent(catalog_path=tmp_path / "catalog.json")

        # Add a small and large part
        small = CatalogEntry(
            source_file="small.stl",
            bounding_box=BoundingBox(x=10, y=10, z=10),
            topology="prismatic",
        )
        large = CatalogEntry(
            source_file="large.stl",
            bounding_box=BoundingBox(x=100, y=80, z=60),
            topology="prismatic",
        )
        catalog.add(small)
        catalog.add(large)

        # Search for parts around 100x80x60
        results = catalog.search_by_size(100, 80, 60, tolerance=0.15)
        assert len(results) == 1
        assert results[0].source_file == "large.stl"

    def test_search_by_topology(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent
        from aria_os.models.scan_models import BoundingBox, CatalogEntry

        catalog = ScanCatalogAgent(catalog_path=tmp_path / "catalog.json")
        catalog.add(CatalogEntry(topology="prismatic", bounding_box=BoundingBox(10, 10, 10)))
        catalog.add(CatalogEntry(topology="turned_part", bounding_box=BoundingBox(20, 20, 40)))

        results = catalog.search(topology="turned_part")
        assert len(results) == 1
        assert results[0].topology == "turned_part"

    def test_delete(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent
        from aria_os.models.scan_models import CatalogEntry

        catalog = ScanCatalogAgent(catalog_path=tmp_path / "catalog.json")
        entry = CatalogEntry(source_file="delete_me.stl")
        catalog.add(entry)
        assert catalog.get(entry.id) is not None

        catalog.delete(entry.id)
        assert catalog.get(entry.id) is None

    def test_update_material_and_tags(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent
        from aria_os.models.scan_models import CatalogEntry

        catalog = ScanCatalogAgent(catalog_path=tmp_path / "catalog.json")
        entry = CatalogEntry(source_file="update_me.stl")
        catalog.add(entry)

        catalog.update(entry.id, material="stainless_316", tags=["bracket", "legacy"])
        updated = catalog.get(entry.id)
        assert updated.material == "stainless_316"
        assert "bracket" in updated.tags

    def test_persistence_across_instances(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent
        from aria_os.models.scan_models import BoundingBox, CatalogEntry

        cat_path = tmp_path / "catalog.json"
        cat1 = ScanCatalogAgent(catalog_path=cat_path)
        entry = CatalogEntry(source_file="persist.stl", bounding_box=BoundingBox(1, 2, 3))
        cat1.add(entry)

        # New instance should load from disk
        cat2 = ScanCatalogAgent(catalog_path=cat_path)
        assert cat2.get(entry.id) is not None


# ---------------------------------------------------------------------------
# Full pipeline tests
# ---------------------------------------------------------------------------

class TestScanPipeline:

    def test_e2e_cube(self, cube_stl, tmp_path):
        from aria_os.scan_pipeline import run_scan_pipeline

        entry = run_scan_pipeline(
            cube_stl,
            material="aluminium_6061",
            tags=["test"],
            output_dir=tmp_path / "out",
            catalog_path=tmp_path / "catalog.json",
        )
        assert entry.id
        assert entry.topology in ("prismatic", "freeform")
        assert entry.material == "aluminium_6061"
        assert "test" in entry.tags
        assert entry.bounding_box is not None
        dims = sorted([entry.bounding_box.x, entry.bounding_box.y, entry.bounding_box.z])
        assert dims == pytest.approx([20.0, 30.0, 50.0], abs=0.5)

    def test_e2e_cylinder(self, cylinder_stl, tmp_path):
        from aria_os.scan_pipeline import run_scan_pipeline

        entry = run_scan_pipeline(
            cylinder_stl,
            output_dir=tmp_path / "out",
            catalog_path=tmp_path / "catalog.json",
        )
        assert entry.id
        assert entry.topology in ("turned_part", "freeform")
        assert entry.confidence > 0

    def test_e2e_sphere(self, sphere_stl, tmp_path):
        from aria_os.scan_pipeline import run_scan_pipeline

        entry = run_scan_pipeline(
            sphere_stl,
            output_dir=tmp_path / "out",
            catalog_path=tmp_path / "catalog.json",
        )
        assert entry.id
        assert entry.confidence >= 0

    def test_features_json_saved(self, cube_stl, tmp_path):
        from aria_os.scan_pipeline import run_scan_pipeline

        out_dir = tmp_path / "out"
        entry = run_scan_pipeline(
            cube_stl,
            output_dir=out_dir,
            catalog_path=tmp_path / "catalog.json",
        )
        features_file = Path(entry.features_path)
        assert features_file.exists()
        data = json.loads(features_file.read_text())
        assert "topology" in data
        assert "primitives" in data
        assert "parametric_description" in data
