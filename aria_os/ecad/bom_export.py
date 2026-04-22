"""BOM export for a `.kicad_pcb`.

Reads footprint data — reference, value, package, position, side, and
any `MPN` / `Manufacturer` fields — and writes a CSV ready to send to
a distributor (JLC Parts, Digi-Key Scheme-It, PCBWay Parts) or feed into
a pick-and-place process.

Two backends tried in order:
  1. pcbnew.Python (preferred) — reads the board via its parser, pulls
     properties cleanly.
  2. Raw-text parse of the .kicad_pcb s-expression file — works when
     pcbnew isn't importable.

Output CSV columns:
  Reference, Value, Footprint, MPN, Manufacturer, Quantity,
  PosX_mm, PosY_mm, Rotation_deg, Side
"""
from __future__ import annotations

import csv
import re
from pathlib import Path


def export_bom(pcb_path: str | Path,
                *, out_csv: str | Path | None = None,
                repo_root: Path | None = None) -> Path:
    pcb_path = Path(pcb_path)
    if not pcb_path.is_file():
        raise FileNotFoundError(f"Not a file: {pcb_path}")
    if out_csv is None:
        out_dir = pcb_path.parent / "bom"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_csv = out_dir / f"{pcb_path.stem}.bom.csv"
    out_csv = Path(out_csv)

    rows = _collect_via_pcbnew(pcb_path)
    if not rows:
        rows = _collect_via_text(pcb_path)
    if not rows:
        raise RuntimeError("No footprints found — BOM would be empty")

    # Group identical (value, footprint, mpn, manufacturer) rows and
    # accumulate references + quantity. This is standard BOM format.
    groups: dict[tuple, dict] = {}
    for r in rows:
        key = (r["value"], r["footprint"], r["mpn"], r["manufacturer"])
        if key in groups:
            groups[key]["references"].append(r["reference"])
            groups[key]["quantity"] += 1
        else:
            groups[key] = {
                "references": [r["reference"]],
                "value":        r["value"],
                "footprint":    r["footprint"],
                "mpn":          r["mpn"],
                "manufacturer": r["manufacturer"],
                "quantity":     1,
                # positions only for the pick-and-place file — stash the first
                "pos_x_mm":     r["pos_x_mm"],
                "pos_y_mm":     r["pos_y_mm"],
                "rotation_deg": r["rotation_deg"],
                "side":         r["side"],
            }

    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "Reference", "Value", "Footprint", "MPN", "Manufacturer",
            "Quantity", "PosX_mm", "PosY_mm", "Rotation_deg", "Side",
        ])
        for g in groups.values():
            writer.writerow([
                ",".join(sorted(g["references"])),
                g["value"], g["footprint"], g["mpn"], g["manufacturer"],
                g["quantity"], g["pos_x_mm"], g["pos_y_mm"],
                g["rotation_deg"], g["side"],
            ])
    return out_csv


def _collect_via_pcbnew(pcb_path: Path) -> list[dict]:
    try:
        import pcbnew  # type: ignore
    except Exception:
        return []
    try:
        board = pcbnew.LoadBoard(str(pcb_path))
    except Exception:
        return []
    rows = []
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        rot = fp.GetOrientationDegrees()
        side = "top" if fp.GetLayer() == pcbnew.F_Cu else "bottom"
        props = {f.GetName(): f.GetText()
                 for f in fp.GetFields() if hasattr(f, "GetName")}
        rows.append({
            "reference":   fp.GetReference(),
            "value":       fp.GetValue(),
            "footprint":   str(fp.GetFPID().GetLibItemName()),
            "mpn":         props.get("MPN", "") or props.get("PartNumber", ""),
            "manufacturer": props.get("Manufacturer", "") or props.get("Mfr", ""),
            "pos_x_mm":    pcbnew.ToMM(pos.x),
            "pos_y_mm":    pcbnew.ToMM(pos.y),
            "rotation_deg": rot,
            "side":        side,
        })
    return rows


def _collect_via_text(pcb_path: Path) -> list[dict]:
    """Fallback s-expression parser for `.kicad_pcb`."""
    text = pcb_path.read_text(encoding="utf-8", errors="ignore")
    rows = []
    # Each footprint is `(footprint "library:name" ... (at x y [rot])
    #   (layer "F.Cu") ... (property "Reference" "R1") (property "Value" "330"))`
    for m in re.finditer(r"\(footprint\s+\"([^\"]+)\"", text):
        start = m.start()
        # Find matching close paren for this footprint block (depth count)
        depth, end = 0, start
        for i in range(start, len(text)):
            c = text[i]
            if c == "(": depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0: end = i + 1; break
        block = text[start:end]
        fp_name = m.group(1)
        ref = _first_property(block, "Reference") or "?"
        val = _first_property(block, "Value") or ""
        mpn = _first_property(block, "MPN") or _first_property(block, "PartNumber") or ""
        mfr = _first_property(block, "Manufacturer") or _first_property(block, "Mfr") or ""
        pos_match = re.search(r"\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+([-\d.]+))?\s*\)", block)
        side = "bottom" if '"B.Cu"' in block and '"F.Cu"' not in block else "top"
        rows.append({
            "reference": ref, "value": val, "footprint": fp_name,
            "mpn": mpn, "manufacturer": mfr,
            "pos_x_mm":    float(pos_match.group(1)) if pos_match else 0.0,
            "pos_y_mm":    float(pos_match.group(2)) if pos_match else 0.0,
            "rotation_deg": float(pos_match.group(3) or 0) if pos_match else 0.0,
            "side": side,
        })
    return rows


def _first_property(block: str, name: str) -> str | None:
    m = re.search(rf'\(property\s+"{re.escape(name)}"\s+"([^"]*)"', block)
    return m.group(1) if m else None
