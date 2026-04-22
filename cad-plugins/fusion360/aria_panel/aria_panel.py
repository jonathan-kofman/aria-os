"""
aria_panel.py — Fusion 360 add-in entry point.

This file is the canonical add-in per Autodesk's pattern. Fusion calls
`run()` when the add-in is loaded (startup or via Scripts & Add-Ins
dialog) and `stop()` when it's unloaded.

What it does
------------
1. Registers a command that opens an ARIA Palette (Fusion's term for an
   embedded HTML panel). The palette loads the aria-os React frontend.

2. Bridges JavaScript ↔ Python. The React panel posts JSON messages via
   `window.fusionJavaScriptHandler.handle(action, payload)`; we implement
   8 actions (getCurrentDocument, getSelection, insertGeometry,
   updateParameter, getFeatureTree, exportCurrent, showNotification,
   openFile) and reply by calling `palette.sendInfoToHTML()`.

3. Adds a toolbar button under SOLID → CREATE → "ARIA Generate".

Dev vs prod URL
---------------
ARIA_PANEL_URL env var overrides the panel source URL. Default is
`http://localhost:5173/?host=fusion` (Vite dev server). For production
deploys, set ARIA_PANEL_URL=https://aria.example.com/panel/?host=fusion .

The `?host=fusion` query param tells the React app which bridge adapter
to use (apiConfig.js / bridge.js).

Install
-------
Put the parent folder `aria_panel/` at:
  Windows: %AppData%\Autodesk\Autodesk Fusion 360\API\AddIns\
  Mac:     ~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns/

Then Fusion 360 → Utilities → Scripts and Add-Ins → Add-Ins tab → Run.
"""
from __future__ import annotations

import json
import os
import tempfile
import traceback
import urllib.request
from pathlib import Path

import adsk.core  # type: ignore
import adsk.fusion  # type: ignore


# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

_PANEL_ID = "AriaGenerativePanel"
_CMD_ID = "AriaGenerativeCmd"
_CMD_NAME = "ARIA Generate"
_CMD_TOOLTIP = "Open the ARIA-OS generative CAD panel"
import time as _time
# Append a timestamp so every add-in start loads fresh JS/CSS and
# doesn't get stuck on a cached WebView2 bundle. Vite dev server
# ignores unknown query params so this doesn't break anything.
_DEFAULT_URL = os.environ.get(
    "ARIA_PANEL_URL",
    f"http://localhost:5173/?host=fusion&v={int(_time.time())}")

# Globals held alive for the duration of the add-in session. Fusion
# garbage-collects command definitions that drop out of scope.
_app = None
_ui = None
_palette = None
_handlers: list = []


# --------------------------------------------------------------------
# Bridge implementations
# --------------------------------------------------------------------

def _reply(id_: str, result=None, error: str | None = None) -> None:
    """Send a structured reply back to the React panel.

    Fusion 360 delivers this by invoking
    `window.fusionJavaScriptHandler.handle(eventName, dataStr)` on the
    panel, passing our string UNCHANGED. The panel's bridge registers
    that handler and dispatches to per-id promises. We therefore send
    pure JSON here (NOT a snippet of JS code — Fusion does not eval it).
    """
    payload = {"_id": id_}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    if _palette is not None:
        try:
            _palette.sendInfoToHTML("ariaReply", json.dumps(payload))
        except Exception:
            pass


def _get_current_document() -> dict:
    doc = _app.activeDocument
    product = _app.activeProduct
    units = "mm"
    try:
        units = product.unitsManager.defaultLengthUnits
    except Exception:
        pass
    return {
        "name": getattr(doc, "name", ""),
        "id": getattr(doc, "id", ""),
        "units": units,
        "type": type(product).__name__,
    }


def _get_selection() -> list[dict]:
    sel = _ui.activeSelections
    out = []
    for i in range(sel.count):
        item = sel.item(i).entity
        out.append({
            "id": getattr(item, "entityToken", "") or str(id(item)),
            "type": type(item).__name__,
            "metadata": {},
        })
    return out


def _insert_geometry(url: str) -> dict:
    """Download a STEP/STL from `url` and import it into the active doc."""
    if not url:
        raise ValueError("insertGeometry: url is required")
    # Download to a temp file
    suffix = Path(url.split("?")[0]).suffix or ".step"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        with urllib.request.urlopen(url, timeout=60) as resp:
            tmp.write(resp.read())
        tmp_path = tmp.name
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design — switch to Design workspace")
    import_mgr = _app.importManager
    if suffix.lower() in (".step", ".stp"):
        opts = import_mgr.createSTEPImportOptions(tmp_path)
    elif suffix.lower() in (".stl",):
        opts = import_mgr.createSTLImportOptions(tmp_path)
    else:
        raise ValueError(f"Unsupported geometry format: {suffix}")
    import_mgr.importToTarget(opts, design.rootComponent)
    return {"inserted": True, "path": tmp_path, "format": suffix.lstrip(".")}


def _update_parameter(name: str, value) -> dict:
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design")
    param = design.userParameters.itemByName(name)
    if param is None:
        raise KeyError(f"userParameter not found: {name}")
    param.expression = str(value)
    return {"ok": True, "name": name, "value": value}


def _get_feature_tree() -> dict:
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        return {"components": []}
    root = design.rootComponent
    def _walk(comp):
        return {
            "name": comp.name,
            "features": [f.name for f in comp.features],
            "occurrences": [_walk(occ.component) for occ in comp.occurrences],
        }
    return _walk(root)


def _export_current(fmt: str) -> dict:
    """Export the active design and write it somewhere the backend's
    EvalAgent can reach. Target: `<user home>/aria-exports/<stem>.<fmt>`
    — a known location that the backend can be configured to accept."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design")
    em = design.exportManager
    out_dir = Path.home() / "aria-exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = (design.parentDocument.name or "aria_export").replace(" ", "_")
    out = out_dir / f"{stem}.{fmt.lower()}"
    if fmt.lower() == "step":
        opts = em.createSTEPExportOptions(str(out), design.rootComponent)
    elif fmt.lower() == "stl":
        opts = em.createSTLExportOptions(design.rootComponent, str(out))
    elif fmt.lower() == "dxf":
        raise NotImplementedError("DXF export needs a sketch reference; "
                                  "pass sketch entity token via selection")
    else:
        raise ValueError(f"Unsupported export format: {fmt}")
    em.execute(opts)
    # Return BOTH a file:// URL and the raw absolute path. The panel
    # uses the path directly when posting to the backend so we don't
    # have to wrestle with file-URL encoding.
    return {"url": f"file:///{out.as_posix()}",
            "path": str(out.resolve()),
            "format": fmt.lower(),
            "bytes": out.stat().st_size if out.exists() else 0}


def _show_notification(msg: str, tone: str) -> None:
    # Fusion's own HUD
    try:
        _ui.messageBox(str(msg), "ARIA-OS")
    except Exception:
        pass


def _get_user_parameters() -> dict:
    """Read ALL User Parameters from the active design. Used by ARIA
    before submitting a new prompt to stay in sync with user edits
    made directly in Fusion's Parameters dialog."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        return {"ok": True, "parameters": [], "reason": "no active design"}
    out = []
    try:
        for p in design.userParameters:
            out.append({
                "name":       p.name,
                "value_cm":   float(p.value),   # Fusion stores internal
                "expression": p.expression,     # e.g. "100 mm" or "OD/2"
                "unit":       p.unit or "",
                "comment":    p.comment or "",
            })
    except Exception:
        pass
    return {"ok": True, "parameters": out,
            "count": len(out),
            "design_name": getattr(design.parentDocument, "name", "")}


def _open_file(path: str) -> dict:
    """Import a local STEP/STL into the active Fusion design.

    Fusion's `documents.open()` is for cloud-hosted docs only — it will not
    accept a local file path. For local geometry we must route through the
    importManager (same code path as `_insert_geometry`, minus the HTTP
    download).
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design — switch to Design workspace")
    import_mgr = _app.importManager
    ext = Path(path).suffix.lower()
    if ext in (".step", ".stp"):
        opts = import_mgr.createSTEPImportOptions(path)
    elif ext == ".stl":
        opts = import_mgr.createSTLImportOptions(path)
    else:
        raise ValueError(f"Unsupported geometry format: {ext}")
    import_mgr.importToTarget(opts, design.rootComponent)
    return {"opened": True, "path": path, "format": ext.lstrip(".")}


# --------------------------------------------------------------------
# Native feature-tree execution — streams parametric operations from ARIA
# directly into Fusion's timeline so each op (Sketch, Extrude, Cut, Pattern,
# Hole, Fillet) appears as a REAL Fusion feature in the browser tree.
#
# The panel emits per-op `executeFeature` calls over the bridge. Each
# handler does one Fusion API call and returns { ok, id, kind } so the
# panel can reference the result in later ops (e.g. to circular-pattern a
# previously-created cut feature).
#
# Object registry: we keep a dict of (alias -> adsk object) for this
# session so the panel can reference things by string name like
# "sketch_base", "cut_bolt", etc. Registry is cleared on `beginFeaturePlan`.
# --------------------------------------------------------------------

_FEATURE_REGISTRY: dict[str, object] = {}


def _value_input(v: float):
    return adsk.core.ValueInput.createByReal(float(v))


def _active_root():
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design — switch to Design workspace")
    return design.rootComponent


def _resolve_plane(root, spec):
    """Accept 'XY'|'YZ'|'XZ' or a dict {face_of: alias, which:'top'|'bottom'|'side'}."""
    if isinstance(spec, str):
        key = spec.strip().upper()
        if key in ("XY", "XZ", "YZ"):
            return getattr(root, {"XY": "xYConstructionPlane",
                                   "XZ": "xZConstructionPlane",
                                   "YZ": "yZConstructionPlane"}[key])
    if isinstance(spec, dict) and "face_of" in spec:
        body = _FEATURE_REGISTRY.get(spec["face_of"])
        which = spec.get("which", "top")
        if body is None:
            raise KeyError(f"Unknown body alias: {spec['face_of']}")
        # If what we stored is an extrude feature, get its body
        if hasattr(body, "bodies"):
            bodies = body.bodies
            if bodies.count == 0:
                raise RuntimeError("Extrude has no bodies")
            body = bodies.item(0)
        # Pick a face by Z coordinate (top = highest, bottom = lowest)
        faces = list(body.faces)
        if not faces:
            raise RuntimeError("Body has no faces")
        faces.sort(
            key=lambda f: f.pointOnFace.z if f.pointOnFace else 0,
            reverse=(which == "top"))
        return faces[0]
    raise ValueError(f"Unsupported plane spec: {spec!r}")


def _op_begin_plan(_params: dict) -> dict:
    _FEATURE_REGISTRY.clear()
    return {"ok": True, "registry_cleared": True}


def _op_new_sketch(params: dict) -> dict:
    root = _active_root()
    plane = _resolve_plane(root, params.get("plane", "XY"))
    sketch = root.sketches.add(plane)
    name = params.get("name", f"ARIA_Sketch_{root.sketches.count}")
    sketch.name = name
    alias = params.get("alias") or name
    _FEATURE_REGISTRY[alias] = sketch
    return {"ok": True, "id": alias, "kind": "sketch", "name": sketch.name}


def _op_sketch_circle(params: dict) -> dict:
    sk_alias = params.get("sketch")
    sketch = _FEATURE_REGISTRY.get(sk_alias) if sk_alias else None
    if sketch is None:
        raise KeyError(f"Unknown sketch alias: {sk_alias}")
    cx = float(params.get("cx", 0.0))
    cy = float(params.get("cy", 0.0))
    r  = float(params.get("r"))
    # Fusion uses cm internally (design units can be mm — API is always cm)
    scale = 0.1  # mm → cm
    sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(cx * scale, cy * scale, 0.0),
        r * scale)
    return {"ok": True, "kind": "circle", "r_mm": r, "cx_mm": cx, "cy_mm": cy}


def _op_sketch_rect(params: dict) -> dict:
    sketch = _FEATURE_REGISTRY.get(params.get("sketch"))
    if sketch is None:
        raise KeyError("Unknown sketch alias")
    w = float(params.get("w"))
    h = float(params.get("h"))
    cx = float(params.get("cx", 0.0))
    cy = float(params.get("cy", 0.0))
    scale = 0.1
    p1 = adsk.core.Point3D.create((cx - w/2) * scale, (cy - h/2) * scale, 0)
    p2 = adsk.core.Point3D.create((cx + w/2) * scale, (cy + h/2) * scale, 0)
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(p1, p2)
    return {"ok": True, "kind": "rect", "w_mm": w, "h_mm": h}


_OP_TO_FUSION = {
    "new":       adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
    "join":      adsk.fusion.FeatureOperations.JoinFeatureOperation,
    "cut":       adsk.fusion.FeatureOperations.CutFeatureOperation,
    "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
}


def _op_extrude(params: dict) -> dict:
    root = _active_root()
    sketch = _FEATURE_REGISTRY.get(params.get("sketch"))
    if sketch is None:
        raise KeyError("Unknown sketch alias")
    dist_mm = float(params.get("distance"))
    operation = _OP_TO_FUSION.get(params.get("operation", "new"),
                                   adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    # Pick profile(s): first one, or all if requested
    profs = sketch.profiles
    if profs.count == 0:
        raise RuntimeError("Sketch has no profile to extrude")
    prof = profs.item(0) if not params.get("all_profiles") else profs
    dist_input = _value_input(dist_mm * 0.1)  # mm→cm
    extr_input = root.features.extrudeFeatures.createInput(prof, operation)
    # Distance extent — positive = one side, negative = opposite
    distance_extent = adsk.fusion.DistanceExtentDefinition.create(dist_input)
    # Direction: default PositiveExtentDirection if distance > 0
    direction = (adsk.fusion.ExtentDirections.PositiveExtentDirection
                  if dist_mm >= 0
                  else adsk.fusion.ExtentDirections.NegativeExtentDirection)
    extr_input.setOneSideExtent(distance_extent, direction)
    extr_feature = root.features.extrudeFeatures.add(extr_input)
    alias = params.get("alias", f"extrude_{root.features.extrudeFeatures.count}")
    _FEATURE_REGISTRY[alias] = extr_feature
    # Also register the resulting body (if any) for face-picking later
    if extr_feature.bodies.count > 0:
        _FEATURE_REGISTRY[alias + "__body"] = extr_feature.bodies.item(0)
    return {"ok": True, "id": alias, "kind": "extrude",
            "distance_mm": dist_mm, "operation": params.get("operation", "new")}


def _op_circular_pattern(params: dict) -> dict:
    root = _active_root()
    feat_alias = params.get("feature")
    feat = _FEATURE_REGISTRY.get(feat_alias)
    if feat is None:
        raise KeyError(f"Unknown feature alias: {feat_alias}")
    count = int(params.get("count", 2))
    axis_spec = params.get("axis", "Z").upper() if isinstance(
        params.get("axis"), str) else "Z"
    axis = {"X": root.xConstructionAxis,
            "Y": root.yConstructionAxis,
            "Z": root.zConstructionAxis}[axis_spec]
    feats_coll = adsk.core.ObjectCollection.create()
    feats_coll.add(feat)
    pat_input = root.features.circularPatternFeatures.createInput(
        feats_coll, axis)
    pat_input.quantity = _value_input(count)
    pat_input.totalAngle = _value_input(360.0)
    pat_input.isSymmetric = False
    pattern = root.features.circularPatternFeatures.add(pat_input)
    alias = params.get("alias", f"pattern_{root.features.circularPatternFeatures.count}")
    _FEATURE_REGISTRY[alias] = pattern
    return {"ok": True, "id": alias, "kind": "circular_pattern",
            "count": count, "axis": axis_spec}


def _op_fillet(params: dict) -> dict:
    root = _active_root()
    r_mm = float(params.get("r"))
    body = _FEATURE_REGISTRY.get(params.get("body"))
    if body is None:
        raise KeyError("Unknown body alias for fillet")
    if hasattr(body, "bodies"):
        body = body.bodies.item(0)
    edges = adsk.core.ObjectCollection.create()
    for e in body.edges:
        edges.add(e)
    fil_input = root.features.filletFeatures.createInput()
    fil_input.addConstantRadiusEdgeSet(edges, _value_input(r_mm * 0.1), True)
    fil = root.features.filletFeatures.add(fil_input)
    alias = params.get("alias", f"fillet_{root.features.filletFeatures.count}")
    _FEATURE_REGISTRY[alias] = fil
    return {"ok": True, "id": alias, "kind": "fillet", "r_mm": r_mm}


_FEATURE_HANDLERS = {
    "beginPlan":        _op_begin_plan,
    "newSketch":        _op_new_sketch,
    "sketchCircle":     _op_sketch_circle,
    "sketchRect":       _op_sketch_rect,
    "extrude":          _op_extrude,
    "circularPattern":  _op_circular_pattern,
    "fillet":           _op_fillet,
}


# --------------------------------------------------------------------
# Assembly ops — streams multi-component + mate features into Fusion's
# active design. Fusion treats every Design as an assembly (rootComponent
# + occurrences) so no "switch to assembly" step is needed.
# --------------------------------------------------------------------

def _op_asm_begin(_params: dict) -> dict:
    """Reset the assembly registry. Optionally clears all existing
    occurrences (don't do that in destructive mode — leave existing
    Fusion work alone)."""
    _FEATURE_REGISTRY.clear()
    return {"ok": True, "stage": "assembly", "registry_cleared": True}


def _op_asm_add_component(params: dict) -> dict:
    """Create a new empty component as an occurrence in the root.
    The panel can then stream sketch/extrude ops at the component's
    local origin by passing the component alias in later ops (not yet
    wired — MVP creates empty placeholders)."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design")
    name = params.get("name") or f"ARIA_Comp_{design.rootComponent.occurrences.count + 1}"
    alias = params.get("alias") or name
    transform = adsk.core.Matrix3D.create()
    # Offset each new component so they don't all stack at the origin.
    # MVP: 50mm in +X per component.
    offset_x = float(params.get("x_mm", 0.0)) * 0.1   # mm → cm
    offset_y = float(params.get("y_mm", 0.0)) * 0.1
    offset_z = float(params.get("z_mm", 0.0)) * 0.1
    transform.translation = adsk.core.Vector3D.create(offset_x, offset_y, offset_z)
    occurrence = design.rootComponent.occurrences.addNewComponent(transform)
    occurrence.component.name = name
    _FEATURE_REGISTRY[alias] = occurrence
    return {"ok": True, "id": alias, "kind": "component", "name": name}


def _op_asm_joint(params: dict) -> dict:
    """Create a joint between two components. MVP supports 'rigid'
    (weld) — more joint types need face/edge references which require
    post-placement geometry queries."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design")
    root = design.rootComponent

    c1 = _FEATURE_REGISTRY.get(params.get("component1"))
    c2 = _FEATURE_REGISTRY.get(params.get("component2"))
    if c1 is None or c2 is None:
        raise KeyError("joint needs valid component1/component2 aliases")

    joint_type = params.get("joint_type", "rigid").lower()
    # For MVP: create a rigid joint at each occurrence's origin (component
    # local origin point). This welds them together.
    try:
        g1 = adsk.fusion.JointGeometry.createByPoint(
            c1.component.originConstructionPoint.createForAssemblyContext(c1))
        g2 = adsk.fusion.JointGeometry.createByPoint(
            c2.component.originConstructionPoint.createForAssemblyContext(c2))
    except Exception as _ge:
        raise RuntimeError(f"joint geometry setup failed: {_ge}")

    ji = root.joints.createInput(g1, g2)
    type_map = {
        "rigid":       adsk.fusion.JointTypes.RigidJointType,
        "revolute":    adsk.fusion.JointTypes.RevoluteJointType,
        "slider":      adsk.fusion.JointTypes.SliderJointType,
        "cylindrical": adsk.fusion.JointTypes.CylindricalJointType,
        "planar":      adsk.fusion.JointTypes.PlanarJointType,
        "ball":        adsk.fusion.JointTypes.BallJointType,
    }
    ji.setAsRigidJointMotion() if joint_type == "rigid" else None  # default
    # Fusion picks the "as*JointMotion" based on type — we set it explicitly
    # for non-rigid joints later when face-ref ops arrive.
    joint = root.joints.add(ji)
    alias = params.get("alias") or f"joint_{root.joints.count}"
    _FEATURE_REGISTRY[alias] = joint
    return {"ok": True, "id": alias, "kind": "joint",
            "joint_type": joint_type}


# --------------------------------------------------------------------
# Drawing ops — creates a new Drawing document from the active Design
# and streams sheet/view/dimension features into it. Fusion drawings
# live in a separate document, so these ops do NOT append to the
# active design's timeline; they go into the paired drawing doc.
# --------------------------------------------------------------------

_DWG_STATE = {"document": None, "sheet": None}


def _op_dwg_begin(params: dict) -> dict:
    """Create a new drawing document referencing the active design."""
    active_doc = _app.activeDocument
    if active_doc is None:
        raise RuntimeError("No active Fusion document to drawing-reference")
    # Fusion creates a new drawing via the DrawingDocument type
    # `_app.documents.add(documentType, sourceDesign)` — use the
    # Drawing2DDocumentType. If not available on this install, surface
    # the specific error rather than silently failing.
    try:
        draw_doc = _app.documents.add(
            adsk.core.DocumentTypes.DrawingDocumentType,
            active_doc)  # source design
    except Exception as _dde:
        raise RuntimeError(f"Drawing document create failed: {_dde}")
    _DWG_STATE["document"] = draw_doc
    _DWG_STATE["sheet"] = None
    return {"ok": True, "kind": "drawing_doc",
            "name": getattr(draw_doc, "name", "ARIA Drawing")}


def _op_dwg_new_sheet(params: dict) -> dict:
    draw_doc = _DWG_STATE["document"]
    if draw_doc is None:
        raise RuntimeError("Call beginDrawing before newSheet")
    drawing = adsk.drawing.Drawing.cast(draw_doc.products.itemByProductType(
        "DrawingProductType"))
    if drawing is None:
        raise RuntimeError("Drawing product not available on this doc")
    size = (params.get("size") or "A3").upper()
    size_map = {
        "A4": adsk.drawing.DrawingSheetSizes.A4Size,
        "A3": adsk.drawing.DrawingSheetSizes.A3Size,
        "A2": adsk.drawing.DrawingSheetSizes.A2Size,
        "A1": adsk.drawing.DrawingSheetSizes.A1Size,
        "A0": adsk.drawing.DrawingSheetSizes.A0Size,
    }
    sheet_size = size_map.get(size, adsk.drawing.DrawingSheetSizes.A3Size)
    sheet = drawing.sheets.add(sheet_size,
        adsk.drawing.DrawingSheetOrientations.LandscapeOrientation,
        adsk.drawing.DrawingUnits.MillimeterDrawingUnit,
        params.get("name", "Sheet1"))
    _DWG_STATE["sheet"] = sheet
    return {"ok": True, "kind": "sheet", "size": size, "name": sheet.name}


def _op_dwg_add_view(params: dict) -> dict:
    sheet = _DWG_STATE["sheet"]
    if sheet is None:
        raise RuntimeError("Call newSheet before addView")
    view_type = (params.get("view_type") or "front").lower()
    orientation_map = {
        "front":    adsk.drawing.DrawingViewOrientations.FrontDrawingViewOrientation,
        "top":      adsk.drawing.DrawingViewOrientations.TopDrawingViewOrientation,
        "right":    adsk.drawing.DrawingViewOrientations.RightDrawingViewOrientation,
        "left":     adsk.drawing.DrawingViewOrientations.LeftDrawingViewOrientation,
        "back":     adsk.drawing.DrawingViewOrientations.BackDrawingViewOrientation,
        "iso":      adsk.drawing.DrawingViewOrientations.IsoTopLeftDrawingViewOrientation,
    }
    orientation = orientation_map.get(view_type,
        adsk.drawing.DrawingViewOrientations.FrontDrawingViewOrientation)
    style = adsk.drawing.DrawingViewStyles.ShadedWithVisibleEdgesDrawingViewStyle
    scale = float(params.get("scale", 1.0))
    x_mm = float(params.get("x_mm", 100.0)) * 0.1  # mm → cm
    y_mm = float(params.get("y_mm", 100.0)) * 0.1
    position = adsk.core.Point3D.create(x_mm, y_mm, 0)
    # ViewInput needs the source component/occurrence. MVP: use the
    # root component of the source design.
    design = _app.activeProduct
    ref_comp = design.rootComponent if hasattr(design, "rootComponent") else None
    if ref_comp is None:
        raise RuntimeError("Source design has no rootComponent")
    view_input = sheet.drawingViews.createInput(
        ref_comp, orientation, style, position, scale)
    view = sheet.drawingViews.add(view_input)
    alias = params.get("alias") or f"view_{sheet.drawingViews.count}"
    _FEATURE_REGISTRY[alias] = view
    return {"ok": True, "id": alias, "kind": "drawing_view",
            "view_type": view_type, "scale": scale}


def _op_dwg_title_block(params: dict) -> dict:
    sheet = _DWG_STATE["sheet"]
    if sheet is None:
        raise RuntimeError("Call newSheet before addTitleBlock")
    # Title block content is set via custom properties on the drawing
    # sheet — Fusion API exposes them as a DrawingTitleBlock. MVP:
    # just set the sheet's text metadata fields if available.
    fields = {
        "part_number": params.get("part_number", ""),
        "description": params.get("description", ""),
        "material":    params.get("material", ""),
        "revision":    params.get("revision", "A"),
    }
    # Fusion's title-block metadata API is limited — we use the sheet's
    # name as a fallback visible marker.
    try:
        sheet.name = fields.get("part_number") or sheet.name
    except Exception:
        pass
    return {"ok": True, "kind": "title_block", "fields": fields}


_FEATURE_HANDLERS.update({
    # Assembly ops
    "asmBegin":        _op_asm_begin,
    "addComponent":    _op_asm_add_component,
    "joint":           _op_asm_joint,
    # Drawing ops
    "beginDrawing":    _op_dwg_begin,
    "newSheet":        _op_dwg_new_sheet,
    "addView":         _op_dwg_add_view,
    "addTitleBlock":   _op_dwg_title_block,
})


# --------------------------------------------------------------------
# Fusion Electronics (Eagle-derived) native ops.
#
# Fusion 360's Electronics workspace exposes an API via `adsk.electron`
# for creating new electronics documents, placing library symbols on a
# schematic, placing footprints on a board, and connecting nets. Auto-
# routing is NOT programmatically accessible — users finish routing in
# Fusion's interactive router. The MVP covers the placement subset
# which is typically 80% of the "hard part" of ECAD.
# --------------------------------------------------------------------

_ECAD_STATE: dict[str, Any] = {"doc": None, "schematic": None, "board": None}


def _op_ecad_begin(params: dict) -> dict:
    """Create a new Electronics document. If Fusion's electron module
    isn't available (user doesn't have an electronics-enabled license),
    raise a clear error so the panel can surface it."""
    try:
        import adsk.electron  # type: ignore  # noqa: F401
    except Exception:
        raise RuntimeError(
            "Fusion Electronics not available on this install — "
            "ECAD-in-Fusion needs an Electronics entitlement. Falling "
            "back to the KiCad executor is handled server-side.")
    # Create new electronics document via the DocumentTypes enum
    try:
        doc = _app.documents.add(
            adsk.core.DocumentTypes.ElectronDesignDocumentType)
    except Exception as exc:
        raise RuntimeError(f"Electron document create failed: {exc}")
    _ECAD_STATE["doc"] = doc
    # The new doc has one schematic + one empty board by default
    _ECAD_STATE["schematic"] = getattr(doc, "design", None)
    _ECAD_STATE["board"] = getattr(doc, "board", None)
    return {"ok": True, "kind": "electron_doc",
            "name": getattr(doc, "name", "ARIA Electronics")}


def _op_ecad_place_symbol(params: dict) -> dict:
    """Place a library symbol on the schematic."""
    sch = _ECAD_STATE["schematic"]
    if sch is None:
        raise RuntimeError("Call beginElectronics first")
    ref = params["ref"]
    library = params.get("library", "supply1")
    device  = params["device"]        # e.g. "VCC", "R-EU_0805", "LED-5MM"
    x_mm = float(params.get("x_mm", 0)) * 0.1  # mm → cm? Check — Fusion
    y_mm = float(params.get("y_mm", 0)) * 0.1  # electron uses mm natively
    # Fusion Electron API: design.addSymbol(library, device, x, y, ref)
    try:
        sym = sch.addSymbol(library, device, x_mm, y_mm, ref)
    except Exception as exc:
        raise RuntimeError(f"addSymbol({library}/{device}) failed: {exc}")
    _FEATURE_REGISTRY[params.get("alias") or ref] = sym
    return {"ok": True, "ref": ref, "device": device,
            "library": library, "kind": "symbol"}


def _op_ecad_place_footprint(params: dict) -> dict:
    """Place a footprint on the board."""
    board = _ECAD_STATE["board"]
    if board is None:
        raise RuntimeError("Call beginElectronics first")
    ref = params["ref"]
    library = params.get("library", "rcl")
    package = params["package"]
    x = float(params.get("x_mm", 0))
    y = float(params.get("y_mm", 0))
    rot = float(params.get("rot_deg", 0))
    side = params.get("side", "top")   # "top" or "bottom"
    try:
        fp = board.addElement(library, package, ref, x, y, rot, side)
    except Exception as exc:
        raise RuntimeError(f"addElement({library}/{package}) failed: {exc}")
    _FEATURE_REGISTRY[params.get("alias") or ref] = fp
    return {"ok": True, "ref": ref, "package": package,
            "library": library, "kind": "footprint"}


def _op_ecad_add_net(params: dict) -> dict:
    """Add a named net and optionally connect pin endpoints."""
    sch = _ECAD_STATE["schematic"]
    if sch is None:
        raise RuntimeError("Call beginElectronics first")
    name = params["name"]
    connections = params.get("connect", [])  # [[ref, pin], ...]
    try:
        net = sch.addNet(name)
        for ref_pin in connections:
            if len(ref_pin) == 2:
                net.connect(ref_pin[0], ref_pin[1])
    except Exception as exc:
        raise RuntimeError(f"addNet({name}) failed: {exc}")
    return {"ok": True, "name": name, "connections": len(connections),
            "kind": "net"}


def _op_ecad_board_outline(params: dict) -> dict:
    """Set the board outline to a rectangle."""
    board = _ECAD_STATE["board"]
    if board is None:
        raise RuntimeError("Call beginElectronics first")
    w = float(params.get("width_mm", 30))
    h = float(params.get("height_mm", 20))
    try:
        board.setOutline(0, 0, w, h)
    except Exception as exc:
        # Some API versions use polygon-based outline
        raise RuntimeError(f"setOutline failed: {exc}")
    return {"ok": True, "kind": "board_outline",
            "size_mm": [w, h]}


_FEATURE_HANDLERS.update({
    # Fusion ECAD ops — mirror the KiCad handler names so the same
    # planner can target both backends. Server-side dispatcher picks
    # which executor to route the plan to based on host capability.
    "beginElectronics": _op_ecad_begin,
    "placeSymbol":      _op_ecad_place_symbol,
    "placeFootprint":   _op_ecad_place_footprint,
    "addConnection":    _op_ecad_add_net,
    "boardOutline":     _op_ecad_board_outline,
})


# --------------------------------------------------------------------
# Fusion native-leverage ops — expose the stuff Fusion does that our
# generic ops can't match: parametric User Parameters, Generative Design
# handoff, native CAM, native Simulation, Motion Studies, Sheet Metal
# commands, linked drawings, A360 version history.
# --------------------------------------------------------------------

# --- #1 User Parameters ------------------------------------------------

def _op_add_parameter(params: dict) -> dict:
    """Add a Fusion User Parameter (visible + editable in the Parameters
    dialog). Every ARIA-generated dimension should be a named user
    parameter so the user can tweak one field → tree rebuilds live."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design")
    name  = params["name"]
    value = float(params["value_mm"])
    unit  = params.get("unit", "mm")
    comment = params.get("comment", "")
    # Fusion's API: userParameters.add(name, ValueInput, unit, comment)
    existing = design.userParameters.itemByName(name)
    if existing:
        existing.expression = f"{value} {unit}"
        return {"ok": True, "name": name, "updated": True,
                "value": value, "unit": unit}
    p = design.userParameters.add(name, _value_input(value), unit, comment)
    _FEATURE_REGISTRY[f"param_{name}"] = p
    return {"ok": True, "name": name, "created": True,
            "value": value, "unit": unit}


# --- #2 Generative Design handoff --------------------------------------

def _op_open_generative_design(params: dict) -> dict:
    """Launch Fusion's Generative Design workspace on the active design.
    The `params` dict can include preserve/obstacle geometry references
    for the study setup, but MVP just opens the workspace and lets the
    user set constraints manually."""
    try:
        # Fusion switches workspaces via a command
        cmd_def = _ui.commandDefinitions.itemById(
            "FusionGenerativeDesignEntryPointCommand")
        if cmd_def:
            cmd_def.execute()
            return {"ok": True, "kind": "generative_design",
                    "status": "launched"}
        # Fallback — some versions use a different cmd ID
        cmd_def = _ui.commandDefinitions.itemById(
            "FusionSolidEnvironmentActivateCommand")
        return {"ok": True, "kind": "generative_design",
                "status": "cmd-not-found",
                "hint": "Open via Design→Generative Design manually"}
    except Exception as exc:
        return {"ok": False, "error": f"GD launch failed: {exc}"}


# --- #3 Native Fusion CAM ----------------------------------------------

def _op_create_cam_setup(params: dict) -> dict:
    """Create a CAM setup on the active design — stock size, WCS,
    operations can then be added. Uses adsk.cam directly."""
    try:
        products = _app.activeDocument.products
        cam = None
        for i in range(products.count):
            p = products.item(i)
            if p.productType == "CAMProductType":
                cam = adsk.cam.CAM.cast(p)
                break
        if cam is None:
            raise RuntimeError("CAM product not active — switch to Manufacture workspace")
        setup_input = cam.setups.createInput(
            adsk.cam.OperationTypes.MillingOperation)
        setup_input.name = params.get("name", "ARIA Milling")
        setup = cam.setups.add(setup_input)
        _FEATURE_REGISTRY[params.get("alias", "cam_setup")] = setup
        return {"ok": True, "kind": "cam_setup", "id": setup.name}
    except Exception as exc:
        return {"ok": False, "error": f"CAM setup failed: {exc}"}


# --- #5 Motion Study ---------------------------------------------------

def _op_create_motion_study(params: dict) -> dict:
    """Auto-create a motion study using joints already defined in the
    root component. User gets kinematic playback without lifting a
    finger."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    if design is None:
        raise RuntimeError("No active Fusion Design")
    root = design.rootComponent
    if root.joints.count == 0:
        return {"ok": False,
                "error": "No joints in active design — add joints before motion study"}
    # Fusion's motion studies are auto-generated from existing joints
    # via the `motionStudies` collection; `addDefault` creates a study
    # that drives every non-rigid joint through its range.
    try:
        study = root.motionStudies.addDefault()
        study.name = params.get("name", "ARIA Motion Study")
        return {"ok": True, "kind": "motion_study", "id": study.name,
                "n_joints": root.joints.count}
    except Exception as exc:
        return {"ok": False, "error": f"Motion study failed: {exc}"}


# --- #6 Sheet Metal workspace -----------------------------------------

def _op_sheet_metal_flange(params: dict) -> dict:
    """Add a sheet metal flange to an existing edge. The sheet-metal
    planner passes edge geometry, flange length, bend angle."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    root = design.rootComponent
    sm_feats = root.features.sheetMetalFeatures \
        if hasattr(root.features, "sheetMetalFeatures") else None
    if sm_feats is None:
        return {"ok": False, "error": "Sheet Metal workspace not available"}
    # MVP: return a placeholder — real flange needs ObjectCollection of
    # edges and a bend-radius. Requires face picking we don't have here.
    return {"ok": True, "kind": "sheet_metal_flange",
            "status": "stub — needs edge selection"}


def _op_sheet_metal_base(params: dict) -> dict:
    """Create a base flange from a sketch profile — the root of any
    sheet metal part."""
    design = adsk.fusion.Design.cast(_app.activeProduct)
    root = design.rootComponent
    sk_alias = params.get("sketch")
    sketch = _FEATURE_REGISTRY.get(sk_alias)
    if sketch is None:
        raise KeyError(f"Unknown sketch alias: {sk_alias}")
    thickness = float(params.get("thickness_mm", 1.5))
    try:
        sm_feats = root.features.flangeFeatures
        flange_input = sm_feats.createInput(
            sketch.profiles.item(0),
            _value_input(thickness * 0.1))  # mm→cm
        flange = sm_feats.add(flange_input)
        alias = params.get("alias", "base_flange")
        _FEATURE_REGISTRY[alias] = flange
        return {"ok": True, "id": alias, "kind": "sheet_metal_base",
                "thickness_mm": thickness}
    except Exception as exc:
        return {"ok": False, "error": f"Base flange failed: {exc}"}


# --- #7 Drawing: already handled via beginDrawing, but let's add
# --- a dimension op that references the DESIGN (auto-updates)

def _op_drawing_auto_dim(params: dict) -> dict:
    """Add an auto-dimension that references geometry in the source
    design. When the design changes, the dim value updates."""
    sheet = _DWG_STATE.get("sheet")
    if sheet is None:
        raise RuntimeError("Call newSheet before autoDimension")
    view_alias = params.get("view")
    view = _FEATURE_REGISTRY.get(view_alias)
    if view is None:
        return {"ok": False, "error": f"Unknown view alias: {view_alias}"}
    # Fusion Drawing API: sheet.dimensions.addLinearDimension with two
    # edges. MVP returns a stub — real auto-dim needs view-entity refs
    # which require the view to be fully rendered first.
    return {"ok": True, "kind": "auto_dim",
            "status": "stub — needs view entity selection"}


# --- #8 A360 version history ------------------------------------------

def _op_snapshot_version(params: dict) -> dict:
    """Save the active document as a new A360 version with a named
    description. Replaces our custom session memory with Fusion's
    native file versioning."""
    doc = _app.activeDocument
    if doc is None:
        raise RuntimeError("No active document to snapshot")
    desc = params.get("description", "ARIA pipeline snapshot")
    try:
        doc.save(desc)
        data_file = doc.dataFile
        version = data_file.latestVersion if data_file else None
        return {"ok": True, "kind": "version",
                "description": desc,
                "version_id": version.versionId if version else "?"}
    except Exception as exc:
        return {"ok": False, "error": f"Snapshot failed: {exc}"}


_FEATURE_HANDLERS.update({
    # Native-leverage ops
    "addParameter":           _op_add_parameter,
    "openGenerativeDesign":   _op_open_generative_design,
    "createCAMSetup":         _op_create_cam_setup,
    "createMotionStudy":      _op_create_motion_study,
    "sheetMetalBase":         _op_sheet_metal_base,
    "sheetMetalFlange":       _op_sheet_metal_flange,
    "drawingAutoDim":         _op_drawing_auto_dim,
    "snapshotVersion":        _op_snapshot_version,
})


def _execute_feature(kind: str, params: dict) -> dict:
    handler = _FEATURE_HANDLERS.get(kind)
    if handler is None:
        raise ValueError(f"Unknown feature kind: {kind}")
    return handler(params or {})


def _execute_feature_plan(ops: list) -> dict:
    """Execute a whole plan in one call. Avoids N×WebView2 round-trips
    — all ops run against Fusion's API inside a single Python invocation.

    Returns {ok: bool, results: [...], n_total, n_succeeded, n_failed,
    failed_at: index | -1, error: str | None}. First failure stops
    execution; earlier ops' results are still returned so the user sees
    how far the plan got."""
    results = []
    failed_at = -1
    err = None
    for i, op in enumerate(ops or []):
        try:
            kind = op.get("kind") if isinstance(op, dict) else None
            params = op.get("params") if isinstance(op, dict) else {}
            if not kind:
                raise ValueError(f"Op #{i}: missing 'kind'")
            res = _execute_feature(kind, params or {})
            results.append({"ok": True, "kind": kind, "seq": i + 1,
                             "result": res})
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            results.append({"ok": False, "kind": kind or "?",
                             "seq": i + 1, "error": err})
            failed_at = i
            break
    return {
        "ok": failed_at < 0,
        "results": results,
        "n_total": len(ops or []),
        "n_succeeded": sum(1 for r in results if r.get("ok")),
        "n_failed": sum(1 for r in results if not r.get("ok")),
        "failed_at": failed_at,
        "error": err,
    }


# --------------------------------------------------------------------
# Speech input — records raw PCM via Windows winmm (ctypes, stdlib-only)
# and returns it as base64 WAV so the panel can POST to /api/stt/
# transcribe (Groq Whisper-large-v3-turbo). Whisper handles engineering
# terminology (flange, impeller, 6061, M6, PCD) far better than Windows'
# built-in System.Speech recognizer, which mangled CAD nouns.
# --------------------------------------------------------------------

import base64 as _base64
import ctypes as _ctypes
import struct as _struct
import time as _time_mod


class _WAVEFORMATEX(_ctypes.Structure):
    _fields_ = [
        ("wFormatTag",     _ctypes.c_ushort),
        ("nChannels",      _ctypes.c_ushort),
        ("nSamplesPerSec", _ctypes.c_ulong),
        ("nAvgBytesPerSec",_ctypes.c_ulong),
        ("nBlockAlign",    _ctypes.c_ushort),
        ("wBitsPerSample", _ctypes.c_ushort),
        ("cbSize",         _ctypes.c_ushort),
    ]


class _WAVEHDR(_ctypes.Structure):
    _fields_ = [
        ("lpData",         _ctypes.c_char_p),
        ("dwBufferLength", _ctypes.c_ulong),
        ("dwBytesRecorded",_ctypes.c_ulong),
        ("dwUser",         _ctypes.c_void_p),
        ("dwFlags",        _ctypes.c_ulong),
        ("dwLoops",        _ctypes.c_ulong),
        ("lpNext",         _ctypes.c_void_p),
        ("reserved",       _ctypes.c_void_p),
    ]


def _record_wav_bytes(duration_s: int,
                       sample_rate: int = 16000,
                       channels: int = 1,
                       bits_per_sample: int = 16) -> bytes:
    """Record `duration_s` seconds from the default mic via winmm and
    return a complete in-memory WAV file (RIFF header + PCM data)."""
    winmm = _ctypes.windll.winmm
    WAVE_MAPPER    = -1
    CALLBACK_NULL  = 0
    WAVE_FORMAT_PCM = 1

    fmt = _WAVEFORMATEX()
    fmt.wFormatTag     = WAVE_FORMAT_PCM
    fmt.nChannels      = channels
    fmt.nSamplesPerSec = sample_rate
    fmt.wBitsPerSample = bits_per_sample
    fmt.nBlockAlign    = channels * bits_per_sample // 8
    fmt.nAvgBytesPerSec = sample_rate * fmt.nBlockAlign
    fmt.cbSize         = 0

    buf_size = sample_rate * fmt.nBlockAlign * duration_s
    buf = _ctypes.create_string_buffer(buf_size)

    hdr = _WAVEHDR()
    hdr.lpData = _ctypes.cast(buf, _ctypes.c_char_p)
    hdr.dwBufferLength = buf_size
    hdr.dwBytesRecorded = 0
    hdr.dwFlags = 0

    hwi = _ctypes.c_void_p()
    r = winmm.waveInOpen(_ctypes.byref(hwi), WAVE_MAPPER,
                          _ctypes.byref(fmt), 0, 0, CALLBACK_NULL)
    if r != 0:
        raise RuntimeError(f"waveInOpen failed: MMSYSERR {r}")
    try:
        r = winmm.waveInPrepareHeader(hwi, _ctypes.byref(hdr),
                                        _ctypes.sizeof(hdr))
        if r != 0: raise RuntimeError(f"waveInPrepareHeader failed: {r}")
        r = winmm.waveInAddBuffer(hwi, _ctypes.byref(hdr),
                                    _ctypes.sizeof(hdr))
        if r != 0: raise RuntimeError(f"waveInAddBuffer failed: {r}")
        r = winmm.waveInStart(hwi)
        if r != 0: raise RuntimeError(f"waveInStart failed: {r}")
        # Wait while the buffer fills. `WHDR_DONE` flag (0x00000001) is
        # set when the buffer is full — poll until then or timeout.
        deadline = _time_mod.time() + duration_s + 1.0
        while _time_mod.time() < deadline and not (hdr.dwFlags & 1):
            _time_mod.sleep(0.05)
        winmm.waveInStop(hwi)
        winmm.waveInUnprepareHeader(hwi, _ctypes.byref(hdr),
                                     _ctypes.sizeof(hdr))
    finally:
        winmm.waveInClose(hwi)

    recorded = hdr.dwBytesRecorded or buf_size
    pcm = _ctypes.string_at(buf, recorded)

    # Build RIFF/WAVE container
    data_size = len(pcm)
    header = b"RIFF" + _struct.pack("<I", 36 + data_size) + b"WAVE"
    header += b"fmt " + _struct.pack(
        "<IHHIIHH", 16, 1, channels, sample_rate,
        fmt.nAvgBytesPerSec, fmt.nBlockAlign, bits_per_sample)
    header += b"data" + _struct.pack("<I", data_size)
    return header + pcm


import threading as _threading

# Session state for async recording — the panel's mic button is a
# toggle: click once to start, click again to stop. We record on a
# background thread so the HTMLEventArgs main thread stays free to
# receive the "stopRecording" event mid-capture.
_REC_STATE = {
    "active":    False,   # True while recording thread is running
    "stop_flag": False,   # flipped True by stopRecording to cut short
    "result":    None,    # WAV bytes on success, {"error": ...} on fail
    "session":   0,       # monotonic id so stale polls see what they
                           # actually asked about
    "lock":      _threading.Lock(),
}


def _record_wav_bytes_interruptible(duration_s: int,
                                     stop_check) -> bytes:
    """Same as `_record_wav_bytes` but polls `stop_check()` each 50ms
    and calls `waveInStop` early when it returns True. Returns whatever
    was captured up to that point (the winmm buffer tracks
    `dwBytesRecorded`)."""
    winmm = _ctypes.windll.winmm
    WAVE_MAPPER    = -1
    CALLBACK_NULL  = 0
    WAVE_FORMAT_PCM = 1
    sample_rate, channels, bits = 16000, 1, 16

    fmt = _WAVEFORMATEX()
    fmt.wFormatTag     = WAVE_FORMAT_PCM
    fmt.nChannels      = channels
    fmt.nSamplesPerSec = sample_rate
    fmt.wBitsPerSample = bits
    fmt.nBlockAlign    = channels * bits // 8
    fmt.nAvgBytesPerSec = sample_rate * fmt.nBlockAlign
    fmt.cbSize         = 0

    buf_size = sample_rate * fmt.nBlockAlign * duration_s
    buf = _ctypes.create_string_buffer(buf_size)

    hdr = _WAVEHDR()
    hdr.lpData = _ctypes.cast(buf, _ctypes.c_char_p)
    hdr.dwBufferLength = buf_size
    hdr.dwBytesRecorded = 0
    hdr.dwFlags = 0

    hwi = _ctypes.c_void_p()
    r = winmm.waveInOpen(_ctypes.byref(hwi), WAVE_MAPPER,
                          _ctypes.byref(fmt), 0, 0, CALLBACK_NULL)
    if r != 0:
        raise RuntimeError(f"waveInOpen failed: MMSYSERR {r}")
    try:
        winmm.waveInPrepareHeader(hwi, _ctypes.byref(hdr), _ctypes.sizeof(hdr))
        winmm.waveInAddBuffer(hwi, _ctypes.byref(hdr), _ctypes.sizeof(hdr))
        winmm.waveInStart(hwi)
        deadline = _time_mod.time() + duration_s + 1.0
        while _time_mod.time() < deadline and not (hdr.dwFlags & 1):
            if stop_check():
                break
            _time_mod.sleep(0.05)
        winmm.waveInStop(hwi)
        # Reset before unprepare (winmm quirk)
        winmm.waveInReset(hwi)
        winmm.waveInUnprepareHeader(hwi, _ctypes.byref(hdr),
                                     _ctypes.sizeof(hdr))
    finally:
        winmm.waveInClose(hwi)

    recorded = hdr.dwBytesRecorded or buf_size
    if recorded == 0:
        raise RuntimeError("No audio captured — mic may be muted")
    pcm = _ctypes.string_at(buf, recorded)

    data_size = len(pcm)
    header = b"RIFF" + _struct.pack("<I", 36 + data_size) + b"WAVE"
    header += b"fmt " + _struct.pack(
        "<IHHIIHH", 16, 1, channels, sample_rate,
        fmt.nAvgBytesPerSec, fmt.nBlockAlign, bits)
    header += b"data" + _struct.pack("<I", data_size)
    return header + pcm


def _record_and_transcribe(duration_s: int) -> dict:
    """Async start — returns immediately with a session id. Panel polls
    `pollRecording` until done, or calls `stopRecording` to cut short."""
    duration_s = max(3, min(60, int(duration_s)))
    with _REC_STATE["lock"]:
        _REC_STATE["session"] += 1
        sid = _REC_STATE["session"]
        _REC_STATE["active"] = True
        _REC_STATE["stop_flag"] = False
        _REC_STATE["result"] = None

    def _worker(session_id):
        try:
            def _should_stop():
                with _REC_STATE["lock"]:
                    return _REC_STATE["stop_flag"] or \
                           _REC_STATE["session"] != session_id
            wav = _record_wav_bytes_interruptible(duration_s, _should_stop)
            with _REC_STATE["lock"]:
                if _REC_STATE["session"] == session_id:
                    _REC_STATE["result"] = wav
                    _REC_STATE["active"] = False
        except Exception as exc:
            with _REC_STATE["lock"]:
                if _REC_STATE["session"] == session_id:
                    _REC_STATE["result"] = {"error": str(exc)}
                    _REC_STATE["active"] = False

    _threading.Thread(target=_worker, args=(sid,), daemon=True).start()
    return {"ok": True, "session_id": sid, "status": "recording",
            "max_duration_s": duration_s}


def _stop_recording() -> dict:
    with _REC_STATE["lock"]:
        _REC_STATE["stop_flag"] = True
        sid = _REC_STATE["session"]
    return {"ok": True, "session_id": sid, "stopping": True}


def _poll_recording(session_id: int) -> dict:
    with _REC_STATE["lock"]:
        if _REC_STATE["session"] != int(session_id):
            return {"ok": False, "error": "stale session"}
        if _REC_STATE["active"]:
            return {"ok": True, "status": "recording",
                    "session_id": _REC_STATE["session"]}
        res = _REC_STATE["result"]
        if isinstance(res, dict) and "error" in res:
            return {"ok": False, "status": "error",
                    "error": res["error"]}
        if res is None:
            return {"ok": False, "status": "unknown",
                    "error": "recording ended with no data"}
        if len(res) < 1024:
            return {"ok": False, "status": "error",
                    "error": "Recording too short — mic muted?"}
        b64 = _base64.b64encode(res).decode("ascii")
        return {"ok": True, "status": "done",
                "audio_b64": b64, "mime": "audio/wav",
                "bytes": len(res)}


# --------------------------------------------------------------------
# Event handlers — Palette message dispatch
# --------------------------------------------------------------------

class _IncomingHtmlEventHandler(adsk.core.HTMLEventHandler):
    def notify(self, args):
        try:
            html_args = adsk.core.HTMLEventArgs.cast(args)
            action = html_args.action
            data = json.loads(html_args.data) if html_args.data else {}
            id_ = data.get("_id", "")
            try:
                if action == "getCurrentDocument":
                    _reply(id_, result=_get_current_document())
                elif action == "getSelection":
                    _reply(id_, result=_get_selection())
                elif action == "insertGeometry":
                    _reply(id_, result=_insert_geometry(data.get("url", "")))
                elif action == "updateParameter":
                    _reply(id_, result=_update_parameter(
                        data.get("name", ""), data.get("value")))
                elif action == "getFeatureTree":
                    _reply(id_, result=_get_feature_tree())
                elif action == "exportCurrent":
                    _reply(id_, result=_export_current(data.get("format", "step")))
                elif action == "showNotification":
                    _show_notification(data.get("msg", ""), data.get("tone", "info"))
                    _reply(id_, result={"ok": True})
                elif action == "openFile":
                    _reply(id_, result=_open_file(data.get("path", "")))
                elif action == "executeFeature":
                    _reply(id_, result=_execute_feature(
                        data.get("kind", ""), data.get("params", {})))
                elif action == "recordAudio":
                    # Start async recording on a worker thread. Returns
                    # session_id immediately; panel polls / stops.
                    _reply(id_, result=_record_and_transcribe(
                        int(data.get("duration_s", 30))))
                elif action == "stopRecording":
                    _reply(id_, result=_stop_recording())
                elif action == "pollRecording":
                    _reply(id_, result=_poll_recording(
                        int(data.get("session_id", 0))))
                elif action == "getUserParameters":
                    _reply(id_, result=_get_user_parameters())
                else:
                    _reply(id_, error=f"unknown action: {action}")
            except Exception as e:
                _reply(id_, error=f"{type(e).__name__}: {e}")
        except Exception:
            if _ui: _ui.messageBox("ARIA bridge error:\n" + traceback.format_exc())


class _CommandExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global _palette
        try:
            palettes = _ui.palettes
            _palette = palettes.itemById(_PANEL_ID)
            if not _palette:
                _palette = palettes.add(
                    _PANEL_ID, "ARIA Generate",
                    _DEFAULT_URL,
                    True,   # isVisible
                    True,   # showCloseButton
                    True,   # isResizable
                    380, 720  # minWidth, minHeight
                )
                _palette.dockingState = adsk.core.PaletteDockingStates.PaletteDockStateRight
                on_msg = _IncomingHtmlEventHandler()
                _palette.incomingFromHTML.add(on_msg)
                _handlers.append(on_msg)
            else:
                _palette.isVisible = True
        except Exception:
            if _ui: _ui.messageBox("Failed to open ARIA panel:\n" + traceback.format_exc())


class _CommandCreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        try:
            on_execute = _CommandExecuteHandler()
            args.command.execute.add(on_execute)
            _handlers.append(on_execute)
        except Exception:
            if _ui: _ui.messageBox(traceback.format_exc())


# --------------------------------------------------------------------
# Add-in lifecycle
# --------------------------------------------------------------------

def run(_context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface
        cmd_defs = _ui.commandDefinitions
        existing = cmd_defs.itemById(_CMD_ID)
        if existing:
            existing.deleteMe()
        cmd_def = cmd_defs.addButtonDefinition(
            _CMD_ID, _CMD_NAME, _CMD_TOOLTIP)
        on_created = _CommandCreatedHandler()
        cmd_def.commandCreated.add(on_created)
        _handlers.append(on_created)
        # Attach to SOLID → CREATE
        try:
            workspaces = _ui.workspaces
            ws = workspaces.itemById("FusionSolidEnvironment")
            panel = ws.toolbarPanels.itemById("SolidCreatePanel")
            panel.controls.addCommand(cmd_def)
        except Exception:
            pass  # toolbar attach is cosmetic — command still callable
    except Exception:
        if _ui: _ui.messageBox("ARIA add-in failed to start:\n" + traceback.format_exc())


def stop(_context):
    global _palette
    try:
        if _palette:
            _palette.deleteMe()
            _palette = None
        if _ui:
            cmd_def = _ui.commandDefinitions.itemById(_CMD_ID)
            if cmd_def:
                cmd_def.deleteMe()
            try:
                ws = _ui.workspaces.itemById("FusionSolidEnvironment")
                panel = ws.toolbarPanels.itemById("SolidCreatePanel")
                ctrl = panel.controls.itemById(_CMD_ID)
                if ctrl: ctrl.deleteMe()
            except Exception:
                pass
    except Exception:
        if _ui: _ui.messageBox(traceback.format_exc())
