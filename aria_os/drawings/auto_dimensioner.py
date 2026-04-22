"""Auto-dimension extractor.

Given a STEP/STL path, return a list of dimension ops that should land
on a drawing:

  - Overall bounding-box: length × width × height (linear dims on
    front / top / right views)
  - Detected cylindrical holes: Ø + center position (diametric +
    linear to the nearest edge)
  - Detected outer cylinders: Ø on whichever view shows a circle

Output shape matches what the DWG planner emits:
    {"kind": "addDimension", "params": {...}, "label": "..."}

The dimensioner is LIBRARY-FREE (uses only trimesh) for portability —
richer STEP feature extraction can plug in later via pythonOCC.
"""
from __future__ import annotations

import math
from pathlib import Path


def _safe_load_mesh(path: str):
    try:
        import trimesh
    except Exception:
        return None
    try:
        return trimesh.load(path, force="mesh")
    except Exception:
        return None


def _detect_cylinders(mesh) -> list[dict]:
    """Very light cylinder detection: group faces whose normals lie in
    the XY plane (|Z|<ε) and whose centers share a common axis. Returns
    [{axis: 'Z', diameter_mm, center: [x, y, z], height: h}, ...].

    This is good enough to flag bolt holes and through-bores on an
    axis-aligned part (the common case for flanges, plates, brackets)."""
    import numpy as np
    try:
        normals = mesh.face_normals
        centers = mesh.triangles_center
    except Exception:
        return []

    # Faces whose normal has a small Z-component point radially outward
    # from a Z-axis cylinder. Conversely: faces whose normal is purely Z
    # belong to the top/bottom caps, not the cylindrical wall.
    # We approximate a "wall face" as one where |normal.z| < 0.35.
    wall_mask = np.abs(normals[:, 2]) < 0.35
    if not wall_mask.any():
        return []

    # Cluster walls by their XY footprint. Use 2mm buckets — fine enough
    # to separate distinct bolt holes but coarse enough to group faces
    # of the same cylinder together despite mesh tessellation noise.
    BUCKET_MM = 2.0
    buckets: dict[tuple[int, int], list[int]] = {}
    wall_idx = np.where(wall_mask)[0]
    for i in wall_idx:
        cx, cy = centers[i, 0], centers[i, 1]
        key = (round(cx / BUCKET_MM), round(cy / BUCKET_MM))
        buckets.setdefault(key, []).append(i)

    # Merge adjacent buckets (a cylinder of radius r is ~4 buckets wide)
    # by scanning for bucket centroids within ~2·BUCKET_MM of each other.
    merged: dict[tuple[int, int], list[int]] = {}
    used: set[tuple[int, int]] = set()
    for key, ids in buckets.items():
        if key in used or len(ids) < 3: continue
        cluster = list(ids)
        used.add(key)
        for k2, ids2 in buckets.items():
            if k2 in used: continue
            if abs(k2[0] - key[0]) <= 2 and abs(k2[1] - key[1]) <= 2:
                cluster.extend(ids2); used.add(k2)
        merged[key] = cluster

    # Each merged cluster of >=6 wall faces is probably a cylinder.
    cylinders: list[dict] = []
    for (kx, ky), ids in merged.items():
        if len(ids) < 6:
            continue
        ids_arr = np.array(ids)
        pts = centers[ids_arr]
        cx = float(np.mean(pts[:, 0]))
        cy = float(np.mean(pts[:, 1]))
        rs = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
        r_med = float(np.median(rs))
        if r_med < 0.5 or r_med > 500:
            continue
        z_min = float(np.min(pts[:, 2]))
        z_max = float(np.max(pts[:, 2]))
        cylinders.append({
            "axis": "Z",
            "diameter_mm": round(r_med * 2, 2),
            "center": [round(cx, 2), round(cy, 2),
                        round((z_min + z_max) / 2, 2)],
            "height_mm": round(z_max - z_min, 2),
        })
    # Dedupe cylinders whose centers coincide within 0.5mm
    deduped: list[dict] = []
    for c in cylinders:
        if any(math.hypot(c["center"][0] - d["center"][0],
                           c["center"][1] - d["center"][1]) < 0.5
                and abs(c["diameter_mm"] - d["diameter_mm"]) < 0.5
                for d in deduped):
            continue
        deduped.append(c)
    return deduped


def extract_dimensions(part_path: str) -> list[dict]:
    """Return a list of dim-ops ready to stream as `addDimension` native
    ops. Each has `kind: "addDimension"` and kind-specific `params`."""
    ops: list[dict] = []
    mesh = _safe_load_mesh(part_path)
    if mesh is None:
        return ops

    # Overall bounding box → 3 linear dims
    try:
        ext = mesh.bounding_box.extents  # (xlen, ylen, zlen)
    except Exception:
        return ops
    w, d, h = float(ext[0]), float(ext[1]), float(ext[2])
    ops.append({"kind": "addDimension",
                "params": {"dim_type": "linear", "view": "top",
                             "axis": "x", "value_mm": round(w, 2)},
                "label": f"Overall width: {w:.2f}mm (top view)"})
    ops.append({"kind": "addDimension",
                "params": {"dim_type": "linear", "view": "top",
                             "axis": "y", "value_mm": round(d, 2)},
                "label": f"Overall depth: {d:.2f}mm (top view)"})
    ops.append({"kind": "addDimension",
                "params": {"dim_type": "linear", "view": "front",
                             "axis": "z", "value_mm": round(h, 2)},
                "label": f"Overall height: {h:.2f}mm (front view)"})

    # Detected cylinders → Ø dims
    cyls = _detect_cylinders(mesh)
    # Largest cylinder is probably the outer diameter
    if cyls:
        cyls.sort(key=lambda c: c["diameter_mm"], reverse=True)
        for i, c in enumerate(cyls[:8]):  # cap at 8 to avoid noise
            # Top view shows circles for Z-axis cylinders
            ops.append({"kind": "addDimension",
                        "params": {"dim_type": "diameter",
                                     "view": "top",
                                     "value_mm": c["diameter_mm"],
                                     "center": c["center"][:2]},
                        "label": (f"Ø{c['diameter_mm']:.2f}mm "
                                   f"{'outer' if i == 0 else 'hole'} "
                                   f"at ({c['center'][0]:.1f}, "
                                   f"{c['center'][1]:.1f})")})
    return ops


def augment_drawing_plan(plan: list[dict], part_path: str) -> list[dict]:
    """Take an existing DWG plan (from dwg_planner.plan_simple_drawing)
    and append auto-extracted dimension ops so the result is fab-ready."""
    dim_ops = extract_dimensions(part_path)
    return list(plan) + dim_ops
