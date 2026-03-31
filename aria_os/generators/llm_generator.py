"""
LLM-based Grasshopper/RhinoCommon script generator for arbitrary parts.
Uses the unified call_llm() fallback chain (Anthropic → Gemini → Ollama → None).
CadQuery-specific prompt removed 2026-03-23 — now generates RhinoCommon Python.
"""
import re
from pathlib import Path
from typing import Any, Optional

from ..context_loader import get_mechanical_constants, load_context
from ..cem_context import load_cem_geometry, format_cem_block
from ..cad_learner import get_few_shot_examples, format_few_shot_block, get_failure_patterns
from ..llm_client import call_llm


def _build_system_prompt(
    context: dict[str, str],
    plan: dict[str, Any],
    repo_root: Optional[Path] = None,
    *,
    goal: Optional[str] = None,
) -> str:
    """Build system prompt: RhinoCommon expert, constants, failures, patterns, required ending."""
    constants = get_mechanical_constants(context)
    constants_block = "\n".join(f"#   {k}: {v}" for k, v in sorted(constants.items()))
    g = (goal or "").strip()
    pid = (plan.get("part_id") or "") if isinstance(plan.get("part_id"), str) else ""
    cem = load_cem_geometry(repo_root, goal=g, part_id=pid)
    cem_block = format_cem_block(cem)
    examples = get_few_shot_examples(plan.get("text", ""), plan.get("part_id", ""), repo_root)
    few_shot_block = format_few_shot_block(examples)
    part_failures = get_failure_patterns(plan.get("part_id", ""), repo_root)
    learned_failures = "\n".join(f"- {e}" for e in part_failures) if part_failures else ""

    avoid = """
- Never use annular profile as first operation. Build solid cylinder/box first, then remove interior.
- Always call rs.AddPlanarSrf or rg.Brep.CreateFromBox before cutting; never operate on an empty scene.
- For hollow parts: create outer solid, then Boolean difference the inner void.
- WRONG: rg.BooleanDifference(a, b) — this does not exist; do NOT use it.
- WRONG: rg.BooleanUnion(a, b) — this does not exist; do NOT use it.
- CORRECT: rg.Brep.CreateBooleanDifference([a], [b], 0.001) — always returns list or None; guard with [0] if result else fallback.
- CORRECT: rg.Brep.CreateBooleanUnion([a, b], 0.001) — always returns list or None; guard with [0] if result else fallback.
- WRONG: rg.Cylinder(plane, radius, height) used directly as a Brep — Cylinder is NOT a Brep.
- CORRECT: circle = rg.Circle(plane, radius); cyl = rg.Cylinder(circle, height).ToBrep(True, True)
- WRONG: rg.Box(plane, x, y, z) used directly as a Brep — Box is NOT a Brep.
- CORRECT: box = rg.Box(plane, rg.Interval(0,x), rg.Interval(0,y), rg.Interval(0,z)); brep = box.ToBrep()
- Do not apply fillet/chamfer (rs.FilletEdge) in the first attempt — add only after base solid validates.
- Revolve profiles must be closed curves. Ensure polyline is closed before revolving.
- Asymmetric teeth: drive face ~8 deg from radial (steep), back face ~60 deg (gradual). Never identical angles.
- For polar arrays use rs.RotateObject in a loop; do not rely on rs.ArrayPolar (not always available).
- All print("BBOX:...") calls must use exact format: BBOX:xlen,ylen,zlen (no spaces).
- File paths in STEP_PATH/STL_PATH use forward slashes (already provided); do not add backslashes.
"""

    return f"""You are a Grasshopper/RhinoCommon Python expert. Output ONLY a Python code block. No explanation, no markdown outside the block.

Imports (use exactly):
  import rhinoscriptsyntax as rs
  import Rhino.Geometry as rg
  import math

Rules:
- All dimensions in mm.
- Build order: base solid first, then Boolean cuts, then additive features, then holes last.
- Use rg.Brep operations for geometry; rs.* helpers for scene manipulation.
- The script runs inside Rhino Compute (headless). Do not call rs.GetObject or any interactive command.
- Write STEP via Rhino.FileIO.FileWriteOptions or via scriptcontext; write STL via rs.ExportObjects.

Mechanical constants (from aria_mechanical.md) — use these when relevant:
{constants_block}

{cem_block}
{few_shot_block}
{("# Known recent failures for this part:" + chr(10) + "# " + learned_failures.replace(chr(10), chr(10) + "# ")) if learned_failures else ""}

Avoid these patterns:
{avoid}

Required code structure:

  ## REQUIRED: All numeric dimensions must be module-level constants

  Every dimension must be declared as an ALL_CAPS module-level constant before use.

  Required format:
    # === PART PARAMETERS (tunable) ===
    LENGTH_MM = 60.0
    WIDTH_MM = 12.0
    THICKNESS_MM = 6.0
    PIVOT_HOLE_DIA_MM = 6.0
    # === END PARAMETERS ===

    # geometry uses constants only, never inline numbers

Common RhinoCommon patterns:
  Box:       rg.Box(rg.Plane.WorldXY, rg.Interval(0, L), rg.Interval(0, W), rg.Interval(0, H))
             brep = box.ToBrep()
  Cylinder:  circle = rg.Circle(rg.Plane.WorldXY, RADIUS_MM)
             cyl = rg.Cylinder(circle, HEIGHT_MM).ToBrep(True, True)
  Difference: _r = rg.Brep.CreateBooleanDifference([solid_a], [solid_b], 0.001)
              result = _r[0] if _r else solid_a   # always guard — returns list or None
  Union:      _r = rg.Brep.CreateBooleanUnion([brep_a, brep_b], 0.001)
              result = _r[0] if _r else brep_a
  Intersect:  _r = rg.Brep.CreateBooleanIntersection([solid_a], [solid_b], 0.001)
              result = _r[0] if _r else None
  Revolve:   profile = rg.Polyline(pts).ToNurbsCurve()
             axis = rg.Line(rg.Point3d(0,0,0), rg.Point3d(0,0,1))
             revolved = rg.Brep.CreateFromRevSurface(
                 rg.RevSurface.Create(profile, axis, 0, 2*math.pi), True, True)
  Polar array + union:
             result = base_brep
             for i in range(N):
                 angle = i * 2 * math.pi / N
                 xform = rg.Transform.Rotation(angle, rg.Vector3d(0,0,1), rg.Point3d.Origin)
                 tooth_copy = tooth_brep.Duplicate()
                 tooth_copy.Transform(xform)
                 _u = rg.Brep.CreateBooleanUnion([result, tooth_copy], 0.001)
                 if _u:
                     result = _u[0]

Every generated script MUST end with these exact lines (STEP_PATH, STL_PATH and PART_NAME are injected at runtime):
  bb = result.GetBoundingBox(True)
  xlen = bb.Max.X - bb.Min.X
  ylen = bb.Max.Y - bb.Min.Y
  zlen = bb.Max.Z - bb.Min.Z
  print(f"BBOX:{{xlen:.3f}},{{ylen:.3f}},{{zlen:.3f}}")

  # Export STEP
  import scriptcontext as sc
  import Rhino
  _obj_id = sc.doc.Objects.AddBrep(result)
  Rhino.RhinoDoc.ActiveDoc.Objects.Select(_obj_id)
  rs.Command(f'_-Export "{{STEP_PATH}}" _Enter', False)

  # Export STL
  rs.Command(f'_-Export "{{STL_PATH}}" _Enter _Enter', False)

  # === META JSON (required for optimizer and CEM) ===
  import json as _json, pathlib as _pathlib
  _meta = {{
      "part_name": PART_NAME,
      "bbox_mm": {{"x": round(xlen, 3), "y": round(ylen, 3), "z": round(zlen, 3)}},
      "dims_mm": {{}}
  }}
  import sys as _sys
  _frame_vars = {{k: v for k, v in globals().items() if k.endswith('_MM') and isinstance(v, (int, float))}}
  _meta["dims_mm"] = _frame_vars
  _json_path = _pathlib.Path(STEP_PATH).parent.parent / "meta" / (_pathlib.Path(STEP_PATH).stem + ".json")
  _json_path.parent.mkdir(parents=True, exist_ok=True)
  _json_path.write_text(_json.dumps(_meta, indent=2))
  print(f'META:{{_json_path}}')

The variable 'result' must be the final rg.Brep. Do not define STEP_PATH, STL_PATH or PART_NAME; they are provided."""


def _build_user_prompt(
    plan: dict[str, Any],
    previous_code: Optional[str] = None,
    previous_error: Optional[str] = None,
) -> str:
    """Build user prompt from plan dict; optionally include previous attempt and error."""
    lines: list[str] = []
    brief = plan.get("engineering_brief")
    if brief:
        lines.extend(
            [
                "=== ENGINEERING BRIEF (authoritative — follow this over the short user phrase) ===",
                str(brief).strip(),
                "",
                "=== STRUCTURED PLAN (summary) ===",
            ]
        )
    lines.extend(
        [
            "Plan (structured):",
            plan.get("text", str(plan)),
            "",
            "Build order:",
        ]
    )
    for s in plan.get("build_order", []):
        lines.append(f"  - {s}")
    lines.append("")
    lines.append("Generate Grasshopper/RhinoCommon Python for this part. Output code only.")
    if previous_error and previous_code:
        lines.append("")
        lines.append(f"Previous attempt failed with: {previous_error}")
        lines.append("Previous code was:")
        lines.append("```")
        lines.append(previous_code[:4000] if len(previous_code) > 4000 else previous_code)
        lines.append("```")
        lines.append("Fix the specific issue and regenerate.")
    return "\n".join(lines)


def _extract_code(response: str) -> Optional[str]:
    """Extract Python code from ```python ... ``` or full response."""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", response, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Accept any response containing rhinoscriptsyntax or Rhino.Geometry imports
    if "import rhinoscriptsyntax" in response or "import Rhino.Geometry" in response or "Rhino.Geometry" in response:
        return response.strip()
    return None


def _call_unified_llm(
    system: str,
    user: str,
    repo_root: Optional[Path] = None,
) -> Optional[str]:
    """Call unified LLM fallback chain (Anthropic → Gemini → Ollama → None).

    Returns raw response text or None if all backends are unavailable.
    Never raises — callers must handle None explicitly.
    """
    return call_llm(user, system, repo_root=repo_root)


def generate(
    plan: dict[str, Any],
    context: dict[str, str],
    repo_root: Optional[Path] = None,
    previous_code: Optional[str] = None,
    previous_error: Optional[str] = None,
    goal: Optional[str] = None,
) -> str:
    """
    Call unified LLM chain to generate Grasshopper/RhinoCommon code. Returns code string.
    Raises RuntimeError if all LLM backends are unavailable or no valid code returned.
    """
    system = _build_system_prompt(context, plan, repo_root=repo_root, goal=goal)
    user = _build_user_prompt(plan, previous_code, previous_error)
    text = _call_unified_llm(system, user, repo_root)
    if text is None:
        raise RuntimeError(
            "All LLM backends unavailable (Anthropic / Gemini / Ollama). "
            "Set ANTHROPIC_API_KEY or GOOGLE_API_KEY in .env, or start Ollama."
        )
    code = _extract_code(text)
    if not code:
        raise RuntimeError("LLM did not return valid RhinoCommon code. No code block or Rhino import found.")
    return code


def generate_rhino_python(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> str:
    """
    Generate a standalone RhinoCommon Python script via LLM for an arbitrary part.

    Injects STEP_PATH and STL_PATH as constants at the top of the user prompt so
    the generated script can export without placeholder substitution.

    Returns code string. Raises RuntimeError if all LLM backends unavailable or
    no valid code block returned.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    context = load_context(repo_root)
    system  = _build_system_prompt(context, plan, repo_root=repo_root, goal=goal)

    # Extend user prompt with concrete export paths so the script is self-contained
    # Use forward slashes — Rhino accepts them and they avoid escape issues
    base_user = _build_user_prompt(plan)
    sp = step_path.replace("\\", "/")
    st = stl_path.replace("\\", "/")
    path_block = (
        f'\nExport constants (use exactly these in your script):\n'
        f'  STEP_PATH = "{sp}"\n'
        f'  STL_PATH  = "{st}"\n'
    )
    user = base_user + path_block

    text = _call_unified_llm(system, user, repo_root)
    if text is None:
        raise RuntimeError(
            f"All LLM backends unavailable — cannot generate RhinoCommon code for "
            f"'{plan.get('part_id', goal)}'. Set ANTHROPIC_API_KEY or GOOGLE_API_KEY "
            f"in .env, or start Ollama."
        )
    code = _extract_code(text)
    if not code:
        raise RuntimeError(
            f"LLM did not return valid RhinoCommon code for '{plan.get('part_id', goal)}'. "
            "No code block or Rhino import found."
        )
    return code


def save_generated_code(code: str, part_name: str, repo_root: Optional[Path] = None) -> Path:
    """Save generated code to outputs/cad/generated_code/YYYY-MM-DD_HH-MM_partname.py"""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    from datetime import datetime
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H-%M")
    safe_name = re.sub(r"[^\w\-]", "_", part_name)[:50]
    dir_path = repo_root / "outputs" / "cad" / "generated_code"
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / f"{stamp}_{safe_name}.py"
    path.write_text(code, encoding="utf-8")
    return path
