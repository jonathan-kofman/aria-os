"""Gerber export for KiCad PCBs.

Takes a `.kicad_pcb` file, plots Gerber files for every copper + mask +
silkscreen + edge-cuts layer, generates drill files, and zips the whole
set for a fab house upload (OSHPark / JLCPCB / PCBWay).

Two backends, tried in order:
  1. KiCad's bundled Python (pcbnew.PlotController) — preferred, matches
     what the KiCad UI "Plot" action produces.
  2. `kicad-cli` shell invocation — works when pcbnew isn't importable
     but the CLI is on PATH.

Graceful degrade: if neither is available, raises RuntimeError with a
clear remediation message.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path


def export_gerbers(pcb_path: str | Path,
                    *, out_zip: str | Path | None = None,
                    repo_root: Path | None = None) -> Path:
    pcb_path = Path(pcb_path)
    if not pcb_path.is_file():
        raise FileNotFoundError(f"Not a file: {pcb_path}")

    if out_zip is None:
        out_dir = pcb_path.parent / "gerbers"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_zip = out_dir / f"{pcb_path.stem}.gerbers.zip"
    out_zip = Path(out_zip)

    # 1. Try pcbnew.PlotController
    plot_dir = Path(tempfile.mkdtemp(prefix="aria_gerber_"))
    try:
        _export_via_pcbnew(pcb_path, plot_dir)
    except Exception as pcbnew_err:
        # 2. Fall back to kicad-cli
        try:
            _export_via_cli(pcb_path, plot_dir)
        except Exception as cli_err:
            shutil.rmtree(plot_dir, ignore_errors=True)
            raise RuntimeError(
                f"Gerber export failed. pcbnew: {pcbnew_err} ; "
                f"kicad-cli: {cli_err}")

    # Zip everything in plot_dir
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(plot_dir.iterdir()):
            if f.is_file():
                zf.write(f, arcname=f.name)
    shutil.rmtree(plot_dir, ignore_errors=True)
    return out_zip


def _export_via_pcbnew(pcb_path: Path, out_dir: Path):
    import pcbnew  # type: ignore  # raises ImportError if not available
    board = pcbnew.LoadBoard(str(pcb_path))
    pc = pcbnew.PLOT_CONTROLLER(board)
    po = pc.GetPlotOptions()
    po.SetOutputDirectory(str(out_dir))
    po.SetPlotFrameRef(False)
    po.SetLineWidth(pcbnew.FromMM(0.15))
    po.SetAutoScale(False)
    po.SetScale(1)
    po.SetMirror(False)
    po.SetUseGerberAttributes(True)
    po.SetUseGerberProtelExtensions(False)
    po.SetCreateGerberJobFile(True)
    po.SetSubtractMaskFromSilk(False)

    # Layers to plot
    layers = [
        ("F.Cu",       pcbnew.F_Cu),
        ("B.Cu",       pcbnew.B_Cu),
        ("F.Paste",    pcbnew.F_Paste),
        ("B.Paste",    pcbnew.B_Paste),
        ("F.SilkS",    pcbnew.F_SilkS),
        ("B.SilkS",    pcbnew.B_SilkS),
        ("F.Mask",     pcbnew.F_Mask),
        ("B.Mask",     pcbnew.B_Mask),
        ("Edge.Cuts",  pcbnew.Edge_Cuts),
    ]
    for name, layer_id in layers:
        pc.SetLayer(layer_id)
        pc.OpenPlotfile(name, pcbnew.PLOT_FORMAT_GERBER, name)
        pc.PlotLayer()
    pc.ClosePlot()

    # Drill files (PTH + NPTH combined)
    drl = pcbnew.EXCELLON_WRITER(board)
    drl.SetMapFileFormat(pcbnew.PLOT_FORMAT_GERBER)
    drl.CreateDrillandMapFilesSet(str(out_dir), True, False)


def _export_via_cli(pcb_path: Path, out_dir: Path):
    exe = shutil.which("kicad-cli")
    if not exe:
        raise RuntimeError("kicad-cli not on PATH")
    # Layers: KiCad 7+ syntax
    layers = ("F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,"
              "F.Mask,B.Mask,Edge.Cuts")
    subprocess.run(
        [exe, "pcb", "export", "gerbers",
         "--output", str(out_dir),
         "--layers", layers,
         "--no-x2",                 # compatibility mode
         str(pcb_path)],
        check=True, capture_output=True, text=True, timeout=120)
    subprocess.run(
        [exe, "pcb", "export", "drill",
         "--output", str(out_dir),
         "--format", "excellon",
         "--drill-origin", "plot",
         str(pcb_path)],
        check=True, capture_output=True, text=True, timeout=60)
