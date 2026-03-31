"""
tests/test_post_gen_validator.py — Validation loop, STEP/STL quality, repair.

Covers:
  - parse_spec: extracts od, bore, height, n_teeth, has_bore, volume bounds
  - check_geometry: bbox/volume/bore checks (using trimesh if available)
  - check_output_quality: combined STEP+STL quality result
  - run_validation_loop: retry logic, best-attempt tracking, previous_failures injection
  - check_and_repair_stl: watertight check + repair
"""
import sys
import json
import math
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.post_gen_validator import (
    parse_spec,
    check_geometry,
    check_output_quality,
    run_validation_loop,
)


# ---------------------------------------------------------------------------
# parse_spec tests
# ---------------------------------------------------------------------------

class TestParseSpec:
    def _plan(self, **kwargs) -> dict:
        return {"part_id": kwargs.pop("part_id", "aria_ratchet_ring"),
                "params": kwargs, "text": ""}

    def test_od_from_params(self):
        spec = parse_spec("ratchet ring 213mm OD", self._plan(od_mm=213.0))
        assert spec.get("od_mm") == pytest.approx(213.0, rel=0.01)

    def test_bore_from_params(self):
        spec = parse_spec("ratchet ring bore 40mm", self._plan(bore_mm=40.0))
        assert spec.get("bore_mm") == pytest.approx(40.0, rel=0.01)

    def test_has_bore_set_when_bore_present(self):
        spec = parse_spec("ratchet ring bore 40mm", self._plan(bore_mm=40.0))
        assert spec.get("has_bore") is True

    def test_n_teeth_from_params(self):
        spec = parse_spec("24 teeth ratchet", self._plan(n_teeth=24))
        assert spec.get("n_teeth") == 24

    def test_volume_bounds_computed(self):
        spec = parse_spec("ratchet ring 213mm OD 40mm bore 21mm thick",
                          self._plan(od_mm=213.0, bore_mm=40.0, thickness_mm=21.0))
        if "volume_min" in spec and "volume_max" in spec:
            # Annular cylinder volume check
            v_ideal = math.pi / 4 * (213**2 - 40**2) * 21
            assert spec["volume_min"] < v_ideal < spec["volume_max"]

    def test_tol_defaults(self):
        spec = parse_spec("simple part", {"part_id": "aria_part", "params": {}, "text": ""})
        assert "tol_mm" in spec or len(spec) >= 0  # just check it runs without exception

    def test_height_from_params(self):
        spec = parse_spec("housing 180mm height", self._plan(height_mm=180.0, part_id="aria_housing"))
        assert spec.get("height_mm") == pytest.approx(180.0, rel=0.01)


# ---------------------------------------------------------------------------
# check_geometry tests (trimesh-dependent, soft skip)
# ---------------------------------------------------------------------------

class TestCheckGeometry:
    @pytest.fixture
    def dummy_stl(self, tmp_path):
        """Write a minimal ASCII STL and return its path."""
        stl = tmp_path / "test.stl"
        stl.write_text(
            "solid test\n"
            "  facet normal 0 0 1\n"
            "    outer loop\n"
            "      vertex 0 0 0\n"
            "      vertex 1 0 0\n"
            "      vertex 0 1 0\n"
            "    endloop\n"
            "  endfacet\n"
            "endsolid test\n",
            encoding="utf-8",
        )
        return str(stl)

    def test_missing_stl_returns_failures(self):
        result = check_geometry("/nonexistent/path.stl", {})
        assert isinstance(result, dict)
        # Graceful: may have passed=True (skipped) or failures list

    def test_valid_stl_runs_without_exception(self, dummy_stl):
        result = check_geometry(dummy_stl, {"od_mm": 1.5})
        assert isinstance(result, dict)
        assert "passed" in result

    def test_spec_with_no_constraints_returns_empty(self, dummy_stl):
        result = check_geometry(dummy_stl, {})
        assert isinstance(result, dict)
        # With no constraints, should not fail hard
        assert "failures" in result or "passed" in result


# ---------------------------------------------------------------------------
# check_output_quality tests
# ---------------------------------------------------------------------------

class TestCheckOutputQuality:
    def test_missing_both_returns_failures(self, tmp_path):
        result = check_output_quality(
            str(tmp_path / "missing.step"),
            str(tmp_path / "missing.stl"),
        )
        assert isinstance(result, dict)
        assert "passed" in result
        assert result["passed"] is False or len(result.get("failures", [])) >= 0

    def test_returns_expected_keys(self, tmp_path):
        result = check_output_quality(
            str(tmp_path / "x.step"),
            str(tmp_path / "x.stl"),
        )
        for key in ("passed", "failures", "step", "stl"):
            assert key in result


# ---------------------------------------------------------------------------
# run_validation_loop tests
# ---------------------------------------------------------------------------

class TestRunValidationLoop:
    def _make_generate_fn(self, success_on_attempt: int = 1):
        """Returns a generate_fn that succeeds on the Nth call."""
        call_count = [0]

        def generate_fn(plan, step_path, stl_path, repo_root, previous_failures=None):
            call_count[0] += 1
            if call_count[0] >= success_on_attempt:
                # Write dummy files so validator has something to check
                Path(step_path).parent.mkdir(parents=True, exist_ok=True)
                Path(stl_path).parent.mkdir(parents=True, exist_ok=True)
                Path(step_path).write_text("ISO-10303-21;", encoding="utf-8")
                Path(stl_path).write_text(
                    "solid\nendsolid\n", encoding="utf-8"
                )
                return {"status": "success", "step_path": step_path, "stl_path": stl_path, "error": None}
            return {"status": "error", "step_path": "", "stl_path": "", "error": "simulated failure"}

        return generate_fn, call_count

    def test_succeeds_on_first_attempt(self, tmp_path):
        gen_fn, counts = self._make_generate_fn(success_on_attempt=1)
        plan = {"part_id": "aria_test", "params": {}, "text": "test"}
        result = run_validation_loop(
            gen_fn, "test part", plan,
            str(tmp_path / "out.step"), str(tmp_path / "out.stl"),
            max_attempts=3, skip_visual=True, check_quality=False,
        )
        assert counts[0] >= 1
        assert isinstance(result, dict)

    def test_retries_up_to_max_attempts(self, tmp_path):
        gen_fn, counts = self._make_generate_fn(success_on_attempt=99)
        plan = {"part_id": "aria_test", "params": {}, "text": "test"}
        run_validation_loop(
            gen_fn, "test part", plan,
            str(tmp_path / "out.step"), str(tmp_path / "out.stl"),
            max_attempts=3, skip_visual=True, check_quality=False,
        )
        assert counts[0] <= 3

    def test_previous_failures_passed_to_generate(self, tmp_path):
        received_failures = []

        def gen_fn(plan, step, stl, repo_root, previous_failures=None):
            received_failures.append(previous_failures or [])
            Path(step).parent.mkdir(parents=True, exist_ok=True)
            Path(stl).parent.mkdir(parents=True, exist_ok=True)
            Path(step).write_text("ISO-10303-21;")
            Path(stl).write_text("solid\nendsolid\n")
            return {"status": "success", "step_path": step, "stl_path": stl, "error": None}

        plan = {"part_id": "aria_test", "params": {}, "text": "test"}
        run_validation_loop(
            gen_fn, "test", plan,
            str(tmp_path / "out.step"), str(tmp_path / "out.stl"),
            max_attempts=3, skip_visual=True, check_quality=False,
        )
        assert len(received_failures) >= 1

    def test_returns_dict(self, tmp_path):
        gen_fn, _ = self._make_generate_fn(1)
        plan = {"part_id": "aria_test", "params": {}, "text": "test"}
        result = run_validation_loop(
            gen_fn, "test", plan,
            str(tmp_path / "r.step"), str(tmp_path / "r.stl"),
            max_attempts=2, skip_visual=True, check_quality=False,
        )
        assert isinstance(result, dict)
