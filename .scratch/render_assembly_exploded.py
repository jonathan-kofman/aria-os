"""
Exploded assembly view of turbopump: housing + impeller + bolts.
Professional engineering exploded-view style with leader callouts.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path

OUT  = Path("outputs/gallery_renders")
STL  = Path("outputs/cad/stl")
ALUM = np.array([175, 183, 198, 255], dtype=np.uint8)
STEEL = np.array([140, 145, 155, 255], dtype=np.uint8)
DARK_BG = (13, 17, 23)


def load(path):
    m = trimesh.load(str(path))
    if hasattr(m, "geometry"):
        m = trimesh.util.concatenate(list(m.geometry.values()))
    return m


def largest(m):
    parts = m.split(only_watertight=False)
    return max(parts, key=lambda p: len(p.faces))


def render_scene(meshes_colors, angles, distance, out_path, resolution=(1000,700)):
    """Render multiple meshes as one scene."""
    scene = trimesh.Scene()
    for mesh, color in meshes_colors:
        m2 = mesh.copy()
        m2.visual = trimesh.visual.ColorVisuals(mesh=m2, face_colors=color)
        scene.add_geometry(m2)

    all_verts = np.vstack([m.vertices for m, _ in meshes_colors])
    center = (all_verts.max(0) + all_verts.min(0)) / 2
    extents = all_verts.max(0) - all_verts.min(0)

    scene.set_camera(angles=angles, distance=distance, center=center)
    data = scene.save_image(resolution=resolution, visible=True)
    Path(out_path).write_bytes(data)
    return len(data)


# ── Load components ───────────────────────────────────────────────────────────
housing = largest(load(STL / "turbopump_v7.stl"))
impeller = load(STL / "llm_aluminium_centrifugal_impeller_od_bore.stl")

# Build a bolt from a simple cylinder (M8×40)
bolt_mesh = trimesh.creation.cylinder(radius=4.0, height=40.0, sections=16)
head_mesh = trimesh.creation.cylinder(radius=7.5, height=6.0, sections=16)
head_mesh.apply_translation([0, 0, 40])
bolt = trimesh.util.concatenate([bolt_mesh, head_mesh])

# ── Exploded positions ────────────────────────────────────────────────────────
# Housing stays at origin. Impeller moves up. Bolts move further up + outward.
housing_bounds = housing.bounds
h_bottom = housing_bounds[0, 2]
h_top    = housing_bounds[1, 2]
housing_h = h_top - h_bottom

# Center housing at origin
housing_center = (housing_bounds[0] + housing_bounds[1]) / 2
housing.apply_translation(-housing_center)

# Impeller: sits above housing in assembly, exploded further up
imp_bounds = impeller.bounds
imp_h = imp_bounds[1,2] - imp_bounds[0,2]
imp_center = (imp_bounds[0] + imp_bounds[1]) / 2
impeller.apply_translation(-imp_center)
impeller.apply_translation([0, 0, housing_h/2 + imp_h/2 + 50])  # explode up 50mm

# 4 bolts arranged on bolt circle
BOLT_PCD_R = 80.0
for ang in [45, 135, 225, 315]:
    a = np.radians(ang)
    b = bolt.copy()
    b.apply_translation([BOLT_PCD_R*np.cos(a), BOLT_PCD_R*np.sin(a), housing_h/2 + 80])

# ── Render the exploded scene ─────────────────────────────────────────────────
DARK_ALUM = np.array([155, 163, 178, 255], dtype=np.uint8)
LIGHT_ALUM = np.array([185, 193, 208, 255], dtype=np.uint8)

meshes = [
    (housing,  DARK_ALUM),
    (impeller, LIGHT_ALUM),
]

# Add bolts
for ang in [45, 135, 225, 315]:
    a = np.radians(ang)
    b = bolt.copy()
    b.apply_translation([BOLT_PCD_R*np.cos(a), BOLT_PCD_R*np.sin(a), housing_h/2+80])
    meshes.append((b, STEEL))

all_verts = np.vstack([m.vertices for m, _ in meshes])
extents = all_verts.max(0) - all_verts.min(0)
dist = float(extents.max()) * 1.3

render_scene(meshes,
    angles=(np.pi*0.28, 0, np.pi/5),
    distance=dist,
    out_path=OUT / "assembly_exploded_raw.png",
    resolution=(1000, 700))

print("Raw render done")

# ── Post-process: dark background, vignette, leader annotations ───────────────
from PIL import Image, ImageDraw, ImageFont
import numpy as np

img = Image.open(OUT / "assembly_exploded_raw.png").convert("RGB")
arr = np.array(img, dtype=float)
h_img, w_img = arr.shape[:2]

# Dark background
is_light = (arr[:,:,0] > 230) & (arr[:,:,1] > 230) & (arr[:,:,2] > 230)
arr[is_light] = DARK_BG

# Flood fill from corners
from PIL import ImageDraw as IDraw
img2 = Image.fromarray(arr.astype(np.uint8))
for corner in [(0,0),(w_img-1,0),(0,h_img-1),(w_img-1,h_img-1)]:
    px = img2.getpixel(corner)
    if all(c < 50 for c in px):
        IDraw.floodfill(img2, corner, DARK_BG, thresh=40)

arr = np.array(img2, dtype=float)

# Vignette
cy, cx = h_img/2, w_img/2
Y, X = np.ogrid[:h_img, :w_img]
dist_map = np.sqrt((X-cx)**2+(Y-cy)**2)
vig = 1.0 - 0.30*(dist_map/np.sqrt(cx**2+cy**2))**1.8
for c in range(3):
    arr[:,:,c] *= vig
arr = np.clip(arr, 0, 255).astype(np.uint8)

final = Image.fromarray(arr)
draw = ImageDraw.Draw(final)

# ── Leader lines and part numbers ────────────────────────────────────────────
# Approximate pixel positions (will vary — good-faith approximation)
W, H_img = final.size
cx_px, cy_px = W//2, H_img//2

callouts = [
    (cx_px - 80, cy_px + 60,  cx_px - 200, cy_px + 140, "1", "TURBOPUMP HOUSING\nAL 6061-T6"),
    (cx_px - 10, cy_px - 100, cx_px - 150, cy_px - 180, "2", "CENTRIFUGAL IMPELLER\nAL 6061-T6"),
    (cx_px + 120, cy_px - 80, cx_px + 250, cy_px - 50, "3", "BOLT M8×40\n(4×) GR8.8 ZnPh"),
]

for x0, y0, x1, y1, num, label in callouts:
    # Leader line
    draw.line([(x0, y0), (x1, y1)], fill=(140, 160, 180), width=1)
    # Circle balloon
    r = 14
    draw.ellipse([(x1-r, y1-r), (x1+r, y1+r)], outline=(140,160,180), width=1)
    draw.text((x1, y1), num, fill=(230,237,243), anchor='mm')
    # Label text
    lx = x1 - 16 if x1 < W//2 else x1 + 16
    align = 'right' if x1 < W//2 else 'left'
    for i, line in enumerate(label.split('\n')):
        draw.text((lx, y1 - 10 + i*12), line, fill=(139, 148, 158))

# Title bar at bottom
draw.rectangle([(0, H_img-38), (W, H_img)], fill=(17, 22, 30))
draw.text((10, H_img-30), "TURBOPUMP ASSEMBLY  |  EXPLODED VIEW  |  ARIA-OS",
         fill=(100,120,140))
draw.text((W-10, H_img-30), "DWG: TP-ASM-001 | SCALE NTS | REV A",
         fill=(100,120,140), anchor='ra')

# Part list (BOM block)
bom_x, bom_y = W-260, 10
draw.rectangle([(bom_x, bom_y), (W-5, bom_y+70)], fill=(17,22,30), outline=(40,50,65))
draw.text((bom_x+8, bom_y+6), "ITEM  QTY  PART NO.       DESCRIPTION", fill=(80,100,120))
draw.line([(bom_x, bom_y+20), (W-5, bom_y+20)], fill=(40,50,65))
bom_rows = [
    "  1    1   TP-HSG-001     Turbopump Housing",
    "  2    1   IMP-001        Centrifugal Impeller",
    "  3    4   FAS-M8x40-GR8  Bolt M8×40 Gr8.8",
]
for i, row in enumerate(bom_rows):
    draw.text((bom_x+8, bom_y+24+i*14), row, fill=(139, 148, 158))

final.save(OUT / "assembly_v7.png")
print(f"Assembly exploded: {(OUT/'assembly_v7.png').stat().st_size:,} bytes")
