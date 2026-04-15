"""
Regenerate all gallery GL renders with the new aluminum material color.
Run from the repo root: python .scratch/regen_gallery_renders.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import numpy as np
import trimesh
from pathlib import Path

OUT = Path("outputs/gallery_renders")
SCR = Path("outputs/screenshots")
OUT.mkdir(exist_ok=True)
SCR.mkdir(exist_ok=True)


def render_iso(stl_path: str, out_path: str, distance_scale: float = 2.5):
    """Render a single isometric GL view and save to out_path."""
    mesh = trimesh.load(stl_path)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

    # Aluminum color
    alum = np.array([175, 183, 198, 255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)

    scene = mesh.scene()
    distance = mesh.scale * distance_scale
    scene.set_camera(
        angles=(np.pi / 3, 0, np.pi / 4),
        distance=distance,
        center=mesh.centroid,
    )
    data = scene.save_image(resolution=(800, 600), visible=True)
    if data and len(data) > 1000:
        Path(out_path).write_bytes(data)
        print(f"  OK  {out_path}")
    else:
        print(f"  FAIL {out_path} ({len(data) if data else 0} bytes)")


def render_three(stl_path: str, slug: str, out_dir: Path):
    """Render top, front, iso views."""
    mesh = trimesh.load(stl_path)
    if hasattr(mesh, "geometry"):
        mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

    alum = np.array([175, 183, 198, 255], dtype=np.uint8)
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=alum)

    distance = mesh.scale * 2.5
    center = mesh.centroid

    views = {
        "top":   (np.pi, 0, 0),
        "front": (np.pi / 2, 0, 0),
        "iso":   (np.pi / 3, 0, np.pi / 4),
    }
    for name, angles in views.items():
        out_path = out_dir / f"gl_{slug}_{name}.png"
        scene = mesh.scene()
        scene.set_camera(angles=angles, distance=distance, center=center)
        data = scene.save_image(resolution=(800, 600), visible=True)
        if data and len(data) > 1000:
            out_path.write_bytes(data)
            print(f"  OK  {out_path}")
        else:
            print(f"  FAIL {out_path}")


STL = "outputs/cad/stl"

GALLERY_PARTS = [
    # (stl_path, output_path)  — single isometric renders for gallery cards
    (f"{STL}/llm_nozzle_bell_small_rocket_engine.stl",   "outputs/screenshots/gl_iso_nozzle.png"),
    (f"{STL}/llm_nozzle_bell_small_rocket_engine.stl",   f"{OUT}/gl_nozzle.png"),
    (f"{STL}/llm_heat_sink_deep_fins_aluminium.stl",     f"{OUT}/gl_heat_sink.png"),
    (f"{STL}/llm_l_bracket_m6_bolt_holes.stl",           f"{OUT}/gl_l_bracket.png"),
    (f"{STL}/llm_asymmetric_freeform_climbing_sloper_hold.stl", f"{OUT}/gl_sloper.png"),
    (f"{STL}/llm_steel_pipe_flange_od_bore.stl",         f"{OUT}/gl_flange.png"),
    (f"{STL}/aria_housing.stl",                           f"{OUT}/gl_housing.png"),
    (f"{STL}/lattice_test_octet.stl",                    f"{OUT}/gl_octet.png"),
]

print("=== Regenerating gallery renders with aluminum material ===")
for stl, out in GALLERY_PARTS:
    if not Path(stl).exists():
        print(f"  SKIP {stl} (not found)")
        continue
    print(f"Rendering {Path(stl).stem}...")
    render_iso(stl, out)

# Also regenerate 3-panel nozzle screenshots for the screenshots dir
print("\nDone.")
