"""
tests/test_topo_opt.py — smoke tests for aria_os/topo_opt.

Scope:
  * Module imports cleanly without gmsh/ccx installed.
  * run_topo_opt short-circuits to {"available": False, ...} when either
    tool is missing, and still returns a well-formed iter dict.
  * _build_graded_lattice + density-field path compose without errors
    against a uniform-density closure (iter 0 behavior).
  * stress_field_from_ccx_frd returns a zero callable gracefully on a
    bogus frd path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.sdf.primitives import sdf_rounded_box
from aria_os.topo_opt import run_topo_opt, stress_field_from_ccx_frd
from aria_os.topo_opt.opt_loop import _build_graded_lattice, _envelope_bounds


ITER_KEYS_REQUIRED = {"iter"}
# Keys we want present when the iter ran (or at least partially);
# when FEA isn't available, these may be None but the keys exist.
ITER_KEYS_SOFT = {
    "max_stress_mpa", "safety_factor", "mass_g",
    "mesh_path", "step_path", "frd_path", "error",
}


@pytest.fixture
def envelope():
    # 40x40x40 mm rounded box envelope
    return sdf_rounded_box(center=(0, 0, 0),
                           size=(40, 40, 40), radius=2.0)


class TestGracefulDegrade:
    def test_runs_without_exception_when_tools_missing(self, envelope, tmp_path):
        """Contract: must never raise, even without gmsh/ccx."""
        r = run_topo_opt(
            envelope_sdf=envelope,
            load_case={"load_n": 500.0, "fixed_z_below_mm": 2.0},
            material="aluminum_6061",
            out_dir=tmp_path,
            max_iters=1,
            bounds=((-20, -20, -20), (20, 20, 20)),
        )
        assert isinstance(r, dict)
        assert "available" in r
        assert "iters" in r
        assert "converged" in r
        assert "final_geometry_path" in r
        assert "final_density_field" in r

    def test_iter_dict_shape(self, envelope, tmp_path):
        r = run_topo_opt(
            envelope_sdf=envelope,
            load_case={"load_n": 500.0, "fixed_z_below_mm": 2.0},
            material="aluminum_6061",
            out_dir=tmp_path,
            max_iters=1,
            bounds=((-20, -20, -20), (20, 20, 20)),
        )
        assert len(r["iters"]) >= 1
        it = r["iters"][0]
        for k in ITER_KEYS_REQUIRED:
            assert k in it, f"missing required iter key: {k}"
        # At least one of the soft keys exists (all of them when running,
        # or just `error` when tools are missing).
        assert any(k in it for k in ITER_KEYS_SOFT)

    def test_unknown_material_short_circuits(self, envelope, tmp_path):
        r = run_topo_opt(
            envelope_sdf=envelope,
            load_case={"load_n": 100.0},
            material="unobtanium",
            out_dir=tmp_path,
            max_iters=1,
            bounds=((-20, -20, -20), (20, 20, 20)),
        )
        assert r["available"] is False
        assert "unknown material" in (r.get("error") or "")

    def test_unsupported_lattice_type_short_circuits(self, envelope, tmp_path):
        r = run_topo_opt(
            envelope_sdf=envelope,
            load_case={"load_n": 100.0},
            material="aluminum_6061",
            out_dir=tmp_path,
            max_iters=1,
            lattice_type="kagome",
            bounds=((-20, -20, -20), (20, 20, 20)),
        )
        assert r["available"] is False
        assert "kagome" in (r.get("error") or "")


class TestBuildGradedLattice:
    def test_uniform_density_octet(self, envelope):
        """Iter 0 path: density_field=None -> uniform t_max."""
        sdf = _build_graded_lattice(
            "octet", cell_size_mm=8.0, density_field=None,
            t_min=0.3, t_max=2.0, envelope_sdf=envelope)
        # Must be callable and return finite values on a small grid
        xs = np.linspace(-10, 10, 5)
        X, Y, Z = np.meshgrid(xs, xs, xs, indexing="ij")
        val = sdf(X, Y, Z)
        assert val.shape == X.shape
        assert np.isfinite(val).all()

    def test_uniform_density_gyroid(self, envelope):
        sdf = _build_graded_lattice(
            "gyroid", cell_size_mm=8.0, density_field=None,
            t_min=0.3, t_max=2.0, envelope_sdf=envelope)
        xs = np.linspace(-10, 10, 5)
        X, Y, Z = np.meshgrid(xs, xs, xs, indexing="ij")
        val = sdf(X, Y, Z)
        assert val.shape == X.shape
        assert np.isfinite(val).all()

    def test_density_field_closure(self, envelope):
        """Pass a non-trivial density closure; lattice must still build."""
        def rho(x, y, z):
            return np.full_like(np.asarray(x, dtype=float), 1.0)
        sdf = _build_graded_lattice(
            "octet", cell_size_mm=8.0, density_field=rho,
            t_min=0.3, t_max=2.0, envelope_sdf=envelope)
        val = sdf(np.array([0.0]), np.array([0.0]), np.array([0.0]))
        assert np.isfinite(val).all()


class TestStressFieldFromFrd:
    def test_missing_frd_returns_zero_field(self, tmp_path):
        """Bogus path must not raise — returns a zero-valued callable."""
        fn = stress_field_from_ccx_frd(
            tmp_path / "nope.frd",
            bounds=((-10, -10, -10), (10, 10, 10)),
            resolution=2.0)
        v = fn(np.array([0.0, 1.0]), np.array([0.0, 1.0]),
               np.array([0.0, 1.0]))
        assert np.allclose(v, 0.0)


class TestEnvelopeBounds:
    def test_probe_returns_sensible_bounds(self, envelope):
        (lo, hi) = _envelope_bounds(envelope)
        # A 40mm rounded box centered at origin should probe to roughly
        # +-20mm in each axis (give or take a pad cell)
        for axis in range(3):
            assert lo[axis] < 0.0 < hi[axis]
            span = hi[axis] - lo[axis]
            assert 30.0 <= span <= 60.0
