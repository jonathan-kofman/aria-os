"""Render terrain - try smaller terrain and different camera setup."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")

terrains = [
    "outputs/terrain/rolling_hills_1km_x_1km_50m_peak_mesh.stl",
    "outputs/terrain/mountain_terrain_3km_x_3km_with_150m_pea_mesh.stl",
]

for stl_path in terrains:
    mesh = trimesh.load(stl_path)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

    name = Path(stl_path).stem[:20]
    print(f"\n{name}: faces={len(mesh.faces)}, scale={mesh.scale:.1f}")
    print(f"  Bounds: {mesh.bounds}")

    # Terrain color
    mesh.visual = trimesh.visual.ColorVisuals(
        mesh=mesh, face_colors=np.array([100, 130, 90, 255], dtype=np.uint8)
    )

    # Try multiple camera setups
    tests = [
        ("2.5x", mesh.scale * 2.5),
        ("5x",   mesh.scale * 5.0),
        ("1x",   mesh.scale * 1.0),
        ("500",  500.0),
    ]
    for dname, distance in tests:
        scene = mesh.scene()
        scene.set_camera(angles=(np.pi/4, 0, np.pi/8), distance=distance, center=mesh.centroid)
        data = scene.save_image(resolution=(800, 600), visible=True)
        out = OUT / f"terrain_test_{name}_{dname}.png"
        out.write_bytes(data)
        print(f"  d={dname}: {len(data)} bytes")
