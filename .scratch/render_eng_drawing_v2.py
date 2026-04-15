"""
ANSI B-size (17x11) engineering drawing of turbopump housing.
Third-angle projection, GD&T, proper title block, section view.
Designed to match output quality from SolidWorks / Creo drawings.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import Arc
from pathlib import Path

OUT = Path("outputs/gallery_renders")
W, H = 17.0, 11.0   # ANSI B landscape, inches
DPI = 120

# ── Color / line style ────────────────────────────────────────────────────────
BG    = "white"
VIS   = "#0d0d0d"    # visible edges (thick)
HID   = "#777777"    # hidden lines (thin dashed)
DIM   = "#0d0d0d"    # dimension lines
CL    = "#777777"    # centerlines
FILL  = "#e8ecf0"    # section hatching background
NOTE  = "#111111"

fig = plt.figure(figsize=(W, H), dpi=DPI)
fig.patch.set_facecolor(BG)

ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W); ax.set_ylim(0, H)
ax.axis('off'); ax.set_facecolor(BG)

# ── Sheet border (ANSI B: 0.75" left, 0.5" others) ───────────────────────────
BL, BR, BB, BT = 0.75, 16.5, 0.5, 10.5   # border extents
ax.add_patch(patches.Rectangle((BL, BB), BR-BL, BT-BB,
    fill=False, edgecolor=VIS, linewidth=2.2, zorder=10))

# Zone letters/numbers around border
for i, ltr in enumerate("ABCDEF"):
    y = BT - (i+0.5)*(BT-BB)/6
    ax.text(BL-0.28, y, ltr, ha='center', va='center', fontsize=6.5,
            color=NOTE, fontfamily='monospace', zorder=10)
    ax.text(BR+0.14, y, ltr, ha='center', va='center', fontsize=6.5,
            color=NOTE, fontfamily='monospace', zorder=10)
for i in range(1, 9):
    x = BL + (i-0.5)*(BR-BL)/8
    ax.text(x, BT+0.14, str(i), ha='center', va='center', fontsize=6.5,
            color=NOTE, fontfamily='monospace', zorder=10)
    ax.text(x, BB-0.18, str(i), ha='center', va='center', fontsize=6.5,
            color=NOTE, fontfamily='monospace', zorder=10)

# ── Title block (bottom-right, 5.8" wide × 2.0" tall) ───────────────────────
tb_x, tb_w, tb_h = BR-5.8, 5.8, 2.0
tb_y = BB
ax.add_patch(patches.Rectangle((tb_x, tb_y), tb_w, tb_h,
    fill=True, facecolor="#f8f9fa", edgecolor=VIS, linewidth=1.2, zorder=8))

def tb_rule(y_off):
    ax.plot([tb_x, tb_x+tb_w], [tb_y+y_off, tb_y+y_off], color=VIS, lw=0.6, zorder=9)

def tb_vrule(x_off, y0, y1):
    ax.plot([tb_x+x_off, tb_x+x_off], [tb_y+y0, tb_y+y1], color=VIS, lw=0.6, zorder=9)

def tb_label(x, y, txt, size=5.5, bold=False):
    ax.text(tb_x+x, tb_y+y, txt, fontsize=size, color="#555555",
            fontfamily='monospace', fontweight='bold' if bold else 'normal',
            ha='left', va='bottom', zorder=10)

def tb_value(x, y, txt, size=7.5, bold=False):
    ax.text(tb_x+x, tb_y+y, txt, fontsize=size, color=NOTE,
            fontfamily='monospace', fontweight='bold' if bold else 'normal',
            ha='left', va='center', zorder=10)

# Row dividers
for yr in [0.38, 0.72, 1.08, 1.44, 1.80]:
    tb_rule(yr)
# Vertical dividers
tb_vrule(1.6,  0, 2.0)
tb_vrule(3.3,  0, 2.0)
tb_vrule(4.4,  0, 1.44)
tb_vrule(5.1,  0, 1.08)

# Row 1 (top): company + part name
ax.text(tb_x+tb_w/2, tb_y+1.92, "ARIA-OS AUTONOMOUS ENGINEERING",
    ha='center', va='top', fontsize=7, fontweight='bold', color=NOTE,
    fontfamily='monospace', zorder=10)
ax.text(tb_x+tb_w/2, tb_y+1.62, "TURBOPUMP HOUSING ASSEMBLY",
    ha='center', va='center', fontsize=11, fontweight='bold', color=NOTE,
    fontfamily='monospace', zorder=10)

# Row 2
tb_label(0.05, 0.96+0.08, "DWG NO.", 4.5)
tb_value(0.05, 0.96+0.28, "TP-HSG-001", 8.5, True)
tb_label(1.65, 0.96+0.08, "SHEET", 4.5)
tb_value(1.65, 0.96+0.28, "1 OF 1", 7.5)
tb_label(3.35, 0.96+0.08, "SCALE", 4.5)
tb_value(3.35, 0.96+0.28, "1:2", 7.5)
tb_label(4.45, 0.96+0.08, "SIZE", 4.5)
tb_value(4.45, 0.96+0.28, "B", 7.5)

# Row 3
tb_label(0.05, 0.62+0.06, "MATERIAL", 4.5)
tb_value(0.05, 0.62+0.22, "AL 6061-T6 ASTM B209", 6.5)
tb_label(3.35, 0.62+0.06, "FINISH", 4.5)
tb_value(3.35, 0.62+0.22, "TYPE II ANODIZE", 6.5)

# Row 4
tb_label(0.05, 0.26+0.06, "DRAWN", 4.5)
tb_value(0.05, 0.26+0.22, "ARIA-OS", 6.5)
tb_label(1.65, 0.26+0.06, "DATE", 4.5)
tb_value(1.65, 0.26+0.22, "2026-04-10", 6.5)
tb_label(3.35, 0.26+0.06, "REV", 4.5)
tb_value(3.35, 0.26+0.22, "A", 7.5, True)
tb_label(4.45, 0.26+0.06, "APVD", 4.5)
tb_value(4.45, 0.26+0.22, "--", 6.5)

# Row 0 (bottom): tolerance block
ax.text(tb_x+0.05, tb_y+0.20, "UNLESS OTHERWISE SPECIFIED:", fontsize=4.5,
    color="#666666", fontfamily='monospace', va='top', zorder=10)
ax.text(tb_x+0.05, tb_y+0.14, "TOLERANCES: LINEAR ±0.10  ANGULAR ±0.5°",
    fontsize=4.5, color="#666666", fontfamily='monospace', va='top', zorder=10)
ax.text(tb_x+0.05, tb_y+0.07, "SURFACE FINISH: Ra 3.2μm  BREAK SHARP EDGES 0.2×45°",
    fontsize=4.5, color="#666666", fontfamily='monospace', va='top', zorder=10)

# Third-angle projection symbol (small circles in title block)
sym_cx, sym_cy = tb_x+4.85, tb_y+0.80
ax.add_patch(patches.Circle((sym_cx-0.08, sym_cy), 0.055, fill=False, edgecolor=NOTE, lw=0.8, zorder=10))
ax.add_patch(patches.Circle((sym_cx+0.08, sym_cy), 0.055, fill=False, edgecolor=NOTE, lw=0.8, zorder=10))
ax.add_patch(patches.Circle((sym_cx+0.08, sym_cy), 0.022, facecolor=NOTE, edgecolor="none", zorder=10))
ax.text(sym_cx, sym_cy-0.11, "3rd ANGLE", ha='center', va='top', fontsize=3.5,
    color="#555555", fontfamily='monospace', zorder=10)

# ── Revision block (top-right) ────────────────────────────────────────────────
rb_x, rb_y = BR-5.8, BT-0.55
for col_x, header in [(rb_x+0.05,"REV"), (rb_x+0.35,"DESCRIPTION"), (rb_x+3.5,"DATE"), (rb_x+4.4,"APVD")]:
    ax.text(col_x, rb_y+0.45, header, fontsize=4.5, color="#555555",
            fontfamily='monospace', ha='left', zorder=10)
ax.plot([rb_x, tb_x+tb_w], [rb_y+0.38, rb_y+0.38], color=VIS, lw=0.6, zorder=9)
ax.add_patch(patches.Rectangle((rb_x, rb_y), tb_w, 0.55,
    fill=False, edgecolor=VIS, linewidth=0.8, zorder=9))
# Rev A entry
ax.text(rb_x+0.05, rb_y+0.20, "A", fontsize=5, color=NOTE, fontfamily='monospace', zorder=10)
ax.text(rb_x+0.35, rb_y+0.20, "INITIAL RELEASE", fontsize=5, color=NOTE, fontfamily='monospace', zorder=10)
ax.text(rb_x+3.5,  rb_y+0.20, "2026-04-10", fontsize=5, color=NOTE, fontfamily='monospace', zorder=10)

# ── View helper functions ─────────────────────────────────────────────────────
def dim_h(ax_, x1, x2, y, label, offset=0.18, tick_h=0.08, fs=6):
    """Horizontal dimension line."""
    ax_.annotate("", xy=(x2, y-offset), xytext=(x1, y-offset),
        arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.6, mutation_scale=5))
    ax_.plot([x1, x1], [y, y-offset-tick_h], color=DIM, lw=0.5)
    ax_.plot([x2, x2], [y, y-offset-tick_h], color=DIM, lw=0.5)
    ax_.text((x1+x2)/2, y-offset-0.05, label, ha='center', va='top',
             fontsize=fs, color=DIM, fontfamily='monospace')

def dim_v(ax_, x, y1, y2, label, offset=0.22, tick_w=0.08, fs=6):
    """Vertical dimension line."""
    ax_.annotate("", xy=(x+offset, y2), xytext=(x+offset, y1),
        arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.6, mutation_scale=5))
    ax_.plot([x, x+offset+tick_w], [y1, y1], color=DIM, lw=0.5)
    ax_.plot([x, x+offset+tick_w], [y2, y2], color=DIM, lw=0.5)
    ax_.text(x+offset+0.06, (y1+y2)/2, label, ha='left', va='center',
             fontsize=fs, color=DIM, fontfamily='monospace', rotation=90)

def center_cross(ax_, cx, cy, r=0.25):
    """Centerline cross."""
    ax_.plot([cx-r, cx+r], [cy, cy], color=CL, lw=0.5, dashes=[5,2], zorder=3)
    ax_.plot([cx, cx], [cy-r, cy+r], color=CL, lw=0.5, dashes=[5,2], zorder=3)

def gdt_frame(ax_, x, y, sym, tol, ref="", w=0.55, h=0.14):
    """GD&T feature control frame."""
    # sym box
    ax_.add_patch(patches.Rectangle((x, y-h/2), h, h,
        fill=True, facecolor="white", edgecolor=DIM, lw=0.5, zorder=12))
    ax_.text(x+h/2, y, sym, ha='center', va='center', fontsize=7, color=DIM, zorder=13)
    # tol box
    tol_w = w
    ax_.add_patch(patches.Rectangle((x+h, y-h/2), tol_w, h,
        fill=True, facecolor="white", edgecolor=DIM, lw=0.5, zorder=12))
    ax_.text(x+h+tol_w/2, y, tol, ha='center', va='center', fontsize=6, color=DIM,
             fontfamily='monospace', zorder=13)
    # datum box
    if ref:
        ax_.add_patch(patches.Rectangle((x+h+tol_w, y-h/2), h, h,
            fill=True, facecolor="white", edgecolor=DIM, lw=0.5, zorder=12))
        ax_.text(x+h+tol_w+h/2, y, ref, ha='center', va='center',
                 fontsize=6.5, color=DIM, fontweight='bold', zorder=13)

def surf_finish(ax_, x, y, ra="1.6"):
    """Surface finish checkmark symbol."""
    ax_.text(x, y, f"\u2713Ra{ra}", fontsize=5.5, color=DIM, ha='center', va='bottom',
             fontfamily='monospace', zorder=12)


# ────────────────────────────────────────────────────────────────────────────
# DIMENSIONS (in inches on the drawing, scale 1:2 so 1mm = 0.019685")
# Actual part: OD=160mm, H=180mm, FlangeOD=200mm, bore=144mm, wall=8mm
# Scale factor: 1 drawing-inch = ~2*25.4 mm = 50.8mm  (1:2 scale)
# So 160mm -> 3.15", 180mm->3.54", 200mm->3.94", 144mm->2.83"
sc = 1/50.8    # mm to drawing-inches

OD   = 160 * sc   # 3.15"
H    = 180 * sc   # 3.54"
FOD  = 200 * sc   # 3.94"
BORE = 144 * sc   # 2.83"
WALL =   8 * sc   # 0.16"
FH   =  20 * sc   # 0.39" flange height
PORT_H = 72 * sc  # height of port center from bottom

# ── FRONT VIEW (XZ plane) ─────────────────────────────────────────────────────
# Center at (4.5, 5.8)
FV_CX, FV_BY = 4.5, 3.8
FV_TY = FV_BY + H
FV_LX = FV_CX - OD/2
FV_RX = FV_CX + OD/2
FL_LX = FV_CX - FOD/2
FL_RX = FV_CX + FOD/2

ax_front = ax   # draw directly on main axes

ax.set_title("", fontsize=8)

# Title under view
ax.text(FV_CX, FV_BY-0.28, "FRONT VIEW", ha='center', fontsize=7,
    color="#333333", fontfamily='monospace')

# Flange rectangle
ax.add_patch(patches.Rectangle((FL_LX, FV_BY-FH), FOD, FH,
    facecolor=FILL, edgecolor=VIS, lw=1.0, zorder=4))

# Main body rectangle
ax.add_patch(patches.Rectangle((FV_LX, FV_BY), OD, H,
    facecolor="white", edgecolor=VIS, lw=1.2, zorder=4))

# Slight taper at top (4mm each side)
taper = 4*sc
ax.plot([FV_LX, FV_LX+taper], [FV_TY, FV_TY], color=VIS, lw=1.2, zorder=5)
ax.plot([FV_RX, FV_RX-taper], [FV_TY, FV_TY], color=VIS, lw=1.2, zorder=5)

# Top cap arc
ax.add_patch(Arc((FV_CX, FV_TY), OD-2*taper, 0.08, theta1=0, theta2=180,
    edgecolor=VIS, lw=1.0, zorder=5))

# Bore (hidden line inside body)
BOR_LX = FV_CX - BORE/2
BOR_RX = FV_CX + BORE/2
ax.plot([BOR_LX, BOR_LX], [FV_BY, FV_TY], color=HID, lw=0.6, dashes=[4,2], zorder=3)
ax.plot([BOR_RX, BOR_RX], [FV_BY, FV_TY], color=HID, lw=0.6, dashes=[4,2], zorder=3)

# Side port stub
PORT_R = 16*sc
PORT_LEN = 30*sc
PORT_CY = FV_BY + PORT_H
ax.add_patch(patches.Rectangle((FV_RX, PORT_CY-PORT_R), PORT_LEN, PORT_R*2,
    facecolor="white", edgecolor=VIS, lw=1.0, zorder=4))
ax.plot([FV_RX+PORT_LEN, FV_RX+PORT_LEN], [PORT_CY-PORT_R*0.5, PORT_CY+PORT_R*0.5],
    color=VIS, lw=1.0, zorder=5)

# Bolt holes visible on flange (2 in front view)
for bx_off in [-FOD*0.3, FOD*0.3]:
    ax.add_patch(patches.Circle((FV_CX+bx_off, FV_BY-FH/2), 4*sc,
        facecolor="white", edgecolor=VIS, lw=0.8, zorder=5))

# Section cut line A-A
scy = FV_BY + H*0.55
ax.plot([FV_LX-0.5, FL_LX-0.1], [scy, scy], color=DIM, lw=1.0, dashes=[8,3,2,3], zorder=6)
ax.plot([FV_RX+PORT_LEN+0.1, FV_RX+PORT_LEN+0.5], [scy, scy], color=DIM, lw=1.0, dashes=[8,3,2,3], zorder=6)
ax.text(FV_LX-0.6, scy, "A", ha='center', va='center', fontsize=8, fontweight='bold', color=DIM, zorder=7)
ax.text(FV_RX+PORT_LEN+0.7, scy, "A", ha='center', va='center', fontsize=8, fontweight='bold', color=DIM, zorder=7)
# Arrow heads on section line
ax.annotate("", xy=(FV_LX-0.2, scy-0.15), xytext=(FV_LX-0.2, scy+0.01),
    arrowprops=dict(arrowstyle='-|>', color=DIM, lw=0.8, mutation_scale=6), zorder=7)
ax.annotate("", xy=(FV_RX+PORT_LEN+0.2, scy-0.15), xytext=(FV_RX+PORT_LEN+0.2, scy+0.01),
    arrowprops=dict(arrowstyle='-|>', color=DIM, lw=0.8, mutation_scale=6), zorder=7)

# Centerlines
ax.plot([FV_CX, FV_CX], [FV_BY-FH-0.2, FV_TY+0.15], color=CL, lw=0.5, dashes=[6,2,1,2], zorder=3)
ax.plot([FV_LX-0.3, FL_RX+0.25], [FV_BY-FH/2, FV_BY-FH/2], color=CL, lw=0.5, dashes=[6,2,1,2], zorder=3)

# Datum A triangle (bore centerline)
ax.text(FV_CX+0.05, FV_TY+0.18, "A", ha='center', va='bottom', fontsize=7,
    fontweight='bold', color=DIM, zorder=10)
ax.add_patch(patches.FancyBboxPatch((FV_CX-0.07, FV_TY+0.14), 0.14, 0.10,
    boxstyle="square,pad=0", facecolor="white", edgecolor=DIM, lw=0.7, zorder=9))

# Dimensions
dim_h(ax, FV_LX, FV_RX, FV_BY, "\u00d8160", offset=0.28)
dim_h(ax, FL_LX, FL_RX, FV_BY-FH, "\u00d8200 (FLANGE)", offset=0.45)
dim_v(ax, FV_RX, FV_BY, FV_TY, "180", offset=0.35)
dim_v(ax, FV_RX, FV_BY-FH, FV_BY, "20", offset=0.58)
dim_h(ax, BOR_LX, BOR_RX, FV_TY, "\u00d8144 BORE", offset=-0.22)

# GD&T on front view
gdt_frame(ax, FV_CX-0.28, FV_BY+H*0.15, "\u25ce", "\u00d80.10", "A", w=0.60)
ax.annotate("", xy=(FV_CX-0.28, FV_BY+H*0.15), xytext=(BOR_LX-0.05, FV_BY+H*0.15),
    arrowprops=dict(arrowstyle='->', color=DIM, lw=0.5, mutation_scale=5), zorder=12)

# Surface finish callouts
surf_finish(ax, FV_RX+0.12, FV_BY+H*0.75, "3.2")
surf_finish(ax, FV_CX, FV_TY+0.07, "1.6")

# ── TOP VIEW (XY plane) ───────────────────────────────────────────────────────
TV_CX, TV_CY = 4.5, 8.1

ax.text(TV_CX, TV_CY-FOD/2-0.22, "TOP VIEW", ha='center', fontsize=7,
    color="#333333", fontfamily='monospace')

# OD circle
ax.add_patch(patches.Circle((TV_CX, TV_CY), OD/2,
    facecolor="white", edgecolor=VIS, lw=1.2, zorder=4))
# Bore
ax.add_patch(patches.Circle((TV_CX, TV_CY), BORE/2,
    facecolor="#f0f0f0", edgecolor=VIS, lw=1.0, zorder=4))
# Flange OD (phantom line)
ax.add_patch(patches.Circle((TV_CX, TV_CY), FOD/2,
    fill=False, edgecolor=HID, lw=0.6, linestyle='dashed', zorder=3))

# Bolt hole PCD circle
PCD_R = 80*sc  # 80mm radius = 160mm PCD
ax.add_patch(patches.Circle((TV_CX, TV_CY), PCD_R,
    fill=False, edgecolor=CL, lw=0.4, linestyle=(0,(5,3)), zorder=3))

# 4 bolt holes at 45, 135, 225, 315
for ang in [45, 135, 225, 315]:
    a = np.radians(ang)
    bx, by = TV_CX + PCD_R*np.cos(a), TV_CY + PCD_R*np.sin(a)
    ax.add_patch(patches.Circle((bx, by), 4*sc,
        facecolor="white", edgecolor=VIS, lw=0.8, zorder=5))
    center_cross(ax, bx, by, r=0.08)

# Side port from top
ax.add_patch(patches.Rectangle((TV_CX+OD/2, TV_CY-PORT_R), PORT_LEN, PORT_R*2,
    facecolor="white", edgecolor=VIS, lw=1.0, zorder=4))
ax.add_patch(patches.Circle((TV_CX+OD/2+PORT_LEN*0.5, TV_CY), PORT_R*0.55,
    facecolor="#f0f0f0", edgecolor=VIS, lw=0.8, zorder=5))

# Centerlines
center_cross(ax, TV_CX, TV_CY, r=OD/2+0.2)

# Dimensions
ax.annotate("", xy=(TV_CX+OD/2, TV_CY+OD*0.55),
    xytext=(TV_CX-OD/2, TV_CY+OD*0.55),
    arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.6, mutation_scale=5))
ax.text(TV_CX, TV_CY+OD*0.55+0.06, "\u00d8160", ha='center', va='bottom',
    fontsize=6, color=DIM, fontfamily='monospace')

# PCD annotation
ax.annotate("", xy=(TV_CX+PCD_R, TV_CY), xytext=(TV_CX, TV_CY),
    arrowprops=dict(arrowstyle='<|-', color=CL, lw=0.5, mutation_scale=4))
ax.text(TV_CX+PCD_R*0.5, TV_CY+0.06, "R80 PCD", ha='center', va='bottom',
    fontsize=5, color=CL, fontfamily='monospace')

# GD&T - flatness of flange face
gdt_frame(ax, TV_CX+FOD/2+0.12, TV_CY, "\u25b1", "0.05", w=0.50)
ax.plot([TV_CX+FOD/2, TV_CX+FOD/2+0.12], [TV_CY, TV_CY], color=DIM, lw=0.5)

# Bolt position GD&T
gdt_frame(ax, TV_CX+PCD_R+0.05, TV_CY-PCD_R*0.6, "\u2295", "\u00d80.30 M", "A", w=0.70)

# ── SECTION A-A (right side) ──────────────────────────────────────────────────
SA_CX, SA_BY = 11.0, 3.8
SA_TY = SA_BY + H
SA_LX = SA_CX - OD/2
SA_RX = SA_CX + OD/2

ax.text(SA_CX, SA_BY-0.28, "SECTION A-A", ha='center', fontsize=7,
    color="#333333", fontfamily='monospace')
ax.text(SA_CX, SA_BY-0.45, "(FULL SECTION)", ha='center', fontsize=5.5,
    color="#555555", fontfamily='monospace')

# Outer walls (cut faces - hatched)
wall_w = WALL
# Left wall
ax.add_patch(patches.Rectangle((SA_LX, SA_BY), wall_w, H,
    facecolor=FILL, edgecolor=VIS, lw=1.0, zorder=4))
ax.add_patch(patches.Rectangle((SA_LX, SA_BY), wall_w, H,
    facecolor="none", edgecolor=VIS, lw=0, hatch='/////', zorder=4))
# Right wall
ax.add_patch(patches.Rectangle((SA_RX-wall_w, SA_BY), wall_w, H,
    facecolor=FILL, edgecolor=VIS, lw=1.0, zorder=4))
ax.add_patch(patches.Rectangle((SA_RX-wall_w, SA_BY), wall_w, H,
    facecolor="none", edgecolor=VIS, lw=0, hatch='/////', zorder=4))
# Interior (hollow bore)
ax.add_patch(patches.Rectangle((SA_LX+wall_w, SA_BY), BORE, H,
    facecolor="white", edgecolor="none", zorder=5))
# Bottom (open - no bottom plate)
ax.plot([SA_LX, SA_RX], [SA_BY, SA_BY], color=VIS, lw=1.0, zorder=6)
ax.plot([SA_LX, SA_RX], [SA_TY, SA_TY], color=VIS, lw=1.0, zorder=6)
ax.plot([SA_LX, SA_LX], [SA_BY, SA_TY], color=VIS, lw=1.2, zorder=6)
ax.plot([SA_RX, SA_RX], [SA_BY, SA_TY], color=VIS, lw=1.2, zorder=6)
ax.plot([SA_LX+wall_w, SA_LX+wall_w], [SA_BY, SA_TY], color=VIS, lw=0.8, zorder=6)
ax.plot([SA_RX-wall_w, SA_RX-wall_w], [SA_BY, SA_TY], color=VIS, lw=0.8, zorder=6)

# Flange (cut)
FL_SA_LX = SA_CX - FOD/2
FL_SA_RX = SA_CX + FOD/2
ax.add_patch(patches.Rectangle((FL_SA_LX, SA_BY-FH), FOD, FH,
    facecolor=FILL, edgecolor=VIS, lw=1.0, hatch='/////', zorder=4))

# Port stub (half section)
port_cy_sa = SA_BY + PORT_H
ax.add_patch(patches.Rectangle((SA_RX, port_cy_sa-PORT_R), PORT_LEN, PORT_R*2,
    facecolor=FILL, edgecolor=VIS, lw=1.0, hatch='////', zorder=4))
ax.add_patch(patches.Rectangle((SA_RX, port_cy_sa-PORT_R*0.5), PORT_LEN, PORT_R,
    facecolor="white", edgecolor=VIS, lw=0.8, zorder=5))

# Centerline
ax.plot([SA_CX, SA_CX], [SA_BY-FH-0.2, SA_TY+0.15], color=CL, lw=0.5, dashes=[6,2,1,2], zorder=3)

# Wall thickness dimension
dim_h(ax, SA_LX, SA_LX+wall_w, SA_BY+H*0.5, "8 WALL", offset=-0.18, tick_h=0.06)

# Section view datum
ax.text(SA_CX, SA_TY+0.18, "(DATUM A)", ha='center', va='bottom', fontsize=5.5,
    color=DIM, fontfamily='monospace')

# ── RIGHT SIDE VIEW (YZ plane) ────────────────────────────────────────────────
SV_CX, SV_CY = 9.0, 7.8

ax.text(SV_CX, SV_CY-OD/2-0.22, "RIGHT SIDE VIEW", ha='center', fontsize=7,
    color="#333333", fontfamily='monospace')

# OD
ax.add_patch(patches.Circle((SV_CX, SV_CY), OD/2,
    facecolor="white", edgecolor=VIS, lw=1.2, zorder=4))
ax.add_patch(patches.Circle((SV_CX, SV_CY), BORE/2,
    facecolor="#f0f0f0", edgecolor=HID, lw=0.6, linestyle='dashed', zorder=3))
ax.add_patch(patches.Circle((SV_CX, SV_CY), FOD/2,
    fill=False, edgecolor=HID, lw=0.6, linestyle='dashed', zorder=3))

# Port (hidden - coming from left)
ax.add_patch(patches.Circle((SV_CX-OD/2, SV_CY), PORT_R,
    fill=False, edgecolor=HID, lw=0.6, linestyle='dashed', zorder=3))

center_cross(ax, SV_CX, SV_CY, r=OD/2+0.18)

# ── GENERAL NOTES ─────────────────────────────────────────────────────────────
notes_x, notes_y = BL+0.1, BB+2.15
ax.text(notes_x, notes_y, "GENERAL NOTES:", fontsize=6, fontweight='bold',
    color=NOTE, fontfamily='monospace')
notes = [
    "1. ALL DIMENSIONS IN MILLIMETRES UNLESS OTHERWISE STATED.",
    "2. MATERIAL: AL 6061-T6 PER ASTM B209. BILLET OR FORGING.",
    "3. HEAT TREAT: T6 CONDITION. HB 95-100.",
    "4. ANODIZE: TYPE II CLEAR PER MIL-A-8625F.",
    "5. ALL THREADED HOLES: CLASS 6H. LIGHTLY OIL AFTER ANODIZE.",
    "6. DEBURR ALL EDGES TO 0.2×45°. REMOVE ALL BURRS AND CHIPS.",
    "7. INSPECT PER ARIA-OS ITP-001 REV A.",
]
for i, note in enumerate(notes):
    ax.text(notes_x, notes_y - 0.18*(i+1), note, fontsize=5.5,
        color=NOTE, fontfamily='monospace')

# ── Save ──────────────────────────────────────────────────────────────────────
plt.savefig(str(OUT / "eng_drawing_turbopump.png"), dpi=DPI,
    bbox_inches='tight', facecolor=BG)
plt.close()
print(f"Engineering drawing v2: {(OUT/'eng_drawing_turbopump.png').stat().st_size:,} bytes")
