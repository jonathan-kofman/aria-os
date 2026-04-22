"""Tests for aria_os.fastener_selector."""

import pytest

from aria_os.fastener_selector import (
    select_fastener,
    list_supported_sizes,
    proof_load_table,
)


class TestSelectFastener:

    def test_small_load_picks_smallest(self):
        f = select_fastener(load_n=100, material="steel")
        # M3 proof load (12.9) ~= 4879 N, >> 200 N. Should pick M3.
        assert f["size"] == "M3"
        assert f["grade"] == "12.9"

    def test_medium_load_sizes_up(self):
        f = select_fastener(load_n=8000, material="steel")
        # M5 proof load 12.9 = 970 * 14.2 = 13774, required = 16000. Too small.
        # M6 = 970 * 20.1 = 19497, required = 16000 → M6 selected.
        assert f["size"] == "M6"

    def test_safety_factor_actually_applied(self):
        low = select_fastener(load_n=5000, material="steel", safety_factor=1.0)
        high = select_fastener(load_n=5000, material="steel", safety_factor=3.0)
        # Higher SF → larger bolt (or equal)
        assert list_supported_sizes().index(high["size"]) >= list_supported_sizes().index(low["size"])

    def test_actual_safety_factor_ge_requested(self):
        f = select_fastener(load_n=5000, material="steel", safety_factor=2.0)
        assert f["safety_factor_actual"] >= 2.0

    def test_marine_env_forces_stainless(self):
        f = select_fastener(load_n=1000, environment="marine")
        assert f["grade"] == "A4-80"

    def test_wet_env_picks_stainless_for_aluminium(self):
        f = select_fastener(load_n=500, material="aluminium", environment="wet")
        assert f["grade"] in ("A2-70", "A4-80")

    def test_dry_steel_uses_12_9(self):
        f = select_fastener(load_n=2000, material="steel", environment="dry")
        assert f["grade"] == "12.9"

    def test_length_override(self):
        f = select_fastener(load_n=2000, length_mm=40)
        assert f["length_mm"] == 40

    def test_default_length_is_preferred(self):
        f = select_fastener(load_n=100)
        # M3 preferred length is 10
        assert f["length_mm"] == 10

    def test_returns_mcmaster_pn(self):
        f = select_fastener(load_n=2000, material="steel")
        pn = f["mcmaster_pn"]
        assert pn is not None
        assert len(pn) > 0
        # McMaster-Carr PNs follow pattern like "91290A111" (digits + letter + digits)
        assert any(c.isdigit() for c in pn), f"PN '{pn}' contains no digits"
        assert any(c.isalpha() for c in pn), f"PN '{pn}' contains no letters"
        # For a 2000 N load the selector must pick at least M3 12.9
        assert f["size"] in ("M3", "M4", "M5", "M6")
        assert f["grade"] == "12.9"

    def test_returns_torque_spec(self):
        f = select_fastener(load_n=2000, material="steel")
        assert f["torque_spec_nm"] > 0

    def test_zero_load_raises(self):
        with pytest.raises(ValueError, match="load_n must be positive"):
            select_fastener(load_n=0)

    def test_negative_load_raises(self):
        with pytest.raises(ValueError):
            select_fastener(load_n=-100)

    def test_sf_below_one_raises(self):
        with pytest.raises(ValueError, match="safety_factor"):
            select_fastener(load_n=1000, safety_factor=0.5)

    def test_excessive_load_raises(self):
        # M16 12.9 proof ~= 152 kN. Need > that × SF 2 → 305 kN service load to overflow.
        with pytest.raises(ValueError, match="exceeds M16"):
            select_fastener(load_n=400_000, safety_factor=2.0)

    def test_head_type_is_socket_cap(self):
        f = select_fastener(load_n=1000)
        assert f["head_type"] == "socket_cap_iso4762"

    def test_all_required_keys_present(self):
        f = select_fastener(load_n=1500)
        required = {
            "size", "length_mm", "head_type", "grade", "mcmaster_pn",
            "torque_spec_nm", "proof_load_n", "required_load_n",
            "safety_factor_actual", "stress_area_mm2",
        }
        assert required.issubset(f.keys())


class TestLookups:

    def test_list_supported_sizes_ordered(self):
        sizes = list_supported_sizes()
        assert sizes[0] == "M3"
        assert sizes[-1] == "M16"
        assert len(sizes) == 9

    def test_proof_load_table_12_9(self):
        t = proof_load_table("12.9")
        assert "M3" in t and "M16" in t
        # Monotonic: larger bolts have more proof load.
        values = [t[s] for s in list_supported_sizes()]
        assert values == sorted(values)

    def test_proof_load_table_stainless(self):
        t = proof_load_table("A2-70")
        assert t["M8"] < proof_load_table("12.9")["M8"]

    def test_unknown_grade_raises(self):
        with pytest.raises(ValueError, match="unknown grade"):
            proof_load_table("fake_grade")
