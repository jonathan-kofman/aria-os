"""
core/drivers/freecad_driver.py — FreeCAD backend driver.

Uses FreeCAD's Python bindings (FreeCAD + Part + Sketcher + PartDesign) to
translate an IGL document into a FreeCAD feature tree, then exports STEP +
STL.

Availability probing
--------------------
FreeCAD can be installed in several ways:
  1. As a system-installed app (Windows / macOS / apt). The Python
     interpreter bundled with FreeCAD has `FreeCAD` importable.
  2. As a pip-installable wheel (`freecad-stubs`, `python-FreeCAD`). Less
     common but some distros ship it.
  3. Not at all. In this case the driver reports is_available() = False and
     the DriverManager skips it without ever importing anything that
     wasn't there.

Headless
--------
FreeCAD can run without the GUI:
    import FreeCAD
    doc = FreeCAD.newDocument("igl_part")
    # ... build features ...
    doc.recompute()
    Part.export([shape], "output.step")

We use plain Part booleans rather than PartDesign Body because PartDesign
depends on a functioning workbench loader that isn't always available in
headless mode. Plain Part.makeBox / makeCylinder / boolean operations are
the most portable subset of the FreeCAD API.

Supported features (v1):
    Stock:        block, cylinder, tube
    Subtractive:  pocket (rect/circular), hole, hole_pattern (rect/circular),
                  cutout
    Additive:     boss, pad
    Modifiers:    fillet, chamfer

Not yet supported:
    slot (FreeCAD has no direct slot primitive — planned as approximation)
    shell (Part.makeShell exists but needs face targeting work)
    sheet metal operations
"""
from __future__ import annotations

import importlib
import math
import os
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


# Features this driver knows how to translate.
_SUPPORTED_FEATURES = (
    "pocket",
    "hole",
    "hole_pattern",
    "cutout",
    "boss",
    "pad",
    "fillet",
    "chamfer",
)


def _try_import_freecad():
    """
    Import FreeCAD and Part modules.

    Returns (FreeCAD, Part) on success, (None, None) on failure. We don't
    want an ImportError from FreeCAD to surface at module import time —
    the driver should be instantiable even on machines without FreeCAD so
    the DriverManager can still report it as unavailable.
    """
    try:
        FreeCAD = importlib.import_module("FreeCAD")
        Part = importlib.import_module("Part")
        return FreeCAD, Part
    except Exception:  # noqa: BLE001 — FreeCAD may raise anything on import
        return None, None


class FreeCADDriver(CADDriver):
    """IGL driver that builds a FreeCAD document and exports STEP/STL."""

    name = "freecad"

    def get_description(self) -> str:
        return "FreeCAD (Part workbench, headless)"

    def is_available(self) -> bool:
        FreeCAD, Part = _try_import_freecad()
        return FreeCAD is not None and Part is not None

    def get_supported_features(self) -> list[str]:
        return list(_SUPPORTED_FEATURES)

    # --------------------------------------------------------------------- #
    # Geometry helpers — all work with `Part` shapes, not PartDesign bodies.
    # --------------------------------------------------------------------- #

    def _build_stock(self, doc: IGLDocument, Part: Any) -> Any:
        """Return a Part.Shape representing the initial stock."""
        scale = igl_units_to_mm_scale(str(doc.part.units))
        stock = doc.stock

        if isinstance(stock, StockBlock):
            # FreeCAD makeBox: corner at origin, extends into +X/+Y/+Z.
            # Match CadQuery driver's centered convention by translating.
            shape = Part.makeBox(stock.x * scale, stock.y * scale, stock.z * scale)
            # Translate so the box center is at origin.
            FreeCAD, _ = _try_import_freecad()
            shape.translate(
                FreeCAD.Vector(-stock.x * scale / 2, -stock.y * scale / 2, 0)
            )
            return shape

        if isinstance(stock, StockCylinder):
            return Part.makeCylinder(
                (stock.diameter * scale) / 2.0,
                stock.height * scale,
            )

        if isinstance(stock, StockTube):
            outer = Part.makeCylinder(
                (stock.outer_diameter * scale) / 2.0,
                stock.height * scale,
            )
            inner = Part.makeCylinder(
                (stock.inner_diameter * scale) / 2.0,
                stock.height * scale,
            )
            return outer.cut(inner)

        raise ValueError(f"unsupported stock type: {type(stock).__name__}")

    def _face_top_z(self, shape: Any) -> float:
        """Return the Z coordinate of the top of the current shape bbox."""
        bb = shape.BoundBox
        return bb.ZMax

    def _face_bottom_z(self, shape: Any) -> float:
        bb = shape.BoundBox
        return bb.ZMin

    def _apply_pocket(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        profile = str(params.get("profile", "rectangle")).lower()
        face = str(params.get("face", "top")).lower()
        cx = _coerce_float(params.get("center_x", 0.0), 0.0) * scale
        cy = _coerce_float(params.get("center_y", 0.0), 0.0) * scale
        depth_raw = params.get("depth", 1.0)
        depth = self._resolve_depth(depth_raw, shape) * (
            1.0 if isinstance(depth_raw, str) else scale
        )

        top_z = self._face_top_z(shape)
        bottom_z = self._face_bottom_z(shape)
        # We create a tool shape, then subtract it from the part.
        if profile == "circular":
            diameter = _coerce_float(params.get("diameter", 10.0), 10.0) * scale
            tool = Part.makeCylinder(diameter / 2.0, depth + 1.0)
        else:
            length = _coerce_float(params.get("length", 10.0), 10.0) * scale
            width = _coerce_float(params.get("width", 10.0), 10.0) * scale
            tool = Part.makeBox(length, width, depth + 1.0)
            tool.translate(FreeCAD.Vector(-length / 2, -width / 2, 0))

        if face == "top":
            tool.translate(FreeCAD.Vector(cx, cy, top_z - depth))
        elif face == "bottom":
            tool.translate(FreeCAD.Vector(cx, cy, bottom_z))
        else:
            # Side pockets — rotate the tool so its axis is horizontal.
            # Approximated via bounding-box translate for now.
            tool.translate(FreeCAD.Vector(cx, cy, top_z - depth))

        return shape.cut(tool)

    def _apply_hole(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        face = str(params.get("face", "top")).lower()
        cx = _coerce_float(params.get("center_x", 0.0), 0.0) * scale
        cy = _coerce_float(params.get("center_y", 0.0), 0.0) * scale
        diameter = _coerce_float(params.get("diameter", 5.0), 5.0) * scale
        depth_raw = params.get("depth", "through")
        depth = self._resolve_depth(depth_raw, shape)
        if not isinstance(depth_raw, str):
            depth = depth * scale

        top_z = self._face_top_z(shape)
        bottom_z = self._face_bottom_z(shape)

        # Build a cylindrical cutter that punches from the named face inward.
        cutter = Part.makeCylinder(diameter / 2.0, depth + 2.0)
        if face == "top":
            cutter.translate(FreeCAD.Vector(cx, cy, top_z - depth - 1.0))
        else:
            cutter.translate(FreeCAD.Vector(cx, cy, bottom_z - 1.0))

        shape = shape.cut(cutter)

        # Counterbore / countersink modifiers
        hole_type = str(params.get("hole_type", "plain")).lower()
        if hole_type == "counterbore":
            cb_d = _coerce_float(
                params.get("cbore_diameter", diameter / scale * 2), diameter / scale * 2
            ) * scale
            cb_depth = _coerce_float(
                params.get("cbore_depth", depth / 2), depth / 2
            ) * (scale if not isinstance(params.get("cbore_depth"), str) else 1.0)
            cb = Part.makeCylinder(cb_d / 2.0, cb_depth)
            cb.translate(FreeCAD.Vector(cx, cy, top_z - cb_depth))
            shape = shape.cut(cb)
        return shape

    def _apply_hole_pattern(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        pattern = str(params.get("pattern", "rectangular")).lower()
        if pattern == "rectangular":
            sx = _coerce_float(params.get("start_x", 0.0), 0.0)
            sy = _coerce_float(params.get("start_y", 0.0), 0.0)
            spx = _coerce_float(params.get("spacing_x", 10.0), 10.0)
            spy = _coerce_float(params.get("spacing_y", 10.0), 10.0)
            cx_n = _coerce_int(params.get("count_x", 2), 2)
            cy_n = _coerce_int(params.get("count_y", 2), 2)
            for i in range(cx_n):
                for j in range(cy_n):
                    p = dict(params)
                    p["center_x"] = sx + i * spx
                    p["center_y"] = sy + j * spy
                    p.pop("pattern", None)
                    p.pop("start_x", None)
                    p.pop("start_y", None)
                    shape = self._apply_hole(shape, p, Part, FreeCAD, scale)
            return shape

        if pattern == "circular":
            bcd = _coerce_float(
                params.get("bolt_circle_diameter", params.get("bolt_circle_radius", 50.0) * 2),
                50.0,
            )
            count = _coerce_int(params.get("count", 4), 4)
            start = _coerce_float(params.get("start_angle_deg", 0.0), 0.0)
            for i in range(count):
                angle = math.radians(start + i * 360.0 / count)
                p = dict(params)
                p["center_x"] = (bcd / 2.0) * math.cos(angle)
                p["center_y"] = (bcd / 2.0) * math.sin(angle)
                for key in ("pattern", "bolt_circle_diameter", "bolt_circle_radius", "count", "start_angle_deg"):
                    p.pop(key, None)
                shape = self._apply_hole(shape, p, Part, FreeCAD, scale)
            return shape

        return shape

    def _apply_cutout(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        # Cutout = through-cut from a 2D profile. Reuse pocket with "through" depth.
        p = dict(params)
        p.setdefault("depth", "through")
        return self._apply_pocket(shape, p, Part, FreeCAD, scale)

    def _apply_boss(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        cx = _coerce_float(params.get("center_x", 0.0), 0.0) * scale
        cy = _coerce_float(params.get("center_y", 0.0), 0.0) * scale
        diameter = _coerce_float(params.get("diameter", 10.0), 10.0) * scale
        height = _coerce_float(params.get("height", 5.0), 5.0) * scale
        top_z = self._face_top_z(shape)
        boss = Part.makeCylinder(diameter / 2.0, height)
        boss.translate(FreeCAD.Vector(cx, cy, top_z))
        return shape.fuse(boss)

    def _apply_pad(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        cx = _coerce_float(params.get("center_x", 0.0), 0.0) * scale
        cy = _coerce_float(params.get("center_y", 0.0), 0.0) * scale
        length = _coerce_float(params.get("length", 10.0), 10.0) * scale
        width = _coerce_float(params.get("width", 10.0), 10.0) * scale
        height = _coerce_float(params.get("height", 5.0), 5.0) * scale
        top_z = self._face_top_z(shape)
        pad = Part.makeBox(length, width, height)
        pad.translate(FreeCAD.Vector(cx - length / 2, cy - width / 2, top_z))
        return shape.fuse(pad)

    def _apply_fillet(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        radius = _coerce_float(params.get("radius", 1.0), 1.0) * scale
        try:
            return shape.makeFillet(radius, shape.Edges)
        except Exception:  # noqa: BLE001
            return shape

    def _apply_chamfer(
        self,
        shape: Any,
        params: dict[str, Any],
        Part: Any,
        FreeCAD: Any,
        scale: float,
    ) -> Any:
        size = _coerce_float(params.get("size", 1.0), 1.0) * scale
        try:
            return shape.makeChamfer(size, shape.Edges)
        except Exception:  # noqa: BLE001
            return shape

    # --------------------------------------------------------------------- #

    def _resolve_depth(self, depth: Any, shape: Any) -> float:
        """Handle 'through' string; for numerics, return raw value (unscaled)."""
        if isinstance(depth, str) and depth.lower() == "through":
            bb = shape.BoundBox
            return max(bb.XLength, bb.YLength, bb.ZLength) * 1.1
        return _coerce_float(depth, 1.0)

    # --------------------------------------------------------------------- #

    def _generate_impl(
        self,
        doc: IGLDocument,
        output_dir: Path,
    ) -> DriverResult:
        FreeCAD, Part = _try_import_freecad()
        if FreeCAD is None or Part is None:
            return DriverResult.failure(
                self.name, "FreeCAD Python bindings not importable"
            )

        scale = igl_units_to_mm_scale(str(doc.part.units))
        try:
            shape = self._build_stock(doc, Part)
        except Exception as exc:  # noqa: BLE001
            return DriverResult.failure(self.name, f"stock build failed: {exc}")

        translators = {
            "pocket": self._apply_pocket,
            "hole": self._apply_hole,
            "hole_pattern": self._apply_hole_pattern,
            "cutout": self._apply_cutout,
            "boss": self._apply_boss,
            "pad": self._apply_pad,
            "fillet": self._apply_fillet,
            "chamfer": self._apply_chamfer,
        }

        warnings: list[str] = []
        for feature in doc.features:
            fn = translators.get(feature.type)
            if fn is None:
                warnings.append(
                    f"feature {feature.id} type {feature.type!r} not implemented in freecad driver"
                )
                continue
            try:
                shape = fn(shape, feature.params, Part, FreeCAD, scale)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{feature.id} ({feature.type}) failed: {exc}")

        step_path = output_dir / "part.step"
        stl_path = output_dir / "part.stl"

        try:
            shape.exportStep(str(step_path))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"STEP export failed: {exc}")

        try:
            # Mesh module does STL. Tessellate with a conservative deflection.
            Mesh = importlib.import_module("Mesh")
            mesh = Mesh.Mesh(shape.tessellate(0.05))
            mesh.write(str(stl_path))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"STL export failed: {exc}")

        success = step_path.is_file() or stl_path.is_file()
        result = DriverResult(
            success=success,
            driver=self.name,
            step_file=str(step_path) if step_path.is_file() else "",
            stl_file=str(stl_path) if stl_path.is_file() else "",
            warnings=warnings,
            metadata={
                "feature_count": len(doc.features),
                "stock_type": doc.stock.type,
                "units": str(doc.part.units),
            },
        )
        if success:
            save_result_sidecar(result, output_dir)
        return result
