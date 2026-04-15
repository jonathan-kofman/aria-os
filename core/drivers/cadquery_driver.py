"""
core/drivers/cadquery_driver.py — IGL → CadQuery driver.

This driver is the BRIDGE between the new IGL abstraction and the existing
ARIA pipeline. It translates each IGL feature into CadQuery Python code,
executes it via the `cadquery` package, and exports STEP + STL.

Importantly, this driver does NOT touch the existing
aria_os/generators/cadquery_generator.py — it generates fresh code from the
IGL directly. The existing 80+ templates remain the authoritative path for
the ARIA part library. The IGL driver is for new flows where the LLM
emits an IGL document instead of raw CadQuery code.

The CadQuery driver is the ultimate fallback when no other driver is
available, because CadQuery ships with the project and is the most mature
geometry backend in this repo.

Supported features (v1):
    Stock:        block, cylinder, tube
    Subtractive:  pocket (rect/circular), hole, hole_pattern (rect/circular),
                  slot, cutout
    Additive:     boss, pad
    Modifiers:    fillet, chamfer, shell

Not yet supported (fall through to .unsupported_features in validate_igl):
    bend, flange, tab, relief (sheet metal — planned)
    mirror, pattern_linear, pattern_circular
    sketch elements as standalone features
    rib, groove
    stock from_profile
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    save_result_sidecar,
)


# ---------------------------------------------------------------------------
# Code generation helpers
#
# We produce a single CadQuery script as a string, then exec() it in a
# fresh namespace. This mirrors what aria_os/generators/cadquery_generator.py
# already does with its templates, so the pattern is proven.
# ---------------------------------------------------------------------------


def _emit_stock(doc: IGLDocument) -> str:
    """Emit CadQuery code that assigns the initial stock shape to `result`."""
    stock = doc.stock
    if isinstance(stock, StockBlock):
        # Use box centered on origin for symmetric coordinate logic.
        return (
            f"result = cq.Workplane('XY').box({stock.x}, {stock.y}, {stock.z})"
        )
    if isinstance(stock, StockCylinder):
        r = stock.diameter / 2.0
        return (
            f"result = cq.Workplane('XY').circle({r}).extrude({stock.height})"
        )
    if isinstance(stock, StockTube):
        ro = stock.outer_diameter / 2.0
        ri = stock.inner_diameter / 2.0
        return (
            f"result = cq.Workplane('XY').circle({ro}).circle({ri}).extrude({stock.height})"
        )
    # from_profile not yet supported — caller should have rejected this.
    raise ValueError(f"unsupported stock type: {type(stock).__name__}")


def _stock_extent_z(doc: IGLDocument) -> float:
    """Max depth a through-hole needs to cut through this stock."""
    stock = doc.stock
    if isinstance(stock, StockBlock):
        return stock.z
    if isinstance(stock, StockCylinder):
        return stock.height
    if isinstance(stock, StockTube):
        return stock.height
    return 1000.0  # generous default


def _workplane_for_face(face: str, stock_z: float) -> str:
    """
    Return a CadQuery .workplane() call that places the sketch on the
    named face. We assume the stock was created centered on origin.
    """
    face = str(face).lower()
    if face == "top":
        return f".faces('>Z').workplane()"
    if face == "bottom":
        return f".faces('<Z').workplane()"
    if face == "front":
        return f".faces('<Y').workplane()"
    if face == "back":
        return f".faces('>Y').workplane()"
    if face == "left":
        return f".faces('<X').workplane()"
    if face == "right":
        return f".faces('>X').workplane()"
    return f".faces('>Z').workplane()"  # fall back to top


def _resolve_depth(depth: Any, stock_z: float) -> float:
    """
    IGL allows depth to be a number or the literal string "through".
    CadQuery needs a negative numeric depth for .cutBlind.
    """
    if isinstance(depth, str) and depth.lower() == "through":
        return stock_z * 1.1  # go slightly past so no shared face
    return _coerce_float(depth, 1.0)


# ---------------------------------------------------------------------------
# Feature translators
#
# Each translator takes (feature_params, stock_z) and returns a CadQuery
# fragment that modifies `result`. We compose the final script by
# concatenating the stock emit plus one fragment per feature.
# ---------------------------------------------------------------------------


def _emit_pocket(p: dict[str, Any], stock_z: float) -> str:
    profile = str(p.get("profile", "rectangle")).lower()
    face = p.get("face", "top")
    cx = _coerce_float(p.get("center_x", 0.0), 0.0)
    cy = _coerce_float(p.get("center_y", 0.0), 0.0)
    depth = _resolve_depth(p.get("depth"), stock_z)
    wp = _workplane_for_face(face, stock_z)

    if profile == "circular":
        diameter = _coerce_float(p.get("diameter", 10.0), 10.0)
        r = diameter / 2.0
        return (
            f"result = result{wp}.center({cx}, {cy}).circle({r}).cutBlind(-{depth})"
        )

    # rectangle (default)
    length = _coerce_float(p.get("length", 10.0), 10.0)
    width = _coerce_float(p.get("width", 10.0), 10.0)
    corner = _coerce_float(p.get("corner_radius", 0.0), 0.0)
    if corner > 0:
        return (
            f"result = result{wp}.center({cx}, {cy})"
            f".rect({length}, {width}).cutBlind(-{depth})"
            f"\nresult = result.edges('|Z').fillet({corner})"
        )
    return (
        f"result = result{wp}.center({cx}, {cy}).rect({length}, {width}).cutBlind(-{depth})"
    )


def _emit_hole(p: dict[str, Any], stock_z: float) -> str:
    face = p.get("face", "top")
    cx = _coerce_float(p.get("center_x", 0.0), 0.0)
    cy = _coerce_float(p.get("center_y", 0.0), 0.0)
    diameter = _coerce_float(p.get("diameter", 5.0), 5.0)
    depth = _resolve_depth(p.get("depth"), stock_z)
    r = diameter / 2.0
    wp = _workplane_for_face(face, stock_z)

    hole_type = str(p.get("hole_type", "plain")).lower()
    if hole_type == "counterbore":
        cbore_d = _coerce_float(p.get("cbore_diameter", diameter * 2), diameter * 2)
        cbore_depth = _coerce_float(p.get("cbore_depth", depth / 2), depth / 2)
        return (
            f"result = result{wp}.center({cx}, {cy})"
            f".cboreHole({diameter}, {cbore_d}, {cbore_depth}, {depth})"
        )
    if hole_type == "countersink":
        csk_d = _coerce_float(p.get("csk_diameter", diameter * 2), diameter * 2)
        csk_angle = _coerce_float(p.get("csk_angle", 82), 82)
        return (
            f"result = result{wp}.center({cx}, {cy})"
            f".cskHole({diameter}, {csk_d}, {csk_angle}, {depth})"
        )
    # plain / tapped — tapped is drawn as a plain hole at nominal diameter
    return (
        f"result = result{wp}.center({cx}, {cy}).circle({r}).cutBlind(-{depth})"
    )


def _emit_hole_pattern(p: dict[str, Any], stock_z: float) -> str:
    pattern = str(p.get("pattern", "rectangular")).lower()
    face = p.get("face", "top")
    diameter = _coerce_float(p.get("diameter", 5.0), 5.0)
    depth = _resolve_depth(p.get("depth"), stock_z)
    r = diameter / 2.0
    wp = _workplane_for_face(face, stock_z)

    if pattern == "rectangular":
        start_x = _coerce_float(p.get("start_x", 0.0), 0.0)
        start_y = _coerce_float(p.get("start_y", 0.0), 0.0)
        spacing_x = _coerce_float(p.get("spacing_x", 10.0), 10.0)
        spacing_y = _coerce_float(p.get("spacing_y", 10.0), 10.0)
        count_x = _coerce_int(p.get("count_x", 2), 2)
        count_y = _coerce_int(p.get("count_y", 2), 2)
        # Build a list of points then cut them in one step.
        lines = [
            f"_pts = []",
        ]
        for i in range(count_x):
            for j in range(count_y):
                x = start_x + i * spacing_x
                y = start_y + j * spacing_y
                lines.append(f"_pts.append(({x}, {y}))")
        lines.append(
            f"result = result{wp}.pushPoints(_pts).circle({r}).cutBlind(-{depth})"
        )
        return "\n".join(lines)

    if pattern == "circular":
        bolt_circle_d = _coerce_float(
            p.get("bolt_circle_diameter", p.get("bolt_circle_radius", 50.0) * 2),
            50.0,
        )
        count = _coerce_int(p.get("count", 4), 4)
        start_angle = _coerce_float(p.get("start_angle_deg", 0.0), 0.0)
        bcr = bolt_circle_d / 2.0
        return (
            f"import math as _m\n"
            f"_pts = [({bcr}*_m.cos(_m.radians({start_angle}+i*360/{count})), "
            f"{bcr}*_m.sin(_m.radians({start_angle}+i*360/{count}))) "
            f"for i in range({count})]\n"
            f"result = result{wp}.pushPoints(_pts).circle({r}).cutBlind(-{depth})"
        )

    # fall back to single center hole
    return _emit_hole(p, stock_z)


def _emit_slot(p: dict[str, Any], stock_z: float) -> str:
    face = p.get("face", "top")
    cx = _coerce_float(p.get("center_x", 0.0), 0.0)
    cy = _coerce_float(p.get("center_y", 0.0), 0.0)
    length = _coerce_float(p.get("length", 20.0), 20.0)
    width = _coerce_float(p.get("width", 5.0), 5.0)
    depth = _resolve_depth(p.get("depth"), stock_z)
    wp = _workplane_for_face(face, stock_z)
    # Slot built from rect with filleted ends — approximated.
    return (
        f"result = result{wp}.center({cx}, {cy}).slot2D({length}, {width}).cutBlind(-{depth})"
    )


def _emit_cutout(p: dict[str, Any], stock_z: float) -> str:
    profile = str(p.get("profile", "rectangle")).lower()
    face = p.get("face", "top")
    cx = _coerce_float(p.get("center_x", 0.0), 0.0)
    cy = _coerce_float(p.get("center_y", 0.0), 0.0)
    depth_val = p.get("depth")
    # "through" for a cutout = full thickness cut
    depth = _resolve_depth(depth_val, stock_z)
    wp = _workplane_for_face(face, stock_z)

    if profile == "circular":
        diameter = _coerce_float(p.get("diameter", 10.0), 10.0)
        r = diameter / 2.0
        return (
            f"result = result{wp}.center({cx}, {cy}).circle({r}).cutBlind(-{depth})"
        )
    length = _coerce_float(p.get("length", 20.0), 20.0)
    width = _coerce_float(p.get("width", 20.0), 20.0)
    corner = _coerce_float(p.get("corner_radius", 0.0), 0.0)
    base = (
        f"result = result{wp}.center({cx}, {cy})"
        f".rect({length}, {width}).cutBlind(-{depth})"
    )
    if corner > 0:
        base += f"\nresult = result.edges('|Z').fillet({corner})"
    return base


def _emit_boss(p: dict[str, Any], stock_z: float) -> str:
    face = p.get("face", "top")
    cx = _coerce_float(p.get("center_x", 0.0), 0.0)
    cy = _coerce_float(p.get("center_y", 0.0), 0.0)
    diameter = _coerce_float(p.get("diameter", 10.0), 10.0)
    height = _coerce_float(p.get("height", 5.0), 5.0)
    r = diameter / 2.0
    wp = _workplane_for_face(face, stock_z)
    return (
        f"result = result{wp}.center({cx}, {cy}).circle({r}).extrude({height})"
    )


def _emit_pad(p: dict[str, Any], stock_z: float) -> str:
    face = p.get("face", "top")
    cx = _coerce_float(p.get("center_x", 0.0), 0.0)
    cy = _coerce_float(p.get("center_y", 0.0), 0.0)
    length = _coerce_float(p.get("length", 10.0), 10.0)
    width = _coerce_float(p.get("width", 10.0), 10.0)
    height = _coerce_float(p.get("height", 5.0), 5.0)
    wp = _workplane_for_face(face, stock_z)
    return (
        f"result = result{wp}.center({cx}, {cy})"
        f".rect({length}, {width}).extrude({height})"
    )


def _emit_fillet(p: dict[str, Any], stock_z: float) -> str:
    radius = _coerce_float(p.get("radius", 1.0), 1.0)
    edges = str(p.get("edges", "all")).lower()
    # We support a small vocabulary of edge selectors here; anything else
    # falls back to all edges.
    if edges in ("all_vertical", "all_outer_vertical"):
        return f"result = result.edges('|Z').fillet({radius})"
    if edges == "all_top":
        return f"result = result.faces('>Z').edges().fillet({radius})"
    if edges == "all_bottom":
        return f"result = result.faces('<Z').edges().fillet({radius})"
    return f"result = result.edges().fillet({radius})"


def _emit_chamfer(p: dict[str, Any], stock_z: float) -> str:
    size = _coerce_float(p.get("size", 1.0), 1.0)
    edges = str(p.get("edges", "all")).lower()
    if edges in ("all_top_outer", "all_top"):
        return f"result = result.faces('>Z').edges().chamfer({size})"
    if edges in ("all_bottom_outer", "all_bottom"):
        return f"result = result.faces('<Z').edges().chamfer({size})"
    if edges == "all_vertical":
        return f"result = result.edges('|Z').chamfer({size})"
    if edges == "all_outer":
        return f"result = result.edges().chamfer({size})"
    return f"result = result.edges().chamfer({size})"


def _emit_shell(p: dict[str, Any], stock_z: float) -> str:
    wall = _coerce_float(p.get("wall_thickness", 2.0), 2.0)
    open_face = str(p.get("open_face", "top")).lower()
    face_selector = {
        "top": "'>Z'", "bottom": "'<Z'", "front": "'<Y'",
        "back": "'>Y'", "left": "'<X'", "right": "'>X'",
    }.get(open_face, "'>Z'")
    return f"result = result.faces({face_selector}).shell(-{wall})"


_TRANSLATORS = {
    "pocket": _emit_pocket,
    "hole": _emit_hole,
    "hole_pattern": _emit_hole_pattern,
    "slot": _emit_slot,
    "cutout": _emit_cutout,
    "boss": _emit_boss,
    "pad": _emit_pad,
    "fillet": _emit_fillet,
    "chamfer": _emit_chamfer,
    "shell": _emit_shell,
}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class CadQueryDriver(CADDriver):
    """IGL driver that produces STEP/STL via the in-process CadQuery kernel."""

    name = "cadquery"

    def get_description(self) -> str:
        return "CadQuery (OpenCascade kernel, in-process)"

    def is_available(self) -> bool:
        try:
            import cadquery  # noqa: F401
            return True
        except ImportError:
            return False

    def get_supported_features(self) -> list[str]:
        return list(_TRANSLATORS.keys())

    # --------------------------------------------------------------------- #
    # Code synthesis
    # --------------------------------------------------------------------- #

    def build_script(self, doc: IGLDocument) -> str:
        """
        Compose the full CadQuery script string for an IGL document.

        Exposed for testing and debugging — the manager and test suite can
        inspect what code would be produced without actually executing it.
        """
        lines: list[str] = [
            "import cadquery as cq",
            "",
            _emit_stock(doc),
        ]
        stock_z = _stock_extent_z(doc)

        for feature in doc.features:
            translator = _TRANSLATORS.get(feature.type)
            if translator is None:
                lines.append(
                    f"# feature {feature.id} of type {feature.type} is not yet supported"
                )
                continue
            try:
                lines.append(translator(feature.params, stock_z))
            except Exception as exc:  # noqa: BLE001
                lines.append(
                    f"# feature {feature.id} translation error: {exc}"
                )

        lines.append("")
        lines.append("bb = result.val().BoundingBox()")
        lines.append(
            'print(f"BBOX:{bb.xlen:.3f},{bb.ylen:.3f},{bb.zlen:.3f}")'
        )
        return "\n".join(lines)

    # --------------------------------------------------------------------- #
    # Execution
    # --------------------------------------------------------------------- #

    def _generate_impl(
        self,
        doc: IGLDocument,
        output_dir: Path,
    ) -> DriverResult:
        script = self.build_script(doc)
        script_path = output_dir / "generated.py"
        script_path.write_text(script)

        # exec the script in a fresh namespace
        namespace: dict[str, Any] = {}
        try:
            exec(script, namespace, namespace)
        except Exception as exc:  # noqa: BLE001
            return DriverResult(
                success=False,
                driver=self.name,
                errors=[f"CadQuery execution error: {exc}"],
                metadata={"script_path": str(script_path)},
            )

        result_obj = namespace.get("result")
        if result_obj is None:
            return DriverResult(
                success=False,
                driver=self.name,
                errors=["script did not assign `result`"],
                metadata={"script_path": str(script_path)},
            )

        step_path = output_dir / "part.step"
        stl_path = output_dir / "part.stl"
        warnings: list[str] = []
        try:
            import cadquery as cq

            cq.exporters.export(result_obj, str(step_path))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"STEP export failed: {exc}")

        try:
            import cadquery as cq

            cq.exporters.export(result_obj, str(stl_path))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"STL export failed: {exc}")

        success = step_path.is_file() or stl_path.is_file()
        res = DriverResult(
            success=success,
            driver=self.name,
            step_file=str(step_path) if step_path.is_file() else "",
            stl_file=str(stl_path) if stl_path.is_file() else "",
            warnings=warnings,
            metadata={
                "script_path": str(script_path),
                "feature_count": len(doc.features),
                "stock_type": doc.stock.type,
                "units": str(doc.part.units),
            },
        )
        if success:
            save_result_sidecar(res, output_dir)
        return res
