"""Try different camera angles for the nozzle."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")
STL = "outputs/cad/stl/llm_nozzle_bell_small_rocket_engine.stl"

mesh = trimesh.load(STL)
print(f"Bounds:\n{mesh.bounds}")
print(f"Scale: {mesh.scale:.1f}, Centroid: {mesh.centroid}")

# The nozzle axis is along Y (120mm extent), bell diameter in XZ (~500mm)
# From standard iso (pi/3, 0, pi/4) looking from front-right-top:
# The nozzle looks like a flat disc from far away — too flat.
# Try viewing from the SIDE to see the bell profile

tests = [
    ("front",   (np.pi/2, 0, 0)),            # side-on to nozzle axis
    ("side",    (np.pi/2, 0, np.pi/2)),       # other side
    ("iso_std", (np.pi/3, 0, np.pi/4)),       # standard iso
    ("iso_low", (np.pi/4, 0, np.pi/4)),       # lower iso
    ("iso_low2",(np.pi/6, 0, np.pi/4)),       # even lower
    ("top",     (np.pi, 0, 0)),               # top-down
]

distance = mesh.scale * 2.5
for name, angles in tests:
    mesh2 = trimesh.load(STL)
    scene = mesh2.scene()
    scene.set_camera(angles=angles, distance=distance, center=mesh2.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    fpath = OUT / f"nozzle_angle_{name}.png"
    fpath.write_bytes(data)
    print(f"  {name}: {len(data)} bytes")
