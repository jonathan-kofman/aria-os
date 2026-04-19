"""
tests/test_kicad_footprint_lib.py

Probe tests for aria_os.ecad.kicad_footprint_lib.

Verifies:
  1. Indexing produces > 10k footprints (KiCad 10 has ~15k)
  2. Six common packages resolve correctly
  3. load_footprint_sexpr returns a parseable (footprint ...) block
     with Reference/Value stripped

Skips gracefully if KiCad isn't installed.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

# Skip the entire module if KiCad is not installed (CI without KiCad)
from aria_os.ecad.drc_check import kicad_share_dir as _kicad_share_dir

_KICAD_AVAILABLE = _kicad_share_dir() is not None

pytestmark = pytest.mark.skipif(
    not _KICAD_AVAILABLE,
    reason="KiCad not installed -- skipping footprint library tests",
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def idx():
    """Build (or load) the footprint index once per test session."""
    from aria_os.ecad.kicad_footprint_lib import index_footprints
    data = index_footprints()
    return data


# --------------------------------------------------------------------------- #
# Index structure tests
# --------------------------------------------------------------------------- #

class TestIndexStructure:
    def test_footprints_dir_found(self, idx):
        assert idx["footprints_dir"] is not None
        assert Path(idx["footprints_dir"]).is_dir()

    def test_library_count(self, idx):
        """KiCad 10 ships ~155 .pretty libs."""
        assert len(idx["libs"]) >= 100, (
            f"Expected >= 100 libs, got {len(idx['libs'])}"
        )

    def test_footprint_count(self, idx):
        """KiCad 10 ships ~15k footprints; require at least 10k."""
        total = sum(len(v) for v in idx["libs"].values())
        assert total > 10_000, f"Expected > 10k footprints, got {total}"

    def test_by_name_populated(self, idx):
        """by_name must have the same scale as total footprints."""
        assert len(idx["by_name"]) > 10_000

    def test_version_field(self, idx):
        assert idx.get("_version") == 2

    def test_libs_contain_expected_families(self, idx):
        """Key library families must be present."""
        expected = {
            "Resistor_SMD", "Capacitor_SMD", "Package_QFP",
            "Package_DFN_QFN", "Package_TO_SOT_SMD", "Connector_USB",
        }
        present = set(idx["libs"].keys())
        missing = expected - present
        assert not missing, f"Missing expected libs: {missing}"


# --------------------------------------------------------------------------- #
# lookup_footprint: the 6 canonical probes
# --------------------------------------------------------------------------- #

class TestLookupFootprint:
    """Verify the six parts called out in the build spec resolve."""

    def _assert_hit(self, fp, *, contains_lib=None, contains_fp=None):
        assert fp is not None, "lookup_footprint returned None"
        assert "lib" in fp and "fp" in fp and "path" in fp
        assert Path(fp["path"]).is_file(), (
            f"Resolved path does not exist: {fp['path']}"
        )
        if contains_lib:
            assert contains_lib.lower() in fp["lib"].lower(), (
                f"Expected lib containing '{contains_lib}', got '{fp['lib']}'"
            )
        if contains_fp:
            assert contains_fp.lower() in fp["fp"].lower(), (
                f"Expected fp containing '{contains_fp}', got '{fp['fp']}'"
            )

    def test_0805_resistor(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("R_0805", idx=idx)
        self._assert_hit(fp, contains_lib="Resistor_SMD", contains_fp="0805")

    def test_0805_resistor_via_package_hint(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("100R", package="0805", idx=idx)
        self._assert_hit(fp, contains_lib="Resistor_SMD", contains_fp="0805")

    def test_0603_capacitor(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("C_0603", idx=idx)
        self._assert_hit(fp, contains_lib="Capacitor_SMD", contains_fp="0603")

    def test_0603_capacitor_via_package_hint(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("100nF", package="C0603", idx=idx)
        self._assert_hit(fp, contains_lib="Capacitor_SMD", contains_fp="0603")

    def test_lqfp64(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("STM32F405RGT6", package="LQFP-64", idx=idx)
        self._assert_hit(fp, contains_lib="Package_QFP", contains_fp="LQFP-64")
        # Must NOT be the exposed-pad variant when plain LQFP-64 is available
        assert "1EP" not in fp["fp"], (
            f"Expected plain LQFP-64, got exposed-pad variant: {fp['fp']}"
        )

    def test_lqfp64_direct(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("LQFP-64", idx=idx)
        self._assert_hit(fp, contains_lib="Package_QFP", contains_fp="LQFP-64")

    def test_qfn32(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("nRF52832", package="QFN-32", idx=idx)
        self._assert_hit(fp, contains_lib="Package_DFN_QFN", contains_fp="QFN-32")

    def test_qfn32_direct(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("QFN-32", idx=idx)
        self._assert_hit(fp, contains_lib="Package_DFN_QFN")

    def test_sot23(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("LM4041", package="SOT-23", idx=idx)
        self._assert_hit(fp, contains_lib="Package_TO_SOT_SMD", contains_fp="SOT-23")

    def test_sot23_direct(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("SOT-23", idx=idx)
        self._assert_hit(fp, contains_lib="Package_TO_SOT_SMD", contains_fp="SOT-23")

    def test_usbc(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("USB-C", idx=idx)
        self._assert_hit(fp, contains_lib="Connector_USB", contains_fp="USB_C")

    def test_usbc_via_package_hint(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("USB3320C-EZK", package="USB-C", idx=idx)
        self._assert_hit(fp, contains_lib="Connector_USB")

    def test_unknown_returns_none(self, idx):
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("XYZZY_NONEXISTENT_PART_12345", idx=idx)
        assert fp is None

    def test_package_hint_takes_priority(self, idx):
        """When package hint is given it should dominate over value."""
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        # Value is nonsense; package hint is valid
        fp = lookup_footprint("some-mcu-chip", package="SOT-23", idx=idx)
        self._assert_hit(fp, contains_fp="SOT-23")


# --------------------------------------------------------------------------- #
# load_footprint_sexpr tests
# --------------------------------------------------------------------------- #

class TestLoadFootprintSexpr:
    def _get_r0805_path(self, idx) -> str:
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint
        fp = lookup_footprint("R_0805", idx=idx)
        assert fp is not None
        return fp["path"], fp["fp"]

    def test_returns_string(self, idx):
        from aria_os.ecad.kicad_footprint_lib import load_footprint_sexpr
        path, name = self._get_r0805_path(idx)
        sexpr = load_footprint_sexpr(path, name)
        assert isinstance(sexpr, str)
        assert len(sexpr) > 100

    def test_starts_with_footprint_tag(self, idx):
        from aria_os.ecad.kicad_footprint_lib import load_footprint_sexpr
        path, name = self._get_r0805_path(idx)
        sexpr = load_footprint_sexpr(path, name)
        assert sexpr.strip().startswith('(footprint')

    def test_reference_stripped(self, idx):
        """Reference property value must be empty string after stripping."""
        from aria_os.ecad.kicad_footprint_lib import load_footprint_sexpr
        path, name = self._get_r0805_path(idx)
        sexpr = load_footprint_sexpr(path, name)
        # "REF**" or any other non-empty reference should not appear in
        # the property "Reference" value slot
        assert '"REF**"' not in sexpr, (
            "Reference property was not stripped from footprint sexpr"
        )

    def test_value_stripped(self, idx):
        """Value property should not echo the footprint name."""
        from aria_os.ecad.kicad_footprint_lib import load_footprint_sexpr
        path, name = self._get_r0805_path(idx)
        sexpr = load_footprint_sexpr(path, name)
        # The property "Value" line must have "" not the footprint name
        m = re.search(r'\(property\s+"Value"\s+"([^"]*)"', sexpr)
        assert m is not None, "Could not find property Value in sexpr"
        assert m.group(1) == "", (
            f"Expected Value property to be empty, got: '{m.group(1)}'"
        )

    def test_contains_pad(self, idx):
        """A real footprint sexpr must contain at least one pad."""
        from aria_os.ecad.kicad_footprint_lib import load_footprint_sexpr
        path, name = self._get_r0805_path(idx)
        sexpr = load_footprint_sexpr(path, name)
        assert "(pad " in sexpr

    def test_bad_path_returns_none(self, idx):
        from aria_os.ecad.kicad_footprint_lib import load_footprint_sexpr
        result = load_footprint_sexpr("/nonexistent/path/foo.kicad_mod", "foo")
        assert result is None

    def test_lqfp64_sexpr(self, idx):
        """LQFP-64 footprint must contain 64 pads."""
        from aria_os.ecad.kicad_footprint_lib import lookup_footprint, load_footprint_sexpr
        fp = lookup_footprint("LQFP-64", idx=idx)
        assert fp is not None
        sexpr = load_footprint_sexpr(fp["path"], fp["fp"])
        assert sexpr is not None
        pad_count = len(re.findall(r'\(pad\s+"?\d+"?', sexpr))
        assert pad_count == 64, f"Expected 64 pads in LQFP-64, got {pad_count}"


# --------------------------------------------------------------------------- #
# Normalize helper tests (unit tests, no KiCad required -- but module is already
# skipped above if KiCad missing, so these run only when KiCad is present)
# --------------------------------------------------------------------------- #

class TestNormalize:
    def test_basic(self):
        from aria_os.ecad.kicad_footprint_lib import _normalize_fp_name
        assert _normalize_fp_name("LQFP-64_10x10mm_P0.5mm") == "LQFP-64_10X10MM_P0.5MM".replace("_", "")
        # Underscores removed, uppercase
        assert _normalize_fp_name("R_0805_2012Metric") == "R08052012METRIC"

    def test_candidate_keys(self):
        from aria_os.ecad.kicad_footprint_lib import _candidate_keys
        keys = _candidate_keys("LQFP-64_10x10mm_P0.5mm")
        # Must contain the full normalized form and a stripped variant
        assert len(keys) >= 2
        # Most specific key is first
        assert "LQFP" in keys[0] or "LQFP" in keys[-1]

    def test_candidate_keys_short(self):
        from aria_os.ecad.kicad_footprint_lib import _candidate_keys
        keys = _candidate_keys("SOT-23")
        assert len(keys) >= 1
        assert keys[0] == "SOT-23"
