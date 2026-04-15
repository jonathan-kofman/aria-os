"""Fix gl_impeller (too small) and gl_flange (white bore interior)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path("outputs/gallery_renders")
STL = Path("outputs/cad/stl")
ALUM = np.array([175, 183, 198, 255], dtype=np.uint8)
DARK_BG = (13, 17, 23)


def load(path):
    m = trimesh.load(str(path))
    if hasattr(m, "geometry"):
        m = trimesh.util.concatenate(list(m.geometry.values()))
    return m


def darken(img_path, thresh=30):
    """Flood-fill from all 4 corners AND replace all isolated near-white regions."""
    img = Image.open(img_path).convert("RGB")
    arr = np.array(img)
    # Step 1: flood-fill from corners
    for corner in [(0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)]:
        px = img.getpixel(corner)
        if all(c > 200 for c in px):
            ImageDraw.floodfill(img, corner, DARK_BG, thresh=thresh)
    arr = np.array(img)
    # Step 2: replace any remaining near-white pixels (bore interior / background
    # fragments not reachable from corners). Aluminum geometry is R~175,G~183,B~198
    # so pixels with all channels > 230 are background/bore leak.
    near_white = (arr[:, :, 0] > 230) & (arr[:, :, 1] > 230) & (arr[:, :, 2] > 230)
    arr[near_white] = DARK_BG
    Image.fromarray(arr.astype(np.uint8)).save(img_path)


def render(mesh, out, scale=1.2, angles=(np.pi/3, 0, np.pi/4)):
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=ALUM)
    extents = np.ptp(mesh.bounds, axis=0)
    dist = float(extents.max()) * scale
    scene = mesh.scene()
    scene.set_camera(angles=angles, distance=dist, center=mesh.centroid)
    data = scene.save_image(resolution=(800, 600), visible=True)
    Path(out).write_bytes(data)
    darken(str(out))
    print(f"  {Path(out).name}: d={dist:.1f}  extents={extents.round(1)}")


# -- Impeller: pull back to scale=1.1 so full disc fits in frame
print("Impeller...")
for candidate in [
    STL / "llm_aluminium_centrifugal_impeller_od_bore.stl",
    STL / "llm_centrifugal_fan_impeller_od_bore.stl",
    STL / "impeller_v3.stl",
]:
    if candidate.exists():
        print(f"  using: {candidate.name}")
        m = load(candidate)
        # Low-angle ISO (22% of pi elevation ≈ 40°) to show blades and hub
        render(m, OUT / "gl_impeller.png", scale=1.1,
               angles=(np.pi * 0.22, 0, np.pi / 4))
        break

# -- Flange: high elevation, scale=0.85, then aggressively kill white bore
print("Flange...")
m = load(STL / "llm_steel_pipe_flange_od_bore.stl")
render(m, OUT / "gl_flange.png", scale=0.9,
       angles=(np.pi * 0.38, 0, np.pi / 4))

print("Done.")
