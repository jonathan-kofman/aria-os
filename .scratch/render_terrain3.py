"""Find right camera distance for terrain overview."""
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
print(f"scale={mesh.scale:.1f}, centroid={mesh.centroid}")

mesh.visual = trimesh.visual.ColorVisuals(
    mesh=mesh, face_colors=np.array([100, 130, 90, 255], dtype=np.uint8)
)

# Try a range of distances - terrain bounds are 0-3000 in XY, 0-150 in Z
# So distance ~2000-4000 should give a good overhead view
for dist in [1000, 1500, 2000, 2500, 3000]:
    scene = mesh.scene()
    scene.set_camera(angles=(np.pi/3.5, 0, np.pi/6), distance=dist, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    out = OUT / f"terrain_d{dist}.png"
    out.write_bytes(data)
    print(f"  d={dist}: {len(data)} bytes")
