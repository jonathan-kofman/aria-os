"""GD&T engineering drawing generator for ARIA-OS.

Takes a STEP file, projects orthographic views via CadQuery, then composes
them into a professional ISO/ASME-style A3 landscape SVG with full title block,
dimension annotations, GD&T feature control frames, section view, and zone marks.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_gdnt_drawing(
    step_path: str | Path,
    part_id: str,
    params: dict | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Generate a GD&T engineering drawing SVG from a STEP file.

    Returns path to the output SVG file.
    Output: outputs/drawings/<part_id>.svg
    """
    step_path = Path(step_path)
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    out_dir = repo_root / "outputs" / "drawings"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{part_id}.svg"

    params = params or {}

    bb: _BBox | None = None
    svg_front: str | None = None
    svg_top: str | None = None
    svg_right: str | None = None
    svg_iso: str | None = None
    svg_section: str | None = None

    if step_path.exists():
        try:
            bb, svg_front, svg_top, svg_right, svg_iso, svg_section = _load_projections(step_path)
        except Exception as exc:
            print(f"[GD&T] CadQuery projection failed ({exc}); generating fallback drawing.")

    svg_content = _compose_drawing(
        part_id=part_id,
        params=params,
        bb=bb,
        svg_front=svg_front,
        svg_top=svg_top,
        svg_right=svg_right,
        svg_iso=svg_iso,
        svg_section=svg_section,
    )

    out_path.write_text(svg_content, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# BBox and projection loading
# ---------------------------------------------------------------------------

class _BBox:
    """Minimal bounding-box wrapper."""
    def __init__(self, xmin, xmax, ymin, ymax, zmin, zmax):
        self.xmin = xmin; self.xmax = xmax
        self.ymin = ymin; self.ymax = ymax
        self.zmin = zmin; self.zmax = zmax
        self.xlen = xmax - xmin
        self.ylen = ymax - ymin
        self.zlen = zmax - zmin


def _load_projections(step_path: Path):
    """Load a STEP file with CadQuery and export orthographic SVG projections.

    Returns (BBox, svg_front, svg_top, svg_right, svg_iso, svg_section).
    The section view is a real cross-section cut at the Y-axis midpoint.
    """
    import cadquery as cq
    from cadquery import exporters, importers  # type: ignore

    shape = importers.importStep(str(step_path))
    raw_bb = shape.val().BoundingBox()
    bb = _BBox(raw_bb.xmin, raw_bb.xmax, raw_bb.ymin, raw_bb.ymax, raw_bb.zmin, raw_bb.zmax)

    _PROJ_OPTS = {"showAxes": False, "strokeColor": (0, 0, 0), "hiddenColor": (160, 160, 160)}

    def _get_svg(solid, direction: tuple) -> str:
        opts = {**_PROJ_OPTS, "projectionDir": direction}
        try:
            return exporters.getSVG(solid, opts=opts)
        except Exception:
            try:
                return exporters.getSVG(solid)
            except Exception:
                return ""

    solid = shape.val()
    svg_front = _get_svg(solid, (0, -1, 0))
    svg_top = _get_svg(solid, (0, 0, 1))
    svg_right = _get_svg(solid, (1, 0, 0))
    iso_dir = (1 / math.sqrt(3), -1 / math.sqrt(3), 1 / math.sqrt(3))
    svg_iso = _get_svg(solid, iso_dir)

    # --- Real section view: cut the solid at Y midpoint ---
    svg_section = ""
    try:
        y_mid = (raw_bb.ymin + raw_bb.ymax) / 2
        # Create a cutting box that covers the positive-Y half of the solid
        cut_x = (raw_bb.xmax - raw_bb.xmin) * 2
        cut_y = (raw_bb.ymax - raw_bb.ymin)
        cut_z = (raw_bb.zmax - raw_bb.zmin) * 2
        cutting_box = (
            cq.Workplane("XZ")
            .workplane(offset=y_mid)
            .rect(cut_x, cut_z)
            .extrude(cut_y)
        )
        half_solid = shape.cut(cutting_box)
        # Project looking into the cut face (from -Y direction)
        svg_section = _get_svg(half_solid.val(), (0, -1, 0))
    except Exception as exc:
        print(f"[GD&T] Section cut failed ({exc}); section view will use fallback.")
        svg_section = ""

    return bb, svg_front, svg_top, svg_right, svg_iso, svg_section


# ---------------------------------------------------------------------------
# Sheet constants
# ---------------------------------------------------------------------------

# A3 landscape at 96 dpi ≈ 1587 × 1123 px
_W = 1587
_H = 1123
_BORDER_OUTER = 20   # outer margin
_BORDER_INNER = 10   # inner gap (double-line border)
_TITLE_H = 110       # title block height
_ZONE_W = 16         # zone mark strip width (left/right)
_ZONE_H = 16         # zone mark strip height (top/bottom)
_FONT = "Arial, Helvetica, sans-serif"
_MONO = "Courier New, monospace"
_COL_BLUE = "#1a1a8c"
_COL_DIM = "#1a1a8c"
_COL_GDT = "#1a1a8c"
_COL_BORDER = "#000000"
_COL_CENTER = "#0000ff"
_COL_HATCH = "#222222"

# Drawing area inner border rect
_IX = _BORDER_OUTER + _BORDER_INNER  # inner border x
_IY = _BORDER_OUTER + _BORDER_INNER  # inner border y
_IW = _W - 2 * (_BORDER_OUTER + _BORDER_INNER)
_IH = _H - 2 * (_BORDER_OUTER + _BORDER_INNER)

# View area: inside inner border, above title block
_VA_X = _IX
_VA_Y = _IY
_VA_W = _IW
_VA_H = _IH - _TITLE_H

# Quadrant layout
_Q_W = _VA_W // 2
_Q_H = _VA_H // 2

_PAD = 12  # padding inside each quadrant


# ---------------------------------------------------------------------------
# Part type detection
# ---------------------------------------------------------------------------

def _detect_part_type(bb: _BBox | None, params: dict) -> str:
    """Detect part type from params and bounding box.

    Returns one of: 'cylindrical_hollow', 'cylindrical_solid', 'flat', 'box'
    """
    od = params.get("od_mm")
    bore = params.get("bore_mm")

    if od is not None and bore is not None:
        return "cylindrical_hollow"

    if od is not None and bore is None:
        return "cylindrical_solid"

    if bb is not None:
        xlen, ylen, zlen = bb.xlen, bb.ylen, bb.zlen
        # Cylindrical: x≈y and z is the axis (or z is short, x≈y for disc)
        if abs(xlen - ylen) < max(xlen, ylen) * 0.1:
            if params.get("od_mm"):
                if params.get("bore_mm"):
                    return "cylindrical_hollow"
                return "cylindrical_solid"
            # Disc-like: z much shorter than diameter
            if zlen < min(xlen, ylen) * 0.5:
                return "flat"
        # Flat plate: z < 30% of smallest face dim
        if zlen < min(xlen, ylen) * 0.3:
            return "flat"

    return "box"


# ---------------------------------------------------------------------------
# Center-line helpers
# ---------------------------------------------------------------------------

def _draw_center_lines(rx: float, ry: float, rw: float, rh: float,
                        cx: float, cy: float) -> list[str]:
    """Dash-dot center lines through (cx, cy) within rect (rx, ry, rw, rh)."""
    dash = "stroke-dasharray=\"12,3,2,3\""
    style = (f'stroke="{_COL_CENTER}" stroke-width="0.4" {dash} fill="none"')
    ext = 8  # extension beyond view edge
    return [
        # Horizontal center line
        f'  <line x1="{rx - ext}" y1="{cy}" x2="{rx + rw + ext}" y2="{cy}" {style}/>',
        # Vertical center line
        f'  <line x1="{cx}" y1="{ry - ext}" x2="{cx}" y2="{ry + rh + ext}" {style}/>',
    ]


# ---------------------------------------------------------------------------
# Datum triangle
# ---------------------------------------------------------------------------

def _draw_datum_triangle(x: float, y: float, label: str, direction: str = "up") -> list[str]:
    """Draw a filled datum triangle with a square label box.

    direction: 'up' | 'down' | 'left' | 'right'
    The triangle tip points in the given direction.
    """
    s = 9  # half-size of triangle base
    if direction == "up":
        pts = f"{x},{y} {x - s},{y + s * 1.6} {x + s},{y + s * 1.6}"
        bx, by = x - 9, y + s * 1.6
    elif direction == "down":
        pts = f"{x},{y} {x - s},{y - s * 1.6} {x + s},{y - s * 1.6}"
        bx, by = x - 9, y - s * 1.6 - 18
    elif direction == "left":
        pts = f"{x},{y} {x + s * 1.6},{y - s} {x + s * 1.6},{y + s}"
        bx, by = x + s * 1.6, y - 9
    else:  # right
        pts = f"{x},{y} {x - s * 1.6},{y - s} {x - s * 1.6},{y + s}"
        bx, by = x - s * 1.6 - 18, y - 9

    bw, bh = 18, 18
    return [
        f'  <polygon points="{pts}" fill="{_COL_BLUE}" stroke="{_COL_BLUE}" stroke-width="0.5"/>',
        f'  <rect x="{bx}" y="{by}" width="{bw}" height="{bh}" '
        f'fill="white" stroke="{_COL_BLUE}" stroke-width="1"/>',
        f'  <text x="{bx + bw / 2}" y="{by + bh - 4}" text-anchor="middle" '
        f'font-size="11" font-weight="bold" fill="{_COL_BLUE}" font-family="{_FONT}">'
        f'{_escape(label)}</text>',
    ]


# ---------------------------------------------------------------------------
# GD&T feature control frame
# ---------------------------------------------------------------------------

def _gdnt_frame(x: float, y: float, symbol: str,
                tolerance: str, datums: list[str] | None = None) -> list[str]:
    """Render a proper ISO compartmentalized GD&T feature control frame.

    Compartments: [symbol 28px] | [tolerance 70px] | [datum 25px each]
    Total height: 22px
    """
    datums = datums or []
    sym_w = 28
    tol_w = 70
    dat_w = 25
    h = 22
    total_w = sym_w + tol_w + dat_w * len(datums)

    lines: list[str] = []
    # Outer box
    lines.append(
        f'  <rect x="{x}" y="{y}" width="{total_w}" height="{h}" '
        f'fill="#fffff8" stroke="{_COL_BORDER}" stroke-width="1"/>'
    )
    # Divider after symbol cell
    lines.append(
        f'  <line x1="{x + sym_w}" y1="{y}" x2="{x + sym_w}" y2="{y + h}" '
        f'stroke="{_COL_BORDER}" stroke-width="1"/>'
    )
    # Divider after tolerance cell
    if datums:
        lines.append(
            f'  <line x1="{x + sym_w + tol_w}" y1="{y}" '
            f'x2="{x + sym_w + tol_w}" y2="{y + h}" '
            f'stroke="{_COL_BORDER}" stroke-width="1"/>'
        )
    # Datum dividers
    for i in range(1, len(datums)):
        dx = x + sym_w + tol_w + dat_w * i
        lines.append(
            f'  <line x1="{dx}" y1="{y}" x2="{dx}" y2="{y + h}" '
            f'stroke="{_COL_BORDER}" stroke-width="1"/>'
        )

    # Symbol text
    lines.append(
        f'  <text x="{x + sym_w / 2}" y="{y + h - 5}" text-anchor="middle" '
        f'font-size="13" font-weight="bold" fill="{_COL_GDT}" font-family="{_FONT}">'
        f'{_escape(symbol)}</text>'
    )
    # Tolerance text
    lines.append(
        f'  <text x="{x + sym_w + 6}" y="{y + h - 5}" '
        f'font-size="10" fill="#000" font-family="{_FONT}">'
        f'{_escape(tolerance)}</text>'
    )
    # Datum texts
    for i, d in enumerate(datums):
        dx = x + sym_w + tol_w + dat_w * i + dat_w / 2
        lines.append(
            f'  <text x="{dx}" y="{y + h - 5}" text-anchor="middle" '
            f'font-size="10" font-weight="bold" fill="#000" font-family="{_FONT}">'
            f'{_escape(d)}</text>'
        )

    return lines


# ---------------------------------------------------------------------------
# Leader line
# ---------------------------------------------------------------------------

def _leader_line(x1: float, y1: float, x2: float, y2: float, text: str,
                 text_side: str = "end") -> list[str]:
    """Angled leader line with arrowhead at (x1,y1) and text near (x2,y2)."""
    # Arrow direction
    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        return []
    ux, uy = dx / length, dy / length

    al = 8  # arrow length
    ah = 3  # arrow half-width
    # Perpendicular
    px, py = -uy * ah, ux * ah

    # Arrowhead tip at (x1, y1)
    tip_x, tip_y = x1, y1
    base_x, base_y = x1 + ux * al, y1 + uy * al
    pts = (f"{tip_x:.1f},{tip_y:.1f} "
           f"{base_x + px:.1f},{base_y + py:.1f} "
           f"{base_x - px:.1f},{base_y - py:.1f}")

    # Short horizontal shelf at end
    shelf_len = 20
    shelf_dir = 1 if x2 >= x1 else -1
    sx2 = x2 + shelf_dir * shelf_len

    lines = [
        f'  <line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="#333" stroke-width="0.8"/>',
        f'  <line x1="{x2:.1f}" y1="{y2:.1f}" x2="{sx2:.1f}" y2="{y2:.1f}" '
        f'stroke="#333" stroke-width="0.8"/>',
        f'  <polygon points="{pts}" fill="#333"/>',
    ]
    tx = sx2 + shelf_dir * 3
    anchor = "start" if shelf_dir > 0 else "end"
    lines.append(
        f'  <text x="{tx:.1f}" y="{y2 + 4:.1f}" font-size="10" '
        f'text-anchor="{anchor}" fill="#000" font-family="{_FONT}">'
        f'{_escape(text)}</text>'
    )
    return lines


# ---------------------------------------------------------------------------
# Dimension line helpers
# ---------------------------------------------------------------------------

def _dim_line_h(x1: float, x2: float, y: float, text: str,
                *, color: str = _COL_DIM, ext_len: float = 14) -> list[str]:
    """Horizontal dimension line with proper extension lines and arrows."""
    ah = 3   # arrowhead half-height
    al = 7   # arrowhead length
    cx = (x1 + x2) / 2
    return [
        # Extension lines (go from view edge 6px past, then 8px gap to dim line)
        f'  <line x1="{x1}" y1="{y - ext_len}" x2="{x1}" y2="{y + 4}" '
        f'stroke="{color}" stroke-width="0.7"/>',
        f'  <line x1="{x2}" y1="{y - ext_len}" x2="{x2}" y2="{y + 4}" '
        f'stroke="{color}" stroke-width="0.7"/>',
        # Main dimension line
        f'  <line x1="{x1}" y1="{y}" x2="{x2}" y2="{y}" '
        f'stroke="{color}" stroke-width="0.8"/>',
        # Arrowheads (inward)
        f'  <polygon points="{x1},{y} {x1 + al},{y - ah} {x1 + al},{y + ah}" fill="{color}"/>',
        f'  <polygon points="{x2},{y} {x2 - al},{y - ah} {x2 - al},{y + ah}" fill="{color}"/>',
        # Label above dim line
        f'  <text x="{cx}" y="{y - 4}" text-anchor="middle" font-size="10" '
        f'fill="{color}" font-family="{_FONT}">{_escape(text)}</text>',
    ]


def _dim_line_v(x: float, y1: float, y2: float, text: str,
                *, color: str = _COL_DIM, ext_len: float = 14) -> list[str]:
    """Vertical dimension line with proper extension lines and arrows."""
    ah = 3
    al = 7
    cy = (y1 + y2) / 2
    return [
        # Extension lines
        f'  <line x1="{x - 4}" y1="{y1}" x2="{x + ext_len}" y2="{y1}" '
        f'stroke="{color}" stroke-width="0.7"/>',
        f'  <line x1="{x - 4}" y1="{y2}" x2="{x + ext_len}" y2="{y2}" '
        f'stroke="{color}" stroke-width="0.7"/>',
        # Main dimension line
        f'  <line x1="{x}" y1="{y1}" x2="{x}" y2="{y2}" '
        f'stroke="{color}" stroke-width="0.8"/>',
        # Arrowheads
        f'  <polygon points="{x},{y1} {x - ah},{y1 + al} {x + ah},{y1 + al}" fill="{color}"/>',
        f'  <polygon points="{x},{y2} {x - ah},{y2 - al} {x + ah},{y2 - al}" fill="{color}"/>',
        # Rotated label
        f'  <text x="{x - 8}" y="{cy}" text-anchor="middle" font-size="10" '
        f'fill="{color}" font-family="{_FONT}" '
        f'transform="rotate(-90,{x - 8},{cy})">{_escape(text)}</text>',
    ]


# ---------------------------------------------------------------------------
# Hatching helper (45-degree crosshatch clipped to a rect)
# ---------------------------------------------------------------------------

def _hatch_rect(rx: float, ry: float, rw: float, rh: float,
                clip_id: str, spacing: float = 2.5) -> list[str]:
    """Generate 45-degree crosshatch lines clipped to rect (rx,ry,rw,rh)."""
    lines: list[str] = [
        f'  <clipPath id="{clip_id}">',
        f'    <rect x="{rx}" y="{ry}" width="{rw}" height="{rh}"/>',
        f'  </clipPath>',
        f'  <g clip-path="url(#{clip_id})">',
    ]
    # Diagonal lines at 45 degrees
    diag = rw + rh
    step = spacing
    t = -diag
    while t <= diag:
        # Lines going ↘
        x1, y1 = rx + t, ry
        x2, y2 = rx + t + rh, ry + rh
        lines.append(
            f'    <line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{_COL_HATCH}" stroke-width="0.4"/>'
        )
        t += step
    lines.append('  </g>')
    return lines


# ---------------------------------------------------------------------------
# Section view (parametric, not from CadQuery)
# ---------------------------------------------------------------------------

def _draw_section_view(params: dict, bb: _BBox | None,
                       rx: float, ry: float, rw: float, rh: float,
                       part_type: str) -> list[str]:
    """Draw a true parametric cross-section SVG in the given rect.

    Returns list of SVG element strings.
    """
    lines: list[str] = []
    pad = _PAD + 20

    # Resolve dimensions
    od = float(params.get("od_mm", bb.xlen if bb else 80))
    bore = float(params.get("bore_mm", 0))
    height = float(params.get("height_mm", params.get("thickness_mm",
                   bb.zlen if bb else 40)))
    width_p = float(params.get("width_mm", bb.xlen if bb else od))
    depth_p = float(params.get("depth_mm", bb.ylen if bb else od))

    if part_type in ("cylindrical_hollow", "cylindrical_solid"):
        # Available space
        avail_w = rw - pad * 2
        avail_h = rh - pad * 2 - 30  # 30px for labels below

        # Scale factor
        scale = min(avail_w / od, avail_h / height) if (od > 0 and height > 0) else 1.0

        scaled_od = od * scale
        scaled_bore = bore * scale
        scaled_h = height * scale

        # Center of section
        cx = rx + rw / 2
        cy = ry + pad + avail_h / 2

        sx = cx - scaled_od / 2
        sy = cy - scaled_h / 2

        wall = (od - bore) / 2 if bore > 0 else od / 2
        scaled_wall = wall * scale

        # Clip id for hatching
        cl_id_l = "hatch_sec_l"
        cl_id_r = "hatch_sec_r"

        if part_type == "cylindrical_hollow" and bore > 0:
            # Left wall rect
            lx, ly, lw, lh = sx, sy, scaled_wall, scaled_h
            # Right wall rect
            rrx = cx + scaled_bore / 2
            rry, rrw, rrh = sy, scaled_wall, scaled_h

            # Draw walls (filled light gray)
            lines.append(
                f'  <rect x="{lx:.1f}" y="{ly:.1f}" width="{lw:.1f}" height="{lh:.1f}" '
                f'fill="#e8e8e8" stroke="black" stroke-width="0.8"/>'
            )
            lines.append(
                f'  <rect x="{rrx:.1f}" y="{rry:.1f}" width="{rrw:.1f}" height="{rrh:.1f}" '
                f'fill="#e8e8e8" stroke="black" stroke-width="0.8"/>'
            )
            # Hatching
            lines.extend(_hatch_rect(lx, ly, lw, lh, cl_id_l, 2.5))
            lines.extend(_hatch_rect(rrx, rry, rrw, rrh, cl_id_r, 2.5))

            # Bore bottom/top caps
            lines.append(
                f'  <line x1="{cx - scaled_bore / 2:.1f}" y1="{sy:.1f}" '
                f'x2="{cx + scaled_bore / 2:.1f}" y2="{sy:.1f}" '
                f'stroke="black" stroke-width="0.8"/>'
            )
            lines.append(
                f'  <line x1="{cx - scaled_bore / 2:.1f}" y1="{sy + scaled_h:.1f}" '
                f'x2="{cx + scaled_bore / 2:.1f}" y2="{sy + scaled_h:.1f}" '
                f'stroke="black" stroke-width="0.8"/>'
            )

            # Center line through bore
            lines.extend(_draw_center_lines(
                cx - scaled_bore / 2, sy, scaled_bore, scaled_h,
                cx, cy
            ))

            # Datum B on bore center
            lines.extend(_draw_datum_triangle(cx, sy + scaled_h, "B", "down"))

            # OD dimension line (below section)
            dim_y = sy + scaled_h + 30
            lines.extend(_dim_line_h(sx, sx + scaled_od, dim_y,
                                     f"\u00d8{od:.1f}", color=_COL_DIM))

            # Bore dimension (above, shorter)
            dim_y_top = sy - 20
            lines.extend(_dim_line_h(cx - scaled_bore / 2, cx + scaled_bore / 2,
                                     dim_y_top, f"\u00d8{bore:.1f} BORE", color=_COL_DIM))

            # Wall thickness (leader on left wall)
            wall_mid_x = sx + scaled_wall / 2
            wall_mid_y = cy
            lines.extend(_leader_line(wall_mid_x, wall_mid_y,
                                      sx - 25, cy - 15,
                                      f"WALL {wall:.1f}"))

        else:
            # Solid cylinder section: full rect with hatching
            lines.append(
                f'  <rect x="{sx:.1f}" y="{sy:.1f}" width="{scaled_od:.1f}" height="{scaled_h:.1f}" '
                f'fill="#e8e8e8" stroke="black" stroke-width="0.8"/>'
            )
            lines.extend(_hatch_rect(sx, sy, scaled_od, scaled_h, "hatch_sec_solid", 2.5))
            lines.extend(_draw_center_lines(sx, sy, scaled_od, scaled_h, cx, cy))

            dim_y = sy + scaled_h + 28
            lines.extend(_dim_line_h(sx, sx + scaled_od, dim_y,
                                     f"\u00d8{od:.1f}", color=_COL_DIM))

        # Height dimension on left
        hx = sx - 35
        lines.extend(_dim_line_v(hx, sy, sy + scaled_h,
                                 f"{height:.1f}", color=_COL_DIM))

    elif part_type == "flat":
        # Show edge-on view of plate
        thickness = float(params.get("thickness_mm",
                          params.get("depth_mm", bb.zlen if bb else 10)))
        w_dim = float(params.get("width_mm", bb.xlen if bb else 100))
        avail_w = rw - pad * 2
        avail_h = rh - pad * 2 - 30

        scale = min(avail_w / w_dim, avail_h / max(thickness * 5, 20)) if w_dim > 0 else 1.0
        scaled_w = w_dim * scale
        scaled_t = max(thickness * scale, 6)  # min 6px for visibility

        cx = rx + rw / 2
        cy = ry + rh / 2
        sx = cx - scaled_w / 2
        sy = cy - scaled_t / 2

        lines.append(
            f'  <rect x="{sx:.1f}" y="{sy:.1f}" width="{scaled_w:.1f}" height="{scaled_t:.1f}" '
            f'fill="#e8e8e8" stroke="black" stroke-width="0.8"/>'
        )
        lines.extend(_hatch_rect(sx, sy, scaled_w, scaled_t, "hatch_flat", 2.5))

        # Thickness dim
        dim_x = sx + scaled_w + 25
        lines.extend(_dim_line_v(dim_x, sy, sy + scaled_t,
                                 f"{thickness:.1f}", color=_COL_DIM))

        # Width dim
        dim_y = sy + scaled_t + 25
        lines.extend(_dim_line_h(sx, sx + scaled_w, dim_y,
                                 f"{w_dim:.1f}", color=_COL_DIM))

    else:
        # Box / housing: draw a simple cross-section rect
        w_dim = float(params.get("width_mm", bb.xlen if bb else 80))
        h_dim = float(params.get("height_mm", bb.zlen if bb else 60))
        wall = float(params.get("wall_mm", max(4.0, w_dim * 0.08)))

        avail_w = rw - pad * 2
        avail_h = rh - pad * 2 - 30

        scale = min(avail_w / w_dim, avail_h / h_dim) if (w_dim > 0 and h_dim > 0) else 1.0
        scaled_w = w_dim * scale
        scaled_h = h_dim * scale
        scaled_wall = wall * scale

        cx = rx + rw / 2
        cy = ry + rh / 2
        sx = cx - scaled_w / 2
        sy = cy - scaled_h / 2

        # Outer box
        lines.append(
            f'  <rect x="{sx:.1f}" y="{sy:.1f}" width="{scaled_w:.1f}" height="{scaled_h:.1f}" '
            f'fill="#e8e8e8" stroke="black" stroke-width="0.8"/>'
        )
        # Inner void
        iw = max(scaled_w - 2 * scaled_wall, 2)
        ih = max(scaled_h - 2 * scaled_wall, 2)
        lines.append(
            f'  <rect x="{sx + scaled_wall:.1f}" y="{sy + scaled_wall:.1f}" '
            f'width="{iw:.1f}" height="{ih:.1f}" '
            f'fill="white" stroke="black" stroke-width="0.6"/>'
        )
        # Hatch walls (using outer rect clipped minus inner)
        lines.extend(_hatch_rect(sx, sy, scaled_wall, scaled_h, "hatch_box_l", 2.5))
        lines.extend(_hatch_rect(sx + scaled_w - scaled_wall, sy,
                                 scaled_wall, scaled_h, "hatch_box_r", 2.5))
        lines.extend(_hatch_rect(sx + scaled_wall, sy,
                                 scaled_w - 2 * scaled_wall, scaled_wall, "hatch_box_t", 2.5))
        lines.extend(_hatch_rect(sx + scaled_wall, sy + scaled_h - scaled_wall,
                                 scaled_w - 2 * scaled_wall, scaled_wall, "hatch_box_b", 2.5))

        lines.extend(_draw_center_lines(sx, sy, scaled_w, scaled_h, cx, cy))

        dim_y = sy + scaled_h + 28
        lines.extend(_dim_line_h(sx, sx + scaled_w, dim_y,
                                 f"{w_dim:.1f}", color=_COL_DIM))
        dim_x = sx - 35
        lines.extend(_dim_line_v(dim_x, sy, sy + scaled_h,
                                 f"{h_dim:.1f}", color=_COL_DIM))

    # Section label
    lx = rx + rw / 2
    label_y = ry + rh - 6
    lines.append(
        f'  <text x="{lx}" y="{label_y}" text-anchor="middle" '
        f'font-size="9" font-weight="bold" fill="#333" font-family="{_FONT}">'
        f'SECTION A-A  &#x2014;  SCALE 1:1</text>'
    )

    return lines


# ---------------------------------------------------------------------------
# Section cut line in front view
# ---------------------------------------------------------------------------

def _draw_section_cut_line(rx: float, ry: float, rw: float, rh: float,
                            cy: float) -> list[str]:
    """Draw a section-cut line across the front view at height cy."""
    dash = "stroke-dasharray=\"8,4\""
    lines: list[str] = [
        f'  <line x1="{rx + 4}" y1="{cy}" x2="{rx + rw - 4}" y2="{cy}" '
        f'stroke="#555" stroke-width="1" {dash}/>',
    ]
    # Arrow at left end (pointing right →)
    ax, ay = rx + 4, cy
    lines.append(
        f'  <polygon points="{ax},{ay} {ax + 10},{ay - 4} {ax + 10},{ay + 4}" '
        f'fill="#555"/>'
    )
    # Arrow at right end (pointing right →)
    bx, by = rx + rw - 4, cy
    lines.append(
        f'  <polygon points="{bx},{by} {bx - 10},{by - 4} {bx - 10},{by + 4}" '
        f'fill="#555"/>'
    )
    # "A" labels at both ends
    lines.append(
        f'  <text x="{ax + 14}" y="{ay - 6}" font-size="10" font-weight="bold" '
        f'fill="#555" font-family="{_FONT}">A</text>'
    )
    lines.append(
        f'  <text x="{bx - 20}" y="{by - 6}" font-size="10" font-weight="bold" '
        f'fill="#555" font-family="{_FONT}">A</text>'
    )
    return lines


# ---------------------------------------------------------------------------
# Zone marks
# ---------------------------------------------------------------------------

def _draw_zone_marks() -> list[str]:
    """Draw zone reference marks (A-D down sides, 1-8 across top/bottom)."""
    lines: list[str] = []

    # Outer border
    b = _BORDER_OUTER
    bi = _BORDER_OUTER + _BORDER_INNER
    w = _W
    h = _H

    letters = ["A", "B", "C", "D"]
    numbers = ["1", "2", "3", "4", "5", "6", "7", "8"]

    # Drawing inner area height for view
    inner_h = h - 2 * b
    zone_h = inner_h / len(letters)

    for i, letter in enumerate(letters):
        cy_zone = b + zone_h * i + zone_h / 2
        # Left strip
        lines.append(
            f'  <rect x="{b}" y="{b + zone_h * i}" width="{_ZONE_W}" height="{zone_h}" '
            f'fill="none" stroke="#888" stroke-width="0.4"/>'
        )
        lines.append(
            f'  <text x="{b + _ZONE_W / 2}" y="{cy_zone + 4}" text-anchor="middle" '
            f'font-size="9" fill="#444" font-family="{_FONT}">{letter}</text>'
        )
        # Right strip
        lines.append(
            f'  <rect x="{w - b - _ZONE_W}" y="{b + zone_h * i}" width="{_ZONE_W}" height="{zone_h}" '
            f'fill="none" stroke="#888" stroke-width="0.4"/>'
        )
        lines.append(
            f'  <text x="{w - b - _ZONE_W / 2}" y="{cy_zone + 4}" text-anchor="middle" '
            f'font-size="9" fill="#444" font-family="{_FONT}">{letter}</text>'
        )

    inner_w = w - 2 * b
    zone_w = inner_w / len(numbers)

    for j, number in enumerate(numbers):
        cx_zone = b + zone_w * j + zone_w / 2
        # Top strip
        lines.append(
            f'  <rect x="{b + zone_w * j}" y="{b}" width="{zone_w}" height="{_ZONE_H}" '
            f'fill="none" stroke="#888" stroke-width="0.4"/>'
        )
        lines.append(
            f'  <text x="{cx_zone}" y="{b + _ZONE_H - 4}" text-anchor="middle" '
            f'font-size="9" fill="#444" font-family="{_FONT}">{number}</text>'
        )
        # Bottom strip
        lines.append(
            f'  <rect x="{b + zone_w * j}" y="{h - b - _ZONE_H}" '
            f'width="{zone_w}" height="{_ZONE_H}" '
            f'fill="none" stroke="#888" stroke-width="0.4"/>'
        )
        lines.append(
            f'  <text x="{cx_zone}" y="{h - b - 4}" text-anchor="middle" '
            f'font-size="9" fill="#444" font-family="{_FONT}">{number}</text>'
        )

    # Revision block: top-right corner inside inner border
    rev_w, rev_h = 80, 20
    rev_x = w - bi - rev_w - 2
    rev_y = bi + 2
    lines.append(
        f'  <rect x="{rev_x}" y="{rev_y}" width="{rev_w}" height="{rev_h}" '
        f'fill="white" stroke="black" stroke-width="0.8"/>'
    )
    lines.append(
        f'  <text x="{rev_x + rev_w / 2}" y="{rev_y + 13}" text-anchor="middle" '
        f'font-size="9" fill="#000" font-family="{_FONT}">REV: &#x2014;</text>'
    )

    return lines


# ---------------------------------------------------------------------------
# Quadrant separator lines
# ---------------------------------------------------------------------------

def _draw_quadrant_lines() -> list[str]:
    """Draw the cross-lines dividing the view area into 4 quadrants."""
    lines: list[str] = []
    # Horizontal mid-line
    mid_x = _VA_X + _Q_W
    mid_y = _VA_Y + _Q_H

    lines.append(
        f'  <line x1="{_VA_X}" y1="{mid_y}" x2="{_VA_X + _VA_W}" y2="{mid_y}" '
        f'stroke="#888" stroke-width="0.6"/>'
    )
    lines.append(
        f'  <line x1="{mid_x}" y1="{_VA_Y}" x2="{mid_x}" y2="{_VA_Y + _VA_H}" '
        f'stroke="#888" stroke-width="0.6"/>'
    )
    return lines


# ---------------------------------------------------------------------------
# Dimension annotations (part-type-aware)
# ---------------------------------------------------------------------------

def _dimension_annotations(bb: _BBox, front_rect: tuple, top_rect: tuple,
                             params: dict, part_type: str) -> list[str]:
    """Return SVG lines for overall dimension annotations."""
    rx, ry, rw, rh = front_rect
    trx, try_, trw, trh = top_rect
    lines: list[str] = []

    od = params.get("od_mm")
    bore = params.get("bore_mm")
    height = params.get("height_mm", round(bb.zlen, 2))

    # --- Front view dimensions ---

    # Height (Z): vertical dim LEFT of front view
    hval = f"{height:.1f}" if isinstance(height, float) else str(height)
    dim_x_left = rx - 38
    lines.extend(_dim_line_v(dim_x_left, ry + _PAD, ry + rh - _PAD,
                              f"{hval} mm", color=_COL_DIM))

    # Datum A triangle on bottom of front view
    lines.extend(_draw_datum_triangle(rx + rw / 2, ry + rh - 2, "A", "up"))

    # Section cut line at mid-height of front view
    mid_y = ry + rh / 2
    lines.extend(_draw_section_cut_line(rx, ry, rw, rh, mid_y))

    if part_type in ("cylindrical_hollow", "cylindrical_solid"):
        # OD dimension: horizontal below front view
        od_val = od if od else round(bb.xlen, 2)
        dim_y_below = ry + rh + 28
        cx_f = rx + rw / 2
        half_od_px = rw * 0.4  # approximate scaled half-OD
        lines.extend(_dim_line_h(cx_f - half_od_px, cx_f + half_od_px,
                                  dim_y_below, f"\u00d8{od_val} mm", color=_COL_DIM))
    else:
        # Width dim below front view
        wval = round(bb.xlen, 2)
        dim_y_below = ry + rh + 28
        lines.extend(_dim_line_h(rx + _PAD, rx + rw - _PAD,
                                  dim_y_below, f"{wval} mm", color=_COL_DIM))

    # --- Top view dimensions ---

    if part_type in ("cylindrical_hollow", "cylindrical_solid"):
        od_val = od if od else round(bb.xlen, 2)
        # OD across top view (horizontal, above top view)
        dim_y_top = try_ - 22
        tcx = trx + trw / 2
        half_od_px = trw * 0.4
        lines.extend(_dim_line_h(tcx - half_od_px, tcx + half_od_px,
                                  dim_y_top, f"\u00d8{od_val} mm", color=_COL_DIM))

        if bore:
            # Leader line for bore in top view
            bore_cx = trx + trw / 2
            bore_cy = try_ + trh / 2
            lines.extend(_leader_line(bore_cx, bore_cy,
                                       trx + trw - 30, try_ + 20,
                                       f"\u00d8{bore} THRU"))
    else:
        wval = round(bb.xlen, 2)
        dval = round(bb.ylen, 2)
        # Width across top view
        dim_y_top = try_ - 22
        lines.extend(_dim_line_h(trx + _PAD, trx + trw - _PAD,
                                  dim_y_top, f"{wval} mm", color=_COL_DIM))
        # Depth: right of top view
        dim_x_right = trx + trw + 30
        lines.extend(_dim_line_v(dim_x_right, try_ + _PAD, try_ + trh - _PAD,
                                  f"{dval} mm", color=_COL_DIM))

    return lines


# ---------------------------------------------------------------------------
# GD&T symbols (part-type-aware)
# ---------------------------------------------------------------------------

def _classify_gdnt_symbols(bb: _BBox | None, params: dict, part_type: str) -> list[dict]:
    """Return GD&T symbol specs based on geometry, params, and part type.

    Checks params for bore_mm, n_teeth, n_bolts, bolt_circle_r_mm, od_mm
    to produce more specific and design-intent-driven callouts rather than
    pure heuristic guesses.
    """
    symbols: list[dict] = []

    has_bore = params.get("bore_mm") is not None and float(params.get("bore_mm", 0)) > 0
    has_teeth = params.get("n_teeth") is not None and int(params.get("n_teeth", 0)) > 0
    has_bolts = params.get("n_bolts") is not None and int(params.get("n_bolts", 0)) > 0
    has_bolt_circle = params.get("bolt_circle_r_mm") is not None
    od = params.get("od_mm")

    # Derive tighter tolerances for safety-critical or precision features
    bore_mm = float(params.get("bore_mm", 0))
    # Bore position tolerance scales with bore size (tighter for small bores)
    bore_pos_tol = f"\u00d8{max(0.02, min(0.10, bore_mm * 0.001)):.2f}" if has_bore else "\u00d80.10"

    if part_type == "cylindrical_hollow":
        symbols.append({"symbol": "\u29be", "tolerance": "0.02",     "datums": []})        # cylindricity on OD
        symbols.append({"symbol": "\u25ce", "tolerance": "\u00d80.03", "datums": ["A"]})   # concentricity bore-to-OD
        symbols.append({"symbol": "\u22a5", "tolerance": "0.05",     "datums": ["A"]})     # perpendicularity end face
        if has_bore:
            # Position tolerance for bore, derived from bore size
            symbols.append({"symbol": "\u2295", "tolerance": bore_pos_tol,
                            "datums": ["A", "B"]})  # position of bore
        if has_teeth:
            # Profile of a line for tooth form (gear / ratchet teeth)
            n_teeth = int(params.get("n_teeth", 0))
            # Tighter tolerance for more teeth (finer pitch)
            tooth_tol = f"0.{max(2, min(5, 50 // max(n_teeth, 1))):02d}"
            symbols.append({"symbol": "\u2312", "tolerance": tooth_tol,
                            "datums": ["A", "B"]})  # profile of a line
        if has_bolts:
            symbols.append({"symbol": "\u2295", "tolerance": "\u00d80.15",
                            "datums": ["A", "B"]})  # bolt hole position
        symbols.append({"symbol": None,      "tolerance": "Ra 1.6",   "datums": [],
                        "surface_finish": True})

    elif part_type == "cylindrical_solid":
        symbols.append({"symbol": "\u29be", "tolerance": "0.02",     "datums": []})        # cylindricity
        symbols.append({"symbol": "\u22a5", "tolerance": "0.05",     "datums": ["A"]})     # perpendicularity
        if has_teeth:
            n_teeth = int(params.get("n_teeth", 0))
            tooth_tol = f"0.{max(2, min(5, 50 // max(n_teeth, 1))):02d}"
            symbols.append({"symbol": "\u2312", "tolerance": tooth_tol,
                            "datums": ["A"]})  # profile of a line for teeth
        if has_bolts:
            symbols.append({"symbol": "\u2295", "tolerance": "\u00d80.15",
                            "datums": ["A"]})  # bolt hole position
        symbols.append({"symbol": None,      "tolerance": "Ra 1.6",   "datums": [],
                        "surface_finish": True})

    elif part_type == "flat":
        symbols.append({"symbol": "\u25ad", "tolerance": "0.02",     "datums": []})        # flatness
        symbols.append({"symbol": "\u22a5", "tolerance": "0.05",     "datums": ["A"]})     # perpendicularity
        if has_bore:
            symbols.append({"symbol": "\u2295", "tolerance": bore_pos_tol,
                            "datums": ["A"]})  # bore position
        if has_bolts:
            bolt_tol = "\u00d80.10" if has_bolt_circle else "\u00d80.15"
            symbols.append({"symbol": "\u2295", "tolerance": bolt_tol,
                            "datums": ["A", "B"] if has_bore else ["A"]})  # bolt pattern
        symbols.append({"symbol": None,      "tolerance": "Ra 3.2",   "datums": [],
                        "surface_finish": True})

    else:  # box / housing / generic
        symbols.append({"symbol": "\u25ad", "tolerance": "0.05",     "datums": []})        # flatness
        symbols.append({"symbol": "\u22a5", "tolerance": "0.05",     "datums": ["A"]})     # perpendicularity
        if has_bore:
            symbols.append({"symbol": "\u2295", "tolerance": bore_pos_tol,
                            "datums": ["A", "B"]})  # bore position
        if has_bolts:
            bolt_tol = "\u00d80.10" if has_bolt_circle else "\u00d80.15"
            symbols.append({"symbol": "\u2295", "tolerance": bolt_tol,
                            "datums": ["A", "B"] if has_bore else ["A"]})  # bolt pattern
        if has_teeth:
            n_teeth = int(params.get("n_teeth", 0))
            tooth_tol = f"0.{max(2, min(5, 50 // max(n_teeth, 1))):02d}"
            symbols.append({"symbol": "\u2312", "tolerance": tooth_tol,
                            "datums": ["A"]})  # profile of a line for teeth
        symbols.append({"symbol": None,      "tolerance": "Ra 3.2",   "datums": [],
                        "surface_finish": True})

    return symbols


def _render_gdnt_symbols(symbols: list[dict], front_rect: tuple,
                          gdnt_rect: tuple) -> list[str]:
    """Render GD&T frames overlaid on the right margin of the front view."""
    lines: list[str] = []

    # Position: inside the GD&T overlay area (right side of front view)
    gx, gy, gw, gh = gdnt_rect
    col_x = gx + 4
    col_y = gy + 8

    # Column header
    lines.append(
        f'  <text x="{col_x}" y="{col_y - 4}" font-size="8" font-weight="bold" '
        f'fill="#333" font-family="{_FONT}">GD&amp;T CALLOUTS</text>'
    )

    # Leader lines: pre-calculated anchors in the front view (left of GD&T column)
    frx, fry, frw, frh = front_rect
    leader_targets = [
        (gx - 20, fry + frh / 3),           # left of GD&T column, upper third
        (frx + frw / 2, fry + 30),          # top edge, center
        (gx - 20, fry + 40),                # left of GD&T column, upper
        (frx + frw / 2, fry + frh - 20),    # bottom edge
    ]

    sym_w_base = 28 + 70  # sym + tol cells min width (no datums)

    for i, sym in enumerate(symbols):
        sy_now = col_y + i * 28

        if sym.get("surface_finish"):
            # Surface finish annotation (not a proper frame)
            sf_val = sym["tolerance"]
            lines.append(
                f'  <rect x="{col_x}" y="{sy_now}" width="100" height="22" '
                f'fill="#fffff8" stroke="{_COL_BORDER}" stroke-width="0.8"/>'
            )
            lines.append(
                f'  <text x="{col_x + 6}" y="{sy_now + 15}" '
                f'font-size="11" fill="{_COL_GDT}" font-family="{_FONT}">'
                f'\u2207\u2207 {_escape(sf_val)}</text>'
            )
        else:
            symbol_char = sym["symbol"]
            tolerance = sym["tolerance"]
            datums = sym.get("datums", [])
            lines.extend(_gdnt_frame(col_x, sy_now, symbol_char, tolerance, datums))

            # Leader line from frame to view feature
            if i < len(leader_targets):
                tx, ty = leader_targets[i]
                frame_cy = sy_now + 11
                lines.append(
                    f'  <line x1="{col_x}" y1="{frame_cy:.1f}" '
                    f'x2="{tx:.1f}" y2="{ty:.1f}" '
                    f'stroke="#999" stroke-width="0.5" stroke-dasharray="4,3"/>'
                )

    return lines


# ---------------------------------------------------------------------------
# Title block
# ---------------------------------------------------------------------------

def _title_block(part_id: str, params: dict) -> list[str]:
    """Return SVG elements for the full-width title block at the bottom."""
    lines: list[str] = []

    # Title block spans full inner border width
    tb_x = _IX
    tb_y = _IY + _VA_H
    tb_w = _IW
    tb_h = _TITLE_H

    today = date.today().strftime("%Y-%m-%d")
    material = str(params.get("material", "\u2014"))
    dwg_num = f"ARIA-{date.today().strftime('%Y%m%d')}-001"

    # Outer border
    lines.append(
        f'  <rect x="{tb_x}" y="{tb_y}" width="{tb_w}" height="{tb_h}" '
        f'fill="white" stroke="black" stroke-width="1.5"/>'
    )

    # Header bar (full width, 22px tall)
    hdr_h = 22
    lines.append(
        f'  <rect x="{tb_x}" y="{tb_y}" width="{tb_w}" height="{hdr_h}" '
        f'fill="{_COL_BLUE}"/>'
    )
    lines.append(
        f'  <text x="{tb_x + tb_w / 2}" y="{tb_y + 15}" text-anchor="middle" '
        f'font-size="13" fill="white" font-weight="bold" font-family="{_FONT}">'
        f'ARIA-OS ENGINEERING DRAWING</text>'
    )

    # Column layout (6 columns, proportional)
    body_y = tb_y + hdr_h
    body_h = tb_h - hdr_h
    col_pcts = [0.20, 0.20, 0.20, 0.20, 0.10, 0.10]
    col_labels = [
        ["PART ID", "MATERIAL"],
        ["SCALE", "DATE"],
        ["DRAWN BY", "DWG NO."],
        ["TOLERANCE", "UNITS"],
        ["APPROVED", ""],
        ["REV", ""],
    ]
    col_vals = [
        [part_id, material],
        ["1:1", today],
        ["ARIA-OS v2.0", dwg_num],
        ["\u00b10.1 GENERAL / \u00b10.05 MACH.", "MILLIMETRES"],
        ["", ""],
        ["\u2014", ""],
    ]

    cx = tb_x
    for ci, pct in enumerate(col_pcts):
        cw = tb_w * pct
        # Vertical divider
        if ci > 0:
            lines.append(
                f'  <line x1="{cx}" y1="{body_y}" x2="{cx}" y2="{body_y + body_h}" '
                f'stroke="black" stroke-width="0.8"/>'
            )

        # Two rows inside this column
        row_h = body_h / 2
        labels = col_labels[ci]
        vals = col_vals[ci]

        for ri in range(2):
            ry_cell = body_y + ri * row_h
            label = labels[ri] if ri < len(labels) else ""
            val = vals[ri] if ri < len(vals) else ""

            # Horizontal divider (between rows)
            if ri == 1:
                lines.append(
                    f'  <line x1="{cx}" y1="{ry_cell}" x2="{cx + cw}" y2="{ry_cell}" '
                    f'stroke="#aaa" stroke-width="0.5"/>'
                )

            # Label (small, gray, top of cell)
            lines.append(
                f'  <text x="{cx + 4}" y="{ry_cell + 9}" font-size="7" '
                f'fill="#666" font-family="{_FONT}">{_escape(label)}</text>'
            )
            # Value (bold, larger)
            lines.append(
                f'  <text x="{cx + 4}" y="{ry_cell + row_h - 4}" font-size="10" '
                f'fill="#000" font-weight="bold" font-family="{_FONT}">'
                f'{_escape(str(val))}</text>'
            )

        cx += cw

    return lines


# ---------------------------------------------------------------------------
# Main SVG composition
# ---------------------------------------------------------------------------

def _compose_drawing(
    *,
    part_id: str,
    params: dict,
    bb: _BBox | None,
    svg_front: str | None,
    svg_top: str | None,
    svg_right: str | None = None,
    svg_iso: str | None,
    svg_section: str | None = None,
) -> str:
    """Build the full A3 SVG drawing and return it as a string.

    Quadrant layout:
      Q1 (top-left):     Front view  — projection (0, -1, 0)
      Q2 (top-right):    Top view    — projection (0, 0, 1)
      Q3 (bottom-left):  Right side  — projection (1, 0, 0)
      Q4 (bottom-right): Section A-A — real STEP cross-section
    Isometric view is rendered as a small inset in Q2.
    GD&T frames are overlaid on the right margin of Q1 (front view).
    """

    part_type = _detect_part_type(bb, params)

    lines: list[str] = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" '
        f'font-family="{_FONT}" font-size="12">'
    )

    # Background
    lines.append(f'  <rect width="{_W}" height="{_H}" fill="white"/>')

    # Outer border (thick)
    b = _BORDER_OUTER
    lines.append(
        f'  <rect x="{b}" y="{b}" width="{_W - 2*b}" height="{_H - 2*b}" '
        f'fill="none" stroke="black" stroke-width="2"/>'
    )

    # Inner border (thinner, zone-mark inset)
    bi = _BORDER_OUTER + _BORDER_INNER
    lines.append(
        f'  <rect x="{bi}" y="{bi}" width="{_W - 2*bi}" height="{_H - 2*bi}" '
        f'fill="none" stroke="black" stroke-width="1"/>'
    )

    # Zone marks
    lines.extend(_draw_zone_marks())

    # Quadrant separators
    lines.extend(_draw_quadrant_lines())

    # --- Compute quadrant rects ---
    q_pad = _PAD
    # Q1: FRONT VIEW (top-left)
    q1_x = _VA_X + q_pad
    q1_y = _VA_Y + q_pad
    q1_w = _Q_W - q_pad * 2
    q1_h = _Q_H - q_pad * 2

    # Q2: TOP VIEW (top-right)
    q2_x = _VA_X + _Q_W + q_pad
    q2_y = _VA_Y + q_pad
    q2_w = _Q_W - q_pad * 2
    q2_h = _Q_H - q_pad * 2

    # Q3: RIGHT SIDE VIEW (bottom-left)
    q3_x = _VA_X + q_pad
    q3_y = _VA_Y + _Q_H + q_pad
    q3_w = _Q_W - q_pad * 2
    q3_h = _Q_H - q_pad * 2

    # Q4: SECTION A-A (bottom-right)
    q4_x = _VA_X + _Q_W + q_pad
    q4_y = _VA_Y + _Q_H + q_pad
    q4_w = _Q_W - q_pad * 2
    q4_h = _Q_H - q_pad * 2

    # GD&T overlay area: right margin of front view (Q1)
    gdnt_col_w = 170
    gdnt_x = q1_x + q1_w - gdnt_col_w + q_pad
    gdnt_y = q1_y + 30  # below view label
    gdnt_w = gdnt_col_w - q_pad * 2
    gdnt_h = q1_h - 30

    # --- View box backgrounds ---
    for qx, qy, qw, qh in ((q1_x, q1_y, q1_w, q1_h),
                             (q2_x, q2_y, q2_w, q2_h),
                             (q3_x, q3_y, q3_w, q3_h),
                             (q4_x, q4_y, q4_w, q4_h)):
        lines.append(
            f'  <rect x="{qx}" y="{qy}" width="{qw}" height="{qh}" '
            f'fill="#fafafa" stroke="#ccc" stroke-width="0.5"/>'
        )

    # --- View labels ---
    lbl_style = f'font-size="9" font-weight="bold" fill="#444" font-family="{_FONT}"'
    lines.append(f'  <text x="{q1_x + 4}" y="{q1_y + 12}" {lbl_style}>FRONT VIEW</text>')
    lines.append(f'  <text x="{q2_x + 4}" y="{q2_y + 12}" {lbl_style}>TOP VIEW</text>')
    lines.append(f'  <text x="{q3_x + 4}" y="{q3_y + 12}" {lbl_style}>RIGHT SIDE VIEW</text>')
    lines.append(f'  <text x="{q4_x + 4}" y="{q4_y + 12}" {lbl_style}>SECTION A-A</text>')

    # --- Embed CadQuery projections ---
    if svg_front:
        lines.append(_embed_svg(svg_front, (q1_x, q1_y, q1_w, q1_h), label="front"))
    else:
        lines.extend(_fallback_view_text(q1_x, q1_y, q1_w, q1_h, "FRONT VIEW\n(no STEP)"))

    if svg_top:
        lines.append(_embed_svg(svg_top, (q2_x, q2_y, q2_w, q2_h), label="top"))
    else:
        lines.extend(_fallback_view_text(q2_x, q2_y, q2_w, q2_h, "TOP VIEW\n(no STEP)"))

    if svg_right:
        lines.append(_embed_svg(svg_right, (q3_x, q3_y, q3_w, q3_h), label="right"))
    else:
        lines.extend(_fallback_view_text(q3_x, q3_y, q3_w, q3_h, "RIGHT SIDE VIEW\n(no STEP)"))

    # Section A-A: prefer real section cut from STEP, fall back to parametric
    if svg_section:
        lines.append(_embed_svg(svg_section, (q4_x, q4_y, q4_w, q4_h), label="section"))
        # Add hatching overlay indicator and label below the real section
        sec_label_y = q4_y + q4_h - 6
        lines.append(
            f'  <text x="{q4_x + q4_w / 2}" y="{sec_label_y}" text-anchor="middle" '
            f'font-size="9" font-weight="bold" fill="#333" font-family="{_FONT}">'
            f'SECTION A-A  &#x2014;  CUT AT Y MIDPLANE</text>'
        )
    elif bb is not None or params:
        lines.extend(_draw_section_view(params, bb, q4_x, q4_y, q4_w, q4_h, part_type))
    else:
        lines.extend(_fallback_view_text(q4_x, q4_y, q4_w, q4_h, "SECTION A-A\n(no STEP)"))

    # Small isometric inset in bottom-right corner of Q2 (top view)
    if svg_iso:
        iso_inset_w = int(q2_w * 0.35)
        iso_inset_h = int(q2_h * 0.35)
        iso_inset_x = q2_x + q2_w - iso_inset_w - 4
        iso_inset_y = q2_y + q2_h - iso_inset_h - 4
        # Light background for inset
        lines.append(
            f'  <rect x="{iso_inset_x}" y="{iso_inset_y}" '
            f'width="{iso_inset_w}" height="{iso_inset_h}" '
            f'fill="#f4f4f4" stroke="#aaa" stroke-width="0.5"/>'
        )
        lines.append(
            f'  <text x="{iso_inset_x + 3}" y="{iso_inset_y + 9}" '
            f'font-size="7" fill="#777" font-family="{_FONT}">ISO</text>'
        )
        lines.append(_embed_svg(svg_iso, (iso_inset_x, iso_inset_y, iso_inset_w, iso_inset_h), label="iso"))

    # --- Center lines in front view ---
    cqfx = q1_x + q1_w / 2
    cqfy = q1_y + q1_h / 2
    lines.extend(_draw_center_lines(q1_x, q1_y, q1_w, q1_h, cqfx, cqfy))

    # --- Center lines in top view ---
    cqtx = q2_x + q2_w / 2
    cqty = q2_y + q2_h / 2
    lines.extend(_draw_center_lines(q2_x, q2_y, q2_w, q2_h, cqtx, cqty))

    # --- Center lines in right side view ---
    cqrx = q3_x + q3_w / 2
    cqry = q3_y + q3_h / 2
    lines.extend(_draw_center_lines(q3_x, q3_y, q3_w, q3_h, cqrx, cqry))

    # --- Dimension annotations ---
    if bb is not None:
        lines.extend(_dimension_annotations(
            bb,
            (q1_x, q1_y, q1_w, q1_h),
            (q2_x, q2_y, q2_w, q2_h),
            params, part_type,
        ))

    # --- GD&T feature control frames ---
    symbols = _classify_gdnt_symbols(bb, params, part_type)
    lines.extend(_render_gdnt_symbols(
        symbols,
        (q1_x, q1_y, q1_w, q1_h),
        (gdnt_x, gdnt_y, gdnt_w, gdnt_h),
    ))

    # --- Title block ---
    lines.extend(_title_block(part_id, params))

    # --- Fallback info when no STEP ---
    if bb is None:
        lines.append(
            f'  <text x="{_W // 2}" y="{q1_y + q1_h // 2 - 20}" '
            f'text-anchor="middle" font-size="11" fill="#c00" font-family="{_FONT}">'
            f'CadQuery projections unavailable &#x2014; check STEP file</text>'
        )
        fb_items = [f"Part ID: {part_id}"]
        for key in ("od_mm", "bore_mm", "height_mm", "width_mm", "depth_mm",
                    "length_mm", "material"):
            if key in params:
                fb_items.append(f"{key}: {params[key]}")
        for i, txt in enumerate(fb_items):
            lines.append(
                f'  <text x="{_W // 2}" y="{q1_y + q1_h // 2 + i * 16}" '
                f'text-anchor="middle" font-size="11" fill="#333" font-family="{_FONT}">'
                f'{_escape(txt)}</text>'
            )

    lines.append("</svg>")
    return "\n".join(lines)


def _fallback_view_text(rx: float, ry: float, rw: float, rh: float,
                         text: str) -> list[str]:
    """Render placeholder text in a view box."""
    lines: list[str] = []
    cx = rx + rw / 2
    cy = ry + rh / 2
    for i, t in enumerate(text.split("\n")):
        lines.append(
            f'  <text x="{cx}" y="{cy + i * 16 - 8}" text-anchor="middle" '
            f'font-size="10" fill="#aaa" font-family="{_FONT}">{_escape(t)}</text>'
        )
    return lines


# ---------------------------------------------------------------------------
# SVG embedding helpers
# ---------------------------------------------------------------------------

def _embed_svg(raw_svg: str, rect: tuple, *, label: str = "") -> str:
    """Parse a CadQuery SVG and embed it scaled/centered within rect = (x,y,w,h)."""
    rx, ry, rw, rh = rect
    label_h = 26  # reserved for view label at top

    vb = _parse_viewbox(raw_svg)
    if vb is None:
        inner = _strip_svg_wrapper(raw_svg)
        clip_id = f"clip_{label}"
        return (
            f'  <clipPath id="{clip_id}"><rect x="{rx}" y="{ry}" width="{rw}" height="{rh}"/></clipPath>\n'
            f'  <g clip-path="url(#{clip_id})" transform="translate({rx},{ry + label_h})">\n'
            f'    {inner}\n'
            f'  </g>'
        )

    vb_x, vb_y, vb_w, vb_h = vb
    if vb_w == 0 or vb_h == 0:
        return ""

    usable_w = rw
    usable_h = rh - label_h
    scale = min(usable_w / vb_w, usable_h / vb_h) * 0.85  # 85% fill

    tx = rx + (usable_w - vb_w * scale) / 2 - vb_x * scale
    ty = ry + label_h + (usable_h - vb_h * scale) / 2 - vb_y * scale

    inner = _strip_svg_wrapper(raw_svg)
    clip_id = f"clip_{label}"
    return (
        f'  <clipPath id="{clip_id}"><rect x="{rx}" y="{ry}" width="{rw}" height="{rh}"/></clipPath>\n'
        f'  <g clip-path="url(#{clip_id})" '
        f'transform="translate({tx:.2f},{ty:.2f}) scale({scale:.4f})">\n'
        f'    {inner}\n'
        f'  </g>'
    )


def _parse_viewbox(svg: str) -> tuple[float, float, float, float] | None:
    """Extract (x, y, w, h) from SVG viewBox attribute."""
    m = re.search(r'viewBox=["\']([^"\']+)["\']', svg, re.IGNORECASE)
    if not m:
        return None
    parts = m.group(1).split()
    if len(parts) != 4:
        return None
    try:
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return None


def _strip_svg_wrapper(svg: str) -> str:
    """Return the inner content of an SVG string (strip outer <svg> tag)."""
    svg = re.sub(r'<\?xml[^>]*\?>', '', svg).strip()
    svg = re.sub(r'^<svg[^>]*>', '', svg, count=1).strip()
    svg = re.sub(r'</svg>\s*$', '', svg).strip()
    return svg


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _escape(text: str) -> str:
    """Escape special XML characters for safe SVG text content."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
