"""STEP and STL export, organize output files under outputs/."""
from pathlib import Path
from typing import Optional


def get_output_paths(goal_or_part_id: str, repo_root: Optional[Path] = None) -> dict[str, str]:
    """Return step_path and stl_path for a part name without writing files."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    base = repo_root / "outputs" / "cad"
    step_dir = base / "step"
    stl_dir = base / "stl"
    step_dir.mkdir(parents=True, exist_ok=True)
    stl_dir.mkdir(parents=True, exist_ok=True)
    name = goal_or_part_id if goal_or_part_id.startswith("aria_") and "_" in goal_or_part_id else _goal_to_part_name(goal_or_part_id)
    return {"step_path": str(step_dir / (name + ".step")), "stl_path": str(stl_dir / (name + ".stl"))}


def export(geometry, goal_or_part_id: str, repo_root: Optional[Path] = None) -> dict[str, str]:
    """Export geometry to outputs/cad/step and outputs/cad/stl. goal_or_part_id can be goal string or part_id (e.g. aria_cam_collar)."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    base = repo_root / "outputs" / "cad"
    step_dir = base / "step"
    stl_dir = base / "stl"
    step_dir.mkdir(parents=True, exist_ok=True)
    stl_dir.mkdir(parents=True, exist_ok=True)

    # part_id style (e.g. aria_cam_collar) or infer from goal
    name = goal_or_part_id if goal_or_part_id.startswith("aria_") and "_" in goal_or_part_id else _goal_to_part_name(goal_or_part_id)
    step_path = step_dir / (name + ".step")
    stl_path = stl_dir / (name + ".stl")

    solid = geometry.val() if hasattr(geometry, "val") else geometry
    solid.exportStep(str(step_path))
    solid.exportStl(str(stl_path))

    return {"step_path": str(step_path), "stl_path": str(stl_path)}


def get_meta_path(goal_or_part_id: str, repo_root: Optional[Path] = None) -> str:
    """Return path to meta JSON for a part (does not create or check file)."""
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    base = repo_root / "outputs" / "cad"
    meta_dir = base / "meta"
    name = goal_or_part_id if goal_or_part_id.startswith("aria_") and "_" in goal_or_part_id else _goal_to_part_name(goal_or_part_id)
    return str(meta_dir / (name + ".json"))

def _goal_to_part_name(goal: str) -> str:
    import re
    g = (goal or "").strip().lower()
    if "housing" in g or "shell" in g:
        return "aria_housing"
    if "spool" in g:
        return "aria_spool"
    if "cam collar" in g or "cam_collar" in g:
        return "aria_cam_collar"
    if "rope guide" in g or "rope_guide" in g:
        return "aria_rope_guide"
    if "motor mount" in g or "motor_mount" in g:
        return "aria_motor_mount"
    # LLM-generated: strip "generate" / "generate the" prefix, first 4-5 meaningful words, slug, prepend llm_
    g = re.sub(r"^generate\s+(?:the\s+)?", "", g)
    words = re.sub(r"[^\w\s]", " ", g).split()
    stop = {"the", "a", "an", "for", "with", "mm", "diameter", "long", "wide", "thick", "from", "has", "all", "centered"}
    words = [w for w in words if len(w) > 0 and w not in stop and not w.isdigit() and not w.endswith("mm")]
    name = "_".join(words[:5]) if words else "part"
    name = name[:40]
    return f"llm_{name}" if not name.startswith("llm_") else name
