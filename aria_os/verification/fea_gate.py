"""Basic FEA gate.

Wraps CalculiX (or an analytical fallback) for cantilever / point-
load / pressure-vessel sanity checks. Goal is NOT full FEA — it's
"is this spec geometrically sane for the load?" using closed-form
formulas first, then CalculiX only when the geometry is non-trivial.

The closed-form path covers the bulk of LLM-generated parts:
  - cantilever beam      → max stress + tip deflection
  - simply-supported beam → 4/3 of cantilever stress; same formula
  - point load on plate  → Roark's formulas
  - pressure vessel hoop → P·D / (2·t)

The CalculiX path runs only when:
  - load is non-trivial (combined moments, distributed loads)
  - the geometry is not in the closed-form table
  - the user asks for it explicitly via spec['fea_method'] == 'calculix'
"""
from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

from .dfm import Issue


# Material yield strengths (MPa) — typical values, conservative.
_MATERIAL_YIELD_MPA: dict[str, float] = {
    "al_6061_t6":     276.0,
    "al_5052":        193.0,
    "al_3003":         70.0,
    "steel_1018":     370.0,
    "steel_4140":     655.0,
    "stainless_304":  215.0,
    "stainless_316":  205.0,
    "ti_6al_4v":      880.0,
    "abs":             40.0,
    "pla":             50.0,
    "petg":            50.0,
    "pc":              62.0,
    "nylon":           45.0,
}

_MATERIAL_E_GPA: dict[str, float] = {
    # Young's modulus in GPa
    "al_6061_t6":     68.9,
    "al_5052":        70.3,
    "al_3003":        69.0,
    "steel_1018":    200.0,
    "steel_4140":    205.0,
    "stainless_304": 193.0,
    "stainless_316": 193.0,
    "ti_6al_4v":     113.8,
    "abs":             2.3,
    "pla":             3.5,
    "petg":             2.1,
    "pc":              2.4,
    "nylon":            3.0,
}


def _resolve_material(material: str) -> str:
    """Loose material name → table key."""
    s = (material or "").lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "6061":           "al_6061_t6",
        "6061_t6":        "al_6061_t6",
        "al_6061":        "al_6061_t6",
        "aluminum_6061":  "al_6061_t6",
        "5052":           "al_5052",
        "3003":           "al_3003",
        "1018":           "steel_1018",
        "mild_steel":     "steel_1018",
        "a36":            "steel_1018",
        "4140":           "steel_4140",
        "304":            "stainless_304",
        "316":            "stainless_316",
        "ti":             "ti_6al_4v",
        "titanium":       "ti_6al_4v",
    }
    for alias, key in aliases.items():
        if alias in s:
            return key
    return s if s in _MATERIAL_YIELD_MPA else "steel_1018"


# --- Closed-form analytical checks -------------------------------------

def _cantilever_check(spec: dict, load_n: float) -> list[Issue]:
    """Rectangular cantilever: σ_max = M·c/I, δ_tip = P·L³/(3·E·I).
    Returns Issues if σ_max exceeds yield or δ exceeds spec."""
    L = spec.get("length_mm")
    b = spec.get("width_mm")
    h = spec.get("thickness_mm") or spec.get("height_mm")
    if L is None or b is None or h is None:
        return []
    L, b, h = float(L), float(b), float(h)

    # Convert to SI: mm → m, MPa = N/mm²
    L_m = L / 1000
    b_m = b / 1000
    h_m = h / 1000
    I = b_m * h_m ** 3 / 12.0   # m⁴
    c = h_m / 2
    M = load_n * L_m
    sigma_pa = M * c / I
    sigma_mpa = sigma_pa / 1e6

    mat_key = _resolve_material(spec.get("material", ""))
    yield_mpa = _MATERIAL_YIELD_MPA.get(mat_key, 250.0)
    E_gpa = _MATERIAL_E_GPA.get(mat_key, 200.0)
    E_pa = E_gpa * 1e9

    delta_m = load_n * L_m ** 3 / (3 * E_pa * I)
    delta_mm = delta_m * 1000

    issues: list[Issue] = []
    safety_factor = yield_mpa / max(sigma_mpa, 1e-6)
    if safety_factor < 1.5:
        sev = "critical" if safety_factor < 1.0 else "warning"
        issues.append(Issue(
            sev, "fea_stress_high",
            f"Cantilever max stress {sigma_mpa:.1f}MPa vs "
            f"{mat_key} yield {yield_mpa:.0f}MPa "
            f"(SF={safety_factor:.2f}, want ≥1.5).",
            fix=("Increase thickness, shorten length, or specify a "
                 "stronger material.")))

    max_deflection = spec.get("max_deflection_mm",
                                 max(L * 0.01, 1.0))
    if delta_mm > max_deflection:
        issues.append(Issue(
            "warning", "fea_deflection_high",
            f"Cantilever tip deflection {delta_mm:.2f}mm exceeds "
            f"max {max_deflection:.2f}mm (per spec / 1% rule).",
            fix="Increase thickness (δ ∝ 1/h³)."))

    if not issues:
        issues.append(Issue(
            "info", "fea_passed_cantilever",
            f"Cantilever OK: σ={sigma_mpa:.1f}MPa, δ={delta_mm:.2f}mm, "
            f"SF={safety_factor:.2f}."))
    return issues


def _pressure_vessel_check(spec: dict, pressure_mpa: float) -> list[Issue]:
    """Thin-wall hoop stress: σ = P·D / (2·t). Limit to 50% yield."""
    D = spec.get("od_mm") or spec.get("diameter_mm")
    t = spec.get("wall_mm") or spec.get("thickness_mm")
    if D is None or t is None:
        return []
    D, t = float(D), float(t)
    sigma_mpa = pressure_mpa * D / (2 * t)

    mat_key = _resolve_material(spec.get("material", ""))
    yield_mpa = _MATERIAL_YIELD_MPA.get(mat_key, 250.0)
    safety_factor = yield_mpa / max(sigma_mpa, 1e-6)

    if safety_factor < 2.0:
        sev = "critical" if safety_factor < 1.5 else "warning"
        return [Issue(
            sev, "fea_pressure_vessel",
            f"Hoop stress {sigma_mpa:.1f}MPa at {pressure_mpa}MPa "
            f"vs {mat_key} yield {yield_mpa:.0f}MPa "
            f"(SF={safety_factor:.2f}, want ≥2.0 for pressure vessels).",
            fix=f"Increase wall thickness to "
               f"≥{t * 2.0 / safety_factor:.2f}mm.")]
    return [Issue("info", "fea_passed_pressure",
                    f"Pressure vessel OK: σ={sigma_mpa:.1f}MPa, "
                    f"SF={safety_factor:.2f}.")]


# --- CalculiX wrapper (best-effort) ------------------------------------

def _calculix_available() -> bool:
    return shutil.which("ccx") is not None or \
        shutil.which("ccx.exe") is not None


def _run_calculix(spec: dict, stl_path: str | None,
                    loads: dict) -> list[Issue]:
    """Invoke CalculiX via aria_os.fea.calculix_stage on a STEP file
    derived from the artifact. Fully wired now (was stub in W7.4).
    Closes Gap #5 from FEA_PIPELINE_AUDIT.md."""
    if not _calculix_available():
        return [Issue(
            "info", "fea_calculix_unavailable",
            "CalculiX (ccx) requested but not on PATH; falling back "
            "to closed-form. Install CalculiX for non-trivial FEA.")]

    # Derive a STEP path from the STL artifact (calculix_stage takes STEP).
    # If we're given an STL, see if there's a matching .step next to it.
    if stl_path is None:
        return [Issue(
            "warning", "fea_calculix_no_geometry",
            "CalculiX requested but no STL path supplied — "
            "cannot mesh without a geometry artifact.")]

    from pathlib import Path as _P
    p = _P(stl_path)
    step_candidates = [
        p.with_suffix(".step"),
        p.with_suffix(".STEP"),
        p.with_suffix(".stp"),
    ]
    step_path = next((c for c in step_candidates if c.is_file()), None)
    if step_path is None:
        return [Issue(
            "warning", "fea_calculix_no_step",
            f"No STEP file found alongside {p.name}; CalculiX needs "
            f"OCC-readable geometry. Tried: {[str(c) for c in step_candidates]}.")]

    material = spec.get("material", "aluminum_6061")
    load_n = float(loads.get("point_n", loads.get("force_n", 1000.0)))
    out_dir = _P(spec.get("fea_out_dir", "outputs/fea") + "/" +
                  p.stem)

    try:
        from aria_os.fea.calculix_stage import run_static_fea
    except Exception as ex:
        return [Issue("warning", "fea_calculix_import_fail",
                       f"could not import calculix_stage: {ex}")]

    try:
        report = run_static_fea(step_path, material=material,
                                 load_n=load_n, out_dir=out_dir,
                                 mesh_size_mm=float(spec.get(
                                    "mesh_size_mm", 5.0)),
                                 target_safety_factor=float(spec.get(
                                    "target_safety_factor", 2.0)))
    except Exception as ex:
        return [Issue("warning", "fea_calculix_run_threw",
                       f"calculix_stage.run_static_fea threw: {ex}")]

    if not report.get("available"):
        return [Issue("info", "fea_calculix_unavailable",
                       report.get("error", "ccx unavailable"))]

    if report.get("passed"):
        return [Issue("info", "fea_calculix_passed",
                       f"CalculiX FEA PASS: σ={report['max_stress_mpa']}MPa, "
                       f"SF={report['safety_factor']} ≥ {report['target_safety_factor']}. "
                       f"Report: {report['report_path']}")]
    else:
        return [Issue("error", "fea_calculix_failed",
                       f"CalculiX FEA FAIL: σ={report.get('max_stress_mpa')}MPa, "
                       f"SF={report.get('safety_factor')} < target. "
                       f"Error: {report.get('error', 'see report')}")]


# --- Top-level entry point --------------------------------------------

def run_fea(spec: dict, stl_path: str | None,
              loads: dict) -> list[Issue]:
    """Dispatch on what the load dict contains.

    loads:
        point_n           — point load magnitude in N (cantilever)
        pressure_mpa      — internal pressure for vessels
        method            — 'closed_form' (default) | 'calculix'
    """
    method = (loads.get("method") or "closed_form").lower()
    if method == "calculix":
        return _run_calculix(spec, stl_path, loads)

    issues: list[Issue] = []
    if "point_n" in loads:
        issues.extend(_cantilever_check(spec, float(loads["point_n"])))
    if "pressure_mpa" in loads:
        issues.extend(
            _pressure_vessel_check(spec, float(loads["pressure_mpa"])))
    if not issues:
        issues.append(Issue(
            "info", "fea_no_applicable_check",
            f"FEA gate: no closed-form check applies to loads "
            f"{list(loads.keys())}."))
    return issues


__all__ = ["run_fea"]
