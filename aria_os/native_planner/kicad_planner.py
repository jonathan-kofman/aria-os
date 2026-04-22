"""KiCad native planner — emits ECAD feature ops the same way the
mechanical planners emit sketch/extrude/pattern ops.

Unlike Fusion/Rhino/Onshape, KiCad has no live WebView bridge we can stream
into. So the panel's `bridge.executeFeature(kind, params)` has no host to
dispatch to for KiCad ops. Instead we run the executor SERVER-SIDE: the
ops land in a growing `.kicad_pcb` file on disk that the user opens in
KiCad. Each op still emits a `native_op` event so the panel's feature
tree fills in live — the user sees what's being placed/routed even
though KiCad itself isn't driven interactively.

Ops (ECAD domain — separate from mechanical's sketch/extrude set):
  beginBoard    : {width_mm, height_mm, layers}         -- new PCB
  setStackup    : {layers: [...]}                        -- layer config
  placeComponent: {ref, footprint, x_mm, y_mm, rot_deg, layer}
  addTrack      : {net, x1_mm, y1_mm, x2_mm, y2_mm, width_mm, layer}
  addVia        : {net, x_mm, y_mm, drill_mm, diameter_mm}
  addZone       : {net, layer, polygon: [[x,y],...]}
  addNet        : {name}
"""
from __future__ import annotations


def plan_led_board(spec: dict) -> list[dict]:
    """A minimal working ECAD plan — single LED + current-limit resistor,
    USB-C power, 2-layer FR-4 board.

    Dimensions default to a 30×20mm board. Used as the first KiCad
    smoke-test; equivalent to `plan_flange` on the mechanical side.
    """
    w = float(spec.get("width_mm",  30.0))
    h = float(spec.get("height_mm", 20.0))

    plan: list[dict] = [
        {"kind": "beginBoard",
         "params": {"width_mm": w, "height_mm": h, "layers": 2,
                    "name": "ARIA LED Demo"},
         "label": f"New board {w:g}×{h:g}mm, 2-layer FR-4"},
        {"kind": "setStackup",
         "params": {"layers": ["F.Cu", "B.Cu"],
                    "dielectric_mm": 1.6, "material": "FR4"},
         "label": "2-layer stackup, 1.6mm FR-4"},
        {"kind": "addNet",
         "params": {"name": "VCC"},
         "label": "Net: VCC (+5V)"},
        {"kind": "addNet",
         "params": {"name": "GND"},
         "label": "Net: GND"},
        {"kind": "addNet",
         "params": {"name": "N_LED"},
         "label": "Net: N_LED (LED anode)"},
        # Components
        {"kind": "placeComponent",
         "params": {"ref": "J1", "footprint": "Connector_USB:USB_C_Receptacle",
                    "x_mm": 2.0, "y_mm": h/2, "rot_deg": 0, "layer": "F.Cu"},
         "label": "Place J1 (USB-C) at left edge"},
        {"kind": "placeComponent",
         "params": {"ref": "R1", "footprint": "Resistor_SMD:R_0603_1608Metric",
                    "x_mm": w/2 - 3, "y_mm": h/2, "rot_deg": 0, "layer": "F.Cu"},
         "label": "Place R1 (0603 330Ω) mid-board"},
        {"kind": "placeComponent",
         "params": {"ref": "D1", "footprint": "LED_SMD:LED_0805_2012Metric",
                    "x_mm": w - 5, "y_mm": h/2, "rot_deg": 0, "layer": "F.Cu"},
         "label": "Place D1 (0805 LED) at right edge"},
        # Tracks (simplified — real routing would use net assignments)
        {"kind": "addTrack",
         "params": {"net": "VCC", "x1_mm": 2.0, "y1_mm": h/2,
                    "x2_mm": w/2 - 3, "y2_mm": h/2,
                    "width_mm": 0.25, "layer": "F.Cu"},
         "label": "Route VCC → R1"},
        {"kind": "addTrack",
         "params": {"net": "N_LED", "x1_mm": w/2 - 3, "y1_mm": h/2,
                    "x2_mm": w - 5, "y2_mm": h/2,
                    "width_mm": 0.25, "layer": "F.Cu"},
         "label": "Route R1 → D1"},
        {"kind": "addTrack",
         "params": {"net": "GND", "x1_mm": w - 5, "y1_mm": h/2 + 1.5,
                    "x2_mm": 2.0, "y2_mm": h/2 + 1.5,
                    "width_mm": 0.3, "layer": "B.Cu"},
         "label": "Route GND (B.Cu)"},
        # Ground zone on bottom layer
        {"kind": "addZone",
         "params": {"net": "GND", "layer": "B.Cu",
                    "polygon": [[0, 0], [w, 0], [w, h], [0, h]]},
         "label": "Pour GND zone on B.Cu"},
        # Auto-route all remaining nets via Freerouting (server-side)
        {"kind": "routeBoard",
         "params": {"timeout_s": 90},
         "label": "Auto-route all nets (Freerouting)"},
    ]
    return plan
