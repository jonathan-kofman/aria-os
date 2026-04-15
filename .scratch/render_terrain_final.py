"""Render final terrain for gallery."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")

# Mountain terrain at d=1000 worked well
STL = "outputs/terrain/mountain_terrain_3km_x_3km_with_150m_pea_mesh.stl"
mesh = trimesh.load(STL)
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

# Try angles to get a nice overhead perspective
angles_list = [
    ("a1", (np.pi/3,   0, np.pi/6)),   # more overhead
    ("a2", (np.pi/2.5, 0, np.pi/8)),   # less overhead
    ("a3", (np.pi/3,   0, np.pi/4)),   # 45deg yaw
]

mesh.visual = trimesh.visual.ColorVisuals(
    mesh=mesh, face_colors=np.array([100, 130, 90, 255], dtype=np.uint8)
)

for name, angles in angles_list:
    scene = mesh.scene()
    scene.set_camera(angles=angles, distance=1000, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    out = OUT / f"terrain_{name}.png"
    out.write_bytes(data)
    print(f"  {name}: {len(data)} bytes")
