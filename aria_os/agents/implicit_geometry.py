"""
aria_os/agents/implicit_geometry.py

Pure-Python SDF (signed distance function) geometry generation for TPMS lattices,
gyroid structures, Schwartz surfaces, and other implicit geometry.

Uses Michael Fogleman's `sdf` library (pip install sdf). Falls back gracefully
if not installed. Claude generates the SDF Python script; this module executes
it in a subprocess and validates the STL output with trimesh.

Typical geometry_types routed here: lattice_tpms
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── SDF generation system prompt ──────────────────────────────────────────────

_SDF_SYSTEM_PROMPT = """You are an expert at the Python `sdf` library (pip install sdf) by Michael Fogleman.
Write a complete Python script that generates the requested geometry and saves it to a file.

Rules:
- Import: from sdf import *; import numpy as np; import math; from pathlib import Path
- Use sdf primitives: sphere(r), box(size), cylinder(r), capsule(a,b,r)
- Use sdf combinators: a & b (intersection), a | b (union), a - b (difference)
- For gyroid: use the gyroid() function or define manually with numpy trig
- For Schwartz P: schwartz() function or sin(x)+sin(y)+sin(z)
- For octet truss / custom: define with @sdf3 decorator + numpy array operations
- Call f.save("output.stl", samples=2**20) to generate (use 2**18 for speed, 2**22 for quality)
- Print exactly: ARIA_STL_OUTPUT:<full_path_to_stl>
- All dimensions in mm
- NEVER use plt, tkinter, or any UI library
- The script must be self-contained and run without user interaction

Output ONLY the Python script — no markdown, no explanation."""


def generate_sdf_geometry(
    goal: str,
    spec: dict,
    build_recipe: str,
    output_dir: Path | str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Generate TPMS/implicit geometry using the sdf library.

    1. Claude writes the sdf Python script
    2. Script is run in a subprocess
    3. Output STL is validated with trimesh

    Returns {success, stl_path, bbox, manifold, script_path, error}.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stl_path = str(output_dir / "implicit_output.stl")
    script_path = output_dir / "aria_sdf_script.py"

    # ── Step 1: Generate script via Claude ───────────────────────────────────
    prompt = f"""Goal: {goal}

Spec: {json.dumps(spec, default=str)[:600]}

Build recipe:
{build_recipe[:2000]}

Output STL path: {stl_path}

Write a complete sdf library Python script that generates this geometry.
Print: ARIA_STL_OUTPUT:{stl_path}"""

    script = None
    try:
        from ..llm_client import call_llm
        script = call_llm(prompt, _SDF_SYSTEM_PROMPT, repo_root)
    except Exception as exc:
        logger.warning("SDF LLM call failed: %s", exc)

    if not script:
        # Fallback: try a hardcoded gyroid for lattice_tpms requests
        script = _gyroid_fallback(goal, stl_path, spec)

    # Strip markdown fences
    script = script.strip()
    if script.startswith("```"):
        lines = script.split("\n")
        script = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    script_path.write_text(script, encoding="utf-8")
    logger.info("SDF script written: %s", script_path)

    # ── Step 2: Run script in subprocess ─────────────────────────────────────
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(output_dir),
        )
        stdout = result.stdout + result.stderr

        # Parse output path from marker
        actual_stl = stl_path
        for line in stdout.splitlines():
            if line.startswith("ARIA_STL_OUTPUT:"):
                actual_stl = line[len("ARIA_STL_OUTPUT:"):].strip()
                break

        if result.returncode != 0 and not Path(actual_stl).exists():
            # Check if sdf library is missing
            if "No module named 'sdf'" in stdout:
                return {
                    "success": False,
                    "error": "sdf library not installed — run: pip install sdf",
                    "install_hint": "pip install sdf",
                }
            return {
                "success": False,
                "error": f"script failed (rc={result.returncode}): {stdout[-500:]}",
                "script_path": str(script_path),
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "SDF generation timed out (180s)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    # ── Step 3: Validate with trimesh ─────────────────────────────────────────
    bbox = None
    manifold = None
    stl_exists = Path(actual_stl).exists()

    if stl_exists:
        try:
            import trimesh
            mesh = trimesh.load(actual_stl)
            bbox = {
                "x": round(float(mesh.bounds[1][0] - mesh.bounds[0][0]), 2),
                "y": round(float(mesh.bounds[1][1] - mesh.bounds[0][1]), 2),
                "z": round(float(mesh.bounds[1][2] - mesh.bounds[0][2]), 2),
            }
            manifold = mesh.is_watertight
            logger.info("SDF mesh: bbox=%s watertight=%s faces=%d", bbox, manifold, len(mesh.faces))
        except Exception as exc:
            logger.warning("trimesh validation skipped: %s", exc)

    if stl_exists:
        logger.info("SDF success: %s", actual_stl)
        return {
            "success": True,
            "stl_path": actual_stl,
            "script_path": str(script_path),
            "bbox": bbox,
            "manifold": manifold,
            "error": None,
        }

    return {
        "success": False,
        "error": "STL file not produced",
        "script_path": str(script_path),
    }


def _gyroid_fallback(goal: str, stl_path: str, spec: dict) -> str:
    """Hardcoded gyroid script — used when LLM is unavailable."""
    # Extract size from spec or default to 50mm cube
    size = float(spec.get("size_mm") or spec.get("width_mm") or spec.get("length") or 50.0)
    cell = max(4.0, size / 8.0)
    wall = max(0.5, cell * 0.15)

    return textwrap.dedent(f"""\
        from sdf import *
        import numpy as np
        from pathlib import Path

        # Gyroid TPMS — {goal}
        # Cell size: {cell:.1f} mm, wall thickness: {wall:.1f} mm, bounding box: {size:.0f} mm

        def gyroid_sdf(scale={cell:.1f}):
            @sdf3
            def f(p):
                q = p * (2 * np.pi / scale)
                return (np.sin(q[:,0]) * np.cos(q[:,1])
                      + np.sin(q[:,1]) * np.cos(q[:,2])
                      + np.sin(q[:,2]) * np.cos(q[:,0]))
            return f

        body = box({size:.1f})
        gyroid = gyroid_sdf({cell:.1f})
        thick = {wall:.1f}

        # Shell: intersect bounding box with gyroid shell (|gyroid| < wall/2)
        shell = body & ~(gyroid > thick / 2) & ~(gyroid < -thick / 2)
        # Alternative solid fill — uncomment for solid gyroid infill:
        # shell = body & (gyroid > 0)

        out = "{stl_path}"
        print(f"Generating gyroid geometry to {{out}} ...")
        shell.save(out, samples=2**20)
        print(f"ARIA_STL_OUTPUT:{{out}}")
    """)
