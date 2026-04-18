#!/usr/bin/env python3
"""ARIA-OS CLI: python run_aria_os.py \"describe the part you want\"
  --list                List all generated parts with file sizes and validation status
  --validate            Re-validate all existing STEP outputs (size + re-import)
  --verify [path]       Visual + geometric verify: DXF/ECAD/STL checks + PNG renders (all outputs/ if no path)
  --modify              Modify existing part: --modify <path_to_.py> \"modification description\"
  --assemble            Create assembly from JSON: --assemble assembly_configs/foo.json
  --assembly            Describe a multi-part assembly: --assembly \"baseplate with bracket bolted to it\"
  --scenario            Interpret a real-world situation and generate all needed parts
  --scenario-dry-run    Show parts list for a scenario without generating
  --system              Two-pass whole-machine design: decompose to subsystems, expand to parts, generate all
  --system-dry-run      Show full subsystem + parts breakdown without generating
"""
import sys
import os
import json
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout on Windows (avoids UnicodeEncodeError for σ, ⁴, · etc.)
# line_buffering=True ensures print() flushes on every newline, even when
# stdout is connected to a pipe instead of a terminal. Without this the
# dashboard's subprocess wrapper drops everything written to stdout when
# the script exits quickly, because reconfigure() defaults line_buffering
# to False which defeats `python -u`.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# Diagnostic: prove stdout capture works for the dashboard subprocess wrapper.
# Remove once /api/run/<id>.lines is reliably non-empty.
print(f"[STARTUP] run_aria_os.py PID={os.getpid()} argv={sys.argv[1:]}", flush=True)

# Repo root
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Load .env — must happen before any aria_os imports so API keys are available
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


def run_modify(base_part_path: str, modification: str):
    """Run PartModifier and print result + geometry stats."""
    from aria_os.modifier import PartModifier
    from aria_os.context_loader import load_context
    mod = PartModifier(repo_root=ROOT)
    context = load_context(ROOT)
    result = mod.modify(base_part_path, modification, context=context)
    if result.passed:
        print("Modification passed.")
        if result.bbox:
            print(f"BBOX: {result.bbox[0]:.2f} x {result.bbox[1]:.2f} x {result.bbox[2]:.2f} mm")
    else:
        print("Modification failed:", result.error)
    return result


def run_assemble(config_path: str):
    """Load JSON config and build assembly via assemble.py (handles component: prefix, pos/rot keys)."""
    from assemble import build_assembly
    path = ROOT / config_path if not Path(config_path).is_absolute() else Path(config_path)
    if not path.exists():
        print(f"Config not found: {path}")
        sys.exit(1)
    out_path = build_assembly(config_path=path, open_preview=False)
    print(f"Assembly exported: {out_path}")


def list_parts():
    """List all .step files in outputs/cad/step with size, validation, and version info."""
    import json as _json
    step_dir = ROOT / "outputs" / "cad" / "step"
    if not step_dir.exists():
        print("No outputs/cad/step directory.")
        return
    from aria_os.validator import validate_step_file
    steps = sorted(step_dir.glob("*.step"))
    if not steps:
        print("No STEP files found.")
        return
    print(f"{'Part':<28} {'STEP':<10} {'STL':<10} {'Valid':<6} {'CEM SF':<8} {'Generated':<20} {'SHA'}")
    print("-" * 100)
    stl_dir  = ROOT / "outputs" / "cad" / "stl"
    meta_dir = ROOT / "outputs" / "cad" / "meta"
    for p in steps:
        name = p.stem
        step_kb   = p.stat().st_size / 1024
        stl_path  = stl_dir / (name + ".stl")
        meta_path = meta_dir / (name + ".json")
        stl_kb    = stl_path.stat().st_size / 1024 if stl_path.exists() else 0
        valid, count, errs = validate_step_file(p, min_size_kb=1.0)
        status = "OK" if valid else "FAIL"

        # Version tracking fields
        cem_sf  = ""
        gen_at  = ""
        git_sha = ""
        stale   = ""
        if meta_path.exists():
            try:
                _m = _json.loads(meta_path.read_text(encoding="utf-8"))
                _sf = _m.get("cem_sf")
                cem_sf  = f"{_sf:.2f}" if _sf is not None else "-"
                gen_at  = (_m.get("generated_at") or "")[:16]
                git_sha = _m.get("git_sha") or ""
                # Stale if STEP file is newer than meta (regenerated outside pipeline)
                _meta_mtime = meta_path.stat().st_mtime
                _step_mtime = p.stat().st_mtime
                stale = " [STALE]" if _step_mtime > _meta_mtime + 2 else ""
            except Exception:
                pass

        size_str = f"{step_kb:>6.0f}KB" if step_kb < 1024 else f"{step_kb/1024:>5.1f}MB"
        stl_str  = f"{stl_kb:>6.0f}KB" if stl_kb < 1024 else f"{stl_kb/1024:>5.1f}MB"
        print(f"{name+stale:<28} {size_str:<10} {stl_str:<10} {status:<6} {cem_sf:<8} {gen_at:<20} {git_sha}")


def validate_all():
    """Re-validate all STEP files: size >= 10 KB and re-import has >= 1 solid."""
    step_dir = ROOT / "outputs" / "cad" / "step"
    if not step_dir.exists():
        print("No outputs/cad/step directory.")
        return
    from aria_os.validator import validate_step_file
    steps = sorted(step_dir.glob("*.step"))
    if not steps:
        print("No STEP files found.")
        return
    all_ok = True
    for p in steps:
        valid, solid_count, errs = validate_step_file(p, min_size_kb=10.0)
        if valid:
            print(f"OK  {p.name}  (solids: {solid_count}, {p.stat().st_size/1024:.1f} KB)")
        else:
            all_ok = False
            print(f"FAIL {p.name}: {'; '.join(errs)}")
    sys.exit(0 if all_ok else 1)

def run_print_scale(args: list[str]):
    """
    Scale an existing STEP file for print-fit checks:
      python run_aria_os.py --print-scale <part_stub> --scale 0.75
    Requires Rhino Compute (RHINO_COMPUTE_URL env var, default http://localhost:6500).
    Reports dims + 256mm bed fit; writes scaled STEP/STL via Rhino Compute.
    """
    if len(args) < 1:
        print("Usage: python run_aria_os.py --print-scale <part_stub> --scale <factor>")
        sys.exit(1)
    part_stub = args[0]
    scale = 1.0
    i = 1
    while i < len(args):
        if args[i] == "--scale" and i + 1 < len(args):
            try:
                scale = float(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    from aria_os.exporter import get_output_paths

    paths = get_output_paths(part_stub, ROOT)
    step_path = Path(paths["step_path"])
    if not step_path.exists():
        step_dir = ROOT / "outputs" / "cad" / "step"
        matches = [p for p in step_dir.glob("*.step") if part_stub.lower() in p.stem.lower()]
        if matches:
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            step_path = matches[0]
        else:
            print(f"STEP not found for stub: {part_stub}")
            sys.exit(1)

    compute_url = (
        __import__("os").environ.get("RHINO_COMPUTE_URL", "http://localhost:6500").rstrip("/")
    )

    try:
        import requests  # type: ignore
        resp = requests.get(f"{compute_url}/version", timeout=5)
        resp.raise_for_status()
    except Exception as e:
        print(f"Rhino Compute not available at {compute_url}: {e}")
        print("To use --print-scale, start Rhino Compute and set RHINO_COMPUTE_URL.")
        print("See docs/rhino_compute_setup.md for setup instructions.")
        sys.exit(1)

    pct = int(round(scale * 100))
    out_base = f"{step_path.stem}_print_{pct}pct"
    out_step = ROOT / "outputs" / "cad" / "step" / f"{out_base}.step"
    out_stl = ROOT / "outputs" / "cad" / "stl" / f"{out_base}.stl"
    out_step.parent.mkdir(parents=True, exist_ok=True)
    out_stl.parent.mkdir(parents=True, exist_ok=True)

    # Build RhinoCommon scale script and post to Rhino Compute
    script = f"""
import rhinoscriptsyntax as rs
import Rhino.Geometry as rg
import Rhino

step_file = r"{step_path}"
out_step = r"{out_step}"
out_stl = r"{out_stl}"
scale = {scale}

objs = rs.Command('_-Import "' + step_file + '" _Enter', False)
all_objs = rs.AllObjects()
if all_objs:
    xform = rg.Transform.Scale(rg.Point3d.Origin, scale)
    for obj_id in all_objs:
        rs.TransformObject(obj_id, xform)
    bb_pts = [rs.BoundingBox([o]) for o in all_objs]
    all_pts = [pt for bb in bb_pts if bb for pt in bb]
    if all_pts:
        xs = [p.X for p in all_pts]
        ys = [p.Y for p in all_pts]
        zs = [p.Z for p in all_pts]
        xlen = max(xs) - min(xs)
        ylen = max(ys) - min(ys)
        zlen = max(zs) - min(zs)
        print(f"BBOX:{{xlen:.3f}},{{ylen:.3f}},{{zlen:.3f}}")
    rs.SelectObjects(all_objs)
    rs.Command(f'_-Export "{{out_step}}" _Enter', False)
    rs.Command(f'_-Export "{{out_stl}}" _Enter _Enter', False)
"""

    try:
        resp = requests.post(
            f"{compute_url}/grasshopper",
            json={"algo": script, "pointer": None, "values": []},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        stdout = str(data.get("stdout", ""))
        m = __import__("re").search(r"BBOX:([\d.]+),([\d.]+),([\d.]+)", stdout)
        scaled_dims = (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else (0, 0, 0)
    except Exception as e:
        print(f"Rhino Compute scale failed: {e}")
        sys.exit(1)

    bed_mm = 256.0
    fit = max(scaled_dims[0], scaled_dims[1]) <= bed_mm
    clearance = (bed_mm - max(scaled_dims[0], scaled_dims[1])) / 2.0

    print("=== Print Scale ===")
    print(f"Input STEP:  {step_path}")
    print(f"Scale:       {scale} ({pct}%)")
    print(f"Scaled dims: {scaled_dims[0]:.2f} x {scaled_dims[1]:.2f} x {scaled_dims[2]:.2f} mm")
    print(f"Output STEP: {out_step}")
    print(f"Output STL:  {out_stl}")
    print(f"Fits 256mm bed: {'YES' if fit else 'NO'} (clearance per side: {clearance:.2f} mm)")


def run_optimize(args: list[str]):
    """CLI entry for --optimize."""
    if len(args) < 1:
        print("Usage: python run_aria_os.py --optimize <code_or_stub> --goal <goal> [--constraint RULE ...] [--max-iter N]")
        sys.exit(1)
    code_stub = args[0]
    goal = "minimize_weight"
    constraints: list[str] = []
    max_iter = 20

    i = 1
    while i < len(args):
        tok = args[i]
        if tok == "--goal" and i + 1 < len(args):
            goal = args[i + 1]
            i += 2
        elif tok == "--constraint" and i + 1 < len(args):
            constraints.append(args[i + 1])
            i += 2
        elif tok in ("--max-iter", "--max_iter") and i + 1 < len(args):
            try:
                max_iter = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    # Resolve code path or stub to an actual file in outputs/cad/generated_code
    gen_dir = ROOT / "outputs" / "cad" / "generated_code"
    direct = Path(code_stub)
    if not direct.is_absolute():
        direct = (ROOT / code_stub).resolve()
    resolved_path: Path | None = None
    if direct.exists():
        resolved_path = direct
    else:
        # Search by substring in generated_code filenames
        if gen_dir.exists():
            matches: list[Path] = []
            for p in gen_dir.glob("*.py"):
                if code_stub.lower() in p.name.lower():
                    matches.append(p)
            if matches:
                matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                resolved_path = matches[0]
    if resolved_path is None:
        print(f"Could not find generated code matching: {code_stub!r}")
        if gen_dir.exists():
            print("Available generated code files:")
            for p in sorted(gen_dir.glob("*.py")):
                print(f"  - {p.name}")
        sys.exit(1)

    from aria_os.optimizer import PartOptimizer

    opt = PartOptimizer(repo_root=ROOT)
    result = opt.optimize(str(resolved_path), goal=goal, constraints=constraints, context=None, max_iterations=max_iter)
    print("=== Optimization Result ===")
    print(f"Part:        {result.part_name}")
    print(f"Goal:        {result.goal}")
    print(f"Constraints: {result.constraints}")
    print(f"Iterations:  {result.iterations}")
    print(f"Converged:   {result.converged}")
    print(f"Best score:  {result.best_score}")
    print(f"Best params: {result.best_params}")
    print(f"Best STEP:   {result.best_step_path}")
    print(result.summary)

def run_optimize_and_regenerate(args: list[str]):
    """CLI entry for --optimize-and-regenerate."""
    if len(args) < 1:
        print("Usage: python run_aria_os.py --optimize-and-regenerate <code_or_stub> --goal <goal> [--constraint RULE ...] [--material MATERIAL_ID] [--max-iter N]")
        sys.exit(1)
    code_stub = args[0]
    goal = "minimize_weight"
    constraints: list[str] = []
    material: str | None = None
    max_iter = 20

    i = 1
    while i < len(args):
        tok = args[i]
        if tok == "--goal" and i + 1 < len(args):
            goal = args[i + 1]
            i += 2
        elif tok == "--constraint" and i + 1 < len(args):
            constraints.append(args[i + 1])
            i += 2
        elif tok == "--material" and i + 1 < len(args):
            material = args[i + 1]
            i += 2
        elif tok in ("--max-iter", "--max_iter") and i + 1 < len(args):
            try:
                max_iter = int(args[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    # Resolve code path or stub to an actual file in outputs/cad/generated_code
    gen_dir = ROOT / "outputs" / "cad" / "generated_code"
    direct = Path(code_stub)
    if not direct.is_absolute():
        direct = (ROOT / code_stub).resolve()
    resolved_path: Path | None = None
    if direct.exists():
        resolved_path = direct
    else:
        if gen_dir.exists():
            matches: list[Path] = []
            for p in gen_dir.glob("*.py"):
                if code_stub.lower() in p.name.lower():
                    matches.append(p)
            if matches:
                matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                resolved_path = matches[0]
    if resolved_path is None:
        print(f"Could not find generated code matching: {code_stub!r}")
        if gen_dir.exists():
            print("Available generated code files:")
            for p in sorted(gen_dir.glob('*.py')):
                print(f"  - {p.name}")
        sys.exit(1)

    from aria_os.optimizer import PartOptimizer
    from aria_os.context_loader import load_context

    context = load_context(ROOT)
    opt = PartOptimizer(repo_root=ROOT)
    out = opt.optimize_and_regenerate(
        base_code_path=str(resolved_path),
        goal=goal,
        constraints=constraints,
        context=context,
        material=material,
        max_iterations=max_iter,
    )

    opt_result = out.get("optimization")
    print("=== Optimize + Regenerate Result ===")
    if opt_result is not None:
        print(f"Part:        {getattr(opt_result, 'part_name', '')}")
        print(f"Goal:        {getattr(opt_result, 'goal', '')}")
        print(f"Constraints: {getattr(opt_result, 'constraints', [])}")
        print(f"Iterations:  {getattr(opt_result, 'iterations', 0)}")
        print(f"Converged:   {getattr(opt_result, 'converged', False)}")
        print(f"Best params: {getattr(opt_result, 'best_params', {})}")
        print(f"Best STEP:   {getattr(opt_result, 'best_step_path', '')}")
    print(f"Recommended material: {out.get('recommended_material')}")
    gen = out.get("generation") or {}
    if gen:
        print(f"Generated STEP: {gen.get('step_path')}")
    print(out.get("summary", ""))


def run_cem_full():
    """Run CEM checks on all parts with meta JSON and print a rich report."""
    from aria_os.context_loader import load_context
    from aria_os import cem_checks
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel

    console = Console(highlight=False, emoji=False)
    context = load_context(ROOT)
    report = cem_checks.run_full_system_cem(ROOT / "outputs", context)

    total = report.get("total_parts", 0)
    passed = report.get("passed", 0)
    failed = report.get("failed", [])
    weakest_part = report.get("weakest_part")
    weakest_sf = report.get("weakest_sf")
    system_passed = report.get("system_passed", True)

    status_text = "OK ALL PARTS PASS" if system_passed else "[!] ATTENTION NEEDED"

    header = Panel.fit(
        f"ARIA SYSTEM CEM REPORT\n\n"
        f"Parts checked: {total}\n"
        f"Passed:        {passed}\n"
        f"Failed:        {len(failed)}\n"
        f"System status: {status_text}",
        title="ARIA CEM",
    )
    console.print(header)

    table = Table(show_header=True, header_style="bold")
    table.add_column("Part", style="cyan")
    table.add_column("Static SF", justify="right")
    table.add_column("Status", justify="center")

    results = report.get("results", {})
    for name, data in results.items():
        sf = data.get("static_min_sf")
        ok = data.get("overall_passed", False)
        status = "[OK] PASS" if ok else "[FAIL] FAIL"
        sf_str = f"{sf:.2f}" if sf is not None else "-"
        table.add_row(name, sf_str, status)

    console.print(table)

    if weakest_part:
        console.print(
            f"Weakest link: [bold]{weakest_part}[/bold] "
            f"({weakest_sf:.2f}x SF)" if weakest_sf is not None else f"Weakest link: {weakest_part}"
        )


def run_generate_and_assemble(description: str, into_path: str, part_label: str, at_vec: str, rot_vec: str | None = None):
    """Generate a part, append it to an assembly config, and re-run assembly."""
    from aria_os import run as orchestrator_run

    # 1. Generate part
    session = orchestrator_run(description, repo_root=ROOT)
    step_path_str = session.get("step_path")
    if not step_path_str:
        print("Generation did not produce a STEP path.")
        sys.exit(1)
    step_path = Path(step_path_str)
    if not step_path.exists():
        print(f"Generated STEP not found: {step_path}")
        sys.exit(1)

    # 2. Load assembly JSON
    cfg_path = ROOT / into_path if not Path(into_path).is_absolute() else Path(into_path)
    if not cfg_path.exists():
        print(f"Assembly config not found: {cfg_path}")
        sys.exit(1)
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # 3. Parse vectors
    def _parse_vec(txt: str) -> list[float]:
        parts = [p for p in txt.split(",") if p.strip()]
        if len(parts) != 3:
            raise ValueError(f"Expected 3 comma-separated values, got: {txt!r}")
        return [float(p) for p in parts]

    pos = _parse_vec(at_vec)
    rot = _parse_vec(rot_vec) if rot_vec else [0.0, 0.0, 0.0]

    # 4. Append new part entry
    rel_step = step_path
    try:
        rel_step = step_path.relative_to(ROOT)
    except ValueError:
        rel_step = step_path

    parts = cfg.get("parts", [])
    parts.append(
        {
            "name": part_label,
            "step_path": str(rel_step).replace("\\", "/"),
            "position": pos,
            "rotation": rot,
            "notes": "auto-added by --generate-and-assemble",
        }
    )
    cfg["parts"] = parts
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    # 5. Re-run assembly
    run_assemble(str(cfg_path))


def run_material_study_cli(part_stub: str):
    """CLI entry for --material-study."""
    from rich.console import Console
    from rich.table import Table
    from aria_os.context_loader import load_context
    from aria_os.material_study import run_material_study

    console = Console(highlight=False, emoji=False)
    context = load_context(ROOT)
    outputs_dir = ROOT / "outputs"
    result = run_material_study(part_stub, context, outputs_dir)

    console.print(f"[bold]Material study for[/bold] {result.part_name} (criticality: {result.part_criticality}, SF target={result.sf_target:.1f}x)")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Rank", justify="right")
    table.add_column("Material")
    table.add_column("SF", justify="right")
    table.add_column("Weight [g]", justify="right")
    table.add_column("Rel Cost", justify="right")
    table.add_column("Mach", justify="right")
    table.add_column("Verdict")

    for r in result.ranked_results:
        table.add_row(
            str(r.rank),
            r.material.id,
            f"{r.sf:.2f}",
            f"{r.weight_g:.0f}",
            f"{r.relative_cost:.2f}",
            f"{r.machinability:.1f}",
            r.verdict,
        )

    console.print(table)
    console.print(f"[bold]Recommendation:[/bold] {result.recommendation.id} - {result.recommendation_reasoning}")
    console.print(f"Baseline material rank: {result.current_material_rank}")


def run_material_study_all_cli():
    from aria_os.context_loader import load_context
    from aria_os.material_study import run_material_study_all
    from rich.console import Console
    from rich.table import Table

    console = Console(highlight=False, emoji=False)
    context = load_context(ROOT)

    console.print("\n[bold]Running material studies on all parts...[/bold]\n")

    report = run_material_study_all(context, ROOT / "outputs")
    if "error" in report:
        console.print(f"[red]Error: {report['error']}[/red]")
        return

    table = Table(title="ARIA Material Study - All Parts")
    table.add_column("Part", style="cyan", width=36)
    table.add_column("Criticality", width=13)
    table.add_column("Recommended", style="green", width=17)
    table.add_column("SF", justify="right", width=6)
    table.add_column("Current", width=12)
    table.add_column("Action", width=8)

    for row in report["summary"]:
        action_style = "green" if row["action"] == "OK" else "red"
        table.add_row(
            row["part"],
            row["criticality"],
            row["recommended"],
            f"{row['recommended_sf']:.2f}",
            row["current"],
            f"[{action_style}]{row['action']}[/{action_style}]",
        )

    console.print(table)
    console.print(f"\nFull results saved to: {report['output_file']}")


def run_lattice_test():
    """Quick test to verify Blender pipeline works."""
    from rich.console import Console
    from aria_os.lattice.blender_pipeline import find_blender

    console = Console(highlight=False, emoji=False)

    blender = find_blender()
    if blender is None:
        console.print("[FAIL] Blender not found.")
        console.print("Install from: https://www.blender.org/download/")
        console.print("Then run: python run_aria_os.py --lattice-test")
        return

    console.print(f"[OK] Blender found: {blender}")
    console.print("Running quick geometry test...")

    from aria_os.lattice import generate_lattice, LatticeParams

    params = LatticeParams(
        pattern="honeycomb",
        form="volumetric",
        width_mm=40,
        height_mm=40,
        depth_mm=5,
        cell_size_mm=10,
        strut_diameter_mm=2.0,
        frame_thickness_mm=3.0,
        process="fdm",
        part_name="lattice_test_honeycomb",
    )

    try:
        result = generate_lattice(params)
        console.print(f"[OK] Honeycomb: {result.summary}")
        console.print(f"     STL: {result.stl_path}")
    except Exception as e:
        console.print(f"[FAIL] Honeycomb: {e}")

    params.pattern = "arc_weave"
    params.part_name = "lattice_test_arc_weave"
    try:
        result = generate_lattice(params)
        console.print(f"[OK] Arc weave: {result.summary}")
    except Exception as e:
        console.print(f"[FAIL] Arc weave: {e}")

    params.pattern = "octet_truss"
    params.width_mm = 30
    params.height_mm = 30
    params.depth_mm = 30
    params.cell_size_mm = 15
    params.part_name = "lattice_test_octet"
    try:
        result = generate_lattice(params)
        console.print(f"[OK] Octet truss: {result.summary}")
    except Exception as e:
        console.print(f"[FAIL] Octet truss: {e}")


def run_lattice(args: list[str]):
    """
    CLI entry for lattice generation via Fusion 360 (primary) or Blender (fallback).
      python run_aria_os.py --lattice --pattern honeycomb --form volumetric ...
      python run_aria_os.py --lattice --pattern gyroid --backend blender ...
    """
    from rich.console import Console

    console = Console(highlight=False, emoji=False)
    backend = "sdf"  # default: fastest, no external deps
    if "--backend" in args:
        try:
            backend = args[args.index("--backend") + 1]
        except IndexError:
            pass

    def get_arg(flag: str, default: str) -> str:
        try:
            idx = args.index(flag)
            return args[idx + 1]
        except (ValueError, IndexError):
            return default

    def get_bool_arg(flag: str, default: bool = False) -> bool:
        if flag in args:
            return True
        neg = f"--no-{flag.lstrip('-')}"
        if neg in args:
            return False
        return default

    pattern   = get_arg("--pattern", "honeycomb")
    form      = get_arg("--form", "volumetric")
    width_mm  = float(get_arg("--width",     "100"))
    height_mm = float(get_arg("--height",    "100"))
    depth_mm  = float(get_arg("--depth",     "10"))
    cell_size = float(get_arg("--cell-size", "10"))
    wall_mm   = float(get_arg("--strut",     "1.5"))
    part_name = get_arg("--name", "lattice_panel")

    if backend in ("sdf", "numpy"):
        # Fastest: pure numpy SDF + marching cubes
        from pathlib import Path as _P
        from aria_os.generators.sdf_generator import SDFScene, sdf_gyroid, sdf_schwarz_p, sdf_diamond, sdf_lattice_cubic, sdf_box, op_intersection
        ROOT = _P(__file__).resolve().parent
        out_stl = str(ROOT / "outputs" / "cad" / "stl" / f"{part_name}.stl")
        _P(out_stl).parent.mkdir(parents=True, exist_ok=True)

        scene = SDFScene(resolution=max(0.5, cell_size / 10))
        tpms_map = {"gyroid": sdf_gyroid, "schwarz_p": sdf_schwarz_p, "diamond": sdf_diamond,
                     "honeycomb": sdf_lattice_cubic, "octet": sdf_lattice_cubic, "arc_weave": sdf_gyroid}
        tpms_fn = tpms_map.get(pattern, sdf_gyroid)
        lattice = tpms_fn(cell_size=cell_size, thickness=wall_mm) if pattern in ("gyroid", "schwarz_p", "diamond") else tpms_fn(cell_size=cell_size, beam_radius=wall_mm / 2)

        # Clip to bounding box
        bounding = sdf_box(center=(width_mm/2, height_mm/2, depth_mm/2), size=(width_mm, height_mm, depth_mm))
        result = op_intersection(bounding, lattice)

        import time
        t0 = time.time()
        bounds = ((0, 0, 0), (width_mm, height_mm, depth_mm))
        mesh = scene.to_mesh(result, bounds)
        stl_path = scene.export_stl(mesh, out_stl)
        stats = scene.mesh_stats(mesh)
        elapsed = time.time() - t0

        dims = stats["dimensions_mm"]
        console.print(f"\n[SDF] {pattern} lattice: {dims[0]:.0f}x{dims[1]:.0f}x{dims[2]:.0f}mm")
        console.print(f"  Mesh: {stats['vertices']} verts, {stats['faces']} faces")
        console.print(f"  Time: {elapsed:.1f}s")
        console.print(f"  STL:  {stl_path}")
        return

    if backend == "fusion":
        # Generate Fusion 360 script via Design Extension lattice
        from pathlib import Path as _P
        from aria_os.fusion_generator import write_fusion_artifacts
        ROOT = _P(__file__).resolve().parent
        out_step = str(ROOT / "outputs" / "cad" / "step" / f"{part_name}.step")
        out_stl  = str(ROOT / "outputs" / "cad" / "stl"  / f"{part_name}.stl")
        _P(out_step).parent.mkdir(parents=True, exist_ok=True)
        _P(out_stl).parent.mkdir(parents=True, exist_ok=True)
        goal = (f"{pattern} {form} lattice {width_mm}x{height_mm}x{depth_mm}mm "
                f"cell {cell_size}mm wall {wall_mm}mm")
        plan = {
            "part_id": part_name,
            "params": {
                "width_mm": width_mm, "height_mm": height_mm,
                "depth_mm": depth_mm, "cell_size_mm": cell_size,
                "wall_mm": wall_mm, "pattern": pattern,
            },
        }
        result_paths = write_fusion_artifacts(plan, goal, out_step, out_stl)
        console.print(f"\n[FUSION] Lattice script generated.")
        console.print(f"  Script: {result_paths['script_path']}")
        console.print(f"  Mode  : {result_paths.get('fusion_mode', 'lattice')}")
        console.print(f"\n  Run in Fusion 360: Utilities > Scripts and Add-Ins > Run Script")
        console.print(f"  Requires: Fusion 360 Design Extension")
        return

    # Fallback: Blender headless pipeline
    from aria_os.lattice import generate_lattice, LatticeParams
    params = LatticeParams(
        pattern=pattern,
        form=form,
        width_mm=width_mm,
        height_mm=height_mm,
        depth_mm=depth_mm,
        cell_size_mm=cell_size,
        strut_diameter_mm=wall_mm,
        skin_thickness_mm=float(get_arg("--skin", "2.0")),
        frame_thickness_mm=float(get_arg("--frame", "5.0")),
        interlaced=get_bool_arg("--interlaced", default=False),
        weave_offset_mm=float(get_arg("--weave-offset", "0.0")),
        process=get_arg("--process", "both"),
        part_name=part_name,
    )
    console.print(f"\nGenerating {params.pattern} {params.form} lattice (Blender)...")
    result = generate_lattice(params)
    for w in result.process_warnings:
        console.print(f"  [WARN] {w}")
    console.print(f"\n[DONE] {result.summary}")
    console.print(f"  STEP: {result.step_path}")
    console.print(f"  STL:  {result.stl_path}")
    console.print(f"  Cells: {result.cell_count}")
    console.print(f"  Min feature: {result.min_feature_mm}mm")
    console.print(f"  Est. weight: {result.estimated_weight_g:.1f}g")
    if result.passed_process_check:
        console.print("  Process check: PASS")
    else:
        console.print("  Process check: FAIL - see warnings")


def _run_full(goal: str) -> None:
    """
    --full mode: generate → FEA → GD&T drawing → PNG render → CAM script + setup sheet.
    All outputs are produced in one shot without any interactive prompts.
    """
    from pathlib import Path as _Path
    _root = _Path(__file__).resolve().parent

    print(f"\n{'='*64}")
    print(f"  FULL PIPELINE  —  {goal}")
    print(f"{'='*64}\n")

    # 1. Generate (auto_draw=True so drawing is made without interactive prompt)
    from aria_os import run as _ario_run
    session = _ario_run(goal, repo_root=_root, auto_draw=True)
    if not isinstance(session, dict):
        print("[FULL] Generation returned no session — aborting.")
        return

    step = session.get("step_path", "")
    stl  = session.get("stl_path", "")
    params = session.get("params") or {}
    part_id = session.get("part_id") or goal.split()[0]

    # 2. FEA (if not already run by orchestrator)
    if step and _Path(step).exists() and not session.get("physics_analysis"):
        print("\n[FULL] Running FEA...")
        try:
            from aria_os.physics_analyzer import analyze as _phys
            _r = _phys(part_id=part_id, analysis_type="auto", params=params, goal=goal, repo_root=_root)
            print(_r["report"])
            sf = _r.get("safety_factor")
            print(f"[FULL] FEA: {'PASS' if _r['passed'] else 'FAIL'}" + (f"  SF={sf:.2f}" if sf else ""))
        except Exception as e:
            print(f"[FULL] FEA skipped: {e}")

    # 2b. Instant quote (after FEA, before drawing)
    if step and _Path(step).exists():
        print("\n[FULL] Generating instant quote...")
        try:
            from aria_os.agents.quote_agent import QuoteAgent, run_quote_cli
            _q_mat = params.get("material", "aluminium_6061")
            run_quote_cli(step, material=_q_mat, process="cnc", quantity=1)
        except Exception as e:
            print(f"[FULL] Quote skipped: {e}")

    # 3. GD&T drawing (if not already made by auto_draw)
    if step and _Path(step).exists() and not session.get("drawing_path"):
        print("\n[FULL] Generating GD&T drawing...")
        try:
            from aria_os.drawing_generator import generate_gdnt_drawing
            _svg = generate_gdnt_drawing(step, part_id, params=params, repo_root=_root)
            print(f"[FULL] Drawing: {_svg}")
        except Exception as e:
            print(f"[FULL] Drawing skipped: {e}")

    # 4. PNG render
    if stl and _Path(stl).exists():
        print("\n[FULL] Rendering PNG preview...")
        try:
            from batch import _render_stl, OUT_SHOTS
            _slug = _Path(stl).stem
            _png = OUT_SHOTS / f"{_slug}.png"
            _err = _render_stl(stl, _png)
            if _err:
                print(f"[FULL] Render WARN: {_err}")
            else:
                print(f"[FULL] Preview: {_png}")
        except Exception as e:
            print(f"[FULL] Render skipped: {e}")

    # 5. CAM script + setup sheet (requires STEP) — uses autonomous CAM agent
    if step and _Path(step).exists():
        print("\n[FULL] Running autonomous CAM agent...")
        _mat = params.get("material", "aluminium_6061")
        _machine = "generic_vmc"
        if "--machine" in sys.argv:
            _machine = sys.argv[sys.argv.index("--machine") + 1]
        try:
            from aria_os.agents.cam_agent import run_cam_agent
            _cam_result = run_cam_agent(
                step, material=_mat, machine=_machine, repo_root=_root)
            if _cam_result.get("passed"):
                print(f"[FULL] CAM agent: PASS — {_cam_result.get('cycle_time_min', '?')} min cycle")
            else:
                print(f"[FULL] CAM agent: completed with {len(_cam_result.get('violations', []))} violations")
        except Exception as e:
            print(f"[FULL] CAM agent skipped: {e}")

    print(f"\n{'='*64}")
    print(f"  FULL PIPELINE COMPLETE")
    print(f"{'='*64}")
    print(f"  STEP    : {step or '(none)'}")
    print(f"  STL     : {stl or '(none)'}")
    if session.get("drawing_path"):
        print(f"  Drawing : {session['drawing_path']}")
    print(f"  CAM     : outputs/cam/{_Path(step).stem if step else '?'}/")
    print(f"  Preview : outputs/screenshots/{_Path(stl).stem if stl else '?'}.png")
    print(f"{'='*64}\n")


def _get_cli_arg(flag: str, default: str = None) -> str | None:
    """Safely get the value following a CLI flag. Returns default if flag absent."""
    if flag not in sys.argv:
        return default
    idx = sys.argv.index(flag)
    if idx + 1 >= len(sys.argv):
        print(f"Error: {flag} requires a value")
        sys.exit(1)
    value = sys.argv[idx + 1]
    if value.startswith("--"):
        print(f"Error: {flag} requires a value, got another flag '{value}'")
        sys.exit(1)
    return value


def _run_check() -> None:
    """Validate the environment before running jobs. Exits 0 if all OK, 1 if not."""
    import shutil
    ok = True

    def _ok(msg: str) -> None:
        print(f"  OK  {msg}")

    def _fail(msg: str, hint: str = "") -> None:
        nonlocal ok
        ok = False
        print(f"  FAIL  {msg}")
        if hint:
            print(f"        -> {hint}")

    def _warn(msg: str) -> None:
        print(f"  WARN  {msg}")

    print("\nARIA-OS environment check\n" + "-" * 40)

    # CadQuery
    try:
        import cadquery  # noqa: F401
        _ok("cadquery importable")
    except ImportError:
        _fail("cadquery not installed",
              "conda install -c conda-forge cadquery   OR   pip install cadquery")

    # trimesh
    try:
        import trimesh  # noqa: F401
        _ok("trimesh importable")
    except ImportError:
        _fail("trimesh not installed", "pip install trimesh")

    # ezdxf
    try:
        import ezdxf  # noqa: F401
        _ok("ezdxf importable")
    except ImportError:
        _fail("ezdxf not installed", "pip install ezdxf")

    # Ollama — skipped entirely in cloud-only mode (Railway etc.)
    cloud_only = os.environ.get("ARIA_CLOUD_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
    if cloud_only:
        _ok("Ollama probe skipped (ARIA_CLOUD_ONLY=1)")
    else:
        import urllib.request
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=3) as r:
                if r.status == 200:
                    _ok("Ollama running (localhost:11434)")
                else:
                    _warn("Ollama returned non-200 (install: ollama serve)")
        except Exception:
            _warn("Ollama not reachable — set ARIA_CLOUD_ONLY=1 if intentional, else 'ollama serve'")

    # API keys — check env and .env
    from aria_os.llm_client import get_anthropic_key, get_google_key, get_groq_key
    ant = get_anthropic_key(ROOT)
    goo = get_google_key(ROOT)
    grq = get_groq_key(ROOT)
    if ant:
        _ok(f"ANTHROPIC_API_KEY set ({ant[:12]}...)")
    else:
        _warn("ANTHROPIC_API_KEY not set (code generation will use Gemini/Ollama fallback)")
    if goo:
        _ok(f"GOOGLE_API_KEY set ({goo[:12]}...)")
    else:
        _warn("GOOGLE_API_KEY not set (visual verification will use Ollama only — lower quality)")
    if grq:
        _ok(f"GROQ_API_KEY set ({grq[:12]}...)")
    else:
        _warn("GROQ_API_KEY not set (optional free-tier fallback vision)")

    if not ant and not goo:
        _fail("No cloud LLM keys set — template parts will work but novel parts will fail",
              "Add GOOGLE_API_KEY or ANTHROPIC_API_KEY to .env")

    # Output dirs
    for d in ["outputs/cad/step", "outputs/cad/stl", "outputs/screenshots"]:
        p = ROOT / d
        try:
            p.mkdir(parents=True, exist_ok=True)
            _ok(f"output dir writable: {d}")
        except Exception as e:
            _fail(f"Cannot create output dir {d}: {e}")

    print("-" * 40)
    if ok:
        print("All checks passed. Ready to run.\n")
    else:
        print("Some checks FAILED. Fix the issues above before running jobs.\n")
        sys.exit(1)


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--check":
        _run_check()
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--full":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --full \"part description\"")
            sys.exit(1)
        _full_goal = " ".join(sys.argv[2:])
        _run_full(_full_goal)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        list_parts()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--validate":
        validate_all()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--verify":
        # Visual + geometric verification of all outputs
        from aria_os.output_verifier import verify_all, verify_dxf, verify_ecad, verify_stl, _print_result
        if len(sys.argv) >= 3:
            target = sys.argv[2]
            goal = sys.argv[3] if len(sys.argv) > 3 else ""
            p = Path(target)
            if p.suffix.lower() == ".dxf":
                r = verify_dxf(p); _print_result(f"DXF: {p.name}", r)
            elif p.suffix.lower() == ".stl":
                r = verify_stl(p, goal); _print_result(f"STL: {p.name}", r)
            elif p.suffix.lower() == ".json":
                r = verify_ecad(p); _print_result(f"ECAD: {p.name}", r)
            else:
                print(f"Unknown file type: {p.suffix}. Use .dxf, .stl, or _bom.json")
        else:
            verify_all()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--modify":
        if len(sys.argv) < 4:
            print("Usage: python run_aria_os.py --modify <path_to_.py> \"modification description\"")
            sys.exit(1)
        base_part_path = sys.argv[2]
        modification = " ".join(sys.argv[3:])
        run_modify(base_part_path, modification)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--refine":
        # --refine "changes"                     -- auto-finds most recent script
        # --refine <stub_or_path> "changes"      -- explicit target
        from aria_os.modifier import PartModifier
        mod = PartModifier(repo_root=ROOT)
        args = sys.argv[2:]
        if len(args) == 0:
            print("Usage: python run_aria_os.py --refine \"changes\"")
            print("       python run_aria_os.py --refine <script_or_keyword> \"changes\"")
            sys.exit(1)
        elif len(args) == 1:
            modification = args[0]
            script = mod.find_latest_script()
            if script is None:
                print("No generated scripts found in outputs/cad/generated_code/")
                sys.exit(1)
        else:
            # Could be a path/stub or a keyword — if last arg looks like a sentence, treat it as modification
            modification = args[-1]
            stub = " ".join(args[:-1])
            # Try exact path first
            candidate = Path(stub)
            if not candidate.is_absolute():
                candidate = (ROOT / stub).resolve()
            if candidate.exists():
                script = candidate
            else:
                script = mod.find_latest_script(keyword=stub)
                if script is None:
                    print(f"No script found matching: {stub!r}")
                    sys.exit(1)
        original_goal = mod.extract_goal(script)
        print(f"Refining: {script.name}")
        if original_goal:
            print(f"Original: {original_goal[:120]}")
        print(f"Change:   {modification}")
        print()
        run_modify(str(script), modification)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--cam":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --cam <step_file> [--material aluminium_6061] [--machine generic_vmc]")
            sys.exit(1)
        step_arg = sys.argv[2]
        mat = "aluminium_6061"
        if "--material" in sys.argv:
            mat = sys.argv[sys.argv.index("--material") + 1]
        machine = "generic_vmc"
        if "--machine" in sys.argv:
            machine = sys.argv[sys.argv.index("--machine") + 1]
        from aria_os.agents.cam_agent import run_cam_agent
        run_cam_agent(step_arg, material=mat, machine=machine, repo_root=ROOT)
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--setup":
        # Usage: python run_aria_os.py --setup <step_file> <cam_script> [--material aluminium_6061]
        if len(sys.argv) < 4:
            print("Usage: python run_aria_os.py --setup <step_file> <cam_script> [--material aluminium_6061]")
            sys.exit(1)
        from aria_os.cam_setup import write_setup_sheet
        _setup_step = sys.argv[2]
        _setup_cam  = sys.argv[3]
        _setup_mat  = "aluminium_6061"
        if "--material" in sys.argv:
            _setup_mat = sys.argv[sys.argv.index("--material") + 1]
        _setup_part = Path(_setup_step).stem
        _setup_out  = ROOT / "outputs" / "cam" / _setup_part
        _setup_out.mkdir(parents=True, exist_ok=True)
        write_setup_sheet(
            step_path=_setup_step,
            cam_script_path=_setup_cam,
            material=_setup_mat,
            out_dir=_setup_out,
            part_id=_setup_part,
        )
        print(f"[setup] setup_sheet.md  : {_setup_out / 'setup_sheet.md'}")
        print(f"[setup] setup_sheet.json: {_setup_out / 'setup_sheet.json'}")
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--cam-validate":
        # Usage: python run_aria_os.py --cam-validate <step_file> [--retries 2]
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --cam-validate <step_file> [--retries 2]")
            sys.exit(1)
        # from aria_os.cam_validator import check_machinability
        _cv_step = sys.argv[2]
        _cv_retries = 2
        if "--retries" in sys.argv:
            _cv_retries = int(sys.argv[sys.argv.index("--retries") + 1])
        _cv_result = None
        for _cv_attempt in range(1, _cv_retries + 2):  # attempts = retries + 1
            print(f"\n[CAM-VALIDATE] Attempt {_cv_attempt}...")
            _cv_result = check_machinability(_cv_step)
            if not _cv_result.get("failures"):
                print(f"[CAM-VALIDATE] PASS — machinable with {_cv_result.get('machinable_with', '?')}")
                sys.exit(0)
            print(f"[CAM-VALIDATE] {len(_cv_result['failures'])} failure(s):")
            for _f in _cv_result["failures"]:
                print(f"  - {_f}")
            if _cv_attempt <= _cv_retries:
                print(f"[CAM-VALIDATE] Retrying ({_cv_attempt}/{_cv_retries})...")
        print(f"\n[CAM-VALIDATE] FAIL after {_cv_retries + 1} attempt(s).")
        sys.exit(1)

    if len(sys.argv) >= 2 and sys.argv[1] == "--quote":
        # Usage: python run_aria_os.py --quote <step_file> [--material aluminium_6061] [--process cnc] [--qty 10]
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --quote <step_file> [--material aluminium_6061] [--process cnc] [--qty 10]")
            sys.exit(1)
        from aria_os.agents.quote_agent import run_quote_cli
        _q_step = sys.argv[2]
        if not Path(_q_step).is_absolute():
            _q_step = str(ROOT / _q_step)
        _q_mat = "aluminium_6061"
        _q_proc = "cnc"
        _q_qty = 1
        if "--material" in sys.argv:
            _q_mat = sys.argv[sys.argv.index("--material") + 1]
        if "--process" in sys.argv:
            _q_proc = sys.argv[sys.argv.index("--process") + 1]
        if "--qty" in sys.argv:
            _q_qty = int(sys.argv[sys.argv.index("--qty") + 1])
        run_quote_cli(_q_step, material=_q_mat, process=_q_proc, quantity=_q_qty)
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--draw":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --draw <step_file>")
            sys.exit(1)
        _draw_step = Path(sys.argv[2])
        if not _draw_step.is_absolute():
            _draw_step = ROOT / _draw_step
        if not _draw_step.exists():
            print(f"STEP file not found: {_draw_step}")
            sys.exit(1)
        _part_stub = _draw_step.stem
        _meta_path = ROOT / "outputs" / "cad" / "meta" / f"{_part_stub}.json"
        _params: dict = {}
        if _meta_path.exists():
            _params = json.loads(_meta_path.read_text(encoding="utf-8"))
        from aria_os.drawing_generator import generate_gdnt_drawing
        _svg = generate_gdnt_drawing(_draw_step, _part_stub, _params, repo_root=ROOT)
        print(f"[DRAW] SVG: {_svg}")
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--ecad":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --ecad \"board description\" [--out outputs/ecad/]")
            sys.exit(1)
        _ecad_args = sys.argv[2:]
        _ecad_out = None
        if "--out" in _ecad_args:
            _ecad_out = _ecad_args[_ecad_args.index("--out") + 1]
            _ecad_args = [a for i, a in enumerate(_ecad_args) if a != "--out" and (i == 0 or _ecad_args[i-1] != "--out")]
        _ecad_desc = " ".join(a for a in _ecad_args if not a.startswith("--"))
        from aria_os.ecad_generator import generate_ecad
        generate_ecad(_ecad_desc, out_dir=Path(_ecad_out) if _ecad_out else ROOT / "outputs" / "ecad")
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--autocad":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --autocad \"drainage plan\" [--state TX] [--discipline drainage] [--out outputs/cad/dxf/] [--view]")
            sys.exit(1)
        _autocad_args = sys.argv[2:]
        _autocad_state = "national"
        _autocad_discipline = None
        _autocad_out = None
        _autocad_view = "--view" in _autocad_args
        _autocad_desc_parts = []
        i = 0
        while i < len(_autocad_args):
            if _autocad_args[i] == "--state" and i + 1 < len(_autocad_args):
                _autocad_state = _autocad_args[i + 1]
                i += 2
            elif _autocad_args[i] == "--discipline" and i + 1 < len(_autocad_args):
                _autocad_discipline = _autocad_args[i + 1]
                i += 2
            elif _autocad_args[i] == "--out" and i + 1 < len(_autocad_args):
                _autocad_out = _autocad_args[i + 1]
                i += 2
            elif _autocad_args[i] == "--view":
                i += 1
            else:
                _autocad_desc_parts.append(_autocad_args[i])
                i += 1
        _autocad_desc = " ".join(_autocad_desc_parts)
        from aria_os.autocad import generate_civil_dxf
        from pathlib import Path as _Path
        _autocad_path = generate_civil_dxf(
            description=_autocad_desc,
            state=_autocad_state,
            discipline=_autocad_discipline,
            output_path=_Path(_autocad_out) if _autocad_out else None,
            view_after=_autocad_view,
        )
        print(f"[autocad] DXF  : {_autocad_path}")
        print(f"[autocad] JSON : {_autocad_path.with_suffix('.json')}")
        # ── DXF post-generation validation ────────────────────────────────
        try:
            import ezdxf as _ezdxf_check
            _dxf_readback = _ezdxf_check.readfile(str(_autocad_path))
            _dxf_n = len(list(_dxf_readback.modelspace()))
            _dxf_kb = _autocad_path.stat().st_size // 1024
            print(f"[autocad] DXF validated: {_dxf_kb} KB, {_dxf_n} entities, parseable OK")
        except ImportError:
            print("[autocad] DXF validation skipped (ezdxf not available)")
        except Exception as _dxf_val_exc:
            print(f"[autocad] DXF validation FAILED: {_dxf_val_exc}")
        print(f"[autocad] To view: python run_aria_os.py --review-view {_autocad_path}")
        if _autocad_view:
            print("[autocad] Viewer open — press Ctrl+C to exit.")
            try:
                import time
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--review-view":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --review-view <file.dxf>")
            sys.exit(1)
        from aria_os.preview_ui import show_dxf_preview
        _rv_dxf = Path(sys.argv[2])
        show_dxf_preview(_rv_dxf, title=_rv_dxf.stem)
        print("[DXF PREVIEW] Server running — press Ctrl+C to stop.")
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--review":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --review <file> [--hint \"add pipe labels\"] [--state TX] [--yes]")
            print("  Supported: .dxf (civil review), .step (CAD redesign), .py (ECAD/KiCad review)")
            sys.exit(1)
        _rv_args = sys.argv[2:]
        _rv_hint = ""
        _rv_state = "national"
        _rv_yes = "--yes" in _rv_args
        _rv_file = None
        _rv_i = 0
        while _rv_i < len(_rv_args):
            if _rv_args[_rv_i] == "--hint" and _rv_i + 1 < len(_rv_args):
                _rv_hint = _rv_args[_rv_i + 1]
                _rv_i += 2
            elif _rv_args[_rv_i] == "--state" and _rv_i + 1 < len(_rv_args):
                _rv_state = _rv_args[_rv_i + 1]
                _rv_i += 2
            elif _rv_args[_rv_i] == "--yes":
                _rv_i += 1
            elif _rv_file is None:
                _rv_file = _rv_args[_rv_i]
                _rv_i += 1
            else:
                _rv_i += 1
        if _rv_file is None:
            print("[review] Error: no file specified.")
            sys.exit(1)
        from aria_os.reviewer import review_file
        _rv_out = review_file(
            _rv_file,
            hint=_rv_hint,
            state=_rv_state,
            interactive=not _rv_yes,
            repo_root=ROOT,
        )
        print(f"[review] Revised file: {_rv_out}")
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--ecad-to-enclosure":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --ecad-to-enclosure <path/to/board.kicad_pcb>")
            sys.exit(1)
        _enc_pcb_path = sys.argv[2]
        _enc_out_dir = None
        if "--out" in sys.argv:
            _oi = sys.argv.index("--out")
            if _oi + 1 < len(sys.argv):
                _enc_out_dir = sys.argv[_oi + 1]
        from aria_os.ecad.ecad_to_enclosure import generate_enclosure_from_pcb
        _enc_result = generate_enclosure_from_pcb(_enc_pcb_path, output_dir=_enc_out_dir)
        if _enc_result.error:
            print(f"[ENCLOSURE] Error: {_enc_result.error}")
            sys.exit(1)
        pcb = _enc_result.pcb_geometry
        print(f"[ENCLOSURE] Board: {pcb.board_width_mm:.1f} x {pcb.board_height_mm:.1f} mm")
        print(f"[ENCLOSURE] Mounting holes: {len(pcb.mounting_holes)}")
        print(f"[ENCLOSURE] Connectors: {len(pcb.connectors)}")
        if _enc_result.stl_paths:
            for name, path in _enc_result.stl_paths.items():
                print(f"[ENCLOSURE] STL ({name}): {path}")
        if _enc_result.step_paths:
            for name, path in _enc_result.step_paths.items():
                print(f"[ENCLOSURE] STEP ({name}): {path}")
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--ecad-variants":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --ecad-variants \"base description\" [--variants path/to/variants.json]")
            sys.exit(1)
        # Collect args after the flag; strip known option flags
        _ev_args = sys.argv[2:]
        _ev_variants_path = None
        if "--variants" in _ev_args:
            _vi = _ev_args.index("--variants")
            if _vi + 1 < len(_ev_args):
                _ev_variants_path = _ev_args[_vi + 1]
            _ev_args = [a for j, a in enumerate(_ev_args) if a != "--variants" and (j == 0 or _ev_args[j - 1] != "--variants")]
        _ev_base_desc = " ".join(a for a in _ev_args if not a.startswith("--"))
        # Resolve variants JSON path
        if _ev_variants_path is None:
            _ev_variants_path = str(ROOT / "variants" / "aria_board_variants.json")
        _ev_variants_file = Path(_ev_variants_path)
        if not _ev_variants_file.is_absolute():
            _ev_variants_file = ROOT / _ev_variants_file
        if not _ev_variants_file.exists():
            print(f"[VARIANT] Variants file not found: {_ev_variants_file}")
            print("[VARIANT] Create a JSON file with a list of variant dicts, each with a 'name' key.")
            sys.exit(1)
        _ev_variants = json.loads(_ev_variants_file.read_text(encoding="utf-8"))
        if not isinstance(_ev_variants, list):
            print("[VARIANT] Variants JSON must be a list of dicts.")
            sys.exit(1)
        from aria_os.ecad_variant_runner import run_variant_study, print_variant_table, save_variant_study, _slug
        _ev_results = run_variant_study(_ev_base_desc, _ev_variants, ROOT)
        print_variant_table(_ev_results)
        _ev_slug = _slug(_ev_base_desc)
        _ev_out = save_variant_study(_ev_results, _ev_slug, ROOT)
        print(f"[VARIANT] Saved: {_ev_out}")
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "--constrain":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --constrain assembly_configs/foo.json [--proximity 50]")
            sys.exit(1)
        import importlib.util as _ilu
        _ac_path = ROOT / "assemble_constrain.py"
        _spec = _ilu.spec_from_file_location("assemble_constrain", _ac_path)
        _ac = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_ac)
        _prox = 50.0
        if "--proximity" in sys.argv:
            _prox = float(sys.argv[sys.argv.index("--proximity") + 1])
        _cfg_path = Path(sys.argv[2])
        if not _cfg_path.is_absolute():
            _cfg_path = ROOT / _cfg_path
        _ac.generate_constrained_script(_cfg_path, proximity_mm=_prox)
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--assemble":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --assemble assembly_configs/aria_clutch_assembly.json")
            sys.exit(1)
        run_assemble(sys.argv[2])
        return

    # --- --assembly: describe a multi-part assembly and generate all parts ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--assembly":
        if len(sys.argv) < 3:
            print('Usage: python run_aria_os.py --assembly "motor mount assembly: baseplate with holes and vertical bracket"')
            sys.exit(1)
        _assembly_goal = " ".join(sys.argv[2:])
        from aria_os.agents.assembly_agent import run_assembly_agent_sync
        _result = run_assembly_agent_sync(_assembly_goal, repo_root=ROOT)
        if _result.get("config_path"):
            print(f"\nAssembly config: {_result['config_path']}")
        if _result.get("assembly_step"):
            print(f"Combined STEP:   {_result['assembly_step']}")
        return

    if len(sys.argv) >= 2 and sys.argv[1] == "--print-scale":
        if len(sys.argv) < 4:
            print("Usage: python run_aria_os.py --print-scale <part_stub> --scale <factor>")
            sys.exit(1)
        run_print_scale(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--optimize":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --optimize <code_path> --goal <goal> [--constraint RULE ...]")
            sys.exit(1)
        run_optimize(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--optimize-and-regenerate":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --optimize-and-regenerate <code_path_or_stub> --goal <goal> [--constraint RULE ...] [--material MATERIAL_ID]")
            sys.exit(1)
        run_optimize_and_regenerate(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--cem-full":
        run_cem_full()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--cem-advise":
        from aria_os.cem_advisor import run_cem_advisor
        run_cem_advisor(Path(__file__).resolve().parent)
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--material-study":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --material-study <part_name_or_stub>")
            sys.exit(1)
        run_material_study_cli(sys.argv[2])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--material-study-all":
        run_material_study_all_cli()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--lattice-test":
        run_lattice_test()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--lattice":
        if len(sys.argv) < 3:
            print(
                "Usage: python run_aria_os.py --lattice "
                "--pattern [arc_weave|honeycomb|octet_truss] "
                "--form [volumetric|conformal|skin_core] ..."
            )
            sys.exit(1)
        run_lattice(sys.argv[2:])
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "--generate-and-assemble":
        # Parse: --generate-and-assemble <desc...> --into PATH --as LABEL --at "x,y,z" [--rot "rx,ry,rz"]
        if len(sys.argv) < 4:
            print("Usage: python run_aria_os.py --generate-and-assemble \"part description\" --into assembly_configs/foo.json --as label --at \"x,y,z\" [--rot \"rx,ry,rz\"]")
            sys.exit(1)
        argv = sys.argv[2:]
        # Find --into as delimiter for description
        try:
            into_idx = argv.index("--into")
        except ValueError:
            print("Missing --into for --generate-and-assemble")
            sys.exit(1)
        description = " ".join(argv[:into_idx])
        into_path = None
        part_label = None
        at_vec = None
        rot_vec = None
        i = into_idx
        while i < len(argv):
            tok = argv[i]
            if tok == "--into" and i + 1 < len(argv):
                into_path = argv[i + 1]
                i += 2
            elif tok == "--as" and i + 1 < len(argv):
                part_label = argv[i + 1]
                i += 2
            elif tok == "--at" and i + 1 < len(argv):
                at_vec = argv[i + 1]
                i += 2
            elif tok == "--rot" and i + 1 < len(argv):
                rot_vec = argv[i + 1]
                i += 2
            else:
                i += 1
        if not (into_path and part_label and at_vec):
            print("Missing required flags for --generate-and-assemble (need --into, --as, --at).")
            sys.exit(1)
        run_generate_and_assemble(description, into_path, part_label, at_vec, rot_vec)
        return
    # --- --scenario: interpret a real-world scenario and generate all parts ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--scenario":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --scenario \"description of situation\" [--auto-confirm]")
            sys.exit(1)
        _argv_rest = sys.argv[2:]
        _auto_confirm = "--auto-confirm" in _argv_rest
        _scenario_text = " ".join(a for a in _argv_rest if a != "--auto-confirm")
        from aria_os.scenario_interpreter import interpret_and_generate
        interpret_and_generate(_scenario_text, repo_root=ROOT, auto_confirm=_auto_confirm)
        return

    # --- --scenario-dry-run: show parts list without generating ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--scenario-dry-run":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --scenario-dry-run \"description of situation\"")
            sys.exit(1)
        _scenario_text = " ".join(sys.argv[2:])
        from aria_os.scenario_interpreter import interpret_scenario, _print_plan
        _goals = interpret_scenario(_scenario_text, repo_root=ROOT)
        _print_plan(_goals, _scenario_text)
        print(f"\nDry run complete. {len(_goals)} part(s) identified. Use --scenario to generate.")
        return

    # --- --system: two-pass whole-machine decomposition + generation ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--system":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --system \"describe the full machine/device\" [--auto-confirm]")
            sys.exit(1)
        _argv_rest   = sys.argv[2:]
        _auto_confirm = "--auto-confirm" in _argv_rest
        _system_text  = " ".join(a for a in _argv_rest if a != "--auto-confirm")
        from aria_os.scenario_interpreter import interpret_system_and_generate
        interpret_system_and_generate(_system_text, repo_root=ROOT, auto_confirm=_auto_confirm)
        return

    # --- --system-dry-run: show subsystem+parts plan without generating ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--system-dry-run":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --system-dry-run \"describe the full machine/device\"")
            sys.exit(1)
        _system_text = " ".join(sys.argv[2:])
        from aria_os.scenario_interpreter import interpret_system, _print_system_plan
        _result = interpret_system(_system_text, repo_root=ROOT)
        _print_system_plan(_result["subsystems"], _result["parts"], _system_text)
        print(f"\nDry run complete. {len(_result['subsystems'])} subsystem(s), "
              f"{len(_result['parts'])} part(s). Use --system to generate.")
        if _result.get("assembly_path"):
            print(f"Assembly config: {_result['assembly_path']}")
        return

    # --- --analyze-part: run FEA/CFD on an existing STEP file ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--analyze-part":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --analyze-part <path_to.step> [--fea|--cfd|--auto]")
            sys.exit(1)
        _step_arg = sys.argv[2]
        _atype_arg = "auto"
        for _a in sys.argv[3:]:
            if _a in ("--fea", "--cfd", "--auto"):
                _atype_arg = _a.lstrip("-")
        _step_path = Path(_step_arg) if Path(_step_arg).is_absolute() else ROOT / _step_arg
        if not _step_path.exists():
            print(f"STEP file not found: {_step_path}")
            sys.exit(1)
        # Try to load params from meta JSON
        _part_stub = _step_path.stem
        _meta_path = ROOT / "outputs" / "cad" / "meta" / f"{_part_stub}.json"
        _params: dict = {}
        if _meta_path.exists():
            _params = json.loads(_meta_path.read_text(encoding="utf-8"))
            print(f"[ANALYZE] Loaded params from {_meta_path}")
        else:
            print(f"[ANALYZE] No meta JSON found at {_meta_path} — running with empty params")
        from aria_os.physics_analyzer import analyze as _phys_analyze
        _result = _phys_analyze(
            part_id=_part_stub,
            analysis_type=_atype_arg,
            params=_params,
            goal=_part_stub.replace("_", " "),
            repo_root=ROOT,
        )
        print()
        print(_result["report"])
        if _result.get("failures"):
            for _f in _result["failures"]:
                print(f"  [FAIL] {_f}")
        if _result.get("warnings"):
            for _w in _result["warnings"]:
                print(f"  [WARN] {_w}")
        _sf_val = _result.get("safety_factor")
        if _sf_val is not None:
            print(f"\n[ANALYZE] {_result['analysis_type']}  SF={_sf_val:.2f}  {'PASS' if _result['passed'] else 'FAIL'}")
        else:
            print(f"\n[ANALYZE] {_result['analysis_type']}  {'PASS' if _result['passed'] else 'FAIL'}")
        return

    # --- --view / --preview: open ANY artifact (STL/STEP/SVG/DXF/PNG/JPG) ---
    # Routes by file extension. STL/STEP open the Three.js browser viewer,
    # DXF opens the dxf preview UI, SVG/PNG/JPG open in default browser.
    if len(sys.argv) >= 2 and sys.argv[1] in ("--view", "--preview"):
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --view <file>")
            print("  Supported: .stl, .step, .stp, .dxf, .svg, .png, .jpg, .jpeg")
            sys.exit(1)
        _view_path = Path(sys.argv[2])
        if not _view_path.exists():
            _view_path = ROOT / sys.argv[2]
        if not _view_path.exists():
            print(f"[VIEW] File not found: {sys.argv[2]}")
            sys.exit(1)
        _suffix = _view_path.suffix.lower()

        if _suffix in (".stl", ".step", ".stp"):
            # 3D model — Three.js viewer (existing path)
            _stl_for_view = _view_path
            if _suffix in (".step", ".stp"):
                _stl_for_view = _view_path.with_suffix(".stl")
                if not _stl_for_view.exists():
                    print(f"[VIEW] Converting STEP -> STL for viewer...")
                    try:
                        import cadquery as cq
                        _shape = cq.importers.importStep(str(_view_path))
                        cq.exporters.export(_shape, str(_stl_for_view))
                    except Exception as _e:
                        print(f"[VIEW] STEP->STL conversion failed: {_e}")
                        sys.exit(1)
            from aria_os.preview_ui import show_preview
            show_preview(str(_stl_for_view), _view_path.stem)
            return

        if _suffix == ".dxf":
            # DXF drawing — dedicated DXF preview UI
            from aria_os.preview_ui import show_dxf_preview
            show_dxf_preview(str(_view_path), title=_view_path.stem)
            return

        if _suffix in (".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp"):
            # 2D drawing/render — open in default browser using a file:// URL
            import webbrowser
            _abs = _view_path.resolve()
            _url = _abs.as_uri()
            print(f"[VIEW] Opening {_view_path.name} in default browser")
            print(f"[VIEW] URL: {_url}")
            try:
                webbrowser.open(_url)
            except Exception as _e:
                print(f"[VIEW] Could not auto-open: {_e}")
                print(f"[VIEW] Open manually: {_abs}")
            return

        if _suffix == ".json":
            # Detect ECAD BOM JSONs and render the PCB layout. Otherwise pretty-print.
            import json as _json
            try:
                _data = _json.loads(_view_path.read_text(encoding="utf-8"))
            except Exception as _e:
                print(f"[VIEW] JSON parse failed: {_e}")
                return
            _is_ecad_bom = (
                isinstance(_data, dict)
                and isinstance(_data.get("components"), list)
                and any(isinstance(c, dict) and "x_mm" in c
                        for c in _data["components"][:5])
            )
            if _is_ecad_bom:
                print(f"[VIEW] Detected ECAD BOM — rendering PCB layout preview")
                from aria_os.ecad.ecad_preview import render_pcb_preview
                _png = render_pcb_preview(_view_path)
                print(f"[VIEW] Preview: {_png}")
                # Auto-open the PNG in default browser
                import webbrowser
                webbrowser.open(_png.resolve().as_uri())
                return
            # Default: pretty-print JSON
            _txt = _json.dumps(_data, indent=2)
            print(_txt[:8000])
            if len(_txt) > 8000:
                print(f"... (truncated; full file at {_view_path})")
            return

        print(f"[VIEW] Unsupported file type: {_suffix}")
        print(f"  Supported: .stl, .step, .stp, .dxf, .svg, .png, .jpg, .json")
        sys.exit(1)

    # --- --image: analyse a photo and derive a goal, then run pipeline ---
    # Uses the fast image-to-CAD path that skips research, DFM, quote, CAM,
    # drawing, and CEM to keep runtime under ~60s.
    # Pass --image-full to force the complete pipeline instead.
    if len(sys.argv) >= 2 and sys.argv[1] in ("--image", "--image-full"):
        _image_full = sys.argv[1] == "--image-full"
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --image <photo.jpg> [\"optional hint\"] [--preview]")
            print("       python run_aria_os.py --image-full <photo.jpg> [\"hint\"] [--preview]  # full pipeline")
            sys.exit(1)
        _argv_rest = sys.argv[2:]
        _preview = "--preview" in _argv_rest
        _argv_clean = [a for a in _argv_rest if a != "--preview"]
        _image_path = _argv_clean[0]
        _hint = " ".join(_argv_clean[1:])
        from aria_os.llm_client import analyze_image_for_cad
        print(f"[IMAGE] Analysing {_image_path}...")
        _goal = analyze_image_for_cad(_image_path, hint=_hint, repo_root=ROOT)
        if not _goal:
            print("[IMAGE] Could not extract a goal from the image. Provide a hint with a description.")
            sys.exit(1)
        print(f"[IMAGE] Goal: {_goal}")
        if _image_full:
            from aria_os import run
            run(_goal, repo_root=ROOT, preview=_preview)
        else:
            from aria_os import run_image_fast
            run_image_fast(_goal, repo_root=ROOT, preview=_preview)
        print("Done.")
        return

    # --- --scan: reverse pipeline (3D scan → features → catalog) ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--scan":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --scan <file.stl|.obj|.ply> [--material X] [--tags tag1,tag2]")
            sys.exit(1)
        _scan_file = sys.argv[2]
        _scan_mat = _get_cli_arg("--material", "unknown")
        _raw_tags = _get_cli_arg("--tags")
        _scan_tags = _raw_tags.split(",") if _raw_tags else []
        from aria_os.scan_pipeline import run_scan_pipeline
        run_scan_pipeline(_scan_file, material=_scan_mat, tags=_scan_tags)
        print("Done.")
        return

    # --- --catalog: list/search scanned parts catalog ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--catalog":
        _cat_topo = _get_cli_arg("--topology")
        _raw_tags = _get_cli_arg("--tags")
        _cat_tags = _raw_tags.split(",") if _raw_tags else None
        _cat_dims = _get_cli_arg("--search")
        from aria_os.scan_pipeline import list_catalog
        list_catalog(topology=_cat_topo, tags=_cat_tags, search_dims=_cat_dims)
        return

    # --- --reconstruct: regenerate CAD from scanned part features ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--reconstruct":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --reconstruct <catalog_part_id>")
            sys.exit(1)
        _recon_id = sys.argv[2]
        from aria_os.scan_pipeline import reconstruct_from_catalog
        reconstruct_from_catalog(_recon_id)
        print("Done.")
        return

    # --- --scan-dir: batch scan a directory of mesh files ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--scan-dir":
        if len(sys.argv) < 3:
            print("Usage: python run_aria_os.py --scan-dir <directory> [--material X] [--tags tag1,tag2]")
            sys.exit(1)
        _sdir = sys.argv[2]
        _sdir_mat = _get_cli_arg("--material", "unknown")
        _raw_tags = _get_cli_arg("--tags")
        _sdir_tags = _raw_tags.split(",") if _raw_tags else []
        from aria_os.scan_pipeline import scan_directory
        scan_directory(_sdir, material=_sdir_mat, tags=_sdir_tags or None)
        print("Done.")
        return

    # --- --multi-domain: outer MCAD + ECAD + inner enclosure + drawings + BOMs ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--multi-domain":
        from aria_os.multi_domain import run_multi_domain, DEFAULT_DEMO_SPEC
        _md_args = sys.argv[2:]
        # Two-spec form: --multi-domain "OUTER" --ecad "ECAD"
        # Or: --multi-domain  (use the demo spec)
        _outer_spec = None
        _ecad_spec = None
        if "--ecad" in _md_args:
            _ei = _md_args.index("--ecad")
            _outer_spec = " ".join(_md_args[:_ei]) or None
            _ecad_spec = " ".join(_md_args[_ei + 1:]) or None
        elif _md_args:
            _outer_spec = " ".join(_md_args)
        print()
        print("=" * 64)
        print("  MULTI-DOMAIN DESIGN — outer MCAD + ECAD + enclosure + drawings + BOMs")
        if not _outer_spec:
            print(f"  Using demo spec: {DEFAULT_DEMO_SPEC['name']}")
        print("=" * 64)
        _md_result = run_multi_domain(
            outer_spec=_outer_spec, ecad_spec=_ecad_spec, repo_root=ROOT,
        )
        print()
        for _stg in _md_result.stages:
            _icon = "[OK]  " if _stg.success else "[FAIL]"
            print(f"  {_icon} {_stg.name:<20s} ({_stg.elapsed_s:.1f}s)")
            if _stg.error:
                print(f"           -> {_stg.error[:140]}")
        print("-" * 64)
        _verdict = ("PASS" if _md_result.success
                    else ("PARTIAL" if _md_result.partial_success else "FAIL"))
        print(f"  OVERALL: {_verdict}  ({_md_result.elapsed_s:.1f}s)")
        if _md_result.html_index:
            print(f"  Preview: {_md_result.html_index}")
        if _md_result.bom_mcad and _md_result.bom_mcad.get("summary"):
            _msum = _md_result.bom_mcad["summary"]
            print(f"  MCAD BOM: {_msum.get('total_parts', 0)} parts, "
                  f"${_msum.get('total_purchased_cost_usd', 0):.2f}")
        if _md_result.bom_ecad:
            print(f"  ECAD BOM: {_md_result.bom_ecad.get('total_components', 0)} components")
        print("=" * 64)
        sys.exit(0 if _md_result.success or _md_result.partial_success else 1)

    # --- --terrain: synthetic terrain DXF + STL from natural language ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--terrain":
        if len(sys.argv) < 3:
            print('Usage: python run_aria_os.py --terrain "mountain terrain 5km x 5km with 200m peak"')
            sys.exit(1)
        _terrain_desc = " ".join(sys.argv[2:])
        from aria_os.generators.terrain_generator import generate_terrain
        _tout = ROOT / "outputs" / "terrain"
        _tres = generate_terrain(_terrain_desc, output_dir=str(_tout))
        print(f"[Terrain] DXF : {_tres.get('dxf_path', 'n/a')}")
        print(f"[Terrain] STL : {_tres.get('stl_path', 'n/a')}")
        print(f"[Terrain] type: {_tres.get('terrain_type', '?')}  "
              f"size: {_tres.get('width_m', '?')}m x {_tres.get('height_m', '?')}m  "
              f"peak: {_tres.get('peak_elevation_m', '?')}m")
        print(f"[Terrain] contours: {_tres.get('n_contours', '?')}  "
              f"resolution: {_tres.get('grid_resolution', '?')}")
        print("Done.")
        return

    # --- --catalog-search: similarity search with natural language ---
    if len(sys.argv) >= 2 and sys.argv[1] == "--catalog-search":
        if len(sys.argv) < 3:
            print('Usage: python run_aria_os.py --catalog-search "75x45x12 bracket with 4 holes"')
            sys.exit(1)
        _search_desc = " ".join(sys.argv[2:])
        from aria_os.scan_pipeline import search_similar
        search_similar(_search_desc)
        return

    # ── --ask: interactive manufacturing Q&A (Jarvis mode) ────────────
    if len(sys.argv) >= 2 and sys.argv[1] == "--ask":
        from aria_os.teaching.engine import TeachingEngine, DifficultyLevel
        _ask_level = "intermediate"
        if "--level" in sys.argv:
            _li = sys.argv.index("--level")
            if _li + 1 < len(sys.argv):
                _ask_level = sys.argv[_li + 1]
        _level_map = {
            "beginner": DifficultyLevel.BEGINNER,
            "intermediate": DifficultyLevel.INTERMEDIATE,
            "expert": DifficultyLevel.EXPERT,
        }
        _engine = TeachingEngine(difficulty=_level_map.get(_ask_level, DifficultyLevel.INTERMEDIATE))

        # If a STEP file or goal is provided, load context from the most recent run
        _ask_args = [a for a in sys.argv[2:] if a not in ("--level", _ask_level)]
        if _ask_args:
            _ask_target = _ask_args[0]
            if Path(_ask_target).exists() and _ask_target.endswith(".step"):
                _engine.update_context("step_file", _ask_target)
                # Try to load metadata
                _meta_p = ROOT / "outputs" / "cad" / "meta" / (Path(_ask_target).stem + ".json")
                if _meta_p.exists():
                    _meta = json.loads(_meta_p.read_text(encoding="utf-8"))
                    _engine.update_context("goal", _meta.get("goal", ""))
                    _engine.update_context("spec", _meta.get("params", {}))
                    _engine.update_context("material", _meta.get("params", {}).get("material", ""))
                    _engine.update_context("cad_tool", _meta.get("cad_tool", ""))
                    _engine.update_context("bbox", _meta.get("bbox_mm", {}))
                    print(f"[ASK] Loaded context from {_meta_p.name}")

        print()
        print("=" * 64)
        print("  ARIA Manufacturing Assistant")
        print(f"  Level: {_ask_level} | Type 'quit' to exit")
        print("=" * 64)
        print()
        print("Ask me anything about manufacturing, materials, design,")
        print("CNC machining, 3D printing, GD&T, DFM, or your current part.")
        print()

        while True:
            try:
                question = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break
            if not question:
                continue
            if question.lower() in ("quit", "exit", "q"):
                print("Goodbye.")
                break
            answer = _engine.ask(question)
            print(f"\nARIA: {answer}\n")
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Usage: python run_aria_os.py \"describe the part you want\"")
        print("       python run_aria_os.py \"part description\" --teach   # with design reasoning narration")
        print("       python run_aria_os.py \"part description\" --teach-interactive   # pause after each phase for Q&A")
        print("       python run_aria_os.py --ask [step_file] [--level beginner|intermediate|expert]")
        print("       python run_aria_os.py \"part description\" --fea   # force FEA after export")
        print("       python run_aria_os.py \"part description\" --cfd   # force CFD after export")
        print("       python run_aria_os.py --analyze-part outputs/cad/step/aria_spool.step")
        print("       python run_aria_os.py --view <file.stl|file.step>  # open existing file in 3D viewer")
        print("       python run_aria_os.py --image <photo.jpg> [\"hint\"] [--preview]        # fast: CAD only")
        print("       python run_aria_os.py --image-full <photo.jpg> [\"hint\"] [--preview]   # full pipeline")
        print("       python run_aria_os.py --scenario \"real-world situation\" [--auto-confirm]")
        print("       python run_aria_os.py --scenario-dry-run \"real-world situation\"")
        print("       python run_aria_os.py --system \"design a desktop CNC router 300x300x100mm\" [--auto-confirm]")
        print("       python run_aria_os.py --system-dry-run \"design a 6-DOF robot arm, 1kg payload\"")
        print("       python run_aria_os.py --list")
        print("       python run_aria_os.py --validate")
        print("       python run_aria_os.py --verify [path]  # visual checks + PNG renders")
        print("       python run_aria_os.py --modify <path_to_.py> \"modification\"")
        print("       python run_aria_os.py --assemble <config.json>")
        print('       python run_aria_os.py --assembly "baseplate assembly with bracket bolted to it"')
        print("       python run_aria_os.py --constrain <config.json> [--proximity 50]")
        print("       python run_aria_os.py --draw <step_file>")
        print("       python run_aria_os.py --autocad \"drainage plan\" [--state TX] [--discipline drainage] [--out path/]")
        print("       python run_aria_os.py --review <file.dxf|.step|.py> [--hint \"add pipe labels\"] [--state TX] [--yes]")
        print("       python run_aria_os.py --ecad \"board description\" [--out outputs/ecad/]")
        print("       python run_aria_os.py --cam <step_file> [--material aluminium_6061]")
        print("       python run_aria_os.py --cam-validate <step_file> [--retries 2]")
        print("       python run_aria_os.py --setup <step_file> <cam_script> [--material aluminium_6061]")
        print("       python run_aria_os.py \"part description\" --render")
        print("       python run_aria_os.py --quote <step_file> [--material aluminium_6061] [--process cnc] [--qty 10]")
        print("       python run_aria_os.py --full \"part description\"  # generate+FEA+draw+render+CAM+setup+quote in one shot")
        print("       python run_aria_os.py --scan <file.stl> [--material X] [--tags tag1,tag2]  # reverse: scan → features → catalog")
        print("       python run_aria_os.py --catalog [--topology prismatic] [--search 50x30x20] [--tags bracket]")
        print("       python run_aria_os.py --reconstruct <catalog_id>  # regenerate parametric CAD from scan features")
        print("       python run_aria_os.py --scan-dir <directory> [--material X] [--tags tag1,tag2]  # batch scan all meshes")
        print('       python run_aria_os.py --catalog-search "75x45x12 bracket with 4 holes"  # similarity search')
        print("Example: python run_aria_os.py \"generate the ARIA housing shell\"")
        sys.exit(1)

    # Strip control flags from args before joining into goal
    _args = sys.argv[1:]
    _preview = "--preview" in _args
    _force_fea = "--fea" in _args
    _force_cfd = "--cfd" in _args
    _render = "--render" in _args
    _no_agent = "--no-agent" in _args
    _agent_mode_flag = "--agent-mode" in _args
    _coordinator_mode = "--coordinator" in _args
    _teach_interactive = "--teach-interactive" in _args
    _teach_mode = "--teach" in _args or _teach_interactive
    _teach_level = "intermediate"
    _max_agent_iter = 3
    for i, a in enumerate(_args):
        if a == "--max-agent-iterations" and i + 1 < len(_args):
            try:
                _max_agent_iter = int(_args[i + 1])
            except ValueError:
                pass
        if a == "--teach-level" and i + 1 < len(_args):
            _teach_level = _args[i + 1]
    _strip_flags = {"--preview", "--fea", "--cfd", "--render",
                    "--no-agent", "--agent-mode", "--coordinator", "--max-agent-iterations",
                    "--teach", "--teach-interactive", "--teach-level"}
    _args_clean = []
    _skip_next = False
    for a in _args:
        if _skip_next:
            _skip_next = False
            continue
        if a in _strip_flags:
            if a in ("--max-agent-iterations", "--teach-level"):
                _skip_next = True
            continue
        _args_clean.append(a)
    goal = " ".join(_args_clean)

    # Determine agent mode: --agent-mode forces on, --no-agent forces off, else auto
    _agent_mode = None  # auto
    if _agent_mode_flag:
        _agent_mode = True
    elif _no_agent:
        _agent_mode = False

    # Coordinator mode: full parallel pipeline with research + CAM + MillForge
    if _coordinator_mode:
        from aria_os.agents.coordinator import run_coordinator_sync
        ctx = run_coordinator_sync(goal, repo_root=ROOT)
        session = {
            "goal": goal,
            "agent_mode": True,
            "coordinator": True,
            "job_id": ctx.job_id,
            "step_path": ctx.geometry_path,
            "stl_path": ctx.stl_path,
            "validation_passed": ctx.validation_passed,
            # cam removed
            "millforge_job": ctx.millforge_job,
            "total_time_s": ctx.total_time_s,
        }
        print("Done.")
        sys.exit(0)

    # Unique run ID — timestamp + 8-char hex. Concurrent runs never collide.
    import uuid as _uuid
    _run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + _uuid.uuid4().hex[:8]

    from aria_os import run
    try:
        session = run(goal, repo_root=ROOT, preview=_preview,
                      agent_mode=_agent_mode, max_agent_iterations=_max_agent_iter,
                      teaching=_teach_mode, teaching_level=_teach_level,
                      teaching_interactive=_teach_interactive)
    except ConnectionRefusedError:
        print("\nERROR: Cannot connect to Ollama.")
        print("  Start it with:  ollama serve")
        print("  Then retry.\n")
        sys.exit(1)
    except ImportError as _ie:
        print(f"\nERROR: Missing dependency — {_ie}")
        print("  Run:  python run_aria_os.py --check  to diagnose.\n")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as _e:
        print(f"\nERROR: Pipeline failed — {_e}")
        print("  Run with --check to validate your environment.")
        print("  For details run:  python run_aria_os.py --check")
        if os.environ.get("ARIA_DEBUG"):
            raise
        sys.exit(1)

    # Write run manifest + copy artifacts to outputs/runs/<run_id>/
    if isinstance(session, dict):
        try:
            from aria_os.run_manifest import create_run as _create_run
            _run_dir = _create_run(
                run_id=_run_id,
                goal=goal,
                session=session,
                spec=session.get("spec"),
                agent_mode=_agent_mode,
                agent_iterations=session.get("agent_iterations"),
                repo_root=ROOT,
            )
            print(f"\n[RUN] {_run_dir.relative_to(ROOT)}")
        except Exception as _me:
            pass  # manifest failure never breaks the main flow

    # --render: save a PNG preview of the generated STL
    if _render and isinstance(session, dict):
        _stl_p = session.get("stl_path") or session.get("stl")
        if _stl_p and Path(_stl_p).exists():
            from batch import _render_stl, OUT_SHOTS
            _slug = Path(_stl_p).stem
            _png = OUT_SHOTS / f"{_slug}.png"
            _err = _render_stl(str(_stl_p), _png)
            if _err:
                print(f"[RENDER] WARN: {_err}")
            else:
                print(f"[RENDER] -> outputs/screenshots/{_slug}.png")

    # --fea / --cfd: run physics analysis immediately after pipeline finishes
    if (_force_fea or _force_cfd) and isinstance(session, dict):
        _forced_type = "fea" if _force_fea else "cfd"
        _plan_params = session.get("params") or {}
        _part_id_f   = goal.split()[0] if goal else "aria_part"
        print(f"\n[PHYSICS] Running forced {_forced_type.upper()} analysis...")
        from aria_os.physics_analyzer import analyze as _phys_analyze
        _result = _phys_analyze(
            part_id=_part_id_f,
            analysis_type=_forced_type,
            params=_plan_params,
            goal=goal,
            repo_root=ROOT,
        )
        print()
        print(_result["report"])
        if _result.get("failures"):
            for _f in _result["failures"]:
                print(f"  [FAIL] {_f}")
        if _result.get("warnings"):
            for _w in _result["warnings"]:
                print(f"  [WARN] {_w}")
        _sf_val = _result.get("safety_factor")
        if _sf_val is not None:
            print(f"[PHYSICS] {_result['analysis_type']}  SF={_sf_val:.2f}  {'PASS' if _result['passed'] else 'FAIL'}")
        else:
            print(f"[PHYSICS] {_result['analysis_type']}  {'PASS' if _result['passed'] else 'FAIL'}")

    # Exit code 1 if no geometry was produced (orchestrator already printed the reason)
    if isinstance(session, dict):
        _exit_step = session.get("step_path", "")
        _exit_stl  = session.get("stl_path", "")
        if not (
            (_exit_step and Path(_exit_step).exists()) or
            (_exit_stl  and Path(_exit_stl).exists())
        ):
            sys.exit(1)


if __name__ == "__main__":
    main()
