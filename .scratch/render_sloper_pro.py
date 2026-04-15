"""
Professional image-to-CAD visualization for the climbing sloper hold.
Left: simulated input design sketch (pen-on-drafting-paper style).
Right: generated CAD model from 3 dramatic render angles.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import trimesh
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.collections import PolyCollection
from pathlib import Path

OUT = Path("outputs/gallery_renders")
STL = Path("outputs/cad/stl/llm_asymmetric_freeform_climbing_sloper_hold.stl")
BG  = "#080c12"

mesh = trimesh.load(str(STL))
if hasattr(mesh, "geometry"):
    mesh = trimesh.util.concatenate(list(mesh.geometry.values()))
mesh.apply_translation(-mesh.centroid)

face_verts  = mesh.vertices[mesh.faces]   # (F,3,3)
face_normals = mesh.face_normals           # (F,3)

AMBIENT = 0.20
FILL_S  = 0.18

def rotation(az_deg, el_deg):
    az, el = np.radians(az_deg), np.radians(el_deg)
    Ry = np.array([[np.cos(az),0,np.sin(az)],[0,1,0],[-np.sin(az),0,np.cos(az)]])
    Rx = np.array([[1,0,0],[0,np.cos(el),-np.sin(el)],[0,np.sin(el),np.cos(el)]])
    return Rx @ Ry

def render_view(ax, R, bg_color, light1, light2, title):
    fv = (face_verts.reshape(-1,3) @ R.T).reshape(-1,3,3)
    fn = face_normals @ R.T
    dep = fv[:,:,2].mean(axis=1)
    ord_ = np.argsort(-dep)
    fv = fv[ord_]; fn = fn[ord_]

    L1 = np.asarray(light1); L1 /= np.linalg.norm(L1)
    L2 = np.asarray(light2); L2 /= np.linalg.norm(L2)
    d1 = np.clip(fn @ L1, 0,1) * 0.65
    d2 = np.clip(fn @ L2, 0,1) * FILL_S
    intensity = AMBIENT + d1 + d2

    # Warm polyurethane resin color
    r_c = np.clip(intensity * 0.82, 0, 1)
    g_c = np.clip(intensity * 0.76, 0, 1)
    b_c = np.clip(intensity * 0.68, 0, 1)
    fc  = np.column_stack([r_c, g_c, b_c, np.ones(len(intensity))])

    ax.set_facecolor(bg_color)
    ax.add_collection(PolyCollection(fv[:,:,:2], facecolors=fc,
                                     edgecolors="none", linewidths=0))
    ec = np.clip(fc[:,:3]*1.2, 0, 1)
    ea = np.where(intensity > 0.45, 0.20, 0.0)
    ax.add_collection(PolyCollection(fv[:,:,:2], facecolors="none",
                                     edgecolors=np.column_stack([ec,ea]),
                                     linewidths=0.15))
    ax.autoscale(); ax.set_aspect('equal'); ax.axis('off')
    ax.set_title(title, color="#8a9aaa", fontsize=7.5, fontfamily='monospace', pad=4)

fig = plt.figure(figsize=(14, 7), dpi=115)
fig.patch.set_facecolor(BG)

# ── Left: design sketch panel ─────────────────────────────────────────────
ax_sk = fig.add_axes([0.01, 0.06, 0.28, 0.88])
ax_sk.set_facecolor("#f5f0e8")  # drafting paper
for sp in ax_sk.spines.values():
    sp.set_edgecolor("#c0b8a8"); sp.set_linewidth(0.8)

# Simulate a design sketch with pen strokes
t = np.linspace(0, 2*np.pi, 200)
# Main sloper outline (asymmetric teardrop)
x_sk = 0.50 + 0.30*np.cos(t) - 0.06*np.cos(2*t)
y_sk = 0.50 + 0.22*np.sin(t) + 0.04*np.sin(3*t) - 0.02*np.sin(t)*np.cos(t)
ax_sk.plot(x_sk, y_sk, color="#1a1a2a", lw=1.5, solid_capstyle='round',
           transform=ax_sk.transAxes, zorder=5)

# Side profile (smaller, dashed)
x_side = 0.22 + 0.08*np.cos(t)
y_side = 0.30 + 0.14*np.sin(t)*0.55
ax_sk.plot(x_side, y_side, color="#404050", lw=0.9, linestyle=(0,(4,2)),
           transform=ax_sk.transAxes, zorder=4)

# Dimension arrows and annotations
for (x1,y1,x2,y2) in [(0.20,0.14,0.80,0.14),(0.88,0.30,0.88,0.72)]:
    ax_sk.annotate("", xy=(x2,y2), xytext=(x1,y1),
        arrowprops=dict(arrowstyle='<->', color='#404060', lw=0.7, mutation_scale=6),
        xycoords='axes fraction', textcoords='axes fraction')
ax_sk.text(0.50, 0.10, "≈ 120mm", ha='center', fontsize=7, color="#404060",
           fontfamily='monospace', transform=ax_sk.transAxes, style='italic')
ax_sk.text(0.92, 0.51, "85mm", ha='left', fontsize=7, color="#404060",
           fontfamily='monospace', transform=ax_sk.transAxes, rotation=90, style='italic')

# Feature call-outs
for (tx, ty, txt, tx2, ty2) in [
    (0.68, 0.78, "GRIP SURFACE\nFREEFORM", 0.60, 0.70),
    (0.28, 0.68, "ASYMMETRIC\nPROFILE", 0.36, 0.60),
    (0.55, 0.28, "MOUNTING\nFACE", 0.52, 0.36),
]:
    ax_sk.annotate(txt, xy=(tx2,ty2), xytext=(tx,ty),
        xycoords='axes fraction', textcoords='axes fraction',
        arrowprops=dict(arrowstyle='->', color='#505070', lw=0.6, mutation_scale=5),
        fontsize=5.5, color="#303050", fontfamily='monospace', ha='center',
        bbox=dict(boxstyle='round,pad=0.2', facecolor='#f0ece0',
                  edgecolor='#b0a898', alpha=0.8))

# Cross-hatch grip area
for i in np.linspace(0,1,18):
    ax_sk.plot([0.50+0.05*i, 0.50+0.05*i+0.12],
               [0.68-0.04*i, 0.72-0.04*i],
               color="#888890", lw=0.35, alpha=0.6,
               transform=ax_sk.transAxes)

ax_sk.text(0.50, 0.03, "INPUT SKETCH  |  FREEHAND + AI ASSIST",
           ha='center', fontsize=6.5, fontweight='bold', color="#505060",
           fontfamily='monospace', transform=ax_sk.transAxes)

ax_sk.set_title("DESIGN INPUT", fontsize=9, fontweight='bold',
                color="#606070", fontfamily='monospace', pad=5)
ax_sk.set_xlim(0,1); ax_sk.set_ylim(0,1); ax_sk.axis('off')
# Re-add border after axis off
for side in ['top','bottom','left','right']:
    ax_sk.spines[side].set_visible(True)
    ax_sk.spines[side].set_edgecolor("#c0b8a8")
    ax_sk.spines[side].set_linewidth(0.8)

# AI arrow divider
ax_div = fig.add_axes([0.295, 0.40, 0.05, 0.20])
ax_div.set_facecolor(BG); ax_div.axis('off')
ax_div.annotate("", xy=(0.85, 0.5), xytext=(0.15, 0.5),
    arrowprops=dict(arrowstyle="-|>", color="#f0a020", lw=2.8, mutation_scale=18),
    xycoords='axes fraction', textcoords='axes fraction')
ax_div.text(0.50, 0.15, "AI\nCAD", ha='center', va='top', fontsize=7,
            color="#f0a020", fontfamily='monospace', fontweight='bold',
            transform=ax_div.transAxes)

# ── Right: 3 CAD render panels ────────────────────────────────────────────
panel_bg = ["#0d1320", "#0d1820", "#0d1220"]
views = [
    (rotation(20, -20),   [0.3, 0.8, 0.6],  [-0.5, 0.2, 0.5],
     "TOP  |  GRIP SURFACE", "#0d1a28"),
    (rotation(200, -15),  [0.4, 0.6, 0.7],  [-0.4, 0.3, 0.5],
     "FRONT  |  ASYMMETRIC PROFILE", "#0e1820"),
    (rotation(135, -30),  [0.35, 0.75, 0.55],[-0.5, 0.25, 0.5],
     "ISO  |  MOUNTING FACE", "#0d1628"),
]

for i, (R, L1, L2, title, bg) in enumerate(views):
    left_pos = 0.355 + i * 0.212
    ax_v = fig.add_axes([left_pos, 0.06, 0.205, 0.88])
    render_view(ax_v, R, bg, L1, L2, title)
    for sp in ax_v.spines.values():
        sp.set_visible(True)
        sp.set_edgecolor("#1c2535"); sp.set_linewidth(0.5)

# ── Global header ─────────────────────────────────────────────────────────
fig.text(0.50, 0.97,
    "IMAGE-TO-CAD  |  ASYMMETRIC FREEFORM CLIMBING SLOPER HOLD  |  ARIA-OS",
    ha='center', va='top', fontsize=10.5, fontweight='bold',
    color="#c9d1d9", fontfamily='monospace')
fig.text(0.50, 0.01,
    "MATERIAL: Shore A70 Polyurethane Resin  |  120×85×45mm  |  "
    "LLM geometry → CadQuery → STL  |  ARIA-OS IMAGE-TO-CAD",
    ha='center', va='bottom', fontsize=6, color="#2a3a4a", fontfamily='monospace')

plt.savefig(str(OUT / "gl_sloper.png"), dpi=115, bbox_inches='tight', facecolor=BG)
plt.close()
print(f"gl_sloper: {(OUT/'gl_sloper.png').stat().st_size:,} bytes")
