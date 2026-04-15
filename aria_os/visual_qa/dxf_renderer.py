"""
aria_os.visual_qa.dxf_renderer — render DXF files to PNG via matplotlib.

Uses ezdxf to parse the file and matplotlib's Agg backend so the
renderer works on headless Linux (no OpenGL needed). Produces a PNG
showing the flat pattern with each layer in a distinct colour, plus a
returned metadata dict (bbox, per-layer entity counts, output path).

Part of the reusable ``aria_os.visual_qa`` visual verification
framework. Never raises — on failure returns a dict with ``ok=False``
and an ``error`` key.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


# Colour table for common sheet-metal layers. Anything not listed falls
# back to a deterministic colour so repeated renders stay stable.
_LAYER_COLOURS = {
    "OUTLINE": "#1f77b4",   # blue — the cut profile
    "BEND":    "#d62728",   # red  — bend lines
    "HOLES":   "#2ca02c",   # green — holes / punches
    "TITLE":   "#888888",   # gray — title block text
    "DIM":     "#ff7f0e",   # orange — dimensions
}

_FALLBACK_COLOURS = [
    "#9467bd", "#8c564b", "#e377c2", "#bcbd22", "#17becf",
]


def _colour_for_layer(name: str, seen: dict[str, str]) -> str:
    if name in seen:
        return seen[name]
    if name in _LAYER_COLOURS:
        seen[name] = _LAYER_COLOURS[name]
        return seen[name]
    idx = len(seen) % len(_FALLBACK_COLOURS)
    seen[name] = _FALLBACK_COLOURS[idx]
    return seen[name]


def render_dxf(
    dxf_path: str | Path,
    png_out: str | Path,
    layers: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Render a DXF to a PNG, returning metadata about what was drawn.

    Args:
        dxf_path: path to an ezdxf-readable DXF file.
        png_out:  where the rendered PNG should be written.
        layers:   if set, only entities on those layers are drawn.
                  ``None`` draws every layer.

    Returns:
        Always a dict. On success:
            {
              "ok": True,
              "png_path": str,
              "bbox": {"xmin","ymin","xmax","ymax","width","height"} | None,
              "layer_counts": {"LAYER": int, ...},
              "entity_total": int,
            }
        On failure:
            {"ok": False, "error": "<message>", "png_path": None}
    """
    dxf_path = Path(dxf_path)
    png_out = Path(png_out)

    if not dxf_path.is_file():
        return {"ok": False, "error": f"dxf not found: {dxf_path}", "png_path": None}

    try:
        import ezdxf  # type: ignore
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"ezdxf import failed: {exc}", "png_path": None}

    try:
        import matplotlib
        matplotlib.use("Agg")  # headless-safe
        import matplotlib.pyplot as plt
        from matplotlib.collections import LineCollection
        from matplotlib.patches import Circle
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"matplotlib import failed: {exc}", "png_path": None}

    try:
        doc = ezdxf.readfile(str(dxf_path))
    except Exception as exc:
        return {"ok": False, "error": f"ezdxf readfile failed: {exc}", "png_path": None}

    msp = doc.modelspace()
    layer_counts: dict[str, int] = {}
    colour_map: dict[str, str] = {}

    # Accumulators per layer so we can draw a single LineCollection per
    # colour — orders of magnitude faster than per-entity plt.plot calls.
    segs_by_colour: dict[str, list] = {}
    circles: list[tuple[float, float, float, str]] = []
    xs: list[float] = []
    ys: list[float] = []

    def _add_seg(layer: str, p1: tuple[float, float], p2: tuple[float, float]) -> None:
        colour = _colour_for_layer(layer, colour_map)
        segs_by_colour.setdefault(colour, []).append([p1, p2])
        xs.extend([p1[0], p2[0]])
        ys.extend([p1[1], p2[1]])

    for ent in msp:
        layer = getattr(ent.dxf, "layer", "0")
        if layers is not None and layer not in layers:
            continue
        layer_counts[layer] = layer_counts.get(layer, 0) + 1
        etype = ent.dxftype()

        try:
            if etype == "LINE":
                p1 = (float(ent.dxf.start[0]), float(ent.dxf.start[1]))
                p2 = (float(ent.dxf.end[0]), float(ent.dxf.end[1]))
                _add_seg(layer, p1, p2)
            elif etype == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in ent.get_points("xy")]
                closed = bool(getattr(ent, "closed", False) or ent.dxf.flags & 1)
                if closed and len(pts) >= 3:
                    pts = pts + [pts[0]]
                for a, b in zip(pts, pts[1:]):
                    _add_seg(layer, a, b)
            elif etype == "POLYLINE":
                pts = [(float(v.dxf.location[0]), float(v.dxf.location[1])) for v in ent.vertices]
                for a, b in zip(pts, pts[1:]):
                    _add_seg(layer, a, b)
            elif etype == "CIRCLE":
                cx = float(ent.dxf.center[0])
                cy = float(ent.dxf.center[1])
                r = float(ent.dxf.radius)
                colour = _colour_for_layer(layer, colour_map)
                circles.append((cx, cy, r, colour))
                xs.extend([cx - r, cx + r])
                ys.extend([cy - r, cy + r])
            elif etype == "ARC":
                # Approximate an arc with 24 segments — fine for preview.
                import math as _math
                cx = float(ent.dxf.center[0])
                cy = float(ent.dxf.center[1])
                r = float(ent.dxf.radius)
                a0 = _math.radians(float(ent.dxf.start_angle))
                a1 = _math.radians(float(ent.dxf.end_angle))
                if a1 < a0:
                    a1 += 2 * _math.pi
                steps = 24
                prev = (cx + r * _math.cos(a0), cy + r * _math.sin(a0))
                for i in range(1, steps + 1):
                    t = a0 + (a1 - a0) * i / steps
                    cur = (cx + r * _math.cos(t), cy + r * _math.sin(t))
                    _add_seg(layer, prev, cur)
                    prev = cur
            # TEXT / MTEXT / other entities are counted but not drawn.
        except Exception:
            # One bad entity should not kill the whole render.
            continue

    try:
        fig, ax = plt.subplots(figsize=(8, 8))
        for colour, segs in segs_by_colour.items():
            lc = LineCollection(segs, colors=colour, linewidths=1.0)
            ax.add_collection(lc)
        for cx, cy, r, colour in circles:
            ax.add_patch(Circle((cx, cy), r, fill=False, edgecolor=colour, linewidth=1.0))
        ax.set_aspect("equal", adjustable="datalim")
        ax.autoscale()  # REQUIRED after add_collection/add_patch
        ax.set_title(f"{dxf_path.name} — layers: {', '.join(sorted(layer_counts)) or 'none'}")
        ax.grid(True, linestyle=":", alpha=0.3)

        png_out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(png_out), dpi=120, bbox_inches="tight")
        plt.close(fig)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"matplotlib render failed: {exc}",
            "png_path": None,
            "layer_counts": layer_counts,
        }

    if xs and ys:
        bbox = {
            "xmin": float(min(xs)),
            "ymin": float(min(ys)),
            "xmax": float(max(xs)),
            "ymax": float(max(ys)),
            "width": float(max(xs) - min(xs)),
            "height": float(max(ys) - min(ys)),
        }
    else:
        bbox = None

    return {
        "ok": True,
        "png_path": str(png_out),
        "bbox": bbox,
        "layer_counts": layer_counts,
        "entity_total": sum(layer_counts.values()),
    }
