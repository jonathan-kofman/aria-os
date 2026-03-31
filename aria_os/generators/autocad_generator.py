"""
autocad_generator.py — AutoCAD/DXF generation backend for aria_os.

Bridges the tool_router → generate_civil_dxf() pipeline.
Called when tool_router routes a goal to "autocad".

Entry points
------------
generate_autocad(plan, step_path, stl_path, repo_root, previous_failures=None)
    Standard generator signature used by the orchestrator's run_validation_loop.
    Returns: Path to written DXF file (used in place of step_path by callers).

generate_dxf_from_goal(goal, state, discipline, output_path, params)
    Lower-level call for direct use or testing.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from aria_os.autocad.dxf_exporter import generate_civil_dxf

# ── helpers ───────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent.parent
_OUT_DXF = _REPO_ROOT / "outputs" / "cad" / "dxf"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")


def _extract_state(goal: str, params: dict) -> str:
    """Extract 2-letter state code from goal string or params."""
    if "state" in params and params["state"]:
        return params["state"].upper()
    # Look for state codes in goal: "Texas", "TX", "California", etc.
    state_names = {
        "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
        "california": "CA", "colorado": "CO", "connecticut": "CT",
        "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI",
        "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
        "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
        "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
        "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
        "montana": "MT", "nebraska": "NE", "nevada": "NV",
        "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
        "new york": "NY", "north carolina": "NC", "north dakota": "ND",
        "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
        "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
        "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
        "virginia": "VA", "washington": "WA", "west virginia": "WV",
        "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
    }
    goal_lower = goal.lower()
    # Full state names first
    for name, code in state_names.items():
        if name in goal_lower:
            return code
    # 2-letter codes in goal (word boundary)
    import re as _re
    m = _re.search(r"\b([A-Z]{2})\b", goal)
    if m and m.group(1) in state_names.values():
        return m.group(1)
    return "national"


def _extract_discipline(goal: str, params: dict) -> str | None:
    """Extract discipline from params or return None for auto-detect."""
    return params.get("discipline") or params.get("civil_discipline")


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_dxf_from_goal(
    goal: str,
    state: str = "national",
    discipline: str | None = None,
    output_path: str | Path | None = None,
    params: dict | None = None,
) -> Path:
    """
    Generate a civil engineering DXF from a natural-language goal string.

    Parameters
    ----------
    goal        : natural language description ("drainage plan for TX subdivision")
    state       : 2-letter state code or "national"
    discipline  : override discipline detection ("transportation", "drainage", etc.)
    output_path : destination path; auto-generated if None
    params      : additional parameters (lane widths, pipe sizes, etc.)
    """
    params = params or {}
    if output_path is None:
        slug = _slug(f"{state}_{goal[:40]}")
        _OUT_DXF.mkdir(parents=True, exist_ok=True)
        output_path = _OUT_DXF / f"{slug}.dxf"

    dxf_path = generate_civil_dxf(
        description=goal,
        state=state,
        discipline=discipline,
        output_path=output_path,
        drawn_by=params.get("drawn_by", ""),
        project=params.get("project", ""),
    )
    return dxf_path


def generate_autocad(
    plan: dict,
    step_path: Path,
    stl_path: Path,
    repo_root: Path,
    previous_failures: list[str] | None = None,
) -> Path:
    """
    Standard generator signature for the orchestrator validation loop.

    Uses plan["goal"] + plan["params"] to drive DXF generation.
    Returns the DXF path (callers treat it as the primary output artifact).
    """
    goal: str = plan.get("goal", "civil site plan")
    params: dict = plan.get("params", {})

    state = _extract_state(goal, params)
    discipline = _extract_discipline(goal, params)

    # Derive output path from step_path directory (keeps outputs co-located)
    out_dir = step_path.parent.parent / "dxf"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _slug(plan.get("part_id", goal[:40]))
    output_path = out_dir / f"{slug}.dxf"

    if previous_failures:
        # Log failures for diagnostics but DXF generation is deterministic
        print(f"[autocad] retrying after failures: {previous_failures}")

    dxf_path = generate_civil_dxf(
        description=goal,
        state=state,
        discipline=discipline,
        output_path=output_path,
        drawn_by=params.get("drawn_by", ""),
        project=params.get("project", ""),
    )

    print(f"[autocad] DXF written: {dxf_path}")
    print(f"[autocad] sidecar JSON: {dxf_path.with_suffix('.json')}")
    return dxf_path
