"""
aria_os/rhino_export.py — Export Rhino Compute geometry to STEP/STL

Two export paths:
1. STL: brep → rhino3dm triangulation → binary STL (fast, no license needed)
2. STEP: brep → 3DM → Rhino CLI export (needs Rhino GUI, slow)
   OR: mesh → trimesh → STL (always works headless)

Usage:
    from aria_os.rhino_export import brep_to_stl, brep_to_3dm
"""
from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Optional


def brep_to_3dm(
    brep,
    dm_path: str | Path,
    meshes: list | None = None,
) -> str:
    """Save a rhino3dm Brep (and optional meshes) to a .3dm file."""
    import rhino3dm

    dm = Path(dm_path)
    dm.parent.mkdir(parents=True, exist_ok=True)

    model = rhino3dm.File3dm()
    model.Objects.AddBrep(brep)
    if meshes:
        for m in meshes:
            model.Objects.AddMesh(m)
    model.Write(str(dm), 8)
    return str(dm)


def brep_to_stl(
    brep,
    stl_path: str | Path,
    *,
    compute_url: str = "http://localhost:8081/",
) -> str:
    """
    Convert a rhino3dm Brep to STL.

    Strategy: extract mesh from brep faces via rhino3dm's built-in
    tessellation, or fall back to Compute's sphere-approximation.
    """
    import rhino3dm

    sp = Path(stl_path)
    sp.parent.mkdir(parents=True, exist_ok=True)

    # rhino3dm Brep has Faces, each Face can give a Mesh
    triangles: list[tuple] = []
    for fi in range(len(brep.Faces)):
        face = brep.Faces[fi]
        mesh = face.GetMesh(rhino3dm.MeshType.Default)
        if mesh is None:
            mesh = face.GetMesh(rhino3dm.MeshType.Any)
        if mesh is None:
            continue
        _extract_triangles(mesh, triangles)

    if not triangles:
        # Fallback: try getting render mesh from the brep directly
        mesh = brep.GetMesh(rhino3dm.MeshType.Default) if hasattr(brep, 'GetMesh') else None
        if mesh:
            _extract_triangles(mesh, triangles)

    if not triangles:
        raise RuntimeError("Could not extract mesh from brep — no face meshes available")

    _write_binary_stl(triangles, sp)
    return str(sp)


def mesh_to_stl(mesh, stl_path: str | Path) -> str:
    """Write a rhino3dm Mesh directly to binary STL."""
    sp = Path(stl_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    triangles: list[tuple] = []
    _extract_triangles(mesh, triangles)
    _write_binary_stl(triangles, sp)
    return str(sp)


def _extract_triangles(mesh, out: list):
    """Extract triangles from a rhino3dm Mesh into (v0, v1, v2) tuples."""
    verts = mesh.Vertices
    faces = mesh.Faces
    for i in range(len(faces)):
        f = faces[i]
        v0 = verts[f[0]]
        v1 = verts[f[1]]
        v2 = verts[f[2]]
        out.append(((v0.X, v0.Y, v0.Z), (v1.X, v1.Y, v1.Z), (v2.X, v2.Y, v2.Z)))
        if f[3] != f[2]:  # quad → second triangle
            v3 = verts[f[3]]
            out.append(((v0.X, v0.Y, v0.Z), (v2.X, v2.Y, v2.Z), (v3.X, v3.Y, v3.Z)))


def _write_binary_stl(triangles: list[tuple], path: Path):
    """Write triangles to binary STL format."""
    with open(path, 'wb') as f:
        f.write(b'\0' * 80)  # header
        f.write(struct.pack('<I', len(triangles)))
        for v0, v1, v2 in triangles:
            f.write(struct.pack('<fff', 0, 0, 0))  # normal placeholder
            f.write(struct.pack('<fff', *v0))
            f.write(struct.pack('<fff', *v1))
            f.write(struct.pack('<fff', *v2))
            f.write(struct.pack('<H', 0))
