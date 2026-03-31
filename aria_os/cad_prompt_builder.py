"""
Build the rich engineering prompt ARIA-OS sends to Claude (CadQuery path) and
summarize which CAD toolchain is selected (CadQuery vs Fusion vs Grasshopper vs Blender).

Users can type a short goal; this module expands it into a structured brief + routing rationale.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .cem_context import load_cem_geometry, format_cem_block
from .tool_router import select_cad_tool


def cad_tool_rationale(cad_tool: str, plan: dict[str, Any]) -> str:
    """Human-readable reason for the selected pipeline."""
    t = (cad_tool or "cadquery").lower()
    part_id = plan.get("part_id", "") or ""
    rr = plan.get("route_reason") or plan.get("cem_route_reason") or ""
    mapping = {
        "cadquery": (
            "Prismatic / boolean / hole features -> headless CadQuery in this repo; "
            "exports STEP + STL under outputs/cad/."
        ),
        "fusion": (
            "Lattice, volumetric infill, or CAM-heavy intent -> run the generated Fusion 360 API script "
            "under outputs/cad/fusion_scripts/ (Design Extension may be required for lattice)."
        ),
        "grasshopper": (
            "Helical / loft / sweep / freeform surface intent -> use Grasshopper + Rhino "
            "(rhino.compute) artifacts under outputs/cad/grasshopper/."
        ),
        "blender": (
            "Mesh cleanup / organic sculpt intent -> run the Blender background script "
            "under outputs/cad/blender/ (STL-focused)."
        ),
    }
    base = mapping.get(t, mapping["cadquery"])
    extras: list[str] = []
    if part_id:
        extras.append(f"part_id={part_id}")
    if rr:
        extras.append(rr)
    if extras:
        return base + " " + " | ".join(extras)
    return base


def _build_dim_hints(plan: dict[str, Any]) -> str:
    """
    Produce a concise, labeled dimension list from plan["params"] so the LLM
    knows unambiguously what each value refers to (outer box vs wall vs sub-feature).
    Always appends ALL remaining plan params (CEM-derived or user-specified) so the
    LLM never has to guess or hallucinate geometry constants.
    """
    params = plan.get("params") or {}
    _raw_base = plan.get("base_shape") or {}
    base = _raw_base if isinstance(_raw_base, dict) else {}
    lines: list[str] = []
    shown: set[str] = set()

    # Outer box
    w = base.get("width") or params.get("width_mm")
    h = base.get("height") or params.get("height_mm")
    d = base.get("depth") or params.get("depth_mm")
    if w and h and d:
        lines.append(f"  outer box: width={w}mm × height={h}mm × depth={d}mm")
    elif w and h:
        lines.append(f"  outer box: width={w}mm × height={h}mm")
    shown.update({"width_mm", "height_mm", "depth_mm"})

    # Cylindrical
    od   = params.get("od_mm") or base.get("od_mm")
    bore = params.get("bore_mm") or base.get("bore_mm")
    thk  = params.get("thickness_mm") or base.get("thickness_mm")
    if od:
        lines.append(f"  outer diameter (OD): {od}mm")
        shown.add("od_mm")
    if bore:
        lines.append(f"  bore / inner diameter: {bore}mm")
        shown.add("bore_mm")
    if thk and not (w and h and d):
        lines.append(f"  axial thickness: {thk}mm")
        shown.add("thickness_mm")

    # Wall / sub-features
    wall = params.get("wall_mm")
    if wall:
        lines.append(f"  wall thickness: {wall}mm  ← this is the wall, NOT the outer height")
        shown.add("wall_mm")

    dia = params.get("diameter_mm")
    if dia:
        lines.append(f"  sub-feature diameter (ports/holes): {dia}mm")
        shown.add("diameter_mm")

    n_teeth = params.get("n_teeth")
    if n_teeth:
        lines.append(f"  teeth count: {n_teeth}")
        shown.add("n_teeth")

    n_baffles = params.get("n_baffles")
    if n_baffles:
        lines.append(f"  baffles: {n_baffles} (serpentine internal channels — alternate gap side each baffle)")
        shown.add("n_baffles")

    n_bolts = params.get("n_bolts")
    bolt_dia = params.get("bolt_dia_mm")
    bolt_r   = params.get("bolt_circle_r_mm")
    if n_bolts:
        bolt_str = f"  bolt holes: {n_bolts}x"
        if bolt_dia:
            bolt_str += f" M{int(bolt_dia)}"
        if bolt_r:
            bolt_str += f" on Ø{bolt_r*2:.0f}mm bolt circle"
        lines.append(bolt_str)
        shown.update({"n_bolts", "bolt_dia_mm", "bolt_circle_r_mm"})

    mat = params.get("material")
    if mat:
        lines.append(f"  material: {mat}")
        shown.add("material")

    # -----------------------------------------------------------------------
    # CEM-derived and user-specified params not yet shown above.
    # These are physics-derived constants from the CEM pipeline — the LLM MUST
    # use these exact values and MUST NOT recalculate or hallucinate its own.
    # -----------------------------------------------------------------------
    # Skip purely diagnostic/thermal output keys and non-geometry metadata
    _SKIP_KEYS = {
        "part_family", "Ra", "Nu", "h_coef_W_m2K", "eta_fin", "Q_total_W",
        "part_type", "length_mm", "fin_length_mm",  # fin_length == fin_run length, shown via outer box
    }
    extra = {
        k: v for k, v in params.items()
        if k not in shown and k not in _SKIP_KEYS and v is not None
        and isinstance(v, (int, float, str))
    }
    # Also expose length_mm / fin_length_mm if distinct from width
    for lk in ("length_mm", "fin_length_mm"):
        v = params.get(lk)
        if v is not None and v != w:
            extra[lk] = v

    if extra:
        lines.append("")
        lines.append("  *** CEM-derived geometry — MANDATORY: use these as Python constants, do NOT recalculate ***")
        for k, v in sorted(extra.items()):
            unit = "mm" if k.endswith("_mm") else ("" if isinstance(v, str) else "")
            lines.append(f"  {k} = {v}{unit}")

    return "\n".join(lines) if lines else "  (no structured dims extracted — use verbatim goal above)"


def build_engineering_brief(
    goal: str,
    plan: dict[str, Any],
    context: dict[str, str],
    *,
    repo_root: Optional[Path] = None,
    cad_tool: Optional[str] = None,
) -> str:
    """
    Full structured brief used as the primary user-facing generation prompt for Claude (CadQuery),
    and printed for other CAD routes so you see what the system inferred.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    tool = cad_tool if cad_tool else select_cad_tool(goal, plan)

    # Only inject ARIA CEM physics for ARIA auto-belay parts.
    # Non-ARIA domains (rocket, nozzle, LRE, …) must not receive auto-belay ground-truth dims.
    _NON_ARIA_KEYWORDS = {
        "nozzle", "rocket", "lre", "liquid rocket", "turbopump", "injector",
        "combustion", "thrust chamber", "propellant", "oxidizer",
    }
    goal_lower = goal.lower()
    part_id_str = (plan.get("part_id") or "").lower()
    # Primary gate: only inject ARIA CEM for parts whose ID starts with "aria_"
    if part_id_str and not part_id_str.startswith("aria_"):
        _is_aria_domain = False
    elif any(kw in goal_lower or kw in part_id_str for kw in _NON_ARIA_KEYWORDS):
        _is_aria_domain = False
    else:
        _is_aria_domain = bool(part_id_str and part_id_str.startswith("aria_"))

    cem = plan.get("cem_context") if isinstance(plan.get("cem_context"), dict) else None
    if _is_aria_domain and not cem:
        cem = load_cem_geometry(
            repo_root,
            goal=goal,
            part_id=(plan.get("part_id") or "") if isinstance(plan.get("part_id"), str) else "",
        )
    cem_block = format_cem_block(cem if isinstance(cem, dict) else {}) if _is_aria_domain else ""

    part_id = plan.get("part_id", "aria_part")
    material = plan.get("material", "")
    base_shape = plan.get("base_shape", {})
    features = plan.get("features", []) or []
    build_order = plan.get("build_order", []) or []

    lines = [
        "=== ARIA CAD GENERATION REQUEST (auto-expanded from your short goal) ===",
        "",
        "## 1) Your intent (verbatim)",
        goal.strip() or "(empty)",
        "",
        "## 2) Selected CAD pipeline",
        f"Primary tool: **{tool}**",
        cad_tool_rationale(tool, plan),
        "",
        "## 3) Part identity",
        f"- part_id: {part_id}",
        f"- material (if specified in plan): {material or 'use context / defaults'}",
        "",
        "## 4) Structured plan (from planner)",
        plan.get("text", str(plan)),
        "",
        "## 5) Base shape / dimensions (structured)",
        json.dumps(base_shape, indent=2) if base_shape else "(none)",
        "",
        "## 6) Planned features",
        json.dumps(features, indent=2) if features else "(none)",
        "",
        "## 7) Build order",
        "\n".join(f"  - {s}" for s in build_order) if build_order else "(none)",
        "",
        "## 8) CEM / physics-derived context (ground truth for sizes where applicable)",
        cem_block.rstrip() if cem_block else "(ARIA structural CEM not applicable for this domain — see section 9 for physics-derived params)",
        "",
        "## 9) Instructions for the code generator",
        "Generate CadQuery Python that implements the part above. Prefer simple, robust solids:",
        "solid first, then cuts/holes; avoid fragile fillets/chamfers unless required.",
        "Honor dimensions in the plan and CEM block. End with STEP/STL export and META JSON as required.",
        "CRITICAL: All values marked '*** CEM-derived geometry ***' below are physics-computed constants.",
        "You MUST use these exact values in your code. Do NOT derive, recalculate, or hallucinate your own.",
        "",
        "### Confirmed dimension mapping (authoritative — use these exact values)",
        _build_dim_hints(plan),
    ]

    if tool != "cadquery":
        lines.extend(
            [
                "",
                "## 10) Note on this route",
                "This run also writes tool-specific automation files; a small CadQuery placeholder solid "
                "may be exported so the pipeline always has STEP/STL paths. Refine geometry in the "
                f"primary tool ({tool}) using the generated scripts.",
            ]
        )

    return "\n".join(lines)


def attach_brief_to_plan(
    goal: str,
    plan: dict[str, Any],
    context: dict[str, str],
    *,
    repo_root: Optional[Path] = None,
    cad_tool: Optional[str] = None,
) -> dict[str, Any]:
    """Mutate plan in place with engineering_brief + selected cad_tool; return plan."""
    tool = cad_tool if cad_tool else select_cad_tool(goal, plan)
    plan["cad_tool_selected"] = tool
    plan["cad_tool_rationale"] = cad_tool_rationale(tool, plan)
    plan["engineering_brief"] = build_engineering_brief(
        goal, plan, context, repo_root=repo_root, cad_tool=tool
    )
    return plan
