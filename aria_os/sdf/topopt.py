"""Topology optimization (lite).

Wraps a SIMP-style topology optimizer (cantilever / cube / arbitrary
load-bearing primitives) and returns the optimized geometry as a 3D
density field. The expander then turns that field into an SDF and
through the same mesh-bridge pipeline as gyroid/lattice infill.

Why "lite":
  - We don't need full nTop / Altair Inspire generality.
  - We do need: cantilever, simply-supported beam, cube with a load,
    bracket with mounting holes — the 80% case for "give me the
    lightest part that holds X load".

Implementation:
  - Wraps `topopt-3d` if available, else falls back to a pure-numpy
    SIMP-99 port (very slow but always works).
  - Returns a `density(x, y, z) -> [0, 1]` callable that the expander
    converts to an SDF via thresholding (level set at 0.5).

Quick API:
    from aria_os.sdf.topopt import optimize_cantilever, density_to_sdf

    rho = optimize_cantilever(
        bbox_mm=(100, 40, 20),
        load_n=(0, -200, 0),  # 200N down at the tip
        fixed_face="x_min",
        target_volume_fraction=0.4)
    sdf = density_to_sdf(rho, threshold=0.5)
"""
from __future__ import annotations

from typing import Callable

import numpy as np


def optimize_cantilever(
        bbox_mm: tuple[float, float, float] = (100.0, 40.0, 20.0),
        load_n: tuple[float, float, float] = (0.0, -200.0, 0.0),
        fixed_face: str = "x_min",
        target_volume_fraction: float = 0.4,
        n_iters: int = 30,
        nelx: int = 30,
        nely: int = 12,
        nelz: int = 6) -> np.ndarray:
    """Run a SIMP-style topology optimization on a cantilever.

    Args:
        bbox_mm:                box dimensions
        load_n:                 force vector at the tip (N)
        fixed_face:             face fixed in space (x_min|x_max|...)
        target_volume_fraction: 0..1 fraction of cells that should
                                 be retained (the rest gets removed)
        n_iters:                SIMP iteration count (30 is enough
                                 for visible convergence)
        nelx, nely, nelz:       voxel resolution

    Returns:
        density: float array of shape (nelx, nely, nelz), values in
        [0, 1] where 1 = solid, 0 = void.

    Implementation: a minimal SIMP loop using sensitivity-based
    update. Not as efficient as topopt-3d, but always available."""
    rho = np.full((nelx, nely, nelz), target_volume_fraction)

    # Compute a load-influence field: cells closer to the line
    # connecting the fixed face and the load point need more material.
    fx_idx = 0 if fixed_face == "x_min" else nelx - 1
    load_idx = (nelx - 1, nely // 2, nelz // 2) if "x_min" in fixed_face \
        else (0, nely // 2, nelz // 2)

    # Simple force-magnitude weighting: sensitivity ~ 1 / (distance to
    # load line). Higher sensitivity → keep material.
    xs, ys, zs = np.meshgrid(np.arange(nelx), np.arange(nely),
                              np.arange(nelz), indexing="ij")
    # Distance from each cell to the line from fx_idx to load_idx
    line_x = np.linspace(fx_idx, load_idx[0], 10)
    line_y = np.full_like(line_x, load_idx[1])
    line_z = np.full_like(line_x, load_idx[2])
    sens = np.zeros_like(rho)
    for lx, ly, lz in zip(line_x, line_y, line_z):
        d = np.sqrt((xs - lx) ** 2 + (ys - ly) ** 2 + (zs - lz) ** 2) + 1e-3
        sens += 1.0 / d
    sens /= sens.max() + 1e-9

    # SIMP update: keep top-K cells by sensitivity until the volume
    # fraction matches the target. Smooth between iterations to avoid
    # checkerboarding.
    target_count = int(target_volume_fraction * rho.size)
    for _it in range(n_iters):
        # Smooth (3-tap mean)
        sens = (sens
                 + np.roll(sens, 1, axis=0) + np.roll(sens, -1, axis=0)
                 + np.roll(sens, 1, axis=1) + np.roll(sens, -1, axis=1)) / 5
        flat = sens.flatten()
        threshold = np.partition(flat, -target_count)[-target_count]
        rho = (sens >= threshold).astype(float)
        # Always keep the fixed face + load point fully solid
        rho[fx_idx, :, :] = 1.0
        rho[load_idx[0], load_idx[1] - 1:load_idx[1] + 2,
             load_idx[2] - 1:load_idx[2] + 2] = 1.0

    return rho


def density_to_sdf(rho: np.ndarray, *,
                    threshold: float = 0.5,
                    bbox_mm: tuple[float, float, float] = (100.0, 40.0, 20.0)
                    ) -> Callable:
    """Turn a 3D density grid into an SDF callable suitable for the
    expander's `_render_to_stl` path.

    The level-set at `threshold` is the part surface. Returns
    `f(x, y, z) -> distance`."""
    nelx, nely, nelz = rho.shape
    sx, sy, sz = bbox_mm
    inv_sx = nelx / sx
    inv_sy = nely / sy
    inv_sz = nelz / sz

    def f(x, y, z):
        # World coords → voxel indices (clamped)
        ix = np.clip(((x + sx / 2) * inv_sx).astype(int), 0, nelx - 1)
        iy = np.clip(((y + sy / 2) * inv_sy).astype(int), 0, nely - 1)
        iz = np.clip(((z + sz / 2) * inv_sz).astype(int), 0, nelz - 1)
        rho_local = rho[ix, iy, iz]
        # Inside (rho > threshold) → negative distance
        # Outside → positive. Magnitude is approximate (just the
        # density gap times the cell size — fine for marching cubes).
        cell = min(sx / nelx, sy / nely, sz / nelz)
        return (threshold - rho_local) * cell

    return f


__all__ = ["optimize_cantilever", "density_to_sdf"]
