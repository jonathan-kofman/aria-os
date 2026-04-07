"""ReconstructAgent — generate CadQuery scripts from scan PartFeatureSet.

Takes a PartFeatureSet (from the scan pipeline) plus the cleaned mesh,
and produces a parametric CadQuery script that recreates the geometry.

Strategy per topology:
  prismatic   — box from bounding box, holes detected from mesh surface analysis
  turned_part — revolve profile from detected cylinder diameters
  freeform    — import the cleaned mesh directly (no parametric reconstruction)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from .. import event_bus
from ..models.scan_models import BoundingBox, CatalogEntry, DetectedPrimitive, PartFeatureSet


def reconstruct(
    features: PartFeatureSet,
    cleaned_mesh_path: str,
    output_dir: str | Path,
    part_id: str = "reconstructed",
) -> dict:
    """
    Generate a CadQuery script from detected features.

    Returns dict with:
        script_path: path to the .py CadQuery script
        step_path:   path to exported STEP (if CadQuery available)
        stl_path:    path to exported STL (if CadQuery available)
        bbox:        dict with x, y, z dimensions
        error:       error message or None
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    topology = features.topology
    bbox = features.parametric_description.get("bounding_box_mm", {})

    event_bus.emit("scan", f"[Reconstruct] Topology: {topology}, generating CadQuery script")

    if topology == "prismatic":
        holes = _detect_holes_from_mesh(cleaned_mesh_path, bbox)
        script = _generate_prismatic(bbox, holes, part_id)
    elif topology == "turned_part":
        cylinders = [p for p in features.primitives if p.type == "cylinder"]
        script = _generate_turned(bbox, cylinders, part_id)
    else:
        script = _generate_freeform_import(cleaned_mesh_path, part_id)

    # Write script
    script_path = output_dir / f"{part_id}_reconstruct.py"
    script_path.write_text(script, encoding="utf-8")
    event_bus.emit("scan", f"[Reconstruct] Script: {script_path}")

    # Try to execute the script to produce STEP/STL
    step_path = str(output_dir / f"{part_id}.step")
    stl_path = str(output_dir / f"{part_id}.stl")
    result_bbox = None
    error = None

    try:
        result_bbox, error = _execute_cq_script(script, step_path, stl_path)
    except Exception as exc:
        error = str(exc)

    if result_bbox:
        event_bus.emit("scan",
                       f"[Reconstruct] Generated: {result_bbox['x']}x{result_bbox['y']}x{result_bbox['z']}mm",
                       {"bbox": result_bbox})

    return {
        "script_path": str(script_path),
        "step_path": step_path if Path(step_path).exists() else "",
        "stl_path": stl_path if Path(stl_path).exists() else "",
        "bbox": result_bbox,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Hole detection from mesh surface
# ---------------------------------------------------------------------------

def _detect_holes_from_mesh(
    mesh_path: str, bbox: dict
) -> List[dict]:
    """
    Detect cylindrical holes by analyzing the mesh surface directly.

    Strategy: find circular boundary loops on flat faces. A hole through a
    prismatic body appears as a ring of vertices at a consistent radius from
    a center point, with their normals pointing inward (toward the hole axis).

    Returns list of dicts: {center: [x,y,z], axis: [ax,ay,az], radius_mm: float, depth_mm: float}
    """
    import trimesh

    mesh = trimesh.load(mesh_path, force="mesh")
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    face_normals = np.asarray(mesh.face_normals, dtype=np.float64)

    bx = bbox.get("x", 0)
    by = bbox.get("y", 0)
    bz = bbox.get("z", 0)

    holes: List[dict] = []

    # For each principal axis, look for holes through the body
    for axis_idx, axis_name, body_thickness in [
        (2, "Z", bz),
        (1, "Y", by),
        (0, "X", bx),
    ]:
        axis_vec = np.zeros(3)
        axis_vec[axis_idx] = 1.0

        # Find faces whose normals are perpendicular to this axis
        # (hole walls have normals perpendicular to the hole axis)
        dots = np.abs(face_normals @ axis_vec)
        perp_mask = dots < 0.3  # normals roughly perpendicular to axis

        if perp_mask.sum() < 10:
            continue

        # Get vertices of perpendicular faces
        perp_face_indices = np.where(perp_mask)[0]
        perp_verts_idx = np.unique(mesh.faces[perp_face_indices].flatten())
        perp_pts = vertices[perp_verts_idx]

        if len(perp_pts) < 10:
            continue

        # Cluster these vertices by their position projected onto the
        # plane perpendicular to the axis. Each cluster = one hole.
        # Project out the axis component
        other_axes = [i for i in range(3) if i != axis_idx]
        proj = perp_pts[:, other_axes]

        # Use simple grid-based clustering
        found = _cluster_circles(proj, perp_pts, axis_idx, body_thickness, bbox)
        holes.extend(found)

    # Deduplicate holes that are very close to each other
    holes = _deduplicate_holes(holes)

    event_bus.emit("scan", f"[Reconstruct] Detected {len(holes)} holes from mesh")
    return holes


def _cluster_circles(
    proj_2d: np.ndarray,
    full_3d: np.ndarray,
    axis_idx: int,
    body_thickness: float,
    bbox: dict,
) -> List[dict]:
    """Find circular clusters in 2D projected points."""
    if len(proj_2d) < 10:
        return []

    from scipy.spatial import cKDTree

    bbox_dims = [bbox.get("x", 100), bbox.get("y", 100), bbox.get("z", 100)]
    search_r = max(bbox_dims) * 0.3
    max_hole_r = min(bbox_dims) * 0.4

    # Build KD-tree for fast neighbor queries
    tree = cKDTree(proj_2d)

    visited = set()
    holes = []

    for i in range(len(proj_2d)):
        if i in visited:
            continue

        # Find points within a scaled radius of this point
        neighbors = tree.query_ball_point(proj_2d[i], r=search_r)
        if len(neighbors) < 8:
            continue

        cluster_pts = proj_2d[neighbors]
        cluster_3d = full_3d[neighbors]

        # Check if these points form a circle
        center_2d = cluster_pts.mean(axis=0)
        radii = np.linalg.norm(cluster_pts - center_2d, axis=1)

        # For a real circle, radii should be consistent
        if len(radii) < 8:
            continue

        median_r = float(np.median(radii))
        if median_r < 1.0 or median_r > max_hole_r:
            continue

        # Check if the points are actually ring-shaped (not a filled disc)
        # Ring: radii cluster near the median. Disc: radii spread from 0 to max.
        r_std = float(np.std(radii))
        if r_std > median_r * 0.4:  # too much spread = not a ring
            continue

        # Inliers within 20% of median radius
        inlier_mask = np.abs(radii - median_r) < median_r * 0.2
        if inlier_mask.sum() < 8:
            continue

        # Compute hole center in 3D and depth
        inlier_3d = cluster_3d[inlier_mask]
        center_3d = inlier_3d.mean(axis=0)
        depth = float(inlier_3d[:, axis_idx].max() - inlier_3d[:, axis_idx].min())
        if depth < 1.0:
            depth = body_thickness  # assume through-hole

        axis_vec = [0.0, 0.0, 0.0]
        axis_vec[axis_idx] = 1.0

        holes.append({
            "center": center_3d.tolist(),
            "axis": axis_vec,
            "radius_mm": round(median_r, 2),
            "depth_mm": round(depth, 2),
        })

        # Mark neighbors as visited
        visited.update(neighbors)

    return holes


def _deduplicate_holes(holes: List[dict], dist_threshold: float = 3.0) -> List[dict]:
    """Remove duplicate holes (same center within threshold)."""
    if len(holes) <= 1:
        return holes

    unique = [holes[0]]
    for h in holes[1:]:
        c = np.array(h["center"])
        is_dup = False
        for u in unique:
            uc = np.array(u["center"])
            if np.linalg.norm(c - uc) < dist_threshold:
                is_dup = True
                break
        if not is_dup:
            unique.append(h)
    return unique


# ---------------------------------------------------------------------------
# CadQuery script generators
# ---------------------------------------------------------------------------

def _generate_prismatic(bbox: dict, holes: List[dict], part_id: str) -> str:
    """Generate CadQuery script for a prismatic (box-like) part with holes."""
    x = bbox.get("x", 50.0)
    y = bbox.get("y", 30.0)
    z = bbox.get("z", 20.0)

    lines = [
        f'"""Reconstructed from scan: {part_id} (prismatic)"""',
        "import cadquery as cq",
        "",
        f"LENGTH_MM = {x}",
        f"WIDTH_MM  = {y}",
        f"HEIGHT_MM = {z}",
        "",
        '# Base body',
        'result = cq.Workplane("XY").box(LENGTH_MM, WIDTH_MM, HEIGHT_MM)',
    ]

    if holes:
        lines.append("")
        lines.append(f"# {len(holes)} detected hole(s)")

        for i, hole in enumerate(holes):
            cx, cy, cz = hole["center"]
            r = hole["radius_mm"]
            depth = hole["depth_mm"]
            axis = hole["axis"]

            # Determine which workplane and position based on hole axis
            if abs(axis[2]) > 0.5:  # Z-axis hole
                wp = "XY"
                hx, hy = round(cx, 2), round(cy, 2)
                offset = round(-z / 2 - 1, 2)
                cut_depth = round(z + 2, 2)
            elif abs(axis[1]) > 0.5:  # Y-axis hole
                wp = "XZ"
                hx, hy = round(cx, 2), round(cz, 2)
                offset = round(-y / 2 - 1, 2)
                cut_depth = round(y + 2, 2)
            else:  # X-axis hole
                wp = "YZ"
                hx, hy = round(cy, 2), round(cz, 2)
                offset = round(-x / 2 - 1, 2)
                cut_depth = round(x + 2, 2)

            lines.append(f"hole_{i} = (")
            lines.append(f'    cq.Workplane("{wp}")')
            lines.append(f"    .workplane(offset={offset})")
            lines.append(f"    .center({hx}, {hy})")
            lines.append(f"    .circle({round(r, 2)})")
            lines.append(f"    .extrude({cut_depth})")
            lines.append(f")")
            lines.append(f"result = result.cut(hole_{i})")

    lines.extend([
        "",
        "bb = result.val().BoundingBox()",
        'print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")',
    ])

    return "\n".join(lines) + "\n"


def _generate_turned(
    bbox: dict, cylinders: List[DetectedPrimitive], part_id: str
) -> str:
    """Generate CadQuery script for a turned (rotational) part."""
    if not cylinders:
        # Fallback: simple cylinder from bbox
        x, y, z = bbox.get("x", 30), bbox.get("y", 30), bbox.get("z", 40)
        r = max(x, y) / 2
        h = z
        return f'''"""Reconstructed from scan: {part_id} (turned_part)"""
import cadquery as cq

RADIUS_MM = {round(r, 2)}
HEIGHT_MM = {round(h, 2)}

result = cq.Workplane("XY").circle(RADIUS_MM).extrude(HEIGHT_MM)
result = result.translate((0, 0, -{round(h/2, 2)}))
bb = result.val().BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
'''

    # Sort cylinders by radius (largest = outer diameter)
    sorted_cyls = sorted(cylinders, key=lambda c: c.parameters["radius_mm"], reverse=True)

    lines = [
        f'"""Reconstructed from scan: {part_id} (turned_part)"""',
        "import cadquery as cq",
        "",
    ]

    # Main body = largest cylinder
    main = sorted_cyls[0]
    main_r = round(main.parameters["radius_mm"], 2)
    main_h = round(main.parameters["height_mm"], 2)

    lines.append(f"OD_MM     = {round(main_r * 2, 2)}")
    lines.append(f"RADIUS_MM = {main_r}")
    lines.append(f"HEIGHT_MM = {main_h}")
    lines.append("")
    lines.append("# Main body")
    lines.append('result = cq.Workplane("XY").circle(RADIUS_MM).extrude(HEIGHT_MM)')
    lines.append(f'result = result.translate((0, 0, -{round(main_h/2, 2)}))')

    # Additional diameters = stepped features or bores
    for i, cyl in enumerate(sorted_cyls[1:], 1):
        r = round(cyl.parameters["radius_mm"], 2)
        h = round(cyl.parameters["height_mm"], 2)
        center = cyl.parameters.get("center", [0, 0, 0])

        if r < main_r * 0.95:
            # Smaller cylinder — bore or step
            cx = round(center[0], 2)
            cy = round(center[1], 2)
            lines.append(f"")
            lines.append(f"# Step/bore #{i}: dia {round(r*2, 2)}mm x {h}mm")
            lines.append(f"bore_{i} = (")
            lines.append(f'    cq.Workplane("XY")')
            lines.append(f"    .workplane(offset={round(-h/2 - 1, 2)})")
            lines.append(f"    .center({cx}, {cy})")
            lines.append(f"    .circle({r})")
            lines.append(f"    .extrude({round(h + 2, 2)})")
            lines.append(f")")
            lines.append(f"result = result.cut(bore_{i})")

    lines.extend([
        "",
        "bb = result.val().BoundingBox()",
        'print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")',
    ])

    return "\n".join(lines) + "\n"


def _generate_freeform_import(mesh_path: str, part_id: str) -> str:
    """Generate a script that imports the mesh as-is (no parametric reconstruction)."""
    return f'''"""Reconstructed from scan: {part_id} (freeform — mesh import only)"""
import cadquery as cq
import trimesh

# Freeform parts cannot be parametrically reconstructed.
# This script loads the cleaned mesh for reference.
MESH_PATH = r"{mesh_path}"

mesh = trimesh.load(MESH_PATH)
bb = mesh.bounding_box.extents
print(f"BBOX:{{bb[0]:.3f}},{{bb[1]:.3f}},{{bb[2]:.3f}}")
print(f"Note: freeform mesh loaded, not parametric CadQuery geometry")

# For CadQuery workflows, create a simple bounding box placeholder:
result = cq.Workplane("XY").box(float(bb[0]), float(bb[1]), float(bb[2]))
bb2 = result.val().BoundingBox()
print(f"BBOX:{{bb2.xlen:.3f}},{{bb2.ylen:.3f}},{{bb2.zlen:.3f}}")
'''


# ---------------------------------------------------------------------------
# Script execution
# ---------------------------------------------------------------------------

def _execute_cq_script(
    script: str, step_path: str, stl_path: str
) -> Tuple[Optional[dict], Optional[str]]:
    """Execute CadQuery script and export STEP/STL."""
    try:
        import cadquery as cq
        from cadquery import exporters
    except ImportError:
        return None, "cadquery not installed"

    ns: dict = {"__builtins__": {
        "__import__": __import__,
        "range": range, "len": len, "print": print,
        "abs": abs, "min": min, "max": max, "round": round,
        "float": float, "int": int, "str": str,
        "list": list, "dict": dict, "tuple": tuple, "set": set,
        "bool": bool, "enumerate": enumerate, "zip": zip, "map": map,
        "isinstance": isinstance, "hasattr": hasattr, "getattr": getattr,
        "True": True, "False": False, "None": None,
        "ValueError": ValueError, "TypeError": TypeError,
        "RuntimeError": RuntimeError, "Exception": Exception,
    }}

    try:
        exec(compile(script, "<reconstruct>", "exec"), ns)
    except Exception as exc:
        return None, f"Script execution failed: {exc}"

    geom = ns.get("result")
    if geom is None:
        return None, "Script did not define 'result'"

    try:
        bb = geom.val().BoundingBox()
        bbox = {"x": round(bb.xlen, 2), "y": round(bb.ylen, 2), "z": round(bb.zlen, 2)}

        Path(step_path).parent.mkdir(parents=True, exist_ok=True)
        exporters.export(geom, step_path, exporters.ExportTypes.STEP)
        exporters.export(geom, stl_path, exporters.ExportTypes.STL, tolerance=0.01)

        return bbox, None
    except Exception as exc:
        return None, f"Export failed: {exc}"
