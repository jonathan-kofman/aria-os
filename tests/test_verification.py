"""W7 verification suite tests."""
from __future__ import annotations

import pytest


# --- W7.1 DFM rule engine ---------------------------------------------

class TestDfmCnc:
    """CNC 3-axis rules — wall, corner radius, deep pocket, hole-edge."""

    def test_wall_too_thin_aluminum(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"wall_mm": 0.8, "material": "AL 6061-T6"},
            stl_path=None, process="cnc_3axis")
        assert not report.passed
        assert any(i.category == "wall_too_thin" for i in report.issues)
        # Issue should be critical
        wt = next(i for i in report.issues if i.category == "wall_too_thin")
        assert wt.severity == "critical"

    def test_wall_ok_aluminum(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"wall_mm": 2.0, "material": "AL 6061-T6"},
            stl_path=None, process="cnc_3axis")
        wall_issues = [i for i in report.issues
                        if i.category == "wall_too_thin"]
        assert not wall_issues

    def test_inside_corner_too_tight(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"inside_radius_mm": 0.2}, stl_path=None, process="cnc_3axis")
        assert any("inside_corner" in i.category for i in report.issues)

    def test_deep_pocket_caught(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"pocket_depth_mm": 50, "pocket_width_mm": 5},
            stl_path=None, process="cnc_3axis")
        assert any("deep_pocket" in i.category for i in report.issues)

    def test_hole_to_edge_critical(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"edge_offset_mm": 3, "bolt_dia_mm": 6},
            stl_path=None, process="cnc_3axis")
        assert not report.passed
        assert any(i.category == "hole_to_edge" for i in report.issues)


class TestDfmSheetMetal:
    """Sheet metal: bend radius, hole-near-bend, flange length."""

    def test_bend_too_tight_for_6061(self):
        from aria_os.verification.dfm import run_dfm_rules
        # 6061-T6 needs 2.5×t — 2mm sheet → 5mm minimum
        report = run_dfm_rules(
            {"bend_radius_mm": 2.0, "thickness_mm": 2.0,
             "material": "AL 6061-T6"},
            stl_path=None, process="sheet_metal")
        assert not report.passed
        assert any("bend_radius" in i.category for i in report.issues)

    def test_bend_ok_for_mild_steel(self):
        """Mild steel needs only 1×t — 2mm sheet allows 2mm bend."""
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"bend_radius_mm": 2.0, "thickness_mm": 2.0,
             "material": "1018 mild steel"},
            stl_path=None, process="sheet_metal")
        bend_issues = [i for i in report.issues
                        if "bend_radius" in i.category]
        assert not bend_issues

    def test_hole_near_bend_warning(self):
        from aria_os.verification.dfm import run_dfm_rules
        # 1.5mm sheet, 1.5mm bend R, hole 3mm from bend → minimum is
        # 3·1.5 + 1.5 = 6mm, so 3mm is too close.
        report = run_dfm_rules(
            {"hole_to_bend_mm": 3, "thickness_mm": 1.5,
             "bend_radius_mm": 1.5},
            stl_path=None, process="sheet_metal")
        assert any("hole_near_bend" in i.category for i in report.issues)

    def test_flange_too_short_critical(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"flange_length_mm": 2, "thickness_mm": 1.5,
             "bend_radius_mm": 1.5},
            stl_path=None, process="sheet_metal")
        assert not report.passed
        assert any("flange_too_short" in i.category for i in report.issues)


class TestDfmFdm:
    """FDM: wall, overhang, min feature."""

    def test_wall_under_two_perimeters_critical(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"wall_mm": 0.6}, stl_path=None,
                                  process="fdm")
        crit = [i for i in report.issues
                if i.category == "fdm_wall_too_thin"]
        assert crit and crit[0].severity == "critical"

    def test_wall_marginal_warning(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"wall_mm": 1.0}, stl_path=None,
                                  process="fdm")
        warn = [i for i in report.issues
                if i.category == "fdm_wall_marginal"]
        assert warn and warn[0].severity == "warning"

    def test_overhang_caught(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"overhang_angle_deg": 60},
                                  stl_path=None, process="fdm")
        assert any("overhang" in i.category for i in report.issues)


class TestDfmSla:
    """SLA: thin walls + missing drain holes for hollow parts."""

    def test_hollow_no_drain_critical(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"hollow": True}, stl_path=None,
                                  process="sla")
        crit = [i for i in report.issues
                if i.category == "sla_no_drain"]
        assert crit and crit[0].severity == "critical"

    def test_hollow_with_drain_passes(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"hollow": True, "drain_holes": [
            {"x": 0, "y": 0, "dia": 3}]},
            stl_path=None, process="sla")
        assert not any(i.category == "sla_no_drain"
                        for i in report.issues)


class TestDfmCasting:
    """Casting: min wall + draft."""

    def test_sand_cast_thin_wall_critical(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules(
            {"wall_mm": 2, "casting_method": "sand"},
            stl_path=None, process="casting")
        assert not report.passed
        assert any(i.category == "cast_wall_too_thin"
                    for i in report.issues)

    def test_no_draft_warning(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"wall_mm": 5}, stl_path=None,
                                  process="casting")
        assert any(i.category == "cast_no_draft" for i in report.issues)

    def test_insufficient_draft_critical(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"wall_mm": 5, "draft_deg": 0.5},
                                  stl_path=None, process="casting")
        crit = [i for i in report.issues
                if i.category == "cast_draft_insufficient"]
        assert crit and crit[0].severity == "critical"


class TestDfmInjectionMold:
    """Injection: wall variation + minimum wall."""

    def test_wall_variation_warning(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"wall_mm": 1.5, "wall_max_mm": 3.0},
                                  stl_path=None, process="injection_mold")
        assert any("im_wall_variation" in i.category
                    for i in report.issues)

    def test_min_wall_critical(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"wall_mm": 0.5}, stl_path=None,
                                  process="injection_mold")
        crit = [i for i in report.issues
                if i.category == "im_wall_too_thin"]
        assert crit and crit[0].severity == "critical"


class TestDfmRegistry:
    """Process registry contract: every advertised process has rules,
    rules don't crash, score reflects severity counts."""

    def test_all_advertised_processes_have_rules(self):
        from aria_os.verification.dfm import (available_processes,
                                                 _REGISTRY)
        for proc in available_processes():
            assert _REGISTRY[proc], f"Process {proc} has no rules"

    def test_unknown_process_falls_back_to_cnc(self):
        from aria_os.verification.dfm import run_dfm_rules
        report = run_dfm_rules({"wall_mm": 0.5,
                                  "material": "AL 6061"},
                                 stl_path=None, process="laser_engrave")
        # Falls back to cnc_3axis rules → catches the 0.5mm wall
        assert not report.passed

    def test_score_decreases_with_severity(self):
        from aria_os.verification.dfm import run_dfm_rules
        clean = run_dfm_rules({}, stl_path=None, process="cnc_3axis")
        bad = run_dfm_rules(
            {"wall_mm": 0.5, "material": "AL 6061",
             "edge_offset_mm": 1, "bolt_dia_mm": 6},
            stl_path=None, process="cnc_3axis")
        assert clean.score == 1.0
        assert bad.score < clean.score

    def test_empty_spec_no_crash(self):
        """Rules must gracefully skip when spec is missing the
        relevant fields — not crash, not fire false positives."""
        from aria_os.verification.dfm import run_dfm_rules
        for proc in ("cnc_3axis", "sheet_metal", "fdm",
                      "sla", "casting", "injection_mold"):
            report = run_dfm_rules({}, stl_path=None, process=proc)
            # Empty spec → most rules skip; only "missing draft"
            # for casting fires.
            real_issues = [i for i in report.issues
                            if i.category != "cast_no_draft"]
            assert all(i.severity != "critical" for i in real_issues), (
                f"{proc} fired criticals on empty spec: "
                f"{[i.category for i in real_issues]}")


class TestToleranceStack:
    """W7.2 — concentric mate stack analysis. Worst-case + RSS."""

    def _planet_carrier_plan(self, bore_dim, bore_tol,
                              shaft_dim, shaft_tol):
        return [
            {"kind": "asmBegin", "params": {}},
            {"kind": "addComponent",
             "params": {"id": "carrier", "type": "plate"}},
            {"kind": "addComponent",
             "params": {"id": "planet", "type": "spur_gear"}},
            {"kind": "mateConcentric",
             "params": {"parts": ["carrier.bore", "planet.shaft"]}},
        ]

    def test_interference_caught(self):
        from aria_os.verification.tolerance_stack import analyze_stack
        # Bore 10±0.05 vs shaft 10.1±0.05 → bore min 9.95, shaft max
        # 10.15 → -0.20mm interference
        plan = self._planet_carrier_plan(10.0, 0.05, 10.1, 0.05)
        spec = {"component_dims": {
            "carrier": {"bore": {"nominal": 10.0, "tol": 0.05}},
            "planet":  {"shaft": {"nominal": 10.1, "tol": 0.05}}}}
        issues = analyze_stack(plan, spec)
        assert any(i.category == "tolerance_stack_interference"
                    for i in issues)

    def test_proper_clearance_passes(self):
        from aria_os.verification.tolerance_stack import analyze_stack
        # Bore 10.5±0.05, shaft 10.0±0.05 → 0.5mm nominal clearance,
        # worst case 0.4mm — ok.
        plan = self._planet_carrier_plan(10.5, 0.05, 10.0, 0.05)
        spec = {"component_dims": {
            "carrier": {"bore": {"nominal": 10.5, "tol": 0.05}},
            "planet":  {"shaft": {"nominal": 10.0, "tol": 0.05}}}}
        issues = analyze_stack(plan, spec)
        assert not any(i.severity == "critical" for i in issues)

    def test_iso_2768_default_tolerance(self):
        from aria_os.verification.tolerance_stack import iso_2768_tolerance
        assert iso_2768_tolerance(2) == 0.10     # <3mm band
        assert iso_2768_tolerance(20) == 0.20    # 3-30mm
        assert iso_2768_tolerance(100) == 0.30   # 30-120mm
        assert iso_2768_tolerance(300) == 0.50   # 120-400mm
        assert iso_2768_tolerance(500) == 0.80   # 400-1000mm
        # Coarse grade doubles
        assert iso_2768_tolerance(20, "c") == 0.40

    def test_no_mate_ops_returns_empty(self):
        from aria_os.verification.tolerance_stack import analyze_stack
        plan = [
            {"kind": "beginPlan", "params": {}},
            {"kind": "newSketch", "params": {"plane": "XY",
                                                "alias": "s"}},
            {"kind": "extrude",
             "params": {"sketch": "s", "distance": 5,
                          "operation": "new", "alias": "b"}},
        ]
        assert analyze_stack(plan, {}) == []


class TestDrawingAudit:
    """W7.3 — every GD&T frame must reference a real model feature
    AND every datum it cites must be declared somewhere."""

    def test_position_without_datums_critical(self):
        from aria_os.verification.drawing_audit import audit_drawing
        plan = [
            {"kind": "beginDrawing", "params": {}},
            {"kind": "newSheet", "params": {"alias": "sh1"}},
            {"kind": "addView", "params": {"alias": "v_top",
                                              "sheet": "sh1"}},
            {"kind": "gdtFrame",
             "params": {"view": "v_top", "feature": "bolt_holes",
                          "characteristic": "position",
                          "tolerance": 0.2}},
        ]
        issues = audit_drawing(plan, {})
        crit = [i for i in issues
                if i.category == "gdt_missing_datum"]
        assert crit and crit[0].severity == "critical"

    def test_position_with_datums_passes(self):
        from aria_os.verification.drawing_audit import audit_drawing
        plan = [
            {"kind": "beginDrawing", "params": {}},
            {"kind": "newSheet", "params": {"alias": "sh1"}},
            {"kind": "addView", "params": {"alias": "v_top",
                                              "sheet": "sh1"}},
            {"kind": "datumLabel",
             "params": {"view": "v_top", "feature": "back_face",
                          "label": "A"}},
            {"kind": "datumLabel",
             "params": {"view": "v_top", "feature": "side1",
                          "label": "B"}},
            {"kind": "datumLabel",
             "params": {"view": "v_top", "feature": "side2",
                          "label": "C"}},
            {"kind": "gdtFrame",
             "params": {"view": "v_top", "feature": "bolt_holes",
                          "characteristic": "position",
                          "tolerance": 0.2,
                          "datums": ["A", "B", "C"]}},
        ]
        issues = audit_drawing(plan, {})
        assert not any(i.severity == "critical" for i in issues)

    def test_undeclared_datum_caught(self):
        """gdtFrame cites datum X but no datumLabel ever declared X."""
        from aria_os.verification.drawing_audit import audit_drawing
        plan = [
            {"kind": "beginDrawing", "params": {}},
            {"kind": "newSheet", "params": {"alias": "sh1"}},
            {"kind": "addView", "params": {"alias": "v_top",
                                              "sheet": "sh1"}},
            {"kind": "gdtFrame",
             "params": {"view": "v_top", "feature": "back_face",
                          "characteristic": "perpendicularity",
                          "tolerance": 0.1, "datums": ["Z"]}},
        ]
        issues = audit_drawing(plan, {})
        # Will both flag missing datumLabel for Z AND missing one entirely
        assert any("Z" in i.message for i in issues)


class TestFeaGate:
    """W7.4 — closed-form cantilever + pressure vessel checks."""

    def test_cantilever_overstressed_critical(self):
        """1mm-thick × 10mm wide × 100mm long AL 6061 cantilever
        with 200N at the tip → way over yield."""
        from aria_os.verification.fea_gate import run_fea
        issues = run_fea(
            {"length_mm": 100, "width_mm": 10, "thickness_mm": 1,
             "material": "AL 6061-T6"},
            stl_path=None, loads={"point_n": 200})
        crit = [i for i in issues if i.category == "fea_stress_high"]
        assert crit and crit[0].severity == "critical"

    def test_cantilever_passes_with_thicker_section(self):
        """20mm-thick AL plate, same load → comfortable SF."""
        from aria_os.verification.fea_gate import run_fea
        issues = run_fea(
            {"length_mm": 100, "width_mm": 30, "thickness_mm": 20,
             "material": "AL 6061-T6"},
            stl_path=None, loads={"point_n": 200,
                                    "max_deflection_mm": 0.5})
        crit = [i for i in issues if i.severity == "critical"]
        assert not crit

    def test_pressure_vessel_thin_wall_critical(self):
        """0.5mm wall on 100mm OD AL vessel at 5MPa → blows up."""
        from aria_os.verification.fea_gate import run_fea
        issues = run_fea(
            {"od_mm": 100, "wall_mm": 0.5, "material": "AL 6061-T6"},
            stl_path=None, loads={"pressure_mpa": 5})
        assert any(i.category == "fea_pressure_vessel"
                    for i in issues)

    def test_no_load_returns_info(self):
        from aria_os.verification.fea_gate import run_fea
        issues = run_fea({"length_mm": 100, "thickness_mm": 5,
                            "width_mm": 10, "material": "steel"},
                            stl_path=None, loads={"unknown_field": 1})
        # No applicable check → info-level no-op
        assert all(i.severity == "info" for i in issues)


class TestVerifyPartTop:
    """Top-level verify_part stitches DFM + (optional) tolerance +
    (optional) drawing audit + (optional) FEA into one report."""

    def test_dfm_only_path(self):
        from aria_os.verification import verify_part
        report = verify_part(
            {"wall_mm": 0.5, "material": "AL"},
            process="cnc_3axis")
        assert not report.passed
        assert "dfm" in report.gates_run
        # Other gates skipped (no plan, no loads)
        skipped = {g for g, _ in report.gates_skipped}
        assert "fea" in skipped

    def test_passing_spec(self):
        from aria_os.verification import verify_part
        report = verify_part(
            {"wall_mm": 2.0, "material": "AL 6061"},
            process="cnc_3axis")
        assert report.passed
        assert report.score == 1.0

    def test_to_dict_serializable(self):
        from aria_os.verification import verify_part
        import json
        report = verify_part({"wall_mm": 0.5}, process="cnc_3axis")
        # Must JSON-serialize for the run_manifest sidecar
        json.dumps(report.to_dict())


class TestVerifyPartOpInPlan:
    """W7.5 — the dispatcher auto-appends a verifyPart op to every
    plan that produces real geometry, picking the right process from
    goal keywords."""

    def test_verify_op_appended_to_geometry_plan(self, monkeypatch):
        """Mock the LLM so the dispatcher returns a known plan; the
        op-append step must add a verifyPart at the end."""
        def fake_plan(goal, spec, *, quality, repo_root=None,
                       host_context=None, mode="new"):
            return [
                {"kind": "beginPlan", "params": {}},
                {"kind": "newSketch",
                 "params": {"plane": "XY", "alias": "s"}},
                {"kind": "sketchCircle",
                 "params": {"sketch": "s", "r": 50}},
                {"kind": "extrude",
                 "params": {"sketch": "s", "distance": 10,
                              "operation": "new", "alias": "b"}},
            ]
        monkeypatch.setattr(
            "aria_os.native_planner.dispatcher.plan_from_llm", fake_plan)

        from aria_os.native_planner.dispatcher import make_plan
        plan = make_plan("widget", {}, prefer_llm=True, quality="fast")
        assert plan[-1]["kind"] == "verifyPart"
        assert plan[-1]["params"]["process"] == "cnc_3axis"

    def test_verify_op_picks_sheet_metal_process(self, monkeypatch):
        def fake_plan(goal, spec, *, quality, repo_root=None,
                       host_context=None, mode="new"):
            return [
                {"kind": "beginPlan", "params": {}},
                {"kind": "newSketch",
                 "params": {"plane": "XY", "alias": "s"}},
                {"kind": "sketchRect",
                 "params": {"sketch": "s", "w": 100, "h": 50}},
                {"kind": "sheetMetalBase",
                 "params": {"sketch": "s", "thickness_mm": 1.5,
                              "alias": "panel"}},
            ]
        monkeypatch.setattr(
            "aria_os.native_planner.dispatcher.plan_from_llm", fake_plan)

        from aria_os.native_planner.dispatcher import make_plan
        plan = make_plan("sheet metal enclosure 100x50", {},
                          prefer_llm=True, quality="fast")
        # Last op should now be a verifyPart with sheet_metal process
        verify_op = plan[-1]
        assert verify_op["kind"] == "verifyPart"
        assert verify_op["params"]["process"] == "sheet_metal"

    def test_skip_verify_flag_honored(self, monkeypatch):
        def fake_plan(goal, spec, *, quality, repo_root=None,
                       host_context=None, mode="new"):
            return [
                {"kind": "beginPlan", "params": {}},
                {"kind": "newSketch",
                 "params": {"plane": "XY", "alias": "s"}},
                {"kind": "sketchCircle",
                 "params": {"sketch": "s", "r": 5}},
                {"kind": "extrude",
                 "params": {"sketch": "s", "distance": 5,
                              "operation": "new", "alias": "b"}},
            ]
        monkeypatch.setattr(
            "aria_os.native_planner.dispatcher.plan_from_llm", fake_plan)

        from aria_os.native_planner.dispatcher import make_plan
        plan = make_plan("widget", {"skip_verify": True},
                          prefer_llm=True, quality="fast")
        # No verifyPart appended
        assert all(op["kind"] != "verifyPart" for op in plan)

    def test_no_verify_op_for_drawing_only_plan(self, monkeypatch):
        """A pure-drawing plan (no body created) shouldn't trigger
        a DFM verify — there's no part to check."""
        def fake_plan(goal, spec, *, quality, repo_root=None,
                       host_context=None, mode="new"):
            return [
                {"kind": "beginDrawing", "params": {}},
                {"kind": "newSheet", "params": {"alias": "sh1"}},
                {"kind": "addView",
                 "params": {"alias": "v_top", "sheet": "sh1"}},
                {"kind": "addTitleBlock",
                 "params": {"sheet": "sh1", "title": "X"}},
            ]
        monkeypatch.setattr(
            "aria_os.native_planner.dispatcher.plan_from_llm", fake_plan)

        from aria_os.native_planner.dispatcher import make_plan
        plan = make_plan("just a title block", {},
                          prefer_llm=True, quality="fast")
        assert all(op["kind"] != "verifyPart" for op in plan)


class TestNegativeAcceptance:
    """W7.6 — 4 prompts crafted to fail specific gates. Each test
    proves the verifier catches the failure that would otherwise
    ship a broken part."""

    def test_undersized_aluminum_pocket(self):
        """0.8mm wall on AL CNC part — should be flagged critical."""
        from aria_os.verification import verify_part
        r = verify_part({"wall_mm": 0.8, "material": "AL 6061-T6"},
                          process="cnc_3axis")
        assert not r.passed
        assert any(i.category == "wall_too_thin" for i in r.issues)

    def test_sub_min_sheet_metal_bend(self):
        """6061-T6 at 2mm needs ≥5mm bend; we ask for 1mm."""
        from aria_os.verification import verify_part
        r = verify_part(
            {"bend_radius_mm": 1.0, "thickness_mm": 2.0,
             "material": "AL 6061-T6"},
            process="sheet_metal")
        assert not r.passed
        assert any("bend_radius" in i.category for i in r.issues)

    def test_contradictory_drawing_tolerances(self):
        """gdtFrame on bolt holes uses position w/o datums."""
        from aria_os.verification import verify_part
        plan = [
            {"kind": "beginDrawing", "params": {}},
            {"kind": "newSheet", "params": {"alias": "sh1"}},
            {"kind": "addView", "params": {"alias": "v_top",
                                              "sheet": "sh1"}},
            {"kind": "gdtFrame",
             "params": {"view": "v_top", "feature": "bolt_holes",
                          "characteristic": "position",
                          "tolerance": 0.2}},
        ]
        r = verify_part({"wall_mm": 5}, process="cnc_3axis", plan=plan)
        assert not r.passed
        assert any(i.category == "gdt_missing_datum"
                    for i in r.issues)

    def test_undersized_cantilever(self):
        """1mm-thick AL plate cantilever at 200N — should fail FEA."""
        from aria_os.verification import verify_part
        r = verify_part(
            {"length_mm": 100, "width_mm": 10, "thickness_mm": 1,
             "material": "AL 6061-T6", "wall_mm": 1},
            process="cnc_3axis", loads={"point_n": 200})
        assert not r.passed
        # Could fail on either DFM (1mm wall) or FEA (overstressed)
        cats = {i.category for i in r.issues}
        assert "wall_too_thin" in cats or "fea_stress_high" in cats
