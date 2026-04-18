"""
ECAD preview — render a top-down PCB layout from a BOM JSON file.

Produces a PNG showing:
  - Board outline (real dimensions)
  - Component bounding boxes color-coded by type (MCU/IC/connector/passive)
  - Reference labels (R1, U2, J3, ...)
  - Validation status banner (ERC/DRC pass/fail)

Usage:
    from aria_os.ecad.ecad_preview import render_pcb_preview
    png_path = render_pcb_preview(bom_json_path)

CLI:
    python -m aria_os.ecad.ecad_preview <bom.json> [--out preview.png]
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


# Color map by component type (ref prefix)
_COLORS = {
    "U":   ("#1f4068", "#5a8dc8"),    # IC / MCU — dark blue
    "J":   ("#7d3c00", "#e08a3c"),    # connector — orange
    "C":   ("#2d5a2d", "#67c267"),    # capacitor — green
    "R":   ("#5a2d2d", "#c67676"),    # resistor — red
    "L":   ("#5a4d2d", "#c6b876"),    # inductor — yellow-brown
    "D":   ("#3d2d5a", "#9b7ec6"),    # diode — purple
    "ANT": ("#444444", "#999999"),    # antenna — gray
}


def render_pcb_preview(bom_path: str | Path, *, out_png: str | Path | None = None) -> Path:
    """Render a top-down PCB layout PNG from a BOM JSON file.

    The BOM is the JSON produced by `generate_ecad()` — must contain a
    "components" list of {ref, value, x_mm, y_mm, width_mm, height_mm, ...}
    plus optional "board_w_mm" / "board_h_mm" / "board_name" keys.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    bom_path = Path(bom_path)
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    components = bom.get("components", [])
    board_w = float(bom.get("board_w_mm", 0)) or _infer_board_size(components, "w")
    board_h = float(bom.get("board_h_mm", 0)) or _infer_board_size(components, "h")
    board_name = bom.get("board_name") or bom_path.stem

    # Look for validation.json in the same directory
    val_path = bom_path.parent / "validation.json"
    val = {}
    if val_path.is_file():
        try:
            val = json.loads(val_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    erc_pass = val.get("erc_pass", val.get("erc", {}).get("passed", None))
    drc_pass = val.get("drc_pass", val.get("drc", {}).get("passed", None))

    # Build figure
    fig, ax = plt.subplots(figsize=(10, 10), dpi=140)
    ax.set_facecolor("#0d4d2c")  # dark green PCB substrate
    fig.patch.set_facecolor("white")

    # Board outline
    board_outline = Rectangle((0, 0), board_w, board_h,
                              linewidth=2.5, edgecolor="#cccccc",
                              facecolor="#1a6b3a", zorder=1)
    ax.add_patch(board_outline)

    # Components
    for c in components:
        ref = c.get("ref", "?")
        x = float(c.get("x_mm", 0))
        y = float(c.get("y_mm", 0))
        w = float(c.get("width_mm", 1))
        h = float(c.get("height_mm", 1))
        ref_prefix = "".join(ch for ch in ref if ch.isalpha()) or "U"
        edge_c, fill_c = _COLORS.get(ref_prefix, ("#444444", "#aaaaaa"))
        rect = Rectangle((x, y), w, h, linewidth=1.0,
                         edgecolor=edge_c, facecolor=fill_c, alpha=0.9, zorder=2)
        ax.add_patch(rect)
        # Label only if box is big enough to show text
        if w > 3 and h > 3:
            ax.text(x + w / 2, y + h / 2, ref,
                    ha="center", va="center",
                    fontsize=max(5, min(9, int(min(w, h) * 1.2))),
                    color="white", weight="bold", zorder=3)

    # Title + validation badges
    title = f"{board_name}  —  {board_w:.1f} × {board_h:.1f} mm  —  {len(components)} components"
    ax.set_title(title, fontsize=12, color="#0d1117", pad=20)

    # Validation banner under title
    if erc_pass is not None or drc_pass is not None:
        erc_txt = ("ERC PASS" if erc_pass else "ERC FAIL") if erc_pass is not None else "ERC ?"
        drc_txt = ("DRC PASS" if drc_pass else "DRC FAIL") if drc_pass is not None else "DRC ?"
        erc_color = "#56d364" if erc_pass else ("#f85149" if erc_pass is False else "#999999")
        drc_color = "#56d364" if drc_pass else ("#f85149" if drc_pass is False else "#999999")
        ax.text(board_w / 2, -3.5, erc_txt,
                ha="right", va="center", fontsize=10,
                color=erc_color, weight="bold")
        ax.text(board_w / 2 + 0.5, -3.5, "  " + drc_txt,
                ha="left", va="center", fontsize=10,
                color=drc_color, weight="bold")

    # Component count by type, in legend
    counts: dict[str, int] = {}
    for c in components:
        ref_prefix = "".join(ch for ch in c.get("ref", "?") if ch.isalpha()) or "?"
        counts[ref_prefix] = counts.get(ref_prefix, 0) + 1
    legend_text = "  |  ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    ax.text(0, board_h + 2.5, legend_text,
            ha="left", va="bottom", fontsize=9, color="#0d1117")

    margin = max(5, min(board_w, board_h) * 0.1)
    ax.set_xlim(-margin, board_w + margin)
    ax.set_ylim(-margin - 4, board_h + margin)
    ax.set_aspect("equal")
    ax.set_xticks([0, board_w / 2, board_w])
    ax.set_yticks([0, board_h / 2, board_h])
    ax.set_xlabel("X (mm)", fontsize=9)
    ax.set_ylabel("Y (mm)", fontsize=9)
    ax.grid(True, alpha=0.15, color="white")

    if out_png is None:
        out_png = bom_path.parent / f"{bom_path.stem}_preview.png"
    out_png = Path(out_png)
    fig.tight_layout()
    fig.savefig(str(out_png), dpi=140, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_png


def _infer_board_size(components: list, axis: str) -> float:
    """Infer board size from component bounding-box extents if not provided."""
    if not components:
        return 50.0
    if axis == "w":
        return max((float(c.get("x_mm", 0)) + float(c.get("width_mm", 0))
                    for c in components), default=50.0) + 2.0
    return max((float(c.get("y_mm", 0)) + float(c.get("height_mm", 0))
                for c in components), default=50.0) + 2.0


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m aria_os.ecad.ecad_preview <bom.json> [--out file.png]")
        sys.exit(1)
    bom = sys.argv[1]
    out = None
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
    p = render_pcb_preview(bom, out_png=out)
    print(f"PCB preview written: {p}")
