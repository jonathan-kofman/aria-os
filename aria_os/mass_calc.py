"""
Per-part mass calculation from STEP volume × material density.

Why: ARIA's BOM had `mass_g: None` everywhere because no one was actually
computing it. Downstream this broke:
  - Flight sim TWR/hover throttle (assumed 600g hardcoded)
  - MillForge cost quotes (no mass → can't price by weight)
  - Slicer print-time estimates (used a heuristic factor only)

This module reads each part's STEP, gets the volume from cadquery's
.Volume() in mm³, multiplies by material density in g/cm³, divides by
1000 to convert to grams. ~30s for a 31-part drone (cached STL helps).

Usage:
    from aria_os.mass_calc import compute_part_masses, MATERIAL_DENSITY
    masses = compute_part_masses(parts_dir, bom_dict)
    # masses = {"bottom_plate": 47.2, "motor": 38.4, ...}  in grams
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# Material densities in g/cm³. Sourced from MatWeb / manufacturer spec
# sheets. PETG/ABS/PLA values assume ~30% infill — adjust per print profile
# at the slicer stage (slicer.py already does the infill multiplier).
MATERIAL_DENSITY: dict[str, float] = {
    # CFRP / composites
    "cfrp":            1.55,   # carbon fiber reinforced epoxy plate
    "carbon_fiber":    1.55,
    "carbon_fibre":    1.55,
    "aramid":          1.44,   # Kevlar 49 nominal
    "kevlar":          1.44,
    # Metals
    "aluminum_6061":   2.70,
    "aluminum_7075":   2.81,
    "aluminum":        2.70,
    "steel":           7.85,
    "steel_4140":      7.85,
    "stainless_steel": 7.93,
    "titanium":        4.43,
    "titanium_6al4v":  4.43,
    "brass":           8.50,
    # Plastics (solid — slicer.py adjusts for infill)
    "petg":            1.27,
    "abs":             1.04,
    "asa":             1.07,
    "pla":             1.24,
    "polycarbonate":   1.20,
    "petg-cf":         1.30,
    "delrin_acetal":   1.41,
    # PCBs
    "fr4":             1.85,   # FR-4 substrate (no copper, no components)
    # Batteries — LiPo pack effective density (cell + wrap + tab)
    "lipo_4s":         2.30,
    "lipo_3s":         2.30,
    "lipo_6s":         2.30,
    # Default fallback for unknown materials
    "default":         1.50,
}


def compute_part_masses(parts_dir: str | Path,
                        bom: dict | None = None) -> dict[str, float]:
    """Compute mass in grams for each unique part STEP under parts_dir.

    Returns {part_spec_name: mass_g}. Uses cadquery to read each STEP and
    pull volume in mm³, then multiplies by the material density derived from
    the BOM (or guesses from the part name if no BOM given).
    """
    import cadquery as cq

    parts_dir = Path(parts_dir)
    masses: dict[str, float] = {}

    # Build a quick spec → material lookup from the BOM
    spec_to_material: dict[str, str] = {}
    if bom:
        for p in (bom.get("parts") or []):
            if not isinstance(p, dict):
                continue
            spec = p.get("spec") or p.get("name", "")
            mat = p.get("material", "")
            if spec and mat and spec not in spec_to_material:
                spec_to_material[spec] = mat

    for step_file in sorted(parts_dir.glob("*.step")):
        spec = step_file.stem
        material = spec_to_material.get(spec, _guess_material_from_name(spec))
        density = MATERIAL_DENSITY.get(material.lower(),
                                        MATERIAL_DENSITY["default"])
        try:
            shape = cq.importers.importStep(str(step_file))
            vol_mm3 = float(shape.val().Volume())
            # mm³ → cm³ (÷1000) → g (× density g/cm³)
            mass_g = (vol_mm3 / 1000.0) * density
            masses[spec] = round(mass_g, 2)
        except Exception as exc:
            print(f"[mass] {spec} skipped: {type(exc).__name__}: {exc}")
            masses[spec] = 0.0
    return masses


def _guess_material_from_name(spec: str) -> str:
    """Fall-back material guess when BOM doesn't say."""
    s = spec.lower()
    if any(k in s for k in ("plate", "arm", "frame")):
        return "cfrp"
    if any(k in s for k in ("standoff", "motor", "rail", "eyelet")):
        return "aluminum_6061"
    if "battery" in s:
        return "lipo_4s"
    if "armor" in s:
        return "aramid"
    if any(k in s for k in ("pcb", "fc_", "esc_")):
        return "fr4"
    if any(k in s for k in ("canopy", "spool", "pod", "module", "puck")):
        return "petg"
    return "default"


def populate_bom_masses(bom_path: str | Path,
                        parts_dir: str | Path) -> dict[str, Any]:
    """Read a BOM, compute mass per part, write the BOM back with mass_g
    populated. Also adds a top-level total_mass_g field.

    Returns the updated BOM dict for downstream consumers (flight_sim,
    MillForge handoff, etc.).
    """
    bom_path = Path(bom_path)
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    masses = compute_part_masses(parts_dir, bom)

    # Per-instance mass: assemblies have N copies of some specs (4 motors,
    # 4 props, etc.). Multiply by instance count from the BOM.
    total = 0.0
    spec_counts: dict[str, int] = {}
    for p in (bom.get("parts") or []):
        if isinstance(p, dict):
            spec = p.get("spec") or p.get("name", "")
            spec_counts[spec] = spec_counts.get(spec, 0) + 1

    for p in (bom.get("parts") or []):
        if not isinstance(p, dict):
            continue
        spec = p.get("spec") or p.get("name", "")
        m = masses.get(spec, 0.0)
        p["mass_g"] = m
        total += m
    bom["total_mass_g"] = round(total, 2)
    bom["mass_breakdown"] = {
        spec: round(masses.get(spec, 0.0) * cnt, 2)
        for spec, cnt in spec_counts.items()
    }

    bom_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")
    return bom


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: python -m aria_os.mass_calc <bom.json> <parts_dir>")
        sys.exit(1)
    bom = populate_bom_masses(sys.argv[1], sys.argv[2])
    print(f"Total mass: {bom['total_mass_g']:.1f} g")
    print("Breakdown:")
    for spec, m in sorted(bom["mass_breakdown"].items(),
                          key=lambda kv: -kv[1]):
        print(f"  {spec:18s}  {m:>7.1f} g")
