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

from .drc_check import _find_kicad_cli, _find_kicad_python


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
    found = shutil.which("java") or shutil.which("java.exe")
    if found:
        return found
    # Windows fallback — check Eclipse Temurin / Adoptium install dirs
    candidates = [
        r"C:\Program Files\Eclipse Adoptium",
        r"C:\Program Files\Java",
        r"C:\Program Files\Eclipse Foundation",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Eclipse Adoptium"),
    ]
    for base in candidates:
        if not os.path.isdir(base):
            continue
        for sub in sorted(os.listdir(base), reverse=True):
            cand = os.path.join(base, sub, "bin", "java.exe")
            if os.path.isfile(cand):
                return cand
    return None


def run_autoroute(pcb_path: str | Path,
                  out_dir: str | Path,
                  *,
                  max_seconds: int = 120) -> dict:
    """Export DSN, run Freerouting, import SES back to a routed .kicad_pcb.
    Returns {available, routed_pcb_path, dsn_path, ses_path, error?}.
    """
    # KiCad 10 dropped DSN/SES from kicad-cli, so we go through pcbnew
    # Python instead. We still need java + freerouting.jar for the
    # actual routing step in the middle.
    kicad_py = _find_kicad_python()
    jar = _find_freerouting_jar()
    java = _find_java()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if kicad_py is None or jar is None or java is None:
        missing = []
        if kicad_py is None: missing.append("KiCad bundled python.exe")
        if jar is None: missing.append("freerouting.jar")
        if java is None: missing.append("java")
        return {
            "available": False, "routed_pcb_path": None,
            "error": f"autoroute unavailable: missing {', '.join(missing)}",
            "_hint": ("install KiCad 8+ via `winget install KiCad.KiCad`, "
                      "Java via `winget install EclipseAdoptium.Temurin.21.JDK`, "
                      "and download freerouting.jar from "
                      "https://github.com/freerouting/freerouting/releases "
                      "to %USERPROFILE%/.tools/freerouting.jar"),
        }

    pcb_path = Path(pcb_path)
    stem = pcb_path.stem
    dsn_path = out_dir / f"{stem}.dsn"
    ses_path = out_dir / f"{stem}.ses"
    routed_path = out_dir / f"{stem}_routed.kicad_pcb"

    # 1. PCB → DSN via pcbnew.ExportSpecctraDSN
    # (kicad_py already resolved + asserted non-None above)
    dsn_export_script = (
        "import sys, pcbnew\n"
        "in_pcb, out_dsn = sys.argv[1], sys.argv[2]\n"
        "board = pcbnew.LoadBoard(in_pcb)\n"
        "ok = pcbnew.ExportSpecctraDSN(board, out_dsn)\n"
        "sys.exit(0 if ok else 4)\n"
    )
    try:
        r = subprocess.run(
            [kicad_py, "-c", dsn_export_script,
             str(pcb_path), str(dsn_path)],
            check=False, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return {"available": True, "routed_pcb_path": None,
                "error": f"DSN export failed: {exc}"}
    if r.returncode != 0 or not dsn_path.is_file():
        return {"available": True, "routed_pcb_path": None,
                "error": f"DSN export produced no file (rc={r.returncode}). "
                         f"stderr: {r.stderr[:300]}"}

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

    # 3. Re-import SES into a new .kicad_pcb via pcbnew Python -- same
    # subprocess pattern as DSN export above (kicad_py already resolved).
    import_script = (
        "import sys, pcbnew\n"
        "in_pcb, ses, out_pcb = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "board = pcbnew.LoadBoard(in_pcb)\n"
        "ok = pcbnew.ImportSpecctraSES(board, ses)\n"
        "if not ok:\n"
        "    print('IMPORT_SES_RETURNED_FALSE', file=sys.stderr)\n"
        "    sys.exit(2)\n"
        "if not pcbnew.SaveBoard(out_pcb, board):\n"
        "    print('SAVE_BOARD_RETURNED_FALSE', file=sys.stderr)\n"
        "    sys.exit(3)\n"
        "print('OK')\n"
    )
    try:
        r = subprocess.run(
            [kicad_py, "-c", import_script,
             str(pcb_path), str(ses_path), str(routed_path)],
            check=False, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"available": True, "routed_pcb_path": None,
                "dsn_path": str(dsn_path), "ses_path": str(ses_path),
                "error": "pcbnew SES import timed out after 120s"}
    except Exception as exc:
        return {"available": True, "routed_pcb_path": None,
                "dsn_path": str(dsn_path), "ses_path": str(ses_path),
                "error": f"pcbnew SES import subprocess failed: {exc}"}

    if r.returncode != 0 or not routed_path.is_file():
        return {"available": True, "routed_pcb_path": None,
                "dsn_path": str(dsn_path), "ses_path": str(ses_path),
                "error": f"pcbnew SES import failed (rc={r.returncode}). "
                         f"stdout: {r.stdout[:200]} stderr: {r.stderr[:300]}"}

    return {
        "available": True,
        "routed_pcb_path": str(routed_path),
        "dsn_path": str(dsn_path),
        "ses_path": str(ses_path),
        "routed_seconds": None,
    }
