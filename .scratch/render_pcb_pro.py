"""
Professional KiCad-style PCB render with copper pour, vias, 45° routing,
thermal reliefs, and multi-layer visibility.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.patches import PathPatch
from matplotlib.path import Path
from pathlib import Path as FilePath

OUT = FilePath("outputs/gallery_renders")
BOARD_W, BOARD_H = 60.0, 40.0

BG          = "#0d1117"
BOARD_FILL  = "#1a5c38"
BOARD_MASK  = "#164e30"
POUR_GND    = "#1e6840"   # GND copper pour (slightly lighter than board)
POUR_PWR    = "#1a5c30"   # power pour
EDGE        = "#e8c84a"
COPPER_F    = "#c8a23c"   # F.Cu gold
COPPER_B    = "#8a6a28"   # B.Cu (darker, partially visible through board)
SILK        = "#ebebeb"
PAD         = "#d4af37"
MASK_OPEN   = "#1a5c38"   # solder mask opening = board color
VIA_DRILL   = "#0d1117"
FAB_LAYER   = "#c8a060"

COMP_BODY = {
    "ic":        "#0e1e10",
    "ic_small":  "#121e12",
    "connector": "#2a1a0e",
    "passive":   "#1e1e0e",
    "led":       "#0e1e10",
    "resistor":  "#2a2010",
}

FOOTPRINTS = [
    ("U1", "L298N",       18.81,22.30, 7.62,25.40,"ic"),
    ("U2", "AMS1117-3.3", 37.75,10.25, 3.50, 6.50,"ic_small"),
    ("J1", "Barrel Jack", 50.50,17.50, 9.00,11.00,"connector"),
    ("J2", "Screw Term",  50.50,28.00, 8.00, 7.00,"connector"),
    ("C1", "100nF",       29.76,19.00, 1.00, 1.00,"passive"),
    ("C2", "10uF",        29.00, 7.63, 2.00, 1.25,"passive"),
    ("C3", "100nF",       35.00,14.00, 1.00, 1.00,"passive"),
    ("D1", "LED",          9.21,35.00, 1.00, 1.00,"led"),
    ("R1", "330R",         7.84,27.50, 1.00, 1.00,"resistor"),
    ("R2", "10k",          7.84,22.00, 1.00, 1.00,"resistor"),
    ("F1", "Fuse 2A",     15.00, 5.00, 3.50, 2.00,"passive"),
]

def make_pcb():
    fig, ax = plt.subplots(figsize=(9.5, 7), dpi=110)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    # ── Board substrate ───────────────────────────────────────────────────
    ax.add_patch(patches.Rectangle((0, 0), BOARD_W, BOARD_H,
                 facecolor=BOARD_MASK, edgecolor=EDGE, linewidth=2.8, zorder=2))

    # ── GND copper pour (covers most of board, with clearance around pads) ──
    # Flood fill as a large rectangle minus keep-out regions
    ax.add_patch(patches.Rectangle((1.5, 1.5), BOARD_W-3, BOARD_H-3,
                 facecolor=POUR_GND, edgecolor="none", linewidth=0, zorder=3, alpha=0.55))
    # Pour boundary shows the copper edge
    ax.add_patch(patches.Rectangle((1.5, 1.5), BOARD_W-3, BOARD_H-3,
                 facecolor="none", edgecolor=COPPER_F, linewidth=0.4, zorder=3, alpha=0.4,
                 linestyle=(0,(3,3))))

    # ── B.Cu traces (visible through board as darker layer) ───────────────
    b_cu_segs = [
        [(3,5),(3,35)],          # Left GND bus B.Cu
        [(57,5),(57,35)],        # Right GND bus
        [(3,35),(57,35)],        # Top bus
        [(3,5),(15,5)],          # Bottom left
        [(45,5),(57,5)],         # Bottom right
    ]
    for seg in b_cu_segs:
        pts = np.array(seg)
        ax.plot(pts[:,0], pts[:,1], color=COPPER_B, lw=1.0,
                solid_capstyle='round', zorder=4, alpha=0.35)

    # ── Vias ──────────────────────────────────────────────────────────────
    via_locs = [
        (22.62,12), (22.62,35), (18.81,37), (37.75,5), (37.75,15),
        (29.76,15), (50.5,37), (50.5,23), (9.21,27.5), (15,35),
        (35,5), (43,10), (25,10),
    ]
    for vx, vy in via_locs:
        ax.add_patch(patches.Circle((vx,vy), 0.75, facecolor=PAD,
                     edgecolor="none", zorder=8))
        ax.add_patch(patches.Circle((vx,vy), 0.40, facecolor=VIA_DRILL,
                     edgecolor="none", zorder=9))

    # ── F.Cu traces with 45° routing ─────────────────────────────────────
    def trace(pts_list, w=0.5, color=COPPER_F, alpha=0.9):
        pts = np.array(pts_list, dtype=float)
        ax.plot(pts[:,0], pts[:,1], color=color, linewidth=w*1.8,
                solid_capstyle='round', solid_joinstyle='round',
                zorder=6, alpha=alpha)

    # Power rail (wide) – J1 → U2 → U1 with 45° jogs
    trace([(46,17.5),(43,17.5),(40,14.5),(39.5,14.5)], w=0.9)   # J1→U2 with jog
    trace([(36,10.25),(33,10.25),(30,13.25),(25,13.25),(22.62,13.25)], w=0.9)  # U2→U1
    # VIN plane trace from J1 top
    trace([(46,23),(44,23),(44,13),(39.5,13)], w=0.8)

    # GND traces (to GND pour / vias)
    trace([(18.81,35),(18.81,37)], w=0.7, color=COPPER_F)
    trace([(50.5,23),(50.5,37)], w=0.6)
    trace([(9.21,34.5),(9.21,37)], w=0.5)

    # Motor output signals U1 → right edge
    for i, y_off in enumerate([18.5, 21.0, 23.5, 26.0]):
        trace([(26.1, 22.3+i*2.54-5), (30, 22.3+i*2.54-5), (32, y_off),
               (56, y_off)], w=0.45)

    # Control inputs left edge → U1 IN1-IN4
    trace([(4,14),(11,14),(13,16),(15,16)], w=0.4)
    trace([(4,17),(11,17),(13,19),(15,19)], w=0.4)
    trace([(4,21),(11,21),(13,23),(15,23)], w=0.4)
    trace([(4,25),(11,25),(13,27),(15,27)], w=0.4)

    # Bypass caps
    trace([(29.76,19),(26.5,19),(26.5,19.5)], w=0.35)
    trace([(29.0,7.63),(35,7.63),(37.75,10)], w=0.35)
    trace([(8.34,27.5),(8.34,34.5)], w=0.35)
    trace([(8.34,22),(8.34,19),(10,17),(14.5,17)], w=0.35)
    trace([(15,5),(22.62,5),(22.62,9.9)], w=0.6)   # Fuse → U1

    # ── Thermal relief patterns on through-hole GND pads ─────────────────
    for vx, vy in [(18.81,37),(50.5,37),(22.62,37),(57,37)]:
        for ang in [0, 90, 180, 270]:
            a = np.radians(ang)
            ax.plot([vx+0.75*np.cos(a), vx+1.5*np.cos(a)],
                    [vy+0.75*np.sin(a), vy+1.5*np.sin(a)],
                    color=COPPER_F, lw=0.5, alpha=0.7, zorder=7)

    # ── Corner mounting holes ─────────────────────────────────────────────
    for cx, cy in [(3,3),(3,37),(57,3),(57,37)]:
        ax.add_patch(patches.Circle((cx,cy), 1.6, facecolor=POUR_GND,
                     edgecolor=COPPER_F, lw=1.2, zorder=10))
        ax.add_patch(patches.Circle((cx,cy), 1.1, facecolor=COPPER_F,
                     edgecolor="none", zorder=11))
        ax.add_patch(patches.Circle((cx,cy), 0.65, facecolor=VIA_DRILL,
                     edgecolor="none", zorder=12))
        # Thermal relief
        for ang in [45, 135, 225, 315]:
            a = np.radians(ang)
            ax.plot([cx+1.1*np.cos(a), cx+1.9*np.cos(a)],
                    [cy+1.1*np.sin(a), cy+1.9*np.sin(a)],
                    color=COPPER_F, lw=0.7, alpha=0.8, zorder=12)

    # ── Component bodies ──────────────────────────────────────────────────
    for ref, value, cx, cy, w, h, ck in FOOTPRINTS:
        x0, y0 = cx-w/2, cy-h/2

        # Solder mask opening (board color, slightly larger)
        ax.add_patch(patches.Rectangle((x0-0.15, y0-0.15), w+0.3, h+0.3,
                     facecolor=BOARD_FILL, edgecolor="none", zorder=12))

        # Fab layer outline
        ax.add_patch(patches.Rectangle((x0, y0), w, h,
                     facecolor=COMP_BODY[ck], edgecolor=FAB_LAYER,
                     linewidth=0.6, zorder=13))

        # U1: DIP-22 L298N
        if ref == "U1":
            n_pins = 11
            pitch  = 2.54
            for i in range(n_pins):
                py = cy - (n_pins-1)*pitch/2 + i*pitch
                for side, px_off in [(+1, w/2), (-1, -w/2)]:
                    px = cx + px_off
                    ax.add_patch(patches.Rectangle(
                        (px - side*0.25 - 0.2, py-0.3), 0.45, 0.6,
                        facecolor=PAD, edgecolor="none", zorder=14))
            # Pin 1 dot
            ax.add_patch(patches.Circle((x0+0.6, y0+0.6), 0.25,
                         facecolor=SILK, zorder=15))
            # IC body stripe
            ax.plot([cx-w/2+0.3, cx+w/2-0.3], [cy+h/2-1.2]*2,
                    color=SILK, lw=0.4, alpha=0.6, zorder=15)

        # U2: SOT-223
        elif ref == "U2":
            # 3 pins on top, 1 tab on bottom
            for i, py_off in enumerate([-h*0.25, 0, h*0.25]):
                ax.add_patch(patches.Rectangle(
                    (cx-w/2-0.4, cy+py_off-0.2), 0.4, 0.4,
                    facecolor=PAD, edgecolor="none", zorder=14))
            ax.add_patch(patches.Rectangle(
                (cx-0.8, cy-h/2-0.5), 1.6, 0.5,
                facecolor=PAD, edgecolor="none", zorder=14))

        # J1/J2 connectors
        elif ref in ("J1", "J2"):
            # Barrel jack visualization
            ax.add_patch(patches.Circle((cx, cy), min(w,h)/2*0.7,
                         facecolor=BOARD_FILL, edgecolor=PAD, lw=1.2, zorder=14))
            ax.add_patch(patches.Circle((cx, cy), min(w,h)/2*0.35,
                         facecolor=VIA_DRILL, edgecolor="none", zorder=15))
            # Through-hole mounting pins
            for px_off in [-w*0.3, w*0.3]:
                ax.add_patch(patches.Circle((cx+px_off, cy),
                             0.65, facecolor=PAD, edgecolor="none", zorder=14))
                ax.add_patch(patches.Circle((cx+px_off, cy),
                             0.35, facecolor=VIA_DRILL, edgecolor="none", zorder=15))

        # SMD passives (0402/0603)
        elif ref in ("C1","C2","C3","R1","R2"):
            for px_off in [-w*0.40, w*0.40]:
                ax.add_patch(patches.Rectangle(
                    (cx+px_off-0.22, cy-0.22), 0.44, 0.44,
                    facecolor=PAD, edgecolor="none", zorder=14))
            # Component body
            ax.add_patch(patches.Rectangle((cx-w*0.25, cy-h*0.35), w*0.5, h*0.7,
                         facecolor=COMP_BODY[ck], edgecolor="none", zorder=13))

        # D1 LED
        elif ref == "D1":
            for px_off in [-w*0.40, w*0.40]:
                ax.add_patch(patches.Rectangle(
                    (cx+px_off-0.20, cy-0.20), 0.40, 0.40,
                    facecolor=PAD, edgecolor="none", zorder=14))
            # LED dome
            ax.add_patch(patches.Circle((cx, cy), 0.35,
                         facecolor="#22aa22", edgecolor="none", alpha=0.7, zorder=14))

        # F1 Fuse
        elif ref == "F1":
            for px_off in [-w*0.35, w*0.35]:
                ax.add_patch(patches.Circle((cx+px_off, cy), 0.65,
                             facecolor=PAD, edgecolor="none", zorder=14))
                ax.add_patch(patches.Circle((cx+px_off, cy), 0.35,
                             facecolor=VIA_DRILL, edgecolor="none", zorder=15))

        # ── Silkscreen reference label ─────────────────────────────────
        fs = 1.4 if ref == "U1" else (1.0 if ref in ("U2","J1","J2") else 0.75)
        label_y = y0 - 0.9
        if cy < 10:
            label_y = y0 + h + 0.3
        ax.text(cx, label_y, ref, color=SILK, fontsize=fs*6,
                ha='center', va='top', fontfamily='monospace', zorder=16,
                fontweight='bold', alpha=0.85)
        # Value text (smaller)
        ax.text(cx, label_y - fs*4.5, value, color=SILK, fontsize=fs*4,
                ha='center', va='top', fontfamily='monospace', zorder=16, alpha=0.55)

    # ── Net labels on board edge ──────────────────────────────────────────
    for lbl, x, y, rot in [
        ("IN1-4", 1.5, 20, 90),
        ("GND/PWR", 1.5, 36, 90),
        ("MOTOR A/B", 58.5, 22, 90),
        ("12V IN", 58.5, 28, 90),
    ]:
        ax.text(x, y, lbl, color="#5a7896", fontsize=5, ha='center', va='center',
                fontfamily='monospace', rotation=rot, zorder=17)

    # ── Board info text ───────────────────────────────────────────────────
    ax.text(30, -2.8,
            "Motor Driver PCB  |  60×40mm  |  2-layer  |  FR4 1.6mm  |  "
            "L298N + AMS1117-3.3  |  11 components",
            color="#6e7e8e", fontsize=6.5, ha='center', va='top',
            fontfamily='monospace')

    ax.set_xlim(-4, BOARD_W + 4)
    ax.set_ylim(-5, BOARD_H + 4)
    ax.set_aspect('equal')
    ax.invert_yaxis()
    ax.axis('off')

    plt.tight_layout(pad=0.3)
    out = OUT / "ecad_motor_driver_fixed.png"
    plt.savefig(str(out), dpi=110, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close()
    print(f"PCB: {out.stat().st_size:,} bytes")

make_pcb()
