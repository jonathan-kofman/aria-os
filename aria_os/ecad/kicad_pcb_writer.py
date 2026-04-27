"""
Direct .kicad_pcb file writer — produces a fabricable KiCad PCB file from
a BOM JSON without requiring KiCad to be installed.

Why: ecad_generator.py has produced Python scripts you'd run inside KiCad's
pcbnew to materialize a board. That's awkward — the user wants the actual
.kicad_pcb file they can open in KiCad / send to a fab house. This module
emits the KiCad 7+ s-expression format directly.

Output format reference:
  https://dev-docs.kicad.org/en/file-formats/sexpr-pcb/

Limitations (vs hand-designed PCB):
  - Footprints are minimal placeholders (rectangle outline + pad grid).
    They're real KiCad footprints (you can move/rotate them), just not
    pulled from KiCad's libraries.
  - Traces are star-routed per net (MCU or first pad -> all other pads
    on the same net) as straight F.Cu segments. No DRC clearance check,
    no via insertion, no layer balancing. Good enough for fabs to
    accept the board; re-route manually for production.
  - No silkscreen text beyond reference designators.

Usage:
  from aria_os.ecad.kicad_pcb_writer import write_kicad_pcb
  write_kicad_pcb(bom_path, out_pcb_path, board_name="my_board")
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


# KiCad 9/10 layer stack — the layer ordinals were remapped in KiCad 9
# from the legacy KiCad 7 numbering. Notably F.Cu=0 still, but B.Cu=2
# (was 31). DRC/Gerber export in KiCad 10 rejects boards using the
# old numbering with a generic "Failed to load board" error. Renderers
# (SVG export) are lenient and accept either, which is why we didn't
# notice until trying gerbers.
#
# Reference: a fresh empty board saved by KiCad 10.0.1.
_LAYERS_2L = """\
    (layers
        (0 "F.Cu" signal)
        (2 "B.Cu" signal)
        (9 "F.Adhes" user "F.Adhesive")
        (11 "B.Adhes" user "B.Adhesive")
        (13 "F.Paste" user)
        (15 "B.Paste" user)
        (5 "F.SilkS" user "F.Silkscreen")
        (7 "B.SilkS" user "B.Silkscreen")
        (1 "F.Mask" user)
        (3 "B.Mask" user)
        (17 "Dwgs.User" user "User.Drawings")
        (19 "Cmts.User" user "User.Comments")
        (21 "Eco1.User" user "User.Eco1")
        (23 "Eco2.User" user "User.Eco2")
        (25 "Edge.Cuts" user)
        (27 "Margin" user)
        (31 "F.CrtYd" user "F.Courtyard")
        (29 "B.CrtYd" user "B.Courtyard")
        (35 "F.Fab" user)
        (33 "B.Fab" user)
        (39 "User.1" user)
        (41 "User.2" user)
        (43 "User.3" user)
        (45 "User.4" user)
        (47 "User.5" user)
    )"""


# 4-layer stack — KiCad 9/10 numbering. F.Cu=0, In1.Cu=4, In2.Cu=6,
# B.Cu=2.
_LAYERS_4L = """\
    (layers
        (0 "F.Cu" signal)
        (4 "In1.Cu" power "GND")
        (6 "In2.Cu" power "PWR")
        (2 "B.Cu" signal)
        (9 "F.Adhes" user "F.Adhesive")
        (11 "B.Adhes" user "B.Adhesive")
        (13 "F.Paste" user)
        (15 "B.Paste" user)
        (5 "F.SilkS" user "F.Silkscreen")
        (7 "B.SilkS" user "B.Silkscreen")
        (1 "F.Mask" user)
        (3 "B.Mask" user)
        (17 "Dwgs.User" user "User.Drawings")
        (19 "Cmts.User" user "User.Comments")
        (21 "Eco1.User" user "User.Eco1")
        (23 "Eco2.User" user "User.Eco2")
        (25 "Edge.Cuts" user)
        (27 "Margin" user)
        (31 "F.CrtYd" user "F.Courtyard")
        (29 "B.CrtYd" user "B.Courtyard")
        (35 "F.Fab" user)
        (33 "B.Fab" user)
    )"""


# Net-class table: (class_name) -> (trace_width_mm, clearance_mm, via_dia_mm, via_drill_mm).
# Widths derived from IPC-2221 for typical 4-layer 0.5 oz copper
# (external) + 1 oz (internal). 20°C ambient, 10°C rise.
#   Power:  10A @ 20°C rise ~= 0.5mm (2mil) — wide traces carry supply currents
#   Signal: <100mA default — standard 0.2mm (8mil) for ease of routing
#   HS:    differential-pair-capable 0.15mm/0.2mm with 0.1mm clearance
#   GND/net 0: same as power for safety; pour is separate
_NET_CLASS_DEFAULTS = {
    "Power":   {"width": 0.5,  "clearance": 0.2,  "via_dia": 0.8,  "via_drill": 0.4},
    "Signal":  {"width": 0.2,  "clearance": 0.15, "via_dia": 0.6,  "via_drill": 0.3},
    "HS_Diff": {"width": 0.15, "clearance": 0.1,  "via_dia": 0.45, "via_drill": 0.2},
    "Default": {"width": 0.25, "clearance": 0.2,  "via_dia": 0.6,  "via_drill": 0.3},
}

# Keyword → net class. Matched in order; first hit wins.
_NET_CLASS_PATTERNS: list[tuple[tuple[str, ...], str]] = [
    (("GND", "AGND", "DGND", "SHLD", "SHIELD"),            "Power"),
    (("VBAT", "VIN", "VBUS", "+3V3", "+5V", "+12V",
      "VCC", "VDD", "+VCC", "+VDD", "-5V", "-12V"),         "Power"),
    (("USB_DP", "USB_DM", "USB+", "USB-",
      "CLK", "XTAL", "HSE", "LSE",
      "MIPI_", "HDMI_", "PCIE_", "DDR_"),                    "HS_Diff"),
]


def _classify_net(name: str) -> str:
    """Map a net name to a net class. Unknown nets fall through to 'Signal'."""
    up = name.upper().strip()
    for patterns, cls in _NET_CLASS_PATTERNS:
        for p in patterns:
            if up == p or up.startswith(p) or p in up:
                return cls
    return "Signal"


def _net_classes_sexpr(nets: list[str]) -> str:
    """Emit per-class net_class declarations inside (setup). Each class
    lists the nets that belong to it — KiCad DRC applies the class's
    trace width + clearance when checking those nets."""
    by_class: dict[str, list[str]] = {}
    for n in nets:
        by_class.setdefault(_classify_net(n), []).append(n)

    # KiCad 7+ uses `(net_class "name" "descr" (clearance X) (trace_width Y) ...)`.
    # Emit "Default" class first even if empty (KiCad requires it).
    blocks: list[str] = []
    # Default goes first
    d = _NET_CLASS_DEFAULTS["Default"]
    default_nets = by_class.get("Default", [])
    block = [
        '    (net_class "Default" "generic nets"',
        f'      (clearance {d["clearance"]})',
        f'      (trace_width {d["width"]})',
        f'      (via_dia {d["via_dia"]})',
        f'      (via_drill {d["via_drill"]})',
        f'      (uvia_dia {d["via_dia"]})',
        f'      (uvia_drill {d["via_drill"]})',
    ]
    for n in default_nets:
        block.append(f'      (add_net "{n}")')
    block.append("    )")
    blocks.append("\n".join(block))

    for cls in ("Power", "HS_Diff", "Signal"):
        members = by_class.get(cls, [])
        if not members: continue
        d = _NET_CLASS_DEFAULTS[cls]
        block = [
            f'    (net_class "{cls}" "{cls} nets"',
            f'      (clearance {d["clearance"]})',
            f'      (trace_width {d["width"]})',
            f'      (via_dia {d["via_dia"]})',
            f'      (via_drill {d["via_drill"]})',
            f'      (uvia_dia {d["via_dia"]})',
            f'      (uvia_drill {d["via_drill"]})',
        ]
        for n in members:
            block.append(f'      (add_net "{n}")')
        block.append("    )")
        blocks.append("\n".join(block))
    return "\n".join(blocks)


def write_kicad_pcb(
    bom_path: str | Path,
    out_pcb_path: str | Path | None = None,
    *,
    board_name: str | None = None,
    pcb_thk_mm: float = 1.6,
    n_layers: int = 2,
) -> Path:
    """Write a real .kicad_pcb file (s-expression format) from a BOM JSON.

    Returns the path to the written .kicad_pcb file.
    """
    bom_path = Path(bom_path)
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    components = bom.get("components", []) or []
    board_w = float(bom.get("board_w_mm", 0)) or _infer_board(components, "w")
    board_h = float(bom.get("board_h_mm", 0)) or _infer_board(components, "h")
    name = board_name or bom.get("board_name") or bom_path.stem

    if out_pcb_path is None:
        out_pcb_path = bom_path.parent / f"{name}.kicad_pcb"
    out_pcb_path = Path(out_pcb_path)

    # KiCad places origin at the top-left of the page. Board coords in BOM
    # are 0-based at lower-left (math convention). We translate Y so that
    # KiCad-Y = page_offset_mm + (board_h - bom_y).
    page_x_mm = 100.0  # offset of board on the KiCad page
    page_y_mm = 100.0

    nets = _collect_nets(components)
    # net index 0 is reserved for "" (unconnected) in KiCad, so real nets
    # start at index 1. The (net 0 "") entry is emitted in the template.
    net_index = {name: i + 1 for i, name in enumerate(nets)}
    net_lines = "\n".join(
        f'    (net {i + 1} "{n}")' for i, n in enumerate(nets)
    )

    footprint_blocks = []
    # Track pad world positions per component so we can route traces.
    # component_pad_positions[ref] = [(x_world, y_world), ...]
    component_pad_positions: dict[str, list[tuple[float, float]]] = {}
    for c in components:
        ref = str(c.get("ref", "?"))
        value = str(c.get("value", ""))
        x = float(c.get("x_mm", 0))
        y = float(c.get("y_mm", 0))
        w = float(c.get("width_mm", 1))
        h = float(c.get("height_mm", 1))
        rotation = float(c.get("rotation_deg", 0))
        footprint_field = str(c.get("footprint", "Generic:Generic"))

        # Translate to KiCad page coords (Y inverted)
        kx = page_x_mm + x + w / 2.0
        ky = page_y_mm + (board_h - y) - h / 2.0

        # Resolve per-pad nets. Priority:
        #   1. c["net_map"] — explicit pad_num → net_name mapping (preferred)
        #   2. c["nets"]    — distribute nets across pads in order (connectors)
        # When neither is present pads stay unassigned (DRC will flag them).
        comp_nets = c.get("nets") or []
        net_map = c.get("net_map") or {}
        n_pads_hint = _resolve_pad_count(c, w)

        # Real KiCad footprint path. Opt out via ARIA_USE_REAL_FOOTPRINTS=0
        # to fall back to the minimal placeholder pads (older behaviour;
        # useful when debugging the placer or when KiCad's bundled
        # footprints aren't installed). Default is ON now that the
        # downstream STEP/GLB exports actually need real geometry to
        # feed the assembler.
        #
        # Known issue: real footprints have larger courtyards than the
        # minimal placeholders, so components placed too tightly will
        # overlap and DRC will report courtyard violations. The placer
        # spaces by w/h from the BOM though, which generally matches
        # the real footprint outline within ±20%. For the smoke-test
        # LED demo the overlap doesn't materialise; for dense boards
        # we may need a courtyard-aware placer (TODO).
        import os as _os
        _use_real_fp = _os.environ.get("ARIA_USE_REAL_FOOTPRINTS", "1") != "0"
        real_fp_block, real_pad_positions = (
            _try_real_footprint(
                value=value, footprint_field=footprint_field,
                ref=ref, kx=kx, ky=ky, rotation=rotation,
                net_map=net_map, comp_nets=comp_nets, net_index=net_index)
            if _use_real_fp else (None, []))
        if real_fp_block is not None:
            footprint_blocks.append(real_fp_block)
            component_pad_positions[ref] = real_pad_positions
        else:
            footprint_blocks.append(
                _build_footprint_sexpr(ref, value, footprint_field,
                                       kx, ky, rotation, w, h,
                                       n_pads=n_pads_hint,
                                       net_map=net_map, comp_nets=comp_nets,
                                       net_index=net_index))
            component_pad_positions[ref] = _compute_pad_world_positions(
                kx, ky, w, h, n_pads=n_pads_hint)

    trace_block = _build_traces_sexpr(components, nets, net_index,
                                      component_pad_positions)

    # Board outline rectangle on Edge.Cuts layer
    edge_cuts = _build_edge_cuts(page_x_mm, page_y_mm, board_w, board_h)

    # NPTH mounting holes at the 4 corners (M3 default).
    mounting_holes = _build_mounting_holes(page_x_mm, page_y_mm,
                                             board_w, board_h)

    # Final s-expression
    timestamp = int(time.time())
    pcb = f'''(kicad_pcb
    (version 20240108)
    (generator "aria_os.kicad_pcb_writer")
    (general
        (thickness {pcb_thk_mm:.2f})
    )
    (paper "A4")
    (title_block
        (title "{name}")
        (date "{time.strftime('%Y-%m-%d')}")
        (rev "1.0")
        (company "ARIA-OS")
        (comment 1 "Generated by aria_os/ecad/kicad_pcb_writer.py")
    )
{(_LAYERS_4L if n_layers >= 4 else _LAYERS_2L)}
    (setup
        (pad_to_mask_clearance 0.051)
        (solder_mask_min_width 0.05)
        (pcbplotparams
            (layerselection 0x00010fc_ffffffff)
            (disableapertmacros false)
            (usegerberextensions false)
            (usegerberattributes true)
            (usegerberadvancedattributes true)
            (creategerberjobfile true)
            (svguseinch false)
            (svgprecision 6)
            (excludeedgelayer true)
            (plotframeref false)
            (viasonmask false)
            (mode 1)
            (useauxorigin false)
            (hpglpennumber 1)
            (hpglpenspeed 20)
            (hpglpendiameter 15.000000)
            (dxfpolygonmode true)
            (dxfimperialunits true)
            (dxfusepcbnewfont true)
            (psnegative false)
            (psa4output false)
            (plotreference true)
            (plotvalue true)
            (plotinvisibletext false)
            (sketchpadsonfab false)
            (subtractmaskfromsilk false)
            (outputformat 1)
            (mirror false)
            (drillshape 1)
            (scaleselection 1)
            (outputdirectory "gerbers/")
        )
    )
    (net 0 "")
{net_lines}
{edge_cuts}
{mounting_holes}
{chr(10).join(footprint_blocks)}
{trace_block}
)
'''
    # Belt-and-braces sanitizer: KiCad 10's S-expression parser fails
    # silently with "Failed to load board" if any stray ";;" line comment
    # leaks into the file (an older code path here used to emit one for
    # the "no traces" case). Strip them before write so a future
    # regression in any sub-builder cannot reintroduce the bug.
    pcb = "\n".join(
        line for line in pcb.splitlines()
        if not line.lstrip().startswith(";"))
    out_pcb_path.write_text(pcb, encoding="utf-8")
    return out_pcb_path


def _try_real_footprint(*, value: str, footprint_field: str, ref: str,
                        kx: float, ky: float, rotation: float,
                        net_map: dict, comp_nets: list,
                        net_index: dict) -> tuple[str | None, list]:
    """Attempt to resolve a real KiCad footprint via kicad_footprint_lib,
    rewrite it for board embedding, and return (footprint_block, pad_xy_list).

    Returns (None, []) if lookup fails or KiCad isn't installed — the
    caller then falls back to the minimal placeholder footprint.
    """
    try:
        from .kicad_footprint_lib import lookup_footprint, load_footprint_sexpr
    except Exception:
        return None, []

    # footprint_field from the BOM looks like "Package_QFP:LQFP-64_10x10mm_P0.5mm"
    # — split on ':' and use the right side as the package hint.
    pkg_hint = footprint_field.split(":", 1)[-1].strip() if footprint_field else ""
    fp_meta = lookup_footprint(value, package=pkg_hint or None)
    if fp_meta is None:
        return None, []

    raw = load_footprint_sexpr(fp_meta["path"], fp_meta["fp"])
    if raw is None:
        return None, []

    return _embed_real_footprint(
        raw_fp_text=raw, library_name=fp_meta["lib"], fp_name=fp_meta["fp"],
        ref=ref, value=value, kx=kx, ky=ky, rotation=rotation,
        net_map=net_map, comp_nets=comp_nets, net_index=net_index)


def _embed_real_footprint(*, raw_fp_text: str, library_name: str, fp_name: str,
                          ref: str, value: str, kx: float, ky: float,
                          rotation: float,
                          net_map: dict, comp_nets: list,
                          net_index: dict) -> tuple[str | None, list]:
    """Rewrite a `.kicad_mod` footprint block for embedding in a .kicad_pcb:
      - top-level tag: (footprint "lib:name" ...) with (at kx ky rot) on the
        parent (positions footprint on the board)
      - set Reference + Value properties
      - inject (net N "name") into each (pad "X" ...) block based on net_map
      - preserve everything else (pad geometry, silkscreen, courtyard, fab)

    Returns (rewritten_sexpr_string, world_pad_positions).
    """
    import re as _re

    text = raw_fp_text
    # Swap the top-level (footprint "NAME" ... to reference lib:name and ensure
    # tstamp/tedit + (at X Y R). The raw text starts with (footprint "NAME"\n
    # so we normalise the header then prepend placement.
    # 1. Extract the tag-name chunk up to the first '(' inside the footprint
    m = _re.match(r'^\s*\(footprint\s+"([^"]+)"', text)
    if not m:
        return None, []
    text = text[:m.end()] + "\n" + text[m.end():]
    # Insert placement after the name
    placement = (f'        (layer "F.Cu")\n'
                 f'        (tstamp {uuid4()})\n'
                 f'        (tedit {int(time.time()):X})\n'
                 f'        (at {kx:.3f} {ky:.3f} {rotation:.1f})\n')
    text = text[:m.end() + 1] + placement + text[m.end() + 1:]
    # Guard against duplicate layer declarations KiCad 10 might ship with
    text = _re.sub(r'\n\s*\(layer\s+"F\.Cu"\)\n',
                   "\n", text, count=1)  # drop a second F.Cu layer line

    # Reference + Value: fill the blanks left by load_footprint_sexpr
    text = _re.sub(r'(\(property\s+"Reference"\s+)""',
                   f'\\1"{ref}"', text, count=1)
    text = _re.sub(r'(\(property\s+"Value"\s+)""',
                   f'\\1"{value}"', text, count=1)

    # Apply per-footprint solder mask margin override. KiCad's default
    # mask aperture (~0.05mm) bridges between adjacent QFP/QFN pads on
    # tight pitches (was 212 solder_mask_bridge DRC errors on the v4 FC).
    # Setting the FP-level margin to 0.0 = mask aperture exactly equals
    # pad copper, so no bridging.
    if not _re.search(r'\(solder_mask_margin\s+', text):
        # Inject right after the (at ...) block we placed
        text = _re.sub(
            r'(\(at\s+[\d.\-]+\s+[\d.\-]+\s+[\d.\-]+\)\n)',
            r'\1        (solder_mask_margin 0)\n',
            text, count=1)

    # Inject (net N "name") AND (solder_mask_margin 0) into each
    # (pad "X" smd|thru_hole ... ) block. KiCad pad format:
    #   (pad "1" smd rect (at ...) (size ...) (layers ...) [other props] )
    # We append the missing fragments just before the closing ')'.
    #
    # The solder_mask_margin override is critical: many KiCad library
    # footprints (QFN/QFP for the MCU, IMU, baro sensors) have tightly
    # spaced pads and KiCad's default mask aperture (0.05 mm) merges
    # adjacent apertures, producing ~100+ solder_mask_bridge DRC errors
    # per dense board. Setting per-pad mask margin to 0 makes the mask
    # opening exactly equal the pad copper, eliminating bridges.
    def _rewrite_pad(match: _re.Match) -> str:
        pad_num_s = match.group(1)
        body = match.group(2)
        injects: list[str] = []

        # Net injection — only if not already netted
        if not _re.search(r'\(net\s+\d+\s+', body):
            try:
                pad_num = int(pad_num_s)
                net_frag = _net_sexpr_for_pad(
                    pad_num, net_map, comp_nets, net_index).strip()
                if net_frag:
                    injects.append(net_frag)
            except ValueError:
                pass  # non-numeric pad ref like "MH" — no net assignment

        # Mask-margin injection — only if pad doesn't already have one
        if not _re.search(r'\(solder_mask_margin\s+', body):
            injects.append("(solder_mask_margin 0)")

        if not injects:
            return match.group(0)
        return f'(pad "{pad_num_s}"{body} {" ".join(injects)})'

    text = _re.sub(
        r'\(pad\s+"([^"]+)"((?:(?:\([^()]*\))|[^()])*?)\)',
        _rewrite_pad, text)

    # Extract pad world positions for the trace router.
    pad_positions: list[tuple[float, float]] = []
    for pm in _re.finditer(
            r'\(pad\s+"[^"]+"[\s\S]*?\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+[-\d.]+)?\)',
            text):
        px = float(pm.group(1))
        py = float(pm.group(2))
        # These are footprint-local; translate to world
        # (rotation handling: apply rot around (kx, ky))
        if abs(rotation) > 0.01:
            import math as _m
            rad = _m.radians(rotation)
            c_, s_ = _m.cos(rad), _m.sin(rad)
            wx = kx + (px * c_ - py * s_)
            wy = ky + (px * s_ + py * c_)
        else:
            wx, wy = kx + px, ky + py
        pad_positions.append((wx, wy))

    return text, pad_positions


def _resolve_pad_count(c: dict, w_mm: float) -> int:
    """Determine how many pads a component should emit.

    Priority:
      1. explicit c["pad_count"] > 0
      2. length of c["net_map"] (per-pin net assignment)
      3. length of c["nets"] (useful for connectors that ship a flat net list)
      4. size-based default (bigger body = more pads, up to 8 columns)
    """
    pc = int(c.get("pad_count") or 0)
    if pc > 0:
        return pc
    net_map = c.get("net_map") or {}
    if net_map:
        return len(net_map)
    nets = c.get("nets") or []
    if nets:
        return len(nets)
    return max(2, min(8, int(w_mm / 3.0)))


def _net_sexpr_for_pad(pad_num: int, net_map: dict,
                       comp_nets: list, net_index: dict) -> str:
    """Return the `(net N "name")` fragment for a pad. Resolution order:

      1. net_map[pad_num]   — explicit per-pin assignment
      2. comp_nets[pad_num - 1] — flat list (XT60-style connectors)
      3. ''                 — no assignment; pad stays unconnected

    Returning '' (no fallback to GND) is intentional: defaulting to GND
    would short non-GND pins of components like regulators to the ground
    pour. DRC reporting an unconnected pad is the correct signal that
    the LLM-emitted netlist is incomplete — better than silent shorts.
    """
    key_str = str(pad_num)
    net_name = None
    if net_map:
        net_name = net_map.get(key_str) or net_map.get(pad_num)
    if net_name is None and comp_nets:
        idx = pad_num - 1
        if 0 <= idx < len(comp_nets):
            net_name = comp_nets[idx]
    if not net_name:
        return ""
    idx = net_index.get(net_name)
    if idx is None:
        return ""
    return f' (net {idx} "{net_name}")'


def _build_footprint_sexpr(ref: str, value: str, footprint_field: str,
                           kx: float, ky: float, rotation: float,
                           w: float, h: float,
                           *,
                           n_pads: int | None = None,
                           net_map: dict | None = None,
                           comp_nets: list | None = None,
                           net_index: dict | None = None) -> str:
    """Build a minimal but valid KiCad footprint s-expression.

    Each pad emits a `(net N "name")` tag so DRC doesn't flag every
    track-over-pad as a short between a named net and `<no net>`.

    Pad-count heuristic:
      - Use `n_pads` if provided (resolved upstream by _resolve_pad_count)
      - Otherwise fall back to a size-based default (2..8 pads wide).
    """
    fp_uuid = str(uuid4())
    if n_pads is None or n_pads <= 0:
        n_pads = max(2, min(8, int(w / 3.0)))
    pad_pitch_x = max(2.0, w / (n_pads + 1))
    pad_y = h * 0.3
    net_map = net_map or {}
    comp_nets = comp_nets or []
    net_index = net_index or {}

    pad_lines = []
    # Per-pad solder mask margin override. The KiCad default is ~0.05mm
    # which fails DRC on tight pad spacing (mask apertures merge). 0.0
    # here makes each pad's mask aperture exactly match the pad — eliminates
    # the "Front solder mask aperture bridges items with different nets"
    # DRC errors that dominated the 60x60 v4 board.
    mask_margin = 0.0
    for i in range(n_pads):
        pad_num = i + 1
        px = -w / 2.0 + pad_pitch_x * (i + 1)
        net_frag = _net_sexpr_for_pad(pad_num, net_map, comp_nets, net_index)
        for sign in (-1, 1):
            py = sign * pad_y
            pad_lines.append(
                f'        (pad "{pad_num}" smd rect '
                f'(at {px:.3f} {py:.3f}) '
                f'(size 1.0 0.6) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") '
                f'(solder_mask_margin {mask_margin:.3f}){net_frag})'
            )

    return f'''    (footprint "{footprint_field}"
        (layer "F.Cu")
        (tedit {int(time.time()):X})
        (tstamp {fp_uuid})
        (at {kx:.3f} {ky:.3f} {rotation:.1f})
        (descr "auto-generated by aria_os")
        (attr smd)
        (fp_text reference "{ref}" (at 0 {-h/2 - 1.0:.3f}) (layer "F.SilkS")
            (effects (font (size 0.8 0.8) (thickness 0.15)))
            (tstamp {uuid4()}))
        (fp_text value "{value}" (at 0 {h/2 + 1.0:.3f}) (layer "F.Fab")
            (effects (font (size 0.6 0.6) (thickness 0.12)))
            (tstamp {uuid4()}))
        (fp_rect (start {-w/2:.3f} {-h/2:.3f}) (end {w/2:.3f} {h/2:.3f})
            (stroke (width 0.1) (type default)) (fill none) (layer "F.SilkS")
            (tstamp {uuid4()}))
        (fp_rect (start {-w/2 - 0.25:.3f} {-h/2 - 0.25:.3f})
                 (end {w/2 + 0.25:.3f} {h/2 + 0.25:.3f})
            (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd")
            (tstamp {uuid4()}))
{chr(10).join(pad_lines)}
    )'''


def _compute_pad_world_positions(kx: float, ky: float,
                                 w: float, h: float,
                                 *, n_pads: int | None = None
                                 ) -> list[tuple[float, float]]:
    """Recompute the SMD pad centers used inside _build_footprint_sexpr and
    translate them from footprint-local coords into board/world coords.

    Mirrors the pad layout rule in _build_footprint_sexpr. Accepts an
    explicit n_pads so connectors (pad_count=0 but nets present) line up
    the same way the footprint does.
    """
    if n_pads is None or n_pads <= 0:
        n_pads = max(2, min(8, int(w / 3.0)))
    pad_pitch_x = max(2.0, w / (n_pads + 1))
    pad_y = h * 0.3
    positions: list[tuple[float, float]] = []
    for i in range(n_pads):
        px = -w / 2.0 + pad_pitch_x * (i + 1)
        for sign in (-1, 1):
            py = sign * pad_y
            positions.append((kx + px, ky + py))
    return positions


def _build_traces_sexpr(components: list,
                        nets: list[str],
                        net_index: dict[str, int],
                        component_pad_positions: dict[str, list[tuple[float, float]]]
                        ) -> str:
    """Emit (segment ...) s-expressions for every net, star-routed.

    For each net we pick a "hub" component (the MCU U1 if it is connected
    to this net; otherwise the first component on the net) and draw a
    straight F.Cu segment from the hub's first pad to the first pad of
    every other component on the same net.

    Fallback when the BOM has no per-component net assignments (common in
    the current ARIA-OS output — the "nets" arrays are empty): each
    default net (GND, +3V3, +5V, VBAT) is connected to every component by
    routing from the hub's first pad to each component's first pad. That
    gives the fab a routable copper pattern even though the true netlist
    is unknown.

    Caveats — intentional, documented limitations:
      * No DRC clearance check. Traces WILL cross component bodies and
        each other. KiCad will flag clearance violations.
      * No via insertion — everything is routed on F.Cu only.
      * No layer balancing, no length matching, no differential pairs.
      * Star topology, not optimal; a real router would use MST / A*.

    Goal: produce non-zero copper so the board is not rejected for being
    trace-less. This is "boards have copper now", not "boards are well
    routed". A human or proper autorouter should re-route before fab.
    """
    # NB: KiCad 10's S-expression parser rejects ";;" line comments —
    # any "Failed to load board" error from kicad-cli traces back here
    # if a comment leaked into the file. Return empty strings instead.
    if not components or not nets:
        return ""

    # Naive star routing produces ~200 solder_mask_bridge DRC errors as
    # straight segments cross pads of unrelated nets. Default OFF; the
    # GND pour + per-pad net assignments are enough for the fab to
    # autoroute properly. Set ARIA_EMIT_TRACES=1 to keep the legacy
    # behaviour for visual rats-nest debugging.
    import os as _os
    if _os.environ.get("ARIA_EMIT_TRACES", "0") != "1":
        return ""

    # Build per-net pad lists: [(ref, (x, y)), ...]
    # Use explicit per-component net assignments when present; otherwise
    # connect every component's first pad to every net (fallback).
    explicit_assignments: dict[str, list[tuple[str, tuple[float, float]]]] = {
        n: [] for n in nets
    }
    have_any_explicit = False
    for c in components:
        ref = str(c.get("ref", "?"))
        pads = component_pad_positions.get(ref, [])
        if not pads:
            continue
        comp_nets = c.get("nets") or []
        if comp_nets:
            have_any_explicit = True
            for n in comp_nets:
                n = str(n)
                if n in explicit_assignments:
                    explicit_assignments[n].append((ref, pads[0]))

    if have_any_explicit:
        net_to_endpoints = explicit_assignments
    else:
        # No explicit netlist provided. The historical fallback was to
        # connect every component's pad 1 to every default net, which
        # produces a board with COPPER but also produces hundreds of
        # DRC shorts (e.g. +3V3 and PWM3 shorted at the MCU pin).
        # Better to ship a board with NO traces than a board with bad
        # traces — the user/router can add real routing later. Tracks
        # are skipped entirely; only the GND copper pour (added
        # downstream as a zone) provides any inter-component net.
        net_to_endpoints = {}

    lines: list[str] = []
    for net_name, endpoints in net_to_endpoints.items():
        if len(endpoints) < 2:
            continue
        nidx = net_index.get(net_name, 0)
        # Pick U1 (MCU) as hub if it's on this net, else first endpoint.
        hub_idx = 0
        for i, (ref, _) in enumerate(endpoints):
            if ref.upper() == "U1":
                hub_idx = i
                break
        hub_ref, hub_xy = endpoints[hub_idx]
        for j, (ref, xy) in enumerate(endpoints):
            if j == hub_idx:
                continue
            sx, sy = hub_xy
            ex, ey = xy
            lines.append(
                f'    (segment (start {sx:.3f} {sy:.3f}) '
                f'(end {ex:.3f} {ey:.3f}) '
                f'(width 0.25) (layer "F.Cu") '
                f'(net {nidx}) (tstamp {uuid4()}))'
            )
    if not lines:
        return ""
    return "\n".join(lines)


def _build_edge_cuts(x0: float, y0: float, w: float, h: float) -> str:
    """Board outline as 4 line segments on Edge.Cuts layer (KiCad fab requirement)."""
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h), (x0, y0)]
    lines = []
    for (sx, sy), (ex, ey) in zip(pts[:-1], pts[1:]):
        lines.append(
            f'    (gr_line (start {sx:.3f} {sy:.3f}) (end {ex:.3f} {ey:.3f}) '
            f'(layer "Edge.Cuts") (width 0.15) (tstamp {uuid4()}))'
        )
    return "\n".join(lines)


def _build_mounting_holes(page_x_mm: float, page_y_mm: float,
                           board_w: float, board_h: float,
                           *,
                           inset: float = 3.5,
                           hole_d: float = 3.2,
                           pad_d: float = 6.0,
                           plated: bool = False) -> str:
    """Emit four corner mounting holes as KiCad footprints. Default M3
    (3.2mm clearance / 6mm pad). NPTH (no plating) for mechanical-only
    holes. The footprints have no nets, so they don't trigger DRC short
    errors against any pour.
    """
    holes = []
    positions = [
        (page_x_mm + inset,             page_y_mm + inset),
        (page_x_mm + board_w - inset,   page_y_mm + inset),
        (page_x_mm + board_w - inset,   page_y_mm + board_h - inset),
        (page_x_mm + inset,             page_y_mm + board_h - inset),
    ]
    pad_layers = '"*.Cu" "*.Mask"' if plated else '"F.Mask" "B.Mask" "Edge.Cuts"'
    pad_type = "thru_hole" if plated else "np_thru_hole"
    for i, (cx, cy) in enumerate(positions, start=1):
        ref = f"H{i}"
        holes.append(f'''    (footprint "MountingHole:MountingHole_3.2mm_M3"
        (layer "F.Cu")
        (tstamp {uuid4()})
        (at {cx:.3f} {cy:.3f})
        (descr "auto-generated mounting hole")
        (attr {('through_hole' if plated else 'exclude_from_pos_files exclude_from_bom')})
        (fp_text reference "{ref}" (at 0 -3.5) (layer "F.SilkS")
            (effects (font (size 0.8 0.8) (thickness 0.15)))
            (tstamp {uuid4()}))
        (fp_text value "M3" (at 0 3.5) (layer "F.Fab")
            (effects (font (size 0.6 0.6) (thickness 0.12)))
            (tstamp {uuid4()}))
        (fp_circle (center 0 0) (end {pad_d/2:.3f} 0)
            (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd")
            (tstamp {uuid4()}))
        (pad "" {pad_type} circle (at 0 0) (size {pad_d:.3f} {pad_d:.3f})
            (drill {hole_d:.3f}) (layers {pad_layers}))
    )''')
    return "\n".join(holes)


def _collect_nets(components: list) -> list[str]:
    """Collect unique net names from component pin assignments."""
    seen = set()
    nets = []
    for c in components:
        for net in (c.get("nets") or []):
            n = str(net)
            if n and n not in seen:
                seen.add(n)
                nets.append(n)
    if not nets:
        # Standard nets so the file is non-trivial
        nets = ["GND", "+3V3", "+5V", "VBAT"]
    return nets


def _infer_board(components: list, axis: str) -> float:
    if not components:
        return 50.0
    if axis == "w":
        return max((float(c.get("x_mm", 0)) + float(c.get("width_mm", 0))
                    for c in components), default=50.0) + 2.0
    return max((float(c.get("y_mm", 0)) + float(c.get("height_mm", 0))
                for c in components), default=50.0) + 2.0


# ---------------------------------------------------------------------------
# Optional: Gerber export via kicad-cli (if installed)
# ---------------------------------------------------------------------------

def export_gerbers(kicad_pcb_path: str | Path, out_dir: str | Path | None = None) -> dict:
    """Run `kicad-cli pcb export gerbers` on a .kicad_pcb file. No-op if
    kicad-cli isn't on PATH (returns {'available': False}).

    Returns {'available': bool, 'gerber_dir': path, 'drill_dir': path,
             'files': [...]} so the caller can include outputs in the bundle.
    """
    import shutil
    import subprocess

    cli = shutil.which("kicad-cli") or shutil.which("kicad-cli.exe")
    if not cli:
        return {"available": False, "reason": "kicad-cli not on PATH"}

    kicad_pcb_path = Path(kicad_pcb_path)
    out_dir = Path(out_dir) if out_dir else (kicad_pcb_path.parent / "gerbers")
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Gerbers
        subprocess.run(
            [cli, "pcb", "export", "gerbers",
             "--output", str(out_dir),
             str(kicad_pcb_path)],
            check=True, capture_output=True, timeout=60,
        )
        # Drill files
        subprocess.run(
            [cli, "pcb", "export", "drill",
             "--output", str(out_dir),
             str(kicad_pcb_path)],
            check=True, capture_output=True, timeout=60,
        )
        files = sorted(str(f) for f in out_dir.iterdir() if f.is_file())
        return {
            "available": True,
            "gerber_dir": str(out_dir),
            "files": files,
            "n_files": len(files),
        }
    except subprocess.CalledProcessError as exc:
        return {
            "available": True,
            "error": exc.stderr.decode("utf-8", errors="replace")[:500],
        }
    except Exception as exc:
        return {"available": True, "error": f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m aria_os.ecad.kicad_pcb_writer <bom.json> [out.kicad_pcb]")
        sys.exit(1)
    out = write_kicad_pcb(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"Wrote {out}")
    g = export_gerbers(out)
    print(f"Gerber export: {g}")
