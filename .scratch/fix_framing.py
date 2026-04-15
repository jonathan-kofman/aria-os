"""Re-render parts that need tighter framing (object too small in frame)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")
SCR = Path("outputs/screenshots")


def render(stl, out, scale=1.8, angles=(np.pi/3, 0, np.pi/4)):
    mesh = trimesh.load(stl)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
    alum = np.array([175, 183, 198, 255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)
    extents = np.ptp(mesh.bounds, axis=0)
    distance = float(extents.max()) * scale
    scene = mesh.scene()
    scene.set_camera(angles=angles, distance=distance, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    out = Path(out)
    out.write_bytes(data)
    print(f"  {out.name}: {len(data)} bytes  (scale={scale})")
    return len(data)


STL = "outputs/cad/stl"

# Nozzle — render from front-side angle for better visibility, tighter framing
render(f"{STL}/nozzle_template_test.stl",
       str(SCR / "gl_iso_nozzle.png"),
       scale=1.6,
       angles=(np.pi/3, 0, np.pi/4))
render(f"{STL}/nozzle_template_test.stl",
       str(OUT / "gl_nozzle.png"),
       scale=1.6,
       angles=(np.pi/3, 0, np.pi/4))

# Sloper — tighter framing
render(f"{STL}/llm_asymmetric_freeform_climbing_sloper_hold.stl",
       str(OUT / "gl_sloper.png"),
       scale=1.4)

# Housing — tighter framing
render(f"{STL}/aria_housing.stl",
       str(OUT / "gl_housing.png"),
       scale=1.6)

# Assembly — tighter framing
render(f"{STL}/turbopump_v7.stl",
       str(OUT / "assembly_v7.png"),
       scale=1.8)

print("Done.")
