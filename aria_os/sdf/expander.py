"""SDF plan-expander.

Implicit ops (`implicitInfill`, `implicitChannel`, `implicitLattice`,
`implicitField`) are emitted by the LLM planner BUT can't be executed
directly in Fusion / Rhino / Onshape — those hosts have no native
implicit-geometry primitive. The expander walks the plan, evaluates
each implicit op via the SDF kernel, writes an STL, and replaces the
implicit op with a `meshImportAndCombine` op pointing to that STL.

The host bridge then handles a single, simple op:
  "import this STL, boolean-combine with body X using mode Y."

Architecture:
    [implicitInfill ...]   →  expander  →  [meshImportAndCombine
                                              stl_path=outputs/sdf/<id>.stl,
                                              target=<body>,
                                              operation=<op>]

The expander is host-agnostic — Fusion, Onshape, and Rhino bridges all
handle `meshImportAndCombine` the same way (per W3.4).

Usage:
    from aria_os.sdf.expander import expand_plan
    expanded = expand_plan(plan, run_dir=Path("outputs/runs/foo"))
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

# We don't import the full sdf kernel at module-import time — the
# expander is exercised in unit tests that don't have skimage on
# every CI runner. Lazy-import inside the heavy functions.


_IMPLICIT_KINDS = {"implicitInfill", "implicitChannel", "implicitLattice",
                    "implicitField"}


def expand_plan(plan: list[dict], *,
                  run_dir: Path | None = None,
                  bbox_hint: tuple[float, float, float, float, float, float] | None = None
                  ) -> list[dict]:
    """Walk the plan; for every implicit op, render its SDF to an STL
    and replace the op with `meshImportAndCombine`.

    Args:
        plan:        ordered ops as emitted by the planner.
        run_dir:     where to write STL files. If None, uses
                     outputs/sdf/<hash> (per-implicit caching).
        bbox_hint:   xyz bounds (mm) to evaluate SDFs over. If None,
                     defaults to ±100mm cube — fine for most parts;
                     callers with bigger geometry pass an explicit hint.

    Returns:
        A new plan with implicit ops replaced. Non-implicit ops pass
        through unchanged. Unknown failures (e.g. SDF library missing)
        are passed through with a warning op so the rest of the plan
        still runs."""
    if run_dir is None:
        run_dir = Path("outputs/sdf")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    out: list[dict] = []
    for op in plan:
        kind = op.get("kind")
        if kind not in _IMPLICIT_KINDS:
            out.append(op)
            continue
        try:
            replacement = _expand_one(op, run_dir, bbox_hint)
            out.append(replacement)
        except Exception as exc:
            # Surface but don't crash the plan
            out.append({
                "kind": "noop",
                "label": (f"Skipped {kind}: {type(exc).__name__}: "
                            f"{str(exc)[:120]}"),
                "params": {"reason": str(exc)[:200]},
            })
    return out


def _stl_cache_path(run_dir: Path, op: dict) -> Path:
    """Stable hash of an implicit op → cache path. Reusing the same
    SDF across runs is free."""
    payload = json.dumps({"kind": op.get("kind"),
                            "params": op.get("params") or {}},
                           sort_keys=True, default=str)
    h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return run_dir / f"{op.get('kind')}_{h}.stl"


def _expand_one(op: dict, run_dir: Path,
                  bbox_hint: tuple | None) -> dict:
    """Render a single implicit op's SDF and emit a meshImportAndCombine."""
    kind = op["kind"]
    params = op.get("params") or {}
    stl_path = _stl_cache_path(run_dir, op)
    if not stl_path.is_file():
        _render_to_stl(kind, params, stl_path, bbox_hint)
    return {
        "kind": "meshImportAndCombine",
        "label": op.get("label", f"SDF {kind}"),
        "params": {
            "stl_path": str(stl_path.resolve()),
            "target":   params.get("target"),
            "operation": params.get("operation", "intersect"),
            "alias":    params.get("alias",
                                     f"{kind}_combined"),
        },
    }


def _render_to_stl(kind: str, params: dict, out_path: Path,
                    bbox_hint: tuple | None) -> None:
    """Build the SDF function for this op, evaluate on a grid, mesh,
    and write STL. Uses the existing SDFScene from sdf_generator."""
    from aria_os.generators.sdf_generator import SDFScene
    from aria_os.generators.sdf_generator import (
        sdf_box, sdf_sphere, sdf_cylinder,
        op_intersection, op_union, op_difference,
        sdf_gyroid, sdf_schwarz_p, sdf_diamond,
    )
    # Lattices: prefer the richer set in sdf.lattices when available.
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

    bounds = bbox_hint or (-100, -100, -100, 100, 100, 100)

    if kind == "implicitInfill":
        pattern = (params.get("pattern") or "gyroid").lower()
        cell = float(params.get("cell_mm", 8.0))
        density = float(params.get("density", 0.5))
        # Map density (0..1) → wall thickness offset for the TPMS.
        # Higher density means thicker walls (smaller offset to skin).
        thickness = 0.5 + density * 1.2  # heuristic
        pat_map = {
            "gyroid":     sdf_gyroid,
            "schwarz_p":  sdf_schwarz_p,
            "diamond":    sdf_diamond,
            "schwarz_w":  sdf_schwarz_w,
            "iwp":        sdf_iwp,
            "neovius":    sdf_neovius,
            "octet_truss":  sdf_octet_truss,
            "bcc":        sdf_bcc_lattice,
            "fcc":        sdf_fcc_lattice,
            "kagome":     sdf_kagome_lattice,
            "honeycomb":  sdf_honeycomb_2d,
        }
        fn = pat_map.get(pattern)
        if fn is None:
            raise ValueError(f"Unknown infill pattern {pattern!r}")
        try:
            sdf = fn(cell_size=cell, thickness=thickness)
        except TypeError:
            # Older signature: (cell, thickness)
            sdf = fn(cell, thickness)
    elif kind == "implicitChannel":
        # Build a channel along the `path` sketch as a tube.
        # MVP: approximate the path as a polyline and sweep a sphere.
        # The proper version would lookup path vertices from the
        # plan's sketch; for now, default to a straight tube of the
        # given diameter centered in the bbox.
        d = float(params.get("diameter", 5.0))
        half_len = (bounds[3] - bounds[0]) * 0.4
        sdf = sdf_cylinder(center=(0, 0, 0), radius=d / 2,
                            height=2 * half_len)
    elif kind == "implicitLattice":
        cell_kind = (params.get("cell") or "octet").lower()
        size = float(params.get("size", 10.0))
        thickness = float(params.get("thickness", 1.0))
        cell_map = {
            "octet":      sdf_octet_truss,
            "bcc":        sdf_bcc_lattice,
            "fcc":        sdf_fcc_lattice,
            "kagome":     sdf_kagome_lattice,
            "honeycomb":  sdf_honeycomb_2d,
        }
        fn = cell_map.get(cell_kind)
        if fn is None:
            raise ValueError(f"Unknown lattice cell {cell_kind!r}")
        try:
            sdf = fn(cell_size=size, thickness=thickness)
        except TypeError:
            sdf = fn(size, thickness)
    elif kind == "implicitField":
        # Pure-numpy expression evaluated in a sandboxed namespace.
        # Caller passes `expr` in the form of a python lambda body
        # over (x, y, z).
        import numpy as np
        expr = str(params.get("expr") or "x*0")
        sdf = lambda x, y, z, _e=expr: eval(
            _e, {"__builtins__": {}},
            {"x": x, "y": y, "z": z, "np": np,
             "sin": np.sin, "cos": np.cos, "exp": np.exp,
             "sqrt": np.sqrt, "abs": np.abs, "min": np.minimum,
             "max": np.maximum, "pi": np.pi})
        if params.get("bounds"):
            bounds = tuple(params["bounds"])
    else:
        raise ValueError(f"_render_to_stl: unhandled kind {kind!r}")

    scene = SDFScene()
    mesh_data = scene.to_mesh(sdf, bounds=bounds)
    scene.export_stl(mesh_data, str(out_path))


__all__ = ["expand_plan"]
