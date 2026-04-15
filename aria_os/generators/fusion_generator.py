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


_FUSION_LLM_SYSTEM = """\
You are an expert Autodesk Fusion 360 Python API programmer. Generate a complete
Fusion 360 script that creates geometry using the Fusion API.

CRITICAL: This script runs INSIDE Fusion 360 via exec(). Do NOT define run(context).
The variables 'app', 'ui', 'design', 'rootComp', 'math', 'adsk' are pre-defined.
Design is already set to PARAMETRIC mode — every feature will appear in the timeline.
Do NOT set designType. Do NOT close the document. Do NOT call app.documents.add().

Fusion API patterns:
  # Sketch + extrude
  sk = rootComp.sketches.add(rootComp.xYConstructionPlane)
  circles = sk.sketchCurves.sketchCircles
  circles.addByCenterRadius(adsk.core.Point3D.create(0,0,0), RADIUS_CM)
  ext_input = rootComp.features.extrudeFeatures.createInput(sk.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
  ext_input.setDistanceExtent(False, adsk.core.ValueInput.createByReal(HEIGHT_CM))
  rootComp.features.extrudeFeatures.add(ext_input)

  # Revolve
  rev_input = rootComp.features.revolveFeatures.createInput(profile, axis, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
  rev_input.setAngleExtent(False, adsk.core.ValueInput.createByReal(math.pi * 2))
  rootComp.features.revolveFeatures.add(rev_input)

  # Sweep
  sweep_input = rootComp.features.sweepFeatures.createInput(profile, path, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
  rootComp.features.sweepFeatures.add(sweep_input)

  # Loft
  loft_input = rootComp.features.loftFeatures.createInput(adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
  loft_input.loftSections.add(profile1)
  loft_input.loftSections.add(profile2)
  rootComp.features.loftFeatures.add(loft_input)

  # Circular pattern
  pat_input = rootComp.features.circularPatternFeatures.createInput(objects, axis)
  pat_input.quantity = adsk.core.ValueInput.createByReal(N)
  rootComp.features.circularPatternFeatures.add(pat_input)

  # Boolean combine
  combine_input = rootComp.features.combineFeatures.createInput(target_body, tool_bodies)
  combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation  # or JoinFeatureOperation
  rootComp.features.combineFeatures.add(combine_input)

  # Fillet
  fillet_input = rootComp.features.filletFeatures.createInput()
  fillet_input.addConstantRadiusEdgeSet(edges, adsk.core.ValueInput.createByReal(R_CM), True)
  rootComp.features.filletFeatures.add(fillet_input)

CRITICAL PATTERNS THAT WORK (copy these exactly for internal features):

  # BOLT HOLES on a flange (6 holes on PCD):
  bolt_sk = rootComp.sketches.add(rootComp.xYConstructionPlane)
  bolt_sk.sketchCurves.sketchCircles.addByCenterRadius(
      adsk.core.Point3D.create(PCD_CM/2, 0, 0), BOLT_R_CM)
  bolt_prof = bolt_sk.profiles.item(0)
  bolt_ext = rootComp.features.extrudeFeatures.createInput(
      bolt_prof, adsk.fusion.FeatureOperations.CutFeatureOperation)
  bolt_ext.setAllExtent(adsk.fusion.ExtentDirections.PositiveExtentDirection)
  bolt_feat = rootComp.features.extrudeFeatures.add(bolt_ext)
  # Circular pattern the bolt hole
  pat_bodies = adsk.core.ObjectCollection.create()
  pat_bodies.add(bolt_feat)
  z_axis = rootComp.zConstructionAxis
  pat_input = rootComp.features.circularPatternFeatures.createInput(pat_bodies, z_axis)
  pat_input.quantity = adsk.core.ValueInput.createByReal(6)
  rootComp.features.circularPatternFeatures.add(pat_input)

  # INTERNAL RIB inside a shelled body (create as separate body then join):
  rib_plane = rootComp.xZConstructionPlane  # or yZConstructionPlane
  rib_sk = rootComp.sketches.add(rib_plane)
  rib_sk.sketchCurves.sketchLines.addTwoPointRectangle(
      adsk.core.Point3D.create(INNER_R_CM, 0, 0),
      adsk.core.Point3D.create(OUTER_R_CM, RIB_HEIGHT_CM, 0))
  rib_ext = rootComp.features.extrudeFeatures.createInput(
      rib_sk.profiles.item(0),
      adsk.fusion.FeatureOperations.NewBodyFeatureOperation)  # NOT Cut!
  rib_ext.setSymmetricExtent(adsk.core.ValueInput.createByReal(RIB_THICK_CM/2), True)
  rib_body = rootComp.features.extrudeFeatures.add(rib_ext).bodies.item(0)
  # Join rib to main body
  main_body = rootComp.bRepBodies.item(0)
  combine_input = rootComp.features.combineFeatures.createInput(main_body, adsk.core.ObjectCollection.create())
  combine_input.toolBodies.add(rib_body)
  combine_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
  rootComp.features.combineFeatures.add(combine_input)

  # OUTLET PIPE from top face (extrude UP from top):
  top_face = None
  for face in rootComp.bRepBodies.item(0).faces:
      normal = face.geometry.normal if hasattr(face.geometry, 'normal') else None
      if normal and abs(normal.z - 1.0) < 0.01:
          bb = face.boundingBox
          if bb.maxPoint.z > top_face_z if top_face else True:
              top_face = face
              top_face_z = bb.maxPoint.z
  pipe_sk = rootComp.sketches.add(top_face)
  pipe_sk.sketchCurves.sketchCircles.addByCenterRadius(
      adsk.core.Point3D.create(0, 0, 0), PIPE_R_CM)
  pipe_ext = rootComp.features.extrudeFeatures.createInput(
      pipe_sk.profiles.item(0),
      adsk.fusion.FeatureOperations.JoinFeatureOperation)
  pipe_ext.setDistanceExtent(False, adsk.core.ValueInput.createByReal(PIPE_H_CM))
  rootComp.features.extrudeFeatures.add(pipe_ext)

RULES:
- ALL dimensions in CM (Fusion internal units). Convert mm to cm: divide by 10.
- Use rootComp (pre-defined), not root.
- Do NOT define run(context) — the code runs via exec() directly.
- Do NOT import adsk — it's pre-defined.
- Do NOT call app.documents.add() — document is already created.
- Use math (pre-defined) for trig.
- For complex parts: build features step by step. Each sketch → extrude/revolve/sweep → combine.
- For blades/vanes: create one blade via sweep along a path, then circular pattern.

Output ONLY Python code. No markdown fences.
"""


def _script_llm_fusion(plan: dict, goal: str, stl_path: str, step_path: str, cem: dict) -> str:
    """Generate Fusion script via LLM for complex parts without a template."""
    from ..llm_client import call_llm

    params = plan.get("params", {})
    param_str = ", ".join(f"{k}={v}" for k, v in params.items()) if params else "none"

    user_prompt = (
        f"Create Fusion 360 geometry for: {goal}\n"
        f"Parameters: {param_str}\n"
        f"Use sweep for curved features, revolve for axisymmetric parts, "
        f"circular pattern for repeated features.\n"
        f"Remember: all dimensions in CM (divide mm by 10)."
    )

    response = call_llm(user_prompt, system=_FUSION_LLM_SYSTEM)
    if not response:
        # Fall back to parametric template
        return _script_parametric(plan, goal, stl_path, step_path, cem)

    code = response.strip()
    # Strip markdown fences and any preamble text
    if "```" in code:
        # Extract content between first ``` and last ```
        import re
        match = re.search(r"```(?:python)?\s*\n(.*?)```", code, re.DOTALL)
        if match:
            code = match.group(1).strip()
    elif not code.startswith(("import ", "#", "app", "design", "root")):
        # Response starts with explanation text — find where Python code begins
        lines = code.split("\n")
        start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("import ", "#", "app ", "design ", "root", "sk ", "hub", "OD", "HEIGHT")):
                start = i
                break
        code = "\n".join(lines[start:])

    return code


def generate_fusion_script(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> str:
    cem = load_cem_geometry(repo_root)
    mode = _detect_mode(goal)

    # For modes with templates, use them
    if mode in _SCRIPT_MAP and mode != "parametric":
        return _SCRIPT_MAP[mode](plan, goal, stl_path, step_path, cem)

    # For parametric/sculpt/simulation: use LLM if goal is complex
    goal_lower = goal.lower()
    complex_keywords = ["blade", "impeller", "sweep", "loft", "curved", "spiral",
                        "helix", "turbine", "propeller", "vane", "involute", "cam"]
    if any(kw in goal_lower for kw in complex_keywords):
        try:
            return _script_llm_fusion(plan, goal, stl_path, step_path, cem)
        except Exception:
            pass

    return _script_parametric(plan, goal, stl_path, step_path, cem)


def write_fusion_artifacts(
    plan: dict[str, Any],
    goal: str,
    step_path: str,
    stl_path: str,
    repo_root: Optional[Path] = None,
) -> dict[str, str]:
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent.parent
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
    # Submit job to Fusion bridge (if running) for automatic execution
    fusion_jobs_dir = repo_root / "outputs" / "cad" / "fusion_jobs"
    fusion_jobs_dir.mkdir(parents=True, exist_ok=True)
    job_data = {
        "part_id": plan.get("part_id", ""),
        "goal": goal,
        "script": script,
        "step_path": str(Path(step_path).resolve()).replace("\\", "/"),
        "stl_path": str(Path(stl_path).resolve()).replace("\\", "/"),
        "fusion_mode": mode,
    }
    job_path = fusion_jobs_dir / f"{part_slug}.json"
    job_path.write_text(json.dumps(job_data, indent=2), encoding="utf-8")

    # Poll for result (Fusion bridge picks up the job)
    import time
    from ..llm_client import call_llm
    import re as _re

    _MAX_FUSION_RETRIES = 3

    for _fusion_attempt in range(_MAX_FUSION_RETRIES):
        result_done = fusion_jobs_dir / f"_done_{part_slug}.json"
        result_err = fusion_jobs_dir / f"_err_{part_slug}.json"
        timeout = 120
        t0 = time.time()
        fusion_error = None

        while time.time() - t0 < timeout:
            if result_done.exists():
                result_data = json.loads(result_done.read_text(encoding="utf-8"))
                step_size = result_data.get('step_size', 0)
                stl_size = result_data.get('stl_size', 0)
                print(f"[FUSION] Job completed: STEP {step_size / 1024:.0f} KB, STL {stl_size / 1024:.0f} KB")

                # Visual verification
                _stl = result_data.get("stl_path", stl_path)
                if Path(_stl).exists():
                    try:
                        from ..visual_verifier import verify_visual
                        vis = verify_visual(
                            result_data.get("step_path", step_path), _stl,
                            goal, plan.get("params", {}), repo_root=repo_root,
                        )
                        conf = vis.get("confidence", 0)
                        if vis.get("verified") and conf >= 0.90:
                            print(f"[FUSION] Visual verification PASS ({conf:.0%})")
                        else:
                            issues = vis.get("issues", [])
                            print(f"[FUSION] Visual verification: {conf:.0%} confidence")
                            for iss in issues[:3]:
                                print(f"  [VISUAL] {iss}")
                    except Exception as ve:
                        print(f"[FUSION] Visual verify skipped: {ve}")

                return {
                    "script_path": str(script_path),
                    "params_path": str(params_path),
                    "fusion_mode": mode,
                    "step_path": result_data.get("step_path", ""),
                    "stl_path": result_data.get("stl_path", ""),
                }

            if result_err.exists():
                err_data = json.loads(result_err.read_text(encoding="utf-8"))
                fusion_error = err_data.get("error", "unknown")
                print(f"[FUSION] Job failed (attempt {_fusion_attempt + 1}): {fusion_error[:150]}")
                # Clean up error file for retry
                try:
                    result_err.unlink()
                except Exception:
                    pass
                break
            time.sleep(2)
        else:
            print(f"[FUSION] Job timed out after {timeout}s.")
            break

        # Retry: feed error back to LLM and generate new script
        if fusion_error and _fusion_attempt < _MAX_FUSION_RETRIES - 1:
            print(f"[FUSION] Regenerating script with error feedback...")
            retry_prompt = (
                f"The previous Fusion 360 script failed with this error:\n{fusion_error[:500]}\n\n"
                f"Fix the script for: {goal}\n"
                f"Common fixes: use NewBodyFeatureOperation instead of CutFeatureOperation for features "
                f"outside the body. Ensure sketches are on the correct face/plane. "
                f"Use construction planes for offset sketches.\n"
                f"All dimensions in CM. Do NOT set designType. Do NOT close document."
            )
            try:
                response = call_llm(retry_prompt, system=_FUSION_LLM_SYSTEM)
                if response:
                    new_code = response.strip()
                    if "```" in new_code:
                        match = _re.search(r"```(?:python)?\s*\n(.*?)```", new_code, _re.DOTALL)
                        if match:
                            new_code = match.group(1).strip()
                    elif not new_code.startswith(("import ", "#")):
                        for i, line in enumerate(new_code.split("\n")):
                            if line.strip().startswith(("import ", "#")):
                                new_code = "\n".join(new_code.split("\n")[i:])
                                break
                    new_code = _re.sub(r".*designType.*\n", "", new_code)
                    new_code = _re.sub(r".*doc\.close.*\n", "", new_code)
                    new_code = _re.sub(r".*documents\.add.*\n", "", new_code)
                    compile(new_code, "<retry>", "exec")
                    # Update script and resubmit
                    script_path.write_text(new_code, encoding="utf-8")
                    job_data["script"] = new_code
                    job_path.write_text(json.dumps(job_data, indent=2), encoding="utf-8")
                    print(f"[FUSION] Retry script submitted ({len(new_code.splitlines())} lines)")
                    continue
            except Exception as retry_exc:
                print(f"[FUSION] Retry generation failed: {retry_exc}")
                break

    print(f"[FUSION] All attempts exhausted. Script saved for manual execution.")

    return {
        "script_path": str(script_path),
        "params_path": str(params_path),
        "fusion_mode": mode,
    }
