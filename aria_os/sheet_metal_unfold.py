"""
aria_os/sheet_metal_unfold.py — flat pattern DXF export for sheet metal parts.

The cadquery sheet_metal templates already compute bend-deduction and flat
blank dimensions but only emit the FORMED 3D body. Production fab shops
need the FLAT PATTERN — the unfolded 2D outline that goes to a laser
cutter or punch with bend lines marked.

This module writes a DXF with:
  - The flat outline (rectangle minus any holes)
  - Bend lines on a separate "BEND" layer (dashed)
  - A title block with material, gauge, k-factor, bend allowance, blank size

Public surface:
  unfold_panel(params, out_path) -> dict
  unfold_box(params, out_path)   -> dict
  unfold_from_session(session, out_dir) -> Optional[Path]
      Auto-detects which template was used and routes accordingly.

Implementation notes:
  - Uses ezdxf (already a hard dep of ARIA-OS for DXF outputs).
  - Bend allowance formula: BA = (pi * (R + k*T) * angle/180)
    (bend allowance, NOT bend deduction — BA is the developed length of
    the curved bend region; BD is what gets subtracted from a sum of
    leg lengths. BA = leg + leg + BA = total flat blank).
  - For a single-flange panel:
      flat_blank = web + leg + BA - 2*(R + T) - 2*leg
                 = web + leg - BD     (simpler form)
    where BD = 2*(R+T)*tan(A/2) - BA. We use BD for parity with the
    existing template's print line.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional


def _bend_metrics(thickness: float, radius: float, angle_deg: float, k_factor: float) -> dict[str, float]:
    """Compute bend allowance + bend deduction for a single bend."""
    a_rad = math.radians(angle_deg)
    ba = math.pi * (radius + k_factor * thickness) * (angle_deg / 180.0)
    bd = 2.0 * (radius + thickness) * math.tan(a_rad / 2.0) - ba
    return {
        "bend_allowance_mm": round(ba, 4),
        "bend_deduction_mm": round(bd, 4),
        "neutral_radius_mm": round(radius + k_factor * thickness, 4),
    }


def unfold_panel(params: dict[str, Any], out_path: Path) -> dict[str, Any]:
    """
    Unfold a `_cq_sheet_metal_panel` part into its flat blank DXF.

    Returns a stats dict the orchestrator can drop into the run manifest.
    """
    try:
        import ezdxf  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ezdxf is required for sheet metal unfold") from exc

    length   = float(params.get("length_mm", 200.0))
    width    = float(params.get("width_mm",  100.0))
    t        = float(params.get("thickness_mm", params.get("gauge_mm", 1.5)))
    angle    = float(params.get("bend_angle_deg", 90.0))
    r        = float(params.get("bend_radius_mm", 2.0 * t))
    kf       = float(params.get("k_factor", 0.45))
    flange   = float(params.get("flange_mm", 25.0))
    n_bends  = int(params.get("n_bends", 1))
    n_holes  = int(params.get("n_holes", 0))
    hdia     = float(params.get("hole_dia_mm", 6.0))

    metrics = _bend_metrics(t, r, angle, kf)
    bd = metrics["bend_deduction_mm"]
    flat_width = round(width + n_bends * flange - n_bends * bd, 3)
    flat_length = length

    # DXF coordinates: (0,0) is the lower-left of the flat blank
    doc = ezdxf.new(dxfversion="R2018", setup=True)
    msp = doc.modelspace()

    # Layers
    if "OUTLINE" not in doc.layers:
        doc.layers.new(name="OUTLINE", dxfattribs={"color": 7})  # white
    if "BEND" not in doc.layers:
        doc.layers.new(name="BEND", dxfattribs={"color": 1, "linetype": "DASHED2"})  # red dashed
    if "HOLES" not in doc.layers:
        doc.layers.new(name="HOLES", dxfattribs={"color": 5})  # blue

    # Outline: rectangle of (flat_length, flat_width)
    p0 = (0.0, 0.0)
    p1 = (flat_length, 0.0)
    p2 = (flat_length, flat_width)
    p3 = (0.0, flat_width)
    msp.add_lwpolyline([p0, p1, p2, p3, p0], dxfattribs={"layer": "OUTLINE"})

    # Bend lines: one or two horizontal lines parallel to the long edge
    # First bend at y = flange (the flat blank's bend region centerline)
    bend_y_positions: list[float] = []
    if n_bends >= 1:
        bend_y_positions.append(flange - bd / 2.0)
    if n_bends >= 2:
        bend_y_positions.append(flat_width - flange + bd / 2.0)
    for by in bend_y_positions:
        msp.add_line((0.0, by), (flat_length, by), dxfattribs={"layer": "BEND"})

    # Holes — placed in the flat web region (between bend lines for n_bends=2,
    # or above the single bend for n_bends=1)
    if n_holes > 0:
        margin = max(hdia * 2.0, 10.0)
        if n_bends == 0:
            web_y_center = flat_width / 2.0
        elif n_bends == 1:
            web_y_center = flange + (width / 2.0)
        else:
            web_y_center = flange + (width / 2.0)
        for i in range(n_holes):
            x = margin + (flat_length - 2 * margin) * i / max(n_holes - 1, 1)
            msp.add_circle((x, web_y_center), hdia / 2.0, dxfattribs={"layer": "HOLES"})

    # Title block (text only, lower-right corner outside the part)
    info_x = flat_length + 5.0
    info_y = flat_width
    title_lines = [
        f"FLAT BLANK: {flat_length:.2f} x {flat_width:.2f} mm",
        f"GAUGE:      {t:.2f} mm",
        f"K-FACTOR:   {kf}",
        f"BEND R:     {r:.2f} mm",
        f"BEND ANGLE: {angle:.1f} deg x {n_bends} bend(s)",
        f"BEND ALLOW: {metrics['bend_allowance_mm']:.3f} mm",
        f"BEND DEDUCT:{metrics['bend_deduction_mm']:.3f} mm",
        f"HOLES:      {n_holes} x diameter {hdia:.2f} mm",
    ]
    for i, line in enumerate(title_lines):
        msp.add_text(
            line,
            dxfattribs={"height": 4.0, "layer": "OUTLINE"},
        ).set_placement((info_x, info_y - i * 6.0))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out_path)

    return {
        "ok": True,
        "dxf_path": str(out_path),
        "flat_blank_mm": [flat_length, flat_width],
        "n_bends": n_bends,
        "n_holes": n_holes,
        "thickness_mm": t,
        "k_factor": kf,
        **metrics,
    }


def unfold_box(params: dict[str, Any], out_path: Path) -> dict[str, Any]:
    """
    Unfold a 5-sided sheet-metal box (open top) into a cross-shaped flat blank.

    Layout:
              ┌─────┐
              │ B   │   back
        ┌────┼─────┼────┐
        │ L  │ Bot │ R  │   left, bottom, right
        └────┼─────┼────┘
              │ F   │   front
              └─────┘
    """
    try:
        import ezdxf  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ezdxf is required for sheet metal unfold") from exc

    L = float(params.get("length_mm", 150.0))
    W = float(params.get("width_mm",  100.0))
    H = float(params.get("height_mm",  50.0))
    t = float(params.get("thickness_mm", 1.5))
    r = float(params.get("bend_radius_mm", 2.0 * t))
    kf = float(params.get("k_factor", 0.45))
    metrics = _bend_metrics(t, r, 90.0, kf)
    bd = metrics["bend_deduction_mm"]

    # Cross layout dimensions (subtract bd at each shared edge)
    bot_w = L - 2 * bd
    bot_h = W - 2 * bd
    side_h = H - bd
    flap_h = H - bd

    doc = ezdxf.new(dxfversion="R2018", setup=True)
    msp = doc.modelspace()
    if "OUTLINE" not in doc.layers:
        doc.layers.new(name="OUTLINE", dxfattribs={"color": 7})
    if "BEND" not in doc.layers:
        doc.layers.new(name="BEND", dxfattribs={"color": 1, "linetype": "DASHED2"})

    # Place the bottom centered at origin
    cx, cy = 0.0, 0.0
    bot = [
        (cx - bot_w / 2, cy - bot_h / 2),
        (cx + bot_w / 2, cy - bot_h / 2),
        (cx + bot_w / 2, cy + bot_h / 2),
        (cx - bot_w / 2, cy + bot_h / 2),
    ]

    # Outer outline as a single closed polyline traced clockwise around the cross
    pts: list[tuple[float, float]] = []
    # Start lower-left of left side
    pts.append((cx - bot_w / 2 - side_h, cy - bot_h / 2))
    pts.append((cx - bot_w / 2, cy - bot_h / 2))
    pts.append((cx - bot_w / 2, cy - bot_h / 2 - flap_h))
    pts.append((cx + bot_w / 2, cy - bot_h / 2 - flap_h))
    pts.append((cx + bot_w / 2, cy - bot_h / 2))
    pts.append((cx + bot_w / 2 + side_h, cy - bot_h / 2))
    pts.append((cx + bot_w / 2 + side_h, cy + bot_h / 2))
    pts.append((cx + bot_w / 2, cy + bot_h / 2))
    pts.append((cx + bot_w / 2, cy + bot_h / 2 + flap_h))
    pts.append((cx - bot_w / 2, cy + bot_h / 2 + flap_h))
    pts.append((cx - bot_w / 2, cy + bot_h / 2))
    pts.append((cx - bot_w / 2 - side_h, cy + bot_h / 2))
    pts.append((cx - bot_w / 2 - side_h, cy - bot_h / 2))
    msp.add_lwpolyline(pts, dxfattribs={"layer": "OUTLINE"})

    # Bend lines — 4 edges of the bottom rectangle
    msp.add_line((cx - bot_w / 2, cy - bot_h / 2), (cx + bot_w / 2, cy - bot_h / 2), dxfattribs={"layer": "BEND"})
    msp.add_line((cx + bot_w / 2, cy - bot_h / 2), (cx + bot_w / 2, cy + bot_h / 2), dxfattribs={"layer": "BEND"})
    msp.add_line((cx + bot_w / 2, cy + bot_h / 2), (cx - bot_w / 2, cy + bot_h / 2), dxfattribs={"layer": "BEND"})
    msp.add_line((cx - bot_w / 2, cy + bot_h / 2), (cx - bot_w / 2, cy - bot_h / 2), dxfattribs={"layer": "BEND"})

    # Title block
    info_x = cx + bot_w / 2 + side_h + 10
    info_y = cy + bot_h / 2 + flap_h
    title_lines = [
        f"5-SIDED BOX (open top)",
        f"INTERNAL: {L:.1f} x {W:.1f} x {H:.1f} mm",
        f"GAUGE:    {t:.2f} mm",
        f"K-FACTOR: {kf}",
        f"BEND R:   {r:.2f} mm",
        f"BEND DED: {bd:.3f} mm",
    ]
    for i, line in enumerate(title_lines):
        msp.add_text(line, dxfattribs={"height": 4.0, "layer": "OUTLINE"}).set_placement((info_x, info_y - i * 6.0))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out_path)

    return {
        "ok": True,
        "dxf_path": str(out_path),
        "internal_mm": [L, W, H],
        "thickness_mm": t,
        "k_factor": kf,
        **metrics,
    }


# ---------------------------------------------------------------------------
# Orchestrator hook
# ---------------------------------------------------------------------------

def unfold_from_session(session: dict[str, Any], out_dir: Path) -> Optional[Path]:
    """
    Given a finished pipeline session, detect whether the part is sheet
    metal, and if so emit a flat-pattern DXF into out_dir.

    Returns the DXF Path on success, None when the part isn't sheet metal
    or unfold failed (logged but not raised).
    """
    spec = session.get("spec") or {}
    params = session.get("params") or {}
    template = (
        session.get("template_used")
        or spec.get("part_type")
        or params.get("part_type")
        or ""
    ).lower()

    sheet_keywords = ("sheet_metal", "sheet metal", "bent_plate", "u_channel",
                       "formed_channel", "sheet_metal_panel", "sheet_metal_box",
                       "sheet_metal_tray", "formed_box")
    if not any(kw in template for kw in sheet_keywords) and not any(
        kw in (session.get("goal") or "").lower() for kw in ("sheet metal", "bent ", "press brake")
    ):
        return None

    # Merge spec + params so the unfold has everything either source provided
    merged: dict[str, Any] = {}
    merged.update(params)
    merged.update(spec)

    out_dir = Path(out_dir)
    part_id = session.get("part_id") or "sheet_part"
    dxf_path = out_dir / f"{part_id}_flat_pattern.dxf"

    try:
        if "box" in template or "tray" in template:
            return Path(unfold_box(merged, dxf_path)["dxf_path"])
        return Path(unfold_panel(merged, dxf_path)["dxf_path"])
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("sheet metal unfold failed: %s", exc)
        return None


def render_unfold(dxf_path: Path, png_out: Path) -> dict:
    """Convenience wrapper that imports aria_os.visual_qa.dxf_renderer and renders.

    Bridges the unfold module to the reusable visual_qa renderer without
    creating a hard coupling — the import is deferred until call time so
    ``sheet_metal_unfold`` stays import-cheap for pipelines that don't
    need PNG previews.

    Returns the dict produced by ``render_dxf`` (``ok``, ``png_path``,
    ``bbox``, ``layer_counts``, ``entity_total``). Never raises.
    """
    try:
        from aria_os.visual_qa.dxf_renderer import render_dxf
    except Exception as exc:
        return {
            "ok": False,
            "error": f"visual_qa import failed: {exc}",
            "png_path": None,
        }
    return render_dxf(Path(dxf_path), Path(png_out))
