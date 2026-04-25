"""
aria_os/generators/gltf_export.py

glTF 2.0 binary (.glb) exporter for ARIA-OS parts.

Reads an STL produced by the CadQuery / SDF / etc. pipeline, optionally tints
the mesh based on a StructSight risk JSON, and writes a .glb suitable for the
structsight-vr WebXR viewer.

Risk-flag tinting (vertex colors):
    red    — any high-risk flag present (corrosion, fatigue, permit,
             fracture, thermal, overload, creep)
    amber  — verification_required is non-empty
    green  — clean

The viewer also does its own tint (modulates material color), but bake-in
vertex colors mean the .glb is informative on its own — useful for
QA preview, AR drops, or any non-VR consumer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional, Union

# High-risk substrings — must match structsight_overlay.js
_HIGH_RISK_KEYWORDS = (
    "corrosion", "fatigue", "permit", "fracture",
    "thermal", "overload", "creep",
)

# RGB tints (0..1 floats, will be applied as vertex colors)
_TINT_RED   = (1.00, 0.25, 0.19)   # 0xff4030 — high risk
_TINT_AMBER = (1.00, 0.63, 0.19)   # 0xffa030 — verification required
_TINT_GREEN = (0.25, 0.75, 0.38)   # 0x40c060 — clean
_TINT_NEUTRAL = (0.78, 0.82, 0.86)  # cool grey when no JSON


def _classify_risk(structsight: dict[str, Any]) -> tuple[float, float, float]:
    """Return the RGB tint that the VR viewer would apply."""
    flags = [str(f).lower() for f in structsight.get("risk_flags") or []]
    has_high = any(any(k in f for k in _HIGH_RISK_KEYWORDS) for f in flags)
    needs_verify = bool(structsight.get("verification_required") or [])
    if has_high:
        return _TINT_RED
    if needs_verify:
        return _TINT_AMBER
    return _TINT_GREEN


def export_to_gltf(
    stl_path: Union[str, Path],
    structsight_json: Optional[Union[str, Path, dict]] = None,
    out_path: Optional[Union[str, Path]] = None,
) -> dict[str, Any]:
    """Export an STL mesh to glTF 2.0 binary (.glb).

    Parameters
    ----------
    stl_path
        Path to a triangulated STL file produced by ARIA-OS.
    structsight_json
        Optional path to a StructSightResult JSON (or a pre-loaded dict).
        When provided, the mesh receives baked-in vertex colors based on
        risk_flags / verification_required. Pass None for a neutral export.
    out_path
        Where to write the .glb. When omitted, defaults to
        ``<stl_dir>/part.glb`` — when STL lives under ``outputs/runs/<id>/``
        the .glb will sit next to it, which is exactly what the VR viewer
        contract in docs/INTEGRATION.md expects.

    Returns
    -------
    dict
        ``{"glb_path": str, "vertex_count": int, "face_count": int,
            "tint": str | None}``

    Raises
    ------
    FileNotFoundError
        If the STL file does not exist.
    ValueError
        If the STL produces an empty / degenerate mesh.
    """
    import numpy as np
    import trimesh

    stl_path = Path(stl_path)
    if not stl_path.is_file():
        raise FileNotFoundError(f"STL not found: {stl_path}")

    mesh = trimesh.load_mesh(str(stl_path), process=False)
    # An STL can technically return a Scene with multiple geometries — fold
    # them into a single mesh for export so the viewer only deals with one
    # node.
    if isinstance(mesh, trimesh.Scene):
        geoms = list(mesh.geometry.values())
        if not geoms:
            raise ValueError(f"STL produced empty scene: {stl_path}")
        mesh = trimesh.util.concatenate(geoms)

    if mesh.vertices is None or len(mesh.vertices) == 0:
        raise ValueError(f"STL produced zero-vertex mesh: {stl_path}")
    if mesh.faces is None or len(mesh.faces) == 0:
        raise ValueError(f"STL produced zero-face mesh: {stl_path}")

    # ---- Apply vertex colors from StructSight, if provided ---------------
    tint_label: Optional[str] = None
    if structsight_json is not None:
        if isinstance(structsight_json, dict):
            data = structsight_json
        else:
            ssj_path = Path(structsight_json)
            if not ssj_path.is_file():
                raise FileNotFoundError(
                    f"StructSight JSON not found: {ssj_path}"
                )
            data = json.loads(ssj_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("StructSight JSON must decode to an object")
        rgb = _classify_risk(data)
        tint_label = (
            "red"   if rgb == _TINT_RED   else
            "amber" if rgb == _TINT_AMBER else
            "green"
        )
    else:
        rgb = _TINT_NEUTRAL

    # trimesh wants RGBA uint8 (0..255). Vertex colors broadcast across faces
    # in glTF as long as the mesh has visual.vertex_colors set.
    rgba = np.tile(
        np.array(
            [int(rgb[0] * 255), int(rgb[1] * 255), int(rgb[2] * 255), 255],
            dtype=np.uint8,
        ),
        (len(mesh.vertices), 1),
    )
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, vertex_colors=rgba)

    # ---- Resolve out_path ------------------------------------------------
    if out_path is None:
        out_path = stl_path.with_name("part.glb")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # trimesh.Scene.export(file_type='glb') returns a glTF 2.0 binary blob.
    scene = trimesh.Scene(mesh)
    glb_bytes = scene.export(file_type="glb")
    out_path.write_bytes(glb_bytes)

    return {
        "glb_path": str(out_path),
        "vertex_count": int(len(mesh.vertices)),
        "face_count": int(len(mesh.faces)),
        "tint": tint_label,
    }
