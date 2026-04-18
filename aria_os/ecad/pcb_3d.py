"""
Populated PCB 3D model — generate a STEP file showing the PCB substrate with
all components as 3D bumps at their placed positions. Far more useful in
the main CAD assembly than a plain green rectangle.

Reads the BOM JSON produced by `generate_ecad()` and produces:
  - PCB substrate (FR-4 green, default 1.6 mm thick)
  - Each component extruded as a 3D body at (x_mm, y_mm) with width × height
    footprint and a height-above-board scaled by component class:
      Connectors (J*, USB-C):  6-9 mm tall
      ICs / MCU (U*):          1-3 mm tall
      Capacitors (C*):         1-2 mm tall
      Resistors (R*):          0.5-1 mm tall
      Antennas (ANT*):         0.5 mm tall (just a marker)

Each component is unioned to the substrate so the result is a single solid
suitable for assembly into the main drone CAD.

Usage:
    from aria_os.ecad.pcb_3d import build_populated_pcb
    step_path = build_populated_pcb(bom_json_path, out_step_path)
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


# Component height-above-board lookup. Approximate real-world heights.
_HEIGHT_MM = {
    # Connectors
    "USB-C":          3.5,
    "JST-PH":         5.5,
    "JST-XH":         6.5,
    "JST-GH":         4.5,
    "JST-SH":         3.5,
    "XT30":           7.0,
    "XT60":           8.0,
    "Barrel":         10.0,
    "PinHeader":      8.5,   # 0.1" pin header
    "Molex":          11.0,
    "KF350":          11.0,
    # Major ICs (heights of common packages)
    "STM32":          1.4,
    "ESP32":          3.5,
    "RP2040":         1.0,
    "ATmega":         3.5,
    "AMS1117":        1.7,   # SOT-223
    "MPU":            1.2,   # LGA-24
    "ICM":            1.0,
    "BMP":            0.8,
    "BMI":            0.8,
    "MS5611":         3.0,
    "QMC5883":        1.0,
    "HMC5883":        1.0,
    "L298":           28.0,  # DIP-20 with heatsink
    "TP4056":         1.7,
    "HX711":          2.6,
    "BLE":            1.0,
    # Passives
    "C":              1.0,   # generic ceramic
    "R":              0.5,   # 0603 / 0805
    "L":              1.5,
    "D":              1.0,
    "ANT":            0.6,
    "LED":            0.8,
}

# Color (RGB 0-1) per component class
_COLOR_RGB = {
    "U":   (0.10, 0.12, 0.15),  # IC — black
    "J":   (0.85, 0.55, 0.20),  # connector — orange/gold
    "C":   (0.85, 0.78, 0.50),  # ceramic cap — tan
    "R":   (0.05, 0.05, 0.05),  # resistor — black
    "L":   (0.50, 0.30, 0.10),  # inductor — brown
    "D":   (0.20, 0.20, 0.20),  # diode — dark gray
    "ANT": (0.60, 0.60, 0.60),  # antenna — silver
    "LED": (1.00, 0.20, 0.20),  # LED — red
}


def _component_height_mm(value: str, ref: str) -> float:
    """Best guess of component Z height for visual fidelity."""
    v = value.upper()
    # Specific value match first
    for key, h in _HEIGHT_MM.items():
        if key.upper() in v:
            return h
    # Fallback by ref prefix
    prefix = "".join(c for c in ref if c.isalpha()).upper()
    return _HEIGHT_MM.get(prefix, 1.0)


def _component_color(ref: str) -> tuple[float, float, float]:
    prefix = "".join(c for c in ref if c.isalpha()).upper()
    return _COLOR_RGB.get(prefix, (0.5, 0.5, 0.5))


def build_populated_pcb(
    bom_path: str | Path,
    out_step: str | Path | None = None,
    *,
    pcb_thk_mm: float = 1.6,
) -> Path:
    """Build a populated PCB STEP file from a BOM JSON.

    The PCB substrate is centered at the origin (XY plane) with bottom face
    at Z=0 and top face at Z=pcb_thk_mm. Components extrude upward from the
    top face. Mounting holes (4× M3 at 30.5 mm pitch) are cut through the
    substrate.

    Returns the path to the written STEP file.
    """
    import cadquery as cq
    from cadquery import Assembly, Color, Location, Vector

    bom_path = Path(bom_path)
    bom = json.loads(bom_path.read_text(encoding="utf-8"))
    components = bom.get("components", []) or []
    board_w = float(bom.get("board_w_mm", 0)) or _infer_board("w", components)
    board_h = float(bom.get("board_h_mm", 0)) or _infer_board("h", components)

    # PCB substrate — centered on origin (drone assembly expects this)
    substrate = (cq.Workplane("XY")
                 .box(board_w, board_h, pcb_thk_mm, centered=(True, True, False)))
    # Stack mounting holes (30.5 mm pitch, 3.2 mm clearance for M3)
    half = 30.5 / 2.0
    if board_w > 35 and board_h > 35:
        substrate = (substrate.faces(">Z").workplane()
                     .pushPoints([(+half, +half), (-half, +half),
                                  (-half, -half), (+half, -half)])
                     .hole(3.2))

    # Build cadquery Assembly so each component can have its own color,
    # then export combined STEP. Components positioned in BOM coordinates
    # (origin at PCB lower-left corner); shift to center for assembly.
    assy = Assembly(name=bom_path.stem)
    assy.add(substrate, name="pcb",
             loc=Location(Vector(0, 0, 0)),
             color=Color(0.05, 0.40, 0.18, 1.0))   # FR-4 green

    for c in components:
        ref = c.get("ref", "?")
        value = str(c.get("value", "")).strip()
        x = float(c.get("x_mm", 0))
        y = float(c.get("y_mm", 0))
        w = float(c.get("width_mm", 1))
        h = float(c.get("height_mm", 1))
        z_height = _component_height_mm(value, ref)

        # Skip degenerate
        if w <= 0 or h <= 0 or z_height <= 0:
            continue

        # Component box, centered on its (x+w/2, y+h/2) in BOM coords.
        # BOM coords have origin at lower-left of board; substrate is
        # centered on origin so we shift by -board/2.
        cx = x + w / 2.0 - board_w / 2.0
        cy = y + h / 2.0 - board_h / 2.0
        cz = pcb_thk_mm   # bottom of component sits ON top of PCB

        comp_solid = (cq.Workplane("XY")
                      .box(w, h, z_height, centered=(True, True, False)))

        rgb = _component_color(ref)
        assy.add(
            comp_solid,
            name=ref,
            loc=Location(Vector(cx, cy, cz)),
            color=Color(*rgb, 1.0),
        )

    # Export
    if out_step is None:
        out_step = bom_path.parent / f"{bom_path.stem}_populated.step"
    out_step = Path(out_step)
    assy.export(str(out_step), exportType="STEP")
    return out_step


def _infer_board(axis: str, components: list) -> float:
    if not components:
        return 50.0
    if axis == "w":
        return max((float(c.get("x_mm", 0)) + float(c.get("width_mm", 0))
                    for c in components), default=50.0) + 2.0
    return max((float(c.get("y_mm", 0)) + float(c.get("height_mm", 0))
                for c in components), default=50.0) + 2.0


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m aria_os.ecad.pcb_3d <bom.json> [out.step]")
        sys.exit(1)
    out = build_populated_pcb(sys.argv[1],
                              sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"Populated PCB STEP written: {out}")
