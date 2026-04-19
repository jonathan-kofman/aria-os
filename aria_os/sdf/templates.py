"""
SDF template library — deterministic SDF generators for common lattice +
TPMS parts, keyed on natural-language goals. Parallel to the CadQuery
template system; lets NL goals like "40x40x40mm octet-truss block" route
through regex spec-extract -> template -> STL without an LLM call.

Usage
-----
    from aria_os.sdf.templates import find_template, build_from_template

    tmpl_fn = find_template("40mm gyroid infill sphere, 1mm wall, 8mm cell")
    if tmpl_fn:
        sdf_func, bounds, meta = tmpl_fn(params)
        # sdf_func is a callable SDF; bounds is the eval envelope;
        # meta captures parameters used (for reporting/caching).
"""
from __future__ import annotations

import numpy as np

from . import (
    sdf_sphere, sdf_box, sdf_cylinder, sdf_rounded_box,
    sdf_gyroid, sdf_schwarz_p, sdf_schwarz_w, sdf_diamond,
    sdf_iwp, sdf_neovius, sdf_frd,
    sdf_octet_truss, sdf_bcc_lattice, sdf_fcc_lattice,
    sdf_lattice_cubic, sdf_kagome_lattice, sdf_honeycomb_2d,
    op_intersection, op_union, op_shell,
)
from .fgm import fgm_radial_gradient, fgm_linear_gradient


# ---------------------------------------------------------------------------
# TPMS / lattice name → constructor
# ---------------------------------------------------------------------------

_TPMS_CONSTRUCTORS = {
    "gyroid":     sdf_gyroid,
    "schwarz_p":  sdf_schwarz_p,
    "schwarz":    sdf_schwarz_p,
    "schwarz_w":  sdf_schwarz_w,
    "diamond":    sdf_diamond,
    "iwp":        sdf_iwp,
    "neovius":    sdf_neovius,
    "frd":        sdf_frd,
}

_STRUT_CONSTRUCTORS = {
    "octet":      sdf_octet_truss,
    "octet-truss": sdf_octet_truss,
    "octet_truss": sdf_octet_truss,
    "bcc":        sdf_bcc_lattice,
    "fcc":        sdf_fcc_lattice,
    "cubic":      sdf_lattice_cubic,
    "kagome":     sdf_kagome_lattice,
    "honeycomb":  sdf_honeycomb_2d,
}


# ---------------------------------------------------------------------------
# Templates — each returns (sdf_func, bounds, meta)
# ---------------------------------------------------------------------------

def _tpms_block(params: dict) -> tuple:
    """Rectangular block filled with a TPMS lattice.
    params: size_mm (scalar or tuple), tpms_type, cell_size_mm, thickness_mm
    """
    size = params.get("size_mm", 40.0)
    if isinstance(size, (int, float)):
        sx = sy = sz = float(size)
    else:
        sx, sy, sz = (float(x) for x in size)
    cell = float(params.get("cell_size_mm", 8.0))
    thk = float(params.get("thickness_mm", 1.0))
    tpms_type = params.get("tpms_type", "gyroid").lower()
    ctor = _TPMS_CONSTRUCTORS.get(tpms_type, sdf_gyroid)

    shell = sdf_box(size=(sx, sy, sz))
    infill = ctor(cell_size=cell, thickness=thk)
    part = op_intersection(shell, infill)
    bounds = ((-sx/2 - 1, -sy/2 - 1, -sz/2 - 1),
              (sx/2 + 1,  sy/2 + 1,  sz/2 + 1))
    return part, bounds, {
        "template": "tpms_block", "tpms_type": tpms_type,
        "size_mm": [sx, sy, sz], "cell_mm": cell, "thickness_mm": thk,
    }


def _strut_lattice_block(params: dict) -> tuple:
    """Rectangular block filled with a strut lattice (BCC/FCC/octet/Kagome).
    params: size_mm, lattice_type, cell_size_mm, beam_radius_mm
    """
    size = params.get("size_mm", 40.0)
    if isinstance(size, (int, float)):
        sx = sy = sz = float(size)
    else:
        sx, sy, sz = (float(x) for x in size)
    cell = float(params.get("cell_size_mm", 8.0))
    beam_r = float(params.get("beam_radius_mm", 1.0))
    lat_type = params.get("lattice_type", "octet").lower()
    ctor = _STRUT_CONSTRUCTORS.get(lat_type, sdf_octet_truss)

    shell = sdf_box(size=(sx, sy, sz))
    infill = ctor(cell_size=cell, beam_radius=beam_r)
    part = op_intersection(shell, infill)
    bounds = ((-sx/2 - 1, -sy/2 - 1, -sz/2 - 1),
              (sx/2 + 1,  sy/2 + 1,  sz/2 + 1))
    return part, bounds, {
        "template": "strut_lattice_block", "lattice_type": lat_type,
        "size_mm": [sx, sy, sz], "cell_mm": cell,
        "beam_radius_mm": beam_r,
    }


def _tpms_sphere(params: dict) -> tuple:
    """Sphere filled with a TPMS lattice."""
    radius = float(params.get("radius_mm", 20.0))
    cell = float(params.get("cell_size_mm", 6.0))
    thk = float(params.get("thickness_mm", 0.8))
    tpms_type = params.get("tpms_type", "gyroid").lower()
    ctor = _TPMS_CONSTRUCTORS.get(tpms_type, sdf_gyroid)

    shell = sdf_sphere(radius=radius)
    infill = ctor(cell_size=cell, thickness=thk)
    part = op_intersection(shell, infill)
    r = radius + 1
    bounds = ((-r, -r, -r), (r, r, r))
    return part, bounds, {
        "template": "tpms_sphere", "tpms_type": tpms_type,
        "radius_mm": radius, "cell_mm": cell, "thickness_mm": thk,
    }


def _lattice_shell(params: dict) -> tuple:
    """Outer rounded-box shell filled with a lattice (user chooses type)."""
    size = params.get("size_mm", 40.0)
    if isinstance(size, (int, float)):
        sx = sy = sz = float(size)
    else:
        sx, sy, sz = (float(x) for x in size)
    wall_mm = float(params.get("wall_mm", 2.0))
    cell = float(params.get("cell_size_mm", 8.0))
    thk = float(params.get("thickness_mm", 0.8))
    lat_type = params.get("lattice_type", "gyroid").lower()
    corner_r = float(params.get("corner_radius_mm", 2.0))

    outer = sdf_rounded_box(size=(sx, sy, sz), radius=corner_r)
    inner = sdf_rounded_box(
        size=(sx - 2 * wall_mm, sy - 2 * wall_mm, sz - 2 * wall_mm),
        radius=max(0.1, corner_r - wall_mm))
    solid_shell = op_intersection(outer, lambda x, y, z: -inner(x, y, z))

    # Choose infill kernel
    if lat_type in _TPMS_CONSTRUCTORS:
        infill_fn = _TPMS_CONSTRUCTORS[lat_type](cell_size=cell, thickness=thk)
    else:
        infill_fn = _STRUT_CONSTRUCTORS.get(
            lat_type, sdf_octet_truss)(cell_size=cell, beam_radius=thk)
    filled_inside = op_intersection(inner, infill_fn)
    part = op_union(solid_shell, filled_inside)

    bounds = ((-sx/2 - 1, -sy/2 - 1, -sz/2 - 1),
              (sx/2 + 1, sy/2 + 1, sz/2 + 1))
    return part, bounds, {
        "template": "lattice_shell", "lattice_type": lat_type,
        "size_mm": [sx, sy, sz], "wall_mm": wall_mm,
        "cell_mm": cell, "thickness_mm": thk,
    }


def _fgm_gyroid_block(params: dict) -> tuple:
    """Block with a radially-graded gyroid (denser at centre)."""
    size = params.get("size_mm", 40.0)
    if isinstance(size, (int, float)):
        sx = sy = sz = float(size)
    else:
        sx, sy, sz = (float(x) for x in size)
    cell = float(params.get("cell_size_mm", 8.0))
    t_min = float(params.get("t_min_mm", 0.3))
    t_max = float(params.get("t_max_mm", 1.5))
    r_max = float(params.get("r_max_mm", max(sx, sy, sz) / 2))

    density = fgm_radial_gradient(
        center=(0, 0, 0), r_min=0.0, r_max=r_max, t_min=t_min, t_max=t_max)

    def graded(x, y, z):
        t = density(x, y, z)
        k = 2 * np.pi / cell
        val = (np.sin(k * x) * np.cos(k * y)
               + np.sin(k * y) * np.cos(k * z)
               + np.sin(k * z) * np.cos(k * x))
        return np.abs(val) - t / 2

    shell = sdf_box(size=(sx, sy, sz))
    part = op_intersection(shell, graded)
    bounds = ((-sx/2 - 1, -sy/2 - 1, -sz/2 - 1),
              (sx/2 + 1, sy/2 + 1, sz/2 + 1))
    return part, bounds, {
        "template": "fgm_gyroid_block", "size_mm": [sx, sy, sz],
        "cell_mm": cell, "t_min_mm": t_min, "t_max_mm": t_max,
        "r_max_mm": r_max,
    }


def _honeycomb_panel(params: dict) -> tuple:
    """Hexagonal honeycomb panel extruded along Z.

    Resolves the thickness-mm collision: if a 3D size tuple was parsed
    from "WxHxD mm", use its Z as the panel depth; otherwise fall back
    to `panel_depth_mm`. `wall_mm` always wins for wall thickness so
    "0.5mm wall" can't accidentally become panel depth.
    """
    w = float(params.get("width_mm", 100.0))
    h = float(params.get("height_mm", 50.0))
    size = params.get("size_mm")
    if isinstance(size, tuple) and len(size) == 3:
        w, h, thk = (float(v) for v in size)
    else:
        thk = float(params.get("panel_depth_mm", params.get("depth_mm", 10.0)))
    cell = float(params.get("cell_size_mm", 5.0))
    wall = float(params.get("wall_mm", 0.5))

    honeycomb = sdf_honeycomb_2d(cell_size=cell, wall_thickness=wall,
                                 thickness=thk)
    bbox = sdf_box(size=(w, h, thk))
    part = op_intersection(bbox, honeycomb)
    bounds = ((-w/2 - 1, -h/2 - 1, -thk/2 - 1),
              (w/2 + 1, h/2 + 1, thk/2 + 1))
    return part, bounds, {
        "template": "honeycomb_panel", "dims_mm": [w, h, thk],
        "cell_mm": cell, "wall_mm": wall,
    }


# ---------------------------------------------------------------------------
# Goal-keyword → template routing
# ---------------------------------------------------------------------------

# Ordered: more-specific keywords before generic. First match wins.
# "sphere" / "panel" checks fire BEFORE lattice-type checks so a goal like
# "iwp sphere" routes to the sphere template, not a block of IWP.
_KEYWORD_TO_TEMPLATE = [
    (["fgm", "functionally graded", "variable density"], _fgm_gyroid_block),
    (["honeycomb panel", "honeycomb"],                   _honeycomb_panel),
    # Sphere templates — any lattice + sphere goes here first
    (["tpms sphere", "gyroid sphere", "spherical lattice",
      "iwp sphere", "schwarz sphere", "diamond sphere",
      "neovius sphere", "lattice sphere"],               _tpms_sphere),
    (["lattice shell", "shelled lattice",
      "lattice enclosure", "hollow lattice"],            _lattice_shell),
    (["octet", "bcc", "fcc", "kagome", "strut lattice"],
                                                         _strut_lattice_block),
    (["tpms", "gyroid", "schwarz", "diamond", "iwp",
      "neovius", "lattice"],                             _tpms_block),
]


def find_template(goal: str):
    """Scan the goal text for keywords. Return a template function or None."""
    g = goal.lower()
    for kws, fn in _KEYWORD_TO_TEMPLATE:
        if any(kw in g for kw in kws):
            return fn
    return None


def extract_sdf_params(goal: str, base_params: dict | None = None) -> dict:
    """Regex-extract common SDF params from NL goal. Merges with base_params.

    Recognises:
      - size: "40mm", "40x30x20mm", "40x40mm", "50x50x50"
      - cell: "8mm cell", "cell=8mm", "8mm unit cell"
      - thickness: "1mm wall", "0.8mm thick", "wall=0.8mm"
      - beam radius: "1mm beam", "beam=0.5mm"
      - tpms type: gyroid, schwarz, diamond, iwp, neovius, frd, schwarz-w
      - lattice type: octet, bcc, fcc, kagome, honeycomb, cubic
      - radius: "20mm radius", "40mm sphere", "r=25mm"
    """
    import re
    p = dict(base_params or {})
    g = goal.lower()

    # Box dims "40x30x20mm" or "40x40mm" or "40mm"
    m = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*mm", g)
    if m:
        p["size_mm"] = (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*mm", g)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            p["width_mm"] = a
            p["height_mm"] = b
            p["size_mm"] = (a, b, b)  # cube-ish default
        else:
            m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:cube|block)", g)
            if m:
                p["size_mm"] = float(m.group(1))

    # Cell size
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:cell|unit\s*cell)"
                  r"|cell\s*=\s*(\d+(?:\.\d+)?)\s*mm", g)
    if m:
        v = m.group(1) or m.group(2)
        p["cell_size_mm"] = float(v)

    # Wall / thickness
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:wall|thick)"
                  r"|wall\s*=\s*(\d+(?:\.\d+)?)\s*mm"
                  r"|thickness\s*=\s*(\d+(?:\.\d+)?)\s*mm", g)
    if m:
        v = m.group(1) or m.group(2) or m.group(3)
        p["thickness_mm"] = float(v)
        p["wall_mm"] = float(v)

    # Beam radius
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*beam"
                  r"|beam\s*=\s*(\d+(?:\.\d+)?)\s*mm"
                  r"|beam\s+radius\s*(\d+(?:\.\d+)?)\s*mm", g)
    if m:
        v = m.group(1) or m.group(2) or m.group(3)
        p["beam_radius_mm"] = float(v)

    # Sphere radius
    m = re.search(r"(\d+(?:\.\d+)?)\s*mm\s*(?:radius|sphere)"
                  r"|r\s*=\s*(\d+(?:\.\d+)?)\s*mm", g)
    if m:
        v = m.group(1) or m.group(2)
        p["radius_mm"] = float(v)

    # TPMS / lattice type
    for tname in ("schwarz-w", "iwp", "neovius", "frd",
                  "gyroid", "schwarz", "diamond"):
        if tname in g:
            p["tpms_type"] = tname.replace("-", "_")
            break
    for lname in ("octet-truss", "octet_truss", "octet",
                  "bcc", "fcc", "kagome", "honeycomb", "cubic"):
        if lname in g:
            p["lattice_type"] = lname.replace("-", "_")
            break

    return p


def build_from_template(goal: str, base_params: dict | None = None):
    """Top-level: goal string + optional base params → (sdf, bounds, meta).
    Returns None if no template matches the goal."""
    fn = find_template(goal)
    if fn is None:
        return None
    params = extract_sdf_params(goal, base_params)
    return fn(params)
