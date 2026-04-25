"""Tolerance stack analyzer.

Walks an assembly plan, sums dimensional tolerances along each
load/fit path, and flags any that close to interference (gap < 0)
or won't assemble (gap > spec max).

Two methods:
    worst_case — every dim at its worst extreme. Conservative; what
                 traditional drafting room used to do.
    rss        — root-sum-square. Statistical (3σ assumed). What's
                 used when you can rely on process capability.

Inputs:
    plan: list of native ops, including addComponent + mate*
    spec: parsed dimensional spec (dim values + tolerances)

The plan is parsed for assembly chains:
    addComponent A → mateConcentric A.bore-B.shaft → addComponent B
    is a single "stack" (A.bore_dim ↔ B.shaft_dim).

For each stack, we look up the nominal + tolerance for each linked
dimension. Tolerance defaults to ISO 2768-m if not specified.
"""
from __future__ import annotations

import math

from .dfm import Issue


# --- ISO 2768-m default tolerances by nominal size ---------------------

_ISO_2768_M = [
    # (nominal_max_mm, ±tolerance_mm)
    (3.0,    0.10),
    (30.0,   0.20),
    (120.0,  0.30),
    (400.0,  0.50),
    (1000.0, 0.80),
]


def iso_2768_tolerance(nominal_mm: float, grade: str = "m") -> float:
    """Return the default ±tolerance for a nominal dim per ISO 2768.
    Grade 'f' = fine (×0.5), 'm' = medium (×1), 'c' = coarse (×2)."""
    factor = {"f": 0.5, "m": 1.0, "c": 2.0}.get(grade, 1.0)
    for max_n, tol in _ISO_2768_M:
        if nominal_mm <= max_n:
            return tol * factor
    return _ISO_2768_M[-1][1] * factor


# --- Stack extraction --------------------------------------------------

def _extract_stacks(plan: list[dict]) -> list[dict]:
    """Walk the plan; for each mateConcentric / mateCoincident /
    mateDistance, build a stack dict:

        {
            "kind": "concentric" | "coincident" | "distance",
            "parts": ["A.bore", "B.shaft"],
            "components": ["A", "B"],
            "fit_target_mm": 0.0 if concentric else float | None,
        }

    The dim lookups happen in `analyze_stack` once we've extracted
    every stack."""
    stacks: list[dict] = []
    for op in plan:
        kind = op.get("kind", "")
        if not kind.startswith("mate"):
            continue
        if kind == "mateGear":
            continue   # gear ratio isn't a dim stack
        params = op.get("params") or {}
        parts = params.get("parts") or []
        if len(parts) < 2:
            continue
        comps = [str(p).split(".", 1)[0] for p in parts[:2]]
        stack = {
            "kind": kind.replace("mate", "").lower(),
            "parts": list(parts[:2]),
            "components": comps,
        }
        if kind == "mateDistance":
            stack["fit_target_mm"] = float(params.get("distance", 0))
        elif kind == "mateConcentric":
            # Concentric mates implicitly require bore/shaft fit:
            # bore Ø ≥ shaft Ø + clearance band.
            stack["fit_target_mm"] = 0.0
        stacks.append(stack)
    return stacks


def _component_dim(comp_id: str, connector_hint: str,
                    spec: dict) -> tuple[float, float] | None:
    """Best-effort lookup of the (nominal, ±tolerance) for a
    component.connector. Looks in spec['component_dims'] first, then
    falls back to top-level spec keys.

    Returns None if the dim isn't in the spec — caller treats as
    "no constraint to check"."""
    cdims = (spec.get("component_dims") or {}).get(comp_id) or {}
    for k in (connector_hint, f"{connector_hint}_dia",
               f"{connector_hint}_d", f"{connector_hint}_mm"):
        if k in cdims:
            entry = cdims[k]
            if isinstance(entry, dict):
                nom = float(entry.get("nominal", entry.get("value", 0)))
                tol = float(entry.get("tol", entry.get("tolerance", 0)))
                if tol == 0:
                    tol = iso_2768_tolerance(nom)
                return (nom, tol)
            elif isinstance(entry, (int, float)):
                return (float(entry), iso_2768_tolerance(float(entry)))
    return None


# --- Analysis ---------------------------------------------------------

def analyze_stack(plan: list[dict], spec: dict,
                    *, method: str = "worst_case") -> list[Issue]:
    """Walk the plan's mate chain and emit Issues for any tolerance
    stack that fails the requested method.

    method:
        "worst_case" (default) — every tolerance at its worst extreme
        "rss"                  — root-sum-square (3σ assumed)
    """
    stacks = _extract_stacks(plan)
    if not stacks:
        return []

    issues: list[Issue] = []
    for st in stacks:
        if st["kind"] != "concentric":
            continue   # only concentric (bore/shaft fit) wired for v1

        # Look up bore + shaft dims by parsing the part_ref hints
        a_ref, b_ref = st["parts"]
        a_comp, a_conn = (a_ref.split(".", 1) + ["bore"])[:2]
        b_comp, b_conn = (b_ref.split(".", 1) + ["shaft"])[:2]
        # First part is conventionally the bore (larger), second the
        # shaft (smaller). If not, we sort by name hint.
        bore_lookup = _component_dim(a_comp, a_conn, spec)
        shaft_lookup = _component_dim(b_comp, b_conn, spec)
        if bore_lookup is None or shaft_lookup is None:
            continue

        bore_nom, bore_tol = bore_lookup
        shaft_nom, shaft_tol = shaft_lookup

        # Worst-case: bore at min, shaft at max → smallest gap
        if method == "worst_case":
            gap_min = (bore_nom - bore_tol) - (shaft_nom + shaft_tol)
            gap_max = (bore_nom + bore_tol) - (shaft_nom - shaft_tol)
        else:   # rss
            stack_tol = math.sqrt(bore_tol ** 2 + shaft_tol ** 2)
            gap_nom = bore_nom - shaft_nom
            gap_min = gap_nom - stack_tol
            gap_max = gap_nom + stack_tol

        if gap_min < 0:
            issues.append(Issue(
                "critical", "tolerance_stack_interference",
                f"{a_ref} ↔ {b_ref}: gap min = {gap_min:.3f}mm "
                f"({method}). Bore {bore_nom}±{bore_tol} interferes "
                f"with shaft {shaft_nom}±{shaft_tol}.",
                fix=f"Increase bore by ≥{abs(gap_min) + 0.05:.2f}mm "
                   "or tighten tolerance bands."))
        elif gap_max > spec.get("max_clearance_mm", 1e9):
            issues.append(Issue(
                "warning", "tolerance_stack_loose",
                f"{a_ref} ↔ {b_ref}: gap max = {gap_max:.3f}mm "
                f"exceeds max_clearance_mm.",
                fix="Tighten bore or shaft tolerance."))

    return issues


__all__ = ["analyze_stack", "iso_2768_tolerance"]
