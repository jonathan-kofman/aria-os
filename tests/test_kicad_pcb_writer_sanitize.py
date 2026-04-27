"""Regression: KiCad 10's S-expression parser fails with "Failed to load
board" on any stray ";;" line comment in a .kicad_pcb. The writer used
to emit such a comment for the "no traces" case; now it emits an empty
string AND has a defensive sanitizer pass that strips any ";"-prefixed
line at write time. This test locks both layers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from aria_os.ecad.kicad_pcb_writer import write_kicad_pcb


def _bom_two_components() -> dict:
    return {
        "board_name": "test_board",
        "board_w_mm": 60.0,
        "board_h_mm": 60.0,
        "components": [
            {"ref": "U1", "value": "STM32F405", "footprint": "LQFP48",
              "x": 10.0, "y": 10.0, "rotation": 0.0,
              "nets": ["GND"]},
            {"ref": "U2", "value": "MPU6000", "footprint": "QFN16",
              "x": 30.0, "y": 30.0, "rotation": 0.0,
              "nets": ["GND"]},
        ],
    }


def _write_bom(tmp_path: Path, bom: dict) -> Path:
    p = tmp_path / "bom.json"
    p.write_text(json.dumps(bom), encoding="utf-8")
    return p


def test_writer_emits_no_line_comments(tmp_path: Path) -> None:
    bom = tmp_path / "bom.json"
    bom.write_text(json.dumps({
        "board_name": "test",
        "board_w_mm": 60.0, "board_h_mm": 60.0,
        "components": [
            {"ref": "U1", "value": "STM32F405", "footprint": "LQFP48",
              "x": 10.0, "y": 10.0, "rotation": 0.0},
        ],
    }), encoding="utf-8")
    out = write_kicad_pcb(bom, n_layers=4)
    body = out.read_text(encoding="utf-8")
    bad = [ln for ln in body.splitlines() if ln.lstrip().startswith(";")]
    assert not bad, (
        "writer emitted ';'-prefixed line(s) that KiCad 10 rejects:\n"
        + "\n".join(bad[:5]))


def test_sanitizer_strips_line_comments_when_traces_emitted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARIA_EMIT_TRACES=1 takes the trace-builder path. The sanitizer
    pass at write time still has to produce a comment-free file."""
    monkeypatch.setenv("ARIA_EMIT_TRACES", "1")
    bom = _write_bom(tmp_path, _bom_two_components())
    out = write_kicad_pcb(bom, n_layers=2)
    body = out.read_text(encoding="utf-8")
    bad = [ln for ln in body.splitlines() if ln.lstrip().startswith(";")]
    assert not bad


def test_kicad_loads_writer_output(tmp_path: Path) -> None:
    """Ground truth: kicad-cli pcb upgrade rejects a malformed file with
    "Failed to load board". If it accepts, no fab-blocking format issue
    exists. Skipped if kicad-cli is missing."""
    import shutil
    import subprocess

    cli = shutil.which("kicad-cli") or (
        r"C:\Users\jonko\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe")
    if not Path(cli).is_file():
        pytest.skip("kicad-cli not installed")

    bom = _write_bom(tmp_path, _bom_two_components())
    out = write_kicad_pcb(bom, n_layers=4)

    proc = subprocess.run(
        [cli, "pcb", "upgrade", "--force", str(out)],
        capture_output=True, timeout=30)
    err = proc.stderr.decode("utf-8", "replace")
    assert "Failed to load board" not in err, (
        f"kicad-cli rejected the writer's output:\n"
        f"stdout: {proc.stdout.decode('utf-8', 'replace')}\n"
        f"stderr: {err}")
