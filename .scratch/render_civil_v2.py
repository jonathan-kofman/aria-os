"""
Civil site plan render v2 — professional civil engineering style.
Light paper background, color-coded utility layers, proper title block.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.lines as lines
from pathlib import Path

OUT = Path("outputs/gallery_renders")
DXF = "outputs/cad/dxf/national_site.dxf"

# ── Load and parse DXF ────────────────────────────────────────────────────────
import ezdxf

layer_groups = {}
try:
    doc = ezdxf.readfile(DXF)
    msp = doc.modelspace()
    for entity in msp:
        layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else "0"
        if layer not in layer_groups:
            layer_groups[layer] = []
        try:
            if entity.dxftype() in ('LINE',):
                s = entity.dxf.start
                e = entity.dxf.end
                layer_groups[layer].append(('line', [s.x, e.x], [s.y, e.y]))
            elif entity.dxftype() == 'LWPOLYLINE':
                pts = list(entity.get_points())
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if entity.closed:
                    xs.append(xs[0]); ys.append(ys[0])
                layer_groups[layer].append(('poly', xs, ys))
            elif entity.dxftype() == 'CIRCLE':
                cx, cy = entity.dxf.center.x, entity.dxf.center.y
                r = entity.dxf.radius
                theta = np.linspace(0, 2*np.pi, 48)
                xs = cx + r*np.cos(theta)
                ys = cy + r*np.sin(theta)
                layer_groups[layer].append(('poly', list(xs), list(ys)))
        except:
            pass
except Exception as ex:
    print(f"DXF load warning: {ex}")

# ── Layer style config ─────────────────────────────────────────────────────────
# Color, linewidth, alpha, zorder
LAYER_STYLES = {
    "ROAD-CL":              ("#e85c20", 0.9, 0.85, 5),
    "ROAD-EOP":             ("#888888", 1.2, 0.90, 4),
    "ROAD-CURB":            ("#666666", 0.8, 0.90, 4),
    "ROAD-SIDEWALK":        ("#aaaaaa", 0.6, 0.70, 3),
    "ROAD-TURN-LANE":       ("#e8a820", 0.7, 0.80, 5),
    "ROAD-BIKE-LANE":       ("#50b840", 0.7, 0.80, 5),
    "DRAIN-PIPE-STORM":     ("#2060d8", 1.0, 0.90, 6),
    "DRAIN-PIPE-SANITARY":  ("#d84020", 0.9, 0.85, 6),
    "DRAIN-INLET":          ("#2060d8", 0.6, 0.80, 6),
    "DRAIN-MH":             ("#2060d8", 0.6, 0.80, 6),
    "DRAIN-CHANNEL":        ("#4090e8", 0.7, 0.75, 6),
    "DRAIN-FLOWLINE":       ("#3070e8", 0.6, 0.75, 6),
    "UTIL-WATER-MAIN":      ("#1890d0", 1.0, 0.90, 7),
    "UTIL-SEWER-MAIN":      ("#c83020", 0.8, 0.85, 7),
    "UTIL-GAS-MAIN":        ("#d89020", 0.8, 0.85, 7),
    "UTIL-ELEC-DUCTBANK":   ("#c82020", 0.7, 0.80, 7),
    "UTIL-FIBER":           ("#a020a8", 0.7, 0.75, 7),
    "UTIL-STORM-MAIN":      ("#3858d0", 0.8, 0.85, 7),
    "SURV-BOUNDARY":        ("#d84010", 1.4, 0.95, 8),
    "SURV-SECTION-LINE":    ("#c83820", 0.9, 0.85, 8),
    "GRADE-EXIST-CONTOUR":  ("#c09060", 0.4, 0.60, 2),
    "GRADE-PROP-CONTOUR":   ("#e0b870", 0.5, 0.65, 2),
    "SITE-BLDG-FOOTPRINT":  ("#404040", 1.2, 0.95, 9),
    "SITE-PARKING":         ("#808080", 0.6, 0.70, 3),
    "SITE-TREES":           ("#38a838", 0.6, 0.80, 3),
    "SITE-SETBACK":         ("#909090", 0.5, 0.60, 2),
    "PARK-LOT-STRIPE":      ("#606060", 0.5, 0.65, 4),
}
DEFAULT_STYLE = ("#303030", 0.4, 0.50, 1)

# ── Figure ────────────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(11, 8.5), dpi=120)
fig.patch.set_facecolor("#f4f0e8")  # drafting paper cream
ax.set_facecolor("#f4f0e8")

# Draw all geometry
drawn_layers = set()
for layer, entities in layer_groups.items():
    style_key = layer.upper()
    color, lw, alpha, zo = LAYER_STYLES.get(style_key, DEFAULT_STYLE)
    for kind, xs, ys in entities:
        ax.plot(xs, ys, color=color, linewidth=lw, alpha=alpha, zorder=zo,
                solid_capstyle='round', solid_joinstyle='round')
    drawn_layers.add(layer)

# ── Calculate extent ──────────────────────────────────────────────────────────
# Get all X/Y ranges from drawn entities
all_xs, all_ys = [], []
for layer, entities in layer_groups.items():
    for kind, xs, ys in entities:
        all_xs.extend(xs); all_ys.extend(ys)

if all_xs:
    xmin, xmax = min(all_xs), max(all_xs)
    ymin, ymax = min(all_ys), max(all_ys)
    margin = max(xmax-xmin, ymax-ymin) * 0.06
    ax.set_xlim(xmin-margin, xmax+margin)
    ax.set_ylim(ymin-margin, ymax+margin)

# ── Border (drafting border) ──────────────────────────────────────────────────
for sp in ax.spines.values():
    sp.set_edgecolor("#303030"); sp.set_linewidth(1.2)

ax.tick_params(labelsize=7, colors="#505050", labelcolor="#505050")
ax.set_xlabel("Easting (ft)", fontsize=8, color="#505050", fontfamily='monospace')
ax.set_ylabel("Northing (ft)", fontsize=8, color="#505050", fontfamily='monospace')
ax.grid(color="#c8c0b0", linewidth=0.3, alpha=0.5, linestyle='--')

# ── Legend ────────────────────────────────────────────────────────────────────
legend_items = [
    ("#e85c20", "Road Centerline"),
    ("#888888", "Edge of Pavement"),
    ("#2060d8", "Storm Drain"),
    ("#d84020", "Sanitary Sewer"),
    ("#1890d0", "Water Main"),
    ("#d89020", "Gas Main"),
    ("#c82020", "Electric Ductbank"),
    ("#d84010", "Survey Boundary"),
    ("#404040", "Building Footprint"),
    ("#c09060", "Existing Contours"),
]
leg_x = ax.get_xlim()[1] - (ax.get_xlim()[1]-ax.get_xlim()[0])*0.28
leg_y_start = ax.get_ylim()[1] - (ax.get_ylim()[1]-ax.get_ylim()[0])*0.03

# Legend box
leg_w = (ax.get_xlim()[1]-ax.get_xlim()[0])*0.27
leg_h = len(legend_items) * (ax.get_ylim()[1]-ax.get_ylim()[0])*0.038 + (ax.get_ylim()[1]-ax.get_ylim()[0])*0.015
ax.add_patch(patches.Rectangle((leg_x-0.01*(ax.get_xlim()[1]-ax.get_xlim()[0]),
                                  leg_y_start-leg_h),
    leg_w, leg_h,
    facecolor="#f8f4ec", edgecolor="#808070", lw=0.8, zorder=20))
ax.text(leg_x + leg_w*0.5, leg_y_start - (ax.get_ylim()[1]-ax.get_ylim()[0])*0.01,
        "LEGEND", ha='center', va='top', fontsize=7, fontweight='bold',
        color="#303030", fontfamily='monospace', zorder=21)
row_h = (ax.get_ylim()[1]-ax.get_ylim()[0])*0.038
for i, (col, label) in enumerate(legend_items):
    y = leg_y_start - (ax.get_ylim()[1]-ax.get_ylim()[0])*0.018 - i*row_h
    seg_x = leg_x + (ax.get_xlim()[1]-ax.get_xlim()[0])*0.01
    seg_len = (ax.get_xlim()[1]-ax.get_xlim()[0])*0.04
    ax.plot([seg_x, seg_x+seg_len], [y, y], color=col, lw=1.8, zorder=21)
    ax.text(seg_x+seg_len+(ax.get_xlim()[1]-ax.get_xlim()[0])*0.008, y,
            label, va='center', fontsize=6, color="#303030",
            fontfamily='monospace', zorder=21)

# ── North arrow ───────────────────────────────────────────────────────────────
xa = ax.get_xlim()
ya = ax.get_ylim()
na_x = xa[0] + (xa[1]-xa[0])*0.06
na_y = ya[1] - (ya[1]-ya[0])*0.12
arr_len = (ya[1]-ya[0])*0.06
ax.annotate("", xy=(na_x, na_y), xytext=(na_x, na_y-arr_len),
    arrowprops=dict(arrowstyle="-|>", color="#303030", lw=1.2, mutation_scale=12),
    zorder=22)
ax.text(na_x, na_y+(ya[1]-ya[0])*0.015, "N",
    ha='center', va='bottom', fontsize=9, fontweight='bold',
    color="#303030", fontfamily='monospace', zorder=22)

# ── Scale bar ─────────────────────────────────────────────────────────────────
sb_x = xa[0] + (xa[1]-xa[0])*0.02
sb_y = ya[0] + (ya[1]-ya[0])*0.04
sb_len = 50.0  # 50 ft
ax.plot([sb_x, sb_x+sb_len], [sb_y, sb_y], color="#303030", lw=2.5,
        solid_capstyle='butt', zorder=22)
ax.plot([sb_x, sb_x], [sb_y-(ya[1]-ya[0])*0.008, sb_y+(ya[1]-ya[0])*0.008],
        color="#303030", lw=1.5, zorder=22)
ax.plot([sb_x+sb_len, sb_x+sb_len],
        [sb_y-(ya[1]-ya[0])*0.008, sb_y+(ya[1]-ya[0])*0.008],
        color="#303030", lw=1.5, zorder=22)
ax.text(sb_x+sb_len/2, sb_y+(ya[1]-ya[0])*0.018, "50 ft",
    ha='center', fontsize=6.5, color="#303030", fontfamily='monospace', zorder=22)
ax.text(sb_x+sb_len/2, sb_y-(ya[1]-ya[0])*0.022, "SCALE: 1\" = 10'",
    ha='center', fontsize=6, color="#505050", fontfamily='monospace', zorder=22)

# ── Title ─────────────────────────────────────────────────────────────────────
ax.set_title("NATIONAL SITE PLAN  —  CIVIL INFRASTRUCTURE DXF  |  70+ LAYERS",
             fontsize=9, fontfamily='monospace', color="#303030", pad=6)

# ── Title block (bottom right) ────────────────────────────────────────────────
fig.text(0.98, 0.01,
    "ARIA-OS AUTONOMOUS ENGINEERING  |  DWG: CS-001  |  SCALE 1\"=10'  |  REV A",
    ha='right', va='bottom', fontsize=6.5, color="#505050", fontfamily='monospace')

plt.tight_layout(pad=0.8)
plt.savefig(str(OUT / "civil_site_plan.png"), dpi=120,
            bbox_inches='tight', facecolor="#f4f0e8")
plt.close()
print(f"Civil site plan v2: {(OUT/'civil_site_plan.png').stat().st_size:,} bytes")
