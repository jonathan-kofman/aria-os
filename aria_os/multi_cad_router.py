"""
aria_os/multi_cad_router.py — Multi-backend CAD Router

Provides CADRouter.route(goal, spec=None) which:
  1. Auto-extracts spec via spec_extractor when spec=None
  2. Applies CLAUDE.md routing rules (CADQUERY_KEYWORDS, GRASSHOPPER_PART_IDS, etc.)
  3. Returns a routing dict with backend, part_id, spec, rationale

This is a higher-level wrapper around tool_router.select_cad_tool() that also
runs spec extraction, so callers get a single unified entry point.

Usage:
    from aria_os.multi_cad_router import CADRouter
    result = CADRouter.route("ARIA ratchet ring, 213mm OD, 24 teeth")
    # result = {backend, part_id, spec, rationale, dry_run_info}
"""
from __future__ import annotations

from typing import Any, Optional

from .spec_extractor import extract_spec, merge_spec_into_plan
from .planner import plan as planner_plan
from .tool_router import select_cad_tool, GRASSHOPPER_PART_IDS, FUSION_PART_IDS

# Non-prefixed aliases for GRASSHOPPER_PART_IDS — planner sometimes omits the "aria_" prefix
_GH_PART_ID_ALIASES: frozenset[str] = frozenset({
    "cam_collar", "spool", "housing", "ratchet_ring", "brake_drum", "rope_guide",
    *GRASSHOPPER_PART_IDS,
})


class CADRouter:
    """
    Stateless multi-backend CAD router.

    All methods are class methods — instantiate only if you need to
    carry per-session state (e.g. iteration history).
    """

    @classmethod
    def route(
        cls,
        goal: str,
        spec: Optional[dict[str, Any]] = None,
        *,
        dry_run: bool = False,
        repo_root=None,
    ) -> dict[str, Any]:
        """
        Route a natural-language goal to the correct CAD backend.

        Parameters
        ----------
        goal     : Natural-language part description.
        spec     : Pre-extracted spec dict. If None, auto-extracted via spec_extractor.
        dry_run  : If True, return routing decision without running any generation.
        repo_root: Optional repo root Path for context loading.

        Returns
        -------
        dict with keys:
            backend     : "cadquery" | "grasshopper" | "blender" | "fusion"
            part_id     : Canonical part identifier string
            spec        : Extracted spec dict (dimensions etc.)
            rationale   : Human-readable routing explanation
            plan        : Planner dict (omitted in dry_run)
        """
        # 1. Spec extraction
        if spec is None:
            spec = extract_spec(goal)

        # 2. Build a minimal plan for router input (lightweight, no LLM)
        try:
            from pathlib import Path as _Path
            _root = repo_root or _Path(__file__).resolve().parent.parent
            plan = planner_plan(goal, {}, repo_root=_root)
            if not isinstance(plan, dict):
                plan = {"part_id": _infer_part_id(goal, spec), "features": []}
        except Exception:
            plan = {"part_id": _infer_part_id(goal, spec), "features": []}

        # Merge spec into plan params
        if spec:
            merge_spec_into_plan(spec, plan)

        part_id = plan.get("part_id") or _infer_part_id(goal, spec)

        # 3. Select backend
        backend = select_cad_tool(goal, plan)

        # 4. Apply CLAUDE.md overrides
        goal_lower = goal.lower()
        # LRE / nozzle parts always go CadQuery headless
        if any(kw in goal_lower for kw in ["lre", "nozzle", "rocket", "turbopump", "injector", "liquid rocket"]):
            backend = "cadquery"
        # Grasshopper part IDs — check both aria_-prefixed and bare variants
        elif part_id in _GH_PART_ID_ALIASES:
            backend = "grasshopper"

        rationale = _build_rationale(goal, part_id, backend, spec)

        result: dict[str, Any] = {
            "backend":   backend,
            "part_id":   part_id,
            "spec":      spec,
            "rationale": rationale,
        }
        if not dry_run:
            result["plan"] = plan

        return result

    @classmethod
    def route_all(cls, goals: list[str], **kwargs) -> list[dict[str, Any]]:
        """Route a list of goals, returning one result per goal."""
        return [cls.route(g, **kwargs) for g in goals]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PART_ID_FROM_TYPE: dict[str, str] = {
    "ratchet_ring": "aria_ratchet_ring",
    "brake_drum":   "aria_brake_drum",
    "cam_collar":   "aria_cam_collar",
    "rope_guide":   "aria_rope_guide",
    "catch_pawl":   "aria_catch_pawl",
    "spool":        "aria_spool",
    "housing":      "aria_housing",
    "bracket":      "aria_bracket",
    "flange":       "aria_flange",
    "shaft":        "aria_shaft",
    "pulley":       "aria_pulley",
    "cam":          "aria_cam",
    "pin":          "aria_pin",
    "spacer":       "aria_spacer",
    "lre_nozzle":   "lre_nozzle",
}


def _infer_part_id(goal: str, spec: dict) -> str:
    """Infer canonical part_id from spec part_type or goal keywords."""
    pt = spec.get("part_type", "")
    if pt and pt in _PART_ID_FROM_TYPE:
        return _PART_ID_FROM_TYPE[pt]
    goal_lower = goal.lower()
    for kw, pid in _PART_ID_FROM_TYPE.items():
        if kw.replace("_", " ") in goal_lower or kw in goal_lower:
            return pid
    return "aria_part"


def _build_rationale(goal: str, part_id: str, backend: str, spec: dict) -> str:
    reasons: list[str] = []

    if backend == "grasshopper":
        reasons.append(
            f"Part '{part_id}' is a Grasshopper part (complex surface geometry). "
            "Will fall back to CadQuery automatically if Rhino Compute is unavailable."
        )
    elif backend == "fusion":
        reasons.append(
            f"Goal requires Fusion 360 (lattice/generative/sheet-metal/CAM/sim). "
            "A Fusion script will be written; a CadQuery approximation is also generated immediately."
        )
    elif backend == "blender":
        reasons.append("Goal contains mesh-repair or organic geometry keywords → Blender.")
    else:
        if any(kw in goal.lower() for kw in ["nozzle", "rocket", "lre"]):
            reasons.append("LRE/nozzle parts route to CadQuery headless per CLAUDE.md.")
        else:
            reasons.append("CadQuery headless — solid parametric geometry (template or LLM fallback).")

    if spec:
        dim_summary = ", ".join(
            f"{k}={v}"
            for k, v in spec.items()
            if k not in ("part_type", "material") and v is not None
        )
        if dim_summary:
            reasons.append(f"Extracted dims: {dim_summary}.")

    return " ".join(reasons)
