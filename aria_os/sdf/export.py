"""
Additional mesh exporters for the SDF pipeline — OBJ, 3MF, PLY.
STL is already handled by sdf_generator.SDFScene.export_stl.

These formats cover:
  OBJ — universal for rendering / visualization / game engines / Blender
  3MF — modern printer-native format, includes metadata (materials, colors,
        units), accepted by PrusaSlicer, Bambu Studio, Cura
  PLY — dense point-cloud / scan-compatible; accepted by MeshLab, CloudCompare
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import numpy as np


def export_obj(mesh_data: tuple, path: str | Path, *,
               object_name: str = "aria_part") -> str:
    """Write a Wavefront .obj. No mtl file (material-less)."""
    verts, faces, normals = mesh_data
    verts = np.asarray(verts)
    faces = np.asarray(faces)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(f"# aria-os OBJ export\n")
        f.write(f"o {object_name}\n")
        for v in verts:
            f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
        if normals is not None and len(normals):
            for n in normals:
                f.write(f"vn {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}\n")
        for face in faces:
            # OBJ is 1-indexed
            a, b, c = face[0] + 1, face[1] + 1, face[2] + 1
            if normals is not None and len(normals):
                f.write(f"f {a}//{a} {b}//{b} {c}//{c}\n")
            else:
                f.write(f"f {a} {b} {c}\n")
    return str(p)


def export_ply(mesh_data: tuple, path: str | Path, *,
               binary: bool = True) -> str:
    """Write a PLY file (ASCII or binary). Useful for scan/point-cloud
    interoperability."""
    verts, faces, normals = mesh_data
    verts = np.asarray(verts, dtype=np.float32)
    faces = np.asarray(faces, dtype=np.int32)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if binary:
        with open(p, "wb") as f:
            header = (
                f"ply\n"
                f"format binary_little_endian 1.0\n"
                f"element vertex {len(verts)}\n"
                f"property float x\n"
                f"property float y\n"
                f"property float z\n"
                f"element face {len(faces)}\n"
                f"property list uchar int vertex_indices\n"
                f"end_header\n"
            )
            f.write(header.encode("ascii"))
            for v in verts:
                f.write(struct.pack("<fff", float(v[0]), float(v[1]), float(v[2])))
            for face in faces:
                f.write(struct.pack("<B", 3))
                f.write(struct.pack("<iii", int(face[0]), int(face[1]), int(face[2])))
    else:
        with open(p, "w", encoding="utf-8") as f:
            f.write("ply\nformat ascii 1.0\n")
            f.write(f"element vertex {len(verts)}\n")
            f.write("property float x\nproperty float y\nproperty float z\n")
            f.write(f"element face {len(faces)}\n")
            f.write("property list uchar int vertex_indices\n")
            f.write("end_header\n")
            for v in verts:
                f.write(f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
            for face in faces:
                f.write(f"3 {face[0]} {face[1]} {face[2]}\n")
    return str(p)


def export_3mf(mesh_data: tuple, path: str | Path, *,
               object_name: str = "aria_part",
               units: str = "millimeter") -> str:
    """Write a 3MF container — zipped XML package. Slicers prefer this
    over STL because it preserves units, material metadata, and colors.
    """
    verts, faces, _ = mesh_data
    verts = np.asarray(verts)
    faces = np.asarray(faces)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    # Build the main model XML
    vertex_lines = []
    for v in verts:
        vertex_lines.append(
            f'<vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>')
    triangle_lines = []
    for face in faces:
        triangle_lines.append(
            f'<triangle v1="{int(face[0])}" v2="{int(face[1])}" v3="{int(face[2])}"/>')
    model_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<model unit="{units}" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
        '  <resources>\n'
        f'    <object id="1" name="{object_name}" type="model">\n'
        '      <mesh>\n'
        '        <vertices>\n'
        '          ' + "\n          ".join(vertex_lines) + '\n'
        '        </vertices>\n'
        '        <triangles>\n'
        '          ' + "\n          ".join(triangle_lines) + '\n'
        '        </triangles>\n'
        '      </mesh>\n'
        '    </object>\n'
        '  </resources>\n'
        '  <build>\n'
        '    <item objectid="1"/>\n'
        '  </build>\n'
        '</model>\n'
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        '</Types>\n'
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Target="/3D/3dmodel.model" Id="rel0" '
        'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        '</Relationships>\n'
    )
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml)
        z.writestr("_rels/.rels", rels_xml)
        z.writestr("3D/3dmodel.model", model_xml)
    return str(p)
