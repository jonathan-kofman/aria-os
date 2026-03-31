"""
aria_os/gh_integration/gh_to_step_bridge.py

run_gh_pipeline(goal, part_id, repo_root) → parses params, runs CEM check,
exports STEP/STL, appends structured entry to outputs/aria_generation_log.json.

This is the high-level entry point for the GH integration flow:

    goal → spec extraction → GH artifact write → CEM SF check
         → CQ fallback export → log entry append
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .gh_aria_parts import write_gh_artifacts, GH_SF_THRESHOLDS


def run_gh_pipeline(
    goal: str,
    part_id: str,
    repo_root: Optional[Path] = None,
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Run the full GH pipeline for a single part.

    Steps:
      1. Extract spec from goal (+ merge with explicit params)
      2. Run CEM physics check and compare against SF thresholds
      3. Write GH + CQ fallback artifacts to disk
      4. Execute CQ fallback to produce STEP + STL (headless)
      5. Append structured entry to outputs/aria_generation_log.json

    Returns a result dict with keys:
        part_id, step_path, stl_path, cem_sf, cem_passed, artifacts, log_entry
    """
    t0 = time.monotonic()

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    # 1. Spec extraction + param merge
    try:
        from aria_os.spec_extractor import extract_spec
        extracted = extract_spec(goal)
    except Exception:
        extracted = {}

    merged_params: dict[str, Any] = {**extracted, **(params or {})}

    # 2. CEM physics check
    cem_sf: dict[str, float] = {}
    cem_passed = True
    cem_warnings: list[str] = []

    thresholds = GH_SF_THRESHOLDS.get(part_id.lower(), {})
    if thresholds:
        try:
            cem_sf, cem_passed, cem_warnings = _run_cem_check(
                part_id, merged_params, thresholds, repo_root
            )
        except Exception as exc:
            cem_warnings.append(f"CEM check failed: {exc}")

    # 3. Write artifacts
    artifacts = write_gh_artifacts(part_id, merged_params, repo_root=repo_root)

    # 4. Execute CQ fallback for headless STEP/STL export
    step_path = ""
    stl_path  = ""
    exec_error: Optional[str] = None

    try:
        step_path, stl_path = _exec_cq_fallback(
            artifacts["cq_script"], part_id, repo_root
        )
    except Exception as exc:
        exec_error = str(exc)

    # 5. Log entry
    elapsed = round(time.monotonic() - t0, 2)
    log_entry = {
        "timestamp":    datetime.utcnow().isoformat() + "Z",
        "goal":         goal,
        "part_id":      part_id,
        "cem_sf":       cem_sf,
        "cem_passed":   cem_passed,
        "cem_warnings": cem_warnings,
        "step_path":    step_path,
        "stl_path":     stl_path,
        "elapsed_s":    elapsed,
        "error":        exec_error,
        "artifacts": {k: str(v) for k, v in artifacts.items()},
    }
    _append_generation_log(log_entry, repo_root)

    return {
        "part_id":    part_id,
        "step_path":  step_path,
        "stl_path":   stl_path,
        "cem_sf":     cem_sf,
        "cem_passed": cem_passed,
        "artifacts":  {k: str(v) for k, v in artifacts.items()},
        "log_entry":  log_entry,
        "error":      exec_error,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run_cem_check(
    part_id: str,
    params: dict,
    thresholds: dict[str, float],
    repo_root: Path,
) -> tuple[dict[str, float], bool, list[str]]:
    """
    Run a minimal CEM SF check using the existing cem_checks module.
    Returns (sf_dict, all_passed, warnings).
    """
    try:
        from aria_os import cem_checks
        from aria_os.context_loader import load_context

        context = load_context(repo_root)
        meta_path = repo_root / "outputs" / "cad" / "meta" / f"{part_id}.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        # Merge params into meta dims
        if params:
            meta.setdefault("dims_mm", {}).update(params)

        result = cem_checks.run_full_cem(part_id, meta, context, repo_root)
        sf_val = result.static_min_sf or 0.0
        sf_dict: dict[str, float] = {}
        for check_name in thresholds:
            sf_dict[check_name] = sf_val

        all_passed = all(
            sf_dict.get(k, 0.0) >= v for k, v in thresholds.items()
        )
        warnings: list[str] = list(result.cem_warnings or [])
        if not all_passed:
            for k, required in thresholds.items():
                actual = sf_dict.get(k, 0.0)
                if actual < required:
                    warnings.append(
                        f"SF for {k} = {actual:.2f} < required {required:.1f}"
                    )
        return sf_dict, all_passed, warnings

    except Exception as exc:
        return {}, False, [str(exc)]


def _exec_cq_fallback(script_path: Path, part_id: str, repo_root: Path) -> tuple[str, str]:
    """
    Execute the CadQuery fallback script and return (step_path, stl_path).
    Redirects /tmp outputs to repo outputs/cad/.
    """
    import subprocess
    import sys
    import os

    out_dir = repo_root / "outputs" / "cad"
    step_dir = out_dir / "step"
    stl_dir  = out_dir / "stl"
    step_dir.mkdir(parents=True, exist_ok=True)
    stl_dir.mkdir(parents=True, exist_ok=True)

    step_path = str(step_dir / f"{part_id}.step")
    stl_path  = str(stl_dir  / f"{part_id}.stl")

    # Patch the script to write to the right output paths
    script_text = script_path.read_text(encoding="utf-8")
    script_text = script_text.replace("/tmp/", str(out_dir / "tmp_").replace("\\", "/") + "")
    # Run patched script
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    result = subprocess.run(
        [sys.executable, "-c", script_text],
        capture_output=True, text=True, timeout=120, env=env, cwd=str(repo_root),
    )
    if result.returncode != 0:
        raise RuntimeError(f"CQ fallback failed: {result.stderr[-500:]}")

    # Locate generated files (they may be in /tmp or cwd)
    for candidate in [
        Path(f"/tmp/{part_id}.step"),
        out_dir / f"{part_id}.step",
        step_dir / f"{part_id}.step",
    ]:
        if candidate.exists():
            import shutil
            shutil.copy2(str(candidate), step_path)
            break

    for candidate in [
        Path(f"/tmp/{part_id}.stl"),
        out_dir / f"{part_id}.stl",
        stl_dir / f"{part_id}.stl",
    ]:
        if candidate.exists():
            import shutil
            shutil.copy2(str(candidate), stl_path)
            break

    return step_path, stl_path


def _append_generation_log(entry: dict, repo_root: Path) -> None:
    log_path = repo_root / "outputs" / "aria_generation_log.json"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        existing: list = []
        if log_path.exists():
            try:
                existing = json.loads(log_path.read_text(encoding="utf-8"))
            except Exception:
                existing = []
        existing.append(entry)
        log_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception:
        pass  # log failure is non-critical
