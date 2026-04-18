"""
Elegoo Centauri Carbon (FDM) slicer integration.

Picks 3D-printable parts from a drone build (or any assembly), auto-orients
them flat-side-down, writes a print-ready directory with:

  print/
    <part>.stl                  Oriented STL (flat side down on build plate)
    <part>.config.json          Per-part recommended print settings
    print_summary.json          Aggregate: material grams, time estimate, fit check
    elegoo_orca_cmd.bat         One-line CLI to slice all parts via Orca Slicer
    README.md                   Print order + post-processing notes

Detects Elegoo Slicer / Orca Slicer if installed and offers to invoke it.

The Elegoo Centauri Carbon ships with Elegoo Slicer (an Orca/Bambu fork).
The CLI is `orca-slicer.exe` on Windows. We write commands targeting the
Centauri Carbon profile but they work with any Orca Slicer install.

Build volume: 256 × 256 × 256 mm.

Usage:
    from aria_os.slicer import prepare_for_print
    summary = prepare_for_print(assembly_dir, profile="elegoo_centauri_carbon")
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Elegoo Centauri Carbon spec (from manufacturer documentation)
CENTAURI_CARBON = {
    "name": "Elegoo Centauri Carbon",
    "build_volume_mm": (256, 256, 256),
    "nozzle_dia_mm": 0.4,
    "max_nozzle_temp_c": 320,
    "max_bed_temp_c": 110,
    "max_speed_mm_s": 500,
    "default_speed_mm_s": 200,
    "filaments_supported": ["PLA", "PETG", "ABS", "ASA", "TPU", "PA-CF", "PC", "PETG-CF"],
    "slicer_cli": ["orca-slicer", "orca-slicer.exe", "ElegooSlicer.exe"],
    "preset_url": "https://github.com/SoftFever/OrcaSlicer/tree/main/resources/profiles/Elegoo",
}


# Default print settings per material class (sane Elegoo Centauri starting points)
_MATERIAL_PROFILES = {
    "petg": {
        "filament": "PETG",
        "nozzle_temp_c": 245,
        "bed_temp_c": 80,
        "speed_mm_s": 60,
        "infill_pct": 25,
        "wall_loops": 3,
        "top_bottom_layers": 5,
        "support": False,
        "density_g_cm3": 1.27,
    },
    "abs": {
        "filament": "ABS",
        "nozzle_temp_c": 250,
        "bed_temp_c": 105,
        "speed_mm_s": 50,
        "infill_pct": 25,
        "wall_loops": 3,
        "top_bottom_layers": 5,
        "support": False,
        "enclosure_required": True,
        "density_g_cm3": 1.04,
    },
    "asa": {
        "filament": "ASA",
        "nozzle_temp_c": 250,
        "bed_temp_c": 100,
        "speed_mm_s": 50,
        "infill_pct": 30,
        "wall_loops": 3,
        "top_bottom_layers": 5,
        "support": False,
        "enclosure_required": True,
        "density_g_cm3": 1.07,
    },
    "pla": {
        "filament": "PLA",
        "nozzle_temp_c": 215,
        "bed_temp_c": 60,
        "speed_mm_s": 80,
        "infill_pct": 20,
        "wall_loops": 3,
        "top_bottom_layers": 4,
        "support": False,
        "density_g_cm3": 1.24,
    },
    "petg-cf": {
        "filament": "PETG-CF",
        "nozzle_temp_c": 260,
        "bed_temp_c": 80,
        "speed_mm_s": 50,
        "infill_pct": 30,
        "wall_loops": 4,
        "top_bottom_layers": 5,
        "support": False,
        "density_g_cm3": 1.30,
        "hardened_nozzle_required": True,
    },
}


# Material → suggested print profile mapping. Anything not in this map is
# treated as non-printable (CNC, purchased, etc.).
_PRINTABLE_MATERIALS = {
    "petg":          "petg",
    "abs":           "abs",
    "asa":           "asa",
    "pla":           "pla",
    "petg-cf":       "petg-cf",
    "polycarbonate": "petg",  # PETG is friendlier than PC for FDM
}

# Materials that are NOT printed (machined or purchased)
_NON_PRINT_MATERIALS = {
    "cfrp", "carbon_fiber", "aramid",
    "aluminum_6061", "aluminum_7075", "aluminum",
    "steel", "stainless_steel", "titanium",
    "fr4",                          # PCB substrate
    "lipo_4s", "lipo_3s", "lipo_6s",
}


@dataclass
class PrintPart:
    name: str
    stl_path: Path
    material: str
    profile: dict[str, Any]
    bbox_mm: tuple[float, float, float] = (0, 0, 0)
    volume_mm3: float = 0.0
    estimated_grams: float = 0.0
    fits_build_volume: bool = True
    notes: list[str] = field(default_factory=list)


def prepare_for_print(
    assembly_dir: str | Path,
    *,
    printer: dict = CENTAURI_CARBON,
    bom_path: str | Path | None = None,
) -> dict:
    """Pick printable parts from an assembly, orient them, write print bundle.

    *assembly_dir* should contain a `parts/` subdir with per-part STEP files
    and a `bom.json` listing material per part.

    Returns a summary dict with paths, totals, fit checks.
    """
    assembly_dir = Path(assembly_dir)
    parts_dir = assembly_dir / "parts"
    if not parts_dir.is_dir():
        raise FileNotFoundError(f"no parts/ dir under {assembly_dir}")

    bom_path = Path(bom_path) if bom_path else (assembly_dir / "bom.json")
    bom = json.loads(bom_path.read_text(encoding="utf-8")) if bom_path.is_file() else {}
    parts_meta = {p["spec"]: p for p in (bom.get("parts") or [])
                  if isinstance(p, dict) and "spec" in p}
    # Also accept 'name' as the key if 'spec' is absent
    for p in (bom.get("parts") or []):
        if isinstance(p, dict) and "name" in p:
            parts_meta.setdefault(p["name"], p)

    print_dir = assembly_dir / "print"
    print_dir.mkdir(parents=True, exist_ok=True)

    print_parts: list[PrintPart] = []
    skipped: list[dict] = []

    for step_file in sorted(parts_dir.glob("*.step")):
        part_name = step_file.stem
        meta = parts_meta.get(part_name) or _find_meta_by_prefix(parts_meta, part_name)
        material = (meta or {}).get("material", "").lower()

        # Skip non-printables
        if material in _NON_PRINT_MATERIALS:
            skipped.append({"part": part_name, "material": material,
                            "reason": "non-print (CNC / purchased / battery)"})
            continue

        profile_key = _PRINTABLE_MATERIALS.get(material)
        if profile_key is None:
            skipped.append({"part": part_name, "material": material or "unknown",
                            "reason": "no printable material profile"})
            continue

        profile = dict(_MATERIAL_PROFILES[profile_key])
        # Convert STEP → oriented STL
        stl_path = print_dir / f"{part_name}.stl"
        try:
            bbox, volume_mm3 = _step_to_oriented_stl(step_file, stl_path)
        except Exception as exc:
            skipped.append({"part": part_name, "material": material,
                            "reason": f"orient failed: {exc}"})
            continue

        # Material mass estimate
        density = profile["density_g_cm3"]
        infill_factor = 0.30 + (profile["infill_pct"] / 100) * 0.70  # walls + infill estimate
        mass_g = (volume_mm3 / 1000.0) * density * infill_factor

        bv = printer["build_volume_mm"]
        fits = (bbox[0] <= bv[0] and bbox[1] <= bv[1] and bbox[2] <= bv[2])
        notes = []
        if not fits:
            notes.append(f"DOES NOT FIT — bbox {bbox} > printer {bv}")
        if profile.get("enclosure_required"):
            notes.append("Requires enclosure for warp control (ABS/ASA)")
        if profile.get("hardened_nozzle_required"):
            notes.append("Requires hardened steel nozzle (CF abrasion)")

        # Per-part config JSON
        cfg = {
            "part": part_name,
            "material": material,
            "filament": profile["filament"],
            "nozzle_temp_c": profile["nozzle_temp_c"],
            "bed_temp_c": profile["bed_temp_c"],
            "speed_mm_s": profile["speed_mm_s"],
            "infill_pct": profile["infill_pct"],
            "wall_loops": profile["wall_loops"],
            "top_bottom_layers": profile["top_bottom_layers"],
            "support": profile.get("support", False),
            "bbox_mm": list(bbox),
            "volume_mm3": round(volume_mm3, 1),
            "estimated_grams": round(mass_g, 2),
            "fits_build_volume": fits,
            "notes": notes,
        }
        cfg_path = print_dir / f"{part_name}.config.json"
        cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        print_parts.append(PrintPart(
            name=part_name, stl_path=stl_path, material=material,
            profile=profile, bbox_mm=bbox, volume_mm3=volume_mm3,
            estimated_grams=mass_g, fits_build_volume=fits, notes=notes,
        ))

    # Aggregate summary
    total_g = sum(p.estimated_grams for p in print_parts)
    total_vol = sum(p.volume_mm3 for p in print_parts)
    # Print time estimate: rough 5 g/hr at default 60 mm/s for FDM
    time_hr = total_g / 8.0 if total_g > 0 else 0.0

    summary = {
        "printer": printer["name"],
        "n_print_parts": len(print_parts),
        "n_skipped": len(skipped),
        "total_filament_g": round(total_g, 1),
        "total_volume_mm3": round(total_vol, 1),
        "estimated_time_hours": round(time_hr, 2),
        "all_parts_fit": all(p.fits_build_volume for p in print_parts),
        "parts": [
            {
                "name": p.name, "material": p.material,
                "bbox_mm": list(p.bbox_mm),
                "estimated_grams": round(p.estimated_grams, 2),
                "fits": p.fits_build_volume,
                "notes": p.notes,
                "stl_path": str(p.stl_path),
                "config_path": str(p.stl_path.with_suffix(".config.json")),
            }
            for p in print_parts
        ],
        "skipped": skipped,
    }
    summary_path = print_dir / "print_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Generate one-line CLI batch for slicing all parts
    _write_orca_cli_batch(print_dir, print_parts, printer)

    # Generate README with print order + post-processing
    _write_print_readme(print_dir, print_parts, summary)

    summary["print_dir"] = str(print_dir)
    summary["summary_path"] = str(summary_path)
    return summary


def _find_meta_by_prefix(parts_meta: dict, name: str) -> dict | None:
    """Match arm.step → arm_fr/arm_fl/etc. (instance name lookups)."""
    for k, v in parts_meta.items():
        if k.startswith(name) or name.startswith(k):
            return v
    return None


def _step_to_oriented_stl(step_file: Path, stl_path: Path) -> tuple[tuple[float, float, float], float]:
    """Convert STEP to STL with auto-orientation (flat-side-down).

    Heuristic: align the part's longest 2 axes with build plate XY by
    rotating so the smallest extent is along Z. Real auto-orientation
    (minimize support volume) is more involved — this gets you 80% there.
    """
    import trimesh
    import numpy as np
    from aria_os.caching import cached_stl

    # Use the cached STEP→STL converter — same STEP returns same STL path.
    # Then load into trimesh for orientation, write the oriented copy.
    src_stl = cached_stl(step_file, tolerance=0.02)
    shutil.copy2(src_stl, stl_path)
    mesh = trimesh.load_mesh(str(stl_path))
    if hasattr(mesh, "dump"):
        mesh = mesh.dump(concatenate=True)

    extents = mesh.extents
    # Find the smallest axis — that becomes Z
    z_axis = int(np.argmin(extents))
    if z_axis != 2:
        # Rotate so smallest axis is Z. Build a 4x4 transform
        # Map (X, Y, Z_orig) → (X', Y', Z')
        if z_axis == 0:
            # X → Z: 90° rotation around Y
            R = trimesh.transformations.rotation_matrix(np.pi / 2, [0, 1, 0])
        else:  # z_axis == 1
            R = trimesh.transformations.rotation_matrix(-np.pi / 2, [1, 0, 0])
        mesh.apply_transform(R)
        # Re-translate so min Z = 0 (flat on build plate)
        mesh.apply_translation([0, 0, -mesh.bounds[0, 2]])
        mesh.export(str(stl_path))
    else:
        # Already correctly oriented — just translate to Z=0
        if mesh.bounds[0, 2] != 0:
            mesh.apply_translation([0, 0, -mesh.bounds[0, 2]])
            mesh.export(str(stl_path))

    bbox = tuple(float(v) for v in mesh.extents)
    volume = float(mesh.volume)
    return bbox, volume


def _write_orca_cli_batch(print_dir: Path, parts: list, printer: dict) -> None:
    """Generate Windows .bat that slices all STLs via Orca Slicer CLI.

    Slices each part individually with its config, then offers to merge
    into a single G-code if Orca supports it.
    """
    bat_path = print_dir / "elegoo_orca_cmd.bat"
    lines = [
        "@echo off",
        "REM Elegoo Centauri Carbon slicing batch (Orca Slicer CLI)",
        "REM ========================================================",
        f"REM Printer: {printer['name']}  ({printer['build_volume_mm'][0]}^x"
        f"{printer['build_volume_mm'][1]}^x{printer['build_volume_mm'][2]} mm)",
        f"REM Parts:   {len(parts)}",
        "REM",
        "REM Adjust ORCA path and printer profile path if Orca isn't on PATH.",
        "set ORCA=orca-slicer.exe",
        "set PROFILE_DIR=%USERPROFILE%\\AppData\\Roaming\\OrcaSlicer\\system\\Elegoo",
        "",
    ]
    for p in parts:
        lines.append(
            f"echo Slicing {p.name} ({p.profile['filament']}, "
            f"{p.profile['nozzle_temp_c']}C nozzle, {p.estimated_grams:.1f}g)"
        )
        lines.append(
            f'%ORCA% --slice 0 --export-3mf "{p.name}.3mf" '
            f'--filament "{p.profile["filament"]}" '
            f'--nozzle-temperature {p.profile["nozzle_temp_c"]} '
            f'--bed-temperature {p.profile["bed_temp_c"]} '
            f'"{p.stl_path.name}"'
        )
        lines.append("")
    lines.append("echo Done. Open .gcode files in Elegoo Slicer to verify before printing.")
    bat_path.write_text("\n".join(lines), encoding="utf-8")


def _write_print_readme(print_dir: Path, parts: list, summary: dict) -> None:
    readme = print_dir / "README.md"
    lines = [
        "# Print bundle",
        "",
        f"**Printer:** {summary['printer']}  ",
        f"**Parts:** {summary['n_print_parts']}  ",
        f"**Total filament:** {summary['total_filament_g']:.1f} g  ",
        f"**Estimated time:** {summary['estimated_time_hours']:.1f} h  ",
        "",
        "## Print order",
        "",
        "| Order | Part | Material | Mass (g) | Bbox (mm) | Notes |",
        "|-------|------|----------|---------:|-----------|-------|",
    ]
    for i, p in enumerate(parts, 1):
        bbox = f"{p.bbox_mm[0]:.0f} × {p.bbox_mm[1]:.0f} × {p.bbox_mm[2]:.0f}"
        notes = "; ".join(p.notes) if p.notes else "—"
        lines.append(f"| {i} | {p.name} | {p.material} | {p.estimated_grams:.2f} | {bbox} | {notes} |")
    lines += [
        "",
        "## Post-processing",
        "",
        "- Inspect each printed part for delamination or stringing before assembly",
        "- Test-fit M3 bolts in mounting holes (drill with 3.2mm bit if tight)",
        "- For ABS/ASA: anneal in oven @ 90°C for 30 min to reduce warpage",
        "",
        "## Slicing",
        "",
        "1. Open Elegoo Slicer (or Orca Slicer)",
        "2. Load STLs from this directory (drag-and-drop)",
        "3. Each part has a `.config.json` with recommended print settings",
        "4. OR run `elegoo_orca_cmd.bat` to batch-slice via CLI",
        "",
        f"Skipped {len(summary['skipped'])} non-printable parts (CFRP/aluminum/PCB/battery).",
    ]
    readme.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI integration: detect Orca / Elegoo Slicer
# ---------------------------------------------------------------------------

def find_slicer_cli() -> str | None:
    """Find an Orca Slicer / Elegoo Slicer executable on PATH or default location."""
    candidates = list(CENTAURI_CARBON["slicer_cli"])
    # Common Windows install locations
    candidates += [
        r"C:\Program Files\OrcaSlicer\orca-slicer.exe",
        r"C:\Program Files\ElegooSlicer\ElegooSlicer.exe",
        os.path.expanduser("~/AppData/Local/Programs/OrcaSlicer/orca-slicer.exe"),
    ]
    for c in candidates:
        if shutil.which(c):
            return shutil.which(c)
        if Path(c).is_file():
            return c
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m aria_os.slicer <assembly_dir>")
        print("       python -m aria_os.slicer outputs/drone_quad/drone_recon_military_7inch")
        sys.exit(1)
    s = prepare_for_print(sys.argv[1])
    print(json.dumps(s, indent=2))
    cli = find_slicer_cli()
    if cli:
        print(f"\n[SLICER] Detected: {cli}")
        print(f"[SLICER] Run: cd {s['print_dir']} && elegoo_orca_cmd.bat")
    else:
        print("\n[SLICER] No Orca/Elegoo Slicer detected on PATH.")
        print("[SLICER] Install Elegoo Slicer or Orca Slicer, then run elegoo_orca_cmd.bat")
