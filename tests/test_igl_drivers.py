"""Tests for core.drivers — CadQuery driver, manager, fallback logic."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.drivers.base_driver import CADDriver, DriverResult
from core.drivers.cadquery_driver import CadQueryDriver
from core.drivers.freecad_driver import FreeCADDriver
from core.drivers.manager import DriverManager
from core.drivers.onshape_driver import OnshapeDriver
from core.drivers.rhino_driver import RhinoDriver
from core.igl_pipeline import load_example, run_igl, driver_status, should_use_igl
from core.igl_schema import parse


# ---------------------------------------------------------------------------
# All drivers are instantiable even on bare machines
# ---------------------------------------------------------------------------

def test_drivers_instantiate_without_backends():
    """Constructors must not crash if the backend is not installed."""
    cq = CadQueryDriver()
    fc = FreeCADDriver()
    on = OnshapeDriver()
    rh = RhinoDriver()
    for d in (cq, fc, on, rh):
        assert d.name
        assert d.get_description()
        assert d.get_supported_features()


def test_is_available_never_raises():
    """is_available() must return bool, not raise, for every driver."""
    for cls in (CadQueryDriver, FreeCADDriver, OnshapeDriver, RhinoDriver):
        result = cls().is_available()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# CadQuery driver: script synthesis
# ---------------------------------------------------------------------------

def test_cadquery_build_script_for_simple_block():
    data = load_example("simple_block")
    doc = parse(data)
    driver = CadQueryDriver()
    script = driver.build_script(doc)
    # Must contain a box primitive and a hole / cutBlind operation.
    assert "cq.Workplane" in script
    assert "box(" in script
    assert "cutBlind" in script
    assert "BBOX" in script


def test_cadquery_build_script_for_cylinder_flange():
    data = load_example("cylinder_flange")
    doc = parse(data)
    driver = CadQueryDriver()
    script = driver.build_script(doc)
    assert "circle" in script
    # 6 bolt holes on circular pattern
    assert script.count("pushPoints") >= 1 or script.count("circle") >= 3
    assert "BBOX" in script


def test_cadquery_build_script_covers_every_example():
    """Schema-level sanity: generator produces SOMETHING for each example."""
    for name in (
        "simple_block",
        "bracket_with_holes",
        "cylinder_flange",
        "complex_bracket",
    ):
        data = load_example(name)
        doc = parse(data)
        script = CadQueryDriver().build_script(doc)
        assert script.strip()
        assert "import cadquery" in script


# ---------------------------------------------------------------------------
# Driver manager
# ---------------------------------------------------------------------------

def test_manager_lists_all_drivers():
    mgr = DriverManager()
    status = mgr.get_driver_status()
    names = {s["name"] for s in status}
    assert {"cadquery", "freecad", "onshape", "rhino"}.issubset(names)


def test_manager_picks_cadquery_when_nothing_else_available(monkeypatch):
    """With no env vars set, an IGL doc full of CadQuery features picks cadquery."""
    monkeypatch.delenv("ARIA_PREFERRED_DRIVER", raising=False)
    monkeypatch.delenv("ARIA_DRIVER_BLACKLIST", raising=False)

    # Force every non-CadQuery driver to report unavailable.
    mgr = DriverManager()
    for name, drv in mgr.drivers.items():
        if name != "cadquery":
            drv.is_available = lambda: False  # type: ignore[method-assign]

    data = load_example("simple_block")
    chosen = mgr.get_best_driver(data)
    assert chosen.name == "cadquery"


def test_manager_honors_preferred_driver(monkeypatch):
    monkeypatch.setenv("ARIA_PREFERRED_DRIVER", "cadquery")
    mgr = DriverManager()
    data = load_example("simple_block")
    chosen = mgr.get_best_driver(data)
    assert chosen.name == "cadquery"


def test_manager_blacklist_skips_driver(monkeypatch):
    monkeypatch.setenv("ARIA_DRIVER_BLACKLIST", "freecad,onshape")
    mgr = DriverManager()
    statuses = {s["name"]: s for s in mgr.get_driver_status()}
    assert statuses["freecad"]["blacklisted"] is True
    assert statuses["onshape"]["blacklisted"] is True
    assert statuses["cadquery"]["blacklisted"] is False


def test_list_candidates_returns_full_priority_order():
    mgr = DriverManager()
    data = load_example("simple_block")
    candidates = mgr.list_candidates(data)
    names = [c[0] for c in candidates]
    # Default priority is freecad, onshape, rhino, cadquery
    assert names == ["freecad", "onshape", "rhino", "cadquery"]


# ---------------------------------------------------------------------------
# Fallback: failing driver falls back to CadQuery
# ---------------------------------------------------------------------------


class _AlwaysFailDriver(CADDriver):
    name = "alwaysfail"

    def is_available(self) -> bool:
        return True

    def get_supported_features(self) -> list[str]:
        return ["pocket", "hole", "hole_pattern", "fillet", "chamfer"]

    def _generate_impl(self, doc, output_dir):  # type: ignore[override]
        return DriverResult.failure(self.name, "deliberate test failure")


def test_fallback_engages_when_preferred_driver_fails(tmp_path, monkeypatch):
    """If the preferred driver fails, manager tries CadQuery."""
    pytest.importorskip("cadquery")
    monkeypatch.delenv("ARIA_PREFERRED_DRIVER", raising=False)
    monkeypatch.delenv("ARIA_DRIVER_BLACKLIST", raising=False)

    cq = CadQueryDriver()
    if not cq.is_available():
        pytest.skip("cadquery not installed")

    failing = _AlwaysFailDriver()
    drivers = {
        "alwaysfail": failing,
        "cadquery": cq,
    }
    mgr = DriverManager(drivers=drivers)
    # Override priority by forcing alwaysfail to win get_best_driver.
    # Easiest route: wrap get_best_driver in test.
    mgr.get_best_driver = lambda igl, preferred=None: failing  # type: ignore[method-assign]

    data = load_example("simple_block")
    out = tmp_path / "run"
    result = mgr.generate_with_fallback(data, out)
    assert result.success, f"expected fallback to succeed, got: {result.errors}"
    assert result.fallback_used
    assert result.driver == "cadquery"


# ---------------------------------------------------------------------------
# End-to-end: IGL pipeline runs CadQuery and produces STEP/STL
# ---------------------------------------------------------------------------

def test_run_igl_produces_files(tmp_path):
    pytest.importorskip("cadquery")
    data = load_example("simple_block")
    result = run_igl(data, tmp_path / "run", preferred="cadquery")
    assert result.success, f"IGL run failed: {result.errors}"
    assert Path(result.step_file).is_file() or Path(result.stl_file).is_file()


# ---------------------------------------------------------------------------
# Opt-in flag
# ---------------------------------------------------------------------------

def test_should_use_igl_defaults_to_false(monkeypatch):
    monkeypatch.delenv("ARIA_GENERATION_MODE", raising=False)
    assert should_use_igl() is False


def test_should_use_igl_reads_env(monkeypatch):
    monkeypatch.setenv("ARIA_GENERATION_MODE", "igl")
    assert should_use_igl() is True


def test_driver_status_returns_structured_list():
    status = driver_status()
    assert isinstance(status, list)
    assert all(set(s.keys()) >= {"name", "description", "available"} for s in status)
