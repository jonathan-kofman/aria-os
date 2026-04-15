"""Render remaining gallery parts that crashed in the first run."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")


def render_iso(stl_path: str, out_path: str, angles=(np.pi/3, 0, np.pi/4), tries=3):
    mesh = trimesh.load(stl_path)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
    alum = np.array([175, 183, 198, 255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)
    distance = mesh.scale * 2.5
    center = mesh.centroid

    for attempt in range(tries):
        try:
            scene = mesh.scene()
            scene.set_camera(angles=angles, distance=distance, center=center)
            data = scene.save_image(resolution=(800, 600), visible=True)
            if data and len(data) > 1000:
                Path(out_path).write_bytes(data)
                print(f"  OK  {out_path}")
                return True
        except Exception as e:
            print(f"  attempt {attempt+1} failed: {e}")
            time.sleep(0.5)
    print(f"  FAIL {out_path}")
    return False


STL = "outputs/cad/stl"

# Parts that still need rendering
remaining = [
    (f"{STL}/llm_steel_pipe_flange_od_bore.stl",   f"{OUT}/gl_flange.png"),
    (f"{STL}/aria_housing.stl",                      f"{OUT}/gl_housing.png"),
    (f"{STL}/lattice_test_octet.stl",               f"{OUT}/gl_octet.png"),
]

print("=== Rendering remaining gallery parts ===")
for stl, out in remaining:
    if not Path(stl).exists():
        print(f"  SKIP {stl}")
        continue
    print(f"Rendering {Path(stl).stem}...")
    render_iso(stl, out)

print("Done.")
