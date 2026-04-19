"""
Functionally-Graded Material (FGM) field helpers — drive lattice
thickness / density with a continuous scalar field so the resulting
part has variable mechanical properties along a gradient.

These return a SCALAR field f(x, y, z) -> [0, 1] which should be
multiplied into the thickness/beam_radius parameter when evaluating
a lattice, producing a spatially-varying lattice density.

Typical use (pseudocode):
    density = fgm_radial_gradient(center=(0,0,0), r_min=5, r_max=30,
                                  t_min=0.3, t_max=1.0)
    def graded(x, y, z):
        local_t = density(x, y, z)
        return sdf_gyroid(cell_size=10, thickness=local_t)(x, y, z)

The pattern above is an approximation — proper FGM evaluation composes
primitives field-wise; for a pragmatic API this module ships the most
common gradient types as first-class primitives.
"""
from __future__ import annotations

import numpy as np


def fgm_radial_gradient(center: tuple = (0, 0, 0),
                        r_min: float = 0.0, r_max: float = 10.0,
                        t_min: float = 0.3, t_max: float = 1.0):
    """Thickness varies radially from `center`. Denser at center
    (t_max) transitioning to sparse at r_max (t_min).
    Commonly used for impact-absorption structures (crushable core
    surrounded by stiff skin)."""
    cx, cy, cz = center
    rm = max(r_max - r_min, 1e-9)
    def f(x, y, z):
        r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2 + (z - cz) ** 2)
        t = np.clip((r - r_min) / rm, 0.0, 1.0)
        return t_max * (1 - t) + t_min * t
    return f


def fgm_linear_gradient(axis: str = "z",
                        v_min: float = 0.0, v_max: float = 10.0,
                        t_min: float = 0.3, t_max: float = 1.0):
    """Thickness varies linearly along an axis between v_min and v_max.
    Used for layered composite emulation and thermal gradient parts."""
    vm = max(v_max - v_min, 1e-9)
    axis_map = {"x": 0, "y": 1, "z": 2}
    def f(x, y, z):
        coord = (x, y, z)[axis_map.get(axis, 2)]
        t = np.clip((coord - v_min) / vm, 0.0, 1.0)
        return t_min * (1 - t) + t_max * t
    return f


def fgm_stress_driven_density(stress_field,
                              yield_mpa: float = 250.0,
                              t_min: float = 0.3, t_max: float = 1.0):
    """Scalar density field driven by a stress field (e.g. FEA von Mises).
    High stress -> denser lattice (more material); low stress -> sparser.

    stress_field: callable f(x, y, z) -> von Mises stress in MPa
    yield_mpa:    material yield stress — 100% density at this value
    """
    def f(x, y, z):
        s = stress_field(x, y, z)
        t = np.clip(s / yield_mpa, 0.0, 1.0)
        return t_min * (1 - t) + t_max * t
    return f
