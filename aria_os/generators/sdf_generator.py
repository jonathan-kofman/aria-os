"""
aria_os/generators/sdf_generator.py — SDF/Voxel geometry backend

Implicit geometry via signed distance fields. Inspired by LEAP71/PicoGK.
Uses numpy for field evaluation, skimage marching cubes for meshing.

Capable of: lattices, gyroids, organic shapes, topology-optimized forms,
variable-density infills, conformal channels — geometry that BRep CAD cannot
represent.

Usage:
    from aria_os.generators.sdf_generator import SDFScene, write_sdf_artifacts

    scene = SDFScene(resolution=0.5)
    outer = scene.cylinder(radius=50, height=100)
    bore = scene.cylinder(radius=20, height=102)
    lattice = scene.gyroid(cell_size=10, thickness=2)
    result = scene.difference(outer, bore)
    result = scene.intersection(result, lattice)
    mesh = scene.to_mesh(result)
    scene.export_stl(mesh, "part.stl")
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from .. import event_bus


# ---------------------------------------------------------------------------
# SDF primitives — each returns a function f(x,y,z) -> signed distance
# Negative = inside, positive = outside, zero = surface
# ---------------------------------------------------------------------------

def sdf_sphere(center: tuple = (0, 0, 0), radius: float = 1.0):
    cx, cy, cz = center
    def f(x, y, z):
        return np.sqrt((x - cx)**2 + (y - cy)**2 + (z - cz)**2) - radius
    return f


def sdf_box(center: tuple = (0, 0, 0), size: tuple = (1, 1, 1)):
    cx, cy, cz = center
    sx, sy, sz = size[0] / 2, size[1] / 2, size[2] / 2
    def f(x, y, z):
        dx = np.abs(x - cx) - sx
        dy = np.abs(y - cy) - sy
        dz = np.abs(z - cz) - sz
        outside = np.sqrt(np.maximum(dx, 0)**2 + np.maximum(dy, 0)**2 + np.maximum(dz, 0)**2)
        inside = np.minimum(np.maximum(dx, np.maximum(dy, dz)), 0)
        return outside + inside
    return f


def sdf_cylinder(center: tuple = (0, 0, 0), radius: float = 1.0, height: float = 1.0,
                 axis: str = "z"):
    cx, cy, cz = center
    h2 = height / 2
    def f(x, y, z):
        if axis == "z":
            d_radial = np.sqrt((x - cx)**2 + (y - cy)**2) - radius
            d_height = np.abs(z - cz - h2) - h2
        elif axis == "y":
            d_radial = np.sqrt((x - cx)**2 + (z - cz)**2) - radius
            d_height = np.abs(y - cy - h2) - h2
        else:  # x
            d_radial = np.sqrt((y - cy)**2 + (z - cz)**2) - radius
            d_height = np.abs(x - cx - h2) - h2
        return np.maximum(d_radial, d_height)
    return f


def sdf_torus(center: tuple = (0, 0, 0), major_radius: float = 5.0, minor_radius: float = 1.0):
    cx, cy, cz = center
    def f(x, y, z):
        qx = np.sqrt((x - cx)**2 + (y - cy)**2) - major_radius
        return np.sqrt(qx**2 + (z - cz)**2) - minor_radius
    return f


def sdf_capsule(a: tuple = (0, 0, 0), b: tuple = (0, 0, 1), radius: float = 1.0):
    ax, ay, az = a
    bx, by, bz = b
    def f(x, y, z):
        pa_x, pa_y, pa_z = x - ax, y - ay, z - az
        ba_x, ba_y, ba_z = bx - ax, by - ay, bz - az
        ba_dot = ba_x**2 + ba_y**2 + ba_z**2
        t = np.clip((pa_x * ba_x + pa_y * ba_y + pa_z * ba_z) / (ba_dot + 1e-12), 0, 1)
        dx = pa_x - t * ba_x
        dy = pa_y - t * ba_y
        dz = pa_z - t * ba_z
        return np.sqrt(dx**2 + dy**2 + dz**2) - radius
    return f


def sdf_cone(center: tuple = (0, 0, 0), radius: float = 1.0, height: float = 2.0):
    cx, cy, cz = center
    def f(x, y, z):
        rz = z - cz
        r_at_z = radius * (1 - np.clip(rz / height, 0, 1))
        d_radial = np.sqrt((x - cx)**2 + (y - cy)**2) - r_at_z
        d_bottom = -(rz)
        d_top = rz - height
        return np.maximum(d_radial, np.maximum(d_bottom, d_top))
    return f


# ---------------------------------------------------------------------------
# Complex SDF shapes — lattices, TPMS, organic
# ---------------------------------------------------------------------------

def sdf_gyroid(cell_size: float = 10.0, thickness: float = 1.0):
    """Gyroid TPMS (triply periodic minimal surface)."""
    k = 2 * np.pi / cell_size
    t2 = thickness / 2
    def f(x, y, z):
        val = np.sin(k * x) * np.cos(k * y) + \
              np.sin(k * y) * np.cos(k * z) + \
              np.sin(k * z) * np.cos(k * x)
        return np.abs(val) - t2
    return f


def sdf_schwarz_p(cell_size: float = 10.0, thickness: float = 1.0):
    """Schwarz-P TPMS surface."""
    k = 2 * np.pi / cell_size
    t2 = thickness / 2
    def f(x, y, z):
        val = np.cos(k * x) + np.cos(k * y) + np.cos(k * z)
        return np.abs(val) - t2
    return f


def sdf_diamond(cell_size: float = 10.0, thickness: float = 1.0):
    """Schwarz-D (Diamond) TPMS surface."""
    k = 2 * np.pi / cell_size
    t2 = thickness / 2
    def f(x, y, z):
        val = (np.sin(k*x) * np.sin(k*y) * np.sin(k*z) +
               np.sin(k*x) * np.cos(k*y) * np.cos(k*z) +
               np.cos(k*x) * np.sin(k*y) * np.cos(k*z) +
               np.cos(k*x) * np.cos(k*y) * np.sin(k*z))
        return np.abs(val) - t2
    return f


def sdf_lattice_cubic(cell_size: float = 10.0, beam_radius: float = 1.0):
    """Cubic lattice — beams along all three axes."""
    def f(x, y, z):
        # Distance to nearest beam axis in each plane
        mx = np.mod(x + cell_size / 2, cell_size) - cell_size / 2
        my = np.mod(y + cell_size / 2, cell_size) - cell_size / 2
        mz = np.mod(z + cell_size / 2, cell_size) - cell_size / 2
        d_xy = np.sqrt(mx**2 + my**2) - beam_radius  # Z-axis beams
        d_xz = np.sqrt(mx**2 + mz**2) - beam_radius  # Y-axis beams
        d_yz = np.sqrt(my**2 + mz**2) - beam_radius  # X-axis beams
        return np.minimum(d_xy, np.minimum(d_xz, d_yz))
    return f


# ---------------------------------------------------------------------------
# SDF operations — composable boolean and morphological ops
# ---------------------------------------------------------------------------

def op_union(a, b):
    def f(x, y, z): return np.minimum(a(x, y, z), b(x, y, z))
    return f


def op_difference(a, b):
    def f(x, y, z): return np.maximum(a(x, y, z), -b(x, y, z))
    return f


def op_intersection(a, b):
    def f(x, y, z): return np.maximum(a(x, y, z), b(x, y, z))
    return f


def op_smooth_union(a, b, k: float = 2.0):
    """Smooth (polynomial) union — blends surfaces together."""
    def f(x, y, z):
        da, db = a(x, y, z), b(x, y, z)
        h = np.clip(0.5 + 0.5 * (db - da) / k, 0, 1)
        return db * (1 - h) + da * h - k * h * (1 - h)
    return f


def op_smooth_difference(a, b, k: float = 2.0):
    """Smooth difference — filleted subtraction."""
    def f(x, y, z):
        da, db = a(x, y, z), -b(x, y, z)
        h = np.clip(0.5 - 0.5 * (db + da) / k, 0, 1)
        return da * (1 - h) + db * h + k * h * (1 - h)
    return f


def op_offset(a, distance: float):
    """Offset (dilate/erode) a surface."""
    def f(x, y, z): return a(x, y, z) - distance
    return f


def op_shell(a, thickness: float):
    """Create a hollow shell."""
    def f(x, y, z): return np.abs(a(x, y, z)) - thickness / 2
    return f


def op_twist(a, rate: float = 0.1):
    """Twist around Z axis (radians per unit Z)."""
    def f(x, y, z):
        angle = z * rate
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        nx = x * cos_a - y * sin_a
        ny = x * sin_a + y * cos_a
        return a(nx, ny, z)
    return f


def op_bend(a, rate: float = 0.01):
    """Bend around Y axis."""
    def f(x, y, z):
        angle = x * rate
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        nx = np.where(np.abs(rate) > 1e-10,
                      (1/rate) * (cos_a - 1) + z * sin_a,
                      x)
        nz = np.where(np.abs(rate) > 1e-10,
                      -(1/rate) * sin_a + z * cos_a,
                      z)
        return a(nx, y, nz)
    return f


def op_repeat(a, period: tuple = (10, 10, 10)):
    """Infinite repetition in 3D."""
    px, py, pz = period
    def f(x, y, z):
        mx = np.mod(x + px/2, px) - px/2 if px > 0 else x
        my = np.mod(y + py/2, py) - py/2 if py > 0 else y
        mz = np.mod(z + pz/2, pz) - pz/2 if pz > 0 else z
        return a(mx, my, mz)
    return f


def op_scale(a, factor: float):
    """Uniform scale."""
    def f(x, y, z): return a(x / factor, y / factor, z / factor) * factor
    return f


def op_translate(a, offset: tuple = (0, 0, 0)):
    ox, oy, oz = offset
    def f(x, y, z): return a(x - ox, y - oy, z - oz)
    return f


def op_rotate_z(a, angle_deg: float):
    angle = np.radians(angle_deg)
    cos_a, sin_a = np.cos(angle), np.sin(angle)
    def f(x, y, z):
        return a(x * cos_a + y * sin_a, -x * sin_a + y * cos_a, z)
    return f


# ---------------------------------------------------------------------------
# SDFScene — high-level builder API
# ---------------------------------------------------------------------------

class SDFScene:
    """
    High-level SDF scene builder. Create primitives, combine with booleans,
    mesh via marching cubes, export to STL.
    """

    def __init__(self, resolution: float = 0.5, padding: float = 2.0):
        self.resolution = resolution
        self.padding = padding

    # --- Primitives ---
    def sphere(self, **kw): return sdf_sphere(**kw)
    def box(self, **kw): return sdf_box(**kw)
    def cylinder(self, **kw): return sdf_cylinder(**kw)
    def torus(self, **kw): return sdf_torus(**kw)
    def capsule(self, **kw): return sdf_capsule(**kw)
    def cone(self, **kw): return sdf_cone(**kw)

    # --- Complex shapes ---
    def gyroid(self, **kw): return sdf_gyroid(**kw)
    def schwarz_p(self, **kw): return sdf_schwarz_p(**kw)
    def diamond(self, **kw): return sdf_diamond(**kw)
    def lattice_cubic(self, **kw): return sdf_lattice_cubic(**kw)

    # --- Booleans ---
    def union(self, a, b): return op_union(a, b)
    def difference(self, a, b): return op_difference(a, b)
    def intersection(self, a, b): return op_intersection(a, b)
    def smooth_union(self, a, b, k=2.0): return op_smooth_union(a, b, k)
    def smooth_difference(self, a, b, k=2.0): return op_smooth_difference(a, b, k)

    # --- Transformations ---
    def offset(self, a, d): return op_offset(a, d)
    def shell(self, a, t): return op_shell(a, t)
    def twist(self, a, rate): return op_twist(a, rate)
    def bend(self, a, rate): return op_bend(a, rate)
    def repeat(self, a, period): return op_repeat(a, period)
    def scale(self, a, factor): return op_scale(a, factor)
    def translate(self, a, offset): return op_translate(a, offset)
    def rotate_z(self, a, angle_deg): return op_rotate_z(a, angle_deg)

    # --- Evaluation and meshing ---
    def evaluate(self, sdf_func, bounds: tuple) -> np.ndarray:
        """Evaluate SDF on a 3D grid. bounds = ((xmin,ymin,zmin),(xmax,ymax,zmax))"""
        (x0, y0, z0), (x1, y1, z1) = bounds
        pad = self.padding
        res = self.resolution
        x = np.arange(x0 - pad, x1 + pad + res, res)
        y = np.arange(y0 - pad, y1 + pad + res, res)
        z = np.arange(z0 - pad, z1 + pad + res, res)
        X, Y, Z = np.meshgrid(x, y, z, indexing='ij')
        return sdf_func(X, Y, Z), (x, y, z)

    def to_mesh(self, sdf_func, bounds: tuple):
        """Convert SDF to triangle mesh via marching cubes."""
        from skimage.measure import marching_cubes

        field, (x, y, z) = self.evaluate(sdf_func, bounds)
        verts, faces, normals, _ = marching_cubes(field, level=0.0, spacing=(
            self.resolution, self.resolution, self.resolution
        ))
        # Offset vertices to world coordinates
        verts[:, 0] += x[0]
        verts[:, 1] += y[0]
        verts[:, 2] += z[0]
        return verts, faces, normals

    def export_stl(self, mesh_data: tuple, path: str | Path) -> str:
        """Write mesh to binary STL."""
        verts, faces, normals = mesh_data
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)

        with open(p, 'wb') as f:
            f.write(b'\0' * 80)
            f.write(struct.pack('<I', len(faces)))
            for face in faces:
                v0, v1, v2 = verts[face[0]], verts[face[1]], verts[face[2]]
                # Compute face normal
                e1 = v1 - v0
                e2 = v2 - v0
                n = np.cross(e1, e2)
                norm = np.linalg.norm(n)
                if norm > 0:
                    n = n / norm
                f.write(struct.pack('<fff', *n))
                f.write(struct.pack('<fff', *v0))
                f.write(struct.pack('<fff', *v1))
                f.write(struct.pack('<fff', *v2))
                f.write(struct.pack('<H', 0))
        return str(p)

    def mesh_stats(self, mesh_data: tuple) -> dict:
        """Get mesh statistics."""
        verts, faces, _ = mesh_data
        bbox_min = verts.min(axis=0)
        bbox_max = verts.max(axis=0)
        dims = bbox_max - bbox_min
        return {
            "vertices": len(verts),
            "faces": len(faces),
            "bbox_min": bbox_min.tolist(),
            "bbox_max": bbox_max.tolist(),
            "dimensions_mm": dims.tolist(),
        }


# ---------------------------------------------------------------------------
# LLM-based SDF generation
# ---------------------------------------------------------------------------

_SDF_SYSTEM_PROMPT = """\
You are an SDF (Signed Distance Field) geometry expert. Generate Python code
using the aria_os SDF API to create complex implicit geometry.

Available primitives (all return SDF functions):
  sdf_sphere(center=(x,y,z), radius=r)
  sdf_box(center=(x,y,z), size=(sx,sy,sz))
  sdf_cylinder(center=(x,y,z), radius=r, height=h, axis="z"|"y"|"x")
  sdf_torus(center=(x,y,z), major_radius=R, minor_radius=r)
  sdf_capsule(a=(x,y,z), b=(x,y,z), radius=r)
  sdf_cone(center=(x,y,z), radius=r, height=h)

TPMS lattices:
  sdf_gyroid(cell_size=10, thickness=1)
  sdf_schwarz_p(cell_size=10, thickness=1)
  sdf_diamond(cell_size=10, thickness=1)
  sdf_lattice_cubic(cell_size=10, beam_radius=1)

Boolean operations:
  op_union(a, b)           — merge two shapes
  op_difference(a, b)      — subtract b from a
  op_intersection(a, b)    — keep only overlap
  op_smooth_union(a, b, k) — blended merge (k=blend radius)
  op_smooth_difference(a, b, k) — filleted subtraction

Transformations:
  op_offset(a, distance)   — dilate (+) or erode (-) surface
  op_shell(a, thickness)   — hollow shell
  op_twist(a, rate)        — twist around Z (radians/mm)
  op_bend(a, rate)         — bend around Y
  op_repeat(a, (px,py,pz)) — infinite 3D repetition
  op_translate(a, (dx,dy,dz))
  op_rotate_z(a, angle_deg)
  op_scale(a, factor)

Rules:
- All dimensions in mm
- Assign final SDF function to variable 'result'
- Assign bounding box to variable 'bounds' as ((xmin,ymin,zmin),(xmax,ymax,zmax))
- Use TPMS lattices for internal structure, lightweight infill, heat exchangers
- Use smooth_union for organic blends between shapes
- Use shell() to hollow solids before adding lattice infill
- Complex shapes: combine primitives with booleans, then intersect with lattice

Output ONLY Python code (no markdown fences).
"""


def _generate_sdf_via_llm(goal: str, plan: dict, repo_root: Path) -> tuple:
    """Use LLM to generate SDF code, execute it, return (sdf_func, bounds)."""
    from ..llm_client import call_llm

    params = plan.get("params", {})
    param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "none"

    user_prompt = (
        f"Create SDF geometry for: {goal}\n"
        f"Parameters: {param_str}\n"
        f"Part ID: {plan.get('part_id', 'unknown')}\n\n"
        "Generate Python code using the SDF API."
    )

    response = call_llm(user_prompt, system=_SDF_SYSTEM_PROMPT, repo_root=repo_root)
    if not response:
        raise RuntimeError("LLM returned empty response for SDF generation")

    code = response.strip()
    if code.startswith("```"):
        lines = code.split("\n")
        code = "\n".join(lines[1:-1])

    # Execute in sandbox with SDF functions available
    ns = {
        "np": np,
        "sdf_sphere": sdf_sphere, "sdf_box": sdf_box, "sdf_cylinder": sdf_cylinder,
        "sdf_torus": sdf_torus, "sdf_capsule": sdf_capsule, "sdf_cone": sdf_cone,
        "sdf_gyroid": sdf_gyroid, "sdf_schwarz_p": sdf_schwarz_p,
        "sdf_diamond": sdf_diamond, "sdf_lattice_cubic": sdf_lattice_cubic,
        "op_union": op_union, "op_difference": op_difference,
        "op_intersection": op_intersection,
        "op_smooth_union": op_smooth_union, "op_smooth_difference": op_smooth_difference,
        "op_offset": op_offset, "op_shell": op_shell,
        "op_twist": op_twist, "op_bend": op_bend, "op_repeat": op_repeat,
        "op_translate": op_translate, "op_rotate_z": op_rotate_z,
        "op_scale": op_scale,
    }

    exec(compile(code, "<sdf_llm>", "exec"), ns)
    result = ns.get("result")
    bounds = ns.get("bounds")

    if result is None:
        raise RuntimeError("SDF code did not produce a 'result' variable")
    if bounds is None:
        raise RuntimeError("SDF code did not produce a 'bounds' variable")

    return result, bounds, code


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

SDF_KEYWORDS = [
    "lattice", "gyroid", "schwarz", "tpms", "infill", "porous",
    "organic", "topology optim", "conformal", "variable density",
    "heat exchanger", "cellular", "foam", "sponge", "lightweight fill",
    "gradient density", "bone structure", "voronoi",
]


def write_sdf_artifacts(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    """
    Generate geometry via SDF/voxel backend.

    Returns dict with: stl_path, plan_path, code_path
    Note: SDF produces STL only (no STEP — implicit geometry has no BRep).
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    part_id = (plan.get("part_id") or "sdf_part").replace("/", "_")
    out_dir = repo_root / "outputs" / "cad" / "sdf" / part_id
    out_dir.mkdir(parents=True, exist_ok=True)

    event_bus.emit("sdf", f"Generating {part_id} via SDF/voxel", {"part_id": part_id})

    # 1) Template-first path — try the deterministic SDF template library
    # (aria_os/sdf/templates.py). Skips the LLM entirely when a template
    # matches the goal (gyroid block, octet-truss sphere, honeycomb panel,
    # FGM-graded lattices, etc.), so the common pro-grade lattice parts
    # generate without burning a Claude/Gemini token.
    template_used: str | None = None
    try:
        from aria_os.sdf.templates import build_from_template
        _tmpl = build_from_template(goal, plan.get("params") if isinstance(plan, dict) else None)
    except Exception as _texc:  # pragma: no cover
        print(f"[SDF] template lookup failed: {type(_texc).__name__}: {_texc}")
        _tmpl = None

    if _tmpl is not None:
        sdf_func, bounds, meta = _tmpl
        template_used = meta.get("template")
        event_bus.emit("sdf",
                       f"template: {template_used} (no LLM call needed)",
                       {"part_id": part_id, "meta": meta})
        code = (f"# aria-os SDF template: {template_used}\n"
                f"# params: {meta}\n"
                f"# source: aria_os/sdf/templates.py\n")
    else:
        # 2) LLM fallback — only used when no template matches the goal.
        sdf_func, bounds, code = _generate_sdf_via_llm(goal, plan, repo_root)

    # Save generated code (either template reference or LLM-emitted code)
    code_path = out_dir / f"{part_id}_sdf.py"
    code_path.write_text(code, encoding="utf-8")

    # Mesh it
    scene = SDFScene(resolution=float(plan.get("params", {}).get("resolution_mm", 0.5)))
    event_bus.emit("step", f"Meshing SDF at {scene.resolution}mm resolution")
    mesh_data = scene.to_mesh(sdf_func, bounds)
    stats = scene.mesh_stats(mesh_data)
    dims = stats["dimensions_mm"]
    event_bus.emit("sdf", f"Mesh: {dims[0]:.1f}x{dims[1]:.1f}x{dims[2]:.1f}mm, "
                   f"{stats['vertices']} verts, {stats['faces']} faces")

    # Export STL
    stl_out = scene.export_stl(mesh_data, stl_path)

    # ── Visual verification (SDF produces STL only, no STEP) ──────────────
    try:
        from ..visual_verifier import verify_visual
        _vis = verify_visual(
            "",  # no STEP for SDF backend
            stl_out,
            goal,
            plan.get("params", {}),
            repo_root=repo_root,
        )
        _vis_conf = _vis.get("confidence", 0.0)
        if _vis.get("verified") is True and _vis_conf >= 0.90:
            print(f"  [VISUAL] PASS — confidence {_vis_conf:.0%}")
        elif _vis.get("verified") is True and _vis_conf < 0.90:
            print(f"  [VISUAL] FAIL — confidence {_vis_conf:.0%} below 90% threshold")
            for _vi in _vis.get("issues", []):
                print(f"    [VISUAL] {_vi}")
        elif _vis.get("verified") is False:
            print(f"  [VISUAL] FAIL — confidence {_vis_conf:.0%}")
            for _vi in _vis.get("issues", []):
                print(f"    [VISUAL] {_vi}")
        elif _vis.get("verified") is None:
            _reason = _vis.get("reason", "unknown")
            print(f"  [VISUAL] SKIPPED — {_reason}")
    except Exception as _vis_exc:
        print(f"  [VISUAL] skipped: {_vis_exc}")

    # Save plan
    plan_data = {
        "part_id": part_id,
        "backend": "sdf",
        "resolution_mm": scene.resolution,
        "bounds": [list(bounds[0]), list(bounds[1])],
        "mesh_stats": stats,
        "template_used": template_used,  # None if LLM fallback was used
    }
    plan_path = out_dir / "sdf_plan.json"
    plan_path.write_text(json.dumps(plan_data, indent=2), encoding="utf-8")

    return {
        "stl_path": stl_out,
        "code_path": str(code_path),
        "plan_path": str(plan_path),
    }
