"""Render terrain as topographic elevation map (matplotlib) — avoids GL camera issues."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib import cm
from pathlib import Path

OUT = Path("outputs/gallery_renders")

stl = "outputs/terrain/mountain_terrain_3km_x_3km_with_150m_pea_mesh.stl"
mesh = trimesh.load(stl)
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))

verts = mesh.vertices  # (66049, 3)
# Grid is 257×257 — sort vertices to reconstruct height map
# Vertices are [x, y, z]. Sort by x then y.
idx = np.lexsort((verts[:, 1], verts[:, 0]))
verts_sorted = verts[idx]
n = int(round(np.sqrt(len(verts_sorted))))  # 257
assert n * n == len(verts_sorted), f"Not a square grid: {len(verts_sorted)}"

Z = verts_sorted[:, 2].reshape(n, n)  # height map
X = verts_sorted[:, 0].reshape(n, n)
Y = verts_sorted[:, 1].reshape(n, n)

# Custom terrain colormap: deep green → light green → tan → white peaks
colors_terrain = [
    (0.25, 0.40, 0.20),   # 0m   — dark forest green
    (0.35, 0.55, 0.25),   # 30m  — mid green
    (0.55, 0.65, 0.30),   # 70m  — light green-yellow
    (0.72, 0.65, 0.45),   # 100m — tan/rocky
    (0.88, 0.84, 0.76),   # 130m — pale stone
    (0.97, 0.97, 0.97),   # 150m — snow white
]
cmap = mcolors.LinearSegmentedColormap.from_list("terrain_custom", colors_terrain)

fig, ax = plt.subplots(figsize=(8, 6), dpi=100)
fig.patch.set_facecolor("#0d1117")
ax.set_facecolor("#0d1117")

# Elevation color fill
im = ax.imshow(Z.T, origin="lower",
               extent=[0, 3000, 0, 3000],
               cmap=cmap, vmin=0, vmax=150,
               interpolation="bilinear",
               aspect="equal")

# Contour lines at 25m intervals
contour_levels = np.arange(0, 155, 25)
cs = ax.contour(X[0, :], Y[:, 0], Z.T,
                levels=contour_levels,
                colors="black", alpha=0.35, linewidths=0.6)

# Labels and styling
ax.set_xlabel("X (m)", color="#8b949e", fontsize=9)
ax.set_ylabel("Y (m)", color="#8b949e", fontsize=9)
ax.set_title("3km × 3km Mountain Terrain  |  150m peak elevation",
             color="#e6edf3", fontsize=10, pad=8)
ax.tick_params(colors="#8b949e", labelsize=8)
for spine in ax.spines.values():
    spine.set_color("#30363d")

# Colorbar
cbar = fig.colorbar(im, ax=ax, shrink=0.75, pad=0.02)
cbar.ax.tick_params(colors="#8b949e", labelsize=8)
cbar.set_label("Elevation (m)", color="#8b949e", fontsize=9)
cbar.outline.set_color("#30363d")

plt.tight_layout(pad=0.5)
out = OUT / "terrain.png"
plt.savefig(str(out), dpi=100, bbox_inches="tight",
            facecolor=fig.get_facecolor())
plt.close()
print(f"terrain.png: {out.stat().st_size:,} bytes")
