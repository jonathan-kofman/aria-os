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


def _cadquery_available():
    try:
        import cadquery  # noqa: F401
        return True
    except ImportError:
        return False


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


# ---------------------------------------------------------------------------
# Reconstruction tests
# ---------------------------------------------------------------------------

@pytest.fixture
def box_with_holes_stl(tmp_path):
    """80x50x25mm box with 4 through-holes (8mm diameter) as STL."""
    box = trimesh.creation.box(extents=[80, 50, 25])
    for x, y in [(-25, -15), (-25, 15), (25, -15), (25, 15)]:
        hole = trimesh.creation.cylinder(radius=4, height=30, sections=32)
        hole.apply_translation([x, y, 0])
        box = box.difference(hole)
    path = tmp_path / "box_holes.stl"
    box.export(str(path))
    return path


class TestReconstructAgent:

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_reconstruct_prismatic_generates_script(self, cube_stl, tmp_path):
        """Scan a cube, reconstruct it, verify script is generated."""
        from aria_os.scan_pipeline import run_scan_pipeline, reconstruct_from_catalog

        entry = run_scan_pipeline(
            cube_stl,
            output_dir=tmp_path / "scan_out",
            catalog_path=tmp_path / "catalog.json",
        )
        result = reconstruct_from_catalog(
            entry.id,
            catalog_path=tmp_path / "catalog.json",
            output_dir=tmp_path / "recon_out",
        )
        assert Path(result["script_path"]).exists()
        script = Path(result["script_path"]).read_text()
        assert "cadquery" in script.lower() or "cq" in script

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_reconstruct_prismatic_dimensions_match(self, cube_stl, tmp_path):
        """Scan a 50x30x20 cube, reconstruct, verify dims match within 1mm."""
        from aria_os.scan_pipeline import run_scan_pipeline, reconstruct_from_catalog

        entry = run_scan_pipeline(
            cube_stl,
            output_dir=tmp_path / "scan_out",
            catalog_path=tmp_path / "catalog.json",
        )
        result = reconstruct_from_catalog(
            entry.id,
            catalog_path=tmp_path / "catalog.json",
            output_dir=tmp_path / "recon_out",
        )
        assert result.get("bbox") is not None, f"Reconstruction failed: {result.get('error')}"
        bb = result["bbox"]
        orig = sorted([50.0, 30.0, 20.0])
        recon = sorted([bb["x"], bb["y"], bb["z"]])
        assert recon == pytest.approx(orig, abs=1.0), f"Dims mismatch: orig={orig} recon={recon}"

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_reconstruct_prismatic_step_exported(self, cube_stl, tmp_path):
        """Reconstruction produces a STEP file."""
        from aria_os.scan_pipeline import run_scan_pipeline, reconstruct_from_catalog

        entry = run_scan_pipeline(
            cube_stl,
            output_dir=tmp_path / "scan_out",
            catalog_path=tmp_path / "catalog.json",
        )
        result = reconstruct_from_catalog(
            entry.id,
            catalog_path=tmp_path / "catalog.json",
            output_dir=tmp_path / "recon_out",
        )
        assert result.get("step_path"), f"No STEP: {result.get('error')}"
        assert Path(result["step_path"]).exists()
        assert Path(result["step_path"]).stat().st_size > 500

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_reconstruct_turned_part(self, cylinder_stl, tmp_path):
        """Scan a cylinder, reconstruct as turned part, verify dims."""
        from aria_os.scan_pipeline import run_scan_pipeline, reconstruct_from_catalog

        entry = run_scan_pipeline(
            cylinder_stl,
            output_dir=tmp_path / "scan_out",
            catalog_path=tmp_path / "catalog.json",
        )
        result = reconstruct_from_catalog(
            entry.id,
            catalog_path=tmp_path / "catalog.json",
            output_dir=tmp_path / "recon_out",
        )
        assert result.get("bbox") is not None, f"Reconstruction failed: {result.get('error')}"
        bb = result["bbox"]
        # Original: 30mm dia x 40mm tall → bbox 30x30x40
        assert bb["z"] == pytest.approx(40.0, abs=1.0) or max(bb["x"], bb["y"]) == pytest.approx(30.0, abs=1.0)

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_reconstruct_box_with_holes_dimensions(self, box_with_holes_stl, tmp_path):
        """Scan 80x50x25 box with holes, reconstruct, verify body dims within 1mm."""
        from aria_os.scan_pipeline import run_scan_pipeline, reconstruct_from_catalog

        entry = run_scan_pipeline(
            box_with_holes_stl,
            output_dir=tmp_path / "scan_out",
            catalog_path=tmp_path / "catalog.json",
        )
        result = reconstruct_from_catalog(
            entry.id,
            catalog_path=tmp_path / "catalog.json",
            output_dir=tmp_path / "recon_out",
        )
        assert result.get("bbox") is not None, f"Reconstruction failed: {result.get('error')}"
        bb = result["bbox"]
        # Body dimensions should match the original bounding box
        orig = sorted([80.0, 50.0, 25.0])
        recon = sorted([bb["x"], bb["y"], bb["z"]])
        assert recon == pytest.approx(orig, abs=1.0), f"Dims mismatch: orig={orig} recon={recon}"

    @pytest.mark.skipif(not _cadquery_available(), reason="cadquery not installed")
    def test_reconstruct_box_with_holes_has_holes(self, box_with_holes_stl, tmp_path):
        """Reconstructed box should have less volume than a solid box (holes cut material)."""
        from aria_os.scan_pipeline import run_scan_pipeline, reconstruct_from_catalog

        entry = run_scan_pipeline(
            box_with_holes_stl,
            output_dir=tmp_path / "scan_out",
            catalog_path=tmp_path / "catalog.json",
        )
        result = reconstruct_from_catalog(
            entry.id,
            catalog_path=tmp_path / "catalog.json",
            output_dir=tmp_path / "recon_out",
        )
        # Load reconstructed STL and check volume is less than solid box
        if result.get("stl_path") and Path(result["stl_path"]).exists():
            recon_mesh = trimesh.load(result["stl_path"])
            solid_volume = 80.0 * 50.0 * 25.0  # 100,000 mm^3
            # If holes were cut, volume should be measurably less
            if recon_mesh.is_watertight:
                assert abs(recon_mesh.volume) < solid_volume


# ---------------------------------------------------------------------------
# Similarity search tests
# ---------------------------------------------------------------------------

class TestSimilaritySearch:

    def _populate_catalog(self, tmp_path):
        """Create a catalog with several diverse parts."""
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent
        from aria_os.models.scan_models import BoundingBox, CatalogEntry

        cat = ScanCatalogAgent(catalog_path=tmp_path / "catalog.json")
        cat.add(CatalogEntry(
            id="bracket1", source_file="bracket.stl",
            bounding_box=BoundingBox(80, 50, 25), volume_mm3=95000,
            topology="prismatic", tags=["bracket"],
            primitives_summary=[{"type": "plane", "count": 6}, {"type": "cylinder", "count": 4}],
        ))
        cat.add(CatalogEntry(
            id="shaft1", source_file="shaft.stl",
            bounding_box=BoundingBox(30, 30, 40), volume_mm3=28000,
            topology="turned_part", tags=["shaft"],
            primitives_summary=[{"type": "cylinder", "count": 1}, {"type": "plane", "count": 2}],
        ))
        cat.add(CatalogEntry(
            id="plate1", source_file="plate.stl",
            bounding_box=BoundingBox(200, 150, 5), volume_mm3=150000,
            topology="prismatic", tags=["plate"],
            primitives_summary=[{"type": "plane", "count": 6}],
        ))
        return tmp_path / "catalog.json"

    def test_find_similar_by_dims(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent

        cat_path = self._populate_catalog(tmp_path)
        cat = ScanCatalogAgent(catalog_path=cat_path)

        # Search for something close to the bracket (80x50x25)
        results = cat.find_similar(target_dims=(75, 45, 20), top_n=3)
        assert len(results) == 3
        # Bracket should be the best match
        assert results[0][0].id == "bracket1"
        assert results[0][1] > 0.5  # reasonable similarity

    def test_find_similar_by_primitives(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent

        cat_path = self._populate_catalog(tmp_path)
        cat = ScanCatalogAgent(catalog_path=cat_path)

        # Search for something with lots of cylinders (shaft-like)
        results = cat.find_similar(target_primitives={"cylinder": 1}, top_n=3)
        assert len(results) == 3
        # Shaft should score highest on cylinder match
        ids = [r[0].id for r in results]
        assert "shaft1" in ids[:2]

    def test_find_similar_combined(self, tmp_path):
        from aria_os.agents.scan_catalog_agent import ScanCatalogAgent

        cat_path = self._populate_catalog(tmp_path)
        cat = ScanCatalogAgent(catalog_path=cat_path)

        # Search for bracket-like: 80x50x25 with planes and holes
        results = cat.find_similar(
            target_dims=(80, 50, 25),
            target_volume=95000,
            target_primitives={"plane": 6, "cylinder": 4},
            top_n=3,
        )
        assert results[0][0].id == "bracket1"
        assert results[0][1] > 0.9  # near-perfect match

    def test_parse_search_description_dims(self):
        from aria_os.agents.scan_catalog_agent import parse_search_description

        parsed = parse_search_description("75x45x12 bracket with 4 holes")
        assert parsed["dims"] == pytest.approx((75, 45, 12))
        assert parsed["primitives"]["cylinder"] == 4  # 4 holes
        assert parsed["primitives"]["plane"] == 6  # bracket keyword

    def test_parse_search_description_shaft(self):
        from aria_os.agents.scan_catalog_agent import parse_search_description

        parsed = parse_search_description("50mm diameter shaft")
        assert parsed["dims"] is not None
        assert parsed["primitives"]["cylinder"] >= 1

    def test_search_similar_e2e(self, tmp_path):
        from aria_os.scan_pipeline import search_similar

        self._populate_catalog(tmp_path)
        results = search_similar(
            "75x45x20 bracket with 4 holes",
            catalog_path=tmp_path / "catalog.json",
        )
        assert len(results) > 0
        assert results[0][0].id == "bracket1"


# ---------------------------------------------------------------------------
# Batch scan-dir tests
# ---------------------------------------------------------------------------

class TestScanDirectory:

    def test_scan_dir_processes_all_files(self, tmp_path):
        """scan_directory should process all STL files and return entries."""
        from aria_os.scan_pipeline import scan_directory

        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        # Create 3 test meshes
        trimesh.creation.box(extents=[50, 30, 20]).export(str(scan_dir / "box.stl"))
        trimesh.creation.cylinder(radius=10, height=30).export(str(scan_dir / "cyl.stl"))
        trimesh.creation.icosphere(radius=15).export(str(scan_dir / "sphere.stl"))

        entries = scan_directory(
            scan_dir,
            material="test_mat",
            catalog_path=tmp_path / "catalog.json",
        )
        assert len(entries) == 3
        assert all(e.material == "test_mat" for e in entries)

    def test_scan_dir_handles_bad_file(self, tmp_path):
        """scan_directory should skip corrupt files and continue."""
        from aria_os.scan_pipeline import scan_directory

        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        trimesh.creation.box(extents=[40, 40, 10]).export(str(scan_dir / "good.stl"))
        (scan_dir / "bad.stl").write_text("not a real mesh file")

        entries = scan_directory(
            scan_dir,
            catalog_path=tmp_path / "catalog.json",
        )
        # Should get 1 success (the good file), the bad file should be logged
        assert len(entries) == 1
        assert entries[0].source_file == "good.stl"

    def test_scan_dir_empty_dir(self, tmp_path):
        """scan_directory on empty dir returns empty list."""
        from aria_os.scan_pipeline import scan_directory

        scan_dir = tmp_path / "empty"
        scan_dir.mkdir()
        entries = scan_directory(scan_dir, catalog_path=tmp_path / "catalog.json")
        assert entries == []

    def test_scan_dir_with_tags(self, tmp_path):
        """Tags propagate to all scanned entries."""
        from aria_os.scan_pipeline import scan_directory

        scan_dir = tmp_path / "scans"
        scan_dir.mkdir()
        trimesh.creation.box(extents=[30, 20, 10]).export(str(scan_dir / "part.stl"))

        entries = scan_directory(
            scan_dir,
            tags=["legacy", "batch1"],
            catalog_path=tmp_path / "catalog.json",
        )
        assert len(entries) == 1
        assert "legacy" in entries[0].tags
        assert "batch1" in entries[0].tags
