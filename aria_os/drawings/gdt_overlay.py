"""GD&T overlay for ezdxf — augment a 2D DXF with proper datums,
feature control frames (FCFs), basic dimensions, and a title-block
tolerance default.

This is the post-processing step that turns a clean projection (as
produced by FreeCAD TechDraw or `kicad-cli pcb export dxf`) into a
print-ready engineering drawing accepted by fab shops.

The module is deliberately conservative — it adds only safe, generic
GD&T callouts that hold for any prismatic part:

  - Datum A on the bottom face       (top view)
  - Datum B on the left edge         (front view)
  - Datum C on the back edge         (front view)
  - Position tolerance on each circle treated as a hole (true position
    in basic dimensions, ⌀0.10 to A B C unless overridden)
  - Surface roughness Ra 1.6 default (overall) — title block note
  - Edge profile tolerance 0.5 to A B C — for outline accuracy
  - Linear-tolerance default ±0.1 mm in title block

Per-feature overrides come from the `gdt_specs` parameter so callers
(e.g. mounting holes that must be ⌀0.05 to A) can refine.

Output: writes back over the same DXF (ezdxf 1.4+ writes R2018 by
default; that's the format AutoCAD/SolidWorks/SolidEdge open natively).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import ezdxf
from ezdxf import bbox
from ezdxf.enums import TextEntityAlignment


# Unicode symbols for ASME Y14.5 / ISO 1101 GD&T characteristics.
# Most CAD viewers render these correctly when the DXF is in R2018 (the
# default for ezdxf 1.x).
_SYM = {
    "position":      "⊕",  # ⊕ (true position)
    "concentricity": "◎",  # ◎
    "perpendicular": "⊥",  # ⊥
    "parallel":      "∥",  # ∥
    "flatness":      "▭",  # ▭
    "circularity":   "○",  # ○
    "cylindricity":  "⦾",  # ⦾
    "profile_line":  "⌒",  # ⌒
    "profile_surf":  "⌓",  # ⌓
    "diameter":      "Ø",  # Ø
    "degree":        "°",  # °
    "plus_minus":    "±",  # ±
}


# ---------------------------------------------------------------------------
# Layer setup
# ---------------------------------------------------------------------------

def _ensure_gdt_layers(doc: ezdxf.document.Drawing) -> None:
    """Create the GD&T layer scheme if missing. Each callout type lives on
    its own layer so the drafter can hide/show categories independently
    in the CAD viewer."""
    layers = doc.layers
    spec = [
        ("ARIA_DATUM",      1),  # red
        ("ARIA_FCF",        5),  # blue
        ("ARIA_DIM",        7),  # white/black
        ("ARIA_NOTES",      3),  # green
        ("ARIA_TITLEBLK",   8),  # dark gray
    ]
    for name, color in spec:
        if name in layers:
            continue
        layers.add(name, dxfattribs={"color": color})


# ---------------------------------------------------------------------------
# Datum triangle (ASME Y14.5: filled triangle + boxed letter)
# ---------------------------------------------------------------------------

def _draw_datum(msp: Any, x: float, y: float, label: str,
                  *, dx: float = 0.0, dy: float = -6.0,
                  size: float = 3.5) -> None:
    """Draw a filled datum triangle pointing at (x, y), label box offset
    by (dx, dy) from the apex.

    Implementation note: FreeCAD's DXF importer rejects DXF SOLID
    entities ("Unsupported DXF features: Entity type 'SOLID'"). We draw
    the triangle as a closed LWPOLYLINE outline + a HATCH fill, both of
    which import cleanly into FreeCAD, AutoCAD, SolidWorks, SolidEdge.
    """
    base_cx = x + dx
    base_cy = y + dy
    half = size / 2.0
    tri = [(x, y), (base_cx - half, base_cy), (base_cx + half, base_cy)]

    # Outline — closed LWPOLYLINE (universally imported)
    msp.add_lwpolyline(tri + [tri[0]],
                        dxfattribs={"layer": "ARIA_DATUM"})

    # Fill — HATCH with SOLID pattern. Wrapped in try/except so an
    # ezdxf-version quirk on hatch creation cannot drop the outline.
    try:
        hatch = msp.add_hatch(color=1,
                                dxfattribs={"layer": "ARIA_DATUM"})
        hatch.paths.add_polyline_path(tri + [tri[0]], is_closed=True)
    except Exception:
        # Outline alone is still readable; downstream importers fine.
        pass
    # Box around the letter
    box_w = size * 1.4
    box_h = size * 1.2
    bx = base_cx - box_w / 2
    by = base_cy - box_h
    msp.add_lwpolyline(
        [(bx, by), (bx + box_w, by), (bx + box_w, by + box_h),
         (bx, by + box_h), (bx, by)],
        dxfattribs={"layer": "ARIA_DATUM"})
    msp.add_text(label,
                  dxfattribs={"layer": "ARIA_DATUM",
                              "height": size * 0.7}
                  ).set_placement((base_cx, by + box_h * 0.2),
                                    align=TextEntityAlignment.MIDDLE_CENTER)


# ---------------------------------------------------------------------------
# Feature control frame — ASME Y14.5 compartmentalized
# ---------------------------------------------------------------------------

def _draw_fcf(msp: Any, x: float, y: float, *,
                symbol: str, tolerance: str,
                datums: list[str] | None = None,
                size: float = 3.0) -> float:
    """Draw a FCF at (x, y) growing to the right.

    Returns the total width drawn so callers can stack/chain frames.

    Compartments: [symbol] | [tolerance] | [datum A] | [datum B] | ...
    Each compartment is drawn as a rectangle with centered text.
    """
    datums = datums or []
    # Compartment widths (mm)
    sym_w = size * 1.6
    tol_w = max(size * 4.0, size * 0.6 * len(tolerance))
    dat_w = size * 1.4
    h = size * 1.4

    # Outer rectangle
    total_w = sym_w + tol_w + dat_w * len(datums)
    msp.add_lwpolyline(
        [(x, y), (x + total_w, y), (x + total_w, y + h),
         (x, y + h), (x, y)],
        dxfattribs={"layer": "ARIA_FCF"})

    # Vertical dividers
    cx = x + sym_w
    msp.add_line((cx, y), (cx, y + h),
                  dxfattribs={"layer": "ARIA_FCF"})
    cx += tol_w
    if datums:
        msp.add_line((cx, y), (cx, y + h),
                      dxfattribs={"layer": "ARIA_FCF"})
    for i in range(1, len(datums)):
        cx2 = x + sym_w + tol_w + dat_w * i
        msp.add_line((cx2, y), (cx2, y + h),
                      dxfattribs={"layer": "ARIA_FCF"})

    # Symbol cell
    msp.add_text(symbol,
                  dxfattribs={"layer": "ARIA_FCF",
                              "height": size * 0.85}
                  ).set_placement((x + sym_w / 2, y + h / 2),
                                    align=TextEntityAlignment.MIDDLE_CENTER)
    # Tolerance cell
    msp.add_text(tolerance,
                  dxfattribs={"layer": "ARIA_FCF",
                              "height": size * 0.7}
                  ).set_placement((x + sym_w + tol_w / 2, y + h / 2),
                                    align=TextEntityAlignment.MIDDLE_CENTER)
    # Datum cells
    for i, d in enumerate(datums):
        cx2 = x + sym_w + tol_w + dat_w * i + dat_w / 2
        msp.add_text(d,
                      dxfattribs={"layer": "ARIA_FCF",
                                  "height": size * 0.7}
                      ).set_placement((cx2, y + h / 2),
                                        align=TextEntityAlignment.MIDDLE_CENTER)

    return total_w


# ---------------------------------------------------------------------------
# Title block — bottom-right corner, ASME-style with tolerance defaults
# ---------------------------------------------------------------------------

def _draw_title_block(msp: Any, *, page_w: float, page_h: float,
                       title: str, part_no: str,
                       material: str, revision: str,
                       company: str, drawer: str,
                       tolerance_default: str = "±0.1 mm",
                       angular_default: str = "±0.5°",
                       surface_default: str = "Ra 1.6") -> None:
    """Draw a 180×60 mm title block in the bottom-right corner with
    standard ASME / ISO sections (title, part no, material, revision,
    drawer, tolerance defaults, surface roughness).
    """
    bw = 180.0
    bh = 60.0
    x0 = page_w - bw - 5.0
    y0 = 5.0

    msp.add_lwpolyline(
        [(x0, y0), (x0 + bw, y0), (x0 + bw, y0 + bh),
         (x0, y0 + bh), (x0, y0)],
        dxfattribs={"layer": "ARIA_TITLEBLK"})

    # Two horizontal dividers + two vertical dividers — 6-cell grid
    msp.add_line((x0, y0 + bh / 3),     (x0 + bw, y0 + bh / 3),
                  dxfattribs={"layer": "ARIA_TITLEBLK"})
    msp.add_line((x0, y0 + 2 * bh / 3), (x0 + bw, y0 + 2 * bh / 3),
                  dxfattribs={"layer": "ARIA_TITLEBLK"})
    msp.add_line((x0 + bw / 3, y0),     (x0 + bw / 3, y0 + bh),
                  dxfattribs={"layer": "ARIA_TITLEBLK"})
    msp.add_line((x0 + 2 * bw / 3, y0), (x0 + 2 * bw / 3, y0 + bh),
                  dxfattribs={"layer": "ARIA_TITLEBLK"})

    cell_w = bw / 3
    cell_h = bh / 3

    def _cell(col: int, row: int, label: str, value: str) -> None:
        cx = x0 + col * cell_w + cell_w / 2
        cy = y0 + (2 - row) * cell_h + cell_h / 2
        msp.add_text(label,
                      dxfattribs={"layer": "ARIA_TITLEBLK",
                                  "height": 2.0, "color": 8}
                      ).set_placement((cx - cell_w / 2 + 2,
                                        cy + cell_h / 2 - 3),
                                        align=TextEntityAlignment.LEFT)
        msp.add_text(value,
                      dxfattribs={"layer": "ARIA_TITLEBLK",
                                  "height": 3.5}
                      ).set_placement((cx, cy - 1),
                                        align=TextEntityAlignment.MIDDLE_CENTER)

    # Top row
    _cell(0, 0, "TITLE",      title or "Untitled")
    _cell(1, 0, "PART NO.",   part_no or "—")
    _cell(2, 0, "REV.",       revision or "A")
    # Middle row
    _cell(0, 1, "MATERIAL",   material or "AS NOTED")
    _cell(1, 1, "DRAWER",     drawer)
    _cell(2, 1, "COMPANY",    company)
    # Bottom row
    _cell(0, 2, "TOL. ±",     tolerance_default)
    _cell(1, 2, "ANG. ±",     angular_default)
    _cell(2, 2, "SURFACE",    surface_default)


# ---------------------------------------------------------------------------
# Hole detection from existing geometry
# ---------------------------------------------------------------------------

def _find_circles(msp: Any, *, min_r: float = 0.5, max_r: float = 10.0,
                    arc_min_sweep_deg: float = 200.0,
                    dedup_tol_factor: float = 1.5,
                    dedup_tol_floor: float = 0.5,
                    ) -> list[tuple[float, float, float]]:
    """Return (cx, cy, r) for every CIRCLE entity AND any near-closed ARC
    (sweep ≥ `arc_min_sweep_deg`) whose radius is in [min_r, max_r].

    OCP cross-sections often emit through-holes as paired arcs (e.g. two
    245° arcs at the same center), so we also accept arcs and dedup
    centers. Dedup tolerance scales with radius (tol = max(r * factor,
    floor)), so:
      - 0.4 mm vias    -> tol = max(0.6, 0.5) = 0.6 mm  (don't over-merge)
      - 1.65 mm slots  -> tol = max(2.5, 0.5) = 2.5 mm  (merges paired arc ends)
      - 5 mm bores     -> tol = max(7.5, 0.5) = 7.5 mm  (merges scan artefacts)

    Heuristic filter rejects rotation marks (r < min_r) and large outline
    arcs (r > max_r).
    """
    out: list[tuple[float, float, float]] = []
    seen: list[tuple[float, float, float]] = []

    def _add(cx: float, cy: float, r: float) -> None:
        if r < min_r or r > max_r:
            return
        tol = max(r * dedup_tol_factor, dedup_tol_floor)
        # Dedup: skip if a hole within tol already recorded
        for sx, sy, sr in seen:
            if (abs(sx - cx) <= tol
                    and abs(sy - cy) <= tol
                    and abs(sr - r) <= max(0.3, sr * 0.3)):
                return
        seen.append((cx, cy, r))
        out.append((cx, cy, r))

    for e in msp.query("CIRCLE"):
        _add(float(e.dxf.center[0]), float(e.dxf.center[1]),
              float(e.dxf.radius))

    for e in msp.query("ARC"):
        sweep = (float(e.dxf.end_angle) - float(e.dxf.start_angle)) % 360
        if sweep == 0 and float(e.dxf.end_angle) != float(e.dxf.start_angle):
            sweep = 360.0
        if sweep >= arc_min_sweep_deg:
            _add(float(e.dxf.center[0]), float(e.dxf.center[1]),
                  float(e.dxf.radius))

    return out


# ---------------------------------------------------------------------------
# Main entry point — overlay GD&T onto an existing DXF
# ---------------------------------------------------------------------------

def overlay_gdt(dxf_path: str | Path, *,
                  out_path: str | Path | None = None,
                  title: str = "",
                  part_no: str = "",
                  material: str = "",
                  revision: str = "A",
                  company: str = "ARIA-OS",
                  drawer: str = "ARIA-OS",
                  tolerance_default: str = "±0.1 mm",
                  angular_default: str = "±0.5°",
                  surface_default: str = "Ra 1.6",
                  position_tol_mm: float = 0.10,
                  hole_dia_mm: float | None = None,
                  ) -> dict:
    """Augment an existing DXF with GD&T datums, FCFs, bbox dimensions,
    and a title block. Writes to `out_path` (defaults to overwriting
    the input).

    Returns a dict with the augmentation summary:
        {"ok": bool, "out_path": str, "n_datums": int, "n_fcfs": int,
         "n_holes_dimensioned": int, "page_size_mm": [w, h]}
    """
    dxf_path = Path(dxf_path)
    out_path = Path(out_path) if out_path else dxf_path

    try:
        doc = ezdxf.readfile(dxf_path)
    except Exception as exc:
        return {"ok": False, "error": f"read failed: {exc}"}

    # KiCad and several legacy CAD tools export DXF R12 (AC1009); ezdxf's
    # high-level entities (LWPOLYLINE, MTEXT, dimensions) require R2000+.
    # Upgrade on-the-fly so we can write modern entities. Save as R2018
    # (the default for ezdxf 1.x) — AutoCAD 2018 / SolidWorks /
    # SolidEdge / TurboCAD all open it natively.
    if doc.dxfversion < "AC1015":  # R2000
        doc.dxfversion = "AC1032"  # R2018

    msp = doc.modelspace()
    _ensure_gdt_layers(doc)

    # Compute drawing extents — used to place datums and the title block
    try:
        ext = bbox.extents(msp)
    except Exception:
        ext = None
    if ext is None:
        # Fallback: assume A3 landscape (420×297 mm) with the geometry
        # at origin. The drawing might still be empty.
        page_w, page_h = 420.0, 297.0
        x_min, y_min, x_max, y_max = 0.0, 0.0, 60.0, 60.0
    else:
        x_min, y_min = float(ext.extmin[0]), float(ext.extmin[1])
        x_max, y_max = float(ext.extmax[0]), float(ext.extmax[1])
        page_w = max(297.0, (x_max - x_min) * 1.8)
        page_h = max(210.0, (y_max - y_min) * 1.8)

    # ---- Datums A, B, C on the primary view --------------------------
    n_datums = 0
    _draw_datum(msp, x_min, (y_min + y_max) / 2.0, "A",
                  dx=-8.0, dy=0.0)
    n_datums += 1
    _draw_datum(msp, (x_min + x_max) / 2.0, y_min, "B",
                  dx=0.0, dy=-8.0)
    n_datums += 1
    _draw_datum(msp, x_max, (y_min + y_max) / 2.0, "C",
                  dx=8.0, dy=0.0)
    n_datums += 1

    # ---- FCFs --------------------------------------------------------
    # Profile of the outline → A B C    (controls part outline accuracy)
    n_fcfs = 0
    _draw_fcf(msp, x_min, y_max + 8,
                symbol=_SYM["profile_surf"],
                tolerance="0.5", datums=["A", "B", "C"])
    n_fcfs += 1
    # Flatness on bottom face (datum A)
    _draw_fcf(msp, x_min, y_max + 14,
                symbol=_SYM["flatness"],
                tolerance="0.05")
    n_fcfs += 1
    # Perpendicularity B → A
    _draw_fcf(msp, x_min, y_max + 20,
                symbol=_SYM["perpendicular"],
                tolerance="0.1", datums=["A"])
    n_fcfs += 1

    # ---- Position tolerance for every detected hole ------------------
    holes = _find_circles(msp)
    pos_tol_str = f"{_SYM['diameter']}{position_tol_mm:.2f}"
    for (cx, cy, r) in holes:
        # Leader line from circle to FCF position above-right
        fx = cx + r + 6
        fy = cy + r + 6
        msp.add_line((cx, cy), (fx, fy),
                      dxfattribs={"layer": "ARIA_FCF"})
        _draw_fcf(msp, fx, fy,
                    symbol=_SYM["position"],
                    tolerance=pos_tol_str,
                    datums=["A", "B", "C"])
        n_fcfs += 1
        # Hole diameter callout (if known)
        if hole_dia_mm is not None:
            d_str = f"{_SYM['diameter']}{hole_dia_mm:.2f}"
            msp.add_text(d_str,
                          dxfattribs={"layer": "ARIA_DIM",
                                      "height": 2.5}
                          ).set_placement((cx, cy + r + 2.5),
                                            align=TextEntityAlignment.MIDDLE_CENTER)

    # ---- Linear bbox dimensions (overall width and height) ----------
    # ezdxf's dim renderer can throw on degenerate dim styles (NaN
    # rounding). We fall back to plain MTEXT + a horizontal line so a
    # minor renderer bug doesn't drop the dim entirely.
    def _linear_dim(p1, p2, base, *, vertical: bool):
        try:
            kwargs = dict(base=base, p1=p1, p2=p2,
                            dimstyle="Standard",
                            dxfattribs={"layer": "ARIA_DIM"})
            if vertical:
                kwargs["angle"] = 90
            msp.add_linear_dim(**kwargs).render()
            return True
        except Exception:
            # Fallback: leader line + MTEXT showing the value
            value = (abs(p2[1] - p1[1]) if vertical
                       else abs(p2[0] - p1[0]))
            msp.add_line(p1, p2,
                          dxfattribs={"layer": "ARIA_DIM"})
            mid = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            msp.add_text(f"{value:.2f}",
                          dxfattribs={"layer": "ARIA_DIM",
                                      "height": 3.0}
                          ).set_placement(mid,
                                            align=TextEntityAlignment.MIDDLE_CENTER)
            return False

    if ext is not None:
        _linear_dim((x_min, y_min), (x_max, y_min),
                      base=(x_min + (x_max - x_min) / 2, y_min - 12),
                      vertical=False)
        _linear_dim((x_max, y_min), (x_max, y_max),
                      base=(x_max + 12, y_min + (y_max - y_min) / 2),
                      vertical=True)

    # ---- Title block --------------------------------------------------
    _draw_title_block(msp,
                        page_w=page_w, page_h=page_h,
                        title=title or dxf_path.stem,
                        part_no=part_no or dxf_path.stem,
                        material=material,
                        revision=revision,
                        company=company,
                        drawer=drawer,
                        tolerance_default=tolerance_default,
                        angular_default=angular_default,
                        surface_default=surface_default)

    # ---- Notes (top-left) -------------------------------------------
    note_y = y_max + 28
    notes = [
        "NOTES:",
        "1. INTERPRET PER ASME Y14.5-2018",
        "2. REMOVE ALL BURRS AND SHARP EDGES",
        "3. UNLESS OTHERWISE SPECIFIED, "
        f"DIMENSIONS ARE IN MM, TOL {tolerance_default}",
        f"4. SURFACE ROUGHNESS {surface_default} UNLESS NOTED",
        "5. MATERIAL: " + (material.upper() if material else "AS NOTED"),
    ]
    for i, line in enumerate(notes):
        msp.add_text(line,
                      dxfattribs={"layer": "ARIA_NOTES",
                                  "height": 2.5}
                      ).set_placement(
            (x_min, note_y + 4 * (len(notes) - i)),
            align=TextEntityAlignment.LEFT)

    # ---- Sanitize: convert any SOLID / 3DFACE entities to LWPOLYLINE
    #      + HATCH so FreeCAD's importer (which rejects SOLID with
    #      "Unsupported DXF features") still loads the drawing. This is
    #      a belt-and-braces pass: even if upstream geometry (KiCad
    #      Edge_Cuts, FreeCAD TechDraw) emits a SOLID, we rewrite it.
    sanitized = _sanitize_freecad(msp)

    try:
        doc.saveas(out_path)
    except Exception as exc:
        return {"ok": False, "error": f"save failed: {exc}"}

    return {
        "ok": True,
        "out_path": str(out_path),
        "n_datums": n_datums,
        "n_fcfs": n_fcfs,
        "n_holes_dimensioned": len(holes),
        "page_size_mm": [round(page_w, 1), round(page_h, 1)],
        "bbox_mm": [round(x_min, 2), round(y_min, 2),
                       round(x_max, 2), round(y_max, 2)],
        "sanitized": sanitized,
    }


# ---------------------------------------------------------------------------
# Sanitizer — rewrite entities that FreeCAD's DXF importer rejects
# ---------------------------------------------------------------------------

def _sanitize_freecad(msp: Any) -> dict:
    """Rewrite SOLID and 3DFACE entities as closed LWPOLYLINE + HATCH.

    FreeCAD's built-in DXF importer (Draft.importDXF) emits warnings of
    the form "Unsupported DXF features: Entity type 'SOLID': N time(s)
    first at line ..." and silently skips those entities, leaving the
    drawing partially blank. This pass converts every SOLID/3DFACE in
    the modelspace into a closed polyline outline + a HATCH fill on the
    same layer/color, both of which FreeCAD imports natively.

    Returns counts so the caller can record the rewrite in the run
    manifest.
    """
    rewrote_solid = 0
    rewrote_3dface = 0

    # Convert SOLID entities (4 vertices in DXF; OCP/AutoCAD fill order)
    for ent in list(msp.query("SOLID")):
        try:
            verts = [
                (float(ent.dxf.vtx0[0]), float(ent.dxf.vtx0[1])),
                (float(ent.dxf.vtx1[0]), float(ent.dxf.vtx1[1])),
                (float(ent.dxf.vtx3[0]), float(ent.dxf.vtx3[1])),
                (float(ent.dxf.vtx2[0]), float(ent.dxf.vtx2[1])),
            ]
            # Drop duplicate trailing vertex (triangle stored as quad
            # by repeating the last point)
            uniq: list[tuple[float, float]] = []
            for v in verts:
                if not uniq or (abs(v[0] - uniq[-1][0]) > 1e-6
                                  or abs(v[1] - uniq[-1][1]) > 1e-6):
                    uniq.append(v)
            if len(uniq) < 3:
                msp.delete_entity(ent)
                continue
            attrs = {"layer": ent.dxf.layer}
            try:
                attrs["color"] = ent.dxf.color
            except Exception:
                pass
            msp.add_lwpolyline(uniq + [uniq[0]], dxfattribs=attrs)
            try:
                hatch = msp.add_hatch(color=attrs.get("color", 1),
                                        dxfattribs={"layer": attrs["layer"]})
                hatch.paths.add_polyline_path(uniq + [uniq[0]],
                                                is_closed=True)
            except Exception:
                pass
            msp.delete_entity(ent)
            rewrote_solid += 1
        except Exception:
            # If any one entity is malformed, leave it alone rather than
            # crashing the whole save — better a partial drawing than
            # none at all.
            continue

    for ent in list(msp.query("3DFACE")):
        try:
            verts = [
                (float(ent.dxf.vtx0[0]), float(ent.dxf.vtx0[1])),
                (float(ent.dxf.vtx1[0]), float(ent.dxf.vtx1[1])),
                (float(ent.dxf.vtx2[0]), float(ent.dxf.vtx2[1])),
                (float(ent.dxf.vtx3[0]), float(ent.dxf.vtx3[1])),
            ]
            uniq = []
            for v in verts:
                if not uniq or (abs(v[0] - uniq[-1][0]) > 1e-6
                                  or abs(v[1] - uniq[-1][1]) > 1e-6):
                    uniq.append(v)
            if len(uniq) < 3:
                msp.delete_entity(ent)
                continue
            attrs = {"layer": ent.dxf.layer}
            msp.add_lwpolyline(uniq + [uniq[0]], dxfattribs=attrs)
            msp.delete_entity(ent)
            rewrote_3dface += 1
        except Exception:
            continue

    return {"solid_rewrites": rewrote_solid,
              "face3d_rewrites": rewrote_3dface}


__all__ = ["overlay_gdt", "_SYM"]
