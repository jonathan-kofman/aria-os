"""Fix remaining gallery issues: terrain framing, flange/bracket framing, assembly floating piece."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")
ALUM = np.array([175, 183, 198, 255], dtype=np.uint8)


def load(stl):
    m = trimesh.load(stl)
    if hasattr(m, "geometry"):
        m = trimesh.util.concatenate(list(m.geometry.values()))
    return m


def render(mesh, out, scale=1.5, angles=(np.pi/3, 0, np.pi/4), distance=None):
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=ALUM)
    extents = np.ptp(mesh.bounds, axis=0)
    dist = distance if distance is not None else float(extents.max()) * scale
    scene = mesh.scene()
    scene.set_camera(angles=angles, distance=dist, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    Path(out).write_bytes(data)
    print(f"  {Path(out).name}: {len(data):,} bytes (d={dist:.0f})")
    return len(data)


STL = "outputs/cad/stl"

# --- Assembly: keep only the largest connected component, drop floating stub ---
print("Assembly...")
mesh = load(f"{STL}/turbopump_v7.stl")
parts = mesh.split(only_watertight=False)
main = max(parts, key=lambda m: len(m.faces))
print(f"  {len(parts)} parts -> keeping {len(main.faces)} faces (dropped {sum(len(p.faces) for p in parts) - len(main.faces)})")
render(main, OUT / "assembly_v7.png", scale=1.6)

# --- Flange: tighter framing ---
print("Flange...")
render(load(f"{STL}/llm_steel_pipe_flange_od_bore.stl"),
       OUT / "gl_flange.png",
       scale=0.75)

# --- L-bracket: tighter framing ---
print("L-bracket...")
render(load(f"{STL}/llm_simple_l_bracket_bolt_holes.stl"),
       OUT / "gl_l_bracket.png",
       scale=0.75)

# --- Terrain: overhead view at increasing distances ---
print("Terrain...")
stl = "outputs/terrain/mountain_terrain_3km_x_3km_with_150m_pea_mesh.stl"
mesh = load(stl)
GREEN = np.array([100, 130, 90, 255], dtype=np.uint8)
mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=GREEN)
# Very overhead angle (75° elevation) at multiple distances to find best view
for dist in [2000, 2500, 3000, 4000]:
    scene = mesh.scene()
    scene.set_camera(angles=(np.pi * 0.42, 0, np.pi / 4),
                     distance=dist, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    out_path = OUT / f"terrain_fix_d{dist}.png"
    out_path.write_bytes(data)
    print(f"  terrain_fix_d{dist}.png: {len(data):,} bytes")

print("Done.")
