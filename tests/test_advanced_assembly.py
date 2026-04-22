"""
Tests for the advanced-assembly layer (#1–#7 from the user's roadmap):
linear motion, BLDC + propellers, load-rated bearings, composites, standards,
dynamics, export control.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Linear motion components
# ---------------------------------------------------------------------------

class TestLinearMotion:
    def test_mgn_rail_registered(self):
        from aria_os.components import catalog
        spec = catalog.get("MGN12_rail_500mm")
        assert spec is not None
        assert spec.subcategory == "profile_rail"
        assert spec.dimensions["length_mm"] == 500

    def test_ballscrew_has_load_rating(self):
        from aria_os.components import catalog
        spec = catalog.get("SFU1605_L500mm")
        assert spec is not None
        assert spec.dynamic_load_n is not None
        assert spec.dynamic_load_n > 10000

    def test_mgn_carriage_registered(self):
        from aria_os.components import catalog
        spec = catalog.get("MGN12H_block")
        assert spec is not None
        assert spec.dynamic_load_n == pytest.approx(2800.0)
        assert spec.subcategory == "linear_carriage"
        assert spec.dimensions["carriage_width_mm"] == 27

    def test_gt2_pulley(self):
        from aria_os.components import catalog
        spec = catalog.get("GT2_20T_bore5_w6")
        assert spec is not None
        assert spec.dimensions["n_teeth"] == 20


# ---------------------------------------------------------------------------
# BLDC motors + propellers
# ---------------------------------------------------------------------------

class TestBLDCAndProps:
    def test_bldc_motor_registered(self):
        from aria_os.components import catalog
        spec = catalog.get("2306-1800KV")
        assert spec is not None
        assert spec.category == "motor"
        assert spec.subcategory == "bldc_outrunner"
        assert spec.dimensions["kv"] == 1800

    def test_propeller_registered(self):
        from aria_os.components import catalog
        spec = catalog.get("5x4.3_3blade")
        assert spec is not None
        assert spec.category == "propulsion"
        assert spec.dimensions["n_blades"] == 3

    def test_large_propeller_flagged_ear_controlled(self):
        """Props over 24" are EAR-classified, not EAR99."""
        from aria_os.components import catalog
        spec = catalog.get("24x7.2_2blade")
        assert spec is not None
        assert spec.export_control != "EAR99"


# ---------------------------------------------------------------------------
# Load-rated bearing selection
# ---------------------------------------------------------------------------

class TestBearingSelection:
    def test_select_bearing_returns_viable(self):
        from aria_os.components.bearings import select_bearing
        spec, life = select_bearing(
            bore_mm=20, load_radial_n=500, rpm=1000, target_life_hours=20000,
        )
        assert spec is not None
        assert life >= 20000

    def test_select_bearing_fails_impossible_load(self):
        from aria_os.components.bearings import select_bearing
        spec, life = select_bearing(
            bore_mm=10, load_radial_n=100000, rpm=1000, target_life_hours=20000,
        )
        # 100kN on a bore-10 bearing is crazy — nothing fits
        assert spec is None
        assert life == 0


# ---------------------------------------------------------------------------
# Composites
# ---------------------------------------------------------------------------

class TestComposites:
    def test_materials_registered(self):
        from aria_os.composites import MATERIALS
        assert "IM7/5320-1" in MATERIALS
        assert "T800S/3900-2" in MATERIALS
        assert len(MATERIALS) >= 5
        # Verify actual material property fields are populated
        im7 = MATERIALS["IM7/5320-1"]
        assert im7.E1_gpa > 100, "IM7 fibre-direction modulus should exceed 100 GPa"
        assert im7.E2_gpa > 0
        assert 0 < im7.cured_ply_thickness_mm < 1.0

    def test_quasi_iso_is_symmetric_and_balanced(self):
        from aria_os.composites import quasi_isotropic_8ply
        qi = quasi_isotropic_8ply()
        assert qi.is_symmetric()
        assert qi.is_balanced()
        assert qi.meets_10_percent_rule()
        assert qi.validate() == []

    def test_unbalanced_stackup_flagged(self):
        from aria_os.composites import Stackup, Ply
        stk = Stackup(name="bad", plies=[
            Ply("IM7/5320-1", 0), Ply("IM7/5320-1", 45), Ply("IM7/5320-1", 0),
            # No -45 anywhere → unbalanced
        ])
        warnings = stk.validate()
        assert any("not balanced" in w.lower() for w in warnings)

    def test_stackup_total_thickness(self):
        from aria_os.composites import quasi_isotropic_8ply
        qi = quasi_isotropic_8ply()
        # 8 plies × 0.125mm (IM7/5320-1) = 1.0mm
        assert qi.total_thickness_mm() == pytest.approx(1.0, abs=0.01)

    def test_mass_per_area(self):
        from aria_os.composites import quasi_isotropic_8ply
        qi = quasi_isotropic_8ply()
        # 1.58 g/cm³ × 0.1cm = 158 g/m²; 8 plies × = 1264 g/m²
        assert qi.total_mass_per_area_g_m2() == pytest.approx(1580, rel=0.01)

    def test_clt_moduli_qi_isotropy(self):
        """QI laminate must be in-plane isotropic: Ex == Ey, Gxy linked to Ex via Poisson."""
        from aria_os.composites import quasi_isotropic_8ply, homogenized_in_plane_moduli
        qi = quasi_isotropic_8ply()
        mods = homogenized_in_plane_moduli(qi)
        assert mods["Ex_gpa"] == pytest.approx(mods["Ey_gpa"], rel=0.02)
        expected_gxy = mods["Ex_gpa"] / (2 * (1 + mods["vxy"]))
        assert mods["Gxy_gpa"] == pytest.approx(expected_gxy, rel=0.05)

    def test_clt_qi_known_value(self):
        """Known-answer test: QI carbon Ex ≈ (3/8)·E1 + (5/8)·E2 (rule of mixtures)."""
        from aria_os.composites import quasi_isotropic_8ply, homogenized_in_plane_moduli, MATERIALS
        qi = quasi_isotropic_8ply("IM7/5320-1")
        mods = homogenized_in_plane_moduli(qi)
        E1 = MATERIALS["IM7/5320-1"].E1_gpa
        E2 = MATERIALS["IM7/5320-1"].E2_gpa
        analytical_qi = (3/8) * E1 + (5/8) * E2  # ≈ 67 GPa for IM7
        assert mods["Ex_gpa"] == pytest.approx(analytical_qi, rel=0.20)

    def test_clt_unidirectional_anisotropy(self):
        """All-zero laminate: Ex==E1, Ey==E2, anisotropy ratio matches E1/E2."""
        from aria_os.composites import Stackup, Ply, homogenized_in_plane_moduli, MATERIALS
        ud = Stackup(name="UD0", plies=[Ply("IM7/5320-1", 0) for _ in range(8)])
        mods = homogenized_in_plane_moduli(ud)
        E1 = MATERIALS["IM7/5320-1"].E1_gpa
        E2 = MATERIALS["IM7/5320-1"].E2_gpa
        assert mods["Ex_gpa"] == pytest.approx(E1, rel=0.05)
        assert mods["Ey_gpa"] == pytest.approx(E2, rel=0.10)
        assert mods["Ex_gpa"] / mods["Ey_gpa"] == pytest.approx(E1 / E2, rel=0.10)

    def test_clt_cross_ply_square_symmetric(self):
        """[0/90]s laminate: Ex == Ey by symmetry."""
        from aria_os.composites import Stackup, Ply, homogenized_in_plane_moduli
        cp = Stackup(name="cp", plies=[Ply("IM7/5320-1", a) for a in (0, 90, 90, 0)])
        mods = homogenized_in_plane_moduli(cp)
        assert mods["Ex_gpa"] == pytest.approx(mods["Ey_gpa"], rel=0.01)

    def test_clt_45_rotation_lower_than_zero(self):
        """A [+45/-45]s laminate must have Ex < UD-zero Ex (off-axis is softer)."""
        from aria_os.composites import Stackup, Ply, homogenized_in_plane_moduli
        bias = Stackup(name="bias", plies=[Ply("IM7/5320-1", a) for a in (45, -45, -45, 45)])
        zero = Stackup(name="zero", plies=[Ply("IM7/5320-1", 0) for _ in range(4)])
        m_bias = homogenized_in_plane_moduli(bias)
        m_zero = homogenized_in_plane_moduli(zero)
        assert m_bias["Ex_gpa"] < m_zero["Ex_gpa"]
        # ±45 has high shear modulus
        assert m_bias["Gxy_gpa"] > m_zero["Gxy_gpa"]

    def test_abaqus_export(self):
        from aria_os.composites import quasi_isotropic_8ply, stackup_to_abaqus_comp_layup
        out = stackup_to_abaqus_comp_layup(quasi_isotropic_8ply())
        assert "*COMPOSITE LAYUP" in out
        assert "SYMMETRIC" in out


# ---------------------------------------------------------------------------
# Standards (in manufacturing-core)
# ---------------------------------------------------------------------------

class TestExpandedStandards:
    def test_aerospace_standards_present(self):
        from manufacturing_core.knowledge.standards import get_standard
        as9100 = get_standard("as9100")
        assert as9100 is not None
        assert "configuration_management" in as9100.common_clauses
        mil = get_standard("mil_std_810")
        assert mil is not None
        assert "method_514" in mil.common_clauses  # vibration method
        far = get_standard("far_part_23_25")
        assert far is not None
        assert "limit_load_factor" in far.common_clauses

    def test_automotive_standards_present(self):
        from manufacturing_core.knowledge.standards import get_standard
        fmvss = get_standard("fmvss")
        assert fmvss is not None
        assert "208_occupant_crash" in fmvss.common_clauses
        fia = get_standard("fia_f1_technical")
        assert fia is not None
        assert "survival_cell" in fia.common_clauses

    def test_itar_standard_present(self):
        from manufacturing_core.knowledge.standards import get_standard
        s = get_standard("itar_usml")
        assert s is not None
        assert "category_IV" in s.common_clauses  # missiles

    def test_astm_a36(self):
        from manufacturing_core.knowledge.standards import get_standard
        s = get_standard("astm_a36")
        assert s is not None
        assert "yield_strength" in s.common_clauses


# ---------------------------------------------------------------------------
# Dynamics
# ---------------------------------------------------------------------------

class TestDynamics:
    def test_fk_zero_joints_returns_origin_plus_links(self):
        from aria_os.dynamics import simple_forward_kinematics
        joints = [{"type": "revolute", "axis": [0, 0, 1]},
                  {"type": "revolute", "axis": [0, 1, 0]}]
        link_lengths = [100, 50]  # mm
        pose = simple_forward_kinematics(joints, link_lengths, [0, 0])
        # With zero joint angles, tip should be at (0, 0, 150)
        assert pose.x_mm == pytest.approx(0, abs=0.01)
        assert pose.y_mm == pytest.approx(0, abs=0.01)
        assert pose.z_mm == pytest.approx(150, abs=0.01)

    def test_reach_analysis(self):
        from aria_os.dynamics import compute_reach
        joints = [
            {"type": "revolute", "axis": [0, 0, 1], "range_deg": [-180, 180]},
            {"type": "revolute", "axis": [0, 1, 0], "range_deg": [-90, 90]},
        ]
        link_lengths = [100, 100]
        report = compute_reach(joints, link_lengths, n_samples=500)
        assert report.min_reach_mm >= 0
        # Max reach should approach total link length
        assert report.max_reach_mm > 150
        assert report.workspace_volume_mm3 > 0

    def test_trajectory_planning(self):
        from aria_os.dynamics import plan_joint_trajectory
        traj = plan_joint_trajectory(
            start=[0.0, 0.0], end=[1.0, 2.0],
            max_velocity_rad_s=[1.0, 1.0], dt=0.01,
        )
        assert len(traj.times_s) > 10
        # Start and end match
        assert traj.joint_positions[0] == pytest.approx([0.0, 0.0])
        assert traj.joint_positions[-1] == pytest.approx([1.0, 2.0])
        # Slowest joint (0 -> 2 @ 1 rad/s) sets duration ≈ 2s
        assert traj.times_s[-1] == pytest.approx(2.0, rel=0.05)

    def test_pinocchio_availability_is_bool(self):
        from aria_os.dynamics import pinocchio_available
        result = pinocchio_available()
        # Whether or not it's installed, the function must return a Python bool (not truthy int)
        assert isinstance(result, bool)
        assert result in (True, False)


# ---------------------------------------------------------------------------
# Export control
# ---------------------------------------------------------------------------

class TestExportControl:
    def test_components_default_ear99(self):
        from aria_os.components import catalog
        spec = catalog.get("M6x20_12.9")
        assert spec.export_control == "EAR99"
        assert not spec.is_itar
        assert not spec.is_export_controlled

    def test_classification_rank(self):
        from aria_os.export_control import classification_rank
        assert classification_rank("EAR99") == 0
        assert classification_rank("EAR-9A991") == 1
        assert classification_rank("ITAR-IV") == 2

    def test_most_restrictive(self):
        from aria_os.export_control import most_restrictive
        assert most_restrictive(["EAR99", "EAR99"]) == "EAR99"
        assert most_restrictive(["EAR99", "EAR-9A991"]) == "EAR-9A991"
        assert most_restrictive(["EAR99", "ITAR-IV"]) == "ITAR-IV"

    def test_classify_clean_assembly(self):
        from aria_os.export_control import classify_assembly
        bom = {"purchased": [
            {"designation": "M6x20_12.9", "export_control": "EAR99"},
            {"designation": "6205", "export_control": "EAR99"},
        ]}
        report = classify_assembly(bom)
        assert report.overall_classification == "EAR99"
        assert not report.is_itar
        assert report.flagged_components == []
        assert report.warnings == []

    def test_classify_itar_assembly_warns(self):
        from aria_os.export_control import classify_assembly
        bom = {"purchased": [
            {"designation": "standard_bolt", "export_control": "EAR99"},
            {"designation": "missile_fin_actuator", "export_control": "ITAR-IV"},
        ]}
        report = classify_assembly(bom)
        assert report.is_itar
        assert "missile_fin_actuator" in report.flagged_components
        assert len(report.warnings) > 0

    def test_millforge_destination_check_refuses_itar_to_non_allowlisted(self):
        from aria_os.export_control import ExportControlReport, check_millforge_destination_ok
        report = ExportControlReport(
            overall_classification="ITAR-IV",
            is_itar=True, is_controlled=True,
            flagged_components=["itar_part"],
        )
        ok, reason = check_millforge_destination_ok(report, "https://millforge.example.com")
        assert not ok
        assert "ITAR" in reason and "allow-list" in reason

    def test_millforge_destination_ok_for_clean(self):
        from aria_os.export_control import ExportControlReport, check_millforge_destination_ok
        report = ExportControlReport(overall_classification="EAR99", is_itar=False, is_controlled=False)
        ok, reason = check_millforge_destination_ok(report, "https://anywhere.com")
        assert ok is True
        # EAR99 clean parts should never trigger a refusal — reason string is empty
        assert "ITAR" not in reason and "allow-list" not in reason

    def test_millforge_destination_localhost_allowed_for_itar_default(self):
        from aria_os.export_control import ExportControlReport, check_millforge_destination_ok
        report = ExportControlReport(
            overall_classification="ITAR-IV", is_itar=True, is_controlled=True,
            flagged_components=["itar_part"],
        )
        # localhost is in the default allow-list
        ok, reason = check_millforge_destination_ok(report, "http://localhost:8000")
        assert ok is True
        # A random external host must still be refused
        blocked_ok, blocked_reason = check_millforge_destination_ok(report, "https://external.example.com")
        assert blocked_ok is False
        assert "ITAR" in blocked_reason

    def test_millforge_destination_env_allowlist(self, monkeypatch):
        from aria_os.export_control import ExportControlReport, check_millforge_destination_ok
        monkeypatch.setenv(
            "MANUFACTURING_CORE_ITAR_ALLOWED_ENDPOINTS",
            "https://us-cleared.example.com",
        )
        report = ExportControlReport(
            overall_classification="ITAR-IV", is_itar=True, is_controlled=True,
            flagged_components=["itar_part"],
        )
        ok, _ = check_millforge_destination_ok(
            report, "https://us-cleared.example.com/api"
        )
        assert ok
        ok, _ = check_millforge_destination_ok(
            report, "https://other-server.example.com/api"
        )
        assert not ok

    def test_url_substring_matching_no_longer_fools_check(self, monkeypatch):
        """Old bug: 'us' substring would match 'https://russia.ru/us-data/'.
        New logic uses prefix allow-list so this URL is blocked."""
        from aria_os.export_control import ExportControlReport, check_millforge_destination_ok
        monkeypatch.delenv("MANUFACTURING_CORE_ITAR_ALLOWED_ENDPOINTS", raising=False)
        report = ExportControlReport(
            overall_classification="ITAR-IV", is_itar=True, is_controlled=True,
            flagged_components=["itar_part"],
        )
        ok, _ = check_millforge_destination_ok(report, "https://russia.ru/us-data/")
        assert not ok

    def test_cloud_llm_refused_for_itar(self):
        from aria_os.export_control import ExportControlReport, check_cloud_llm_ok
        report = ExportControlReport(overall_classification="ITAR-IV", is_itar=True, is_controlled=True)
        ok, reason = check_cloud_llm_ok(report)
        assert not ok
        assert "cloud" in reason.lower() or "ITAR" in reason

    def test_cloud_llm_ok_for_ear99(self):
        from aria_os.export_control import ExportControlReport, check_cloud_llm_ok
        report = ExportControlReport(overall_classification="EAR99", is_itar=False, is_controlled=False)
        ok, reason = check_cloud_llm_ok(report)
        assert ok is True
        # EAR99 content has no cloud restriction — reason must be empty (no refusal message)
        assert reason == "" or reason is None

    def test_bom_annotated_with_export_control(self):
        from aria_os.assembly_bom import generate_bom
        config = {"name": "test", "parts": [
            {"id": "b1", "component": "M6x20_12.9"},
        ]}
        bom = generate_bom(config)
        assert "export_control" in bom
        assert bom["export_control"]["overall_classification"] == "EAR99"


# ---------------------------------------------------------------------------
# End-to-end integration: hypercar suspension upright
# ---------------------------------------------------------------------------

class TestIntegrationHypercarUpright:
    """Proof-of-concept: a hypercar suspension upright with linear motion,
    bearings, standard fasteners — end-to-end BOM + export check."""

    def test_upright_bom_generation(self):
        from aria_os.assembly_bom import generate_bom
        config = {
            "name": "hypercar_front_upright",
            "parts": [
                {"id": "upright", "step": "/tmp/upright.step"},
                # 2x wheel bearings (front)
                {"id": "b_outer", "component": "6205"},
                {"id": "b_inner", "component": "6205"},
                # 6x M10 bolts for hub mounting
                *[{"id": f"b{i}", "component": "M10x30_12.9"} for i in range(6)],
                # 4x M8 caliper mount bolts
                *[{"id": f"c{i}", "component": "M8x25_12.9"} for i in range(4)],
            ],
        }
        bom = generate_bom(config)
        # 1 fabricated upright + 2 bearings + 6 + 4 bolts = 13 parts
        assert bom["summary"]["total_parts"] == 13
        assert bom["summary"]["fabricated_count"] == 1
        assert bom["summary"]["purchased_count"] == 12
        # Should be unrestricted
        assert bom["export_control"]["overall_classification"] == "EAR99"
        assert bom["summary"]["total_purchased_cost_usd"] > 0
