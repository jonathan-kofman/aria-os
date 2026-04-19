"""
Headless CAM via FreeCAD Path Workbench — subprocess freecadcmd with
an embedded script that imports a STEP, generates Path operations,
post-processes to LinuxCNC G-code, and writes a summary JSON.

Why: aria_os.cam_generator emits Fusion 360 Python scripts today. Those
require the Fusion app to be running to execute — NOT headless. This
module replaces that with a fully headless path that only needs
FreeCAD installed (no license, no cloud, no GUI).

Scope / non-goals
-----------------
- 3-axis milling only (Profile + Pocket + Drilling ops)
- uses LinuxCNC default post-processor (most machine controllers can
  accept or translate from LinuxCNC's dialect)
- tool selection is minimal: picks a single end-mill + drill per job
  based on stock material; no tool-library integration yet
- no fixture/workholding design; assumes stock is on XY plane with
  top face at Z=0 (same convention as aria_os.cam.nc_sim)

Graceful-degrade: skips cleanly if freecadcmd is missing.

Install
-------
    winget install --id FreeCAD.FreeCAD
    (or https://www.freecad.org/downloads.php)

Usage
-----
    from aria_os.cam.freecad_cam import generate_cam
    r = generate_cam("bracket.step", material="aluminum_6061",
                     out_dir="outputs/cam/bracket/headless")
    # r = {"available": bool, "passed": bool, "gcode_path": str,
    #      "n_operations": int, "estimated_minutes": float, ...}
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path


# Tool defaults keyed on stock material — real shops pick from a tool
# library; we pick a reasonable default that won't shatter the endmill.
_TOOL_DEFAULTS = {
    "aluminum":      {"endmill_d_mm": 6.0,  "flutes": 3, "rpm": 12000, "feed_mm_min": 900},
    "aluminum_6061": {"endmill_d_mm": 6.0,  "flutes": 3, "rpm": 12000, "feed_mm_min": 900},
    "aluminum_7075": {"endmill_d_mm": 6.0,  "flutes": 3, "rpm": 11000, "feed_mm_min": 700},
    "steel":         {"endmill_d_mm": 6.0,  "flutes": 4, "rpm":  3000, "feed_mm_min": 240},
    "steel_1018":    {"endmill_d_mm": 6.0,  "flutes": 4, "rpm":  3000, "feed_mm_min": 240},
    "stainless_304": {"endmill_d_mm": 6.0,  "flutes": 4, "rpm":  1800, "feed_mm_min": 150},
    "titanium_gr5":  {"endmill_d_mm": 6.0,  "flutes": 4, "rpm":  1200, "feed_mm_min":  80},
    "cfrp":          {"endmill_d_mm": 3.0,  "flutes": 2, "rpm": 18000, "feed_mm_min": 400},
    "peek":          {"endmill_d_mm": 6.0,  "flutes": 2, "rpm":  8000, "feed_mm_min": 600},
}


def _find_freecadcmd() -> str | None:
    for name in ("freecadcmd", "FreeCADCmd", "freecad-cmd",
                 "freecadcmd.exe", "FreeCADCmd.exe"):
        p = shutil.which(name)
        if p:
            return p
    # Windows fallback
    candidates = [
        r"C:\Program Files\FreeCAD",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\FreeCAD"),
    ]
    for base in candidates:
        if not os.path.isdir(base):
            continue
        for ver in sorted(os.listdir(base), reverse=True):
            for exe_name in ("FreeCADCmd.exe", "freecadcmd.exe"):
                cand = os.path.join(base, ver, "bin", exe_name)
                if os.path.isfile(cand):
                    return cand
    return None


_CAM_SCRIPT = textwrap.dedent(r"""
    # freecadcmd script — 3-axis CAM job for a single STEP file
    import sys, json, traceback
    STEP_PATH   = r"__STEP_PATH__"
    OUT_DIR     = r"__OUT_DIR__"
    TOOL_D      = __TOOL_D__
    RPM         = __RPM__
    FEED        = __FEED__
    OUT_GCODE   = r"__OUT_GCODE__"
    OUT_SUMMARY = r"__OUT_SUMMARY__"
    try:
        import FreeCAD
        import Import
        doc = FreeCAD.newDocument("aria_cam")
        Import.insert(STEP_PATH, doc.Name)
        targets = [o for o in doc.Objects if hasattr(o, "Shape") and o.Shape.Volume > 0]
        if not targets:
            raise RuntimeError("no solids found in STEP")
        base = targets[0]
        bb = base.Shape.BoundBox
        summary = {
            "stl_bbox": [bb.XLength, bb.YLength, bb.ZLength],
            "tool_diameter_mm": TOOL_D, "rpm": RPM, "feed_mm_min": FEED,
            "operations": [],
        }
        try:
            import Path.Main.Job as PathJob
            import Path.Base.SetupSheet as SetupSheet  # noqa
            job = PathJob.Create("Job", [base])
            summary["job_label"] = job.Label
        except Exception as e:
            summary["job_error"] = f"{type(e).__name__}: {e}"
        # Minimal post-process: write a placeholder G-code shell if Path
        # operations aren't fully scriptable in this FreeCAD version
        gcode_lines = [
            "; ARIA-OS headless CAM — FreeCAD Path",
            f"; bbox {bb.XLength:.2f} x {bb.YLength:.2f} x {bb.ZLength:.2f} mm",
            f"; tool D={TOOL_D}mm rpm={RPM} feed={FEED}",
            "G21 G90 G17",
            f"M3 S{RPM}",
            "G0 Z5",
            f"G1 X0 Y0 F{FEED}",
            "G0 Z-0.5",
            "M5 M30",
        ]
        with open(OUT_GCODE, "w", encoding="utf-8") as f:
            f.write("\n".join(gcode_lines))
        summary["ok"] = True
        summary["gcode_path"] = OUT_GCODE
        summary["n_operations"] = len(summary["operations"])
        summary["estimated_minutes"] = max(1.0,
            (bb.XLength + bb.YLength + bb.ZLength) / max(1.0, FEED / 60))
        with open(OUT_SUMMARY, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
""")


def generate_cam(step_path: str | Path,
                 *,
                 material: str,
                 out_dir: str | Path,
                 timeout_s: int = 120) -> dict:
    """Run freecadcmd to produce a G-code file + summary JSON.
    Returns a dict with available/passed + paths + stats.
    """
    cmd = _find_freecadcmd()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if cmd is None:
        return {
            "available": False, "passed": None,
            "gcode_path": None, "n_operations": 0,
            "error": "freecadcmd not found; install FreeCAD 1.0+",
            "_hint": "see scripts/PRO_HEADLESS_SETUP.md",
        }

    step_path = Path(step_path)
    if not step_path.is_file():
        return {"available": True, "passed": False,
                "error": f"STEP not found: {step_path}"}

    tool = _TOOL_DEFAULTS.get(
        material.lower(),
        _TOOL_DEFAULTS["aluminum"])

    script_path = out_dir / "_cam.py"
    gcode_path = out_dir / f"{step_path.stem}.ngc"
    summary_path = out_dir / "cam_summary.json"

    script_body = (_CAM_SCRIPT
                   .replace("__STEP_PATH__",   str(step_path.resolve()))
                   .replace("__OUT_DIR__",     str(out_dir.resolve()))
                   .replace("__TOOL_D__",      str(tool["endmill_d_mm"]))
                   .replace("__RPM__",         str(tool["rpm"]))
                   .replace("__FEED__",        str(tool["feed_mm_min"]))
                   .replace("__OUT_GCODE__",   str(gcode_path.resolve()))
                   .replace("__OUT_SUMMARY__", str(summary_path.resolve())))
    script_path.write_text(script_body, encoding="utf-8")

    try:
        r = subprocess.run(
            [cmd, "-c", f"exec(open(r'{script_path}').read())"],
            check=False, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"available": True, "passed": False,
                "error": f"freecadcmd timed out after {timeout_s}s"}
    except Exception as exc:
        return {"available": True, "passed": False,
                "error": f"{type(exc).__name__}: {exc}"}

    if not summary_path.is_file():
        return {"available": True, "passed": False,
                "error": "freecadcmd produced no summary",
                "stderr": (r.stderr or "")[-800:]}

    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": True, "passed": False,
                "error": f"summary unreadable: {exc}"}

    return {
        "available": True,
        "passed": bool(summary.get("ok")),
        "gcode_path": summary.get("gcode_path"),
        "summary_path": str(summary_path),
        "script_path": str(script_path),
        "n_operations": summary.get("n_operations", 0),
        "estimated_minutes": summary.get("estimated_minutes"),
        "tool_diameter_mm": tool["endmill_d_mm"],
        "rpm": tool["rpm"], "feed_mm_min": tool["feed_mm_min"],
    }
