"""ARIA verification suite — runs every generated part through a
gauntlet before the user sees it.

Catches the most common LLM-shaped failure: "looks plausible, would
fail at the shop". Wraps DFM + tolerance stack + drawing-vs-model
GD&T audit + (optional) FEA into one entry point.

Usage:
    from aria_os.verification import verify_part

    report = verify_part(
        spec={"od_mm": 100, "wall_mm": 0.8, "material": "AL 6061-T6"},
        stl_path="outputs/runs/<id>/part.stl",
        process="cnc_3axis",
        plan=plan_ops,            # optional, for drawing audit
        loads={"point_n": 200},   # optional, for FEA gate
    )
    if not report.passed:
        for issue in report.issues:
            print(f"[{issue.severity}] {issue.message}")

The report has the shape:
    {
        passed: bool,
        score:  float in [0, 1],
        issues: [Issue(severity, category, message, fix)],
        gates_run: [str],
        gates_skipped: [(str, reason)],
    }
"""
from .dfm import (
    Issue, DfmReport,
    run_dfm_rules, available_processes,
)

__all__ = [
    "Issue", "DfmReport",
    "run_dfm_rules", "available_processes",
    "verify_part",
]


def verify_part(
        spec: dict,
        stl_path: str | None = None,
        *,
        process: str = "cnc_3axis",
        plan: list[dict] | None = None,
        loads: dict | None = None,
        skip_llm: bool = True,
) -> "VerifyReport":
    """Top-level verification gate. Runs every applicable check based
    on what the caller provides and returns a unified report.

    Args:
        spec:        parsed dimensional spec dict
        stl_path:    optional path to the meshed part for geometry
                     checks (wall thickness, undercuts, bbox)
        process:     manufacturing process — picks the rule set
                     (cnc_3axis, cnc_5axis, sheet_metal, fdm, sla,
                     casting, injection_mold)
        plan:        optional ordered op list — enables drawing audit
                     by cross-referencing GD&T frames against actual
                     model features
        loads:       optional load dict for the FEA gate
                     ({point_n, pressure_mpa, fixed_face, ...})
        skip_llm:    if True, no LLM reasoning — deterministic only.
                     Default True so verification doesn't burn credits
                     on every plan."""
    from dataclasses import dataclass, field
    from .dfm import run_dfm_rules

    issues: list[Issue] = []
    gates_run: list[str] = []
    gates_skipped: list[tuple[str, str]] = []

    # Gate 1: DFM rule engine — always runs if we have a spec
    dfm_report = run_dfm_rules(spec, stl_path, process=process,
                                  skip_llm=skip_llm)
    issues.extend(dfm_report.issues)
    gates_run.append("dfm")

    # Gate 2: tolerance stack — only if the plan has assembly mates
    if plan and any(op.get("kind", "").startswith("mate") for op in plan):
        try:
            from .tolerance_stack import analyze_stack
            stack_issues = analyze_stack(plan, spec)
            issues.extend(stack_issues)
            gates_run.append("tolerance_stack")
        except Exception as exc:
            gates_skipped.append(("tolerance_stack", f"{type(exc).__name__}: {exc}"))
    else:
        gates_skipped.append(("tolerance_stack", "no mate ops in plan"))

    # Gate 3: drawing audit — only if the plan has drawing ops
    if plan and any(op.get("kind") in ("beginDrawing", "gdtFrame")
                     for op in plan):
        try:
            from .drawing_audit import audit_drawing
            audit_issues = audit_drawing(plan, spec)
            issues.extend(audit_issues)
            gates_run.append("drawing_audit")
        except Exception as exc:
            gates_skipped.append(("drawing_audit", f"{type(exc).__name__}: {exc}"))
    else:
        gates_skipped.append(("drawing_audit", "no drawing ops in plan"))

    # Gate 4: FEA — only if loads provided
    if loads:
        try:
            from .fea_gate import run_fea
            fea_issues = run_fea(spec, stl_path, loads)
            issues.extend(fea_issues)
            gates_run.append("fea")
        except Exception as exc:
            gates_skipped.append(("fea", f"{type(exc).__name__}: {exc}"))
    else:
        gates_skipped.append(("fea", "no loads provided"))

    # Compose report
    crit = sum(1 for i in issues if i.severity == "critical")
    warn = sum(1 for i in issues if i.severity == "warning")
    score = max(0.0, 1.0 - 0.4 * crit - 0.1 * warn)
    return VerifyReport(
        passed=(crit == 0),
        score=round(score, 2),
        issues=issues,
        gates_run=gates_run,
        gates_skipped=gates_skipped,
    )


from dataclasses import dataclass, field


@dataclass
class VerifyReport:
    passed: bool
    score: float
    issues: list  # list[Issue]
    gates_run: list[str]
    gates_skipped: list  # list[(str, str)]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "score":  self.score,
            "issues": [i.to_dict() for i in self.issues],
            "gates_run": self.gates_run,
            "gates_skipped": [
                {"gate": g, "reason": r} for g, r in self.gates_skipped],
        }
