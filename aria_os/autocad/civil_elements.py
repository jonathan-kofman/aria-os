"""
civil_elements.py — ezdxf entity builders for civil engineering disciplines.

Each builder returns a list of (entity_type, kwargs) tuples OR writes directly
to an ezdxf ModelSpace.  The caller (dxf_exporter) places them on the correct layer.

All geometry in project coordinates (feet or meters per project units).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import ezdxf
    from ezdxf.layouts import Modelspace


# ── helpers ───────────────────────────────────────────────────────────────────

def _angle_pts(cx: float, cy: float, r: float, n: int = 32) -> list[tuple[float, float]]:
    """Return n points on a circle of radius r centered at (cx, cy)."""
    return [
        (cx + r * math.cos(2 * math.pi * i / n),
         cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]


# ── Road elements ─────────────────────────────────────────────────────────────

def add_road_centerline(msp: "Modelspace", start: tuple, end: tuple,
                        layer: str = "ROAD-CL") -> None:
    """Add road centerline between two points."""
    msp.add_line(start, end, dxfattribs={"layer": layer, "linetype": "CENTER"})


def add_road_lanes(msp: "Modelspace", cl_start: tuple, cl_end: tuple,
                   lane_width_ft: float = 12.0, n_lanes: int = 2,
                   shoulder_width_ft: float = 8.0,
                   layer_eop: str = "ROAD-EOP",
                   layer_shldr: str = "ROAD-SHLDR") -> None:
    """
    Add edge-of-pavement and shoulder lines parallel to a centerline.
    Works in 2D (XY plane).
    """
    dx = cl_end[0] - cl_start[0]
    dy = cl_end[1] - cl_start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    # perpendicular unit vector
    px, py = -dy / length, dx / length

    half_road = lane_width_ft * n_lanes / 2.0
    half_total = half_road + shoulder_width_ft

    for side in (+1, -1):
        eop_offset = side * half_road
        shldr_offset = side * half_total

        def _shift(pt: tuple, offset: float) -> tuple:
            return (pt[0] + px * offset, pt[1] + py * offset)

        msp.add_line(
            _shift(cl_start, eop_offset), _shift(cl_end, eop_offset),
            dxfattribs={"layer": layer_eop}
        )
        msp.add_line(
            _shift(cl_start, shldr_offset), _shift(cl_end, shldr_offset),
            dxfattribs={"layer": layer_shldr, "linetype": "DASHED"}
        )


def add_intersection(msp: "Modelspace", center: tuple,
                     radius_ft: float = 40.0,
                     layer: str = "ROAD-CL") -> None:
    """Add a simple circular intersection representation."""
    msp.add_circle(center, radius_ft, dxfattribs={"layer": layer})


def add_turning_radius(msp: "Modelspace", corner: tuple,
                       radius_ft: float = 30.0,
                       start_angle: float = 0.0,
                       end_angle: float = 90.0,
                       layer: str = "ROAD-EOP") -> None:
    """Add curb return arc at intersection corner."""
    msp.add_arc(corner, radius_ft, start_angle, end_angle,
                dxfattribs={"layer": layer})


def add_station_label(msp: "Modelspace", point: tuple, station: float,
                      layer: str = "ROAD-STATION",
                      height: float = 0.1) -> None:
    """Add a station label (e.g., 10+00) at a point."""
    sta_int = int(station)
    sta_str = f"{sta_int // 100}+{sta_int % 100:02d}"
    msp.add_text(sta_str, dxfattribs={"layer": layer, "height": height,
                                       "insert": point})


def add_pavement_marking(msp: "Modelspace", start: tuple, end: tuple,
                          marking_type: str = "centerline",
                          layer: str = "ROAD-STRIPING") -> None:
    """Add pavement markings (centerline, edge line, stop bar, crosswalk)."""
    if marking_type in ("centerline", "edgeline"):
        msp.add_line(start, end, dxfattribs={"layer": layer, "linetype": "DASHED"})
    elif marking_type == "stopbar":
        msp.add_line(start, end, dxfattribs={"layer": layer, "lineweight": 50})
    elif marking_type == "crosswalk":
        # two parallel lines
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        length = math.hypot(dx, dy)
        if length == 0:
            return
        px, py = -dy / length * 2.0, dx / length * 2.0
        msp.add_line(start, end, dxfattribs={"layer": layer})
        msp.add_line((start[0] + px, start[1] + py),
                     (end[0] + px, end[1] + py),
                     dxfattribs={"layer": layer})


# ── Drainage elements ─────────────────────────────────────────────────────────

def add_storm_pipe(msp: "Modelspace", start: tuple, end: tuple,
                   diameter_in: float = 18.0,
                   layer: str = "DRAIN-PIPE-STORM") -> None:
    """Add storm sewer pipe run. Labels diameter."""
    msp.add_line(start, end, dxfattribs={"layer": layer})
    mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    msp.add_text(f"Ø{diameter_in}\" RCP",
                 dxfattribs={"layer": "DRAIN-LABEL", "height": 0.08,
                              "insert": mid})


def add_inlet(msp: "Modelspace", center: tuple,
              width_ft: float = 2.0, length_ft: float = 4.0,
              label: str = "",
              layer: str = "DRAIN-INLET") -> None:
    """Add catch basin / curb inlet symbol."""
    hw, hl = width_ft / 2, length_ft / 2
    pts = [
        (center[0] - hw, center[1] - hl),
        (center[0] + hw, center[1] - hl),
        (center[0] + hw, center[1] + hl),
        (center[0] - hw, center[1] + hl),
    ]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
    # X symbol inside
    msp.add_line(pts[0], pts[2], dxfattribs={"layer": layer})
    msp.add_line(pts[1], pts[3], dxfattribs={"layer": layer})
    if label:
        msp.add_text(label, dxfattribs={"layer": "DRAIN-LABEL",
                                         "height": 0.08, "insert": center})


def add_manhole(msp: "Modelspace", center: tuple,
                diameter_ft: float = 4.0, label: str = "MH",
                layer: str = "DRAIN-MH") -> None:
    """Add manhole circle + label."""
    msp.add_circle(center, diameter_ft / 2, dxfattribs={"layer": layer})
    offset = (center[0] + diameter_ft * 0.6, center[1])
    msp.add_text(label, dxfattribs={"layer": "DRAIN-LABEL",
                                     "height": 0.08, "insert": offset})


def add_culvert(msp: "Modelspace", start: tuple, end: tuple,
                diameter_in: float = 24.0,
                layer: str = "DRAIN-CULVERT") -> None:
    """Add culvert run with parallel offset lines."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    half = (diameter_in / 12.0) / 2.0
    px, py = -dy / length * half, dx / length * half
    for sign in (+1, -1):
        msp.add_line(
            (start[0] + px * sign, start[1] + py * sign),
            (end[0] + px * sign, end[1] + py * sign),
            dxfattribs={"layer": layer}
        )


def add_detention_pond(msp: "Modelspace", center: tuple,
                       width_ft: float = 200.0, depth_ft: float = 150.0,
                       layer: str = "DRAIN-POND") -> None:
    """Add detention/retention pond outline (ellipse approximation)."""
    pts = _angle_pts(center[0], center[1], 1.0, 32)
    scaled = [(center[0] + (p[0] - center[0]) * width_ft / 2,
               center[1] + (p[1] - center[1]) * depth_ft / 2) for p in pts]
    msp.add_lwpolyline(scaled, close=True, dxfattribs={"layer": layer})


def add_swale(msp: "Modelspace", points: list[tuple],
              layer: str = "DRAIN-SWALE") -> None:
    """Add drainage swale polyline."""
    msp.add_lwpolyline(points, dxfattribs={"layer": layer})


# ── Grading elements ──────────────────────────────────────────────────────────

def add_contour(msp: "Modelspace", points: list[tuple],
                elevation: float = 0.0,
                is_index: bool = False,
                is_proposed: bool = False) -> None:
    """Add existing or proposed contour line with optional elevation label."""
    if is_proposed:
        layer = "GRADE-PROP-INDEX" if is_index else "GRADE-PROP-CONTOUR"
    else:
        layer = "GRADE-EXIST-INDEX" if is_index else "GRADE-EXIST-CONTOUR"
    msp.add_lwpolyline(points, dxfattribs={"layer": layer})
    if is_index and len(points) > 0:
        mid_idx = len(points) // 2
        msp.add_text(f"{elevation:.1f}",
                     dxfattribs={"layer": layer, "height": 0.07,
                                  "insert": points[mid_idx]})


def add_slope_arrow(msp: "Modelspace", start: tuple, end: tuple,
                    slope_pct: float,
                    layer: str = "GRADE-SLOPE") -> None:
    """Add slope arrow with percent label."""
    msp.add_line(start, end, dxfattribs={"layer": layer})
    # arrowhead
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    ux, uy = dx / length, dy / length
    arrow_len = min(0.3, length * 0.15)
    msp.add_line(end,
                 (end[0] - ux * arrow_len + uy * arrow_len * 0.3,
                  end[1] - uy * arrow_len - ux * arrow_len * 0.3),
                 dxfattribs={"layer": layer})
    msp.add_line(end,
                 (end[0] - ux * arrow_len - uy * arrow_len * 0.3,
                  end[1] - uy * arrow_len + ux * arrow_len * 0.3),
                 dxfattribs={"layer": layer})
    mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    msp.add_text(f"{slope_pct:.1f}%",
                 dxfattribs={"layer": layer, "height": 0.07, "insert": mid})


def add_retaining_wall(msp: "Modelspace", points: list[tuple],
                       layer: str = "GRADE-RETWALL") -> None:
    """Add retaining wall polyline with tick marks on high side."""
    msp.add_lwpolyline(points, dxfattribs={"layer": layer})
    # tick marks every ~20ft to indicate retained side
    for i in range(len(points) - 1):
        x1, y1 = points[i]
        x2, y2 = points[i + 1]
        seg_len = math.hypot(x2 - x1, y2 - y1)
        if seg_len == 0:
            continue
        n_ticks = max(1, int(seg_len / 20.0))
        for t in range(n_ticks):
            frac = (t + 0.5) / n_ticks
            tx = x1 + (x2 - x1) * frac
            ty = y1 + (y2 - y1) * frac
            # perpendicular tick
            px = -(y2 - y1) / seg_len * 1.5
            py = (x2 - x1) / seg_len * 1.5
            msp.add_line((tx, ty), (tx + px, ty + py),
                         dxfattribs={"layer": layer})


def add_spot_elevation(msp: "Modelspace", point: tuple, elevation: float,
                       layer: str = "GRADE-SPOT-ELEV") -> None:
    """Add spot elevation marker (X with elevation label)."""
    d = 0.15
    msp.add_line((point[0] - d, point[1] - d), (point[0] + d, point[1] + d),
                 dxfattribs={"layer": layer})
    msp.add_line((point[0] + d, point[1] - d), (point[0] - d, point[1] + d),
                 dxfattribs={"layer": layer})
    msp.add_text(f"{elevation:.2f}",
                 dxfattribs={"layer": layer, "height": 0.07,
                              "insert": (point[0] + 0.2, point[1] + 0.2)})


# ── Utility elements ──────────────────────────────────────────────────────────

def add_utility_line(msp: "Modelspace", start: tuple, end: tuple,
                     utility_type: str = "water",
                     diameter_in: float = 8.0) -> None:
    """Add utility pipe/conduit run."""
    layer_map = {
        "water":    ("UTIL-WATER-MAIN",    "CONTINUOUS"),
        "sewer":    ("UTIL-SEWER-MAIN",    "CONTINUOUS"),
        "gas":      ("UTIL-GAS-MAIN",      "DASHED2"),
        "electric": ("UTIL-ELEC-DUCTBANK", "DASHED"),
        "fiber":    ("UTIL-FIBER",         "DASHED"),
        "storm":    ("UTIL-STORM-MAIN",    "CONTINUOUS"),
    }
    layer, lt = layer_map.get(utility_type.lower(),
                              ("UTIL-LABEL", "CONTINUOUS"))
    msp.add_line(start, end, dxfattribs={"layer": layer, "linetype": lt})
    mid = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    abbrev = {"water": "W", "sewer": "SS", "gas": "G",
              "electric": "E", "fiber": "FO", "storm": "SD"}
    ab = abbrev.get(utility_type.lower(), "UTIL")
    msp.add_text(f"{ab}-{diameter_in}\"",
                 dxfattribs={"layer": "UTIL-LABEL", "height": 0.07,
                              "insert": mid})


def add_utility_crossing(msp: "Modelspace", point: tuple,
                          layer: str = "UTIL-XING") -> None:
    """Add utility crossing marker circle."""
    msp.add_circle(point, 0.5, dxfattribs={"layer": layer})
    msp.add_text("XING", dxfattribs={"layer": layer, "height": 0.07,
                                      "insert": (point[0] + 0.6, point[1])})


# ── Survey / boundary elements ────────────────────────────────────────────────

def add_property_boundary(msp: "Modelspace", points: list[tuple],
                           close: bool = True,
                           layer: str = "SURV-BOUNDARY") -> None:
    """Add property boundary polyline."""
    msp.add_lwpolyline(points, close=close, dxfattribs={"layer": layer})


def add_row(msp: "Modelspace", cl_start: tuple, cl_end: tuple,
            row_width_ft: float = 60.0,
            layer: str = "SURV-ROW") -> None:
    """Add right-of-way lines parallel to a centerline."""
    dx = cl_end[0] - cl_start[0]
    dy = cl_end[1] - cl_start[1]
    length = math.hypot(dx, dy)
    if length == 0:
        return
    px, py = -dy / length, dx / length
    half = row_width_ft / 2.0
    for sign in (+1, -1):
        msp.add_line(
            (cl_start[0] + px * half * sign, cl_start[1] + py * half * sign),
            (cl_end[0] + px * half * sign, cl_end[1] + py * half * sign),
            dxfattribs={"layer": layer}
        )


def add_easement(msp: "Modelspace", points: list[tuple],
                 label: str = "DRAINAGE ESMT",
                 layer: str = "SURV-EASEMENT") -> None:
    """Add easement boundary with label."""
    msp.add_lwpolyline(points, close=True,
                       dxfattribs={"layer": layer, "linetype": "DASHED"})
    if len(points) >= 2:
        mid_idx = len(points) // 2
        msp.add_text(label, dxfattribs={"layer": layer, "height": 0.07,
                                         "insert": points[mid_idx]})


def add_survey_monument(msp: "Modelspace", point: tuple,
                         label: str = "MON",
                         layer: str = "SURV-MONUMENT") -> None:
    """Add survey monument symbol (filled square) + label."""
    d = 0.2
    pts = [
        (point[0] - d, point[1] - d),
        (point[0] + d, point[1] - d),
        (point[0] + d, point[1] + d),
        (point[0] - d, point[1] + d),
    ]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
    msp.add_text(label, dxfattribs={"layer": layer, "height": 0.07,
                                     "insert": (point[0] + 0.3, point[1])})


# ── Site elements ──────────────────────────────────────────────────────────────

def add_building_footprint(msp: "Modelspace",
                            corner: tuple, width_ft: float, depth_ft: float,
                            layer: str = "SITE-BLDG") -> None:
    """Add rectangular building footprint."""
    x, y = corner
    pts = [
        (x, y), (x + width_ft, y),
        (x + width_ft, y + depth_ft), (x, y + depth_ft)
    ]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})


def add_parking_stalls(msp: "Modelspace",
                        origin: tuple, stall_width_ft: float = 9.0,
                        stall_depth_ft: float = 18.0, n_stalls: int = 10,
                        angle_deg: float = 90.0,
                        layer: str = "SITE-PARKING") -> None:
    """Add parking stalls from an origin point."""
    angle_rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
    # drive aisle direction: along X; stall direction at angle
    for i in range(n_stalls + 1):
        # stall stripe
        sx = origin[0] + i * stall_width_ft
        sy = origin[1]
        ex = sx + stall_depth_ft * cos_a
        ey = sy + stall_depth_ft * sin_a
        msp.add_line((sx, sy), (ex, ey), dxfattribs={"layer": layer})
    # front and back lines
    msp.add_line(origin,
                 (origin[0] + n_stalls * stall_width_ft, origin[1]),
                 dxfattribs={"layer": layer})
    back_x = origin[0] + stall_depth_ft * cos_a
    back_y = origin[1] + stall_depth_ft * sin_a
    msp.add_line((back_x, back_y),
                 (back_x + n_stalls * stall_width_ft, back_y),
                 dxfattribs={"layer": layer})


def add_ada_ramp(msp: "Modelspace", center: tuple,
                 width_ft: float = 5.0, depth_ft: float = 5.0,
                 layer: str = "SITE-ADA-RAMP") -> None:
    """Add ADA curb ramp symbol (triangle truncated)."""
    x, y = center
    pts = [
        (x - width_ft / 2, y),
        (x + width_ft / 2, y),
        (x, y + depth_ft),
    ]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
    # detectable warning surface hatch lines
    for j in range(3):
        frac = (j + 1) / 4.0
        msp.add_line(
            (x - width_ft / 2 * (1 - frac), y + depth_ft * frac),
            (x + width_ft / 2 * (1 - frac), y + depth_ft * frac),
            dxfattribs={"layer": layer}
        )


# ── Structural elements ────────────────────────────────────────────────────────

def add_column_grid(msp: "Modelspace",
                    origin: tuple, spacing_x: float, spacing_y: float,
                    n_cols: int, n_rows: int,
                    layer: str = "STRUC-COLUMN") -> None:
    """Add structural column grid with circle markers."""
    col_r = min(spacing_x, spacing_y) * 0.03
    for row in range(n_rows):
        for col in range(n_cols):
            cx = origin[0] + col * spacing_x
            cy = origin[1] + row * spacing_y
            msp.add_circle((cx, cy), col_r, dxfattribs={"layer": layer})
            # crosshair
            msp.add_line((cx - col_r * 1.5, cy), (cx + col_r * 1.5, cy),
                         dxfattribs={"layer": layer})
            msp.add_line((cx, cy - col_r * 1.5), (cx, cy + col_r * 1.5),
                         dxfattribs={"layer": layer})


def add_footing(msp: "Modelspace", center: tuple,
                width_ft: float = 4.0, depth_ft: float = 4.0,
                layer: str = "STRUC-FOOTING") -> None:
    """Add foundation footing rectangle (hidden line)."""
    hw, hd = width_ft / 2, depth_ft / 2
    x, y = center
    pts = [(x - hw, y - hd), (x + hw, y - hd),
           (x + hw, y + hd), (x - hw, y + hd)]
    msp.add_lwpolyline(pts, close=True,
                       dxfattribs={"layer": layer, "linetype": "HIDDEN"})


# ── Annotation elements ───────────────────────────────────────────────────────

def add_dimension_linear(msp: "Modelspace",
                          p1: tuple, p2: tuple,
                          dimline_offset: float = 3.0,
                          layer: str = "ANNO-DIM") -> None:
    """Add a simple linear dimension."""
    # Vertical offset for horizontal dims
    mid_x = (p1[0] + p2[0]) / 2
    mid_y = max(p1[1], p2[1]) + dimline_offset
    msp.add_linear_dim(
        base=(mid_x, mid_y),
        p1=p1, p2=p2,
        dimstyle="Standard",
        dxfattribs={"layer": layer}
    ).render()


def add_north_arrow(msp: "Modelspace", center: tuple,
                    size: float = 5.0,
                    layer: str = "ANNO-NORTH") -> None:
    """Add north arrow symbol."""
    x, y = center
    # shaft
    msp.add_line((x, y), (x, y + size), dxfattribs={"layer": layer})
    # arrowhead
    msp.add_line((x, y + size), (x - size * 0.15, y + size * 0.7),
                 dxfattribs={"layer": layer})
    msp.add_line((x, y + size), (x + size * 0.15, y + size * 0.7),
                 dxfattribs={"layer": layer})
    # N label
    msp.add_text("N", dxfattribs={"layer": layer,
                                   "height": size * 0.3,
                                   "insert": (x - size * 0.15, y + size * 1.1)})


def add_title_block(msp: "Modelspace",
                    origin: tuple = (0, 0),
                    width: float = 34.0, height: float = 22.0,
                    title: str = "CIVIL SITE PLAN",
                    project: str = "", drawn_by: str = "",
                    date: str = "", scale: str = "1\"=20'",
                    sheet: str = "1 of 1",
                    layer: str = "ANNO-TITLEBLOCK") -> None:
    """Add a standard title block border."""
    x, y = origin
    # outer border
    pts = [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": layer})
    # title block box bottom-right
    tb_w, tb_h = 8.0, 3.0
    tb_x = x + width - tb_w
    tb_y = y
    tb_pts = [(tb_x, tb_y), (x + width, tb_y),
              (x + width, tb_y + tb_h), (tb_x, tb_y + tb_h)]
    msp.add_lwpolyline(tb_pts, close=True, dxfattribs={"layer": layer})
    # text fields
    fields = [
        (title,    (tb_x + 0.2, tb_y + 2.3), 0.25),
        (project,  (tb_x + 0.2, tb_y + 1.8), 0.12),
        (f"SCALE: {scale}", (tb_x + 0.2, tb_y + 1.2), 0.10),
        (f"DATE: {date}",   (tb_x + 0.2, tb_y + 0.8), 0.10),
        (f"BY: {drawn_by}", (tb_x + 0.2, tb_y + 0.4), 0.10),
        (f"SHEET: {sheet}", (tb_x + 4.0, tb_y + 0.4), 0.10),
    ]
    for text, insert, h in fields:
        if text.strip():
            msp.add_text(text, dxfattribs={"layer": layer,
                                            "height": h, "insert": insert})
