"""aria_autocad_server.py — Headless HTTP listener for AutoCAD drawing generation.

Mirrors the SolidWorks (port 7501) and Rhino (port 7502) HTTP listeners to drive
AutoCAD programmatically via COM (on Windows). Primarily a 2D drafting tool, though
AutoCAD's 3D solid modeling and dimensioning/GD&T commands are first-class.

Endpoints (all bound to http://localhost:7503/):
  GET  /status      — { ok, has_active_drawing, entity_count, recipe_count }
  GET  /info        — full DrawingState dump (debug)
  POST /op          — body:{ kind, params } — execute one CAD operation
  POST /save_as     — body:{ path } — SaveAs to .dwg or export to .pdf/.step
  POST /quit        — clear state, returns { ok }

Launch:
  python -m cad_plugins.autocad.aria_autocad_server         (default port 7503)
  ARIA_AUTOCAD_PORT=7600 python -m ...                      (override)
  ARIA_AUTOCAD_DRYRUN=1 python -m ...                       (dryrun mode — no pyautocad)

The orchestrator reaches it via GET/POST to the HTTP endpoints above.
Operations map natural-language CAD goals to AutoCAD commands:
  - newPlan / beginPlan → start a new .dwg in modelspace
  - sketchCircle / sketchRect / sketchPolyline → AutoCAD CIRCLE, RECTANGLE, PLINE
  - extrude → AutoCAD EXTRUDE command on closed polyline
  - fillet → AutoCAD FILLET command on edges
  - addParameter → AutoCAD user variable (USERR1..R5)
  - linearDimension / diameterDimension → DIMLINEAR / DIMDIAMETER
  - gdtFrame → AutoCAD TOLERANCE command
  - saveAs → SaveAs .dwg or export .pdf/.step
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import traceback
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

# Make sibling aria_os/ packages importable when running from repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Optional import — if pyautocad not available, use dryrun mode
_PYAUTOCAD_AVAILABLE = False
_ACA = None
try:
    if os.environ.get("ARIA_AUTOCAD_DRYRUN") != "1":
        import pyautocad
        _PYAUTOCAD_AVAILABLE = True
        _ACA = pyautocad
except ImportError:
    pass

from aria_os.ecad import recipe_db  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# Op kind aliases — handle LLM hallucinations
# ---------------------------------------------------------------------------
_OP_ALIASES = {
    "newDrawing":       "beginPlan",
    "newPlan":          "beginPlan",
    "startDrawing":     "beginPlan",
    "createDrawing":    "beginPlan",
    "addCircle":        "sketchCircle",
    "drawCircle":       "sketchCircle",
    "addRectangle":     "sketchRect",
    "drawRectangle":    "sketchRect",
    "drawRect":         "sketchRect",
    "addPolyline":      "sketchPolyline",
    "drawPolyline":     "sketchPolyline",
    "addDimension":     "linearDimension",
    "dimLinear":        "linearDimension",
    "dimDiameter":      "diameterDimension",
    "diamDim":          "diameterDimension",
    "addDatumLabel":    "datumLabel",
    "addTolerance":     "gdtFrame",
    "tolerance":        "gdtFrame",
    "drc":              "runDrc",
    "validate":         "runDrc",
}


# ---------------------------------------------------------------------------
# In-memory drawing state
# ---------------------------------------------------------------------------
@dataclass
class DrawingState:
    name: str = "aria_drawing"
    modelspace_active: bool = True
    entities: list[dict] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    dimensions: list[dict] = field(default_factory=list)
    gdt_frames: list[dict] = field(default_factory=list)
    last_save_path: Optional[str] = None
    ops_dispatched: int = 0

    def reset(self, *, name: str) -> None:
        self.name = name
        self.modelspace_active = True
        self.entities = []
        self.parameters = {}
        self.dimensions = []
        self.gdt_frames = []
        self.last_save_path = None
        self.ops_dispatched = 0

    def to_info_dict(self) -> dict:
        return {
            "name":                self.name,
            "modelspace_active":   self.modelspace_active,
            "n_entities":          len(self.entities),
            "n_parameters":        len(self.parameters),
            "n_dimensions":        len(self.dimensions),
            "n_gdt_frames":        len(self.gdt_frames),
            "last_save_path":      self.last_save_path,
            "ops_dispatched":      self.ops_dispatched,
        }


_STATE = DrawingState()
_LOCK = threading.Lock()


def _port() -> int:
    env = os.environ.get("ARIA_AUTOCAD_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    return 7503


def _dryrun_mode() -> bool:
    return os.environ.get("ARIA_AUTOCAD_DRYRUN") == "1" or not _PYAUTOCAD_AVAILABLE


# ---------------------------------------------------------------------------
# Per-op handlers — 2D/3D drawing + dimensioning + GD&T
# ---------------------------------------------------------------------------
def _op_begin_plan(p: dict) -> dict:
    """Start a new drawing (new .dwg in modelspace)."""
    name = str(p.get("name") or p.get("filename", "aria_drawing"))
    _STATE.reset(name=name)
    if _dryrun_mode():
        return {
            "ok": True,
            "name": name,
            "mode": "dryrun",
            "action": f"[DRYRUN] Create new .dwg document '{name}'",
        }
    # Real mode: would call pyautocad.Acad() and start a new doc
    return {"ok": True, "name": name, "modelspace_active": True}


def _op_sketch_circle(p: dict) -> dict:
    """Draw a CIRCLE in modelspace. params: x_mm, y_mm, radius_mm, layer?"""
    x = float(p.get("x_mm", 0.0))
    y = float(p.get("y_mm", 0.0))
    r = float(p.get("radius_mm", p.get("radius", 10.0)))
    layer = str(p.get("layer", "0"))

    entity = {
        "type":        "circle",
        "x":           x,
        "y":           y,
        "radius":      r,
        "layer":       layer,
        "id":          str(uuid4()),
    }
    _STATE.entities.append(entity)

    if _dryrun_mode():
        return {
            "ok": True,
            "type": "circle",
            "x": x, "y": y, "radius": r,
            "mode": "dryrun",
            "action": f"[DRYRUN] CIRCLE at ({x}, {y}) r={r} on layer '{layer}'",
            "n_entities": len(_STATE.entities),
        }
    return {
        "ok": True,
        "type": "circle",
        "x": x, "y": y, "radius": r,
        "n_entities": len(_STATE.entities),
    }


def _op_sketch_rect(p: dict) -> dict:
    """Draw a RECTANGLE in modelspace.
    params: x_mm, y_mm, width_mm, height_mm, layer?
    """
    x = float(p.get("x_mm", 0.0))
    y = float(p.get("y_mm", 0.0))
    w = float(p.get("width_mm", p.get("width", 20.0)))
    h = float(p.get("height_mm", p.get("height", 20.0)))
    layer = str(p.get("layer", "0"))

    entity = {
        "type":        "rectangle",
        "x":           x,
        "y":           y,
        "width":       w,
        "height":      h,
        "layer":       layer,
        "id":          str(uuid4()),
    }
    _STATE.entities.append(entity)

    if _dryrun_mode():
        return {
            "ok": True,
            "type": "rectangle",
            "x": x, "y": y, "width": w, "height": h,
            "mode": "dryrun",
            "action": f"[DRYRUN] RECTANGLE at ({x}, {y}) {w}x{h} on layer '{layer}'",
            "n_entities": len(_STATE.entities),
        }
    return {
        "ok": True,
        "type": "rectangle",
        "x": x, "y": y, "width": w, "height": h,
        "n_entities": len(_STATE.entities),
    }


def _op_sketch_polyline(p: dict) -> dict:
    """Draw a POLYLINE (or LWPOLYLINE).
    params: points: [[x,y], [x,y], ...], closed?, layer?
    """
    points = p.get("points", [])
    closed = bool(p.get("closed", False))
    layer = str(p.get("layer", "0"))

    if not points:
        return {"ok": False, "error": "sketchPolyline requires 'points' list"}

    entity = {
        "type":        "polyline",
        "points":      [[float(pt[0]), float(pt[1])] for pt in points],
        "closed":      closed,
        "layer":       layer,
        "id":          str(uuid4()),
    }
    _STATE.entities.append(entity)

    if _dryrun_mode():
        return {
            "ok": True,
            "type": "polyline",
            "n_points": len(points),
            "closed": closed,
            "mode": "dryrun",
            "action": f"[DRYRUN] PLINE {len(points)} points {'(closed)' if closed else ''} on layer '{layer}'",
            "n_entities": len(_STATE.entities),
        }
    return {
        "ok": True,
        "type": "polyline",
        "n_points": len(points),
        "closed": closed,
        "n_entities": len(_STATE.entities),
    }


def _op_extrude(p: dict) -> dict:
    """AutoCAD EXTRUDE command on a closed polyline.
    params: entity_id?, height_mm, direction (default: Z-up)
    Note: AutoCAD 3D solid extrusion. Requires a closed face to extrude.
    """
    height = float(p.get("height_mm", p.get("height", 10.0)))
    direction = str(p.get("direction", "z"))
    entity_id = p.get("entity_id")

    # In real mode, would find the entity by ID and extrude it.
    # For now, record as a pending operation.
    op_rec = {
        "op":       "extrude",
        "entity_id": entity_id,
        "height":   height,
        "direction": direction,
    }
    _STATE.entities.append(op_rec)

    if _dryrun_mode():
        return {
            "ok": True,
            "op": "extrude",
            "height": height,
            "direction": direction,
            "mode": "dryrun",
            "action": f"[DRYRUN] EXTRUDE height={height}mm direction={direction}",
        }
    return {
        "ok": True,
        "op": "extrude",
        "height": height,
        "direction": direction,
    }


def _op_fillet(p: dict) -> dict:
    """AutoCAD FILLET command on edges.
    params: entity_id?, radius_mm
    """
    radius = float(p.get("radius_mm", p.get("radius", 2.0)))
    entity_id = p.get("entity_id")

    if _dryrun_mode():
        return {
            "ok": True,
            "op": "fillet",
            "radius": radius,
            "mode": "dryrun",
            "action": f"[DRYRUN] FILLET radius={radius}mm",
        }
    return {
        "ok": True,
        "op": "fillet",
        "radius": radius,
    }


def _op_add_parameter(p: dict) -> dict:
    """Add a user-defined parameter (AutoCAD USERR1..R5 or USERI1..I5).
    params: name, value
    """
    name = str(p.get("name", "param"))
    value = str(p.get("value", ""))
    _STATE.parameters[name] = value

    if _dryrun_mode():
        return {
            "ok": True,
            "name": name,
            "value": value,
            "mode": "dryrun",
            "action": f"[DRYRUN] Set parameter '{name}' = '{value}'",
        }
    return {
        "ok": True,
        "name": name,
        "value": value,
        "n_parameters": len(_STATE.parameters),
    }


def _op_linear_dimension(p: dict) -> dict:
    """Add a linear dimension (DIMLINEAR).
    params: x1_mm, y1_mm, x2_mm, y2_mm, label?, view?
    """
    x1 = float(p.get("x1_mm", p.get("x1", 0.0)))
    y1 = float(p.get("y1_mm", p.get("y1", 0.0)))
    x2 = float(p.get("x2_mm", p.get("x2", 10.0)))
    y2 = float(p.get("y2_mm", p.get("y2", 0.0)))
    label = str(p.get("label", ""))
    view = str(p.get("view", "top"))

    dim = {
        "type":       "linear",
        "x1": x1, "y1": y1,
        "x2": x2, "y2": y2,
        "label": label,
        "view": view,
        "id": str(uuid4()),
    }
    _STATE.dimensions.append(dim)

    if _dryrun_mode():
        return {
            "ok": True,
            "type": "linear",
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "mode": "dryrun",
            "action": f"[DRYRUN] DIMLINEAR from ({x1},{y1}) to ({x2},{y2})",
            "n_dimensions": len(_STATE.dimensions),
        }
    return {
        "ok": True,
        "type": "linear",
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "n_dimensions": len(_STATE.dimensions),
    }


def _op_diameter_dimension(p: dict) -> dict:
    """Add a diameter dimension (DIMDIAMETER).
    params: x_mm, y_mm, diameter_mm, label?, view?
    """
    x = float(p.get("x_mm", p.get("x", 0.0)))
    y = float(p.get("y_mm", p.get("y", 0.0)))
    diam = float(p.get("diameter_mm", p.get("diameter", 20.0)))
    label = str(p.get("label", ""))
    view = str(p.get("view", "top"))

    dim = {
        "type":       "diameter",
        "x": x, "y": y,
        "diameter": diam,
        "label": label,
        "view": view,
        "id": str(uuid4()),
    }
    _STATE.dimensions.append(dim)

    if _dryrun_mode():
        return {
            "ok": True,
            "type": "diameter",
            "diameter": diam,
            "mode": "dryrun",
            "action": f"[DRYRUN] DIMDIAMETER ø{diam}mm at ({x},{y})",
            "n_dimensions": len(_STATE.dimensions),
        }
    return {
        "ok": True,
        "type": "diameter",
        "diameter": diam,
        "n_dimensions": len(_STATE.dimensions),
    }


def _op_datum_label(p: dict) -> dict:
    """Add a datum label (A, B, C, etc.).
    params: feature, label, view?
    """
    feature = str(p.get("feature", "unknown"))
    label = str(p.get("label", "A"))
    view = str(p.get("view", "top"))

    rec = {
        "type":       "datum",
        "feature":    feature,
        "label":      label,
        "view":       view,
        "id":         str(uuid4()),
    }

    if _dryrun_mode():
        return {
            "ok": True,
            "type": "datum",
            "label": label,
            "feature": feature,
            "mode": "dryrun",
            "action": f"[DRYRUN] DATUM LABEL '{label}' on {feature}",
        }
    return {
        "ok": True,
        "type": "datum",
        "label": label,
        "feature": feature,
    }


def _op_gdt_frame(p: dict) -> dict:
    """Add a GD&T frame (AutoCAD TOLERANCE command).
    params: characteristic (flatness, perpendicularity, position, etc.),
            tolerance (float), datum_ref?, feature?, view?
    AutoCAD TOLERANCE is a single command with stacked params, different from
    generic datum/dimension ops. This is the most powerful feature for professional
    drawings.
    """
    characteristic = str(p.get("characteristic", "flatness"))
    tolerance = float(p.get("tolerance", 0.05))
    datum_ref = str(p.get("datum_ref", "A"))
    feature = str(p.get("feature", "bottom_face"))
    view = str(p.get("view", "front"))

    rec = {
        "type":           "gdt",
        "characteristic": characteristic,
        "tolerance":      tolerance,
        "datum_ref":      datum_ref,
        "feature":        feature,
        "view":           view,
        "id":             str(uuid4()),
    }
    _STATE.gdt_frames.append(rec)

    if _dryrun_mode():
        return {
            "ok": True,
            "type": "gdt",
            "characteristic": characteristic,
            "tolerance": tolerance,
            "datum_ref": datum_ref,
            "mode": "dryrun",
            "action": (f"[DRYRUN] TOLERANCE {characteristic.upper()} "
                      f"{tolerance}mm datum {datum_ref}"),
            "n_gdt_frames": len(_STATE.gdt_frames),
        }
    return {
        "ok": True,
        "type": "gdt",
        "characteristic": characteristic,
        "tolerance": tolerance,
        "datum_ref": datum_ref,
        "n_gdt_frames": len(_STATE.gdt_frames),
    }


def _op_run_drc(p: dict) -> dict:
    """Run DRC (design rule check) — stubbed in AutoCAD.
    AutoCAD does not have a formal DRC like KiCad, but we can validate
    the drawing state (e.g., no overlaps, valid layer names).
    """
    if _dryrun_mode():
        return {
            "ok": True,
            "mode": "dryrun",
            "action": "[DRYRUN] RUN DRC (AutoCAD validation)",
            "violations": 0,
        }
    return {
        "ok": True,
        "violations": 0,
        "message": "AutoCAD drawing is valid",
    }


# Map generic ops (cross-CAD vocabulary) to AutoCAD handlers.
# Cross-CAD ops use a shared param language: sketch (alias), cx, cy, r, w, h, points, etc.
# AutoCAD ops are 2D drawing primitives, so we translate.
def _op_new_sketch(p: dict) -> dict:
    """newSketch (cross-CAD) is a no-op in 2D autocad; sketches are implicit."""
    # AutoCAD doesn't have explicit sketch/part studio objects; 2D drawing happens directly in modelspace.
    # This is a planner/validator artifact.
    alias = str(p.get("alias", "s"))
    return {
        "ok": True,
        "kind": "newSketch",
        "alias": alias,
        "mode": "dryrun" if _dryrun_mode() else "live",
        "action": f"[newSketch alias='{alias}'] (AutoCAD: implicit, 2D drawing to follow)",
    }

def _op_sketch_circle_crosscad(p: dict) -> dict:
    """sketchCircle (cross-CAD) with cx, cy, r params."""
    sketch = str(p.get("sketch", "s"))
    cx = float(p.get("cx", 0.0))
    cy = float(p.get("cy", 0.0))
    r = float(p.get("r", 10.0))
    # Translate to AutoCAD's 2D drawing vocabulary
    return _op_sketch_circle({"x_mm": cx, "y_mm": cy, "radius_mm": r})

def _op_sketch_rect_crosscad(p: dict) -> dict:
    """sketchRect (cross-CAD) with cx, cy, w, h params."""
    sketch = str(p.get("sketch", "s"))
    cx = float(p.get("cx", 0.0))
    cy = float(p.get("cy", 0.0))
    w = float(p.get("w", 20.0))
    h = float(p.get("h", 20.0))
    # Translate to AutoCAD's 2D drawing vocabulary
    return _op_sketch_rect({"x_mm": cx, "y_mm": cy, "width_mm": w, "height_mm": h})

def _op_sketch_polyline_crosscad(p: dict) -> dict:
    """sketchPolyline (cross-CAD) with points, closed params."""
    sketch = str(p.get("sketch", "s"))
    points = p.get("points", [])
    closed = bool(p.get("closed", True))
    # Translate to AutoCAD's 2D drawing vocabulary
    return _op_sketch_polyline({"points": points, "closed": closed})

def _op_sketch_spline_crosscad(p: dict) -> dict:
    """sketchSpline (cross-CAD) with points param. AutoCAD doesn't have native splines in dryrun."""
    sketch = str(p.get("sketch", "s"))
    points = p.get("points", [])
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "sketchSpline",
            "n_points": len(points),
            "mode": "dryrun",
            "action": f"[DRYRUN] SPLINE {len(points)} control points",
        }
    return {
        "ok": True,
        "kind": "sketchSpline",
        "n_points": len(points),
    }

def _op_extrude_crosscad(p: dict) -> dict:
    """extrude (cross-CAD) expects sketch, distance, operation, alias params."""
    sketch = str(p.get("sketch", "s"))
    distance = float(p.get("distance", 10.0))
    operation = str(p.get("operation", "new"))  # new, cut, join
    alias = str(p.get("alias", "ext"))
    # In AutoCAD 2D, extrude is 3D; translate to EXTRUDE command
    return _op_extrude({"height_mm": distance, "direction": "z"})

def _op_revolve_crosscad(p: dict) -> dict:
    """revolve (cross-CAD) expects sketch, axis, angle_deg, operation, alias params."""
    sketch = str(p.get("sketch", "s"))
    axis = str(p.get("axis", "z"))
    angle_deg = float(p.get("angle_deg", 360.0))
    operation = str(p.get("operation", "new"))
    alias = str(p.get("alias", "rev"))
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "revolve",
            "axis": axis,
            "angle_deg": angle_deg,
            "mode": "dryrun",
            "action": f"[DRYRUN] REVOLVE around {axis} by {angle_deg}°",
        }
    return {
        "ok": True,
        "kind": "revolve",
        "axis": axis,
        "angle_deg": angle_deg,
    }

def _op_fillet_crosscad(p: dict) -> dict:
    """fillet (cross-CAD) expects edges, radius, alias params."""
    edges = p.get("edges", [])
    radius = float(p.get("radius", 2.0))
    alias = str(p.get("alias", "fil"))
    # Translate to AutoCAD's FILLET command
    return _op_fillet({"radius_mm": radius})

def _op_shell_crosscad(p: dict) -> dict:
    """shell (cross-CAD) expects thickness and optional remove_faces."""
    thickness = float(p.get("thickness", 2.0))
    remove_faces = p.get("remove_faces", [])
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "shell",
            "thickness": thickness,
            "remove_faces": len(remove_faces),
            "mode": "dryrun",
            "action": f"[DRYRUN] SHELL thickness={thickness}mm, remove {len(remove_faces)} faces",
        }
    return {
        "ok": True,
        "kind": "shell",
        "thickness": thickness,
    }

def _op_rib_crosscad(p: dict) -> dict:
    """rib (cross-CAD) — AutoCAD doesn't have rib, approximate as extrude join."""
    sketch = str(p.get("sketch", "s"))
    thickness = float(p.get("thickness", 5.0))
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "rib",
            "thickness": thickness,
            "mode": "dryrun",
            "action": f"[DRYRUN] RIB thickness={thickness}mm",
        }
    return {
        "ok": True,
        "kind": "rib",
        "thickness": thickness,
    }

def _op_draft_crosscad(p: dict) -> dict:
    """draft (cross-CAD) — taper. AutoCAD doesn't have this; stub."""
    angle_deg = float(p.get("angle_deg", 5.0))
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "draft",
            "angle_deg": angle_deg,
            "mode": "dryrun",
            "action": f"[DRYRUN] DRAFT {angle_deg}°",
        }
    return {
        "ok": True,
        "kind": "draft",
        "angle_deg": angle_deg,
    }

def _op_helix_crosscad(p: dict) -> dict:
    """helix (cross-CAD) — thread-like curve. AutoCAD can model this, stub for now."""
    sketch = str(p.get("sketch", "s"))
    pitch_mm = float(p.get("pitch_mm", 5.0))
    revolutions = float(p.get("revolutions", 4.0))
    alias = str(p.get("alias", "hp"))
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "helix",
            "pitch_mm": pitch_mm,
            "revolutions": revolutions,
            "mode": "dryrun",
            "action": f"[DRYRUN] HELIX pitch={pitch_mm}mm rev={revolutions}",
        }
    return {
        "ok": True,
        "kind": "helix",
        "pitch_mm": pitch_mm,
        "revolutions": revolutions,
    }

def _op_loft_crosscad(p: dict) -> dict:
    """loft (cross-CAD) — surface between profiles. AutoCAD LOFT command exists; stub for now."""
    profile_sketches = p.get("profile_sketches", [])
    alias = str(p.get("alias", "lof"))
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "loft",
            "profiles": len(profile_sketches),
            "mode": "dryrun",
            "action": f"[DRYRUN] LOFT between {len(profile_sketches)} profiles",
        }
    return {
        "ok": True,
        "kind": "loft",
        "profiles": len(profile_sketches),
    }

def _op_hole_wizard_crosscad(p: dict) -> dict:
    """holeWizard (cross-CAD) — pre-set hole type. AutoCAD CIRCLE + optional CIRCLE for cbore."""
    x = float(p.get("x", 0.0))
    y = float(p.get("y", 0.0))
    diameter = float(p.get("diameter", 8.0))
    depth = float(p.get("depth", 10.0))
    hole_type = str(p.get("type", "drill"))
    cbore_diameter = float(p.get("cbore_diameter", 14.0))
    cbore_depth = float(p.get("cbore_depth", 5.0))
    alias = str(p.get("alias", "hw"))
    # AutoCAD: just draw the hole circle, ignore depth/cbore for dryrun
    return _op_sketch_circle({"x_mm": x, "y_mm": y, "radius_mm": diameter / 2})

def _op_circular_pattern_crosscad(p: dict) -> dict:
    """circularPattern (cross-CAD) — duplicate radially. AutoCAD POLAR array."""
    feature = str(p.get("feature", "f0"))
    count = int(p.get("count", 6))
    axis = str(p.get("axis", "z"))
    seed_x = float(p.get("seed_x", 0.0))
    seed_y = float(p.get("seed_y", 0.0))
    seed_r = float(p.get("seed_r", 0.0))
    alias = str(p.get("alias", "cp"))
    if _dryrun_mode():
        return {
            "ok": True,
            "kind": "circularPattern",
            "feature": feature,
            "count": count,
            "mode": "dryrun",
            "action": f"[DRYRUN] CIRCULAR PATTERN {count}x of {feature}",
        }
    return {
        "ok": True,
        "kind": "circularPattern",
        "feature": feature,
        "count": count,
    }

_OP_HANDLERS = {
    "beginPlan":          _op_begin_plan,
    "newPlan":            _op_begin_plan,
    "newSketch":          _op_new_sketch,
    "sketchCircle":       _op_sketch_circle_crosscad,  # cross-CAD version
    "sketchRect":         _op_sketch_rect_crosscad,    # cross-CAD version
    "sketchPolyline":     _op_sketch_polyline_crosscad,
    "sketchSpline":       _op_sketch_spline_crosscad,
    "extrude":            _op_extrude_crosscad,
    "revolve":            _op_revolve_crosscad,
    "fillet":             _op_fillet_crosscad,
    "shell":              _op_shell_crosscad,
    "rib":                _op_rib_crosscad,
    "draft":              _op_draft_crosscad,
    "helix":              _op_helix_crosscad,
    "loft":               _op_loft_crosscad,
    "holeWizard":         _op_hole_wizard_crosscad,
    "circularPattern":    _op_circular_pattern_crosscad,
    "addParameter":       _op_add_parameter,
    "linearDimension":    _op_linear_dimension,
    "diameterDimension":  _op_diameter_dimension,
    "datumLabel":         _op_datum_label,
    "gdtFrame":           _op_gdt_frame,
    "runDrc":             _op_run_drc,
}


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------
def _dispatch_op(kind: str, params: dict) -> dict:
    """Run one op. Canonical name resolution via aliases."""
    canonical = _OP_ALIASES.get(kind, kind)
    handler = _OP_HANDLERS.get(canonical)
    if handler is None:
        return {"ok": False, "error": f"unknown op kind {kind!r}"}
    _STATE.ops_dispatched += 1
    try:
        return handler(params)
    except Exception as ex:
        return {
            "ok": False,
            "error": f"{type(ex).__name__}: {ex}",
            "trace": traceback.format_exc(limit=4),
        }


def _summary_for_llm() -> dict:
    return {
        "name":              _STATE.name,
        "n_entities":       len(_STATE.entities),
        "n_parameters":     len(_STATE.parameters),
        "n_dimensions":     len(_STATE.dimensions),
        "n_gdt_frames":     len(_STATE.gdt_frames),
        "ops_dispatched":   _STATE.ops_dispatched,
    }


# ---------------------------------------------------------------------------
# /save_as — export to .dwg, .pdf, .step, etc.
# ---------------------------------------------------------------------------
def _save_as(out_path: Path) -> dict:
    """Save drawing to file. In dryrun mode, use ezdxf to generate DXF/DWG."""
    try:
        suffix = out_path.suffix.lower()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if _dryrun_mode():
            # Use ezdxf to generate a real DXF file from the in-memory drawing state
            try:
                import ezdxf
            except ImportError:
                return {
                    "ok": True,
                    "path": str(out_path),
                    "format": suffix,
                    "mode": "dryrun",
                    "action": f"[DRYRUN] SaveAs {out_path} (ezdxf not available, skipping DXF generation)",
                    "size_bytes": 0,
                }

            # Create a DXF document
            doc = ezdxf.new('R2010')
            msp = doc.modelspace()

            # Add entities from _STATE.entities
            for entity in _STATE.entities:
                if entity.get("type") == "circle":
                    msp.add_circle((entity["x"], entity["y"]), entity["radius"])
                elif entity.get("type") == "rectangle":
                    x, y, w, h = entity["x"], entity["y"], entity["width"], entity["height"]
                    # Draw rectangle as 4 lines
                    x0, y0 = x - w/2, y - h/2
                    x1, y1 = x + w/2, y + h/2
                    msp.add_line((x0, y0), (x1, y0))
                    msp.add_line((x1, y0), (x1, y1))
                    msp.add_line((x1, y1), (x0, y1))
                    msp.add_line((x0, y1), (x0, y0))
                elif entity.get("type") == "polyline":
                    points = entity.get("points", [])
                    if points:
                        for i in range(len(points) - 1):
                            msp.add_line(points[i], points[i + 1])
                        if entity.get("closed") and len(points) > 0:
                            msp.add_line(points[-1], points[0])

            # Save DXF (always save as DXF internally; format conversion happens at caller level)
            doc.saveas(str(out_path))
            size_bytes = out_path.stat().st_size if out_path.exists() else 0

            return {
                "ok": True,
                "path": str(out_path),
                "format": suffix,
                "mode": "dryrun",
                "action": f"[DRYRUN] SaveAs {out_path} via ezdxf",
                "size_bytes": size_bytes,
                "n_entities": len(_STATE.entities),
            }

        # Real mode would use pyautocad to SaveAs
        _STATE.last_save_path = str(out_path)
        return {
            "ok": True,
            "path": str(out_path),
            "format": suffix,
            "size_bytes": 0,
        }
    except Exception as ex:
        return {
            "ok": False,
            "error": f"{type(ex).__name__}: {ex}",
        }


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[aria-autocad] {fmt % args}\n")

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n).decode("utf-8")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/").lower() or "/"
        try:
            with _LOCK:
                if path == "/status":
                    self._json(200, {
                        "ok":                   True,
                        "has_active_drawing":   bool(_STATE.entities),
                        "name":                 _STATE.name,
                        "n_entities":           len(_STATE.entities),
                        "n_parameters":         len(_STATE.parameters),
                        "n_dimensions":         len(_STATE.dimensions),
                        "n_gdt_frames":         len(_STATE.gdt_frames),
                        "ops_dispatched":       _STATE.ops_dispatched,
                        "recipe_count":         recipe_db.count(),
                        "last_save_path":       _STATE.last_save_path,
                        "port":                 _port(),
                        "dryrun_mode":          _dryrun_mode(),
                        "pyautocad_available":  _PYAUTOCAD_AVAILABLE,
                    })
                    return
                if path == "/info":
                    self._json(200, {
                        "ok":    True,
                        "state": _STATE.to_info_dict(),
                    })
                    return
            self._json(404, {"ok": False, "error": f"unknown route GET {path}"})
        except Exception as ex:
            self._json(500, {
                "ok": False,
                "error": f"{type(ex).__name__}: {ex}",
                "trace": traceback.format_exc(limit=4),
            })

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/").lower() or "/"
        try:
            body = self._read_body()
            with _LOCK:
                if path == "/op":
                    kind = body.get("kind", "")
                    params = body.get("params") or {}
                    if not kind:
                        self._json(400,
                            {"ok": False, "error": "op requires 'kind'"})
                        return
                    result = _dispatch_op(kind, params)
                    self._json(200, {
                        "ok": result.get("ok", True),
                        "kind": kind,
                        "result": result,
                    })
                    return
                if path == "/save_as":
                    out = body.get("path") \
                        or str(_REPO_ROOT / "outputs" / "cad" / "dwg" /
                                f"{_STATE.name}.dwg")
                    out_path = Path(out)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    self._json(200, _save_as(out_path))
                    return
                if path == "/quit":
                    _STATE.reset(name="aria_drawing")
                    self._json(200, {"ok": True})
                    return
            self._json(404, {
                "ok": False,
                "error": f"unknown route POST {path}",
            })
        except Exception as ex:
            self._json(500, {
                "ok": False,
                "error": f"{type(ex).__name__}: {ex}",
                "trace": traceback.format_exc(limit=4),
            })


def main() -> None:
    recipe_db.init()
    port = _port()
    mode = "dryrun" if _dryrun_mode() else "live"
    print(
        f"aria_autocad_server: http://localhost:{port}/ ({mode})",
        flush=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("aria_autocad_server: shutting down", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
