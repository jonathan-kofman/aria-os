"""Render nozzle gallery images with the good nozzle STL."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

NOZZLE = "outputs/cad/stl/nozzle_template_test.stl"

def render(stl, out, angles=(np.pi/3, 0, np.pi/4)):
    mesh = trimesh.load(stl)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
    alum = np.array([175, 183, 198, 255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)
    distance = mesh.scale * 2.5
    scene = mesh.scene()
    scene.set_camera(angles=angles, distance=distance, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    Path(out).write_bytes(data)
    print(f"  {out}: {len(data)} bytes")

render(NOZZLE, "outputs/screenshots/gl_iso_nozzle.png")
render(NOZZLE, "outputs/gallery_renders/gl_nozzle.png")
print("Done.")
