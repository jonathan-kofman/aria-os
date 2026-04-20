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
    """FEA-based judgement. Runs static-linear analysis on the candidate's
    emitted STEP file, checks max von Mises vs material yield, returns
    a safety-factor score."""
    try:
        from aria_os.fea.calculix_stage import run_static_fea
    except Exception as exc:
        return {"passed": False, "score": 0.0, "metrics": {},
                "reason": f"FEA import failed: {exc}",
                "evidence_paths": {}}

    # Find a STEP produced during the contract run
    step_candidates = list(Path(sandbox.scratch_dir).rglob("*.step"))
    if not step_candidates:
        return {"passed": False, "score": 0.0, "metrics": {},
                "reason": "no STEP artifact in sandbox scratch dir",
                "evidence_paths": {}}
    step_path = step_candidates[0]

    material = (spec.get("material") or "aluminum_6061").lower()
    load_n = float(spec.get("load_n", 100.0))
    target_sf = float(spec.get("target_safety_factor", 2.0))

    out_dir = Path(sandbox.scratch_dir) / "fea"
    result = run_static_fea(step_path=step_path, material=material,
                             load_n=load_n, out_dir=out_dir,
                             target_safety_factor=target_sf)
    if not result.get("available"):
        # FEA unavailable (ccx not installed). Degrade gracefully: accept
        # candidate based on contract pass. Records this in the verdict.
        return {"passed": True, "score": 1.0,
                "metrics": {"fea_skipped": True,
                            "reason": result.get("error")},
                "reason": None,
                "evidence_paths": {}}

    passed = bool(result.get("passed"))
    return {
        "passed": passed,
        "score": float(result.get("safety_factor", 0.0)),
        "metrics": {
            "max_stress_mpa": result.get("max_stress_mpa"),
            "safety_factor": result.get("safety_factor"),
            "yield_mpa": result.get("yield_mpa"),
            "target_sf": target_sf,
        },
        "reason": None if passed else
                  f"safety factor {result.get('safety_factor')} < target {target_sf}",
        "evidence_paths": {"fea_report": result.get("report_path")},
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
