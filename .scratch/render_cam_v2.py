"""
CAM toolpath visualization v2 — professional layout.
Left sidebar: operation list + tool specs. Right: 3D part + paths.
No matplotlib axes box. Clean dark theme matching Fusion 360 / HSMWorks style.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from mpl_toolkits.mplot3d import Axes3D
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from pathlib import Path

OUT = Path("outputs/gallery_renders")
BG  = "#080b10"
PANEL = "#0d1117"

# Brake drum geometry
OD, H_DRUM, BORE = 100.0, 60.0, 40.0

# ── Toolpath generators ───────────────────────────────────────────────────────
def op1_adaptive():
    paths = []
    for z in np.linspace(H_DRUM, 6, 10):
        stock_r = OD + 8
        for r in np.linspace(stock_r, OD + 1.5, 6):
            theta = np.linspace(0, 2*np.pi, 90)
            paths.append(np.column_stack([r*np.cos(theta), r*np.sin(theta), np.full(90, z)]))
    return paths

def op2_finish():
    paths = []
    z = H_DRUM - 0.3
    for y in np.linspace(-OD+4, OD-4, 22):
        x1 = -np.sqrt(max(OD**2 - y**2, 0)) + 2
        x2 =  np.sqrt(max(OD**2 - y**2, 0)) - 2
        if x2 > x1 + 4:
            x = np.linspace(x1, x2, 50)
            paths.append(np.column_stack([x, np.full(50, y), np.full(50, z)]))
    return paths

def op3_contour():
    paths = []
    for z in [H_DRUM*0.82, H_DRUM*0.55, H_DRUM*0.28, 2.0]:
        theta = np.linspace(0, 2*np.pi+0.05, 140)
        r = OD + 0.25
        paths.append(np.column_stack([r*np.cos(theta), r*np.sin(theta), np.full(140, z)]))
    return paths

def op4_bore():
    """Bore finishing — inner bore surface."""
    paths = []
    for z in np.linspace(H_DRUM-2, 2, 8):
        theta = np.linspace(0, 2*np.pi, 60)
        r = BORE + 0.2
        paths.append(np.column_stack([r*np.cos(theta), r*np.sin(theta), np.full(60, z)]))
    return paths

# ── Build drum mesh ────────────────────────────────────────────────────────────
theta = np.linspace(0, 2*np.pi, 72)
verts, faces = [], []
for i in range(len(theta)-1):
    a, b = theta[i], theta[i+1]
    for R in [OD, BORE]:
        p0 = [R*np.cos(a), R*np.sin(a), 0]
        p1 = [R*np.cos(b), R*np.sin(b), 0]
        p2 = [R*np.cos(b), R*np.sin(b), H_DRUM]
        p3 = [R*np.cos(a), R*np.sin(a), H_DRUM]
        idx = len(verts)
        verts.extend([p0,p1,p2,p3])
        if R == OD:
            faces.extend([[idx,idx+1,idx+2],[idx,idx+2,idx+3]])
        else:
            faces.extend([[idx,idx+2,idx+1],[idx,idx+3,idx+2]])
    # Top annular
    p0 = [BORE*np.cos(a), BORE*np.sin(a), H_DRUM]
    p1 = [BORE*np.cos(b), BORE*np.sin(b), H_DRUM]
    p2 = [OD*np.cos(b), OD*np.sin(b), H_DRUM]
    p3 = [OD*np.cos(a), OD*np.sin(a), H_DRUM]
    idx = len(verts)
    verts.extend([p0,p1,p2,p3])
    faces.extend([[idx,idx+1,idx+2],[idx,idx+2,idx+3]])
verts = np.array(verts)
faces = np.array(faces)

# ── Layout: sidebar (30%) + 3D view (70%) ────────────────────────────────────
fig = plt.figure(figsize=(12, 7.5), dpi=120)
fig.patch.set_facecolor(BG)

# Sidebar axis (2D)
ax_side = fig.add_axes([0.00, 0.00, 0.28, 1.00])
ax_side.set_facecolor(PANEL)
ax_side.set_xlim(0,1); ax_side.set_ylim(0,1); ax_side.axis('off')

# 3D view axis
ax3d = fig.add_axes([0.28, 0.04, 0.71, 0.93], projection='3d')
ax3d.set_facecolor(BG)

# ── Sidebar contents ──────────────────────────────────────────────────────────
# Vertical separator
ax_side.add_patch(patches.Rectangle((0.97,0),0.03,1, facecolor="#0a1018", edgecolor="none"))
ax_side.plot([0.97,0.97],[0,1], color="#1c2535", lw=0.8)

# Program header
ax_side.text(0.08, 0.965, "CAM OPERATIONS", ha='left', va='top',
    fontsize=9, fontweight='bold', color="#c9d1d9", fontfamily='monospace')
ax_side.text(0.08, 0.945, "ARIA-BRAKE-DRUM-001", ha='left', va='top',
    fontsize=7, color="#58a6ff", fontfamily='monospace')
ax_side.plot([0.04,0.94],[0.935,0.935], color="#1c2535", lw=0.7)

# Part info
info = [
    ("Material", "Steel 1045"),
    ("Blank",    "Ø210 × 65mm"),
    ("Setup",    "3-jaw chuck + centre"),
    ("Machine",  "3-axis VMC"),
    ("WCS",      "G54  Z0=top face"),
]
y0 = 0.918
for k, v in info:
    ax_side.text(0.08, y0, k, ha='left', va='top', fontsize=6, color="#484f58", fontfamily='monospace')
    ax_side.text(0.58, y0, v, ha='left', va='top', fontsize=6, color="#8b949e", fontfamily='monospace')
    y0 -= 0.028

ax_side.plot([0.04,0.94],[y0+0.010,y0+0.010], color="#1c2535", lw=0.7)
y0 -= 0.008

# Operation blocks
ops = [
    ("#ff6b35", "01", "3D Adaptive Clear",  "EM-12  AL/TiN",  "2,425 rpm",   "727 mm/min",  "2.50mm ae",  "5.00mm ap",  True),
    ("#3fb950", "02", "Parallel Finish",     "EM-3   AL/TiN",  "9,702 rpm",  "1,940 mm/min", "0.25mm ae",  "0.50mm ap",  True),
    ("#58a6ff", "03", "2D Contour",          "BN-6   TiAlN",  "4,851 rpm",   "970 mm/min",   "0.30mm ae",  "60mm ap",    True),
    ("#d2a8ff", "04", "Bore Finish",         "BN-6   TiAlN",  "4,851 rpm",   "485 mm/min",   "0.15mm ae",  "60mm ap",    True),
]

for color, opnum, name, tool, rpm, feed, ae, ap, active in ops:
    # Operation header strip
    ax_side.add_patch(patches.Rectangle((0.04, y0-0.004), 0.90, 0.046,
        facecolor="#0d1117", edgecolor=color, lw=0.8, alpha=0.9))
    # Color indicator bar on left
    ax_side.add_patch(patches.Rectangle((0.04, y0-0.004), 0.018, 0.046,
        facecolor=color, edgecolor="none", alpha=0.85))
    # Op number + name
    ax_side.text(0.08, y0+0.028, f"OP {opnum}", ha='left', va='top',
        fontsize=6, fontweight='bold', color=color, fontfamily='monospace')
    ax_side.text(0.19, y0+0.028, name, ha='left', va='top',
        fontsize=6.5, color="#c9d1d9", fontfamily='monospace')
    # Tool
    ax_side.text(0.08, y0+0.010, tool, ha='left', va='top',
        fontsize=5.5, color="#484f58", fontfamily='monospace')
    # Speed / feed on right
    ax_side.text(0.58, y0+0.028, rpm, ha='left', va='top',
        fontsize=5.5, color="#8b949e", fontfamily='monospace')
    ax_side.text(0.58, y0+0.010, feed, ha='left', va='top',
        fontsize=5.5, color="#8b949e", fontfamily='monospace')
    # Step-over / step-down
    ax_side.text(0.08, y0-0.006, f"ae {ae}  ap {ap}", ha='left', va='top',
        fontsize=5, color="#30363d", fontfamily='monospace')
    y0 -= 0.068
    ax_side.plot([0.04,0.94],[y0+0.012,y0+0.012], color="#161b22", lw=0.4)

y0 -= 0.004
ax_side.plot([0.04,0.94],[y0+0.005,y0+0.005], color="#1c2535", lw=0.7)
y0 -= 0.012

# Cycle time summary
ax_side.text(0.08, y0, "CYCLE TIME ESTIMATE", ha='left', va='top',
    fontsize=7, fontweight='bold', color="#58a6ff", fontfamily='monospace')
y0 -= 0.030
time_rows = [
    ("Op 01  Adaptive:   ", "18 min"),
    ("Op 02  Finish:     ", "11 min"),
    ("Op 03  Contour:    ",  "8 min"),
    ("Op 04  Bore:       ",  "6 min"),
    ("Setup + load:      ", "15 min"),
]
for label, val in time_rows:
    ax_side.text(0.08, y0, label, ha='left', va='top',
        fontsize=6, color="#484f58", fontfamily='monospace')
    ax_side.text(0.72, y0, val, ha='left', va='top',
        fontsize=6, color="#8b949e", fontfamily='monospace')
    y0 -= 0.024

ax_side.plot([0.04,0.94],[y0+0.012,y0+0.012], color="#1c2535", lw=0.5)
y0 -= 0.006
ax_side.text(0.08, y0, "TOTAL: ", ha='left', va='top',
    fontsize=7, fontweight='bold', color="#c9d1d9", fontfamily='monospace')
ax_side.text(0.38, y0, "58 min", ha='left', va='top',
    fontsize=7, fontweight='bold', color="#3fb950", fontfamily='monospace')

# ── 3D Plot ────────────────────────────────────────────────────────────────────
# Part mesh (semi-transparent)
face_verts = verts[faces]
mesh_col = Poly3DCollection(face_verts, alpha=0.28, zorder=1)
mesh_col.set_facecolor("#7a8fa8")
mesh_col.set_edgecolor("#4a6070")
mesh_col.set_linewidth(0.05)
ax3d.add_collection3d(mesh_col)

# Toolpaths
op_styles = [
    (op1_adaptive(), "#ff6b35", 0.55, "Op1 Adaptive"),
    (op2_finish(),   "#3fb950", 0.90, "Op2 Finish"),
    (op3_contour(),  "#58a6ff", 1.0,  "Op3 Contour"),
    (op4_bore(),     "#d2a8ff", 0.80, "Op4 Bore"),
]
for paths, color, alpha, label in op_styles:
    first = True
    for path in paths:
        ax3d.plot(path[:,0], path[:,1], path[:,2],
                 color=color, linewidth=0.55, alpha=alpha,
                 label=label if first else None, zorder=5)
        first = False

# Tool indicator at last point
last_path = op3_contour()[-1]
tx, ty, tz = last_path[-1]
# Tool shank
ax3d.plot([tx,tx],[ty,ty],[tz,tz+18], color='#e8e8e8', lw=1.5, zorder=10)
# Flute tip
ax3d.scatter([tx],[ty],[tz], color='#ffffff', s=25, zorder=11, marker='o')
ax3d.text(tx+10, ty+10, tz+20, "BN-6\nφ6", fontsize=5.5,
         color="#c9d1d9", fontfamily='monospace')

# ── Axis styling ───────────────────────────────────────────────────────────────
ax3d.set_xlim(-OD-12, OD+12)
ax3d.set_ylim(-OD-12, OD+12)
ax3d.set_zlim(-5, H_DRUM+25)
ax3d.view_init(elev=30, azim=-50)

ax3d.set_xlabel("X", color="#2a3545", fontsize=7, fontfamily='monospace')
ax3d.set_ylabel("Y", color="#2a3545", fontsize=7, fontfamily='monospace')
ax3d.set_zlabel("Z", color="#2a3545", fontsize=7, fontfamily='monospace')

ax3d.tick_params(colors="#1c2535", labelsize=5.5, labelcolor="#2a3545")
ax3d.xaxis.pane.fill = False; ax3d.yaxis.pane.fill = False; ax3d.zaxis.pane.fill = False
ax3d.xaxis.pane.set_edgecolor("#131820"); ax3d.yaxis.pane.set_edgecolor("#131820"); ax3d.zaxis.pane.set_edgecolor("#131820")
ax3d.grid(True, alpha=0.08, color="#1c2535", linewidth=0.5)

# Legend (top-right of 3D area, minimal)
handles = [plt.Line2D([0],[0], color=c, lw=1.5, label=l)
           for _, c, _, l in op_styles]
leg = ax3d.legend(handles=handles, loc='upper right', fontsize=6,
                  facecolor='#0d1117', edgecolor='#1c2535',
                  labelcolor='#8b949e', framealpha=0.9)
leg.get_frame().set_linewidth(0.6)

plt.savefig(str(OUT / "cam_toolpath_visual.png"), dpi=120,
            bbox_inches='tight', facecolor=BG)
plt.close()
print(f"CAM v2: {(OUT/'cam_toolpath_visual.png').stat().st_size:,} bytes")
