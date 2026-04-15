"""
Render motor_driver_pcb_60x40mm as a proper PCB visualization.
Uses actual component positions from the .kicad_pcb file.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from matplotlib.collections import LineCollection
from pathlib import Path

OUT = Path("outputs/gallery_renders")

# ── KiCad component data (parsed from .kicad_pcb) ──────────────────────────
# Board: 60×40mm. KiCad y-down (top=0, bottom=40).
BOARD_W, BOARD_H = 60.0, 40.0

# (ref, value, cx, cy, w, h, color_key)
# w,h are full dimensions (fp_line bounds × 2)
FOOTPRINTS = [
    ("U1", "L298N",        18.810, 22.300,  7.62, 25.40, "ic"),
    ("J1", "Barrel Jack",  50.500, 17.500,  9.00, 11.00, "connector"),
    ("U2", "AMS1117-3.3",  37.750, 10.250,  3.50,  6.50, "ic_small"),
    ("C1", "100nF",        29.760, 19.000,  1.00,  1.00, "passive"),
    ("C2", "10uF",         29.000,  7.625,  2.00,  1.25, "passive"),
    ("D1", "LED",           9.210, 35.000,  1.00,  1.00, "led"),
    ("R1", "330R",          7.840, 27.500,  1.00,  1.00, "passive"),
]

# ── Colors ──────────────────────────────────────────────────────────────────
BG          = "#0d1117"
BOARD_FILL  = "#1a5c38"       # FR4 green
BOARD_MASK  = "#174e30"       # solder mask (slightly darker)
EDGE_CUT    = "#e8c84a"       # yellow board outline
COPPER      = "#c8a23c"       # F.Cu gold
SILK        = "#e8e8e8"       # white silkscreen
PAD_COLOR   = "#d4af37"       # pad gold
TRACE_VIN   = "#c8a23c"       # power trace
TRACE_GND   = "#a07820"       # GND (slightly darker)

COMP_COLORS = {
    "ic":        "#1a2e1a",   # dark green IC body
    "ic_small":  "#1a2e1a",
    "connector": "#2a1a0e",   # dark brown for connector
    "passive":   "#2a2a1a",   # dark yellow-gray
    "led":       "#1a2a10",   # green LED
}

def make_pcb():
    fig, ax = plt.subplots(figsize=(8, 6), dpi=100)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    # Board substrate
    board = patches.Rectangle((0, 0), BOARD_W, BOARD_H,
                               facecolor=BOARD_FILL, edgecolor=EDGE_CUT,
                               linewidth=2.5, zorder=2)
    ax.add_patch(board)

    # Corner mounting holes
    for cx, cy in [(3, 3), (3, 37), (57, 3), (57, 37)]:
        ax.add_patch(patches.Circle((cx, cy), 1.0,
                                    facecolor=BOARD_FILL, edgecolor=EDGE_CUT,
                                    linewidth=1.5, zorder=4))
        ax.add_patch(patches.Circle((cx, cy), 0.65,
                                    facecolor=BG, edgecolor="none", zorder=5))

    # ── Synthetic copper traces (representative routing) ──────────────────
    # These represent key nets: power, GND, motor outputs
    trace_segs = []
    trace_widths = []

    # Power rail: J1 → U2 → U1
    trace_segs += [
        # J1 pin (left side ~46,17.5) to U2 right side (~39.5,10.25)
        [(46, 17.5), (43, 17.5), (43, 10.25), (39.5, 10.25)],
        # U2 output (left ~36, 10.25) to U1 power (~22.62, 15)
        [(36, 10.25), (25, 10.25), (25, 15), (22.62, 15)],
    ]
    trace_widths += [0.8, 0.8]

    # GND rail (wide): bottom horizontal bus
    trace_segs += [
        [(4, 37), (56, 37)],         # GND bus along bottom
        [(50.5, 23), (50.5, 37)],    # J1 GND to bus
        [(18.81, 35), (18.81, 37)],  # U1 GND to bus
        [(9.21, 35.5), (9.21, 37)],  # D1 to GND bus
    ]
    trace_widths += [1.2, 0.6, 0.6, 0.6]

    # Signal traces: U1 motor outputs → right edge (representing motor leads)
    trace_segs += [
        [(22.62, 18), (56, 18)],
        [(22.62, 22), (56, 22)],
        [(22.62, 26), (56, 26)],
        [(22.62, 30), (56, 30)],
    ]
    trace_widths += [0.5, 0.5, 0.5, 0.5]

    # Control signals: U1 left side → left edge (IN1-IN4 from MCU)
    trace_segs += [
        [(15, 14), (4, 14)],
        [(15, 17), (4, 17)],
        [(15, 21), (4, 21)],
        [(15, 25), (4, 25)],
    ]
    trace_widths += [0.4, 0.4, 0.4, 0.4]

    # Bypass cap connections
    trace_segs += [
        # C1 (29.76,19) to U1 power pad
        [(29.76, 19), (22.62, 19)],
        # C2 (29.0, 7.625) to U2
        [(29.0, 7.625), (39.5, 7.625), (39.5, 10.25)],
        # R1 (7.84, 27.5) to D1 (9.21, 35)
        [(8.34, 27.5), (9.21, 27.5), (9.21, 34.5)],
    ]
    trace_widths += [0.4, 0.4, 0.4]

    # Draw traces as polylines
    for seg, w in zip(trace_segs, trace_widths):
        pts = np.array(seg)
        ax.plot(pts[:, 0], pts[:, 1], color=COPPER,
                linewidth=w * 1.5, solid_capstyle='round', zorder=3,
                alpha=0.85)

    # ── Component bodies and outlines ────────────────────────────────────
    for ref, value, cx, cy, w, h in [(f[0],f[1],f[2],f[3],f[4],f[5]) for f in FOOTPRINTS]:
        ck = next(f[6] for f in FOOTPRINTS if f[0] == ref)
        x0, y0 = cx - w/2, cy - h/2

        # Body fill
        body = patches.Rectangle((x0, y0), w, h,
                                  facecolor=COMP_COLORS[ck],
                                  edgecolor=COPPER, linewidth=1.0, zorder=5)
        ax.add_patch(body)

        # Pin indicators on large IC (U1)
        if ref == "U1":
            n_pins = 11
            pin_pitch = 2.54
            for i in range(n_pins):
                py = cy - (n_pins-1)*pin_pitch/2 + i*pin_pitch
                for px_off in [-w/2, w/2]:
                    ax.add_patch(patches.Rectangle(
                        (cx + px_off - 0.3*(1 if px_off > 0 else -1), py - 0.3),
                        0.3, 0.6,
                        facecolor=PAD_COLOR, edgecolor="none", zorder=6))
            # Pin 1 indicator
            ax.add_patch(patches.Circle((x0 + 0.8, y0 + 0.8), 0.3,
                                        facecolor=SILK, zorder=7))

        # Connector pins (J1)
        elif ref == "J1":
            ax.add_patch(patches.Circle((cx, cy), 2.0,
                                        facecolor=BOARD_FILL, edgecolor=PAD_COLOR,
                                        linewidth=1.5, zorder=6))
            ax.add_patch(patches.Circle((cx, cy), 1.1,
                                        facecolor=BG, edgecolor="none", zorder=7))

        # Pads for small components
        elif ref in ("C1", "C2", "D1", "R1"):
            for px_off in [-w*0.35, w*0.35]:
                ax.add_patch(patches.Rectangle(
                    (cx + px_off - 0.25, cy - 0.25), 0.5, 0.5,
                    facecolor=PAD_COLOR, edgecolor="none", zorder=6))

        # Small IC pins (U2)
        elif ref == "U2":
            for py_off in [-h*0.3, 0, h*0.3]:
                for px_off in [-w/2, w/2]:
                    ax.add_patch(patches.Rectangle(
                        (cx + px_off - 0.15*(1 if px_off > 0 else -1), cy + py_off - 0.2),
                        0.15, 0.4,
                        facecolor=PAD_COLOR, edgecolor="none", zorder=6))

        # Silkscreen label
        fs = 1.2 if ref in ("U1",) else (0.9 if ref in ("U2", "J1") else 0.65)
        ax.text(cx, y0 - 0.8, ref, color=SILK, fontsize=fs * 6,
                ha='center', va='top', fontfamily='monospace', zorder=8,
                fontweight='bold')

    # ── Board outline text ────────────────────────────────────────────────
    ax.text(30, 42.5,
            "Motor Driver PCB  |  60×40mm  |  L298N + AMS1117-3.3  |  7 components",
            color="#8b949e", fontsize=7, ha='center', va='bottom',
            fontfamily='monospace')

    # ── Net labels on edge connectors ────────────────────────────────────
    for label, x, y in [
        ("IN1-4", 2, 14), ("GND/PWR", 2, 36), ("MOTOR A/B", 58, 22),
    ]:
        ax.text(x, y, label, color="#6e88a8", fontsize=5.5, ha='center',
                va='center', fontfamily='monospace', rotation=90 if x < 5 else 0)

    ax.set_xlim(-3, BOARD_W + 3)
    ax.set_ylim(-4, BOARD_H + 5)
    ax.set_aspect('equal')
    ax.invert_yaxis()           # KiCad: y=0 is top, y=40 is bottom
    ax.axis('off')

    plt.tight_layout(pad=0)
    out = OUT / "ecad_motor_driver_fixed.png"
    plt.savefig(str(out), dpi=100, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    plt.close()
    print(f"PCB render: {out.stat().st_size:,} bytes")

make_pcb()
