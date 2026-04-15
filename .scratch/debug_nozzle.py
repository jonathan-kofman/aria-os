"""Debug nozzle rendering."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")
STL = "outputs/cad/stl/llm_nozzle_bell_small_rocket_engine.stl"

mesh = trimesh.load(STL)
print(f"Faces: {len(mesh.faces)}, scale: {mesh.scale:.1f}, centroid: {mesh.centroid}")
print(f"Bounds: {mesh.bounds}")

# Test 1: default colors (dark gray)
mesh2 = trimesh.load(STL)
scene = mesh2.scene()
distance = mesh2.scale * 2.5
scene.set_camera(angles=(np.pi/3, 0, np.pi/4), distance=distance, center=mesh2.centroid)
data = scene.save_image(resolution=(800, 600), visible=True)
print(f"Default (no color): {len(data)} bytes")
(OUT / "nozzle_default.png").write_bytes(data)

# Test 2: face_colors on existing visual
mesh3 = trimesh.load(STL)
mesh3.visual.face_colors = [130, 138, 155, 255]
scene = mesh3.scene()
scene.set_camera(angles=(np.pi/3, 0, np.pi/4), distance=distance, center=mesh3.centroid)
data = scene.save_image(resolution=(800, 600), visible=True)
print(f"face_colors=[130,138,155]: {len(data)} bytes")
(OUT / "nozzle_set_direct.png").write_bytes(data)

# Test 3: replace visual entirely
mesh4 = trimesh.load(STL)
alum = np.tile([130, 138, 155, 255], (len(mesh4.faces), 1)).astype(np.uint8)
mesh4.visual = trimesh.visual.ColorVisuals(mesh=mesh4, face_colors=alum)
scene = mesh4.scene()
scene.set_camera(angles=(np.pi/3, 0, np.pi/4), distance=distance, center=mesh4.centroid)
data = scene.save_image(resolution=(800, 600), visible=True)
print(f"Replace visual (per-face array): {len(data)} bytes")
(OUT / "nozzle_replace_visual.png").write_bytes(data)

# Test 4: try vertex_colors instead
mesh5 = trimesh.load(STL)
vc = np.tile([130, 138, 155, 255], (len(mesh5.vertices), 1)).astype(np.uint8)
mesh5.visual = trimesh.visual.ColorVisuals(mesh=mesh5, vertex_colors=vc)
scene = mesh5.scene()
scene.set_camera(angles=(np.pi/3, 0, np.pi/4), distance=distance, center=mesh5.centroid)
data = scene.save_image(resolution=(800, 600), visible=True)
print(f"vertex_colors: {len(data)} bytes")
(OUT / "nozzle_vertex_colors.png").write_bytes(data)
