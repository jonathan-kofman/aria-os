"""
aria_os/fusion_generator.py

Generates Fusion 360 Python API scripts (run inside Fusion 360 via
Utilities > Scripts and Add-Ins > Run Script).

Fusion 360 is the PRIMARY tool for:
  - Lattice / infill        (Design Extension: LatticeMeshFeature)
  - Generative design       (Design Extension: cloud topology optimization)
  - Sheet metal             (built-in SheetMetalFeatures)
  - Additive mfg prep       (Manufacturing Extension: build setup, supports)
  - CAM toolpath generation (Manufacturing Extension: milling/turning)
  - Multi-body assemblies   (motion constraints, contact sets)
  - FEA simulation setup    (Design Extension: SimulationStudy)
  - T-spline / organic      (SculptFeatures)

CadQuery handles: known ARIA templates, simple parametric solids, LRE nozzles
Grasshopper handles: the 6 core ARIA structural parts
Blender handles: mesh visualization / rendering ONLY
"""
import json
from pathlib import Path
from typing import Any, Optional

from ..cem_context import load_cem_geometry


# -------------------------------------------------------------------------
# Routing: decide which Fusion template to generate
# -------------------------------------------------------------------------

_LATTICE_KW     = {"lattice", "gyroid", "octet", "honeycomb", "infill",
                   "cellular", "volumetric", "gradient density",
                   "energy absorber", "lightweight fill", "tpms",
                   "conformal lattice", "arc weave"}
_SHEET_METAL_KW = {"sheet metal", "sheetmetal", "sheet-metal", "stamping",
                   "flat pattern", "bend", "flange", "enclosure panel",
                   "mounting plate", "bracket skin"}
_GENERATIVE_KW  = {"generative", "topology optim", "topopt",
                   "minimum weight", "lightest", "structural optim",
                   "generative design"}
_ADDITIVE_KW    = {"additive setup", "build prep", "print orientation",
                   "support generation", "am setup", "slm setup",
                   "dmls setup", "fdm setup"}
_SIMULATION_KW  = {"simulate", "fea", "stress analysis", "thermal sim",
                   "modal analysis", "frequency response", "buckling"}
_SCULPT_KW      = {"sculpt", "t-spline", "organic", "ergonomic grip",
                   "freeform surface", "soft body", "blend surface"}
_CAM_KW         = {"toolpath", "cam setup", "cnc program", "g-code",
                   "machining strategy", "multi-axis", "adaptive clearing",
                   "scallop height", "3+2", "5-axis"}


def _detect_mode(goal: str) -> str:
    g = goal.lower()
    for kw in _LATTICE_KW:
        if kw in g: return "lattice"
    for kw in _GENERATIVE_KW:
        if kw in g: return "generative"
    for kw in _SHEET_METAL_KW:
        if kw in g: return "sheet_metal"
    for kw in _ADDITIVE_KW:
        if kw in g: return "additive"
    for kw in _SIMULATION_KW:
        if kw in g: return "simulation"
    for kw in _SCULPT_KW:
        if kw in g: return "sculpt"
    for kw in _CAM_KW:
        if kw in g: return "cam"
    return "parametric"


def _ep(p: str) -> str:
    """Escape path for Fusion script strings."""
    return p.replace("\\", "/").replace("\\", "/")


# -------------------------------------------------------------------------
# Per-mode script generators
# -------------------------------------------------------------------------

def _script_lattice(plan: dict, goal: str, stl_path: str,
                    step_path: str, cem: dict) -> str:
    p         = plan.get("params", {})
    width     = float(p.get("width_mm",  p.get("od_mm",  100.0)))
    height    = float(p.get("height_mm", p.get("length_mm", 100.0)))
    depth     = float(p.get("depth_mm",  p.get("thickness_mm", 20.0)))
    cell_size = float(p.get("cell_size_mm", 8.0))
    wall      = float(p.get("wall_mm", 1.5))
    pattern   = str(p.get("pattern", "honeycomb")).lower()
    part_name = (plan.get("part_id") or "fusion_lattice").replace("/", "_")
    ct_map = {
        "honeycomb":   "adsk.fusion.LatticeCellTypes.SolidLatticeCellType",
        "octet":       "adsk.fusion.LatticeCellTypes.SolidLatticeCellType",
        "octet_truss": "adsk.fusion.LatticeCellTypes.SolidLatticeCellType",
        "gyroid":      "adsk.fusion.LatticeCellTypes.SurfaceLatticeCellType",
        "tpms":        "adsk.fusion.LatticeCellTypes.SurfaceLatticeCellType",
        "arc_weave":   "adsk.fusion.LatticeCellTypes.RuledSurfaceLatticeCellType",
    }
    ct  = ct_map.get(pattern, "adsk.fusion.LatticeCellTypes.SolidLatticeCellType")
    se  = stl_path.replace("\\", "/")
    ste = step_path.replace("\\", "/")
    lines = [
        "# ARIA-OS Fusion 360 Lattice Script",
        "# Design Extension required: LatticeMeshFeature",
        "# Run: Utilities > Scripts and Add-Ins > Run Script",
        "import adsk.core, adsk.fusion, traceback",
        "",
        f"WIDTH_MM={width}; HEIGHT_MM={height}; DEPTH_MM={depth}",
        f"CELL_SIZE_MM={cell_size}; WALL_MM={wall}",
        f"CELL_TYPE={ct}",
        f'PART_NAME="{part_name}"',
        f'STL_PATH=r"{se}"; STEP_PATH=r"{ste}"',
        "",
        "def run(context):",
        "    ui = None",
        "    try:",
        "        app    = adsk.core.Application.get(); ui = app.userInterface",
        "        design = adsk.fusion.Design.cast(app.activeProduct)",
        "        design.designType = adsk.fusion.DesignTypes.DirectDesignType",
        "        root   = design.rootComponent",
        "        sk = root.sketches.add(root.xYConstructionPlane)",
        "        sk.sketchCurves.sketchLines.addTwoPointRectangle(",
        "            adsk.core.Point3D.create(0, 0, 0),",
        "            adsk.core.Point3D.create(WIDTH_MM/10, HEIGHT_MM/10, 0))",
        "        ei = root.features.extrudeFeatures.createInput(",
        "            sk.profiles.item(0),",
        "            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)",
        "        ei.setDistanceExtent(False, adsk.core.ValueInput.createByReal(DEPTH_MM/10.0))",
        "        body = root.features.extrudeFeatures.add(ei).bodies.item(0)",
        "        body.name = PART_NAME",
        "        lat = root.features.latticeMeshFeatures",
        "        li  = lat.createInput(body)",
        "        li.latticeCellType = CELL_TYPE",
        "        li.cellSize        = adsk.core.ValueInput.createByReal(CELL_SIZE_MM/10.0)",
        "        li.beamDiameter    = adsk.core.ValueInput.createByReal(WALL_MM/10.0)",
        "        lat.add(li)",
        "        mgr = design.exportManager",
        "        mgr.execute(mgr.createSTEPExportOptions(STEP_PATH))",
        "        so = mgr.createSTLExportOptions(root)",
        "        so.filename = STL_PATH",
        "        so.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementMedium",
        "        mgr.execute(so)",
        "        ui.messageBox(f'Lattice done  cell={CELL_SIZE_MM}mm\\nSTEP: {STEP_PATH}')",
        "    except Exception:",
        "        if ui: ui.messageBox('Lattice failed:\\n' + traceback.format_exc())",
    ]
    return "\n".join(lines) + "\n"


def _script_generative(plan: dict, goal: str, stl_path: str,
                        step_path: str, cem: dict) -> str:
    p         = plan.get("params", {})
    mass_tgt  = float(p.get("target_mass_kg", 0.5))
    sf_min    = float(p.get("safety_factor", 2.5))
    material  = str(p.get("material", "AlSi10Mg"))
    part_name = (plan.get("part_id") or "gen_design").replace("/", "_")
    ste       = step_path.replace("\\", "/")
    lines = [
        "# ARIA-OS Fusion 360 Generative Design Setup",
        "# Requires: Fusion 360 Design Extension + cloud credits",
        "# Run: Utilities > Scripts and Add-Ins > Run Script",
        "import adsk.core, adsk.fusion, traceback",
        "",
        f"PART_NAME={repr(part_name)}; TARGET_MASS_KG={mass_tgt}; SAFETY_FACTOR={sf_min}",
        f"MATERIAL={repr(material)}; STEP_PATH=r{repr(ste)}",
        "",
        "def run(context):",
        "    ui = None",
        "    try:",
        "        app = adsk.core.Application.get(); ui = app.userInterface",
        f"        ui.messageBox(",
        f"            'Generative Design Study\\n\\n'",
        f"            f'Part         : {part_name}\\n'",
        f"            f'Target mass  : {mass_tgt} kg\\n'",
        f"            f'Safety factor: {sf_min}\\n'",
        f"            f'Material     : {material}\\n\\n'",
        "            'Next steps:\\n'",
        "            '1. Switch to Generative Design workspace\\n'",
        "            '2. Define Preserve (keep) + Obstacle (no-go) bodies\\n'",
        "            '3. Add structural loads + fixed constraints\\n'",
        "            '4. Study Settings: mass target + safety factor\\n'",
        "            '5. Solve All Outcomes (cloud credits required)\\n'",
        "            '6. Select best outcome > export STEP'",
        "        )",
        "    except Exception:",
        "        if ui: ui.messageBox('Setup failed:\\n' + traceback.format_exc())",
    ]
    return "\n".join(lines) + "\n"


def _script_sheet_metal(plan: dict, goal: str, stl_path: str,
                         step_path: str, cem: dict) -> str:
    p         = plan.get("params", {})
    width     = float(p.get("width_mm",  150.0))
    height    = float(p.get("height_mm", 100.0))
    thick     = float(p.get("thickness_mm", 2.0))
    bend_r    = round(thick * 1.5, 2)
    part_name = (plan.get("part_id") or "sheet_metal_part").replace("/", "_")
    se        = stl_path.replace("\\", "/")
    ste       = step_path.replace("\\", "/")
    lines = [
        "# ARIA-OS Fusion 360 Sheet Metal (no extension needed)",
        "# Run: Utilities > Scripts and Add-Ins > Run Script",
        "import adsk.core, adsk.fusion, traceback",
        "",
        f"WIDTH_MM={width}; HEIGHT_MM={height}; THICK_MM={thick}; BEND_RAD_MM={bend_r}",
        "K_FACTOR=0.44",
        f"PART_NAME={repr(part_name)}",
        f"STL_PATH=r{repr(se)}; STEP_PATH=r{repr(ste)}",
        "",
        "def run(context):",
        "    ui = None",
        "    try:",
        "        app    = adsk.core.Application.get(); ui = app.userInterface",
        "        design = adsk.fusion.Design.cast(app.activeProduct)",
        "        design.designType = adsk.fusion.DesignTypes.DirectDesignType",
        "        root   = design.rootComponent",
        "        rule   = design.sheetMetalRules.add()",
        "        rule.name='ARIA_SM'",
        "        rule.thickness  = adsk.core.ValueInput.createByReal(THICK_MM/10)",
        "        rule.bendRadius = adsk.core.ValueInput.createByReal(BEND_RAD_MM/10)",
        "        rule.kFactor    = K_FACTOR",
        "        sk = root.sketches.add(root.xYConstructionPlane)",
        "        sk.sketchCurves.sketchLines.addTwoPointRectangle(",
        "            adsk.core.Point3D.create(0,0,0),",
        "            adsk.core.Point3D.create(WIDTH_MM/10, HEIGHT_MM/10, 0))",
        "        fi = root.features.sheetMetalFeatures.createSheetMetalFlangeFeatureInput(",
        "            sk.profiles.item(0), rule)",
        "        fi.isChain = False",
        "        root.features.sheetMetalFeatures.addSheetMetalFlange(fi)",
        "        root.features.flatPatternFeatures.add(root.bRepBodies.item(0))",
        "        mgr = design.exportManager",
        "        mgr.execute(mgr.createSTEPExportOptions(STEP_PATH))",
        "        so = mgr.createSTLExportOptions(root); so.filename=STL_PATH; mgr.execute(so)",
        "        ui.messageBox(f'Sheet metal done  {THICK_MM}mm  bend R={BEND_RAD_MM}mm\\nSTEP: {STEP_PATH}')",
        "    except Exception:",
        "        if ui: ui.messageBox('Sheet metal failed:\\n' + traceback.format_exc())",
    ]
    return "\n".join(lines) + "\n"


def _script_additive(plan: dict, goal: str, stl_path: str,
                      step_path: str, cem: dict) -> str:
    p         = plan.get("params", {})
    part_name = (plan.get("part_id") or "additive_part").replace("/", "_")
    material  = str(p.get("material", "AlSi10Mg (DMLS)"))
    process   = str(p.get("process",  "DMLS"))
    layer_um  = int(p.get("layer_thickness_um", 30))
    se        = stl_path.replace("\\", "/")
    ste       = step_path.replace("\\", "/")
    lines = [
        "# ARIA-OS Fusion 360 Additive Setup (Manufacturing Extension required)",
        "# Run: Utilities > Scripts and Add-Ins > Run Script",
        "import adsk.core, adsk.fusion, traceback",
        "",
        f"PART_NAME={repr(part_name)}; MATERIAL={repr(material)}; PROCESS={repr(process)}",
        f"LAYER_UM={layer_um}; STL_PATH=r{repr(se)}; STEP_PATH=r{repr(ste)}",
        "",
        "def run(context):",
        "    ui = None",
        "    try:",
        "        app    = adsk.core.Application.get(); ui = app.userInterface",
        "        design = adsk.fusion.Design.cast(app.activeProduct)",
        "        root   = design.rootComponent",
        "        mgr    = design.exportManager",
        "        mgr.execute(mgr.createSTEPExportOptions(STEP_PATH))",
        "        so = mgr.createSTLExportOptions(root)",
        "        so.filename = STL_PATH",
        "        so.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh",
        "        mgr.execute(so)",
        "        ui.messageBox(",
        "            f'Additive export done\\nProcess={PROCESS}  Material={MATERIAL}  Layer={LAYER_UM}um\\n'",
        "            f'STL: {STL_PATH}\\n\\nNext in Manufacture > Additive:\\n'",
        "            '1. New Setup > select machine  2. Build orientation\\n'",
        "            '3. Generate supports  4. Simulate build  5. Post-process'",
        "        )",
        "    except Exception:",
        "        if ui: ui.messageBox('Additive failed:\\n' + traceback.format_exc())",
    ]
    return "\n".join(lines) + "\n"


def _script_cam(plan: dict, goal: str, stl_path: str,
                step_path: str, cem: dict) -> str:
    p         = plan.get("params", {})
    part_name = (plan.get("part_id") or "machined_part").replace("/", "_")
    material  = str(p.get("material", "6061 Al"))
    ste       = step_path.replace("\\", "/")
    lines = [
        "# ARIA-OS Fusion 360 CAM Setup (Manufacturing Extension required)",
        "# Run: Utilities > Scripts and Add-Ins > Run Script",
        "import adsk.core, adsk.fusion, adsk.cam, traceback",
        "",
        f"PART_NAME={repr(part_name)}; MATERIAL={repr(material)}; STEP_PATH=r{repr(ste)}",
        "",
        "def run(context):",
        "    ui = None",
        "    try:",
        "        app = adsk.core.Application.get(); ui = app.userInterface",
        "        cam = adsk.cam.CAM.cast(app.activeProduct)",
        "        if not cam:",
        "            ui.messageBox('Switch to Manufacture workspace first'); return",
        "        si = cam.setups.createInput(adsk.cam.OperationTypes.MillingType)",
        "        si.name = f'ARIA_{PART_NAME}'",
        "        cam.setups.add(si)",
        "        ui.messageBox(",
        "            f'CAM setup created  {PART_NAME} / {MATERIAL}\\n\\n'",
        "            'Next:\\n1. Stock size  2. Add ops (Adaptive/Contour/Bore)\\n'",
        "            '3. Select tools  4. Simulate  5. Post-process to G-code'",
        "        )",
        "    except Exception:",
        "        if ui: ui.messageBox('CAM failed:\\n' + traceback.format_exc())",
    ]
    return "\n".join(lines) + "\n"


def _script_parametric(plan: dict, goal: str, stl_path: str,
                        step_path: str, cem: dict) -> str:
    p         = plan.get("params", {})
    part_name = (plan.get("part_id") or "aria_part").replace("/", "_")
    od        = float(p.get("od_mm", p.get("width_mm", 100.0)))
    height    = float(p.get("height_mm", p.get("length_mm", 50.0)))
    bore      = float(p.get("bore_mm", od * 0.3))
    se        = stl_path.replace("\\", "/")
    ste       = step_path.replace("\\", "/")
    lines = [
        "# ARIA-OS Fusion 360 Parametric",
        "# Run: Utilities > Scripts and Add-Ins > Run Script",
        "import adsk.core, adsk.fusion, traceback",
        "",
        f"OD_MM={od}; HEIGHT_MM={height}; BORE_MM={bore}",
        f"PART_NAME={repr(part_name)}; STL_PATH=r{repr(se)}; STEP_PATH=r{repr(ste)}",
        "",
        "def run(context):",
        "    ui = None",
        "    try:",
        "        app    = adsk.core.Application.get(); ui = app.userInterface",
        "        design = adsk.fusion.Design.cast(app.activeProduct)",
        "        design.designType = adsk.fusion.DesignTypes.DirectDesignType",
        "        root   = design.rootComponent",
        "        sk = root.sketches.add(root.xYConstructionPlane)",
        "        sk.sketchCurves.sketchCircles.addByCenterRadius(",
        "            adsk.core.Point3D.create(0,0,0), OD_MM/20.0)",
        "        ei = root.features.extrudeFeatures.createInput(",
        "            sk.profiles.item(0),",
        "            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)",
        "        ei.setDistanceExtent(False, adsk.core.ValueInput.createByReal(HEIGHT_MM/10.0))",
        "        root.features.extrudeFeatures.add(ei).bodies.item(0).name = PART_NAME",
        "        if BORE_MM > 0:",
        "            sk2 = root.sketches.add(root.xYConstructionPlane)",
        "            sk2.sketchCurves.sketchCircles.addByCenterRadius(",
        "                adsk.core.Point3D.create(0,0,0), BORE_MM/20.0)",
        "            ci = root.features.extrudeFeatures.createInput(",
        "                sk2.profiles.item(0),",
        "                adsk.fusion.FeatureOperations.CutFeatureOperation)",
        "            ci.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)",
        "            root.features.extrudeFeatures.add(ci)",
        "        mgr = design.exportManager",
        "        mgr.execute(mgr.createSTEPExportOptions(STEP_PATH))",
        "        so = mgr.createSTLExportOptions(root); so.filename=STL_PATH; mgr.execute(so)",
        "        ui.messageBox(f'{PART_NAME} done\\nSTEP: {STEP_PATH}')",
        "    except Exception:",
        "        if ui: ui.messageBox('Fusion failed:\\n' + traceback.format_exc())",
    ]
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

_SCRIPT_MAP: dict = {
    "lattice":     _script_lattice,
    "generative":  _script_generative,
    "sheet_metal": _script_sheet_metal,
    "additive":    _script_additive,
    "cam":         _script_cam,
    "parametric":  _script_parametric,
    "sculpt":      _script_parametric,
    "simulation":  _script_parametric,
}


def generate_fusion_script(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> str:
    cem  = load_cem_geometry(repo_root)
    mode = _detect_mode(goal)
    return _SCRIPT_MAP.get(mode, _script_parametric)(plan, goal, stl_path, step_path, cem)


def write_fusion_artifacts(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    part_slug   = (plan.get("part_id") or "aria_part").replace("/", "_")
    out_dir     = repo_root / "outputs" / "cad" / "fusion_scripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    mode        = _detect_mode(goal)
    script_path = out_dir / f"{part_slug}.py"
    params_path = out_dir / f"{part_slug}.json"
    script = generate_fusion_script(plan, goal, step_path, stl_path, repo_root=repo_root)
    script_path.write_text(script, encoding="utf-8")
    params_path.write_text(
        json.dumps({
            "goal": goal, "part_id": plan.get("part_id", ""),
            "fusion_mode": mode, "step_path": step_path, "stl_path": stl_path,
            "features": plan.get("features", []),
            "base_shape": plan.get("base_shape", {}),
            "params": plan.get("params", {}),
        }, indent=2), encoding="utf-8",
    )
    return {
        "script_path": str(script_path),
        "params_path": str(params_path),
        "fusion_mode": mode,
    }
