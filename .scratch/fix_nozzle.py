"""Fix nozzle renders - try different material shades to find best contrast."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import trimesh
from pathlib import Path

STL = "outputs/cad/stl/llm_nozzle_bell_small_rocket_engine.stl"

def render_iso(stl_path, out_path, color, angles=(np.pi/3, 0, np.pi/4)):
    mesh = trimesh.load(stl_path)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
    c = np.array(list(color) + [255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=c)
    distance = mesh.scale * 2.5
    scene = mesh.scene()
    scene.set_camera(angles=angles, distance=distance, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    if data and len(data) > 1000:
        Path(out_path).write_bytes(data)
        print(f"  OK  {out_path} ({len(data)} bytes, color={color})")
    else:
        print(f"  FAIL {out_path} ({len(data) if data else 0} bytes)")

OUT = Path("outputs/gallery_renders")

# Test a range of grays to find best contrast
render_iso(STL, str(OUT / "nozzle_test_dark.png"),    (120, 128, 145))  # darker steel
render_iso(STL, str(OUT / "nozzle_test_medium.png"),  (150, 157, 172))  # medium aluminum
render_iso(STL, str(OUT / "nozzle_test_lighter.png"), (165, 172, 188))  # lighter
