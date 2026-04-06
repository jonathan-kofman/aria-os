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

# ── Text scaling for civil plot scales ────────────────────────────────────────
# Civil drawings are drawn in model-space feet but plotted at e.g. 1"=40'.
# Text specified in inches at plot scale must be scaled up to model units.
PLOT_SCALE = 40.0           # 1" = 40' (typical civil scale)
TEXT_SCALE = PLOT_SCALE * 0.08   # 0.08" text at plot scale = 3.2 model units
TEXT_SCALE_SM = PLOT_SCALE * 0.06   # smaller annotations
TEXT_SCALE_LG = PLOT_SCALE * 0.12   # titles / headings
TEXT_SCALE_XL = PLOT_SCALE * 0.18   # plan titles


# ── LLM-driven plan interpretation ───────────────────────────────────────────

def _interpret_plan_description(description: str, discipline: str, std: dict) -> dict:
    """Use LLM to extract plan parameters from natural language description.

    Falls back to an empty dict (callers use .get() with defaults) when
    no LLM is reachable or parsing fails.
    """
    try:
        from aria_os.agents.base_agent import _call_ollama
        from aria_os.agents.ollama_config import AGENT_MODELS

        std_snippet = {k: v for k, v in std.items() if isinstance(v, (int, float, str))}

        prompt = f"""Extract civil engineering plan parameters from this description:
"{description}"

Discipline: {discipline}
State standards: {json.dumps(std_snippet, indent=2)}

Return a JSON object with these fields (use realistic values inferred from the
description, omit any you cannot determine):
{{
    "road_length_ft": 800,
    "n_lanes": 2,
    "lane_width_ft": 12,
    "design_speed_mph": 35,
    "n_intersections": 1,
    "intersection_type": "T",
    "has_sidewalks": true,
    "has_bike_lanes": false,
    "n_manholes": 4,
    "pipe_sizes_in": [18, 24],
    "detention_required": true,
    "site_area_acres": 50,
    "site_width_ft": 300,
    "site_depth_ft": 200,
    "contour_interval_ft": 2,
    "n_buildings": 1,
    "building_width_ft": 80,
    "building_depth_ft": 120,
    "n_parking_stalls": 32,
    "lot_width_ft": 200,
    "lot_depth_ft": 218,
    "corridor_length_ft": 500,
    "ffe_elevation": 112.5,
    "has_retaining_wall": false,
    "has_loading_zone": true,
    "has_bike_parking": true
}}

Return ONLY the JSON object, no explanation."""

        response = _call_ollama(
            prompt,
            "You are a civil engineer extracting plan parameters. Return only valid JSON.",
            AGENT_MODELS.get("spec", "qwen2.5-coder:7b"),
        )
        if response:
            match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if match:
                return json.loads(match.group(0))
    except Exception:
        pass
    return {}  # fallback to defaults


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
    Road improvement plan.  Parameters are extracted from *description*
    via LLM when available; otherwise sensible defaults are used.
    """
    params = _interpret_plan_description(description, "transportation", std)

    lane_w  = float(params.get("lane_width_ft", std.get("lane_width_ft", 12.0)))
    n_lanes = int(params.get("n_lanes", std.get("lanes_min", 2)))
    shldr_w = std.get("shoulder_width_ft", 8.0)
    row_w   = std.get("row_width_ft", 66.0)
    dspd    = float(params.get("design_speed_mph", std.get("design_speed_mph", 35)))

    road_len  = float(params.get("road_length_ft", 800.0))
    has_sidewalks = params.get("has_sidewalks", True)
    has_bike_lanes = params.get("has_bike_lanes", False)
    n_intersections = int(params.get("n_intersections", 1))
    bike_w    = 6.0 if has_bike_lanes else 0.0
    swalk_w   = 5.0 if has_sidewalks else 0.0
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
    for sta in range(0, int(road_len) + 100, 100):
        tick_h = 2.0
        msp.add_line((sta, -tick_h), (sta, tick_h),
                     dxfattribs={"layer": "ANNO-DIM"})
        sta_label = f"{sta // 100}+{sta % 100:02d}"
        msp.add_text(sta_label, dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
    if has_bike_lanes:
        for sign in (+1, -1):
            msp.add_line((0, sign * bike_y), (road_len, sign * bike_y),
                         dxfattribs={"layer": "ROAD-MARKING", "linetype": "DASHED"})
            msp.add_text("BIKE LANE", dxfattribs={
                "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
                "insert": (50, sign * (eop_y + bike_w / 2) - 0.3),
            })

    # ── Sidewalk lines ────────────────────────────────────────────────────────
    if has_sidewalks:
        for sign in (+1, -1):
            for y_off in (sw_inner, sw_outer):
                msp.add_line((0, sign * y_off), (road_len, sign * y_off),
                             dxfattribs={"layer": "ROAD-EDGE"})
            msp.add_text(f"{swalk_w:.0f}' SIDEWALK (TYP.)", dxfattribs={
                "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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

    # ── Intersection(s) ──────────────────────────────────────────────────────
    int_x    = road_len / 2.0   # first intersection at midpoint
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
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (int_x - eop_y - r - 8, -eop_y - 5),
    })

    # Turn-lane taper 100 LF west of intersection
    taper_start_x = int_x - taper_len
    msp.add_line((taper_start_x, eop_y),
                 (int_x - eop_y, eop_y + lane_w),
                 dxfattribs={"layer": "ROAD-MARKING", "linetype": "DASHED"})
    msp.add_text(f"TURN LANE TAPER ({taper_len:.0f} LF)", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (taper_start_x, eop_y + lane_w + 1),
    })

    # Stop bar (cross-street approach)
    msp.add_line(
        (int_x - eop_y, -row_half),
        (int_x + eop_y, -row_half),
        dxfattribs={"layer": "ROAD-MARKING"},
    )
    msp.add_text("STOP BAR", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (int_x + eop_y + 2, -row_half),
    })

    # ── Design callout box ────────────────────────────────────────────────────
    bx, by = road_len * 0.75, row_half + 10
    msp.add_lwpolyline(
        [(bx, by), (bx + 110, by), (bx + 110, by + 18), (bx, by + 18), (bx, by)],
        dxfattribs={"layer": "ANNO-DIM"},
    )
    msp.add_text(f"DESIGN SPEED: {dspd:.0f} MPH", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE, "insert": (bx + 3, by + 12),
    })
    msp.add_text(f"LANE WIDTH: {lane_w:.0f}' (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE, "insert": (bx + 3, by + 7),
    })
    msp.add_text(f"ROW WIDTH: {row_w:.0f}'", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE, "insert": (bx + 3, by + 2),
    })

    # ── General notes block ───────────────────────────────────────────────────
    notes_x, notes_y = 0.0, -(row_half + 20)
    msp.add_text("GENERAL NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (notes_x, notes_y),
    })
    notes = [
        "1. ALL PAVEMENT MARKINGS PER MUTCD.",
        "2. CURB RETURN RADIUS = 30'.",
        "3. ADA RAMPS AT ALL CORNERS (TYP.).",
        "4. MOUNTABLE CURB & GUTTER EACH SIDE OF ROADWAY.",
        "5. SEE TYPICAL SECTION FOR ADDITIONAL PAVEMENT DETAILS.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (notes_x, notes_y - 2.5 - i * 2.0),
        })

    # ── Title and north arrow ─────────────────────────────────────────────────
    msp.add_text("ROAD IMPROVEMENT PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_XL,
        "insert": (road_len / 3, -(row_half + 50)),
    })
    msp.add_text("SCALE: 1\"=50'", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (road_len / 3, -(row_half + 55)),
    })
    # North arrow (simple)
    na_x, na_y = road_len + 20, 30
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (na_x - 1, na_y + 11),
    })


def _generate_drainage_plan(msp: Any, std: dict, description: str) -> None:  # noqa: C901
    """
    Full subdivision storm drainage plan.

    Parameters are extracted from *description* via LLM when available;
    otherwise sensible defaults are used.  The plan layout adapts the
    number of manholes, pipe sizes, and detention requirements based on
    those parameters while preserving the full complexity of the output
    (profile view, hydraulics table, legend, etc.).
    """
    params = _interpret_plan_description(description, "drainage", std)

    design_storm = std.get("design_storm_minor_year", std.get("design_storm_yr", 10))
    min_cover    = std.get("min_pipe_cover_ft", 2.0)
    n_manholes   = int(params.get("n_manholes", 8))
    pipe_sizes   = params.get("pipe_sizes_in", [18, 24])
    has_detention = params.get("detention_required", True)
    site_acres   = float(params.get("site_area_acres", 6.0))

    # Scale block dimensions based on site area (default ~6 ac = 1200x800)
    area_scale   = max(0.5, min(3.0, site_acres / 6.0))
    BLOCK_W_BASE = 1200.0
    BLOCK_H_BASE = 800.0

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
    BLOCK_W = BLOCK_W_BASE * math.sqrt(area_scale)
    BLOCK_H = BLOCK_H_BASE * math.sqrt(area_scale)
    CL_Y   = BLOCK_H / 2.0   # Main St CL
    CL_X   = BLOCK_W / 2.0   # Oak Ave CL
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
    for sta in range(0, int(BLOCK_W) + 100, 100):
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
    # Dynamically generate based on n_manholes parameter
    trunk_y = CL_Y - EOP - 3    # just south of curb
    north_y = CL_Y + EOP + 3

    # Allocate manholes: ~60% trunk, ~25% north branch, ~15% west branch
    n_trunk = max(2, int(n_manholes * 0.6))
    n_branch = max(1, int(n_manholes * 0.25))
    n_west = max(0, n_manholes - n_trunk - n_branch)

    # Determine trunk pipe size (largest available)
    trunk_pipe_in = max(pipe_sizes) if pipe_sizes else 24
    branch_pipe_in = min(pipe_sizes) if len(pipe_sizes) > 1 else max(12, trunk_pipe_in - 6)

    trunk_mh = []
    trunk_spacing = (BLOCK_W - 160) / max(1, n_trunk - 1) if n_trunk > 1 else BLOCK_W / 2
    for i in range(n_trunk):
        x_pos = 80 + i * trunk_spacing
        rim = 104.82 - i * 0.25
        inv_out = 100.15 - i * 0.30
        inv_in = inv_out + 0.10 if i > 0 else None
        trunk_mh.append({
            "id": f"MH-{i+1}", "x": x_pos, "y": trunk_y,
            "rim": round(rim, 2), "inv_in": round(inv_in, 2) if inv_in else None,
            "inv_out": round(inv_out, 2),
            "pipe_out": f'{trunk_pipe_in}" RCP',
        })

    branch_mh = []
    branch_spacing = BLOCK_W * 0.4 / max(1, n_branch) if n_branch > 0 else 0
    for i in range(n_branch):
        x_pos = BLOCK_W * 0.25 + i * branch_spacing
        rim = 105.10 - i * 0.20
        inv_out = 100.40 - i * 0.30
        inv_in = inv_out + 0.20 if i > 0 else None
        mat = "RCP" if i % 2 == 0 else "HDPE"
        branch_mh.append({
            "id": f"MH-{n_trunk + i + 1}", "x": x_pos, "y": north_y,
            "rim": round(rim, 2), "inv_in": round(inv_in, 2) if inv_in else None,
            "inv_out": round(inv_out, 2),
            "pipe_out": f'{branch_pipe_in}" {mat}',
        })

    west_mh = []
    if n_west > 0:
        west_pipe_in = max(12, branch_pipe_in - 3)
        for i in range(n_west):
            west_mh.append({
                "id": f"MH-{n_trunk + n_branch + i + 1}",
                "x": CL_X - 100 - i * 80, "y": CL_Y,
                "rim": 104.55 - i * 0.15,
                "inv_in": None,
                "inv_out": round(100.00 - i * 0.20, 2),
                "pipe_out": f'{west_pipe_in}" RCP',
            })
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
    for i in range(len(trunk_mh) - 1):
        x0, y0 = _pl(trunk_mh[i]["x"], trunk_mh[i]["y"])
        x1, y1 = _pl(trunk_mh[i + 1]["x"], trunk_mh[i + 1]["y"])
        msp.add_line((x0, y0), (x1, y1), dxfattribs={"layer": "UTIL-STORM"})
        mid = ((x0 + x1) / 2, (y0 + y1) / 2)
        # Compute slope from invert drop / distance
        inv_drop = abs(trunk_mh[i]["inv_out"] - trunk_mh[i + 1]["inv_out"])
        seg_len = abs(trunk_mh[i + 1]["x"] - trunk_mh[i]["x"])
        slope_pct = (inv_drop / seg_len * 100) if seg_len > 0 else 0.20
        msp.add_text(f'{trunk_pipe_in}" RCP @ {slope_pct:.2f}%', dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5, "insert": (mid[0] - 20, mid[1] - 7)})

    # Outfall pipe from last trunk MH south to headwall
    last_trunk_x = trunk_mh[-1]["x"]
    msp.add_line(_pl(last_trunk_x, trunk_y), _pl(last_trunk_x, trunk_y - 120),
                 dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text(f'{trunk_pipe_in}" RCP OUTFALL', dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": _pl(last_trunk_x + 4, trunk_y - 60)})
    # Headwall symbol
    hw_x, hw_y = _pl(last_trunk_x, trunk_y - 120)
    msp.add_lwpolyline([
        (hw_x - 12, hw_y), (hw_x + 12, hw_y),
        (hw_x + 12, hw_y - 6), (hw_x - 12, hw_y - 6), (hw_x - 12, hw_y)],
        dxfattribs={"layer": "UTIL-STORM"})
    msp.add_text("CONC. HEADWALL (TYP.)", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (hw_x - 20, hw_y - 12)})
    msp.add_text("RIPRAP APRON: CLASS B, 8'W x 12'L", dxfattribs={
        "layer": "ANNO-TEXT", "height": 2.5, "insert": (hw_x - 30, hw_y - 17)})

    # North branch pipe segments
    for i in range(len(branch_mh) - 1):
        msp.add_line(_pl(branch_mh[i]["x"], branch_mh[i]["y"]),
                     _pl(branch_mh[i + 1]["x"], branch_mh[i + 1]["y"]),
                     dxfattribs={"layer": "UTIL-STORM"})
        bmid = ((branch_mh[i]["x"] + branch_mh[i + 1]["x"]) / 2,
                (branch_mh[i]["y"] + branch_mh[i + 1]["y"]) / 2)
        msp.add_text(f'{branch_pipe_in}" RCP @ 0.30%', dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5, "insert": _pl(bmid[0] - 18, bmid[1] + 5)})

    # Junction pipe: last branch MH → nearest trunk MH (tying north branch to trunk)
    if branch_mh and len(trunk_mh) > 1:
        jct_trunk_idx = min(len(trunk_mh) - 1, max(1, len(trunk_mh) // 2))
        msp.add_line(_pl(branch_mh[-1]["x"], branch_mh[-1]["y"]),
                     _pl(trunk_mh[jct_trunk_idx]["x"], trunk_mh[jct_trunk_idx]["y"]),
                     dxfattribs={"layer": "UTIL-STORM"})
        msp.add_text(f'{branch_pipe_in}" RCP JCT.', dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5,
            "insert": _pl((branch_mh[-1]["x"] + trunk_mh[jct_trunk_idx]["x"]) / 2 + 5,
                          (branch_mh[-1]["y"] + trunk_mh[jct_trunk_idx]["y"]) / 2)})

    # West branch
    if west_mh and len(trunk_mh) > 2:
        west_trunk_idx = min(2, len(trunk_mh) - 1)
        west_pipe_in_lbl = west_mh[0]["pipe_out"].split('"')[0]
        msp.add_line(_pl(west_mh[0]["x"], west_mh[0]["y"]),
                     _pl(trunk_mh[west_trunk_idx]["x"], trunk_mh[west_trunk_idx]["y"]),
                     dxfattribs={"layer": "UTIL-STORM"})
        msp.add_text(f'{west_pipe_in_lbl}" RCP @ 0.40%', dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5,
            "insert": _pl((west_mh[0]["x"] + trunk_mh[west_trunk_idx]["x"]) / 2 - 15,
                          (west_mh[0]["y"] + trunk_mh[west_trunk_idx]["y"]) / 2 + 3)})

    # ── Curb inlets — generate one per trunk + branch manhole ───────────────
    ci_data = []
    for i, mh in enumerate(trunk_mh):
        ci_data.append((f"CI-{i+1}", mh["x"], +1, mh["x"], mh["rim"] - 0.07))
    for i, mh in enumerate(branch_mh):
        ci_data.append((f"CI-{len(trunk_mh)+i+1}", mh["x"], -1, mh["x"],
                         mh["rim"] - 0.05))
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

    # ── Detention basin (only if detention_required) ────────────────────────
    pond_lx, pond_ly = CL_X + BLOCK_W * 0.25, 100
    if has_detention:
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
    PROF_W   = BLOCK_W       # match plan width
    # Derive elevation range from actual manhole data
    all_inverts = [mh["inv_out"] for mh in trunk_mh]
    all_rims = [mh["rim"] for mh in trunk_mh]
    ELEV_MIN = min(all_inverts) - 1.0 if all_inverts else 97.5
    ELEV_MAX = max(all_rims) + 1.5 if all_rims else 106.0
    ELEV_RNG = max(ELEV_MAX - ELEV_MIN, 1.0)

    def _prof_pt(sta_ft, elev_ft):
        """Map (station, elevation) → sheet XY for profile view."""
        px2 = PROF_PX + sta_ft * PROF_W / BLOCK_W
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

    # Existing ground line (profile along trunk) — derived from manhole rims
    exist_g = [(0, trunk_mh[0]["rim"] - 0.70 if trunk_mh else 103.80)]
    for mh in trunk_mh:
        exist_g.append((mh["x"], mh["rim"] - 0.70))
    eg_pts = [_prof_pt(s, e) for s, e in exist_g]
    msp.add_lwpolyline(eg_pts, dxfattribs={"layer": "GRAD-EXIST"})
    if len(eg_pts) > 2:
        msp.add_text("EXISTING GROUND", dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5, "insert": (eg_pts[2][0], eg_pts[2][1] + 4)})

    # Pipe invert line — derived from trunk manholes
    pipe_inverts = [(mh["x"], mh["inv_out"]) for mh in trunk_mh]
    pi_pts = [_prof_pt(s, i) for s, i in pipe_inverts]
    if pi_pts:
        msp.add_lwpolyline(pi_pts, dxfattribs={"layer": "UTIL-STORM"})
    # Pipe crown (invert + pipe_dia/12 ft)
    pipe_crown_ft = trunk_pipe_in / 12.0
    crown_pts = [_prof_pt(s, i + pipe_crown_ft) for s, i in pipe_inverts]
    if crown_pts:
        msp.add_lwpolyline(crown_pts, dxfattribs={"layer": "UTIL-STORM"})
    if len(pi_pts) > 1:
        msp.add_text(f'{trunk_pipe_in}" RCP (PROPOSED)', dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5, "insert": (pi_pts[1][0], pi_pts[1][1] - 8)})

    # HGL line (design-storm) — estimated ~1 ft above crown
    hgl_data = [(s, i + pipe_crown_ft + 1.0) for s, i in pipe_inverts]
    hgl_pts = [_prof_pt(s, h) for s, h in hgl_data]
    try:
        msp.add_lwpolyline(hgl_pts, dxfattribs={"layer": "UTIL-STORM", "linetype": "DASHED"})
    except Exception:
        msp.add_lwpolyline(hgl_pts, dxfattribs={"layer": "UTIL-STORM"})
    hgl_label_idx = min(3, len(hgl_pts) - 1) if hgl_pts else 0
    if hgl_pts:
        msp.add_text(f"HGL ({design_storm}-YR)", dxfattribs={
            "layer": "ANNO-TEXT", "height": 2.5,
            "insert": (hgl_pts[hgl_label_idx][0] + 5, hgl_pts[hgl_label_idx][1] + 2)})

    # Manhole drop lines in profile
    pipe_inv_map = dict(pipe_inverts)
    exist_g_map = dict(exist_g)
    for mh in trunk_mh:
        sx = mh["x"]
        inv_e = pipe_inv_map.get(sx, mh["inv_out"])
        eg_e  = exist_g_map.get(sx, mh["rim"] - 0.70)
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
    # Data rows — generated dynamically from manhole network
    ht_rows = []
    pipe_num = 1
    # Trunk segments
    for i in range(len(trunk_mh) - 1):
        seg_len = abs(trunk_mh[i + 1]["x"] - trunk_mh[i]["x"])
        inv_drop = abs(trunk_mh[i]["inv_out"] - trunk_mh[i + 1]["inv_out"])
        slope_pct = (inv_drop / seg_len * 100) if seg_len > 0 else 0.20
        # Manning's Q estimate for circular pipe (simplified)
        pipe_r_ft = trunk_pipe_in / 24.0  # hydraulic radius ~ D/4
        n_manning = 0.013  # RCP
        q_cfs = (1.49 / n_manning) * (math.pi * (trunk_pipe_in / 24.0)**2) * pipe_r_ft**(2/3) * (slope_pct / 100)**0.5
        v_fps = q_cfs / (math.pi * (trunk_pipe_in / 24.0)**2) if trunk_pipe_in > 0 else 0
        ht_rows.append((
            f"P-{pipe_num}", trunk_mh[i]["id"], trunk_mh[i + 1]["id"],
            f'{trunk_pipe_in}" RCP', f"{seg_len:.0f} LF", f"{slope_pct:.2f}%",
            f"{q_cfs:.1f} cfs", f"{v_fps:.1f} fps",
        ))
        pipe_num += 1
    # Outfall from last trunk MH
    if trunk_mh:
        ht_rows.append((
            f"P-{pipe_num}", trunk_mh[-1]["id"], "HW-1",
            f'{trunk_pipe_in}" RCP', "120 LF", "0.28%",
            f"{q_cfs:.1f} cfs" if trunk_mh else "0 cfs", f"{v_fps:.1f} fps" if trunk_mh else "0 fps",
        ))
        pipe_num += 1
    # Branch segments
    for i in range(len(branch_mh) - 1):
        seg_len = abs(branch_mh[i + 1]["x"] - branch_mh[i]["x"])
        ht_rows.append((
            f"P-{pipe_num}", branch_mh[i]["id"], branch_mh[i + 1]["id"],
            f'{branch_pipe_in}" RCP', f"{seg_len:.0f} LF", "0.30%",
            "7.6 cfs", "5.2 fps",
        ))
        pipe_num += 1
    # West branch
    for wmh in west_mh:
        ht_rows.append((
            f"P-{pipe_num}", wmh["id"],
            trunk_mh[min(2, len(trunk_mh) - 1)]["id"] if trunk_mh else "MH-?",
            wmh["pipe_out"], "130 LF", "0.40%", "4.2 cfs", "4.6 fps",
        ))
        pipe_num += 1
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
        f"3. ALL PROPOSED PIPE: {trunk_pipe_in}\" RCP CL. III (ASTM C76) UNLESS NOTED. MIN SLOPE 0.20%.",
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
    Grading plan.  Parameters are extracted from *description* via LLM
    when available; otherwise sensible defaults are used.
    """
    params = _interpret_plan_description(description, "grading", std)

    site_w = float(params.get("site_width_ft", 300.0))
    site_h = float(params.get("site_depth_ft", 200.0))
    ffe = float(params.get("ffe_elevation", 112.50))
    contour_interval = int(params.get("contour_interval_ft", 2))
    has_retaining_wall = params.get("has_retaining_wall", True)

    # ── Site boundary ─────────────────────────────────────────────────────────
    msp.add_lwpolyline(
        [(0, 0), (site_w, 0), (site_w, site_h), (0, site_h), (0, 0)],
        dxfattribs={"layer": "PROP-BOUNDARY"},
    )
    msp.add_text("SITE BOUNDARY", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (site_w / 2 - 15, -5),
    })

    # ── Existing contours (dynamic interval, index every 10', elev range) ────
    elev_base = int(ffe - 12)
    elev_top = int(ffe + 10)
    for elev in range(elev_base, elev_top, contour_interval):
        is_index = (elev % 10 == 0)
        # Undulating polyline across site
        elev_range = max(1, elev_top - elev_base)
        t = (elev - elev_base) / float(elev_range)  # 0..1
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
                "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
    msp.add_text("BUILDING PAD", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (pad_x0 + 20, pad_y0 + pad_d / 2),
    })
    msp.add_text(f"FFE = {ffe:.2f}", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
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
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": ((ax0 + ax1) / 2 + 1, (ay0 + ay1) / 2 + 1),
        })

    # ── Retaining wall (north property line, where cut > 4') ─────────────────
    if has_retaining_wall:
        msp.add_line((0, site_h - 5), (site_w, site_h - 5),
                     dxfattribs={"layer": "GRAD-RETAIN-WALL"})
        msp.add_text("RETAINING WALL (CUT > 4')", dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (sx + 1.5, sy - 0.5),
        })

    # ── Benchmark callout ─────────────────────────────────────────────────────
    bm_x, bm_y = site_w + 30, site_h - 20
    msp.add_text("BENCHMARK:", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE, "insert": (bm_x, bm_y),
    })
    msp.add_text("N: 1,234,567.89  E: 456,789.01", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM, "insert": (bm_x, bm_y - 3),
    })
    msp.add_text("ELEV: 105.000 (NAVD 88)", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM, "insert": (bm_x, bm_y - 6),
    })

    # ── General notes ─────────────────────────────────────────────────────────
    notes_x, notes_y = 0.0, -30.0
    msp.add_text("GRADING NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (notes_x, notes_y),
    })
    notes = [
        f"1. FINISH FLOOR ELEVATION = {ffe:.2f} NAVD88.",
        "2. ALL SLOPES TO DRAIN AWAY FROM BUILDING.",
        "3. MAX SLOPE IN PAVED AREAS: 5% (MIN 1%).",
        "4. CONTRACTOR TO VERIFY EXISTING GRADES PRIOR TO CONSTRUCTION.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (notes_x, notes_y - 3 - i * 2.2),
        })

    # ── Title ─────────────────────────────────────────────────────────────────
    msp.add_text("GRADING PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_XL,
        "insert": (site_w / 2 - 25, -55),
    })
    msp.add_text("SCALE: 1\"=20'", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (site_w / 2 - 15, -61),
    })
    # North arrow
    na_x, na_y = site_w + 30, 20
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (na_x - 1, na_y + 11),
    })


def _generate_utilities_plan(msp: Any, std: dict, description: str) -> None:
    """
    Utility plan.  Parameters are extracted from *description* via LLM
    when available; otherwise sensible defaults are used.
    """
    params = _interpret_plan_description(description, "utilities", std)

    run_len = float(params.get("corridor_length_ft", 500.0))
    # Y-offsets for each utility (all running east-west)
    water_y   =  15.0
    sewer_y   =   0.0
    gas_y     = -10.0
    elec_y    = -15.0

    # ── Water main (8") ───────────────────────────────────────────────────────
    msp.add_line((0, water_y), (run_len, water_y),
                 dxfattribs={"layer": "UTIL-WATER"})
    msp.add_text("8\" DIP CL350 WATER MAIN", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (5, water_y + 2),
    })

    # Gate valves every 500 LF (one at sta 0 and one at end)
    for gv_x in (0, run_len):
        msp.add_circle((gv_x, water_y), 2.5,
                        dxfattribs={"layer": "UTIL-WATER"})
        msp.add_text("GV", dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (gv_x - 2, water_y + 3),
        })

    # Fire hydrant at midpoint
    fh_x = run_len / 2.0
    msp.add_circle((fh_x, water_y + 5), 3.0,
                    dxfattribs={"layer": "UTIL-WATER"})
    msp.add_line((fh_x, water_y), (fh_x, water_y + 5),
                 dxfattribs={"layer": "UTIL-WATER"})
    msp.add_text("FH-1", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (fh_x + 3, water_y + 6),
    })
    msp.add_text("FH ASSEMBLY W/ 6\" GATE VALVE, BREAK-AWAY FLANGE", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (fh_x + 5, water_y + 3),
    })

    # Hydrant at start
    msp.add_circle((0, water_y + 5), 3.0, dxfattribs={"layer": "UTIL-WATER"})
    msp.add_text("FH-2", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (3, water_y + 6),
    })

    # ── Sewer (8" gravity, 4% max slope) ─────────────────────────────────────
    msp.add_line((0, sewer_y), (run_len, sewer_y),
                 dxfattribs={"layer": "UTIL-SEWER"})
    msp.add_text("8\" SDR-35 PVC GRAVITY SEWER @ 0.40% MIN", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (5, sewer_y + 2),
    })

    # Sewer manholes — spaced along corridor
    smh_spacing = run_len / 2.0
    smh_data = [
        (0,          "SMH-1", 103.50, 99.00),
        (smh_spacing, "SMH-2", 103.20, 97.80),
        (run_len,    "SMH-3", 103.00, 97.20),
    ]
    for smh_x, smh_lbl, smh_rim, smh_inv in smh_data:
        msp.add_circle((smh_x, sewer_y), 3.0,
                        dxfattribs={"layer": "UTIL-SEWER"})
        msp.add_text(smh_lbl, dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (smh_x - 4, sewer_y - 6),
        })
        msp.add_text(f"RIM={smh_rim:.2f} / INV={smh_inv:.2f}", dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (lat_x + 0.5, sewer_y - 6),
        })

    # ── Gas main (4") — 10' min horizontal separation ────────────────────────
    msp.add_line((0, gas_y), (run_len, gas_y),
                 dxfattribs={"layer": "UTIL-GAS"})
    msp.add_text("4\" STEEL GAS MAIN", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (5, gas_y - 2),
    })
    msp.add_text(f"HORIZ. SEP.: {abs(sewer_y - gas_y):.0f}' MIN (GAS TO SEWER)", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (run_len * 0.4, gas_y - 3),
    })

    # ── Electric conduit (2") — 5' min from gas ──────────────────────────────
    msp.add_line((0, elec_y), (run_len, elec_y),
                 dxfattribs={"layer": "UTIL-ELECTRIC"})
    msp.add_text("2\" ELEC. CONDUIT", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (5, elec_y - 2),
    })
    msp.add_text(f"HORIZ. SEP.: {abs(gas_y - elec_y):.0f}' MIN (ELEC. TO GAS)", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (run_len * 0.4, elec_y - 3),
    })

    # ── Crossing conflict at midpoint: sewer over water ───────────────────────
    cross_x = run_len / 2.0
    # Crossing marker (X symbol)
    d = 4.0
    msp.add_line((cross_x - d, sewer_y - d), (cross_x + d, water_y + d),
                 dxfattribs={"layer": "UTIL-CROSSING"})
    msp.add_line((cross_x - d, water_y + d), (cross_x + d, sewer_y - d),
                 dxfattribs={"layer": "UTIL-CROSSING"})
    msp.add_text("CROSSING", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (cross_x + 5, (sewer_y + water_y) / 2 + 2),
    })
    msp.add_text("WATER MAIN DEFLECTS DOWN 18\" MIN VERTICAL CLEARANCE AT CROSSING", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (cross_x + 5, (sewer_y + water_y) / 2 - 2),
    })

    # ── Meter vault MV-1 near end ─────────────────────────────────────────────
    mv_x = run_len * 0.92
    msp.add_lwpolyline(
        [(mv_x - 4, water_y - 4), (mv_x + 4, water_y - 4),
         (mv_x + 4, water_y + 4), (mv_x - 4, water_y + 4),
         (mv_x - 4, water_y - 4)],
        dxfattribs={"layer": "UTIL-WATER"},
    )
    msp.add_text("MV-1", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (mv_x - 2, water_y + 5),
    })

    # ── General notes ─────────────────────────────────────────────────────────
    notes_x, notes_y = 0.0, elec_y - 20.0
    msp.add_text("UTILITY NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (notes_x, notes_y),
    })
    notes = [
        "1. MIN HORIZONTAL SEPARATION WATER/SEWER: 10'-0\" (AWWA C600).",
        "2. 18\" MIN VERTICAL CLEARANCE AT CROSSINGS.",
        "3. ALL WATER MAIN: DIP CLASS 350 OR PVC C-900 DR-18.",
        "4. ALL SEWER: SDR-35 PVC ASTM D3034.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (notes_x, notes_y - 3 - i * 2.2),
        })

    # ── Title ─────────────────────────────────────────────────────────────────
    msp.add_text("UTILITY PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_XL,
        "insert": (run_len * 0.4, elec_y - 60),
    })
    msp.add_text("SCALE: 1\"=50'", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (run_len * 0.4, elec_y - 66),
    })
    # North arrow
    na_x, na_y = run_len + 20, 30
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (na_x - 1, na_y + 11),
    })


def _generate_site_plan(msp: Any, std: dict, description: str) -> None:
    """
    Site plan.  Parameters are extracted from *description* via LLM
    when available; otherwise sensible defaults are used.
    """
    params = _interpret_plan_description(description, "site", std)

    lot_w = float(params.get("lot_width_ft", 200.0))
    lot_d = float(params.get("lot_depth_ft", 218.0))
    n_buildings = int(params.get("n_buildings", 1))
    bldg_w = float(params.get("building_width_ft", 80.0))
    bldg_d = float(params.get("building_depth_ft", 120.0))
    n_parking = int(params.get("n_parking_stalls", 32))
    has_loading = params.get("has_loading_zone", True)
    has_bike_parking = params.get("has_bike_parking", True)

    # ── Property boundary with bearings ───────────────────────────────────────
    corners = [(0, 0), (lot_w, 0), (lot_w, lot_d), (0, lot_d), (0, 0)]
    msp.add_lwpolyline(corners, dxfattribs={"layer": "PROP-BOUNDARY"})
    bearings = [
        (lot_w / 2, -4, f"N 89\u00b014'32\" E  {lot_w:.2f}'"),
        (lot_w + 2, lot_d / 2, f"N 00\u00b045'28\" W  {lot_d:.2f}'"),
        (lot_w / 2, lot_d + 2, f"S 89\u00b014'32\" W  {lot_w:.2f}'"),
        (-45, lot_d / 2, f"S 00\u00b045'28\" E  {lot_d:.2f}'"),
    ]
    for bx, by, bearing in bearings:
        msp.add_text(bearing, dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM, "insert": (2, 27),
    })
    # Rear setback
    try:
        msp.add_line((0, lot_d - 10), (lot_w, lot_d - 10),
                     dxfattribs={"layer": "PROP-SETBACK", "linetype": "DASHED"})
    except Exception:
        msp.add_line((0, lot_d - 10), (lot_w, lot_d - 10),
                     dxfattribs={"layer": "PROP-SETBACK"})
    msp.add_text("10' REAR SETBACK", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM, "insert": (2, lot_d - 8),
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
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM, "insert": (6, lot_d / 2),
    })

    # ── Building footprint (centered on pad) ────────────────────────────────
    bldg_x0 = (lot_w - bldg_w) / 2
    bldg_y0 = (lot_d - bldg_d) / 2 + 15   # slightly south of center
    msp.add_lwpolyline(
        [(bldg_x0, bldg_y0), (bldg_x0 + bldg_w, bldg_y0),
         (bldg_x0 + bldg_w, bldg_y0 + bldg_d), (bldg_x0, bldg_y0 + bldg_d),
         (bldg_x0, bldg_y0)],
        dxfattribs={"layer": "BLDG-FOOTPRINT"},
    )
    bldg_sf = int(bldg_w * bldg_d)
    msp.add_text("PROPOSED BUILDING", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (bldg_x0 + 10, bldg_y0 + bldg_d / 2 + 3),
    })
    msp.add_text(f"\u00b1{bldg_sf:,} SF", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (bldg_x0 + 18, bldg_y0 + bldg_d / 2 - 3),
    })

    # ── Parking field ─────────────────────────────────────────────────────────
    stall_w, stall_d = 9.0, 18.0
    stalls_per_row = max(1, int((lot_w - 10) / stall_w))
    n_rows = max(1, math.ceil(n_parking / stalls_per_row))
    row1_y = 30.0   # first row, face of stall at y=30
    for row_idx in range(min(n_rows, 4)):  # cap at 4 rows
        row_y = row1_y + row_idx * (stall_d + 24.0)
        stalls_this_row = min(stalls_per_row, n_parking - row_idx * stalls_per_row)
        for col in range(max(0, stalls_this_row)):
            sx = 5.0 + col * stall_w
            if sx + stall_w > lot_w - 5:
                break
            msp.add_lwpolyline(
                [(sx, row_y), (sx + stall_w, row_y),
                 (sx + stall_w, row_y + stall_d), (sx, row_y + stall_d),
                 (sx, row_y)],
                dxfattribs={"layer": "SITE-PARKING"},
            )
    row2_y = row1_y + stall_d + 24.0  # for ADA/notes positioning

    n_ada = max(2, n_parking // 16)
    total_provided = n_parking + n_ada
    msp.add_text(f"{n_parking} STANDARD + {n_ada} ADA = {total_provided} PROVIDED ({n_parking} REQUIRED)", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (5, row2_y + stall_d + 2),
    })

    # ADA stalls (van-accessible, 8'+5' access aisle) at west end
    ada_x = 5.0
    ada_y = row1_y
    for i in range(n_ada):
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
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (5 + 8, row1_y + stall_d + 10),
    })

    # ── Loading zone (12'x35' at rear) ───────────────────────────────────────
    if has_loading:
        lz_x0 = lot_w - 40.0
        lz_y0 = lot_d - 10 - 35
        msp.add_lwpolyline(
            [(lz_x0, lz_y0), (lz_x0 + 35, lz_y0),
             (lz_x0 + 35, lz_y0 + 12), (lz_x0, lz_y0 + 12),
             (lz_x0, lz_y0)],
            dxfattribs={"layer": "SITE-LOADING"},
        )
        msp.add_text("LOADING ZONE", dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (lz_x0 + 3, lz_y0 + 7),
        })
        msp.add_text("NO PARKING", dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (lz_x0 + 5, lz_y0 + 3),
        })

    # ── Dumpster enclosure (12'x20') ─────────────────────────────────────────
    de_x0, de_y0 = lot_w - 30.0, lot_d - 40.0  # always draw dumpster
    msp.add_lwpolyline(
        [(de_x0, de_y0), (de_x0 + 20, de_y0),
         (de_x0 + 20, de_y0 + 12), (de_x0, de_y0 + 12),
         (de_x0, de_y0)],
        dxfattribs={"layer": "SITE-MISC"},
    )
    msp.add_text("DUMPSTER\nENCL.", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (de_x0 + 2, de_y0 + 5),
    })

    # ── Bicycle parking (rack near entrance) ────────────────────────────────
    if has_bike_parking:
        bp_x, bp_y = bldg_x0 + bldg_w + 5, bldg_y0 + 5
        msp.add_lwpolyline(
            [(bp_x, bp_y), (bp_x + 10, bp_y),
             (bp_x + 10, bp_y + 5), (bp_x, bp_y + 5), (bp_x, bp_y)],
            dxfattribs={"layer": "SITE-MISC"},
        )
        msp.add_text("BIKE PARKING\n(6 SPACES)", dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
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
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
        "insert": (si_x + 3, si_y),
    })

    # ── General notes ─────────────────────────────────────────────────────────
    lot_sf = int(lot_w * lot_d)
    lot_ac = lot_sf / 43560.0
    impervious_sf = int(bldg_w * bldg_d + n_parking * stall_w * stall_d)
    impervious_pct = impervious_sf / lot_sf * 100 if lot_sf > 0 else 0
    notes_x, notes_y = 0.0, -20.0
    msp.add_text("SITE NOTES:", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (notes_x, notes_y),
    })
    notes = [
        "1. ZONING: C-2 COMMERCIAL.",
        f"2. LOT AREA: {lot_sf:,} SF ({lot_ac:.2f} AC).",
        f"3. IMPERVIOUS COVER: {impervious_sf:,} SF ({impervious_pct:.1f}% -- MAX ALLOWED 75%).",
        f"4. REQUIRED PARKING: 1 SPACE PER 300 SF GFA = {n_parking} SPACES.",
    ]
    for i, note in enumerate(notes):
        msp.add_text(note, dxfattribs={
            "layer": "ANNO-TEXT", "height": TEXT_SCALE_SM,
            "insert": (notes_x, notes_y - 3 - i * 2.2),
        })

    # ── Title ─────────────────────────────────────────────────────────────────
    msp.add_text("SITE PLAN", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_XL,
        "insert": (lot_w / 2 - 15, -50),
    })
    msp.add_text("SCALE: 1\"=30'", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE,
        "insert": (lot_w / 2 - 12, -56),
    })
    # North arrow
    na_x, na_y = lot_w + 15, lot_d - 20
    msp.add_line((na_x, na_y - 10), (na_x, na_y + 10),
                 dxfattribs={"layer": "ANNO-TEXT"})
    msp.add_text("N", dxfattribs={
        "layer": "ANNO-TEXT", "height": TEXT_SCALE_LG, "insert": (na_x - 1, na_y + 11),
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
    # If output_path is a directory (no .dxf suffix), generate filename inside it
    if output_path.suffix.lower() != ".dxf":
        slug = re.sub(r"[^a-z0-9_]+", "_",
                      f"{state}_{discipline}".lower()).strip("_")
        output_path = output_path / f"{slug}.dxf"
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
                "height": TEXT_SCALE_SM,
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
