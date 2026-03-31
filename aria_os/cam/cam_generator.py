"""
cam_generator.py — Automated Fusion 360 CAM script generator.

Reads a STEP file + material → analyzes geometry → selects tools →
generates a complete Fusion 360 Python CAM script that, when run inside
Fusion's Script editor, fully sets up and posts CNC toolpaths.

No manual CAM work required — just open Fusion, run the script, get gcode.

Usage:
    from aria_os.cam_generator import generate_cam_script
    generate_cam_script("outputs/cad/step/aria_housing.step", material="aluminium_6061")

    # or from CLI:
    python -m aria_os.cam_generator outputs/cad/step/aria_housing.step --material aluminium_6061
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL_LIB_PATH = ROOT / "tools" / "fusion_tool_library.json"
OUT_CAM = ROOT / "outputs" / "cam"


# ─── Geometry analysis ────────────────────────────────────────────────────────

def analyze_step(step_path: Path) -> dict:
    """
    Load STEP file with CadQuery and extract geometry facts needed for CAM.
    Returns bbox, estimated min feature size, hole diameters, face count.
    """
    try:
        import cadquery as cq
    except ImportError:
        return {"bbox": None, "min_feature_mm": 10.0, "holes": [], "volume_cm3": None}

    try:
        solid = cq.importers.importStep(str(step_path))
        bb = solid.val().BoundingBox()
        bbox = {
            "x_mm": round(bb.xlen, 2),
            "y_mm": round(bb.ylen, 2),
            "z_mm": round(bb.zlen, 2),
        }

        # Estimate min feature size as smallest bbox dimension ÷ 4
        min_feature = min(bb.xlen, bb.ylen, bb.zlen) / 4.0

        # Find circular edges (holes) — detect cylinders by radius
        holes = []
        try:
            for edge in solid.edges("%Circle").vals():
                r = edge.radius()
                dia = round(r * 2, 2)
                if 1.0 <= dia <= 30.0:
                    holes.append(dia)
            holes = sorted(set(round(h, 1) for h in holes))
        except Exception:
            holes = []

        # Volume estimate
        try:
            vol = solid.val().Volume() / 1000.0  # mm³ → cm³
        except Exception:
            vol = None

        return {
            "bbox": bbox,
            "min_feature_mm": round(min_feature, 2),
            "holes": holes,
            "volume_cm3": round(vol, 1) if vol else None,
        }

    except Exception as exc:
        return {"bbox": None, "min_feature_mm": 10.0, "holes": [], "volume_cm3": None, "error": str(exc)}


# ─── Tool selection ───────────────────────────────────────────────────────────

def select_tools(geom: dict, material: str, lib: dict) -> dict:
    """
    Select roughing endmill, finishing endmill, and drills from the library
    based on geometry and material.
    """
    endmills = lib["endmills"]
    min_feat = geom.get("min_feature_mm", 10.0)
    holes    = geom.get("holes", [])
    bbox     = geom.get("bbox") or {}
    max_dim  = max(bbox.get("x_mm", 50), bbox.get("y_mm", 50), bbox.get("z_mm", 20))

    # Roughing: largest tool whose diameter fits the part (≤ 40% of max dim AND ≤ min_feature)
    roughing = None
    for tool in sorted(endmills, key=lambda t: -t["dia_mm"]):
        if tool["dia_mm"] <= min_feat and tool["dia_mm"] <= max_dim * 0.4:
            roughing = tool
            break
    if roughing is None:
        # Fallback: smallest available endmill
        roughing = sorted(endmills, key=lambda t: t["dia_mm"])[0]

    # Finishing: smallest tool that is strictly smaller than roughing
    finishing = None
    rough_dia = roughing["dia_mm"]
    for tool in sorted(endmills, key=lambda t: t["dia_mm"]):
        if tool["dia_mm"] < rough_dia:
            finishing = tool
            break
    if finishing is None:
        finishing = roughing  # same tool if part is already very small

    # Drills: match hole diameters
    selected_drills = []
    for hole_dia in holes:
        best_drill = None
        for d in lib["drills"]:
            if abs(d["dia_mm"] - hole_dia) < 0.3:
                best_drill = d
                break
        if best_drill:
            selected_drills.append(best_drill)

    return {"roughing": roughing, "finishing": finishing, "drills": selected_drills}


# ─── Feed/speed calculation ───────────────────────────────────────────────────

def calc_feeds(tool: dict, mat_props: dict) -> dict:
    """
    Calculate RPM, feed rate, plunge rate from tool + material properties.
    Formula: RPM = (SFM × 3.82) / diameter_inches
    """
    sfm       = mat_props["sfm"]
    cf        = mat_props["chip_load_factor"]
    dia_mm    = tool["dia_mm"]
    flutes    = tool["flutes"]
    base_cl   = tool["chip_load_mm"]

    dia_in    = dia_mm / 25.4
    rpm       = int((sfm * 3.82) / dia_in)
    rpm       = min(rpm, 24000)  # cap at typical spindle max

    chip_load = base_cl * cf     # mm/tooth
    feed_mmpm = int(chip_load * flutes * rpm)
    plunge    = int(feed_mmpm * 0.25)

    doc_axial  = round(dia_mm * mat_props["axial_doc_factor"], 2)
    doc_radial = round(dia_mm * mat_props["radial_doc_factor"], 2)

    return {
        "rpm": rpm,
        "feed_mmpm": feed_mmpm,
        "plunge_mmpm": plunge,
        "doc_axial_mm": doc_axial,
        "doc_radial_mm": doc_radial,
    }


# ─── Fusion 360 script generation ────────────────────────────────────────────

_FUSION_SCRIPT_TEMPLATE = '''"""
Auto-generated Fusion 360 CAM script.
Generated by aria_os/cam_generator.py

Part:     {part_name}
Material: {material}
Bbox:     {bbox_str}

HOW TO USE:
  1. In Fusion 360, open the STEP file: {step_path}
  2. Go to Tools → Add-Ins → Scripts and Add-Ins
  3. Click + to add this script, then Run
  4. Toolpaths generate automatically. Review then post to gcode.
"""
import adsk.core
import adsk.fusion
import adsk.cam
import traceback

def run(context):
    ui = None
    try:
        app    = adsk.core.Application.get()
        ui     = app.userInterface
        design = adsk.fusion.Design.cast(app.activeProduct)

        # ── Switch to Manufacturing workspace ──────────────────────────────────
        camWs = ui.workspaces.itemById("CAMEnvironment")
        camWs.activate()

        cam = adsk.cam.CAM.cast(app.activeProduct)
        if cam is None:
            ui.messageBox("Could not get CAM object — ensure a part is open.")
            return

        # ── Create Setup ───────────────────────────────────────────────────────
        setups = cam.setups
        setupInput = setups.createInput(adsk.cam.OperationTypes.MillingOperation)

        # Setup orientation: top face up, origin at stock corner
        setupInput.stockMode = adsk.cam.SetupStockModes.FixedBoxStock
        boxStockInput = adsk.cam.FixedStockSizeInput.cast(setupInput.stock)
        boxStockInput.xOffset = adsk.core.ValueInput.createByReal({stock_offset_cm})
        boxStockInput.yOffset = adsk.core.ValueInput.createByReal({stock_offset_cm})
        boxStockInput.zOffset = adsk.core.ValueInput.createByReal({stock_z_offset_cm})

        setup = setups.add(setupInput)
        setup.name = "{part_name}_setup"

        # ── Build tool library entries ─────────────────────────────────────────
        toolLib = cam.documentToolLibrary

        def make_flat_endmill(name, dia_cm, flutes, rpm, feed_cmpm, plunge_cmpm):
            t = adsk.cam.ToolingData.createFlatMill()
            t.name          = name
            t.diameter      = adsk.core.ValueInput.createByReal(dia_cm)
            t.numberOfFlutes = flutes
            t.spindleSpeed  = adsk.core.ValueInput.createByReal(rpm)
            t.feedrate      = adsk.core.ValueInput.createByReal(feed_cmpm)
            t.plungeFeedrate = adsk.core.ValueInput.createByReal(plunge_cmpm)
            return toolLib.add(t)

        rough_tool    = make_flat_endmill(
            "{rough_name}", {rough_dia_cm}, {rough_flutes},
            {rough_rpm}, {rough_feed_cm}, {rough_plunge_cm}
        )
        finish_tool   = make_flat_endmill(
            "{finish_name}", {finish_dia_cm}, {finish_flutes},
            {finish_rpm}, {finish_feed_cm}, {finish_plunge_cm}
        )

        # ── Operation 1: 3D Adaptive Clearing (roughing) ──────────────────────
        adaptiveInput = setup.operations.createInput("adaptive")
        adaptiveInput.tool = rough_tool
        adaptiveInput.parameters.itemByName("optimalLoad").expression = "{rough_radial_cm}"
        adaptiveInput.parameters.itemByName("maximumStepdown").expression = "{rough_axial_cm}"
        adaptiveInput.parameters.itemByName("smoothingMode").expression = "true"
        adaptiveInput.parameters.itemByName("stockToLeave").expression = "0.03"  # 0.3mm stock
        adaptive_op = setup.operations.add(adaptiveInput)
        adaptive_op.name = "3D_Adaptive_Rough"

        # ── Operation 2: Parallel (scallop finishing) ──────────────────────────
        parallelInput = setup.operations.createInput("parallel")
        parallelInput.tool = finish_tool
        parallelInput.parameters.itemByName("stepover").expression = "{finish_stepover_cm}"
        parallelInput.parameters.itemByName("maximumStepdown").expression = "{finish_axial_cm}"
        parallelInput.parameters.itemByName("stockToLeave").expression = "0"
        parallel_op = setup.operations.add(parallelInput)
        parallel_op.name = "Parallel_Finish"

        # ── Operation 3: Contour (edge finishing) ─────────────────────────────
        contourInput = setup.operations.createInput("contour")
        contourInput.tool = finish_tool
        contourInput.parameters.itemByName("stockToLeave").expression = "0"
        contour_op = setup.operations.add(contourInput)
        contour_op.name = "Contour_Edges"

{drill_ops}

        # ── Generate and post toolpaths ────────────────────────────────────────
        # Generate all toolpaths
        cam.generateAllToolpaths(False)  # False = wait for completion

        # Post to gcode — uses Generic Milling post (swap for your machine post)
        postConfig  = cam.postConfigurations.itemByName("Generic Milling")
        outputFolder = r"{gcode_out_dir}"
        postInput = adsk.cam.PostOutputUnitOptions.DocumentUnitsOutput
        if postConfig:
            cam.postProcess(setup, postConfig, outputFolder, postInput, "{part_name}")
            ui.messageBox(f"CAM complete! Gcode saved to:\\n{gcode_out_dir}")
        else:
            ui.messageBox("Toolpaths generated. Select a post config to export gcode.")

    except Exception:
        if ui:
            ui.messageBox(f"CAM generation failed:\\n{{traceback.format_exc()}}")
'''

_DRILL_OP_TEMPLATE = '''
        # Drill: {name} dia={dia_mm}mm
        drillInput_{idx} = setup.operations.createInput("drill")
        drillInput_{idx}.parameters.itemByName("cycleType").expression = "\\\"chip_breaking\\\""
        drill_op_{idx} = setup.operations.add(drillInput_{idx})
        drill_op_{idx}.name = "Drill_{dia_mm}mm"
'''


def generate_cam_script(
    step_path: str | Path,
    material: str = "aluminium_6061",
    out_dir: Path | None = None,
) -> Path:
    """
    Analyze STEP geometry, select tools, compute feeds/speeds, write Fusion script.
    Returns path to the generated .py script.
    """
    step_path = Path(step_path)
    part_name = step_path.stem
    out_dir   = out_dir or (OUT_CAM / part_name)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[cam] Analyzing: {step_path.name}")

    # Load tool library
    with open(TOOL_LIB_PATH, encoding="utf-8") as fh:
        lib = json.load(fh)

    mat_props = lib["materials"].get(material, lib["materials"]["default"])

    # Analyze geometry
    geom = analyze_step(step_path)
    bbox = geom.get("bbox") or {"x_mm": 50, "y_mm": 50, "z_mm": 20}
    print(f"[cam] Bbox: {bbox['x_mm']} × {bbox['y_mm']} × {bbox['z_mm']} mm")
    print(f"[cam] Min feature: {geom['min_feature_mm']} mm  Holes: {geom['holes']}")

    # Select tools
    tools = select_tools(geom, material, lib)
    rough  = tools["roughing"]
    finish = tools["finishing"]
    drills = tools["drills"]

    # Compute feeds/speeds
    rf = calc_feeds(rough, mat_props)
    ff = calc_feeds(finish, mat_props)

    # Stock: bbox + 1mm per side, 1.5mm top
    stock_offset_cm = 0.1   # 1mm → cm
    stock_z_cm      = 0.15  # 1.5mm top stock

    # Drill operations block
    drill_ops_str = ""
    for idx, drill in enumerate(drills):
        drill_ops_str += _DRILL_OP_TEMPLATE.format(
            idx=idx,
            name=drill["name"],
            dia_mm=drill["dia_mm"],
        )

    def mm2cm(v): return round(v / 10.0, 4)

    bbox_str = f"{bbox['x_mm']} × {bbox['y_mm']} × {bbox['z_mm']} mm"
    gcode_dir = str(out_dir / "gcode").replace("\\", "\\\\")

    script = _FUSION_SCRIPT_TEMPLATE.format(
        part_name       = part_name,
        material        = material,
        bbox_str        = bbox_str,
        step_path       = str(step_path).replace("\\", "\\\\"),
        stock_offset_cm = stock_offset_cm,
        stock_z_offset_cm = stock_z_cm,

        rough_name      = rough["name"],
        rough_dia_cm    = mm2cm(rough["dia_mm"]),
        rough_flutes    = rough["flutes"],
        rough_rpm       = rf["rpm"],
        rough_feed_cm   = mm2cm(rf["feed_mmpm"]),
        rough_plunge_cm = mm2cm(rf["plunge_mmpm"]),
        rough_radial_cm = mm2cm(rf["doc_radial_mm"]),
        rough_axial_cm  = mm2cm(rf["doc_axial_mm"]),

        finish_name      = finish["name"],
        finish_dia_cm    = mm2cm(finish["dia_mm"]),
        finish_flutes    = finish["flutes"],
        finish_rpm       = ff["rpm"],
        finish_feed_cm   = mm2cm(ff["feed_mmpm"]),
        finish_plunge_cm = mm2cm(ff["plunge_mmpm"]),
        finish_stepover_cm = mm2cm(finish["dia_mm"] * 0.1),  # 10% stepover for finishing
        finish_axial_cm  = mm2cm(ff["doc_axial_mm"]),

        drill_ops       = drill_ops_str,
        gcode_out_dir   = gcode_dir,
    )

    out_script = out_dir / f"{part_name}_cam.py"
    out_script.write_text(script, encoding="utf-8")

    # ── Physics validation (feeds/speeds + power + deflection) ───────────────
    physics_result: dict = {}
    try:
        from .cam_physics import validate_feeds_speeds as _vfs
        tool_dia = rough["dia_mm"]
        physics_result = _vfs(
            tool_dia_mm=tool_dia,
            material=material,
            depth_of_cut_mm=tool_dia * 0.5,
            width_of_cut_mm=tool_dia * 0.3,
            overhang_mm=tool_dia * 3.0,
        )
        for w in physics_result.get("warnings", []):
            print(f"[CAM_PHYSICS] {w}")
    except Exception:
        pass

    # Write a human-readable summary alongside
    summary = {
        "part": part_name,
        "material": material,
        "bbox_mm": bbox,
        "min_feature_mm": geom["min_feature_mm"],
        "holes_mm": geom["holes"],
        "roughing_tool": rough["name"],
        "finishing_tool": finish["name"],
        "drills": [d["name"] for d in drills],
        "roughing_feeds": rf,
        "finishing_feeds": ff,
        "physics": {
            "surface_finish_ra_um": physics_result.get("surface_finish_ra_um"),
            "mrr_mm3_min": physics_result.get("mrr_mm3_min"),
            "required_power_w": physics_result.get("required_power_w"),
            "deflection_mm": physics_result.get("deflection_mm"),
            "passed": physics_result.get("passed"),
            "warnings": physics_result.get("warnings", []),
        } if physics_result else {},
    }
    (out_dir / f"{part_name}_cam_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )

    print(f"[cam] Roughing:  {rough['name']}  RPM={rf['rpm']}  feed={rf['feed_mmpm']}mm/min")
    print(f"[cam] Finishing: {finish['name']}  RPM={ff['rpm']}  feed={ff['feed_mmpm']}mm/min")
    if drills:
        print(f"[cam] Drills:    {', '.join(d['name'] for d in drills)}")
    print(f"[cam] Script:    {out_script}")
    print(f"[cam] Summary:   {out_dir / (part_name + '_cam_summary.json')}")

    # ── Run machinability check ───────────────────────────────────────────────
    result = {"script": out_script}
    try:
        from .cam_validator import run_machinability_check
        _mac = run_machinability_check(str(step_path), material)
        result["machinability"] = _mac
        if not _mac["passed"]:
            print(f"[CAM] Machinability warnings: {_mac['violations']}")
    except Exception:
        pass

    return out_script


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Auto-generate Fusion 360 CAM script from STEP file")
    parser.add_argument("step", type=Path, help="STEP file to analyze")
    parser.add_argument("--material", default="aluminium_6061",
                        help="Material key from tool_library.json (default: aluminium_6061)")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Output directory (default: outputs/cam/<part_name>/)")
    args = parser.parse_args()

    step = args.step if args.step.is_absolute() else ROOT / args.step
    generate_cam_script(step, material=args.material, out_dir=args.out_dir)
