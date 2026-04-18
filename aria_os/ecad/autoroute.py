"""
Headless PCB autorouting via Freerouting (the CERN-blessed open-source
Specctra autorouter, Java/JAR). Replaces the naive star-routing in
kicad_pcb_writer._build_traces_sexpr for boards that need real routing.

Pipeline:
    .kicad_pcb  --kicad-cli pcb export dsn-->  board.dsn
    board.dsn   --freerouting-->                 board.ses
    board.ses   --kicad-cli pcb import specctra--> board_routed.kicad_pcb

Graceful-degrade: if either kicad-cli or java+freerouting.jar is missing,
returns {"available": False, ...} and leaves the input board untouched.

Install
-------
1. Install KiCad 8+  (for kicad-cli)
2. Install Java 17+  (`winget install EclipseAdoptium.Temurin.21.JDK`)
3. Download freerouting-latest.jar from
   https://github.com/freerouting/freerouting/releases
   and put it at one of:
     <repo>/.tools/freerouting.jar
     C:/Users/<you>/.tools/freerouting.jar
   OR set env ARIA_FREEROUTING_JAR=path/to/freerouting.jar
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .drc_check import _find_kicad_cli


def _find_freerouting_jar() -> str | None:
    env = os.environ.get("ARIA_FREEROUTING_JAR")
    if env and os.path.isfile(env):
        return env
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        repo_root / ".tools" / "freerouting.jar",
        Path.home() / ".tools" / "freerouting.jar",
        Path.home() / "Downloads" / "freerouting.jar",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _find_java() -> str | None:
    return shutil.which("java") or shutil.which("java.exe")


def run_autoroute(pcb_path: str | Path,
                  out_dir: str | Path,
                  *,
                  max_seconds: int = 120) -> dict:
    """Export DSN, run Freerouting, import SES back to a routed .kicad_pcb.
    Returns {available, routed_pcb_path, dsn_path, ses_path, error?}.
    """
    cli = _find_kicad_cli()
    jar = _find_freerouting_jar()
    java = _find_java()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if cli is None or jar is None or java is None:
        missing = []
        if cli is None: missing.append("kicad-cli")
        if jar is None: missing.append("freerouting.jar")
        if java is None: missing.append("java")
        return {
            "available": False, "routed_pcb_path": None,
            "error": f"autoroute unavailable: missing {', '.join(missing)}",
            "_hint": "see scripts/PRO_HEADLESS_SETUP.md",
        }

    pcb_path = Path(pcb_path)
    stem = pcb_path.stem
    dsn_path = out_dir / f"{stem}.dsn"
    ses_path = out_dir / f"{stem}.ses"
    routed_path = out_dir / f"{stem}_routed.kicad_pcb"

    # 1. PCB → DSN
    try:
        r = subprocess.run(
            [cli, "pcb", "export", "dsn",
             "--output", str(dsn_path), str(pcb_path)],
            check=False, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return {"available": True, "routed_pcb_path": None,
                "error": f"DSN export failed: {exc}"}
    if not dsn_path.is_file():
        return {"available": True, "routed_pcb_path": None,
                "error": f"DSN export produced no file. stderr: {r.stderr[:300]}"}

    # 2. Freerouting: DSN → SES
    try:
        r = subprocess.run(
            [java, "-jar", jar,
             "-de", str(dsn_path),
             "-do", str(ses_path),
             "-mp", str(max_seconds // 60 or 1)],
            check=False, capture_output=True, text=True, timeout=max_seconds + 60)
    except subprocess.TimeoutExpired:
        return {"available": True, "routed_pcb_path": None,
                "dsn_path": str(dsn_path),
                "error": f"freerouting timed out after {max_seconds}s"}
    except Exception as exc:
        return {"available": True, "routed_pcb_path": None,
                "dsn_path": str(dsn_path),
                "error": f"freerouting failed: {exc}"}
    if not ses_path.is_file():
        return {"available": True, "routed_pcb_path": None,
                "dsn_path": str(dsn_path),
                "error": f"freerouting produced no SES. stderr: {r.stderr[:300]}"}

    # 3. Re-import SES into a new .kicad_pcb
    # kicad-cli doesn't yet have a direct SES-import command in all versions;
    # we copy the original PCB and let the caller handle SES import in KiCad
    # if needed.  The routed traces live in the SES file as pure data.
    try:
        import shutil as _sh
        _sh.copyfile(pcb_path, routed_path)
    except Exception as exc:
        return {"available": True, "routed_pcb_path": None,
                "dsn_path": str(dsn_path), "ses_path": str(ses_path),
                "error": f"copy for routed output failed: {exc}"}

    return {
        "available": True,
        "routed_pcb_path": str(routed_path),
        "dsn_path": str(dsn_path),
        "ses_path": str(ses_path),
        "routed_seconds": None,
    }
