"""
aria_os/run_manifest.py — per-run output folder and manifest writer.

Every pipeline run produces:
    outputs/runs/<run_id>/
        part.step               (copy of primary STEP)
        part.stl                (copy of primary STL)
        run_manifest.json       (metadata, paths, agent flags, spec)

The run_id is a timestamp + short UUID: 20260409T215033_a3f1c9b2
Concurrent runs never share a directory.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROCESS_OP_NAMES: dict[str, str] = {
    "cnc_milling": "CNC mill all features",
    "milling": "CNC mill all features",
    "turning": "CNC turn to dimension",
    "cnc_turning": "CNC turn to dimension",
    "grinding": "Grind to final dimension",
    "sheet_metal": "Form sheet metal",
    "bending": "Press brake bend",
    "press_brake": "Press brake bend",
    "welding": "Weld assembly",
    "welding_arc": "MIG/TIG weld",
    "tig_welder": "TIG weld",
    "mig_welder": "MIG weld",
    "laser_cutter": "Laser cut profile",
    "cutting_laser": "Laser cut profile",
    "plasma_cutter": "Plasma cut",
    "waterjet": "Waterjet cut",
    "stamping": "Stamp / punch",
    "edm": "EDM to tolerance",
    "wire_edm": "Wire EDM",
    "injection_molding": "Injection mold",
    "inspection_station": "Dimensional inspection",
    "anodizing_line": "Anodize finish",
    "powder_coat_booth": "Powder coat",
    "heat_treat_oven": "Heat treat",
}


def _process_to_op_name(process: str) -> str:
    return _PROCESS_OP_NAMES.get(process.lower().strip(), f"Process: {process}")


def _git_commit(repo_root: Path) -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root, stderr=subprocess.DEVNULL, text=True
        )
        return out.strip()
    except Exception:
        return None


def create_run(
    *,
    run_id: str,
    goal: str,
    session: dict[str, Any],
    spec: dict[str, Any] | None = None,
    agent_mode: bool = True,
    agent_iterations: int | None = None,
    repo_root: Path,
) -> Path:
    """
    Create outputs/runs/<run_id>/, copy artifacts, write run_manifest.json.
    Returns the run directory Path.
    """
    run_dir = repo_root / "outputs" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    step_src = session.get("step_path", "")
    stl_src  = session.get("stl_path", "")

    # Copy primary artifacts into the run folder
    copied_step = ""
    copied_stl  = ""
    if step_src and Path(step_src).is_file():
        dest = run_dir / "part.step"
        shutil.copy2(step_src, dest)
        copied_step = str(dest)
    if stl_src and Path(stl_src).is_file():
        dest = run_dir / "part.stl"
        shutil.copy2(stl_src, dest)
        copied_stl = str(dest)

    # Copy CAM output if present
    cam_path = session.get("cam_path", "")
    copied_cam = ""
    if cam_path and Path(cam_path).is_file():
        dest = run_dir / Path(cam_path).name
        shutil.copy2(cam_path, dest)
        copied_cam = str(dest)

    # Build operations array for V2 bridge (process-agnostic multi-op schema)
    # Can be provided explicitly in session["operations"] or derived from DFM output.
    operations: list[dict[str, Any]] = session.get("operations") or []
    if not operations:
        # Derive a minimal single-operation entry from DFM process recommendation
        dfm = session.get("dfm_analysis", {})
        process = dfm.get("process_recommendation", "")
        if process:
            operations = [
                {
                    "sequence": 10,
                    "operation_name": _process_to_op_name(process),
                    "work_center_category": process,
                    "estimated_setup_min": 30,
                    "estimated_run_min": session.get("cam_result", {}).get("cycle_time_min", 60),
                    "ai_confidence": dfm.get("confidence", 0.8),
                    "detected_features": session.get("detected_features", []),
                }
            ]

    schema_ver = "2.0" if operations else "1.0"

    # Build manifest
    manifest: dict[str, Any] = {
        "schema_version": schema_ver,
        "run_id": run_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(repo_root),

        # Goal and spec
        "goal": goal,
        "spec": spec or session.get("spec") or {},

        # Pipeline flags
        "agent_mode": agent_mode,
        "agent_iterations": agent_iterations,

        # Artifacts (run-relative paths)
        "artifacts": {
            "step": "part.step" if copied_step else None,
            "stl":  "part.stl"  if copied_stl  else None,
            "cam":  Path(copied_cam).name if copied_cam else None,
        },

        # Legacy absolute paths (for tools that haven't moved to run dirs yet)
        "legacy_paths": {
            "step": step_src,
            "stl":  stl_src,
        },

        # Quality signals
        "validation": {
            "geometry_passed":  session.get("geometry_validation", {}).get("passed"),
            "visual_passed":    session.get("visual_verification", {}).get("passed"),
            "visual_confidence":session.get("visual_verification", {}).get("confidence"),
            "dfm_score":        session.get("dfm_analysis", {}).get("score"),
            "dfm_process":      session.get("dfm_analysis", {}).get("process_recommendation"),
            "watertight":       session.get("geometry_validation", {}).get("watertight"),
        },

        # V2: multi-process operations array (populated from DFM or explicit session data)
        "operations": operations,
    }

    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return run_dir
