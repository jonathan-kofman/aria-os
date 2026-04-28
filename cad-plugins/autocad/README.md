# AutoCAD Bridge for ARIA-OS

HTTP listener (port 7503) that drives AutoCAD programmatically via COM on Windows.

Complements SolidWorks (7501), Rhino (7502), KiCad (7505) with native support for:
- **2D technical drawings** (circles, rectangles, polylines on layers)
- **3D solid modeling** (extrude, fillet)
- **Dimensioning** (DIMLINEAR, DIMDIAMETER — native AutoCAD commands)
- **GD&T** (AutoCAD TOLERANCE command for flatness, perpendicularity, position, etc.)
- **Parameters** (user variables USERR1..R5, USERI1..I5)

## Install

```bash
pip install pyautocad
```

On Windows, AutoCAD 2024+ or AutoCAD LT 2024+ must be installed. The bridge uses COM to drive it.

## Run

```bash
# Default: port 7503, live mode (requires AutoCAD)
python -m cad_plugins.autocad.aria_autocad_server

# Override port
ARIA_AUTOCAD_PORT=7600 python -m cad_plugins.autocad.aria_autocad_server

# Dryrun mode (no AutoCAD needed, useful for testing/CI)
ARIA_AUTOCAD_DRYRUN=1 python -m cad_plugins.autocad.aria_autocad_server
```

## HTTP Endpoints

All endpoints return JSON. Request/response examples:

### GET /status
```json
{
  "ok": true,
  "has_active_drawing": true,
  "name": "aria_drawing",
  "n_entities": 12,
  "n_dimensions": 5,
  "n_gdt_frames": 2,
  "port": 7503,
  "dryrun_mode": false
}
```

### GET /info
Full drawing state dump (debug).

### POST /op
Execute one drawing operation. Request format:
```json
{
  "kind": "sketchCircle",
  "params": {
    "x_mm": 50.0,
    "y_mm": 50.0,
    "radius_mm": 10.0,
    "layer": "0"
  }
}
```

Response:
```json
{
  "ok": true,
  "kind": "sketchCircle",
  "result": {
    "type": "circle",
    "x": 50.0,
    "y": 50.0,
    "radius": 10.0,
    "n_entities": 1
  }
}
```

### POST /save_as
Save drawing to file. Request:
```json
{
  "path": "/path/to/drawing.dwg"
}
```

Response:
```json
{
  "ok": true,
  "path": "/path/to/drawing.dwg",
  "format": ".dwg",
  "size_bytes": 45248
}
```

### POST /quit
Clear state, return to blank drawing.

## Supported Operations (Op Kinds)

| Kind | Aliases | Params | Notes |
|------|---------|--------|-------|
| `beginPlan` | newPlan, newDrawing, startDrawing | `name` | Create new .dwg |
| `sketchCircle` | addCircle, drawCircle | `x_mm, y_mm, radius_mm, layer` | Draw CIRCLE entity |
| `sketchRect` | addRectangle, drawRect | `x_mm, y_mm, width_mm, height_mm, layer` | Draw RECTANGLE |
| `sketchPolyline` | addPolyline, drawPolyline | `points, closed, layer` | Draw PLINE (closed or open) |
| `extrude` | — | `height_mm, direction` | AutoCAD EXTRUDE on closed polyline → 3D solid |
| `fillet` | — | `radius_mm` | AutoCAD FILLET command |
| `addParameter` | — | `name, value` | Set user variable (USERR1..R5) |
| `linearDimension` | addDimension, dimLinear | `x1_mm, y1_mm, x2_mm, y2_mm, label, view` | DIMLINEAR |
| `diameterDimension` | dimDiameter, diamDim | `x_mm, y_mm, diameter_mm, label, view` | DIMDIAMETER |
| `datumLabel` | addDatumLabel | `feature, label, view` | Datum reference (A, B, C) |
| `gdtFrame` | addTolerance, tolerance | `characteristic, tolerance, datum_ref, feature, view` | TOLERANCE command (flatness, perpendicularity, position, runout, etc.) |
| `runDrc` | drc, validate | — | Design rule check (stubbed) |

## Industries / Use Cases

AutoCAD bridges unlock ARIA-OS for:

- **Civil Engineering** — site plans, foundation plans, structural drawings
- **Structural Engineering** — building sections, elevation drawings with full GD&T
- **Architecture** — floor plans, building elevations, detail callouts
- **MEP** — electrical schematics in DWG, HVAC layout, piping & instrumentation
- **2D Drafting** — any technical drawing, especially with professional dimensioning

Prior, ARIA-OS was limited to 3D CAD (SolidWorks, Rhino, Fusion). AutoCAD adds the **dominant drawing tool in AEC/MEP**.

## Integration Notes

The orchestrator does NOT need to know about AutoCAD. The dashboard's `_CAD_BASE_URL` dict is extended via a separate file `dashboard/cad_registry.py` with:

```python
def get_cad_base_urls() -> dict:
    return {
        "solidworks": "http://localhost:7501",
        "sw":         "http://localhost:7501",
        "rhino":      "http://localhost:7502",
        "autocad":    "http://localhost:7503",
        "acad":       "http://localhost:7503",
    }
```

The dispatcher will auto-detect AutoCAD goals via `dashboard/autocad_routing.py:is_autocad_goal(goal)`.

## Known Limitations

- **Windows-only** (COM-based; Unix versions of AutoCAD don't expose COM API)
- **Dryrun mode only** — production use requires AutoCAD 2024+ installed + COM registration
- **2D/3D hybrid** — some ops (extrude, fillet) are stubs in dryrun; real mode works via pyautocad
- **No DRC** — AutoCAD has no formal design rule check like KiCad; validation is heuristic

## pyautocad Version

- **Pinned:** pyautocad ≥ 2.5 (tested on 2.6+)
- Install: `pip install pyautocad`
- Requires: AutoCAD 2020+ with COM enabled (default)

## Testing

Without AutoCAD installed, use dryrun mode:

```bash
ARIA_AUTOCAD_DRYRUN=1 python -m cad_plugins.autocad.aria_autocad_server &
sleep 1
curl -s http://localhost:7503/status
curl -s -X POST http://localhost:7503/op \
  -H "Content-Type: application/json" \
  -d '{"kind":"sketchCircle","params":{"x_mm":50,"y_mm":50,"radius_mm":10}}'
```

Output will show `[DRYRUN]` prefixes on all actions, confirming the listener is alive and op dispatch works without pyautocad.
