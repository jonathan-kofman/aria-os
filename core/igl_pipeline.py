"""
core/igl_pipeline.py — Opt-in IGL pipeline entry point.

This module is the ONE place the existing ARIA orchestrator needs to look
when it wants to use the new Intermediate Geometry Language pipeline. It
does not modify any existing file in aria_os/.

Existing pipeline (unchanged):
    LLM → CadQuery code → STEP/STL

Opt-in IGL pipeline:
    LLM → IGL JSON → DriverManager → backend → STEP/STL

How to enable it
----------------
Set either of these environment variables:
    ARIA_GENERATION_MODE=igl     # route through this module
    ARIA_GENERATION_MODE=cadquery # default; existing pipeline

A caller in the orchestrator can import `should_use_igl` and `run_igl` and
branch on them, with no other changes required:

    from core.igl_pipeline import should_use_igl, run_igl

    if should_use_igl():
        result = run_igl(igl_json_doc, output_dir)
        if result.success:
            return result
    # otherwise fall through to the existing pipeline unchanged

This module purposefully has no side effects at import time. Importing it
on a machine with neither FreeCAD nor Onshape credentials is fine — it
simply reports which drivers are available.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .drivers.base_driver import DriverResult
from .drivers.manager import DriverManager
from .igl_schema import IGLDocument, parse
from .igl_validator import ValidationReport, validate


def should_use_igl() -> bool:
    """
    True if the user has opted into IGL mode via environment.

    Defaults to False so existing behavior is preserved for every user who
    hasn't explicitly turned it on.
    """
    mode = os.environ.get("ARIA_GENERATION_MODE", "cadquery").strip().lower()
    return mode == "igl"


def preferred_driver() -> Optional[str]:
    """Return the driver name the user wants to use, or None."""
    name = os.environ.get("ARIA_PREFERRED_DRIVER", "").strip().lower()
    return name or None


def run_igl(
    igl: dict[str, Any] | IGLDocument,
    output_dir: str | Path,
    preferred: Optional[str] = None,
) -> DriverResult:
    """
    Execute an IGL document through the driver manager.

    Parameters
    ----------
    igl:
        Either a parsed IGLDocument or a raw dict / JSON-parsed structure.
    output_dir:
        Where STEP / STL / native / result.json files should be written.
    preferred:
        Optional driver name override. If None, ARIA_PREFERRED_DRIVER is
        consulted, then the default priority order.

    Returns a DriverResult. Never raises — errors are captured in the
    result object for the caller to decide what to do.
    """
    manager = DriverManager()
    return manager.generate_with_fallback(
        igl,
        output_dir=output_dir,
        preferred=preferred or preferred_driver(),
    )


def validate_igl(igl: Any) -> ValidationReport:
    """Convenience wrapper for core.igl_validator.validate()."""
    return validate(igl)


def driver_status() -> list[dict[str, Any]]:
    """
    Return the current availability status of every registered driver.

    Useful for the dashboard Runs/Health page:

        [{name, description, available, supported_features}, ...]
    """
    return DriverManager().get_driver_status()


def load_example(name: str) -> dict[str, Any]:
    """
    Load a named IGL example from core/igl_examples/ and return the
    parsed dict. Useful for tests and interactive exploration.
    """
    path = Path(__file__).parent / "igl_examples" / f"{name}.json"
    if not path.is_file():
        raise FileNotFoundError(f"IGL example not found: {path}")
    return json.loads(path.read_text())
