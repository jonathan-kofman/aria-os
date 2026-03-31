"""DFM (Design for Manufacturability) Agent — LLM-driven manufacturability analysis."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base_agent import BaseAgent, _call_ollama
from .ollama_config import AGENT_MODELS
from .design_state import DesignState
from .dfm_tools import (
    analyze_step_geometry,
    estimate_wall_thickness,
    check_undercuts,
    classify_machining_axes,
    estimate_feature_complexity,
)


# ---------------------------------------------------------------------------
# DFM system prompt for LLM reasoning
# ---------------------------------------------------------------------------

_DFM_SYSTEM_PROMPT = """You are a manufacturing engineer specializing in Design for Manufacturability (DFM) analysis.

Given geometry metrics and deterministic check results for a mechanical part, provide:
1. The optimal manufacturing process recommendation
2. Additional manufacturing issues the deterministic checks may have missed
3. Cost drivers that affect production cost
4. Specific design suggestions to improve manufacturability

You MUST respond with ONLY valid JSON in this exact format (no markdown, no explanation):
{
    "process_recommendation": "<one of: cnc_3axis, cnc_5axis, fdm, sla, sheet_metal, injection_mold, casting, turning>",
    "reasoning": "<1-2 sentences explaining your process choice>",
    "additional_issues": [
        {"severity": "critical|warning|info", "category": "<category>", "description": "<what>", "suggestion": "<fix>"}
    ],
    "cost_drivers": ["<driver1>", "<driver2>"],
    "design_suggestions": ["<suggestion1>", "<suggestion2>"]
}

Manufacturing process selection guidelines:
- cnc_3axis: Simple prismatic parts, low undercut count, moderate tolerance
- cnc_5axis: Complex surfaces, multiple undercuts, tight tolerance
- fdm: Prototypes, low-stress parts, complex internal geometry, large parts
- sla: High-detail prototypes, small parts with fine features
- sheet_metal: Thin flat parts (thickness < 6mm), uniform wall, simple bends
- injection_mold: High volume (>1000 units), uniform wall thickness, draft angles needed
- casting: Large complex shapes, moderate tolerance, high volume
- turning: Axially symmetric parts (aspect_ratio context: cylindrical)

Consider:
- Material removal percentage (volume vs bounding box volume) — high removal = expensive CNC
- Feature count vs complexity — many features may need 5-axis or multiple setups
- Wall thickness uniformity — critical for injection molding and casting
- Part size vs tolerance — large parts with tight tolerances are expensive
"""


def _run_deterministic_checks(
    geometry: dict[str, Any],
    undercut_info: dict[str, Any],
    wall_thickness_mm: float,
) -> list[dict[str, Any]]:
    """Run fast deterministic DFM checks. Returns list of issue dicts."""
    issues: list[dict[str, Any]] = []
    bbox = geometry.get("bbox_mm", [0, 0, 0])

    # ── Wall thickness ──────────────────────────────────────────────────
    if wall_thickness_mm > 0:
        if wall_thickness_mm < 0.4:
            issues.append({
                "severity": "critical",
                "category": "wall_thickness",
                "description": f"Estimated wall thickness {wall_thickness_mm:.2f}mm is below 0.4mm minimum (3D print limit)",
                "suggestion": "Increase wall thickness to at least 0.8mm for CNC or 0.4mm for SLA",
            })
        elif wall_thickness_mm < 0.8:
            issues.append({
                "severity": "warning",
                "category": "wall_thickness",
                "description": f"Estimated wall thickness {wall_thickness_mm:.2f}mm is below 0.8mm CNC minimum",
                "suggestion": "Consider SLA/SLS for thin walls, or increase to 0.8mm+ for CNC",
            })

    # ── Aspect ratio ────────────────────────────────────────────────────
    if bbox and all(d > 0 for d in bbox):
        max_dim = max(bbox)
        min_dim = min(bbox)
        aspect_ratio = max_dim / min_dim if min_dim > 0.01 else 999

        if aspect_ratio > 15:
            issues.append({
                "severity": "critical",
                "category": "aspect_ratio",
                "description": f"Aspect ratio {aspect_ratio:.1f}:1 exceeds 15:1 — chatter risk (CNC) or warping (3D print)",
                "suggestion": "Add ribs or supports, or split into subassemblies",
            })
        elif aspect_ratio > 10:
            issues.append({
                "severity": "warning",
                "category": "aspect_ratio",
                "description": f"Aspect ratio {aspect_ratio:.1f}:1 is high — may cause vibration in CNC or warping in FDM",
                "suggestion": "Consider work-holding strategy carefully; add stiffening ribs",
            })

    # ── Undercuts ───────────────────────────────────────────────────────
    uc_count = undercut_info.get("undercut_count", 0)
    axis_class = undercut_info.get("axis_classification", "3axis")
    if uc_count > 0:
        severity = "warning" if axis_class == "4axis" else ("critical" if axis_class == "5axis" else "info")
        issues.append({
            "severity": severity,
            "category": "undercuts",
            "description": f"{uc_count} undercut face(s) detected — requires {axis_class} machining",
            "suggestion": "Redesign to eliminate undercuts, or plan for multi-axis setup / EDM",
        })

    # ── Thin features (any bbox dimension < 1mm) ───────────────────────
    if bbox:
        for i, dim in enumerate(bbox):
            if 0 < dim < 1.0:
                axis = ["X", "Y", "Z"][i]
                issues.append({
                    "severity": "warning",
                    "category": "thin_feature",
                    "description": f"Bounding box {axis}-dimension is {dim:.2f}mm — very thin feature",
                    "suggestion": "Verify this is intentional; thin features are fragile and hard to machine",
                })

    # ── Deep pockets heuristic ──────────────────────────────────────────
    # Use volume vs bbox volume to estimate material removal
    if bbox and all(d > 0 for d in bbox):
        bbox_vol = bbox[0] * bbox[1] * bbox[2]
        actual_vol = geometry.get("volume_mm3", 0)
        if bbox_vol > 0 and actual_vol > 0:
            fill_ratio = actual_vol / bbox_vol
            removal_pct = (1 - fill_ratio) * 100

            if removal_pct > 80:
                issues.append({
                    "severity": "warning",
                    "category": "material_removal",
                    "description": f"~{removal_pct:.0f}% material removal — high machining cost",
                    "suggestion": "Consider near-net-shape process (casting, forging) or additive manufacturing",
                })

            # Deep pocket proxy: if part is tall and mostly hollow
            sorted_dims = sorted(bbox)
            if len(sorted_dims) == 3 and sorted_dims[0] > 0:
                depth_width_ratio = sorted_dims[2] / sorted_dims[0]
                if depth_width_ratio > 4 and fill_ratio < 0.5:
                    issues.append({
                        "severity": "warning",
                        "category": "deep_pocket",
                        "description": f"Depth/width ratio {depth_width_ratio:.1f}:1 with low fill — potential deep pocket",
                        "suggestion": "Use long-reach tooling or consider EDM for deep features",
                    })

    # ── Tight internal radii heuristic ──────────────────────────────────
    # Approximate: high face count relative to volume suggests small features
    face_count = geometry.get("face_count", 0)
    vol = geometry.get("volume_mm3", 0)
    if face_count > 0 and vol > 0:
        # Faces per cm3 — high density suggests tight features
        faces_per_cm3 = face_count / (vol / 1000.0)
        if faces_per_cm3 > 5.0:
            issues.append({
                "severity": "info",
                "category": "tight_radii",
                "description": f"High face density ({faces_per_cm3:.1f} faces/cm3) — may indicate tight internal radii",
                "suggestion": "Verify minimum internal corner radius is >= 1mm for standard endmills",
            })

    return issues


def _compute_score(issues: list[dict[str, Any]]) -> float:
    """Compute DFM score 0-100 from issue list. Higher = more manufacturable."""
    score = 100.0
    for issue in issues:
        sev = issue.get("severity", "info")
        if sev == "critical":
            score -= 25
        elif sev == "warning":
            score -= 10
        elif sev == "info":
            score -= 2
    return max(0.0, min(100.0, score))


def _build_llm_prompt(
    geometry: dict[str, Any],
    wall_mm: float,
    undercut_info: dict[str, Any],
    deterministic_issues: list[dict[str, Any]],
    goal: str = "",
) -> str:
    """Build the prompt for LLM DFM reasoning."""
    bbox = geometry.get("bbox_mm", [0, 0, 0])
    max_dim = max(bbox) if bbox else 0
    min_dim = min(d for d in bbox if d > 0) if bbox and any(d > 0 for d in bbox) else 1
    aspect = max_dim / min_dim if min_dim > 0.01 else 0
    bbox_vol = bbox[0] * bbox[1] * bbox[2] if bbox and all(d > 0 for d in bbox) else 0
    fill = geometry.get("volume_mm3", 0) / bbox_vol * 100 if bbox_vol > 0 else 0

    det_summary = "\n".join(
        f"  - [{i['severity'].upper()}] {i['category']}: {i['description']}"
        for i in deterministic_issues
    ) or "  (no issues found)"

    return f"""Analyze this part for manufacturability:

PART: {goal or 'unknown'}

GEOMETRY:
  Bounding box: {bbox[0]:.1f} x {bbox[1]:.1f} x {bbox[2]:.1f} mm
  Volume: {geometry.get('volume_mm3', 0):.1f} mm3
  Surface area: {geometry.get('surface_area_mm2', 0):.1f} mm2
  Faces: {geometry.get('face_count', 0)}
  Edges: {geometry.get('edge_count', 0)}
  Solids: {geometry.get('solid_count', 0)}
  Estimated wall thickness: {wall_mm:.2f} mm
  Aspect ratio: {aspect:.1f}:1
  Material fill: {fill:.0f}% (volume / bbox volume)
  Undercuts: {undercut_info.get('undercut_count', 0)} faces
  Machining axes: {undercut_info.get('axis_classification', 'unknown')}

DETERMINISTIC CHECK RESULTS:
{det_summary}

Respond with JSON only."""


def run_dfm_analysis(
    step_path: str,
    goal: str = "",
    *,
    skip_llm: bool = False,
) -> dict[str, Any]:
    """Run full DFM analysis on a STEP file.

    Parameters
    ----------
    step_path : path to STEP file
    goal : original part description (for LLM context)
    skip_llm : if True, skip LLM reasoning (deterministic only)

    Returns structured DFM report dict.
    """
    step_path = str(step_path)

    # ── Step 1: Geometry extraction ─────────────────────────────────────
    geometry = analyze_step_geometry(step_path)
    if geometry.get("error"):
        return {
            "passed": False,
            "score": 0.0,
            "process_recommendation": "unknown",
            "issues": [{"severity": "critical", "category": "geometry_load",
                        "description": geometry["error"], "suggestion": "Fix STEP file"}],
            "geometry_summary": geometry,
            "cost_drivers": ["geometry_error"],
        }

    wall_mm = estimate_wall_thickness(step_path)
    undercut_info = check_undercuts(step_path)
    complexity = estimate_feature_complexity(
        geometry.get("face_count", 0), geometry.get("edge_count", 0))

    bbox = geometry.get("bbox_mm", [0, 0, 0])
    max_dim = max(bbox) if bbox else 0
    min_dim = min(d for d in bbox if d > 0) if bbox and any(d > 0 for d in bbox) else 1
    aspect_ratio = max_dim / min_dim if min_dim > 0.01 else 0

    # ── Step 2: Deterministic checks ────────────────────────────────────
    det_issues = _run_deterministic_checks(geometry, undercut_info, wall_mm)

    # ── Step 3: LLM reasoning ──────────────────────────────────────────
    llm_issues: list[dict[str, Any]] = []
    process_rec = "cnc_3axis"  # default
    cost_drivers: list[str] = []
    llm_used = False

    if not skip_llm:
        prompt = _build_llm_prompt(geometry, wall_mm, undercut_info, det_issues, goal)
        model = AGENT_MODELS.get("spec", "llama3.1:8b")

        response = _call_ollama(prompt, _DFM_SYSTEM_PROMPT, model)
        if response:
            llm_used = True
            parsed = _parse_llm_response(response)
            if parsed:
                process_rec = parsed.get("process_recommendation", process_rec)
                llm_issues = parsed.get("additional_issues", [])
                cost_drivers = parsed.get("cost_drivers", [])

    # ── Fallback process recommendation (deterministic) ─────────────────
    if not llm_used:
        process_rec = _deterministic_process_recommendation(
            geometry, wall_mm, undercut_info, aspect_ratio, complexity)
        cost_drivers = _deterministic_cost_drivers(
            geometry, undercut_info, wall_mm, complexity)

    # ── Combine all issues ──────────────────────────────────────────────
    all_issues = det_issues + llm_issues

    # ── Compute score ───────────────────────────────────────────────────
    score = _compute_score(all_issues)
    passed = score >= 50 and not any(i["severity"] == "critical" for i in all_issues)

    # ── Build geometry summary ──────────────────────────────────────────
    geometry_summary = {
        "bbox_mm": bbox,
        "volume_mm3": geometry.get("volume_mm3", 0),
        "surface_area_mm2": geometry.get("surface_area_mm2", 0),
        "face_count": geometry.get("face_count", 0),
        "estimated_wall_mm": wall_mm,
        "aspect_ratio": round(aspect_ratio, 2),
        "undercut_count": undercut_info.get("undercut_count", 0),
    }

    return {
        "passed": passed,
        "score": round(score, 1),
        "process_recommendation": process_rec,
        "issues": all_issues,
        "geometry_summary": geometry_summary,
        "cost_drivers": cost_drivers,
        "llm_used": llm_used,
        "complexity": complexity,
    }


def _parse_llm_response(response: str) -> dict[str, Any] | None:
    """Extract JSON from LLM response, tolerating markdown fences."""
    text = response.strip()

    # Strip markdown code fences
    if "```" in text:
        import re
        match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

    # Try direct JSON parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the response
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    return None


def _deterministic_process_recommendation(
    geometry: dict, wall_mm: float, undercut_info: dict,
    aspect_ratio: float, complexity: str,
) -> str:
    """Fallback process recommendation when LLM is unavailable."""
    bbox = geometry.get("bbox_mm", [0, 0, 0])
    vol = geometry.get("volume_mm3", 0)
    bbox_vol = bbox[0] * bbox[1] * bbox[2] if bbox and all(d > 0 for d in bbox) else 1
    fill_ratio = vol / bbox_vol if bbox_vol > 0 else 1
    axis_class = undercut_info.get("axis_classification", "3axis")

    # Sheet metal: thin, flat, high fill
    if wall_mm > 0 and wall_mm < 6 and fill_ratio > 0.7 and aspect_ratio > 5:
        return "sheet_metal"

    # Turning: near-cylindrical (two bbox dims similar, third different)
    if bbox and all(d > 0 for d in bbox):
        sorted_dims = sorted(bbox)
        if sorted_dims[1] > 0 and abs(sorted_dims[0] - sorted_dims[1]) / sorted_dims[1] < 0.15:
            if sorted_dims[2] / sorted_dims[1] > 1.5:
                return "turning"

    # 5-axis CNC
    if axis_class == "5axis":
        return "cnc_5axis"

    # FDM: very complex, low volume, or thin walls that CNC can't reach
    if complexity == "complex" and fill_ratio < 0.3:
        return "fdm"

    # 4-axis or moderate complexity
    if axis_class == "4axis":
        return "cnc_5axis"

    return "cnc_3axis"


def _deterministic_cost_drivers(
    geometry: dict, undercut_info: dict, wall_mm: float, complexity: str,
) -> list[str]:
    """Fallback cost driver list when LLM is unavailable."""
    drivers: list[str] = []
    bbox = geometry.get("bbox_mm", [0, 0, 0])
    vol = geometry.get("volume_mm3", 0)
    bbox_vol = bbox[0] * bbox[1] * bbox[2] if bbox and all(d > 0 for d in bbox) else 1

    if bbox_vol > 0 and vol > 0:
        removal = (1 - vol / bbox_vol) * 100
        if removal > 60:
            drivers.append(f"material_removal_{removal:.0f}pct")

    axis_class = undercut_info.get("axis_classification", "3axis")
    if axis_class != "3axis":
        drivers.append(f"{axis_class}_required")

    if complexity == "complex":
        drivers.append("high_feature_count")

    if wall_mm > 0 and wall_mm < 1.0:
        drivers.append("thin_wall_tooling")

    if bbox and max(bbox) > 300:
        drivers.append("large_part_fixturing")

    return drivers


def print_dfm_report(report: dict[str, Any]) -> None:
    """Print formatted DFM report to stdout."""
    score = report.get("score", 0)
    proc = report.get("process_recommendation", "unknown")
    issues = report.get("issues", [])
    cost_drivers = report.get("cost_drivers", [])
    geo = report.get("geometry_summary", {})

    print(f"  [DFM] Score: {score:.0f}/100 -- {proc.replace('_', ' ').upper()} recommended")

    # Print deterministic check results
    checked = set()
    for issue in issues:
        cat = issue.get("category", "")
        sev = issue.get("severity", "info")
        desc = issue.get("description", "")
        tag = "CRIT" if sev == "critical" else ("WARN" if sev == "warning" else "INFO")
        print(f"  [DFM] {tag} {cat}: {desc}")
        checked.add(cat)

    # Print OK for checks that passed
    wall_mm = geo.get("estimated_wall_mm", 0)
    aspect = geo.get("aspect_ratio", 0)
    uc = geo.get("undercut_count", 0)

    if "wall_thickness" not in checked and wall_mm > 0:
        print(f"  [DFM] OK   wall_thickness: {wall_mm:.1f}mm (min 0.8mm for CNC)")
    if "aspect_ratio" not in checked and aspect > 0:
        print(f"  [DFM] OK   aspect_ratio: {aspect:.1f}:1 (max 15:1)")
    if "undercuts" not in checked:
        print(f"  [DFM] OK   undercuts: {uc} faces")

    if cost_drivers:
        print(f"  [DFM] Cost drivers: {', '.join(cost_drivers)}")
