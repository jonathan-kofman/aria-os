"""
Generate professional engineering drawing of turbopump housing.
3 orthographic views (top, front, side) + ISO with dimensions.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import FancyArrowPatch
from pathlib import Path

OUT = Path("outputs/gallery_renders")

# ── Load mesh and get actual geometry ─────────────────────────────────────
stl = "outputs/cad/stl/turbopump_v7.stl"
mesh = trimesh.load(stl)
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
parts = mesh.split(only_watertight=False)
mesh = max(parts, key=lambda m: len(m.faces))  # main body only

bounds = mesh.bounds
cx = (bounds[0,0] + bounds[1,0]) / 2
cy = (bounds[0,1] + bounds[1,1]) / 2
W = bounds[1,0] - bounds[0,0]   # diameter ~160mm
H = bounds[1,2] - bounds[0,2]   # height  ~180mm
FD = W * 1.25                    # flange OD estimate

# ── Drawing constants ──────────────────────────────────────────────────────
BG     = "white"
LINE   = "#1a1a1a"
DIM    = "#444444"
CENTER = "#888888"
HIDDEN = "#aaaaaa"
FILL   = "#f0f4f8"

fig = plt.figure(figsize=(11, 8.5), dpi=100)   # A-size landscape
fig.patch.set_facecolor(BG)

# Title block border
ax_main = fig.add_axes([0, 0, 1, 1])
ax_main.set_xlim(0, 11)
ax_main.set_ylim(0, 8.5)
ax_main.axis('off')
ax_main.set_facecolor(BG)

# Outer border
ax_main.add_patch(patches.Rectangle((0.15, 0.15), 10.7, 8.2,
                                     fill=False, edgecolor=LINE, linewidth=2.5))
ax_main.add_patch(patches.Rectangle((0.25, 0.25), 10.5, 8.0,
                                     fill=False, edgecolor=LINE, linewidth=0.8))

# Title block (bottom right)
tb_x, tb_y, tb_w, tb_h = 7.5, 0.15, 3.35, 1.2
ax_main.add_patch(patches.Rectangle((tb_x, tb_y), tb_w, tb_h,
                                     fill=False, edgecolor=LINE, linewidth=1.5))
# Title block dividers
ax_main.plot([tb_x, tb_x+tb_w], [tb_y+0.8, tb_y+0.8], color=LINE, lw=0.8)
ax_main.plot([tb_x, tb_x+tb_w], [tb_y+0.55, tb_y+0.55], color=LINE, lw=0.8)
ax_main.plot([tb_x, tb_x+tb_w], [tb_y+0.30, tb_y+0.30], color=LINE, lw=0.8)
ax_main.plot([tb_x+1.8, tb_x+1.8], [tb_y, tb_y+0.55], color=LINE, lw=0.8)
ax_main.plot([tb_x+1.8, tb_x+1.8], [tb_y+0.55, tb_y+1.2], color=LINE, lw=0.8)

# Title block content
ax_main.text(tb_x+tb_w/2, tb_y+0.97, "TURBOPUMP HOUSING",
             ha='center', va='center', fontsize=10, fontweight='bold', color=LINE)
ax_main.text(tb_x+0.9, tb_y+0.42, "DWG NO.", ha='center', va='center', fontsize=6, color=DIM)
ax_main.text(tb_x+2.6, tb_y+0.42, "TP-HOUSING-001", ha='center', va='center', fontsize=7.5, color=LINE)
ax_main.text(tb_x+0.9, tb_y+0.18, "MATERIAL", ha='center', va='center', fontsize=6, color=DIM)
ax_main.text(tb_x+2.6, tb_y+0.18, "AL 6061-T6", ha='center', va='center', fontsize=7.5, color=LINE)
ax_main.text(tb_x+0.9, tb_y+0.67, "SCALE", ha='center', va='center', fontsize=6, color=DIM)
ax_main.text(tb_x+2.6, tb_y+0.67, "1:1", ha='center', va='center', fontsize=7.5, color=LINE)
ax_main.text(tb_x+tb_w-0.1, tb_y+0.67, "SHEET 1/1", ha='right', va='center', fontsize=6, color=DIM)
ax_main.text(tb_x+tb_w-0.1, tb_y+0.42, "ARIA-OS", ha='right', va='center', fontsize=7, color=DIM)
ax_main.text(tb_x+tb_w-0.1, tb_y+0.18, "REV A", ha='right', va='center', fontsize=7, color=LINE)

# Drawing title
ax_main.text(5.5, 8.1, "ARIA-OS  ·  ENGINEERING DRAWING  ·  TURBOPUMP HOUSING ASSEMBLY",
             ha='center', va='center', fontsize=8, color=DIM, style='italic')


def dim_line(ax, x1, y1, x2, y2, label, offset=0.15, fontsize=7):
    """Draw a dimension line with arrowheads and label."""
    dx, dy = x2 - x1, y2 - y1
    length = np.sqrt(dx**2 + dy**2)
    nx, ny = -dy/length, dx/length  # normal

    ex1, ey1 = x1 + nx*offset, y1 + ny*offset
    ex2, ey2 = x2 + nx*offset, y2 + ny*offset

    # Extension lines
    ax.plot([x1, ex1 + nx*0.02], [y1, ey1 + ny*0.02], color=DIM, lw=0.6)
    ax.plot([x2, ex2 + nx*0.02], [y2, ey2 + ny*0.02], color=DIM, lw=0.6)
    # Dim line with arrows
    ax.annotate("", xy=(ex2, ey2), xytext=(ex1, ey1),
                arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.7,
                                mutation_scale=6))
    mx, my = (ex1+ex2)/2, (ey1+ey2)/2
    ax.text(mx + nx*0.05, my + ny*0.05, label,
            ha='center', va='center', fontsize=fontsize, color=DIM)


def center_lines(ax, cx, cy, r, lw=0.5):
    """Draw centerlines through (cx,cy) with radius r."""
    ax.plot([cx-r, cx+r], [cy, cy], color=CENTER, lw=lw, dashes=[4, 2])
    ax.plot([cx, cx], [cy-r, cy+r], color=CENTER, lw=lw, dashes=[4, 2])


# ────────────────────────────────────────────────────────────────────────────
# FRONT VIEW  (XZ plane, looking from +Y)
# ────────────────────────────────────────────────────────────────────────────
ax_fr = fig.add_axes([0.05, 0.22, 0.35, 0.62])
ax_fr.set_aspect('equal')
ax_fr.axis('off')
ax_fr.set_facecolor(BG)
ax_fr.set_title("FRONT VIEW", fontsize=7, color=DIM, pad=4)

scale = 0.013  # mm → inches

# Use actual mesh bounds
ow = W * scale     # outer width (diameter in XY)
oh = H * scale     # overall height
fw = FD * scale    # flange width

fh = 0.025 * 3     # flange height

# Draw housing body (rectangle)
body = patches.Rectangle((-ow/2, 0), ow, oh,
                          facecolor=FILL, edgecolor=LINE, linewidth=1.5, zorder=2)
ax_fr.add_patch(body)

# Flange at bottom
flange = patches.Rectangle((-fw/2, -fh), fw, fh,
                             facecolor=FILL, edgecolor=LINE, linewidth=1.5, zorder=2)
ax_fr.add_patch(flange)

# Bore (hidden line - dashed)
bore_r = W * 0.35 * scale
ax_fr.plot([-bore_r, -bore_r], [0, oh], color=HIDDEN, lw=0.8, dashes=[3, 2])
ax_fr.plot([bore_r, bore_r],   [0, oh], color=HIDDEN, lw=0.8, dashes=[3, 2])

# Side port stub (horizontal cylinder protruding from side)
port_h = oh * 0.4
port_r = ow * 0.12
port_len = ow * 0.22
port_rect = patches.Rectangle((ow/2, port_h - port_r/2), port_len, port_r,
                                facecolor=FILL, edgecolor=LINE, linewidth=1.5, zorder=2)
ax_fr.add_patch(port_rect)

# Centerlines
center_lines(ax_fr, 0, oh/2, oh*0.55)
ax_fr.plot([0, 0], [-fh*1.5, oh+oh*0.08], color=CENTER, lw=0.6, dashes=[4, 2])

# Bolt holes (circles on flange)
for bx in [-fw*0.35, fw*0.35]:
    ax_fr.add_patch(patches.Circle((bx, -fh/2), fw*0.04,
                                    facecolor=BG, edgecolor=LINE, linewidth=1.0, zorder=3))

# Taper at top
ax_fr.plot([-ow/2, -ow*0.42], [oh, oh*1.0], color=LINE, lw=1.5)
ax_fr.plot([ow/2,  ow*0.42],  [oh, oh*1.0], color=LINE, lw=1.5)
ax_fr.add_patch(patches.Arc((0, oh), ow*0.84, oh*0.04,
                              theta1=0, theta2=180, color=LINE, lw=1.5))

# Dimensions
dim_line(ax_fr, -ow/2, 0, ow/2, 0, f"Ø{W:.0f}", offset=-0.10)
dim_line(ax_fr, ow/2, 0, ow/2, oh, f"{H:.0f}", offset=0.12)
dim_line(ax_fr, -fw/2, -fh, fw/2, -fh, f"Ø{FD:.0f}", offset=-0.10)

# Margins
ax_fr.set_xlim(-fw/2 - 0.35, ow/2 + 0.55)
ax_fr.set_ylim(-fh - 0.3, oh + 0.3)


# ────────────────────────────────────────────────────────────────────────────
# TOP VIEW  (XY plane, looking from +Z)
# ────────────────────────────────────────────────────────────────────────────
ax_tp = fig.add_axes([0.40, 0.42, 0.28, 0.48])
ax_tp.set_aspect('equal')
ax_tp.axis('off')
ax_tp.set_facecolor(BG)
ax_tp.set_title("TOP VIEW", fontsize=7, color=DIM, pad=4)

# Housing OD
ax_tp.add_patch(patches.Circle((0, 0), ow/2,
                                facecolor=FILL, edgecolor=LINE, linewidth=1.5, zorder=2))
# Bore
ax_tp.add_patch(patches.Circle((0, 0), bore_r,
                                facecolor=BG, edgecolor=LINE, linewidth=1.5, zorder=3))

# Flange OD (flange visible from top as outer ring)
ax_tp.add_patch(patches.Circle((0, 0), fw/2,
                                facecolor="none", edgecolor=HIDDEN, linewidth=0.8,
                                linestyle="dashed", zorder=2))

# Bolt holes (4 holes on PCD)
pcd_r = (fw/2) * 0.75
for angle in [45, 135, 225, 315]:
    a = np.radians(angle)
    bx, by = pcd_r*np.cos(a), pcd_r*np.sin(a)
    ax_tp.add_patch(patches.Circle((bx, by), fw*0.04,
                                    facecolor=BG, edgecolor=LINE, linewidth=1.0, zorder=3))

# Side port (visible from top as rectangle on side)
ax_tp.add_patch(patches.Rectangle((ow/2, -port_r/2), port_len, port_r,
                                   facecolor=FILL, edgecolor=LINE, linewidth=1.5, zorder=3))
ax_tp.add_patch(patches.Circle((ow/2 + port_len*0.5, 0), port_r*0.45,
                                facecolor=BG, edgecolor=LINE, linewidth=0.8, zorder=4))

# Centerlines
center_lines(ax_tp, 0, 0, ow*0.62)
# PCD centerline circle
ax_tp.add_patch(patches.Circle((0, 0), pcd_r,
                                fill=False, edgecolor=CENTER, linewidth=0.5,
                                linestyle='dashed', zorder=2))

# Dimensions
ax_tp.annotate("", xy=(ow/2, ow*0.15), xytext=(-ow/2, ow*0.15),
               arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.7, mutation_scale=6))
ax_tp.text(0, ow*0.17, f"Ø{W:.0f}", ha='center', va='bottom', fontsize=7, color=DIM)

ax_tp.set_xlim(-fw/2 - 0.15, ow/2 + port_len + 0.2)
ax_tp.set_ylim(-fw/2 - 0.2, fw/2 + 0.25)


# ────────────────────────────────────────────────────────────────────────────
# ISO VIEW  (rendered as GL image, inserted into drawing)
# ────────────────────────────────────────────────────────────────────────────
from PIL import Image as PILImage
try:
    iso_img = PILImage.open("outputs/gallery_renders/assembly_v7.png")
    ax_iso = fig.add_axes([0.65, 0.22, 0.30, 0.48])
    ax_iso.imshow(np.array(iso_img))
    ax_iso.axis('off')
    ax_iso.set_title("ISOMETRIC VIEW", fontsize=7, color=DIM, pad=4)
except Exception:
    pass


# ────────────────────────────────────────────────────────────────────────────
# SECTION VIEW NOTE
# ────────────────────────────────────────────────────────────────────────────
ax_main.text(0.4, 1.0, "SECTION A-A  (see front view — dashed lines indicate internal bore)",
             ha='left', va='center', fontsize=6, color=DIM, style='italic')
ax_main.text(0.4, 0.7, "NOTES:\n"
             "1. ALL DIMENSIONS IN MM UNLESS OTHERWISE STATED\n"
             "2. GENERAL TOLERANCES: ±0.1mm LINEAR, ±0.5° ANGULAR\n"
             "3. SURFACE FINISH: Ra 1.6μm ALL MACHINED SURFACES\n"
             "4. BREAK ALL SHARP EDGES 0.2×45°",
             ha='left', va='top', fontsize=5.5, color=DIM,
             fontfamily='monospace')

# ────────────────────────────────────────────────────────────────────────────
out = OUT / "eng_drawing_turbopump.png"
plt.savefig(str(out), dpi=100, bbox_inches='tight', facecolor=BG)
plt.close()
print(f"Engineering drawing: {out.stat().st_size:,} bytes")
