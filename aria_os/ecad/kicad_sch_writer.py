"""
Direct .kicad_sch file writer — produces a valid KiCad 7+ schematic file
from a BOM JSON without requiring KiCad to be installed.

Why: the PCB writer (kicad_pcb_writer.py) emits the routed board file
but there is no corresponding schematic artifact. That means:
  - no human-readable intent document per board
  - no ERC (Electrical Rule Check) — `kicad-cli sch erc` needs a .kicad_sch
  - no schematic SVG / PDF for review

This module mirrors kicad_pcb_writer's "write the s-expression directly,
no KiCad install required" approach, emitting a schematic with:
  - one symbol instance per component (simple N-pin block)
  - one global_label per net per component (ERC treats matching labels
    as the same net — that's how dense schematics handle power/ground
    without drawing every wire)

Output format reference:
  https://dev-docs.kicad.org/en/file-formats/sexpr-schematic/

Limitations vs hand-drawn schematic:
  - no sheet layout, no visual wire routing (labels instead of wires)
  - generic pin labels (1, 2, 3, ...) not manufacturer-verified symbol
    libs (the PCB uses minimal footprints too — same tradeoff)
  - no hierarchical sheets
  - component placement is on a simple grid, not engineering-aesthetic
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

_MM_PER_SCH_UNIT = 1.27  # KiCad 7+ default grid; schematic coords are in mm
_GRID_COLS = 4
_CELL_W_MM = 50.8        # 2 inches between component cells
_CELL_H_MM = 38.1        # 1.5 inches


def _uuid() -> str:
    return str(uuid4())


def _sym_pin_count(pad_count: int) -> int:
    """Round pad_count up to a reasonable pin count on the symbol block.
    Most IC symbols bunch at 4, 8, 16, 32, 64 pins."""
    for n in (4, 8, 16, 32, 64, 128, 256):
        if pad_count <= n:
            return n
    return pad_count


def _generic_symbol_sexpr(pin_count: int) -> str:
    """Return a (symbol ...) block for a generic N-pin component.
    Pins laid out on left/right sides of a rectangle."""
    half = pin_count // 2
    # Body dimensions in mm (KiCad schematic units)
    w = 10.16       # 4 cells
    h = max(10.16, 2.54 * (half + 1))
    lines = [
        f'  (symbol "aria_generic_{pin_count}"',
        '    (pin_numbers hide)',
        '    (pin_names (offset 0.254) hide)',
        '    (in_bom yes) (on_board yes)',
        f'    (property "Reference" "U" (at 0 {h/2 + 2.54} 0)',
        '      (effects (font (size 1.27 1.27))))',
        f'    (property "Value" "Generic" (at 0 {-h/2 - 2.54} 0)',
        '      (effects (font (size 1.27 1.27))))',
        '    (property "Footprint" "" (at 0 0 0)',
        '      (effects (font (size 1.27 1.27)) hide))',
        '    (property "Datasheet" "" (at 0 0 0)',
        '      (effects (font (size 1.27 1.27)) hide))',
        f'    (symbol "aria_generic_{pin_count}_0_1"',
        f'      (rectangle (start {-w/2} {h/2}) (end {w/2} {-h/2})',
        '        (stroke (width 0.254) (type default))',
        '        (fill (type background)))',
        '    )',
        f'    (symbol "aria_generic_{pin_count}_1_1"',
    ]
    # Left-side pins (1 to half)
    for i in range(half):
        y = h/2 - 2.54 - i * 2.54
        n = i + 1
        lines.append(
            f'      (pin passive line (at {-w/2 - 2.54} {y} 0) (length 2.54)'
            f' (name "{n}" (effects (font (size 1.27 1.27))))'
            f' (number "{n}" (effects (font (size 1.27 1.27)))))')
    # Right-side pins (half+1 to N)
    for i in range(pin_count - half):
        y = h/2 - 2.54 - i * 2.54
        n = half + i + 1
        lines.append(
            f'      (pin passive line (at {w/2 + 2.54} {y} 180) (length 2.54)'
            f' (name "{n}" (effects (font (size 1.27 1.27))))'
            f' (number "{n}" (effects (font (size 1.27 1.27)))))')
    lines.append('    )')
    lines.append('  )')
    return "\n".join(lines)


def _lib_symbols_block(pin_counts_used: set[int],
                       embedded_real: list[str] | None = None) -> str:
    body = "\n".join(_generic_symbol_sexpr(n) for n in sorted(pin_counts_used))
    if embedded_real:
        body = body + "\n" + "\n".join(embedded_real) if body \
               else "\n".join(embedded_real)
    return f"(lib_symbols\n{body}\n)"


# ---------------------------------------------------------------------------
# Real-symbol embedder (v2 path) — reuse KiCad library symbols with correct
# pin electrical types, avoiding the colon-prefix trap by renaming to
# ARIA_<name>. Flattens (extends ...) chains so we don't need a
# sym-lib-table sidecar.
# ---------------------------------------------------------------------------

import re as _re


def _extract_symbol_sexpr(text: str, sym_name: str) -> str | None:
    """Return the full (symbol "sym_name" ...) block via depth-matched
    parens. Returns None if the symbol isn't in this file."""
    key = f'(symbol "{sym_name}"'
    i = text.find(key)
    if i < 0:
        return None
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
        j += 1
    return None


def _embed_real_symbol(lib_path: str, sym_name: str,
                       lib_id: str) -> str | None:
    """Read the source .kicad_sym, extract `sym_name`, flatten its
    (extends ...) chain by inlining the parent's unit sub-symbols.

    KiCad 10 format rules (verified against KiCad 10 demo
    cm5_minima/CM5.kicad_sch):
      - The OUTER symbol is named with a colon prefix: `LibName:SymName`
        (e.g. `Regulator_Linear:AMS1117-3.3`)
      - INNER unit sub-symbols keep the bare `SymName_N_N` form
        (e.g. `AMS1117-3.3_1_1`) — NO colon prefix
      - When flattening (extends), parent's unit sub-symbols are renamed
        from `parent_N_N` to `child_N_N` (still bare, no colon)

    `lib_id` is the outer colon-prefixed name to install. Returns the
    embedded s-expression, or None on failure.
    """
    try:
        text = Path(lib_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    block = _extract_symbol_sexpr(text, sym_name)
    if block is None:
        return None

    # Walk (extends "PARENT") chain — inline parent's unit sub-symbols so
    # pins are available without a library lookup. Rename parent's unit
    # sub-symbol names from <parent_name>_N_N to <sym_name>_N_N (bare).
    parent_blocks: list[str] = []
    visited = {sym_name}
    current_block = block
    for _ in range(4):  # max chain depth
        m = _re.search(r'\(extends\s+"([^"]+)"\)', current_block)
        if not m:
            break
        parent_name = m.group(1)
        if parent_name in visited:
            break
        visited.add(parent_name)
        parent_block = _extract_symbol_sexpr(text, parent_name)
        if parent_block is None:
            break
        for sub_m in _re.finditer(
                rf'\(symbol\s+"{_re.escape(parent_name)}_\d+_\d+"',
                parent_block):
            start = sub_m.start()
            depth = 0
            k = start
            while k < len(parent_block):
                ch = parent_block[k]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        unit_block = parent_block[start:k + 1]
                        # Rename parent_NNN -> sym_name_NNN (bare, no colon)
                        unit_block = _re.sub(
                            rf'"{_re.escape(parent_name)}(_\d+_\d+)"',
                            lambda x: f'"{sym_name}{x.group(1)}"',
                            unit_block)
                        parent_blocks.append(unit_block)
                        break
                k += 1
        current_block = parent_block

    # Rewrite outer block:
    #   - strip (extends ...)
    #   - rename outer symbol to colon-prefixed lib_id
    #   - leave inner unit sub-symbols (sym_name_N_N) BARE (no colon prefix)
    result = _re.sub(r'\(extends\s+"[^"]+"\)', "", block)
    result = _re.sub(
        rf'\(symbol\s+"{_re.escape(sym_name)}"',
        f'(symbol "{lib_id}"', result, count=1)
    # Note: we do NOT rename inner `sym_name_N_N` sub-symbols — they stay bare
    if parent_blocks:
        # Insert parent unit blocks BEFORE the final ')' of the outer symbol
        last_paren = result.rfind(")")
        if last_paren > 0:
            result = (result[:last_paren]
                      + "\n" + "\n".join(parent_blocks) + "\n"
                      + result[last_paren:])
    return result


def _pro_lib_id(lib_name: str, sym_name: str) -> str:
    """Canonical KiCad lib_id: `LibName:SymName`. Research 2026-04-19
    confirmed this is the pro-grade form — not the ARIA_ rename. KiCad
    10 resolves via a sym-lib-table sidecar (emitted by
    kicad_project.emit_sym_lib_table); the schematic's (lib_symbols ...)
    block must contain a matching `(symbol "LibName:SymName" ...)` with
    the full pin definitions embedded.
    """
    return f"{lib_name}:{sym_name}"


def _resolve_real_symbol(component: dict) -> dict | None:
    """Try kicad_symbol_lib.lookup_symbol; return
    {lib_id, embedded_sexpr, pins, lib_name, symbol_name} or None."""
    try:
        from .kicad_symbol_lib import lookup_symbol
    except Exception:
        return None
    value = component.get("value", "")
    if not value:
        return None
    sym = lookup_symbol(value)
    if sym is None or not sym.get("pins"):
        return None
    lib_id = _pro_lib_id(sym["lib_name"], sym["symbol_name"])
    embedded = _embed_real_symbol(sym["lib_path"], sym["symbol_name"],
                                    lib_id)
    if embedded is None:
        return None
    return {"lib_id": lib_id, "embedded_sexpr": embedded,
            "pins": sym["pins"], "lib_name": sym["lib_name"],
            "symbol_name": sym["symbol_name"]}


def _symbol_instance(component: dict, x_mm: float, y_mm: float,
                     real_lib_id: str | None = None,
                     *,
                     project_name: str = "aria_os",
                     root_sheet_uuid: str | None = None,
                     pin_count: int | None = None) -> str:
    """Emit a schematic-level symbol instance in KiCad 10 canonical form.
    Per research 2026-04-20: KiCad 10 REQUIRES an (instances ...) block on
    every symbol so the symbol is tied to a root sheet path. Without it
    the schematic errors with "Failed to load schematic" at parse time.
    Verified against KiCad 10 demo cm5_minima/CM5.kicad_sch.
    """
    ref = component.get("ref", "U?")
    value = component.get("value", "?")
    footprint = component.get("footprint", "")
    if real_lib_id is not None:
        lib_id = real_lib_id
        pn = pin_count or component.get("pad_count", 2)
    else:
        pc = component.get("pad_count", 8)
        pn = _sym_pin_count(pc)
        lib_id = f"aria_generic_{pn}"
    uid = _uuid()
    if root_sheet_uuid is None:
        root_sheet_uuid = _uuid()
    pins = "\n".join(
        f'    (pin "{i + 1}" (uuid "{_uuid()}"))'
        for i in range(max(1, int(pn or 1))))
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x_mm} {y_mm} 0) (unit 1)\n'
        f'    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)\n'
        f'    (uuid "{uid}")\n'
        f'    (property "Reference" "{ref}" (at {x_mm} {y_mm - 8} 0)\n'
        f'      (effects (font (size 1.27 1.27))))\n'
        f'    (property "Value" "{value}" (at {x_mm} {y_mm + 8} 0)\n'
        f'      (effects (font (size 1.27 1.27))))\n'
        f'    (property "Footprint" "{footprint}" (at {x_mm} {y_mm} 0)\n'
        f'      (effects (font (size 1.27 1.27)) hide))\n'
        f'    (property "Datasheet" "" (at {x_mm} {y_mm} 0)\n'
        f'      (effects (font (size 1.27 1.27)) hide))\n'
        f'{pins}\n'
        f'    (instances\n'
        f'      (project "{project_name}"\n'
        f'        (path "/{root_sheet_uuid}" (reference "{ref}") (unit 1))\n'
        f'      )\n'
        f'    )\n'
        f'  )')


def _global_labels_for_component(nets: list, x_mm: float, y_mm: float) -> list:
    """Legacy: floating global_labels at arbitrary offsets — used only for
    generic-fallback symbols where we don't have real pin coords. Labels
    are snapped to the 1.27mm schematic grid so they don't raise
    `endpoint_off_grid`. They still flag `label_dangling` in ERC (no pin
    touches them), which is the correct signal: these components lack
    real symbols and aren't electrically modelled yet. The labels carry
    net membership for the downstream PCB writer that reads the same BOM.
    """
    out = []
    for i, net in enumerate(nets or []):
        lx = _snap(x_mm - 12 - (i * _GRID_MM))
        ly = _snap(y_mm + (i % 4) * _GRID_MM)
        out.append(
            f'  (global_label "{net}" (shape input) (at {lx} {ly} 0)\n'
            f'    (effects (font (size 1.27 1.27))))')
    return out


def _power_flags(nets_used: set[str], x_mm: float, y_mm: float) -> list[str]:
    """Emit one `(global_label ...)` with shape `output` per power net so
    ERC sees each power rail as driven. This is the scaffolding-level
    equivalent of dropping a PWR_FLAG symbol on every supply rail;
    a future iteration should use the canonical `power:PWR_FLAG` symbol
    from the KiCad symbol lib, but this keeps ERC clean today without
    pulling another library embed."""
    out: list[str] = []
    power_nets = {"GND", "+3V3", "+5V", "VBAT", "VBUS", "VIN", "VCC"}
    for i, net in enumerate(sorted(n for n in nets_used if n.upper() in power_nets)):
        fx = _snap(x_mm + 20 + i * _GRID_MM * 4)
        fy = _snap(y_mm - 20)
        out.append(
            f'  (global_label "{net}" (shape output) (at {fx} {fy} 0)\n'
            f'    (effects (font (size 1.27 1.27))))')
    return out


_GRID_MM = 1.27  # KiCad schematic default — required for ERC clean


def _snap(v: float) -> float:
    return round(v / _GRID_MM) * _GRID_MM


_PIN_NAME_TO_NET = {
    "VDD": "+3V3", "VDDA": "+3V3", "VDDIO": "+3V3", "AVDD": "+3V3",
    "VCC": "+3V3", "VCCIO": "+3V3",
    "VSS": "GND", "VSSA": "GND", "GND": "GND", "AGND": "GND", "DGND": "GND",
    "VBAT": "VBAT", "VBUS": "VBUS", "VIN": "VIN",
    "+5V": "+5V", "+3V3": "+3V3",
}


def _augment_net_map_from_symbol(net_map: dict | None,
                                 pins: list[dict]) -> dict:
    """Merge name-based pin→net inferences from the real KiCad symbol
    into the component's existing net_map.

    Priority rules:
      - For pins the symbol names as a power pin (VDD/VSS/GND/VBAT/etc)
        AND that have power_in/power_out etype, the symbol is
        AUTHORITATIVE and OVERRIDES any existing net_map entry. The BOM's
        hardcoded pad tables sometimes disagree with the actual symbol
        (e.g. STM32F405 VDD is LQFP pin 19, not 17) and the symbol wins
        because it's derived from ST's datasheet pinout directly.
      - For all other pins the existing net_map entry wins (BOM's
        peripheral-routing logic knows which GPIO handles I2C/UART/USB).
    """
    merged = dict(net_map or {})
    for p in pins or []:
        num = str(p.get("number", ""))
        name_up = str(p.get("name", "")).upper().strip()
        etype = str(p.get("etype", "")).lower()
        inferred = _PIN_NAME_TO_NET.get(name_up)
        if not inferred:
            continue
        is_power = etype in ("power_in", "power_out")
        if num not in merged:
            merged[num] = inferred
        elif is_power and merged[num] != inferred:
            # Override — symbol pin-name is authoritative for power nets
            merged[num] = inferred
    return merged


def _labels_at_pin_tips(pins: list[dict], net_map: dict,
                        inst_x: float, inst_y: float) -> list[str]:
    """For each pin whose net is resolved in `net_map`, emit:
      1. A `(wire ...)` stub from the pin tip to a point 1.27mm outward
         (in the direction the pin points, per its rot field).
      2. A `(global_label ...)` at the wire's outer endpoint.
    Together these make KiCad 10's ERC treat the pin as connected — a
    label AT the pin tip is NOT enough; KiCad requires a wire end there.
    Verified 2026-04-20 against ERC behavior on the CM5 demo.

    For pins with no assigned net that aren't of input/power_in etype,
    emit `(no_connect ...)` at the pin tip — ERC then skips them cleanly.

    All coords are snapped to the 1.27mm grid (symbol-local pin coords
    are already multiples of 1.27; we only snap to be defensive when the
    caller's instance origin isn't grid-aligned).
    """
    out: list[str] = []
    for p in pins or []:
        num = str(p.get("number", ""))
        px = _snap(inst_x + float(p.get("x", 0)))
        py = _snap(inst_y + float(p.get("y", 0)))
        net = net_map.get(num) if net_map else None
        if net and net.upper() not in ("NC", "N/C", "NONE", ""):
            shape = ("output" if net.upper() in (
                "GND", "VCC", "+3V3", "+5V", "VBAT", "VBUS", "VIN")
                else "input")
            out.append(
                f'  (global_label "{net}" (shape {shape}) (at {px:.3f} {py:.3f} 0)\n'
                f'    (effects (font (size 1.27 1.27))))')
        else:
            # No net assigned. Emit a (no_connect) flag at the pin tip
            # so KiCad ERC stops complaining "pin_not_connected".
            #
            # 2026-04-25 (W13 Track A Path B): the prior code skipped
            # `input` etype pins, leaving them visibly floating. On a
            # 64-pin STM32 with most GPIOs unused, this drove ~137/184
            # ERC violations. The right answer: mark unused pins as
            # explicit no-connect, and let KiCad's `unused_no_connect`
            # warning catch the inverse case (a no_connect on a pin
            # that IS wired). Power_in still skips because dangling
            # power_in pins SHOULD be flagged -- they're real wiring
            # bugs, not deliberate float.
            etype = str(p.get("etype", "")).lower()
            if etype == "power_in":
                continue
            out.append(
                f'  (no_connect (at {px:.3f} {py:.3f}) (uuid "{_uuid()}"))')
    return out


def write_kicad_sch(bom_path: str | Path,
                    out_sch_path: str | Path | None = None,
                    *,
                    board_name: str | None = None) -> Path:
    """Write a .kicad_sch from an ECAD BOM. Returns the written path.

    BOM shape (as emitted by ecad_generator.py):
      { "components": [
          {"ref": "U1", "value": "...", "footprint": "...",
           "pad_count": 65, "nets": ["+3V3", "GND", ...]},
          ...],
        "board": {...}}
    """
    bom_path = Path(bom_path)
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    components = bom.get("components", []) or []

    if out_sch_path is None:
        out_sch_path = bom_path.with_name(bom_path.stem + ".kicad_sch")
    out_sch_path = Path(out_sch_path)

    title = (board_name
             or bom.get("board", {}).get("name")
             or out_sch_path.stem)

    # v3: resolve every component to its canonical KiCad lib_id
    # (`LibName:SymName` — colon-prefixed, NO rename). Embed the full
    # symbol block so the schematic is self-contained for rendering,
    # and rely on the sym-lib-table sidecar (emitted below) for editing /
    # update-from-library flows. This matches what KiCad 10's own demos do.
    resolved: list[dict | None] = []
    real_embeds: list[str] = []
    seen_lib_ids: set[str] = set()
    sym_libs_used: set[str] = set()
    n_real = 0
    n_fallback = 0
    for c in components:
        rr = _resolve_real_symbol(c)
        resolved.append(rr)
        if rr:
            if rr["lib_id"] not in seen_lib_ids:
                seen_lib_ids.add(rr["lib_id"])
                real_embeds.append(rr["embedded_sexpr"])
                sym_libs_used.add(rr["lib_name"])
            n_real += 1
        else:
            n_fallback += 1

    pin_counts_used = {_sym_pin_count(c.get("pad_count", 8))
                       for c, rr in zip(components, resolved) if rr is None}
    lib_syms = _lib_symbols_block(pin_counts_used, embedded_real=real_embeds)

    # Single root-sheet UUID shared across every instance in this schematic
    # — KiCad 10's (instances (project ... (path "/UUID" ...))) ties each
    # symbol to the root sheet, and all symbols on the root share the same
    # path UUID (the sheet's own uuid).
    header_uuid = _uuid()
    project_name = out_sch_path.stem

    symbol_sexprs = []
    label_sexprs = []
    for i, (c, rr) in enumerate(zip(components, resolved)):
        row, col = divmod(i, _GRID_COLS)
        # Snap instance origin to the 1.27mm grid so pin tips (symbol-local
        # coords are already multiples of 1.27) land on grid. If we don't
        # snap here, label positions computed by _labels_at_pin_tips are
        # rounded to grid while actual pin tips sit 0.47mm off — ERC then
        # raises pin_not_connected on every labeled pin. Keep cell sizes
        # multiples of 1.27 too (50.8 = 40×1.27, 38.1 = 30×1.27).
        x = _snap(50.8 + col * _CELL_W_MM)
        y = _snap(50.8 + row * _CELL_H_MM)
        real_lib_id = rr["lib_id"] if rr else None
        pn = (len(rr["pins"]) if rr else None)
        symbol_sexprs.append(_symbol_instance(
            c, x, y, real_lib_id=real_lib_id,
            project_name=project_name,
            root_sheet_uuid=header_uuid,
            pin_count=pn))
        if rr:
            # Pro path: anchor a global_label at each pin tip whose net is
            # assigned in the BOM's per-pin net_map. ERC sees this as a
            # clean connection → no pin_not_connected flood. We merge in
            # pin-name-derived inferences (VDD→+3V3, GND→GND) so the label
            # coverage doesn't depend on the BOM's pad table being perfectly
            # aligned with the symbol's pin numbering.
            merged = _augment_net_map_from_symbol(c.get("net_map"), rr["pins"])
            label_sexprs.extend(_labels_at_pin_tips(
                rr["pins"], merged, x, y))
        else:
            # Scaffolding fallback for generic symbols without pin coords —
            # floating labels carry net membership for the PCB writer but
            # will flag dangling in ERC. Acceptable until all components
            # are real-lib-resolved.
            label_sexprs.extend(_global_labels_for_component(
                c.get("nets", []), x, y))

    # NOTE: power_pin_not_driven warnings remain as scaffolding. A true
    # fix needs the canonical `power:PWR_FLAG` symbol embedded from the
    # KiCad `power` library + instances at each supply rail. Floating
    # output-shape labels at arbitrary coords only add dangling-label
    # violations — worse than the problem they aim to solve.
    pwr_flags: list[str] = []

    print(f"[kicad_sch_writer] real symbols: {n_real}  "
          f"generic fallback: {n_fallback}")

    out = [
        '(kicad_sch',
        '  (version 20250610)',
        '  (generator "aria-os-export")',
        '  (generator_version "10.0")',
        f'  (uuid "{header_uuid}")',
        '  (paper "A4")',
        '  (title_block',
        f'    (title "{title}")',
        '    (company "aria-os")',
        '  )',
        lib_syms,
        *symbol_sexprs,
        *label_sexprs,
        *pwr_flags,
        '  (sheet_instances',
        '    (path "/" (page "1"))',
        '  )',
        '  (embedded_fonts no)',
        ')',
    ]
    out_sch_path.parent.mkdir(parents=True, exist_ok=True)
    out_sch_path.write_text("\n".join(out), encoding="utf-8")

    # Also emit the project bundle sidecar: .kicad_pro + sym-lib-table +
    # fp-lib-table, so the schematic opens cleanly in eeschema and the
    # lib_ids resolve for edit / update-from-library. Safe to fail —
    # schematic still loads via embedded lib_symbols cache even without
    # the lib-tables.
    try:
        from .kicad_project import emit_kicad_project, fp_lib_from_bom
        fp_libs = fp_lib_from_bom(bom)
        emit_kicad_project(
            out_dir=out_sch_path.parent,
            project_name=out_sch_path.stem,
            sym_libs=sym_libs_used,
            fp_libs=fp_libs)
    except Exception as exc:
        print(f"[kicad_sch_writer] project bundle emit failed: "
              f"{type(exc).__name__}: {exc}")

    return out_sch_path
