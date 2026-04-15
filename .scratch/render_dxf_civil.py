"""Render civil site plan DXF with proper CAD-style visualization."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import ezdxf
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from pathlib import Path

OUT = Path("outputs/gallery_renders")

DXF_FILE = "outputs/cad/dxf/national_site.dxf"

# Layer color mapping (CAD-style)
LAYER_COLORS = {
    "ROAD-CL":            "#ffff00",   # centerline yellow
    "ROAD-EOP":           "#c8c8c8",   # edge of pavement light gray
    "ROAD-CURB":          "#a0a0a0",
    "ROAD-SIDEWALK":      "#808080",
    "ROAD-TURN-LANE":     "#ffff44",
    "ROAD-BIKE-LANE":     "#40ff40",
    "ROAD-DIM":           "#ffffff",
    "DRAIN-PIPE-STORM":   "#4080ff",   # storm blue
    "DRAIN-PIPE-SANITARY":"#ff8040",   # sanitary orange
    "DRAIN-INLET":        "#40a0ff",
    "DRAIN-MH":           "#40a0ff",
    "DRAIN-CHANNEL":      "#60b0ff",
    "DRAIN-FLOWLINE":     "#4080ff",
    "DRAIN-LABEL":        "#80c0ff",
    "GRADE-EXIST-CONTOUR":"#806040",   # brown existing contour
    "GRADE-PROP-CONTOUR": "#a07840",
    "GRADE-SLOPE":        "#c0a060",
    "UTIL-WATER-MAIN":    "#00c8ff",   # cyan water
    "UTIL-SEWER-MAIN":    "#ff6040",   # red-orange sewer
    "UTIL-GAS-MAIN":      "#ffa040",   # orange gas
    "UTIL-ELEC-DUCTBANK": "#ff4040",   # red electric
    "UTIL-FIBER":         "#ff40ff",   # magenta fiber
    "UTIL-STORM-MAIN":    "#4060ff",
    "SURV-BOUNDARY":      "#ff8040",
    "SURV-ROW":           "#ffa040",
    "SITE-BLDG":          "#808080",
    "SITE-PARKING":       "#404040",
    "ANNO-DIM":           "#ffffff",
    "ANNO-TEXT":          "#e0e0e0",
    "ANNO-TITLEBLOCK":    "#e0e0e0",
    "0":                  "#c8c8c8",
}

def render_dxf_civil():
    doc = ezdxf.readfile(DXF_FILE)
    msp = doc.modelspace()

    fig, ax = plt.subplots(figsize=(8, 6), dpi=100)
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")
    ax.set_aspect('equal')

    drawn = 0
    for entity in msp:
        etype = entity.dxftype()
        layer = entity.dxf.layer if hasattr(entity.dxf, 'layer') else "0"
        color = LAYER_COLORS.get(layer, "#808080")
        lw = 1.2 if "CL" in layer or "MAIN" in layer else 0.8

        if etype == "LWPOLYLINE":
            pts = list(entity.get_points())
            if len(pts) < 2:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if entity.closed:
                xs.append(xs[0])
                ys.append(ys[0])
            ax.plot(xs, ys, color=color, linewidth=lw, solid_capstyle='round',
                    solid_joinstyle='round', zorder=2)
            drawn += 1

        elif etype == "LINE":
            s = entity.dxf.start
            e = entity.dxf.end
            x1, y1 = float(s[0]), float(s[1])
            x2, y2 = float(e[0]), float(e[1])
            ax.plot([x1, x2], [y1, y2], color=color, linewidth=lw, zorder=2)
            drawn += 1

        elif etype == "CIRCLE":
            c = entity.dxf.center
            cx, cy = float(c[0]), float(c[1])
            r = entity.dxf.radius
            circle = patches.Circle((cx, cy), r,
                                    fill=False, edgecolor=color,
                                    linewidth=lw, zorder=2)
            ax.add_patch(circle)
            drawn += 1

        elif etype == "TEXT":
            try:
                ins = entity.dxf.insert
                x, y = float(ins[0]), float(ins[1])
                h = getattr(entity.dxf, 'height', 1.0)
                txt = entity.dxf.text
                if txt and len(txt) < 60:
                    ax.text(x, y, txt, color=color,
                            fontsize=max(3, min(7, h * 1.2)),
                            ha='left', va='bottom', fontfamily='monospace',
                            zorder=3)
                drawn += 1
            except Exception:
                pass

    ax.autoscale()
    ax.set_xlabel("", color="none")
    ax.set_ylabel("", color="none")
    ax.tick_params(colors="#30363d", labelsize=7, labelcolor="#8b949e")
    for spine in ax.spines.values():
        spine.set_color("#30363d")

    # Title overlay
    ax.set_title("National Site Plan — Civil Infrastructure DXF",
                 color="#e6edf3", fontsize=9, pad=6)

    # Legend (key layers)
    legend_items = [
        ("#ffff00", "Road CL"),
        ("#4080ff", "Storm Drain"),
        ("#ff6040", "Sanitary"),
        ("#00c8ff", "Water Main"),
        ("#ffa040", "Gas/ROW"),
    ]
    for i, (c, label) in enumerate(legend_items):
        ax.plot([], [], color=c, linewidth=2, label=label)
    ax.legend(loc='lower right', fontsize=6, framealpha=0.3,
              facecolor="#0d1117", edgecolor="#30363d",
              labelcolor="#8b949e")

    plt.tight_layout(pad=0.5)
    out = OUT / "civil_site_plan.png"
    plt.savefig(str(out), dpi=100, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close()
    print(f"Civil plan: {out.stat().st_size:,} bytes  ({drawn} entities drawn)")
    return str(out)

render_dxf_civil()
