"""
aria_os.visual_qa.stl_renderer — STL rendering wrapper.

Thin adapter around the existing ``aria_os.visual_verifier._render_views``
so the visual_qa package exposes a single consistent API without
re-implementing OpenGL/matplotlib view rendering.

Part of the reusable ``aria_os.visual_qa`` visual verification
framework. Never raises — on failure returns a dict with ``ok=False``
and an ``error`` key.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def render_stl(
    stl_path: str | Path,
    out_dir: str | Path,
    goal: str = "stl preview",
) -> dict[str, Any]:
    """Render an STL to one or more preview PNGs + return metadata.

    Imports ``_render_views`` from ``aria_os.visual_verifier`` lazily so
    this module stays cheap to import even without trimesh present.

    Returns:
        On success:
            {
              "ok": True,
              "png_paths": [str, ...],
              "view_labels": [str, ...],
              "bbox": {"xmin","ymin","zmin","xmax","ymax","zmax","dx","dy","dz"},
              "triangle_count": int,
            }
        On failure:
            {"ok": False, "error": "<message>", "png_paths": []}
    """
    stl_path = Path(stl_path)
    out_dir = Path(out_dir)

    if not stl_path.is_file():
        return {"ok": False, "error": f"stl not found: {stl_path}", "png_paths": []}

    try:
        from aria_os.visual_verifier import _render_views  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"visual_verifier import failed: {exc}", "png_paths": []}

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths, labels = _render_views(str(stl_path), goal, out_dir)
    except Exception as exc:
        return {"ok": False, "error": f"_render_views failed: {exc}", "png_paths": []}

    bbox = None
    tri_count = 0
    try:
        import trimesh  # type: ignore
        mesh = trimesh.load(str(stl_path))
        if hasattr(mesh, "geometry"):
            mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
        b = mesh.bounds  # (2, 3)
        bbox = {
            "xmin": float(b[0][0]), "ymin": float(b[0][1]), "zmin": float(b[0][2]),
            "xmax": float(b[1][0]), "ymax": float(b[1][1]), "zmax": float(b[1][2]),
            "dx": float(b[1][0] - b[0][0]),
            "dy": float(b[1][1] - b[0][1]),
            "dz": float(b[1][2] - b[0][2]),
        }
        tri_count = int(len(mesh.faces))
    except Exception:
        # bbox/triangle info is best-effort — don't fail the render.
        pass

    return {
        "ok": True,
        "png_paths": [str(p) for p in paths],
        "view_labels": list(labels),
        "bbox": bbox,
        "triangle_count": tri_count,
    }
