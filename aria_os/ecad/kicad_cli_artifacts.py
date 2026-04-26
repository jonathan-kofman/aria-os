"""Post-ECAD artifact export via kicad-cli.

Run after the pipeline writes a .kicad_pcb file. Calls KiCad 8/9/10's
`kicad-cli` to produce the artifacts a user actually wants out of the
ECAD pipeline:

  - Gerbers (zipped, fab-house ready)
  - Drill files (Excellon)
  - 3D STEP (mechanical assembly handoff — feeds aria_os.assembler)
  - 3D GLB (browser preview via /viewer)
  - SVG render of the top copper + silkscreen (thumbnail for run output)
  - PNG render via SVG (board image for the dashboard run card)
  - Board stats JSON (component counts, pad counts, copper area)

Graceful degrade: if kicad-cli isn't on PATH, log and return an empty
ArtifactSet; the rest of the pipeline carries on. Each artifact is a
best-effort — one failed export doesn't block the others.

The dashboard's run output panel iterates `ArtifactSet.items` and
renders each as a download chip + (where applicable) inline preview.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _kicad_cli_path() -> Optional[str]:
    """Return the absolute path to kicad-cli or None.

    Order:
      1. $KICAD_CLI env var (escape hatch for non-standard installs)
      2. shutil.which (PATH lookup)
      3. Known Windows install location (added by `winget install KiCad`)
    """
    env = os.environ.get("KICAD_CLI")
    if env and Path(env).is_file():
        return env
    on_path = shutil.which("kicad-cli")
    if on_path:
        return on_path
    # Per-user winget install on Windows
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) /
            "Programs" / "KiCad" / "10.0" / "bin" / "kicad-cli.exe",
        Path("C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"),
        Path("C:/Program Files/KiCad/9.0/bin/kicad-cli.exe"),
        Path("C:/Program Files/KiCad/8.0/bin/kicad-cli.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


@dataclass
class ArtifactSet:
    pcb_path: Path
    out_dir: Path
    items: dict[str, Path] = field(default_factory=dict)  # name -> path
    errors: dict[str, str] = field(default_factory=dict)

    def add(self, name: str, path: Path):
        self.items[name] = path

    def fail(self, name: str, err: str):
        self.errors[name] = err

    def to_dict(self) -> dict:
        return {
            "pcb": str(self.pcb_path),
            "out_dir": str(self.out_dir),
            "items": {k: str(v) for k, v in self.items.items()},
            "errors": self.errors,
        }


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    """Run a command, return (returncode, combined_output). Captures
    stderr+stdout so callers can log diagnostic detail on failure."""
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def export_all_artifacts(pcb_path: str | Path,
                          out_dir: str | Path | None = None) -> ArtifactSet:
    """Run every kicad-cli export against a .kicad_pcb. Returns the
    ArtifactSet describing what landed (and what failed)."""
    pcb = Path(pcb_path)
    if not pcb.is_file():
        raise FileNotFoundError(pcb)

    out = Path(out_dir) if out_dir else pcb.parent / "artifacts"
    out.mkdir(parents=True, exist_ok=True)
    aset = ArtifactSet(pcb_path=pcb, out_dir=out)

    cli = _kicad_cli_path()
    if cli is None:
        aset.fail("setup",
                   "kicad-cli not found. Install KiCad 8/9/10 or set "
                   "$KICAD_CLI to the binary path.")
        return aset

    stem = pcb.stem

    # --- Gerbers (zipped) ----------------------------------------------
    gerber_dir = out / "gerbers"
    gerber_dir.mkdir(exist_ok=True)
    rc, msg = _run([
        cli, "pcb", "export", "gerbers",
        "--output", str(gerber_dir) + os.sep,
        "--no-x2",
        str(pcb),
    ])
    if rc == 0:
        # Zip the Gerber dir for fab upload convenience
        zip_path = out / f"{stem}.gerbers.zip"
        try:
            shutil.make_archive(str(zip_path).replace(".zip", ""),
                                  "zip", str(gerber_dir))
            aset.add("gerbers_zip", zip_path)
        except Exception as e:
            aset.fail("gerbers_zip", str(e))
        aset.add("gerbers_dir", gerber_dir)
    else:
        aset.fail("gerbers", msg.strip()[:500])

    # --- Drill (Excellon) ----------------------------------------------
    rc, msg = _run([
        cli, "pcb", "export", "drill",
        "--output", str(out) + os.sep,
        "--format", "excellon",
        "--drill-origin", "plot",
        str(pcb),
    ])
    if rc == 0:
        # kicad-cli writes <stem>.drl (or PTH/NPTH split). Adopt any new file.
        for f in out.glob(f"{stem}*.drl"):
            aset.add(f"drill_{f.stem}", f)
    else:
        aset.fail("drill", msg.strip()[:500])

    # --- 3D STEP (mech assembly handoff) -------------------------------
    step_path = out / f"{stem}.step"
    rc, msg = _run([
        cli, "pcb", "export", "step",
        "--force",
        "--no-dnp",                  # skip DNP parts
        "--subst-models",            # use STEP models when VRML missing
        "--output", str(step_path),
        str(pcb),
    ], timeout=180)
    if rc == 0 and step_path.is_file():
        aset.add("step", step_path)
    else:
        aset.fail("step", msg.strip()[:500])

    # --- 3D GLB (browser preview) --------------------------------------
    glb_path = out / f"{stem}.glb"
    rc, msg = _run([
        cli, "pcb", "export", "glb",
        "--force",
        "--subst-models",
        "--output", str(glb_path),
        str(pcb),
    ], timeout=180)
    if rc == 0 and glb_path.is_file():
        aset.add("glb", glb_path)
    else:
        aset.fail("glb", msg.strip()[:500])

    # --- SVG (top copper + silk for thumbnail) -------------------------
    svg_path = out / f"{stem}.svg"
    rc, msg = _run([
        cli, "pcb", "export", "svg",
        "--layers", "F.Cu,F.Silkscreen,F.Mask,Edge.Cuts",
        "--page-size-mode", "2",      # board-only, no page frame
        "--output", str(svg_path),
        str(pcb),
    ])
    if rc == 0 and svg_path.is_file():
        aset.add("svg", svg_path)
    else:
        aset.fail("svg", msg.strip()[:500])

    # --- Stats JSON (component / pad / copper area summary) ------------
    stats_path = out / f"{stem}.stats.json"
    rc, msg = _run([
        cli, "pcb", "export", "stats",
        "--output", str(stats_path),
        str(pcb),
    ])
    if rc == 0 and stats_path.is_file():
        aset.add("stats", stats_path)
    else:
        # `stats` was added in KiCad 10; tolerate older versions
        aset.fail("stats", msg.strip()[:500])

    # --- Manifest ------------------------------------------------------
    manifest_path = out / "artifacts.json"
    manifest_path.write_text(
        json.dumps(aset.to_dict(), indent=2), encoding="utf-8")
    aset.add("manifest", manifest_path)

    return aset


def summarize(aset: ArtifactSet) -> str:
    """Pretty single-line summary for log output."""
    ok = ", ".join(sorted(aset.items)) or "none"
    err = ""
    if aset.errors:
        err = " | failed: " + ", ".join(sorted(aset.errors))
    return f"[ECAD artifacts] {ok}{err}"
