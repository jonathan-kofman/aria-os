"""
ASME Y14.5 engineering drawing of turbopump housing — v3.
3-view layout: FRONT VIEW + TOP VIEW + SECTION A-A.
Replaces RIGHT SIDE VIEW with DETAIL VIEW (bolt hole callout).
Fixes view overlap, improves dimensions, adds proper GD&T.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import Arc, FancyArrowPatch
from pathlib import Path

OUT = Path("outputs/gallery_renders")
W, H = 17.0, 11.0  # ANSI B landscape
DPI = 120

BG   = "white"
VIS  = "#0d0d0d"
HID  = "#888888"
DIM  = "#0d0d0d"
CL   = "#808080"
FILL = "#e2e8ee"   # section hatch fill
NOTE = "#111111"

fig = plt.figure(figsize=(W, H), dpi=DPI)
fig.patch.set_facecolor(BG)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W); ax.set_ylim(0, H)
ax.axis('off'); ax.set_facecolor(BG)

# ── Sheet border ──────────────────────────────────────────────────────────
BL, BR, BB, BT = 0.75, 16.50, 0.50, 10.50
ax.add_patch(patches.Rectangle((BL, BB), BR-BL, BT-BB,
    fill=False, edgecolor=VIS, linewidth=2.5, zorder=10))

# Zone references
for i, ltr in enumerate("ABCDEF"):
    y = BT - (i+0.5)*(BT-BB)/6
    ax.text(BL-0.30, y, ltr, ha='center', va='center', fontsize=6, color=NOTE,
            fontfamily='monospace')
    ax.text(BR+0.16, y, ltr, ha='center', va='center', fontsize=6, color=NOTE,
            fontfamily='monospace')
for i in range(1, 9):
    x = BL + (i-0.5)*(BR-BL)/8
    ax.text(x, BT+0.16, str(i), ha='center', va='center', fontsize=6, color=NOTE,
            fontfamily='monospace')
    ax.text(x, BB-0.20, str(i), ha='center', va='center', fontsize=6, color=NOTE,
            fontfamily='monospace')

# ── Title block ───────────────────────────────────────────────────────────
tb_x, tb_w, tb_h = BR-5.8, 5.8, 2.0
ax.add_patch(patches.Rectangle((tb_x, BB), tb_w, tb_h,
    facecolor="#f8f9fa", edgecolor=VIS, linewidth=1.2, zorder=8))

def hline(y):
    ax.plot([tb_x, tb_x+tb_w], [BB+y, BB+y], color=VIS, lw=0.6, zorder=9)
def vline(x, y0, y1):
    ax.plot([tb_x+x]*2, [BB+y0, BB+y1], color=VIS, lw=0.6, zorder=9)
def lbl(x, y, t, fs=4.5):
    ax.text(tb_x+x, BB+y, t, fontsize=fs, color="#555", fontfamily='monospace',
            ha='left', va='bottom', zorder=10)
def val(x, y, t, fs=7.5, bold=False):
    ax.text(tb_x+x, BB+y, t, fontsize=fs, color=NOTE, fontfamily='monospace',
            fontweight='bold' if bold else 'normal', ha='left', va='center', zorder=10)

for yr in [0.38, 0.72, 1.08, 1.44, 1.80]:
    hline(yr)
vline(1.60, 0, 2.0); vline(3.30, 0, 2.0)
vline(4.40, 0, 1.44); vline(5.10, 0, 1.08)

# Company / part name
ax.text(tb_x+tb_w/2, BB+1.92, "ARIA-OS AUTONOMOUS ENGINEERING",
    ha='center', va='top', fontsize=6.5, fontweight='bold', color=NOTE,
    fontfamily='monospace', zorder=10)
ax.text(tb_x+tb_w/2, BB+1.60, "TURBOPUMP HOUSING ASSEMBLY",
    ha='center', va='center', fontsize=10.5, fontweight='bold', color=NOTE,
    fontfamily='monospace', zorder=10)

lbl(0.05, 1.04, "DWG NO.", 4.5); val(0.05, 1.18, "TP-HSG-001", 8.5, True)
lbl(1.65, 1.04, "SHEET"); val(1.65, 1.18, "1 OF 1")
lbl(3.35, 1.04, "SCALE"); val(3.35, 1.18, "1:2")
lbl(4.45, 1.04, "SIZE"); val(4.45, 1.18, "B")

lbl(0.05, 0.68, "MATERIAL"); val(0.05, 0.82, "AL 6061-T6 ASTM B209", 6.5)
lbl(3.35, 0.68, "FINISH");   val(3.35, 0.82, "TYPE II ANODIZE", 6.5)

lbl(0.05, 0.30, "DRAWN"); val(0.05, 0.44, "ARIA-OS", 6.5)
lbl(1.65, 0.30, "DATE");  val(1.65, 0.44, "2026-04-10", 6.5)
lbl(3.35, 0.30, "REV");   val(3.35, 0.44, "A", 8, True)
lbl(4.45, 0.30, "APVD");  val(4.45, 0.44, "--", 6.5)

# Tolerance block
for i, t in enumerate(["UNLESS OTHERWISE SPECIFIED:",
                        "TOLERANCES: LINEAR ±0.10  ANGULAR ±0.5°",
                        "SURFACE FINISH: Ra 3.2  BREAK EDGES 0.2×45°"]):
    ax.text(tb_x+0.05, BB+0.22-i*0.07, t, fontsize=4, color="#666",
            fontfamily='monospace', va='top', zorder=10)

# 3rd-angle projection symbol
sym_cx, sym_cy = tb_x+4.85, BB+0.72
ax.add_patch(patches.Circle((sym_cx-0.09, sym_cy), 0.06, fill=False,
             edgecolor=NOTE, lw=0.8, zorder=10))
ax.add_patch(patches.Circle((sym_cx+0.09, sym_cy), 0.06, fill=False,
             edgecolor=NOTE, lw=0.8, zorder=10))
ax.add_patch(patches.Circle((sym_cx+0.09, sym_cy), 0.025, facecolor=NOTE,
             edgecolor="none", zorder=10))
ax.text(sym_cx, sym_cy-0.12, "3rd ANGLE", ha='center', va='top',
        fontsize=3.5, color="#555", fontfamily='monospace', zorder=10)

# ── Revision block ────────────────────────────────────────────────────────
rb_x, rb_y = BR-5.8, BT-0.60
ax.add_patch(patches.Rectangle((rb_x, rb_y), tb_w, 0.60,
    fill=False, edgecolor=VIS, lw=0.8, zorder=9))
ax.plot([rb_x, rb_x+tb_w], [rb_y+0.42]*2, color=VIS, lw=0.6, zorder=9)
for x_col, hdr in [(rb_x+0.05,"REV"),(rb_x+0.35,"DESCRIPTION"),(rb_x+3.6,"DATE"),(rb_x+4.5,"APVD")]:
    ax.text(x_col, rb_y+0.50, hdr, fontsize=4.5, color="#555",
            fontfamily='monospace', ha='left', va='center', zorder=10)
ax.text(rb_x+0.05, rb_y+0.18, "A", fontsize=5, color=NOTE, fontfamily='monospace', zorder=10)
ax.text(rb_x+0.35, rb_y+0.18, "INITIAL RELEASE", fontsize=5, color=NOTE, fontfamily='monospace', zorder=10)
ax.text(rb_x+3.60, rb_y+0.18, "2026-04-10", fontsize=5, color=NOTE, fontfamily='monospace', zorder=10)

# ── Scale / helpers ───────────────────────────────────────────────────────
sc   = 1/50.8     # 1:2 scale: mm → drawing inches
OD   = 160*sc     # 3.150"
H_   = 180*sc     # 3.543"
FOD  = 200*sc     # 3.937"
BORE = 144*sc     # 2.835"
WALL =   8*sc
FH   =  20*sc
PORT_R   = 16*sc
PORT_LEN = 30*sc
PORT_H   = 80*sc   # port centre from base
PCD_R    = 80*sc

def dim_h(x1, x2, y, label, off=0.20, fs=5.5):
    ax.annotate("", xy=(x2, y-off), xytext=(x1, y-off),
        arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.6, mutation_scale=5))
    ax.plot([x1,x1],[y, y-off-0.06], color=DIM, lw=0.5)
    ax.plot([x2,x2],[y, y-off-0.06], color=DIM, lw=0.5)
    ax.text((x1+x2)/2, y-off-0.06, label, ha='center', va='top',
            fontsize=fs, color=DIM, fontfamily='monospace')

def dim_v(x, y1, y2, label, off=0.25, fs=5.5):
    ax.annotate("", xy=(x+off, y2), xytext=(x+off, y1),
        arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.6, mutation_scale=5))
    ax.plot([x, x+off+0.06],[y1,y1], color=DIM, lw=0.5)
    ax.plot([x, x+off+0.06],[y2,y2], color=DIM, lw=0.5)
    ax.text(x+off+0.06, (y1+y2)/2, label, ha='left', va='center',
            fontsize=fs, color=DIM, fontfamily='monospace', rotation=90)

def clinesH(cx, cy, r):
    ax.plot([cx-r, cx+r],[cy,cy], color=CL, lw=0.5, dashes=[6,2,1,2])

def clinesV(cx, cy, r):
    ax.plot([cx,cx],[cy-r, cy+r], color=CL, lw=0.5, dashes=[6,2,1,2])

def gdt(x, y, sym, tol, ref="", w=0.55):
    h_box = 0.14
    ax.add_patch(patches.Rectangle((x, y-h_box/2), h_box, h_box,
        facecolor="white", edgecolor=DIM, lw=0.5, zorder=12))
    ax.text(x+h_box/2, y, sym, ha='center', va='center', fontsize=7, color=DIM, zorder=13)
    ax.add_patch(patches.Rectangle((x+h_box, y-h_box/2), w, h_box,
        facecolor="white", edgecolor=DIM, lw=0.5, zorder=12))
    ax.text(x+h_box+w/2, y, tol, ha='center', va='center', fontsize=5.5,
            color=DIM, fontfamily='monospace', zorder=13)
    if ref:
        ax.add_patch(patches.Rectangle((x+h_box+w, y-h_box/2), h_box, h_box,
            facecolor="white", edgecolor=DIM, lw=0.5, zorder=12))
        ax.text(x+h_box+w+h_box/2, y, ref, ha='center', va='center',
                fontsize=6, color=DIM, fontweight='bold', zorder=13)


# ═══════════════════════════════════════════════════════════════════════════
# FRONT VIEW  (lower-left, centred at x=3.8, y=5.2)
# ═══════════════════════════════════════════════════════════════════════════
FV_CX, FV_BY = 3.80, 3.60
FV_TY = FV_BY + H_
FV_LX = FV_CX - OD/2
FV_RX = FV_CX + OD/2
FL_LX = FV_CX - FOD/2
FL_RX = FV_CX + FOD/2

ax.text(FV_CX, FV_BY-0.35, "FRONT VIEW", ha='center', fontsize=7,
        color="#333", fontfamily='monospace')

# Flange rectangle with hatch
ax.add_patch(patches.Rectangle((FL_LX, FV_BY-FH), FOD, FH,
    facecolor=FILL, edgecolor=VIS, lw=1.0, zorder=4))
ax.add_patch(patches.Rectangle((FL_LX, FV_BY-FH), FOD, FH,
    facecolor="none", edgecolor=VIS, lw=0, hatch='+++', alpha=0.15, zorder=4))

# Main cylinder body
ax.add_patch(patches.Rectangle((FV_LX, FV_BY), OD, H_,
    facecolor="white", edgecolor=VIS, lw=1.3, zorder=4))

# Top dome
ax.add_patch(Arc((FV_CX, FV_TY), OD*0.92, 0.10, theta1=0, theta2=180,
    edgecolor=VIS, lw=1.0, zorder=5))

# Bore hidden lines
for x_ in [FV_CX-BORE/2, FV_CX+BORE/2]:
    ax.plot([x_,x_],[FV_BY, FV_TY], color=HID, lw=0.7, dashes=[4,2], zorder=3)

# Side port stub
PC_Y = FV_BY + PORT_H
ax.add_patch(patches.Rectangle((FV_RX, PC_Y-PORT_R), PORT_LEN, PORT_R*2,
    facecolor="white", edgecolor=VIS, lw=1.0, zorder=4))
# Port end cap
ax.plot([FV_RX+PORT_LEN]*2, [PC_Y-PORT_R*0.55, PC_Y+PORT_R*0.55],
        color=VIS, lw=1.2, zorder=5)
# Port bore hidden
ax.plot([FV_RX, FV_RX+PORT_LEN], [PC_Y, PC_Y], color=HID, lw=0.5,
        dashes=[3,2], zorder=3)

# 2 bolt holes visible on flange
for bx_off in [-FOD*0.30, +FOD*0.30]:
    ax.add_patch(patches.Circle((FV_CX+bx_off, FV_BY-FH/2), 4.5*sc,
        facecolor="white", edgecolor=VIS, lw=0.8, zorder=5))

# Section line A-A
scy = FV_BY + H_*0.55
for x_side, dir_ in [(FL_LX-0.55, +1), (FV_RX+PORT_LEN+0.55, -1)]:
    ax.plot([x_side-0.0, x_side+dir_*0.40], [scy,scy],
            color=DIM, lw=1.0, dashes=[8,3,2,3], zorder=6)
    ax.text(x_side-dir_*0.15, scy, "A", ha='center', va='center',
            fontsize=9, fontweight='bold', color=DIM, zorder=7)
    ax.annotate("", xy=(x_side-dir_*0.08, scy-0.18),
        xytext=(x_side-dir_*0.08, scy+0.05),
        arrowprops=dict(arrowstyle='-|>', color=DIM, lw=0.8, mutation_scale=6),
        zorder=7)

# Centerlines
clinesV(FV_CX, (FV_BY+FV_TY)/2, H_/2+0.25)
clinesH(FV_CX, FV_BY-FH/2, FL_RX-FL_LX)

# Datum A box (bore axis)
ax.add_patch(patches.FancyBboxPatch((FV_CX-0.09, FV_TY+0.16), 0.18, 0.14,
    boxstyle="square,pad=0", facecolor="white", edgecolor=DIM, lw=0.7, zorder=9))
ax.text(FV_CX, FV_TY+0.23, "A", ha='center', va='center', fontsize=7.5,
        color=DIM, zorder=10)

# Dimensions
dim_h(FV_LX, FV_RX, FV_BY, "\u00d8160", off=0.32)
dim_h(FL_LX, FL_RX, FV_BY-FH, "\u00d8200 PCD FLANGE", off=0.55)
dim_v(FV_RX, FV_BY, FV_TY, "180", off=0.42)
dim_v(FV_RX, FV_BY-FH, FV_BY, "20", off=0.68)
dim_h(FV_CX-BORE/2, FV_CX+BORE/2, FV_TY, "\u00d8144 THRU BORE", off=-0.25)

# GD&T
gdt(FV_CX-0.30, FV_BY+H_*0.22, "O", "\u00d80.10", "[A]", w=0.60)
ax.annotate("", xy=(FV_CX-0.30, FV_BY+H_*0.22),
    xytext=(FV_CX-BORE/2-0.05, FV_BY+H_*0.22),
    arrowprops=dict(arrowstyle='->', color=DIM, lw=0.5, mutation_scale=5), zorder=12)

# Surface finish
ax.text(FV_RX+0.10, FV_BY+H_*0.72, "\u2713Ra3.2", fontsize=5, color=DIM,
        fontfamily='monospace', zorder=12)
ax.text(FV_CX, FV_TY+0.09, "\u2713Ra1.6", fontsize=5, color=DIM,
        fontfamily='monospace', zorder=12)

# ═══════════════════════════════════════════════════════════════════════════
# TOP VIEW  (above front view, same x-centre, 3rd-angle: above)
# ═══════════════════════════════════════════════════════════════════════════
TV_CX = FV_CX
TV_CY = FV_TY + 0.80 + FOD/2   # gap of 0.80" between front and top
TVLIM = FOD/2 + 0.30

ax.text(TV_CX, TV_CY-TVLIM-0.28, "TOP VIEW", ha='center', fontsize=7,
        color="#333", fontfamily='monospace')

# OD circle
ax.add_patch(patches.Circle((TV_CX, TV_CY), OD/2,
    facecolor="white", edgecolor=VIS, lw=1.3, zorder=4))
# Bore circle
ax.add_patch(patches.Circle((TV_CX, TV_CY), BORE/2,
    facecolor="#f2f2f2", edgecolor=VIS, lw=1.0, zorder=4))
# Flange phantom circle (dashed)
ax.add_patch(patches.Circle((TV_CX, TV_CY), FOD/2,
    fill=False, edgecolor=HID, lw=0.6, linestyle='dashed', zorder=3))
# Bolt hole PCD phantom circle
ax.add_patch(patches.Circle((TV_CX, TV_CY), PCD_R,
    fill=False, edgecolor=CL, lw=0.4, linestyle=(0,(5,3)), zorder=3))

# 4 bolt holes at 45°/135°/225°/315°
for ang in [45, 135, 225, 315]:
    a = np.radians(ang)
    bx, by = TV_CX + PCD_R*np.cos(a), TV_CY + PCD_R*np.sin(a)
    ax.add_patch(patches.Circle((bx, by), 4.5*sc,
        facecolor="white", edgecolor=VIS, lw=0.8, zorder=5))
    ax.plot([bx-0.09, bx+0.09],[by,by], color=CL, lw=0.4, dashes=[3,2])
    ax.plot([bx,bx],[by-0.09, by+0.09], color=CL, lw=0.4, dashes=[3,2])

# Side port from top
ax.add_patch(patches.Rectangle((TV_CX+OD/2, TV_CY-PORT_R), PORT_LEN, PORT_R*2,
    facecolor="white", edgecolor=VIS, lw=1.0, zorder=4))
ax.add_patch(patches.Circle((TV_CX+OD/2+PORT_LEN*0.5, TV_CY), PORT_R*0.55,
    facecolor="#f2f2f2", edgecolor=VIS, lw=0.8, zorder=5))

# Centerlines
clinesH(TV_CX, TV_CY, FOD/2+0.25)
clinesV(TV_CX, TV_CY, FOD/2+0.25)

# Top view dimensions
ax.annotate("", xy=(TV_CX+OD/2, TV_CY+FOD/2+0.12),
    xytext=(TV_CX-OD/2, TV_CY+FOD/2+0.12),
    arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.6, mutation_scale=5))
ax.text(TV_CX, TV_CY+FOD/2+0.18, "\u00d8160", ha='center', va='bottom',
        fontsize=5.5, color=DIM, fontfamily='monospace')

ax.annotate("", xy=(TV_CX+PCD_R, TV_CY), xytext=(TV_CX, TV_CY),
    arrowprops=dict(arrowstyle='<|-', color=CL, lw=0.5, mutation_scale=4))
ax.text(TV_CX+PCD_R*0.5, TV_CY+0.06, "R80 PCD", ha='center', va='bottom',
        fontsize=4.5, color=CL, fontfamily='monospace')

# GD&T on top view
gdt(TV_CX+FOD/2+0.12, TV_CY, "\u25b1", "0.05", w=0.48)
ax.plot([TV_CX+FOD/2, TV_CX+FOD/2+0.12],[TV_CY]*2, color=DIM, lw=0.5)
gdt(TV_CX+PCD_R+0.05, TV_CY-PCD_R*0.55, "+", "\u00d80.25 M", "[A]", w=0.68)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION A-A  (right side, same baseline as FRONT VIEW)
# ═══════════════════════════════════════════════════════════════════════════
SA_CX = 10.30
SA_BY = FV_BY
SA_TY = SA_BY + H_
SA_LX = SA_CX - OD/2
SA_RX = SA_CX + OD/2
FL_SLX = SA_CX - FOD/2
FL_SRX = SA_CX + FOD/2

ax.text(SA_CX, SA_BY-0.35, "SECTION A-A", ha='center', fontsize=7,
        color="#333", fontfamily='monospace')
ax.text(SA_CX, SA_BY-0.52, "(FULL SECTION)", ha='center', fontsize=5.5,
        color="#555", fontfamily='monospace')

# Hatched wall sections
def hatch_rect(x0, y0, w_, h_, ec="#505050"):
    ax.add_patch(patches.Rectangle((x0,y0), w_, h_,
        facecolor=FILL, edgecolor=VIS, lw=0.9, zorder=4))
    ax.add_patch(patches.Rectangle((x0,y0), w_, h_,
        facecolor="none", edgecolor=ec, lw=0, hatch='////', alpha=0.55, zorder=4))

hatch_rect(SA_LX,      SA_BY, WALL, H_)   # left wall
hatch_rect(SA_RX-WALL, SA_BY, WALL, H_)   # right wall
# Bore interior (clear)
ax.add_patch(patches.Rectangle((SA_LX+WALL, SA_BY), BORE, H_,
    facecolor="white", edgecolor="none", zorder=5))
# Outer perimeter lines
for x_ in [SA_LX, SA_RX]:
    ax.plot([x_,x_],[SA_BY, SA_TY], color=VIS, lw=1.3, zorder=6)
for y_ in [SA_BY, SA_TY]:
    ax.plot([SA_LX, SA_RX],[y_,y_], color=VIS, lw=1.0, zorder=6)
# Inner bore lines
for x_ in [SA_LX+WALL, SA_RX-WALL]:
    ax.plot([x_,x_],[SA_BY, SA_TY], color=VIS, lw=0.8, zorder=6)

# Flange (hatched)
hatch_rect(FL_SLX, SA_BY-FH, FOD, FH)
ax.plot([FL_SLX, FL_SRX],[SA_BY]*2, color=VIS, lw=1.0, zorder=6)

# Port stub (half section, cut view)
pc_sa_y = SA_BY + PORT_H
hatch_rect(SA_RX, pc_sa_y-PORT_R, PORT_LEN, PORT_R*2)
ax.add_patch(patches.Rectangle((SA_RX, pc_sa_y-PORT_R*0.5), PORT_LEN, PORT_R,
    facecolor="white", edgecolor=VIS, lw=0.8, zorder=5))

# Top dome (section)
ax.add_patch(Arc((SA_CX, SA_TY), OD*0.92, 0.10, theta1=0, theta2=180,
    edgecolor=VIS, lw=1.0, zorder=6))

# Centerline
clinesV(SA_CX, (SA_BY+SA_TY)/2, H_/2+0.25)

# Wall thickness dim (inside section)
dim_h(SA_LX, SA_LX+WALL, SA_BY+H_*0.42, "8 WALL",  off=-0.16, fs=5)
dim_h(SA_RX-WALL, SA_RX, SA_BY+H_*0.42, "8 WALL",  off=-0.16, fs=5)

# Section bore dim
dim_v(SA_RX, SA_BY, SA_TY, "180", off=0.42)
dim_v(SA_RX, SA_BY-FH, SA_BY, "20", off=0.68)
dim_h(SA_LX+WALL, SA_RX-WALL, SA_TY, "\u00d8144 BORE", off=-0.22)

# Datum reference
ax.text(SA_CX, SA_TY+0.22, "(DATUM [A])", ha='center', va='bottom',
        fontsize=5.5, color=DIM, fontfamily='monospace')

# ═══════════════════════════════════════════════════════════════════════════
# DETAIL VIEW  B — Bolt Hole (4×, upper-right area)
# ═══════════════════════════════════════════════════════════════════════════
DV_CX, DV_CY = 13.40, 8.00
DV_R = 0.50   # drawing radius at 4:1 scale (4mm hole = 0.63" at 4:1 vs 0.157" at 1:2)
SCALE4 = 4/2  # 4:1 vs 1:2 base scale = 2× magnification relative to drawing

ax.text(DV_CX, DV_CY-DV_R-0.55, "DETAIL  B", ha='center', fontsize=7,
        color="#333", fontfamily='monospace', fontweight='bold')
ax.text(DV_CX, DV_CY-DV_R-0.75, "(SCALE 4:1)", ha='center', fontsize=5.5,
        color="#555", fontfamily='monospace')

# Detail circle border (phantom line)
ax.add_patch(patches.Circle((DV_CX, DV_CY), DV_R+0.28,
    fill=False, edgecolor=HID, lw=0.7, linestyle='dashed', zorder=3))

# The bolt hole detail: M8 tapped hole cross-section
hole_r = 4*sc*SCALE4      # 4mm hole radius, 2× magnified
thread_r = hole_r*1.18    # minor/major thread diameter (~18% larger)

# Outer thread helix (simplified)
ax.add_patch(patches.Circle((DV_CX, DV_CY), thread_r,
    fill=False, edgecolor=VIS, lw=0.8, zorder=5))
# Inner bore circle
ax.add_patch(patches.Circle((DV_CX, DV_CY), hole_r,
    facecolor="#f2f2f2", edgecolor=VIS, lw=0.5, zorder=5))
# Thread helix lines (simplified)
for ang in np.linspace(0, np.pi, 5):
    x1 = DV_CX + hole_r*np.cos(ang)
    x2 = DV_CX + thread_r*np.cos(ang)
    y1 = DV_CY + hole_r*np.sin(ang)
    y2 = DV_CY + thread_r*np.sin(ang)
    ax.plot([x1,x2],[y1,y2], color=VIS, lw=0.4, zorder=6)
    ax.plot([x1,x2],[-y1+2*DV_CY, -y2+2*DV_CY], color=VIS, lw=0.4, zorder=6)

# Centerlines
ax.plot([DV_CX-DV_R-0.1, DV_CX+DV_R+0.1],[DV_CY]*2,
        color=CL, lw=0.4, dashes=[5,2])
ax.plot([DV_CX]*2,[DV_CY-DV_R-0.1, DV_CY+DV_R+0.1],
        color=CL, lw=0.4, dashes=[5,2])

# Dimensions
ax.annotate("", xy=(DV_CX+thread_r, DV_CY+DV_R+0.12),
    xytext=(DV_CX-thread_r, DV_CY+DV_R+0.12),
    arrowprops=dict(arrowstyle='<|-|>', color=DIM, lw=0.5, mutation_scale=4))
ax.text(DV_CX, DV_CY+DV_R+0.18, "\u00d88.0 THRU (×4)", ha='center', va='bottom',
        fontsize=5, color=DIM, fontfamily='monospace')

# Callout balloon B
bal_x, bal_y = TV_CX + PCD_R*np.cos(np.radians(45)), TV_CY + PCD_R*np.sin(np.radians(45))
ax.add_patch(patches.Circle((bal_x, bal_y), 0.16,
    facecolor="white", edgecolor=DIM, lw=0.7, zorder=14))
ax.text(bal_x, bal_y, "B", ha='center', va='center', fontsize=6.5,
        color=DIM, fontweight='bold', zorder=15)
ax.annotate("", xy=(DV_CX-0.02, DV_CY+DV_R+0.30),
    xytext=(bal_x+0.18, bal_y+0.18),
    arrowprops=dict(arrowstyle='-', color=DIM, lw=0.5), zorder=13)

# ── ISOMETRIC REFERENCE ───────────────────────────────────────────────────
ax.text(13.40, 5.60, "ISOMETRIC REF", ha='center', fontsize=6,
        color="#777", fontfamily='monospace')
ax.text(13.40, 5.40, "(NOT FOR MANUFACTURING)", ha='center', fontsize=4.5,
        color="#999", fontfamily='monospace')
# Draw simplified isometric sketch lines
iso_cx, iso_cy = 13.40, 4.50
r_iso = OD/2 * 0.75
# Top ellipse
t = np.linspace(0, 2*np.pi, 60)
ax.plot(iso_cx + r_iso*np.cos(t), iso_cy + r_iso*np.sin(t)*0.35 + 0.60,
        color="#aaa", lw=0.6, zorder=3)
# Body lines
for x_off in [-r_iso, +r_iso]:
    ax.plot([iso_cx+x_off]*2, [iso_cy, iso_cy+0.55], color="#aaa", lw=0.6, zorder=3)
# Bottom (flange)
ax.add_patch(patches.Ellipse((iso_cx, iso_cy), r_iso*2.4, r_iso*2.4*0.35,
    facecolor=FILL, edgecolor="#aaa", lw=0.6, zorder=3))
# Port stub
ax.plot([iso_cx+r_iso, iso_cx+r_iso+0.40],[iso_cy+0.30, iso_cy+0.30],
        color="#aaa", lw=0.6)
ax.add_patch(patches.Ellipse((iso_cx+r_iso+0.40, iso_cy+0.30),
    PORT_R*1.4, PORT_R*1.4*0.35, facecolor="white", edgecolor="#aaa", lw=0.6))

# ── GENERAL NOTES ─────────────────────────────────────────────────────────
notes_x = BL+0.10
notes_y = BB+2.20
ax.text(notes_x, notes_y, "GENERAL NOTES:", fontsize=6, fontweight='bold',
        color=NOTE, fontfamily='monospace')
notes_list = [
    "1. ALL DIMENSIONS IN MILLIMETRES UNLESS OTHERWISE STATED.",
    "2. MATERIAL: AL 6061-T6 PER ASTM B209. BILLET OR PLATE STOCK.",
    "3. HEAT TREAT: T6 TEMPER. BRINELL HARDNESS 95–100 HB.",
    "4. ANODIZE: TYPE II CLEAR ANODIZE PER MIL-A-8625F.",
    "5. ALL INTERNAL THREADS: CLASS 6H PER ISO 965. OIL LIGHTLY.",
    "6. DEBURR ALL EDGES 0.2×45°. NO LOOSE BURRS OR CHIPS.",
    "7. INSPECT PER ARIA-OS ITP-001 REV A. CMM REPORT REQUIRED.",
]
for i, note in enumerate(notes_list):
    ax.text(notes_x, notes_y-0.20*(i+1), note, fontsize=5, color=NOTE,
            fontfamily='monospace')

plt.savefig(str(OUT / "eng_drawing_turbopump.png"), dpi=DPI,
    bbox_inches='tight', facecolor=BG)
plt.close()
print(f"Eng drawing v3: {(OUT/'eng_drawing_turbopump.png').stat().st_size:,} bytes")
