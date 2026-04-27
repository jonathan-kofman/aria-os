"""STEP -> 2D DXF via CadQuery section + ezdxf.

FreeCAD's `TechDraw.writeDXFPage` in headless mode (1.0) saves the
template skeleton but doesn't bake the projected view geometry into
the DXF. So the produced file is empty of real entities, and GD&T
overlay finds no holes/edges to annotate.

This module provides a deterministic CadQuery-based projector: import
the STEP, slice it at mid-height, and write each edge to DXF as the
right primitive (LINE / CIRCLE / ARC). The result is a real DXF that
GD&T overlay can detect mounting holes from.

Usage:
    from aria_os.drawings.cq_dxf_projector import step_to_top_view_dxf
    n_circles = step_to_top_view_dxf("part.step", "part_top.dxf")
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cadquery as cq
import ezdxf
import math


def _classify_edge(edge: Any) -> tuple[str, dict]:
    """Return (kind, params) for a CadQuery edge, where kind is one of:
       'circle'   — params: cx, cy, r
       'arc'      — params: cx, cy, r, start_a_deg, end_a_deg
       'line'     — params: x1, y1, x2, y2
       'polyline' — params: points (list of (x,y))   — fallback
    """
    geom = edge.geomType()
    if geom == "CIRCLE":
        # Closed circle if start==end at full sweep; otherwise an arc
        try:
            r = edge.radius()
            c = edge.Center()
            cx, cy = float(c.x), float(c.y)
            sp = edge.startPoint()
            ep = edge.endPoint()
            d = math.hypot(sp.x - ep.x, sp.y - ep.y)
            # Full circle if start coincides with end
            if d < 1e-3:
                return "circle", {"cx": cx, "cy": cy, "r": r}
            sa = math.degrees(math.atan2(sp.y - cy, sp.x - cx))
            ea = math.degrees(math.atan2(ep.y - cy, ep.x - cx))
            return "arc", {"cx": cx, "cy": cy, "r": r,
                              "start_a": sa, "end_a": ea}
        except Exception:
            pass
    if geom == "LINE":
        try:
            sp = edge.startPoint()
            ep = edge.endPoint()
            return "line", {"x1": float(sp.x), "y1": float(sp.y),
                                "x2": float(ep.x), "y2": float(ep.y)}
        except Exception:
            pass
    # Fallback: discretise into a polyline
    try:
        ts = [i / 24 for i in range(25)]
        pts = []
        for t in ts:
            try:
                p = edge.positionAt(t)
                pts.append((float(p.x), float(p.y)))
            except Exception:
                continue
        return "polyline", {"points": pts}
    except Exception:
        return "polyline", {"points": []}


def step_to_top_view_dxf(step_path: str | Path,
                            out_dxf: str | Path,
                            *, slice_z: float | None = None,
                            min_circle_r: float = 0.3,
                            max_circle_r: float = 50.0,
                            ) -> dict:
    """Slice the STEP at `slice_z` (default mid-height) and write a DXF
    of the resulting cross-section edges.

    Returns:
        {"ok": bool, "n_edges": int, "n_circles": int,
         "n_lines": int, "n_arcs": int, "bbox_mm": [xmin,ymin,xmax,ymax],
         "out_path": str}
    """
    step_path = Path(step_path)
    out_dxf = Path(out_dxf)
    out_dxf.parent.mkdir(parents=True, exist_ok=True)

    if not step_path.is_file():
        return {"ok": False, "error": f"STEP not found: {step_path}"}

    try:
        wp = cq.importers.importStep(str(step_path))
        sol = wp.val()
    except Exception as exc:
        return {"ok": False, "error": f"STEP import failed: {exc}"}

    bb = sol.BoundingBox()
    z = slice_z if slice_z is not None else (bb.zmin + bb.zmax) / 2.0
    try:
        sec = wp.section(z)
        edges = sec.edges().vals()
    except Exception as exc:
        return {"ok": False, "error": f"section failed: {exc}"}

    if not edges:
        return {"ok": False, "error": "section returned no edges",
                  "n_edges": 0}

    # Build the DXF
    doc = ezdxf.new(dxfversion="R2018")
    msp = doc.modelspace()
    # Layer scheme matching the GD&T overlay's expectations
    for name, color in (("OUTLINE", 7), ("HOLES", 1), ("INTERNAL", 3)):
        if name not in doc.layers:
            doc.layers.add(name, dxfattribs={"color": color})

    n_circles = n_lines = n_arcs = n_poly = 0
    for e in edges:
        kind, p = _classify_edge(e)
        if kind == "circle" and min_circle_r <= p["r"] <= max_circle_r:
            # Hole: small circle goes on HOLES layer; large on OUTLINE.
            layer = "HOLES" if p["r"] < 5.0 else "OUTLINE"
            msp.add_circle(center=(p["cx"], p["cy"]), radius=p["r"],
                              dxfattribs={"layer": layer})
            n_circles += 1
        elif kind == "arc":
            msp.add_arc(center=(p["cx"], p["cy"]), radius=p["r"],
                          start_angle=p["start_a"], end_angle=p["end_a"],
                          dxfattribs={"layer": "OUTLINE"})
            n_arcs += 1
        elif kind == "line":
            msp.add_line(start=(p["x1"], p["y1"]),
                            end=(p["x2"], p["y2"]),
                            dxfattribs={"layer": "OUTLINE"})
            n_lines += 1
        elif kind == "polyline" and p["points"]:
            msp.add_lwpolyline(p["points"],
                                  dxfattribs={"layer": "INTERNAL"})
            n_poly += 1

    try:
        doc.saveas(out_dxf)
    except Exception as exc:
        return {"ok": False, "error": f"save failed: {exc}"}

    # Recompute bbox from the geometry we wrote
    try:
        from ezdxf import bbox as _bbox
        ext = _bbox.extents(msp)
        x0, y0 = float(ext.extmin[0]), float(ext.extmin[1])
        x1, y1 = float(ext.extmax[0]), float(ext.extmax[1])
    except Exception:
        x0 = y0 = x1 = y1 = 0.0

    return {
        "ok":          True,
        "n_edges":     len(edges),
        "n_circles":   n_circles,
        "n_lines":     n_lines,
        "n_arcs":      n_arcs,
        "n_polyline":  n_poly,
        "bbox_mm":     [round(x0, 2), round(y0, 2),
                          round(x1, 2), round(y1, 2)],
        "out_path":    str(out_dxf),
        "slice_z":     round(z, 2),
    }


__all__ = ["step_to_top_view_dxf"]
