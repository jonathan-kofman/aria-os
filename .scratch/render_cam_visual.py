"""
CAM toolpath visualization — brake drum with overlaid NC toolpaths.
Renders the 3D part with color-coded tool passes per operation.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from mpl_toolkits.mplot3d.art3d import Poly3DCollection, Line3DCollection
from pathlib import Path
import trimesh

OUT = Path("outputs/gallery_renders")

# ── Brake drum geometry (Ø200×60mm, bore Ø80) ────────────────────────────────
OD, H, BORE = 100.0, 60.0, 40.0  # radii/heights in mm (OD=radius=100 for Ø200)
MOUNT_R = 75.0  # bolt hole radius

# ── Generate toolpaths ────────────────────────────────────────────────────────

def op1_adaptive_paths():
    """3D Adaptive clearing — EM-12. Spiral inward at multiple Z levels."""
    paths = []
    z_levels = np.linspace(H, 5, 8)       # rough Z levels from top
    for z in z_levels:
        stock_r = OD + 10 + (H - z) * 0.05  # stock slightly larger
        for r in np.linspace(stock_r, OD + 1.5, 5):  # radial passes
            theta = np.linspace(0, 2*np.pi, 80)
            x = r * np.cos(theta)
            y = r * np.sin(theta)
            zz = np.full_like(theta, z)
            paths.append(np.column_stack([x, y, zz]))
    return paths

def op2_parallel_finish_paths():
    """Parallel finish passes — EM-3. Raster over top face."""
    paths = []
    z = H - 0.5   # finish depth
    for y in np.linspace(-OD+5, OD-5, 18):
        x1 = -np.sqrt(max(OD**2 - y**2, 0)) + 2
        x2 =  np.sqrt(max(OD**2 - y**2, 0)) - 2
        if x2 > x1 + 5:
            x = np.linspace(x1, x2, 40)
            yy = np.full_like(x, y)
            zz = np.full_like(x, z)
            paths.append(np.column_stack([x, yy, zz]))
    return paths

def op3_contour_paths():
    """Contour pass — BN-6. Follow outer profile at multiple depths."""
    paths = []
    for z in [H*0.8, H*0.5, H*0.2, 2.0]:
        theta = np.linspace(0, 2*np.pi+0.1, 120)
        r = OD + 0.3
        x = r * np.cos(theta)
        y = r * np.sin(theta)
        zz = np.full_like(theta, z)
        paths.append(np.column_stack([x, y, zz]))
    return paths

# ── Load or build brake drum mesh ─────────────────────────────────────────────
def build_drum():
    """Build a brake drum cylinder mesh for visualization."""
    # Outer cylinder
    theta = np.linspace(0, 2*np.pi, 64)
    # Top face
    top_x = OD * np.cos(theta)
    top_y = OD * np.sin(theta)
    verts = []
    faces = []

    # Outer wall quads -> triangles
    for i in range(len(theta)-1):
        a, b = theta[i], theta[i+1]
        # outer wall
        p0 = [OD*np.cos(a), OD*np.sin(a), 0]
        p1 = [OD*np.cos(b), OD*np.sin(b), 0]
        p2 = [OD*np.cos(b), OD*np.sin(b), H]
        p3 = [OD*np.cos(a), OD*np.sin(a), H]
        idx = len(verts)
        verts.extend([p0, p1, p2, p3])
        faces.extend([[idx,idx+1,idx+2],[idx,idx+2,idx+3]])

        # Inner bore wall
        p0 = [BORE*np.cos(a), BORE*np.sin(a), 0]
        p1 = [BORE*np.cos(b), BORE*np.sin(b), 0]
        p2 = [BORE*np.cos(b), BORE*np.sin(b), H]
        p3 = [BORE*np.cos(a), BORE*np.sin(a), H]
        idx = len(verts)
        verts.extend([p0, p1, p2, p3])
        faces.extend([[idx,idx+2,idx+1],[idx,idx+3,idx+2]])

    # Top annular face
    for i in range(len(theta)-1):
        a, b = theta[i], theta[i+1]
        p0 = [BORE*np.cos(a), BORE*np.sin(a), H]
        p1 = [BORE*np.cos(b), BORE*np.sin(b), H]
        p2 = [OD*np.cos(b), OD*np.sin(b), H]
        p3 = [OD*np.cos(a), OD*np.sin(a), H]
        idx = len(verts)
        verts.extend([p0, p1, p2, p3])
        faces.extend([[idx,idx+1,idx+2],[idx,idx+2,idx+3]])

    return np.array(verts), np.array(faces)

verts, faces = build_drum()

# ── Plot ──────────────────────────────────────────────────────────────────────
fig = plt.figure(figsize=(8, 6.4), dpi=120)
fig.patch.set_facecolor("#0d1117")
ax3d = fig.add_subplot(111, projection='3d')
ax3d.set_facecolor("#0d1117")

# Draw mesh faces
face_verts = verts[faces]  # (N, 3, 3)
mesh_col = Poly3DCollection(face_verts, alpha=0.22, zorder=1)
mesh_col.set_facecolor("#8899aa")
mesh_col.set_edgecolor("#aabbcc")
mesh_col.set_linewidth(0.1)
ax3d.add_collection3d(mesh_col)

# Draw toolpath operations
op_config = [
    (op1_adaptive_paths(), "#ff6b35", "Op1  Adaptive   EM-Ø12  (roughing)",  0.8, True),
    (op2_parallel_finish_paths(), "#3fb950", "Op2  Par. Finish EM-Ø3   (finishing)", 1.2, False),
    (op3_contour_paths(), "#58a6ff", "Op3  Contour    BN-Ø6   (contour)",   1.5, False),
]

legend_handles = []
for paths, color, label, lw, first_only in op_config:
    added_label = False
    for path in paths:
        xs, ys, zs = path[:,0], path[:,1], path[:,2]
        lbl = label if not added_label else None
        line, = ax3d.plot(xs, ys, zs, color=color, linewidth=lw*0.6,
                         alpha=0.85, zorder=5, label=lbl)
        added_label = True

# Tool position indicator (at end of last contour pass)
last = op3_contour_paths()[-1]
tool_x, tool_y, tool_z = last[-1,0], last[-1,1], last[-1,2]
ax3d.scatter([tool_x], [tool_y], [tool_z+2], color='white', s=30, zorder=10, marker='o')
ax3d.plot([tool_x, tool_x], [tool_y, tool_y], [tool_z+2, tool_z+12],
         color='white', lw=1.0, zorder=10)  # tool shank
ax3d.text(tool_x+8, tool_y+8, tool_z+14, "BN-6\nφ6mm", fontsize=5.5,
         color="white", fontfamily='monospace')

# ── Annotation panel (top-left inset) ────────────────────────────────────────
info_lines = [
    ("ARIA-BRAKE-DRUM-001", "#e6edf3", 8, True),
    ("Steel 1045  |  Ø200×60mm", "#8b949e", 6.5, False),
    ("", "#8b949e", 6, False),
    ("Op 1  Adaptive     EM-12   2425rpm  727mm/min", "#ff6b35", 6, False),
    ("Op 2  Par. Finish  EM-3    9702rpm 1940mm/min", "#3fb950", 6, False),
    ("Op 3  Contour      BN-6    4851rpm  970mm/min", "#58a6ff", 6, False),
    ("", "#8b949e", 6, False),
    ("Est. cycle time: 47 min   Setup: 15 min", "#8b949e", 5.5, False),
]
y_start = 0.97
for text, color, size, bold in info_lines:
    fig.text(0.02, y_start, text, color=color, fontsize=size,
             fontfamily='monospace', fontweight='bold' if bold else 'normal',
             va='top', transform=fig.transFigure)
    y_start -= 0.046 if size >= 6.5 else 0.040

# ── Axis styling ──────────────────────────────────────────────────────────────
ax3d.set_xlim(-OD-15, OD+15)
ax3d.set_ylim(-OD-15, OD+15)
ax3d.set_zlim(-5, H+20)
ax3d.view_init(elev=28, azim=-55)

ax3d.set_xlabel("X (mm)", color="#484f58", fontsize=7, fontfamily='monospace')
ax3d.set_ylabel("Y (mm)", color="#484f58", fontsize=7, fontfamily='monospace')
ax3d.set_zlabel("Z (mm)", color="#484f58", fontsize=7, fontfamily='monospace')

ax3d.tick_params(colors="#30363d", labelsize=6, labelcolor="#484f58")
ax3d.xaxis.pane.fill = False
ax3d.yaxis.pane.fill = False
ax3d.zaxis.pane.fill = False
ax3d.xaxis.pane.set_edgecolor("#21262d")
ax3d.yaxis.pane.set_edgecolor("#21262d")
ax3d.zaxis.pane.set_edgecolor("#21262d")
ax3d.grid(True, alpha=0.15, color="#21262d")

ax3d.set_title("CAM Toolpath Visualization — Brake Drum",
               color="#8b949e", fontsize=9, pad=8, fontfamily='monospace')

plt.tight_layout(pad=0.5)
out = OUT / "cam_toolpath_visual.png"
plt.savefig(str(out), dpi=120, bbox_inches='tight', facecolor=fig.get_facecolor())
plt.close()
print(f"CAM visual: {out.stat().st_size:,} bytes")
