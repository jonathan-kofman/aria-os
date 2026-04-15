"""
aria_os/agents/implicit_geometry.py

Pure-Python SDF (signed distance function) geometry generation for TPMS lattices,
gyroid structures, Schwartz surfaces, and other implicit geometry.

Approach: Claude generates a numpy SDF function; this module evaluates it on a 3D
grid, extracts the surface via scikit-image marching cubes, and exports STL via
trimesh. No third-party geometry DSL required — just numpy, scikit-image, trimesh.

Geometry types routed here: lattice_tpms
Fallback: hardcoded gyroid when LLM is unavailable
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

_SDF_SYSTEM_PROMPT = """You are an expert at numpy-based implicit geometry (signed distance functions).
Write a complete self-contained Python script that generates a 3D mesh and saves it as STL.

Required imports (ONLY use these):
  import numpy as np
  from skimage import measure
  import trimesh
  from pathlib import Path

Script structure:
  1. Define the SDF function: def sdf(x, y, z): ...
     - Takes numpy arrays x, y, z (same shape from np.mgrid)
     - Returns scalar field where surface is at f=0
     - Negative inside, positive outside
  2. Set grid parameters: BOUNDS (mm), RESOLUTION (integer, 80-150 is good)
  3. Evaluate: x,y,z = np.mgrid[...]; vol = sdf(x,y,z)
  4. Extract surface: verts, faces, _, _ = measure.marching_cubes(vol, 0.0)
  5. Scale verts from grid to mm
  6. Export: mesh = trimesh.Trimesh(vertices=verts, faces=faces); mesh.export(out_path)
  7. Print: ARIA_STL_OUTPUT:<out_path>

SDF math reference:
  Gyroid:   sin(sx)*cos(sy) + sin(sy)*cos(sz) + sin(sz)*cos(sx)  where s=2π/cell_size
  Schwartz P: cos(sx) + cos(sy) + cos(sz)
  Shell:    abs(f) - wall_thickness/2  (makes solid lattice into thin shell)
  Union:    np.minimum(a, b)
  Intersect: np.maximum(a, b)
  Box SDF:  np.maximum(abs(x)-hx, np.maximum(abs(y)-hy, abs(z)-hz))
  Sphere:   np.sqrt(x**2+y**2+z**2) - r
  Bounding box mask: apply np.where(box_sdf < 0, lattice_sdf, 1.0) to clip to shape

All dimensions in mm. Output ONLY the Python script — no markdown, no explanation."""


def generate_sdf_geometry(
    goal: str,
    spec: dict,
    build_recipe: str,
    output_dir: Path | str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Generate TPMS/implicit geometry using numpy + scikit-image marching cubes.

    1. Claude writes the numpy SDF script
    2. Script runs in subprocess
    3. Output STL validated with trimesh

    Returns {success, stl_path, bbox, manifold, script_path, error}.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stl_path = str(output_dir / "implicit_output.stl")
    script_path = output_dir / "aria_sdf_script.py"

    # ── Step 1: Generate script via Claude ────────────────────────────────────
    prompt = f"""Goal: {goal}

Spec: {json.dumps(spec, default=str)[:600]}

Build recipe:
{build_recipe[:2000]}

Output STL path (use exactly): {stl_path}

Write the numpy SDF script. Print ARIA_STL_OUTPUT:{stl_path} when done."""

    script = None
    try:
        from ..llm_client import call_llm
        script = call_llm(prompt, system=_SDF_SYSTEM_PROMPT, repo_root=repo_root)
    except Exception as exc:
        logger.warning("SDF LLM call failed: %s", exc)

    if not script:
        logger.info("LLM unavailable — using hardcoded gyroid fallback")
        script = _gyroid_fallback(goal, stl_path, spec)

    # Strip markdown fences
    script = script.strip()
    if script.startswith("```"):
        lines = script.split("\n")
        script = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    # Ensure UTF-8 encoding declaration so Windows Python doesn't choke on non-ASCII
    if not script.startswith("# -*-"):
        script = "# -*- coding: utf-8 -*-\n" + script

    script_path.write_text(script, encoding="utf-8")
    logger.info("SDF script written: %s", script_path)

    # ── Step 2: Run script in subprocess ──────────────────────────────────────
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True, text=True, timeout=300, cwd=str(output_dir),
        )
        stdout = (result.stdout + result.stderr).strip()

        actual_stl = stl_path
        for line in stdout.splitlines():
            if line.startswith("ARIA_STL_OUTPUT:"):
                actual_stl = line[len("ARIA_STL_OUTPUT:"):].strip()
                break

        if result.returncode != 0 and not Path(actual_stl).exists():
            return {
                "success": False,
                "error": f"script failed (rc={result.returncode}): {stdout[-600:]}",
                "script_path": str(script_path),
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "SDF generation timed out (300s)"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    # ── Step 3: Validate output ────────────────────────────────────────────────
    stl_file = Path(actual_stl)
    if not stl_file.exists():
        # Try the default location
        stl_file = Path(stl_path)

    if not stl_file.exists():
        return {"success": False, "error": "STL file not produced", "script_path": str(script_path)}

    bbox = None
    manifold = None
    try:
        import trimesh
        mesh = trimesh.load(str(stl_file))
        bbox = {
            "x": round(float(mesh.bounds[1][0] - mesh.bounds[0][0]), 2),
            "y": round(float(mesh.bounds[1][1] - mesh.bounds[0][1]), 2),
            "z": round(float(mesh.bounds[1][2] - mesh.bounds[0][2]), 2),
        }
        manifold = bool(mesh.is_watertight)
        logger.info("SDF mesh validated: bbox=%s watertight=%s faces=%d", bbox, manifold, len(mesh.faces))
    except Exception as exc:
        logger.warning("trimesh validation skipped: %s", exc)

    logger.info("SDF success: %s", stl_file)
    return {
        "success": True,
        "stl_path": str(stl_file),
        "script_path": str(script_path),
        "bbox": bbox,
        "manifold": manifold,
        "error": None,
    }


def _gyroid_fallback(goal: str, stl_path: str, spec: dict) -> str:
    """Hardcoded gyroid — used when LLM is unavailable."""
    size = float(spec.get("size_mm") or spec.get("width_mm") or spec.get("length") or 50.0)
    cell = max(4.0, size / 8.0)
    wall = max(0.4, cell * 0.15)
    res = 100

    return textwrap.dedent(f"""\
        # -*- coding: utf-8 -*-
        import numpy as np
        from skimage import measure
        import trimesh
        from pathlib import Path

        # Gyroid TPMS -- {goal.replace(chr(8212), '--').encode('ascii', 'replace').decode()}
        # Bounding box: {size:.0f} mm cube, cell size: {cell:.1f} mm, wall: {wall:.1f} mm

        SIZE = {size:.1f}
        CELL = {cell:.1f}
        WALL = {wall:.1f}
        RES  = {res}

        coords = np.linspace(-SIZE / 2, SIZE / 2, RES)
        x, y, z = np.meshgrid(coords, coords, coords, indexing='ij')

        s = 2 * np.pi / CELL
        gyroid = np.sin(s*x)*np.cos(s*y) + np.sin(s*y)*np.cos(s*z) + np.sin(s*z)*np.cos(s*x)

        # Thin shell around the gyroid surface
        shell = np.abs(gyroid) - WALL / 2

        # Clip to bounding box
        box = np.maximum(np.abs(x) - SIZE/2 + 1, np.maximum(np.abs(y) - SIZE/2 + 1, np.abs(z) - SIZE/2 + 1))
        vol = np.maximum(shell, box)

        verts, faces, _, _ = measure.marching_cubes(vol, 0.0)

        # Scale from grid indices to mm
        scale = SIZE / (RES - 1)
        verts = verts * scale - SIZE / 2

        mesh = trimesh.Trimesh(vertices=verts, faces=faces)
        out = r"{stl_path}"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        mesh.export(out)
        print(f"ARIA_STL_OUTPUT:{{out}}")
        print(f"Gyroid complete: {{len(mesh.faces)}} faces, bbox ~{{SIZE:.0f}}mm cube")
    """)
