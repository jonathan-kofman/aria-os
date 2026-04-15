"""Try rendering different nozzle STLs."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")
STL_DIR = Path("outputs/cad/stl")

nozzles = [
    "nozzle_bell_v2.stl",
    "nozzle_template_test.stl",
    "nozzle_v4.stl",
    "nozzle_v6.stl",
]

for fname in nozzles:
    stl = STL_DIR / fname
    mesh = trimesh.load(str(stl))
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
    alum = np.array([175, 183, 198, 255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)
    distance = mesh.scale * 2.5
    scene = mesh.scene()
    scene.set_camera(angles=(np.pi/3, 0, np.pi/4), distance=distance, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    out = OUT / f"nozzle_test_{fname}"
    out.write_bytes(data)
    print(f"  {fname}: {len(data)} bytes, scale={mesh.scale:.1f}")
