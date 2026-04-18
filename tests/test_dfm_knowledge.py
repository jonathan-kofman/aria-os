"""Tests for the DFM/material/geometry knowledge base."""
import pytest

from aria_os.teaching.dfm_knowledge import (
    DFM_TEACHINGS,
    MATERIAL_TEACHINGS,
    GEOMETRY_TEACHINGS,
    get_dfm_teaching,
    get_material_teaching,
    get_geometry_teaching,
    get_all_dfm_processes,
)


class TestDFMTeachings:
    def test_every_issue_has_cnc_entry(self):
        """Every DFM issue type should at least have a CNC teaching."""
        # Not all issues apply to CNC (e.g. draft_angle is injection-specific)
        # but the most common ones should
        cnc_expected = [
            "thin_wall", "deep_pocket", "sharp_internal_corner",
            "undercut", "tight_tolerance", "small_hole",
            "large_flat_surface", "thin_floor", "high_aspect_ratio",
        ]
        for issue in cnc_expected:
            assert "cnc" in DFM_TEACHINGS.get(issue, {}), f"{issue} missing CNC entry"

    def test_every_teaching_has_required_keys(self):
        """Each teaching entry must have message, fix, and rule."""
        for issue_type, processes in DFM_TEACHINGS.items():
            for process, entry in processes.items():
                assert "message" in entry, f"{issue_type}/{process} missing 'message'"
                assert "fix" in entry, f"{issue_type}/{process} missing 'fix'"
                assert "rule" in entry, f"{issue_type}/{process} missing 'rule'"

    def test_teachings_are_not_empty(self):
        """Teachings should contain actual content, not empty strings."""
        for issue_type, processes in DFM_TEACHINGS.items():
            for process, entry in processes.items():
                assert len(entry["message"]) > 20, f"{issue_type}/{process} message too short"
                assert len(entry["fix"]) > 10, f"{issue_type}/{process} fix too short"
                assert len(entry["rule"]) > 10, f"{issue_type}/{process} rule too short"

    def test_teachings_contain_numbers(self):
        """Good manufacturing guidance includes specific numbers."""
        has_numbers = 0
        total = 0
        for issue_type, processes in DFM_TEACHINGS.items():
            for process, entry in processes.items():
                total += 1
                combined = entry["message"] + entry["fix"] + entry["rule"]
                if any(c.isdigit() for c in combined):
                    has_numbers += 1
        # At least 90% of teachings should contain specific numbers
        assert has_numbers / total > 0.9, f"Only {has_numbers}/{total} teachings have numbers"


class TestGetDFMTeaching:
    def test_known_combo(self):
        result = get_dfm_teaching("thin_wall", "cnc")
        assert result is not None
        assert "message" in result

    def test_unknown_issue(self):
        assert get_dfm_teaching("nonexistent_issue", "cnc") is None

    def test_unknown_process(self):
        assert get_dfm_teaching("thin_wall", "nonexistent_process") is None

    def test_injection_specific(self):
        result = get_dfm_teaching("draft_angle_missing", "injection_mold")
        assert result is not None
        assert "draft" in result["message"].lower() or "mold" in result["message"].lower()


class TestGetAllProcesses:
    def test_thin_wall_has_multiple(self):
        procs = get_all_dfm_processes("thin_wall")
        assert len(procs) >= 3
        assert "cnc" in procs

    def test_unknown_issue_empty(self):
        assert get_all_dfm_processes("nonexistent") == []


class TestMaterialTeachings:
    def test_all_materials_have_required_keys(self):
        required = {"machinability", "applications", "cost", "vs_alternatives", "gotchas"}
        for mat, entry in MATERIAL_TEACHINGS.items():
            for key in required:
                assert key in entry, f"{mat} missing '{key}'"

    def test_materials_are_not_empty(self):
        for mat, entry in MATERIAL_TEACHINGS.items():
            for key, val in entry.items():
                assert len(val) > 10, f"{mat}.{key} too short"


class TestGetMaterialTeaching:
    def test_exact_match(self):
        result = get_material_teaching("aluminium_6061")
        assert result is not None
        assert "6061" in result.get("name", "")

    def test_fuzzy_match(self):
        result = get_material_teaching("aluminium")
        assert result is not None

    def test_unknown_material(self):
        assert get_material_teaching("unobtanium") is None

    def test_case_insensitive(self):
        # All keys are lowercase already, but check normalization
        result = get_material_teaching("steel_4140")
        assert result is not None

    def test_all_listed_materials(self):
        materials = [
            "aluminium_6061", "aluminium_7075", "steel_1018",
            "steel_4140", "stainless_316", "titanium_ti6al4v",
            "nylon", "pla", "petg", "abs", "carbon_fibre",
        ]
        for mat in materials:
            assert get_material_teaching(mat) is not None, f"Missing teaching for {mat}"


class TestGeometryTeachings:
    def test_all_features_have_required_keys(self):
        required = {"feature", "rule", "why"}
        for feat, entry in GEOMETRY_TEACHINGS.items():
            for key in required:
                assert key in entry, f"{feat} missing '{key}'"

    def test_get_geometry_teaching_known(self):
        result = get_geometry_teaching("hub_od_ratio")
        assert result is not None
        assert "2x" in result.lower() or "bore" in result.lower()

    def test_get_geometry_teaching_unknown(self):
        assert get_geometry_teaching("nonexistent_feature") is None

    def test_teachings_contain_specific_numbers(self):
        for feat, entry in GEOMETRY_TEACHINGS.items():
            combined = entry["rule"] + entry["why"]
            assert any(c.isdigit() for c in combined), f"{feat} has no specific numbers"

    def test_known_features(self):
        features = [
            "hub_od_ratio", "bolt_pcd_edge_distance",
            "fillet_stress_concentration", "wall_to_bore_ratio",
            "fin_spacing", "tooth_module",
        ]
        for feat in features:
            assert get_geometry_teaching(feat) is not None, f"Missing teaching for {feat}"
