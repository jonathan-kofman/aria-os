"""Autonomous CAM contract — unified interface for any CAD's CAM stage.

This module defines the standard operation shape that each CAD bridge must implement:

    {kind: "generateToolpath",
     params: {step_path, stock_oversize_mm, tool_diameter_mm, feed_mm_min,
              spindle_rpm, operation: "facing|profile|pocket|drill|3d_contour",
              post_processor: "fanuc|haas|grbl|linuxcnc",
              output_path: "<dir>/program.nc"}}

The operation MUST return:
    {ok, gcode_path, operations_count, estimated_time_min, tool_list, rendered_images}

Each CAD bridge (SW, Fusion, Onshape, Rhino) either:
1. Implements native CAM (Fusion 360, maybe SW CAM)
2. Falls back to synthetic CAM (Rhino, AutoCAD, or if license unavailable)

This layer orchestrates the dispatch and tracks which CADs support which paths.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from .synthetic_cam import generate_synthetic_cam


ToolpathOperation = Literal["facing", "profile", "pocket", "drill", "3d_contour"]
PostProcessor = Literal["fanuc", "haas", "grbl", "linuxcnc"]


class GenerateToolpathOp:
    """Autonomous toolpath generation operation."""

    def __init__(
        self,
        step_path: str | Path,
        stock_oversize_mm: float = 5.0,
        tool_diameter_mm: float = 6.0,
        feed_mm_min: float = 200.0,
        spindle_rpm: int = 3000,
        operation: ToolpathOperation = "facing",
        post_processor: PostProcessor = "fanuc",
        output_path: str | Path | None = None,
    ):
        self.step_path = Path(step_path)
        self.stock_oversize_mm = stock_oversize_mm
        self.tool_diameter_mm = tool_diameter_mm
        self.feed_mm_min = feed_mm_min
        self.spindle_rpm = spindle_rpm
        self.operation = operation
        self.post_processor = post_processor
        self.output_path = Path(output_path) if output_path else None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "kind": "generateToolpath",
            "params": {
                "step_path": str(self.step_path),
                "stock_oversize_mm": self.stock_oversize_mm,
                "tool_diameter_mm": self.tool_diameter_mm,
                "feed_mm_min": self.feed_mm_min,
                "spindle_rpm": self.spindle_rpm,
                "operation": self.operation,
                "post_processor": self.post_processor,
                "output_path": str(self.output_path) if self.output_path else None,
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> GenerateToolpathOp:
        """Deserialize from dict."""
        p = d.get("params", {})
        return cls(
            step_path=p["step_path"],
            stock_oversize_mm=p.get("stock_oversize_mm", 5.0),
            tool_diameter_mm=p.get("tool_diameter_mm", 6.0),
            feed_mm_min=p.get("feed_mm_min", 200.0),
            spindle_rpm=p.get("spindle_rpm", 3000),
            operation=p.get("operation", "facing"),
            post_processor=p.get("post_processor", "fanuc"),
            output_path=p.get("output_path"),
        )


class GenerateToolpathResult:
    """Result of a CAM generation run."""

    def __init__(
        self,
        ok: bool,
        gcode_path: str | None = None,
        operations_count: int = 0,
        estimated_time_min: float = 0.0,
        tool_list: list[dict] | None = None,
        rendered_images: dict[str, str] | None = None,
        error: str | None = None,
        cad_name: str | None = None,
        method: str | None = None,
    ):
        self.ok = ok
        self.gcode_path = gcode_path
        self.operations_count = operations_count
        self.estimated_time_min = estimated_time_min
        self.tool_list = tool_list or []
        self.rendered_images = rendered_images or {}
        self.error = error
        self.cad_name = cad_name  # Which CAD generated this (SW, Fusion, synthetic, etc.)
        self.method = method  # native_cam, synthetic, agent, etc.

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON."""
        return {
            "ok": self.ok,
            "gcode_path": self.gcode_path,
            "operations_count": self.operations_count,
            "estimated_time_min": self.estimated_time_min,
            "tool_list": self.tool_list,
            "rendered_images": self.rendered_images,
            "error": self.error,
            "cad_name": self.cad_name,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GenerateToolpathResult:
        """Deserialize from dict."""
        return cls(
            ok=d["ok"],
            gcode_path=d.get("gcode_path"),
            operations_count=d.get("operations_count", 0),
            estimated_time_min=d.get("estimated_time_min", 0.0),
            tool_list=d.get("tool_list"),
            rendered_images=d.get("rendered_images"),
            error=d.get("error"),
            cad_name=d.get("cad_name"),
            method=d.get("method"),
        )


async def dispatch_autonomous_cam(
    op: GenerateToolpathOp,
    cad_bridge_ports: dict[str, int] | None = None,
) -> GenerateToolpathResult:
    """
    Dispatch a CAM operation to the appropriate CAD or fallback to synthetic.

    Args:
        op: GenerateToolpathOp with all parameters
        cad_bridge_ports: dict mapping CAD names to HTTP ports (sw=7501, fusion=7504, etc.)

    Returns:
        GenerateToolpathResult with final gcode_path, renders, and metadata.

    Strategy:
        1. Try native CAD CAM (Fusion 360 on port 7504)
        2. Fall back to synthetic CAM (always available)
        3. Return result with cad_name + method metadata
    """
    cad_bridge_ports = cad_bridge_ports or {
        "solidworks": 7501,
        "fusion": 7504,
        "onshape": 7503,
        "rhino": 7502,
        "autocad": 7506,
    }

    # Try Fusion 360 CAM first (has built-in CAM module)
    fusion_port = cad_bridge_ports.get("fusion")
    if fusion_port:
        try:
            result = await _try_fusion_cam(op, fusion_port)
            if result.ok:
                return result
        except Exception as e:
            print(f"[CAM] Fusion CAM failed: {e}, falling back to synthetic")

    # Fall back to synthetic CAM (always works, uses simple raster + profile)
    print(f"[CAM] Using synthetic CAM fallback")
    return _generate_synthetic_cam_result(op)


def _generate_synthetic_cam_result(op: GenerateToolpathOp) -> GenerateToolpathResult:
    """Dispatch to synthetic CAM and wrap result."""
    result = generate_synthetic_cam(
        step_path=op.step_path,
        out_dir=Path(op.output_path).parent if op.output_path else None,
        material="aluminium_6061",  # Default; could pass through op
        machine="generic",
        stock_oversize_mm=op.stock_oversize_mm,
    )

    if result["ok"]:
        return GenerateToolpathResult(
            ok=True,
            gcode_path=result.get("gcode_path"),
            operations_count=result.get("operations_count", 0),
            estimated_time_min=result.get("estimated_time_min", 0.0),
            tool_list=result.get("tool_list"),
            rendered_images=result.get("rendered_images", {}),
            cad_name="synthetic",
            method="raster_profile_pocket",
        )
    else:
        return GenerateToolpathResult(
            ok=False,
            error=result.get("error"),
            cad_name="synthetic",
            method="raster_profile_pocket",
        )


async def _try_fusion_cam(op: GenerateToolpathOp, port: int) -> GenerateToolpathResult:
    """Attempt Fusion 360 CAM via HTTP bridge (placeholder)."""
    # This would POST to http://localhost:{port}/api/cam
    # For now, raise to trigger fallback
    raise NotImplementedError("Fusion HTTP CAM bridge not yet wired")


CAM_CAPABILITY_MATRIX = {
    "solidworks": {
        "native_cam": False,  # requires SOLIDWORKS CAM add-in (license not checked)
        "synthetic_fallback": True,
    },
    "fusion360": {
        "native_cam": True,  # adsk.cam module built-in
        "synthetic_fallback": True,
    },
    "onshape": {
        "native_cam": False,  # Manufacturing Studio is cloud REST, not exposed in bridge
        "synthetic_fallback": True,
    },
    "rhino": {
        "native_cam": False,  # RhinoCAM is 3rd-party, not integrated
        "synthetic_fallback": True,
    },
    "autocad": {
        "native_cam": False,  # No native CAM module
        "synthetic_fallback": True,
    },
    "synthetic": {
        "native_cam": False,
        "synthetic_fallback": True,  # This IS the synthetic fallback
    },
}


def get_cam_status(cad_name: str) -> dict[str, bool]:
    """Get CAM capability status for a CAD."""
    return CAM_CAPABILITY_MATRIX.get(cad_name, {"native_cam": False, "synthetic_fallback": False})
