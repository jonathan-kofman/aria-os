"""MeshInterpretAgent — load, clean, and analyze 3D scan meshes."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from .. import event_bus
from ..models.scan_models import BoundingBox, CleanedMesh


# Supported scan formats
_SUPPORTED_EXTENSIONS = {".stl", ".obj", ".ply"}

# Decimation threshold — meshes above this get simplified
_MAX_FACES = 100_000
_TARGET_FACES = 50_000

# Minimum face area to keep (mm^2) — smaller faces are degenerate
_MIN_FACE_AREA = 1e-10


class MeshInterpretAgent:
    """
    Load a raw scan file (STL/OBJ/PLY), clean it, and return
    a CleanedMesh with metadata for downstream feature extraction.
    """

    def __init__(self, output_dir: Optional[str | Path] = None):
        self.output_dir = Path(output_dir) if output_dir else None

    def run(self, scan_path: str | Path) -> CleanedMesh:
        import trimesh

        scan_path = Path(scan_path)
        if scan_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported format '{scan_path.suffix}'. "
                f"Supported: {', '.join(_SUPPORTED_EXTENSIONS)}"
            )
        if not scan_path.exists():
            raise FileNotFoundError(f"Scan file not found: {scan_path}")

        # Load
        event_bus.emit("scan", f"[MeshInterpret] Loading {scan_path.name}")
        mesh = trimesh.load(str(scan_path), force="mesh")
        event_bus.emit("scan", f"[MeshInterpret] Loaded: {len(mesh.vertices)} verts, {len(mesh.faces)} faces")

        # Clean
        mesh = self._clean(mesh)

        # Compute metadata
        bb = mesh.bounding_box.extents
        bbox = BoundingBox(x=round(float(bb[0]), 2), y=round(float(bb[1]), 2), z=round(float(bb[2]), 2))

        volume = float(mesh.volume) if mesh.is_watertight else 0.0
        sa = float(mesh.area)
        com = tuple(float(c) for c in mesh.center_mass) if mesh.is_watertight else (0.0, 0.0, 0.0)

        # Save cleaned mesh
        out_path = ""
        if self.output_dir:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(self.output_dir / "cleaned.stl")
            mesh.export(out_path)

        result = CleanedMesh(
            vertices=len(mesh.vertices),
            faces=len(mesh.faces),
            bounding_box=bbox,
            volume_mm3=round(volume, 2),
            surface_area_mm2=round(sa, 2),
            watertight=mesh.is_watertight,
            file_path=out_path or str(scan_path),
            center_of_mass=com,
        )

        event_bus.emit("scan", f"[MeshInterpret] Clean: {result.faces} faces, "
                       f"bbox {bbox.x}x{bbox.y}x{bbox.z}mm, "
                       f"{'watertight' if result.watertight else 'open mesh'}",
                       {"bbox": bbox.as_dict(), "watertight": result.watertight})

        return result

    def _clean(self, mesh) -> "trimesh.Trimesh":
        """Run cleanup pipeline: dedup, degenerate removal, hole fill, normals, decimate."""
        import trimesh

        event_bus.emit("scan", "[MeshInterpret] Cleaning mesh...")

        # 1. Merge duplicate vertices
        mesh.merge_vertices()

        # 2. Remove degenerate faces (near-zero area)
        if len(mesh.faces) > 0:
            areas = mesh.area_faces
            valid = areas > _MIN_FACE_AREA
            if not valid.all():
                n_removed = int((~valid).sum())
                mesh.update_faces(valid)
                mesh.remove_unreferenced_vertices()
                event_bus.emit("scan", f"[MeshInterpret] Removed {n_removed} degenerate faces")

        # 3. Fill small holes
        trimesh.repair.fill_holes(mesh)

        # 4. Fix normals
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fix_inversion(mesh)

        # 5. Decimate if too many faces
        if len(mesh.faces) > _MAX_FACES:
            event_bus.emit("scan", f"[MeshInterpret] Decimating {len(mesh.faces)} → {_TARGET_FACES} faces")
            mesh = mesh.simplify_quadric_decimation(_TARGET_FACES)

        return mesh
