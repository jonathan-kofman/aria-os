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
        f'  (symbol "ARIA_Generic{pin_count}"',
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
        f'    (symbol "ARIA_Generic{pin_count}_0_1"',
        f'      (rectangle (start {-w/2} {h/2}) (end {w/2} {-h/2})',
        '        (stroke (width 0.254) (type default))',
        '        (fill (type background)))',
        '    )',
        f'    (symbol "ARIA_Generic{pin_count}_1_1"',
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
                       renamed: str) -> str | None:
    """Read the source .kicad_sym, extract `sym_name`, flatten its
    (extends ...) chain by inlining the parent's body, rename the
    outer symbol name + all unit sub-symbols to `renamed` (no colon,
    avoids the sym-lib-table requirement).

    Returns the embedded s-expression, or None on failure.
    """
    try:
        text = Path(lib_path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    block = _extract_symbol_sexpr(text, sym_name)
    if block is None:
        return None

    # Walk (extends "PARENT") chain — inline parent's unit sub-symbols so
    # pins are available without a library lookup. Re-rename parent's unit
    # sub-symbol names from <parent_orig>_N_N to <renamed>_N_N.
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
        # Grab parent's unit sub-symbols (_N_N) for inlining
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
                        # Rename parent_name -> renamed in the unit block
                        unit_block = _re.sub(
                            rf'"{_re.escape(parent_name)}(_\d+_\d+)"',
                            lambda x: f'"{renamed}{x.group(1)}"',
                            unit_block)
                        parent_blocks.append(unit_block)
                        break
                k += 1
        current_block = parent_block

    # Rewrite the outer block: strip (extends ...), rename symbol refs,
    # append parent unit sub-symbols before the closing paren.
    result = _re.sub(r'\(extends\s+"[^"]+"\)', "", block)
    result = _re.sub(
        rf'\(symbol\s+"{_re.escape(sym_name)}"',
        f'(symbol "{renamed}"', result, count=1)
    result = _re.sub(
        rf'"{_re.escape(sym_name)}(_\d+_\d+)"',
        lambda x: f'"{renamed}{x.group(1)}"', result)
    if parent_blocks:
        # Insert parent unit blocks BEFORE the final ')' of the outer symbol
        last_paren = result.rfind(")")
        if last_paren > 0:
            result = (result[:last_paren]
                      + "\n" + "\n".join(parent_blocks) + "\n"
                      + result[last_paren:])
    return result


def _renamed_lib_id(sym_name: str) -> str:
    """KiCad-safe lib_id from a library symbol name. No colons."""
    clean = _re.sub(r"[^A-Za-z0-9_]", "_", sym_name)
    return f"ARIA_{clean}"


def _resolve_real_symbol(component: dict) -> dict | None:
    """Try kicad_symbol_lib.lookup_symbol; return
    {renamed, embedded_sexpr} or None on miss."""
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
    renamed = _renamed_lib_id(sym["symbol_name"])
    embedded = _embed_real_symbol(sym["lib_path"], sym["symbol_name"],
                                    renamed)
    if embedded is None:
        return None
    return {"renamed": renamed, "embedded_sexpr": embedded,
            "pins": sym["pins"], "symbol_name": sym["symbol_name"]}


def _symbol_instance(component: dict, x_mm: float, y_mm: float,
                     real_lib_id: str | None = None) -> str:
    ref = component.get("ref", "U?")
    value = component.get("value", "?")
    footprint = component.get("footprint", "")
    if real_lib_id is not None:
        lib_id = real_lib_id
    else:
        pad_count = component.get("pad_count", 8)
        pin_n = _sym_pin_count(pad_count)
        lib_id = f"ARIA_Generic{pin_n}"
    uid = _uuid()
    return (
        f'  (symbol (lib_id "{lib_id}") (at {x_mm} {y_mm} 0)'
        f' (unit 1) (in_bom yes) (on_board yes)\n'
        f'    (uuid "{uid}")\n'
        f'    (property "Reference" "{ref}" (at {x_mm} {y_mm - 8} 0)\n'
        f'      (effects (font (size 1.27 1.27))))\n'
        f'    (property "Value" "{value}" (at {x_mm} {y_mm + 8} 0)\n'
        f'      (effects (font (size 1.27 1.27))))\n'
        f'    (property "Footprint" "{footprint}" (at {x_mm} {y_mm} 0)\n'
        f'      (effects (font (size 1.27 1.27)) hide))\n'
        f'    (property "Datasheet" "" (at {x_mm} {y_mm} 0)\n'
        f'      (effects (font (size 1.27 1.27)) hide))\n'
        f'  )')


def _global_labels_for_component(nets: list, x_mm: float, y_mm: float) -> list:
    """For each net the component uses, place a global_label at a small
    offset. ERC matches globals with same text as connected."""
    out = []
    for i, net in enumerate(nets or []):
        lx = x_mm - 12 - (i * 3)
        ly = y_mm + (i % 4) * 2.54
        out.append(
            f'  (global_label "{net}" (shape input) (at {lx} {ly} 0)\n'
            f'    (effects (font (size 1.27 1.27))))')
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

    # v2: try to resolve every component to a real KiCad library symbol
    # (kicad_symbol_lib). Those that hit get embedded with real pin
    # electrical types; those that miss fall back to the generic N-pin
    # block. The generic block is only emitted for pin counts that are
    # actually used by fallback components.
    resolved: list[dict | None] = []
    real_embeds: list[str] = []
    seen_renamed: set[str] = set()
    n_real = 0
    n_fallback = 0
    for c in components:
        rr = _resolve_real_symbol(c)
        resolved.append(rr)
        if rr:
            if rr["renamed"] not in seen_renamed:
                seen_renamed.add(rr["renamed"])
                real_embeds.append(rr["embedded_sexpr"])
            n_real += 1
        else:
            n_fallback += 1

    pin_counts_used = {_sym_pin_count(c.get("pad_count", 8))
                       for c, rr in zip(components, resolved) if rr is None}
    lib_syms = _lib_symbols_block(pin_counts_used, embedded_real=real_embeds)

    symbol_sexprs = []
    label_sexprs = []
    for i, (c, rr) in enumerate(zip(components, resolved)):
        row, col = divmod(i, _GRID_COLS)
        x = 50 + col * _CELL_W_MM
        y = 50 + row * _CELL_H_MM
        real_lib_id = rr["renamed"] if rr else None
        symbol_sexprs.append(_symbol_instance(c, x, y,
                                               real_lib_id=real_lib_id))
        label_sexprs.extend(_global_labels_for_component(c.get("nets", []), x, y))

    print(f"[kicad_sch_writer] real symbols: {n_real}  "
          f"generic fallback: {n_fallback}")

    header_uuid = _uuid()

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
        '  (sheet_instances',
        '    (path "/" (page "1"))',
        '  )',
        ')',
    ]
    out_sch_path.parent.mkdir(parents=True, exist_ok=True)
    out_sch_path.write_text("\n".join(out), encoding="utf-8")
    return out_sch_path
