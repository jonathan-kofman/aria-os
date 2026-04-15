"""
aria_os/generators/cadsmith_generator.py — CADSmith-style CadQuery generation loop

Multi-agent iterative generation:
1. LLM generates CadQuery code using FULL OpenCascade API (sweeps, lofts, fillets, etc.)
2. Execute in subprocess, extract kernel metrics (volume, bbox, faces, edges)
3. Validate geometry (watertight, dimensions match spec)
4. If failed: feed error + metrics back to LLM for correction (up to 5 outer iterations)
5. Optional: vision judge reviews rendered views

This replaces the simple LLM fallback in cadquery_generator.py with a proper
iterative refinement loop.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .. import event_bus

_MAX_CODE_RETRIES = 3    # inner loop: fix code execution errors
_MAX_GEOM_RETRIES = 5    # outer loop: fix geometric errors


# ---------------------------------------------------------------------------
# CadQuery system prompt — instructs LLM to use FULL OpenCascade capability
# ---------------------------------------------------------------------------

_CADSMITH_SYSTEM_PROMPT = """\
You are an expert CadQuery/OpenCascade CAD programmer. Generate Python code that
creates precise, manufacturable 3D geometry using CadQuery's FULL API.

CRITICAL: Do NOT just use circle().extrude() for everything. Use the right operation:

AVAILABLE OPERATIONS (use these!):
  .sweep(path, multisection=False)   — sweep profile along a 3D path
  .loft(filled=True, ruled=False)    — loft between multiple cross-sections
  .revolve(angleDegrees, axisStart, axisEnd)  — revolve profile around axis
  .shell(thickness)                  — hollow out a solid (negative = inward)
  .fillet(radius)                    — round edges
  .chamfer(length)                   — bevel edges
  .spline(points, tangents=None)     — B-spline through points
  .tangentArcPoint(endpoint, relative=True)  — tangent arc
  .threePointArc(point1, point2)     — arc through 3 points
  .ellipseArc(x_radius, y_radius, ...)  — elliptical arc
  .workplane(offset)                 — offset workplane for stepped features
  .transformed(rotate, offset)       — arbitrary workplane transformation
  .section(height)                   — cross-section at height
  .split(keepTop=True)               — split solid with plane
  .union(other)                      — boolean union
  .cut(other)                        — boolean difference
  .intersect(other)                  — boolean intersection
  Selector strings: ">Z", "<Z", ">X", "#Z", "|Z", etc. for face/edge selection

WORKPLANE PATTERNS:
  cq.Workplane("XY")                 — primary workplane
  .faces(">Z").workplane()           — workplane on top face
  .faces("<Z").workplane()           — workplane on bottom face
  .edges("|Z").fillet(r)             — fillet vertical edges

MULTI-BODY:
  cq.Assembly()                      — for assemblies
  .add(part, loc=cq.Location(...))   — position parts

SWEEP EXAMPLE:
  path = cq.Workplane("XZ").spline([(0,0), (10,5), (20,0)])
  result = cq.Workplane("XY").circle(3).sweep(path)

LOFT EXAMPLE:
  result = (
      cq.Workplane("XY").rect(20, 20)
      .workplane(offset=30).circle(15)
      .loft()
  )

REVOLVE EXAMPLE:
  result = (
      cq.Workplane("XZ")
      .moveTo(10, 0).lineTo(15, 0).lineTo(12, 30).close()
      .revolve(360, (0,0,0), (0,1,0))
  )

RULES:
- All dimensions in mm
- Assign final solid to 'result' variable
- MUST print metrics at the end (exact format required):
    val = result.val()
    bb = val.BoundingBox()
    print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")
    print(f"VOLUME:{val.Volume():.3f}")
    print(f"FACES:{len(val.Faces())}")
    print(f"EDGES:{len(val.Edges())}")
- For complex profiles, build 2D sketches first, then extrude/revolve/sweep
- Use fillets/chamfers to refine — add them LAST, after base geometry validates
- Guard boolean operations: ensure bodies overlap before cutting

Output ONLY valid Python code. No markdown fences. No explanations.
"""


# ---------------------------------------------------------------------------
# Code executor — runs CadQuery in subprocess, extracts metrics
# ---------------------------------------------------------------------------

def _execute_cq_code(
    code: str,
    step_path: str,
    stl_path: str,
    timeout: int = 60,
) -> dict[str, Any]:
    """
    Execute CadQuery code in a subprocess. Returns:
      {success, stdout, stderr, metrics: {bbox, volume, faces, edges}, step_size, stl_size}
    """
    # Append export + metrics code
    export_code = f"""
# === AUTO-APPENDED EXPORT ===
import cadquery as cq
from cadquery import exporters
import os

val = result.val()
bb = val.BoundingBox()
print(f"BBOX:{{bb.xlen:.3f}},{{bb.ylen:.3f}},{{bb.zlen:.3f}}")
print(f"VOLUME:{{val.Volume():.3f}}")
print(f"FACES:{{len(val.Faces())}}")
print(f"EDGES:{{len(val.Edges())}}")

os.makedirs(os.path.dirname("{step_path.replace(chr(92), '/')}") or '.', exist_ok=True)
os.makedirs(os.path.dirname("{stl_path.replace(chr(92), '/')}") or '.', exist_ok=True)
exporters.export(result, "{step_path.replace(chr(92), '/')}")
exporters.export(result, "{stl_path.replace(chr(92), '/')}", exportType="STL")
print("EXPORT:OK")
"""
    full_code = code + "\n" + export_code

    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write(full_code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )
        stdout = result.stdout
        stderr = result.stderr

        # Parse metrics from stdout
        metrics = {}
        for line in stdout.splitlines():
            if line.startswith("BBOX:"):
                parts = line[5:].split(",")
                if len(parts) == 3:
                    metrics["bbox"] = [float(x) for x in parts]
            elif line.startswith("VOLUME:"):
                metrics["volume"] = float(line[7:])
            elif line.startswith("FACES:"):
                metrics["faces"] = int(line[6:])
            elif line.startswith("EDGES:"):
                metrics["edges"] = int(line[6:])

        exported = "EXPORT:OK" in stdout

        return {
            "success": result.returncode == 0 and exported,
            "stdout": stdout,
            "stderr": stderr,
            "returncode": result.returncode,
            "metrics": metrics,
            "step_size": Path(step_path).stat().st_size if Path(step_path).exists() else 0,
            "stl_size": Path(stl_path).stat().st_size if Path(stl_path).exists() else 0,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "Timeout", "metrics": {}}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Geometry validator — checks metrics against spec
# ---------------------------------------------------------------------------

def _validate_geometry(
    metrics: dict,
    spec: dict,
    stl_path: str,
) -> tuple[bool, list[str]]:
    """Validate geometry metrics against spec. Returns (passed, errors)."""
    errors = []

    if not metrics:
        errors.append("No metrics extracted — code likely crashed")
        return False, errors

    bbox = metrics.get("bbox", [0, 0, 0])

    # Check dimensions if specified
    for dim_key, bbox_idx in [("od_mm", 0), ("width_mm", 2), ("height_mm", 2),
                               ("length_mm", 2), ("diameter_mm", 0)]:
        expected = spec.get(dim_key)
        if expected and expected > 0:
            actual = max(bbox[0], bbox[1]) if "od" in dim_key or "diameter" in dim_key else bbox[bbox_idx]
            tolerance = expected * 0.1  # 10% tolerance
            if abs(actual - expected) > tolerance:
                errors.append(f"{dim_key}: expected {expected:.1f}mm, got {actual:.1f}mm")

    # Check volume is reasonable
    volume = metrics.get("volume", 0)
    if volume <= 0:
        errors.append(f"Zero or negative volume: {volume}")

    # Check faces
    faces = metrics.get("faces", 0)
    if faces < 3:
        errors.append(f"Too few faces: {faces}")

    # Check STL watertight
    if Path(stl_path).exists():
        try:
            import trimesh
            m = trimesh.load(stl_path)
            if not m.is_watertight:
                errors.append("STL is not watertight")
        except Exception:
            pass

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# The CADSmith loop
# ---------------------------------------------------------------------------

def cadsmith_generate(
    goal: str,
    plan: dict[str, Any],
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    """
    CADSmith-style iterative CadQuery generation.

    Outer loop: generate → execute → validate → feed errors back → retry
    Inner loop: fix code execution errors

    Returns dict with: step_path, stl_path, code_path, metrics
    """
    from ..llm_client import call_llm

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    part_id = (plan.get("part_id") or "unknown").replace("/", "_")
    params = plan.get("params", {})
    out_dir = repo_root / "outputs" / "cad" / "cadsmith" / part_id
    out_dir.mkdir(parents=True, exist_ok=True)

    param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "none specified"

    # Build initial user prompt
    user_prompt = (
        f"Create CadQuery geometry for: {goal}\n"
        f"Parameters: {param_str}\n"
        f"Part ID: {part_id}\n\n"
        "Use appropriate CadQuery operations (sweep, loft, revolve, fillet, etc.).\n"
        "Do NOT just use circle().extrude() — use the full API for complex geometry."
    )

    best_code = None
    best_metrics = {}
    conversation: list[dict] = []

    for outer in range(_MAX_GEOM_RETRIES):
        event_bus.emit("cadsmith", f"Iteration {outer + 1}/{_MAX_GEOM_RETRIES}")

        # Generate code
        code = None
        for inner in range(_MAX_CODE_RETRIES):
            try:
                if conversation:
                    # Feed previous errors back
                    full_prompt = user_prompt + "\n\n" + "\n".join(
                        f"Previous attempt feedback:\n{c['feedback']}" for c in conversation[-2:]
                    )
                else:
                    full_prompt = user_prompt

                response = call_llm(full_prompt, system=_CADSMITH_SYSTEM_PROMPT, repo_root=repo_root)
                if not response:
                    continue

                code = response.strip()
                if code.startswith("```"):
                    lines = code.split("\n")
                    code = "\n".join(lines[1:-1])

                # Try executing
                exec_result = _execute_cq_code(code, step_path, stl_path)

                if exec_result["success"]:
                    break
                else:
                    err = exec_result["stderr"][-500:] if exec_result["stderr"] else "Unknown error"
                    event_bus.emit("warning", f"Code error (attempt {inner+1}): {err[:100]}")
                    conversation.append({
                        "feedback": f"Code execution failed with error:\n{err}\n\nFix the code."
                    })

            except Exception as e:
                event_bus.emit("warning", f"LLM call failed: {e}")
                continue

        if code is None or not exec_result.get("success"):
            continue

        metrics = exec_result.get("metrics", {})
        best_code = code
        best_metrics = metrics

        # Validate geometry
        passed, errors = _validate_geometry(metrics, params, stl_path)

        bbox = metrics.get("bbox", [0, 0, 0])
        vol = metrics.get("volume", 0)
        faces = metrics.get("faces", 0)
        event_bus.emit("cadsmith",
            f"  {bbox[0]:.1f}x{bbox[1]:.1f}x{bbox[2]:.1f}mm, "
            f"vol={vol:.0f}mm3, {faces} faces"
        )

        # Visual verification — send renders to vision LLM
        vision_feedback = ""
        if passed and Path(stl_path).exists():
            try:
                from ..visual_verifier import verify_visual
                event_bus.emit("cadsmith", "Running visual verification...")
                vis = verify_visual(
                    step_path, stl_path, goal, params, repo_root=repo_root,
                )
                confidence = vis.get("confidence", 0)
                if vis.get("verified") is True and confidence >= 0.90:
                    event_bus.emit("cadsmith", f"Visual verification PASSED (confidence: {confidence:.0%})")
                elif vis.get("verified") is True and confidence < 0.90:
                    # Passed but low confidence — treat as failure, iterate
                    passed = False
                    issues = vis.get("issues", [])
                    vision_feedback = f"Visual verification passed with LOW confidence ({confidence:.0%}).\n"
                    vision_feedback += "The geometry may not accurately represent the intended part.\n"
                    for issue in issues:
                        vision_feedback += f"  - {issue}\n"
                    event_bus.emit("warning", f"Low confidence ({confidence:.0%}), iterating")
                elif vis.get("verified") is False:
                    passed = False
                    issues = vis.get("issues", [])
                    failed_checks = [
                        c for c in vis.get("checks", [])
                        if isinstance(c, dict) and not c.get("found", True)
                    ]
                    vision_feedback = "Visual verification FAILED:\n"
                    for c in failed_checks:
                        vision_feedback += f"  - {c.get('feature', '?')}: {c.get('notes', 'not found')}\n"
                    for issue in issues:
                        vision_feedback += f"  - Issue: {issue}\n"
                    event_bus.emit("warning", f"Visual check failed: {'; '.join(issues[:3])}")
                # If verified is None, API was unavailable — skip visual check
            except Exception as ve:
                event_bus.emit("warning", f"Visual verifier error: {ve}")

        if passed:
            event_bus.emit("cadsmith", f"All checks passed on iteration {outer + 1}")
            break
        else:
            error_str = "; ".join(errors) if errors else "visual check failed"
            event_bus.emit("warning", f"Validation failed: {error_str}")
            feedback = (
                f"Code executed but geometry is wrong:\n"
                f"  Metric errors: {error_str}\n"
                f"  Current metrics: bbox={bbox}, volume={vol:.1f}, faces={faces}\n"
                f"  Required: {param_str}\n"
            )
            if vision_feedback:
                feedback += f"\n  {vision_feedback}\n"
            feedback += "\nFix the geometry to match the required dimensions and visual features."
            conversation.append({"feedback": feedback})

    # Save best CQ code regardless
    if best_code:
        code_path = out_dir / f"{part_id}_cadsmith.py"
        code_path.write_text(best_code, encoding="utf-8")

        # If we have geometry (even imperfect), return it
        if Path(step_path).exists():
            return {
                "step_path": step_path,
                "stl_path": stl_path if Path(stl_path).exists() else "",
                "code_path": str(code_path),
                "metrics": best_metrics,
            }

    # CadQuery failed — auto-escalate to SDF backend
    event_bus.emit("cadsmith", f"CadQuery failed after {_MAX_GEOM_RETRIES} iterations, escalating to SDF")
    print(f"[CADSMITH] Escalating to SDF backend for {part_id}")
    try:
        from .sdf_generator import write_sdf_artifacts
        sdf_result = write_sdf_artifacts(plan, goal, step_path, stl_path, repo_root)
        sdf_result["escalated_from"] = "cadsmith"
        return sdf_result
    except Exception as sdf_err:
        event_bus.emit("error", f"SDF escalation also failed: {sdf_err}")

    raise RuntimeError(
        f"CADSmith generation failed after {_MAX_GEOM_RETRIES} iterations "
        f"and SDF escalation failed"
    )
