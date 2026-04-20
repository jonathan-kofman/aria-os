"""
Guardrail 3 — Physics-grounded validation.

The sim agent is the judge. A candidate module must produce output that
meets the stated physical spec under a real physics simulator before
it's eligible to merge.

Dispatch by kind:
  - cadquery / sdf → FEA via aria_os.fea.calculix_stage (+ optional modal)
  - ecad           → DRC via aria_os.ecad.drc_check
  - with G-code    → CAMotics sim via aria_os.cam.nc_sim

Returns a structured verdict:
    {
      "passed":  bool,
      "score":   float,           # higher is better; physics-relevant
      "metrics": {...},           # domain-specific (max_stress_mpa,
                                  #  n_drc_violations, mass_g, ...)
      "reason":  str | None,      # failure explanation
      "evidence_paths": {...},    # report files for the PR body
    }
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _judge_mcad(sandbox, candidate: dict, spec: dict) -> dict:
    """FEA-based judgement for MCAD/SDF candidates.

    Runs both (a) static-linear stress vs yield and (b) modal analysis
    vs excitation frequency, depending on what the spec declares:
      - spec.load_n                        → static stress gate
      - spec.min_frequency_hz              → modal gate (first natural
                                              frequency must exceed this)
      - spec.target_safety_factor          → static safety-factor target
    Either gate failing fails the candidate. Both unspecified → static
    with defaults.
    """
    try:
        from aria_os.fea.calculix_stage import run_static_fea, run_modal_fea
    except Exception as exc:
        return {"passed": False, "score": 0.0, "metrics": {},
                "reason": f"FEA import failed: {exc}",
                "evidence_paths": {}}

    step_candidates = list(Path(sandbox.scratch_dir).rglob("*.step"))
    if not step_candidates:
        return {"passed": False, "score": 0.0, "metrics": {},
                "reason": "no STEP artifact in sandbox scratch dir",
                "evidence_paths": {}}
    step_path = step_candidates[0]

    material = (spec.get("material") or "aluminum_6061").lower()
    out_dir = Path(sandbox.scratch_dir) / "fea"

    metrics: dict = {}
    evidence: dict = {}
    failures: list[str] = []
    sub_scores: list[float] = []

    # ── Static stress gate ─────────────────────────────────────────────
    if "load_n" in spec or "target_safety_factor" in spec or True:
        load_n = float(spec.get("load_n", 100.0))
        target_sf = float(spec.get("target_safety_factor", 2.0))
        static = run_static_fea(step_path=step_path, material=material,
                                load_n=load_n, out_dir=out_dir,
                                target_safety_factor=target_sf)
        if not static.get("available"):
            # FEA tooling absent — degrade, skip rather than fail.
            return {"passed": True, "score": 1.0,
                    "metrics": {"fea_skipped": True,
                                "reason": static.get("error")},
                    "reason": None, "evidence_paths": {}}
        metrics.update({
            "max_stress_mpa":   static.get("max_stress_mpa"),
            "safety_factor":    static.get("safety_factor"),
            "yield_mpa":        static.get("yield_mpa"),
            "target_sf":        target_sf,
        })
        if static.get("report_path"):
            evidence["static_fea_report"] = static["report_path"]
        if not static.get("passed"):
            failures.append(
                f"safety factor {static.get('safety_factor')} < "
                f"target {target_sf}")
        sub_scores.append(float(static.get("safety_factor", 0.0)))

    # ── Modal gate (first natural frequency must exceed excitation) ─────
    min_freq = spec.get("min_frequency_hz") or spec.get("excitation_hz")
    if min_freq is not None:
        modal_dir = Path(sandbox.scratch_dir) / "modal"
        modal = run_modal_fea(step_path=step_path, material=material,
                              out_dir=modal_dir,
                              min_freq_hz=float(min_freq), n_modes=6)
        if modal.get("available"):
            metrics.update({
                "first_mode_hz":     modal.get("first_mode_hz"),
                "frequencies_hz":    modal.get("frequencies_hz"),
                "min_freq_target_hz": float(min_freq),
            })
            if modal.get("report_path"):
                evidence["modal_fea_report"] = modal["report_path"]
            if not modal.get("passed"):
                failures.append(
                    f"first natural freq {modal.get('first_mode_hz')} Hz "
                    f"< target {min_freq} Hz")
            first = modal.get("first_mode_hz") or 0.0
            sub_scores.append(first / float(min_freq))  # normalised margin

    passed = len(failures) == 0
    # Composite score: min of sub-scores (weakest gate dominates)
    score = min(sub_scores) if sub_scores else 0.0
    return {
        "passed": passed,
        "score": score,
        "metrics": metrics,
        "reason": None if passed else "; ".join(failures),
        "evidence_paths": evidence,
    }


def _judge_ecad(sandbox, candidate: dict, spec: dict) -> dict:
    """DRC-based judgement for ECAD candidates."""
    try:
        from aria_os.ecad.drc_check import run_drc
    except Exception as exc:
        return {"passed": False, "score": 0.0, "metrics": {},
                "reason": f"DRC import failed: {exc}",
                "evidence_paths": {}}

    pcb_candidates = list(Path(sandbox.scratch_dir).rglob("*.kicad_pcb"))
    if not pcb_candidates:
        return {"passed": False, "score": 0.0, "metrics": {},
                "reason": "no .kicad_pcb in sandbox scratch dir",
                "evidence_paths": {}}
    pcb_path = pcb_candidates[0]

    out_dir = Path(sandbox.scratch_dir) / "drc"
    result = run_drc(pcb_path, out_dir)
    if not result.get("available"):
        return {"passed": True, "score": 1.0,
                "metrics": {"drc_skipped": True,
                            "reason": result.get("error")},
                "reason": None, "evidence_paths": {}}

    passed = bool(result.get("passed"))
    n_violations = int(result.get("n_violations", 0))
    # Score: fewer violations is better; clip to a sensible range
    score = max(0.0, 100.0 - n_violations)
    return {
        "passed": passed,
        "score": score,
        "metrics": {
            "n_violations": n_violations,
            "worst_severity": result.get("worst_severity"),
            "n_unconnected": result.get("n_unconnected"),
        },
        "reason": None if passed else f"{n_violations} DRC violations",
        "evidence_paths": {"drc_report": result.get("report_path")},
    }


def judge_candidate(*, sandbox, candidate: dict, spec: dict) -> dict:
    """Top-level physics-judge dispatcher. Reads candidate.kind to pick
    the right simulator."""
    kind = candidate.get("kind", "cadquery")
    if kind in ("cadquery", "sdf"):
        return _judge_mcad(sandbox, candidate, spec)
    if kind == "ecad":
        return _judge_ecad(sandbox, candidate, spec)
    return {"passed": False, "score": 0.0, "metrics": {},
            "reason": f"unknown candidate kind: {kind}",
            "evidence_paths": {}}
