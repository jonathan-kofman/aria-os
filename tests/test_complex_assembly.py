"""
Tests for the complex-assembly stack: components catalog, hierarchical assembly,
kinematic joints, BOM generation.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Components catalog
# ---------------------------------------------------------------------------

class TestComponentCatalog:
    def test_catalog_nonempty(self):
        from aria_os.components import catalog
        total = len(catalog)
        assert total > 100
        # Spot-check that known entries are present
        assert catalog.get("M6x20_12.9") is not None
        assert catalog.get("6205") is not None
        assert catalog.get("NEMA17-48mm-5mm") is not None

    def test_five_categories_populated(self):
        from aria_os.components import catalog
        expected_minimums = {
            "fastener": 10,
            "bearing": 5,
            "motor": 3,
            "coupling": 2,
            "hardware": 2,
        }
        for cat, min_count in expected_minimums.items():
            items = catalog.list_category(cat)
            assert len(items) >= min_count, (
                f"Category '{cat}' has {len(items)} items, expected >= {min_count}"
            )
            # Each item must have the correct category field
            assert all(i.category == cat for i in items), f"Category mismatch in '{cat}'"

    def test_lookup_bolt(self):
        from aria_os.components import catalog
        spec = catalog.get("M6x20_12.9")
        assert spec is not None
        assert spec.category == "fastener"
        assert spec.subcategory == "bolt"
        assert spec.mass_g > 0
        assert spec.cost_usd > 0

    def test_lookup_bearing(self):
        from aria_os.components import catalog
        spec = catalog.get("6205")
        assert spec is not None
        assert spec.category == "bearing"
        assert spec.dimensions["bore_mm"] == 25
        assert spec.dimensions["od_mm"] == 52

    def test_lookup_nema_motor(self):
        from aria_os.components import catalog
        spec = catalog.get("NEMA23-56mm-8mm")
        assert spec is not None
        assert spec.category == "motor"
        assert spec.dimensions["shaft_dia_mm"] == 8.0

    def test_mating_features_on_motor(self):
        from aria_os.components import catalog
        spec = catalog.get("NEMA17-48mm-5mm")
        assert spec is not None
        feature_names = {f.name for f in spec.mating_features}
        assert "shaft_axis" in feature_names
        assert "mount_bolts" in feature_names

    def test_search_returns_results(self):
        from aria_os.components import catalog
        results = catalog.search("M6")
        assert len(results) > 0
        # Every returned component must have "M6" in its designation
        for spec in results:
            assert "M6" in spec.designation, (
                f"search('M6') returned '{spec.designation}' which has no 'M6'"
            )
        # The fastener category must dominate M6 results
        categories = [s.category for s in results]
        assert "fastener" in categories

    def test_generate_bolt_produces_step(self, tmp_path):
        from aria_os.components import catalog
        out = tmp_path / "bolt.step"
        path = catalog.generate("M6x20_12.9", str(out))
        assert Path(path).is_file()
        assert Path(path).stat().st_size > 1000  # at least 1 KB

    def test_generate_bearing_produces_step(self, tmp_path):
        from aria_os.components import catalog
        out = tmp_path / "bearing.step"
        path = catalog.generate("6200", str(out))
        assert Path(path).is_file()
        assert Path(path).stat().st_size > 1000

    def test_unknown_component_raises(self):
        from aria_os.components import catalog
        with pytest.raises(KeyError):
            catalog.generate("NOT_A_REAL_PART", "/tmp/x.step")

    def test_bom_row_format(self):
        from aria_os.components import catalog
        spec = catalog.get("M6x20_12.9")
        row = spec.to_bom_row(quantity=4)
        assert row["designation"] == "M6x20_12.9"
        assert row["quantity"] == 4
        assert row["total_cost_usd"] == spec.cost_usd * 4


# ---------------------------------------------------------------------------
# Hierarchical assembly
# ---------------------------------------------------------------------------

class TestHierarchicalAssembly:
    def test_flatten_flat_assembly(self):
        from aria_os.hierarchical_assembly import flatten_assembly
        config = {
            "name": "flat_test",
            "parts": [
                {"id": "a", "step": "/tmp/a.step", "pos": [0, 0, 0]},
                {"id": "b", "step": "/tmp/b.step", "pos": [50, 0, 0]},
            ],
        }
        flat = flatten_assembly(config)
        assert len(flat) == 2
        assert flat[0]["id"] == "a"
        assert flat[1]["id"] == "b"

    def test_component_reference_resolves(self, tmp_path):
        from aria_os.hierarchical_assembly import flatten_assembly
        config = {
            "name": "with_component",
            "parts": [
                {"id": "bolt1", "component": "M6x20_12.9", "pos": [0, 0, 0]},
            ],
        }
        flat = flatten_assembly(config)
        assert len(flat) == 1
        assert flat[0]["_component"] == "M6x20_12.9"
        # STEP file was auto-generated
        assert Path(flat[0]["step"]).is_file()

    def test_subassembly_recursion(self, tmp_path):
        from aria_os.hierarchical_assembly import flatten_assembly

        # Inner sub-assembly with 2 components
        sub_cfg_path = tmp_path / "sub.json"
        sub_cfg_path.write_text(json.dumps({
            "name": "motor_unit",
            "parts": [
                {"id": "motor", "component": "NEMA17-48mm-5mm", "pos": [0, 0, 0]},
                {"id": "bolt", "component": "M3x16_12.9", "pos": [10, 0, 0]},
            ],
        }))

        # Outer config references sub
        outer = {
            "name": "machine",
            "parts": [
                {"id": "frame", "step": str(tmp_path / "frame.step"), "pos": [0, 0, 0]},
                {"id": "unit1", "assembly": str(sub_cfg_path), "pos": [100, 0, 0]},
            ],
        }
        flat = flatten_assembly(outer, config_path=tmp_path / "outer.json")
        # Should have 1 frame + 2 from subassembly (with prefixed ids)
        assert len(flat) == 3
        ids = {p["id"] for p in flat}
        assert "frame" in ids
        assert any(i.startswith("unit1/") for i in ids)

    def test_circular_subassembly_raises(self, tmp_path):
        from aria_os.hierarchical_assembly import flatten_assembly, AssemblyResolutionError

        a_path = tmp_path / "a.json"
        b_path = tmp_path / "b.json"
        a_path.write_text(json.dumps({
            "name": "a", "parts": [{"id": "to_b", "assembly": "b.json"}],
        }))
        b_path.write_text(json.dumps({
            "name": "b", "parts": [{"id": "to_a", "assembly": "a.json"}],
        }))
        with pytest.raises(AssemblyResolutionError, match="[Cc]ircular|cycle"):
            flatten_assembly(json.loads(a_path.read_text()), config_path=a_path)

    def test_missing_reference_raises(self):
        from aria_os.hierarchical_assembly import flatten_assembly, AssemblyResolutionError
        config = {"name": "bad", "parts": [
            {"id": "x", "component": "NOT_A_REAL_COMPONENT"},
        ]}
        with pytest.raises(AssemblyResolutionError):
            flatten_assembly(config)

    def test_depends_on_with_offset(self):
        from aria_os.hierarchical_assembly import flatten_assembly
        config = {
            "name": "chain",
            "parts": [
                {"id": "base", "step": "/tmp/base.step", "pos": [10, 0, 0]},
                {"id": "top", "step": "/tmp/top.step",
                 "depends_on": "base", "offset": [0, 0, 20]},
            ],
        }
        flat = flatten_assembly(config)
        top = next(p for p in flat if p["id"] == "top")
        assert top["pos"] == [10, 0, 20]


# ---------------------------------------------------------------------------
# Kinematic joints
# ---------------------------------------------------------------------------

class TestJoints:
    def test_revolute_joint_valid(self):
        from aria_os.joints import Joint
        j = Joint(id="j1", type="revolute", parent="base", child="link1",
                  axis=(0, 0, 1), range_deg=(-180, 180))
        j.validate()  # should not raise

    def test_unknown_joint_type_raises(self):
        from aria_os.joints import Joint
        j = Joint(id="j1", type="invalid_type", parent="a", child="b")
        with pytest.raises(ValueError, match="unknown type"):
            j.validate()

    def test_self_joint_raises(self):
        from aria_os.joints import Joint
        j = Joint(id="j1", type="revolute", parent="link1", child="link1")
        with pytest.raises(ValueError, match="parent == child"):
            j.validate()

    def test_kinematic_chain_validates_tree(self):
        from aria_os.joints import KinematicChain, Joint
        chain = KinematicChain(name="arm", link_ids=["base", "l1", "l2"])
        chain.add_joint(Joint("j1", "revolute", "base", "l1", axis=(0, 0, 1)))
        chain.add_joint(Joint("j2", "revolute", "l1", "l2", axis=(1, 0, 0)))
        errors = chain.validate()
        assert errors == []

    def test_chain_detects_multiple_parents(self):
        from aria_os.joints import KinematicChain, Joint
        chain = KinematicChain(name="bad", link_ids=["a", "b", "c"])
        chain.add_joint(Joint("j1", "revolute", "a", "c"))
        chain.add_joint(Joint("j2", "revolute", "b", "c"))  # c has 2 parents
        errors = chain.validate()
        assert any("2 parent joints" in e for e in errors)

    def test_export_urdf(self, tmp_path):
        from aria_os.joints import KinematicChain, Joint, export_urdf
        chain = KinematicChain(name="test_arm", link_ids=["base", "l1", "l2"])
        chain.add_joint(Joint("j1", "revolute", "base", "l1",
                              axis=(0, 0, 1), range_deg=(-180, 180)))
        chain.add_joint(Joint("j2", "revolute", "l1", "l2",
                              axis=(0, 1, 0), range_deg=(-90, 90)))
        out = tmp_path / "arm.urdf"
        path = export_urdf(chain, output_path=str(out))
        content = Path(path).read_text()
        assert '<robot name="test_arm">' in content
        assert '<joint name="j1"' in content
        assert '<joint name="j2"' in content
        assert '<link name="base"' in content


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

class TestBOM:
    def test_bom_counts_purchased(self, tmp_path):
        from aria_os.assembly_bom import generate_bom
        config = {
            "name": "test",
            "parts": [
                {"id": "b1", "component": "M6x20_12.9"},
                {"id": "b2", "component": "M6x20_12.9"},
                {"id": "b3", "component": "M6x20_12.9"},
                {"id": "bearing1", "component": "6205"},
            ],
        }
        bom = generate_bom(config)
        # 3 bolts + 1 bearing = 2 unique SKUs
        assert bom["summary"]["unique_components"] == 2
        assert bom["summary"]["purchased_count"] == 4

    def test_bom_calculates_cost(self):
        from aria_os.assembly_bom import generate_bom
        from aria_os.components import catalog
        bolt_cost = catalog.get("M6x20_12.9").cost_usd
        config = {"name": "t", "parts": [
            {"id": f"b{i}", "component": "M6x20_12.9"} for i in range(4)
        ]}
        bom = generate_bom(config)
        assert bom["summary"]["total_purchased_cost_usd"] == pytest.approx(bolt_cost * 4)

    def test_bom_separates_fabricated(self, tmp_path):
        from aria_os.assembly_bom import generate_bom
        config = {"name": "mixed", "parts": [
            {"id": "custom", "step": str(tmp_path / "custom.step")},
            {"id": "bolt", "component": "M4x10_12.9"},
        ]}
        bom = generate_bom(config)
        assert bom["summary"]["fabricated_count"] == 1
        assert bom["summary"]["purchased_count"] == 1

    def test_bom_markdown_output(self, tmp_path):
        from aria_os.assembly_bom import generate_bom, write_bom_markdown
        config = {"name": "tiny", "parts": [
            {"id": "b1", "component": "M3x8_12.9"},
        ]}
        bom = generate_bom(config)
        out = tmp_path / "bom.md"
        write_bom_markdown(bom, str(out))
        content = out.read_text()
        assert "Bill of Materials" in content
        assert "M3x8_12.9" in content

    def test_bom_to_millforge_jobs(self, tmp_path):
        from aria_os.assembly_bom import generate_bom, bom_to_millforge_jobs
        config = {"name": "fab", "parts": [
            {"id": "frame", "step": str(tmp_path / "frame.step")},
            {"id": "bolt1", "component": "M6x16_12.9"},
        ]}
        bom = generate_bom(config)
        jobs = bom_to_millforge_jobs(bom, goal="test fab")
        # Only fabricated parts become MillForge jobs (bolts go to procurement)
        assert len(jobs) == 1
        assert jobs[0]["part_name"] == "frame"


# ---------------------------------------------------------------------------
# Mating solver — new constraint types
# ---------------------------------------------------------------------------

class TestMatingConstraints:
    def test_gear_mesh_positions_correctly(self):
        from aria_os.assembler import AssemblyPart
        from aria_os.mating_solver import MatingSolver, MatingConstraint
        parts = [
            AssemblyPart(step_path="/a.step", position=(0, 0, 0),
                         rotation=(0, 0, 0), name="gear_a"),
            AssemblyPart(step_path="/b.step", position=(0, 0, 0),
                         rotation=(0, 0, 0), name="gear_b"),
        ]
        c = MatingConstraint(
            type="gear_mesh", part_a="gear_a", part_b="gear_b",
            params={"od_a_mm": 40.0, "od_b_mm": 20.0, "axis_angle_deg": 0},
        )
        solved = MatingSolver().solve(parts, [c], context={})
        b = next(p for p in solved if p.name == "gear_b")
        # Center distance = (40+20)/2 = 30 along +X
        assert b.position[0] == pytest.approx(30.0)
        assert b.position[1] == pytest.approx(0.0)

    def test_shaft_into_bore(self):
        from aria_os.assembler import AssemblyPart
        from aria_os.mating_solver import MatingSolver, MatingConstraint
        parts = [
            AssemblyPart(step_path="/bearing.step", position=(100, 50, 20),
                         rotation=(0, 0, 0), name="bearing"),
            AssemblyPart(step_path="/shaft.step", position=(0, 0, 0),
                         rotation=(0, 0, 0), name="shaft"),
        ]
        c = MatingConstraint(
            type="shaft_into_bore", part_a="bearing", part_b="shaft",
            params={"insertion_depth_mm": 5.0},
        )
        solved = MatingSolver().solve(parts, [c], context={})
        shaft = next(p for p in solved if p.name == "shaft")
        assert shaft.position[0] == 100
        assert shaft.position[1] == 50
        assert shaft.position[2] == 15  # 20 - 5mm insertion

    def test_dowel_locate_rotation_inherited(self):
        from aria_os.assembler import AssemblyPart
        from aria_os.mating_solver import MatingSolver, MatingConstraint
        parts = [
            AssemblyPart(step_path="/plate.step", position=(10, 20, 0),
                         rotation=(0, 0, 45), name="plate"),
            AssemblyPart(step_path="/cover.step", position=(0, 0, 0),
                         rotation=(0, 0, 0), name="cover"),
        ]
        c = MatingConstraint(
            type="dowel_locate", part_a="plate", part_b="cover",
            params={"dowel_offset": [0, 0, 10]},
        )
        solved = MatingSolver().solve(parts, [c], context={})
        cover = next(p for p in solved if p.name == "cover")
        assert cover.position == (10, 20, 10)
        assert cover.rotation == (0, 0, 45)  # inherited
