"""
assemble.py — General assembly builder.
Reads a JSON assembly config, loads STEP files, positions them in 3D space,
and exports a combined STEP.

Assembly JSON format:
    {
      "name": "My Assembly",
      "parts": [
        {"id": "part_id", "step": "outputs/cad/step/part.step", "pos": [x,y,z], "rot": [rx,ry,rz]},
        ...
      ]
    }

Parts may include a ``depends_on`` key and an ``offset`` key so that their
position is resolved relative to another part at build time:

    {"id": "center_pinion", "step": "...", "depends_on": "center_arbor", "offset": [0, 0, 12], "rot": [0,0,0]}

Usage:
    python assemble.py assembly.json
    python assemble.py assembly.json --output outputs/cad/step/my_assembly.step
    python assemble.py assembly.json --preview       (open STL in preview after building)
    python assemble.py assembly.json --no-clearance  (skip post-assembly clearance check)

Rotation values are in degrees, applied as Rx then Ry then Rz (intrinsic XYZ Euler).
"""
import sys
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

OUT_STEP = ROOT / "outputs" / "cad" / "step"


def _resolve_path(raw: str, config_dir: Path) -> Path:
    """
    Resolve a STEP path.  Three forms supported:
      component:<key>        — fetch/generate from registry (e.g. "component:nema17")
      absolute path          — used as-is
      relative path          — resolved vs repo root then config dir
    """
    # component: prefix → auto-fetch from registry
    if raw.startswith("component:"):
        key = raw[len("component:"):]
        from fetch_component import fetch
        return fetch(key)

    p = Path(raw)
    if p.is_absolute():
        return p
    # Try relative to repo root first
    candidate = ROOT / p
    if candidate.exists():
        return candidate
    # Try relative to the config file's directory
    candidate2 = config_dir / p
    if candidate2.exists():
        return candidate2
    # Return the repo-root-relative path even if it doesn't exist yet
    return candidate


def _fmt_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n/1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n/1024:.1f} KB"
    return f"{n} B"


def build_assembly(
    config_path: Path,
    output_path: Path | None = None,
    open_preview: bool = False,
    run_clearance: bool = True,
    min_clearance_mm: float = 0.5,
) -> Path:
    """
    Load all parts from the assembly config, position them, and export combined STEP.
    Returns the path to the exported STEP file.
    """
    try:
        import cadquery as cq
        from cadquery import exporters
    except ImportError:
        print("[assemble] Error: cadquery is not installed.")
        print("           Run: pip install cadquery==2.7.0")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as fh:
        config = json.load(fh)

    assembly_name = config.get("name", config_path.stem)
    parts_cfg = config.get("parts", [])
    config_dir = config_path.parent

    # Resolve depends_on references before anything else
    from aria_os.assembler import resolve_depends_on
    parts_cfg = resolve_depends_on(parts_cfg)

    if not parts_cfg:
        print(f"[assemble] Warning: no parts defined in '{config_path}'")

    if output_path is None:
        import re as _re
        safe_name = _re.sub(r"[^\w\-]+", "_", assembly_name).strip("_").lower()
        output_path = OUT_STEP / f"{safe_name}.step"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[assemble] Building: {assembly_name}")
    print(f"[assemble] Output:   {output_path}\n")

    # We accumulate solids into a CadQuery Workplane compound
    combined: cq.Workplane | None = None
    loaded_count = 0
    failed_count = 0

    for i, part_cfg in enumerate(parts_cfg):
        part_id  = part_cfg.get("id", f"part_{i}")
        step_raw = part_cfg.get("step", "")
        pos      = part_cfg.get("pos", [0.0, 0.0, 0.0])
        rot      = part_cfg.get("rot", [0.0, 0.0, 0.0])

        if not step_raw:
            print(f"  [{i+1:3d}] [SKIP] {part_id:30s}  no 'step' path provided")
            failed_count += 1
            continue

        step_path = _resolve_path(step_raw, config_dir)

        if not step_path.exists():
            print(f"  [{i+1:3d}] [MISS] {part_id:30s}  not found: {step_path}")
            failed_count += 1
            continue

        # Load the STEP file
        try:
            solid = cq.importers.importStep(str(step_path))
        except Exception as exc:
            print(f"  [{i+1:3d}] [ERR ] {part_id:30s}  load failed: {exc}")
            failed_count += 1
            continue

        # Apply rotations (degrees, intrinsic XYZ order)
        rx, ry, rz = float(rot[0]), float(rot[1]), float(rot[2])
        if rx != 0.0:
            solid = solid.rotate((0, 0, 0), (1, 0, 0), rx)
        if ry != 0.0:
            solid = solid.rotate((0, 0, 0), (0, 1, 0), ry)
        if rz != 0.0:
            solid = solid.rotate((0, 0, 0), (0, 0, 1), rz)

        # Apply translation
        tx, ty, tz = float(pos[0]), float(pos[1]), float(pos[2])
        if tx != 0.0 or ty != 0.0 or tz != 0.0:
            solid = solid.translate((tx, ty, tz))

        sz = _fmt_size(step_path.stat().st_size)
        print(f"  [{i+1:3d}] [OK  ] {part_id:30s}  pos={pos}  rot={rot}  ({sz})")

        # Merge into combined workplane
        if combined is None:
            combined = solid
        else:
            # Add each solid to a compound via union-friendly approach
            combined = combined.add(solid)

        loaded_count += 1

    if combined is None or loaded_count == 0:
        print(f"\n[assemble] No parts loaded — nothing to export.")
        sys.exit(1)

    # Export combined STEP
    print(f"\n[assemble] Exporting {loaded_count} part(s) to STEP...")
    try:
        exporters.export(combined, str(output_path), exporters.ExportTypes.STEP)
    except Exception as exc:
        print(f"[assemble] Error exporting STEP: {exc}")
        sys.exit(1)

    step_size = _fmt_size(output_path.stat().st_size)
    print(f"[assemble] Done. STEP: {output_path}  ({step_size})")

    if failed_count:
        print(f"[assemble] Warning: {failed_count} part(s) failed to load")

    # Post-assembly clearance check
    if run_clearance:
        try:
            from aria_os.clearance_checker import check_clearance, print_clearance_table
            result = check_clearance(
                parts_cfg,
                min_clearance_mm=min_clearance_mm,
                proximity_threshold_mm=100.0,
            )
            print_clearance_table(result, min_clearance_mm=min_clearance_mm)
        except ImportError:
            print("[assemble] Clearance check skipped (trimesh not installed).")
        except Exception as exc:
            print(f"[assemble] Clearance check failed: {exc}")

    # Optionally open preview
    if open_preview:
        # Export a temporary STL for preview
        stl_path = output_path.with_suffix(".stl")
        try:
            exporters.export(combined, str(stl_path), exporters.ExportTypes.STL)
            from aria_os.preview_ui import show_preview
            print(f"\n[assemble] Opening preview for {output_path.stem}...")
            show_preview(str(stl_path), part_id=output_path.stem)
        except Exception as exc:
            print(f"[assemble] Preview failed: {exc}")

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assembly builder — combines positioned STEP files into a single STEP."
    )
    parser.add_argument("config", type=Path, help="Path to assembly JSON config file")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="PATH",
        help="Output STEP path (default: outputs/cad/step/<assembly_name>.step)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Open the assembled model in the 3D STL preview after building",
    )
    parser.add_argument(
        "--no-clearance",
        action="store_true",
        help="Skip the post-assembly clearance check",
    )
    parser.add_argument(
        "--min-clearance",
        type=float,
        default=0.5,
        metavar="MM",
        help="Minimum acceptable clearance in mm (default: 0.5)",
    )
    args = parser.parse_args()

    config_path = args.config
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    if not config_path.exists():
        print(f"[assemble] Error: config file not found: {config_path}")
        sys.exit(1)

    output_path = args.output
    if output_path is not None and not output_path.is_absolute():
        output_path = ROOT / output_path

    build_assembly(
        config_path=config_path,
        output_path=output_path,
        open_preview=args.preview,
        run_clearance=not args.no_clearance,
        min_clearance_mm=args.min_clearance,
    )


if __name__ == "__main__":
    main()
