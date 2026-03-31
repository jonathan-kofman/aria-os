from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, List
from pathlib import Path

import json

from .context_loader import load_context, load_materials
from . import cem_checks


@dataclass
class Material:
    id: str
    name: str
    yield_mpa: float
    ultimate_mpa: float
    density_gcc: float
    relative_cost: float
    machinability: float  # 1-10
    processes: List[str]


@dataclass
class MaterialResult:
    material: Material
    sf: float              # safety factor from stress model
    weight_g: float        # estimated part weight
    relative_cost: float   # material.relative_cost * weight (scaled)
    machinability: float
    passes_sf: bool        # SF >= SF_TARGET (2.0)
    score: float           # weighted composite score
    rank: int
    verdict: str           # "RECOMMENDED" | "ACCEPTABLE" | "MARGINAL" | "FAIL"


@dataclass
class MaterialStudyResult:
    part_name: str
    part_criticality: str  # "safety_critical" | "structural" | "non_critical"
    sf_target: float       # always 2.0 for ARIA
    materials_tested: int
    materials_passing: int
    ranked_results: List[MaterialResult]
    recommendation: Material
    recommendation_reasoning: str
    current_material_rank: int  # where the current material sits


def classify_criticality(part_id: str) -> str:
    safety_critical = ["pawl", "ratchet", "ring", "catch", "arrest"]
    structural = ["housing", "shaft", "spool", "bearing", "retainer"]
    lid = part_id.lower()
    if any(x in lid for x in safety_critical):
        return "safety_critical"
    if any(x in lid for x in structural):
        return "structural"
    return "non_critical"


WEIGHTS = {
    "safety_critical": {
        "sf": 0.60, "weight": 0.10, "cost": 0.10, "machinability": 0.20
    },
    "structural": {
        "sf": 0.40, "weight": 0.20, "cost": 0.20, "machinability": 0.20
    },
    "non_critical": {
        "sf": 0.20, "weight": 0.30, "cost": 0.30, "machinability": 0.20
    },
}


def _find_meta_for_stub(stub: str, outputs_dir: Path) -> Path:
    meta_dir = outputs_dir / "cad" / "meta"
    stub_low = stub.lower()
    candidates: list[Path] = []
    if not meta_dir.exists():
        raise FileNotFoundError(f"No meta directory found at {meta_dir}")
    for p in meta_dir.glob("*.json"):
        if stub_low in p.stem.lower():
            candidates.append(p)
        else:
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                pname = str(meta.get("part_name", "")).lower()
                if stub_low in pname:
                    candidates.append(p)
            except Exception:
                continue
    if not candidates:
        raise FileNotFoundError(f"Could not find meta JSON matching stub {stub!r}")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _baseline_material_for_part(part_name: str) -> str:
    lid = part_name.lower()
    if any(x in lid for x in ("pawl", "lever", "trip", "blocker", "ratchet", "ring", "catch", "arrest")):
        return "4140_ht"
    if any(x in lid for x in ("shaft",)):
        return "4140_ht"
    if any(x in lid for x in ("housing", "spool", "bearing", "retainer")):
        return "6061_t6"
    return "6061_t6"


def run_material_study(part_stub: str, context: dict, outputs_dir: Path) -> MaterialStudyResult:
    # 1. Find meta JSON for part
    meta_path = _find_meta_for_stub(part_stub, outputs_dir)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    part_name = str(meta.get("part_name", meta_path.stem))

    # 2. Classify criticality
    criticality = classify_criticality(part_name)
    weights = WEIGHTS[criticality]
    sf_target = 2.0

    # 3. Load materials
    materials = load_materials(context)
    if not materials:
        raise RuntimeError("No materials loaded from aria_materials.md")

    # 4. Precompute bbox and volume
    bbox = meta.get("bbox_mm") or {}
    bx = float(bbox.get("x", 0.0))
    by = float(bbox.get("y", 0.0))
    bz = float(bbox.get("z", 0.0))
    volume_mm3 = bx * by * bz

    # 5. Evaluate each material
    results: list[MaterialResult] = []
    best_sf = 0.0
    min_weight = None
    min_cost = None

    for mat in materials:
        # Stress check: scale existing SF using new yield
        min_sf, failure_mode = cem_checks.run_static_check_with_material(part_name, meta, mat.yield_mpa, context)
        sf = float(min_sf or 0.0)
        if sf > best_sf:
            best_sf = sf

        # Weight estimate: volume -> cm^3 -> grams, with fill factor 0.6
        vol_cm3 = volume_mm3 * 1e-3
        weight_g = vol_cm3 * mat.density_gcc * 0.6

        # Relative cost scaled by kg
        weight_kg = weight_g / 1000.0
        rel_cost = mat.relative_cost * weight_kg

        if min_weight is None or weight_g < min_weight:
            min_weight = weight_g
        if min_cost is None or rel_cost < min_cost:
            min_cost = rel_cost

        results.append(
            MaterialResult(
                material=mat,
                sf=sf,
                weight_g=weight_g,
                relative_cost=rel_cost,
                machinability=mat.machinability,
                passes_sf=sf >= sf_target,
                score=0.0,
                rank=0,
                verdict="FAIL",
            )
        )

    # 6. Normalize and compute scores
    min_weight = min_weight or 1.0
    min_cost = min_cost or 1.0
    best_sf = best_sf or 1.0

    for r in results:
        sf_norm = max(0.0, min(r.sf / best_sf, 1.0))
        weight_norm = min_weight / r.weight_g if r.weight_g > 0 else 0.0
        cost_norm = min_cost / r.relative_cost if r.relative_cost > 0 else 0.0
        mach_norm = r.machinability / 10.0

        r.score = (
            weights["sf"] * sf_norm +
            weights["weight"] * weight_norm +
            weights["cost"] * cost_norm +
            weights["machinability"] * mach_norm
        )

    # 7. Rank and set verdicts
    results.sort(key=lambda r: r.score, reverse=True)
    best_score = results[0].score if results else 0.0
    passing = 0
    for idx, r in enumerate(results, start=1):
        r.rank = idx
        if not r.passes_sf:
            r.verdict = "FAIL"
        else:
            passing += 1
            if idx == 1:
                r.verdict = "RECOMMENDED"
            elif r.score >= 0.8 * best_score:
                r.verdict = "ACCEPTABLE"
            else:
                r.verdict = "MARGINAL"

    recommendation = results[0].material
    current_id = _baseline_material_for_part(part_name)
    current_rank = next((r.rank for r in results if r.material.id == current_id), -1)

    # 8. Simple reasoning string
    top = results[0]
    reasoning = (
        f"{top.material.name} recommended: SF={top.sf:.2f}x, "
        f"weight={top.weight_g:.0f} g, relative_cost~{top.relative_cost:.2f}, "
        f"machinability={top.machinability:.1f}/10. "
        f"Baseline material '{current_id}' ranks {current_rank}."
    )

    return MaterialStudyResult(
        part_name=part_name,
        part_criticality=criticality,
        sf_target=sf_target,
        materials_tested=len(results),
        materials_passing=passing,
        ranked_results=results,
        recommendation=recommendation,
        recommendation_reasoning=reasoning,
        current_material_rank=current_rank,
    )


def run_material_study_all(context: dict, outputs_dir: Path) -> dict:
    """Run material study on all parts with meta JSONs."""
    import datetime

    meta_dir = outputs_dir / "cad" / "meta"
    if not meta_dir.exists():
        return {"error": "No meta directory found"}

    results: dict[str, MaterialStudyResult] = {}
    summary_rows: list[dict] = []

    for meta_file in sorted(meta_dir.glob("*.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        part_name = meta.get("part_name", meta_file.stem)
        try:
            study = run_material_study(meta_file.stem, context, outputs_dir)
            results[part_name] = study
            rec = study.recommendation
            current_mat = _baseline_material_for_part(part_name)
            action = "OK" if rec.id == current_mat else "CHANGE"
            summary_rows.append(
                {
                    "part": part_name[:35],
                    "criticality": study.part_criticality[:12],
                    "recommended": rec.name[:16],
                    "recommended_sf": study.ranked_results[0].sf if study.ranked_results else 0.0,
                    "current": current_mat,
                    "action": action,
                }
            )
        except Exception as e:
            summary_rows.append(
                {
                    "part": part_name[:35],
                    "criticality": "ERROR",
                    "recommended": str(e)[:16],
                    "recommended_sf": 0.0,
                    "current": "unknown",
                    "action": "ERROR",
                }
            )

    out_dir = outputs_dir / "material_studies"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d")
    out_path = out_dir / f"{ts}_material_study_all.json"

    serializable: dict[str, dict] = {}
    for k, v in results.items():
        try:
            serializable[k] = {
                "part_criticality": v.part_criticality,
                "recommendation": v.recommendation.name,
                "recommendation_sf": v.ranked_results[0].sf if v.ranked_results else 0.0,
                "current_material_rank": v.current_material_rank,
                "materials_passing": v.materials_passing,
                "reasoning": v.recommendation_reasoning,
                "ranked": [
                    {
                        "material": r.material.name,
                        "sf": round(r.sf, 2),
                        "weight_g": round(r.weight_g, 1),
                        "verdict": r.verdict,
                    }
                    for r in v.ranked_results
                ],
            }
        except Exception:
            continue

    out_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")
    return {"summary": summary_rows, "output_file": str(out_path)}

