"""
core/drivers/rhino_driver.py — Rhino / Rhino Compute driver.

Wraps the existing aria_os/compute_client.py ComputeClient for use with the
IGL pipeline. Rhino Compute at http://localhost:8081 provides a headless
RhinoCommon + Grasshopper environment that handles freeform NURBS geometry
that CadQuery cannot.

This driver consumes an IGL document, assembles a JSON blob of geometry
operations, and asks Compute to execute them. Because writing real
RhinoCommon operations from Python requires rhino3dm and the Compute
protocol (which has per-operation quirks), the v1 driver covers only a
minimal subset:

  - block / cylinder / tube stock via makeBox / makeCylinder equivalents
  - simple pocket and hole as boolean difference operations
  - basic fillet / chamfer

For anything more complex, the existing Grasshopper pipeline in
aria_os/generators/grasshopper_generator.py remains the canonical path.
This driver is ADDITIVE — it does not replace the existing Rhino/GH pipeline
and only runs when the user opts into IGL mode.

Availability
------------
`is_available()` delegates to ComputeClient.is_available() which pings the
healthcheck endpoint. If Compute is down, the driver reports unavailable
and the DriverManager skips it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from ..igl_schema import (
    IGLDocument,
    StockBlock,
    StockCylinder,
    StockTube,
)
from .base_driver import (
    CADDriver,
    DriverResult,
    _coerce_float,
    _coerce_int,
    igl_units_to_mm_scale,
    save_result_sidecar,
)


_SUPPORTED_FEATURES = (
    "pocket",
    "hole",
    "cutout",
    "boss",
    "pad",
    "fillet",
    "chamfer",
)


def _try_import_compute_client():
    """
    Import the ComputeClient from the existing aria_os pipeline.

    Returns the ComputeClient class, or None if the aria_os package is
    not on sys.path. We do NOT raise on import failure — that would
    prevent the DriverManager from instantiating the driver for
    availability probing.
    """
    try:
        from aria_os.compute_client import ComputeClient

        return ComputeClient
    except Exception:  # noqa: BLE001
        return None


class RhinoDriver(CADDriver):
    """
    IGL driver that routes geometry operations through Rhino Compute.

    We build a simple operation script (list of dicts describing each
    geometry step) and push it to Compute via a small Python routine
    evaluated server-side. If the server has the `rhino3dm`-based
    evaluator, we use it; otherwise we fall back to a stub that records
    unsupported status and lets the manager pick a fallback.
    """

    name = "rhino"

    def __init__(self, compute_client: Any = None) -> None:
        self._client = compute_client  # lazy-created on first use

    def get_description(self) -> str:
        return "Rhino Compute (localhost:8081 by default)"

    def _get_client(self):
        if self._client is None:
            ComputeClient = _try_import_compute_client()
            if ComputeClient is None:
                return None
            self._client = ComputeClient()
        return self._client

    def is_available(self) -> bool:
        client = self._get_client()
        if client is None:
            return False
        try:
            return bool(client.is_available(timeout=3.0))
        except Exception:  # noqa: BLE001
            return False

    def get_supported_features(self) -> list[str]:
        return list(_SUPPORTED_FEATURES)

    # --------------------------------------------------------------------- #
    # IGL → operation list
    # --------------------------------------------------------------------- #

    def build_operations(self, doc: IGLDocument) -> tuple[list[dict[str, Any]], list[str]]:
        """
        Convert an IGL document into an ordered list of geometry operations.

        This list is purely data — the driver can ship it to Compute, save
        it to disk for inspection, or translate it into a Grasshopper
        definition depending on what's available.
        """
        scale = igl_units_to_mm_scale(str(doc.part.units))
        ops: list[dict[str, Any]] = []
        warnings: list[str] = []

        # Stock
        stock = doc.stock
        if isinstance(stock, StockBlock):
            ops.append({
                "op": "make_box",
                "x": stock.x * scale,
                "y": stock.y * scale,
                "z": stock.z * scale,
                "centered": True,
            })
        elif isinstance(stock, StockCylinder):
            ops.append({
                "op": "make_cylinder",
                "diameter": stock.diameter * scale,
                "height": stock.height * scale,
            })
        elif isinstance(stock, StockTube):
            ops.append({
                "op": "make_tube",
                "outer_diameter": stock.outer_diameter * scale,
                "inner_diameter": stock.inner_diameter * scale,
                "height": stock.height * scale,
            })
        else:
            warnings.append(f"unsupported stock type: {stock.type}")

        # Features
        for feature in doc.features:
            t = feature.type
            p = feature.params

            if t == "pocket":
                ops.append({
                    "op": "pocket",
                    "id": feature.id,
                    "face": p.get("face", "top"),
                    "profile": p.get("profile", "rectangle"),
                    "center": [
                        _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                        _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
                    ],
                    "length": _coerce_float(p.get("length", 10.0), 10.0) * scale,
                    "width": _coerce_float(p.get("width", 10.0), 10.0) * scale,
                    "diameter": _coerce_float(p.get("diameter", 10.0), 10.0) * scale,
                    "depth": _scale_depth(p.get("depth"), scale),
                    "corner_radius": _coerce_float(p.get("corner_radius", 0.0), 0.0) * scale,
                })
                continue

            if t == "hole":
                ops.append({
                    "op": "hole",
                    "id": feature.id,
                    "face": p.get("face", "top"),
                    "center": [
                        _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                        _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
                    ],
                    "diameter": _coerce_float(p.get("diameter", 5.0), 5.0) * scale,
                    "depth": _scale_depth(p.get("depth", "through"), scale),
                    "hole_type": p.get("hole_type", "plain"),
                })
                continue

            if t == "cutout":
                ops.append({
                    "op": "cutout",
                    "id": feature.id,
                    "face": p.get("face", "top"),
                    "profile": p.get("profile", "rectangle"),
                    "center": [
                        _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                        _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
                    ],
                    "length": _coerce_float(p.get("length", 10.0), 10.0) * scale,
                    "width": _coerce_float(p.get("width", 10.0), 10.0) * scale,
                    "depth": _scale_depth(p.get("depth", "through"), scale),
                })
                continue

            if t == "boss":
                ops.append({
                    "op": "boss",
                    "id": feature.id,
                    "face": p.get("face", "top"),
                    "center": [
                        _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                        _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
                    ],
                    "diameter": _coerce_float(p.get("diameter", 10.0), 10.0) * scale,
                    "height": _coerce_float(p.get("height", 5.0), 5.0) * scale,
                })
                continue

            if t == "pad":
                ops.append({
                    "op": "pad",
                    "id": feature.id,
                    "face": p.get("face", "top"),
                    "center": [
                        _coerce_float(p.get("center_x", 0.0), 0.0) * scale,
                        _coerce_float(p.get("center_y", 0.0), 0.0) * scale,
                    ],
                    "length": _coerce_float(p.get("length", 10.0), 10.0) * scale,
                    "width": _coerce_float(p.get("width", 10.0), 10.0) * scale,
                    "height": _coerce_float(p.get("height", 5.0), 5.0) * scale,
                })
                continue

            if t == "fillet":
                ops.append({
                    "op": "fillet",
                    "id": feature.id,
                    "radius": _coerce_float(p.get("radius", 1.0), 1.0) * scale,
                    "edges": p.get("edges", "all"),
                    "target": p.get("target"),
                })
                continue

            if t == "chamfer":
                ops.append({
                    "op": "chamfer",
                    "id": feature.id,
                    "size": _coerce_float(p.get("size", 1.0), 1.0) * scale,
                    "edges": p.get("edges", "all"),
                })
                continue

            warnings.append(
                f"feature {feature.id} type {t!r} not supported by rhino driver"
            )

        return ops, warnings

    # --------------------------------------------------------------------- #

    def _generate_impl(
        self,
        doc: IGLDocument,
        output_dir: Path,
    ) -> DriverResult:
        client = self._get_client()
        if client is None:
            return DriverResult.failure(
                self.name,
                "Rhino Compute client not available (aria_os.compute_client missing)",
            )
        if not self.is_available():
            return DriverResult.failure(
                self.name,
                f"Rhino Compute healthcheck failed at {getattr(client, 'url', 'unknown')}",
            )

        ops, warnings = self.build_operations(doc)
        ops_path = output_dir / "rhino_ops.json"
        ops_path.write_text(json.dumps(ops, indent=2))

        # v1: we do not yet have a server-side evaluator that accepts this
        # operation list. We record the ops for inspection and return a
        # controlled failure so the DriverManager engages the CadQuery
        # fallback. Once the server-side evaluator is available, replace
        # this with an actual client.solve_grasshopper() call.
        return DriverResult(
            success=False,
            driver=self.name,
            errors=[
                "Rhino driver v1: server-side IGL evaluator not yet wired up. "
                "Operations written to rhino_ops.json; fallback will engage."
            ],
            warnings=warnings,
            metadata={
                "ops_path": str(ops_path),
                "compute_url": getattr(client, "url", ""),
                "feature_count": len(doc.features),
            },
        )


def _scale_depth(depth: Any, scale: float) -> Any:
    """Preserve 'through' as a sentinel; scale numeric depths."""
    if isinstance(depth, str):
        return depth
    return _coerce_float(depth, 1.0) * scale
