"""
diy_fab.py - convert a generated .kicad_pcb into fabrication artifacts that can
be made at home on a hobbyist 3D printer + CNC, no PCB house involved.

Input:  a .kicad_pcb file written by aria_os.ecad.kicad_pcb_writer (single-
        layer, SMD + through-hole pads, straight F.Cu segments, rectangular
        Edge.Cuts outline).

Two fabrication routes share a common trace extractor:

  Route A - CNC isolation milling:
      emit_cnc_isolation_gcode()  F.Cu trace isolation channels + pad holes
      emit_cnc_drill_gcode()      through-hole pad drill program

  Route B - 3D-printed channel substrate + copper-tape inlay (novel):
      emit_printed_substrate_stl()     printable slab with trace channels
      emit_copper_tape_cut_svg()       vinyl-cutter pattern for copper foil
      emit_solder_paste_stencil_stl()  printable solder paste stencil

  run_diy_fab()  top-level orchestrator, writes everything into
                 <out_dir>/diy_fab/ and returns a result dict.

Scope / non-goals:
  - single-layer F.Cu only; back-copper segments are ignored
  - through-hole drill pattern is derived from SMD pad centers (kicad_pcb_writer
    does not emit real PTH pads yet, so every pad becomes an anchor hole for
    DIY hand-soldered components)
  - no DRC, no clearance check, no multi-layer handling
  - coordinate system for all emitted artifacts is "board frame":
        origin at bottom-left of board, Y+ up, Z+ up, mm

Standalone usage (no pipeline wiring yet):
    from aria_os.ecad.diy_fab import run_diy_fab
    result = run_diy_fab("board.kicad_pcb", "out/", route="both")
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

try:
    import cadquery as cq  # noqa: F401  (imported lazily inside functions)
    _HAVE_CQ = True
except Exception:
    _HAVE_CQ = False


# ---------------------------------------------------------------------------
# S-expression parser  (tolerant, non-validating, just enough for kicad_pcb_writer)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()]+')


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _parse_sexpr(text: str) -> list:
    """Parse a KiCad-style s-expression into nested lists. Strings keep quotes
    stripped; all other atoms are kept as plain strings (numbers are parsed
    later at the point of use)."""
    tokens = _tokenize(text)
    pos = 0

    def parse_node() -> Any:
        nonlocal pos
        tok = tokens[pos]
        if tok == "(":
            pos += 1
            node: list = []
            while pos < len(tokens) and tokens[pos] != ")":
                node.append(parse_node())
            pos += 1  # consume ')'
            return node
        if tok.startswith('"') and tok.endswith('"'):
            pos += 1
            return tok[1:-1]
        pos += 1
        return tok

    # find outermost '('
    while pos < len(tokens) and tokens[pos] != "(":
        pos += 1
    return parse_node()


def _find_all(node: list, head: str) -> list[list]:
    """Yield every immediate child list whose first atom equals `head`."""
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def _find_first(node: list, head: str) -> list | None:
    for c in node:
        if isinstance(c, list) and c and c[0] == head:
            return c
    return None


def _floats(atoms: list) -> list[float]:
    out: list[float] = []
    for a in atoms:
        if isinstance(a, str):
            try:
                out.append(float(a))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------------------
# Common extractor
# ---------------------------------------------------------------------------

def _extract_traces_from_pcb(kicad_pcb_path: str | Path) -> dict:
    """Parse a .kicad_pcb and return a fabrication-oriented dict.

    Returned keys:
      board_size:      (w_mm, h_mm)
      origin:          (ox, oy)   KiCad page coords of bottom-left corner
      components:      [{ref, xy_mm, pads:[(x,y), ...], n_pads}]
      traces:          [(net_name, [(x1,y1),(x2,y2)], width_mm)]  polylines
      mounting_holes:  [{xy, dia_mm}]
      pad_holes:       [(x,y,dia_mm)]  flattened list of every pad centre

    All xy coordinates are in *board frame* (origin at bottom-left, Y+ up).
    """
    path = Path(kicad_pcb_path)
    tree = _parse_sexpr(path.read_text(encoding="utf-8"))

    # --- Edge.Cuts bounding box -> board size, plus board-frame origin ---
    edge_x: list[float] = []
    edge_y: list[float] = []
    for line in _find_all(tree, "gr_line"):
        layer = _find_first(line, "layer")
        if not layer or len(layer) < 2 or layer[1] != "Edge.Cuts":
            continue
        start = _find_first(line, "start")
        end = _find_first(line, "end")
        if start:
            edge_x.append(float(start[1])); edge_y.append(float(start[2]))
        if end:
            edge_x.append(float(end[1])); edge_y.append(float(end[2]))

    if edge_x and edge_y:
        ox, oy = min(edge_x), min(edge_y)
        board_w = max(edge_x) - ox
        board_h = max(edge_y) - oy
    else:
        ox = oy = 0.0
        board_w = board_h = 50.0

    # KiCad Y increases DOWN, so to put origin at bottom-left of board in
    # math-standard Y+ up frame we flip: board_y = (oy + board_h) - kicad_y
    def to_board(kx: float, ky: float) -> tuple[float, float]:
        return (kx - ox, (oy + board_h) - ky)

    # --- Footprints + pads (pads are in footprint-local coords) ---
    components: list[dict] = []
    pad_holes: list[tuple[float, float, float]] = []
    for fp in _find_all(tree, "footprint"):
        at = _find_first(fp, "at")
        if not at:
            continue
        fx = float(at[1]); fy = float(at[2])
        rot = float(at[3]) if len(at) > 3 else 0.0
        cos_r = math.cos(math.radians(rot))
        sin_r = math.sin(math.radians(rot))

        ref = ""
        for fp_text in _find_all(fp, "fp_text"):
            if len(fp_text) > 2 and fp_text[1] == "reference":
                ref = fp_text[2] if isinstance(fp_text[2], str) else ""
                break

        pads_world: list[tuple[float, float]] = []
        for pad in _find_all(fp, "pad"):
            pad_at = _find_first(pad, "at")
            if not pad_at:
                continue
            lx = float(pad_at[1]); ly = float(pad_at[2])
            # rotate, then translate by footprint (at)
            wx = fx + lx * cos_r - ly * sin_r
            wy = fy + lx * sin_r + ly * cos_r
            bx, by = to_board(wx, wy)
            pads_world.append((bx, by))
            # default SMD pad drill = 0.8mm for DIY through-hole retrofit
            pad_holes.append((bx, by, 0.8))

        components.append({
            "ref": ref,
            "xy_mm": to_board(fx, fy),
            "pads": pads_world,
            "n_pads": len(pads_world),
        })

    # --- Copper traces (F.Cu segments only) ---
    traces: list[tuple[str, list[tuple[float, float]], float]] = []
    net_names: dict[int, str] = {0: ""}
    for net in _find_all(tree, "net"):
        if len(net) >= 3:
            try:
                net_names[int(net[1])] = str(net[2])
            except ValueError:
                pass

    for seg in _find_all(tree, "segment"):
        layer = _find_first(seg, "layer")
        if not layer or len(layer) < 2 or layer[1] != "F.Cu":
            continue
        start = _find_first(seg, "start")
        end = _find_first(seg, "end")
        width = _find_first(seg, "width")
        net = _find_first(seg, "net")
        if not (start and end):
            continue
        s_b = to_board(float(start[1]), float(start[2]))
        e_b = to_board(float(end[1]), float(end[2]))
        w_mm = float(width[1]) if width and len(width) > 1 else 0.25
        n_idx = int(net[1]) if net and len(net) > 1 else 0
        traces.append((net_names.get(n_idx, ""), [s_b, e_b], w_mm))

    # --- Mounting holes: none in kicad_pcb_writer output yet ---
    mounting_holes: list[dict] = []

    return {
        "board_size": (round(board_w, 3), round(board_h, 3)),
        "origin": (ox, oy),
        "components": components,
        "traces": traces,
        "mounting_holes": mounting_holes,
        "pad_holes": pad_holes,
    }


# ---------------------------------------------------------------------------
# Route A: CNC isolation milling
# ---------------------------------------------------------------------------

def _gcode_header(title: str, feed: float, travel_z: float) -> list[str]:
    return [
        f"; {title}",
        "; units: mm, absolute coords, origin = bottom-left top of board",
        "G21 ; mm",
        "G90 ; absolute",
        "G17 ; XY plane",
        f"G0 Z{travel_z:.3f}",
        f"F{feed:.0f}",
    ]


def _offset_segment(p0: tuple[float, float],
                    p1: tuple[float, float],
                    offset: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Offset a line segment perpendicularly by `offset` mm (+ = left of
    direction). Returns (new_p0, new_p1)."""
    dx = p1[0] - p0[0]; dy = p1[1] - p0[1]
    L = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / L, dx / L  # left-hand perpendicular
    return ((p0[0] + nx * offset, p0[1] + ny * offset),
            (p1[0] + nx * offset, p1[1] + ny * offset))


def emit_cnc_isolation_gcode(trace_data: dict,
                              out_gcode: str | Path,
                              *,
                              tool_dia_mm: float = 0.2,
                              cut_depth_mm: float = 0.08,
                              travel_z_mm: float = 1.0,
                              feed: float = 400.0) -> Path:
    """Carve isolation channels around every F.Cu trace by offsetting the
    polyline by +/-tool_dia/2 on both sides, then drill pad holes.

    Produces flat, straight-line G-code. Suitable for a hobby CNC with a
    0.1-0.3mm V-bit doing single-sided isolation milling.
    """
    out = Path(out_gcode)
    offset = tool_dia_mm / 2.0
    lines = _gcode_header("Aria-OS DIY PCB isolation milling",
                          feed, travel_z_mm)

    for net_name, polyline, _w in trace_data["traces"]:
        # cut a channel on each side of the trace
        for side in (-1.0, 1.0):
            for (p0, p1) in zip(polyline[:-1], polyline[1:]):
                o0, o1 = _offset_segment(p0, p1, offset * side)
                lines.append(f"; net={net_name} side={'L' if side<0 else 'R'}")
                lines.append(f"G0 X{o0[0]:.3f} Y{o0[1]:.3f}")
                lines.append(f"G1 Z{-cut_depth_mm:.3f}")
                lines.append(f"G1 X{o1[0]:.3f} Y{o1[1]:.3f}")
                lines.append(f"G0 Z{travel_z_mm:.3f}")

    # anchor holes at every pad (shallow divot with this same endmill so the
    # user can see where to drill by hand with the proper drill bit later)
    lines.append("; --- pad marker divots ---")
    for (x, y, _d) in trace_data["pad_holes"]:
        lines.append(f"G0 X{x:.3f} Y{y:.3f}")
        lines.append(f"G1 Z{-cut_depth_mm:.3f}")
        lines.append(f"G0 Z{travel_z_mm:.3f}")

    lines.append(f"G0 Z{travel_z_mm + 5:.3f}")
    lines.append("M5 ; spindle stop")
    lines.append("M30 ; end")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def emit_cnc_drill_gcode(trace_data: dict,
                         out_gcode: str | Path,
                         *,
                         hole_drill_dia: float = 0.8,
                         board_thickness_mm: float = 1.6,
                         travel_z_mm: float = 2.0,
                         feed: float = 150.0) -> Path:
    """Separate drill program: G0 to every pad/mounting-hole centre, plunge
    through the full board thickness, retract. Tool change expected before
    running (user swaps to a {hole_drill_dia}mm drill)."""
    out = Path(out_gcode)
    lines = _gcode_header(f"Aria-OS DIY PCB drill ({hole_drill_dia:.2f}mm)",
                          feed, travel_z_mm)
    lines.append(f"; expects tool dia = {hole_drill_dia:.2f}mm")

    holes: list[tuple[float, float]] = [(x, y) for (x, y, _d) in trace_data["pad_holes"]]
    for mh in trace_data["mounting_holes"]:
        holes.append(tuple(mh["xy"]))

    for (x, y) in holes:
        lines.append(f"G0 X{x:.3f} Y{y:.3f}")
        lines.append(f"G1 Z{-(board_thickness_mm + 0.2):.3f}")
        lines.append(f"G0 Z{travel_z_mm:.3f}")

    lines.append(f"G0 Z{travel_z_mm + 5:.3f}")
    lines.append("M5")
    lines.append("M30")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Route B: 3D-printed substrate + copper-tape inlay
# ---------------------------------------------------------------------------

def _require_cadquery():
    if not _HAVE_CQ:
        raise RuntimeError(
            "cadquery is required for STL export (printed substrate / "
            "solder-paste stencil). Install cadquery or skip route B.")


def emit_printed_substrate_stl(trace_data: dict,
                                out_stl: str | Path,
                                *,
                                channel_width_mm: float = 0.8,
                                channel_depth_mm: float = 0.5,
                                substrate_thickness_mm: float = 2.0) -> Path:
    """Rectangular slab with channels along every trace and through-holes at
    every pad. Copper foil tape is inlaid into the channels after printing.

    Each trace segment becomes a swept rectangular prism of size
    (segment_length x channel_width x channel_depth) positioned at the top
    face of the slab, then subtracted.
    """
    _require_cadquery()
    import cadquery as cq

    out = Path(out_stl)
    bw, bh = trace_data["board_size"]
    t = float(substrate_thickness_mm)

    slab = cq.Workplane("XY").box(bw, bh, t, centered=(False, False, False))

    # subtract trace channels
    for _net, polyline, _w in trace_data["traces"]:
        for p0, p1 in zip(polyline[:-1], polyline[1:]):
            dx = p1[0] - p0[0]; dy = p1[1] - p0[1]
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            angle = math.degrees(math.atan2(dy, dx))
            cx = (p0[0] + p1[0]) / 2.0
            cy = (p0[1] + p1[1]) / 2.0
            # channel sits at top of slab, depth = channel_depth
            ch = (cq.Workplane("XY")
                  .box(L, channel_width_mm, channel_depth_mm,
                       centered=True))
            ch = ch.rotate((0, 0, 0), (0, 0, 1), angle)
            ch = ch.translate((cx, cy, t - channel_depth_mm / 2.0))
            slab = slab.cut(ch)

    # through-holes at every pad and mounting hole
    for (x, y, dia) in trace_data["pad_holes"]:
        hole = (cq.Workplane("XY")
                .circle(dia / 2.0).extrude(t + 1.0)
                .translate((x, y, -0.5)))
        slab = slab.cut(hole)
    for mh in trace_data["mounting_holes"]:
        x, y = mh["xy"]; dia = float(mh.get("dia", 2.0))
        hole = (cq.Workplane("XY")
                .circle(dia / 2.0).extrude(t + 1.0)
                .translate((x, y, -0.5)))
        slab = slab.cut(hole)

    cq.exporters.export(slab, str(out), exportType="STL")
    return out


def emit_copper_tape_cut_svg(trace_data: dict,
                              out_svg: str | Path,
                              *,
                              channel_width_mm: float = 0.8) -> Path:
    """SVG for a vinyl cutter: one closed rectangular polyline per trace at
    the same width as the printed channels, plus four L-shaped register marks
    in the corners so you can align the cut sheet with the printed substrate.

    mm units: viewBox is in mm, user units are 1:1 (user draws in mm).
    """
    out = Path(out_svg)
    bw, bh = trace_data["board_size"]

    paths: list[str] = []

    def trace_rect(p0, p1, w) -> str:
        """Closed rounded-end rectangular path along segment p0->p1."""
        dx = p1[0] - p0[0]; dy = p1[1] - p0[1]
        L = math.hypot(dx, dy)
        if L < 1e-6:
            return ""
        ux, uy = dx / L, dy / L
        nx, ny = -uy, ux  # perpendicular
        half = w / 2.0
        corners = [
            (p0[0] + nx * half, p0[1] + ny * half),
            (p1[0] + nx * half, p1[1] + ny * half),
            (p1[0] - nx * half, p1[1] - ny * half),
            (p0[0] - nx * half, p0[1] - ny * half),
        ]
        # SVG: Y grows down, so flip Y using bh
        pts = " ".join(f"{cx:.3f},{bh - cy:.3f}" for (cx, cy) in corners)
        return (f'  <polygon points="{pts}" fill="#c87a2f" '
                f'stroke="black" stroke-width="0.05"/>')

    for _net, polyline, _w in trace_data["traces"]:
        for p0, p1 in zip(polyline[:-1], polyline[1:]):
            s = trace_rect(p0, p1, channel_width_mm)
            if s:
                paths.append(s)

    # register marks - L-shaped corner crosshairs, 3mm arms
    arm = 3.0
    reg = []
    for (cx, cy) in [(0, 0), (bw, 0), (bw, bh), (0, bh)]:
        svg_y = bh - cy
        reg.append(
            f'  <path d="M {cx - arm:.3f},{svg_y} L {cx + arm:.3f},{svg_y} '
            f'M {cx:.3f},{svg_y - arm:.3f} L {cx:.3f},{svg_y + arm:.3f}" '
            f'stroke="black" stroke-width="0.15" fill="none"/>')

    svg = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {bw:.3f} {bh:.3f}" '
        f'width="{bw:.3f}mm" height="{bh:.3f}mm">\n'
        f'  <title>ARIA-OS DIY copper-tape cut pattern</title>\n'
        f'  <rect x="0" y="0" width="{bw:.3f}" height="{bh:.3f}" '
        f'fill="none" stroke="black" stroke-width="0.1"/>\n'
        + "\n".join(paths) + "\n"
        + "\n".join(reg) + "\n"
        '</svg>\n'
    )
    out.write_text(svg, encoding="utf-8")
    return out


def emit_solder_paste_stencil_stl(trace_data: dict,
                                   out_stl: str | Path,
                                   *,
                                   stencil_thickness_mm: float = 0.15,
                                   aperture_dia_mm: float = 0.9) -> Path:
    """Thin plate with a circular aperture at every pad centre. A squeegee
    pushes paste through the apertures onto the PCB."""
    _require_cadquery()
    import cadquery as cq

    out = Path(out_stl)
    bw, bh = trace_data["board_size"]
    t = float(stencil_thickness_mm)

    plate = cq.Workplane("XY").box(bw, bh, t, centered=(False, False, False))

    for (x, y, _d) in trace_data["pad_holes"]:
        hole = (cq.Workplane("XY")
                .circle(aperture_dia_mm / 2.0).extrude(t + 0.2)
                .translate((x, y, -0.1)))
        plate = plate.cut(hole)

    cq.exporters.export(plate, str(out), exportType="STL")
    return out


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def run_diy_fab(kicad_pcb_path: str | Path,
                out_dir: str | Path,
                *,
                route: str = "both") -> dict:
    """Dispatch to Route A (CNC isolation + drill) and/or Route B (printed
    substrate + copper-tape + stencil). Writes everything into
    <out_dir>/diy_fab/ and returns a result dict with paths + recommendations.

    route: "cnc" | "printed" | "both"
    """
    src = Path(kicad_pcb_path)
    out_root = Path(out_dir) / "diy_fab"
    out_root.mkdir(parents=True, exist_ok=True)

    trace_data = _extract_traces_from_pcb(src)
    (out_root / "trace_data.json").write_text(
        json.dumps(_trace_data_jsonable(trace_data), indent=2),
        encoding="utf-8")

    result: dict[str, Any] = {
        "source_pcb": str(src),
        "out_dir": str(out_root),
        "board_size_mm": trace_data["board_size"],
        "n_components": len(trace_data["components"]),
        "n_traces": len(trace_data["traces"]),
        "n_pad_holes": len(trace_data["pad_holes"]),
        "route": route,
        "paths": {},
        "recommendations": {
            "cnc_tool": "0.1-0.3mm V-bit, 30deg, carbide",
            "cnc_stock": "single-sided 1.6mm FR1 phenolic or FR4",
            "cnc_feed_mm_min": 400,
            "printed_material": "PLA or PETG, 0.2mm layer, 100% infill",
            "copper_tape": "3M 1181 conductive copper foil tape (0.8mm width)",
            "stencil_material": "0.15mm PETG, single perimeter, no infill",
        },
    }

    if route in ("cnc", "both"):
        iso = emit_cnc_isolation_gcode(
            trace_data, out_root / "isolation.gcode")
        drill = emit_cnc_drill_gcode(
            trace_data, out_root / "drill.gcode")
        result["paths"]["isolation_gcode"] = str(iso)
        result["paths"]["drill_gcode"] = str(drill)

    if route in ("printed", "both"):
        svg = emit_copper_tape_cut_svg(
            trace_data, out_root / "copper_tape.svg")
        result["paths"]["copper_tape_svg"] = str(svg)
        if _HAVE_CQ:
            sub = emit_printed_substrate_stl(
                trace_data, out_root / "substrate.stl")
            sten = emit_solder_paste_stencil_stl(
                trace_data, out_root / "stencil.stl")
            result["paths"]["substrate_stl"] = str(sub)
            result["paths"]["stencil_stl"] = str(sten)
        else:
            result["paths"]["substrate_stl"] = None
            result["paths"]["stencil_stl"] = None
            result["recommendations"]["cadquery_warning"] = (
                "cadquery unavailable - STL outputs skipped")

    (out_root / "manifest.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    return result


def _trace_data_jsonable(td: dict) -> dict:
    """Render tuples as lists for JSON serialisation."""
    return {
        "board_size": list(td["board_size"]),
        "origin": list(td["origin"]),
        "components": [
            {**c, "xy_mm": list(c["xy_mm"]),
             "pads": [list(p) for p in c["pads"]]}
            for c in td["components"]
        ],
        "traces": [
            [net, [list(p) for p in poly], w]
            for (net, poly, w) in td["traces"]
        ],
        "mounting_holes": td["mounting_holes"],
        "pad_holes": [list(h) for h in td["pad_holes"]],
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python -m aria_os.ecad.diy_fab <board.kicad_pcb> "
              "<out_dir> [cnc|printed|both]")
        sys.exit(1)
    route = sys.argv[3] if len(sys.argv) > 3 else "both"
    r = run_diy_fab(sys.argv[1], sys.argv[2], route=route)
    print(json.dumps(r, indent=2))
