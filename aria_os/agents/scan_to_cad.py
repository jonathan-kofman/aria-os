"""3D scan ingestion → parametric CAD plan.

Takes a mesh (STL/PLY/OBJ/3MF) from a depth camera, photogrammetry,
or LiDAR and converts it into a parametric ARIA plan via:
  1. Mesh repair (close holes, remove duplicate verts, orient normals)
  2. Geometry analysis: bbox, volume, principal axes, primitive
     fits (cylinder/box/sphere), feature detection (holes, fillets)
  3. Part-family classification from the analysis
  4. Hand-off to the LLM planner with the analysis dict as a spec

The output is a parametric model — NOT just an STL re-import. The
LLM gets enough analytical info that it can emit a real feature
plan (sketch + extrude + holes) instead of forcing the user to
work with the raw mesh.

Public API:
    from aria_os.agents.scan_to_cad import scan_to_plan
    out = scan_to_plan(
        mesh_path="scan.stl",
        repo_root=Path("."))
"""
from __future__ import annotations

import math
from pathlib import Path


def _load_mesh(mesh_path: Path):
    """Load + repair the mesh. Returns trimesh.Trimesh."""
    try:
        import trimesh  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "scan_to_plan requires trimesh — pip install trimesh") from exc
    mesh = trimesh.load(str(mesh_path), force="mesh")
    if mesh is None or mesh.is_empty:
        raise ValueError(f"Mesh {mesh_path} is empty / unreadable")

    # Repair pass: merge verts, remove duplicate faces, fix normals,
    # close small holes (best-effort).
    mesh.merge_vertices()
    mesh.update_faces(mesh.unique_faces())
    mesh.remove_unreferenced_vertices()
    mesh.fix_normals()
    return mesh


def _analyze_mesh(mesh) -> dict:
    """Extract geometric features that point at a part family.

    Returns a dict that's a pre-spec for the LLM planner."""
    extents = mesh.bounding_box.extents
    bbox_mm = [float(e) for e in extents]
    bbox_mm.sort(reverse=True)   # [longest, mid, shortest]

    volume_mm3 = float(mesh.volume) if mesh.is_volume else None
    surface_area_mm2 = float(mesh.area)
    aspect = bbox_mm[0] / max(bbox_mm[2], 1e-6)
    # Box-like volume ratio: actual_volume / bounding_box_volume
    bbox_vol = bbox_mm[0] * bbox_mm[1] * bbox_mm[2]
    box_fill = (volume_mm3 / bbox_vol) if (volume_mm3 and bbox_vol)\
        else None

    # Cylindricity heuristic: PCA second + third axes equal-ish AND
    # aspect ≥ 1.5 → likely cylindrical (shaft, tube).
    cylindrical_score = 0.0
    if abs(bbox_mm[1] - bbox_mm[2]) / max(bbox_mm[1], 1e-6) < 0.05 \
            and aspect >= 1.5:
        cylindrical_score = 0.9
    elif aspect < 1.2:
        cylindrical_score = 0.0

    # Plate-like: shortest axis << other two
    plate_score = 0.0
    if bbox_mm[2] < bbox_mm[1] / 5 and bbox_mm[2] < bbox_mm[0] / 5:
        plate_score = 0.9

    # Symmetry hint: does the mesh look revolved? (centroid near
    # bbox center + cylindrical_score high)
    centroid = mesh.centroid
    bbox_center = mesh.bounding_box.centroid
    centroid_offset = float(((centroid[0] - bbox_center[0]) ** 2
                                + (centroid[1] - bbox_center[1]) ** 2
                                + (centroid[2] - bbox_center[2]) ** 2)
                              ** 0.5)
    revolved_likely = (centroid_offset < bbox_mm[2] * 0.05
                         and cylindrical_score > 0.7)

    # Hole detection — Euler characteristic of a closed mesh:
    # χ = V - E + F = 2 - 2*genus, so genus = (2 - χ) / 2.
    # Genus N → likely N through-holes (or handles).
    genus = None
    if mesh.is_watertight:
        try:
            chi = (len(mesh.vertices) - len(mesh.edges_unique)
                    + len(mesh.faces))
            genus = max(0, int((2 - chi) / 2))
        except Exception:
            pass

    # Suggested part family from features
    family = "other"
    if revolved_likely and aspect >= 1.5:
        family = "shaft" if aspect >= 3 else "pulley"
    elif plate_score > 0.7:
        family = "plate" if (genus or 0) == 0 else "bracket"
    elif cylindrical_score > 0.7 and aspect < 2.0:
        family = "flange"
    elif (genus or 0) >= 4:
        family = "bracket"  # multiple holes → mounting plate

    return {
        "bbox_mm":         bbox_mm,
        "volume_mm3":      volume_mm3,
        "surface_area_mm2": surface_area_mm2,
        "aspect_ratio":    aspect,
        "box_fill":        box_fill,
        "cylindrical_score": cylindrical_score,
        "plate_score":     plate_score,
        "revolved_likely": revolved_likely,
        "centroid_offset_mm": centroid_offset,
        "genus":           genus,
        "watertight":      bool(mesh.is_watertight),
        "n_vertices":      int(len(mesh.vertices)),
        "n_faces":         int(len(mesh.faces)),
        "suggested_family": family,
    }


def _analysis_to_goal(analysis: dict) -> str:
    """Compose a natural-language goal string from the analysis,
    suitable as the planner's input."""
    family = analysis["suggested_family"]
    bbox = analysis["bbox_mm"]
    longest, mid, shortest = bbox
    genus = analysis.get("genus") or 0

    if family == "shaft":
        return (f"shaft {longest:.0f}mm long, {mid:.0f}mm OD, "
                "AL 6061-T6")
    if family == "pulley":
        return (f"pulley/disc {longest:.0f}mm OD, {shortest:.0f}mm "
                "thick, AL 6061-T6")
    if family == "plate":
        return (f"flat plate {longest:.0f}x{mid:.0f}x{shortest:.0f}mm, "
                "AL 6061-T6")
    if family == "bracket":
        n_holes = max(2, genus)
        return (f"bracket plate {longest:.0f}x{mid:.0f}mm, "
                f"{shortest:.0f}mm thick, {n_holes} mounting holes, "
                "AL 6061-T6")
    if family == "flange":
        n_holes = max(4, genus - 1)  # subtract the center bore
        return (f"flange {longest:.0f}mm OD, {shortest:.0f}mm thick, "
                f"~{n_holes} bolt holes, AL 6061-T6")
    return (f"part {longest:.0f}x{mid:.0f}x{shortest:.0f}mm "
            f"reverse-engineered from scan, AL 6061-T6")


def scan_to_plan(
        mesh_path: str | Path,
        *,
        repo_root: Path | None = None,
        prefer_llm: bool = True,
        quality: str = "balanced",
        goal_override: str | None = None) -> dict:
    """Convert a 3D-scan mesh into a parametric ARIA plan.

    Args:
        mesh_path: STL/PLY/OBJ/3MF path
        goal_override: if provided, skip the automatic goal
                       composition and use this string verbatim.
                       Useful when the user knows what the part is
                       and only wants the analyzer to extract dims.
    """
    mesh_path = Path(mesh_path)
    if not mesh_path.is_file():
        raise FileNotFoundError(mesh_path)

    mesh = _load_mesh(mesh_path)
    analysis = _analyze_mesh(mesh)
    goal = goal_override or _analysis_to_goal(analysis)

    spec = {
        "scan_source":       str(mesh_path),
        "scan_analysis":     analysis,
        # Convert key dims so the planner's existing spec keys work
        "bbox_mm":           analysis["bbox_mm"],
        "thickness_mm":      analysis["bbox_mm"][2],
        "od_mm":             analysis["bbox_mm"][0]
                              if analysis["suggested_family"] in
                                ("flange", "shaft", "pulley") else None,
    }
    spec = {k: v for k, v in spec.items() if v is not None}

    from ..native_planner.dispatcher import make_plan
    plan = make_plan(goal, spec, prefer_llm=prefer_llm,
                       quality=quality, repo_root=repo_root)

    return {
        "goal":     goal,
        "spec":     spec,
        "analysis": analysis,
        "plan":     plan,
    }


__all__ = ["scan_to_plan", "_analyze_mesh", "_analysis_to_goal"]
