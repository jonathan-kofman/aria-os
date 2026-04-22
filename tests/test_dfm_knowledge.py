"""Re-export check for aria_os.teaching.dfm_knowledge.

The substantive assertions for the DFM/material/geometry knowledge base
live in `manufacturing-core/tests/test_knowledge.py`. This file USED to
duplicate all 22 of them — a legacy copy from before the shared
manufacturing-core library existed.

The duplication was flagged by `scripts/test_audit.py` on 2026-04-20
(22 clusters, same body hash). We collapsed it to a single re-export
check: verify that ariaOS's re-export module exposes the same constants
and callables as the canonical source. If the re-export breaks, THIS
test catches it; the underlying knowledge content stays tested by the
mfg-core suite.
"""
from __future__ import annotations

import pytest

# The canonical source
from manufacturing_core.knowledge import (
    DFM_TEACHINGS as MC_DFM,
    MATERIAL_TEACHINGS as MC_MAT,
    GEOMETRY_TEACHINGS as MC_GEO,
    get_dfm_teaching as mc_get_dfm,
    get_material_teaching as mc_get_mat,
    get_geometry_teaching as mc_get_geo,
    get_all_dfm_processes as mc_all_dfm,
)

# The ariaOS re-export that consumers use
try:
    from aria_os.teaching.dfm_knowledge import (
        DFM_TEACHINGS as A_DFM,
        MATERIAL_TEACHINGS as A_MAT,
        GEOMETRY_TEACHINGS as A_GEO,
        get_dfm_teaching as a_get_dfm,
        get_material_teaching as a_get_mat,
        get_geometry_teaching as a_get_geo,
        get_all_dfm_processes as a_all_dfm,
    )
    _REEXPORT_AVAILABLE = True
except ImportError:
    _REEXPORT_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _REEXPORT_AVAILABLE,
    reason="aria_os.teaching.dfm_knowledge not present in this deploy")


class TestReexportIdentity:
    """The re-export must expose the SAME object, not a copy — otherwise
    mutation (e.g. plugins extending the knowledge base) wouldn't propagate."""

    def test_dfm_teachings_is_same_object(self):
        assert A_DFM is MC_DFM, "DFM_TEACHINGS re-export must be `is`-identical"

    def test_material_teachings_is_same_object(self):
        assert A_MAT is MC_MAT

    def test_geometry_teachings_is_same_object(self):
        assert A_GEO is MC_GEO


class TestReexportCallables:
    """The re-exported getters must BE the same callable — not a shim that
    drops args or returns different shapes."""

    def test_get_dfm_teaching_is_same(self):
        assert a_get_dfm is mc_get_dfm

    def test_get_material_teaching_is_same(self):
        assert a_get_mat is mc_get_mat

    def test_get_geometry_teaching_is_same(self):
        assert a_get_geo is mc_get_geo

    def test_get_all_dfm_processes_is_same(self):
        assert a_all_dfm is mc_all_dfm


class TestReexportBehavior:
    """Light end-to-end: going through the re-export still produces a
    correct result for one canonical lookup. The mfg-core suite covers
    the full surface."""

    def test_canonical_material_lookup_via_reexport(self):
        r = a_get_mat("aluminium_6061")
        assert r is not None, "re-export returned None for known material"
        assert "6061" in r.get("name", ""), \
            f"re-exported material lookup returned {r}"

    def test_canonical_dfm_lookup_via_reexport(self):
        # get_all_dfm_processes takes an issue_type and returns the
        # processes that have a teaching for that issue.
        procs = a_all_dfm("thin_wall")
        assert procs, "re-export returned empty process list for thin_wall"
        # Must include at least CNC — canonical assertion
        assert "cnc" in procs, f"thin_wall should teach cnc, got {procs}"
        # Going from process back through the lookup must yield a teaching
        r = a_get_dfm("thin_wall", "cnc")
        assert r is not None, "re-exported DFM lookup returned None for known pair"
