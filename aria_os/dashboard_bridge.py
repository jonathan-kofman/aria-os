"""
dashboard_bridge.py
Bridge between ARIA-OS outputs and the Streamlit dashboard.
Provides lightweight helpers for reading ARIA-OS artifacts without
importing the full generation/optimization pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _latest_json(dir_path: Path, glob_pat: str = "*.json") -> Path | None:
    if not dir_path.exists():
        return None
    files = list(dir_path.glob(glob_pat))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def get_parts_library() -> list[dict[str, Any]]:
    """
    Read all meta JSONs from outputs/cad/meta/.
    Returns list of part dicts with:
      name, bbox_mm, dims_mm, step_path, stl_path,
      material_study (if exists), sf_value
    """
    root = _repo_root()
    meta_dir = root / "outputs" / "cad" / "meta"
    step_dir = root / "outputs" / "cad" / "step"
    stl_dir = root / "outputs" / "cad" / "stl"

    mat_results = get_material_study_results() or {}

    parts: list[dict[str, Any]] = []
    if not meta_dir.exists():
        return parts

    for mp in sorted(meta_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = mp.stem
        step_path = step_dir / f"{name}.step"
        stl_path = stl_dir / f"{name}.stl"

        # Best-effort mapping into latest material study results (keys are part ids/stems)
        study = None
        sf_val = None
        for k, v in mat_results.items():
            if k == name or name in k or k in name:
                study = v
                sf_val = v.get("recommendation_sf")
                break

        parts.append(
            {
                "name": name,
                "bbox_mm": meta.get("bbox_mm") or {},
                "dims_mm": meta.get("dims_mm") or {},
                "step_path": str(step_path) if step_path.exists() else None,
                "stl_path": str(stl_path) if stl_path.exists() else None,
                "material_study": study,
                "sf_value": sf_val,
                "step_size_kb": round(step_path.stat().st_size / 1024, 1) if step_path.exists() else None,
            }
        )
    return parts


def get_material_study_results() -> dict[str, Any]:
    """
    Read latest material study JSON from outputs/material_studies/.
    Returns parsed results dict.
    """
    root = _repo_root()
    ms_dir = root / "outputs" / "material_studies"
    latest = _latest_json(ms_dir, "*.json")
    if not latest:
        return {}
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_cem_constants() -> dict[str, Any]:
    """
    Read outputs/cem_constants.json.
    Returns firmware-relevant constants dict.
    """
    root = _repo_root()
    p = root / "outputs" / "cem_constants.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_assembly_status() -> dict[str, Any]:
    """
    Read assembly_configs/aria_clutch_assembly.json.
    Returns: part count, optimization_notes, which STEPs exist vs missing.
    """
    root = _repo_root()
    cfg_path = root / "cad-pipeline" / "assembly_configs" / "aria_clutch_assembly.json"
    if not cfg_path.exists():
        return {}
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    parts = cfg.get("parts", [])
    exists = []
    missing = []
    for p in parts:
        sp = p.get("step_path")
        if not sp:
            continue
        abs_p = (root / sp) if not Path(sp).is_absolute() else Path(sp)
        if abs_p.exists():
            exists.append(str(sp))
        else:
            missing.append(str(sp))
    return {
        "part_count": len(parts),
        "optimization_notes": cfg.get("optimization_notes") or {},
        "steps_exist": exists,
        "steps_missing": missing,
    }


def get_manufacturing_readiness() -> dict[str, Any]:
    """
    Parse outputs/manufacturing_readiness.md.
    Returns: parts list with status, ANSI compliance, next steps.
    """
    root = _repo_root()
    p = root / "outputs" / "manufacturing_readiness.md"
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8", errors="ignore")

    # Parse the Part List markdown table
    lines = text.splitlines()
    parts: list[dict[str, str]] = []
    in_table = False
    for ln in lines:
        if ln.strip().startswith("| Part |") and "STEP File" in ln:
            in_table = True
            continue
        if in_table:
            if not ln.strip().startswith("|"):
                break
            if set(ln.strip()) <= {"|", "-", " "}:
                continue
            cols = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cols) >= 5:
                parts.append(
                    {
                        "name": cols[0],
                        "material": cols[1],
                        "step_file": cols[2],
                        "sf": cols[3],
                        "status": cols[4],
                    }
                )

    ansi = {
        "all_structural_sf_ge_2": "UNKNOWN",
        "safety_critical_sf_ge_3": "UNKNOWN",
        "proof_load_16kn": "UNKNOWN",
    }
    for ln in lines:
        s = ln.strip()
        if s.startswith("- All structural SF"):
            ansi["all_structural_sf_ge_2"] = s.split(":")[-1].strip(" *")
        if s.startswith("- Safety-critical SF"):
            ansi["safety_critical_sf_ge_3"] = s.split(":")[-1].strip(" *")
        if s.startswith("- Proof load capacity"):
            ansi["proof_load_16kn"] = s.split(":")[-1].strip(" *")

    # Next steps checklist
    next_steps = [ln.strip("- ").strip() for ln in lines if ln.strip().startswith("- [ ]")]

    return {"parts": parts, "ansi": ansi, "next_steps": next_steps}

