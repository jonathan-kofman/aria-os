"""
ecad_generator.py — KiCad PCB script generator.

Parses a natural-language board description (regex + keyword matching, no LLM)
and generates a pcbnew Python script the user runs inside KiCad's scripting
console.  Also writes a BOM JSON alongside.

Usage:
    from aria_os.ecad_generator import generate_ecad
    generate_ecad("ARIA ESP32 board, 80x60mm, 12V, UART, BLE")

    # CLI:
    python -m aria_os.ecad_generator "ARIA ESP32 board, 80x60mm, 12V, UART, BLE" --out outputs/ecad/
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
OUT_ECAD = ROOT / "outputs" / "ecad"

ECAD_BOM_SCHEMA_VERSION = "1.0"


# ─── Firmware pin extractor ───────────────────────────────────────────────────

def extract_firmware_pins(repo_root: Path) -> dict[str, str]:
    """
    Read STM32 and ESP32 firmware files and extract GPIO pin definitions.

    Patterns recognised:
      STM32 (aria_main.cpp):
        static constexpr uint8_t PIN_NAME = PA8;
        static constexpr uint8_t PIN_NAME = 13;
      ESP32 (aria_esp32_firmware.ino):
        #define PIN_NAME value
        #define PIN_NAME GPIO_NUM_X
        #define VARNAME_PIN value
        const int PIN_NAME = value;
        const uint8_t PIN_NAME = value;

    Returns dict like {"PIN_BRAKE": "PB13", "STM32_UART_TX": "43", ...}.
    Returns {} if firmware files are missing.
    """
    firmware_files = [
        repo_root / "device" / "firmware" / "stm32" / "aria_main.cpp",
        repo_root / "device" / "firmware" / "esp32" / "aria_esp32_firmware.ino",
    ]

    # Patterns in order of specificity:
    patterns = [
        # static constexpr uint8_t PIN_NAME = PA8;
        re.compile(
            r"static\s+constexpr\s+uint8_t\s+(PIN_\w+)\s*=\s*([A-Za-z0-9_]+)\s*;"
        ),
        # #define PIN_NAME value  or  #define VARNAME_GPIO_NUM value
        re.compile(
            r"^\s*#define\s+((?:[A-Z0-9_]*PIN[A-Z0-9_]*|[A-Z0-9_]*GPIO_NUM[A-Z0-9_]*))\s+(-?\d+|[A-Za-z][A-Za-z0-9_]*)\s*$",
            re.MULTILINE,
        ),
        # const int PIN_NAME = value;  or  const uint8_t PIN_NAME = value;
        re.compile(
            r"const\s+(?:int|uint8_t)\s+((?:[A-Z_]*PIN[A-Z_0-9]*|[A-Z_0-9]*_PIN))\s*=\s*(-?\d+|[A-Za-z][A-Za-z0-9_]*)\s*;"
        ),
    ]

    pins: dict[str, str] = {}

    for fw_path in firmware_files:
        if not fw_path.exists():
            continue
        try:
            text = fw_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pat in patterns:
            for m in pat.finditer(text):
                name, value = m.group(1), m.group(2)
                # Skip -1 sentinel values (camera "not connected")
                if value == "-1":
                    continue
                if name not in pins:
                    pins[name] = value

    return pins


# ─── Component descriptor ─────────────────────────────────────────────────────

@dataclass
class Component:
    ref: str
    value: str
    footprint: str           # KiCad footprint library:footprint
    width_mm: float          # bounding-box width for placement
    height_mm: float         # bounding-box height for placement
    description: str = ""
    qty: int = 1
    # placement filled in by placer
    x_mm: float = 0.0
    y_mm: float = 0.0


# ─── Parser ──────────────────────────────────────────────────────────────────

def _slug(description: str) -> str:
    """Convert a board description to a filesystem-safe name."""
    txt = description.lower()
    txt = re.sub(r"[^a-z0-9]+", "_", txt)
    txt = txt.strip("_")
    # Truncate to 48 chars so paths stay sane
    return txt[:48]


def parse_board_dimensions(description: str) -> tuple[float, float]:
    """Extract WxH in mm from description. Returns (80.0, 60.0) if not found."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[xX×]\s*(\d+(?:\.\d+)?)\s*mm", description)
    if m:
        return float(m.group(1)), float(m.group(2))
    return 80.0, 60.0


def parse_components(description: str) -> List[Component]:
    """
    Regex + keyword scan of the board description.
    Returns a list of Component descriptors, deduplicated.
    Each ref is auto-incremented per component type prefix.
    """
    lower = description.lower()
    components: List[Component] = []
    ref_counters: dict[str, int] = {}

    def next_ref(prefix: str) -> str:
        ref_counters[prefix] = ref_counters.get(prefix, 0) + 1
        return f"{prefix}{ref_counters[prefix]}"

    # ── MCU ──────────────────────────────────────────────────────────────────
    if "esp32" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="ESP32-S3-WROOM-1",
            footprint="RF_Module:ESP32-S3-WROOM-1",
            width_mm=18.0, height_mm=31.0,
            description="ESP32-S3 WiFi/BLE module",
        ))
    if "stm32" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="STM32F405RGT6",
            footprint="Package_QFP:LQFP-64_10x10mm_P0.5mm",
            width_mm=10.0, height_mm=10.0,
            description="STM32 ARM Cortex-M4 MCU",
        ))
    if "arduino" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="Arduino-Nano",
            footprint="Module:Arduino_Nano",
            width_mm=18.0, height_mm=43.0,
            description="Arduino Nano module",
        ))

    # ── Power supply ──────────────────────────────────────────────────────────
    if re.search(r"\b12\s*v\b", lower) or re.search(r"\b24\s*v\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="Barrel-Jack-2.1mm",
            footprint="Connector_BarrelJack:BarrelJack_Horizontal",
            width_mm=9.0, height_mm=11.0,
            description="DC barrel jack (12-24 V input)",
        ))
        components.append(Component(
            ref=next_ref("U"),
            value="AMS1117-3.3",
            footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            width_mm=3.5, height_mm=6.5,
            description="3.3 V LDO regulator",
        ))
    if re.search(r"\b5\s*v\b", lower) and "usb" not in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="USB-C-Receptacle",
            footprint="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            width_mm=9.0, height_mm=3.5,
            description="USB-C 5 V power / data",
        ))
        components.append(Component(
            ref=next_ref("U"),
            value="AMS1117-3.3",
            footprint="Package_TO_SOT_SMD:SOT-223-3_TabPin2",
            width_mm=3.5, height_mm=6.5,
            description="3.3 V LDO regulator",
        ))
    if re.search(r"\bbattery\b|\blipo\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="JST-PH-2P",
            footprint="Connector_JST:JST_PH_S2B-PH-K_1x02_P2.00mm_Horizontal",
            width_mm=6.0, height_mm=5.5,
            description="LiPo battery connector (JST-PH 2-pin)",
        ))
        components.append(Component(
            ref=next_ref("U"),
            value="TP4056",
            footprint="Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            width_mm=4.0, height_mm=5.0,
            description="LiPo BMS / charger IC",
        ))

    # ── Interface connectors ──────────────────────────────────────────────────
    if "uart" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-4P-UART",
            footprint="Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
            width_mm=10.0, height_mm=7.5,
            description="UART header (TX/RX/3V3/GND)",
        ))
    if "i2c" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-4P-I2C",
            footprint="Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
            width_mm=10.0, height_mm=7.5,
            description="I2C header (SDA/SCL/3V3/GND)",
        ))
    if "spi" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-6P-SPI",
            footprint="Connector_JST:JST_XH_B6B-XH-A_1x06_P2.50mm_Vertical",
            width_mm=15.0, height_mm=7.5,
            description="SPI header (MOSI/MISO/SCK/CS/3V3/GND)",
        ))
    if "usb" in lower:
        components.append(Component(
            ref=next_ref("J"),
            value="USB-C-Receptacle",
            footprint="Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12",
            width_mm=9.0, height_mm=3.5,
            description="USB-C connector",
        ))

    # ── BLE / RF ──────────────────────────────────────────────────────────────
    # BLE antenna keepout is modelled as a copper-free zone, not a discrete part.
    # We add a marker component so the pcbnew script can draw it.
    if re.search(r"\bble\b|\bbluetooth\b|\bantenna\b", lower):
        components.append(Component(
            ref=next_ref("ANT"),
            value="BLE-Antenna-Keepout",
            footprint="RF_Antenna:Antenna_Chip_TDK_ANT1608AI_0603",
            width_mm=3.2, height_mm=1.6,
            description="BLE antenna keepout zone marker",
        ))

    # ── Sensors ───────────────────────────────────────────────────────────────
    if "hx711" in lower or "load cell" in lower:
        components.append(Component(
            ref=next_ref("U"),
            value="HX711",
            footprint="Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
            width_mm=4.0, height_mm=10.0,
            description="HX711 24-bit load cell ADC",
        ))
        components.append(Component(
            ref=next_ref("J"),
            value="JST-XH-4P-LoadCell",
            footprint="Connector_JST:JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical",
            width_mm=10.0, height_mm=7.5,
            description="Load cell connector (E+/E-/A+/A-)",
        ))
    if re.search(r"\bhall\b", lower):
        components.append(Component(
            ref=next_ref("U"),
            value="SS49E-Hall",
            footprint="Package_TO_SOT_THT:TO-92_Inline",
            width_mm=5.0, height_mm=5.0,
            description="Hall effect sensor",
        ))
    if re.search(r"\bimu\b|\bmpu6050\b", lower):
        components.append(Component(
            ref=next_ref("U"),
            value="MPU-6050",
            footprint="Package_LGA:LGA-24_4x4mm_P0.5mm",
            width_mm=4.0, height_mm=4.0,
            description="MPU-6050 6-axis IMU",
        ))

    # ── Motor / actuator connectors ───────────────────────────────────────────
    if re.search(r"\bvesc\b|\bmotor\b|\bstepper\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="Molex-6P-Motor",
            footprint="Connector_Molex:Molex_Mini-Fit_Jr_5566-06A2_2x03_P4.20mm_Vertical",
            width_mm=12.6, height_mm=10.0,
            description="Motor / VESC 6-pin power connector",
        ))
    if re.search(r"\bservo\b", lower):
        components.append(Component(
            ref=next_ref("J"),
            value="Servo-3P-PWM",
            footprint="Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
            width_mm=7.62, height_mm=2.54,
            description="Servo PWM 3-pin header",
        ))

    # ── Passives: always add decoupling caps and a power LED ─────────────────
    components.append(Component(
        ref=next_ref("C"),
        value="100nF",
        footprint="Capacitor_SMD:C_0402_1005Metric",
        width_mm=1.0, height_mm=0.5,
        description="100 nF decoupling capacitor",
        qty=4,
    ))
    components.append(Component(
        ref=next_ref("C"),
        value="10uF",
        footprint="Capacitor_SMD:C_0805_2012Metric",
        width_mm=2.0, height_mm=1.25,
        description="10 µF bulk decoupling capacitor",
        qty=2,
    ))
    components.append(Component(
        ref=next_ref("D"),
        value="LED-Green",
        footprint="LED_SMD:LED_0402_1005Metric",
        width_mm=1.0, height_mm=0.5,
        description="Power indicator LED",
    ))
    components.append(Component(
        ref=next_ref("R"),
        value="330R",
        footprint="Resistor_SMD:R_0402_1005Metric",
        width_mm=1.0, height_mm=0.5,
        description="LED current-limiting resistor",
    ))

    return components


# ─── Placer ──────────────────────────────────────────────────────────────────

def place_components(components: List[Component], board_w: float, board_h: float) -> None:
    """
    Simple grid placement.
    - First MCU ("U1") is centred on the board.
    - Power connectors go top-right.
    - All other connectors flow around the perimeter (left column then right).
    - Small passives fill rows below the MCU.
    Modifies Component.x_mm / .y_mm in-place.
    """
    MARGIN = 5.0
    GAP    = 2.0

    mcu        = [c for c in components if c.ref.startswith("U") and c.height_mm > 15]
    power_j    = [c for c in components if c.ref.startswith("J") and ("barrel" in c.description.lower() or "usb" in c.description.lower() or "lipo" in c.description.lower())]
    sig_j      = [c for c in components if c.ref.startswith("J") and c not in power_j]
    ics        = [c for c in components if c.ref.startswith("U") and c not in mcu]
    passives   = [c for c in components if c.ref[0] in ("C", "R", "D", "ANT")]

    # MCU centred
    for comp in mcu:
        comp.x_mm = (board_w - comp.width_mm) / 2.0
        comp.y_mm = (board_h - comp.height_mm) / 2.0

    # Power connectors: top-right corner, stacked vertically
    cur_x = board_w - MARGIN
    cur_y = MARGIN
    for comp in power_j:
        comp.x_mm = cur_x - comp.width_mm
        comp.y_mm = cur_y
        cur_y += comp.height_mm + GAP

    # Signal connectors: left edge, top-to-bottom
    cur_x = MARGIN
    cur_y = MARGIN
    for comp in sig_j:
        comp.x_mm = cur_x
        comp.y_mm = cur_y
        cur_y += comp.height_mm + GAP
        if cur_y + comp.height_mm > board_h - MARGIN:
            cur_y = MARGIN
            cur_x += comp.width_mm + GAP

    # ICs: below/beside MCU, right side
    cx = board_w * 0.65
    cy = MARGIN
    for comp in ics:
        comp.x_mm = min(cx, board_w - MARGIN - comp.width_mm)
        comp.y_mm = cy
        cy += comp.height_mm + GAP

    # Passives: bottom row
    px = MARGIN
    py = board_h - MARGIN - 4.0
    for comp in passives:
        comp.x_mm = px
        comp.y_mm = py
        px += comp.width_mm + GAP
        if px + comp.width_mm > board_w - MARGIN:
            px = MARGIN
            py -= 4.0


# ─── pcbnew script generation ─────────────────────────────────────────────────

_PCBNEW_HEADER = '''\
"""
Auto-generated KiCad pcbnew script.
Generated by aria_os/ecad_generator.py

Board:  {board_name}
Size:   {board_w} x {board_h} mm

HOW TO USE:
  1. Open KiCad 7, create or open a project.
  2. Open PCB Editor (pcbnew).
  3. Tools → Scripting Console → paste or exec this file:
         exec(open(r"{script_path}").read())
  4. The board outline, footprints, mounting holes, and labels are added
     automatically.  Press Ctrl+Z to undo if needed.
"""
import pcbnew

BOARD_W_MM = {board_w}
BOARD_H_MM = {board_h}

board = pcbnew.GetBoard()

def mm(v):
    return pcbnew.FromMM(v)

# ── Board outline (Edge.Cuts) ──────────────────────────────────────────────────
def add_outline(board, w, h):
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        seg = pcbnew.PCB_SHAPE(board)
        seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
        seg.SetLayer(pcbnew.Edge_Cuts)
        seg.SetStart(pcbnew.VECTOR2I(mm(x1), mm(y1)))
        seg.SetEnd(pcbnew.VECTOR2I(mm(x2), mm(y2)))
        seg.SetWidth(mm(0.05))
        board.Add(seg)

add_outline(board, BOARD_W_MM, BOARD_H_MM)

# ── Mounting holes (M3, 4 corners, 3 mm from edge) ────────────────────────────
HOLE_OFFSET = 3.0
HOLE_DIA    = 3.2   # M3 clearance

def add_mounting_hole(board, x, y):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("MH")
    fp.SetValue("MountingHole_3.2mm")
    fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))

    pad = pcbnew.PAD(fp)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_NPTH)
    pad.SetSize(pcbnew.VECTOR2I(mm(HOLE_DIA), mm(HOLE_DIA)))
    pad.SetDrillSize(pcbnew.VECTOR2I(mm(HOLE_DIA), mm(HOLE_DIA)))
    pad.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    fp.Add(pad)
    board.Add(fp)

add_mounting_hole(board, HOLE_OFFSET,             HOLE_OFFSET)
add_mounting_hole(board, BOARD_W_MM - HOLE_OFFSET, HOLE_OFFSET)
add_mounting_hole(board, HOLE_OFFSET,             BOARD_H_MM - HOLE_OFFSET)
add_mounting_hole(board, BOARD_W_MM - HOLE_OFFSET, BOARD_H_MM - HOLE_OFFSET)

# ── Helper: add text label on silkscreen ──────────────────────────────────────
def silk_label(board, x, y, text, size_mm=1.0):
    t = pcbnew.PCB_TEXT(board)
    t.SetText(text)
    t.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    t.SetLayer(pcbnew.F_SilkS)
    t.SetTextSize(pcbnew.VECTOR2I(mm(size_mm), mm(size_mm)))
    t.SetTextThickness(mm(0.15))
    board.Add(t)

# Board title label
silk_label(board, 2.0, BOARD_H_MM - 3.5, "{board_name}", size_mm=1.2)

# ── Helper: add footprint placeholder ─────────────────────────────────────────
def add_fp(board, ref, value, fp_id, x, y, desc=""):
    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(value)
    fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y)))
    # Silkscreen reference label
    silk_label(board, x, y - 1.5, ref, size_mm=0.8)
    board.Add(fp)
    return fp

'''

_PCBNEW_FOOTER = '''\

# ── Refresh and save ───────────────────────────────────────────────────────────
pcbnew.Refresh()
board.Save(r"{kicad_pcb_path}")
print("[ecad] Saved:", r"{kicad_pcb_path}")
'''


def _build_pcbnew_script(
    board_name: str,
    board_w: float,
    board_h: float,
    components: List[Component],
    script_path: str,
    kicad_pcb_path: str,
) -> str:
    lines: list[str] = []
    lines.append(_PCBNEW_HEADER.format(
        board_name=board_name,
        board_w=board_w,
        board_h=board_h,
        script_path=script_path.replace("\\", "\\\\"),
    ))

    lines.append("# ── Components ───────────────────────────────────────────────────────────────")
    for comp in components:
        fp_esc = comp.footprint.replace('"', '\\"')
        desc_esc = comp.description.replace('"', '\\"')
        lines.append(
            f'add_fp(board, "{comp.ref}", "{comp.value}", '
            f'"{fp_esc}", {comp.x_mm:.2f}, {comp.y_mm:.2f}, "{desc_esc}")'
        )

    lines.append("")
    lines.append(_PCBNEW_FOOTER.format(
        kicad_pcb_path=kicad_pcb_path.replace("\\", "\\\\"),
    ))

    return "\n".join(lines)


# ─── BOM generation ──────────────────────────────────────────────────────────

def build_bom(components: List[Component]) -> dict:
    return {
        "schema_version": ECAD_BOM_SCHEMA_VERSION,
        "components": [
            {
                "ref":       c.ref,
                "value":     c.value,
                "footprint": c.footprint,
                "description": c.description,
                "qty":       c.qty,
                "x_mm":      round(c.x_mm, 2),
                "y_mm":      round(c.y_mm, 2),
            }
            for c in components
        ]
    }


# ─── Public API ──────────────────────────────────────────────────────────────

def _build_firmware_pin_comment_block(pins: dict[str, str]) -> str:
    """
    Return a Python comment block listing firmware pin assignments, suitable
    for injection near the top of the generated pcbnew script.
    """
    if not pins:
        return "# Firmware pin assignments: none found (firmware files missing or no pins matched)\n"
    lines = [
        "# Firmware pin assignments (from aria_main.cpp / aria_esp32_firmware.ino):",
    ]
    for name, value in sorted(pins.items()):
        lines.append(f"# {name} = {value}")
    lines.append("")
    return "\n".join(lines)


def generate_ecad(description: str, out_dir: Path | None = None) -> tuple[Path, Path]:
    """
    Parse description, place components, write pcbnew script + BOM JSON.

    Returns (pcbnew_script_path, bom_json_path).
    """
    board_name = _slug(description)
    board_w, board_h = parse_board_dimensions(description)
    components = parse_components(description)
    place_components(components, board_w, board_h)

    # Extract firmware pin definitions and report
    fw_pins = extract_firmware_pins(ROOT)
    print(f"[ecad] Firmware pins found: {len(fw_pins)}")

    out_dir = (out_dir or OUT_ECAD) / board_name
    out_dir.mkdir(parents=True, exist_ok=True)

    script_path   = out_dir / f"{board_name}_pcbnew.py"
    bom_path      = out_dir / f"{board_name}_bom.json"
    kicad_pcb     = str(out_dir / f"{board_name}.kicad_pcb")

    pcbnew_src = _build_pcbnew_script(
        board_name=board_name,
        board_w=board_w,
        board_h=board_h,
        components=components,
        script_path=str(script_path),
        kicad_pcb_path=kicad_pcb,
    )

    # Inject firmware pin comment block immediately after the opening docstring /
    # import block (before the board-outline helpers).  We insert it just before
    # the first "BOARD_W_MM" assignment so it is visible at the top of the script.
    pin_block = _build_firmware_pin_comment_block(fw_pins)
    pcbnew_src = pcbnew_src.replace(
        "BOARD_W_MM = ",
        pin_block + "BOARD_W_MM = ",
        1,
    )

    script_path.write_text(pcbnew_src, encoding="utf-8")

    bom = build_bom(components)
    bom["firmware_pins"] = fw_pins
    bom_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

    print(f"[ecad] Board:      {board_name}  ({board_w} x {board_h} mm)")
    print(f"[ecad] Components: {len(components)}")
    for c in components:
        print(f"[ecad]   {c.ref:6s} {c.value:<30s} @ ({c.x_mm:.1f}, {c.y_mm:.1f}) mm")
    print(f"[ecad] Script:     {script_path}")
    print(f"[ecad] BOM:        {bom_path}")

    # ── Validation pipeline ────────────────────────────────────────────────────
    try:
        from .ecad_validator import run_full_check
        from .ecad_simulator import simulate
        from .ecad_pin_checker import check as pin_check

        # Convert Component dataclasses to dicts for the validators
        def _components_to_dicts(comp_list: List[Component]) -> list[dict]:
            return [
                {
                    "ref":         c.ref,
                    "value":       c.value,
                    "description": c.description,
                    "x_mm":        c.x_mm,
                    "y_mm":        c.y_mm,
                    "width_mm":    c.width_mm,
                    "height_mm":   c.height_mm,
                    "pins":        [],   # pcbnew generator does not assign per-pin nets
                }
                for c in comp_list
            ]

        comp_dicts = _components_to_dicts(components)

        # Run validation with optional retry on ERC/DRC errors (up to 2 retries)
        _retry_count = 0
        _MAX_RETRIES = 2
        while True:
            _erc_result = run_full_check(
                description, comp_dicts, fw_pins, board_w, board_h,
                out_dir=out_dir,
            )

            _has_errors = bool(_erc_result.get("errors"))

            # Attempt LLM-assisted fix if errors are present and retries remain
            if _has_errors and _retry_count < _MAX_RETRIES:
                _llm_available = False
                try:
                    from aria_os.llm_client import call_llm, get_anthropic_key, get_google_key
                    _llm_available = bool(get_anthropic_key(ROOT) or get_google_key(ROOT))
                except ImportError:
                    pass

                if _llm_available:
                    _retry_count += 1
                    previous_failures = _erc_result["errors"]
                    _failure_block = "\n".join(f"  - {e}" for e in previous_failures)
                    _retry_prompt = (
                        f"The following ERC/DRC errors were found in a KiCad board generated "
                        f"for this description:\n  {description}\n\n"
                        f"Errors (attempt {_retry_count}):\n{_failure_block}\n\n"
                        f"List the missing or incorrect components that must be added or changed "
                        f"to fix these errors. Reply with a concise bullet list only."
                    )
                    print(
                        f"[ECAD] Validation FAIL — {len(previous_failures)} error(s); "
                        f"retrying with LLM guidance (attempt {_retry_count}/{_MAX_RETRIES}) ..."
                    )
                    try:
                        _llm_advice = call_llm(_retry_prompt, repo_root=ROOT)
                    except Exception:
                        _llm_advice = None

                    # Inject failure context into the description for re-parse so
                    # parse_components picks up any newly needed keywords.
                    # This mirrors the previous_failures injection pattern used by
                    # post_gen_validator._call_generate_fn().
                    if _llm_advice:
                        _augmented_desc = (
                            description
                            + f"\n[RETRY {_retry_count} — previous failures: "
                            + "; ".join(previous_failures)
                            + f"\nLLM advice: {_llm_advice}]"
                        )
                    else:
                        _augmented_desc = (
                            description
                            + f"\n[RETRY {_retry_count} — previous failures: "
                            + "; ".join(previous_failures) + "]"
                        )

                    # Re-parse components with augmented description and re-place
                    _retry_components = parse_components(_augmented_desc)
                    place_components(_retry_components, board_w, board_h)
                    comp_dicts = _components_to_dicts(_retry_components)
                    # Update the live components list so BOM reflects the fix
                    components = _retry_components
                    continue  # re-run validation loop

            # No more retries or no errors — exit loop
            break

        _sim_result = simulate(description, comp_dicts)
        _pin_result = pin_check(ROOT, comp_dicts)

        bom["validation"] = {
            "erc":           _erc_result,
            "simulation":    _sim_result,
            "pin_conflicts": _pin_result,
            "passed": (
                _erc_result["passed"]
                and _sim_result["passed"]
                and _pin_result["passed"]
            ),
        }

        # Re-write BOM with validation results included
        bom_path.write_text(json.dumps(bom, indent=2), encoding="utf-8")

        _n_errors = len(_erc_result.get("errors", []))
        _val_status = "PASS" if bom["validation"]["passed"] else "FAIL"
        print(f"[ECAD] Validation {_val_status} — {_n_errors} error(s)")

    except ImportError as _ie:
        # Validation modules not yet available — skip gracefully
        print(f"[ecad] Validation skipped (import error: {_ie})")

    # ──────────────────────────────────────────────────────────────────────────

    return script_path, bom_path


# ─── CLI ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate a KiCad pcbnew script from a natural-language board description"
    )
    parser.add_argument("description", type=str,
                        help='Board description, e.g. "ARIA ESP32 board, 80x60mm, 12V, UART, BLE"')
    parser.add_argument("--out", type=Path, default=None,
                        help="Output parent directory (default: outputs/ecad/)")
    args = parser.parse_args()

    generate_ecad(args.description, out_dir=args.out)
