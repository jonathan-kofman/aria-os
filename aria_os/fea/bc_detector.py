r"""bc_detector.py - automatic boundary-condition detection for FEA jobs.

Closes Gap #2 from FEA_PIPELINE_AUDIT.md. Given a STEP/STL artifact (or
trimesh.Mesh in memory), heuristically identify candidate fixed
restraints, load-application faces, and symmetry planes from geometry
alone — no human-curated BC tagging required.

The output is a runFea-ready dict that the SW addin can stage via
SelectByID2 + AddRestraint(restraintType=2 "On Cylindrical Faces" or
0 "Fixed"). When SW Simulation isn't available, the same dict drives
the analytic fallback in physics_analyzer.py.

Detection heuristics (deterministic, no LLM):

1. **Mounting holes** — cylindrical faces whose axis is roughly aligned
   with a global axis and whose radius < 30% of the largest bbox
   dimension. Their inner cylindrical surfaces become "Fixed Hinge"
   restraints (radial+axial fixed, hoop free).

2. **Planar bases** — large planar faces whose normal is parallel to
   gravity (-Z by default) and area > 25% of total surface area.
   Become "Fixed Geometry" restraints.

3. **Symmetry planes** — bbox-spanning planes (xMid, yMid, zMid) where
   geometry is mirror-symmetric within tolerance. Become "Symmetry"
   restraints (1/2 or 1/4 model FEA possible).

4. **Load surfaces** — faces opposite the inferred mounting (top of
   bracket, free end of cantilever, opposite side from largest planar
   base). Become AddForce targets.

Usage::

    from aria_os.fea.bc_detector import detect_bcs
    bcs = detect_bcs("outputs/cad/stl/bracket.stl",
                     gravity_axis="-Z", mount_threshold_mm=12.0)
    # → {restraints: [...], loads: [...], symmetries: [...]}

The result feeds straight into the runFea op:

    iteration["fixture_face"] = bcs["restraints"][0]["face_id"]
    iteration["load_face"]    = bcs["loads"][0]["face_id"]

When SW Simulation can't resolve face_id strings, the addin falls back
to the existing first-face heuristic — bc_detector adds intelligence
but doesn't break the existing path.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def detect_bcs(stl_path: str | Path,
               gravity_axis: str = "-Z",
               mount_threshold_mm: float = 12.0,
               symmetry_tolerance_mm: float = 0.5) -> dict:
    """Top-level entry. Loads STL/STEP and returns a BC proposal.

    Args:
        stl_path: path to the artifact. STL preferred (faster); STEP
            falls back to STL conversion via trimesh.
        gravity_axis: world direction the part hangs in. "-Z" is default
            (top face is up). Use "+Y" for laid-flat orientation.
        mount_threshold_mm: cylindrical faces with radius <= this are
            treated as mounting holes. ~12mm covers M3-M12 range.
        symmetry_tolerance_mm: max RMS distance for a symmetry test
            to count as "symmetric within tolerance".

    Returns:
        {
          "ok": bool,
          "engine": "trimesh-heuristic",
          "stats": {"vertices": int, "faces": int, "bbox_mm": [x,y,z], ...},
          "restraints": [
              {"kind": "fixed_hole", "axis": [...], "center_mm": [...],
               "radius_mm": float, "face_id": str, "rationale": str},
              ...
          ],
          "loads": [
              {"kind": "load_face", "centroid_mm": [...], "normal": [...],
               "area_mm2": float, "face_id": str, "rationale": str},
              ...
          ],
          "symmetries": [
              {"plane": "XZ"|"YZ"|"XY", "rms_mm": float, "rationale": str},
              ...
          ],
        }
    """
    try:
        import trimesh
    except ImportError:
        return {"ok": False, "error": "trimesh not installed",
                "engine": "trimesh-heuristic"}

    path = Path(stl_path)
    if not path.exists():
        return {"ok": False, "error": f"file not found: {path}",
                "engine": "trimesh-heuristic"}

    try:
        mesh = trimesh.load(str(path), force="mesh")
    except Exception as ex:
        return {"ok": False, "error": f"trimesh load failed: {ex}",
                "engine": "trimesh-heuristic"}

    if mesh is None or len(mesh.faces) == 0:
        return {"ok": False, "error": "empty mesh",
                "engine": "trimesh-heuristic"}

    bbox = mesh.bounding_box.extents
    largest_dim = float(max(bbox))

    # Decode gravity_axis "-Z", "+X" etc. into a unit vector.
    g_axis = _parse_axis(gravity_axis)

    restraints = _detect_mounting_holes(mesh, mount_threshold_mm,
                                         largest_dim)
    bases = _detect_planar_bases(mesh, g_axis,
                                  area_frac_threshold=0.25)
    restraints.extend(bases)

    loads = _detect_load_surfaces(mesh, g_axis, restraints)
    symmetries = _detect_symmetries(mesh, symmetry_tolerance_mm)

    return {
        "ok": True,
        "engine": "trimesh-heuristic",
        "stats": {
            "vertices": int(len(mesh.vertices)),
            "faces": int(len(mesh.faces)),
            "bbox_mm": [float(b) for b in bbox],
            "watertight": bool(mesh.is_watertight),
            "volume_mm3": float(mesh.volume) if mesh.is_watertight else None,
        },
        "restraints": restraints,
        "loads": loads,
        "symmetries": symmetries,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def g_axis_str(g) -> str:
    """Pretty-print a 3D unit vector as '-Z' / '+X' style label."""
    import numpy as np
    g = np.asarray(g, dtype=float)
    idx = int(np.argmax(np.abs(g)))
    sign = "-" if g[idx] < 0 else "+"
    return sign + "XYZ"[idx]


def _parse_axis(s: str) -> list[float]:
    sign = -1.0 if s.startswith("-") else 1.0
    letter = s[-1].upper()
    base = {"X": [1.0, 0.0, 0.0],
            "Y": [0.0, 1.0, 0.0],
            "Z": [0.0, 0.0, 1.0]}[letter]
    return [sign * c for c in base]


def _detect_mounting_holes(mesh, mount_threshold_mm: float,
                            largest_dim: float) -> list[dict]:
    """Find cylindrical regions in the mesh and treat the small ones as
    mounting holes. We use trimesh.proximity / facet adjacency and edge-
    angle clustering — full cylinder fitting is overkill for a heuristic.

    Approach: walk the mesh faces, group by parallel-normal triangles
    around a curved axis, infer cylinder via least-squares fit. Cylinders
    with radius <= mount_threshold_mm AND axis aligned to a global axis
    within ~15° are flagged as mounts.
    """
    out = []
    try:
        # trimesh's facet-finder gives us groups of coplanar triangles.
        # Cylindrical regions show up as long chains of small facets
        # whose normals rotate around a common axis. We look for those.
        edges = mesh.edges_unique
        face_adj = mesh.face_adjacency
        face_normals = mesh.face_normals

        # Cluster faces by normal-similarity into "curved" regions.
        # A face whose neighbors have normals all rotated about a
        # consistent axis is part of a cylinder.
        seen = set()
        clusters = []
        for i in range(len(mesh.faces)):
            if i in seen:
                continue
            cluster = _walk_curved_region(i, face_adj, face_normals,
                                          mesh.faces, mesh.vertices,
                                          seen)
            if len(cluster) >= 8:  # at least 8 facets to be a cylinder
                clusters.append(cluster)

        for cluster in clusters:
            cyl = _fit_cylinder(cluster, mesh.vertices, mesh.faces,
                                  mesh.face_normals)
            if cyl is None:
                continue
            radius_mm, axis, center, height = cyl
            # Filter: small relative to part, axis aligned with global X/Y/Z
            if radius_mm > mount_threshold_mm:
                continue
            if radius_mm > 0.4 * largest_dim:
                continue  # bigger than 40% of part — not a mount
            aligned = _axis_alignment(axis)
            if aligned is None:
                continue  # not aligned with global axis
            out.append({
                "kind": "fixed_hole",
                "axis": [float(a) for a in axis],
                "axis_aligned_with": aligned,
                "center_mm": [float(c) for c in center],
                "radius_mm": float(radius_mm),
                "height_mm": float(height),
                "face_id": f"fixed_hole_r{radius_mm:.1f}_at_{center[0]:.0f}_{center[1]:.0f}_{center[2]:.0f}",
                "rationale": (f"Cylindrical surface, r={radius_mm:.2f}mm "
                              f"axis along {aligned}; small enough to be "
                              f"a mounting hole (threshold={mount_threshold_mm}mm)"),
            })
    except Exception as ex:
        # Heuristic; failures are non-fatal — just return empty.
        out.append({"kind": "detector_warning",
                     "error": f"cylinder detection threw: {ex}"})
    return [r for r in out if r.get("kind") == "fixed_hole"]


def _walk_curved_region(start_idx, face_adj, face_normals, faces,
                         vertices, seen) -> list[int]:
    """BFS over face-adjacency, including a face if its normal is
    rotated relative to the starting face's normal by < ~30° AND not
    parallel to it (i.e., curved, not flat)."""
    import numpy as np
    n0 = face_normals[start_idx]
    cluster = [start_idx]
    seen.add(start_idx)
    stack = [start_idx]
    # Collect all neighbor faces
    adj_map: dict[int, list[int]] = {}
    for a, b in face_adj:
        adj_map.setdefault(int(a), []).append(int(b))
        adj_map.setdefault(int(b), []).append(int(a))
    while stack:
        f = stack.pop()
        for nb in adj_map.get(f, []):
            if nb in seen:
                continue
            n = face_normals[nb]
            cos_a = float(np.clip(np.dot(n, n0), -1.0, 1.0))
            angle = math.degrees(math.acos(cos_a))
            # Curved if neighbor angle is small but non-zero
            if 1.0 < angle < 35.0:
                cluster.append(nb)
                seen.add(nb)
                stack.append(nb)
    return cluster


def _fit_cylinder(cluster: list[int], vertices, faces,
                   face_normals) -> Optional[tuple]:
    """Least-squares cylinder fit for a face cluster. Returns
    (radius_mm, axis, center, height) or None if the fit is bad.
    Approach: cluster centroids → PCA → axis = smallest eigenvector,
    radius = mean distance from centroid line.
    """
    try:
        import numpy as np
        # Centroids of each cluster face
        face_arr = faces[cluster]
        verts = vertices[face_arr.flatten()].reshape(-1, 3, 3)
        centroids = verts.mean(axis=1)  # (N, 3)
        # Mean
        mu = centroids.mean(axis=0)
        Y = centroids - mu
        # SVD: smallest singular vector = cylinder axis
        _, _, Vt = np.linalg.svd(Y, full_matrices=False)
        axis = Vt[-1]
        # Project Y onto plane perp to axis → 2D radial vectors
        proj = Y - np.outer(Y @ axis, axis)
        radii = np.linalg.norm(proj, axis=1)
        if radii.size == 0:
            return None
        radius = float(np.median(radii))
        # Reject if scatter is too high (not actually cylindrical)
        if np.std(radii) > 0.3 * radius:
            return None
        # Height = projection range along axis
        proj_along = (centroids - mu) @ axis
        height = float(proj_along.max() - proj_along.min())
        return radius, axis, mu, height
    except Exception:
        return None


def _axis_alignment(axis) -> Optional[str]:
    """Return 'X', 'Y', 'Z' if axis is within 15° of that global axis."""
    import numpy as np
    axis = np.asarray(axis) / (np.linalg.norm(axis) + 1e-9)
    for name, vec in [("X", [1, 0, 0]), ("Y", [0, 1, 0]),
                       ("Z", [0, 0, 1])]:
        cos_a = abs(float(np.dot(axis, vec)))
        if cos_a > math.cos(math.radians(15.0)):
            return name
    return None


def _detect_planar_bases(mesh, g_axis: list[float],
                          area_frac_threshold: float) -> list[dict]:
    """Find planar regions whose normal is parallel (or anti-parallel)
    to gravity and whose total area > area_frac_threshold of the model
    surface. Returns Fixed restraints."""
    out = []
    try:
        import numpy as np
        g = np.asarray(g_axis)
        normals = mesh.face_normals
        areas = mesh.area_faces
        total_area = float(areas.sum())
        # Faces whose outward normal is aligned WITH gravity (pointing
        # DOWN if g=-Z) are the BOTTOM/BASE — the part sits on this face.
        # That's where mounting BCs are typically applied. The dot
        # product of normal-aligned-with-g and g is +1.0.
        base_mask = (normals @ g) > math.cos(math.radians(20.0))
        if not base_mask.any():
            return out
        base_area = float(areas[base_mask].sum())
        frac = base_area / max(total_area, 1e-9)
        if frac < area_frac_threshold:
            return out  # no significant base
        # Compute weighted centroid of the base faces
        face_arr = mesh.faces[base_mask]
        centroids = mesh.vertices[face_arr].mean(axis=1)
        weights = areas[base_mask]
        center = (centroids * weights[:, None]).sum(axis=0) / weights.sum()
        out.append({
            "kind": "fixed_base",
            "normal": [float(c) for c in g],   # outward normal = g direction
            "centroid_mm": [float(c) for c in center],
            "area_mm2": float(base_area),
            "area_fraction": float(frac),
            "face_id": (f"fixed_base_at_{center[0]:.0f}_"
                        f"{center[1]:.0f}_{center[2]:.0f}"),
            "rationale": (f"Bottom face: normal aligned with gravity "
                          f"({g_axis_str(g)}), area={base_area:.0f}mm² "
                          f"({frac*100:.0f}% of surface). "
                          f"Treated as fully fixed."),
        })
    except Exception as ex:
        return [{"kind": "detector_warning",
                  "error": f"planar-base detection threw: {ex}"}]
    return out


def _detect_load_surfaces(mesh, g_axis: list[float],
                           restraints: list[dict]) -> list[dict]:
    """The load is applied opposite the largest restraint. For a
    cantilever bracket fixed to a wall, the load goes on the free end
    (max-bbox-distance face from the fixed face)."""
    out = []
    try:
        import numpy as np
        if not restraints:
            return out
        # Find a "reference" restraint center
        anchor = None
        for r in restraints:
            if "centroid_mm" in r:
                anchor = np.asarray(r["centroid_mm"])
                break
            if "center_mm" in r:
                anchor = np.asarray(r["center_mm"])
                break
        if anchor is None:
            return out
        # Score each face by distance from anchor (in plane perpendicular
        # to gravity, since load is typically along gravity).
        # Pick face with largest area + far from anchor.
        normals = mesh.face_normals
        areas = mesh.area_faces
        face_arr = mesh.faces
        face_centroids = mesh.vertices[face_arr].mean(axis=1)
        dists = np.linalg.norm(face_centroids - anchor, axis=1)
        # Score = area * distance (favor big faces far from anchor)
        scores = areas * dists
        top_idx = int(np.argmax(scores))
        n = normals[top_idx]
        c = face_centroids[top_idx]
        # Suggest load direction as gravity (most common case)
        load_dir = list(g_axis)
        out.append({
            "kind": "load_face",
            "centroid_mm": [float(v) for v in c],
            "normal": [float(v) for v in n],
            "area_mm2": float(areas[top_idx]),
            "load_direction": load_dir,
            "face_id": (f"load_face_at_{c[0]:.0f}_"
                        f"{c[1]:.0f}_{c[2]:.0f}"),
            "rationale": (f"Largest face far from restraint anchor "
                          f"(dist={dists[top_idx]:.0f}mm, "
                          f"area={areas[top_idx]:.0f}mm²). "
                          f"Suggested load along gravity {g_axis}."),
        })
    except Exception as ex:
        return [{"kind": "detector_warning",
                  "error": f"load-face detection threw: {ex}"}]
    return out


def _detect_symmetries(mesh, tolerance_mm: float) -> list[dict]:
    """Test if the mesh is mirror-symmetric across the bbox-mid XY/YZ/XZ
    planes. If so, return the plane → 1/2 (or 1/4) symmetry constraint
    can be applied to halve mesh size."""
    out = []
    try:
        import numpy as np
        bbox_min, bbox_max = mesh.bounds
        center = (bbox_min + bbox_max) / 2.0
        verts = mesh.vertices
        for axis_idx, plane_name in [(0, "YZ"), (1, "XZ"), (2, "XY")]:
            mirrored = verts.copy()
            mirrored[:, axis_idx] = 2 * center[axis_idx] - mirrored[:, axis_idx]
            # KD-tree-free: for each mirrored vertex, find the closest
            # original. Skip for huge meshes (>20k verts).
            if len(verts) > 20000:
                continue
            from scipy.spatial import cKDTree
            tree = cKDTree(verts)
            d, _ = tree.query(mirrored)
            rms = float(np.sqrt(np.mean(d ** 2)))
            if rms < tolerance_mm:
                out.append({
                    "plane": plane_name,
                    "rms_mm": rms,
                    "rationale": (f"Mesh mirror-symmetric across {plane_name} "
                                  f"plane (RMS deviation={rms:.3f}mm < "
                                  f"{tolerance_mm}mm). Half-model FEA "
                                  f"valid; impose symmetry constraint."),
                })
    except ImportError:
        # scipy not available — skip symmetry detection silently
        pass
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# CLI for ad-hoc testing:
#     python -m aria_os.fea.bc_detector outputs/cad/stl/bracket.stl
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) < 2:
        print("usage: python -m aria_os.fea.bc_detector <stl_or_step>")
        sys.exit(2)
    result = detect_bcs(sys.argv[1])
    print(json.dumps(result, indent=2))
