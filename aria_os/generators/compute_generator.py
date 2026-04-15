"""
aria_os/generators/compute_generator.py — Direct Compute API geometry generator

Builds geometry via rhino3dm (local primitives) + compute_rhino3d (server booleans).
Exports via CadQuery. No IronPython scripts, no PythonEvaluate.

Pipeline: rhino3dm primitives → compute_rhino3d booleans → .3dm + CQ STEP/STL
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import rhino3dm
import compute_rhino3d.Util
import compute_rhino3d.Brep as cBrep

from .. import event_bus

RHINO_COMPUTE_URL = os.environ.get("RHINO_COMPUTE_URL", "http://localhost:8081/")
compute_rhino3d.Util.url = RHINO_COMPUTE_URL


# ---------------------------------------------------------------------------
# Declarative geometry plan
# ---------------------------------------------------------------------------

@dataclass
class Primitive:
    kind: Literal["cylinder", "box", "sphere"]
    label: str
    params: dict[str, float]


@dataclass
class BooleanOp:
    op: Literal["difference", "union", "intersection"]
    target: str
    tool: str
    tolerance: float = 0.001


@dataclass
class GeometryPlan:
    part_id: str
    primitives: list[Primitive] = field(default_factory=list)
    operations: list[BooleanOp] = field(default_factory=list)
    result_label: str = "result"
    cq_export_code: str = ""


# ---------------------------------------------------------------------------
# Primitive builder (local, no server)
# ---------------------------------------------------------------------------

def _build_primitives(plan: GeometryPlan) -> dict[str, rhino3dm.Brep]:
    breps: dict[str, rhino3dm.Brep] = {}
    for prim in plan.primitives:
        p = prim.params
        if prim.kind == "cylinder":
            center = rhino3dm.Point3d(
                p.get("center_x", 0), p.get("center_y", 0), p.get("center_z", 0)
            )
            circle = rhino3dm.Circle(center, p["radius"])
            cyl = rhino3dm.Cylinder(circle, p["height"])
            breps[prim.label] = cyl.ToBrep(
                p.get("cap_bottom", True), p.get("cap_top", True)
            )
        elif prim.kind == "box":
            min_pt = rhino3dm.Point3d(
                p.get("origin_x", 0), p.get("origin_y", 0), p.get("origin_z", 0)
            )
            max_pt = rhino3dm.Point3d(
                min_pt.X + p["size_x"], min_pt.Y + p["size_y"], min_pt.Z + p["size_z"]
            )
            bbox = rhino3dm.BoundingBox(min_pt, max_pt)
            breps[prim.label] = rhino3dm.Brep.CreateFromBox(bbox)
        elif prim.kind == "sphere":
            center = rhino3dm.Point3d(
                p.get("center_x", 0), p.get("center_y", 0), p.get("center_z", 0)
            )
            sphere = rhino3dm.Sphere(center, p["radius"])
            breps[prim.label] = sphere.ToBrep()
    return breps


# ---------------------------------------------------------------------------
# Boolean executor (server-side via Compute)
# ---------------------------------------------------------------------------

def _execute_booleans(
    primitives: dict[str, rhino3dm.Brep],
    operations: list[BooleanOp],
) -> rhino3dm.Brep | None:
    working = dict(primitives)
    for op in operations:
        target = working.get(op.target)
        tool = working.get(op.tool)
        if target is None or tool is None:
            continue
        try:
            if op.op == "difference":
                result = cBrep.CreateBooleanDifference1(
                    [target], [tool], op.tolerance, True
                )
            elif op.op == "union":
                result = cBrep.CreateBooleanUnion([target, tool], op.tolerance, True)
            elif op.op == "intersection":
                result = cBrep.CreateBooleanIntersection1(
                    [target], [tool], op.tolerance, True
                )
            else:
                continue
            if result and len(result) > 0:
                working[op.target] = result[0]
        except Exception as e:
            event_bus.emit("warning", f"Boolean {op.op} failed: {e}")

    # Return the last operation's target, or the first primitive
    if operations:
        return working.get(operations[-1].target)
    if primitives:
        return next(iter(primitives.values()))
    return None


# ---------------------------------------------------------------------------
# CadQuery export helper
# ---------------------------------------------------------------------------

def _export_via_cq(cq_code: str, step_path: str, stl_path: str) -> tuple[str, str]:
    """Execute CadQuery code and export STEP + STL."""
    import cadquery as cq
    from cadquery import exporters

    ns: dict[str, Any] = {"cq": cq}
    exec(compile(cq_code, "<cq_export>", "exec"), ns)
    result = ns.get("result")
    if result is None:
        raise RuntimeError("CadQuery code did not produce a 'result' variable")

    Path(step_path).parent.mkdir(parents=True, exist_ok=True)
    Path(stl_path).parent.mkdir(parents=True, exist_ok=True)
    exporters.export(result, step_path)
    exporters.export(result, stl_path, exportType="STL")
    return step_path, stl_path


# ---------------------------------------------------------------------------
# Part templates — return GeometryPlan
# ---------------------------------------------------------------------------

def _compute_brake_drum(plan: dict, params: dict) -> GeometryPlan:
    od = float(params.get("od_mm", 200.0))
    width = float(params.get("width_mm", 60.0))
    wall = float(params.get("wall_mm", 8.0))
    bore = float(params.get("bore_mm", 40.0))
    id_ = od - 2 * wall

    gp = GeometryPlan(part_id="aria_brake_drum")
    gp.primitives = [
        Primitive("cylinder", "outer", {"radius": od / 2, "height": width}),
        Primitive("cylinder", "inner", {"radius": id_ / 2, "height": width + 1}),
        Primitive("cylinder", "bore", {"radius": bore / 2, "height": width + 2}),
    ]
    gp.operations = [
        BooleanOp("difference", "outer", "inner"),
        BooleanOp("difference", "outer", "bore"),
    ]
    gp.result_label = "outer"
    gp.cq_export_code = f"""
import cadquery as cq
result = (
    cq.Workplane("XY")
    .circle({od/2}).extrude({width})
    .cut(cq.Workplane("XY").circle({id_/2}).extrude({width}))
    .cut(cq.Workplane("XY").circle({bore/2}).extrude({width}))
)
"""
    return gp


def _compute_spool(plan: dict, params: dict) -> GeometryPlan:
    hub_od = float(params.get("hub_od_mm", 120.0))
    flange_od = float(params.get("flange_od_mm", 200.0))
    width = float(params.get("width_mm", 80.0))
    bore = float(params.get("bore_mm", 30.0))
    flange_t = float(params.get("flange_t_mm", 8.0))

    gp = GeometryPlan(part_id="aria_spool")
    gp.primitives = [
        Primitive("cylinder", "hub", {"radius": hub_od / 2, "height": width}),
        Primitive("cylinder", "fl_bot", {"radius": flange_od / 2, "height": flange_t}),
        Primitive("cylinder", "fl_top", {
            "radius": flange_od / 2, "height": flange_t,
            "center_z": width - flange_t,
        }),
        Primitive("cylinder", "bore_cyl", {"radius": bore / 2, "height": width + 2}),
    ]
    gp.operations = [
        BooleanOp("union", "hub", "fl_bot"),
        BooleanOp("union", "hub", "fl_top"),
        BooleanOp("difference", "hub", "bore_cyl"),
    ]
    gp.result_label = "hub"
    gp.cq_export_code = f"""
import cadquery as cq
hub = cq.Workplane("XY").circle({hub_od/2}).extrude({width})
fl_b = cq.Workplane("XY").circle({flange_od/2}).extrude({flange_t})
fl_t = cq.Workplane("XY").workplane(offset={width - flange_t}).circle({flange_od/2}).extrude({flange_t})
result = hub.union(fl_b).union(fl_t).cut(cq.Workplane("XY").circle({bore/2}).extrude({width}))
"""
    return gp


def _compute_cam_collar(plan: dict, params: dict) -> GeometryPlan:
    od = float(params.get("od_mm", 80.0))
    bore = float(params.get("bore_mm", 30.0))
    length = float(params.get("length_mm", 40.0))

    gp = GeometryPlan(part_id="aria_cam_collar")
    gp.primitives = [
        Primitive("cylinder", "outer", {"radius": od / 2, "height": length}),
        Primitive("cylinder", "bore_cyl", {"radius": bore / 2, "height": length + 2}),
    ]
    gp.operations = [
        BooleanOp("difference", "outer", "bore_cyl"),
    ]
    gp.result_label = "outer"
    gp.cq_export_code = f"""
import cadquery as cq
result = (
    cq.Workplane("XY").circle({od/2}).extrude({length})
    .cut(cq.Workplane("XY").circle({bore/2}).extrude({length}))
)
"""
    return gp


def _compute_housing(plan: dict, params: dict) -> GeometryPlan:
    od = float(params.get("od_mm", 260.0))
    wall = float(params.get("wall_mm", 10.0))
    length = float(params.get("length_mm", 180.0))
    id_ = od - 2 * wall

    gp = GeometryPlan(part_id="aria_housing")
    gp.primitives = [
        Primitive("cylinder", "outer", {"radius": od / 2, "height": length}),
        Primitive("cylinder", "inner", {"radius": id_ / 2, "height": length + 1}),
    ]
    gp.operations = [
        BooleanOp("difference", "outer", "inner"),
    ]
    gp.result_label = "outer"
    gp.cq_export_code = f"""
import cadquery as cq
result = (
    cq.Workplane("XY").circle({od/2}).extrude({length})
    .cut(cq.Workplane("XY").circle({id_/2}).extrude({length}))
)
"""
    return gp


def _compute_rope_guide(plan: dict, params: dict) -> GeometryPlan:
    width = float(params.get("width_mm", 60.0))
    height = float(params.get("height_mm", 40.0))
    thick = float(params.get("thickness_mm", 12.0))
    slot = float(params.get("slot_dia_mm", 12.0))

    gp = GeometryPlan(part_id="aria_rope_guide")
    gp.primitives = [
        Primitive("box", "body", {
            "origin_x": -width / 2, "origin_y": -height / 2,
            "size_x": width, "size_y": height, "size_z": thick,
        }),
        Primitive("cylinder", "slot_cyl", {
            "center_x": 0, "center_y": -height, "center_z": thick / 2,
            "radius": slot / 2, "height": 2 * height,
        }),
    ]
    gp.operations = [
        BooleanOp("difference", "body", "slot_cyl"),
    ]
    gp.result_label = "body"
    gp.cq_export_code = f"""
import cadquery as cq
result = (
    cq.Workplane("XY")
    .box({width}, {height}, {thick})
    .faces(">Y").workplane()
    .hole({slot})
)
"""
    return gp


# ---------------------------------------------------------------------------
# Template map
# ---------------------------------------------------------------------------

_COMPUTE_TEMPLATE_MAP: dict[str, Callable] = {
    "aria_brake_drum": _compute_brake_drum,
    "aria_spool": _compute_spool,
    "aria_cam_collar": _compute_cam_collar,
    "aria_housing": _compute_housing,
    "aria_rope_guide": _compute_rope_guide,
}


# ---------------------------------------------------------------------------
# LLM fallback — generates a GeometryPlan as JSON
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = """\
You are a parametric CAD geometry planner. Given a part description, output a JSON
object describing primitives and boolean operations.

Available primitives:
- cylinder: {center_x, center_y, center_z, radius, height}
- box: {origin_x, origin_y, origin_z, size_x, size_y, size_z}
- sphere: {center_x, center_y, center_z, radius}

Available boolean operations (applied in order):
- difference: subtract tool from target
- union: merge target and tool
- intersection: keep only overlapping volume

Rules:
- All dimensions in mm
- Build base solid first, then subtractive features, then holes last
- For hollow parts: create outer solid, then boolean-difference the inner void
- Maximum 20 primitives, 15 operations
- The "target" in each operation refers to the running result by label
- Also provide CadQuery Python code that builds equivalent geometry (assign to 'result')

Output ONLY valid JSON (no markdown fences):
{
  "part_id": "...",
  "primitives": [{"kind": "cylinder", "label": "outer", "params": {...}}, ...],
  "operations": [{"op": "difference", "target": "outer", "tool": "bore"}, ...],
  "result_label": "outer",
  "cq_export_code": "import cadquery as cq\\nresult = ..."
}
"""


def _generate_plan_via_llm(goal: str, plan: dict, repo_root: Path) -> GeometryPlan:
    """Use LLM to generate a GeometryPlan for unknown parts."""
    from ..llm_client import call_llm

    params = plan.get("params", {})
    param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "none"

    user_prompt = (
        f"Part: {goal}\n"
        f"Parameters: {param_str}\n"
        f"Part ID: {plan.get('part_id', 'unknown')}\n\n"
        "Generate a GeometryPlan JSON for this part."
    )

    response = call_llm(user_prompt, system=_LLM_SYSTEM_PROMPT, repo_root=repo_root)
    if not response:
        raise RuntimeError("LLM returned empty response")

    # Extract JSON from response
    text = response.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])
    data = json.loads(text)

    gp = GeometryPlan(part_id=data.get("part_id", plan.get("part_id", "unknown")))
    for p in data.get("primitives", []):
        gp.primitives.append(Primitive(
            kind=p["kind"], label=p["label"], params=p.get("params", {})
        ))
    for o in data.get("operations", []):
        gp.operations.append(BooleanOp(
            op=o["op"], target=o["target"], tool=o["tool"],
            tolerance=o.get("tolerance", 0.001),
        ))
    gp.result_label = data.get("result_label", "result")
    gp.cq_export_code = data.get("cq_export_code", "")
    return gp


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def write_compute_artifacts(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    """
    Build geometry via Compute API and export STEP/STL.

    Returns dict with: step_path, stl_path, dm_path, plan_path
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent

    part_id = (plan.get("part_id") or "unknown_part").replace("/", "_")
    params = plan.get("params", {})
    out_dir = repo_root / "outputs" / "cad" / "compute" / part_id
    out_dir.mkdir(parents=True, exist_ok=True)

    event_bus.emit("compute", f"Building {part_id} via Compute API", {"part_id": part_id})

    # 1. Get or generate GeometryPlan
    template_fn = _COMPUTE_TEMPLATE_MAP.get(part_id)
    if template_fn:
        event_bus.emit("step", f"Using Compute template: {part_id}")
        gp = template_fn(plan, params)
    else:
        event_bus.emit("step", f"LLM generating Compute plan for: {part_id}")
        try:
            gp = _generate_plan_via_llm(goal, plan, repo_root)
        except Exception as e:
            event_bus.emit("error", f"LLM plan generation failed: {e}")
            raise

    # Save plan for debugging
    plan_path = out_dir / "geometry_plan.json"
    plan_path.write_text(json.dumps({
        "part_id": gp.part_id,
        "primitives": [{"kind": p.kind, "label": p.label, "params": p.params} for p in gp.primitives],
        "operations": [{"op": o.op, "target": o.target, "tool": o.tool} for o in gp.operations],
        "result_label": gp.result_label,
    }, indent=2), encoding="utf-8")

    # 2. Build primitives locally
    event_bus.emit("step", "Building primitives via rhino3dm")
    primitives = _build_primitives(gp)

    # 3. Execute booleans on Compute
    event_bus.emit("step", "Executing booleans via Compute API")
    brep = _execute_booleans(primitives, gp.operations)

    artifacts: dict[str, str] = {"plan_path": str(plan_path)}

    # 4. Save .3dm
    if brep:
        dm_path = out_dir / f"{part_id}.3dm"
        from ..rhino_export import brep_to_3dm
        brep_to_3dm(brep, dm_path)
        artifacts["dm_path"] = str(dm_path)
        bb = brep.GetBoundingBox()
        dims = (bb.Max.X - bb.Min.X, bb.Max.Y - bb.Min.Y, bb.Max.Z - bb.Min.Z)
        event_bus.emit("compute", f"Brep: {dims[0]:.1f}x{dims[1]:.1f}x{dims[2]:.1f}mm")

    # 5. Export STEP/STL via CadQuery
    if gp.cq_export_code:
        event_bus.emit("step", "Exporting STEP/STL via CadQuery")
        try:
            _export_via_cq(gp.cq_export_code, step_path, stl_path)
            artifacts["step_path"] = step_path
            artifacts["stl_path"] = stl_path
            event_bus.emit("compute", f"STEP: {Path(step_path).stat().st_size / 1024:.1f} KB")
        except Exception as e:
            event_bus.emit("error", f"CQ export failed: {e}")

    return artifacts
