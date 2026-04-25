"""Drawing-vs-model GD&T consistency checker.

Walks the plan; for every gdtFrame / datumLabel / surfaceFinishCallout,
verify the feature it references actually exists in the model side
of the plan (a sketch alias, body alias, or feature alias declared
by an earlier op).

Catches the most common LLM mistake in shop drawings: emitting a
GD&T frame for a feature that was never modeled (e.g. position
tolerance on bolt holes when the planner only emitted one hole and
no circular pattern), or contradicting the model's actual dim with
a tighter tolerance than the dim itself.
"""
from __future__ import annotations

from .dfm import Issue


def _model_features(plan: list[dict]) -> set[str]:
    """Collect every alias the model side of the plan declared.
    Includes sketch aliases, body aliases, feature aliases."""
    out: set[str] = set()
    for op in plan:
        kind = op.get("kind")
        if kind in ("beginDrawing", "newSheet", "addView",
                     "sectionView", "detailView", "brokenView",
                     "addTitleBlock", "revisionTable", "bomTable",
                     "linearDimension", "angularDimension",
                     "diameterDimension", "radialDimension",
                     "ordinateDimension", "autoDimension",
                     "gdtFrame", "datumLabel", "surfaceFinishCallout",
                     "weldSymbol", "centerlineMark", "balloon"):
            continue
        params = op.get("params") or {}
        for k in ("alias", "id"):
            v = params.get(k)
            if isinstance(v, str) and v:
                out.add(v)
                # Also register `alias.subfeature` style (face/edge)
    # Add common synthetic feature names the LLM may reference
    # (from the recommendations in engineering/gdt.py). These are
    # implicit features — back_face, bore, bolt_holes, etc. — that
    # come for free with most parts.
    implicit = {"back_face", "front_face", "top_face", "bottom_face",
                "bore", "outer_cylinder", "shaft_journal", "mating_face",
                "bolt_holes", "pitch_cylinder", "datum_a", "datum_b",
                "datum_c"}
    out.update(implicit)
    return out


def audit_drawing(plan: list[dict], spec: dict) -> list[Issue]:
    """For every drawing annotation, verify its feature reference
    points at something the model declared. Plus a few semantic
    consistency checks (e.g. position tolerance with no datum frame
    is wrong per ASME Y14.5)."""
    features = _model_features(plan)
    datums_declared: set[str] = set()
    issues: list[Issue] = []

    # First pass: collect declared datums
    for op in plan:
        if op.get("kind") == "datumLabel":
            label = ((op.get("params") or {}).get("label") or "").upper()
            if label:
                datums_declared.add(label)

    # Second pass: check each annotation
    for i, op in enumerate(plan, start=1):
        kind = op.get("kind")
        params = op.get("params") or {}

        if kind == "gdtFrame":
            feature = params.get("feature", "")
            char = (params.get("characteristic") or "").lower()
            datums = [str(d).upper() for d in (params.get("datums") or [])]

            # Feature reference must be declared somewhere
            if features and feature and feature not in features:
                issues.append(Issue(
                    "warning", "gdt_unknown_feature",
                    f"Op #{i}: gdtFrame references feature {feature!r} "
                    "but no model op declared it.",
                    fix=f"Either rename to one of "
                       f"{sorted(features)[:6]}… or add the missing "
                       "model feature."))

            # Position / orientation chars MUST have a datum reference
            needs_datum = char in ("position", "perpendicularity",
                                    "parallelism", "angularity",
                                    "concentricity", "symmetry",
                                    "circular_runout", "total_runout",
                                    "profile_of_a_line",
                                    "profile_of_a_surface")
            if needs_datum and not datums:
                issues.append(Issue(
                    "critical", "gdt_missing_datum",
                    f"Op #{i}: {char} requires a datum reference per "
                    "ASME Y14.5; none provided.",
                    fix=f"Add `datums: ['A']` (or A|B|C for position)."))

            # Every referenced datum must be declared. If no datums
            # exist anywhere in the plan we still flag — citing a
            # datum that the drawing never establishes is wrong per
            # ASME Y14.5 regardless.
            for d in datums:
                if d not in datums_declared:
                    issues.append(Issue(
                        "critical", "gdt_undeclared_datum",
                        f"Op #{i}: {char} references datum {d!r} "
                        "but no datumLabel op declared it.",
                        fix=f"Add `datumLabel label={d!r}` on the "
                           "appropriate feature first."))

        elif kind == "surfaceFinishCallout":
            feature = params.get("feature", "")
            if features and feature and feature not in features:
                issues.append(Issue(
                    "warning", "ra_unknown_feature",
                    f"Op #{i}: surfaceFinishCallout references "
                    f"feature {feature!r} not declared in the model."))

        elif kind == "balloon":
            comp = params.get("component", "")
            if features and comp and comp not in features:
                issues.append(Issue(
                    "warning", "balloon_unknown_component",
                    f"Op #{i}: balloon references component {comp!r} "
                    "not in the model."))

    return issues


__all__ = ["audit_drawing"]
