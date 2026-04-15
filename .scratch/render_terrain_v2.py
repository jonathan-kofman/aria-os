"""
Terrain render v2: hillshaded elevation map with dramatic 3D lighting.
Uses matplotlib LightSource for realistic terrain appearance.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.colors import LightSource
from pathlib import Path

OUT = Path("outputs/gallery_renders")
STL = "outputs/terrain/mountain_terrain_3km_x_3km_with_150m_pea_mesh.stl"

mesh = trimesh.load(STL)
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

verts = mesh.vertices
idx = np.lexsort((verts[:, 1], verts[:, 0]))
vs = verts[idx]
n = int(round(np.sqrt(len(vs))))   # 257
Z = vs[:, 2].reshape(n, n)

x = vs[:n, 0]
y = vs[::n, 1]
X, Y = np.meshgrid(x, y, indexing='ij')

# ── Custom terrain colormap ──────────────────────────────────────────────────
terrain_colors = [
    (0.15, 0.28, 0.12),   # deep forest green
    (0.26, 0.44, 0.18),   # forest
    (0.42, 0.58, 0.26),   # meadow
    (0.62, 0.60, 0.38),   # subalpine
    (0.78, 0.73, 0.58),   # rock
    (0.91, 0.90, 0.88),   # snow
    (0.99, 0.99, 1.00),   # peak snow
]
cmap = mcolors.LinearSegmentedColormap.from_list("terrain_hd", terrain_colors, N=512)

# ── Hillshading with LightSource ─────────────────────────────────────────────
ls = LightSource(azdeg=315, altdeg=40)    # NW sun, medium altitude
shaded = ls.shade(Z.T, cmap=cmap, vmin=0, vmax=155,
                  vert_exag=4.0, blend_mode='overlay')

# ── Plot ─────────────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(8, 6.4), dpi=120)
fig.patch.set_facecolor("#0d1117")
ax.set_facecolor("#0d1117")

im = ax.imshow(shaded, origin="lower", extent=[0, 3000, 0, 3000],
               interpolation='bilinear')

# Contour lines at 25m intervals — subtle
ax.contour(x, y, Z.T, levels=np.arange(0, 160, 25),
           colors="#000000", alpha=0.22, linewidths=0.4)

# Index contours at 50m — slightly bolder
ax.contour(x, y, Z.T, levels=np.arange(0, 160, 50),
           colors="#000000", alpha=0.38, linewidths=0.7)

# ── Colorbar ─────────────────────────────────────────────────────────────────
sm = plt.cm.ScalarMappable(cmap=cmap, norm=mcolors.Normalize(0, 155))
sm.set_array([])
cbar = plt.colorbar(sm, ax=ax, fraction=0.028, pad=0.02)
cbar.set_label("Elevation (m)", color="#8b949e", fontsize=8, fontfamily="monospace")
cbar.ax.yaxis.set_tick_params(color="#8b949e", labelsize=7, labelcolor="#8b949e")
cbar.outline.set_edgecolor("#30363d")

# ── Labels ───────────────────────────────────────────────────────────────────
ax.set_xlabel("Easting (m)", color="#484f58", fontsize=8, fontfamily="monospace")
ax.set_ylabel("Northing (m)", color="#484f58", fontsize=8, fontfamily="monospace")
ax.tick_params(colors="#30363d", labelsize=7, labelcolor="#484f58")
for spine in ax.spines.values():
    spine.set_color("#21262d")
ax.set_title("3km × 3km Mountain Terrain  |  150m peak elevation",
             color="#8b949e", fontsize=9, pad=8, fontfamily="monospace")

# ── Peak label ───────────────────────────────────────────────────────────────
peak_idx = np.unravel_index(Z.argmax(), Z.shape)
px, py = x[peak_idx[0]], y[peak_idx[1]]
ax.annotate("▲ 150m", xy=(px, py), xytext=(px + 180, py + 180),
            color="#e6edf3", fontsize=7, fontfamily="monospace",
            arrowprops=dict(arrowstyle="-", color="#484f58", lw=0.7))

plt.tight_layout(pad=0.8)
out = OUT / "terrain.png"
plt.savefig(str(out), dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close()
print(f"Terrain v2: {out.stat().st_size:,} bytes")
