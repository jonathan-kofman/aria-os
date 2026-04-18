"""
Headless G-code simulation via CAMotics CLI.

What CAMotics does:
    Reads G-code + stock STL + tool table, simulates the toolpath, detects:
      - rapid moves into stock (would crash)
      - tools cutting past their max depth
      - Z-height errors (tool rising while cutting)
      - collisions between tool body and fixtures

Why this matters for pro quality:
    CAM scripts produce G-code but nothing currently verifies the toolpath
    actually runs without crashing the machine. CAMotics is the open
    equivalent of Vericut — it finds the class of errors that destroys
    machines.

Graceful-degrade: skips cleanly if `camotics` isn't on PATH.

Install
-------
    winget install --id CAMotics.CAMotics
    (or download https://github.com/CauldronDevelopmentLLC/CAMotics/releases)

Usage
-----
    from aria_os.cam.nc_sim import simulate_gcode
    r = simulate_gcode("motor_mount.nc", stock_stl="stock.stl",
                       out_dir="outputs/cam/motor_mount/sim")
    # r = {"available": bool, "passed": bool, "collisions": int,
    #      "report_path": str, "video_path": str | None}
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def _find_camotics() -> str | None:
    found = shutil.which("camotics") or shutil.which("camotics.exe")
    if found:
        return found
    # Windows fallback — CAMotics installs to Program Files
    for base in (
        r"C:\Program Files\CAMotics",
        r"C:\Program Files (x86)\CAMotics",
    ):
        if os.path.isdir(base):
            for ver in sorted(os.listdir(base), reverse=True):
                candidate = os.path.join(base, ver, "bin", "camotics.exe")
                if os.path.isfile(candidate):
                    return candidate
            candidate = os.path.join(base, "bin", "camotics.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


def simulate_gcode(gcode_path: str | Path,
                   *,
                   stock_stl: str | Path | None = None,
                   tool_table: str | Path | None = None,
                   out_dir: str | Path,
                   timeout_s: int = 120) -> dict:
    """Run CAMotics on a G-code file, return collision/error report.

    Parameters
    ----------
    gcode_path : path to .nc / .ngc / .gcode file
    stock_stl : optional STL file defining starting stock
    tool_table : optional .tbl file; CAMotics uses LinuxCNC table format
    out_dir : where to write camotics_report.json + optional preview
    timeout_s : abort if sim runs longer
    """
    exe = _find_camotics()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "camotics_report.json"

    if exe is None:
        return {
            "available": False, "passed": None,
            "report_path": None, "collisions": 0,
            "error": "camotics not found; install CAMotics",
            "_hint": "see scripts/PRO_HEADLESS_SETUP.md",
        }

    gcode_path = Path(gcode_path)
    if not gcode_path.is_file():
        return {"available": True, "passed": False,
                "error": f"gcode not found: {gcode_path}"}

    # Build CAMotics project file (XML) referencing the gcode + optional stock
    proj_path = out_dir / "sim.camotics"
    _write_project_file(proj_path, gcode_path, stock_stl, tool_table)

    # Run sim with --simulate flag (produces JSON status via --verbose)
    cmd = [exe, "--simulate", "--verbose", str(proj_path)]
    try:
        r = subprocess.run(cmd, check=False, capture_output=True,
                           text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"available": True, "passed": False, "collisions": 0,
                "error": f"camotics timed out after {timeout_s}s",
                "report_path": None}
    except Exception as exc:
        return {"available": True, "passed": False, "collisions": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "report_path": None}

    stdout = r.stdout or ""
    stderr = r.stderr or ""

    # Parse collision/error count from output — CAMotics logs lines like:
    #   "Collision at (x,y,z): tool-with-stock during G0"
    #   "WARNING: rapid move through stock"
    #   "ERROR: axis limit exceeded"
    collisions = sum(1 for line in (stdout + "\n" + stderr).splitlines()
                     if any(kw in line.lower()
                            for kw in ("collision", "rapid through",
                                       "axis limit", "tool crash")))

    passed = r.returncode == 0 and collisions == 0

    report = {
        "available": True,
        "passed": passed,
        "collisions": collisions,
        "returncode": r.returncode,
        "stdout_tail": stdout[-1500:],
        "stderr_tail": stderr[-1500:],
        "gcode_path": str(gcode_path),
        "stock_stl": str(stock_stl) if stock_stl else None,
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def _write_project_file(path: Path,
                        gcode_path: Path,
                        stock_stl: Path | None,
                        tool_table: Path | None) -> None:
    """Write a minimal CAMotics project XML referencing the G-code."""
    abs_gcode = str(gcode_path.resolve()).replace("\\", "/")
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<camotics version="1.2.1">',
        '  <nc-files>',
        f'    <file>{abs_gcode}</file>',
        '  </nc-files>',
    ]
    if stock_stl and Path(stock_stl).is_file():
        abs_stl = str(Path(stock_stl).resolve()).replace("\\", "/")
        parts += [
            '  <workpiece>',
            f'    <stl>{abs_stl}</stl>',
            '  </workpiece>',
        ]
    if tool_table and Path(tool_table).is_file():
        abs_tbl = str(Path(tool_table).resolve()).replace("\\", "/")
        parts += [
            '  <tool-table>',
            f'    <file>{abs_tbl}</file>',
            '  </tool-table>',
        ]
    parts.append('</camotics>')
    path.write_text("\n".join(parts), encoding="utf-8")
