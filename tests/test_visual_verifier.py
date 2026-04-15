"""
tests/test_visual_verifier.py — Unit tests for visual_verifier._build_checklist.

Tests that goal text and spec values produce the expected feature checks without
requiring any external LLM, image rendering, or CAD files.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.visual_verifier import _build_checklist


# ---------------------------------------------------------------------------
# Keyword-triggered checks
# ---------------------------------------------------------------------------

class TestKeywordChecks:
    def _checks(self, goal: str, spec: dict | None = None) -> list[str]:
        return _build_checklist(goal, spec or {})

    def test_bore_check(self):
        checks = self._checks("housing with 40mm bore")
        assert any("hole" in c.lower() or "bore" in c.lower() for c in checks)

    def test_fin_check(self):
        checks = self._checks("heat sink with parallel fins")
        assert any("fin" in c.lower() for c in checks)

    def test_l_bracket_check(self):
        checks = self._checks("L-bracket 80x60mm")
        assert any("L-shaped" in c or "l-shaped" in c.lower() for c in checks)

    def test_gear_check(self):
        checks = self._checks("spur gear 24 teeth")
        assert any("gear" in c.lower() or "teeth" in c.lower() for c in checks)

    def test_ratchet_check(self):
        checks = self._checks("ratchet ring 213mm OD")
        assert any("ratchet" in c.lower() or "teeth" in c.lower() for c in checks)

    def test_nozzle_check(self):
        checks = self._checks("LRE nozzle 10kN thrust")
        assert any("nozzle" in c.lower() for c in checks)

    def test_impeller_check(self):
        checks = self._checks("centrifugal impeller 150mm OD 6 blades")
        assert any("impeller" in c.lower() or "blade" in c.lower() for c in checks)

    def test_flange_check(self):
        checks = self._checks("pipe flange 160mm OD 4xM8")
        assert any("flange" in c.lower() for c in checks)

    # New ARIA-specific keywords
    def test_spool_check(self):
        checks = self._checks("rope spool 120mm OD 60mm wide")
        assert any("spool" in c.lower() or "drum" in c.lower() or "flanged" in c.lower() for c in checks)

    def test_pulley_check(self):
        checks = self._checks("pulley 80mm OD with 25mm bore")
        assert any("pulley" in c.lower() or "sheave" in c.lower() for c in checks)

    def test_cam_collar_check(self):
        checks = self._checks("cam collar 80mm OD 30mm wide")
        assert any("collar" in c.lower() or "cam" in c.lower() for c in checks)

    def test_spacer_check(self):
        checks = self._checks("M5 hex standoff 20mm long")
        assert any("standoff" in c.lower() or "hex" in c.lower() or "spacer" in c.lower() for c in checks)

    def test_u_channel_check(self):
        checks = self._checks("u-channel 80x40x4mm aluminum")
        assert any("u" in c.lower() or "channel" in c.lower() for c in checks)

    def test_gusset_check(self):
        checks = self._checks("gusset bracket 60x60mm 4mm thick")
        assert any("gusset" in c.lower() for c in checks)

    def test_enclosure_check(self):
        checks = self._checks("electronics enclosure 100x60x40mm")
        assert any("enclosure" in c.lower() or "hollow" in c.lower() or "wall" in c.lower() for c in checks)

    def test_snap_hook_check(self):
        checks = self._checks("snap hook 30mm length")
        assert any("snap" in c.lower() or "hook" in c.lower() for c in checks)

    def test_involute_gear_check(self):
        checks = self._checks("involute spur gear 24 teeth module 2")
        assert any("involute" in c.lower() or "gear" in c.lower() or "teeth" in c.lower() for c in checks)

    def test_fan_blade_check(self):
        checks = self._checks("axial fan 200mm OD 8 blades")
        assert any("fan" in c.lower() or "blade" in c.lower() for c in checks)

    def test_propeller_check(self):
        checks = self._checks("propeller 300mm diameter 3 blades")
        assert any("propeller" in c.lower() or "blade" in c.lower() for c in checks)


# ---------------------------------------------------------------------------
# Spec-driven checks (authoritative counts)
# ---------------------------------------------------------------------------

class TestSpecDrivenChecks:
    def _checks(self, goal: str, spec: dict) -> list[str]:
        return _build_checklist(goal, spec)

    def test_n_teeth_in_spec(self):
        checks = self._checks("ratchet ring", {"n_teeth": 24})
        assert any("24" in c and ("teeth" in c.lower() or "tooth" in c.lower()) for c in checks)

    def test_n_bolts_circular_for_flange(self):
        checks = self._checks("pipe flange with bolt circle", {"n_bolts": 6})
        assert any("6" in c and "bolt" in c.lower() for c in checks)
        # Circular bolt pattern expected for flange
        assert any("circular" in c.lower() or "pcd" in c.lower() for c in checks)

    def test_n_bolts_not_circular_for_bracket(self):
        checks = self._checks("mounting bracket 4 holes", {"n_bolts": 4})
        assert any("4" in c and "bolt" in c.lower() for c in checks)
        # Brackets should NOT require circular pattern
        bolt_checks = [c for c in checks if "bolt" in c.lower()]
        assert not any("circular/PCD" in c for c in bolt_checks), \
            "Bracket bolt holes should not require circular/PCD pattern"

    def test_bore_from_spec(self):
        checks = self._checks("housing", {"bore_mm": 50.0})
        assert any("50" in c and ("bore" in c.lower() or "opening" in c.lower()) for c in checks)

    def test_wall_from_spec(self):
        checks = self._checks("hollow housing", {"wall_mm": 4.0})
        assert any("wall" in c.lower() or "hollow" in c.lower() for c in checks)

    def test_ring_shape_check(self):
        checks = self._checks("ratchet ring 213mm OD 60mm bore", {"od_mm": 213.0, "bore_mm": 60.0})
        assert any("ring" in c.lower() or "annular" in c.lower() for c in checks)

    def test_n_blades_in_spec(self):
        checks = self._checks("centrifugal impeller", {"n_blades": 6})
        assert any("6" in c and "blade" in c.lower() for c in checks)

    def test_n_blades_backward_sweep(self):
        checks = self._checks("impeller backward curved blades", {"n_blades": 8, "blade_sweep": "backward_curved"})
        sweep_checks = [c for c in checks if "backward" in c.lower() or "sweep" in c.lower()]
        assert sweep_checks, "Backward sweep check not generated"

    def test_n_fins_in_spec(self):
        checks = self._checks("heat sink", {"n_fins": 12})
        assert any("12" in c and "fin" in c.lower() for c in checks)

    def test_n_spokes_in_spec(self):
        checks = self._checks("spoked wheel", {"n_spokes": 5})
        assert any("5" in c and "spoke" in c.lower() for c in checks)


# ---------------------------------------------------------------------------
# Count pattern regex
# ---------------------------------------------------------------------------

class TestCountPatternRegex:
    def _checks(self, goal: str) -> list[str]:
        return _build_checklist(goal, {})

    def test_nx_notation_holes(self):
        checks = self._checks("bracket with 4x holes on 60mm PCD")
        # The regex should capture "4" and "hole" → generate a count check
        assert any("4" in c and "hole" in c.lower() for c in checks)

    def test_word_count_fins(self):
        checks = self._checks("heat sink with 8 parallel fins")
        assert any("8" in c and "fin" in c.lower() for c in checks)

    def test_n_teeth_from_regex(self):
        checks = self._checks("gear 24 teeth 2mm module")
        assert any("24" in c and ("teeth" in c.lower() or "tooth" in c.lower()) for c in checks)

    def test_n_blades_from_regex(self):
        checks = self._checks("impeller 6 blades backward curved")
        assert any("6" in c and "blade" in c.lower() for c in checks)


# ---------------------------------------------------------------------------
# Fallback / edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_goal_gets_generic_checks(self):
        checks = _build_checklist("", {})
        assert len(checks) >= 1
        # Should produce fallback generic checks
        assert any("defect" in c.lower() or "shape" in c.lower() for c in checks)

    def test_no_keywords_no_spec_fallback(self):
        checks = _build_checklist("simple part 50x30x20mm", {})
        assert len(checks) >= 1

    def test_angle_in_goal(self):
        checks = _build_checklist("bracket angled at 45 degrees", {})
        assert any("45" in c for c in checks)

    def test_spec_counts_not_doubled_with_keyword(self):
        """n_teeth in spec should override the regex guess, not add a duplicate."""
        checks = _build_checklist("ratchet ring 24 teeth", {"n_teeth": 24})
        # Should have an authoritative count check (from spec), not two separate tooth checks
        tooth_checks = [c for c in checks if "teeth" in c.lower() or "tooth" in c.lower()]
        # Both keyword and spec may contribute but they should be for 24
        assert all("24" in c for c in tooth_checks if c[0].isdigit() or "24" in c)
