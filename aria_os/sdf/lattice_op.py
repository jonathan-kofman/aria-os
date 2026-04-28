"""Editable-lattice op — bake a lattice STL on demand from a recipe.

Sole purpose: feed the SW addin's `OpLatticeFeature` with a fresh STL
whenever the user changes one of the lattice parameters (cell size,
wall thickness, pattern, density). The SW addin records the recipe on
SW user parameters; the regen hook re-POSTs that recipe to
/api/native/lattice/bake on the dashboard, which calls into here.

Why a dedicated module: the existing `aria_os/sdf/expander.py` knows
how to render an `implicitInfill` op into an STL but it's tied to a
plan-step lifecycle. This shim is the simpler "give me bytes for these
parameters, ideally cached, never raise" surface the bridge needs.

Returns the path to the cached STL on disk (deterministic per recipe).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


# Pattern → SDF callable. Mirrors the keys in expander.py so the LLM
# planner and the regen hook accept the same names.
_PATTERN_NAMES = (
    "gyroid", "schwarz_p", "schwarz_w", "diamond", "iwp", "neovius",
    "octet_truss", "bcc", "fcc", "kagome", "honeycomb",
)


def cache_root() -> Path:
    """Persistent per-user cache. Hash of the recipe is the filename so
    repeat builds with the same params skip the (slow) marching cubes
    pass entirely."""
    base = Path(os.environ.get("LOCALAPPDATA",
                                str(Path.home() / ".cache"))) / "AriaLattice"
    base.mkdir(parents=True, exist_ok=True)
    return base


def recipe_key(recipe: dict[str, Any]) -> str:
    canon = {
        "pattern":         (recipe.get("pattern") or "gyroid").lower(),
        "cell_mm":         float(recipe.get("cell_mm", 8.0)),
        "wall_mm":         float(recipe.get("wall_mm", 1.0)),
        "bbox":            list(recipe.get("bbox") or
                                  [-25, -25, -25, 25, 25, 25]),
        "resolution":      int(recipe.get("resolution", 96)),
    }
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def normalise_recipe(recipe: dict[str, Any]) -> dict[str, Any]:
    """Apply the same defaults / clamps the bake function applies, so
    callers can echo a normalised recipe back to the user (so the SW
    user parameters reflect what was actually used)."""
    pattern = (recipe.get("pattern") or "gyroid").lower()
    if pattern not in _PATTERN_NAMES:
        pattern = "gyroid"
    cell_mm = max(1.0, min(50.0, float(recipe.get("cell_mm", 8.0))))
    wall_mm = max(0.2, min(cell_mm * 0.6,
                             float(recipe.get("wall_mm", 1.0))))
    bbox = list(recipe.get("bbox") or [-25, -25, -25, 25, 25, 25])
    if len(bbox) != 6:
        bbox = [-25, -25, -25, 25, 25, 25]
    res = max(32, min(256, int(recipe.get("resolution", 96))))
    return {
        "pattern":    pattern,
        "cell_mm":    cell_mm,
        "wall_mm":    wall_mm,
        "bbox":       bbox,
        "resolution": res,
    }


def bake(recipe: dict[str, Any], *, force: bool = False
          ) -> tuple[Path, dict]:
    """Bake the lattice STL for `recipe`. Returns (stl_path, recipe_used).

    On cache hit, returns the cached STL without re-meshing. On miss,
    runs marching cubes via the existing SDFScene and writes the STL
    next to the recipe JSON so the SW addin can swap the import body
    without orchestrating a fresh aria_os run.
    """
    rec = normalise_recipe(recipe)
    key = recipe_key(rec)
    out_dir = cache_root()
    stl_path = out_dir / f"{key}.stl"
    json_path = out_dir / f"{key}.json"
    if stl_path.is_file() and not force:
        return stl_path, rec

    json_path.write_text(json.dumps(rec, indent=2), encoding="utf-8")

    # Build the SDF using the same pattern map as expander.py so the
    # planner and the editable-regen path produce identical geometry.
    from aria_os.generators.sdf_generator import (
        SDFScene, sdf_gyroid, sdf_schwarz_p, sdf_diamond,
        sdf_box, op_intersection,
    )
    try:
        from aria_os.sdf.lattices import (
            sdf_schwarz_w, sdf_iwp, sdf_neovius,
            sdf_octet_truss, sdf_bcc_lattice, sdf_fcc_lattice,
            sdf_kagome_lattice, sdf_honeycomb_2d,
        )
    except Exception:
        sdf_schwarz_w = sdf_iwp = sdf_neovius = None
        sdf_octet_truss = sdf_bcc_lattice = sdf_fcc_lattice = None
        sdf_kagome_lattice = sdf_honeycomb_2d = None

    pat_map = {
        "gyroid":      sdf_gyroid,
        "schwarz_p":   sdf_schwarz_p,
        "schwarz_w":   sdf_schwarz_w,
        "diamond":     sdf_diamond,
        "iwp":         sdf_iwp,
        "neovius":     sdf_neovius,
        "octet_truss": sdf_octet_truss,
        "bcc":         sdf_bcc_lattice,
        "fcc":         sdf_fcc_lattice,
        "kagome":      sdf_kagome_lattice,
        "honeycomb":   sdf_honeycomb_2d,
    }
    fn = pat_map.get(rec["pattern"])
    if fn is None:
        raise ValueError(
            f"Unknown lattice pattern {rec['pattern']!r}; "
            f"available: {sorted(k for k, v in pat_map.items() if v is not None)}")
    try:
        infill = fn(cell_size=rec["cell_mm"], thickness=rec["wall_mm"])
    except TypeError:
        infill = fn(rec["cell_mm"], rec["wall_mm"])

    bbox = rec["bbox"]
    sx = bbox[3] - bbox[0]; sy = bbox[4] - bbox[1]; sz = bbox[5] - bbox[2]
    cx = (bbox[0] + bbox[3]) / 2
    cy = (bbox[1] + bbox[4]) / 2
    cz = (bbox[2] + bbox[5]) / 2
    shell = sdf_box(center=(cx, cy, cz), size=(sx, sy, sz))
    sdf_total = op_intersection(shell, infill)

    # SDFScene's `resolution` arg is the GRID SPACING in mm, not a
    # sample count. We convert recipe.resolution (samples per longest
    # axis) to spacing so the user's mental model — "more = smoother" —
    # matches the on-disk behaviour.
    longest_axis_mm = max(sx, sy, sz)
    spacing_mm = max(0.2, longest_axis_mm / float(rec["resolution"]))
    scene = SDFScene(resolution=spacing_mm)
    bounds = ((bbox[0] - 0.5, bbox[1] - 0.5, bbox[2] - 0.5),
              (bbox[3] + 0.5, bbox[4] + 0.5, bbox[5] + 0.5))
    mesh = scene.to_mesh(sdf_total, bounds=bounds)
    scene.export_stl(mesh, str(stl_path))
    return stl_path, rec


__all__ = ["bake", "cache_root", "recipe_key", "normalise_recipe"]
