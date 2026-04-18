"""
tests/test_diy_fab.py - minimal end-to-end coverage for aria_os.ecad.diy_fab.

Builds a 40x30mm PCB with two components and a single net via
kicad_pcb_writer, then runs both fabrication routes through run_diy_fab and
asserts the artifacts exist, are non-empty, and pass basic format sniff
checks (G-code header, SVG XML root, STL binary/ascii prefix).

STL checks are skipped when cadquery is not importable in the test env.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria_os.ecad.kicad_pcb_writer import write_kicad_pcb
from aria_os.ecad.diy_fab import (
    _extract_traces_from_pcb,
    emit_cnc_isolation_gcode,
    emit_cnc_drill_gcode,
    emit_copper_tape_cut_svg,
    run_diy_fab,
)

try:
    import cadquery  # noqa: F401
    HAVE_CQ = True
except Exception:
    HAVE_CQ = False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tiny_pcb(tmp_path: Path) -> Path:
    """40x30mm board with U1 (MCU) + R1 (resistor) sharing a single net."""
    bom = {
        "board_name": "tinyboard",
        "board_w_mm": 40.0,
        "board_h_mm": 30.0,
        "components": [
            {"ref": "U1", "value": "ESP32", "x_mm": 4.0, "y_mm": 6.0,
             "width_mm": 12.0, "height_mm": 10.0, "rotation_deg": 0,
             "footprint": "Generic:ESP32", "nets": ["+3V3", "GND"]},
            {"ref": "R1", "value": "10k", "x_mm": 25.0, "y_mm": 8.0,
             "width_mm": 5.0, "height_mm": 2.5, "rotation_deg": 0,
             "footprint": "Generic:R_0805", "nets": ["+3V3"]},
        ],
    }
    bom_path = tmp_path / "tinyboard.bom.json"
    bom_path.write_text(json.dumps(bom), encoding="utf-8")
    pcb_path = tmp_path / "tinyboard.kicad_pcb"
    return write_kicad_pcb(bom_path, pcb_path, board_name="tinyboard")


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

def test_extract_parses_board_and_traces(tiny_pcb: Path):
    td = _extract_traces_from_pcb(tiny_pcb)
    bw, bh = td["board_size"]
    assert 39.0 <= bw <= 41.0, f"board width off: {bw}"
    assert 29.0 <= bh <= 31.0, f"board height off: {bh}"
    assert len(td["components"]) == 2
    refs = {c["ref"] for c in td["components"]}
    assert refs == {"U1", "R1"}
    # every component gets at least 2 pads from kicad_pcb_writer
    assert all(c["n_pads"] >= 2 for c in td["components"])
    # at least one F.Cu trace on the shared +3V3 net
    assert len(td["traces"]) >= 1
    assert any("3V3" in net for (net, _, _) in td["traces"])


# ---------------------------------------------------------------------------
# Route A: CNC
# ---------------------------------------------------------------------------

def test_cnc_isolation_gcode(tmp_path: Path, tiny_pcb: Path):
    td = _extract_traces_from_pcb(tiny_pcb)
    out = emit_cnc_isolation_gcode(td, tmp_path / "iso.gcode",
                                   tool_dia_mm=0.2, cut_depth_mm=0.08)
    assert out.is_file() and out.stat().st_size > 200
    text = out.read_text(encoding="utf-8")
    # header sanity
    assert text.startswith(";")
    assert "G21" in text and "G90" in text
    # generated motion for at least one trace side
    assert "G1 Z-0.080" in text
    assert "M30" in text.strip().splitlines()[-1] or "M30" in text


def test_cnc_drill_gcode(tmp_path: Path, tiny_pcb: Path):
    td = _extract_traces_from_pcb(tiny_pcb)
    out = emit_cnc_drill_gcode(td, tmp_path / "drill.gcode",
                               hole_drill_dia=0.8, board_thickness_mm=1.6)
    assert out.is_file() and out.stat().st_size > 100
    text = out.read_text(encoding="utf-8")
    assert "G21" in text
    assert "G1 Z-1.800" in text  # 1.6 + 0.2 breakthrough
    # number of plunges == number of pad holes
    plunges = text.count("G1 Z-1.800")
    assert plunges == len(td["pad_holes"])


# ---------------------------------------------------------------------------
# Route B: 3D-printed substrate / copper tape / stencil
# ---------------------------------------------------------------------------

def test_copper_tape_svg(tmp_path: Path, tiny_pcb: Path):
    td = _extract_traces_from_pcb(tiny_pcb)
    out = emit_copper_tape_cut_svg(td, tmp_path / "tape.svg",
                                   channel_width_mm=0.8)
    assert out.is_file() and out.stat().st_size > 200
    text = out.read_text(encoding="utf-8")
    assert text.startswith("<?xml")
    assert "<svg " in text and "</svg>" in text
    assert 'viewBox="0 0' in text
    # one polygon per trace segment (>= 1 trace -> >= 1 polygon)
    assert text.count("<polygon") >= 1


@pytest.mark.skipif(not HAVE_CQ, reason="cadquery not importable")
def test_printed_substrate_stl(tmp_path: Path, tiny_pcb: Path):
    from aria_os.ecad.diy_fab import emit_printed_substrate_stl
    td = _extract_traces_from_pcb(tiny_pcb)
    out = emit_printed_substrate_stl(
        td, tmp_path / "sub.stl",
        channel_width_mm=0.8, channel_depth_mm=0.5,
        substrate_thickness_mm=2.0)
    assert out.is_file() and out.stat().st_size > 200
    raw = out.read_bytes()
    head = raw[:80]
    # STL may be ascii "solid ..." or binary (80-byte header + uint32 tri count)
    if head[:5].lower() == b"solid":
        assert b"facet" in raw[:2000]
    else:
        tri_count = struct.unpack("<I", raw[80:84])[0]
        assert tri_count > 0


@pytest.mark.skipif(not HAVE_CQ, reason="cadquery not importable")
def test_solder_paste_stencil_stl(tmp_path: Path, tiny_pcb: Path):
    from aria_os.ecad.diy_fab import emit_solder_paste_stencil_stl
    td = _extract_traces_from_pcb(tiny_pcb)
    out = emit_solder_paste_stencil_stl(
        td, tmp_path / "stencil.stl", stencil_thickness_mm=0.15)
    assert out.is_file() and out.stat().st_size > 200


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def test_run_diy_fab_both(tmp_path: Path, tiny_pcb: Path):
    r = run_diy_fab(tiny_pcb, tmp_path, route="both")
    assert r["route"] == "both"
    assert r["n_traces"] >= 1
    assert r["n_components"] == 2
    # always present (G-code / SVG always generated)
    for key in ("isolation_gcode", "drill_gcode", "copper_tape_svg"):
        p = r["paths"][key]
        assert p and Path(p).is_file() and Path(p).stat().st_size > 0
    # STL only if cadquery is importable
    if HAVE_CQ:
        for key in ("substrate_stl", "stencil_stl"):
            p = r["paths"][key]
            assert p and Path(p).is_file()
    # manifest + extracted trace snapshot
    manifest = Path(r["out_dir"]) / "manifest.json"
    assert manifest.is_file()
    m = json.loads(manifest.read_text())
    assert m["n_traces"] == r["n_traces"]
    trace_json = Path(r["out_dir"]) / "trace_data.json"
    assert trace_json.is_file()
    tj = json.loads(trace_json.read_text())
    assert len(tj["traces"]) >= 1


def test_run_diy_fab_printed_only(tmp_path: Path, tiny_pcb: Path):
    r = run_diy_fab(tiny_pcb, tmp_path, route="printed")
    assert "isolation_gcode" not in r["paths"]
    assert "drill_gcode" not in r["paths"]
    svg = r["paths"].get("copper_tape_svg")
    assert svg and Path(svg).is_file() and Path(svg).stat().st_size > 0
    if HAVE_CQ:
        for key in ("substrate_stl", "stencil_stl"):
            p = r["paths"][key]
            assert p and Path(p).is_file()
    else:
        assert r["paths"].get("substrate_stl") is None
        assert r["paths"].get("stencil_stl") is None
        assert "cadquery_warning" in r["recommendations"]


def test_run_diy_fab_cnc_only(tmp_path: Path, tiny_pcb: Path):
    r = run_diy_fab(tiny_pcb, tmp_path, route="cnc")
    assert "isolation_gcode" in r["paths"]
    assert "copper_tape_svg" not in r["paths"]
