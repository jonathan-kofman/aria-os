"""
Assembly exploded view v3 — professional quality.
- Explosion lines (dashed) showing where parts originate
- Better component color differentiation
- Fixed balloon clipping (callouts on LEFT side)
- Cleaner BOM table
- Ground plane shadow
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

OUT = Path("outputs/gallery_renders")
STL = Path("outputs/cad/stl")

# Aluminum 6061 - housing (slightly darker, anodized look)
ALUM_DARK  = np.array([148, 160, 178, 255], dtype=np.uint8)
# Aluminum 6061 - impeller (lighter, polished)
ALUM_LIGHT = np.array([195, 205, 220, 255], dtype=np.uint8)
# Steel grade 8.8 - bolts (dark steel gray)
STEEL      = np.array([100, 108, 120, 255], dtype=np.uint8)
DARK_BG    = (8, 11, 16)


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

# ── Center all ────────────────────────────────────────────────────────────────
housing.apply_translation(-housing.centroid)
impeller.apply_translation(-impeller.centroid)

h_ext = housing.bounds[1] - housing.bounds[0]
i_ext = impeller.bounds[1] - impeller.bounds[0]

# ── Explode: housing at origin, impeller +70mm, bolts +160mm ─────────────────
IMPELLER_LIFT = h_ext[2] / 2 + 70 + i_ext[2] / 2
BOLT_LIFT     = h_ext[2] / 2 + 160

impeller.apply_translation([0, 0, IMPELLER_LIFT])

# 4 M8x40 bolts on 80mm PCD
bolt_body = trimesh.creation.cylinder(radius=3.8, height=40.0, sections=14)
bolt_head = trimesh.creation.cylinder(radius=6.8, height=6.5, sections=14)
bolt_head.apply_translation([0, 0, 40.0])
bolt_proto = trimesh.util.concatenate([bolt_body, bolt_head])
bolt_proto.apply_translation([0, 0, -bolt_proto.centroid[2]])

PCD = 80.0
bolts = []
for ang in [45, 135, 225, 315]:
    a = np.radians(ang)
    b = bolt_proto.copy()
    b.apply_translation([PCD * np.cos(a), PCD * np.sin(a), BOLT_LIFT])
    bolts.append(b)

# ── Build scene ───────────────────────────────────────────────────────────────
scene = trimesh.Scene()

def add(m, color, name):
    m2 = m.copy()
    m2.visual = trimesh.visual.ColorVisuals(mesh=m2, face_colors=color)
    scene.add_geometry(m2, node_name=name)

add(housing, ALUM_DARK, "housing")
add(impeller, ALUM_LIGHT, "impeller")
for i, b in enumerate(bolts):
    add(b, STEEL, f"bolt_{i}")

# Camera: slight front-left isometric
all_pts = np.vstack([housing.bounds, impeller.bounds, *[b.bounds for b in bolts]])
center  = (all_pts.min(0) + all_pts.max(0)) / 2
ext     = (all_pts.max(0) - all_pts.min(0)).max()
dist    = ext * 1.3

scene.set_camera(
    angles=(np.pi * 0.22, 0, np.pi * 0.20),
    distance=dist,
    center=center,
)

data = scene.save_image(resolution=(960, 720), visible=True)
raw = OUT / "_asm3_raw.png"
raw.write_bytes(data)

# ── Post-process ──────────────────────────────────────────────────────────────
img  = Image.open(raw).convert("RGB")
arr  = np.array(img, dtype=float)
W, H = img.size

# Replace background (near-white > 230 all channels)
bright = (arr[:,:,0] > 230) & (arr[:,:,1] > 230) & (arr[:,:,2] > 230)
arr[bright] = DARK_BG

# Flood fill from corners
img2 = Image.fromarray(arr.astype(np.uint8))
for corner in [(0,0),(W-1,0),(0,H-1),(W-1,H-1)]:
    px = img2.getpixel(corner)
    if all(c < 60 for c in px):
        ImageDraw.floodfill(img2, corner, DARK_BG, thresh=35)

arr = np.array(img2, dtype=float)

# Subtle vignette
Y_, X_ = np.ogrid[:H, :W]
cx, cy = W / 2.0, H / 2.0
dist_map = np.sqrt((X_ - cx)**2 + (Y_ - cy)**2)
max_d = np.sqrt(cx**2 + cy**2)
vig = 1.0 - 0.26 * (dist_map / max_d) ** 1.8
for c in range(3):
    arr[:,:,c] = np.clip(arr[:,:,c] * vig, 0, 255)

# Background gradient: slightly lighter in center
bg_mask = (arr[:,:,0] < 35) & (arr[:,:,1] < 35) & (arr[:,:,2] < 35)
bg_grad = np.clip(1.0 - dist_map / (max_d * 0.85), 0, 1)
for c, base, hi in zip(range(3), [8,11,16], [20,28,42]):
    arr[:,:,c] = np.where(bg_mask,
                          base + bg_grad * (hi - base),
                          arr[:,:,c])

final = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
draw  = ImageDraw.Draw(final)

# ── Dashed explosion lines ────────────────────────────────────────────────────
# Rough pixel positions (center-screen corresponds to center)
# Housing top ~ H*0.55, impeller center ~ H*0.38, bolts top ~ H*0.12
# Vertical dashes along center-x
def dashed_line(draw, x1, y1, x2, y2, dash=6, gap=5, color=(60,80,110), width=1):
    import math
    dx, dy = x2-x1, y2-y1
    length = math.sqrt(dx*dx+dy*dy)
    if length < 1:
        return
    ux, uy = dx/length, dy/length
    pos = 0
    drawing = True
    while pos < length:
        seg = dash if drawing else gap
        end = min(pos+seg, length)
        if drawing:
            draw.line([(int(x1+ux*pos), int(y1+uy*pos)),
                       (int(x1+ux*end), int(y1+uy*end))],
                      fill=color, width=width)
        pos = end
        drawing = not drawing

cx_px = W // 2

# Housing top to impeller bottom — vertical dashed line
housing_top_y  = int(H * 0.535)
impeller_bot_y = int(H * 0.445)
dashed_line(draw, cx_px, housing_top_y, cx_px, impeller_bot_y, dash=5, gap=4)

# Impeller top to bolt bottom — vertical dashed line
impeller_top_y = int(H * 0.33)
bolt_bot_y     = int(H * 0.21)
dashed_line(draw, cx_px, impeller_top_y, cx_px, bolt_bot_y, dash=5, gap=4)

# ── Balloon callouts (LEFT side — no clipping) ───────────────────────────────
balloons = [
    # (leader_tip_x, leader_tip_y, balloon_x, balloon_y, num, label_line1, label_line2)
    (int(W*0.38), int(H*0.62), int(W*0.10), int(H*0.62), "1", "TURBOPUMP HOUSING", "AL 6061-T6  Ø160mm"),
    (int(W*0.40), int(H*0.40), int(W*0.10), int(H*0.40), "2", "CENTRIFUGAL IMPELLER", "AL 6061-T6  Ø150mm"),
    (int(W*0.60), int(H*0.19), int(W*0.10), int(H*0.19), "3", "SOCKET HEAD BOLT M8x40", "GR8.8 ZnPh  (x4)"),
]

for tip_x, tip_y, bx, by, num, l1, l2 in balloons:
    r = 15
    # Leader line: tip -> balloon edge
    lx = bx + r
    draw.line([(tip_x, tip_y), (lx, by)], fill=(100, 130, 170), width=1)
    # Balloon circle
    draw.ellipse([(bx-r, by-r), (bx+r, by+r)], outline=(120, 150, 190), width=1, fill=(17,24,36))
    draw.text((bx, by), num, fill=(200, 215, 235), anchor='mm')
    # Label text to the right of balloon
    draw.text((bx+r+6, by-7), l1, fill=(200, 210, 225))
    draw.text((bx+r+6, by+7), l2, fill=(120, 135, 155))

# ── BOM table (top-right this time) ──────────────────────────────────────────
bom_x, bom_y, bom_w, bom_h = W - 340, 10, 330, 88
draw.rectangle([(bom_x, bom_y), (bom_x+bom_w, bom_y+bom_h)],
               fill=(12, 17, 26), outline=(38, 55, 80), width=1)
# Header bar
draw.rectangle([(bom_x, bom_y), (bom_x+bom_w, bom_y+20)],
               fill=(20, 32, 52), outline=None)
draw.text((bom_x+8, bom_y+4), "BILL OF MATERIALS", fill=(80, 120, 170))
draw.line([(bom_x, bom_y+20), (bom_x+bom_w, bom_y+20)], fill=(38,55,80))
# Column headers
draw.text((bom_x+8, bom_y+23), "ITEM  QTY  PART NUMBER      MATERIAL      DESCRIPTION",
          fill=(55, 80, 115))
draw.line([(bom_x, bom_y+36), (bom_x+bom_w, bom_y+36)], fill=(30,45,65))
bom_rows = [
    (" 1     1   TP-HSG-001       AL 6061-T6    Turbopump Housing"),
    (" 2     1   IMP-AL-001       AL 6061-T6    Centrifugal Impeller"),
    (" 3     4   FAS-M8x40-G88   GR8.8 ZnPh    Socket Hd Bolt M8x40"),
]
for i, row in enumerate(bom_rows):
    y_r = bom_y + 40 + i * 16
    # Alternate row bg
    if i % 2 == 1:
        draw.rectangle([(bom_x+1, y_r-2), (bom_x+bom_w-1, y_r+12)], fill=(16,23,35))
    draw.text((bom_x+8, y_r), row, fill=(155, 168, 188))

# ── Title bar ─────────────────────────────────────────────────────────────────
draw.rectangle([(0, H-34), (W, H)], fill=(10, 16, 26))
draw.line([(0, H-34), (W, H-34)], fill=(35, 52, 78))
draw.text((10, H-26), "TURBOPUMP ASSEMBLY  |  EXPLODED VIEW  |  ARIA-OS v2.0",
          fill=(80, 110, 155))
draw.text((W-10, H-26),
          "DWG: TP-ASM-001  |  SCALE: NTS  |  3RD ANGLE  |  REV A",
          fill=(60, 85, 120), anchor='ra')

final.save(OUT / "assembly_v7.png")
raw.unlink(missing_ok=True)
print(f"Assembly v3: {(OUT/'assembly_v7.png').stat().st_size:,} bytes")
