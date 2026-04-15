"""
Terrain render v3 — professional GIS/DEM style.
Left: topographic hillshade map with contours, scale, north arrow, grid.
Right: 3D oblique surface with proper elevation colormap.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.ticker as mticker
from matplotlib.colors import LightSource
from matplotlib.patches import FancyArrowPatch
from mpl_toolkits.mplot3d import Axes3D
from pathlib import Path

OUT = Path("outputs/gallery_renders")
STL = "outputs/terrain/mountain_terrain_3km_x_3km_with_150m_pea_mesh.stl"

# ── Load heightmap from STL ───────────────────────────────────────────────────
mesh = trimesh.load(STL)
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
verts = mesh.vertices
idx   = np.lexsort((verts[:, 1], verts[:, 0]))
vs    = verts[idx]
n     = int(round(np.sqrt(len(vs))))  # 257
Z     = vs[:, 2].reshape(n, n)        # elevation grid
# After lexsort(y,x): first n rows have x=X[0], varying y.
# So: X values = every n-th row's x; Y values = first n rows' y.
X0    = vs[::n, 0]  # x coords: 0, 11.7, ..., 3000
Y0    = vs[:n, 1]   # y coords: 0, 11.7, ..., 3000
# Extent in meters
x_ext = float(X0[-1] - X0[0])
y_ext = float(Y0[-1] - Y0[0])

# ── Professional elevation colormap ──────────────────────────────────────────
# USGS-style: deep water blue → green valley → tan hills → gray rock → snow
terrain_colors = [
    (0.10, 0.22, 0.42),  # deep water (lowest)
    (0.22, 0.48, 0.38),  # riparian / lowland
    (0.35, 0.58, 0.32),  # grassland
    (0.55, 0.65, 0.38),  # forest transition
    (0.72, 0.67, 0.42),  # subalpine meadow
    (0.68, 0.62, 0.52),  # scree / rock
    (0.82, 0.80, 0.78),  # frost / near-peak
    (0.97, 0.97, 0.99),  # snow cap
]
cmap = mcolors.LinearSegmentedColormap.from_list("dem_pro", terrain_colors, N=512)

# LightSource: azimuth 320 (NW), altitude 35 (low-angle dramatic)
ls = LightSource(azdeg=320, altdeg=35)
Z_min, Z_max = float(Z.min()), float(Z.max())

# ── Figure layout ─────────────────────────────────────────────────────────────
BG = "#0a0e14"
fig = plt.figure(figsize=(14, 7.5), dpi=120)
fig.patch.set_facecolor(BG)

# Left: topographic map (55%)
ax_map = fig.add_axes([0.02, 0.08, 0.50, 0.86])
ax_map.set_facecolor(BG)

# Right: 3D perspective (42%)
ax3d = fig.add_axes([0.54, 0.05, 0.44, 0.90], projection='3d')
ax3d.set_facecolor(BG)

# ── Left: Topographic map ─────────────────────────────────────────────────────
shaded = ls.shade(Z.T, cmap=cmap, vmin=Z_min - 5, vmax=Z_max + 5,
                  vert_exag=4.5, blend_mode='overlay')

im = ax_map.imshow(shaded, origin="lower",
                   extent=[0, x_ext, 0, y_ext],
                   interpolation='bilinear', aspect='equal')

# Contour lines: thin at 10m, bold at 50m
x1d = np.linspace(0, x_ext, n)
y1d = np.linspace(0, y_ext, n)
levels_minor = np.arange(0, Z_max + 10, 10)
levels_major = np.arange(0, Z_max + 50, 50)

cs_minor = ax_map.contour(x1d, y1d, Z.T,
    levels=levels_minor, colors="#000000",
    alpha=0.18, linewidths=0.3, linestyles='solid')

cs_major = ax_map.contour(x1d, y1d, Z.T,
    levels=levels_major, colors="#000000",
    alpha=0.45, linewidths=0.7, linestyles='solid')

# Label major contours
ax_map.clabel(cs_major, fmt="%dm", fontsize=5, colors="#1a1a1a",
              inline=True, inline_spacing=3)

# Tick styling (coordinate grid)
ax_map.set_xlabel("Easting (m)", color="#6a7888", fontsize=7, fontfamily='monospace', labelpad=3)
ax_map.set_ylabel("Northing (m)", color="#6a7888", fontsize=7, fontfamily='monospace', labelpad=3)
ax_map.tick_params(colors="#30363d", labelsize=6, labelcolor="#5a6878")
for sp in ax_map.spines.values():
    sp.set_edgecolor("#1c2535"); sp.set_linewidth(0.8)
ax_map.grid(color="#1c2535", linewidth=0.4, alpha=0.6)

# Colorbar
cb_ax = fig.add_axes([0.527, 0.12, 0.012, 0.74])
sm = plt.cm.ScalarMappable(cmap=cmap,
                            norm=mcolors.Normalize(vmin=Z_min, vmax=Z_max))
sm.set_array([])
cb = fig.colorbar(sm, cax=cb_ax, orientation='vertical')
cb.ax.tick_params(labelsize=6, colors="#5a6878")
cb.ax.yaxis.set_tick_params(color="#30363d")
cb.outline.set_edgecolor("#1c2535")
cb.set_label("Elevation (m)", color="#5a6878", fontsize=6, fontfamily='monospace',
             rotation=90, labelpad=8)

# North arrow
arr_x, arr_y = 0.90, 0.88
ax_map.annotate("", xy=(arr_x*x_ext, (arr_y+0.06)*y_ext),
                xytext=(arr_x*x_ext, arr_y*y_ext),
                xycoords='data',
                arrowprops=dict(arrowstyle="-|>", color="#dde4ec",
                                lw=1.2, mutation_scale=12))
ax_map.text(arr_x*x_ext, (arr_y+0.08)*y_ext, "N",
            ha='center', va='bottom', fontsize=8, fontweight='bold',
            color="#dde4ec", fontfamily='monospace')

# Scale bar (500m)
sb_x0, sb_y = 0.05*x_ext, 0.04*y_ext
sb_len = 500.0
ax_map.plot([sb_x0, sb_x0+sb_len], [sb_y, sb_y],
            color="#dde4ec", lw=2.5, solid_capstyle='butt')
ax_map.plot([sb_x0, sb_x0], [sb_y-20, sb_y+20], color="#dde4ec", lw=1.5)
ax_map.plot([sb_x0+sb_len, sb_x0+sb_len], [sb_y-20, sb_y+20], color="#dde4ec", lw=1.5)
ax_map.text(sb_x0 + sb_len/2, sb_y + 40, "500 m",
            ha='center', fontsize=6, color="#dde4ec", fontfamily='monospace')

# Peak annotation (find highest point)
peak_idx = np.unravel_index(Z.argmax(), Z.shape)
peak_x = x1d[peak_idx[0]]
peak_y = y1d[peak_idx[1]]
ax_map.annotate(f"  {Z_max:.0f}m peak",
    xy=(peak_x, peak_y), xytext=(peak_x - 200, peak_y - 250),
    fontsize=6, color="#dde4ec", fontfamily='monospace',
    arrowprops=dict(arrowstyle="-", color="#aab8c8", lw=0.7))
ax_map.scatter([peak_x], [peak_y], s=12, c='#ffffff', zorder=10, marker='^')

# Map title
ax_map.set_title("TOPOGRAPHIC MAP  |  3km × 3km  |  1:15,000  |  10m contours",
                 color="#5a6878", fontsize=7.5, fontfamily='monospace',
                 pad=6, loc='left')

# ── Right: 3D surface ─────────────────────────────────────────────────────────
# Downsample for 3D plot performance (257->65)
step = 4
Xd = X0[::step]; Yd = Y0[::step]; Zd = Z[::step, ::step]
Xg, Yg = np.meshgrid(Xd, Yd, indexing='ij')

# Surface colors based on elevation
norm = mcolors.Normalize(vmin=Z_min, vmax=Z_max)
face_colors = cmap(norm(Zd))

ax3d.plot_surface(Xg, Yg, Zd,
    facecolors=face_colors,
    rstride=1, cstride=1,
    linewidth=0, antialiased=True,
    alpha=1.0, shade=True)

# Contour on ground plane
offset = Z_min - 15
levels_3d = np.arange(0, Z_max, 25)
ax3d.contour(Xg, Yg, Zd, levels=levels_3d, zdir='z', offset=offset,
             colors='#3a5070', alpha=0.4, linewidths=0.5)

# 3D styling
ax3d.view_init(elev=38, azim=-65)
ax3d.set_box_aspect([1, 1, 0.28])

ax3d.set_xlabel("Easting (m)", color="#30363d", fontsize=6, fontfamily='monospace', labelpad=2)
ax3d.set_ylabel("Northing (m)", color="#30363d", fontsize=6, fontfamily='monospace', labelpad=2)
ax3d.set_zlabel("Elev (m)", color="#30363d", fontsize=6, fontfamily='monospace', labelpad=2)
ax3d.tick_params(labelsize=5, colors="#30363d", labelcolor="#30363d")

ax3d.xaxis.pane.fill = False; ax3d.yaxis.pane.fill = False; ax3d.zaxis.pane.fill = False
ax3d.xaxis.pane.set_edgecolor("#131820")
ax3d.yaxis.pane.set_edgecolor("#131820")
ax3d.zaxis.pane.set_edgecolor("#131820")
ax3d.grid(True, alpha=0.12, color="#1c2535", linewidth=0.4)

ax3d.set_title("3D OBLIQUE VIEW  (4× vertical exaggeration)",
               color="#5a6878", fontsize=7, fontfamily='monospace', pad=4)

# ── Title banner at bottom ────────────────────────────────────────────────────
fig.text(0.50, 0.017,
         "ARIA-OS  |  Diamond-Square Fractal Terrain  |  3km × 3km  |  150m Peak  |  131,072 Faces  |  DXF Contours",
         ha='center', va='bottom', fontsize=6.5, color="#3a4a5a", fontfamily='monospace')

plt.savefig(str(OUT / "terrain.png"), dpi=120,
            bbox_inches='tight', facecolor=BG)
plt.close()
print(f"Terrain v3: {(OUT/'terrain.png').stat().st_size:,} bytes")
