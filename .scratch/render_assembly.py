"""Render turbopump assembly with aluminum material."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")

def render(stl, out, angles=(np.pi/3, 0, np.pi/4)):
    mesh = trimesh.load(stl)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
    alum = np.array([175, 183, 198, 255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)
    print(f"  {Path(stl).stem}: faces={len(mesh.faces)}, scale={mesh.scale:.1f}")
    distance = mesh.scale * 2.5
    scene = mesh.scene()
    scene.set_camera(angles=angles, distance=distance, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    Path(out).write_bytes(data)
    print(f"    -> {out}: {len(data)} bytes")
    return len(data)

# Try v7 (most complex) and v5 (good feature count)
for stl, name in [
    ("outputs/cad/stl/turbopump_v7.stl", "assembly_v7"),
    ("outputs/cad/stl/turbopump_v5.stl", "assembly_v5"),
    ("outputs/cad/stl/turbopump_manual.stl", "assembly_manual"),
]:
    try:
        render(stl, f"{OUT}/{name}.png")
    except Exception as e:
        print(f"  FAIL {stl}: {e}")
