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


@dataclass
class _LoadCase:
    """Deterministic load case auto-detected from part type and geometry."""
    load_type: str          # "cantilever" | "pressure_vessel" | "torsion" | "tooth_bending" | "tension"
    applied_stress_mpa: float
    description: str


def _classify_load_type(part_id: str, meta: dict) -> str:
    """Classify load type from part_id, part_name, and params.part_type."""
    lid = part_id.lower()
    part_type = str(meta.get("params", {}).get("part_type", "")).lower()
    combined = f"{lid} {part_type}"

    if any(kw in combined for kw in ("bracket", "plate", "flange", "mount", "lever", "arm", "beam")):
        return "cantilever"
    if any(kw in combined for kw in ("housing", "cylinder", "drum", "vessel", "tank", "shell", "enclosure", "tube", "pipe", "turbopump")):
        return "pressure_vessel"
    if any(kw in combined for kw in ("shaft", "axle", "spindle", "rod", "spool", "collar")):
        return "torsion"
    if any(kw in combined for kw in ("gear", "pinion", "sprocket", "tooth", "involute")):
        return "tooth_bending"
    return "tension"


def _auto_detect_load(part_id: str, meta: dict) -> _LoadCase:
    """
    Compute applied stress (MPa) from part type and bounding box geometry.

    Default loads by part type:
        bracket/plate    -> cantilever beam, 100 N
        housing/cylinder -> pressure vessel, 1 MPa internal
        shaft/axle       -> torsion, 10 Nm
        gear             -> tooth bending, 500 N
        generic          -> simple tension, 1000 N

    Returns _LoadCase with the computed stress so SF = yield / stress.
    """
    import math

    bbox = meta.get("bbox_mm") or {}
    bx = max(float(bbox.get("x", 50.0)), 1.0)
    by = max(float(bbox.get("y", 50.0)), 1.0)
    bz = max(float(bbox.get("z", 10.0)), 1.0)

    # Sort dimensions to get meaningful L (longest), W (mid), T (shortest)
    dims_sorted = sorted([bx, by, bz])
    t_mm = dims_sorted[0]   # thickness (shortest)
    w_mm = dims_sorted[1]   # width (middle)
    l_mm = dims_sorted[2]   # length (longest)

    load_type = _classify_load_type(part_id, meta)

    if load_type == "cantilever":
        # Cantilever beam: sigma = M*c/I, M = F*L, I = w*t^3/12, c = t/2
        # sigma = 6*F*L / (w * t^2)    [all in mm -> result in MPa if F in N]
        F_n = 100.0
        if w_mm * t_mm * t_mm == 0:
            stress = 999.0
        else:
            stress = (6.0 * F_n * l_mm) / (w_mm * t_mm * t_mm)
        return _LoadCase("cantilever", stress, f"Cantilever beam, F={F_n:.0f} N, L={l_mm:.1f} mm")

    if load_type == "pressure_vessel":
        # Thin-wall pressure vessel hoop stress: sigma = p*r/t
        # Use the larger of the two non-length dims as diameter
        p_mpa = 1.0
        # For cylinders: OD ~ max(bx,by), wall ~ t_mm is too aggressive;
        # estimate wall as 10% of OD or t_mm, whichever is smaller
        od_mm = max(bx, by)
        # Try to get explicit wall from params
        wall_mm = float(meta.get("params", {}).get("wall_mm", 0))
        if wall_mm <= 0:
            wall_mm = min(t_mm, od_mm * 0.10)
        wall_mm = max(wall_mm, 0.5)  # floor
        r_mm = (od_mm / 2.0) - wall_mm
        r_mm = max(r_mm, 1.0)
        stress = (p_mpa * r_mm) / wall_mm
        return _LoadCase("pressure_vessel", stress, f"Pressure vessel, P={p_mpa:.1f} MPa, OD={od_mm:.1f} mm, wall={wall_mm:.1f} mm")

    if load_type == "torsion":
        # Torsion of solid circular shaft: tau = T*c/J, J = pi*d^4/32, c = d/2
        # tau = 16*T / (pi * d^3)    [T in N*mm, d in mm -> MPa]
        T_nm = 10.0
        T_nmm = T_nm * 1000.0  # convert to N*mm
        d_mm = min(bx, by)  # shaft cross-section diameter
        d_mm = max(d_mm, 1.0)
        stress = (16.0 * T_nmm) / (math.pi * d_mm ** 3)
        # Convert shear to von Mises equivalent: sigma_eq = tau * sqrt(3)
        stress *= math.sqrt(3)
        return _LoadCase("torsion", stress, f"Torsion, T={T_nm:.0f} Nm, d={d_mm:.1f} mm")

    if load_type == "tooth_bending":
        # Lewis tooth bending: sigma = F / (b * m * Y)
        # Approximate: module m from pitch diameter and tooth count
        # Simpler: sigma = F * 6 / (b * t^2) treating tooth as cantilever
        F_n = 500.0
        # face width ~ bz (thickness of gear), tooth height ~ rough estimate
        face_width = bz
        # Estimate tooth height as ~2.25 * module; module ~ OD / (N_teeth + 2)
        n_teeth = float(meta.get("params", {}).get("n_teeth", 20))
        pitch_dia = max(bx, by)
        module_mm = pitch_dia / (n_teeth + 2)
        tooth_height = 2.25 * module_mm
        tooth_height = max(tooth_height, 1.0)
        face_width = max(face_width, 1.0)
        # Lewis bending at tooth root: sigma = (6 * F * tooth_height) / (face_width * (0.3 * module)^2)
        # Using simplified: sigma ~ F / (face_width * module * 0.3)
        lewis_y = 0.3  # conservative Lewis form factor
        stress = F_n / (face_width * module_mm * lewis_y)
        return _LoadCase("tooth_bending", stress, f"Tooth bending, F={F_n:.0f} N, m={module_mm:.2f} mm, b={face_width:.1f} mm")

    # Default: simple tension
    # sigma = F / A, A = w * t (cross-section of smallest face)
    F_n = 1000.0
    area_mm2 = w_mm * t_mm
    area_mm2 = max(area_mm2, 1.0)
    stress = F_n / area_mm2
    return _LoadCase("tension", stress, f"Simple tension, F={F_n:.0f} N, A={w_mm:.1f}x{t_mm:.1f} mm")


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

    # 4b. Auto-detect load case for fallback stress calculation
    auto_load = _auto_detect_load(part_name, meta)

    # 5. Evaluate each material
    results: list[MaterialResult] = []
    best_sf = 0.0
    min_weight = None
    min_cost = None

    for mat in materials:
        # Stress check: try CEM first, fall back to auto-detected load
        min_sf, failure_mode = cem_checks.run_static_check_with_material(part_name, meta, mat.yield_mpa, context)
        if min_sf is None:
            # CEM unavailable — use deterministic stress from part type + geometry
            if auto_load.applied_stress_mpa > 0:
                sf = mat.yield_mpa / auto_load.applied_stress_mpa
            else:
                sf = 999.0  # no meaningful stress
            failure_mode = auto_load.load_type
        else:
            sf = float(min_sf)
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
    load_info = f" Load case: {auto_load.description} (stress={auto_load.applied_stress_mpa:.2f} MPa)." if auto_load.applied_stress_mpa > 0 else ""
    reasoning = (
        f"{top.material.name} recommended: SF={top.sf:.2f}x, "
        f"weight={top.weight_g:.0f} g, relative_cost~{top.relative_cost:.2f}, "
        f"machinability={top.machinability:.1f}/10. "
        f"Baseline material '{current_id}' ranks {current_rank}."
        f"{load_info}"
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

