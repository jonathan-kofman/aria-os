"""
Civil site plan v4 — professional engineering quality.
Full title block, coordinate grid, contour lines, spot elevations,
APWA utility notations, tree symbols, proper dimensioning.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.patheffects as pe
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch
from pathlib import Path

OUT = Path("outputs/gallery_renders")

# ── Sheet layout ──────────────────────────────────────────────────────────────
# 24×18" sheet at 1"=20' scale. Plot window covers drawing area.
fig = plt.figure(figsize=(14, 10.5), dpi=110)
fig.patch.set_facecolor("#f8f5ee")

# Outer border (sheet edge)
ax_sheet = fig.add_axes([0, 0, 1, 1])
ax_sheet.set_facecolor("#f8f5ee")
ax_sheet.axis('off')
ax_sheet.add_patch(patches.Rectangle((0.005, 0.005), 0.990, 0.990,
    linewidth=2.5, edgecolor="#282828", facecolor="none", transform=ax_sheet.transAxes))
# Inner margin line
ax_sheet.add_patch(patches.Rectangle((0.020, 0.020), 0.960, 0.960,
    linewidth=0.8, edgecolor="#282828", facecolor="none", transform=ax_sheet.transAxes))

BG  = "#f8f5ee"
INK = "#1a1a1a"

# ── Drawing area ──────────────────────────────────────────────────────────────
ax = fig.add_axes([0.025, 0.095, 0.720, 0.845])
ax.set_facecolor(BG)
ax.set_aspect('equal')

# Survey coordinates (northing/easting, US survey feet)
# Site origin at N=5000, E=4000 for realism
N0, E0 = 5000, 4000
SITE_W, SITE_H = 280, 200   # feet
SITE_X = E0 + 20            # easting of SW corner
SITE_Y = N0 + 20            # northing of SW corner

# ── Survey coordinate grid ────────────────────────────────────────────────────
grid_spacing = 50
E_start = (SITE_X // grid_spacing) * grid_spacing
N_start = (SITE_Y // grid_spacing) * grid_spacing
for ex in np.arange(E_start, E0 + SITE_W + 80, grid_spacing):
    ax.plot([ex, ex], [N0 - 10, N0 + SITE_H + 80],
            color="#c0bab0", lw=0.25, linestyle='--', alpha=0.7, zorder=1)
    ax.text(ex, N0 - 14, f"E{int(ex)}", ha='center', fontsize=4,
            color="#888070", fontfamily='monospace', rotation=0)
for ny in np.arange(N_start, N0 + SITE_H + 80, grid_spacing):
    ax.plot([E0 - 10, E0 + SITE_W + 80], [ny, ny],
            color="#c0bab0", lw=0.25, linestyle='--', alpha=0.7, zorder=1)
    ax.text(E0 - 14, ny, f"N{int(ny)}", ha='right', va='center', fontsize=4,
            color="#888070", fontfamily='monospace')

# ── Road geometry ─────────────────────────────────────────────────────────────
road_top_y = SITE_Y + SITE_H + 22
road_btm_y = road_top_y - 44   # 44ft ROW
road_left_x = SITE_X - 28
road_rght_x = road_left_x + 36

# Pavement fill
ax.fill_between([E0 - 20, E0 + SITE_W + 80], road_btm_y, road_top_y,
    color="#ccc8c0", zorder=2)
ax.fill_betweenx([N0 - 20, road_top_y], road_left_x, road_rght_x,
    color="#ccc8c0", zorder=2)

# Curb lines
ax.plot([E0 - 20, E0 + SITE_W + 80], [road_top_y]*2,   color="#606050", lw=0.9, zorder=3)
ax.plot([E0 - 20, E0 + SITE_W + 80], [road_btm_y]*2,   color="#606050", lw=0.9, zorder=3)
ax.plot([road_left_x]*2, [N0 - 20, road_top_y],         color="#606050", lw=0.9, zorder=3)
ax.plot([road_rght_x]*2, [N0 - 20, road_top_y],         color="#606050", lw=0.9, zorder=3)

# Road centerlines
cl_y = (road_top_y + road_btm_y) / 2
cl_x = (road_left_x + road_rght_x) / 2
ax.plot([E0-20, E0+SITE_W+80], [cl_y]*2, color="#d08820", lw=0.8,
        linestyle=(0,(8,4)), zorder=4)
ax.plot([cl_x]*2, [N0-20, road_top_y], color="#d08820", lw=0.8,
        linestyle=(0,(8,4)), zorder=4)

# Road name labels with background
for txt, x, y, rot in [
    ("NATIONAL AVENUE", E0 + SITE_W/2 + 40, cl_y, 0),
    ("INDUSTRY BLVD",   cl_x, N0 + SITE_H/2, 90),
]:
    ax.text(x, y, txt, ha='center', va='center', fontsize=5.5, rotation=rot,
            color="#505040", fontfamily='monospace',
            bbox=dict(facecolor="#ccc8c0", edgecolor="none", alpha=0.8, pad=1.5))

# ── Contour lines (synthetic; 1ft contours; terrain slopes N→S slightly) ─────
# Elevation at SW corner = 412.5 ft; slopes +0.8ft per 100ft N, +0.3ft per 100ft E
def elev(e, n):
    return 412.5 + 0.008 * (n - N0) + 0.003 * (e - E0)

major_contours = [411, 412, 413, 414, 415]
minor_contours = [411.5, 412.5, 413.5, 414.5]

ex_rng = np.linspace(E0 - 15, E0 + SITE_W + 70, 300)
for c_val in minor_contours:
    # Solve n such that elev(e, n) = c_val
    n_vals = [(c_val - 412.5 - 0.003*(ex - E0)) / 0.008 + N0 for ex in ex_rng]
    # Filter to drawing extent
    pts = [(ex, nv) for ex, nv in zip(ex_rng, n_vals)
           if N0 - 15 < nv < N0 + SITE_H + 70]
    if pts:
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color="#b0a888", lw=0.30, alpha=0.7, zorder=2)

for c_val in major_contours:
    n_vals = [(c_val - 412.5 - 0.003*(ex - E0)) / 0.008 + N0 for ex in ex_rng]
    pts = [(ex, nv) for ex, nv in zip(ex_rng, n_vals)
           if N0 - 15 < nv < N0 + SITE_H + 70]
    if pts:
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color="#907858", lw=0.55, alpha=0.8, zorder=2)
        # Label near right edge of drawing area
        mid_idx = len(xs) // 2
        ax.text(xs[mid_idx], ys[mid_idx], f"{c_val}'",
                fontsize=4, color="#706040", ha='center', va='center',
                fontfamily='monospace',
                bbox=dict(facecolor=BG, edgecolor="none", pad=0.5))

# ── Spot elevations ───────────────────────────────────────────────────────────
spot_locs = [
    (SITE_X,             SITE_Y,             "▼"),
    (SITE_X + SITE_W,    SITE_Y,             "▼"),
    (SITE_X,             SITE_Y + SITE_H,    "▼"),
    (SITE_X + SITE_W,    SITE_Y + SITE_H,    "▼"),
    (SITE_X + SITE_W/2,  SITE_Y + SITE_H/2,  "▼"),
]
for ex, ny, mrk in spot_locs:
    ev = elev(ex, ny)
    ax.plot(ex, ny, 'x', color="#804020", ms=3.5, mew=0.8, zorder=15)
    ax.text(ex + 3, ny + 2, f"▲{ev:.1f}'", fontsize=4, color="#804020",
            fontfamily='monospace', zorder=15)

# ── Driveway/access ───────────────────────────────────────────────────────────
DRV_X = SITE_X + 28
DRV_W = 26  # 26ft drive aisle
ax.fill_betweenx([road_btm_y, SITE_Y + SITE_H],
    [DRV_X]*2, [DRV_X + DRV_W]*2, color="#c4c0b8", zorder=2)
# Drive curb cuts (flared)
for off, side in [(-4, -1), (DRV_W + 4, 1)]:
    ax.fill_betweenx([road_btm_y - 1, road_btm_y + 6],
        [DRV_X + off]*2, [DRV_X + off + side*4]*2, color="#c4c0b8", zorder=3)

# ── Site boundary (property line) ─────────────────────────────────────────────
site_rect = patches.Rectangle((SITE_X, SITE_Y), SITE_W, SITE_H,
    linewidth=1.6, edgecolor="#b82808", facecolor="none",
    zorder=10, linestyle='solid')
ax.add_patch(site_rect)

# Property line bearing & distance labels (surveyor style)
PL_COLOR = "#b82808"
ax.text(SITE_X + SITE_W/2, SITE_Y - 7, "S 89°58'42\" E  280.00'",
    ha='center', fontsize=4.5, color=PL_COLOR, fontfamily='monospace')
ax.text(SITE_X + SITE_W/2, SITE_Y + SITE_H + 5, "N 89°58'42\" W  280.00'",
    ha='center', fontsize=4.5, color=PL_COLOR, fontfamily='monospace')
ax.text(SITE_X - 3, SITE_Y + SITE_H/2, "N 00°01'18\" W\n200.00'",
    ha='right', va='center', fontsize=4.5, color=PL_COLOR, fontfamily='monospace')
ax.text(SITE_X + SITE_W + 3, SITE_Y + SITE_H/2, "S 00°01'18\" E\n200.00'",
    ha='left', va='center', fontsize=4.5, color=PL_COLOR, fontfamily='monospace')

# ── Setback lines ─────────────────────────────────────────────────────────────
SETBACK_F, SETBACK_S, SETBACK_R = 20, 10, 15   # front, side, rear
sb_rect = patches.Rectangle(
    (SITE_X + SETBACK_S, SITE_Y + SETBACK_R),
    SITE_W - 2*SETBACK_S, SITE_H - SETBACK_F - SETBACK_R,
    linewidth=0.65, edgecolor="#909090", facecolor="none",
    linestyle=(0,(4,3)), zorder=5)
ax.add_patch(sb_rect)
ax.text(SITE_X + SETBACK_S + 3, SITE_Y + SETBACK_R + 3,
    "15' REAR SB", fontsize=4, color="#909090", fontfamily='monospace')
ax.text(SITE_X + SETBACK_S + 3, SITE_Y + SITE_H - SETBACK_F - 4,
    "20' FRONT SB", fontsize=4, color="#909090", fontfamily='monospace')

# ── Building footprint ────────────────────────────────────────────────────────
BLD_X = SITE_X + 60
BLD_Y = SITE_Y + 48
BLD_W = 120
BLD_H = 80
bld = patches.Rectangle((BLD_X, BLD_Y), BLD_W, BLD_H,
    linewidth=1.5, edgecolor="#202020", facecolor="#dedad4", zorder=8)
ax.add_patch(bld)
bld_hatch = patches.Rectangle((BLD_X, BLD_Y), BLD_W, BLD_H,
    linewidth=0, edgecolor="#808080", facecolor="none",
    hatch='////', zorder=9, alpha=0.25)
ax.add_patch(bld_hatch)
# Building labels
ax.text(BLD_X + BLD_W/2, BLD_Y + BLD_H/2 + 10,
    "PROPOSED BUILDING A", ha='center', fontsize=6.5, fontweight='bold',
    color="#202020", fontfamily='monospace', zorder=11)
ax.text(BLD_X + BLD_W/2, BLD_Y + BLD_H/2 - 1,
    f"{BLD_W*BLD_H:,} SF / {BLD_W*BLD_H*0.0929:.0f} SM", ha='center',
    fontsize=5.5, color="#404040", fontfamily='monospace', zorder=11)
ax.text(BLD_X + BLD_W/2, BLD_Y + BLD_H/2 - 11,
    "FFE = 414.00'", ha='center', fontsize=5.5,
    color="#404040", fontfamily='monospace', zorder=11,
    bbox=dict(facecolor="#dedad4", edgecolor="none", pad=0.5))

# ── Parking lot ───────────────────────────────────────────────────────────────
PK_X = SITE_X + SETBACK_S
PK_Y = SITE_Y + SETBACK_R
PK_W = 42
PK_H = SITE_H - SETBACK_F - SETBACK_R
pk = patches.Rectangle((PK_X, PK_Y), PK_W, PK_H,
    linewidth=0.7, edgecolor="#888", facecolor="#d8d4cc", zorder=4)
ax.add_patch(pk)

# Parking stall lines (9ft wide, 18ft deep)
STALL_W = 9.0
STALL_D = 18.0
stall_count = 0
for i in range(1, int(PK_H / STALL_W)):
    y = PK_Y + i * STALL_W
    ax.plot([PK_X + 2, PK_X + PK_W - 2], [y, y],
            color="#aaa", lw=0.5, zorder=5)
    stall_count += 1
# Drive aisle stripe (center of parking lot)
ax.plot([PK_X + PK_W/2]*2, [PK_Y, PK_Y + PK_H],
    color="#c8c0b8", lw=0.4, linestyle=(0,(6,3)), zorder=5)

# ADA stalls (first 2)
ax.add_patch(patches.Rectangle((PK_X + 2, PK_Y + 2), PK_W - 4, 2*STALL_W,
    linewidth=0.7, edgecolor="#1870c8", facecolor="#c8e0f4",
    zorder=6, alpha=0.75))
ax.text(PK_X + PK_W/2, PK_Y + STALL_W, "ADA ×2", ha='center',
    fontsize=4.5, color="#1870c8", fontfamily='monospace', fontweight='bold', zorder=7)
# ADA symbol (simplified ICF)
ax.add_patch(patches.Circle((PK_X + PK_W/2, PK_Y + STALL_W * 0.45), 3.5,
    facecolor="#1870c8", edgecolor="none", alpha=0.5, zorder=7))

# Flow arrows in parking (grade/drainage direction)
for y_arr in [PK_Y + 30, PK_Y + 70, PK_Y + 110]:
    ax.annotate("", xy=(PK_X + PK_W - 3, y_arr), xytext=(PK_X + 3, y_arr),
        arrowprops=dict(arrowstyle="-|>", color="#2060c8", lw=0.5, mutation_scale=5),
        zorder=9)
ax.text(PK_X + PK_W/2, PK_Y + 30 - 5, "2.0% SLOPE", ha='center',
    fontsize=4, color="#2060c8", fontfamily='monospace')

# ── Trees / landscaping ───────────────────────────────────────────────────────
TREE_COLOR = "#2a7a38"
TREE_CANOPY = "#3a9a48"
tree_locs = [
    (SITE_X + 12, SITE_Y + 30),
    (SITE_X + 12, SITE_Y + 70),
    (SITE_X + 12, SITE_Y + 110),
    (SITE_X + 12, SITE_Y + 150),
    (SITE_X + 35, SITE_Y + SITE_H - 12),
    (SITE_X + 80, SITE_Y + SITE_H - 12),
    (SITE_X + 130, SITE_Y + SITE_H - 12),
    (SITE_X + 190, SITE_Y + SITE_H - 12),
    (SITE_X + 240, SITE_Y + SITE_H - 12),
    (SITE_X + SITE_W - 20, SITE_Y + 50),
    (SITE_X + SITE_W - 20, SITE_Y + 110),
]
for tx, ty in tree_locs:
    # APWA/ASCE tree symbol: concentric circles + cross spokes
    ax.add_patch(patches.Circle((tx, ty), 8, facecolor=TREE_CANOPY,
        edgecolor=TREE_COLOR, lw=0.7, alpha=0.55, zorder=6))
    ax.add_patch(patches.Circle((tx, ty), 4, facecolor="none",
        edgecolor=TREE_COLOR, lw=0.5, alpha=0.7, zorder=7))
    for ang in [0, 90, 180, 270]:
        a = np.radians(ang)
        ax.plot([tx + 4*np.cos(a), tx + 8*np.cos(a)],
                [ty + 4*np.sin(a), ty + 8*np.sin(a)],
                color=TREE_COLOR, lw=0.5, alpha=0.8, zorder=7)

# Shrub hedge along N boundary
for sx in np.arange(SITE_X + SETBACK_S, SITE_X + SITE_W - SETBACK_S, 12):
    ax.add_patch(patches.Circle((sx, SITE_Y + SITE_H - 8), 5.5,
        facecolor="#5aaa68", edgecolor="#2a6a38", lw=0.5, alpha=0.45, zorder=6))

# ── Storm drainage ────────────────────────────────────────────────────────────
CB_COLOR = "#1858b8"
# Catch basins
cb_locs = [
    (PK_X + PK_W,     PK_Y + 10,     "CB-1"),
    (PK_X + PK_W,     PK_Y + 60,     "CB-2"),
    (PK_X + PK_W,     PK_Y + PK_H - 10, "CB-3"),
    (SITE_X + SITE_W - SETBACK_S, SITE_Y + SETBACK_R, "CB-4"),
]
for cx, cy, lbl in cb_locs:
    ax.add_patch(patches.Rectangle((cx-3, cy-3), 6, 6,
        edgecolor=CB_COLOR, facecolor="#b8d0f0", lw=0.8, zorder=12))
    ax.text(cx + 5, cy, lbl, fontsize=4, color=CB_COLOR,
            va='center', fontfamily='monospace')

# Storm mains (solid blue)
storm_segs = [
    (PK_X + PK_W, PK_Y + 10,   PK_X + PK_W + 18, PK_Y + 10),
    (PK_X + PK_W, PK_Y + 60,   PK_X + PK_W + 18, PK_Y + 60),
    (PK_X + PK_W, PK_Y + PK_H - 10, PK_X + PK_W + 18, PK_Y + PK_H - 10),
    (PK_X + PK_W + 18, PK_Y + 10, PK_X + PK_W + 18, PK_Y + PK_H - 10),
]
for x1, y1, x2, y2 in storm_segs:
    ax.plot([x1, x2], [y1, y2], color=CB_COLOR, lw=1.2, zorder=11)

# Storm manhole
MH_X = PK_X + PK_W + 18
MH_Y = PK_Y + (PK_H - 10 + 10) / 2
ax.add_patch(patches.Circle((MH_X, MH_Y), 5,
    edgecolor=CB_COLOR, facecolor="#b8d0f0", lw=0.8, zorder=12))
ax.text(MH_X + 6, MH_Y, "SMH-1\nRIM=412.8'", fontsize=4, color=CB_COLOR,
        va='center', fontfamily='monospace')

# Outlet to road (18" RCP under driveway)
ax.plot([MH_X, MH_X + 28], [MH_Y, road_btm_y - 5],
    color=CB_COLOR, lw=1.0, linestyle=(0,(5,3)), zorder=10)
ax.text(MH_X + 14, (MH_Y + road_btm_y - 5)/2 + 5, "18\" RCP\nSL=0.5%",
    fontsize=4, color=CB_COLOR, fontfamily='monospace', ha='center')

# Pipe annotations
ax.text(PK_X + PK_W + 19, MH_Y, "15\" RCP", fontsize=4, rotation=90,
    va='center', color=CB_COLOR, fontfamily='monospace')

# ── Water main ────────────────────────────────────────────────────────────────
WM_COLOR = "#2090d8"
WM_Y = road_btm_y - 9
ax.plot([E0 - 15, E0 + SITE_W + 70], [WM_Y]*2, color=WM_COLOR, lw=1.3, zorder=6)
# APWA notation: W = water, size, material
ax.text(E0 + SITE_W - 20, WM_Y - 5, "8\" DIP W.M.\nINV=408.2'",
    fontsize=4.5, color=WM_COLOR, va='top', fontfamily='monospace')

# Gate valve
for gv_x in [SITE_X + 15, SITE_X + SITE_W - 15]:
    ax.add_patch(patches.Circle((gv_x, WM_Y), 3.5,
        facecolor=WM_COLOR, edgecolor="#1060a0", lw=0.7, alpha=0.7, zorder=13))
    ax.text(gv_x, WM_Y + 5.5, "GV", ha='center', fontsize=3.5,
            color=WM_COLOR, fontfamily='monospace')

# Fire hydrant
FH_X = SITE_X + 5
ax.add_patch(patches.Circle((FH_X, WM_Y - 3), 3,
    facecolor="#e82020", edgecolor="#a01010", lw=0.7, zorder=14))
ax.text(FH_X + 5, WM_Y - 3, "FH\n300' COVERAGE", fontsize=3.5, va='center',
        color="#c01010", fontfamily='monospace')

# Service line to building
ax.plot([BLD_X + BLD_W/2, BLD_X + BLD_W/2], [WM_Y, BLD_Y],
    color=WM_COLOR, lw=0.8, linestyle=(0,(4,2)), zorder=6)
ax.text(BLD_X + BLD_W/2 + 3, (WM_Y + BLD_Y)/2, "2\" DOM. W.S.\n+ 4\" FIRE",
    fontsize=4, color=WM_COLOR, fontfamily='monospace')

# ── Sanitary sewer ─────────────────────────────────────────────────────────────
SS_COLOR = "#b82808"
SS_Y = road_btm_y - 17
ax.plot([E0 - 15, E0 + SITE_W + 70], [SS_Y]*2, color=SS_COLOR, lw=1.2, zorder=6)
ax.text(E0 + SITE_W - 20, SS_Y - 5, "8\" PVC SDR-35\nSAN. SEWER SL=0.4%",
    fontsize=4.5, color=SS_COLOR, va='top', fontfamily='monospace')

# Sanitary manhole
SS_MH_X = SITE_X + SITE_W/2
ax.add_patch(patches.Circle((SS_MH_X, SS_Y), 4.5,
    edgecolor=SS_COLOR, facecolor="#f0c8c0", lw=0.8, zorder=13))
ax.text(SS_MH_X, SS_Y + 6, "SSMH-1\nRIM=411.9'", ha='center',
    fontsize=4, color=SS_COLOR, fontfamily='monospace')

# Sewer service
ax.plot([BLD_X + BLD_W/3, BLD_X + BLD_W/3], [SS_Y, BLD_Y],
    color=SS_COLOR, lw=0.8, linestyle=(0,(4,2)), zorder=6)
ax.text(BLD_X + BLD_W/3 - 3, (SS_Y + BLD_Y)/2, "6\" PVC\nSAN. LAT.",
    fontsize=4, color=SS_COLOR, fontfamily='monospace', ha='right')

# ── Gas main ──────────────────────────────────────────────────────────────────
GAS_COLOR = "#c87820"
GAS_Y = road_btm_y - 25
ax.plot([E0 - 15, E0 + 200], [GAS_Y]*2, color=GAS_COLOR, lw=1.0, zorder=6)
ax.text(E0 + 30, GAS_Y - 5, "4\" HDPE GAS MAIN\n(HIGH PRESSURE)",
    fontsize=4.5, color=GAS_COLOR, va='top', fontfamily='monospace')
# Gas regulator
ax.add_patch(patches.Rectangle((BLD_X + BLD_W/2 - 3, BLD_Y - 12), 6, 8,
    facecolor="#e0c060", edgecolor=GAS_COLOR, lw=0.7, zorder=12))
ax.text(BLD_X + BLD_W/2, BLD_Y - 7, "GR", ha='center', fontsize=4,
    color=GAS_COLOR, fontfamily='monospace')
ax.plot([BLD_X + BLD_W/2, SITE_X + 80], [BLD_Y - 8, GAS_Y],
    color=GAS_COLOR, lw=0.8, linestyle=(0,(4,2)), zorder=6)

# ── Electrical ────────────────────────────────────────────────────────────────
ELEC_COLOR = "#806010"
# Transformer pad
TP_X = SITE_X + SITE_W - 18
TP_Y = SITE_Y + SITE_H - 30
ax.add_patch(patches.Rectangle((TP_X, TP_Y), 14, 14,
    facecolor="#d0c880", edgecolor=ELEC_COLOR, lw=0.9, zorder=12))
ax.text(TP_X + 7, TP_Y + 7, "XFMR\n250 KVA", ha='center', va='center',
    fontsize=4, color=ELEC_COLOR, fontfamily='monospace')
# Service run (dashed underground)
ax.plot([TP_X + 7, BLD_X + BLD_W], [TP_Y + 7, BLD_Y + BLD_H/2],
    color=ELEC_COLOR, lw=0.8, linestyle=(0,(3,2)), zorder=6)
ax.text(TP_X - 5, TP_Y + 7, "4\" ELEC.\nCONDUIT", fontsize=4, va='center',
    color=ELEC_COLOR, fontfamily='monospace', ha='right')

# ── Utility crossings ─────────────────────────────────────────────────────────
for ux in [DRV_X + 13, SITE_X + SITE_W/2]:
    for uy in [WM_Y, SS_Y, GAS_Y]:
        ax.plot([ux-3, ux+3], [uy-3, uy+3], color="#606050", lw=0.7, zorder=16)
        ax.plot([ux-3, ux+3], [uy+3, uy-3], color="#606050", lw=0.7, zorder=16)

# ── Survey control monuments ──────────────────────────────────────────────────
for sx, sy, lbl, e_val, n_val in [
    (SITE_X,          SITE_Y,          "MON-1", SITE_X, SITE_Y),
    (SITE_X + SITE_W, SITE_Y,          "MON-2", SITE_X + SITE_W, SITE_Y),
    (SITE_X,          SITE_Y + SITE_H, "MON-3", SITE_X, SITE_Y + SITE_H),
    (SITE_X + SITE_W, SITE_Y + SITE_H, "MON-4", SITE_X + SITE_W, SITE_Y + SITE_H),
]:
    ax.plot([sx-5, sx+5], [sy, sy],    color="#c02808", lw=0.9, zorder=16)
    ax.plot([sx, sx],     [sy-5, sy+5], color="#c02808", lw=0.9, zorder=16)
    ax.add_patch(patches.Circle((sx, sy), 3, edgecolor="#c02808",
        facecolor="none", lw=0.8, zorder=16))
    ax.text(sx + 5, sy + 5, f"{lbl}\nE={e_val}\nN={n_val}",
        fontsize=3.5, color="#c02808", fontfamily='monospace')

# ── Light poles ───────────────────────────────────────────────────────────────
LP_COLOR = "#707030"
for lx, ly in [
    (PK_X + 5, PK_Y + PK_H + 10),
    (PK_X + PK_W + 5, PK_Y + PK_H + 10),
    (DRV_X + DRV_W + 5, SITE_Y + SITE_H - 10),
    (SITE_X + SITE_W - 8, SITE_Y + 30),
]:
    ax.add_patch(patches.Circle((lx, ly), 3.5, edgecolor=LP_COLOR,
        facecolor="#fffff0", lw=0.7, zorder=13))
    # Pole symbol (cross)
    ax.plot([lx, lx], [ly, ly - 8], color=LP_COLOR, lw=0.6, zorder=13)
    ax.text(lx + 5, ly - 4, "LP-1\n25' 400W", fontsize=3.5,
            color=LP_COLOR, fontfamily='monospace')

# ── Dimensions ────────────────────────────────────────────────────────────────
def dim_h(ax, x1, x2, y, txt, offset=10, color='#404040'):
    ax.annotate('', xy=(x1, y), xytext=(x2, y),
        arrowprops=dict(arrowstyle='<->', color=color, lw=0.6, mutation_scale=6))
    ax.text((x1+x2)/2, y + offset, txt, ha='center', fontsize=5,
        color=color, fontfamily='monospace')
    ax.plot([x1, x1], [y-3, y+3], color=color, lw=0.5)
    ax.plot([x2, x2], [y-3, y+3], color=color, lw=0.5)

def dim_v(ax, x, y1, y2, txt, offset=10, color='#404040'):
    ax.annotate('', xy=(x, y1), xytext=(x, y2),
        arrowprops=dict(arrowstyle='<->', color=color, lw=0.6, mutation_scale=6))
    ax.text(x + offset, (y1+y2)/2, txt, ha='left', va='center', fontsize=5,
        color=color, fontfamily='monospace')
    ax.plot([x-3, x+3], [y1, y1], color=color, lw=0.5)
    ax.plot([x-3, x+3], [y2, y2], color=color, lw=0.5)

dim_h(ax, SITE_X, SITE_X + SITE_W, SITE_Y - 22, "280'-0\" (TOTAL PARCEL WIDTH)")
dim_v(ax, SITE_X + SITE_W + 18, SITE_Y, SITE_Y + SITE_H, "200'-0\" (PARCEL DEPTH)")
dim_h(ax, BLD_X, BLD_X + BLD_W, BLD_Y - 12, f"{BLD_W}'-0\" (BLDG FOOTPRINT)")
dim_v(ax, BLD_X - 10, BLD_Y, BLD_Y + BLD_H, f"{BLD_H}'-0\"")
dim_h(ax, SITE_X, BLD_X, BLD_Y + BLD_H/2, f"{BLD_X - SITE_X}' SETBACK", offset=-7)

# ── Axis styling ──────────────────────────────────────────────────────────────
ax.set_xlim(E0 - 22, E0 + SITE_W + 82)
ax.set_ylim(N0 - 38, N0 + SITE_H + 82)
ax.tick_params(labelsize=5, labelcolor='#808070', colors='#808070')
for sp in ax.spines.values():
    sp.set_edgecolor('#505040'); sp.set_linewidth(0.8)
ax.set_xlabel("EASTING (US SURVEY FEET)", fontsize=5.5, color="#505040",
    fontfamily='monospace', labelpad=2)
ax.set_ylabel("NORTHING (US SURVEY FEET)", fontsize=5.5, color="#505040",
    fontfamily='monospace', labelpad=2)

# ── Right side panel: legend + notes ──────────────────────────────────────────
ax_info = fig.add_axes([0.755, 0.095, 0.235, 0.845])
ax_info.set_facecolor(BG)
ax_info.axis('off')
for sp in ax_info.spines.values():
    sp.set_visible(True); sp.set_edgecolor("#404030"); sp.set_linewidth(0.6)

cur_y = 0.98
def info_text(txt, size=6, color="#202020", bold=False, y_offset=0.025):
    global cur_y
    cur_y -= y_offset
    ax_info.text(0.04, cur_y, txt, va='top', fontsize=size, color=color,
        fontfamily='monospace', fontweight='bold' if bold else 'normal',
        transform=ax_info.transAxes)

# ── Legend ─────────────────────────────────────────────────────────────────────
ax_info.text(0.50, cur_y, "LEGEND", ha='center', fontsize=8, fontweight='bold',
    color="#202020", fontfamily='monospace', transform=ax_info.transAxes)
ax_info.add_patch(patches.FancyBboxPatch((0.02, 0.82), 0.96, 0.16,
    boxstyle="square,pad=0.01", facecolor="#f0ece4",
    edgecolor="#808070", lw=0.7, transform=ax_info.transAxes))

legend_items = [
    ("#b82808", "────",  "Property Line (P.L.)"),
    ("#909090", "- - -", "Building Setback"),
    ("#202020", "════",  "Building Footprint"),
    ("#1858b8", "────",  "Storm Sewer (RCP)"),
    ("#2090d8", "────",  "Water Main (DIP)"),
    ("#b82808", "────",  "Sanitary Sewer (PVC)"),
    ("#c87820", "────",  "Gas Main (HDPE)"),
    ("#806010", "- - -", "Electrical Conduit"),
    ("#c02808", "───",   "Survey Monument"),
    ("#d08820", "- - -", "Road Centerline"),
    ("#2a7a38", "●",     "Deciduous Tree"),
]
for i, (col, sym, lbl) in enumerate(legend_items):
    y_t = 0.965 - 0.013 * (i + 1.5)
    ax_info.text(0.04, y_t, sym, color=col, fontsize=6,
        fontfamily='monospace', transform=ax_info.transAxes)
    ax_info.text(0.24, y_t, lbl, color="#202020", fontsize=5.5,
        fontfamily='monospace', transform=ax_info.transAxes)

# ── Site notes ─────────────────────────────────────────────────────────────────
cur_y = 0.80
ax_info.add_patch(patches.FancyBboxPatch((0.02, 0.555), 0.96, 0.24,
    boxstyle="square,pad=0.01", facecolor="#f0ece4",
    edgecolor="#808070", lw=0.7, transform=ax_info.transAxes))
ax_info.text(0.50, 0.79, "GENERAL NOTES", ha='center', fontsize=7, fontweight='bold',
    color="#202020", fontfamily='monospace', transform=ax_info.transAxes)

notes = [
    "1. ALL DIMENSIONS IN US SURVEY FEET",
    "   UNLESS OTHERWISE NOTED.",
    "2. SETBACKS: FRONT 20', SIDES 10', REAR 15'",
    "3. PARKING: 22 STD (9×18') + 2 ADA = 24 TOTAL",
    "4. IMPERVIOUS COVER: 58.4% (MAX ALLOWED 70%)",
    "5. ALL UTILITIES SHOWN ARE APPROX. FIELD-",
    "   VERIFY BEFORE EXCAVATION. 811 REQUIRED.",
    "6. ELEVATIONS REF NAVD 88 DATUM.",
    "7. CONTOUR INTERVAL = 1.0 FT (MAJOR = 5 FT)",
    "8. FFE = 412.00 FT MIN. (100-YR FLOOD = 410.5')",
    "9. ALL STORM CONNECTIONS REQ. CITY APPROVAL.",
    "10. ACCESSIBLE PATH OF TRAVEL PROVIDED PER",
    "    ADA / CBC SECTION 11B.",
]
for i, n in enumerate(notes):
    ax_info.text(0.04, 0.77 - i * 0.017, n, va='top', fontsize=4.8,
        color="#303030" if i == 0 else "#404040", fontfamily='monospace',
        fontweight='bold' if i == 0 else 'normal',
        transform=ax_info.transAxes)

# ── Area table ─────────────────────────────────────────────────────────────────
cur_y = 0.545
ax_info.add_patch(patches.FancyBboxPatch((0.02, 0.42), 0.96, 0.12,
    boxstyle="square,pad=0.01", facecolor="#f0ece4",
    edgecolor="#808070", lw=0.7, transform=ax_info.transAxes))
ax_info.text(0.50, 0.54, "AREA TABULATION", ha='center', fontsize=7, fontweight='bold',
    color="#202020", fontfamily='monospace', transform=ax_info.transAxes)

area_rows = [
    ("DESCRIPTION",    "AREA (SF)",  "% SITE"),
    ("Site Area",       "56,000",    "100.0%"),
    ("Building",         "9,600",     "17.1%"),
    ("Paving / Drives",  "8,736",     "15.6%"),
    ("Parking Lot",      "7,350",     "13.1%"),
    ("Landscape / Green","30,314",    "54.2%"),
    ("Impervious Total", "32,682",    "58.4%"),
]
for i, (desc, area, pct) in enumerate(area_rows):
    y_row = 0.525 - i * 0.015
    bold = i == 0 or i == len(area_rows) - 1
    ax_info.text(0.04, y_row, desc, va='top', fontsize=4.8,
        color="#101010" if bold else "#303030", fontfamily='monospace',
        fontweight='bold' if bold else 'normal',
        transform=ax_info.transAxes)
    ax_info.text(0.65, y_row, area, va='top', fontsize=4.8, ha='right',
        color="#101010" if bold else "#303030", fontfamily='monospace',
        fontweight='bold' if bold else 'normal',
        transform=ax_info.transAxes)
    ax_info.text(0.98, y_row, pct, va='top', fontsize=4.8, ha='right',
        color="#101010" if bold else "#303030", fontfamily='monospace',
        fontweight='bold' if bold else 'normal',
        transform=ax_info.transAxes)

# ── North arrow ────────────────────────────────────────────────────────────────
na_cx, na_cy = 0.50, 0.31
ax_info.add_patch(patches.Circle((na_cx, na_cy), 0.10,
    edgecolor="#303030", facecolor="none", lw=1.0, transform=ax_info.transAxes))
# Filled N half
theta = np.linspace(-np.pi/6, np.pi/6 + np.pi, 60)
ax_info.fill(
    [na_cx + 0.08*np.sin(t) for t in theta],
    [na_cy + 0.08*np.cos(t) for t in theta],
    color="#202020", transform=ax_info.transAxes, zorder=20)
ax_info.annotate("", xy=(na_cx, na_cy + 0.10), xytext=(na_cx, na_cy - 0.10),
    arrowprops=dict(arrowstyle="-|>", color="#202020", lw=1.5, mutation_scale=12),
    xycoords='axes fraction', textcoords='axes fraction')
ax_info.text(na_cx, na_cy + 0.135, "N", ha='center', va='bottom', fontsize=14,
    fontweight='bold', color="#202020", fontfamily='monospace',
    transform=ax_info.transAxes)
ax_info.text(na_cx, na_cy - 0.135, "TRUE NORTH", ha='center', va='top', fontsize=4.5,
    color="#404040", fontfamily='monospace', transform=ax_info.transAxes)

# ── Scale bar ──────────────────────────────────────────────────────────────────
sb_left = 0.04; sb_y = 0.17; sb_seg = 0.18  # 0.18 * panel_width = 1 segment
ax_info.text(0.50, 0.205, "GRAPHIC SCALE  (1\" = 20')",
    ha='center', fontsize=5.5, color="#303030", fontfamily='monospace',
    transform=ax_info.transAxes)
for i in range(4):
    col = "#202020" if i % 2 == 0 else "#f0ece4"
    ax_info.add_patch(patches.Rectangle(
        (sb_left + i * sb_seg, sb_y - 0.008), sb_seg, 0.016,
        facecolor=col, edgecolor="#202020", lw=0.6,
        transform=ax_info.transAxes))
for i, lbl in enumerate(["0", "20'", "40'", "60'", "80'"]):
    ax_info.text(sb_left + i * sb_seg, sb_y - 0.016, lbl, ha='center',
        fontsize=4.5, color="#303030", fontfamily='monospace',
        transform=ax_info.transAxes)

# ── Title block ────────────────────────────────────────────────────────────────
ax_tb = fig.add_axes([0.020, 0.008, 0.970, 0.082])
ax_tb.set_facecolor("#eee8da")
ax_tb.axis('off')
ax_tb.add_patch(patches.Rectangle((0, 0), 1, 1,
    linewidth=0.8, edgecolor="#282828", facecolor="none",
    transform=ax_tb.transAxes))

# Dividers
for x in [0.32, 0.54, 0.68, 0.80, 0.89]:
    ax_tb.plot([x, x], [0, 1], color="#282828", lw=0.6, transform=ax_tb.transAxes)
ax_tb.plot([0, 1], [0.52, 0.52], color="#282828", lw=0.5, transform=ax_tb.transAxes)

# Project name (large)
ax_tb.text(0.16, 0.82, "NATIONAL COMMERCE PARK — LOT 7A",
    ha='center', va='center', fontsize=9.5, fontweight='bold', color="#101010",
    fontfamily='monospace', transform=ax_tb.transAxes)
ax_tb.text(0.16, 0.28, "PRELIMINARY SITE DEVELOPMENT PLAN  |  CIVIL ENGINEERING SHEET",
    ha='center', va='center', fontsize=6, color="#303030",
    fontfamily='monospace', transform=ax_tb.transAxes)

# Project info cells
cells = [
    (0.43, "PROJECT NO.", "AOS-2026-0017"),
    (0.61, "DRAWN BY",    "ARIA-OS AGENT"),
    (0.74, "CHECKED BY",  "J. ENGINEER, PE"),
    (0.845,"SCALE",       "1\" = 20' (HORIZ)"),
    (0.945,"SHEET",       "CS-001"),
]
for xc, lbl, val in cells:
    ax_tb.text(xc, 0.82, lbl, ha='center', va='center', fontsize=5,
        color="#606060", fontfamily='monospace', transform=ax_tb.transAxes)
    ax_tb.text(xc, 0.28, val, ha='center', va='center', fontsize=6.5,
        color="#101010", fontfamily='monospace', fontweight='bold',
        transform=ax_tb.transAxes)

# Date / revision
ax_tb.text(0.32 + 0.003, 0.82, "DATE", va='center', fontsize=5,
    color="#606060", fontfamily='monospace', transform=ax_tb.transAxes)
ax_tb.text(0.32 + 0.003, 0.28, "2026-04-10", va='center', fontsize=6.5,
    color="#101010", fontfamily='monospace', fontweight='bold',
    transform=ax_tb.transAxes)

# Firm name (right of title block)
ax_tb.text(1.0 - 0.003, 0.55, "ARIA-OS AUTONOMOUS ENGINEERING  |  CIVIL-SITE PIPELINE",
    ha='right', va='center', fontsize=5, color="#404040",
    fontfamily='monospace', transform=ax_tb.transAxes)
ax_tb.text(1.0 - 0.003, 0.15, "DWG: CS-NATL-001  |  REV A  |  DATUM: NAVD 88  |  "
    "PHOTOGRAMMETRY + AI SITE LAYOUT",
    ha='right', va='center', fontsize=5, color="#606060",
    fontfamily='monospace', transform=ax_tb.transAxes)

# ── Save ──────────────────────────────────────────────────────────────────────
plt.savefig(str(OUT / "civil_site_plan.png"), dpi=110,
            bbox_inches='tight', facecolor=BG)
plt.close()
print(f"civil_v4: {(OUT/'civil_site_plan.png').stat().st_size:,} bytes")
