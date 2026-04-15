"""
core/drivers/base_driver.py — Abstract base class for CAD backend drivers.

Every driver implements this interface. The DriverManager uses it to probe
availability, check feature coverage, and run generation with a consistent
return type.

Drivers never raise from generate() on backend failures. They catch
exceptions and return a DriverResult with success=False and the error
details captured. Only genuinely programmer-side errors (e.g. a malformed
IGL dict that Pydantic rejects before the driver runs) propagate.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from ..igl_schema import IGLDocument, parse
from ..igl_validator import ValidationReport, validate


@dataclass
class DriverResult:
    """
    Unified return shape for every driver.generate() call.

    Fields are intentionally plain so the object is JSON-serializable via
    asdict().

    Attributes:
        success:      True if STEP/STL files were produced.
        driver:       Name of the driver that produced this result.
        step_file:    Absolute path to the exported STEP file, or "".
        stl_file:     Absolute path to the exported STL file, or "".
        native_file:  Path to a backend-specific file (e.g. .FCStd), or "".
        errors:       Human-readable error strings.
        warnings:     Non-fatal issues.
        generation_time_seconds: Wall-clock time for generate() itself.
        fallback_used: Set by DriverManager when it ran a fallback driver.
        metadata:     Free-form diagnostic data (feature counts, bbox, etc).
    """
    success: bool = False
    driver: str = ""
    step_file: str = ""
    stl_file: str = ""
    native_file: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generation_time_seconds: float = 0.0
    fallback_used: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def failure(cls, driver: str, *errors: str) -> "DriverResult":
        """Build a failed result with one or more error strings."""
        return cls(success=False, driver=driver, errors=list(errors))


class CADDriver:
    """
    Abstract base class for every CAD backend driver.

    Subclasses MUST override:
        name                     — class attribute, short lowercase identifier
        is_available()           — availability probe
        _generate_impl()         — the actual generation logic
        get_supported_features() — list of IGL feature type strings

    They SHOULD override get_description() for UI display.
    """

    # Subclasses set this to a short lowercase identifier such as "cadquery",
    # "freecad", "onshape", "rhino".
    name: str = "base"

    # --------------------------------------------------------------------- #
    # Availability
    # --------------------------------------------------------------------- #

    def is_available(self) -> bool:
        """
        Return True if this backend is ready to generate geometry right now.

        Drivers should be cheap in this method — import probing, env var
        checks, simple pings. It is called on every manager selection.
        """
        raise NotImplementedError

    def get_description(self) -> str:
        """Short human-readable description for UI display."""
        return self.name

    # --------------------------------------------------------------------- #
    # Capability inspection
    # --------------------------------------------------------------------- #

    def get_supported_features(self) -> list[str]:
        """Return the list of IGL feature type tags this driver supports."""
        raise NotImplementedError

    def validate_igl(self, igl: dict[str, Any] | IGLDocument) -> dict[str, Any]:
        """
        Check whether this driver can handle all features in the IGL document.

        Returns a dict with:
            valid: bool — True if every feature type is supported
            unsupported_features: list of feature dicts this driver can't handle
            schema_errors: list of schema/semantic issues from the validator
        """
        if isinstance(igl, IGLDocument):
            doc = igl
        else:
            try:
                doc = parse(igl)
            except Exception as exc:
                return {
                    "valid": False,
                    "unsupported_features": [],
                    "schema_errors": [str(exc)],
                }

        supported = set(self.get_supported_features())
        unsupported: list[dict[str, Any]] = []
        for feature in doc.features:
            if feature.type not in supported:
                unsupported.append({"id": feature.id, "type": feature.type})

        report = validate(doc)
        return {
            "valid": not unsupported and report.ok,
            "unsupported_features": unsupported,
            "schema_errors": [i.message for i in report.errors],
            "warnings": [i.message for i in report.warnings],
        }

    # --------------------------------------------------------------------- #
    # Generation
    # --------------------------------------------------------------------- #

    def generate(
        self,
        igl: dict[str, Any] | IGLDocument,
        output_dir: str,
    ) -> DriverResult:
        """
        Run the driver's generation pipeline end-to-end.

        This method is a thin wrapper around _generate_impl() that:
          1. Parses/validates the IGL if a dict was passed.
          2. Creates output_dir if it doesn't exist.
          3. Catches any exception from the backend and wraps it in a
             DriverResult rather than propagating.
          4. Times the generation for metrics.
          5. Stamps the result with the driver name.

        Subclasses should override _generate_impl, not this.
        """
        start = time.perf_counter()

        try:
            doc = igl if isinstance(igl, IGLDocument) else parse(igl)
        except Exception as exc:
            return DriverResult.failure(
                self.name, f"IGL parse failed: {exc}"
            )

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        try:
            result = self._generate_impl(doc, out)
        except Exception as exc:  # noqa: BLE001 — drivers may raise anything
            result = DriverResult.failure(
                self.name, f"{type(exc).__name__}: {exc}"
            )

        result.driver = self.name
        result.generation_time_seconds = round(time.perf_counter() - start, 3)
        return result

    def _generate_impl(
        self,
        doc: IGLDocument,
        output_dir: Path,
    ) -> DriverResult:
        """
        Real generation work. Subclasses override this.

        Arguments:
            doc:        A parsed, schema-validated IGLDocument.
            output_dir: Existing directory into which step_file, stl_file,
                        and any native files should be written.

        Returns a DriverResult with success set appropriately. Do NOT set
        `driver` or `generation_time_seconds` — generate() does that.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers shared by driver implementations
# ---------------------------------------------------------------------------

def save_result_sidecar(result: DriverResult, output_dir: Path) -> Path:
    """
    Write a small result.json sidecar next to the generated files.

    This makes it trivial for downstream tools (MillForge bundle, test
    harnesses) to see which driver produced the files and how long it took.
    """
    sidecar = output_dir / "igl_driver_result.json"
    sidecar.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return sidecar


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def igl_units_to_mm_scale(units: str) -> float:
    """Return the scale factor to convert an IGL document's units to mm."""
    mapping = {"mm": 1.0, "inches": 25.4, "meters": 1000.0}
    return mapping.get(str(units), 1.0)
