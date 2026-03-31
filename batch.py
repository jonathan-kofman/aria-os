"""
batch.py — General batch part generator. Reads a JSON parts list file.

Parts list JSON format:
    [
      {"label": "Part name", "part_id": "template_key", "params": {...}},
      ...
    ]

Usage:
    python batch.py parts/clock_parts.json
    python batch.py parts/clock_parts.json --skip-existing
    python batch.py parts/clock_parts.json --only "escape"    (substring match on label)
    python batch.py parts/clock_parts.json --dry-run
    python batch.py parts/clock_parts.json --workers 4        (parallel, default 1)
    python batch.py parts/clock_parts.json --verify-mesh      (check gear module compatibility)
    python batch.py parts/clock_parts.json --render           (save PNG preview per part to outputs/screenshots/)

- Outputs STEP to outputs/cad/step/{slug}.step, STL to outputs/cad/stl/{slug}.stl
- --skip-existing: skip if STEP already exists
- --workers N: run up to N parts in parallel using separate processes (safe for CadQuery)
- --verify-mesh: after generation, validate gear pair module compatibility from the parts JSON
- Calls gc.collect() between parts (serial mode) to avoid RAM accumulation
- Prints live [OK] / [FAIL] per part with bbox
- Prints summary table at end (N/total passed)
- --render: renders each STL to a PNG using trimesh and saves to outputs/screenshots/
"""
import sys
import gc
import re
import json
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

OUT_STEP  = ROOT / "outputs" / "cad" / "step"
OUT_STL   = ROOT / "outputs" / "cad" / "stl"
OUT_SHOTS = ROOT / "outputs" / "screenshots"


def _render_stl(stl_path: str, out_png: Path) -> str:
    """
    Render an STL to a PNG using trimesh's offscreen renderer.
    Returns "" on success or an error message string.
    Tries trimesh scene.save_image first; falls back to pyrender if available.
    """
    try:
        import trimesh
        import numpy as np

        mesh = trimesh.load(stl_path, force="mesh")
        if mesh is None or (hasattr(mesh, "is_empty") and mesh.is_empty):
            return "empty mesh"

        # Center and normalize for a clean view
        mesh.apply_translation(-mesh.center_mass)

        scene = trimesh.Scene(mesh)

        # Isometric-ish camera: pull back 2.5× the bounding sphere radius
        bounds = mesh.bounds
        diag = float(np.linalg.norm(bounds[1] - bounds[0]))
        dist = diag * 1.8

        # Place camera at a 45°/35° isometric position
        cam_pos = np.array([dist * 0.7, dist * -0.7, dist * 0.5])
        scene.set_camera(angles=(0.6, 0, 0.8), distance=dist, center=mesh.center_mass.tolist())

        png_bytes = scene.save_image(resolution=(800, 600), visible=False)
        if png_bytes:
            out_png.parent.mkdir(parents=True, exist_ok=True)
            out_png.write_bytes(png_bytes)
            return ""
        return "save_image returned empty"
    except Exception as exc:
        return str(exc)


def _fmt_bbox(bbox) -> str:
    if not bbox:
        return "?"
    if isinstance(bbox, dict):
        return f"{bbox.get('x','?')}x{bbox.get('y','?')}x{bbox.get('z','?')}"
    return str(bbox)


def _fmt_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n/1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n/1024:.1f} KB"
    return f"{n} B"


def load_parts(parts_file: Path) -> list[dict]:
    with open(parts_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"Parts file must be a JSON array, got {type(data).__name__}")
    for i, entry in enumerate(data):
        if "label" not in entry:
            raise ValueError(f"Entry {i} missing 'label'")
        if "part_id" not in entry:
            raise ValueError(f"Entry {i} missing 'part_id'")
    return data


def _slug_for(label: str) -> str:
    return re.sub(r"[^\w]+", "_", label.lower()).strip("_")


# ---------------------------------------------------------------------------
# Per-part worker — must be a top-level function so ProcessPoolExecutor can
# pickle it on Windows (spawn start method).
# ---------------------------------------------------------------------------

def _generate_one(job: dict) -> dict:
    """
    Run CAD generation for a single part. Called in a worker process.
    Returns a result dict with keys: label, part_id, bbox, error, ok.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _root = _Path(__file__).resolve().parent
    _sys.path.insert(0, str(_root))

    from aria_os.cadquery_generator import write_cadquery_artifacts

    label     = job["label"]
    part_id   = job["part_id"]
    params    = job["params"]
    step_path = job["step_path"]
    stl_path  = job["stl_path"]

    plan = {"part_id": part_id, "params": params}
    try:
        result = write_cadquery_artifacts(plan, label, step_path, stl_path, _root)
        ok   = not result.get("error")
        bbox = result.get("bbox")
        err  = result.get("error") or ""
    except Exception as exc:
        ok, bbox, err = False, None, str(exc)

    return {"label": label, "part_id": part_id, "bbox": bbox, "error": err, "ok": ok}


# ---------------------------------------------------------------------------
# Mesh verification (optional, runs after generation)
# ---------------------------------------------------------------------------

def _verify_mesh(parts_file: Path, parts: list[dict]) -> None:
    try:
        from mesh_check import detect_pairs_from_parts, check_pair, _print_header, _print_row
    except ImportError:
        print("[batch] mesh_check not available — skipping gear validation")
        return

    pairs = detect_pairs_from_parts(parts)
    if not pairs:
        print("[batch] No gear pairs detected for mesh check.")
        return

    print(f"\n[batch] Gear mesh check ({len(pairs)} pair(s)):")
    _print_header()
    results = []
    for wheel_entry, pinion_entry in pairs:
        p = wheel_entry.get("params", {})
        q = pinion_entry.get("params", {})
        n1, m1 = int(p.get("n_teeth", 0)), float(p.get("module_mm", 1.0))
        n2, m2 = int(q.get("n_teeth", 0)), float(q.get("module_mm", 1.0))
        if n1 == 0 or n2 == 0:
            continue
        r = check_pair(
            wheel_entry.get("label", "?"), n1, m1,
            pinion_entry.get("label", "?"), n2, m2,
        )
        _print_row(r)
        results.append(r)

    passed = sum(1 for r in results if r["pass"])
    failed = len(results) - passed
    print(f"\n[batch] Mesh check: {passed}/{len(results)} pairs OK"
          + (f", {failed} FAILED" if failed else ""))
    if failed:
        print("[batch] WARNING: mismatched modules will not mesh — update parts JSON")


# ---------------------------------------------------------------------------
# Core batch runner
# ---------------------------------------------------------------------------

def run_batch(
    parts_file: Path,
    skip_existing: bool = False,
    only_filter: str | None = None,
    dry_run: bool = False,
    workers: int = 1,
    verify_mesh: bool = False,
    render: bool = False,
) -> None:
    OUT_STEP.mkdir(parents=True, exist_ok=True)
    OUT_STL.mkdir(parents=True, exist_ok=True)
    if render:
        OUT_SHOTS.mkdir(parents=True, exist_ok=True)

    parts = load_parts(parts_file)
    total = len(parts)

    # Apply --only filter
    if only_filter:
        filt = only_filter.lower()
        parts = [p for p in parts if filt in p["label"].lower()]
        print(f"[batch] Filter '{only_filter}' matched {len(parts)}/{total} parts")
        total = len(parts)

    if total == 0:
        print("[batch] No parts to process.")
        return

    print(f"[batch] Processing {total} part(s) from {parts_file}")
    if dry_run:
        print("[batch] DRY RUN — no files will be written\n")
    elif workers > 1:
        print(f"[batch] Parallel mode: {workers} workers\n")
    print()

    # Build per-part job descriptors
    jobs: list[dict] = []
    results: list[tuple[str, str, str, str, str]] = []  # (status, label, part_id, bbox, err)

    for entry in parts:
        label   = entry["label"]
        template = entry.get("template", entry.get("part_id", "aria_spacer"))
        params   = entry.get("params", {})
        slug     = _slug_for(label)
        step_path = str(OUT_STEP / f"{slug}.step")
        stl_path  = str(OUT_STL  / f"{slug}.stl")

        if dry_run:
            print(f"  [DRY] {label:35s}  -> {slug}.step")
            results.append(("DRY", label, template, "-", ""))
            continue

        if skip_existing and Path(step_path).exists():
            sz = _fmt_size(Path(step_path).stat().st_size)
            print(f"  [SKIP] {label:34s}  (exists, {sz})")
            results.append(("SKIP", label, template, "-", ""))
            continue

        jobs.append({
            "label":     label,
            "part_id":   template,   # template key selects the CQ function
            "params":    params,
            "step_path": step_path,
            "stl_path":  stl_path,
        })

    if dry_run:
        print(f"\n{'='*65}")
        print(f"[batch] DRY RUN complete — {total} parts listed, 0 generated")
        print(f"{'='*65}")
        return

    if not jobs:
        # All skipped
        skipped = sum(1 for r in results if r[0] == "SKIP")
        print(f"\n[batch] All {skipped} part(s) skipped (already exist).")
        if verify_mesh:
            _verify_mesh(parts_file, load_parts(parts_file))
        return

    # ── Run jobs ──────────────────────────────────────────────────────────────
    if workers <= 1:
        # Serial: run in-process, gc between parts
        for i, job in enumerate(jobs, 1):
            prefix = f"[{i:3d}/{len(jobs)}]"
            res = _generate_one(job)
            _record_and_print(prefix, res, results)
            if render and res["ok"]:
                slug = _slug_for(res["label"])
                png_path = OUT_SHOTS / f"{slug}.png"
                render_err = _render_stl(job["stl_path"], png_path)
                if render_err:
                    print(f"           {'':34s}  [render] WARN: {render_err[:80]}")
                else:
                    print(f"           {'':34s}  [render] -> screenshots/{slug}.png")
            gc.collect()
    else:
        # Parallel: each part runs in its own worker process
        # Use a dict to preserve submission order for display
        future_to_index: dict = {}
        ordered: list[dict | None] = [None] * len(jobs)

        with ProcessPoolExecutor(max_workers=workers) as pool:
            for idx, job in enumerate(jobs):
                fut = pool.submit(_generate_one, job)
                future_to_index[fut] = idx

            for fut in as_completed(future_to_index):
                idx = future_to_index[fut]
                job = jobs[idx]
                prefix = f"[{idx+1:3d}/{len(jobs)}]"
                try:
                    res = fut.result()
                except Exception as exc:
                    res = {"label": job["label"], "part_id": job["part_id"],
                           "bbox": None, "error": str(exc), "ok": False}
                _record_and_print(prefix, res, results)
                if render and res["ok"]:
                    slug = _slug_for(res["label"])
                    png_path = OUT_SHOTS / f"{slug}.png"
                    render_err = _render_stl(job["stl_path"], png_path)
                    if render_err:
                        print(f"           {'':34s}  [render] WARN: {render_err[:80]}")
                    else:
                        print(f"           {'':34s}  [render] -> screenshots/{slug}.png")

    # ── Summary ───────────────────────────────────────────────────────────────
    real_results = [r for r in results if r[0] not in ("SKIP", "DRY")]
    passed  = sum(1 for r in real_results if r[0] == "OK")
    skipped = sum(1 for r in results if r[0] == "SKIP")
    failed  = sum(1 for r in real_results if r[0] == "FAIL")

    print(f"\n{'='*65}")
    print(f"[batch] Complete: {passed}/{len(real_results)} passed"
          + (f", {skipped} skipped" if skipped else "")
          + (f", {failed} FAILED" if failed else ""))

    if failed:
        print(f"\nFailed parts:")
        for status, label, pid, bbox, err in results:
            if status == "FAIL":
                last_line = err.strip().splitlines()[-1][:100] if err.strip() else "unknown error"
                print(f"  {label} ({pid}): {last_line}")

    print(f"\nSTEP files: {OUT_STEP}")
    print(f"STL  files: {OUT_STL}")
    print(f"{'='*65}")

    if verify_mesh:
        _verify_mesh(parts_file, load_parts(parts_file))


def _record_and_print(
    prefix: str,
    res: dict,
    results: list,
) -> None:
    ok    = res["ok"]
    label = res["label"]
    pid   = res["part_id"]
    bbox  = _fmt_bbox(res.get("bbox"))
    err   = res.get("error") or ""

    status = "OK  " if ok else "FAIL"
    print(f"  {prefix} [{status}] {label:34s}  bbox={bbox}")
    if not ok and err:
        short_err = err.strip().splitlines()[-1][:100] if err.strip() else ""
        print(f"           {'':34s}  ERR: {short_err}")

    results.append((status.strip(), label, pid, bbox, err))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch CAD part generator — reads a JSON parts list and generates STEP+STL for each."
    )
    parser.add_argument("parts_file", type=Path, help="Path to parts list JSON file")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip parts whose STEP file already exists",
    )
    parser.add_argument(
        "--only",
        metavar="FILTER",
        default=None,
        help="Only process parts whose label contains FILTER (case-insensitive substring)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List parts that would be generated without actually running the CAD pipeline",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel worker processes (default: 1 = serial)",
    )
    parser.add_argument(
        "--verify-mesh",
        action="store_true",
        help="After generation, check gear pair module compatibility from the parts JSON",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="Render each generated STL to a PNG preview saved in outputs/screenshots/",
    )
    args = parser.parse_args()

    parts_file = args.parts_file
    if not parts_file.is_absolute():
        parts_file = ROOT / parts_file
    if not parts_file.exists():
        print(f"[batch] Error: parts file not found: {parts_file}")
        sys.exit(1)

    run_batch(
        parts_file=parts_file,
        skip_existing=args.skip_existing,
        only_filter=args.only,
        dry_run=args.dry_run,
        workers=max(1, args.workers),
        verify_mesh=args.verify_mesh,
        render=args.render,
    )


if __name__ == "__main__":
    main()
