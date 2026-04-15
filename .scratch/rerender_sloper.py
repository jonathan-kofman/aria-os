"""Re-render the climbing sloper hold with proper framing."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

OUT = Path("outputs/gallery_renders")
STL = Path("outputs/cad/stl/llm_asymmetric_freeform_climbing_sloper_hold.stl")
DARK_BG = (8, 11, 16)

mesh = trimesh.load(str(STL))
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

# Center mesh
mesh.apply_translation(-mesh.centroid)

# Color: polyurethane resin (slightly warm off-white)
color = np.array([195, 185, 172, 255], dtype=np.uint8)
mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=color)

scene = trimesh.Scene([mesh])

bounds = mesh.bounds
extents = bounds[1] - bounds[0]
dist = float(extents.max()) * 1.05  # tight framing

scene.set_camera(
    angles=(np.pi * 0.28, 0, np.pi * 0.12),
    distance=dist,
    center=[0, 0, 0],
)

data = scene.save_image(resolution=(800, 600), visible=True)
raw = OUT / "_sloper_raw.png"
raw.write_bytes(data)

# Post-process
img = Image.open(raw).convert("RGB")
arr = np.array(img, dtype=float)
W, H = img.size

# Replace bright background
bright = (arr[:,:,0] > 230) & (arr[:,:,1] > 230) & (arr[:,:,2] > 230)
arr[bright] = DARK_BG

img2 = Image.fromarray(arr.astype(np.uint8))
for corner in [(0,0),(W-1,0),(0,H-1),(W-1,H-1)]:
    px = img2.getpixel(corner)
    if all(c < 60 for c in px):
        ImageDraw.floodfill(img2, corner, DARK_BG, thresh=35)

arr = np.array(img2, dtype=float)

# Vignette
Y_, X_ = np.ogrid[:H, :W]
cx, cy = W/2.0, H/2.0
dist_map = np.sqrt((X_-cx)**2 + (Y_-cy)**2)
vig = 1.0 - 0.28*(dist_map/np.sqrt(cx**2+cy**2))**1.8
for c in range(3):
    arr[:,:,c] = np.clip(arr[:,:,c]*vig, 0, 255)

Image.fromarray(arr.astype(np.uint8)).save(OUT / "gl_sloper.png")
raw.unlink(missing_ok=True)
print(f"Sloper: {(OUT/'gl_sloper.png').stat().st_size:,} bytes")
