"""
core/drivers/manager.py — Driver selection and fallback orchestration.

The DriverManager is the entry point for running an IGL document through
the driver system. It:

  1. Holds a registry of available drivers (CadQuery, FreeCAD, Onshape, Rhino).
  2. Probes which drivers are actually usable on this machine.
  3. Picks the best driver for a given IGL document, honoring any user
     preference, then falling back to the highest-priority driver that can
     handle all features in the doc.
  4. Runs the driver and, if it fails, falls back to the next candidate
     in priority order until one succeeds or all drivers have been tried.

Selection priority
------------------
User preference > FreeCAD > Onshape > Rhino > CadQuery

CadQuery is always last because it ships with the project — it's the safest
fallback. The other backends are tried first when their features are
supported, under the theory that they may handle specific feature classes
(NURBS, sheet metal, complex booleans) better than CadQuery.

Fallback behavior
-----------------
If the preferred driver fails, the manager tries the next available driver
that supports all required features. If none of the fully-capable drivers
succeed, the manager tries CadQuery as a last-ditch attempt even if it
marked some features as unsupported — the CadQuery driver emits warnings
for unknown feature types rather than crashing, so partial output is still
useful.

Environment variables
---------------------
  ARIA_PREFERRED_DRIVER=<name>  # optional user preference
  ARIA_DRIVER_BLACKLIST=a,b     # optional comma-separated skip list
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from ..igl_schema import IGLDocument, parse
from .base_driver import CADDriver, DriverResult
from .cadquery_driver import CadQueryDriver
from .freecad_driver import FreeCADDriver
from .onshape_driver import OnshapeDriver
from .rhino_driver import RhinoDriver


# Priority order used when the user has no preference. First one whose
# supported-feature set contains every feature in the IGL wins. CadQuery is
# at the end because it's always available and serves as the fallback.
_DEFAULT_PRIORITY = ("freecad", "onshape", "rhino", "cadquery")


class DriverManager:
    """Select and run the best CAD backend driver for an IGL document."""

    def __init__(self, drivers: Optional[dict[str, CADDriver]] = None) -> None:
        if drivers is None:
            drivers = {
                "cadquery": CadQueryDriver(),
                "freecad": FreeCADDriver(),
                "onshape": OnshapeDriver(),
                "rhino": RhinoDriver(),
            }
        self.drivers: dict[str, CADDriver] = drivers
        self._blacklist = self._load_blacklist()

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #

    def get_available_drivers(self) -> list[str]:
        """Return names of drivers whose is_available() returns True."""
        return [
            name
            for name, driver in self.drivers.items()
            if name not in self._blacklist and driver.is_available()
        ]

    def get_driver_status(self) -> list[dict[str, Any]]:
        """
        Return a richer status list for UI display.

        Each entry has {name, description, available, supported_features}.
        """
        status: list[dict[str, Any]] = []
        for name, driver in self.drivers.items():
            blacklisted = name in self._blacklist
            status.append({
                "name": name,
                "description": driver.get_description(),
                "available": (not blacklisted) and driver.is_available(),
                "blacklisted": blacklisted,
                "supported_features": driver.get_supported_features(),
            })
        return status

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #

    def get_best_driver(
        self,
        igl: dict[str, Any] | IGLDocument,
        preferred: Optional[str] = None,
    ) -> CADDriver:
        """
        Pick the best available driver for this document.

        Precedence:
          1. Explicit `preferred` argument, if set and capable.
          2. ARIA_PREFERRED_DRIVER env var, if set and capable.
          3. Default priority order, skipping drivers that can't handle
             every feature in the doc.
          4. CadQuery as a last resort.
        """
        doc = igl if isinstance(igl, IGLDocument) else parse(igl)

        preferred = preferred or os.environ.get("ARIA_PREFERRED_DRIVER") or None
        if preferred and preferred in self.drivers and preferred not in self._blacklist:
            driver = self.drivers[preferred]
            if driver.is_available():
                validation = driver.validate_igl(doc)
                if validation["valid"]:
                    return driver

        for name in _DEFAULT_PRIORITY:
            if name in self._blacklist:
                continue
            driver = self.drivers.get(name)
            if driver is None or not driver.is_available():
                continue
            validation = driver.validate_igl(doc)
            if validation["valid"]:
                return driver

        return self.drivers["cadquery"]

    def list_candidates(
        self,
        igl: dict[str, Any] | IGLDocument,
    ) -> list[tuple[str, bool, dict[str, Any]]]:
        """
        Return [(name, available, validation_result), ...] for every
        registered driver. Useful for debugging selection decisions.
        """
        doc = igl if isinstance(igl, IGLDocument) else parse(igl)
        out: list[tuple[str, bool, dict[str, Any]]] = []
        for name in _DEFAULT_PRIORITY:
            driver = self.drivers.get(name)
            if driver is None:
                continue
            available = driver.is_available() and name not in self._blacklist
            try:
                validation = driver.validate_igl(doc)
            except Exception as exc:  # noqa: BLE001
                validation = {"valid": False, "schema_errors": [str(exc)]}
            out.append((name, available, validation))
        return out

    # ------------------------------------------------------------------ #
    # Generation with fallback
    # ------------------------------------------------------------------ #

    def generate_with_fallback(
        self,
        igl: dict[str, Any] | IGLDocument,
        output_dir: str | Path,
        preferred: Optional[str] = None,
    ) -> DriverResult:
        """
        Try the best driver first, then walk down the priority list on failure.

        The result object carries `fallback_used=True` whenever a driver
        other than the initially selected one ultimately produced the output.
        """
        doc = igl if isinstance(igl, IGLDocument) else parse(igl)
        output_dir = Path(output_dir)

        primary = self.get_best_driver(doc, preferred=preferred)
        tried: list[str] = []

        result = primary.generate(doc, str(output_dir))
        tried.append(primary.name)
        if result.success:
            result.metadata.setdefault("tried_drivers", tried)
            return result

        # Build fallback chain: remaining available drivers in priority order,
        # minus any we've already tried. CadQuery always included last.
        fallback_order = [
            name
            for name in _DEFAULT_PRIORITY
            if name != primary.name
            and name in self.drivers
            and name not in self._blacklist
        ]

        for name in fallback_order:
            driver = self.drivers[name]
            if not driver.is_available() and name != "cadquery":
                continue
            # CadQuery gets a permissive retry even if it lacks some features —
            # it will emit warnings for unsupported features but still produce
            # output for the rest.
            fallback_result = driver.generate(doc, str(output_dir))
            tried.append(name)
            if fallback_result.success:
                fallback_result.fallback_used = True
                fallback_result.metadata.setdefault("tried_drivers", tried)
                fallback_result.metadata["primary_driver"] = primary.name
                fallback_result.metadata["primary_errors"] = result.errors
                return fallback_result

        # Everything failed. Return the primary result but with the tried list.
        result.metadata.setdefault("tried_drivers", tried)
        return result

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _load_blacklist(self) -> set[str]:
        raw = os.environ.get("ARIA_DRIVER_BLACKLIST", "")
        return {name.strip() for name in raw.split(",") if name.strip()}
