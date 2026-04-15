"""Render terrain STL for gallery."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")
STL = "outputs/terrain/mountain_terrain_3km_x_3km_with_150m_pea_mesh.stl"

mesh = trimesh.load(STL)
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

print(f"Faces: {len(mesh.faces)}, scale: {mesh.scale:.1f}")
print(f"Bounds: {mesh.bounds}")

# Terrain color: warm green-gray (mountainous terrain)
terrain_color = np.array([110, 130, 100, 255], dtype=np.uint8)  # olive green
mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=terrain_color)

distance = mesh.scale * 1.8  # closer for terrain
center = mesh.centroid

# Render from above-front (classic terrain view angle)
angles = (np.pi / 3.5, 0, np.pi / 6)  # slightly more top-down, slight yaw

scene = mesh.scene()
scene.set_camera(angles=angles, distance=distance, center=center)
data = scene.save_image(resolution=(800, 600), visible=True)
out = OUT / "terrain.png"
out.write_bytes(data)
print(f"terrain.png: {len(data)} bytes")
