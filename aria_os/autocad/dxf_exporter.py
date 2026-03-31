"""
dxf_exporter.py — headless DXF generation for civil engineering plans.

Entry point: generate_civil_dxf(description, state, discipline, output_path)

Uses ezdxf (already installed).  No GUI, no AutoCAD needed.
State standards are loaded from standards_library and applied automatically.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import ezdxf
    from ezdxf import units
    _EZDXF_AVAILABLE = True
except ImportError:
    _EZDXF_AVAILABLE = False

from aria_os.autocad.layer_manager import LAYER_DEFS, get_layer
from aria_os.autocad.standards_library import get_standard, get_pipe_design

# ── Output directory ───────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).parent.parent.parent
_OUT_DIR = _REPO_ROOT / "outputs" / "cad" / "dxf"


# ── DXF document setup ─────────────────────────────────────────────────────────

def _create_doc(units_type: str = "imperial") -> "ezdxf.document.Drawing":
    """Create a new DXF R2018 document with all civil layers pre-loaded."""
    # setup=["linetypes"] loads all standard AutoCAD linetypes (CENTER, DASHED, HIDDEN, etc.)
    doc = ezdxf.new("R2018", setup=["linetypes"])
    doc.header["$INSUNITS"] = 2 if units_type == "imperial" else 4  # feet or mm
    doc.header["$LUNITS"] = 2  # decimal
    doc.header["$ANGBASE"] = 0
    doc.header["$ANGDIR"] = 0  # CCW
    doc.header["$LTSCALE"] = 1.0

    # Create all civil engineering layers
    for name, props in LAYER_DEFS.items():
        if name not in doc.layers:
            layer = doc.layers.add(name)
            layer.color = props["color"]
            layer.linetype = props["linetype"]
            lw_val = _lineweight_to_dxf(props["lineweight"])
            layer.lineweight = lw_val
            layer.description = props.get("description", "")

    return doc



def _lineweight_to_dxf(lw_mm: float) -> int:
    """Map mm lineweight to DXF lineweight enum value."""
    table = {
        0.00: 0,   0.05: 5,   0.09: 9,   0.13: 13,
        0.15: 15,  0.18: 18,  0.20: 20,  0.25: 25,
        0.30: 30,  0.35: 35,  0.40: 40,  0.50: 50,
        0.53: 53,  0.60: 60,  0.70: 70,  0.80: 80,
        0.90: 90,  1.00: 100, 1.06: 106, 1.20: 120,
        1.40: 140, 1.58: 158, 2.00: 200, 2.11: 211,
    }
    closest = min(table.keys(), key=lambda k: abs(k - lw_mm))
    return table[closest]


# ── Discipline plan generators ─────────────────────────────────────────────────

def _generate_road_plan(msp: Any, std: dict, description: str) -> None:
    """
    Subdivision arterial street improvement plan.
    1,200 ft centerline STA 0+00 to STA 12+00, two 12' travel lanes,
    6' bike lane, 5' sidewalk each side, mountable curb & gutter.
    T-intersection at STA 6+00 with 30' curb return radii and turn-lane taper.
    """
    lane_w  = std.get("lane_width_ft", 12.0)
    n_lanes = std.get("lanes_min", 2)
    shldr_w = std.get("shoulder_width_ft", 8.0)
    row_w   = std.get("row_width_ft", 66.0)
    dspd    = std.get("design_speed_mph", 35)

    road_len  = 1200.0
    bike_w    = 6.0
    swalk_w   = 5.0
    half_pvmt = (lane_w * n_lanes) / 2.0   # half-width of travel lanes
    eop_y     = half_pvmt                  # edge of pavement offset from CL
    bike_y    = eop_y + bike_w
    sw_inner  = bike_y
    sw_outer  = sw_inner + swalk_w
    row_half  = row_w / 2.0

    # ── Centerline STA 0+00 to 12+00 ─────────────────────────────────────────
    msp.add_line((0, 0), (road_len, 0),
                 dxfattribs={"layer": "ROAD-CENTERLINE", "linetype": "CENTER"})

    # Station tick marks and labels every 100 ft
    for sta in range(0, 1300, 100):
        tick_h = 2.0
        msp.add_line((sta, -tick_h), (sta, tick_h),
                     dxfattribs={"layer": "ANNO-DIM"})
        sta_label = f"{sta // 100}+{sta % 100:02d}"
        msp.add_text(sta_label, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.15,
            "insert": (sta - 1.5, -(row_half + 3)),
        })

    # ── ROW lines ─────────────────────────────────────────────────────────────
    for sign in (+1, -1):
        msp.add_line((0, sign * row_half), (road_len, sign * row_half),
                     dxfattribs={"layer": "ROAD-ROW", "linetype": "DASHED"})

    # ── Edge of pavement lines ────────────────────────────────────────────────
    for sign in (+1, -1):
        msp.add_line((0, sign * eop_y), (road_len, sign * eop_y),
                     dxfattribs={"layer": "ROAD-EDGE"})

    # ── Bike lane stripe (6' from EOP) ────────────────────────────────────────
    for sign in (+1, -1):
        msp.add_line((0, sign * bike_y), (road_len, sign * bike_y),
                     dxfattribs={"layer": "ROAD-MARKING", "linetype": "DASHED"})
        msp.add_text("BIKE LANE", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.15,
            "insert": (50, sign * (eop_y + bike_w / 2) - 0.3),
        })

    # ── Sidewalk lines ────────────────────────────────────────────────────────
    for sign in (+1, -1):
        for y_off in (sw_inner, sw_outer):
            msp.add_line((0, sign * y_off), (road_len, sign * y_off),
                         dxfattribs={"layer": "ROAD-EDGE"})
        msp.add_text("5' SIDEWALK (TYP.)", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (20, sign * (sw_inner + swalk_w / 2) - 0.2),
        })

    # ── Centerline pavement marking ───────────────────────────────────────────
    dash_len, gap_len = 10.0, 30.0
    x = 0.0
    seg = 0
    while x < road_len:
        x_end = min(x + dash_len, road_len)
        msp.add_line((x, 0), (x_end, 0),
                     dxfattribs={"layer": "ROAD-MARKING"})
        x += dash_len + gap_len
        seg += 1

    # ── T-intersection at STA 6+00 ────────────────────────────────────────────
    int_x    = 600.0
    cross_len = 200.0
    taper_len = 100.0

    # Cross-street centerline (south leg only — T-intersection)
    msp.add_line((int_x, 0), (int_x, -(cross_len + row_half)),
                 dxfattribs={"layer": "ROAD-CENTERLINE", "linetype": "CENTER"})

    # Cross-street edge of pavement
    for sign in (+1, -1):
        msp.add_line(
            (int_x + sign * eop_y, -(row_half)),
            (int_x + sign * eop_y, -(cross_len + row_half)),
            dxfattribs={"layer": "ROAD-EDGE"},
        )

    # 30' curb return radii (arc approximated as polyline quadrant)
    r = 30.0
    for corner_cx, corner_cy, a_start, a_end in [
        (int_x - eop_y - r, -eop_y,  0,   90),
        (int_x + eop_y + r, -eop_y, 90,  180),
    ]:
        pts = []
        for deg in range(int(a_start), int(a_end) + 1, 5):
            rad = math.radians(deg)
            pts.append((corner_cx + r * math.cos(rad),
                        corner_cy + r * math.sin(rad)))
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, dxfattribs={"layer": "ROAD-EDGE"})

    msp.add_text("R=30' (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (int_x - eop_y - r - 8, -eop_y - 5),
    })

    # Turn-lane taper 100 LF west of intersection
    taper_start_x = int_x - taper_len
    msp.add_line((taper_start_x, eop_y),
                 (int_x - eop_y, eop_y + lane_w),
                 dxfattribs={"layer": "ROAD-MARKING", "linetype": "DASHED"})
    msp.add_text("TURN LANE TAPER (100 LF)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (taper_start_x, eop_y + lane_w + 1),
    })

    # Stop bar (cross-street approach)
    msp.add_line(
        (int_x - eop_y, -row_half),
        (int_x + eop_y, -row_half),
        dxfattribs={"layer": "ROAD-MARKING"},
    )
    msp.add_text("STOP BAR", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (int_x + eop_y + 2, -row_half),
    })

    # ── Design callout box ────────────────────────────────────────────────────
    bx, by = 900.0, row_half + 10
    msp.add_lwpolyline(
        [(bx, by), (bx + 110, by), (bx + 110, by + 18), (bx, by + 18), (bx, by)],
        dxfattribs={"layer": "ANNO-DIM"},
    )
    msp.add_text(f"DESIGN SPEED: {dspd} MPH", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15, "insert": (bx + 3, by + 12),
    })
    msp.add_text(f"LANE WIDTH: {lane_w:.0f}' (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15, "insert": (bx + 3, by + 7),
    })
    msp.add_text(f"ROW WIDTH: {row_w:.0f}'", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15, "insert": (bx + 3, by + 2),
    })

    # ── General notes block ───────────────────────────────────────────────────
    notes_x, notes_y = 0.0, -(row_half + 20)
    msp.add_text("GENERAL NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (notes_x, notes_y),
    })
    notes = [
        "1. ALL PAVEMENT MARKINGS PER MUTCD.",
        "2. CURB RETURN RADIUS = 30'.",
        "3. ADA RAMPS AT ALL CORNERS (TYP.).",
        f"4. MOUNTABLE CURB & GUTTER EACH SIDE OF ROADWAY.",
        f"5. SEE TYPICAL SECTION FOR ADDITIONAL PAVEMENT DETAILS.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.12,
            "insert": (notes_x, notes_y - 2.5 - i * 2.0),
        })

    # ── Title and north arrow ─────────────────────────────────────────────────
    msp.add_text("ROAD IMPROVEMENT PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.30,
        "insert": (400, -(row_half + 50)),
    })
    msp.add_text("SCALE: 1\"=50'", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (400, -(row_half + 55)),
    })
    # North arrow (simple)
    na_x, na_y = road_len + 20, 30
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (na_x - 1, na_y + 11),
    })


def _generate_drainage_plan(msp: Any, std: dict, description: str) -> None:  # noqa: C901
    """
    Full subdivision storm drainage plan.

    Layout (all in project feet, 1 unit = 1 ft):
      Sheet border 1320 x 1020 at origin.
      Plan view: 1200 x 700 inset.
        - Street grid: Main St (E-W) + Oak Ave (N-S), 1200 x 800 block
        - Existing 2-ft contours (index every 10 ft), elevations 98–116
        - Storm network: trunk main (24" RCP) + 2 branches (18" RCP) + 8 laterals
        - 8 manholes (MH-1..8), 8 curb inlets (CI-1..8), 1 headwall
        - Existing water main and sanitary sewer (dashed)
        - Detention basin with berm polygon, normal/emergency WS elevations
        - Drainage area boundaries (DA-1..DA-3) with acreage labels
        - Spot elevations at key points
      Pipe profile view: 1200 x 180 below plan.
        - HGL, pipe barrel, existing grade, manhole drop lines
      Hydraulics table: pipe-by-pipe summary
      Legend, north arrow, scale bar, title block
    """
    design_storm = std.get("design_storm_minor_year", std.get("design_storm_yr", 10))
    min_cover    = std.get("min_pipe_cover_ft", 2.0)

    # ── Sheet border & title block ────────────────────────────────────────────
    SH_W, SH_H = 1320.0, 1020.0
    msp.add_lwpolyline(
        [(0, 0), (SH_W, 0), (SH_W, SH_H), (0, SH_H), (0, 0)],
        dxfattribs={"layer": "ANNO-DIM"},
    )
    # Inner border
    msp.add_lwpolyline(
        [(15, 15), (SH_W - 15, 15), (SH_W - 15, SH_H - 15),
         (15, SH_H - 15), (15, 15)],
        dxfattribs={"layer": "ANNO-DIM"},
    )
    # Title block (bottom strip)
    tb_y = 15.0
    for x_div in [SH_W * 0.35, SH_W * 0.60, SH_W * 0.78, SH_W - 15]:
        msp.add_line((x_div, tb_y), (x_div, tb_y + 60),
                     dxfattribs={"layer": "ANNO-DIM"})
    msp.add_line((15, tb_y + 60), (SH_W - 15, tb_y + 60),
                 dxfattribs={"layer": "ANNO-DIM"})
    msp.add_line((15, tb_y + 30), (SH_W * 0.35, tb_y + 30),
                 dxfattribs={"layer": "ANNO-DIM"})

    tb_fields = [
        (20, tb_y + 45, 0.30, "STORM DRAINAGE PLAN"),
        (20, tb_y + 18, 0.18, "RESIDENTIAL STREET RECONSTRUCTION"),
        (SH_W * 0.35 + 10, tb_y + 45, 0.20, "PROJECT NO: TX-2024-0831"),
        (SH_W * 0.35 + 10, tb_y + 20, 0.18, "DESIGNED: R. MORALES, PE"),
        (SH_W * 0.60 + 10, tb_y + 45, 0.20, "SHEET: C-3.1"),
        (SH_W * 0.60 + 10, tb_y + 20, 0.18, "OF 7"),
        (SH_W * 0.78 + 10, tb_y + 45, 0.18, "DATE: 03-29-2026"),
        (SH_W * 0.78 + 10, tb_y + 20, 0.18, "SCALE: 1\"=40'"),
    ]
    for tx, ty, th, txt in tb_fields:
        msp.add_text(txt, dxfattribs={"layer": "ANNO-TEXT", "height": th,
                                       "insert": (tx, ty)})

    # Plan area origin (lower-left of plan viewport)
    PX, PY = 30.0, 100.0   # offset inside sheet

    # ─────────────────────────────────────────────────────────────────────────
    # STREET GRID
    # Main St: E-W, y = 400 (in plan coords relative to PX/PY)
    # Oak Ave: N-S, x = 600
    # Block: 1200 ft E-W x 800 ft N-S
    # ─────────────────────────────────────────────────────────────────────────
    BLOCK_W, BLOCK_H = 1200.0, 800.0
    CL_Y   = 400.0   # Main St CL
    CL_X   = 600.0   # Oak Ave CL
    LANE   = 12.0
    ROW_H  = 60.0 / 2   # half-ROW
    EOP    = LANE        # 2-lane road, half-width = 1 lane

    def _pl(ox, oy):
        """Convert plan-local to sheet coords."""
        return (PX + ox, PY + oy)

    # Main Street centerline
    msp.add_line(_pl(0, CL_Y), _pl(BLOCK_W, CL_Y),
                 dxfattribs={"layer": "ROAD-CENTERLINE", "linetype": "CENTER"})
    # Oak Ave centerline
    msp.add_line(_pl(CL_X, 0), _pl(CL_X, BLOCK_H),
                 dxfattribs={"layer": "ROAD-CENTERLINE", "linetype": "CENTER"})

    # ROW lines (4 sides of each street)
    for sign in (+1, -1):
        # Main St ROW
        msp.add_line(_pl(0, CL_Y + sign * ROW_H), _pl(BLOCK_W, CL_Y + sign * ROW_H),
                     dxfattribs={"layer": "ROAD-ROW", "linetype": "DASHED"})
        # Oak Ave ROW
        msp.add_line(_pl(CL_X + sign * ROW_H, 0), _pl(CL_X + sign * ROW_H, BLOCK_H),
                     dxfattribs={"layer": "ROAD-ROW", "linetype": "DASHED"})
        # Edge of pavement
        msp.add_line(_pl(0, CL_Y + sign * EOP), _pl(BLOCK_W, CL_Y + sign * EOP),
                     dxfattribs={"layer": "ROAD-EDGE"})
        msp.add_line(_pl(CL_X + sign * EOP, 0), _pl(CL_X + sign * EOP, BLOCK_H),
                     dxfattribs={"layer": "ROAD-EDGE"})

    # Station labels Main St every 100 ft
    for sta in range(0, 1300, 100):
        msp.add_line(_pl(sta, CL_Y - 1.5), _pl(sta, CL_Y + 1.5),
                     dxfattribs={"layer": "ANNO-DIM"})
        msp.add_text(f"{sta // 100}+{sta % 100:02d}", dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5,
            "insert": _pl(sta - 4, CL_Y - ROW_H - 8),
        })

    # Street name labels
    msp.add_text("MAIN ST.", dxfattribs={
        "layer": "ANNO-TEXT", "height": 5.0, "insert": _pl(60, CL_Y + ROW_H + 4)})
    msp.add_text("OAK AVE.", dxfattribs={
        "layer": "ANNO-TEXT", "height": 5.0, "insert": _pl(CL_X + ROW_H + 4, 80)})

    # Curb & gutter lines (1.5 ft face-of-curb from EOP)
    CRB = EOP + 1.5
    for sign in (+1, -1):
        msp.add_line(_pl(0, CL_Y + sign * CRB), _pl(BLOCK_W, CL_Y + sign * CRB),
                     dxfattribs={"layer": "ROAD-EDGE"})
        msp.add_line(_pl(CL_X + sign * CRB, 0), _pl(CL_X + sign * CRB, BLOCK_H),
                     dxfattribs={"layer": "ROAD-EDGE"})

    # Intersection curb returns (30 ft radius)
    r_ret = 30.0
    corners = [
        # (cx, cy, a_start_deg, a_end_deg) — in plan-local
        (CL_X - CRB - r_ret, CL_Y - CRB,  270, 360),
        (CL_X + CRB + r_ret, CL_Y - CRB,  180, 270),
        (CL_X - CRB - r_ret, CL_Y + CRB,    0,  90),
        (CL_X + CRB + r_ret, CL_Y + CRB,   90, 180),
    ]
    for ccx, ccy, a0, a1 in corners:
        pts = []
        for deg in range(a0, a1 + 1, 5):
            rad = math.radians(deg)
            pts.append(_pl(ccx + r_ret * math.cos(rad),
                           ccy + r_ret * math.sin(rad)))
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, dxfattribs={"layer": "ROAD-EDGE"})

    # ─────────────────────────────────────────────────────────────────────────
    # EXISTING CONTOURS  2-ft interval, index every 10 ft, elevations 98–116
    # Site slopes gently south-to-north (higher north)
    # ─────────────────────────────────────────────────────────────────────────
    for elev in range(98, 118, 2):
        is_idx = (elev % 10 == 0)
        # base y position in plan coords: scale 0-ft = y=0, 20-ft range = block_H
        t = (elev - 98) / 20.0
        base_y = t * BLOCK_H
        n_seg = 12
        pts_c = []
        for seg in range(n_seg + 1):
            x = seg * BLOCK_W / n_seg
            wave = 12.0 * math.sin(math.pi * seg / n_seg * 3 + t * 1.8)
            y = max(5, min(BLOCK_H - 5, base_y + wave))
            pts_c.append(_pl(x, y))
        lyr = "GRAD-EXIST-INDEX" if is_idx else "GRAD-EXIST"
        try:
            lt = "Continuous" if is_idx else "DASHED"
            msp.add_lwpolyline(pts_c, dxfattribs={"layer": lyr, "linetype": lt})
        except Exception:
            msp.add_lwpolyline(pts_c, dxfattribs={"layer": lyr})
        if is_idx:
            lx, ly = pts_c[n_seg // 2]
            msp.add_text(f"{elev}", dxfattribs={
                "layer": "ANNO-TEXT", "height": 2.5, "insert": (lx + 2, ly + 1)})

    # ─────────────────────────────────────────────────────────────────────────
    # DRAINAGE AREA BOUNDARIES  DA-1, DA-2, DA-3
    # ─────────────────────────────────────────────────────────────────────────
    da_areas = [
        # (label, acres, polygon pts in plan-local)
        ("DA-1\n2.4 AC\nC=0.70", 2.4, [
            (50, CL_Y + CRB), (CL_X - CRB, CL_Y + CRB),
            (CL_X - CRB, BLOCK_H - 20), (50, BLOCK_H - 20), (50, CL_Y + CRB)]),
        ("DA-2\n1.8 AC\nC=0.65", 1.8, [
            (CL_X + CRB, CL_Y + CRB), (BLOCK_W - 50, CL_Y + CRB),
            (BLOCK_W - 50, BLOCK_H - 20), (CL_X + CRB, BLOCK_H - 20),
            (CL_X + CRB, CL_Y + CRB)]),
        ("DA-3\n3.1 AC\nC=0.72", 3.1, [
            (50, 20), (BLOCK_W - 50, 20),
            (BLOCK_W - 50, CL_Y - CRB), (50, CL_Y - CRB), (50, 20)]),
    ]
    for da_label, _, da_pts in da_areas:
        sheet_pts = [_pl(x, y) for x, y in da_pts]
        try:
            msp.add_lwpolyline(sheet_pts, dxfattribs={"layer": "GRAD-LIMIT",
                                                        "linetype": "DASHED"})
        except Exception:
            msp.add_lwpolyline(sheet_pts, dxfattribs={"layer": "GRAD-LIMIT"})
        # centroid label
        xs = [p[0] for p in sheet_pts[:-1]]
        ys = [p[1] for p in sheet_pts[:-1]]
        cx_da, cy_da = sum(xs) / len(xs), sum(ys) / len(ys)
        for li, line in enumerate(da_label.split("\n")):
            msp.add_text(line, dxfattribs={
                "layer": "ANNO-TEXT", "height": 4.0,
                "insert": (cx_da - 15, cy_da + 6 - li * 6)})

    # ─────────────────────────────────────────────────────────────────────────
    # EXISTING UTILITIES  (shown dashed, crossing street)
    # ─────────────────────────────────────────────────────────────────────────
    # 8" Water main along Main St south side
    wm_y = CL_Y - EOP - 8
    msp.add_line(_pl(0, wm_y), _pl(BLOCK_W, wm_y),
                 dxfattribs={"layer": "UTIL-WATER", "linetype": "DASHED"})
    msp.add_text("8\" D.I. WATER MAIN (EXIST.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": _pl(30, wm_y - 6)})

    # 10" Sanitary sewer along Main St north side
    ss_y = CL_Y + EOP + 8
    msp.add_line(_pl(0, ss_y), _pl(BLOCK_W, ss_y),
                 dxfattribs={"layer": "UTIL-SANITARY", "linetype": "DASHED"})
    msp.add_text("10\" PVC SAN. SEWER (EXIST.) INV=96.40", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": _pl(30, ss_y + 4)})

    # Gas main crossing Oak Ave
    gm_x = CL_X - 20
    msp.add_line(_pl(gm_x, 0), _pl(gm_x, BLOCK_H),
                 dxfattribs={"layer": "UTIL-GAS", "linetype": "DASHED"})
    msp.add_text("4\" GAS (EXIST.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": _pl(gm_x + 2, 40)})

    # ─────────────────────────────────────────────────────────────────────────
    # STORM DRAIN NETWORK
    # Trunk: MH-1..MH-5 along Main St south curb, 24" RCP
    # North branch: MH-6..MH-7 along north side, 18" RCP
    # West branch: MH-8 off CL_X, 15" RCP
    # 8 curb inlets (CI-1..8) lateral to trunk & branches
    # ─────────────────────────────────────────────────────────────────────────

    # --- Trunk manholes along storm main (south of Main St CL) ---
    trunk_y = CL_Y - EOP - 3    # just south of curb
    trunk_mh = [
        {"id": "MH-1", "x":  80, "y": trunk_y, "rim": 104.82, "inv_in": None,  "inv_out": 100.15, "pipe_out": '24" RCP'},
        {"id": "MH-2", "x": 280, "y": trunk_y, "rim": 104.60, "inv_in": 99.95, "inv_out": 99.85,  "pipe_out": '24" RCP'},
        {"id": "MH-3", "x": 480, "y": trunk_y, "rim": 104.35, "inv_in": 99.65, "inv_out": 99.55,  "pipe_out": '24" RCP'},
        {"id": "MH-4", "x": 680, "y": trunk_y, "rim": 104.10, "inv_in": 99.35, "inv_out": 99.22,  "pipe_out": '24" RCP'},
        {"id": "MH-5", "x": 900, "y": trunk_y, "rim": 103.80, "inv_in": 99.00, "inv_out": 98.90,  "pipe_out": '24" RCP'},
    ]
    # North branch manholes
    north_y = CL_Y + EOP + 3
    branch_mh = [
        {"id": "MH-6", "x": 350, "y": north_y, "rim": 105.10, "inv_in": None,  "inv_out": 100.40, "pipe_out": '18" RCP'},
        {"id": "MH-7", "x": 600, "y": north_y, "rim": 104.90, "inv_in": 100.20,"inv_out": 100.10, "pipe_out": '18" HDPE'},
    ]
    # West branch
    west_mh = [
        {"id": "MH-8", "x": CL_X - 100, "y": CL_Y, "rim": 104.55, "inv_in": None, "inv_out": 100.00, "pipe_out": '15" RCP'},
    ]
    all_mh = trunk_mh + branch_mh + west_mh

    def _draw_mh(mh_dict):
        mx, my = _pl(mh_dict["x"], mh_dict["y"])
        # Double-circle manhole symbol (4 ft / 2 ft radius)
        msp.add_circle((mx, my), 4.0, dxfattribs={"layer": "UTIL-STORM"})
        msp.add_circle((mx, my), 2.0, dxfattribs={"layer": "UTIL-STORM"})
        # Cross hairs
        msp.add_line((mx - 4, my), (mx + 4, my), dxfattribs={"layer": "UTIL-STORM"})
        msp.add_line((mx, my - 4), (mx, my + 4), dxfattribs={"layer": "UTIL-STORM"})
        # Label & elevations (leader line)
        lx, ly = mx + 6, my + 8
        msp.add_line((mx, my), (lx, ly), dxfattribs={"layer": "ANNO-DIM"})
        msp.add_text(mh_dict["id"], dxfattribs={
            "layer": "ANNO-TEXT", "height": 3.0, "insert": (lx, ly + 2)})
        msp.add_text(f"RIM={mh_dict['rim']:.2f}", dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.2, "insert": (lx, ly - 2)})
        inv_str = (f"INV IN={mh_dict['inv_in']:.2f}" if mh_dict["inv_in"]
                   else f"INV OUT={mh_dict['inv_out']:.2f}")
        msp.add_text(inv_str, dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.2, "insert": (lx, ly - 6)})

    for mh in all_mh:
        _draw_mh(mh)

    # Draw trunk pipe segments
    trunk_slopes  = [0.0020, 0.0022, 0.0020, 0.0025]
    for i in range(len(trunk_mh) - 1):
        x0, y0 = _pl(trunk_mh[i]["x"], trunk_mh[i]["y"])
        x1, y1 = _pl(trunk_mh[i + 1]["x"], trunk_mh[i + 1]["y"])
        msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": "UTIL-STORM"})
        mid = ((x0 + x1) / 2, (y0 + y1) / 2)
        msp.add_text(f'24" RCP @ {trunk_slopes[i]*100:.2f}%', dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5, "insert": (mid[0] - 20, mid[1] - 7)})

    # Outfall pipe from MH-5 south to headwall
    msp.add_line(_pl(900, trunk_y), _pl(900, trunk_y - 120),
                 dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text('24" RCP OUTFALL', dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": _pl(904, trunk_y - 60)})
    # Headwall symbol
    hw_x, hw_y = _pl(900, trunk_y - 120)
    msp.add_lwpolyline([
        (hw_x - 12, hw_y), (hw_x + 12, hw_y),
        (hw_x + 12, hw_y - 6), (hw_x - 12, hw_y - 6), (hw_x - 12, hw_y)],
        dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text("CONC. HEADWALL (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (hw_x - 20, hw_y - 12)})
    msp.add_text("RIPRAP APRON: CLASS B, 8'W x 12'L", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (hw_x - 30, hw_y - 17)})

    # North branch pipe (MH-6 to MH-7, with junction tie to MH-3)
    msp.add_line(_pl(branch_mh[0]["x"], branch_mh[0]["y"]),
                 _pl(branch_mh[1]["x"], branch_mh[1]["y"]),
                 dxfattribs={"layer": "UTIL-STORM"})
    bmid = ((branch_mh[0]["x"] + branch_mh[1]["x"]) / 2,
            (branch_mh[0]["y"] + branch_mh[1]["y"]) / 2)
    msp.add_text('18" RCP @ 0.30%', dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": _pl(bmid[0] - 18, bmid[1] + 5)})
    # Junction pipe: MH-7 → MH-4 (tying north branch to trunk)
    msp.add_line(_pl(branch_mh[1]["x"], branch_mh[1]["y"]),
                 _pl(trunk_mh[3]["x"], trunk_mh[3]["y"]),
                 dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text('18" RCP JCT.', dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5,
        "insert": _pl((branch_mh[1]["x"] + trunk_mh[3]["x"]) / 2 + 5,
                      (branch_mh[1]["y"] + trunk_mh[3]["y"]) / 2)})
    # West branch
    msp.add_line(_pl(west_mh[0]["x"], west_mh[0]["y"]),
                 _pl(trunk_mh[2]["x"], trunk_mh[2]["y"]),
                 dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text('15" RCP @ 0.40%', dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5,
        "insert": _pl((west_mh[0]["x"] + trunk_mh[2]["x"]) / 2 - 15,
                      (west_mh[0]["y"] + trunk_mh[2]["y"]) / 2 + 3)})

    # ── Curb inlets CI-1..CI-8 ────────────────────────────────────────────────
    ci_data = [
        # (id, x, side, connects_to_mh_x, rim)
        ("CI-1",  80,  +1, 80,  104.75),
        ("CI-2",  200, +1, 280, 104.65),
        ("CI-3",  380, +1, 280, 104.50),
        ("CI-4",  530, +1, 480, 104.38),
        ("CI-5",  700, +1, 680, 104.18),
        ("CI-6",  820, +1, 900, 104.05),
        ("CI-7",  350, -1, 350, 105.05),  # north branch inlet
        ("CI-8",  580, -1, 600, 104.88),
    ]
    for ci_id, ci_lx, side, mh_x, ci_rim in ci_data:
        ci_y_base = (CL_Y - CRB - 2) if side == +1 else (CL_Y + CRB + 2)
        inlet_y   = ci_y_base - side * 6   # inlet grate sits 6 ft from curb
        # Lateral pipe
        msp.add_line(_pl(ci_lx, inlet_y), _pl(mh_x, trunk_y if side == +1 else north_y),
                     dxfattribs={"layer": "UTIL-STORM"})
        # Inlet box symbol (3x5 ft rectangle)
        ix, iy = _pl(ci_lx, inlet_y)
        msp.add_lwpolyline([
            (ix - 2.5, iy - 1.5), (ix + 2.5, iy - 1.5),
            (ix + 2.5, iy + 1.5), (ix - 2.5, iy + 1.5), (ix - 2.5, iy - 1.5)],
            dxfattribs={"layer": "UTIL-STORM"})
        msp.add_text(ci_id, dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5, "insert": (ix - 3, iy + 3)})
        msp.add_text(f"RIM={ci_rim:.2f}", dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.0, "insert": (ix + 3, iy - 4)})

    # ── Spot elevations ───────────────────────────────────────────────────────
    spots = [
        (120, CL_Y + 30, 105.12),
        (400, CL_Y - 30, 104.40),
        (760, CL_Y + 25, 104.22),
        (CL_X + 50, CL_Y + 200, 106.50),
        (CL_X - 80, CL_Y - 180, 103.95),
        (200, BLOCK_H - 80, 108.30),
        (900, BLOCK_H - 60, 107.75),
    ]
    for sx, sy, selev in spots:
        spx, spy = _pl(sx, sy)
        # X cross symbol
        msp.add_line((spx - 1.5, spy - 1.5), (spx + 1.5, spy + 1.5),
                     dxfattribs={"layer": "ANNO-ELEV"})
        msp.add_line((spx - 1.5, spy + 1.5), (spx + 1.5, spy - 1.5),
                     dxfattribs={"layer": "ANNO-ELEV"})
        msp.add_text(f"{selev:.2f}", dxfattribs={
            "layer": "ANNO-ELEV", "height": 2.2, "insert": (spx + 2, spy + 1)})

    # ── Detention basin ───────────────────────────────────────────────────────
    pond_lx, pond_ly = CL_X + 300, 100
    # Outer berm polygon
    berm_pts = [
        _pl(pond_lx,       pond_ly),
        _pl(pond_lx + 240, pond_ly),
        _pl(pond_lx + 270, pond_ly + 60),
        _pl(pond_lx + 240, pond_ly + 180),
        _pl(pond_lx + 30,  pond_ly + 200),
        _pl(pond_lx - 20,  pond_ly + 120),
        _pl(pond_lx,       pond_ly),
    ]
    msp.add_lwpolyline(berm_pts, dxfattribs={"layer": "UTIL-STORM"})
    # Normal WS contour (inner, offset ~20 ft)
    ws_pts = [
        _pl(pond_lx + 20,  pond_ly + 20),
        _pl(pond_lx + 220, pond_ly + 20),
        _pl(pond_lx + 245, pond_ly + 70),
        _pl(pond_lx + 215, pond_ly + 160),
        _pl(pond_lx + 40,  pond_ly + 175),
        _pl(pond_lx,       pond_ly + 100),
        _pl(pond_lx + 20,  pond_ly + 20),
    ]
    try:
        msp.add_lwpolyline(ws_pts, dxfattribs={"layer": "UTIL-STORM", "linetype": "DASHED"})
    except Exception:
        msp.add_lwpolyline(ws_pts, dxfattribs={"layer": "UTIL-STORM"})

    pond_labels = [
        (pond_lx + 80,  pond_ly + 90,  5.0, "DETENTION BASIN"),
        (pond_lx + 60,  pond_ly + 70,  3.0, "100-YR WSEL = 101.50"),
        (pond_lx + 60,  pond_ly + 58,  3.0, "10-YR  WSEL = 100.85"),
        (pond_lx + 60,  pond_ly + 46,  3.0, "EMERG. SPILLWAY = 102.20"),
        (pond_lx + 60,  pond_ly + 34,  3.0, "BOTTOM EL = 97.50"),
        (pond_lx + 60,  pond_ly + 22,  2.5, "VOL (100-YR) = 1.42 AC-FT"),
    ]
    for plx, ply, pth, ptxt in pond_labels:
        msp.add_text(ptxt, dxfattribs={
            "layer": "ANNO-TEXT", "height": pth, "insert": _pl(plx, ply)})

    # Emergency spillway
    sp_x, sp_y = _pl(pond_lx + 240, pond_ly + 90)
    msp.add_lwpolyline([
        (sp_x, sp_y - 5), (sp_x + 20, sp_y - 5),
        (sp_x + 20, sp_y + 5), (sp_x, sp_y + 5), (sp_x, sp_y - 5)],
        dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text("EMERG.\nSPILLWAY", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (sp_x + 22, sp_y)})

    # ─────────────────────────────────────────────────────────────────────────
    # PIPE PROFILE VIEW  (below plan view at sheet Y = PY - 200)
    # ─────────────────────────────────────────────────────────────────────────
    PROF_PX  = PX
    PROF_PY  = PY - 210     # bottom of profile area on sheet
    PROF_H   = 160.0         # profile box height (ft on sheet)
    PROF_W   = 1200.0
    ELEV_MIN = 97.5
    ELEV_MAX = 106.0
    ELEV_RNG = ELEV_MAX - ELEV_MIN

    def _prof_pt(sta_ft, elev_ft):
        """Map (station, elevation) → sheet XY for profile view."""
        px2 = PROF_PX + sta_ft * PROF_W / 1200.0
        py2 = PROF_PY + (elev_ft - ELEV_MIN) / ELEV_RNG * PROF_H
        return (px2, py2)

    # Profile box
    msp.add_lwpolyline([
        (PROF_PX, PROF_PY), (PROF_PX + PROF_W, PROF_PY),
        (PROF_PX + PROF_W, PROF_PY + PROF_H),
        (PROF_PX, PROF_PY + PROF_H), (PROF_PX, PROF_PY)],
        dxfattribs={"layer": "ANNO-DIM"})
    msp.add_text("STORM DRAIN PROFILE — MAIN STREET TRUNK (STA 0+80 TO 9+00)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 4.0,
        "insert": (PROF_PX + 20, PROF_PY + PROF_H + 5)})

    # Elevation grid lines
    for elev_g in range(int(ELEV_MIN) + 1, int(ELEV_MAX) + 1):
        gy = PROF_PY + (elev_g - ELEV_MIN) / ELEV_RNG * PROF_H
        try:
            msp.add_line((PROF_PX, gy), (PROF_PX + PROF_W, gy),
                         dxfattribs={"layer": "ANNO-DIM", "linetype": "DASHED"})
        except Exception:
            msp.add_line((PROF_PX, gy), (PROF_PX + PROF_W, gy),
                         dxfattribs={"layer": "ANNO-DIM"})
        msp.add_text(f"{elev_g:.0f}", dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5,
            "insert": (PROF_PX - 18, gy - 1.5)})

    # Existing ground line (profile along trunk)
    exist_g = [
        (0,    103.80), (80,   104.10), (280,  104.55), (480,  104.30),
        (600,  104.10), (680,  103.95), (800,  103.75), (900,  103.80),
    ]
    eg_pts = [_prof_pt(s, e) for s, e in exist_g]
    msp.add_lwpolyline(eg_pts, dxfattribs={"layer": "GRAD-EXIST"})
    msp.add_text("EXISTING GROUND", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (eg_pts[2][0], eg_pts[2][1] + 4)})

    # Pipe invert line (24" RCP)
    pipe_inverts = [
        (80,  100.15), (280, 99.85), (480, 99.55), (680, 99.22), (900, 98.90)
    ]
    pi_pts = [_prof_pt(s, i) for s, i in pipe_inverts]
    msp.add_lwpolyline(pi_pts, dxfattribs={"layer": "UTIL-STORM"})
    # Pipe crown (invert + 24/12 = 2 ft)
    crown_pts = [_prof_pt(s, i + 2.0) for s, i in pipe_inverts]
    msp.add_lwpolyline(crown_pts, dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text('24" RCP (PROPOSED)', dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (pi_pts[1][0], pi_pts[1][1] - 8)})

    # HGL line (10-yr storm)
    hgl_data = [
        (80,  101.20), (280, 100.95), (480, 100.65), (680, 100.30), (900, 99.95)
    ]
    hgl_pts = [_prof_pt(s, h) for s, h in hgl_data]
    try:
        msp.add_lwpolyline(hgl_pts, dxfattribs={"layer": "UTIL-STORM", "linetype": "DASHED"})
    except Exception:
        msp.add_lwpolyline(hgl_pts, dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text(f"HGL ({design_storm}-YR)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (hgl_pts[3][0] + 5, hgl_pts[3][1] + 2)})

    # Manhole drop lines in profile
    for mh in trunk_mh:
        sx = mh["x"]
        inv_e = next(i for s, i in pipe_inverts if s == sx)
        eg_e  = next(e for s, e in exist_g if s == sx)
        x_prof, y_inv  = _prof_pt(sx, inv_e)
        _,       y_grnd = _prof_pt(sx, eg_e)
        msp.add_line((x_prof, y_inv), (x_prof, y_grnd),
                     dxfattribs={"layer": "UTIL-STORM"})
        # Manhole symbol in profile (rectangle)
        msp.add_lwpolyline([
            (x_prof - 3, y_inv), (x_prof + 3, y_inv),
            (x_prof + 3, y_grnd), (x_prof - 3, y_grnd), (x_prof - 3, y_inv)],
            dxfattribs={"layer": "UTIL-STORM"})
        msp.add_text(mh["id"], dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.2, "insert": (x_prof - 3, y_grnd + 2)})

    # ─────────────────────────────────────────────────────────────────────────
    # HYDRAULICS TABLE
    # ─────────────────────────────────────────────────────────────────────────
    HT_X = PX + PROF_W + 30
    HT_Y = PROF_PY + PROF_H
    msp.add_text("PIPE SCHEDULE — HYDRAULIC SUMMARY", dxfattribs={
        "layer": "ANNO-TEXT", "height": 4.0, "insert": (HT_X, HT_Y + 10)})
    col_headers = ["PIPE", "FROM", "TO", "SIZE", "LENGTH", "SLOPE", "Q10", "V10"]
    col_widths  = [30, 30, 30, 25, 30, 25, 25, 25]
    col_x = [HT_X + sum(col_widths[:i]) for i in range(len(col_widths))]
    ht_row_h = 7.0
    # Header row
    for cx, hdr in zip(col_x, col_headers):
        msp.add_text(hdr, dxfattribs={"layer": "ANNO-TEXT", "height": 2.5,
                                       "insert": (cx, HT_Y - ht_row_h)})
    # Divider
    msp.add_line((HT_X, HT_Y - ht_row_h - 2),
                 (HT_X + sum(col_widths), HT_Y - ht_row_h - 2),
                 dxfattribs={"layer": "ANNO-DIM"})
    # Data rows
    ht_rows = [
        ("P-1", "MH-1", "MH-2", '24" RCP', "200 LF", "0.20%", "12.4 cfs", "5.0 fps"),
        ("P-2", "MH-2", "MH-3", '24" RCP', "200 LF", "0.22%", "16.8 cfs", "5.4 fps"),
        ("P-3", "MH-3", "MH-4", '24" RCP', "200 LF", "0.20%", "20.1 cfs", "5.2 fps"),
        ("P-4", "MH-4", "MH-5", '24" RCP', "220 LF", "0.25%", "22.7 cfs", "5.8 fps"),
        ("P-5", "MH-5", "HW-1", '24" RCP', "120 LF", "0.28%", "22.7 cfs", "5.8 fps"),
        ("P-6", "MH-6", "MH-7", '18" RCP', "250 LF", "0.30%",  "7.6 cfs", "5.2 fps"),
        ("P-7", "MH-7", "MH-4", '18" RCP', "140 LF", "0.35%",  "7.6 cfs", "5.2 fps"),
        ("P-8", "MH-8", "MH-3", '15" RCP', "130 LF", "0.40%",  "4.2 cfs", "4.6 fps"),
    ]
    for r_idx, row in enumerate(ht_rows):
        for cx, cell in zip(col_x, row):
            msp.add_text(cell, dxfattribs={"layer": "ANNO-TEXT", "height": 2.2,
                                            "insert": (cx, HT_Y - ht_row_h * (r_idx + 2.5))})

    # ─────────────────────────────────────────────────────────────────────────
    # LEGEND
    # ─────────────────────────────────────────────────────────────────────────
    LEG_X = HT_X
    LEG_Y = HT_Y - ht_row_h * 14
    msp.add_text("LEGEND", dxfattribs={
        "layer": "ANNO-TEXT", "height": 4.0, "insert": (LEG_X, LEG_Y)})
    legend_items = [
        ("UTIL-STORM",    "STORM DRAIN PIPE (PROPOSED)"),
        ("UTIL-WATER",    "WATER MAIN (EXISTING)"),
        ("UTIL-SANITARY", "SANITARY SEWER (EXISTING)"),
        ("UTIL-GAS",      "GAS LINE (EXISTING)"),
        ("ROAD-CENTERLINE","ROAD CENTERLINE"),
        ("ROAD-ROW",      "RIGHT-OF-WAY LINE"),
        ("GRAD-EXIST",    "EXISTING CONTOUR (2-FT INTERVAL)"),
        ("GRAD-LIMIT",    "DRAINAGE AREA BOUNDARY"),
        ("ANNO-ELEV",     "SPOT ELEVATION"),
    ]
    for i, (lyr, txt) in enumerate(legend_items):
        lx = LEG_X
        ly = LEG_Y - 9 - i * 7
        msp.add_line((lx, ly), (lx + 20, ly), dxfattribs={"layer": lyr})
        msp.add_text(txt, dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5, "insert": (lx + 22, ly - 1)})

    # ─────────────────────────────────────────────────────────────────────────
    # NORTH ARROW + SCALE BAR
    # ─────────────────────────────────────────────────────────────────────────
    NA_X = SH_W - 120
    NA_Y = SH_H - 120
    # Circle
    msp.add_circle((NA_X, NA_Y), 18, dxfattribs={"layer": "ANNO-DIM"})
    # Arrow shaft
    msp.add_line((NA_X, NA_Y - 18), (NA_X, NA_Y + 18),
                 dxfattribs={"layer": "ANNO-DIM"})
    # Arrow head (triangle pointing north)
    msp.add_lwpolyline([
        (NA_X, NA_Y + 18), (NA_X - 5, NA_Y + 5),
        (NA_X + 5, NA_Y + 5), (NA_X, NA_Y + 18)],
        dxfattribs={"layer": "ANNO-DIM"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": 6.0, "insert": (NA_X - 3, NA_Y + 20)})

    # Graphical scale bar (1" = 40 ft, draw 200 ft = 5 inches → 200 units)
    SB_X = SH_W - 300
    SB_Y = SH_H - 100
    msp.add_line((SB_X, SB_Y), (SB_X + 200, SB_Y),
                 dxfattribs={"layer": "ANNO-DIM"})
    for tick_x, tick_lbl in [(SB_X, "0"), (SB_X + 100, "100"),
                               (SB_X + 200, "200 FT")]:
        msp.add_line((tick_x, SB_Y - 3), (tick_x, SB_Y + 3),
                     dxfattribs={"layer": "ANNO-DIM"})
        msp.add_text(tick_lbl, dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5,
            "insert": (tick_x - 5, SB_Y - 8)})
    msp.add_text("GRAPHIC SCALE: 1\" = 40'", dxfattribs={
        "layer": "ANNO-TEXT", "height": 3.0, "insert": (SB_X, SB_Y + 8)})

    # ── General notes ─────────────────────────────────────────────────────────
    GN_X = PX
    GN_Y = PROF_PY - 10
    msp.add_text("DRAINAGE NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": 4.0, "insert": (GN_X, GN_Y)})
    notes = [
        f"1. DESIGN STORM: {design_storm}-YR (MINOR) / 100-YR (MAJOR) PER TxDOT HYD. MANUAL.",
        f"2. MIN PIPE COVER: {min_cover:.1f} FT OVER CROWN, PER STATE DOT STANDARD.",
        "3. ALL PROPOSED PIPE: 24\" RCP CL. III (ASTM C76) UNLESS NOTED. MIN SLOPE 0.20%.",
        "4. ALL MANHOLES: 4-FT DIA. PRECAST CONC. (ASTM C478) W/ WATERTIGHT JOINTS.",
        "5. CURB INLETS: TYPE C-MODIFIED, 5-FT OPENING. GRATE & FRAME PER TXDOT STD.",
        "6. CONTRACTOR SHALL FIELD-VERIFY ALL EXISTING UTILITY LOCATIONS PRIOR TO WORK.",
        "7. DETENTION BASIN: DESIGN PER TxDOT 100-YR CRITERIA. VOL. = 1.42 AC-FT.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5,
            "insert": (GN_X, GN_Y - 7 - i * 5)})


def _generate_grading_plan(msp: Any, std: dict, description: str) -> None:
    """
    Commercial pad grading plan, 300' x 200' site.
    Existing 2' contours (index every 10'), proposed graded pad at FFE=112.50,
    slope arrows, retaining wall, spot elevations, benchmark, general notes.
    """
    site_w, site_h = 300.0, 200.0
    ffe = 112.50

    # ── Site boundary ─────────────────────────────────────────────────────────
    msp.add_lwpolyline(
        [(0, 0), (site_w, 0), (site_w, site_h), (0, site_h), (0, 0)],
        dxfattribs={"layer": "PROP-BOUNDARY"},
    )
    msp.add_text("SITE BOUNDARY", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (site_w / 2 - 15, -5),
    })

    # ── Existing contours (2' interval, index every 10', elev 100–120) ────────
    for elev in range(100, 122, 2):
        is_index = (elev % 10 == 0)
        # Undulating polyline across site
        t = (elev - 100) / 20.0  # 0..1
        base_y = t * site_h
        # Add some undulation
        pts = []
        n_seg = 8
        for seg in range(n_seg + 1):
            x = seg * site_w / n_seg
            wave = 8.0 * math.sin(math.pi * seg / n_seg * 2.5 + t * math.pi)
            y = base_y + wave
            y = max(0.0, min(site_h, y))
            pts.append((x, y))
        layer = "GRAD-EXIST-INDEX" if is_index else "GRAD-EXIST"
        # Use dashed for existing; index contours get normal existing layer
        lt = "Continuous" if is_index else "DASHED"
        try:
            msp.add_lwpolyline(pts, dxfattribs={"layer": layer, "linetype": lt})
        except Exception:
            msp.add_lwpolyline(pts, dxfattribs={"layer": layer})
        # Label index contours
        if is_index and len(pts) > 2:
            lx, ly = pts[len(pts) // 2]
            msp.add_text(f"{elev:.0f}", dxfattribs={
                "layer": "ANNO-TEXT", "height": 0.10,
                "insert": (lx + 1, ly + 0.5),
            })

    # ── Building pad boundary (80' x 100' centered, approx) ──────────────────
    pad_x0, pad_y0 = 100.0, 60.0
    pad_w, pad_d   = 100.0, 80.0
    msp.add_lwpolyline(
        [(pad_x0, pad_y0), (pad_x0 + pad_w, pad_y0),
         (pad_x0 + pad_w, pad_y0 + pad_d), (pad_x0, pad_y0 + pad_d),
         (pad_x0, pad_y0)],
        dxfattribs={"layer": "GRAD-LIMIT"},
    )
    msp.add_text(f"BUILDING PAD", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (pad_x0 + 20, pad_y0 + pad_d / 2),
    })
    msp.add_text(f"FFE = {ffe:.2f}", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (pad_x0 + 25, pad_y0 + pad_d / 2 - 3),
    })

    # ── Proposed contours (dashed) around graded pad ──────────────────────────
    for elev_off, dist in [(0.5, 10), (1.0, 20), (1.5, 30), (2.0, 45)]:
        prop_elev = ffe - elev_off
        expand = dist
        px0 = max(0, pad_x0 - expand)
        py0 = max(0, pad_y0 - expand)
        px1 = min(site_w, pad_x0 + pad_w + expand)
        py1 = min(site_h, pad_y0 + pad_d + expand)
        pts_prop = [
            (px0, py0), (px1, py0), (px1, py1), (px0, py1), (px0, py0)
        ]
        try:
            msp.add_lwpolyline(pts_prop, dxfattribs={"layer": "GRAD-PROP", "linetype": "DASHED"})
        except Exception:
            msp.add_lwpolyline(pts_prop, dxfattribs={"layer": "GRAD-PROP"})
        msp.add_text(f"{prop_elev:.1f}", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.08,
            "insert": (px1 + 1, (py0 + py1) / 2),
        })

    # ── Slope arrows (2% away from building in all four directions) ───────────
    arrow_data = [
        (pad_x0 + pad_w / 2, pad_y0 + pad_d, pad_x0 + pad_w / 2, pad_y0 + pad_d + 25, "2.0%"),
        (pad_x0 + pad_w / 2, pad_y0, pad_x0 + pad_w / 2, pad_y0 - 25, "2.0%"),
        (pad_x0, pad_y0 + pad_d / 2, pad_x0 - 25, pad_y0 + pad_d / 2, "2.0%"),
        (pad_x0 + pad_w, pad_y0 + pad_d / 2, pad_x0 + pad_w + 25, pad_y0 + pad_d / 2, "2.0%"),
    ]
    for ax0, ay0, ax1, ay1, pct in arrow_data:
        msp.add_line((ax0, ay0), (ax1, ay1),
                     dxfattribs={"layer": "GRAD-SLOPE-ARROW"})
        # Arrowhead approximation
        angle = math.atan2(ay1 - ay0, ax1 - ax0)
        ah_len = 3.0
        for side in (+0.4, -0.4):
            msp.add_line(
                (ax1, ay1),
                (ax1 - ah_len * math.cos(angle + side),
                 ay1 - ah_len * math.sin(angle + side)),
                dxfattribs={"layer": "GRAD-SLOPE-ARROW"},
            )
        msp.add_text(pct, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.12,
            "insert": ((ax0 + ax1) / 2 + 1, (ay0 + ay1) / 2 + 1),
        })

    # ── Retaining wall (north property line, where cut > 4') ─────────────────
    msp.add_line((0, site_h - 5), (site_w, site_h - 5),
                 dxfattribs={"layer": "GRAD-RETAIN-WALL"})
    msp.add_text("RETAINING WALL (CUT > 4')", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (site_w / 2 - 30, site_h - 3),
    })

    # ── Limits of disturbance (heavy dashed) ─────────────────────────────────
    lod_margin = 20.0
    lod_pts = [
        (-lod_margin, -lod_margin), (site_w + lod_margin, -lod_margin),
        (site_w + lod_margin, site_h + lod_margin), (-lod_margin, site_h + lod_margin),
        (-lod_margin, -lod_margin),
    ]
    try:
        msp.add_lwpolyline(lod_pts, dxfattribs={"layer": "GRAD-LIMIT", "linetype": "DASHED"})
    except Exception:
        msp.add_lwpolyline(lod_pts, dxfattribs={"layer": "GRAD-LIMIT"})
    msp.add_text("LIMITS OF DISTURBANCE (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (site_w + lod_margin + 2, site_h / 2),
    })

    # ── Spot elevations ───────────────────────────────────────────────────────
    spots = [
        (pad_x0, pad_y0, ffe),
        (pad_x0 + pad_w, pad_y0, ffe),
        (pad_x0, pad_y0 + pad_d, ffe),
        (pad_x0 + pad_w, pad_y0 + pad_d, ffe),
        (10, 10, 100.5),
        (site_w - 10, 10, 101.2),
        (10, site_h - 10, 118.8),
        (site_w - 10, site_h - 10, 119.4),
        (pad_x0 - 30, pad_y0 - 15, ffe - 2.5),   # parking low point
    ]
    for sx, sy, se in spots:
        msp.add_circle((sx, sy), 1.0,
                        dxfattribs={"layer": "ANNO-DIM"})
        msp.add_text(f"{se:.2f}", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (sx + 1.5, sy - 0.5),
        })

    # ── Benchmark callout ─────────────────────────────────────────────────────
    bm_x, bm_y = site_w + 30, site_h - 20
    msp.add_text("BENCHMARK:", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15, "insert": (bm_x, bm_y),
    })
    msp.add_text("N: 1,234,567.89  E: 456,789.01", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10, "insert": (bm_x, bm_y - 3),
    })
    msp.add_text("ELEV: 105.000 (NAVD 88)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10, "insert": (bm_x, bm_y - 6),
    })

    # ── General notes ─────────────────────────────────────────────────────────
    notes_x, notes_y = 0.0, -30.0
    msp.add_text("GRADING NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (notes_x, notes_y),
    })
    notes = [
        "1. FINISH FLOOR ELEVATION = 112.50 NAVD88.",
        "2. ALL SLOPES TO DRAIN AWAY FROM BUILDING.",
        "3. MAX SLOPE IN PAVED AREAS: 5% (MIN 1%).",
        "4. CONTRACTOR TO VERIFY EXISTING GRADES PRIOR TO CONSTRUCTION.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (notes_x, notes_y - 3 - i * 2.2),
        })

    # ── Title ─────────────────────────────────────────────────────────────────
    msp.add_text("GRADING PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.30,
        "insert": (site_w / 2 - 25, -55),
    })
    msp.add_text("SCALE: 1\"=20'", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (site_w / 2 - 15, -61),
    })
    # North arrow
    na_x, na_y = site_w + 30, 20
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (na_x - 1, na_y + 11),
    })


def _generate_utilities_plan(msp: Any, std: dict, description: str) -> None:
    """
    New development utility extension, 500 LF corridor.
    8\" water main, 8\" gravity sewer, 4\" gas, 2\" electric conduit,
    crossing conflict at STA 2+50, hydrant, meter vault, service laterals.
    """
    run_len = 500.0
    # Y-offsets for each utility (all running east-west)
    water_y   =  15.0
    sewer_y   =   0.0
    gas_y     = -10.0
    elec_y    = -15.0

    # ── Water main (8") ───────────────────────────────────────────────────────
    msp.add_line((0, water_y), (run_len, water_y),
                 dxfattribs={"layer": "UTIL-WATER"})
    msp.add_text("8\" DIP CL350 WATER MAIN", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (5, water_y + 2),
    })

    # Gate valves every 500 LF (one at sta 0 and one at 500)
    for gv_x in (0, run_len):
        msp.add_circle((gv_x, water_y), 2.5,
                        dxfattribs={"layer": "UTIL-WATER"})
        msp.add_text("GV", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (gv_x - 2, water_y + 3),
        })

    # Fire hydrant at STA 2+50
    fh_x = 250.0
    msp.add_circle((fh_x, water_y + 5), 3.0,
                    dxfattribs={"layer": "UTIL-WATER"})
    msp.add_line((fh_x, water_y), (fh_x, water_y + 5),
                 dxfattribs={"layer": "UTIL-WATER"})
    msp.add_text("FH-1", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (fh_x + 3, water_y + 6),
    })
    msp.add_text("FH ASSEMBLY W/ 6\" GATE VALVE, BREAK-AWAY FLANGE", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.08,
        "insert": (fh_x + 5, water_y + 3),
    })

    # Hydrants every 250 LF
    msp.add_circle((0, water_y + 5), 3.0, dxfattribs={"layer": "UTIL-WATER"})
    msp.add_text("FH-2", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (3, water_y + 6),
    })

    # ── Sewer (8" gravity, 4% max slope) ─────────────────────────────────────
    msp.add_line((0, sewer_y), (run_len, sewer_y),
                 dxfattribs={"layer": "UTIL-SEWER"})
    msp.add_text("8\" SDR-35 PVC GRAVITY SEWER @ 0.40% MIN", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (5, sewer_y + 2),
    })

    # Sewer manholes at 0, 300 LF
    smh_data = [
        (0,   "SMH-1", 103.50, 99.00),
        (300, "SMH-2", 103.20, 97.80),
        (run_len, "SMH-3", 103.00, 97.20),
    ]
    for smh_x, smh_lbl, smh_rim, smh_inv in smh_data:
        msp.add_circle((smh_x, sewer_y), 3.0,
                        dxfattribs={"layer": "UTIL-SEWER"})
        msp.add_text(smh_lbl, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.12,
            "insert": (smh_x - 4, sewer_y - 6),
        })
        msp.add_text(f"RIM={smh_rim:.2f} / INV={smh_inv:.2f}", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (smh_x - 12, sewer_y - 9),
        })

    # Service lateral stubs every 50 LF (dashed, 4" dia)
    for lat_x in range(50, int(run_len), 50):
        try:
            msp.add_line((lat_x, sewer_y), (lat_x, sewer_y - 12),
                         dxfattribs={"layer": "UTIL-SEWER", "linetype": "DASHED"})
        except Exception:
            msp.add_line((lat_x, sewer_y), (lat_x, sewer_y - 12),
                         dxfattribs={"layer": "UTIL-SEWER"})
        msp.add_text("4\"", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.08,
            "insert": (lat_x + 0.5, sewer_y - 6),
        })

    # ── Gas main (4") — 10' min horizontal separation ────────────────────────
    msp.add_line((0, gas_y), (run_len, gas_y),
                 dxfattribs={"layer": "UTIL-GAS"})
    msp.add_text("4\" STEEL GAS MAIN", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (5, gas_y - 2),
    })
    msp.add_text(f"HORIZ. SEP.: {abs(sewer_y - gas_y):.0f}' MIN (GAS TO SEWER)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.08,
        "insert": (200, gas_y - 3),
    })

    # ── Electric conduit (2") — 5' min from gas ──────────────────────────────
    msp.add_line((0, elec_y), (run_len, elec_y),
                 dxfattribs={"layer": "UTIL-ELECTRIC"})
    msp.add_text("2\" ELEC. CONDUIT", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (5, elec_y - 2),
    })
    msp.add_text(f"HORIZ. SEP.: {abs(gas_y - elec_y):.0f}' MIN (ELEC. TO GAS)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.08,
        "insert": (200, elec_y - 3),
    })

    # ── Crossing conflict STA 2+50: sewer over water ──────────────────────────
    cross_x = 250.0
    # Crossing marker (X symbol)
    d = 4.0
    msp.add_line((cross_x - d, sewer_y - d), (cross_x + d, water_y + d),
                 dxfattribs={"layer": "UTIL-CROSSING"})
    msp.add_line((cross_x - d, water_y + d), (cross_x + d, sewer_y - d),
                 dxfattribs={"layer": "UTIL-CROSSING"})
    msp.add_text("CROSSING", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10,
        "insert": (cross_x + 5, (sewer_y + water_y) / 2 + 2),
    })
    msp.add_text("WATER MAIN DEFLECTS DOWN 18\" MIN VERTICAL CLEARANCE AT CROSSING", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.08,
        "insert": (cross_x + 5, (sewer_y + water_y) / 2 - 2),
    })

    # ── Meter vault MV-1 near end ─────────────────────────────────────────────
    mv_x = 460.0
    msp.add_lwpolyline(
        [(mv_x - 4, water_y - 4), (mv_x + 4, water_y - 4),
         (mv_x + 4, water_y + 4), (mv_x - 4, water_y + 4),
         (mv_x - 4, water_y - 4)],
        dxfattribs={"layer": "UTIL-WATER"},
    )
    msp.add_text("MV-1", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (mv_x - 2, water_y + 5),
    })

    # ── General notes ─────────────────────────────────────────────────────────
    notes_x, notes_y = 0.0, elec_y - 20.0
    msp.add_text("UTILITY NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (notes_x, notes_y),
    })
    notes = [
        "1. MIN HORIZONTAL SEPARATION WATER/SEWER: 10'-0\" (AWWA C600).",
        "2. 18\" MIN VERTICAL CLEARANCE AT CROSSINGS.",
        "3. ALL WATER MAIN: DIP CLASS 350 OR PVC C-900 DR-18.",
        "4. ALL SEWER: SDR-35 PVC ASTM D3034.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (notes_x, notes_y - 3 - i * 2.2),
        })

    # ── Title ─────────────────────────────────────────────────────────────────
    msp.add_text("UTILITY PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.30,
        "insert": (200, elec_y - 60),
    })
    msp.add_text("SCALE: 1\"=50'", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (200, elec_y - 66),
    })
    # North arrow
    na_x, na_y = run_len + 20, 30
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (na_x - 1, na_y + 11),
    })


def _generate_site_plan(msp: Any, std: dict, description: str) -> None:
    """
    Commercial site plan for a 1-acre lot (200' x 218').
    Property boundary with bearings, setbacks, 80'x120' building, parking field
    (32 standard + 2 ADA), ADA route, loading zone, dumpster enclosure,
    bike parking, site lighting, storm inlet, zoning compliance notes.
    """
    lot_w, lot_d = 200.0, 218.0   # feet

    # ── Property boundary with bearings ───────────────────────────────────────
    corners = [(0, 0), (lot_w, 0), (lot_w, lot_d), (0, lot_d), (0, 0)]
    msp.add_lwpolyline(corners, dxfattribs={"layer": "PROP-BOUNDARY"})
    bearings = [
        (lot_w / 2, -4, "N 89°14'32\" E  200.00'"),
        (lot_w + 2, lot_d / 2, "N 00°45'28\" W  218.00'"),
        (lot_w / 2, lot_d + 2, "S 89°14'32\" W  200.00'"),
        (-45, lot_d / 2, "S 00°45'28\" E  218.00'"),
    ]
    for bx, by, bearing in bearings:
        msp.add_text(bearing, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.12,
            "insert": (bx, by),
        })

    # ── Setback lines ─────────────────────────────────────────────────────────
    setback_data = [
        (25, "FRONT SETBACK (25')"),   # south (front) — y offset from bottom
        (10, "REAR SETBACK (10')"),    # north — y from top, but draw as from top
    ]
    # Front setback
    try:
        msp.add_line((0, 25), (lot_w, 25),
                     dxfattribs={"layer": "PROP-SETBACK", "linetype": "DASHED"})
    except Exception:
        msp.add_line((0, 25), (lot_w, 25),
                     dxfattribs={"layer": "PROP-SETBACK"})
    msp.add_text("25' FRONT SETBACK", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12, "insert": (2, 27),
    })
    # Rear setback
    try:
        msp.add_line((0, lot_d - 10), (lot_w, lot_d - 10),
                     dxfattribs={"layer": "PROP-SETBACK", "linetype": "DASHED"})
    except Exception:
        msp.add_line((0, lot_d - 10), (lot_w, lot_d - 10),
                     dxfattribs={"layer": "PROP-SETBACK"})
    msp.add_text("10' REAR SETBACK", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12, "insert": (2, lot_d - 8),
    })
    # Side setbacks (5' each side)
    for sx, label in [(5, "5' SIDE SETBACK"), (lot_w - 5, "5' SIDE SETBACK")]:
        try:
            msp.add_line((sx, 0), (sx, lot_d),
                         dxfattribs={"layer": "PROP-SETBACK", "linetype": "DASHED"})
        except Exception:
            msp.add_line((sx, 0), (sx, lot_d),
                         dxfattribs={"layer": "PROP-SETBACK"})
    msp.add_text("5' SIDE SETBACK (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10, "insert": (6, lot_d / 2),
    })

    # ── Building footprint (80' x 120' centered on pad) ───────────────────────
    bldg_w, bldg_d = 80.0, 120.0
    bldg_x0 = (lot_w - bldg_w) / 2
    bldg_y0 = (lot_d - bldg_d) / 2 + 15   # slightly south of center
    msp.add_lwpolyline(
        [(bldg_x0, bldg_y0), (bldg_x0 + bldg_w, bldg_y0),
         (bldg_x0 + bldg_w, bldg_y0 + bldg_d), (bldg_x0, bldg_y0 + bldg_d),
         (bldg_x0, bldg_y0)],
        dxfattribs={"layer": "BLDG-FOOTPRINT"},
    )
    msp.add_text("PROPOSED BUILDING", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (bldg_x0 + 10, bldg_y0 + bldg_d / 2 + 3),
    })
    msp.add_text(u"\u00b19,600 SF", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (bldg_x0 + 18, bldg_y0 + bldg_d / 2 - 3),
    })

    # ── Parking field ─────────────────────────────────────────────────────────
    # 32 standard stalls (9'x18') in two rows along south portion
    stall_w, stall_d = 9.0, 18.0
    row1_y = 30.0   # first row, face of stall at y=30, backs at y=48
    row2_y = 30.0 + stall_d + 24.0   # drive aisle 24', then second row
    for row_y in (row1_y, row2_y):
        for col in range(16):
            sx = 5.0 + col * stall_w
            if sx + stall_w > lot_w - 5:
                break
            msp.add_lwpolyline(
                [(sx, row_y), (sx + stall_w, row_y),
                 (sx + stall_w, row_y + stall_d), (sx, row_y + stall_d),
                 (sx, row_y)],
                dxfattribs={"layer": "SITE-PARKING"},
            )

    msp.add_text("32 STANDARD + 2 ADA = 34 PROVIDED (32 REQUIRED)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (5, row2_y + stall_d + 2),
    })

    # ADA stalls (2 van-accessible, 8'+5' access aisle) at west end
    ada_x = 5.0
    ada_y = row1_y
    for i in range(2):
        msp.add_lwpolyline(
            [(ada_x, ada_y), (ada_x + 8, ada_y),
             (ada_x + 8, ada_y + stall_d), (ada_x, ada_y + stall_d),
             (ada_x, ada_y)],
            dxfattribs={"layer": "SITE-ADA"},
        )
        # 5' access aisle
        msp.add_lwpolyline(
            [(ada_x + 8, ada_y), (ada_x + 13, ada_y),
             (ada_x + 13, ada_y + stall_d), (ada_x + 8, ada_y + stall_d),
             (ada_x + 8, ada_y)],
            dxfattribs={"layer": "SITE-ADA"},
        )
        msp.add_text("ADA VAN", dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (ada_x + 0.5, ada_y + stall_d / 2),
        })
        ada_x += 13 + 9   # shift for next ADA stall

    # ── ADA accessible route ──────────────────────────────────────────────────
    # From ADA stalls to building entrance
    ada_route = [
        (5 + 6, row1_y + stall_d),
        (5 + 6, bldg_y0),
        (bldg_x0 + bldg_w / 2, bldg_y0),
    ]
    msp.add_lwpolyline(ada_route, dxfattribs={"layer": "SITE-ADA"})
    msp.add_text("ACCESSIBLE ROUTE", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (5 + 8, row1_y + stall_d + 10),
    })

    # ── Loading zone (12'x35' at rear) ───────────────────────────────────────
    lz_x0 = lot_w - 40.0
    lz_y0 = lot_d - 10 - 35
    msp.add_lwpolyline(
        [(lz_x0, lz_y0), (lz_x0 + 35, lz_y0),
         (lz_x0 + 35, lz_y0 + 12), (lz_x0, lz_y0 + 12),
         (lz_x0, lz_y0)],
        dxfattribs={"layer": "SITE-LOADING"},
    )
    msp.add_text("LOADING ZONE", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.12,
        "insert": (lz_x0 + 3, lz_y0 + 7),
    })
    msp.add_text("NO PARKING", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10,
        "insert": (lz_x0 + 5, lz_y0 + 3),
    })

    # ── Dumpster enclosure (12'x20') ─────────────────────────────────────────
    de_x0, de_y0 = lot_w - 30.0, lot_d - 40.0
    msp.add_lwpolyline(
        [(de_x0, de_y0), (de_x0 + 20, de_y0),
         (de_x0 + 20, de_y0 + 12), (de_x0, de_y0 + 12),
         (de_x0, de_y0)],
        dxfattribs={"layer": "SITE-MISC"},
    )
    msp.add_text("DUMPSTER\nENCL.", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10,
        "insert": (de_x0 + 2, de_y0 + 5),
    })

    # ── Bicycle parking (6-space rack near entrance) ─────────────────────────
    bp_x, bp_y = bldg_x0 + bldg_w + 5, bldg_y0 + 5
    msp.add_lwpolyline(
        [(bp_x, bp_y), (bp_x + 10, bp_y),
         (bp_x + 10, bp_y + 5), (bp_x, bp_y + 5), (bp_x, bp_y)],
        dxfattribs={"layer": "SITE-MISC"},
    )
    msp.add_text("BIKE PARKING\n(6 SPACES)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10,
        "insert": (bp_x + 1, bp_y + 6),
    })

    # ── Site lighting (4 pole lights in parking lot) ──────────────────────────
    light_positions = [
        (lot_w / 4, row1_y + stall_d + 12),
        (3 * lot_w / 4, row1_y + stall_d + 12),
        (lot_w / 4, row2_y + stall_d + 5),
        (3 * lot_w / 4, row2_y + stall_d + 5),
    ]
    for lx, ly in light_positions:
        msp.add_circle((lx, ly), 2.0, dxfattribs={"layer": "SITE-LIGHT"})
        msp.add_line((lx, ly - 2), (lx, ly + 2),
                     dxfattribs={"layer": "SITE-LIGHT"})
        msp.add_line((lx - 2, ly), (lx + 2, ly),
                     dxfattribs={"layer": "SITE-LIGHT"})

    msp.add_text("25' LIGHT POLE W/ 400W LED (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10,
        "insert": (light_positions[0][0] + 3, light_positions[0][1] + 3),
    })

    # ── Storm inlet in parking lot low point ─────────────────────────────────
    si_x, si_y = lot_w / 2, row1_y + stall_d + 5
    msp.add_lwpolyline(
        [(si_x - 2, si_y - 2), (si_x + 2, si_y - 2),
         (si_x + 2, si_y + 2), (si_x - 2, si_y + 2),
         (si_x - 2, si_y - 2)],
        dxfattribs={"layer": "UTIL-STORM"},
    )
    msp.add_text("STORM INLET\n(LOW PT.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.10,
        "insert": (si_x + 3, si_y),
    })

    # ── General notes ─────────────────────────────────────────────────────────
    notes_x, notes_y = 0.0, -20.0
    msp.add_text("SITE NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (notes_x, notes_y),
    })
    notes = [
        "1. ZONING: C-2 COMMERCIAL.",
        "2. LOT AREA: 43,560 SF (1.00 AC).",
        "3. IMPERVIOUS COVER: 28,400 SF (65.2% — MAX ALLOWED 75%).",
        "4. REQUIRED PARKING: 1 SPACE PER 300 SF GFA = 32 SPACES.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": 0.10,
            "insert": (notes_x, notes_y - 3 - i * 2.2),
        })

    # ── Title ─────────────────────────────────────────────────────────────────
    msp.add_text("SITE PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.30,
        "insert": (lot_w / 2 - 15, -50),
    })
    msp.add_text("SCALE: 1\"=30'", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.15,
        "insert": (lot_w / 2 - 12, -56),
    })
    # North arrow
    na_x, na_y = lot_w + 15, lot_d - 20
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": 0.20, "insert": (na_x - 1, na_y + 11),
    })


def _generate_grading_and_drainage(msp: Any, std: dict, description: str) -> None:
    """Combined grading + drainage plan."""
    _generate_grading_plan(msp, std, description)
    _generate_drainage_plan(msp, std, description)


_DISCIPLINE_GENERATORS = {
    "transportation": _generate_road_plan,
    "road":           _generate_road_plan,
    "drainage":       _generate_drainage_plan,
    "grading":        _generate_grading_plan,
    "utilities":      _generate_utilities_plan,
    "utility":        _generate_utilities_plan,
    "site":           _generate_site_plan,
    "grading_drainage": _generate_grading_and_drainage,
}


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_civil_dxf(
    description: str,
    state: str = "national",
    discipline: str | None = None,
    output_path: str | Path | None = None,
    units_type: str = "imperial",
    drawn_by: str = "",
    project: str = "",
    date: str | None = None,
    view_after: bool = False,
) -> Path:
    """
    Generate a headless civil engineering DXF file.

    Parameters
    ----------
    description : str
        Natural-language description of the plan content.
    state : str
        2-letter US state code (e.g. "TX") or "national" for AASHTO defaults.
    discipline : str | None
        Civil discipline: "transportation", "drainage", "grading", "utilities",
        "site", or None to auto-detect from description.
    output_path : str | Path | None
        Where to write the .dxf file.  Auto-generated if None.
    units_type : str
        "imperial" (default) or "metric".
    drawn_by : str
        Designer initials / name for title block.
    project : str
        Project name / number for title block.
    date : str | None
        Date string; defaults to today.
    view_after : bool
        When True, launch the ezdxf viewer after saving the DXF.

    Returns
    -------
    Path
        Absolute path to the written DXF file.
    """
    if not _EZDXF_AVAILABLE:
        raise ImportError(
            "ezdxf is required for DXF generation. "
            "Install it with: pip install ezdxf"
        )

    # Resolve discipline
    if discipline is None:
        discipline = _detect_discipline(description)

    # Load applicable standards
    std = get_standard(state.upper(), discipline)

    # Build output path
    if output_path is None:
        slug = re.sub(r"[^a-z0-9_]+", "_",
                      f"{state}_{discipline}".lower()).strip("_")
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _OUT_DIR / f"{slug}.dxf"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Create DXF document
    doc = _create_doc(units_type)
    msp = doc.modelspace()

    # Add custom properties / metadata
    doc.set_modelspace_vport(height=500, center=(200, 100))

    # Run discipline generator
    gen_fn = _DISCIPLINE_GENERATORS.get(discipline.lower().replace(" ", "_"))
    if gen_fn is None:
        gen_fn = _generate_site_plan  # safe default

    date_str = date or datetime.now().strftime("%Y-%m-%d")
    gen_fn(msp, std, description)

    # Inject standards note as text
    _add_standards_note(msp, std, state)

    # Save
    doc.saveas(str(output_path))

    # Write sidecar JSON (standards applied + metadata)
    meta_path = output_path.with_suffix(".json")
    meta = {
        "schema_version": "1.0",
        "description": description,
        "state": state.upper(),
        "discipline": discipline,
        "standards_applied": _summarise_standards(std),
        "units": units_type,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "output_dxf": str(output_path),
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    # Launch viewer if requested
    if view_after:
        from aria_os.preview_ui import show_dxf_preview
        show_dxf_preview(output_path, title=output_path.stem,
                         discipline=discipline or "", state=state)

    return output_path


def _detect_discipline(description: str) -> str:
    """Infer civil discipline from description keywords."""
    desc = description.lower()
    keywords = {
        "transportation": ["road", "highway", "street", "intersection",
                           "pavement", "lane", "traffic", "asphalt"],
        "drainage":       ["storm", "sewer", "culvert", "inlet", "drainage",
                           "runoff", "detention", "retention", "swale"],
        "grading":        ["grade", "grading", "contour", "elevation",
                           "earthwork", "cut", "fill", "retaining"],
        "utilities":      ["water", "gas", "electric", "fiber", "utility",
                           "main", "service", "ductbank"],
        "site":           ["site", "parking", "building", "landscape",
                           "ada", "ramp", "sidewalk"],
    }
    scores = {disc: sum(kw in desc for kw in kws)
              for disc, kws in keywords.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "site"


def _add_standards_note(msp: Any, std: dict, state: str) -> None:
    """Add standards compliance note to drawing."""
    lines = [f"STANDARDS: {state.upper()} DOT / AASHTO 7th Ed."]
    # std is the flat merged discipline dict
    lane_w = std.get("lane_width_ft")
    if lane_w:
        spd = std.get("design_speed_mph", 45)
        lines.append(f"LANE WIDTH: {lane_w}' | SPEED: {spd} MPH")
    min_cover = std.get("min_pipe_cover_ft")
    if min_cover:
        storm_yr = std.get("design_storm_minor_year", std.get("design_storm_yr", 10))
        lines.append(f"STORM: {storm_yr}-yr | PIPE COVER: {min_cover}'min")
    frost = std.get("frost_depth_in")
    if frost:
        lines.append(f"FROST DEPTH: {frost}\"")

    for i, line in enumerate(lines):
        msp.add_text(
            line,
            dxfattribs={
                "layer": "ANNO-TEXT",
                "height": 0.12,
                "insert": (-20, -5 - i * 1.5),
            }
        )


def _summarise_standards(std: dict) -> dict:
    """Return a flat summary of key standards values for the JSON sidecar."""
    # std is already a flat discipline dict — return directly
    return {k: v for k, v in std.items() if not isinstance(v, dict)}


# ── Batch generation ───────────────────────────────────────────────────────────

def generate_all_disciplines(state: str = "national",
                             output_dir: str | Path | None = None) -> list[Path]:
    """Generate DXF files for all disciplines for a given state."""
    disciplines = ["transportation", "drainage", "grading", "utilities", "site"]
    out_dir = Path(output_dir) if output_dir else _OUT_DIR / state.lower()
    results = []
    for disc in disciplines:
        path = generate_civil_dxf(
            description=f"{disc} plan",
            state=state,
            discipline=disc,
            output_path=out_dir / f"{state.lower()}_{disc}.dxf",
        )
        results.append(path)
        print(f"[autocad] wrote {disc}: {path}")
    return results
