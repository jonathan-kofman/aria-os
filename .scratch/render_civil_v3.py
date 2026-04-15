"""
Civil site plan v3 — fully synthetic, professional quality.
Demonstrates what ARIA-OS civil infrastructure DXF output looks like.
Road network, storm drainage, utilities, building, parking, survey control.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.lines as mlines
from matplotlib.patches import FancyArrowPatch
from pathlib import Path

OUT = Path("outputs/gallery_renders")
BG  = "#f5f1e8"   # drafting paper cream

fig, ax = plt.subplots(figsize=(11, 8.5), dpi=110)
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.set_aspect('equal')

# ── Site extents (feet) ────────────────────────────────────────────────────────
SITE_W, SITE_H = 280, 200   # site parcel
SITE_X, SITE_Y = 30, 30     # lower-left

# ── Road geometry ──────────────────────────────────────────────────────────────
# Main road along top (N)
road_top_y = SITE_Y + SITE_H + 20
road_btm_y = road_top_y - 48  # 48ft road width
ax.fill_between([0, 360], [road_btm_y]*2, [road_top_y]*2, color="#d0ccc4", zorder=1)
# Road edge lines
ax.plot([0, 360], [road_top_y]*2, color="#888", lw=1.0, zorder=2)
ax.plot([0, 360], [road_btm_y]*2, color="#888", lw=1.0, zorder=2)
# Centerline (dashed yellow)
cl_y = (road_top_y + road_btm_y) / 2
ax.plot([0, 360], [cl_y]*2, color="#e8a820", lw=0.9, linestyle=(0,(8,4)), zorder=3)
ax.text(180, cl_y + 2, "NATIONAL AVE", ha='center', fontsize=5.5,
        color="#606050", fontfamily='monospace')

# Side road along left (W)
road_left_x = SITE_X - 30
road_rght_x = road_left_x + 40
ax.fill_betweenx([0, 280], [road_left_x]*2, [road_rght_x]*2, color="#d0ccc4", zorder=1)
ax.plot([road_left_x]*2, [0, 280], color="#888", lw=1.0, zorder=2)
ax.plot([road_rght_x]*2, [0, 280], color="#888", lw=1.0, zorder=2)
cl_x = (road_left_x + road_rght_x) / 2
ax.plot([cl_x]*2, [0, 280], color="#e8a820", lw=0.9, linestyle=(0,(8,4)), zorder=3)
ax.text(cl_x, 140, "INDUSTRY BLVD", ha='center', va='center', fontsize=5.5, rotation=90,
        color="#606050", fontfamily='monospace')

# ── Driveway / access ──────────────────────────────────────────────────────────
drv_x = SITE_X + 30
ax.fill_betweenx([road_btm_y, SITE_Y + SITE_H], [drv_x]*2, [drv_x+24]*2, color="#c8c4bc", zorder=1)

# ── Site boundary ──────────────────────────────────────────────────────────────
site_rect = patches.Rectangle((SITE_X, SITE_Y), SITE_W, SITE_H,
    linewidth=1.6, edgecolor="#c83010", facecolor="none", zorder=10, linestyle='solid')
ax.add_patch(site_rect)
ax.text(SITE_X + SITE_W/2, SITE_Y - 8, "PROPERTY LINE", ha='center', fontsize=5.5,
        color="#c83010", fontfamily='monospace')

# ── Setback lines (dashed) ────────────────────────────────────────────────────
SETBACK = 15
sb_rect = patches.Rectangle((SITE_X+SETBACK, SITE_Y+SETBACK),
    SITE_W-2*SETBACK, SITE_H-2*SETBACK,
    linewidth=0.7, edgecolor="#909090", facecolor="none", linestyle=(0,(4,3)), zorder=5)
ax.add_patch(sb_rect)
ax.text(SITE_X+SETBACK+2, SITE_Y+SETBACK+2, "15' SETBACK (TYP)", fontsize=4.5,
        color="#909090", fontfamily='monospace')

# ── Building footprint ─────────────────────────────────────────────────────────
BLD_X, BLD_Y, BLD_W, BLD_H = SITE_X+60, SITE_Y+55, 120, 90
bld = patches.Rectangle((BLD_X, BLD_Y), BLD_W, BLD_H,
    linewidth=1.4, edgecolor="#303030", facecolor="#e0dcd2", zorder=8)
ax.add_patch(bld)
# Hatch fill
bld_hatch = patches.Rectangle((BLD_X, BLD_Y), BLD_W, BLD_H,
    linewidth=0, edgecolor="#808080", facecolor="none", hatch='////', zorder=9, alpha=0.3)
ax.add_patch(bld_hatch)
ax.text(BLD_X+BLD_W/2, BLD_Y+BLD_H/2+8, "PROPOSED BUILDING", ha='center',
        fontsize=6, fontweight='bold', color="#303030", fontfamily='monospace', zorder=11)
ax.text(BLD_X+BLD_W/2, BLD_Y+BLD_H/2-8, f"{BLD_W*BLD_H:,} SF", ha='center',
        fontsize=6, color="#606060", fontfamily='monospace', zorder=11)

# ── Parking lot ────────────────────────────────────────────────────────────────
PK_X, PK_Y, PK_W, PK_H = SITE_X+SETBACK, SITE_Y+SETBACK, 38, SITE_H-2*SETBACK
pk = patches.Rectangle((PK_X, PK_Y), PK_W, PK_H,
    linewidth=0.8, edgecolor="#888", facecolor="#dedad2", zorder=4)
ax.add_patch(pk)
# Parking stall lines (9ft wide)
for i in range(1, int(PK_H/9)):
    y = PK_Y + i*9
    ax.plot([PK_X+2, PK_X+PK_W-2], [y,y], color="#aaa", lw=0.5, zorder=5)
# ADA spaces (first 2)
ax.add_patch(patches.Rectangle((PK_X+2, PK_Y+2), PK_W-4, 18,
    linewidth=0.6, edgecolor="#1890d0", facecolor="#d0e8f8", zorder=6, alpha=0.7))
ax.text(PK_X+PK_W/2, PK_Y+11, "ADA", ha='center', fontsize=5, color="#1890d0",
        fontfamily='monospace', zorder=7)

# ── Storm drainage ─────────────────────────────────────────────────────────────
# Catch basins (CB) at parking lot corners
CB_COLOR = "#2060c8"
cb_locs = [(PK_X+PK_W, PK_Y+10), (PK_X+PK_W, PK_Y+PK_H-10),
           (SITE_X+SITE_W-SETBACK, SITE_Y+SETBACK)]
for cx, cy in cb_locs:
    ax.add_patch(patches.Rectangle((cx-3,cy-3), 6, 6,
        edgecolor=CB_COLOR, facecolor="#c0d8f0", lw=0.8, zorder=12))
    ax.text(cx+4, cy, "CB", fontsize=4, color=CB_COLOR, va='center', fontfamily='monospace')

# Storm pipe (12" RCP) from CB to outlet
pipe_pts = [(PK_X+PK_W, PK_Y+10), (PK_X+PK_W+20, PK_Y+10),
            (PK_X+PK_W+20, PK_Y+PK_H-10), (PK_X+PK_W, PK_Y+PK_H-10)]
for i in range(len(pipe_pts)-1):
    ax.plot([pipe_pts[i][0], pipe_pts[i+1][0]],
            [pipe_pts[i][1], pipe_pts[i+1][1]],
            color=CB_COLOR, lw=1.4, zorder=11)
ax.text(PK_X+PK_W+21, PK_Y+PK_H/2, "12\" RCP", fontsize=4.5, rotation=90,
        va='center', color=CB_COLOR, fontfamily='monospace')

# Storm manhole (MH)
MH_X, MH_Y = PK_X+PK_W+20, PK_Y+PK_H/2
ax.add_patch(patches.Circle((MH_X, MH_Y), 5, edgecolor=CB_COLOR,
    facecolor="#c0d8f0", lw=0.8, zorder=12))
ax.text(MH_X+6, MH_Y, "MH-1", fontsize=4, color=CB_COLOR, va='center', fontfamily='monospace')

# ── Water main ────────────────────────────────────────────────────────────────
WM_Y = road_btm_y - 8
ax.plot([0, 360], [WM_Y]*2, color="#1890d0", lw=1.3, zorder=6)
ax.text(280, WM_Y-4, "8\" DIP W.M.", fontsize=4.5, color="#1890d0",
        va='top', fontfamily='monospace')
# Water service to building
ax.plot([BLD_X+BLD_W/2, BLD_X+BLD_W/2], [WM_Y, BLD_Y],
        color="#1890d0", lw=0.9, linestyle='--', zorder=6)
ax.text(BLD_X+BLD_W/2+2, (WM_Y+BLD_Y)/2, '1\" W.S.', fontsize=4,
        color="#1890d0", fontfamily='monospace')

# ── Sanitary sewer ─────────────────────────────────────────────────────────────
SS_Y = road_btm_y - 16
ax.plot([0, 360], [SS_Y]*2, color="#c83820", lw=1.2, zorder=6)
ax.text(280, SS_Y-4, "8\" PVC SAN.", fontsize=4.5, color="#c83820",
        va='top', fontfamily='monospace')
# Sewer service to building
ax.plot([BLD_X+BLD_W/3, BLD_X+BLD_W/3], [SS_Y, BLD_Y],
        color="#c83820", lw=0.9, linestyle='--', zorder=6)
ax.text(BLD_X+BLD_W/3+2, (SS_Y+BLD_Y)/2, '6\" SAN.', fontsize=4,
        color="#c83820", fontfamily='monospace')

# ── Gas main ──────────────────────────────────────────────────────────────────
GAS_Y = road_btm_y - 24
ax.plot([0, 200], [GAS_Y]*2, color="#c87820", lw=1.0, zorder=6)
ax.text(100, GAS_Y-4, "4\" GAS", fontsize=4.5, color="#c87820",
        va='top', fontfamily='monospace')

# ── Utility crossings (X markers) ─────────────────────────────────────────────
for ux in [drv_x+12, SITE_X+SITE_W/2]:
    for uy in [WM_Y, SS_Y]:
        ax.plot([ux-3,ux+3],[uy-3,uy+3], color="#606060", lw=0.7, zorder=15)
        ax.plot([ux-3,ux+3],[uy+3,uy-3], color="#606060", lw=0.7, zorder=15)

# ── Light poles ───────────────────────────────────────────────────────────────
for lx, ly in [(PK_X+5, PK_Y+PK_H+8), (PK_X+PK_W+5, PK_Y+PK_H+8),
               (SITE_X+SITE_W-10, SITE_Y+SETBACK+10)]:
    ax.add_patch(patches.Circle((lx,ly), 3, edgecolor="#808040",
        facecolor="#fffff0", lw=0.7, zorder=12))
    ax.text(lx+4, ly, "LP", fontsize=4, color="#808040", va='center', fontfamily='monospace')
    ax.text(lx, ly-6, "25' POLE\n400W LED", fontsize=3.5, ha='center',
            color="#909050", fontfamily='monospace')

# ── Survey control points ──────────────────────────────────────────────────────
for sx, sy, label in [(SITE_X, SITE_Y, "MON-1"), (SITE_X+SITE_W, SITE_Y, "MON-2"),
                       (SITE_X, SITE_Y+SITE_H, "MON-3")]:
    ax.plot([sx-4,sx+4],[sy,sy], color="#d84010", lw=0.9)
    ax.plot([sx,sx],[sy-4,sy+4], color="#d84010", lw=0.9)
    ax.add_patch(patches.Circle((sx,sy), 2.5, edgecolor="#d84010",
        facecolor="none", lw=0.8, zorder=15))
    ax.text(sx+4, sy+4, label, fontsize=4, color="#d84010", fontfamily='monospace')

# ── Dimensions ────────────────────────────────────────────────────────────────
def dim_h(ax, x1, x2, y, txt, offset=8):
    ax.annotate('', xy=(x1,y), xytext=(x2,y),
        arrowprops=dict(arrowstyle='<->',color='#505050',lw=0.7,mutation_scale=6))
    ax.text((x1+x2)/2, y+offset, txt, ha='center', fontsize=5,
        color='#505050', fontfamily='monospace')
    ax.plot([x1,x1],[y-3,y+3],color='#505050',lw=0.5)
    ax.plot([x2,x2],[y-3,y+3],color='#505050',lw=0.5)

def dim_v(ax, x, y1, y2, txt, offset=8):
    ax.annotate('', xy=(x,y1), xytext=(x,y2),
        arrowprops=dict(arrowstyle='<->',color='#505050',lw=0.7,mutation_scale=6))
    ax.text(x+offset, (y1+y2)/2, txt, ha='left', fontsize=5, va='center',
        color='#505050', fontfamily='monospace')
    ax.plot([x-3,x+3],[y1,y1],color='#505050',lw=0.5)
    ax.plot([x-3,x+3],[y2,y2],color='#505050',lw=0.5)

dim_h(ax, SITE_X, SITE_X+SITE_W, SITE_Y-18, f"{SITE_W}'-0\"")
dim_v(ax, SITE_X+SITE_W+12, SITE_Y, SITE_Y+SITE_H, f"{SITE_H}'-0\"")
dim_h(ax, BLD_X, BLD_X+BLD_W, BLD_Y-10, f"{BLD_W}'-0\"")

# ── Site notes ────────────────────────────────────────────────────────────────
notes = [
    "SITE NOTES:",
    "1. ALL DIMENSIONS IN FEET UNLESS NOTED",
    "2. SETBACK: FRONT 15', SIDES 10', REAR 15'",
    "3. PARKING: 28 STANDARD + 2 ADA = 30 TOTAL",
    "4. IMPERVIOUS COVER: 62% (MAX 70%)",
    "5. FIRE HYDRANT WITHIN 300' OF BLDG ENTRY",
]
for i, note in enumerate(notes):
    ax.text(SITE_X, SITE_Y-32-i*8, note, fontsize=5,
        color="#404040" if i==0 else "#606060", fontfamily='monospace',
        fontweight='bold' if i==0 else 'normal')

# ── Legend ────────────────────────────────────────────────────────────────────
LG_X, LG_Y = SITE_X+SITE_W+30, SITE_Y+SITE_H-5
legend_items = [
    ("#c83010", "Property Line", 1.5, 'solid'),
    ("#909090", "Building Setback", 0.8, (0,(4,3))),
    ("#303030", "Building Footprint", 1.4, 'solid'),
    ("#2060c8", "Storm Drainage", 1.3, 'solid'),
    ("#1890d0", "Water Main / Service", 1.2, 'solid'),
    ("#c83820", "Sanitary Sewer", 1.2, 'solid'),
    ("#c87820", "Gas Main", 1.0, 'solid'),
    ("#d84010", "Survey Control", 0.9, 'solid'),
    ("#e8a820", "Road Centerline", 0.9, (0,(8,4))),
    ("#808040", "Light Pole", 0.8, 'solid'),
]
ax.add_patch(patches.Rectangle((LG_X-3, LG_Y-len(legend_items)*12-6), 88, len(legend_items)*12+18,
    facecolor="#f8f4ec", edgecolor="#808070", lw=0.8, zorder=18))
ax.text(LG_X+38, LG_Y, "LEGEND", ha='center', fontsize=6.5, fontweight='bold',
        color="#303030", fontfamily='monospace', zorder=19)
for i, (col, label, lw, ls) in enumerate(legend_items):
    y = LG_Y - 10 - i*11
    ax.plot([LG_X, LG_X+22], [y,y], color=col, lw=lw, linestyle=ls, zorder=19)
    ax.text(LG_X+25, y, label, va='center', fontsize=5.5, color="#303030",
            fontfamily='monospace', zorder=19)

# ── North arrow ───────────────────────────────────────────────────────────────
na_x, na_y = LG_X+38, SITE_Y+18
ax.annotate('', xy=(na_x,na_y+22), xytext=(na_x,na_y),
    arrowprops=dict(arrowstyle='-|>',color='#303030',lw=1.4,mutation_scale=12), zorder=20)
ax.text(na_x, na_y+26, 'N', ha='center', va='bottom', fontsize=10, fontweight='bold',
        color='#303030', fontfamily='monospace', zorder=20)
# Circle around arrow base
ax.add_patch(patches.Circle((na_x, na_y+11), 14, edgecolor='#303030',
    facecolor='none', lw=0.9, zorder=20))

# ── Scale bar ─────────────────────────────────────────────────────────────────
sb_x = LG_X; sb_y = SITE_Y+52
for i, seg_len in enumerate([0, 25, 50]):
    sx = sb_x + i*25
    ex = sx + 25
    ax.fill_between([sx,ex],[sb_y-3,sb_y-3],[sb_y+3,sb_y+3],
        color='#303030' if i%2==0 else '#f0ece0', zorder=20)
    ax.plot([sx,sx],[sb_y-5,sb_y+5],color='#303030',lw=0.8,zorder=21)
ax.plot([sb_x+75,sb_x+75],[sb_y-5,sb_y+5],color='#303030',lw=0.8,zorder=21)
ax.text(sb_x, sb_y-9, "0", ha='center', fontsize=5, color='#303030', fontfamily='monospace')
ax.text(sb_x+50, sb_y-9, "50'", ha='center', fontsize=5, color='#303030', fontfamily='monospace')
ax.text(sb_x+37.5, sb_y+7, "SCALE: 1\" = 10'", ha='center', fontsize=5.5,
        color='#303030', fontfamily='monospace')

# ── Axis limits and styling ───────────────────────────────────────────────────
ax.set_xlim(-5, 360)
ax.set_ylim(-50, 290)
ax.tick_params(labelsize=5.5, labelcolor='#707070')
for sp in ax.spines.values():
    sp.set_edgecolor('#303030'); sp.set_linewidth(1.0)
ax.grid(color="#d8d4cc", linewidth=0.25, alpha=0.7, linestyle='--')

ax.set_title("NATIONAL SITE PLAN  —  CIVIL INFRASTRUCTURE  |  70+ LAYERS",
    fontsize=9, fontfamily='monospace', color='#303030', pad=6)

fig.text(0.98, 0.01,
    "ARIA-OS  |  DWG: CS-NATL-001  |  SCALE 1\"=10'  |  DATE: 2026-04-10  |  REV A",
    ha='right', va='bottom', fontsize=5.5, color='#707070', fontfamily='monospace')

plt.savefig(str(OUT / "civil_site_plan.png"), dpi=110,
            bbox_inches='tight', facecolor=BG)
plt.close()
print(f"Civil v3: {(OUT/'civil_site_plan.png').stat().st_size:,} bytes")
