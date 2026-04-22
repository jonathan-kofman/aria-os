"""Fusion Electronics planner — emits ops the Fusion add-in dispatches
into the native Electronics workspace (adsk.electron). The Op set
mirrors the KiCad planner where possible so the same user prompt can
target either backend:

    beginElectronics   → new Electron document
    boardOutline       → set PCB board rectangle
    placeSymbol        → schematic symbol placement
    placeFootprint     → PCB footprint placement
    addConnection      → named net with pin connections

Autorouting is out of scope — Fusion's interactive router finishes the
board. The planner leaves the PCB populated with placed footprints and
declared nets so the user picks up in Fusion's native ECAD tools.
"""
from __future__ import annotations


def plan_led_board_fusion(spec: dict) -> list[dict]:
    """Fusion-flavored equivalent of `plan_led_board` — same LED demo
    but using Fusion's Eagle library names (supply1, rcl, led)."""
    w = float(spec.get("width_mm",  30.0))
    h = float(spec.get("height_mm", 20.0))
    return [
        {"kind": "beginElectronics", "params": {},
         "label": "New Fusion Electronics document"},
        {"kind": "boardOutline",
         "params": {"width_mm": w, "height_mm": h},
         "label": f"Board outline {w:g}×{h:g}mm"},
        # Schematic: three symbols
        {"kind": "placeSymbol",
         "params": {"library": "con-hirose", "device": "USB-C",
                    "ref": "J1", "x_mm": 10, "y_mm": 40},
         "label": "Schematic: J1 USB-C"},
        {"kind": "placeSymbol",
         "params": {"library": "resistor", "device": "R-EU_0603",
                    "ref": "R1", "x_mm": 40, "y_mm": 40},
         "label": "Schematic: R1 330Ω 0603"},
        {"kind": "placeSymbol",
         "params": {"library": "led", "device": "LED-0805",
                    "ref": "D1", "x_mm": 70, "y_mm": 40},
         "label": "Schematic: D1 LED 0805"},
        # Nets connecting the pins
        {"kind": "addConnection",
         "params": {"name": "VCC",
                    "connect": [["J1", "VBUS"], ["R1", "1"]]},
         "label": "Net VCC: J1.VBUS ↔ R1.1"},
        {"kind": "addConnection",
         "params": {"name": "N_LED",
                    "connect": [["R1", "2"], ["D1", "A"]]},
         "label": "Net N_LED: R1.2 ↔ D1.A"},
        {"kind": "addConnection",
         "params": {"name": "GND",
                    "connect": [["J1", "GND"], ["D1", "K"]]},
         "label": "Net GND: J1.GND ↔ D1.K"},
        # PCB footprint placement
        {"kind": "placeFootprint",
         "params": {"library": "con-hirose", "package": "USB-C-SMD",
                    "ref": "J1", "x_mm": 3.0, "y_mm": h/2,
                    "rot_deg": 0, "side": "top"},
         "label": "PCB: J1 at left edge"},
        {"kind": "placeFootprint",
         "params": {"library": "rcl", "package": "R0603",
                    "ref": "R1", "x_mm": w/2, "y_mm": h/2,
                    "rot_deg": 0, "side": "top"},
         "label": "PCB: R1 mid-board"},
        {"kind": "placeFootprint",
         "params": {"library": "led", "package": "LED-0805",
                    "ref": "D1", "x_mm": w - 3.0, "y_mm": h/2,
                    "rot_deg": 0, "side": "top"},
         "label": "PCB: D1 at right edge"},
    ]
