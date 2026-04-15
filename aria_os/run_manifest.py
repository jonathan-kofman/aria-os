"""
aria_os/run_manifest.py — per-run output folder and manifest writer.

Every pipeline run produces:
    outputs/runs/<run_id>/
        part.step               (copy of primary STEP)
        part.stl                (copy of primary STL)
        run_manifest.json       (metadata, paths, agent flags, spec)

The run_id is a timestamp + short UUID: 20260409T215033_a3f1c9b2
Concurrent runs never share a directory.

Schema versions
---------------
1.0  Single-process legacy schema (no operations array)
2.0  Multi-process bridge V2 (operations[] populated from DFM or explicit)
2.1  Adds pipeline_stats + mesh_stats blocks for per-run quality telemetry:
       - pipeline_stats: agent iterations used, wall time, success_agent,
         llm_calls broken down by provider, llm_total_calls
       - mesh_stats: triangle count, bbox, volume_cm3, watertight flag
     Older fields are unchanged so existing readers (MillForge bridge,
     dashboard run history) keep working.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
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


def _mesh_stats(stl_path: str | Path | None) -> dict[str, Any]:
    """
    Probe an STL for basic geometry stats. Best-effort: returns whatever
    fields succeed and silently skips the rest. Never raises.

    Lightweight enough to run in the manifest hot path — it's a single
    trimesh.load + a couple of property accesses, no remeshing.
    """
    out: dict[str, Any] = {}
    if not stl_path:
        return out
    p = Path(stl_path)
    if not p.is_file():
        return out
    try:
        import trimesh  # noqa: PLC0415
        mesh = trimesh.load(str(p), force="mesh")
        if hasattr(mesh, "vertices") and hasattr(mesh, "faces"):
            out["triangle_count"] = int(len(mesh.faces))
            out["vertex_count"] = int(len(mesh.vertices))
            ext = mesh.bounding_box.extents
            out["bbox_mm"] = [round(float(x), 3) for x in ext]
            try:
                vol_mm3 = float(mesh.volume)
                out["volume_cm3"] = round(vol_mm3 / 1000.0, 3)
            except Exception:
                pass
            try:
                out["watertight"] = bool(mesh.is_watertight)
            except Exception:
                pass
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _pipeline_stats(
    session: dict[str, Any],
    started_at: float | None,
    agent_iterations: int | None,
) -> dict[str, Any]:
    """
    Build a per-run pipeline_stats block from session data. All fields are
    optional — if the orchestrator didn't record them, the field is None
    or absent rather than fabricated.
    """
    # The orchestrator's existing schema uses "agent_iterations" for the
    # iteration count actually used; the v2.1 manifest exposes it under
    # "agent_iterations_used" for clarity. Accept either name.
    iters_used = (
        session.get("agent_iterations_used")
        or session.get("agent_iterations")
        or agent_iterations
    )
    stats: dict[str, Any] = {
        "agent_iterations_used": iters_used,
        "stalled": bool(session.get("agent_stalled", False)),
        "agent_converged": session.get("agent_converged"),
        "success_agent": session.get("success_agent"),  # template | cadsmith | llm | refiner
    }

    if started_at is not None:
        stats["wall_time_seconds"] = round(time.time() - started_at, 2)
    elif session.get("wall_time_seconds") is not None:
        stats["wall_time_seconds"] = round(float(session["wall_time_seconds"]), 2)

    # LLM call counts. The llm_client emits print() lines like "[LLM] anthropic"
    # but here we expect the orchestrator to aggregate and stash structured counts
    # in session["llm_calls"] = {"anthropic": int, "gemini": int, ...}.
    llm_calls = session.get("llm_calls") or {}
    if isinstance(llm_calls, dict):
        stats["llm_calls"] = {k: int(v) for k, v in llm_calls.items() if isinstance(v, (int, float))}
        stats["llm_total_calls"] = int(sum(stats["llm_calls"].values()))

    # Failure history (one entry per failed iteration). Useful for debugging.
    failures = session.get("agent_failures")
    if isinstance(failures, list):
        stats["agent_failures"] = failures[-10:]  # cap to last 10

    return stats


def create_run(
    *,
    run_id: str,
    goal: str,
    session: dict[str, Any],
    spec: dict[str, Any] | None = None,
    agent_mode: bool = True,
    agent_iterations: int | None = None,
    repo_root: Path,
    started_at: float | None = None,
) -> Path:
    """
    Create outputs/runs/<run_id>/, copy artifacts, write run_manifest.json.
    Returns the run directory Path.

    started_at: optional time.time() value captured at orchestrator start.
                When provided, manifest.pipeline_stats.wall_time_seconds is
                populated from (now - started_at). The orchestrator should
                pass this; older callers that don't pass it still work.
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

    # Schema version: 2.1 once the new pipeline_stats / mesh_stats blocks land,
    # 2.0 when only operations[] is populated, 1.0 otherwise.
    if operations:
        schema_ver = "2.1"
    elif session.get("agent_iterations_used") or session.get("llm_calls"):
        # Have telemetry but no operations array — still 2.1 so readers know
        # the pipeline_stats fields are available.
        schema_ver = "2.1"
    else:
        schema_ver = "1.0"

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

        # V2.1: per-run quality telemetry (agent iterations, LLM calls, wall time)
        "pipeline_stats": _pipeline_stats(session, started_at, agent_iterations),

        # V2.1: lightweight STL geometry stats (triangle count, bbox, watertight)
        "mesh_stats": _mesh_stats(copied_stl or stl_src),
    }

    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return run_dir
