"""Regression tests for `gdt_overlay.overlay_gdt`.

Ensures the DXF written by the GD&T overlay never contains entities
that FreeCAD's importer rejects (SOLID, 3DFACE), and that the datum
triangles are still drawn — as closed LWPOLYLINEs with optional HATCH
fill.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ezdxf = pytest.importorskip("ezdxf")

from aria_os.drawings.gdt_overlay import overlay_gdt


def _make_seed_dxf(path: Path) -> None:
    """Write a tiny seed DXF with a 60x60 outline + 4 mounting holes,
    mimicking a `kicad-cli pcb export dxf` Edge_Cuts file."""
    doc = ezdxf.new(dxfversion="R2018")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (60, 0), (60, 60), (0, 60), (0, 0)])
    for cx, cy in [(5, 5), (55, 5), (55, 55), (5, 55)]:
        msp.add_circle((cx, cy), 1.6)
    doc.saveas(path)


def test_overlay_emits_no_unsupported_entities(tmp_path: Path) -> None:
    src = tmp_path / "edge_cuts.dxf"
    out = tmp_path / "edge_cuts_gdt.dxf"
    _make_seed_dxf(src)

    r = overlay_gdt(str(src), out_path=str(out),
                      title="t", part_no="p",
                      material="FR4", revision="A",
                      hole_dia_mm=3.2)
    assert r["ok"], r

    doc = ezdxf.readfile(out)
    msp = doc.modelspace()
    types = {e.dxftype() for e in msp}

    # FreeCAD's Draft importer warns on these; bundle preview goes blank.
    assert "SOLID" not in types, (
        "SOLID entity detected — FreeCAD will reject. "
        "_draw_datum or another path emitted add_solid().")
    assert "3DFACE" not in types

    # The datum triangles must still be visible — closed LWPOLYLINEs.
    assert "LWPOLYLINE" in types
    assert r["n_datums"] == 3
    # Sanitizer reported zero SOLID rewrites (because we never wrote
    # one in the first place after the fix).
    assert r["sanitized"]["solid_rewrites"] == 0


def test_sanitizer_rewrites_legacy_solid(tmp_path: Path) -> None:
    """If some upstream geometry sneaks a SOLID into the DXF, the
    overlay's sanitizer pass must rewrite it as LWPOLYLINE before save."""
    src = tmp_path / "legacy.dxf"
    out = tmp_path / "legacy_gdt.dxf"
    doc = ezdxf.new(dxfversion="R2018")
    msp = doc.modelspace()
    msp.add_lwpolyline(
        [(0, 0), (60, 0), (60, 60), (0, 60), (0, 0)])
    msp.add_circle((30, 30), 5)
    # Inject an offending SOLID — represents a TechDraw or KiCad export
    # that decided to fill an area.
    msp.add_solid([(10, 10), (20, 10), (20, 20), (20, 20)])
    doc.saveas(src)

    r = overlay_gdt(str(src), out_path=str(out),
                      title="t", part_no="p", hole_dia_mm=3.2)
    assert r["ok"], r
    assert r["sanitized"]["solid_rewrites"] >= 1, (
        "Sanitizer did not rewrite the injected SOLID")

    doc2 = ezdxf.readfile(out)
    types = {e.dxftype() for e in doc2.modelspace()}
    assert "SOLID" not in types
