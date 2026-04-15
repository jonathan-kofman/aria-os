"""
Exploded assembly view: turbopump housing + impeller + M8 bolts.
Better separation, clearer component visibility, BOM block.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path
from PIL import Image, ImageDraw
import io

OUT  = Path("outputs/gallery_renders")
STL  = Path("outputs/cad/stl")
ALUM_H  = np.array([168, 176, 192, 255], dtype=np.uint8)  # housing - slightly darker
ALUM_I  = np.array([195, 202, 215, 255], dtype=np.uint8)  # impeller - lighter
STEEL   = np.array([130, 138, 150, 255], dtype=np.uint8)  # bolts
DARK_BG = (13, 17, 23)


def load(path):
    m = trimesh.load(str(path))
    if hasattr(m, "geometry"):
        m = trimesh.util.concatenate(list(m.geometry.values()))
    return m


def largest(m):
    parts = m.split(only_watertight=False)
    return max(parts, key=lambda p: len(p.faces))


# ── Load ──────────────────────────────────────────────────────────────────────
housing  = largest(load(STL / "turbopump_v7.stl"))
impeller = load(STL / "llm_aluminium_centrifugal_impeller_od_bore.stl")

# ── Center all parts at their own centroids ───────────────────────────────────
housing.apply_translation(-housing.centroid)
impeller.apply_translation(-impeller.centroid)

h_extents = housing.bounds[1] - housing.bounds[0]
i_extents = impeller.bounds[1] - impeller.bounds[0]

# ── Explode along Z: impeller goes UP, bolts go further up ───────────────────
IMPELLER_GAP = 80    # mm clearance between housing top and impeller bottom
BOLT_EXTRA   = 100   # mm above impeller for bolts

housing_top  = housing.bounds[1, 2]
impeller_bot_offset = housing_top + IMPELLER_GAP + i_extents[2]/2
impeller.apply_translation([0, 0, impeller_bot_offset])

# 4 M8×40 bolts on 80mm PCD
bolt_body = trimesh.creation.cylinder(radius=4.0, height=42.0, sections=12)
bolt_head = trimesh.creation.cylinder(radius=7.0, height=7.0, sections=12)
bolt_head.apply_translation([0, 0, 42])
bolt_full = trimesh.util.concatenate([bolt_body, bolt_head])
bolt_full.apply_translation([0, 0, -bolt_full.centroid[2]])

bolt_z = impeller.bounds[1, 2] + BOLT_EXTRA
PCD_R = 80.0
bolts = []
for ang in [45, 135, 225, 315]:
    a = np.radians(ang)
    b = bolt_full.copy()
    b.apply_translation([PCD_R*np.cos(a), PCD_R*np.sin(a), bolt_z])
    bolts.append(b)

# ── Build scene ───────────────────────────────────────────────────────────────
scene = trimesh.Scene()

def add(m, color, name):
    m2 = m.copy()
    m2.visual = trimesh.visual.ColorVisuals(mesh=m2, face_colors=color)
    scene.add_geometry(m2, node_name=name)

add(housing,  ALUM_H, "housing")
add(impeller, ALUM_I, "impeller")
for i, b in enumerate(bolts):
    add(b, STEEL, f"bolt_{i}")

# Camera framing
all_pts = np.vstack([
    housing.bounds, impeller.bounds,
    *[b.bounds for b in bolts]
])
center = (all_pts.min(0) + all_pts.max(0)) / 2
ext    = (all_pts.max(0) - all_pts.min(0)).max()
dist   = ext * 1.35

scene.set_camera(
    angles=(np.pi * 0.25, 0, np.pi * 0.18),
    distance=dist,
    center=center
)

data = scene.save_image(resolution=(900, 700), visible=True)
raw_path = OUT / "_asm_raw.png"
raw_path.write_bytes(data)

# ── Post-process ──────────────────────────────────────────────────────────────
img = Image.open(raw_path).convert("RGB")
arr = np.array(img, dtype=np.uint8)
W, H = img.size

# Dark background: replace near-white bg
from PIL import ImageDraw as IDraw
bright = (arr[:,:,0] > 230) & (arr[:,:,1] > 230) & (arr[:,:,2] > 230)
arr[bright] = DARK_BG

img2 = Image.fromarray(arr)
for corner in [(0,0),(W-1,0),(0,H-1),(W-1,H-1)]:
    px = img2.getpixel(corner)
    if all(c < 60 for c in px):
        IDraw.floodfill(img2, corner, DARK_BG, thresh=35)

arr = np.array(img2, dtype=float)

# Vignette
cy_v, cx_v = H/2, W/2
Y_, X_ = np.ogrid[:H, :W]
vig = 1.0 - 0.28 * (np.sqrt((X_-cx_v)**2+(Y_-cy_v)**2) / np.sqrt(cx_v**2+cy_v**2))**1.8
for c in range(3):
    arr[:,:,c] *= vig
arr = np.clip(arr, 0, 255).astype(np.uint8)

final = Image.fromarray(arr)
draw  = ImageDraw.Draw(final)

# ── Annotations ───────────────────────────────────────────────────────────────
# Approximate balloon positions (right side, stacked)
balloons = [
    (W-90, H//2 + 80,  "1", "TURBOPUMP HOUSING"),
    (W-90, H//2 - 20,  "2", "CENTRIFUGAL IMPELLER"),
    (W-90, H//2 - 100, "3", "BOLT M8×40 Gr8.8 (×4)"),
]

for bx, by, num, label in balloons:
    r = 13
    # Leader line to part (approximate)
    draw.line([(bx-r, by), (bx-r-40, by)], fill=(100,120,145), width=1)
    # Balloon
    draw.ellipse([(bx-r, by-r),(bx+r, by+r)], outline=(140,160,180), width=1)
    draw.text((bx, by), num, fill=(220,230,245), anchor='mm')
    draw.text((bx+r+4, by), label, fill=(139,148,158), anchor='lm')

# BOM inset (top-left)
bom_x, bom_y, bom_w, bom_h = 8, 8, 340, 72
draw.rectangle([(bom_x, bom_y),(bom_x+bom_w, bom_y+bom_h)],
               fill=(17,22,30), outline=(40,50,65))
draw.text((bom_x+6, bom_y+5), "BILL OF MATERIALS", fill=(80,110,150))
draw.line([(bom_x, bom_y+20),(bom_x+bom_w, bom_y+20)], fill=(40,50,65))
draw.text((bom_x+6, bom_y+22), "ITEM  QTY  PART NO.       MATERIAL     DESCRIPTION",
         fill=(60,80,105))
draw.line([(bom_x, bom_y+34),(bom_x+bom_w, bom_y+34)], fill=(40,50,65))
bom_data = [
    " 1     1   TP-HSG-001     AL 6061-T6   Turbopump Housing",
    " 2     1   IMP-AL-001     AL 6061-T6   Centrifugal Impeller",
    " 3     4   FAS-M8x40-G8  GR8.8 ZnPh   Socket Head Bolt M8×40",
]
for i, row in enumerate(bom_data):
    draw.text((bom_x+6, bom_y+37+i*11), row, fill=(139,148,158))

# Title bar
draw.rectangle([(0, H-36),(W, H)], fill=(15,20,28))
draw.text((8, H-28), "TURBOPUMP ASSEMBLY  ·  EXPLODED VIEW  ·  ARIA-OS v2.0",
         fill=(80,110,145))
draw.text((W-8, H-28),
         "DWG: TP-ASM-001  |  SCALE: NTS  |  3RD ANGLE  |  REV A",
         fill=(70,90,115), anchor='ra')

final.save(OUT / "assembly_v7.png")
raw_path.unlink(missing_ok=True)
print(f"Assembly: {(OUT/'assembly_v7.png').stat().st_size:,} bytes")
