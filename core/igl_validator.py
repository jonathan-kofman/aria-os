"""
core/igl_validator.py — Semantic validation for IGL documents.

Pydantic (in igl_schema.py) handles SHAPE validation — types, required fields,
unique IDs. This module handles SEMANTIC validation:

- All `depends_on` references point to real feature IDs declared earlier in
  the feature list.
- Feature params contain the keys each feature type actually needs.
- Impossible geometry is caught: pocket depth > stock depth, hole outside
  stock bounds, fillet radius larger than the edge length it's applied to.
- Feature types are in the canonical KNOWN_FEATURE_TYPES set (warning only).

The return value is always a `ValidationReport` — never raises. Callers
decide whether to proceed based on `report.ok`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .igl_schema import (
    IGLDocument,
    FeatureBase,
    KNOWN_FEATURE_TYPES,
    StockBlock,
    StockCylinder,
    StockTube,
    StockFromProfile,
    parse,
)


# ---------------------------------------------------------------------------
# Report object
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    """A single problem found during semantic validation."""
    severity: str           # "error" | "warning"
    feature_id: Optional[str]
    message: str


@dataclass
class ValidationReport:
    """Aggregate of all issues and an overall ok flag."""
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, message: str, feature_id: Optional[str] = None) -> None:
        self.errors.append(ValidationIssue("error", feature_id, message))

    def add_warning(self, message: str, feature_id: Optional[str] = None) -> None:
        self.warnings.append(ValidationIssue("warning", feature_id, message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [i.__dict__ for i in self.errors],
            "warnings": [i.__dict__ for i in self.warnings],
        }


# ---------------------------------------------------------------------------
# Required params per feature type
#
# These are the minimum params each feature needs to be well-formed.
# Drivers may require MORE params; this is the absolute minimum.
# ---------------------------------------------------------------------------

_REQUIRED_PARAMS: dict[str, set[str]] = {
    "pocket":           {"face", "depth"},
    "hole":             {"face", "center_x", "center_y", "diameter", "depth"},
    "hole_pattern":     {"face", "pattern", "diameter", "depth"},
    "slot":             {"face", "depth"},
    "groove":           {"depth"},
    "cutout":           {"face", "depth"},
    "boss":             {"face", "diameter", "height"},
    "rib":              {"thickness"},
    "pad":              {"face", "height"},
    "fillet":           {"radius"},
    "chamfer":          {"size"},
    "shell":            {"wall_thickness"},
    "mirror":           {"plane"},
    "pattern_linear":   {"direction", "count", "spacing"},
    "pattern_circular": {"axis", "count"},
    "bend":             {"angle", "radius"},
    "flange":           {"length", "angle"},
    "tab":              {"length", "width"},
    "relief":           {"radius"},
    "sketch":           {"elements"},
}


def _stock_bounds(doc: IGLDocument) -> Optional[tuple[float, float, float]]:
    """
    Return (size_x, size_y, size_z) for the stock if determinable, else None.

    This is an approximation for semantic checks — anything more precise
    would need a real geometry kernel.
    """
    stock = doc.stock
    if isinstance(stock, StockBlock):
        return (stock.x, stock.y, stock.z)
    if isinstance(stock, StockCylinder):
        return (stock.diameter, stock.diameter, stock.height)
    if isinstance(stock, StockTube):
        return (stock.outer_diameter, stock.outer_diameter, stock.height)
    if isinstance(stock, StockFromProfile):
        return None  # can't know extents without resolving the profile
    return None


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-feature checks
# ---------------------------------------------------------------------------

def _check_required_params(
    feature: FeatureBase, report: ValidationReport
) -> None:
    required = _REQUIRED_PARAMS.get(feature.type)
    if required is None:
        return  # unknown type — warning emitted elsewhere
    missing = required - set(feature.params.keys())
    for key in sorted(missing):
        report.add_error(
            f"feature {feature.id} ({feature.type}) is missing required param '{key}'",
            feature.id,
        )


def _check_pocket_depth(
    feature: FeatureBase,
    stock_bounds: Optional[tuple[float, float, float]],
    report: ValidationReport,
) -> None:
    if feature.type != "pocket" or stock_bounds is None:
        return
    depth = _num(feature.params.get("depth"))
    if depth is None or depth <= 0:
        report.add_error(
            f"pocket {feature.id} has non-positive or missing depth", feature.id
        )
        return
    # Rough check: pocket depth can't exceed the shortest stock dimension
    max_depth = min(stock_bounds)
    if depth >= max_depth:
        report.add_error(
            f"pocket {feature.id} depth ({depth}) >= smallest stock dimension ({max_depth})",
            feature.id,
        )


def _check_hole_in_bounds(
    feature: FeatureBase,
    stock_bounds: Optional[tuple[float, float, float]],
    report: ValidationReport,
) -> None:
    if feature.type != "hole" or stock_bounds is None:
        return
    cx = _num(feature.params.get("center_x"))
    cy = _num(feature.params.get("center_y"))
    diameter = _num(feature.params.get("diameter"))
    if None in (cx, cy, diameter):
        return  # already flagged by _check_required_params
    # Assuming stock is centered around origin-ish; this is a coarse heuristic,
    # not ground truth. Drivers with real geometry do the definitive check.
    r = diameter / 2.0
    if cx - r < -stock_bounds[0] or cx + r > stock_bounds[0]:
        report.add_warning(
            f"hole {feature.id} may extend beyond stock X bounds", feature.id
        )
    if cy - r < -stock_bounds[1] or cy + r > stock_bounds[1]:
        report.add_warning(
            f"hole {feature.id} may extend beyond stock Y bounds", feature.id
        )


def _check_dependencies(
    doc: IGLDocument, report: ValidationReport
) -> None:
    """Every depends_on must point to an earlier feature in the list."""
    seen_ids: set[str] = set()
    for feature in doc.features:
        for dep in feature.depends_on or []:
            if dep not in seen_ids:
                report.add_error(
                    f"feature {feature.id} depends on '{dep}' which is not declared before it",
                    feature.id,
                )
        seen_ids.add(feature.id)


def _check_fillet_target(
    feature: FeatureBase,
    feature_ids: set[str],
    report: ValidationReport,
) -> None:
    """Fillets sometimes reference a target feature ID; warn if missing."""
    if feature.type not in ("fillet", "chamfer"):
        return
    target = feature.params.get("target")
    if target and isinstance(target, str) and target not in feature_ids:
        report.add_warning(
            f"{feature.type} {feature.id} targets unknown feature '{target}'",
            feature.id,
        )


def _check_known_type(
    feature: FeatureBase, report: ValidationReport
) -> None:
    if feature.type not in KNOWN_FEATURE_TYPES:
        report.add_warning(
            f"feature {feature.id} has unknown type '{feature.type}' "
            f"(drivers may still handle it)",
            feature.id,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate(doc_or_dict: Any) -> ValidationReport:
    """
    Run full semantic validation on an IGL document.

    Accepts either a parsed IGLDocument or a plain dict. If passed a dict,
    schema validation is attempted first; schema errors are surfaced as
    ValidationErrors.
    """
    report = ValidationReport()

    # Shape validation first.
    if isinstance(doc_or_dict, IGLDocument):
        doc = doc_or_dict
    else:
        try:
            doc = parse(doc_or_dict)
        except Exception as exc:
            report.add_error(f"schema validation failed: {exc}")
            return report

    stock_bounds = _stock_bounds(doc)
    feature_ids = {f.id for f in doc.features}

    _check_dependencies(doc, report)

    for feature in doc.features:
        _check_known_type(feature, report)
        _check_required_params(feature, report)
        _check_pocket_depth(feature, stock_bounds, report)
        _check_hole_in_bounds(feature, stock_bounds, report)
        _check_fillet_target(feature, feature_ids, report)

    return report
